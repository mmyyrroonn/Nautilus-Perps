"""Native Ondo continuous execution and an event-confirmed startup check.

Construction never starts a node or loads credentials. The startup check uses native
Strategy order commands; authentication, order serialization, admission, reconciliation
and DMS shutdown belong to the Rust adapter. Trading policy belongs to the application.
"""

from __future__ import annotations

import ipaddress
import functools
import time
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from nautilus_trader.common import Environment, LoggerConfig, LogLevel
from nautilus_trader.config import LiveRiskEngineConfig
from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
from nautilus_trader.model import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    Money,
    OrderFilled,
    OrderSide,
    QuoteTick,
    TimeInForce,
    TraderId,
)
from nautilus_trader.trading import Strategy, StrategyConfig


def exact(value: str | Decimal) -> Decimal:
    if not isinstance(value, (str, Decimal)):
        raise ValueError("monetary values must be exact decimal strings")
    number = Decimal(value)
    if not number.is_finite():
        raise ValueError("monetary values must be finite")
    return number


@dataclass(frozen=True)
class ContinuousLimits:
    """Caller-allocated Ondo venue budget, passed unchanged into native admission.

    A shared application allocator must reserve this budget against its global
    cross-venue exposure cap. This adapter cannot observe other venue accounts.
    """

    max_notional_per_order_usd: str = "20"
    max_gross_exposure_usd: str = "40"
    min_available_margin_usdc: str = "25"
    max_live_orders: int = 2
    max_requests_per_second: int = 2
    max_unresolved_orders: int = 1

    def validate(self) -> None:
        order, gross, margin = map(
            exact,
            (
                self.max_notional_per_order_usd,
                self.max_gross_exposure_usd,
                self.min_available_margin_usdc,
            ),
        )
        if not (Decimal(0) < order <= 100 and order <= gross <= 500 and margin > 0):
            raise ValueError(
                "continuous order, gross or margin limits exceed the native contract"
            )
        if (
            any(
                type(value) is not int or value <= 0
                for value in (
                    self.max_live_orders,
                    self.max_requests_per_second,
                    self.max_unresolved_orders,
                )
            )
            or self.max_unresolved_orders > self.max_live_orders
        ):
            raise ValueError(
                "continuous order/request/unresolved counts must be positive and consistent"
            )


@dataclass(frozen=True)
class ContinuousSession:
    """Explicit node construction inputs; default construction is sandbox read-only."""

    instrument_ids: tuple[str, ...] = ("BTC-USD-PERP.ONDO",)
    account_id: str = "ONDO-CONTINUOUS-001"
    environment: str = "sandbox"
    execution_enabled: bool = False
    expected_venue_account_id: str | None = None
    diagnostics_run_id: str | None = None
    journal_path: Path | None = None
    dms_timeout_secs: int = 30
    reconcile_interval_secs: int = 1
    limits: ContinuousLimits = field(default_factory=ContinuousLimits)
    base_url_http: str | None = None
    base_url_ws: str | None = None
    api_key: str | None = field(default=None, repr=False)
    api_secret: str | None = field(default=None, repr=False)
    mainnet_authorized: bool = False

    def validate(self) -> None:
        self.limits.validate()
        if self.environment not in {"sandbox", "production"}:
            raise ValueError("unknown Ondo environment")
        if not self.instrument_ids or len(set(self.instrument_ids)) != len(
            self.instrument_ids
        ):
            raise ValueError("explicit unique instruments are required")
        if any(not name.endswith("-USD-PERP.ONDO") for name in self.instrument_ids):
            raise ValueError("continuous instruments must be Ondo USD perpetuals")
        if not self.execution_enabled:
            return
        if not all(
            (self.expected_venue_account_id, self.diagnostics_run_id, self.journal_path)
        ):
            raise ValueError(
                "execution requires explicit account identity, run id and journal"
            )
        if (
            type(self.dms_timeout_secs) is not int
            or not 1 <= self.dms_timeout_secs <= 30
        ):
            raise ValueError("DMS timeout must be within 1..30 seconds")
        if self.reconcile_interval_secs != 1:
            raise ValueError(
                "continuous execution requires one-second native reconciliation"
            )
        if self.environment == "production" and not self.mainnet_authorized:
            # A synthetic production-contract test must use two explicit loopback endpoints.
            # This exception cannot connect to a real account and does not load an .env file.
            if not (_loopback(self.base_url_http) and _loopback(self.base_url_ws)):
                raise ValueError(
                    "production execution requires current-turn mainnet authorization"
                )


def _loopback(url: str | None) -> bool:
    if not url:
        return False
    parsed = urlsplit(url)
    try:
        return (
            parsed.scheme in {"http", "ws"}
            and ipaddress.ip_address(parsed.hostname).is_loopback
        )
    except (TypeError, ValueError):
        return False


def minimum_quantity(instrument, protected_buy_price: Decimal, cap: Decimal) -> Decimal:
    """Smallest quantity supported by current native instrument metadata, within the cap."""
    step = instrument.size_increment.as_decimal()
    tick = instrument.price_increment.as_decimal()
    if step <= 0 or tick <= 0 or protected_buy_price <= 0:
        raise ValueError("instrument increments and protected price must be positive")
    minimum = instrument.min_quantity
    quantity = max(step, minimum.as_decimal() if minimum is not None else step)
    notional = instrument.min_notional
    if notional is not None:
        if str(notional.currency) not in {"USD", "USDC"}:
            raise ValueError("unsupported minimum notional currency")
        quantity = max(quantity, notional.as_decimal() / protected_buy_price)
    quantity = (quantity / step).to_integral_value(rounding=ROUND_CEILING) * step
    maximum = instrument.max_quantity
    if maximum is not None and quantity > maximum.as_decimal():
        raise ValueError("minimum startup quantity exceeds instrument maximum")
    if quantity * protected_buy_price > cap:
        raise ValueError("minimum startup quantity exceeds the approved order notional")
    return quantity


class ContinuousStrategyConfig(StrategyConfig):
    _CUSTOM_FIELDS = (
        "instrument_id",
        "account_id",
        "startup_timeout_secs",
        "slippage_bps",
    )

    def __new__(cls, *args, **kwargs):
        for name in cls._CUSTOM_FIELDS:
            kwargs.pop(name, None)
        return super().__new__(cls, *args, **kwargs)

    def __init__(
        self,
        instrument_id: InstrumentId,
        account_id: AccountId,
        startup_timeout_secs: int = 120,
        slippage_bps: str = "20",
        **_kwargs,
    ):
        super().__init__()
        self.instrument_id = instrument_id
        self.account_id = account_id
        self.startup_timeout_secs = startup_timeout_secs
        self.slippage_bps = slippage_bps


def _guarded_callback(method):
    """Retain callback failures that the native event loop otherwise logs and consumes."""

    @functools.wraps(method)
    def invoke(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as exc:
            self._fail(f"{method.__name__}: {type(exc).__name__}: {exc}")

    return invoke


class OndoContinuousStrategy(Strategy):
    """Check cancel/replace/IOC/RO once, then serve policy callbacks in this same Strategy.

    Post-only orders must be confirmed cancelled without fills before a replacement
    is created. A maker fill/cancel race fails the startup check rather than replaying
    or guessing exposure. Native shutdown remains responsible for owned cleanup.
    """

    def __new__(cls, config: ContinuousStrategyConfig, **_kwargs):
        return super().__new__(cls, config)

    def __init__(
        self,
        config: ContinuousStrategyConfig,
        *,
        session: ContinuousSession,
        execution_factory,
        on_continuous_quote: Callable | None = None,
    ) -> None:
        super().__init__(config)
        session.validate()
        if str(config.instrument_id) not in session.instrument_ids:
            raise ValueError(
                "startup instrument is not in the native continuous instrument list"
            )
        if config.startup_timeout_secs <= 0 or not 0 < exact(config.slippage_bps) <= 20:
            raise ValueError(
                "startup timeout must be positive and price protection within 20 bps"
            )
        self.session = session
        self._configured_ids = tuple(
            InstrumentId.from_str(name) for name in session.instrument_ids
        )
        self.execution_factory = execution_factory
        self.on_continuous_quote = on_continuous_quote
        self.phase = "disabled" if not session.execution_enabled else "awaiting_native"
        self.startup_verified = False
        self.failures: list[str] = []
        self.operations: list[dict] = []
        self.fills: dict[tuple[str, str], dict] = {}
        self._orders: dict[str, str] = {}
        self._active: str | None = None
        self._last_quote: QuoteTick | None = None
        self._quotes_by_instrument: dict[InstrumentId, QuoteTick] = {}
        self._instrument = None
        self._quantity: Decimal | None = None
        self._started: float | None = None
        self._timer_name = "ondo-continuous-startup"
        self._entry_price: Decimal | None = None
        self._maker_price: Decimal | None = None
        self._startup_ids: dict[str, str] = {}

    @_guarded_callback
    def on_start(self) -> None:
        self._started = time.monotonic()
        self._instrument = self.cache.instrument(self.config.instrument_id)
        if self._instrument is None:
            self._fail("native instrument metadata is unavailable")
            return
        if not self.session.execution_enabled:
            return
        for instrument_id in self._configured_ids:
            self.subscribe_quotes(instrument_id)
        self.clock.set_timer(
            self._timer_name, timedelta(milliseconds=100), callback=self._tick
        )

    @_guarded_callback
    def on_stop(self) -> None:
        if self._timer_name in self.clock.timer_names():
            self.clock.cancel_timer(self._timer_name)
        # Cancels are native Strategy commands. No replacement or RO order is guessed here.
        for name in self._orders:
            order = self.cache.order(ClientOrderId.from_str(name))
            if order is not None and not order.is_closed:
                self.cancel_order(order.client_order_id)

    def _fail(self, reason: str) -> None:
        if reason not in self.failures:
            self.failures.append(reason)
        self.phase = "failed"

    def _snapshot(self) -> dict | None:
        snapshot = self.execution_factory.production_trade_snapshot()
        return snapshot if isinstance(snapshot, dict) else None

    def _native_ready(self, *, flat: bool = False, reduce_only: bool = False) -> bool:
        snapshot = self._snapshot()
        if (
            snapshot is None
            or snapshot.get("run_id") != self.session.diagnostics_run_id
        ):
            return False
        now = int(self.clock.timestamp_ns())
        observed = snapshot.get("snapshot_unix_nanos", 0)
        return bool(
            0 < observed <= now
            and now - observed <= 15_000_000_000
            and snapshot.get("mode") == "continuous"
            and (reduce_only or snapshot.get("coverage_complete") is True)
            and snapshot.get("reduction_ready" if reduce_only else "native_ready")
            is True
            and snapshot.get("metadata_fresh") is True
            and snapshot.get("account_positions_owned") is True
            and snapshot.get("reconciled_activity_generation")
            == snapshot.get("latest_activity_generation")
            and snapshot.get("foreign_open_orders") == 0
            and snapshot.get("unknown_submissions") == 0
            and (
                not flat
                or (
                    snapshot.get("reconciled_flat") is True
                    and snapshot.get("own_open_orders") == 0
                )
            )
        )

    @_guarded_callback
    def on_quote(self, quote: QuoteTick) -> None:
        if quote.instrument_id not in self._configured_ids:
            return
        now = int(self.clock.timestamp_ns())
        if not (0 < quote.ts_init <= now and now - quote.ts_init <= 2_000_000_000):
            return
        if (
            quote.bid_price.as_decimal() <= 0
            or quote.ask_price < quote.bid_price
            or quote.bid_size.as_decimal() <= 0
            or quote.ask_size.as_decimal() <= 0
        ):
            return
        self._quotes_by_instrument[quote.instrument_id] = quote
        if quote.instrument_id == self.config.instrument_id:
            # BTC remains the startup context. Other allowed quotes cannot replace its
            # tick size, minimum quantity or protected entry/close observation.
            self._last_quote = quote
            self._advance()
        if (
            self.startup_verified
            and self.phase == "continuous"
            and self.on_continuous_quote
        ):
            self.on_continuous_quote(self, quote)

    @_guarded_callback
    def _tick(self, _event) -> None:
        if (
            not self.startup_verified
            and self.phase not in {"disabled", "failed"}
            and self._started is not None
            and time.monotonic() - self._started > self.config.startup_timeout_secs
        ):
            self._fail(
                "startup check exceeded its application timeout; no commands replayed"
            )
            return
        self._advance()

    def _protected_price(
        self, side: OrderSide, instrument_id: InstrumentId | None = None
    ) -> Decimal:
        target = instrument_id or self.config.instrument_id
        quote = self._quotes_by_instrument.get(target)
        if (
            quote is None
            or int(self.clock.timestamp_ns()) - quote.ts_init > 2_000_000_000
        ):
            raise ValueError("fresh native quote is required")
        instrument = self.cache.instrument(target)
        if instrument is None:
            raise ValueError("native instrument metadata is required")
        tick = instrument.price_increment.as_decimal()
        protection = exact(self.config.slippage_bps) / 10_000
        if side == OrderSide.BUY:
            touch = quote.ask_price.as_decimal()
            return ((touch * (1 + protection)) / tick).to_integral_value(
                rounding=ROUND_FLOOR
            ) * tick
        touch = quote.bid_price.as_decimal()
        return ((touch * (1 - protection)) / tick).to_integral_value(
            rounding=ROUND_CEILING
        ) * tick

    def _advance(self) -> None:
        if (
            self.phase in {"failed", "disabled", "continuous"}
            or self._last_quote is None
        ):
            return
        now = int(self.clock.timestamp_ns())
        if not 0 <= now - self._last_quote.ts_init <= 2_000_000_000:
            return
        if self.phase == "awaiting_native":
            if not self._native_ready(flat=True):
                return
            if self.cache.positions_open(
                instrument_id=self.config.instrument_id,
                account_id=self.config.account_id,
            ):
                self._fail("startup requires native and application account Flat")
                return
            try:
                protected = self._protected_price(OrderSide.BUY)
                self._maker_price = (
                    self._last_quote.bid_price.as_decimal()
                    - 2 * self._instrument.price_increment.as_decimal()
                )
                minimum_price = min(
                    self._maker_price - self._instrument.price_increment.as_decimal(),
                    self._protected_price(OrderSide.SELL),
                )
                self._quantity = minimum_quantity(
                    self._instrument,
                    minimum_price,
                    exact(self.session.limits.max_notional_per_order_usd),
                )
                if self._quantity * protected > exact(
                    self.session.limits.max_notional_per_order_usd
                ):
                    raise ValueError(
                        "protected IOC minimum exceeds the approved order notional"
                    )
                self._submit_startup(
                    "maker",
                    OrderSide.BUY,
                    self._quantity,
                    self._maker_price,
                    TimeInForce.GTC,
                    post_only=True,
                )
            except ValueError as exc:
                self._fail(str(exc))
            return
        if self.phase == "awaiting_replacement_quote":
            if not self._native_ready(flat=True):
                return
            price = min(
                self._last_quote.bid_price.as_decimal()
                - 3 * self._instrument.price_increment.as_decimal(),
                self._maker_price - self._instrument.price_increment.as_decimal(),
            )
            self._submit_startup(
                "replacement",
                OrderSide.BUY,
                self._quantity,
                price,
                TimeInForce.GTC,
                post_only=True,
            )
        elif self.phase == "awaiting_entry_quote" and self._native_ready(flat=True):
            self._submit_startup(
                "entry",
                OrderSide.BUY,
                self._quantity,
                self._protected_price(OrderSide.BUY),
                TimeInForce.IOC,
            )
        elif self.phase == "awaiting_close_quote":
            if not self._native_ready(reduce_only=True):
                return
            entry = self.cache.order(ClientOrderId.from_str(self._startup_ids["entry"]))
            if (
                entry is not None
                and entry.is_closed
                and entry.filled_qty.as_decimal() > 0
            ):
                self._submit_startup(
                    "close",
                    OrderSide.SELL,
                    entry.filled_qty.as_decimal(),
                    self._protected_price(OrderSide.SELL),
                    TimeInForce.IOC,
                    reduce_only=True,
                )
        elif self.phase == "awaiting_flat" and self._native_ready(flat=True):
            if self.cache.positions_open(
                instrument_id=self.config.instrument_id,
                account_id=self.config.account_id,
            ):
                return
            if self.portfolio.net_position(
                self.config.instrument_id, self.config.account_id
            ) != Decimal(0):
                return
            for role in ("entry", "close"):
                order = self.cache.order(
                    ClientOrderId.from_str(self._startup_ids[role])
                )
                fills = [fill for fill in self.fills.values() if fill["role"] == role]
                if (
                    order is None
                    or not fills
                    or sum((exact(fill["quantity"]) for fill in fills), Decimal(0))
                    != order.filled_qty.as_decimal()
                    or any(
                        fill["commission"] is None or fill["currency"] is None
                        for fill in fills
                    )
                ):
                    return
            self.startup_verified = True
            self.phase = "continuous"

    def submit_limit(
        self,
        role: str,
        side: OrderSide,
        quantity: Decimal,
        price: Decimal,
        time_in_force: TimeInForce,
        *,
        post_only: bool = False,
        reduce_only: bool = False,
        instrument_id: InstrumentId | None = None,
    ) -> str:
        """Policy hook: submit via this real Strategy; native admission remains authoritative."""
        if self.failures or not self.session.execution_enabled:
            raise ValueError("execution is disabled or the startup check failed")
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and limit price must be positive")
        target = instrument_id or self.config.instrument_id
        if target not in self._configured_ids:
            raise ValueError(
                "policy instrument is outside the native configured instrument list"
            )
        instrument = self.cache.instrument(target)
        if instrument is None:
            raise ValueError("policy instrument metadata is unavailable")
        order = self.order_factory.limit(
            instrument_id=target,
            order_side=side,
            quantity=instrument.make_qty(quantity),
            price=instrument.make_price(price),
            time_in_force=time_in_force,
            post_only=post_only,
            reduce_only=reduce_only,
        )
        name = str(order.client_order_id)
        self._orders[name] = role
        self.operations.append(
            {
                "operation": "create",
                "role": role,
                "client_order_id": name,
                "instrument_id": str(target),
                "requested_unix_nanos": int(self.clock.timestamp_ns()),
                "requested_monotonic": time.monotonic(),
                "events": [],
                "quantity": str(order.quantity.as_decimal()),
                "price": str(order.price.as_decimal()),
                "post_only": post_only,
                "reduce_only": reduce_only,
                "time_in_force": str(time_in_force),
            }
        )
        self.submit_order(order)
        return name

    def _submit_startup(self, role, side, quantity, price, tif, **kwargs) -> None:
        self.phase = f"{role}_pending"
        try:
            name = self.submit_limit(role, side, quantity, price, tif, **kwargs)
        except (ValueError, TypeError):
            self._fail("startup order construction was refused locally; no replay")
            return
        self._startup_ids[role] = name
        self._active = name

    def _event(self, event, kind: str) -> str | None:
        name = str(event.client_order_id)
        role = self._orders.get(name)
        if role is None:
            return None
        for operation in reversed(self.operations):
            if operation["client_order_id"] == name:
                operation["events"].append(
                    {
                        "kind": kind,
                        "ts_event": int(event.ts_event),
                        "ts_init": int(event.ts_init),
                        "observed_monotonic": time.monotonic(),
                        "elapsed_secs": time.monotonic()
                        - operation["requested_monotonic"],
                    }
                )
                break
        return role

    def on_order_submitted(self, event) -> None:
        self._event(event, "submitted")

    @_guarded_callback
    def on_order_accepted(self, event) -> None:
        role = self._event(event, "accepted")
        if role in {"maker", "replacement"} and self.phase == f"{role}_pending":
            order = self.cache.order(event.client_order_id)
            if order is None or order.filled_qty.as_decimal() != 0:
                self._fail("post-only acceptance has unexpected fills")
                return
            self.phase = f"{role}_cancel_pending"
            self.operations.append(
                {
                    "operation": "cancel",
                    "role": role,
                    "client_order_id": str(event.client_order_id),
                    "requested_unix_nanos": int(self.clock.timestamp_ns()),
                    "requested_monotonic": time.monotonic(),
                    "events": [],
                }
            )
            self.cancel_order(event.client_order_id)

    @_guarded_callback
    def on_order_canceled(self, event) -> None:
        role = self._event(event, "canceled")
        if (
            role not in {"maker", "replacement"}
            or self.phase != f"{role}_cancel_pending"
        ):
            return
        order = self.cache.order(event.client_order_id)
        if order is None or not order.is_closed or order.filled_qty.as_decimal() != 0:
            self._fail("cancel confirmation is not a zero-fill terminal cached order")
            return
        self.phase = (
            "awaiting_replacement_quote" if role == "maker" else "awaiting_entry_quote"
        )
        self._advance()

    @_guarded_callback
    def on_order_filled(self, event: OrderFilled) -> None:
        role = self._event(event, "filled")
        if role is None:
            return
        key = (str(event.client_order_id), str(event.trade_id))
        if key in self.fills:
            return
        commission = event.commission
        self.fills.setdefault(
            key,
            {
                "client_order_id": key[0],
                "trade_id": key[1],
                "role": role,
                "instrument_id": str(event.instrument_id),
                "quantity": str(event.last_qty.as_decimal()),
                "price": str(event.last_px.as_decimal()),
                "commission": str(commission.as_decimal())
                if isinstance(commission, Money)
                else None,
                "currency": str(commission.currency)
                if isinstance(commission, Money)
                else None,
                "ts_event": int(event.ts_event),
                "ts_init": int(event.ts_init),
            },
        )
        if role in {"maker", "replacement"}:
            self._fail(
                "post-only cancel raced a fill; exposure must be reconciled by native cleanup"
            )
            return
        order = self.cache.order(event.client_order_id)
        if order is None or not order.is_closed:
            return
        if role == "entry":
            self.phase = "awaiting_close_quote"
        elif role == "close":
            entry = self.cache.order(ClientOrderId.from_str(self._startup_ids["entry"]))
            if (
                entry is None
                or order.filled_qty.as_decimal() != entry.filled_qty.as_decimal()
            ):
                self._fail("reduce-only close did not cover the confirmed entry fill")
                return
            self.phase = "awaiting_flat"
        self._advance()

    def on_order_rejected(self, event) -> None:
        if self._event(event, "rejected"):
            self._fail(f"owned order was rejected; no automatic replay: {event.reason}")

    def on_order_denied(self, event) -> None:
        if self._event(event, "denied"):
            self._fail(f"owned order was denied; no automatic replay: {event.reason}")

    def on_order_expired(self, event) -> None:
        role = self._event(event, "expired")
        if role == "entry":
            order = self.cache.order(event.client_order_id)
            if (
                order is not None
                and order.is_closed
                and order.filled_qty.as_decimal() > 0
            ):
                self.phase = "awaiting_close_quote"
                self._advance()
                return
        if role:
            self._fail(
                "startup IOC expired without the required fill; no automatic replay"
            )

    def report(self) -> dict:
        account = self.cache.account(self.config.account_id)
        fees: dict[str, Decimal] = {}
        for fill in self.fills.values():
            if fill["commission"] is not None and fill["currency"] is not None:
                currency = fill["currency"]
                fees[currency] = fees.get(currency, Decimal(0)) + exact(
                    fill["commission"]
                )
        return {
            "source": "installed-native-LiveNode-Strategy",
            "phase": self.phase,
            "startup_verified": self.startup_verified,
            "failures": list(self.failures),
            "operations": self.operations,
            "fills": list(self.fills.values()),
            "fees_by_currency": {key: str(value) for key, value in fees.items()},
            "native_snapshot": self._snapshot(),
            "net_position": str(
                self.portfolio.net_position(
                    self.config.instrument_id, self.config.account_id
                )
            ),
            "net_positions_by_instrument": {
                str(instrument_id): str(
                    self.portfolio.net_position(instrument_id, self.config.account_id)
                )
                for instrument_id in self._configured_ids
            },
            "open_positions": len(
                self.cache.positions_open(
                    instrument_id=self.config.instrument_id,
                    account_id=self.config.account_id,
                )
            ),
            "open_positions_by_instrument": {
                str(instrument_id): len(
                    self.cache.positions_open(
                        instrument_id=instrument_id, account_id=self.config.account_id
                    )
                )
                for instrument_id in self._configured_ids
            },
            "account_balances": None
            if account is None
            else {
                str(currency): {
                    "total": str(balance.total.as_decimal()),
                    "free": str(balance.free.as_decimal()),
                    "locked": str(balance.locked.as_decimal()),
                }
                for currency, balance in account.balances().items()
            },
            "strategy_pnl_claimed": False,
        }


def build_session(
    session: ContinuousSession,
    *,
    startup_instrument: str | None = None,
    startup_timeout_secs: int = 120,
    on_continuous_quote: Callable | None = None,
):
    """Register real native data/execution factories and the reusable Strategy, without I/O."""
    session.validate()
    from nautilus_trader.adapters import ondo

    factory = ondo.OndoExecutionClientFactory()
    if (
        not getattr(factory, "supports_continuous_execution", False)
        or getattr(factory, "supports_native_amend", True)
        or not hasattr(ondo, "OndoContinuousExecutionConfig")
    ):
        raise ValueError(
            "installed Ondo wheel lacks the reviewed continuous cancel/replace contract"
        )
    instruments = [InstrumentId.from_str(name) for name in session.instrument_ids]
    limits = session.limits
    continuous = None
    if session.execution_enabled:
        continuous = ondo.OndoContinuousExecutionConfig(
            instrument_ids=instruments,
            max_notional_per_order_usd=limits.max_notional_per_order_usd,
            max_gross_exposure_usd=limits.max_gross_exposure_usd,
            min_available_margin_usdc=limits.min_available_margin_usdc,
            max_live_orders=limits.max_live_orders,
            max_requests_per_second=limits.max_requests_per_second,
            max_unresolved_orders=limits.max_unresolved_orders,
        )
    environment = (
        ondo.OndoEnvironment.PRODUCTION
        if session.environment == "production"
        else ondo.OndoEnvironment.SANDBOX
    )
    config = ondo.OndoExecutionClientConfig(
        environment=environment,
        account_id=AccountId.from_str(session.account_id),
        api_key=session.api_key,
        api_secret=session.api_secret,
        base_url_http=session.base_url_http,
        base_url_ws=session.base_url_ws,
        account_read_only=not session.execution_enabled,
        expected_venue_account_id=session.expected_venue_account_id,
        diagnostics_run_id=session.diagnostics_run_id,
        journal_path=str(session.journal_path) if session.journal_path else None,
        allow_production_orders=session.execution_enabled
        and session.environment == "production",
        continuous_execution=continuous,
        dms_timeout_secs=session.dms_timeout_secs,
        reconcile_interval_secs=session.reconcile_interval_secs,
        http_timeout_secs=15,
    )
    instrument = InstrumentId.from_str(startup_instrument or session.instrument_ids[0])
    strategy = OndoContinuousStrategy(
        ContinuousStrategyConfig(
            instrument_id=instrument,
            account_id=AccountId.from_str(session.account_id),
            startup_timeout_secs=startup_timeout_secs,
        ),
        session=session,
        execution_factory=factory,
        on_continuous_quote=on_continuous_quote,
    )
    node = (
        LiveNode.builder(
            "ONDO-CONTINUOUS",
            TraderId.from_str("ONDO-CONTINUOUS-001"),
            Environment.LIVE,
        )
        .with_logging(
            LoggerConfig(
                stdout_level=LogLevel.OFF,
                fileout_level=LogLevel.OFF,
                bypass_logging=True,
                print_config=False,
            )
        )
        .with_timeout_connection(60)
        .with_timeout_disconnection_secs(30)
        .with_risk_engine_config(
            LiveRiskEngineConfig(
                bypass=False,
                max_notional_per_order={
                    name: limits.max_notional_per_order_usd
                    for name in session.instrument_ids
                },
            )
        )
        .with_exec_engine_config(
            LiveExecutionEngineConfig(filter_unclaimed_external_orders=False)
        )
        .add_data_client(
            None,
            ondo.OndoDataClientFactory(),
            ondo.OndoDataClientConfig(
                environment=environment,
                load_ids=instruments,
                base_url_http=session.base_url_http,
                base_url_ws=session.base_url_ws,
            ),
        )
        .add_exec_client(None, factory, config)
        .build()
    )
    node.add_strategy(strategy)
    return node, factory, strategy

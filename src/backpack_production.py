"""Finite Backpack operator commands through the native LiveNode and Strategy stack."""

from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, localcontext
import json
import os
from pathlib import Path
import re
import time
import tomllib

from backpack_roundtrip import ClosePosition, RoundTrip

LEG_LIMIT_USD = Decimal("100")
TOTAL_LIMIT_USD = Decimal("500")
SECRET_ENV = "BACKPACK_MAINNET_API_SECRET"
KEY_ENV = "BACKPACK_MAINNET_API_KEY"


class OperatorError(ValueError):
    """A fixed safe operator failure, without credential or request payloads."""


def exact_positive(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise OperatorError(
            "positive quantities and prices require exact decimal strings"
        )
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise OperatorError("invalid exact decimal") from None
    if not number.is_finite() or number <= 0:
        raise OperatorError("decimal must be finite and positive")
    return number


@dataclass(frozen=True)
class OperatorPlan:
    symbols: tuple[str, ...]
    state_directory: Path
    max_leg_usd: Decimal = LEG_LIMIT_USD
    max_total_usd: Decimal = TOTAL_LIMIT_USD
    timeout_secs: int = 60

    def __post_init__(self):
        if (
            not isinstance(self.state_directory, Path)
            or not self.state_directory.is_absolute()
            or not self.state_directory.name
        ):
            raise OperatorError(
                "native state directory must be an explicit absolute directory path"
            )
        if (
            not self.symbols
            or len(self.symbols) > 100
            or len(set(self.symbols)) != len(self.symbols)
            or any(
                not re.fullmatch(r"[A-Z0-9]+_USDC_PERP", symbol)
                for symbol in self.symbols
            )
        ):
            raise OperatorError(
                "an explicit unique USDC perpetual symbol allowlist is required"
            )
        if not (
            0 < self.max_leg_usd <= LEG_LIMIT_USD
            and self.max_leg_usd <= self.max_total_usd <= TOTAL_LIMIT_USD
        ):
            raise OperatorError(
                "limits may only shrink the USD 100 leg and USD 500 total caps"
            )
        if not 1 <= self.timeout_secs <= 600:
            raise OperatorError("operator timeout must be between 1 and 600 seconds")


def load_limits(path):
    """Read the explicit operator envelope with exact TOML decimals and immutable hard caps."""
    try:
        with Path(path).open("rb") as file:
            configured = tomllib.load(file, parse_float=Decimal)["backpack_trade"]
        leg = exact_positive(str(configured["order_notional_usd"]))
        total = exact_positive(str(configured["max_gross_notional_usd"]))
    except Exception:
        raise OperatorError("Backpack limits file is missing or invalid") from None
    if not 0 < leg <= LEG_LIMIT_USD or not leg <= total <= TOTAL_LIMIT_USD:
        raise OperatorError(
            "Backpack limits file exceeds the USD 100 leg or USD 500 total caps"
        )
    return leg, total


def check_maker_reprice(command):
    interval = command.get("maker_reprice_seconds", 0)
    maximum = command.get("maker_max_reprices", 0)
    if type(interval) is not int or type(maximum) is not int:
        raise OperatorError("maker repricing settings must be integers")
    if interval == 0 and maximum == 0:
        return
    if (command["style"] != "maker" or not 5 <= interval <= command["wait_seconds"]
            or not 1 <= maximum <= 23):
        raise OperatorError("maker repricing requires a 5-second minimum interval within the leg deadline and 1 to 23 replacements")


def checked_command(document, plan):
    if not isinstance(document, dict):
        raise OperatorError("operator command must be a JSON object")
    command = dict(document)
    action = command.get("action")
    if action not in {"submit", "query", "cancel", "cancel-replace", "roundtrip", "close-only"}:
        raise OperatorError(
            "command action must be submit, query, cancel, cancel-replace, roundtrip or close-only"
        )
    if action == "close-only":
        required = {"action", "symbol", "style", "max_quantity", "entry_client_order_id", "wait_seconds"}
        if not required <= set(command) or set(command) - required - {"maker_reprice_seconds", "maker_max_reprices"}:
            raise OperatorError("close-only has missing or unsupported fields")
        if command["symbol"] not in plan.symbols or command["style"] not in {"maker", "market"}:
            raise OperatorError("close-only symbol or style is unsupported")
        exact_positive(command["max_quantity"])
        if (not isinstance(command["entry_client_order_id"], str)
                or not command["entry_client_order_id"] or command["entry_client_order_id"] == "last"):
            raise OperatorError("close-only requires an explicit original entry client order ID")
        if type(command["wait_seconds"]) is not int or not 5 <= command["wait_seconds"] <= 120:
            raise OperatorError("close-only leg wait must be between 5 and 120 seconds")
        check_maker_reprice(command)
        return command
    if action == "roundtrip":
        required = {"action", "symbol", "style", "quantity", "reference_price", "wait_seconds"}
        if not required <= set(command) or set(command) - required - {"maker_reprice_seconds", "maker_max_reprices"}:
            raise OperatorError("roundtrip has missing or unsupported fields")
        if command["symbol"] not in plan.symbols or command["style"] not in {"maker", "market"}:
            raise OperatorError("roundtrip symbol or style is unsupported")
        quantity = exact_positive(command["quantity"])
        reference = exact_positive(command["reference_price"])
        if quantity * reference > min(plan.max_leg_usd, Decimal("5")):
            raise OperatorError("roundtrip exceeds its USD 5 submission valuation limit")
        if type(command["wait_seconds"]) is not int or not 5 <= command["wait_seconds"] <= 120:
            raise OperatorError("roundtrip leg wait must be between 5 and 120 seconds")
        check_maker_reprice(command)
        return command
    if action == "submit":
        allowed = {
            "action",
            "symbol",
            "side",
            "quantity",
            "price",
            "time_in_force",
            "post_only",
            "reduce_only",
            "cancel_after_accept",
            "order_type",
            "reference_price",
        }
        if set(command) - allowed or not {"symbol", "side", "quantity"} <= set(
            command
        ):
            raise OperatorError("submit command has missing or unsupported fields")
        if command["symbol"] not in plan.symbols or command["side"] not in {
            "buy",
            "sell",
        }:
            raise OperatorError("submit side or symbol is outside the configured scope")
        command.setdefault("order_type", "limit")
        if command["order_type"] not in {"limit", "market"}:
            raise OperatorError("order type must be limit or market")
        market = command["order_type"] == "market"
        if (market and ("price" in command or "reference_price" not in command)) or (
                not market and ("price" not in command or "reference_price" in command)):
            raise OperatorError("limit requires price; market requires reference_price and no limit price")
        price_field = "reference_price" if market else "price"
        price = exact_positive(command[price_field])
        quantity = exact_positive(command["quantity"])
        with localcontext() as context:
            context.prec = max(28, len(command[price_field]) + len(command["quantity"]))
            notional = price * quantity
        if notional > plan.max_leg_usd:
            raise OperatorError("order exceeds the configured leg notional limit")
        command.setdefault("time_in_force", "IOC" if market else "GTC")
        command.setdefault("post_only", False)
        command.setdefault("reduce_only", False)
        command.setdefault("cancel_after_accept", False)
        if command["time_in_force"] not in {"GTC", "IOC"}:
            raise OperatorError("only GTC and IOC are supported by this operator")
        if any(
            type(command[name]) is not bool
            for name in ("post_only", "reduce_only", "cancel_after_accept")
        ):
            raise OperatorError("execution flags must be JSON booleans")
        if command["post_only"] and command["time_in_force"] != "GTC":
            raise OperatorError("post-only requires GTC")
        if command["cancel_after_accept"] and command["time_in_force"] != "GTC":
            raise OperatorError("cancel-after-accept requires GTC")
        if market and (command["time_in_force"] != "IOC" or command["post_only"] or command["cancel_after_accept"]):
            raise OperatorError("market requires IOC without post-only or cancel-after-accept")
    else:
        allowed = {"action", "client_order_id"} | (
            {"replacement"} if action == "cancel-replace" else set()
        )
        if (
            set(command) != allowed
            or not isinstance(command.get("client_order_id"), str)
            or not command["client_order_id"]
        ):
            raise OperatorError(
                "query and cancellation require an original client order ID or last"
            )
        if action == "cancel-replace":
            command["replacement"] = checked_command(command["replacement"], plan)
            if command["replacement"]["action"] != "submit":
                raise OperatorError("replacement must be a submit command")
    return command


def resolve_credentials(env_file=None):
    """Read only explicitly selected environment values and verify the Ed25519 public key."""
    values = {SECRET_ENV: os.environ.get(SECRET_ENV), KEY_ENV: os.environ.get(KEY_ENV)}
    if env_file is not None:
        from dotenv import dotenv_values

        try:
            selected = dotenv_values(Path(env_file), interpolate=False)
        except Exception:
            raise OperatorError(
                "explicit credential environment file could not be read"
            ) from None
        values.update({name: selected.get(name) for name in values})
    if not all(isinstance(value, str) and value for value in values.values()):
        raise OperatorError(
            "explicit Backpack mainnet secret and public key are required"
        )
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

        seed = base64.b64decode(values[SECRET_ENV], validate=True)
        expected = base64.b64decode(values[KEY_ENV], validate=True)
        if len(seed) != 32 or len(expected) != 32:
            raise ValueError
        actual = (
            Ed25519PrivateKey.from_private_bytes(seed)
            .public_key()
            .public_bytes(Encoding.Raw, PublicFormat.Raw)
        )
        if actual != expected:
            raise ValueError
    except Exception:
        raise OperatorError(
            "Backpack secret and public key did not pass the credential check"
        ) from None
    return values[SECRET_ENV]


def runtime_error_category(error):
    """Classify fixed native diagnostics without exposing exception payloads."""
    message = str(error)
    for marker, category in (
        ("data-connect timeout", "data_connection_timeout"),
        ("exec-connect timeout", "execution_connection_timeout"),
        ("readiness timeout while waiting for engine connections", "engine_readiness_timeout"),
        ("Backpack complete metadata refresh failed", "public_metadata_failure"),
        ("Backpack public transport failed", "public_transport_failure"),
        ("Startup reconciliation timeout reached", "startup_reconciliation_timeout"),
    ):
        if marker in message:
            return category
    return "other_native_runtime_failure"


def build_node(plan, commands, seed, *, read_only=False, endpoint_overrides=None):
    """Build real native owners and one registered Strategy; transport creation remains native."""
    if read_only and commands:
        raise OperatorError("read-only inspection cannot contain trading commands")
    if not read_only and not 1 <= len(commands) <= 32:
        raise OperatorError("provide between 1 and 32 finite commands")
    commands = tuple(checked_command(command, plan) for command in commands)
    from backpack_session import load_recovery_start

    try:
        recovery_start_ms = load_recovery_start(plan.state_directory, seed)
    except ValueError as error:
        raise OperatorError(str(error)) from None
    from nautilus_trader.adapters.backpack import (
        BackpackCredential,
        BackpackDataClientConfig,
        BackpackDataClientFactory,
        BackpackExecutionClientConfig,
        BackpackInstrumentEconomics,
        BackpackProductionExecutionAuthority,
        BackpackProductionExecutionClientConfig,
        BackpackProductionExecutionClientFactory,
        BackpackQuota,
    )
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import (
        ClientId,
        ClientOrderId,
        InstrumentId,
        OmsType,
        OrderSide,
        Price,
        Quantity,
        StrategyId,
        TimeInForce,
        TraderId,
    )
    from nautilus_trader.trading import Strategy, StrategyConfig

    # Native endpoint constructors restrict overrides to the numeric loopback peer.
    # The operator CLI never accepts endpoint overrides or forwards credentials to one.
    endpoints = {} if endpoint_overrides is None else dict(endpoint_overrides)
    quota = BackpackQuota()
    credential = BackpackCredential(seed, **endpoints)
    # Conservative configured instrument bounds. Native admission uses authenticated account
    # collateral and fee observations; true fills carry their own exact commissions.
    economics = BackpackInstrumentEconomics(
        "1",
        "1",
        "0.01",
        "0.01",
        "Configured",
        "operator full-notional margin and configured one-percent fee estimate; native admission uses observed fees",
    )
    public = BackpackDataClientConfig(
        list(plan.symbols),
        {symbol: economics for symbol in plan.symbols},
        quote_stale_after_ms=3000,
        **endpoints,
    )
    account = BackpackExecutionClientConfig(
        list(plan.symbols),
        credential,
        "BACKPACK-OPERATOR",
        "operator-label",
        str(plan.state_directory / "identity"),
        quota=quota,
        connect_timeout_ms=min(60, plan.timeout_secs) * 1000,
        recovery_interval_ms=1000,
        recovery_lookback_ms=86400000,
        recovery_start_ms=recovery_start_ms,
        **endpoints,
    )
    authority = BackpackProductionExecutionAuthority(
        expires_at_ms=time.time_ns() // 1000000 + (plan.timeout_secs + 10) * 1000,
        max_account_age_ms=15000,
        max_market_age_ms=15000,
        max_order_notional=str(plan.max_leg_usd),
        max_reserved_notional=str(plan.max_total_usd),
        max_reserved_margin=str(plan.max_total_usd),
        max_unsettled_orders=32,
        allow_new_risk=not read_only and any(command["action"] != "close-only" for command in commands),
        allow_reduction=not read_only,
        allow_owned_cancel=not read_only,
    )
    configuration = BackpackProductionExecutionClientConfig(
        account,
        public,
        authority=authority,
        mutation_budget_ms=10000,
        receive_window_ms=5000,
        economic_state_directory=str(plan.state_directory / "economics"),
    )
    control = configuration.control

    class OperatorStrategy(Strategy):
        def __init__(self):
            super().__init__(
                StrategyConfig(
                    strategy_id=StrategyId.from_str("BACKPACK-OPERATOR-001"),
                    oms_type=OmsType.NETTING,
                    use_uuid_client_order_ids=True,
                    log_events=False,
                    log_commands=False,
                )
            )
            self.started = False
            self.public_config = public
            self.index = 0
            self.phase = None
            self.order_id = None
            self.last_id = None
            self.replacement = None
            self.results = []
            self.events = []
            self.failure = None
            self.admission = {"ready": False, "reason": "native startup pending"}
            self.economics = None
            self.settlement_diagnostic = None
            self.command_started_ns = None
            self.round_trip = None
            self.roundtrip_results = []
            self.stop_roundtrips = False

        def ready(self):
            self.admission = json.loads(control.admission_status_json())
            health = json.loads(control.telemetry_snapshot_json())
            return (self.admission["ready"] and health.get("rest_snapshot_observed")
                    and health.get("completed_recovery_count", 0) > 0)

        def quote(self, symbol):
            if not json.loads(public.telemetry_snapshot_json())["quotes_fresh"].get(symbol, False):
                return None
            quote = self.cache.quote(InstrumentId.from_str(symbol + ".BACKPACK"))
            if quote is None:
                return None
            return {"bid": Decimal(str(quote.bid_price)), "ask": Decimal(str(quote.ask_price))}

        def position_quantity(self, symbol):
            from nautilus_trader.model import Venue

            total = Decimal("0")
            for position in self.cache.positions(venue=Venue.from_str("BACKPACK")):
                if not position.is_open or str(position.instrument_id) != symbol + ".BACKPACK":
                    continue
                if str(position.strategy_id) != "BACKPACK-OPERATOR-001" or str(position.side) != "LONG":
                    raise OperatorError("roundtrip encountered a non-owned or non-long position")
                total += Decimal(str(position.quantity))
            return total

        def check_close_origin(self, symbol, entry_id):
            from nautilus_trader.model import Venue

            for order in self.cache.orders(venue=Venue.from_str("BACKPACK")):
                if str(order.instrument_id) == symbol + ".BACKPACK" and not order.is_closed:
                    raise OperatorError("close-only requires no outstanding order for its symbol")
            for position in self.cache.positions(venue=Venue.from_str("BACKPACK")):
                if (position.is_open and str(position.instrument_id) == symbol + ".BACKPACK"
                        and str(position.opening_order_id) != entry_id):
                    raise OperatorError("close-only position does not originate from the selected entry")

        def on_start(self):
            self.started = True
            for symbol in plan.symbols:
                self.subscribe_quotes(
                    InstrumentId.from_str(symbol + ".BACKPACK"),
                    client_id=ClientId.from_str("BACKPACK"),
                )

        def on_stop(self):
            self.started = False

        def owned_order(self, reference):
            reference = self.last_id if reference == "last" else reference
            if reference is None:
                raise OperatorError(
                    "last does not identify an order in this command run"
                )
            order = self.cache.order(ClientOrderId.from_str(reference))
            if order is None or str(order.strategy_id) != "BACKPACK-OPERATOR-001":
                raise OperatorError(
                    "original owned order is absent from native recovery cache"
                )
            if str(order.instrument_id).removesuffix(".BACKPACK") not in plan.symbols:
                raise OperatorError(
                    "original order is outside the configured symbol allowlist"
                )
            return order

        def submit(self, command):
            command = checked_command(command, plan)
            arguments = (
                InstrumentId.from_str(command["symbol"] + ".BACKPACK"),
                OrderSide.BUY if command["side"] == "buy" else OrderSide.SELL,
                Quantity.from_str(command["quantity"]),
            )
            if command["order_type"] == "market":
                order = self.order_factory.market(*arguments, time_in_force=TimeInForce.IOC,
                                                  reduce_only=command["reduce_only"])
            else:
                order = self.order_factory.limit(
                    *arguments, Price.from_str(command["price"]),
                    time_in_force=TimeInForce.GTC if command["time_in_force"] == "GTC" else TimeInForce.IOC,
                    post_only=command["post_only"], reduce_only=command["reduce_only"],
                )
            self.order_id = str(order.client_order_id)
            self.last_id = self.order_id
            self.phase = "submit"
            self.submit_order(order)

        def finish(self, action, order, *, terminal=False):
            self.results.append(
                {
                    "action": action,
                    "client_order_id": str(order.client_order_id),
                    "venue_order_id": None
                    if order.venue_order_id is None
                    else str(order.venue_order_id),
                    "status": str(order.status),
                    "filled_quantity": str(order.filled_qty),
                    "terminal_reconciled": terminal,
                    "elapsed_ms": (time.monotonic_ns() - self.command_started_ns)
                    // 1_000_000,
                }
            )
            self.index += 1
            self.phase = self.order_id = self.replacement = None
            self.command_started_ns = None

        def settled(self, order):
            if not order.is_closed:
                return False
            try:
                self.economics = json.loads(control.persist_economics())
                terminal = json.loads(control.reconcile_terminal_evidence())
            except RuntimeError as error:
                self.admission = json.loads(control.admission_status_json())
                self.settlement_diagnostic = {
                    "client_order_id": str(order.client_order_id),
                    "reason": ("economic_checkpoint_refused" if "economic checkpoint" in str(error)
                               else "terminal_reconciliation_refused"),
                    "admission": self.admission,
                }
                return False
            row = next((row for row in terminal.get("orders", [])
                        if row["client_order_id"] == str(order.client_order_id)), None)
            reconciled = row is not None and row["reconciled"]
            self.settlement_diagnostic = {
                "client_order_id": str(order.client_order_id),
                "reason": None if reconciled else ("terminal_order_absent" if row is None
                                                   else "terminal_evidence_incomplete"),
                "evidence": row,
            }
            return reconciled

        def advance(self):
            if not self.started or self.failure or self.index == len(commands):
                return
            try:
                command = commands[self.index]
                if self.command_started_ns is None:
                    self.command_started_ns = time.monotonic_ns()
                if command["action"] in {"roundtrip", "close-only"}:
                    if self.round_trip is None:
                        if self.stop_roundtrips:
                            raise OperatorError("roundtrip cleanup deadline reached before the next entry")
                        controller = ClosePosition if command["action"] == "close-only" else RoundTrip
                        self.round_trip = controller(command, min(plan.max_leg_usd, Decimal("5")))
                    self.round_trip.advance(self)
                    if self.round_trip.failure:
                        self.failure = self.round_trip.failure
                    elif self.round_trip.done:
                        result = self.round_trip.snapshot()
                        self.roundtrip_results.append(result)
                        self.results.append({"action": command["action"], **result})
                        self.index += 1
                        self.round_trip = None
                        self.phase = self.order_id = self.command_started_ns = None
                    return
                if self.phase is None:
                    if command["action"] == "submit":
                        self.admission = json.loads(control.admission_status_json())
                        if self.admission["ready"]:
                            self.submit(command)
                        return
                    order = self.owned_order(command["client_order_id"])
                    if command["action"] == "query":
                        self.query_order(order)
                        self.finish("query_requested", order)
                        return
                    if order.is_closed:
                        self.order_id = str(order.client_order_id)
                        self.phase = "cancel"
                    else:
                        self.order_id = str(order.client_order_id)
                        self.phase = "cancel"
                        self.cancel_order(order.client_order_id)
                order = self.owned_order(self.order_id)
                if self.phase == "submit":
                    active_command = self.replacement or command
                    if order.is_closed:
                        if self.settled(order):
                            self.finish(command["action"], order, terminal=True)
                    elif str(order.status) in {"ACCEPTED", "PARTIALLY_FILLED"}:
                        if active_command["cancel_after_accept"]:
                            self.phase = "cancel_after_accept"
                            self.cancel_order(order.client_order_id)
                        elif active_command["time_in_force"] == "IOC":
                            return
                        else:
                            self.finish(command["action"], order)
                elif self.settled(order):
                    if command["action"] == "cancel-replace" and self.phase == "cancel":
                        self.admission = json.loads(control.admission_status_json())
                        if self.admission["ready"]:
                            previous = str(order.client_order_id)
                            self.results.append(
                                {
                                    "action": "replacement_prior_terminal_confirmed",
                                    "client_order_id": previous,
                                    "status": str(order.status),
                                    "terminal_reconciled": True,
                                }
                            )
                            self.replacement = command["replacement"]
                            self.submit(self.replacement)
                            if self.order_id == previous:
                                raise OperatorError(
                                    "replacement did not receive a new original client ID"
                                )
                    else:
                        self.finish(command["action"], order, terminal=True)
            except OperatorError as e:
                self.failure = str(e)
            except Exception:
                self.failure = "native operator command failed"

        def record_event(self, kind, event):
            row = {
                "kind": kind,
                "client_order_id": str(event.client_order_id),
                "instrument_id": str(event.instrument_id),
                "ts_event": event.ts_event,
            }
            venue_id = getattr(event, "venue_order_id", None)
            if venue_id is not None:
                row["venue_order_id"] = str(venue_id)
            if kind == "filled":
                row.update(
                    trade_id=str(event.trade_id),
                    quantity=str(event.last_qty),
                    price=str(event.last_px),
                    commission=None if event.commission is None else str(event.commission),
                    liquidity_side=str(event.liquidity_side),
                )
            self.events.append(row)

        def on_order_accepted(self, event):
            self.record_event("accepted", event)

        def on_order_filled(self, event):
            self.record_event("filled", event)

        def on_order_canceled(self, event):
            self.record_event("canceled", event)

        def on_order_expired(self, event):
            self.record_event("expired", event)

        def on_order_denied(self, event):
            self.record_event("denied", event)
            self.failure = "native order admission refused"

        def on_order_rejected(self, event):
            self.record_event("rejected", event)
            self.failure = "native order rejected"

        def on_order_cancel_rejected(self, event):
            self.record_event("cancel_rejected", event)
            self.failure = "native owned cancellation refused"

    node = (
        LiveNode.builder(
            "BACKPACK-OPERATOR",
            TraderId.from_str("BACKPACK-OPERATOR-001"),
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
        # Data and execution clients share this deadline, including private recovery.
        # The operator's overall deadline still bounds all startup and command work.
        .with_timeout_connection(min(60, plan.timeout_secs))
        .with_reconciliation(False)
        .with_timeout_reconciliation(0)
        .with_timeout_portfolio(0)
        .with_timeout_disconnection_secs(5)
        .with_delay_post_stop_secs(0)
        .with_delay_shutdown_secs(0)
        .with_load_state(False)
        .with_save_state(False)
        .with_exec_engine_config(
            LiveExecutionEngineConfig(
                reconciliation=False,
                inflight_check_interval_ms=0,
                open_check_interval_secs=None,
                position_check_interval_secs=None,
            )
        )
        .add_data_client(None, BackpackDataClientFactory(quota=quota), public)
        .add_exec_client(
            None, BackpackProductionExecutionClientFactory(), configuration
        )
        .build()
    )
    strategy = OperatorStrategy()
    node.add_strategy(strategy)
    return node, strategy, control


async def inspect_native(plan, seed):
    """Observe the real native transports with no commands and no mutation authority."""
    node, strategy, control = build_node(plan, (), seed, read_only=True)
    handle = node.handle()
    task = asyncio.ensure_future(node.run_async())
    deadline = time.monotonic() + plan.timeout_secs
    observations = []
    last_state = None
    failure = None
    admission = {"ready": False, "reason": "native startup pending"}
    health = None
    public_health = None
    try:
        while not task.done() and time.monotonic() < deadline:
            admission = json.loads(control.admission_status_json())
            health = json.loads(control.telemetry_snapshot_json())
            public_health = json.loads(strategy.public_config.telemetry_snapshot_json())
            state = (strategy.started, admission.get("ready"), admission.get("reason"))
            if state != last_state:
                observations.append({"strategy_started": strategy.started, "admission": admission})
                last_state = state
            if (strategy.started and admission.get("ready")
                    and health.get("completed_recovery_count", 0) > 0):
                break
            await asyncio.sleep(0.2)
        if task.done():
            failure = "native runtime returned before inspection completed"
        elif not admission.get("ready") or not health.get("completed_recovery_count", 0):
            failure = "native readiness not reached before inspection deadline"
    finally:
        handle.stop()
        try:
            await asyncio.wait_for(asyncio.shield(task), 12)
        except Exception as error:
            failure = failure or "native runtime or shutdown failed"
            observations.append({
                "runtime_error_type": type(error).__name__,
                "runtime_error_category": runtime_error_category(error),
            })
        finally:
            node.dispose()
    shutdown = control.shutdown_report_json()
    return {
        "schema_version": 1,
        "mode": "native-read-only",
        "mutations": 0,
        "mutation_authority": False,
        "ready": failure is None and bool(admission.get("ready")),
        "failure": failure,
        "observations": observations,
        "health": health,
        "public_health": public_health,
        "shutdown": None if shutdown is None else json.loads(shutdown),
    }


async def execute(plan, commands, seed, *, endpoint_overrides=None, progress=None):
    """Run one finite native command sequence and retain honest sticky shutdown evidence."""
    node, strategy, control = build_node(plan, commands, seed, endpoint_overrides=endpoint_overrides)
    cache = node.cache
    handle = node.handle()
    task = asyncio.ensure_future(node.run_async())
    deadline = time.monotonic() + plan.timeout_secs
    failure = None
    health = None
    positions = None
    runtime_error_type = None
    runtime_failure_category = None
    account_reconciled = False
    last_progress = None
    progress_at = 0
    try:
        while not task.done() and time.monotonic() < deadline:
            if time.monotonic() >= deadline - 15:
                strategy.stop_roundtrips = True
                if strategy.round_trip is not None:
                    strategy.round_trip.stop_requested = True
            strategy.advance()
            current_health = json.loads(control.telemetry_snapshot_json())
            if not strategy.started:
                strategy.admission = json.loads(control.admission_status_json())
                if current_health.get("last_recovery_error_category") == "AccountEconomicConflict":
                    strategy.failure = "restored fill conflicts with exchange history; inspect the read-only fill comparison"
            current = {
                "kind": "progress", "commands_completed": strategy.index,
                "events_observed": len(strategy.events),
                "last_event": strategy.events[-1] if strategy.events else None,
                "roundtrip": None if strategy.round_trip is None else strategy.round_trip.snapshot(),
                "admission": strategy.admission,
                "settlement": strategy.settlement_diagnostic,
                "failure": strategy.failure,
                "recovery_diagnostic": {key: current_health.get(key) for key in (
                    "last_recovery_error_stage", "last_recovery_error_category", "last_private_error_category",
                    "last_fill_conflict_fields")},
            }
            if progress is not None and (current != last_progress or time.monotonic() - progress_at >= 5):
                progress(current)
                last_progress, progress_at = current, time.monotonic()
            if strategy.failure or strategy.index == len(commands):
                break
            await asyncio.sleep(0.1)
        failure = strategy.failure
        if strategy.index != len(commands) and failure is None:
            failure = (
                "native runtime returned before commands completed"
                if task.done()
                else "operator deadline reached"
            )
        # Final private frames may invalidate the account after an order acknowledgement.
        # Require a complete subsequent snapshot/history/fill recovery within the same deadline.
        if failure is None:
            health = json.loads(control.telemetry_snapshot_json())
            recovery_before = health.get("completed_recovery_count", 0)
            while not task.done() and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
                strategy.admission = json.loads(control.admission_status_json())
                health = json.loads(control.telemetry_snapshot_json())
                if (health.get("completed_recovery_count", 0) > recovery_before
                        and health.get("rest_snapshot_observed")
                        and health.get("pending_fills") == 0
                        and strategy.admission.get("ready")):
                    account_reconciled = True
                    break
            if not account_reconciled:
                failure = "post-command account reconciliation did not complete"
        try:
            strategy.economics = json.loads(control.persist_economics())
        except RuntimeError:
            failure = failure or "native economic checkpoint unavailable"
        try:
            strategy.admission = json.loads(control.admission_status_json())
        except RuntimeError:
            strategy.admission = {"ready": False, "reason": "production_admission_unavailable"}
        from nautilus_trader.model import Venue

        positions = [
            {
                "instrument_id": str(position.instrument_id),
                "strategy_id": str(position.strategy_id),
                "is_open": position.is_open,
                "side": str(position.side),
                "quantity": str(position.quantity),
                "commissions": [str(fee) for fee in position.commissions()],
                "realized_pnl": None if position.realized_pnl is None else str(position.realized_pnl),
            }
            for position in cache.positions(venue=Venue.from_str("BACKPACK"))
        ]
        health = json.loads(control.telemetry_snapshot_json())
    finally:
        handle.stop()
        try:
            await asyncio.wait_for(asyncio.shield(task), 12)
        except Exception as error:
            failure = failure or "native runtime or shutdown failed"
            runtime_error_type = type(error).__name__
            runtime_failure_category = runtime_error_category(error)
        finally:
            node.dispose()
    shutdown = control.shutdown_report_json()
    return {
        "schema_version": 1,
        "mode": "mainnet-execution",
        "completed": failure is None,
        "failure": failure,
        "commands_completed": strategy.index,
        "account_reconciled": account_reconciled,
        "results": strategy.results,
        "events": strategy.events,
        "roundtrips": strategy.roundtrip_results + (
            [] if strategy.round_trip is None else [strategy.round_trip.snapshot()]
        ),
        "native_positions": positions,
        "health": health,
        "runtime_error_type": runtime_error_type,
        "runtime_error_category": runtime_failure_category,
        "admission": strategy.admission,
        "settlement": strategy.settlement_diagnostic,
        "economic_consumer": strategy.economics,
        "shutdown": None if shutdown is None else json.loads(shutdown),
    }

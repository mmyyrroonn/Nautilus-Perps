"""NautilusTrader LiveNode assembly and dynamic Strategy lifecycle for Route A Carry."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
import math
import os
from pathlib import Path
import signal
import threading
import time
from types import MappingProxyType
from typing import Any, Mapping
import uuid

from .account_reader import CarryStartupAccountReader, StartupSnapshotError
from .config import CarryPlan, CarryPolicy, ledger_domain_for_mode
from .executor import CarryExecutionController, OrderAck, PaperVenueGateway
from .funding_feed import LegMarketInfo, PublicCarryFeed
from .ledger import CarryLedger, LedgerOwnershipError
from .recorder import CarryRecorder
from .risk import BasisStopTracker, FastRiskEvaluator
from .signal import (
    InstrumentSpec,
    SubscriptionManager,
    evaluate_entry_candidates,
    evaluate_watch_candidates,
    should_exit_carry_decay,
)


_GRACEFUL_SHUTDOWN_DRAIN_SECS = 30.0


@dataclass(frozen=True)
class _SlowPollResult:
    poll_ts_s: int
    snapshot: Mapping[str, Mapping[str, LegMarketInfo]] | None
    error: str | None = None
    partial_errors: tuple[tuple[str, str], ...] = ()


def _resolve_quote_event_ts(tick: Any, now_s: float) -> float:
    raw_ns = getattr(tick, "ts_event", None)
    if raw_ns is None:
        return now_s
    try:
        ev_s = float(raw_ns) / 1e9
    except (TypeError, ValueError):
        return now_s
    if not math.isfinite(ev_s):
        return 0.0
    return min(now_s, ev_s)


def _extract_money_decimal(raw: Any) -> Decimal:
    """Extract a non-negative Decimal from Nautilus Money or numeric/string test mocks."""
    if raw is None:
        return Decimal("0")
    as_dec = getattr(raw, "as_decimal", None)
    if callable(as_dec):
        val = as_dec()
        return max(Decimal("0"), val if isinstance(val, Decimal) else Decimal(str(val)))
    if isinstance(raw, Decimal):
        return max(Decimal("0"), raw)
    text = str(raw).strip()
    if not text:
        return Decimal("0")
    return max(Decimal("0"), Decimal(text.split()[0]))


class _NautilusStrategyGateway:
    """Thin adapter translating CarryExecutionController calls to Nautilus Strategy order APIs."""

    def __init__(self, strategy: Any) -> None:
        self.strategy = strategy
        self._orders: dict[str, Any] = {}

    def submit_limit(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        price: Decimal,
        post_only: bool,
        ioc: bool,
        reduce_only: bool,
    ) -> OrderAck:
        from nautilus_trader.model import ClientOrderId, InstrumentId, OrderSide, Price, Quantity, TimeInForce

        iid = InstrumentId.from_str(instrument_id)
        inst = self.strategy.cache.instrument(iid)
        if inst is None:
            return OrderAck(client_order_id, "", "REJECTED", reason="instrument_not_in_cache")
        tif = TimeInForce.IOC if ioc else TimeInForce.GTC
        order = self.strategy.order_factory.limit(
            instrument_id=iid,
            order_side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            quantity=Quantity(float(qty_contracts), inst.size_precision),
            price=Price(float(price), inst.price_precision),
            time_in_force=tif,
            post_only=post_only,
            reduce_only=reduce_only,
            client_order_id=ClientOrderId(client_order_id),
        )
        self._orders[client_order_id] = order
        self.strategy.submit_order(order)
        return OrderAck(client_order_id, "", "OPEN", Decimal("0"), None)

    def submit_market(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        reduce_only: bool,
    ) -> OrderAck:
        from nautilus_trader.model import ClientOrderId, InstrumentId, OrderSide, Quantity

        iid = InstrumentId.from_str(instrument_id)
        inst = self.strategy.cache.instrument(iid)
        if inst is None:
            return OrderAck(client_order_id, "", "REJECTED", reason="instrument_not_in_cache")
        order = self.strategy.order_factory.market(
            instrument_id=iid,
            order_side=OrderSide.BUY if side == "BUY" else OrderSide.SELL,
            quantity=Quantity(float(qty_contracts), inst.size_precision),
            reduce_only=reduce_only,
            client_order_id=ClientOrderId(client_order_id),
        )
        self._orders[client_order_id] = order
        self.strategy.submit_order(order)
        return OrderAck(client_order_id, "", "OPEN", Decimal("0"), None)

    def cancel_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        from nautilus_trader.model import ClientOrderId

        coid = ClientOrderId(client_order_id)
        cached = self.strategy.cache.order(coid)
        order = cached if cached is not None else self._orders.get(client_order_id)
        if order is not None and not order.is_closed:
            self.strategy.cancel_order(coid)
        return self.poll_order(client_order_id=client_order_id, venue=venue, instrument_id=instrument_id)

    def poll_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        from nautilus_trader.model import ClientOrderId

        order = self.strategy.cache.order(ClientOrderId(client_order_id)) or self._orders.get(client_order_id)
        if order is None:
            return OrderAck(client_order_id, "", "OPEN", Decimal("0"))
        inst = self.strategy.cache.instrument(order.instrument_id)
        mult = Decimal(str(getattr(inst, "multiplier", 1))) if inst is not None else Decimal("1")
        filled = Decimal(str(order.filled_qty)) * mult
        avg_px_raw = getattr(order, "avg_px", None)
        avg_px = Decimal(str(avg_px_raw)) if avg_px_raw is not None and float(avg_px_raw) > 0 else None
        status_str = str(order.status).upper()
        if "FILLED" in status_str and "PARTIAL" not in status_str:
            st = "FILLED"
        elif "PARTIAL" in status_str:
            st = "PARTIAL"
        elif "CANCELED" in status_str or "CANCELLED" in status_str or "EXPIRED" in status_str:
            st = "CANCELED"
        elif "REJECTED" in status_str or "DENIED" in status_str:
            st = "REJECTED"
        else:
            st = "OPEN"
        return OrderAck(client_order_id, str(order.venue_order_id or ""), st, filled, avg_px)


def build_data_clients(plan: CarryPlan) -> list[tuple[str, Any, Any]]:
    """Build native public DataClient (factory, config) tuples for all configured venues."""
    clients: list[tuple[str, Any, Any]] = []
    iv_mins = plan.runtime.update_instruments_interval_mins
    is_main = plan.environment == "mainnet"

    for venue in plan.venues:
        if venue == "HL":
            from nautilus_trader.adapters.hyperliquid import (
                HyperliquidDataClientConfig, HyperliquidDataClientFactory, HyperliquidEnvironment,
            )
            clients.append((
                "HYPERLIQUID",
                HyperliquidDataClientFactory(),
                HyperliquidDataClientConfig(
                    environment=HyperliquidEnvironment.MAINNET if is_main else HyperliquidEnvironment.TESTNET,
                    bbo_cache_only=False,
                    update_instruments_interval_mins=iv_mins,
                ),
            ))
        elif venue == "LIGHTER":
            from nautilus_trader.adapters.lighter import (
                LighterDataClientConfig, LighterDataClientFactory, LighterEnvironment,
            )
            clients.append((
                "LIGHTER",
                LighterDataClientFactory(),
                LighterDataClientConfig(
                    environment=LighterEnvironment.MAINNET if is_main else LighterEnvironment.TESTNET,
                    update_instruments_interval_mins=iv_mins,
                ),
            ))
        elif venue == "OKX":
            from nautilus_trader.adapters.okx import (
                OKXDataClientConfig, OKXDataClientFactory, OKXEnvironment, OKXInstrumentType,
            )
            clients.append((
                "OKX",
                OKXDataClientFactory(),
                OKXDataClientConfig(
                    instrument_types=[OKXInstrumentType.SWAP],
                    environment=OKXEnvironment.LIVE if is_main else OKXEnvironment.DEMO,
                    update_instruments_interval_mins=iv_mins,
                ),
            ))
        elif venue == "BYBIT":
            from nautilus_trader.adapters.bybit import (
                BybitDataClientConfig, BybitDataClientFactory, BybitEnvironment, BybitProductType,
            )
            clients.append((
                "BYBIT",
                BybitDataClientFactory(),
                BybitDataClientConfig(
                    product_types=[BybitProductType.LINEAR],
                    environment=BybitEnvironment.MAINNET if is_main else BybitEnvironment.TESTNET,
                    update_instruments_interval_mins=iv_mins,
                ),
            ))
        else:
            raise ValueError(f"unsupported venue {venue}")
    return clients


def _require_env(env: Mapping[str, str], keys: tuple[str, ...], venue: str) -> dict[str, str]:
    missing = [k for k in keys if not env.get(k)]
    if missing:
        raise ValueError(f"{venue} credentials missing from environment: {', '.join(missing)}")
    return {k: env[k] for k in keys}


def build_execution_clients(plan: CarryPlan, *, environ: Mapping[str, str] | None = None) -> list[tuple[str, Any, Any]]:
    """Build native ExecutionClient configs for readonly / live modes with strict env resolution."""
    env = os.environ if environ is None else environ
    is_main = plan.environment == "mainnet"
    from nautilus_trader import model as nt_model

    clients: list[tuple[str, Any, Any]] = []
    for venue in plan.venues:
        if venue == "HL":
            from nautilus_trader.adapters.hyperliquid import (
                HyperliquidEnvironment, HyperliquidExecutionClientConfig, HyperliquidExecutionClientFactory,
            )
            pfx = "HYPERLIQUID_" if is_main else "HYPERLIQUID_TESTNET_"
            creds = _require_env(env, (pfx + "ACCOUNT_ADDRESS", pfx + "PK"), "HL")
            clients.append((
                "HYPERLIQUID",
                HyperliquidExecutionClientFactory(),
                HyperliquidExecutionClientConfig(
                    account_id=nt_model.AccountId.from_str("HYPERLIQUID-001"),
                    environment=HyperliquidEnvironment.MAINNET if is_main else HyperliquidEnvironment.TESTNET,
                    private_key=creds[pfx + "PK"],
                    account_address=creds[pfx + "ACCOUNT_ADDRESS"],
                    vault_address=env.get(pfx + "VAULT"),
                    include_builder_attribution=False,
                ),
            ))
        elif venue == "LIGHTER":
            from nautilus_trader.adapters.lighter import (
                LighterEnvironment, LighterExecutionClientConfig, LighterExecutionClientFactory, LighterExecutionControl,
            )
            pfx = "LIGHTER_" if is_main else "LIGHTER_TESTNET_"
            creds = _require_env(env, (pfx + "ACCOUNT_INDEX", pfx + "API_KEY_INDEX", pfx + "API_SECRET"), "LIGHTER")
            clients.append((
                "LIGHTER",
                LighterExecutionClientFactory(),
                LighterExecutionClientConfig(
                    account_id=nt_model.AccountId.from_str("LIGHTER-001"),
                    control=LighterExecutionControl(),
                    environment=LighterEnvironment.MAINNET if is_main else LighterEnvironment.TESTNET,
                    account_index=int(creds[pfx + "ACCOUNT_INDEX"]),
                    api_key_index=int(creds[pfx + "API_KEY_INDEX"]),
                    private_key=creds[pfx + "API_SECRET"],
                    sendtx_quota_per_min=36,
                ),
            ))
        elif venue == "OKX":
            from nautilus_trader.adapters.okx import (
                OKXEnvironment, OKXExecutionClientConfig, OKXExecutionClientFactory, OKXInstrumentType, OKXMarginMode,
            )
            pfx = "OKX_" if is_main else "OKX_DEMO_"
            creds = _require_env(env, (pfx + "API_KEY", pfx + "API_SECRET", pfx + "API_PASSPHRASE"), "OKX")
            clients.append((
                "OKX",
                OKXExecutionClientFactory(),
                OKXExecutionClientConfig(
                    account_id=nt_model.AccountId.from_str("OKX-001"),
                    instrument_types=[OKXInstrumentType.SWAP],
                    environment=OKXEnvironment.LIVE if is_main else OKXEnvironment.DEMO,
                    api_key=creds[pfx + "API_KEY"],
                    api_secret=creds[pfx + "API_SECRET"],
                    api_passphrase=creds[pfx + "API_PASSPHRASE"],
                    margin_mode=OKXMarginMode.CROSS,
                ),
            ))
        elif venue == "BYBIT":
            from nautilus_trader.adapters.bybit import (
                BybitEnvironment, BybitExecutionClientConfig, BybitExecutionClientFactory, BybitMarginMode, BybitProductType,
            )
            pfx = "BYBIT_" if is_main else "BYBIT_TESTNET_"
            creds = _require_env(env, (pfx + "API_KEY", pfx + "API_SECRET"), "BYBIT")
            clients.append((
                "BYBIT",
                BybitExecutionClientFactory(),
                BybitExecutionClientConfig(
                    account_id=nt_model.AccountId.from_str("BYBIT-001"),
                    product_types=[BybitProductType.LINEAR],
                    environment=BybitEnvironment.MAINNET if is_main else BybitEnvironment.TESTNET,
                    api_key=creds[pfx + "API_KEY"],
                    api_secret=creds[pfx + "API_SECRET"],
                    margin_mode=BybitMarginMode.REGULAR_MARGIN,
                ),
            ))
    return clients


def _create_carry_strategy(
    plan: CarryPlan,
    ledger: CarryLedger,
    *,
    mode: str,
    recorder: CarryRecorder | None = None,
    environ: Mapping[str, str] | None = None,
    account_reader: CarryStartupAccountReader | None = None,
):
    from nautilus_trader.model import InstrumentId
    from nautilus_trader.trading import Strategy, StrategyConfig

    class CarryStrategy(Strategy):
        def __init__(self) -> None:
            super().__init__(StrategyConfig())
            self.plan = plan
            self.ledger = ledger
            self.mode = mode
            self.recorder = recorder
            self.account_reader = (
                account_reader
                if account_reader is not None
                else CarryStartupAccountReader(environment=plan.environment, environ=environ)
            )
            self.feed = PublicCarryFeed(venues=plan.venues, environment=plan.environment)
            self.subs = SubscriptionManager(
                top_k=plan.runtime.watch_top_k,
                unsubscribe_hysteresis_secs=plan.runtime.unsubscribe_hysteresis_secs,
            )
            self.basis_tracker = BasisStopTracker(
                basis_stop_bps=plan.risk.basis_stop_bps,
                persist_mins=plan.risk.basis_persist_mins,
                max_gap_secs=max(30, plan.runtime.slow_poll_secs + 15),
            )
            self.fast_risk = FastRiskEvaluator(plan.risk)
            self.paper_gateway = PaperVenueGateway(fill_maker_immediately=False, use_realistic_bbo=True) if mode == "paper" else None
            self.gateway = self.paper_gateway if self.paper_gateway is not None else _NautilusStrategyGateway(self)
            self.controller = CarryExecutionController(
                ledger=ledger,
                gateway=self.gateway,
                risk=plan.risk,
                runtime=plan.runtime,
                policy=plan.policy,
            )
            self.live_mids: dict[str, float] = {}
            self.live_mid_ts: dict[str, float] = {}
            self.live_quotes: dict[str, tuple[float, float]] = {}
            self.live_quote_ts: dict[str, float] = {}
            self.snapshot: dict[str, dict[str, LegMarketInfo]] = {}
            self.reconciled = mode == "paper"
            self._shutdown_requested = threading.Event()
            self._shutdown_drain_started_ts: float | None = None
            self._shutdown_drain_timeout_secs: float = _GRACEFUL_SHUTDOWN_DRAIN_SECS
            self._stop_node_cb: Any = None
            self._stop_node_triggered: bool = False
            self._slow_poll_lock = threading.Lock()
            self._slow_poll_executor: ThreadPoolExecutor | None = None
            self._slow_poll_inflight: bool = False
            self._slow_poll_stopped: bool = False
            self._pending_slow_result: _SlowPollResult | None = None
            self._last_slow_poll_error: str | None = None

        def close_slow_poller(self, *, wait: bool = False) -> None:
            with self._slow_poll_lock:
                self._slow_poll_stopped = True
                self._pending_slow_result = None
                inflight = self._slow_poll_inflight
                executor = self._slow_poll_executor
                self._slow_poll_executor = None
            if executor is not None:
                executor.shutdown(wait=(wait or not inflight), cancel_futures=True)

        def _can_trade(self) -> bool:
            if self.mode not in ("paper", "live") or not self.reconciled:
                return False
            rec_halted, _ = self.ledger.is_reconcile_halted()
            return not rec_halted

        def request_graceful_shutdown(
            self,
            *,
            stop_cb: Any = None,
            drain_timeout_secs: float = _GRACEFUL_SHUTDOWN_DRAIN_SECS,
        ) -> None:
            self._stop_node_cb = stop_cb
            self._shutdown_drain_timeout_secs = max(0.0, float(drain_timeout_secs))
            self._shutdown_requested.set()

        def _start_graceful_shutdown_drain(self, now_s: int) -> None:
            if self._shutdown_drain_started_ts is None:
                self._shutdown_drain_started_ts = time.monotonic()
            if self._can_trade():
                self.controller.begin_shutdown(now_s=now_s, live_mids=self.live_mids, live_quotes=self.live_quotes)
            else:
                self.controller._stopping = True

        def _drive_graceful_shutdown(self, now_s: int) -> None:
            if not self._shutdown_requested.is_set() or self._stop_node_triggered:
                return
            self._start_graceful_shutdown_drain(now_s)
            if self._can_trade() and self.ledger.active_positions():
                try:
                    specs = self._specs_from_cache()
                except RuntimeError:
                    specs = {}
                self.controller.reconcile_shutdown_positions(specs=specs, now_s=now_s, live_mids=self.live_mids)
            else:
                specs = {}
            ready, blockers = self.controller.is_shutdown_ready(specs=specs, live_mids=self.live_mids)
            elapsed = time.monotonic() - (self._shutdown_drain_started_ts or time.monotonic())
            reconcile_halted = self.ledger.is_reconcile_halted()[0]
            if ready or (reconcile_halted and not self.controller.has_any_in_flight_orders()):
                self._stop_node_triggered = True
                if self._stop_node_cb is not None:
                    self._stop_node_cb()
                return
            if elapsed >= self._shutdown_drain_timeout_secs:
                if not reconcile_halted:
                    reason = f"shutdown_timeout:{';'.join(blockers[:3])}"[:240]
                    self.ledger.set_halt("HALTED_RECONCILE", reason, now_s)
                    self.ledger.record_event(
                        "graceful_shutdown_timeout",
                        {"blockers": blockers, "elapsed_secs": round(elapsed, 2)},
                        now_s,
                    )
                self._stop_node_triggered = True
                if self._stop_node_cb is not None:
                    self._stop_node_cb()

        def on_stop(self) -> None:
            now_s = int(time.time())
            if self._can_trade():
                canceled = self.controller.begin_shutdown(now_s=now_s, live_mids=self.live_mids, live_quotes=self.live_quotes)
            else:
                self.controller._stopping = True
                canceled = []
            self.close_slow_poller(wait=False)
            try:
                self.log.info(
                    f"[carry] on_stop canceled_makers={len(canceled)} open_positions={len(self.ledger.active_positions())}"
                )
            except Exception:
                pass

        def on_start(self) -> None:
            if not self.reconciled:
                self._reconcile_on_start()
            self.clock.set_timer(
                "carry_fast_risk",
                timedelta(seconds=self.plan.runtime.fast_risk_poll_secs),
                callback=self._on_fast_risk_timer,
            )
            self.clock.set_timer(
                "carry_basis_sample",
                timedelta(seconds=self.plan.runtime.basis_sample_secs),
                callback=self._on_basis_timer,
            )
            self.clock.set_timer(
                "carry_slow_poll",
                timedelta(seconds=self.plan.runtime.slow_poll_secs),
                callback=self._on_slow_timer,
            )
            # Run initial slow discovery immediately so dynamic subscriptions start right away
            self._on_slow_timer(None)

        def on_quote(self, tick: Any) -> None:
            iid = str(tick.instrument_id)
            bid = float(tick.bid_price)
            ask = float(tick.ask_price)
            if bid > 0 and ask >= bid:
                ts_now = time.time()
                quote_ts = _resolve_quote_event_ts(tick, ts_now)
                stale_s = float(self.plan.runtime.quote_stale_secs)
                if ts_now - quote_ts > stale_s:
                    existing_ts = self.live_quote_ts.get(iid)
                    if existing_ts is not None and ts_now - existing_ts > stale_s:
                        self.live_quotes.pop(iid, None)
                        self.live_quote_ts.pop(iid, None)
                    return
                prev_ts = self.live_quote_ts.get(iid)
                if prev_ts is not None and quote_ts < prev_ts:
                    return
                mid = 0.5 * (bid + ask)
                self.live_quotes[iid] = (bid, ask)
                self.live_quote_ts[iid] = quote_ts
                self.live_mids[iid] = mid
                self.live_mid_ts[iid] = quote_ts
                if self.paper_gateway is not None:
                    filled_coids = self.paper_gateway.update_quote(iid, Decimal(str(bid)), Decimal(str(ask)))
                    if filled_coids and self._can_trade():
                        self.controller.poll_active_orders(
                            live_mids=self.live_mids, now_s=int(ts_now), live_quotes=self.live_quotes
                        )
                if self.recorder is not None:
                    self.recorder.record_bbo(
                        instrument_id=iid,
                        bid=bid,
                        ask=ask,
                        mid=mid,
                        now_s=ts_now,
                        event_ts_ns=getattr(tick, "ts_event", None),
                    )

        def _evict_quote(self, iid: str) -> None:
            self.live_quotes.pop(iid, None)
            self.live_quote_ts.pop(iid, None)
            self.live_mids.pop(iid, None)
            self.live_mid_ts.pop(iid, None)

        def _fresh_mid_max_age_secs(self) -> float:
            return max(float(self.plan.runtime.quote_stale_secs), float(self.plan.runtime.slow_poll_secs) + 15.0)

        def _prune_stale_quotes(self, now_ts: float) -> list[str]:
            stale_s = float(self.plan.runtime.quote_stale_secs)
            stale_iids = [k for k in list(self.live_quotes) if now_ts - self.live_quote_ts.get(k, 0.0) > stale_s]
            for iid in stale_iids:
                self.live_quotes.pop(iid, None)
                self.live_quote_ts.pop(iid, None)
            return stale_iids

        def _overlay_fresh_quotes(self, now_s: float) -> None:
            stale_s = float(self.plan.runtime.quote_stale_secs)
            self._prune_stale_quotes(now_s)
            for vmap in self.snapshot.values():
                for v, leg in list(vmap.items()):
                    q = self.live_quotes.get(leg.instrument_id)
                    ts = self.live_quote_ts.get(leg.instrument_id, 0.0)
                    if q is not None and (now_s - ts) <= stale_s:
                        vmap[v] = LegMarketInfo(**{**leg.__dict__, "bid": q[0], "ask": q[1]})
                    else:
                        rest_ts = min(float(now_s), float(leg.fetched_ts)) if leg.fetched_ts > 0 else float(now_s)
                        if rest_ts >= self.live_mid_ts.get(leg.instrument_id, 0.0):
                            self.live_mids[leg.instrument_id] = leg.mid
                            self.live_mid_ts[leg.instrument_id] = rest_ts
                            if self.paper_gateway is not None:
                                self.paper_gateway.seed_quote(leg.instrument_id, Decimal(str(leg.bid)), Decimal(str(leg.ask)))

        def _handle_order_event(self, event: Any) -> None:
            if not self._can_trade():
                return
            coid = str(getattr(event, "client_order_id", ""))
            now_s = int(time.time())
            if coid and self.controller.is_tracked_order(coid):
                self.controller.poll_active_orders(
                    live_mids=self.live_mids, now_s=now_s, live_quotes=self.live_quotes
                )
            if self._shutdown_requested.is_set():
                self._drive_graceful_shutdown(now_s)

        def on_order_filled(self, event: Any) -> None:
            self._handle_order_event(event)

        def on_order_canceled(self, event: Any) -> None:
            self._handle_order_event(event)

        def on_order_expired(self, event: Any) -> None:
            self._handle_order_event(event)

        def on_order_rejected(self, event: Any) -> None:
            self._handle_order_event(event)

        def on_order_denied(self, event: Any) -> None:
            self._handle_order_event(event)

        def on_order_cancel_rejected(self, event: Any) -> None:
            coid = str(getattr(event, "client_order_id", ""))
            if coid:
                self.controller.on_cancel_rejected(coid)
            self._handle_order_event(event)

        def _reconcile_on_start(self) -> None:
            now_s = int(time.time())
            okx_multipliers: dict[str, Decimal] = {}
            try:
                for inst in self.cache.instruments():
                    iid = str(inst.id)
                    if iid.endswith(".OKX"):
                        mult = Decimal(str(getattr(inst, "multiplier", 1)))
                        if mult > 0:
                            okx_multipliers[iid] = mult
            except Exception:
                pass
            try:
                snap = self.account_reader.fetch_startup_snapshot(
                    self.plan.venues, okx_multipliers=okx_multipliers
                )
            except StartupSnapshotError as exc:
                reason = f"startup_snapshot_failed:{exc}"[:240]
                self.ledger.set_halt("HALTED_RECONCILE", reason, now_s)
                self.reconciled = False
                self.log.error(f"[carry] HALTED_RECONCILE: {reason}")
                return
            except Exception as exc:
                reason = f"startup_snapshot_failed:{type(exc).__name__}"[:240]
                self.ledger.set_halt("HALTED_RECONCILE", reason, now_s)
                self.reconciled = False
                self.log.error(f"[carry] HALTED_RECONCILE: {reason}")
                return

            res = self.ledger.reconcile_startup(
                venue_positions=snap.positions,
                venue_open_orders=snap.open_orders,
                tolerance_pct=self.plan.risk.reconcile_tolerance_pct,
                ts_s=now_s,
            )
            rec_halted, _ = self.ledger.is_reconcile_halted()
            self.reconciled = res.ok and not rec_halted
            if not self.reconciled:
                self.log.error(f"[carry] HALTED_RECONCILE: {res.discrepancies}")

        def _specs_from_cache(self) -> dict[str, InstrumentSpec]:
            specs: dict[str, InstrumentSpec] = {}
            for inst in self.cache.instruments():
                iid = str(inst.id)
                venue = str(inst.id.venue)
                norm_v = "HL" if venue == "HYPERLIQUID" else venue
                spec = InstrumentSpec(
                    instrument_id=iid,
                    venue=norm_v,
                    size_increment=Decimal(str(inst.size_increment)),
                    contract_multiplier=Decimal(str(getattr(inst, "multiplier", 1))),
                    min_quantity=Decimal(str(getattr(inst, "min_quantity", 0) or 0)),
                    min_notional_usd=_extract_money_decimal(getattr(inst, "min_notional", None)),
                    price_increment=Decimal(str(inst.price_increment)),
                )
                specs[iid] = spec
                if self.paper_gateway is not None and iid in self.live_mids:
                    self.paper_gateway.register_spec(spec, Decimal(str(self.live_mids[iid])))
            return specs

        def _on_fast_risk_timer(self, _event: Any) -> None:
            now_s = int(time.time())
            if self._shutdown_requested.is_set() and self._shutdown_drain_started_ts is None:
                self._start_graceful_shutdown_drain(now_s)
            if self._can_trade():
                self.controller.poll_active_orders(
                    live_mids=self.live_mids, now_s=now_s, live_quotes=self.live_quotes
                )
            active = self.ledger.active_positions()
            daily_pnl, streak = self.ledger.daily_stats(now_s)
            now_f = time.time()
            max_age = self._fresh_mid_max_age_secs()
            fresh_mids = {
                iid: px
                for iid, px in self.live_mids.items()
                if iid in self.live_mid_ts and (now_f - self.live_mid_ts[iid]) <= max_age
            }
            dec = self.fast_risk.evaluate(
                positions=active,
                live_mids=fresh_mids,
                daily_realized_pnl_usd=daily_pnl,
                consecutive_unhedged=streak,
                now_s=now_s,
            )
            if dec.halt_system and dec.halt_reason:
                if not self.ledger.is_kill_halted()[0]:
                    self.ledger.set_halt("HALTED_KILL", dec.halt_reason, now_s)
            if self._can_trade() and dec.close_positions:
                specs = self._specs_from_cache()
                for pid, reason in dec.close_positions:
                    if str(active[pid]["iid_long"]) in specs and str(active[pid]["iid_short"]) in specs:
                        self.controller.emergency_close_position(
                            pid, specs=specs, reason=reason, now_s=now_s, live_mids=self.live_mids
                        )
            if self._can_trade():
                halted_now, _ = self.ledger.is_halted()
                if not halted_now and not self._shutdown_requested.is_set() and not self.controller.is_stopping:
                    self._prune_stale_quotes(time.time())
                    specs_now = self._specs_from_cache()
                    self.controller.advance_pending_slices(
                        specs=specs_now, quotes=self.live_quotes, mids=self.live_mids, now_s=now_s
                    )
            if not self._shutdown_requested.is_set() and not self.controller.is_stopping:
                self._consume_pending_slow_snapshot()
            if self._shutdown_requested.is_set():
                self._drive_graceful_shutdown(now_s)

        def _on_basis_timer(self, _event: Any) -> None:
            now_f = time.time()
            now_s = int(now_f)
            max_age = self._fresh_mid_max_age_secs()
            active = self.ledger.active_positions()
            specs = self._specs_from_cache() if active else {}
            for pid, pos in active.items():
                iid_l = str(pos["iid_long"])
                iid_s = str(pos["iid_short"])
                ml = self.live_mids.get(iid_l)
                ms = self.live_mids.get(iid_s)
                ts_l = self.live_mid_ts.get(iid_l)
                ts_s = self.live_mid_ts.get(iid_s)
                fresh = (
                    ml is not None
                    and ms is not None
                    and ml > 0
                    and ms > 0
                    and ts_l is not None
                    and ts_s is not None
                    and (now_f - ts_l) <= max_age
                    and (now_f - ts_s) <= max_age
                )
                b_now = ((ms - ml) / (0.5 * (ml + ms)) * 1e4) if fresh else None
                sample_ts = int(min(ts_l, ts_s)) if fresh and ts_l is not None and ts_s is not None else None
                hit, _ = self.basis_tracker.observe(
                    position_id=pid,
                    entry_basis_bps=float(pos["entry_basis_bps"]),
                    basis_now_bps=b_now,
                    now_s=now_s,
                    sample_ts=sample_ts,
                )
                if hit and self._can_trade():
                    if iid_l in specs and iid_s in specs:
                        self.controller.emergency_close_position(
                            pid, specs=specs, reason="basis_stop_30m", now_s=now_s, live_mids=self.live_mids
                        )

        def _trigger_slow_poll(self, *, now_s: int, priority_symbols: frozenset[str]) -> bool:
            with self._slow_poll_lock:
                if self._slow_poll_stopped or self._slow_poll_inflight:
                    return False
                if self._slow_poll_executor is None:
                    self._slow_poll_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="carry-slow-poll")
                self._slow_poll_inflight = True
                executor = self._slow_poll_executor
                feed = self.feed
                min_vol = float(self.plan.policy.min_24h_vol_usd)

            def _worker() -> None:
                result: _SlowPollResult | None = None
                try:
                    raw = feed.poll_snapshot(
                        now_s=now_s,
                        priority_symbols=set(priority_symbols),
                        min_24h_vol_usd=min_vol,
                    )
                    frozen = MappingProxyType({sym: MappingProxyType(dict(vmap)) for sym, vmap in raw.items()})
                    partial = tuple(sorted(getattr(feed, "last_venue_errors", {}).items()))
                    result = _SlowPollResult(poll_ts_s=now_s, snapshot=frozen, partial_errors=partial)
                except Exception as exc:
                    result = _SlowPollResult(poll_ts_s=now_s, snapshot=None, error=f"{type(exc).__name__}: {exc}")
                finally:
                    with self._slow_poll_lock:
                        self._slow_poll_inflight = False
                        if not self._slow_poll_stopped and result is not None:
                            self._pending_slow_result = result

            try:
                executor.submit(_worker)
            except Exception as exc:
                with self._slow_poll_lock:
                    self._slow_poll_inflight = False
                try:
                    self.log.warning(f"[carry] slow poll dispatch failed: {exc}")
                except Exception:
                    pass
                return False
            return True

        def _consume_pending_slow_snapshot(self) -> bool:
            with self._slow_poll_lock:
                res = self._pending_slow_result
                self._pending_slow_result = None
            if res is None:
                return False
            if res.error is not None:
                self._last_slow_poll_error = res.error
                self.log.warning(f"[carry] slow poll failed: {res.error}")
                return False
            if res.snapshot is None:
                return False
            now_s = int(time.time())
            max_slow_age_s = max(2 * self.plan.runtime.slow_poll_secs, 180)
            if res.poll_ts_s <= 0 or (now_s - res.poll_ts_s) > max_slow_age_s:
                age_s = now_s - res.poll_ts_s
                self._last_slow_poll_error = f"stale_slow_snapshot:age={age_s}s"
                self.log.warning(f"[carry] discarding stale slow poll snapshot: age={age_s}s > {max_slow_age_s}s")
                return False
            if res.partial_errors:
                summary = "; ".join(f"{v}={err}" for v, err in res.partial_errors)
                self._last_slow_poll_error = f"partial_venue_failures:{summary}"
                self.log.warning(f"[carry] slow poll partial venue failures: {summary}")
            else:
                self._last_slow_poll_error = None
            self._apply_slow_snapshot(res.snapshot, poll_ts_s=res.poll_ts_s)
            return True

        def _apply_slow_snapshot(
            self,
            frozen_snapshot: Mapping[str, Mapping[str, LegMarketInfo]],
            *,
            poll_ts_s: int | None = None,
        ) -> None:
            now_s = int(time.time())
            max_slow_age_s = max(2 * self.plan.runtime.slow_poll_secs, 180)
            if poll_ts_s is not None and (poll_ts_s <= 0 or (now_s - poll_ts_s) > max_slow_age_s):
                return
            active = self.ledger.active_positions()
            open_syms = {str(p["symbol"]) for p in active.values()}
            self.snapshot = {sym: dict(vmap) for sym, vmap in frozen_snapshot.items()}

            # Overlay only fresh WS BBO quotes onto slow snapshot; stale quotes are evicted to REST
            self._overlay_fresh_quotes(time.time())

            entry_snapshot = {
                sym: {
                    v: leg
                    for v, leg in vmap.items()
                    if leg.fetched_ts <= 0 or (now_s - leg.fetched_ts) <= max_slow_age_s
                }
                for sym, vmap in self.snapshot.items()
            }

            venue_used: dict[str, Decimal] = {}
            for pos in active.values():
                lv, sv = str(pos["long_venue"]), str(pos["short_venue"])
                venue_used[lv] = venue_used.get(lv, Decimal("0")) + self.plan.risk.leg_notional_usd
                venue_used[sv] = venue_used.get(sv, Decimal("0")) + self.plan.risk.leg_notional_usd

            cooldown_syms = self.ledger.stop_loss_cooldown_symbols(
                now_s=now_s, cooldown_hours=self.plan.risk.stop_loss_cooldown_hours
            )
            base_excl = set(self.plan.excluded_symbols)
            strict_cands = evaluate_entry_candidates(
                entry_snapshot,
                open_symbols=open_syms,
                venue_notional_used=venue_used,
                excluded_symbols=base_excl | cooldown_syms,
                policy=self.plan.policy,
                risk=self.plan.risk,
                now_s=now_s,
                respect_capacity=False,
            )
            watch_cands = evaluate_watch_candidates(
                self.snapshot,
                strict_candidates=strict_cands,
                open_symbols=open_syms,
                excluded_symbols=base_excl,
                policy=self.plan.policy,
                top_k=self.plan.runtime.watch_top_k,
                min_watch_carry_bps_h=float(self.plan.runtime.watch_fill_min_carry_bps_h),
            )

            open_iids = {str(p["iid_long"]) for p in active.values()} | {str(p["iid_short"]) for p in active.values()}
            to_sub, to_unsub = self.subs.reconcile(open_instrument_ids=open_iids, ranked_candidates=watch_cands, now_s=now_s)
            for iid in to_sub:
                try:
                    self.subscribe_quotes(InstrumentId.from_str(iid))
                except Exception:
                    pass
            for iid in to_unsub:
                self._evict_quote(iid)
                try:
                    self.unsubscribe_quotes(InstrumentId.from_str(iid))
                except Exception:
                    pass

            if self.recorder is not None:
                self.recorder.record_slow_snapshot(
                    ts_s=now_s,
                    snapshot=self.snapshot,
                    candidates=strict_cands,
                    active_count=len(active),
                    subscribed_count=len(self.subs.active_instruments),
                )

            self.log.info(
                f"[carry] active={len(active)} strict={len(strict_cands)} watch={len(watch_cands)} sub={len(self.subs.active_instruments)} (+{len(to_sub)}/-{len(to_unsub)})"
            )
            if not self._can_trade():
                return
            halted, _ = self.ledger.is_halted()
            if halted or self._shutdown_requested.is_set() or self.controller.is_stopping:
                return

            specs = self._specs_from_cache()
            # Check carry_decay exits
            for pid, pos in list(active.items()):
                vmap = self.snapshot.get(str(pos["symbol"]), {})
                ll, ls = vmap.get(str(pos["long_venue"])), vmap.get(str(pos["short_venue"]))
                if ll and ls:
                    do_exit, rsn = should_exit_carry_decay(
                        opened_ts=int(pos["opened_ts"]), leg_l=ll, leg_s=ls, policy=self.plan.policy, now_s=now_s
                    )
                    if do_exit and str(pos["iid_long"]) in specs and str(pos["iid_short"]) in specs:
                        self.controller.start_sliced_close(
                            pid, specs=specs, quotes=self.live_quotes, mids=self.live_mids, reason=rsn, now_s=now_s
                        )

            daily_pnl_now, streak_now = self.ledger.daily_stats(now_s)
            kill_dec = self.fast_risk.evaluate_account_guards(
                daily_realized_pnl_usd=daily_pnl_now,
                consecutive_unhedged=streak_now,
            )
            if kill_dec.halt_system and kill_dec.halt_reason:
                if not self.ledger.is_kill_halted()[0]:
                    self.ledger.set_halt("HALTED_KILL", kill_dec.halt_reason, now_s)
                return

            # Open highest-ranked entry candidate if slots & venue/gross capacity remain
            active_after = self.ledger.active_positions()
            open_syms_after = {str(p["symbol"]) for p in active_after.values()}
            venue_used_after: dict[str, Decimal] = {}
            for pos in active_after.values():
                lv, sv = str(pos["long_venue"]), str(pos["short_venue"])
                venue_used_after[lv] = venue_used_after.get(lv, Decimal("0")) + self.plan.risk.leg_notional_usd
                venue_used_after[sv] = venue_used_after.get(sv, Decimal("0")) + self.plan.risk.leg_notional_usd
            gross_used_after = sum(venue_used_after.values(), Decimal("0"))
            leg_n = self.plan.risk.leg_notional_usd
            if (
                len(active_after) < self.plan.risk.max_slots
                and gross_used_after + Decimal("2") * leg_n <= self.plan.risk.max_gross_notional_usd
            ):
                for cand in strict_cands:
                    if cand.symbol in open_syms_after:
                        continue
                    if venue_used_after.get(cand.long_venue, Decimal("0")) + leg_n > self.plan.risk.max_venue_notional_usd:
                        continue
                    if venue_used_after.get(cand.short_venue, Decimal("0")) + leg_n > self.plan.risk.max_venue_notional_usd:
                        continue
                    if (
                        cand.long_leg.instrument_id in specs
                        and cand.short_leg.instrument_id in specs
                        and cand.long_leg.instrument_id in self.live_quotes
                        and cand.short_leg.instrument_id in self.live_quotes
                    ):
                        pid, err = self.controller.start_entry(cand, specs=specs, now_s=now_s)
                        if pid:
                            self.log.info(f"[carry] opened {cand.symbol} {cand.long_venue}->{cand.short_venue} score={cand.net_score_bps_h:.2f}")
                            break
                        if err:
                            self.log.debug(f"[carry] skip {cand.symbol}: {err}")

        def _on_slow_timer(self, _event: Any) -> None:
            self._consume_pending_slow_snapshot()
            if self._shutdown_requested.is_set() or self.controller.is_stopping:
                return
            now_s = int(time.time())
            open_syms = frozenset(str(p["symbol"]) for p in self.ledger.active_positions().values())
            started = self._trigger_slow_poll(now_s=now_s, priority_symbols=open_syms)
            if not started and self._slow_poll_inflight:
                try:
                    self.log.warning("[carry] slow poll still in flight; skipping overlapping poll")
                except Exception:
                    pass

    return CarryStrategy()


def _live_exec_engine_config(mode: str):
    from nautilus_trader.live import LiveExecutionEngineConfig

    enable_reconcile = mode in ("readonly", "live")
    return LiveExecutionEngineConfig(
        reconciliation=enable_reconcile,
        reconciliation_lookback_mins=60 if enable_reconcile else None,
        generate_missing_orders=enable_reconcile,
        inflight_check_interval_ms=0,
    )


def build_live_node(plan: CarryPlan, *, mode: str, environ: Mapping[str, str] | None = None):
    """Build Nautilus LiveNode for paper (no exec clients) or readonly/live (with exec clients)."""
    from nautilus_trader.common import CacheConfig, Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveDataEngineConfig, LiveNode
    from nautilus_trader.model import TraderId

    plan_domain = ledger_domain_for_mode(plan.mode)
    run_domain = ledger_domain_for_mode(mode)
    if plan_domain != run_domain:
        raise LedgerOwnershipError(
            f"cannot run mode '{mode}' (domain={run_domain}) against '{plan.mode}' config state_path {plan.state_path} (domain={plan_domain})"
        )

    ledger = CarryLedger(
        plan.state_path,
        plan.fingerprint(),
        ledger_domain=run_domain,
        legacy_fingerprint=plan.legacy_fingerprint(),
    )
    try:
        builder = (
            LiveNode.builder(f"CARRY-{mode.upper()}", TraderId.from_str("CARRY-001"), Environment.LIVE)
            .with_logging(LoggerConfig(stdout_level=LogLevel.INFO, fileout_level=LogLevel.OFF, print_config=False))
            .with_cache_config(CacheConfig(tick_capacity=1, bar_capacity=1, save_market_data=False, persist_account_events=False))
            .with_data_engine_config(LiveDataEngineConfig(emit_quotes_from_book=False, emit_quotes_from_book_depths=False))
            .with_exec_engine_config(_live_exec_engine_config(mode))
            .with_reconciliation(mode in ("readonly", "live"))
            .with_load_state(False)
            .with_save_state(False)
            .with_timeout_connection(60)
            .with_timeout_disconnection_secs(15)
        )
        for client_id, factory, config in build_data_clients(plan):
            builder = builder.add_data_client(client_id, factory, config)
        if mode in ("readonly", "live"):
            for client_id, factory, config in build_execution_clients(plan, environ=environ):
                builder = builder.add_exec_client(client_id, factory, config)
        node = builder.build()
        recorder = (
            CarryRecorder(plan.runtime.record_dir, bbo_interval_secs=plan.runtime.bbo_record_interval_secs)
            if plan.runtime.record_dir is not None
            else None
        )
        strategy = _create_carry_strategy(plan, ledger, mode=mode, recorder=recorder, environ=environ)
        node.add_strategy(strategy)
        return node, ledger, strategy
    except BaseException:
        ledger.close()
        raise


def run_node(plan: CarryPlan, *, mode: str, execute_live: bool = False, duration_secs: int | None = None) -> int:
    plan_domain = ledger_domain_for_mode(plan.mode)
    run_domain = ledger_domain_for_mode(mode)
    if plan_domain != run_domain:
        raise LedgerOwnershipError(
            f"cannot run mode '{mode}' (domain={run_domain}) against '{plan.mode}' config state_path {plan.state_path} (domain={plan_domain})"
        )
    if mode == "live" and not execute_live:
        raise ValueError("--mode live refuses to start without explicit --execute-live")
    dur = plan.runtime.duration_secs if duration_secs is None else duration_secs
    node, ledger, strategy = build_live_node(plan, mode=mode)
    handle = node.handle()
    timer = None
    hard_timer = None
    timers_lock = threading.RLock()
    stopped_evt = threading.Event()
    graceful_started = False

    def _hard_stop() -> None:
        if not stopped_evt.is_set():
            stopped_evt.set()
            handle.stop()

    def _trigger_graceful_stop() -> None:
        nonlocal hard_timer, graceful_started
        with timers_lock:
            if stopped_evt.is_set() or graceful_started:
                return
            graceful_started = True
        if hasattr(strategy, "request_graceful_shutdown"):
            strategy.request_graceful_shutdown(
                stop_cb=_hard_stop,
                drain_timeout_secs=_GRACEFUL_SHUTDOWN_DRAIN_SECS,
            )
            with timers_lock:
                if not stopped_evt.is_set() and hard_timer is None:
                    hard_timer = threading.Timer(_GRACEFUL_SHUTDOWN_DRAIN_SECS + 5.0, _hard_stop)
                    hard_timer.daemon = True
                    hard_timer.start()
        else:
            _hard_stop()

    def _on_stop_signal(_signum: int = 0, _frame: Any = None) -> None:
        with timers_lock:
            already_graceful = graceful_started
        if not already_graceful:
            _trigger_graceful_stop()
        else:
            _hard_stop()

    if dur > 0:
        timer = threading.Timer(dur, _trigger_graceful_stop)
        timer.daemon = True
        timer.start()
    try:
        if callable(getattr(node, "run_async", None)):
            async def _run_hosted() -> None:
                loop = asyncio.get_running_loop()
                installed_sigs: list[signal.Signals] = []
                sigs = [signal.SIGINT]
                if hasattr(signal, "SIGTERM"):
                    sigs.append(signal.SIGTERM)
                for sig in sigs:
                    try:
                        loop.add_signal_handler(sig, _on_stop_signal)
                        installed_sigs.append(sig)
                    except (NotImplementedError, RuntimeError, ValueError):
                        pass
                try:
                    await node.run_async()
                finally:
                    for sig in installed_sigs:
                        try:
                            loop.remove_signal_handler(sig)
                        except (NotImplementedError, RuntimeError, ValueError):
                            pass

            try:
                asyncio.run(_run_hosted())
            except KeyboardInterrupt:
                _hard_stop()
        else:
            prev_handlers: dict[signal.Signals, Any] = {}
            if threading.current_thread() is threading.main_thread():
                sigs = [signal.SIGINT]
                if hasattr(signal, "SIGTERM"):
                    sigs.append(signal.SIGTERM)
                for sig in sigs:
                    try:
                        prev_handlers[sig] = signal.getsignal(sig)
                        signal.signal(sig, _on_stop_signal)
                    except (ValueError, OSError, RuntimeError):
                        pass
            try:
                node.run()
            except KeyboardInterrupt:
                _hard_stop()
            finally:
                for sig, prev in prev_handlers.items():
                    try:
                        signal.signal(sig, prev)
                    except (ValueError, OSError, RuntimeError):
                        pass
    finally:
        stopped_evt.set()
        if timer is not None:
            timer.cancel()
        with timers_lock:
            if hard_timer is not None:
                hard_timer.cancel()
        if hasattr(strategy, "close_slow_poller"):
            try:
                strategy.close_slow_poller(wait=False)
            except Exception:
                pass
        if getattr(strategy, "recorder", None) is not None:
            try:
                strategy.recorder.close()
            except Exception:
                pass
        ledger.close()
    return 0

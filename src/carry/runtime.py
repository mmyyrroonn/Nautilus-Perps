"""NautilusTrader LiveNode assembly and dynamic Strategy lifecycle for Route A Carry."""
from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
import os
from pathlib import Path
import threading
import time
from typing import Any, Mapping
import uuid

from .config import CarryPlan, CarryPolicy
from .executor import CarryExecutionController, OrderAck, PaperVenueGateway
from .funding_feed import LegMarketInfo, PublicCarryFeed
from .ledger import CarryLedger
from .recorder import CarryRecorder
from .risk import BasisStopTracker, FastRiskEvaluator
from .signal import (
    InstrumentSpec,
    SubscriptionManager,
    evaluate_entry_candidates,
    evaluate_watch_candidates,
    should_exit_carry_decay,
)


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
        order = self._orders.get(client_order_id)
        if order is not None and not order.is_closed:
            self.strategy.cancel_order(order)
        return self.poll_order(client_order_id=client_order_id, venue=venue, instrument_id=instrument_id)

    def poll_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        from nautilus_trader.model import ClientOrderId

        order = self.strategy.cache.order(ClientOrderId(client_order_id)) or self._orders.get(client_order_id)
        if order is None:
            return OrderAck(client_order_id, "", "OPEN", Decimal("0"))
        inst = self.strategy.cache.instrument(order.instrument_id)
        mult = Decimal(str(getattr(inst, "multiplier", 1))) if inst is not None else Decimal("1")
        filled = Decimal(str(order.filled_qty)) * mult
        status_str = str(order.status).upper()
        if "FILLED" in status_str and "PARTIAL" not in status_str:
            st = "FILLED"
        elif "PARTIAL" in status_str:
            st = "PARTIAL"
        elif "CANCELED" in status_str or "EXPIRED" in status_str:
            st = "CANCELED"
        elif "REJECTED" in status_str or "DENIED" in status_str:
            st = "REJECTED"
        else:
            st = "OPEN"
        return OrderAck(client_order_id, str(order.venue_order_id or ""), st, filled)


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
            self.feed = PublicCarryFeed(venues=plan.venues)
            self.subs = SubscriptionManager(
                top_k=plan.runtime.watch_top_k,
                unsubscribe_hysteresis_secs=plan.runtime.unsubscribe_hysteresis_secs,
            )
            self.basis_tracker = BasisStopTracker(
                basis_stop_bps=plan.risk.basis_stop_bps,
                persist_mins=plan.risk.basis_persist_mins,
            )
            self.fast_risk = FastRiskEvaluator(plan.risk)
            self.paper_gateway = PaperVenueGateway(fill_maker_immediately=True) if mode == "paper" else None
            self.gateway = self.paper_gateway if self.paper_gateway is not None else _NautilusStrategyGateway(self)
            self.controller = CarryExecutionController(
                ledger=ledger,
                gateway=self.gateway,
                risk=plan.risk,
                runtime=plan.runtime,
            )
            self.live_mids: dict[str, float] = {}
            self.live_quotes: dict[str, tuple[float, float]] = {}
            self.live_quote_ts: dict[str, float] = {}
            self.snapshot: dict[str, dict[str, LegMarketInfo]] = {}
            self.reconciled = mode == "paper"

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
                mid = 0.5 * (bid + ask)
                self.live_quotes[iid] = (bid, ask)
                self.live_quote_ts[iid] = ts_now
                self.live_mids[iid] = mid
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

        def _overlay_fresh_quotes(self, now_s: float) -> None:
            stale_s = float(self.plan.runtime.quote_stale_secs)
            for iid in [k for k in self.live_quotes if now_s - self.live_quote_ts.get(k, 0.0) > stale_s]:
                self.live_quotes.pop(iid, None)
                self.live_quote_ts.pop(iid, None)
            for vmap in self.snapshot.values():
                for v, leg in list(vmap.items()):
                    q = self.live_quotes.get(leg.instrument_id)
                    ts = self.live_quote_ts.get(leg.instrument_id, 0.0)
                    if q is not None and (now_s - ts) <= stale_s:
                        vmap[v] = LegMarketInfo(**{**leg.__dict__, "bid": q[0], "ask": q[1]})
                    else:
                        self.live_mids[leg.instrument_id] = leg.mid

        def on_order_filled(self, event: Any) -> None:
            coid = str(event.client_order_id)
            if coid in self.controller._active_makers:
                now_s = int(time.time())
                ctx = self.controller._active_makers[coid]
                h_mid = Decimal(str(self.live_mids.get(ctx.spec_hedge.instrument_id, 1.0)))
                self.controller.poll_active_makers(live_mids=self.live_mids, now_s=now_s)
                _ = h_mid

        def _reconcile_on_start(self) -> None:
            now_s = int(time.time())
            venue_pos: dict[tuple[str, str], Decimal] = {}
            venue_orders: dict[tuple[str, str], int] = {}
            for pos in self.cache.positions_open():
                v = str(pos.instrument_id.venue)
                norm_v = "HL" if v == "HYPERLIQUID" else v
                qty = Decimal(str(pos.signed_qty))
                inst = self.cache.instrument(pos.instrument_id)
                mult = Decimal(str(getattr(inst, "multiplier", 1))) if inst is not None else Decimal("1")
                venue_pos[(norm_v, str(pos.instrument_id))] = qty * mult
            for ord_obj in self.cache.orders_open():
                v = str(ord_obj.instrument_id.venue)
                norm_v = "HL" if v == "HYPERLIQUID" else v
                key = (norm_v, str(ord_obj.instrument_id))
                venue_orders[key] = venue_orders.get(key, 0) + 1
            res = self.ledger.reconcile_startup(
                venue_positions=venue_pos,
                venue_open_orders=venue_orders,
                tolerance_pct=self.plan.risk.reconcile_tolerance_pct,
                ts_s=now_s,
            )
            self.reconciled = res.ok
            if not res.ok:
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
                    min_notional_usd=Decimal("0"),
                    price_increment=Decimal(str(inst.price_increment)),
                )
                specs[iid] = spec
                if self.paper_gateway is not None and iid in self.live_mids:
                    self.paper_gateway.register_spec(spec, Decimal(str(self.live_mids[iid])))
            return specs

        def _on_fast_risk_timer(self, _event: Any) -> None:
            now_s = int(time.time())
            if self.mode in ("paper", "live") and self.reconciled:
                self.controller.poll_active_makers(live_mids=self.live_mids, now_s=now_s)
                halted_now, _ = self.ledger.is_halted()
                if not halted_now:
                    specs_now = self._specs_from_cache()
                    self.controller.advance_pending_slices(
                        specs=specs_now, quotes=self.live_quotes, mids=self.live_mids, now_s=now_s
                    )
            active = self.ledger.active_positions()
            if not active:
                return
            daily_pnl, streak = self.ledger.daily_stats(now_s)
            dec = self.fast_risk.evaluate(
                positions=active,
                live_mids=self.live_mids,
                daily_realized_pnl_usd=daily_pnl,
                consecutive_unhedged=streak,
                now_s=now_s,
            )
            if dec.halt_system and dec.halt_reason:
                self.ledger.set_halt("HALTED_KILL", dec.halt_reason, now_s)
            if self.mode in ("paper", "live") and dec.close_positions:
                specs = self._specs_from_cache()
                for pid, reason in dec.close_positions:
                    if str(active[pid]["iid_long"]) in specs and str(active[pid]["iid_short"]) in specs:
                        self.controller.emergency_close_position(
                            pid, specs=specs, reason=reason, now_s=now_s, live_mids=self.live_mids
                        )

        def _on_basis_timer(self, _event: Any) -> None:
            now_s = int(time.time())
            active = self.ledger.active_positions()
            specs = self._specs_from_cache() if active else {}
            for pid, pos in active.items():
                ml = self.live_mids.get(str(pos["iid_long"]))
                ms = self.live_mids.get(str(pos["iid_short"]))
                b_now = ((ms - ml) / (0.5 * (ml + ms)) * 1e4) if (ml and ms) else None
                hit, _ = self.basis_tracker.observe(
                    position_id=pid,
                    entry_basis_bps=float(pos["entry_basis_bps"]),
                    basis_now_bps=b_now,
                    now_s=now_s,
                )
                if hit and self.mode in ("paper", "live"):
                    if str(pos["iid_long"]) in specs and str(pos["iid_short"]) in specs:
                        self.controller.emergency_close_position(
                            pid, specs=specs, reason="basis_stop_30m", now_s=now_s, live_mids=self.live_mids
                        )

        def _on_slow_timer(self, _event: Any) -> None:
            now_s = int(time.time())
            active = self.ledger.active_positions()
            open_syms = {str(p["symbol"]) for p in active.values()}
            try:
                self.snapshot = self.feed.poll_snapshot(
                    now_s=now_s,
                    priority_symbols=open_syms,
                    min_24h_vol_usd=float(self.plan.policy.min_24h_vol_usd),
                )
            except Exception as exc:
                self.log.warning(f"[carry] slow poll failed: {exc}")
                return

            # Overlay only fresh WS BBO quotes onto slow snapshot; stale quotes are evicted to REST
            self._overlay_fresh_quotes(time.time())

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
                self.snapshot,
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
            if self.mode not in ("paper", "live") or not self.reconciled:
                return
            halted, _ = self.ledger.is_halted()
            if halted:
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
                    if cand.long_leg.instrument_id in specs and cand.short_leg.instrument_id in specs:
                        pid, err = self.controller.start_entry(cand, specs=specs, now_s=now_s)
                        if pid:
                            self.log.info(f"[carry] opened {cand.symbol} {cand.long_venue}->{cand.short_venue} score={cand.net_score_bps_h:.2f}")
                            break
                        if err:
                            self.log.debug(f"[carry] skip {cand.symbol}: {err}")

    return CarryStrategy()


def build_live_node(plan: CarryPlan, *, mode: str, environ: Mapping[str, str] | None = None):
    """Build Nautilus LiveNode for paper (no exec clients) or readonly/live (with exec clients)."""
    from nautilus_trader.common import CacheConfig, Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveDataEngineConfig, LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import TraderId

    ledger = CarryLedger(plan.state_path, plan.fingerprint())
    builder = (
        LiveNode.builder(f"CARRY-{mode.upper()}", TraderId.from_str("CARRY-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.INFO, fileout_level=LogLevel.OFF, print_config=False))
        .with_cache_config(CacheConfig(tick_capacity=1, bar_capacity=1, save_market_data=False, persist_account_events=False))
        .with_data_engine_config(LiveDataEngineConfig(emit_quotes_from_book=False, emit_quotes_from_book_depths=False))
        .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=False, generate_missing_orders=False, inflight_check_interval_ms=0))
        .with_reconciliation(False)
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
    strategy = _create_carry_strategy(plan, ledger, mode=mode, recorder=recorder)
    node.add_strategy(strategy)
    return node, ledger, strategy


def run_node(plan: CarryPlan, *, mode: str, execute_live: bool = False, duration_secs: int | None = None) -> int:
    if mode == "live" and not execute_live:
        raise ValueError("--mode live refuses to start without explicit --execute-live")
    dur = plan.runtime.duration_secs if duration_secs is None else duration_secs
    node, ledger, strategy = build_live_node(plan, mode=mode)
    handle = node.handle()
    timer = None
    if dur > 0:
        timer = threading.Timer(dur, handle.stop)
        timer.daemon = True
        timer.start()
    try:
        node.run()
    except KeyboardInterrupt:
        handle.stop()
    finally:
        if timer is not None:
            timer.cancel()
        if getattr(strategy, "recorder", None) is not None:
            try:
                strategy.recorder.close()
            except Exception:
                pass
        ledger.close()
    return 0

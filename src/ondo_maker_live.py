"""Native execution bridge for the cancel-first maker; no startup test trades."""

from __future__ import annotations

import functools
import time
from collections import deque
from datetime import timedelta
from decimal import Decimal

from nautilus_trader.model import (
    AccountId,
    ClientOrderId,
    InstrumentId,
    OrderSide,
    TimeInForce,
)
from nautilus_trader.trading import Strategy

from ondo_continuous import ContinuousStrategyConfig
from ondo_maker import MARKETS, SECOND, ZERO, Instrument, MakerConfig, MakerEngine


def guarded(method):
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except Exception as e:  # noqa: BLE001 - keep cleanup alive; never print SDK payloads
            # Retain a small diagnostic without raw SDK data or credentials. The
            # timer continues cancellation/reduction after a callback failure.
            self.engine.fail(f"{method.__name__}:{type(e).__name__}")
            if method.__name__ != "drive":
                self.drive()

    return call


class OndoMakerStrategy(Strategy):
    def __new__(cls, config, **_kwargs):
        return super().__new__(cls, config)

    def __init__(self, config, *, session, factory, maker: MakerConfig, run_id: str):
        super().__init__(config)
        self.session, self.factory = session, factory
        self.engine = MakerEngine(maker, run_id)
        self.ids = {m: InstrumentId.from_str(f"{m}-USD-PERP.ONDO") for m in MARKETS}
        self.symbols = {v: k for k, v in self.ids.items()}
        self.account = AccountId.from_str(session.account_id)
        self.last_activity_wall_ns = 0
        self.account_health_until_ns = 0
        self.last_proof_started_ns = 0
        self.last_native_quote_ns: dict[str, int] = {}
        self.timer_name = "ondo-maker-supervision"
        self.driving = False
        self.latencies_ns: deque[int] = deque(maxlen=8192)
        self.command_counts = {"cancel": 0, "maker": 0, "close": 0}

    @guarded
    def on_start(self):
        self.clock.set_timer(
            self.timer_name, timedelta(milliseconds=50), callback=self.on_timer
        )
        for market, instrument_id in self.ids.items():
            instrument = self.cache.instrument(instrument_id)
            if instrument is None:
                raise ValueError("missing native instrument")
            self.engine.markets[market].instrument = Instrument(
                instrument.price_increment.as_decimal(),
                instrument.size_increment.as_decimal(),
                instrument.min_quantity.as_decimal()
                if instrument.min_quantity
                else instrument.size_increment.as_decimal(),
                instrument.min_notional.as_decimal()
                if instrument.min_notional
                else ZERO,
            )
            self.subscribe_quotes(instrument_id)
            self.subscribe_instrument_status(instrument_id)

    @guarded
    def on_instrument_status(self, status):
        market = self.symbols.get(status.instrument_id)
        if market and (status.is_quoting is False or status.is_trading is False):
            self.engine.disconnect("ondo", market)
            self.drive()

    @guarded
    def on_quote(self, quote):
        market = self.symbols.get(quote.instrument_id)
        if market is None:
            return
        wall = int(self.clock.timestamp_ns())
        previous = self.last_native_quote_ns.get(market, 0)
        if (
            quote.ts_init < previous
            or not 0 <= wall - quote.ts_init <= 2 * SECOND
            or quote.bid_size.as_decimal() <= 0
            or quote.ask_size.as_decimal() <= 0
        ):
            self.engine.disconnect("ondo", market)
        else:
            self.last_native_quote_ns[market] = quote.ts_init
            self.engine.on_ondo(
                market,
                time.monotonic_ns(),
                quote.bid_price.as_decimal(),
                quote.ask_price.as_decimal(),
            )
        self.drive()

    @guarded
    def binance_quote(self, market, now, bid, ask):
        self.engine.on_binance(market, now, bid, ask)
        self.drive()

    @guarded
    def binance_disconnect(self):
        self.engine.disconnect("binance")
        self.drive()

    def request_stop(self):
        self.engine.stop()
        self.drive()

    @guarded
    def on_timer(self, _event):
        for state in self.engine.markets.values():
            for pending in tuple(state.orders.values()):
                cached = self.cache.order(ClientOrderId.from_str(pending.client_id))
                if cached is not None and (
                    cached.is_closed or cached.filled_qty.as_decimal() > pending.filled
                ):
                    self.engine.update_order(
                        pending.client_id,
                        filled=cached.filled_qty.as_decimal(),
                        terminal=cached.is_closed,
                        now=time.monotonic_ns(),
                    )
                    self.last_activity_wall_ns = int(self.clock.timestamp_ns())
        self.refresh_account()
        self.drive()

    def refresh_account(self):
        snapshot = self.factory.production_trade_snapshot()
        transport = self.factory.read_only_snapshot()
        private_ready = bool(
            isinstance(transport, dict)
            and transport.get("run_id") == self.session.diagnostics_run_id
            and transport.get("run_state") == "trading_ready"
        )
        now = int(self.clock.timestamp_ns())
        valid = (
            isinstance(snapshot, dict)
            and snapshot.get("run_id") == self.session.diagnostics_run_id
            and snapshot.get("mode") == "continuous"
            and 0 <= now - snapshot.get("snapshot_unix_nanos", 0) <= 3 * SECOND
        )
        if not valid:
            # Native note_activity invalidates the completion proof on every own
            # order action. This is not a transport failure. Retain only the last
            # bounded health lease while awaiting the next reconciliation; it
            # authorizes neither new orders nor shutdown nor guessed reductions.
            healthy = (
                snapshot is None
                and private_ready
                and 0 < now <= self.account_health_until_ns
                and self.last_activity_wall_ns > self.last_proof_started_ns
            )
            self.engine.set_account(
                ready=False, reduction_ready=False, flat=False, healthy=healthy
            )
            if not healthy:
                self.account_health_until_ns = 0
            return
        current = (
            snapshot.get("reconciled_activity_generation")
            == snapshot.get("latest_activity_generation")
            and snapshot.get("snapshot_started_ns", 0) >= self.last_activity_wall_ns
        )
        expected = {
            name for state in self.engine.markets.values() for name in state.orders
        }
        observed = {o.get("client_order_id") for o in snapshot.get("open_orders", [])}
        inventory_matches = all(
            Decimal(snapshot.get("position_quantities", {}).get(str(self.ids[m]), "0"))
            == s.position
            for m, s in self.engine.markets.items()
        )
        no_unknown = snapshot.get("unknown_submissions") == 0
        owned = snapshot.get("account_positions_owned") is True
        no_foreign = snapshot.get("foreign_open_orders") == 0
        healthy = (
            snapshot.get("connected_ready") is True
            and no_unknown
            and owned
            and no_foreign
            and private_ready
            and (snapshot.get("native_ready") is True or not current)
        )
        self.last_proof_started_ns = snapshot.get("snapshot_started_ns", 0)
        self.account_health_until_ns = (
            snapshot["snapshot_unix_nanos"] + 3 * SECOND if healthy else 0
        )
        flat = (
            current
            and private_ready
            and observed == expected
            and inventory_matches
            and no_unknown
            and no_foreign
            and snapshot.get("reconciled_flat") is True
            and all(
                self.portfolio.net_position(i, self.account) == ZERO
                for i in self.ids.values()
            )
        )
        self.engine.set_account(
            ready=healthy
            and current
            and inventory_matches
            and snapshot.get("native_ready") is True,
            reduction_ready=snapshot.get("reduction_ready") is True,
            flat=flat,
            healthy=healthy,
        )

    @guarded
    def order_event(self, event):
        order = self.cache.order(event.client_order_id)
        if order is None:
            self.engine.fail("missing_cached_order_event")
            self.drive()
            return
        self.engine.update_order(
            str(event.client_order_id),
            filled=order.filled_qty.as_decimal(),
            terminal=order.is_closed,
            now=time.monotonic_ns(),
            rejected=type(event).__name__ in {"OrderDenied", "OrderRejected"},
        )
        self.last_activity_wall_ns = int(self.clock.timestamp_ns())
        self.drive()

    on_order_filled = order_event
    on_order_canceled = order_event
    on_order_expired = order_event
    on_order_rejected = order_event
    on_order_denied = order_event
    on_order_accepted = order_event
    on_order_cancel_rejected = order_event

    @guarded
    def drive(self):
        if self.driving:
            return
        start = time.monotonic_ns()
        self.driving = True
        try:
            commands = self.engine.step(start)
            cancel_failed = False
            dispatch_failed = False
            for command in commands:
                order = command.order
                client_id = ClientOrderId.from_str(order.client_id)
                if command.kind == "cancel":
                    cached = self.cache.order(client_id)
                    if cached is not None and not cached.is_closed:
                        try:
                            self.cancel_order(client_id)
                            self.command_counts["cancel"] += 1
                        except Exception:  # noqa: BLE001 - a failed callback must not stop cleanup
                            cancel_failed = True
                            order.last_cancel_ns = -1
                            self.engine.fail("cancel_dispatch_failed")
                    continue
                if (
                    cancel_failed
                    or dispatch_failed
                    or (
                        not order.reduce_only
                        and (
                            self.engine.stopping
                            or self.engine.reducing
                            or self.engine.markets[order.market].replacing
                        )
                    )
                ):
                    # This command was reserved but never dispatched.
                    self.engine.update_order(
                        order.client_id,
                        filled=ZERO,
                        terminal=True,
                        now=start,
                        rejected=True,
                    )
                    continue
                instrument = self.cache.instrument(self.ids[order.market])
                try:
                    native = self.order_factory.limit(
                        instrument_id=self.ids[order.market],
                        client_order_id=client_id,
                        order_side=OrderSide.BUY if order.side > 0 else OrderSide.SELL,
                        quantity=instrument.make_qty(order.quantity),
                        price=instrument.make_price(order.price),
                        time_in_force=TimeInForce.IOC
                        if order.reduce_only
                        else TimeInForce.GTC,
                        post_only=not order.reduce_only,
                        reduce_only=order.reduce_only,
                    )
                except Exception:  # noqa: BLE001 - a failed callback must not stop cleanup
                    self.engine.update_order(
                        order.client_id,
                        filled=ZERO,
                        terminal=True,
                        now=start,
                        rejected=True,
                    )
                    self.engine.fail("order_construction_failed")
                    continue
                self.last_activity_wall_ns = int(self.clock.timestamp_ns())
                # A dispatch exception leaves the outcome reserved/unknown until
                # the cache or native reconciliation supplies its terminal state.
                try:
                    self.submit_order(native)
                    self.command_counts["close" if order.reduce_only else "maker"] += 1
                except Exception:  # noqa: BLE001 - reserve uncertain outcomes; release unsent siblings
                    self.engine.fail("order_dispatch_outcome_unknown")
                    dispatch_failed = True
        finally:
            self.driving = False
            self.latencies_ns.append(time.monotonic_ns() - start)

    @guarded
    def on_stop(self):
        if self.timer_name in self.clock.timer_names():
            self.clock.cancel_timer(self.timer_name)

    def status(self):
        def phase(state):
            if state.position or any(o.reduce_only for o in state.orders.values()):
                return "flattening"
            if any(o.cancel_requested for o in state.orders.values()):
                return "waiting_cancel"
            if state.replacing and not self.engine.account_flat:
                return "waiting_account"
            return state.reason

        return {
            "stopping": self.engine.stopping,
            "safe_to_stop": self.engine.can_stop,
            "fault": self.engine.fault,
            "commands": dict(self.command_counts),
            "markets": {
                m: {
                    "state": phase(s),
                    "reason": s.reason,
                    "position": str(s.position),
                    "live_orders": len(s.orders),
                    "pause_until_ns": s.guard.pause_until,
                }
                for m, s in self.engine.markets.items()
            },
        }


def make_strategy(session, factory, *, maker, run_id):
    if not getattr(factory, "supports_cancel_first_unpaced_rest", False):
        raise ValueError(
            "install the cancel-first Ondo native candidate before using this launcher"
        )
    return OndoMakerStrategy(
        ContinuousStrategyConfig(
            instrument_id=InstrumentId.from_str(session.instrument_ids[0]),
            account_id=AccountId.from_str(session.account_id),
        ),
        session=session,
        factory=factory,
        maker=maker,
        run_id=run_id,
    )

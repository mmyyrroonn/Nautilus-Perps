"""Bounded current-book state and a public-data-only Nautilus runtime."""
from __future__ import annotations

from copy import deepcopy
from collections import deque
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
import sys
import threading
import time
from datetime import timedelta

from opportunity_core import (BookSnapshot, EventRecorder, MarketMetadata, evaluate_opportunity,
                              exact_decimal, _book_valid, _metadata_valid)
from opportunity_scan import ScanConfigError, ScanPlan


def instrument_decimal(value):
    if value is None:
        return None
    try:
        result = Decimal(str(value).split()[0])
    except (InvalidOperation, IndexError):
        return None
    return result if result.is_finite() else None


def market_metadata(market, instrument) -> MarketMetadata:
    """Use actual lot constraints; record explicit fee overrides as assumptions."""
    if str(instrument.id) != market.instrument_id or getattr(instrument, "is_inverse", False):
        raise ScanConfigError("instrument identity mismatch or inverse contract")
    step = instrument_decimal(getattr(instrument, "size_increment", None))
    native_multiplier = instrument_decimal(getattr(instrument, "multiplier", None))
    if step is None or step <= 0 or native_multiplier is None or native_multiplier <= 0:
        raise ScanConfigError("instrument quantity step or multiplier is unknown")
    multiplier = exact_decimal(Fraction(native_multiplier) * Fraction(market.canonical_multiplier))
    fee, source = market.taker_fee_bps, market.fee_source
    if fee is None:
        raw_fee = instrument_decimal(getattr(instrument, "taker_fee", None))
        # A generic instrument's zero is often an unpublished default. Lighter
        # explicitly publishes its zero rate; Ondo's positive metadata is usable.
        fee = exact_decimal(Fraction(raw_fee) * 10000) if raw_fee is not None and (
            raw_fee > 0 or market.venue in {"LIGHTER", "LIGHTER_RH", "BACKPACK"}) else None
        source = "instrument_metadata" if fee is not None else "missing"
        if market.venue == "BACKPACK":
            fee = exact_decimal(Fraction(Decimal(market.backpack_economics["taker_fee"])) * 10000)
            source = "configured_backpack_economics: " + market.backpack_economics["source_reference"]
    minima = {}
    for name in ("min_quantity", "min_notional"):
        raw = getattr(instrument, name, None)
        value = instrument_decimal(raw)
        if raw is not None and (value is None or value < 0):
            raise ScanConfigError(f"instrument {name} is invalid")
        minima[name] = value
    minimum = minima["min_notional"]
    return MarketMetadata(market.instrument_id, step, multiplier, fee, source or "missing",
                          minima["min_quantity"], None if minimum is None else
                          exact_decimal(Fraction(minimum) * Fraction(market.quote_to_usd)))


def normalized_levels(market, levels):
    """Map native base prices without cancelling the native contract exposure."""
    if market.quote_to_usd == market.canonical_multiplier:
        return tuple(levels)
    factor = Fraction(market.quote_to_usd) / Fraction(market.canonical_multiplier)
    return tuple((exact_decimal(Fraction(price) * factor), size) for price, size in levels)


class ScanState:
    """One current snapshot per market; one small current display row per direction."""

    def __init__(self, plan: ScanPlan):
        self.plan = plan
        self.markets = {m.instrument_id: m for m in plan.markets}
        self.groups = {}
        for market in plan.markets:
            self.groups.setdefault(market.symbol, []).append(market.instrument_id)
        self.metadata: dict[str, MarketMetadata] = {}
        self.books: dict[str, BookSnapshot] = {}
        self.raw_books: dict[str, dict] = {}
        self.current: dict[tuple, dict] = {}
        self.recorder = EventRecorder(plan.output_path, plan.cooldown_ms, plan.edge_change_bps,
                                     plan.quantity_change_fraction, plan.max_interval_ms)
        self.saved = 0

    def update_metadata(self, instrument_id: str, metadata: MarketMetadata, now_ns: int, *, evaluate=True):
        if instrument_id not in self.markets:
            raise ValueError("unconfigured instrument")
        self.metadata[instrument_id] = metadata
        if evaluate:
            self.evaluate_symbol(self.markets[instrument_id].symbol, now_ns, changed_id=instrument_id)

    def invalidate(self, instrument_id: str, now_ns: int, *, metadata=False):
        self.books.pop(instrument_id, None)
        self.raw_books.pop(instrument_id, None)
        if metadata:
            self.metadata.pop(instrument_id, None)
        self.evaluate_symbol(self.markets[instrument_id].symbol, now_ns, changed_id=instrument_id)

    def update_book(self, instrument_id: str, book: BookSnapshot, now_ns: int, *, raw_book=None, evaluate=True):
        market = self.markets.get(instrument_id)
        if market is None or book.symbol != market.symbol or book.venue != market.venue:
            raise ValueError("book does not match configured market")
        self.books[instrument_id] = book
        if raw_book is not None:
            self.raw_books[instrument_id] = deepcopy(raw_book)
        else:
            self.raw_books.pop(instrument_id, None)
        if evaluate:
            self.evaluate_symbol(market.symbol, now_ns, changed_id=instrument_id)

    def evaluate_symbol(self, symbol: str, now_ns: int, *, changed_id: str | None = None):
        for buy_id in self.groups[symbol]:
            for sell_id in self.groups[symbol]:
                if buy_id == sell_id:
                    continue
                if changed_id is not None and changed_id not in (buy_id, sell_id):
                    continue
                buy, sell = self.markets[buy_id], self.markets[sell_id]
                key = (symbol, buy.venue, sell.venue)
                result = None
                if all(i in self.books and i in self.metadata for i in (buy_id, sell_id)):
                    result = evaluate_opportunity(self.books[buy_id], self.metadata[buy_id],
                        self.books[sell_id], self.metadata[sell_id], self.plan.settings, now_ns)
                if result is None:
                    self.current.pop(key, None)
                else:
                    result["raw_buy_book"] = self.raw_books.get(buy_id)
                    result["raw_sell_book"] = self.raw_books.get(sell_id)
                    result["valuation"] = {
                        side: {"quote_to_usd": str(m.quote_to_usd), "source": m.valuation_source,
                               "price_unit": "USD per canonical base unit",
                               "book_quantity_unit": "native contract units",
                               "canonical_multiplier": str(m.canonical_multiplier),
                               "native_contract_multiplier": str(exact_decimal(
                                   Fraction(self.metadata[i].multiplier) / Fraction(m.canonical_multiplier))),
                               "raw_price_rule": "normalized_price * canonical_multiplier / quote_to_usd"}
                        for side, m, i in (("buy", buy, buy_id), ("sell", sell, sell_id))
                    }
                    self.current[key] = {"symbol": symbol, "buy_venue": buy.venue,
                        "sell_venue": sell.venue, "base_quantity": result["sizing"]["base_quantity"],
                        "entry_edge_bps": result["economics"]["entry_after_fees_and_reserve_bps"]}
                if self.recorder.observe(key, result, now_ns):
                    self.saved += 1

    def expire(self, now_ns: int):
        for symbol in self.groups:
            self.evaluate_symbol(symbol, now_ns)

    def prune(self, now_ns: int, blocked_symbols=()):
        """Remove superseded/stale display rows without discovering or recording."""
        for key in set(self.current) | set(self.recorder.active_keys()):
            symbol, buy_venue, sell_venue = key
            ids = [i for i in self.groups[symbol]
                   if self.markets[i].venue in {buy_venue, sell_venue}]
            usable = (len(ids) == 2 and all(i in self.books and i in self.metadata
                and _metadata_valid(self.metadata[i])
                and _book_valid(self.books[i], self.plan.settings, now_ns) for i in ids))
            if usable:
                usable = abs(self.books[ids[0]].ts_event_ns - self.books[ids[1]].ts_event_ns) <= self.plan.settings.max_skew_ms * 1_000_000
            if not usable:
                self.current.pop(key, None)
                self.recorder.observe(key, None, now_ns)
            elif symbol in blocked_symbols:
                self.current.pop(key, None)

    def display(self, stream=sys.stdout):
        rows = sorted(self.current.values(), key=lambda r: Decimal(r["entry_edge_bps"]), reverse=True)
        if stream.isatty():
            stream.write("\x1b[2J\x1b[H")
        stream.write(f"[scan] current opportunities={len(rows)} saved={self.saved} "
                     f"books={len(self.books)}/{len(self.markets)} history=0\n")
        for row in rows[:self.plan.top_n]:
            stream.write(f"  {row['symbol']} buy {row['buy_venue']} sell {row['sell_venue']} "
                         f"base={row['base_quantity']} entry_edge={Decimal(row['entry_edge_bps']):.3f} bps\n")
        stream.flush()


def data_clients(plan):
    """Reuse public factories; constructing this list never registers execution."""
    from spread_watch import (
        _aster_client, _hyperliquid_client, _lighter_client, _lighter_rh_client, _ondo_client,
    )
    factories = {"HYPERLIQUID": _hyperliquid_client, "LIGHTER": _lighter_client,
                 "LIGHTER_ROBINHOOD": _lighter_rh_client, "ASTER": _aster_client,
                 "ONDO": _ondo_client}
    groups = {}
    for market in plan.markets:
        groups.setdefault(market.client_id, []).append(market)
    result = []
    for client_id, markets in groups.items():
        if client_id == "BACKPACK":
            from nautilus_trader.adapters.backpack import (
                BackpackDataClientConfig, BackpackDataClientFactory, BackpackInstrumentEconomics,
            )
            economics = {m.instrument_id.removesuffix(".BACKPACK"):
                BackpackInstrumentEconomics(**m.backpack_economics) for m in markets}
            # Native public-client bounds are independent of the node's startup budget.
            backpack_cap = min(plan.max_levels_per_side, 10_000)
            snapshot_depth = max(depth for depth in (5, 10, 20, 50, 100, 500, 1000)
                                 if depth <= backpack_cap)
            config = BackpackDataClientConfig(list(economics), economics,
                max_levels_per_side=backpack_cap,
                depth_snapshot_limit=snapshot_depth,
                quote_stale_after_ms=min(plan.settings.max_age_ms, 30_000),
                http_timeout_secs=min(plan.connection_timeout_secs, 60),
                shutdown_timeout_secs=10)
            factory = BackpackDataClientFactory()
        else:
            factory, config = factories[client_id]([m.instrument_id for m in markets])
        result.append((client_id, factory, config))
    return result


def create_observer(plan, state, stop, client_configs):
    from nautilus_trader.common import DataActor, DataActorConfig
    from nautilus_trader.model import BookType, ClientId, InstrumentId, OrderBook, RecordFlag

    class Observer(DataActor):
        def __init__(self):
            super().__init__(DataActorConfig(log_events=False, log_commands=False))
            self.markets = {InstrumentId.from_str(m.instrument_id): m for m in plan.markets}
            self.local_books = {i: OrderBook(i, BookType.L2_MBP) for i in self.markets}
            self.level_prices = {i: {"BUY": set(), "SELL": set()} for i in self.markets}
            self.snapshot_seen = set()
            self.pending_snapshot = set()
            self.halted = set()
            self.metadata_stale = set()
            self.disconnected = set()
            self.backpack_unhealthy = set()
            self.epochs = {}
            self.failure = None
            self.started = False
            self.running = False
            self.subscribed = set()
            self.pending_aster = deque()
            self.next_aster_ns = 0
            self.dirty = {}
            self.last_display_ns = 0
            self.last_evaluation_ns = 0
            self.pending_symbols = set()

        def fail(self, message):
            self.failure = message
            self.running = False
            state.current.clear()
            print(f"[scan] stopped: {message}", file=sys.stderr, flush=True)
            stop()

        def on_start(self):
            self.running = True
            try:
                for instrument_id, market in self.markets.items():
                    instrument = self.cache.instrument(instrument_id)
                    if instrument is None:
                        raise ScanConfigError(f"instrument unavailable: {market.instrument_id}")
                    state.update_metadata(market.instrument_id, market_metadata(market, instrument), time.time_ns())
                    if market.venue == "ASTER":
                        self.pending_aster.append(instrument_id)
                    else:
                        self.subscribe_market(instrument_id)
                self.advance_subscriptions(time.time_ns())
                self.last_display_ns = time.time_ns()
                self.last_evaluation_ns = self.last_display_ns
                tick_ms = min(plan.refresh_ms, plan.aster_subscription_interval_ms,
                              plan.evaluation_interval_ms or plan.refresh_ms)
                self.clock.set_timer("scan-display", timedelta(milliseconds=tick_ms))
                self.started = True
            except Exception as exc:
                self.fail(f"startup: {type(exc).__name__}: {exc}")

        def subscribe_market(self, instrument_id):
            market = self.markets[instrument_id]
            client = ClientId.from_str(market.client_id)
            self.subscribe_instrument(instrument_id, client_id=client)
            kwargs = {"depth": plan.aster_snapshot_depth} if market.venue == "ASTER" else {}
            self.subscribe_book_deltas(instrument_id, BookType.L2_MBP,
                                       client_id=client, managed=False, **kwargs)
            if market.venue == "ONDO":
                self.subscribe_instrument_status(instrument_id, client_id=client)
            self.subscribed.add(instrument_id)

        def advance_subscriptions(self, now_ns):
            if self.pending_aster and now_ns >= self.next_aster_ns:
                self.subscribe_market(self.pending_aster.popleft())
                self.next_aster_ns = now_ns + plan.aster_subscription_interval_ms * 1_000_000

        def on_stop(self):
            self.running = False
            self.pending_aster.clear()
            self.dirty.clear()
            self.pending_symbols.clear()
            state.current.clear()
            if self.started:
                self.clock.cancel_timer("scan-display")
            for instrument_id in self.subscribed:
                market = self.markets[instrument_id]
                client = ClientId.from_str(market.client_id)
                self.unsubscribe_book_deltas(instrument_id, client_id=client)
                self.unsubscribe_instrument(instrument_id, client_id=client)
                if market.venue == "ONDO":
                    self.unsubscribe_instrument_status(instrument_id, client_id=client)

        def invalidate(self, instrument_id, *, metadata=False):
            self.local_books[instrument_id].reset()
            for prices in self.level_prices[instrument_id].values():
                prices.clear()
            self.snapshot_seen.discard(instrument_id)
            self.pending_snapshot.discard(instrument_id)
            self.dirty.pop(instrument_id, None)
            state.invalidate(str(instrument_id), time.time_ns(), metadata=metadata)

        def check_backpack(self):
            config = client_configs.get("BACKPACK")
            if config is None:
                return
            health = json.loads(config.telemetry_snapshot_json())
            epoch = health.get("connection_epoch")
            for instrument_id, market in self.markets.items():
                if market.venue != "BACKPACK":
                    continue
                raw = market.instrument_id.removesuffix(".BACKPACK")
                healthy = (health.get("connected") is True and health.get("metadata_ready") is True
                           and health.get("books_continuous", {}).get(raw) is True
                           and health.get("books_fresh", {}).get(raw) is True)
                if not healthy or (instrument_id in self.epochs and self.epochs[instrument_id] != epoch):
                    self.invalidate(instrument_id)
                if healthy:
                    self.backpack_unhealthy.discard(instrument_id)
                else:
                    self.backpack_unhealthy.add(instrument_id)
                self.epochs[instrument_id] = epoch

        def on_book_deltas(self, batch):
            if not self.running or batch.instrument_id not in self.markets:
                return
            try:
                if not plan.evaluation_interval_ms:
                    self.check_backpack()
                instrument_id = batch.instrument_id
                market = self.markets[instrument_id]
                deltas = batch.deltas
                clear = any(d.action.name == "CLEAR" for d in deltas)
                snapshot_part = any(
                    RecordFlag.F_SNAPSHOT.matches(d.flags) for d in deltas)
                if clear or (snapshot_part and instrument_id not in self.pending_snapshot):
                    self.local_books[instrument_id].reset()
                    for prices in self.level_prices[instrument_id].values():
                        prices.clear()
                    self.snapshot_seen.discard(instrument_id)
                    self.pending_snapshot.add(instrument_id)
                    self.dirty.pop(instrument_id, None)
                    state.invalidate(market.instrument_id, time.time_ns())
                if instrument_id not in self.snapshot_seen and instrument_id not in self.pending_snapshot:
                    return
                book = self.local_books[instrument_id]
                # L2_MBP has one order per price. Track only current price keys
                # to enforce the bound without cloning full ladders every tick.
                sides = self.level_prices[instrument_id]
                for delta in deltas:
                    action = delta.action.name
                    if action == "CLEAR":
                        for prices in sides.values():
                            prices.clear()
                        continue
                    order = delta.order
                    prices = sides[order.side.name]
                    if action == "DELETE" or order.size.is_zero():
                        prices.discard(order.price.raw)
                    else:
                        prices.add(order.price.raw)
                    if len(prices) > plan.max_levels_per_side:
                        self.fail(f"current book exceeds max_levels_per_side: {market.instrument_id}")
                        return
                book.apply_deltas(batch)
                if not RecordFlag.F_LAST.matches(batch.flags):
                    self.dirty.pop(instrument_id, None)
                    state.invalidate(market.instrument_id, time.time_ns())
                    return
                if instrument_id in self.pending_snapshot:
                    self.pending_snapshot.discard(instrument_id)
                    self.snapshot_seen.add(instrument_id)
                stamp = (batch.ts_event, batch.ts_init, batch.sequence)
                if plan.evaluation_interval_ms:
                    self.dirty[instrument_id] = stamp
                else:
                    self.publish_book(instrument_id, stamp)
            except Exception as exc:
                self.fail(f"book processing: {type(exc).__name__}: {exc}")

        def publish_book(self, instrument_id, stamp, *, evaluate=True):
            market = self.markets[instrument_id]
            if market.instrument_id not in state.metadata:
                return
            book = self.local_books[instrument_id]
            raw_bids = tuple(book.bids_to_dict(plan.depth_levels).items())
            raw_asks = tuple(book.asks_to_dict(plan.depth_levels).items())
            ts_event, ts_received, sequence = stamp
            valid = instrument_id not in (self.halted | self.metadata_stale | self.disconnected | self.backpack_unhealthy)
            snapshot = BookSnapshot(market.venue, market.symbol,
                normalized_levels(market, raw_bids), normalized_levels(market, raw_asks),
                ts_event, ts_received, valid=valid, coverage_limit=plan.depth_levels)
            raw = None
            if plan.output_path is not None:
                def rows(levels):
                    result = []
                    for price, size in levels:
                        quantity = format(size, "f")
                        if "." in quantity:
                            quantity = quantity.rstrip("0").rstrip(".")
                        result.append([str(price), quantity])
                    return result
                raw = {"instrument_id": market.instrument_id,
                       "bids": rows(raw_bids), "asks": rows(raw_asks),
                       "ts_event_ns": ts_event, "ts_received_ns": ts_received,
                       "coverage_limit": plan.depth_levels, "complete_book_claimed": False,
                       "sequence": sequence}
            state.update_book(market.instrument_id, snapshot, time.time_ns(), raw_book=raw, evaluate=evaluate)

        def on_instrument(self, instrument):
            if not self.running or instrument.id not in self.markets:
                return
            try:
                self.check_backpack()
                market = self.markets[instrument.id]
                if instrument.id in self.metadata_stale:
                    return
                metadata = market_metadata(market, instrument)
                old = state.metadata.get(market.instrument_id)
                if old and (old.multiplier, old.size_increment) != (metadata.multiplier, metadata.size_increment):
                    self.invalidate(instrument.id)
                state.update_metadata(market.instrument_id, metadata, time.time_ns(),
                                      evaluate=not plan.evaluation_interval_ms)
                if plan.evaluation_interval_ms:
                    self.pending_symbols.add(market.symbol)
            except Exception as exc:
                self.fail(f"metadata: {type(exc).__name__}: {exc}")

        def on_instrument_status(self, status):
            if not self.running or status.instrument_id not in self.markets:
                return
            try:
                instrument_id = status.instrument_id
                reason = getattr(status, "reason", "") or ""
                if reason == "adapter:disconnected":
                    self.disconnected.add(instrument_id)
                    self.invalidate(instrument_id)
                elif reason == "adapter:snapshot_ready":
                    self.disconnected.discard(instrument_id)
                elif reason == "adapter:metadata_stale":
                    self.metadata_stale.add(instrument_id)
                    self.invalidate(instrument_id, metadata=True)
                elif reason == "adapter:metadata_ready":
                    self.metadata_stale.discard(instrument_id)
                    self.on_instrument(self.cache.instrument(instrument_id))
                elif not reason.startswith("adapter:"):
                    action = getattr(getattr(status, "action", None), "name", "")
                    if getattr(status, "is_trading", None) is False or action in {
                            "HALT", "PAUSE", "CLOSE", "SUSPEND", "NOT_AVAILABLE_FOR_TRADING"}:
                        self.halted.add(instrument_id)
                        state.invalidate(str(instrument_id), time.time_ns())
                    elif getattr(status, "is_trading", None) is True or action == "TRADING":
                        self.halted.discard(instrument_id)
                if plan.evaluation_interval_ms:
                    self.pending_symbols.add(self.markets[instrument_id].symbol)
                else:
                    state.expire(time.time_ns())
            except Exception as exc:
                self.fail(f"instrument status: {type(exc).__name__}: {exc}")

        def on_time_event(self, event):
            if not self.running:
                return
            try:
                self.check_backpack()
                now_ns = time.time_ns()
                self.advance_subscriptions(now_ns)
                if now_ns - self.last_evaluation_ns >= plan.evaluation_interval_ms * 1_000_000:
                    changed = set(self.pending_symbols)
                    self.pending_symbols.clear()
                    for instrument_id, stamp in tuple(self.dirty.items()):
                        self.dirty.pop(instrument_id, None)
                        self.publish_book(instrument_id, stamp, evaluate=False)
                        changed.add(self.markets[instrument_id].symbol)
                    # Every changed leg is current before evaluating any pair.
                    for symbol in changed:
                        state.evaluate_symbol(symbol, time.time_ns())
                    self.last_evaluation_ns = time.time_ns()
                if time.time_ns() - self.last_display_ns >= plan.refresh_ms * 1_000_000:
                    if plan.evaluation_interval_ms:
                        blocked = self.pending_symbols | {self.markets[i].symbol for i in self.dirty}
                        state.prune(time.time_ns(), blocked)
                    else:
                        state.expire(time.time_ns())
                    state.display()
                    self.last_display_ns = time.time_ns()
            except Exception as exc:
                self.fail(f"refresh/recording: {type(exc).__name__}: {exc}")

    return Observer()


def build_node(plan):
    from nautilus_trader.common import CacheConfig, Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveDataEngineConfig, LiveNode
    from nautilus_trader.model import TraderId
    state = ScanState(plan)
    builder = (LiveNode.builder("OPPORTUNITY-SCAN", TraderId.from_str("SCAN-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.WARNING, fileout_level=LogLevel.OFF,
                                   print_config=False))
        .with_cache_config(CacheConfig(tick_capacity=1, bar_capacity=1, save_market_data=False,
                                       persist_account_events=False))
        .with_data_engine_config(LiveDataEngineConfig(emit_quotes_from_book=False,
                                                     emit_quotes_from_book_depths=False))
        .with_load_state(False).with_save_state(False)
        .with_timeout_connection(plan.connection_timeout_secs)
        .with_timeout_reconciliation(0).with_timeout_portfolio(0)
        .with_timeout_disconnection_secs(10).with_delay_post_stop_secs(0).with_delay_shutdown_secs(0))
    configs = {}
    for client_id, factory, config in data_clients(plan):
        builder = builder.add_data_client(client_id, factory, config)
        configs[client_id] = config
    node = builder.build()
    observer = create_observer(plan, state, node.handle().stop, configs)
    node.add_actor(observer)
    return node, observer, state


def run_native(plan):
    node, observer, state = build_node(plan)
    handle = node.handle()
    timer = threading.Timer(plan.duration_secs, handle.stop) if plan.duration_secs else None
    if timer:
        timer.daemon = True
        timer.start()
    try:
        node.run()
    except KeyboardInterrupt:
        handle.stop()
    finally:
        if timer:
            timer.cancel()
    print(f"[scan] stopped; books={len(state.books)}/{len(plan.markets)} opportunities saved={state.saved}")
    return 1 if observer.failure or not observer.started else 0

"""Bounded current-book state and a public-data-only Nautilus runtime."""
from __future__ import annotations

from opportunity_timing import (record_latency, sampled_delivery_age, sampled_start_latency,
                                span, start_latency, timed, diagnostics_enabled)

from copy import deepcopy
from collections import deque
from decimal import Decimal, InvalidOperation
from fractions import Fraction
from functools import lru_cache
import json
import math
import sys
import threading
import time
from dataclasses import replace
from datetime import timedelta

from opportunity_core import (BookSnapshot, EventRecorder, FastBbo, MarketMetadata, evaluate_opportunity,
                              exact_decimal, _book_valid, _metadata_valid)
from opportunity_scan import ScanConfigError, ScanPlan
from opportunity_book_diagnostics import BookEvidence
from opportunity_price_timing import QuoteTiming


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
    maker_fee = None
    if market.venue == "BACKPACK" and "maker_fee" in getattr(market, "backpack_economics", {}):
        maker_fee = exact_decimal(Fraction(Decimal(market.backpack_economics["maker_fee"])) * 10000)
    else:
        raw_maker = instrument_decimal(getattr(instrument, "maker_fee", None))
        if raw_maker is not None and (
                raw_maker != 0 or market.venue in {"LIGHTER", "LIGHTER_RH", "BACKPACK", "ONDO"}):
            maker_fee = exact_decimal(Fraction(raw_maker) * 10000)
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
                          exact_decimal(Fraction(minimum) * Fraction(market.quote_to_usd)),
                          maker_fee_bps=maker_fee)


@lru_cache(maxsize=1024)
def _normalization_factor(quote_to_usd, canonical_multiplier):
    return Fraction(quote_to_usd) / Fraction(canonical_multiplier)


@lru_cache(maxsize=65536)
def _normalized_price(price, factor):
    return exact_decimal(Fraction(price) * factor)


def normalized_levels(market, levels):
    """Map native base prices without cancelling the native contract exposure."""
    if market.quote_to_usd == market.canonical_multiplier:
        return tuple(levels)
    factor = _normalization_factor(market.quote_to_usd, market.canonical_multiplier)
    return tuple((_normalized_price(price, factor), size) for price, size in levels)


class ScanState:
    """One current snapshot per market; one small current display row per direction."""

    supports_lazy_books = False

    def __init__(self, plan: ScanPlan):
        self.plan = plan
        self.markets = {m.instrument_id: m for m in plan.markets}
        self.allowed_pairs = None if plan.pair_allowlist is None else {
            frozenset(pair) for pair in plan.pair_allowlist}
        self.groups = {}
        for market in plan.markets:
            self.groups.setdefault(market.symbol, []).append(market.instrument_id)
        self.metadata: dict[str, MarketMetadata] = {}
        self.books: dict[str, BookSnapshot] = {}
        self.fast_books: dict[str, FastBbo] = {}
        self._book_materializer = None
        self.book_evidence = ({i: BookEvidence() for i in self.markets}
                              if diagnostics_enabled() else None)
        self.quote_timings = {} if diagnostics_enabled() else None
        self.raw_books: dict[str, dict] = {}
        self.current: dict[tuple, dict] = {}
        self.recorder = EventRecorder(plan.output_path, plan.cooldown_ms, plan.edge_change_bps,
                                     plan.quantity_change_fraction, plan.max_interval_ms)
        self.saved = 0

    def attach_book_materializer(self, materializer):
        self._book_materializer = materializer

    def needs_full_book(self, instrument_id: str) -> bool:
        return True

    def ensure_book(self, instrument_id: str) -> BookSnapshot | None:
        book = self.books.get(instrument_id)
        if book is not None:
            return book
        fb = self.fast_books.get(instrument_id)
        if fb is None or fb.source is not None or self._book_materializer is None:
            return None
        return self._book_materializer(instrument_id, fb)

    def update_metadata(self, instrument_id: str, metadata: MarketMetadata, now_ns: int, *, evaluate=True):
        if instrument_id not in self.markets:
            raise ValueError("unconfigured instrument")
        self.metadata[instrument_id] = metadata
        if evaluate:
            self.evaluate_symbol(self.markets[instrument_id].symbol, now_ns, changed_id=instrument_id)

    def invalidate(self, instrument_id: str, now_ns: int, *, metadata=False, reason="invalidated"):
        if self.quote_timings is not None:
            self.quote_timings.pop(instrument_id, None)
        if self.book_evidence is not None:
            self.book_evidence[instrument_id].invalidate("metadata_invalid" if metadata else reason, now_ns)
        self.books.pop(instrument_id, None)
        self.fast_books.pop(instrument_id, None)
        self.raw_books.pop(instrument_id, None)
        if metadata:
            self.metadata.pop(instrument_id, None)
        self.evaluate_symbol(self.markets[instrument_id].symbol, now_ns, changed_id=instrument_id)

    def update_fast_bbo(self, instrument_id: str, fast_bbo: FastBbo, *, evaluate=True):
        market = self.markets.get(instrument_id)
        if market is None or fast_bbo.symbol != market.symbol or fast_bbo.venue != market.venue:
            raise ValueError("book does not match configured market")
        self.fast_books[instrument_id] = fast_bbo
        self.books.pop(instrument_id, None)
        self.raw_books.pop(instrument_id, None)
        if self.quote_timings is not None:
            self.quote_timings.pop(instrument_id, None)
        if self.book_evidence is not None:
            self.book_evidence[instrument_id].publish_fast(fast_bbo.valid, fast_bbo.published_ns)
        if evaluate:
            self.evaluate_symbol(market.symbol, fast_bbo.published_ns, changed_id=instrument_id)

    def update_book(self, instrument_id: str, book: BookSnapshot, now_ns: int, *, raw_book=None, evaluate=True):
        market = self.markets.get(instrument_id)
        if market is None or book.symbol != market.symbol or book.venue != market.venue:
            raise ValueError("book does not match configured market")
        self.books[instrument_id] = book
        if self.supports_lazy_books:
            try:
                if book.bids and book.asks and len(book.bids[0]) == 2 and len(book.asks[0]) == 2:
                    bid_f = float(book.bids[0][0])
                    ask_f = float(book.asks[0][0])
                    if math.isfinite(bid_f) and math.isfinite(ask_f) and 0.0 < bid_f < ask_f:
                        self.fast_books[instrument_id] = FastBbo(
                            book.venue, book.symbol, bid_f, ask_f,
                            book.ts_event_ns, book.ts_received_ns, book.valid,
                            now_ns, 0, -1, source=book,
                        )
                    else:
                        self.fast_books.pop(instrument_id, None)
                else:
                    self.fast_books.pop(instrument_id, None)
            except (TypeError, ValueError, IndexError):
                self.fast_books.pop(instrument_id, None)
        if self.quote_timings is not None:
            self.quote_timings.pop(instrument_id, None)
        if self.book_evidence is not None:
            self.book_evidence[instrument_id].publish(book, now_ns)
        if raw_book is not None:
            self.raw_books[instrument_id] = deepcopy(raw_book)
        else:
            self.raw_books.pop(instrument_id, None)
        if evaluate:
            self.evaluate_symbol(market.symbol, now_ns, changed_id=instrument_id)

    def pair_allowed(self, first: str, second: str) -> bool:
        return first != second and (self.allowed_pairs is None or
                                    frozenset((first, second)) in self.allowed_pairs)

    def pending_evaluation_symbols(self):
        return set()

    @timed("scan.evaluate_symbol")
    def evaluate_symbol(self, symbol: str, now_ns: int, *, changed_id: str | None = None,
                        changed_ids=None):
        affected = {changed_id} if changed_id is not None else changed_ids
        for buy_id in self.groups[symbol]:
            for sell_id in self.groups[symbol]:
                if not self.pair_allowed(buy_id, sell_id):
                    continue
                if affected is not None and buy_id not in affected and sell_id not in affected:
                    continue
                buy, sell = self.markets[buy_id], self.markets[sell_id]
                key = (symbol, buy.venue, sell.venue) if self.allowed_pairs is None else (symbol, buy_id, sell_id)
                result = None
                if all(i in self.books and i in self.metadata for i in (buy_id, sell_id)):
                    with span("scan.evaluate_pair"):
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
                        "sell_venue": sell.venue, "buy_id": buy_id, "sell_id": sell_id,
                        "base_quantity": result["sizing"]["base_quantity"],
                        "entry_edge_bps": result["economics"]["entry_after_fees_and_reserve_bps"]}
                if self.recorder.observe(key, result, now_ns):
                    self.saved += 1

    def expire(self, now_ns: int):
        for symbol in self.groups:
            self.evaluate_symbol(symbol, now_ns)

    def on_evaluation_timer(self, now_ns: int):
        """Optional state hook after all changed native books have been published."""

    def prune(self, now_ns: int, blocked_symbols=()):
        """Remove superseded/stale display rows without discovering or recording."""
        for key in set(self.current) | set(self.recorder.active_keys()):
            symbol, buy_leg, sell_leg = key
            ids = [buy_leg, sell_leg] if self.allowed_pairs is not None else [
                i for i in self.groups[symbol] if self.markets[i].venue in {buy_leg, sell_leg}]
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


@timed("startup.data_clients")
def data_clients(plan, *, backpack_quota=None):
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
            bp_kwargs = {
                "max_levels_per_side": backpack_cap,
                "depth_snapshot_limit": snapshot_depth,
                "quote_stale_after_ms": min(plan.settings.max_age_ms, 30_000),
                "http_timeout_secs": min(plan.connection_timeout_secs, 60),
                "shutdown_timeout_secs": 10,
                "quote_cache_only": plan.backpack_quote_cache_only,
                "depth_coalesce_ms": plan.backpack_depth_coalesce_ms,
            }
            max_buffer_frames = getattr(plan, "backpack_max_buffer_frames", 2048)
            if max_buffer_frames != 2048:
                bp_kwargs["max_buffer_frames"] = max_buffer_frames
            config = BackpackDataClientConfig(list(economics), economics, **bp_kwargs)
            factory = (BackpackDataClientFactory(quota=backpack_quota)
                       if backpack_quota is not None else BackpackDataClientFactory())
        else:
            kwargs = {"bbo_cache_only": True} if client_id == "HYPERLIQUID" and plan.hyperliquid_bbo_gate else {}
            if client_id == "HYPERLIQUID":
                candidates = getattr(plan, "hl_ws_select_candidates", 1)
                if candidates > 1:
                    kwargs["ws_select_candidates"] = candidates
                    kwargs["ws_select_probe_ms"] = getattr(plan, "hl_ws_select_probe_ms", 2500)
            factory, config = factories[client_id]([m.instrument_id for m in markets], **kwargs)
        result.append((client_id, factory, config))
    return result


class _BookSidePricesView:
    """Lazy compatibility view over one side of a native OrderBook."""

    __slots__ = ("_book", "_is_bid")

    def __init__(self, book, is_bid: bool):
        self._book = book
        self._is_bid = is_bid

    def __len__(self) -> int:
        return self._book.bid_count() if self._is_bid else self._book.ask_count()

    def __bool__(self) -> bool:
        return len(self) > 0

    def _price_set(self) -> set[int]:
        levels = self._book.bids() if self._is_bid else self._book.asks()
        return {level.price.raw for level in levels}

    def __iter__(self):
        return iter(self._price_set())

    def __contains__(self, item) -> bool:
        return item in self._price_set()

    def __eq__(self, other) -> bool:
        if isinstance(other, _BookSidePricesView):
            return self._price_set() == other._price_set()
        if isinstance(other, (set, frozenset)):
            return self._price_set() == other
        return NotImplemented

    def clear(self) -> None:
        pass


def create_observer(plan, state, stop, client_configs, *, drive_evaluation_timer=True, tape_recorder=None):
    from nautilus_trader.common import DataActor, DataActorConfig
    from nautilus_trader.model import (
        BookAction, BookType, ClientId, InstrumentId, OrderBook, OrderSide, RecordFlag,
    )

    class Observer(DataActor):
        def __init__(self):
            super().__init__(DataActorConfig(log_events=False, log_commands=False))
            self.markets = {InstrumentId.from_str(m.instrument_id): m for m in plan.markets}
            self.backpack_markets = tuple(
                (instrument_id, market, market.instrument_id.removesuffix(".BACKPACK"))
                for instrument_id, market in self.markets.items() if market.venue == "BACKPACK"
            )
            self.local_books = {i: OrderBook(i, BookType.L2_MBP) for i in self.markets}
            sample_book = next(iter(self.local_books.values()), None)
            self._native_book_counts = bool(
                sample_book is not None
                and callable(getattr(sample_book, "bid_count", None))
                and callable(getattr(sample_book, "ask_count", None))
            )
            if self._native_book_counts:
                self.level_prices = {
                    i: {
                        "BUY": _BookSidePricesView(book, True),
                        "SELL": _BookSidePricesView(book, False),
                    }
                    for i, book in self.local_books.items()
                }
            else:
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
            self.subscription_ids = {kind: set() for kind in ("instrument", "book_deltas", "quotes", "instrument_status")}
            self.timer_active = False
            self.pending_aster = deque()
            self.next_aster_ns = 0
            self.dirty = {}
            # At most one cycle and one publication marker per configured market.
            self.book_timings = {}
            self.pending_quote_timings = {}
            self.publication_timings = {}
            self.last_display_ns = 0
            self.last_evaluation_ns = 0
            self.last_backpack_check_ns = None
            self.pending_symbols = set()
            self.pending_instruments = set()
            from opportunity_memory import current_diagnostics
            self.memory_diagnostics = current_diagnostics()
            self._f_snapshot = RecordFlag.F_SNAPSHOT.value
            self._f_last = RecordFlag.F_LAST.value
            self._act_clear = BookAction.CLEAR
            self._act_delete = BookAction.DELETE
            self._side_buy = OrderSide.BUY
            self._side_sell = OrderSide.SELL
            self._max_levels = plan.max_levels_per_side
            self._str_ids = {i: m.instrument_id for i, m in self.markets.items()}
            self._native_ids = {m.instrument_id: i for i, m in self.markets.items()}
            self._norm_factors = {
                i: (
                    1.0
                    if m.quote_to_usd == m.canonical_multiplier
                    else float(_normalization_factor(m.quote_to_usd, m.canonical_multiplier))
                )
                for i, m in self.markets.items()
            }
            self._hl_config = client_configs.get("HYPERLIQUID") if client_configs else None
            self._hl_instrument_ids = tuple(
                i for i, m in self.markets.items() if m.client_id == "HYPERLIQUID"
            )
            self._hl_bbo_overlay_enabled = bool(
                plan.hyperliquid_bbo_gate
                and tape_recorder is None
                and plan.output_path is None
                and self._hl_config is not None
                and hasattr(self._hl_config, "latest_bbo")
                and self._hl_instrument_ids
            )
            self._hl_l2_stamps = {}
            self._hl_bbo_state = {}
            self._lazy_books = (
                bool(getattr(state, "supports_lazy_books", False))
                and tape_recorder is None
                and plan.output_path is None
            )
            if self._lazy_books:
                state.attach_book_materializer(self._materialize_book)
            if plan.hyperliquid_bbo_gate and hasattr(state, "bbo_provider"):
                hl_config = self._hl_config
                if hl_config is not None and hasattr(hl_config, "latest_bbo"):
                    hl_native_ids = {
                        m.instrument_id: InstrumentId.from_str(m.instrument_id)
                        for m in plan.markets if m.client_id == "HYPERLIQUID"
                    }

                    def _hl_bbo(instrument_id):
                        native_id = hl_native_ids.get(instrument_id)
                        if native_id is None:
                            return None
                        quote = hl_config.latest_bbo(native_id)
                        if quote is None:
                            return None
                        return (
                            Fraction(str(quote.bid_price)),
                            Fraction(str(quote.ask_price)),
                            int(quote.ts_event),
                            int(quote.ts_init),
                        )

                    state.bbo_provider = _hl_bbo
                    if hasattr(state, "bbo_l1_provider"):
                        def _hl_bbo_l1(instrument_id):
                            native_id = hl_native_ids.get(instrument_id)
                            if native_id is None:
                                return None
                            quote = hl_config.latest_bbo(native_id)
                            if quote is None:
                                return None
                            bid_sz = getattr(quote, "bid_size", None)
                            ask_sz = getattr(quote, "ask_size", None)
                            return (
                                Fraction(str(quote.bid_price)),
                                Fraction(str(quote.ask_price)),
                                None if bid_sz is None else Fraction(str(bid_sz)),
                                None if ask_sz is None else Fraction(str(ask_sz)),
                                int(quote.ts_event),
                                int(quote.ts_init),
                            )

                        state.bbo_l1_provider = _hl_bbo_l1

        def fail(self, message):
            self.failure = message
            self.running = False
            state.current.clear()
            if state.book_evidence is not None:
                now_ns = time.time_ns()
                for evidence in state.book_evidence.values():
                    evidence.invalidate("observer_failed", now_ns)
            print(f"[scan] stopped: {message}", file=sys.stderr, flush=True)
            stop()

        @timed("startup.market_observer")
        def on_start(self):
            if self.running:
                return
            if self.timer_active or any(self.subscription_ids.values()):
                self.on_stop()
                if self.timer_active or any(self.subscription_ids.values()):
                    self.fail("startup: previous subscriptions could not be cleaned up")
                    return
            self.failure = None
            self.next_aster_ns = 0
            self.running = True
            try:
                for instrument_id, market in self.markets.items():
                    instrument = self.cache.instrument(instrument_id)
                    if instrument is None:
                        raise ScanConfigError(f"instrument unavailable: {market.instrument_id}")
                    now_ns = time.time_ns()
                    metadata = market_metadata(market, instrument)
                    state.update_metadata(market.instrument_id, metadata, now_ns)
                    if tape_recorder is not None:
                        tape_recorder.record_instrument(
                            market, metadata, now_ns,
                            price_increment=instrument_decimal(getattr(instrument, "price_increment", None)),
                            source="instrument_metadata",
                        )
                    if market.venue == "ASTER":
                        self.pending_aster.append(instrument_id)
                    else:
                        self.subscribe_market(instrument_id)
                self.pending_aster = deque(sorted(self.pending_aster,
                    key=lambda i: (self.markets[i].symbol != "BTC", str(i))))
                self.advance_subscriptions(time.time_ns())
                self.last_display_ns = time.time_ns()
                self.last_evaluation_ns = self.last_display_ns
                tick_ms = min(plan.refresh_ms, plan.aster_subscription_interval_ms,
                              plan.evaluation_interval_ms or plan.refresh_ms)
                self.clock.set_timer("scan-display", timedelta(milliseconds=tick_ms))
                self.timer_active = True
                self.started = True
            except Exception as exc:
                self.on_stop()
                self.fail(f"startup: {type(exc).__name__}: {exc}")

        def subscribe_market(self, instrument_id):
            if instrument_id in self.subscribed:
                return
            market = self.markets[instrument_id]
            client = ClientId.from_str(market.client_id)
            self.subscribe_instrument(instrument_id, client_id=client)
            self.subscription_ids["instrument"].add(instrument_id)
            kwargs = {"depth": plan.aster_snapshot_depth} if market.venue == "ASTER" else {}
            if market.client_id == "HYPERLIQUID" and plan.hyperliquid_fast_book:
                kwargs = {"depth": min(plan.depth_levels, 5), "params": {"fast": True}}
            self.subscribe_book_deltas(instrument_id, BookType.L2_MBP,
                                       client_id=client, managed=False, **kwargs)
            self.subscription_ids["book_deltas"].add(instrument_id)
            if market.venue in {"BACKPACK", "ONDO"} or (
                    market.venue == "HL" and plan.hyperliquid_bbo_gate):
                self.subscribe_quotes(instrument_id, client_id=client)
                self.subscription_ids["quotes"].add(instrument_id)
            if market.venue == "ONDO":
                self.subscribe_instrument_status(instrument_id, client_id=client)
                self.subscription_ids["instrument_status"].add(instrument_id)
            self.subscribed.add(instrument_id)

        def advance_subscriptions(self, now_ns):
            if self.pending_aster and now_ns >= self.next_aster_ns:
                self.subscribe_market(self.pending_aster.popleft())
                self.next_aster_ns = now_ns + plan.aster_subscription_interval_ms * 1_000_000

        def _read_fresh_hl_bbo(self, instrument_id, l2_stamp, now_ns):
            quote = self._hl_config.latest_bbo(instrument_id)
            if quote is None:
                return None
            bbo_ts_event, bbo_ts_init = int(quote.ts_event), int(quote.ts_init)
            l2_ts_event, l2_ts_received, _ = l2_stamp
            if not (bbo_ts_event > l2_ts_event or (bbo_ts_event == l2_ts_event and bbo_ts_init > l2_ts_received)):
                return None
            prev = self._hl_bbo_state.get(instrument_id)
            if prev is not None and prev[0] == bbo_ts_event and prev[1] == bbo_ts_init:
                return (prev, False)
            if not 0 < bbo_ts_event <= bbo_ts_init <= now_ns:
                return None
            bid_sz_raw = getattr(quote, "bid_size", None)
            ask_sz_raw = getattr(quote, "ask_size", None)
            if bid_sz_raw is None or ask_sz_raw is None:
                return None
            try:
                bid_px = Decimal(str(quote.bid_price))
                ask_px = Decimal(str(quote.ask_price))
                bid_sz = Decimal(str(bid_sz_raw))
                ask_sz = Decimal(str(ask_sz_raw))
            except (InvalidOperation, ValueError, TypeError):
                return None
            if not (
                bid_px.is_finite() and ask_px.is_finite()
                and bid_sz.is_finite() and ask_sz.is_finite()
                and Decimal(0) < bid_px < ask_px and bid_sz > 0 and ask_sz > 0
            ):
                return None
            overlay = (bbo_ts_event, bbo_ts_init, bid_px, ask_px, bid_sz, ask_sz, now_ns)
            self._hl_bbo_state[instrument_id] = overlay
            return (overlay, True)

        def _build_hl_overlay_snapshot(self, instrument_id, book, overlay, valid):
            bbo_ts_event, bbo_ts_init, bid_px, ask_px, bid_sz, ask_sz, _ = overlay
            market = self.markets[instrument_id]
            depth = min(plan.depth_levels, 5) if plan.hyperliquid_fast_book else plan.depth_levels
            best_bid_fn = getattr(book, "best_bid_price", None)
            best_ask_fn = getattr(book, "best_ask_price", None)
            raw_bids = raw_asks = None
            same_l1_price = False
            if best_bid_fn is not None and best_ask_fn is not None:
                bp, ap = best_bid_fn(), best_ask_fn()
                if bp is not None and ap is not None:
                    same_l1_price = Decimal(str(bp)) == bid_px and Decimal(str(ap)) == ask_px
            else:
                raw_bids = tuple(book.bids_to_dict(depth).items())
                raw_asks = tuple(book.asks_to_dict(depth).items())
                same_l1_price = bool(
                    raw_bids and raw_asks and raw_bids[0][0] == bid_px and raw_asks[0][0] == ask_px
                )
            if same_l1_price:
                if raw_bids is None:
                    raw_bids = tuple(book.bids_to_dict(depth).items())
                    raw_asks = tuple(book.asks_to_dict(depth).items())
                if raw_bids and raw_asks and raw_bids[0][0] == bid_px and raw_asks[0][0] == ask_px:
                    bids = ((bid_px, bid_sz),) + raw_bids[1:]
                    asks = ((ask_px, ask_sz),) + raw_asks[1:]
                    cov_limit = depth
                else:
                    bids = ((bid_px, bid_sz),)
                    asks = ((ask_px, ask_sz),)
                    cov_limit = 1
            else:
                bids = ((bid_px, bid_sz),)
                asks = ((ask_px, ask_sz),)
                cov_limit = 1
            return BookSnapshot(
                market.venue, market.symbol,
                normalized_levels(market, bids), normalized_levels(market, asks),
                bbo_ts_event, bbo_ts_init, valid=valid, coverage_limit=cov_limit,
            )

        def _publish_hl_bbo_overlay(self, instrument_id, overlay):
            market = self.markets[instrument_id]
            inst_str = self._str_ids[instrument_id]
            if inst_str not in state.metadata:
                return
            book = self.local_books[instrument_id]
            valid = (
                instrument_id not in self.halted
                and instrument_id not in self.metadata_stale
                and instrument_id not in self.disconnected
                and instrument_id not in self.backpack_unhealthy
            )
            bbo_ts_event, bbo_ts_init, bid_px, ask_px, _, _, pub_ns = overlay
            update_count = getattr(book, "update_count", None)
            if (
                self._lazy_books
                and not state.needs_full_book(inst_str)
                and getattr(book, "best_bid_price", None) is not None
                and update_count is not None
            ):
                factor = self._norm_factors[instrument_id]
                bid_f = float(bid_px) if factor == 1.0 else float(bid_px) * factor
                ask_f = float(ask_px) if factor == 1.0 else float(ask_px) * factor
                if 0.0 < bid_f < ask_f and math.isfinite(ask_f):
                    fast_bbo = FastBbo(
                        market.venue, market.symbol, bid_f, ask_f,
                        bbo_ts_event, bbo_ts_init, valid, pub_ns,
                        0, update_count,
                    )
                    state.update_fast_bbo(inst_str, fast_bbo, evaluate=False)
                    return
            snapshot = self._build_hl_overlay_snapshot(instrument_id, book, overlay, valid)
            state.update_book(inst_str, snapshot, pub_ns, evaluate=False)

        def _sync_hl_bbo(self, changed, affected, now_ns):
            for instrument_id in self._hl_instrument_ids:
                if (
                    instrument_id not in self.snapshot_seen
                    or instrument_id in self.pending_snapshot
                    or instrument_id in self.dirty
                    or instrument_id in self.halted
                    or instrument_id in self.metadata_stale
                    or instrument_id in self.disconnected
                ):
                    continue
                inst_str = self._str_ids[instrument_id]
                if inst_str not in state.metadata:
                    continue
                l2_stamp = self._hl_l2_stamps.get(instrument_id)
                if l2_stamp is None:
                    continue
                result = self._read_fresh_hl_bbo(instrument_id, l2_stamp, now_ns)
                if result is None:
                    continue
                overlay, is_new = result
                if not is_new:
                    continue
                self._publish_hl_bbo_overlay(instrument_id, overlay)
                changed.add(self.markets[instrument_id].symbol)
                affected.add(inst_str)

        def on_stop(self):
            self.running = False
            self.pending_aster.clear()
            self.dirty.clear()
            self._hl_l2_stamps.clear()
            self._hl_bbo_state.clear()
            self.book_timings.clear()
            self.pending_quote_timings.clear()
            self.publication_timings.clear()
            self.pending_symbols.clear()
            self.pending_instruments.clear()
            state.current.clear()
            if self.timer_active:
                try:
                    self.clock.cancel_timer("scan-display")
                except Exception as exc:
                    self.failure = f"cancel timer: {type(exc).__name__}: {exc}"
                    print(f"[scan] {self.failure}", file=sys.stderr, flush=True)
                else:
                    self.timer_active = False
            # Track each successful request independently: a later subscription
            # can fail before the market is added to the completed set.
            for kind in ("quotes", "book_deltas", "instrument_status", "instrument"):
                ids = self.subscription_ids[kind]
                unsubscribe = getattr(self, "unsubscribe_" + kind)
                for instrument_id in tuple(ids):
                    client = ClientId.from_str(self.markets[instrument_id].client_id)
                    try:
                        unsubscribe(instrument_id, client_id=client)
                    except Exception as exc:
                        self.failure = f"unsubscribe {kind}: {type(exc).__name__}: {exc}"
                        print(f"[scan] {self.failure}", file=sys.stderr, flush=True)
                    else:
                        ids.discard(instrument_id)
            self.subscribed.clear()
            for instrument_id in self.markets:
                self.invalidate(instrument_id, notify=False, reason="stopped")
            self.epochs.clear()

        def invalidate(self, instrument_id, *, metadata=False, notify=True, reason="invalidated"):
            self.local_books[instrument_id].reset()
            for prices in self.level_prices[instrument_id].values():
                prices.clear()
            self.snapshot_seen.discard(instrument_id)
            self.pending_snapshot.discard(instrument_id)
            self.dirty.pop(instrument_id, None)
            self._hl_l2_stamps.pop(instrument_id, None)
            self._hl_bbo_state.pop(instrument_id, None)
            self.clear_book_timing(instrument_id)
            if notify:
                now_ns = time.time_ns()
                state.invalidate(str(instrument_id), now_ns, metadata=metadata, reason=reason)
                if tape_recorder is not None:
                    tape_recorder.record_invalidation(
                        self.markets[instrument_id], now_ns, reason=reason, metadata_invalid=metadata,
                    )
            else:
                # Stop must not evaluate partially cleared multi-venue books and
                # accidentally prepare a new pair while subscriptions are closing.
                inst_str = self._str_ids[instrument_id]
                state.books.pop(inst_str, None)
                if hasattr(state, "fast_books"):
                    state.fast_books.pop(inst_str, None)
                state.raw_books.pop(inst_str, None)
                if state.book_evidence is not None:
                    state.book_evidence[inst_str].invalidate(reason, time.time_ns())

        def _materialize_book(self, inst_str, fast_bbo):
            native_id = self._native_ids.get(inst_str)
            if (native_id is None or native_id not in self.snapshot_seen
                    or native_id in self.dirty or native_id in self.pending_snapshot):
                return None
            book = self.local_books[native_id]
            if getattr(book, "update_count", None) != fast_bbo.update_count:
                return None
            overlay = self._hl_bbo_state.get(native_id) if self._hl_bbo_overlay_enabled else None
            if (
                overlay is not None
                and overlay[0] == fast_bbo.ts_event_ns
                and overlay[1] == fast_bbo.ts_received_ns
            ):
                snapshot = self._build_hl_overlay_snapshot(native_id, book, overlay, fast_bbo.valid)
            else:
                market = self.markets[native_id]
                depth = plan.depth_levels
                if market.client_id == "HYPERLIQUID" and plan.hyperliquid_fast_book:
                    depth = min(depth, 5)
                raw_bids = tuple(book.bids_to_dict(depth).items())
                raw_asks = tuple(book.asks_to_dict(depth).items())
                snapshot = BookSnapshot(
                    market.venue, market.symbol,
                    normalized_levels(market, raw_bids), normalized_levels(market, raw_asks),
                    fast_bbo.ts_event_ns, fast_bbo.ts_received_ns,
                    valid=fast_bbo.valid, coverage_limit=depth,
                )
            state.books[inst_str] = snapshot
            state.fast_books[inst_str] = replace(fast_bbo, source=snapshot)
            if hasattr(state, "book_publications"):
                state.book_publications[inst_str] = (snapshot, fast_bbo.published_ns)
            if state.book_evidence is not None:
                state.book_evidence[inst_str].publish(snapshot, fast_bbo.published_ns)
            return snapshot

        def _begin_snapshot_rebuild(self, instrument_id, market):
            self.local_books[instrument_id].reset()
            for prices in self.level_prices[instrument_id].values():
                prices.clear()
            self.snapshot_seen.discard(instrument_id)
            self.pending_snapshot.add(instrument_id)
            self.dirty.pop(instrument_id, None)
            self.clear_book_timing(instrument_id)
            now_ns = time.time_ns()
            state.invalidate(market.instrument_id, now_ns, reason="snapshot_rebuilding")
            if tape_recorder is not None:
                tape_recorder.record_invalidation(market, now_ns, reason="snapshot_rebuilding")

        def clear_book_timing(self, instrument_id):
            self.pending_quote_timings.pop(instrument_id, None)
            self.book_timings.pop(instrument_id, None)
            market = self.markets[instrument_id]
            pending = self.publication_timings.get(market.symbol)
            if pending is not None:
                pending.pop(instrument_id, None)
                if not pending:
                    self.publication_timings.pop(market.symbol, None)

        def update_diagnostic_availability(self, instrument_id, backpack_reason=None):
            if state.book_evidence is not None:
                state.book_evidence[str(instrument_id)].unavailable_reason = (
                    "disconnected" if instrument_id in self.disconnected else
                    "metadata_invalid" if instrument_id in self.metadata_stale else
                    "halted" if instrument_id in self.halted else backpack_reason)

        def evaluate_published(self, symbol, *, changed_id=None, changed_ids=None):
            pending = self.publication_timings.pop(symbol, {})
            endpoint = start_latency() if pending else None
            for venue, marker in pending.values():
                record_latency(marker, "market.publish_to_evaluate", venue=venue, sample_every=127,
                               ended_ns=endpoint[1] if endpoint is not None else None)
            state.evaluate_symbol(symbol, time.time_ns(), changed_id=changed_id,
                                  changed_ids=changed_ids)

        @timed("market.backpack_health")
        def check_backpack(self):
            config = client_configs.get("BACKPACK")
            if config is None or not self.backpack_markets:
                return
            self.last_backpack_check_ns = time.monotonic_ns()
            health = json.loads(config.telemetry_snapshot_json())
            self.backpack_public_health = health
            epoch = health.get("connection_epoch")
            for instrument_id, _, raw in self.backpack_markets:
                healthy = (health.get("connected") is True and health.get("metadata_ready") is True
                           and health.get("books_continuous", {}).get(raw) is True
                           and health.get("books_fresh", {}).get(raw) is True)
                if not healthy or (instrument_id in self.epochs and self.epochs[instrument_id] != epoch):
                    reason = ("disconnected" if health.get("connected") is not True else
                              "metadata_invalid" if health.get("metadata_ready") is not True else
                              "book_discontinuous" if health.get("books_continuous", {}).get(raw) is not True else
                              "stale" if health.get("books_fresh", {}).get(raw) is not True else
                              "connection_changed")
                    self.invalidate(instrument_id, reason=reason)
                if healthy:
                    self.backpack_unhealthy.discard(instrument_id)
                else:
                    self.backpack_unhealthy.add(instrument_id)
                if state.book_evidence is not None:
                    self.update_diagnostic_availability(instrument_id, None if healthy else reason)
                self.epochs[instrument_id] = epoch

        def maybe_check_backpack(self):
            if not self.backpack_markets:
                return
            if self.last_backpack_check_ns is None or time.monotonic_ns() - self.last_backpack_check_ns >= 100_000_000:
                self.check_backpack()

        @timed("market.book_deltas", labels=lambda self, batch: (self.markets[batch.instrument_id].venue, ""))
        def on_book_deltas(self, batch):
            callback_ns = time.perf_counter_ns() if state.quote_timings is not None else None
            if not self.running or batch.instrument_id not in self.markets:
                return
            sampled_delivery_age(batch.ts_init, venue=self.markets[batch.instrument_id].venue)
            try:
                if not plan.evaluation_interval_ms:
                    self.maybe_check_backpack()
                instrument_id = batch.instrument_id
                market = self.markets[instrument_id]
                book = self.local_books[instrument_id]
                bid_count_fn = getattr(book, "bid_count", None) if self._native_book_counts else None
                ask_count_fn = getattr(book, "ask_count", None) if self._native_book_counts else None
                if bid_count_fn is not None and ask_count_fn is not None:
                    summary_fn = getattr(batch, "batch_summary", None)
                    if summary_fn is not None:
                        delta_count, has_clear, has_snapshot = summary_fn()
                    else:
                        deltas = batch.deltas
                        delta_count = len(deltas)
                        act_clear = self._act_clear
                        f_snapshot = self._f_snapshot
                        has_clear = any(d.action == act_clear for d in deltas)
                        has_snapshot = any(d.flags & f_snapshot for d in deltas)
                    if self.memory_diagnostics is not None:
                        self.memory_diagnostics.record_batch(market.venue, delta_count, batch.ts_init)
                    in_pending = instrument_id in self.pending_snapshot
                    in_seen = instrument_id in self.snapshot_seen
                    if not in_seen and not in_pending:
                        if has_clear or has_snapshot:
                            self._begin_snapshot_rebuild(instrument_id, market)
                        else:
                            return
                    elif has_clear or (has_snapshot and not in_pending):
                        self._begin_snapshot_rebuild(instrument_id, market)
                    book.apply_deltas(batch)
                    bid_count = bid_count_fn()
                    ask_count = ask_count_fn()
                    if bid_count > self._max_levels or ask_count > self._max_levels:
                        self.fail(f"current book exceeds max_levels_per_side: {market.instrument_id}")
                        return
                else:
                    deltas = batch.deltas
                    if self.memory_diagnostics is not None:
                        self.memory_diagnostics.record_batch(market.venue, len(deltas), batch.ts_init)
                    sides = self.level_prices[instrument_id]
                    if isinstance(sides["BUY"], _BookSidePricesView):
                        sides = {"BUY": set(sides["BUY"]), "SELL": set(sides["SELL"])}
                        self.level_prices[instrument_id] = sides
                    act_clear = self._act_clear
                    act_delete = self._act_delete
                    side_buy = self._side_buy
                    side_sell = self._side_sell
                    f_snapshot = self._f_snapshot
                    max_levels = self._max_levels
                    if len(deltas) == 1:
                        delta = deltas[0]
                        act = delta.action
                        is_clear = act == act_clear
                        if is_clear or ((delta.flags & f_snapshot) and instrument_id not in self.pending_snapshot):
                            self._begin_snapshot_rebuild(instrument_id, market)
                        elif instrument_id not in self.snapshot_seen and instrument_id not in self.pending_snapshot:
                            return
                        if not is_clear:
                            order = delta.order
                            side = order.side
                            if side == side_buy:
                                prices = sides["BUY"]
                            elif side == side_sell:
                                prices = sides["SELL"]
                            else:
                                raise KeyError(side.name)
                            if act == act_delete or order.size.raw == 0:
                                prices.discard(order.price.raw)
                            else:
                                prices.add(order.price.raw)
                                if len(prices) > max_levels:
                                    self.fail(f"current book exceeds max_levels_per_side: {market.instrument_id}")
                                    return
                    else:
                        in_pending = instrument_id in self.pending_snapshot
                        in_seen = instrument_id in self.snapshot_seen
                        rebuilt = False
                        if not in_seen and not in_pending:
                            if any(d.action == act_clear or (d.flags & f_snapshot) for d in deltas):
                                self._begin_snapshot_rebuild(instrument_id, market)
                                rebuilt = True
                            else:
                                return
                        elif deltas and (
                            deltas[0].action == act_clear
                            or (not in_pending and (deltas[0].flags & f_snapshot))
                        ):
                            self._begin_snapshot_rebuild(instrument_id, market)
                            rebuilt = True
                        buys, sells = sides["BUY"], sides["SELL"]
                        idx = 0
                        total = len(deltas)
                        while idx < total:
                            delta = deltas[idx]
                            act = delta.action
                            if not rebuilt and (act == act_clear or (not in_pending and (delta.flags & f_snapshot))):
                                self._begin_snapshot_rebuild(instrument_id, market)
                                rebuilt = True
                                idx = 0
                                continue
                            idx += 1
                            if act == act_clear:
                                buys.clear()
                                sells.clear()
                                continue
                            order = delta.order
                            side = order.side
                            if side == side_buy:
                                prices = buys
                            elif side == side_sell:
                                prices = sells
                            else:
                                raise KeyError(side.name)
                            if act == act_delete or order.size.raw == 0:
                                prices.discard(order.price.raw)
                            else:
                                prices.add(order.price.raw)
                                if len(prices) > max_levels:
                                    if not rebuilt and any(
                                        d.action == act_clear or (not in_pending and (d.flags & f_snapshot))
                                        for d in deltas[idx:]
                                    ):
                                        self._begin_snapshot_rebuild(instrument_id, market)
                                        rebuilt = True
                                        idx = 0
                                        continue
                                    self.fail(f"current book exceeds max_levels_per_side: {market.instrument_id}")
                                    return
                    book.apply_deltas(batch)
                    bid_count = len(sides["BUY"])
                    ask_count = len(sides["SELL"])
                if not (batch.flags & self._f_last):
                    self.dirty.pop(instrument_id, None)
                    self._hl_l2_stamps.pop(instrument_id, None)
                    self._hl_bbo_state.pop(instrument_id, None)
                    self.clear_book_timing(instrument_id)
                    now_ns = time.time_ns()
                    inv_reason = ("snapshot_rebuilding" if instrument_id in self.pending_snapshot
                                  else "partial_update")
                    state.invalidate(market.instrument_id, now_ns, reason=inv_reason)
                    if tape_recorder is not None:
                        tape_recorder.record_invalidation(market, now_ns, reason=inv_reason)
                    return
                if instrument_id in self.pending_snapshot:
                    self.pending_snapshot.discard(instrument_id)
                    self.snapshot_seen.add(instrument_id)
                stamp = (batch.ts_event, batch.ts_init, batch.sequence)
                if self._hl_bbo_overlay_enabled and market.client_id == "HYPERLIQUID":
                    self._hl_l2_stamps[instrument_id] = stamp
                if callback_ns is not None:
                    self.pending_quote_timings[instrument_id] = QuoteTiming(
                        *stamp, callback_ns, time.perf_counter_ns())
                if instrument_id not in self.dirty:
                    marker = sampled_start_latency("market.publication_cycle", venue=market.venue)
                    if marker is not None:
                        self.book_timings[instrument_id] = (marker, marker)
                elif instrument_id in self.book_timings:
                    first, _ = self.book_timings[instrument_id]
                    self.book_timings[instrument_id] = (first, start_latency())
                if plan.evaluation_interval_ms:
                    self.dirty[instrument_id] = stamp
                    if state.book_evidence is not None:
                        # F_LAST only: metadata describes a complete local batch,
                        # never a ladder copy or a substitute admission snapshot.
                        state.book_evidence[market.instrument_id].pending = (
                            *stamp, time.time_ns(), bid_count, ask_count)
                else:
                    self.publish_book(instrument_id, stamp)
            except Exception as exc:
                self.fail(f"book processing: {type(exc).__name__}: {exc}")

        @timed("market.publish_book", labels=lambda self, instrument_id, *a, **kw: (self.markets[instrument_id].venue, ""))
        def publish_book(self, instrument_id, stamp, *, evaluate=True):
            market = self.markets[instrument_id]
            markers = self.book_timings.pop(instrument_id, None)
            inst_str = self._str_ids[instrument_id]
            if inst_str not in state.metadata:
                return
            book = self.local_books[instrument_id]
            depth = plan.depth_levels
            if market.client_id == "HYPERLIQUID" and plan.hyperliquid_fast_book:
                bid_count_fn = getattr(book, "bid_count", None) if self._native_book_counts else None
                ask_count_fn = getattr(book, "ask_count", None) if self._native_book_counts else None
                if bid_count_fn is not None and ask_count_fn is not None:
                    exceeds = bid_count_fn() > 5 or ask_count_fn() > 5
                else:
                    sides = self.level_prices[instrument_id]
                    exceeds = len(sides["BUY"]) > 5 or len(sides["SELL"]) > 5
                if exceeds:
                    raise ScanConfigError("Hyperliquid fast subscription delivered more than five levels")
                depth = min(depth, 5)
            ts_event, ts_received, sequence = stamp
            valid = (
                instrument_id not in self.halted
                and instrument_id not in self.metadata_stale
                and instrument_id not in self.disconnected
                and instrument_id not in self.backpack_unhealthy
            )
            if self._hl_bbo_overlay_enabled and market.client_id == "HYPERLIQUID":
                self._hl_l2_stamps[instrument_id] = stamp
                result = self._read_fresh_hl_bbo(instrument_id, stamp, time.time_ns())
                if result is not None:
                    overlay, _ = result
                    self._publish_hl_bbo_overlay(instrument_id, overlay)
                    self.pending_quote_timings.pop(instrument_id, None)
                    if state.quote_timings is not None:
                        state.quote_timings.pop(inst_str, None)
                    if evaluate:
                        self.evaluate_published(market.symbol, changed_id=inst_str)
                    return
                self._hl_bbo_state.pop(instrument_id, None)
            lazy_published = False
            if self._lazy_books and not state.needs_full_book(inst_str):
                best_bid_fn = getattr(book, "best_bid_price", None)
                if best_bid_fn is not None:
                    bp = best_bid_fn()
                    ap = book.best_ask_price()
                    if bp is not None and ap is not None:
                        factor = self._norm_factors[instrument_id]
                        bid_f = bp.as_double() if factor == 1.0 else bp.as_double() * factor
                        ask_f = ap.as_double() if factor == 1.0 else ap.as_double() * factor
                        if 0.0 < bid_f < ask_f and math.isfinite(ask_f):
                            published_ns = time.time_ns()
                            fast_bbo = FastBbo(
                                market.venue, market.symbol, bid_f, ask_f,
                                ts_event, ts_received, valid, published_ns,
                                sequence, book.update_count,
                            )
                            state.update_fast_bbo(inst_str, fast_bbo, evaluate=False)
                            lazy_published = True
            if not lazy_published:
                raw_bids = tuple(book.bids_to_dict(depth).items())
                raw_asks = tuple(book.asks_to_dict(depth).items())
                snapshot = BookSnapshot(market.venue, market.symbol,
                    normalized_levels(market, raw_bids), normalized_levels(market, raw_asks),
                    ts_event, ts_received, valid=valid, coverage_limit=depth)
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
                    raw = {"instrument_id": inst_str,
                           "bids": rows(raw_bids), "asks": rows(raw_asks),
                           "ts_event_ns": ts_event, "ts_received_ns": ts_received,
                           "coverage_limit": depth, "complete_book_claimed": False,
                           "sequence": sequence}
                published_ns = time.time_ns()
                state.update_book(inst_str, snapshot, published_ns, raw_book=raw, evaluate=False)
                if tape_recorder is not None:
                    tape_recorder.record_book(market, snapshot, published_ns, sequence=sequence)
            if state.quote_timings is not None:
                quote_timing = self.pending_quote_timings.pop(instrument_id, None)
                if quote_timing is not None and (
                        quote_timing.source_wall_ns, quote_timing.native_init_wall_ns,
                        quote_timing.sequence) == stamp:
                    state.quote_timings[inst_str] = quote_timing.publish()
                else:
                    state.quote_timings.pop(inst_str, None)
            if markers is not None:
                published = start_latency()
                for label, marker in zip(("first", "latest"), markers):
                    record_latency(marker, "market." + label + "_update_to_publish",
                                   venue=market.venue, sample_every=127,
                                   ended_ns=published[1] if published is not None else None)
                self.publication_timings.setdefault(market.symbol, {})[instrument_id] = (
                    market.venue, published)
            if evaluate:
                self.evaluate_published(market.symbol, changed_id=inst_str)

        @timed("market.instrument")
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
                    self.invalidate(instrument.id, reason="metadata_invalid")
                now_ns = time.time_ns()
                state.update_metadata(market.instrument_id, metadata, now_ns,
                                      evaluate=not plan.evaluation_interval_ms)
                if tape_recorder is not None:
                    tape_recorder.record_instrument(
                        market, metadata, now_ns,
                        price_increment=instrument_decimal(getattr(instrument, "price_increment", None)),
                        source="instrument_update",
                    )
                if plan.evaluation_interval_ms:
                    self.pending_symbols.add(market.symbol)
                    self.pending_instruments.add(market.instrument_id)
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
                    self.invalidate(instrument_id, reason="disconnected")
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
                        now_ns = time.time_ns()
                        state.invalidate(str(instrument_id), now_ns, reason="halted")
                        if tape_recorder is not None:
                            tape_recorder.record_invalidation(
                                self.markets[instrument_id], now_ns, reason="halted",
                            )
                    elif getattr(status, "is_trading", None) is True or action == "TRADING":
                        self.halted.discard(instrument_id)
                self.update_diagnostic_availability(instrument_id)
                if plan.evaluation_interval_ms:
                    self.pending_symbols.add(self.markets[instrument_id].symbol)
                    self.pending_instruments.add(str(instrument_id))
                else:
                    state.expire(time.time_ns())
            except Exception as exc:
                self.fail(f"instrument status: {type(exc).__name__}: {exc}")

        @timed("market.timer")
        def on_time_event(self, event):
            if not self.running:
                return
            try:
                if self.memory_diagnostics is not None:
                    self.memory_diagnostics.record_timer(event)
                if plan.evaluation_interval_ms:
                    self.maybe_check_backpack()
                else:
                    self.check_backpack()
                now_ns = time.time_ns()
                self.advance_subscriptions(now_ns)
                interval_ns = plan.evaluation_interval_ms * 1_000_000
                elapsed_ns = now_ns - self.last_evaluation_ns
                if elapsed_ns >= interval_ns:
                    # Keep the original cadence across processing time and timer jitter.
                    # A late callback publishes once, without replaying missed intervals.
                    self.last_evaluation_ns = (self.last_evaluation_ns +
                        (elapsed_ns // interval_ns) * interval_ns) if interval_ns else now_ns
                    changed = set(self.pending_symbols)
                    affected = self.pending_instruments.copy()
                    self.pending_symbols.clear()
                    self.pending_instruments.clear()
                    if self.dirty:
                        dirty_items = tuple(self.dirty.items())
                        self.dirty.clear()
                        for instrument_id, stamp in dirty_items:
                            self.publish_book(instrument_id, stamp, evaluate=False)
                            changed.add(self.markets[instrument_id].symbol)
                            affected.add(self._str_ids[instrument_id])
                    if self._hl_bbo_overlay_enabled:
                        self._sync_hl_bbo(changed, affected, now_ns)
                    # Every changed leg is current before evaluating any pair.
                    changed.update(state.pending_evaluation_symbols())
                    if changed:
                        for symbol in sorted(changed):
                            self.evaluate_published(symbol, changed_ids=affected)
                    if drive_evaluation_timer:
                        state.on_evaluation_timer(time.time_ns())
                if time.time_ns() - self.last_display_ns >= plan.refresh_ms * 1_000_000:
                    if plan.evaluation_interval_ms:
                        blocked = self.pending_symbols | {self.markets[i].symbol for i in self.dirty}
                        state.prune(time.time_ns(), blocked)
                    else:
                        state.expire(time.time_ns())
                    state.display()
                    self.last_display_ns = time.time_ns()
                if self.memory_diagnostics is not None:
                    self.memory_diagnostics.capture_books(self, state, time.time_ns())
            except Exception as exc:
                self.fail(f"refresh/recording: {type(exc).__name__}: {exc}")

    return Observer()


def build_node(plan, *, state_factory=ScanState, tape_recorder=None):
    from nautilus_trader.common import CacheConfig, Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveDataEngineConfig, LiveNode
    from nautilus_trader.model import TraderId
    state = state_factory(plan)
    builder = (LiveNode.builder("OPPORTUNITY-SCAN", TraderId.from_str("SCAN-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.WARNING, fileout_level=LogLevel.OFF,
                                   print_config=False))
        .with_cache_config(CacheConfig(tick_capacity=1, bar_capacity=1, save_market_data=False,
                                       persist_account_events=False))
        .with_data_engine_config(LiveDataEngineConfig(emit_quotes_from_book=False,
                                                     emit_quotes_from_book_depths=False,
                                                     coalesce_book_deltas=plan.coalesce_book_deltas))
        .with_load_state(False).with_save_state(False)
        .with_timeout_connection(plan.connection_timeout_secs)
        .with_timeout_reconciliation(0).with_timeout_portfolio(0)
        .with_timeout_disconnection_secs(10).with_delay_post_stop_secs(0).with_delay_shutdown_secs(0))
    configs = {}
    for client_id, factory, config in data_clients(plan):
        builder = builder.add_data_client(client_id, factory, config)
        configs[client_id] = config
    node = builder.build()
    observer = create_observer(plan, state, node.handle().stop, configs, tape_recorder=tape_recorder)
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

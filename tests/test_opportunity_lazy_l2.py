"""Tests for Stage 1 lazy L2 materialization, float BBO prescreen, and Scheme C hot paths."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from opportunity_basis import BasisFilterPolicy, RollingBasisTracker, RollingLegSpreadTracker
from opportunity_core import BookSnapshot, FastBbo, MarketMetadata, ScanSettings, _fast_bbo_valid
from opportunity_live import LiveTradeState
from opportunity_live_runner import LiveCoordinator, SessionStoreProxy, load_live_plan
import opportunity_memory as memory
from opportunity_paper import PaperTradeState
import opportunity_runtime as runtime
from opportunity_scan import MarketPlan, ScanPlan
import opportunity_timing as timing
from opportunity_trade import TradePolicy
from test_opportunity_live import MemoryStore, accounts, fill, state
from test_opportunity_live_runner import FakeGateway, configs
from test_opportunity_scan import NS, batch, native_observer
from test_opportunity_trade import NOW, SECOND, make_plan, make_state, set_book

MS = 1_000_000


class _BookSpy:
    def __init__(self, inner):
        self._inner = inner
        self.l2_calls = 0

    def bids_to_dict(self, depth=None):
        self.l2_calls += 1
        return self._inner.bids_to_dict(depth)

    def asks_to_dict(self, depth=None):
        self.l2_calls += 1
        return self._inner.asks_to_dict(depth)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _pepe_scan_plan(*, output_path=None, evaluation_interval_ms=0, hl_fast=False,
                    quote_to_usd="1", canonical_multiplier="1"):
    markets = (
        MarketPlan("PEPE", "HL", "1000PEPE-USD-PERP.HYPERLIQUID", D(quote_to_usd),
                   "synthetic", canonical_multiplier=D(canonical_multiplier)),
        MarketPlan("PEPE", "ASTER", "1000PEPEUSDT-PERP.ASTER", D(quote_to_usd),
                   "synthetic", canonical_multiplier=D(canonical_multiplier)),
    )
    return ScanPlan(
        markets, ScanSettings(reserve_bps=D(0)), output_path,
        depth_levels=5, max_levels_per_side=100, refresh_ms=1000, top_n=10,
        evaluation_interval_ms=evaluation_interval_ms, hyperliquid_fast_book=hl_fast,
    )


def _lazy_observer(tmp_path, monkeypatch, state_obj, *, scan_plan=None, output_path=None, tape_recorder=None,
                   evaluation_interval_ms=0, hl_fast=False, quote_to_usd="1", canonical_multiplier="1"):
    native = pytest.importorskip("nautilus_trader.model")
    if scan_plan is None:
        scan_plan = _pepe_scan_plan(
            output_path=output_path, evaluation_interval_ms=evaluation_interval_ms, hl_fast=hl_fast,
            quote_to_usd=quote_to_usd, canonical_multiplier=canonical_multiplier,
        )
    elif output_path is not None or evaluation_interval_ms:
        scan_plan = replace(scan_plan, output_path=output_path, evaluation_interval_ms=evaluation_interval_ms)
    ids = [native.InstrumentId.from_str(m.instrument_id) for m in scan_plan.markets]
    instruments = {
        i: SimpleNamespace(id=i, size_increment="0.01", multiplier="1",
                           min_quantity="0.01", min_notional="1 USD", taker_fee=D("0"))
        for i in ids
    }
    calls = []
    clock = [NS]
    monkeypatch.setattr(runtime.time, "time_ns", lambda: clock[0])
    template = runtime.create_observer(
        scan_plan, state_obj, lambda: calls.append("stop"), {}, tape_recorder=tape_recorder,
    )

    class UnderTest(type(template)):
        @property
        def cache(self):
            return SimpleNamespace(instrument=lambda i: instruments.get(i))

        @property
        def clock(self):
            return SimpleNamespace(set_timer=lambda *a, **k: calls.append("timer"),
                                   cancel_timer=lambda *a, **k: calls.append("cancel_timer"))

        def subscribe_instrument(self, *a, **k):
            pass

        def subscribe_book_deltas(self, *a, **k):
            pass

        def subscribe_quotes(self, *a, **k):
            pass

        def subscribe_instrument_status(self, *a, **k):
            pass

        def unsubscribe_instrument(self, *a, **k):
            pass

        def unsubscribe_book_deltas(self, *a, **k):
            pass

        def unsubscribe_quotes(self, *a, **k):
            pass

        def unsubscribe_instrument_status(self, *a, **k):
            pass

    observer = UnderTest()
    observer.on_start()
    assert observer.failure is None
    spies = {}
    for iid in ids:
        spy = _BookSpy(observer.local_books[iid])
        observer.local_books[iid] = spy
        spies[iid] = spy
    return scan_plan, observer, ids, spies, calls, clock


def _live_for_pepe(policy=None, *, scan_plan=None, basis_filter=BasisFilterPolicy(), phase="active"):
    if scan_plan is None:
        scan_plan = _pepe_scan_plan()
    live, events = state(scan_plan, policy=policy or TradePolicy(
        order_notional_usd=D("20"), entry_edge_bps=D("20"),
        max_rolling_leg_spread_bps=D("20"), rolling_leg_spread_min_samples=2,
    ), basis_filter=basis_filter, activate=(phase == "active"))
    return live, events


def _snap_at(iid, bid, ask, ts_ns, qty="1000.00"):
    from nautilus_trader.model import (
        BookAction, BookOrder, OrderBookDelta, OrderBookDeltas, OrderSide, Price, Quantity, RecordFlag,
    )
    flags = RecordFlag.F_SNAPSHOT.value
    return OrderBookDeltas(iid, [
        OrderBookDelta.clear(iid, 1, ts_ns, ts_ns),
        OrderBookDelta(iid, BookAction.UPDATE, BookOrder(OrderSide.BUY, Price.from_str(bid), Quantity.from_str(qty), 0), flags, 1, ts_ns, ts_ns),
        OrderBookDelta(iid, BookAction.UPDATE, BookOrder(OrderSide.SELL, Price.from_str(ask), Quantity.from_str(qty), 0), flags | RecordFlag.F_LAST.value, 1, ts_ns, ts_ns),
    ])


def test_lazy_l2_skips_bids_asks_to_dict_until_bbo_qualifies_and_matches_eager(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader.model")
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)

    plan_norm = _pepe_scan_plan(quote_to_usd="1.0001", canonical_multiplier="1000")
    live, _ = _live_for_pepe(
        scan_plan=plan_norm,
        basis_filter=BasisFilterPolicy(enabled=True, window_secs=60, warmup_secs=5, min_deviation_bps=D("5")),
    )
    _, observer, ids, spies, _, clock = _lazy_observer(tmp_path, monkeypatch, live, scan_plan=plan_norm)
    accounts(live, clock[0])
    for m in live.plan.markets:
        live.register_instrument(m.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(m.instrument_id, MarketMetadata(m.instrument_id, D("0.01"), multiplier=D(1000), taker_fee_bps=D(0), fee_source="synthetic"), clock[0], evaluate=False)

    # 1. Non-qualifying spread: L2 ladder dicts MUST NOT be copied, yet fast_books, basis & spread trackers update.
    observer.on_book_deltas(_snap_at(ids[0], "99.99", "100.00", clock[0]))
    observer.on_book_deltas(_snap_at(ids[1], "99.99", "100.00", clock[0]))
    assert spies[ids[0]].l2_calls == 0
    assert spies[ids[1]].l2_calls == 0
    assert not live.books
    assert set(live.fast_books) == {str(ids[0]), str(ids[1])}
    assert live.book_evidence[str(ids[0])].reason == "published"
    assert live.basis_tracker.summary()["tracked_pairs"] == 1
    assert live.spread_tracker.summary()["tracked_instruments"] == 2
    summary = memory.summarize_books(observer, live, clock[0])
    assert summary["current_snapshots"] == 2

    # Warm up basis + leg spread windows using lazy updates
    for _ in range(5):
        clock[0] += SECOND
        observer.on_book_deltas(_snap_at(ids[0], "99.99", "100.00", clock[0]))
        observer.on_book_deltas(_snap_at(ids[1], "99.99", "100.00", clock[0]))
    assert spies[ids[0]].l2_calls == 0 and spies[ids[1]].l2_calls == 0

    # 2. Qualifying BBO spread arrives -> materializes exact normalized L2 snapshots and prepares orders.
    clock[0] += SECOND
    accounts(live, clock[0])
    observer.on_book_deltas(_snap_at(ids[0], "99.99", "100.00", clock[0]))
    pub_ns = clock[0]
    observer.on_book_deltas(_snap_at(ids[1], "100.50", "100.51", clock[0]))

    assert spies[ids[0]].l2_calls == 2 and spies[ids[1]].l2_calls == 2
    assert set(live.books) == {str(ids[0]), str(ids[1])}
    assert live.book_publications[str(ids[1])] == (live.books[str(ids[1])], pub_ns)
    assert live.book_publications[str(ids[1])][0] is live.books[str(ids[1])]
    assert live.book_evidence[str(ids[1])].last_complete is live.books[str(ids[1])]
    # Normalization applied accurately: 100.50 * (1.0001 / 1000)
    assert live.books[str(ids[1])].bids[0][0] == D("100.50") * D("1.0001") / D("1000")
    assert len(live.outbox) == 2

    # 3. Compare materialized orders vs eager baseline
    eager_live, _ = _live_for_pepe(
        scan_plan=plan_norm,
        basis_filter=BasisFilterPolicy(enabled=True, window_secs=60, warmup_secs=5, min_deviation_bps=D("5")),
    )
    eager_live.supports_lazy_books = False
    _, eager_obs, e_ids, _, _, e_clock = _lazy_observer(tmp_path / "eager", monkeypatch, eager_live, scan_plan=plan_norm)
    accounts(eager_live, e_clock[0])
    for m in eager_live.plan.markets:
        eager_live.register_instrument(m.instrument_id, D("0.01"), D(1), "USD")
        eager_live.update_metadata(m.instrument_id, MarketMetadata(m.instrument_id, D("0.01"), multiplier=D(1000), taker_fee_bps=D(0), fee_source="synthetic"), e_clock[0], evaluate=False)
    for _ in range(5):
        e_clock[0] += SECOND
        eager_obs.on_book_deltas(_snap_at(e_ids[0], "99.99", "100.00", e_clock[0]))
        eager_obs.on_book_deltas(_snap_at(e_ids[1], "99.99", "100.00", e_clock[0]))
    e_clock[0] += SECOND
    accounts(eager_live, e_clock[0])
    eager_obs.on_book_deltas(_snap_at(e_ids[0], "99.99", "100.00", e_clock[0]))
    eager_obs.on_book_deltas(_snap_at(e_ids[1], "100.50", "100.51", e_clock[0]))
    assert [(i.instrument_id, i.side, i.quantity, i.price) for i in live.outbox] == [
        (i.instrument_id, i.side, i.quantity, i.price) for i in eager_live.outbox
    ]


def test_lazy_l2_works_with_paper_trade_state(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader.model")
    paper, _ = make_state(_pepe_scan_plan(),
                          policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")))
    _, observer, ids, spies, _, _ = _lazy_observer(tmp_path, monkeypatch, paper)
    for m in paper.plan.markets:
        paper.update_metadata(m.instrument_id, MarketMetadata(m.instrument_id, D("0.01"), taker_fee_bps=D(0), fee_source="synthetic"), NS, evaluate=False)

    observer.on_book_deltas(batch(ids[0], "99.99", "100.00"))
    observer.on_book_deltas(batch(ids[1], "99.99", "100.00"))
    assert spies[ids[0]].l2_calls == 0 and spies[ids[1]].l2_calls == 0
    assert not paper.positions

    observer.on_book_deltas(batch(ids[1], "100.50", "100.51"))
    assert spies[ids[0]].l2_calls == 2 and spies[ids[1]].l2_calls == 2
    assert len(paper.positions) == 1


@pytest.mark.parametrize("mode", ["owned", "tape", "output_path", "scan_state", "startup_phase"])
def test_eager_l2_preserved_when_required(tmp_path, monkeypatch, mode):
    pytest.importorskip("nautilus_trader.model")
    if mode == "scan_state":
        p, st, obs, ids, _, _ = native_observer(tmp_path, monkeypatch)
        spy = _BookSpy(obs.local_books[ids[0]])
        obs.local_books[ids[0]] = spy
        obs.on_book_deltas(batch(ids[0], "99.00", "100.00"))
        assert spy.l2_calls == 2 and str(ids[0]) in st.books
        return

    phase = "waiting_books" if mode == "startup_phase" else "active"
    live, _ = _live_for_pepe(phase=phase, policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")))
    tape = SimpleNamespace(record_instrument=lambda *a, **k: None, record_book=lambda *a, **k: None, record_invalidation=lambda *a, **k: None) if mode == "tape" else None
    out = tmp_path / "opp.jsonl" if mode == "output_path" else None
    _, observer, ids, spies, _, clock = _lazy_observer(tmp_path, monkeypatch, live, tape_recorder=tape, output_path=out)
    for m in live.plan.markets:
        live.register_instrument(m.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(m.instrument_id, MarketMetadata(m.instrument_id, D("0.01"), multiplier=D(1000), taker_fee_bps=D(0), fee_source="synthetic"), clock[0], evaluate=False)
    if mode == "owned":
        live.positions[("PEPE", "ASTER", "HL")] = {
            "buy_id": str(ids[0]), "sell_id": str(ids[1]),
            "remaining_buy_base": "0.1", "remaining_sell_base": "0.1",
            "entry_buy_base": "0.1", "entry_sell_base": "0.1",
            "actual_entry": {"buy_notional_usd": "10", "sell_notional_usd": "10"},
            "entry": {"buy_metadata": {"multiplier": "1"}, "sell_metadata": {"multiplier": "1"}},
            "reserved_gross_usd": "20", "entry_ids": [], "exit_ids": [], "rebalance_ids": [],
            "opened_ns": clock[0], "status": "open", "exit_reason": None, "exit_requested_ns": None,
            "closing_long_cash_usd": "0", "closing_short_cash_usd": "0", "closing_fees_usd": "0",
            "pending_commissions": {}, "unknown_commission": False,
        }
    observer.on_book_deltas(batch(ids[0], "99.99", "100.00"))
    assert spies[ids[0]].l2_calls == 2
    assert str(ids[0]) in live.books


def test_lazy_l2_crossed_native_book_falls_back_to_eager(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader.model")
    live, _ = _live_for_pepe(policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")))
    _, observer, ids, spies, _, clock = _lazy_observer(tmp_path, monkeypatch, live)
    for m in live.plan.markets:
        live.register_instrument(m.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(m.instrument_id, MarketMetadata(m.instrument_id, D("0.01"), multiplier=D(1000), taker_fee_bps=D(0), fee_source="synthetic"), clock[0], evaluate=False)
    # Crossed bid > ask triggers eager fallback (fast_bbo gate rejects bid >= ask)
    observer.on_book_deltas(batch(ids[0], "101.00", "100.00"))
    assert spies[ids[0]].l2_calls == 2
    assert str(ids[0]) in live.books and str(ids[0]) not in live.fast_books


def test_stale_ladder_protection_returns_none_when_mutated_or_dirty(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader.model")
    live, _ = _live_for_pepe(policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")))
    _, observer, ids, spies, _, clock = _lazy_observer(tmp_path, monkeypatch, live, evaluation_interval_ms=100)
    for m in live.plan.markets:
        live.register_instrument(m.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(m.instrument_id, MarketMetadata(m.instrument_id, D("0.01"), multiplier=D(1000), taker_fee_bps=D(0), fee_source="synthetic"), clock[0], evaluate=False)

    observer.on_book_deltas(batch(ids[0], "99.99", "100.00"))
    clock[0] += 100 * MS
    observer.on_time_event(None)
    assert str(ids[0]) in live.fast_books and str(ids[0]) not in live.books

    # New delta arrives while evaluation_interval_ms=100 holds it in dirty & increments update_count
    observer.on_book_deltas(batch(ids[0], "99.98", "99.99"))
    assert ids[0] in observer.dirty
    assert live.ensure_book(str(ids[0])) is None
    # Even if dirty were cleared manually, update_count mismatch prevents mixing old FastBbo with new ladder!
    saved_dirty = observer.dirty.pop(ids[0])
    assert live.ensure_book(str(ids[0])) is None
    observer.dirty[ids[0]] = saved_dirty


def test_float_bbo_prescreen_boundary_negative_edge_and_direct_mutations(monkeypatch):
    live, _ = state(make_plan(venues=("ASTER", "BACKPACK")),
                    policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")))
    live.update_native_admission({"BACKPACK": {"ready": True, "reason": None, "checked_ns": NOW}}, NOW)

    # Missing leg skips _entry cleanly
    entry_calls = []
    orig_entry = live._entry
    monkeypatch.setattr(live, "_entry", lambda b, s, n: (entry_calls.append((b, s)), orig_entry(b, s, n))[1])
    live.update_fast_bbo("BTC.ASTER", FastBbo("ASTER", "BTC", 99.99, 100.0, NOW, NOW, True, NOW), evaluate=False)
    live.evaluate_symbol("BTC", NOW)
    assert entry_calls == []

    # Boundary within 1e-8 falls through to exact Decimal/Fraction
    live.books["BTC.ASTER"] = BookSnapshot("ASTER", "BTC", ((D("99.99"), D("10")),), ((D("100.00"), D("10")),), NOW, NOW)
    live.books["BTC.BACKPACK"] = BookSnapshot("BACKPACK", "BTC", ((D("100.199999999"), D("10")),), ((D("100.21"), D("10")),), NOW, NOW)
    live.fast_books["BTC.ASTER"] = FastBbo("ASTER", "BTC", 99.99, 100.0, NOW, NOW, True, NOW, source=live.books["BTC.ASTER"])
    live.fast_books["BTC.BACKPACK"] = FastBbo("BACKPACK", "BTC", 100.199999999, 100.21, NOW, NOW, True, NOW, source=live.books["BTC.BACKPACK"])
    # 100.199999999 < 100.20 (within 1e-8) -> float shortcut passes to exact check, which rules it out
    assert live._bbo_ruled_out("BTC.ASTER", "BTC.BACKPACK") is True

    # Direct mutation of live.books[id] invalidates stale fast_books entry via source identity
    live.books["BTC.BACKPACK"] = BookSnapshot("BACKPACK", "BTC", ((D("100.40"), D("10")),), ((D("100.41"), D("10")),), NOW, NOW)
    assert live._bbo_ruled_out("BTC.ASTER", "BTC.BACKPACK") is False

    # Direct mutation of live.metadata[id] invalidates cached float ratio
    set_book(live, "ASTER", "99.99", "100.00", NOW)
    set_book(live, "BACKPACK", "100.25", "100.26", NOW)
    assert live._bbo_ruled_out("BTC.ASTER", "BTC.BACKPACK") is False
    live.metadata["BTC.BACKPACK"] = MarketMetadata("BTC.BACKPACK", D("0.01"), taker_fee_bps=D("10"), fee_source="s")
    assert live._bbo_ruled_out("BTC.ASTER", "BTC.BACKPACK") is True

    # Negative min_entry_edge_bps falls through to exact _best_quotes_rule_out (which returns False)
    neg_plan = ScanPlan(live.plan.markets, ScanSettings(min_entry_edge_bps=D("-5"), reserve_bps=D("0")), None, evaluation_interval_ms=0)
    neg_paper = PaperTradeState(neg_plan, TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")))
    neg_paper.plan = replace(neg_paper.plan, settings=ScanSettings(min_entry_edge_bps=D("-5"), reserve_bps=D("0")))
    neg_paper.policy = SimpleNamespace(
        **{k: getattr(neg_paper.policy, k) for k in TradePolicy.__dataclass_fields__},
        effective_entry_edge_bps=lambda b, s: D("-5"),
    )
    for m in neg_plan.markets:
        neg_paper.update_metadata(m.instrument_id, MarketMetadata(m.instrument_id, D("0.01"), taker_fee_bps=D("0"), fee_source="s"), NOW, evaluate=False)
    set_book(neg_paper, "ASTER", "99.99", "100.00", NOW)
    set_book(neg_paper, "BACKPACK", "99.99", "100.00", NOW)
    assert neg_paper._bbo_ruled_out("BTC.ASTER", "BTC.BACKPACK") is False


def test_rolling_leg_spread_tracker_per_symbol_gate_and_fast_books():
    tracker = RollingLegSpreadTracker(window_secs=60, min_samples=2, sample_interval_secs=2)
    settings = ScanSettings()
    insts = ["BTC.HL", "BTC.ASTER"]
    fast = {
        "BTC.HL": FastBbo("HL", "BTC", 100.0, 100.1, NOW, NOW),
        "BTC.ASTER": FastBbo("ASTER", "BTC", 100.0, 100.2, NOW, NOW),
    }
    assert tracker.observe_instruments(insts, {}, settings, NOW, fast_books=fast, symbol="BTC") == 2
    assert tracker._symbol_earliest_next_ns["BTC"] == NOW + 2 * SECOND
    # Repeated calls before +2s short-circuit immediately even with changed prices
    fast["BTC.HL"] = FastBbo("HL", "BTC", 100.0, 105.0, NOW + SECOND, NOW + SECOND)
    assert tracker.observe_instruments(insts, {}, settings, NOW + SECOND, fast_books=fast, symbol="BTC") == 0
    # At +2s gate opens and samples from fast_books
    assert tracker.observe_instruments(insts, {}, settings, NOW + 2 * SECOND, fast_books=fast, symbol="BTC") == 2
    assert tracker.median_bps("BTC.ASTER", NOW + 2 * SECOND) == pytest.approx(20.0)


def test_single_pass_on_book_deltas_handles_backpack_single_and_mid_batch_clear(tmp_path, monkeypatch):
    native = pytest.importorskip("nautilus_trader.model")
    p, st, observer, ids, calls, _ = native_observer(tmp_path, monkeypatch, depth_levels=2, max_levels_per_side=2)
    iid = ids[0]

    # Single-delta batch before snapshot is ignored
    single_inc = native.OrderBookDeltas(iid, [
        native.OrderBookDelta(iid, native.BookAction.UPDATE,
                              native.BookOrder(native.OrderSide.BUY, native.Price.from_str("99.00"), native.Quantity.from_str("1.0"), 0),
                              native.RecordFlag.F_LAST.value, 1, NS, NS),
    ])
    observer.on_book_deltas(single_inc)
    assert str(iid) not in st.books

    # Establish snapshot, then single-delta BUY update & zero-size DELETE
    observer.on_book_deltas(batch(iid, "99.00", "100.00"))
    observer.on_book_deltas(native.OrderBookDeltas(iid, [
        native.OrderBookDelta(iid, native.BookAction.UPDATE,
                              native.BookOrder(native.OrderSide.BUY, native.Price.from_str("98.00"), native.Quantity.from_str("2.0"), 0),
                              native.RecordFlag.F_LAST.value, 2, NS, NS),
    ]))
    assert len(observer.level_prices[iid]["BUY"]) == 2
    observer.on_book_deltas(native.OrderBookDeltas(iid, [
        native.OrderBookDelta(iid, native.BookAction.DELETE,
                              native.BookOrder(native.OrderSide.BUY, native.Price.from_str("98.00"), native.Quantity.from_str("0.0"), 0),
                              native.RecordFlag.F_LAST.value, 3, NS, NS),
    ]))
    assert len(observer.level_prices[iid]["BUY"]) == 1

    # Mid-batch CLEAR resets level_prices cleanly even if earlier deltas exceed max_levels_per_side
    mid_clear = native.OrderBookDeltas(iid, [
        native.OrderBookDelta(iid, native.BookAction.UPDATE,
                              native.BookOrder(native.OrderSide.BUY, native.Price.from_str("98.50"), native.Quantity.from_str("1.0"), 0),
                              0, 4, NS, NS),
        native.OrderBookDelta(iid, native.BookAction.UPDATE,
                              native.BookOrder(native.OrderSide.BUY, native.Price.from_str("97.00"), native.Quantity.from_str("1.0"), 0),
                              0, 4, NS, NS),
        native.OrderBookDelta.clear(iid, 4, NS, NS),
        native.OrderBookDelta(iid, native.BookAction.UPDATE,
                              native.BookOrder(native.OrderSide.BUY, native.Price.from_str("95.00"), native.Quantity.from_str("1.0"), 0),
                              native.RecordFlag.F_SNAPSHOT.value, 4, NS, NS),
        native.OrderBookDelta(iid, native.BookAction.UPDATE,
                              native.BookOrder(native.OrderSide.SELL, native.Price.from_str("96.00"), native.Quantity.from_str("1.0"), 0),
                              native.RecordFlag.F_SNAPSHOT.value | native.RecordFlag.F_LAST.value, 4, NS, NS),
    ])
    observer.on_book_deltas(mid_clear)
    assert observer.failure is None
    assert observer.level_prices[iid]["BUY"] == {native.Price.from_str("95.00").raw}
    assert st.books[str(iid)].bids[0][0] == D("95")


def test_coordinator_tick_skips_loss_monitor_and_document_when_not_due(tmp_path, monkeypatch):
    from opportunity_loss_monitor import LossMonitorPolicy
    plan = load_live_plan(configs(tmp_path, mandatory="false", all_five=True, venues='["HL","ASTER"]'))
    plan = replace(plan, loss_monitor=LossMonitorPolicy(enabled=True, max_loss_usd=D("20"), interval_secs=600))
    store = MemoryStore()
    proxy = SessionStoreProxy(store, restart_from_flat=True)
    live = LiveTradeState(plan.scan, plan.policy, max_pairs=plan.max_pairs,
                          max_gross_notional_usd=plan.max_gross_notional_usd, store=proxy,
                          aster_leverage_targets={m.instrument_id: 5 for m in plan.scan.markets if m.venue == "ASTER"})
    coordinator = LiveCoordinator(plan, live, proxy)
    coordinator.attach(FakeGateway())
    coordinator.phase = "active"
    coordinator.started_ns = NOW
    coordinator.loss_monitor.status = "ready"
    coordinator.loss_monitor.baseline_usd = "1000"
    coordinator.loss_monitor.baseline_ns = NOW
    coordinator.loss_monitor.next_check_ns = NOW + 600 * SECOND

    doc_calls = []
    orig_doc = live.document
    monkeypatch.setattr(live, "document", lambda: (doc_calls.append(True), orig_doc())[1])

    # Not due -> neither monitor.tick, _has_residual, nor live.document is called!
    tick_calls = []
    monkeypatch.setattr(coordinator.loss_monitor, "tick", lambda *a, **k: tick_calls.append(True))
    coordinator.tick(NOW + SECOND)
    assert not tick_calls and not doc_calls

    # When due -> _has_residual uses live.has_residual() without calling live.document()
    assert coordinator._has_residual() is False
    assert not doc_calls
    live.orders["direct"] = {"terminal_ns": None, "status": "unknown"}
    assert coordinator._has_residual() is True
    assert not doc_calls


def test_account_domain_ready_fast_path_flat_nonflat_mismatch_and_unowned(monkeypatch):
    from opportunity_preflight import AccountSnapshot
    symbols = tuple(f"S{i}" for i in range(160))
    live = LiveTradeState(make_plan(symbols=symbols, venues=("HL", "ASTER")), TradePolicy(),
                          max_pairs=2, max_gross_notional_usd=D(500))
    rows = tuple((i, D(0)) for i in live.account_scopes[("ASTER", "")]) + tuple(
        (f"OUTSIDE{i}.ASTER", D(0)) for i in range(474))
    rep = live._account_representative(live.account_scopes[("ASTER", "")])
    live.on_account(AccountSnapshot("ASTER", rep, None, None, NOW, D(0),
                                    account_positions=rows, account_open_orders=()), NOW)
    assert live._account_domain_ready("ASTER", NOW) is True
    assert live.account_check_metrics["ASTER"]["position_rows_checked"] == 634
    assert live.account_check_metrics["ASTER"]["unconfigured_position_rows"] == 474

    # Unexpected non-zero unconfigured position triggers position_mismatch
    bad_rows = rows[:-1] + (("OUTSIDE473.ASTER", D("1")),)
    live.on_account(AccountSnapshot("ASTER", rep, None, None, NOW + 1, D(0),
                                    account_positions=bad_rows, account_open_orders=()), NOW + 1)
    assert live._account_domain_ready("ASTER", NOW + 1) is False
    assert live.account_pauses["ASTER"]["reason"] == "position_mismatch"
    live.account_pauses.clear()

    # Non-flat owned position matches expected_native accurately
    live.positions[("S0", "ASTER", "HL")] = {
        "buy_id": "S0.ASTER", "sell_id": "S0.HL",
        "remaining_buy_base": "2", "remaining_sell_base": "2",
        "entry": {"buy_metadata": {"multiplier": "1"}, "sell_metadata": {"multiplier": "1"}},
        "entry_ids": [], "exit_ids": [], "rebalance_ids": [],
    }
    owned_rows = (("S0.ASTER", D(2)),) + rows[1:]
    live.on_account(AccountSnapshot("ASTER", rep, None, None, NOW + 2, D(2),
                                    account_positions=owned_rows, account_open_orders=()), NOW + 2)
    assert live._account_domain_ready("ASTER", NOW + 2) is True

    # Activity fence still blocks pre-fence observation
    live.fences["S0.ASTER"] = NOW + 5
    assert live._account_problem("S0.ASTER", NOW + 5) == "predates_activity"


def test_analytics_decay_needs_full_book_scoped_to_tracked_pair(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    plan = make_plan(symbols=("BTC", "ETH"), venues=("HL", "ASTER"))
    live, _ = state(plan, policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")))
    paper, _ = make_state(plan, policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")))

    assert live.needs_full_book("BTC.HL") is False
    assert live.needs_full_book("ETH.ASTER") is False
    assert paper.needs_full_book("BTC.HL") is False
    assert paper.needs_full_book("ETH.ASTER") is False

    recorder.analytics.track_candidate_decay("BTC", "BTC.HL", "BTC.ASTER", "HL", "ASTER", 25.0, 20.0, NOW)
    assert recorder.analytics.has_active_decay() is True
    assert live.needs_full_book("BTC.HL") is True
    assert live.needs_full_book("BTC.ASTER") is True
    assert live.needs_full_book("ETH.HL") is False
    assert live.needs_full_book("ETH.ASTER") is False
    assert paper.needs_full_book("BTC.HL") is True
    assert paper.needs_full_book("BTC.ASTER") is True
    assert paper.needs_full_book("ETH.HL") is False
    assert paper.needs_full_book("ETH.ASTER") is False

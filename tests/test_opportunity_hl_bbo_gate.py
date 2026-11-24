"""Tests for Hyperliquid real-time BBO pre-entry & pre-dispatch gating (Issue #130)."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opportunity_core import BookSnapshot, MarketMetadata, ScanSettings
from opportunity_entry_evidence import entry_dispatch_evidence, entry_pair_snapshot
from opportunity_runtime import create_observer, data_clients
from opportunity_live import LiveTradeState
from opportunity_live_runner import LiveCoordinator
from opportunity_scan import MarketPlan, ScanConfigError, ScanPlan, load_plan, parse_plan
import opportunity_timing
from opportunity_trade import TradePolicy
import spread_watch
from test_opportunity_live import NOW, accounts, state as live_state, submissions
from test_opportunity_trade import make_plan, publish, set_book


MS = 1_000_000


def _bbo_state(bbo_provider=None, *, policy=None, hl_cushion_bps="4"):
    if policy is None:
        policy = TradePolicy(
            order_notional_usd=D(100),
            entry_edge_bps=D(20),
            slippage_bps=D(0),
            entry_slippage_bps_by_venue={"HL": hl_cushion_bps, "ASTER": "0"},
        )
    live, events = live_state(make_plan(), policy)
    live.bbo_provider = bbo_provider
    return live, events


def test_parse_plan_and_universe_hyperliquid_bbo_gate():
    base_doc = {
        "schema_version": 1,
        "scan": {"target_notional": "100", "min_entry_edge_bps": "1",
                 "reserve_bps": "0", "max_age_ms": 2000, "max_skew_ms": 500,
                 "max_receive_age_ms": 2000},
        "recording": {"enabled": False},
        "runtime": {"hyperliquid_bbo_gate": True},
        "markets": [
            {"symbol": "BTC", "venue": "HL", "instrument_id": "BTC-USD-PERP.HYPERLIQUID",
             "quote_to_usd": "1", "valuation_source": "test"},
            {"symbol": "BTC", "venue": "ASTER", "instrument_id": "BTCUSDT-PERP.ASTER",
             "quote_to_usd": "1", "valuation_source": "test"},
        ],
    }
    plan = parse_plan(base_doc, ROOT / "config")
    assert plan.hyperliquid_bbo_gate is True

    bad_doc = dict(base_doc, runtime={"hyperliquid_bbo_gate": "yes"})
    with pytest.raises(ScanConfigError, match="hyperliquid_bbo_gate must be boolean"):
        parse_plan(bad_doc, ROOT / "config")

    universe = load_plan(ROOT / "config" / "opportunity-scan.universe.toml")
    assert universe.hyperliquid_bbo_gate is True


def test_stage1_entry_hl_buy_bbo_cushion_and_freshness(tmp_path):
    timing_dir = tmp_path / "timing"
    bbo = {"BTC.HL": (F("99.99"), F("100.05"), NOW + MS, NOW + MS)}

    def run_all():
        # Fresh BBO ask 100.05 > persisted limit 100.04 (+4 bps cushion): must reject Stage 1.
        live, _ = _bbo_state(lambda inst: bbo.get(inst))
        publish(live, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
        assert not live.positions and not submissions(live)

        # Fresh BBO ask 100.03 <= 100.04 cushion: must pass Stage 1.
        bbo["BTC.HL"] = (F("99.99"), F("100.03"), NOW + MS, NOW + MS)
        live_ok, _ = _bbo_state(lambda inst: bbo.get(inst))
        publish(live_ok, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
        assert len(live_ok.positions) == 1 and len(submissions(live_ok)) == 2

        # Older BBO (ts_event < book.ts_event_ns) ignored even at 101.00.
        bbo["BTC.HL"] = (F("100.99"), F("101.00"), NOW - MS, NOW + 5 * MS)
        live_old_evt, _ = _bbo_state(lambda inst: bbo.get(inst))
        publish(live_old_evt, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
        assert len(live_old_evt.positions) == 1

        # Same-ms BBO with ts_init <= book.ts_received_ns ignored; ts_init > book.ts_received_ns enforced.
        bbo["BTC.HL"] = (F("100.99"), F("101.00"), NOW, NOW)
        live_same_init, _ = _bbo_state(lambda inst: bbo.get(inst))
        publish(live_same_init, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
        assert len(live_same_init.positions) == 1

        bbo["BTC.HL"] = (F("100.99"), F("101.00"), NOW, NOW + 1)
        live_newer_init, _ = _bbo_state(lambda inst: bbo.get(inst))
        publish(live_newer_init, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
        assert not live_newer_init.positions and not submissions(live_newer_init)

        # None BBO passes cleanly.
        live_none, _ = _bbo_state(lambda _inst: None)
        publish(live_none, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
        assert len(live_none.positions) == 1
        return 0

    assert opportunity_timing.run_timed(run_all, enabled=True, directory=timing_dir) == 0
    report = next(timing_dir.glob("timing-*.json"))
    text = report.read_text(encoding="utf-8")
    assert "hl_bbo_outside_limit" in text


def test_stage1_entry_hl_sell_and_kpepe_native_units():
    # HL SELL direction: raw ask/bid at 100.30, 4 bps cushion -> floor limit 100.25.
    bbo = {"BTC.HL": (F("100.24"), F("100.25"), NOW + MS, NOW + MS)}
    live_sell_rej, _ = _bbo_state(lambda inst: bbo.get(inst))
    set_book(live_sell_rej, "ASTER", "99.99", "100.00", NOW)
    set_book(live_sell_rej, "HL", "100.30", "100.31", NOW)
    live_sell_rej.evaluate_symbol("BTC", NOW)
    assert not live_sell_rej.positions and not submissions(live_sell_rej)

    bbo["BTC.HL"] = (F("100.26"), F("100.27"), NOW + MS, NOW + MS)
    live_sell_ok, _ = _bbo_state(lambda inst: bbo.get(inst))
    set_book(live_sell_ok, "ASTER", "99.99", "100.00", NOW)
    set_book(live_sell_ok, "HL", "100.30", "100.31", NOW)
    live_sell_ok.evaluate_symbol("BTC", NOW)
    assert len(live_sell_ok.positions) == 1

    # kPEPE canonical_multiplier=1000: canonical ask 0.010 -> native bound 10.00 (with 4 bps = 10.004).
    markets = (
        MarketPlan("PEPE", "HL", "KPEPE-USD-PERP.HYPERLIQUID", D(1), "synthetic parity",
                   canonical_multiplier=D(1000)),
        MarketPlan("PEPE", "ASTER", "1000PEPEUSDT-PERP.ASTER", D(1), "synthetic parity",
                   canonical_multiplier=D(1000)),
    )
    plan = ScanPlan(markets, ScanSettings(reserve_bps=D(0)), None, evaluation_interval_ms=100)
    policy = TradePolicy(
        order_notional_usd=D(100), entry_edge_bps=D(20), slippage_bps=D(0),
        entry_slippage_bps_by_venue={"HL": "4", "ASTER": "0"},
    )
    for ask_px, expect_open in ((F("10.005"), False), (F("10.003"), True)):
        live_k = LiveTradeState(
            plan, policy, max_pairs=2, max_gross_notional_usd=D(500),
            aster_leverage_targets={"1000PEPEUSDT-PERP.ASTER": 5},
        )
        for m in markets:
            live_k.register_instrument(m.instrument_id, D("0.001"), D(1), "USD")
            live_k.update_metadata(m.instrument_id, MarketMetadata(
                m.instrument_id, D(1), multiplier=D(1000), taker_fee_bps=D(0), fee_source="synthetic"),
                NOW, evaluate=False)
        accounts(live_k, NOW)
        assert live_k.activate(NOW)
        live_k.drain_intents()
        live_k.bbo_provider = lambda i, px=ask_px: (F("9.999"), px, NOW + MS, NOW + MS) if i == "KPEPE-USD-PERP.HYPERLIQUID" else None
        live_k.update_book("KPEPE-USD-PERP.HYPERLIQUID", BookSnapshot(
            "HL", "PEPE", ((D("0.00999"), D("10")),), ((D("0.010"), D("10")),), NOW, NOW), NOW, evaluate=False)
        live_k.update_book("1000PEPEUSDT-PERP.ASTER", BookSnapshot(
            "ASTER", "PEPE", ((D("0.01005"), D("10")),), ((D("0.01006"), D("10")),), NOW, NOW), NOW, evaluate=False)
        live_k.evaluate_symbol("PEPE", NOW)
        assert (len(live_k.positions) == 1) is expect_open


def test_stage2_dispatch_evidence_snapshot_and_timing_fields(tmp_path):
    bbo = {}
    live, events = _bbo_state(lambda inst: bbo.get(inst))
    publish(live, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
    pos = next(iter(live.positions.values()))
    assert len(live.outbox) == 2

    snap0 = entry_pair_snapshot(live, pos)
    check_now = NOW + 10 * MS

    # Stale BBO does not trigger block, and snapshot tracks BBO tuple changes.
    bbo["BTC.HL"] = (F("99.99"), F("101.00"), NOW - MS, NOW + MS)
    snap1 = entry_pair_snapshot(live, pos)
    assert snap1 != snap0
    reason_stale, _ = entry_dispatch_evidence(live, pos, check_now)
    assert reason_stale == "valid"

    # Fresh BBO inside 4 bps cushion passes.
    bbo["BTC.HL"] = (F("100.01"), F("100.02"), NOW + MS, NOW + 2 * MS)
    reason_ok, evidence_ok = entry_dispatch_evidence(live, pos, check_now)
    assert reason_ok == "valid"
    assert evidence_ok["buy"]["bbo_ask_price"] == "100.02"

    # Fresh BBO outside 4 bps limit blocks with bbo_price_outside_limit & populates fields.
    bbo["BTC.HL"] = (F("100.04"), F("100.05"), NOW + 2 * MS, NOW + 3 * MS)
    snap2 = entry_pair_snapshot(live, pos)
    assert snap2 != snap1
    reason, evidence = entry_dispatch_evidence(live, pos, check_now)
    assert reason == "bbo_price_outside_limit"
    assert evidence["buy"]["bbo_bid_price"] == "100.04"
    assert evidence["buy"]["bbo_ask_price"] == "100.05"
    assert evidence["buy"]["bbo_ts_event_ns"] == NOW + 2 * MS
    assert evidence["buy"]["bbo_ts_init_ns"] == NOW + 3 * MS

    # record_entry_stage preserves bbo_price_outside_limit & the 4 BBO leg fields.
    timing_dir = tmp_path / "stage2_timing"
    req_id = pos["entry_ids"][0]

    def run_stage():
        marker = opportunity_timing.start_latency()
        opportunity_timing.begin_entry_trace(req_id, "HL", marker, check_now)
        opportunity_timing.record_entry_stage(req_id, "send_check", wall_ns=check_now, reason=reason, evidence=evidence)
        opportunity_timing.record_entry_stage(req_id, "local_blocked", wall_ns=check_now, reason=reason)
        return 0

    assert opportunity_timing.run_timed(run_stage, enabled=True, directory=timing_dir) == 0
    doc = next(timing_dir.glob("timing-*.json")).read_text(encoding="utf-8")
    assert '"reason": "bbo_price_outside_limit"' in doc
    assert '"bbo_bid_price": "100.04"' in doc
    assert '"bbo_ask_price": "100.05"' in doc
    assert f'"bbo_ts_event_ns": {NOW + 2 * MS}' in doc
    assert f'"bbo_ts_init_ns": {NOW + 3 * MS}' in doc

    sent = []
    coord = NS(live=live, proxy=NS(restart_pending=False), gateway=NS(dispatch=lambda i, s, **k: sent.append(i)))
    LiveCoordinator._live_intents(coord, check_now)
    assert sent == []
    assert [e for e in events if e["event"] == "entry_dispatch_blocked"][-1]["reason"] == "bbo_price_outside_limit"


def test_runtime_wires_bbo_cache_only_and_bbo_provider(monkeypatch):
    captured = {}

    def fake_hl_client(instrument_ids, *, bbo_cache_only=False):
        captured["instrument_ids"] = tuple(instrument_ids)
        captured["bbo_cache_only"] = bbo_cache_only
        return object(), NS(latest_bbo=lambda _id: None)

    monkeypatch.setattr(spread_watch, "_hyperliquid_client", fake_hl_client)
    monkeypatch.setattr(spread_watch, "_aster_client", lambda ids: (object(), object()))

    markets = (
        MarketPlan("BTC", "HL", "BTC-USD-PERP.HYPERLIQUID", D(1), "synthetic parity"),
        MarketPlan("BTC", "ASTER", "BTCUSDT-PERP.ASTER", D(1), "synthetic parity"),
    )
    plan = ScanPlan(markets, ScanSettings(), None, hyperliquid_bbo_gate=True)
    clients = data_clients(plan)
    assert captured["bbo_cache_only"] is True
    assert captured["instrument_ids"] == ("BTC-USD-PERP.HYPERLIQUID",)
    configs = {cid: cfg for cid, _factory, cfg in clients}

    fake_quote = NS(bid_price="100.01", ask_price="100.02", bid_size="3.5", ask_size="4.5",
                    ts_event=NOW + MS, ts_init=NOW + 2 * MS)
    configs["HYPERLIQUID"] = NS(latest_bbo=lambda nid: fake_quote if str(nid) == "BTC-USD-PERP.HYPERLIQUID" else None)
    state = NS(bbo_provider=None, bbo_l1_provider=None)
    create_observer(plan, state, lambda: None, configs)
    assert callable(state.bbo_provider)
    assert state.bbo_provider("BTC-USD-PERP.HYPERLIQUID") == (
        F("100.01"), F("100.02"), NOW + MS, NOW + 2 * MS
    )
    assert state.bbo_provider("BTCUSDT-PERP.ASTER") is None
    assert callable(state.bbo_l1_provider)
    assert state.bbo_l1_provider("BTC-USD-PERP.HYPERLIQUID") == (
        F("100.01"), F("100.02"), F("3.5"), F("4.5"), NOW + MS, NOW + 2 * MS
    )
    assert state.bbo_l1_provider("BTCUSDT-PERP.ASTER") is None


def _overlay_observer(tmp_path, monkeypatch, state_obj, *, scan_plan=None, output_path=None, tape_recorder=None,
                      latest_bbo_map=None):
    import opportunity_runtime as runtime

    native = pytest.importorskip("nautilus_trader.model")
    if scan_plan is None:
        markets = (
            MarketPlan("BTC", "HL", "BTC-USD-PERP.HYPERLIQUID", D(1), "synthetic parity"),
            MarketPlan("BTC", "ASTER", "BTCUSDT-PERP.ASTER", D(1), "synthetic parity"),
        )
        scan_plan = ScanPlan(
            markets,
            ScanSettings(reserve_bps=D(0), max_age_ms=2000, max_skew_ms=500, max_receive_age_ms=2000),
            output_path,
            depth_levels=5,
            max_levels_per_side=100,
            refresh_ms=1000,
            top_n=10,
            evaluation_interval_ms=20,
            hyperliquid_fast_book=True,
            hyperliquid_bbo_gate=True,
        )
    clock = [NOW]
    monkeypatch.setattr(runtime.time, "time_ns", lambda: clock[0])
    ids = [native.InstrumentId.from_str(m.instrument_id) for m in scan_plan.markets]
    instruments = {
        i: NS(id=i, size_increment="0.01", multiplier="1",
              min_quantity="0.01", min_notional="1 USD", taker_fee=D("0.0001"))
        for i in ids
    }
    bbo_map = {} if latest_bbo_map is None else latest_bbo_map
    client_configs = {"HYPERLIQUID": NS(latest_bbo=lambda iid: bbo_map.get(iid))}
    template = runtime.create_observer(
        scan_plan, state_obj, lambda: None, client_configs, tape_recorder=tape_recorder,
    )

    class UnderTest(type(template)):
        @property
        def cache(self):
            return NS(instrument=lambda i: instruments.get(i))

        @property
        def clock(self):
            return NS(set_timer=lambda *a, **k: None, cancel_timer=lambda *a, **k: None)

        def subscribe_instrument(self, *a, **k): pass
        def subscribe_book_deltas(self, *a, **k): pass
        def subscribe_quotes(self, *a, **k): pass
        def subscribe_instrument_status(self, *a, **k): pass
        def unsubscribe_instrument(self, *a, **k): pass
        def unsubscribe_book_deltas(self, *a, **k): pass
        def unsubscribe_quotes(self, *a, **k): pass
        def unsubscribe_instrument_status(self, *a, **k): pass

    observer = UnderTest()
    observer.on_start()
    assert observer.failure is None
    return scan_plan, observer, ids, bbo_map, clock, native


def _snap2(native, iid, bid1, ask1, ts_ns, *, bid2=None, ask2=None, qty="10.00"):
    flags = native.RecordFlag.F_SNAPSHOT.value
    deltas = [
        native.OrderBookDelta.clear(iid, 1, ts_ns, ts_ns),
        native.OrderBookDelta(
            iid, native.BookAction.UPDATE,
            native.BookOrder(native.OrderSide.BUY, native.Price.from_str(bid1), native.Quantity.from_str(qty), 0),
            flags, 1, ts_ns, ts_ns,
        ),
    ]
    if bid2 is not None:
        deltas.append(native.OrderBookDelta(
            iid, native.BookAction.UPDATE,
            native.BookOrder(native.OrderSide.BUY, native.Price.from_str(bid2), native.Quantity.from_str(qty), 0),
            flags, 1, ts_ns, ts_ns,
        ))
    if ask2 is not None:
        deltas.append(native.OrderBookDelta(
            iid, native.BookAction.UPDATE,
            native.BookOrder(native.OrderSide.SELL, native.Price.from_str(ask2), native.Quantity.from_str(qty), 0),
            flags, 1, ts_ns, ts_ns,
        ))
    deltas.append(native.OrderBookDelta(
        iid, native.BookAction.UPDATE,
        native.BookOrder(native.OrderSide.SELL, native.Price.from_str(ask1), native.Quantity.from_str(qty), 0),
        flags | native.RecordFlag.F_LAST.value, 1, ts_ns, ts_ns,
    ))
    return native.OrderBookDeltas(iid, deltas)


def test_bbo_overlay_case_b_lazy_and_eager_wakes_and_avoids_book_skew(tmp_path, monkeypatch):
    # Case B: L1 price changed in BBO -> coverage_limit=1 (never mixes stale L2 level 2) & avoids book_skew.
    markets = (
        MarketPlan("BTC", "HL", "BTC-USD-PERP.HYPERLIQUID", D(1), "synthetic parity"),
        MarketPlan("BTC", "ASTER", "BTCUSDT-PERP.ASTER", D(1), "synthetic parity"),
    )
    scan_plan = ScanPlan(
        markets,
        ScanSettings(reserve_bps=D(0), max_age_ms=2000, max_skew_ms=500, max_receive_age_ms=2000),
        None,
        depth_levels=5, max_levels_per_side=100, refresh_ms=1000, top_n=10,
        evaluation_interval_ms=20, hyperliquid_fast_book=True, hyperliquid_bbo_gate=True,
    )
    policy = TradePolicy(
        order_notional_usd=D("20"), entry_edge_bps=D("20"), slippage_bps=D("0"),
        entry_slippage_bps_by_venue={"HL": "0", "ASTER": "0"},
    )

    for eager in (False, True):
        live, _ = live_state(scan_plan, policy)
        if eager:
            live.supports_lazy_books = False
        for m in markets:
            live.register_instrument(m.instrument_id, D("0.01"), D(1), "USD")
            live.update_metadata(m.instrument_id, MarketMetadata(
                m.instrument_id, D("0.01"), multiplier=D(1), taker_fee_bps=D(0), fee_source="synthetic"),
                NOW, evaluate=False)
        accounts(live, NOW)
        _, observer, ids, bbo_map, clock, native = _overlay_observer(tmp_path, monkeypatch, live, scan_plan=scan_plan)
        hl_id, aster_id = ids[0], ids[1]

        # Initial 2-level L2 snapshots on both venues with non-qualifying spread.
        observer.on_book_deltas(_snap2(native, hl_id, "99.99", "100.00", clock[0], bid2="99.90", ask2="100.10"))
        observer.on_book_deltas(_snap2(native, aster_id, "99.99", "100.00", clock[0], bid2="99.90", ask2="100.10"))
        clock[0] += 20 * MS
        observer.on_time_event(None)
        assert not live.positions

        # Advance 600ms (> max_skew_ms=500): Aster moves up; HL L2 stays at NOW, only HL latest_bbo updates!
        clock[0] += 580 * MS
        accounts(live, clock[0])
        observer.on_book_deltas(_snap2(native, aster_id, "100.50", "100.51", clock[0], bid2="100.40", ask2="100.60"))
        clock[0] += 20 * MS
        observer.on_time_event(None)
        # HL L2 is 600ms older than Aster -> blocked by skew before BBO arrives.
        assert not live.positions

        # HL BBO moves to tighter ask 100.02 (price changed vs l2Book 100.00) -> immediately overlays & opens!
        bbo_ts = clock[0]
        bbo_map[hl_id] = NS(bid_price="100.01", ask_price="100.02", bid_size="5.00", ask_size="5.00",
                            ts_event=bbo_ts, ts_init=bbo_ts)
        clock[0] += 20 * MS
        observer.on_time_event(None)
        hl_book = live.books[str(hl_id)]
        assert hl_book.ts_event_ns == bbo_ts
        assert hl_book.coverage_limit == 1
        assert hl_book.asks == ((D("100.02"), D("5.00")),)
        assert hl_book.bids == ((D("100.01"), D("5.00")),)
        assert len(live.positions) == 1
        assert len([i for i in live.outbox if i.action == "submit"]) == 2


def test_bbo_overlay_case_a_preserves_l2_depth_and_kpepe_normalization(tmp_path, monkeypatch):
    markets = (
        MarketPlan("PEPE", "HL", "KPEPE-USD-PERP.HYPERLIQUID", D(1), "synthetic", canonical_multiplier=D(1000)),
        MarketPlan("PEPE", "ASTER", "1000PEPEUSDT-PERP.ASTER", D(1), "synthetic", canonical_multiplier=D(1000)),
    )
    scan_plan = ScanPlan(
        markets,
        ScanSettings(reserve_bps=D(0), max_age_ms=2000, max_skew_ms=500, max_receive_age_ms=2000),
        None,
        depth_levels=5, max_levels_per_side=100, refresh_ms=1000, top_n=10,
        evaluation_interval_ms=20, hyperliquid_fast_book=True, hyperliquid_bbo_gate=True,
    )
    policy = TradePolicy(
        order_notional_usd=D("20"), entry_edge_bps=D("20"), slippage_bps=D("0"),
        entry_slippage_bps_by_venue={"HL": "0", "ASTER": "0"},
    )
    live, _ = live_state(scan_plan, policy)
    for m in markets:
        live.register_instrument(m.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(m.instrument_id, MarketMetadata(
            m.instrument_id, D("0.01"), multiplier=D(1000), taker_fee_bps=D(0), fee_source="synthetic"),
            NOW, evaluate=False)
    accounts(live, NOW)
    _, observer, ids, bbo_map, clock, native = _overlay_observer(tmp_path, monkeypatch, live, scan_plan=scan_plan)
    hl_id, aster_id = ids[0], ids[1]

    # Native 10.00 / 10.01 with L1 qty 1.50 (~$15 < $20 notional -> needs L2 level 10.02!)
    observer.on_book_deltas(_snap2(native, hl_id, "9.99", "10.01", clock[0], bid2="9.98", ask2="10.02", qty="1.50"))
    observer.on_book_deltas(_snap2(native, aster_id, "9.99", "10.01", clock[0], qty="5.00"))
    clock[0] += 20 * MS
    observer.on_time_event(None)

    # Case A: Same native L1 price (9.99 / 10.01) in BBO with refreshed size 1.60 & newer ts; Aster moves to 10.06.
    bbo_ts = clock[0]
    bbo_map[hl_id] = NS(bid_price="9.99", ask_price="10.01", bid_size="1.60", ask_size="1.60",
                        ts_event=bbo_ts, ts_init=bbo_ts)
    observer.on_book_deltas(_snap2(native, aster_id, "10.06", "10.07", bbo_ts, qty="5.00"))
    clock[0] += 20 * MS
    observer.on_time_event(None)

    hl_book = live.books[str(hl_id)]
    assert hl_book.ts_event_ns == bbo_ts
    assert hl_book.coverage_limit == 5
    # Level 1 normalized with updated size 1.60, level 2 preserved from L2 book!
    assert hl_book.asks == ((D("0.01001"), D("1.60")), (D("0.01002"), D("1.50")))
    assert live.fast_books[str(hl_id)].ask_price == pytest.approx(0.01001)
    assert len(live.positions) == 1


def test_bbo_overlay_fail_closed_guards_out_of_order_l2_invalidation_and_tape(tmp_path, monkeypatch):
    markets = (
        MarketPlan("BTC", "HL", "BTC-USD-PERP.HYPERLIQUID", D(1), "synthetic parity"),
        MarketPlan("BTC", "ASTER", "BTCUSDT-PERP.ASTER", D(1), "synthetic parity"),
    )
    scan_plan = ScanPlan(
        markets,
        ScanSettings(reserve_bps=D(0), max_age_ms=2000, max_skew_ms=500, max_receive_age_ms=2000),
        None,
        depth_levels=5, max_levels_per_side=100, refresh_ms=1000, top_n=10,
        evaluation_interval_ms=20, hyperliquid_fast_book=True, hyperliquid_bbo_gate=True,
    )
    policy = TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20"))
    live, _ = live_state(scan_plan, policy)
    live.supports_lazy_books = False
    for m in markets:
        live.register_instrument(m.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(m.instrument_id, MarketMetadata(
            m.instrument_id, D("0.01"), multiplier=D(1), taker_fee_bps=D(0), fee_source="synthetic"),
            NOW, evaluate=False)
    accounts(live, NOW)
    _, observer, ids, bbo_map, clock, native = _overlay_observer(tmp_path, monkeypatch, live, scan_plan=scan_plan)
    hl_id = ids[0]

    l2_t0 = clock[0]
    observer.on_book_deltas(_snap2(native, hl_id, "99.99", "100.00", l2_t0))
    clock[0] += 20 * MS
    observer.on_time_event(None)

    # Guard 1: missing bid_size / ask_size, crossed/zero BBO, future/inverted ts, older BBO -> ignored.
    for make_bad in (
        lambda t: NS(bid_price="99.98", ask_price="99.99", ts_event=t, ts_init=t),
        lambda t: NS(bid_price="100.00", ask_price="99.99", bid_size="5", ask_size="5", ts_event=t, ts_init=t),
        lambda t: NS(bid_price="0", ask_price="99.99", bid_size="5", ask_size="5", ts_event=t, ts_init=t),
        lambda t: NS(bid_price="99.98", ask_price="99.99", bid_size="5", ask_size="0", ts_event=t, ts_init=t),
        lambda t: NS(bid_price="99.98", ask_price="99.99", bid_size="5", ask_size="5", ts_event=t + 5, ts_init=t),
        lambda t: NS(bid_price="99.98", ask_price="99.99", bid_size="5", ask_size="5", ts_event=t + 5, ts_init=t + 10),
        lambda t: NS(bid_price="99.98", ask_price="99.99", bid_size="5", ask_size="5", ts_event=l2_t0 - MS, ts_init=t),
    ):
        clock[0] += 20 * MS
        bbo_map[hl_id] = make_bad(clock[0])
        observer.on_time_event(None)
        assert live.books[str(hl_id)].ts_event_ns == l2_t0

    # Guard 2: valid newer BBO applies, then out-of-order older l2Book arrives -> ts & published_ns preserved!
    bbo_ts = clock[0]
    bbo_map[hl_id] = NS(bid_price="99.97", ask_price="99.98", bid_size="4.0", ask_size="4.0",
                        ts_event=bbo_ts, ts_init=bbo_ts)
    clock[0] += 20 * MS
    orig_pub_ns = clock[0]
    observer.on_time_event(None)
    assert live.books[str(hl_id)].ts_event_ns == bbo_ts
    assert live.book_publications[str(hl_id)][1] == orig_pub_ns

    # Out-of-order older L2 batch (ts = bbo_ts - 10ms) arrives later: must NOT regress book or published_ns!
    observer.on_book_deltas(_snap2(native, hl_id, "99.96", "99.97", bbo_ts - 10 * MS))
    clock[0] += 200 * MS
    observer.on_time_event(None)
    assert live.books[str(hl_id)].ts_event_ns == bbo_ts
    assert live.books[str(hl_id)].asks == ((D("99.98"), D("4.0")),)
    assert live.book_publications[str(hl_id)][1] == orig_pub_ns

    # Guard 3: invalidate clears overlay state so subsequent timer does not resurrect stale book!
    observer.invalidate(hl_id, reason="disconnected")
    clock[0] += 20 * MS
    observer.on_time_event(None)
    assert str(hl_id) not in live.books
    assert observer._hl_l2_stamps == {} and observer._hl_bbo_state == {}

    # Guard 4: tape_recorder or output_path disables overlay
    tape = NS(record_instrument=lambda *a, **k: None, record_book=lambda *a, **k: None,
              record_invalidation=lambda *a, **k: None)
    live_tape, _ = live_state(scan_plan, policy)
    _, obs_tape, _, _, _, _ = _overlay_observer(tmp_path / "t", monkeypatch, live_tape, tape_recorder=tape)
    assert obs_tape._hl_bbo_overlay_enabled is False
    live_out, _ = live_state(scan_plan, policy)
    out_plan = replace(scan_plan, output_path=tmp_path / "out.jsonl")
    _, obs_out, _, _, _, _ = _overlay_observer(tmp_path / "o", monkeypatch, live_out, scan_plan=out_plan)
    assert obs_out._hl_bbo_overlay_enabled is False


def test_stage1_and_stage2_bbo_l1_depth_insufficient(tmp_path):
    # Stage 1 & Stage 2 reject when BBO price moved (within 4 bps cushion) and L1 size < order quantity.
    bbo = {}

    def l1_prov(inst_id):
        return bbo.get(inst_id)

    live, events = _bbo_state()
    live.bbo_l1_provider = l1_prov
    # Buy 1 BTC (~$100 notional): L2 book at 100.00 qty 2.00; fresh BBO moves to 100.01 (<=100.04) but qty only 0.40 (<1.0)
    bbo["BTC.HL"] = (F("99.99"), F("100.01"), F("2.0"), F("0.4"), NOW + MS, NOW + MS)
    publish(live, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
    assert not live.positions and not submissions(live)

    # Same L1 price (100.00 < limit 100.04) with small L1 qty passes when L2 covers behind it;
    # at limit price (100.04 == limit) with small L1 qty blocks!
    bbo["BTC.HL"] = (F("99.99"), F("100.00"), F("2.0"), F("0.4"), NOW + 2 * MS, NOW + 2 * MS)
    live_same, _ = _bbo_state()
    live_same.bbo_l1_provider = l1_prov
    publish(live_same, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
    assert len(live_same.positions) == 1

    # Now mutate L1 qty at Stage 2 dispatch with BBO price moved to 100.01 and qty 0.4 < 1.0 -> bbo_insufficient_depth
    pos = next(iter(live_same.positions.values()))
    check_now = NOW + 10 * MS
    snap_before = entry_pair_snapshot(live_same, pos)
    bbo["BTC.HL"] = (F("99.99"), F("100.01"), F("2.0"), F("0.4"), NOW + 3 * MS, NOW + 3 * MS)
    assert entry_pair_snapshot(live_same, pos) != snap_before
    reason, evidence = entry_dispatch_evidence(live_same, pos, check_now)
    assert reason == "bbo_insufficient_depth"
    assert evidence["buy"]["bbo_bid_size"] == "2"
    assert evidence["buy"]["bbo_ask_size"] == "0.4"

    timing_dir = tmp_path / "bbo_depth_timing"
    req_id = pos["entry_ids"][0]

    def run_stage():
        marker = opportunity_timing.start_latency()
        opportunity_timing.begin_entry_trace(req_id, "HL", marker, check_now)
        opportunity_timing.record_entry_stage(req_id, "send_check", wall_ns=check_now, reason=reason, evidence=evidence)
        return 0

    assert opportunity_timing.run_timed(run_stage, enabled=True, directory=timing_dir) == 0
    doc = next(timing_dir.glob("timing-*.json")).read_text(encoding="utf-8")
    assert '"reason": "bbo_insufficient_depth"' in doc
    assert '"bbo_bid_size": "2"' in doc
    assert '"bbo_ask_size": "0.4"' in doc

    from opportunity_review import REASONS
    assert "bbo_insufficient_depth" in REASONS

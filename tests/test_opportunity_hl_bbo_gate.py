"""Tests for Hyperliquid real-time BBO pre-entry & pre-dispatch gating (Issue #130)."""
from __future__ import annotations

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

    fake_quote = NS(bid_price="100.01", ask_price="100.02", ts_event=NOW + MS, ts_init=NOW + 2 * MS)
    configs["HYPERLIQUID"] = NS(latest_bbo=lambda nid: fake_quote if str(nid) == "BTC-USD-PERP.HYPERLIQUID" else None)
    state = NS(bbo_provider=None)
    create_observer(plan, state, lambda: None, configs)
    assert callable(state.bbo_provider)
    assert state.bbo_provider("BTC-USD-PERP.HYPERLIQUID") == (
        F("100.01"), F("100.02"), NOW + MS, NOW + 2 * MS
    )
    assert state.bbo_provider("BTCUSDT-PERP.ASTER") is None

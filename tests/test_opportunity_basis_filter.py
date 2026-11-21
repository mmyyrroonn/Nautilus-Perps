"""Tests for rolling basis baseline filter and adaptive max_hold route blocking (#125)."""
from __future__ import annotations

from decimal import Decimal as D
from fractions import Fraction as F
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "scripts"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import analyze_opportunity_session as analyzer
import opportunity_analytics as analytics_mod
from opportunity_basis import (
    BasisFilterPolicy,
    RollingBasisTracker,
    RouteMaxHoldTracker,
    mid_basis_bps,
)
from opportunity_core import BookSnapshot, MarketMetadata
from opportunity_entry_evidence import entry_dispatch_evidence
from opportunity_paper import PaperTradeState
from opportunity_scan import ScanConfigError, ScanSettings
from opportunity_trade import TradePolicy
from test_opportunity_live import accounts, fill, open_pair, state, submissions
from test_opportunity_trade import NOW, SECOND, make_plan, publish, set_book


def _paper_state(plan, policy, basis_policy):
    paper = PaperTradeState(plan, policy, basis_filter=basis_policy)
    for market in paper.plan.markets:
        paper.update_metadata(
            market.instrument_id,
            MarketMetadata(market.instrument_id, D("0.01"), taker_fee_bps=D("0"), fee_source="synthetic"),
            NOW,
            evaluate=False,
        )
    return paper


def _book(venue: str, symbol: str, bid: str, ask: str, ts_ns: int, size: str = "10") -> BookSnapshot:
    return BookSnapshot(
        venue, symbol, ((D(bid), D(size)),), ((D(ask), D(size)),), ts_ns, ts_ns, valid=True, coverage_limit=20,
    )


def test_basis_filter_policy_validation_and_document():
    policy = BasisFilterPolicy(
        enabled=True,
        window_secs=600,
        warmup_secs=120,
        min_deviation_bps=D("20"),
        max_hold_loss_cooldown_secs=900,
        max_hold_loss_block_strikes=2,
        max_hold_loss_block_secs=3600,
    )
    assert policy.deviation_enabled is True
    assert policy.max_hold_guard_enabled is True
    assert policy.sample_interval_secs == 5
    doc = policy.document()
    assert doc == {
        "enabled": True,
        "window_secs": 600,
        "warmup_secs": 120,
        "min_deviation_bps": "20",
        "max_hold_loss_cooldown_secs": 900,
        "max_hold_loss_block_strikes": 2,
        "max_hold_loss_block_secs": 3600,
        "sample_interval_secs": 5,
    }

    disabled = BasisFilterPolicy(enabled=False)
    assert disabled.deviation_enabled is False
    assert disabled.max_hold_guard_enabled is False

    for bad_kwargs in (
        {"enabled": "true"},
        {"window_secs": 5},
        {"warmup_secs": 0},
        {"window_secs": 60, "warmup_secs": 120},
        {"min_deviation_bps": "0"},
        {"min_deviation_bps": "10000"},
        {"max_hold_loss_block_strikes": 0},
        {"max_hold_loss_cooldown_secs": 900, "max_hold_loss_block_secs": 300},
    ):
        with pytest.raises(ScanConfigError):
            BasisFilterPolicy(**bad_kwargs)


def test_rolling_basis_tracker_warmup_persistent_trap_vs_transient_spike():
    policy = BasisFilterPolicy(enabled=True, window_secs=300, warmup_secs=60, min_deviation_bps=D("20"))
    tracker = RollingBasisTracker(policy)
    settings = ScanSettings(max_age_ms=2000, max_skew_ms=500, max_receive_age_ms=2000)

    # Antisymmetry check: mid basis of A->B equals negated B->A
    book_hl = _book("HL", "XPL", "99.98", "100.00", NOW)
    book_aster = _book("ASTER", "XPL", "100.34", "100.36", NOW)
    fwd = mid_basis_bps(book_hl, book_aster)
    rev = mid_basis_bps(book_aster, book_hl)
    assert fwd is not None and rev is not None
    assert abs(fwd + rev) < 1e-12

    # Warmup fails closed until both warmup_secs span and MIN_WARMUP_SAMPLES are reached
    for step in (0, 20, 40):
        t = NOW + step * SECOND
        assert tracker.observe_pair(
            "XPL.HL", _book("HL", "XPL", "99.98", "100.00", t),
            "XPL.ASTER", _book("ASTER", "XPL", "100.34", "100.36", t),
            settings, t,
        ) is True
        reason, est = tracker.evaluate_entry("XPL.HL", "XPL.ASTER", 35.0, t)
        assert reason == "basis_warmup" and est.ready is False

    # At t = NOW + 60s, persistent +36 bps baseline is warmed -> +35 bps spread has ~-1 bps deviation -> rejected
    t_warm = NOW + 60 * SECOND
    reason, est = tracker.evaluate_entry("XPL.HL", "XPL.ASTER", 35.0, t_warm)
    assert reason == "basis_deviation_insufficient"
    assert est.ready is True and est.samples == 3 and est.deviation_bps is not None and est.deviation_bps < 0

    # Moderate persistent +20 bps baseline: +35 bps (dev=15 < 20) rejected, +45 bps (dev=25 >= 20) admitted
    for step in range(0, 80, 5):
        t = NOW + step * SECOND
        tracker.observe_pair(
            "ADA.HL", _book("HL", "ADA", "99.99", "100.01", t),
            "ADA.ASTER", _book("ASTER", "ADA", "100.19", "100.21", t),
            settings, t,
        )
    assert tracker.evaluate_entry("ADA.HL", "ADA.ASTER", 35.0, NOW + 75 * SECOND)[0] == "basis_deviation_insufficient"
    ok_reason, ok_est = tracker.evaluate_entry("ADA.HL", "ADA.ASTER", 45.0, NOW + 75 * SECOND)
    assert ok_reason is None and ok_est.deviation_bps is not None and ok_est.deviation_bps >= 20.0

    # Hook for #109 carry offset shifts effective baseline cleanly
    carry_reason, carry_est = tracker.evaluate_entry(
        "ADA.HL", "ADA.ASTER", 45.0, NOW + 75 * SECOND, carry_offset_bps=10.0,
    )
    assert carry_reason == "basis_deviation_insufficient"
    assert abs(carry_est.baseline_bps - 30.0) < 0.1


def test_rolling_basis_tracker_outlier_resilience_stale_skip_and_non_flapping():
    # Also verifies window_secs == warmup_secs never flaps warmup in steady state
    policy = BasisFilterPolicy(enabled=True, window_secs=60, warmup_secs=60, min_deviation_bps=D("20"))
    tracker = RollingBasisTracker(policy)
    settings = ScanSettings(max_age_ms=2000, max_skew_ms=500, max_receive_age_ms=2000)

    # Normal ~0 bps ticks + one +50 bps outlier + stale/skewed books (must be ignored)
    for sec in range(0, 70, 5):
        t = NOW + sec * SECOND
        if sec == 30:
            # One outlier tick at +50 bps
            tracker.observe_pair(
                "MNT.HL", _book("HL", "MNT", "99.99", "100.01", t),
                "MNT.BP", _book("BP", "MNT", "100.49", "100.51", t),
                settings, t,
            )
        else:
            tracker.observe_pair(
                "MNT.HL", _book("HL", "MNT", "99.99", "100.01", t),
                "MNT.BP", _book("BP", "MNT", "99.99", "100.01", t),
                settings, t,
            )
    # Stale book (5s old) and skewed book (1s skew) must be rejected without corrupting window
    t_bad = NOW + 75 * SECOND
    assert tracker.observe_pair(
        "MNT.HL", _book("HL", "MNT", "99.99", "100.01", t_bad - 5 * SECOND),
        "MNT.BP", _book("BP", "MNT", "105.00", "105.02", t_bad),
        settings, t_bad,
    ) is False
    assert tracker.observe_pair(
        "MNT.HL", _book("HL", "MNT", "99.99", "100.01", t_bad - SECOND),
        "MNT.BP", _book("BP", "MNT", "105.00", "105.02", t_bad),
        settings, t_bad,
    ) is False

    # Sliding median stays ~0 bps despite outlier and stale ticks; +35 bps transient spike is admitted
    reason, est = tracker.evaluate_entry("MNT.HL", "MNT.BP", 35.0, t_bad)
    assert reason is None
    assert est.ready is True and abs(est.baseline_bps) < 1.0 and est.deviation_bps >= 34.0
    # Bounded window size
    assert len(tracker._windows[("MNT.BP", "MNT.HL")].samples) <= tracker.max_samples

    # Steady-state ticks at window_secs == warmup_secs keep ready=True without flapping
    for sec in range(80, 120, 5):
        t = NOW + sec * SECOND
        tracker.observe_pair(
            "MNT.HL", _book("HL", "MNT", "99.99", "100.01", t),
            "MNT.BP", _book("BP", "MNT", "99.99", "100.01", t),
            settings, t,
        )
        r, _ = tracker.evaluate_entry("MNT.HL", "MNT.BP", 35.0, t)
        assert r is None


def test_offline_replay_rejects_persistent_spread_and_admits_transient_spike(monkeypatch):
    basis_policy = BasisFilterPolicy(
        enabled=True, window_secs=600, warmup_secs=60, min_deviation_bps=D("20"),
    )
    trade_policy = TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20"))
    plan = make_plan(symbols=("XPL", "MNT"), venues=("HL", "ASTER"))

    # 1. LiveTradeState replay
    live, _ = state(plan=plan, policy=trade_policy, max_pairs=1, basis_filter=basis_policy)
    collector = analytics_mod.AnalyticsCollector()
    monkeypatch.setattr("opportunity_live.current_analytics", lambda: collector)

    # Even while holding max_pairs=1 on MNT later or starting with warmup, both symbols observe BBOs
    # Simulate 15 minutes of persistent +35 bps spread on XPL (HL -> ASTER), while MNT stays at ~0 bps
    for step in range(0, 180, 5):
        t = NOW + step * SECOND
        set_book(live, "HL", "99.99", "100.00", t, symbol="XPL")
        set_book(live, "ASTER", "100.35", "100.36", t, symbol="XPL")
        live.evaluate_symbol("XPL", t)
        set_book(live, "HL", "99.99", "100.00", t, symbol="MNT")
        set_book(live, "ASTER", "99.99", "100.00", t, symbol="MNT")
        live.evaluate_symbol("MNT", t)
        assert not live.positions and not submissions(live)

    # Confirm telemetry recorded warmup and deviation rejections on XPL|HL->ASTER
    xpl_funnel = collector.funnel_by_route.get("XPL|HL->ASTER", {})
    assert xpl_funnel.get("live_rejected:basis_warmup", 0) >= 1
    assert xpl_funnel.get("live_rejected:basis_deviation_insufficient", 0) >= 1

    # Now MNT spikes transiently to +35 bps above ~0 bps baseline -> admitted normally!
    t_spike = NOW + 185 * SECOND
    accounts(live, t_spike)
    set_book(live, "HL", "99.99", "100.00", t_spike, symbol="MNT")
    set_book(live, "ASTER", "100.35", "100.36", t_spike, symbol="MNT")
    live.evaluate_symbol("MNT", t_spike)
    intents = submissions(live)
    assert len(intents) == 2
    pos = next(iter(live.positions.values()))
    assert pos["symbol"] == "MNT"
    assert pos["entry"]["basis"]["ready"] is True
    assert abs(pos["entry"]["basis"]["baseline_bps"]) < 1.0
    assert pos["entry"]["basis"]["deviation_bps"] >= 34.0

    # Holding max_pairs=1 on MNT must NOT starve basis warmup on a flat symbol (XPL updates still recorded)
    t_hold = t_spike + 5 * SECOND
    set_book(live, "HL", "99.99", "100.00", t_hold, symbol="XPL")
    set_book(live, "ASTER", "100.35", "100.36", t_hold, symbol="XPL")
    live.evaluate_symbol("XPL", t_hold, changed_id="XPL.ASTER")
    assert live.basis_tracker._windows[("XPL.ASTER", "XPL.HL")].last_sample_ns == t_hold

    # 2. PaperTradeState replay mirrors the exact same behavior
    monkeypatch.setattr("opportunity_paper.current_analytics", lambda: collector)
    paper = _paper_state(plan, trade_policy, basis_policy)
    for step in range(0, 70, 5):
        t = NOW + step * SECOND
        publish(paper, t, symbol="XPL", buy_bid="99.99", buy_ask="100.00", sell_bid="100.35", sell_ask="100.36")
        publish(paper, t, symbol="MNT", buy_bid="99.99", buy_ask="100.00", sell_bid="99.99", sell_ask="100.00")
        assert not paper.positions
    t_paper_spike = NOW + 75 * SECOND
    publish(paper, t_paper_spike, symbol="MNT", buy_bid="99.99", buy_ask="100.00", sell_bid="100.35", sell_ask="100.36")
    assert len(paper.positions) == 1
    ppos = next(iter(paper.positions.values()))
    assert ppos["symbol"] == "MNT" and ppos["entry"]["basis"]["deviation_bps"] >= 34.0


def test_adaptive_max_hold_cooldown_escalation_block_expiry_and_reset(monkeypatch):
    basis_policy = BasisFilterPolicy(
        enabled=True,
        window_secs=600,
        warmup_secs=60,
        min_deviation_bps=D("20"),
        max_hold_loss_cooldown_secs=900,
        max_hold_loss_block_strikes=2,
        max_hold_loss_block_secs=3600,
    )
    trade_policy = TradePolicy(
        order_notional_usd=D("20"),
        entry_edge_bps=D("20"),
        take_profit_bps=D("5"),
        max_loss_bps=D("150"),
        breakeven_after_secs=180,
        force_close_after_secs=300,
        loss_cooldown_secs=60,
    )
    plan = make_plan(symbols=("XPL",), venues=("HL", "ASTER"))
    live, events = state(plan=plan, policy=trade_policy, basis_filter=basis_policy)
    collector = analytics_mod.AnalyticsCollector()
    monkeypatch.setattr("opportunity_live.current_analytics", lambda: collector)

    # Warm up at ~0 bps
    for step in range(0, 70, 5):
        t = NOW + step * SECOND
        set_book(live, "HL", "99.99", "100.00", t, symbol="XPL")
        set_book(live, "ASTER", "99.99", "100.00", t, symbol="XPL")
        live.evaluate_symbol("XPL", t)

    def _open_and_close_max_hold(t_open: int, *, exit_spread_widened: bool = True) -> int:
        accounts(live, t_open)
        set_book(live, "HL", "99.99", "100.00", t_open, symbol="XPL")
        set_book(live, "ASTER", "100.35", "100.36", t_open, symbol="XPL")
        live.evaluate_symbol("XPL", t_open)
        entries = submissions(live)
        assert len(entries) == 2
        for o in entries:
            fill(live, o, t_open + 10_000_000, fee="0.01")
        accounts(live, t_open + 20_000_000)
        live.on_evaluation_timer(t_open + 20_000_000)
        live.drain_intents()
        # Hold past 300s force_close_after_secs with non-converging spread (+45 bps on close)
        t_hold = t_open + 305 * SECOND
        accounts(live, t_hold)
        if exit_spread_widened:
            set_book(live, "HL", "99.98", "100.00", t_hold, symbol="XPL")
            set_book(live, "ASTER", "100.43", "100.45", t_hold, symbol="XPL")
        else:
            set_book(live, "HL", "100.18", "100.20", t_hold, symbol="XPL")
            set_book(live, "ASTER", "100.20", "100.22", t_hold, symbol="XPL")
        live.evaluate_symbol("XPL", t_hold)
        exits = submissions(live)
        assert len(exits) == 2
        for o in exits:
            fill(live, o, t_hold + 10_000_000, fee="0.01")
        accounts(live, t_hold + 20_000_000)
        live.on_evaluation_timer(t_hold + 20_000_000)
        live.drain_intents()
        return t_hold + 20_000_000

    # Strike 1: bilateral max_hold loss triggers extended 900s cooldown (distinct from 60s loss_cooldown_secs)
    t_close1 = _open_and_close_max_hold(NOW + 75 * SECOND)
    cd_event = next(e for e in events if e["event"] == "route_max_hold_cooldown")
    assert cd_event["route"] == "XPL|HL->ASTER"
    assert cd_event["strikes"] == 1 and cd_event["cooldown_secs"] == 900
    assert cd_event["convergence_bps"] is not None and cd_event["convergence_bps"] < 0
    assert live.document()["route_max_hold_blocks"]["XPL|HL->ASTER"]["strikes"] == 1

    # At +120s (after short 60s loss_cooldown_secs expired, still inside 900s route_max_hold_cooldown),
    # forward direction XPL|HL->ASTER is blocked while reverse direction XPL|ASTER->HL is NOT blocked.
    t_mid = t_close1 + 120 * SECOND
    for step in range(-60, 5, 5):
        ts = t_mid + step * SECOND
        set_book(live, "HL", "99.99", "100.00", ts, symbol="XPL")
        set_book(live, "ASTER", "99.99", "100.00", ts, symbol="XPL")
        live.evaluate_symbol("XPL", ts)
    accounts(live, t_mid + 5 * SECOND)
    set_book(live, "HL", "99.99", "100.00", t_mid + 5 * SECOND, symbol="XPL")
    set_book(live, "ASTER", "100.35", "100.36", t_mid + 5 * SECOND, symbol="XPL")
    live.evaluate_symbol("XPL", t_mid + 5 * SECOND)
    assert not live.positions and not submissions(live)
    assert collector.pre_gates.get("route_max_hold_cooldown", 0) >= 1
    assert collector.funnel_by_route["XPL|HL->ASTER"].get("live_rejected:route_max_hold_cooldown", 0) >= 1

    # Verify reverse direction is unaffected
    assert live.route_max_hold_tracker.active_block("XPL.ASTER", "XPL.HL", t_mid + 5 * SECOND)[0] is None

    # Wait past 900s cooldown; Strike 2 escalates to 3600s route block
    t_retry = t_close1 + 910 * SECOND
    for step in range(-65, 0, 5):
        ts = t_retry + step * SECOND
        set_book(live, "HL", "99.99", "100.00", ts, symbol="XPL")
        set_book(live, "ASTER", "99.99", "100.00", ts, symbol="XPL")
        live.evaluate_symbol("XPL", ts)
    t_close2 = _open_and_close_max_hold(t_retry)
    blk_event = next(e for e in events if e["event"] == "route_max_hold_blocked")
    assert blk_event["strikes"] == 2 and blk_event["cooldown_secs"] == 3600 and blk_event["blocked"] is True

    # While blocked, _entry refuses and entry_dispatch_evidence also fails closed on route_max_hold_block
    t_during_block = t_close2 + 60 * SECOND
    accounts(live, t_during_block)
    set_book(live, "HL", "99.99", "100.00", t_during_block, symbol="XPL")
    set_book(live, "ASTER", "100.35", "100.36", t_during_block, symbol="XPL")
    live.evaluate_symbol("XPL", t_during_block)
    assert not live.positions
    assert collector.pre_gates.get("route_max_hold_block", 0) >= 1
    assert collector.funnel_by_route["XPL|HL->ASTER"].get("live_rejected:route_max_hold_block", 0) >= 1

    # Verify dispatch-time defense-in-depth on a prepared position
    live.orders["e-buy"] = {"sent_ns": t_during_block - 1_000_000, "leg": "buy", "price": "100", "quantity": "0.2"}
    live.orders["e-sell"] = {"sent_ns": t_during_block - 1_000_000, "leg": "sell", "price": "100.35", "quantity": "0.2"}
    fake_pos = {
        "position_id": 999, "buy_id": "XPL.HL", "sell_id": "XPL.ASTER",
        "entry_ids": ["e-buy", "e-sell"], "native_rules": {}, "entry": {},
    }
    dispatch_reason, _ = entry_dispatch_evidence(live, fake_pos, t_during_block)
    assert dispatch_reason == "route_max_hold_block"
    live.orders.pop("e-buy", None)
    live.orders.pop("e-sell", None)

    # Bounded TTL expiry automatically unblocks route on timer tick and clears strikes
    t_expired = t_close2 + 3601 * SECOND
    live.on_evaluation_timer(t_expired)
    assert any(e["event"] == "route_max_hold_unblocked" and e["route"] == "XPL|HL->ASTER" for e in events)
    assert "XPL|HL->ASTER" not in live.document()["route_max_hold_blocks"]

    # After expiry, re-warming and entering works normally again
    for step in range(-65, 0, 5):
        ts = t_expired + 70 * SECOND + step * SECOND
        set_book(live, "HL", "99.99", "100.00", ts, symbol="XPL")
        set_book(live, "ASTER", "99.99", "100.00", ts, symbol="XPL")
        live.evaluate_symbol("XPL", ts)
    t_reopen = t_expired + 75 * SECOND
    accounts(live, t_reopen)
    set_book(live, "HL", "99.99", "100.00", t_reopen, symbol="XPL")
    set_book(live, "ASTER", "100.35", "100.36", t_reopen, symbol="XPL")
    live.evaluate_symbol("XPL", t_reopen)
    assert len(submissions(live)) == 2


def test_route_max_hold_strike_rules_and_session_analyzer_rendering():
    policy = BasisFilterPolicy(
        enabled=True,
        max_hold_loss_cooldown_secs=900,
        max_hold_loss_block_strikes=2,
        max_hold_loss_block_secs=3600,
    )
    tracker = RouteMaxHoldTracker(policy)
    econ_loss = {
        "actual_entry": {"buy_notional_usd": "20", "sell_notional_usd": "20.07"},
        "closing_long_cash_usd": "20",
        "closing_short_cash_usd": "20.09",
    }

    # Non-negative max_hold does not trigger a strike
    ev, _ = tracker.record_close(
        route="XPL|HL->ASTER", symbol="XPL", buy_id="XPL.HL", sell_id="XPL.ASTER",
        position_id=1, close_reason="max_hold", bilateral=True,
        effective_net=F("0.001"), economics=econ_loss, now_ns=NOW,
    )
    assert ev is None and not tracker.entries

    # Strike 1 max_hold loss
    ev, payload = tracker.record_close(
        route="XPL|HL->ASTER", symbol="XPL", buy_id="XPL.HL", sell_id="XPL.ASTER",
        position_id=2, close_reason="max_hold", bilateral=True,
        effective_net=F("-0.03"), economics=econ_loss, now_ns=NOW,
    )
    assert ev == "route_max_hold_cooldown" and payload["strikes"] == 1

    # entry_imbalance and positive_after_wait scratch do NOT erase the max_hold strike
    tracker.record_close(
        route="XPL|HL->ASTER", symbol="XPL", buy_id="XPL.HL", sell_id="XPL.ASTER",
        position_id=3, close_reason="entry_imbalance", bilateral=False,
        effective_net=F("-0.02"), economics=econ_loss, now_ns=NOW + 10 * SECOND,
    )
    tracker.record_close(
        route="XPL|HL->ASTER", symbol="XPL", buy_id="XPL.HL", sell_id="XPL.ASTER",
        position_id=4, close_reason="positive_after_wait", bilateral=True,
        effective_net=F("0.002"), economics=econ_loss, now_ns=NOW + 20 * SECOND,
    )
    assert tracker.entries[("XPL.HL", "XPL.ASTER")]["strikes"] == 1

    # take_profit convergence resets strikes
    tracker.record_close(
        route="XPL|HL->ASTER", symbol="XPL", buy_id="XPL.HL", sell_id="XPL.ASTER",
        position_id=5, close_reason="take_profit", bilateral=True,
        effective_net=F("0.02"), economics=econ_loss, now_ns=NOW + 30 * SECOND,
    )
    assert ("XPL.HL", "XPL.ASTER") not in tracker.entries

    # Verify session analyzer renders blocked routes in Top 路由 table
    report = {
        "sources": {},
        "timing": {
            "elapsed_seconds": 10.0,
            "status": "completed",
            "analysis": {
                "funnel": {
                    "pre_gates": {"route_max_hold_block": 5},
                    "totals": {"live_rejected:route_max_hold_block": 5},
                    "by_route": {
                        "XPL|HL->ASTER": {"live_rejected:route_max_hold_block": 5},
                    },
                },
            },
        },
    }
    md = analyzer.render_markdown(report)
    assert "XPL|HL->ASTER" in md
    assert "live_rejected:route_max_hold_block=5" in md


def test_paper_trade_state_max_hold_triggers_route_cooldown_and_expiry():
    basis_policy = BasisFilterPolicy(
        enabled=True, window_secs=600, warmup_secs=60, min_deviation_bps=D("20"),
        max_hold_loss_cooldown_secs=900, max_hold_loss_block_strikes=2, max_hold_loss_block_secs=3600,
    )
    trade_policy = TradePolicy(
        order_notional_usd=D("20"), entry_edge_bps=D("20"),
        take_profit_bps=D("5"), max_loss_bps=D("150"),
        breakeven_after_secs=180, force_close_after_secs=300,
    )
    paper = _paper_state(make_plan(symbols=("XPL",), venues=("HL", "ASTER")), trade_policy, basis_policy)
    for step in range(0, 70, 5):
        t = NOW + step * SECOND
        publish(paper, t, symbol="XPL", buy_bid="99.99", buy_ask="100.00", sell_bid="99.99", sell_ask="100.00")
    t_open = NOW + 75 * SECOND
    publish(paper, t_open, symbol="XPL", buy_bid="99.99", buy_ask="100.00", sell_bid="100.35", sell_ask="100.36")
    assert len(paper.positions) == 1
    t_hold = t_open + 305 * SECOND
    publish(paper, t_hold, symbol="XPL", buy_bid="99.98", buy_ask="100.00", sell_bid="100.43", sell_ask="100.45")
    assert not paper.positions
    assert "XPL|HL->ASTER" in paper.document()["route_max_hold_blocks"]
    # Blocked during cooldown
    publish(paper, t_hold + 60 * SECOND, symbol="XPL", buy_bid="99.99", buy_ask="100.00", sell_bid="100.35", sell_ask="100.36")
    assert not paper.positions
    paper.on_evaluation_timer(t_hold + 3601 * SECOND)


def test_bbo_prescreen_skips_l2_pricing_and_decay_pollution_while_persistent(monkeypatch):
    import opportunity_paper as paper_mod
    basis_policy = BasisFilterPolicy(
        enabled=True, window_secs=600, warmup_secs=60, min_deviation_bps=D("20"),
    )
    trade_policy = TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20"))
    plan = make_plan(symbols=("XPL",), venues=("HL", "ASTER"))

    l2_calls = []
    real_eval = paper_mod.evaluate_opportunity

    def spy_eval(*args, **kwargs):
        l2_calls.append(args[0].symbol)
        return real_eval(*args, **kwargs)

    monkeypatch.setattr(paper_mod, "evaluate_opportunity", spy_eval)

    # 1. LiveTradeState: persistent +35 bps spread never invokes L2 pricing or pollutes decay/exit_liq
    live, _ = state(plan=plan, policy=trade_policy, basis_filter=basis_policy)
    live_collector = analytics_mod.AnalyticsCollector()
    monkeypatch.setattr("opportunity_live.current_analytics", lambda: live_collector)
    monkeypatch.setattr(paper_mod, "current_analytics", lambda: live_collector)
    for step in range(0, 80, 5):
        t = NOW + step * SECOND
        set_book(live, "HL", "99.99", "100.00", t, symbol="XPL")
        set_book(live, "ASTER", "100.35", "100.36", t, symbol="XPL")
        live.evaluate_symbol("XPL", t)
    assert l2_calls == []
    assert live_collector.funnel_totals.get("priced_qualified", 0) == 0
    assert live_collector.funnel_totals.get("live_rejected:basis_warmup", 0) >= 1
    assert live_collector.funnel_totals.get("live_rejected:basis_deviation_insufficient", 0) >= 1
    assert not live_collector._active_decay and not live_collector.decay_completed
    assert not live_collector.exit_liq_by_route

    # Transient spike (+65 bps above ~35 bps baseline) invokes L2 pricing and enters
    t_spike = NOW + 85 * SECOND
    accounts(live, t_spike)
    set_book(live, "HL", "99.99", "100.00", t_spike, symbol="XPL")
    set_book(live, "ASTER", "100.65", "100.66", t_spike, symbol="XPL")
    live.evaluate_symbol("XPL", t_spike)
    assert len(l2_calls) >= 1
    assert len(submissions(live)) == 2
    assert live_collector.funnel_totals.get("priced_qualified", 0) == 1

    # 2. PaperTradeState mirrors the exact same BBO pre-screen skip
    l2_calls.clear()
    paper = _paper_state(plan, trade_policy, basis_policy)
    paper_collector = analytics_mod.AnalyticsCollector()
    monkeypatch.setattr(paper_mod, "current_analytics", lambda: paper_collector)
    for step in range(0, 80, 5):
        t = NOW + step * SECOND
        publish(paper, t, symbol="XPL", buy_bid="99.99", buy_ask="100.00", sell_bid="100.35", sell_ask="100.36")
    assert l2_calls == []
    assert paper_collector.funnel_totals.get("pricing_rejected:basis_warmup", 0) >= 1
    assert paper_collector.funnel_totals.get("pricing_rejected:basis_deviation_insufficient", 0) >= 1
    assert paper_collector.funnel_totals.get("priced_qualified", 0) == 0

    publish(paper, t_spike, symbol="XPL", buy_bid="99.99", buy_ask="100.00", sell_bid="100.65", sell_ask="100.66")
    assert len(l2_calls) >= 1
    assert len(paper.positions) == 1
    assert paper_collector.funnel_totals.get("priced_qualified", 0) == 1


def test_bbo_prescreen_boundary_conservativeness():
    policy = BasisFilterPolicy(enabled=True, window_secs=300, warmup_secs=60, min_deviation_bps=D("20"))
    tracker = RollingBasisTracker(policy)
    settings = ScanSettings(max_age_ms=2000, max_skew_ms=500, max_receive_age_ms=2000)

    for step in range(0, 70, 5):
        t = NOW + step * SECOND
        tracker.observe_pair(
            "ADA.HL", _book("HL", "ADA", "99.99", "100.01", t),
            "ADA.ASTER", _book("ASTER", "ADA", "99.99", "100.01", t),
            settings, t,
        )
    t_eval = NOW + 70 * SECOND
    # Exact 20.0 bps BBO boundary above ~0 bps baseline must NOT be falsely dropped by pre-screen
    assert tracker.bbo_rule_out(
        "ADA.HL", "ADA.ASTER",
        _book("HL", "ADA", "99.99", "100.00", t_eval),
        _book("ASTER", "ADA", "100.20", "100.21", t_eval),
        t_eval,
    ) is None
    # Clearly below threshold (+15 bps < +20 bps) IS ruled out conservatively at BBO
    assert tracker.bbo_rule_out(
        "ADA.HL", "ADA.ASTER",
        _book("HL", "ADA", "99.99", "100.00", t_eval),
        _book("ASTER", "ADA", "100.15", "100.16", t_eval),
        t_eval,
    ) == "basis_deviation_insufficient"


def test_observe_symbol_fast_skip_and_partial_venue_safety(monkeypatch):
    policy = BasisFilterPolicy(enabled=True, window_secs=300, warmup_secs=60, min_deviation_bps=D("20"))
    tracker = RollingBasisTracker(policy)  # sample_interval_secs = 2
    settings = ScanSettings(max_age_ms=5000, max_skew_ms=1000, max_receive_age_ms=5000)
    pairs = (("XPL.ASTER", "XPL.BP"), ("XPL.ASTER", "XPL.HL"), ("XPL.BP", "XPL.HL"))

    pair_calls = []
    real_observe_pair = tracker.observe_pair

    def spy_observe_pair(id_a, book_a, id_b, book_b, s, now_ns):
        pair_calls.append((id_a, id_b, now_ns))
        return real_observe_pair(id_a, book_a, id_b, book_b, s, now_ns)

    monkeypatch.setattr(tracker, "observe_pair", spy_observe_pair)

    # Initially only 2 of 3 venues have books -> fast-skip must NOT engage while third venue is missing
    books = {
        "XPL.ASTER": _book("ASTER", "XPL", "99.99", "100.01", NOW),
        "XPL.HL": _book("HL", "XPL", "99.99", "100.01", NOW),
    }
    assert tracker.observe_symbol(pairs, books, settings, NOW, symbol="XPL") == 1
    assert len(pair_calls) == 1

    # Third venue arrives 1s later -> NOT fast-skipped; observes the 2 newly complete pairs
    t1 = NOW + SECOND
    books["XPL.BP"] = _book("BP", "XPL", "99.99", "100.01", t1)
    assert tracker.observe_symbol(pairs, books, settings, t1, affected={"XPL.BP"}, symbol="XPL") == 2
    assert len(pair_calls) == 3

    # Now all 3 pairs are tracked (next due: t=2s for ASTER-HL, t=3s for BP pairs)
    # Calls before t=2s must fast-skip in O(1) without calling observe_pair at all
    pair_calls.clear()
    t1_5 = NOW + int(1.5 * SECOND)
    assert tracker.observe_symbol(pairs, books, settings, t1_5, symbol="XPL") == 0
    assert pair_calls == []

    # At t=2s (when only ASTER-HL is due), only ASTER-HL is observed; BP pairs remain skipped
    t2 = NOW + 2 * SECOND
    books["XPL.ASTER"] = _book("ASTER", "XPL", "99.99", "100.01", t2)
    books["XPL.HL"] = _book("HL", "XPL", "99.99", "100.01", t2)
    assert tracker.observe_symbol(pairs, books, settings, t2, symbol="XPL") == 1
    assert pair_calls == [("XPL.ASTER", "XPL.HL", t2)]

    # At t=2.5s (< t=3s for BP pairs), fast-skip engages again
    pair_calls.clear()
    assert tracker.observe_symbol(pairs, books, settings, NOW + int(2.5 * SECOND), symbol="XPL") == 0
    assert pair_calls == []

    # At t=3s, the 2 BP pairs become due and are observed
    t3 = NOW + 3 * SECOND
    books["XPL.BP"] = _book("BP", "XPL", "99.99", "100.01", t3)
    assert tracker.observe_symbol(pairs, books, settings, t3, symbol="XPL") == 2
    assert len(pair_calls) == 2

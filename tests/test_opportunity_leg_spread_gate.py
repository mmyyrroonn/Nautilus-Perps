"""Tests for instantaneous & rolling per-leg bid-ask spread entry gates."""
from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import opportunity_timing as timing
from opportunity_basis import RollingLegSpreadTracker
from opportunity_core import BookSnapshot, ScanSettings
from opportunity_scan import ScanConfigError
from opportunity_trade import TradePlan, TradePolicy
from test_opportunity_live import accounts, state, submissions
from test_opportunity_trade import NOW, SECOND, make_plan, make_state, publish, set_book


def test_trade_policy_spread_gate_validation_and_legacy_fingerprint(tmp_path):
    with pytest.raises(ScanConfigError):
        TradePolicy(max_leg_spread_bps=D("0"))
    with pytest.raises(ScanConfigError):
        TradePolicy(max_leg_spread_bps=D("10000"))
    with pytest.raises(ScanConfigError):
        TradePolicy(max_rolling_leg_spread_bps=D("-1"))
    with pytest.raises(ScanConfigError):
        TradePolicy(max_rolling_leg_spread_bps=D("10000"))
    with pytest.raises(ScanConfigError):
        TradePolicy(rolling_leg_spread_window_secs=9)
    with pytest.raises(ScanConfigError):
        TradePolicy(rolling_leg_spread_min_samples=0)
    with pytest.raises(ScanConfigError):
        TradePolicy(rolling_leg_spread_window_secs=20, rolling_leg_spread_min_samples=21)

    scan = make_plan()
    legacy = TradePlan(scan, TradePolicy(), tmp_path / "a.sqlite3")
    explicit = TradePlan(
        scan,
        TradePolicy(
            max_leg_spread_bps=None,
            max_rolling_leg_spread_bps=None,
            rolling_leg_spread_window_secs=300,
            rolling_leg_spread_min_samples=10,
        ),
        tmp_path / "b.sqlite3",
    )
    assert legacy.fingerprint() == explicit.fingerprint()

    with_inst = TradePlan(scan, TradePolicy(max_leg_spread_bps=D("25")), tmp_path / "c.sqlite3")
    with_roll = TradePlan(scan, TradePolicy(max_rolling_leg_spread_bps=D("20")), tmp_path / "d.sqlite3")
    assert with_inst.fingerprint() != legacy.fingerprint()
    assert with_roll.fingerprint() != legacy.fingerprint()


def test_rolling_leg_spread_tracker_1hz_eviction_warmup_and_invalid_books():
    tracker = RollingLegSpreadTracker(window_secs=10, min_samples=4, sample_interval_secs=1)
    settings = ScanSettings()
    iid = "BTC.HL"

    # Many updates inside 1 second only record 1 sample -> bursts cannot bypass warmup/median
    for k in range(10):
        t = NOW + k * 50_000_000
        book = BookSnapshot("HL", "BTC", ((D("100"), D("5")),), ((D("100.02"), D("5")),), t, t, valid=True)
        tracker.observe_instruments((iid,), {iid: book}, settings, t)
    assert tracker.summary()["tracked_instruments"] == 1
    assert tracker.summary()["ready_instruments"] == 0
    assert tracker.median_bps(iid, NOW + 450_000_000) is None

    # Stale / crossed / empty books are ignored cleanly
    bad_t = NOW + 2 * SECOND
    stale = BookSnapshot("HL", "BTC", ((D("100"), D("5")),), ((D("100.02"), D("5")),), NOW, NOW, valid=True)
    crossed = BookSnapshot("HL", "BTC", ((D("100.1"), D("5")),), ((D("100"), D("5")),), bad_t, bad_t, valid=True)
    assert tracker.observe_instruments((iid,), {iid: stale}, settings, bad_t + 3 * SECOND) == 0
    assert tracker.observe_instruments((iid,), {iid: crossed}, settings, bad_t) == 0

    # Feed 1 Hz valid samples: spreads [2, 50, 50, 50] bps -> median 50 bps once min_samples=4 reached
    for sec in range(1, 4):
        t = NOW + sec * SECOND
        book = BookSnapshot("HL", "BTC", ((D("100"), D("5")),), ((D("100.50"), D("5")),), t, t, valid=True)
        assert tracker.observe_instruments((iid,), {iid: book}, settings, t) == 1
    med = tracker.median_bps(iid, NOW + 3 * SECOND)
    assert med is not None and med == pytest.approx(50.0, rel=1e-6)
    assert tracker.summary()["ready_instruments"] == 1

    # Advancing beyond window_secs evicts early samples and returns None again (< min_samples)
    assert tracker.median_bps(iid, NOW + 13 * SECOND) is None


def test_paper_entry_instantaneous_leg_spread_wide_rejects_and_admits(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    pol = TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("10"), max_leg_spread_bps=D("25"))
    st, _ = make_state(make_plan(), pol)

    # Buy leg HL has 26 bps own spread (> 25 bps), even with 30 bps cross edge -> blocked
    publish(st, NOW, buy_bid="100.00", buy_ask="100.26", sell_bid="100.60", sell_ask="100.65")
    assert not st.positions
    assert "leg_spread_wide" in str(recorder.document())

    # Tightening buy leg to 10 bps own spread -> opens cleanly
    t1 = NOW + SECOND
    publish(st, t1, buy_bid="100.00", buy_ask="100.10", sell_bid="100.40", sell_ask="100.45")
    assert len(st.positions) == 1


def test_paper_rolling_leg_spread_blocks_wide_pinch_and_admits_healthy_spike(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    pol = TradePolicy(
        order_notional_usd=D("20"),
        entry_edge_bps=D("10"),
        max_loss_bps=D("150"),
        max_leg_spread_bps=D("25"),
        max_rolling_leg_spread_bps=D("20"),
        rolling_leg_spread_window_secs=60,
        rolling_leg_spread_min_samples=5,
    )
    # Warmup fails closed even when cross edge and instantaneous spreads look good
    st_warm, _ = make_state(make_plan(), pol)
    publish(st_warm, NOW, buy_bid="100.00", buy_ask="100.10", sell_bid="100.40", sell_ask="100.45")
    assert not st_warm.positions
    assert "rolling_leg_spread_warmup" in str(recorder.document())

    # Case A: XTEN-style wide baseline (50 bps) momentarily pinches to 8 bps -> blocked
    st, _ = make_state(make_plan(), pol)
    for sec in range(5):
        t = NOW + sec * SECOND
        publish(st, t, buy_bid="100.00", buy_ask="100.50", sell_bid="100.40", sell_ask="100.42")
        assert not st.positions

    # Now momentary 1-tick pinch on HL down to 8 bps with huge 32 bps cross edge -> rolling median still 50 > 20 -> blocked!
    t_pinch = NOW + 5 * SECOND
    publish(st, t_pinch, buy_bid="100.00", buy_ask="100.08", sell_bid="100.40", sell_ask="100.42")
    assert not st.positions
    assert "rolling_leg_spread_wide" in str(recorder.document())

    # Case B: Healthy tight coin (~5 bps rolling median) has one transient 22 bps spike (<= 25 inst cap) -> admitted!
    st_ok, _ = make_state(make_plan(), pol)
    for sec in range(5):
        t = NOW + sec * SECOND
        publish(st_ok, t, buy_bid="100.00", buy_ask="100.05", sell_bid="100.04", sell_ask="100.08")
        assert not st_ok.positions
    t_ok = NOW + 5 * SECOND
    publish(st_ok, t_ok, buy_bid="100.00", buy_ask="100.22", sell_bid="100.50", sell_ask="100.55")
    assert len(st_ok.positions) == 1


def test_live_rolling_leg_spread_preactive_warmup_blocks_pinch_and_opens_tight():
    pol = TradePolicy(
        order_notional_usd=D("20"),
        entry_edge_bps=D("10"),
        max_leg_spread_bps=D("25"),
        max_rolling_leg_spread_bps=D("20"),
        rolling_leg_spread_window_secs=60,
        rolling_leg_spread_min_samples=4,
    )
    # 1. Pre-active warmup collects wide history cleanly while inactive
    live_wide, _ = state(policy=pol, activate=False)
    for sec in range(4):
        t = NOW + sec * SECOND
        set_book(live_wide, "HL", "100.00", "100.50", t)
        set_book(live_wide, "ASTER", "100.40", "100.42", t)
        live_wide.evaluate_symbol("BTC", t)
    doc = live_wide.document()
    assert doc["spread_tracker"] == {
        "window_secs": 60,
        "min_samples": 4,
        "tracked_instruments": 2,
        "ready_instruments": 2,
    }
    t_act = NOW + 4 * SECOND
    accounts(live_wide, t_act)
    assert live_wide.activate(t_act)
    live_wide.drain_intents()
    # Momentary pinch on HL down to 8 bps is still rejected because rolling median is 50 bps (> 20)
    set_book(live_wide, "HL", "100.00", "100.08", t_act)
    set_book(live_wide, "ASTER", "100.40", "100.42", t_act)
    live_wide.evaluate_symbol("BTC", t_act)
    assert not submissions(live_wide)
    assert not live_wide.positions

    # 2. Healthy tight rolling baseline (5 bps) admits entry with transient 22 bps spike <= 25 bps inst cap
    live_tight, _ = state(policy=pol, activate=False)
    for sec in range(4):
        t = NOW + sec * SECOND
        set_book(live_tight, "HL", "100.00", "100.05", t)
        set_book(live_tight, "ASTER", "100.04", "100.08", t)
        live_tight.evaluate_symbol("BTC", t)
    accounts(live_tight, t_act)
    assert live_tight.activate(t_act)
    live_tight.drain_intents()
    set_book(live_tight, "HL", "100.00", "100.22", t_act)
    set_book(live_tight, "ASTER", "100.50", "100.55", t_act)
    live_tight.evaluate_symbol("BTC", t_act)
    assert len(submissions(live_tight)) == 2
    assert len(live_tight.positions) == 1

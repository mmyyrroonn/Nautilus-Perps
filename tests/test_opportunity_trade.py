"""Offline paper execution policy, durable pair ownership and CLI boundaries."""
from __future__ import annotations

import builtins
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
import json
from pathlib import Path
import socket
import sqlite3
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opportunity_core import BookSnapshot, MarketMetadata, ScanSettings
from opportunity_paper import PaperTradeState
from opportunity_scan import MarketPlan, ScanConfigError, ScanPlan
from opportunity_trade import TradePlan, TradePolicy, load_trade_plan, main
from opportunity_trade_store import TradeStore

NOW = 1_800_000_000_000_000_000
SECOND = 1_000_000_000


def make_plan(symbols=("BTC",), venues=("HL", "ASTER"), reserve="0"):
    markets = tuple(MarketPlan(symbol, venue, f"{symbol}.{venue}", D(1), "synthetic parity")
                    for symbol in symbols for venue in venues)
    return ScanPlan(markets, ScanSettings(reserve_bps=D(reserve)), None,
                    evaluation_interval_ms=100)


def make_state(plan=None, policy=None, *, store=None, fees=None, metadata=None):
    events = []
    state = PaperTradeState(plan or make_plan(), policy or TradePolicy(),
                            store=store, event_sink=events.append)
    for market in state.plan.markets:
        value = (metadata or {}).get(market.instrument_id,
            MarketMetadata(market.instrument_id, D("0.01"), taker_fee_bps=D(
                (fees or {}).get(market.venue, "0")), fee_source="synthetic fee"))
        state.update_metadata(market.instrument_id, value, NOW, evaluate=False)
    return state, events


def set_book(state, venue, bid, ask, now=NOW, *, symbol="BTC", bid_size="10", ask_size="10"):
    state.update_book(f"{symbol}.{venue}", BookSnapshot(venue, symbol,
        ((D(bid), D(bid_size)),), ((D(ask), D(ask_size)),), now, now), now, evaluate=False)


def publish(state, now=NOW, *, symbol="BTC", buy_bid="99.99", buy_ask="100",
            sell_bid="100.3", sell_ask="100.31", sell_ask_size="10"):
    set_book(state, "HL", buy_bid, buy_ask, now, symbol=symbol)
    set_book(state, "ASTER", sell_bid, sell_ask, now, symbol=symbol, ask_size=sell_ask_size)
    state.evaluate_symbol(symbol, now)


def mark_bps(state, bps, seconds=1):
    # Opening spread is 0.30, and the opening buy is 100 per base unit.
    cover = D("100.29") - D(str(bps)) / 100
    now = NOW + seconds * SECOND
    publish(state, now, sell_bid=cover-D("0.01"), sell_ask=cover)
    return now


@pytest.mark.parametrize("bid,opens", [("100.319999999999999999", False), ("100.32", True)])
def test_entry_uses_net_after_opening_fees_and_reserve_exactly(bid, opens):
    state, events = make_state(make_plan(reserve="1"), fees={"HL": "1"})
    publish(state, sell_bid=bid, sell_ask=D(bid)+D("0.01"))
    assert bool(state.positions) is opens
    assert sum(e["event"] == "opened" for e in events) == int(opens)
    if opens:
        entry = next(iter(state.positions.values()))["entry"]
        assert F(entry["economics"]["entry_after_fees_and_reserve_bps"]) == 30


@pytest.mark.parametrize("invalid", ["stale", "skew", "crossed", "missing_fee", "shallow_entry"])
def test_unusable_entry_data_never_opens(invalid):
    state, events = make_state()
    set_book(state, "HL", "99.99", "100")
    set_book(state, "ASTER", "100.3", "100.31")
    now = NOW
    if invalid == "stale":
        now += 3*SECOND
    elif invalid == "skew":
        now += SECOND
        set_book(state, "ASTER", "100.3", "100.31", now)
    elif invalid == "crossed":
        set_book(state, "HL", "100.01", "100")
    elif invalid == "missing_fee":
        state.update_metadata("BTC.HL", replace(state.metadata["BTC.HL"], taker_fee_bps=None),
                              now, evaluate=False)
    else:
        set_book(state, "ASTER", "100.3", "100.31", bid_size="0.01")
    state.evaluate_symbol("BTC", now)
    assert not state.positions and not events


def test_each_leg_respects_cap_and_native_lots_share_canonical_quantity():
    metadata = {
        "BTC.HL": MarketMetadata("BTC.HL", D("0.01"), D(1), D(0), "synthetic"),
        "BTC.ASTER": MarketMetadata("BTC.ASTER", D("0.002"), D(10), D(0), "synthetic"),
    }
    state, _ = make_state(metadata=metadata)
    publish(state)
    entry = next(iter(state.positions.values()))["entry"]
    sizing, fills = entry["sizing"], entry["paper_fills"]
    assert F(sizing["buy_quantity"]) == F(sizing["sell_quantity"]) * 10
    assert F(sizing["base_quantity"]) == F("0.98")
    assert all(F(fills[key]) <= 100 for key in ("buy_notional_usd", "sell_notional_usd"))


@pytest.mark.parametrize("bps,closes", [("4.999999999999999999", False), ("5", True)])
def test_take_profit_exact_boundary_uses_opening_buy_capital(bps, closes):
    state, events = make_state()
    publish(state)
    mark_bps(state, bps)
    assert (not state.positions) is closes
    if closes:
        closed = events[-1]
        assert closed["reason"] == "take_profit"
        assert F(closed["mark"]["net_bps"]) == 5
        assert F(closed["mark"]["capital_usd"]) == 99
        assert F(closed["mark"]["net_usd"]) == F("0.0495")


@pytest.mark.parametrize("bps,closes", [("-19.999999999999999999", False), ("-20", True)])
def test_max_loss_exact_boundary(bps, closes):
    state, events = make_state()
    publish(state)
    mark_bps(state, bps)
    assert (not state.positions) is closes
    if closes:
        assert events[-1]["reason"] == "max_loss"
        assert F(events[-1]["mark"]["net_bps"]) == -20


@pytest.mark.parametrize("seconds,bps,closes", [(179, "1", False), (180, "0", False),
    (180, "-0.0000001", False), (180, "0.0000001", True), (180, "4", True)])
def test_three_minute_wait_requires_strict_positive_net(seconds, bps, closes):
    state, events = make_state()
    publish(state)
    mark_bps(state, bps, seconds)
    assert (not state.positions) is closes
    if closes:
        assert events[-1]["reason"] == "positive_after_wait"


def test_five_minute_forces_close_even_when_loss_is_below_stop():
    state, events = make_state()
    publish(state)
    mark_bps(state, "-1", 299)
    assert len(state.positions) == 1
    mark_bps(state, "-1", 300)
    assert not state.positions
    assert events[-1]["reason"] == "max_hold"
    assert events[-1]["age_ms"] == 300000


@pytest.mark.parametrize("unavailable", ["disconnect", "stale", "depth"])
def test_deadline_requests_exit_keeps_lock_until_full_fresh_exit(unavailable):
    state, events = make_state()
    publish(state)
    position = next(iter(state.positions.values()))
    if unavailable == "disconnect":
        state.invalidate("BTC.HL", NOW + SECOND)
    elif unavailable == "depth":
        publish(state, NOW + 300*SECOND, sell_ask_size="0.01")
    state.on_evaluation_timer(NOW + 300*SECOND)
    assert len(state.positions) == 1
    assert position["status"] == "exit_pending"
    assert position["exit_reason"] == "max_hold"
    assert state.document()["closed_pairs"] == 0
    # A qualifying same-pair signal cannot release or duplicate the pending pair.
    state.on_evaluation_timer(NOW + 301*SECOND)
    assert state.document()["opened_pairs"] == 1
    assert sum(e["event"] == "exit_blocked" for e in events) == 1
    publish(state, NOW + 302*SECOND)
    assert not state.positions
    closed = events[-1]
    assert closed["event"] == "closed" and closed["reason"] == "max_hold"
    assert closed["exit_requested_ns"] == NOW + 300*SECOND
    assert closed["observed_ns"] == NOW + 302*SECOND
    assert state.document()["opened_pairs"] == 1


def test_round_trip_uses_four_cash_fees_and_adverse_slippage():
    state, _ = make_state(policy=TradePolicy(slippage_bps=D(2)),
                          fees={"HL": "1", "ASTER": "2"})
    publish(state, sell_bid="100.5", sell_ask="100.51")
    position = next(iter(state.positions.values()))
    q = F(position["entry"]["sizing"]["base_quantity"])
    now = NOW + SECOND
    publish(state, now, buy_bid="99.99", sell_bid="100.49", sell_ask="100.50")
    mark = state.measure(position, now)
    buy, sell = q * 100 * F("1.0002"), q * F("100.5") * F("0.9998")
    sale, cover = q * F("99.99") * F("0.9998"), q * F("100.50") * F("1.0002")
    fees = (buy+sale) / 10000 + (sell+cover)*2/10000
    assert F(mark["round_trip_fees_usd"]) == fees
    assert F(mark["net_usd"]) == sell-buy+sale-cover-fees
    assert F(mark["capital_usd"]) == buy
    assert max(buy, sell) <= 100


@pytest.mark.parametrize("sell_bid,opens", [("100.3", False), ("100.5", True)])
def test_extra_slippage_must_still_leave_net_thirty_bps(sell_bid, opens):
    state, _ = make_state(policy=TradePolicy(slippage_bps=D(2)))
    publish(state, sell_bid=sell_bid, sell_ask=D(sell_bid)+D("0.01"))
    assert bool(state.positions) is opens
    if opens:
        entry = next(iter(state.positions.values()))["entry"]
        fills = entry["paper_fills"]
        net = F(fills["sell_notional_usd"])-F(fills["buy_notional_usd"])
        assert net * 10000 >= 30 * F(fills["buy_notional_usd"])


@pytest.mark.parametrize("bps,seconds,pending", [(5, 1, False), (1, 180, False), (-20, 1, True)])
def test_shallow_exit_cannot_claim_profit_but_can_request_loss_stop(bps, seconds, pending):
    state, events = make_state()
    publish(state)
    cover = D("100.29")-D(bps)/100
    publish(state, NOW+seconds*SECOND, sell_bid=cover-D("0.01"), sell_ask=cover,
            sell_ask_size="0.01")
    assert len(state.positions) == 1 and state.document()["closed_pairs"] == 0
    position = next(iter(state.positions.values()))
    assert position["status"] == ("exit_pending" if pending else "open")
    if pending:
        assert position["exit_reason"] == "max_loss"
        mark = [e for e in events if e["event"] == "exit_requested"][-1]["mark"]
        assert mark["executable"] is False and F(mark["net_bps"]) == -20
        # A hard stop stays requested even after the market recovers.
        mark_bps(state, 1, seconds+1)
        assert not state.positions and events[-1]["reason"] == "max_loss"


def test_one_shared_entry_snapshot_cannot_fill_two_pairs_from_same_shallow_ask():
    state, _ = make_state(make_plan(venues=("HL", "ASTER", "LIGHTER")))
    set_book(state, "HL", "99.99", "100", ask_size="1")
    set_book(state, "ASTER", "100.3", "100.31")
    set_book(state, "LIGHTER", "100.4", "100.41")
    state.evaluate_symbol("BTC", NOW)
    state.evaluate_symbol("BTC", NOW)
    assert len(state.positions) == 1
    assert F(state.document()["exposure"]["BTC.HL"]["long_canonical_quantity"]) <= 1
    # New public snapshots replenish liquidity for the other independent pair.
    set_book(state, "HL", "99.99", "100", NOW+SECOND, ask_size="1")
    set_book(state, "ASTER", "100.3", "100.31", NOW+SECOND)
    set_book(state, "LIGHTER", "100.4", "100.41", NOW+SECOND)
    state.evaluate_symbol("BTC", NOW+SECOND)
    assert len(state.positions) == 2


def test_shared_exit_snapshot_cannot_reuse_bid_depth_for_two_forced_exits():
    state, events = make_state(make_plan(venues=("HL", "ASTER", "LIGHTER")))
    publish(state)
    set_book(state, "LIGHTER", "100.4", "100.41")
    state.evaluate_symbol("BTC", NOW)
    assert len(state.positions) == 2
    deadline = NOW+300*SECOND
    set_book(state, "HL", "99.99", "100", deadline, bid_size="1.5")
    set_book(state, "ASTER", "100.3", "100.31", deadline)
    set_book(state, "LIGHTER", "100.4", "100.41", deadline)
    state.on_evaluation_timer(deadline)
    assert state.document()["closed_pairs"] == 1
    assert len(state.positions) == 1
    assert next(iter(state.positions.values()))["status"] == "exit_pending"
    state.on_evaluation_timer(deadline)
    assert state.document()["closed_pairs"] == 1
    fresh = deadline+SECOND
    set_book(state, "HL", "99.99", "100", fresh, bid_size="1.5")
    set_book(state, "ASTER", "100.3", "100.31", fresh)
    set_book(state, "LIGHTER", "100.4", "100.41", fresh)
    state.on_evaluation_timer(fresh)
    assert not state.positions and state.document()["closed_pairs"] == 2
    assert all(e["mark"]["executable"] for e in events if e["event"] == "closed")


def test_l2_entry_and_exit_walk_depth_without_partial_fills():
    state, _ = make_state()
    state.update_book("BTC.HL", BookSnapshot("HL", "BTC",
        ((D("99.99"), D("0.5")), (D("99.98"), D(10))),
        ((D(100), D("0.5")), (D("100.01"), D(10))), NOW, NOW), NOW, evaluate=False)
    set_book(state, "ASTER", "100.4", "100.41")
    state.evaluate_symbol("BTC", NOW)
    position = next(iter(state.positions.values()))
    entry = position["entry"]
    q = F(entry["sizing"]["base_quantity"])
    assert q == F("0.99")
    assert F(entry["paper_fills"]["buy_notional_usd"]) == F("50") + F("0.49")*F("100.01")
    mark = state.measure(position, NOW)
    assert F(mark["long_sale_usd"]) == F("0.5")*F("99.99") + F("0.49")*F("99.98")
    set_book(state, "ASTER", "100.4", "100.41", NOW+SECOND, ask_size="0.98")
    set_book(state, "HL", "99.99", "100", NOW+SECOND)
    shallow_mark = state.measure(position, NOW+SECOND)
    assert shallow_mark is None or shallow_mark["executable"] is False
    assert len(state.positions) == 1


def test_duplicate_and_reverse_signals_share_one_unordered_pair_lock():
    # High TP/loss thresholds keep the original pair alive during reversal.
    state, _ = make_state(policy=TradePolicy(take_profit_bps=D(1000), max_loss_bps=D(1000)))
    publish(state)
    original = next(iter(state.positions.values()))
    publish(state, NOW+SECOND)
    publish(state, NOW+2*SECOND, buy_bid="100.8", buy_ask="100.81",
            sell_bid="100", sell_ask="100.01")
    assert len(state.positions) == 1
    assert next(iter(state.positions.values()))["position_id"] == original["position_id"]
    assert state.document()["opened_pairs"] == 1


def test_other_symbols_pairs_and_shared_leg_quantities_can_coexist():
    state, events = make_state(make_plan(symbols=("BTC", "ETH"), venues=("HL", "ASTER", "LIGHTER")))
    publish(state)
    set_book(state, "LIGHTER", "100.4", "100.41")
    state.evaluate_symbol("BTC", NOW)
    publish(state, symbol="ETH")
    assert len(state.positions) == 3
    exposure = state.document()["exposure"]
    assert F(exposure["BTC.HL"]["long_canonical_quantity"]) == F("1.98")
    shared_position = state.positions[("BTC", "HL", "LIGHTER")]
    # Close HL/ASTER while HL/LIGHTER stays independently owned.
    mark_bps(state, 5)
    assert len(state.positions) == 2
    assert state.positions[("BTC", "HL", "LIGHTER")]["position_id"] == shared_position["position_id"]
    assert F(state.document()["exposure"]["BTC.HL"]["long_canonical_quantity"]) == F("0.99")
    assert events[-1]["base_quantity"] == "0.99"


def test_reentry_needs_new_observation_on_both_legs_after_close():
    state, _ = make_state()
    publish(state)
    now = mark_bps(state, 5)
    state.evaluate_symbol("BTC", now)
    state.on_evaluation_timer(now+1)
    assert state.document()["opened_pairs"] == 1
    set_book(state, "ASTER", "100.3", "100.31", now+SECOND)
    state.evaluate_symbol("BTC", now+SECOND)
    assert not state.positions
    publish(state, now+2*SECOND)
    assert state.document()["opened_pairs"] == 2 and len(state.positions) == 1


@pytest.mark.parametrize("pending", [False, True])
def test_sqlite_restart_restores_original_deadline_quantity_and_pair_lock(tmp_path, pending):
    plan, policy = make_plan(), TradePolicy()
    path = tmp_path / "paper.sqlite3"
    fingerprint = TradePlan(plan, policy, path).fingerprint()
    store = TradeStore(path, fingerprint)
    state, _ = make_state(plan, policy, store=store)
    publish(state)
    original = json.loads(json.dumps(next(iter(state.positions.values()))))
    if pending:
        state.on_evaluation_timer(NOW+300*SECOND)
    store.close()
    store = TradeStore(path, fingerprint)
    try:
        resumed, events = make_state(plan, policy, store=store)
        position = next(iter(resumed.positions.values()))
        assert position["opened_ns"] == NOW
        assert position["entry"]["sizing"] == original["entry"]["sizing"]
        assert position["status"] == ("exit_pending" if pending else "open")
        publish(resumed, NOW+301*SECOND)
        assert not resumed.positions
        assert resumed.document()["opened_pairs"] == 1
        assert events[-1]["opened_ns"] == NOW and events[-1]["reason"] == "max_hold"
        assert store.load()["positions"] == []
        records = store.connection.execute("SELECT kind FROM events ORDER BY sequence").fetchall()
        assert sum(kind == "opened" for (kind,) in records) == 1
        assert sum(kind == "closed" for (kind,) in records) == 1
    finally:
        store.close()


def test_second_process_cannot_own_same_state_and_can_after_release(tmp_path):
    path = tmp_path / "paper.sqlite3"
    script = """import sys
sys.path.insert(0, sys.argv[1])
from opportunity_trade_store import TradeStore
try:
    store = TradeStore(sys.argv[2], 'synthetic')
except OSError:
    raise SystemExit(7)
store.close()
"""
    owner = TradeStore(path, "synthetic")
    try:
        second = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT / "src"), str(path)],
                                capture_output=True, text=True, timeout=15)
        assert second.returncode == 7, second.stderr
    finally:
        owner.close()
    later = subprocess.run([sys.executable, "-B", "-c", script, str(ROOT / "src"), str(path)],
                           capture_output=True, text=True, timeout=15)
    assert later.returncode == 0, later.stderr


def test_restart_rejects_changed_policy_without_altering_saved_state(tmp_path):
    path = tmp_path / "paper.sqlite3"
    owner = TradeStore(path, "original")
    owner.save({"positions": [{"position_id": 1}]}, [])
    owner.close()
    with pytest.raises(ValueError, match="different policy"):
        TradeStore(path, "changed")
    owner = TradeStore(path, "original")
    try:
        assert owner.load() == {"positions": [{"position_id": 1}]}
    finally:
        owner.close()


@pytest.mark.parametrize("reason,trigger_bps,trigger_second", [
    ("take_profit", 5, 1), ("positive_after_wait", 1, 180)])
def test_restart_rechecks_soft_exit_profit_before_filling(tmp_path, reason, trigger_bps, trigger_second):
    plan, policy = make_plan(), TradePolicy()
    path = tmp_path / "paper.sqlite3"
    fingerprint = TradePlan(plan, policy, path).fingerprint()
    owner = TradeStore(path, fingerprint)
    state, _ = make_state(plan, policy, store=owner)
    publish(state)
    def crash_after_durable_request(event):
        if event["event"] == "exit_requested":
            raise RuntimeError("synthetic shutdown after exit request was saved")
    state.event_sink = crash_after_durable_request
    try:
        with pytest.raises(RuntimeError, match="synthetic shutdown"):
            mark_bps(state, trigger_bps, trigger_second)
        assert owner.load()["positions"][0]["exit_reason"] == reason
    finally:
        owner.close()
    owner = TradeStore(path, fingerprint)
    try:
        resumed, events = make_state(plan, policy, store=owner)
        mark_bps(resumed, -1, trigger_second+1)
        assert len(resumed.positions) == 1 and resumed.document()["closed_pairs"] == 0
        assert next(iter(resumed.positions.values()))["status"] == "open"
        assert any(e["event"] == "exit_deferred" for e in events)
        mark_bps(resumed, 5, trigger_second+2)
        assert not resumed.positions and events[-1]["reason"] == "take_profit"
        assert resumed.document()["opened_pairs"] == 1
    finally:
        owner.close()


@pytest.mark.parametrize("field,value", [
    ("order_notional_usd", 0), ("entry_edge_bps", "NaN"), ("take_profit_bps", "Infinity"),
    ("max_loss_bps", -1), ("max_loss_bps", True), ("take_profit_bps", 0.5),
    ("slippage_bps", -1), ("slippage_bps", 10000), ("breakeven_after_secs", True),
    ("force_close_after_secs", 1.5), ("force_close_after_secs", 180),
    ("breakeven_after_secs", 300), ("force_close_after_secs", 86401),
    ("loss_stop_enabled", "false"), ("loss_stop_enabled", 1),
])
def test_invalid_policy_fails_before_state_creation(field, value):
    with pytest.raises(ScanConfigError):
        TradePolicy(**{field: value})


@pytest.mark.parametrize("loss_bps", [-20, -50])
def test_disabling_loss_stop_retains_position_until_five_minute_reduce(loss_bps):
    state, events = make_state(policy=TradePolicy(loss_stop_enabled=False))
    publish(state)
    mark_bps(state, loss_bps)
    assert len(state.positions) == 1
    assert next(iter(state.positions.values()))["status"] == "open"
    assert not any(e["event"] == "exit_requested" for e in events)
    mark_bps(state, loss_bps, 300)
    assert not state.positions and events[-1]["reason"] == "max_hold"


def test_forced_close_reduces_live_leg_when_other_venue_is_disconnected():
    state, events = make_state()
    publish(state)
    quantity = F(next(iter(state.positions.values()))["entry"]["sizing"]["base_quantity"])
    state.invalidate("BTC.HL", NOW+SECOND)
    deadline = NOW+300*SECOND
    set_book(state, "ASTER", "100.3", "100.31", deadline)
    state.on_evaluation_timer(deadline)
    position = next(iter(state.positions.values()))
    assert position["status"] == "exit_pending"
    assert F(position["remaining_buy_base"]) == quantity
    assert F(position["remaining_sell_base"]) == 0
    reduction = [e for e in events if e["event"] == "partial_reduction"][-1]
    assert [f["instrument_id"] for f in reduction["fills"]] == ["BTC.ASTER"]
    assert reduction["fills"][0]["side"] == "BUY"
    assert F(reduction["fills"][0]["base_quantity"]) == quantity
    # Retry the identical snapshot cannot cover the already-flat leg again.
    previous_reductions = len([e for e in events if e["event"] == "partial_reduction"])
    state.on_evaluation_timer(deadline)
    assert len([e for e in events if e["event"] == "partial_reduction"]) == previous_reductions
    set_book(state, "HL", "99.99", "100", deadline+SECOND)
    state.on_evaluation_timer(deadline+SECOND)
    assert not state.positions and events[-1]["reason"] == "max_hold"
    assert [f["instrument_id"] for f in events[-1]["fills"]] == ["BTC.HL"]


def test_restart_restores_partial_residuals_and_accumulated_cash(tmp_path):
    plan, policy = make_plan(), TradePolicy()
    path = tmp_path / "paper.sqlite3"
    fingerprint = TradePlan(plan, policy, path).fingerprint()
    owner = TradeStore(path, fingerprint)
    state, _ = make_state(plan, policy, store=owner)
    publish(state)
    entry = next(iter(state.positions.values()))["entry"]
    quantity = F(entry["sizing"]["base_quantity"])
    publish(state, NOW+300*SECOND, sell_ask_size="0.01")
    position = next(iter(state.positions.values()))
    assert F(position["remaining_buy_base"]) == 0
    assert F(position["remaining_sell_base"]) == quantity-F("0.01")
    assert F(position["closing_long_cash_usd"]) == quantity*F("99.99")
    assert F(position["closing_short_cash_usd"]) == F("0.01")*F("100.31")
    saved = owner.load()["positions"][0]
    owner.close()
    owner = TradeStore(path, fingerprint)
    try:
        resumed, events = make_state(plan, policy, store=owner)
        residual = next(iter(resumed.positions.values()))
        for field in ("remaining_buy_base", "remaining_sell_base", "closing_long_cash_usd",
                      "closing_short_cash_usd", "closing_fees_usd", "opened_ns"):
            assert residual[field] == saved[field]
        publish(resumed, NOW+301*SECOND)
        assert not resumed.positions
        closed = events[-1]
        assert closed["reason"] == "max_hold" and closed["opened_ns"] == NOW
        assert [f["instrument_id"] for f in closed["fills"]] == ["BTC.ASTER"]
        expected_net = quantity*(F("100.3")-100+F("99.99")-F("100.31"))
        assert F(closed["mark"]["net_usd"]) == expected_net
        assert F(resumed.document()["realized_net_usd"]) == expected_net
        assert resumed.document()["opened_pairs"] == 1
    finally:
        owner.close()


def test_partial_reductions_preserve_native_lots_and_residual_exposure():
    metadata = {
        "BTC.HL": MarketMetadata("BTC.HL", D("0.01"), D(1), D(0), "synthetic"),
        "BTC.ASTER": MarketMetadata("BTC.ASTER", D("0.002"), D(10), D(0), "synthetic"),
    }
    state, events = make_state(metadata=metadata)
    publish(state)
    deadline = NOW+300*SECOND
    publish(state, deadline, sell_ask_size="0.003")
    position = next(iter(state.positions.values()))
    assert F(position["remaining_buy_base"]) == 0
    assert F(position["remaining_sell_base"]) == F("0.96")
    reduction = [event for event in events if event["event"] == "partial_reduction"][-1]
    short_fill = [fill for fill in reduction["fills"] if fill["instrument_id"] == "BTC.ASTER"][0]
    assert F(short_fill["base_quantity"]) == F("0.02")
    assert F(short_fill["native_quantity"]) == F("0.002")
    exposure = state.document()["exposure"]
    assert F(exposure["BTC.ASTER"]["short_canonical_quantity"]) == F("0.96")
    assert F(exposure["BTC.HL"]["net_canonical_quantity"]) == 0
    state.on_evaluation_timer(deadline)
    assert F(position["remaining_sell_base"]) == F("0.96")
    # The unconsumed 0.001 native units cannot fill a 0.002 native lot.
    set_book(state, "ASTER", "100.3", "100.31", deadline+SECOND, ask_size="0.1")
    state.on_evaluation_timer(deadline+SECOND)
    assert not state.positions
    assert F(events[-1]["fills"][0]["native_quantity"]) == F("0.096")


@pytest.mark.parametrize("minimum_field,minimum,residual", [
    ("min_quantity", "0.2", "0.2"), ("min_notional", "20", "0.201")])
def test_forced_partial_reduction_preserves_executable_minimum_residual(minimum_field, minimum, residual):
    long_metadata = MarketMetadata("BTC.HL", D("0.001"), D(1), D(0), "synthetic",
                                   **{minimum_field: D(minimum)})
    short_metadata = MarketMetadata("BTC.ASTER", D("0.001"), D(1), D(0), "synthetic")
    state, events = make_state(policy=TradePolicy(loss_stop_enabled=False), metadata={
        "BTC.HL": long_metadata, "BTC.ASTER": short_metadata})
    publish(state)
    position = next(iter(state.positions.values()))
    assert F(position["remaining_buy_base"]) == F("0.997")
    deadline = NOW+300*SECOND
    set_book(state, "HL", "99.99", "100", deadline, bid_size="0.9")
    set_book(state, "ASTER", "100.3", "100.31", deadline)
    state.on_evaluation_timer(deadline)
    assert len(state.positions) == 1 and position["status"] == "exit_pending"
    assert F(position["remaining_buy_base"]) == F(residual)
    assert F(position["remaining_sell_base"]) == 0
    reduction = [event for event in events if event["event"] == "partial_reduction"][-1]
    long_fill = [fill for fill in reduction["fills"] if fill["instrument_id"] == "BTC.HL"][0]
    reduced = F(long_fill["base_quantity"])
    assert reduced == F("0.997")-F(residual)
    assert 0 < reduced <= F("0.9")
    assert reduced % F("0.001") == 0
    assert F(residual) % F("0.001") == 0
    if minimum_field == "min_quantity":
        assert F(long_fill["native_quantity"]) >= F(minimum)
        assert F(residual) >= F(minimum)
    else:
        assert F(long_fill["cash_usd"]) >= F(minimum)
        assert F(residual)*F("99.99") >= F(minimum)
    set_book(state, "HL", "99.99", "100", deadline+SECOND, bid_size="10")
    state.on_evaluation_timer(deadline+SECOND)
    assert not state.positions and events[-1]["event"] == "closed"
    assert events[-1]["reason"] == "max_hold"
    assert F(events[-1]["fills"][0]["base_quantity"]) == F(residual)


def test_partial_close_accumulates_adverse_slippage_and_every_fill_fee():
    state, events = make_state(policy=TradePolicy(slippage_bps=D(2)),
        fees={"HL": "1", "ASTER": "2"})
    publish(state, sell_bid="100.5", sell_ask="100.51")
    position = next(iter(state.positions.values()))
    opening = position["entry"]["paper_fills"]
    quantity = F(position["entry"]["sizing"]["base_quantity"])
    publish(state, NOW+300*SECOND, sell_bid="100.5", sell_ask="100.51", sell_ask_size="0.01")
    assert len(state.positions) == 1
    set_book(state, "ASTER", "100.54", "100.55", NOW+301*SECOND)
    state.on_evaluation_timer(NOW+301*SECOND)
    assert not state.positions
    fills = [fill for event in events if event["event"] in {"partial_reduction", "closed"}
             for fill in event["fills"]]
    sale = sum((F(fill["cash_usd"]) for fill in fills if fill["side"] == "SELL"), F(0))
    cover = sum((F(fill["cash_usd"]) for fill in fills if fill["side"] == "BUY"), F(0))
    assert sale == quantity*F("99.99")*F("0.9998")
    assert cover == (F("0.01")*F("100.51") + (quantity-F("0.01"))*F("100.55"))*F("1.0002")
    fees = F(opening["buy_fee_usd"])+F(opening["sell_fee_usd"])
    fees += sum((F(fill["fee_usd"]) for fill in fills), F(0))
    net = F(opening["sell_notional_usd"])-F(opening["buy_notional_usd"])+sale-cover-fees
    assert F(events[-1]["mark"]["round_trip_fees_usd"]) == fees
    assert F(events[-1]["mark"]["net_usd"]) == net
    assert F(state.document()["realized_net_usd"]) == net


def write_configs(tmp_path, *, mode="paper", extra=""):
    scan = tmp_path / "scan.toml"
    scan.write_text('''schema_version = 1
[scan]
reserve_bps = "1"
[recording]
enabled = false
[[markets]]
symbol = "BTC"
venue = "HL"
instrument_id = "BTC-PERP.HYPERLIQUID"
quote_to_usd = "1"
valuation_source = "synthetic parity"
[[markets]]
symbol = "BTC"
venue = "ASTER"
instrument_id = "BTCUSDT-PERP.ASTER"
quote_to_usd = "1"
valuation_source = "synthetic parity"
''', encoding="utf-8")
    trade = tmp_path / "trade.toml"
    trade.write_text(f'''schema_version = 1
mode = "{mode}"
scan_config = "scan.toml"
state_path = "runtime/paper.sqlite3"
[trade]
order_notional_usd = "100"
entry_edge_bps = "30"
take_profit_bps = "5"
max_loss_bps = "20"
breakeven_after_secs = 180
force_close_after_secs = 300
{extra}
''', encoding="utf-8")
    return trade


def test_dry_run_validates_without_native_socket_credentials_or_state(tmp_path, monkeypatch, capsys):
    config = write_configs(tmp_path)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith(("nautilus_trader", "dotenv", "opportunity_runtime",
                            "opportunity_paper", "opportunity_trade_store")):
            raise AssertionError("dry run imported execution or runtime dependencies")
        return original_import(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("dry run attempted a side effect")
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sqlite3, "connect", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    assert main(["--config", str(config), "--dry-run", "--duration-secs", "0", "--symbols", "BTC"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["policy"]["entry_edge_bps"] == "30"
    assert document["policy"]["max_loss_bps"] == "20"
    assert document["scan"]["settings"]["target_notional"] == "100"
    assert document["scan"]["evaluation_interval_ms"] == 100
    assert document["scan"]["duration_secs"] == 0
    assert document["registered_execution_clients"] == 0
    assert document["remote_writes_allowed"] is False
    assert document["runtime_started"] is False
    assert {path.name for path in tmp_path.iterdir()} == {"scan.toml", "trade.toml"}


@pytest.mark.parametrize("mode,extra", [("live", ""), ("testnet", ""),
    ("paper", 'private_key = "disallowed"'), ("paper", 'slippage_bps = "NaN"')])
def test_invalid_trade_config_cannot_start_runtime_or_create_state(tmp_path, mode, extra):
    config = write_configs(tmp_path, mode=mode, extra=extra)
    with pytest.raises(ScanConfigError):
        load_trade_plan(config)
    assert not (tmp_path / "runtime").exists()


def test_shutdown_preserves_open_pair_for_resume():
    state, events = make_state()
    publish(state)
    state.finish(NOW+SECOND)
    assert len(state.positions) == 1 and state.document()["closed_pairs"] == 0
    assert events[-1]["event"] == "stopped"
    publish(state, NOW+2*SECOND)
    state.finish(NOW+2*SECOND)
    assert state.document()["opened_pairs"] == 1
    assert sum(e["event"] == "stopped" for e in events) == 1

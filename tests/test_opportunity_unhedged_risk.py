"""Independent per-pair risk deadlines never manufacture terminal order evidence."""
from copy import deepcopy
from contextlib import closing
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F

import pytest

from test_opportunity_live import MemoryStore, accounts, fill, receipt, state, submissions
from test_opportunity_live_rebalance import enter, refresh
from test_opportunity_trade import NOW, SECOND, make_plan, publish, set_book
from opportunity_trade import TradePolicy
from opportunity_trade_store import TradeStore


def pending_entry(live, leg="buy", quantity="0.8", terminal=True):
    publish(live)
    buy, sell = submissions(live)
    filled, unknown = (buy, sell) if leg == "buy" else (sell, buy)
    fill(live, filled, NOW + SECOND // 10, quantity=quantity, terminal=terminal)
    live.on_receipt(receipt(unknown, "unknown"), NOW + SECOND // 10)
    return next(iter(live.positions.values())), filled, unknown


@pytest.mark.parametrize("leg", ["buy", "sell"])
def test_deadline_reduces_only_confirmed_leg_and_late_fill_is_owned_until_flat(leg):
    live, events = state()
    position, filled, unknown = pending_entry(live, leg)
    start = position["opened_ns"]
    assert position["unhedged_since_ns"] == start
    assert not refresh(live, start + 10 * SECOND - 1)
    unwind, = refresh(live, start + 10 * SECOND)
    assert unwind.instrument_id == filled.instrument_id and unwind.quantity == D("0.8")
    assert unwind.side != filled.side and unwind.reduce_only
    assert position["exit_reason"] == "unhedged_timeout"
    assert set(position["entry_ids"]) == {filled.client_order_id, unknown.client_order_id}
    fill(live, unwind, start + 10 * SECOND + 1)
    assert not refresh(live, start + 11 * SECOND)
    assert live.positions and live.completed == 0 and live.gross_exposure(reserve=True) == 200
    assert live.orders[unknown.client_order_id]["terminal_ns"] is None
    fill(live, unknown, start + 12 * SECOND, quantity="0.4")
    late_exit, = refresh(live, start + 13 * SECOND)
    assert late_exit.instrument_id == unknown.instrument_id and late_exit.quantity == D("0.4")
    assert late_exit.side != unknown.side and not position["rebalance_ids"]
    fill(live, late_exit, start + 13 * SECOND + 1)
    assert not refresh(live, start + 14 * SECOND)
    assert not live.positions and live.completed == 1 and live.gross_exposure(reserve=True) == 0
    assert sum(e["event"] == "unhedged_limit_breached" for e in events) == 1


def test_unknown_without_fills_never_starts_exposure_clock_or_releases_budget():
    live, _ = state(order_timeout_secs=1, max_unhedged_secs=2)
    publish(live)
    entries = submissions(live)
    for order in entries:
        live.on_receipt(receipt(order, "unknown"), NOW + SECOND)
    assert not refresh(live, NOW + 30 * SECOND)
    position = next(iter(live.positions.values()))
    assert position["status"] == "entering" and position["unhedged_since_ns"] is None
    assert "unhedged_breach" not in position and live.gross_exposure(reserve=True) == 200
    assert all(o["terminal_ns"] is None and o["filled_quantity"] == "0" for o in live.orders.values())


def test_risk_deadline_is_independent_of_query_interval_and_max_hold():
    live, _ = state(order_timeout_secs=30, max_unhedged_secs=2)
    position, filled, _ = pending_entry(live)
    unwind, = refresh(live, position["opened_ns"] + 2 * SECOND)
    assert unwind.instrument_id == filled.instrument_id
    assert live.policy.force_close_after_secs == 300 and live.order_timeout_ns == 30 * SECOND


def test_amount_limit_latches_on_fill_before_timer_and_blocks_other_pair_admission():
    live, events = state(make_plan(symbols=("BTC", "ETH")), max_unhedged_notional_usd="80")
    publish(live)
    buy, sell = submissions(live)
    assert buy.quantity < D("0.8")
    # Admission reserves headroom; a real subsequent jump still trips the cap.
    set_book(live, "HL", "101.99", "102", NOW + 1)
    fill(live, buy, NOW + SECOND // 10)
    position = next(iter(live.positions.values()))
    assert position["exit_reason"] == "unhedged_notional"
    assert F(position["unhedged_breach"]["notional_usd"]) > 80
    assert position["unhedged_breach"]["duration_ms"] == "0"
    publish(live, NOW + SECOND // 2, symbol="ETH")
    assert not submissions(live) and len(live.positions) == 1
    assert sum(e["event"] == "unhedged_limit_breached" for e in events) == 1


def test_default_amount_cap_uses_current_canonical_price_without_waiting_ten_seconds():
    live, _ = state()
    position, _, _ = pending_entry(live)
    now = NOW + SECOND
    accounts(live, now)
    set_book(live, "HL", "124.99", "125", now)
    live.on_evaluation_timer(now)
    unwind, = submissions(live)
    assert position["exit_reason"] == "unhedged_notional"
    assert position["unhedged_breach"]["notional_usd"] == "100"
    assert unwind.quantity == D("0.8") and unwind.quantity * unwind.price <= 100


def test_canonical_quantity_not_native_quantity_or_leg_cash_defines_exposure():
    live, _ = state()
    live.register_instrument("BTC.ASTER", D("0.01"), D(10), "USD")
    live.metadata["BTC.ASTER"] = replace(live.metadata["BTC.ASTER"], multiplier=D(10), size_increment=D("0.001"))
    publish(live)
    buy, sell = submissions(live)
    fill(live, buy, NOW + SECOND // 10, quantity="0.5")
    fill(live, sell, NOW + SECOND // 10, quantity="0.05")
    position = next(iter(live.positions.values()))
    assert position["actual_entry"]["buy_notional_usd"] != position["actual_entry"]["sell_notional_usd"]
    assert position["unhedged_base"] == "0" and position["unhedged_since_ns"] is None
    assert not refresh(live, NOW + 11 * SECOND)
    assert position["status"] == "open" and "unhedged_breach" not in position


def test_partial_pending_entry_can_reduce_confirmed_quantity_without_replacing_original_id():
    live, _ = state()
    position, entry, other = pending_entry(live, terminal=False)
    now = position["opened_ns"] + 10 * SECOND
    # Only this instrument's original entry ID is allowed in its account proof.
    accounts(live, now)
    live.on_account(replace(live.accounts[entry.instrument_id], open_order_ids=(entry.client_order_id,)), now)
    set_book(live, "HL", "99.99", "100", now)
    live.on_evaluation_timer(now)
    unwind, = submissions(live)
    assert unwind.quantity == D("0.8") and unwind.reduce_only
    live.on_receipt(receipt(unwind, "unknown"), now + 1)
    assert not refresh(live, now + SECOND)
    fill(live, unwind, now + SECOND + 1)
    # More of the original entry fills after the first confirmed amount exits.
    fill(live, entry, now + 2 * SECOND, quantity="0.1", trade_id="late-entry", terminal=False)
    residual, = refresh(live, now + 3 * SECOND)
    assert residual.quantity == D("0.1") and residual.instrument_id == entry.instrument_id
    assert position["entry_ids"] == [entry.client_order_id, other.client_order_id]
    assert live.orders[entry.client_order_id]["terminal_ns"] is None
    fill(live, residual, now + 3 * SECOND + 1)
    for order in (entry, other):
        live.on_receipt(receipt(order, "terminal"), now + 4 * SECOND)
    assert not refresh(live, now + 5 * SECOND)
    assert not live.positions


@pytest.mark.parametrize("problem", ["stale", "mismatch", "foreign", "disconnected", "other_owner", "pending_reduction"])
def test_pending_entry_cleanup_still_requires_account_and_exclusive_ownership(problem):
    live, _ = state()
    position, entry, _ = pending_entry(live, terminal=False)
    now = position["opened_ns"] + 10 * SECOND
    accounts(live, now)
    account = live.accounts[entry.instrument_id]
    if problem == "stale":
        live.accounts[entry.instrument_id] = replace(account, observed_ns=NOW)
    elif problem == "mismatch":
        live.accounts[entry.instrument_id] = replace(account, position_quantity=D("0.9"))
    elif problem == "foreign":
        live.accounts[entry.instrument_id] = replace(account, open_order_ids=("foreign",))
    elif problem == "disconnected":
        live.accounts[entry.instrument_id] = replace(account, connected=False)
    else:
        extra = deepcopy(live.orders[entry.client_order_id])
        extra["client_order_id"] = "different"
        if problem == "other_owner":
            extra["position_id"] += 1
        else:
            extra["purpose"] = "rebalance"
        live.orders["different"] = extra
    set_book(live, "HL", "99.99", "100", now)
    live.on_evaluation_timer(now)
    assert not submissions(live) and position["exit_reason"] == "unhedged_timeout"
    assert position["remaining_buy_base"] == "0.8" and live.gross_exposure(reserve=True) >= 200


def test_partial_fills_and_sign_change_do_not_restart_continuous_unhedged_clock():
    live, _ = state()
    position, buy, sell = pending_entry(live, quantity="0.4", terminal=False)
    start = position["unhedged_since_ns"]
    fill(live, sell, start + 4 * SECOND, quantity="0.2", terminal=False)
    fill(live, sell, start + 8 * SECOND, quantity="0.3", trade_id="cross", terminal=False)
    assert position["unhedged_since_ns"] == start and F(position["unhedged_base"]) == F("-0.1")
    exits = refresh(live, start + 10 * SECOND)
    assert {i.quantity for i in exits} == {D("0.4"), D("0.5")}
    assert position["exit_reason"] == "unhedged_timeout"


def test_balance_before_deadline_resets_clock_and_duplicate_fill_cannot_change_it():
    live, _ = state()
    position, buy, sell = pending_entry(live, quantity="0.4", terminal=False)
    start = position["unhedged_since_ns"]
    fill(live, sell, start + 9 * SECOND, quantity="0.4", terminal=False)
    assert position["unhedged_since_ns"] is None
    payload = fill(live, buy, start + 12 * SECOND, quantity="0.1", trade_id="next", terminal=False)
    new_start = position["unhedged_since_ns"]
    live.on_fill(payload, start + 15 * SECOND)
    assert new_start == start + 12 * SECOND and position["unhedged_since_ns"] == new_start
    assert not refresh(live, new_start + 9 * SECOND)
    assert "unhedged_breach" not in position
    assert len(refresh(live, new_start + 10 * SECOND)) == 2


def test_fill_arriving_at_deadline_cannot_erase_expired_risk_before_timer_runs():
    live, _ = state()
    position, _, sell = pending_entry(live, quantity="0.4")
    fill(live, sell, position["opened_ns"] + 10 * SECOND, quantity="0.4")
    assert position["unhedged_base"] == "0" and position["unhedged_since_ns"] is None
    assert position["exit_reason"] == "unhedged_timeout"
    assert len(refresh(live, NOW + 11 * SECOND)) == 2


def test_rebalance_unknown_blocks_duplicate_but_late_trim_is_accounted_before_exit():
    live, _ = state()
    position, _ = enter(live)
    trim, = refresh(live, NOW + SECOND)
    fill(live, trim, NOW + 2 * SECOND, quantity="0.1", terminal=False)
    live.on_receipt(receipt(trim, "unknown"), NOW + 2 * SECOND + 1)
    other_exit, = refresh(live, NOW + 11 * SECOND)
    assert other_exit.instrument_id == "BTC.ASTER" and other_exit.quantity == D("0.5")
    fill(live, other_exit, NOW + 11 * SECOND + 1)
    assert not refresh(live, NOW + 12 * SECOND)
    fill(live, trim, NOW + 13 * SECOND, quantity="0.2", trade_id="late-trim")
    exit_order, = refresh(live, NOW + 14 * SECOND)
    assert exit_order.instrument_id == trim.instrument_id and exit_order.quantity == D("0.5")
    assert position["rebalance_ids"] == [trim.client_order_id] and position["exit_reason"] == "unhedged_timeout"
    fill(live, exit_order, NOW + 14 * SECOND + 1)
    assert not refresh(live, NOW + 15 * SECOND) and not live.positions


def test_untradeable_rebalance_excess_times_out_and_exits_owned_lots_without_rounding_up():
    live, _ = state()
    for venue, step in (("HL", "0.02"), ("ASTER", "0.03")):
        iid = f"BTC.{venue}"
        live.metadata[iid] = replace(live.metadata[iid], size_increment=D(step))
    position, _ = enter(live, "0.10", "0.09")
    assert not refresh(live, NOW + SECOND) and position["rebalance_blocked"]
    exits = refresh(live, position["opened_ns"] + 10 * SECOND)
    assert {i.quantity for i in exits} == {D("0.10"), D("0.09")}
    assert not position["rebalance_ids"] and position["exit_reason"] == "unhedged_timeout"


def test_partial_rebalance_restart_keeps_original_risk_clock_and_actual_cash(tmp_path):
    path = tmp_path / "live.sqlite3"
    with closing(TradeStore(path, "unhedged-risk")) as store:
        live, _ = state(store=store)
        position, _ = enter(live)
        trim, = refresh(live, NOW + SECOND)
        fill(live, trim, NOW + 2 * SECOND, quantity="0.1", fee="0.001")
        saved = deepcopy(position)
    with closing(TradeStore(path, "unhedged-risk")) as store:
        resumed, _ = state(store=store)
        position = next(iter(resumed.positions.values()))
        assert position["unhedged_since_ns"] == saved["opened_ns"]
        assert position["actual_entry"] == saved["actual_entry"]
        assert position["closing_fees_usd"] == "0.001"
        assert position["rebalance_target_base"] == "0.5"
        exits = refresh(resumed, saved["opened_ns"] + 10 * SECOND)
        assert {i.quantity for i in exits} == {D("0.7"), D("0.5")}
        assert position["exit_reason"] == "unhedged_timeout"


def test_risk_latch_survives_unknown_restart_without_automatic_replay(tmp_path):
    with closing(TradeStore(tmp_path / "live.sqlite3", "unhedged-risk")) as store:
        live, _ = state(store=store)
        position, _, _ = pending_entry(live)
        live.on_evaluation_timer(position["opened_ns"] + 10 * SECOND)
        breach = deepcopy(position["unhedged_breach"])
        resumed, _ = state(store=store, activate=False)
        recovered = next(iter(resumed.positions.values()))
        assert recovered["unhedged_breach"] == breach and recovered["exit_reason"] == "unhedged_timeout"
        assert resumed.phase == "recovery_blocked" and not resumed.activate(NOW + 20 * SECOND)
        assert not resumed.drain_intents() and resumed.gross_exposure(reserve=True) == 200


def test_legacy_unbalanced_ledger_does_not_gain_a_new_grace_period():
    store = MemoryStore()
    live, _ = state(store=store)
    position, _ = enter(live)
    saved = live.snapshot()
    for key in tuple(saved["positions"][0]):
        if key.startswith("unhedged_"):
            saved["positions"][0].pop(key)
    restored, _ = state(store=MemoryStore(saved))
    recovered = next(iter(restored.positions.values()))
    assert recovered["unhedged_since_ns"] == position["opened_ns"]
    assert len(refresh(restored, position["opened_ns"] + 10 * SECOND)) == 2
    assert recovered["exit_reason"] == "unhedged_timeout"


def test_gateway_keeps_querying_original_entry_after_independent_risk_exit():
    from test_opportunity_native_execution import FakeStrategy, MODEL, event
    from opportunity_native_execution import NativeOrderGateway

    live, _ = state(max_unhedged_secs=2)
    native = FakeStrategy()
    now = NOW
    gateway = NativeOrderGateway(native, live.plan.markets,
        lambda r: live.on_receipt(r, now), lambda a: live.on_account(a, now),
        lambda f: live.on_fill(f, now), model=MODEL)

    def execute(intent, quantity):
        cached = native.cached[intent.client_order_id]
        cached.is_closed, cached.filled_qty = True, D(quantity)
        gateway.on_native_event("filled", event(client_order_id=intent.client_order_id,
            instrument_id=intent.instrument_id, trade_id="trade-" + intent.client_order_id,
            order_side=intent.side, last_qty=D(quantity), last_px=intent.price, commission=None), now)

    try:
        publish(live)
        buy, sell = submissions(live)
        for entry in (buy, sell):
            gateway.dispatch(entry, now)
        now += SECOND // 10
        execute(buy, "0.8")
        now += 2 * SECOND
        unwind, = refresh(live, now)
        gateway.dispatch(unwind, now)
        execute(unwind, "0.8")
        now = NOW + 11 * SECOND
        gateway.poll(now)
        assert ("query", sell.client_order_id, {"client_id": "ASTER"}) in native.calls
        assert live.orders[sell.client_order_id]["status"] == "unknown"
        assert not refresh(live, now) and live.gross_exposure(reserve=True) == 200
        now = NOW + 21 * SECOND
        gateway.poll(now)
        assert sum(call[:2] == ("query", sell.client_order_id) for call in native.calls) == 2
        execute(sell, "0.4")
        now += SECOND
        late_exit, = refresh(live, now)
        assert late_exit.instrument_id == sell.instrument_id and late_exit.quantity == D("0.4")
        gateway.dispatch(late_exit, now)
        execute(late_exit, "0.4")
        assert not refresh(live, now + SECOND) and not live.positions
        assert sum(call[:2] == ("submit", sell.client_order_id) for call in native.calls) == 1
    finally:
        gateway.close()


def test_missing_books_or_fees_do_not_reset_deadline_but_cleanup_needs_fresh_price():
    live, _ = state()
    position, _, _ = pending_entry(live)
    live.books.clear()
    for iid, meta in tuple(live.metadata.items()):
        live.metadata[iid] = replace(meta, taker_fee_bps=None, fee_source="unknown")
    live.on_evaluation_timer(position["opened_ns"] + 10 * SECOND)
    assert not submissions(live) and position["exit_reason"] == "unhedged_timeout"
    assert position["unhedged_notional_usd"] == "80"
    unwind, = refresh(live, NOW + 11 * SECOND)
    assert unwind.quantity == D("0.8") and unwind.reduce_only


def test_shared_leg_account_net_zero_does_not_cancel_pair_exposure_or_expand_ownership():
    live, _ = state(make_plan(venues=("HL", "ASTER", "LIGHTER")),
                    policy=TradePolicy(loss_stop_enabled=False))
    first, _ = enter(live, "0.5", "0.8")
    now = NOW + SECOND
    accounts(live, now)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "100.3", "100.31", now)
    set_book(live, "LIGHTER", "100.7", "100.71", now)
    assert live._entry("BTC.ASTER", "BTC.LIGHTER", now)
    for intent in submissions(live):
        fill(live, intent, now + 1, quantity="0.8")
    assert live.expected_native("BTC.ASTER") == 0
    now = first["opened_ns"] + 10 * SECOND
    accounts(live, now)
    for venue, bid, ask in (("HL", "99.99", "100"), ("ASTER", "100.3", "100.31"), ("LIGHTER", "100.7", "100.71")):
        set_book(live, venue, bid, ask, now)
    live.on_evaluation_timer(now)
    exits = submissions(live)
    assert len(exits) == 2 and all(live.orders[i.client_order_id]["position_id"] == first["position_id"] for i in exits)
    shared, = [i for i in exits if i.instrument_id == "BTC.ASTER"]
    assert shared.quantity == D("0.8") and shared.side == "BUY" and not shared.reduce_only
    assert F(first["unhedged_base"]) == F("-0.3")


@pytest.mark.parametrize("kwargs", [
    {"max_unhedged_secs": 0}, {"max_unhedged_secs": -1}, {"max_unhedged_secs": True},
    {"max_unhedged_secs": 11}, {"max_unhedged_secs": 1.5},
    {"max_unhedged_notional_usd": "0"}, {"max_unhedged_notional_usd": "101"},
    {"max_unhedged_notional_usd": "NaN"}, {"max_unhedged_notional_usd": "Infinity"},
    {"max_unhedged_notional_usd": True},
])
def test_invalid_or_widened_risk_limits_rejected(kwargs):
    with pytest.raises(ValueError):
        state(**kwargs)

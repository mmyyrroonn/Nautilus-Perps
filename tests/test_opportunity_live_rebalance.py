"""Confirmed entry imbalance retains only the existing matched inventory."""
from copy import deepcopy
from decimal import Decimal as D
from fractions import Fraction as F

import pytest

from test_opportunity_live import (
    MemoryStore, accounts, fill, receipt, state, submissions,
)
from test_opportunity_trade import NOW, SECOND, make_plan, publish, set_book
from opportunity_core import MarketMetadata


def refresh(live, now, **account_kwargs):
    accounts(live, now, **account_kwargs)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "100.3", "100.31", now)
    live.on_evaluation_timer(now)
    return submissions(live)


def enter(live, buy_qty="0.8", sell_qty="0.5"):
    publish(live)
    entries = submissions(live)
    assert len(entries) == 2
    for intent, quantity in zip(entries, (buy_qty, sell_qty)):
        if D(quantity):
            fill(live, intent, NOW+SECOND//10, quantity=quantity)
        else:
            live.on_receipt(receipt(intent, "canceled"), NOW+SECOND//10+1)
    return next(iter(live.positions.values())), entries


@pytest.mark.parametrize("buy_qty,sell_qty,venue,side,leg", [
    ("0.8", "0.5", "HL", "SELL", "buy"),
    ("0.5", "0.8", "ASTER", "BUY", "sell"),
])
def test_trim_only_excess_preserves_pair_clock_budget_and_actual_cash(buy_qty, sell_qty, venue, side, leg):
    live, events = state()
    position, _ = enter(live, buy_qty, sell_qty)
    opened_ns, actual_entry = position["opened_ns"], deepcopy(position["actual_entry"])
    trim, = refresh(live, NOW+SECOND)
    assert (trim.venue, trim.side, trim.quantity, trim.reduce_only) == (venue, side, D("0.3"), True)
    assert live.orders[trim.client_order_id]["purpose"] == "rebalance"
    assert position["exit_ids"] == [] and position["status"] == "rebalancing"
    payload = fill(live, trim, NOW+SECOND+1, fee="0.001")
    live.on_fill(payload, NOW+SECOND+3)
    assert not refresh(live, NOW+2*SECOND)
    assert position["status"] == "open" and position["exit_reason"] is None
    assert position["opened_ns"] == opened_ns and position["actual_entry"] == actual_entry
    assert F(position["remaining_buy_base"]) == F(position["remaining_sell_base"]) == F("0.5")
    cash_field = "closing_long_cash_usd" if leg == "buy" else "closing_short_cash_usd"
    assert F(position[cash_field]) == F("0.3")*F(trim.price)
    assert F(position["closing_fees_usd"]) == F("0.001")
    assert live.completed == 0 and live.gross_exposure(reserve=True) == F(200)
    completed, = [event for event in events if event["event"] == "entry_rebalanced"]
    assert F(completed["elapsed_ms"]) == 1000


def test_partial_trim_retries_only_remaining_excess_after_terminal_and_account_proof():
    live, _ = state()
    position, _ = enter(live)
    trim, = refresh(live, NOW+SECOND)
    fill(live, trim, NOW+SECOND+1, quantity="0.1")
    assert not refresh(live, NOW+2*SECOND, observed_ns=NOW+SECOND)
    next_trim, = refresh(live, NOW+2*SECOND+1)
    assert next_trim.quantity == D("0.2") and next_trim.instrument_id == trim.instrument_id
    assert next_trim.client_order_id != trim.client_order_id
    assert F(position["remaining_sell_base"]) == F("0.5")
    fill(live, next_trim, NOW+2*SECOND+2)
    assert not refresh(live, NOW+3*SECOND)
    assert position["status"] == "open"


def test_balanced_fill_does_not_complete_rebalance_before_terminal_and_fresh_account_proof():
    live, events = state()
    position, _ = enter(live)
    trim, = refresh(live, NOW+SECOND)
    fill(live, trim, NOW+SECOND+1, terminal=False)
    assert not refresh(live, NOW+2*SECOND)
    assert position["status"] == "rebalancing"
    live.on_receipt(receipt(trim, "terminal"), NOW+2*SECOND+1)
    live.on_evaluation_timer(NOW+2*SECOND+2)
    assert not submissions(live) and position["status"] == "rebalancing"
    assert not refresh(live, NOW+2*SECOND+3)
    assert position["status"] == "open"
    assert sum(event["event"] == "entry_rebalanced" for event in events) == 1


def test_unknown_trim_never_replaced_and_blocks_restart_activation():
    store = MemoryStore()
    live, _ = state(store=store)
    position, _ = enter(live)
    trim, = refresh(live, NOW+SECOND)
    live.on_receipt(receipt(trim, "unknown"), NOW+SECOND+1)
    assert not refresh(live, NOW+2*SECOND)
    exit_order, = refresh(live, NOW+11*SECOND)
    assert exit_order.instrument_id == "BTC.ASTER" and exit_order.quantity == D("0.5")
    assert position["exit_reason"] == "unhedged_timeout"
    assert not refresh(live, NOW+25*SECOND)
    assert position["rebalance_ids"] == [trim.client_order_id]
    restored, _ = state(store=store, activate=False)
    assert restored.phase == "recovery_blocked" and not restored.activate(NOW+26*SECOND)
    assert not restored.drain_intents()


def test_confirmed_partial_trim_restart_keeps_target_clock_cash_and_remaining_difference():
    store = MemoryStore()
    live, _ = state(store=store)
    position, _ = enter(live)
    trim, = refresh(live, NOW+SECOND)
    fill(live, trim, NOW+SECOND+1, quantity="0.1", fee="0.001")
    restored, _ = state(store=store)
    recovered = next(iter(restored.positions.values()))
    assert recovered["rebalance_target_base"] == position["rebalance_target_base"]
    assert recovered["opened_ns"] == position["opened_ns"]
    assert recovered["closing_long_cash_usd"] == position["closing_long_cash_usd"]
    assert recovered["closing_fees_usd"] == "0.001"
    next_trim, = refresh(restored, NOW+2*SECOND)
    assert next_trim.quantity == D("0.2") and next_trim.side == "SELL"
    assert next_trim.client_order_id != trim.client_order_id


@pytest.mark.parametrize("buy_qty,sell_qty,venue,side", [
    ("0.8", "0", "HL", "SELL"), ("0", "0.8", "ASTER", "BUY"),
])
def test_pure_single_leg_exits_all_owned_quantity_without_opening_missing_leg(buy_qty, sell_qty, venue, side):
    live, _ = state()
    position, _ = enter(live, buy_qty, sell_qty)
    unwind, = refresh(live, NOW+SECOND)
    assert (unwind.venue, unwind.side, unwind.quantity) == (venue, side, D("0.8"))
    assert live.orders[unwind.client_order_id]["purpose"] == "exit"
    assert position["exit_reason"] == "entry_imbalance" and not position["rebalance_ids"]
    fill(live, unwind, NOW+SECOND+1)
    assert not refresh(live, NOW+2*SECOND)
    assert not live.positions and live.completed == 1


def test_unknown_entry_is_not_classified_as_confirmed_single_leg_or_rebalanced():
    live, _ = state()
    publish(live)
    buy, sell = submissions(live)
    fill(live, buy, NOW+SECOND//10, quantity="0.8")
    fill(live, sell, NOW+SECOND//10, quantity="0.5", terminal=False)
    live.on_receipt(receipt(sell, "unknown"), NOW+SECOND//10+1)
    assert not refresh(live, NOW+2*SECOND)
    position = next(iter(live.positions.values()))
    assert position["status"] == "entering" and not position["rebalance_ids"]


@pytest.mark.parametrize("trigger", ["stop", "max_hold", "max_loss"])
def test_existing_hard_risk_exit_overrides_retain_balanced_pair(trigger):
    live, _ = state()
    position, _ = enter(live)
    live.on_evaluation_timer(NOW+SECOND//2)
    assert position["status"] == "rebalancing"
    now = NOW+SECOND
    if trigger == "stop":
        live.stop_entries(now)
    elif trigger == "max_hold":
        now = position["opened_ns"]+300*SECOND
    accounts(live, now)
    set_book(live, "HL", "98" if trigger == "max_loss" else "99.99", "100", now)
    set_book(live, "ASTER", "100.3", "100.31", now)
    live.on_evaluation_timer(now)
    exits = submissions(live)
    assert len(exits) == 2 and {intent.quantity for intent in exits} == {D("0.8"), D("0.5")}
    assert all(live.orders[intent.client_order_id]["purpose"] == "exit" for intent in exits)
    assert position["exit_reason"] == ("operator_stop" if trigger == "stop" else trigger)


def test_untradeable_excess_is_retained_and_reported_without_rounding_up_or_trimming_other_leg():
    live, events = state()
    for venue, step in (("HL", "0.02"), ("ASTER", "0.03")):
        iid = f"BTC.{venue}"
        live.update_metadata(iid, MarketMetadata(iid, D(step), taker_fee_bps=D(0),
                             fee_source="synthetic"), NOW, evaluate=False)
    position, _ = enter(live, "0.10", "0.09")
    assert not refresh(live, NOW+SECOND)
    assert not refresh(live, NOW+2*SECOND)
    assert position["rebalance_blocked"] and position["status"] == "rebalancing"
    assert F(position["remaining_buy_base"]) == F("0.10")
    assert F(position["remaining_sell_base"]) == F("0.09")
    assert sum(event["event"] == "entry_rebalance_blocked" for event in events) == 1


def test_shared_net_zero_leg_rebalances_only_owned_difference_with_ordinary_ioc():
    live, _ = state(make_plan(venues=("HL", "ASTER", "LIGHTER")))
    position, _ = enter(live, "0.5", "0.8")
    now = NOW+SECOND
    accounts(live, now)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "100.3", "100.31", now)
    set_book(live, "LIGHTER", "100.7", "100.71", now)
    assert live._entry("BTC.ASTER", "BTC.LIGHTER", now)
    for intent in submissions(live):
        fill(live, intent, now+1, quantity="0.8")
    assert live.expected_native("BTC.ASTER") == 0
    accounts(live, now+SECOND)
    live.on_evaluation_timer(now+SECOND)
    trim, = submissions(live)
    assert trim.instrument_id == "BTC.ASTER" and trim.side == "BUY" and trim.quantity == D("0.3")
    assert not trim.reduce_only
    assert live.orders[trim.client_order_id]["position_id"] == position["position_id"]

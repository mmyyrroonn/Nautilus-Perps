"""Risk reduction progresses independently of account and economic reconciliation."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F

import pytest

from test_opportunity_live import MemoryStore, fill, open_pair, publish, receipt, state, submissions
from test_opportunity_trade import NOW, SECOND, set_book
from test_opportunity_unknown_reduction import pending_exit, refresh


def test_account_wide_mismatch_never_blocks_the_confirmed_target_exit():
    live, position, original, _, timeout, _ = pending_exit()
    now = timeout + SECOND
    refresh(live, now)
    account = live.accounts[original.instrument_id]
    live.accounts[original.instrument_id] = replace(account,
        account_positions=account.account_positions + (("UNRELATED.HL", D(123)),),
        account_open_orders=(("UNRELATED.HL", "foreign-order"),))
    assert not live._account_ready(original.instrument_id, now)
    live.on_evaluation_timer(now)
    retry, = submissions(live)
    assert retry.reduce_only and retry.quantity == D("0.69")
    assert live.orders[original.client_order_id]["status"] == "unknown"


def test_persistence_failure_keeps_reductions_running_and_entries_closed():
    store = MemoryStore()
    live, _ = state(store=store)
    position, _ = open_pair(live)
    def fail(*args):
        raise OSError("synthetic disk failure")
    store.save = fail
    now = NOW + 2 * SECOND
    live.stop_entries(now)
    refresh(live, now + 1)
    live.on_evaluation_timer(now + 1)
    exits = submissions(live)
    assert len(exits) == 2 and all(order.reduce_only for order in exits)
    assert live.persistence_failed and live.halt_reason
    assert not live._entry(position["buy_id"], position["sell_id"], now + 1)
    for order in exits:
        fill(live, order, now + 2)
    assert not live.has_exit_risk(now + 3)


def test_unknown_retries_without_account_reads_and_stops_sending_after_target_flat():
    live, position, original, _, timeout, _ = pending_exit()
    now = timeout + SECOND
    live.accounts.clear()
    set_book(live, "HL", "99.99", "100", now)
    live.on_evaluation_timer(now)
    retry, = submissions(live)
    assert retry.reduce_only and retry.quantity == D("0.69")
    # Exchange reports only 0.5 left; the old entry/exit bookkeeping is unresolved.
    live.on_receipt(receipt(retry, "unknown"), now + SECOND)
    refresh(live, now + SECOND + 1)
    account = live.accounts[original.instrument_id]
    live.accounts[original.instrument_id] = replace(account, position_quantity=D("0.5"),
        bid=D("99.99"), ask=D("100"), account_positions=None, account_open_orders=None)
    live.on_evaluation_timer(now + SECOND + 1)
    capped, = submissions(live)
    assert capped.quantity == D("0.5") and capped.reduce_only
    fill(live, capped, now + SECOND + 2)
    refresh(live, now + 2 * SECOND)
    live.accounts[original.instrument_id] = replace(live.accounts[original.instrument_id],
        position_quantity=D(0), bid=D("99.99"), ask=D("100"),
        open_order_ids=(original.client_order_id, "foreign"), account_positions=None, account_open_orders=None)
    live.on_evaluation_timer(now + 2 * SECOND)
    assert not submissions(live) and not live.has_exit_risk(now + 2 * SECOND)
    assert F(position["remaining_buy_base"]) == F("0.19")  # Never fabricate an old fill.


@pytest.mark.parametrize("trigger", ["operator_stop", "execution_halt"])
def test_target_position_with_delayed_entry_fill_can_close_before_bookkeeping_catches_up(trigger):
    live, _ = state()
    publish(live)
    buy, sell = submissions(live)
    live.on_receipt(receipt(buy, "unknown"), NOW + SECOND)
    live.on_receipt(receipt(sell, "terminal"), NOW + SECOND)
    if trigger == "operator_stop":
        live.stop_entries(NOW + 2 * SECOND)
    else:
        live.halt_reason = "synthetic inconsistent state"
    now = NOW + 2 * SECOND + 1
    refresh(live, now)
    live.accounts[buy.instrument_id] = replace(live.accounts[buy.instrument_id],
        position_quantity=D("0.3"), bid=D("99.99"), ask=D("100"), account_positions=None, account_open_orders=None)
    live.on_evaluation_timer(now)
    close, = submissions(live)
    assert close.reduce_only and close.side == "SELL" and close.quantity == D("0.3")
    position = next(iter(live.positions.values()))
    fill(live, close, now + 1)
    assert F(position["remaining_buy_base"]) == 0
    assert F(position["unmatched_exit_buy_base"]) == F("0.3")
    fill(live, buy, now + 2, quantity="0.3", trade_id="delayed-entry")
    assert F(position["remaining_buy_base"]) == 0
    assert F(position["unmatched_exit_buy_base"]) == 0
    assert len(live.executions) == 2


def test_recovery_blocked_and_diagnostic_failure_still_drive_confirmed_exits():
    live, _ = state()
    position, _ = open_pair(live)
    live.phase = "recovery_blocked"
    live.halt_reason = "synthetic inconsistent state"
    def fail(event):
        raise RuntimeError("synthetic diagnostic failure")
    live.event_sink = fail
    now = NOW + 2 * SECOND
    refresh(live, now)
    live.on_evaluation_timer(now)
    assert len(submissions(live)) == 2
    assert live.diagnostic_failures and not live.activate(now)


def test_failed_journal_discards_queued_entries_without_leaving_phantom_exit_risk():
    store = MemoryStore()
    live, _ = state(store=store)
    publish(live)
    assert len(live.outbox) == 2
    def fail(*args):
        raise OSError("synthetic disk failure")
    store.save = fail
    live.emit("diagnostic", NOW + 1)
    assert not submissions(live)
    assert all(o["status"] == "local_not_dispatched" for o in live.orders.values())
    assert not live.has_exit_risk(NOW + 2)

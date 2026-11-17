"""An unresolved exit must preserve accounting without stranding verified risk."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F

import pytest

from test_opportunity_live import fill, open_pair, receipt, state, submissions
from test_opportunity_trade import NOW, SECOND, set_book
from opportunity_preflight import AccountSnapshot


def pending_exit():
    live, events = state()
    position, _ = open_pair(live)
    now = NOW + 2 * SECOND
    live.stop_entries(now)
    refresh(live, now + 1)
    live.on_evaluation_timer(now + 1)
    exits = submissions(live)
    buy = next(i for i in exits if i.side == "SELL")
    sell = next(i for i in exits if i.side == "BUY")
    fill(live, sell, now + 2)
    payload = fill(live, buy, now + 3, quantity="0.3", terminal=False)
    timeout = now + live.order_timeout_ns + 5
    live.on_receipt(receipt(buy, "unknown"), timeout)
    live.drain_intents()
    return live, position, buy, payload, timeout, events


def refresh(live, now):
    for i, market in live.markets.items():
        quantity = live.expected_native(i)
        quantity = D(quantity.numerator) / D(quantity.denominator)
        live.on_account(AccountSnapshot(market.venue, i, None, None, now, quantity,
            account_positions=((i, quantity),), account_open_orders=()), now)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "99.28", "99.29", now)


def test_partial_unknown_exit_retries_residual_and_keeps_original_unknown():
    live, position, original, payload, timeout, _ = pending_exit()
    now = timeout + SECOND
    refresh(live, now)
    assert not live._account_ready(original.instrument_id, now)  # Entry admission stays fenced.
    live.on_evaluation_timer(now)
    retry, = submissions(live)
    assert retry.reduce_only and retry.quantity == D("0.69") and retry.side == "SELL"
    assert retry.client_order_id != original.client_order_id
    assert live.orders[original.client_order_id]["terminal_ns"] is None
    assert live.orders[original.client_order_id]["status"] == "unknown"
    # An immediately repeated callback cannot send another reduction.
    refresh(live, now + SECOND)
    live.on_evaluation_timer(now + SECOND)
    assert not submissions(live)
    # A duplicate late trade cannot reduce inventory twice.
    live.on_fill(payload, now + SECOND + 1)
    assert F(position["remaining_buy_base"]) == F("0.69")
    fill(live, retry, now + SECOND + 2)
    refresh(live, now + 2 * SECOND)
    live.on_evaluation_timer(now + 2 * SECOND)
    assert not submissions(live) and F(position["remaining_buy_base"]) == 0
    assert live.positions and live.completed == 0  # Economics still await original proof.
    live.on_receipt(receipt(original, "terminal"), now + 3 * SECOND)
    refresh(live, now + 3 * SECOND + 1)
    live.on_evaluation_timer(now + 3 * SECOND + 1)
    assert not live.positions and live.completed == 1


@pytest.mark.parametrize("problem", ["stale", "not_authoritative", "disconnected", "incomplete",
    "open_order", "other_owner", "not_reduce_only", "accepted", "mismatch"])
def test_secondary_account_and_order_problems_do_not_block_reduce_only_retry(problem):
    live, position, original, _, timeout, _ = pending_exit()
    now = timeout + SECOND
    refresh(live, now)
    i = original.instrument_id
    account = live.accounts[i]
    order = live.orders[original.client_order_id]
    if problem == "stale":
        live.accounts[i] = replace(account, observed_ns=timeout)
    elif problem == "not_authoritative":
        live.accounts[i] = replace(account, authoritative=False)
    elif problem == "disconnected":
        live.accounts[i] = replace(account, connected=False)
    elif problem == "incomplete":
        live.accounts[i] = replace(account, bid=D(100), ask=D("100.01"),
            account_positions=None, account_open_orders=None)
    elif problem == "open_order":
        live.accounts[i] = replace(account, open_order_ids=(original.client_order_id,),
            account_open_orders=((i, original.client_order_id),))
    elif problem == "other_owner":
        order["position_id"] += 100
    elif problem == "not_reduce_only":
        order["reduce_only"] = False
    elif problem == "accepted":
        order["status"] = "accepted"
    else:
        live.accounts[i] = replace(account, position_quantity=D("0.5"), account_positions=((i, D("0.5")),))
    live.on_evaluation_timer(now)
    retry, = submissions(live)
    assert retry.reduce_only and retry.side == "SELL"
    assert retry.quantity == D("0.5" if problem == "mismatch" else "0.69")
    assert not live._account_ready(i, now)
    assert F(position["remaining_buy_base"]) == F("0.69")


def test_zero_fill_retry_is_allowed_again_only_after_next_timeout_and_fresh_read():
    live, _, _, _, timeout, _ = pending_exit()
    now = timeout + SECOND
    refresh(live, now)
    live.on_evaluation_timer(now)
    first, = submissions(live)
    next_timeout = now + live.order_timeout_ns + 1
    live.on_receipt(receipt(first, "unknown"), next_timeout)
    refresh(live, next_timeout + 1)
    live.on_evaluation_timer(next_timeout + 1)
    second, = submissions(live)
    assert second.quantity == first.quantity == D("0.69")
    assert second.client_order_id != first.client_order_id and second.reduce_only


def test_late_additional_original_fill_and_partial_retry_close_exactly_once():
    live, position, original, _, timeout, _ = pending_exit()
    now = timeout + SECOND
    refresh(live, now)
    live.on_evaluation_timer(now)
    retry, = submissions(live)
    assert retry.quantity == D("0.69") and retry.reduce_only
    # An original fill arrives after the retry was sent. The venue's reduce-only
    # cap leaves only 0.49 for that retry; neither receipt can invent 0.69 filled.
    fill(live, original, now + 1, quantity="0.2", trade_id="late-original", terminal=False)
    fill(live, retry, now + 2, quantity="0.49", trade_id="partial-retry")
    assert F(position["remaining_buy_base"]) == 0
    live.on_receipt(receipt(original, "terminal"), now + 3)
    refresh(live, now + SECOND)
    live.on_evaluation_timer(now + SECOND)
    assert not submissions(live) and not live.positions and live.completed == 1


def test_persisted_unknown_without_timestamp_requires_post_timeout_account_read():
    live, _, original, _, timeout, _ = pending_exit()
    del live.orders[original.client_order_id]["unknown_ns"]
    now = timeout + SECOND
    refresh(live, now)
    live.on_evaluation_timer(now)
    retry, = submissions(live)
    assert retry.quantity == D("0.69") and retry.reduce_only

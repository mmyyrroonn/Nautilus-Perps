"""Margin admission and exact create rejection close the original lifecycle."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opportunity_lighter_margin import LighterMarginSnapshot, margin_check
from opportunity_account_reader import AccountReadError, ReadOnlyAccountReader
from opportunity_preflight import ExecutionReceipt
from test_execution_terminal_reliability import lighter, evidence
from test_opportunity_native_execution import harness, event
from test_opportunity_live import state, submissions, fill, receipt, accounts
from test_opportunity_trade import make_plan, set_book, NOW, SECOND
from test_opportunity_history import flat


@pytest.mark.parametrize("actual,allowed", [(None, False), (0, False), (1, True)])
def test_isolated_market_requires_explicit_account_mode(actual, allowed):
    snapshot = LighterMarginSnapshot(NOW, {"FOLKS": 1}, {} if actual is None else {"FOLKS": actual})
    reason, proof = margin_check(snapshot, "FOLKS-PERP.LIGHTER", NOW + 1, SECOND)
    assert (reason is None) == allowed
    assert proof["market_margin_mode"] == 1 and proof["account_margin_mode"] == actual
    assert margin_check(snapshot, "FOLKS-PERP.LIGHTER", NOW + 2 * SECOND, SECOND)[0] == "lighter_margin_stale"


def test_margin_read_failure_preserves_reduction_account_proof():
    def transport(method, url):
        assert method == "GET"
        if "orderBookDetails" in url:
            return {"code": 200, "order_book_details": []}
        assert "active_only=false" in url
        return {"code": 200, "accounts": [{"index": 42, "positions": [],
            "total_order_count": 0, "pending_order_count": 0, "total_isolated_order_count": 0}]}
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"}, transport=transport)
    snapshot = reader.read(NS(venue="LIGHTER", instrument_id="FOLKS-PERP.LIGHTER"), None, None, NOW)
    assert snapshot.authoritative and snapshot.position_quantity == 0
    assert snapshot.account_positions == snapshot.account_open_orders == ()
    assert snapshot.lighter_margin is None and snapshot.lighter_margin_error == "lighter_margin_read_failed"


def test_margin_incompatibility_blocks_both_legs_before_dispatch_and_can_recover():
    live, events = state(make_plan(venues=("HL", "LIGHTER")))
    iid = "BTC.LIGHTER"
    snapshot = LighterMarginSnapshot(NOW, {"BTC": 1}, {})
    live.accounts[iid] = replace(live.accounts[iid], lighter_margin=snapshot)
    set_book(live, "HL", "99.99", "100")
    set_book(live, "LIGHTER", "100.3", "100.31")
    live.evaluate_symbol("BTC", NOW)
    assert not submissions(live) and not live.positions
    assert any(e["event"] == "margin_admission_blocked" for e in events)
    live.accounts[iid] = replace(live.accounts[iid],
        lighter_margin=LighterMarginSnapshot(NOW, {"BTC": 1}, {"BTC": 1}))
    live.evaluate_symbol("BTC", NOW + 1)
    assert len(submissions(live)) == 2


def test_margin_endpoint_cooldown_does_not_block_fresh_position_proof():
    calls = []
    def transport(method, url):
        calls.append(url)
        if "orderBookDetails" in url:
            raise AccountReadError("optional metadata throttled", retry_after_secs=10)
        return {"code": 200, "accounts": [{"index": 42, "positions": [],
            "total_order_count": 0, "pending_order_count": 0, "total_isolated_order_count": 0}]}
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"}, transport=transport,
        clock_ns=lambda: NOW)
    market = NS(venue="LIGHTER", instrument_id="FOLKS-PERP.LIGHTER")
    for stamp in (NOW, NOW + 1):
        reader.invalidate("LIGHTER")
        snapshot = reader.read(market, None, None, stamp)
        assert snapshot.observed_ns == stamp and snapshot.authoritative
        assert snapshot.account_positions == snapshot.account_open_orders == ()
        assert snapshot.lighter_margin is None
    assert len(calls) == 3 and sum("orderBookDetails" in url for url in calls) == 1


def test_exact_rejection_finishes_without_querying_nonexistent_order(harness):
    proof = [None]
    gateway, strategy, receipts, fills, order = lighter(harness, proof)
    proof[0] = {**evidence(order, "0"), "source": "submission_rejected", "venue_order_id": None}
    cached = strategy.cached[order.client_order_id]
    cached.is_closed, cached.status = True, "REJECTED"
    gateway.on_native_event("rejected", event(instrument_id=order.instrument_id,
        reason="LIGHTER_21613: invalid margin mode"), NOW + 1)
    gateway.on_native_event("rejected", event(instrument_id=order.instrument_id), NOW + 2)
    gateway.poll(NOW + 30 * SECOND)
    assert len(receipts) == 1 and receipts[0].kind == "rejected"
    assert receipts[0].final_fill_source == "lighter_submission_rejected"
    assert receipts[0].reason_code == "margin_configuration" and receipts[0].exchange_error_code == 21613
    assert receipts[0].final_filled_quantity == 0 and not fills
    assert [call[0] for call in strategy.calls] == ["submit"]


@pytest.mark.parametrize("bad", ["no_proof", "nonzero", "venue_id", "wrong_id", "conflicting", "canceled"])
def test_unproved_rejection_does_not_release_original_order(harness, bad):
    proof = [None]
    gateway, strategy, receipts, _, order = lighter(harness, proof)
    row = {**evidence(order, "0"), "source": "submission_rejected", "venue_order_id": None}
    if bad == "nonzero": row["final_filled_quantity"] = "0.001"
    if bad == "venue_id": row["venue_order_id"] = "123"
    if bad == "wrong_id": row["client_order_id"] = "different-order"
    if bad == "conflicting": row["conflicting"] = True
    proof[0] = None if bad == "no_proof" else row
    cached = strategy.cached[order.client_order_id]
    cached.is_closed, cached.status = True, "CANCELED" if bad == "canceled" else "REJECTED"
    gateway.on_native_event("rejected", event(instrument_id=order.instrument_id), NOW + 1)
    assert not any(r.kind == "rejected" for r in receipts)
    assert any(call[0] == "query" for call in strategy.calls)


def test_unknown_leg_reduces_other_leg_then_exact_rejection_releases_reserve():
    live, _ = state(make_plan(venues=("HL", "LIGHTER")))
    set_book(live, "HL", "99.99", "100")
    set_book(live, "LIGHTER", "100.3", "100.31")
    live.evaluate_symbol("BTC", NOW)
    filled, unknown = submissions(live)
    assert unknown.venue == "LIGHTER"
    fill(live, filled, NOW + SECOND // 10, quantity="0.8")
    live.on_receipt(receipt(unknown, "unknown"), NOW + SECOND // 10)
    position = next(iter(live.positions.values()))
    deadline = position["opened_ns"] + 10 * SECOND
    accounts(live, deadline)
    set_book(live, "HL", "99.99", "100", deadline)
    set_book(live, "LIGHTER", "100.3", "100.31", deadline)
    live.on_evaluation_timer(deadline)
    unwind, = submissions(live)
    assert unwind.reduce_only and unwind.instrument_id == filled.instrument_id
    now = position["opened_ns"] + 10 * SECOND + 1
    fill(live, unwind, now)
    flat(live, now + 1)
    assert live.gross_exposure(reserve=True) == 200
    live.on_receipt(replace(receipt(unknown, "rejected"), final_filled_quantity=D(0),
        final_fill_source="lighter_submission_rejected"), now + 2)
    flat(live, now + 3)
    live.on_evaluation_timer(now + 4)
    assert live.gross_exposure(reserve=True) == 0
    assert not live.positions and live.orders[unknown.client_order_id]["status"] == "rejected"

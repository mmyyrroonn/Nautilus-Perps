"""Sub-minimum residuals and native pre-submission denials must finish safely."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F

import pytest

from test_execution_terminal_reliability import lighter
from test_opportunity_native_execution import harness, event, intent
from test_opportunity_live import state, fill, submissions, accounts
from test_opportunity_trade import NOW, SECOND, set_book, make_plan
from opportunity_trade import TradePolicy
from opportunity_preflight import ExecutionReceipt


VENUES = ("HL", "ASTER", "LIGHTER", "ONDO", "BACKPACK")


def native_order(harness, venue):
    if venue == "LIGHTER":
        return lighter(harness, [None])
    gateway, strategy, receipts, _, fills = harness
    iid = next(m.instrument_id for m in gateway.markets.values() if m.venue == venue)
    order = intent(venue=venue, instrument_id=iid)
    gateway.dispatch(order, NOW)
    return gateway, strategy, receipts, fills, order


def venue_pair(venue, side):
    peer = "ASTER" if venue == "HL" else "HL"
    buy, sell = (venue, peer) if side == "buy" else (peer, venue)
    live, _ = state(make_plan(venues=(buy, sell)))
    if venue == "BACKPACK":
        live.update_native_admission({venue: {"ready": True, "reason": "ready", "checked_ns": NOW}}, NOW)
    set_book(live, buy, "99.99", "100")
    set_book(live, sell, "100.3", "100.31")
    live.evaluate_symbol("BTC", NOW)
    entries = submissions(live)
    assert len(entries) == 2
    for order in entries:
        fill(live, order, NOW + 100_000_000)
    accounts(live, NOW + 100_000_002)
    live.on_evaluation_timer(NOW + 100_000_002)
    live.drain_intents()
    position, = live.positions.values()
    assert position["status"] == "open"
    return live, position


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("cache_only", [False, True])
def test_native_denial_finishes_without_adapter_query(harness, venue, cache_only):
    gateway, strategy, receipts, fills, order = native_order(harness, venue)
    cached = strategy.cached[order.client_order_id]
    cached.is_closed, cached.status = True, "DENIED"
    if not cache_only:
        gateway.on_native_event("denied", event(instrument_id=order.instrument_id,
            reason="QUANTITY_BELOW_MINIMUM: effective=0.06, min=2.50"), NOW + 1)
    gateway.poll(NOW + 2)
    gateway.poll(NOW + gateway.timeout_ns * 2)
    assert len(receipts) == 1 and receipts[0].kind == "rejected"
    assert receipts[0].final_filled_quantity == 0
    assert receipts[0].final_fill_source == "native_order_denied"
    assert receipts[0].native_status == "denied"
    assert not fills and [call[0] for call in strategy.calls] == ["submit"]


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("conflict", ["venue_id", "accepted", "cached_fill", "processed_fill"])
def test_ambiguous_terminal_cannot_use_local_denial_shortcut(harness, venue, conflict):
    gateway, strategy, receipts, _, order = native_order(harness, venue)
    cached = strategy.cached[order.client_order_id]
    cached.is_closed, cached.status = True, "DENIED"
    if conflict == "venue_id": cached.venue_order_id = "venue-order"
    elif conflict == "accepted": gateway.on_native_event("accepted", event(instrument_id=order.instrument_id), NOW + 1)
    elif conflict == "cached_fill": cached.filled_qty = D("0.001")
    elif conflict == "processed_fill": gateway.orders[order.client_order_id]["processed_qty"] = F("0.001")
    gateway.on_native_event("denied", event(instrument_id=order.instrument_id), NOW + 2)
    gateway.poll(NOW + 3)
    assert not any(receipt.final_fill_source == "native_order_denied" for receipt in receipts)
    assert [call[0] for call in strategy.calls] == ["submit", "query"]


def test_lighter_rejected_still_requires_adapter_evidence(harness):
    gateway, strategy, receipts, _, order = lighter(harness, [None])
    cached = strategy.cached[order.client_order_id]
    cached.is_closed, cached.status = True, "REJECTED"
    gateway.on_native_event("rejected", event(instrument_id=order.instrument_id), NOW + 1)
    gateway.poll(NOW + 2)
    assert not receipts and [call[0] for call in strategy.calls] == ["submit", "query"]


@pytest.mark.parametrize("venue", VENUES)
def test_actual_residual_sizing_respects_cap_and_never_rounds_up(venue):
    peer = "ASTER" if venue == "HL" else "HL"
    live, _ = state(make_plan(venues=(peer, venue)),
                    TradePolicy(slippage_bps=D(0)), price_increment="0.0001")
    iid = f"BTC.{venue}"
    live.update_metadata(iid, replace(live.metadata[iid], min_quantity=D("2.50"),
                         min_notional=D(10)), NOW, evaluate=False)
    set_book(live, venue, "2.5800", "2.5876", NOW, ask_size="100")
    first = live._limit(iid, "BUY", F("38.70"), NOW, partial=True, reduce_only=True)
    assert first["quantity"] == D("38.64") and first["cash"] <= 100
    tail = F("38.70") - first["base_quantity"]
    final = live._limit(iid, "BUY", tail, NOW, partial=True, reduce_only=True)
    assert final["quantity"] == D("0.06") and final["reduce_only"]
    assert live._limit(iid, "BUY", tail, NOW, partial=True, reduce_only=False) is None


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_committed_exit_continues_one_leg_after_equal_partial_fills_and_lost_profit(venue, side):
    live, position = venue_pair(venue, side)
    now = NOW + SECOND
    accounts(live, now)
    set_book(live, live.markets[position["buy_id"]].venue, "99.99", "100", now)
    set_book(live, live.markets[position["sell_id"]].venue, "99.28", "99.29", now)
    live.on_evaluation_timer(now)
    exits = submissions(live)
    assert len(exits) == 2 and position["exit_reason"] == "take_profit"
    for order in exits:
        fill(live, order, now + 1, quantity="0.30")
    later = now + 2 * SECOND
    live.accounts.clear()
    other = "sell" if side == "buy" else "buy"
    live.books.pop(position[other + "_id"])
    iid = position[side + "_id"]
    live.metadata[iid] = replace(live.metadata[iid], taker_fee_bps=None)
    live.update_native_admission({venue: {"ready": False, "reason": "entry_fenced", "checked_ns": later}}, later)
    bid, ask = ("90", "91") if side == "buy" else ("110", "111")
    set_book(live, venue, bid, ask, later)
    live.on_evaluation_timer(later)
    close, = submissions(live)
    assert close.instrument_id == iid and close.reduce_only
    assert F(close.quantity) == F(position[f"remaining_{side}_base"])


@pytest.mark.parametrize("venue", VENUES)
def test_local_native_denial_releases_exit_reservation_without_inventing_fill(venue):
    live, position = venue_pair(venue, "buy")
    now = NOW + SECOND
    live.stop_entries(now)
    live.on_evaluation_timer(now)
    exits = submissions(live)
    order = next(order for order in exits if order.venue == venue)
    remaining = position["remaining_buy_base"]
    live.on_receipt(ExecutionReceipt(order.request_id, order.client_order_id, "rejected",
        final_filled_quantity=D(0), final_fill_source="native_order_denied", native_status="denied"), now + 1)
    assert live.orders[order.client_order_id]["status"] == "rejected"
    assert order.client_order_id in live.validated_final_orders
    assert not live.order_reconciliations
    assert position["remaining_buy_base"] == remaining


@pytest.mark.parametrize("venue", VENUES)
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_forced_exit_sends_available_leg_while_other_book_is_unavailable(venue, side):
    live, position = venue_pair(venue, side)
    now = NOW + 2 * SECOND
    live.stop_entries(now)
    live.accounts.clear()
    other = "sell" if side == "buy" else "buy"
    live.books.pop(position[other + "_id"])
    set_book(live, venue, "99.99", "100", now + 1)
    live.on_evaluation_timer(now + 1)
    close, = submissions(live)
    assert close.instrument_id == position[side + "_id"] and close.reduce_only
    assert close.quantity > 0 and close.quantity * close.price <= live.policy.order_notional_usd

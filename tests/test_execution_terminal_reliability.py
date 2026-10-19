"""Replay terminal/fill permutations and narrowly scoped recovery evidence."""
from decimal import Decimal as D
import json
from types import SimpleNamespace as NS

import pytest

from test_opportunity_native_execution import harness, intent, event
from test_opportunity_live import state, publish, submissions, fill, receipt, MemoryStore, NOW
from opportunity_preflight import AccountSnapshot, ExecutionReceipt


def lighter(harness, proof):
    gateway, strategy, receipts, _, fills = harness
    iid = "BTC-USDC-PERP.LIGHTER"
    gateway.markets[iid] = NS(venue="LIGHTER", instrument_id=iid)
    gateway.native_controls["LIGHTER"] = NS(terminal_fill_evidence_json=lambda cid: json.dumps(proof[0]))
    order = intent(venue="LIGHTER", instrument_id=iid)
    gateway.dispatch(order, NOW)
    return gateway, strategy, receipts, fills, order


def evidence(order, quantity):
    return {"client_order_id": order.client_order_id, "instrument_id": order.instrument_id,
            "final_filled_quantity": quantity, "source": "websocket_terminal_order", "conflicting": False}


def test_lighter_cache_zero_cannot_finish_before_final_evidence(harness):
    proof = [None]
    gateway, strategy, receipts, fills, order = lighter(harness, proof)
    cached = strategy.cached[order.client_order_id]
    cached.is_closed, cached.status = True, "CANCELED"
    gateway.on_native_event("canceled", event(instrument_id=order.instrument_id), NOW + 1)
    gateway.poll(NOW + 2)
    assert not receipts
    gateway.poll(NOW + 11_000_000_000)
    assert [r.kind for r in receipts] == ["unknown"]
    assert [call[0] for call in strategy.calls] == ["submit", "query"]
    proof[0] = evidence(order, ".007")
    for trade_id, quantity in (("one", ".003"), ("two", ".004")):
        cached.filled_qty += D(quantity)
        message = event(instrument_id=order.instrument_id, trade_id=trade_id, order_side="BUY",
                        last_qty=D(quantity), last_px=D(100), commission=None)
        gateway.on_native_event("filled", message, NOW + 12_000_000_000)
        gateway.on_native_event("filled", message, NOW + 12_000_000_001)
    assert len(fills) == 2
    assert [r.kind for r in receipts] == ["unknown", "fill", "fill", "terminal"]
    assert receipts[-1].final_filled_quantity == D(".007")


@pytest.mark.parametrize("fault", ["missing", "wrong_owner", "wrong_market", "conflicting", "mismatch", "nonfinite", "query_failed"])
def test_invalid_final_evidence_stays_pending(harness, fault):
    proof = [None]
    gateway, strategy, receipts, _, order = lighter(harness, proof)
    proof[0] = evidence(order, "0")
    if fault == "missing":
        proof[0] = None
    elif fault == "wrong_owner":
        proof[0]["client_order_id"] = "another-order"
    elif fault == "wrong_market":
        proof[0]["instrument_id"] = "another-market"
    elif fault == "conflicting":
        proof[0]["conflicting"] = True
    elif fault == "mismatch":
        proof[0]["final_filled_quantity"] = ".001"
    elif fault == "nonfinite":
        proof[0]["final_filled_quantity"] = "NaN"
    else:
        def fail(cid):
            raise RuntimeError("query unavailable")
        gateway.native_controls["LIGHTER"].terminal_fill_evidence_json = fail
    strategy.cached[order.client_order_id].is_closed = True
    gateway.on_native_event("canceled", event(instrument_id=order.instrument_id), NOW + 1)
    assert [r.kind for r in receipts] == (["reconciliation_required"] if fault == "conflicting" else [])


def test_explicit_zero_terminal_evidence(harness):
    proof = [None]
    gateway, strategy, receipts, _, order = lighter(harness, proof)
    proof[0] = evidence(order, "0")
    strategy.cached[order.client_order_id].is_closed = True
    gateway.on_native_event("canceled", event(instrument_id=order.instrument_id), NOW + 1)
    assert [r.kind for r in receipts] == ["terminal"]
    assert receipts[0].final_filled_quantity == 0


def test_fill_before_terminal_and_conflicting_final_update(harness):
    proof = [None]
    gateway, strategy, receipts, fills, order = lighter(harness, proof)
    cached = strategy.cached[order.client_order_id]
    cached.filled_qty = D(".01")
    gateway.on_native_event("filled", event(instrument_id=order.instrument_id, trade_id="one",
        order_side="BUY", last_qty=D(".01"), last_px=D(100)), NOW + 1)
    cached.is_closed = True
    gateway.poll(NOW + 2)
    assert [r.kind for r in receipts] == ["fill"]
    proof[0] = evidence(order, ".01")
    gateway.on_native_event("canceled", event(instrument_id=order.instrument_id), NOW + 3)
    assert [r.kind for r in receipts] == ["fill", "terminal"]
    proof[0]["conflicting"] = True
    gateway.poll(NOW + 4)
    assert receipts[-1].kind == "reconciliation_required" and len(fills) == 1


def complete_accounts(live, now, *, complete=True):
    def exact(value):
        return D(value.numerator) / D(value.denominator)
    for market in live.plan.markets:
        quantity = exact(live.expected_native(market.instrument_id))
        positions = tuple((m.instrument_id, exact(live.expected_native(m.instrument_id)))
                          for m in live.plan.markets if m.venue == market.venue)
        live.on_account(AccountSnapshot(market.venue, market.instrument_id, D(100), D(101), now,
            quantity, account_positions=positions if complete else None,
            account_open_orders=() if complete else None), now)


def late_fill(*, hard=False, store=None, fee="0"):
    live, events = state(store=store)
    publish(live, NOW)
    buy, sell = submissions(live)
    live.on_receipt(receipt(buy, "terminal"), NOW + 1)
    live.on_receipt(receipt(sell, "rejected"), NOW + 2)
    if hard:
        live.halt_reason = "conflicting execution replay"
    fill(live, buy, NOW + 3, terminal=False, fee=fee)
    return live, events, buy


def final_proof(live, order, now, quantity=None):
    live.on_receipt(ExecutionReceipt(order.request_id, order.client_order_id, "terminal",
        final_filled_quantity=order.quantity if quantity is None else quantity,
        final_fill_source="lighter_websocket_terminal_order"), now)


@pytest.mark.parametrize("hard", [False, True])
def test_only_complete_order_and_account_proof_recovers_late_fill(hard):
    live, events, order = late_fill(hard=hard, store=MemoryStore())
    assert order.client_order_id in live.order_reconciliations
    complete_accounts(live, NOW + 4)
    assert live.order_reconciliations  # Flatness/positions cannot replace final fills.
    final_proof(live, order, NOW + 5)
    complete_accounts(live, NOW + 6, complete=False)
    assert live.order_reconciliations
    complete_accounts(live, NOW + 7)
    assert not live.order_reconciliations
    assert live.halt_reason == ("conflicting execution replay" if hard else None)
    assert events[-1]["event"] == "order_reconciliation_recovered"
    assert not live.store.saved["order_reconciliations"]
    assert len(live.executions) == 1


def test_mismatched_economics_and_restart_never_clear_guard():
    store = MemoryStore()
    live, _, order = late_fill(store=store, fee=None)
    final_proof(live, order, NOW + 4, D(0))
    complete_accounts(live, NOW + 5)
    assert live.order_reconciliations
    final_proof(live, order, NOW + 6)
    complete_accounts(live, NOW + 7)
    assert live.order_reconciliations  # Unconfirmed fee evidence.
    restored, _ = state(store=MemoryStore(store.saved), activate=False)
    complete_accounts(restored, NOW + 8)
    assert restored.order_reconciliations and not restored.activate(NOW + 9)


def test_restart_flat_archive_cannot_erase_execution_fence(tmp_path):
    from test_opportunity_restart import previous_session, prove_accounts, archives
    from test_opportunity_live_runner import readonly_coordinator
    saved = previous_session(tmp_path, traded=True)
    saved["live"]["order_reconciliations"] = {"old-order": {"since_ns": NOW}}
    coordinator, _, _, store, _ = readonly_coordinator(tmp_path, saved=saved, all_five=True)
    coordinator.tick(NOW)
    prove_accounts(coordinator, NOW + 1)
    coordinator.tick(NOW + 1)
    assert coordinator.proxy.restart_pending and not archives(store)
    assert coordinator.proxy.account_recovery["execution_problem"] == "original_order_final_fill_proof_required"


def test_fresh_order_proof_can_recover_restored_ledger_but_conflict_revokes_it():
    store = MemoryStore()
    live, _, order = late_fill(store=store)
    final_proof(live, order, NOW + 4)
    restored, _ = state(store=MemoryStore(store.saved), activate=False)
    complete_accounts(restored, NOW + 5)
    assert restored.order_reconciliations  # Persisted proof cannot replace a new original-ID query.
    final_proof(restored, order, NOW + 6)
    complete_accounts(restored, NOW + 7)
    assert not restored.order_reconciliations and restored.activate(NOW + 8)
    restored.on_receipt(ExecutionReceipt(order.request_id, order.client_order_id,
                                        "reconciliation_required"), NOW + 9)
    complete_accounts(restored, NOW + 10)
    assert restored.order_reconciliations and restored.halt_reason


def test_lighter_late_fill_requires_original_order_http_confirmation():
    from test_opportunity_trade import make_plan, set_book
    live, _ = state(plan=make_plan(venues=("HL", "LIGHTER")))
    set_book(live, "HL", "99.99", "100")
    set_book(live, "LIGHTER", "100.3", "100.31")
    live.evaluate_symbol("BTC", NOW)
    buy, sell = submissions(live)
    live.on_receipt(receipt(buy, "rejected"), NOW + 1)
    live.on_receipt(receipt(sell, "terminal"), NOW + 2)
    fill(live, sell, NOW + 3, terminal=False)
    final_proof(live, sell, NOW + 4)
    complete_accounts(live, NOW + 5)
    assert live.order_reconciliations
    live.on_receipt(ExecutionReceipt(sell.request_id, sell.client_order_id, "terminal_evidence",
        final_filled_quantity=sell.quantity, final_fill_source="lighter_http_order_status"), NOW + 6)
    complete_accounts(live, NOW + 7)
    assert not live.order_reconciliations and live.halt_reason is None

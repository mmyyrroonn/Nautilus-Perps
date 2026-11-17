"""Inactive old orders release risk without inventing trades or changing new positions."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
from types import SimpleNamespace as NS
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_history import order_has_execution_risk
from opportunity_preflight import AccountSnapshot, ExecutionReceipt
from opportunity_live_runner import LiveCoordinator
from opportunity_loss_monitor import SessionLossMonitor, LossMonitorPolicy
from test_opportunity_live import state, open_pair, fill, receipt, submissions, publish, leverage_snapshot, MemoryStore
from test_opportunity_trade import NOW, SECOND, set_book
from test_opportunity_native_execution import harness, intent, event
from test_opportunity_loss_monitor import balance


def flat(live, now, **changes):
    for market in live.plan.markets:
        account = AccountSnapshot(market.venue, market.instrument_id, None, None, now, D(0),
            account_positions=((market.instrument_id, D(0)),), account_open_orders=(),
            aster_leverage=leverage_snapshot(live.plan.markets, now) if market.venue == "ASTER" else None)
        live.on_account(replace(account, **changes), now)


def historical_candidate():
    live, events = state()
    position, _ = open_pair(live)
    now = NOW + 2 * SECOND
    position.update(status="exit_pending", exit_reason="max_hold", exit_requested_ns=now)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "99.28", "99.29", now)
    live.observe(live.pair_key(position["buy_id"], position["sell_id"]), now)
    exits = submissions(live)
    old = next(i for i in exits if i.side == "SELL")
    other = next(i for i in exits if i.side == "BUY")
    fill(live, other, now + 1)
    fill(live, old, now + 2, quantity="0.3", trade_id="old-part", terminal=False)
    live.on_receipt(receipt(old, "unknown"), now + 3)
    closed = ExecutionReceipt(old.request_id, old.client_order_id, "execution_closed",
        final_filled_quantity=old.quantity, final_fill_source="native_terminal_order")
    live.on_receipt(closed, now + 4)
    live.drain_intents()
    return live, position, old, now + SECOND, events


def test_inactive_unknown_archives_after_flat_and_allows_actual_new_entry():
    live, position, old, now, _ = historical_candidate()
    flat(live, now)
    assert not live.positions and not live.has_exit_risk(now)
    assert live.gross_exposure(reserve=True) == 0
    assert live.completed == 1 and str(position["position_id"]) in live.pending_economics
    assert live.pending_economics[str(position["position_id"])]["net_usd"] is None
    order = live.orders[old.client_order_id]
    assert order["status"] == "unknown" and order["terminal_ns"] is None
    assert not order_has_execution_risk(order)
    assert live.document()["pending_orders"] == 0
    assert live.document()["accounting_pending_orders"] == 1
    # A new post-recovery account read releases any earlier mismatch pause.
    flat(live, now + 1)
    publish(live, now + SECOND)
    new = submissions(live)
    assert len(new) == 2 and all(not i.reduce_only for i in new)
    assert next(iter(live.positions.values()))["position_id"] != position["position_id"]


@pytest.mark.parametrize("problem", ["no_lifecycle_proof", "open_order", "untrusted", "nonzero", "old_snapshot", "incomplete"])
def test_flatness_never_erases_live_or_unproved_original_order(problem):
    live, position, old, now, _ = historical_candidate()
    changes = {}
    if problem == "no_lifecycle_proof":
        live.orders[old.client_order_id].pop("execution_closed")
    elif problem == "open_order":
        changes["account_open_orders"] = ((old.instrument_id, old.client_order_id),)
    elif problem == "untrusted":
        changes["authoritative"] = False
    elif problem == "nonzero":
        changes["position_quantity"] = D("0.1")
    elif problem == "old_snapshot":
        changes["observed_ns"] = NOW
    else:
        changes["account_open_orders"] = None
        changes["account_positions"] = None
        changes["bid"], changes["ask"] = D(99), D(100)
    flat(live, now, **changes)
    assert live.positions and not live.historical_positions
    assert live.orders[old.client_order_id]["status"] == "unknown"


def test_late_historical_fill_only_updates_old_pnl_and_is_deduplicated():
    live, position, old, now, _ = historical_candidate()
    flat(live, now)
    flat(live, now + 1)
    publish(live, now + SECOND)
    new = submissions(live)
    assert len(new) == 2
    current = deepcopy(live.positions)
    fences = dict(live.fences)
    pnl_before = live.realized_net
    payload = fill(live, old, now + 2 * SECOND, quantity=str(old.quantity - D("0.3")),
        trade_id="old-late", fee="0.01", terminal=False)
    assert live.positions == current and live.fences == fences and live.halt_reason is None
    assert live.realized_net == pnl_before + live._pnl(position)
    assert str(position["position_id"]) not in live.pending_economics
    after = live.snapshot()
    live.on_fill(payload, now + 2 * SECOND + 1)
    assert live.snapshot() == after
    live.on_receipt(receipt(old, "terminal"), now + 2 * SECOND + 2)
    assert live.positions == current and live.fences == fences and live.halt_reason is None
    assert live.orders[old.client_order_id]["terminal_ns"] is not None


def test_restore_keeps_old_economics_and_does_not_revive_execution_risk():
    live, position, old, now, _ = historical_candidate()
    flat(live, now)
    restored, _ = state(store=MemoryStore(live.snapshot()), activate=False)
    assert restored.phase == "waiting_preflight" and not restored.positions
    assert not restored.has_exit_risk(now)
    assert restored.orders[old.client_order_id]["status"] == "unknown"
    fill(restored, old, now + SECOND, quantity=str(old.quantity - D("0.3")),
        trade_id="restored-late", terminal=False)
    assert not restored.pending_economics and restored.realized_net == live._pnl(restored._position(position["position_id"]))


def test_history_does_not_hold_loss_monitor_and_preserves_loss_baseline():
    live, _, _, now, _ = historical_candidate()
    flat(live, now)
    actor = NS(live=live, preflight=None, proxy=NS(startup=None))
    assert not LiveCoordinator._has_residual(actor)
    monitor = SessionLossMonitor(LossMonitorPolicy(enabled=True, max_loss_usd=D(10)), ("HL", "ASTER"), 5000)
    monitor.baseline_usd, monitor.status = "2000", "ready"
    gateway = NS(query_balance=lambda venue: None,
        read_balance=lambda venue, stamp, age: balance(venue, stamp, "990"))
    monitor.tick(gateway, live, now, has_residual=LiveCoordinator._has_residual(actor))
    assert monitor.baseline_usd == "2000" and monitor.loss_usd == "20"
    assert monitor.status == "breached" and live.entry_pause_reason == "session_loss_check"


def test_gateway_reports_inactive_order_before_missing_trade_details_and_keeps_querying(harness):
    gateway, strategy, receipts, _, _ = harness
    original = intent()
    gateway.dispatch(original, NOW)
    cached = strategy.cached[original.client_order_id]
    cached.is_closed, cached.status, cached.filled_qty = True, "CANCELED", D(".007")
    gateway.on_native_event("canceled", event(), NOW + 1)
    assert [r.kind for r in receipts] == ["execution_closed"]
    assert receipts[0].final_filled_quantity == D(".007")
    gateway.poll(NOW + 11 * SECOND)
    assert [c[0] for c in strategy.calls].count("query") >= 2
    assert not any(r.kind in {"terminal", "canceled"} for r in receipts)


def test_historical_fee_arrival_updates_pnl_once_without_reapplying_inventory():
    live, position, old, now, _ = historical_candidate()
    flat(live, now)
    payload = fill(live, old, now + 1, quantity=str(old.quantity - D("0.3")),
        trade_id="fee-late", fee=None, terminal=False)
    assert position["unknown_commission"] and live.realized_net == 0
    quantity = live.orders[old.client_order_id]["filled_quantity"]
    cash = position["closing_long_cash_usd"]
    fences = dict(live.fences)
    payload["commission_amount"], payload["commission_currency"] = D("0.02"), "USD"
    live.on_fill(payload, now + 2)
    assert not live.pending_economics and not position["unknown_commission"]
    assert live.realized_net == live._pnl(position)
    assert live.orders[old.client_order_id]["filled_quantity"] == quantity
    assert position["closing_long_cash_usd"] == cash and live.fences == fences
    pnl = live.realized_net
    live.on_fill(payload, now + 3)
    assert live.realized_net == pnl and live.halt_reason is None
    payload["commission_amount"] = D("0.03")
    live.on_fill(payload, now + 4)
    assert live.halt_reason is None and live.pending_economics and live.realized_net == 0
    assert position["historical_evidence_conflicts"]


def test_gateway_missing_fee_can_be_enriched_without_a_second_fill_quantity(harness):
    gateway, strategy, _, _, fills = harness
    order = intent()
    gateway.dispatch(order, NOW)
    message = event(trade_id="fee-late", order_side="BUY", last_qty=D(".003"), last_px=D(100), commission=None)
    cached = strategy.cached[order.client_order_id]
    cached.is_closed, cached.status, cached.filled_qty = True, "CANCELED", D(".003")
    gateway.on_native_event("filled", message, NOW + 1)
    gateway.poll(NOW + gateway.timeout_ns + 1)
    assert any(call[0] == "query" for call in strategy.calls)
    class Fee:
        currency = "USD"
        def as_decimal(self):
            return D("0.01")
    message.commission = Fee()
    gateway.on_native_event("filled", message, NOW + gateway.timeout_ns + 2)
    gateway.on_native_event("filled", message, NOW + gateway.timeout_ns + 3)
    assert len(fills) == 2 and fills[-1]["commission_amount"] == D("0.01")
    assert gateway.orders[order.client_order_id]["processed_qty"] == F(".003")
    before = list(strategy.calls)
    gateway.poll(NOW + 2 * gateway.timeout_ns + 4)
    assert strategy.calls == before


def test_changed_historical_terminal_evidence_invalidates_only_old_pnl():
    live, position, old, now, _ = historical_candidate()
    flat(live, now)
    fill(live, old, now + 1, quantity=str(old.quantity - D("0.3")), trade_id="last", terminal=False)
    assert not live.pending_economics
    live.on_receipt(ExecutionReceipt(old.request_id, old.client_order_id, "terminal_evidence",
        final_filled_quantity=D("0.5"), final_fill_source="native_terminal_order"), now + 2)
    assert live.halt_reason is None and not live.positions
    assert live.pending_economics[str(position["position_id"])]["net_usd"] is None
    assert live.realized_net == 0


@pytest.mark.parametrize("late_fill_fence", [False, True])
def test_normal_close_with_missing_fee_retains_owner_for_later_confirmation(late_fill_fence):
    from test_opportunity_live import accounts, mark
    live, _ = state()
    position, _ = open_pair(live)
    now, exits = mark(live, 5)
    payloads = [fill(live, order, now + 1, fee=None) for order in exits]
    if late_fill_fence:
        live.halt_reason = "late fill after order terminal requires reconciliation"
        live.order_reconciliations[exits[0].client_order_id] = {"since_ns": now + 2}
    accounts(live, now + 3)
    live.on_evaluation_timer(now + 3)
    if late_fill_fence:
        assert live.positions  # A target-only read cannot certify account order absence.
        flat(live, now + 4)
    assert not live.positions and live.pending_economics and live.realized_net == 0
    assert live.halt_reason is None and not live.order_reconciliations
    for payload in payloads:
        live.on_fill({**payload, "commission_amount": D("0.01"), "commission_currency": "USD"}, now + 5)
    assert not live.pending_economics and live.halt_reason is None
    assert live.realized_net == live._pnl(position)


def test_recoverable_accounting_halt_does_not_latch_operator_stop(tmp_path):
    from test_opportunity_loss_monitor import setup, start
    actor, live, _, _, stopped, _ = setup(tmp_path)
    start(actor)
    live.halt_reason = "late fill after order terminal requires reconciliation"
    actor.tick(NOW + 2)
    assert actor.phase == "active" and not live.stopping and not stopped
    live.halt_reason = None
    actor.tick(NOW + 3)
    assert actor.phase == "active" and not live.stopping


def test_history_archive_never_undoes_operator_stop():
    live, _, _, now, _ = historical_candidate()
    live.stop_entries(now)
    flat(live, now + 1)
    flat(live, now + 2)
    publish(live, now + SECOND)
    assert live.stopping and not live.positions and not submissions(live)

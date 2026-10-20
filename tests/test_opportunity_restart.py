"""Restart from current whole-account evidence without replaying old trades."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import json
import sqlite3
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_live import LiveTradeState
from opportunity_live_runner import LiveCoordinator, SessionStoreProxy
from opportunity_preflight import AccountSnapshot, ExecutionReceipt
from opportunity_trade_store import TradeStore
from test_opportunity_live import open_pair, state as trade_state
from test_opportunity_live_runner import NOW, FakeGateway, FakePreflight, MemoryStore, readonly_coordinator
from test_opportunity_loss_monitor import setup as loss_setup

SECOND = 1_000_000_000


def previous_session(tmp_path, *, traded=False):
    _, _, _, store, _ = readonly_coordinator(tmp_path, all_five=True)
    saved = deepcopy(store.saved)
    saved["native_shutdown"] = {"clean": False, "venues": {"BACKPACK": {
        "clean": False, "dirty": True, "positions_unknown_or_nonzero": True,
        "unsent": 0, "unknown": 0, "observed_unreconciled": 0, "pending_cancellations": 0}}}
    if traded:
        old, _ = trade_state()
        open_pair(old)
        saved["live"] = old.snapshot()
        saved["live"]["halt_reason"] = "old execution requires reconciliation"
        saved["startup"] = {"result": "blocked", "owned_quantity": "0.01",
                            "unresolved_client_order_ids": ["previous-btc-order"]}
    return saved


def prove_accounts(coordinator, now, *, fault=None):
    for market in coordinator.account_markets:
        values = dict(account_positions=(), account_open_orders=())
        stamp = now
        if market.venue == "BACKPACK":
            if fault == "missing":
                continue
            if fault == "position":
                values["account_positions"] = (("UNCONFIGURED.BACKPACK", D(1)),)
            elif fault == "order":
                values["account_open_orders"] = (("UNCONFIGURED.BACKPACK", "manual-order"),)
            elif fault == "partial":
                values = {}
            elif fault == "stale":
                stamp = now - 30 * SECOND
            elif fault == "before_start":
                stamp = coordinator.started_ns - 1
            elif fault == "disconnected":
                values["connected"] = False
            elif fault == "unknown_submission":
                # Native readers with unknown submissions cannot assert authority.
                values["authoritative"] = False
        coordinator.on_account(AccountSnapshot(market.venue, market.instrument_id, D(100), D(101),
            stamp, D(0), **values), now)


def archives(store):
    return [event for _, events in store.writes for event in events
            if event["event"] == "previous_session_archived"]


def unresolved_session(tmp_path):
    saved = previous_session(tmp_path, traded=True)
    order = next(iter(saved["live"]["orders"].values()))
    order.update(terminal_ns=None, status="unknown")
    saved["live"]["order_reconciliations"] = {"old-order": {"since_ns": NOW}}
    saved["live"]["pending_economics"] = {"old-trade": {"commission": None}}
    saved["live"]["halt_reason"] = "conflicting execution replay"
    return saved


@pytest.mark.parametrize("fault", ["missing", "position", "order", "partial", "stale",
                                   "before_start", "disconnected", "unknown_submission"])
def test_unknown_history_needs_complete_current_flat_proof_and_stays_archived(tmp_path, fault):
    saved = unresolved_session(tmp_path)
    coordinator, live, gateway, store, _ = readonly_coordinator(tmp_path, saved=saved, all_five=True)
    coordinator.tick(NOW)
    prove_accounts(coordinator, NOW + 1, fault=fault)
    coordinator.tick(NOW + 1)
    assert coordinator.proxy.restart_pending and not archives(store)
    assert store.saved["live"] == saved["live"]
    prove_accounts(coordinator, NOW + 2)
    coordinator.tick(NOW + 2)
    assert coordinator.phase == "active" and not coordinator.proxy.restart_pending
    assert archives(store)[0]["previous_session"] == saved
    assert not live.orders and not live.positions and not live.halt_reason
    assert not live.executions and not live.pending_economics and not live.order_reconciliations
    assert store.saved["live"]["realized_net_usd"] == "0"
    assert all(i.action == "reconcile" for i in gateway.sent)
    assert store.saved["account_recovery"]["recovery_policy"] == "fresh_session_from_flat_accounts"


@pytest.mark.parametrize("traded", [False, True])
def test_manual_cleanup_restarts_only_after_all_current_accounts_are_flat(tmp_path, traded):
    saved = previous_session(tmp_path, traded=traded)
    coordinator, live, gateway, store, stopped = readonly_coordinator(tmp_path, saved=saved, all_five=True)
    reports = []
    coordinator.account_reporter = reports.append
    assert live.session_id != saved["live"]["session_id"]
    assert store.saved["live"] == saved["live"]
    books = dict(live.books)
    live.books.clear()
    coordinator.tick(NOW)
    assert len(gateway.sent) == 5 and live.phase != "active"
    prove_accounts(coordinator, NOW + 1, fault="missing")
    coordinator.tick(NOW + 1)
    assert coordinator.phase == "waiting_account_recovery" and not archives(store)
    assert store.saved["live"] == saved["live"]
    prove_accounts(coordinator, NOW + 2)
    coordinator.tick(NOW + 2)
    assert coordinator.phase == "waiting_books" and not coordinator.proxy.restart_pending
    assert archives(store)[0]["previous_session"] == saved
    assert set(archives(store)[0]["account_recovery"]["read_started_ns"]) == set(coordinator.plan.venues)
    assert store.saved["startup"] is None and store.saved["native_shutdown"] is None
    assert not live.orders and not live.positions and not live.halt_reason
    live.books.update(books)
    coordinator.tick(NOW + 3)
    assert coordinator.phase == "active" and not stopped
    assert len(archives(store)) == 1 and all(i.action == "reconcile" for i in gateway.sent)
    assert reports[0]["result"] == "checking" and reports[-1]["result"] == "passed"


@pytest.mark.parametrize("fault", ["missing", "position", "order", "partial", "stale",
                                   "before_start", "disconnected", "unknown_submission"])
def test_unsafe_current_state_keeps_old_ledger_and_can_be_rechecked(tmp_path, fault):
    saved = previous_session(tmp_path, traded=True)
    coordinator, live, gateway, store, stopped = readonly_coordinator(tmp_path, saved=saved, all_five=True)
    coordinator.tick(NOW)
    prove_accounts(coordinator, NOW + 1, fault=fault)
    coordinator.tick(NOW + 1)
    assert coordinator.phase == "waiting_account_recovery" and live.entry_pause_reason
    assert "BACKPACK" in coordinator.document()["account_recovery"]["account_problems"]
    assert store.saved["live"] == saved["live"] and not archives(store)
    # A slow/manual cleanup never turns a startup timeout into a permanent block.
    later = NOW + (coordinator.plan.scan.connection_timeout_secs + 10) * SECOND
    coordinator.tick(later)
    prove_accounts(coordinator, later + 1)
    coordinator.tick(later + 1)
    assert coordinator.phase == "waiting_books" and not stopped and not coordinator.failure
    assert archives(store)[0]["previous_session"] == saved
    assert all(i.action == "reconcile" for i in gateway.sent)


@pytest.mark.parametrize("retry_after,delay", [(0, 5), (45, 45)])
def test_recovery_read_failure_retries_without_ten_minute_wait(tmp_path, retry_after, delay):
    saved = previous_session(tmp_path)
    coordinator, _, gateway, _, _ = readonly_coordinator(tmp_path, saved=saved, all_five=True)
    coordinator.tick(NOW)
    request = next(i for i in gateway.sent if i.venue == "ASTER")
    coordinator.on_receipt(ExecutionReceipt(request.request_id, "", "unknown",
                                           retry_after_secs=retry_after), NOW)
    coordinator.tick(NOW + delay * SECOND - 1)
    assert len([i for i in gateway.sent if i.venue == "ASTER"]) == 1
    coordinator.tick(NOW + delay * SECOND)
    assert len([i for i in gateway.sent if i.venue == "ASTER"]) == 2
    assert all(i.action == "reconcile" for i in gateway.sent)


@pytest.mark.parametrize("activity", ["fill", "receipt"])
def test_private_activity_during_recovery_requires_newer_account_reads(tmp_path, activity):
    coordinator, _, gateway, store, _ = readonly_coordinator(tmp_path,
        saved=previous_session(tmp_path, traded=True), all_five=True)
    coordinator.tick(NOW)
    prove_accounts(coordinator, NOW + 1)
    if activity == "fill":
        coordinator.on_fill({"client_order_id": "old-manual-fill"}, NOW + 2)
    else:
        coordinator.on_receipt(ExecutionReceipt("old-request", "old-order", "accepted"), NOW + 2)
    coordinator.tick(NOW + 2)
    assert coordinator.phase == "waiting_account_recovery" and not archives(store)
    prove_accounts(coordinator, NOW + 3)
    coordinator.tick(NOW + 3)
    assert coordinator.phase == "active" and len(archives(store)) == 1
    assert all(i.action == "reconcile" for i in gateway.sent)


def test_repeated_restart_never_reuses_saved_passed_proof(tmp_path):
    coordinator, _, _, store, _ = readonly_coordinator(tmp_path,
        saved=previous_session(tmp_path, traded=True), all_five=True)
    coordinator.tick(NOW)
    prove_accounts(coordinator, NOW + 1)
    coordinator.tick(NOW + 1)
    resumed, live, gateway, resumed_store, _ = readonly_coordinator(tmp_path, saved=store.saved, all_five=True)
    resumed.tick(NOW + 2)
    assert resumed.phase == "waiting_account_recovery" and live.phase != "active"
    assert resumed_store.saved["account_recovery"]["result"] == "checking"
    assert len(gateway.sent) == 5 and not archives(resumed_store)


def test_operator_stop_during_recovery_preserves_history_without_cleanup_orders(tmp_path):
    saved = previous_session(tmp_path, traded=True)
    coordinator, live, gateway, store, stopped = readonly_coordinator(tmp_path, saved=saved, all_five=True)
    coordinator.tick(NOW)
    coordinator.request_stop(NOW + 1)
    coordinator.tick(NOW + 2)
    assert stopped == [True] and coordinator.finished and coordinator.failure
    assert store.saved["live"] == saved["live"] and not archives(store)
    assert live.phase != "active" and all(i.action == "reconcile" for i in gateway.sent)


def test_current_account_gate_precedes_opted_in_btc_test(tmp_path):
    coordinator, _, gateway, store, _ = readonly_coordinator(tmp_path,
        saved=previous_session(tmp_path, traded=True), all_five=True)
    coordinator.startup_self_test = True
    preflight = FakePreflight(coordinator.account_markets)
    coordinator.attach(gateway, preflight)
    coordinator.tick(NOW)
    assert preflight.started == 0
    prove_accounts(coordinator, NOW + 1)
    coordinator.tick(NOW + 1)
    assert preflight.started == 1 and coordinator.phase == "preflight"
    assert archives(store)[0]["previous_session"]["startup"]["unresolved_client_order_ids"]


def test_recovery_requires_each_hyperliquid_dex(tmp_path):
    saved = previous_session(tmp_path)
    template, _, _, _, _ = readonly_coordinator(tmp_path, all_five=True)
    market = next(m for m in template.plan.scan.markets if m.venue == "HL")
    dex_market = replace(market, instrument_id="xyz:ABC.HYPERLIQUID", symbol="xyz:ABC")
    plan = replace(template.plan, scan=replace(template.plan.scan,
                    markets=(*template.plan.scan.markets, dex_market)))
    store = MemoryStore(saved)
    proxy = SessionStoreProxy(store, restart_from_flat=True)
    live = LiveTradeState(plan.scan, plan.policy, max_pairs=1, max_gross_notional_usd="500", store=proxy)
    coordinator = LiveCoordinator(plan, live, proxy)
    gateway = FakeGateway()
    coordinator.attach(gateway)
    coordinator.tick(NOW)
    assert len(gateway.sent) == 6
    prove_accounts(coordinator, NOW + 1)
    coordinator.tick(NOW + 1)
    assert coordinator.phase == "waiting_account_recovery"
    assert coordinator.document()["account_recovery"]["account_problems"] == {"HL:xyz": "missing"}
    coordinator.on_account(AccountSnapshot("HL", dex_market.instrument_id, None, None, NOW + 2, D(0),
        account_positions=(), account_open_orders=()), NOW + 2)
    coordinator.tick(NOW + 2)
    assert coordinator.phase == "waiting_books" and len(archives(store)) == 1


def test_traded_restart_takes_new_loss_baseline_after_manual_cleanup(tmp_path):
    saved = previous_session(tmp_path, traded=True)
    coordinator, live, gateway, store, stopped, _ = loss_setup(tmp_path, saved=saved)
    gateway.foreign_order = "ASTER"
    coordinator.tick(NOW)
    coordinator.tick(NOW + 1)
    assert coordinator.loss_monitor.baseline_usd is None and not gateway.queries
    assert store.saved["live"] == saved["live"] and not live.orders
    gateway.foreign_order = None
    coordinator.tick(NOW + 5 * SECOND)
    coordinator.tick(NOW + 5 * SECOND + 1)
    coordinator.tick(NOW + 5 * SECOND + 2)
    assert coordinator.loss_monitor.baseline_usd == "5000" and not stopped
    # Recovery and baseline now reuse the same completed account proof.
    assert coordinator.loss_monitor.checked_ns == NOW + 5 * SECOND + 1
    assert coordinator.loss_monitor.next_check_ns == coordinator.loss_monitor.checked_ns + 600 * SECOND
    assert store.saved["live"]["realized_net_usd"] == "0"
    assert all(i.action == "reconcile" for i in gateway.sent)


def test_sqlite_archival_and_new_session_are_atomic(tmp_path):
    saved = unresolved_session(tmp_path)
    template, _, _, _, _ = readonly_coordinator(tmp_path, all_five=True)
    store = TradeStore(tmp_path / "restart.sqlite3", "offline-restart")
    try:
        store.save(saved, [])
        proxy = SessionStoreProxy(store, restart_from_flat=True)
        live = LiveTradeState(template.plan.scan, template.plan.policy, max_pairs=1,
                              max_gross_notional_usd="500", store=proxy)
        coordinator = LiveCoordinator(template.plan, live, proxy)
        coordinator.attach(FakeGateway())
        coordinator.tick(NOW)
        prove_accounts(coordinator, NOW + 1)
        # Fail after archive insertion but before state replacement.
        store.connection.execute("""CREATE TRIGGER fail_restart BEFORE UPDATE ON state
            WHEN json_extract(NEW.snapshot, '$.live.session_id') != json_extract(OLD.snapshot, '$.live.session_id')
            BEGIN SELECT RAISE(ABORT, 'offline disk failure'); END""")
        with pytest.raises(sqlite3.IntegrityError, match="offline disk failure"):
            coordinator.tick(NOW + 1)
        assert proxy.restart_pending and live.phase != "active"
        assert store.load()["live"] == saved["live"]
        assert not store.connection.execute("SELECT 1 FROM events WHERE kind='previous_session_archived'").fetchall()
        store.connection.execute("DROP TRIGGER fail_restart")
        coordinator.tick(NOW + 2)
        payload, = store.connection.execute("SELECT payload FROM events WHERE kind='previous_session_archived'").fetchone()
        assert json.loads(payload)["previous_session"] == saved
        assert store.load()["live"]["session_id"] == live.session_id
    finally:
        store.close()

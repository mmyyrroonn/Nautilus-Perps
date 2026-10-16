"""Startup evidence is collected once per venue, not expired while peers wait."""
from collections import Counter
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opportunity_balances import BalanceUnavailable, native_balance_snapshot
from opportunity_live_runner import LiveCoordinator
from opportunity_preflight import AccountSnapshot
from test_opportunity_balances import account_cache
from test_opportunity_loss_monitor import NOW, SECOND, VENUES, Gateway, balance, setup


def complete_account(coordinator, venue, stamp, now):
    market = next(m for m in coordinator.account_markets if m.venue == venue)
    coordinator.on_account(AccountSnapshot(venue, market.instrument_id, None, None, stamp, D(0),
        account_positions=(), account_open_orders=()), now)


def test_slow_venue_does_not_expire_completed_account_reads(tmp_path):
    coordinator, live, gateway, _, _, _ = setup(tmp_path)
    coordinator.tick(NOW)
    gateway.pending.clear()
    for venue in VENUES[:-1]:
        complete_account(coordinator, venue, NOW + 1, NOW + 1)
    for seconds in (5, 16, 25, 40):
        coordinator.tick(NOW + seconds * SECOND)
        assert Counter(i.venue for i in gateway.sent) == Counter(VENUES)
        assert set(live.flat_account_problems) == {"BACKPACK"}
    complete_account(coordinator, "BACKPACK", NOW + 40 * SECOND, NOW + 40 * SECOND)
    assert live.flat_accounts_ready(NOW + 40 * SECOND, NOW)
    assert not live.flat_account_problems


def test_completed_balances_survive_timeout_and_retry_of_one_venue(tmp_path):
    coordinator, live, gateway, _, _, _ = setup(tmp_path)
    gateway.bad_balance = "LIGHTER"
    coordinator.tick(NOW)
    coordinator.tick(NOW + 1)
    assert set(coordinator.loss_monitor.collected_venues) == set(VENUES) - {"LIGHTER"}
    assert coordinator.document()["phase"] == "waiting_baseline"
    for seconds in (16, 30, 60):
        coordinator.tick(NOW + seconds * SECOND)
    assert coordinator.loss_monitor.status == "paused"
    assert coordinator.loss_monitor.reasons["timeout"] == "flat_equity_read_timeout"
    for venue in set(VENUES) - {"LIGHTER"}:
        assert gateway.queries.count(venue) == 1
    gateway.bad_balance = None
    coordinator.tick(NOW + 65 * SECOND)
    assert coordinator.loss_monitor.baseline_usd == "5000"
    assert coordinator.loss_monitor.baseline_venues["HL"]["observed_ns"] == NOW + 1


def test_current_invocation_precheck_is_reused_after_slow_node_start(tmp_path):
    original, live, _, store, stopped, _ = setup(tmp_path)
    plan = replace(original.plan, startup_balance_check=True)
    check = {"enabled": True, "result": "passed", "started_ns": NOW, "completed_ns": NOW + 1,
             "venues": {v: {**balance(v, NOW), "result": "passed", "read_ns": NOW + 1} for v in VENUES}}
    report = {"mode": "balance_check_only", "remote_writes_allowed": False, "failure": None,
              "shutdown_complete": True, "balance_check": check}
    coordinator = LiveCoordinator(plan, live, original.proxy, initial_balance=report)
    gateway = Gateway(coordinator)
    gateway.query_error = True
    coordinator.attach(gateway)
    later = NOW + 180 * SECOND
    coordinator.tick(later)
    coordinator.tick(later + 1)
    assert coordinator.loss_monitor.baseline_usd == "5000"
    assert gateway.queries == []
    assert coordinator.loss_monitor.baseline_venues["LIGHTER"]["observed_ns"] == NOW
    assert len(gateway.sent) == len(VENUES)


def test_recovery_proof_is_shared_with_baseline_in_same_tick(tmp_path):
    saved = {"schema_version": 1, "mode": "live", "live": {"positions": [], "orders": {}}}
    coordinator, live, gateway, store, _, _ = setup(tmp_path, saved=saved)
    coordinator.tick(NOW)
    coordinator.tick(NOW + SECOND)
    assert coordinator.proxy.account_recovery["result"] == "passed"
    assert coordinator.loss_monitor.baseline_usd == "5000"
    assert len(gateway.sent) == len(VENUES)


def test_quiet_native_balance_keeps_original_timestamp_and_activity_boundary():
    cache, event = account_cache(stamp=NOW)
    result = native_balance_snapshot(cache, "LIGHTER", "HL-001", NOW + 180 * SECOND,
                                     None, after_ns=NOW)
    assert result["observed_ns"] == NOW
    assert result["read_ns"] == NOW + 180 * SECOND
    for boundary in (NOW + 1, NOW + SECOND):
        with pytest.raises(BalanceUnavailable):
            native_balance_snapshot(cache, "LIGHTER", "HL-001", NOW + 180 * SECOND,
                                    None, after_ns=boundary)
    event.ts_init = NOW + 181 * SECOND
    with pytest.raises(BalanceUnavailable):
        native_balance_snapshot(cache, "LIGHTER", "HL-001", NOW + 180 * SECOND, None, after_ns=NOW)


def test_own_activity_invalidates_only_the_affected_collected_balance(tmp_path):
    coordinator, live, gateway, _, _, _ = setup(tmp_path)
    gateway.bad_balance = "LIGHTER"
    coordinator.tick(NOW)
    coordinator.tick(NOW + 1)
    instrument = next(m.instrument_id for m in coordinator.account_markets if m.venue == "HL")
    live.fences[instrument] = NOW + SECOND
    complete_account(coordinator, "HL", NOW + 2 * SECOND, NOW + 2 * SECOND)
    gateway.balance_stamp = NOW
    coordinator.tick(NOW + 3 * SECOND)
    assert "HL" not in coordinator.loss_monitor.collected_venues
    assert set(coordinator.loss_monitor.collected_venues) == {"ASTER", "ONDO", "BACKPACK"}
    gateway.balance_stamp = gateway.bad_balance = None
    coordinator.tick(NOW + 4 * SECOND)
    assert coordinator.loss_monitor.baseline_usd == "5000"
    assert coordinator.loss_monitor.baseline_venues["HL"]["observed_ns"] == NOW + 4 * SECOND


def test_invalid_account_observation_revokes_fixed_flat_proof(tmp_path):
    coordinator, live, gateway, _, _, _ = setup(tmp_path)
    coordinator.tick(NOW)
    coordinator.tick(NOW + 1)
    instrument = next(m.instrument_id for m in coordinator.account_markets if m.venue == "LIGHTER")
    live.on_account(replace(live.accounts[instrument], connected=False), NOW + SECOND)
    assert not live.flat_accounts_ready(NOW + 20 * SECOND, NOW)
    assert "LIGHTER" in live.flat_account_problems


def test_native_monitor_accepts_quiet_session_event_but_not_previous_session(tmp_path):
    coordinator, live, gateway, _, _, _ = setup(tmp_path)
    monitor = coordinator.loss_monitor
    monitor.balance_epoch_ns = NOW
    boundaries = []
    def read_since(venue, now, boundary):
        boundaries.append(boundary)
        return balance(venue, NOW)
    gateway.read_balance_since = read_since
    coordinator.tick(NOW + 180 * SECOND)
    coordinator.tick(NOW + 180 * SECOND + 1)
    assert monitor.baseline_usd == "5000" and set(boundaries) == {NOW}
    coordinator, live, gateway, _, _, _ = setup(tmp_path)
    coordinator.loss_monitor.balance_epoch_ns = NOW + 1
    gateway.read_balance_since = read_since
    coordinator.tick(NOW + 180 * SECOND)
    coordinator.tick(NOW + 180 * SECOND + 1)
    assert coordinator.loss_monitor.baseline_usd is None
    assert set(coordinator.loss_monitor.reasons.values()) == {"equity_stale_or_before_activity"}


def test_running_check_reads_updated_balances_instead_of_fixed_startup_values(tmp_path):
    coordinator, live, gateway, _, _, _ = setup(tmp_path)
    monitor = coordinator.loss_monitor
    monitor.balance_epoch_ns = NOW
    source_stamp = NOW
    gateway.read_balance_since = lambda venue, now, boundary: balance(
        venue, source_stamp, gateway.amounts[venue])
    coordinator.tick(NOW)
    coordinator.tick(NOW + 1)
    assert monitor.baseline_usd == "5000"
    gateway.amounts["HL"] = "790"
    source_stamp = NOW + 20 * SECOND
    instrument = next(m.instrument_id for m in coordinator.account_markets if m.venue == "HL")
    live.fences[instrument] = NOW + 10 * SECOND
    due = monitor.next_check_ns
    coordinator.tick(due)
    coordinator.tick(due + 1)
    assert monitor.status == "breached" and monitor.loss_usd == "210"
    assert coordinator.stop_reason == "session_loss_limit"

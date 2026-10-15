"""Startup balance gating without credentials, sockets or order mutations."""
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_balances import BalanceUnavailable, StartupBalanceCheck, native_balance_snapshot
from opportunity_live_runner import LiveCoordinator, load_live_plan, main
from test_opportunity_live_runner import NOW, configs, coordinator, readonly_coordinator


VENUES = ("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK")


def money(value, currency="USDC"):
    return NS(currency=currency, as_decimal=lambda: D(value))


def event_balance(total="123.45678901", free="120.45678901", locked="3", currency="USDC"):
    return NS(currency=currency, total=money(total, currency), free=money(free, currency),
              locked=money(locked, currency))


def account_cache(*, rows=None, stamp=NOW, reported=True):
    event = NS(account_id="HL-001", is_reported=reported, ts_init=stamp, ts_event=0,
               balances=[event_balance()] if rows is None else rows)
    # Old cached currencies must not be stamped with the latest partial update.
    account = NS(id="HL-001", last_event=event, balances=lambda: {"OLD": event_balance()})
    return NS(account=lambda account_id: account), event


@pytest.mark.parametrize("venue", VENUES)
def test_snapshot_preserves_native_amounts_currency_and_timestamp(venue):
    cache, event = account_cache()
    snapshot = native_balance_snapshot(cache, venue, "HL-001", NOW + 2, 15000)
    assert snapshot["balances"] == [{"currency": "USDC", "total": "123.45678901",
                                     "free": "120.45678901", "locked": "3"}]
    assert snapshot["observed_ns"] == NOW and snapshot["event_ns"] == 0
    assert snapshot["read_ns"] == NOW + 2 and snapshot["venue"] == venue
    assert "HL-001" not in json.dumps(snapshot)
    assert "margin" not in snapshot["balances"][0]


@pytest.mark.parametrize("bad", ["missing", "missing_event", "unreported", "stale", "future",
                                 "identity", "nonfinite", "inconsistent", "currency", "duplicate"])
def test_missing_stale_or_invalid_balance_never_becomes_zero_or_success(bad):
    cache, event = account_cache()
    if bad == "missing":
        cache.account = lambda _: None
    elif bad == "missing_event":
        cache.account("HL-001").last_event = None
    elif bad == "unreported":
        event.is_reported = False
    elif bad == "stale":
        event.ts_init = NOW - 15_000_000_001
    elif bad == "future":
        event.ts_init = NOW + 1
    elif bad == "identity":
        event.account_id = "WRONG-001"
    elif bad == "nonfinite":
        event.balances[0].total = money("NaN")
    elif bad == "inconsistent":
        event.balances[0].free = money("999")
    elif bad == "currency":
        event.balances[0].free = money("120.45678901", "USD")
    else:
        event.balances.append(event.balances[0])
    with pytest.raises(BalanceUnavailable):
        native_balance_snapshot(cache, "HL", "HL-001", NOW, 15000)


def test_explicit_zero_negative_and_empty_events_remain_distinct():
    for rows in ([], [event_balance("0", "0", "0")], [event_balance("-2", "-5", "3")]):
        cache, _ = account_cache(rows=rows)
        result = native_balance_snapshot(cache, "HL", "HL-001", NOW, 15000)
        assert len(result["balances"]) == len(rows)
        if rows:
            assert D(result["balances"][0]["total"]) == rows[0].total.as_decimal()


class BalanceGateway:
    def __init__(self, ready=(), *, bad_query=False):
        self.ready = set(ready)
        self.queries, self.reads = [], []
        self.bad_query = bad_query

    def query_balance(self, venue):
        self.queries.append(venue)
        if self.bad_query:
            raise RuntimeError("PRIVATE RESPONSE MUST NOT APPEAR")

    def read_balance(self, venue, now_ns, max_age_ms):
        self.reads.append(venue)
        if venue not in self.ready:
            raise BalanceUnavailable("account_not_observed")
        return {"venue": venue, "observed_ns": now_ns, "balances": [
            {"currency": "USDC", "total": "0", "free": "0", "locked": "0"}]}


def test_five_reads_are_independent_bounded_and_terminal_results_do_not_requery():
    check = StartupBalanceCheck(VENUES, enabled=True, timeout_secs=2, max_age_ms=15000)
    gateway = BalanceGateway(VENUES[:-1])
    check.tick(gateway, NOW)
    assert check.result == "running" and gateway.queries == list(VENUES)
    check.tick(gateway, NOW + 2_000_000_000)
    assert check.result == "failed"
    rows = check.document()["venues"]
    assert [rows[v]["result"] for v in VENUES] == ["passed"] * 4 + ["failed"]
    assert "balances" not in rows["BACKPACK"]
    reads = list(gateway.reads)
    check.tick(gateway, NOW + 3_000_000_000)
    assert gateway.reads == reads and gateway.queries == list(VENUES)


def test_query_error_is_sanitized_and_disabled_check_does_nothing():
    check = StartupBalanceCheck(VENUES, enabled=True, timeout_secs=2, max_age_ms=15000)
    check.tick(BalanceGateway(bad_query=True), NOW)
    assert check.result == "failed"
    assert "PRIVATE" not in json.dumps(check.document())
    disabled = StartupBalanceCheck(VENUES, enabled=False, timeout_secs=2, max_age_ms=15000)
    disabled.tick(None, NOW)
    assert disabled.result == "disabled" and disabled.document()["venues"] == {}


def test_balance_arriving_after_deadline_cannot_turn_timeout_into_success():
    check = StartupBalanceCheck(VENUES, enabled=True, timeout_secs=2, max_age_ms=15000)
    gateway = BalanceGateway()
    check.tick(gateway, NOW)
    gateway.ready.update(VENUES)
    check.tick(gateway, NOW + 2_000_000_001)
    assert check.result == "failed" and len(gateway.reads) == len(VENUES)


def test_config_and_cli_toggle_are_independent_and_do_not_change_ledger_identity(tmp_path, capsys):
    path = configs(tmp_path, mandatory="false")
    disabled = load_live_plan(path)
    assert not disabled.startup_balance_check
    path.write_text(path.read_text().replace("[startup]", "[startup]\nbalance_check_enabled = true"))
    enabled = load_live_plan(path)
    assert enabled.startup_balance_check and not enabled.startup_self_test
    assert disabled.fingerprint() == enabled.fingerprint()
    assert main(["--config", str(path), "--no-startup-balance-check"]) == 0
    assert not json.loads(capsys.readouterr().out)["startup"]["balance_check_enabled"]
    assert main(["--config", str(path), "--startup-balance-check"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["startup"]["balance_check_enabled"] and not document["startup"]["self_test_enabled"]
    assert not document["runtime_started"] and not (tmp_path / "state").exists()
    path.write_text(path.read_text().replace("balance_check_enabled = true", 'balance_check_enabled = "true"'))
    with pytest.raises(ValueError, match="boolean"):
        load_live_plan(path)


def enable_balances(state, live, gateway, stopped):
    plan = replace(state.plan, startup_balance_check=True)
    reports = []
    result = LiveCoordinator(plan, live, state.proxy, stop=lambda: stopped.append(True),
                             balance_reporter=reports.append)
    result.attach(gateway, state.preflight)
    bridge = BalanceGateway()
    gateway.query_balance, gateway.read_balance = bridge.query_balance, bridge.read_balance
    return result, bridge, reports


def test_balance_step_precedes_books_account_gate_and_never_activates_trading_itself(tmp_path):
    state, live, gateway, store, stopped = readonly_coordinator(tmp_path)
    state, balances, reports = enable_balances(state, live, gateway, stopped)
    live.books.clear()
    state.tick(NOW)
    assert state.phase == "waiting_balances" and not gateway.sent and live.phase != "active"
    assert store.saved["balance_check"]["result"] == "running"
    balances.ready.update(state.plan.venues)
    state.tick(NOW + 1)
    assert state.phase == "waiting_books" and live.phase != "active" and not gateway.sent
    assert reports[0]["result"] == "passed" and store.saved["balance_check"]["result"] == "passed"
    assert store.saved["account_gate"] is None and store.saved["startup"] is None
    state.tick(NOW + 2)
    assert len(reports) == 1


@pytest.mark.parametrize("success", [False, True])
def test_balance_failure_blocks_btc_self_test_success_proceeds_to_it(tmp_path, success):
    state, live, preflight, gateway, store, stopped = coordinator(tmp_path)
    state, balances, reports = enable_balances(state, live, gateway, stopped)
    if success:
        balances.ready.update(state.plan.venues)
    state.tick(NOW)
    if success:
        assert preflight.started == 1 and state.phase == "preflight"
        assert reports[0]["result"] == "passed"
    else:
        assert preflight.started == 0 and not gateway.sent
        state.tick(NOW + state.plan.startup_timeout_secs * 1_000_000_000)
        assert state.finished and state.failure and stopped
        assert preflight.started == 0 and not live.activated and not gateway.sent
        assert reports[0]["result"] == "failed"


def test_restart_does_not_reuse_previous_balance_success_and_stop_interrupts_read(tmp_path):
    state, live, gateway, store, stopped = readonly_coordinator(tmp_path)
    state, balances, _ = enable_balances(state, live, gateway, stopped)
    balances.ready.update(state.plan.venues)
    state.tick(NOW)
    saved = store.saved
    assert saved["balance_check"]["result"] == "passed"
    state, live, gateway, store, stopped = readonly_coordinator(tmp_path, saved=saved)
    state, balances, reports = enable_balances(state, live, gateway, stopped)
    state.tick(NOW + 1)
    assert state.phase == "waiting_balances" and store.saved["balance_check"]["result"] == "running"
    state.request_stop(NOW + 2)
    assert reports[0]["result"] == "interrupted" and live.phase != "active"
    assert store.saved["balance_check"]["result"] == "interrupted"


def test_disabled_startup_never_calls_balance_gateway(tmp_path):
    state, live, gateway, _, _ = readonly_coordinator(tmp_path)
    def forbidden(*args):
        raise AssertionError("disabled balance check made a request")
    gateway.query_balance = gateway.read_balance = forbidden
    state.tick(NOW)
    assert state.phase == "waiting_accounts" and state.document()["balance_check"]["result"] == "disabled"


def test_successful_balances_still_need_all_account_proofs_before_activation(tmp_path):
    from opportunity_preflight import AccountSnapshot
    state, live, gateway, store, stopped = readonly_coordinator(tmp_path)
    state, balances, reports = enable_balances(state, live, gateway, stopped)
    balances.ready.update(state.plan.venues)
    state.tick(NOW)
    assert state.phase == "waiting_accounts" and live.phase != "active"
    assert all(intent.action == "reconcile" for intent in gateway.sent)
    for market in state.account_markets:
        state.on_account(AccountSnapshot(market.venue, market.instrument_id, D(100), D("100.02"),
                                         NOW + 1, D(0)), NOW + 1)
    state.tick(NOW + 2)
    assert state.phase == "active" and not stopped
    assert store.saved["balance_check"]["result"] == store.saved["account_gate"]["result"] == "passed"
    assert store.saved["startup"] is None and not live.orders and len(reports) == 1


def test_enabled_balances_cannot_override_blocked_trading_recovery(tmp_path):
    saved = {"schema_version": 1, "startup_complete": False,
             "startup": {"result": "blocked", "owned_quantity": "0.001"},
             "balance_check": {"result": "passed"}, "live": {"positions": []}}
    state, live, preflight, gateway, store, stopped = coordinator(tmp_path, saved=saved)
    state, balances, _ = enable_balances(state, live, gateway, stopped)
    balances.ready.update(state.plan.venues)
    state.tick(NOW)
    assert state.phase == "resume_readonly" and state.failure and stopped
    assert not balances.queries and not gateway.sent and not live.activated and preflight.started == 0
    assert store.saved == saved

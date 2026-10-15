"""CLI and lifecycle checks for the independent, finite balance-only session."""
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opportunity_balance_runner as runner
from opportunity_balances import StartupBalanceCheck
from opportunity_live_runner import load_live_plan, main
from test_opportunity_balances import BalanceGateway
from test_opportunity_live_runner import configs, NOW


@pytest.mark.parametrize("fault", ["failed", "dirty_shutdown", "missing_venue", "malformed"])
def test_initial_balance_failure_never_constructs_live_clients_or_claims_a_ledger(
        tmp_path, monkeypatch, capsys, fault):
    from dataclasses import replace
    import opportunity_account_scope as scope
    import opportunity_live_runner as live_runner
    from test_opportunity_account_scope import credentials
    plan = replace(load_live_plan(configs(tmp_path)), startup_balance_check=True)
    report = {"mode": "balance_check_only", "remote_writes_allowed": False,
        "failure": None, "shutdown_complete": True, "balance_check": {
            "enabled": True, "result": "passed", "started_ns": NOW, "completed_ns": NOW + 1,
            "venues": {venue: {"result": "passed"} for venue in plan.venues}}}
    if fault == "failed":
        report["balance_check"]["result"] = "failed"
    elif fault == "dirty_shutdown":
        report["shutdown_complete"] = False
    elif fault == "missing_venue":
        report["balance_check"]["venues"].pop(plan.venues[0])
    else:
        report["balance_check"] = None
    monkeypatch.setitem(sys.modules, "dotenv", NS(load_dotenv=lambda *a, **kw: None))
    monkeypatch.setattr(live_runner.os, "environ", credentials())
    monkeypatch.setattr(runner, "run_startup_balance_session", lambda *a: report)
    monkeypatch.setattr(scope, "AccountRiskGuard", lambda *a: pytest.fail("claimed live account"))
    monkeypatch.setattr(live_runner, "build_live_node", lambda *a, **kw: pytest.fail("created live clients"))
    assert live_runner.run_live(plan) == 1
    assert not plan.state_path.exists()
    assert json.loads(capsys.readouterr().out)["event"] == "startup_balance_check"


@pytest.mark.parametrize("outcome", ["passed", "timeout", "worker_hung", "worker_crashed", "interrupted"])
def test_isolated_balance_worker_must_exit_before_returning_a_success(tmp_path, monkeypatch, outcome):
    import multiprocessing
    plan = load_live_plan(configs(tmp_path))
    calls = []
    report = {"mode": "balance_check_only", "remote_writes_allowed": False,
        "failure": None, "shutdown_complete": True, "balance_check": {
            "enabled": True, "result": "passed", "started_ns": NOW, "completed_ns": NOW + 1,
            "venues": {venue: {"result": "passed"} for venue in plan.venues}}}
    class Process:
        exitcode = None if outcome in {"timeout", "worker_hung", "interrupted"} else 1 if outcome == "worker_crashed" else 0
        def start(self): calls.append("start")
        def join(self, *, timeout): calls.append("join")
        def is_alive(self): return self.exitcode is None
        def terminate(self):
            calls.append("terminate")
            self.exitcode = -15
        def kill(self): pytest.fail("worker did not terminate")
        def close(self): calls.append("process_close")
    def poll(timeout):
        assert timeout > plan.scan.connection_timeout_secs + plan.startup_timeout_secs
        if outcome == "interrupted":
            raise KeyboardInterrupt
        return outcome != "timeout"
    receiver = NS(poll=poll, recv=lambda: report, close=lambda: calls.append("receiver_close"))
    sender = NS(close=lambda: calls.append("sender_close"))
    def process_factory(*, target, args):
        assert target is runner._balance_worker and args == (plan, {"SYNTHETIC": "value"}, sender)
        return Process()
    def get_context(mode):
        assert mode == "spawn"
        return NS(Pipe=lambda **kw: (receiver, sender), Process=process_factory)
    monkeypatch.setattr(multiprocessing, "get_context", get_context)
    result = runner.run_startup_balance_session(plan, {"SYNTHETIC": "value"})
    assert runner.balance_session_passed(result, plan.venues) == (outcome == "passed")
    assert "process_close" in calls and "receiver_close" in calls
    assert ("terminate" in calls) == (outcome in {"timeout", "worker_hung", "interrupted"})


def test_real_spawn_serializes_plan_and_refuses_empty_credentials_without_loading_dotenv(tmp_path):
    plan = load_live_plan(configs(tmp_path))
    report = runner.run_startup_balance_session(plan, {})
    assert report["failure_stage"] == "credentials"
    assert report["failure"].startswith("missing Hyperliquid private key:")
    assert report["remote_writes_allowed"] is False
    assert not runner.balance_session_passed(report, plan.venues)
    assert all(row["result"] == "not_run" for row in report["balance_check"]["venues"].values())
    assert not plan.state_path.exists()


def test_balance_only_cli_overrides_configured_trading_self_test(tmp_path, monkeypatch):
    path = configs(tmp_path, mandatory="true", pairs=0, gross="0")
    received = []
    monkeypatch.setattr(runner, "run_balance_check", lambda plan: received.append(plan) or 0)
    assert main(["--config", str(path), "--balance-check-only"]) == 0
    assert len(received) == 1 and received[0].startup_balance_check
    assert not received[0].startup_self_test and not received[0].ready
    assert not (tmp_path / "state").exists()


def test_balance_only_dry_run_does_not_load_runtime(tmp_path, monkeypatch, capsys):
    import builtins
    original = builtins.__import__
    def guarded(name, *args, **kwargs):
        if name.startswith(("opportunity_balance_runner", "dotenv", "nautilus_trader")):
            raise AssertionError("dry-run imported a runtime")
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded)
    path = configs(tmp_path)
    assert main(["--config", str(path), "--balance-check-only", "--dry-run"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["mode"] == "balance_check_only" and not doc["runtime_started"]
    assert not doc["remote_writes_allowed"] and not doc["startup"]["self_test_enabled"]
    assert not (tmp_path / "state").exists()


@pytest.mark.parametrize("flag", ["--execute-live", "--startup-self-test", "--self-test-only",
                                  "--no-startup-balance-check"])
def test_conflicting_balance_only_flags_refuse_before_runtime(tmp_path, monkeypatch, flag):
    monkeypatch.setattr(runner, "run_balance_check", lambda _: pytest.fail("runtime started"))
    assert main(["--config", str(configs(tmp_path)), "--balance-check-only", flag]) == 2


@pytest.mark.parametrize("outcome", ["passed", "failed", "crash", "interrupted", "dispose_failed", "connect_timeout", "not_ready"])
def test_balance_only_session_disposes_without_a_trading_ledger(tmp_path, monkeypatch, capsys, outcome):
    import opportunity_account_scope as scope
    import opportunity_trade_store
    from test_opportunity_account_scope import credentials
    plan = load_live_plan(configs(tmp_path, mandatory="true"))
    monkeypatch.setitem(sys.modules, "dotenv", NS(load_dotenv=lambda *a, **kw: None))
    monkeypatch.setattr(runner.os, "environ", credentials())
    monkeypatch.setattr(runner, "prepare_balance_environment", lambda venues, environment, env: env)
    monkeypatch.setattr(scope, "verify_execution_identity", lambda *a: None)
    monkeypatch.setattr(opportunity_trade_store, "TradeStore", lambda *a, **kw: pytest.fail("opened trading ledger"))
    check = StartupBalanceCheck(plan.venues, enabled=True, timeout_secs=20, max_age_ms=15000)
    calls = []
    def run():
        if outcome == "connect_timeout":
            raise RuntimeError("exec-connect timeout")
        if outcome == "not_ready":
            raise RuntimeError("readiness timeout while waiting for engine connections; failed to finalize startup abort: PRIVATE RESPONSE")
        if outcome == "crash":
            raise RuntimeError("PRIVATE RESPONSE")
        if outcome == "interrupted":
            raise KeyboardInterrupt()
        check.tick(BalanceGateway(plan.venues if outcome != "failed" else ()), NOW)
        if outcome == "failed":
            check.tick(BalanceGateway(), NOW + 20_000_000_000)
    def dispose():
        calls.append("dispose")
        if outcome == "dispose_failed":
            raise RuntimeError("PRIVATE RESPONSE")
    node = NS(run=run, handle=lambda: NS(stop=lambda: calls.append("stop")), dispose=dispose)
    def build(received, environ, directory):
        assert received is plan and directory.is_absolute()
        assert ".native-cache" in directory.parts and directory != plan.state_path.parent
        return node, NS(failure=None), check
    monkeypatch.setattr(runner, "build_balance_node", build)
    assert runner.run_balance_check(plan) == (0 if outcome == "passed" else 1)
    document = json.loads(capsys.readouterr().out)
    assert document["mode"] == "balance_check_only" and not document["remote_writes_allowed"]
    assert document["shutdown_complete"] == (outcome != "dispose_failed")
    assert calls == ["stop", "dispose"] and "PRIVATE RESPONSE" not in json.dumps(document)
    if outcome in {"crash", "interrupted", "connect_timeout", "not_ready"}:
        assert document["failure_stage"] == "native_startup"
        assert all(row["result"] == "not_run" for row in document["balance_check"]["venues"].values())
    if outcome == "connect_timeout":
        assert document["failure_code"] == "connection_timeout"
    if outcome == "not_ready":
        assert document["failure_code"] == "clients_not_ready"
    if outcome == "dispose_failed":
        assert document["failure_stage"] == "shutdown"
    assert not plan.state_path.exists()


def test_legacy_hyperliquid_name_and_owner_discovery_are_readonly_local_aliases(monkeypatch):
    original = {"HYPERLIQUID_PRIVATE_KEY": "synthetic-key"}
    calls = []
    owner = "0x" + "ab" * 20
    monkeypatch.setattr(runner, "hyperliquid_owner_address",
        lambda key, environment, transport: calls.append((key, environment)) or owner)
    env = runner.prepare_balance_environment(("HL",), "mainnet", original)
    assert env["HYPERLIQUID_PK"] == original["HYPERLIQUID_PRIVATE_KEY"]
    assert env["HYPERLIQUID_ACCOUNT_ADDRESS"] == owner
    assert calls == [("synthetic-key", "mainnet")]
    assert list(original) == ["HYPERLIQUID_PRIVATE_KEY"]


def test_explicit_hyperliquid_account_and_canonical_key_are_not_overridden(monkeypatch):
    env = {"HYPERLIQUID_PK": "explicit-key", "HYPERLIQUID_PRIVATE_KEY": "legacy-key",
           "HYPERLIQUID_ACCOUNT_ADDRESS": "0x" + "ab" * 20}
    monkeypatch.setattr(runner, "hyperliquid_owner_address", lambda *a: pytest.fail("unexpected discovery"))
    assert dict(runner.prepare_balance_environment(("HL",), "mainnet", env)) == env
    testnet = runner.prepare_balance_environment(("LIGHTER",), "testnet", env)
    assert "HYPERLIQUID_TESTNET_PK" not in testnet


def test_balance_setup_failures_are_actionable_without_exposing_credentials(tmp_path, monkeypatch, capsys):
    import opportunity_account_scope as scope
    plan = load_live_plan(configs(tmp_path))
    monkeypatch.setitem(sys.modules, "dotenv", NS(load_dotenv=lambda *a, **kw: None))
    monkeypatch.setattr(runner.os, "environ", {})
    monkeypatch.setattr(runner, "build_balance_node", lambda *a: pytest.fail("native build reached"))
    assert runner.run_balance_check(plan) == 1
    report = json.loads(capsys.readouterr().out)
    assert report["failure_stage"] == "credentials"
    assert "HYPERLIQUID_PRIVATE_KEY" in report["failure"]
    assert not report["shutdown_complete"]
    def refused(*a):
        raise ValueError("Hyperliquid signer ownership could not be verified for the bound account")
    monkeypatch.setattr(scope, "verify_execution_identity", refused)
    with pytest.raises(runner.BalanceSetupError, match="not the API agent address"):
        runner.verify_balance_identity(None, {})

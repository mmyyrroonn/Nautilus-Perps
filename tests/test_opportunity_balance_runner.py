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


@pytest.mark.parametrize("outcome", ["passed", "failed", "crash", "interrupted", "dispose_failed"])
def test_balance_only_session_disposes_without_a_trading_ledger(tmp_path, monkeypatch, capsys, outcome):
    import opportunity_account_scope as scope
    import opportunity_trade_store
    from test_opportunity_account_scope import credentials
    plan = load_live_plan(configs(tmp_path, mandatory="true"))
    monkeypatch.setitem(sys.modules, "dotenv", NS(load_dotenv=lambda *a, **kw: None))
    monkeypatch.setattr(runner.os, "environ", credentials())
    monkeypatch.setattr(scope, "verify_execution_identity", lambda *a: None)
    monkeypatch.setattr(opportunity_trade_store, "TradeStore", lambda *a, **kw: pytest.fail("opened trading ledger"))
    check = StartupBalanceCheck(plan.venues, enabled=True, timeout_secs=20, max_age_ms=15000)
    calls = []
    def run():
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
    assert not plan.state_path.exists()

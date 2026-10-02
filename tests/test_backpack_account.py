"""Credential boundary tests; actual native account acceptance uses local protocol peers."""
from __future__ import annotations

import asyncio
import importlib.abc
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_config import BackpackConfigError, parse_plan
from backpack_account import credential_seed, run_account
import backpack_account
import backpack_probe
from test_backpack_config import document, private_document


def test_wrong_mode_refused_without_native_import_or_credentials(tmp_path, monkeypatch):
    monkeypatch.setattr(backpack_account, "credential_seed", lambda _: pytest.fail("credential read"))
    with pytest.raises(BackpackConfigError, match="explicit account-readonly"):
        asyncio.run(run_account(parse_plan(document(), tmp_path)))
    assert not list(tmp_path.iterdir())


def test_missing_explicit_credential_refused_before_native_import(tmp_path, monkeypatch, capsys):
    plan = parse_plan(private_document(), tmp_path)
    monkeypatch.delenv(plan.account.credential_env, raising=False)
    monkeypatch.setattr(backpack_account, "APP_ROOT", tmp_path)
    class NoNative(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(("nautilus_trader", "dotenv")):
                raise AssertionError("imported runtime before explicit credential")
    monkeypatch.setattr(sys, "meta_path", [NoNative(), *sys.meta_path])
    monkeypatch.setattr(backpack_probe, "load_plan", lambda _: plan)
    assert backpack_probe.main(["--config", "unused.toml"]) == 2
    assert "configured Backpack account credential is unavailable" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())


def test_explicit_credential_does_not_search_dotenv_or_other_variables(tmp_path, monkeypatch):
    plan = parse_plan(private_document(), tmp_path)
    monkeypatch.setenv(plan.account.credential_env, "public-synthetic-sentinel")
    monkeypatch.setattr(backpack_account, "APP_ROOT", tmp_path / "absent")
    assert credential_seed(plan) == "public-synthetic-sentinel"
    assert not list(tmp_path.iterdir())


def test_account_dry_run_never_loads_credentials_or_constructs_native(tmp_path, monkeypatch, capsys):
    plan = parse_plan(private_document(), tmp_path)
    monkeypatch.setattr(backpack_probe, "load_plan", lambda _: plan)
    monkeypatch.setattr(backpack_account, "credential_seed", lambda _: pytest.fail("credential read"))
    assert backpack_probe.main(["--config", "unused.toml", "--dry-run"]) == 0
    output = capsys.readouterr().out
    assert plan.account.credential_env not in output
    assert plan.account.venue_account not in output
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("gap,reason", [
    ("PrivateSubscriptionTransportFailure", "account_private_subscription_transport_failed"),
    ("RecoveryIncomplete", "account_recovery_incomplete"),
    ("PrivateQueueOverflow", "account_private_input_failed"),
])
def test_native_fault_codes_cannot_complete_despite_connected_observed_snapshot(gap, reason):
    from backpack_account import account_failure
    health = {"account": {"evidence_gaps": [gap], "parse_failures": 0,
        "rest_snapshot_observed": True, "transport_connected": True}}
    assert account_failure(health) == reason


def test_expected_native_gaps_and_recovered_disconnect_do_not_claim_verified_or_failed():
    from backpack_account import account_failure
    health = {"account": {"evidence_gaps": ["AccountIdentityUnverified", "PrivateSubscriptionUnconfirmed",
        "NonAtomicSnapshot", "RetentionOrReplicationUnknown", "PrivateReconnectGap", "PrivateConnectionLost"],
        "parse_failures": 0, "rest_snapshot_observed": True, "transport_connected": True}}
    assert account_failure(health) is None

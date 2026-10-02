"""Strict local-only plans and dry-run side-effect regression coverage."""
from copy import deepcopy
import importlib.abc
import json
import os
from pathlib import Path
import socket
import sys
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_config import BackpackConfigError
from backpack_loopback_config import load_loopback_plan, parse_loopback_plan
import backpack_loopback

ROOT = Path(__file__).resolve().parents[1]


def loopback_document():
    return tomllib.loads((ROOT / "config/backpack-loopback-execution.example.toml").read_text())


def test_sample_dry_run_has_no_credentials_native_or_output(tmp_path, monkeypatch, capsys):
    config = tmp_path / "loopback.toml"
    config.write_bytes((ROOT / "config/backpack-loopback-execution.example.toml").read_bytes())
    class NoNative(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(("nautilus_trader", "dotenv")):
                raise AssertionError("dry-run imported a private runtime")
    def forbidden(*args, **kwargs):
        raise AssertionError("dry-run performed I/O")
    original_open = Path.open
    def configuration_only(path, *args, **kwargs):
        assert path == config
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", configuration_only)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(sys, "meta_path", [NoNative(), *sys.meta_path])
    assert backpack_loopback.main(["--config", str(config), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == "loopback-execution"
    assert result["scenario"]["maximum_submit_attempts"] == 2
    assert not result["runtime_started"] and not result["execution_ready"]
    assert not result["production_writes_supported"]
    assert not result["durable_economic_acknowledgement"]
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.parametrize("path,value", [
    (("mode",), "production"), (("schema_version",), True),
    (("session", "environment"), "production"),
    (("session", "base_url_http"), "https://api.backpack.exchange"),
    (("session", "base_url_http"), "http://localhost:1234"),
    (("session", "base_url_ws"), "ws://127.0.0.2:1234"),
    (("session", "base_url_ws"), "wss://127.0.0.1:1234"),
    (("session", "economics", "BTC_USDC_PERP", "source"), "Configured"),
    (("authority", "max_unsettled_orders"), 1),
    (("authority", "max_unsettled_orders"), True),
    (("authority", "allow_new_risk"), False),
    (("authority", "allow_owned_cancel"), False),
    (("authority", "valid_for_ms"), 10001),
    (("authority", "max_account_age_ms"), 1000),
    (("authority", "max_market_age_ms"), 1000),
    (("authority", "max_reserved_notional"), "0.003"),
    (("authority", "max_order_notional"), 0.01),
    (("authority", "max_reserved_margin"), "0.00001"),
    (("synthetic_account", "net_positions"), {}),
    (("synthetic_account", "net_positions", "BTC_USDC_PERP.BACKPACK"), "0.00001"),
    (("synthetic_account", "complete"), False),
    (("synthetic_account", "auto_borrow"), True),
    (("synthetic_account", "auto_lend"), True),
    (("synthetic_account", "auto_repay"), True),
    (("synthetic_account", "liquidating"), True),
    (("synthetic_account", "margin_per_notional"), "1.1"),
    (("synthetic_account", "economics_reference"), ""),
    (("scenario", "symbol"), "ETH_USDC_PERP"),
    (("scenario", "quantity"), "NaN"),
    (("scenario", "quantity"), "0"),
    (("scenario", "limit_price"), "1e2"),
    (("scenario", "stale_probe_delay_ms"), 100),
    (("scenario", "reassert_flat_before_stale_probe"), False),
    (("mutation_budget_ms",), 2001),
    (("receive_window_ms",), 60001),
    (("session", "max_report_bytes"), 8191),
])
def test_unsafe_incomplete_or_inexact_plans_refused(tmp_path, path, value):
    document = deepcopy(loopback_document())
    current = document
    for name in path[:-1]:
        current = current[name]
    current[path[-1]] = value
    with pytest.raises(BackpackConfigError):
        parse_loopback_plan(document, tmp_path)


def test_all_permissions_and_policy_flags_are_mandatory(tmp_path):
    document = loopback_document()
    for section in ("authority", "synthetic_account", "scenario"):
        for key in document[section]:
            changed = deepcopy(document)
            del changed[section][key]
            with pytest.raises(BackpackConfigError):
                parse_loopback_plan(changed, tmp_path)


def test_single_order_keeps_one_capacity_when_probe_disabled(tmp_path):
    document = loopback_document()
    document["scenario"].update(stale_probe=False, cancel_after_fill=False)
    document["authority"].update(max_unsettled_orders=1, allow_owned_cancel=False)
    plan = parse_loopback_plan(document, tmp_path)
    assert plan.scenario.document()["maximum_submit_attempts"] == 1
    assert plan.scenario.document()["maximum_cancel_attempts"] == 0
    assert plan.journal_dir == plan.session.journal_dir
    assert plan.output_dir != plan.session.output_dir


def test_cli_runtime_requires_candidate_before_credential_read(tmp_path, monkeypatch, capsys):
    config = tmp_path / "loopback.toml"
    config.write_bytes((ROOT / "config/backpack-loopback-execution.example.toml").read_bytes())
    monkeypatch.setattr(backpack_loopback, "loopback_seed", lambda _: pytest.fail("read credentials"))
    assert backpack_loopback.main(["--config", str(config)]) == 2
    assert "source-bound candidate" in capsys.readouterr().err
    assert list(tmp_path.iterdir()) == [config]


def test_runtime_seed_never_falls_back_to_dotenv(tmp_path, monkeypatch):
    plan = parse_loopback_plan(loopback_document(), tmp_path)
    monkeypatch.delenv(plan.account.credential_env, raising=False)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("opened dotenv"))
    with pytest.raises(BackpackConfigError, match="synthetic credential"):
        backpack_loopback.loopback_seed(plan)
    monkeypatch.setenv(plan.account.credential_env, "explicit-local-value")
    assert backpack_loopback.loopback_seed(plan) == "explicit-local-value"


def test_no_secret_field_or_dotenv_configuration_allowed(tmp_path, monkeypatch):
    value = loopback_document()
    value["api_secret"] = "must-not-leak"
    with pytest.raises(BackpackConfigError) as error:
        parse_loopback_plan(value, tmp_path)
    assert "must-not-leak" not in str(error.value)
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("opened dotenv"))
    with pytest.raises(BackpackConfigError, match="dotenv"):
        load_loopback_plan(tmp_path / ".env")


def test_compact_publish_preserves_native_unknown_dirty_counts_within_budget(tmp_path):
    from dataclasses import replace
    from backpack_public import PublicEvidence
    plan = parse_loopback_plan(loopback_document(), tmp_path)
    plan = replace(plan, session=replace(plan.session, max_report_bytes=8192))
    evidence = PublicEvidence(plan)
    shutdown = {"schema_version": 1, "dirty": True, "unsent": 0, "unknown": 1,
        "observed_unreconciled": 1, "pending_cancellations": 1, "positions_unknown_or_nonzero": True}
    evidence.compact_summary = {"mode": plan.mode, "scenario_completed": True,
        "execution_settled": False, "flat_verified": False, "exposure_unknown": True,
        "pending_fill_count": 1, "native_shutdown_report": shutdown, "dirty_shutdown": True,
        "durable_economic_acknowledgement": False, "cancel_unsettled_observed": True}
    while not evidence.limit_reached:
        evidence.record("budget-fill", {"bounded": "x" * 500})
    assert evidence.bytes > 3000
    # Full details cannot fit, but native uncertainty must survive alongside retained events.
    summary, path = evidence.publish({"schema_version": 1, "run_id": "compact-test",
        "configuration_sha256": "f" * 64, "status": "failed", "failure": "report_limit",
        "private_details": "x" * 10000})
    assert summary["summary_truncated"] and summary["dirty_shutdown"]
    assert summary["native_shutdown_report"] == shutdown and summary["pending_fill_count"] == 1
    assert summary["exposure_unknown"] and not summary["flat_verified"]
    assert not summary["execution_settled"] and not summary["durable_economic_acknowledgement"]
    assert sum(p.stat().st_size for p in path.parent.iterdir()) <= plan.max_report_bytes


def test_post_stop_requires_authoritative_native_shutdown_report():
    assert backpack_loopback.loopback_final_failure({"loopback": {"shutdown_report": None}}) == "loopback_shutdown_report_unavailable"
    assert backpack_loopback.loopback_final_failure({"loopback": {"shutdown_report": {"dirty": True}}}) is None

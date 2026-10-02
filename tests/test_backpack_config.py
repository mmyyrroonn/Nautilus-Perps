"""Configuration tests, with sentinels on every prohibited dry-run side effect."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal
import importlib.abc
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_config import BackpackConfigError, load_plan, parse_plan
import backpack_probe

ROOT = Path(__file__).resolve().parents[1]
SYMBOL = "BTC_USDC_PERP"


def document():
    return {"schema_version": 1, "mode": "public", "environment": "production",
            "symbols": [SYMBOL], "duration_secs": 30,
            "output_dir": "reports", "state_dir": "state",
            "economics": {SYMBOL: {"margin_init": "0.1", "margin_maint": "0.05",
                "maker_fee": "-0.0001", "taker_fee": "0.0005", "source": "Configured",
                "source_reference": "explicit test input"}}}


def private_document():
    value = document()
    value["mode"] = "account-readonly"
    value["account"] = {"account_id": "BACKPACK-SYNTHETIC", "venue_account": "synthetic-owner",
                        "subaccount": "0", "credential_env": "BACKPACK_TEST_SEED"}
    return value


def test_sample_dry_run_without_native_import_credentials_or_socket(tmp_path, monkeypatch, capsys):
    config = tmp_path / "session.toml"
    config.write_bytes((ROOT / "config/backpack-public.example.toml").read_bytes())
    class NoNative(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(("nautilus_trader", "dotenv")):
                raise AssertionError(f"unexpected import {fullname}")
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected dry-run side effect")
    original_open = Path.open
    def named_config_only(path, *args, **kwargs):
        assert path == config
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", named_config_only)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(os, "environ", {})
    monkeypatch.setattr(sys, "meta_path", [NoNative(), *sys.meta_path])
    assert backpack_probe.main(["--config", str(config), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["instrument_ids"] == ["BTC_USDC_PERP.BACKPACK"]
    assert result["runtime_started"] is False
    assert result["execution_ready"] is False
    assert result["remote_writes_allowed"] is False
    assert list(tmp_path.iterdir()) == [config]


def test_cli_refuses_missing_config_without_starting_runtime(tmp_path):
    result = subprocess.run([sys.executable, str(ROOT / "src/backpack_probe.py"),
        "--config", str(tmp_path / "missing.toml")], capture_output=True, text=True, check=False)
    assert result.returncode == 2
    assert "cannot read or publish session files" in result.stderr
    assert not list(tmp_path.iterdir())


def test_namespace_preserves_exact_identity_across_credential_rotation_and_runs(tmp_path):
    value = private_document()
    plan = parse_plan(value, tmp_path)
    other = deepcopy(value)
    other["account"]["credential_env"] = "ROTATED_SEED"
    other["duration_secs"] = 60
    other["account"]["account_id"] = "BACKPACK-ANOTHER_LOCAL_LABEL"
    assert parse_plan(other, tmp_path).journal_dir == plan.journal_dir
    for key, replacement in [("venue_account", "different-owner"), ("subaccount", "1")]:
        other = deepcopy(value)
        other["account"][key] = replacement
        assert parse_plan(other, tmp_path).journal_dir != plan.journal_dir
    other = deepcopy(value)
    del other["account"]["subaccount"]
    assert parse_plan(other, tmp_path).journal_dir != plan.journal_dir
    output = json.dumps(plan.document()) + repr(plan) + repr(plan.account)
    assert "synthetic-owner" not in output
    assert "BACKPACK_TEST_SEED" not in output
    assert "BACKPACK-SYNTHETIC" not in output
    assert plan.economics[0][1].maker_fee == Decimal("-0.0001")


@pytest.mark.parametrize("key,value", [
    ("schema_version", True), ("schema_version", 2), ("mode", "live"),
    ("environment", "testnet"), ("environment", "offline"),
    ("symbols", []), ("symbols", [SYMBOL, SYMBOL]), ("symbols", ["BTC_USDT_PERP"]),
    ("symbols", ["BTC_USDC_PERP "]), ("symbols", "BTC_USDC_PERP"),
    ("duration_secs", 0), ("duration_secs", 601), ("duration_secs", True),
    ("request_timeout_secs", 61), ("request_timeout_secs", 31),
    ("stale_after_ms", 0), ("max_report_events", 100001), ("max_report_bytes", 67108865),
    ("base_url_http", "https://example.com"), ("replay_file", "events.jsonl"),
    ("api_secret", "must-not-appear"), ("economics", {}),
])
def test_invalid_configuration_is_refused(key, value, tmp_path):
    data = document()
    data[key] = value
    with pytest.raises(BackpackConfigError) as error:
        parse_plan(data, tmp_path)
    assert "must-not-appear" not in str(error.value)


@pytest.mark.parametrize("field,value", [
    ("margin_init", 0.1), ("margin_init", "NaN"), ("margin_init", "1e-2"),
    ("margin_init", "-0.1"), ("margin_maint", "0.2"), ("maker_fee", "2"),
    ("taker_fee", "-1.0000000000000000000000000001"),
    ("maker_fee", "0.00000000000000000000000000001"),
    ("maker_fee", "9" * 5000), ("source", "Unknown"), ("source_reference", ""),
])
def test_economic_precision_range_and_source_fail_closed(field, value, tmp_path):
    data = document()
    data["economics"][SYMBOL][field] = value
    with pytest.raises(BackpackConfigError):
        parse_plan(data, tmp_path)


@pytest.mark.parametrize("http,ws", [
    ("http://127.0.0.1:1234", "ws://127.0.0.1:1235"),
    ("https://[::1]:443", "wss://[::1]:443"),
])
def test_explicit_loopback_and_offline_replay_plans(tmp_path, http, ws):
    data = document()
    data.update(environment="loopback", base_url_http=http, base_url_ws=ws)
    plan = parse_plan(data, tmp_path)
    assert plan.http_origin == http
    assert plan.namespace != parse_plan(document(), tmp_path).namespace
    data = document()
    data.update(mode="replay", environment="offline", replay_file="absent.jsonl")
    plan = parse_plan(data, tmp_path)
    assert plan.http_origin is None and plan.ws_origin is None
    assert plan.replay_file == tmp_path / "absent.jsonl"
    assert not plan.replay_file.exists()


@pytest.mark.parametrize("http", ["http://localhost:1", "https://example.com", "http://127.0.0.1:0",
    "http://user:pass@127.0.0.1", "http://127.0.0.1/path", "http://127.0.0.1?key=secret",
    "ftp://127.0.0.1", "http://127.0.0.1#fragment"])
def test_loopback_rejects_remote_and_ambiguous_origins(tmp_path, http):
    data = document()
    data.update(environment="loopback", base_url_http=http, base_url_ws="ws://127.0.0.1:1")
    with pytest.raises(BackpackConfigError):
        parse_plan(data, tmp_path)


def test_private_scope_only_allowed_in_account_readonly(tmp_path):
    data = private_document()
    data["mode"] = "public"
    with pytest.raises(BackpackConfigError):
        parse_plan(data, tmp_path)
    data = document()
    data["mode"] = "account-readonly"
    with pytest.raises(BackpackConfigError):
        parse_plan(data, tmp_path)


def test_bad_toml_and_size_do_not_echo_contents(tmp_path):
    config = tmp_path / "session.toml"
    for contents in [b'bad = "private-value', b"x" * 65537, b"\xff"]:
        config.write_bytes(contents)
        with pytest.raises(BackpackConfigError) as error:
            load_plan(config)
        assert "private-value" not in str(error.value)


@pytest.mark.parametrize("name", [".env", ".env.local"])
def test_dotenv_refused_without_open(name, tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("dotenv must not be opened")
    monkeypatch.setattr(Path, "open", forbidden)
    with pytest.raises(BackpackConfigError, match="dotenv"):
        load_plan(tmp_path / name)


def test_exact_small_economics_never_use_scientific_notation(tmp_path):
    data = document()
    data["economics"][SYMBOL]["maker_fee"] = "0.0000000000000000000000000001"
    assert parse_plan(data, tmp_path).document()["economics"][SYMBOL]["maker_fee"] == data["economics"][SYMBOL]["maker_fee"]

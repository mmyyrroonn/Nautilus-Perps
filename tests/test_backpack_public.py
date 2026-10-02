"""Offline evidence bounds and public-only CLI refusal; native proof lives in loopback tests."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import importlib.abc
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_config import BackpackConfigError, parse_plan
from backpack_public import PublicEvidence, configuration_hash, run_public
import backpack_probe
from test_backpack_config import document, private_document


def test_evidence_bounds_events_bytes_and_minimal_summary(tmp_path):
    data = document()
    data.update(max_report_events=2, max_report_bytes=1024)
    plan = parse_plan(data, tmp_path)
    evidence = PublicEvidence(plan)
    for _ in range(10):
        evidence.record("quote", {"bid_price": "100.1"})
    summary, path = evidence.publish({"run_id": "synthetic-run", "status": "completed",
        "failure": None, "configuration_sha256": configuration_hash(plan), "large": "x" * 10000})
    assert evidence.dropped == 8
    assert summary["summary_truncated"] is True
    assert summary["execution_ready"] is False
    assert sum(p.stat().st_size for p in path.parent.iterdir()) <= 1024
    assert len((path.parent / "events.jsonl").read_text().splitlines()) == 2
    with pytest.raises(FileExistsError):
        evidence.publish(summary)


def test_configuration_hash_tracks_exact_economics_and_bounds(tmp_path):
    data = document()
    first = configuration_hash(parse_plan(data, tmp_path))
    for key, value in [("stale_after_ms", 500), ("max_report_events", 3)]:
        other = deepcopy(data)
        other[key] = value
        assert configuration_hash(parse_plan(other, tmp_path)) != first
    data["economics"]["BTC_USDC_PERP"]["maker_fee"] = "0.0001"
    assert configuration_hash(parse_plan(data, tmp_path)) != first


@pytest.mark.parametrize("mode", ["account-readonly", "paper", "replay"])
def test_unsupported_mode_refused_before_native_import_and_output(tmp_path, monkeypatch, mode):
    data = private_document() if mode == "account-readonly" else document()
    data["mode"] = mode
    if mode == "replay":
        data.update(environment="offline", replay_file="not-opened.jsonl")
    plan = parse_plan(data, tmp_path)
    class NoNative(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(("nautilus_trader", "dotenv")):
                raise AssertionError("native or dotenv imported for unsupported mode")
    monkeypatch.setattr(sys, "meta_path", [NoNative(), *sys.meta_path])
    with pytest.raises(BackpackConfigError, match="only public"):
        asyncio.run(run_public(plan))
    assert not list(tmp_path.iterdir())


def test_native_startup_failure_is_sanitized_and_published(tmp_path, monkeypatch):
    import backpack_public
    def fail(*args):
        raise RuntimeError("SECRET-SENTINEL raw body")
    monkeypatch.setattr(backpack_public, "native_identity", fail)
    plan = parse_plan(document(), tmp_path)
    summary, path = asyncio.run(run_public(plan))
    assert summary["failure"] == "startup_failed"
    assert summary["runtime_started"] is False
    assert summary["shutdown_complete"] is False
    assert "SECRET-SENTINEL" not in path.read_text()
    assert not plan.journal_dir.exists()


def test_cli_unsupported_mode_refuses_before_import(tmp_path, monkeypatch, capsys):
    plan = parse_plan(private_document(), tmp_path)
    monkeypatch.setattr(backpack_probe, "load_plan", lambda _: plan)
    assert backpack_probe.main(["--config", "unused.toml"]) == 2
    assert "only public runtime" in capsys.readouterr().err
    assert not list(tmp_path.iterdir())

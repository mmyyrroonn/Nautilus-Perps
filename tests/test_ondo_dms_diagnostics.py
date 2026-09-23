"""Offline checks for private DMS diagnostic redaction and run isolation."""

from __future__ import annotations

import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ondo_dms_diagnostics import read_dms_release_diagnostics  # noqa: E402


def target(payload, *, run_id="current", phase="reconciled"):
    return SimpleNamespace(production_trade_snapshot=lambda: {
        "run_id": run_id, "phase": phase, "dms_release": payload,
    })


def test_failed_release_observations_survive_without_claiming_final_proof():
    result = read_dms_release_diagnostics(target({
        "attempted": True, "frame_sent": True, "acknowledged": False,
        "outcome": "ack_timeout", "updates_before_release": 0, "updates_after_release": 1,
        "last_update_data_kind": "object", "last_update_op": "unsubscribe",
        "last_update_timeout": "zero", "last_update_status": "disabled",
        "last_update_enabled": "false",
    }), run_id="current")

    assert result["available"] is True
    assert result["acknowledged"] is False
    assert result["frame_sent"] is True
    assert result["outcome"] == "ack_timeout"
    assert result["updates_after_release"] == 1
    assert "phase" not in result
    assert "complete" not in result
    assert "production_execution_verified" not in result


def test_arbitrary_private_values_and_unknown_keys_never_reach_the_report():
    secret = "SYNTHETIC_PRIVATE_VALUE_DO_NOT_PUBLISH"
    result = read_dms_release_diagnostics(target({
        "attempted": secret, "frame_sent": {"key": secret}, "acknowledged": secret,
        "outcome": secret, "updates_before_release": True, "updates_after_release": secret,
        "last_update_data_kind": secret, "last_update_op": secret,
        "last_update_timeout": secret, "last_update_status": {"key": secret},
        "last_update_enabled": [secret], "raw_frame": secret, "account_id": secret,
    }), run_id="current")

    assert secret not in json.dumps(result)
    assert result["outcome"] is None
    assert result["updates_before_release"] is None
    assert result["acknowledged"] is None
    assert "raw_frame" not in result
    assert "account_id" not in result


@pytest.mark.parametrize("count", [-1, True, 1.0, "1", 2**64])
def test_invalid_or_unbounded_counts_stay_unknown(count):
    result = read_dms_release_diagnostics(target({
        "updates_before_release": count, "updates_after_release": count,
    }), run_id="current")
    assert result["updates_before_release"] is None
    assert result["updates_after_release"] is None


@pytest.mark.parametrize("native", [None, object(), target({}, run_id="other"), target(None)])
def test_missing_or_cross_run_observations_are_unavailable(native):
    result = read_dms_release_diagnostics(native, run_id="current")
    assert result["available"] is False
    assert result["acknowledged"] is None


def test_accessor_exception_does_not_escape_or_publish_private_text():
    def fail():
        raise RuntimeError("SYNTHETIC_PRIVATE_EXCEPTION")

    result = read_dms_release_diagnostics(
        SimpleNamespace(production_trade_snapshot=fail), run_id="current",
    )
    assert result["available"] is False
    assert "SYNTHETIC_PRIVATE_EXCEPTION" not in json.dumps(result)

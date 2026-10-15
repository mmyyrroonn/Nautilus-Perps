"""Offline stop proofs copied from the native public binding contracts."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_shutdown import collect_native_shutdown


def backpack_report(**updates):
    raw = {"schema_version": 1, "dirty": False, "unsent": 0, "unknown": 0,
           "observed_unreconciled": 0, "pending_cancellations": 0, "positions_unknown_or_nonzero": False}
    raw.update(updates)
    return raw


def backpack(raw=None):
    return NS(shutdown_report_json=lambda: json.dumps(backpack_report() if raw is None else raw))


def ondo_proofs():
    return ({"schema_version": 1, "venue": "ONDO", "position_quantities": {},
             "run_id": "synthetic-run", "mode": "continuous", "phase": "final", "shutdown_status": "clean",
             "complete": True, "coverage_complete": True, "reconciled_flat": True, "account_positions_owned": True,
             "own_open_orders": 0, "foreign_open_orders": 0, "unknown_submissions": 0, "late_fills": 0,
             "latest_activity_generation": 5, "reconciled_activity_generation": 5},
            {"attempted": True, "frame_sent": True, "acknowledged": True, "outcome": "acknowledged",
             "trace": {"requests_drained": True, "shutdown_started_unix_nanos": 50,
                       "shutdown_finished_unix_nanos": 100}},
            {"run_id": "synthetic-run", "shutdown_status": "complete"})


def ondo(proofs=None):
    account, diagnostics, owned = ondo_proofs() if proofs is None else proofs
    return NS(production_trade_snapshot=lambda: deepcopy(account),
              production_shutdown_diagnostics=lambda: deepcopy(diagnostics), read_only_snapshot=lambda: deepcopy(owned))


def test_all_supplied_native_owners_must_report_actual_clean_stop():
    result = collect_native_shutdown({"ONDO": ondo(), "BACKPACK": backpack()})
    assert result["clean"] and set(result["venues"]) == {"ONDO", "BACKPACK"}
    assert all(row["clean"] for row in result["venues"].values())
    assert "synthetic-run" not in json.dumps(result)


@pytest.mark.parametrize("updates", [{"dirty": True}, {"positions_unknown_or_nonzero": True}, {"unsent": 1},
    {"unknown": 1}, {"observed_unreconciled": 1}, {"pending_cancellations": 1}, {"unknown": True},
    {"schema_version": True}, {"dirty": "false"}, {"positions_unknown_or_nonzero": None}, {"unknown": -1}])
def test_backpack_nonzero_or_invalid_native_economic_and_order_evidence_fails(updates):
    result = collect_native_shutdown({"BACKPACK": backpack(backpack_report(**updates))})
    assert not result["clean"] and not result["venues"]["BACKPACK"]["clean"]


@pytest.mark.parametrize("field", tuple(backpack_report()))
def test_backpack_every_field_is_required(field):
    raw = backpack_report()
    raw.pop(field)
    assert not collect_native_shutdown({"BACKPACK": backpack(raw)})["clean"]


@pytest.mark.parametrize("section,updates", [
    (0, {"phase": "reconciled"}), (0, {"shutdown_status": "uncertain"}), (0, {"mode": "envelope"}),
    (0, {"complete": False}), (0, {"coverage_complete": False}), (0, {"reconciled_flat": False}),
    (0, {"account_positions_owned": False}), (0, {"unknown_submissions": 1}), (0, {"own_open_orders": 1}),
    (0, {"foreign_open_orders": 1}), (0, {"late_fills": 1}), (0, {"reconciled_activity_generation": 4}),
    (0, {"latest_activity_generation": True}), (0, {"complete": "true"}), (1, {"acknowledged": False}),
    (0, {"position_quantities": {"BTC-USD-PERP.ONDO": "0.001"}}),
    (0, {"position_quantities": {"BTC-USD-PERP.ONDO": "NaN"}}),
    (0, {"schema_version": True}),
    (1, {"outcome": "ack_timeout"}), (2, {"shutdown_status": "incomplete"}), (2, {"run_id": "other-run"}),
])
def test_ondo_stop_requires_final_flat_owned_economics_and_dms_ack(section, updates):
    proofs = list(ondo_proofs())
    proofs[section].update(updates)
    assert not collect_native_shutdown({"ONDO": ondo(proofs)})["clean"]


@pytest.mark.parametrize("change", [{"requests_drained": False}, {"requests_drained": None},
    {"shutdown_started_unix_nanos": 0}, {"shutdown_started_unix_nanos": 101},
    {"shutdown_finished_unix_nanos": None}])
def test_ondo_owned_tasks_must_have_finished_inside_an_actual_shutdown_trace(change):
    proofs = list(ondo_proofs())
    proofs[1]["trace"].update(change)
    assert not collect_native_shutdown({"ONDO": ondo(proofs)})["clean"]


def test_ondo_dms_diagnostics_alone_are_not_account_or_owned_stop_proof():
    control = ondo()
    control.production_trade_snapshot = lambda: None
    assert not collect_native_shutdown({"ONDO": control})["clean"]
    control = ondo()
    control.read_only_snapshot = lambda: None
    assert not collect_native_shutdown({"ONDO": control})["clean"]


def test_missing_control_report_and_private_errors_do_not_become_success_or_log_text():
    secret = "synthetic-sensitive-payload-do-not-print"
    def failure():
        raise RuntimeError(secret)
    for control in (None, NS(), NS(shutdown_report_json=failure), NS(shutdown_report_json=lambda: secret),
                    NS(shutdown_report_json=lambda: None)):
        result = collect_native_shutdown({"BACKPACK": control})
        assert not result["clean"] and secret not in json.dumps(result)


def test_unselected_venues_have_no_native_control_requirement():
    assert collect_native_shutdown({}) == {"clean": True, "venues": {}}
    assert collect_native_shutdown({"ASTER": object()}) == {"clean": True, "venues": {}}
    assert not collect_native_shutdown(None)["clean"]


def test_new_installed_native_ondo_factory_unattached_never_reports_clean_stop():
    ondo_module = pytest.importorskip("nautilus_trader.adapters.ondo")
    factory = ondo_module.OndoExecutionClientFactory()
    assert not collect_native_shutdown({"ONDO": factory})["clean"]

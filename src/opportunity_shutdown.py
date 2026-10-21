"""Collect bounded native stop proof without exposing private payloads or errors.

Backpack exposes a sticky shutdown report. Ondo's DMS diagnostics alone are not
account proof: its final production snapshot and owned-stop status must agree.
Only native owners supplied by the caller are inspected; no I/O or financial
command is initiated here.
"""
from __future__ import annotations

from opportunity_timing import timed

from collections.abc import Mapping
from decimal import Decimal
import json


class _InvalidProof(ValueError):
    pass


_BACKPACK_SHUTDOWN_REASONS = frozenset({
    "account_proof_missing", "refresh_timeout", "refresh_failed", "private_unavailable",
    "private_revision_changed", "terminal_evidence_unresolved", "open_orders", "unconsumed_fills",
    "account_invalid", "native_position_mismatch", "account_stale", "positions_nonzero",
    "unresolved_orders", "prior_dirty_stop", "disconnect_incomplete",
})


def _object(value):
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise _InvalidProof
    return value


def _count(value):
    if type(value) is not int or not 0 <= value <= (1 << 64) - 1:
        raise _InvalidProof
    return value


def _boolean(value):
    if type(value) is not bool:
        raise _InvalidProof
    return value


def _backpack(control):
    raw = _object(control.shutdown_report_json())
    if type(raw.get("schema_version")) is not int or raw["schema_version"] != 1:
        raise _InvalidProof
    counts = {name: _count(raw.get(name)) for name in (
        "unsent", "unknown", "observed_unreconciled", "pending_cancellations")}
    dirty = _boolean(raw.get("dirty"))
    position_unknown = _boolean(raw.get("positions_unknown_or_nonzero"))
    clean = not dirty and not position_unknown and not any(counts.values())
    details = {}
    reason = None
    if any(name in raw for name in ("positions_unknown", "positions_nonzero", "complete_account_proof")):
        details = {name: _boolean(raw.get(name)) for name in (
            "positions_unknown", "positions_nonzero", "complete_account_proof")}
        unknown, nonzero = details["positions_unknown"], details["positions_nonzero"]
        if (unknown and nonzero) or position_unknown != (unknown or nonzero):
            raise _InvalidProof
        reason = raw["reason"]
        if reason is not None and (not isinstance(reason, str) or reason not in _BACKPACK_SHUTDOWN_REASONS):
            raise _InvalidProof
        for name in ("account_observed_at_ms", "account_completed_at_ms", "evaluated_at_ms",
                     "private_generation", "private_epoch", "private_revision"):
            value = raw[name]
            details[name] = None if value is None else _count(value)
        if details["complete_account_proof"]:
            started, completed, evaluated, generation, epoch, revision = (
                details[name] for name in ("account_observed_at_ms", "account_completed_at_ms",
                    "evaluated_at_ms", "private_generation", "private_epoch", "private_revision"))
            if (unknown or any(value is None for value in (started, completed, evaluated, generation, epoch, revision))
                    or not 0 < started <= completed <= evaluated or generation == 0):
                raise _InvalidProof
        clean = clean and details["complete_account_proof"] and reason is None
    return {"clean": clean, "reason": "clean" if clean else reason or "native_shutdown_dirty",
            "dirty": dirty, "positions_unknown_or_nonzero": position_unknown, **counts, **details}


def _ondo(control):
    proof = _object(control.production_trade_snapshot())
    diagnostics = _object(control.production_shutdown_diagnostics())
    owned = _object(control.read_only_snapshot())
    if (type(proof.get("schema_version")) is not int or proof["schema_version"] != 1
            or proof.get("venue") != "ONDO"):
        raise _InvalidProof
    # Run IDs only establish agreement; they are never retained in the report.
    run_id = proof.get("run_id")
    if not isinstance(run_id, str) or not run_id or owned.get("run_id") != run_id:
        raise _InvalidProof
    quantities = _object(proof.get("position_quantities"))
    positions_flat = True
    for instrument, value in quantities.items():
        if (not isinstance(instrument, str) or not instrument.endswith(".ONDO")
                or isinstance(value, bool) or not isinstance(value, (str, int, Decimal))):
            raise _InvalidProof
        amount = Decimal(value)
        if not amount.is_finite():
            raise _InvalidProof
        positions_flat = positions_flat and amount == 0
    counts = {name: _count(proof.get(name)) for name in (
        "own_open_orders", "foreign_open_orders", "unknown_submissions", "late_fills")}
    coverage = {name: _boolean(proof.get(name)) for name in (
        "complete", "coverage_complete", "reconciled_flat", "account_positions_owned")}
    latest = _count(proof.get("latest_activity_generation"))
    reconciled = _count(proof.get("reconciled_activity_generation"))
    attempted = _boolean(diagnostics.get("attempted"))
    sent = _boolean(diagnostics.get("frame_sent"))
    acknowledged = _boolean(diagnostics.get("acknowledged"))
    trace = _object(diagnostics.get("trace"))
    drained = _boolean(trace.get("requests_drained"))
    started, finished = (_count(trace.get(name)) for name in (
        "shutdown_started_unix_nanos", "shutdown_finished_unix_nanos"))
    clean = (proof.get("mode") == "continuous" and proof.get("phase") == "final"
             and proof.get("shutdown_status") == "clean" and owned.get("shutdown_status") == "complete"
             and all(coverage.values()) and not any(counts.values()) and latest == reconciled
             and positions_flat
             and attempted and sent and acknowledged and diagnostics.get("outcome") == "acknowledged"
             and drained and 0 < started <= finished)
    return {"clean": clean, "reason": "clean" if clean else "native_shutdown_dirty", **counts,
            "complete_account_proof": all(coverage.values()), "activity_reconciled": latest == reconciled,
            "native_positions_flat": positions_flat,
            "owned_stop_complete": owned.get("shutdown_status") == "complete",
            "dms_release_confirmed": attempted and sent and acknowledged
                                     and diagnostics.get("outcome") == "acknowledged",
            "requests_drained": drained}


@timed("shutdown.native_proof")
def collect_native_shutdown(native_controls):
    """Require complete clean proof from every supplied Ondo/Backpack owner.

    Missing, malformed, unavailable or dirty native proof makes ``clean`` false.
    Other venues have no equivalent control API and are covered by the caller's
    application ledger checks. Errors are fixed labels, never exception strings.
    """
    if not isinstance(native_controls, Mapping):
        return {"clean": False, "venues": {}, "reason": "invalid_control_mapping"}
    venues = {}
    for venue, collect in (("ONDO", _ondo), ("BACKPACK", _backpack)):
        if venue not in native_controls:
            continue
        try:
            venues[venue] = collect(native_controls[venue])
        except Exception:
            venues[venue] = {"clean": False, "reason": "native_shutdown_proof_unavailable_or_invalid"}
    return {"clean": all(row["clean"] for row in venues.values()), "venues": venues}

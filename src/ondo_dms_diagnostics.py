"""Allowlisted DMS release observations; these never authorize a clean shutdown."""

from __future__ import annotations

from collections.abc import Mapping


_LABELS = {
    "outcome": frozenset({
        "not_attempted", "checking", "validation_refused", "no_connection",
        "inactive_connection", "send_failed", "awaiting_ack", "ack_timeout",
        "acknowledged", "shutdown_timeout", "blocked_unsettled",
    }),
    "last_update_data_kind": frozenset({
        "missing", "null", "object", "array", "string", "number", "boolean",
    }),
    "last_update_op": frozenset({"missing", "subscribe", "unsubscribe", "unrecognized"}),
    "last_update_timeout": frozenset({"missing", "zero", "positive", "negative", "invalid"}),
    "last_update_status": frozenset({
        "missing", "armed", "disarmed", "enabled", "disabled", "active", "inactive",
        "released", "cancelled", "canceled", "success", "ok", "unrecognized",
    }),
    "last_update_enabled": frozenset({"missing", "true", "false", "invalid"}),
}
_BOOLS = ("attempted", "frame_sent", "acknowledged")
_COUNTS = ("updates_before_release", "updates_after_release")


def read_dms_release_diagnostics(target: object | None, *, run_id: str) -> dict[str, object]:
    """Read fixed labels from this run only, independent of reconciliation phase.

    A failed release leaves a reconciled snapshot rather than a final snapshot. Its
    observations are still useful, but must never be promoted to final account proof.
    No exception text, arbitrary key, raw payload, or unrecognized value is published.
    """
    result: dict[str, object] = {
        "available": False,
        "source": "unavailable",
        "reason": "DMS release observations are unavailable for this run",
        **dict.fromkeys((*_BOOLS, *_COUNTS, *_LABELS)),
    }
    try:
        accessor = getattr(target, "production_trade_snapshot", None)
        snapshot = accessor() if callable(accessor) else accessor
        if not isinstance(snapshot, Mapping) or snapshot.get("run_id") != run_id:
            return result
        raw = snapshot.get("dms_release")
        if not isinstance(raw, Mapping):
            return result
        for name in _BOOLS:
            value = raw.get(name)
            result[name] = value if type(value) is bool else None
        for name in _COUNTS:
            value = raw.get(name)
            result[name] = value if type(value) is int and 0 <= value < 2**64 else None
        for name, labels in _LABELS.items():
            value = raw.get(name)
            result[name] = value if type(value) is str and value in labels else None
    except Exception:
        # A broken diagnostics accessor cannot interrupt cleanup or publish its exception
        return {
            **result,
            **dict.fromkeys((*_BOOLS, *_COUNTS, *_LABELS)),
        }
    result.update({
        "available": True,
        "source": "native-dms-release-observation",
        "reason": "fixed labels and counters only; observations do not certify shutdown",
    })
    return result

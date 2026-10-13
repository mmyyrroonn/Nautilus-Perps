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

_SHUTDOWN_TIMESTAMPS = (
    "shutdown_started_unix_nanos", "shutdown_finished_unix_nanos",
    "checkpoint_finished_unix_nanos", "request_drain_finished_unix_nanos",
    "stream_stop_started_unix_nanos", "stream_stop_finished_unix_nanos",
    "release_started_unix_nanos", "release_sent_unix_nanos",
    "release_ack_observed_unix_nanos", "last_frame_unix_nanos",
)
_SHUTDOWN_COUNTS = (
    "shutdown_budget_nanos", "text_frames_before_release", "text_frames_after_release",
    "dms_subscribed_after_release", "dms_unsubscribed_after_release",
    "other_unsubscribed_after_release", "unclassified_after_release",
)
_FRAME_KINDS = frozenset({
    "subscribed", "unsubscribed", "update", "logged_in", "pong", "venue_error",
    "unclassified",
})


def read_shutdown_diagnostics(target: object | None) -> dict[str, object]:
    """Project the current factory's native shutdown telemetry through fixed fields.

    The caller creates one factory per run. Native shutdown telemetry has no run token,
    so this reader must only be used with that factory, never a shared factory cache.
    These observations cannot establish account proof or a clean shutdown.
    """
    unavailable: dict[str, object] = {
        "available": False,
        "source": "unavailable",
        "trace": None,
        "release": None,
    }
    try:
        accessor = getattr(target, "production_shutdown_diagnostics", None)
        raw = accessor() if callable(accessor) else None
        if not isinstance(raw, Mapping):
            return unavailable
        trace = raw.get("trace")
        if not isinstance(trace, Mapping):
            return unavailable
        safe_trace: dict[str, object] = {}
        for name in (*_SHUTDOWN_TIMESTAMPS, *_SHUTDOWN_COUNTS):
            value = trace.get(name)
            safe_trace[name] = value if type(value) is int and 0 <= value < 2**64 else None
        drained = trace.get("requests_drained")
        safe_trace["requests_drained"] = drained if type(drained) is bool else None
        kind = trace.get("last_frame_kind")
        safe_trace["last_frame_kind"] = (
            kind if type(kind) is str and kind in _FRAME_KINDS else None
        )
        safe_release: dict[str, object] = {}
        for name in _BOOLS:
            value = raw.get(name)
            safe_release[name] = value if type(value) is bool else None
        for name in _COUNTS:
            value = raw.get(name)
            safe_release[name] = value if type(value) is int and 0 <= value < 2**64 else None
        for name, labels in _LABELS.items():
            value = raw.get(name)
            safe_release[name] = value if type(value) is str and value in labels else None
        return {
            "available": True,
            "source": "native-shutdown-observation",
            "trace": safe_trace,
            "release": safe_release,
        }
    except Exception:
        # Diagnostics must not interrupt cleanup or expose an accessor exception.
        return unavailable


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

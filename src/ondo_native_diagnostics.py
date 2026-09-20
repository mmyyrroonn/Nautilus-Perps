#!/usr/bin/env python3
"""Fail-closed reader for the native read-only diagnostics snapshot.

A production-read-only session is only as good as what it can *observe*: a login that
succeeded, the private channels the venue acknowledged, the account state the native layer
published, a reconnect/recovery count, and an identity check. None of that is invented
here. This module reads one bounded, per-run snapshot the native adapter exposes and
reports every fact it does not carry as ``None`` (unknown).

Three rules make the reader safe:

1. **No value is guessed.** The snapshot is counters and fixed enum labels only. A missing
   key, a wrong type, or a value outside the known set becomes ``None``, never a default
   that could be read as a positive.
2. **A snapshot must belong to this run.** It is merged only when its run token equals the
   run id this process minted; a snapshot that cannot be proven to be this run's is
   rejected as cross-run state rather than merged.
3. **The reader invents no field.** The accessor name and exact nine-key shape below are the
   frozen contract in ``native-readonly/interface.md``. A read against an older wheel resolves
   to ``available = False`` when that wheel exposes no accessor: the honest answer is
   "unsupported", never a fabricated host fact or a value inferred from a dry run or a
   factory marker.

This module never renders a frame, a credential, a key id, an account id, an order id or a
monetary field. Every string it can carry is checked against an explicit allowlist (run
states, shutdown statuses and the two documented report channels); an unrecognized value is
dropped, never echoed, so a native snapshot cannot smuggle a value into the report through a
label. The only run token it echoes is this run's own.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

# ---------------------------------------------------------------- required contract
#
# These names are the native adapter's frozen N2 contract. This block and the normalize
# function below remain the single place a future versioned rename would land.

NATIVE_DIAGNOSTICS_ACCESSOR = "read_only_snapshot"
NATIVE_RUN_TOKEN_KEY = "run_id"
NATIVE_LOGIN_KEY = "logged_in"
NATIVE_SUBSCRIPTIONS_KEY = "subscriptions_acked"
NATIVE_RUN_STATE_KEY = "run_state"
NATIVE_RECONNECTS_KEY = "reconnects"
NATIVE_RECOVERIES_KEY = "recoveries"
NATIVE_ACCOUNT_STATE_KEY = "account_state_events"
NATIVE_IDENTITY_KEY = "identity_match"
NATIVE_SHUTDOWN_KEY = "shutdown_status"

REQUIRED_NATIVE_DIAGNOSTICS_KEYS = (
    NATIVE_LOGIN_KEY,
    NATIVE_SUBSCRIPTIONS_KEY,
    NATIVE_RUN_STATE_KEY,
    NATIVE_RECONNECTS_KEY,
    NATIVE_RECOVERIES_KEY,
    NATIVE_ACCOUNT_STATE_KEY,
    NATIVE_IDENTITY_KEY,
    NATIVE_SHUTDOWN_KEY,
)

IDENTITY_MATCHED = "matched"
IDENTITY_MISMATCH = "mismatch"
IDENTITY_UNKNOWN = "unknown"
IDENTITY_VALUES = (IDENTITY_MATCHED, IDENTITY_MISMATCH, IDENTITY_UNKNOWN)

# ------------------------------------------------------------------ fixed vocabularies
#
# The privacy claim is that this report carries fixed labels and counters only. That is only
# true if an unknown label is *dropped* rather than echoed, so every string field is checked
# against an explicit allowlist and anything else becomes ``None``. The vocabularies are the
# adapter's own spellings, read from the native source and recorded here.

# ``websocket/private/stream.rs::PrivateRunState::as_str``.
RUN_STATE_VALUES = (
    "disconnected",
    "authenticating",
    "recovering",
    "read_only_synced",
    "trading_ready",
    "uncertain",
    "stopping",
    "stopped",
)

# The native ``OwnedShutdownStatus`` spellings frozen in ``interface.md``. ``complete`` is
# Clean and ``incomplete`` is Dirty; every other value remains non-clean. An unrecognized
# value is dropped rather than echoed.
SHUTDOWN_STATUS_VALUES = (
    "not_attempted",
    "running",
    "stopping",
    "complete",
    "incomplete",
)

# ``websocket/private/messages.rs::PrivateChannel::as_str``. The dead-man switch channel is
# deliberately absent: a read-only session never subscribes to it, so an ack for it would be
# an unexpected value to drop rather than report.
REPORT_CHANNEL_LABELS = (
    "ordersPerps",
    "fillsPerps",
)

SOURCE_NATIVE = "native-snapshot"
SOURCE_ACCESSOR_ABSENT = "accessor-absent"
SOURCE_LOOKUP_FAILED = "lookup-failed"
SOURCE_NOT_A_MAPPING = "not-a-mapping"
SOURCE_NO_RUN_TOKEN = "no-run-token"
SOURCE_CROSS_RUN = "cross-run"
SOURCE_NO_TARGET = "no-target"

UNSUPPORTED_REASON = (
    "the installed adapter exposes no read-only diagnostics snapshot: native login, "
    "acknowledged-subscription, run-state, reconnect/recovery, account-state and identity "
    "telemetry are unsupported, so every field is unknown. This is not evidence that any "
    "of them happened or did not happen"
)


def _fields() -> dict[str, object]:
    return {key: None for key in REQUIRED_NATIVE_DIAGNOSTICS_KEYS}


@dataclass(frozen=True)
class NativeDiagnostics:
    """One run's native telemetry, with every unobserved fact left ``None``.

    ``available`` is the whole-snapshot verdict: it is ``True`` only when a snapshot that
    carried this run's token was read. ``fields`` is keyed by the app's required contract
    and always carries every key, so a reader never has to guess whether a key is absent or
    simply unknown.
    """

    available: bool
    schema_confirmed: bool
    source: str
    reason: str
    run_id: str | None
    fields: Mapping[str, object]

    def field(self, name: str) -> object:
        return self.fields.get(name)

    def as_dict(self) -> dict[str, object]:
        document: dict[str, object] = {
            "available": self.available,
            # Availability proves correlation only. Schema confirmation separately requires
            # the native contract's exact nine keys and exact safe value types.
            "schema_confirmed": self.schema_confirmed,
            "source": self.source,
            "reason": self.reason,
            "run_id": self.run_id,
        }
        for key in REQUIRED_NATIVE_DIAGNOSTICS_KEYS:
            value = self.fields.get(key)
            document[key] = list(value) if isinstance(value, tuple) else value
        return document


def _unavailable(source: str, reason: str, *, run_id: str | None = None) -> NativeDiagnostics:
    return NativeDiagnostics(False, False, source, reason, run_id, _fields())


def _as_exact_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_count(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _as_allowlisted(value: object, allowed: tuple[str, ...]) -> str | None:
    """The value if it is exactly one of ``allowed``, else ``None`` - never echoed."""
    if isinstance(value, str) and value in allowed:
        return value
    return None


def _as_subscriptions(value: object) -> tuple[str, ...] | None:
    """Acked channel labels from the native ``{label: bool}`` mapping.

    Only labels in :data:`REPORT_CHANNEL_LABELS` survive; anything else is dropped, never
    echoed, so a native snapshot cannot smuggle an account id, key or frame text into the
    report through a subscription name. A value that carried entries but named no channel
    this app knows is reported as unknown (``None``) rather than as an empty, confident set.
    """
    if not isinstance(value, Mapping):
        return None
    known = {key for key in value if isinstance(key, str)}
    acked = tuple(sorted(
        label for label in known
        if label in REPORT_CHANNEL_LABELS and value.get(label) is True
    ))
    had_entries = len(value) > 0
    recognized = any(label in REPORT_CHANNEL_LABELS for label in known)
    if had_entries and not recognized:
        return None
    return acked


def _schema_is_confirmed(snapshot: Mapping[object, object],
                         fields: Mapping[str, object]) -> bool:
    """Whether ``snapshot`` exactly matches the frozen, sanitized native contract."""
    expected_keys = {NATIVE_RUN_TOKEN_KEY, *REQUIRED_NATIVE_DIAGNOSTICS_KEYS}
    if set(snapshot.keys()) != expected_keys:
        return False
    subscriptions = snapshot.get(NATIVE_SUBSCRIPTIONS_KEY)
    if not isinstance(subscriptions, Mapping):
        return False
    if set(subscriptions.keys()) != set(REPORT_CHANNEL_LABELS):
        return False
    if any(not isinstance(subscriptions.get(label), bool) for label in REPORT_CHANNEL_LABELS):
        return False
    return all(fields.get(key) is not None for key in REQUIRED_NATIVE_DIAGNOSTICS_KEYS)


def _as_identity(value: object) -> str | None:
    return value if value in IDENTITY_VALUES else None


def normalize_field(key: str, value: object) -> object:
    """Coerce one snapshot member to the app's vocabulary, or ``None`` when it is unknown."""
    if key == NATIVE_LOGIN_KEY:
        return _as_exact_bool(value)
    if key == NATIVE_SUBSCRIPTIONS_KEY:
        return _as_subscriptions(value)
    if key == NATIVE_RUN_STATE_KEY:
        return _as_allowlisted(value, RUN_STATE_VALUES)
    if key == NATIVE_IDENTITY_KEY:
        return _as_identity(value)
    if key == NATIVE_SHUTDOWN_KEY:
        return _as_allowlisted(value, SHUTDOWN_STATUS_VALUES)
    return _as_count(value)


def read_native_diagnostics(target: object | None, *,
                            run_id: str) -> NativeDiagnostics:
    """Read this run's native snapshot from ``target``, or report it unsupported.

    ``target`` is whatever native object the interface declares as the snapshot host. The
    accessor may be a mapping-valued attribute or a zero-argument method. Any failure,
    absence or token mismatch fails closed with a named source; it never raises out of this
    reader, because a diagnostics read must not be able to fail a session that otherwise
    ran.
    """
    if target is None:
        return _unavailable(
            SOURCE_NO_TARGET,
            "no native object was available to read a diagnostics snapshot from, so the "
            "native read-only telemetry is unknown rather than unsupported or passed",
        )
    try:
        accessor = getattr(target, NATIVE_DIAGNOSTICS_ACCESSOR, None)
    except Exception:
        # A fixed category, never the exception type name: an error string can carry a value.
        return _unavailable(
            SOURCE_LOOKUP_FAILED,
            f"reading {NATIVE_DIAGNOSTICS_ACCESSOR} raised an exception: the snapshot is "
            f"unknown, never guessed",
        )
    if accessor is None:
        return _unavailable(
            SOURCE_ACCESSOR_ABSENT,
            f"the installed adapter exposes no {NATIVE_DIAGNOSTICS_ACCESSOR} accessor: "
            f"{UNSUPPORTED_REASON}",
        )
    snapshot = accessor
    if callable(snapshot):
        try:
            snapshot = snapshot()
        except Exception:
            return _unavailable(
                SOURCE_LOOKUP_FAILED,
                f"calling {NATIVE_DIAGNOSTICS_ACCESSOR} raised an exception: the snapshot "
                f"is unknown, never guessed",
            )
    if not isinstance(snapshot, Mapping):
        return _unavailable(
            SOURCE_NOT_A_MAPPING,
            f"{NATIVE_DIAGNOSTICS_ACCESSOR} returned a non-mapping value: the snapshot is "
            f"unknown, never coerced",
        )
    token = snapshot.get(NATIVE_RUN_TOKEN_KEY)
    if not isinstance(token, str) or not token.strip():
        return _unavailable(
            SOURCE_NO_RUN_TOKEN,
            f"the snapshot carries no {NATIVE_RUN_TOKEN_KEY}, so it cannot be proven to "
            f"belong to this run and is not merged",
        )
    if token != run_id:
        # The foreign token is deliberately not echoed: a native run id is not a value this
        # report should carry, and the app's own run id is already in the report.
        return _unavailable(
            SOURCE_CROSS_RUN,
            "the snapshot belongs to a different run and is not merged: recovery is never "
            "claimed from a separate, unrelated run, and its token is not echoed here",
        )
    fields = {key: normalize_field(key, snapshot.get(key))
              for key in REQUIRED_NATIVE_DIAGNOSTICS_KEYS}
    schema_confirmed = _schema_is_confirmed(snapshot, fields)
    return NativeDiagnostics(
        True,
        schema_confirmed,
        SOURCE_NATIVE,
        "the snapshot carries this run's token; only recognized counters and fixed labels "
        "are reported, and an unrecognized value is dropped rather than echoed. Schema "
        + ("matches the confirmed native interface"
           if schema_confirmed else "does not exactly match the confirmed native interface"),
        run_id=token,
        fields=fields,
    )


def native_diagnostics_document(diagnostics: NativeDiagnostics) -> dict[str, object]:
    """The report's sanitized view: counters and fixed labels only, never a payload."""
    return diagnostics.as_dict()

#!/usr/bin/env python3
"""Ondo Perps bounded session probe (plan R4, task R4.1).

    src/ondo_probe.py --mode public --minutes 2
    src/ondo_probe.py --mode account-readonly --minutes 1
    src/ondo_probe.py --mode paper --minutes 1
    src/ondo_probe.py --mode sandbox --symbols NVDA --instrument NVDA-USD-PERP.ONDO \
        --notional-usd 10 --max-orders 2 --max-exposure-usd 25 --allow-sandbox-orders
    src/ondo_probe.py --mode sandbox --symbols NVDA --instrument NVDA-USD-PERP.ONDO \
        --notional-usd 10 --max-orders 2 --max-exposure-usd 25 --allow-sandbox-orders --dry-run

One Ondo session, one bounded deadline, one published run:

    <out>/runs/<run_id>/    this attempt alone: its payload and its own manifest
    <out>/probe.json        the published view - the accounting of the run that just ended
    <out>/meta.json         written last, by temp-file + rename: the commit point

The four modes differ in what may be *sent*, and the report says which was which rather
than collapsing them into one verdict:

* ``public`` reads the venue's public surface and registers no execution client at all;
* ``account-readonly`` syncs a private account and stops there: ``account_read_only``, no
  dead-man switch armed, no cancel path, never trading-ready;
* ``paper`` runs the framework's simulated execution client, so every order, fill, balance
  and position it reports is ``synthetic`` and no remote write exists to make;
* ``sandbox`` is the only writing mode, and it starts only when the caller names every
  bound (instrument, per-order notional, order count, total exposure) *and* passes
  ``--allow-sandbox-orders`` *and* the resolved endpoint is the allowlisted sandbox
  authority. Submission, when it happens, goes through the native factory and client
  only: this file implements no REST signing, no HMAC, no POST retry, and must never
  grow one - that is the adapter's job and a second implementation of it would be the
  one thing this probe could do that is worse than doing nothing.

Three things this probe refuses to claim, and they are the reason it exists in this shape:

1. **No protocol verification has ever happened.**  Not one request has reached the real
   Ondo venue. The REST auth header names, the WS login signature concatenation order,
   the real private frame shapes and the dead-man-switch renewal semantics are all
   *documented, not host-confirmed*. ``protocol_verified`` is therefore ``False`` in
   every report this file can produce, and no field of it may be read as a host fact.
2. **Exit code 0 does not mean the account was cleaned.**  The adapter has an ordered
   bounded stop executor (``OndoAccountRuntime::stop_and_wait``:
   ``CancelOwnOrders -> ConfirmOwnOrders -> ReleaseDeadMansSwitch -> ClosePrivateStream``)
   and it is tested in Rust - but the client lifecycle never calls it: its only call site
   is ``crates/adapters/ondo/tests/private_runtime.rs``.  Framework ``stop()`` and
   ``disconnect()`` do not cancel this run's orders, do not confirm the cancels and do not
   release the dead-man switch, so on shutdown this client drops the transport and leaves
   orders behind.  That is R3 acceptance section 7 item 1, it is the prerequisite for the
   R5 sandbox submission, and it is why
   ``exit_code_zero_means_clean_account`` is ``False`` and ``converging_stop_available``
   is ``False`` with that reason attached.
3. **A refusal is not an outcome.**  A refused invocation is a startup rejection: the
   reason goes to stderr, the exit code is 2, no client is constructed, no request is
   sent, and nothing at all is written to disk - the tool never started, so there is no
   attempt to record and inventing a run directory for it would fill the evidence tree
   with entries that record nothing but a typo.

The four hard caps below are module constants applied with ``min(flag, CAP)``.  No flag,
configuration file or environment variable can widen them, and each is reported in the
frozen three-way shape (configured / cap / applied) so a reader can see which one bound.

R4 sends no order of its own in any mode.  The plan's four bounds are the *authorization
envelope* that must be complete before a writing client is armed at all, and the report's
accounting axes are the ledger R5 will fill: what this run observed is what an injected
session observed, and ``submitted`` is empty for every mode here.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import ipaddress
import json
import os
import re
import shutil
import sys
import threading
import time
import traceback
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from urllib.parse import urlsplit

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from ondo_preflight import (  # noqa: E402  (needs sys.path above)
    BUILD_POINTER,
    RUNS_DIRNAME,
    STAGING_PREFIX,
    new_run_id,
)

from nautilus_trader.adapters.sandbox import (  # noqa: E402
    SandboxExecutionClientConfig,
    SandboxExecutionClientFactory,
)
from nautilus_trader.common import Environment, LogLevel, LoggerConfig  # noqa: E402
from nautilus_trader.config import LiveRiskEngineConfig  # noqa: E402
from nautilus_trader.live import LiveNode  # noqa: E402
from nautilus_trader.model import AccountId, InstrumentId, Money, TraderId, Venue  # noqa: E402

# ------------------------------------------------------------------------ identity

ONDO_ADAPTER = "nautilus_trader.adapters.ondo"
ONDO_VENUE = "ONDO"
VEHICLE_SUFFIX = ".ONDO"
PROBE_FILE = "probe"
META_FILE = "meta"
PAYLOAD_FILES = (PROBE_FILE,)  # this tool's own artifacts, and nothing else is touched
SCHEMA_VERSION = 1
PROBE_INSTANCE = "ONDO-PROBE-001"
TRADER_ID = "ONDO-PROBE-001"
CONNECTION_TIMEOUT_SECS = 10

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_REFUSED = 2
EXIT_TIMEOUT = 3

MODE_PUBLIC = "public"
MODE_ACCOUNT_READONLY = "account-readonly"
MODE_PAPER = "paper"
MODE_SANDBOX = "sandbox"
MODES = (MODE_PUBLIC, MODE_ACCOUNT_READONLY, MODE_PAPER, MODE_SANDBOX)

# Modes whose session is authenticated, and therefore the only ones that resolve a
# credential or read a ``.env`` file. ``public`` and ``paper`` never read either.
CREDENTIAL_MODES = (MODE_ACCOUNT_READONLY, MODE_SANDBOX)
# Modes whose clients are pointed at the sandbox authority. The endpoint allowlist is
# enforced for these and only these: ``public`` and ``paper`` read the *production*
# public surface on purpose, and a refusal there would contradict the mode's meaning.
SANDBOX_ENDPOINT_MODES = (MODE_ACCOUNT_READONLY, MODE_SANDBOX)
# Modes whose data client reads the production public surface.
PRODUCTION_DATA_MODES = (MODE_PUBLIC, MODE_PAPER)

DEFAULT_SYMBOLS = "NVDA,TSLA"
DEFAULT_OUT = "reports/ondo-probe"
DEFAULT_MINUTES = 2.0  # finite by default: there is no unbounded default deadline

# --------------------------------------------------------------------- hard caps

# Chosen as the smallest round numbers that keep a sandbox probe a probe: 50 USD per
# order and 100 USD of total exposure cannot move a market, and 10 orders cannot build a
# position worth reconciling. ``min(flag, CAP)`` is the only arithmetic applied to them.
MAX_NOTIONAL_PER_ORDER_USD_CAP = Decimal("50")
MAX_EXPOSURE_USD_CAP = Decimal("100")
MAX_ORDERS_CAP = 10
MAX_MINUTES_CAP = 60.0

# ----------------------------------------------------------------- credentials

# The adapter's Rust side reads exactly these two names from the process environment.
# It is handed no credential by this file, and neither value is ever read, rendered,
# logged or copied into a report: only their presence is checked.
API_KEY_VARIABLE = "ONDO_SANDBOX_API_KEY"
API_SECRET_VARIABLE = "ONDO_SANDBOX_API_SECRET"
# The third name is app-side only - the adapter does not read it. This probe resolves it
# and passes it in as the execution config's ``account_id``.
ACCOUNT_ID_VARIABLE = "ONDO_SANDBOX_ACCOUNT_ID"
CREDENTIAL_VARIABLES = (API_KEY_VARIABLE, API_SECRET_VARIABLE, ACCOUNT_ID_VARIABLE)

# ------------------------------------------------------------ endpoint allowlist

# A positive allowlist over the *parsed* endpoint, mirroring the adapter's R0 endpoint
# policy (crates/adapters/ondo/src/common/endpoint.rs) so this file cannot be the weaker
# of the two gates. Compared as scheme + normalised host, never as a substring: a host
# that merely *contains* the sandbox host is a different authority
# (``api.ondoperps-sandbox.xyz.evil.example``), and an unparsed string is refused rather
# than passed through unjudged.
SANDBOX_ENDPOINT_ALLOWLIST: tuple[tuple[str, str], ...] = (
    ("https", "api.ondoperps-sandbox.xyz"),
)
SANDBOX_BASE_URL_HTTP = SANDBOX_ENDPOINT_ALLOWLIST[0][0] + "://" + SANDBOX_ENDPOINT_ALLOWLIST[0][1]
# Bound to the domain the same way the adapter binds its own constants to its policy, so
# the host this file reports and the host it refuses cannot drift apart.
PRODUCTION_HOST = "api.ondoperps.xyz"
PRODUCTION_DOMAIN = "ondoperps.xyz"
PRODUCTION_BASE_URL_HTTP = "https://" + PRODUCTION_HOST

ENDPOINT_OFFICIAL = "official"
ENDPOINT_LOOPBACK = "loopback"
ENDPOINT_PRODUCTION = "production"
ENDPOINT_REFUSED = "refused"

DEFAULT_PORTS = {"https": 443, "wss": 443, "http": 80, "ws": 80}

# ------------------------------------------------------------------- stop states

STOP_RUN_RETURNED = "run-returned"
STOP_DEADLINE_NORMAL = "deadline (a normal end for this mode)"
STOP_DEADLINE_SANDBOX = "deadline (the sandbox terminal state)"
STOP_KEYBOARD_INTERRUPT = "keyboard-interrupt"
STOP_EXCEPTION = "exception"
STOP_NOT_STARTED = "not-started"

# The converging stop exists in Rust and is tested there; the client lifecycle never
# calls it. This string is the citation both booleans below point at.
CONVERGING_STOP_REASON = (
    "the adapter's ordered bounded stop executor (OndoAccountRuntime::stop_and_wait: "
    "CancelOwnOrders -> ConfirmOwnOrders -> ReleaseDeadMansSwitch -> ClosePrivateStream) "
    "is tested in Rust but has no call site outside "
    "crates/adapters/ondo/tests/private_runtime.rs: the client lifecycle never reaches "
    "it, so the framework stop()/disconnect() this probe can call does not cancel this "
    "run's orders, does not confirm the cancels and does not release the dead-man "
    "switch. The client drops the transport and leaves orders behind (R3 acceptance "
    "section 7 item 1); this is the prerequisite the R5 sandbox submission is blocked on"
)
PROTOCOL_VERIFIED_REASON = (
    "no request has ever been sent to the real Ondo venue: REST auth header names, the WS "
    "login signature concatenation order, the real private frame shapes and the dead-man-"
    "switch renewal semantics are documented, not host-confirmed. Nothing in this report "
    "may be read as a host fact"
)

# The accounting buckets, named once so the report keys and the classifiers cannot drift.
BUCKET_SUBMITTED = "submitted"
BUCKET_ACKED = "acked"
BUCKET_FILLED = "filled"
BUCKET_PARTIAL = "partial"
BUCKET_CANCELED = "canceled"
BUCKET_UNKNOWN = "unknown"
BUCKET_NO_TRADE = "no_trade"

LOOKUP_PENDING = "pending"
LOOKUP_UNKNOWN = "unknown"

ORDER_STATUS_ACKED = frozenset(
    {
        "SUBMITTED",
        "ACCEPTED",
        "PENDING_UPDATE",
        "PENDING_CANCEL",
        "RELEASED",
        "TRIGGERED",
    },
)
ORDER_STATUS_FILLED = frozenset({"FILLED"})
ORDER_STATUS_PARTIAL = frozenset({"PARTIALLY_FILLED"})
ORDER_STATUS_CANCELED = frozenset({"CANCELED", "CANCELLED", "EXPIRED"})
# A rejection is the venue establishing that no trade happened, which is the only way
# ``no_trade`` is ever populated. An *absent* answer is never treated as one.
ORDER_STATUS_NO_TRADE = frozenset({"REJECTED", "DENIED"})


class OndoProbeError(RuntimeError):
    """This probe cannot run at all (missing adapter, unusable plan, bad value)."""


class OndoProbeRefused(OndoProbeError):
    """A startup rejection: exit 2, no client constructed, no request sent, no write."""


# ------------------------------------------------------------------ adapter seam


@dataclass(frozen=True)
class OndoAdapter:
    """The adapter's public classes, resolved only when a session is actually built.

    Mirrors ``ondo_preflight``'s holder: the import happens in :func:`load_adapter`, never
    at module import time, so this file - and any test that imports it - works whether or
    not the candidate wheel is installed.
    """

    OndoEnvironment: object
    OndoDataClientConfig: object
    OndoDataClientFactory: object
    OndoExecutionClientConfig: object
    OndoExecutionClientFactory: object


def load_adapter() -> OndoAdapter:
    """Import the adapter's public classes, or say clearly that they are absent."""
    try:
        from nautilus_trader.adapters.ondo import (
            OndoDataClientConfig,
            OndoDataClientFactory,
            OndoEnvironment,
            OndoExecutionClientConfig,
            OndoExecutionClientFactory,
        )
    except ImportError as exc:
        raise OndoProbeError(
            f"the Ondo adapter is not importable from {ONDO_ADAPTER}: {BUILD_POINTER} ({exc!r})",
        ) from exc
    return OndoAdapter(
        OndoEnvironment=OndoEnvironment,
        OndoDataClientConfig=OndoDataClientConfig,
        OndoDataClientFactory=OndoDataClientFactory,
        OndoExecutionClientConfig=OndoExecutionClientConfig,
        OndoExecutionClientFactory=OndoExecutionClientFactory,
    )


# ------------------------------------------------------------ endpoint allowlist


@dataclass(frozen=True)
class EndpointVerdict:
    """Which authority a base URL names, as a decision about the parsed URL.

    ``url`` is always the *reconstructed* ``scheme://host[:port]``, never the string that
    was handed in: a base URL carrying userinfo carries a credential in it, and a refusal
    message is the last place that should surface.
    """

    url: str
    scheme: str
    host: str
    endpoint_class: str
    reason: str | None

    @property
    def allowed(self) -> bool:
        return self.endpoint_class == ENDPOINT_OFFICIAL

    def as_dict(self) -> dict[str, object]:
        return {
            "url": self.url,
            "scheme": self.scheme,
            "host": self.host,
            "endpoint_class": self.endpoint_class,
            "reason": self.reason,
        }


def _allowlisted(scheme: str, host: str) -> bool:
    return any(scheme == allowed_scheme and host == allowed_host
               for allowed_scheme, allowed_host in SANDBOX_ENDPOINT_ALLOWLIST)


def _is_production_host(host: str) -> bool:
    """Whether ``host`` is inside the production domain (refused by name, first)."""
    return host == PRODUCTION_DOMAIN or host.endswith("." + PRODUCTION_DOMAIN)


def classify_endpoint(url: str) -> EndpointVerdict:
    """Classify one base URL against the sandbox allowlist, mirroring the adapter's R0.

    The rules, in the adapter's order: parse; refuse a URL with no host; refuse a
    production-domain host by name; refuse userinfo; admit a loopback *address* (a name is
    not an address - ``localhost`` resolves through something this policy cannot read);
    then require the allowlisted host, the family's official scheme and no non-default
    port. The parser is the check: ``HTTPS://API.ONDOPERPS-SANDBOX.XYZ``,
    ``https://api.ondoperps-sandbox.xyz.`` (one root dot) and an explicit ``:443`` are one
    authority, and a host that merely contains the sandbox host is another.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").rstrip(".").lower()
    try:
        port = parts.port
    except ValueError:  # a non-numeric port: the parser read it, and it is not a port
        return EndpointVerdict(f"{scheme}://{parts.netloc}", scheme, "", ENDPOINT_REFUSED,
                               f"the port in {scheme}://{parts.netloc!r} is not a number")

    if not scheme or not host:
        return EndpointVerdict(url if not scheme else f"{scheme}://", scheme, host,
                               ENDPOINT_REFUSED,
                               f"{url!r} does not parse as an absolute URL with a host, so "
                               f"the endpoint it would be dialled as cannot be judged")

    shown = f"{scheme}://{host}" + (f":{port}" if port is not None else "")

    if _is_production_host(host):
        return EndpointVerdict(shown, scheme, host, ENDPOINT_REFUSED,
                               f"the host {host!r} belongs to the production Ondo Perps "
                               f"domain, so a sandbox credential may not be sent to it")

    if parts.username is not None or parts.password is not None:
        return EndpointVerdict(shown, scheme, host, ENDPOINT_REFUSED,
                               f"the base URL for {host!r} carries userinfo; a credential "
                               f"belongs in the credential store, not in a URL")

    bare = host.strip("[]")
    try:
        loopback = ipaddress.ip_address(bare).is_loopback
    except ValueError:
        loopback = False
    if loopback:
        return EndpointVerdict(shown, scheme, host, ENDPOINT_LOOPBACK,
                               f"{shown} is a loopback test service, not the sandbox "
                               f"authority {SANDBOX_BASE_URL_HTTP}")

    if not _allowlisted(scheme, host):
        wanted = ", ".join(f"{s}://{h}" for s, h in SANDBOX_ENDPOINT_ALLOWLIST)
        return EndpointVerdict(shown, scheme, host, ENDPOINT_REFUSED,
                               f"{shown} is not an endpoint this session may sign for: the "
                               f"allowlist for a sandbox session is exactly {wanted}, and a "
                               f"host that only contains an allowlisted name is a different "
                               f"authority")

    if port is not None and port != DEFAULT_PORTS.get(scheme):
        return EndpointVerdict(shown, scheme, host, ENDPOINT_REFUSED,
                               f"{shown} names a port the sandbox authority does not listen "
                               f"on: an explicit port may only be the scheme's default")

    return EndpointVerdict(shown, scheme, host, ENDPOINT_OFFICIAL, None)


def sandbox_endpoint_refusal(url: str) -> str | None:
    """Why ``url`` may not be signed for, or ``None`` when it is the sandbox authority.

    Loopback is admitted by the adapter for its own offline tests and refused here: the
    adapter's exemption exists so a *mock* can be dialled by a client under test, and a
    probe that reports ``write_capable`` for a session aimed at a mock would be reporting
    a sandbox session that does not exist. This gate is therefore strictly narrower than
    the adapter's, never wider.
    """
    verdict = classify_endpoint(url)
    if verdict.allowed:
        return None
    if verdict.endpoint_class == ENDPOINT_LOOPBACK:
        return (
            f"{verdict.url} is a loopback test service and not the allowlisted sandbox "
            f"authority {SANDBOX_BASE_URL_HTTP}: the adapter admits a loopback mock for its "
            f"own offline tests, and this probe does not - a session aimed at a mock must "
            f"never be reported as a sandbox session"
        )
    return verdict.reason


def production_endpoint_verdict(url: str) -> EndpointVerdict:
    """The authority the public surface is read from: judged for the record, not gated.

    ``public`` and ``paper`` read the production public surface on purpose, so the sandbox
    allowlist is the wrong question for them and asking it would report every production
    session as refused. A URL that is not the production public surface is still *reported*
    as refused - that is what the mode's own HTTP client would do with it - but it is not
    turned into a startup refusal here, because a production read is not what the sandbox
    gate protects and widening the refusal set would be this file inventing policy.
    """
    parts = urlsplit(url)
    scheme = (parts.scheme or "").lower()
    host = (parts.hostname or "").rstrip(".").lower()
    shown = f"{scheme}://{host}" if scheme else url
    if scheme == "https" and host == PRODUCTION_HOST:
        return EndpointVerdict(shown, scheme, host, ENDPOINT_PRODUCTION, None)
    return EndpointVerdict(shown, scheme, host, ENDPOINT_REFUSED,
                           f"{shown} is not the production public surface this mode reads "
                           f"({PRODUCTION_BASE_URL_HTTP})")


def resolve_endpoint(mode: str, adapter: OndoAdapter | None) -> tuple[str, str]:
    """The endpoint this session would use, and where that value came from.

    With no injected adapter the answer is this module's declared authority for the mode.
    With one, the endpoint the adapter's own execution config actually carries is read
    back - a configuration object is a value, not a client, so this stays on the safe side
    of "no client is constructed before a refusal". The adapter is the second, independent
    gate on the same decision (its R0 policy runs before a credential is read), so the two
    cannot disagree in a way that widens either.

    A production mode's endpoint is the module constant and not the adapter's: those modes
    read the public surface with the *data* client, and the adapter binds its own default
    to the same host (``consts::ONDO_HTTP_BASE_URL_PRODUCTION``), so reading a value back
    would only be a second way to say the same thing.
    """
    if mode not in SANDBOX_ENDPOINT_MODES:
        return PRODUCTION_BASE_URL_HTTP, "module-default"
    if adapter is None:
        return SANDBOX_BASE_URL_HTTP, "module-default"
    config = _exec_config(adapter, mode, account_id=None, base_url_http=None)
    carried = getattr(config, "base_url_http", None)
    if isinstance(carried, str) and carried:
        return carried, "adapter-config"
    return SANDBOX_BASE_URL_HTTP, "adapter-default"


# ------------------------------------------------------------------------ plan


@dataclass(frozen=True)
class CapRow:
    """One bound in the frozen three-way shape: configured, cap, applied.

    ``live_limits.Row`` is the house version of this shape; a cap here is never a refusal
    (the flag is honoured up to the cap and the row shows that it bound), because a
    silently raised value and a silently clamped one are both invisible, and only the
    three numbers together are not.
    """

    key: str
    configured: object
    cap: object
    applied: object

    @property
    def capped(self) -> bool:
        return self.configured != self.applied

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": _json_scalar(self.configured),
            "cap": _json_scalar(self.cap),
            "applied": _json_scalar(self.applied),
            "capped": self.capped,
        }

    def line(self) -> str:
        mark = "  <- the cap bound this value" if self.capped else ""
        return (
            f"  {self.key:<30} configured={_fmt(self.configured):>10}  "
            f"cap={_fmt(self.cap):>10}  applied={_fmt(self.applied):>10}{mark}"
        )


@dataclass(frozen=True)
class Plan:
    """Everything the invocation resolved to, before any client exists."""

    mode: str
    environment: str
    symbols: tuple[str, ...]
    targets: tuple[dict[str, str], ...]
    out_dir: Path
    log_level: str
    dry_run: bool
    minutes: float
    instruments: tuple[str, ...]
    notional_usd: Decimal | None
    max_orders: int | None
    max_exposure_usd: Decimal | None
    allow_sandbox_orders: bool
    credentials_required: bool
    endpoint: str
    endpoint_source: str
    endpoint_class: str
    caps: tuple[CapRow, ...]
    read_capable: bool
    write_capable: bool
    dms_armed: bool
    cancel_capable: bool
    expected_stop_condition: str

    @property
    def venue(self) -> str:
        return ONDO_VENUE

    @property
    def synthetic(self) -> bool:
        return self.mode == MODE_PAPER

    def cap(self, key: str) -> CapRow:
        for row in self.caps:
            if row.key == key:
                return row
        raise OndoProbeError(f"no cap named {key!r}")  # pragma: no cover - internal

    def instrument_ids(self) -> tuple[str, ...]:
        """The instrument ids this session's data client would load.

        The CLI envelope's ``--instrument`` names an *execution* instrument; the data
        client loads the ids the symbol mapping yields. One session, one id per symbol.
        """
        return tuple(row["instrument_id"] for row in self.targets)


def mode_facts(mode: str) -> dict[str, object]:
    """The per-mode banner facts: environment, what may be read, what may be sent."""
    if mode == MODE_PUBLIC:
        return {
            "environment": "production",
            "read_capable": True,
            "write_capable": False,
            "dms_armed": False,
            "cancel_capable": False,
            "expected_stop_condition": STOP_DEADLINE_NORMAL,
        }
    if mode == MODE_ACCOUNT_READONLY:
        return {
            "environment": "sandbox",
            "read_capable": True,
            "write_capable": False,
            "dms_armed": False,
            "cancel_capable": False,
            "expected_stop_condition": STOP_DEADLINE_NORMAL,
        }
    if mode == MODE_PAPER:
        return {
            "environment": "production",
            "read_capable": True,
            "write_capable": False,  # the simulated client makes no remote write at all
            "dms_armed": False,
            "cancel_capable": False,
            "expected_stop_condition": STOP_DEADLINE_NORMAL,
        }
    if mode == MODE_SANDBOX:
        return {
            "environment": "sandbox",
            "read_capable": True,
            "write_capable": True,
            "dms_armed": True,
            "cancel_capable": True,
            "expected_stop_condition": STOP_DEADLINE_SANDBOX,
        }
    raise OndoProbeError(f"unknown mode {mode!r}")  # pragma: no cover - argparse guards


# ------------------------------------------------------------------- formatting


def _fmt(value: object) -> str:
    if value is None:
        return "-"
    if isinstance(value, Decimal):
        return format(value.normalize(), "f") if value == value.to_integral_value() else str(value)
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _json_scalar(value: object) -> object:
    """A JSON-safe rendering of a bound: money stays a decimal *string*, never a float."""
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


def _json_default(value: object) -> object:
    """Serialize only what a report may carry; refuse everything else loudly.

    A blanket ``default=str`` would also stringify a config object or a credential
    holder, which is exactly the accident this module must not have.
    """
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"{type(value).__name__} is not a report value")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _attr(obj: object, name: str, default: object = None) -> object:
    """Read ``obj.name`` defensively - a venue object's surface is not this file's to assume."""
    try:
        return getattr(obj, name, default)
    except Exception:  # pragma: no cover - a property that raises is not a value
        return default


def _text(value: object) -> str | None:
    if value is None:
        return None
    try:
        return str(value)
    except Exception:  # pragma: no cover - str() failing is not this file's problem
        return None


# ------------------------------------------------------------------- arguments


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    """The frozen 11-flag surface. There are no default order parameters."""
    parser = argparse.ArgumentParser(
        prog="ondo_probe",
        description=(
            "Bounded Ondo Perps session probe: one mode, one deadline, one published run. "
            "No order parameter has a default, and only --mode sandbox may carry one."
        ),
    )
    parser.add_argument("--mode", choices=MODES, default=MODE_PUBLIC,
                        help="what this session may do (default: public, read-only)")
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                        help=f"comma-separated symbols to load (default: {DEFAULT_SYMBOLS})")
    parser.add_argument("--minutes", type=float, default=DEFAULT_MINUTES,
                        help=f"run deadline in minutes, capped at {MAX_MINUTES_CAP:g} "
                             f"(default: {DEFAULT_MINUTES:g}; there is no unbounded default)")
    parser.add_argument("--out", default=DEFAULT_OUT,
                        help=f"report directory (default: {DEFAULT_OUT})")
    parser.add_argument("--log-level", choices=("DEBUG", "INFO", "WARNING", "ERROR"),
                        default="INFO", help="node log level (default: INFO)")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved plan and exit: no network, no client, no .env")
    parser.add_argument("--instrument", action="append", metavar="VENUE_SYMBOL",
                        help="sandbox only; repeatable execution instrument id")
    parser.add_argument("--notional-usd", type=Decimal, metavar="DECIMAL",
                        help="sandbox only; per-order notional ceiling in USD")
    parser.add_argument("--max-orders", type=int, metavar="INT",
                        help="sandbox only; maximum orders this session may submit")
    parser.add_argument("--max-exposure-usd", type=Decimal, metavar="DECIMAL",
                        help="sandbox only; total exposure ceiling in USD")
    parser.add_argument("--allow-sandbox-orders", action="store_true",
                        help="sandbox only; required acknowledgement that this mode may write")
    return parser.parse_args(argv)


def parse_symbols(raw: str) -> tuple[str, ...]:
    symbols = tuple(part.strip().upper() for part in str(raw).split(",") if part.strip())
    if not symbols:
        raise OndoProbeRefused("--symbols resolved to no symbol at all")
    return symbols


def resolve_targets(symbols: Sequence[str]) -> tuple[dict[str, str], ...]:
    """The symbol -> market -> instrument mapping, from the preflight's own table."""
    from ondo_preflight import OndoPreflightError, targets  # local: keeps the import cheap

    try:
        rows = targets(symbols)
    except OndoPreflightError as exc:
        raise OndoProbeRefused(f"--symbols {','.join(symbols)}: {exc}") from exc
    return tuple(dict(row) for row in rows)


def _validate_instruments(instruments: Sequence[str]) -> tuple[str, ...]:
    """Accept only syntactically valid ONDO instrument ids, and say why otherwise."""
    seen: list[str] = []
    for raw in instruments:
        value = raw.strip()
        if not value:
            raise OndoProbeRefused("--instrument was given an empty value")
        if not re.fullmatch(r"[A-Za-z0-9._-]+\.[A-Za-z0-9_]+", value):
            raise OndoProbeRefused(
                f"--instrument {value!r} is not a <symbol>.<venue> instrument id",
            )
        if not value.upper().endswith(VEHICLE_SUFFIX):
            raise OndoProbeRefused(
                f"--instrument {value!r} names a venue other than {ONDO_VENUE}: this session "
                f"is an {ONDO_VENUE} session and may only carry {VEHICLE_SUFFIX} instruments",
            )
        if value not in seen:
            seen.append(value)  # a repeated flag is one instrument, not two orders' worth
    return tuple(seen)


def _positive_decimal(flag: str, value: Decimal | None) -> Decimal:
    if value is None:  # pragma: no cover - the caller checks presence first
        raise OndoProbeRefused(f"{flag} is required by --mode sandbox")
    if value <= 0:
        raise OndoProbeRefused(f"{flag} must be greater than zero, got {_fmt(value)}")
    return value


def _positive_int(flag: str, value: int | None) -> int:
    if value is None:  # pragma: no cover - the caller checks presence first
        raise OndoProbeRefused(f"{flag} is required by --mode sandbox")
    if value <= 0:
        raise OndoProbeRefused(f"{flag} must be greater than zero, got {value}")
    return value


def resolve_plan(args: argparse.Namespace, adapter: OndoAdapter | None = None) -> Plan:
    """Turn the parsed flags into a plan, or refuse to start.

    Every check in here runs before a client exists: that is what makes a refusal a
    startup rejection rather than a failed session. The order is deliberate - what the
    *mode* permits, then the sandbox envelope, then the endpoint the session would be
    dialled at. Credentials are not resolved here at all (see :func:`check_credentials`),
    so this function reads no environment and no file.
    """
    facts = mode_facts(args.mode)
    instruments = _validate_instruments(args.instrument or ())
    write_flags = _write_flags_present(args)

    # Rule 1 / rule 6. The same rule, two messages: production is public and paper, and
    # account-readonly is not a writing mode either. Naming the flag, the mode and the set
    # of modes that *would* accept it is what makes the refusal actionable.
    for flag in write_flags:
        if args.mode == MODE_SANDBOX:
            continue
        if args.mode in (MODE_PUBLIC, MODE_PAPER):
            raise OndoProbeRefused(
                f"{flag} is a sandbox-only parameter and --mode {args.mode} is a production "
                f"mode: production (--mode public, --mode paper) may not carry any write "
                f"parameter, and --mode sandbox is the only mode that may",
            )
        raise OndoProbeRefused(
            f"{flag} is a sandbox-only parameter and --mode {args.mode} may not write: "
            f"account-readonly syncs an account and stops there, and --mode sandbox is the "
            f"only mode that may carry a write parameter",
        )

    if args.minutes is None or args.minutes != args.minutes or args.minutes in (
        float("inf"), float("-inf"),
    ):
        raise OndoProbeRefused("--minutes must be a finite number")
    if args.minutes <= 0:
        raise OndoProbeRefused(
            f"--minutes must be greater than zero, got {args.minutes:g}: a non-positive "
            f"deadline is not a bounded run",
        )

    # Rule 2: the acknowledgement comes before the bounds, because it is the flag that
    # says the caller knows what the other four are for.
    if args.mode == MODE_SANDBOX and not args.allow_sandbox_orders:
        raise OndoProbeRefused(
            "--mode sandbox writes to the venue and requires --allow-sandbox-orders: the "
            "acknowledgement is the flag that distinguishes a sandbox session from every "
            "other mode, and it is never implied by the bounds being present",
        )

    # Rule 3: name every missing bound, not the first one found.
    if args.mode == MODE_SANDBOX:
        missing = [
            flag for flag, value in (
                ("--instrument", instruments or None),
                ("--notional-usd", args.notional_usd),
                ("--max-orders", args.max_orders),
                ("--max-exposure-usd", args.max_exposure_usd),
            ) if value is None
        ]
        if missing:
            raise OndoProbeRefused(
                f"--mode sandbox is missing {', '.join(missing)}: a sandbox session starts "
                f"only with all four bounds named - instrument, per-order notional, order "
                f"count and total exposure - so that the envelope is never whatever the "
                f"defaults happened to be",
            )
        _positive_decimal("--notional-usd", args.notional_usd)
        _positive_decimal("--max-exposure-usd", args.max_exposure_usd)
        _positive_int("--max-orders", args.max_orders)

    caps = (
        CapRow("minutes", args.minutes, MAX_MINUTES_CAP, min(args.minutes, MAX_MINUTES_CAP)),
        CapRow(
            "notional_per_order_usd",
            args.notional_usd,
            MAX_NOTIONAL_PER_ORDER_USD_CAP,
            min(args.notional_usd, MAX_NOTIONAL_PER_ORDER_USD_CAP)
            if args.notional_usd is not None else None,
        ),
        CapRow(
            "exposure_usd",
            args.max_exposure_usd,
            MAX_EXPOSURE_USD_CAP,
            min(args.max_exposure_usd, MAX_EXPOSURE_USD_CAP)
            if args.max_exposure_usd is not None else None,
        ),
        CapRow(
            "orders",
            args.max_orders,
            MAX_ORDERS_CAP,
            min(args.max_orders, MAX_ORDERS_CAP) if args.max_orders is not None else None,
        ),
    )

    endpoint, endpoint_source = resolve_endpoint(args.mode, adapter)
    # Rule 4, for the modes whose own environment is the sandbox. public and paper read
    # the production public surface on purpose and are not gated by the sandbox allowlist.
    if args.mode in SANDBOX_ENDPOINT_MODES:
        refusal = sandbox_endpoint_refusal(endpoint)
        if refusal is not None:
            raise OndoProbeRefused(
                f"--mode {args.mode} would sign for an endpoint outside the sandbox "
                f"allowlist (resolved from {endpoint_source}): {refusal}",
            )
        verdict = classify_endpoint(endpoint)
    else:
        verdict = production_endpoint_verdict(endpoint)
    symbols = parse_symbols(args.symbols)

    return Plan(
        mode=args.mode,
        environment=str(facts["environment"]),
        symbols=symbols,
        targets=resolve_targets(symbols),
        out_dir=Path(args.out),
        log_level=str(args.log_level),
        dry_run=bool(args.dry_run),
        minutes=float(min(args.minutes, MAX_MINUTES_CAP)),
        instruments=instruments,
        notional_usd=args.notional_usd,
        max_orders=args.max_orders,
        max_exposure_usd=args.max_exposure_usd,
        allow_sandbox_orders=bool(args.allow_sandbox_orders),
        credentials_required=args.mode in CREDENTIAL_MODES,
        endpoint=endpoint,
        endpoint_source=endpoint_source,
        endpoint_class=verdict.endpoint_class,
        caps=caps,
        read_capable=bool(facts["read_capable"]),
        write_capable=bool(facts["write_capable"]),
        dms_armed=bool(facts["dms_armed"]),
        cancel_capable=bool(facts["cancel_capable"]),
        expected_stop_condition=str(facts["expected_stop_condition"]),
    )


def _write_flags_present(args: argparse.Namespace) -> list[str]:
    """The sandbox-only flags this invocation actually carried, in a stable order."""
    present: list[str] = []
    if args.instrument:
        present.append("--instrument")
    if args.notional_usd is not None:
        present.append("--notional-usd")
    if args.max_orders is not None:
        present.append("--max-orders")
    if args.max_exposure_usd is not None:
        present.append("--max-exposure-usd")
    if args.allow_sandbox_orders:
        present.append("--allow-sandbox-orders")
    return present


# ---------------------------------------------------------------- environment


def load_environment(environ) -> dict[str, str]:
    """The environment a session resolves credentials from.

    An explicitly injected mapping is the *whole* environment: ``.env`` is not consulted
    for it, or an injected empty environment would silently acquire the real one and a
    refusal could never be reproduced. Only the real process environment gets the
    ``python-dotenv`` convenience, and only for a mode that reads a credential at all -
    ``public`` and ``paper`` never read ``.env``, and neither does ``--dry-run``.
    """
    if environ is not None:
        return {str(key): str(value) for key, value in dict(environ).items()}
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is a convenience, not a requirement
        pass
    return dict(os.environ)


def check_credentials(mode: str, environ: dict[str, str]) -> None:
    """Refuse a credentialed mode whose credential names are missing or empty.

    Only the *names* are reported, and only their presence is checked: the values are
    never read into a message, a log line or a report. A missing credential is a refusal
    (exit 2), never a failure and never a pass - "not configured yet" and "verified" are
    different answers and this probe does not blur them.
    """
    if mode not in CREDENTIAL_MODES:
        return
    missing = [name for name in CREDENTIAL_VARIABLES if not str(environ.get(name) or "").strip()]
    if missing:
        raise OndoProbeRefused(
            f"--mode {mode} needs credentials and {', '.join(missing)} "
            f"{'is' if len(missing) == 1 else 'are'} missing or empty in the environment. "
            f"Their values are never read into this message. Note that "
            f"{ACCOUNT_ID_VARIABLE} is read by this probe alone: the adapter reads "
            f"{API_KEY_VARIABLE} and {API_SECRET_VARIABLE} from the process environment, "
            f"and is handed no credential by this file",
        )
    try:
        # Parsed here so that an unusable account id is a startup rejection, and reported
        # by variable name - the value never appears in the message, valid or not.
        AccountId.from_str(str(environ[ACCOUNT_ID_VARIABLE]).strip())
    except Exception as exc:
        raise OndoProbeRefused(
            f"{ACCOUNT_ID_VARIABLE} is not an account id this session can be opened with "
            f"({type(exc).__name__}); its value is not printed here",
        ) from exc


# ------------------------------------------------------------------------ config


def _exec_config(adapter: OndoAdapter, mode: str, *, account_id: AccountId | None,
                 base_url_http: str | None) -> object:
    """The execution client config for ``mode``, carrying no credential value.

    The credential is *not* passed in: the adapter resolves it from the process
    environment through its own gate-first path, and an explicit override would put the
    secret in an object this file owns. ``environment`` is sandbox for every credentialed
    mode because the adapter refuses to authenticate against production at all.
    """
    environment = adapter.OndoEnvironment.SANDBOX
    kwargs: dict[str, object] = {"environment": environment, "account_read_only": False}
    if account_id is not None:
        kwargs["account_id"] = account_id
    if base_url_http is not None:
        kwargs["base_url_http"] = base_url_http
    if mode == MODE_ACCOUNT_READONLY:
        kwargs["account_read_only"] = True
    config = adapter.OndoExecutionClientConfig(**kwargs)
    # The adapter's config object exposes no api_key/api_secret attribute and this file
    # never asks for one; if that ever changes, this assertion is where it shows up.
    for forbidden in ("api_key", "api_secret"):
        if getattr(config, forbidden, None) is not None:
            raise OndoProbeError(
                f"the execution config exposed {forbidden}: this probe must never hold or "
                f"render a credential value",
            )
    return config


def _data_config(adapter: OndoAdapter, plan: Plan) -> object:
    environment = (
        adapter.OndoEnvironment.PRODUCTION if plan.mode in PRODUCTION_DATA_MODES
        else adapter.OndoEnvironment.SANDBOX
    )
    return adapter.OndoDataClientConfig(
        environment=environment,
        load_ids=[InstrumentId.from_str(row["instrument_id"]) for row in plan.targets],
    )


def _risk_config(plan: Plan) -> LiveRiskEngineConfig:
    """The risk engine rail: no bypass, and a per-order notional bound where one applies.

    R4 submits nothing, so this is a rail and not a limiter of anything that happens. It
    is set where a client could accept a submission (paper's simulated client, sandbox's
    real one) so that the bound is the envelope the operator named rather than whatever a
    later edit to the submission path might assume.
    """
    bounds: dict[str, str] = {}
    if plan.mode == MODE_SANDBOX:
        applied = plan.cap("notional_per_order_usd").applied
        if applied is not None:
            bounds = {instrument: str(applied) for instrument in plan.instruments}
    elif plan.mode == MODE_PAPER:
        bounds = {
            plan.targets[0]["instrument_id"]: str(MAX_NOTIONAL_PER_ORDER_USD_CAP)
        } if plan.targets else {}
    return LiveRiskEngineConfig(bypass=False, max_notional_per_order=bounds)


def _node_environment(mode: str) -> Environment:
    """The framework's environment label for the node.

    Only paper's execution client is the framework's own simulated one, so only paper is
    labelled SANDBOX; every mode with a real client is LIVE. The venue a session talks to
    is decided by the client config's ``OndoEnvironment``, not by this label, and the
    label must not be read as one.
    """
    return Environment.SANDBOX if mode == MODE_PAPER else Environment.LIVE


# ------------------------------------------------------------------------- stop


def _node_running(node: object) -> bool:
    value = _attr(node, "is_running", False)
    if callable(value):
        try:
            value = value()
        except Exception:  # pragma: no cover - a raising probe reads as "still running"
            return True
    return bool(value)


def resolve_stop_target(node: object | None) -> tuple[object | None, str]:
    """Return ``(target, target_name)`` for stopping ``node`` - **call on the owning thread**.

    A ``LiveNode`` is declared ``unsendable`` in PyO3 (``crates/live/src/python/node.rs``),
    and that check is a *thread id* check: reading any attribute of the node from another
    thread trips a Rust assertion, which arrives in Python as a ``PanicException`` deriving
    from ``BaseException`` - so no ``except Exception`` here would catch it, the blocking
    ``run()`` on the owning thread would never return, and the report would never be
    published. That is not a hypothetical: a deadline stop written that way killed the run
    it was meant to end.

    The framework's answer is ``LiveNodeHandle``, documented as "safe to call from any
    thread or from a signal handler" and as "the supported way to stop a hosted run", and
    ``LiveNode``'s own doc says to capture it *before* starting a run. So the handle is
    resolved here, on the owning thread, before ``run()`` blocks, and the watchdog is handed
    only the result. A double without a handle keeps the node itself, which is safe because
    a double is not a PyO3 object; a real node always has one.
    """
    if node is None:
        return None, "none"
    getter = _attr(node, "handle")
    if callable(getter):
        try:
            handle = getter()
        except Exception:  # pragma: no cover - a node whose handle() raises has no handle
            handle = None
        if handle is not None and callable(_attr(handle, "stop")):
            return handle, "handle"
    if callable(_attr(node, "stop")):
        return node, "node"
    return None, "none"


def bounded_stop(target: object | None, *, label: str, via: str = "node",
                 grace_secs: float = 2.0, poll_secs: float = 0.05) -> dict[str, object]:
    """Stop ``target`` and wait for it, for a bounded number of poll iterations.

    ``target`` is whatever :func:`resolve_stop_target` returned - a ``LiveNodeHandle`` for a
    real node - and never the node itself from a foreign thread.

    Bounded by *iteration count*, not by a clock read: a wall clock is the injected
    ``now`` and is not consulted here, and no clock this file holds can extend the wait.
    What this cannot do is the point of the report - the framework stop does not cancel
    this run's orders, confirm the cancels or release the dead-man switch, so a clean
    stop here says nothing about what the venue still holds.
    """
    outcome: dict[str, object] = {
        "label": label,
        "attempted": target is not None,
        "requested": False,
        "stopped": False,
        "error": None,
        "iterations": 0,
        "stop_target": via,
        "cancels_own_orders": False,
        "confirms_cancels": False,
        "releases_dead_mans_switch": False,
    }
    if target is None:
        return outcome
    stop = _attr(target, "stop")
    if not callable(stop):
        outcome["error"] = "the stop target exposed no stop()"
        return outcome
    try:
        stop()
        outcome["requested"] = True
    except Exception as exc:
        outcome["error"] = f"{type(exc).__name__}: {exc}"
    iterations = max(1, int(grace_secs / poll_secs))
    for index in range(iterations):
        outcome["iterations"] = index + 1
        if not _node_running(target):
            outcome["stopped"] = True
            break
        time.sleep(poll_secs)
    return outcome


def start_stop_watchdog(done_event: threading.Event, stop_callable: Callable[[], object],
                        timeout_secs: float) -> tuple[threading.Thread, dict[str, bool]]:
    """Stop the node when the run signals done, or when the deadline elapses.

    This mirrors ``exec_probe.start_stop_watchdog`` rather than importing it: that module
    imports the Aster adapter at import time, and docs/ondo.md section 8 keeps the Aster
    probe and this path independent. ``node.run()`` blocks, so an out-of-process stop is
    the only way a deadline can end a session at all.
    """
    state = {"fired": False, "timed_out": False, "stopped": False}

    def _run() -> None:
        fired = done_event.wait(timeout_secs)
        state["fired"] = fired
        state["timed_out"] = not fired
        try:
            stop_callable()
        finally:
            state["stopped"] = True

    thread = threading.Thread(target=_run, name="ondo-probe-watchdog", daemon=True)
    thread.start()
    return thread, state


# ----------------------------------------------------------------------- ledger


@dataclass
class Fill:
    """One fill, keyed by client order id wherever it is recorded."""

    client_order_id: str
    status: str
    filled_qty: str | None
    avg_px: str | None
    synthetic: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "status": self.status,
            "filled_qty": self.filled_qty,
            "avg_px": self.avg_px,
            "synthetic": self.synthetic,
        }


@dataclass
class Ledger:
    """What the session observed, recorded once per fact.

    Every collection is keyed by client order id, so a receipt that arrives twice
    overwrites rather than appends: a duplicate ack and a duplicate fill are the same
    fact delivered twice, and counting either twice would inflate the accounting in the
    direction that hides a problem. The duplicate itself is counted separately, so the
    report can show that it happened without acting on it.
    """

    synthetic: bool = False
    submitted: dict[str, str] = field(default_factory=dict)  # id -> where it was sent
    acked: dict[str, str] = field(default_factory=dict)  # id -> status at the ack
    filled: dict[str, Fill] = field(default_factory=dict)
    partial: dict[str, Fill] = field(default_factory=dict)
    canceled: dict[str, str] = field(default_factory=dict)  # id -> terminal status
    unknown: dict[str, str] = field(default_factory=dict)  # id -> why it is unattributable
    no_trade: dict[str, str] = field(default_factory=dict)  # id -> how absence was established
    pending_ids: dict[str, str] = field(default_factory=dict)
    duplicates: dict[str, int] = field(default_factory=lambda: {"acked": 0, "fills": 0})
    lookups: dict[str, dict[str, object]] = field(default_factory=dict)

    # -- recording ---------------------------------------------------------

    def note_submitted(self, client_order_id: str, where: str) -> None:
        self.submitted.setdefault(client_order_id, where)

    def note_acked(self, client_order_id: str, status: str) -> None:
        if client_order_id in self.acked:
            self.duplicates["acked"] += 1
        self.acked[client_order_id] = status

    def note_fill(self, fill: Fill) -> None:
        """Record a fill or partial fill, counting a repeated receipt only once."""
        bucket = self.filled if fill.status in ORDER_STATUS_FILLED else self.partial
        if fill.client_order_id in bucket:
            self.duplicates["fills"] += 1
        bucket[fill.client_order_id] = fill
        for other in (self.partial, self.filled):
            if other is not bucket and fill.client_order_id in other:
                del other[fill.client_order_id]  # a partial that later fills is one order

    def note_canceled(self, client_order_id: str, status: str) -> None:
        self.canceled[client_order_id] = status
        self.pending_ids.pop(client_order_id, None)

    def note_no_trade(self, client_order_id: str, reason: str) -> None:
        """Record an *established* absence - the venue said so, nobody merely stopped asking."""
        self.no_trade[client_order_id] = reason
        self.pending_ids.pop(client_order_id, None)

    def note_unknown(self, client_order_id: str, reason: str) -> None:
        self.unknown[client_order_id] = reason

    def note_pending(self, client_order_id: str, reason: str) -> None:
        """An id whose outcome is not established stays pending, and exit never clears it."""
        self.pending_ids[client_order_id] = reason

    def note_cancel_unconfirmed(self, client_order_id: str, status_code: int | None) -> None:
        """A cancel the venue acknowledged, whose confirmation query then failed.

        The acknowledgement stands and the follow-up failed, so the order is *still
        pending*: not canceled, not unknown, and above all not "no trade".
        """
        bucket, reason = classify_lookup_failure(status_code, client_order_id)
        self.lookups[client_order_id] = {"bucket": bucket, "status_code": status_code,
                                         "reason": reason}
        if bucket == LOOKUP_UNKNOWN:
            self.note_unknown(client_order_id, reason)
        else:
            self.note_pending(client_order_id, reason)

    # -- rendering ---------------------------------------------------------

    def settle(self) -> None:
        """Close the accounting: an id with *no* recorded outcome stays pending.

        The fail-closed final pass, and the axes stay disjoint: an id that already carries
        a classification is not listed a second time, and one that carries none is listed
        here rather than being quietly absent or defaulted to "no trade".

        Note what "pending" does *not* mean: an order that was acknowledged and is still
        working is ``outstanding_orders``, not a pending id. ``pending_ids`` is the list of
        ids this run asked about and could not get an answer for - the ones that must be
        chased - and it is never cleared by exiting.
        """
        resolved = (
            set(self.acked) | set(self.filled) | set(self.partial) | set(self.canceled)
            | set(self.no_trade) | set(self.unknown)
        )
        for client_order_id in self.submitted:
            if client_order_id in resolved:
                continue
            self.pending_ids.setdefault(
                client_order_id,
                "the send was recorded and no acknowledgement, fill, cancel or rejection "
                "was ever attributed to it",
            )


def classify_lookup_failure(status_code: int | None, client_order_id: str) -> tuple[str, str]:
    """Classify a failed follow-up query about one order, fail-closed.

    A 404 is the case this function exists for: the venue not attributing an id is not
    evidence that no order exists - it is a *missing answer*, and treating it as "no
    trade" is how an outstanding order becomes an invisible one. It lands in ``unknown``.
    Every other failure (a transport error, a 5xx, a timeout) leaves the earlier
    acknowledgement standing, so the id stays ``pending``.
    """
    if status_code == 404:
        return LOOKUP_UNKNOWN, (
            f"the follow-up query for {client_order_id} answered 404: the venue did not "
            f"attribute the id, which is a missing answer rather than an established "
            f"absence, so this id is not counted as no-trade"
        )
    if status_code is None:
        return LOOKUP_PENDING, (
            f"the follow-up query for {client_order_id} did not complete (no status): the "
            f"earlier acknowledgement stands and the outcome is unestablished"
        )
    return LOOKUP_PENDING, (
        f"the follow-up query for {client_order_id} failed with status {status_code}: the "
        f"earlier acknowledgement stands and the outcome is unestablished"
    )


def observe_cache(cache: object, *, venue: str, synthetic: bool) -> dict[str, object]:
    """What the session's cache holds at exit, in the report's own vocabulary.

    Read defensively throughout: an order object is the venue layer's, and a probe that
    crashed while rendering one would lose the accounting it exists to publish. Anything
    unrecognised is reported as unattributable rather than dropped - a status this file
    does not know is not a status this file may silently ignore.
    """
    observation: dict[str, object] = {
        "orders": {},
        "outstanding": [],
        "account": None,
        "errors": [],
    }
    if cache is None:
        return observation
    orders: dict[str, dict[str, object]] = {}

    def _consider(order: object) -> None:
        client_order_id = _text(_attr(order, "client_order_id")) or _text(_attr(order, "id"))
        if not client_order_id:
            observation["errors"].append("an order in the cache carried no id")
            return
        status = (_text(_attr(order, "status")) or "UNKNOWN").upper()
        filled_qty = _text(_attr(order, "filled_qty"))
        avg_px = _text(_attr(order, "avg_px"))
        record = {
            "client_order_id": client_order_id,
            "status": status,
            "filled_qty": filled_qty,
            "avg_px": avg_px,
            "instrument_id": _text(_attr(order, "instrument_id")),
            "synthetic": synthetic,
        }
        orders[client_order_id] = record

    for name in ("orders_open", "orders_closed", "orders"):
        reader = _attr(cache, name)
        if not callable(reader):
            continue
        try:
            for order in list(reader() or []):
                _consider(order)
        except Exception as exc:
            observation["errors"].append(f"cache.{name}() failed: {type(exc).__name__}: {exc}")

    open_reader = _attr(cache, "orders_open")
    if callable(open_reader):
        try:
            observation["outstanding"] = [
                orders.get(_text(_attr(order, "client_order_id")) or "", {})
                or {"client_order_id": _text(_attr(order, "client_order_id")),
                    "status": (_text(_attr(order, "status")) or "UNKNOWN").upper(),
                    "synthetic": synthetic}
                for order in list(open_reader() or [])
            ]
        except Exception as exc:
            observation["errors"].append(f"cache.orders_open() failed: {type(exc).__name__}: {exc}")

    account_reader = _attr(cache, "account_for_venue")
    if callable(account_reader):
        try:
            account = account_reader(Venue.from_str(venue))
            observation["account"] = None if account is None else {
                "account_id": _text(_attr(account, "id")),
                "synthetic": synthetic,
            }
        except Exception as exc:
            observation["errors"].append(
                f"cache.account_for_venue() failed: {type(exc).__name__}: {exc}",
            )
    observation["orders"] = orders
    return observation


def merge_observation(ledger: Ledger, observation: dict[str, object]) -> None:
    """Fold what the cache holds into the ledger, without letting it overwrite an answer.

    The cache is the client's view and the ledger is what the session recorded; where they
    disagree, the ledger's resolved outcome wins and the cache's order is only rendered.
    """
    for client_order_id, record in (observation.get("orders") or {}).items():
        if not isinstance(record, dict):  # pragma: no cover - observe_cache controls this
            continue
        status = str(record.get("status") or "UNKNOWN").upper()
        if status in ORDER_STATUS_FILLED or status in ORDER_STATUS_PARTIAL:
            ledger.note_fill(Fill(
                client_order_id=client_order_id,
                status=status,
                filled_qty=_text(record.get("filled_qty")),
                avg_px=_text(record.get("avg_px")),
                synthetic=bool(record.get("synthetic")),
            ))
            if status in ORDER_STATUS_FILLED:
                ledger.pending_ids.pop(client_order_id, None)
            continue
        if status in ORDER_STATUS_CANCELED:
            ledger.note_canceled(client_order_id, status)
            continue
        if status in ORDER_STATUS_NO_TRADE:
            ledger.note_no_trade(client_order_id, f"the venue reported {status}")
            continue
        if status in ORDER_STATUS_ACKED:
            ledger.note_acked(client_order_id, status)
            continue
        if status == "INITIALIZED":
            ledger.note_unknown(
                client_order_id,
                "the order exists locally as INITIALIZED and was never sent: the venue has "
                "no record of it and its outcome is not an absence of trade",
            )
            continue
        ledger.note_unknown(
            client_order_id,
            f"the order's status {status!r} is not one this probe classifies, so it is "
            f"reported as unattributable rather than assumed",
        )


def ledger_document(ledger: Ledger, plan: Plan, observation: dict[str, object]) -> dict[str, object]:
    """The accounting axes, each separately readable and never collapsed into one total."""
    ledger.settle()
    fills = [fill.as_dict() for fill in ledger.filled.values()]
    partials = [fill.as_dict() for fill in ledger.partial.values()]
    return {
        BUCKET_SUBMITTED: sorted(ledger.submitted),
        BUCKET_ACKED: sorted(ledger.acked),
        BUCKET_FILLED: fills,
        BUCKET_PARTIAL: partials,
        BUCKET_CANCELED: [
            {"client_order_id": client_order_id, "status": status, "synthetic": plan.synthetic}
            for client_order_id, status in sorted(ledger.canceled.items())
        ],
        BUCKET_UNKNOWN: [
            {"client_order_id": client_order_id, "reason": reason}
            for client_order_id, reason in sorted(ledger.unknown.items())
        ],
        BUCKET_NO_TRADE: [
            {"client_order_id": client_order_id, "reason": reason}
            for client_order_id, reason in sorted(ledger.no_trade.items())
        ],
        "pending_ids": dict(sorted(ledger.pending_ids.items())),
        "outstanding_orders": list(observation.get("outstanding") or []),
        "duplicate_receipts": dict(ledger.duplicates),
        "lookup_failures": dict(sorted(ledger.lookups.items())),
        "cache_errors": list(observation.get("errors") or []),
    }


# ------------------------------------------------------------------------ node


def _invoke_node_factory(factory: Callable[..., object], builder: object,
                         plan: Plan) -> object:
    """Call the injected node factory with the arguments it actually accepts.

    The seam is used by callers other than this file, so the documented one-argument form
    (``factory(builder)``) and a form that also wants the plan both have to work; reading
    the signature is cheaper than a convention nobody can check.
    """
    try:
        signature = inspect.signature(factory)
    except (TypeError, ValueError):  # a builtin or C callable: no signature to read
        return factory(builder, plan)
    names = set(signature.parameters)
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in signature.parameters.values()):
        names |= {"builder", "plan"}
    kwargs: dict[str, object] = {}
    if "builder" in names:
        kwargs["builder"] = builder
    if "plan" in names:
        kwargs["plan"] = plan
    if kwargs:
        return factory(**kwargs)
    positional = [
        p for p in signature.parameters.values()
        if p.kind in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
    ]
    if len(positional) >= 2:
        return factory(builder, plan)
    return factory(builder)


def build_node(plan: Plan, adapter: OndoAdapter, node_factory: Callable[..., object] | None,
               *, account_id: AccountId | None) -> tuple[object, dict[str, object]]:
    """Configure one node for ``plan`` and hand it to the factory (or to ``build()``).

    Registration is by mode and nothing else: ``public`` registers no execution client at
    all, ``paper`` registers the framework's simulated client, and the two authenticated
    modes register the real one - read-only for ``account-readonly``, writable only for
    ``sandbox``. The returned dict is the banner: what was registered, and therefore what
    this session could have done.
    """
    registered: dict[str, object] = {
        "data_client": f"{ONDO_ADAPTER}.OndoDataClientFactory",
        "exec_client": None,
        "simulated": False,
        "account_read_only": plan.mode == MODE_ACCOUNT_READONLY,
        "node_environment": _node_environment(plan.mode).name,
    }
    builder = (
        LiveNode.builder(PROBE_INSTANCE, TraderId.from_str(TRADER_ID),
                         _node_environment(plan.mode))
        .with_logging(LoggerConfig(stdout_level=LogLevel.from_str(plan.log_level)))
        .with_risk_engine_config(_risk_config(plan))
        .with_timeout_connection(CONNECTION_TIMEOUT_SECS)
        .add_data_client(None, adapter.OndoDataClientFactory(), _data_config(adapter, plan))
    )

    if plan.mode in CREDENTIAL_MODES:
        exec_config = _exec_config(adapter, plan.mode, account_id=account_id,
                                   base_url_http=None)
        # The real object's own endpoint is checked again here, because this is the value
        # the client will actually dial; the plan check judged what the probe *resolved*.
        carried = getattr(exec_config, "base_url_http", None)
        if isinstance(carried, str) and carried:
            refusal = sandbox_endpoint_refusal(carried)
            if refusal is not None:
                # A failure, not a refusal: the plan already gated the endpoint this probe
                # resolved, so reaching here means the config disagrees with the plan, and
                # the session has begun - exit 2 is reserved for "nothing was constructed".
                raise OndoProbeError(
                    f"the execution config for --mode {plan.mode} carries an endpoint "
                    f"outside the sandbox allowlist, which the plan did not: {refusal}",
                )
        builder = builder.add_exec_client(None, adapter.OndoExecutionClientFactory(),
                                          exec_config)
        registered["exec_client"] = f"{ONDO_ADAPTER}.OndoExecutionClientFactory"
    elif plan.mode == MODE_PAPER:
        builder = builder.add_simulated_exec_client(
            None,
            SandboxExecutionClientFactory(),
            SandboxExecutionClientConfig(
                venue=Venue.from_str(ONDO_VENUE),
                # A simulated account's size is not a result: it only has to be large
                # enough not to bind an envelope capped at 100 USD of exposure, and every
                # balance it reports is marked synthetic in this report either way.
                starting_balances=[Money.from_str("100000 USDC")],
            ),
        )
        registered["exec_client"] = "nautilus_trader.adapters.sandbox.SandboxExecutionClientFactory"
        registered["simulated"] = True

    factory = node_factory if node_factory is not None else _default_node_factory
    return _invoke_node_factory(factory, builder, plan), registered


def _default_node_factory(builder: object) -> object:
    return builder.build()


# ------------------------------------------------------------------------ report


def _write_json(path: Path, payload: object) -> dict[str, object]:
    """Write one JSON document atomically (temporary file + replace) and describe it."""
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False,
                      default=_json_default) + "\n"
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return {"path": path.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def _publish_file(source: Path, target: Path) -> dict[str, object]:
    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return {"path": target.name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}


def write_report(payload: dict[str, object], out_dir: Path, *, run_id: str, complete: bool,
                 meta: dict[str, object], failure: str | None = None) -> dict[str, object]:
    """Publish one attempt: stage it under its run id, then switch the published view.

    This mirrors ``ondo_preflight``'s pattern instead of calling it, because that
    function's ``PAYLOAD_FILES`` is a module constant fixed to the preflight's five
    documents - reusing it would publish a ``probe.json`` beside an ``instruments.json``
    this tool never wrote and an ``missing.json`` that would be a lie. The pattern is the
    point, not the function: stage under ``<out>/runs/<run_id>/``, rename that directory
    into place in one step, then replace ``<out>/meta.json`` last, by temporary file, as
    the commit point. A reader then always finds the manifest of the attempt that just
    ended - a failed attempt writes its own ``complete: false`` and its reason, so a
    previous attempt's ``complete: true`` is never left standing.
    """
    out_dir = Path(out_dir)
    runs_dir = out_dir / RUNS_DIRNAME
    staging = runs_dir / f"{STAGING_PREFIX}{run_id}"
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)

    manifest = dict(meta)
    manifest["run_id"] = run_id
    manifest["complete"] = bool(complete)
    manifest["failure"] = failure if failure is not None else meta.get("failure")
    manifest["run_dir"] = f"{RUNS_DIRNAME}/{run_id}"
    files: dict[str, object] = {}
    for name in PAYLOAD_FILES:
        if name in payload:
            files[name] = _write_json(staging / f"{name}.json", payload[name])
    manifest["files"] = files
    _write_json(staging / f"{META_FILE}.json", manifest)

    final_dir = runs_dir / run_id
    if final_dir.exists():
        shutil.rmtree(final_dir)
    os.replace(staging, final_dir)

    for name in PAYLOAD_FILES:
        produced = final_dir / f"{name}.json"
        target = out_dir / f"{name}.json"
        if name in files:
            files[name] = _publish_file(produced, target)
        elif target.exists():
            target.unlink()
    manifest["files"] = files
    _write_json(out_dir / f"{META_FILE}.json", manifest)
    return manifest


def verify_run(out_dir: Path) -> dict[str, object]:
    """Check the published run against the identity and hashes its own manifest names.

    ``complete: true`` is not taken on trust: every named sha256 is recomputed here, so a
    publishing interrupted between its payload and its manifest reads as a mismatch rather
    than as a tidy success.
    """
    out_dir = Path(out_dir)
    problems: list[str] = []
    result: dict[str, object] = {
        "out": str(out_dir),
        "run_id": None,
        "complete": False,
        "verified": False,
        "problems": problems,
    }
    meta_path = out_dir / f"{META_FILE}.json"
    try:
        manifest = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append(f"{meta_path} cannot be read: {exc!r}")
        return result
    if not isinstance(manifest, dict):
        problems.append(f"{meta_path} is not an object")
        return result
    result["run_id"] = manifest.get("run_id")
    result["complete"] = manifest.get("complete") is True
    if not result["run_id"]:
        problems.append(f"{meta_path} names no run id: the run's identity is unverifiable")
    if not result["complete"]:
        problems.append(
            f"the published run {result['run_id']} is not complete: "
            f"{manifest.get('failure') or 'no reason recorded'}",
        )
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        problems.append(f"{meta_path} names no files to verify")
        return result
    for name, entry in files.items():
        path = out_dir / f"{name}.json"
        if not path.exists():
            problems.append(f"{name}.json is named by the manifest but is missing")
            continue
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if isinstance(entry, dict):
            if entry.get("sha256") != digest:
                problems.append(
                    f"{name}.json does not match the manifest's sha256: the published file "
                    f"and the manifest are not from the same run",
                )
            elif entry.get("bytes") != len(data):
                problems.append(f"{name}.json is {len(data)} bytes, the manifest says "
                                f"{entry.get('bytes')}")
    result["verified"] = not problems
    return result


# ------------------------------------------------------------------ documents


def banner_document(plan: Plan, *, run_id: str | None = None) -> dict[str, object]:
    """The startup banner facts, shared by ``--dry-run`` and every published report."""
    return {
        "mode": plan.mode,
        "environment": plan.environment,
        "read_capable": plan.read_capable,
        "write_capable": plan.write_capable,
        "dms_armed": plan.dms_armed,
        "cancel_capable": plan.cancel_capable,
        "synthetic": plan.synthetic,
        "expected_stop_condition": plan.expected_stop_condition,
        "deadline_secs": Decimal(str(plan.minutes)) * 60,
        "endpoint": plan.endpoint,
        "endpoint_source": plan.endpoint_source,
        "endpoint_class": plan.endpoint_class,
        "credentials_required": plan.credentials_required,
        "credential_variables": list(CREDENTIAL_VARIABLES) if plan.credentials_required else [],
        "run_id": run_id,
    }


def envelope_document(plan: Plan) -> dict[str, object]:
    """The authorization envelope: the bounds this run is actually held to.

    Each bound is the **applied** value - the flag after ``min(flag, CAP)`` - because that is
    the number the run is enforced against: ``notional_usd`` here is what ``_risk_config``
    puts on the risk engine's ``max_notional_per_order``. What the operator asked for is not
    lost, it stays in ``caps`` as ``configured`` beside the cap that bound it; publishing the
    requested value here as well would put two contradictory numbers for one bound into the
    same report, and the larger of the two would be the one that is not true.
    """
    return {
        "instruments": list(plan.instruments),
        "notional_usd": _json_scalar(plan.cap("notional_per_order_usd").applied),
        "max_orders": _json_scalar(plan.cap("orders").applied),
        "max_exposure_usd": _json_scalar(plan.cap("exposure_usd").applied),
        "allow_sandbox_orders": plan.allow_sandbox_orders,
        "armed": plan.write_capable,
    }


def caps_document(plan: Plan) -> dict[str, object]:
    return {row.key: row.as_dict() for row in plan.caps}


def caps_lines(plan: Plan) -> list[str]:
    return [row.line() for row in plan.caps]


def plan_document(plan: Plan) -> dict[str, object]:
    """The ``--dry-run`` document: what this invocation resolved to, and what it refuses.

    Everything the run would announce, and the three facts the run would carry, with the
    counts that make "no client, no request, no read" checkable rather than asserted.
    """
    document: dict[str, object] = {
        "tool": "ondo_probe",
        "schema_version": SCHEMA_VERSION,
        "venue": ONDO_VENUE,
        "dry_run": True,
        "client_constructed": False,
        "requests_sent": 0,
        "env_file_read": False,
        "symbols": list(plan.symbols),
        "targets": [dict(row) for row in plan.targets],
        "instrument_ids": list(plan.instrument_ids()),
        # POSIX form: a published document should not need to say which platform wrote it.
        "out": plan.out_dir.as_posix(),
        "log_level": plan.log_level,
        "caps": caps_document(plan),
        "envelope": envelope_document(plan),
        "protocol_verified": False,
        "protocol_verified_reason": PROTOCOL_VERIFIED_REASON,
        "exit_code_zero_means_clean_account": False,
        "converging_stop_available": False,
        "converging_stop_reason": CONVERGING_STOP_REASON,
        "unverified": unverified_document(),
        "converging_stop": converging_stop_document(),
    }
    document.update(banner_document(plan))
    return document


def converging_stop_document() -> dict[str, object]:
    return {
        "available": False,
        "reason": CONVERGING_STOP_REASON,
        "cancels_own_orders": False,
        "confirms_cancels": False,
        "releases_dead_mans_switch": False,
        "blocks": "R5 sandbox submission (R3 acceptance section 7 item 1)",
    }


def unverified_document() -> list[dict[str, object]]:
    """The two facts this probe will not let a reader infer as verified."""
    return [
        {
            "item": "protocol",
            "verified": False,
            "reason": PROTOCOL_VERIFIED_REASON,
            "documented_not_confirmed": [
                "REST authentication header names",
                "the WebSocket login signature concatenation order",
                "the real private frame shapes",
                "dead-man-switch renewal and release semantics",
            ],
        },
        {
            "item": "converging_stop",
            "verified": False,
            "reason": CONVERGING_STOP_REASON,
            "consequence": (
                "a zero exit code must not be read as a cleaned account: outstanding "
                "orders and pending ids are reported instead"
            ),
        },
    ]


def report_document(plan: Plan, *, run_id: str, started: datetime, finished: datetime,
                    exit_code: int, stop_condition: str, failure: str | None,
                    node: dict[str, object] | None, ledger: Ledger,
                    observation: dict[str, object], stops: Sequence[dict[str, object]],
                    watchdog: dict[str, bool] | None) -> dict[str, object]:
    """The published report: the axes, the banner, and the two honest denials."""
    document: dict[str, object] = {
        "tool": "ondo_probe",
        "schema_version": SCHEMA_VERSION,
        "venue": ONDO_VENUE,
        "run_id": run_id,
        "run_dir": f"{RUNS_DIRNAME}/{run_id}",
        "started_at_utc": started.isoformat(timespec="milliseconds"),
        "finished_at_utc": finished.isoformat(timespec="milliseconds"),
        "complete": exit_code == EXIT_OK and failure is None,
        "failure": failure,
        "exit_code": exit_code,
        "stop_condition": stop_condition,
        "caps": caps_document(plan),
        "envelope": envelope_document(plan),
        "session": node or {},
        "watchdog": dict(watchdog) if watchdog is not None else None,
        "stops": [dict(stop) for stop in stops],
        "account_reconciled": bool(observation.get("account")),
        "account": observation.get("account"),
        "account_coverage_proven": False,
        "account_coverage_reason": (
            "this run requested no venue response, so no coverage of the account could be "
            "proven from one: an account object in the cache is the client's view, not a "
            "reconciliation against the venue"
        ),
        "protocol_verified": False,
        "protocol_verified_reason": PROTOCOL_VERIFIED_REASON,
        "exit_code_zero_means_clean_account": False,
        "converging_stop_available": False,
        "converging_stop_reason": CONVERGING_STOP_REASON,
        "converging_stop": converging_stop_document(),
        "unverified": unverified_document(),
        "orders_submitted_by_probe": 0,
    }
    document.update(banner_document(plan, run_id=run_id))
    document.update(ledger_document(ledger, plan, observation))
    return document


# ------------------------------------------------------------------------- run


def execute(plan: Plan, *, adapter: OndoAdapter, node_factory: Callable[..., object] | None,
            environ: dict[str, str], now: Callable[[], datetime],
            out_dir: Path, log: Callable[[str], None]) -> int:
    """Run one bounded session and publish it. Every exit path stops and publishes.

    The report is written from a ``finally`` block, so a ``KeyboardInterrupt``, a deadline,
    a watchdog stop, a login failure the framework turns into an exception and any other
    exception all still produce their own ``complete: false`` with the reason - the run's
    accounting is never lost to the way it ended.
    """
    # Only a credentialed mode has one: public and paper never resolve a credential, so
    # the account id is absent rather than empty for them.
    account_id = (
        AccountId.from_str(str(environ[ACCOUNT_ID_VARIABLE]).strip())
        if plan.credentials_required else None
    )
    started = now()
    run_id = new_run_id(started)
    ledger = Ledger(synthetic=plan.synthetic)
    observation: dict[str, object] = {"orders": {}, "outstanding": [], "account": None,
                                      "errors": []}
    stops: list[dict[str, object]] = []
    watchdog_state: dict[str, bool] | None = None
    node: object | None = None
    session: dict[str, object] | None = None
    # The watchdog must never touch the node itself. A `LiveNode` is unsendable in PyO3, so
    # an attribute read from another thread trips a Rust assertion and the run dies where it
    # stood instead of stopping - which is exactly what a deadline stop did before this held
    # the framework's thread-safe `LiveNodeHandle`. Both are filled on this thread, below,
    # before `run()` blocks; the lists are the closure's slot because the watchdog is
    # started before the node it will stop has been built.
    stop_target: list[object] = []
    stop_via: list[str] = ["none"]
    done = threading.Event()
    failure: str | None = None
    stop_condition = STOP_NOT_STARTED
    exit_code = EXIT_OK

    def _stop_holder() -> dict[str, object]:
        outcome = bounded_stop(stop_target[0] if stop_target else None, label="watchdog",
                               via=stop_via[0])
        stops.append(outcome)
        return outcome

    try:
        node, session = build_node(plan, adapter, node_factory, account_id=account_id)
        # Resolved here, on the thread that built the node and before `run()` blocks: the
        # watchdog is only ever handed this, never the node.
        target, via = resolve_stop_target(node)
        stop_target.append(target)
        stop_via[0] = via
        # The deadline is exactly the applied minutes: flooring it here would make the
        # report's own ``deadline_secs`` disagree with how long the session could run.
        watchdog, watchdog_state = start_stop_watchdog(
            done, _stop_holder, float(plan.minutes) * 60.0,
        )
        log(f"ondo_probe: banner\n{json.dumps(banner_document(plan, run_id=run_id), indent=2, default=_json_default)}")
        log("ondo_probe: caps\n" + "\n".join(caps_lines(plan)))
        log(f"ondo_probe: envelope {json.dumps(envelope_document(plan), default=_json_default)}")
        run = _attr(node, "run")
        if not callable(run):
            raise OndoProbeError("the node exposed no run(): nothing can be started")
        run()
        if watchdog_state["timed_out"]:
            stop_condition = (
                STOP_DEADLINE_SANDBOX if plan.mode == MODE_SANDBOX else STOP_DEADLINE_NORMAL
            )
            if plan.mode == MODE_SANDBOX:
                # The deadline *is* this mode's terminal state: a sandbox session that had
                # to be stopped by the deadline did not end on its own terms.
                exit_code = EXIT_TIMEOUT
        else:
            stop_condition = STOP_RUN_RETURNED
    except KeyboardInterrupt:
        # Not a failure of the venue or of the plan: the operator ended the run, and the
        # report still has to exist for whatever the session left behind.
        stop_condition = STOP_KEYBOARD_INTERRUPT
        failure = "the run was interrupted by the operator (KeyboardInterrupt)"
        exit_code = EXIT_FAILURE
        log("ondo_probe: interrupted; stopping and publishing what was observed")
    except OndoProbeRefused as exc:
        stop_condition = STOP_EXCEPTION
        failure = f"refused late, before any request was sent: {exc}"
        exit_code = EXIT_REFUSED
        print(f"ondo_probe: refused: {exc}", file=sys.stderr)
    except Exception as exc:
        stop_condition = STOP_EXCEPTION
        failure = f"{type(exc).__name__}: {exc}"
        exit_code = EXIT_FAILURE
        log(f"ondo_probe: the session failed: {failure}")
        log(traceback.format_exc())
    finally:
        done.set()
        outcome = bounded_stop(stop_target[0] if stop_target else None, label="final",
                               via=stop_via[0])
        stops.append(outcome)
        try:
            cache = _attr(node, "cache")
            observation = observe_cache(cache, venue=ONDO_VENUE, synthetic=plan.synthetic)
            merge_observation(ledger, observation)
        except Exception as exc:  # pragma: no cover - observe_cache is already defensive
            observation["errors"] = [f"the post-run observation failed: {type(exc).__name__}: {exc}"]
        finished = now()
        document = report_document(
            plan, run_id=run_id, started=started, finished=finished, exit_code=exit_code,
            stop_condition=stop_condition, failure=failure, node=session, ledger=ledger,
            observation=observation, stops=stops, watchdog=watchdog_state,
        )
        meta = {
            "tool": "ondo_probe",
            "schema_version": SCHEMA_VERSION,
            "venue": ONDO_VENUE,
            "mode": plan.mode,
            "environment": plan.environment,
            "started_at_utc": started.isoformat(timespec="milliseconds"),
            "finished_at_utc": finished.isoformat(timespec="milliseconds"),
            "exit_code": exit_code,
            "stop_condition": stop_condition,
            "failure": failure,
            "outstanding_orders": len(document.get("outstanding_orders") or []),
            "pending_ids": len(document.get("pending_ids") or {}),
            "account_reconciled": document.get("account_reconciled"),
            "protocol_verified": False,
            "converging_stop_available": False,
        }
        try:
            manifest = write_report(
                {PROBE_FILE: document}, out_dir, run_id=run_id,
                complete=exit_code == EXIT_OK and failure is None, meta=meta, failure=failure,
            )
            log(f"ondo_probe: published {out_dir / (PROBE_FILE + '.json')} "
                f"(run {manifest.get('run_id')}, complete={manifest.get('complete')})")
        except Exception as exc:
            # A report that cannot be written is a failure of the run, never a silent one.
            print(f"ondo_probe: the report could not be written: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            if exit_code == EXIT_OK:
                exit_code = EXIT_FAILURE
        if exit_code == EXIT_OK and failure is None:
            # Said out loud, because a zero exit code is read as "clean" by everything
            # downstream and this one is not: the converging stop is unreachable.
            log(f"ondo_probe: exit 0 does not mean the account is clean - {CONVERGING_STOP_REASON}")
    return exit_code


# ------------------------------------------------------------------------ main


def main(argv: Sequence[str] | None = None, *, adapter=None, node_factory=None,
         environ=None, now=None) -> int:
    """Resolve, refuse, or run. The exit code is the answer; the report is the evidence.

    Every refusal happens here, before a session is built: a plan that cannot be resolved
    prints its reason and returns 2 having constructed nothing, read no credential and
    written no file.
    """
    args = parse_args(argv)
    clock = now if now is not None else _utc_now
    try:
        plan = resolve_plan(args, adapter)
    except OndoProbeRefused as exc:
        print(f"ondo_probe: refused: {exc}", file=sys.stderr)
        print("ondo_probe: nothing was constructed, nothing was sent, nothing was written",
              file=sys.stderr)
        return EXIT_REFUSED
    except OndoProbeError as exc:
        print(f"ondo_probe: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    if plan.dry_run:
        json.dump(plan_document(plan), sys.stdout, indent=2, sort_keys=True,
                  ensure_ascii=False, default=_json_default)
        sys.stdout.write("\n")
        return EXIT_OK

    try:
        # Credentials are resolved only for a mode that reads one, and only after the
        # dry-run exit above: --dry-run reads no environment and no .env file.
        resolved = load_environment(environ if environ is not None else None) \
            if plan.credentials_required else {}
        check_credentials(plan.mode, resolved)
        active_adapter = adapter if adapter is not None else load_adapter()
    except OndoProbeRefused as exc:
        print(f"ondo_probe: refused: {exc}", file=sys.stderr)
        print("ondo_probe: nothing was constructed, nothing was sent, nothing was written",
              file=sys.stderr)
        return EXIT_REFUSED
    except OndoProbeError as exc:
        print(f"ondo_probe: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    def log(message: str) -> None:
        print(message)

    try:
        return execute(plan, adapter=active_adapter, node_factory=node_factory,
                       environ=resolved, now=clock, out_dir=plan.out_dir, log=log)
    except KeyboardInterrupt:
        # execute() publishes from its own finally block; this catches only an interrupt
        # that arrived outside it, and it must still be an exit code rather than a trace.
        print("ondo_probe: interrupted before the session could publish", file=sys.stderr)
        return EXIT_FAILURE
    except OndoProbeError as exc:
        print(f"ondo_probe: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())

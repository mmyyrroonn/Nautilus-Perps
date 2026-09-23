#!/usr/bin/env python3
"""Ondo Perps restricted production-trade probe (app orchestration only).

    src/ondo_trade_probe.py --mode production-trade --side buy --dry-run
    src/ondo_trade_probe.py --mode production-trade --plan <approved-plan.json> \
        --confirm-production-trade --acknowledge-mainnet-authorization

What this file is
-----------------

A focused CLI and one Nautilus ``Strategy`` that *orchestrates* a single bounded production
trade through the native Ondo adapter.  It does not sign anything, does not open a second
HTTP client, does not encode an order body and does not enforce risk on its own: every one
of those is the native adapter's job, and a second Python implementation of any of them
would be the single worst thing this file could grow.  The strategy only decides *when* to
call ``order_factory`` / ``submit_order`` / ``cancel_order``; the native pre-send envelope,
the reconciliation machine and the ordered shutdown remain the authorities.

What this file refuses
----------------------

1. **It never writes by default.**  A live run needs a complete approved plan, the
   ``--confirm-production-trade`` acknowledgement, the
   ``--acknowledge-mainnet-authorization`` acknowledgement, a native adapter that exposes
    the write-envelope contract *and* every documented start gate satisfied. Any one missing is a
   refusal (exit 2) *before a credential is read or a client is built*.
2. **It cannot enable the installed R52 wheel.**  The installed wheel has no production
   write envelope, no production read-only scope and no trade snapshot accessor.  The
   capability probe reads the installed classes offline and fails closed: a live run is
   refused before ``load_environment``.  A dry run still prints its plan and reports
   ``native_write_capable: false`` honestly.
3. **It never reports a guess as a fact.**  Zero fill is ``no_trade``; a residual position
   is ``uncertain``, never clean; a fee that was not observed stays ``unknown``; a plan
   whose hash does not bind is refused.  ``production_execution_verified`` is true only when
   a confirmed entry fill, a confirmed reduce-only close and a flat reconciled account are
   all observed - it is false for every offline test and every dry run.
4. **It never widens a frozen bound.**  The approved plan freezes the instrument ticks, the
   quantity, the worst acceptable limit price, the maximum slippage, the notional and the
   counts.  Fresh metadata and a fresh quote can only make the order *smaller or better*;
   a fresh value outside the frozen bound is a refusal, not a resize.

The proposed native interface this file needs is recorded in
``reports/ondo-acceptance/20260919-production-native/trade-app/required-native-interface.md``.
Until that contract lands, a live run is impossible by design and the dry run says so.
"""

from __future__ import annotations

import argparse
import functools
import hashlib
import inspect
import json
import os
import shutil
import sys
import threading
import time
import traceback
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from ondo_dms_diagnostics import read_dms_release_diagnostics  # noqa: E402
from ondo_native_diagnostics import (  # noqa: E402  (needs sys.path above)
    IDENTITY_MATCHED,
    read_native_diagnostics,
)
from ondo_preflight import RUNS_DIRNAME, STAGING_PREFIX, new_run_id  # noqa: E402
from ondo_probe import (  # noqa: E402
    EXIT_FAILURE,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_TIMEOUT,
    MODE_PRODUCTION_READONLY,
    ONDO_VENUE,
    PRODUCTION_BASE_URL_HTTP,
    PRODUCTION_BASE_URL_WS,
    PRODUCTION_READONLY_CONNECTION_TIMEOUT_SECS,
    OndoAdapter,
    OndoProbeError,
    OndoProbeRefused,
    bounded_stop,
    load_adapter,
    production_endpoint_refusal,
    resolve_account_identity,
    resolve_stop_target,
    start_stop_watchdog,
)
from ondo_trade_limits import (  # noqa: E402
    HARD_MAX_APP_REQUESTS,
    HARD_MAX_CLOSE_ATTEMPTS,
    HARD_MAX_GROSS_EXPOSURE_USD,
    HARD_MAX_NEW_RISK_REQUESTS,
    HARD_MAX_NOTIONAL_PER_ORDER_USD,
    HARD_MAX_ORDERS,
    HARD_MAX_RUN_SECS,
    HARD_MAX_SLIPPAGE_BPS,
    TARGET_NOTIONAL_MAX_USD,
    TARGET_NOTIONAL_MIN_USD,
    TIME_IN_FORCE,
    TradeLimits,
    TradeLimitsError,
    load_trade_limits,
)

from nautilus_trader.common import Environment, LogLevel, LoggerConfig  # noqa: E402
from nautilus_trader.config import LiveRiskEngineConfig  # noqa: E402
from nautilus_trader.live import LiveNode  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountId,
    ClientOrderId,
    InstrumentId,
    OrderRejected,
    OrderSide,
    TraderId,
)
from nautilus_trader.model import TimeInForce  # noqa: E402
from nautilus_trader.trading import Strategy, StrategyConfig  # noqa: E402

# ------------------------------------------------------------------------ identity

TOOL = "ondo_trade_probe"
SCHEMA_VERSION = 1
MODE_PRODUCTION_TRADE = "production-trade"
MODES = (MODE_PRODUCTION_TRADE,)
PROBE_INSTANCE = "ONDO-TRADE-001"
TRADER_ID = "ONDO-TRADE-001"
TRADE_FILE = "trade"
META_FILE = "meta"
PAYLOAD_FILES = (TRADE_FILE,)
DEFAULT_OUT = "reports/ondo-trade"
DEFAULT_MINUTES = 10.0
PRODUCTION_TRADE_DISCONNECTION_TIMEOUT_SECS = 30

# The environment names this tool resolves.  There is no fallback between them.
MAINNET_API_KEY_VARIABLE = "ONDO_MAINNET_API_KEY"
MAINNET_API_SECRET_VARIABLE = "ONDO_MAINNET_API_SECRET"
MAINNET_ACCOUNT_ID_VARIABLE = "ONDO_MAINNET_ACCOUNT_ID"
MAINNET_CREDENTIAL_VARIABLES = (
    MAINNET_API_KEY_VARIABLE,
    MAINNET_API_SECRET_VARIABLE,
    MAINNET_ACCOUNT_ID_VARIABLE,
)
ENV_FILE_VARIABLE = "ONDO_PROBE_ENV_FILE"

# ------------------------------------------------------------- native contract names
#
# These names are the *app's required native write contract*, recorded for the next native
# worker in required-native-interface.md.  The capability probe below fails closed when any
# of them is absent, so the installed R52 wheel (which has none of them) can never be
# written through by this layer.

NATIVE_ENVELOPE_CLASS = "OndoExecutionEnvelopeConfig"
NATIVE_ENVELOPE_FIELD = "execution_envelope"
NATIVE_IDENTITY_FIELD = "expected_venue_account_id"
NATIVE_RUN_TOKEN_FIELD = "diagnostics_run_id"
NATIVE_DMS_TIMEOUT_FIELD = "dms_timeout_secs"
NATIVE_RECONCILE_INTERVAL_FIELD = "reconcile_interval_secs"
NATIVE_PRODUCTION_OPT_IN_FIELD = "allow_production_orders"
NATIVE_TRADE_MARKER = "supports_production_trade_envelope"
NATIVE_TRADE_SNAPSHOT = "production_trade_snapshot"
# The config fields the live write path must accept.  The run token is the point of
# P0-3: without a field to carry the app's run id, the native snapshot can never prove it
# belongs to this run, so the capability probe refuses rather than ship an unwireable
# consumer.
NATIVE_REQUIRED_CONFIG_FIELDS = (
    NATIVE_ENVELOPE_FIELD,
    NATIVE_IDENTITY_FIELD,
    NATIVE_RUN_TOKEN_FIELD,
    NATIVE_DMS_TIMEOUT_FIELD,
    NATIVE_RECONCILE_INTERVAL_FIELD,
    NATIVE_PRODUCTION_OPT_IN_FIELD,
)
ENVELOPE_REQUIRED_FIELDS = (
    "instrument_id",
    "entry_side",
    "entry_max_quantity",
    "entry_worst_price",
    "entry_max_notional_usd",
    "close_side",
    "close_max_quantity",
    "close_worst_price",
    "max_close_attempts",
    "max_notional_per_order_usd",
    "max_gross_exposure_usd",
    "max_orders",
    "max_new_risk_requests",
    "max_app_requests",
    "min_available_margin_usdc",
    "entry_deadline_unix_nanos",
    "cleanup_deadline_unix_nanos",
    "require_flat_start",
)

CAPABILITY_SOURCE_NATIVE = "native-trade-contract"
CAPABILITY_SOURCE_ADAPTER_ABSENT = "adapter-unavailable"
CAPABILITY_SOURCE_ENVELOPE_ABSENT = "envelope-class-absent"
CAPABILITY_SOURCE_ENVELOPE_SHAPE = "envelope-constructor-mismatch"
CAPABILITY_SOURCE_CONFIG_FIELD = "config-envelope-field-absent"
CAPABILITY_SOURCE_IDENTITY_FIELD = "config-identity-field-absent"
CAPABILITY_SOURCE_RUN_TOKEN_FIELD = "config-run-token-field-absent"
CAPABILITY_SOURCE_DMS_FIELD = "config-dms-timeout-field-absent"
CAPABILITY_SOURCE_RECONCILE_INTERVAL_FIELD = "config-reconcile-interval-field-absent"
CAPABILITY_SOURCE_PRODUCTION_OPT_IN_FIELD = "config-production-opt-in-field-absent"
CAPABILITY_SOURCE_MARKER_ABSENT = "trade-marker-absent"
CAPABILITY_SOURCE_MARKER_FALSE = "trade-marker-false"
CAPABILITY_SOURCE_MARKER_TYPE = "trade-marker-wrong-type"
CAPABILITY_SOURCE_SNAPSHOT_ABSENT = "trade-snapshot-absent"
CAPABILITY_SOURCE_LOOKUP_FAILED = "lookup-failed"

_MISSING = object()

# ------------------------------------------------------------------- order outcomes

PHASE_NOT_STARTED = "not_started"
PHASE_AWAITING_QUOTE = "awaiting_quote"
PHASE_ENTERING = "entering"
PHASE_CLOSING = "closing"
PHASE_DONE = "done"

OUTCOME_NOT_STARTED = "not_started"
OUTCOME_NO_TRADE = "no_trade"
OUTCOME_PARTIAL = "partial"
OUTCOME_FILLED = "filled"
OUTCOME_UNCERTAIN = "uncertain"
OUTCOME_REJECTED = "rejected"
OUTCOME_BLOCKED = "blocked"

ROLE_ENTRY = "entry"
ROLE_CLOSE = "close"

SIDE_BUY = "buy"
SIDE_SELL = "sell"

REJECTED_STATUSES = frozenset({"REJECTED", "DENIED"})


class TradePlanError(ValueError):
    """The approved plan is missing a field, malformed, or inconsistent with the limits."""


class TradeProbeRefused(OndoProbeRefused):
    """A startup rejection: exit 2, no credential read, no client constructed."""


# ------------------------------------------------------------------- JSON helpers


def _json_default(value: object) -> object:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat(timespec="milliseconds")
    if isinstance(value, Path):
        return value.as_posix()
    raise TypeError(f"{type(value).__name__} is not a report value")


def canonical_json(document: object) -> str:
    """Canonical JSON for hashing: sorted keys, no insignificant whitespace."""
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                      default=_json_default)


def plan_sha256(document: Mapping[str, object]) -> str:
    """Hash the immutable execution intent, excluding the self-referential fields.

    The ``authorization`` block binds the hash and the top-level ``plan_sha256`` carries it,
    so neither can be part of its own input.  Everything else - instrument, entry, close,
    envelope, account, dms, cleanup, reporting - is inside the hash, so tampering with any
    bound changes the recomputed value and the plan is refused.
    """
    body = {key: value for key, value in document.items()
            if key not in ("plan_sha256", "authorization")}
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ plan model


@dataclass(frozen=True)
class InstrumentSpec:
    symbol: str
    instrument_id: str
    price_increment: Decimal
    size_increment: Decimal
    min_quantity: Decimal | None
    min_quantity_source: str
    min_notional: Decimal | None
    min_notional_currency: str | None
    min_notional_source: str
    max_quantity: Decimal | None
    quote_currency: str = "USD"

    def as_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "instrument_id": self.instrument_id,
            "price_increment": str(self.price_increment),
            "size_increment": str(self.size_increment),
            "min_quantity": None if self.min_quantity is None else str(self.min_quantity),
            "min_quantity_source": self.min_quantity_source,
            "min_notional": None if self.min_notional is None else str(self.min_notional),
            "min_notional_currency": self.min_notional_currency,
            "min_notional_source": self.min_notional_source,
            "max_quantity": None if self.max_quantity is None else str(self.max_quantity),
            "quote_currency": self.quote_currency,
        }


@dataclass(frozen=True)
class EntrySpec:
    side: str
    quantity: Decimal
    limit_price: Decimal
    max_slippage_bps: Decimal
    notional_usd: Decimal
    max_quote_age_secs: Decimal
    order_type: str = "limit"
    time_in_force: str = TIME_IN_FORCE
    reduce_only: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "side": self.side,
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "reduce_only": self.reduce_only,
            "quantity": str(self.quantity),
            "limit_price": str(self.limit_price),
            "max_slippage_bps": str(self.max_slippage_bps),
            "notional_usd": str(self.notional_usd),
            "max_quote_age_secs": str(self.max_quote_age_secs),
        }


@dataclass(frozen=True)
class CloseSpec:
    side: str
    limit_price: Decimal
    max_close_attempts: int
    order_type: str = "limit"
    time_in_force: str = TIME_IN_FORCE
    reduce_only: bool = True
    max_quote_age_secs: Decimal = Decimal("5")

    def as_dict(self) -> dict[str, object]:
        return {
            "side": self.side,
            "limit_price": str(self.limit_price),
            "order_type": self.order_type,
            "time_in_force": self.time_in_force,
            "reduce_only": self.reduce_only,
            "max_close_attempts": self.max_close_attempts,
            "max_quote_age_secs": str(self.max_quote_age_secs),
        }


@dataclass(frozen=True)
class EnvelopeSpec:
    max_notional_per_order_usd: Decimal
    max_gross_exposure_usd: Decimal
    max_orders: int
    max_new_risk_requests: int
    max_app_requests: int
    min_available_margin_usdc: Decimal
    deadline_secs: int
    require_flat_start: bool = True

    def as_dict(self) -> dict[str, object]:
        return {
            "max_notional_per_order_usd": str(self.max_notional_per_order_usd),
            "max_gross_exposure_usd": str(self.max_gross_exposure_usd),
            "max_orders": self.max_orders,
            "max_new_risk_requests": self.max_new_risk_requests,
            "max_app_requests": self.max_app_requests,
            "min_available_margin_usdc": str(self.min_available_margin_usdc),
            "deadline_secs": self.deadline_secs,
            "require_flat_start": self.require_flat_start,
        }


@dataclass(frozen=True)
class TradePlan:
    plan_version: int
    instrument: InstrumentSpec
    entry: EntrySpec
    close: CloseSpec
    envelope: EnvelopeSpec
    journal_path: str
    expected_venue_account_id_present: bool
    dms: Mapping[str, object]
    cleanup: Mapping[str, object]
    plan_sha256: str
    document: Mapping[str, object]

    @property
    def deadline_secs(self) -> int:
        return self.envelope.deadline_secs


def _require_mapping(block: object, name: str) -> Mapping[str, object]:
    if not isinstance(block, Mapping):
        raise TradePlanError(f"the plan's {name!r} block is missing or not an object")
    return block


def _require_str(block: Mapping[str, object], key: str, where: str) -> str:
    value = block.get(key)
    if not isinstance(value, str) or not value.strip():
        raise TradePlanError(f"{where}.{key} must be a non-empty string")
    return value.strip()


def _require_decimal(block: Mapping[str, object], key: str, where: str) -> Decimal:
    value = block.get(key)
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise TradePlanError(
            f"{where}.{key} must be an exact decimal string (quoted), got {value!r}",
        )
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise TradePlanError(f"{where}.{key} is not a decimal: {value!r} ({exc})") from exc
    if not number.is_finite():
        raise TradePlanError(f"{where}.{key} must be finite, got {number}")
    return number


def _optional_decimal(block: Mapping[str, object], key: str, where: str) -> Decimal | None:
    if block.get(key) is None:
        return None
    return _require_decimal(block, key, where)


def _require_int(block: Mapping[str, object], key: str, where: str) -> int:
    value = block.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TradePlanError(f"{where}.{key} must be an integer, got {value!r}")
    return value


def _require_bool(block: Mapping[str, object], key: str, where: str) -> bool:
    value = block.get(key)
    if not isinstance(value, bool):
        raise TradePlanError(f"{where}.{key} must be true or false, got {value!r}")
    return value


def parse_trade_plan(document: object, limits: TradeLimits) -> TradePlan:
    """Parse and validate one approved plan document, or refuse it by name.

    Structural problems (a missing block, a float where an exact decimal is required, a
    self-inconsistent bound) raise :class:`TradePlanError`.  Every bound is cross-checked
    against the fully-resolved user limits, so a plan can only be tighter than the file's
    ``[ondo_trade]`` table.
    """
    if not isinstance(document, Mapping):
        raise TradePlanError("the plan document is not a JSON object")
    version = document.get("plan_version")
    if version != 1:
        raise TradePlanError(f"plan_version must be 1, got {version!r}")

    instrument_block = _require_mapping(document.get("instrument"), "instrument")
    entry_block = _require_mapping(document.get("entry"), "entry")
    close_block = _require_mapping(document.get("close"), "close")
    envelope_block = _require_mapping(document.get("envelope"), "envelope")
    account_block = _require_mapping(document.get("account"), "account")
    dms_block = _require_mapping(document.get("dms"), "dms")
    cleanup_block = _require_mapping(document.get("cleanup"), "cleanup")

    instrument = InstrumentSpec(
        symbol=_require_str(instrument_block, "symbol", "instrument").upper(),
        instrument_id=_require_str(instrument_block, "instrument_id", "instrument"),
        price_increment=_require_decimal(instrument_block, "price_increment", "instrument"),
        size_increment=_require_decimal(instrument_block, "size_increment", "instrument"),
        min_quantity=_optional_decimal(instrument_block, "min_quantity", "instrument"),
        min_quantity_source=_require_str(
            instrument_block, "min_quantity_source", "instrument",
        ),
        min_notional=_optional_decimal(instrument_block, "min_notional", "instrument"),
        min_notional_currency=(
            _require_str(instrument_block, "min_notional_currency", "instrument")
            if instrument_block.get("min_notional_currency") is not None else None
        ),
        min_notional_source=_require_str(
            instrument_block, "min_notional_source", "instrument",
        ),
        max_quantity=(
            _require_decimal(instrument_block, "max_quantity", "instrument")
            if instrument_block.get("max_quantity") is not None else None
        ),
        quote_currency=_require_str(instrument_block, "quote_currency", "instrument"),
    )
    side = _require_str(entry_block, "side", "entry").lower()
    entry = EntrySpec(
        side=side,
        quantity=_require_decimal(entry_block, "quantity", "entry"),
        limit_price=_require_decimal(entry_block, "limit_price", "entry"),
        max_slippage_bps=_require_decimal(entry_block, "max_slippage_bps", "entry"),
        notional_usd=_require_decimal(entry_block, "notional_usd", "entry"),
        max_quote_age_secs=_require_decimal(entry_block, "max_quote_age_secs", "entry"),
        order_type=_require_str(entry_block, "order_type", "entry").lower(),
        time_in_force=_require_str(entry_block, "time_in_force", "entry").upper(),
        reduce_only=_require_bool(entry_block, "reduce_only", "entry"),
    )
    close = CloseSpec(
        side=_require_str(close_block, "side", "close").lower(),
        limit_price=_require_decimal(close_block, "limit_price", "close"),
        max_close_attempts=_require_int(close_block, "max_close_attempts", "close"),
        order_type=_require_str(close_block, "order_type", "close").lower(),
        time_in_force=_require_str(close_block, "time_in_force", "close").upper(),
        reduce_only=_require_bool(close_block, "reduce_only", "close"),
        max_quote_age_secs=_require_decimal(close_block, "max_quote_age_secs", "close"),
    )
    envelope = EnvelopeSpec(
        max_notional_per_order_usd=_require_decimal(
            envelope_block, "max_notional_per_order_usd", "envelope",
        ),
        max_gross_exposure_usd=_require_decimal(
            envelope_block, "max_gross_exposure_usd", "envelope",
        ),
        max_orders=_require_int(envelope_block, "max_orders", "envelope"),
        max_new_risk_requests=_require_int(
            envelope_block, "max_new_risk_requests", "envelope",
        ),
        max_app_requests=_require_int(envelope_block, "max_app_requests", "envelope"),
        min_available_margin_usdc=_require_decimal(
            envelope_block, "min_available_margin_usdc", "envelope",
        ),
        deadline_secs=_require_int(envelope_block, "deadline_secs", "envelope"),
        require_flat_start=_require_bool(envelope_block, "require_flat_start", "envelope"),
    )

    journal_path = _require_str(account_block, "journal_path", "account")
    environment = _require_str(account_block, "environment", "account").lower()
    identity_present = _require_bool(
        account_block, "expected_venue_account_id_present", "account",
    )

    computed = plan_sha256(document)
    embedded = document.get("plan_sha256")
    if embedded is not None and embedded != computed:
        raise TradePlanError(
            f"plan_sha256 does not bind the document: embedded {embedded!r}, recomputed "
            f"{computed!r} - the plan was modified after it was hashed",
        )
    authorization = _require_mapping(document.get("authorization"), "authorization")
    approved = authorization.get("approved_plan_sha256")
    if approved is not None and approved != computed:
        raise TradePlanError(
            f"authorization.approved_plan_sha256 does not match the plan: approved "
            f"{approved!r}, recomputed {computed!r}",
        )

    plan = TradePlan(
        plan_version=1,
        instrument=instrument,
        entry=entry,
        close=close,
        envelope=envelope,
        journal_path=journal_path,
        expected_venue_account_id_present=identity_present,
        dms=dict(dms_block),
        cleanup=dict(cleanup_block),
        plan_sha256=computed,
        document=dict(document),
    )
    problems = validate_trade_plan(plan, limits, environment=environment)
    if problems:
        raise TradePlanError("; ".join(problems))
    return plan


def _is_tick(value: Decimal, increment: Decimal) -> bool:
    if increment <= 0:
        return False
    return (value % increment) == 0


def validate_trade_plan(plan: TradePlan, limits: TradeLimits, *,
                        environment: str = "production") -> list[str]:
    """Every frozen-bound and cross-bound check, returned as a list of problems."""
    problems: list[str] = []
    entry, close, envelope, instrument = plan.entry, plan.close, plan.envelope, plan.instrument

    if environment != "production":
        problems.append(f"account.environment must be 'production', got {environment!r}")
    if not plan.expected_venue_account_id_present:
        problems.append(
            "account.expected_venue_account_id_present must be true: without it the native "
            "identity check cannot match and a production write is refused",
        )
    if not plan.journal_path:
        problems.append("account.journal_path must name a durable journal")

    # --- instrument / entry ---------------------------------------------------------
    if instrument.instrument_id != limits.instrument:
        problems.append(
            f"instrument.instrument_id {instrument.instrument_id!r} is not the configured "
            f"{limits.instrument!r}: this probe trades one instrument only",
        )
    if instrument.symbol != limits.symbol:
        problems.append(
            f"instrument.symbol {instrument.symbol!r} is not the configured "
            f"{limits.symbol!r}",
        )
    if instrument.price_increment <= 0 or instrument.size_increment <= 0:
        problems.append("instrument increments must be positive")
    if instrument.min_quantity is None:
        if instrument.min_quantity_source != "unpublished":
            problems.append(
                "instrument.min_quantity_source must be 'unpublished' when no standalone "
                "minimum quantity was published",
            )
    elif instrument.min_quantity <= 0:
        problems.append("instrument.min_quantity must be positive when present")
    elif instrument.min_quantity_source == "unpublished":
        problems.append("a present instrument.min_quantity cannot have unpublished provenance")
    if instrument.min_notional is None:
        if instrument.min_notional_source != "unpublished":
            problems.append(
                "instrument.min_notional_source must be 'unpublished' when no standalone "
                "minimum notional was published",
            )
        if instrument.min_notional_currency is not None:
            problems.append(
                "instrument.min_notional_currency must be null when min_notional is unpublished",
            )
    else:
        if instrument.min_notional <= 0:
            problems.append("instrument.min_notional must be positive when present")
        if instrument.min_notional_source == "unpublished":
            problems.append("a present instrument.min_notional cannot have unpublished provenance")
        if not instrument.min_notional_currency:
            problems.append("instrument.min_notional_currency is required with min_notional")
        elif instrument.min_notional_currency != instrument.quote_currency:
            problems.append(
                f"instrument.min_notional_currency {instrument.min_notional_currency!r} "
                f"must equal quote_currency {instrument.quote_currency!r}",
            )
    if entry.side not in (SIDE_BUY, SIDE_SELL):
        problems.append(f"entry.side must be buy or sell, got {entry.side!r}")
    if entry.quantity <= 0:
        problems.append("entry.quantity must be greater than zero")
    if entry.limit_price <= 0:
        problems.append("entry.limit_price must be greater than zero")
    if entry.max_slippage_bps < 0:
        problems.append("entry.max_slippage_bps must not be negative")
    if entry.max_slippage_bps > HARD_MAX_SLIPPAGE_BPS:
        problems.append(
            f"entry.max_slippage_bps {entry.max_slippage_bps} exceeds the hard cap "
            f"{HARD_MAX_SLIPPAGE_BPS}",
        )
    if entry.max_slippage_bps > limits.max_slippage_bps:
        problems.append(
            f"entry.max_slippage_bps {entry.max_slippage_bps} exceeds the configured "
            f"{limits.max_slippage_bps}",
        )
    if entry.max_quote_age_secs <= 0:
        problems.append("entry.max_quote_age_secs must be greater than zero")
    if entry.order_type != "limit":
        problems.append(f"entry.order_type must be 'limit', got {entry.order_type!r}")
    if entry.time_in_force != TIME_IN_FORCE:
        problems.append(
            f"entry.time_in_force must be {TIME_IN_FORCE}, got {entry.time_in_force!r}",
        )
    if entry.reduce_only:
        problems.append("entry.reduce_only must be false: the entry opens the position")

    if not _is_tick(entry.quantity, instrument.size_increment):
        problems.append(
            f"entry.quantity {entry.quantity} is not a multiple of size_increment "
            f"{instrument.size_increment}",
        )
    if not _is_tick(entry.limit_price, instrument.price_increment):
        problems.append(
            f"entry.limit_price {entry.limit_price} is not a multiple of price_increment "
            f"{instrument.price_increment}",
        )
    if instrument.min_quantity is not None and entry.quantity < instrument.min_quantity:
        problems.append(
            f"entry.quantity {entry.quantity} is below min_quantity {instrument.min_quantity}",
        )
    if instrument.max_quantity is not None and entry.quantity > instrument.max_quantity:
        problems.append(
            f"entry.quantity {entry.quantity} exceeds max_quantity {instrument.max_quantity}",
        )

    notional = entry.quantity * entry.limit_price
    if instrument.min_notional is not None and notional < instrument.min_notional:
        problems.append(
            f"entry.quantity * entry.limit_price = {notional} is below min_notional "
            f"{instrument.min_notional}",
        )
    if notional > entry.notional_usd:
        problems.append(
            f"entry quantity*price {notional} exceeds the frozen notional_usd "
            f"{entry.notional_usd}: the plan must not promise a bound it cannot honour",
        )
    if entry.notional_usd < TARGET_NOTIONAL_MIN_USD:
        problems.append(
            f"entry.notional_usd {entry.notional_usd} is below the {TARGET_NOTIONAL_MIN_USD} "
            f"target floor",
        )
    if entry.notional_usd > TARGET_NOTIONAL_MAX_USD:
        problems.append(
            f"entry.notional_usd {entry.notional_usd} is above the {TARGET_NOTIONAL_MAX_USD} "
            f"target ceiling",
        )
    if entry.notional_usd > limits.entry_notional_usd:
        problems.append(
            f"entry.notional_usd {entry.notional_usd} exceeds the configured "
            f"{limits.entry_notional_usd}",
        )

    # --- close ----------------------------------------------------------------------
    if close.side not in (SIDE_BUY, SIDE_SELL):
        problems.append(f"close.side must be buy or sell, got {close.side!r}")
    elif entry.side in (SIDE_BUY, SIDE_SELL) and close.side == entry.side:
        problems.append(
            f"close.side {close.side!r} must be the opposite of entry.side {entry.side!r}",
        )
    if not close.reduce_only:
        problems.append("close.reduce_only must be true: the close may only reduce")
    if close.order_type != "limit":
        problems.append(f"close.order_type must be 'limit', got {close.order_type!r}")
    if close.time_in_force != TIME_IN_FORCE:
        problems.append(
            f"close.time_in_force must be {TIME_IN_FORCE}, got {close.time_in_force!r}",
        )
    if close.max_close_attempts < 1:
        problems.append("close.max_close_attempts must be at least 1")
    if close.max_close_attempts > HARD_MAX_CLOSE_ATTEMPTS:
        problems.append(
            f"close.max_close_attempts {close.max_close_attempts} exceeds the hard cap "
            f"{HARD_MAX_CLOSE_ATTEMPTS}",
        )
    if close.max_close_attempts != limits.max_close_attempts:
        problems.append(
            f"close.max_close_attempts {close.max_close_attempts} does not match the "
            f"configured {limits.max_close_attempts}",
        )
    if close.max_quote_age_secs <= 0:
        problems.append("close.max_quote_age_secs must be greater than zero")
    if close.limit_price <= 0:
        problems.append("close.limit_price must be greater than zero")
    elif not _is_tick(close.limit_price, instrument.price_increment):
        problems.append(
            f"close.limit_price {close.limit_price} is not a multiple of price_increment "
            f"{instrument.price_increment}",
        )

    # --- envelope -------------------------------------------------------------------
    if envelope.max_notional_per_order_usd <= 0:
        problems.append("envelope.max_notional_per_order_usd must be positive")
    if envelope.max_notional_per_order_usd > HARD_MAX_NOTIONAL_PER_ORDER_USD:
        problems.append(
            f"envelope.max_notional_per_order_usd {envelope.max_notional_per_order_usd} "
            f"exceeds the {HARD_MAX_NOTIONAL_PER_ORDER_USD} hard cap",
        )
    if envelope.max_notional_per_order_usd > limits.max_notional_per_order_usd:
        problems.append(
            f"envelope.max_notional_per_order_usd {envelope.max_notional_per_order_usd} "
            f"exceeds the configured {limits.max_notional_per_order_usd}",
        )
    if envelope.max_notional_per_order_usd < entry.notional_usd:
        problems.append(
            "envelope.max_notional_per_order_usd must cover the entry notional_usd",
        )
    if envelope.max_gross_exposure_usd <= 0:
        problems.append("envelope.max_gross_exposure_usd must be positive")
    if envelope.max_gross_exposure_usd > HARD_MAX_GROSS_EXPOSURE_USD:
        problems.append(
            f"envelope.max_gross_exposure_usd {envelope.max_gross_exposure_usd} exceeds the "
            f"{HARD_MAX_GROSS_EXPOSURE_USD} hard cap",
        )
    if envelope.max_gross_exposure_usd > limits.max_gross_exposure_usd:
        problems.append(
            f"envelope.max_gross_exposure_usd {envelope.max_gross_exposure_usd} exceeds the "
            f"configured {limits.max_gross_exposure_usd}",
        )
    if envelope.max_gross_exposure_usd < entry.notional_usd:
        problems.append("envelope.max_gross_exposure_usd must cover the entry notional_usd")
    if envelope.max_orders > HARD_MAX_ORDERS:
        problems.append(
            f"envelope.max_orders {envelope.max_orders} exceeds the {HARD_MAX_ORDERS} hard cap",
        )
    if envelope.max_orders > limits.max_orders:
        problems.append(
            f"envelope.max_orders {envelope.max_orders} exceeds the configured "
            f"{limits.max_orders}",
        )
    if envelope.max_orders < 1 + close.max_close_attempts:
        problems.append(
            f"envelope.max_orders {envelope.max_orders} is below 1 entry + "
            f"{close.max_close_attempts} close attempts",
        )
    if envelope.max_new_risk_requests > HARD_MAX_NEW_RISK_REQUESTS:
        problems.append(
            f"envelope.max_new_risk_requests {envelope.max_new_risk_requests} exceeds the "
            f"{HARD_MAX_NEW_RISK_REQUESTS} hard cap",
        )
    if envelope.max_new_risk_requests != 1:
        problems.append(
            f"envelope.max_new_risk_requests must be exactly 1 (one opening attempt only; "
            f"reduce-only exits are not new-risk requests), got "
            f"{envelope.max_new_risk_requests}",
        )
    if envelope.max_app_requests > HARD_MAX_APP_REQUESTS:
        problems.append(
            f"envelope.max_app_requests {envelope.max_app_requests} exceeds the "
            f"{HARD_MAX_APP_REQUESTS} hard cap",
        )
    if envelope.max_app_requests > limits.max_app_requests:
        problems.append(
            f"envelope.max_app_requests {envelope.max_app_requests} exceeds the configured "
            f"{limits.max_app_requests}",
        )
    if envelope.max_app_requests < 1 + close.max_close_attempts:
        problems.append(
            f"envelope.max_app_requests {envelope.max_app_requests} is below 1 entry + "
            f"{close.max_close_attempts} close attempts",
        )
    if envelope.min_available_margin_usdc <= 0:
        problems.append("envelope.min_available_margin_usdc must be positive")
    if envelope.min_available_margin_usdc < limits.min_available_margin_usdc:
        problems.append(
            f"envelope.min_available_margin_usdc "
            f"{envelope.min_available_margin_usdc} is below the configured USDC threshold "
            f"{limits.min_available_margin_usdc}",
        )
    if envelope.deadline_secs <= 0:
        problems.append("envelope.deadline_secs must be positive")
    if envelope.deadline_secs > HARD_MAX_RUN_SECS:
        problems.append(
            f"envelope.deadline_secs {envelope.deadline_secs} exceeds the "
            f"{HARD_MAX_RUN_SECS} hard cap",
        )
    if envelope.deadline_secs > limits.deadline_secs:
        problems.append(
            f"envelope.deadline_secs {envelope.deadline_secs} exceeds the configured "
            f"{limits.deadline_secs}",
        )
    stop_budget = plan.cleanup.get("stop_budget_secs")
    if isinstance(stop_budget, int) and not isinstance(stop_budget, bool):
        if envelope.deadline_secs <= stop_budget:
            problems.append(
                "envelope.deadline_secs must exceed cleanup.stop_budget_secs so opening "
                "risk closes before the bounded cleanup deadline",
            )
    if not envelope.require_flat_start:
        problems.append("envelope.require_flat_start must be true for a bounded probe")

    # --- dms / cleanup ----------------------------------------------------------------
    dms = plan.dms
    if str(dms.get("mode", "")).lower() != "trading":
        problems.append("dms.mode must be 'trading': a write session arms the switch")
    for key in ("activation", "release_on_stop", "account_wide_effect"):
        if not isinstance(dms.get(key), str) or not str(dms.get(key)).strip():
            problems.append(f"dms.{key} must state the account-wide effect in words")
    for key in ("timeout_secs", "renewal_interval_secs"):
        value = dms.get(key)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            problems.append(f"dms.{key} must be a positive integer")
    timeout = dms.get("timeout_secs")
    renewal = dms.get("renewal_interval_secs")
    if isinstance(timeout, int) and not isinstance(timeout, bool) and timeout > 0:
        derived = max(1, timeout // 2)
        if renewal != derived:
            problems.append(
                f"dms.renewal_interval_secs {renewal!r} differs from the native-derived "
                f"{derived} (timeout_secs // 2): the native adapter exposes only the "
                f"timeout, so a differing renewal setting is unsupported and refused"
            )
    if not isinstance(dms.get("renewal_message_verified"), bool):
        problems.append("dms.renewal_message_verified must be true or false")
    cleanup = plan.cleanup
    if cleanup.get("cancel_own_orders_only") is not True:
        problems.append("cleanup.cancel_own_orders_only must be true")
    if cleanup.get("confirm_cancels") is not True:
        problems.append("cleanup.confirm_cancels must be true")
    stop_budget = cleanup.get("stop_budget_secs")
    if isinstance(stop_budget, bool) or not isinstance(stop_budget, int) or stop_budget <= 0:
        problems.append("cleanup.stop_budget_secs must be a positive integer")
    elif stop_budget > limits.cleanup_budget_secs:
        problems.append(
            f"cleanup.stop_budget_secs {stop_budget} exceeds the configured "
            f"{limits.cleanup_budget_secs}",
        )

    return problems


def authorization_problems(plan: TradePlan) -> list[str]:
    """The conversational-authorization gates the plan itself must carry."""
    problems: list[str] = []
    authorization = plan.document.get("authorization")
    if not isinstance(authorization, Mapping):
        problems.append("authorization block is missing: the plan is not approved")
        return problems
    if authorization.get("user_phrase_上主网_present") is not True:
        problems.append(
            "authorization.user_phrase_上主网_present must be true: the plan records that "
            "the user gave current-turn authorization, and this tool cannot verify a "
            "conversation for them",
        )
    if authorization.get("approved_plan_sha256") != plan.plan_sha256:
        problems.append(
            "authorization.approved_plan_sha256 must equal the plan's recomputed hash",
        )
    return problems


def sanitized_plan_document(plan: TradePlan) -> dict[str, object]:
    """The plan view that is safe to log: only known execution fields, never the raw file.

    A plan file carrying an extra key (for example an account id) must not be echoed to a
    log or report just because it parsed; this projection is the only plan view that leaves
    the process, and it carries a boolean "expected id present" rather than any value.
    """
    authorization = plan.document.get("authorization")
    authorization = authorization if isinstance(authorization, Mapping) else {}
    return {
        "plan_version": plan.plan_version,
        "plan_sha256": plan.plan_sha256,
        "instrument": plan.instrument.as_dict(),
        "entry": plan.entry.as_dict(),
        "close": plan.close.as_dict(),
        "envelope": plan.envelope.as_dict(),
        "account": {
            "environment": "production",
            "journal_path": plan.journal_path,
            "expected_venue_account_id_present": plan.expected_venue_account_id_present,
        },
        "dms": {
            key: plan.dms.get(key) for key in (
                "mode", "activation", "timeout_secs", "renewal_interval_secs",
                "renewal_message_verified", "release_on_stop", "account_wide_effect",
            )
        },
        "cleanup": {
            key: plan.cleanup.get(key) for key in (
                "cancel_own_orders_only", "confirm_cancels", "confirm_flat_position",
                "stop_budget_secs",
            )
        },
        "authorization": {
            "user_phrase_上主网_present": authorization.get("user_phrase_上主网_present"),
            "approved_plan_sha256": authorization.get("approved_plan_sha256"),
        },
    }


# ------------------------------------------------------- native write capability


@dataclass(frozen=True)
class NativeTradeCapability:
    """Whether the installed adapter exposes the production write-envelope contract."""

    supported: bool
    source: str
    reason: str
    missing: tuple[str, ...] = ()

    def as_dict(self) -> dict[str, object]:
        return {
            "supported": self.supported,
            "source": self.source,
            "reason": self.reason,
            "missing": list(self.missing),
        }


def _accepts_field(target: object, field_name: str) -> bool:
    """Whether ``target``'s constructor accepts ``field_name`` (fail-open on introspection)."""
    try:
        parameters = inspect.signature(target).parameters
    except (TypeError, ValueError):
        return True
    if field_name in parameters:
        return True
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in parameters.values())


def _unsupported(source: str, reason: str, *missing: str) -> NativeTradeCapability:
    return NativeTradeCapability(False, source, reason, tuple(missing))


def detect_native_trade_capability(adapter: OndoAdapter | None) -> NativeTradeCapability:
    """Read the installed adapter's production write contract, offline and fail-closed.

    This is introspection, not a session: at most it constructs the *factory* (a
    credential-free object), reads class attributes and constructor signatures, and opens no
    socket.  The installed R52 wheel fails every check, so a live run is refused before any
    credential is read.  Anything other than the exact boolean ``True`` for the marker fails
    closed.
    """
    if adapter is None:
        return _unsupported(
            CAPABILITY_SOURCE_ADAPTER_ABSENT,
            "no Ondo adapter was resolved, so the production write contract cannot be read; "
            "a live run is refused rather than assuming a wheel",
        )
    try:
        envelope_cls = getattr(adapter, NATIVE_ENVELOPE_CLASS, _MISSING)
        config_cls = getattr(adapter, "OndoExecutionClientConfig", _MISSING)
        factory_cls = getattr(adapter, "OndoExecutionClientFactory", _MISSING)
    except Exception as exc:
        return _unsupported(
            CAPABILITY_SOURCE_LOOKUP_FAILED,
            f"reading the Ondo classes raised {type(exc).__name__}: the write contract "
            f"fails closed",
        )
    if envelope_cls is _MISSING or not callable(envelope_cls):
        return _unsupported(
            CAPABILITY_SOURCE_ENVELOPE_ABSENT,
            f"the installed adapter exposes no {NATIVE_ENVELOPE_CLASS}: there is no native "
            f"pre-send write envelope, so this layer must not write",
            NATIVE_ENVELOPE_CLASS,
        )
    missing_fields = [name for name in ENVELOPE_REQUIRED_FIELDS
                      if not _accepts_field(envelope_cls, name)]
    if missing_fields:
        return _unsupported(
            CAPABILITY_SOURCE_ENVELOPE_SHAPE,
            f"{NATIVE_ENVELOPE_CLASS} does not accept the required fields "
            f"{', '.join(missing_fields)}: the write envelope contract is not the one this "
            f"layer requires",
            *missing_fields,
        )
    config_field_sources = {
        NATIVE_ENVELOPE_FIELD: CAPABILITY_SOURCE_CONFIG_FIELD,
        NATIVE_IDENTITY_FIELD: CAPABILITY_SOURCE_IDENTITY_FIELD,
        NATIVE_RUN_TOKEN_FIELD: CAPABILITY_SOURCE_RUN_TOKEN_FIELD,
        NATIVE_DMS_TIMEOUT_FIELD: CAPABILITY_SOURCE_DMS_FIELD,
        NATIVE_RECONCILE_INTERVAL_FIELD: CAPABILITY_SOURCE_RECONCILE_INTERVAL_FIELD,
        NATIVE_PRODUCTION_OPT_IN_FIELD: CAPABILITY_SOURCE_PRODUCTION_OPT_IN_FIELD,
    }
    if config_cls is _MISSING:
        return _unsupported(
            CAPABILITY_SOURCE_CONFIG_FIELD,
            "the adapter exposes no OndoExecutionClientConfig to carry the write contract",
        )
    for field_name in NATIVE_REQUIRED_CONFIG_FIELDS:
        if not _accepts_field(config_cls, field_name):
            return _unsupported(
                config_field_sources[field_name],
                f"OndoExecutionClientConfig does not accept {field_name!r}: the native write "
                f"contract is incomplete and a live run is refused",
                field_name,
            )
    try:
        marker = getattr(factory_cls, NATIVE_TRADE_MARKER, _MISSING)
    except Exception as exc:
        return _unsupported(
            CAPABILITY_SOURCE_LOOKUP_FAILED,
            f"reading {NATIVE_TRADE_MARKER} raised {type(exc).__name__}: capability fails "
            f"closed",
        )
    if marker is _MISSING:
        return _unsupported(
            CAPABILITY_SOURCE_MARKER_ABSENT,
            f"the installed OndoExecutionClientFactory exposes no {NATIVE_TRADE_MARKER}: "
            f"production writes are unsupported by this wheel",
            NATIVE_TRADE_MARKER,
        )
    if not isinstance(marker, bool):
        # A PyO3 getter is a descriptor at class level: read it from a credential-free
        # factory *instance* only.
        try:
            factory = factory_cls()
        except Exception as exc:
            return _unsupported(
                CAPABILITY_SOURCE_LOOKUP_FAILED,
                f"the OndoExecutionClientFactory could not be constructed to read "
                f"{NATIVE_TRADE_MARKER} ({type(exc).__name__}): capability fails closed",
            )
        try:
            marker = getattr(factory, NATIVE_TRADE_MARKER, _MISSING)
        except Exception as exc:
            return _unsupported(
                CAPABILITY_SOURCE_LOOKUP_FAILED,
                f"reading {NATIVE_TRADE_MARKER} raised {type(exc).__name__}: capability "
                f"fails closed",
            )
    if marker is not True:
        source = CAPABILITY_SOURCE_MARKER_FALSE if marker is False else CAPABILITY_SOURCE_MARKER_TYPE
        return _unsupported(
            source,
            f"the installed OndoExecutionClientFactory reports {NATIVE_TRADE_MARKER} as "
            f"{marker!r}; only the exact boolean True enables a live run",
        )
    try:
        snapshot = getattr(factory_cls, NATIVE_TRADE_SNAPSHOT, _MISSING)
    except Exception as exc:
        return _unsupported(
            CAPABILITY_SOURCE_LOOKUP_FAILED,
            f"reading {NATIVE_TRADE_SNAPSHOT} raised {type(exc).__name__}: capability "
            f"fails closed",
        )
    if snapshot is _MISSING:
        return _unsupported(
            CAPABILITY_SOURCE_SNAPSHOT_ABSENT,
            f"the installed factory exposes no {NATIVE_TRADE_SNAPSHOT}: the required "
            f"start gates (identity, flat account, coverage, foreign orders, margin, DMS) "
            f"cannot be read, so a live run is refused",
            NATIVE_TRADE_SNAPSHOT,
        )
    return NativeTradeCapability(
        True,
        CAPABILITY_SOURCE_NATIVE,
        "the installed adapter exposes the production write-envelope contract and the "
        "readiness snapshot; the native layer remains the enforcing authority",
    )


# --------------------------------------------------------------- start readiness


@dataclass(frozen=True)
class TradeReadiness:
    """The native start gates, every unobserved fact left ``None`` (unknown)."""

    available: bool
    source: str
    reason: str
    generation: int | None = None
    snapshot_unix_nanos: int | None = None
    identity_match: str | None = None
    account_flat: bool | None = None
    coverage_complete: bool | None = None
    foreign_open_orders: int | None = None
    own_open_orders: int | None = None
    native_ready: bool | None = None
    metadata_fresh: bool | None = None
    trading_enabled: bool | None = None
    underlying_market_closed: bool | None = None
    dms_verified: bool | None = None
    available_margin_usdc: Decimal | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "source": self.source,
            "reason": self.reason,
            "generation": self.generation,
            "snapshot_unix_nanos": self.snapshot_unix_nanos,
            "identity_match": self.identity_match,
            "account_flat": self.account_flat,
            "coverage_complete": self.coverage_complete,
            "foreign_open_orders": self.foreign_open_orders,
            "own_open_orders": self.own_open_orders,
            "native_ready": self.native_ready,
            "metadata_fresh": self.metadata_fresh,
            "trading_enabled": self.trading_enabled,
            "underlying_market_closed": self.underlying_market_closed,
            "dms_verified": self.dms_verified,
            "available_margin_usdc": (
                None if self.available_margin_usdc is None
                else str(self.available_margin_usdc)
            ),
        }


def _unready(reason: str, source: str = "unavailable") -> TradeReadiness:
    return TradeReadiness(False, source, reason)


def _as_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _as_count_or_none(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 0 else None


def _as_decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _read_snapshot_mapping(target: object | None, *, run_id: str,
                           phase: str) -> tuple[Mapping[str, object] | None, str | None]:
    """Read the per-run native snapshot mapping for ``phase`` (fail closed).

    Shared by the start gate and the final reconciliation so both apply the same run-token
    check.  Returns ``(mapping, None)`` or ``(None, reason)``; it never raises out.
    """
    if target is None:
        return None, "no native object was available to read the trade snapshot from"
    try:
        accessor = getattr(target, NATIVE_TRADE_SNAPSHOT, None)
    except Exception as exc:
        return None, f"reading {NATIVE_TRADE_SNAPSHOT} raised {type(exc).__name__}"
    if accessor is None:
        return None, f"the adapter exposes no {NATIVE_TRADE_SNAPSHOT} accessor"
    snapshot = accessor
    if callable(snapshot):
        try:
            snapshot = snapshot()
        except Exception as exc:
            return None, f"calling {NATIVE_TRADE_SNAPSHOT} raised {type(exc).__name__}"
    if not isinstance(snapshot, Mapping):
        return None, f"{NATIVE_TRADE_SNAPSHOT} returned {type(snapshot).__name__}, not a mapping"
    token = snapshot.get("run_id")
    if not isinstance(token, str) or not token.strip():
        return None, f"the {phase} snapshot carries no run_id and cannot be proven this run's"
    if token != run_id:
        return None, f"the {phase} snapshot belongs to a different run and is not merged"
    snapshot_phase = snapshot.get("phase")
    if snapshot_phase != phase:
        return None, (
            f"the snapshot is phase {snapshot_phase!r}, not the required {phase!r}: the "
            f"start and final facts are never conflated"
        )
    return snapshot, None


def read_trade_readiness(target: object | None, *, run_id: str) -> TradeReadiness:
    """Read this run's native *start* readiness snapshot, or report it unavailable.

    The accessor name and keys are the app's required write contract (see
    required-native-interface.md).  A missing accessor, a wrong type, a token mismatch, a
    wrong phase or a raising getter all fail closed; this function never raises out,
    because a readiness read must not itself be the thing that fails a session.
    """
    snapshot, reason = _read_snapshot_mapping(target, run_id=run_id, phase="start")
    if snapshot is None:
        return _unready(reason or "the start readiness snapshot is unavailable")
    return TradeReadiness(
        available=True,
        source="native-start-snapshot",
        reason="the start snapshot carries this run's token; an absent field stays unknown",
        generation=_as_count_or_none(snapshot.get("generation")),
        snapshot_unix_nanos=_as_count_or_none(snapshot.get("snapshot_unix_nanos")),
        identity_match=(
            snapshot.get("identity_match") if snapshot.get("identity_match") in
            (IDENTITY_MATCHED, "mismatch", "unknown") else None
        ),
        account_flat=_as_bool(snapshot.get("account_flat")),
        coverage_complete=_as_bool(snapshot.get("coverage_complete")),
        foreign_open_orders=_as_count_or_none(snapshot.get("foreign_open_orders")),
        own_open_orders=_as_count_or_none(snapshot.get("own_open_orders")),
        native_ready=_as_bool(snapshot.get("native_ready")),
        metadata_fresh=_as_bool(snapshot.get("metadata_fresh")),
        trading_enabled=_as_bool(snapshot.get("trading_enabled")),
        underlying_market_closed=_as_bool(snapshot.get("underlying_market_closed")),
        dms_verified=_as_bool(snapshot.get("dms_verified")),
        available_margin_usdc=_as_decimal(snapshot.get("available_margin_usdc")),
    )


@dataclass(frozen=True)
class FinalReconciliation:
    """The native *post-run* reconciliation, every unobserved fact left ``None``.

    This is the only thing that may certify ``production_execution_verified``; local
    sequencer arithmetic and the start snapshot are explicitly not enough.
    """

    available: bool
    source: str
    reason: str
    phase: str | None = None
    generation: int | None = None
    snapshot_unix_nanos: int | None = None
    latest_activity_generation: int | None = None
    latest_activity_unix_nanos: int | None = None
    reconciled_activity_generation: int | None = None
    reconciliation_unix_nanos: int | None = None
    ordered_after_start: bool | None = None
    covers_latest_activity: bool | None = None
    instrument_matches: bool | None = None
    follows_reconciliation: bool | None = None
    complete: bool | None = None
    snapshot_fresh: bool | None = None
    instrument_id: str | None = None
    position_qty: Decimal | None = None
    reconciled_flat: bool | None = None
    own_open_orders: int | None = None
    foreign_open_orders: int | None = None
    unknown_submissions: int | None = None
    shutdown_status: str | None = None
    late_fills: int | None = None
    execution_cost: Mapping[str, object] | None = None

    def as_dict(self) -> dict[str, object]:
        return {
            "available": self.available,
            "source": self.source,
            "reason": self.reason,
            "phase": self.phase,
            "generation": self.generation,
            "snapshot_unix_nanos": self.snapshot_unix_nanos,
            "latest_activity_generation": self.latest_activity_generation,
            "latest_activity_unix_nanos": self.latest_activity_unix_nanos,
            "reconciled_activity_generation": self.reconciled_activity_generation,
            "reconciliation_unix_nanos": self.reconciliation_unix_nanos,
            "ordered_after_start": self.ordered_after_start,
            "covers_latest_activity": self.covers_latest_activity,
            "instrument_matches": self.instrument_matches,
            "follows_reconciliation": self.follows_reconciliation,
            "complete": self.complete,
            "snapshot_fresh": self.snapshot_fresh,
            "instrument_id": self.instrument_id,
            "position_qty": None if self.position_qty is None else str(self.position_qty),
            "reconciled_flat": self.reconciled_flat,
            "own_open_orders": self.own_open_orders,
            "foreign_open_orders": self.foreign_open_orders,
            "unknown_submissions": self.unknown_submissions,
            "shutdown_status": self.shutdown_status,
            "late_fills": self.late_fills,
            "execution_cost": dict(self.execution_cost) if self.execution_cost else None,
        }


def _unfinal(reason: str) -> FinalReconciliation:
    return FinalReconciliation(False, "unavailable", reason)


def _read_reconciliation_phase(
    target: object | None,
    *,
    run_id: str,
    phase: str,
    instrument_id: str,
    start_generation: int,
    start_snapshot_unix_nanos: int,
    reconciliation: FinalReconciliation | None = None,
) -> FinalReconciliation:
    snapshot, reason = _read_snapshot_mapping(target, run_id=run_id, phase=phase)
    if snapshot is None:
        return _unfinal(reason or f"the {phase} snapshot is unavailable")
    cost = snapshot.get("execution_cost")
    generation = _as_count_or_none(snapshot.get("generation"))
    snapshot_ns = _as_count_or_none(snapshot.get("snapshot_unix_nanos"))
    latest_generation = _as_count_or_none(snapshot.get("latest_activity_generation"))
    latest_ns = _as_count_or_none(snapshot.get("latest_activity_unix_nanos"))
    reconciled_activity = _as_count_or_none(snapshot.get("reconciled_activity_generation"))
    reconciliation_ns = _as_count_or_none(snapshot.get("reconciliation_unix_nanos"))
    observed_instrument = snapshot.get("instrument_id")
    return FinalReconciliation(
        available=True,
        source=f"native-{phase}-snapshot",
        reason=f"the {phase} snapshot carries this run's token; absent facts stay unknown",
        phase=phase,
        generation=generation,
        snapshot_unix_nanos=snapshot_ns,
        latest_activity_generation=latest_generation,
        latest_activity_unix_nanos=latest_ns,
        reconciled_activity_generation=reconciled_activity,
        reconciliation_unix_nanos=reconciliation_ns,
        ordered_after_start=bool(
            generation is not None and generation > start_generation
            and snapshot_ns is not None and snapshot_ns >= start_snapshot_unix_nanos
        ),
        covers_latest_activity=bool(
            latest_generation is not None
            and reconciled_activity == latest_generation
            and latest_ns is not None
            and reconciliation_ns is not None
            and reconciliation_ns >= latest_ns
        ),
        instrument_matches=bool(observed_instrument == instrument_id),
        follows_reconciliation=(
            None if reconciliation is None
            else bool(
                generation is not None
                and reconciliation.generation is not None
                and generation >= reconciliation.generation
                and snapshot_ns is not None
                and reconciliation.snapshot_unix_nanos is not None
                and snapshot_ns >= reconciliation.snapshot_unix_nanos
                and latest_generation is not None
                and reconciliation.latest_activity_generation is not None
                and latest_generation >= reconciliation.latest_activity_generation
                and latest_ns is not None
                and reconciliation.latest_activity_unix_nanos is not None
                and latest_ns >= reconciliation.latest_activity_unix_nanos
                and reconciliation_ns is not None
                and reconciliation.reconciliation_unix_nanos is not None
                and reconciliation_ns >= reconciliation.reconciliation_unix_nanos
            )
        ),
        complete=_as_bool(snapshot.get("complete")),
        snapshot_fresh=_as_bool(snapshot.get("snapshot_fresh")),
        instrument_id=(observed_instrument if isinstance(observed_instrument, str) else None),
        position_qty=_as_decimal(snapshot.get("position_qty")),
        reconciled_flat=_as_bool(snapshot.get("reconciled_flat")),
        own_open_orders=_as_count_or_none(snapshot.get("own_open_orders")),
        foreign_open_orders=_as_count_or_none(snapshot.get("foreign_open_orders")),
        unknown_submissions=_as_count_or_none(snapshot.get("unknown_submissions")),
        shutdown_status=(
            str(snapshot.get("shutdown_status")).strip()
            if isinstance(snapshot.get("shutdown_status"), str)
            and str(snapshot.get("shutdown_status")).strip() else None
        ),
        late_fills=_as_count_or_none(snapshot.get("late_fills")),
        execution_cost=dict(cost) if isinstance(cost, Mapping) else None,
    )


def read_trade_reconciliation(target: object | None, *, run_id: str, instrument_id: str,
                              start_generation: int,
                              start_snapshot_unix_nanos: int) -> FinalReconciliation:
    """Read the fresh authoritative reconciliation required before ordered stop."""
    return _read_reconciliation_phase(
        target, run_id=run_id, phase="reconciled", instrument_id=instrument_id,
        start_generation=start_generation,
        start_snapshot_unix_nanos=start_snapshot_unix_nanos,
    )


def read_trade_final(target: object | None, *, run_id: str, instrument_id: str,
                     start_generation: int, start_snapshot_unix_nanos: int,
                     reconciliation: FinalReconciliation) -> FinalReconciliation:
    """Read the clean-shutdown snapshot after a fresh pre-stop reconciliation."""
    return _read_reconciliation_phase(
        target, run_id=run_id, phase="final", instrument_id=instrument_id,
        start_generation=start_generation,
        start_snapshot_unix_nanos=start_snapshot_unix_nanos,
        reconciliation=reconciliation,
    )


def reconciliation_is_clean(reconciliation: FinalReconciliation) -> bool:
    """Whether native proved flat account state after all known activity, before stop."""
    return bool(
        reconciliation.available
        and reconciliation.phase in ("reconciled", "final")
        and reconciliation.ordered_after_start is True
        and reconciliation.covers_latest_activity is True
        and reconciliation.instrument_matches is True
        and reconciliation.complete is True
        and reconciliation.snapshot_fresh is True
        and reconciliation.position_qty == 0
        and reconciliation.reconciled_flat is True
        and reconciliation.own_open_orders == 0
        and reconciliation.foreign_open_orders == 0
        and reconciliation.unknown_submissions == 0
        and reconciliation.late_fills == 0
    )


def final_is_clean(final: FinalReconciliation) -> bool:
    """The native end-of-run conjunction; an unknown value is never clean."""
    return bool(
        reconciliation_is_clean(final)
        and final.phase == "final"
        and final.follows_reconciliation is True
        and final.shutdown_status == "clean"
    )


def wait_for_trade_reconciliation(
    target: object | None,
    *,
    run_id: str,
    instrument_id: str,
    start_generation: int,
    start_snapshot_unix_nanos: int,
    deadline_monotonic: float,
    cancel_event: threading.Event | None = None,
    poll_secs: float = 0.05,
) -> FinalReconciliation:
    """Poll the bounded native snapshot until latest activity is authoritatively reconciled."""
    last = _unfinal("the reconciled snapshot was not observed")
    while True:
        if cancel_event is not None and cancel_event.is_set():
            return _unfinal("the reconciled snapshot wait was canceled before completion")
        last = read_trade_reconciliation(
            target, run_id=run_id, instrument_id=instrument_id,
            start_generation=start_generation,
            start_snapshot_unix_nanos=start_snapshot_unix_nanos,
        )
        if reconciliation_is_clean(last):
            return last
        remaining = deadline_monotonic - time.monotonic()
        if remaining <= 0:
            return last
        time.sleep(min(poll_secs, remaining))


def readiness_blockers(readiness: TradeReadiness, plan: TradePlan) -> list[str]:
    """Every start gate that is not explicitly satisfied, fail-closed.

    An unknown value is a blocker: the run does not treat "the snapshot did not say" as
    "the account is fine".  ``account_flat`` is only required when the plan requires a flat
    start (it always does today), and the required margin is the entry notional plus a fixed
    buffer.
    """
    if not readiness.available:
        return [f"native readiness snapshot unavailable: {readiness.reason}"]
    problems: list[str] = []
    if readiness.generation is None or readiness.generation < 1:
        problems.append("start snapshot generation is missing or not positive")
    if readiness.snapshot_unix_nanos is None or readiness.snapshot_unix_nanos < 1:
        problems.append("start snapshot timestamp is missing or not positive")
    if readiness.identity_match != IDENTITY_MATCHED:
        problems.append(
            f"account identity is {readiness.identity_match!r}, not 'matched': an unknown or "
            f"unmatched identity refuses",
        )
    if plan.envelope.require_flat_start and readiness.account_flat is not True:
        problems.append(
            f"account flat is {readiness.account_flat!r}, not true: the bounded probe refuses "
            f"a non-flat or unknown start",
        )
    if readiness.coverage_complete is not True:
        problems.append(
            f"account coverage is {readiness.coverage_complete!r}, not complete: an absent "
            f"row is never read as flat",
        )
    if readiness.foreign_open_orders != 0:
        problems.append(
            f"foreign open orders is {readiness.foreign_open_orders!r}, not 0",
        )
    if readiness.own_open_orders != 0:
        problems.append(
            f"this client's open orders is {readiness.own_open_orders!r}, not 0",
        )
    if readiness.native_ready is not True:
        problems.append(f"native readiness is {readiness.native_ready!r}, not true")
    if readiness.metadata_fresh is not True:
        problems.append(f"fresh metadata is {readiness.metadata_fresh!r}, not true")
    if readiness.trading_enabled is not True:
        problems.append(
            f"instrument trading enabled is {readiness.trading_enabled!r}, not true",
        )
    if readiness.underlying_market_closed is not False:
        problems.append(
            "the official underlying-market isClosed flag is not explicitly false; this is "
            "a conservative probe policy for equity-hours execution, not a claim that Ondo "
            "disables perpetual trading outside underlying hours",
        )
    if readiness.dms_verified is not True:
        problems.append(f"DMS verified is {readiness.dms_verified!r}, not true")
    required_usdc = plan.envelope.min_available_margin_usdc
    if readiness.available_margin_usdc is None:
        problems.append(
            f"available margin USDC is unknown: the configured same-unit threshold is "
            f"{required_usdc} USDC, and an unknown balance refuses",
        )
    elif readiness.available_margin_usdc < required_usdc:
        problems.append(
            f"available margin {readiness.available_margin_usdc} USDC is below the "
            f"configured threshold {required_usdc} USDC; no USD/USDC conversion is inferred",
        )
    return problems


# ------------------------------------------------------------------ sequencer


@dataclass
class OrderView:
    """A cumulative view of one order, read from the venue cache."""

    role: str
    client_order_id: str
    status: str
    filled_qty: Decimal
    avg_px: Decimal | None
    is_closed: bool
    rejected_reason: str | None = None


@dataclass(frozen=True)
class EntryDecision:
    role: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    reduce_only: bool
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "side": self.side,
            "quantity": str(self.quantity),
            "limit_price": str(self.limit_price),
            "reduce_only": self.reduce_only,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class CloseDecision:
    role: str
    side: str
    quantity: Decimal
    limit_price: Decimal
    reduce_only: bool
    attempt: int
    reason: str

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "side": self.side,
            "quantity": str(self.quantity),
            "limit_price": str(self.limit_price),
            "reduce_only": self.reduce_only,
            "attempt": self.attempt,
            "reason": self.reason,
        }


@dataclass
class FillRecord:
    role: str
    client_order_id: str
    trade_id: str | None
    quantity: Decimal
    price: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "role": self.role,
            "client_order_id": self.client_order_id,
            "trade_id": self.trade_id,
            "quantity": str(self.quantity),
            "price": str(self.price),
        }


def _floor_to(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_FLOOR) * increment


def _ceil_to(value: Decimal, increment: Decimal) -> Decimal:
    if increment <= 0:
        return value
    return (value / increment).to_integral_value(rounding=ROUND_CEILING) * increment


class TradeSequencer:
    """The bounded trade decision core, independent of Nautilus.

    It owns exactly the orchestration policy: one entry limit-IOC, then at most
    ``max_close_attempts`` reduce-only limit-IOC closes for the *confirmed* filled quantity,
    no new opening retry, and a fail-closed ``uncertain`` whenever the outcome cannot be
    established.  It never signs, encodes or submits anything; the strategy translates its
    decisions into ``order_factory`` / ``submit_order`` calls, and the native pre-send
    envelope remains the enforcing authority.
    """

    def __init__(self, plan: TradePlan, limits: TradeLimits) -> None:
        self.plan = plan
        self.limits = limits
        self.instrument = plan.instrument
        self.entry = plan.entry
        self.close = plan.close
        self.phase = PHASE_NOT_STARTED
        self.outcome = OUTCOME_NOT_STARTED
        self.terminal_reason: str | None = None
        self.done = False
        self.failures: list[str] = []
        self.entry_client_order_id: str | None = None
        self.close_client_order_ids: list[str] = []
        self.entry_view: OrderView | None = None
        self.close_views: dict[str, OrderView] = {}
        self.close_attempts = 0
        self.fills: list[FillRecord] = []
        self._seen_fill_keys: set[tuple] = set()
        self.duplicate_fills = 0
        self.unknown: dict[str, str] = {}
        self._last_bid: Decimal | None = None
        self._last_ask: Decimal | None = None
        self._last_quote_monotonic: float | None = None
        self._entry_terminal_seen = False
        self._close_terminal_seen: set[str] = set()
        # App-side send accounting.  ``new_risk_requests`` counts opening (new-risk) creates
        # and is capped at 1; ``app_requests`` counts every write the app can observe itself
        # send, including cancels; ``orders_submitted`` counts orders.  The native transport's
        # broader request budget also covers background reads the app cannot see, so the app
        # reports that number but does not claim to enforce it.
        self.entry_attempts = 0
        self.new_risk_requests = 0
        self.app_requests = 0
        self.orders_submitted = 0
        self.cancel_requests = 0
        self.late_fills = 0
        # A close that could not be priced on a fresh quote waits for the next one instead of
        # sending a stale price.
        self._pending_close = False
        self._inflight_close_id: str | None = None

    # -- quote -----------------------------------------------------------------------

    def on_quote(self, bid: Decimal, ask: Decimal, *, now: float) -> None:
        self._last_bid = bid
        self._last_ask = ask
        self._last_quote_monotonic = now
        if self.phase == PHASE_NOT_STARTED:
            self.phase = PHASE_AWAITING_QUOTE

    def begin(self) -> None:
        if self.phase == PHASE_NOT_STARTED:
            self.phase = PHASE_AWAITING_QUOTE

    def quote_is_fresh(self, now: float, max_age_secs: Decimal) -> bool:
        if self._last_quote_monotonic is None:
            return False
        return Decimal(str(now - self._last_quote_monotonic)) <= max_age_secs

    def _touch(self, side: str) -> Decimal | None:
        if side == SIDE_BUY:
            return self._last_ask
        return self._last_bid

    # -- entry -----------------------------------------------------------------------

    def decide_entry(self, *, now: float) -> EntryDecision | None:
        """Return the one entry the plan permits, or finish with a truthful no-trade."""
        if self.done or self.phase not in (PHASE_AWAITING_QUOTE, PHASE_NOT_STARTED):
            return None
        if self.phase == PHASE_NOT_STARTED:
            self.phase = PHASE_AWAITING_QUOTE
        if not self.quote_is_fresh(now, self.entry.max_quote_age_secs):
            self._finish(OUTCOME_NO_TRADE, "no fresh quote within the frozen freshness window")
            return None
        touch = self._touch(self.entry.side)
        if touch is None or touch <= 0:
            self._finish(OUTCOME_NO_TRADE, "no executable touch to price the entry from")
            return None
        slip = self.entry.max_slippage_bps / Decimal("10000")
        increment = self.instrument.price_increment
        if self.entry.side == SIDE_BUY:
            raw = touch * (Decimal(1) + slip)
            limit = min(raw, self.entry.limit_price)
            limit = _floor_to(limit, increment)
            if limit < touch:
                self._finish(
                    OUTCOME_NO_TRADE,
                    f"the fresh ask {touch} is above the frozen worst price "
                    f"{self.entry.limit_price}: no marketable entry within the bound",
                )
                return None
        else:
            raw = touch * (Decimal(1) - slip)
            limit = max(raw, self.entry.limit_price)
            limit = _ceil_to(limit, increment)
            if limit > touch:
                self._finish(
                    OUTCOME_NO_TRADE,
                    f"the fresh bid {touch} is below the frozen worst price "
                    f"{self.entry.limit_price}: no marketable entry within the bound",
                )
                return None
        quantity = _floor_to(self.entry.quantity, self.instrument.size_increment)
        if quantity <= 0:
            self._finish(OUTCOME_NO_TRADE, "the frozen quantity rounds to zero at the venue tick")
            return None
        notional = quantity * limit
        if (
            self.instrument.min_notional is not None
            and notional < self.instrument.min_notional
        ):
            self._finish(
                OUTCOME_NO_TRADE,
                f"the marketable order {quantity} @ {limit} is {notional}, below the venue "
                f"min_notional {self.instrument.min_notional}",
            )
            return None
        if notional > self.entry.notional_usd or notional > self.limits.max_notional_per_order_usd:
            self._finish(
                OUTCOME_NO_TRADE,
                f"the marketable order notional {notional} exceeds the frozen bound "
                f"{self.entry.notional_usd}",
            )
            return None
        refusal = self._authorize_send(ROLE_ENTRY)
        if refusal is not None:
            self._finish(OUTCOME_UNCERTAIN, refusal)
            return None
        self.entry_attempts += 1
        self.new_risk_requests += 1
        self.app_requests += 1
        self.orders_submitted += 1
        self.phase = PHASE_ENTERING
        return EntryDecision(
            role=ROLE_ENTRY,
            side=self.entry.side,
            quantity=quantity,
            limit_price=limit,
            reduce_only=False,
            reason=(
                f"limit-IOC {self.entry.side} {quantity} @ {limit} "
                f"(touch {touch}, frozen worst {self.entry.limit_price})"
            ),
        )

    def bind_entry(self, client_order_id: str) -> None:
        self.entry_client_order_id = client_order_id

    def _authorize_send(self, role: str) -> str | None:
        """The app-side backstop for the count budgets; ``None`` means it may be sent."""
        if self.done:
            return "the run is already finished; no further write is permitted"
        if self.app_requests >= self.plan.envelope.max_app_requests:
            return (
                f"the app write-request cap {self.plan.envelope.max_app_requests} is reached; the "
                f"next write is refused rather than sent"
            )
        if self.orders_submitted >= self.plan.envelope.max_orders:
            return f"the order cap {self.plan.envelope.max_orders} is reached"
        if role == ROLE_ENTRY:
            if self.entry_attempts >= 1:
                return "only ONE opening attempt is permitted; no new opening retry"
            if self.new_risk_requests >= self.plan.envelope.max_new_risk_requests:
                return (
                    f"the new-risk request cap {self.plan.envelope.max_new_risk_requests} is "
                    f"reached"
                )
        return None

    def note_cancel_requested(self) -> str | None:
        """Reserve one bounded own-order cancel, or return the named refusal."""
        if self.app_requests >= self.plan.envelope.max_app_requests:
            return (
                f"the app write-request cap {self.plan.envelope.max_app_requests} is reached; "
                f"the cancel is refused rather than sent"
            )
        self.cancel_requests += 1
        self.app_requests += 1
        return None

    def note_entry(self, view: OrderView, *, now: float) -> CloseDecision | None:
        """Fold an entry order state in and, when terminal and filled, start the close."""
        self.entry_view = view
        if not view.is_closed and view.status not in REJECTED_STATUSES:
            return None
        if self._entry_terminal_seen:
            return None
        self._entry_terminal_seen = True
        self.entry_client_order_id = view.client_order_id or self.entry_client_order_id
        if view.rejected_reason or view.status in REJECTED_STATUSES:
            self._finish(OUTCOME_REJECTED, f"entry rejected: {view.rejected_reason or view.status}")
            return None
        if view.filled_qty <= 0:
            self._finish(OUTCOME_NO_TRADE, "the entry reached a terminal state with zero fill")
            return None
        self.phase = PHASE_CLOSING
        self._pending_close = True
        return self._next_close(now)

    # -- close -----------------------------------------------------------------------

    def bind_close(self, client_order_id: str) -> None:
        self.close_client_order_ids.append(client_order_id)
        self._inflight_close_id = client_order_id
        self._pending_close = False

    def maybe_close(self, *, now: float) -> CloseDecision | None:
        """Resume a pending close when a fresh quote arrives (do not send a stale price)."""
        if self.done or self.phase != PHASE_CLOSING:
            return None
        if not self._pending_close or self._inflight_close_id is not None:
            return None
        return self._next_close(now)

    def note_close(self, view: OrderView, *, now: float) -> CloseDecision | None:
        """Fold a close order state in and decide retry / flat / uncertain."""
        if view.client_order_id in self._close_terminal_seen:
            return None
        if not view.is_closed:
            self.close_views[view.client_order_id] = view
            return None
        self._close_terminal_seen.add(view.client_order_id)
        self.close_views[view.client_order_id] = view
        if self._inflight_close_id == view.client_order_id:
            self._inflight_close_id = None
        if self.entry_view is None or self.entry_view.filled_qty <= 0:
            self._finish(OUTCOME_UNCERTAIN, "a close was observed before a confirmed entry fill")
            return None
        remaining = self.entry_view.filled_qty - self.closed_quantity
        if remaining <= 0:
            self._finish(self._flat_outcome(), "the confirmed position is flat")
            return None
        if self.close_attempts >= self.close.max_close_attempts:
            self._finish(
                OUTCOME_UNCERTAIN,
                f"the position is still short {remaining} after "
                f"{self.close_attempts} reduce-only attempt(s); it is not flat",
            )
            return None
        if view.status in REJECTED_STATUSES:
            # A rejected reduce-only is still a failed flatten; retry is permitted within the
            # attempt budget because reduce-only cannot grow the position.
            self.failures.append(
                f"a reduce-only close was rejected: {view.rejected_reason or view.status}",
            )
        self._pending_close = True
        return self._next_close(now)

    def _next_close(self, now: float) -> CloseDecision | None:
        if self.entry_view is None:
            return None
        remaining = self.entry_view.filled_qty - self.closed_quantity
        if remaining <= 0:
            self._finish(self._flat_outcome(), "the confirmed position is flat")
            return None
        if self.close_attempts >= self.close.max_close_attempts:
            self._finish(OUTCOME_UNCERTAIN, "no bounded close attempt remains and the position "
                        "is not flat")
            return None
        if not self.quote_is_fresh(now, self.close.max_quote_age_secs):
            # Do not send a stale price: mark pending and let the next fresh quote wake us.
            self._pending_close = True
            return None
        if self._inflight_close_id is not None:
            return None
        if self._last_bid is None or self._last_ask is None:
            self._pending_close = True
            return None
        quantity = _floor_to(remaining, self.instrument.size_increment)
        if quantity <= 0:
            self._finish(
                OUTCOME_UNCERTAIN,
                f"the residual {remaining} rounds to zero at the venue size increment; it "
                f"cannot be closed as a separate order",
            )
            return None
        slip_bps = self.entry.max_slippage_bps / Decimal("10000")
        increment = self.instrument.price_increment
        if self.close.side == SIDE_BUY:
            raw = self._last_ask * (Decimal(1) + slip_bps)
            limit = _ceil_to(min(raw, self.close.limit_price), increment)
            if limit < self._last_ask:
                self._pending_close = True
                return None
        else:
            raw = self._last_bid * (Decimal(1) - slip_bps)
            limit = _floor_to(max(raw, self.close.limit_price), increment)
            if limit > self._last_bid:
                self._pending_close = True
                return None
        if limit <= 0:
            self._finish(OUTCOME_UNCERTAIN, "the reduce-only limit price rounds to zero")
            return None
        refusal = self._authorize_send(ROLE_CLOSE)
        if refusal is not None:
            self._finish(OUTCOME_UNCERTAIN, refusal)
            return None
        self.close_attempts += 1
        self.app_requests += 1
        self.orders_submitted += 1
        self._pending_close = False
        return CloseDecision(
            role=ROLE_CLOSE,
            side=self.close.side,
            quantity=quantity,
            limit_price=limit,
            reduce_only=True,
            attempt=self.close_attempts,
            reason=(
                f"reduce-only {self.close.side} {quantity} @ {limit} "
                f"(attempt {self.close_attempts}/{self.close.max_close_attempts}, residual "
                f"{remaining})"
            ),
        )

    @property
    def closed_quantity(self) -> Decimal:
        """The confirmed quantity closed by this run, from each close order's cumulative fill."""
        return sum((view.filled_qty for view in self.close_views.values()), Decimal(0))

    @property
    def net_position(self) -> Decimal:
        entry_filled = self.entry_view.filled_qty if self.entry_view is not None else Decimal(0)
        return entry_filled - self.closed_quantity

    def _flat_outcome(self) -> str:
        if self.entry_view is None:
            return OUTCOME_UNCERTAIN
        if self.entry_view.filled_qty >= self.entry.quantity:
            return OUTCOME_FILLED
        return OUTCOME_PARTIAL

    # -- fills / errors --------------------------------------------------------------

    def note_fill(self, fill: FillRecord) -> None:
        """Record a fill once; a duplicate trade id is not counted twice.

        A non-duplicate fill that arrives for an already-terminal order or after the run
        finished is a *late fill*: it invalidates a clean result, because the position it
        changed was reconciled without it.
        """
        key = (fill.client_order_id, fill.trade_id) if fill.trade_id else None
        if key is not None:
            if key in self._seen_fill_keys:
                self.duplicate_fills += 1
                return
            self._seen_fill_keys.add(key)
        late = bool(
            self.done
            or (fill.role == ROLE_ENTRY and self._entry_terminal_seen)
            or (fill.role == ROLE_CLOSE and fill.client_order_id in self._close_terminal_seen)
        )
        self.fills.append(fill)
        if late:
            self.late_fills += 1

    def note_submit_error(self, role: str, reason: str) -> None:
        """A submission whose result is unknown stops new sends and defers reconciliation."""
        self.unknown[role] = reason
        self.failures.append(f"{role} submission outcome unknown: {reason}")
        # Never resend: the native layer owns the original client order id and reconciles it.
        self._finish(
            OUTCOME_UNCERTAIN,
            f"the {role} submission result is unknown ({reason}); no replacement id is sent "
            f"and the native original-id reconciliation is the authority",
        )

    def _finish(self, outcome: str, reason: str) -> None:
        if self.done:
            return
        self.done = True
        self.phase = PHASE_DONE
        self.outcome = outcome
        self.terminal_reason = reason
        self._pending_close = False
        if outcome == OUTCOME_UNCERTAIN:
            self.failures.append(reason)

    # -- reporting -------------------------------------------------------------------

    def outstanding_client_order_ids(self) -> list[str]:
        ids: list[str] = []
        if self.entry_client_order_id and not self._entry_terminal():
            ids.append(self.entry_client_order_id)
        for client_order_id in self.close_client_order_ids:
            view = self.close_views.get(client_order_id)
            if view is None or not view.is_closed:
                ids.append(client_order_id)
        return ids

    def _entry_terminal(self) -> bool:
        return bool(
            self.entry_view is not None
            and (self.entry_view.is_closed or self.entry_view.status in REJECTED_STATUSES)
        )

    def snapshot(self) -> dict[str, object]:
        entry_filled = self.entry_view.filled_qty if self.entry_view is not None else Decimal(0)
        entry_avg = self.entry_view.avg_px if self.entry_view is not None else None
        return {
            "phase": self.phase,
            "outcome": self.outcome,
            "done": self.done,
            "entry_client_order_id": self.entry_client_order_id,
            "close_client_order_ids": list(self.close_client_order_ids),
            "close_attempts": self.close_attempts,
            "entry_attempts": self.entry_attempts,
            "new_risk_requests": self.new_risk_requests,
            "app_requests": self.app_requests,
            "orders_submitted": self.orders_submitted,
            "cancel_requests": self.cancel_requests,
            "late_fills": self.late_fills,
            "pending_close": self._pending_close,
            "entry_filled_qty": str(entry_filled),
            "entry_avg_px": None if entry_avg is None else str(entry_avg),
            "closed_quantity": str(self.closed_quantity),
            "net_position": str(self.net_position),
            "fills": [fill.as_dict() for fill in self.fills],
            "duplicate_fills": self.duplicate_fills,
            "unknown": dict(self.unknown),
            "failures": list(self.failures),
        }


# --------------------------------------------------------------------- strategy


class OndoTradeConfig(StrategyConfig):
    """Configuration for :class:`OndoTradeStrategy`."""

    _CUSTOM_FIELDS = ("instrument_id", "plan", "limits", "readiness_provider")

    def __new__(cls, *args: object, **kwargs: object) -> "OndoTradeConfig":
        for field_name in cls._CUSTOM_FIELDS:
            kwargs.pop(field_name, None)
        return super().__new__(cls, *args, **kwargs)

    def __init__(
        self,
        instrument_id: InstrumentId,
        plan: TradePlan,
        limits: TradeLimits,
        readiness_provider: Callable[[], TradeReadiness] | None = None,
        **_kwargs: object,
    ) -> None:
        super().__init__()
        self.instrument_id = instrument_id
        self.plan = plan
        self.limits = limits
        self.readiness_provider = readiness_provider


def _guarded(method: Callable[..., None]) -> Callable[..., None]:
    """Log-and-fail wrapper for strategy callbacks (Nautilus swallows handler errors)."""

    @functools.wraps(method)
    def wrapper(self: "OndoTradeStrategy", *args: object, **kwargs: object) -> None:
        try:
            return method(self, *args, **kwargs)
        except Exception:
            self.record_failure(f"unhandled exception in {method.__name__}:\n{traceback.format_exc()}")
            return None

    return wrapper


class OndoTradeStrategy(Strategy):
    """Drives one bounded production trade: one entry limit-IOC, <=2 reduce-only closes.

    Every callback is wrapped, every decision comes from :class:`TradeSequencer`, and the
    only order methods the strategy calls are ``order_factory`` / ``submit_order`` /
    ``cancel_order`` on its *own* client order ids.  There is no batch submission, no
    cancel-all and no account-wide action.
    """

    def __init__(self, config: OndoTradeConfig) -> None:
        super().__init__(config)
        self._instrument_id = config.instrument_id
        self._plan = config.plan
        self._limits = config.limits
        self._readiness_provider = config.readiness_provider
        self._sequencer = TradeSequencer(config.plan, config.limits)
        self._instrument: object | None = None
        self._readiness: TradeReadiness | None = None
        self._start_blockers: list[str] = []
        self._submit_errors: list[str] = []
        self._cancel_errors: list[str] = []
        self._cancel_requested_ids: set[str] = set()
        self._leftovers: list[str] = []
        self._leftover_source = "no cleanup has run"
        self.done_event = threading.Event()
        self.cleanup_done_event = threading.Event()
        self.summary_line = ""

    # -- properties ------------------------------------------------------------------

    @property
    def sequencer(self) -> TradeSequencer:
        return self._sequencer

    @property
    def outcome(self) -> str:
        if self._start_blockers:
            return OUTCOME_BLOCKED
        return self._sequencer.outcome

    @property
    def failures(self) -> list[str]:
        return list(self._start_blockers) + list(self._sequencer.failures) + list(self._submit_errors)

    @property
    def leftovers(self) -> list[str]:
        return list(self._leftovers)

    @property
    def cancel_errors(self) -> list[str]:
        return list(self._cancel_errors)

    @property
    def readiness(self) -> TradeReadiness | None:
        return self._readiness

    @property
    def started(self) -> bool:
        return self._instrument is not None and not self._start_blockers

    # -- logging ---------------------------------------------------------------------

    def _log_safe(self, level: str, message: str) -> None:
        flat = " | ".join(message.splitlines())
        try:
            getattr(self.log, level)(flat)
        except RuntimeError:
            print(message, file=sys.stderr, flush=True)

    def _note_failure(self, reason: str) -> None:
        self._sequencer.failures.append(reason)
        print(f"[trade] FAILED: {reason}", file=sys.stderr, flush=True)
        self._log_safe("error", f"[trade] FAILED: {reason}")

    def record_failure(self, reason: str) -> None:
        self._note_failure(reason)
        self._finish("failure")

    # -- lifecycle -------------------------------------------------------------------

    @_guarded
    def on_start(self) -> None:
        self._instrument = self.cache.instrument(self._instrument_id) if self.cache else None
        if self._instrument is None:
            self.record_failure(f"instrument {self._instrument_id} not found in the cache")
            return
        mismatches = instrument_matches_plan(self._instrument, self._plan.instrument)
        if mismatches:
            self._start_blockers = mismatches
            self._note_failure(
                "the fresh instrument metadata does not match the approved plan: "
                + "; ".join(mismatches),
            )
            self._finish("start-blocked")
            return
        if self._readiness_provider is not None:
            readiness = self._readiness_provider()
        else:
            readiness = _unready("no readiness provider was configured")
        self._readiness = readiness
        blockers = readiness_blockers(readiness, self._plan)
        if blockers:
            self._start_blockers = blockers
            self._note_failure("start gates not satisfied: " + "; ".join(blockers))
            self._finish("start-blocked")
            return
        self._sequencer.begin()
        self.subscribe_quotes(self._instrument_id)
        self._log_safe(
            "info",
            f"[trade] instrument {self._instrument.id} verified; awaiting a fresh quote",
        )

    @_guarded
    def on_stop(self) -> None:
        # This callback is synchronous on the strategy event dispatcher. Never sleep or poll
        # here: doing so prevents the order events being awaited from being delivered.
        outstanding = self._outstanding_orders()
        self._leftovers = [f"{coid}={status}" for coid, status in outstanding]
        self._leftover_source = (
            "on_stop local observation; native final reconciliation is authoritative"
        )
        if not outstanding:
            self.cleanup_done_event.set()
        else:
            message = (
                f"[trade] LEFTOVER: {len(self._leftovers)} own order(s) not confirmed closed "
                f"when synchronous on_stop ran: "
                f"{', '.join(self._leftovers)} - the position must not be called flat"
            )
            print(message, file=sys.stderr, flush=True)
            self._log_safe("error", message)

    # -- market data -----------------------------------------------------------------

    @_guarded
    def on_quote(self, quote) -> None:
        bid = _decimal_attr(quote, "bid_price")
        ask = _decimal_attr(quote, "ask_price")
        if bid is None or ask is None or bid <= 0 or ask <= 0:
            return
        now = time.monotonic()
        self._sequencer.on_quote(bid, ask, now=now)
        if self._sequencer.phase == PHASE_AWAITING_QUOTE:
            decision = self._sequencer.decide_entry(now=now)
        elif self._sequencer.phase == PHASE_CLOSING:
            # A pending close is woken by a fresh quote; it is never sent at a stale price.
            decision = self._sequencer.maybe_close(now=now)
        else:
            decision = None
        if decision is None:
            self._maybe_finish()
            return
        self._submit(decision)

    # -- order submission ------------------------------------------------------------

    def _submit(self, decision: EntryDecision | CloseDecision) -> None:
        side = OrderSide.BUY if decision.side == SIDE_BUY else OrderSide.SELL
        try:
            order = self.order_factory.limit(
                instrument_id=self._instrument_id,
                order_side=side,
                quantity=self._instrument.make_qty(decision.quantity),
                price=self._instrument.make_price(decision.limit_price),
                time_in_force=TimeInForce.IOC,
                reduce_only=decision.reduce_only,
            )
        except Exception as exc:  # noqa: BLE001 - a local refusal is still a result
            self._submit_errors.append(f"{decision.role}: {type(exc).__name__}: {exc}")
            self._sequencer.note_submit_error(
                decision.role, f"order_factory refused locally: {type(exc).__name__}: {exc}",
            )
            self._maybe_finish()
            return
        client_order_id = str(getattr(order, "client_order_id", ""))
        if decision.role == ROLE_ENTRY:
            self._sequencer.bind_entry(client_order_id)
        else:
            self._sequencer.bind_close(client_order_id)
        self._log_safe("info", f"[trade] submitting {decision.reason} client_order_id={client_order_id}")
        try:
            self.submit_order(order)
        except Exception as exc:  # noqa: BLE001 - unknown, never resent
            self._sequencer.note_submit_error(
                decision.role,
                f"submit_order raised {type(exc).__name__}: {exc}; the original client order "
                f"id {client_order_id} is preserved for native reconciliation",
            )
            self._maybe_finish()

    # -- order events ----------------------------------------------------------------

    def _order_view(self, role: str, client_order_id: str) -> OrderView | None:
        try:
            # The real Cache.order enforces the ClientOrderId type; a str raises TypeError.
            order = self.cache.order(_client_order_id(client_order_id))
        except Exception:
            order = None
        if order is None:
            return None
        status = str(getattr(order, "status", "UNKNOWN")).upper()
        filled = _decimal_attr(order, "filled_qty") or Decimal(0)
        avg = _decimal_attr(order, "avg_px")
        closed = bool(getattr(order, "is_closed", False)) or status in REJECTED_STATUSES
        reason = None
        if status in REJECTED_STATUSES:
            try:
                rejection = next(
                    (event for event in reversed(order.events())
                     if isinstance(event, OrderRejected)),
                    None,
                )
                reason = str(getattr(rejection, "reason", "") or "")
            except Exception:
                reason = ""
        return OrderView(role, str(client_order_id), status, filled, avg, closed, reason or None)

    def _handle(self, role: str, client_order_id: str) -> None:
        now = time.monotonic()
        view = self._order_view(role, client_order_id)
        if view is None:
            self._note_failure(f"no cached order for {client_order_id}")
            return
        if role == ROLE_ENTRY:
            decision = self._sequencer.note_entry(view, now=now)
        else:
            decision = self._sequencer.note_close(view, now=now)
        if decision is not None and self._sequencer.phase == PHASE_CLOSING:
            self._submit(decision)
        self._maybe_finish()

    @_guarded
    def on_order_submitted(self, event) -> None:
        return None

    @_guarded
    def on_order_accepted(self, event) -> None:
        role = self._role_for(event.client_order_id)
        self._handle(role, str(event.client_order_id))

    @_guarded
    def on_order_canceled(self, event) -> None:
        role = self._role_for(event.client_order_id)
        self._handle(role, str(event.client_order_id))

    @_guarded
    def on_order_expired(self, event) -> None:
        role = self._role_for(event.client_order_id)
        self._handle(role, str(event.client_order_id))

    @_guarded
    def on_order_filled(self, event) -> None:
        role = self._role_for(event.client_order_id)
        client_order_id = str(event.client_order_id)
        self._sequencer.note_fill(FillRecord(
            role=role,
            client_order_id=client_order_id,
            trade_id=_trade_id(event),
            quantity=_decimal_attr(event, "last_qty") or Decimal(0),
            price=_decimal_attr(event, "last_px") or Decimal(0),
        ))
        self._handle(role, client_order_id)

    @_guarded
    def on_order_rejected(self, event) -> None:
        role = self._role_for(event.client_order_id)
        self._handle(role, str(event.client_order_id))

    @_guarded
    def on_order_denied(self, event) -> None:
        role = self._role_for(event.client_order_id)
        self._handle(role, str(event.client_order_id))

    def _role_for(self, client_order_id: object) -> str:
        value = str(client_order_id)
        if value in self._sequencer.close_client_order_ids:
            return ROLE_CLOSE
        return ROLE_ENTRY

    # -- cleanup ---------------------------------------------------------------------

    def _outstanding_orders(self) -> list[tuple[str, str]]:
        outstanding: list[tuple[str, str]] = []
        for client_order_id in self._sequencer.outstanding_client_order_ids():
            role = self._role_for(client_order_id)
            view = self._order_view(role, client_order_id)
            if view is None or not view.is_closed:
                outstanding.append(
                    (client_order_id, view.status if view is not None else "unknown (not cached)"),
                )
        return outstanding

    def _run_cleanup(self, trigger: str) -> None:
        outstanding = self._outstanding_orders()
        if not outstanding:
            self.cleanup_done_event.set()
            return
        for client_order_id, status in outstanding:
            if client_order_id in self._cancel_requested_ids:
                continue
            refusal = self._sequencer.note_cancel_requested()
            if refusal is not None:
                if refusal not in self._sequencer.failures:
                    self._note_failure(refusal)
                continue
            self._cancel_requested_ids.add(client_order_id)
            try:
                # The real Strategy.cancel_order enforces the ClientOrderId type too.
                self.cancel_order(_client_order_id(client_order_id))
            except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
                detail = f"{client_order_id}: {type(exc).__name__}: {exc}"
                self._cancel_errors.append(detail)
                self._note_failure(
                    f"cleanup ({trigger}) could not request a cancel for {detail}; the order "
                    f"may still be live on the venue",
                )

    def wait_for_cleanup(self, timeout_secs: float, *, poll_secs: float = 0.05) -> bool:
        """Bounded wait for every own order to reach a terminal state.

        Each own cancel is requested at most once. Returns ``True`` when nothing is
        outstanding and ``False`` on timeout; either way ``cleanup_done_event`` is set, and a
        timeout records the leftovers as unconfirmed.  The native stop does not close a
        position, so this app-side cleanup is the flatten's confirmation path.
        """
        deadline = time.monotonic() + max(0.0, float(timeout_secs))
        self._run_cleanup("wait")
        while True:
            outstanding = self._outstanding_orders()
            if not outstanding:
                self._leftovers = []
                self._leftover_source = "cleanup wait confirmed every own order terminal"
                self.cleanup_done_event.set()
                return True
            if time.monotonic() >= deadline:
                self._leftovers = [f"{coid}={status}" for coid, status in outstanding]
                self._leftover_source = (
                    "bounded cleanup wait expired - local state, NOT venue-confirmed"
                )
                self.cleanup_done_event.set()
                return False
            time.sleep(poll_secs)

    # -- completion ------------------------------------------------------------------

    def _maybe_finish(self) -> None:
        if self._sequencer.done and not self.done_event.is_set():
            self._finish("done")

    def _finish(self, reason: str) -> None:
        if self.done_event.is_set():
            return
        try:
            self._run_cleanup(f"finish:{reason}")
        except Exception:  # noqa: BLE001 - recorded, never swallowed
            self._note_failure(f"cleanup raised while finishing:\n{traceback.format_exc()}")
        finally:
            self.summary_line = (
                f"[trade] SUMMARY reason={reason} outcome={self.outcome} "
                f"entry_filled={self._sequencer.net_position + self._sequencer.closed_quantity} "
                f"closed={self._sequencer.closed_quantity} "
                f"net_position={self._sequencer.net_position} "
                f"close_attempts={self._sequencer.close_attempts} "
                f"unknown={len(self._sequencer.unknown)} failures={len(self.failures)}"
            )
            self._log_safe("error" if self.failures else "info", self.summary_line)
            self.done_event.set()


def _client_order_id(value: object) -> ClientOrderId:
    """Coerce ``value`` to the real ``ClientOrderId`` the Cache/Strategy require."""
    if isinstance(value, ClientOrderId):
        return value
    return ClientOrderId.from_str(str(value))


def _decimal_attr(obj: object, name: str) -> Decimal | None:
    try:
        value = getattr(obj, name, None)
    except Exception:
        return None
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    as_decimal = getattr(value, "as_decimal", None)
    if callable(as_decimal):
        try:
            value = as_decimal()
        except Exception:
            return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _amount_attr(
    obj: object,
    name: str,
) -> tuple[Decimal | None, str | None, str]:
    """Return exact amount, optional currency and present/absent/malformed state."""
    try:
        value = getattr(obj, name, None)
    except Exception:
        return None, None, "malformed"
    if value is None:
        return None, None, "absent"
    currency_value = getattr(value, "currency", None)
    currency = str(currency_value) if currency_value is not None else None
    if isinstance(value, Decimal):
        number = value
    else:
        as_decimal = getattr(value, "as_decimal", None)
        try:
            raw = as_decimal() if callable(as_decimal) else value
            number = Decimal(str(raw))
        except (InvalidOperation, ValueError, TypeError):
            return None, currency, "malformed"
    if not number.is_finite() or number <= 0:
        return None, currency, "malformed"
    return number, currency, "present"


def _trade_id(event: object) -> str | None:
    for name in ("trade_id", "fill_id", "venue_order_id"):
        try:
            value = getattr(event, name, None)
        except Exception:
            value = None
        if value is not None:
            text = str(value)
            if text:
                return text
    return None


def instrument_matches_plan(instrument: object, spec: InstrumentSpec) -> list[str]:
    """The fresh-metadata checks that must agree with the approved plan before any order."""
    problems: list[str] = []
    instrument_id = str(getattr(instrument, "id", ""))
    if instrument_id and instrument_id != spec.instrument_id:
        problems.append(
            f"instrument id {instrument_id!r} is not the approved {spec.instrument_id!r}",
        )
    checks = (
        ("price_increment", spec.price_increment),
        ("size_increment", spec.size_increment),
    )
    for name, expected in checks:
        actual = _decimal_attr(instrument, name)
        if actual is None or actual != expected:
            problems.append(f"{name} is {actual}, the approved plan freezes {expected}")
    min_quantity, _min_quantity_currency, min_quantity_state = _amount_attr(
        instrument, "min_quantity",
    )
    if min_quantity_state == "malformed":
        problems.append("min_quantity is malformed; present invalid metadata is not unpublished")
    elif spec.min_quantity is None:
        if min_quantity_state == "present":
            problems.append(
                f"min_quantity is now {min_quantity}, but the approved plan records it "
                "unpublished; regenerate the plan",
            )
    elif min_quantity_state != "present" or min_quantity != spec.min_quantity:
        problems.append(
            f"min_quantity is {min_quantity}, the approved plan freezes {spec.min_quantity}",
        )
    min_notional, min_notional_currency, min_notional_state = _amount_attr(
        instrument, "min_notional",
    )
    if min_notional_state == "malformed":
        problems.append("min_notional is malformed; present invalid metadata is not unpublished")
    elif spec.min_notional is None:
        if min_notional_state == "present":
            problems.append(
                f"min_notional is now {min_notional} {min_notional_currency or ''}, but the "
                "approved plan records it unpublished; regenerate the plan",
            )
    else:
        if min_notional_state != "present" or min_notional != spec.min_notional:
            problems.append(
                f"min_notional is {min_notional}, the approved plan freezes "
                f"{spec.min_notional}",
            )
        if min_notional_currency != spec.min_notional_currency:
            problems.append(
                f"min_notional currency is {min_notional_currency!r}, the approved plan "
                f"freezes {spec.min_notional_currency!r}",
            )
    if spec.max_quantity is not None:
        actual_max = _decimal_attr(instrument, "max_quantity")
        if actual_max is not None and actual_max != spec.max_quantity:
            problems.append(
                f"max_quantity is {actual_max}, the approved plan freezes {spec.max_quantity}",
            )
    quote_currency = str(getattr(instrument, "quote_currency", "") or "")
    if quote_currency != spec.quote_currency:
        problems.append(
            f"quote_currency is {quote_currency!r}, the approved plan freezes "
            f"{spec.quote_currency!r}",
        )
    return problems


# ------------------------------------------------------------------------ plan docs


def intent_document(args: argparse.Namespace, limits: TradeLimits,
                    capability: NativeTradeCapability) -> dict[str, object]:
    """The ``--dry-run`` intent: resolved limits, a plan template with unresolved values.

    It carries no credential, opens no socket and constructs no client; the null plan values
    are the point - a value the run has not resolved cannot be approved.
    """
    return {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "plan_version": 1,
        "dry_run": True,
        "resolved": False,
        "client_constructed": False,
        "requests_sent": 0,
        "env_file_read": False,
        "mode": MODE_PRODUCTION_TRADE,
        "venue": ONDO_VENUE,
        "environment": "production",
        "side": args.side,
        "canonical_env_file_for_live_run": "E:\\Nautilus-Perps\\.env",
        "native_write_capable": capability.supported,
        "limits_configured": limits.limits_configured,
        "live_execution_ready": False,
        "live_execution_ready_reason": (
            "dry-run has no account/readiness/authorization evidence; static limits and native "
            "capability do not make a production run ready"
        ),
        "native_trade_capability": capability.as_dict(),
        "limits": limits.as_dict(),
        "caps": [row.as_dict() for row in limits.rows],
        "instrument": {
            "symbol": limits.symbol,
            "instrument_id": limits.instrument,
            "price_increment": None,
            "size_increment": None,
            "min_quantity": None,
            "min_quantity_source": "unpublished",
            "min_notional": None,
            "min_notional_currency": None,
            "min_notional_source": "unpublished",
            "max_quantity": None,
            "quote_currency": "USD",
        },
        "entry": {
            "side": args.side,
            "order_type": "limit",
            "time_in_force": TIME_IN_FORCE,
            "reduce_only": False,
            "quantity": None,
            "limit_price": None,
            "max_slippage_bps": str(limits.max_slippage_bps),
            "notional_usd": str(limits.entry_notional_usd),
            "max_quote_age_secs": None,
        },
        "close": {
            "side": None if args.side not in (SIDE_BUY, SIDE_SELL)
            else (SIDE_SELL if args.side == SIDE_BUY else SIDE_BUY),
            "order_type": "limit",
            "time_in_force": TIME_IN_FORCE,
            "reduce_only": True,
            "limit_price": None,
            "max_close_attempts": limits.max_close_attempts,
            "max_quote_age_secs": None,
        },
        "envelope": {
            "max_notional_per_order_usd": str(limits.max_notional_per_order_usd),
            "max_gross_exposure_usd": str(limits.max_gross_exposure_usd),
            "max_orders": limits.max_orders,
            "max_new_risk_requests": limits.max_new_risk_requests,
            "max_app_requests": limits.max_app_requests,
            "min_available_margin_usdc": str(limits.min_available_margin_usdc),
            "deadline_secs": limits.deadline_secs,
            "require_flat_start": True,
        },
        "account": {
            "environment": "production",
            "journal_path": None,
            "expected_venue_account_id_present": None,
        },
        "dms": {
            "mode": "trading",
            "activation": "subscribe cancelAllOrdersAfterPerps on login",
            "timeout_secs": 30,
            "renewal_interval_secs": 15,
            "renewal_message_verified": False,
            "release_on_stop": "only when nothing is unconfirmed",
            "account_wide_effect": (
                "a lapsed dead-man-switch timer cancels every resting order on the whole "
                "account, not only this run's orders"
            ),
        },
        "cleanup": {
            "cancel_own_orders_only": True,
            "confirm_cancels": True,
            "confirm_flat_position": True,
            "stop_budget_secs": limits.cleanup_budget_secs,
        },
        "authorization": {
            "user_phrase_上主网_present": False,
            "approved_plan_sha256": None,
        },
        "plan_sha256": None,
        "requires": {
            "native_write_envelope": NATIVE_ENVELOPE_CLASS,
            "native_config_field": NATIVE_ENVELOPE_FIELD,
            "native_identity_field": NATIVE_IDENTITY_FIELD,
            "native_marker": NATIVE_TRADE_MARKER,
            "native_readiness_snapshot": NATIVE_TRADE_SNAPSHOT,
        },
        "production_execution_verified": False,
        "fees": "unknown",
        "no_economic_claim_made": True,
        "protocol_verified": False,
    }


# ---------------------------------------------------------------------- report


def _write_json(path: Path, payload: object) -> dict[str, object]:
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


def publish_report(payload: dict[str, object], out_dir: Path, *, run_id: str, complete: bool,
                   meta: dict[str, object], failure: str | None = None) -> dict[str, object]:
    """Publish one attempt: stage under its run id, switch the payload, then the manifest."""
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


def acceptance_document(strategy: "OndoTradeStrategy | None", plan: TradePlan | None,
                        capability: NativeTradeCapability,
                        readiness: TradeReadiness | None,
                        final: FinalReconciliation | None = None) -> dict[str, object]:
    """The explicit gates behind ``production_execution_verified``, each named.

    A gate is true only when the run actually observed the fact it names.  ``clean`` is the
    conjunction of every start gate, the local entry/close confirmation, and the native
    FINAL reconciliation - a local flat and the start snapshot are not enough.  It is never
    true on a dry run, a refusal, an uncertain outcome, a late fill or a dirty shutdown.
    """
    sequencer = strategy.sequencer if strategy is not None else None
    entry_view = sequencer.entry_view if sequencer is not None else None
    entry_filled = entry_view.filled_qty if entry_view is not None else None
    closed = sequencer.closed_quantity if sequencer is not None else None
    net = sequencer.net_position if sequencer is not None else None
    entry_confirmed = bool(
        plan is not None and entry_view is not None and entry_filled is not None
        and entry_filled >= plan.entry.quantity
    )
    close_confirmed = bool(
        plan is not None and entry_filled is not None and closed is not None
        and entry_filled > 0 and closed >= entry_filled
    )
    local_flat = bool(net is not None and net == 0)
    no_unknown = bool(sequencer is not None and not sequencer.unknown)
    no_late_fills = bool(sequencer is not None and sequencer.late_fills == 0)
    no_outstanding = bool(
        strategy is not None and not strategy._sequencer.outstanding_client_order_ids()
    )
    flat_reconciled = bool(
        local_flat
        and final is not None
        and final.reconciled_flat is True
        and final.position_qty == 0
        and final.instrument_matches is True
    )
    native_final_clean = bool(final is not None and final_is_clean(final))
    start_gates = {
        "start_snapshot_ordered": bool(
            readiness is not None
            and readiness.generation is not None and readiness.generation >= 1
            and readiness.snapshot_unix_nanos is not None
            and readiness.snapshot_unix_nanos >= 1
        ),
        "identity_matched": bool(readiness is not None
                                 and readiness.identity_match == IDENTITY_MATCHED),
        "account_flat": bool(readiness is not None and readiness.account_flat is True),
        "coverage_complete": bool(readiness is not None
                                  and readiness.coverage_complete is True),
        "foreign_open_orders_zero": bool(readiness is not None
                                         and readiness.foreign_open_orders == 0),
        "own_open_orders_zero": bool(readiness is not None
                                     and readiness.own_open_orders == 0),
        "native_ready": bool(readiness is not None and readiness.native_ready is True),
        "metadata_fresh": bool(readiness is not None and readiness.metadata_fresh is True),
        "trading_enabled": bool(readiness is not None
                                and readiness.trading_enabled is True),
        "underlying_market_open": bool(
            readiness is not None and readiness.underlying_market_closed is False
        ),
        "dms_verified": bool(readiness is not None and readiness.dms_verified is True),
        "margin_sufficient": bool(
            readiness is not None and readiness.available_margin_usdc is not None
            and plan is not None and readiness.available_margin_usdc
            >= plan.envelope.min_available_margin_usdc
        ),
    }
    # ``clean`` is the conjunction of EVERY gate; a future refactor cannot drop one.
    clean = bool(
        capability.supported
        and plan is not None
        and all(start_gates.values())
        and entry_confirmed
        and close_confirmed
        and local_flat
        and no_unknown
        and no_late_fills
        and no_outstanding
        and native_final_clean
    )
    document = {
        "native_write_envelope": capability.supported,
        "authorization_confirmed": bool(plan is not None),
        "plan_hash_bound": bool(plan is not None and plan.plan_sha256),
        "entry_confirmed": entry_confirmed,
        "close_confirmed": close_confirmed,
        "local_sequencer_flat": local_flat,
        "flat_reconciled": flat_reconciled,
        "no_unknown_submission": no_unknown,
        "no_late_fills": no_late_fills,
        "no_outstanding_own_orders": no_outstanding,
        "native_final_clean": native_final_clean,
        "clean": clean,
    }
    document.update(start_gates)
    return document


def execution_result_is_success(
    strategy: "OndoTradeStrategy | None",
    plan: TradePlan | None,
    capability: NativeTradeCapability,
    readiness: TradeReadiness | None,
    final: FinalReconciliation | None,
) -> bool:
    """Whether the session may return zero after its local and native evidence is combined."""
    if strategy is None:
        return False
    sequencer = strategy.sequencer
    if sequencer.orders_submitted == 0:
        return strategy.outcome == OUTCOME_NO_TRADE and not strategy.failures
    return acceptance_document(strategy, plan, capability, readiness, final)["clean"] is True


def trade_report_document(*, run_id: str, started: datetime, finished: datetime,
                          exit_code: int, failure: str | None, plan: TradePlan | None,
                          limits: TradeLimits, capability: NativeTradeCapability,
                          readiness: TradeReadiness | None,
                          strategy: "OndoTradeStrategy | None",
                          stop_condition: str,
                          stops: Sequence[dict[str, object]],
                          watchdog: dict[str, bool] | None,
                          diagnostics: object,
                          reconciliation: FinalReconciliation | None = None,
                          final: FinalReconciliation | None = None,
                          dms_release_target: object | None = None) -> dict[str, object]:
    """The published report: plan identity, gates, accounting and honest verdicts."""
    sequencer = strategy.sequencer if strategy is not None else None
    net = sequencer.net_position if sequencer is not None else None
    entry_filled = sequencer.entry_view.filled_qty if (
        sequencer is not None and sequencer.entry_view is not None) else None
    acceptance = acceptance_document(strategy, plan, capability, readiness, final)
    execution_cost = final.execution_cost if final is not None else None
    fees_known = bool(isinstance(execution_cost, Mapping)
                      and execution_cost.get("fees_usd") is not None)
    outcome = strategy.outcome if strategy is not None else OUTCOME_NOT_STARTED
    document: dict[str, object] = {
        "tool": TOOL,
        "schema_version": SCHEMA_VERSION,
        "venue": ONDO_VENUE,
        "run_id": run_id,
        "run_dir": f"{RUNS_DIRNAME}/{run_id}",
        "mode": MODE_PRODUCTION_TRADE,
        "environment": "production",
        "started_at_utc": started.isoformat(timespec="milliseconds"),
        "finished_at_utc": finished.isoformat(timespec="milliseconds"),
        "complete": exit_code == EXIT_OK and failure is None,
        "failure": failure,
        "exit_code": exit_code,
        "stop_condition": stop_condition,
        "watchdog": dict(watchdog) if watchdog is not None else None,
        "stops": [dict(stop) for stop in stops],
        "native_write_capable": capability.supported,
        "native_trade_capability": capability.as_dict(),
        "readiness": None if readiness is None else readiness.as_dict(),
        "limits": limits.as_dict(),
        "caps": [row.as_dict() for row in limits.rows],
        "plan": None if plan is None else {
            "plan_sha256": plan.plan_sha256,
            "instrument": plan.instrument.as_dict(),
            "entry": plan.entry.as_dict(),
            "close": plan.close.as_dict(),
            "envelope": plan.envelope.as_dict(),
            "journal_path": plan.journal_path,
            "expected_venue_account_id_present": plan.expected_venue_account_id_present,
            "dms": dict(plan.dms),
            "cleanup": dict(plan.cleanup),
        },
        "account_id_published": False,
        "credentials_published": False,
        "raw_frames_published": False,
        "dms_release": read_dms_release_diagnostics(dms_release_target, run_id=run_id),
        "outcome": outcome,
        "terminal_reason": sequencer.terminal_reason if sequencer is not None else None,
        "phase": sequencer.phase if sequencer is not None else PHASE_NOT_STARTED,
        "entry_client_order_id": sequencer.entry_client_order_id if sequencer else None,
        "close_client_order_ids": list(sequencer.close_client_order_ids) if sequencer else [],
        "entry_filled_qty": None if entry_filled is None else str(entry_filled),
        "closed_quantity": None if sequencer is None else str(sequencer.closed_quantity),
        "net_position": None if net is None else str(net),
        "close_attempts": sequencer.close_attempts if sequencer is not None else 0,
        "fills": [fill.as_dict() for fill in sequencer.fills] if sequencer else [],
        "duplicate_fills": sequencer.duplicate_fills if sequencer is not None else 0,
        "unknown_submissions": dict(sequencer.unknown) if sequencer else {},
        "outstanding_orders": (
            list(strategy.leftovers) if strategy is not None else []
        ),
        "reported_failures": (
            list(strategy.failures) if strategy is not None else []
        ),
        "accounting": {
            "submitted": [sequencer.entry_client_order_id] if (
                sequencer is not None and sequencer.entry_client_order_id) else [],
            "reduce_only_submitted": list(sequencer.close_client_order_ids) if sequencer else [],
            "filled": [
                fill.as_dict() for fill in (sequencer.fills if sequencer else [])
                if fill.role == ROLE_ENTRY
            ],
            "unknown": dict(sequencer.unknown) if sequencer else {},
            "no_trade": outcome == OUTCOME_NO_TRADE,
            "partial": outcome == OUTCOME_PARTIAL,
            "uncertain": outcome == OUTCOME_UNCERTAIN,
        },
        "acceptance": acceptance,
        "production_execution_verified": acceptance["clean"],
        "pre_stop_reconciliation": (
            None if reconciliation is None else reconciliation.as_dict()
        ),
        "final_reconciliation": None if final is None else final.as_dict(),
        "send_accounting": {
            "entry_attempts": sequencer.entry_attempts if sequencer else 0,
            "new_risk_requests": sequencer.new_risk_requests if sequencer else 0,
            "max_new_risk_requests": (
                plan.envelope.max_new_risk_requests if plan is not None else None
            ),
            "app_requests": sequencer.app_requests if sequencer else 0,
            "max_app_requests": plan.envelope.max_app_requests if plan is not None else None,
            "orders_submitted": sequencer.orders_submitted if sequencer else 0,
            "max_orders": plan.envelope.max_orders if plan is not None else None,
            "cancel_requests": sequencer.cancel_requests if sequencer else 0,
            "total_http_requests_claimed": False,
            "total_http_requests_reason": (
                "native background reconciliation reads are outside the app counter; the "
                "probe does not advertise an unobservable global HTTP count"
            ),
        },
        "late_fills": sequencer.late_fills if sequencer else 0,
        "fees": execution_cost.get("fees_usd") if fees_known else "unknown",
        "fees_known": fees_known,
        "fees_reason": (
            "a fee is recorded only when the native final snapshot carried one; no fee is "
            "invented and no cost or profit is computed"
        ),
        "execution_cost": dict(execution_cost) if isinstance(execution_cost, Mapping) else None,
        "no_economic_claim_made": True,
        "protocol_verified": False,
        "protocol_verified_reason": (
            "the private protocol is documented, not host-confirmed; a single observed fill "
            "would not prove it, and no fill was observed offline"
        ),
        "native_diagnostics": (
            diagnostics.as_dict() if hasattr(diagnostics, "as_dict") else diagnostics
        ),
    }
    return document


# ------------------------------------------------------------------------- run


def _load_environment(environ: Mapping[str, str] | None) -> dict[str, str]:
    """Resolve the credential environment, honouring the explicit canonical ``.env`` only.

    Mirrors ``ondo_probe.load_environment``: an injected mapping is the whole environment;
    the real process environment gets the ``python-dotenv`` convenience, and
    ``ONDO_PROBE_ENV_FILE`` names the canonical credential file rather than a worktree copy.
    """
    if environ is not None:
        return {str(key): str(value) for key, value in dict(environ).items()}
    explicit = str(os.environ.get(ENV_FILE_VARIABLE) or "").strip()
    try:
        from dotenv import load_dotenv

        if explicit:
            if not Path(explicit).is_file():
                raise TradeProbeRefused(
                    f"{ENV_FILE_VARIABLE} names {explicit!r}, which is not a readable file: "
                    f"the credential file is refused rather than silently skipped",
                )
            load_dotenv(explicit)
        else:
            load_dotenv()
    except ImportError:  # pragma: no cover - dotenv is a convenience
        pass
    return dict(os.environ)


def check_credentials(environ: Mapping[str, str]) -> None:
    """Refuse a live run whose MAINNET credential names are missing or empty."""
    missing = [name for name in MAINNET_CREDENTIAL_VARIABLES
               if not str(environ.get(name) or "").strip()]
    if missing:
        raise TradeProbeRefused(
            f"--mode {MODE_PRODUCTION_TRADE} needs production credentials and "
            f"{', '.join(missing)} {'is' if len(missing) == 1 else 'are'} missing or empty in "
            f"the environment. Their values are never read into this message, and no sandbox "
            f"variable is consulted as a fallback",
        )
    try:
        # The production identity follows the read-only layer exactly: the configured value
        # is the venue's raw accountID, the Nautilus AccountId is ONDO-{raw}, and the raw
        # value is what is handed to the native expected_venue_account_id.
        resolve_account_identity(MODE_PRODUCTION_READONLY, dict(environ),
                                 MAINNET_CREDENTIAL_VARIABLES)
    except OndoProbeRefused as exc:
        raise TradeProbeRefused(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - the value is never printed
        raise TradeProbeRefused(
            f"{MAINNET_ACCOUNT_ID_VARIABLE} is not an account id this session can be opened "
            f"with ({type(exc).__name__}); its value is not printed here",
        ) from exc


def _build_node(plan: TradePlan, adapter: OndoAdapter, limits: TradeLimits,
                readiness_provider: Callable[[], TradeReadiness],
                 node_factory: Callable[..., object] | None,
                 account_id: AccountId, expected_venue_account_id: str,
                 entry_deadline_unix_nanos: int, cleanup_deadline_unix_nanos: int,
                 run_id: str,
                ) -> tuple[object, object | None, dict[str, object], "OndoTradeStrategy"]:
    """Configure the node, a real execution client and the trade strategy."""
    # The envelope and the identity are passed only to a wheel that accepts them; the
    # capability probe already refused a live run when they are absent, so this is the
    # construction of a contract the probe proved present.
    envelope_cls = getattr(adapter, NATIVE_ENVELOPE_CLASS)
    envelope = envelope_cls(
        instrument_id=InstrumentId.from_str(plan.instrument.instrument_id),
        entry_side=plan.entry.side,
        entry_max_quantity=str(plan.entry.quantity),
        entry_worst_price=str(plan.entry.limit_price),
        entry_max_notional_usd=str(plan.entry.notional_usd),
        close_side=plan.close.side,
        close_max_quantity=str(plan.entry.quantity),
        close_worst_price=str(plan.close.limit_price),
        max_close_attempts=plan.close.max_close_attempts,
        max_notional_per_order_usd=str(plan.envelope.max_notional_per_order_usd),
        max_gross_exposure_usd=str(plan.envelope.max_gross_exposure_usd),
        max_orders=plan.envelope.max_orders,
        max_new_risk_requests=plan.envelope.max_new_risk_requests,
        max_app_requests=plan.envelope.max_app_requests,
        min_available_margin_usdc=str(plan.envelope.min_available_margin_usdc),
        entry_deadline_unix_nanos=entry_deadline_unix_nanos,
        cleanup_deadline_unix_nanos=cleanup_deadline_unix_nanos,
        require_flat_start=plan.envelope.require_flat_start,
    )
    exec_config_kwargs: dict[str, object] = {
        "environment": adapter.OndoEnvironment.PRODUCTION,
        "account_read_only": False,
        "account_id": account_id,
        "base_url_http": PRODUCTION_BASE_URL_HTTP,
        "base_url_ws": PRODUCTION_BASE_URL_WS,
        "journal_path": plan.journal_path,
        "expected_venue_account_id": expected_venue_account_id,
        "execution_envelope": envelope,
        # The handshake that makes the native snapshot provably this run's; the capability
        # probe refuses a wheel whose config does not accept it.
        "diagnostics_run_id": run_id,
        # The approved dead-man-switch timeout is driven into the native config; the native
        # adapter derives the renewal interval from it.
        "dms_timeout_secs": int(plan.dms["timeout_secs"]),
        "reconcile_interval_secs": 1,
        "allow_production_orders": True,
    }
    exec_config = adapter.OndoExecutionClientConfig(**exec_config_kwargs)
    endpoint_refusal = production_endpoint_refusal(PRODUCTION_BASE_URL_HTTP)
    if endpoint_refusal is not None:  # pragma: no cover - the constant is the authority
        raise OndoProbeError(f"the production endpoint is refused: {endpoint_refusal}")
    builder = (
        LiveNode.builder(PROBE_INSTANCE, TraderId.from_str(TRADER_ID), Environment.LIVE)
        .with_logging(LoggerConfig(
            stdout_level=LogLevel.OFF,
            fileout_level=LogLevel.OFF,
            bypass_logging=True,
            print_config=False,
        ))
        .with_risk_engine_config(LiveRiskEngineConfig(
            bypass=False,
            max_notional_per_order={plan.instrument.instrument_id: str(
                plan.envelope.max_notional_per_order_usd)},
        ))
        .with_timeout_connection(PRODUCTION_READONLY_CONNECTION_TIMEOUT_SECS)
        # Production stop performs a fresh signed account reconciliation before it can release
        # the DMS. Keep the node's outer bound wider than the native 15-second production budget
        # and its final private-stream close allowance.
        .with_timeout_disconnection_secs(PRODUCTION_TRADE_DISCONNECTION_TIMEOUT_SECS)
    )
    builder = builder.add_data_client(
        None, adapter.OndoDataClientFactory(),
        adapter.OndoDataClientConfig(
            environment=adapter.OndoEnvironment.PRODUCTION,
            load_ids=[InstrumentId.from_str(plan.instrument.instrument_id)],
        ),
    )
    exec_factory = adapter.OndoExecutionClientFactory()
    builder = builder.add_exec_client(None, exec_factory, exec_config)
    strategy = OndoTradeStrategy(OndoTradeConfig(
        instrument_id=InstrumentId.from_str(plan.instrument.instrument_id),
        plan=plan,
        limits=limits,
        readiness_provider=readiness_provider,
    ))
    factory = node_factory if node_factory is not None else (lambda b: b.build())
    node = factory(builder)
    add = getattr(node, "add_strategy", None)
    if callable(add):
        add(strategy)
    session = {
        "data_client": "nautilus_trader.adapters.ondo.OndoDataClientFactory",
        "exec_client": "nautilus_trader.adapters.ondo.OndoExecutionClientFactory",
        "account_read_only": False,
        "armed_production_trade": True,
    }
    return node, exec_factory, session, strategy  # type: ignore[return-value]


def start_trade_watchdog(done_event: threading.Event, stop_callable: Callable[[], object],
                         timeout_secs: float,
                         cleanup_event: threading.Event | None = None,
                         cleanup_grace_secs: float = 0.0,
                         cancel_event: threading.Event | None = None,
                         ) -> tuple[threading.Thread, dict[str, object]]:
    """Stop the node when the strategy signals done, or at the deadline.

    After a finished strategy, the node is kept alive up to the separate cleanup budget so
    the app's own cancel/confirm wait can observe order events; the outcome is reported in
    ``cleanup_confirmed``.  The timeout path never waits - ``on_stop`` does the last-chance
    cleanup there.
    """
    state: dict[str, object] = {
        "fired": False,
        "timed_out": False,
        "canceled": False,
        "stop_attempted": False,
        "stop_confirmed": False,
        "stop_error": None,
        "stopped": False,
        "worker_finished": False,
        "cleanup_confirmed": False,
    }

    def _run() -> None:
        deadline = time.monotonic() + max(0.0, timeout_secs)
        fired = False
        while True:
            if cancel_event is not None and cancel_event.is_set():
                state["canceled"] = True
                state["worker_finished"] = True
                return
            if done_event.wait(min(0.05, max(0.0, deadline - time.monotonic()))):
                fired = True
                break
            if time.monotonic() >= deadline:
                break
        state["fired"] = fired
        state["timed_out"] = not fired
        if fired and cleanup_event is not None and cleanup_grace_secs > 0:
            state["cleanup_confirmed"] = cleanup_event.wait(cleanup_grace_secs)
        try:
            state["stop_attempted"] = True
            outcome = stop_callable()
            state["stop_confirmed"] = bool(
                isinstance(outcome, Mapping)
                and outcome.get("attempted") is True
                and outcome.get("requested") is True
                and outcome.get("stopped") is True
                and outcome.get("error") is None
            )
        except Exception as exc:  # noqa: BLE001 - worker reports through plain state
            state["stop_error"] = f"{type(exc).__name__}: {exc}"
        finally:
            state["stopped"] = state["stop_confirmed"] is True
            state["worker_finished"] = True

    thread = threading.Thread(target=_run, name="ondo-trade-watchdog", daemon=True)
    thread.start()
    return thread, state


def execute(plan: TradePlan, limits: TradeLimits, *, adapter: OndoAdapter, environ: dict[str, str],
            node_factory: Callable[..., object] | None, now: Callable[[], datetime],
            out_dir: Path, log: Callable[[str], None],
            capability: NativeTradeCapability,
            deadline_secs: int | None = None) -> int:
    """Run one bounded trade session and publish it; every exit path stops and publishes."""
    # The production identity follows the read-only layer: raw venue accountID ->
    # AccountId("ONDO-{raw}"), and the raw value to expected_venue_account_id.
    account_id, expected_venue_account_id = resolve_account_identity(
        MODE_PRODUCTION_READONLY, dict(environ), MAINNET_CREDENTIAL_VARIABLES)
    if account_id is None or not expected_venue_account_id:
        raise TradeProbeRefused("the production account identity could not be resolved")
    effective_deadline = int(plan.envelope.deadline_secs if deadline_secs is None
                             else min(plan.envelope.deadline_secs, deadline_secs))
    session_started_monotonic = time.monotonic()
    started = now()
    cleanup_budget_secs = int(plan.cleanup["stop_budget_secs"])
    if effective_deadline <= cleanup_budget_secs:
        raise TradeProbeRefused(
            "the effective deadline must leave time for the bounded cleanup window",
        )
    cleanup_deadline_unix_nanos = int(
        (started + timedelta(seconds=effective_deadline)).timestamp() * 1_000_000_000
    )
    entry_deadline_unix_nanos = int(
        (started + timedelta(seconds=effective_deadline - cleanup_budget_secs)).timestamp()
        * 1_000_000_000
    )
    entry_deadline_monotonic = (
        session_started_monotonic + effective_deadline - cleanup_budget_secs
    )
    cleanup_deadline_monotonic = session_started_monotonic + effective_deadline
    run_id = new_run_id(started)
    stops: list[dict[str, object]] = []
    stop_target: list[object] = []
    stop_via: list[str] = ["none"]
    watchdog_slot: list[threading.Thread] = []
    watchdog_state: dict[str, object] | None = None
    node: object | None = None
    strategy: OndoTradeStrategy | None = None
    native_target: object | None = None
    readiness: TradeReadiness | None = None
    reconciliation: FinalReconciliation | None = None
    final: FinalReconciliation | None = None
    readiness_slot: list[TradeReadiness] = []
    stop_handoff: dict[str, object] = {
        "reconciliation": None,
        "failure": None,
        "stop": None,
    }
    handoff_lock = threading.Lock()
    watchdog_cancel = threading.Event()
    diagnostics: object = None
    failure: str | None = None
    stop_condition = "not-started"
    exit_code = EXIT_OK
    def _stop_holder() -> dict[str, object]:
        local_reconciliation: FinalReconciliation
        local_failure: str | None = None
        outcome: dict[str, object] | None = None
        with handoff_lock:
            accepted_readiness = readiness_slot[-1] if readiness_slot else None
        try:
            if (
                accepted_readiness is not None
                and accepted_readiness.generation is not None
                and accepted_readiness.snapshot_unix_nanos is not None
            ):
                local_reconciliation = wait_for_trade_reconciliation(
                    native_target, run_id=run_id,
                    instrument_id=plan.instrument.instrument_id,
                    start_generation=accepted_readiness.generation,
                    start_snapshot_unix_nanos=accepted_readiness.snapshot_unix_nanos,
                    deadline_monotonic=cleanup_deadline_monotonic,
                    cancel_event=watchdog_cancel,
                )
            else:
                local_reconciliation = _unfinal(
                    "no accepted start snapshot existed, so final reconciliation cannot be "
                    "ordered",
                )
            if not reconciliation_is_clean(local_reconciliation):
                local_failure = (
                    "native pre-stop reconciliation did not prove fresh flat account state"
                )
        except Exception as exc:  # noqa: BLE001 - plain handoff; stop still runs
            local_reconciliation = _unfinal(
                f"pre-stop reconciliation raised {type(exc).__name__}",
            )
            local_failure = (
                f"native pre-stop reconciliation raised {type(exc).__name__}; "
                "the run is uncertain"
            )
        finally:
            try:
                outcome = bounded_stop(
                    stop_target[0] if stop_target else None,
                    label="watchdog",
                    via=stop_via[0],
                )
            finally:
                with handoff_lock:
                    stop_handoff["reconciliation"] = local_reconciliation
                    stop_handoff["failure"] = local_failure
                    stop_handoff["stop"] = outcome
        return outcome or {
            "attempted": True,
            "requested": False,
            "stopped": False,
            "error": "watchdog stop returned no outcome",
        }

    def _readiness_provider() -> TradeReadiness:
        observed = read_trade_readiness(native_target, run_id=run_id)
        with handoff_lock:
            readiness_slot[:] = [observed]
        return observed

    try:
        node, native_target, session, strategy = _build_node(
            plan, adapter, limits, _readiness_provider, node_factory, account_id,
            expected_venue_account_id, entry_deadline_unix_nanos,
            cleanup_deadline_unix_nanos, run_id,
        )
        target, via = resolve_stop_target(node)
        stop_target.append(target)
        stop_via[0] = via
        # A fake/offline node may have driven on_start during construction. Real nodes fill
        # this through _readiness_provider on the owner thread once strategy start runs.
        initial_readiness = strategy.readiness
        if initial_readiness is not None:
            with handoff_lock:
                readiness_slot[:] = [initial_readiness]
        watchdog, watchdog_state = start_trade_watchdog(
            strategy.done_event, _stop_holder,
            max(0.0, entry_deadline_monotonic - time.monotonic()),
            strategy.cleanup_done_event, 0.0,
            cancel_event=watchdog_cancel,
        )
        watchdog_slot.append(watchdog)
        # Log the sanitized plan view, never the raw file (an extra unknown key must not be
        # echoed).
        log(f"{TOOL}: plan {json.dumps(sanitized_plan_document(plan), indent=2, default=_json_default)}")
        run = getattr(node, "run", None)
        if not callable(run):
            raise OndoProbeError("the node exposed no run()")
        lifecycle_result = run()
        if lifecycle_result is False:
            raise OndoProbeError(
                "the node run() returned False: the native lifecycle reported failure",
            )
        if watchdog_state["timed_out"]:
            stop_condition = "deadline"
            exit_code = EXIT_TIMEOUT
        else:
            stop_condition = "run-returned"
    except KeyboardInterrupt:
        stop_condition = "keyboard-interrupt"
        failure = "the run was interrupted by the operator (KeyboardInterrupt)"
        exit_code = EXIT_FAILURE
    except (TradeProbeRefused, OndoProbeRefused) as exc:
        stop_condition = "refused"
        failure = f"refused before a request: {exc}"
        exit_code = EXIT_REFUSED
        print(f"{TOOL}: refused: {exc}", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001 - reported, never swallowed
        stop_condition = "exception"
        failure = f"{type(exc).__name__}: {exc}"
        exit_code = EXIT_FAILURE
        log(f"{TOOL}: the session failed: {failure}")
        log(traceback.format_exc())
    finally:
        watchdog_cancel.set()
        watchdog_worker_alive = False
        if watchdog_slot:
            join_remaining = max(0.0, cleanup_deadline_monotonic - time.monotonic())
            watchdog_slot[0].join(min(0.5, join_remaining))
            watchdog_worker_alive = watchdog_slot[0].is_alive()
            if watchdog_state is not None:
                watchdog_state["worker_alive_after_join"] = watchdog_worker_alive
        outcome = bounded_stop(stop_target[0] if stop_target else None, label="final",
                               via=stop_via[0])
        stops.append(outcome)
        with handoff_lock:
            if readiness_slot:
                readiness = readiness_slot[-1]
            handed_reconciliation = stop_handoff.get("reconciliation")
            handed_failure = stop_handoff.get("failure")
            handed_stop = stop_handoff.get("stop")
        if isinstance(handed_stop, Mapping):
            stops.insert(0, dict(handed_stop))
        if watchdog_worker_alive:
            reconciliation = _unfinal(
                "the reconciliation watchdog remained active after the absolute cleanup "
                "deadline; no clean final snapshot is accepted",
            )
            handed_failure = (
                "reconciliation watchdog remained active after bounded cancellation/join"
            )
        elif isinstance(handed_reconciliation, FinalReconciliation):
            reconciliation = handed_reconciliation
        if handed_failure and strategy is not None:
            strategy._note_failure(str(handed_failure))
        if watchdog_state is not None and watchdog_state.get("stop_error") and strategy is not None:
            strategy._note_failure(
                f"watchdog stop failed: {watchdog_state['stop_error']}",
            )
        if strategy is not None:
            if strategy.sequencer.net_position != 0 or strategy.sequencer.unknown:
                if exit_code == EXIT_OK:
                    exit_code = EXIT_FAILURE
            if strategy.leftovers and exit_code == EXIT_OK:
                exit_code = EXIT_FAILURE
        diagnostics = read_native_diagnostics(native_target, run_id=run_id)
        if (
            reconciliation_is_clean(reconciliation) if reconciliation is not None else False
        ) and readiness is not None and readiness.generation is not None and (
            readiness.snapshot_unix_nanos is not None
        ) and reconciliation is not None and reconciliation.generation is not None:
            final = read_trade_final(
                native_target, run_id=run_id,
                instrument_id=plan.instrument.instrument_id,
                start_generation=readiness.generation,
                start_snapshot_unix_nanos=readiness.snapshot_unix_nanos,
                reconciliation=reconciliation,
            )
        else:
            final = _unfinal(
                "no clean pre-stop reconciliation existed, so final clean shutdown proof "
                "cannot be accepted",
            )
        if strategy is not None and strategy.sequencer.orders_submitted > 0:
            # Only a native FINAL clean snapshot certifies a writing run; a missing, stale,
            # residual, unknown or dirty final snapshot is a failure, not a clean account.
            if not final_is_clean(final) and exit_code == EXIT_OK:
                exit_code = EXIT_FAILURE
        if (
            strategy is not None
            and exit_code == EXIT_OK
            and not execution_result_is_success(
                strategy, plan, capability, readiness, final,
            )
        ):
            exit_code = EXIT_FAILURE
        finished = now()
        document = trade_report_document(
            run_id=run_id, started=started, finished=finished, exit_code=exit_code,
            failure=failure, plan=plan, limits=limits, capability=capability,
            readiness=readiness, strategy=strategy, stop_condition=stop_condition,
            stops=stops, watchdog=watchdog_state, diagnostics=diagnostics, final=final,
            reconciliation=reconciliation, dms_release_target=native_target,
        )
        meta = {
            "tool": TOOL,
            "schema_version": SCHEMA_VERSION,
            "venue": ONDO_VENUE,
            "mode": MODE_PRODUCTION_TRADE,
            "environment": "production",
            "started_at_utc": started.isoformat(timespec="milliseconds"),
            "finished_at_utc": finished.isoformat(timespec="milliseconds"),
            "exit_code": exit_code,
            "stop_condition": stop_condition,
            "failure": failure,
            "plan_sha256": plan.plan_sha256,
            "outcome": document.get("outcome"),
            "net_position": document.get("net_position"),
            "production_execution_verified": document.get("production_execution_verified"),
            "native_final_clean": final_is_clean(final),
            "native_write_capable": capability.supported,
            "fees": document.get("fees"),
        }
        try:
            manifest = publish_report(
                {TRADE_FILE: document}, out_dir, run_id=run_id,
                complete=exit_code == EXIT_OK and failure is None, meta=meta, failure=failure,
            )
            log(f"{TOOL}: published {out_dir / (TRADE_FILE + '.json')} "
                f"(run {manifest.get('run_id')}, complete={manifest.get('complete')})")
        except Exception as exc:  # noqa: BLE001 - a report that cannot be written is a failure
            print(f"{TOOL}: the report could not be written: {type(exc).__name__}: {exc}",
                  file=sys.stderr)
            if exit_code == EXIT_OK:
                exit_code = EXIT_FAILURE
    return exit_code


# ------------------------------------------------------------------------ main


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog=TOOL,
        description=(
            "Restricted Ondo Perps production-trade probe: one entry limit-IOC and at most "
            "two reduce-only closes for the confirmed quantity. A live run needs an approved "
            "plan, explicit acknowledgements and a native adapter that exposes the "
            "production write-envelope contract."
        ),
    )
    parser.add_argument("--mode", choices=MODES, default=None,
                        help=f"required; only {MODE_PRODUCTION_TRADE!r} is accepted")
    parser.add_argument("--side", choices=(SIDE_BUY, SIDE_SELL), default=None,
                        help="entry side; required unless --plan supplies it")
    parser.add_argument("--plan", default=None,
                        help="path to the approved plan JSON (required for a live run)")
    parser.add_argument("--limits", default=None,
                        help="dedicated [ondo_trade] limits file (default: config/limits.toml)")
    parser.add_argument("--confirm-production-trade", action="store_true",
                        help="acknowledge that this is a production write run")
    parser.add_argument("--acknowledge-mainnet-authorization", action="store_true",
                        help=(
                            "record that the user gave current-turn 上主网 authorization and "
                            "approved the exact plan; this tool cannot verify a conversation"
                        ))
    parser.add_argument("--minutes", type=float, default=None,
                        help="run deadline override in minutes (capped, and never above 600 s)")
    parser.add_argument("--out", default=DEFAULT_OUT, help=f"report directory (default: {DEFAULT_OUT})")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved intent and exit: no network, no credential, no client")
    return parser.parse_args(argv)


def _load_plan(path: str, limits: TradeLimits, *, require_authorization: bool = True) -> TradePlan:
    try:
        document = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise TradeProbeRefused(f"--plan {path!r} cannot be read as JSON: {exc}") from exc
    try:
        plan = parse_trade_plan(document, limits)
    except TradePlanError as exc:
        raise TradeProbeRefused(f"--plan {path!r} is refused: {exc}") from exc
    if require_authorization:
        problems = authorization_problems(plan)
        if problems:
            raise TradeProbeRefused("the plan is not approved: " + "; ".join(problems))
    return plan


def main(argv: Sequence[str] | None = None, *, adapter: OndoAdapter | None = None,
         node_factory: Callable[..., object] | None = None,
         environ: Mapping[str, str] | None = None,
         now: Callable[[], datetime] | None = None,
         capability: NativeTradeCapability | None = None) -> int:
    """Resolve, refuse, or run. Every refusal returns 2 having done nothing."""
    args = parse_args(argv)
    clock = now if now is not None else (lambda: datetime.now(timezone.utc))
    try:
        limits = load_trade_limits(args.limits)
    except TradeLimitsError as exc:
        print(f"{TOOL}: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED

    if args.mode != MODE_PRODUCTION_TRADE:
        print(
            f"{TOOL}: refused: --mode must be {MODE_PRODUCTION_TRADE!r}, got {args.mode!r}; "
            f"this tool has no read-only or sandbox path",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    active_adapter: OndoAdapter | None = None
    try:
        active_adapter = adapter if adapter is not None else load_adapter()
    except OndoProbeError as exc:
        if not args.dry_run:
            print(f"{TOOL}: refused: {exc}", file=sys.stderr)
            return EXIT_REFUSED
        # A dry run has no session behind it, so an absent adapter still prints its plan.
        active_adapter = None
    capability = capability if capability is not None else detect_native_trade_capability(
        active_adapter)

    if args.dry_run:
        if args.plan:
            try:
                plan = _load_plan(args.plan, limits, require_authorization=False)
            except TradeProbeRefused as exc:
                print(f"{TOOL}: refused: {exc}", file=sys.stderr)
                return EXIT_REFUSED
            document = sanitized_plan_document(plan)
            document["dry_run"] = True
            document["native_write_capable"] = capability.supported
            document["native_trade_capability"] = capability.as_dict()
            document["limits_configured"] = limits.limits_configured
            document["live_execution_ready"] = False
            document["live_execution_ready_reason"] = (
                "dry-run has no account/readiness/authorization evidence"
            )
            json.dump(document, sys.stdout, indent=2, sort_keys=True, ensure_ascii=False,
                      default=_json_default)
            sys.stdout.write("\n")
            return EXIT_OK
        if args.side is None:
            print(
                f"{TOOL}: refused: --dry-run without --plan needs --side "
                f"({SIDE_BUY!r} or {SIDE_SELL!r})",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        json.dump(intent_document(args, limits, capability), sys.stdout, indent=2,
                  sort_keys=True, ensure_ascii=False, default=_json_default)
        sys.stdout.write("\n")
        return EXIT_OK

    # Live path. Every refusal below happens before a credential is read or a client built.
    if not args.confirm_production_trade:
        print(f"{TOOL}: refused: a live production run requires --confirm-production-trade",
              file=sys.stderr)
        return EXIT_REFUSED
    if not args.acknowledge_mainnet_authorization:
        print(
            f"{TOOL}: refused: a live production run requires "
            f"--acknowledge-mainnet-authorization (the user's current-turn 上主网 authorization "
            f"and approval of the exact plan)",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if not args.plan:
        print(f"{TOOL}: refused: a live production run requires --plan", file=sys.stderr)
        return EXIT_REFUSED
    if not capability.supported:
        print(
            f"{TOOL}: refused: the installed adapter does not expose the production "
            f"write-envelope contract, so this layer must not write ({capability.source}: "
            f"{capability.reason}). No credential was read and no client was built.",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    try:
        plan = _load_plan(args.plan, limits)
    except TradeProbeRefused as exc:
        print(f"{TOOL}: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if args.side is not None and args.side != plan.entry.side:
        print(
            f"{TOOL}: refused: --side {args.side!r} does not match the approved plan's entry "
            f"side {plan.entry.side!r}; the plan is the authority and a differing flag is "
            f"not silently ignored",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    deadline_secs: int | None = None
    if args.minutes is not None:
        requested = int(round(args.minutes * 60.0))
        if requested <= 0:
            print(f"{TOOL}: refused: --minutes must be positive", file=sys.stderr)
            return EXIT_REFUSED
        if requested > plan.envelope.deadline_secs:
            print(
                f"{TOOL}: refused: --minutes {args.minutes:g} widens the frozen deadline "
                f"{plan.envelope.deadline_secs}s; a plan may not be widened at run time",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        if requested <= int(plan.cleanup["stop_budget_secs"]):
            print(
                f"{TOOL}: refused: --minutes leaves no bounded entry window before the "
                f"{plan.cleanup['stop_budget_secs']}s cleanup deadline",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        # A shorter deadline is honoured: it shortens the watchdog and the native envelope.
        deadline_secs = requested

    if not limits.limits_configured:
        print(
            f"{TOOL}: refused: live execution requires an explicit [ondo_trade] "
            f"safety table; missing keys: {', '.join(limits.missing_execution_keys)}. "
            f"Defaults may describe a dry-run proposal but cannot authorize a live client.",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    try:
        resolved = _load_environment(environ)
        check_credentials(resolved)
    except TradeProbeRefused as exc:
        print(f"{TOOL}: refused: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    except OndoProbeError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return EXIT_FAILURE

    def log(message: str) -> None:
        print(message)

    try:
        return execute(plan, limits, adapter=active_adapter, environ=resolved,
                       node_factory=node_factory, now=clock, out_dir=Path(args.out), log=log,
                       capability=capability,
                       deadline_secs=deadline_secs)
    except KeyboardInterrupt:
        print(f"{TOOL}: interrupted before the session could publish", file=sys.stderr)
        return EXIT_FAILURE
    except OndoProbeError as exc:
        print(f"{TOOL}: {exc}", file=sys.stderr)
        return EXIT_FAILURE


if __name__ == "__main__":
    raise SystemExit(main())

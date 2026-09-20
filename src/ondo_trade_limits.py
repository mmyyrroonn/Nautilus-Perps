#!/usr/bin/env python3
"""Dedicated limits for the Ondo production-trade probe.

This module owns the *user-tightened* envelope the trade probe is allowed to run with.  It
is deliberately separate from ``live_limits.py``: the maker limits there are about quoting
a book, and reusing ``[order]`` / ``[inventory]`` / ``[exposure]`` would silently inherit a
different strategy's defaults.  A production trade reads one dedicated table, ``[ondo_trade]``
in ``config/limits.toml`` (or a file named by ``--limits``), and **never** a maker table.

Three rules, applied here and nowhere else:

* every numeric key has a module-constant hard cap, and configuration may only *tighten* it
  (``applied = min(configured, cap)``) - no flag, no file and no environment variable can
  widen it;
* a value under a safety floor, a wrong type, or a value inconsistent with another key is a
  refusal, not a silent correction - "we could not read your limit" and "we decided to ignore
  it" are different answers;
* every resolved key is recorded in the ``configured / cap / applied`` three-way shape, so a
  reader can see which rail bound it instead of having to trust the loader.

The hard caps themselves are the trade plan's outer bound: 50 USD per order, 100 USD gross,
3 orders (1 entry + at most 2 reduce-only close attempts), 1 new-risk request, 6
app-originated create/cancel requests and a 600 s deadline. They are module constants, not
defaults. Native background REST traffic has its own transport budget; this app does not
advertise a total HTTP count it cannot observe.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path

# --------------------------------------------------------------------- hard caps

HARD_MAX_NOTIONAL_PER_ORDER_USD = Decimal("50")
HARD_MAX_GROSS_EXPOSURE_USD = Decimal("100")
HARD_MAX_ORDERS = 3
# Only ONE opening (new-risk) request per run.  The two reduce-only exits are separate,
# risk-reducing orders and are not new-risk requests; the correction round made this
# explicit after the first pass advertised three opening requests.
HARD_MAX_NEW_RISK_REQUESTS = 1
# The app's own cap on the write requests it can observe itself send: 1 entry + 2 closes +
# up to 3 cancels.  This is the count the sequencer enforces.
HARD_MAX_APP_REQUESTS = 6
HARD_MIN_AVAILABLE_MARGIN_USDC = Decimal("25")
HARD_MAX_RUN_SECS = 600
HARD_MAX_CLOSE_ATTEMPTS = 2
HARD_MAX_SLIPPAGE_BPS = Decimal("100")
HARD_MAX_CLEANUP_SECS = 15

# The intended entry band.  This is a *target*, not only a cap: an order below the venue's
# minimum notional cannot trade, so the loader refuses a target under 10 rather than sizing
# up to meet the floor at run time.
TARGET_NOTIONAL_MIN_USD = Decimal("10")
TARGET_NOTIONAL_MAX_USD = Decimal("20")

# Defaults are already inside every cap.  A missing section or a missing key falls back to
# these, so the loader can never make a run larger than its own defaults.
DEFAULT_ENTRY_NOTIONAL_USD = Decimal("15")
DEFAULT_MAX_NOTIONAL_PER_ORDER_USD = HARD_MAX_NOTIONAL_PER_ORDER_USD
DEFAULT_MAX_GROSS_EXPOSURE_USD = HARD_MAX_GROSS_EXPOSURE_USD
DEFAULT_MAX_ORDERS = HARD_MAX_ORDERS
DEFAULT_MAX_NEW_RISK_REQUESTS = HARD_MAX_NEW_RISK_REQUESTS
DEFAULT_MAX_APP_REQUESTS = HARD_MAX_APP_REQUESTS
DEFAULT_MIN_AVAILABLE_MARGIN_USDC = HARD_MIN_AVAILABLE_MARGIN_USDC
DEFAULT_DEADLINE_SECS = HARD_MAX_RUN_SECS
DEFAULT_CLOSE_ATTEMPTS = HARD_MAX_CLOSE_ATTEMPTS
DEFAULT_SLIPPAGE_BPS = Decimal("20")
DEFAULT_CLEANUP_BUDGET_SECS = 5

DEFAULT_INSTRUMENT = "NVDA-USD-PERP.ONDO"
DEFAULT_SYMBOL = "NVDA"
SECTION = "ondo_trade"
DEFAULT_LIMITS_PATH = "config/limits.toml"

REQUIRED_EXECUTION_KEYS = (
    "instrument",
    "symbol",
    "entry_notional_usd",
    "max_notional_per_order_usd",
    "max_gross_exposure_usd",
    "max_orders",
    "max_new_risk_requests",
    "max_app_requests",
    "min_available_margin_usdc",
    "deadline_secs",
    "max_close_attempts",
    "max_slippage_bps",
    "cleanup_budget_secs",
)

# A time-in-force the venue actually documents.  The probe never sends market orders and
# never sends anything but IOC, so only IOC is accepted.
TIME_IN_FORCE = "IOC"


class TradeLimitsError(ValueError):
    """The dedicated trade section exists but cannot produce a usable envelope."""


@dataclass(frozen=True)
class Row:
    """One resolved key: what configuration asked for, the hard rail, and what is used."""

    key: str
    configured: object
    cap: object
    applied: object
    floor: object | None = None

    @property
    def capped(self) -> bool:
        return self.cap is not None and self.configured != self.applied

    @staticmethod
    def _fmt(value: object) -> str:
        if isinstance(value, Decimal):
            return str(value)
        return str(value)

    def as_dict(self) -> dict[str, object]:
        return {
            "configured": self.configured,
            "cap": self.cap,
            "applied": self.applied,
            "floor": self.floor,
            "capped": self.capped,
        }

    def line(self) -> str:
        rail = self._fmt(self.cap) if self.cap is not None else "-"
        mark = "  <- CAPPED" if self.capped else ""
        return (
            f"  {self.key:<38} configured={self._fmt(self.configured):>10}  "
            f"cap={rail:>10}  applied={self._fmt(self.applied):>10}{mark}"
        )


@dataclass(frozen=True)
class TradeLimits:
    """The resolved dedicated trade envelope, with every rail already applied."""

    instrument: str = DEFAULT_INSTRUMENT
    symbol: str = DEFAULT_SYMBOL
    entry_notional_usd: Decimal = DEFAULT_ENTRY_NOTIONAL_USD
    max_notional_per_order_usd: Decimal = DEFAULT_MAX_NOTIONAL_PER_ORDER_USD
    max_gross_exposure_usd: Decimal = DEFAULT_MAX_GROSS_EXPOSURE_USD
    max_orders: int = DEFAULT_MAX_ORDERS
    max_new_risk_requests: int = DEFAULT_MAX_NEW_RISK_REQUESTS
    max_app_requests: int = DEFAULT_MAX_APP_REQUESTS
    min_available_margin_usdc: Decimal = DEFAULT_MIN_AVAILABLE_MARGIN_USDC
    deadline_secs: int = DEFAULT_DEADLINE_SECS
    max_close_attempts: int = DEFAULT_CLOSE_ATTEMPTS
    max_slippage_bps: Decimal = DEFAULT_SLIPPAGE_BPS
    cleanup_budget_secs: int = DEFAULT_CLEANUP_BUDGET_SECS
    source: str = "defaults"
    rows: tuple[Row, ...] = ()
    limits_configured: bool = False
    missing_execution_keys: tuple[str, ...] = REQUIRED_EXECUTION_KEYS

    def cap(self, key: str) -> Row:
        for row in self.rows:
            if row.key == key:
                return row
        raise TradeLimitsError(f"no resolved row named {key!r}")  # pragma: no cover

    def as_dict(self) -> dict[str, object]:
        return {
            "instrument": self.instrument,
            "symbol": self.symbol,
            "entry_notional_usd": str(self.entry_notional_usd),
            "max_notional_per_order_usd": str(self.max_notional_per_order_usd),
            "max_gross_exposure_usd": str(self.max_gross_exposure_usd),
            "max_orders": self.max_orders,
            "max_new_risk_requests": self.max_new_risk_requests,
            "max_app_requests": self.max_app_requests,
            "min_available_margin_usdc": str(self.min_available_margin_usdc),
            "deadline_secs": self.deadline_secs,
            "max_close_attempts": self.max_close_attempts,
            "max_slippage_bps": str(self.max_slippage_bps),
            "cleanup_budget_secs": self.cleanup_budget_secs,
            "time_in_force": TIME_IN_FORCE,
            "source": self.source,
            "limits_configured": self.limits_configured,
            "missing_execution_keys": list(self.missing_execution_keys),
        }


def _decimal(key: str, raw: object, default: Decimal) -> Decimal:
    if raw is None:  # a TOML value cannot be null, but the fallback keeps the default honest
        raw = default
    if isinstance(raw, Decimal):
        value = raw
    elif isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise TradeLimitsError(f"[{SECTION}] {key} must be a decimal string, got {raw!r}")
    elif isinstance(raw, float):
        # A float here is a configuration mistake: exact decimal arithmetic is the whole
        # point of this module, and ``0.1`` is not ``Decimal('0.1')``.  Refuse rather than
        # accept a value whose binary rounding we cannot see.
        raise TradeLimitsError(
            f"[{SECTION}] {key} must be a decimal string (quoted), not a float {raw!r}: "
            f"exact decimal arithmetic is required",
        )
    else:
        try:
            value = Decimal(str(raw))
        except (InvalidOperation, ValueError) as exc:
            raise TradeLimitsError(
                f"[{SECTION}] {key} is not a decimal: {raw!r} ({exc})",
            ) from exc
    if not value.is_finite():
        raise TradeLimitsError(f"[{SECTION}] {key} must be finite, got {value}")
    if value <= 0:
        raise TradeLimitsError(f"[{SECTION}] {key} must be greater than zero, got {value}")
    return value


def _integer(key: str, raw: object, default: int) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise TradeLimitsError(f"[{SECTION}] {key} must be an integer, got {raw!r}")
    if raw <= 0:
        raise TradeLimitsError(f"[{SECTION}] {key} must be greater than zero, got {raw}")
    return raw


def _capped(key: str, value: Decimal, cap: Decimal, rows: list[Row]) -> Decimal:
    applied = min(value, cap)
    rows.append(Row(key, value, cap, applied))
    return applied


def _capped_count(key: str, value: int, cap: int, rows: list[Row]) -> int:
    applied = min(value, cap)
    rows.append(Row(key, value, cap, applied))
    return applied


def _section(data: dict, path: Path) -> dict:
    block = data.get(SECTION)
    if block is None:
        return {}
    if not isinstance(block, dict):
        raise TradeLimitsError(f"[{SECTION}] in {path} is not a table")
    return block


def load_trade_limits(path: Path | str | None = None) -> TradeLimits:
    """Resolve the dedicated trade envelope from ``path``, or the safe defaults.

    A missing file, a file with no ``[ondo_trade]`` section, and a section with missing keys
    all fall back to the module defaults (already inside every cap).  A section that is
    present but malformed - a wrong type, a non-positive number, a target outside the 10-20
    band, or a cap below the target it must cover - is a refusal.  Only the ``[ondo_trade]``
    table is read; the maker tables are never consulted.
    """
    if path is None:
        path = Path(DEFAULT_LIMITS_PATH)
    path = Path(path)
    rows: list[Row] = []
    if not path.is_file():
        return TradeLimits(source=f"defaults ({path.as_posix()} has no [{SECTION}])", rows=())
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise TradeLimitsError(f"{path} cannot be parsed as TOML: {exc}") from exc
    if not isinstance(data, dict):  # pragma: no cover - tomllib always returns a dict
        raise TradeLimitsError(f"{path} did not parse to a table")

    block = _section(data, path)
    if not block:
        return TradeLimits(source=f"defaults ({path.as_posix()} has no [{SECTION}])", rows=())

    missing_execution_keys = tuple(key for key in REQUIRED_EXECUTION_KEYS if key not in block)

    symbol = block.get("symbol", DEFAULT_SYMBOL)
    if not isinstance(symbol, str) or not symbol.strip():
        raise TradeLimitsError(f"[{SECTION}] symbol must be a non-empty string")

    instrument = block.get("instrument", DEFAULT_INSTRUMENT)
    if not isinstance(instrument, str) or not instrument.strip():
        raise TradeLimitsError(f"[{SECTION}] instrument must be a non-empty string")

    entry = _decimal(
        "entry_notional_usd", block.get("entry_notional_usd", DEFAULT_ENTRY_NOTIONAL_USD),
        DEFAULT_ENTRY_NOTIONAL_USD,
    )
    if entry < TARGET_NOTIONAL_MIN_USD:
        raise TradeLimitsError(
            f"[{SECTION}] entry_notional_usd {entry} is below the venue-minimum target "
            f"{TARGET_NOTIONAL_MIN_USD}: the probe will not size up to meet a floor",
        )
    entry = _capped("entry_notional_usd", entry, TARGET_NOTIONAL_MAX_USD, rows)

    per_order_default = max(entry, DEFAULT_MAX_NOTIONAL_PER_ORDER_USD)
    per_order = _decimal(
        "max_notional_per_order_usd",
        block.get("max_notional_per_order_usd", per_order_default),
        per_order_default,
    )
    if per_order < entry:
        raise TradeLimitsError(
            f"[{SECTION}] max_notional_per_order_usd {per_order} is below the target entry "
            f"notional {entry}: the per-order rail must cover the intended order",
        )
    per_order = _capped(
        "max_notional_per_order_usd", per_order, HARD_MAX_NOTIONAL_PER_ORDER_USD, rows,
    )

    gross_default = max(entry, DEFAULT_MAX_GROSS_EXPOSURE_USD)
    gross = _decimal(
        "max_gross_exposure_usd",
        block.get("max_gross_exposure_usd", gross_default),
        gross_default,
    )
    if gross < entry:
        raise TradeLimitsError(
            f"[{SECTION}] max_gross_exposure_usd {gross} is below the target entry notional "
            f"{entry}",
        )
    gross = _capped("max_gross_exposure_usd", gross, HARD_MAX_GROSS_EXPOSURE_USD, rows)

    orders = _capped_count(
        "max_orders", _integer("max_orders", block.get("max_orders", DEFAULT_MAX_ORDERS),
                               DEFAULT_MAX_ORDERS),
        HARD_MAX_ORDERS, rows,
    )
    new_risk = _capped_count(
        "max_new_risk_requests",
        _integer("max_new_risk_requests",
                 block.get("max_new_risk_requests", DEFAULT_MAX_NEW_RISK_REQUESTS),
                 DEFAULT_MAX_NEW_RISK_REQUESTS),
        HARD_MAX_NEW_RISK_REQUESTS, rows,
    )
    app_requests = _capped_count(
        "max_app_requests",
        _integer("max_app_requests",
                 block.get("max_app_requests", DEFAULT_MAX_APP_REQUESTS),
                 DEFAULT_MAX_APP_REQUESTS),
        HARD_MAX_APP_REQUESTS, rows,
    )
    min_available_margin_usdc = _decimal(
        "min_available_margin_usdc",
        block.get("min_available_margin_usdc", DEFAULT_MIN_AVAILABLE_MARGIN_USDC),
        DEFAULT_MIN_AVAILABLE_MARGIN_USDC,
    )
    if min_available_margin_usdc < HARD_MIN_AVAILABLE_MARGIN_USDC:
        raise TradeLimitsError(
            f"[{SECTION}] min_available_margin_usdc {min_available_margin_usdc} is below "
            f"the hard safety floor {HARD_MIN_AVAILABLE_MARGIN_USDC}",
        )
    rows.append(Row(
        "min_available_margin_usdc",
        min_available_margin_usdc,
        None,
        min_available_margin_usdc,
        floor=HARD_MIN_AVAILABLE_MARGIN_USDC,
    ))
    deadline = _capped_count(
        "deadline_secs",
        _integer("deadline_secs", block.get("deadline_secs", DEFAULT_DEADLINE_SECS),
                 DEFAULT_DEADLINE_SECS),
        HARD_MAX_RUN_SECS, rows,
    )
    close_attempts = _capped_count(
        "max_close_attempts",
        _integer("max_close_attempts",
                 block.get("max_close_attempts", DEFAULT_CLOSE_ATTEMPTS),
                 DEFAULT_CLOSE_ATTEMPTS),
        HARD_MAX_CLOSE_ATTEMPTS, rows,
    )
    slippage = _decimal(
        "max_slippage_bps", block.get("max_slippage_bps", DEFAULT_SLIPPAGE_BPS),
        DEFAULT_SLIPPAGE_BPS,
    )
    slippage = _capped("max_slippage_bps", slippage, HARD_MAX_SLIPPAGE_BPS, rows)
    cleanup = _capped_count(
        "cleanup_budget_secs",
        _integer("cleanup_budget_secs",
                 block.get("cleanup_budget_secs", DEFAULT_CLEANUP_BUDGET_SECS),
                 DEFAULT_CLEANUP_BUDGET_SECS),
        HARD_MAX_CLEANUP_SECS, rows,
    )

    if orders < 1 + close_attempts:
        # One entry plus the close attempts must fit in the order budget; otherwise the plan
        # cannot even attempt its own flatten, which would leave a position by construction.
        raise TradeLimitsError(
            f"[{SECTION}] max_orders {orders} is below 1 entry + max_close_attempts "
            f"{close_attempts}: the envelope must permit the bounded flatten it promises",
        )
    if app_requests < 1 + close_attempts:
        raise TradeLimitsError(
            f"[{SECTION}] max_app_requests {app_requests} is below 1 entry + "
            f"max_close_attempts {close_attempts}",
        )
    if new_risk != 1:
        raise TradeLimitsError(
            f"[{SECTION}] max_new_risk_requests must be exactly 1: one opening attempt only; "
            f"reduce-only exits are not new-risk requests, got {new_risk}",
        )

    return TradeLimits(
        instrument=instrument.strip(),
        symbol=symbol.strip().upper(),
        entry_notional_usd=entry,
        max_notional_per_order_usd=per_order,
        max_gross_exposure_usd=gross,
        max_orders=orders,
        max_new_risk_requests=new_risk,
        max_app_requests=app_requests,
        min_available_margin_usdc=min_available_margin_usdc,
        deadline_secs=deadline,
        max_close_attempts=close_attempts,
        max_slippage_bps=slippage,
        cleanup_budget_secs=cleanup,
        source=path.as_posix(),
        rows=tuple(rows),
        limits_configured=not missing_execution_keys,
        missing_execution_keys=missing_execution_keys,
    )

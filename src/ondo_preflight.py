#!/usr/bin/env python3
"""Ondo Perps public metadata / status / fee preflight (plan 4.1, task 3).

    src/ondo_preflight.py --symbols NVDA,TSLA --out <dir>

The CLI reads the venue's *public* surface and writes evidence to ``<dir>``:

    <dir>/status.json       GET /status, as delivered
    <dir>/markets.json      GET /v1/markets, as delivered (exact decimal strings)
    <dir>/contracts.json    GET /v1/contracts, as delivered
    <dir>/instruments.json  the normalized target instrument table
    <dir>/missing.json      every missing/unknown field of a requested target
    <dir>/meta.json         this attempt's identity, status and sha256 manifest
    <dir>/runs/<run_id>/    the immutable copy of that attempt, payloads and meta alike

Every attempt publishes under its own ``run_id``: the files are staged in a run-scoped
temporary directory, that directory is renamed into ``runs/`` in one step, and only then
is ``<dir>/meta.json`` replaced - atomically, via a temporary file - as the last act of
publishing. Two things follow, and both are the point (F12):

* ``meta.json`` always describes the **current** attempt. A failed preflight writes
  ``complete: false`` with its failure reason, so a previous run's ``complete: true`` can
  never be left standing as the state of a run that just failed; and
* a reader checks the run it is reading with :func:`verify_run`, which recomputes every
  sha256 the manifest names. An interruption between publishing the payloads and
  publishing ``meta.json`` therefore shows up as a hash mismatch rather than as a tidy lie.

A payload a run did not produce is removed from ``<dir>``, so the published view is
exactly one attempt. Those five names are this tool's own artifacts and nothing else is
ever touched.

Two hard rules from plan 4.1:

* the venue calls go through ``OndoHttpClient`` from the candidate adapter - never
  a second hand-rolled HTTP client, and never through a credential;
* any failed call, or any missing field of a requested target, exits non-zero. A
  403 (or any error body) is a failure, never an empty market list reported as
  success.

One field is deliberately *not* "missing when absent": the market status. The real
2026-09-14 ``/v1/markets`` capture carries no ``status`` string on any of its 81
markets - 20 carry ``"disabled": true`` and the other 61 omit the key - so a target
with neither is **enabled by the venue convention**, the same convention the fork
adapter was ruled onto. The verdict is recorded with that source (never as a venue
assertion). What still fails closed: an *unrecognised* status value, a non-boolean
``disabled``, or a target market absent from the response.

It reads no ``.env`` file and loads no key: the public surface needs neither, and
``OndoHttpClient`` has no credential parameter to load one into.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import inspect
import json
import os
import re
import shutil
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from spread_watch import _adapter_missing  # noqa: E402

ONDO_ADAPTER = "nautilus_trader.adapters.ondo"
ONDO_VENUE = "ONDO"
DEFAULT_SYMBOLS = "NVDA,TSLA"
ALL_SYMBOLS = "ALL"
SYMBOL_RE = re.compile(r"[A-Z0-9][A-Z0-9._-]*")
USD_PERP_RE = re.compile(r"([A-Z0-9][A-Z0-9._-]*)-USD\.P")
SCHEMA_VERSION = 1
STATUS_FILE = "status"
MARKET_FILE = "markets"
CONTRACT_FILE = "contracts"
INSTRUMENT_FILE = "instruments"
MISSING_FILE = "missing"
META_FILE = "meta"
PAYLOAD_FILES = (STATUS_FILE, MARKET_FILE, CONTRACT_FILE, INSTRUMENT_FILE, MISSING_FILE)
RUNS_DIRNAME = "runs"  # one immutable directory per run id, under the output directory
STAGING_PREFIX = ".staging-"  # the run-scoped temporary directory publishing starts from

# The pointer printed when this venv has no ondo adapter: enough to build and
# install the candidate wheel without guessing (plan 9).
BUILD_POINTER = (
    "build the candidate wheel from the nautilus_trader fork (BUILD_WINDOWS.md, "
    "cargo test -p nautilus-ondo then maturin build --release) and install it into "
    "this repo's venv: uv pip install --python "
    "E:\\Nautilus-Perps\\.venv\\Scripts\\python.exe --reinstall <candidate .whl>"
)

# plan 4.1: sizeIncrement is the quantity step and quoteIncrement the price step -
# never interchangeable. The fee/status names are the documented candidates the
# preflight probes for; when the real response uses another name the field counts
# as missing and the run fails, which is exactly the signal wanted rather than a
# guessed mapping. P0's fixture is what will pin them for good.
SIZE_FIELDS = ("baseIncrement",)
PRICE_FIELDS = ("quoteIncrement",)
BASE_FIELDS = ("base", "baseCurrency", "baseAsset")
TAKER_FEE_FIELDS = ("takerFeeRate", "takerFee", "taker_fee", "takerFeeBps", "taker_fee_bps")
MAKER_FEE_FIELDS = ("makerFeeRate", "makerFee", "maker_fee", "makerFeeBps", "maker_fee_bps")
STATUS_FIELDS = ("status", "state", "tradingStatus", "marketStatus")
# The venue's own enabled/disabled key (plan 4.1). Unlike the candidate status
# names above this one is not a probe: the 2026-09-14 capture pins it, see below.
DISABLED_FIELDS = ("disabled",)

ACTIVE_STATUS_VALUES = frozenset({"active", "trading", "open", "online", "enabled", "live"})
DISABLED_STATUS_VALUES = frozenset({
    "disabled", "halted", "closed", "suspended", "offline", "paused", "inactive",
})

# The venue convention, pinned by the real 2026-09-14 /v1/markets capture (81
# markets): no entry carries a `status` string at all, 20 carry `"disabled": true`
# and the other 61 omit the key entirely. `disabled` is therefore emitted only for
# a disabled market, and its absence is the venue's way of saying "enabled". The
# fork adapter was ruled onto exactly this convention; the preflight follows it so
# a target with no status string is *enabled by convention*, never a missing item.
STATUS_CONVENTION_RULE = (
    "disabled present-and-true => disabled; disabled absent => enabled by the venue "
    "convention; a status string, if one ever appears, is mapped through the adapter's rule"
)
# Recorded with every run: a reader must be able to see that the "active" verdict
# rests on this convention plus its evidence, not on a venue assertion the venue
# never made. ``0/81`` and ``20/81`` are the counts from the pinned capture.
STATUS_CONVENTION_SOURCE = (
    "venue_convention:enabled - no status string and no `disabled` flag present; by the "
    "venue convention absence of `disabled` means enabled (2026-09-14 capture: 0/81 markets "
    "carried a status string, 20/81 carried disabled:true)"
)
STATUS_CAPTURE_EVIDENCE = {
    "captured": "2026-09-14",
    "source": "reports/ondo-acceptance/20260914T142730Z-p1-build/rest-capture/markets.json",
    "markets": 81,
    "with_status_string": 0,
    "with_disabled_true": 20,
}


class OndoPreflightError(RuntimeError):
    """A preflight precondition failed: the caller must exit non-zero."""


class AdapterMissing(RuntimeError):
    """The installed nautilus_trader wheel has no ondo adapter."""


@dataclass(frozen=True)
class OndoAdapter:
    """The two adapter classes the preflight uses (the rest are out of scope)."""

    OndoEnvironment: object
    OndoHttpClient: object


# ------------------------------------------------------------------ adapter load


def load_adapter() -> OndoAdapter:
    """Import the adapter's public classes, or say clearly that they are absent.

    Imported only when the call is made - never at module import time - so the
    offline test suite collects whether or not the candidate wheel is installed.
    """
    try:
        from nautilus_trader.adapters.ondo import OndoEnvironment, OndoHttpClient
    except ImportError as exc:
        raise AdapterMissing(_adapter_missing("ONDO", ONDO_ADAPTER)) from exc
    return OndoAdapter(OndoEnvironment=OndoEnvironment, OndoHttpClient=OndoHttpClient)


# ----------------------------------------------------------------------- targets


def targets(symbols: Sequence[str]) -> list[dict[str, str]]:
    """Derive explicit CLI symbols without maintaining a venue-market allowlist.

    The derived target is still verified against the venue's public market response and
    the adapter's normalized instrument definitions before a preflight can pass. ``ALL``
    is different: it needs the live market response and is resolved by
    :func:`discover_enabled_targets`.
    """
    if not symbols:
        raise OndoPreflightError("no symbols requested")
    normalized = [str(symbol).strip().upper() for symbol in symbols]
    if ALL_SYMBOLS in normalized:
        if normalized != [ALL_SYMBOLS]:
            raise OndoPreflightError("ALL cannot be combined with explicit symbols")
        return []
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for symbol in normalized:
        if not SYMBOL_RE.fullmatch(symbol):
            raise OndoPreflightError(f"{symbol!r} is not a valid Ondo symbol")
        if symbol in seen:
            continue
        seen.add(symbol)
        rows.append({
            "symbol": symbol,
            "raw_market": f"{symbol}-USD.P",
            "instrument_id": f"{symbol}-USD-PERP.ONDO",
            "venue": ONDO_VENUE,
            "client_id": ONDO_VENUE,
        })
    return rows


def discover_enabled_targets(markets: object) -> list[dict[str, str]]:
    """Return every enabled USD perpetual advertised by the venue, in venue order."""
    rows: list[dict[str, str]] = []
    seen: set[str] = set()
    for entry in market_entries(markets):
        raw = entry.get("market")
        match = USD_PERP_RE.fullmatch(raw) if isinstance(raw, str) else None
        if match is None or market_status(entry)["status"] != "active":
            continue
        symbol = match.group(1)
        if symbol in seen:
            continue
        seen.add(symbol)
        rows.extend(targets([symbol]))
    if not rows:
        raise OndoPreflightError("venue returned no enabled USD perpetual markets")
    return rows


def discovery_summary(markets: object) -> dict[str, int]:
    """Count discoverable USD perpetuals by resolved market state."""
    counts = {"total": 0, "enabled": 0, "disabled": 0, "unknown": 0}
    for entry in market_entries(markets):
        raw = entry.get("market")
        if not isinstance(raw, str) or USD_PERP_RE.fullmatch(raw) is None:
            continue
        counts["total"] += 1
        state = str(market_status(entry)["status"])
        counts["enabled" if state == "active" else state] += 1
    return counts


def describe(symbols: Sequence[str], args) -> dict[str, object]:
    """The --dry-run document: what would be read, and from where."""
    rows = targets(symbols)
    return {
        "mode": "dry-run",
        "tool": "ondo_preflight",
        "schema_version": SCHEMA_VERSION,
        "venue": ONDO_VENUE,
        "environment": args.environment,
        "timeout_secs": args.timeout_secs,
        "symbols": [row["symbol"] for row in rows],
        "requested_symbols": [str(symbol).upper() for symbol in symbols],
        "selector": "all-enabled" if not rows and list(symbols) == [ALL_SYMBOLS] else "explicit",
        "targets": rows,
        "load_ids": [row["instrument_id"] for row in rows],
        "out": str(args.out),
        "network": "not contacted in --dry-run",
        "env_file": "never read",
    }


# --------------------------------------------------------------------- fetching


async def _resolve(value):
    """Await the adapter's coroutine, or pass a plain value through.

    The generated stub prints ``OndoHttpClient``'s four methods as sync ``def``
    although they are async at runtime (Stage 3a report, A.7), so both shapes must
    work: the real client is awaited, a test double may simply return.
    """
    if inspect.isawaitable(value):
        return await value
    return value


async def collect(client, rows: Sequence[dict[str, str]]) -> dict[str, object]:
    """Call the four public reads once each, in order, and never swallow a failure."""
    load_ids = [row["instrument_id"] for row in rows]
    payloads: dict[str, object] = {}
    calls = (
        (STATUS_FILE, "get_status", lambda: client.get_status()),
        (MARKET_FILE, "get_markets", lambda: client.get_markets()),
        (CONTRACT_FILE, "get_contracts", lambda: client.get_contracts()),
        (INSTRUMENT_FILE, "load_instrument_definitions",
         lambda: client.load_instrument_definitions(load_ids)),
    )
    for name, method, call in calls:
        try:
            payloads[name] = await _resolve(call())
        except OndoPreflightError:
            raise
        except Exception as exc:  # noqa: BLE001 - any transport/schema fault is fatal
            raise OndoPreflightError(
                f"{method} failed: {exc.__class__.__name__}: {exc} - refusing to write "
                f"an empty {name} result as if the venue had none",
            ) from exc
    return payloads


async def collect_all(client) -> tuple[dict[str, object], list[dict[str, str]]]:
    """Discover enabled markets first, then load exactly their normalized instruments."""
    payloads: dict[str, object] = {}
    for name, method, call in (
        (STATUS_FILE, "get_status", lambda: client.get_status()),
        (MARKET_FILE, "get_markets", lambda: client.get_markets()),
        (CONTRACT_FILE, "get_contracts", lambda: client.get_contracts()),
    ):
        try:
            payloads[name] = await _resolve(call())
        except Exception as exc:  # noqa: BLE001 - any transport/schema fault is fatal
            raise OndoPreflightError(
                f"{method} failed: {exc.__class__.__name__}: {exc} - refusing to write "
                f"an empty {name} result as if the venue had none",
            ) from exc
    rows = discover_enabled_targets(payloads[MARKET_FILE])
    try:
        payloads[INSTRUMENT_FILE] = await _resolve(client.load_instrument_definitions(
            [row["instrument_id"] for row in rows],
        ))
    except Exception as exc:  # noqa: BLE001 - public adapter failure is fatal
        raise OndoPreflightError(
            f"load_instrument_definitions failed: {exc.__class__.__name__}: {exc} - refusing "
            "to publish an incomplete dynamic market set",
        ) from exc
    return payloads, rows


# ------------------------------------------------------------------------ schema


def _field(entry: dict, names: Sequence[str]) -> tuple[str | None, object]:
    """First present, non-empty field of ``entry`` among ``names``."""
    for name in names:
        if name in entry and entry[name] not in (None, "", []):
            return name, entry[name]
    return None, None


def _decimal(value: object) -> Decimal | None:
    try:
        dec = Decimal(str(value).strip())
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return dec if dec.is_finite() else None


def _dec_text(value: Decimal) -> str:
    """Exact decimal text, no scientific notation and no f64 round trip."""
    return format(value.normalize(), "f")


def _fee_bps(value: object, field_name: str) -> Decimal | None:
    """A fee field as bps. A name ending in 'bps' is already bps, else a rate."""
    dec = _decimal(value)
    if dec is None or dec < 0:
        return None
    if field_name.endswith("bps"):
        return dec
    return dec * Decimal(10_000)


def _normalize_status(value: object) -> str:
    if value is None:
        return "unknown"
    if isinstance(value, bool):
        return "active" if value else "disabled"
    text = str(value).strip().lower()
    if text in ACTIVE_STATUS_VALUES:
        return "active"
    if text in DISABLED_STATUS_VALUES:
        return "disabled"
    return "unknown"


def market_status(entry: dict) -> dict[str, object]:
    """Resolve one market entry to active/disabled/unknown, with its explicit source.

    This is the venue convention the fork adapter was ruled onto, applied here so a
    target with no status string is not miscounted as a missing field:

    * a ``status`` string (if one ever appears) is mapped through the adapter's rule;
      an *unrecognised* value is the one status case that still fails closed;
    * otherwise ``disabled`` present-and-true => disabled, present-and-false => active,
      a non-boolean ``disabled`` => fail closed (malformed), and
    * ``disabled`` absent and no status string => **active by convention**, with the
      absence itself recorded as the source so it is never read as a verified venue
      assertion (2026-09-14 capture: 0/81 markets had a status string, 20/81 had
      ``disabled: true``).

    Returns ``status`` (active/disabled/unknown), ``raw`` (the signal that drove it,
    or ``None`` for the convention), ``source`` (a string that names the evidence) and
    ``reason`` (a fail-closed explanation when ``status`` is ``unknown``).
    """
    status_field, status_value = _field(entry, STATUS_FIELDS)
    if status_field is not None:
        state = _normalize_status(status_value)
        reason = None
        if state == "unknown":
            reason = (
                f"status {status_value!r} from {status_field} maps to no known state; "
                "not tradable until known"
            )
        return {
            "status": state,
            "raw": str(status_value),
            "source": f"market_fields:{status_field}",
            "reason": reason,
        }

    disabled_field, _ = _field(entry, DISABLED_FIELDS)
    if disabled_field is not None:
        value = entry[disabled_field]
        if not isinstance(value, bool):
            return {
                "status": "unknown",
                "raw": value,
                "source": f"market_fields:{disabled_field}",
                "reason": (
                    f"`{disabled_field}` is {value!r} ({type(value).__name__}), not a boolean; "
                    "cannot tell enabled from disabled"
                ),
            }
        return {
            "status": "disabled" if value else "active",
            "raw": value,
            "source": f"market_fields:{disabled_field}",
            "reason": None,
        }

    return {
        "status": "active",
        "raw": None,
        "source": STATUS_CONVENTION_SOURCE,
        "reason": None,
    }


def status_convention_evidence(entries: Sequence[dict]) -> dict[str, object]:
    """The venue-level counts behind the per-market source, computed from *this* response."""
    markets = [entry for entry in entries if isinstance(entry, dict)]
    return {
        "rule": STATUS_CONVENTION_RULE,
        "markets": len(markets),
        "with_status_string": sum(1 for e in markets if _field(e, STATUS_FIELDS)[0] is not None),
        "with_disabled_flag": sum(1 for e in markets if _field(e, DISABLED_FIELDS)[0] is not None),
        "disabled_true": sum(1 for e in markets if e.get("disabled") is True),
        "disabled_false": sum(1 for e in markets if e.get("disabled") is False),
        "pinned_capture_2026_09_14": STATUS_CAPTURE_EVIDENCE,
    }


def market_entries(markets: object) -> list[dict]:
    """``/v1/markets`` -> its ``perps.tradingPairs`` list (plan 4.1).

    A missing path is a *failure*, not an empty market list: that is how a 403 or
    an error body must be distinguished from a venue that genuinely lists nothing.
    """
    if not isinstance(markets, dict):
        raise OndoPreflightError(f"markets response is {type(markets).__name__}, not an object")
    perps = markets.get("perps")
    if not isinstance(perps, dict):
        raise OndoPreflightError(
            "markets response has no 'perps' object - an error body is not an empty market list",
        )
    pairs = perps.get("tradingPairs")
    if not isinstance(pairs, list):
        raise OndoPreflightError("markets response has no 'perps.tradingPairs' list")
    return [entry for entry in pairs if isinstance(entry, dict)]


def _by_market(entries: Sequence[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for entry in entries:
        name = entry.get("market")
        if isinstance(name, str) and name:
            out.setdefault(name, entry)
    return out


def _returned_instrument_ids(instruments: object) -> set[str]:
    if not isinstance(instruments, list):
        raise OndoPreflightError(
            f"load_instrument_definitions returned {type(instruments).__name__}, expected a list",
        )
    ids: set[str] = set()
    for item in instruments:
        if isinstance(item, dict):  # a plain stand-in for a Nautilus instrument
            value = item.get("id") or item.get("instrument_id")
        else:
            value = getattr(item, "id", None) or getattr(item, "instrument_id", None)
        if value is not None:
            ids.add(str(value))
        elif isinstance(item, str):
            ids.add(item)
    return ids


def _miss(symbol: str, market: str, field: str, reason: str, value: object = None) -> dict:
    return {"symbol": symbol, "market": market, "field": field, "reason": reason, "value": value}


def normalize(payloads: dict[str, object], rows: Sequence[dict[str, str]]) -> tuple[list[dict], list[dict]]:
    """Build the normalized instrument table plus the missing/unknown item list."""
    entries = market_entries(payloads.get(MARKET_FILE))
    by_market = _by_market(entries)
    contracts = payloads.get(CONTRACT_FILE) or []
    if not isinstance(contracts, list):
        raise OndoPreflightError(
            f"contracts response is {type(contracts).__name__}, expected a list",
        )
    by_contract = _by_market([entry for entry in contracts if isinstance(entry, dict)])
    instrument_ids = _returned_instrument_ids(payloads.get(INSTRUMENT_FILE))

    table: list[dict] = []
    missing: list[dict] = []
    for target in rows:
        symbol, raw = target["symbol"], target["raw_market"]
        row: dict[str, object] = {
            "symbol": symbol,
            "raw_market": raw,
            "instrument_id": target["instrument_id"],
            "venue": ONDO_VENUE,
            "quote_currency": "USD",  # plan 4.1: USD-quoted, USDC-settled, linear,
            "settlement_currency": "USDC",  # quantity is base, never inverse
            "is_inverse": False,
            "base_currency": None,
            "base_currency_source": None,
            "size_increment": None,
            "price_increment": None,
            "taker_fee_bps": None,
            "maker_fee_bps": None,
            "fee_source": "missing",
            "maker_fee_source": "missing",
            "market_status": "unknown",
            "market_status_raw": None,
            "market_status_source": None,
        }
        entry = by_market.get(raw)
        if entry is None:
            missing.append(_miss(
                symbol, raw, "market",
                f"no perps.tradingPairs entry for {raw} (venue returned {len(entries)} markets)",
            ))
            table.append(row)
            if target["instrument_id"] not in instrument_ids:
                missing.append(_miss(
                    symbol, raw, "instrument",
                    f"load_instrument_definitions did not return {target['instrument_id']}",
                ))
            continue

        base_field, base_value = _field(entry, BASE_FIELDS)
        row["base_currency"] = str(base_value) if base_field else raw.split("-")[0]
        row["base_currency_source"] = f"market_fields:{base_field}" if base_field else "raw_market_name"

        for key, names, label in (
            ("size_increment", SIZE_FIELDS, "baseIncrement (quantity step)"),
            ("price_increment", PRICE_FIELDS, "quoteIncrement (price step)"),
        ):
            field_name, value = _field(entry, names)
            dec = _decimal(value) if field_name else None
            if field_name is None or dec is None or dec <= 0:
                missing.append(_miss(
                    symbol, raw, names[0],
                    f"{label} is absent or not a positive decimal: {value!r}",
                    value,
                ))
            else:
                row[key] = _dec_text(dec)

        status = market_status(entry)
        row["market_status"] = status["status"]
        row["market_status_raw"] = status["raw"]
        row["market_status_source"] = status["source"]
        if status["status"] == "unknown":
            missing.append(_miss(
                symbol, raw, "market_status", status["reason"], status["raw"],
            ))

        # Fee source precedence inside the public metadata: the market entry first
        # (plan 4.1 says the real response is richer than the schema), the contract
        # entry as the fallback. The account rate is the rung above both and is not
        # readable without a private feed.
        fee_field, fee_value = _field(entry, TAKER_FEE_FIELDS)
        fee_origin = "market_fields" if fee_field else None
        if fee_field is None:
            fee_field, fee_value = _field(by_contract.get(raw, {}), TAKER_FEE_FIELDS)
            fee_origin = "contract_fields" if fee_field else None
        if fee_field is None:
            missing.append(_miss(
                symbol, raw, "taker_fee",
                f"no taker fee among {list(TAKER_FEE_FIELDS)} in the market or contract entry",
            ))
        else:
            dec = _fee_bps(fee_value, fee_field)
            if dec is None:
                missing.append(_miss(
                    symbol, raw, "taker_fee",
                    f"taker fee {fee_value!r} from {fee_field} is not a usable rate", fee_value,
                ))
            else:
                row["taker_fee_bps"] = _dec_text(dec)
                row["fee_source"] = f"{fee_origin}:{fee_field}"

        maker_field, maker_value = _field(entry, MAKER_FEE_FIELDS)
        maker_origin = "market_fields" if maker_field else None
        if maker_field is None:
            maker_field, maker_value = _field(by_contract.get(raw, {}), MAKER_FEE_FIELDS)
            maker_origin = "contract_fields" if maker_field else None
        if maker_field is not None:
            dec = _fee_bps(maker_value, maker_field)
            if dec is not None:
                row["maker_fee_bps"] = _dec_text(dec)
                row["maker_fee_source"] = f"{maker_origin}:{maker_field}"
        # A missing maker fee is recorded but not fatal: this stage is a taker-leg
        # observation, and the venue is free not to publish a maker rate.

        if target["instrument_id"] not in instrument_ids:
            missing.append(_miss(
                symbol, raw, "instrument",
                f"load_instrument_definitions did not return {target['instrument_id']}",
            ))
        table.append(row)
    return table, missing


def build_report(payloads: dict[str, object], symbols: Sequence[str]) -> dict[str, object]:
    """Assemble the report body from the raw payloads (no I/O, no clock)."""
    rows = targets(symbols)
    return build_report_for_rows(payloads, rows)


def build_report_for_rows(payloads: dict[str, object], rows: Sequence[dict[str, str]]) -> dict[str, object]:
    """Assemble a report for already resolved explicit or discovered targets."""
    table, missing = normalize(payloads, rows)
    return {
        STATUS_FILE: payloads.get(STATUS_FILE),
        MARKET_FILE: payloads.get(MARKET_FILE),
        CONTRACT_FILE: payloads.get(CONTRACT_FILE),
        INSTRUMENT_FILE: table,
        MISSING_FILE: missing,
    }


# ---------------------------------------------------------------------- writing


def _write_json(path: Path, payload: object) -> dict[str, object]:
    """Write one JSON document atomically (temporary file + replace) and describe it."""
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    data = text.encode("utf-8")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return {
        "path": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def new_run_id(moment: datetime) -> str:
    """A run id that is unique per attempt and sorts by when it started."""
    return moment.strftime("%Y%m%dT%H%M%S%fZ")


def _publish_file(source: Path, target: Path) -> dict[str, object]:
    """Copy one published file into place atomically, and describe what landed."""
    data = source.read_bytes()
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, target)
    return {
        "path": target.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def write_report(report: dict[str, object], out_dir: Path, *, run_id: str, complete: bool,
                 meta: dict[str, object], failure: str | None = None) -> dict[str, object]:
    """Publish one attempt: stage it under its run id, then switch the published view.

    ``report`` holds the payloads this attempt produced - empty for an attempt that could
    not read the venue at all (a transport failure produces no evidence and none is
    invented for it). Every attempt, successful or not, gets a ``meta.json`` naming its own
    run id, its status and the hashes of what it wrote; the published ``<out>/meta.json``
    is replaced last, so it always describes the attempt that just finished (F12).
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
        if name in report:
            files[name] = _write_json(staging / f"{name}.json", report[name])
    manifest["files"] = files
    # Always present, so a reader finds the verdict it is looking for in one document: []
    # for a complete run or one that never read anything, the items for an incomplete one.
    manifest["missing"] = list(report.get(MISSING_FILE) or [])
    _write_json(staging / f"{META_FILE}.json", manifest)

    # The run directory is the immutable record of this attempt: one rename publishes all
    # of it at once, under a name no other attempt can hold.
    final_dir = runs_dir / run_id
    if final_dir.exists():
        shutil.rmtree(final_dir)
    os.replace(staging, final_dir)

    # Then the published view: the payloads of *this* attempt, with any payload this
    # attempt did not produce removed, and meta.json replaced last as the commit point.
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

    A reader must never take ``complete: true`` on trust: this recomputes each sha256 and
    each byte count, confirms every named file is present, and reports the run id it
    verified. A publishing that was interrupted between its payloads and its ``meta.json``
    therefore fails verification instead of reading as a success.
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
    # The identity is checked, not just reported: a manifest carries both the run id and
    # the run directory that id belongs to, and they have to name the same attempt. A
    # manifest whose two halves disagree is not from one run, so no reader should be told
    # the run id it claims (F12: "校验 run identity"). Only the manifest's *own* halves
    # are compared - the published view stays readable when it is copied on its own,
    # without the runs/ tree beside it.
    run_dir = manifest.get("run_dir")
    if result["run_id"] and isinstance(run_dir, str):
        expected = f"{RUNS_DIRNAME}/{result['run_id']}"
        if run_dir != expected:
            problems.append(
                f"{meta_path} names run id {result['run_id']!r} but the run directory "
                f"{run_dir!r}: the manifest's identity does not agree with itself "
                f"(expected {expected!r})",
            )
    if not result["complete"]:
        problems.append(
            f"the published run {result['run_id']} is not complete: "
            f"{manifest.get('failure') or manifest.get('missing') or 'no reason recorded'}",
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
                    f"{name}.json does not match the manifest's sha256: the published "
                    f"files and the manifest are not from the same run",
                )
            elif entry.get("bytes") != len(data):
                problems.append(f"{name}.json is {len(data)} bytes, the manifest says "
                                f"{entry.get('bytes')}")
    result["verified"] = not problems
    return result


# -------------------------------------------------------------------------- run


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run(symbols: Sequence[str], out_dir: Path, *, environment: str = "production",
        timeout_secs: int = 15, client=None, run_id: str | None = None) -> int:
    """Read the public surface once and publish the evidence. 0 iff every target passed.

    Every attempt is published under its own run id, a failed one included: an attempt
    that could not read the venue still has a status, and leaving the previous run's
    success in place would report that failed attempt as the current state (F12).
    """
    requested = [str(symbol).strip().upper() for symbol in symbols]
    dynamic = requested == [ALL_SYMBOLS]
    rows = targets(requested)  # explicit validation; ALL intentionally resolves to []
    if client is None:
        adapter = load_adapter()
        client = adapter.OndoHttpClient(_environment(adapter, environment), timeout_secs)

    started = _now()
    attempt = run_id or new_run_id(started)
    base_meta: dict[str, object] = {
        "tool": "ondo_preflight",
        "schema_version": SCHEMA_VERSION,
        "venue": ONDO_VENUE,
        "environment": environment,
        "requested_symbols": requested,
        "symbols": [row["symbol"] for row in rows],
        "targets": rows,
        "started_at_utc": started.isoformat(timespec="milliseconds"),
    }
    out_dir = Path(out_dir)
    try:
        if dynamic:
            payloads, rows = asyncio.run(collect_all(client))
            base_meta["symbols"] = [row["symbol"] for row in rows]
            base_meta["targets"] = rows
            base_meta["discovery"] = discovery_summary(payloads[MARKET_FILE])
        else:
            payloads = asyncio.run(collect(client, rows))
        # The schema checks run before anything is written: an error body must fail
        # the run rather than become a tidy "no markets" report.
        report = build_report_for_rows(payloads, rows)
    except OndoPreflightError as exc:
        # Nothing was read, so no payload is invented - but this attempt's own status is
        # published, with the previous published view cleared of payloads it cannot back.
        write_report({}, out_dir, run_id=attempt, complete=False, meta=base_meta,
                     failure=str(exc))
        print(f"[ondo-preflight] FAILED: {exc}", file=sys.stderr, flush=True)
        print(f"[ondo-preflight] a failed read is not an empty market: no payload was "
              f"written; this attempt is recorded at {out_dir / RUNS_DIRNAME / attempt}",
              file=sys.stderr, flush=True)
        return 1
    fetched = _now()

    missing = report[MISSING_FILE]
    meta = dict(base_meta)
    meta["fetched_at_utc"] = fetched.isoformat(timespec="milliseconds")
    meta["fetched_at_ns"] = int(fetched.timestamp() * 1_000_000_000)
    meta["complete"] = not missing
    if missing:
        # An incomplete attempt states why, in the same field a transport failure uses, so
        # a reader never has to reconstruct the verdict from the payloads.
        meta["failure"] = (
            f"{len(missing)} missing/unknown item(s) for "
            f"{', '.join(row['symbol'] for row in rows)}: not a complete preflight"
        )
    # The venue convention the status verdicts rest on, with the counts behind it,
    # so meta.json records the evidence as well as the per-market source string.
    meta["market_status_convention"] = status_convention_evidence(
        market_entries(payloads.get(MARKET_FILE)),
    )
    write_report(report, out_dir, run_id=attempt, complete=not missing, meta=meta)

    if missing:
        print(
            f"[ondo-preflight] FAILED: {len(missing)} missing/unknown item(s) for "
            f"{', '.join(row['symbol'] for row in rows)} -> {out_dir} "
            f"(run {attempt}, complete=false)",
            file=sys.stderr, flush=True,
        )
        for item in missing:
            print(
                f"[ondo-preflight]   {item['symbol']} {item['market']} {item['field']}: "
                f"{item['reason']}",
                file=sys.stderr, flush=True,
            )
        return 1
    print(
        f"[ondo-preflight] OK {', '.join(row['symbol'] for row in rows)} -> {out_dir} "
        f"(status, markets, contracts, instruments, meta; run {attempt})",
        flush=True,
    )
    return 0


def _environment(adapter: OndoAdapter, name: str):
    enum = adapter.OndoEnvironment
    return enum.SANDBOX if name == "sandbox" else enum.PRODUCTION


# -------------------------------------------------------------------------- cli


def parse_args(argv: Sequence[str] | None = None):
    parser = argparse.ArgumentParser(
        description="Ondo Perps public status/markets/contracts preflight (read-only, no keys)",
    )
    parser.add_argument("--symbols", default=DEFAULT_SYMBOLS,
                        help="comma list of Ondo symbols, or ALL for every enabled USD perp")
    parser.add_argument("--out", type=Path, default=Path("reports/ondo-preflight"),
                        help="output directory for the evidence files")
    parser.add_argument("--environment", default="production", choices=["production", "sandbox"],
                        help="which Ondo environment to read (public data only)")
    parser.add_argument("--timeout-secs", type=int, default=15, help="HTTP timeout per call")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved targets as JSON and exit: no network, "
                             "no open positions, no client construction, no .env")
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None, *, client=None) -> int:
    """CLI entry point. Returns the exit code (0 ok, 1 data fault, 2 environment fault)."""
    args = parse_args(argv)
    symbols = [part.strip().upper() for part in args.symbols.split(",") if part.strip()]
    try:
        if args.dry_run:
            load_adapter()  # proves the adapter is importable; constructs nothing
            print(json.dumps(describe(symbols, args), indent=2))
            return 0
        return run(symbols, Path(args.out), environment=args.environment,
                   timeout_secs=args.timeout_secs, client=client)
    except AdapterMissing as exc:
        print(f"[ondo-preflight] _adapter_missing: {exc}", file=sys.stderr, flush=True)
        print(f"[ondo-preflight] build pointer: {BUILD_POINTER}", file=sys.stderr, flush=True)
        return 2
    except OndoPreflightError as exc:
        print(f"[ondo-preflight] FAILED: {exc}", file=sys.stderr, flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Ondo Perps public metadata / status / fee preflight (plan 4.1, task 3).

    src/ondo_preflight.py --symbols NVDA,TSLA --out <dir>

The CLI reads the venue's *public* surface and writes evidence to ``<dir>``:

    status.json       GET /status, as delivered
    markets.json      GET /v1/markets, as delivered (exact decimal strings)
    contracts.json    GET /v1/contracts, as delivered
    instruments.json  the normalized target instrument table
    missing.json      every missing/unknown field of a requested target
    meta.json         fetch times + a sha256/byte-size manifest of the five files

``meta.json`` exists only when every target passed: it is the "this preflight is
complete" marker, so a half-run can never be mistaken for a green one.

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
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from spread_watch import INSTRUMENTS, ONDO_RAW_MARKETS, _adapter_missing  # noqa: E402

ONDO_ADAPTER = "nautilus_trader.adapters.ondo"
ONDO_VENUE = "ONDO"
DEFAULT_SYMBOLS = "NVDA,TSLA"
SCHEMA_VERSION = 1
STATUS_FILE = "status"
MARKET_FILE = "markets"
CONTRACT_FILE = "contracts"
INSTRUMENT_FILE = "instruments"
MISSING_FILE = "missing"
META_FILE = "meta"
PAYLOAD_FILES = (STATUS_FILE, MARKET_FILE, CONTRACT_FILE, INSTRUMENT_FILE, MISSING_FILE)

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
    """CLI symbol -> the venue's raw market and the planned Nautilus instrument id."""
    if not symbols:
        raise OndoPreflightError("no symbols requested")
    rows: list[dict[str, str]] = []
    for symbol in symbols:
        raw = ONDO_RAW_MARKETS.get(symbol)
        planned = INSTRUMENTS.get(symbol, {}).get("ONDO")
        if raw is None or planned is None:
            raise OndoPreflightError(
                f"{symbol} is not a mapped Ondo market; known: {sorted(ONDO_RAW_MARKETS)}",
            )
        rows.append({
            "symbol": symbol,
            "raw_market": raw,
            "instrument_id": planned[0],
            "venue": ONDO_VENUE,
            "client_id": ONDO_VENUE,
        })
    return rows


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
    text = json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    data = text.encode("utf-8")
    path.write_bytes(data)
    return {
        "path": path.name,
        "bytes": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def write_report(report: dict[str, object], out_dir: Path, *, complete: bool,
                 meta: dict[str, object]) -> dict[str, object]:
    """Write the five payload files, and ``meta.json`` only for a complete run."""
    out_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, object] = {}
    for name in PAYLOAD_FILES:
        files[name] = _write_json(out_dir / f"{name}.json", report[name])
    manifest = dict(meta)
    manifest["files"] = files
    if complete:
        manifest["missing"] = []
        _write_json(out_dir / f"{META_FILE}.json", manifest)
    return manifest


# -------------------------------------------------------------------------- run


def _now() -> datetime:
    return datetime.now(timezone.utc)


def run(symbols: Sequence[str], out_dir: Path, *, environment: str = "production",
        timeout_secs: int = 15, client=None) -> int:
    """Read the public surface once and write the evidence. 0 iff every target passed."""
    rows = targets(symbols)  # validates the symbols before any client exists
    if client is None:
        adapter = load_adapter()
        client = adapter.OndoHttpClient(_environment(adapter, environment), timeout_secs)

    started = _now()
    try:
        payloads = asyncio.run(collect(client, rows))
        # The schema checks run before anything is written: an error body must fail
        # the run rather than become a tidy "no markets" report.
        report = build_report(payloads, symbols)
    except OndoPreflightError as exc:
        print(f"[ondo-preflight] FAILED: {exc}", file=sys.stderr, flush=True)
        print("[ondo-preflight] nothing was written: a failed read is not an empty market",
              file=sys.stderr, flush=True)
        return 1
    fetched = _now()

    missing = report[MISSING_FILE]
    meta: dict[str, object] = {
        "tool": "ondo_preflight",
        "schema_version": SCHEMA_VERSION,
        "venue": ONDO_VENUE,
        "environment": environment,
        "symbols": [row["symbol"] for row in rows],
        "targets": rows,
        "started_at_utc": started.isoformat(timespec="milliseconds"),
        "fetched_at_utc": fetched.isoformat(timespec="milliseconds"),
        "fetched_at_ns": int(fetched.timestamp() * 1_000_000_000),
        "complete": not missing,
    }
    # The venue convention the status verdicts rest on, with the counts behind it,
    # so meta.json records the evidence as well as the per-market source string.
    meta["market_status_convention"] = status_convention_evidence(
        market_entries(payloads.get(MARKET_FILE)),
    )
    write_report(report, out_dir, complete=not missing, meta=meta)

    if missing:
        print(
            f"[ondo-preflight] FAILED: {len(missing)} missing/unknown item(s) for "
            f"{', '.join(row['symbol'] for row in rows)} -> {out_dir}",
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
        f"(status, markets, contracts, instruments, meta)",
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
                        help=f"comma list of CLI symbols; known: {sorted(ONDO_RAW_MARKETS)}")
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

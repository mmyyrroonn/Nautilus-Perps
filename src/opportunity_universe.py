"""Discover comparable crypto perps from public metadata, without native imports.

Identity is based on explicit crypto classifications and exact native base units.
No k/1000 aliases are silently merged. Public fees and USD parity are labelled
assumptions; discovery does not establish account fees or usable live books.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import re
import sys
import urllib.request


VENUES = ("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK")
FILES = {venue: f"{venue.lower()}-metadata.json" for venue in VENUES}
URLS = {
    "HL": "https://api.hyperliquid.xyz/info",
    "LIGHTER": "https://mainnet.zklighter.elliot.ai/api/v1/orderBooks",
    "ASTER": "https://fapi.asterdex.com/fapi/v1/exchangeInfo",
    "ONDO": "https://api.ondoperps.xyz/v1/markets",
    "BACKPACK": "https://api.backpack.exchange/api/v1/markets",
}
ASTER_FEES_URL = "https://docs.asterdex.com/trading/perpetuals/fees-and-specs/fees"
GROUP_B = frozenset((
    "1000NEXUSDT", "AEONUSDT", "ASTEROIDUSDT", "AVLUSDT", "BAYUSDT", "BASECATUSDT",
    "BLENDUSDT", "B3USDT", "CARDSUSDT", "CATEUSDT", "DELTAUSDT", "FONEUSDT",
    "MEMEUSDT", "MARSCOINUSDT", "NESUSDT", "OUSDT", "OKBUSDT", "PENGUINUSDT",
    "PUNDIAIUSDT", "RTXUSDT", "SKHYNIXUSDT",
))
RWA_SUBTYPES = frozenset(("STOCK", "STOCKS", "COMMODITY", "COMMODITIES", "ETF", "INDEX", "FX", "FOREX", "USD1-RWA"))
TOKEN = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,39}\Z")
MAX_METADATA_BYTES = 16 * 1024 * 1024
FX_SOURCE = "Configured USD/USDC/USDT parity assumption; not a live FX rate"


class UniverseError(ValueError):
    pass


def _selected(venues):
    selected = set(VENUES if venues is None else venues)
    if not selected or selected - set(VENUES):
        raise UniverseError("venues must be a nonempty subset of HL,LIGHTER,ASTER,ONDO,BACKPACK")
    return tuple(venue for venue in VENUES if venue in selected)


def _decimal(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    if not number.is_finite() or number < 0 or (positive and number == 0):
        return None
    return number


def _token(value):
    if not isinstance(value, str) or not value or value != value.strip():
        return None
    canonical = value.upper()
    return canonical if TOKEN.fullmatch(canonical) else None


def _rows(venue, data):
    try:
        if venue == "HL":
            rows = data["universe"]
        elif venue == "LIGHTER":
            if type(data.get("code")) is not int or data["code"] != 200:
                raise UniverseError("LIGHTER metadata did not report code 200")
            rows = data.get("order_books", data.get("order_book_details"))
        elif venue == "ASTER":
            rows = data["symbols"]
        elif venue == "ONDO":
            if data.get("success") is not True:
                raise UniverseError("ONDO metadata did not report success")
            rows = data["result"]["perps"]["tradingPairs"]
        else:
            rows = data
    except (KeyError, TypeError, AttributeError) as exc:
        raise UniverseError(f"{venue} metadata has an invalid response envelope") from exc
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise UniverseError(f"{venue} metadata must contain a list of market objects")
    return rows


def _mapping(venue, canonical, native):
    mapping = {"symbol": canonical, "venue": venue, "instrument_id": native,
               "quote_to_usd": "1", "valuation_source": FX_SOURCE}
    if venue == "HL":
        mapping.update(taker_fee_bps="4.5", fee_source=(
            "Configured standard public taker assumption 4.5 bps; account tier unverified; "
            "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees"))
    elif venue == "ASTER":
        raw = native.removesuffix("-PERP.ASTER")
        group = "Group B" if raw in GROUP_B else "crypto general"
        mapping.update(taker_fee_bps="10" if raw in GROUP_B else "4", fee_source=(
            f"Configured public {group} taker assumption; classification frozen 2026-10-06; "
            f"verify current published fees; {ASTER_FEES_URL}"))
    elif venue == "BACKPACK":
        reference = "Configured public sample assumptions; account fees/margins unverified; https://backpack.exchange/fees"
        mapping.update(taker_fee_bps="5", fee_source=reference,
                       backpack_economics={"margin_init": "0.1", "margin_maint": "0.05",
                           "maker_fee": "0", "taker_fee": "0.0005", "source": "Configured",
                           "source_reference": reference})
    # Lighter and Ondo retain native fee metadata, without guessing overrides.
    return mapping


def _candidate(venue, row):
    """Return (canonical, native id, rejection reason) for one public row."""
    if venue == "HL":
        raw = row.get("name")
        canonical = _token(raw)
        if canonical is None or ":" in raw:
            return None, None, "nonprimary_or_invalid_identity"
        if row.get("isDelisted", False) is not False:
            return None, None, "inactive_or_unknown_status"
        if type(row.get("szDecimals")) is not int or not 0 <= row["szDecimals"] <= 16:
            return None, None, "invalid_quantity_precision"
        return canonical, f"{raw}-USD-PERP.HYPERLIQUID", None
    if venue == "ASTER":
        canonical = _token(row.get("baseAsset"))
        raw = row.get("symbol")
        subtypes = row.get("underlyingSubType", [])
        if (row.get("underlyingType") != "COIN" or type(row.get("symbolType")) is not int
                or row["symbolType"] != 0 or not isinstance(subtypes, list)
                or any(not isinstance(value, str) or value.upper() in RWA_SUBTYPES for value in subtypes)):
            return None, None, "noncrypto_or_unknown_classification"
        if row.get("contractType") != "PERPETUAL" or row.get("status") != "TRADING":
            return None, None, "inactive_or_nonperpetual"
        if row.get("quoteAsset") != "USDT" or row.get("marginAsset") != "USDT":
            return None, None, "unsupported_quote_or_margin"
        if canonical is None or raw != canonical + "USDT":
            return None, None, "inconsistent_identity"
        return canonical, f"{raw}-PERP.ASTER", None
    if venue == "ONDO":
        raw = row.get("market")
        tags = row.get("tags")
        if not isinstance(tags, list) or "Crypto" not in tags or any(
                tag in tags for tag in ("Stock", "ETF", "Commodity", "Index", "FX")):
            return None, None, "noncrypto_or_unknown_classification"
        canonical = _token(raw.removesuffix("-USD.P")) if isinstance(raw, str) and raw.endswith("-USD.P") else None
        if canonical is None:
            return None, None, "invalid_identity"
        status = row.get("status", "active")
        if row.get("disabled", False) is not False or not isinstance(status, str) or status.lower() not in (
                "active", "enabled", "trading", "open"):
            return None, None, "inactive_or_unknown_status"
        if any(_decimal(row.get(field), positive=True) is None for field in ("baseIncrement", "quoteIncrement")):
            return None, None, "invalid_increments"
        if _decimal(row.get("takerFee")) is None:
            return None, None, "missing_or_invalid_fee"
        return canonical, raw.removesuffix(".P") + "-PERP.ONDO", None
    if venue == "BACKPACK":
        if row.get("marketType") != "PERP" or row.get("quoteSymbol") != "USDC":
            return None, None, "unsupported_product_or_quote"
        if row.get("rwaMarketType", "missing") is not None:
            return None, None, "rwa_or_unknown_classification"
        if row.get("orderBookState") != "Open" or row.get("visible") is not True:
            return None, None, "inactive_or_hidden"
        canonical = _token(row.get("baseSymbol"))
        if canonical is not None and row["baseSymbol"] != canonical:
            return None, None, "unsupported_native_case"
        if canonical is None or row.get("symbol") != canonical + "_USDC_PERP":
            return None, None, "inconsistent_identity"
        filters = row.get("filters")
        if not isinstance(filters, dict) or any(
                not isinstance(filters.get(side), dict) or _decimal(filters[side].get(field), positive=True) is None
                for side, field in (("price", "tickSize"), ("quantity", "stepSize"), ("quantity", "minQuantity"))):
            return None, None, "invalid_filters"
        return canonical, row["symbol"] + ".BACKPACK", None
    if row.get("market_type") != "perp" or row.get("status") != "active" or row.get("is_frozen", False) is not False:
        return None, None, "inactive_spot_or_frozen"
    canonical = _token(row.get("symbol"))
    if canonical is None or type(row.get("market_id")) is not int:
        return None, None, "invalid_identity"
    if _decimal(row.get("multiplier", "1"), positive=True) != 1:
        return None, None, "unsupported_nonunit_multiplier"
    return canonical, canonical + "-PERP.LIGHTER", None


def _rwa_names(venue, rows):
    names = set()
    for row in rows:
        raw = None
        subtypes = row.get("underlyingSubType", [])
        if venue == "ASTER" and (row.get("symbolType") == 1 or (
                isinstance(subtypes, list) and any(isinstance(value, str) and value.upper() in RWA_SUBTYPES
                                                for value in subtypes))):
            raw = row.get("baseAsset")
        elif venue == "ONDO" and isinstance(row.get("tags"), list) and any(
                tag in row["tags"] for tag in ("Stock", "ETF", "Commodity", "Index", "FX")):
            market = row.get("market", "")
            raw = market.removesuffix("-USD.P") if isinstance(market, str) else None
        elif venue == "BACKPACK" and row.get("marketType") == "PERP" and row.get("rwaMarketType") is not None:
            raw = row.get("baseSymbol")
        canonical = _token(raw)
        if canonical:
            names.add(canonical)
    return names


def generate_document(metadata, *, venues=None, symbols=None, provenance=None, generated_at=None):
    """Return a deterministic registry document and bounded discovery summary.

    Metadata can include unselected venues as classification evidence. Ambiguous
    per-symbol/per-venue duplicates are all excluded, rather than picking a leg.
    """
    selected = _selected(venues)
    if not isinstance(metadata, dict) or set(selected) - metadata.keys():
        raise UniverseError("metadata is missing a selected venue")
    if metadata.keys() - set(VENUES):
        raise UniverseError("unknown metadata venue")
    requested = None if symbols is None else set(symbols)
    if requested is not None and (not requested or any(_token(value) != value for value in requested)):
        raise UniverseError("symbols must contain canonical uppercase identifiers")
    rows = {venue: _rows(venue, data) for venue, data in metadata.items()}
    exclusions = {venue: Counter() for venue in rows}
    candidates = defaultdict(list)
    anchors = set()
    rwa_names = set()
    for venue in VENUES:
        if venue not in rows:
            continue
        rwa_names.update(_rwa_names(venue, rows[venue]))
        for row in rows[venue]:
            canonical, native, reason = _candidate(venue, row)
            if reason:
                exclusions[venue][reason] += 1
            else:
                candidates[canonical, venue].append(native)
    eligible = {}
    for (canonical, venue), native_ids in candidates.items():
        if len(native_ids) != 1:
            exclusions[venue]["ambiguous_duplicate_market"] += len(native_ids)
        else:
            eligible[canonical, venue] = native_ids[0]
            if venue != "LIGHTER":
                anchors.add(canonical)
    for key in list(eligible):
        canonical, venue = key
        if venue == "LIGHTER" and (canonical not in anchors or canonical in rwa_names):
            exclusions[venue]["unanchored_or_ambiguous_asset"] += 1
            del eligible[key]
        elif venue not in selected:
            exclusions[venue]["venue_not_selected"] += 1
            del eligible[key]
        elif requested is not None and canonical not in requested:
            exclusions[venue]["symbol_not_selected"] += 1
            del eligible[key]
    comparable = {symbol for symbol, _ in eligible if sum((symbol, venue) in eligible for venue in selected) >= 2}
    markets = []
    optional = []
    venue_counts = Counter()
    for canonical in sorted({key[0] for key in eligible}):
        if canonical not in comparable:
            for venue in selected:
                if (canonical, venue) in eligible:
                    exclusions[venue]["fewer_than_two_venues"] += 1
            continue
        legs = []
        for venue in selected:
            if (canonical, venue) not in eligible:
                continue
            if venue == "BACKPACK" and venue_counts[venue] >= 100:
                exclusions[venue]["backpack_100_market_cap"] += 1
                continue
            legs.append(_mapping(venue, canonical, eligible[canonical, venue]))
        if len(legs) < 2:
            for leg in legs:
                exclusions[leg["venue"]]["fewer_than_two_venues_after_cap"] += 1
            continue
        # Reserve two legs per underlying first, maximizing comparable symbols.
        if len(markets) + 2 > 1000:
            for leg in legs:
                exclusions[leg["venue"]]["registry_1000_mapping_cap"] += 1
            continue
        markets.extend(legs[:2])
        venue_counts.update(leg["venue"] for leg in legs[:2])
        optional.extend(legs[2:])
    # Spend remaining slots on additional venues, favoring underrepresented legs.
    while optional:
        index = min(range(len(optional)), key=lambda i: (
            venue_counts[optional[i]["venue"]], optional[i]["symbol"], VENUES.index(optional[i]["venue"])))
        leg = optional.pop(index)
        if leg["venue"] == "BACKPACK" and venue_counts["BACKPACK"] >= 100:
            exclusions["BACKPACK"]["backpack_100_market_cap"] += 1
        elif len(markets) >= 1000:
            exclusions[leg["venue"]]["registry_1000_mapping_cap"] += 1
        else:
            markets.append(leg)
            venue_counts[leg["venue"]] += 1
    markets.sort(key=lambda market: (market["symbol"], VENUES.index(market["venue"])))
    if not markets:
        raise UniverseError("no unambiguous active crypto symbols occur on two selected venues")
    document = {"schema_version": 1,
        "scan": {"target_notional": "500", "min_entry_edge_bps": "1", "reserve_bps": "5",
                 "max_age_ms": 2000, "max_skew_ms": 500, "max_receive_age_ms": 2000},
        "recording": {"enabled": True, "path": "../reports/opportunities/opportunities.jsonl",
                      "cooldown_ms": 5000, "edge_change_bps": "2", "quantity_change_fraction": "0.1"},
        "runtime": {"connection_timeout_secs": 120, "duration_secs": 3600, "depth_levels": 20,
                    "max_levels_per_side": 20000, "refresh_ms": 1000, "top_n": 20,
                    "evaluation_interval_ms": 100, "aster_snapshot_depth": 100,
                    "aster_subscription_interval_ms": 500},
        "markets": markets}
    observed_operators = Counter(str(row.get("operator_account_index", "missing"))
                                 for row in rows.get("LIGHTER", []))
    summary = {"schema_version": 1, "generated_at_utc": generated_at or datetime.now(timezone.utc).isoformat(),
        "symbol_count": len({market["symbol"] for market in markets}), "market_count": len(markets),
        "selected_venues": list(selected), "requested_symbols": sorted(requested) if requested is not None else None,
        "per_venue_counts": {venue: sum(market["venue"] == venue for market in markets) for venue in selected},
        "input_counts": {venue: len(rows[venue]) for venue in VENUES if venue in rows},
        "exclusions": {venue: dict(sorted(exclusions[venue].items())) for venue in VENUES if venue in rows},
        "alias_policy": "exact native base units only; k/1000 aliases not merged or independently verified",
        "capacity_policy": "reserve two legs per alphabetically ordered symbol, then favor underrepresented optional venues; Backpack cap100, total cap1000",
        "lighter_identity_policy": "trusted crypto anchor intersection; known RWA collisions excluded; operator index is not a classifier",
        "lighter_public_operator_counts": dict(sorted(observed_operators.items())),
        "inputs": {venue: (provenance or {}).get(venue, {"url": URLS[venue],
            "canonical_document_sha256": hashlib.sha256(json.dumps(metadata[venue], sort_keys=True,
                separators=(",", ":"), default=str).encode()).hexdigest(), "source_capture_time": "unknown"})
            for venue in VENUES if venue in metadata},
        "historical_samples_retained": 0, "live_connections_verified": False,
        "fees_and_fx_are_assumptions": True}
    return document, summary


def render_toml(document):
    """Render the scanner's fixed schema using JSON string quoting valid in TOML."""
    def value(item):
        if isinstance(item, bool):
            return "true" if item else "false"
        if isinstance(item, int):
            return str(item)
        if isinstance(item, str):
            return json.dumps(item, ensure_ascii=False)
        raise UniverseError("unsupported TOML value")
    lines = ["# Generated public crypto registry; assumptions require verification.", "schema_version = 1"]
    for section in ("scan", "recording", "runtime"):
        lines += ["", f"[{section}]"]
        lines += [f"{key} = {value(item)}" for key, item in document[section].items()]
    for market in document["markets"]:
        lines += ["", "[[markets]]"]
        lines += [f"{key} = {value(item)}" for key, item in market.items() if key != "backpack_economics"]
        if "backpack_economics" in market:
            lines += ["[markets.backpack_economics]"]
            lines += [f"{key} = {value(item)}" for key, item in market["backpack_economics"].items()]
    return "\n".join(lines) + "\n"


def _decode(raw, venue):
    if len(raw) > MAX_METADATA_BYTES:
        raise UniverseError(f"{venue} metadata exceeds the bounded response size")
    try:
        return json.loads(raw, parse_float=Decimal, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except (ValueError, UnicodeError) as exc:
        raise UniverseError(f"{venue} metadata is not valid finite JSON") from exc


def load_metadata(directory, venues=None):
    """Load selected markets and all available public classification evidence."""
    required = set(_selected(venues))
    metadata, provenance = {}, {}
    for venue in VENUES:
        path = Path(directory) / FILES[venue]
        if venue not in required and not path.exists():
            continue
        if path.stat().st_size > MAX_METADATA_BYTES:
            raise UniverseError(f"{venue} metadata exceeds the bounded response size")
        raw = path.read_bytes()
        metadata[venue] = _decode(raw, venue)
        provenance[venue] = {"url": URLS[venue], "input_path": str(path.resolve()),
            "sha256": hashlib.sha256(raw).hexdigest(), "source_capture_time": "unknown",
            "input_mtime_utc": datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat()}
    return metadata, provenance


def fetch_metadata(venues=None, timeout_secs=30):
    """Fetch fixed credential-free public URLs directly, without proxy inheritance."""
    if type(timeout_secs) is not int or not 1 <= timeout_secs <= 300:
        raise UniverseError("timeout_secs must be an integer in [1,300]")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    metadata, provenance = {}, {}
    for venue in _selected(venues):
        payload = b'{"type":"meta"}' if venue == "HL" else None
        request = urllib.request.Request(URLS[venue], data=payload,
            headers={"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "persarb-public-universe/1"})
        with opener.open(request, timeout=timeout_secs) as response:
            raw = response.read(MAX_METADATA_BYTES + 1)
        metadata[venue] = _decode(raw, venue)
        provenance[venue] = {"url": URLS[venue], "sha256": hashlib.sha256(raw).hexdigest(),
            "fetched_at_utc": datetime.now(timezone.utc).isoformat(), "source_capture_time": "unknown"}
    return metadata, provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata-dir", type=Path, help="offline captured public JSON; otherwise fetch fixed public URLs directly")
    parser.add_argument("--output", type=Path, required=True, help="requested scanner TOML path")
    parser.add_argument("--summary", type=Path, help="optional requested JSON provenance summary")
    parser.add_argument("--venues", default=",".join(VENUES), help="comma-separated supported venue subset")
    parser.add_argument("--symbols", help="optional comma-separated canonical uppercase symbols")
    parser.add_argument("--timeout-secs", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="validate and print summary without creating config or summary files")
    args = parser.parse_args(argv)
    try:
        venues = _selected(args.venues.split(","))
        # Output selection must not remove trusted RWA/crypto identity evidence.
        evidence_venues = set(venues) | {"HL", "ASTER", "ONDO", "BACKPACK"}
        metadata, provenance = load_metadata(args.metadata_dir, venues) if args.metadata_dir else fetch_metadata(evidence_venues, args.timeout_secs)
        document, summary = generate_document(metadata, venues=venues,
            symbols=args.symbols.split(",") if args.symbols is not None else None, provenance=provenance)
        from opportunity_scan import parse_plan
        parse_plan(document, args.output.parent)
        if not args.dry_run:
            if args.summary is not None and args.summary.resolve() == args.output.resolve():
                raise UniverseError("output and summary must be distinct paths")
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(render_toml(document), encoding="utf-8", newline="\n")
            if args.summary is not None:
                args.summary.parent.mkdir(parents=True, exist_ok=True)
                args.summary.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0
    except (OSError, ValueError) as exc:
        print(f"[universe] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

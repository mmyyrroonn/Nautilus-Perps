"""Discover comparable crypto perps from public metadata, without native imports.

Discovery uses crypto classifications and exact native base-unit strings, subject
to explicit identity quarantines. This does not certify asset identity.
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
import time
import urllib.request

from opportunity_identity import LIGHTER_PRL_QUARANTINE_CODE, identity_quarantine_reason


VENUES = ("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK")
RWA_VENUES = VENUES
HL_HIP3_DEXES = ("xyz", "io", "para")
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
ONDO_RWA_TAGS = frozenset(("Stock", "ETF", "Commodity", "Index", "FX", "Pre-IPO"))
RWA_ALIASES = {
    "GOLD": "XAU",
    "SILVER": "XAG",
    "COPPER": "XCU",
    "PLATINUM": "XPT",
    "PALLADIUM": "XPD",
    "WTI": "CL",
    "BRENT": "BRENTOIL",
    "NATGAS": "NG",
    "SP500": "SPX",
    "US500": "SPX",
    "XYZ100": "NDX",
    "US100": "NDX",
    "EUR": "EURUSD",
    "JPY": "USDJPY",
    "GBP": "GBPUSD",
    "SKHX": "SKHYNIX",
    "SKHYNIXUSD": "SKHYNIX",
    "SMSN": "SAMSUNG",
    "SAMSUNGUSD": "SAMSUNG",
    "HYUNDAIUSD": "HYUNDAI",
}
LIGHTER_KRW_EXCLUDED = frozenset(("SAMSUNG", "SKHYNIX", "HYUNDAI"))
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


def _mapping(venue, canonical, native, *, asset_class="crypto"):
    mapping = {"symbol": canonical, "venue": venue, "instrument_id": native,
               "quote_to_usd": "1", "valuation_source": FX_SOURCE}
    if venue == "HL":
        mapping.update(taker_fee_bps="4.5", fee_source=(
            "Configured standard public taker assumption 4.5 bps; account tier unverified; "
            "https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees"))
    elif venue == "ASTER":
        raw = native.removesuffix("-PERP.ASTER")
        if asset_class == "rwa":
            mapping.update(taker_fee_bps="4", fee_source=(
                f"Configured public RWA taker assumption 4 bps; verify current published RWA fees; {ASTER_FEES_URL}"))
        else:
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


def _canonical_rwa(symbol):
    return RWA_ALIASES.get(symbol, symbol)


def _hl_hip3_pairs(entry, dex):
    if isinstance(entry, list) and len(entry) == 2 and isinstance(entry[0], dict) and isinstance(entry[1], list):
        universe, ctxs = entry[0].get("universe"), entry[1]
    elif isinstance(entry, dict):
        universe, ctxs = entry.get("universe"), entry.get("ctxs", [])
    else:
        raise UniverseError(f"HL HIP-3 {dex} metadata has an invalid response envelope")
    if not isinstance(universe, list) or any(not isinstance(row, dict) for row in universe):
        raise UniverseError(f"HL HIP-3 {dex} metadata must contain a list of market objects")
    if not isinstance(ctxs, list):
        raise UniverseError(f"HL HIP-3 {dex} context metadata must be a list")
    if len(ctxs) != len(universe):
        return [(row, {}) for row in universe]
    return list(zip(universe, ctxs))


def _crypto_anchor_names(rows):
    names = set()
    for row in rows.get("HL", ()):
        raw = row.get("name")
        if row.get("isDelisted", False) is False and isinstance(raw, str) and ":" not in raw:
            canonical = _token(raw)
            if canonical:
                names.add(canonical)
    for row in rows.get("ASTER", ()):
        subtypes = row.get("underlyingSubType", [])
        if (row.get("underlyingType") == "COIN" and row.get("symbolType") == 0
                and row.get("status") == "TRADING" and isinstance(subtypes, list)
                and not any(isinstance(v, str) and v.upper() in RWA_SUBTYPES for v in subtypes)):
            canonical = _token(row.get("baseAsset"))
            if canonical:
                names.add(canonical)
    return names


def _discover_rwa_candidates(metadata, rows, exclusions):
    crypto_names = _crypto_anchor_names(rows)
    eligible = {}
    anchors = set()

    if "HL" in metadata:
        hip3 = metadata["HL"].get("hip3", {})
        if not isinstance(hip3, dict):
            raise UniverseError("HL hip3 metadata must be a mapping")
        hl_picks = {}
        hl_dups = defaultdict(list)
        for dex in HL_HIP3_DEXES:
            entry = hip3.get(dex)
            if entry is None:
                continue
            for row, ctx in _hl_hip3_pairs(entry, dex):
                raw = row.get("name")
                if not isinstance(raw, str) or not raw.startswith(f"{dex}:"):
                    exclusions["HL"]["nonprimary_or_invalid_identity"] += 1
                    continue
                base = _token(raw.split(":", 1)[1])
                if base is None:
                    exclusions["HL"]["nonprimary_or_invalid_identity"] += 1
                    continue
                if base in crypto_names:
                    exclusions["HL"]["crypto_asset_excluded"] += 1
                    continue
                if row.get("isDelisted", False) is not False:
                    exclusions["HL"]["inactive_or_unknown_status"] += 1
                    continue
                if type(row.get("szDecimals")) is not int or not 0 <= row["szDecimals"] <= 16:
                    exclusions["HL"]["invalid_quantity_precision"] += 1
                    continue
                canonical = _canonical_rwa(base)
                vlm = _decimal(ctx.get("dayNtlVlm", "0")) if isinstance(ctx, dict) else None
                priority = (dex == "xyz", vlm or Decimal(0), -HL_HIP3_DEXES.index(dex))
                native = f"{raw}-USD-PERP.HYPERLIQUID"
                hl_dups[(canonical, dex)].append(native)
                prev = hl_picks.get(canonical)
                if prev is None or priority > prev[0]:
                    hl_picks[canonical] = (priority, native, dex)
        for canonical, (_, native, dex) in hl_picks.items():
            if len(hl_dups[(canonical, dex)]) != 1:
                exclusions["HL"]["ambiguous_duplicate_market"] += len(hl_dups[(canonical, dex)])
            else:
                eligible[canonical, "HL"] = native
                anchors.add(canonical)

    if "ASTER" in rows:
        aster_by_quote = defaultdict(lambda: defaultdict(list))
        for row in rows["ASTER"]:
            subtypes = row.get("underlyingSubType", [])
            is_rwa = (
                row.get("underlyingType") != "COIN"
                or (type(row.get("symbolType")) is int and row["symbolType"] != 0)
                or (isinstance(subtypes, list)
                    and any(isinstance(v, str) and v.upper() in RWA_SUBTYPES for v in subtypes))
            )
            if not is_rwa:
                exclusions["ASTER"]["crypto_or_unknown_classification"] += 1
                continue
            if row.get("contractType") != "PERPETUAL" or row.get("status") != "TRADING":
                exclusions["ASTER"]["inactive_or_nonperpetual"] += 1
                continue
            quote = row.get("quoteAsset")
            if quote not in {"USDT", "USD1"} or row.get("marginAsset") != quote:
                exclusions["ASTER"]["unsupported_quote_or_margin"] += 1
                continue
            base = _token(row.get("baseAsset"))
            raw = row.get("symbol")
            if base is None or raw != base + quote:
                exclusions["ASTER"]["inconsistent_identity"] += 1
                continue
            aster_by_quote[_canonical_rwa(base)][quote].append(f"{raw}-PERP.ASTER")
        for canonical, by_quote in aster_by_quote.items():
            natives = by_quote.get("USDT") or by_quote.get("USD1")
            if len(natives) != 1:
                exclusions["ASTER"]["ambiguous_duplicate_market"] += len(natives)
            else:
                eligible[canonical, "ASTER"] = natives[0]
                anchors.add(canonical)

    if "ONDO" in rows:
        ondo_candidates = defaultdict(list)
        for row in rows["ONDO"]:
            raw = row.get("market")
            tags = row.get("tags")
            is_rwa = isinstance(tags, list) and (
                any(tag in ONDO_RWA_TAGS for tag in tags) or "Crypto" not in tags
            )
            if not is_rwa:
                exclusions["ONDO"]["crypto_or_unknown_classification"] += 1
                continue
            base = _token(raw.removesuffix("-USD.P")) if isinstance(raw, str) and raw.endswith("-USD.P") else None
            if base is None:
                exclusions["ONDO"]["invalid_identity"] += 1
                continue
            status = row.get("status", "active")
            if row.get("disabled", False) is not False or not isinstance(status, str) or status.lower() not in (
                    "active", "enabled", "trading", "open"):
                exclusions["ONDO"]["inactive_or_unknown_status"] += 1
                continue
            if any(_decimal(row.get(field), positive=True) is None for field in ("baseIncrement", "quoteIncrement")):
                exclusions["ONDO"]["invalid_increments"] += 1
                continue
            if _decimal(row.get("takerFee")) is None:
                exclusions["ONDO"]["missing_or_invalid_fee"] += 1
                continue
            ondo_candidates[_canonical_rwa(base)].append(raw.removesuffix(".P") + "-PERP.ONDO")
        for canonical, natives in ondo_candidates.items():
            if len(natives) != 1:
                exclusions["ONDO"]["ambiguous_duplicate_market"] += len(natives)
            else:
                eligible[canonical, "ONDO"] = natives[0]
                anchors.add(canonical)

    if "BACKPACK" in rows:
        bp_candidates = defaultdict(list)
        for row in rows["BACKPACK"]:
            if row.get("marketType") != "PERP" or row.get("quoteSymbol") != "USDC":
                exclusions["BACKPACK"]["unsupported_product_or_quote"] += 1
                continue
            if row.get("rwaMarketType") is None:
                exclusions["BACKPACK"]["crypto_or_unknown_classification"] += 1
                continue
            if row.get("orderBookState") != "Open" or row.get("visible") is not True:
                exclusions["BACKPACK"]["inactive_or_hidden"] += 1
                continue
            base_symbol = row.get("baseSymbol")
            if not isinstance(base_symbol, str) or "." not in base_symbol:
                exclusions["BACKPACK"]["inconsistent_identity"] += 1
                continue
            stem, suffix = base_symbol.split(".", 1)
            base = _token(stem)
            if base is None or stem != base or _token(suffix) != suffix or row.get("symbol") != f"{base_symbol}_USDC_PERP":
                exclusions["BACKPACK"]["inconsistent_identity"] += 1
                continue
            filters = row.get("filters")
            if not isinstance(filters, dict) or any(
                    not isinstance(filters.get(side), dict) or _decimal(filters[side].get(field), positive=True) is None
                    for side, field in (("price", "tickSize"), ("quantity", "stepSize"), ("quantity", "minQuantity"))):
                exclusions["BACKPACK"]["invalid_filters"] += 1
                continue
            bp_candidates[_canonical_rwa(base)].append(f"{row['symbol']}.BACKPACK")
        for canonical, natives in bp_candidates.items():
            if len(natives) != 1:
                exclusions["BACKPACK"]["ambiguous_duplicate_market"] += len(natives)
            else:
                eligible[canonical, "BACKPACK"] = natives[0]
                anchors.add(canonical)

    if "LIGHTER" in rows:
        lighter_candidates = defaultdict(list)
        for row in rows["LIGHTER"]:
            if row.get("market_type") != "perp" or row.get("status") != "active" or row.get("is_frozen", False) is not False:
                exclusions["LIGHTER"]["inactive_spot_or_frozen"] += 1
                continue
            base = _token(row.get("symbol"))
            if base is None or type(row.get("market_id")) is not int:
                exclusions["LIGHTER"]["invalid_identity"] += 1
                continue
            native = f"{base}-PERP.LIGHTER"
            if identity_quarantine_reason("LIGHTER", base, native):
                exclusions["LIGHTER"][LIGHTER_PRL_QUARANTINE_CODE] += 1
                continue
            if _decimal(row.get("multiplier", "1"), positive=True) != 1:
                exclusions["LIGHTER"]["unsupported_nonunit_multiplier"] += 1
                continue
            if base in LIGHTER_KRW_EXCLUDED or base in crypto_names:
                exclusions["LIGHTER"]["non_usd_or_crypto_asset"] += 1
                continue
            canonical = _canonical_rwa(base)
            if canonical not in anchors:
                exclusions["LIGHTER"]["unanchored_or_ambiguous_asset"] += 1
                continue
            lighter_candidates[canonical].append(native)
        for canonical, natives in lighter_candidates.items():
            if len(natives) != 1:
                exclusions["LIGHTER"]["ambiguous_duplicate_market"] += len(natives)
            else:
                eligible[canonical, "LIGHTER"] = natives[0]

    return eligible


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
    native = canonical + "-PERP.LIGHTER"
    if identity_quarantine_reason(venue, canonical, native):
        return None, None, LIGHTER_PRL_QUARANTINE_CODE
    if _decimal(row.get("multiplier", "1"), positive=True) != 1:
        return None, None, "unsupported_nonunit_multiplier"
    return canonical, native, None


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


def generate_document(metadata, *, venues=None, symbols=None, provenance=None, generated_at=None, asset_class="crypto"):
    """Return a deterministic registry document and bounded discovery summary.

    Metadata can include unselected venues as classification evidence. Ambiguous
    per-symbol/per-venue duplicates are all excluded, rather than picking a leg.
    """
    if asset_class not in {"crypto", "rwa"}:
        raise UniverseError("asset_class must be crypto or rwa")
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
    if asset_class == "rwa":
        eligible = _discover_rwa_candidates(metadata, rows, exclusions)
        for key in list(eligible):
            canonical, venue = key
            if venue not in selected:
                exclusions[venue]["venue_not_selected"] += 1
                del eligible[key]
            elif requested is not None and canonical not in requested:
                exclusions[venue]["symbol_not_selected"] += 1
                del eligible[key]
    else:
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
            legs.append(_mapping(venue, canonical, eligible[canonical, venue], asset_class=asset_class))
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
        raise UniverseError(f"no unambiguous active {asset_class} symbols occur on two selected venues")
    aster_interval_ms = 500 if asset_class == "rwa" else 1000
    document = {"schema_version": 1,
        "scan": {"target_notional": "500", "min_entry_edge_bps": "1", "reserve_bps": "5",
                 "max_age_ms": 2000, "max_skew_ms": 500, "max_receive_age_ms": 2000},
        "recording": {"enabled": True, "path": "../reports/opportunities/opportunities.jsonl",
                      "cooldown_ms": 5000, "edge_change_bps": "2", "quantity_change_fraction": "0.1"},
        "runtime": {"connection_timeout_secs": 300, "duration_secs": 3600, "depth_levels": 20,
                    "max_levels_per_side": 20000, "refresh_ms": 1000, "top_n": 20,
                    "evaluation_interval_ms": 100, "aster_snapshot_depth": 100,
                    "aster_subscription_interval_ms": aster_interval_ms},
        "markets": markets}
    observed_operators = Counter(str(row.get("operator_account_index", "missing"))
                                 for row in rows.get("LIGHTER", []))
    summary = {"schema_version": 1, "asset_class": asset_class,
        "generated_at_utc": generated_at or datetime.now(timezone.utc).isoformat(),
        "symbol_count": len({market["symbol"] for market in markets}), "market_count": len(markets),
        "selected_venues": list(selected), "requested_symbols": sorted(requested) if requested is not None else None,
        "per_venue_counts": {venue: sum(market["venue"] == venue for market in markets) for venue in selected},
        "input_counts": {venue: len(rows[venue]) for venue in VENUES if venue in rows},
        "exclusions": {venue: dict(sorted(exclusions[venue].items())) for venue in VENUES if venue in rows},
        "alias_policy": ("explicit USD-quoted RWA aliases normalized across venues; KRW bare tickers excluded"
                         if asset_class == "rwa" else
                         "exact native base units only; k/1000 aliases not merged or independently verified"),
        "capacity_policy": "reserve two legs per alphabetically ordered symbol, then favor underrepresented optional venues; Backpack cap100, total cap1000",
        "lighter_identity_policy": "ticker-only crypto anchor intersection; known RWA collisions and quarantined Lighter PRL excluded; operator index is not a classifier; other asset identities are not certified",
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


def _open_with_retries(opener, url, payload, timeout_secs, *, attempts=4):
    headers = {"Accept": "application/json", "Content-Type": "application/json", "User-Agent": "persarb-public-universe/1"}
    last_exc = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=payload, headers=headers)
            with opener.open(request, timeout=timeout_secs) as response:
                return response.read(MAX_METADATA_BYTES + 1)
        except OSError as exc:
            last_exc = exc
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.5)
    raise last_exc


def fetch_metadata(venues=None, timeout_secs=30, *, asset_class="crypto"):
    """Fetch fixed credential-free public URLs directly, without proxy inheritance."""
    if type(timeout_secs) is not int or not 1 <= timeout_secs <= 300:
        raise UniverseError("timeout_secs must be an integer in [1,300]")
    if asset_class not in {"crypto", "rwa"}:
        raise UniverseError("asset_class must be crypto or rwa")
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    metadata, provenance = {}, {}
    for venue in _selected(venues):
        payload = b'{"type":"meta"}' if venue == "HL" else None
        raw = _open_with_retries(opener, URLS[venue], payload, timeout_secs)
        decoded = _decode(raw, venue)
        prov = {"url": URLS[venue], "sha256": hashlib.sha256(raw).hexdigest(),
                "fetched_at_utc": datetime.now(timezone.utc).isoformat(), "source_capture_time": "unknown"}
        if asset_class == "rwa" and venue == "HL":
            hip3 = {}
            hip3_hashes = {}
            for dex in HL_HIP3_DEXES:
                dex_payload = json.dumps({"type": "metaAndAssetCtxs", "dex": dex}).encode()
                dex_raw = _open_with_retries(opener, URLS["HL"], dex_payload, timeout_secs)
                hip3[dex] = _decode(dex_raw, "HL")
                hip3_hashes[dex] = hashlib.sha256(dex_raw).hexdigest()
            decoded["hip3"] = hip3
            prov["hip3_sha256"] = hip3_hashes
        metadata[venue] = decoded
        provenance[venue] = prov
    return metadata, provenance


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--metadata-dir", type=Path, help="offline captured public JSON; otherwise fetch fixed public URLs directly")
    parser.add_argument("--output", type=Path, required=True, help="requested scanner TOML path")
    parser.add_argument("--summary", type=Path, help="optional requested JSON provenance summary")
    parser.add_argument("--asset-class", choices=("crypto", "rwa"), default="crypto",
                        help="asset class to discover (default: crypto)")
    parser.add_argument("--venues", default=None, help="comma-separated supported venue subset")
    parser.add_argument("--symbols", help="optional comma-separated canonical uppercase symbols")
    parser.add_argument("--timeout-secs", type=int, default=30)
    parser.add_argument("--dry-run", action="store_true", help="validate and print summary without creating config or summary files")
    args = parser.parse_args(argv)
    try:
        default_venues = RWA_VENUES if args.asset_class == "rwa" else VENUES
        venues = _selected(default_venues if args.venues is None else args.venues.split(","))
        # Output selection must not remove trusted RWA/crypto identity evidence.
        evidence_venues = set(venues) | {"HL", "ASTER", "ONDO", "BACKPACK"}
        metadata, provenance = (
            load_metadata(args.metadata_dir, venues)
            if args.metadata_dir
            else fetch_metadata(evidence_venues, args.timeout_secs, asset_class=args.asset_class)
        )
        document, summary = generate_document(
            metadata, venues=venues,
            symbols=args.symbols.split(",") if args.symbols is not None else None,
            provenance=provenance, asset_class=args.asset_class,
        )
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

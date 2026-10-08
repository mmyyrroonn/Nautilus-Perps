"""Public io catalog and explicit equity observation mappings, without native imports.

The catalog preserves protocol indexes. A manifest is evidence supplied by the
operator, not a conclusion that matching tickers create equivalent contracts.
This module performs no network, account or trading operations.
"""
from __future__ import annotations

from collections import Counter
from decimal import Decimal, InvalidOperation
import re


class EntropyUniverseError(ValueError):
    pass


_SYMBOL = re.compile(r"[A-Z0-9][A-Z0-9_-]{0,39}\Z")
_NATIVE = re.compile(r"io:[A-Za-z0-9][A-Za-z0-9_-]{0,39}\Z")
# Mainnet Hyperliquid token identity, not a ticker or a spot array position.
# Public spotMeta capture: reports/entropy-discovery/20261008-public.
_MAINNET_USDC_TOKEN_ID = "0x6d1e7cde53ba9467b783cb7c530ce054"
_MATCH_FIELDS = {
    "symbol", "product_class", "underlying", "underlying_source", "oracle",
    "trading_hours", "corporate_actions", "notes", "legs",
}
_LEG_FIELDS = {
    "venue", "instrument_id", "canonical_multiplier", "quote_currency",
    "settlement_currency", "collateral_currency", "quote_to_usd",
    "valuation_source", "taker_fee_bps", "fee_source",
}
_SCANNER_FIELDS = (
    "venue", "instrument_id", "canonical_multiplier", "quote_to_usd",
    "valuation_source", "taker_fee_bps", "fee_source",
)


def _plain(value):
    """Copy finite public JSON and convert exact Decimal inputs to strings."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise EntropyUniverseError("public metadata contains a nonfinite decimal")
        return str(value)
    if isinstance(value, float):
        # API decoders should use Decimal, but ordinary finite JSON remains safe.
        if not Decimal(str(value)).is_finite():
            raise EntropyUniverseError("public metadata contains a nonfinite number")
        return value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {key: _plain(item) for key, item in value.items()}
    raise EntropyUniverseError("public metadata is not a JSON object")


def _number(value, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        return None
    try:
        result = Decimal(value)
    except InvalidOperation:
        return None
    if not result.is_finite() or result < 0 or (positive and result == 0):
        return None
    return result


def _text(value):
    return isinstance(value, str) and bool(value.strip()) and value == value.strip()


def parse_catalog(snapshot):
    """Parse perpDexs + io metaAndAssetCtxs + spotMeta, preserving every slot.

    Malformed envelopes and context misalignment fail closed. Individual market
    defects remain visible in the catalog, with reasons blocking scanner use.
    The absence of isDelisted means only 'not marked delisted', not a live book.
    """
    if not isinstance(snapshot, dict):
        raise EntropyUniverseError("Entropy snapshot must be an object")
    data = _plain(snapshot)
    slots = data.get("perp_dexs")
    if (not isinstance(slots, list) or not slots or slots[0] is not None
            or any(row is not None and not isinstance(row, dict) for row in slots)):
        raise EntropyUniverseError("perp_dexs must preserve its primary null slot")
    indexes = [index for index, row in enumerate(slots) if row is not None and row.get("name") == "io"]
    if len(indexes) != 1:
        raise EntropyUniverseError("perp_dexs must contain exactly one io deployer")
    dex_index = indexes[0]
    response = data.get("meta_and_asset_ctxs")
    if not isinstance(response, list) or len(response) != 2 or not isinstance(response[0], dict):
        raise EntropyUniverseError("io meta_and_asset_ctxs must contain meta and contexts")
    meta, contexts = response
    rows = meta.get("universe")
    if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
            or not isinstance(contexts, list) or any(not isinstance(row, dict) for row in contexts)):
        raise EntropyUniverseError("io universe and contexts must contain objects")
    if len(rows) != len(contexts):
        raise EntropyUniverseError("io universe/context count mismatch; indexes cannot be inferred")
    if len(rows) > 10000:
        raise EntropyUniverseError("io universe exceeds the protocol dex asset slot range")
    spot = data.get("spot_meta")
    tokens = spot.get("tokens") if isinstance(spot, dict) else None
    if not isinstance(tokens, list) or any(not isinstance(token, dict) for token in tokens):
        raise EntropyUniverseError("spot_meta must contain token objects")
    token_index = meta.get("collateralToken")
    candidates = [token for token in tokens if type(token.get("index")) is int and token["index"] == token_index]
    collateral = candidates[0] if type(token_index) is int and len(candidates) == 1 else None
    known_collateral = (collateral is not None and collateral.get("name") == "USDC"
                        and collateral.get("isCanonical") is True
                        and collateral.get("tokenId") == _MAINNET_USDC_TOKEN_ID)
    names = Counter(row.get("name") for row in rows if isinstance(row.get("name"), str))
    deployer = slots[dex_index]
    multipliers = deployer.get("assetToFundingMultiplier", [])
    if not isinstance(multipliers, list):
        multipliers = []
    assets = []
    for index, (row, context) in enumerate(zip(rows, contexts, strict=True)):
        native = row.get("name")
        reasons = []
        valid_native = isinstance(native, str) and bool(_NATIVE.fullmatch(native))
        if not valid_native:
            reasons.append("invalid_io_identity")
        elif names[native] != 1:
            reasons.append("duplicate_io_identity")
        delisted = row.get("isDelisted", False)
        if delisted is not False:
            reasons.append("delisted_or_unknown_status")
        if type(row.get("szDecimals")) is not int or not 0 <= row["szDecimals"] <= 6:
            reasons.append("invalid_quantity_precision")
        if not known_collateral:
            reasons.append("unknown_or_unsupported_collateral")
        funding_values = [pair[1] for pair in multipliers if isinstance(pair, list)
                          and len(pair) == 2 and pair[0] == native]
        assets.append({
            "universe_index": index,
            "asset_id": 100000 + dex_index * 10000 + index,
            "native_symbol": native,
            "instrument_id": f"{native}-USD-PERP.HYPERLIQUID" if valid_native else None,
            "metadata": row, "context": context,
            "status": "delisted" if delisted is True else (
                "not_marked_delisted" if delisted is False else "unknown"),
            "exclusion_reasons": reasons,
            "current_funding_fraction_per_hour": context.get("funding"),
            "predicted_funding_fraction_per_hour": None,
            "funding_multiplier": funding_values[0] if len(funding_values) == 1 else None,
        })
    return {"schema_version": 1, "dex": "io", "dex_index": dex_index,
            "dex_slots": slots, "deployer": deployer, "meta": meta,
            "collateral_token_index": token_index, "collateral_token": collateral,
            "assets": assets}


def _leg_reason(leg):
    if not isinstance(leg, dict) or set(leg) != _LEG_FIELDS:
        return "incomplete_or_unknown_leg_fields"
    if leg["venue"] not in ("ENTROPY", "ASTER"):
        return "unsupported_matching_venue"
    if not all(_text(leg[field]) for field in (
            "instrument_id", "valuation_source", "fee_source")):
        return "missing_identity_or_assumption_source"
    if any(leg[field] not in ("USD", "USDC", "USDT", "USD1") for field in (
            "quote_currency", "settlement_currency", "collateral_currency")):
        return "unknown_or_unsupported_currency"
    if any(_number(leg[field], positive=True) is None for field in ("canonical_multiplier", "quote_to_usd")):
        return "invalid_quantity_or_currency_conversion"
    fee = _number(leg["taker_fee_bps"])
    if fee is None or fee > 1000:
        return "invalid_fee_assumption"
    return None


def _aster_reason(row, leg):
    subtypes = row.get("underlyingSubType")
    if (type(row.get("symbolType")) is not int or row["symbolType"] != 1
            or row.get("underlyingType") != "COIN" or not isinstance(subtypes, list)
            or any(not isinstance(value, str) for value in subtypes)
            or "STOCK" not in subtypes or any(value in subtypes for value in (
                "ETF", "INDEX", "CRYPTO", "COMMODITY", "FX", "FOREX"))):
        return "counterleg_not_explicit_stock"
    if row.get("contractType") != "PERPETUAL" or row.get("status") != "TRADING":
        return "counterleg_inactive_or_nonperpetual"
    if (row.get("quoteAsset") != leg["quote_currency"]
            or row.get("marginAsset") != leg["collateral_currency"]
            or row.get("marginAsset") != leg["settlement_currency"]):
        return "counterleg_currency_mismatch"
    base = row.get("baseAsset")
    if not isinstance(base, str) or not _SYMBOL.fullmatch(base) or row.get("symbol") != base + row["quoteAsset"]:
        return "counterleg_inconsistent_native_identity"
    filters = row.get("filters")
    if not isinstance(filters, list) or any(not isinstance(item, dict) for item in filters):
        return "counterleg_invalid_precision_filters"
    for kind, fields in (("PRICE_FILTER", ("tickSize",)), ("LOT_SIZE", ("stepSize", "minQty"))):
        selected = [item for item in filters if item.get("filterType") == kind]
        if len(selected) != 1 or any(_number(selected[0].get(field), positive=True) is None for field in fields):
            return "counterleg_invalid_precision_filters"
    notional = [item for item in filters if item.get("filterType") == "MIN_NOTIONAL"]
    if len(notional) != 1 or _number(notional[0].get("notional"), positive=True) is None:
        return "counterleg_invalid_notional_filter"
    return None


def discover_matches(snapshot, matching_document, counter_metadata):
    """Return scanner fields and per-market evidence; never join by ticker.

    The first version supports explicit io equity / Aster stock-perpetual pairs.
    Empty/missing manifests yield a useful catalog with no inferred matches.
    Duplicate declarations and duplicate native metadata are all rejected.
    """
    catalog = parse_catalog(snapshot)
    if matching_document is None:
        matching_document = {"schema_version": 1, "dex": "io", "matches": []}
    if (not isinstance(matching_document, dict)
            or set(matching_document) != {"schema_version", "dex", "matches"}
            or type(matching_document.get("schema_version")) is not int
            or matching_document["schema_version"] != 1 or matching_document.get("dex") != "io"
            or not isinstance(matching_document.get("matches"), list)):
        raise EntropyUniverseError("matching document requires schema_version 1, dex io and matches")
    matches = _plain(matching_document)["matches"]
    if len(matches) > 500:
        raise EntropyUniverseError("matching document exceeds the scanner mapping cap")
    counters = counter_metadata if isinstance(counter_metadata, dict) else {}
    aster = counters.get("ASTER", {})
    aster_rows = aster.get("symbols", []) if isinstance(aster, dict) else []
    if not isinstance(aster_rows, list) or any(not isinstance(row, dict) for row in aster_rows):
        raise EntropyUniverseError("ASTER metadata requires symbols objects")
    symbol_counts = Counter(match.get("symbol") for match in matches
                            if isinstance(match, dict) and isinstance(match.get("symbol"), str))
    id_counts = Counter(leg.get("instrument_id") for match in matches if isinstance(match, dict)
                        and isinstance(match.get("legs"), list) for leg in match["legs"]
                        if isinstance(leg, dict) and isinstance(leg.get("instrument_id"), str))
    markets, evidence, rejected = [], [], []
    rejected_by_id = {}
    for index, match in enumerate(matches):
        reason = None
        legs = match.get("legs") if isinstance(match, dict) else None
        symbol = match.get("symbol") if isinstance(match, dict) else None
        if (not isinstance(match, dict) or set(match) != _MATCH_FIELDS
                or not isinstance(symbol, str) or not _SYMBOL.fullmatch(symbol)
                or not all(_text(match.get(field)) for field in _MATCH_FIELDS - {"legs", "symbol"})):
            reason = "incomplete_or_unknown_matching_evidence"
        elif match["product_class"] != "equity_perpetual":
            reason = "unsupported_product_class"
        elif not isinstance(legs, list) or len(legs) != 2:
            reason = "requires_two_explicit_legs"
        elif any(_leg_reason(leg) for leg in legs):
            reason = next(_leg_reason(leg) for leg in legs if _leg_reason(leg))
        elif {leg["venue"] for leg in legs} != {"ENTROPY", "ASTER"}:
            reason = "requires_entropy_and_aster"
        elif symbol_counts[symbol] != 1 or any(id_counts[leg["instrument_id"]] != 1 for leg in legs):
            reason = "duplicate_matching_declaration"
        if reason is None:
            entropy_leg = next(leg for leg in legs if leg["venue"] == "ENTROPY")
            counter_leg = next(leg for leg in legs if leg["venue"] == "ASTER")
            io_rows = [asset for asset in catalog["assets"] if asset["instrument_id"] == entropy_leg["instrument_id"]]
            native = counter_leg["instrument_id"].removesuffix("-PERP.ASTER")
            counter_rows = [row for row in aster_rows if row.get("symbol") == native]
            if len(io_rows) != 1:
                reason = "missing_or_duplicate_io_market"
            elif io_rows[0]["exclusion_reasons"]:
                reason = io_rows[0]["exclusion_reasons"][0]
            elif (entropy_leg["quote_currency"] != "USD" or any(
                    entropy_leg[field] != catalog["collateral_token"]["name"]
                    for field in ("settlement_currency", "collateral_currency"))):
                reason = "io_currency_mismatch"
            elif not counter_leg["instrument_id"].endswith("-PERP.ASTER") or len(counter_rows) != 1:
                reason = "missing_or_duplicate_counter_market"
            else:
                reason = _aster_reason(counter_rows[0], counter_leg)
        if reason is not None:
            rejected.append({"match_index": index, "symbol": symbol, "reason": reason})
            if isinstance(legs, list):
                for leg in legs:
                    if isinstance(leg, dict) and leg.get("venue") == "ENTROPY" and isinstance(leg.get("instrument_id"), str):
                        rejected_by_id.setdefault(leg["instrument_id"], []).append(reason)
            continue
        for leg in sorted(legs, key=lambda item: 0 if item["venue"] == "ENTROPY" else 1):
            market = {key: leg[key] for key in _SCANNER_FIELDS}
            market["symbol"] = symbol
            for field in ("canonical_multiplier", "quote_to_usd", "taker_fee_bps"):
                market[field] = str(leg[field])
            if leg["venue"] == "ENTROPY":
                row = io_rows[0]["metadata"]
                expected = {"raw_symbol": row["name"], "quote_currency": "USD",
                            "settlement_currency": catalog["collateral_token"]["name"],
                            "size_increment": str(Decimal(1).scaleb(-row["szDecimals"])),
                            "multiplier": "1"}
            else:
                row = counter_rows[0]
                lot = next(item for item in row["filters"] if item.get("filterType") == "LOT_SIZE")
                expected = {"raw_symbol": row["symbol"], "quote_currency": row["quoteAsset"],
                            "settlement_currency": row["marginAsset"],
                            "size_increment": str(lot["stepSize"]), "multiplier": "1"}
            market["expected_instrument"] = expected
            markets.append(market)
        evidence.append({"match_index": index, "declaration": _plain(match),
                         "io_asset_id": io_rows[0]["asset_id"],
                         "io_metadata": io_rows[0]["metadata"],
                         "precision_policy": "io: at most 5 significant figures (integers excepted) and 6-szDecimals price decimals; Aster: captured PRICE_FILTER/LOT_SIZE/MIN_NOTIONAL",
                         "aster_metadata": _plain(counter_rows[0]),
                         "verification": "explicit observation declaration; economic equivalence not independently verified"})
    markets.sort(key=lambda item: (item["symbol"], 0 if item["venue"] == "ENTROPY" else 1))
    mapped = {market["instrument_id"] for market in markets if market["venue"] == "ENTROPY"}
    exclusions = []
    for asset in catalog["assets"]:
        if asset["instrument_id"] in mapped:
            continue
        reasons = list(asset["exclusion_reasons"])
        reasons.extend(rejected_by_id.get(asset["instrument_id"], []))
        if not reasons:
            reasons.append("no_explicit_matching_declaration")
        exclusions.append({"native_symbol": asset["native_symbol"], "universe_index": asset["universe_index"],
                           "asset_id": asset["asset_id"], "reasons": list(dict.fromkeys(reasons))})
    summary = {"schema_version": 1, "scope": "public discovery and entry estimates; no accounts or trades",
               "catalog": catalog, "matching_evidence": evidence, "rejected_matches": rejected,
               "exclusions": exclusions, "symbol_count": len(evidence), "market_count": len(markets),
               "historical_samples_retained": 0, "live_connections_verified": False,
               "accounts_verified": False, "economic_equivalence_verified": False,
               "predicted_funding": "unknown; current context and deployer multiplier are recorded without rescaling",
               "fees_and_fx_are_assumptions": True}
    return markets, summary

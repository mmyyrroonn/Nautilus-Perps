"""Offline io discovery preserves indexes and rejects ambiguous observation legs."""
from copy import deepcopy
from decimal import Decimal
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from entropy_universe import EntropyUniverseError, discover_matches, parse_catalog
from opportunity_scan import parse_plan


def snapshot():
    return {
        "perp_dexs": [None, {"name": "other"}, None, {"name": "io", "assetToFundingMultiplier": [
            ["io:SNDK", "0.5"]]}],
        "meta_and_asset_ctxs": [{"collateralToken": 7, "universe": [
            {"name": "io:OLD", "szDecimals": 2, "isDelisted": True},
            {"name": "io:SNDK", "szDecimals": 4, "growthMode": "enabled", "deployerFeeScale": "1.0"},
            {"name": "io:NEW", "szDecimals": 1},
        ]}, [{"funding": "0.0"}, {"funding": "-0.0002", "markPx": "125"}, {"markPx": "2"}]],
        "spot_meta": {"tokens": [{"index": 100, "name": "OTHER"},
                                  {"index": 7, "name": "USDC", "isCanonical": True,
                                   "tokenId": "0x6d1e7cde53ba9467b783cb7c530ce054"}]},
    }


def aster_row(**changes):
    return {"symbol": "SNDKUSD1", "baseAsset": "SNDK", "quoteAsset": "USD1", "marginAsset": "USD1",
            "contractType": "PERPETUAL", "status": "TRADING", "underlyingType": "COIN", "symbolType": 1,
            "underlyingSubType": ["STOCK", "USD1-RWA"], "filters": [
                {"filterType": "PRICE_FILTER", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"},
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ], **changes}


def manifest():
    economics = {"canonical_multiplier": "1", "quote_to_usd": "1",
                 "valuation_source": "Configured nominal USD parity; stablecoin basis unverified",
                 "fee_source": "Configured public fee sample; account tier unverified"}
    return {"schema_version": 1, "dex": "io", "matches": [{
        "symbol": "SNDK", "product_class": "equity_perpetual", "underlying": "SanDisk Corp. common stock",
        "underlying_source": "https://docs.entropy.io/asset-directory/equity-assets",
        "oracle": "Entropy equity oracle; Aster oracle equivalence unverified",
        "trading_hours": "Observe synthetic perps; session differences unverified",
        "corporate_actions": "Observe; corporate action equivalence unverified",
        "notes": "Native unit 1 and nominal currency parity are observation assumptions, no hedge certification",
        "legs": [
            {"venue": "ENTROPY", "instrument_id": "io:SNDK-USD-PERP.HYPERLIQUID",
             "quote_currency": "USD", "settlement_currency": "USDC", "collateral_currency": "USDC",
             "taker_fee_bps": "0.9", **economics},
            {"venue": "ASTER", "instrument_id": "SNDKUSD1-PERP.ASTER", "quote_currency": "USD1",
             "settlement_currency": "USD1", "collateral_currency": "USD1", "taker_fee_bps": "1.25", **economics},
        ],
    }]}


def discover(data=None, document=None, row=None):
    return discover_matches(snapshot() if data is None else data,
                            manifest() if document is None else document,
                            {"ASTER": {"symbols": [aster_row() if row is None else row]}})


def test_catalog_keeps_null_dex_slots_and_original_delisted_asset_index():
    data = snapshot()
    catalog = parse_catalog(data)
    assert catalog["dex_index"] == 3
    assert catalog["dex_slots"] == data["perp_dexs"]
    assert catalog["collateral_token"]["index"] == 7
    assert catalog["collateral_token"]["name"] == "USDC"
    assert [asset["asset_id"] for asset in catalog["assets"]] == [130000, 130001, 130002]
    assert catalog["assets"][0]["status"] == "delisted"
    assert catalog["assets"][1]["status"] == "not_marked_delisted"
    assert catalog["assets"][1]["context"] == data["meta_and_asset_ctxs"][1][1]
    assert catalog["assets"][1]["current_funding_fraction_per_hour"] == "-0.0002"
    assert catalog["assets"][1]["funding_multiplier"] == "0.5"
    assert catalog["assets"][1]["predicted_funding_fraction_per_hour"] is None
    assert catalog["assets"][2]["current_funding_fraction_per_hour"] is None


def test_catalog_and_mapping_are_json_safe_and_do_not_mutate_inputs(tmp_path):
    data, document, counter = snapshot(), manifest(), {"ASTER": {"symbols": [aster_row()]}}
    data["meta_and_asset_ctxs"][1][1]["oraclePx"] = Decimal("125.0100")
    originals = deepcopy((data, document, counter))
    markets, summary = discover_matches(data, document, counter)
    assert (data, document, counter) == originals
    json.dumps(summary, allow_nan=False)
    assert summary["catalog"]["assets"][1]["context"]["oraclePx"] == "125.0100"
    assert len(markets) == 2
    assert parse_plan({"schema_version": 1, "markets": markets}, tmp_path).markets[0].canonical_multiplier == 1
    assert {market["venue"] for market in markets} == {"ENTROPY", "ASTER"}
    assert summary["matching_evidence"][0]["declaration"] == document["matches"][0]
    assert not summary["economic_equivalence_verified"] and not summary["accounts_verified"]
    assert not summary["live_connections_verified"]
    assert [item["native_symbol"] for item in summary["exclusions"]] == ["io:OLD", "io:NEW"]
    assert markets[1]["taker_fee_bps"] == "1.25"
    summary["catalog"]["assets"][1]["metadata"]["name"] = "changed"
    assert data["meta_and_asset_ctxs"][0]["universe"][1]["name"] == "io:SNDK"


@pytest.mark.parametrize("change", ["short_contexts", "long_contexts", "no_io", "duplicate_io", "no_primary_null"])
def test_malformed_or_misaligned_protocol_envelope_fails_closed(change):
    data = snapshot()
    if change == "short_contexts":
        data["meta_and_asset_ctxs"][1].pop()
    elif change == "long_contexts":
        data["meta_and_asset_ctxs"][1].append({})
    elif change == "no_io":
        data["perp_dexs"].pop()
    elif change == "duplicate_io":
        data["perp_dexs"].append({"name": "io"})
    else:
        data["perp_dexs"].pop(0)
    with pytest.raises(EntropyUniverseError):
        parse_catalog(data)


@pytest.mark.parametrize("value", [-1, 7, True, "4", None])
def test_invalid_io_quantity_precision_excludes_matching_asset(value):
    data = snapshot()
    data["meta_and_asset_ctxs"][0]["universe"][1]["szDecimals"] = value
    markets, summary = discover(data)
    assert not markets
    assert summary["rejected_matches"][0]["reason"] == "invalid_quantity_precision"


@pytest.mark.parametrize("value", [True, "false", None, 0])
def test_io_delisted_or_unknown_explicit_status_never_maps(value):
    data = snapshot()
    data["meta_and_asset_ctxs"][0]["universe"][1]["isDelisted"] = value
    markets, summary = discover(data)
    assert not markets
    assert summary["rejected_matches"][0]["reason"] == "delisted_or_unknown_status"


@pytest.mark.parametrize("change", ["unknown", "duplicate", "noncanonical", "bool_index", "missing_id", "fake_same_name"])
def test_collateral_resolves_exact_token_index_and_rejects_unknown_units(change):
    data = snapshot()
    if change == "unknown":
        data["meta_and_asset_ctxs"][0]["collateralToken"] = 8
    elif change == "duplicate":
        data["spot_meta"]["tokens"].append(deepcopy(data["spot_meta"]["tokens"][1]))
    elif change == "noncanonical":
        data["spot_meta"]["tokens"][1]["isCanonical"] = False
    elif change == "bool_index":
        data["meta_and_asset_ctxs"][0]["collateralToken"] = True
    elif change == "missing_id":
        del data["spot_meta"]["tokens"][1]["tokenId"]
    else:
        data["spot_meta"]["tokens"][1]["tokenId"] = "0xfakesamename"
    markets, summary = discover(data)
    assert not markets
    assert summary["rejected_matches"][0]["reason"] == "unknown_or_unsupported_collateral"


def test_missing_manifest_never_infers_same_ticker_equivalence():
    markets, summary = discover_matches(snapshot(), None, {"ASTER": {"symbols": [aster_row()]}})
    assert not markets
    assert len(summary["exclusions"]) == 3
    assert summary["exclusions"][1]["reasons"] == ["no_explicit_matching_declaration"]


@pytest.mark.parametrize("changes,reason", [
    ({"symbolType": 0, "underlyingSubType": []}, "counterleg_not_explicit_stock"),
    ({"symbolType": True}, "counterleg_not_explicit_stock"),
    ({"underlyingSubType": ["STOCK", "ETF"]}, "counterleg_not_explicit_stock"),
    ({"status": "SETTLING"}, "counterleg_inactive_or_nonperpetual"),
    ({"contractType": "CURRENT_QUARTER"}, "counterleg_inactive_or_nonperpetual"),
    ({"quoteAsset": "USDT"}, "counterleg_currency_mismatch"),
    ({"marginAsset": None}, "counterleg_currency_mismatch"),
    ({"filters": []}, "counterleg_invalid_precision_filters"),
    ({"baseAsset": "OTHER"}, "counterleg_inconsistent_native_identity"),
])
def test_counterleg_must_be_stock_perpetual_with_exact_native_units(changes, reason):
    markets, summary = discover(row=aster_row(**changes))
    assert not markets
    assert summary["rejected_matches"][0]["reason"] == reason


@pytest.mark.parametrize("change", ["zero_tick", "duplicate_lot", "missing_minqty", "missing_notional", "zero_notional"])
def test_counterleg_precision_and_minimum_notional_are_public_validated(change):
    row = aster_row()
    reason = "counterleg_invalid_precision_filters"
    if change == "zero_tick":
        row["filters"][0]["tickSize"] = "0"
    elif change == "duplicate_lot":
        row["filters"].append(deepcopy(row["filters"][1]))
    elif change == "missing_minqty":
        del row["filters"][1]["minQty"]
    else:
        reason = "counterleg_invalid_notional_filter"
        if change == "missing_notional":
            row["filters"].pop()
        else:
            row["filters"][2]["notional"] = "0"
    markets, summary = discover(row=row)
    assert not markets and summary["rejected_matches"][0]["reason"] == reason


@pytest.mark.parametrize("field,value,reason", [
    ("canonical_multiplier", "0", "invalid_quantity_or_currency_conversion"),
    ("quote_to_usd", None, "invalid_quantity_or_currency_conversion"),
    ("quote_currency", "EUR", "unknown_or_unsupported_currency"),
    ("settlement_currency", "USDT", "io_currency_mismatch"),
    ("fee_source", "", "missing_identity_or_assumption_source"),
    ("taker_fee_bps", "NaN", "invalid_fee_assumption"),
])
def test_manifest_assumptions_are_explicit_not_defaulted(field, value, reason):
    document = manifest()
    document["matches"][0]["legs"][0][field] = value
    markets, summary = discover(document=document)
    assert not markets and summary["rejected_matches"][0]["reason"] == reason


def test_quantity_conversion_is_explicit_not_funding_multiplier():
    document = manifest()
    document["matches"][0]["legs"][0]["canonical_multiplier"] = "0.125"
    markets, summary = discover(document=document)
    assert markets[0]["canonical_multiplier"] == "0.125"
    assert markets[0]["expected_instrument"]["multiplier"] == "1"
    assert summary["catalog"]["assets"][1]["funding_multiplier"] == "0.5"
    assert summary["catalog"]["assets"][1]["current_funding_fraction_per_hour"] == "-0.0002"


def test_expected_instrument_is_complete_and_derived_from_native_metadata():
    data = snapshot()
    data["meta_and_asset_ctxs"][0]["universe"][1]["szDecimals"] = 3
    row = aster_row()
    row["filters"][1]["stepSize"] = Decimal("0.0200")
    markets, _ = discover(data=data, row=row)
    assert markets[0]["expected_instrument"] == {
        "raw_symbol": "io:SNDK", "quote_currency": "USD", "settlement_currency": "USDC",
        "size_increment": "0.001", "multiplier": "1"}
    assert markets[1]["expected_instrument"] == {
        "raw_symbol": "SNDKUSD1", "quote_currency": "USD1", "settlement_currency": "USD1",
        "size_increment": "0.0200", "multiplier": "1"}
    expected_keys = {"raw_symbol", "quote_currency", "settlement_currency", "size_increment", "multiplier"}
    assert all(set(market["expected_instrument"]) == expected_keys for market in markets)
    assert all(isinstance(value, str) for market in markets for value in market["expected_instrument"].values())


@pytest.mark.parametrize("where", ["io", "aster", "manifest", "reused_counter"])
def test_duplicates_do_not_silently_select_one_market(where):
    data, document, counter = snapshot(), manifest(), {"ASTER": {"symbols": [aster_row()]}}
    if where == "io":
        data["meta_and_asset_ctxs"][0]["universe"].append(deepcopy(data["meta_and_asset_ctxs"][0]["universe"][1]))
        data["meta_and_asset_ctxs"][1].append({})
    elif where == "aster":
        counter["ASTER"]["symbols"].append(aster_row())
    else:
        document["matches"].append(deepcopy(document["matches"][0]))
        if where == "reused_counter":
            document["matches"][1]["symbol"] = "OTHER"
            document["matches"][1]["legs"][0]["instrument_id"] = "io:NEW-USD-PERP.HYPERLIQUID"
    markets, summary = discover_matches(data, document, counter)
    assert not markets
    assert summary["rejected_matches"]
    assert any("duplicate" in item["reason"] for item in summary["rejected_matches"])


def test_missing_counterleg_metadata_is_rejected_with_market_reason():
    markets, summary = discover_matches(snapshot(), manifest(), {})
    assert not markets
    assert summary["rejected_matches"][0]["reason"] == "missing_or_duplicate_counter_market"
    assert "missing_or_duplicate_counter_market" in summary["exclusions"][1]["reasons"]


@pytest.mark.parametrize("change", ["missing_unit", "unknown_field", "missing_oracle", "crypto_product", "missing_native"])
def test_incomplete_manifest_never_guesses_identity_or_economics(change):
    document = manifest()
    match = document["matches"][0]
    if change == "missing_unit":
        del match["legs"][0]["canonical_multiplier"]
    elif change == "unknown_field":
        match["legs"][0]["size_multiplier"] = "1"
    elif change == "missing_oracle":
        del match["oracle"]
    elif change == "crypto_product":
        match["product_class"] = "crypto_perpetual"
    else:
        match["legs"][0]["instrument_id"] = "io:ABSENT-USD-PERP.HYPERLIQUID"
    markets, summary = discover(document=document)
    assert not markets and summary["rejected_matches"]


def test_catalog_does_not_hardcode_io_asset_names_or_slot():
    data = snapshot()
    data["perp_dexs"].insert(1, {"name": "future"})
    data["meta_and_asset_ctxs"][0]["universe"][2]["name"] = "io:FUTURE"
    catalog = parse_catalog(data)
    assert catalog["dex_index"] == 4
    assert catalog["assets"][2]["asset_id"] == 140002
    assert catalog["assets"][2]["instrument_id"] == "io:FUTURE-USD-PERP.HYPERLIQUID"

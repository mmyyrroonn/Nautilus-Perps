"""Public identity classification, bounded registry sizing and offline generation."""
from copy import deepcopy
from decimal import Decimal
import io
import json
from pathlib import Path
import sys
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opportunity_universe as universe
from opportunity_scan import parse_plan


def hl(symbol):
    return {"name": symbol, "szDecimals": 3}


def lighter(symbol, **changes):
    return {"symbol": symbol, "market_id": 1, "market_type": "perp", "status": "active",
            "is_frozen": False, "multiplier": "1", "operator_account_index": 1, **changes}


def aster(symbol, **changes):
    return {"symbol": symbol + "USDT", "baseAsset": symbol, "quoteAsset": "USDT", "marginAsset": "USDT",
            "contractType": "PERPETUAL", "status": "TRADING", "underlyingType": "COIN",
            "symbolType": 0, "underlyingSubType": [], **changes}


def ondo(symbol, **changes):
    return {"market": symbol + "-USD.P", "tags": ["Crypto"], "baseIncrement": "0.001",
            "quoteIncrement": "0.01", "takerFee": "0.00025", **changes}


def backpack(symbol, **changes):
    return {"symbol": symbol + "_USDC_PERP", "baseSymbol": symbol, "quoteSymbol": "USDC",
            "marketType": "PERP", "orderBookState": "Open", "visible": True, "rwaMarketType": None,
            "filters": {"price": {"tickSize": "0.01"}, "quantity": {"stepSize": "0.001", "minQuantity": "0.001"}},
            **changes}


def metadata(symbols=("BTC", "ETH")):
    return {"HL": {"universe": [hl(symbol) for symbol in symbols]},
            "LIGHTER": {"code": 200, "order_books": [lighter(symbol) for symbol in symbols]},
            "ASTER": {"symbols": [aster(symbol) for symbol in symbols]},
            "ONDO": {"success": True, "result": {"perps": {"tradingPairs": [ondo(symbol) for symbol in symbols]}}},
            "BACKPACK": [backpack(symbol) for symbol in symbols]}


def rows(data, venue):
    return {"HL": lambda: data[venue]["universe"], "LIGHTER": lambda: data[venue]["order_books"],
            "ASTER": lambda: data[venue]["symbols"], "ONDO": lambda: data[venue]["result"]["perps"]["tradingPairs"],
            "BACKPACK": lambda: data[venue]}[venue]()


def generate(data=None, **kwargs):
    return universe.generate_document(metadata() if data is None else data,
                                       generated_at="2026-10-06T00:00:00+00:00", **kwargs)


def test_valid_crypto_registry_round_trips_and_validates_without_native(tmp_path):
    document, summary = generate()
    parsed = tomllib.loads(universe.render_toml(document))
    assert parsed == document
    plan = parse_plan(parsed, tmp_path)
    assert len(plan.markets) == 10
    assert summary["symbol_count"] == 2 and summary["market_count"] == 10
    assert plan.connection_timeout_secs == 120
    assert plan.max_levels_per_side == 20000 and plan.depth_levels == 20
    assert plan.output_path.suffix == ".jsonl"
    assert summary["historical_samples_retained"] == 0
    assert not summary["live_connections_verified"]
    assert all(m.canonical_multiplier == 1 for m in plan.markets)
    assert list(tmp_path.iterdir()) == []


def test_native_ids_preserve_hl_case_and_do_not_merge_thousand_aliases():
    data = metadata()
    rows(data, "HL").extend([hl("kPEPE"), hl("PEPE")])
    rows(data, "LIGHTER").append(lighter("kPEPE"))
    rows(data, "ASTER").append(aster("1000PEPE"))
    rows(data, "BACKPACK").append(backpack("PEPE"))
    document, summary = generate(data)
    ids = {m["instrument_id"] for m in document["markets"]}
    assert "kPEPE-USD-PERP.HYPERLIQUID" in ids
    assert "KPEPE-PERP.LIGHTER" in ids
    assert "PEPE_USDC_PERP.BACKPACK" in ids
    assert "1000PEPEUSDT-PERP.ASTER" not in ids
    assert "KPEPE-USD-PERP.HYPERLIQUID" not in ids
    assert "not merged" in summary["alias_policy"]


def test_equity_spot_inactive_and_frozen_never_become_crypto_anchors():
    data = metadata()
    rows(data, "HL").extend([hl("xyz:NVDA"), {**hl("DELIST"), "isDelisted": True}])
    rows(data, "ASTER").extend([
        aster("NVDA", symbolType=1, underlyingSubType=["STOCK"]),
        aster("GOLD", underlyingSubType=["Commodities"]),
        aster("INACTIVE", status="BREAK"), aster("DELIVERY", contractType="CURRENT_MONTH"),
        aster("NOCLASS", symbolType=None),
    ])
    rows(data, "ONDO").extend([ondo("NVDA", tags=["Stock"]), ondo("DISABLED", disabled=True)])
    rows(data, "BACKPACK").extend([backpack("NVDA", rwaMarketType="Stock"),
        backpack("SPOT", marketType="SPOT"), backpack("HIDDEN", visible=False)])
    rows(data, "LIGHTER").extend([lighter("NVDA"), lighter("DELIST"), lighter("FROZEN", is_frozen=True),
                                  lighter("SPOT", market_type="spot"), lighter("INACTIVE", status="inactive")])
    document, summary = generate(data)
    assert {m["symbol"] for m in document["markets"]} == {"BTC", "ETH"}
    assert summary["exclusions"]["ASTER"]["noncrypto_or_unknown_classification"] == 3
    assert summary["exclusions"]["LIGHTER"]["inactive_spot_or_frozen"] == 3
    assert summary["exclusions"]["LIGHTER"]["unanchored_or_ambiguous_asset"] == 2


def test_aster_coin_field_does_not_admit_live_schema_stock_rows():
    data = metadata()
    rows(data, "ASTER").append(aster("COIN", symbolType=1, underlyingSubType=["STOCK"]))
    rows(data, "LIGHTER").append(lighter("COIN", operator_account_index=1))
    document, summary = generate(data)
    assert "COIN" not in {m["symbol"] for m in document["markets"]}
    assert summary["lighter_public_operator_counts"] == {"1": 3}
    assert "not a classifier" in summary["lighter_identity_policy"]


def test_lighter_known_stock_collision_is_rejected_even_with_crypto_anchor():
    data = metadata(("BTC", "COIN"))
    rows(data, "ASTER").append(aster("COIN", symbolType=1, underlyingSubType=["STOCK"]))
    document, summary = generate(data)
    assert not any(m["venue"] == "LIGHTER" and m["symbol"] == "COIN" for m in document["markets"])
    assert summary["exclusions"]["LIGHTER"]["unanchored_or_ambiguous_asset"] == 1


def test_venue_and_symbol_subsets_keep_only_two_leg_symbols():
    data = metadata()
    rows(data, "ASTER").append(aster("ONLYASTER"))
    document, summary = generate(data, venues=("ASTER", "LIGHTER"), symbols=("BTC", "ONLYASTER"))
    assert len(document["markets"]) == 2
    assert {m["venue"] for m in document["markets"]} == {"ASTER", "LIGHTER"}
    assert summary["symbol_count"] == 1
    assert summary["exclusions"]["ASTER"]["fewer_than_two_venues"] == 1
    with pytest.raises(universe.UniverseError, match="two selected"):
        generate(data, venues=("ASTER",))


def test_ambiguous_duplicate_market_choices_are_excluded_not_selected():
    data = metadata()
    rows(data, "HL").append(hl("BTC"))
    document, summary = generate(data)
    assert not any(m["venue"] == "HL" and m["symbol"] == "BTC" for m in document["markets"])
    assert summary["exclusions"]["HL"]["ambiguous_duplicate_market"] == 2
    assert any(m["symbol"] == "BTC" for m in document["markets"])


def test_public_fee_group_and_native_fee_provenance():
    document, _ = generate(metadata(("BTC", "AEON")))
    markets = {(m["symbol"], m["venue"]): m for m in document["markets"]}
    assert markets["BTC", "ASTER"]["taker_fee_bps"] == "4"
    assert markets["AEON", "ASTER"]["taker_fee_bps"] == "10"
    assert universe.ASTER_FEES_URL in markets["AEON", "ASTER"]["fee_source"]
    assert markets["BTC", "HL"]["taker_fee_bps"] == "4.5"
    assert markets["BTC", "BACKPACK"]["taker_fee_bps"] == "5"
    assert "unverified" in markets["BTC", "BACKPACK"]["fee_source"]
    assert markets["BTC", "BACKPACK"]["backpack_economics"]["taker_fee"] == "0.0005"
    for venue in ("LIGHTER", "ONDO"):
        assert "taker_fee_bps" not in markets["BTC", venue]
        assert "fee_source" not in markets["BTC", venue]


def test_nonunit_lighter_wire_multiplier_is_not_silently_assumed():
    data = metadata()
    rows(data, "LIGHTER")[0]["multiplier"] = "1.0017"
    document, summary = generate(data)
    assert not any(m["symbol"] == "BTC" and m["venue"] == "LIGHTER" for m in document["markets"])
    assert summary["exclusions"]["LIGHTER"]["unsupported_nonunit_multiplier"] == 1


def test_backpack_cap_does_not_admit_single_leg_symbols():
    data = metadata(tuple(f"T{i:03}" for i in range(150)))
    document, summary = generate(data, venues=("HL", "BACKPACK"))
    assert summary["symbol_count"] == 100 and summary["market_count"] == 200
    assert summary["per_venue_counts"]["BACKPACK"] == 100
    assert len({m["symbol"] for m in document["markets"]}) == 100


def test_total_cap_reserves_comparability_and_represents_multiple_venues():
    document, summary = generate(metadata(tuple(f"T{i:03}" for i in range(400))))
    assert summary["symbol_count"] == 400
    assert summary["market_count"] == 1000
    assert summary["per_venue_counts"]["BACKPACK"] <= 100
    assert all(summary["per_venue_counts"][venue] > 0 for venue in universe.VENUES)
    for symbol in {m["symbol"] for m in document["markets"]}:
        assert sum(m["symbol"] == symbol for m in document["markets"]) >= 2


def test_generation_is_deterministic_under_input_order_and_does_not_mutate_metadata():
    data = metadata()
    original = deepcopy(data)
    first = generate(data)
    assert data == original
    reordered = {venue: data[venue] for venue in reversed(universe.VENUES)}
    for venue in universe.VENUES:
        rows(reordered, venue).reverse()
    assert generate(reordered)[0] == first[0]
    assert original == metadata()
    assert universe.render_toml(first[0]) == universe.render_toml(generate()[0])


@pytest.mark.parametrize("venue,invalid", [
    ("HL", {}), ("HL", {"universe": [None]}), ("LIGHTER", {"code": 500, "order_books": []}),
    ("LIGHTER", {"code": 200, "order_books": None}), ("ASTER", {"symbols": {}}),
    ("ONDO", {"success": False, "result": {}}), ("ONDO", {"success": True, "result": {}}),
    ("BACKPACK", {}),
])
def test_malformed_responses_fail_closed(venue, invalid):
    data = metadata()
    data[venue] = invalid
    with pytest.raises(universe.UniverseError):
        generate(data)


@pytest.mark.parametrize("changes", [{"status": None}, {"disabled": None}, {"baseIncrement": "NaN"}, {"takerFee": None}])
def test_malformed_ondo_market_is_excluded_without_guessing(changes):
    data = metadata()
    rows(data, "ONDO")[0].update(changes)
    document, _ = generate(data)
    assert not any(m["symbol"] == "BTC" and m["venue"] == "ONDO" for m in document["markets"])


@pytest.mark.parametrize("changes", [{"symbolType": True}, {"underlyingSubType": None}, {"marginAsset": "USD1"}])
def test_malformed_aster_classification_or_margin_is_excluded(changes):
    data = metadata()
    rows(data, "ASTER")[0].update(changes)
    document, _ = generate(data)
    assert not any(m["symbol"] == "BTC" and m["venue"] == "ASTER" for m in document["markets"])


def save_metadata(path, data):
    path.mkdir()
    for venue in universe.VENUES:
        (path / universe.FILES[venue]).write_text(json.dumps(data[venue]), encoding="utf-8")


def test_offline_load_provenance_and_cli_dry_run_creates_nothing(tmp_path, capsys, monkeypatch):
    inputs = tmp_path / "inputs"
    save_metadata(inputs, metadata())
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected network or native import")
    monkeypatch.setattr(universe, "fetch_metadata", forbidden)
    output = tmp_path / "absent" / "generated.toml"
    summary_path = tmp_path / "absent" / "generated.json"
    assert universe.main(["--metadata-dir", str(inputs), "--output", str(output),
                          "--summary", str(summary_path), "--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["market_count"] == 10
    assert all(len(value["sha256"]) == 64 for value in summary["inputs"].values())
    assert summary["inputs"]["HL"]["url"] == universe.URLS["HL"]
    assert not output.parent.exists()


def test_output_venue_selection_keeps_unselected_public_rwa_collision_evidence(tmp_path, capsys):
    data = metadata(("BTC", "BB"))
    rows(data, "ONDO")[1]["tags"] = ["Stock"]
    inputs = tmp_path / "inputs"
    save_metadata(inputs, data)
    loaded, _ = universe.load_metadata(inputs, ("HL", "LIGHTER"))
    assert set(loaded) == set(universe.VENUES)
    assert universe.main(["--metadata-dir", str(inputs), "--output", str(tmp_path / "absent.toml"),
                          "--venues", "HL,LIGHTER", "--dry-run"]) == 0
    summary = json.loads(capsys.readouterr().out)
    assert summary["symbol_count"] == 1
    assert summary["market_count"] == 2
    assert summary["exclusions"]["LIGHTER"]["unanchored_or_ambiguous_asset"] == 1


def test_backpack_lowercase_k_namespace_is_excluded_without_renaming():
    data = metadata()
    rows(data, "BACKPACK").append(backpack("kPEPE"))
    document, summary = generate(data)
    assert not any(m["instrument_id"] == "kPEPE_USDC_PERP.BACKPACK" for m in document["markets"])
    assert summary["exclusions"]["BACKPACK"]["unsupported_native_case"] == 1


def test_requested_cli_outputs_are_scan_compatible_and_summary_is_optional(tmp_path, capsys):
    inputs = tmp_path / "inputs"
    save_metadata(inputs, metadata())
    output = tmp_path / "generated" / "broad.toml"
    assert universe.main(["--metadata-dir", str(inputs), "--output", str(output), "--venues", "HL,ASTER", "--symbols", "BTC"]) == 0
    parsed = tomllib.loads(output.read_text(encoding="utf-8"))
    assert len(parse_plan(parsed, output.parent).markets) == 2
    assert list(output.parent.iterdir()) == [output]
    capsys.readouterr()


def test_invalid_json_and_unknown_venues_are_reported_without_outputs(tmp_path, capsys):
    inputs = tmp_path / "inputs"
    save_metadata(inputs, metadata())
    (inputs / universe.FILES["HL"]).write_text('{"universe":NaN}', encoding="utf-8")
    output = tmp_path / "absent" / "broad.toml"
    assert universe.main(["--metadata-dir", str(inputs), "--output", str(output)]) == 2
    assert "valid finite JSON" in capsys.readouterr().err
    assert universe.main(["--metadata-dir", str(inputs), "--output", str(output), "--venues", "UNKNOWN"]) == 2
    assert not output.parent.exists()


def test_direct_fetch_uses_only_fixed_public_urls_and_no_auth(monkeypatch):
    requests = []
    data = metadata()
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            venue = next(venue for venue, url in universe.URLS.items() if url == request.full_url)
            assert timeout == 30
            assert not request.has_header("Authorization")
            return io.BytesIO(json.dumps(data[venue]).encode())
    monkeypatch.setattr(universe.urllib.request, "build_opener", lambda *handlers: Opener())
    fetched, provenance = universe.fetch_metadata(("HL", "ASTER"))
    assert set(fetched) == {"HL", "ASTER"}
    assert requests[0].data == b'{"type":"meta"}'
    assert requests[1].data is None
    assert all(len(value["sha256"]) == 64 for value in provenance.values())


def test_generated_config_and_summary_paths_must_not_collide(tmp_path, capsys):
    inputs = tmp_path / "inputs"
    save_metadata(inputs, metadata())
    output = tmp_path / "same.toml"
    assert universe.main(["--metadata-dir", str(inputs), "--output", str(output), "--summary", str(output)]) == 2
    assert "distinct paths" in capsys.readouterr().err
    assert not output.exists()

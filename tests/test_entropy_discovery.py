"""io CLI replay, source integrity and runtime contract admission."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tomllib

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opportunity_universe as universe
from opportunity_runtime import market_metadata
from opportunity_scan import ScanConfigError, parse_plan
from test_entropy_universe import aster_row, manifest, snapshot


def data():
    return {"ENTROPY": snapshot(), "ASTER": {"symbols": [aster_row()]}}


def generate(public=None, matches=None):
    return universe.generate_document(data() if public is None else public,
        venues=("ENTROPY", "ASTER"), dex="io", entropy_matches=manifest() if matches is None else matches,
        generated_at="2026-10-08T09:00:00+00:00")


def save_inputs(path):
    path.mkdir()
    for venue, document in data().items():
        (path / universe.FILES[venue]).write_text(json.dumps(document), encoding="utf-8")
    matches = path / "matches.json"
    matches.write_text(json.dumps(manifest()), encoding="utf-8")
    return matches


def test_io_generation_retains_full_scope_expected_units_and_assumptions(tmp_path):
    public = data()
    original = deepcopy(public)
    document, summary = generate(public)
    plan = parse_plan(tomllib.loads(universe.render_toml(document)), tmp_path)
    assert public == original
    assert len(plan.markets) == 2
    assert {market.client_id for market in plan.markets} == {"HYPERLIQUID", "ASTER"}
    entropy = next(market for market in plan.markets if market.venue == "ENTROPY")
    assert entropy.instrument_id == "io:SNDK-USD-PERP.HYPERLIQUID"
    assert entropy.expected_instrument["size_increment"] == "0.0001"
    assert entropy.expected_instrument["settlement_currency"] == "USDC"
    assert summary["per_venue_counts"] == {"ASTER": 1, "ENTROPY": 1}
    assert summary["entropy"]["catalog"]["dex_index"] == 3
    assert summary["entropy"]["catalog"]["assets"][1]["funding_multiplier"] == "0.5"
    assert summary["entropy"]["matching_evidence"][0]["declaration"] == manifest()["matches"][0]
    assert "not independent" in summary["platform_risk_note"]
    assert not summary["live_connections_verified"]
    assert len(summary["entropy_matches_sha256"]) == 64
    json.dumps(summary, allow_nan=False)


@pytest.mark.parametrize("dex,venues,matches", [
    (None, ("ENTROPY", "ASTER"), manifest()),
    ("io", ("ASTER",), manifest()),
    (None, ("ASTER",), manifest()),
])
def test_io_requires_an_explicit_dex_and_venue_selection(dex, venues, matches):
    with pytest.raises(universe.UniverseError):
        universe.generate_document(data(), venues=venues, dex=dex, entropy_matches=matches)


def test_equity_matching_cannot_join_an_automatic_crypto_group():
    public = data()
    public["HL"] = {"universe": [{"name": "SNDK", "szDecimals": 3}]}
    public["ASTER"]["symbols"].append({"symbol": "SNDKUSDT", "baseAsset": "SNDK",
        "quoteAsset": "USDT", "marginAsset": "USDT", "contractType": "PERPETUAL",
        "status": "TRADING", "underlyingType": "COIN", "symbolType": 0, "underlyingSubType": []})
    with pytest.raises(universe.UniverseError, match="collide"):
        universe.generate_document(public, venues=("HL", "ENTROPY", "ASTER"), dex="io", entropy_matches=manifest())


def test_offline_dry_run_does_not_import_native_connect_or_create_outputs(tmp_path, capsys, monkeypatch):
    inputs = tmp_path / "inputs"
    matches = save_inputs(inputs)
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected network")
    monkeypatch.setattr(universe, "fetch_metadata", forbidden)
    output = tmp_path / "absent" / "scan.toml"
    summary = tmp_path / "absent" / "summary.json"
    assert universe.main(["--dex", "io", "--metadata-dir", str(inputs), "--entropy-matches", str(matches),
        "--output", str(output), "--summary", str(summary), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["market_count"] == 2
    assert result["entropy_matches_input"]["sha256"] == hashlib.sha256(matches.read_bytes()).hexdigest()
    assert not output.parent.exists()


def test_catalog_only_lists_unmatched_and_delisted_assets_without_inventing_pairs(tmp_path, capsys):
    inputs = tmp_path / "inputs"
    save_inputs(inputs)
    summary = tmp_path / "absent" / "catalog.json"
    assert universe.main(["--dex", "io", "--metadata-dir", str(inputs), "--catalog-only",
                          "--summary", str(summary), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert len(result["entropy"]["assets"]) == 3
    assert result["entropy"]["assets"][0]["status"] == "delisted"
    assert result["catalog_only"] and not result["scanner_config_generated"]
    assert not summary.parent.exists()


def test_cli_writes_a_valid_matched_plan_and_only_requested_evidence(tmp_path, capsys):
    inputs = tmp_path / "inputs"
    matches = save_inputs(inputs)
    output = tmp_path / "out" / "scan.toml"
    summary = output.with_suffix(".json")
    assert universe.main(["--dex", "io", "--metadata-dir", str(inputs), "--entropy-matches", str(matches),
                          "--output", str(output), "--summary", str(summary)]) == 0
    assert len(parse_plan(tomllib.loads(output.read_text()), output.parent).markets) == 2
    assert json.loads(summary.read_text())["entropy"]["market_count"] == 2
    assert set(output.parent.iterdir()) == {output, summary}
    capsys.readouterr()


@pytest.mark.parametrize("options", [
    ["--dex", "io", "--output", "scan.toml"],
    ["--venues", "ENTROPY,ASTER", "--output", "scan.toml"],
    ["--dex", "io", "--venues", "HL,ASTER", "--output", "scan.toml"],
    ["--catalog-only"],
    ["--dex", "io", "--catalog-only", "--output", "scan.toml"],
    ["--dex", "io", "--catalog-only", "--entropy-matches", "matches.json"],
])
def test_invalid_cli_scope_fails_before_fetching(options, monkeypatch, capsys):
    def forbidden(*args, **kwargs):
        raise AssertionError("invalid config fetched public metadata")
    monkeypatch.setattr(universe, "fetch_metadata", forbidden)
    assert universe.main(options) == 2
    assert "[universe]" in capsys.readouterr().err


def capture_fixture(path, monkeypatch):
    public = data()
    requests = []
    class Opener:
        def open(self, request, timeout):
            requests.append(request)
            assert not request.has_header("Authorization") and not request.has_header("Cookie")
            assert timeout == 30
            if request.full_url == universe.URLS["ASTER"]:
                assert request.data is None
                response = public["ASTER"]
            else:
                assert request.full_url == universe.URLS["ENTROPY"]
                body = json.loads(request.data)
                field = {"perpDexs": "perp_dexs", "metaAndAssetCtxs": "meta_and_asset_ctxs",
                         "spotMeta": "spot_meta"}[body["type"]]
                if body["type"] == "metaAndAssetCtxs":
                    assert body["dex"] == "io"
                response = public["ENTROPY"][field]
            return io.BytesIO(json.dumps(response).encode())
    monkeypatch.setattr(universe.urllib.request, "build_opener", lambda *handlers: Opener())
    fetched, provenance = universe.fetch_metadata(("ENTROPY", "ASTER"), dex="io", capture_dir=path)
    return fetched, provenance, requests


def test_public_capture_and_replay_preserve_component_identity_request_and_time(tmp_path, monkeypatch):
    fetched, provenance, requests = capture_fixture(tmp_path, monkeypatch)
    assert len(requests) == 4
    replayed, replay_provenance = universe.load_metadata(tmp_path, ("ENTROPY", "ASTER"))
    assert replayed == fetched
    assert replay_provenance["ENTROPY"]["capture"] == provenance["ENTROPY"]
    components = replay_provenance["ENTROPY"]["capture"]["components"]
    assert all(component["fetched_at_utc"] for component in components.values())
    assert components["meta_and_asset_ctxs"]["request"] == {"type": "metaAndAssetCtxs", "dex": "io"}
    assert len(replay_provenance["ENTROPY"]["provenance_sha256"]) == 64


@pytest.mark.parametrize("tamper", ["raw_component", "wrapper", "request_dex", "counterleg"])
def test_replay_fails_closed_for_tampered_sources(tmp_path, monkeypatch, tamper):
    capture_fixture(tmp_path, monkeypatch)
    if tamper == "raw_component":
        (tmp_path / "entropy-perp-dexs.json").write_text("[]")
    elif tamper == "wrapper":
        path = tmp_path / universe.FILES["ENTROPY"]
        changed = json.loads(path.read_text())
        changed["meta_and_asset_ctxs"][0]["universe"][1]["szDecimals"] = 3
        path.write_text(json.dumps(changed))
    elif tamper == "counterleg":
        (tmp_path / universe.FILES["ASTER"]).write_text('{"symbols":[]}')
    else:
        path = tmp_path / "public-metadata-provenance.json"
        changed = json.loads(path.read_text())
        changed["ENTROPY"]["components"]["meta_and_asset_ctxs"]["request"]["dex"] = "xyz"
        path.write_text(json.dumps(changed))
    with pytest.raises(universe.UniverseError, match="provenance|snapshot"):
        universe.load_metadata(tmp_path, ("ENTROPY", "ASTER"))


@pytest.mark.parametrize("side", ["ENTROPY", "ASTER"])
def test_every_io_comparison_leg_requires_a_complete_runtime_contract(tmp_path, side):
    document, _ = generate()
    del next(market for market in document["markets"] if market["venue"] == side)["expected_instrument"]
    with pytest.raises(ScanConfigError, match="expected_instrument"):
        parse_plan(document, tmp_path)


@pytest.mark.parametrize("field,value", [
    ("raw_symbol", "xyz:SNDK"), ("quote_currency", "USDC"), ("settlement_currency", "USDT"),
    ("size_increment", Decimal("0.001")), ("multiplier", Decimal("1000")),
])
def test_runtime_rejects_instrument_scope_or_native_unit_drift(tmp_path, field, value):
    plan = parse_plan(generate()[0], tmp_path)
    market = next(market for market in plan.markets if market.venue == "ENTROPY")
    instrument = SimpleNamespace(id=market.instrument_id, raw_symbol="io:SNDK", quote_currency="USD",
        settlement_currency="USDC", size_increment=Decimal("0.0001"), multiplier=Decimal("1"),
        min_quantity=None, min_notional=None, is_inverse=False)
    accepted = market_metadata(market, instrument)
    assert accepted.size_increment == Decimal("0.0001") and accepted.multiplier == 1
    setattr(instrument, field, value)
    with pytest.raises(ScanConfigError, match=field):
        market_metadata(market, instrument)

#!/usr/bin/env python3
"""
Offline tests for ``src/ondo_preflight.py`` (the Ondo public metadata preflight).

No network and no credentials: the HTTP client is a fake, the payload shapes come
from plan 4.1, and the production client can only be imported when the candidate
wheel is installed. The suite must pass with *and* without that wheel.

    .venv\\Scripts\\python.exe -m pytest tests/test_ondo_preflight.py -q
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ondo_preflight  # noqa: E402
import spread_watch  # noqa: E402


ONDO_ID = "NVDA-USD-PERP.ONDO"
TSLA_ONDO_ID = "TSLA-USD-PERP.ONDO"

# plan 4.1: GET /v1/markets -> perps.tradingPairs, with baseIncrement (size step)
# and quoteIncrement (price step); the fee/status fields are the documented
# candidates the preflight probes for, not a hard-coded single name.
MARKETS = {
    "perps": {
        "tradingPairs": [
            {
                "market": "NVDA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
                "status": "active", "takerFeeRate": "0.00025", "makerFeeRate": "0.0001",
            },
            {
                "market": "TSLA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
                "status": "active", "takerFeeRate": "0.00025",
            },
        ],
    },
}
CONTRACTS = [
    {"market": "NVDA-USD.P", "takerFeeRate": "0.00025", "makerFeeRate": "0.0001"},
    {"market": "TSLA-USD.P", "takerFeeRate": "0.00025"},
]
STATUS = {"status": "ok", "serverTime": "2026-09-14T12:00:00Z"}
INSTRUMENTS = [{"id": ONDO_ID, "market": "NVDA-USD.P"}, {"id": TSLA_ONDO_ID, "market": "TSLA-USD.P"}]


class FakeClient:
    """Stand-in for ``OndoHttpClient``: records the calls, can fail per endpoint.

    ``aio`` selects whether the four methods are coroutines (the real runtime
    shape) or plain functions - the preflight must accept both because the
    generated stub prints them as sync ``def`` (Stage 3a caveat).
    """

    def __init__(self, *, status=STATUS, markets=MARKETS, contracts=CONTRACTS,
                 instruments=INSTRUMENTS, error=None, aio=True) -> None:
        self._status = status
        self._markets = markets
        self._contracts = contracts
        self._instruments = instruments
        self._error = error or {}
        self.calls: list[str] = []
        if aio:
            self.get_status = self._async("get_status", self._status)
            self.get_markets = self._async("get_markets", self._markets)
            self.get_contracts = self._async("get_contracts", self._contracts)
            self.load_instrument_definitions = self._async(
                "load_instrument_definitions", self._instruments,
            )

    def _async(self, name, value):
        async def call(*_args):
            self.calls.append(name)
            failure = self._error.get(name)
            if failure is not None:
                raise failure
            return value
        return call

    def get_status(self) -> object:  # replaced in __init__ when aio=True
        self.calls.append("get_status")
        failure = self._error.get("get_status")
        if failure is not None:
            raise failure
        return self._status

    def get_markets(self) -> object:
        self.calls.append("get_markets")
        failure = self._error.get("get_markets")
        if failure is not None:
            raise failure
        return self._markets

    def get_contracts(self) -> object:
        self.calls.append("get_contracts")
        failure = self._error.get("get_contracts")
        if failure is not None:
            raise failure
        return self._contracts

    def load_instrument_definitions(self, load_ids) -> object:
        self.calls.append("load_instrument_definitions")
        failure = self._error.get("load_instrument_definitions")
        if failure is not None:
            raise failure
        return self._instruments


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def fake_adapter():
    """What ``load_adapter`` returns when the candidate wheel is installed."""
    return ondo_preflight.OndoAdapter(OndoEnvironment=object(), OndoHttpClient=FakeClient())


# ---------------------------------------------------------------------------- targets


def test_targets_are_the_two_planned_markets():
    rows = {row["symbol"]: row for row in ondo_preflight.targets(["NVDA", "TSLA"])}
    assert rows["NVDA"]["raw_market"] == "NVDA-USD.P"
    assert rows["NVDA"]["instrument_id"] == ONDO_ID
    assert rows["TSLA"]["raw_market"] == "TSLA-USD.P"
    assert rows["TSLA"]["instrument_id"] == TSLA_ONDO_ID
    for row in rows.values():
        assert row["venue"] == "ONDO"
        assert row["client_id"] == "ONDO"
    # The raw market is the venue's name, the instrument id is Nautilus's.
    assert rows["NVDA"]["raw_market"] != rows["NVDA"]["instrument_id"]


def test_a_symbol_outside_the_plan_is_rejected():
    with pytest.raises(ondo_preflight.OndoPreflightError):
        ondo_preflight.targets(["AAPL"])


def test_only_the_two_mapped_symbols_have_markets():
    assert sorted(spread_watch.ONDO_RAW_MARKETS) == ["NVDA", "TSLA"]
    assert ondo_preflight.targets(["NVDA", "TSLA"])


# ------------------------------------------------------------------------ dry run


def test_dry_run_prints_the_plan_and_touches_nothing(tmp_path, capsys):
    out_dir = tmp_path / "preflight"
    with patch.object(ondo_preflight, "load_adapter", return_value=fake_adapter()) as load, \
            patch("socket.socket.connect",
                  side_effect=AssertionError("dry-run must not open a socket")), \
            patch("dotenv.load_dotenv",
                  side_effect=AssertionError("ondo_preflight must never read .env")):
        code = ondo_preflight.main(
            ["--symbols", "NVDA,TSLA", "--out", str(out_dir), "--dry-run"],
        )
    assert code == 0
    load.assert_called_once()
    assert not out_dir.exists(), "dry-run must not create the output directory"
    plan = json.loads(capsys.readouterr().out)
    assert plan["mode"] == "dry-run"
    assert plan["environment"] == "production"
    assert [t["instrument_id"] for t in plan["targets"]] == [ONDO_ID, TSLA_ONDO_ID]
    assert plan["load_ids"] == [ONDO_ID, TSLA_ONDO_ID]


def test_dry_run_does_not_construct_the_http_client(tmp_path, capsys):
    calls: list[str] = []

    class Exploding:
        def __init__(self, *args, **kwargs):
            calls.append("constructed")
            raise AssertionError("dry-run must not construct the HTTP client")

    adapter = ondo_preflight.OndoAdapter(OndoEnvironment=object(), OndoHttpClient=Exploding)
    with patch.object(ondo_preflight, "load_adapter", return_value=adapter):
        code = ondo_preflight.main(["--out", str(tmp_path / "p"), "--dry-run"])
    assert code == 0
    assert calls == []
    assert json.loads(capsys.readouterr().out)["mode"] == "dry-run"


def test_adapter_missing_is_reported_with_a_build_pointer(tmp_path, capsys):
    missing = ondo_preflight.AdapterMissing(
        spread_watch._adapter_missing("ONDO", ondo_preflight.ONDO_ADAPTER),
    )
    with patch.object(ondo_preflight, "load_adapter", side_effect=missing):
        code = ondo_preflight.main(
            ["--symbols", "NVDA", "--out", str(tmp_path / "p"), "--dry-run"],
        )
    err = capsys.readouterr().err
    assert code == 2, "a missing adapter is an environment fault, not a data fault"
    assert spread_watch._adapter_missing("ONDO", "nautilus_trader.adapters.ondo") in err
    assert "_adapter_missing" in err
    assert ondo_preflight.BUILD_POINTER in err
    assert "uv pip install --python" in err
    assert not (tmp_path / "p").exists()


def test_the_real_loader_returns_the_adapter_or_says_it_is_missing():
    """With or without the wheel: never a bare ImportError, always a decision."""
    try:
        adapter = ondo_preflight.load_adapter()
    except ondo_preflight.AdapterMissing as exc:
        assert spread_watch._adapter_missing("ONDO", ondo_preflight.ONDO_ADAPTER) in str(exc)
        return
    assert adapter.OndoEnvironment is not None
    assert adapter.OndoHttpClient is not None


# ---------------------------------------------------------------------- report build


def test_report_records_hashes_fetch_time_and_a_normalized_table(tmp_path):
    client = FakeClient()
    code = ondo_preflight.run(["NVDA", "TSLA"], tmp_path / "out", client=client)
    assert code == 0
    out = tmp_path / "out"
    for name in ("status", "markets", "contracts", "instruments", "missing", "meta"):
        assert (out / f"{name}.json").exists(), name

    assert read_json(out / "status.json") == STATUS
    assert read_json(out / "markets.json") == MARKETS
    assert read_json(out / "contracts.json") == CONTRACTS
    assert read_json(out / "missing.json") == []
    assert client.calls == ["get_status", "get_markets", "get_contracts",
                           "load_instrument_definitions"]

    table = read_json(out / "instruments.json")
    rows = {row["symbol"]: row for row in table}
    assert rows["NVDA"]["instrument_id"] == ONDO_ID
    assert rows["NVDA"]["raw_market"] == "NVDA-USD.P"
    assert rows["NVDA"]["size_increment"] == "0.001", "baseIncrement is the size step"
    assert rows["NVDA"]["price_increment"] == "0.01", "quoteIncrement is the price step"
    assert rows["NVDA"]["quote_currency"] == "USD"
    assert rows["NVDA"]["settlement_currency"] == "USDC"
    assert rows["NVDA"]["is_inverse"] is False
    assert rows["NVDA"]["taker_fee_bps"] == "2.5", "0.00025 as an exact decimal rate"
    assert rows["NVDA"]["fee_source"].startswith("market_fields:")
    assert rows["NVDA"]["market_status"] == "active"

    meta = read_json(out / "meta.json")
    assert meta["tool"] == "ondo_preflight"
    assert meta["venue"] == "ONDO"
    assert meta["environment"] == "production"
    assert meta["symbols"] == ["NVDA", "TSLA"]
    assert meta["fetched_at_utc"].endswith("+00:00")
    assert isinstance(meta["fetched_at_ns"], int) and meta["fetched_at_ns"] > 0
    assert meta["missing"] == []
    for name in ("status", "markets", "contracts", "instruments", "missing"):
        digest = hashlib.sha256((out / f"{name}.json").read_bytes()).hexdigest()
        assert meta["files"][name]["sha256"] == digest, name
        assert meta["files"][name]["bytes"] == (out / f"{name}.json").stat().st_size


def test_exact_decimals_survive_the_normalized_table(tmp_path):
    """No f64 anywhere: a 6-decimal increment must come back byte-identical."""
    markets = {"perps": {"tradingPairs": [{
        "market": "NVDA-USD.P", "baseIncrement": "0.000001", "quoteIncrement": "0.00001",
        "status": "active", "takerFeeRate": "0.0000063",
    }, {
        "market": "TSLA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
        "status": "active", "takerFeeRate": "0.00025",
    }]}}
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out", client=FakeClient(markets=markets),
    )
    assert code == 0
    rows = {row["symbol"]: row for row in read_json(tmp_path / "out" / "instruments.json")}
    assert rows["NVDA"]["size_increment"] == "0.000001"
    assert rows["NVDA"]["price_increment"] == "0.00001"
    assert rows["NVDA"]["taker_fee_bps"] == "0.063", "0.0000063 * 1e4 exactly"


def test_a_sync_client_is_accepted_like_the_generated_stub_prints_it(tmp_path):
    client = FakeClient(aio=False)
    code = ondo_preflight.run(["NVDA", "TSLA"], tmp_path / "out", client=client)
    assert code == 0
    assert read_json(tmp_path / "out" / "missing.json") == []


# ------------------------------------------------------------------- failure modes


def test_a_request_failure_is_never_an_empty_market_success(tmp_path):
    forbidden = RuntimeError("HTTP 403 Forbidden: GET /v1/markets -> {'error':'Forbidden'}")
    client = FakeClient(error={"get_markets": forbidden})
    code = ondo_preflight.run(["NVDA", "TSLA"], tmp_path / "out", client=client)
    assert code != 0
    assert not (tmp_path / "out" / "markets.json").exists()
    assert not (tmp_path / "out" / "missing.json").exists(), (
        "a 403 must not be written up as 'no markets'"
    )


def test_a_403_shaped_error_from_any_endpoint_fails(tmp_path):
    """A 403 fails the run and produces no payload - but it does produce its own status.

    The assertion that ``meta.json`` is absent moved with F12: a failed attempt now
    publishes ``complete: false`` so that a *previous* success can never be read as the
    state of this attempt. What must still not exist is any payload: an error body is
    never written up as an empty market.
    """
    for endpoint in ("get_status", "get_markets", "get_contracts",
                     "load_instrument_definitions"):
        out_dir = tmp_path / f"out-{endpoint}"
        client = FakeClient(error={endpoint: RuntimeError("HTTP 403 Forbidden")})
        code = ondo_preflight.run(["NVDA"], out_dir, client=client)
        assert code != 0, endpoint
        for name in ondo_preflight.PAYLOAD_FILES:
            assert not (out_dir / f"{name}.json").exists(), (endpoint, name)
        meta = read_json(out_dir / "meta.json")
        assert meta["complete"] is False, endpoint
        assert endpoint in meta["failure"], meta
        assert meta["files"] == {}, "a failed read writes no payload to describe"


def test_a_markets_body_without_the_perps_path_is_a_failure(tmp_path):
    client = FakeClient(markets={"error": "Forbidden"})
    code = ondo_preflight.run(["NVDA", "TSLA"], tmp_path / "out", client=client)
    assert code != 0
    assert not (tmp_path / "out" / "markets.json").exists()


def test_an_empty_trading_pairs_list_is_a_failure_not_an_empty_success(tmp_path):
    client = FakeClient(markets={"perps": {"tradingPairs": []}}, instruments=[])
    code = ondo_preflight.run(["NVDA", "TSLA"], tmp_path / "out", client=client)
    assert code != 0


def test_a_missing_target_market_is_recorded_and_fails(tmp_path):
    markets = {"perps": {"tradingPairs": [MARKETS["perps"]["tradingPairs"][1]]}}
    client = FakeClient(markets=markets)
    code = ondo_preflight.run(["NVDA", "TSLA"], tmp_path / "out", client=client)
    assert code != 0
    missing = read_json(tmp_path / "out" / "missing.json")
    fields = {(m["symbol"], m["field"]) for m in missing}
    assert ("NVDA", "market") in fields, missing
    assert all(m["market"] == "NVDA-USD.P" for m in missing if m["symbol"] == "NVDA")


def test_a_missing_fee_is_recorded_and_fails(tmp_path):
    markets = {"perps": {"tradingPairs": [
        {"market": "NVDA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "status": "active"},
        {"market": "TSLA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "status": "active"},
    ]}}
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out",
        client=FakeClient(markets=markets, contracts=[]),
    )
    assert code != 0
    missing = read_json(tmp_path / "out" / "missing.json")
    assert {(m["symbol"], m["field"]) for m in missing} >= {
        ("NVDA", "taker_fee"), ("TSLA", "taker_fee"),
    }
    rows = {row["symbol"]: row for row in read_json(tmp_path / "out" / "instruments.json")}
    assert rows["NVDA"]["taker_fee_bps"] is None
    assert rows["NVDA"]["fee_source"] == "missing"


def test_a_missing_status_and_no_disabled_flag_is_enabled_by_convention(tmp_path):
    """The venue emits no status string: absence of `disabled` means enabled.

    The 2026-09-14 capture (81 markets) carries no `status` key at all, 20 markets
    with `"disabled": true` and the other 61 omitting the key, so a target with
    neither signal is enabled by convention - not a missing field.
    """
    markets = {"perps": {"tradingPairs": [
        {"market": "NVDA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "takerFeeRate": "0.00025"},
        {"market": "TSLA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "takerFeeRate": "0.00025"},
    ]}}
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out", client=FakeClient(markets=markets),
    )
    assert code == 0, "no status string is not a missing field under the venue convention"
    assert read_json(tmp_path / "out" / "missing.json") == []
    rows = {row["symbol"]: row for row in read_json(tmp_path / "out" / "instruments.json")}
    for symbol in ("NVDA", "TSLA"):
        assert rows[symbol]["market_status"] == "active"
        assert rows[symbol]["market_status_raw"] is None
        source = rows[symbol]["market_status_source"]
        assert "venue_convention" in source, source
        assert "no status string" in source and "disabled" in source, source
    # the counts behind the convention belong in the run's notes (meta.json)
    convention = read_json(tmp_path / "out" / "meta.json")["market_status_convention"]
    assert convention["markets"] == 2
    assert convention["with_status_string"] == 0
    assert convention["disabled_true"] == 0
    assert convention["pinned_capture_2026_09_14"]["with_status_string"] == 0
    assert convention["pinned_capture_2026_09_14"]["with_disabled_true"] == 20


def test_a_disabled_flag_is_recorded_as_a_recognised_state_not_a_missing_field(tmp_path):
    markets = {"perps": {"tradingPairs": [
        {"market": "NVDA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "disabled": True, "takerFeeRate": "0.00025"},
        {"market": "TSLA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "disabled": False, "takerFeeRate": "0.00025"},
    ]}}
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out", client=FakeClient(markets=markets),
    )
    assert code == 0, "a known disabled/active state is recorded, not a missing item"
    assert read_json(tmp_path / "out" / "missing.json") == []
    rows = {row["symbol"]: row for row in read_json(tmp_path / "out" / "instruments.json")}
    assert rows["NVDA"]["market_status"] == "disabled"
    assert rows["NVDA"]["market_status_raw"] is True
    assert rows["NVDA"]["market_status_source"] == "market_fields:disabled"
    assert rows["TSLA"]["market_status"] == "active"
    assert rows["TSLA"]["market_status_raw"] is False
    convention = read_json(tmp_path / "out" / "meta.json")["market_status_convention"]
    assert convention["with_status_string"] == 0
    assert convention["disabled_true"] == 1
    assert convention["disabled_false"] == 1


def test_a_status_string_is_still_mapped_through_the_adapter_rule(tmp_path):
    markets = {"perps": {"tradingPairs": [
        {"market": "NVDA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "status": "halted", "takerFeeRate": "0.00025"},
        {"market": "TSLA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "status": "active", "takerFeeRate": "0.00025"},
    ]}}
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out", client=FakeClient(markets=markets),
    )
    assert code == 0
    rows = {row["symbol"]: row for row in read_json(tmp_path / "out" / "instruments.json")}
    assert rows["NVDA"]["market_status"] == "disabled"
    assert rows["NVDA"]["market_status_source"] == "market_fields:status"
    assert rows["TSLA"]["market_status"] == "active"


def test_a_malformed_disabled_flag_is_recorded_and_fails(tmp_path):
    """A non-boolean `disabled` cannot tell enabled from disabled: fail closed."""
    markets = {"perps": {"tradingPairs": [
        {"market": "NVDA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "disabled": "yes", "takerFeeRate": "0.00025"},
        {"market": "TSLA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "takerFeeRate": "0.00025"},
    ]}}
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out", client=FakeClient(markets=markets),
    )
    assert code != 0
    missing = read_json(tmp_path / "out" / "missing.json")
    assert ("NVDA", "market_status") in {(m["symbol"], m["field"]) for m in missing}
    rows = {row["symbol"]: row for row in read_json(tmp_path / "out" / "instruments.json")}
    assert rows["NVDA"]["market_status"] == "unknown"
    assert rows["NVDA"]["market_status_raw"] == "yes"
    # TSLA alone is a clean target: enabled by convention, no status item for it
    assert all(m["symbol"] != "TSLA" for m in missing if m["field"] == "market_status")


def test_an_unknown_status_value_is_not_tradable(tmp_path):
    markets = {"perps": {"tradingPairs": [
        {"market": "NVDA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "status": "weird-new-state", "takerFeeRate": "0.00025"},
        {"market": "TSLA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "status": "active", "takerFeeRate": "0.00025"},
    ]}}
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out", client=FakeClient(markets=markets),
    )
    assert code != 0, "an unrecognised status value still fails closed"
    missing = read_json(tmp_path / "out" / "missing.json")
    assert ("NVDA", "market_status") in {(m["symbol"], m["field"]) for m in missing}
    rows = {row["symbol"]: row for row in read_json(tmp_path / "out" / "instruments.json")}
    assert rows["NVDA"]["market_status"] == "unknown"
    assert rows["NVDA"]["market_status_raw"] == "weird-new-state"
    assert rows["NVDA"]["market_status_source"] == "market_fields:status"


def test_a_missing_increment_is_recorded_and_fails(tmp_path):
    markets = {"perps": {"tradingPairs": [
        {"market": "NVDA-USD.P", "baseIncrement": "0.001", "status": "active",
         "takerFeeRate": "0.00025"},
        {"market": "TSLA-USD.P", "baseIncrement": "0.001", "quoteIncrement": "0.01",
         "status": "active", "takerFeeRate": "0.00025"},
    ]}}
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out", client=FakeClient(markets=markets),
    )
    assert code != 0
    missing = read_json(tmp_path / "out" / "missing.json")
    assert ("NVDA", "quoteIncrement") in {(m["symbol"], m["field"]) for m in missing}


def test_no_instrument_definitions_for_a_requested_target_fails(tmp_path):
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out",
        client=FakeClient(instruments=[{"id": ONDO_ID}]),
    )
    assert code != 0
    missing = read_json(tmp_path / "out" / "missing.json")
    assert ("TSLA", "instrument") in {(m["symbol"], m["field"]) for m in missing}


def test_zero_instrument_definitions_fail_even_when_markets_look_fine(tmp_path):
    code = ondo_preflight.run(
        ["NVDA", "TSLA"], tmp_path / "out", client=FakeClient(instruments=[]),
    )
    assert code != 0


# --------------------------------------------------- run isolation and publishing (F12)


def test_a_failed_run_after_a_success_never_leaves_the_old_success_in_place(tmp_path):
    """F12: the same --out, a success then a missing-field failure.

    Before the fix the payloads were overwritten while ``meta.json`` was written only for
    a complete run, so the directory held the old ``complete: true`` manifest, the old
    hashes and the new, non-empty ``missing.json`` at the same time.
    """
    out_dir = tmp_path / "preflight"
    assert ondo_preflight.run(
        ["NVDA", "TSLA"], out_dir, client=FakeClient(), run_id="run-ok",
    ) == 0
    good = read_json(out_dir / "meta.json")
    assert good["run_id"] == "run-ok" and good["complete"] is True
    assert ondo_preflight.verify_run(out_dir)["verified"] is True

    markets = {"perps": {"tradingPairs": [MARKETS["perps"]["tradingPairs"][1]]}}
    assert ondo_preflight.run(
        ["NVDA", "TSLA"], out_dir, client=FakeClient(markets=markets), run_id="run-bad",
    ) == 1

    meta = read_json(out_dir / "meta.json")
    assert meta["run_id"] == "run-bad", "the published view is the attempt that just ran"
    assert meta["complete"] is False, "the old success must not survive as the current state"
    assert meta["missing"], "the new failure names its missing items"
    assert read_json(out_dir / "missing.json"), "the payload belongs to the current attempt"
    verdict = ondo_preflight.verify_run(out_dir)
    assert verdict["run_id"] == "run-bad"
    assert verdict["verified"] is False and verdict["complete"] is False
    # Both attempts remain readable under their own run ids, neither overwritten.
    runs = sorted(p.name for p in (out_dir / "runs").iterdir())
    assert runs == ["run-bad", "run-ok"]
    assert read_json(out_dir / "runs" / "run-ok" / "meta.json")["complete"] is True
    assert read_json(out_dir / "runs" / "run-bad" / "meta.json")["complete"] is False

    # And the success→success direction still replaces the view cleanly.
    assert ondo_preflight.run(
        ["NVDA", "TSLA"], out_dir, client=FakeClient(), run_id="run-ok-2",
    ) == 0
    assert ondo_preflight.verify_run(out_dir)["verified"] is True
    assert read_json(out_dir / "meta.json")["run_id"] == "run-ok-2"


def test_a_transport_failure_after_a_success_publishes_that_failure(tmp_path):
    """F12: a transport error is a result too - it must not leave the old run standing."""
    out_dir = tmp_path / "preflight"
    assert ondo_preflight.run(
        ["NVDA", "TSLA"], out_dir, client=FakeClient(), run_id="run-ok",
    ) == 0
    assert ondo_preflight.verify_run(out_dir)["verified"] is True

    forbidden = RuntimeError("HTTP 403 Forbidden: GET /v1/markets -> {'error':'Forbidden'}")
    assert ondo_preflight.run(
        ["NVDA", "TSLA"], out_dir, client=FakeClient(error={"get_markets": forbidden}),
        run_id="run-403",
    ) == 1

    meta = read_json(out_dir / "meta.json")
    assert meta["run_id"] == "run-403"
    assert meta["complete"] is False and "403" in meta["failure"]
    assert meta["files"] == {}, "nothing is described that was not read"
    for name in ondo_preflight.PAYLOAD_FILES:
        assert not (out_dir / f"{name}.json").exists(), (
            f"{name}.json is a previous run's payload and this attempt produced none: "
            f"it must not be left where the current manifest points"
        )
    assert read_json(out_dir / "runs" / "run-403" / "meta.json")["complete"] is False
    assert not ondo_preflight.verify_run(out_dir)["verified"]


def test_a_run_is_staged_under_its_run_id_before_it_is_published(tmp_path, monkeypatch):
    """F12: run-scoped staging, then one rename - never a file-by-file rewrite of --out."""
    out_dir = tmp_path / "preflight"
    seen: dict[str, object] = {}
    real_write_json = ondo_preflight._write_json

    def spy(path, payload):
        path = Path(path)
        if ondo_preflight.RUNS_DIRNAME in path.parts and path.name != "meta.json":
            staged = [p for p in path.parts if p.startswith(ondo_preflight.STAGING_PREFIX)]
            seen.setdefault("staged", []).append((staged[0] if staged else None, path.name))
        return real_write_json(path, payload)

    monkeypatch.setattr(ondo_preflight, "_write_json", spy)
    assert ondo_preflight.run(
        ["NVDA", "TSLA"], out_dir, client=FakeClient(), run_id="run-1",
    ) == 0
    assert seen["staged"], "every payload is written inside the run's staging directory"
    assert {entry[0] for entry in seen["staged"]} == {".staging-run-1"}
    assert not list((out_dir / "runs").glob(f"{ondo_preflight.STAGING_PREFIX}*")), (
        "the staging directory is gone once the run is published"
    )
    manifest = read_json(out_dir / "runs" / "run-1" / "meta.json")
    assert manifest["run_dir"] == "runs/run-1"
    assert manifest["run_id"] == "run-1"


def test_a_crash_in_the_middle_of_publishing_is_visible_to_the_reader(tmp_path, monkeypatch):
    """F12: an interruption between the payloads and the manifest fails verification.

    The commit point is the atomic replacement of ``<out>/meta.json``. If the process dies
    after the payloads are in place and before the manifest is switched, the manifest that
    is still there describes a *different* run whose hashes no longer match what is on the
    disk - so the reader sees a mismatch, not a tidy success.
    """
    out_dir = tmp_path / "preflight"
    assert ondo_preflight.run(
        ["NVDA", "TSLA"], out_dir, client=FakeClient(), run_id="run-ok",
    ) == 0
    good_manifest = read_json(out_dir / "meta.json")

    class Crash(RuntimeError):
        pass

    real_write_json = ondo_preflight._write_json

    def dying_write_json(path, payload):
        """Let the payloads land, then die before the published manifest is switched."""
        path = Path(path)
        if path.parent == out_dir and path.name == "meta.json":
            raise Crash("the process died between the payloads and the manifest")
        return real_write_json(path, payload)

    markets = {"perps": {"tradingPairs": [MARKETS["perps"]["tradingPairs"][1]]}}
    with monkeypatch.context() as patched:
        patched.setattr(ondo_preflight, "_write_json", dying_write_json)
        with pytest.raises(Crash):
            ondo_preflight.run(
                ["NVDA", "TSLA"], out_dir, client=FakeClient(markets=markets),
                run_id="run-2",
            )

    # The old manifest is still published and its hashes no longer describe the directory.
    assert read_json(out_dir / "meta.json") == good_manifest
    verdict = ondo_preflight.verify_run(out_dir)
    assert verdict["run_id"] == "run-ok"
    assert verdict["verified"] is False, (
        "a half-published attempt must be detectable, never read as the old success"
    )
    assert any("sha256" in problem for problem in verdict["problems"]), verdict["problems"]
    # The interrupted attempt *did* get its own immutable run directory - that rename is
    # the first step of publishing, and it is complete in itself - but the published view
    # never switched to it, which is exactly what the mismatch above says.
    assert read_json(out_dir / "runs" / "run-2" / "meta.json")["complete"] is False


def test_the_reader_verifies_the_run_identity_and_every_hash(tmp_path):
    """F12: `complete: true` is checked against the bytes, not taken on trust."""
    out_dir = tmp_path / "preflight"
    assert ondo_preflight.run(
        ["NVDA", "TSLA"], out_dir, client=FakeClient(), run_id="run-ok",
    ) == 0
    verdict = ondo_preflight.verify_run(out_dir)
    assert verdict["verified"] is True and verdict["complete"] is True
    assert verdict["run_id"] == "run-ok"
    assert verdict["problems"] == []

    meta = read_json(out_dir / "meta.json")
    assert meta["run_id"] == "run-ok"
    assert set(meta["files"]) == set(ondo_preflight.PAYLOAD_FILES)

    # A payload edited after publishing no longer matches the manifest it was published with.
    (out_dir / "instruments.json").write_text("[]\n", encoding="utf-8")
    verdict = ondo_preflight.verify_run(out_dir)
    assert verdict["verified"] is False
    assert any("sha256" in problem for problem in verdict["problems"])

    # …and so is a manifest that names a file which is not there at all.
    (out_dir / "instruments.json").unlink()
    verdict = ondo_preflight.verify_run(out_dir)
    assert verdict["verified"] is False
    assert any("missing" in problem for problem in verdict["problems"])

    # A directory that was never published at all reports why, rather than guessing.
    verdict = ondo_preflight.verify_run(tmp_path / "nothing-here")
    assert verdict["verified"] is False and verdict["problems"]


def test_a_manifest_whose_run_id_and_run_dir_disagree_is_not_verified(tmp_path):
    """F12: the run identity is *checked*, not merely reported back to the reader.

    Every attempt publishes both a run id and the run directory that id belongs to. A
    manifest whose two halves name different attempts is not one run's record, so a reader
    must not be handed the run id it claims - the point of naming an identity at all.
    """
    out_dir = tmp_path / "preflight"
    assert ondo_preflight.run(
        ["NVDA", "TSLA"], out_dir, client=FakeClient(), run_id="run-ok",
    ) == 0
    meta = read_json(out_dir / "meta.json")
    assert meta["run_dir"] == f"{ondo_preflight.RUNS_DIRNAME}/run-ok"

    # Re-point the run id while the run directory still names the original attempt.
    meta["run_id"] = "run-somebody-else"
    (out_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    verdict = ondo_preflight.verify_run(out_dir)
    assert verdict["verified"] is False
    assert any("identity" in problem for problem in verdict["problems"]), verdict

    # The payloads themselves are untouched, so this is the identity check firing and not
    # the hash check: put the two halves back together and it verifies again.
    meta["run_id"] = "run-ok"
    (out_dir / "meta.json").write_text(json.dumps(meta), encoding="utf-8")
    assert ondo_preflight.verify_run(out_dir)["verified"] is True


def test_every_run_id_is_unique_per_attempt():
    first = ondo_preflight.new_run_id(datetime(2026, 9, 15, 12, 0, 0, 1, tzinfo=timezone.utc))
    second = ondo_preflight.new_run_id(datetime(2026, 9, 15, 12, 0, 0, 2, tzinfo=timezone.utc))
    assert first == "20260915T120000000001Z"
    assert first != second
    assert second > first, "run ids sort by when the attempt started"


# --------------------------------------------------------------------- hygiene


def _source() -> str:
    return (Path(__file__).resolve().parents[1] / "src" / "ondo_preflight.py").read_text(
        encoding="utf-8",
    )


def test_it_never_reads_dotenv_or_the_environment():
    source = _source()
    assert "load_dotenv" not in source
    assert "dotenv" not in source
    assert "os.environ" not in source
    assert "getenv" not in source


def test_it_reuses_the_adapter_http_client_instead_of_a_second_one():
    source = _source()
    assert "OndoHttpClient" in source
    assert "urllib" not in source, "plan 4.1 forbids a second urllib/requests client"
    assert "requests" not in source
    assert "apscheduler" not in source


def test_it_never_builds_an_execution_client():
    source = _source()
    assert "add_exec_client" not in source
    assert "ExecutionClient" not in source


def test_the_default_targets_are_the_two_planned_symbols():
    parser_defaults = ondo_preflight.parse_args([])
    assert parser_defaults.symbols == "NVDA,TSLA"
    assert parser_defaults.environment == "production"
    assert parser_defaults.dry_run is False


def test_main_writes_nothing_when_the_symbol_list_is_unknown(tmp_path, capsys):
    with patch.object(ondo_preflight, "load_adapter", return_value=fake_adapter()):
        code = ondo_preflight.main(
            ["--symbols", "AAPL", "--out", str(tmp_path / "p"), "--dry-run"],
        )
    assert code != 0
    assert not (tmp_path / "p").exists()
    assert "AAPL" in capsys.readouterr().err


def test_a_failed_run_never_leaves_a_half_report(tmp_path):
    """A failed run publishes a status that says it failed - never a partial success.

    F12 changed what "no half report" means: the run still writes its own ``complete:
    false`` manifest with the failure, and the payloads it could produce (here an empty
    missing list is not a success either - the schema check fails the run first). What it
    must never do is leave a manifest claiming a complete run.
    """
    markets = {"perps": {"tradingPairs": []}}
    out_dir = tmp_path / "p"
    with contextlib.redirect_stderr(io.StringIO()) as err:
        code = ondo_preflight.main(
            ["--symbols", "NVDA,TSLA", "--out", str(out_dir)],
            client=FakeClient(markets=markets, instruments=[]),
        )
    assert code != 0
    assert "preflight" in err.getvalue().lower()
    meta = read_json(out_dir / "meta.json")
    assert meta["complete"] is False
    assert meta["failure"], "a failed attempt states why it failed"
    assert meta["missing"], "and names the items that were missing"
    assert meta["files"], "the payloads it did read are published with its own verdict"
    assert not ondo_preflight.verify_run(out_dir)["verified"]


if __name__ == "__main__":
    import unittest

    unittest.main()

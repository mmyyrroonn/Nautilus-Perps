"""Recorded native replay and actual native paper outcomes."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_config import parse_plan
from backpack_replay import run_offline
from test_backpack_config import document, SYMBOL

ROOT = Path(__file__).resolve().parents[1]
BASE = 1_700_000_000_000_000_000


def quote(sequence, *, generation=1, received=None, bid="100.0", ask="101.0"):
    receipt = BASE + sequence * 1_000_000 if received is None else received
    return {
        "kind": "frame",
        "generation": generation,
        "received_at_ns": receipt,
        "payload": {
            "stream": f"bookTicker.{SYMBOL}",
            "data": {
                "e": "bookTicker",
                "s": SYMBOL,
                "E": receipt // 1000,
                "T": receipt // 1000 - 1,
                "u": sequence,
                "b": bid,
                "B": "1.00000",
                "a": ask,
                "A": "2.00000",
            },
        },
    }


def setup_session(tmp_path, records=None, *, paper=False):
    records = (
        [quote(1), quote(2, bid="102.0", ask="103.0")] if records is None else records
    )
    raw = b"".join(json.dumps(record).encode() + b"\n" for record in records)
    (tmp_path / "btc.jsonl").write_bytes(raw)
    manifest = {
        "schema_version": 1,
        "venue": "BACKPACK",
        "source": "Synthetic",
        "source_reference": "Synthetic offline adapter acceptance; no captured account data",
        "streams": {
            SYMBOL: {
                "market_json": (
                    ROOT / "tests/fixtures/backpack/btc_market.json"
                ).read_text(),
                "metadata_received_at_ns": BASE,
                "generation": 1,
                "records_file": "btc.jsonl",
            }
        },
    }
    (tmp_path / "session.json").write_text(json.dumps(manifest))
    config = document()
    config.update(
        mode="paper" if paper else "replay",
        environment="offline",
        replay_file="session.json",
    )
    if paper:
        config["paper"] = {
            "initial_balance_usdc": "10000",
            "quantities": {SYMBOL: "0.01000"},
        }
    return parse_plan(config, tmp_path), raw


def test_native_replay_exact_price_duplicate_old_generation_restart_and_hashes(
    tmp_path,
):
    one = quote(1)
    records = [
        one,
        one,
        {**quote(2, generation=0), "payload": {"bad": "ignored old epoch"}},
        {"kind": "restart", "generation": 2, "received_at_ns": BASE + 2_000_000},
        quote(3, generation=2),
    ]
    plan, raw = setup_session(tmp_path, records)
    summary, path = asyncio.run(run_offline(plan))
    assert summary["status"] == "completed", summary
    events = [
        json.loads(line)
        for line in (path.parent / "events.jsonl").read_text().splitlines()
    ]
    quotes = [event for event in events if event["kind"] == "quote"]
    assert len(quotes) == 2
    assert quotes[0]["bid_price"] == "100.0"
    assert quotes[0]["bid_size"] == "1.00000"
    assert quotes[0]["ts_received_ns"] == str(BASE + 1_000_000)
    assert summary["input"]["no_data_records"] == 3
    assert (
        summary["input"]["streams"][SYMBOL]["consumed_sha256"]
        == hashlib.sha256(raw).hexdigest()
    )
    assert summary["historical"] and not summary["live_freshness_claimed"]
    assert not plan.journal_dir.exists()


def test_native_paper_actual_fills_fees_cash_and_flat_position(tmp_path):
    plan, _ = setup_session(tmp_path, paper=True)
    summary, path = asyncio.run(run_offline(plan))
    assert summary["status"] == "completed", summary
    result = summary["paper"]
    assert result["shutdown_complete"] and result["complete"]
    assert [fill["price"] for fill in result["fills"]] == ["101.0", "102.0"]
    assert [fill["commission"]["amount"] for fill in result["fills"]] == [
        "0.00050500",
        "0.00051000",
    ]
    assert result["cash"] == {"amount": "10000.00898500", "currency": "USDC"}
    assert len(result["orders"]) == 2
    assert result["positions"][0]["is_closed"]
    assert result["positions"][0]["quantity"] == "0.00000"
    assert (
        not summary["venue_account_observed"] and not summary["remote_writes_allowed"]
    )
    assert path.exists()


def test_native_paper_insufficient_quotes_reports_actual_residual_position(tmp_path):
    plan, _ = setup_session(tmp_path, [quote(1)], paper=True)
    summary, _ = asyncio.run(run_offline(plan))
    assert summary["failure"] == "paper_scenario_incomplete"
    assert summary["shutdown_complete"]
    assert len(summary["paper"]["fills"]) == 1
    assert not summary["paper"]["positions"][0]["is_closed"]
    assert summary["paper"]["positions"][0]["quantity"] == "0.01000"


def snapshot(sequence=100, *, generation=1, receipt=BASE + 1_000_000, crossed=False):
    return {
        "kind": "snapshot",
        "generation": generation,
        "received_at_ns": receipt,
        "payload": {
            "lastUpdateId": str(sequence),
            "timestamp": receipt // 1000 - 1,
            "bids": [["102.0" if crossed else "100.0", "1.00000"]],
            "asks": [["101.0", "2.00000"]],
        },
    }


def test_native_depth_snapshot_restart_preserves_domain_times(tmp_path):
    records = [
        snapshot(),
        {"kind": "restart", "generation": 2, "received_at_ns": BASE + 2_000_000},
        snapshot(200, generation=2, receipt=BASE + 3_000_000),
    ]
    plan, _ = setup_session(tmp_path, records)
    summary, path = asyncio.run(run_offline(plan))
    assert summary["status"] == "completed", summary
    books = [
        json.loads(line)
        for line in (path.parent / "events.jsonl").read_text().splitlines()
        if json.loads(line)["kind"] == "book_deltas"
    ]
    assert [book["sequence"] for book in books] == ["100", "200"]
    assert books[0]["ts_event_ns"] == str(BASE + 999_000)
    assert books[0]["ts_received_ns"] == str(BASE + 1_000_000)
    assert not books[0]["complete_book_claimed"]


@pytest.mark.parametrize(
    "records",
    [
        [snapshot(crossed=True)],
        [quote(1, bid="100.01")],
        [quote(1, generation=2)],
        [quote(2), quote(3, received=BASE + 1_000_000)],
    ],
)
def test_native_record_faults_fail_closed_without_raw_input(tmp_path, records):
    plan, _ = setup_session(tmp_path, records)
    summary, path = asyncio.run(run_offline(plan))
    assert summary["status"] == "failed"
    assert summary["failure"] in {
        "invalid_native_replay_record",
        "nonmonotonic_recorded_receipts",
    }
    assert not summary["input"]["streams"][SYMBOL]["complete"]
    assert not summary["execution_ready"] and path.exists()


@pytest.mark.parametrize(
    "mutation", ["allowlist", "escape", "env", "duplicate", "version"]
)
def test_manifest_scope_and_version_are_bounded(tmp_path, mutation):
    plan, _ = setup_session(tmp_path)
    path = tmp_path / "session.json"
    manifest = json.loads(path.read_text())
    if mutation == "allowlist":
        manifest["streams"]["ETH_USDC_PERP"] = manifest["streams"][SYMBOL]
    elif mutation in {"escape", "env"}:
        manifest["streams"][SYMBOL]["records_file"] = (
            "../outside.jsonl" if mutation == "escape" else ".env.jsonl"
        )
    elif mutation == "version":
        manifest["schema_version"] = 2
    path.write_text(json.dumps(manifest))
    if mutation == "duplicate":
        path.write_text(
            path.read_text().replace(
                '"schema_version": 1', '"schema_version": 1, "schema_version": 1'
            )
        )
    summary, _ = asyncio.run(run_offline(plan))
    assert summary["status"] == "failed"
    assert summary["input"]["records"] == 0


@pytest.mark.parametrize("bound", ["records", "bytes", "line", "report"])
def test_total_input_and_output_limits(tmp_path, bound):
    from dataclasses import replace

    plan, _ = setup_session(tmp_path)
    if bound == "records":
        plan = replace(plan, max_input_records=1)
    elif bound == "bytes":
        plan = replace(
            plan, max_input_bytes=(tmp_path / "session.json").stat().st_size + 5
        )
    elif bound == "line":
        (tmp_path / "btc.jsonl").write_bytes(b" " * 1_048_577)
    else:
        plan = replace(plan, max_report_events=1)
    summary, path = asyncio.run(run_offline(plan))
    assert summary["status"] == "failed"
    assert summary["failure"] in {"recorded_input_limit", "report_limit"}
    assert (
        sum(file.stat().st_size for file in path.parent.iterdir())
        <= plan.max_report_bytes
    )


def test_paper_cancel_after_actual_fill_finalizes_and_reports_open_position(
    tmp_path, monkeypatch
):
    from backpack_public import PublicEvidence

    plan, _ = setup_session(tmp_path, paper=True)
    original = PublicEvidence.record

    async def scenario():
        task = None

        def observe(self, kind, payload):
            original(self, kind, payload)
            if kind == "paper_fill":
                asyncio.get_running_loop().call_soon(task.cancel)

        monkeypatch.setattr(PublicEvidence, "record", observe)
        task = asyncio.create_task(run_offline(plan))
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    summary = json.loads(next(plan.output_dir.glob("*/summary.json")).read_text())
    assert summary["failure"] == "cancelled"
    assert summary["shutdown_complete"]
    assert len(summary["paper"]["fills"]) == 1
    assert summary["paper"]["positions"][0]["quantity"] == "0.01000"
    assert not summary["paper"]["complete"]


def test_offline_has_no_venue_socket_or_credential_lookup(tmp_path, monkeypatch):
    import os
    import socket

    plan, _ = setup_session(tmp_path, paper=True)

    def forbidden(*args, **kwargs):
        raise AssertionError("offline venue socket or credential access")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(os, "getenv", forbidden)
    monkeypatch.setattr(os, "environ", {})
    summary, _ = asyncio.run(run_offline(plan))
    assert summary["status"] == "completed", summary


def test_paper_quantity_never_rounds_silently(tmp_path):
    from dataclasses import replace
    from decimal import Decimal
    from backpack_config import PaperScenario

    plan, _ = setup_session(tmp_path, paper=True)
    plan = replace(
        plan, paper=PaperScenario(Decimal("10000"), ((SYMBOL, Decimal("0.000001")),))
    )
    summary, _ = asyncio.run(run_offline(plan))
    assert summary["failure"] == "paper_quantity_grid_mismatch"
    assert not summary["paper"]["complete"]
    assert summary["shutdown_complete"]


def test_native_trade_and_mark_are_actual_domains_with_original_times(tmp_path):
    receipt = BASE + 1_000_000
    common = {"s": SYMBOL, "E": receipt // 1000, "T": receipt // 1000 - 2}
    frames = [
        {
            "stream": f"trade.{SYMBOL}",
            "data": {
                **common,
                "e": "trade",
                "a": "11",
                "b": "12",
                "t": 99,
                "p": "100.0",
                "q": "0.07206",
                "m": False,
            },
        },
        {
            "stream": f"markPrice.{SYMBOL}",
            "data": {
                **common,
                "e": "markPrice",
                "f": "0.0000125",
                "i": "100.0",
                "p": "101.0",
                "n": (receipt // 1_000_000) + 3_600_000,
            },
        },
    ]
    plan, _ = setup_session(
        tmp_path,
        [
            {
                "kind": "frame",
                "generation": 1,
                "received_at_ns": receipt + i,
                "payload": frame,
            }
            for i, frame in enumerate(frames)
        ],
    )
    summary, path = asyncio.run(run_offline(plan))
    assert summary["status"] == "completed", summary
    events = [
        json.loads(line)
        for line in (path.parent / "events.jsonl").read_text().splitlines()
    ][1:]
    assert [event["kind"] for event in events] == ["trade", "mark"]
    assert events[0]["trade_id"] == "99" and events[0]["size"] == "0.07206"
    assert events[1]["price"] == "101"
    assert all(event["ts_event_ns"] == str(receipt - 2000) for event in events)
    assert "funding_rate" not in events[1]


def test_multisymbol_global_receipt_order_is_deterministic(tmp_path):
    from copy import deepcopy
    from dataclasses import replace

    plan, _ = setup_session(tmp_path, [quote(3)])
    eth = "ETH_USDC_PERP"
    plan = replace(
        plan,
        symbols=(SYMBOL, eth),
        economics=(*plan.economics, (eth, plan.economics[0][1])),
    )
    manifest_path = tmp_path / "session.json"
    manifest = json.loads(manifest_path.read_text())
    entry = deepcopy(manifest["streams"][SYMBOL])
    entry["market_json"] = entry["market_json"].replace("BTC", "ETH")
    entry["records_file"] = "eth.jsonl"
    manifest["streams"][eth] = entry
    manifest_path.write_text(json.dumps(manifest))
    (tmp_path / "eth.jsonl").write_text(
        json.dumps(quote(1)).replace("BTC", "ETH") + "\n"
    )
    summary, path = asyncio.run(run_offline(plan))
    assert summary["status"] == "completed", summary
    events = [
        json.loads(line)
        for line in (path.parent / "events.jsonl").read_text().splitlines()
    ]
    assert [event["kind"] for event in events[:2]] == ["instrument", "instrument"]
    assert [event["instrument_id"] for event in events[2:]] == [
        f"{eth}.BACKPACK",
        f"{SYMBOL}.BACKPACK",
    ]


def test_paper_runtime_budget_after_fill_preserves_residual_state(
    tmp_path, monkeypatch
):
    import backpack_paper

    original = backpack_paper.checkpoint
    plan, _ = setup_session(tmp_path, paper=True)

    async def expire(deadline, evidence):
        if evidence.counts.get("paper_fill"):
            from backpack_replay import OfflineFailure

            raise OfflineFailure("offline_duration_limit")
        await original(deadline, evidence)

    monkeypatch.setattr(backpack_paper, "checkpoint", expire)
    summary, _ = asyncio.run(run_offline(plan))
    assert (
        summary["failure"] == "offline_duration_limit" and summary["shutdown_complete"]
    )
    assert summary["paper"]["positions"][0]["quantity"] == "0.01000"


@pytest.mark.parametrize("mode", ["paper", "replay"])
def test_offline_dryrun_reads_only_named_config_without_native_or_manifest(
    tmp_path, monkeypatch, capsys, mode
):
    import importlib.abc
    import socket
    import backpack_probe

    config = tmp_path / "plan.toml"
    example = ROOT / f"config/backpack-{mode}.example.toml"
    config.write_bytes(example.read_bytes())
    original = Path.open

    def open_config_only(path, *args, **kwargs):
        assert path == config
        return original(path, *args, **kwargs)

    class NoNative(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(("nautilus_trader", "dotenv")):
                raise AssertionError("dryrun native import")

    def forbidden(*args, **kwargs):
        raise AssertionError("dryrun socket/output")

    monkeypatch.setattr(Path, "open", open_config_only)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(sys, "meta_path", [NoNative(), *sys.meta_path])
    assert backpack_probe.main(["--config", str(config), "--dry-run"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["mode"] == mode and result["environment"] == "offline"
    assert not result["runtime_started"]


def test_last_native_event_triggering_output_limit_fails_replay(tmp_path):
    from dataclasses import replace

    plan, _ = setup_session(tmp_path, [quote(1), quote(2)])
    plan = replace(plan, max_report_events=2)
    summary, _ = asyncio.run(run_offline(plan))
    assert summary["failure"] == "report_limit" and summary["report_limit_reached"]
    assert summary["dropped_events"] == 1


def test_actual_thin_liquidity_never_sends_more_than_two_orders(tmp_path):
    records = [quote(i) for i in range(1, 7)]
    for record in records:
        record["payload"]["data"].update(B="0.00500", A="0.00500")
    plan, _ = setup_session(tmp_path, records, paper=True)
    summary, _ = asyncio.run(run_offline(plan))
    assert summary["status"] == "completed"
    assert len(summary["paper"]["orders"]) == 2
    assert len(summary["paper"]["fills"]) == 4
    assert len({fill["client_order_id"] for fill in summary["paper"]["fills"]}) == 2
    assert [fill["quantity"] for fill in summary["paper"]["fills"]] == ["0.00500"] * 4
    assert all(order["status"] == "FILLED" for order in summary["paper"]["orders"])
    assert all(position["is_closed"] for position in summary["paper"]["positions"])


def test_thin_liquidity_with_no_exit_quote_reports_actual_residual(tmp_path):
    record = quote(1)
    record["payload"]["data"].update(B="0.00500", A="0.00500")
    plan, _ = setup_session(tmp_path, [record], paper=True)
    summary, _ = asyncio.run(run_offline(plan))
    assert summary["failure"] == "paper_scenario_incomplete"
    assert len(summary["paper"]["orders"]) == 1
    assert len(summary["paper"]["fills"]) == 2
    assert summary["paper"]["positions"][0]["quantity"] == "0.01000"
    assert not summary["paper"]["positions"][0]["is_closed"]


@pytest.mark.parametrize(
    "field,value",
    [
        ("initial_balance_usdc", "0"),
        ("initial_balance_usdc", "1000001"),
        ("initial_balance_usdc", "0.0000001"),
        ("initial_balance_usdc", 10000),
        ("quantities", {}),
        ("quantities", {SYMBOL: "0"}),
        ("quantities", {SYMBOL: 0.01}),
        ("unknown", "synthetic-private-sentinel"),
    ],
)
def test_paper_config_exact_economics_and_allowlist_fail_closed(tmp_path, field, value):
    from backpack_config import BackpackConfigError

    config = document()
    config.update(mode="paper", environment="offline", replay_file="not-opened.json")
    config["paper"] = {
        "initial_balance_usdc": "10000",
        "quantities": {SYMBOL: "0.01000"},
    }
    config["paper"][field] = value
    with pytest.raises(BackpackConfigError) as error:
        parse_plan(config, tmp_path)
    assert "synthetic-private-sentinel" not in str(error.value)


def test_actual_native_malformed_line_failure_never_echoes_raw_contents(tmp_path):
    plan, _ = setup_session(tmp_path)
    (tmp_path / "btc.jsonl").write_bytes(b"{sensitive-fixture-replay-token\n")
    summary, path = asyncio.run(run_offline(plan))
    assert summary["failure"] == "invalid_native_replay_record"
    assert "sensitive-fixture-replay-token" not in path.read_text()
    assert (
        "sensitive-fixture-replay-token"
        not in (path.parent / "events.jsonl").read_text()
    )


def test_timestamp_group_limit_is_checked_before_native_engine_run(tmp_path):
    records = [quote(i, received=BASE + 1_000_000) for i in range(1, 66)]
    plan, _ = setup_session(tmp_path, records, paper=True)
    summary, _ = asyncio.run(run_offline(plan))
    assert summary["failure"] == "paper_timestamp_batch_limit"
    assert summary["shutdown_complete"]
    assert summary["paper"]["fills"] == [] and summary["paper"]["orders"] == []

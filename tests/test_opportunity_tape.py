"""Tests for asynchronous L2 tape recording and paper CLI flags."""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

import market_tape
from opportunity_core import BookSnapshot, MarketMetadata
from opportunity_scan import MarketPlan
from opportunity_tape import OpportunityTapeRecorder, iter_tape_records
from opportunity_trade import main as trade_main
from test_opportunity_trade import write_configs


def _sample_market(symbol: str = "BTC", venue: str = "HL") -> MarketPlan:
    return MarketPlan(
        symbol=symbol,
        venue=venue,
        instrument_id=f"{symbol}-USD-PERP.{venue}",
        quote_to_usd=Decimal("1"),
        valuation_source="synthetic",
        canonical_multiplier=Decimal("1"),
    )


def _sample_metadata(market: MarketPlan) -> MarketMetadata:
    return MarketMetadata(
        instrument_id=market.instrument_id,
        size_increment=Decimal("0.001"),
        multiplier=Decimal("1"),
        taker_fee_bps=Decimal("4.5"),
        fee_source="synthetic",
        min_quantity=Decimal("0.001"),
        min_notional=Decimal("10"),
        maker_fee_bps=Decimal("1.5"),
    )


def _sample_book(market: MarketPlan, idx: int, ts_ns: int) -> BookSnapshot:
    bid = Decimal("100.123456789012345678") + Decimal(idx) * Decimal("0.01")
    ask = bid + Decimal("0.05")
    return BookSnapshot(
        venue=market.venue,
        symbol=market.symbol,
        bids=((bid, Decimal("1.25")), (bid - Decimal("0.01"), Decimal("3.5"))),
        asks=((ask, Decimal("2.75")), (ask + Decimal("0.01"), Decimal("4.0"))),
        ts_event_ns=ts_ns,
        ts_received_ns=ts_ns + 500,
        valid=True,
        coverage_limit=20,
    )


def test_gzip_rotation_manifest_and_exact_decimal_roundtrip(tmp_path):
    tape_dir = tmp_path / "tape-gz"
    market = _sample_market("BTC", "HL")
    meta = _sample_metadata(market)
    now_ns = 1_800_000_000_000_000_000

    with OpportunityTapeRecorder(
        tape_dir,
        run_id="20261122T120000Z",
        legs=[{"symbol": market.symbol, "venue": market.venue, "instrument_id": market.instrument_id}],
        evaluation_interval_ms=20,
        rotate_bytes=1200,
        compress=True,
    ) as rec:
        rec.record_instrument(market, meta, now_ns, price_increment=Decimal("0.01"))
        for i in range(18):
            rec.record_book(market, _sample_book(market, i, now_ns + i * 1000), now_ns + i * 1000 + 800, sequence=i + 1)
        rec.record_invalidation(market, now_ns + 20_000, reason="disconnected", metadata_invalid=False)

    doc = rec.document()
    assert doc["complete"] is True
    assert doc["failed"] is False
    assert doc["records"] == 20
    assert doc["dropped"] == 0
    assert doc["gaps"] == 0
    assert doc["segments"] >= 2
    assert doc["compressed_bytes"] > 0

    gz_files = sorted(tape_dir.glob("tape_*.jsonl.gz"))
    assert len(gz_files) == doc["segments"]
    manifest = json.loads((tape_dir / "manifest.json").read_text(encoding="utf-8"))
    assert len(manifest["segments"]) == doc["segments"]
    assert all(seg["closed"] is True for seg in manifest["segments"])

    records = list(iter_tape_records(tape_dir))
    assert records[0]["event_kind"] == "run_start"
    assert records[0]["evaluation_interval_ms"] == 20
    assert records[-1]["event_kind"] == "run_end"
    assert records[-1]["complete"] is True

    data_records = [r for r in records if r["event_kind"] not in {"run_start", "run_end", "gap"}]
    assert len(data_records) == 20
    assert [r["arrival_seq"] for r in data_records] == list(range(1, 21))

    inst_rec = data_records[0]
    assert inst_rec["event_kind"] == "instrument"
    assert inst_rec["metadata"]["taker_fee_bps"] == "4.5"
    assert inst_rec["metadata"]["price_increment"] == "0.01"
    assert inst_rec["metadata"]["size_increment"] == "0.001"

    first_book = data_records[1]
    assert first_book["event_kind"] == "book"
    assert first_book["bids"][0] == ["100.123456789012345678", "1.25"]
    assert first_book["sequence"] == 1

    inv_rec = data_records[-1]
    assert inv_rec["event_kind"] == "status"
    assert inv_rec["valid"] is False
    assert inv_rec["reason"] == "disconnected"
    assert inv_rec["metadata_invalid"] is False


def test_uncompressed_tape_is_compatible_with_market_tape_reader(tmp_path):
    tape_dir = tmp_path / "tape-plain"
    market = _sample_market("ETH", "ASTER")
    meta = _sample_metadata(market)
    now_ns = 1_800_000_000_000_000_000

    with OpportunityTapeRecorder(
        tape_dir,
        run_id="20261122T120100Z",
        rotate_bytes=1200,
        compress=False,
    ) as rec:
        rec.record_instrument(market, meta, now_ns, price_increment=Decimal("0.01"))
        for i in range(12):
            rec.record_book(market, _sample_book(market, i, now_ns + i * 1000), now_ns + i * 1000 + 200, sequence=i + 1)
        rec.record_invalidation(market, now_ns + 50_000, reason="halted")

    for target in (tape_dir, tape_dir / "manifest.json"):
        records, status = market_tape.read_tape_status([target])
        assert status.complete is True
        assert status.failed is False
        assert status.gaps == 0
        assert status.dropped == 0
        # 14 data records + run_start + run_end
        assert status.records == 16
        assert len(records) == 16


def test_queue_full_drops_without_blocking_and_writes_gap_marker(tmp_path, monkeypatch):
    tape_dir = tmp_path / "tape-gap"
    market = _sample_market("SOL", "LIGHTER")
    now_ns = 1_800_000_000_000_000_000

    rec = OpportunityTapeRecorder(tape_dir, queue_max=2, compress=True, flush_secs=0.05)

    # Hold the writer thread on the very first batch so subsequent items overflow queue_max=2.
    import opportunity_tape as tape_mod
    entered = False
    release = False
    orig_process = rec._process_batch

    def stalled_process(batch):
        nonlocal entered
        entered = True
        while not release:
            time.sleep(0.01)
        return orig_process(batch)

    monkeypatch.setattr(rec, "_process_batch", stalled_process)
    rec.open()
    try:
        rec.record_book(market, _sample_book(market, 0, now_ns), now_ns, sequence=1)
        deadline = time.monotonic() + 2.0
        while not entered and time.monotonic() < deadline:
            time.sleep(0.005)
        assert entered
        t0 = time.monotonic()
        for i in range(1, 20):
            rec.record_book(market, _sample_book(market, i, now_ns + i), now_ns + i, sequence=i + 1)
        # Must remain non-blocking even while the writer is stalled.
        assert (time.monotonic() - t0) < 0.5
        release = True
    finally:
        rec.close()

    doc = rec.document()
    assert doc["dropped"] > 0
    assert doc["gaps"] >= 1
    assert doc["complete"] is False

    records = list(iter_tape_records(tape_dir))
    gap_records = [r for r in records if r["event_kind"] == "gap"]
    assert len(gap_records) >= 1
    assert sum(r["dropped"] for r in gap_records) == doc["dropped"]
    assert gap_records[0]["invalid_reason"] == "recording_gap:queue_full"
    assert records[-1]["event_kind"] == "run_end"
    assert records[-1]["complete"] is False


def test_trade_cli_dry_run_and_validation_for_tape_flags(tmp_path, capsys):
    config = write_configs(tmp_path)
    tape_dir = tmp_path / "out-tape"

    assert trade_main([
        "--config", str(config),
        "--dry-run",
        "--duration-secs", "0",
        "--evaluation-interval-ms", "20",
        "--tape-dir", str(tape_dir),
    ]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["scan"]["evaluation_interval_ms"] == 20
    assert document["tape_dir"] == str(tape_dir)
    assert not tape_dir.exists()

    # Invalid evaluation_interval_ms bounds (> 1000 or < 0) fail cleanly with exit code 2.
    assert trade_main([
        "--config", str(config),
        "--dry-run",
        "--evaluation-interval-ms", "1001",
    ]) == 2
    assert trade_main([
        "--config", str(config),
        "--dry-run",
        "--evaluation-interval-ms", "-1",
    ]) == 2


def test_run_paper_integrates_tape_recorder_end_to_end(tmp_path, monkeypatch, capsys):
    config = write_configs(tmp_path)
    tape_dir = tmp_path / "paper-tape"
    import opportunity_runtime as runtime_mod

    class FakeHandle:
        def stop(self):
            pass

    class FakeObserver:
        failure = None
        started = True
        memory_diagnostics = None

    class FakeNode:
        def __init__(self, plan, observer, tape_recorder):
            self._plan = plan
            self._observer = observer
            self._tape = tape_recorder

        def handle(self):
            return FakeHandle()

        def run(self):
            market = self._plan.markets[0]
            meta = _sample_metadata(market)
            now_ns = 1_800_000_000_000_000_000
            self._tape.record_instrument(market, meta, now_ns, price_increment=Decimal("0.01"))
            self._tape.record_book(market, _sample_book(market, 0, now_ns + 1000), now_ns + 2000, sequence=7)

    def fake_build_node(scan_plan, *, state_factory=None, tape_recorder=None):
        state = state_factory(scan_plan)
        obs = FakeObserver()
        return FakeNode(scan_plan, obs, tape_recorder), obs, state

    monkeypatch.setattr(runtime_mod, "build_node", fake_build_node)
    rc = trade_main([
        "--config", str(config),
        "--duration-secs", "0",
        "--evaluation-interval-ms", "20",
        "--tape-dir", str(tape_dir),
    ])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["tape"]["complete"] is True
    assert out["tape"]["records"] == 2
    assert out["tape"]["dropped"] == 0
    assert (tape_dir / "manifest.json").exists()

    recs = [r for r in iter_tape_records(tape_dir) if r["event_kind"] in {"instrument", "book"}]
    assert [r["event_kind"] for r in recs] == ["instrument", "book"]
    assert recs[1]["sequence"] == 7

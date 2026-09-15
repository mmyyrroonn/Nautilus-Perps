#!/usr/bin/env python3
"""Offline tests for ``src/market_tape.py`` (the standardized multi-leg L2 tape, plan 5.1).

Everything here runs without a network and without a LiveNode: the writer, the reader,
the bounded queue, the rotation/restart rules and the corruption rules are exercised on
real files under ``tmp_path``.

    .venv\\Scripts\\python.exe -m pytest tests/test_market_tape.py -q -p no:cacheprovider
"""

from __future__ import annotations

import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from market_tape import (  # noqa: E402
    ADD,
    BOOK,
    CLEAR,
    FUNDING,
    GAP,
    INSTRUMENT,
    QUOTE,
    RECORD_FIELDS,
    RUN_END,
    RUN_START,
    SCHEMA_VERSION,
    STATUS,
    BookTape,
    Delta,
    TapeCorruptionError,
    TapeError,
    TapeOrderError,
    TapeSchemaError,
    TapeSessionError,
    TapeTypeError,
    TapeWriteError,
    TapeWriter,
    funding_event,
    instrument_event,
    quote_event,
    read_manifest,
    read_run_manifest,
    read_tape,
    status_event,
    write_run_manifest,
)


RUN_ID = "fixture"


# ------------------------------------------------------------------ plan 5.1's helper


def make_book_event(*, bids, **overrides):
    """The brief's helper, with overrides so a test can drive the lifecycle fields.

    Every keyword of the plan's own fixture keeps its documented default; ``overrides``
    only exist because these tests need several records of one shape.
    """
    event = {
        "schema_version": 1, "run_id": "fixture", "arrival_seq": 1,
        "symbol": "NVDA", "venue": "ONDO", "instrument_id": "NVDA-USD-PERP.ONDO",
        "event_kind": "book", "ts_event_ns": 1_000_000_000,
        "ts_init_ns": 1_000_000_010, "recorded_mono_ns": 10,
        "bids": bids, "asks": [["100.10", "0.001"]],
        "coverage_limit": 100, "source": "snapshot", "valid": True,
        "invalid_reason": None,
    }
    event.update(overrides)
    return event


def test_tape_preserves_decimal_text(tmp_path):
    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id="fixture") as writer:
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]]))
    rows = [r for r in read_tape([path]) if r["event_kind"] == "book"]
    assert rows[0]["bids"] == [["100.05", "0.001"]]


# ------------------------------------------------------------------------- helpers


def full_book(venue="ONDO", *, instrument_id="NVDA-USD-PERP.ONDO", base="100.05",
              levels=12, size="0.001", stride=Decimal("0.01"), **overrides):
    """A two-sided batch with `levels` levels per side, built from exact Decimals."""
    bid_levels = [
        (Decimal(base) - stride * n, Decimal(size) * (n + 1)) for n in range(levels)
    ]
    # The ask side starts one step above the best bid: a two-sided, uncrossed book.
    ask_levels = [(Decimal(base) + stride * (n + 1), Decimal(size) * (n + 1))
                  for n in range(levels)]
    ops = [Delta(CLEAR)]
    ops += [Delta(ADD, "bid", price, size) for price, size in bid_levels]
    ops += [Delta(ADD, "ask", price, size) for price, size in ask_levels]
    record = {"venue": venue, "instrument_id": instrument_id}
    record.update(overrides)
    return ops, bid_levels, ask_levels


def write_records(path: Path, records) -> Path:
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


def crash_after(path: Path, records, partial: bytes = b'{"schema_version": 2, "run_id": "x"'
                ) -> Path:
    """A fragment whose last line was cut in half: what a dying process leaves behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    write_records(path, records)
    with path.open("ab") as handle:
        handle.write(partial)
    return path


def hand_record(*, session_id="s1", arrival_seq=1, run_id=RUN_ID, event_kind=BOOK,
                **overrides):
    """A hand-written line: the reader's own validation is what is under test."""
    event = make_book_event(bids=overrides.pop("bids", [["100.05", "0.001"]]), **overrides)
    event["run_id"] = run_id
    event["session_id"] = session_id
    event["event_kind"] = event_kind
    if arrival_seq is not None:
        event["arrival_seq"] = arrival_seq
    return event


def books(rows) -> list[dict]:
    return [r for r in rows if r["event_kind"] == BOOK]


# ----------------------------------------------------------------- record schema


def test_every_record_carries_the_documented_fields_and_kinds(tmp_path):
    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id=RUN_ID, symbol="NVDA") as writer:
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
        writer.write_event(quote_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            bid=Decimal("100.05"), ask=Decimal("100.10"), bid_size=Decimal("0.001"),
            ask_size=Decimal("0.002"), ts_event_ns=1, ts_init_ns=2,
        ))
        writer.write_event(funding_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            rate=Decimal("0.0000063"), interval_secs=60, ts_event_ns=3, ts_init_ns=4,
        ))
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="adapter:snapshot_ready", reason="adapter:snapshot_ready",
            is_trading=True, is_quoting=True, ts_event_ns=5, ts_init_ns=6,
        ))
        writer.write_event(instrument_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            metadata={"tick_size": "0.01", "taker_fee_bps": "2.5"},
            ts_event_ns=7, ts_init_ns=8,
        ))

    rows = list(read_tape([path]))
    kinds = {r["event_kind"] for r in rows}
    assert {RUN_START, BOOK, QUOTE, FUNDING, STATUS, INSTRUMENT, RUN_END} <= kinds
    for record in rows:
        assert set(RECORD_FIELDS) <= set(record), record["event_kind"]
        assert record["schema_version"] == SCHEMA_VERSION
        assert record["run_id"] == RUN_ID
        assert record["session_id"]
        if record["event_kind"] not in (RUN_START, RUN_END, GAP):
            assert isinstance(record["arrival_seq"], int)
    assert [r["arrival_seq"] for r in rows if r["event_kind"] == QUOTE] == [2]
    quote = next(r for r in rows if r["event_kind"] == QUOTE)
    assert quote["bid"] == "100.05" and isinstance(quote["bid"], str)
    funding = next(r for r in rows if r["event_kind"] == FUNDING)
    assert funding["rate"] == "0.0000063" and isinstance(funding["rate"], str)
    assert funding["coverage_limit"] is None


def test_a_float_never_reaches_the_tape(tmp_path):
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    with pytest.raises(TapeTypeError):
        writer.write_event(make_book_event(bids=[[100.05, 0.001]]))
    with pytest.raises(TapeTypeError):
        writer.write_event(make_book_event(bids=[["100.05", 0.001]]))
    with pytest.raises(TapeTypeError):
        writer.write_event(quote_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            bid=100.05, ask="100.10", bid_size="0.001", ask_size="0.002",
        ))
    assert writer.records == 0, "a refused event must not be recorded"

    writer.write_event(make_book_event(bids=[[Decimal("100.050"), Decimal("0.00100")]]))
    writer.close()
    rows = books(list(read_tape([path])))
    assert rows[0]["bids"] == [["100.050", "0.00100"]]
    assert all(isinstance(text, str) for pair in rows[0]["bids"] for text in pair)


def test_the_writer_stamps_schema_two_and_the_reader_still_accepts_schema_one(tmp_path):
    """Contract B: write 2, read 1 and 2.

    A schema 1 tape (one run-start metadata record, no per-arrival metadata, no
    ``size_increment``) must keep reading after the writer moved to schema 2. The
    reader reports which versions it actually saw, so a consumer can tell an old tape
    from a new one instead of guessing from a missing field.
    """
    from market_tape import SCHEMA_VERSION_V1, SUPPORTED_SCHEMA_VERSIONS

    assert SCHEMA_VERSION == 2
    assert SCHEMA_VERSION_V1 == 1
    assert set(SUPPORTED_SCHEMA_VERSIONS) == {1, 2}

    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id=RUN_ID, flush_secs=0.0) as writer:
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    reader = read_tape([path])
    rows = list(reader)
    assert {r["schema_version"] for r in rows} == {2}
    assert reader.status.schema_versions == [2]
    assert reader.status.complete is True

    old = write_records(tmp_path / "old.jsonl", [
        hand_record(session_id="s1", arrival_seq=1, schema_version=1),
        hand_record(session_id="s1", arrival_seq=2, schema_version=1,
                    event_kind=RUN_END, complete=True),
    ])
    old_reader = read_tape([old])
    list(old_reader)
    assert old_reader.status.schema_versions == [1]
    assert old_reader.status.complete is True, "a schema 1 tape is not corrupt"


def test_an_instrument_record_can_carry_the_stale_marker_and_its_source(tmp_path):
    """Contract C: source names the arrival, ``valid``/``invalid_reason`` the verdict.

    A stale marker keeps the last known payload on the record for traceability while
    telling every reader not to use it.
    """
    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id=RUN_ID, flush_secs=0.0) as writer:
        writer.write_event(instrument_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            metadata={"taker_fee_bps": "2.50", "size_increment": "0.001"},
            source="instrument_metadata", ts_event_ns=1, ts_init_ns=1,
        ))
        writer.write_event(instrument_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            metadata={"taker_fee_bps": "2.50", "size_increment": "0.001"},
            source="instrument_update", valid=False, invalid_reason="metadata_stale",
            ts_event_ns=2, ts_init_ns=2,
        ))
    rows = [r for r in read_tape([path]) if r["event_kind"] == INSTRUMENT]
    assert [r["source"] for r in rows] == ["instrument_metadata", "instrument_update"]
    assert rows[0]["valid"] is True and rows[0]["invalid_reason"] is None
    assert rows[1]["valid"] is False and rows[1]["invalid_reason"] == "metadata_stale"
    assert rows[1]["metadata"] == rows[0]["metadata"], (
        "the stale marker keeps the last known payload for traceability"
    )


def test_the_reader_refuses_levels_that_are_not_decimal_strings(tmp_path):
    path = write_records(tmp_path / "l2.jsonl", [
        hand_record(bids=[[100.05, 0.001]]),
    ])
    with pytest.raises(TapeSchemaError):
        list(read_tape([path]))


# -------------------------------------------------------------- two legs, full L2


def test_two_legs_each_record_a_complete_l2_with_decimal_strings(tmp_path):
    path = tmp_path / "l2" / "l2_NVDA_ONDO-ASTER_20260914T000000Z.jsonl"
    with TapeWriter(path, run_id=RUN_ID, symbol="NVDA") as writer:
        ondo = BookTape(writer, symbol="NVDA", venue="ONDO",
                        instrument_id="NVDA-USD-PERP.ONDO", coverage_limit=100)
        aster = BookTape(writer, symbol="NVDA", venue="ASTER",
                         instrument_id="NVDAUSDT-PERP.ASTER")
        ops, ondo_bids, ondo_asks = full_book("ONDO")
        ondo.apply_batch(ops, ts_event_ns=1_000, ts_init_ns=1_001)
        ops, aster_bids, aster_asks = full_book(
            "ASTER", instrument_id="NVDAUSDT-PERP.ASTER", base="200.50",
            size="0.002", stride=Decimal("0.5"),
        )
        aster.apply_batch(ops, ts_event_ns=1_002, ts_init_ns=1_003)

    rows = books(list(read_tape([path])))
    assert len(rows) == 2, "one book record per leg per batch"
    by_venue = {r["venue"]: r for r in rows}
    assert set(by_venue) == {"ONDO", "ASTER"}
    assert by_venue["ONDO"]["bids"][0] == ["100.05", "0.001"]
    assert by_venue["ASTER"]["bids"][0] == ["200.50", "0.002"]
    for record in rows:
        for side in ("bids", "asks"):
            levels = record[side]
            # Complete L2, not a top-of-book: 12 levels per side, not 1 and not 10.
            assert len(levels) == 12, (record["venue"], side)
            assert all(isinstance(text, str) for pair in levels for text in pair)
        assert record["coverage_limit"] == 100 or record["coverage_limit"] is None
        assert record["valid"] is True and record["invalid_reason"] is None
        assert record["ts_event_ns"] in (1_000, 1_002)


def test_a_deltas_batch_records_one_book_and_never_the_intermediate_clear(tmp_path):
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    book = BookTape(writer, symbol="NVDA", venue="ONDO",
                    instrument_id="NVDA-USD-PERP.ONDO", coverage_limit=100)

    # A full replacement snapshot: CLEAR then ADDs. The CLEAR is not a depth record.
    book.apply_batch(
        [Delta(CLEAR), Delta(ADD, "bid", "100.05", "0.001"),
         Delta(ADD, "ask", "100.10", "0.002")],
        ts_event_ns=1, ts_init_ns=2,
    )
    # One incremental delta: still exactly one record.
    book.apply_batch([Delta(ADD, "bid", "100.06", "0.003")], ts_event_ns=3, ts_init_ns=4)
    # A second full replacement: the old levels must be gone in the SAME record.
    book.apply_batch(
        [Delta(CLEAR), Delta(ADD, "bid", "101.00", "0.004")],
        ts_event_ns=5, ts_init_ns=6,
    )
    writer.close()

    rows = books(list(read_tape([path])))
    assert len(rows) == 3, "one record per fully applied batch, never one per delta"
    assert [r["arrival_seq"] for r in rows] == [1, 2, 3]
    assert rows[0]["source"] == "snapshot"
    assert rows[0]["bids"] == [["100.05", "0.001"]]
    assert rows[0]["asks"] == [["100.10", "0.002"]]
    assert rows[1]["source"] == "deltas"
    assert rows[1]["bids"] == [["100.06", "0.003"], ["100.05", "0.001"]]
    assert rows[2]["bids"] == [["101.00", "0.004"]], "the CLEAR wiped the old bid"
    assert rows[2]["asks"] == [], "…including the other side the CLEAR replaced"


def test_a_clear_only_batch_is_an_explicitly_invalid_empty_book(tmp_path):
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    book = BookTape(writer, symbol="NVDA", venue="ONDO",
                    instrument_id="NVDA-USD-PERP.ONDO", coverage_limit=100)
    book.apply_batch([Delta(CLEAR), Delta(ADD, "bid", "100.05", "0.001"),
                      Delta(ADD, "ask", "100.10", "0.002")], ts_event_ns=1, ts_init_ns=2)
    book.apply_batch([Delta(CLEAR)], ts_event_ns=3, ts_init_ns=4)
    writer.close()

    rows = books(list(read_tape([path])))
    assert len(rows) == 2
    assert rows[1]["bids"] == [] and rows[1]["asks"] == []
    assert rows[1]["valid"] is False
    assert rows[1]["invalid_reason"] == "empty_book"
    for record in rows:
        if not record["bids"] and not record["asks"]:
            assert record["valid"] is False, "an empty book is never valid depth"


def test_a_caller_may_mark_a_record_invalid_with_a_reason_of_its_own(tmp_path):
    """``BookTape`` passes an explicit verdict through; the watcher never supplies one.

    Renamed from ``test_a_feed_disconnect_marks_the_book_record_invalid``: the watcher used
    to fold the feed's state into a book record's ``valid`` that way, and contract D removed
    that (a record's validity is its own snapshot's, and the feed's state is replayed from
    the status records). The *API* still takes an explicit verdict, because a caller that
    knows a snapshot itself is bad - a truncated depth10 fallback, say - must be able to say
    so with its own reason. No caller in ``src/`` passes one today.
    """
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    book = BookTape(writer, symbol="NVDA", venue="ONDO",
                    instrument_id="NVDA-USD-PERP.ONDO", coverage_limit=100)
    ops, _bids, _asks = full_book("ONDO")
    book.apply_batch(ops, ts_event_ns=1, ts_init_ns=2, valid=False,
                     invalid_reason="depth10_truncated")
    writer.close()
    record = books(list(read_tape([path])))[0]
    assert record["valid"] is False and record["invalid_reason"] == "depth10_truncated"
    assert record["bids"], "the levels are still recorded, the record is just not valid"


def test_the_depth10_fallback_is_recorded_with_its_coverage_limit(tmp_path):
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    book = BookTape(writer, symbol="NVDA", venue="ONDO",
                    instrument_id="NVDA-USD-PERP.ONDO", coverage_limit=100)
    book.replace([["100.05", "0.001"]], [["100.10", "0.002"]],
                 ts_event_ns=9, ts_init_ns=10)
    writer.close()
    record = books(list(read_tape([path])))[0]
    assert record["source"] == "depth10"
    assert record["coverage_limit"] == 10, "10 levels is not complete L2 and says so"
    assert record["bids"] == [["100.05", "0.001"]]


def test_the_depth10_snapshot_and_the_deltas_book_are_two_separate_states(tmp_path):
    """A leg fed by *both* book sources records each from its own state (Important 1).

    HL/Lighter legs get the managed deltas stream and, when it stays empty, the depth10
    fallback as well. A depth10 snapshot is truncated to ten levels, so if it became the
    deltas book a later deltas batch would be applied to a ten-level dict and written as a
    complete-L2 record holding ten levels. The two states are kept apart, so every record's
    levels are the levels of the feed its ``source``/``coverage_limit`` name - in either
    arrival order.
    """
    ten_bids = [(f"555.{n:02d}", "0.002") for n in range(10, 0, -1)]
    ten_asks = [(f"666.{n:02d}", "0.002") for n in range(1, 11)]
    book_bids = [(f"100.{n:02d}", "0.001") for n in range(15, 0, -1)]
    book_asks = [(f"200.{n:02d}", "0.001") for n in range(1, 16)]

    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    # Leg one: depth10 first, then an incremental deltas batch (no CLEAR).
    first = BookTape(writer, symbol="NVDA", venue="HYPERLIQUID",
                     instrument_id="NVDA-USD-PERP.HYPERLIQUID", coverage_limit=100)
    first.replace(ten_bids, ten_asks, ts_event_ns=1, ts_init_ns=2)
    first.apply_batch([Delta(ADD, "bid", "100.05", "0.001"),
                       Delta(ADD, "ask", "100.06", "0.001")], ts_event_ns=3, ts_init_ns=4)
    # Leg two: the reverse order - a full deltas book, then a depth10 snapshot, then a delta.
    second = BookTape(writer, symbol="NVDA", venue="LIGHTER",
                      instrument_id="NVDA-PERP.LIGHTER", coverage_limit=None)
    second.apply_batch([Delta(CLEAR)]
                       + [Delta(ADD, "bid", p, s) for p, s in book_bids]
                       + [Delta(ADD, "ask", p, s) for p, s in book_asks],
                       ts_event_ns=5, ts_init_ns=6)
    second.replace(ten_bids, ten_asks, ts_event_ns=7, ts_init_ns=8)
    second.apply_batch([Delta(ADD, "bid", "100.16", "0.001")], ts_event_ns=9, ts_init_ns=10)
    writer.close()

    rows = books(list(read_tape([path])))
    assert [r["source"] for r in rows] == ["depth10", "deltas", "snapshot", "depth10", "deltas"]
    assert [r["coverage_limit"] for r in rows] == [10, 100, None, 10, None], (
        "each record carries its own feed's coverage, never the other feed's"
    )

    depth10_first, deltas_first = rows[0], rows[1]
    assert depth10_first["bids"] == [[p, s] for p, s in ten_bids]
    assert deltas_first["bids"] == [["100.05", "0.001"]], (
        "the deltas record holds the deltas book alone - no depth10 level leaked into it"
    )
    assert deltas_first["asks"] == [["100.06", "0.001"]]
    assert deltas_first["coverage_limit"] != 10, "a deltas record is not a limited depth10"

    deltas_book, depth10_second, deltas_after = rows[2], rows[3], rows[4]
    assert [p for p, _s in deltas_book["bids"]] == [p for p, _s in book_bids]
    assert depth10_second["bids"] == [[p, s] for p, s in ten_bids]
    assert [p for p, _s in deltas_after["bids"]] == ["100.16", *[p for p, _s in book_bids]], (
        "the depth10 snapshot did not replace the deltas book"
    )
    assert [p for p, _s in deltas_after["asks"]] == [p for p, _s in book_asks], (
        "…and the deltas book still owns its other side"
    )


# ------------------------------------------------------- queue, disk, rotation, close


def _jsonl_lines(fragment: Path) -> list[dict]:
    """The raw lines of one fragment: the manifest is checked against the file itself."""
    return [
        json.loads(line)
        for line in Path(fragment).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


MARKER_KINDS_IN_FILE = (RUN_START, RUN_END, GAP)


def test_the_plan_defaults_are_a_bounded_queue_a_second_flush_and_128_mib(tmp_path):
    """Plan 5.1's resource limits, as the defaults a production writer actually gets."""
    from market_tape import FLUSH_SECS, QUEUE_MAX, ROTATE_BYTES

    assert QUEUE_MAX == 4096, "a bounded queue: never an unbounded memory cache"
    assert FLUSH_SECS == 1.0, "a dying process loses at most a second"
    assert ROTATE_BYTES == 128 * 1024 * 1024
    writer = TapeWriter(tmp_path / "l2.jsonl", run_id=RUN_ID)
    assert (writer.queue_max, writer.flush_secs, writer.rotate_bytes) == (
        QUEUE_MAX, FLUSH_SECS, ROTATE_BYTES,
    )


def test_every_fragment_reports_only_the_records_it_holds(tmp_path):
    """A flush can rotate several times: a fragment's facts are the *written* facts.

    All forty events were queued while the writer still sat on fragment 1 (no flush had
    happened yet) and only four survived the bounded queue. The flush that writes those
    four rotates through several fragments, so attributing records or ``arrival_seq``
    bounds at enqueue time would describe fragments that do not hold them.
    """
    path = tmp_path / "l2" / "l2_NVDA.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, queue_max=4, flush_secs=1e9,
                        rotate_bytes=400, symbol="NVDA")
    for _ in range(40):
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    writer.close()

    fragments = sorted(path.parent.glob("*.jsonl"))
    assert len(fragments) >= 3, fragments
    doc = read_manifest(writer.manifest_path)
    assert [entry["segment_index"] for entry in doc["segments"]] == list(
        range(1, len(fragments) + 1),
    )
    for entry in doc["segments"]:
        rows = _jsonl_lines(path.parent / entry["path"])
        data = [r for r in rows if r["event_kind"] not in MARKER_KINDS_IN_FILE]
        assert entry["lines"] == len(rows), entry
        assert entry["bytes"] == (path.parent / entry["path"]).stat().st_size, entry
        assert entry["records"] == len(data), (
            f"fragment {entry['path']} holds {len(data)} data record(s), the manifest "
            f"claims {entry['records']}"
        )
        if data:
            assert (entry["first_arrival_seq"], entry["last_arrival_seq"]) == (
                data[0]["arrival_seq"], data[-1]["arrival_seq"],
            ), entry
        else:
            assert entry["first_arrival_seq"] is None and entry["last_arrival_seq"] is None

    # The drop count lands on the fragment that carries the marker naming the range.
    markers = [
        (entry, [r for r in _jsonl_lines(path.parent / entry["path"])
                 if r["event_kind"] == GAP])
        for entry in doc["segments"]
    ]
    carrying = [(entry, gaps) for entry, gaps in markers if gaps]
    assert len(carrying) == 1, markers
    entry, gaps = carrying[0]
    assert (entry["gaps"], entry["dropped"]) == (1, gaps[0]["dropped"])
    assert sum(e["dropped"] for e in doc["segments"]) == writer.dropped == 36
    for other, gaps in markers:
        if other is not entry:
            assert other["dropped"] == 0 and other["gaps"] == 0

    # …and the tape still reads as the receive order it was: 1..4 served, 5..40 dropped.
    rows = list(read_tape(fragments))
    assert [r["arrival_seq"] for r in rows if r["event_kind"] == BOOK] == [1, 2, 3, 4]
    marker = next(r for r in rows if r["event_kind"] == GAP)
    assert (marker["missing_from"], marker["missing_to"], marker["dropped"]) == (5, 40, 36)


def test_a_gap_names_the_exact_hole_so_no_replay_can_cross_it(tmp_path):
    """A hole in the receive sequence is named; nothing may be replayed across it."""
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    for seq in (1, 2, 7, 8):
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=seq))
    writer.close()

    reader = read_tape([path])
    rows = list(reader)
    kinds = [r["event_kind"] for r in rows]
    assert reader.status.failed is True
    assert reader.status.complete is False
    assert reader.status.dropped == 4 and reader.status.gaps == 1
    marker = next(r for r in rows if r["event_kind"] == GAP)
    assert (marker["missing_from"], marker["missing_to"], marker["dropped"]) == (3, 6, 4)
    # The marker sits between the two sides of the hole: a replay stops at it.
    assert kinds[:kinds.index(GAP)].count(BOOK) == 2, kinds
    served = [r["arrival_seq"] for r in rows if r["event_kind"] == BOOK]
    assert served == [1, 2, 7, 8]
    assert not [seq for seq in served
                if marker["missing_from"] <= seq <= marker["missing_to"]]
    assert rows[-1]["event_kind"] == RUN_END and rows[-1]["complete"] is False


def test_a_full_queue_accumulates_a_gap_and_never_claims_complete(tmp_path):
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, queue_max=2, flush_secs=1e9)
    for _ in range(6):
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    assert writer.dropped == 4
    writer.close()

    assert writer.dropped == 4 and writer.gaps == 1
    rows = list(read_tape([path]))
    gaps = [r for r in rows if r["event_kind"] == GAP]
    assert len(gaps) == 1
    assert gaps[0]["dropped"] == 4
    assert gaps[0]["missing_from"] == 3 and gaps[0]["missing_to"] == 6
    assert gaps[0]["valid"] is False and gaps[0]["invalid_reason"]
    assert len(books(rows)) == 2, "the records that fit are kept"

    reader = read_tape([path])
    list(reader)
    assert reader.status.dropped == 4 and reader.status.gaps == 1
    assert reader.status.complete is False, "a gap means the acceptance fails"
    assert "gap" in reader.status.failure_reason

    # The manifest carries the failure too: the whole segment is identifiable.
    doc = read_manifest(reader.status.paths[0].parent / "l2.manifest.json")
    assert doc["segments"][0]["dropped"] == 4
    assert doc["segments"][0]["gaps"] == 1


class FakeClock:
    """A hand-cranked monotonic clock for the writer's flush deadline."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, secs: float) -> None:
        self.now += secs


def test_a_full_queue_still_reaches_the_flush_deadline(tmp_path):
    """F10: queue_max=1, flush_secs=1, writes at t=0/2/5 - every one of them lands.

    Before the fix the full queue returned before the deadline check, so the write at
    t=2 and the one at t=5 were both dropped *and* the flush that would have emptied the
    queue never ran: accepted=1, dropped=2, last_flush=0, and the tape only moved when
    something outside it flushed.
    """
    path = tmp_path / "l2.jsonl"
    clock = FakeClock()
    writer = TapeWriter(path, run_id=RUN_ID, queue_max=1, flush_secs=1.0, clock=clock)
    for at in (0.0, 2.0, 5.0):
        clock.now = at
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))

    assert writer.records == 3, "every write of the run was accepted"
    assert writer.dropped == 0 and writer.gaps == 0, "a one-slot queue is not a gap"
    assert writer._last_flush == 5.0, "the deadline moved with each flush"
    assert len(writer._queue) == 1, "only the record written at t=5 is still queued"
    writer.close()

    reader = read_tape([path])
    rows = list(reader)
    assert [r["arrival_seq"] for r in books(rows)] == [1, 2, 3]
    assert reader.status.complete is True and reader.status.dropped == 0


def test_a_quiet_tape_flushes_on_its_deadline_without_a_new_record(tmp_path):
    """F10: the periodic flush is a time promise, not a side effect of accepting a message.

    One record is written and nothing else ever arrives; the caller's event-loop timer
    calls ``tick()`` and the record must reach the disk by its deadline.
    """
    path = tmp_path / "l2.jsonl"
    clock = FakeClock()
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=1.0, clock=clock)
    writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    assert writer._queue, "nothing is on the disk yet: the deadline has not passed"

    clock.advance(0.5)
    writer.tick()
    assert writer._queue, "half the interval is not a deadline"

    clock.advance(0.6)
    writer.tick()
    assert not writer._queue, "no new record arrived, the deadline still had to be met"
    rows = books(list(read_tape([path])))
    assert [r["arrival_seq"] for r in rows] == [1], "the quiet record is on the disk"
    writer.close()
    assert list(read_tape([path]))[-1]["event_kind"] == RUN_END


def test_a_gap_marker_never_starves_a_one_slot_queue(tmp_path):
    """F10: the marker that names a gap must not take the queue hostage.

    With one slot, a burst fills it, the overflow is dropped into a pending gap, and the
    next deadline writes the marker *and* clears it - so the record after the burst is
    accepted again instead of every later record being dropped forever.
    """
    path = tmp_path / "l2.jsonl"
    clock = FakeClock()
    writer = TapeWriter(path, run_id=RUN_ID, queue_max=1, flush_secs=1.0, clock=clock)
    writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    clock.advance(0.25)
    for _ in range(3):  # a burst with no deadline in sight: dropped, and named
        writer.write_event(make_book_event(bids=[["100.06", "0.001"]], arrival_seq=None))
    assert writer.dropped == 3 and writer._pending_gap is not None

    clock.advance(1.0)  # the deadline passes: the gap is written out with its range
    writer.tick()
    assert writer._pending_gap is None and not writer._queue

    clock.advance(1.0)  # and the next record is accepted again, not starved
    writer.write_event(make_book_event(bids=[["100.07", "0.001"]], arrival_seq=None))
    assert writer.records == 2, "the queue recovered after the gap"
    assert writer.dropped == 3
    writer.close()

    reader = read_tape([path])
    rows = list(reader)
    gap = next(r for r in rows if r["event_kind"] == GAP)
    assert (gap["missing_from"], gap["missing_to"], gap["dropped"]) == (2, 4, 3)
    assert [r["arrival_seq"] for r in books(rows)] == [1, 5]
    assert reader.status.complete is False, "a dropped range still fails the recording"


def test_a_skipped_arrival_seq_is_recorded_as_a_gap(tmp_path):
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=5))
    writer.close()
    reader = read_tape([path])
    rows = list(reader)
    gap = [r for r in rows if r["event_kind"] == GAP]
    assert len(gap) == 1
    assert gap[0]["missing_from"] == 2 and gap[0]["missing_to"] == 4
    assert reader.status.dropped == 3 and reader.status.complete is False
    assert [r["arrival_seq"] for r in books(rows)] == [1, 5]


def test_a_disk_write_failure_aborts_the_recorder_and_surfaces(tmp_path, monkeypatch):
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))

    class Boom:
        """A broken device: every operation on the handle fails."""

        def write(self, _text):
            raise OSError(28, "No space left on device")

        def flush(self):
            raise OSError(28, "No space left on device")

        def close(self):
            raise OSError(28, "No space left on device")

    monkeypatch.setattr(writer, "_handle", Boom())
    with pytest.raises(TapeWriteError) as caught:
        writer.write_event(make_book_event(bids=[["100.06", "0.001"]], arrival_seq=None))
    assert "disk write failed" in str(caught.value)
    assert writer.failed is True
    with pytest.raises(TapeWriteError):
        writer.write_event(make_book_event(bids=[["100.07", "0.001"]], arrival_seq=None))
    writer.close()  # must not raise a second time; the recorder is already dead

    reader = read_tape([path])
    list(reader)
    assert reader.status.complete is False, "no run-end: the segment is incomplete"


def test_close_flushes_and_rotation_keeps_continuous_numbering(tmp_path):
    path = tmp_path / "l2" / "l2_NVDA.jsonl"
    # Nothing flushes on its own: only the rotation and the final close do.
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=1e9, rotate_bytes=2600, symbol="NVDA")
    for _ in range(20):
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    assert len(list(read_tape([path]))) == 1, "the queued records are not on disk yet"
    writer.close()

    fragments = sorted(path.parent.glob("*.jsonl"))
    assert len(fragments) >= 3, fragments
    assert fragments[0] == path
    assert [f.name for f in fragments[:3]] == [
        "l2_NVDA.jsonl", "l2_NVDA_part0002.jsonl", "l2_NVDA_part0003.jsonl",
    ]
    reader = read_tape(fragments)
    rows = list(reader)
    assert [r["arrival_seq"] for r in books(rows)] == list(range(1, 21))
    assert reader.status.complete is True

    doc = read_manifest(writer.manifest_path)
    indexes = [entry["segment_index"] for entry in doc["segments"]]
    assert indexes == list(range(1, len(fragments) + 1)), "continuous segment numbering"
    assert all(entry["closed"] for entry in doc["segments"])
    assert sum(entry["lines"] for entry in doc["segments"]) == len(rows)
    assert sum(entry["records"] for entry in doc["segments"]) == 20


def test_a_process_that_dies_without_a_run_end_is_incomplete(tmp_path):
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    # No close(): the process died in the middle of the run.
    reader = read_tape([path])
    rows = list(reader)
    assert not [r for r in rows if r["event_kind"] == RUN_END]
    assert reader.status.complete is False
    assert "no_run_end" in reader.status.failure_reason
    doc = read_manifest(writer.manifest_path)
    assert doc["segments"][-1]["closed"] is False


# --------------------------------------------------------------------- corruption


def test_a_truncated_tail_is_reported_and_the_good_records_are_readable(tmp_path):
    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id=RUN_ID, flush_secs=0.0) as writer:
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
        writer.write_event(make_book_event(bids=[["100.06", "0.001"]], arrival_seq=None))
    with path.open("ab") as handle:  # the crash cut the next record in half
        handle.write(b'{"schema_version": 1, "run_id": "fixture", "arrival_seq": 3, "sym')

    reader = read_tape([path])
    rows = list(reader)
    assert len(books(rows)) == 2, "the complete records before the cut are readable"
    assert reader.status.truncated_tail is True
    assert reader.status.truncated_at.startswith(str(path))


def test_an_old_sessions_truncated_tail_does_not_hide_a_later_complete_session(tmp_path):
    """F11: the reader must keep reading past an old crash, not stop at its cut line.

    A session that died mid-line leaves a truncated tail; the restart writes a new
    fragment under a new session and ends it with its own run-end. Reading stopped at the
    old cut, so the complete new session was never seen and the handle stayed open.
    """
    path = tmp_path / "l2" / "l2_NVDA.jsonl"
    # Session one died mid-line: its fragment ends without a newline and without a run-end.
    path.parent.mkdir(parents=True, exist_ok=True)
    crash_after(path, [hand_record(session_id="old", arrival_seq=1)])

    # The restart appends a fragment of its own under a new session, and closes it.
    revived = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0, symbol="NVDA")
    revived.write_event(make_book_event(bids=[["100.06", "0.001"]], arrival_seq=None))
    revived.close()

    fragments = sorted(path.parent.glob("*.jsonl"))
    assert len(fragments) == 2, "the restart appended its own fragment"
    reader = read_tape(fragments)
    rows = list(reader)

    assert [s.session_id for s in reader.status.sessions] == ["old", revived.session_id], (
        "the new session is read, not hidden behind the old cut line"
    )
    assert [s.complete for s in reader.status.sessions] == [False, True]
    assert "truncated_tail" in reader.status.sessions[0].reason
    assert reader.status.complete is False, "the old session stays incomplete"
    assert reader.status.truncated_tail is True
    assert [(r["venue"], r["arrival_seq"]) for r in books(rows)] == [
        ("ONDO", 1), ("ONDO", 1),
    ], "both the old session's record and the new session's record were read"
    assert [r["bids"][0][0] for r in books(rows)] == ["100.05", "100.06"]


def test_damage_inside_one_session_after_a_cut_is_a_hard_failure(tmp_path):
    """F11: a cut tail that the *same* session continues is corruption, not a tail.

    Skipping it would silently drop the middle of a session and call the rest of it
    readable. Only a cut at the end of a session is a tail.
    """
    first = crash_after(tmp_path / "l2_part0001.jsonl", [
        hand_record(session_id="s1", arrival_seq=1),
    ])
    second = write_records(tmp_path / "l2_part0002.jsonl", [
        hand_record(session_id="s1", arrival_seq=2),
    ])

    reader = read_tape([first, second])
    with pytest.raises(TapeCorruptionError) as caught:
        list(reader)
    assert "damage inside a session" in str(caught.value)
    assert reader.status.truncated_tail is True
    assert [s.complete for s in reader.status.sessions] == [False]


def test_reading_a_tape_never_keeps_a_file_handle(tmp_path):
    """F11: every exit path releases the fragment, so Windows can rename/delete it.

    An open tape file cannot be renamed or deleted on Windows, which is how the leak
    the review found showed up: the reader stopped at a cut line and kept the generator
    (and its file) alive.
    """
    def assert_released(path: Path) -> None:
        moved = path.with_name(path.name + ".moved")
        path.rename(moved)
        moved.unlink()

    # 1. a normal end, with an old cut tail followed by a complete new session.
    first = crash_after(tmp_path / "normal_part0001.jsonl", [
        hand_record(session_id="old", arrival_seq=1),
    ])
    second = write_records(tmp_path / "normal_part0002.jsonl", [
        hand_record(session_id="new", arrival_seq=1),
        hand_record(session_id="new", arrival_seq=2, event_kind=RUN_END, complete=True),
    ])
    reader = read_tape([first, second])  # held alive: the reader owns the handles
    list(reader)
    assert reader.status.truncated_tail is True
    assert_released(first)
    assert_released(second)

    # 2. an early stop: the caller stops iterating long before the fragments are done.
    abandoned = write_records(tmp_path / "abandoned.jsonl", [
        hand_record(session_id="s-early", arrival_seq=seq) for seq in (1, 2, 3)
    ])
    reader = read_tape([abandoned])
    next(reader)
    reader.close()
    assert_released(abandoned)

    # 3. a hard failure in the middle of a fragment.
    broken = write_records(tmp_path / "broken.jsonl", [
        hand_record(session_id="s-bad", arrival_seq=1),
        hand_record(session_id="s-bad", arrival_seq=2),
    ])
    lines = broken.read_text(encoding="utf-8").splitlines()
    lines[1] = "{not json at all}"
    broken.write_text("\n".join(lines) + "\n", encoding="utf-8")
    failing = read_tape([broken])
    with pytest.raises(TapeCorruptionError):
        list(failing)
    assert_released(broken)


def test_a_bad_line_in_the_middle_is_a_hard_failure(tmp_path):
    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id=RUN_ID, flush_secs=0.0) as writer:
        for _ in range(3):
            writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    lines = path.read_text(encoding="utf-8").splitlines()
    lines[2] = "{not json at all}"  # a damaged line in the middle, newline intact
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    reader = read_tape([path])
    with pytest.raises(TapeCorruptionError) as caught:
        list(reader)
    assert "not valid JSON" in str(caught.value)
    assert reader.status.truncated_tail is False, "a middle bad line is not a tail cut"


def test_a_bad_last_line_with_its_newline_is_a_hard_failure(tmp_path):
    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id=RUN_ID, flush_secs=0.0) as writer:
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"broken": \n')

    with pytest.raises(TapeCorruptionError):
        list(read_tape([path]))


def test_an_unknown_or_missing_schema_version_is_rejected(tmp_path):
    """Schema 2 is the current version and 1 is still read: 3 and "absent" are not.

    The version this test calls unknown moved from 2 to 3 when the writer moved to
    schema 2 (per-arrival instrument metadata); the rule itself is unchanged.
    """
    good = hand_record()
    for broken in ({**good, "schema_version": 3}, {**good, "schema_version": 2},
                   {k: v for k, v in good.items() if k != "schema_version"}):
        if broken.get("schema_version") == 2:
            # The current version, not an unknown one: read, never refused.
            path = write_records(tmp_path / "v2.jsonl", [broken])
            assert list(read_tape([path]))
            continue
        path = write_records(tmp_path / f"v{broken.get('schema_version')}.jsonl", [broken])
        with pytest.raises(TapeSchemaError):
            list(read_tape([path]))


def test_a_duplicate_session_id_is_rejected(tmp_path):
    path = write_records(tmp_path / "l2.jsonl", [
        hand_record(session_id="s1", arrival_seq=1),
        hand_record(session_id="s2", arrival_seq=1),
        hand_record(session_id="s1", arrival_seq=2),
    ])
    with pytest.raises(TapeSessionError):
        list(read_tape([path]))


def test_a_reverse_or_repeated_arrival_seq_is_rejected(tmp_path):
    reverse = write_records(tmp_path / "reverse.jsonl", [
        hand_record(session_id="s1", arrival_seq=5),
        hand_record(session_id="s1", arrival_seq=3),
    ])
    with pytest.raises(TapeOrderError):
        list(read_tape([reverse]))
    repeated = write_records(tmp_path / "repeated.jsonl", [
        hand_record(session_id="s1", arrival_seq=5),
        hand_record(session_id="s1", arrival_seq=5),
    ])
    with pytest.raises(TapeOrderError):
        list(read_tape([repeated]))


def test_the_reader_refuses_a_pickle_and_the_module_never_imports_it():
    """A tape is JSONL: an untrusted file must never be unpickled (plan 5.1)."""
    source = (Path(__file__).resolve().parents[1] / "src" / "market_tape.py").read_text(
        encoding="utf-8",
    )
    assert "import pickle" not in source
    assert "pickle.load" not in source
    assert "pickle.dump" not in source


# ------------------------------------------------------------------ writer contract


def test_the_writer_keeps_a_legitimate_arrival_seq_and_refuses_a_backwards_one(tmp_path):
    path = tmp_path / "l2.jsonl"
    writer = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0)
    event = make_book_event(bids=[["100.05", "0.001"]], arrival_seq=7)
    writer.write_event(event)
    assert event["arrival_seq"] == 7, "the caller's event is not rewritten"
    assert writer.next_arrival_seq == 8
    writer.write_event(make_book_event(bids=[["100.06", "0.001"]], arrival_seq=None))
    assert writer.next_arrival_seq == 9
    with pytest.raises(TapeOrderError):
        writer.write_event(make_book_event(bids=[["100.07", "0.001"]], arrival_seq=3))
    assert writer.next_arrival_seq == 9, "a refused event consumes no sequence"
    writer.close()

    rows = books(list(read_tape([path])))
    assert [r["arrival_seq"] for r in rows] == [7, 8]


def test_replay_preserves_the_arrival_order_and_never_sorts_by_exchange_time(tmp_path):
    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id=RUN_ID, flush_secs=0.0) as writer:
        for n, ts in enumerate([5_000_000_000, 4_000_000_000, 3_000_000_000]):
            writer.write_event(make_book_event(
                bids=[[f"100.0{n}", "0.001"]], arrival_seq=None, ts_event_ns=ts,
            ))
    rows = books(list(read_tape([path])))
    assert [r["arrival_seq"] for r in rows] == [1, 2, 3]
    assert [r["ts_event_ns"] for r in rows] == [5_000_000_000, 4_000_000_000, 3_000_000_000]
    assert [r["bids"][0][0] for r in rows] == ["100.00", "100.01", "100.02"]


def test_a_restart_appends_a_new_session_without_repeating_the_header(tmp_path):
    path = tmp_path / "l2.jsonl"
    first = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0, symbol="NVDA")
    first.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    first.close()
    session_one = first.session_id
    header_lines = [line for line in path.read_text(encoding="utf-8").splitlines()
                    if json.loads(line)["event_kind"] == RUN_START]
    assert len(header_lines) == 1

    second = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0, symbol="NVDA")
    second.write_event(make_book_event(bids=[["100.06", "0.001"]], arrival_seq=None))
    second.close()
    assert second.session_id != session_one

    fragments = sorted(tmp_path.glob("*.jsonl"))
    assert len(fragments) == 2, "the restart appends a fragment, it never rewrites one"
    reader = read_tape(fragments)
    rows = list(reader)
    assert len([r for r in rows if r["event_kind"] == RUN_START]) == 1, "one header"
    assert [s.session_id for s in reader.status.sessions] == [session_one, second.session_id]
    assert reader.status.complete is True, "both sessions ended with their own run-end"
    assert len(books(rows)) == 2


def test_a_restart_does_not_hide_a_previous_session_without_a_run_end(tmp_path):
    path = tmp_path / "l2.jsonl"
    crashed = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0, symbol="NVDA")
    crashed.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    # No close(): the first session never wrote its run-end.
    revived = TapeWriter(path, run_id=RUN_ID, flush_secs=0.0, symbol="NVDA")
    revived.write_event(make_book_event(bids=[["100.06", "0.001"]], arrival_seq=None))
    revived.close()

    reader = read_tape(sorted(tmp_path.glob("*.jsonl")))
    list(reader)
    assert [s.session_id for s in reader.status.sessions] == [
        crashed.session_id, revived.session_id,
    ]
    assert [s.complete for s in reader.status.sessions] == [False, True]
    assert "no_run_end" in reader.status.sessions[0].reason
    assert reader.status.complete is False
    assert "no_run_end" in reader.status.failure_reason


def test_the_writer_is_a_context_manager_and_closes_the_run(tmp_path):
    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id=RUN_ID) as writer:
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]], arrival_seq=None))
    reader = read_tape([path])
    rows = list(reader)
    assert rows[-1]["event_kind"] == RUN_END
    assert rows[-1]["complete"] is True
    assert reader.status.complete is True
    assert reader.status.failure_reason is None


# --------------------------------------------------------------------- manifest


def test_the_run_manifest_lists_every_fragment_in_order(tmp_path):
    l2 = tmp_path / "l2"
    for symbol, base in (("NVDA", "100.05"), ("TSLA", "200.05")):
        writer = TapeWriter(l2 / f"l2_{symbol}_ONDO-ASTER.jsonl", run_id=RUN_ID,
                            symbol=symbol, flush_secs=0.0, rotate_bytes=220)
        book = BookTape(writer, symbol=symbol, venue="ONDO",
                        instrument_id=f"{symbol}-USD-PERP.ONDO", coverage_limit=100)
        for _ in range(4):
            ops, _bids, _asks = full_book("ONDO", base=base)
            book.apply_batch(ops, ts_event_ns=1, ts_init_ns=2)
        writer.close()

    manifest = write_run_manifest(l2)
    assert manifest is not None
    doc = read_run_manifest(l2)
    by_tape: dict[str, list[int]] = {}
    for fragment in doc["fragments"]:
        by_tape.setdefault(fragment["tape"], []).append(fragment["segment_index"])
    assert set(by_tape) == {"l2_NVDA_ONDO-ASTER.jsonl", "l2_TSLA_ONDO-ASTER.jsonl"}
    assert doc["fragments"][0]["tape"] == "l2_NVDA_ONDO-ASTER.jsonl"
    for tape, indexes in by_tape.items():
        assert indexes == list(range(1, len(indexes) + 1)), tape
    assert any(len(indexes) > 1 for indexes in by_tape.values()), "rotated fragments"
    assert [fragment["order"] for fragment in doc["fragments"]] == list(
        range(1, len(doc["fragments"]) + 1),
    )
    assert all(fragment["closed"] for fragment in doc["fragments"])

    via_manifest = list(read_tape([manifest]))
    via_dir = list(read_tape([l2]))
    assert len(via_manifest) == len(via_dir)
    assert {r["symbol"] for r in books(via_manifest)} == {"NVDA", "TSLA"}


def test_the_run_manifest_is_absent_when_nothing_was_recorded(tmp_path):
    assert write_run_manifest(tmp_path / "l2") is None
    assert not (tmp_path / "l2" / "manifest.json").exists()


def test_every_error_type_is_a_tape_error():
    for error in (TapeCorruptionError, TapeOrderError, TapeSchemaError,
                  TapeSessionError, TapeTypeError, TapeWriteError):
        assert issubclass(error, TapeError)

#!/usr/bin/env python3
"""Offline tests for ``src/analysis/ondo_depth.py`` (plan 5.2, task 5 / P2).

Everything here is offline and stdlib-only: the tapes are real files under
``tmp_path`` written through ``market_tape.TapeWriter`` (or hand-written lines when
the test is about a hole in the recording), and no test touches the network, a
LiveNode or a real credential.

    .venv\\Scripts\\python.exe -m pytest tests/test_ondo_depth.py -q -p no:cacheprovider
"""

from __future__ import annotations

import csv
import dataclasses
import json
import sys
from decimal import Decimal
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import analysis.ondo_depth as ondo_depth  # noqa: E402
from analysis.ondo_depth import (  # noqa: E402
    DEFAULT_FUTURE_TOLERANCE_MS,
    DEFAULT_MAX_AGE_MS,
    DEFAULT_MAX_EVENT_AGE_MS,
    DEFAULT_MAX_SKEW_MS,
    DEFAULT_SYMBOLS,
    DEFAULT_VENUES,
    MAPPING_UNVERIFIED,
    NON_TRADING_ACTIONS,
    REJECT_BELOW_ONE_STEP,
    REJECT_BOOK_INVALID,
    REJECT_CROSSED,
    REJECT_DISCONNECTED,
    REJECT_EMPTY_BOOK,
    REJECT_EVENT_AGE,
    REJECT_EVENT_SKEW,
    REJECT_FEE_UNKNOWN,
    REJECT_FUTURE_TIME,
    REJECT_INSUFFICIENT_DEPTH,
    REJECT_MARKET_HALTED,
    REJECT_METADATA_STALE,
    REJECT_METADATA_UNKNOWN,
    REJECT_NO_BOOK,
    REJECT_ONE_SIDED,
    REJECT_RECORDING_GAP,
    REJECT_SESSION_MISMATCH,
    REJECT_STALE_BOOK,
    REJECT_STEP_UNKNOWN,
    REJECT_UNKNOWN_TIME,
    STEP_ORIGIN_METADATA,
    STEP_ORIGIN_OVERRIDE,
    STEP_ORIGIN_UNKNOWN,
    TAPE_SCHEMA_VERSIONS,
    BookState,
    DepthError,
    LegMetadata,
    LegReading,
    Params,
    QualityParams,
    ReplayOrderError,
    TimePoint,
    VwapResult,
    analyse_run,
    build_parser,
    common_quantity,
    common_step,
    event_age_status,
    leg_step,
    main,
    metadata_state,
    positive_decimal,
    quality_gate,
    replay_events,
    target_base_quantity,
    ts_utc,
    vwap_for_quantity,
)
from analysis.opportunities import hourly_bps  # noqa: E402
import market_tape  # noqa: E402
from market_tape import (  # noqa: E402
    BookTape,
    TapeWriter,
    funding_event,
    instrument_event,
    quote_event,
    read_tape,
    status_event,
    write_run_manifest,
)
from spread_watch import RESERVE_BPS  # noqa: E402

RUN_ID = "20260914T000000Z"
SESSION = f"{RUN_ID}-aaaa"
SESSION_B = f"{RUN_ID}-bbbb"
MS = 1_000_000


def tape_schema() -> int:
    """The tape schema the installed writer/reader speak.

    Records that carry the newer, per-arrival metadata are stamped with whatever the
    installed ``market_tape`` currently writes (contract A: the reader accepts both
    version 1 and version 2), while the tests that pin *old tape* behaviour stamp an
    explicit 1. That way the two are never confused and neither depends on which
    version happens to be current.
    """
    return market_tape.SCHEMA_VERSION


# --------------------------------------------------------------------- fixtures


def rec(
    seq: int,
    kind: str = "book",
    *,
    venue: str | None = "ONDO",
    symbol: str | None = "NVDA",
    session: str | None = SESSION,
    run_id: str = RUN_ID,
    event_ns: int | None = None,
    init_ns: int | None = None,
    mono_ns: int | None = None,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
    valid: bool = True,
    invalid: str | None = None,
    schema: int = 1,
    **extra,
) -> dict:
    """One hand-written tape record: the reader's schema, no writer involved.

    ``schema`` defaults to the version-1 record shape (no per-arrival metadata, no
    ``size_increment``); a test that writes the newer fields says
    ``schema=tape_schema()`` so the stamp matches its content.
    """
    record = {
        "schema_version": schema,
        "run_id": run_id,
        "session_id": session,
        "arrival_seq": seq,
        "symbol": symbol,
        "venue": venue,
        "instrument_id": None,
        "event_kind": kind,
        "ts_event_ns": event_ns,
        "ts_init_ns": init_ns,
        "recorded_mono_ns": mono_ns,
        "bids": bids,
        "asks": asks,
        "coverage_limit": None,
        "source": "fixture",
        "valid": valid,
        "invalid_reason": invalid,
    }
    record.update(extra)
    return record


def bookstate(
    venue: str = "ONDO",
    *,
    bids=(("100.00", "1"),),
    asks=(("100.10", "1"),),
    event_ns: int | None = 1_000 * MS,
    init_ns: int | None = None,
    mono_ns: int | None = 5_000 * MS,
    session: str | None = SESSION,
    valid: bool = True,
    reason: str | None = None,
    arrival_seq: int = 1,
) -> BookState:
    """One leg's book state, with coherent times unless a test says otherwise.

    ``init_ns`` defaults to one millisecond after ``event_ns`` (what a real tape
    shows: the venue event time, then its local receipt) so that a test which only
    cares about the age does not accidentally trip the future-timestamp rule.
    """
    if init_ns is None and event_ns is not None:
        init_ns = event_ns + MS
    return BookState(
        venue=venue, symbol="NVDA", instrument_id=f"NVDA-USD-PERP.{venue}",
        bids=tuple((Decimal(p), Decimal(s)) for p, s in bids),
        asks=tuple((Decimal(p), Decimal(s)) for p, s in asks),
        ts_event_ns=event_ns, ts_init_ns=init_ns, receive_mono_ns=mono_ns,
        session_id=session, arrival_seq=arrival_seq, coverage_limit=100,
        source="snapshot", valid=valid, invalid_reason=reason,
    )


def write_lines(path: Path, records: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in records), encoding="utf-8",
    )
    return path


def markers(*, session: str = SESSION, run_id: str = RUN_ID, symbol="NVDA",
            complete: bool = True) -> tuple[dict, dict]:
    """The run_start / run_end pair every complete tape needs (lifecycle markers)."""
    start = {
        "schema_version": 1, "run_id": run_id, "session_id": session, "arrival_seq": None,
        "symbol": symbol, "venue": None, "instrument_id": None, "event_kind": "run_start",
        "ts_event_ns": None, "ts_init_ns": None, "recorded_mono_ns": None, "bids": None,
        "asks": None, "coverage_limit": None, "source": "tape", "valid": True,
        "invalid_reason": None,
    }
    end = dict(start, event_kind="run_end", complete=complete)
    return start, end


# --------------------------------------------------------- vwap exactness


def test_vwap_requires_full_quantity():
    """The brief's own fixture, verbatim."""
    levels = [(Decimal("100"), Decimal("1")), (Decimal("102"), Decimal("1"))]
    result = vwap_for_quantity(levels, Decimal("1.5"))
    assert result.vwap == Decimal("151") / Decimal("1.5")
    assert not result.insufficient_depth
    short = vwap_for_quantity(levels, Decimal("3"))
    assert short.insufficient_depth and short.vwap is None
    assert short.filled_qty == Decimal("2")


def test_vwap_never_extrapolates_the_last_level_and_reports_the_missing_quantity():
    levels = [(Decimal("100"), Decimal("1")), (Decimal("102"), Decimal("1"))]
    short = vwap_for_quantity(levels, Decimal("3"))
    # 151/1.5 = 100.66... is NOT the answer for 3 units: the third unit has no price.
    assert short.vwap is None, "a short book must not produce an average price"
    assert short.requested_qty == Decimal("3")
    assert short.filled_qty == Decimal("2")
    assert short.missing_qty == Decimal("1")
    assert short.insufficient_depth


def test_vwap_walks_in_the_given_order_and_stops_at_the_quantity():
    levels = [(Decimal("100"), Decimal("1")), (Decimal("102"), Decimal("1")),
              (Decimal("200"), Decimal("10"))]
    result = vwap_for_quantity(levels, Decimal("1.5"))
    # The deep third level is never touched, so it cannot move the average.
    assert result.vwap == Decimal("151") / Decimal("1.5")
    assert result.filled_qty == Decimal("1.5")
    only_first = vwap_for_quantity(levels, Decimal("1"))
    assert only_first.vwap == Decimal("100")


def test_vwap_result_is_exact_decimal_arithmetic_never_f64():
    levels = [(Decimal("3"), Decimal("1")), (Decimal("3"), Decimal("1")),
              (Decimal("4"), Decimal("1"))]
    result = vwap_for_quantity(levels, Decimal("3"))
    assert isinstance(result.vwap, Decimal)
    assert result.vwap == Decimal("10") / Decimal("3")
    assert str(result.vwap) != str(10.0 / 3.0)
    with pytest.raises(ValueError):
        vwap_for_quantity(levels, Decimal("0"))


def test_an_empty_book_is_insufficient_depth_not_a_zero_price():
    result = vwap_for_quantity([], Decimal("1"))
    assert isinstance(result, VwapResult)
    assert result.insufficient_depth and result.vwap is None
    assert result.filled_qty == Decimal("0")


# ------------------------------------------------------------- common step


def test_common_step_is_the_integer_scaled_lcm_not_the_max():
    assert common_step([Decimal("0.002"), Decimal("0.003")]) == Decimal("0.006")
    assert common_step([Decimal("0.002"), Decimal("0.003")]) != max(
        Decimal("0.002"), Decimal("0.003"),
    )
    # 0.006 is the smallest common multiple: 0.003/0.002 is not an integer.
    assert Decimal("0.006") / Decimal("0.002") == Decimal("3")
    assert Decimal("0.006") / Decimal("0.003") == Decimal("2")
    assert common_step([Decimal("0.01"), Decimal("0.1")]) == Decimal("0.1")
    assert common_step([Decimal("0.5"), Decimal("0.25")]) == Decimal("0.5")
    assert common_step([Decimal("1"), Decimal("0.001")]) == Decimal("1")
    assert common_step([Decimal("0.002"), Decimal("0.003"), Decimal("0.005")]) == Decimal(
        "0.03",
    )


def test_common_step_is_none_when_a_legs_step_is_not_known():
    assert common_step([Decimal("0.001"), None]) is None
    assert common_step([]) is None
    assert common_step([Decimal("0.001"), Decimal("0")]) is None
    assert common_step([Decimal("0.001"), Decimal("-1")]) is None


def test_common_quantity_floors_and_never_rounds_up():
    assert common_quantity(Decimal("1.0005"), Decimal("0.001")) == Decimal("1.000")
    assert common_quantity(Decimal("1.5"), Decimal("1")) == Decimal("1")
    assert common_quantity(Decimal("0.4"), Decimal("0.5")) == Decimal("0")


def test_target_base_quantity_uses_the_then_visible_bid_price():
    # $100 against a 100.00 bid with a 0.001 step is exactly 1.000 base units.
    assert target_base_quantity(Decimal("100"), Decimal("100.00"), Decimal("0.001")) == (
        Decimal("1.000")
    )
    # A $100 tier below one step's worth of price never rounds up.
    assert target_base_quantity(Decimal("1"), Decimal("100000"), Decimal("0.5")) == 0
    with pytest.raises(ValueError):
        target_base_quantity(Decimal("100"), Decimal("0"), Decimal("0.001"))


def test_a_quantity_step_is_never_inferred_from_the_size_precision():
    """F08: the old ``venue_step(size_precision)`` inference is gone on purpose.

    The module used to expose ``venue_step(3) == 0.001``, and the analysis used it:
    that is exactly how a run whose real increments were 0.002 / 0.003 produced a
    common step of 0.001. A step now comes from the instrument's own
    ``size_increment`` or not at all, so the function that could invent one no longer
    exists - and ``size_precision`` (still recorded in the tape and still printed in
    the fee snapshot as evidence) can no longer be turned into a step.
    """
    assert not hasattr(ondo_depth, "venue_step"), (
        "the precision-to-step inference is removed, not merely unused"
    )
    assert positive_decimal("0.001") == Decimal("0.001")
    assert positive_decimal(None) is None, "a step is never invented"
    assert positive_decimal(True) is None
    assert positive_decimal("0") is None
    assert positive_decimal("-0.5") is None
    assert positive_decimal("not a number") is None
    # a metadata payload that only carries the precision yields no step at all
    payload_only = metadata_state(
        {"metadata": {"size_precision": 3, "taker_fee_bps": "2.5"},
         "session_id": SESSION, "arrival_seq": 1}, "ONDO",
    )
    assert payload_only.size_precision == 3
    assert payload_only.size_increment is None
    assert leg_step("ONDO", payload_only, params()) == (None, STEP_ORIGIN_UNKNOWN)
    assert leg_step(
        "ONDO", payload_only, params(steps={"ONDO": Decimal("0.002")}),
    ) == (Decimal("0.002"), STEP_ORIGIN_OVERRIDE)
    # a *stale* metadata state carries no usable increment either, even when the record
    # repeated one: nothing is derived from a payload the tape called untrustworthy
    stale = LegMetadata(venue="ONDO", known=False, stale=True,
                        size_increment=Decimal("0.002"), arrival_seq=1)
    assert stale.usable is False
    assert leg_step("ONDO", stale, params()) == (None, STEP_ORIGIN_UNKNOWN)


# ----------------------------------------------------------------- replay


def test_replay_updates_the_book_by_arrival_seq_only():
    """A later arrival wins, even when its exchange event time is older."""
    first = rec(1, event_ns=5_000 * MS, mono_ns=1 * MS,
                bids=[["100.00", "1"]], asks=[["100.10", "1"]])
    second = rec(2, event_ns=1_000 * MS, mono_ns=2 * MS,
                 bids=[["90.00", "1"]], asks=[["90.10", "1"]])
    steps = list(replay_events([first, second]))
    assert steps[0].books["ONDO"].best_bid == Decimal("100.00")
    # receive order, not exchange order: the record that arrived second is the state
    assert steps[1].books["ONDO"].best_bid == Decimal("90.00")
    assert steps[1].arrival_seq == 2
    assert steps[1].record is second


def test_replay_refuses_an_out_of_order_arrival_seq_instead_of_sorting():
    first = rec(1, event_ns=1_000 * MS, bids=[["100.00", "1"]], asks=[["100.10", "1"]])
    second = rec(2, event_ns=1_000 * MS, bids=[["90.00", "1"]], asks=[["90.10", "1"]])
    with pytest.raises(ReplayOrderError):
        list(replay_events([second, first]))
    with pytest.raises(ReplayOrderError):
        list(replay_events([first, first]))


def test_a_quote_never_refreshes_the_books_own_time():
    steps = list(replay_events([
        rec(1, event_ns=1_000 * MS, init_ns=1_001 * MS, mono_ns=10 * MS,
            bids=[["100.00", "1"]], asks=[["100.10", "1"]]),
        rec(2, kind="quote", event_ns=9_000 * MS, init_ns=9_001 * MS, mono_ns=9_002 * MS,
            bid="100.00", ask="100.10", bid_size="1", ask_size="1"),
    ]))
    quote_step = steps[1]
    book = quote_step.books["ONDO"]
    # the depth keeps the book record's own receipt and event time
    assert book.receive_mono_ns == 10 * MS
    assert book.ts_event_ns == 1_000 * MS
    assert book.arrival_seq == 1
    # the quote's own time lives only on the quote
    assert quote_step.quotes["ONDO"].receive_mono_ns == 9_002 * MS
    assert quote_step.quotes["ONDO"].ts_event_ns == 9_000 * MS


def test_a_disconnect_and_a_new_snapshot_toggle_the_leg_state():
    steps = list(replay_events([
        rec(1, kind="status", venue="ONDO", action="HALT",
            reason="adapter:disconnected", mono_ns=1 * MS),
        rec(2, kind="status", venue="ONDO", action="HALT",
            reason="adapter:snapshot_ready", mono_ns=2 * MS),
        rec(3, kind="status", venue="ONDO", action="HALT",
            reason="adapter:connecting", mono_ns=3 * MS),
    ]))
    assert steps[0].disconnected["ONDO"] is True
    assert steps[1].disconnected["ONDO"] is False
    # a bare local feed notice is not a recovery, but it does not invalidate either
    assert steps[2].disconnected["ONDO"] is False


def test_a_gap_clears_every_legs_depth_and_names_its_range():
    steps = list(replay_events([
        rec(1, mono_ns=1 * MS, bids=[["100.00", "1"]], asks=[["100.10", "1"]]),
        {"schema_version": 1, "run_id": RUN_ID, "session_id": SESSION,
         "arrival_seq": None, "symbol": "NVDA", "venue": None, "instrument_id": None,
         "event_kind": "gap", "ts_event_ns": None, "ts_init_ns": None,
         "recorded_mono_ns": 2 * MS, "bids": None, "asks": None, "coverage_limit": None,
         "source": "queue", "valid": False,
         "invalid_reason": "recording_gap:queue_full", "dropped": 3,
         "missing_from": 2, "missing_to": 4},
        rec(5, mono_ns=3 * MS, bids=[["100.00", "1"]], asks=[["100.10", "1"]]),
    ]))
    assert steps[0].books["ONDO"].arrival_seq == 1
    assert steps[1].gap is not None
    assert steps[1].gap.dropped == 3
    assert (steps[1].gap.missing_from, steps[1].gap.missing_to) == (2, 4)
    assert steps[1].gaps_seen == 1 and steps[1].dropped_seen == 3
    # nothing is replayed across the hole: the old depth is unusable afterwards
    assert steps[1].books == {}
    assert steps[2].books["ONDO"].arrival_seq == 5


def test_replay_carries_the_funding_rate_of_each_leg():
    steps = list(replay_events([
        rec(1, kind="funding", venue="ONDO", rate="0.0000063", interval_secs=3600,
            mono_ns=1 * MS),
    ]))
    assert steps[0].funding["ONDO"].rate == Decimal("0.0000063")
    assert steps[0].funding["ONDO"].interval_secs == 3600


# ------------------------------------------------------------ quality gate


def test_the_quality_defaults_are_the_plans_research_parameters():
    assert DEFAULT_MAX_AGE_MS == 2_000
    assert DEFAULT_MAX_SKEW_MS == 500
    assert DEFAULT_FUTURE_TOLERANCE_MS == 1_000
    quality = QualityParams()
    assert (quality.max_age_ms, quality.max_skew_ms, quality.future_tolerance_ms) == (
        2_000, 500, 1_000,
    )
    # F09: the event age is measured and published, but a threshold on it is a decision
    # the caller makes explicitly - it is off unless asked for, because the age carries
    # the unmeasured local clock offset.
    assert DEFAULT_MAX_EVENT_AGE_MS is None
    assert quality.max_event_age_ms is None


def test_the_gate_passes_a_fresh_two_sided_pair():
    record = TimePoint(label="quote", event_ns=10_000 * MS, init_ns=10_001 * MS,
                       receive_mono_ns=10_010 * MS, session_id=SESSION)
    legs = [
        LegReading("ONDO", bookstate("ONDO", event_ns=10_000 * MS, mono_ns=10_005 * MS)),
        LegReading("ASTER", bookstate("ASTER", event_ns=10_000 * MS, mono_ns=10_006 * MS)),
    ]
    verdict = quality_gate(record, legs)
    assert verdict.quality_ok and verdict.reject_reason is None
    assert verdict.receive_age_ms == {"ONDO": Decimal("5"), "ASTER": Decimal("4")}
    assert verdict.event_skew_ms == Decimal("0")


def test_the_gate_measures_age_from_each_legs_own_book_time():
    """A fresh quote on a stale book does not pass: the depth time is the book's."""
    record = TimePoint(label="quote", event_ns=20_000 * MS, init_ns=20_001 * MS,
                       receive_mono_ns=20_000 * MS, session_id=SESSION)
    stale = LegReading("ONDO", bookstate(
        "ONDO", event_ns=19_999 * MS, init_ns=19_999 * MS, mono_ns=17_000 * MS,
    ))
    fresh = LegReading("ASTER", bookstate(
        "ASTER", event_ns=19_999 * MS, init_ns=19_999 * MS, mono_ns=19_999 * MS,
    ))
    verdict = quality_gate(record, [stale, fresh])
    assert not verdict.quality_ok
    assert verdict.reject_reason == REJECT_STALE_BOOK
    assert verdict.receive_age_ms["ONDO"] == Decimal("3000")
    assert verdict.receive_age_ms["ASTER"] == Decimal("1")


def test_the_gate_fails_a_book_older_than_the_receive_age_and_passes_just_inside_it():
    record = TimePoint(event_ns=10_000 * MS, init_ns=10_000 * MS,
                       receive_mono_ns=10_000 * MS, session_id=SESSION)
    fresh_leg = LegReading("ASTER", bookstate(
        "ASTER", event_ns=10_000 * MS, init_ns=10_000 * MS, mono_ns=9_999 * MS,
    ))

    exactly_two_seconds = LegReading("ONDO", bookstate(
        "ONDO", event_ns=10_000 * MS, init_ns=10_000 * MS, mono_ns=8_000 * MS,
    ))  # 10_000_000_000 - 8_000_000_000 = exactly 2000 ms
    verdict = quality_gate(record, [exactly_two_seconds, fresh_leg])
    assert verdict.quality_ok, "2000 ms is the threshold, not a failure"
    assert verdict.receive_age_ms["ONDO"] == Decimal("2000")

    one_nanosecond_over = LegReading("ONDO", bookstate(
        "ONDO", event_ns=10_000 * MS, init_ns=10_000 * MS, mono_ns=8_000 * MS - 1,
    ))
    verdict = quality_gate(record, [one_nanosecond_over, fresh_leg])
    assert verdict.reject_reason == REJECT_STALE_BOOK
    assert verdict.receive_age_ms["ONDO"] > Decimal("2000")


def test_the_gate_fails_two_legs_whose_own_event_times_skew():
    record = TimePoint(event_ns=10_000 * MS, init_ns=10_001 * MS,
                       receive_mono_ns=10_000 * MS, session_id=SESSION)
    legs = [
        LegReading("ONDO", bookstate("ONDO", event_ns=10_000 * MS, init_ns=10_000 * MS,
                                     mono_ns=10_000 * MS)),
        LegReading("ASTER", bookstate("ASTER", event_ns=9_400 * MS, init_ns=9_400 * MS,
                                      mono_ns=10_000 * MS)),
    ]
    verdict = quality_gate(record, legs)
    assert verdict.reject_reason == REJECT_EVENT_SKEW
    assert verdict.event_skew_ms == Decimal("600")
    edge = [
        LegReading("ONDO", bookstate("ONDO", event_ns=10_000 * MS, init_ns=10_000 * MS,
                                     mono_ns=10_000 * MS)),
        LegReading("ASTER", bookstate("ASTER", event_ns=9_500 * MS, init_ns=9_500 * MS,
                                      mono_ns=10_000 * MS)),
    ]
    assert quality_gate(record, edge).quality_ok, "500 ms is the threshold"


def test_the_gate_fails_a_future_exchange_timestamp():
    record = TimePoint(event_ns=10_001_500 * MS, init_ns=10_000 * MS,
                       receive_mono_ns=10_000 * MS, session_id=SESSION)
    legs = [
        LegReading("ONDO", bookstate("ONDO", event_ns=10_000 * MS, init_ns=10_000 * MS,
                                     mono_ns=10_000 * MS)),
        LegReading("ASTER", bookstate("ASTER", event_ns=10_000 * MS, init_ns=10_000 * MS,
                                      mono_ns=10_000 * MS)),
    ]
    verdict = quality_gate(record, legs)
    assert verdict.reject_reason == REJECT_FUTURE_TIME, (
        "the record's own event time claims to be 1500 ms in the future"
    )
    # a book from the future relative to the record it would be compared against
    record = TimePoint(event_ns=10_000 * MS, init_ns=10_000 * MS,
                       receive_mono_ns=10_000 * MS, session_id=SESSION)
    legs = [
        LegReading("ONDO", bookstate("ONDO", event_ns=10_002_000 * MS, mono_ns=10_000 * MS)),
        LegReading("ASTER", bookstate("ASTER", event_ns=10_000 * MS, init_ns=10_000 * MS,
                                      mono_ns=10_000 * MS)),
    ]
    assert quality_gate(record, legs).reject_reason == REJECT_FUTURE_TIME


def test_the_gate_fails_empty_one_sided_crossed_and_invalid_books():
    record = TimePoint(event_ns=10_000 * MS, init_ns=10_000 * MS,
                       receive_mono_ns=10_000 * MS, session_id=SESSION)
    other = LegReading("ASTER", bookstate("ASTER", event_ns=10_000 * MS,
                                          init_ns=10_000 * MS, mono_ns=10_000 * MS))

    def verdict(book):
        return quality_gate(record, [LegReading("ONDO", book), other])

    empty = bookstate("ONDO", bids=(), asks=(), valid=False, reason=REJECT_EMPTY_BOOK)
    assert verdict(empty).reject_reason == REJECT_EMPTY_BOOK
    one_sided = bookstate("ONDO", bids=(("100.00", "1"),), asks=(), valid=False,
                          reason=REJECT_ONE_SIDED)
    assert verdict(one_sided).reject_reason == REJECT_ONE_SIDED
    crossed = bookstate("ONDO", bids=(("101.00", "1"),), asks=(("100.10", "1"),),
                        valid=True)
    assert verdict(crossed).reject_reason == REJECT_CROSSED, (
        "a crossed book fails even when the record claims valid=True"
    )
    invalidated = bookstate("ONDO", valid=False, reason="feed-invalidated")
    assert verdict(invalidated).reject_reason == REJECT_BOOK_INVALID, (
        "a version-1 tape's 'feed-invalidated' marker folded the feed axis into the "
        "depth's validity (F18). It is refused on the depth, and the note says the v1 "
        "tape cannot separate the two axes - the reader never guesses which one it was"
    )
    assert "cannot say which axis" in verdict(invalidated).notes[0]


def test_the_gate_fails_a_gap_a_disconnect_unknown_metadata_and_a_missing_book():
    record = TimePoint(event_ns=10_000 * MS, init_ns=10_000 * MS,
                       receive_mono_ns=10_000 * MS, session_id=SESSION)
    fresh = bookstate("ONDO", event_ns=10_000 * MS, init_ns=10_000 * MS, mono_ns=10_000 * MS)
    other = bookstate("ASTER", event_ns=10_000 * MS, init_ns=10_000 * MS, mono_ns=10_000 * MS)
    assert quality_gate(
        record, [LegReading("ONDO", fresh), LegReading("ASTER", other)],
        recording_gap=True,
    ).reject_reason == REJECT_RECORDING_GAP
    assert quality_gate(
        record, [LegReading("ONDO", fresh, disconnected=True), LegReading("ASTER", other)],
    ).reject_reason == REJECT_DISCONNECTED
    assert quality_gate(
        record,
        [LegReading("ONDO", fresh, metadata_known=False), LegReading("ASTER", other)],
    ).reject_reason == REJECT_METADATA_UNKNOWN
    assert quality_gate(
        record, [LegReading("ONDO", None), LegReading("ASTER", other)],
    ).reject_reason == REJECT_NO_BOOK


def test_the_gate_fails_when_there_is_no_time_to_measure_freshness_with():
    no_receipt = TimePoint(event_ns=10_000 * MS, init_ns=10_000 * MS, session_id=SESSION)
    fresh = bookstate("ONDO", event_ns=10_000 * MS, init_ns=10_000 * MS, mono_ns=10_000 * MS)
    other = bookstate("ASTER", event_ns=10_000 * MS, init_ns=10_000 * MS, mono_ns=10_000 * MS)
    assert quality_gate(
        no_receipt, [LegReading("ONDO", fresh), LegReading("ASTER", other)],
    ).reject_reason == REJECT_UNKNOWN_TIME
    book_without_time = bookstate("ONDO", event_ns=None, init_ns=None, mono_ns=None)
    assert quality_gate(
        TimePoint(event_ns=10_000 * MS, init_ns=10_000 * MS, receive_mono_ns=10_000 * MS,
                  session_id=SESSION),
        [LegReading("ONDO", book_without_time), LegReading("ASTER", other)],
    ).reject_reason == REJECT_UNKNOWN_TIME


def test_cross_session_monotonic_times_are_not_comparable():
    """A book from another session has no measurable age, so it cannot pass."""
    record = TimePoint(event_ns=10_000 * MS, init_ns=10_001 * MS,
                       receive_mono_ns=10_000 * MS, session_id=SESSION_B)
    in_session_b = LegReading("ASTER", bookstate(
        "ASTER", session=SESSION_B, event_ns=10_000 * MS, init_ns=10_000 * MS,
        mono_ns=10_000 * MS,
    ))
    verdict = quality_gate(record, [
        LegReading("ONDO", bookstate("ONDO", session=SESSION, event_ns=10_000 * MS,
                                     init_ns=10_000 * MS, mono_ns=3_000 * MS)),
        in_session_b,
    ])
    assert verdict.reject_reason == REJECT_SESSION_MISMATCH
    assert verdict.receive_age_ms["ONDO"] is None, (
        "two sessions' monotonic stamps have no comparable difference"
    )
    # the same numbers inside one session are a perfectly good (and stale) reading
    verdict = quality_gate(record, [
        LegReading("ONDO", bookstate("ONDO", session=SESSION_B, event_ns=10_000 * MS,
                                     init_ns=10_000 * MS, mono_ns=3_000 * MS)),
        in_session_b,
    ])
    assert verdict.reject_reason == REJECT_STALE_BOOK
    assert verdict.receive_age_ms["ONDO"] == Decimal("7000")


def test_ts_utc_is_integer_arithmetic():
    assert ts_utc(1_755_000_000_500_000_000) == "2025-08-12T12:00:00.500Z"
    assert ts_utc(None) == ""


# --------------------------------------------------------------- one real tape


def test_a_replay_over_a_written_tape_reads_the_decimal_strings(tmp_path):
    """The reader's records and a hand-built one agree, level for level."""
    path = tmp_path / "l2" / "l2_NVDA_ONDO-ASTER_20260914T000000Z.jsonl"
    with TapeWriter(path, run_id=RUN_ID, symbol="NVDA") as writer:
        ondo = BookTape(writer, symbol="NVDA", venue="ONDO",
                        instrument_id="NVDA-USD-PERP.ONDO", coverage_limit=100)
        ondo.replace([["100.00", "1"]], [["100.10", "1"]], coverage_limit=100,
                     source="snapshot", ts_event_ns=1_000 * MS, ts_init_ns=1_001 * MS,
                     recorded_mono_ns=10 * MS)
    from market_tape import read_tape

    records = [r for r in read_tape([path]) if r["event_kind"] == "book"]
    step = list(replay_events(records))[0]
    book = step.books["ONDO"]
    assert book.bids == ((Decimal("100.00"), Decimal("1")),)
    assert book.asks == ((Decimal("100.10"), Decimal("1")),)
    assert book.coverage_limit == 100
    assert book.receive_mono_ns == 10 * MS


# ======================================================= the analysis and the CLI


def push(leg, *, bids, asks, at_ms, receive_ms=None, init_ms=None, valid=None, reason=None,
         source="snapshot"):
    """One leg's complete book at ``at_ms``, received at ``receive_ms``.

    ``init_ms`` is the record's own local epoch receipt stamp and defaults to one
    millisecond after its venue event (what a real tape shows). A test that asserts an
    exact event age - the age is measured from this stamp - passes it explicitly.
    """
    return leg.replace(
        bids, asks, coverage_limit=100, source=source,
        ts_event_ns=int(at_ms * MS),
        ts_init_ns=int((init_ms if init_ms is not None else at_ms) * MS) + (
            0 if init_ms is not None else MS
        ),
        recorded_mono_ns=int(receive_ms * MS) if receive_ms is not None else int(at_ms * MS),
        valid=valid, invalid_reason=reason,
    )


def push_quote(writer, *, venue, symbol="NVDA", at_ms, receive_ms=None, init_ms=None,
               bid="100.00", ask="100.10", bid_size="1", ask_size="1"):
    """A top-of-book update: it must never refresh the leg's depth time."""
    return writer.write_event(quote_event(
        symbol=symbol, venue=venue, instrument_id=f"{symbol}-USD-PERP.{venue}",
        bid=bid, ask=ask, bid_size=bid_size, ask_size=ask_size, source="quotes",
        ts_event_ns=int(at_ms * MS),
        ts_init_ns=int((init_ms if init_ms is not None else at_ms) * MS) + (
            0 if init_ms is not None else MS
        ),
        recorded_mono_ns=int(receive_ms * MS) if receive_ms is not None else int(at_ms * MS),
    ))


def write_run_dir(tmp_path, body, *, symbol="NVDA", venues=("ONDO", "ASTER"), fees=None,
                  fee_source="instrument_metadata", size_precision=3, stamp=RUN_ID,
                  manifest=True, rotate_bytes=None, run_dir=None, size_increments=None,
                  metadata_extra=None, instruments=True):
    """A recorded run directory written by the real writer, in plan 5.1's layout.

    ``size_increments`` is the *real* per-venue ``size_increment`` the tape's instrument
    records carry, and it defaults to the value ``size_precision`` describes (0.001 for
    the default 3 decimals) - a v2 tape of an instrument whose step matches its
    precision. Pass ``{"ONDO": None}`` for a venue with no increment at all (a legacy
    tape), which the analysis must then call ``quantity_step_unknown``: it never falls
    back to ``10**-size_precision``. ``instruments=False`` writes no instrument record
    at all, for the "this session announced no metadata" case.
    """
    run_dir = Path(run_dir) if run_dir is not None else tmp_path / "run"
    l2 = run_dir / "l2"
    l2.mkdir(parents=True, exist_ok=True)
    path = l2 / f"l2_{symbol}_{'-'.join(venues)}_{stamp}.jsonl"
    if fees is None:
        fees = {"ONDO": "2.5", "ASTER": "0.9"}
    if size_increments is None:
        implied = None if size_precision is None else Decimal(1).scaleb(-size_precision)
        size_increments = {
            venue: (None if implied is None else str(implied)) for venue in venues
        }
    extra = {} if rotate_bytes is None else {"rotate_bytes": int(rotate_bytes)}
    with TapeWriter(path, run_id=RUN_ID, symbol=symbol,
                    legs=[{"venue_key": v, "venue": v} for v in venues], **extra) as writer:
        legs = {}
        for venue in venues:
            instrument = f"{symbol}-USD-PERP.{venue}"
            legs[venue] = BookTape(writer, symbol=symbol, venue=venue,
                                   instrument_id=instrument, coverage_limit=100)
            if instruments:
                payload = {"venue": venue, "client_id": venue,
                           "taker_fee_bps": fees.get(venue), "fee_source": fee_source,
                           "price_precision": 2, "size_precision": size_precision,
                           "tick_size": "0.01"}
                increment = size_increments.get(venue)
                if increment is not None:
                    payload["size_increment"] = increment
                payload.update(metadata_extra or {})
                writer.write_event(instrument_event(
                    symbol=symbol, venue=venue, instrument_id=instrument,
                    metadata=payload, coverage_limit=100))
        body(writer, legs)
    if manifest:
        write_run_manifest(l2)
    return run_dir


def params(*, symbols=("NVDA",), notionals=(Decimal("100"),), venues=("ONDO", "ASTER"),
           max_age_ms=2_000, max_skew_ms=500, steps=None, mapping_verified=(),
           max_hits=200, max_event_age_ms=None):
    return Params(
        symbols=tuple(symbols), venues=tuple(venues), notionals=tuple(notionals),
        quality=QualityParams(max_age_ms=max_age_ms, max_skew_ms=max_skew_ms,
                              max_event_age_ms=max_event_age_ms),
        steps=dict(steps or {}), mapping_verified=frozenset(mapping_verified),
        max_hits=max_hits,
    )


def market(report, symbol):
    return next(m for m in report.document["markets"] if m["symbol"] == symbol)


def direction(report, symbol, sell, buy):
    return next(
        d for d in market(report, symbol)["directions"]
        if d["sell_venue"] == sell and d["buy_venue"] == buy
    )


def notional_agg(report, symbol, sell, buy, notional=Decimal("100")):
    return next(
        n for n in direction(report, symbol, sell, buy)["notionals"]
        if Decimal(n["notional_usd"]) == Decimal(notional)
    )


def first_row(report, symbol, sell, buy, notional=Decimal("100")):
    """The first *passing* row of one bucket: what a cost-qualified comparison saw."""
    examples = notional_agg(report, symbol, sell, buy, notional)["examples"]
    assert examples, "this bucket has no passing row to look at"
    return examples[0]


def summary_header(header):
    return [name.strip() for name in header]


# -------------------------------------------------------- one quantity for both


def test_the_two_legs_share_one_base_quantity_never_two_independent_scans(tmp_path):
    """$100 is a *chooser*: Q comes from the sell leg's bid, and ASTER is filled
    for that same Q - not for its own separate $100."""
    def body(writer, legs):
        push(legs["ONDO"], bids=[["100.00", "1000"]], asks=[["100.10", "1000"]],
             at_ms=1_000)
        push(legs["ASTER"], bids=[["200.00", "1000"]],
             asks=[["200.10", "0.5"], ["210.00", "100"]], at_ms=1_000)

    report = analyse_run(write_run_dir(tmp_path, body), params())
    row = first_row(report, "NVDA", "ONDO", "ASTER")
    assert Decimal(row["quantity"]) == Decimal("1.000"), (
        "one base quantity, from the ONDO bid of 100.00 and the 3-decimal step"
    )
    assert Decimal(row["common_step"]) == Decimal("0.001")
    assert Decimal(row["sell_vwap"]) == Decimal("100.00")
    # 1 unit crosses into ASTER's second level: a *separate* $100 scan on ASTER
    # would have filled 0.4997... units at 200.10 and hidden this.
    assert Decimal(row["buy_vwap"]) == Decimal("205.05")
    assert Decimal(row["fill_usd_est"]) == Decimal("100.00")
    assert row["reject_reason"] == ""
    assert row["quality_ok"] is True

    # the reverse direction chooses its own quantity from *its* sell leg (ASTER)
    other = first_row(report, "NVDA", "ASTER", "ONDO")
    assert Decimal(other["quantity"]) == Decimal("0.500")
    assert Decimal(other["sell_vwap"]) == Decimal("200.00")
    assert Decimal(other["buy_vwap"]) == Decimal("100.10")


def test_the_quantity_step_is_the_lcm_of_the_two_legs_steps(tmp_path):
    """0.002 and 0.003 on the two legs -> the bucket says 0.006, never 0.003."""
    def body(writer, legs):
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    run_dir = write_run_dir(tmp_path, body)
    # ONDO size precision 3 (0.001) and ASTER precision 3 would both be 0.001; drive the
    # two *different* steps through the explicit override the CLI exposes.
    report = analyse_run(run_dir, params(steps={"ONDO": Decimal("0.002"),
                                                "ASTER": Decimal("0.003")}))
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert Decimal(agg["common_step"]) == Decimal("0.006")
    assert Decimal(agg["common_step"]) != Decimal("0.003")


def test_a_tier_below_one_step_is_reported_not_traded(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["100000", "100"]], asks=[["100010", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100000", "100"]], asks=[["100010", "100"]], at_ms=1_000)
        push(legs["ONDO"], bids=[["100000", "100"]], asks=[["100010", "100"]], at_ms=2_000)

    report = analyse_run(
        write_run_dir(tmp_path, body),
        params(notionals=(Decimal("1"),), steps={"ONDO": Decimal("0.5"),
                                                 "ASTER": Decimal("0.5")}),
    )
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER", Decimal("1"))
    assert agg["pass"] == 0
    assert agg["quality_pass"] == 1, (
        "only one of the three moments is a usable comparison: the moment before "
        "ASTER's first book has no book, and the second two-legged moment fails the "
        "gate first (its legs' own event times are 1000 ms apart)"
    )
    assert agg["reject_reasons"] == {
        REJECT_NO_BOOK: 1,         # the moment before ASTER's first book
        REJECT_BELOW_ONE_STEP: 1,  # the one moment both books were fresh and unskewed
        REJECT_EVENT_SKEW: 1,      # ONDO's 2000 ms book against ASTER's 1000 ms one
    }, (
        "the $1 tier at a bid of 100000 with a 0.5 step floors to zero: it is reported "
        "and never traded. The gate runs before any quantity is chosen (plan 5.2: a "
        "1000 ms leg-to-leg event-time difference over the 500 ms threshold must not "
        "pass), so the second two-legged moment is an event_skew, not a bad tier"
    )
    assert agg["samples"] == sum(agg["reject_reasons"].values()), (
        "every moment is accounted for by exactly one reason"
    )
    assert agg["examples"] == [], "nothing was computed, so there is nothing to show"


# --------------------------------------------------------------------- quality


def test_a_fresh_quote_never_refreshes_a_stale_book_in_the_report(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        # three seconds later only a *quote* arrives on ONDO: its depth is unchanged
        push_quote(writer, venue="ONDO", at_ms=4_000)
        # ... and then both legs publish a real book again
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=4_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=4_000)

    report = analyse_run(write_run_dir(tmp_path, body), params())
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["reject_reasons"].get(REJECT_STALE_BOOK, 0) >= 1, (
        "the ONDO quote at 4000 ms did not refresh the ONDO book received at 1000 ms"
    )
    assert agg["pass"] >= 2, "the two moments where both legs had a fresh book"
    assert agg["quality_pass"] >= 2


def test_a_leg_that_is_behind_on_its_own_event_time_fails_the_skew_rule(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        # ASTER's next book claims an exchange time 600 ms *before* ONDO's, while both
        # were received inside the age window.
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=2_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_400)

    report = analyse_run(write_run_dir(tmp_path, body), params())
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["reject_reasons"].get(REJECT_EVENT_SKEW, 0) == 2, (
        "both later moments have the two legs' own event times 600 ms apart"
    )
    assert agg["pass"] == 1, "only the first two-legged moment is inside the skew rule"
    assert agg["reject_reasons"].get(REJECT_NO_BOOK, 0) == 1
    assert agg["samples"] == agg["pass"] + sum(agg["reject_reasons"].values())


def test_the_report_keeps_zero_opportunities_and_a_missing_leg_honestly(tmp_path):
    def body(writer, legs):
        # a crossed ONDO book at every moment: no comparison can ever pass
        push(legs["ONDO"], bids=[["101.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    report = analyse_run(write_run_dir(tmp_path, body), params())
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["samples"] > 0, "an empty opportunity list is still a result"
    assert agg["pass"] == 0 and agg["hits"] == 0
    assert agg["zero_opportunity"] is True
    assert agg["reject_reasons"].get(REJECT_CROSSED, 0) == agg["samples"]
    assert "NVDA" in report.document["zero_opportunity_markets"]

    # a symbol the tape does not carry keeps its section, with a note and zero samples
    lonely = write_run_dir(
        tmp_path / "lonely",
        lambda writer, legs: push(legs["ONDO"], bids=[["100.00", "100"]],
                                  asks=[["100.10", "100"]], at_ms=1_000),
        venues=("ONDO",),
    )
    report = analyse_run(lonely, params())
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["samples"] == 0
    assert "no complete book" in agg["note"] or "ASTER" in agg["note"]
    assert direction(report, "NVDA", "ONDO", "ASTER")["totals"]["samples"] == 0


def test_a_recording_gap_fails_the_report_and_the_replay_never_crosses_it(tmp_path):
    l2 = tmp_path / "run" / "l2"
    l2.mkdir(parents=True)
    start, end = markers(complete=False)
    # Instrument metadata is an ordinary record, not a lifecycle marker: the writer
    # stamps it an arrival_seq too (a real run numbers its two instruments 1 and 2,
    # before its first book), so a hand-written tape must as well or the reader -
    # rightly - refuses it. The data records therefore start at 3 here.
    instruments = [
        {**start, "arrival_seq": seq, "event_kind": "instrument",
         "venue": venue, "instrument_id": f"NVDA-USD-PERP.{venue}",
         "source": "instrument_metadata", "coverage_limit": 100,
         "metadata": {"taker_fee_bps": "1.5", "fee_source": "instrument_metadata",
                      "size_precision": 3}}
        for seq, venue in enumerate(("ONDO", "ASTER"), start=1)
    ]
    lines = [
        start,
        *instruments,
        rec(3, event_ns=1_000 * MS, init_ns=1_000 * MS + 1, mono_ns=1_000 * MS,
            bids=[["100.00", "1"]], asks=[["100.10", "1"]]),
        rec(4, venue="ASTER", event_ns=1_000 * MS, init_ns=1_000 * MS + 1,
            mono_ns=1_000 * MS, bids=[["100.00", "1"]], asks=[["100.10", "1"]]),
        {**start, "arrival_seq": None, "event_kind": "gap", "valid": False,
         "invalid_reason": "recording_gap:queue_full", "dropped": 3, "missing_from": 5,
         "missing_to": 7, "recorded_mono_ns": 2_000 * MS, "venue": None},
        rec(8, event_ns=3_000 * MS, init_ns=3_000 * MS + 1, mono_ns=3_000 * MS,
            bids=[["100.00", "1"]], asks=[["100.10", "1"]]),
        end,
    ]
    write_lines(l2 / f"l2_NVDA_ONDO-ASTER_{RUN_ID}.jsonl", lines)

    report = analyse_run(tmp_path / "run", params())
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["reject_reasons"].get(REJECT_RECORDING_GAP, 0) >= 1, (
        "the gap marker is an evaluation moment, so the hole is named in the report"
    )
    assert agg["reject_reasons"].get(REJECT_NO_BOOK, 0) >= 1, (
        "after the hole every leg has to publish a fresh complete book again"
    )
    tape = report.document["tapes"][0]
    assert tape["complete"] is False
    assert tape["gaps"] == 1 and tape["dropped"] == 3
    assert report.document["executable"] is False


# ------------------------------------------------------------------------ fees


def test_the_run_fee_snapshot_is_the_source_never_todays_registry(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    # The tape says ONDO charges 9.9 bps: the static registry would say 2.5 / 0.9.
    report = analyse_run(
        write_run_dir(tmp_path, body, fees={"ONDO": "9.9", "ASTER": "0.9"}),
        params(),
    )
    assert Decimal(first_row(report, "NVDA", "ONDO", "ASTER")["entry_fees_bps"]) == (
        Decimal("10.8")
    )
    snapshot = report.document["fee_snapshot"]["NVDA"]["ONDO"]
    assert snapshot["taker_fee_bps"] == "9.9"
    assert snapshot["fee_source"] == "instrument_metadata"


def test_a_fee_missing_from_the_run_withholds_the_cost_qualification(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    report = analyse_run(
        write_run_dir(tmp_path, body, fees={"ONDO": None, "ASTER": "0.9"},
                      fee_source="missing"),
        params(),
    )
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["entry_fees_bps"] == "unknown"
    assert agg["reject_reasons"].get(REJECT_FEE_UNKNOWN, 0) == agg["samples"] - 1, (
        "every moment with both legs' books is withheld; the first has no book yet"
    )
    assert agg["quality_pass"] == agg["samples"] - 1, (
        "the gate passed every moment it could evaluate; only the cost qualification "
        "is withheld"
    )
    text = json.dumps(report.document)
    assert "2.5" not in text, (
        "the dated documentation assumption must never be substituted for a missing fee"
    )
    assert report.document["fee_snapshot"]["NVDA"]["ONDO"]["taker_fee_bps"] == "unknown"


def test_mapping_unverified_still_emits_the_nominal_comparison_but_never_executable(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    report = analyse_run(write_run_dir(tmp_path, body), params())
    row = first_row(report, "NVDA", "ONDO", "ASTER")
    assert row["mapping_verified"] is False
    assert row["mapping_status"] == MAPPING_UNVERIFIED
    assert row["executable"] is False
    assert Decimal(row["gross_entry_bps"]) > 0, "the nominal comparison is still published"
    assert report.document["hits"], "a positive nominal spread is still recorded"

    report = analyse_run(
        write_run_dir(tmp_path / "verified", body),
        params(mapping_verified=(("ONDO", "ASTER"),)),
    )
    row = first_row(report, "NVDA", "ONDO", "ASTER")
    assert row["mapping_verified"] is True
    assert row["executable"] is False, "a nominal comparison is never an order promise"
    assert first_row(report, "NVDA", "ASTER", "ONDO")["mapping_verified"] is False


# ------------------------------------------------------------------- funding


def test_the_funding_estimate_uses_the_run_tape_and_the_repository_units(tmp_path):
    def body(writer, legs):
        for venue, rate in (("ONDO", "0.0000063"), ("ASTER", "0.0001")):
            writer.write_event(funding_event(
                symbol="NVDA", venue=venue,
                instrument_id=f"NVDA-USD-PERP.{venue}", rate=rate, interval_secs=3_600,
                ts_event_ns=1_000 * MS, ts_init_ns=1_000 * MS + 1,
                recorded_mono_ns=1_000 * MS,
            ))
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    report = analyse_run(write_run_dir(tmp_path, body), params())
    row = first_row(report, "NVDA", "ONDO", "ASTER")
    assert row["funding_status"] == "observed"
    ours = Decimal(row["funding_estimate_bps"])
    # the sell leg's hourly carry minus the buy leg's - in the repository's own units
    expected = Decimal(str(
        hourly_bps(0.0000063, "ONDO", "NVDA") - hourly_bps(0.0001, "ASTER", "NVDA")
    ))
    assert abs(ours - expected) < Decimal("0.000001")
    assert ours == Decimal("0.063") - Decimal("1") / Decimal("8") * Decimal("1")


def test_a_tape_with_no_funding_record_says_missing_not_zero(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    report = analyse_run(write_run_dir(tmp_path, body), params())
    row = first_row(report, "NVDA", "ONDO", "ASTER")
    assert row["funding_estimate_bps"] == "unknown"
    assert row["funding_status"].startswith("missing")
    assert row["exit_status"] == "unclosed", "no exit data: the round trip stays unverified"


# ------------------------------------------------------- determinism and shape


def test_two_runs_over_the_same_tape_differ_only_in_the_generation_timestamp(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=2_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=2_000)

    run_dir = write_run_dir(tmp_path, body, fees={"ONDO": None, "ASTER": "0.9"},
                            fee_source="missing")
    first = analyse_run(run_dir, params())
    second = analyse_run(run_dir, params())
    left, right = (json.loads(json.dumps(r.document)) for r in (first, second))
    left.pop("generated_utc")
    right.pop("generated_utc")
    assert left == right, "two runs over one tape differ only in the generation stamp"

    out_a, out_b = tmp_path / "a", tmp_path / "b"
    main(["--dir", str(run_dir), "--out", str(out_a)])
    main(["--dir", str(run_dir), "--out", str(out_b)])
    assert (out_a / "ondo_depth_summary.csv").read_bytes() == (
        out_b / "ondo_depth_summary.csv"
    ).read_bytes()
    assert (out_a / "ondo_depth_hits.csv").read_bytes() == (
        out_b / "ondo_depth_hits.csv"
    ).read_bytes()
    # the unknown / unclosed markers survive both runs
    document = json.loads((out_a / "ondo_depth.json").read_text(encoding="utf-8"))
    agg = document["markets"][0]["directions"][0]["notionals"][0]
    assert agg["entry_fees_bps"] == "unknown"
    assert agg["examples"], "a withheld fee still shows the numbers it computed"
    assert all(
        row["entry_fees_bps"] == "unknown"
        and row["funding_estimate_bps"] == "unknown"
        and row["exit_status"] == "unclosed"
        and row["executable"] is False
        for row in agg["examples"]
    )
    assert document["hits"] == [], "nothing cost-qualified passed, and that is reported"


def test_the_fragments_are_read_in_manifest_order_and_a_glob_fallback_works(tmp_path):
    def body(writer, legs):
        for index in range(12):
            push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]],
                 at_ms=1_000 + index)
            push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]],
                 at_ms=1_000 + index)

    run_dir = write_run_dir(tmp_path, body, rotate_bytes=900)
    report = analyse_run(run_dir, params())
    tape = report.document["tapes"][0]
    assert tape["fragments"] > 1, "the rotation actually produced several fragments"
    assert tape["manifest"] is not None
    assert tape["first_arrival_seq"] == 1
    assert tape["records"] > len(tape["sessions"])
    assert tape["complete"] is True

    # without the manifests the same fragments are found by name (rotation order)
    for path in (run_dir / "l2").glob("*.manifest.json"):
        path.unlink()
    (run_dir / "l2" / "manifest.json").unlink()
    fallback = analyse_run(run_dir, params())
    assert fallback.document["tapes"][0]["records"] == tape["records"]
    assert fallback.document["tapes"][0]["fragments"] == tape["fragments"]
    assert fallback.document["tapes"][0]["last_arrival_seq"] == tape["last_arrival_seq"]
    assert fallback.document["tapes"][0]["manifest"] is None


def test_the_report_uses_the_plans_field_names_and_never_renames_net_bps(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    run_dir = write_run_dir(tmp_path, body)
    out = tmp_path / "out"
    assert main(["--dir", str(run_dir), "--out", str(out)]) == 0
    document = json.loads((out / "ondo_depth.json").read_text(encoding="utf-8"))
    row = document["hits"][0]
    for name in ("gross_entry_bps", "entry_fees_bps", "entry_after_fees_bps",
                 "exit_fee_assumption_bps", "reserve_bps", "funding_estimate_bps",
                 "quality_ok", "mapping_verified", "reject_reason", "executable"):
        assert name in row, name
        assert name in document["field_notes"], name
    assert row["executable"] is False
    assert Decimal(row["reserve_bps"]) == Decimal(str(RESERVE_BPS))
    assert Decimal(row["exit_fee_assumption_bps"]) == Decimal(row["entry_fees_bps"])
    assert Decimal(row["entry_after_fees_bps"]) == (
        Decimal(row["gross_entry_bps"]) - Decimal(row["entry_fees_bps"])
    )
    with open(out / "ondo_depth_hits.csv", newline="", encoding="utf-8") as handle:
        header = summary_header(next(csv.reader(handle)))
    assert set(row) <= set(header) | {"direction"}
    assert "net_bps" not in header
    assert "net_bps" not in document["field_notes"]
    summary = (out / "ondo_depth_summary.csv").read_text(encoding="utf-8")
    assert "net_bps" not in summary


def test_the_analysis_never_reaches_for_the_static_fee_registry(tmp_path):
    source = Path(ondo_depth.__file__).read_text(encoding="utf-8")
    assert "venue_fees" not in source, (
        "a historical file must use the run's own fee snapshot, never a recomputation "
        "from today's registry (plan 5.2)"
    )
    assert "INSTRUMENTS" not in source


def test_the_default_comparison_is_nvda_tsla_across_ondo_aster_in_both_directions():
    assert DEFAULT_SYMBOLS == ("NVDA", "TSLA")
    assert DEFAULT_VENUES == ("ONDO", "ASTER")
    parser = build_parser()
    args = parser.parse_args(["--dir", "."])
    assert args.symbols == "NVDA,TSLA"
    assert args.venues == "ONDO,ASTER"
    assert args.notionals == "100,500,1000"
    assert args.max_age_ms == 2_000 and args.max_skew_ms == 500


def test_the_acceptance_command_shape_runs_and_writes_every_output(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    run_dir = write_run_dir(tmp_path, body)
    out = tmp_path / "depth-analysis"
    code = main([
        "--dir", str(run_dir), "--symbols", "NVDA,TSLA", "--notionals", "100,500,1000",
        "--max-age-ms", "2000", "--max-skew-ms", "500", "--out", str(out),
    ])
    assert code == 0
    for name in ("ondo_depth.json", "ondo_depth_summary.csv", "ondo_depth_hits.csv",
                 "ondo_depth.md"):
        assert (out / name).is_file(), name
    document = json.loads((out / "ondo_depth.json").read_text(encoding="utf-8"))
    assert document["research_parameters"]["max_age_ms"] == 2_000
    assert document["research_parameters"]["max_skew_ms"] == 500
    assert document["research_parameters"]["future_tolerance_ms"] == 1_000
    assert document["research_parameters"]["notionals_usd"] == ["100", "500", "1000"]
    assert document["research_parameters"]["directions"] == ["ONDO>ASTER", "ASTER>ONDO"]
    assert document["research_parameters"]["reserve_bps"] == str(RESERVE_BPS)
    assert document["research_parameters"]["funding_basis"] == "bps_per_hour"
    assert document["research_parameters"]["exit_status"] == "unclosed"
    md = (out / "ondo_depth.md").read_text(encoding="utf-8")
    assert "## NVDA" in md and "## TSLA" in md, "a zero-opportunity market keeps its section"
    assert "ONDO>ASTER" in md and "ASTER>ONDO" in md
    assert "reject" in md.lower()


def test_a_run_dir_without_a_tape_is_refused_not_reported_as_empty(tmp_path, capsys):
    empty = tmp_path / "empty"
    empty.mkdir()
    assert main(["--dir", str(empty), "--out", str(tmp_path / "out")]) == 2
    assert "no tape fragment" in capsys.readouterr().err
    with pytest.raises(DepthError):
        analyse_run(empty, params())


# ----------------------------- event age vs receive age (plan 5.2: 时钟偏差未知)
#
# Plan 5.2: "接收新鲜也不证明交易所事件新鲜；报告同时给出 event age 和时钟偏差未知标记，不将本地
# clock offset 混成网络延迟结论." The gate's own thresholds are untouched by this section;
# it still decides on the receive age, the future offsets and the skew - the event age
# is published beside them so a reader can see a book that is fresh locally and old on
# the venue's own clock.

OLD_EVENT_MS = 1_000  # the venue's own event time: two minutes before the moment below
NOW_MS = 121_000      # the record being evaluated (a quote that arrives 1 ms later)


def frozen_venue_tape(tmp_path, *, run_dir=None):
    """Both books old on the *venue's* clock, both received 1 ms before the moment.

    ONDO 110.00/110.10 against ASTER 100.00/100.10 is the same positive spread the
    other run tests use, so the only unusual thing about this tape is time: each
    book's own ``ts_event_ns`` is two minutes behind the record being evaluated,
    while its ``recorded_mono_ns`` is one millisecond behind it.

    The local epoch receipt stamps are passed explicitly (``init_ms``) rather than
    left at the helper's "venue event + 1 ms" default, so the numbers the plan's
    regression names - event 1000 ms, local receipt 121000 ms, event age 120000 ms -
    are the numbers this tape actually carries. The age is measured from that local
    receipt stamp (F09), so the two must agree exactly for the assertion to mean what
    it says.
    """
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]],
             at_ms=OLD_EVENT_MS, receive_ms=NOW_MS - 1, init_ms=OLD_EVENT_MS)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]],
             at_ms=OLD_EVENT_MS, receive_ms=NOW_MS - 1, init_ms=OLD_EVENT_MS)
        push_quote(writer, venue="ONDO", at_ms=NOW_MS, receive_ms=NOW_MS, init_ms=NOW_MS)

    return write_run_dir(tmp_path, body, run_dir=run_dir)


def test_an_old_venue_event_with_a_fresh_receipt_keeps_its_row_and_shows_the_event_age(tmp_path):
    """The row the gate produced before is the same row - with the staleness visible."""
    report = analyse_run(frozen_venue_tape(tmp_path), params())
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")

    # the gate's decision is exactly what it was before the field existed: three
    # moments, the first one before ASTER's book (no_book), the other two usable
    assert agg["samples"] == 3
    assert agg["quality_pass"] == 2
    assert agg["pass"] == 2
    assert agg["reject_reasons"] == {REJECT_NO_BOOK: 1}
    assert agg["hits"] == 2

    fresh, stale_event = agg["examples"]
    # the moment ONDO's own book is evaluated: the event age of that leg is 0, so a
    # small event age is not the field's default - it is measured per record
    assert Decimal(fresh["event_age_sell_ms"]) == Decimal("0")
    assert Decimal(fresh["receive_age_sell_ms"]) == Decimal("0")

    # the quote moment: received one millisecond ago, but its venue event is old
    assert stale_event["arrival_seq"] > fresh["arrival_seq"]
    assert stale_event["quality_ok"] is True and stale_event["reject_reason"] == ""
    assert Decimal(stale_event["receive_age_sell_ms"]) == Decimal("1")
    assert Decimal(stale_event["receive_age_buy_ms"]) == Decimal("1")
    assert Decimal(stale_event["event_age_sell_ms"]) == Decimal("120000")
    assert Decimal(stale_event["event_age_buy_ms"]) == Decimal("120000")
    assert stale_event["clock_offset_unknown"] is True
    assert Decimal(stale_event["event_age_sell_ms"]) != Decimal(
        stale_event["receive_age_sell_ms"],
    ), "the venue event age is never the receive age"

    # the same two minutes are on the stored hit rows (this run asked for the $100
    # tier only, so one hit per direction carries them)
    aged = [row for row in report.document["hits"] if row["event_age_sell_ms"] == "120000"]
    assert len(aged) == 1, "the ONDO>ASTER hit for the $100 tier, carrying the event age"
    assert all(
        row["event_age_buy_ms"] == "120000" and row["clock_offset_unknown"] is True
        and row["quality_ok"] is True
        for row in aged
    )
    # the bucket remembers the worst event age it saw even though its example sample is
    # bounded: the field cannot be hidden by three fresh rows arriving first
    assert Decimal(agg["event_age_sell_ms_max"]) == Decimal("120000")
    assert Decimal(agg["event_age_buy_ms_max"]) == Decimal("120000")


def test_the_event_age_and_the_clock_offset_marker_reach_the_json_the_csv_and_the_markdown(tmp_path):
    run_dir = frozen_venue_tape(tmp_path)
    out = tmp_path / "out"
    assert main(["--dir", str(run_dir), "--out", str(out)]) == 0

    document = json.loads((out / "ondo_depth.json").read_text(encoding="utf-8"))
    assert document["clock_offset_unknown"] is True
    parameters = document["research_parameters"]
    assert parameters["clock_offset_unknown"] is True
    assert "NOT measured" in parameters["clock_offset_unknown_note"]
    assert "ts_event_ns" in parameters["event_age_ms_rule"]
    assert any("fresh venue event" in note for note in document["notes"])
    for name in ("event_age_sell_ms", "event_age_buy_ms", "clock_offset_unknown"):
        assert name in document["field_notes"], name
        assert name in ondo_depth.ROW_FIELDS, name
    assert "unknown" in document["field_notes"]["event_age_sell_ms"], (
        "the field note says a missing stamp is 'unknown'"
    )
    assert "network latency" in document["field_notes"]["clock_offset_unknown"]
    json_rows = [row for row in document["hits"] if row["event_age_sell_ms"] == "120000"]
    assert json_rows, "the old-but-freshly-received book is published in the JSON"
    assert all(
        row["receive_age_sell_ms"] == "1" and row["event_age_buy_ms"] == "120000"
        and row["clock_offset_unknown"] is True
        for row in json_rows
    )

    with open(out / "ondo_depth_hits.csv", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or ())
        csv_rows = list(reader)
    assert {"event_age_sell_ms", "event_age_buy_ms", "clock_offset_unknown"} <= header
    csv_aged = [row for row in csv_rows if row["event_age_sell_ms"] == "120000"]
    assert len(csv_aged) == len(json_rows)
    assert all(
        row["receive_age_sell_ms"] == "1" and row["clock_offset_unknown"] == "true"
        for row in csv_aged
    )

    summary_header = (out / "ondo_depth_summary.csv").read_text(
        encoding="utf-8",
    ).splitlines()[0].split(",")
    assert {"event_age_sell_ms_max", "event_age_buy_ms_max"} <= set(summary_header)
    with open(out / "ondo_depth_summary.csv", newline="", encoding="utf-8") as handle:
        summary_rows = list(csv.DictReader(handle))
    assert summary_rows and summary_rows[0]["event_age_sell_ms_max"] == "120000"

    markdown = (out / "ondo_depth.md").read_text(encoding="utf-8")
    assert "## Event age vs receive age" in markdown
    assert "`event_age_sell_ms`" in markdown and "`event_age_buy_ms`" in markdown
    assert "`clock_offset_unknown`" in markdown
    assert "never network latency" in markdown
    assert "max event age sell/buy ms" in markdown, "the bucket tables carry the maximum"
    assert "120000/120000" in markdown, "the numbers themselves, not only the field names"


def test_every_emitted_row_carries_the_event_age_and_the_marker(tmp_path):
    report = analyse_run(frozen_venue_tape(tmp_path), params())
    emitted = [
        row
        for market_entry in report.document["markets"]
        for direction_entry in market_entry["directions"]
        for entry in direction_entry["notionals"]
        for row in entry["examples"]
    ] + list(report.document["hits"])
    assert emitted
    for row in emitted:
        for name in ("event_age_sell_ms", "event_age_buy_ms", "clock_offset_unknown"):
            assert name in row, name
        assert row["event_age_sell_ms"] in {"unknown"} or Decimal(
            row["event_age_sell_ms"],
        ) >= 0
        assert row["clock_offset_unknown"] is True
    # the bounded median sample is untouched by the new field
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["median_sample"] == "all"
    assert ondo_depth.SAMPLE_KEEP == 20_000


def test_an_unusable_event_time_is_unknown_and_never_a_fabricated_zero(tmp_path):
    """No usable venue event stamp -> 'unknown'; the gate's reason does not change."""
    other = LegReading("ASTER", bookstate("ASTER", event_ns=NOW_MS * MS,
                                          init_ns=NOW_MS * MS, mono_ns=NOW_MS * MS))
    record = TimePoint(label="quote", event_ns=NOW_MS * MS, init_ns=NOW_MS * MS,
                       receive_mono_ns=NOW_MS * MS, session_id=SESSION)
    no_event = LegReading("ONDO", bookstate("ONDO", event_ns=None, init_ns=None,
                                           mono_ns=NOW_MS * MS))
    verdict = quality_gate(record, [no_event, other])
    assert verdict.reject_reason == REJECT_UNKNOWN_TIME, "the reason it always gave"
    assert verdict.event_age_ms["ONDO"] is None
    assert verdict.event_age_ms["ASTER"] == Decimal("0")

    # A record with no *venue event* stamp of its own still measures both legs' ages:
    # the age is the record's LOCAL receipt time minus the book's venue event time
    # (F09), so the record's own exchange stamp is not part of it. Both legs' books
    # carry the same event stamp as the record's receipt here, hence 0 - and 0 is a
    # real measurement of "the venue event is now", not the old two-venue difference
    # that read 0 for a two-minute-old pair.
    no_record_event = TimePoint(label="book", event_ns=None, init_ns=NOW_MS * MS,
                                receive_mono_ns=NOW_MS * MS, session_id=SESSION)
    verdict = quality_gate(no_record_event, [
        LegReading("ONDO", bookstate("ONDO", event_ns=NOW_MS * MS, init_ns=NOW_MS * MS,
                                     mono_ns=NOW_MS * MS)),
        other,
    ])
    assert verdict.event_age_ms == {"ONDO": Decimal("0"), "ASTER": Decimal("0")}
    assert verdict.reject_reason == REJECT_UNKNOWN_TIME

    # What does make the age unknown is a missing *local receipt* stamp on the record
    # being evaluated (or on the book's event): there is then no local epoch time to
    # measure from, and 'unknown' is reported instead of a fabricated 0.
    no_receipt = TimePoint(label="book", event_ns=NOW_MS * MS, init_ns=None,
                           receive_mono_ns=NOW_MS * MS, session_id=SESSION)
    verdict = quality_gate(no_receipt, [
        LegReading("ONDO", bookstate("ONDO", event_ns=NOW_MS * MS, init_ns=NOW_MS * MS,
                                     mono_ns=NOW_MS * MS)),
        other,
    ])
    assert verdict.event_age_ms == {"ONDO": None, "ASTER": None}
    assert event_age_status(QualityParams(max_event_age_ms=1_000), verdict.event_age_ms) == (
        "unknown"
    )

    # and the row serialization turns a missing event age into 'unknown', never 0
    blank = {f.name: None for f in dataclasses.fields(ondo_depth.Row)}
    row = ondo_depth.Row(**{**blank, "event_age_sell_ms": None, "event_age_buy_ms": None})
    assert row.as_dict()["event_age_sell_ms"] == "unknown"
    assert row.as_dict()["event_age_buy_ms"] == "unknown"
    assert row.as_dict()["event_age_sell_ms"] != "0"
    assert row.as_dict()["clock_offset_unknown"] is True


def test_the_cli_refuses_a_corrupt_tape_and_says_so(tmp_path, capsys):
    l2 = tmp_path / "run" / "l2"
    l2.mkdir(parents=True)
    path = l2 / f"l2_NVDA_ONDO-ASTER_{RUN_ID}.jsonl"
    path.write_text(
        json.dumps(rec(1, event_ns=1_000 * MS, init_ns=1_000 * MS, mono_ns=1_000 * MS,
                       bids=[["100.00", "1"]], asks=[["100.10", "1"]])) + "\n"
        + "{not json at all\n"
        + json.dumps(rec(3, event_ns=1_000 * MS, init_ns=1_000 * MS, mono_ns=1_000 * MS,
                         bids=[["100.00", "1"]], asks=[["100.10", "1"]])) + "\n",
        encoding="utf-8",
    )
    code = main(["--dir", str(tmp_path / "run"), "--out", str(tmp_path / "out")])
    assert code == 2
    assert "not valid JSON" in capsys.readouterr().err


# =============================== R2.2 — replay priced by then-available information
#
# The plan's numeric regressions, one test each:
#
# | input                                                      | expected              |
# |------------------------------------------------------------|-----------------------|
# | metadata 2.5/0.9 bps -> seq4 book -> seq5 ONDO 100 bps      | seq4 stays 3.4 bps,   |
# |                                                             | later 100.9 bps       |
# | two legs fresh -> ONDO HALT -> quote, then                  | quality rejects; the  |
# | disconnect -> snapshot_ready                                | HALT survives         |
# | real lots 0.002 / 0.003, sell 110.00, $100 tier             | step 0.006, Q 0.906   |
# | event=1000 ms, local epoch init=121000 ms, both legs alike  | age ~120000 ms, not 0 |
# | a new session with no metadata / no increment               | unknown; no reuse of  |
# |                                                             | another session       |
#
# Everything here is offline: hand-written records and the real writer, no network.


def example_rows(report, symbol="NVDA", sell="ONDO", buy="ASTER",
                 notional=Decimal("100")):
    """Every row this bucket actually computed a quantity for, in arrival order."""
    return notional_agg(report, symbol, sell, buy, notional)["examples"]


def instrument_rec(seq: int, venue: str, *, fee: str | None, increment: str | None,
                   session: str = SESSION, source: str = "instrument_metadata",
                   valid: bool = True, invalid: str | None = None,
                   symbol: str = "NVDA", schema: int | None = None) -> dict:
    """One hand-written instrument record, in the newer per-arrival metadata shape."""
    metadata: dict[str, object] = {
        "venue": venue, "client_id": venue, "taker_fee_bps": fee,
        "fee_source": "instrument_metadata" if fee is not None else "missing",
        "price_precision": 2, "size_precision": 3, "tick_size": "0.01",
    }
    if increment is not None:
        metadata["size_increment"] = increment
    return rec(seq, kind="instrument", venue=venue, symbol=symbol, session=session,
               schema=tape_schema() if schema is None else schema,
               metadata=metadata, source=source,
               instrument_id=f"{symbol}-USD-PERP.{venue}", coverage_limit=100,
               valid=valid, invalid=invalid)


def book_rec(seq: int, venue: str, *, at_ms: int, receive_ms: int | None = None,
             session: str = SESSION, symbol: str = "NVDA",
             bid: str = "100.00", ask: str = "100.10", init_ms: int | None = None) -> dict:
    """One hand-written complete book: the record *is* the book's own arrival."""
    return rec(seq, venue=venue, symbol=symbol, session=session, schema=tape_schema(),
               event_ns=int(at_ms * MS),
               init_ns=int((init_ms if init_ms is not None else at_ms) * MS),
               mono_ns=int((receive_ms if receive_ms is not None else at_ms) * MS),
               bids=[[bid, "100"]], asks=[[ask, "100"]], coverage_limit=100,
               source="snapshot")


def mark(session: str, kind: str, **extra) -> dict:
    """A run_start / run_end marker, stamped with the current tape schema."""
    start, end = markers(session=session)
    record = {**(start if kind == "run_start" else end), "schema_version": tape_schema()}
    record.update(extra)
    return record


def two_fragment_tape(l2: Path, *, first: list[dict], second: list[dict]) -> None:
    """One tape written as two fragments, the second appended later (a rotation)."""
    l2.mkdir(parents=True, exist_ok=True)
    write_lines(l2 / f"l2_NVDA_ONDO-ASTER_{RUN_ID}.jsonl", first)
    write_lines(l2 / f"l2_NVDA_ONDO-ASTER_{RUN_ID}_part0002.jsonl", second)


# ------------------------------------------------- F06: metadata applies by arrival


def test_a_metadata_update_prices_only_the_arrivals_after_it(tmp_path):
    """The plan's F06 regression: seq4 is 3.4 bps and never becomes 100.9 bps."""
    def body(writer, legs):
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        # the venue's ONDO taker fee changes to 100 bps from this arrival on
        writer.write_event(instrument_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            metadata={"venue": "ONDO", "client_id": "ONDO", "taker_fee_bps": "100",
                      "fee_source": "instrument_metadata", "price_precision": 2,
                      "size_precision": 3, "tick_size": "0.01",
                      "size_increment": "0.001"},
            coverage_limit=100, source="instrument_update"))
        push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=3_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=3_000)

    report = analyse_run(
        write_run_dir(tmp_path, body),
        params(max_age_ms=500_000, max_skew_ms=500_000),
    )
    rows = example_rows(report)
    assert [(row["arrival_seq"], row["entry_fees_bps"]) for row in rows] == [
        (4, "3.4"), (6, "100.9"), (7, "100.9"),
    ], (
        "the two legs' fees are 2.5 + 0.9 = 3.4 before the update and 100 + 0.9 = 100.9 "
        "after it: the update reaches the arrivals after it, never the ones before"
    )
    # the numbers the early row was priced with are the early ones, all the way through
    early = rows[0]
    assert Decimal(early["entry_after_fees_bps"]) == (
        Decimal(early["gross_entry_bps"]) - Decimal("3.4")
    )
    assert Decimal(early["exit_fee_assumption_bps"]) == Decimal("3.4")
    assert rows[1]["quality_ok"] is True and rows[1]["cost_qualified"] is True

    # the bucket refuses to elect one of the two fees, and names both instead
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["entry_fees_bps"] == "unknown"
    assert "3.4, 100.9" in agg["entry_fees_note"]
    snapshot = report.document["fee_snapshot"]["NVDA"]["ONDO"]
    assert snapshot["taker_fee_bps_values"] == ["2.5", "100"]
    assert [arrival["source"] for arrival in snapshot["arrivals"]] == [
        "instrument_metadata", "instrument_update",
    ]
    assert snapshot["metadata_records"] == 2


def test_appending_a_later_fragment_never_reprices_the_prefix_already_reported(tmp_path):
    """F06's second half: the prefix of one tape is invariant under a later file."""
    l2 = tmp_path / "run" / "l2"
    prefix = [
        mark(SESSION, "run_start"),
        instrument_rec(1, "ONDO", fee="2.5", increment="0.001"),
        instrument_rec(2, "ASTER", fee="0.9", increment="0.001"),
        book_rec(3, "ONDO", at_ms=1_000),
        book_rec(4, "ASTER", at_ms=1_000),
    ]
    two_fragment_tape(l2, first=prefix, second=[])
    before = analyse_run(tmp_path / "run", params(max_age_ms=500_000, max_skew_ms=500_000))
    assert [(row["arrival_seq"], row["entry_fees_bps"]) for row in example_rows(before)] == [
        (4, "3.4"),
    ]

    # the recording continues: the fee changes and two more books arrive
    two_fragment_tape(l2, first=prefix, second=[
        instrument_rec(5, "ONDO", fee="100", increment="0.001",
                       source="instrument_update"),
        book_rec(6, "ONDO", at_ms=3_000),
        book_rec(7, "ASTER", at_ms=3_000),
        mark(SESSION, "run_end"),
    ])
    after = analyse_run(tmp_path / "run", params(max_age_ms=500_000, max_skew_ms=500_000))
    rows_after = [(row["arrival_seq"], row["entry_fees_bps"]) for row in example_rows(after)]
    assert rows_after == [(4, "3.4"), (6, "100.9"), (7, "100.9")]
    assert rows_after[:1] == [
        (row["arrival_seq"], row["entry_fees_bps"]) for row in example_rows(before)
    ], "the appended file changed no number the earlier report had already published"

    # and the whole earlier row is what it was, not only its fee
    old_row, new_row = example_rows(before)[0], example_rows(after)[0]
    assert json.dumps(old_row, sort_keys=True) == json.dumps(new_row, sort_keys=True)


def test_an_instrument_record_marked_stale_withdraws_the_metadata_from_then_on(tmp_path):
    """Contract B: valid=false + metadata_stale means 'do not use this from here'."""
    l2 = tmp_path / "run" / "l2"
    two_fragment_tape(l2, first=[
        mark(SESSION, "run_start"),
        instrument_rec(1, "ONDO", fee="2.5", increment="0.001"),
        instrument_rec(2, "ASTER", fee="0.9", increment="0.001"),
        book_rec(3, "ONDO", at_ms=1_000),
        book_rec(4, "ASTER", at_ms=1_000),
        # the venue's metadata can no longer be trusted; the record repeats the old
        # numbers, and none of them may be used from this arrival on
        instrument_rec(5, "ONDO", fee="2.5", increment="0.001",
                       source="instrument_update", valid=False, invalid="metadata_stale"),
        book_rec(6, "ONDO", at_ms=3_000),
        book_rec(7, "ASTER", at_ms=3_000),
        mark(SESSION, "run_end"),
    ], second=[])

    report = analyse_run(tmp_path / "run", params(max_age_ms=500_000, max_skew_ms=500_000))
    rows = example_rows(report)
    assert [(row["arrival_seq"], row["entry_fees_bps"]) for row in rows] == [(4, "3.4")], (
        "only the arrival before the stale record computed numbers: the stale record "
        "withdraws ONDO's metadata for everything after it"
    )
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["reject_reasons"].get(REJECT_METADATA_STALE, 0) == 2, (
        "the two moments after the stale record are refused on the metadata axis"
    )
    snapshot = report.document["fee_snapshot"]["NVDA"]["ONDO"]
    assert snapshot["metadata_records"] == 2 and snapshot["metadata_stale_records"] == 1
    assert snapshot["taker_fee_bps"] == "2.5", (
        "the record-level view still lists the value the tape announced, while no row "
        "after the stale record was priced with it"
    )


# ------------------------- a session announces its own metadata (and only its own)


def two_session_tape(l2: Path, *, first_metadata: bool, second_metadata: bool) -> None:
    """One tape, two sessions; each may or may not announce instrument metadata."""
    first = [mark(SESSION, "run_start")]
    if first_metadata:
        first += [instrument_rec(1, "ONDO", fee="2.5", increment="0.001"),
                  instrument_rec(2, "ASTER", fee="0.9", increment="0.001")]
        seq = 3
    else:
        seq = 1
    first += [book_rec(seq, "ONDO", at_ms=1_000), book_rec(seq + 1, "ASTER", at_ms=1_000),
              mark(SESSION, "run_end")]

    second = [mark(SESSION_B, "run_start")]
    if second_metadata:
        second += [instrument_rec(1, "ONDO", fee="2.5", increment="0.001",
                                  session=SESSION_B),
                   instrument_rec(2, "ASTER", fee="0.9", increment="0.001",
                                  session=SESSION_B)]
        seq = 3
    else:
        seq = 1
    second += [book_rec(seq, "ONDO", at_ms=3_000, session=SESSION_B),
               book_rec(seq + 1, "ASTER", at_ms=3_000, session=SESSION_B),
               mark(SESSION_B, "run_end")]
    two_fragment_tape(l2, first=first, second=second)


def test_a_session_that_announces_no_metadata_is_unknown_for_its_whole_span(tmp_path):
    l2 = tmp_path / "run" / "l2"
    two_session_tape(l2, first_metadata=True, second_metadata=False)
    report = analyse_run(tmp_path / "run", params(max_age_ms=500_000, max_skew_ms=500_000))
    rows = example_rows(report)
    assert [row["session_id"] for row in rows] == [SESSION], (
        "the second session published no instrument record, so none of its moments "
        "produced a priced row"
    )
    assert Decimal(rows[0]["entry_fees_bps"]) == Decimal("3.4")
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["reject_reasons"].get(REJECT_METADATA_UNKNOWN, 0) >= 1
    assert all(
        row["session_id"] == SESSION
        for row in agg["examples"] + report.document["hits"]
    ), "no row of the metadata-less session was priced with the other session's fee"


def test_a_later_session_never_reprices_an_earlier_one(tmp_path):
    """The other direction: the first session has no metadata, the second one does."""
    l2 = tmp_path / "run" / "l2"
    two_session_tape(l2, first_metadata=False, second_metadata=True)
    report = analyse_run(tmp_path / "run", params(max_age_ms=500_000, max_skew_ms=500_000))
    rows = example_rows(report)
    assert [row["session_id"] for row in rows] == [SESSION_B], (
        "only the session that announced its own instrument metadata is priced"
    )
    assert Decimal(rows[0]["entry_fees_bps"]) == Decimal("3.4")
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["reject_reasons"].get(REJECT_METADATA_UNKNOWN, 0) >= 1, (
        "the first session's moments stay metadata_unknown: the second session's fee "
        "is not priced backwards into them"
    )


# ----------------------------------------------------- F08: the real quantity step


def test_the_common_step_is_the_lcm_of_the_real_size_increments(tmp_path):
    """The plan's regression: lots 0.002 / 0.003 at a 110.00 bid -> 0.006 and 0.906."""
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)

    report = analyse_run(
        write_run_dir(tmp_path, body,
                      size_increments={"ONDO": "0.002", "ASTER": "0.003"}),
        params(),
    )
    row = first_row(report, "NVDA", "ONDO", "ASTER")
    assert Decimal(row["common_step"]) == Decimal("0.006"), (
        "0.006 is the smallest quantity both a 0.002 and a 0.003 increment can "
        "represent exactly; 0.003 (the maximum) cannot"
    )
    assert Decimal(row["quantity"]) == Decimal("0.906"), (
        "100 / 110.00 floored to a whole 0.006 step"
    )
    assert row["step_origin"] == STEP_ORIGIN_METADATA
    snapshot = report.document["fee_snapshot"]["NVDA"]
    assert snapshot["ONDO"]["size_increment"] == "0.002"
    assert snapshot["ASTER"]["size_increment"] == "0.003"
    assert snapshot["ONDO"]["step_origin"] == STEP_ORIGIN_METADATA
    assert Decimal(row["sell_vwap"]) == Decimal("110.00")


def test_a_tape_with_no_increment_leaves_the_quantity_step_unknown(tmp_path):
    """F08a: precision-only metadata never becomes a step, and the row says so."""
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)

    report = analyse_run(
        write_run_dir(tmp_path, body, size_increments={"ONDO": None, "ASTER": None}),
        params(),
    )
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["reject_reasons"].get(REJECT_STEP_UNKNOWN, 0) == 1, (
        "the one moment that passed the gate is refused for an unknown step: "
        "10**-size_precision (0.001 here) is never used as a quantity step"
    )
    assert agg["common_step"] == "unknown"
    assert agg["step_origin"] == STEP_ORIGIN_UNKNOWN
    assert "quantity_step_unknown" in agg["common_step_note"]
    assert agg["examples"] == [], "no quantity was computed, so nothing is shown as one"
    snapshot = report.document["fee_snapshot"]["NVDA"]["ONDO"]
    assert snapshot["size_increment"] == "unknown"
    assert snapshot["step"] == "unknown" and snapshot["step_origin"] == STEP_ORIGIN_UNKNOWN
    assert snapshot["size_precision"] == 3, (
        "the published precision is still reported as evidence; it is just never a step"
    )
    assert agg["entry_fees_bps"] == "3.4", (
        "the fee is a separate axis from the step: it is still known and published"
    )


def test_a_legacy_tape_without_increments_reads_and_downgrades_honestly(tmp_path):
    """A version-1 tape (no per-arrival metadata fields) still reads, as unknown."""
    l2 = tmp_path / "run" / "l2"
    l2.mkdir(parents=True)
    write_lines(l2 / f"l2_NVDA_ONDO-ASTER_{RUN_ID}.jsonl", [
        mark(SESSION, "run_start", schema_version=1),
        instrument_rec(1, "ONDO", fee="2.5", increment=None, schema=1),
        instrument_rec(2, "ASTER", fee="0.9", increment=None, schema=1),
        book_rec(3, "ONDO", at_ms=1_000),
        book_rec(4, "ASTER", at_ms=1_000),
        mark(SESSION, "run_end", schema_version=1),
    ])
    assert TAPE_SCHEMA_VERSIONS[0] == 1, "the reader states the versions it accepts"
    report = analyse_run(tmp_path / "run", params())
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert Decimal(agg["entry_fees_bps"]) == Decimal("3.4"), (
        "a v1 tape's metadata block still prices its arrivals: the fee is there"
    )
    assert agg["reject_reasons"].get(REJECT_STEP_UNKNOWN, 0) == 1
    assert agg["step_origin"] == STEP_ORIGIN_UNKNOWN


def test_a_cli_step_override_is_published_as_an_override_not_as_a_venue_fact(tmp_path):
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)

    # the tape carries a real increment (0.001); the caller overrides it with 0.006
    report = analyse_run(
        write_run_dir(tmp_path, body),
        params(steps={"ONDO": Decimal("0.006"), "ASTER": Decimal("0.006")}),
    )
    row = first_row(report, "NVDA", "ONDO", "ASTER")
    assert Decimal(row["common_step"]) == Decimal("0.006")
    assert row["step_origin"] == STEP_ORIGIN_OVERRIDE
    assert Decimal(row["quantity"]) == Decimal("0.906"), "100 / 110 floored to 0.006"
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["step_origin"] == STEP_ORIGIN_OVERRIDE
    assert "never evidence that either venue verified" in agg["common_step_note"]
    snapshot = report.document["fee_snapshot"]["NVDA"]["ONDO"]
    assert snapshot["step_origin"] == STEP_ORIGIN_OVERRIDE
    assert snapshot["size_increment"] == "0.001", (
        "the venue's own increment stays reported as evidence; the override is what "
        "sized the row, and it is never presented as a verified venue step"
    )


# ------------------------------------------- F07/F18: three axes, and the first frame


def test_a_real_halt_blocks_the_judgement_and_only_a_resume_lifts_it():
    """The gate's market axis, on its own, with no report or tape involved."""
    record = TimePoint(label="quote", event_ns=10_000 * MS, init_ns=10_000 * MS,
                       receive_mono_ns=10_000 * MS, session_id=SESSION)
    book = bookstate("ONDO", event_ns=10_000 * MS, init_ns=10_000 * MS, mono_ns=10_000 * MS)
    other = bookstate("ASTER", event_ns=10_000 * MS, init_ns=10_000 * MS, mono_ns=10_000 * MS)

    halted = LegReading("ONDO", book, market_halted=True)
    verdict = quality_gate(record, [halted, LegReading("ASTER", other)])
    assert verdict.reject_reason == REJECT_MARKET_HALTED, (
        "a halt is its own reason, never confused with a disconnect or a bad book"
    )
    assert verdict.reject_reason != REJECT_DISCONNECTED
    assert quality_gate(record, [
        LegReading("ONDO", book), LegReading("ASTER", other),
    ]).quality_ok


def test_the_replay_separates_the_feed_the_market_and_the_metadata_axes():
    """The three axes are independent state, and each has its own reason (F07)."""
    status = lambda seq, **extra: rec(  # noqa: E731 - one local record builder
        seq, kind="status", venue="ONDO", mono_ns=seq * MS, **extra)
    steps = list(replay_events([
        status(1, action="HALT", reason="", is_trading=False, is_quoting=False),
        status(2, action="HALT", reason="adapter:disconnected", is_trading=False),
        status(3, action="HALT", reason="adapter:snapshot_ready", is_trading=False),
        status(4, action="TRADING", reason="", is_trading=True),
    ]))
    assert steps[0].market_halted["ONDO"] is True
    assert steps[0].feed_ready == {}, "a halt says nothing about the local feed"
    assert steps[1].disconnected["ONDO"] is True
    assert steps[1].market_halted["ONDO"] is True, "a disconnect never lifts a halt"
    assert steps[2].disconnected["ONDO"] is False
    assert steps[2].market_halted["ONDO"] is True, (
        "snapshot_ready re-arms the feed and leaves the market halt exactly where it "
        "was: a reconnect is not evidence that the venue resumed trading"
    )
    assert steps[3].market_halted["ONDO"] is False, "an explicit resume lifts it"

    # the action set the replay mirrors is the watcher's own, name for name
    import spread_watch

    assert NON_TRADING_ACTIONS == frozenset(
        str(action.name) for action in spread_watch.NON_TRADING_ACTIONS
    )


def test_a_halt_survives_a_disconnect_and_the_recovery_snapshot(tmp_path):
    """F07 + F18 in one tape: HALT -> disconnect -> one frame -> snapshot_ready."""
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="", is_trading=False, is_quoting=False,
            ts_event_ns=1_500 * MS, ts_init_ns=1_500 * MS + 1,
            recorded_mono_ns=1_500 * MS))
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="adapter:disconnected", is_trading=None,
            ts_event_ns=2_000 * MS, ts_init_ns=2_000 * MS + 1,
            recorded_mono_ns=2_000 * MS))
        # ONE recovery frame per leg, then the adapter says the feed is ready again
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=3_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=3_000)
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="adapter:snapshot_ready", is_trading=None,
            ts_event_ns=3_000 * MS, ts_init_ns=3_000 * MS + 1,
            recorded_mono_ns=3_000 * MS))
        push_quote(writer, venue="ONDO", at_ms=4_000, receive_ms=4_000)

    report = analyse_run(
        write_run_dir(tmp_path, body), params(max_age_ms=500_000, max_skew_ms=500_000),
    )
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["reject_reasons"] == {
        REJECT_NO_BOOK: 1,       # before ASTER's first book
        REJECT_DISCONNECTED: 2,  # the recovery frames, while the feed was still down
        REJECT_MARKET_HALTED: 1,  # the quote after snapshot_ready: the halt survived it
    }, (
        "each moment is refused on the axis that actually blocked it: the feed while it "
        "was down, the market halt even after the feed was re-armed"
    )
    assert agg["pass"] == 1, "the moment before the halt is still a usable comparison"


def test_the_first_recovery_frame_is_usable_once_the_feed_is_ready(tmp_path):
    """F18: no second depth frame is needed, and the frame keeps its own time."""
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000,
             init_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000,
             init_ms=1_000)
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="adapter:disconnected", is_trading=None,
            ts_event_ns=2_000 * MS, ts_init_ns=2_000 * MS + 1,
            recorded_mono_ns=2_000 * MS))
        # exactly one new depth frame per leg - the recovery snapshot
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=3_000,
             init_ms=3_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=3_000,
             init_ms=3_000)
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="adapter:snapshot_ready", is_trading=None,
            ts_event_ns=3_000 * MS, ts_init_ns=3_000 * MS + 1,
            recorded_mono_ns=3_000 * MS))
        push_quote(writer, venue="ONDO", at_ms=4_000, receive_ms=4_000, init_ms=4_000)

    report = analyse_run(
        write_run_dir(tmp_path, body), params(max_age_ms=500_000, max_skew_ms=500_000),
    )
    rows = example_rows(report)
    assert [(row["arrival_seq"], row["receive_age_sell_ms"], row["event_age_sell_ms"])
            for row in rows] == [
        (4, "0", "0"),        # the first frame's own arrival: fresh, it just arrived
        (9, "1000", "1000"),  # a quote 1 s later does not refresh the frame's time
    ], (
        "the single recovery frame is usable as soon as the feed is ready, and its own "
        "receipt and venue times are what the ages are measured from - the later quote "
        "refreshes neither"
    )
    agg = notional_agg(report, "NVDA", "ONDO", "ASTER")
    assert agg["reject_reasons"].get(REJECT_BOOK_INVALID, 0) == 0, (
        "F18: a recovery frame is not 'invalid' because it landed while the feed was "
        "being re-armed; a book record's validity is about the depth itself"
    )
    assert agg["reject_reasons"].get(REJECT_MARKET_HALTED, 0) == 0, (
        "no market halt was announced here, so the market axis never blocked a row"
    )


# --------------------------------------------- F09: the age is a local-clock quantity


def test_the_event_age_is_measured_from_the_local_receipt_not_between_two_events():
    """The plan's F09 regression: both legs delayed together report 120000 ms, not 0."""
    record = TimePoint(label="quote", event_ns=NOW_MS * MS, init_ns=NOW_MS * MS,
                       receive_mono_ns=NOW_MS * MS, session_id=SESSION)
    legs = [
        LegReading(venue, bookstate(venue, event_ns=OLD_EVENT_MS * MS,
                                    init_ns=OLD_EVENT_MS * MS, mono_ns=NOW_MS * MS))
        for venue in ("ONDO", "ASTER")
    ]
    verdict = quality_gate(record, legs)
    assert verdict.quality_ok, (
        "the receive ages are what the gate decides on, and both receipts are fresh"
    )
    assert verdict.receive_age_ms == {"ONDO": Decimal("0"), "ASTER": Decimal("0")}
    assert verdict.event_age_ms == {
        "ONDO": Decimal("120000"), "ASTER": Decimal("120000"),
    }, (
        "each leg's venue event is two minutes before this record's local receipt, so "
        "each leg reports 120000 ms - the difference between the two venue stamps is "
        "0 and is reported as event_skew_ms, never as the age"
    )
    assert verdict.event_skew_ms == Decimal("0")
    assert verdict.event_age_ms["ONDO"] != verdict.event_skew_ms


def test_a_negative_event_age_is_kept_as_clock_evidence_and_never_zeroed():
    """A venue stamp ahead of the local receipt is evidence, not something to hide."""
    record = TimePoint(label="book", event_ns=NOW_MS * MS, init_ns=NOW_MS * MS,
                       receive_mono_ns=NOW_MS * MS, session_id=SESSION)
    ahead = LegReading("ONDO", bookstate(
        "ONDO", event_ns=(NOW_MS + 500) * MS, init_ns=(NOW_MS + 500) * MS,
        mono_ns=NOW_MS * MS,
    ))
    other = LegReading("ASTER", bookstate("ASTER", event_ns=NOW_MS * MS,
                                          init_ns=NOW_MS * MS, mono_ns=NOW_MS * MS))
    verdict = quality_gate(record, [ahead, other])
    assert verdict.event_age_ms["ONDO"] == Decimal("-500"), (
        "the venue's stamp is 500 ms ahead of the local receipt: the age stays "
        "negative (neither floored to 0 nor made absolute), which is the only visible "
        "evidence that the two clocks disagree by at least that much"
    )
    assert verdict.event_age_ms["ASTER"] == Decimal("0")


def test_the_event_age_threshold_is_an_explicit_parameter_and_an_output_field(tmp_path):
    """F09: the threshold is configuration that is printed, never a silent default."""
    # off by default: the frozen tape's books are two minutes old on the local clock,
    # and the rows are still produced, each carrying the status that says so
    relaxed = analyse_run(frozen_venue_tape(tmp_path / "relaxed", run_dir=(
        tmp_path / "relaxed" / "run")), params())
    relaxed_agg = notional_agg(relaxed, "NVDA", "ONDO", "ASTER")
    assert relaxed_agg["pass"] == 2
    assert relaxed_agg["max_event_age_ms"] is None
    assert relaxed_agg["event_age_rejects"] == 0
    assert {row["event_age_status"] for row in example_rows(relaxed)} == {"not_checked"}
    assert relaxed.document["research_parameters"]["max_event_age_ms"] is None

    # a threshold the ages pass: the rows say the check ran and passed
    wide = analyse_run(frozen_venue_tape(tmp_path / "wide", run_dir=(
        tmp_path / "wide" / "run")), params(max_event_age_ms=200_000))
    assert {row["event_age_status"] for row in example_rows(wide)} == {"ok"}
    assert notional_agg(wide, "NVDA", "ONDO", "ASTER")["event_age_rejects"] == 0

    # a threshold the ages fail: the refusal is its own reason, and the report prints
    # the limit it was decided against
    strict = analyse_run(frozen_venue_tape(tmp_path / "strict", run_dir=(
        tmp_path / "strict" / "run")), params(max_event_age_ms=1_000))
    strict_agg = notional_agg(strict, "NVDA", "ONDO", "ASTER")
    assert strict_agg["pass"] == 1, (
        "the moment the book itself arrived is still fresh - its venue event IS its "
        "own arrival - so exactly one moment survives a 1 s event-age limit"
    )
    assert strict_agg["reject_reasons"].get(REJECT_EVENT_AGE, 0) == 1, (
        "the quote two minutes later is refused: the fresh receipt does not make the "
        "book it re-uses any younger"
    )
    assert strict_agg["event_age_rejects"] == 1
    assert strict_agg["max_event_age_ms"] == 1_000
    assert REJECT_STALE_BOOK not in strict_agg["reject_reasons"], (
        "the receive ages were a millisecond: only the event-age axis rejected this"
    )
    assert {row["event_age_status"] for row in example_rows(strict)} == {"ok"}

    # the CLI parameter and the printed research parameters agree with it
    out = tmp_path / "cli-out"
    assert main(["--dir", str(tmp_path / "strict" / "run"), "--out", str(out),
                 "--max-event-age-ms", "1000"]) == 0
    document = json.loads((out / "ondo_depth.json").read_text(encoding="utf-8"))
    assert document["research_parameters"]["max_event_age_ms"] == 1_000
    assert "unmeasured" in document["research_parameters"]["max_event_age_note"]
    assert "not enforced" not in json.dumps(document)
    bucket = document["markets"][0]["directions"][0]["notionals"][0]
    assert bucket["event_age_rejects"] == 1
    assert bucket["max_event_age_ms"] == 1_000
    assert "event_age_exceeded" in bucket["reject_reasons"]
    assert "event_age_exceeded" in document["field_notes"]["reject_reason"]
    assert "event_age_status" in document["field_notes"]
    assert "event_age_status" in ondo_depth.ROW_FIELDS
    summary = (out / "ondo_depth_summary.csv").read_text(encoding="utf-8")
    assert "max_event_age_ms" in summary.splitlines()[0]


def test_the_new_axis_fields_reach_the_json_the_csv_and_the_markdown(tmp_path):
    """Everything this round added is published, not only computed."""
    def body(writer, legs):
        push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        push(legs["ASTER"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    run_dir = write_run_dir(
        tmp_path, body, size_increments={"ONDO": "0.002", "ASTER": "0.003"},
    )
    out = tmp_path / "out"
    assert main(["--dir", str(run_dir), "--out", str(out)]) == 0
    document = json.loads((out / "ondo_depth.json").read_text(encoding="utf-8"))
    assert document["schema_version"] == ondo_depth.SCHEMA_VERSION
    assert document["research_parameters"]["tape_schema_versions"] == list(
        TAPE_SCHEMA_VERSIONS
    )
    assert document["research_parameters"]["metadata_rule"]
    assert document["research_parameters"]["axes_rule"]
    row = document["hits"][0]
    assert row["step_origin"] == STEP_ORIGIN_METADATA
    assert row["session_id"], "the row names the session it was priced in"
    assert row["event_age_status"] == "not_checked"
    with open(out / "ondo_depth_hits.csv", newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or ())
        csv_rows = list(reader)
    assert {"session_id", "step_origin", "event_age_status"} <= header
    assert all(row["session_id"] for row in csv_rows)
    markdown = (out / "ondo_depth.md").read_text(encoding="utf-8")
    assert "`session_id`" in markdown and "`step_origin`" in markdown
    assert "`event_age_status`" in markdown
    assert "not enforced" in markdown, "the disabled event-age threshold is printed"
    assert "`10**-size_precision` was therefore not used" in markdown
    assert "| NVDA | ONDO |" in markdown and "0.002" in markdown
    assert "instrument_metadata |" in markdown, "the step's origin is printed per venue"


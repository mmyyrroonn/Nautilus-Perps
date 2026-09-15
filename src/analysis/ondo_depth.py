#!/usr/bin/env python3
"""Deterministic replay of the multi-leg L2 tape and the same-quantity VWAP
comparison (plan 5.2, task 5 / P2).

The legacy 2/5/10 bps capacity CSVs cannot rebuild a VWAP and they record one
venue's depth next to another venue's top of book, so the P2 question ("what does
it actually cost to cross *both* books with *one* base quantity?") is answered
from the standardized tape alone (``src/market_tape.py``, plan 5.1) - the fragments
under ``<runDir>/l2``, read in the manifest's order.

Four rules this module exists to keep honest:

1. **Receive order is the only order.** :func:`replay_events` advances the books by
   ``arrival_seq`` and never sorts by ``ts_event_ns``: sorting by exchange time
   would fabricate information that was not visible when the decision was made
   (plan 5.1). A *fresh quote* therefore never refreshes a *stale book*: the depth
   time travels with the book record, not with the quote that followed it.
2. **One base quantity, not two "$100 each" scans.** A nominal tier
   ($100/$500/$1000) only *chooses* one base quantity: it is computed from the
   then-visible bid price of the leg being sold and snapped to the quantity both
   venues can represent exactly (the integer-scaled LCM of the two legs' steps,
   never ``max(step_a, step_b)``). Both legs are then filled for that one quantity.
3. **Freshness is per arrival and per session.** The quality gate measures each
   leg's own book against the local receipt time of the record being evaluated
   (``recorded_mono_ns``), inside one session: two sessions' monotonic clocks are
   not comparable, and the difference of two legs' ``ts_init`` is not "how old
   this is now". A *fresh receipt* still proves nothing about the *venue event*:
   every row carries each leg's own **event age** (on the venues' clock) beside the
   receive ages, and ``clock_offset_unknown`` says plainly that the local clock
   offset - and therefore the network latency - is not measured here.
4. **Fees come from this run's tape.** The snapshot is the tape's own instrument
   records; a fee that is missing there withholds the leg's cost qualification and
   is never replaced by today's static registry (plan 5.2 precedence: account rate
   > live public metadata > a dated documentation assumption).
   ``mapping_verified`` is published next to every row and today no venue pair is
   verified, so ``executable`` is always ``False``: this is research observation,
   never an order promise.

The report names follow plan 5.2 exactly: ``gross_entry_bps``, ``entry_fees_bps``,
``entry_after_fees_bps``, ``exit_fee_assumption_bps``, ``reserve_bps``,
``funding_estimate_bps``, ``quality_ok``, ``mapping_verified``, ``reject_reason``,
``executable=False``. The watcher's legacy ``net_bps`` (entry-only: gross minus the
entry fees minus the reserve) is *not* renamed into anything that looks like a full
round-trip profit; the analogue is published as
``entry_after_fees_and_reserve_bps`` and the round trip stays ``unclosed`` because
P2 has no exit and no realised funding interval.

Usage (plan 8 acceptance shape)::

    python src/analysis/ondo_depth.py --dir <runDir> --symbols NVDA,TSLA \\
        --notionals 100,500,1000 --max-age-ms 2000 --max-skew-ms 500 --out <dir>

Read-only, offline, stdlib only. Every price, quantity, fee and bps value in the
price path is a :class:`decimal.Decimal` - never an f64.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_FLOOR, Decimal
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

# The units are the repository's, not this module's: the funding scale/intervals are
# registered next to the CSVs they were verified against, and the reserve is the one
# the watcher charges at entry. Importing them keeps one source of truth (and a test
# pins the agreement).
from analysis.opportunities import FUNDING_SCALE, funding_hours  # noqa: E402
from market_tape import (  # noqa: E402
    BOOK,
    FUNDING,
    GAP,
    INSTRUMENT,
    JSONL_SUFFIX,
    MANIFEST_NAME,
    QUOTE,
    RUN_END,
    RUN_START,
    STATUS,
    TapeError,
    manifest_tape_paths,
    read_run_manifest,
    read_tape,
)
from spread_watch import L2_DIRNAME, RESERVE_BPS  # noqa: E402

SCHEMA_VERSION = 1
REPORT_STEM = "ondo_depth"

# ---------------------------------------------------------------- parameters

# Plan 5.2's defaults. They are *research parameters*: every report prints them.
DEFAULT_MAX_AGE_MS = 2_000  # both legs' receive age, measured per arrival
DEFAULT_MAX_SKEW_MS = 500  # both legs' own event-time difference
DEFAULT_FUTURE_TOLERANCE_MS = 1_000  # an event claiming to be from the future
DEFAULT_SYMBOLS = ("NVDA", "TSLA")  # the initial default comparison (plan 5.2)
DEFAULT_VENUES = ("ONDO", "ASTER")  # both directions
DEFAULT_NOTIONALS = (Decimal("100"), Decimal("500"), Decimal("1000"))
# The median sample is bounded exactly like spread_watch.GROSS_KEEP: the first N
# rows of the run, so two runs over one tape report the same median.
SAMPLE_KEEP = 20_000
UNKNOWN = "unknown"  # the one honest marker for "not known"
UNCLOSED = "unclosed"  # no exit/funding evidence: never assume the spread reverts
BPS = Decimal(10_000)
NS_PER_MS = Decimal(1_000_000)
_MS = Decimal(1_000_000)

# Reject reasons. One flat vocabulary so a report can count and compare them.
REJECT_RECORDING_GAP = "recording_gap"
REJECT_NO_BOOK = "no_book"
REJECT_EMPTY_BOOK = "empty_book"
REJECT_ONE_SIDED = "one_sided_book"
REJECT_CROSSED = "crossed_book"
REJECT_BOOK_INVALID = "book_invalid"
REJECT_METADATA_UNKNOWN = "metadata_unknown"
REJECT_DISCONNECTED = "disconnected"
REJECT_SESSION_MISMATCH = "session_mismatch"
REJECT_UNKNOWN_TIME = "unknown_time"
REJECT_FUTURE_TIME = "future_event_time"
REJECT_STALE_BOOK = "stale_book"
REJECT_EVENT_SKEW = "event_skew"
REJECT_FEE_UNKNOWN = "fee_unknown"
REJECT_STEP_UNKNOWN = "step_unknown"
REJECT_BELOW_ONE_STEP = "below_one_step"
REJECT_INSUFFICIENT_DEPTH = "insufficient_depth"

BOOK_REASONS_IN_TAPE = frozenset({REJECT_EMPTY_BOOK, REJECT_ONE_SIDED, REJECT_CROSSED})

# Plan 5.2: "接收新鲜也不证明交易所事件新鲜；报告同时给出 event age 和时钟偏差未知标记，
# 不将本地 clock offset 混成网络延迟结论". The two texts below are the report's own words
# for that rule; they are published in the JSON, in the Markdown and (as a field note)
# next to the field names, so a reader can never see a receive age without them.
EVENT_AGE_RULE = (
    "the record's own ts_event_ns minus the leg's own book ts_event_ns, in exact "
    "integer-nanosecond milliseconds: both stamps are the venue's, so a local clock "
    "offset cancels out, and a missing/unusable stamp is 'unknown' (never 0)"
)
CLOCK_OFFSET_NOTE = (
    "a fresh local receipt never proves a fresh venue event: the local clock offset to "
    "either venue is NOT measured (clock_offset_unknown), so receive_age_sell_ms / "
    "receive_age_buy_ms are local RECEIVE ages and are never network latency; read them "
    "beside event_age_sell_ms / event_age_buy_ms, which can be minutes while the receive "
    "age is one millisecond"
)

MAPPING_UNVERIFIED = "mapping_unverified"
MAPPING_VERIFIED = "mapping_verified"
MAPPING_NOTE = (
    "the multiplier / settlement-asset / underlying equivalence of this pair is not "
    "verified anywhere in this repository (plan 5.2), so the row is a nominal "
    "comparison and never an executable conclusion"
)


class DepthError(Exception):
    """The analysis cannot be produced at all (no tape, a missing fragment, ...)."""


# ------------------------------------------------------------------ quantities


@dataclass(frozen=True)
class VwapResult:
    """What a requested base quantity actually fills, in exact decimals.

    ``vwap`` is ``None`` (never the last level's price) when the book cannot fill the
    whole requested quantity; the shortfall is ``requested_qty - filled_qty`` and
    ``insufficient_depth`` says so. A caller must therefore never read a price out of
    a short result.
    """

    requested_qty: Decimal
    filled_qty: Decimal
    vwap: Decimal | None
    insufficient_depth: bool

    @property
    def missing_qty(self) -> Decimal:
        return self.requested_qty - self.filled_qty


def vwap_for_quantity(
    levels: Sequence[tuple[Decimal, Decimal]], quantity: Decimal,
) -> VwapResult:
    """The average price of ``quantity`` base units walked through ``levels``.

    ``levels`` are consumed in the order given - the tape stores a book best-first,
    so the caller passes bids descending / asks ascending and this walks the real
    book rather than re-sorting it. Every step is Decimal arithmetic: the result of
    a full fill is exactly ``notional / quantity``, and it is never extrapolated
    past the last level (a short book returns ``vwap=None`` and the filled amount).
    """
    requested = Decimal(quantity)
    if requested <= 0:
        raise ValueError(f"quantity must be positive, got {quantity!r}")
    remaining = requested
    filled = Decimal(0)
    notional = Decimal(0)
    for level in levels:
        if remaining <= 0:
            break
        price, size = level
        price = Decimal(price)
        size = Decimal(size)
        if size <= 0:
            continue
        take = size if size < remaining else remaining
        notional += price * take
        filled += take
        remaining -= take
    if remaining > 0:
        return VwapResult(requested, filled, None, True)
    return VwapResult(requested, filled, notional / filled, False)


def common_step(steps: Sequence[Decimal]) -> Decimal | None:
    """The smallest quantity both legs can represent exactly: the LCM of the steps.

    Integer-scaled: every step is scaled to an integer by the largest number of
    decimal places, the integers' LCM is taken there, and the result is scaled back.
    For 0.002 and 0.003 that is 0.006 - ``max(0.002, 0.003) = 0.003`` is NOT a common
    unit (0.003 / 0.002 is not an integer), so the maximum is never used.

    ``None`` when a step is missing or not positive: an unknown step means the
    comparison's quantity is not known to be representable on both venues.
    """
    clean: list[Decimal] = []
    for step in steps:
        if step is None:
            return None
        step = Decimal(step)
        if not step.is_finite() or step <= 0:
            return None
        clean.append(step)
    if not clean:
        return None
    places = 0
    for step in clean:
        exponent = step.as_tuple().exponent
        if isinstance(exponent, int):
            places = max(places, -exponent)
    scale = Decimal(1).scaleb(places)
    values = []
    for step in clean:
        scaled = step * scale
        if scaled != scaled.to_integral_value():
            return None
        values.append(int(scaled))
    unit = values[0]
    for value in values[1:]:
        unit = unit * value // math.gcd(unit, value)
    return Decimal(unit).scaleb(-places)


def common_quantity(quantity: Decimal, step: Decimal) -> Decimal:
    """``quantity`` floored to a whole multiple of ``step`` (never rounded up)."""
    step = Decimal(step)
    if step <= 0:
        raise ValueError(f"step must be positive, got {step!r}")
    return (Decimal(quantity) / step).to_integral_value(rounding=ROUND_FLOOR) * step


def target_base_quantity(notional: Decimal, bid_price: Decimal, step: Decimal) -> Decimal:
    """The one base quantity a nominal tier chooses.

    ``notional`` is only a chooser: it is divided by the then-visible bid price of
    the leg being sold and snapped down to ``step``, the quantity both venues can
    represent exactly. Zero means the tier is below one step of the book (the caller
    reports ``below_one_step`` rather than trading a quantity the venue cannot
    express).
    """
    bid_price = Decimal(bid_price)
    if bid_price <= 0:
        raise ValueError(f"bid price must be positive, got {bid_price!r}")
    return common_quantity(Decimal(notional) / bid_price, step)


def venue_step(size_precision: int | None) -> Decimal | None:
    """A venue's quantity step from the size precision its instrument published.

    ``None`` (no published precision) stays ``None``: a step is never invented.
    """
    if size_precision is None or isinstance(size_precision, bool):
        return None
    try:
        precision = int(size_precision)
    except (TypeError, ValueError):
        return None
    if precision < 0 or precision > 30:
        return None
    return Decimal(1).scaleb(-precision)


# ------------------------------------------------------------------------ time


def ts_utc(ns: int | None) -> str:
    """An integer nanosecond timestamp as UTC text, in integer arithmetic only."""
    if ns is None:
        return ""
    seconds, rest = divmod(int(ns), 1_000_000_000)
    stamp = datetime.fromtimestamp(seconds, timezone.utc).replace(
        microsecond=rest // 1_000,
    )
    return stamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _ms_between(later_ns: int, earlier_ns: int) -> Decimal:
    """A nanosecond difference as exact milliseconds - never an f64."""
    return Decimal(int(later_ns) - int(earlier_ns)) / _MS


@dataclass(frozen=True)
class TimePoint:
    """One observation's times exactly as the tape recorded them.

    ``event_ns`` is the venue's own exchange timestamp, ``init_ns`` the local
    receipt timestamp (unix epoch, comparable across machines) and
    ``receive_mono_ns`` the local monotonic receipt stamp (comparable *only* inside
    one session, which is why ``session_id`` travels with it).
    """

    label: str = ""
    event_ns: int | None = None
    init_ns: int | None = None
    receive_mono_ns: int | None = None
    session_id: str | None = None

    @classmethod
    def of_record(cls, record: Mapping[str, Any], *, label: str = "") -> TimePoint:
        def _int(key: str) -> int | None:
            value = record.get(key)
            if isinstance(value, bool) or not isinstance(value, int):
                return None
            return int(value)

        return cls(
            label=label or str(record.get("event_kind") or ""),
            event_ns=_int("ts_event_ns"),
            init_ns=_int("ts_init_ns"),
            receive_mono_ns=_int("recorded_mono_ns"),
            session_id=record.get("session_id") if isinstance(
                record.get("session_id"), str) else None,
        )


# ---------------------------------------------------------------------- replay


@dataclass(frozen=True)
class BookState:
    """One leg's complete L2 as of the record that last carried it."""

    venue: str
    symbol: str | None
    instrument_id: str | None
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    ts_event_ns: int | None
    ts_init_ns: int | None
    receive_mono_ns: int | None
    session_id: str | None
    arrival_seq: int
    coverage_limit: int | None
    source: str
    valid: bool
    invalid_reason: str | None

    @property
    def best_bid(self) -> Decimal | None:
        return self.bids[0][0] if self.bids else None

    @property
    def best_ask(self) -> Decimal | None:
        return self.asks[0][0] if self.asks else None


@dataclass(frozen=True)
class QuoteState:
    """The leg's latest top of book. Never a substitute for the depth."""

    venue: str
    bid: Decimal | None
    ask: Decimal | None
    ts_event_ns: int | None
    receive_mono_ns: int | None
    session_id: str | None
    arrival_seq: int


@dataclass(frozen=True)
class FundingState:
    """The venue's latest raw funding rate as delivered (Decimal text)."""

    venue: str
    rate: Decimal
    interval_secs: int | None
    ts_event_ns: int | None
    receive_mono_ns: int | None
    arrival_seq: int


@dataclass(frozen=True)
class GapState:
    """A recording gap: the range it names may never be replayed across."""

    dropped: int
    missing_from: int | None
    missing_to: int | None
    reason: str | None


@dataclass(frozen=True)
class ReplayStep:
    """The book state as of one record, in receive order.

    ``books``/``quotes``/``funding``/``disconnected`` are snapshots: the next step
    gets its own copies, so a consumer cannot be surprised by a later record
    mutating the step it is holding.
    """

    record: Mapping[str, Any]
    kind: str
    arrival_seq: int | None
    session_id: str | None
    books: Mapping[str, BookState]
    quotes: Mapping[str, QuoteState]
    funding: Mapping[str, FundingState]
    disconnected: Mapping[str, bool]
    gap: GapState | None
    gaps_seen: int
    dropped_seen: int


class ReplayError(Exception):
    """The replay cannot continue (a malformed record in the stream)."""


class ReplayOrderError(ReplayError):
    """``arrival_seq`` went backwards: the stream is not in receive order."""


def _decimal_pairs(value: Any) -> tuple[tuple[Decimal, Decimal], ...]:
    out: list[tuple[Decimal, Decimal]] = []
    for item in value or ():
        try:
            price, size = item
        except (TypeError, ValueError) as exc:
            raise ReplayError(f"a book level must be [price, size], got {item!r}") from exc
        out.append((Decimal(str(price)), Decimal(str(size))))
    return tuple(out)


class _ReplayState:
    """The mutable half of :func:`replay_events`."""

    def __init__(self) -> None:
        self.books: dict[str, BookState] = {}
        self.quotes: dict[str, QuoteState] = {}
        self.funding: dict[str, FundingState] = {}
        self.disconnected: dict[str, bool] = {}
        self.last_seq: dict[str, int] = {}
        self.gaps = 0
        self.dropped = 0
        self.last_session: str | None = None

    # ------------------------------------------------------------------ helpers

    def _seq(self, record: Mapping[str, Any]) -> int:
        seq = record.get("arrival_seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            raise ReplayError(
                f"a {record.get('event_kind')!r} record has no integer arrival_seq: the "
                f"tape is replayed in receive order, so an unnumbered record cannot be "
                f"placed in time",
            )
        session = record.get("session_id")
        session = session if isinstance(session, str) else ""
        previous = self.last_seq.get(session)
        if previous is not None and seq <= previous:
            raise ReplayOrderError(
                f"arrival_seq {seq} is not after {previous} in session {session!r}: the "
                f"replay follows receive order and never sorts by exchange time",
            )
        self.last_seq[session] = seq
        return seq

    # ------------------------------------------------------------------- events

    def apply(self, record: Mapping[str, Any]) -> ReplayStep:
        kind = record.get("event_kind")
        if not isinstance(kind, str) or not kind:
            raise ReplayError("a tape record needs a non-empty event_kind")
        session = record.get("session_id")
        session = session if isinstance(session, str) else None
        if session is not None and session != self.last_session:
            self.last_session = session
        seq: int | None = None
        gap: GapState | None = None
        if kind in (RUN_START, RUN_END):
            pass
        elif kind == GAP:
            dropped = record.get("dropped")
            dropped = int(dropped) if isinstance(dropped, int) and not isinstance(
                dropped, bool) else 0
            self.gaps += 1
            self.dropped += dropped
            gap = GapState(
                dropped=dropped,
                missing_from=record.get("missing_from"),
                missing_to=record.get("missing_to"),
                reason=record.get("invalid_reason"),
            )
            # Nothing may be replayed across a hole: the depth recorded before it is
            # not trusted after it, so every leg has to publish a fresh complete book
            # again before its depth is used (plan 5.1).
            self.books.clear()
        else:
            seq = self._seq(record)
            venue = record.get("venue")
            if kind == BOOK and isinstance(venue, str) and venue:
                self.books[venue] = BookState(
                    venue=venue,
                    symbol=record.get("symbol") if isinstance(
                        record.get("symbol"), str) else None,
                    instrument_id=record.get("instrument_id") if isinstance(
                        record.get("instrument_id"), str) else None,
                    bids=_decimal_pairs(record.get("bids")),
                    asks=_decimal_pairs(record.get("asks")),
                    ts_event_ns=record.get("ts_event_ns"),
                    ts_init_ns=record.get("ts_init_ns"),
                    receive_mono_ns=record.get("recorded_mono_ns"),
                    session_id=session,
                    arrival_seq=seq,
                    coverage_limit=record.get("coverage_limit"),
                    source=str(record.get("source") or ""),
                    valid=bool(record.get("valid", True)),
                    invalid_reason=record.get("invalid_reason") if isinstance(
                        record.get("invalid_reason"), str) else None,
                )
            elif kind == QUOTE and isinstance(venue, str) and venue:
                # A quote updates the *quote* time only. The leg's depth keeps its own
                # time: a fresh quote must never refresh a stale book (plan 5.2).
                self.quotes[venue] = QuoteState(
                    venue=venue,
                    bid=Decimal(str(record["bid"])) if record.get("bid") is not None
                    else None,
                    ask=Decimal(str(record["ask"])) if record.get("ask") is not None
                    else None,
                    ts_event_ns=record.get("ts_event_ns"),
                    receive_mono_ns=record.get("recorded_mono_ns"),
                    session_id=session,
                    arrival_seq=seq,
                )
            elif kind == FUNDING and isinstance(venue, str) and venue:
                if record.get("rate") is not None:
                    self.funding[venue] = FundingState(
                        venue=venue,
                        rate=Decimal(str(record["rate"])),
                        interval_secs=record.get("interval_secs") if isinstance(
                            record.get("interval_secs"), int) else None,
                        ts_event_ns=record.get("ts_event_ns"),
                        receive_mono_ns=record.get("recorded_mono_ns"),
                        arrival_seq=seq,
                    )
            elif kind == STATUS and isinstance(venue, str) and venue:
                reason = record.get("reason")
                if reason == "adapter:disconnected":
                    self.disconnected[venue] = True
                elif reason == "adapter:snapshot_ready":
                    self.disconnected[venue] = False
            # Any other kind (instrument metadata, a later stage's record) is carried
            # by the step's ``record`` and changes no book state.
        return ReplayStep(
            record=record,
            kind=kind,
            arrival_seq=seq,
            session_id=session,
            books=dict(self.books),
            quotes=dict(self.quotes),
            funding=dict(self.funding),
            disconnected=dict(self.disconnected),
            gap=gap,
            gaps_seen=self.gaps,
            dropped_seen=self.dropped,
        )


def replay_events(events: Iterable[Mapping[str, Any]]) -> Iterator[ReplayStep]:
    """Replay tape records in receive order, yielding the state after each one.

    ``events`` must already be in ``arrival_seq`` order - which is exactly what
    :func:`market_tape.read_tape` hands over. This function never sorts: it only
    *checks* that the sequence increases within a session, so a stream that was
    reordered anywhere is refused (:class:`ReplayOrderError`) instead of being
    silently repaired into a state that was never visible at the time. Exchange
    timestamps are carried in the state and never used for ordering.
    """
    state = _ReplayState()
    for record in events:
        if not isinstance(record, Mapping):
            raise ReplayError(f"a tape record must be a mapping, got {type(record).__name__}")
        yield state.apply(record)


# ---------------------------------------------------------------- quality gate


@dataclass(frozen=True)
class LegReading:
    """One leg's own state as visible at the record being evaluated."""

    venue: str
    book: BookState | None
    disconnected: bool = False
    metadata_known: bool = True


@dataclass(frozen=True)
class QualityParams:
    """Plan 5.2's thresholds. Research parameters, printed into every report."""

    max_age_ms: int = DEFAULT_MAX_AGE_MS
    max_skew_ms: int = DEFAULT_MAX_SKEW_MS
    future_tolerance_ms: int = DEFAULT_FUTURE_TOLERANCE_MS


@dataclass(frozen=True)
class QualityVerdict:
    """The gate's verdict, with the numbers it was decided on.

    ``event_age_ms`` is *reported*, never decided on: the gate's checks are the
    receive age, the future offsets and the skew, and adding the event ages here
    changed no threshold and no outcome (they belong to the same two legs, so they
    travel with the verdict the row already reads its receive ages from).
    """

    quality_ok: bool
    reject_reason: str | None
    receive_age_ms: Mapping[str, Decimal | None]
    event_skew_ms: Decimal | None
    event_age_ms: Mapping[str, Decimal | None] = field(default_factory=dict)
    notes: tuple[str, ...] = ()


def _age_ms(record: TimePoint, leg: LegReading) -> Decimal | None:
    """A leg's depth age as of the current record's local receipt time.

    ``None`` when the two monotonic stamps are not comparable: they belong to
    different sessions (or one is missing), and a difference across sessions is
    meaningless (plan 5.2: local monotonic only compares inside one run/session).
    """
    book = leg.book
    if book is None or record.receive_mono_ns is None or book.receive_mono_ns is None:
        return None
    if record.session_id != book.session_id:
        return None
    return _ms_between(record.receive_mono_ns, book.receive_mono_ns)


def _event_age_ms(record: TimePoint, leg: LegReading) -> Decimal | None:
    """A leg's *venue* event age: how much older its own book event is than this record.

    Both stamps are the exchange's own (``ts_event_ns``), so this is measured on the
    venues' clock and no local clock offset enters it - which is exactly why it is
    published *beside* the receive age instead of being derived from it: a book whose
    venue event is minutes old while its local receipt is one millisecond old looks
    perfectly fresh from the receive age alone (plan 5.2). Unlike the monotonic
    receive age, exchange stamps are absolute epoch times, so they stay comparable
    across sessions. ``None`` (never ``0``) when either stamp is missing.
    """
    book = leg.book
    if book is None or record.event_ns is None or book.ts_event_ns is None:
        return None
    return _ms_between(record.event_ns, book.ts_event_ns)


def _skew_ms(legs: Sequence[LegReading]) -> Decimal | None:
    """The absolute difference of the legs' *own* exchange event times."""
    stamps = [
        leg.book.ts_event_ns for leg in legs
        if leg.book is not None and leg.book.ts_event_ns is not None
    ]
    if len(stamps) < 2:
        return None
    return _ms_between(max(stamps), min(stamps))


def _book_reason(book: BookState) -> str | None:
    """Why a leg's book cannot be used, from its levels and its own validity flag."""
    if not book.bids and not book.asks:
        return REJECT_EMPTY_BOOK
    if not book.bids or not book.asks:
        return REJECT_ONE_SIDED
    if book.best_bid is not None and book.best_ask is not None and (
        book.best_bid >= book.best_ask
    ):
        return REJECT_CROSSED
    if not book.valid:
        reason = book.invalid_reason
        if reason in BOOK_REASONS_IN_TAPE:
            return reason
        return REJECT_BOOK_INVALID
    return None


def quality_gate(
    record: TimePoint,
    legs: Sequence[LegReading],
    *,
    params: QualityParams = QualityParams(),
    recording_gap: bool = False,
) -> QualityVerdict:
    """Decide whether this moment is a usable comparison (plan 5.2).

    The gate receives the *current record's* time and each leg's *own book* time
    (the depth time, never the quote's), and fails on: a recording gap, a missing
    book, empty/one-sided/crossed levels, an unknown instrument metadata, a
    disconnected feed, ages that are not comparable (two sessions), a missing
    receipt time, an exchange timestamp more than ``future_tolerance_ms`` in the
    future, a leg older than ``max_age_ms`` and two legs whose own event times
    differ by more than ``max_skew_ms``. Checks run in that documented order and the
    first failure is the reported reason.

    Each leg's *event* age is measured here too and returned in the verdict, but it
    is **not** one of those checks: the gate is left exactly as it was (plan 5.2's
    thresholds are the receive age, the future offsets and the skew), and the event
    age exists so a report can show a book that is fresh on the local clock while it
    is old on the venue's own.
    """
    ages: dict[str, Decimal | None] = {leg.venue: _age_ms(record, leg) for leg in legs}
    event_ages: dict[str, Decimal | None] = {
        leg.venue: _event_age_ms(record, leg) for leg in legs
    }
    skew = _skew_ms(legs)

    def verdict(reason: str, *notes: str) -> QualityVerdict:
        return QualityVerdict(False, reason, ages, skew, event_ages, tuple(notes))

    if recording_gap:
        return verdict(
            REJECT_RECORDING_GAP,
            "the tape has a recording gap: nothing may be replayed across it",
        )
    if record.receive_mono_ns is None or record.event_ns is None:
        return verdict(
            REJECT_UNKNOWN_TIME,
            "the record carries no local receipt / event time to measure freshness with",
        )
    if record.init_ns is not None and (
        _ms_between(record.event_ns, record.init_ns) > Decimal(params.future_tolerance_ms)
    ):
        return verdict(
            REJECT_FUTURE_TIME,
            "the record's own exchange timestamp is more than "
            f"{params.future_tolerance_ms} ms ahead of its local receipt time",
        )
    tolerance = Decimal(params.future_tolerance_ms)
    max_age = Decimal(params.max_age_ms)
    for leg in legs:
        book = leg.book
        if book is None:
            return verdict(
                REJECT_NO_BOOK,
                f"{leg.venue}: no complete book has been replayed for this leg",
            )
        if not leg.metadata_known:
            return verdict(
                REJECT_METADATA_UNKNOWN,
                f"{leg.venue}: this run's tape carries no instrument metadata for the leg",
            )
        if leg.disconnected:
            return verdict(
                REJECT_DISCONNECTED,
                f"{leg.venue}: the adapter reported the local feed disconnected",
            )
        reason = _book_reason(book)
        if reason is not None:
            return verdict(
                reason,
                f"{leg.venue}: book is not a two-sided uncrossed depth"
                + (f" ({book.invalid_reason})" if book.invalid_reason else ""),
            )
        if book.session_id != record.session_id:
            return verdict(
                REJECT_SESSION_MISMATCH,
                f"{leg.venue}: the book was received in another session "
                f"({book.session_id!r}) than this record ({record.session_id!r}): two "
                f"sessions' monotonic clock stamps are not comparable",
            )
        if book.receive_mono_ns is None or book.ts_event_ns is None:
            return verdict(
                REJECT_UNKNOWN_TIME,
                f"{leg.venue}: the book record carries no local receipt / event time",
            )
        if book.ts_init_ns is not None and (
            _ms_between(book.ts_event_ns, book.ts_init_ns) > tolerance
        ):
            return verdict(
                REJECT_FUTURE_TIME,
                f"{leg.venue}: the book's exchange timestamp is more than "
                f"{params.future_tolerance_ms} ms ahead of its local receipt time",
            )
        if (
            record.event_ns is not None
            and _ms_between(book.ts_event_ns, record.event_ns) > tolerance
        ):
            return verdict(
                REJECT_FUTURE_TIME,
                f"{leg.venue}: the book's exchange timestamp is more than "
                f"{params.future_tolerance_ms} ms ahead of the record being evaluated",
            )
        age = ages.get(leg.venue)
        if age is not None and age > max_age:
            return verdict(
                REJECT_STALE_BOOK,
                f"{leg.venue}: depth is {age} ms old at this record "
                f"(> {params.max_age_ms} ms)",
            )
    if skew is not None and skew > Decimal(params.max_skew_ms):
        return verdict(
            REJECT_EVENT_SKEW,
            f"the legs' own event times differ by {skew} ms "
            f"(> {params.max_skew_ms} ms)",
        )
    return QualityVerdict(True, None, ages, skew, event_ages, ())


# ------------------------------------------------------------- the run's tape


@dataclass(frozen=True)
class TapeFiles:
    """One tape: every fragment that belongs to one writer, in read order."""

    tape: str
    manifest: Path | None
    fragments: tuple[Path, ...]
    source: str  # "run_manifest" | "glob"


def tape_stem(filename: str) -> str:
    """``l2_NVDA_ONDO-ASTER_20260914T000000Z_part0002.jsonl`` -> its tape name."""
    name = Path(filename).name
    if name.endswith(JSONL_SUFFIX):
        name = name[: -len(JSONL_SUFFIX)]
    head, sep, tail = name.rpartition("_part")
    if sep and tail.isdigit():
        return head
    return name


def resolve_l2_dir(run_dir: Path) -> Path:
    """``<runDir>/l2`` when it exists, else ``<runDir>`` itself (already an l2 dir)."""
    run_dir = Path(run_dir)
    if (run_dir / L2_DIRNAME).is_dir():
        return run_dir / L2_DIRNAME
    if run_dir.is_dir():
        return run_dir
    raise DepthError(f"[ondo-depth] not a directory: {run_dir}")


def discover_tapes(l2_dir: Path) -> list[TapeFiles]:
    """The run's tapes and their fragments, in the manifest's order.

    The run manifest (``<l2>/manifest.json``, plan 5.1) is the authority: its
    ``fragments[]`` list is in read order, so a rotated tape is read
    ``l2.jsonl``, ``l2_part0002.jsonl``, ... and never by an accident of file-system
    enumeration. Without a manifest the plain name sort is the rotation order, which
    is exactly how the writer names fragments.
    """
    l2_dir = Path(l2_dir)
    manifest = l2_dir / MANIFEST_NAME
    grouped: dict[str, list[Path]] = {}
    order: list[str] = []
    source = "glob"
    if manifest.is_file():
        document = read_run_manifest(l2_dir)
        paths = manifest_tape_paths(manifest)
        entries = sorted(
            document.get("fragments") or [], key=lambda entry: int(entry.get("order", 0)),
        )
        for entry, path in zip(entries, paths):
            name = str(entry.get("tape") or tape_stem(Path(path).name))
            if name not in grouped:
                grouped[name] = []
                order.append(name)
            grouped[name].append(Path(path))
        if order:
            source = "run_manifest"
            return [
                TapeFiles(name, manifest, tuple(grouped[name]), source) for name in order
            ]
        grouped, order = {}, []
    for path in sorted(p for p in l2_dir.glob(f"*{JSONL_SUFFIX}") if p.is_file()):
        name = tape_stem(path.name)
        if name not in grouped:
            grouped[name] = []
            order.append(name)
        grouped[name].append(path)
    return [TapeFiles(name, None, tuple(grouped[name]), source) for name in order]


@dataclass(frozen=True)
class TapeScan:
    """What one tape says about itself, read once and reused by the report."""

    tape: str
    files: tuple[str, ...]
    fragments: int
    records: int
    first_arrival_seq: int | None
    last_arrival_seq: int | None
    symbol: str | None
    book_venues: tuple[str, ...]
    metadata: Mapping[str, Mapping[str, Any]]
    instrument_records: int
    sessions: tuple[Mapping[str, Any], ...]
    complete: bool
    gaps: int
    dropped: int
    truncated_tail: bool
    truncated_at: str | None
    run_ids: tuple[str, ...]
    failure_reason: str | None
    manifest: str | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "tape": self.tape,
            "manifest": self.manifest,
            "files": list(self.files),
            "fragments": self.fragments,
            "records": self.records,
            "first_arrival_seq": self.first_arrival_seq,
            "last_arrival_seq": self.last_arrival_seq,
            "symbol": self.symbol,
            "book_venues": list(self.book_venues),
            "instrument_records": self.instrument_records,
            "sessions": [dict(session) for session in self.sessions],
            "complete": self.complete,
            "gaps": self.gaps,
            "dropped": self.dropped,
            "truncated_tail": self.truncated_tail,
            "truncated_at": self.truncated_at,
            "run_ids": list(self.run_ids),
            "failure_reason": self.failure_reason,
        }


def scan_tape(files: TapeFiles) -> TapeScan:
    """Read one tape once for its run-level facts: metadata, venues, verdict."""
    for fragment in files.fragments:
        if not Path(fragment).is_file():
            raise DepthError(
                f"[ondo-depth] the manifest names a fragment that does not exist: "
                f"{fragment}",
            )
    reader = read_tape(list(files.fragments))
    metadata: dict[str, dict[str, Any]] = {}
    book_venues: list[str] = []
    instrument_records = 0
    symbol: str | None = None
    for record in reader:
        if symbol is None and isinstance(record.get("symbol"), str) and record["symbol"]:
            symbol = record["symbol"]
        kind = record.get("event_kind")
        venue = record.get("venue")
        if not isinstance(venue, str) or not venue:
            continue
        if kind == INSTRUMENT:
            instrument_records += 1
            payload = record.get("metadata")
            payload = payload if isinstance(payload, Mapping) else {}
            metadata[venue] = {
                "taker_fee_bps": payload.get("taker_fee_bps"),
                "fee_source": payload.get("fee_source"),
                "price_precision": payload.get("price_precision"),
                "size_precision": payload.get("size_precision"),
                "tick_size": payload.get("tick_size"),
            }
        elif kind in (BOOK, QUOTE) and venue not in book_venues:
            book_venues.append(venue)
    status = reader.status
    sessions = tuple(
        {
            "session_id": session.session_id,
            "run_id": session.run_id,
            "records": session.records,
            "first_arrival_seq": session.first_arrival_seq,
            "last_arrival_seq": session.last_arrival_seq,
            "complete": session.complete,
            "reason": session.reason,
            "dropped": session.dropped,
            "gaps": session.gaps,
        }
        for session in status.sessions
    )
    return TapeScan(
        tape=files.tape,
        files=tuple(Path(path).name for path in files.fragments),
        fragments=len(files.fragments),
        records=status.records,
        first_arrival_seq=min(
            (session.first_arrival_seq for session in status.sessions
             if session.first_arrival_seq is not None), default=None,
        ),
        last_arrival_seq=max(
            (session.last_arrival_seq for session in status.sessions
             if session.last_arrival_seq is not None), default=None,
        ),
        symbol=symbol,
        book_venues=tuple(book_venues),
        metadata=metadata,
        instrument_records=instrument_records,
        sessions=sessions,
        complete=status.complete,
        gaps=status.gaps,
        dropped=status.dropped,
        truncated_tail=status.truncated_tail,
        truncated_at=status.truncated_at,
        run_ids=tuple(status.run_ids),
        failure_reason=status.failure_reason,
        manifest=None if files.manifest is None else str(files.manifest),
    )


# ------------------------------------------------------------------ analysis


@dataclass(frozen=True)
class Params:
    """Everything the analysis was asked for; fixed once per run."""

    symbols: tuple[str, ...] = DEFAULT_SYMBOLS
    venues: tuple[str, str] = DEFAULT_VENUES
    notionals: tuple[Decimal, ...] = DEFAULT_NOTIONALS
    quality: QualityParams = QualityParams()
    steps: Mapping[str, Decimal] = field(default_factory=dict)
    mapping_verified: frozenset[tuple[str, str]] = frozenset()
    max_hits: int = 200
    reserve_bps: Decimal = Decimal(str(RESERVE_BPS))

    @property
    def directions(self) -> tuple[tuple[str, str], ...]:
        first, second = self.venues
        return ((first, second), (second, first))


# The exact field names plan 5.2 fixes, in the order a row is written. Nothing in
# this report renames the watcher's legacy ``net_bps`` into a "full profit": the
# entry-only analogue is ``entry_after_fees_and_reserve_bps`` and the round trip
# stays ``unclosed``.
ROW_FIELDS = (
    "symbol", "direction", "sell_venue", "buy_venue", "notional_usd", "quantity",
    "common_step", "arrival_seq", "ts_utc", "sell_vwap", "buy_vwap", "reference_price",
    "fill_usd_est", "gross_entry_bps", "entry_fees_bps", "entry_after_fees_bps",
    "entry_after_fees_and_reserve_bps", "exit_fee_assumption_bps", "reserve_bps",
    "funding_estimate_bps", "funding_status", "quality_ok", "cost_qualified",
    "mapping_verified", "mapping_status", "reject_reason", "executable",
    "receive_age_sell_ms", "receive_age_buy_ms", "event_skew_ms", "event_age_sell_ms",
    "event_age_buy_ms", "clock_offset_unknown", "exit_status", "note",
)

FIELD_NOTES = {
    "gross_entry_bps": (
        "the entry spread at the VWAP of both legs: (sell_vwap - buy_vwap) / "
        "reference_price * 10000, so it is exactly the watcher's gross_bps but with "
        "each leg walked for the same base quantity"
    ),
    "entry_fees_bps": (
        "the two entry taker fees (one per leg) from THIS run's tape; 'unknown' when "
        "the run published no fee for a leg, in which case the cost-qualified "
        "judgement is withheld rather than falling back to a dated documentation "
        "assumption (a number that is only ever valid for a dry run or for a CSV that "
        "labels itself an assumption, and that this report therefore never repeats)"
    ),
    "entry_after_fees_bps": "gross_entry_bps - entry_fees_bps (entry only)",
    "exit_fee_assumption_bps": (
        "the same two taker fees charged again for the exit, at the same nominal: an "
        "ASSUMPTION because P2 has no exit evidence"
    ),
    "reserve_bps": "the one-leg failure reserve the watcher charges at entry",
    "funding_estimate_bps": (
        "the two legs' latest funding rates, normalised to bps per hour with the "
        "repository's own scale/intervals, sell leg minus buy leg; 'unknown' when the "
        "run carried no funding record for a leg"
    ),
    "quality_ok": (
        "the quality gate's verdict for this moment (freshness, skew, book sanity, "
        "disconnect, metadata, gap) - not a statement about profitability"
    ),
    "mapping_verified": (
        "whether the multiplier / settlement / underlying equivalence of this pair is "
        "verified; False means the row is a nominal comparison only"
    ),
    "reject_reason": (
        "why this row is not a usable comparison: a gate reason, or fee_unknown / "
        "insufficient_depth / below_one_step / step_unknown; empty when it passed"
    ),
    "executable": "always False: this is a research observation, never an order promise",
    "exit_status": (
        "the round trip is 'unclosed' - P2 has no exit book and no realised funding "
        "interval, and the spread is never assumed to revert"
    ),
    "quantity": (
        "the ONE base quantity both legs are filled for: the nominal tier divided by "
        "the sell leg's then-visible best bid and floored to common_step"
    ),
    "common_step": (
        "the integer-scaled LCM of the two legs' quantity steps (never the maximum), so "
        "both venues can represent the quantity exactly"
    ),
    "reference_price": (
        "the two legs' touch mids averaged, the same reference the watcher divides by"
    ),
    "entry_after_fees_and_reserve_bps": (
        "entry_after_fees_bps - reserve_bps: the entry-only analogue of the watcher's "
        "legacy net_bps (which is NOT a full round-trip profit either)"
    ),
    "arrival_seq": "the receive order of the record this row was evaluated at",
    "receive_age_sell_ms": "the sell leg's own depth age at this record (per arrival)",
    "receive_age_buy_ms": "the buy leg's own depth age at this record (per arrival)",
    "event_skew_ms": "the absolute difference of the two legs' own exchange event times",
    "event_age_sell_ms": (
        "the sell leg's own VENUE event age at this record - " + EVENT_AGE_RULE
        + "; carried on every emitted row (the per-bucket examples and the stored hits), "
        "and it is no part of the bounded median sample, which is unchanged"
    ),
    "event_age_buy_ms": (
        "the buy leg's own VENUE event age at this record - " + EVENT_AGE_RULE
        + "; carried on every emitted row (the per-bucket examples and the stored hits), "
        "and it is no part of the bounded median sample, which is unchanged"
    ),
    "clock_offset_unknown": CLOCK_OFFSET_NOTE,
    "event_age_sell_ms_max": (
        "the largest VENUE event age this bucket saw on the sell leg, across every moment "
        "it evaluated (not only the bounded examples and stored hits), 'unknown' when no "
        "moment carried a usable sell-leg event stamp"
    ),
    "event_age_buy_ms_max": (
        "the largest VENUE event age this bucket saw on the buy leg, across every moment "
        "it evaluated (not only the bounded examples and stored hits), 'unknown' when no "
        "moment carried a usable buy-leg event stamp"
    ),
    "funding_status": "'observed' or 'missing:<VENUE>[,<VENUE>]'",
    "cost_qualified": "both legs' taker fees are known for this run",
}


def _dec_text(value: Decimal | int | None) -> str:
    return UNKNOWN if value is None else str(value)


def _fee_bps(value: Any, source: Any) -> Decimal | None:
    """A leg's taker fee from the run's snapshot, or ``None`` (unknown).

    Mirrors the watcher: a leg whose recorded ``fee_source`` is ``missing`` is
    unknown even if a number travelled next to it, and a negative or unparsable rate
    is unknown too. ``None`` withholds the leg's cost qualification.
    """
    if isinstance(source, str) and source == "missing":
        return None
    if value is None:
        return None
    try:
        rate = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not rate.is_finite() or rate < 0:
        return None
    return rate


def _median(values: Sequence[Decimal]) -> Decimal | None:
    if not values:
        return None
    ordered = sorted(values)
    count = len(ordered)
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


@dataclass(frozen=True)
class Row:
    """One comparison: the record it was evaluated at, and what it implies."""

    symbol: str
    sell_venue: str
    buy_venue: str
    notional_usd: Decimal
    arrival_seq: int | None
    ts_utc: str
    quantity: Decimal | None
    common_step: Decimal | None
    sell_vwap: Decimal | None
    buy_vwap: Decimal | None
    reference_price: Decimal | None
    fill_usd_est: Decimal | None
    gross_entry_bps: Decimal | None
    entry_fees_bps: Decimal | None
    entry_after_fees_bps: Decimal | None
    entry_after_fees_and_reserve_bps: Decimal | None
    exit_fee_assumption_bps: Decimal | None
    reserve_bps: Decimal
    funding_estimate_bps: Decimal | None
    funding_status: str
    quality_ok: bool
    cost_qualified: bool
    mapping_verified: bool
    mapping_status: str
    reject_reason: str | None
    receive_age_sell_ms: Decimal | None
    receive_age_buy_ms: Decimal | None
    event_skew_ms: Decimal | None
    event_age_sell_ms: Decimal | None
    event_age_buy_ms: Decimal | None
    note: str

    @property
    def direction(self) -> str:
        return f"{self.sell_venue}>{self.buy_venue}"

    @property
    def executable(self) -> bool:
        """Never: a nominal comparison is not an order promise (plan 5.2)."""
        return False

    @property
    def clock_offset_unknown(self) -> bool:
        """Always: this run measured no local clock offset to either venue.

        A receive age is a local receipt age; presenting it as network latency would
        need a measured offset this analysis never takes (plan 5.2).
        """
        return True

    @property
    def is_hit(self) -> bool:
        return (
            self.reject_reason is None
            and self.entry_after_fees_bps is not None
            and self.entry_after_fees_bps > 0
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "direction": self.direction,
            "sell_venue": self.sell_venue,
            "buy_venue": self.buy_venue,
            "notional_usd": str(self.notional_usd),
            "quantity": _dec_text(self.quantity),
            "common_step": _dec_text(self.common_step),
            "arrival_seq": self.arrival_seq,
            "ts_utc": self.ts_utc,
            "sell_vwap": _dec_text(self.sell_vwap),
            "buy_vwap": _dec_text(self.buy_vwap),
            "reference_price": _dec_text(self.reference_price),
            "fill_usd_est": _dec_text(self.fill_usd_est),
            "gross_entry_bps": _dec_text(self.gross_entry_bps),
            "entry_fees_bps": _dec_text(self.entry_fees_bps),
            "entry_after_fees_bps": _dec_text(self.entry_after_fees_bps),
            "entry_after_fees_and_reserve_bps": _dec_text(
                self.entry_after_fees_and_reserve_bps,
            ),
            "exit_fee_assumption_bps": _dec_text(self.exit_fee_assumption_bps),
            "reserve_bps": str(self.reserve_bps),
            "funding_estimate_bps": _dec_text(self.funding_estimate_bps),
            "funding_status": self.funding_status,
            "quality_ok": self.quality_ok,
            "cost_qualified": self.cost_qualified,
            "mapping_verified": self.mapping_verified,
            "mapping_status": self.mapping_status,
            "reject_reason": self.reject_reason or "",
            "executable": self.executable,
            "receive_age_sell_ms": _dec_text(self.receive_age_sell_ms),
            "receive_age_buy_ms": _dec_text(self.receive_age_buy_ms),
            "event_skew_ms": _dec_text(self.event_skew_ms),
            "event_age_sell_ms": _dec_text(self.event_age_sell_ms),
            "event_age_buy_ms": _dec_text(self.event_age_buy_ms),
            "clock_offset_unknown": self.clock_offset_unknown,
            "exit_status": UNCLOSED,
            "note": self.note,
        }


EXAMPLE_ROWS = 3  # per (market, direction, notional): what a passing row looks like


@dataclass
class _Acc:
    """The per (symbol, direction, notional) accumulator of one run."""

    symbol: str
    sell: str
    buy: str
    notional: Decimal
    step: Decimal | None = None
    note: str | None = None
    samples: int = 0
    quality_pass: int = 0
    passed: int = 0
    hits: int = 0
    executable_hits: int = 0
    reasons: Counter = field(default_factory=Counter)
    gross: list[Decimal] = field(default_factory=list)
    after: list[Decimal] = field(default_factory=list)
    funding: list[Decimal] = field(default_factory=list)
    examples: list[Row] = field(default_factory=list)
    truncated: bool = False
    gross_min: Decimal | None = None
    gross_max: Decimal | None = None
    after_min: Decimal | None = None
    after_max: Decimal | None = None
    funding_min: Decimal | None = None
    funding_max: Decimal | None = None
    # The largest venue event age this bucket saw, on each leg: reported whichever row
    # carried it, so the bounded example/hit samples cannot hide it.
    event_age_sell_max: Decimal | None = None
    event_age_buy_max: Decimal | None = None
    # The bucket's two entry taker fees from the run's snapshot, or None when either
    # leg's fee is unknown (the cost qualification is then withheld).
    fees: Decimal | None = None

    def add(self, row: Row) -> None:
        self.samples += 1
        if row.quality_ok:
            self.quality_pass += 1
        # Every row contributes its own event ages, even the rows that never reach the
        # bounded examples/hits samples: the staleness is a property of the moment.
        self.event_age_sell_max = _max_of(self.event_age_sell_max, row.event_age_sell_ms)
        self.event_age_buy_max = _max_of(self.event_age_buy_max, row.event_age_buy_ms)
        if row.quantity is not None and len(self.examples) < EXAMPLE_ROWS:
            # What a comparison *computed*: a withheld fee or a short book still shows
            # the quantity and the VWAPs it derived, so a reader can see the numbers
            # behind a rejection instead of only its name.
            self.examples.append(row)
        if row.reject_reason is not None:
            self.reasons[row.reject_reason] += 1
            return
        self.passed += 1
        self.gross_min, self.gross_max = _extend(
            self.gross_min, self.gross_max, row.gross_entry_bps,
        )
        self.after_min, self.after_max = _extend(
            self.after_min, self.after_max, row.entry_after_fees_bps,
        )
        self.funding_min, self.funding_max = _extend(
            self.funding_min, self.funding_max, row.funding_estimate_bps,
        )
        for series, value in ((self.gross, row.gross_entry_bps),
                              (self.after, row.entry_after_fees_bps),
                              (self.funding, row.funding_estimate_bps)):
            if value is None:
                continue
            if len(series) < SAMPLE_KEEP:
                series.append(value)
            else:
                self.truncated = True
        if row.is_hit:
            self.hits += 1
            if row.mapping_verified:
                self.executable_hits += 1


def _extend(low: Decimal | None, high: Decimal | None, value: Decimal | None):
    if value is None:
        return low, high
    low = value if low is None or value < low else low
    high = value if high is None or value > high else high
    return low, high


def _max_of(current: Decimal | None, value: Decimal | None) -> Decimal | None:
    """The larger of two optional Decimals - how a bucket remembers its worst event age.

    The example rows are bounded (``EXAMPLE_ROWS``) and the stored hits are bounded
    (``--max-hits``), so a bucket that saw a book minutes old on the venue's clock could
    otherwise report only the fresh rows it happened to keep first.
    """
    if value is None:
        return current
    return value if current is None or value > current else current


class _TapeContext:
    """Everything one tape contributes to the analysis."""

    def __init__(self, symbol: str, scan: TapeScan, params: Params) -> None:
        self.symbol = symbol
        self.scan = scan
        self.params = params
        self.steps: dict[str, tuple[Decimal | None, str | None, int | None]] = {}
        for venue in params.venues:
            self.steps[venue] = _resolve_step(venue, scan, params)
        self.direction_step: dict[tuple[str, str], Decimal | None] = {
            (sell, buy): common_step([self.steps[sell][0], self.steps[buy][0]])
            for sell, buy in params.directions
        }
        self.fees: dict[str, Decimal | None] = {}
        self.metadata_known: dict[str, bool] = {}
        for venue in params.venues:
            payload = scan.metadata.get(venue)
            self.metadata_known[venue] = payload is not None
            self.fees[venue] = None if payload is None else _fee_bps(
                payload.get("taker_fee_bps"), payload.get("fee_source"),
            )
        self.directions: list[tuple[str, str]] = [
            (sell, buy) for sell, buy in params.directions
            if sell in scan.book_venues and buy in scan.book_venues
        ]
        self.skipped: list[tuple[str, str]] = [
            (sell, buy) for sell, buy in params.directions
            if (sell, buy) not in self.directions
        ]


def _resolve_step(
    venue: str, scan: TapeScan, params: Params,
) -> tuple[Decimal | None, str | None, int | None]:
    """A venue's quantity step: the CLI override first, then the run's metadata."""
    override = params.steps.get(venue)
    if override is not None:
        return Decimal(override), "cli", None
    payload = scan.metadata.get(venue) or {}
    precision = payload.get("size_precision")
    step = venue_step(precision if isinstance(precision, int) else None)
    if step is None:
        return None, None, None
    return step, "instrument_metadata", int(precision)


def _funding_estimate(
    step: ReplayStep, symbol: str, sell: str, buy: str,
) -> tuple[Decimal | None, str]:
    """The two legs' funding normalised to bps per hour, sell leg minus buy leg.

    The units and the settlement interval come from the repository's own registry
    (``FUNDING_SCALE`` / ``funding_hours``); nothing here re-derives an Ondo or Aster
    unit. The sign is the carry of the pair as analysed: what the *sell* leg receives
    per hour minus what the *buy* leg pays.
    """
    values: dict[str, Decimal] = {}
    missing: list[str] = []
    for venue in (sell, buy):
        state = step.funding.get(venue)
        if state is None:
            missing.append(venue)
            continue
        scale = Decimal(str(FUNDING_SCALE.get(venue, 1e4)))
        hours = Decimal(str(funding_hours(venue, symbol)))
        if hours <= 0:
            hours = Decimal(1)
        values[venue] = state.rate * scale / hours
    if missing:
        return None, "missing:" + ",".join(missing)
    return values[sell] - values[buy], "observed"


def _evaluate(
    ctx: _TapeContext, step: ReplayStep, sell: str, buy: str, notional: Decimal,
) -> Row:
    """One comparison at one record: the gate first, then one quantity for both legs."""
    params = ctx.params
    record_time = TimePoint.of_record(step.record)
    sell_book = step.books.get(sell)
    buy_book = step.books.get(buy)
    legs = (
        LegReading(sell, sell_book, disconnected=bool(step.disconnected.get(sell)),
                   metadata_known=bool(ctx.metadata_known.get(sell))),
        LegReading(buy, buy_book, disconnected=bool(step.disconnected.get(buy)),
                   metadata_known=bool(ctx.metadata_known.get(buy))),
    )
    verdict = quality_gate(
        record_time, legs, params=params.quality, recording_gap=step.gap is not None,
    )
    reason = verdict.reject_reason
    note = " | ".join(verdict.notes)

    fee_sell = ctx.fees.get(sell)
    fee_buy = ctx.fees.get(buy)
    cost_qualified = fee_sell is not None and fee_buy is not None
    entry_fees = (fee_sell + fee_buy) if cost_qualified else None

    quantity: Decimal | None = None
    sell_vwap: Decimal | None = None
    buy_vwap: Decimal | None = None
    reference: Decimal | None = None
    fill: Decimal | None = None
    gross: Decimal | None = None
    after: Decimal | None = None
    after_reserve: Decimal | None = None
    step_q = ctx.direction_step.get((sell, buy))

    if verdict.quality_ok and sell_book is not None and buy_book is not None:
        if step_q is None:
            reason = REJECT_STEP_UNKNOWN
            note = (
                "no quantity step is known for both legs, so the nominal comparison's "
                "quantity is not known to be representable on both venues"
            )
        else:
            quantity = target_base_quantity(notional, sell_book.best_bid, step_q)
            if quantity <= 0:
                reason = REJECT_BELOW_ONE_STEP
                quantity = None
                note = (
                    f"the {notional} tier is below one {step_q} step at a bid of "
                    f"{sell_book.best_bid}: no representable base quantity"
                )
            else:
                sell_walk = vwap_for_quantity(sell_book.bids, quantity)
                buy_walk = vwap_for_quantity(buy_book.asks, quantity)
                sell_vwap, buy_vwap = sell_walk.vwap, buy_walk.vwap
                if sell_walk.insufficient_depth or buy_walk.insufficient_depth:
                    reason = REJECT_INSUFFICIENT_DEPTH
                    note = (
                        f"{sell} is short {sell_walk.missing_qty} and {buy} is short "
                        f"{buy_walk.missing_qty} of the {quantity} base quantity: no "
                        f"average price is produced from a short book"
                    )
                else:
                    reference = _reference_mid(sell_book, buy_book)
                    if reference is None or reference <= 0:
                        reason = REJECT_NO_BOOK
                        note = "no positive reference price for the pair"
                    else:
                        gross = (sell_walk.vwap - buy_walk.vwap) / reference * BPS
                        fill = quantity * sell_walk.vwap
    if reason is None and not cost_qualified:
        reason = REJECT_FEE_UNKNOWN
        missing_fee = [
            venue for venue, fee in ((sell, fee_sell), (buy, fee_buy)) if fee is None
        ]
        note = (
            "this run's tape published no taker fee for "
            + ", ".join(missing_fee)
            + ": the cost-qualified judgement is withheld (the dated assumption is "
            "never substituted)"
        )
    if cost_qualified and gross is not None:
        after = gross - entry_fees
        after_reserve = after - params.reserve_bps

    funding, funding_status = _funding_estimate(step, ctx.symbol, sell, buy)
    mapping_verified = (sell, buy) in params.mapping_verified
    if not mapping_verified:
        note = (note + " | " if note else "") + MAPPING_NOTE

    return Row(
        symbol=ctx.symbol,
        sell_venue=sell,
        buy_venue=buy,
        notional_usd=Decimal(notional),
        arrival_seq=step.arrival_seq,
        ts_utc=ts_utc(record_time.init_ns),
        quantity=quantity,
        common_step=step_q,
        sell_vwap=sell_vwap,
        buy_vwap=buy_vwap,
        reference_price=reference,
        fill_usd_est=fill,
        gross_entry_bps=gross,
        entry_fees_bps=entry_fees,
        entry_after_fees_bps=after,
        entry_after_fees_and_reserve_bps=after_reserve,
        exit_fee_assumption_bps=entry_fees,
        reserve_bps=Decimal(params.reserve_bps),
        funding_estimate_bps=funding,
        funding_status=funding_status,
        quality_ok=verdict.quality_ok,
        cost_qualified=cost_qualified,
        mapping_verified=mapping_verified,
        mapping_status=MAPPING_VERIFIED if mapping_verified else MAPPING_UNVERIFIED,
        reject_reason=reason,
        receive_age_sell_ms=verdict.receive_age_ms.get(sell),
        receive_age_buy_ms=verdict.receive_age_ms.get(buy),
        event_skew_ms=verdict.event_skew_ms,
        event_age_sell_ms=verdict.event_age_ms.get(sell),
        event_age_buy_ms=verdict.event_age_ms.get(buy),
        note=note,
    )


def _reference_mid(sell_book: BookState, buy_book: BookState) -> Decimal | None:
    """The two legs' touch mids averaged - the same reference the watcher uses."""
    if (
        sell_book.best_bid is None or sell_book.best_ask is None
        or buy_book.best_bid is None or buy_book.best_ask is None
    ):
        return None
    return ((sell_book.best_bid + sell_book.best_ask) / 2
            + (buy_book.best_bid + buy_book.best_ask) / 2) / 2


def _replay_tape(
    files: TapeFiles,
    ctx: _TapeContext,
    params: Params,
    accs: dict[tuple[str, str, str, Decimal], _Acc],
    hits: list[Row],
) -> None:
    """Replay one tape once and accumulate every comparison it implies."""
    if not ctx.directions:
        return
    reader = read_tape(list(files.fragments))
    for step in replay_events(reader):
        # A gap marker is an evaluation moment of its own: it is where a comparison
        # would have been made and cannot be, so the reason distribution names it.
        if step.kind not in (BOOK, QUOTE, GAP):
            continue
        for sell, buy in ctx.directions:
            for notional in params.notionals:
                acc = accs[(ctx.symbol, sell, buy, Decimal(notional))]
                row = _evaluate(ctx, step, sell, buy, notional)
                acc.add(row)
                if row.is_hit and len(hits) < params.max_hits:
                    hits.append(row)


# ------------------------------------------------------------- the report


@dataclass(frozen=True)
class Report:
    """The analysis, ready to be written (JSON + two CSVs + Markdown)."""

    document: dict[str, Any]
    summary_rows: list[dict[str, Any]]
    hit_rows: list[dict[str, Any]]
    markdown: str

    def write(self, out_dir: Path) -> list[Path]:
        out_dir = Path(out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = [
            out_dir / f"{REPORT_STEM}.json",
            out_dir / f"{REPORT_STEM}.md",
            out_dir / f"{REPORT_STEM}_summary.csv",
            out_dir / f"{REPORT_STEM}_hits.csv",
        ]
        paths[0].write_text(
            json.dumps(self.document, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        paths[1].write_text(self.markdown, encoding="utf-8")
        _write_csv(paths[2], SUMMARY_FIELDS, self.summary_rows)
        _write_csv(paths[3], ROW_FIELDS, self.hit_rows)
        return paths


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _csv_value(value: Any) -> str:
    if value is None:
        return UNKNOWN
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _write_csv(path: Path, fields: Sequence[str], rows: Sequence[Mapping[str, Any]]) -> None:
    with Path(path).open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(fields), lineterminator="\n")
        writer.writeheader()
        for row in rows:
            writer.writerow({name: _csv_value(row.get(name)) for name in fields})


SUMMARY_FIELDS = (
    "symbol", "sell_venue", "buy_venue", "notional_usd", "common_step", "samples",
    "quality_pass", "pass", "reject", "hits", "executable_hits", "zero_opportunity",
    "entry_fees_bps", "exit_fee_assumption_bps", "reserve_bps", "mapping_verified",
    "executable", "gross_entry_bps_median", "gross_entry_bps_min", "gross_entry_bps_max",
    "entry_after_fees_bps_median", "entry_after_fees_bps_min", "entry_after_fees_bps_max",
    "funding_estimate_bps_median", "funding_estimate_bps_min", "funding_estimate_bps_max",
    "event_age_sell_ms_max", "event_age_buy_ms_max",
    "median_sample", "reject_reasons", "note",
)


def _reason_text(reasons: Mapping[str, int]) -> str:
    ordered = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    return ";".join(f"{name}={count}" for name, count in ordered)


def _acc_dict(acc: _Acc, params: Params) -> dict[str, Any]:
    verified = (acc.sell, acc.buy) in params.mapping_verified
    reject = acc.samples - acc.passed
    return {
        "notional_usd": str(acc.notional),
        "common_step": _dec_text(acc.step),
        "samples": acc.samples,
        "quality_pass": acc.quality_pass,
        "pass": acc.passed,
        "reject": reject,
        "hits": acc.hits,
        "executable_hits": acc.executable_hits,
        "zero_opportunity": acc.hits == 0,
        "reject_reasons": {
            name: count
            for name, count in sorted(acc.reasons.items(), key=lambda item: (-item[1], item[0]))
        },
        "entry_fees_bps": _dec_text(acc.fees),
        "exit_fee_assumption_bps": _dec_text(acc.fees),
        "reserve_bps": str(params.reserve_bps),
        "mapping_verified": verified,
        "executable": False,
        "gross_entry_bps_median": _dec_text(_median(acc.gross)),
        "gross_entry_bps_min": _dec_text(acc.gross_min),
        "gross_entry_bps_max": _dec_text(acc.gross_max),
        "entry_after_fees_bps_median": _dec_text(_median(acc.after)),
        "entry_after_fees_bps_min": _dec_text(acc.after_min),
        "entry_after_fees_bps_max": _dec_text(acc.after_max),
        "funding_estimate_bps_median": _dec_text(_median(acc.funding)),
        "funding_estimate_bps_min": _dec_text(acc.funding_min),
        "funding_estimate_bps_max": _dec_text(acc.funding_max),
        "event_age_sell_ms_max": _dec_text(acc.event_age_sell_max),
        "event_age_buy_ms_max": _dec_text(acc.event_age_buy_max),
        "median_sample": "first " + str(SAMPLE_KEEP) if acc.truncated else "all",
        "note": acc.note,
        "examples": [row.as_dict() for row in acc.examples],
    }


def analyse_run(
    run_dir: Path, params: Params, *, generated_utc: str | None = None,
) -> Report:
    """Read one recorded run directory and produce the whole report.

    The ``l2`` directory (or the directory itself, if it already is one) is read once
    per tape for its run-level facts and once more for the replay, so the fee snapshot
    and the metadata a row sees are *run* facts rather than an accident of arrival
    order. Every requested (market, direction, notional) bucket exists in the output
    even when the tape has nothing for it: an empty result is a result.
    """
    run_dir = Path(run_dir)
    l2_dir = resolve_l2_dir(run_dir)
    tapes = discover_tapes(l2_dir)
    if not tapes:
        raise DepthError(
            f"[ondo-depth] no tape fragment under {l2_dir}: run the recorder first "
            f"(spread_watch.py --record-l2 --out <runDir>)",
        )
    scans: list[TapeScan] = []
    ignored: list[str] = []
    tapes_by_symbol: dict[str, list[TapeScan]] = {}
    accs: dict[tuple[str, str, str, Decimal], _Acc] = {}
    for symbol in params.symbols:
        for sell, buy in params.directions:
            for notional in params.notionals:
                accs[(symbol, sell, buy, Decimal(notional))] = _Acc(
                    symbol=symbol, sell=sell, buy=buy, notional=Decimal(notional),
                )
    hits: list[Row] = []
    for files in tapes:
        scan = scan_tape(files)
        scans.append(scan)
        symbol = scan.symbol or _symbol_from_tape(files.tape)
        if symbol is None or symbol not in params.symbols:
            ignored.append(files.tape)
            continue
        tapes_by_symbol.setdefault(symbol, []).append(scan)
        ctx = _TapeContext(symbol, scan, params)
        for sell, buy in ctx.skipped:
            missing = [venue for venue in (sell, buy) if venue not in scan.book_venues]
            for notional in params.notionals:
                acc = accs[(symbol, sell, buy, Decimal(notional))]
                acc.note = (
                    f"tape {files.tape} carries no depth record for "
                    + ", ".join(missing) + ": the direction cannot be compared"
                )
        for sell, buy in params.directions:
            fee_sell, fee_buy = ctx.fees.get(sell), ctx.fees.get(buy)
            bucket_fees = (
                None if fee_sell is None or fee_buy is None else fee_sell + fee_buy
            )
            for notional in params.notionals:
                acc = accs[(symbol, sell, buy, Decimal(notional))]
                acc.step = ctx.direction_step.get((sell, buy))
                acc.fees = bucket_fees
        _replay_tape(files, ctx, params, accs, hits)

    document = _document(
        run_dir=run_dir, l2_dir=l2_dir, params=params, scans=scans,
        tapes_by_symbol=tapes_by_symbol, accs=accs, hits=hits, ignored=ignored,
        generated_utc=generated_utc or _now_utc(),
    )
    summary_rows = _summary_rows(document, params)
    return Report(
        document=document,
        summary_rows=summary_rows,
        hit_rows=[row.as_dict() for row in hits],
        markdown=_markdown(document, summary_rows),
    )


def _symbol_from_tape(tape: str) -> str | None:
    """``l2_NVDA_ONDO-ASTER_<stamp>`` -> ``NVDA`` (a fallback for an empty tape)."""
    name = tape
    if name.startswith("l2_"):
        name = name[3:]
    head = name.split("_")[0]
    return head.upper() or None


def _document(
    *,
    run_dir: Path,
    l2_dir: Path,
    params: Params,
    scans: Sequence[TapeScan],
    tapes_by_symbol: Mapping[str, Sequence[TapeScan]],
    accs: Mapping[tuple[str, str, str, Decimal], _Acc],
    hits: Sequence[Row],
    ignored: Sequence[str],
    generated_utc: str,
) -> dict[str, Any]:
    markets: list[dict[str, Any]] = []
    zero_markets: list[str] = []
    for symbol in params.symbols:
        symbol_scans = list(tapes_by_symbol.get(symbol, ()))
        notes: list[str] = []
        if not symbol_scans:
            notes.append(f"no tape fragment under {l2_dir} carries {symbol}")
        directions: list[dict[str, Any]] = []
        for sell, buy in params.directions:
            verified = (sell, buy) in params.mapping_verified
            notionals = [
                _acc_dict(accs[(symbol, sell, buy, Decimal(notional))], params)
                for notional in params.notionals
            ]
            totals = {
                "samples": sum(n["samples"] for n in notionals),
                "quality_pass": sum(n["quality_pass"] for n in notionals),
                "pass": sum(n["pass"] for n in notionals),
                "reject": sum(n["reject"] for n in notionals),
                "hits": sum(n["hits"] for n in notionals),
                "executable_hits": sum(n["executable_hits"] for n in notionals),
            }
            totals["zero_opportunity"] = totals["hits"] == 0
            reasons: Counter = Counter()
            for entry in notionals:
                for name, count in entry["reject_reasons"].items():
                    reasons[name] += count
            totals["reject_reasons"] = {
                name: count
                for name, count in sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
            }
            directions.append({
                "sell_venue": sell,
                "buy_venue": buy,
                "direction": f"{sell}>{buy}",
                "mapping_verified": verified,
                "mapping_status": MAPPING_VERIFIED if verified else MAPPING_UNVERIFIED,
                "notes": [] if verified else [MAPPING_NOTE],
                "totals": totals,
                "notionals": notionals,
            })
        if all(entry["totals"]["hits"] == 0 for entry in directions):
            zero_markets.append(symbol)
        markets.append({
            "symbol": symbol,
            "tapes": [scan.tape for scan in symbol_scans],
            "notes": notes,
            "directions": directions,
        })
    return {
        "report": REPORT_STEM,
        "schema_version": SCHEMA_VERSION,
        "generated_utc": generated_utc,
        "mode": "read-only",
        "executable": False,
        "clock_offset_unknown": True,
        "clock_offset_unknown_note": CLOCK_OFFSET_NOTE,
        "run_dir": str(run_dir),
        "l2_dir": str(l2_dir),
        "research_parameters": {
            "symbols": list(params.symbols),
            "venues": list(params.venues),
            "directions": [f"{sell}>{buy}" for sell, buy in params.directions],
            "notionals_usd": [str(notional) for notional in params.notionals],
            "max_age_ms": params.quality.max_age_ms,
            "max_skew_ms": params.quality.max_skew_ms,
            "future_tolerance_ms": params.quality.future_tolerance_ms,
            "reserve_bps": str(params.reserve_bps),
            "funding_basis": "bps_per_hour",
            "exit_status": UNCLOSED,
            "clock_offset_unknown": True,
            "clock_offset_unknown_note": CLOCK_OFFSET_NOTE,
            "event_age_ms_rule": EVENT_AGE_RULE,
            "quantity_reference": "sell_leg_best_bid",
            "common_step_rule": "integer_scaled_lcm",
            "median_sample": f"first {SAMPLE_KEEP} passing rows per bucket",
            "mapping_verified_pairs": [f"{sell}>{buy}" for sell, buy in sorted(
                params.mapping_verified)],
            "note": (
                "these thresholds are research parameters (plan 5.2 defaults), printed "
                "here so a report can never be read without them"
            ),
        },
        "field_notes": FIELD_NOTES,
        "fee_snapshot": _fee_snapshot(params, tapes_by_symbol),
        "tapes": [scan.as_dict() for scan in scans],
        "markets": markets,
        "hits": [row.as_dict() for row in hits],
        "hits_written": len(hits),
        "hit_limit": params.max_hits,
        "zero_opportunity_markets": zero_markets,
        "ignored_tapes": list(ignored),
        "notes": [
            "no row is executable: mapping_verified is False for every pair compared "
            "here, and executable is always False (plan 5.2)",
            "the round trip stays 'unclosed' (exit_status): P2 has no exit book and no "
            "realised funding interval, and the spread is never assumed to revert",
            "each bucket's 'examples' are the first rows it computed numbers for, so a "
            "withheld fee or a short book still shows the quantity and the VWAPs behind "
            "the rejection",
            "a fresh local receipt does not prove a fresh venue event: every row carries "
            "each leg's own venue event age (event_age_sell_ms / event_age_buy_ms) beside "
            "the receive ages, and the local clock offset is not measured "
            "(clock_offset_unknown), so a receive age is never network latency",
            "'unknown' is the one marker for a value this run's tape did not carry; it "
            "is never replaced by a registry default",
        ],
    }


def _fee_snapshot(
    params: Params, tapes_by_symbol: Mapping[str, Sequence[TapeScan]],
) -> dict[str, Any]:
    """Every leg's fee and step as *this run's tape* recorded them."""
    snapshot: dict[str, Any] = {}
    for symbol in params.symbols:
        symbol_scans = list(tapes_by_symbol.get(symbol, ()))
        entry: dict[str, Any] = {}
        for venue in params.venues:
            step, origin, precision = _resolve_scan_step(venue, symbol_scans, params)
            payload: Mapping[str, Any] = {}
            for scan in symbol_scans:
                if venue in scan.metadata:
                    payload = scan.metadata[venue]
            fee = _fee_bps(payload.get("taker_fee_bps"), payload.get("fee_source")) if (
                payload
            ) else None
            entry[venue] = {
                "taker_fee_bps": _dec_text(fee),
                "fee_source": payload.get("fee_source") if payload else None,
                "size_precision": precision,
                "step": _dec_text(step),
                "step_origin": origin,
                "instrument_metadata": bool(payload),
            }
        snapshot[symbol] = entry
    return snapshot


def _resolve_scan_step(venue, scans, params):
    for scan in scans:
        if venue in scan.metadata:
            return _resolve_step(venue, scan, params)
    return _resolve_step(venue, TapeScan(
        tape="", files=(), fragments=0, records=0, first_arrival_seq=None,
        last_arrival_seq=None, symbol=None, book_venues=(),
        metadata={}, instrument_records=0, sessions=(), complete=False, gaps=0,
        dropped=0, truncated_tail=False, truncated_at=None, run_ids=(),
        failure_reason=None,
    ), params)


def _summary_rows(document: Mapping[str, Any], params: Params) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for market_entry in document["markets"]:
        for direction_entry in market_entry["directions"]:
            for entry in direction_entry["notionals"]:
                rows.append({
                    "symbol": market_entry["symbol"],
                    "sell_venue": direction_entry["sell_venue"],
                    "buy_venue": direction_entry["buy_venue"],
                    "notional_usd": entry["notional_usd"],
                    "common_step": entry["common_step"],
                    "samples": entry["samples"],
                    "quality_pass": entry["quality_pass"],
                    "pass": entry["pass"],
                    "reject": entry["reject"],
                    "hits": entry["hits"],
                    "executable_hits": entry["executable_hits"],
                    "zero_opportunity": entry["zero_opportunity"],
                    "entry_fees_bps": entry["entry_fees_bps"],
                    "exit_fee_assumption_bps": entry["exit_fee_assumption_bps"],
                    "reserve_bps": entry["reserve_bps"],
                    "mapping_verified": direction_entry["mapping_verified"],
                    "executable": entry["executable"],
                    "gross_entry_bps_median": entry["gross_entry_bps_median"],
                    "gross_entry_bps_min": entry["gross_entry_bps_min"],
                    "gross_entry_bps_max": entry["gross_entry_bps_max"],
                    "entry_after_fees_bps_median": entry["entry_after_fees_bps_median"],
                    "entry_after_fees_bps_min": entry["entry_after_fees_bps_min"],
                    "entry_after_fees_bps_max": entry["entry_after_fees_bps_max"],
                    "funding_estimate_bps_median": entry["funding_estimate_bps_median"],
                    "funding_estimate_bps_min": entry["funding_estimate_bps_min"],
                    "funding_estimate_bps_max": entry["funding_estimate_bps_max"],
                    "event_age_sell_ms_max": entry["event_age_sell_ms_max"],
                    "event_age_buy_ms_max": entry["event_age_buy_ms_max"],
                    "median_sample": entry["median_sample"],
                    "reject_reasons": _reason_text(entry["reject_reasons"]),
                    "note": entry["note"],
                })
    return rows


def _markdown(document: Mapping[str, Any], summary_rows: Sequence[Mapping[str, Any]]) -> str:
    params = document["research_parameters"]
    lines = [
        f"# Ondo depth analysis — same-quantity VWAP ({REPORT_STEM}, plan 5.2 / P2)",
        "",
        f"run `{document['run_dir']}`  |  l2 `{document['l2_dir']}`  |  mode "
        f"{document['mode']}  |  generated {document['generated_utc']}",
        "",
        f"markets {', '.join(params['symbols'])}  |  directions "
        f"{', '.join(params['directions'])}  |  notionals "
        f"${'/'.join(params['notionals_usd'])}  |  thresholds: receive age "
        f"{params['max_age_ms']} ms, event skew {params['max_skew_ms']} ms, future "
        f"tolerance {params['future_tolerance_ms']} ms  |  reserve "
        f"{params['reserve_bps']} bps  |  quantity from the sell leg's best bid, common "
        f"step {params['common_step_rule']}  |  funding {params['funding_basis']}  |  "
        f"exit {params['exit_status']}",
        "",
        f"**No row here is executable.** `mapping_verified` is False for every pair "
        f"compared (`{MAPPING_UNVERIFIED}`) and `executable` is always False: this is a "
        f"nominal comparison of two books, not an order promise.",
        "",
        f"**A fresh receipt is not a fresh venue event.** {CLOCK_OFFSET_NOTE}.",
        "",
    ]
    if not document["hits"]:
        lines += [
            f"No hit (entry_after_fees_bps > 0) was recorded; {len(document['hits'])} of "
            f"at most {document['hit_limit']} sample rows are stored. Zero opportunities "
            f"is a result, not a missing section.",
            "",
        ]
    lines += ["## Fee snapshot (from this run's tape, never today's registry)", ""]
    lines += ["| market | venue | taker bps | source | step | origin |", "|---|---|---|---|---|---|"]
    for symbol, venues in document["fee_snapshot"].items():
        for venue, entry in venues.items():
            lines.append(
                f"| {symbol} | {venue} | {entry['taker_fee_bps']} | "
                f"{entry['fee_source'] or UNKNOWN} | {entry['step']} | "
                f"{entry['step_origin'] or UNKNOWN} |",
            )
    lines += ["", "## Tape verdict", ""]
    lines += [
        "| tape | symbol | fragments | records | sessions | complete | gaps | dropped |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for tape in document["tapes"]:
        lines.append(
            f"| {tape['tape']} | {tape['symbol'] or UNKNOWN} | {tape['fragments']} | "
            f"{tape['records']} | {len(tape['sessions'])} | "
            f"{'true' if tape['complete'] else 'false'} | {tape['gaps']} | "
            f"{tape['dropped']} |",
        )
    if document["ignored_tapes"]:
        lines += ["", f"tapes not requested (ignored): {', '.join(document['ignored_tapes'])}"]
    for market_entry in document["markets"]:
        lines += ["", f"## {market_entry['symbol']}"]
        for note in market_entry["notes"]:
            lines.append(f"*{note}*")
        for direction_entry in market_entry["directions"]:
            lines += ["", f"### {direction_entry['direction']}", ""]
            lines += [
                "| notional | samples | quality pass | pass | reject | hits | "
                "exec-files | median gross bps | median after fees bps | "
                "max event age sell/buy ms | top reject |",
                "|---|---|---|---|---|---|---|---|---|---|---|",
            ]
            for entry in direction_entry["notionals"]:
                reasons = entry["reject_reasons"]
                top = next(iter(reasons), "-") if reasons else "-"
                lines.append(
                    f"| ${entry['notional_usd']} | {entry['samples']} | "
                    f"{entry['quality_pass']} | {entry['pass']} | {entry['reject']} | "
                    f"{entry['hits']} | {entry['executable_hits']} | "
                    f"{entry['gross_entry_bps_median']} | "
                    f"{entry['entry_after_fees_bps_median']} | "
                    f"{entry['event_age_sell_ms_max']}/"
                    f"{entry['event_age_buy_ms_max']} | {top} |",
                )
            totals = direction_entry["totals"]
            distribution = ", ".join(
                f"{name} {count}" for name, count in totals["reject_reasons"].items()
            ) or "none"
            lines.append("")
            lines.append(
                f"totals: samples {totals['samples']}, quality-pass "
                f"{totals['quality_pass']}, pass {totals['pass']}, reject "
                f"{totals['reject']}, hits {totals['hits']} (executable "
                f"{totals['executable_hits']}), zero-opportunity "
                f"{str(totals['zero_opportunity']).lower()}  |  reject reasons: "
                f"{distribution}",
            )
            for note in direction_entry["notes"]:
                lines.append(f"*{note}*")
    lines += ["", "## Event age vs receive age (venue clock, per example row)", ""]
    lines.append(
        f"`event_age_sell_ms` / `event_age_buy_ms` are the venue's own: "
        f"{params['event_age_ms_rule']}. They are printed beside the receive ages so a "
        f"book that is minutes old on the venue's clock while its local receipt is fresh "
        f"is visible instead of passing as a fresh quote. Each bucket's own table also "
        f"reports the largest event age it saw on each leg "
        f"(`event_age_sell_ms_max` / `event_age_buy_ms_max`), which no bounded sample of "
        f"example or hit rows can hide.",
    )
    age_rows = [
        (market_entry["symbol"], direction_entry["direction"], entry, row)
        for market_entry in document["markets"]
        for direction_entry in market_entry["directions"]
        for entry in direction_entry["notionals"]
        for row in entry["examples"]
    ]
    if age_rows:
        lines += [
            "",
            "| market | direction | notional | arrival_seq | receive sell/buy ms | "
            "event sell/buy ms |",
            "|---|---|---|---|---|---|",
        ]
        for symbol, direction_name, entry, row in age_rows:
            lines.append(
                f"| {symbol} | {direction_name} | ${entry['notional_usd']} | "
                f"{row['arrival_seq']} | {row['receive_age_sell_ms']}/"
                f"{row['receive_age_buy_ms']} | {row['event_age_sell_ms']}/"
                f"{row['event_age_buy_ms']} |",
            )
    else:
        lines.append("")
        lines.append(
            "*No row computed a quantity, so no event age was measured; the receive ages "
            "of the moments that were evaluated are in the buckets above.*",
        )
    lines += ["", "## Field names (plan 5.2)", ""]
    for name in ("gross_entry_bps", "entry_fees_bps", "entry_after_fees_bps",
                 "exit_fee_assumption_bps", "reserve_bps", "funding_estimate_bps",
                 "quality_ok", "mapping_verified", "reject_reason", "executable",
                 "receive_age_sell_ms", "receive_age_buy_ms",
                 "event_age_sell_ms", "event_age_buy_ms", "clock_offset_unknown"):
        lines.append(f"- `{name}`: {document['field_notes'][name]}")
    lines += ["", "## What P2 acceptance still needs", ""]
    lines += [
        "- a real recording run (`spread_watch.py --symbols NVDA,TSLA --venues ONDO,ASTER "
        "--record-l2 --out <runDir>`, plan §8) so this analysis has live fragments: every "
        "number above is only as good as the tape it was read from",
        "- the exit side: a reverse book after entry (`unclosed` here) and a realised "
        "funding interval, before any round-trip number may be quoted",
        "- the mapping verification: until the multiplier / settlement / underlying "
        "equivalence of ONDO and ASTER is verified with evidence, no row may be called "
        "executable",
    ]
    lines += [""]
    return "\n".join(lines)


# ---------------------------------------------------------------------- CLI


def parse_steps(text: str | None) -> dict[str, Decimal]:
    """``ONDO=0.001,ASTER=0.001`` -> the explicit step override, or nothing."""
    steps: dict[str, Decimal] = {}
    if not text:
        return steps
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        venue, sep, value = chunk.partition("=")
        if not sep:
            raise DepthError(f"[ondo-depth] bad --steps {chunk!r}: expected VENUE=DECIMAL")
        try:
            step = Decimal(value.strip())
        except ArithmeticError as exc:
            raise DepthError(f"[ondo-depth] bad --steps {chunk!r}: {exc}") from exc
        if step <= 0:
            raise DepthError(f"[ondo-depth] bad --steps {chunk!r}: the step must be > 0")
        steps[venue.strip().upper()] = step
    return steps


def parse_pairs(text: str | None) -> frozenset[tuple[str, str]]:
    """``ONDO>ASTER`` pairs the caller explicitly declares as verified."""
    pairs: set[tuple[str, str]] = set()
    if not text:
        return frozenset()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        sell, sep, buy = chunk.partition(">")
        if not sep or not sell.strip() or not buy.strip():
            raise DepthError(
                f"[ondo-depth] bad --mapping-verified {chunk!r}: expected SELL>BUY",
            )
        pairs.add((sell.strip().upper(), buy.strip().upper()))
    return frozenset(pairs)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Deterministic replay of the multi-leg L2 tape and the same-quantity VWAP "
            "comparison (plan 5.2). Read-only, offline."
        ),
    )
    parser.add_argument("--dir", type=Path, default=Path("reports/stage1"),
                        help="the run directory (its l2/ subdirectory holds the tape)")
    parser.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS),
                        help="comma list of markets (default: NVDA,TSLA)")
    parser.add_argument("--venues", default=",".join(DEFAULT_VENUES),
                        help="the two venue keys compared, both directions")
    parser.add_argument("--notionals", default=",".join(
        str(value) for value in DEFAULT_NOTIONALS),
        help="comma list of nominal USD tiers used to choose the base quantity")
    parser.add_argument("--max-age-ms", type=int, default=DEFAULT_MAX_AGE_MS,
                        help="max receive age of either leg's own book")
    parser.add_argument("--max-skew-ms", type=int, default=DEFAULT_MAX_SKEW_MS,
                        help="max difference of the two legs' own event times")
    parser.add_argument("--future-ms", type=int, default=DEFAULT_FUTURE_TOLERANCE_MS,
                        help="a timestamp this far in the future fails")
    parser.add_argument("--steps", default=None,
                        help="optional VENUE=DECIMAL quantity-step override")
    parser.add_argument("--mapping-verified", default=None,
                        help="SELL>BUY pairs whose multiplier equivalence is verified")
    parser.add_argument("--max-hits", type=int, default=200,
                        help="how many passing rows to store in the report")
    parser.add_argument("--out", type=Path, default=None,
                        help="output directory (default: <dir>/depth-analysis)")
    return parser


def _params_from_args(args) -> Params:
    symbols = tuple(chunk.strip().upper() for chunk in args.symbols.split(",") if chunk.strip())
    venues = tuple(chunk.strip().upper() for chunk in args.venues.split(",") if chunk.strip())
    if len(venues) != 2:
        raise DepthError(
            f"[ondo-depth] --venues needs exactly two venue keys, got {args.venues!r}",
        )
    notionals: list[Decimal] = []
    for chunk in args.notionals.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            notionals.append(Decimal(chunk))
        except ArithmeticError as exc:
            raise DepthError(f"[ondo-depth] bad --notionals {args.notionals!r}: {exc}") from exc
    if not symbols or not notionals:
        raise DepthError("[ondo-depth] --symbols and --notionals must not be empty")
    return Params(
        symbols=symbols,
        venues=venues,  # type: ignore[arg-type]
        notionals=tuple(notionals),
        quality=QualityParams(
            max_age_ms=int(args.max_age_ms), max_skew_ms=int(args.max_skew_ms),
            future_tolerance_ms=int(args.future_ms),
        ),
        steps=parse_steps(args.steps),
        mapping_verified=parse_pairs(args.mapping_verified),
        max_hits=int(args.max_hits),
    )


def main(argv: Sequence[str] | None = None) -> int:
    """The plan §8 command. Returns 0 on a written report, 2 on an unusable run."""
    args = build_parser().parse_args(argv)
    try:
        params = _params_from_args(args)
        report = analyse_run(args.dir, params)
    except (DepthError, TapeError, ReplayError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    out_dir = Path(args.out) if args.out is not None else Path(args.dir) / "depth-analysis"
    written = report.write(out_dir)
    print(report.markdown, end="")
    print(f"[ondo-depth] wrote {', '.join(str(path) for path in written)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


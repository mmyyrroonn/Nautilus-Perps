#!/usr/bin/env python3
"""Deterministic replay of the multi-leg L2 tape and the same-quantity VWAP
comparison (plan 5.2, task 5 / P2).

The legacy 2/5/10 bps capacity CSVs cannot rebuild a VWAP and they record one
venue's depth next to another venue's top of book, so the P2 question ("what does
it actually cost to cross *both* books with *one* base quantity?") is answered
from the standardized tape alone (``src/market_tape.py``, plan 5.1) - the fragments
under ``<runDir>/l2``, read in the manifest's order.

Seven rules this module exists to keep honest:

1. **Receive order is the only order.** :func:`replay_events` advances the books by
   ``arrival_seq`` and never sorts by ``ts_event_ns``: sorting by exchange time
   would fabricate information that was not visible when the decision was made
   (plan 5.1). A *fresh quote* therefore never refreshes a *stale book*: the depth
   time travels with the book record, not with the quote that followed it.
2. **One base quantity, not two "$100 each" scans.** A nominal tier
   ($100/$500/$1000) only *chooses* one base quantity: it is computed from the
   then-visible bid price of the leg being sold and snapped to the quantity both
   venues can represent exactly (the integer-scaled LCM of the two legs' *real*
   quantity steps, never ``max(step_a, step_b)``). Both legs are then filled for
   that one quantity.
3. **Freshness is per arrival and per session.** The quality gate measures each
   leg's own book against the local receipt time of the record being evaluated
   (``recorded_mono_ns``), inside one session: two sessions' monotonic clocks are
   not comparable. A *fresh receipt* proves nothing about the *venue event*: every
   row carries each leg's own **event age** - the *local epoch receipt time of this
   record* (``ts_init_ns``) minus that leg's book *venue* event time - and
   ``clock_offset_unknown`` says plainly that the local clock offset, and therefore
   the network latency, is not measured here. A negative age is kept as the clock
   evidence it is: it is never floored to zero and never turned into an absolute
   value, because that would hide the very skew the field exists to show.
4. **Fees and steps come from this run's tape, arrival by arrival.** Instrument
   metadata is applied in receive order (F06): an arrival is priced with the
   metadata *it* could see, so a metadata update - or a whole new session - never
   rewrites the numbers already published for the arrivals before it. The fee
   snapshot is the tape's own instrument records; a fee that is missing there
   withholds the leg's cost qualification and is never replaced by today's static
   registry (plan 5.2 precedence: account rate > live public metadata > a dated
   documentation assumption). The quantity step is the instrument's real
   ``size_increment`` or it is ``quantity_step_unknown``: ``10 ** -size_precision``
   is **never** used as a step (F08), and a CLI ``--steps`` override is published
   as ``override``, never as a venue-verified increment.
5. **Feed, market and metadata are three separate axes.** A book record's own
   ``valid`` says whether *that depth* is usable; it does not say whether the local
   feed is up, whether the venue is trading, or whether the instrument metadata is
   trusted. The replay keeps the three as independent state: only an explicit
   ``adapter:snapshot_ready`` re-arms the feed, only an explicit trading status
   (``is_trading=True``) lifts a real halt, ``snapshot_ready`` **never** lifts one,
   and a ``metadata_stale`` instrument record makes that venue's metadata unusable
   from that arrival on. Each refusal names its own axis (F07).
6. **A new session re-announces its own facts.** One tape session is one writer's
   run: the three axes above start each session the way the recorder's own state
   starts (feed up, market trading, metadata unknown until the session's instrument
   records arrive). A session that publishes no instrument metadata is
   ``metadata_unknown`` for its whole span - it never inherits another session's
   fee or step, and no future session can re-price it.
7. **``mapping_verified`` is published next to every row** and today no venue pair
   is verified, so ``executable`` is always ``False``: this is research
   observation, never an order promise. Fixing the quantity, the VWAP or the fee
   arithmetic proves nothing about the contract multiplier, the settlement asset,
   the trading session or the underlying/index equivalence of the two legs.

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
import dataclasses
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

SCHEMA_VERSION = 2  # this *report* document's schema (it gained the axis fields)
REPORT_STEM = "ondo_depth"

# The tape schemas this reader accepts (contract A). Version 1 is the historical
# tape: one metadata block at the top of the run, no per-arrival metadata and no
# ``size_increment``. Version 2 adds the per-arrival instrument records. The reader
# never branches on the version itself - a missing field is simply unknown, which is
# exactly how a v1 tape reads - but it states the contract here so the two versions
# cannot drift apart silently.
TAPE_SCHEMA_VERSIONS = (1, 2)

# ---------------------------------------------------------------- parameters

# Plan 5.2's defaults. They are *research parameters*: every report prints them.
DEFAULT_MAX_AGE_MS = 2_000  # both legs' receive age, measured per arrival
DEFAULT_MAX_SKEW_MS = 500  # both legs' own event-time difference
DEFAULT_FUTURE_TOLERANCE_MS = 1_000  # an event claiming to be from the future
# F09: an event-age threshold is a *decision*, not a default. The event age is the
# local epoch receipt time minus the venue event time, so it contains the (unmeasured)
# local clock offset to that venue; rejecting on it while that offset is unknown would
# turn a clock skew into a market conclusion. ``None`` means "reported, not enforced"
# and the report says so on every row (``event_age_status``).
DEFAULT_MAX_EVENT_AGE_MS: int | None = None
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
# The plan's own name for "no real size_increment reached this moment": the quantity
# step is unknown and is never inferred from ``size_precision`` (F08).
REJECT_STEP_UNKNOWN = "quantity_step_unknown"
REJECT_BELOW_ONE_STEP = "below_one_step"
REJECT_INSUFFICIENT_DEPTH = "insufficient_depth"
# F07: the market axis and the metadata axis get their own reasons, so a refusal says
# which of the three axes (feed / market / metadata) caused it. ``metadata_stale`` is
# the same text the tape's own invalid_reason uses for a metadata record (contract B).
REJECT_MARKET_HALTED = "market_halted"
REJECT_METADATA_STALE = "metadata_stale"
# F09: only reachable when the caller explicitly sets ``--max-event-age-ms``.
REJECT_EVENT_AGE = "event_age_exceeded"

BOOK_REASONS_IN_TAPE = frozenset({REJECT_EMPTY_BOOK, REJECT_ONE_SIDED, REJECT_CROSSED})

# The *version-1* watcher wrote this marker on a book record whose leg had
# ``feed_ready and book_valid`` false, i.e. it folded the feed axis into the depth's own
# validity (F18). Version-2 tapes never carry it. A v1 tape therefore cannot say which
# axis it meant, and the report says exactly that instead of assuming either one.
V1_FEED_INVALIDATED = "feed-invalidated"
V1_FEED_INVALIDATED_NOTE = (
    "this book record carries the version-1 marker 'feed-invalidated', which folded "
    "the local feed state into the depth's own validity: a version-1 tape cannot say "
    "which axis it meant, so the depth is refused here and neither axis is assumed"
)

# MarketStatusAction *names* that mean "not tradable right now", mirroring
# ``spread_watch.NON_TRADING_ACTIONS`` (the tape stores the action's name as text, and
# a test pins the two sets against each other so they cannot drift). ``None``
# ``is_trading`` with a non-halting action keeps the state it had: readiness is never
# assumed from a status that does not say "trading".
NON_TRADING_ACTIONS = frozenset({
    "HALT", "SUSPEND", "CLOSE", "PRE_CLOSE", "POST_CLOSE", "NOT_AVAILABLE_FOR_TRADING",
})
# The adapter's own *local feed* notices: they travel on the same status record as a
# venue halt and must never be read as one (plan 4.2).
ADAPTER_REASON_PREFIX = "adapter:"
ADAPTER_DISCONNECTED = "adapter:disconnected"
ADAPTER_SNAPSHOT_READY = "adapter:snapshot_ready"

# Where a venue's quantity step came from. ``override`` is the caller's CLI declaration
# and never claims the venue verified anything (contract D); ``instrument_metadata``
# means the tape carried a real ``size_increment``; ``quantity_step_unknown`` means it
# did not and no override was given.
STEP_ORIGIN_OVERRIDE = "override"
STEP_ORIGIN_METADATA = "instrument_metadata"
STEP_ORIGIN_UNKNOWN = "quantity_step_unknown"
STEP_ORIGIN_MIXED = "mixed"

# Plan 5.2: "接收新鲜也不证明交易所事件新鲜；报告同时给出 event age 和时钟偏差未知标记，
# 不将本地 clock offset 混成网络延迟结论". The two texts below are the report's own words
# for that rule; they are published in the JSON, in the Markdown and (as a field note)
# next to the field names, so a reader can never see a receive age without them.
EVENT_AGE_RULE = (
    "the local epoch receipt time of the record being evaluated (ts_init_ns) minus the "
    "leg's own book venue event time (ts_event_ns), in exact integer-nanosecond "
    "milliseconds: it is the age measured on the local clock, so two legs delayed "
    "together by the same feed never cancel out to 0, and a missing/unusable stamp is "
    "'unknown' (never 0). A negative value is kept: it is clock evidence (the venue's "
    "stamp is ahead of the local receipt), never floored to 0 and never made absolute"
)
CLOCK_OFFSET_NOTE = (
    "a fresh local receipt never proves a fresh venue event: the local clock offset to "
    "either venue is NOT measured (clock_offset_unknown), so receive_age_sell_ms / "
    "receive_age_buy_ms are local RECEIVE ages and are never network latency; read them "
    "beside event_age_sell_ms / event_age_buy_ms, which can be minutes while the receive "
    "age is one millisecond, and remember that the event age itself carries the "
    "unmeasured offset between the local clock and the venue's"
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


def fee_bps(value: Any, source: Any) -> Decimal | None:
    """A leg's taker fee from a tape metadata payload, or ``None`` (unknown).

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


@dataclass(frozen=True)
class LegMetadata:
    """One venue's instrument metadata **as of one arrival** (F06, contract B).

    The replay applies metadata records in receive order, so this is a snapshot: the
    values a later arrival may use are the ones its own predecessors published. A
    record whose own ``valid`` is false (``invalid_reason="metadata_stale"``) says the
    venue's metadata is not trustworthy *from that arrival on*; the payload it carries
    is the previous, known-to-be-stale one, so **nothing is derived from it** - the
    derived fields go to ``None`` and ``stale`` names why.
    """

    venue: str
    known: bool = False
    stale: bool = False
    fee_bps: Decimal | None = None
    fee_source: Any = None
    size_increment: Decimal | None = None
    size_precision: int | None = None
    price_increment: Decimal | None = None
    tick_size: str | None = None
    version: str | None = None
    available_ns: int | None = None
    arrival_seq: int | None = None
    session_id: str | None = None
    source: str | None = None
    record_valid: bool = True
    invalid_reason: str | None = None

    @property
    def usable(self) -> bool:
        """Whether this metadata may price an arrival at all (the metadata axis)."""
        return self.known and not self.stale


def metadata_state(record: Mapping[str, Any], venue: str) -> LegMetadata:
    """The metadata state one ``instrument`` record leaves behind.

    Read from the record's own ``metadata`` block, never from a scan of the whole
    tape: the caller applies this in receive order, which is what keeps a later
    update (or a later session) from re-pricing an earlier arrival (F06).
    """
    payload = record.get("metadata")
    payload = payload if isinstance(payload, Mapping) else {}
    raw_valid = record.get("valid")
    valid = True if raw_valid is None else bool(raw_valid)
    reason = record.get("invalid_reason")
    reason = reason if isinstance(reason, str) else None
    session = record.get("session_id")
    seq = record.get("arrival_seq")
    origin = record.get("source")
    version = payload.get("metadata_version")
    available = payload.get("metadata_available_ns")
    state = LegMetadata(
        venue=venue,
        known=valid,
        stale=not valid,
        version=version if isinstance(version, str) else None,
        available_ns=available if isinstance(available, int) and not isinstance(
            available, bool) else None,
        arrival_seq=seq if isinstance(seq, int) and not isinstance(seq, bool) else None,
        session_id=session if isinstance(session, str) else None,
        source=origin if isinstance(origin, str) else None,
        record_valid=valid,
        invalid_reason=reason,
    )
    if not valid:
        # The record itself says the metadata is not usable: keep the record-level
        # facts as evidence, derive nothing from the payload it repeats.
        return state
    precision = payload.get("size_precision")
    tick = payload.get("tick_size")
    return dataclasses.replace(
        state,
        fee_bps=fee_bps(payload.get("taker_fee_bps"), payload.get("fee_source")),
        fee_source=payload.get("fee_source"),
        size_increment=positive_decimal(payload.get("size_increment")),
        size_precision=precision if isinstance(precision, int) and not isinstance(
            precision, bool) else None,
        price_increment=positive_decimal(payload.get("price_increment")),
        tick_size=None if tick is None else str(tick),
    )


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


def positive_decimal(value: Any) -> Decimal | None:
    """An exact positive Decimal from a tape payload, or ``None`` (never invented).

    The tape stores decimals as text; this reads that text exactly and refuses
    anything that is not a finite positive number (F08: a step is either the real
    ``size_increment`` or it is unknown - ``10 ** -size_precision`` is never used).
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        dec = Decimal(str(value))
    except (ArithmeticError, TypeError, ValueError):
        return None
    if not dec.is_finite() or dec <= 0:
        return None
    return dec


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

    ``books``/``quotes``/``funding``/``metadata``/``feed_ready``/``market_halted``
    are snapshots: the next step gets its own copies, so a consumer cannot be
    surprised by a later record mutating the step it is holding.

    The last three are the three *axes* F07 separates. A venue that never appeared
    in a status record has no entry at all in ``feed_ready``/``market_halted`` -
    absence means "this tape never said", which is not the same as "ready" and not
    the same as "halted", and the gate never reads an absent entry as an event.
    """

    record: Mapping[str, Any]
    kind: str
    arrival_seq: int | None
    session_id: str | None
    books: Mapping[str, BookState]
    quotes: Mapping[str, QuoteState]
    funding: Mapping[str, FundingState]
    metadata: Mapping[str, LegMetadata]
    feed_ready: Mapping[str, bool]
    market_halted: Mapping[str, bool]
    gap: GapState | None
    gaps_seen: int
    dropped_seen: int

    @property
    def disconnected(self) -> Mapping[str, bool]:
        """The feed axis in the negative: venues whose local feed is known to be down.

        Derived from ``feed_ready`` so the two views cannot disagree; a venue with
        no feed notice is simply absent, exactly as before.
        """
        return {venue: not ready for venue, ready in self.feed_ready.items()}


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
        self.metadata: dict[str, LegMetadata] = {}
        self.feed_ready: dict[str, bool] = {}
        self.market_halted: dict[str, bool] = {}
        self.last_seq: dict[str, int] = {}
        self.gaps = 0
        self.dropped = 0
        self.last_session: str | None = None

    def _new_session(self) -> None:
        """Start a session the way the recorder's own state starts (rule 6).

        A session is one writer's run. Its first records say what *it* saw: the feed
        axis and the market axis begin with the recorder's documented defaults (feed
        up, market trading) and the metadata axis begins unknown, because a session
        that publishes no instrument metadata has not told us the fee or the step -
        it must never inherit another session's, and no later session may re-price it.
        """
        self.metadata.clear()
        self.feed_ready.clear()
        self.market_halted.clear()

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
            self._new_session()
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
            elif kind == INSTRUMENT and isinstance(venue, str) and venue:
                # F06: applied here, in receive order, so the arrivals before it keep
                # the metadata they could see and a later session starts unknown.
                self.metadata[venue] = metadata_state(record, venue)
            elif kind == STATUS and isinstance(venue, str) and venue:
                self._apply_status(venue, record)
            # Any other kind (a later stage's record) is carried by the step's
            # ``record`` and changes no state.
        return ReplayStep(
            record=record,
            kind=kind,
            arrival_seq=seq,
            session_id=session,
            books=dict(self.books),
            quotes=dict(self.quotes),
            funding=dict(self.funding),
            metadata=dict(self.metadata),
            feed_ready=dict(self.feed_ready),
            market_halted=dict(self.market_halted),
            gap=gap,
            gaps_seen=self.gaps,
            dropped_seen=self.dropped,
        )

    def _apply_status(self, venue: str, record: Mapping[str, Any]) -> None:
        """Update the feed axis or the market axis - never both (F07).

        The two travel on one record type and mean opposite things. A reason under
        ``adapter:`` is the adapter talking about its *own local feed*: it moves the
        feed axis and nothing else. Anything else is a real venue market status, and
        it moves the market axis, where only an explicit ``is_trading=True`` lifts a
        halt - the mirror of ``spread_watch._on_market_status``, so the live watcher
        and this replay cannot disagree about a leg.
        """
        reason = record.get("reason")
        reason = reason if isinstance(reason, str) else ""
        if reason == ADAPTER_DISCONNECTED:
            self.feed_ready[venue] = False
            return
        if reason == ADAPTER_SNAPSHOT_READY:
            # A complete new snapshot landed: the feed is usable again. It says
            # nothing about the market, so a venue halt the tape announced survives
            # its own reconnect (F07/F18) - the adapter resolves the market axis with
            # the same rule on the live side.
            self.feed_ready[venue] = True
            return
        if reason.startswith(ADAPTER_REASON_PREFIX):
            # Any other local feed notice (a bare "socket connected", a subscribe ack)
            # is not a recovery and changes nothing.
            return
        action = record.get("action")
        action = str(action) if action is not None else ""
        trading = record.get("is_trading")
        if trading is False or action in NON_TRADING_ACTIONS:
            self.market_halted[venue] = True
        elif trading is True:
            # An explicit resume - the only thing that lifts a real halt.
            self.market_halted[venue] = False
        # is_trading=None with a non-halting action: keep what we had. Readiness is
        # never assumed from a status that does not say "trading".


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
    """One leg's own state as visible at the record being evaluated.

    The three axes are separate fields on purpose (F07): ``disconnected`` is the
    feed, ``market_halted`` the venue's own trading state and ``metadata_known`` /
    ``metadata_stale`` the instrument metadata. A caller that only fills in the book
    gets the historical defaults, which is what a v1 tape describes.
    """

    venue: str
    book: BookState | None
    disconnected: bool = False
    metadata_known: bool = True
    metadata_stale: bool = False
    market_halted: bool = False


@dataclass(frozen=True)
class QualityParams:
    """Plan 5.2's thresholds. Research parameters, printed into every report."""

    max_age_ms: int = DEFAULT_MAX_AGE_MS
    max_skew_ms: int = DEFAULT_MAX_SKEW_MS
    future_tolerance_ms: int = DEFAULT_FUTURE_TOLERANCE_MS
    # F09: ``None`` = measured and reported, never enforced (the default). Set it and
    # the gate adds one check, reported as its own reject reason.
    max_event_age_ms: int | None = DEFAULT_MAX_EVENT_AGE_MS


@dataclass(frozen=True)
class QualityVerdict:
    """The gate's verdict, with the numbers it was decided on.

    ``event_age_ms`` is *reported* by default: the gate's checks are the receive age,
    the future offsets and the skew, and an event age only becomes a check when the
    caller configured ``QualityParams.max_event_age_ms`` (F09 - the age carries the
    unmeasured local clock offset). Either way the ages travel with the verdict, so a
    row always shows the evidence behind its own decision.
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
    """A leg's event age: the local epoch receipt time of *this record* minus its book's
    own venue event time (F09, contract E).

    The left-hand stamp is the record being evaluated, not another venue event. That
    matters: two legs whose feeds were both delayed by the same two minutes arrive
    together, so the difference between their two event stamps is zero - which is a
    measurement of *skew*, not of age, and reading it as age is exactly the defect
    this field replaces. Here both legs report the same ~120000 ms, because both are
    two minutes old, while ``event_skew_ms`` still reports their 0 ms of disagreement.

    A negative value is kept as-is: it says the venue's stamp is ahead of the local
    receipt, i.e. the (unmeasured) clock offset is at least that size. Flooring it to
    zero or taking an absolute value would hide that evidence, so neither happens.
    ``None`` (never ``0``) when either stamp is missing.
    """
    book = leg.book
    if book is None or record.init_ns is None or book.ts_event_ns is None:
        return None
    return _ms_between(record.init_ns, book.ts_event_ns)


def event_age_status(params: QualityParams, ages: Mapping[str, Decimal | None]) -> str:
    """Whether the event-age threshold was checked, and what it said (F09).

    One of ``not_checked`` (no threshold configured - the default, because the local
    clock offset is unmeasured), ``unknown`` (a leg's age is not measurable),
    ``exceeded`` or ``ok``. The value is published on every row so a report can never
    imply that an unconfigured threshold was passed.
    """
    if params.max_event_age_ms is None:
        return "not_checked"
    if any(age is None for age in ages.values()):
        return "unknown"
    limit = Decimal(params.max_event_age_ms)
    return "exceeded" if any(age > limit for age in ages.values()) else "ok"


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
    book, a stale or unknown instrument metadata, a disconnected feed, a halted
    market, empty/one-sided/crossed levels, ages that are not comparable (two
    sessions), a missing receipt time, an exchange timestamp more than
    ``future_tolerance_ms`` in the future, a leg older than ``max_age_ms``, two legs
    whose own event times differ by more than ``max_skew_ms`` and - only when the
    caller set it - an event age over ``max_event_age_ms``. Checks run in that
    documented order and the first failure is the reported reason.

    Each leg's *event* age is measured here too and returned in the verdict. It is
    **not** one of those checks unless the caller configured ``max_event_age_ms``
    (F09: the age carries the unmeasured local clock offset, so enforcing a threshold
    is an explicit decision, and the report says which state applies).
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
        if leg.metadata_stale:
            return verdict(
                REJECT_METADATA_STALE,
                f"{leg.venue}: the tape's last instrument record marked this venue's "
                f"metadata stale, so no fee, step or increment from it may be used "
                f"from that arrival on",
            )
        if not leg.metadata_known:
            return verdict(
                REJECT_METADATA_UNKNOWN,
                f"{leg.venue}: this session's tape carries no instrument metadata for "
                f"the leg, and another session's is never inherited",
            )
        if leg.disconnected:
            return verdict(
                REJECT_DISCONNECTED,
                f"{leg.venue}: the adapter reported the local feed disconnected",
            )
        if leg.market_halted:
            return verdict(
                REJECT_MARKET_HALTED,
                f"{leg.venue}: a venue market status said this market is not trading "
                f"and no explicit resume has lifted it (a feed reconnect does not)",
            )
        reason = _book_reason(book)
        if reason is not None:
            return verdict(
                reason,
                f"{leg.venue}: book is not a two-sided uncrossed depth"
                + (f" ({book.invalid_reason})" if book.invalid_reason else "")
                + (f" - {V1_FEED_INVALIDATED_NOTE}"
                   if book.invalid_reason == V1_FEED_INVALIDATED else ""),
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
        if params.max_event_age_ms is not None:
            event_age = event_ages.get(leg.venue)
            if event_age is not None and event_age > Decimal(params.max_event_age_ms):
                return verdict(
                    REJECT_EVENT_AGE,
                    f"{leg.venue}: the book's venue event is {event_age} ms before this "
                    f"record's local receipt (> the configured "
                    f"{params.max_event_age_ms} ms event-age limit): the age carries the "
                    f"unmeasured local clock offset, which is why this check only runs "
                    f"when the caller asks for it",
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
class MetadataArrival:
    """One instrument record the scan saw, in arrival order (F06).

    The scan's job is to *list* the metadata a tape carried, never to elect one of
    them as "the run's metadata": the replay applies them in receive order, and a
    report that wants a run-level view must say which arrival it is describing.
    """

    venue: str
    arrival_seq: int | None
    session_id: str | None
    source: str | None
    state: LegMetadata


@dataclass(frozen=True)
class TapeScan:
    """What one tape says about itself, read once and reused by the report.

    ``metadata_arrivals`` is a *list*, deliberately: no part of this module may read
    the last one as the whole run's fact (that was F06's defect). Only the run-level
    fee snapshot describes the arrivals, and it names every one of them.
    """

    tape: str
    files: tuple[str, ...]
    fragments: int
    records: int
    first_arrival_seq: int | None
    last_arrival_seq: int | None
    symbol: str | None
    book_venues: tuple[str, ...]
    metadata_arrivals: Mapping[str, tuple[MetadataArrival, ...]]
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

    @property
    def metadata_venues(self) -> tuple[str, ...]:
        """Venues this tape announced instrument metadata for, in first-seen order."""
        return tuple(self.metadata_arrivals)

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
    """Read one tape once for its run-level facts: which venues, which records, what verdict.

    It counts the instrument records and lists them in arrival order. It deliberately
    does **not** keep "the" metadata of the tape: the arrivals are the fact, and only
    the replay (in receive order) may turn them into fees and steps for a given
    moment (F06).
    """
    for fragment in files.fragments:
        if not Path(fragment).is_file():
            raise DepthError(
                f"[ondo-depth] the manifest names a fragment that does not exist: "
                f"{fragment}",
            )
    reader = read_tape(list(files.fragments))
    metadata: dict[str, list[MetadataArrival]] = {}
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
            session = record.get("session_id")
            seq = record.get("arrival_seq")
            origin = record.get("source")
            metadata.setdefault(venue, []).append(MetadataArrival(
                venue=venue,
                arrival_seq=seq if isinstance(seq, int) and not isinstance(seq, bool)
                else None,
                session_id=session if isinstance(session, str) else None,
                source=origin if isinstance(origin, str) else None,
                state=metadata_state(record, venue),
            ))
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
        metadata_arrivals={
            venue: tuple(arrivals) for venue, arrivals in metadata.items()
        },
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
    "common_step", "step_origin", "arrival_seq", "session_id", "ts_utc",
    "sell_vwap", "buy_vwap",
    "reference_price", "fill_usd_est", "gross_entry_bps", "entry_fees_bps",
    "entry_after_fees_bps", "entry_after_fees_and_reserve_bps",
    "exit_fee_assumption_bps", "reserve_bps", "funding_estimate_bps", "funding_status",
    "quality_ok", "cost_qualified", "mapping_verified", "mapping_status",
    "reject_reason", "executable", "receive_age_sell_ms", "receive_age_buy_ms",
    "event_skew_ms", "event_age_sell_ms", "event_age_buy_ms", "event_age_status",
    "clock_offset_unknown", "exit_status", "note",
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
        "why this row is not a usable comparison: a gate reason (recording_gap, "
        "no_book, empty/one_sided/crossed book, metadata_unknown, metadata_stale, "
        "disconnected, market_halted, session_mismatch, unknown_time, "
        "future_event_time, stale_book, event_skew, event_age_exceeded), or fee_unknown "
        "/ insufficient_depth / below_one_step / quantity_step_unknown; empty when it "
        "passed. The three axes of F07 report separately: disconnected is the local "
        "feed, market_halted the venue's trading state, metadata_* the instrument facts"
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
        "both venues can represent the quantity exactly. Each leg's step is its own "
        "instrument's real size_increment as of this arrival, or the caller's CLI "
        "override; a step is never inferred from size_precision"
    ),
    "session_id": (
        "the tape session this row was priced in. Metadata is announced per session: a "
        "session that published no instrument record is metadata_unknown for its whole "
        "span and never inherits another session's fee or step"
    ),
    "step_origin": (
        "where this row's quantity step came from: 'instrument_metadata' (both legs' "
        "own size_increment), 'override' (the caller's --steps declaration, which is "
        "never a statement that the venue verified anything), 'mixed' (one leg each) or "
        "'quantity_step_unknown'"
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
    "event_age_status": (
        "'not_checked' when no event-age threshold is configured (the default: the "
        "local clock offset to either venue is unmeasured, so an age threshold would "
        "turn a clock skew into a market conclusion), 'unknown' when a leg's age is not "
        "measurable, else 'exceeded' / 'ok' against max_event_age_ms. Published on every "
        "row so a report can never imply that an unconfigured threshold was passed"
    ),
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
    session_id: str | None
    ts_utc: str
    quantity: Decimal | None
    common_step: Decimal | None
    step_origin: str
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
    event_age_status: str
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
            "step_origin": self.step_origin,
            "arrival_seq": self.arrival_seq,
            "session_id": self.session_id if self.session_id is not None else UNKNOWN,
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
            "event_age_status": self.event_age_status or UNKNOWN,
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
    # The metadata this bucket's *moments* used, in first-seen order. A bucket is a bag
    # of arrivals, and under F06 the fee and the step are properties of an arrival, not
    # of the tape: the bucket reports a single value only when every row agreed on one,
    # and says so explicitly otherwise (never silently electing the last one).
    fee_values: list[Decimal] = field(default_factory=list)
    step_values: list[Decimal] = field(default_factory=list)
    step_origins: list[str] = field(default_factory=list)
    event_age_rejects: int = 0

    def add(self, row: Row) -> None:
        self.samples += 1
        if row.quality_ok:
            self.quality_pass += 1
        if row.entry_fees_bps is not None and row.entry_fees_bps not in self.fee_values:
            self.fee_values.append(row.entry_fees_bps)
        if row.common_step is not None:
            if row.common_step not in self.step_values:
                self.step_values.append(row.common_step)
            if row.step_origin not in self.step_origins:
                self.step_origins.append(row.step_origin)
        if row.reject_reason == REJECT_EVENT_AGE:
            self.event_age_rejects += 1
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
    """What one tape contributes to the analysis that is *not* per-arrival.

    Only the tape's own shape lives here: which venues published depth, and therefore
    which directions can be compared at all. Fees and steps are per-arrival facts and
    are read from each replayed step's metadata state, never from this object (F06).
    """

    def __init__(self, symbol: str, scan: TapeScan, params: Params) -> None:
        self.symbol = symbol
        self.scan = scan
        self.params = params
        self.directions: list[tuple[str, str]] = [
            (sell, buy) for sell, buy in params.directions
            if sell in scan.book_venues and buy in scan.book_venues
        ]
        self.skipped: list[tuple[str, str]] = [
            (sell, buy) for sell, buy in params.directions
            if (sell, buy) not in self.directions
        ]


def leg_step(
    venue: str, metadata: LegMetadata | None, params: Params,
) -> tuple[Decimal | None, str]:
    """One leg's quantity step and where it came from, as of one arrival.

    The CLI override wins (a caller's explicit research parameter, published as
    ``override`` and never as a venue-verified increment). Otherwise the step is the
    instrument's real ``size_increment`` - or nothing at all: ``size_precision`` is
    never turned into a step (F08, contract D).
    """
    override = params.steps.get(venue)
    if override is not None:
        return Decimal(override), STEP_ORIGIN_OVERRIDE
    if metadata is None or not metadata.usable or metadata.size_increment is None:
        return None, STEP_ORIGIN_UNKNOWN
    return metadata.size_increment, STEP_ORIGIN_METADATA


def _direction_step(
    step: ReplayStep, sell: str, buy: str, params: Params,
) -> tuple[Decimal | None, str]:
    """The common quantity step of a direction at one arrival, and its origin."""
    sell_step, sell_origin = leg_step(sell, step.metadata.get(sell), params)
    buy_step, buy_origin = leg_step(buy, step.metadata.get(buy), params)
    origin = sell_origin if sell_origin == buy_origin else STEP_ORIGIN_MIXED
    return common_step([sell_step, buy_step]), origin


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


def _leg_reading(step: ReplayStep, venue: str) -> LegReading:
    """One leg's three axes as of this arrival - feed, market and metadata, separately."""
    metadata = step.metadata.get(venue)
    return LegReading(
        venue,
        step.books.get(venue),
        disconnected=bool(step.disconnected.get(venue)),
        metadata_known=bool(metadata is not None and metadata.known),
        metadata_stale=bool(metadata is not None and metadata.stale),
        market_halted=bool(step.market_halted.get(venue)),
    )


def _evaluate(
    ctx: _TapeContext, step: ReplayStep, sell: str, buy: str, notional: Decimal,
) -> Row:
    """One comparison at one record: the gate first, then one quantity for both legs."""
    params = ctx.params
    record_time = TimePoint.of_record(step.record)
    sell_book = step.books.get(sell)
    buy_book = step.books.get(buy)
    sell_metadata = step.metadata.get(sell)
    buy_metadata = step.metadata.get(buy)
    legs = (_leg_reading(step, sell), _leg_reading(step, buy))
    verdict = quality_gate(
        record_time, legs, params=params.quality, recording_gap=step.gap is not None,
    )
    reason = verdict.reject_reason
    note = " | ".join(verdict.notes)

    # The fees this *arrival* could see: each leg's own metadata state, in receive
    # order. A later instrument update (or a later session) cannot reach back (F06).
    fee_sell = sell_metadata.fee_bps if sell_metadata is not None else None
    fee_buy = buy_metadata.fee_bps if buy_metadata is not None else None
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
    step_q, step_origin = _direction_step(step, sell, buy, params)
    if step_q is not None and step_origin == STEP_ORIGIN_MIXED:
        # The two legs are sized from different kinds of source (an override on one,
        # the venue's own increment on the other): the LCM is still exact, but the
        # report says so instead of implying both venues verified their step.
        note = (note + " | " if note else "") + (
            "quantity step source is mixed: one leg's step is a CLI override and the "
            "other's is the instrument's own size_increment"
        )

    if verdict.quality_ok and sell_book is not None and buy_book is not None:
        if step_q is None:
            reason = REJECT_STEP_UNKNOWN
            note = (
                "no real size_increment reached this arrival for both legs, so the "
                "nominal comparison's quantity is not known to be representable on "
                "both venues (10**-size_precision is never used as a step)"
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
        step_origin=step_origin,
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
        session_id=step.session_id,
        receive_age_sell_ms=verdict.receive_age_ms.get(sell),
        receive_age_buy_ms=verdict.receive_age_ms.get(buy),
        event_skew_ms=verdict.event_skew_ms,
        event_age_sell_ms=verdict.event_age_ms.get(sell),
        event_age_buy_ms=verdict.event_age_ms.get(buy),
        event_age_status=event_age_status(params.quality, verdict.event_age_ms),
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
    "symbol", "sell_venue", "buy_venue", "notional_usd", "common_step", "step_origin",
    "common_step_note", "samples",
    "quality_pass", "pass", "reject", "hits", "executable_hits", "zero_opportunity",
    "entry_fees_bps", "entry_fees_note", "exit_fee_assumption_bps", "reserve_bps",
    "mapping_verified",
    "executable", "gross_entry_bps_median", "gross_entry_bps_min", "gross_entry_bps_max",
    "entry_after_fees_bps_median", "entry_after_fees_bps_min", "entry_after_fees_bps_max",
    "funding_estimate_bps_median", "funding_estimate_bps_min", "funding_estimate_bps_max",
    "event_age_sell_ms_max", "event_age_buy_ms_max", "max_event_age_ms",
    "event_age_rejects",
    "median_sample", "reject_reasons", "note",
)


def _reason_text(reasons: Mapping[str, int]) -> str:
    ordered = sorted(reasons.items(), key=lambda item: (-item[1], item[0]))
    return ";".join(f"{name}={count}" for name, count in ordered)


def _one_value_text(values: Sequence[Decimal]) -> str:
    """A bucket's single metadata value, or ``unknown`` when its rows did not agree.

    A bucket is a bag of arrivals: under F06 the entry fee and the quantity step
    belong to an arrival, not to the tape. One value means every row that carried one
    carried the same; no value means none did; several mean the tape changed its
    metadata mid-run and the bucket refuses to elect one of them.
    """
    if len(values) == 1:
        return str(values[0])
    return UNKNOWN


def _fee_note(acc: _Acc) -> str:
    if len(acc.fee_values) == 1:
        return ""
    if not acc.fee_values:
        return (
            "no moment this bucket evaluated had both legs' taker fee in this run's "
            "tape, so no entry fee is published for the bucket (each row still carries "
            "the fee of its own moment, and the dated assumption is never substituted)"
        )
    return (
        "the entry fee changed during this tape ("
        + ", ".join(str(value) for value in acc.fee_values)
        + " bps): each row carries the fee of its own arrival, and the bucket does not "
        "elect one of them"
    )


def _step_note(acc: _Acc) -> str:
    unknown_rows = acc.reasons.get(REJECT_STEP_UNKNOWN, 0)
    if len(acc.step_values) > 1:
        return (
            "the quantity step changed during this tape ("
            + ", ".join(str(value) for value in acc.step_values)
            + "): each row carries the step of its own arrival"
        )
    if not acc.step_values:
        if unknown_rows:
            return (
                f"quantity_step_unknown: {unknown_rows} moment(s) passed the quality "
                f"gate but no real size_increment reached both legs, and 10**-"
                f"size_precision is never used as a quantity step"
            )
        return "no moment this bucket evaluated reached a quantity step"
    if acc.step_origins and acc.step_origins[0] == STEP_ORIGIN_OVERRIDE:
        return (
            "the step is the caller's CLI override, published as 'override': it is a "
            "research parameter and never evidence that either venue verified a step"
        )
    return ""


def _acc_dict(acc: _Acc, params: Params) -> dict[str, Any]:
    verified = (acc.sell, acc.buy) in params.mapping_verified
    reject = acc.samples - acc.passed
    return {
        "notional_usd": str(acc.notional),
        "common_step": _one_value_text(acc.step_values),
        "common_step_note": _step_note(acc),
        "step_origin": (
            acc.step_origins[0] if len(acc.step_origins) == 1
            else (STEP_ORIGIN_MIXED if acc.step_origins else STEP_ORIGIN_UNKNOWN)
        ),
        "max_event_age_ms": params.quality.max_event_age_ms,
        "event_age_rejects": acc.event_age_rejects,
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
        "entry_fees_bps": _one_value_text(acc.fee_values),
        "entry_fees_note": _fee_note(acc),
        "exit_fee_assumption_bps": _one_value_text(acc.fee_values),
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
    per tape for its run-level facts and once more for the replay. The two passes are
    deliberately different: the scan counts files, records and venues, while every
    *price* - the fee, the increment, the availability of the metadata - comes from the
    replay's own receive-ordered state, so no later arrival (and no later session) can
    re-price a row that was already computed (F06). Every requested (market,
    direction, notional) bucket exists in the output even when the tape has nothing
    for it: an empty result is a result.
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
            "max_event_age_ms": params.quality.max_event_age_ms,
            "max_event_age_note": (
                "null means the event age is measured and published on every row but "
                "never enforced: the age is measured on the local clock, so it carries "
                "the unmeasured local offset to the venue's clock, and a threshold is a "
                "decision the caller has to make explicitly (--max-event-age-ms)"
            ),
            "reserve_bps": str(params.reserve_bps),
            "funding_basis": "bps_per_hour",
            "exit_status": UNCLOSED,
            "clock_offset_unknown": True,
            "clock_offset_unknown_note": CLOCK_OFFSET_NOTE,
            "event_age_ms_rule": EVENT_AGE_RULE,
            "quantity_reference": "sell_leg_best_bid",
            "common_step_rule": "integer_scaled_lcm_of_real_size_increments",
            "tape_schema_versions": list(TAPE_SCHEMA_VERSIONS),
            "metadata_rule": (
                "instrument metadata is applied in receive order: a row uses the fee, "
                "step and availability of the arrivals at or before its own, so a later "
                "update or a later session never re-prices it; a session that published "
                "no instrument record is metadata_unknown for its whole span"
            ),
            "axes_rule": (
                "feed (adapter:disconnected / adapter:snapshot_ready), market "
                "(is_trading / non-trading actions, lifted only by an explicit resume) "
                "and metadata (known / stale) are three separate states, and each "
                "refusal names the axis that caused it"
            ),
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
            "the event age is measured from the local epoch receipt time of the record "
            "being evaluated, so two legs delayed together by the same feed both report "
            "the full age instead of cancelling out to zero; a negative age is kept as "
            "clock evidence and is never floored to zero",
            "instrument metadata is applied in receive order (F06): a fee or increment "
            "that arrived later never re-prices an earlier arrival, and a bucket whose "
            "moments disagreed reports 'unknown' with a note instead of electing one "
            "value",
            "a quantity step is the instrument's real size_increment (the LCM of the two "
            "legs') or it is quantity_step_unknown: 10**-size_precision is never used as "
            "a step, and a CLI --steps override is published as 'override', never as a "
            "venue-verified increment",
            "the three axes of F07 are separate states: a feed reconnect "
            "(adapter:snapshot_ready) never lifts a venue halt, and a metadata record "
            "marked stale makes that venue's metadata unusable from that arrival on",
            "'unknown' is the one marker for a value this run's tape did not carry; it "
            "is never replaced by a registry default",
        ],
    }


def _fee_snapshot(
    params: Params, tapes_by_symbol: Mapping[str, Sequence[TapeScan]],
) -> dict[str, Any]:
    """Every leg's fee and step as *this run's tape* recorded them, arrival by arrival.

    This is a description of the tape, not a value any row was priced with: a leg whose
    metadata changed mid-run shows every value it announced (``*_values`` and the
    ``arrivals`` list) instead of a single elected one, and a leg whose session never
    announced any is ``instrument_metadata: false`` with an unknown fee and step.
    """
    snapshot: dict[str, Any] = {}
    for symbol in params.symbols:
        symbol_scans = list(tapes_by_symbol.get(symbol, ()))
        entry: dict[str, Any] = {}
        for venue in params.venues:
            arrivals = [
                arrival
                for scan in symbol_scans
                for arrival in scan.metadata_arrivals.get(venue, ())
            ]
            usable = [arrival for arrival in arrivals if arrival.state.usable]
            fees: list[Decimal] = []
            increments: list[Decimal] = []
            for arrival in usable:
                state = arrival.state
                if state.fee_bps is not None and state.fee_bps not in fees:
                    fees.append(state.fee_bps)
                if state.size_increment is not None and state.size_increment not in (
                    increments
                ):
                    increments.append(state.size_increment)
            override = params.steps.get(venue)
            if override is not None:
                step, origin = Decimal(override), STEP_ORIGIN_OVERRIDE
            elif len(increments) == 1:
                step, origin = increments[0], STEP_ORIGIN_METADATA
            else:
                step, origin = None, STEP_ORIGIN_UNKNOWN
            last = usable[-1].state if usable else None
            entry[venue] = {
                # One value only when every usable announcement agreed; otherwise the
                # arrivals themselves are listed below rather than a value being elected
                # (F06: the tape's metadata is a sequence, not a fact).
                "taker_fee_bps": _one_value_text(fees),
                "taker_fee_bps_values": [str(value) for value in fees],
                "fee_source": last.fee_source if last is not None else None,
                "size_increment": _one_value_text(increments),
                "size_increment_values": [str(value) for value in increments],
                "size_precision": last.size_precision if last is not None else None,
                "step": _dec_text(step),
                "step_origin": origin,
                "metadata_version": last.version if last is not None else None,
                "metadata_available_ns": last.available_ns if last is not None else None,
                "instrument_metadata": bool(usable),
                "metadata_records": len(arrivals),
                "metadata_stale_records": len(arrivals) - len(usable),
                "sessions_with_metadata": sorted({
                    arrival.session_id for arrival in arrivals
                    if arrival.session_id is not None
                }),
                "arrivals": [
                    {
                        "arrival_seq": arrival.arrival_seq,
                        "session_id": arrival.session_id,
                        "source": arrival.source,
                        "valid": arrival.state.record_valid,
                        "invalid_reason": arrival.state.invalid_reason,
                        "taker_fee_bps": _dec_text(arrival.state.fee_bps),
                        "size_increment": _dec_text(arrival.state.size_increment),
                    }
                    for arrival in arrivals
                ],
            }
        snapshot[symbol] = entry
    return snapshot


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
                    "step_origin": entry["step_origin"],
                    "common_step_note": entry["common_step_note"],
                    "samples": entry["samples"],
                    "quality_pass": entry["quality_pass"],
                    "pass": entry["pass"],
                    "reject": entry["reject"],
                    "hits": entry["hits"],
                    "executable_hits": entry["executable_hits"],
                    "zero_opportunity": entry["zero_opportunity"],
                    "entry_fees_bps": entry["entry_fees_bps"],
                    "entry_fees_note": entry["entry_fees_note"],
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
                    "max_event_age_ms": entry["max_event_age_ms"],
                    "event_age_rejects": entry["event_age_rejects"],
                    "median_sample": entry["median_sample"],
                    "reject_reasons": _reason_text(entry["reject_reasons"]),
                    "note": entry["note"],
                })
    return rows


def _markdown(document: Mapping[str, Any], summary_rows: Sequence[Mapping[str, Any]]) -> str:
    params = document["research_parameters"]
    event_age_limit = (
        params["max_event_age_ms"] if params["max_event_age_ms"] is not None
        else "not enforced"
    )
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
        f"tolerance {params['future_tolerance_ms']} ms, event age "
        f"{event_age_limit}  |  reserve "
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
    lines += [
        "## Fee snapshot (from this run's tape, never today's registry)", "",
        "`taker bps` is a value only when every usable instrument record in the tape "
        "agreed on one; when the run changed its metadata mid-tape the values it "
        "announced are listed and no single value is elected (F06). `increment` is the "
        "real `size_increment` - `quantity_step_unknown` means the tape carried none and "
        "`10**-size_precision` was therefore not used.", "",
    ]
    lines += [
        "| market | venue | taker bps | fee values | source | increment | step | origin |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for symbol, venues in document["fee_snapshot"].items():
        for venue, entry in venues.items():
            values = ", ".join(entry["taker_fee_bps_values"]) or UNKNOWN
            lines.append(
                f"| {symbol} | {venue} | {entry['taker_fee_bps']} | {values} | "
                f"{entry['fee_source'] or UNKNOWN} | {entry['size_increment']} | "
                f"{entry['step']} | {entry['step_origin'] or UNKNOWN} |",
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
    lines += ["", "## Event age vs receive age (local epoch vs venue clock, per example row)", ""]
    lines.append(
        f"`event_age_sell_ms` / `event_age_buy_ms`: {params['event_age_ms_rule']}. They "
        f"are printed beside the receive ages so a book that is minutes old on the "
        f"venue's clock while its local receipt is fresh is visible instead of passing as "
        f"a fresh quote - and because the age is measured against the record's own local "
        f"receipt, two legs delayed together report their full age instead of cancelling "
        f"out to 0. Each bucket's own table also reports the largest event age it saw on "
        f"each leg (`event_age_sell_ms_max` / `event_age_buy_ms_max`), which no bounded "
        f"sample of example or hit rows can hide, and every row carries "
        f"`event_age_status`: {params['max_event_age_note']}.",
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
            "| market | direction | notional | arrival_seq | session | "
            "receive sell/buy ms | event sell/buy ms | event age status |",
            "|---|---|---|---|---|---|---|---|",
        ]
        for symbol, direction_name, entry, row in age_rows:
            lines.append(
                f"| {symbol} | {direction_name} | ${entry['notional_usd']} | "
                f"{row['arrival_seq']} | {row['session_id']} | "
                f"{row['receive_age_sell_ms']}/"
                f"{row['receive_age_buy_ms']} | {row['event_age_sell_ms']}/"
                f"{row['event_age_buy_ms']} | {row['event_age_status']} |",
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
                 "event_age_sell_ms", "event_age_buy_ms", "event_age_status",
                 "clock_offset_unknown", "common_step", "step_origin", "session_id"):
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
    parser.add_argument("--max-event-age-ms", type=int, default=DEFAULT_MAX_EVENT_AGE_MS,
                        help="reject a leg whose book venue event is older than this "
                             "many ms before the local receipt; unset by default "
                             "because the age carries the unmeasured clock offset")
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
            max_event_age_ms=(
                None if args.max_event_age_ms is None else int(args.max_event_age_ms)
            ),
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


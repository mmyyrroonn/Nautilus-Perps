#!/usr/bin/env python3
"""The standardized multi-leg L2 tape: writer, per-tape/run manifest and reader.

Plan 5.1 is the specification implemented here:

* JSONL, one record per line, every record carrying ``schema_version=1``, ``run_id``,
  ``session_id``, ``arrival_seq``, ``symbol``/``venue``/``instrument_id``,
  ``event_kind``, ``ts_event_ns``/``ts_init_ns``/``recorded_mono_ns``,
  ``bids``/``asks`` as arrays of **decimal strings**, ``coverage_limit``, ``source``,
  ``valid`` and ``invalid_reason``. The book, quote, funding, status and instrument
  metadata kinds share that field set so a reader never has to guess; ``run_start``,
  ``gap`` and ``run_end`` are lifecycle markers.
* All decimals are written as the exact text they were given (``Decimal`` via ``str``,
  a string byte for byte). A price or size that arrives as an ``f64`` is refused: it
  has already lost the precision the tape is supposed to preserve.
* One book record per **fully applied** deltas batch (:class:`BookTape`). The state
  between a batch's ``CLEAR`` and its ``ADD``s is never written as ordinary depth, and
  the ``CLEAR`` itself is never a depth record.
* A bounded in-memory queue (default 4096 records), a flush every second and 128 MiB
  file rotation. When the queue is full the records are dropped **and counted**, and a
  ``gap`` marker naming the missing ``arrival_seq`` range is written: a recording with a
  gap is not complete and cannot be replayed across it. A disk write failure aborts the
  recorder and surfaces a :class:`TapeWriteError` to the caller.
* Replay follows ``arrival_seq`` in receive order. It never sorts by exchange time:
  that would fabricate an ordering that was not visible at the time.
* Corruption: a truncated final line may be ignored and is reported as
  ``truncated_tail``; a bad line anywhere else is a hard failure. A restart appends a
  new session and a new segment without repeating the run header and without hiding a
  previous session's missing ``run_end``.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections import deque
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, TextIO


SCHEMA_VERSION = 1
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"  # the run-level manifest, one per l2 directory
MANIFEST_SUFFIX = ".manifest.json"  # the per-tape manifest, next to its fragments
JSONL_SUFFIX = ".jsonl"
SEGMENT_INFIX = "part"  # <stem>_part0002.jsonl sorts after <stem>.jsonl

QUEUE_MAX = 4096  # records in memory before the writer starts dropping (never unbounded)
FLUSH_SECS = 1.0  # a periodic flush so a dying process loses at most a second
ROTATE_BYTES = 128 * 1024 * 1024

# Record kinds.
BOOK = "book"
QUOTE = "quote"
FUNDING = "funding"
STATUS = "status"
INSTRUMENT = "instrument"
GAP = "gap"  # a marker naming a range of arrival_seq values that was dropped
RUN_START = "run_start"  # the header, written once per fragment set
RUN_END = "run_end"  # written by close(): its absence means the process died
# Lifecycle markers do not consume an arrival_seq: the data numbering starts at 1 for
# the first record a caller writes (the plan's own fixture does exactly that).
MARKER_KINDS = frozenset({GAP, RUN_START, RUN_END})

# Book operation actions (the normalized form of a venue's CLEAR/ADD/UPDATE/DELETE).
CLEAR = "clear"
ADD = "add"
UPDATE = "update"
DELETE = "delete"
BID = "bid"
ASK = "ask"

# The field set every record carries (plan 5.1).
RECORD_FIELDS = (
    "schema_version", "run_id", "session_id", "arrival_seq", "symbol", "venue",
    "instrument_id", "event_kind", "ts_event_ns", "ts_init_ns", "recorded_mono_ns",
    "bids", "asks", "coverage_limit", "source", "valid", "invalid_reason",
)

INVALID_EMPTY_BOOK = "empty_book"
INVALID_ONE_SIDED = "one_sided_book"
INVALID_CROSSED = "crossed_book"


class TapeError(Exception):
    """Base class: every tape failure is a TapeError."""


class TapeSchemaError(TapeError):
    """A record does not carry the schema (version, kind, session, or level shape)."""


class TapeOrderError(TapeError):
    """``arrival_seq`` is not strictly increasing within its session."""


class TapeSessionError(TapeError):
    """A session id reappears after another session: the fragments are mixed up."""


class TapeCorruptionError(TapeError):
    """A line that is not the truncated tail is unreadable."""


class TapeTypeError(TapeError):
    """A value that cannot be stored exactly (an f64, a non-finite decimal, ...)."""


class TapeWriteError(TapeError):
    """The disk write failed: the recorder is aborted and the failure is surfaced."""


# ---------------------------------------------------------------- decimal handling


def decimal_text(value: Any, *, field_name: str = "value") -> str:
    """The exact decimal text of ``value``: never a float round-trip.

    A ``str`` is kept byte for byte (only checked to be a finite decimal), a
    ``Decimal`` goes through ``str`` (so ``Decimal("100.050")`` stays ``"100.050"``)
    and an ``int`` is the integer. A ``float`` is refused: a price or quantity that
    went through f64 has already lost precision and the tape must not pretend
    otherwise (plan 5.1, exact decimals throughout).
    """
    if isinstance(value, bool):
        raise TapeTypeError(f"{field_name}: expected a decimal, got {value!r}")
    if isinstance(value, str):
        text = value.strip()
        try:
            number = Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise TapeTypeError(f"{field_name}: {value!r} is not a decimal") from exc
        if not number.is_finite():
            raise TapeTypeError(f"{field_name}: {value!r} is not finite")
        return text
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TapeTypeError(f"{field_name}: {value!r} is not finite")
        return str(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        raise TapeTypeError(
            f"{field_name}: {value!r} is an f64; prices and sizes must be Decimal or an "
            f"exact decimal string (plan 5.1)",
        )
    # A fixed-point type that renders its own exact text (a Nautilus Price/Quantity
    # renders from its integer + precision, never through f64). Anything whose text is
    # not an exact finite decimal is still refused.
    try:
        text = str(value).strip()
        number = Decimal(text)
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TapeTypeError(
            f"{field_name}: unsupported type {type(value).__name__}",
        ) from exc
    if not number.is_finite():
        raise TapeTypeError(f"{field_name}: {value!r} is not finite")
    return text


def decimal_levels(pairs: Any, *, field_name: str = "levels") -> list[list[str]]:
    """``[[price, size], ...]`` as arrays of exact decimal strings."""
    if isinstance(pairs, (str, bytes)) or not isinstance(pairs, (list, tuple)):
        raise TapeTypeError(f"{field_name}: expected a sequence of [price, size]")
    out: list[list[str]] = []
    for item in pairs:
        if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)):
            raise TapeTypeError(f"{field_name}: expected [price, size], got {item!r}")
        pair = list(item)
        if len(pair) != 2:
            raise TapeTypeError(f"{field_name}: expected [price, size], got {item!r}")
        out.append([
            decimal_text(pair[0], field_name=f"{field_name}[price]"),
            decimal_text(pair[1], field_name=f"{field_name}[size]"),
        ])
    return out


def _int_or_none(value: Any, *, field_name: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise TapeTypeError(f"{field_name}: expected an integer, got {value!r}")
    return int(value)


def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _reject_floats(value: Any, *, path: str = "record") -> None:
    """No f64 anywhere in a record: plan 5.1 keeps the tape exact."""
    if isinstance(value, float):
        raise TapeTypeError(
            f"{path}: {value!r} is an f64; the tape stores exact decimal strings",
        )
    if isinstance(value, dict):
        for key, item in value.items():
            _reject_floats(item, path=f"{path}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_floats(item, path=f"{path}[{index}]")


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return decimal_text(value, field_name="record")
    raise TypeError(f"{type(value).__name__} is not JSON-serialisable in the tape")


def _dump(record: dict) -> str:
    return json.dumps(record, separators=(",", ":"), default=_json_default) + "\n"


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- record builders


def base_event(
    event_kind: str,
    *,
    symbol: str | None = None,
    venue: str | None = None,
    instrument_id: str | None = None,
    source: str = "unknown",
    ts_event_ns: int | None = None,
    ts_init_ns: int | None = None,
    recorded_mono_ns: int | None = None,
    bids: Any = None,
    asks: Any = None,
    coverage_limit: int | None = None,
    valid: bool = True,
    invalid_reason: str | None = None,
    **extra: Any,
) -> dict:
    """A record with every field of plan 5.1's standard tape present.

    ``run_id``, ``session_id`` and ``arrival_seq`` are stamped by
    :class:`TapeWriter`; a builder leaves them ``None`` so the writer owns them.
    """
    event: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": None,
        "session_id": None,
        "arrival_seq": None,
        "symbol": _text_or_none(symbol),
        "venue": _text_or_none(venue),
        "instrument_id": _text_or_none(instrument_id),
        "event_kind": str(event_kind),
        "ts_event_ns": _int_or_none(ts_event_ns, field_name="ts_event_ns"),
        "ts_init_ns": _int_or_none(ts_init_ns, field_name="ts_init_ns"),
        "recorded_mono_ns": _int_or_none(recorded_mono_ns, field_name="recorded_mono_ns"),
        "bids": None if bids is None else decimal_levels(bids, field_name="bids"),
        "asks": None if asks is None else decimal_levels(asks, field_name="asks"),
        "coverage_limit": _int_or_none(coverage_limit, field_name="coverage_limit"),
        "source": str(source),
        "valid": bool(valid),
        "invalid_reason": None if invalid_reason is None else str(invalid_reason),
    }
    event.update(extra)
    return event


def book_event(
    *,
    symbol: str | None,
    venue: str | None,
    instrument_id: str | None,
    bids: Sequence,
    asks: Sequence,
    coverage_limit: int | None = None,
    source: str = "snapshot",
    valid: bool = True,
    invalid_reason: str | None = None,
    ts_event_ns: int | None = None,
    ts_init_ns: int | None = None,
    recorded_mono_ns: int | None = None,
) -> dict:
    """One fully applied book state, as arrays of exact decimal strings."""
    return base_event(
        BOOK, symbol=symbol, venue=venue, instrument_id=instrument_id, source=source,
        ts_event_ns=ts_event_ns, ts_init_ns=ts_init_ns, recorded_mono_ns=recorded_mono_ns,
        bids=bids, asks=asks, coverage_limit=coverage_limit, valid=valid,
        invalid_reason=invalid_reason,
    )


def quote_event(
    *,
    symbol: str | None,
    venue: str | None,
    instrument_id: str | None,
    bid: Any,
    ask: Any,
    bid_size: Any,
    ask_size: Any,
    source: str = "quotes",
    ts_event_ns: int | None = None,
    ts_init_ns: int | None = None,
    recorded_mono_ns: int | None = None,
    valid: bool = True,
    invalid_reason: str | None = None,
) -> dict:
    """A top-of-book update, kept next to the book records for replay ordering."""
    return base_event(
        QUOTE, symbol=symbol, venue=venue, instrument_id=instrument_id, source=source,
        ts_event_ns=ts_event_ns, ts_init_ns=ts_init_ns, recorded_mono_ns=recorded_mono_ns,
        valid=valid, invalid_reason=invalid_reason,
        bid=decimal_text(bid, field_name="bid"),
        ask=decimal_text(ask, field_name="ask"),
        bid_size=decimal_text(bid_size, field_name="bid_size"),
        ask_size=decimal_text(ask_size, field_name="ask_size"),
    )


def funding_event(
    *,
    symbol: str | None,
    venue: str | None,
    instrument_id: str | None,
    rate: Any,
    interval_secs: int | None = None,
    source: str = "funding",
    ts_event_ns: int | None = None,
    ts_init_ns: int | None = None,
    recorded_mono_ns: int | None = None,
    valid: bool = True,
    invalid_reason: str | None = None,
) -> dict:
    """The venue's raw funding rate as delivered: a Decimal text, never a float."""
    return base_event(
        FUNDING, symbol=symbol, venue=venue, instrument_id=instrument_id, source=source,
        ts_event_ns=ts_event_ns, ts_init_ns=ts_init_ns, recorded_mono_ns=recorded_mono_ns,
        valid=valid, invalid_reason=invalid_reason,
        rate=decimal_text(rate, field_name="rate"),
        interval_secs=_int_or_none(interval_secs, field_name="interval_secs"),
    )


def status_event(
    *,
    symbol: str | None,
    venue: str | None,
    instrument_id: str | None,
    action: str,
    reason: str,
    is_trading: bool | None = None,
    is_quoting: bool | None = None,
    source: str = "instrument_status",
    ts_event_ns: int | None = None,
    ts_init_ns: int | None = None,
    recorded_mono_ns: int | None = None,
    valid: bool = True,
    invalid_reason: str | None = None,
) -> dict:
    """A feed/market status event (the local-feed notices and real halts alike)."""
    return base_event(
        STATUS, symbol=symbol, venue=venue, instrument_id=instrument_id, source=source,
        ts_event_ns=ts_event_ns, ts_init_ns=ts_init_ns, recorded_mono_ns=recorded_mono_ns,
        valid=valid, invalid_reason=invalid_reason,
        action=str(action), reason=str(reason),
        is_trading=None if is_trading is None else bool(is_trading),
        is_quoting=None if is_quoting is None else bool(is_quoting),
    )


def instrument_event(
    *,
    symbol: str | None,
    venue: str | None,
    instrument_id: str | None,
    metadata: dict,
    coverage_limit: int | None = None,
    source: str = "instrument_metadata",
    ts_event_ns: int | None = None,
    ts_init_ns: int | None = None,
    recorded_mono_ns: int | None = None,
) -> dict:
    """The normalized instrument metadata a run started from (fee, tick, coverage)."""
    return base_event(
        INSTRUMENT, symbol=symbol, venue=venue, instrument_id=instrument_id,
        source=source, ts_event_ns=ts_event_ns, ts_init_ns=ts_init_ns,
        recorded_mono_ns=recorded_mono_ns, coverage_limit=coverage_limit,
        metadata=dict(metadata),
    )


# ------------------------------------------------------------------ book recording


@dataclass(frozen=True)
class Delta:
    """One normalized book operation: the venue's CLEAR/ADD/UPDATE/DELETE, as text."""

    action: str
    side: str | None = None
    price: Any = None
    size: Any = None

    def __post_init__(self) -> None:
        action = str(self.action).lower()
        if action not in (CLEAR, ADD, UPDATE, DELETE):
            raise TapeTypeError(f"unknown book action {self.action!r}")
        object.__setattr__(self, "action", action)
        if action == CLEAR:
            return
        side = str(self.side).lower()
        if side not in (BID, ASK):
            raise TapeTypeError(f"unknown book side {self.side!r}")
        object.__setattr__(self, "side", side)
        object.__setattr__(self, "price", decimal_text(self.price, field_name="price"))
        object.__setattr__(self, "size", decimal_text(self.size, field_name="size"))


def book_quality(bids: Sequence, asks: Sequence) -> tuple[bool, str | None]:
    """Whether a resulting book is usable depth, and why not when it is not.

    Plan 5.2's gate: an empty or crossed book is not depth. A one-sided book is not
    depth either - a limit-depth snapshot that carries one side replaces *both* sides
    (plan 4.2), so an empty other side means the book is not complete.
    """
    if not bids and not asks:
        return False, INVALID_EMPTY_BOOK
    if not bids or not asks:
        return False, INVALID_ONE_SIDED
    if Decimal(bids[0][0]) >= Decimal(asks[0][0]):
        return False, INVALID_CROSSED
    return True, None


class BookTape:
    """One leg's normalized L2 and the *one record per fully applied batch* rule.

    A batch is applied in full before anything is written: a ``CLEAR`` empties the book,
    then every ADD/UPDATE/DELETE lands, and only then is exactly one book record written
    from the resulting state. The intermediate state between a batch's ``CLEAR`` and its
    ``ADD``s is therefore never visible to a reader as ordinary depth (plan 5.1).

    The book itself is maintained here, from the deltas as they arrive. That keeps the
    record's state deterministic and independent of when the engine updates its own
    cache book, and it is the same state the depth10 fallback replaces (:meth:`replace`).

    **Invariant: two feeds, two states.** A leg can be fed by *both* book sources at once
    (the managed deltas stream, and the on-demand depth10 fallback the watcher engages when
    that stream stays empty). Those are different views of the book - one may be truncated
    to ten levels - so each owns its own state here: the deltas book is written by
    :meth:`apply_batch` alone and the fixed-depth snapshot by :meth:`replace` alone, and a
    record's levels always come from the state of the feed its ``source``/``coverage_limit``
    describe. A deltas record therefore cannot be built on a depth10-truncated book (and
    claim the venue's complete-L2 coverage while holding ten levels), and a depth10 record
    cannot claim to be the deltas book.
    """

    def __init__(
        self,
        writer: TapeWriter,
        *,
        symbol: str | None,
        venue: str | None,
        instrument_id: str | None,
        coverage_limit: int | None = None,
        source: str = "deltas",
    ) -> None:
        self.writer = writer
        self.symbol = symbol
        self.venue = venue
        self.instrument_id = instrument_id
        self.coverage_limit = coverage_limit
        self.source = source
        # The deltas book: Decimal price -> (exact price text, exact size text). Equality and
        # hashing of Decimal are by value, so "100.050" and "100.05" are the same level; the
        # latest text is what gets recorded. Only apply_batch writes it, and every batch
        # ("deltas" / "snapshot") record is built from it.
        self._bids: dict[Decimal, tuple[str, str]] = {}
        self._asks: dict[Decimal, tuple[str, str]] = {}
        # The fixed-depth snapshot: only replace writes it, and only a "depth10" record is
        # built from it. Separate state, so neither feed can contaminate the other's record.
        self._snapshot_bids: dict[Decimal, tuple[str, str]] = {}
        self._snapshot_asks: dict[Decimal, tuple[str, str]] = {}
        self.records = 0

    # ------------------------------------------------------------------ applying

    def apply_batch(
        self,
        deltas: Sequence[Delta],
        *,
        ts_event_ns: int | None = None,
        ts_init_ns: int | None = None,
        recorded_mono_ns: int | None = None,
        source: str | None = None,
        coverage_limit: int | None = None,
        valid: bool | None = None,
        invalid_reason: str | None = None,
    ) -> dict:
        """Apply a whole deltas batch, then write exactly ONE book record for it."""
        ops = list(deltas)
        for op in ops:
            self._apply(op)
        batch_source = source or ("snapshot" if any(
            op.action == CLEAR for op in ops) else self.source)
        bids, asks = self.levels()
        return self._record(
            bids=bids, asks=asks,
            ts_event_ns=ts_event_ns, ts_init_ns=ts_init_ns,
            recorded_mono_ns=recorded_mono_ns, source=batch_source,
            coverage_limit=self.coverage_limit if coverage_limit is None else coverage_limit,
            valid=valid, invalid_reason=invalid_reason,
        )

    def replace(
        self,
        bids: Sequence,
        asks: Sequence,
        *,
        coverage_limit: int = 10,
        source: str = "depth10",
        ts_event_ns: int | None = None,
        ts_init_ns: int | None = None,
        recorded_mono_ns: int | None = None,
        valid: bool | None = None,
        invalid_reason: str | None = None,
    ) -> dict:
        """Replace the whole fixed-depth snapshot, then record it once.

        The deltas book is deliberately untouched: a depth10 snapshot is a *different*, at
        most ten-level view of the leg, so it neither becomes the deltas book nor truncates
        it (see the class invariant).
        """
        self._snapshot_bids = self._levels(bids)
        self._snapshot_asks = self._levels(asks)
        snapshot_bids, snapshot_asks = self.snapshot_levels()
        return self._record(
            bids=snapshot_bids, asks=snapshot_asks,
            ts_event_ns=ts_event_ns, ts_init_ns=ts_init_ns,
            recorded_mono_ns=recorded_mono_ns, source=source,
            coverage_limit=coverage_limit, valid=valid, invalid_reason=invalid_reason,
        )

    @staticmethod
    def _levels(pairs: Sequence) -> dict[Decimal, tuple[str, str]]:
        levels: dict[Decimal, tuple[str, str]] = {}
        for price_text, size_text in decimal_levels(pairs):
            price = Decimal(price_text)
            if Decimal(size_text) == 0:
                continue
            levels[price] = (price_text, size_text)
        return levels

    def _apply(self, op: Delta) -> None:
        if op.action == CLEAR:
            self._bids.clear()
            self._asks.clear()
            return
        side = self._bids if op.side == BID else self._asks
        price = Decimal(op.price)
        if op.action == DELETE or Decimal(op.size) == 0:
            side.pop(price, None)
            return
        side[price] = (op.price, op.size)

    # ------------------------------------------------------------------ recording

    def levels(self) -> tuple[list[list[str]], list[list[str]]]:
        """The deltas book, canonical order (bids best-first, asks best-first)."""
        return (
            [[self._bids[price][0], self._bids[price][1]]
             for price in sorted(self._bids, reverse=True)],
            [[self._asks[price][0], self._asks[price][1]]
             for price in sorted(self._asks)],
        )

    def snapshot_levels(self) -> tuple[list[list[str]], list[list[str]]]:
        """The depth snapshot :meth:`replace` last took, canonical order."""
        return (
            [[self._snapshot_bids[price][0], self._snapshot_bids[price][1]]
             for price in sorted(self._snapshot_bids, reverse=True)],
            [[self._snapshot_asks[price][0], self._snapshot_asks[price][1]]
             for price in sorted(self._snapshot_asks)],
        )

    def _record(
        self,
        *,
        bids: list[list[str]],
        asks: list[list[str]],
        ts_event_ns: int | None,
        ts_init_ns: int | None,
        recorded_mono_ns: int | None,
        source: str,
        coverage_limit: int | None,
        valid: bool | None,
        invalid_reason: str | None,
    ) -> dict:
        if valid is None:
            valid, invalid_reason = book_quality(bids, asks)
        event = book_event(
            symbol=self.symbol, venue=self.venue, instrument_id=self.instrument_id,
            bids=bids, asks=asks, coverage_limit=coverage_limit, source=source,
            valid=valid, invalid_reason=invalid_reason, ts_event_ns=ts_event_ns,
            ts_init_ns=ts_init_ns, recorded_mono_ns=recorded_mono_ns,
        )
        self.writer.write_event(event)
        self.records += 1
        return event


# --------------------------------------------------------------------- segments


def segment_path(base: Path, index: int) -> Path:
    """The path of fragment ``index`` (index 1 is the base path).

    ``l2.jsonl`` -> ``l2_part0002.jsonl``: a plain name sort (which is what an analyzer
    globbing the directory gets) lists the fragments in rotation order.
    """
    base = Path(base)
    if index <= 1:
        return base
    return base.with_name(f"{base.stem}_{SEGMENT_INFIX}{index:04d}{base.suffix}")


def manifest_path_for(base: Path) -> Path:
    base = Path(base)
    return base.with_name(base.stem + MANIFEST_SUFFIX)


def _read_json(path: Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _write_json(path: Path, document: dict) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def read_manifest(path: Path) -> dict:
    """One tape's manifest: its ordered fragments and their session/quality facts."""
    document = _read_json(Path(path))
    if not isinstance(document, dict) or "segments" not in document:
        raise TapeSchemaError(f"{path}: not a tape manifest")
    return document


def _manifest_fragment_paths(document: dict, base: Path) -> list[Path]:
    entries = document.get("segments") or document.get("fragments") or []
    ordered = sorted(entries, key=lambda entry: int(entry.get("segment_index", 1)))
    out: list[Path] = []
    for entry in ordered:
        name = Path(str(entry.get("path", "")))
        out.append(name if name.is_absolute() else base / name)
    return out


def manifest_tape_paths(path: Path) -> list[Path]:
    """Every fragment a manifest names, in rotation order (per-tape or run manifest)."""
    path = Path(path)
    document = _read_json(path)
    if not isinstance(document, dict):
        raise TapeSchemaError(f"{path}: not a manifest")
    if "segments" in document:
        return _manifest_fragment_paths(document, path.parent)
    if "fragments" in document:
        # The run manifest: the fragments of every tape, in the order it recorded them.
        ordered = sorted(
            document["fragments"], key=lambda entry: int(entry.get("order", 0)),
        )
        return [path.parent / str(entry["path"]) for entry in ordered]
    raise TapeSchemaError(f"{path}: not a tape manifest")


# ------------------------------------------------------------------------ writer


class TapeWriter:
    """Append-only JSONL tape writer: bounded queue, 1 s flush, 128 MiB rotation.

    Usable directly or as a context manager::

        with TapeWriter(path, run_id="20260914T000000Z") as writer:
            writer.write_event(event)

    One instance is one *session*. Opening a path that already holds a tape appends a
    new fragment with a new session id, never a second header, and never rewrites what
    a previous session left behind - including the absence of its ``run_end``.
    """

    def __init__(
        self,
        path: Path,
        *,
        run_id: str,
        session_id: str | None = None,
        symbol: str | None = None,
        legs: Sequence[dict] | None = None,
        queue_max: int = QUEUE_MAX,
        flush_secs: float = FLUSH_SECS,
        rotate_bytes: int = ROTATE_BYTES,
        manifest_path: Path | None = None,
        clock=None,
    ) -> None:
        self.path = Path(path)
        self.run_id = str(run_id)
        self.session_id = str(session_id) if session_id else (
            f"{self.run_id}-{uuid.uuid4().hex[:12]}"
        )
        self.symbol = symbol
        self.legs = [dict(leg) for leg in (legs or ())]
        self.queue_max = int(queue_max)
        self.flush_secs = float(flush_secs)
        self.rotate_bytes = int(rotate_bytes)
        self.manifest_path = (
            Path(manifest_path) if manifest_path is not None else manifest_path_for(self.path)
        )
        self._clock = clock or time.monotonic

        # ``(line, arrival_seq, gap_dropped)``: the seq is ``None`` for a lifecycle marker
        # and ``gap_dropped`` is only non-``None`` for a gap marker. The queue carries
        # them so a fragment's own facts (how many data records, which ``arrival_seq``
        # range, how many records a gap marker inside it accounts for) are counted when
        # the line is *written*, not when it was queued: one flush can rotate several
        # times, and a record must never be attributed to a fragment that does not hold
        # it.
        self._queue: deque[tuple[str, int | None, int | None]] = deque()
        self._handle: TextIO | None = None
        self._opened = False
        self._closed = False
        self._failed = False
        self._failure_reason: str | None = None
        self._data_records = 0  # records this writer accepted into the stream
        self._next_seq = 1
        self._first_seq: int | None = None
        self._last_seq: int | None = None
        self._dropped = 0
        self._gaps = 0
        self._pending_gap: dict | None = None
        # Segment (fragment) state.
        self._segment_index = 0
        self._segment_path: Path | None = None
        self._segment_records = 0  # JSONL lines in this fragment
        self._segment_data_records = 0  # data records (markers excluded) in this fragment
        self._segment_bytes = 0
        self._segment_first_seq: int | None = None
        self._segment_last_seq: int | None = None
        self._segment_dropped = 0
        self._segment_gaps = 0
        self._segments: list[dict] = []  # closed fragments, in rotation order
        self._open_entry: dict | None = None
        self._last_flush = self._clock()
        self.started_utc = _now_utc()

    # ------------------------------------------------------------------ lifecycle

    def __enter__(self) -> TapeWriter:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"TapeWriter({self.path!s}, run_id={self.run_id!r}, "
            f"session_id={self.session_id!r}, records={self.records})"
        )

    def open(self) -> None:
        """Create the next fragment and, only for a fresh tape, write the run header."""
        if self._opened:
            return
        if self._failed:
            raise TapeWriteError(self._failure_reason or "tape recorder aborted")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        previous = self._load_previous_segments()
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        if not previous and fresh:
            self._segment_index, self._segment_path = 1, self.path
            header = True
        else:
            # A restart of the same run (the stamp is fixed at process start): a new
            # fragment with a new session, so nothing of the previous session is
            # rewritten and the header is not repeated.
            self._segment_index = max(
                (int(entry["segment_index"]) for entry in previous), default=1) + 1
            self._segment_path = segment_path(self.path, self._segment_index)
            header = False
        self._segments = previous
        self._segment_records = 0
        self._segment_data_records = 0
        self._segment_bytes = 0
        self._segment_first_seq = None
        self._segment_last_seq = None
        self._segment_dropped = 0
        self._segment_gaps = 0
        self._open_segment()
        self._opened = True
        if header:
            self._write_direct(self._header_record())
        self.write_manifest()

    def write_event(self, event: dict) -> None:
        """Validate, stamp and queue one record. Never mutates the caller's event."""
        self.open()
        if self._closed:
            raise TapeWriteError(f"the tape {self.path} is closed")
        if self._failed:
            raise TapeWriteError(self._failure_reason or "tape recorder aborted")
        record = self._stamp(event)
        self._enqueue(record)

    def flush(self) -> None:
        """Write every queued record out, the pending gap marker included."""
        if not self._opened or self._failed:
            return
        while True:
            pending = list(self._queue)
            self._queue.clear()
            if pending:
                self._write_lines(pending)
            if self._pending_gap is not None:
                self._emit_gap()
                continue  # the gap marker is now queued: write it as well
            self._last_flush = self._clock()
            return

    def close(self) -> None:
        """Flush, write the run-end marker, close the fragment and the manifest.

        A recorder that already failed writes no run-end (and cannot): the segment is
        left visibly incomplete rather than falsely closed. A failure *during* close is
        raised, so the caller learns the tape did not end cleanly.
        """
        if self._closed:
            return
        self._closed = True
        if not self._opened:
            return
        if self._failed:
            self._close_handle_best_effort()
            self._finish_segment(closed=False)
            self._write_manifest_best_effort()
            return
        try:
            self.flush()
            if self._pending_gap is not None:
                self._emit_gap()
            self._write_direct(self._run_end_record())
            self._flush_handle()
        except TapeWriteError:
            self._close_handle_best_effort()
            self._finish_segment(closed=False)
            self._write_manifest_best_effort()
            raise
        self._close_handle_best_effort()
        self._finish_segment(closed=True)
        self.write_manifest()

    # -------------------------------------------------------------------- record

    def _stamp(self, event: dict) -> dict:
        if not isinstance(event, dict):
            raise TapeTypeError(f"an event must be a dict, got {type(event).__name__}")
        record = dict(event)  # a copy: the caller's event is never rewritten
        kind = record.get("event_kind")
        if not isinstance(kind, str) or not kind:
            raise TapeSchemaError("an event needs a non-empty event_kind")
        record["schema_version"] = SCHEMA_VERSION
        record["run_id"] = self.run_id
        record["session_id"] = self.session_id
        seq: int | None = None
        if kind not in MARKER_KINDS:
            given = record.get("arrival_seq")
            if given is None:
                seq = self._next_seq
            else:
                seq = _int_or_none(given, field_name="arrival_seq")
                assert seq is not None
                if seq <= 0:
                    raise TapeOrderError(f"arrival_seq {seq} must be positive")
                if seq < self._next_seq:
                    raise TapeOrderError(
                        f"arrival_seq {seq} is behind the next sequence "
                        f"{self._next_seq}: the tape is receive order, so an "
                        f"out-of-order event is refused rather than renumbered",
                    )
            record["arrival_seq"] = seq
        if record.get("recorded_mono_ns") is None:
            record["recorded_mono_ns"] = time.monotonic_ns()
        for key in ("bids", "asks"):
            if record.get(key) is not None:
                record[key] = decimal_levels(record[key], field_name=key)
        _reject_floats(record)
        _dump(record)  # refuse anything that cannot be written before it is queued

        if seq is not None:
            if self._first_seq is not None and seq > self._next_seq:
                # A hole in the receive sequence: name it, never paper over it.
                self._note_gap(seq - self._next_seq, self._next_seq, seq - 1, reason="missing")
            if self._first_seq is None:
                self._first_seq = seq
            # The *fragment's* first/last arrival_seq are not known here: the record is
            # only queued. :meth:`_write_lines` counts them into the fragment the line
            # is actually written into (a flush may rotate between records).
            self._last_seq = seq
            self._next_seq = seq + 1
        return record

    def _enqueue(self, record: dict) -> None:
        seq = record.get("arrival_seq")
        if isinstance(seq, bool) or not isinstance(seq, int):
            seq = None  # a lifecycle marker consumes no arrival_seq
        if self._pending_gap is not None and len(self._queue) < self.queue_max:
            self._emit_gap()
        if len(self._queue) >= self.queue_max:
            # The queue is full: the record is dropped and *counted*, and the range it
            # leaves behind is named by a gap marker. It is never skipped silently.
            if seq is not None:
                self._note_gap(1, seq, seq, reason="queue_full")
            self._dropped += 1
            return
        self._queue.append((_dump(record), seq, None))
        self._data_records += 1
        if self._clock() - self._last_flush >= self.flush_secs:
            self.flush()

    def _note_gap(self, dropped: int, first: int, last: int, *, reason: str) -> None:
        """Remember a dropped range; the marker is written as soon as there is room."""
        if self._pending_gap is None:
            self._pending_gap = {
                "dropped": 0, "missing_from": first, "missing_to": last, "reason": reason,
            }
        gap = self._pending_gap
        gap["dropped"] += dropped
        gap["missing_from"] = min(gap["missing_from"], first)
        gap["missing_to"] = max(gap["missing_to"], last)
        if gap["reason"] != reason:
            gap["reason"] = f"{gap['reason']}+{reason}"

    def _emit_gap(self) -> None:
        """Write the pending gap marker: the range it names is never replayed across.

        The marker is queued, not written here, so the fragment that ends up holding it
        is the fragment whose manifest entry reports the dropped count (the counters move
        in :meth:`_write_lines`, together with the line).
        """
        gap = self._pending_gap
        self._pending_gap = None
        if gap is None:
            return
        self._gaps += 1
        record = base_event(
            GAP, symbol=self.symbol, source="queue", valid=False,
            invalid_reason=f"recording_gap:{gap['reason']}",
            recorded_mono_ns=time.monotonic_ns(),
            dropped=gap["dropped"], missing_from=gap["missing_from"],
            missing_to=gap["missing_to"],
        )
        record["run_id"] = self.run_id
        record["session_id"] = self.session_id
        self._queue.append((_dump(record), None, int(gap["dropped"])))

    def _header_record(self) -> dict:
        record = base_event(
            RUN_START, symbol=self.symbol, source="tape",
            recorded_mono_ns=time.monotonic_ns(),
            session_started_utc=self.started_utc, tape=self.path.name,
            legs=self.legs, queue_max=self.queue_max, flush_secs=self.flush_secs,
            rotate_bytes=self.rotate_bytes,
        )
        record["run_id"] = self.run_id
        record["session_id"] = self.session_id
        return record

    def _run_end_record(self) -> dict:
        complete = not self._failed and self._gaps == 0 and self._dropped == 0
        reason = None if complete else (
            f"{self._dropped} record(s) dropped in {self._gaps} gap(s)"
        )
        record = base_event(
            RUN_END, symbol=self.symbol, source="tape", valid=not complete,
            invalid_reason=reason, recorded_mono_ns=time.monotonic_ns(),
            complete=complete, records=self._data_records, dropped=self._dropped,
            gaps=self._gaps, first_arrival_seq=self._first_seq,
            last_arrival_seq=self._last_seq, segment_index=self._segment_index,
            session_started_utc=self.started_utc,
        )
        record["run_id"] = self.run_id
        record["session_id"] = self.session_id
        return record

    # ------------------------------------------------------------------- the disk

    def _open_segment(self) -> None:
        assert self._segment_path is not None
        try:
            # newline="\n": one record per line, independent of the platform.
            self._handle = self._segment_path.open("a", encoding="utf-8", newline="\n")
        except OSError as exc:
            self._fail(f"cannot open tape fragment {self._segment_path}: {exc!r}")
            raise TapeWriteError(self._failure_reason or str(exc)) from exc

    def _write_lines(self, lines: Sequence[tuple[str, int | None, int | None]]) -> None:
        """Write queued ``(line, arrival_seq, gap_dropped)`` items into the fragment.

        The fragment's own facts are counted here, as each line lands: ``records`` and the
        ``first/last_arrival_seq`` bounds describe the records this fragment *holds*, and
        a gap marker's dropped count lands on the fragment that carries the marker. A
        record queued into one fragment and written into the next (the rotation happens
        inside this loop) is therefore attributed to the one that has it.
        """
        handle = self._handle
        if handle is None:
            raise TapeWriteError(f"the tape fragment {self._segment_path} is not open")
        try:
            for line, seq, gap_dropped in lines:
                handle.write(line)
                self._segment_records += 1
                self._segment_bytes += len(line.encode("utf-8"))
                if gap_dropped is not None:
                    self._segment_gaps += 1
                    self._segment_dropped += gap_dropped
                elif seq is not None:
                    self._segment_data_records += 1
                    if self._segment_first_seq is None:
                        self._segment_first_seq = seq
                    self._segment_last_seq = seq
                if self._segment_bytes >= self.rotate_bytes:
                    # The fragment is full: continue in the next one, numbering intact,
                    # and flush this one on the way out.
                    self._rotate()
                    handle = self._handle
            handle.flush()  # a flushed record is a durable record
        except OSError as exc:
            self._fail(f"disk write failed on {self._segment_path}: {exc!r}")
            raise TapeWriteError(self._failure_reason or str(exc)) from exc

    def _write_direct(self, record: dict) -> None:
        """Write a lifecycle marker immediately, bypassing the queue (queue is drained).

        A direct write failure is deliberate: ``close()`` must be able to surface it.
        """
        handle = self._handle
        if handle is None:
            return
        line = _dump(record)
        try:
            handle.write(line)
            handle.flush()
        except OSError as exc:
            self._fail(f"disk write failed on {self._segment_path}: {exc!r}")
            raise TapeWriteError(self._failure_reason or str(exc)) from exc
        self._segment_records += 1
        self._segment_bytes += len(line.encode("utf-8"))

    def _flush_handle(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            handle.flush()
        except OSError as exc:
            self._fail(f"disk flush failed on {self._segment_path}: {exc!r}")
            raise TapeWriteError(self._failure_reason or str(exc)) from exc

    def _close_handle_best_effort(self) -> None:
        handle, self._handle = self._handle, None
        if handle is None:
            return
        try:
            handle.flush()
            handle.close()
        except OSError:
            pass  # a handle that already failed must not raise a second time

    def _fail(self, reason: str) -> None:
        if not self._failed:
            self._failure_reason = reason
        self._failed = True

    def _rotate(self) -> None:
        """Close the full fragment and continue in the next one, numbering intact."""
        self._finish_segment(closed=True)
        self._segment_index += 1
        self._segment_path = segment_path(self.path, self._segment_index)
        self._segment_records = 0
        self._segment_data_records = 0
        self._segment_bytes = 0
        self._segment_first_seq = None
        self._segment_last_seq = None
        self._segment_dropped = 0
        self._segment_gaps = 0
        self._open_segment()
        self.write_manifest()

    def _segment_entry(self, *, closed: bool) -> dict:
        return {
            "segment_index": self._segment_index,
            "path": self._segment_path.name if self._segment_path else None,
            "session_id": self.session_id,
            # lines: every JSONL line in the fragment (header/run-end markers included);
            # records: the data records only. An analyzer sizing a fragment wants the
            # first; a completeness check wants the second.
            "lines": self._segment_records,
            "records": self._segment_data_records,
            "first_arrival_seq": self._segment_first_seq,
            "last_arrival_seq": self._segment_last_seq,
            "dropped": self._segment_dropped,
            "gaps": self._segment_gaps,
            "bytes": self._segment_bytes,
            "closed": closed,
        }

    def _finish_segment(self, *, closed: bool) -> None:
        self._close_handle_best_effort()
        self._open_entry = self._segment_entry(closed=closed)
        self._segments.append(self._open_entry)

    def _load_previous_segments(self) -> list[dict]:
        try:
            document = _read_json(self.manifest_path)
        except (OSError, ValueError):
            return []
        entries = document.get("segments") if isinstance(document, dict) else None
        if not isinstance(entries, list):
            return []
        return [dict(entry) for entry in entries if isinstance(entry, dict)]

    def manifest_entries(self) -> list[dict]:
        entries = list(self._segments)
        if not self._closed and self._segment_path is not None:
            entries = entries + [self._segment_entry(closed=False)]
        return entries

    def write_manifest(self) -> Path:
        document = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": self.run_id,
            "tape": self.path.name,
            "session_id": self.session_id,
            "updated_utc": _now_utc(),
            "segments": self.manifest_entries(),
        }
        return _write_json(self.manifest_path, document)

    def _write_manifest_best_effort(self) -> None:
        try:
            self.write_manifest()
        except OSError:
            pass

    # ------------------------------------------------------------------ reporting

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    @property
    def records(self) -> int:
        """Records this writer accepted into the stream (lifecycle markers excluded)."""
        return self._data_records

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def gaps(self) -> int:
        return self._gaps

    @property
    def next_arrival_seq(self) -> int:
        return self._next_seq

    @property
    def fragments(self) -> list[Path]:
        return [
            (self.path.parent / entry["path"]) for entry in self.manifest_entries()
            if entry.get("path")
        ]

    @property
    def complete(self) -> bool:
        return not self._failed and self._gaps == 0 and self._dropped == 0


# ------------------------------------------------------------------------ reader


@dataclass
class TapeSessionStatus:
    """What the reader learned about one session of one tape."""

    session_id: str
    run_id: str | None = None
    records: int = 0
    first_arrival_seq: int | None = None
    last_arrival_seq: int | None = None
    complete: bool = False
    reason: str | None = None
    dropped: int = 0
    gaps: int = 0


@dataclass
class TapeStatus:
    """The reader's verdict for the fragments it was asked to read."""

    paths: list[Path] = field(default_factory=list)
    fragments: list[Path] = field(default_factory=list)
    records: int = 0
    sessions: list[TapeSessionStatus] = field(default_factory=list)
    gaps: int = 0
    dropped: int = 0
    truncated_tail: bool = False
    truncated_at: str | None = None
    run_ids: list[str] = field(default_factory=list)
    _current: TapeSessionStatus | None = None

    @property
    def complete(self) -> bool:
        """Only a tape whose every session ended with a complete run-end is complete."""
        return bool(self.sessions) and all(session.complete for session in self.sessions)

    @property
    def failed(self) -> bool:
        return not self.complete or self.gaps > 0 or self.dropped > 0

    @property
    def failure_reason(self) -> str | None:
        if self.gaps or self.dropped:
            return (
                f"recording_gap: {self.dropped} record(s) dropped in {self.gaps} gap(s); "
                f"a replay cannot cross a gap"
            )
        if not self.sessions:
            return "no records"
        for session in self.sessions:
            if not session.complete:
                return f"incomplete session {session.session_id}: {session.reason}"
        return None


def _lines_with_last(path: Path) -> Iterator[tuple[str, bool]]:
    """Yield ``(line, is_last)`` with a one-line lookahead: never the whole file."""
    with Path(path).open("r", encoding="utf-8") as handle:
        previous: str | None = None
        for line in handle:
            if previous is not None:
                yield previous, False
            previous = line
        if previous is not None:
            yield previous, True


def expand_paths(paths: Sequence[Path]) -> list[Path]:
    """Directories expand to their fragments, manifests to the fragments they name."""
    out: list[Path] = []
    for raw in paths:
        path = Path(raw)
        if path.is_dir():
            out.extend(sorted(path.glob(f"*{JSONL_SUFFIX}")))
        elif path.name.endswith(MANIFEST_SUFFIX) or path.name == MANIFEST_NAME:
            out.extend(manifest_tape_paths(path))
        else:
            out.append(path)
    return out


def _validate_levels(value: Any, *, where: str, field_name: str) -> None:
    if not isinstance(value, list):
        raise TapeSchemaError(f"{where}: {field_name} must be an array of [price, size]")
    for item in value:
        if not isinstance(item, list) or len(item) != 2:
            raise TapeSchemaError(
                f"{where}: {field_name} entries must be [price, size] arrays, got {item!r}",
            )
        for text in item:
            if not isinstance(text, str):
                raise TapeSchemaError(
                    f"{where}: {field_name} prices and sizes must be decimal strings, "
                    f"got {text!r} (an f64 in the tape is a lost precision)",
                )
            try:
                number = Decimal(text)
            except (InvalidOperation, ValueError) as exc:
                raise TapeSchemaError(f"{where}: {field_name} {text!r} is not a decimal") from exc
            if not number.is_finite():
                raise TapeSchemaError(f"{where}: {field_name} {text!r} is not finite")


class TapeReader(Iterator[dict]):
    """Validating iterator over one or more tape fragments (receive order).

    ``status`` (available while and after iterating) carries the verdict: which sessions
    were seen, whether each ended with a complete run-end, how many records were dropped
    into gaps, and whether the file's last line was a truncated tail.
    """

    def __init__(self, paths: Sequence[Path]) -> None:
        self.status = TapeStatus()
        self.status.fragments = expand_paths(paths)
        self.status.paths = list(self.status.fragments)
        self._lines: Iterator[tuple[str, bool]] | None = None
        self._path: Path | None = None
        self._lineno = 0
        self._index = -1
        self._done = False
        self._finished = False

    def __iter__(self) -> TapeReader:
        return self

    def __next__(self) -> dict:
        while True:
            if self._done:
                self._finish()
                raise StopIteration
            if self._lines is None:
                if not self._next_fragment():
                    self._finish()
                    raise StopIteration
                continue
            try:
                line, is_last = next(self._lines)
            except StopIteration:
                self._lines = None
                continue
            self._lineno += 1
            record = self._read_line(line, is_last)
            if record is None:
                continue
            self._validate(record)
            self._track(record)
            self.status.records += 1
            return record

    # ----------------------------------------------------------------- fragments

    def _next_fragment(self) -> bool:
        self._index += 1
        if self._index >= len(self.status.fragments):
            return False
        self._path = self.status.fragments[self._index]
        self._lineno = 0
        self._lines = _lines_with_last(self._path)
        return True

    def _finish(self) -> None:
        if self._finished:
            return
        self._finished = True
        for session in self.status.sessions:
            if not session.complete and session.reason is None:
                session.reason = "no_run_end"

    # -------------------------------------------------------------------- parsing

    def _read_line(self, line: str, is_last: bool) -> dict | None:
        where = f"{self._path}:{self._lineno}"
        text = line[:-1] if line.endswith("\n") else line
        if not text.strip():
            return None
        cut = is_last and not line.endswith("\n")
        try:
            record = json.loads(text)
        except ValueError as exc:
            if cut:
                self._truncated_tail(where)
                return None
            raise TapeCorruptionError(f"{where}: not valid JSON: {exc}") from exc
        if not isinstance(record, dict):
            if cut:
                self._truncated_tail(where)
                return None
            raise TapeCorruptionError(f"{where}: the record is not a JSON object")
        return record

    def _truncated_tail(self, where: str) -> None:
        """The file's last line was cut short: reported, not fatal (plan 5.1)."""
        self.status.truncated_tail = True
        self.status.truncated_at = where
        self._done = True  # nothing meaningful can follow a cut line

    # ----------------------------------------------------------------- validation

    def _validate(self, record: dict) -> None:
        where = f"{self._path}:{self._lineno}"
        version = record.get("schema_version")
        if version != SCHEMA_VERSION:
            raise TapeSchemaError(
                f"{where}: unsupported schema_version {version!r} "
                f"(expected {SCHEMA_VERSION})",
            )
        kind = record.get("event_kind")
        if not isinstance(kind, str) or not kind:
            raise TapeSchemaError(f"{where}: event_kind must be a non-empty string")
        run_id = record.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise TapeSchemaError(f"{where}: run_id is required")
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise TapeSchemaError(f"{where}: session_id is required")
        session = self._session(session_id, run_id, where)
        if kind not in MARKER_KINDS:
            seq = record.get("arrival_seq")
            if isinstance(seq, bool) or not isinstance(seq, int):
                raise TapeSchemaError(f"{where}: arrival_seq must be an integer")
            if seq <= 0:
                raise TapeSchemaError(f"{where}: arrival_seq {seq} must be positive")
            if session.last_arrival_seq is not None and seq <= session.last_arrival_seq:
                raise TapeOrderError(
                    f"{where}: arrival_seq {seq} is not after "
                    f"{session.last_arrival_seq} in session {session_id!r}: the tape is "
                    f"replayed in receive order, never sorted by exchange time",
                )
            if session.first_arrival_seq is None:
                session.first_arrival_seq = seq
            session.last_arrival_seq = seq
        for key in ("bids", "asks"):
            if record.get(key) is not None:
                _validate_levels(record[key], where=where, field_name=key)

    def _session(self, session_id: str, run_id: str, where: str) -> TapeSessionStatus:
        current = self.status._current
        if current is None or current.session_id != session_id:
            if any(session.session_id == session_id for session in self.status.sessions):
                raise TapeSessionError(
                    f"{where}: session id {session_id!r} reappears after another session: "
                    f"the fragments of two runs (or two writers) are mixed up",
                )
            current = TapeSessionStatus(session_id=session_id, run_id=run_id)
            self.status.sessions.append(current)
            self.status._current = current
            if run_id not in self.status.run_ids:
                self.status.run_ids.append(run_id)
        elif current.run_id != run_id:
            raise TapeSchemaError(
                f"{where}: session {session_id!r} mixes run ids "
                f"{current.run_id!r} and {run_id!r}",
            )
        return current

    def _track(self, record: dict) -> None:
        kind = record["event_kind"]
        session = self.status._current
        assert session is not None
        session.records += 1
        if kind == GAP:
            dropped = record.get("dropped")
            if isinstance(dropped, bool) or not isinstance(dropped, int) or dropped < 0:
                dropped = 0
            self.status.gaps += 1
            self.status.dropped += dropped
            session.gaps += 1
            session.dropped += dropped
        elif kind == RUN_END:
            complete = record.get("complete")
            if not isinstance(complete, bool):
                raise TapeSchemaError(
                    f"{self._path}:{self._lineno}: a run-end record needs a boolean "
                    f"'complete'",
                )
            if complete and session.gaps == 0 and session.dropped == 0:
                session.complete = True
                session.reason = None
            else:
                session.complete = False
                session.reason = (
                    f"run_end_incomplete: {session.dropped} record(s) dropped in "
                    f"{session.gaps} gap(s)"
                )


def read_tape(paths: Sequence[Path]) -> TapeReader:
    """Read tape fragments in receive order, validating schema and ``arrival_seq``.

    Accepts files, directories (every ``*.jsonl``, sorted - which is rotation order) and
    manifests (the fragments they name, in order). Never uses pickle: a tape is JSONL,
    so a hostile file can at worst be invalid JSON. Raises :class:`TapeError` subclasses
    on corruption, an out-of-order or repeated ``arrival_seq``, a repeated session id or
    an unsupported schema version; a truncated final line is reported instead.
    """
    return TapeReader(paths)


def read_tape_status(paths: Sequence[Path]) -> tuple[list[dict], TapeStatus]:
    """Every record, plus the status: the convenience form for an analyzer."""
    reader = read_tape(paths)
    records = list(reader)
    return records, reader.status


def write_run_manifest(l2_dir: Path) -> Path | None:
    """Aggregate the run's per-tape manifests so an analyzer reads fragments in order.

    ``None`` when nothing was recorded: a manifest must never claim a tape that does
    not exist.
    """
    l2_dir = Path(l2_dir)
    manifest_files = sorted(
        path for path in l2_dir.glob(f"*{MANIFEST_SUFFIX}") if path.name != MANIFEST_NAME
    ) if l2_dir.is_dir() else []
    if not manifest_files:
        return None
    fragments: list[dict] = []
    tapes: list[dict] = []
    for manifest_file in manifest_files:
        document = read_manifest(manifest_file)
        entries = sorted(
            document.get("segments", []), key=lambda entry: int(entry.get("segment_index", 1)),
        )
        for entry in entries:
            fragments.append({
                "order": len(fragments) + 1,
                "tape": document.get("tape"),
                "manifest": manifest_file.name,
                "run_id": document.get("run_id"),
                "session_id": entry.get("session_id"),
                "segment_index": int(entry.get("segment_index", 1)),
                "path": entry.get("path"),
                "lines": entry.get("lines"),
                "records": entry.get("records"),
                "first_arrival_seq": entry.get("first_arrival_seq"),
                "last_arrival_seq": entry.get("last_arrival_seq"),
                "dropped": entry.get("dropped"),
                "gaps": entry.get("gaps"),
                "closed": entry.get("closed"),
            })
        tapes.append({
            "tape": document.get("tape"),
            "run_id": document.get("run_id"),
            "manifest": manifest_file.name,
            "segments": len(entries),
            "dropped": sum(int(entry.get("dropped") or 0) for entry in entries),
            "gaps": sum(int(entry.get("gaps") or 0) for entry in entries),
            "complete": all(bool(entry.get("closed")) for entry in entries) and not any(
                int(entry.get("dropped") or 0) for entry in entries
            ),
        })
    document = {
        "schema_version": MANIFEST_SCHEMA_VERSION,
        "created_utc": _now_utc(),
        "l2_dir": str(l2_dir),
        "fragments": fragments,
        "tapes": tapes,
    }
    return _write_json(l2_dir / MANIFEST_NAME, document)


def read_run_manifest(l2_dir: Path) -> dict:
    """The run manifest of an l2 directory (the ordered fragment list)."""
    l2_dir = Path(l2_dir)
    document = _read_json(l2_dir / MANIFEST_NAME)
    if not isinstance(document, dict) or "fragments" not in document:
        raise TapeSchemaError(f"{l2_dir / MANIFEST_NAME}: not a run manifest")
    return document

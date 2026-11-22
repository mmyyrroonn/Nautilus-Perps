"""Low-overhead asynchronous L2 market-data tape recorder for paper runs.

Producer callbacks enqueue immutable ``BookSnapshot`` / ``MarketMetadata``
references onto a bounded deque without performing decimal formatting, JSON
serialization, or disk I/O. A dedicated non-daemon writer thread batches queued
events into schema-2 JSONL segments (gzip-compressed by default) and writes
``manifest.json`` atomically on segment rotation and shutdown.
"""
from __future__ import annotations

from collections import deque
from collections.abc import Iterator, Sequence
from datetime import datetime, timezone
from decimal import Decimal
import gzip
import json
import os
from pathlib import Path
import threading
import time
from typing import Any, IO
import uuid


SCHEMA_VERSION = 2
MANIFEST_SCHEMA_VERSION = 1
MANIFEST_NAME = "manifest.json"
DEFAULT_QUEUE_MAX = 65536
DEFAULT_ROTATE_BYTES = 128 * 1024 * 1024
DEFAULT_FLUSH_SECS = 1.0
DEFAULT_BATCH_SIZE = 512


def _now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _dec_str(value: Any) -> str:
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"non-finite Decimal in tape: {value!r}")
        return str(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return str(value)
    raise TypeError(f"tape requires Decimal or int, got {type(value).__name__}")


def _opt_dec_str(value: Any) -> str | None:
    return None if value is None else _dec_str(value)


def _levels_json(levels: Sequence[tuple[Any, Any]]) -> list[list[str]]:
    return [[_dec_str(price), _dec_str(size)] for price, size in levels]


def _dump_line(record: dict[str, Any]) -> str:
    return json.dumps(record, separators=(",", ":")) + "\n"


def _write_json_atomic(path: Path, document: dict[str, Any]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def _segment_filename(index: int, *, compress: bool) -> str:
    suffix = ".jsonl.gz" if compress else ".jsonl"
    return f"tape_{index:04d}{suffix}"


def _open_fragment(path: Path, *, compress: bool, compresslevel: int) -> IO[str]:
    if compress:
        return gzip.open(
            path, mode="wt", encoding="utf-8", compresslevel=compresslevel, newline="\n",
        )
    return path.open("w", encoding="utf-8", newline="\n")


def _base_record(
    event_kind: str,
    *,
    run_id: str,
    session_id: str,
    arrival_seq: int | None,
    symbol: str | None,
    venue: str | None,
    instrument_id: str | None,
    source: str,
    ts_event_ns: int | None,
    ts_init_ns: int | None,
    recorded_mono_ns: int,
    valid: bool = True,
    invalid_reason: str | None = None,
    bids: list[list[str]] | None = None,
    asks: list[list[str]] | None = None,
    coverage_limit: int | None = None,
    **extra: Any,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "session_id": session_id,
        "arrival_seq": arrival_seq,
        "symbol": symbol,
        "venue": venue,
        "instrument_id": instrument_id,
        "event_kind": event_kind,
        "ts_event_ns": ts_event_ns,
        "ts_init_ns": ts_init_ns,
        "recorded_mono_ns": recorded_mono_ns,
        "bids": bids,
        "asks": asks,
        "coverage_limit": coverage_limit,
        "source": source,
        "valid": bool(valid),
        "invalid_reason": invalid_reason,
    }
    if extra:
        record.update(extra)
    return record


class OpportunityTapeRecorder:
    """Bounded-queue, streaming-gzip market-data tape recorder."""

    def __init__(
        self,
        directory: Path | str,
        *,
        run_id: str | None = None,
        session_id: str | None = None,
        legs: Sequence[dict[str, Any]] | None = None,
        evaluation_interval_ms: int | None = None,
        queue_max: int = DEFAULT_QUEUE_MAX,
        rotate_bytes: int = DEFAULT_ROTATE_BYTES,
        flush_secs: float = DEFAULT_FLUSH_SECS,
        compress: bool = True,
        compresslevel: int = 1,
        batch_size: int = DEFAULT_BATCH_SIZE,
    ) -> None:
        if queue_max <= 0:
            raise ValueError("queue_max must be positive")
        if rotate_bytes <= 0:
            raise ValueError("rotate_bytes must be positive")
        if flush_secs <= 0:
            raise ValueError("flush_secs must be positive")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.directory = Path(directory)
        self.run_id = run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        self.session_id = session_id or f"{self.run_id}-{uuid.uuid4().hex[:12]}"
        self.legs = [dict(leg) for leg in (legs or ())]
        self.evaluation_interval_ms = evaluation_interval_ms
        self.queue_max = int(queue_max)
        self.rotate_bytes = int(rotate_bytes)
        self.flush_secs = float(flush_secs)
        self.compress = bool(compress)
        self.compresslevel = int(compresslevel)
        self.batch_size = int(batch_size)
        self.manifest_path = self.directory / MANIFEST_NAME
        self.started_utc = _now_utc()

        self._lock = threading.Lock()
        self._cond = threading.Condition(self._lock)
        self._queue: deque[tuple[Any, ...]] = deque()

        self._opened = False
        self._closing = False
        self._closed = False
        self._failed = False
        self._failure_reason: str | None = None

        self._next_seq = 1
        self._first_seq: int | None = None
        self._last_seq: int | None = None
        self._accepted_records = 0
        self._dropped = 0
        self._gaps = 0
        self._pending_gap: list[Any] | None = None
        self._final_gap: list[Any] | None = None
        self._queue_high_water = 0

        self._worker: threading.Thread | None = None
        self._handle: IO[str] | None = None
        self._segment_index = 0
        self._segment_path: Path | None = None
        self._segment_records = 0
        self._segment_data_records = 0
        self._segment_uncompressed_bytes = 0
        self._segment_first_seq: int | None = None
        self._segment_last_seq: int | None = None
        self._segment_dropped = 0
        self._segment_gaps = 0
        self._segments: list[dict[str, Any]] = []
        self._total_uncompressed_bytes = 0
        self._total_compressed_bytes = 0
        self._last_flush = time.monotonic()

    def __enter__(self) -> OpportunityTapeRecorder:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> bool:
        self.close()
        return False

    def open(self) -> None:
        with self._lock:
            if self._opened:
                return
            if self._closed:
                raise RuntimeError("cannot reopen a closed tape recorder")
            self.directory.mkdir(parents=True, exist_ok=True)
            self._open_next_segment_locked(write_header=True)
            self._write_manifest_locked()
            self._opened = True
            self._worker = threading.Thread(
                target=self._run_writer,
                name=f"opportunity-tape-{self.session_id[:8]}",
                daemon=False,
            )
            self._worker.start()

    def close(self) -> None:
        worker: threading.Thread | None
        with self._lock:
            if self._closed:
                return
            if not self._opened:
                self._closed = True
                return
            if not self._closing:
                self._closing = True
                if self._pending_gap is not None:
                    self._final_gap = self._pending_gap
                    self._pending_gap = None
                self._cond.notify_all()
            worker = self._worker
        if worker is not None:
            worker.join()
        with self._lock:
            self._closed = True
            self._worker = None

    # ---------------------------------------------------------------- producer

    def record_instrument(
        self,
        market,
        metadata,
        ts_ns: int,
        *,
        price_increment=None,
        source: str = "instrument_metadata",
    ) -> None:
        self._enqueue_item("instrument", (market, metadata, int(ts_ns), price_increment, source))

    def record_book(self, market, snapshot, published_ns: int, *, sequence: int | None = None) -> None:
        self._enqueue_item("book", (market, snapshot, int(published_ns), sequence))

    def record_invalidation(
        self,
        market,
        ts_ns: int,
        *,
        reason: str = "invalidated",
        metadata_invalid: bool = False,
    ) -> None:
        self._enqueue_item("status", (market, int(ts_ns), str(reason), bool(metadata_invalid)))

    def _enqueue_item(self, kind: str, payload: tuple[Any, ...]) -> None:
        mono_ns = time.monotonic_ns()
        with self._lock:
            if not self._opened or self._closing or self._closed or self._failed:
                return
            seq = self._next_seq
            self._next_seq = seq + 1
            if len(self._queue) >= self.queue_max:
                self._dropped += 1
                if self._pending_gap is None:
                    self._pending_gap = [1, seq, seq, "queue_full"]
                else:
                    gap = self._pending_gap
                    gap[0] += 1
                    if seq < gap[1]:
                        gap[1] = seq
                    if seq > gap[2]:
                        gap[2] = seq
                return
            gap = self._pending_gap
            self._pending_gap = None
            if self._first_seq is None:
                self._first_seq = seq
            self._last_seq = seq
            self._accepted_records += 1
            self._queue.append((kind, seq, mono_ns, payload, gap))
            qlen = len(self._queue)
            if qlen > self._queue_high_water:
                self._queue_high_water = qlen
            if qlen == 1 or qlen >= self.batch_size:
                self._cond.notify()

    # ------------------------------------------------------------------ writer

    def _run_writer(self) -> None:
        while True:
            batch: list[tuple[Any, ...]] = []
            closing = False
            final_gap: list[Any] | None = None
            with self._lock:
                while not self._queue and not self._closing and not self._failed:
                    wait_secs = max(0.05, min(0.5, self.flush_secs - (time.monotonic() - self._last_flush)))
                    self._cond.wait(timeout=wait_secs)
                    if not self._queue and not self._closing and (time.monotonic() - self._last_flush) >= self.flush_secs:
                        break
                if self._failed:
                    self._queue.clear()
                    break
                count = min(self.batch_size, len(self._queue))
                for _ in range(count):
                    batch.append(self._queue.popleft())
                closing = self._closing and not self._queue
                if closing:
                    final_gap = self._final_gap
                    self._final_gap = None

            if batch:
                try:
                    self._process_batch(batch)
                except OSError as exc:
                    self._abort_on_error(f"disk write failed on {self._segment_path}: {exc!r}")
                    break
                except Exception as exc:
                    self._abort_on_error(f"tape serialization failed: {type(exc).__name__}: {exc}")
                    break

            now = time.monotonic()
            if not closing and (now - self._last_flush) >= self.flush_secs:
                try:
                    if self._handle is not None:
                        self._handle.flush()
                    self._last_flush = now
                except OSError as exc:
                    self._abort_on_error(f"disk flush failed on {self._segment_path}: {exc!r}")
                    break

            if closing:
                try:
                    if final_gap is not None:
                        self._write_gap(final_gap, time.monotonic_ns())
                    self._write_run_end()
                except OSError as exc:
                    self._abort_on_error(f"disk close failed on {self._segment_path}: {exc!r}")
                break

    def _process_batch(self, batch: Sequence[tuple[Any, ...]]) -> None:
        for kind, seq, mono_ns, payload, gap in batch:
            if gap is not None:
                self._write_gap(gap, mono_ns)
            if kind == "book":
                market, snapshot, published_ns, sequence = payload
                valid = bool(snapshot.valid)
                record = _base_record(
                    "book",
                    run_id=self.run_id,
                    session_id=self.session_id,
                    arrival_seq=seq,
                    symbol=market.symbol,
                    venue=market.venue,
                    instrument_id=market.instrument_id,
                    source="published_book",
                    ts_event_ns=snapshot.ts_event_ns,
                    ts_init_ns=snapshot.ts_received_ns,
                    recorded_mono_ns=mono_ns,
                    valid=valid,
                    invalid_reason=None if valid else "book_marked_invalid",
                    bids=_levels_json(snapshot.bids),
                    asks=_levels_json(snapshot.asks),
                    coverage_limit=snapshot.coverage_limit,
                    published_ns=published_ns,
                    sequence=sequence,
                )
            elif kind == "instrument":
                market, metadata, ts_ns, price_increment, source = payload
                meta_doc = {
                    "instrument_id": metadata.instrument_id,
                    "size_increment": _dec_str(metadata.size_increment),
                    "multiplier": _dec_str(metadata.multiplier),
                    "taker_fee_bps": _opt_dec_str(metadata.taker_fee_bps),
                    "maker_fee_bps": _opt_dec_str(metadata.maker_fee_bps),
                    "fee_source": str(metadata.fee_source),
                    "min_quantity": _opt_dec_str(metadata.min_quantity),
                    "min_notional": _opt_dec_str(metadata.min_notional),
                    "price_increment": _opt_dec_str(price_increment),
                    "quote_to_usd": _dec_str(market.quote_to_usd),
                    "canonical_multiplier": _dec_str(market.canonical_multiplier),
                }
                record = _base_record(
                    "instrument",
                    run_id=self.run_id,
                    session_id=self.session_id,
                    arrival_seq=seq,
                    symbol=market.symbol,
                    venue=market.venue,
                    instrument_id=market.instrument_id,
                    source=source,
                    ts_event_ns=ts_ns,
                    ts_init_ns=ts_ns,
                    recorded_mono_ns=mono_ns,
                    valid=True,
                    metadata=meta_doc,
                )
            elif kind == "status":
                market, ts_ns, reason, metadata_invalid = payload
                record = _base_record(
                    "status",
                    run_id=self.run_id,
                    session_id=self.session_id,
                    arrival_seq=seq,
                    symbol=market.symbol,
                    venue=market.venue,
                    instrument_id=market.instrument_id,
                    source="observer_invalidation",
                    ts_event_ns=ts_ns,
                    ts_init_ns=ts_ns,
                    recorded_mono_ns=mono_ns,
                    valid=False,
                    invalid_reason=reason,
                    action="INVALIDATE",
                    reason=reason,
                    metadata_invalid=metadata_invalid,
                )
            else:
                raise ValueError(f"unknown tape item kind: {kind!r}")
            self._write_line(_dump_line(record), seq=seq)

    def _write_gap(self, gap: Sequence[Any], mono_ns: int) -> None:
        dropped, first_seq, last_seq, reason = int(gap[0]), int(gap[1]), int(gap[2]), str(gap[3])
        self._gaps += 1
        record = _base_record(
            "gap",
            run_id=self.run_id,
            session_id=self.session_id,
            arrival_seq=None,
            symbol=None,
            venue=None,
            instrument_id=None,
            source="queue",
            ts_event_ns=None,
            ts_init_ns=None,
            recorded_mono_ns=mono_ns,
            valid=False,
            invalid_reason=f"recording_gap:{reason}",
            dropped=dropped,
            missing_from=first_seq,
            missing_to=last_seq,
        )
        self._write_line(_dump_line(record), gap_dropped=dropped)

    def _write_run_end(self) -> None:
        with self._lock:
            dropped = self._dropped
            records = self._accepted_records
            first_seq = self._first_seq
            last_seq = self._last_seq
        gaps = self._gaps
        complete = (not self._failed) and gaps == 0 and dropped == 0
        reason = None if complete else f"{dropped} record(s) dropped in {gaps} gap(s)"
        record = _base_record(
            "run_end",
            run_id=self.run_id,
            session_id=self.session_id,
            arrival_seq=None,
            symbol=None,
            venue=None,
            instrument_id=None,
            source="tape",
            ts_event_ns=None,
            ts_init_ns=None,
            recorded_mono_ns=time.monotonic_ns(),
            valid=complete,
            invalid_reason=reason,
            complete=complete,
            records=records,
            dropped=dropped,
            gaps=gaps,
            first_arrival_seq=first_seq,
            last_arrival_seq=last_seq,
            segment_index=self._segment_index,
            session_started_utc=self.started_utc,
        )
        self._write_line(_dump_line(record), allow_rotate=False)
        self._close_segment(closed=True)
        self._write_manifest_locked()

    def _write_line(
        self,
        line: str,
        *,
        seq: int | None = None,
        gap_dropped: int | None = None,
        allow_rotate: bool = True,
    ) -> None:
        handle = self._handle
        if handle is None:
            raise OSError("tape segment handle is not open")
        handle.write(line)
        nbytes = len(line.encode("utf-8"))
        self._segment_records += 1
        self._segment_uncompressed_bytes += nbytes
        self._total_uncompressed_bytes += nbytes
        if gap_dropped is not None:
            self._segment_gaps += 1
            self._segment_dropped += gap_dropped
        elif seq is not None:
            self._segment_data_records += 1
            if self._segment_first_seq is None:
                self._segment_first_seq = seq
            self._segment_last_seq = seq
        if allow_rotate and self._segment_uncompressed_bytes >= self.rotate_bytes:
            self._close_segment(closed=True)
            self._open_next_segment_locked(write_header=False)
            self._write_manifest_locked()

    def _open_next_segment_locked(self, *, write_header: bool) -> None:
        self._segment_index += 1
        self._segment_path = self.directory / _segment_filename(self._segment_index, compress=self.compress)
        self._segment_records = 0
        self._segment_data_records = 0
        self._segment_uncompressed_bytes = 0
        self._segment_first_seq = None
        self._segment_last_seq = None
        self._segment_dropped = 0
        self._segment_gaps = 0
        self._handle = _open_fragment(
            self._segment_path, compress=self.compress, compresslevel=self.compresslevel,
        )
        if write_header:
            header = _base_record(
                "run_start",
                run_id=self.run_id,
                session_id=self.session_id,
                arrival_seq=None,
                symbol=None,
                venue=None,
                instrument_id=None,
                source="tape",
                ts_event_ns=None,
                ts_init_ns=None,
                recorded_mono_ns=time.monotonic_ns(),
                valid=True,
                session_started_utc=self.started_utc,
                tape=self._segment_path.name,
                legs=self.legs,
                evaluation_interval_ms=self.evaluation_interval_ms,
                queue_max=self.queue_max,
                flush_secs=self.flush_secs,
                rotate_bytes=self.rotate_bytes,
                compress=self.compress,
            )
            line = _dump_line(header)
            self._handle.write(line)
            self._handle.flush()
            nbytes = len(line.encode("utf-8"))
            self._segment_records += 1
            self._segment_uncompressed_bytes += nbytes
            self._total_uncompressed_bytes += nbytes
        self._last_flush = time.monotonic()

    def _segment_entry(self, *, closed: bool, compressed_bytes: int | None = None) -> dict[str, Any]:
        if compressed_bytes is None and self._segment_path is not None:
            try:
                compressed_bytes = self._segment_path.stat().st_size
            except OSError:
                compressed_bytes = None
        return {
            "segment_index": self._segment_index,
            "path": self._segment_path.name if self._segment_path else None,
            "session_id": self.session_id,
            "lines": self._segment_records,
            "records": self._segment_data_records,
            "first_arrival_seq": self._segment_first_seq,
            "last_arrival_seq": self._segment_last_seq,
            "dropped": self._segment_dropped,
            "gaps": self._segment_gaps,
            "bytes": self._segment_uncompressed_bytes,
            "compressed_bytes": compressed_bytes,
            "closed": closed,
        }

    def _close_segment(self, *, closed: bool) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.flush()
            finally:
                handle.close()
        compressed = None
        if self._segment_path is not None:
            try:
                compressed = self._segment_path.stat().st_size
            except OSError:
                compressed = None
        if compressed is not None:
            self._total_compressed_bytes += compressed
        self._segments.append(self._segment_entry(closed=closed, compressed_bytes=compressed))

    def _manifest_entries(self) -> list[dict[str, Any]]:
        if self._handle is None:
            return list(self._segments)
        return list(self._segments) + [self._segment_entry(closed=False)]

    def _write_manifest_locked(self) -> None:
        document = {
            "schema_version": MANIFEST_SCHEMA_VERSION,
            "run_id": self.run_id,
            "session_id": self.session_id,
            "compress": self.compress,
            "updated_utc": _now_utc(),
            "segments": self._manifest_entries(),
        }
        _write_json_atomic(self.manifest_path, document)

    def _abort_on_error(self, reason: str) -> None:
        with self._lock:
            self._failed = True
            self._failure_reason = reason
            self._queue.clear()
        handle, self._handle = self._handle, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        try:
            self._segments.append(self._segment_entry(closed=False))
            self._write_manifest_locked()
        except OSError:
            pass

    @property
    def failed(self) -> bool:
        return self._failed

    @property
    def failure_reason(self) -> str | None:
        return self._failure_reason

    @property
    def records(self) -> int:
        return self._accepted_records

    @property
    def dropped(self) -> int:
        return self._dropped

    @property
    def gaps(self) -> int:
        return self._gaps

    @property
    def complete(self) -> bool:
        return (not self._failed) and self._gaps == 0 and self._dropped == 0 and (self._closed or not self._opened)

    def document(self) -> dict[str, Any]:
        with self._lock:
            entries = self._manifest_entries()
            records = self._accepted_records
            dropped = self._dropped
            high_water = self._queue_high_water
            uncompressed = self._total_uncompressed_bytes
            closed_compressed = self._total_compressed_bytes
            open_compressed = 0
            if self._handle is not None and self._segment_path is not None:
                try:
                    open_compressed = self._segment_path.stat().st_size
                except OSError:
                    open_compressed = 0
        return {
            "directory": str(self.directory),
            "manifest_path": str(self.manifest_path),
            "run_id": self.run_id,
            "session_id": self.session_id,
            "compress": self.compress,
            "records": records,
            "dropped": dropped,
            "gaps": self._gaps,
            "segments": len(entries),
            "uncompressed_bytes": uncompressed,
            "compressed_bytes": closed_compressed + open_compressed,
            "queue_high_water": high_water,
            "complete": (not self._failed) and self._gaps == 0 and dropped == 0 and self._closed,
            "failed": self._failed,
            "failure_reason": self._failure_reason,
        }


def _resolve_tape_paths(path_or_dir: Path | str) -> list[Path]:
    target = Path(path_or_dir)
    if target.is_dir():
        manifest = target / MANIFEST_NAME
        if manifest.exists():
            return _resolve_tape_paths(manifest)
        return sorted(list(target.glob("*.jsonl.gz")) + list(target.glob("*.jsonl")))
    if target.name == MANIFEST_NAME or target.name.endswith(".manifest.json"):
        doc = json.loads(target.read_text(encoding="utf-8"))
        entries = sorted(doc.get("segments", []), key=lambda item: int(item.get("segment_index", 1)))
        return [target.parent / str(entry["path"]) for entry in entries if entry.get("path")]
    return [target]


def iter_tape_records(path_or_dir: Path | str) -> Iterator[dict[str, Any]]:
    """Yield decoded records in receive order across `.jsonl.gz`, `.jsonl`, dirs, or manifests."""
    for frag in _resolve_tape_paths(path_or_dir):
        opener = gzip.open if frag.suffix == ".gz" else Path.open
        with opener(frag, "rt", encoding="utf-8") as handle:
            for line in handle:
                text = line.strip()
                if text:
                    yield json.loads(text)

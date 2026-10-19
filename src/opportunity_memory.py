"""Opt-in bounded memory and market-data diagnostics, without trading controls."""
from __future__ import annotations

from collections import deque
from datetime import UTC, datetime
import gc
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import tracemalloc
import uuid


DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / "reports/opportunity-trading/memory"
INTERVAL_SECONDS = 15
MAX_LOG_BYTES = 8 * 1024 * 1024
LATENCY_SAMPLES = 128
VENUES = ("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK")
CHANNELS = ("time_events", "exec_events", "exec_commands", "data_events", "data_commands")
_active = None
_session_lock = threading.Lock()


def current_diagnostics():
    return _active


def _notice(message):
    try:
        print("[memory] " + message, file=sys.stderr, flush=True)
    except Exception:
        pass


def process_memory():
    """Return Linux process accounting; no allocator attribution is inferred."""
    path = Path("/proc/self/status")
    result = {}
    if path.exists():
        for line in path.read_text().splitlines():
            name, _, value = line.partition(":")
            if name in {"VmRSS", "VmHWM", "RssAnon", "RssFile", "VmSwap", "VmSize"}:
                result[name] = int(value.split()[0]) * 1024
    return result


def runner_rates(before, after):
    """Keep dispatch and queue sampling intervals distinct; omit reset intervals."""
    if not before or not after:
        return {}
    elapsed = after["elapsed_ns"] - before["elapsed_ns"]
    sampled = after["queue_sampled_at_ns"] - before["queue_sampled_at_ns"]
    if elapsed <= 0 or any(after[c]["dispatched"] < before[c]["dispatched"] for c in CHANNELS):
        return {}
    result = {}
    for channel in CHANNELS:
        dispatched = after[channel]["dispatched"] - before[channel]["dispatched"]
        busy = after[channel]["dispatch_busy_ns"] - before[channel]["dispatch_busy_ns"]
        row = {"dispatch_per_second": dispatched * 1e9 / elapsed,
               "dispatch_busy_fraction": busy / elapsed,
               "mean_dispatch_ms": busy / dispatched / 1e6 if dispatched else None}
        if sampled > 0:
            row["queue_net_growth_per_second"] = (after[channel]["queue_depth"] - before[channel]["queue_depth"]) * 1e9 / sampled
            # Endpoints only: this is evidence to investigate, not a claim that
            # the receiver was continuously nonempty throughout the interval.
            row["queued_without_dispatch"] = (dispatched == 0 and before[channel]["queue_depth"] > 0
                                               and after[channel]["queue_depth"] > 0)
            row["queue_sample_interval_seconds"] = sampled / 1e9
        row["queue_sample_lag_ms"] = max(0, after["elapsed_ns"] - after["queue_sampled_at_ns"]) / 1e6
        result[channel] = row
    return result


def summarize_books(observer, state, now_ns):
    """Count existing price sets without cloning native ladders or retaining history."""
    venues = {}
    largest = []
    for instrument_id, sides in observer.level_prices.items():
        market = observer.markets[instrument_id]
        bids, asks = len(sides["BUY"]), len(sides["SELL"])
        row = venues.setdefault(market.venue, {"markets": 0, "bid_levels": 0,
            "ask_levels": 0, "max_levels_per_side": 0, "snapshot_ready": 0,
            "subscriptions_requested": 0})
        row["markets"] += 1
        row["subscriptions_requested"] += instrument_id in observer.subscribed
        row["bid_levels"] += bids
        row["ask_levels"] += asks
        row["max_levels_per_side"] = max(row["max_levels_per_side"], bids, asks)
        row["snapshot_ready"] += instrument_id in observer.snapshot_seen
        largest.append({"instrument_id": str(instrument_id), "venue": market.venue,
                        "bid_levels": bids, "ask_levels": asks})
    largest.sort(key=lambda row: row["bid_levels"] + row["ask_levels"], reverse=True)
    return {"sampled_at_unix_ns": now_ns, "venues": venues, "largest_books": largest[:20],
        "total_levels": sum(row["bid_levels"] + row["ask_levels"] for row in venues.values()),
        "dirty_books": len(observer.dirty), "pending_snapshots": len(observer.pending_snapshot),
        "pending_aster_subscriptions": len(observer.pending_aster),
        "current_snapshots": len(state.books), "current_opportunities": len(state.current)}


class MemoryDiagnostics:
    """Store fixed-label counters and publish reports on a separate writer thread."""

    def __init__(self, directory, *, trace_python=False):
        self.directory = Path(directory)
        self.run_id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        self.path = self.directory / f"memory-{os.getpid()}-{self.run_id}.jsonl"
        self.latest = self.directory / "memory-latest.json"
        self.handle = None
        self.previous_native = None
        self.previous_batches = None
        self.previous_sample_ns = None
        self.books = None
        self.account_checks = None
        self.next_book_sample_ns = 0
        self.counters = {venue: {"batches": 0, "price_updates": 0} for venue in VENUES}
        self.latencies = {venue: deque(maxlen=LATENCY_SAMPLES) for venue in VENUES}
        self.last_batches = {venue: None for venue in VENUES}
        self.max_batch_updates = {venue: 0 for venue in VENUES}
        self.timer_ages = deque(maxlen=LATENCY_SAMPLES)
        self.timer_callbacks = 0
        self.trace_python = trace_python
        self.owns_tracing = False
        self.stop_event = threading.Event()
        self.writer = None
        self.errors = 0
        self.status = "running"

    def attach_handle(self, handle):
        if not callable(getattr(handle, "metrics_snapshot", None)):
            raise RuntimeError("memory diagnostics require native LiveNodeHandle.metrics_snapshot")
        self.handle = handle

    def record_batch(self, venue, count, ts_init):
        try:
            row = self.counters.get(venue)
            if row is None:
                return
            row["batches"] += 1
            row["price_updates"] += count
            self.max_batch_updates[venue] = max(self.max_batch_updates[venue], count)
            self.last_batches[venue] = ts_init
            # Sample one batch in 128; keep the signed age to expose clock anomalies.
            if row["batches"] % 128 == 0:
                self.latencies[venue].append((time.time_ns() - ts_init) / 1e6)
        except Exception:
            self.errors += 1

    def capture_books(self, observer, state, now_ns):
        if now_ns < self.next_book_sample_ns:
            return
        self.next_book_sample_ns = now_ns + INTERVAL_SECONDS * 1_000_000_000
        try:
            self.books = summarize_books(observer, state, now_ns)
            self.account_checks = {domain: dict(row)
                                   for domain, row in getattr(state, "account_check_metrics", {}).items()}
        except Exception:
            self.errors += 1

    def record_timer(self, event):
        try:
            if event is not None:
                self.timer_callbacks += 1
                self.timer_ages.append((time.time_ns() - event.ts_event) / 1e6)
        except Exception:
            self.errors += 1

    def sample(self):
        from opportunity_timing import current_timing_snapshot

        now_ns = time.time_ns()
        monotonic_ns = time.monotonic_ns()
        native = self.handle.metrics_snapshot() if self.handle is not None else None
        rates = runner_rates(self.previous_native, native)
        self.previous_native = native
        python = {"allocated_blocks": sys.getallocatedblocks(), "gc_counts": gc.get_count(),
                  "tracemalloc_enabled": tracemalloc.is_tracing()}
        if tracemalloc.is_tracing():
            current, peak = tracemalloc.get_traced_memory()
            python.update(traced_current_bytes=current, traced_peak_bytes=peak,
                          tracer_overhead_bytes=tracemalloc.get_tracemalloc_memory())
        batches = {}
        for venue, counts in self.counters.items():
            samples = sorted(self.latencies[venue])
            batches[venue] = {**counts, "delivery_age_sample_count": len(samples)}
            batches[venue]["max_batch_price_updates"] = self.max_batch_updates[venue]
            batches[venue]["last_processed_batch_ts_init_ns"] = self.last_batches[venue]
            if self.previous_batches is not None and monotonic_ns > self.previous_sample_ns:
                seconds = (monotonic_ns - self.previous_sample_ns) / 1e9
                for key in ("batches", "price_updates"):
                    batches[venue][key + "_per_second"] = (counts[key] - self.previous_batches[venue][key]) / seconds
            if samples:
                batches[venue].update(delivery_age_p50_ms=samples[math.ceil(len(samples) * .5) - 1],
                    delivery_age_p95_ms=samples[math.ceil(len(samples) * .95) - 1],
                    delivery_age_p99_ms=samples[math.ceil(len(samples) * .99) - 1],
                    delivery_age_max_ms=samples[-1])
        books = self.books
        self.previous_batches = {venue: dict(counts) for venue, counts in self.counters.items()}
        self.previous_sample_ns = monotonic_ns
        timing = current_timing_snapshot()
        timer_ages = sorted(self.timer_ages)
        timer = {"callbacks": self.timer_callbacks, "age_sample_count": len(timer_ages)}
        if timer_ages:
            timer.update(age_p50_ms=timer_ages[math.ceil(len(timer_ages) * .5) - 1],
                         age_p95_ms=timer_ages[math.ceil(len(timer_ages) * .95) - 1],
                         age_p99_ms=timer_ages[math.ceil(len(timer_ages) * .99) - 1],
                         age_max_ms=timer_ages[-1])
        return {"schema_version": 1, "run_id": self.run_id, "pid": os.getpid(),
            "started_at_utc": self.started_at, "time": datetime.now(UTC).isoformat(),
            "unix_ns": now_ns, "status": self.status, "diagnostic_errors": self.errors,
            "process_bytes": process_memory(), "python": python, "native_runner": native,
            "native_rates": rates, "market_batches": batches, "books": books,
            "processing_timings": timing,
            "account_checks": self.account_checks,
            "market_timer": timer,
            "book_sample_age_ms": None if books is None else (now_ns - books["sampled_at_unix_ns"]) / 1e6,
            "notes": ["Queue depth counts events; one book event can contain many price updates.",
                "Native fields are non-transactional atomic reads; queue depths use a maintenance-tick sample.",
                "Delivery age samples time since native ts_init, not exact oldest-queue age.",
                "Subscription counts record requests issued, not exchange acknowledgements.",
                "Batch counts include received callbacks even when a book is not yet snapshot-ready.",
                "Timer age is callback time minus scheduled ts_event; samples retain signed wall-clock skew.",
                "Python tracing excludes Rust mimalloc allocations; RSS minus traced bytes is not native live bytes.",
                "Reports contain counts and public instrument IDs, never prices, account IDs or credentials."]}

    def write(self):
        try:
            document = self.sample()
            temporary = self.directory / f"memory-latest-{self.run_id}.tmp"
            temporary.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.latest)
            if self.path.exists() and self.path.stat().st_size >= MAX_LOG_BYTES:
                self.path.replace(self.path.with_suffix(".previous.jsonl"))
            with self.path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(document) + "\n")
        except Exception:
            self.errors += 1
            if self.errors == 1:
                _notice("report unavailable; diagnostics do not control trading")

    def __enter__(self):
        global _active
        if not _session_lock.acquire(blocking=False):
            raise RuntimeError("memory diagnostics already active")
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            self.path.touch(exist_ok=False)
            if self.trace_python and not tracemalloc.is_tracing():
                tracemalloc.start(1)
                self.owns_tracing = True
            _active = self
            self.write()

            def flush():
                while not self.stop_event.wait(INTERVAL_SECONDS):
                    self.write()

            self.writer = threading.Thread(target=flush, name="memory-report", daemon=True)
            self.writer.start()
            _notice(f"report: {self.path}")
            return self
        except BaseException:
            _active = None
            if self.owns_tracing:
                tracemalloc.stop()
            _session_lock.release()
            raise

    def __exit__(self, kind, value, traceback):
        global _active
        self.status = "finished" if kind is None else "interrupted"
        self.stop_event.set()
        self.writer.join(timeout=2)
        if not self.writer.is_alive():
            self.write()
            if self.owns_tracing:
                tracemalloc.stop()
        else:
            _notice("report writer still busy; final diagnostic snapshot deferred")
        _active = None
        _session_lock.release()
        return False


def add_memory_arguments(parser):
    parser.add_argument("--memory-diagnostics", action="store_true",
                        help="record bounded queue, book and process-memory diagnostics")
    parser.add_argument("--memory-diagnostics-dir", type=Path, default=DEFAULT_DIRECTORY)
    parser.add_argument("--memory-tracemalloc", action="store_true",
                        help="also trace Python allocations (adds overhead; requires --memory-diagnostics)")


def run_with_memory(function, args):
    if args.memory_tracemalloc and not args.memory_diagnostics:
        raise ValueError("--memory-tracemalloc requires --memory-diagnostics")
    if not args.memory_diagnostics:
        return function()
    with MemoryDiagnostics(args.memory_diagnostics_dir, trace_python=args.memory_tracemalloc):
        return function()

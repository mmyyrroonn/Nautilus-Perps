"""Opt-in, process-wide wall-time measurements for one trading CLI session.

Only fixed operation names and allowlisted venue/outcome labels are recorded.
Arguments, results, exception messages and account/order identities never enter
the report. Worker threads share the recorder; nested spans use thread-local
stacks. No tracing/profiling hook or trading-clock replacement is installed.
"""
from __future__ import annotations

from collections import deque
from contextlib import nullcontext
from datetime import UTC, datetime
from functools import wraps
import json
import math
import os
from pathlib import Path
import sys
import threading
import time
import uuid


SAMPLE_WINDOW = 1024
FLUSH_SECONDS = 10
DEFAULT_DIRECTORY = Path(__file__).resolve().parents[1] / "reports/opportunity-trading/timings"
_active = None
_session_lock = threading.Lock()
_disabled_span = nullcontext()
_VENUES = {"HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"}
_OUTCOMES = {"connect", "reconcile", "submit", "modify", "cancel", "accepted", "updated",
             "fill", "filled", "terminal", "canceled", "expired", "rejected", "denied",
             "modify_rejected", "cancel_rejected", "connected", "unknown", "account"}


def _labels(venue, outcome):
    venue = "HL" if venue == "HYPERLIQUID" else venue
    return (venue if isinstance(venue, str) and venue in _VENUES else "",
            outcome if isinstance(outcome, str) and outcome in _OUTCOMES else "")


class _Span:
    def __init__(self, recorder, name, venue, outcome):
        self.recorder, self.name = recorder, name
        self.venue, self.outcome = _labels(venue, outcome)
        self.children_ns = 0

    def __enter__(self):
        self.started_ns = time.perf_counter_ns()
        stack = getattr(self.recorder.local, "stack", None)
        if stack is None:
            stack = self.recorder.local.stack = []
        stack.append(self)
        return self

    def __exit__(self, kind, value, traceback):
        elapsed = time.perf_counter_ns() - self.started_ns
        stack = self.recorder.local.stack
        stack.pop()
        if stack:
            stack[-1].children_ns += elapsed
        self.recorder.record(self.name, elapsed, self_ns=max(0, elapsed - self.children_ns),
                             venue=self.venue, outcome=self.outcome, error=kind is not None)
        return False


class TimingRecorder:
    """Bounded aggregates; report I/O occurs only on the writer thread/CLI edges."""
    def __init__(self):
        self.run_id = uuid.uuid4().hex
        self.started_at = datetime.now(UTC).isoformat()
        self.started_ns = time.perf_counter_ns()
        self.ended_ns = None
        self.finished_at = None
        self.exit_code = None
        self.status = "running"
        self.local = threading.local()
        self.lock = threading.Lock()
        self.rows = {}
        self.closed = False
        self.path = None
        self.io_warned = False

    def record(self, name, elapsed_ns, *, self_ns=None, venue="", outcome="", error=False,
               category="span"):
        venue, outcome = _labels(venue, outcome)
        key = (category, name, venue, outcome)
        with self.lock:
            if self.closed:
                return
            row = self.rows.get(key)
            if row is None:
                row = self.rows[key] = {"count": 0, "errors": 0, "total_ns": 0,
                    "self_ns": 0, "min_ns": elapsed_ns, "max_ns": 0,
                    "recent": deque(maxlen=SAMPLE_WINDOW)}
            row["count"] += 1
            row["errors"] += int(error)
            row["total_ns"] += elapsed_ns
            row["self_ns"] += elapsed_ns if self_ns is None else self_ns
            row["min_ns"] = min(row["min_ns"], elapsed_ns)
            row["max_ns"] = max(row["max_ns"], elapsed_ns)
            row["recent"].append(elapsed_ns)

    def document(self):
        with self.lock:
            # Copy under the lock; sorting/serialization must not block callbacks.
            copied = [(key, {**row, "recent": tuple(row["recent"])}) for key, row in self.rows.items()]
        rows = []
        for (category, name, venue, outcome), row in copied:
            recent = sorted(row.pop("recent"))
            rows.append({"category": category, "name": name, "venue": venue, "outcome": outcome,
                "count": row["count"], "errors": row["errors"],
                "total_ms": row["total_ns"] / 1e6, "self_ms": row["self_ns"] / 1e6,
                "mean_ms": row["total_ns"] / row["count"] / 1e6,
                "min_ms": row["min_ns"] / 1e6, "max_ms": row["max_ns"] / 1e6,
                "recent_samples": len(recent),
                "recent_p50_ms": recent[math.ceil(len(recent) * .50) - 1] / 1e6,
                "recent_p95_ms": recent[math.ceil(len(recent) * .95) - 1] / 1e6})
        return {"schema_version": 1, "run_id": self.run_id, "pid": os.getpid(),
            "started_at_utc": self.started_at, "updated_at_utc": datetime.now(UTC).isoformat(),
            "finished_at_utc": self.finished_at, "status": self.status, "exit_code": self.exit_code,
            "elapsed_ms": ((self.ended_ns or time.perf_counter_ns()) - self.started_ns) / 1e6,
            "clock": "perf_counter_ns", "recent_sample_window": SAMPLE_WINDOW,
            "notes": ["Wall time, not CPU time. Spans include synchronous child calls and waits.",
                "self_ms excludes instrumented children on the same thread; concurrent threads overlap.",
                "Latency rows measure dispatch to first observed outcome; they are not CPU hotspots.",
                "Counts/totals/min/max cover the run; percentiles cover only the last 1024 completions.",
                "In-flight work is absent from aggregates; abrupt termination leaves the last snapshot.",
                "Rust adapter internals and work before CLI argument parsing are not instrumented."],
            "operations": sorted(rows, key=lambda row: (row["category"], -row["self_ms"]))}

    def write(self):
        try:
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self.document(), indent=2) + "\n", encoding="utf-8")
            temporary.replace(self.path)
        except Exception:
            if not self.io_warned:
                self.io_warned = True
                _notice("report write failed; timing remains in memory and trading continues")

    def finish(self, exit_code, status):
        with self.lock:
            self.ended_ns = time.perf_counter_ns()
            self.finished_at = datetime.now(UTC).isoformat()
            self.exit_code, self.status, self.closed = exit_code, status, True


def _notice(message):
    try:
        print("[timing] " + message, file=sys.stderr, flush=True)
    except Exception:
        pass  # Diagnostics must not prevent execution or cleanup.


def span(name, *, venue="", outcome=""):
    recorder = _active
    return _disabled_span if recorder is None else _Span(recorder, name, venue, outcome)


def timed(name, *, labels=None):
    """Measure a synchronous function; disabled calls take one branch only."""
    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            recorder = _active
            if recorder is None:
                return function(*args, **kwargs)
            venue, outcome = "", ""
            if labels is not None:
                try:
                    venue, outcome = labels(*args, **kwargs)
                except Exception:
                    pass
            with _Span(recorder, name, venue, outcome):
                return function(*args, **kwargs)
        return wrapped
    return decorate


def start_latency():
    recorder = _active
    return None if recorder is None else (recorder, time.perf_counter_ns())


def record_latency(marker, name, *, venue="", outcome="", error=False):
    if marker is not None:
        recorder, started_ns = marker
        recorder.record(name, time.perf_counter_ns() - started_ns, venue=venue,
                        outcome=outcome, error=error, category="latency")


def add_timing_arguments(parser):
    parser.add_argument("--timing", action="store_true",
                        help="record module wall times and per-venue latencies (off by default)")
    parser.add_argument("--timing-dir", type=Path, default=DEFAULT_DIRECTORY,
                        help="directory for unique timing JSON reports; used only with --timing")


def run_timed(function, *, enabled=False, directory=DEFAULT_DIRECTORY):
    """Preserve the wrapped exit/exception and write reports independently of state."""
    global _active
    if not enabled:
        return function()
    # The runner owns one session per process. Never reassign a running session's
    # worker-thread measurements to a second report.
    if not _session_lock.acquire(blocking=False):
        raise RuntimeError("a timing session is already active in this process")
    recorder = TimingRecorder()
    stop = threading.Event()
    writer = None
    exit_code, status = None, "failed"
    try:
        try:
            directory = Path(directory)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
            report_path = directory / f"timing-{stamp}-{recorder.run_id}.json"
            report_path.touch(exist_ok=False)
            recorder.path = report_path
            recorder.write()
            def flush_loop():
                while not stop.wait(FLUSH_SECONDS):
                    recorder.write()
                recorder.write()
            thread = threading.Thread(target=flush_loop, name="timing-report", daemon=True)
            thread.start()
            writer = thread
            _notice(f"report: {recorder.path}")
        except Exception:
            _notice("report setup failed; timing remains in memory and trading continues")
        _active = recorder
        try:
            with span("run.total"):
                exit_code = function()
            status = "completed" if exit_code == 0 else "failed"
            return exit_code
        except KeyboardInterrupt:
            status = "interrupted"
            raise
    finally:
        _active = None
        recorder.finish(exit_code, status)
        stop.set()
        if writer is not None:
            writer.join(timeout=1)
        # Avoid waiting indefinitely for a filesystem stall on the writer.
        if writer is not None and writer.is_alive():
            _notice("report writer is still busy; final snapshot deferred")
        elif writer is None and recorder.path is not None:
            recorder.write()
        document = recorder.document()
        _notice(f"{status}; elapsed {document['elapsed_ms'] / 1000:.3f}s")
        for row in [r for r in document["operations"] if r["category"] == "span"][:10]:
            label = "/".join(filter(None, (row["name"], row["venue"], row["outcome"])))
            _notice(f"{label}: calls={row['count']} self={row['self_ms']:.3f}ms "
                    f"total={row['total_ms']:.3f}ms p95_recent={row['recent_p95_ms']:.3f}ms")
        _session_lock.release()

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
_OUTCOMES = {"connect", "reconcile", "submit", "modify", "cancel", "accepted", "updated", "partial_fill", "full_fill",
             "fill", "filled", "terminal", "canceled", "expired", "rejected", "denied",
             "modify_rejected", "cancel_rejected", "connected", "unknown", "account"}


def diagnostics_enabled():
    return _active is not None and not _active.closed


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
        self.entry_orders = {}
        self.entry_pair_counts = {name: 0 for name in
            ("bilateral_full", "bilateral_partial", "single_leg", "unfilled")}
        self.entry_pairs_seen = {}
        self.entry_pair_reclassifications = 0
        self.entry_trace_evictions = 0

    def record(self, name, elapsed_ns, *, self_ns=None, venue="", outcome="", error=False,
               category="span", sample_every=1):
        venue, outcome = _labels(venue, outcome)
        key = (category, name, venue, outcome)
        with self.lock:
            if self.closed:
                return
            row = self.rows.get(key)
            if row is None:
                row = self.rows[key] = {"count": 0, "errors": 0, "total_ns": 0,
                    "self_ns": 0, "min_ns": elapsed_ns, "max_ns": 0,
                    "recent": deque(maxlen=SAMPLE_WINDOW), "sample_every": sample_every}
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
            traces = [dict(trace_id=row["trace_id"], venue=row["venue"],
                           stages=list(row["stages"]), native_events=list(row["native_events"]),
                           native_events_seen=row["native_events_seen"]) for row in self.entry_orders.values()]
            pair_counts = dict(self.entry_pair_counts)
            evictions = self.entry_trace_evictions
            reclassifications = self.entry_pair_reclassifications
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
                "recent_p95_ms": recent[math.ceil(len(recent) * .95) - 1] / 1e6,
                "recent_p99_ms": recent[math.ceil(len(recent) * .99) - 1] / 1e6,
                "sample_every": row["sample_every"]})
        return {"schema_version": 1, "run_id": self.run_id, "pid": os.getpid(),
            "started_at_utc": self.started_at, "updated_at_utc": datetime.now(UTC).isoformat(),
            "finished_at_utc": self.finished_at, "status": self.status, "exit_code": self.exit_code,
            "elapsed_ms": ((self.ended_ns or time.perf_counter_ns()) - self.started_ns) / 1e6,
            "clock": "perf_counter_ns", "recent_sample_window": SAMPLE_WINDOW,
            "notes": ["Wall time, not CPU time. Spans include synchronous child calls and waits.",
                "self_ms excludes instrumented children on the same thread; concurrent threads overlap.",
                "Latency rows measure named stage boundaries; they are not CPU hotspots or network RTT.",
                "Counts/totals/min/max cover the run; percentiles cover only the last 1024 completions.",
                "sampled_span rows cover sampled completions only; self_ms equals inclusive total_ms. "
                "They do not subtract from full spans and must not be summed with them.",
                "sampled_latency rows use periodic samples; count/total cover sampled stages only.",
                "market.delivery_age uses time.time_ns minus native ts_init, not exact queue residence.",
                "Submission ends at the Python submit_order handoff, not the native network write.",
                "In-flight work is absent from aggregates; abrupt termination leaves the last snapshot.",
                "Rust adapter internals and work before CLI argument parsing are not instrumented."],
            "operations": sorted(rows, key=lambda row: (row["category"], -row["self_ms"])),
            "entry_execution": {"traces": traces, "trace_limit": SAMPLE_WINDOW,
                "stage_limit": 32, "trace_evictions": evictions,
                "native_event_limit": 16,
                "native_event_notes": [
                    "ts_init is local native event construction time, not a socket receive timestamp.",
                    "ts_event is adapter/venue dependent; never interpreted as local network time.",
                    "OrderSubmitted precedes transport completion; its placement differs by adapter.",
                    "Callback books are local observations, not exchange matching-engine snapshots.",
                    "last_complete is invalid historical configured depth; never current liquidity or admission evidence.",
                    "local_pending contains complete-batch metadata only, not a usable or copied local ladder.",
                    "Invalid or future local timestamps have no derived delivery latency."],
                "missing_native_stages": ["native_received", "signing", "network_write", "native_receipt_received"],
                "clock_notes": "stage monotonic_ns uses perf_counter_ns within this process only; wall_ns and market timestamps use Unix wall time; do not subtract across clock domains",
                "pair_outcomes": pair_counts, "completed_pairs": sum(pair_counts.values()),
                "pair_reclassifications": reclassifications,
                "bilateral_completion_rate": pair_counts["bilateral_full"] / sum(pair_counts.values()) if sum(pair_counts.values()) else None,
                "single_leg_rate": pair_counts["single_leg"] / sum(pair_counts.values()) if sum(pair_counts.values()) else None,
                "limitations": "Opt-in process-local observations; restored orders and evicted traces lack full correlation. Percentiles are bounded recent samples; market load differs between windows."}}

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


def current_timing_snapshot():
    """Return bounded aggregates for periodic diagnostics, or None when disabled."""
    recorder = _active
    if recorder is None:
        return None
    document = recorder.document()
    return {key: document[key] for key in ("run_id", "elapsed_ms", "operations")}


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


def sampled_timed(name, *, every=127):
    """Sample fixed-name hot stages per thread without changing full-span nesting.

    Every Nth call is timed while diagnostics are enabled. Skipped calls only
    advance a bounded ordinal; they do not read clocks, acquire recorder locks,
    or retain arguments/results. Sampling is periodic, not a random estimator.
    """
    if type(every) is not int or every < 2:
        raise ValueError("sample interval must be an integer of at least two")

    def decorate(function):
        @wraps(function)
        def wrapped(*args, **kwargs):
            recorder = _active
            if recorder is None:
                return function(*args, **kwargs)
            counts = getattr(recorder.local, "sampled_calls", None)
            if counts is None:
                counts = recorder.local.sampled_calls = {}
            ordinal = counts.get(name, 0) + 1
            counts[name] = ordinal if ordinal < every else 0
            if ordinal < every:
                return function(*args, **kwargs)
            started_ns = time.perf_counter_ns()
            error = True
            try:
                result = function(*args, **kwargs)
                error = False
                return result
            finally:
                recorder.record(name, time.perf_counter_ns() - started_ns,
                    category="sampled_span", sample_every=every, error=error)
        return wrapped
    return decorate


def start_latency():
    recorder = _active
    return None if recorder is None else (recorder, time.perf_counter_ns())


def begin_entry_trace(request_id, venue, marker, wall_ns):
    """Keep request IDs solely as bounded in-memory keys; export random trace IDs."""
    recorder = _active
    if recorder is None or marker is None:
        return
    with recorder.lock:
        if len(recorder.entry_orders) >= SAMPLE_WINDOW:
            recorder.entry_orders.pop(next(iter(recorder.entry_orders)))
            recorder.entry_trace_evictions += 1
        recorder.entry_orders[request_id] = {"trace_id": uuid.uuid4().hex,
            "venue": _labels(venue, "")[0], "native_events": deque(maxlen=16),
            "native_events_seen": 0, "stages": deque([{
                "stage": "candidate", "monotonic_ns": marker[1], "wall_ns": wall_ns}], maxlen=32)}


def record_entry_native_event(request_id, kind, received_ns, *, ts_event=None, ts_init=None,
                              market_provider=None):
    """Bounded native lifecycle timestamps and local books; no trading side effects."""
    recorder = _active
    if recorder is None or kind not in {"submitted", "accepted", "filled", "canceled",
                                        "expired", "rejected", "denied"}:
        return
    with recorder.lock:
        trace = recorder.entry_orders.get(request_id)
        if trace is None or recorder.closed:
            return
    row = {"kind": kind, "received_wall_ns": received_ns}
    if type(ts_event) is int and ts_event > 0:
        row["native_event_wall_ns"] = ts_event
    if type(ts_init) is int and 0 < ts_init <= received_ns:
        row["native_init_wall_ns"] = ts_init
        row["native_init_to_python_ms"] = (received_ns - ts_init) / 1e6
        row["local_timestamp_status"] = "valid"
    else:
        row["local_timestamp_status"] = "missing_or_invalid"
    try:
        market = market_provider() if market_provider else None
        if market is not None:
            # Provider is an internal fixed-schema observer, never a native payload.
            fields = {"status", "source_wall_ns", "received_wall_ns", "source_age_ms", "receive_age_ms",
                      "bid_price", "ask_price", "limit_price", "quantity", "depth_within_limit",
                      "full_quantity_available", "touch_outside_limit_bps", "diagnostic_only",
                      "evidence_state", "published_wall_ns", "coverage_limit", "complete_book_claimed",
                      "bid_levels", "ask_levels", "lifecycle_reason", "lifecycle_wall_ns"}
            row["market"] = {k: v for k, v in market.items() if k in fields}
            for name, extra in (
                    ("last_complete", {"invalidation_reason", "invalidated_wall_ns", "invalidation_time_basis"}),
                    ("local_pending", {"completed_wall_ns", "sequence"})):
                if name in market:
                    allowed = fields | extra
                    row["market"][name] = {k: v for k, v in market[name].items() if k in allowed}
    except Exception:
        row["market"] = {"status": "observation_failed"}
    with recorder.lock:
        if recorder.entry_orders.get(request_id) is not trace or recorder.closed:
            return
        trace["native_events"].append(row)
        trace["native_events_seen"] += 1


def record_entry_stage(request_id, stage, *, wall_ns=None, evidence=None, reason=None):
    recorder = _active
    if recorder is None or stage not in {"persisted", "coordinator", "send_check", "local_blocked",
            "python_submit", "accepted", "rejected", "unknown", "partial_fill", "full_fill", "terminal",
            "canceled", "expired", "denied", "strategy_accepted", "strategy_rejected",
            "strategy_fill", "strategy_terminal", "python_receipt_accepted", "python_receipt_fill",
            "python_receipt_rejected", "python_receipt_terminal"}:
        return
    interval = None
    with recorder.lock:
        trace = recorder.entry_orders.get(request_id)
        if trace is None:
            return
        row = {"stage": stage, "monotonic_ns": time.perf_counter_ns()}
        previous_stage, operation, outcome = {
            "coordinator": ("persisted", "entry.persisted_to_coordinator", ""),
            "python_submit": ("coordinator", "entry.coordinator_to_submit", ""),
            "strategy_accepted": ("python_receipt_accepted", "entry.receipt_to_strategy", "accepted"),
            "strategy_rejected": ("python_receipt_rejected", "entry.receipt_to_strategy", "rejected"),
            "strategy_fill": ("python_receipt_fill", "entry.receipt_to_strategy", "fill"),
            "strategy_terminal": ("python_receipt_terminal", "entry.receipt_to_strategy", "terminal"),
        }.get(stage, (None, None, None))
        previous = next((s for s in reversed(trace["stages"]) if s["stage"] == previous_stage), None)
        if previous is not None:
            interval = (operation, row["monotonic_ns"] - previous["monotonic_ns"], trace["venue"], outcome)
        if type(wall_ns) is int:
            row["wall_ns"] = wall_ns
        if reason in {"valid", "missing_book", "metadata_unavailable", "metadata_changed", "stale_or_invalid_book",
                "insufficient_depth", "price_outside_limit", "invalid_execution_rules_or_fees", "book_skew",
                "net_edge_insufficient", "ioc_no_match", "rate_limit", "insufficient_balance", "invalid_order",
                "post_only", "venue_other", "local_validation", "unhedged_headroom_insufficient",
                "entry_admission_changed", "account_admission_changed", "leverage_configuration",
                "leverage_target_pending", "leverage_unavailable", "leverage_stale", "leverage_read_failed",
                "leverage_recheck_required", "leverage_symbol_missing", "leverage_notional_unsupported",
                "leverage_above_maximum", "leverage_target_mismatch"}:
            row["reason"] = reason
        if evidence is not None:
            # The only caller constructs this fixed schema. Never accept raw
            # native events, exception text or account/order payloads here.
            fields = {"checked_wall_ns", "net_usd", "minimum_net_usd", "net_margin_usd", "reserve_usd",
                      "unhedged_reserve_bps", "per_leg_risk_budget_usd"}
            leg_fields = {"limit_price", "quantity", "source_wall_ns", "received_wall_ns", "source_age_ms",
                "receive_age_ms", "available_quantity", "depth_within_limit", "executable_vwap",
                "current_bound_price", "limit_cash_usd", "fee_usd", "standalone_risk_usd"}
            row["evidence"] = {k: v for k, v in evidence.items() if k in fields}
            for leg in ("buy", "sell"):
                if leg in evidence:
                    row["evidence"][leg] = {k: v for k, v in evidence[leg].items() if k in leg_fields}
                    if "leverage" in evidence[leg]:
                        row["evidence"][leg]["leverage"] = {
                            k: v for k, v in evidence[leg]["leverage"].items() if k in {
                                "target", "actual", "allowed_maximum", "observed_ns", "checked_ns",
                                "projected_notional", "target_source"}}
        trace["stages"].append(row)
    if interval is not None:
        operation, elapsed, venue, outcome = interval
        recorder.record(operation, elapsed, category="latency", venue=venue, outcome=outcome)


def record_entry_pair(request_ids, outcome):
    recorder = _active
    if recorder is None:
        return
    with recorder.lock:
        if outcome not in recorder.entry_pair_counts:
            return
        # Require both process-local traces; never count restored or evicted
        # pairs as fully observed in this diagnostic session.
        if not all(rid in recorder.entry_orders for rid in request_ids):
            return
        previous = recorder.entry_pairs_seen.get(request_ids)
        if previous == outcome:
            return
        if previous is not None:
            recorder.entry_pair_counts[previous] -= 1
            recorder.entry_pair_reclassifications += 1
        elif len(recorder.entry_pairs_seen) >= SAMPLE_WINDOW:
            recorder.entry_pairs_seen.pop(next(iter(recorder.entry_pairs_seen)))
        recorder.entry_pairs_seen[request_ids] = outcome
        recorder.entry_pair_counts[outcome] += 1


def _latency_sample(recorder, name, venue, every):
    counts = getattr(recorder.local, "latency_calls", None)
    if counts is None:
        counts = recorder.local.latency_calls = {}
    key = (name, _labels(venue, "")[0])
    ordinal = counts.get(key, 0) + 1
    counts[key] = ordinal if ordinal < every else 0
    return ordinal == every


def sampled_start_latency(name, *, venue="", every=127):
    """Select a whole stage/cycle; skipped calls read no clocks or recorder lock."""
    recorder = _active
    if recorder is None or not _latency_sample(recorder, name, venue, every):
        return None
    return (recorder, time.perf_counter_ns())


def sampled_delivery_age(ts_init, *, venue=""):
    """Observe native creation to Python callback age, not enqueue residence."""
    recorder = _active
    if recorder is None or not _latency_sample(recorder, "market.delivery_age", venue, 127):
        return
    now_ns = time.time_ns()
    if type(ts_init) is int and 0 < ts_init <= now_ns:
        recorder.record("market.delivery_age", now_ns - ts_init, venue=venue,
                        category="sampled_latency", sample_every=127)


def record_latency(marker, name, *, venue="", outcome="", error=False, sample_every=1, ended_ns=None):
    if marker is not None:
        recorder, started_ns = marker
        ended_ns = time.perf_counter_ns() if ended_ns is None else ended_ns
        recorder.record(name, ended_ns - started_ns, venue=venue,
                        outcome=outcome, error=error, sample_every=sample_every,
                        category="sampled_latency" if sample_every > 1 else "latency")


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

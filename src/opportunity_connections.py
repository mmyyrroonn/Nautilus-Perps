#!/usr/bin/env python3
"""Check the scanner's real public feeds without saving quotes or opportunities."""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import threading
import time

from opportunity_core import _book_valid, _metadata_valid
from opportunity_scan import load_plan


class ConnectionHealth:
    """Bounded scalar counters and current coverage, never a sample history."""

    def __init__(self, state):
        self.state = state
        self.plan = state.plan
        self.started_at = time.monotonic()
        self.started_cpu = time.process_time()
        self.last_clock = self.started_at
        self.last_cpu = self.started_cpu
        self.updates = {m.instrument_id: 0 for m in self.plan.markets}
        self.seen = set()
        self.seen_fresh = set()
        self.last_fresh = set()
        self.peak_fresh = 0
        self.peak_comparable_symbols = 0
        self.peak_cpu_percent = 0.0
        self.samples = 0
        self.max_callback_age_ms = 0.0

    def updated(self, instrument_id, book):
        self.updates[instrument_id] += 1
        self.seen.add(instrument_id)
        self.max_callback_age_ms = max(self.max_callback_age_ms,
                                      (time.time_ns() - book.ts_received_ns) / 1_000_000)
        if self.usable(instrument_id, book, time.time_ns()):
            self.seen_fresh.add(instrument_id)

    def usable(self, instrument_id, book, now_ns):
        metadata = self.state.metadata.get(instrument_id)
        return (metadata is not None and _metadata_valid(metadata)
                and _book_valid(book, self.plan.settings, now_ns))

    def sample(self, now_ns=None):
        now_ns = time.time_ns() if now_ns is None else now_ns
        fresh = {i for i, b in self.state.books.items() if self.usable(i, b, now_ns)}
        self.seen_fresh.update(fresh)
        self.last_fresh = fresh
        self.peak_fresh = max(self.peak_fresh, len(fresh))
        comparable = 0
        for ids in self.state.groups.values():
            books = [self.state.books[i] for i in ids if i in fresh]
            if any(a.venue != b.venue and
                   abs(a.ts_event_ns - b.ts_event_ns) <= self.plan.settings.max_skew_ms * 1_000_000
                   for n, a in enumerate(books) for b in books[n + 1:]):
                comparable += 1
        self.peak_comparable_symbols = max(self.peak_comparable_symbols, comparable)
        clock, cpu = time.monotonic(), time.process_time()
        if clock - self.last_clock >= 0.1:
            self.peak_cpu_percent = max(self.peak_cpu_percent,
                100 * (cpu - self.last_cpu) / (clock - self.last_clock))
        self.last_clock, self.last_cpu = clock, cpu
        self.samples += 1

    def document(self):
        venues = {}
        for market in self.plan.markets:
            row = venues.setdefault(market.venue, {
                "configured": 0, "received_book": 0, "ever_fresh": 0, "last_fresh": 0,
                "book_updates": 0, "missing_fresh_instruments": []})
            instrument_id = market.instrument_id
            row["configured"] += 1
            row["received_book"] += instrument_id in self.seen
            row["ever_fresh"] += instrument_id in self.seen_fresh
            row["last_fresh"] += instrument_id in self.last_fresh
            row["book_updates"] += self.updates[instrument_id]
            if instrument_id not in self.seen_fresh:
                row["missing_fresh_instruments"].append(instrument_id)
        elapsed = time.monotonic() - self.started_at
        cpu = time.process_time() - self.started_cpu
        try:
            import resource
            peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
            peak_rss_bytes = int(peak_rss if sys.platform == "darwin" else peak_rss * 1024)
        except ImportError:
            peak_rss_bytes = None
        return {
            "schema_version": 1,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "configured_symbols": len(self.state.groups),
            "configured_markets": len(self.plan.markets),
            "venues": venues,
            "peak_simultaneously_fresh_markets": self.peak_fresh,
            "peak_comparable_symbols": self.peak_comparable_symbols,
            "refresh_samples": self.samples,
            "elapsed_secs": round(elapsed, 3),
            "process_cpu_secs": round(cpu, 3),
            "average_process_cpu_percent": round(100 * cpu / elapsed, 2) if elapsed else 0,
            "peak_refresh_interval_cpu_percent": round(self.peak_cpu_percent, 2),
            "peak_rss_bytes": peak_rss_bytes,
            "max_observed_receive_to_callback_ms": round(self.max_callback_age_ms, 3),
            "opportunities_recorded": self.state.saved,
            "historical_book_samples_retained": 0,
        }


def run_check(plan, progress_secs=15):
    from opportunity_runtime import build_node

    # The connection check always overrides any enabled event recorder.
    plan = replace(plan, output_path=None)
    node, observer, state = build_node(plan)
    health = ConnectionHealth(state)
    original_update = state.update_book

    def update(instrument_id, book, now_ns, *, raw_book=None, evaluate=True):
        original_update(instrument_id, book, now_ns, raw_book=raw_book, evaluate=evaluate)
        health.updated(instrument_id, book)

    state.update_book = update
    last_progress = health.started_at

    def display():
        nonlocal last_progress
        health.sample()
        if time.monotonic() - last_progress >= progress_secs:
            progress = health.document()
            for venue in progress["venues"].values():
                venue["missing_fresh_count"] = len(venue.pop("missing_fresh_instruments"))
            print("[connections] " + json.dumps(progress, sort_keys=True), flush=True)
            last_progress = time.monotonic()

    state.display = display
    timer = threading.Timer(plan.duration_secs, node.handle().stop)
    timer.daemon = True
    timer.start()
    try:
        node.run()
    except KeyboardInterrupt:
        node.handle().stop()
    finally:
        timer.cancel()
    document = health.document()
    document.update(actor_started=observer.started, actor_failure=observer.failure,
                    recording_disabled=True)
    complete = (observer.started and observer.failure is None
                and len(health.seen_fresh) == len(plan.markets))
    document["all_markets_observed_fresh"] = complete
    return document, 0 if complete else 1


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--venues", help="Comma-separated logical venues; other configured legs are omitted")
    parser.add_argument("--duration-secs", type=int, default=180)
    parser.add_argument("--progress-secs", type=int, default=15)
    parser.add_argument("--report", type=Path, help="Optional aggregate health JSON; contains no book prices")
    args = parser.parse_args(argv)
    try:
        if not 1 <= args.duration_secs <= 86400 or args.progress_secs < 1:
            raise ValueError("duration_secs must be 1..86400 and progress_secs must be positive")
        plan = load_plan(args.config)
        if args.venues:
            venues = {v.strip().upper() for v in args.venues.split(",") if v.strip()}
            available = {m.venue for m in plan.markets}
            if not venues or venues - available:
                raise ValueError("requested venues are not in the config")
            plan = replace(plan, markets=tuple(m for m in plan.markets if m.venue in venues))
        document, status = run_check(replace(plan, duration_secs=args.duration_secs), args.progress_secs)
        rendered = json.dumps(document, indent=2, sort_keys=True) + "\n"
        if args.report:
            args.report.parent.mkdir(parents=True, exist_ok=True)
            args.report.write_text(rendered, encoding="utf-8")
        print(rendered, end="", flush=True)
        return status
    except (OSError, ValueError, ImportError, RuntimeError) as exc:
        print(f"[connections] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

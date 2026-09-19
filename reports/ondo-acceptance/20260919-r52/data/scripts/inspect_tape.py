#!/usr/bin/env python3
"""Inspect the *content* of a historical L2 tape: which record kinds, venues,
sessions and state events the sample actually contains, versus what is absent.

Read-only. Prints a JSON summary so an acceptance report can state what the
historical sample did and did not exercise instead of implying a scenario is
covered when it is not.
"""

from __future__ import annotations

import argparse
import collections
import json
from pathlib import Path


def summarise(tape: Path) -> dict:
    kinds = collections.Counter()
    venues = collections.Counter()
    sessions = collections.Counter()
    nonnull = collections.Counter()
    invalid_reasons = collections.Counter()
    statuses: list[dict] = []
    metadata: list[dict] = []
    first_seqs: dict[str, int | None] = {}
    last_seqs: dict[str, int | None] = {}
    ts_init_missing = 0
    ts_event_missing = 0
    depth_records = 0
    depth_with_size_increment = 0

    for line in tape.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        kind = record.get("event_kind")
        kinds[kind] += 1
        venues[record.get("venue")] += 1
        sessions[record.get("session_id")] += 1
        seq = record.get("arrival_seq")
        if kind is not None:
            first_seqs.setdefault(kind, seq)
            last_seqs[kind] = seq
        for key, value in record.items():
            if value is not None:
                nonnull[key] += 1
        if record.get("invalid_reason"):
            invalid_reasons[record["invalid_reason"]] += 1
        if record.get("ts_init_ns") is None:
            ts_init_missing += 1
        if record.get("ts_event_ns") is None:
            ts_event_missing += 1
        if kind in ("book", "quote"):
            depth_records += 1
            meta = record.get("metadata") or {}
            if meta.get("size_increment") is not None:
                depth_with_size_increment += 1
        if kind == "status":
            statuses.append(
                {
                    key: record.get(key)
                    for key in (
                        "arrival_seq",
                        "venue",
                        "valid",
                        "invalid_reason",
                        "ts_event_ns",
                        "ts_init_ns",
                        "metadata",
                    )
                }
            )
        if kind == "instrument":
            metadata.append(
                {
                    "arrival_seq": record.get("arrival_seq"),
                    "venue": record.get("venue"),
                    "metadata": record.get("metadata"),
                }
            )

    return {
        "tape": str(tape),
        "line_count": sum(kinds.values()),
        "kinds": dict(kinds),
        "venues": {str(key): value for key, value in venues.items()},
        "sessions": dict(sessions),
        "nonnull_key_counts": dict(nonnull),
        "invalid_reasons": dict(invalid_reasons),
        "kind_first_arrival_seq": first_seqs,
        "kind_last_arrival_seq": last_seqs,
        "ts_init_ns_missing_records": ts_init_missing,
        "ts_event_ns_missing_records": ts_event_missing,
        "depth_records": depth_records,
        "depth_with_real_size_increment": depth_with_size_increment,
        "status_records": statuses,
        "instrument_records": metadata,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("tapes", nargs="+", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args()
    summary = [summarise(tape) for tape in args.tapes]
    text = json.dumps(summary, indent=2, ensure_ascii=False)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(text + "\n", encoding="utf-8")
        print(f"[inspect] wrote {args.out}")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

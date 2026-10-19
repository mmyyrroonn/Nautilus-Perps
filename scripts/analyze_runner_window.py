"""Summarize bounded diagnostic files without exporting account/credential data."""
import argparse
import json
from pathlib import Path

CHANNELS = ("time_events", "exec_events", "exec_commands", "data_events", "data_commands")


def quantiles(values):
    values = sorted(values)
    return {f"p{p}": values[int((len(values) - 1) * p / 100)] if values else None
            for p in (50, 95, 99)}


def summarize(rows, seconds=1800):
    rows = sorted(rows, key=lambda r: r["unix_ns"])
    if not rows:
        raise ValueError("no diagnostic samples")
    latest = rows[-1]
    # Never join process runs or compare a reset counter with its predecessor.
    rows = [r for r in rows if (r["pid"], r["run_id"]) == (latest["pid"], latest["run_id"])
            and r["unix_ns"] >= latest["unix_ns"] - seconds * 1e9 and r.get("native_runner")]
    if len(rows) < 2:
        raise ValueError("need two native samples from the same run")
    rows = list({r["unix_ns"]: r for r in rows}.values())
    if len(rows) < 2:
        raise ValueError("need two distinct native sample times")
    result = {"pid": latest["pid"], "run_id": latest["run_id"], "samples": len(rows),
        "window_seconds": (rows[-1]["unix_ns"] - rows[0]["unix_ns"]) / 1e9,
        "rss_mib": quantiles([r["process_bytes"].get("VmRSS", 0) / 1024**2 for r in rows]),
        "max_swap_mib": max(r["process_bytes"].get("VmSwap", 0) / 1024**2 for r in rows),
        "max_sample_gap_seconds": max((b["unix_ns"] - a["unix_ns"]) / 1e9 for a, b in zip(rows, rows[1:])),
        "channels": {}, "hotspots": [],
        "limits": "Sampled queue endpoints do not measure enqueue latency or prove continuous starvation. "
                  "This report does not establish trading admission health."}
    for channel in CHANNELS:
        q = [r["native_runner"][channel]["queue_depth"] for r in rows]
        count = busy = elapsed = stalls = 0
        for a, b in zip(rows, rows[1:]):
            a, b = a["native_runner"], b["native_runner"]
            dt = b["elapsed_ns"] - a["elapsed_ns"]
            dc = b[channel]["dispatched"] - a[channel]["dispatched"]
            db = b[channel]["dispatch_busy_ns"] - a[channel]["dispatch_busy_ns"]
            if dt <= 0 or dc < 0 or db < 0:
                continue
            elapsed += dt
            count += dc
            busy += db
            if b["queue_sampled_at_ns"] > a["queue_sampled_at_ns"]:
                stalls += bool(dc == 0 and a[channel]["queue_depth"] > 0 and b[channel]["queue_depth"] > 0)
        result["channels"][channel] = {"queue_depth": quantiles(q), "max_depth": max(q),
            "zero_depth_samples": q.count(0), "queued_without_dispatch_intervals": stalls,
            "dispatched": count, "dispatch_per_second": count * 1e9 / elapsed if elapsed else None,
            "dispatch_busy_fraction": busy / elapsed if elapsed else None,
            "mean_dispatch_ms": busy / count / 1e6 if count else None}
    def operations(row):
        return {(r["category"], r["name"], r["venue"], r["outcome"]): r
                for r in (row.get("processing_timings") or {}).get("operations", [])}
    first = operations(rows[0])
    for key, end in operations(rows[-1]).items():
        start = first.get(key, {})
        count = end["count"] - start.get("count", 0)
        if key[0] != "span" or count <= 0:
            continue
        own = end["self_ms"] - start.get("self_ms", 0)
        result["hotspots"].append({"name": key[1], "venue": key[2], "calls": count,
            "self_ms": own, "mean_self_ms": own / count,
            "recent_p95_ms": end.get("recent_p95_ms"), "recent_p99_ms": end.get("recent_p99_ms")})
    result["hotspots"] = sorted(result["hotspots"], key=lambda r: -r["self_ms"])[:20]
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    latest = json.loads((args.directory / "memory-latest.json").read_text())
    rows = []
    for path in args.directory.glob(f'memory-{latest["pid"]}-{latest["run_id"]}*.jsonl'):
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass  # A concurrent writer may not have completed the last line.
    print(json.dumps(summarize(rows, args.seconds), indent=2))


if __name__ == "__main__":
    main()

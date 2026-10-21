"""Compare in-memory paired send checks against an exact local Git baseline."""
import argparse
import gc
import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import tracemalloc
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from test_opportunity_live import state
from test_opportunity_trade import NOW, publish
from opportunity_entry_evidence import discard_invalid_entry


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", default="a95805e8a2d7dcdd4fcc1ea4437eaf6963820fac")
    parser.add_argument("--iterations", type=int, default=3000)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = subprocess.check_output(["git", "show", args.baseline + ":src/opportunity_entry_evidence.py"], cwd=ROOT)
    baseline = ModuleType("baseline_entry_evidence")
    exec(compile(source, "baseline_entry_evidence.py", "exec"), baseline.__dict__)
    results = {"baseline": [], "candidate": []}
    for repeat in range(3):
        for name in (("baseline", "candidate") if repeat % 2 == 0 else ("candidate", "baseline")):
            fn = baseline.discard_invalid_entry if name == "baseline" else discard_invalid_entry
            live, _ = state()
            publish(live)
            intents = live.drain_intents()
            def check():
                for intent in intents:
                    assert not fn(live, intent, NOW)
            for _ in range(100):
                check()
            durations = []
            cpu = time.process_time_ns()
            wall = time.perf_counter_ns()
            for _ in range(args.iterations):
                started = time.perf_counter_ns()
                check()
                durations.append(time.perf_counter_ns() - started)
            wall, cpu = time.perf_counter_ns() - wall, time.process_time_ns() - cpu
            durations.sort()
            gc.collect()
            tracemalloc.start()
            before = tracemalloc.get_traced_memory()[0]
            for _ in range(100):
                check()
            gc.collect()
            current, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            results[name].append(dict(p50_us=statistics.median(durations) / 1000,
                p95_us=durations[int(len(durations) * .95)] / 1000,
                p99_us=durations[int(len(durations) * .99)] / 1000,
                pairs_per_second=args.iterations * 1e9 / wall,
                cpu_us_per_pair=cpu / args.iterations / 1000,
                retained_bytes_after_100_pairs=current-before, peak_bytes=peak-before))
    report = dict(baseline_sha=args.baseline, python=sys.version, iterations_per_sample=args.iterations,
        scope="Two send checks per pair; in-memory books/accounts, timing disabled, no store/native/network; latency without tracemalloc",
        results=results, medians={name: {key: statistics.median(row[key] for row in rows)
                                       for key in rows[0]} for name, rows in results.items()})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report["medians"], indent=2))


if __name__ == "__main__":
    main()

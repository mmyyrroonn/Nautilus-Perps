"""Synthetic before/after benchmark; no credentials, network, or native runtime."""
import argparse
from dataclasses import replace
import importlib
import json
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
from time import perf_counter_ns

ROOT = Path(__file__).resolve().parents[3]
MODULES = ("opportunity_preflight", "opportunity_native_execution", "opportunity_live")


def worker(source, samples, storage):
    sys.path[:0] = [str(source), str(ROOT / "src"), str(ROOT / "tests")]
    for name in MODULES:
        importlib.import_module(name)
    from test_opportunity_live import state, receipt, submissions
    from test_opportunity_trade import NOW, publish
    from opportunity_trade_store import TradeStore

    class Store:
        def __init__(self, actual):
            self.actual, self.writes, self.event_bytes = actual, 0, 0
        def load(self):
            return None
        def save(self, snapshot, events):
            self.writes += 1
            if self.actual:
                self.actual.save(snapshot, events)
            else:
                # Match production JSON serialization, omitting only SQLite I/O.
                document = json.dumps(snapshot, sort_keys=True)
                rows = [json.dumps(e, sort_keys=True) for e in events]
                self.event_bytes += sum(len(row.encode()) for row in rows)

    entry, rejection = [], []
    writes, payloads = [], []
    with tempfile.TemporaryDirectory(prefix="trade-review-bench-") as directory:
        actual = TradeStore(Path(directory) / "bench.sqlite3", "synthetic") if storage == "sqlite" else None
        try:
            for index in range(samples + 20):
                store = Store(actual)
                live, _ = state(store=store)
                store.writes = store.event_bytes = 0
                start = perf_counter_ns()
                publish(live)
                end = perf_counter_ns()
                order = submissions(live)[0]
                result = receipt(order, "rejected")
                if hasattr(result, "reason_code"):
                    result = replace(result, reason_code="insufficient_balance",
                                     reason_source="native_event", native_status="rejected")
                reject_start = perf_counter_ns()
                live.on_receipt(result, NOW + 1)
                reject_end = perf_counter_ns()
                if index >= 20:
                    entry.append((end-start)/1000)
                    rejection.append((reject_end-reject_start)/1000)
                    writes.append(store.writes)
                    payloads.append(store.event_bytes)
        finally:
            if actual:
                actual.close()
    return {"entry_us": entry, "rejection_us": rejection,
            "writes_per_cycle": sorted(set(writes)),
            "event_payload_bytes_per_cycle": statistics.median(payloads) if storage == "memory" else None}


def summarize(values):
    values = sorted(values)
    return {"samples": len(values), "p50_us": statistics.median(values),
            "p95_us": values[int(.95*(len(values)-1))], "p99_us": values[int(.99*(len(values)-1))]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--source", type=Path)
    parser.add_argument("--samples", type=int, default=200)
    parser.add_argument("--storage", choices=("memory", "sqlite"), default="memory")
    args = parser.parse_args()
    if args.worker:
        print(json.dumps(worker(args.source, args.samples, args.storage)))
        return
    output = {"baseline": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
              "method": "4 alternating baseline/current subprocess rounds; 20 warmups per subprocess; microseconds",
              "limitations": "Synthetic Windows entry and rejection paths; not network latency or production throughput.",
              "storage": {}}
    with tempfile.TemporaryDirectory(prefix="trade-review-baseline-") as directory:
        baseline = Path(directory)
        for name in MODULES:
            (baseline / (name + ".py")).write_bytes(subprocess.check_output(
                ["git", "show", f"HEAD:src/{name}.py"], cwd=ROOT))
        for storage, samples in (("memory", args.samples), ("sqlite", 80)):
            data = {name: {"entry_us": [], "rejection_us": [], "writes": [], "bytes": []}
                    for name in ("baseline", "current")}
            rounds = []
            for iteration in range(4):
                variants = ("baseline", "current") if iteration % 2 == 0 else ("current", "baseline")
                for name in variants:
                    source = baseline if name == "baseline" else ROOT / "src"
                    result = json.loads(subprocess.check_output([sys.executable, str(Path(__file__).resolve()),
                        "--worker", "--source", str(source), "--storage", storage, "--samples", str(samples)], cwd=ROOT))
                    for stage in ("entry_us", "rejection_us"):
                        data[name][stage].extend(result[stage])
                    data[name]["writes"].extend(result["writes_per_cycle"])
                    data[name]["bytes"].append(result["event_payload_bytes_per_cycle"])
                    rounds.append({"variant": name, "round": iteration,
                                   **{stage: summarize(result[stage]) for stage in ("entry_us", "rejection_us")}})
            output["storage"][storage] = {"rounds": rounds, "aggregate": {
                name: {**{stage: summarize(rows[stage]) for stage in ("entry_us", "rejection_us")},
                       "writes_per_cycle": sorted(set(rows["writes"])),
                       "event_payload_bytes_per_cycle": rows["bytes"]}
                for name, rows in data.items()}}
    destination = Path(__file__).with_name("benchmark.json")
    destination.write_text(json.dumps(output, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({mode: value["aggregate"] for mode, value in output["storage"].items()}, indent=2))


if __name__ == "__main__":
    main()

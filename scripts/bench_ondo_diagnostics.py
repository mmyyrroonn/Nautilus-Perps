"""Offline observation overhead; mocked dispatch, no credentials or network."""

import argparse
import importlib.util
import json
import platform
import sys
import time
import tracemalloc
from collections import deque
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ondo_maker_diagnostics import CAPACITY, MakerDiagnostics, format_diagnostics
from ondo_maker_live import OndoMakerStrategy


def summary(values):
    values = sorted(values)
    n = len(values)
    return {
        "samples": n,
        "p50_us": values[(n - 1) // 2] / 1000,
        "p95_us": values[(n * 95 + 99) // 100 - 1] / 1000,
        "p99_us": values[(n * 99 + 99) // 100 - 1] / 1000,
        "max_us": values[-1] / 1000,
        "mean_cpu_capacity_per_second": n * 1e9 / sum(values),
    }


def host(commands, diagnostics):
    cached = NS(is_closed=False)
    return NS(
        engine=NS(step=lambda _: commands), driving=False,
        cache=NS(order=lambda _: cached), cancel_order=lambda _: None,
        command_counts={"cancel": 0}, diagnostics=diagnostics,
        latencies_ns=deque(maxlen=2048),
    )


def compare(baseline, commands, count):
    cases = [
        ("previous_always_on", baseline.OndoMakerStrategy.drive, host(commands, None)),
        ("diagnostics_off", OndoMakerStrategy.drive, host(commands, None)),
        ("diagnostics_on", OndoMakerStrategy.drive, host(commands, MakerDiagnostics())),
    ]
    samples = {name: [] for name, _, _ in cases}
    for iteration in range(count + 2000):
        # Rotate execution order to reduce fixed-order bias.
        for offset in range(3):
            name, drive, value = cases[(iteration + offset) % 3]
            before = time.perf_counter_ns()
            drive(value)
            elapsed = time.perf_counter_ns() - before
            if iteration >= 2000:
                samples[name].append(elapsed)
            if value.diagnostics is not None:
                # Terminal callbacks are outside the measured dispatch interval.
                for command in commands:
                    value.diagnostics.cancel.canceled(command.order.client_id, time.monotonic_ns())
    if commands:
        assert all(value.command_counts["cancel"] == (count + 2000) * 4 for _, _, value in cases)
    return {name: summary(values) for name, values in samples.items()}


def populated(count):
    value = MakerDiagnostics()
    for n in range(count):
        value.maker.begin("maker", n)
        value.maker.submitted("maker", n)
        value.maker.accepted_event("maker", n + 100, n + 90)
        value.cancel.begin("cancel", n, n + 1)
        value.cancel.canceled("cancel", n + 200)
        value.drive_samples.append(n)
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, required=True, help="Previous ondo_maker_live.py")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location("maker_baseline", args.baseline)
    baseline = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(baseline)
    commands = tuple(NS(kind="cancel", order=NS(client_id=f"bench-{n}")) for n in range(4))
    result = {
        "scope": "Offline Python bridge CPU only; mocked engine/dispatch; excludes network and native I/O",
        "platform": platform.platform(), "python": platform.python_version(),
        "idle_bridge": compare(baseline, (), 30000),
        "four_cancel_bridge": compare(baseline, commands, 30000),
    }
    value = populated(CAPACITY)
    copy_samples, format_samples = [], []
    for _ in range(500):
        start = time.perf_counter_ns()
        snapshot = value.snapshot()
        copy_samples.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        format_diagnostics(snapshot)
        format_samples.append(time.perf_counter_ns() - start)
    result["full_snapshot_copy"] = summary(copy_samples)
    result["full_snapshot_format"] = summary(format_samples)
    tracemalloc.start()
    retained = populated(100000)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result["memory"] = {"current_bytes": current, "peak_bytes": peak,
                        "retained_per_series": [len(retained.maker.samples), len(retained.cancel.samples), len(retained.drive_samples)]}
    result["limitations"] = "Single-host microbenchmark; background Python formatting still shares the GIL; not live cancel latency or a latency guarantee"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

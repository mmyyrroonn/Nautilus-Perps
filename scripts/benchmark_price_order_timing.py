"""Offline paired-entry timing: memory ledger and fake native handoff, no network."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
from types import SimpleNamespace as NS


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--iterations", type=int, default=500)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    sys.path[:0] = [str(args.root / "src"), str(args.root / "tests")]
    import opportunity_timing as timing
    from opportunity_live_runner import LiveCoordinator
    from opportunity_native_execution import NativeOrderGateway
    from test_opportunity_live import MemoryStore, state
    from test_opportunity_native_execution import MODEL, FakeStrategy
    from test_opportunity_trade import NOW, publish

    result = {}
    for enabled in (False, True):
        timing._active = timing.TimingRecorder() if enabled else None
        durations, cpu_durations = [], []
        for index in range(args.iterations + 50):
            live, _ = state(store=MemoryStore())
            strategy = FakeStrategy()
            gateway = NativeOrderGateway(strategy, live.plan.markets, lambda _: None,
                lambda _: None, lambda _: None, model=MODEL, environ={})
            coordinator = NS(live=live, proxy=NS(restart_pending=False), gateway=gateway)
            cpu, begin = time.process_time_ns(), time.perf_counter_ns()
            publish(live)
            LiveCoordinator._live_intents(coordinator, NOW)
            elapsed, cpu_elapsed = time.perf_counter_ns() - begin, time.process_time_ns() - cpu
            assert len(strategy.calls) == 2
            gateway.close()
            if index >= 50:
                durations.append(elapsed)
                cpu_durations.append(cpu_elapsed)
        durations.sort()
        result[str(enabled)] = {"p50_us": statistics.median(durations) / 1000,
            "p95_us": durations[int(len(durations) * .95)] / 1000,
            "p99_us": durations[int(len(durations) * .99)] / 1000,
            "mean_us": statistics.mean(durations) / 1000,
            "pairs_per_second": len(durations) * 1e9 / sum(durations),
            "cpu_us_per_pair": statistics.mean(cpu_durations) / 1000}
        timing._active = None
    args.output.write_text(json.dumps({"iterations": args.iterations,
        "scope": "Two books through paired fake submit; setup excluded; no fsync, network or venue work",
        "results": result}, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

"""Offline book hot-path microbenchmark against a selected application checkout."""
import argparse
import json
from pathlib import Path
import statistics
import sys
import time
import tracemalloc


def measure(function, iterations):
    for _ in range(1000):
        function()
    samples = []
    cpu = time.process_time_ns()
    start = time.perf_counter_ns()
    for _ in range(iterations):
        begin = time.perf_counter_ns()
        function()
        samples.append(time.perf_counter_ns() - begin)
    elapsed = time.perf_counter_ns() - start
    cpu = time.process_time_ns() - cpu
    samples.sort()
    return {"median_ns": statistics.median(samples), "p99_ns": samples[int(iterations * .99)],
            "calls_per_second": iterations * 1e9 / elapsed, "cpu_ns_per_call": cpu / iterations}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--iterations", type=int, default=10000)
    args = parser.parse_args()
    sys.path[:0] = [str(args.root / "tests"), str(args.root / "src")]
    import pytest
    import opportunity_timing as timing
    from test_opportunity_scan import NS, batch, native_observer
    from nautilus_trader.model import (BookAction, BookOrder, OrderBookDelta, OrderBookDeltas,
                                       OrderSide, Price, Quantity, RecordFlag)

    results = {}
    for enabled in (False, True):
        with pytest.MonkeyPatch.context() as patch:
            patch.setattr(timing, "_active", timing.TimingRecorder() if enabled else None)
            _, state, observer, ids, _, _ = native_observer(
                args.root, patch, evaluation_interval_ms=100, output_path=None, depth_levels=20)
            instrument = ids[0]
            deltas = [OrderBookDelta.clear(instrument, 1, NS, NS)]
            for side, sign in ((OrderSide.BUY, -1), (OrderSide.SELL, 1)):
                for level in range(20):
                    flags = RecordFlag.F_SNAPSHOT.value
                    if side == OrderSide.SELL and level == 19:
                        flags |= RecordFlag.F_LAST.value
                    deltas.append(OrderBookDelta(instrument, BookAction.UPDATE,
                        BookOrder(side, Price.from_str(f"{100 + sign * (level + 1)}.00"),
                                  Quantity.from_str("10.00"), 0), flags, 1, NS, NS))
            snapshot = OrderBookDeltas(instrument, deltas)
            incremental = batch(instrument, "99.00", "101.00", snapshot=False)
            observer.on_book_deltas(snapshot)
            publish = lambda: observer.publish_book(instrument, (NS, NS, 1), evaluate=False)
            publish()
            result = results[str(enabled)] = {
                "incremental": measure(lambda: observer.on_book_deltas(incremental), args.iterations),
                "replacement": measure(lambda: observer.on_book_deltas(snapshot), args.iterations),
                "publication": measure(publish, args.iterations),
            }
            tracemalloc.start()
            for _ in range(1000):
                observer.on_book_deltas(snapshot)
                publish()
            retained_before = tracemalloc.get_traced_memory()[0]
            for _ in range(args.iterations):
                observer.on_book_deltas(snapshot)
                publish()
            retained_after, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
            result["memory"] = {"retained_growth_bytes": retained_after - retained_before,
                                "peak_python_bytes": peak}
            assert observer.failure is None
    print(json.dumps({"iterations": args.iterations, "levels_per_side": 20, "results": results}, indent=2))


if __name__ == "__main__":
    main()

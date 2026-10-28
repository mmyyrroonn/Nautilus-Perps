"""Offline guard/controller CPU latency; excludes exchange/network latency."""

import json
import statistics
import sys
import time
import tracemalloc
from collections import deque
from decimal import Decimal
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from ondo_maker import SECOND, Instrument, MakerConfig, MakerEngine, VolatilityGuard


def summary(samples):
    ordered = sorted(samples)
    return {
        "samples": len(samples),
        "p50_us": statistics.median(samples) / 1000,
        "p95_us": ordered[int(len(samples) * 0.95)] / 1000,
        "p99_us": ordered[int(len(samples) * 0.99)] / 1000,
        "max_us": max(samples) / 1000,
    }


def main():
    count = 30_000
    g = VolatilityGuard()
    fast, scan = [], []
    points = deque()
    for n in range(count):
        now, price = n * 100_000, 100 + (n % 101) / 10_000
        start = time.perf_counter_ns()
        g.update(now, price, price)
        fast.append(time.perf_counter_ns() - start)
        start = time.perf_counter_ns()
        while len(points) > 1 and points[1][0] <= now - SECOND:
            points.popleft()
        points.append((now, price))
        low = min(p for _, p in points)
        high = max(p for _, p in points)
        _range = (high - low) / low * 10_000
        scan.append(time.perf_counter_ns() - start)
    engine = MakerEngine(MakerConfig(Decimal(20), Decimal(20)), "bench")
    for market in ("BTC", "ETH"):
        engine.markets[market].instrument = Instrument(
            Decimal(".01"), Decimal(".001"), Decimal(".001")
        )
        engine.on_binance(market, 0, 100, 100)
        engine.on_binance(market, SECOND, 100, 100)
        engine.on_ondo(market, SECOND, Decimal("99.99"), Decimal("100.01"))
    engine.set_account(ready=True, reduction_ready=True, flat=True)
    engine.step(SECOND)
    controller = []
    for n in range(count):
        start = time.perf_counter_ns()
        engine.step(SECOND + n)
        controller.append(time.perf_counter_ns() - start)
    tracemalloc.start()
    bounded = VolatilityGuard()
    for n in range(100_000):
        bounded.update(n * 100_000, 100 + (n % 101) / 10_000, 100 + (n % 101) / 10_000)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result = {
        "scope": "offline Python only; not signal-to-wire or cancel-ACK latency",
        "reference": "literal trailing min/max scan at synthetic 10000 updates/second",
        "scan_baseline": summary(scan),
        "incremental_guard": summary(fast),
        "two_market_idle_controller": summary(controller),
        "guard_traced_current_bytes": current,
        "guard_traced_peak_bytes": peak,
        "guard_retained_points": len(bounded.points),
    }
    if len(sys.argv) == 2:
        out = Path(sys.argv[1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()

"""Offline depth snapshot and idle decision costs; no network or credentials."""

import json
import sys
import time
import tracemalloc
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from bench_ondo_maker import summary
from ondo_maker import SECOND, Instrument, MakerConfig, MakerEngine


def bridge_samples(engine, bids, asks):
    from nautilus_trader.model import (
        BookAction, BookOrder, InstrumentId, OrderBookDelta, OrderBookDeltas,
        OrderSide, Price, Quantity, RecordFlag,
    )
    from ondo_maker_live import OndoMakerStrategy

    instrument = InstrumentId.from_str("BTC-USD-PERP.ONDO")
    stamp = time.time_ns()
    deltas = [OrderBookDelta.clear(instrument, 0, stamp, stamp)]
    for side, rows in ((OrderSide.BUY, bids), (OrderSide.SELL, asks)):
        for index, (price, quantity) in enumerate(rows):
            flags = int(RecordFlag.F_SNAPSHOT)
            if side == OrderSide.SELL and index == len(rows) - 1:
                flags |= int(RecordFlag.F_LAST)
            deltas.append(OrderBookDelta(
                instrument, BookAction.ADD,
                BookOrder(side, Price.from_str(str(price)), Quantity.from_str(str(quantity)), 0),
                flags, 0, stamp, stamp,
            ))
    batch = OrderBookDeltas(instrument, deltas)
    host = SimpleNamespace(
        engine=engine, symbols={instrument: "BTC"}, last_native_depth_ns={},
        clock=SimpleNamespace(timestamp_ns=lambda: stamp),
        drive=lambda: engine.step(time.monotonic_ns()),
    )
    samples = []
    for n in range(1000):
        if n % 50 == 0:
            now = time.monotonic_ns()
            for market in engine.markets:
                engine.on_binance(market, now - SECOND, 100000., 100000.1)
                engine.on_binance(market, now, 100000., 100000.1)
                engine.on_ondo(market, now, bids[0][0], asks[0][0])
                engine.on_depth(market, now, bids, asks)
        before = time.perf_counter_ns()
        OndoMakerStrategy.on_book_deltas.__wrapped__(host, batch)
        samples.append(time.perf_counter_ns() - before)
    return summary(samples)


def main():
    engine = MakerEngine(MakerConfig(D(50), D(50)), "depthbench")
    bids = [(D(100000) - D("0.1") * n, D(1)) for n in range(1000)]
    asks = [(D("100000.1") + D("0.1") * n, D(1)) for n in range(1000)]
    for market, state in engine.markets.items():
        state.instrument = Instrument(D("0.1"), D("0.00001"), D("0.00001"))
        engine.on_binance(market, 0, 100000., 100000.1)
        engine.on_binance(market, SECOND, 100000., 100000.1)
        engine.on_ondo(market, SECOND, bids[0][0], asks[0][0])
        engine.on_depth(market, SECOND, bids, asks)
    engine.set_account(ready=True, reduction_ready=True, flat=True)
    engine.step(SECOND)
    durations = []
    started = time.process_time_ns()
    for n in range(3000):
        before = time.perf_counter_ns()
        engine.on_depth("BTC", SECOND + n, bids, asks)
        engine.step(SECOND + n)
        durations.append(time.perf_counter_ns() - before)
    cpu_ns = time.process_time_ns() - started
    idle = []
    for n in range(30_000):
        before = time.perf_counter_ns()
        engine.step(SECOND + 3000 + n)
        idle.append(time.perf_counter_ns() - before)
    tracemalloc.start()
    for n in range(1000):
        engine.on_depth("BTC", SECOND + 40_000 + n, bids, asks)
        engine.step(SECOND + 40_000 + n)
    current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    result = {
        "scope": "offline Python; 1000 levels per side; excludes native decoding, transport and cancel ACK",
        "depth_update_and_decision": summary(durations),
        "depth_updates_per_cpu_second": 3000 * 1e9 / cpu_ns,
        "two_market_idle_controller": summary(idle),
        "depth_traced_current_bytes": current,
        "depth_traced_peak_bytes": peak,
        "retained_levels": sum(len(s.prices) for s in engine.markets["BTC"].depth.sides.values()),
        "native_batch_to_decision": bridge_samples(engine, bids, asks),
    }
    print(json.dumps(result, indent=2))
    if len(sys.argv) == 2:
        Path(sys.argv[1]).write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

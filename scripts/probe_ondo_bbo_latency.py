"""Compare public BBO/depth receipts; no account, credentials, orders or dotenv."""

import argparse
import asyncio
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from decimal import Decimal
import json
import math
from pathlib import Path
import time

from websockets.asyncio.client import connect

ENDPOINT = "wss://api.ondoperps.xyz/ws"
BBO = "topOfBooksPerps"
DEPTH = "depthBooksPerps"
CHANNELS = (BBO, DEPTH)
MARKETS = ("BTC-USD.P", "ETH-USD.P")


def snapshot_key(item):
    """Only identical event timestamps AND best prices/sizes can form a pair."""
    market, stamp = item["market"], item["time"]
    if market not in MARKETS or not isinstance(stamp, str) or not stamp:
        raise ValueError("invalid market/time")
    bid, bid_size = map(Decimal, item["bids"][0])
    ask, ask_size = map(Decimal, item["asks"][0])
    if any(not v.is_finite() or v <= 0 for v in (bid, bid_size, ask, ask_size)) or bid >= ask:
        raise ValueError("invalid top of book")
    return market, stamp, bid, bid_size, ask, ask_size


def distribution(values):
    if not values:
        return {"samples": 0}
    rows = sorted(values)
    result = {"samples": len(rows), "min_us": rows[0] / 1000, "max_us": rows[-1] / 1000}
    for name, q in (("p50_us", .5), ("p95_us", .95), ("p99_us", .99)):
        result[name] = rows[max(0, math.ceil(q * len(rows)) - 1)] / 1000
    return result


class Matcher:
    def __init__(self, capacity=10_000, ttl_ns=5_000_000_000, sample_limit=100_000):
        self.capacity, self.ttl_ns, self.sample_limit = capacity, ttl_ns, sample_limit
        self.events = OrderedDict()
        self.counts = {m: Counter() for m in MARKETS}
        self.samples = {m: [] for m in MARKETS}

    def expire(self, now, *, flush=False):
        while self.events:
            key, entry = next(iter(self.events.items()))
            if not flush and now - entry["first"] <= self.ttl_ns and len(self.events) <= self.capacity:
                break
            self.events.popitem(last=False)
            if len(entry["receipts"]) == 1:
                channel = next(iter(entry["receipts"]))
                self.counts[key[0]]["unmatched_" + channel] += 1
                if not flush and now - entry["first"] <= self.ttl_ns:
                    self.counts[key[0]]["capacity_evictions"] += 1

    def add(self, channel, item, received_ns):
        key = snapshot_key(item)
        counts = self.counts[key[0]]
        self.expire(received_ns)
        entry = self.events.get(key)
        if entry is None:
            entry = {"first": received_ns, "receipts": {}}
            self.events[key] = entry
        receipts = entry["receipts"]
        if channel in receipts:
            counts["duplicate_" + channel] += 1
            return
        counts["unique_" + channel] += 1
        receipts[channel] = received_ns
        if len(receipts) == 2:
            delta = receipts[DEPTH] - receipts[BBO]
            counts["matched"] += 1
            counts["bbo_first" if delta > 0 else "depth_first" if delta < 0 else "tie"] += 1
            if len(self.samples[key[0]]) < self.sample_limit:
                self.samples[key[0]].append(delta)
            else:
                counts["samples_dropped"] += 1
        self.expire(received_ns)

    def result(self, now):
        self.expire(now, flush=True)
        return {
            m: {
                "counts": dict(c),
                "matched_fraction": {
                    channel: c["matched"] / c["unique_" + channel] if c["unique_" + channel] else 0
                    for channel in CHANNELS
                },
                "depth_minus_bbo_receipt": distribution(self.samples[m]),
            }
            for m, c in self.counts.items()
        }


class PriceTransitions:
    """Heuristic visibility comparison, deliberately separate from exact events."""

    def __init__(self, capacity=10_000, max_gap_ns=1_000_000_000):
        self.capacity, self.max_gap_ns = capacity, max_gap_ns
        self.previous = {}
        self.transitions = {}
        self.changes = {m: Counter() for m in MARKETS}

    def add(self, channel, item, received_ns):
        market, _stamp, bid, _bid_size, ask, _ask_size = snapshot_key(item)
        price = bid, ask
        previous = self.previous.get((market, channel))
        self.previous[market, channel] = price
        if previous is None or previous == price:
            return
        self.changes[market][channel] += 1
        key = market, previous, price
        entry = self.transitions.get(key)
        if entry is None:
            if len(self.transitions) >= self.capacity:
                self.changes[market]["capacity_dropped"] += 1
                return
            entry = {}
            self.transitions[key] = entry
        count, first = entry.get(channel, (0, received_ns))
        entry[channel] = count + 1, first

    def result(self):
        counts = {m: Counter() for m in MARKETS}
        samples = {m: [] for m in MARKETS}
        for (market, _previous, _price), entry in self.transitions.items():
            c = counts[market]
            if any(n != 1 for n, _ in entry.values()):
                c["excluded_repeated_transition_keys"] += 1
            elif len(entry) != 2:
                c["unpaired_transition_keys"] += 1
            else:
                delta = entry[DEPTH][1] - entry[BBO][1]
                if abs(delta) > self.max_gap_ns:
                    c["excluded_gap_over_one_second"] += 1
                    continue
                samples[market].append(delta)
                c["bbo_first" if delta > 0 else "depth_first" if delta < 0 else "tie"] += 1
        return {
            "method": "same preceding and next best bid/ask prices; transition occurs exactly once in each channel over the whole sample; receipt gap <=1s; sizes/timestamps not matched",
            "limitation": "Heuristic visibility of sampled price transitions, NOT proof of the same exchange event. Skipped/repeated changes and unmatched paths bias the subset.",
            "markets": {
                m: {"observed_changes": dict(self.changes[m]), "counts": dict(counts[m]),
                    "depth_minus_bbo_receipt": distribution(samples[m])}
                for m in MARKETS
            },
        }


async def collect(seconds, warmup, depth_first):
    matcher = Matcher()
    transitions = PriceTransitions()
    order = tuple(reversed(CHANNELS)) if depth_first else CHANNELS
    metrics = {c: Counter() for c in CHANNELS}
    examples = {c: [] for c in CHANNELS}
    acknowledgements, errors = [], []
    started = time.perf_counter()
    report = {
        "started_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint": ENDPOINT,
        "markets": MARKETS,
        "subscription_order": order,
        "requested_measurement_seconds": seconds,
        "warmup_seconds": warmup,
        "depth_limit": 1000,
        "transport": "one shared WebSocket, no compression, max_queue=16, max_size=2 MiB",
        "method": "strict match of market, event-time string and Decimal best prices AND sizes; receipt before JSON decode",
        "positive_delta_means": "BBO received earlier",
        "receipt_clock": {
            "name": "perf_counter_ns",
            "implementation": time.get_clock_info("perf_counter").implementation,
            "resolution_seconds": time.get_clock_info("perf_counter").resolution,
            "monotonic": time.get_clock_info("perf_counter").monotonic,
        },
        "limitations": [
            "Local application receipt includes scheduling, prior-frame parsing and shared TCP head-of-line effects.",
            "This is not exchange publication latency, wire arrival time or an independent-connection comparison.",
            "Only matched states are comparable; unmatched updates may bias the subset.",
            "Duplicates are suppressed within a bounded 5-second/10000-event window; each market retains at most 100000 samples.",
        ],
    }
    measurement_start = None
    completed = False
    try:
        async with connect(ENDPOINT, open_timeout=15, close_timeout=3, max_size=2**21,
                           max_queue=16, compression=None, ping_interval=20) as ws:
            for channel in order:
                message = {"op": "subscribe", "channel": channel, "markets": MARKETS}
                if channel == DEPTH:
                    message["limit"] = 1000
                await ws.send(json.dumps(message))
            measurement_start = time.perf_counter() + warmup
            deadline = measurement_start + seconds
            next_ping = time.perf_counter() + 20
            while time.perf_counter() < deadline:
                if time.perf_counter() >= next_ping:
                    await ws.send('{"op":"ping"}')
                    next_ping = time.perf_counter() + 20
                try:
                    raw = await asyncio.wait_for(ws.recv(), min(1, deadline - time.perf_counter()))
                except asyncio.TimeoutError:
                    continue
                received = time.perf_counter_ns()
                before = time.perf_counter_ns()
                frame = json.loads(raw)
                channel = frame.get("channel")
                if frame.get("type") == "error":
                    # Do not persist arbitrary server text or unsolicited payloads.
                    errors.append("server_error_frame")
                    break
                if frame.get("type") == "subscribed" and channel in CHANNELS:
                    if channel not in acknowledgements:
                        acknowledgements.append(channel)
                if frame.get("type") != "update" or channel not in CHANNELS:
                    continue
                if received / 1e9 < measurement_start or received / 1e9 >= deadline:
                    continue
                counters = metrics[channel]
                size = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
                counters["frames"] += 1
                counters["bytes"] += size
                counters["max_frame_bytes"] = max(counters["max_frame_bytes"], size)
                data = frame.get("data")
                if not isinstance(data, list):
                    counters["invalid_frames"] += 1
                    continue
                for item in data:
                    try:
                        matcher.add(channel, item, received)
                        transitions.add(channel, item, received)
                        counters["max_bid_levels"] = max(counters["max_bid_levels"], len(item["bids"]))
                        counters["max_ask_levels"] = max(counters["max_ask_levels"], len(item["asks"]))
                        if len(examples[channel]) < 4:
                            examples[channel].append({
                                "market": item["market"], "time": item["time"],
                                "bid": item["bids"][0], "ask": item["asks"][0],
                            })
                    except (KeyError, IndexError, ValueError, TypeError, ArithmeticError):
                        counters["invalid_snapshots"] += 1
                counters["decode_and_match_ns"] += time.perf_counter_ns() - before
            completed = time.perf_counter() >= deadline and not errors
    except (OSError, TimeoutError, ValueError) as exc:
        errors.append(type(exc).__name__)
    except Exception as exc:
        # WebSocket protocol/close errors still produce an explicitly incomplete report.
        errors.append(type(exc).__name__)
    ended = time.perf_counter()
    report.update(
        completed=completed,
        elapsed_seconds=ended - started,
        observed_measurement_seconds=max(0, min(seconds, ended - measurement_start)) if measurement_start else 0,
        acknowledgements=acknowledgements,
        errors=errors,
        channels={c: dict(metrics[c]) for c in CHANNELS},
        first_public_states=examples,
        comparison=matcher.result(time.perf_counter_ns()),
        price_transition_observation=transitions.result(),
    )
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--warmup", type=float, default=5)
    parser.add_argument("--depth-first", action="store_true")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or not 1 <= args.seconds <= 600:
        parser.error("seconds must be in 1..600")
    if not math.isfinite(args.warmup) or not 0 <= args.warmup <= 60:
        parser.error("warmup must be in 0..60")
    result = asyncio.run(collect(args.seconds, args.warmup, args.depth_first))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, indent=2))
    if not result["completed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

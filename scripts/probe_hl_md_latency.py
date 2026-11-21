#!/usr/bin/env python3
"""Read-only Hyperliquid market-data latency probe (l2Book, bbo, trades, multi-conn racing).

No credentials, no .env, no execution client, no orders.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter, OrderedDict
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, NamedTuple

from websockets.asyncio.client import connect

ENDPOINT = "wss://api.hyperliquid.xyz/ws"
DEFAULT_COINS = (
    "BTC",
    "ETH",
    "SOL",
    "HYPE",
    "XPL",
    "ASTER",
    "BIO",
    "MNT",
    "LIT",
    "BLUR",
    "APT",
    "MEME",
)
CHANNELS = ("l2Book", "bbo", "trades")
TOTAL_UNIVERSE_HL_COINS = 152
MAX_SUBSCRIPTIONS_SAFE_CAP = 900
HEARTBEAT_INTERVAL_SECS = 20.0
CONNECT_STAGGER_SECS = 0.2


def parse_coins(raw: str) -> tuple[str, ...]:
    """Parse and deduplicate comma-separated Hyperliquid coin symbols."""
    seen: list[str] = []
    for part in raw.split(","):
        coin = part.strip()
        if coin and coin not in seen:
            seen.append(coin)
    if not seen:
        raise ValueError("at least one coin must be specified")
    return tuple(seen)


def validate_probe_args(coins: tuple[str, ...], seconds: float, warmup: float, race_conns: int) -> None:
    """Validate CLI bounds and HL per-IP subscription budget before connecting."""
    if not coins:
        raise ValueError("at least one coin must be specified")
    if not math.isfinite(seconds) or not 5 <= seconds <= 600:
        raise ValueError("seconds must be in 5..600")
    if not math.isfinite(warmup) or not 0 <= warmup <= 60:
        raise ValueError("warmup must be in 0..60")
    if not 1 <= race_conns <= 4:
        raise ValueError("race_conns must be in 1..4")
    total_subs = race_conns * len(coins) * len(CHANNELS)
    if total_subs > MAX_SUBSCRIPTIONS_SAFE_CAP:
        raise ValueError(
            f"total subscriptions {total_subs} exceeds safe cap {MAX_SUBSCRIPTIONS_SAFE_CAP} "
            f"(HL per-IP limit is 1000)"
        )


def _positive_decimal(raw: Any, field_name: str) -> Decimal:
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        raise ValueError(f"invalid {field_name}")
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"invalid {field_name}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"non-positive {field_name}")
    return value


def _nonneg_int(raw: Any, field_name: str) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"invalid {field_name}")
    return raw


def parse_l2_book(
    data: Any,
) -> tuple[str, int, Decimal, Decimal, Decimal, Decimal, int]:
    """Validate and extract top-of-book state and depth from a Hyperliquid l2Book payload."""
    if not isinstance(data, dict):
        raise ValueError("l2Book data must be a dict")
    coin = data.get("coin")
    if not isinstance(coin, str) or not coin:
        raise ValueError("invalid l2Book coin")
    time_ms = _nonneg_int(data.get("time"), "l2Book.time")
    levels = data.get("levels")
    if not isinstance(levels, (list, tuple)) or len(levels) != 2:
        raise ValueError("l2Book levels must have 2 sides")
    bids, asks = levels[0], levels[1]
    if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
        raise ValueError("l2Book missing bid or ask levels")
    top_bid, top_ask = bids[0], asks[0]
    if not isinstance(top_bid, dict) or not isinstance(top_ask, dict):
        raise ValueError("invalid l2Book top level")
    bid_px = _positive_decimal(top_bid.get("px"), "l2Book.bid.px")
    bid_sz = _positive_decimal(top_bid.get("sz"), "l2Book.bid.sz")
    ask_px = _positive_decimal(top_ask.get("px"), "l2Book.ask.px")
    ask_sz = _positive_decimal(top_ask.get("sz"), "l2Book.ask.sz")
    if bid_px >= ask_px:
        raise ValueError("crossed l2Book top of book")
    return coin, time_ms, bid_px, bid_sz, ask_px, ask_sz, max(len(bids), len(asks))


def parse_bbo(
    data: Any,
) -> tuple[str, int, Decimal, Decimal, Decimal, Decimal] | None:
    """Validate and extract a Hyperliquid bbo payload; return None if one-sided."""
    if not isinstance(data, dict):
        raise ValueError("bbo data must be a dict")
    coin = data.get("coin")
    if not isinstance(coin, str) or not coin:
        raise ValueError("invalid bbo coin")
    time_ms = _nonneg_int(data.get("time"), "bbo.time")
    bbo = data.get("bbo")
    if not isinstance(bbo, (list, tuple)) or len(bbo) != 2:
        raise ValueError("bbo field must have 2 sides")
    bid, ask = bbo[0], bbo[1]
    if bid is None or ask is None:
        return None
    if not isinstance(bid, dict) or not isinstance(ask, dict):
        raise ValueError("invalid bbo level")
    bid_px = _positive_decimal(bid.get("px"), "bbo.bid.px")
    bid_sz = _positive_decimal(bid.get("sz"), "bbo.bid.sz")
    ask_px = _positive_decimal(ask.get("px"), "bbo.ask.px")
    ask_sz = _positive_decimal(ask.get("sz"), "bbo.ask.sz")
    if bid_px >= ask_px:
        raise ValueError("crossed bbo top of book")
    return coin, time_ms, bid_px, bid_sz, ask_px, ask_sz


def parse_trades(
    data: Any,
    *,
    min_time_ms: int | None = None,
) -> tuple[list[tuple[str, int, int, str, Decimal, Decimal]], int]:
    """Validate Hyperliquid trades payload; optionally skip historical replay trades."""
    if not isinstance(data, list):
        raise ValueError("trades data must be a list")
    parsed: list[tuple[str, int, int, str, Decimal, Decimal]] = []
    historical_skipped = 0
    for item in data:
        if not isinstance(item, dict):
            raise ValueError("trade item must be a dict")
        coin = item.get("coin")
        if not isinstance(coin, str) or not coin:
            raise ValueError("invalid trade coin")
        side = item.get("side")
        if side not in ("B", "A"):
            raise ValueError("invalid trade side")
        time_ms = _nonneg_int(item.get("time"), "trade.time")
        tid = _nonneg_int(item.get("tid"), "trade.tid")
        px = _positive_decimal(item.get("px"), "trade.px")
        sz = _positive_decimal(item.get("sz"), "trade.sz")
        if min_time_ms is not None and time_ms < min_time_ms:
            historical_skipped += 1
            continue
        parsed.append((coin, time_ms, tid, side, px, sz))
    return parsed, historical_skipped


def summarize_quantiles(values: list[int | float], *, suffix: str = "ms", scale: float = 1.0) -> dict[str, Any]:
    """Summarize min/p50/p95/p99/max/mean over a numeric sample list."""
    if not values:
        return {"samples": 0}
    rows = sorted(values)
    n = len(rows)
    mean_val = (sum(rows) / n) * scale
    result: dict[str, Any] = {
        "samples": n,
        f"min_{suffix}": round(rows[0] * scale, 4),
    }
    for name, q in ((f"p50_{suffix}", 0.5), (f"p95_{suffix}", 0.95), (f"p99_{suffix}", 0.99)):
        idx = max(0, math.ceil(q * n) - 1)
        result[name] = round(rows[idx] * scale, 4)
    result[f"max_{suffix}"] = round(rows[-1] * scale, 4)
    result[f"mean_{suffix}"] = round(mean_val, 4)
    return result


class BoundedSamples:
    """Fixed-capacity sample list with explicit overflow counter."""

    def __init__(self, limit: int = 200_000):
        self.limit = limit
        self.values: list[int | float] = []
        self.dropped = 0

    def append(self, value: int | float) -> None:
        if len(self.values) < self.limit:
            self.values.append(value)
        else:
            self.dropped += 1

    def summarize(self, *, suffix: str = "ms", scale: float = 1.0) -> dict[str, Any]:
        out = summarize_quantiles(self.values, suffix=suffix, scale=scale)
        if self.dropped:
            out["samples_dropped"] = self.dropped
        return out


class _LatestTop(NamedTuple):
    time_ms: int
    recv_mono_ns: int
    bid_px: Decimal
    bid_sz: Decimal
    ask_px: Decimal
    ask_sz: Decimal


def _pop_expired(
    events: OrderedDict[Any, dict[str, Any]],
    now_mono_ns: int,
    ttl_ns: int,
    capacity: int,
    flush: bool,
):
    """Yield `(popped_entry, evicted_for_capacity)` for expired or overflowing OrderedDict entries."""
    while events:
        _, entry = next(iter(events.items()))
        aged = now_mono_ns > entry["first_mono"] and (now_mono_ns - entry["first_mono"] > ttl_ns)
        over_cap = len(events) > capacity
        if not flush and not aged and not over_cap:
            break
        _, popped = events.popitem(last=False)
        yield popped, (not flush and not aged and over_cap)


class ThroughputTracker:
    """Per-channel frame/event/byte throughput, BBO price-vs-size churn, and 152-coin extrapolation."""

    def __init__(self, num_coins: int):
        if num_coins <= 0:
            raise ValueError("num_coins must be positive")
        self.num_coins = num_coins
        self.single_conn0 = self._new_channel_counters()
        self.all_conns = self._new_channel_counters()
        self._last_bbo_conn0: dict[str, tuple[Decimal, Decimal, Decimal, Decimal]] = {}

    @staticmethod
    def _new_channel_counters() -> dict[str, dict[str, int]]:
        return {
            c: {
                "frames": 0,
                "events": 0,
                "bytes": 0,
                "max_frame_bytes": 0,
                "decode_ns": 0,
                "invalid_frames": 0,
                "invalid_events": 0,
                "one_sided_bbo": 0,
                "historical_trades_skipped": 0,
                "max_book_levels": 0,
                "bbo_price_changed_events": 0,
                "bbo_size_only_events": 0,
                "bbo_unchanged_events": 0,
            }
            for c in CHANNELS
        }

    def _targets(self, conn_id: int) -> list[dict[str, dict[str, int]]]:
        return [self.all_conns, self.single_conn0] if conn_id == 0 else [self.all_conns]

    def record_frame(self, conn_id: int, channel: str, byte_len: int, decode_ns: int) -> None:
        for group in self._targets(conn_id):
            c = group[channel]
            c["frames"] += 1
            c["bytes"] += byte_len
            if byte_len > c["max_frame_bytes"]:
                c["max_frame_bytes"] = byte_len
            c["decode_ns"] += decode_ns

    def record_invalid(self, conn_id: int, channel: str, *, frame_level: bool = False) -> None:
        key = "invalid_frames" if frame_level else "invalid_events"
        for group in self._targets(conn_id):
            group[channel][key] += 1

    def record_one_sided_bbo(self, conn_id: int) -> None:
        for group in self._targets(conn_id):
            group["bbo"]["one_sided_bbo"] += 1

    def record_historical_trades_skipped(self, conn_id: int, count: int) -> None:
        if count <= 0:
            return
        for group in self._targets(conn_id):
            group["trades"]["historical_trades_skipped"] += count

    def record_l2_event(self, conn_id: int, max_levels: int) -> None:
        for group in self._targets(conn_id):
            c = group["l2Book"]
            c["events"] += 1
            if max_levels > c["max_book_levels"]:
                c["max_book_levels"] = max_levels

    def record_bbo_event(
        self,
        conn_id: int,
        coin: str,
        bid_px: Decimal,
        bid_sz: Decimal,
        ask_px: Decimal,
        ask_sz: Decimal,
    ) -> None:
        for group in self._targets(conn_id):
            group["bbo"]["events"] += 1
        if conn_id == 0:
            prev = self._last_bbo_conn0.get(coin)
            self._last_bbo_conn0[coin] = (bid_px, bid_sz, ask_px, ask_sz)
            if prev is None or (bid_px, ask_px) != (prev[0], prev[2]):
                self.single_conn0["bbo"]["bbo_price_changed_events"] += 1
                self.all_conns["bbo"]["bbo_price_changed_events"] += 1
            elif (bid_sz, ask_sz) != (prev[1], prev[3]):
                self.single_conn0["bbo"]["bbo_size_only_events"] += 1
                self.all_conns["bbo"]["bbo_size_only_events"] += 1
            else:
                self.single_conn0["bbo"]["bbo_unchanged_events"] += 1
                self.all_conns["bbo"]["bbo_unchanged_events"] += 1

    def record_trade_events(self, conn_id: int, count: int) -> None:
        if count <= 0:
            return
        for group in self._targets(conn_id):
            group["trades"]["events"] += count

    def _summarize_group(
        self,
        group: dict[str, dict[str, int]],
        observed_seconds: float,
        *,
        include_152_extrapolation: bool,
    ) -> dict[str, Any]:
        scale_152 = TOTAL_UNIVERSE_HL_COINS / self.num_coins
        out: dict[str, Any] = {}
        for channel in CHANNELS:
            c = dict(group[channel])
            frames = c["frames"]
            events = c["events"]
            c["avg_decode_us_per_frame"] = round((c["decode_ns"] / 1000.0) / frames, 3) if frames else 0.0
            if observed_seconds > 0:
                frames_per_sec = frames / observed_seconds
                events_per_sec = events / observed_seconds
                kb_per_sec = (c["bytes"] / 1024.0) / observed_seconds
                c["frames_per_sec"] = round(frames_per_sec, 3)
                c["events_per_sec"] = round(events_per_sec, 3)
                c["kb_per_sec"] = round(kb_per_sec, 3)
                if include_152_extrapolation:
                    c["est_152_coins_msg_per_sec"] = round(frames_per_sec * scale_152, 2)
                    c["est_152_coins_events_per_sec"] = round(events_per_sec * scale_152, 2)
                    c["est_152_coins_kb_per_sec"] = round(kb_per_sec * scale_152, 2)
            else:
                c["frames_per_sec"] = 0.0
                c["events_per_sec"] = 0.0
                c["kb_per_sec"] = 0.0
            if channel == "bbo" and events > 0:
                price_frac = c["bbo_price_changed_events"] / events
                c["bbo_price_changed_fraction"] = round(price_frac, 4)
                if include_152_extrapolation and observed_seconds > 0:
                    c["est_152_coins_price_changed_msg_per_sec"] = round(
                        (c["bbo_price_changed_events"] / observed_seconds) * scale_152, 2
                    )
            out[channel] = c
        return out

    def result(self, observed_seconds: float) -> dict[str, Any]:
        return {
            "num_probed_coins": self.num_coins,
            "extrapolation_target_coins": TOTAL_UNIVERSE_HL_COINS,
            "single_conn0": self._summarize_group(
                self.single_conn0, observed_seconds, include_152_extrapolation=True
            ),
            "all_conns_aggregate": self._summarize_group(
                self.all_conns, observed_seconds, include_152_extrapolation=False
            ),
        }


class RaceTracker:
    """Track per-event arrival racing across multiple WebSocket connections for one channel."""

    def __init__(
        self,
        channel: str,
        race_conns: int,
        *,
        capacity: int = 20_000,
        ttl_ns: int = 5_000_000_000,
        sample_limit: int = 200_000,
    ):
        self.channel = channel
        self.race_conns = race_conns
        self.capacity = capacity
        self.ttl_ns = ttl_ns
        self.sample_limit = sample_limit
        self.events: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()

        self.per_conn_source_lag: dict[int, BoundedSamples] = {
            i: BoundedSamples(sample_limit) for i in range(race_conns)
        }
        self.duplicate_per_conn: Counter[int] = Counter()
        self.fastest_source_lag = BoundedSamples(sample_limit)
        self.conn0_minus_fastest = BoundedSamples(sample_limit)
        self.conn0_minus_fastest_when_not_winner = BoundedSamples(sample_limit)
        self.slowest_minus_fastest = BoundedSamples(sample_limit)

        self.winner_counts: Counter[str] = Counter()
        self.unique_events = 0
        self.events_seen_by_multi_conns = 0
        self.events_seen_by_all_conns = 0
        self.events_seen_by_one_conn = 0
        self.capacity_evictions = 0

    def _finalize(self, entry: dict[str, Any]) -> None:
        receipts: dict[int, tuple[int, int]] = entry["receipts"]
        if not receipts:
            return
        self.unique_events += 1
        fastest_cid, (fastest_mono, fastest_lag_ns) = min(
            receipts.items(), key=lambda kv: (kv[1][0], kv[0])
        )
        self.fastest_source_lag.append(fastest_lag_ns)

        if len(receipts) == 1:
            self.events_seen_by_one_conn += 1
            return

        self.events_seen_by_multi_conns += 1
        if len(receipts) == self.race_conns:
            self.events_seen_by_all_conns += 1

        winners = [cid for cid, (m, _) in receipts.items() if m == fastest_mono]
        if len(winners) == 1:
            self.winner_counts[f"conn{winners[0]}"] += 1
        else:
            self.winner_counts["tie"] += 1

        slowest_mono = max(m for m, _ in receipts.values())
        self.slowest_minus_fastest.append(slowest_mono - fastest_mono)

        if 0 in receipts:
            conn0_mono = receipts[0][0]
            delta = conn0_mono - fastest_mono
            self.conn0_minus_fastest.append(delta)
            if delta > 0:
                self.conn0_minus_fastest_when_not_winner.append(delta)

    def expire(self, now_mono_ns: int, *, flush: bool = False) -> None:
        for popped, cap_evicted in _pop_expired(self.events, now_mono_ns, self.ttl_ns, self.capacity, flush):
            if cap_evicted:
                self.capacity_evictions += 1
            self._finalize(popped)

    def add(
        self,
        conn_id: int,
        key: tuple[Any, ...],
        time_ms: int,
        recv_mono_ns: int,
        recv_wall_ns: int,
    ) -> bool:
        """Record an event receipt on conn_id; return True if this is the global first arrival."""
        source_lag_ns = recv_wall_ns - time_ms * 1_000_000
        self.expire(recv_mono_ns)
        entry = self.events.get(key)
        is_first = entry is None
        if entry is None:
            entry = {"first_mono": recv_mono_ns, "receipts": {}}
            self.events[key] = entry
        receipts = entry["receipts"]
        if conn_id in receipts:
            self.duplicate_per_conn[conn_id] += 1
            return False
        receipts[conn_id] = (recv_mono_ns, source_lag_ns)
        self.per_conn_source_lag[conn_id].append(source_lag_ns)
        self.expire(recv_mono_ns)
        return is_first

    def result(self, now_mono_ns: int) -> dict[str, Any]:
        self.expire(now_mono_ns, flush=True)
        return {
            "channel": self.channel,
            "race_conns": self.race_conns,
            "unique_events": self.unique_events,
            "events_seen_by_one_conn": self.events_seen_by_one_conn,
            "events_seen_by_multi_conns": self.events_seen_by_multi_conns,
            "events_seen_by_all_conns": self.events_seen_by_all_conns,
            "capacity_evictions": self.capacity_evictions,
            "duplicate_per_conn": {f"conn{k}": v for k, v in sorted(self.duplicate_per_conn.items())},
            "winner_counts_multi_conn": dict(sorted(self.winner_counts.items())),
            "per_conn_source_lag_ms": {
                f"conn{cid}": buf.summarize(suffix="ms", scale=1e-6)
                for cid, buf in sorted(self.per_conn_source_lag.items())
            },
            "fastest_source_lag_ms": self.fastest_source_lag.summarize(suffix="ms", scale=1e-6),
            "conn0_minus_fastest_ms": self.conn0_minus_fastest.summarize(suffix="ms", scale=1e-6),
            "conn0_minus_fastest_when_not_winner_ms": (
                self.conn0_minus_fastest_when_not_winner.summarize(suffix="ms", scale=1e-6)
            ),
            "slowest_minus_fastest_ms": self.slowest_minus_fastest.summarize(suffix="ms", scale=1e-6),
        }


class CrossChannelTracker:
    """Compare l2Book, bbo, and trades arrival timing and staleness on one logical feed."""

    def __init__(
        self,
        *,
        capacity: int = 20_000,
        ttl_ns: int = 5_000_000_000,
        transition_max_gap_ns: int = 2_000_000_000,
        sample_limit: int = 200_000,
    ):
        self.capacity = capacity
        self.ttl_ns = ttl_ns
        self.transition_max_gap_ns = transition_max_gap_ns
        self.sample_limit = sample_limit

        self.source_lag: dict[str, BoundedSamples] = {c: BoundedSamples(sample_limit) for c in CHANNELS}
        self.per_coin_source_lag: dict[str, dict[str, BoundedSamples]] = {}

        # Exact state match (l2Book vs bbo)
        self._exact_events: OrderedDict[tuple[Any, ...], dict[str, Any]] = OrderedDict()
        self._exact_counts: Counter[str] = Counter()
        self._exact_l2_minus_bbo = BoundedSamples(sample_limit)

        # Same-block timestamp match ((coin, time_ms))
        self._block_events: OrderedDict[tuple[str, int], dict[str, Any]] = OrderedDict()
        self._block_pair_counts: dict[str, Counter[str]] = {
            "l2_minus_bbo": Counter(),
            "bbo_minus_trade": Counter(),
            "l2_minus_trade": Counter(),
        }
        self._block_deltas: dict[str, BoundedSamples] = {
            "l2_minus_bbo": BoundedSamples(sample_limit),
            "bbo_minus_trade": BoundedSamples(sample_limit),
            "l2_minus_trade": BoundedSamples(sample_limit),
        }
        self._block_capacity_evictions = 0

        # Unique price transitions (l2Book vs bbo)
        self._prev_prices: dict[tuple[str, str], tuple[Decimal, Decimal]] = {}
        self._transitions: dict[tuple[str, tuple[Decimal, Decimal], tuple[Decimal, Decimal]], dict[str, list[int]]] = {}
        self._transition_counts: Counter[str] = Counter()

        # Latest state for staleness & lead-to-next-L2 & trade-through audits
        self.latest_l2: dict[str, _LatestTop] = {}
        self.latest_bbo: dict[str, _LatestTop] = {}
        self.pending_bbo_lead: dict[str, tuple[int, int]] = {}  # coin -> (bbo_time_ms, bbo_recv_mono_ns)

        self.l2_arrivals_total = 0
        self.l2_arrivals_with_prior_bbo = 0
        self.l2_stale_vs_bbo_count = 0
        self.l2_stale_with_price_diff_count = 0
        self.l2_stale_time_lead_ms = BoundedSamples(sample_limit)
        self.l2_stale_mid_drift_bps = BoundedSamples(sample_limit)

        self.bbo_lead_to_next_l2 = BoundedSamples(sample_limit)

        self.trades_total = 0
        self.trades_newer_than_l2 = 0
        self.trades_crossing_l2 = 0
        self.trades_crossing_l2_with_newer_bbo = 0
        self.trades_crossing_l2_and_bbo = 0
        self.trades_newer_than_bbo = 0
        self.trades_crossing_bbo = 0

    def _record_source_lag(self, channel: str, coin: str, time_ms: int, recv_wall_ns: int) -> None:
        lag_ns = recv_wall_ns - time_ms * 1_000_000
        self.source_lag[channel].append(lag_ns)
        coin_map = self.per_coin_source_lag.get(coin)
        if coin_map is None:
            coin_map = {c: BoundedSamples(self.sample_limit) for c in CHANNELS}
            self.per_coin_source_lag[coin] = coin_map
        coin_map[channel].append(lag_ns)

    def _expire_exact(self, now_mono_ns: int, *, flush: bool = False) -> None:
        for popped, cap_evicted in _pop_expired(
            self._exact_events, now_mono_ns, self.ttl_ns, self.capacity, flush
        ):
            if cap_evicted:
                self._exact_counts["capacity_evictions"] += 1
            receipts: dict[str, int] = popped["receipts"]
            if len(receipts) == 2:
                delta = receipts["l2Book"] - receipts["bbo"]
                self._exact_counts["matched"] += 1
                self._exact_counts["bbo_first" if delta > 0 else "l2_first" if delta < 0 else "tie"] += 1
                self._exact_l2_minus_bbo.append(delta)
            elif len(receipts) == 1:
                only = next(iter(receipts))
                self._exact_counts[f"unmatched_{only}"] += 1

    def _add_exact(self, channel: str, key: tuple[Any, ...], recv_mono_ns: int) -> None:
        self._expire_exact(recv_mono_ns)
        entry = self._exact_events.get(key)
        if entry is None:
            entry = {"first_mono": recv_mono_ns, "receipts": {}}
            self._exact_events[key] = entry
        receipts = entry["receipts"]
        if channel in receipts:
            self._exact_counts[f"duplicate_{channel}"] += 1
            return
        self._exact_counts[f"unique_{channel}"] += 1
        receipts[channel] = recv_mono_ns
        self._expire_exact(recv_mono_ns)

    def _expire_block(self, now_mono_ns: int, *, flush: bool = False) -> None:
        for popped, cap_evicted in _pop_expired(
            self._block_events, now_mono_ns, self.ttl_ns, self.capacity, flush
        ):
            if cap_evicted:
                self._block_capacity_evictions += 1
            receipts: dict[str, int] = popped["receipts"]
            for name, first_ch, second_ch in (
                ("l2_minus_bbo", "l2Book", "bbo"),
                ("bbo_minus_trade", "bbo", "trades"),
                ("l2_minus_trade", "l2Book", "trades"),
            ):
                if first_ch in receipts and second_ch in receipts:
                    delta = receipts[first_ch] - receipts[second_ch]
                    self._block_deltas[name].append(delta)
                    c = self._block_pair_counts[name]
                    c["matched"] += 1
                    c["second_earlier" if delta > 0 else "first_earlier" if delta < 0 else "tie"] += 1

    def _add_block(self, channel: str, coin: str, time_ms: int, recv_mono_ns: int) -> None:
        self._expire_block(recv_mono_ns)
        key = (coin, time_ms)
        entry = self._block_events.get(key)
        if entry is None:
            entry = {"first_mono": recv_mono_ns, "receipts": {}}
            self._block_events[key] = entry
        receipts = entry["receipts"]
        if channel not in receipts or recv_mono_ns < receipts[channel]:
            receipts[channel] = recv_mono_ns
        self._expire_block(recv_mono_ns)

    def _add_transition(
        self, channel: str, coin: str, bid_px: Decimal, ask_px: Decimal, recv_mono_ns: int
    ) -> None:
        price = (bid_px, ask_px)
        prev = self._prev_prices.get((coin, channel))
        self._prev_prices[(coin, channel)] = price
        if prev is None or prev == price:
            return
        self._transition_counts[f"changes_{channel}"] += 1
        key = (coin, prev, price)
        entry = self._transitions.get(key)
        if entry is None:
            if len(self._transitions) >= self.capacity:
                self._transition_counts["capacity_dropped"] += 1
                return
            entry = {}
            self._transitions[key] = entry
        bucket = entry.setdefault(channel, [])
        if len(bucket) < 2:
            bucket.append(recv_mono_ns)

    def on_l2_book(
        self,
        coin: str,
        time_ms: int,
        bid_px: Decimal,
        bid_sz: Decimal,
        ask_px: Decimal,
        ask_sz: Decimal,
        recv_mono_ns: int,
        recv_wall_ns: int,
    ) -> None:
        self._record_source_lag("l2Book", coin, time_ms, recv_wall_ns)
        self._add_exact("l2Book", (coin, time_ms, bid_px, bid_sz, ask_px, ask_sz), recv_mono_ns)
        self._add_block("l2Book", coin, time_ms, recv_mono_ns)
        self._add_transition("l2Book", coin, bid_px, ask_px, recv_mono_ns)

        self.l2_arrivals_total += 1
        bbo = self.latest_bbo.get(coin)
        if bbo is not None:
            self.l2_arrivals_with_prior_bbo += 1
            if bbo.time_ms > time_ms:
                self.l2_stale_vs_bbo_count += 1
                self.l2_stale_time_lead_ms.append(bbo.time_ms - time_ms)
                if (bbo.bid_px, bbo.ask_px) != (bid_px, ask_px):
                    self.l2_stale_with_price_diff_count += 1
                    mid_l2 = (bid_px + ask_px) / Decimal(2)
                    mid_bbo = (bbo.bid_px + bbo.ask_px) / Decimal(2)
                    drift_bps = float(abs(mid_bbo - mid_l2) / mid_l2 * Decimal(10000))
                    self.l2_stale_mid_drift_bps.append(drift_bps)

        pending = self.pending_bbo_lead.get(coin)
        if pending is not None and time_ms >= pending[0]:
            self.pending_bbo_lead.pop(coin)
            self.bbo_lead_to_next_l2.append(recv_mono_ns - pending[1])

        prev_l2 = self.latest_l2.get(coin)
        if prev_l2 is None or time_ms >= prev_l2.time_ms:
            self.latest_l2[coin] = _LatestTop(time_ms, recv_mono_ns, bid_px, bid_sz, ask_px, ask_sz)

    def on_bbo(
        self,
        coin: str,
        time_ms: int,
        bid_px: Decimal,
        bid_sz: Decimal,
        ask_px: Decimal,
        ask_sz: Decimal,
        recv_mono_ns: int,
        recv_wall_ns: int,
    ) -> None:
        self._record_source_lag("bbo", coin, time_ms, recv_wall_ns)
        self._add_exact("bbo", (coin, time_ms, bid_px, bid_sz, ask_px, ask_sz), recv_mono_ns)
        self._add_block("bbo", coin, time_ms, recv_mono_ns)
        self._add_transition("bbo", coin, bid_px, ask_px, recv_mono_ns)

        l2 = self.latest_l2.get(coin)
        if (
            l2 is not None
            and time_ms >= l2.time_ms
            and (bid_px, ask_px) != (l2.bid_px, l2.ask_px)
            and coin not in self.pending_bbo_lead
        ):
            self.pending_bbo_lead[coin] = (time_ms, recv_mono_ns)

        prev_bbo = self.latest_bbo.get(coin)
        if prev_bbo is None or time_ms >= prev_bbo.time_ms:
            self.latest_bbo[coin] = _LatestTop(time_ms, recv_mono_ns, bid_px, bid_sz, ask_px, ask_sz)

    def on_trade(
        self,
        coin: str,
        time_ms: int,
        side: str,
        px: Decimal,
        recv_mono_ns: int,
        recv_wall_ns: int,
    ) -> None:
        self._record_source_lag("trades", coin, time_ms, recv_wall_ns)
        self._add_block("trades", coin, time_ms, recv_mono_ns)

        self.trades_total += 1
        l2 = self.latest_l2.get(coin)
        bbo = self.latest_bbo.get(coin)

        def crosses(top: _LatestTop) -> bool:
            return (side == "B" and px >= top.ask_px) or (side == "A" and px <= top.bid_px)

        if bbo is not None and time_ms > bbo.time_ms:
            self.trades_newer_than_bbo += 1
            if crosses(bbo):
                self.trades_crossing_bbo += 1

        if l2 is not None and time_ms > l2.time_ms:
            self.trades_newer_than_l2 += 1
            if crosses(l2):
                self.trades_crossing_l2 += 1
                if bbo is not None and time_ms > bbo.time_ms:
                    self.trades_crossing_l2_with_newer_bbo += 1
                    if crosses(bbo):
                        self.trades_crossing_l2_and_bbo += 1

    def _transition_result(self) -> dict[str, Any]:
        counts = Counter(self._transition_counts)
        deltas = BoundedSamples(self.sample_limit)
        for entry in self._transitions.values():
            if len(entry) != 2:
                counts["unpaired_transition_keys"] += 1
            elif len(entry["l2Book"]) != 1 or len(entry["bbo"]) != 1:
                counts["excluded_repeated_transition_keys"] += 1
            else:
                delta = entry["l2Book"][0] - entry["bbo"][0]
                if abs(delta) > self.transition_max_gap_ns:
                    counts["excluded_gap_over_max"] += 1
                    continue
                deltas.append(delta)
                counts["bbo_first" if delta > 0 else "l2_first" if delta < 0 else "tie"] += 1
        return {
            "counts": dict(counts),
            "l2_minus_bbo_ms": deltas.summarize(suffix="ms", scale=1e-6),
        }

    def result(self, now_mono_ns: int) -> dict[str, Any]:
        self._expire_exact(now_mono_ns, flush=True)
        self._expire_block(now_mono_ns, flush=True)

        exact_matched = self._exact_counts["matched"]
        u_l2 = self._exact_counts["unique_l2Book"]
        u_bbo = self._exact_counts["unique_bbo"]
        with_prior = self.l2_arrivals_with_prior_bbo

        return {
            "source_lag_ms_by_channel": {
                c: self.source_lag[c].summarize(suffix="ms", scale=1e-6) for c in CHANNELS
            },
            "source_lag_ms_by_coin": {
                coin: {
                    c: self.per_coin_source_lag[coin][c].summarize(suffix="ms", scale=1e-6)
                    for c in CHANNELS
                }
                for coin in sorted(self.per_coin_source_lag)
            },
            "exact_state_match_l2_vs_bbo": {
                "counts": dict(self._exact_counts),
                "matched_fraction": {
                    "l2Book": round(exact_matched / u_l2, 4) if u_l2 else 0.0,
                    "bbo": round(exact_matched / u_bbo, 4) if u_bbo else 0.0,
                },
                "l2_minus_bbo_ms": self._exact_l2_minus_bbo.summarize(suffix="ms", scale=1e-6),
            },
            "same_block_timestamp_match": {
                "capacity_evictions": self._block_capacity_evictions,
                "l2_minus_bbo": {
                    "counts": dict(self._block_pair_counts["l2_minus_bbo"]),
                    "delta_ms": self._block_deltas["l2_minus_bbo"].summarize(suffix="ms", scale=1e-6),
                    "positive_means": "bbo_earlier",
                },
                "bbo_minus_trade": {
                    "counts": dict(self._block_pair_counts["bbo_minus_trade"]),
                    "delta_ms": self._block_deltas["bbo_minus_trade"].summarize(suffix="ms", scale=1e-6),
                    "positive_means": "trades_earlier",
                },
                "l2_minus_trade": {
                    "counts": dict(self._block_pair_counts["l2_minus_trade"]),
                    "delta_ms": self._block_deltas["l2_minus_trade"].summarize(suffix="ms", scale=1e-6),
                    "positive_means": "trades_earlier",
                },
            },
            "unique_price_transitions_l2_vs_bbo": self._transition_result(),
            "stale_on_arrival_l2_vs_bbo": {
                "l2_arrivals_total": self.l2_arrivals_total,
                "l2_arrivals_with_prior_bbo": with_prior,
                "l2_stale_vs_bbo_count": self.l2_stale_vs_bbo_count,
                "l2_stale_vs_bbo_fraction": round(self.l2_stale_vs_bbo_count / with_prior, 4)
                if with_prior
                else 0.0,
                "l2_stale_with_price_diff_count": self.l2_stale_with_price_diff_count,
                "l2_stale_with_price_diff_fraction": round(
                    self.l2_stale_with_price_diff_count / with_prior, 4
                )
                if with_prior
                else 0.0,
                "bbo_time_minus_l2_time_ms": self.l2_stale_time_lead_ms.summarize(suffix="ms", scale=1.0),
                "mid_drift_bps": self.l2_stale_mid_drift_bps.summarize(suffix="bps", scale=1.0),
            },
            "bbo_price_change_lead_to_next_l2_ms": self.bbo_lead_to_next_l2.summarize(
                suffix="ms", scale=1e-6
            ),
            "trade_through_book_audit": {
                "trades_total": self.trades_total,
                "trades_newer_than_l2": self.trades_newer_than_l2,
                "trades_crossing_l2": self.trades_crossing_l2,
                "trades_crossing_l2_fraction_of_newer": round(
                    self.trades_crossing_l2 / self.trades_newer_than_l2, 4
                )
                if self.trades_newer_than_l2
                else 0.0,
                "trades_crossing_l2_with_newer_bbo": self.trades_crossing_l2_with_newer_bbo,
                "trades_crossing_l2_and_bbo": self.trades_crossing_l2_and_bbo,
                "trades_newer_than_bbo": self.trades_newer_than_bbo,
                "trades_crossing_bbo": self.trades_crossing_bbo,
                "trades_crossing_bbo_fraction_of_newer": round(
                    self.trades_crossing_bbo / self.trades_newer_than_bbo, 4
                )
                if self.trades_newer_than_bbo
                else 0.0,
            },
        }


def _peak_rss_bytes() -> int | None:
    try:
        import resource

        peak_rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak_rss if sys.platform == "darwin" else peak_rss * 1024)
    except ImportError:
        return None


def _peer_address(ws: Any) -> str | None:
    transport = getattr(ws, "transport", None)
    if transport is None:
        return None
    try:
        peer = transport.get_extra_info("peername")
        return str(peer) if peer is not None else None
    except Exception:
        return None


async def collect(
    coins: tuple[str, ...],
    seconds: float,
    warmup: float,
    race_conns: int,
    l2_fast: bool,
) -> dict[str, Any]:
    validate_probe_args(coins, seconds, warmup, race_conns)

    throughput = ThroughputTracker(len(coins))
    race_trackers = {c: RaceTracker(c, race_conns) for c in CHANNELS}
    single_conn0 = CrossChannelTracker()
    raced_fastest = CrossChannelTracker()

    acknowledgements: dict[str, Counter[str]] = {f"conn{i}": Counter() for i in range(race_conns)}
    peer_addresses: dict[str, str | None] = {}
    errors: list[str] = []

    started_utc = datetime.now(timezone.utc).isoformat()
    started_mono_ns = time.perf_counter_ns()
    started_cpu = time.process_time()

    stop_event = asyncio.Event()
    sockets: list[Any] = []
    warmup_end_mono_ns = 0
    deadline_mono_ns = 0
    measurement_start_wall_ms = 0

    def handle_frame(conn_id: int, raw: str | bytes, recv_mono_ns: int, recv_wall_ns: int) -> None:
        in_window = warmup_end_mono_ns <= recv_mono_ns < deadline_mono_ns
        byte_len = len(raw.encode("utf-8")) if isinstance(raw, str) else len(raw)
        decode_start = time.perf_counter_ns()
        try:
            frame = json.loads(raw)
        except Exception:
            if in_window:
                throughput.record_invalid(conn_id, "l2Book", frame_level=True)
            return
        if not isinstance(frame, dict):
            return

        channel = frame.get("channel")
        if channel == "error":
            errors.append(f"conn{conn_id}:server_error:{str(frame.get('data', ''))[:160]}")
            stop_event.set()
            return
        if channel == "subscriptionResponse":
            sub = frame.get("data", {}).get("subscription", {})
            sub_type = sub.get("type")
            if isinstance(sub_type, str):
                acknowledgements[f"conn{conn_id}"][sub_type] += 1
            return
        if channel not in CHANNELS or not in_window:
            return

        data = frame.get("data")
        if channel == "l2Book":
            try:
                coin, time_ms, bid_px, bid_sz, ask_px, ask_sz, max_levels = parse_l2_book(data)
            except ValueError:
                throughput.record_frame(conn_id, "l2Book", byte_len, time.perf_counter_ns() - decode_start)
                throughput.record_invalid(conn_id, "l2Book")
                return
            decode_ns = time.perf_counter_ns() - decode_start
            throughput.record_frame(conn_id, "l2Book", byte_len, decode_ns)
            throughput.record_l2_event(conn_id, max_levels)
            key = (coin, time_ms, bid_px, bid_sz, ask_px, ask_sz)
            is_first = race_trackers["l2Book"].add(conn_id, key, time_ms, recv_mono_ns, recv_wall_ns)
            if conn_id == 0:
                single_conn0.on_l2_book(
                    coin, time_ms, bid_px, bid_sz, ask_px, ask_sz, recv_mono_ns, recv_wall_ns
                )
            if is_first:
                raced_fastest.on_l2_book(
                    coin, time_ms, bid_px, bid_sz, ask_px, ask_sz, recv_mono_ns, recv_wall_ns
                )
        elif channel == "bbo":
            try:
                parsed = parse_bbo(data)
            except ValueError:
                throughput.record_frame(conn_id, "bbo", byte_len, time.perf_counter_ns() - decode_start)
                throughput.record_invalid(conn_id, "bbo")
                return
            decode_ns = time.perf_counter_ns() - decode_start
            throughput.record_frame(conn_id, "bbo", byte_len, decode_ns)
            if parsed is None:
                throughput.record_one_sided_bbo(conn_id)
                return
            coin, time_ms, bid_px, bid_sz, ask_px, ask_sz = parsed
            throughput.record_bbo_event(conn_id, coin, bid_px, bid_sz, ask_px, ask_sz)
            key = (coin, time_ms, bid_px, bid_sz, ask_px, ask_sz)
            is_first = race_trackers["bbo"].add(conn_id, key, time_ms, recv_mono_ns, recv_wall_ns)
            if conn_id == 0:
                single_conn0.on_bbo(
                    coin, time_ms, bid_px, bid_sz, ask_px, ask_sz, recv_mono_ns, recv_wall_ns
                )
            if is_first:
                raced_fastest.on_bbo(
                    coin, time_ms, bid_px, bid_sz, ask_px, ask_sz, recv_mono_ns, recv_wall_ns
                )
        elif channel == "trades":
            try:
                trades, skipped = parse_trades(data, min_time_ms=measurement_start_wall_ms - 5_000)
            except ValueError:
                throughput.record_frame(conn_id, "trades", byte_len, time.perf_counter_ns() - decode_start)
                throughput.record_invalid(conn_id, "trades")
                return
            decode_ns = time.perf_counter_ns() - decode_start
            throughput.record_frame(conn_id, "trades", byte_len, decode_ns)
            throughput.record_historical_trades_skipped(conn_id, skipped)
            throughput.record_trade_events(conn_id, len(trades))
            for coin, time_ms, tid, side, px, _sz in trades:
                is_first = race_trackers["trades"].add(
                    conn_id, (coin, tid), time_ms, recv_mono_ns, recv_wall_ns
                )
                if conn_id == 0:
                    single_conn0.on_trade(coin, time_ms, side, px, recv_mono_ns, recv_wall_ns)
                if is_first:
                    raced_fastest.on_trade(coin, time_ms, side, px, recv_mono_ns, recv_wall_ns)

    async def reader_loop(conn_id: int, ws: Any) -> None:
        next_ping = time.monotonic() + HEARTBEAT_INTERVAL_SECS
        while not stop_event.is_set():
            now_mono_ns = time.perf_counter_ns()
            if deadline_mono_ns and now_mono_ns >= deadline_mono_ns:
                stop_event.set()
                break
            now_m = time.monotonic()
            if now_m >= next_ping:
                await ws.send('{"method":"ping"}')
                next_ping = now_m + HEARTBEAT_INTERVAL_SECS
            remaining = ((deadline_mono_ns - now_mono_ns) / 1e9) if deadline_mono_ns else 1.0
            timeout = max(0.05, min(1.0, remaining))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                continue
            recv_mono_ns = time.perf_counter_ns()
            recv_wall_ns = time.time_ns()
            handle_frame(conn_id, raw, recv_mono_ns, recv_wall_ns)

    tasks: list[asyncio.Task[None]] = []
    try:
        for conn_id in range(race_conns):
            if conn_id > 0:
                await asyncio.sleep(CONNECT_STAGGER_SECS)
            ws = await connect(
                ENDPOINT,
                open_timeout=15,
                close_timeout=3,
                max_size=2**22,
                max_queue=4096,
                compression=None,
                ping_interval=20,
            )
            sockets.append(ws)
            peer_addresses[f"conn{conn_id}"] = _peer_address(ws)
            for coin in coins:
                l2_sub: dict[str, Any] = {"type": "l2Book", "coin": coin}
                if l2_fast:
                    l2_sub["fast"] = True
                for sub in (
                    l2_sub,
                    {"type": "bbo", "coin": coin},
                    {"type": "trades", "coin": coin},
                ):
                    await ws.send(json.dumps({"method": "subscribe", "subscription": sub}))

        now_mono = time.perf_counter_ns()
        now_wall = time.time_ns()
        warmup_end_mono_ns = now_mono + int(warmup * 1_000_000_000)
        deadline_mono_ns = warmup_end_mono_ns + int(seconds * 1_000_000_000)
        measurement_start_wall_ms = (now_wall + int(warmup * 1_000_000_000)) // 1_000_000

        for conn_id, ws in enumerate(sockets):
            tasks.append(asyncio.create_task(reader_loop(conn_id, ws)))

        async def stop_timer() -> None:
            wait_s = max(0.0, (deadline_mono_ns - time.perf_counter_ns()) / 1e9)
            await asyncio.sleep(wait_s)
            stop_event.set()

        timer_task = asyncio.create_task(stop_timer())
        done, pending = await asyncio.wait(
            [*tasks, timer_task],
            return_when=asyncio.FIRST_COMPLETED,
        )
        for t in done:
            exc = t.exception()
            if exc is not None:
                errors.append(f"task_error:{type(exc).__name__}:{exc}")
        stop_event.set()
        for t in pending:
            t.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
    except Exception as exc:
        errors.append(f"{type(exc).__name__}:{exc}")
    finally:
        for t in tasks:
            if not t.done():
                t.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for ws in sockets:
            try:
                await ws.close()
            except Exception:
                pass

    end_mono_ns = time.perf_counter_ns()
    cpu_secs = time.process_time() - started_cpu
    elapsed_secs = (end_mono_ns - started_mono_ns) / 1e9
    if warmup_end_mono_ns:
        observed_secs = max(0.0, min(seconds, (min(end_mono_ns, deadline_mono_ns) - warmup_end_mono_ns) / 1e9))
    else:
        observed_secs = 0.0
    completed = bool(deadline_mono_ns and end_mono_ns >= deadline_mono_ns and not errors)

    return {
        "schema_version": 1,
        "started_at_utc": started_utc,
        "finished_at_utc": datetime.now(timezone.utc).isoformat(),
        "endpoint": ENDPOINT,
        "coins": list(coins),
        "l2_fast": l2_fast,
        "race_conns": race_conns,
        "subscriptions_per_conn": len(coins) * len(CHANNELS),
        "total_subscriptions": race_conns * len(coins) * len(CHANNELS),
        "requested_seconds": seconds,
        "warmup_seconds": warmup,
        "observed_measurement_seconds": round(observed_secs, 3),
        "elapsed_seconds": round(elapsed_secs, 3),
        "completed": completed,
        "errors": errors,
        "peer_addresses": peer_addresses,
        "acknowledgements": {k: dict(v) for k, v in acknowledgements.items()},
        "process": {
            "cpu_count": os.cpu_count(),
            "process_cpu_secs": round(cpu_secs, 4),
            "avg_cpu_percent": round(100.0 * cpu_secs / elapsed_secs, 2) if elapsed_secs > 0 else 0.0,
            "peak_rss_bytes": _peak_rss_bytes(),
            "clocks": {
                "perf_counter": {
                    "implementation": time.get_clock_info("perf_counter").implementation,
                    "resolution_s": time.get_clock_info("perf_counter").resolution,
                    "monotonic": time.get_clock_info("perf_counter").monotonic,
                },
                "time": {
                    "implementation": time.get_clock_info("time").implementation,
                    "resolution_s": time.get_clock_info("time").resolution,
                    "monotonic": time.get_clock_info("time").monotonic,
                },
            },
        },
        "throughput": throughput.result(observed_secs),
        "racing": {c: race_trackers[c].result(end_mono_ns) for c in CHANNELS},
        "cross_channel": {
            "single_conn0": single_conn0.result(end_mono_ns),
            "raced_fastest": raced_fastest.result(end_mono_ns),
        },
        "limitations": [
            "source_lag_ms = recv_wall_ns - venue_time_ms*1e6 includes local NTP clock offset vs Hyperliquid server clock (all cross-channel and multi-conn deltas use monotonic perf_counter_ns and cancel clock offset).",
            "Selected coins are active major/target symbols; linear 152-coin extrapolation assumes uniform rate and therefore acts as a conservative upper bound over long-tail coins.",
            "Python asyncio reader tasks share one event loop; local JSON decode time on prior frames adds small queuing jitter to subsequent recv timestamps.",
            "All connections originate from the same host IP; racing only captures diversity across sockets/routes if Cloudflare/backend routing differs.",
        ],
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--coins",
        default=",".join(DEFAULT_COINS),
        help="Comma-separated HL coin symbols to probe",
    )
    parser.add_argument("--seconds", type=float, default=180.0)
    parser.add_argument("--warmup", type=float, default=10.0)
    parser.add_argument("--race-conns", type=int, default=3)
    parser.add_argument("--no-l2-fast", action="store_true")
    parser.add_argument("--output", type=Path)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        coins = parse_coins(args.coins)
        validate_probe_args(coins, args.seconds, args.warmup, args.race_conns)
    except ValueError as exc:
        parser.error(str(exc))

    report = asyncio.run(
        collect(
            coins=coins,
            seconds=args.seconds,
            warmup=args.warmup,
            race_conns=args.race_conns,
            l2_fast=not args.no_l2_fast,
        )
    )
    rendered = json.dumps(report, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if report["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Rolling basis baseline admission and adaptive max_hold route blocking.

Pure strategy/risk policy helpers with no exchange, Nautilus, network, or I/O
imports. Keeps the rolling basis estimator cleanly decoupled so future carry /
funding-rate models (#109) can shift or refine the expected baseline via
``carry_offset_bps``.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction as F
from itertools import combinations
import math
import statistics

from opportunity_core import _best_quotes_rule_out, _book_valid
from opportunity_scan import ScanConfigError, decimal_value, integer_value


def route_block_reject(
    tracker: "RouteMaxHoldTracker",
    markets: dict,
    books: dict,
    metadata: dict,
    settings,
    buy_id: str,
    sell_id: str,
    now_ns: int,
    analytics,
    stage: str,
) -> bool:
    """Return True if directed route is in max_hold cooldown/block, recording pre-gate & funnel telemetry."""
    route_block, _ = tracker.active_block(buy_id, sell_id, now_ns)
    if route_block is None:
        return False
    if analytics is not None:
        reason = "route_max_hold_block" if route_block["blocked"] else "route_max_hold_cooldown"
        analytics.record_pre_gate(reason)
        if (buy_id in books and sell_id in books and buy_id in metadata and sell_id in metadata
                and not _best_quotes_rule_out(
                    books[buy_id], metadata[buy_id], books[sell_id], metadata[sell_id], settings)):
            bm, sm = markets[buy_id], markets[sell_id]
            analytics.record_funnel(bm.symbol, bm.venue, sm.venue, stage, detail=reason)
    return True


def symbol_venue_pairs(groups: dict[str, list[str]], markets: dict) -> dict[str, tuple[tuple[str, str], ...]]:
    """Precompute canonical unordered cross-venue instrument pairs per symbol."""
    return {
        symbol: tuple(
            (first, second)
            for first, second in combinations(sorted(ids), 2)
            if markets[first].venue != markets[second].venue
        )
        for symbol, ids in groups.items()
    }


def route_label(symbol: str, buy_venue: str, sell_venue: str) -> str:
    """Return canonical route string matching opportunity_analytics conventions."""
    buy_v = "HL" if buy_venue == "HYPERLIQUID" else buy_venue
    sell_v = "HL" if sell_venue == "HYPERLIQUID" else sell_venue
    return f"{symbol}|{buy_v}->{sell_v}"


@dataclass(frozen=True)
class BasisFilterPolicy:
    """Configuration for rolling basis deviation admission and max_hold route blocks."""

    enabled: bool = False
    window_secs: int = 600
    warmup_secs: int = 120
    min_deviation_bps: Decimal | None = Decimal("20")
    max_hold_loss_cooldown_secs: int = 900
    max_hold_loss_block_strikes: int = 2
    max_hold_loss_block_secs: int = 3600

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ScanConfigError("basis_filter.enabled must be boolean")
        window = integer_value(self.window_secs, "basis_filter.window_secs", 10, 86400)
        warmup = integer_value(self.warmup_secs, "basis_filter.warmup_secs", 1, 86400)
        if warmup > window:
            raise ScanConfigError("basis_filter.warmup_secs must not exceed window_secs")
        if self.min_deviation_bps is not None:
            dev = decimal_value(self.min_deviation_bps, "basis_filter.min_deviation_bps", positive=True)
            if dev >= 10000:
                raise ScanConfigError("basis_filter.min_deviation_bps must be below 10000")
            object.__setattr__(self, "min_deviation_bps", dev)
        cooldown = integer_value(
            self.max_hold_loss_cooldown_secs, "basis_filter.max_hold_loss_cooldown_secs", 0, 86400
        )
        integer_value(self.max_hold_loss_block_strikes, "basis_filter.max_hold_loss_block_strikes", 1, 100)
        block = integer_value(
            self.max_hold_loss_block_secs, "basis_filter.max_hold_loss_block_secs", 0, 86400
        )
        if cooldown > 0 and block > 0 and block < cooldown:
            raise ScanConfigError(
                "basis_filter.max_hold_loss_block_secs must be at least max_hold_loss_cooldown_secs"
            )

    @property
    def deviation_enabled(self) -> bool:
        return self.enabled and self.min_deviation_bps is not None

    @property
    def max_hold_guard_enabled(self) -> bool:
        return self.enabled and (self.max_hold_loss_cooldown_secs > 0 or self.max_hold_loss_block_secs > 0)

    @property
    def sample_interval_secs(self) -> int:
        return max(1, min(max(1, self.window_secs // 120), max(1, self.warmup_secs // 3)))

    def document(self) -> dict:
        return {
            "enabled": self.enabled,
            "window_secs": self.window_secs,
            "warmup_secs": self.warmup_secs,
            "min_deviation_bps": None if self.min_deviation_bps is None else str(self.min_deviation_bps),
            "max_hold_loss_cooldown_secs": self.max_hold_loss_cooldown_secs,
            "max_hold_loss_block_strikes": self.max_hold_loss_block_strikes,
            "max_hold_loss_block_secs": self.max_hold_loss_block_secs,
            "sample_interval_secs": self.sample_interval_secs,
        }


@dataclass(frozen=True)
class BasisEstimate:
    """Point-in-time basis deviation evaluation attached to admitted entries."""

    baseline_bps: float | None
    current_spread_bps: float
    deviation_bps: float | None
    samples: int
    span_secs: float
    ready: bool
    source: str = "rolling_median_mid"

    def document(self) -> dict:
        def _rounded(value: float | None) -> float | None:
            if value is None or not math.isfinite(value):
                return None
            return round(value, 4)

        return {
            "baseline_bps": _rounded(self.baseline_bps),
            "current_spread_bps": _rounded(self.current_spread_bps),
            "deviation_bps": _rounded(self.deviation_bps),
            "samples": self.samples,
            "span_secs": _rounded(self.span_secs),
            "ready": self.ready,
            "source": self.source,
        }


def mid_basis_bps(buy_book, sell_book) -> float | None:
    """Compute antisymmetric mid-to-mid basis in bps: (sell_mid - buy_mid) / avg_mid * 10000."""
    if not buy_book.bids or not buy_book.asks or not sell_book.bids or not sell_book.asks:
        return None
    buy_mid = (float(buy_book.bids[0][0]) + float(buy_book.asks[0][0])) * 0.5
    sell_mid = (float(sell_book.bids[0][0]) + float(sell_book.asks[0][0])) * 0.5
    avg_mid = (buy_mid + sell_mid) * 0.5
    if not (math.isfinite(avg_mid) and avg_mid > 0.0):
        return None
    basis = (sell_mid - buy_mid) / avg_mid * 10000.0
    return basis if math.isfinite(basis) else None


class _PairBasisWindow:
    __slots__ = ("samples", "last_sample_ns", "cached_median")

    def __init__(self, maxlen: int) -> None:
        self.samples: deque[tuple[int, float]] = deque(maxlen=maxlen)
        self.last_sample_ns: int = -1
        self.cached_median: float | None = None


class RollingBasisTracker:
    """Bounded per-pair sliding median mid-basis estimator."""

    MIN_WARMUP_SAMPLES = 3

    def __init__(self, policy: BasisFilterPolicy) -> None:
        self.policy = policy
        self.window_ns = policy.window_secs * 1_000_000_000
        self.warmup_ns = policy.warmup_secs * 1_000_000_000
        self.sample_interval_ns = policy.sample_interval_secs * 1_000_000_000
        # Retain one extra sample interval so steady-state ticks never flap warmup when warmup_secs == window_secs.
        self.retention_ns = self.window_ns + self.sample_interval_ns
        self.max_gap_ns = max(60_000_000_000, self.warmup_ns)
        self.max_samples = (policy.window_secs // policy.sample_interval_secs) + 4
        self._windows: dict[tuple[str, str], _PairBasisWindow] = {}
        self._symbol_earliest_next_ns: dict[str, int] = {}

    def observe_pair(self, id_a: str, book_a, id_b: str, book_b, settings, now_ns: int) -> bool:
        """Sample valid, low-skew mid basis into the canonical unordered pair window."""
        if id_a == id_b or book_a is None or book_b is None:
            return False
        if id_a > id_b:
            id_a, id_b = id_b, id_a
            book_a, book_b = book_b, book_a
        key = (id_a, id_b)
        win = self._windows.get(key)
        if win is not None and (now_ns <= win.last_sample_ns or now_ns - win.last_sample_ns < self.sample_interval_ns):
            return False
        if not _book_valid(book_a, settings, now_ns) or not _book_valid(book_b, settings, now_ns):
            return False
        if abs(book_a.ts_event_ns - book_b.ts_event_ns) > settings.max_skew_ms * 1_000_000:
            return False
        basis = mid_basis_bps(book_a, book_b)
        if basis is None:
            return False
        if win is None:
            win = _PairBasisWindow(self.max_samples)
            self._windows[key] = win
        cutoff = now_ns - self.retention_ns
        while win.samples and win.samples[0][0] < cutoff:
            win.samples.popleft()
        win.samples.append((now_ns, basis))
        win.last_sample_ns = now_ns
        win.cached_median = None
        return True

    def observe_symbol(
        self,
        symbol_pairs,
        books,
        settings,
        now_ns: int,
        *,
        affected=None,
        symbol: str | None = None,
    ) -> int:
        """Observe all matching unordered cross-venue pairs for one symbol."""
        if not symbol_pairs:
            return 0
        if symbol is not None:
            next_ns = self._symbol_earliest_next_ns.get(symbol)
            if next_ns is not None and now_ns < next_ns:
                return 0
        recorded = 0
        for first, second in symbol_pairs:
            if affected is not None and first not in affected and second not in affected:
                continue
            key = (first, second) if first < second else (second, first)
            win = self._windows.get(key)
            if win is not None and (now_ns <= win.last_sample_ns or now_ns - win.last_sample_ns < self.sample_interval_ns):
                continue
            book_a = books.get(first)
            book_b = books.get(second)
            if book_a is not None and book_b is not None:
                if self.observe_pair(first, book_a, second, book_b, settings, now_ns):
                    recorded += 1
        if symbol is not None:
            earliest_next = None
            for first, second in symbol_pairs:
                key = (first, second) if first < second else (second, first)
                win = self._windows.get(key)
                if win is None:
                    earliest_next = None
                    break
                candidate_next = win.last_sample_ns + self.sample_interval_ns
                if earliest_next is None or candidate_next < earliest_next:
                    earliest_next = candidate_next
            if earliest_next is not None and earliest_next > now_ns:
                self._symbol_earliest_next_ns[symbol] = earliest_next
            else:
                self._symbol_earliest_next_ns.pop(symbol, None)
        return recorded

    def bbo_rule_out(
        self,
        buy_id: str,
        sell_id: str,
        buy_book,
        sell_book,
        now_ns: int,
        *,
        carry_offset_bps: float = 0.0,
    ) -> str | None:
        """Conservatively rule out entries using best-touch gross spread before full L2 sizing."""
        if not buy_book.asks or not sell_book.bids:
            return None
        buy_ask = float(buy_book.asks[0][0])
        sell_bid = float(sell_book.bids[0][0])
        if not (math.isfinite(buy_ask) and math.isfinite(sell_bid) and buy_ask > 0.0):
            return None
        bbo_gross_bps = (sell_bid - buy_ask) / buy_ask * 10000.0 + 1e-6
        reason, _ = self.evaluate_entry(
            buy_id, sell_id, bbo_gross_bps, now_ns, carry_offset_bps=carry_offset_bps,
        )
        return reason

    def baseline(self, buy_id: str, sell_id: str, now_ns: int) -> tuple[float | None, int, float, bool]:
        """Return (baseline_bps, sample_count, span_secs, ready) for directed buy_id -> sell_id."""
        if buy_id == sell_id:
            return None, 0, 0.0, False
        key = (buy_id, sell_id) if buy_id < sell_id else (sell_id, buy_id)
        sign = 1.0 if buy_id < sell_id else -1.0
        win = self._windows.get(key)
        if win is None:
            return None, 0, 0.0, False
        cutoff = now_ns - self.retention_ns
        while win.samples and win.samples[0][0] < cutoff:
            win.samples.popleft()
            win.cached_median = None
        if not win.samples:
            return None, 0, 0.0, False
        count = len(win.samples)
        span_ns = max(0, now_ns - win.samples[0][0])
        gap_ns = now_ns - win.samples[-1][0]
        ready = count >= self.MIN_WARMUP_SAMPLES and span_ns >= self.warmup_ns and 0 <= gap_ns <= self.max_gap_ns
        if win.cached_median is None:
            win.cached_median = float(statistics.median(val for _, val in win.samples))
        median_val = sign * win.cached_median
        if median_val == 0.0:
            median_val = 0.0
        return median_val, count, span_ns / 1e9, ready

    def evaluate_entry(
        self,
        buy_id: str,
        sell_id: str,
        current_spread_bps,
        now_ns: int,
        *,
        carry_offset_bps: float = 0.0,
    ) -> tuple[str | None, BasisEstimate]:
        """Check whether current_spread_bps exceeds the route's baseline + carry offset by min_deviation_bps."""
        spread = float(current_spread_bps)
        baseline_val, count, span_secs, ready = self.baseline(buy_id, sell_id, now_ns)
        if not ready or baseline_val is None:
            return "basis_warmup", BasisEstimate(baseline_val, spread, None, count, span_secs, False)
        effective_baseline = baseline_val + float(carry_offset_bps)
        deviation = spread - effective_baseline
        est = BasisEstimate(effective_baseline, spread, deviation, count, span_secs, True)
        if self.policy.min_deviation_bps is not None and deviation < float(self.policy.min_deviation_bps):
            return "basis_deviation_insufficient", est
        return None, est

    def summary(self, now_ns: int | None = None) -> dict:
        doc = {
            "tracked_pairs": len(self._windows),
            "window_secs": self.policy.window_secs,
            "warmup_secs": self.policy.warmup_secs,
            "sample_interval_secs": self.policy.sample_interval_secs,
        }
        if now_ns is not None:
            doc["warmed_pairs"] = sum(
                1 for id_lo, id_hi in tuple(self._windows) if self.baseline(id_lo, id_hi, now_ns)[3]
            )
        return doc


class _InstSpreadWindow:
    __slots__ = ("samples", "last_sample_ns", "cached_median")

    def __init__(self, maxlen: int) -> None:
        self.samples: deque[tuple[int, float]] = deque(maxlen=maxlen)
        self.last_sample_ns: int = -1
        self.cached_median: float | None = None


class RollingLegSpreadTracker:
    """Bounded per-instrument sliding median bid-ask spread tracker (1 Hz sampled)."""

    def __init__(
        self,
        window_secs: int = 300,
        min_samples: int = 10,
        sample_interval_secs: int = 1,
    ) -> None:
        self.window_secs = window_secs
        self.min_samples = min_samples
        self.sample_interval_secs = sample_interval_secs
        self.window_ns = window_secs * 1_000_000_000
        self.sample_interval_ns = sample_interval_secs * 1_000_000_000
        self.max_samples = (window_secs // sample_interval_secs) + 4
        self._samples: dict[str, _InstSpreadWindow] = {}

    def observe_instruments(self, instrument_ids, books, settings, now_ns: int) -> int:
        """Sample valid books at most once per sample_interval_secs per instrument."""
        recorded = 0
        cutoff = now_ns - self.window_ns
        for instrument_id in instrument_ids:
            win = self._samples.get(instrument_id)
            if win is not None and (
                now_ns <= win.last_sample_ns
                or now_ns - win.last_sample_ns < self.sample_interval_ns
            ):
                continue
            book = books.get(instrument_id)
            if book is None or not _book_valid(book, settings, now_ns):
                continue
            if not book.bids or not book.asks:
                continue
            bid0 = float(book.bids[0][0])
            ask0 = float(book.asks[0][0])
            if not (math.isfinite(bid0) and math.isfinite(ask0) and bid0 > 0.0 and ask0 >= bid0):
                continue
            spread_bps = (ask0 - bid0) / bid0 * 10000.0
            if not math.isfinite(spread_bps):
                continue
            if win is None:
                win = _InstSpreadWindow(self.max_samples)
                self._samples[instrument_id] = win
            while win.samples and win.samples[0][0] < cutoff:
                win.samples.popleft()
            win.samples.append((now_ns, spread_bps))
            win.last_sample_ns = now_ns
            win.cached_median = None
            recorded += 1
        return recorded

    def median_bps(self, instrument_id: str, now_ns: int) -> float | None:
        """Return rolling median bid-ask spread in bps, or None during warmup."""
        win = self._samples.get(instrument_id)
        if win is None:
            return None
        cutoff = now_ns - self.window_ns
        while win.samples and win.samples[0][0] < cutoff:
            win.samples.popleft()
            win.cached_median = None
        if len(win.samples) < self.min_samples:
            return None
        if win.cached_median is None:
            win.cached_median = float(statistics.median(val for _, val in win.samples))
        return win.cached_median

    def evaluate_pair(self, buy_id: str, sell_id: str, max_bps: float, now_ns: int) -> str | None:
        """Fail closed if either leg lacks warmup samples or its rolling median exceeds max_bps."""
        buy_med = self.median_bps(buy_id, now_ns)
        sell_med = self.median_bps(sell_id, now_ns)
        if buy_med is None or sell_med is None:
            return "rolling_leg_spread_warmup"
        if buy_med > max_bps or sell_med > max_bps:
            return "rolling_leg_spread_wide"
        return None

    def summary(self) -> dict:
        ready = sum(1 for win in self._samples.values() if len(win.samples) >= self.min_samples)
        return {
            "window_secs": self.window_secs,
            "min_samples": self.min_samples,
            "tracked_instruments": len(self._samples),
            "ready_instruments": ready,
        }


def _spread_convergence(economics: dict | None) -> tuple[float | None, float | None, float | None]:
    if not isinstance(economics, dict):
        return None, None, None
    entry = economics.get("actual_entry") or economics.get("paper_fills")
    if not isinstance(entry, dict):
        return None, None, None
    try:
        buy_in = F(entry.get("buy_notional_usd", "0"))
        sell_in = F(entry.get("sell_notional_usd", "0"))
        close_long = F(economics.get("closing_long_cash_usd", "0"))
        close_short = F(economics.get("closing_short_cash_usd", "0"))
    except (TypeError, ValueError):
        return None, None, None
    if buy_in <= 0 or sell_in <= 0 or close_long <= 0 or close_short <= 0:
        return None, None, None
    entry_spread = round(float((sell_in - buy_in) * 10000 / buy_in), 4)
    exit_spread = round(float((close_short - close_long) * 10000 / buy_in), 4)
    convergence = round(entry_spread - exit_spread, 4)
    return entry_spread, exit_spread, convergence


class RouteMaxHoldTracker:
    """Adaptive per-route cooldown/block escalation for repeated max_hold losses."""

    def __init__(self, policy: BasisFilterPolicy) -> None:
        self.policy = policy
        self.reset_window_ns = max(policy.max_hold_loss_block_secs, policy.max_hold_loss_cooldown_secs) * 1_000_000_000
        self.entries: dict[tuple[str, str], dict] = {}

    def active_block(self, buy_id: str, sell_id: str, now_ns: int) -> tuple[dict | None, dict | None]:
        """Return (active_entry, expired_blocked_entry) for directed route (buy_id, sell_id)."""
        key = (buy_id, sell_id)
        entry = self.entries.get(key)
        if entry is None:
            return None, None
        if now_ns < entry["until_ns"]:
            return entry, None
        if entry["blocked"]:
            self.entries.pop(key, None)
            return None, entry
        if self.reset_window_ns > 0 and now_ns - entry["triggered_ns"] > self.reset_window_ns:
            self.entries.pop(key, None)
        return None, None

    def expire_all(self, now_ns: int) -> list[dict]:
        """Expire elapsed blocks and stale strikes, returning unblocked records for telemetry."""
        unblocked = []
        for buy_id, sell_id in tuple(self.entries):
            _, expired = self.active_block(buy_id, sell_id, now_ns)
            if expired is not None:
                unblocked.append(expired)
        return unblocked

    def expire_and_emit(self, emit, now_ns: int) -> None:
        """Expire elapsed blocks and emit route_max_hold_unblocked telemetry."""
        for expired in self.expire_all(now_ns):
            emit("route_max_hold_unblocked", now_ns, **expired)

    def record_close(
        self,
        *,
        route: str,
        symbol: str,
        buy_id: str,
        sell_id: str,
        position_id: int | None,
        close_reason: str | None,
        bilateral: bool,
        effective_net: F,
        economics: dict | None,
        now_ns: int,
    ) -> tuple[str | None, dict | None]:
        """Update strike/block state on trade close; return (event_name, event_payload) or (None, None)."""
        if not self.policy.max_hold_guard_enabled or not bilateral:
            return None, None
        key = (buy_id, sell_id)
        if close_reason == "take_profit" and effective_net > 0:
            self.entries.pop(key, None)
            return None, None
        if close_reason != "max_hold" or effective_net >= 0:
            return None, None
        prev = self.entries.get(key)
        if prev is not None and (self.reset_window_ns == 0 or now_ns - prev["triggered_ns"] <= self.reset_window_ns):
            strikes = prev["strikes"] + 1
        else:
            strikes = 1
        blocked = self.policy.max_hold_loss_block_secs > 0 and strikes >= self.policy.max_hold_loss_block_strikes
        ttl_secs = self.policy.max_hold_loss_block_secs if blocked else self.policy.max_hold_loss_cooldown_secs
        entry_spread_bps, exit_spread_bps, convergence_bps = _spread_convergence(economics)
        record = {
            "route": route,
            "symbol": symbol,
            "buy_id": buy_id,
            "sell_id": sell_id,
            "strikes": strikes,
            "blocked": blocked,
            "cooldown_secs": ttl_secs,
            "triggered_ns": now_ns,
            "until_ns": now_ns + ttl_secs * 1_000_000_000,
            "position_id": position_id,
            "entry_spread_bps": entry_spread_bps,
            "exit_spread_bps": exit_spread_bps,
            "convergence_bps": convergence_bps,
        }
        self.entries[key] = record
        if ttl_secs <= 0:
            return None, None
        event_name = "route_max_hold_blocked" if blocked else "route_max_hold_cooldown"
        return event_name, dict(record)

    def document(self) -> dict[str, dict]:
        return {row["route"]: dict(row) for row in self.entries.values()}

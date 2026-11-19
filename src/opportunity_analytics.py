"""Bounded opt-in strategy analytics for funnel, exit depth, alpha decay, and hot-path splits.

Read-only diagnostic observations attached to an active TimingRecorder session.
Never alters trading admission, sizing, risk caps, or ledger accounting, and
never stores credentials, account identifiers, or raw native error payloads.
"""
from __future__ import annotations

from collections import Counter, deque
from fractions import Fraction as F
import math
import threading

from opportunity_core import _book_valid, _metadata_valid, _walk, evaluate_opportunity

MAX_ROUTE_KEYS = 512
MAX_SAMPLES = 256
MAX_ACTIVE_DECAY = 64
PERSIST_WINDOW = 1024
DECAY_HORIZONS_MS = (50, 150, 300, 500, 1000, 3000)
DECAY_COOLDOWN_NS = 3_000_000_000
DECAY_EXPIRE_NS = 5_000_000_000
_VENUES = frozenset({"HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK", "LIGHTER_RH"})


def _safe_venue(venue):
    if venue == "HYPERLIQUID":
        return "HL"
    return venue if isinstance(venue, str) and venue in _VENUES else "UNKNOWN"


def _safe_token(value, *, max_len=64, fallback="other"):
    if not isinstance(value, str) or not value or len(value) > max_len or not value.isascii():
        return fallback
    if not all(ch.isalnum() or ch in "_-:/." for ch in value):
        return fallback
    return value


def _route_key(symbol, buy_venue, sell_venue):
    return f"{_safe_token(symbol, max_len=32, fallback='UNKNOWN')}|{_safe_venue(buy_venue)}->{_safe_venue(sell_venue)}"


def _quantiles(values):
    if not values:
        return None
    ordered = sorted(values)
    n = len(ordered)
    return {
        "count": n,
        "min": round(ordered[0], 4),
        "p25": round(ordered[max(0, math.ceil(n * 0.25) - 1)], 4),
        "p50": round(ordered[max(0, math.ceil(n * 0.50) - 1)], 4),
        "p75": round(ordered[max(0, math.ceil(n * 0.75) - 1)], 4),
        "p95": round(ordered[max(0, math.ceil(n * 0.95) - 1)], 4),
        "p99": round(ordered[max(0, math.ceil(n * 0.99) - 1)], 4),
        "max": round(ordered[-1], 4),
        "mean": round(sum(ordered) / n, 4),
    }


def classify_pair_rejection(books, metadata, buy_id, sell_id, settings, now_ns):
    """Classify why _pair() rejected a BBO-qualified candidate without changing admission."""
    buy_book, sell_book = books.get(buy_id), books.get(sell_id)
    buy_meta, sell_meta = metadata.get(buy_id), metadata.get(sell_id)
    if buy_book is None or sell_book is None:
        return "missing_book"
    if buy_meta is None or sell_meta is None or not _metadata_valid(buy_meta) or not _metadata_valid(sell_meta):
        return "metadata_invalid"
    for book in (buy_book, sell_book):
        if not book.valid or not book._structure_valid:
            return "book_invalid"
        if not 0 < book.ts_event_ns <= book.ts_received_ns <= now_ns:
            return "book_clock_invalid"
        if now_ns - book.ts_event_ns > settings.max_age_ms * 1_000_000:
            return "book_stale_source"
        if now_ns - book.ts_received_ns > settings.max_receive_age_ms * 1_000_000:
            return "book_stale_receive"
        if book.ts_received_ns - book.ts_event_ns > settings.max_age_ms * 1_000_000:
            return "book_stale_source"
    if abs(buy_book.ts_event_ns - sell_book.ts_event_ns) > settings.max_skew_ms * 1_000_000:
        return "book_skew"
    return "pair_invalid"


def compute_exit_liquidity(buy_book, buy_meta, sell_book, sell_meta, base_quantity,
                           entry_buy_vwap, entry_sell_vwap):
    """Evaluate reverse exit-side depth and immediate round-trip reversal slippage (bps)."""
    qty = F(base_quantity)
    if qty <= 0 or not buy_book.bids or not buy_book.asks or not sell_book.bids or not sell_book.asks:
        return None
    buy_mult, sell_mult = F(buy_meta.multiplier), F(sell_meta.multiplier)
    buy_avail = sum((F(size) * buy_mult for _, size in buy_book.bids), F(0))
    sell_avail = sum((F(size) * sell_mult for _, size in sell_book.asks), F(0))
    buy_cov = min(1.0, float(buy_avail / qty))
    sell_cov = min(1.0, float(sell_avail / qty))
    buy_exit_cash = _walk(buy_book.bids, buy_mult, qty)
    sell_exit_cash = _walk(sell_book.asks, sell_mult, qty)
    buy_bid, buy_ask = float(buy_book.bids[0][0]), float(buy_book.asks[0][0])
    sell_bid, sell_ask = float(sell_book.bids[0][0]), float(sell_book.asks[0][0])
    buy_spread_bps = ((buy_ask - buy_bid) / buy_ask * 10000.0) if buy_ask > 0 else None
    sell_spread_bps = ((sell_ask - sell_bid) / sell_ask * 10000.0) if sell_ask > 0 else None
    reversal_bps = None
    entry_buy_f = float(entry_buy_vwap)
    entry_sell_f = float(entry_sell_vwap)
    if buy_exit_cash is not None and sell_exit_cash is not None and entry_buy_f > 0:
        exit_buy_vwap = float(buy_exit_cash / qty)
        exit_sell_vwap = float(sell_exit_cash / qty)
        # Cost of reversing immediately at exit L2 VWAP (positive = cost/loss in bps).
        reversal_bps = ((entry_buy_f - exit_buy_vwap) + (exit_sell_vwap - entry_sell_f)) / entry_buy_f * 10000.0
    return {
        "buy_exit_full": buy_exit_cash is not None,
        "sell_exit_full": sell_exit_cash is not None,
        "both_exit_full": buy_exit_cash is not None and sell_exit_cash is not None,
        "buy_exit_coverage": round(buy_cov, 4),
        "sell_exit_coverage": round(sell_cov, 4),
        "buy_touch_spread_bps": round(buy_spread_bps, 4) if buy_spread_bps is not None else None,
        "sell_touch_spread_bps": round(sell_spread_bps, 4) if sell_spread_bps is not None else None,
        "instant_reversal_slippage_bps": round(reversal_bps, 4) if reversal_bps is not None else None,
    }


def _current_edge_snapshot(books, metadata, buy_id, sell_id, settings, now_ns):
    """Read-only current edge observation for alpha-decay tracking."""
    buy_book, sell_book = books.get(buy_id), books.get(sell_id)
    buy_meta, sell_meta = metadata.get(buy_id), metadata.get(sell_id)
    if buy_book is None or sell_book is None:
        return {"status": "missing_book", "bbo_net_bps": None, "l2_net_bps": None}
    if buy_meta is None or sell_meta is None or not _metadata_valid(buy_meta) or not _metadata_valid(sell_meta):
        return {"status": "metadata_invalid", "bbo_net_bps": None, "l2_net_bps": None}
    if not _book_valid(buy_book, settings, now_ns) or not _book_valid(sell_book, settings, now_ns):
        return {"status": "stale_or_invalid_book", "bbo_net_bps": None, "l2_net_bps": None}
    if abs(buy_book.ts_event_ns - sell_book.ts_event_ns) > settings.max_skew_ms * 1_000_000:
        return {"status": "book_skew", "bbo_net_bps": None, "l2_net_bps": None}
    buy_ask = float(buy_book.asks[0][0])
    sell_bid = float(sell_book.bids[0][0])
    if buy_ask <= 0:
        return {"status": "invalid_price", "bbo_net_bps": None, "l2_net_bps": None}
    fee_bps = float(buy_meta.taker_fee_bps) + float(sell_meta.taker_fee_bps) + float(settings.reserve_bps)
    bbo_net_bps = (sell_bid - buy_ask) / buy_ask * 10000.0 - fee_bps
    l2_net_bps = None
    # Use relaxed threshold (-10000 bps) to read the actual remaining L2 edge even after decay.
    from dataclasses import replace
    probe_settings = replace(settings, min_entry_edge_bps=-10000)
    probe = evaluate_opportunity(buy_book, buy_meta, sell_book, sell_meta, probe_settings, now_ns)
    if probe is not None:
        l2_net_bps = float(probe["economics"]["entry_after_fees_and_reserve_bps"])
    return {
        "status": "valid",
        "bbo_net_bps": round(bbo_net_bps, 4),
        "l2_net_bps": round(l2_net_bps, 4) if l2_net_bps is not None else None,
    }


class AnalyticsCollector:
    """Bounded process-local collector for pre-trade funnel, exit depth, decay, and hot-path splits."""

    def __init__(self):
        self.lock = threading.Lock()
        self.errors = 0
        # 1. Pre-trade funnel
        self.pre_gates = Counter()
        self.funnel_totals = Counter()
        self.funnel_by_route: dict[str, Counter] = {}
        self.route_overflows = 0
        # 2. Exit-side liquidity
        self.exit_liq_by_route: dict[str, dict] = {}
        self.exit_liq_samples: deque = deque(maxlen=MAX_SAMPLES)
        # 3. Alpha decay
        self._active_decay: dict[tuple[str, str, str], dict] = {}
        self._decay_last_start_ns: dict[tuple[str, str, str], int] = {}
        self.decay_completed: deque = deque(maxlen=MAX_SAMPLES)
        self.decay_horizon_stats: dict[int, dict] = {
            h: {"samples": 0, "valid": 0, "above_threshold": 0, "positive": 0,
                "l2_edges": deque(maxlen=MAX_SAMPLES), "bbo_edges": deque(maxlen=MAX_SAMPLES),
                "edge_deltas": deque(maxlen=MAX_SAMPLES)}
            for h in DECAY_HORIZONS_MS
        }
        self.decay_evictions = 0
        # 4. Hot-path holding vs flat + emit persistence
        self.eval_modes = {
            "flat": {"calls": 0, "full_scans": 0, "total_ns": 0, "max_ns": 0},
            "holding": {"calls": 0, "full_scans": 0, "total_ns": 0, "max_ns": 0},
        }
        self.persist_count = 0
        self.persist_total_ns = 0
        self.persist_max_ns = 0
        self.persist_recent_ms: deque = deque(maxlen=PERSIST_WINDOW)

    def _bucket(self, mapping, key, factory):
        bucket = mapping.get(key)
        if bucket is not None:
            return bucket
        if len(mapping) >= MAX_ROUTE_KEYS:
            self.route_overflows += 1
            key = "__other__"
            bucket = mapping.get(key)
            if bucket is not None:
                return bucket
        bucket = factory()
        mapping[key] = bucket
        return bucket

    def record_pre_gate(self, reason):
        try:
            tag = _safe_token(reason, max_len=48)
            with self.lock:
                self.pre_gates[tag] += 1
        except Exception:
            self.errors += 1

    def record_funnel(self, symbol, buy_venue, sell_venue, stage, *, detail=None):
        try:
            route = _route_key(symbol, buy_venue, sell_venue)
            stage_tag = _safe_token(stage, max_len=48)
            full_tag = f"{stage_tag}:{_safe_token(detail, max_len=48)}" if detail else stage_tag
            with self.lock:
                self.funnel_totals[full_tag] += 1
                counts = self._bucket(self.funnel_by_route, route, Counter)
                counts[full_tag] += 1
        except Exception:
            self.errors += 1

    def observe_exit_liquidity(self, symbol, buy_book, buy_meta, sell_book, sell_meta,
                               base_quantity, entry_buy_vwap, entry_sell_vwap,
                               entry_edge_bps, now_ns, *, stage="priced_qualified"):
        try:
            metrics = compute_exit_liquidity(
                buy_book, buy_meta, sell_book, sell_meta,
                base_quantity, entry_buy_vwap, entry_sell_vwap,
            )
            if metrics is None:
                return
            route = _route_key(symbol, buy_book.venue, sell_book.venue)
            entry_edge_f = round(float(entry_edge_bps), 4)
            with self.lock:
                agg = self._bucket(self.exit_liq_by_route, route, lambda: {
                    "samples": 0, "both_exit_full": 0, "thin_exit": 0,
                    "buy_cov_sum": 0.0, "sell_cov_sum": 0.0,
                    "reversal_count": 0, "reversal_sum_bps": 0.0, "reversal_max_bps": None,
                    "buy_spread_sum_bps": 0.0, "sell_spread_sum_bps": 0.0,
                })
                agg["samples"] += 1
                if metrics["both_exit_full"]:
                    agg["both_exit_full"] += 1
                else:
                    agg["thin_exit"] += 1
                agg["buy_cov_sum"] += metrics["buy_exit_coverage"]
                agg["sell_cov_sum"] += metrics["sell_exit_coverage"]
                if metrics["buy_touch_spread_bps"] is not None:
                    agg["buy_spread_sum_bps"] += metrics["buy_touch_spread_bps"]
                if metrics["sell_touch_spread_bps"] is not None:
                    agg["sell_spread_sum_bps"] += metrics["sell_touch_spread_bps"]
                rev = metrics["instant_reversal_slippage_bps"]
                if rev is not None:
                    agg["reversal_count"] += 1
                    agg["reversal_sum_bps"] += rev
                    agg["reversal_max_bps"] = rev if agg["reversal_max_bps"] is None else max(agg["reversal_max_bps"], rev)
                self.exit_liq_samples.append({
                    "observed_ns": int(now_ns),
                    "route": route,
                    "stage": _safe_token(stage, max_len=32),
                    "entry_edge_bps": entry_edge_f,
                    **metrics,
                })
        except Exception:
            self.errors += 1

    def track_candidate_decay(self, symbol, buy_id, sell_id, buy_venue, sell_venue,
                              initial_edge_bps, threshold_bps, now_ns):
        try:
            key = (symbol, buy_id, sell_id)
            with self.lock:
                last = self._decay_last_start_ns.get(key)
                if last is not None and now_ns - last < DECAY_COOLDOWN_NS:
                    return
                if key in self._active_decay:
                    return
                if len(self._active_decay) >= MAX_ACTIVE_DECAY:
                    oldest_key = next(iter(self._active_decay))
                    self._active_decay.pop(oldest_key, None)
                    self.decay_evictions += 1
                if len(self._decay_last_start_ns) >= MAX_ROUTE_KEYS:
                    self._decay_last_start_ns.pop(next(iter(self._decay_last_start_ns)))
                self._decay_last_start_ns[key] = now_ns
                self._active_decay[key] = {
                    "route": _route_key(symbol, buy_venue, sell_venue),
                    "symbol": _safe_token(symbol, max_len=32, fallback="UNKNOWN"),
                    "buy_id": buy_id,
                    "sell_id": sell_id,
                    "started_ns": now_ns,
                    "initial_edge_bps": round(float(initial_edge_bps), 4),
                    "threshold_bps": round(float(threshold_bps), 4),
                    "next_idx": 0,
                    "horizons": {},
                }
        except Exception:
            self.errors += 1

    def has_active_decay(self):
        return bool(self._active_decay)

    def sample_decay(self, state, now_ns, *, symbol=None):
        """Advance due alpha-decay horizons using current immutable books."""
        if not self._active_decay:
            return
        try:
            with self.lock:
                candidates = [
                    (key, tracker["buy_id"], tracker["sell_id"], tracker["started_ns"], tracker["next_idx"])
                    for key, tracker in self._active_decay.items()
                    if (symbol is None or key[0] == symbol)
                    and (
                        (tracker["next_idx"] < len(DECAY_HORIZONS_MS)
                         and now_ns - tracker["started_ns"] >= DECAY_HORIZONS_MS[tracker["next_idx"]] * 1_000_000)
                        or now_ns - tracker["started_ns"] >= DECAY_EXPIRE_NS
                    )
                ]
            if not candidates:
                return
            observations = {}
            settings = state.plan.settings
            for key, buy_id, sell_id, _, _ in candidates:
                observations[key] = _current_edge_snapshot(
                    state.books, state.metadata, buy_id, sell_id, settings, now_ns
                )
            with self.lock:
                for key, _, _, _, _ in candidates:
                    tracker = self._active_decay.get(key)
                    if tracker is None:
                        continue
                    obs = observations[key]
                    elapsed_ms = (now_ns - tracker["started_ns"]) / 1e6
                    while (tracker["next_idx"] < len(DECAY_HORIZONS_MS)
                           and elapsed_ms >= DECAY_HORIZONS_MS[tracker["next_idx"]]):
                        horizon_ms = DECAY_HORIZONS_MS[tracker["next_idx"]]
                        tracker["next_idx"] += 1
                        edge_val = obs["l2_net_bps"] if obs["l2_net_bps"] is not None else obs["bbo_net_bps"]
                        delta = round(edge_val - tracker["initial_edge_bps"], 4) if edge_val is not None else None
                        tracker["horizons"][str(horizon_ms)] = {
                            "actual_ms": round(elapsed_ms, 2),
                            "status": obs["status"],
                            "bbo_net_bps": obs["bbo_net_bps"],
                            "l2_net_bps": obs["l2_net_bps"],
                            "delta_bps": delta,
                        }
                        stats = self.decay_horizon_stats[horizon_ms]
                        stats["samples"] += 1
                        if obs["status"] == "valid":
                            stats["valid"] += 1
                        if obs["bbo_net_bps"] is not None:
                            stats["bbo_edges"].append(obs["bbo_net_bps"])
                        if obs["l2_net_bps"] is not None:
                            stats["l2_edges"].append(obs["l2_net_bps"])
                        if edge_val is not None:
                            if edge_val >= tracker["threshold_bps"]:
                                stats["above_threshold"] += 1
                            if edge_val > 0.0:
                                stats["positive"] += 1
                            stats["edge_deltas"].append(edge_val - tracker["initial_edge_bps"])
                    if tracker["next_idx"] >= len(DECAY_HORIZONS_MS) or now_ns - tracker["started_ns"] >= DECAY_EXPIRE_NS:
                        self._active_decay.pop(key, None)
                        self.decay_completed.append({
                            "route": tracker["route"],
                            "started_ns": tracker["started_ns"],
                            "initial_edge_bps": tracker["initial_edge_bps"],
                            "threshold_bps": tracker["threshold_bps"],
                            "horizons": tracker["horizons"],
                        })
        except Exception:
            self.errors += 1

    def record_eval_mode(self, holding, full_scan, elapsed_ns):
        try:
            bucket_name = "holding" if holding else "flat"
            with self.lock:
                row = self.eval_modes[bucket_name]
                row["calls"] += 1
                if full_scan:
                    row["full_scans"] += 1
                row["total_ns"] += elapsed_ns
                if elapsed_ns > row["max_ns"]:
                    row["max_ns"] = elapsed_ns
        except Exception:
            self.errors += 1

    def record_persist(self, elapsed_ns):
        try:
            with self.lock:
                self.persist_count += 1
                self.persist_total_ns += elapsed_ns
                if elapsed_ns > self.persist_max_ns:
                    self.persist_max_ns = elapsed_ns
                self.persist_recent_ms.append(elapsed_ns / 1e6)
        except Exception:
            self.errors += 1

    def document(self):
        with self.lock:
            pre_gates = dict(self.pre_gates)
            funnel_totals = dict(self.funnel_totals)
            funnel_by_route = {k: dict(v) for k, v in self.funnel_by_route.items()}
            route_overflows = self.route_overflows
            exit_liq_raw = {k: dict(v) for k, v in self.exit_liq_by_route.items()}
            exit_liq_samples = list(self.exit_liq_samples)
            decay_completed = list(self.decay_completed)
            decay_active_count = len(self._active_decay)
            decay_evictions = self.decay_evictions
            decay_horizons_raw = {
                str(h): {
                    "samples": s["samples"],
                    "valid": s["valid"],
                    "above_threshold": s["above_threshold"],
                    "positive": s["positive"],
                    "l2_edges": list(s["l2_edges"]),
                    "bbo_edges": list(s["bbo_edges"]),
                    "edge_deltas": list(s["edge_deltas"]),
                }
                for h, s in self.decay_horizon_stats.items()
            }
            eval_modes_raw = {k: dict(v) for k, v in self.eval_modes.items()}
            persist_count = self.persist_count
            persist_total_ns = self.persist_total_ns
            persist_max_ns = self.persist_max_ns
            persist_recent = list(self.persist_recent_ms)
            errors = self.errors

        exit_liq_summary = {}
        for route, agg in exit_liq_raw.items():
            n = agg["samples"]
            rc = agg["reversal_count"]
            exit_liq_summary[route] = {
                "samples": n,
                "both_exit_full": agg["both_exit_full"],
                "thin_exit": agg["thin_exit"],
                "both_exit_full_rate": round(agg["both_exit_full"] / n, 4) if n else None,
                "mean_buy_exit_coverage": round(agg["buy_cov_sum"] / n, 4) if n else None,
                "mean_sell_exit_coverage": round(agg["sell_cov_sum"] / n, 4) if n else None,
                "mean_buy_touch_spread_bps": round(agg["buy_spread_sum_bps"] / n, 4) if n else None,
                "mean_sell_touch_spread_bps": round(agg["sell_spread_sum_bps"] / n, 4) if n else None,
                "mean_instant_reversal_slippage_bps": round(agg["reversal_sum_bps"] / rc, 4) if rc else None,
                "max_instant_reversal_slippage_bps": round(agg["reversal_max_bps"], 4) if agg["reversal_max_bps"] is not None else None,
            }

        decay_horizons = {}
        for h_str, s in decay_horizons_raw.items():
            n = s["samples"]
            decay_horizons[h_str] = {
                "samples": n,
                "valid": s["valid"],
                "above_threshold_rate": round(s["above_threshold"] / n, 4) if n else None,
                "positive_rate": round(s["positive"] / n, 4) if n else None,
                "l2_net_bps": _quantiles(s["l2_edges"]),
                "bbo_net_bps": _quantiles(s["bbo_edges"]),
                "delta_from_initial_bps": _quantiles(s["edge_deltas"]),
            }

        eval_modes = {}
        for mode_name, row in eval_modes_raw.items():
            calls = row["calls"]
            eval_modes[mode_name] = {
                "calls": calls,
                "full_scans": row["full_scans"],
                "full_scan_rate": round(row["full_scans"] / calls, 4) if calls else None,
                "total_ms": round(row["total_ns"] / 1e6, 4),
                "mean_ms": round(row["total_ns"] / calls / 1e6, 4) if calls else None,
                "max_ms": round(row["max_ns"] / 1e6, 4),
            }

        persist_stats = {
            "count": persist_count,
            "total_ms": round(persist_total_ns / 1e6, 4),
            "mean_ms": round(persist_total_ns / persist_count / 1e6, 4) if persist_count else None,
            "max_ms": round(persist_max_ns / 1e6, 4),
            "recent_ms": _quantiles(persist_recent),
        }

        return {
            "schema_version": 1,
            "diagnostic_errors": errors,
            "funnel": {
                "pre_gates": pre_gates,
                "totals": funnel_totals,
                "by_route": funnel_by_route,
                "route_overflows": route_overflows,
            },
            "exit_liquidity": {
                "by_route": exit_liq_summary,
                "recent_samples": exit_liq_samples,
            },
            "alpha_decay": {
                "horizons_ms": list(DECAY_HORIZONS_MS),
                "active_trackers": decay_active_count,
                "evictions": decay_evictions,
                "by_horizon": decay_horizons,
                "recent_traces": decay_completed,
            },
            "hotpath": {
                "evaluate_symbol_by_position_state": eval_modes,
                "emit_persist_ms": persist_stats,
            },
        }

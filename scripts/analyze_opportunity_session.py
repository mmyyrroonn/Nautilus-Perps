#!/usr/bin/env python3
"""Unified offline post-run analyzer for multi-venue opportunity sessions (live or paper).

Combines bounded timing/analytics JSON, memory/queue JSONL, and stopped SQLite ledgers
into a single Markdown or JSON diagnostic report. Strictly read-only; never connects
to exchanges or exports credentials/account payloads.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from decimal import Decimal, InvalidOperation
import json
import math
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from opportunity_trade_store import FileLock

CHANNELS = ("time_events", "exec_events", "exec_commands", "data_events", "data_commands")
ENTRY_STAGE_PAIRS = (
    ("pricing_started", "candidate"),
    ("candidate", "persisted"),
    ("persisted", "wakeup_consumed"),
    ("persisted", "coordinator"),
    ("coordinator", "send_check"),
    ("send_check", "python_submit"),
    ("python_submit", "python_submit_returned"),
    ("pricing_started", "python_submit"),
)


def _dec(value, default=None):
    if value is None:
        return default
    try:
        d = Decimal(str(value))
        return d if d.is_finite() else default
    except (InvalidOperation, ValueError):
        return default


def _float(value, default=None):
    d = _dec(value)
    return float(d) if d is not None else default


def _quantiles(values):
    clean = sorted(v for v in values if v is not None and math.isfinite(v))
    if not clean:
        return None
    n = len(clean)
    return {
        "count": n,
        "min": round(clean[0], 4),
        "p50": round(clean[max(0, math.ceil(n * 0.50) - 1)], 4),
        "p95": round(clean[max(0, math.ceil(n * 0.95) - 1)], 4),
        "p99": round(clean[max(0, math.ceil(n * 0.99) - 1)], 4),
        "max": round(clean[-1], 4),
        "mean": round(sum(clean) / n, 4),
    }


def _linear_slope_per_min(points):
    if len(points) < 2:
        return None
    t0 = points[0][0]
    xs = [(t - t0) / 60.0 for t, _ in points]
    ys = [y for _, y in points]
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    denom = sum((x - mx) ** 2 for x in xs)
    if denom <= 0:
        return None
    return round(sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / denom, 4)


def _resolve_timing_path(target: Path) -> Path:
    target = target.resolve()
    if target.is_file():
        return target
    candidates = sorted(target.glob("timing-*.json"), key=lambda p: p.stat().st_mtime)
    if not candidates:
        raise ValueError(f"no timing-*.json found in {target}")
    return candidates[-1]


def _load_memory_rows(target: Path) -> list[dict]:
    target = target.resolve()
    paths = []
    if target.is_file():
        paths = [target]
    else:
        latest_path = target / "memory-latest.json"
        if latest_path.is_file():
            latest = json.loads(latest_path.read_text(encoding="utf-8"))
            paths = sorted(target.glob(f'memory-{latest["pid"]}-{latest["run_id"]}*.jsonl'))
        if not paths:
            candidates = sorted(target.glob("memory-*.jsonl"), key=lambda p: p.stat().st_mtime)
            if not candidates:
                raise ValueError(f"no memory-*.jsonl found in {target}")
            paths = [candidates[-1]]
    rows = []
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return rows


def analyze_memory(rows: list[dict], window_secs: int = 3600) -> dict | None:
    if not rows:
        return None
    ordered = sorted(rows, key=lambda r: r.get("unix_ns", 0))
    latest = ordered[-1]
    run_key = (latest.get("pid"), latest.get("run_id"))
    cutoff = latest.get("unix_ns", 0) - window_secs * 1_000_000_000
    scoped = [r for r in ordered if (r.get("pid"), r.get("run_id")) == run_key and r.get("unix_ns", 0) >= cutoff]
    deduped = list({r["unix_ns"]: r for r in scoped if "unix_ns" in r}.values())
    deduped.sort(key=lambda r: r["unix_ns"])
    if not deduped:
        return None

    rss_points = []
    swaps = []
    pause_reasons = Counter()
    halt_reasons = Counter()
    for row in deduped:
        pb = row.get("process_bytes") or {}
        if "VmRSS" in pb:
            rss_points.append((row["unix_ns"] / 1e9, pb["VmRSS"] / (1024 ** 2)))
        if "VmSwap" in pb:
            swaps.append(pb["VmSwap"] / (1024 ** 2))
        hp = row.get("strategy_hotpath") or {}
        if hp.get("entry_pause_reason"):
            pause_reasons[hp["entry_pause_reason"]] += 1
        if hp.get("halt_reason"):
            halt_reasons[hp["halt_reason"]] += 1

    rss_vals = [y for _, y in rss_points]
    duration_s = (deduped[-1]["unix_ns"] - deduped[0]["unix_ns"]) / 1e9 if len(deduped) >= 2 else 0.0

    channels = {}
    native_rows = [r for r in deduped if r.get("native_runner")]
    if len(native_rows) >= 2:
        for ch in CHANNELS:
            q = [r["native_runner"][ch]["queue_depth"] for r in native_rows]
            count = busy = elapsed = stalls = 0
            for a, b in zip(native_rows, native_rows[1:]):
                na, nb = a["native_runner"], b["native_runner"]
                dt = nb["elapsed_ns"] - na["elapsed_ns"]
                dc = nb[ch]["dispatched"] - na[ch]["dispatched"]
                db = nb[ch]["dispatch_busy_ns"] - na[ch]["dispatch_busy_ns"]
                if dt <= 0 or dc < 0 or db < 0:
                    continue
                elapsed += dt
                count += dc
                busy += db
                if nb["queue_sampled_at_ns"] > na["queue_sampled_at_ns"]:
                    stalls += int(dc == 0 and na[ch]["queue_depth"] > 0 and nb[ch]["queue_depth"] > 0)
            channels[ch] = {
                "queue_depth": _quantiles(q),
                "max_depth": max(q),
                "stalls": stalls,
                "dispatched": count,
                "dispatch_per_sec": round(count * 1e9 / elapsed, 2) if elapsed else None,
                "busy_fraction": round(busy / elapsed, 4) if elapsed else None,
            }

    first_hp = next((r.get("strategy_hotpath") for r in deduped if r.get("strategy_hotpath")), None)
    last_hp = next((r.get("strategy_hotpath") for r in reversed(deduped) if r.get("strategy_hotpath")), None)
    first_gw = next((r.get("gateway_containers") for r in deduped if r.get("gateway_containers")), None)
    last_gw = next((r.get("gateway_containers") for r in reversed(deduped) if r.get("gateway_containers")), None)

    container_growth = {}
    for label, start_map, end_map in (
        ("state", (first_hp or {}).get("state_containers"), (last_hp or {}).get("state_containers")),
        ("gateway", first_gw, last_gw),
    ):
        if start_map and end_map:
            container_growth[label] = {
                k: {"start": start_map.get(k, 0), "end": end_map.get(k, 0),
                    "delta": end_map.get(k, 0) - start_map.get(k, 0)}
                for k in sorted(set(start_map) | set(end_map))
            }

    return {
        "pid": latest.get("pid"),
        "run_id": latest.get("run_id"),
        "samples": len(deduped),
        "window_seconds": round(duration_s, 2),
        "rss_mib": _quantiles(rss_vals),
        "rss_start_mib": round(rss_vals[0], 2) if rss_vals else None,
        "rss_end_mib": round(rss_vals[-1], 2) if rss_vals else None,
        "rss_slope_mib_per_min": _linear_slope_per_min(rss_points),
        "max_swap_mib": round(max(swaps), 2) if swaps else None,
        "channels": channels,
        "entry_pause_samples": dict(pause_reasons),
        "halt_samples": dict(halt_reasons),
        "container_growth": container_growth,
    }


def analyze_timing(doc: dict | None) -> dict | None:
    if not doc:
        return None
    ops = doc.get("operations") or []
    spans = [r for r in ops if r.get("category") == "span"]
    spans.sort(key=lambda r: -r.get("self_ms", 0))
    total_span_self = sum(r.get("self_ms", 0) for r in spans) or 0.0
    hotspots = [
        {
            "name": r["name"],
            "venue": r.get("venue") or "",
            "calls": r["count"],
            "self_ms": round(r["self_ms"], 3),
            "self_share_pct": round(r["self_ms"] / total_span_self * 100, 2) if total_span_self else None,
            "mean_ms": round(r["mean_ms"], 4),
            "p95_ms": round(r["recent_p95_ms"], 4),
            "p99_ms": round(r["recent_p99_ms"], 4),
        }
        for r in spans[:15]
    ]
    backpack_health = next((r for r in spans if r.get("name") == "market.backpack_health"), None)

    # Entry trace stage latency breakdown
    entry_exec = doc.get("entry_execution") or {}
    stage_intervals = defaultdict(list)
    for trace in entry_exec.get("traces") or []:
        stages = trace.get("stages") or []
        by_name = {}
        for row in stages:
            if row.get("stage") and type(row.get("monotonic_ns")) is int:
                by_name.setdefault(row["stage"], row["monotonic_ns"])
        for start_name, end_name in ENTRY_STAGE_PAIRS:
            if start_name in by_name and end_name in by_name:
                delta = (by_name[end_name] - by_name[start_name]) / 1e6
                if delta >= 0:
                    stage_intervals[f"{start_name}->{end_name}"].append(delta)

    return {
        "run_id": doc.get("run_id"),
        "status": doc.get("status"),
        "elapsed_seconds": round((doc.get("elapsed_ms") or 0) / 1000.0, 2),
        "hotspots": hotspots,
        "backpack_health_span": {
            "calls": backpack_health["count"],
            "self_ms": round(backpack_health["self_ms"], 3),
            "self_share_pct": round(backpack_health["self_ms"] / total_span_self * 100, 2) if total_span_self else None,
            "p95_ms": round(backpack_health["recent_p95_ms"], 4),
        } if backpack_health else None,
        "entry_pair_outcomes": entry_exec.get("pair_outcomes"),
        "entry_stage_latencies_ms": {k: _quantiles(v) for k, v in stage_intervals.items()},
        "analysis": doc.get("analysis"),
    }


def _crossed_funding_boundaries(opened_ns: int | None, closed_ns: int | None) -> dict:
    if not opened_ns or not closed_ns or closed_ns <= opened_ns:
        return {"1h": False, "4h": False, "8h": False}
    open_s = opened_ns // 1_000_000_000
    close_s = closed_ns // 1_000_000_000
    return {
        "1h": (close_s // 3600) > (open_s // 3600),
        "4h": (close_s // 14400) > (open_s // 14400),
        "8h": (close_s // 28800) > (open_s // 28800),
    }


def _venue_from_instrument(instrument_id: str | None) -> str:
    if not instrument_id or "." not in instrument_id:
        return "UNKNOWN"
    raw = instrument_id.rsplit(".", 1)[1]
    return "HL" if raw == "HYPERLIQUID" else raw


def analyze_ledger_events(events: list[dict], *, session_id: str | None = None) -> dict:
    """Summarize execution quality, slippage, PnL attribution, and exit reasons (live or paper)."""
    mode = None
    trades = {}
    owners = {}
    rejections = Counter()
    epoch = 0

    for ev in events:
        kind = ev.get("event")
        if kind == "previous_session_archived":
            epoch += 1
            continue
        if mode is None and ev.get("mode") in {"live", "paper"}:
            mode = ev["mode"]
        sess = ev.get("session_id") or f"segment-{epoch}"
        if session_id is not None and sess != session_id:
            continue

        # Paper mode events
        if ev.get("mode") == "paper":
            if kind == "opened":
                pos = ev.get("position") or {}
                pid = pos.get("position_id")
                key = (sess, pid)
                buy_v = _venue_from_instrument(pos.get("buy_id"))
                sell_v = _venue_from_instrument(pos.get("sell_id"))
                entry = pos.get("entry") or {}
                pf = entry.get("paper_fills") or {}
                econ = entry.get("economics") or {}
                trades[key] = {
                    "session_id": sess, "position_id": pid,
                    "symbol": pos.get("symbol") or (pos.get("pair") or ["UNKNOWN"])[0],
                    "buy_venue": buy_v, "sell_venue": sell_v,
                    "route": f"{pos.get('symbol') or 'UNKNOWN'}|{buy_v}->{sell_v}",
                    "venue_pair": f"{buy_v}->{sell_v}",
                    "opened_ns": pos.get("opened_ns") or ev.get("observed_ns"),
                    "closed_ns": None, "status": "open", "exit_reason": None,
                    "expected_entry_edge_bps": _float(
                        pf.get("entry_after_fees_slippage_and_reserve_bps")
                        or econ.get("entry_after_fees_and_reserve_bps")),
                    "actual_entry_spread_bps": None,
                    "entry_slippage_bps": None,
                    "gross_usd": None, "fees_usd": None, "net_usd": None,
                    "fee_confirmed": True, "single_leg": False, "unfilled": False,
                    "orders": {},
                }
            elif kind == "closed":
                pid = ev.get("position_id")
                key = (sess, pid)
                trade = trades.get(key)
                if trade is None:
                    buy_v = _venue_from_instrument(ev.get("buy_id"))
                    sell_v = _venue_from_instrument(ev.get("sell_id"))
                    sym = (ev.get("pair") or ["UNKNOWN"])[0]
                    trade = trades[key] = {
                        "session_id": sess, "position_id": pid, "symbol": sym,
                        "buy_venue": buy_v, "sell_venue": sell_v,
                        "route": f"{sym}|{buy_v}->{sell_v}", "venue_pair": f"{buy_v}->{sell_v}",
                        "opened_ns": ev.get("opened_ns"), "closed_ns": None, "status": "open",
                        "exit_reason": None, "expected_entry_edge_bps": None,
                        "actual_entry_spread_bps": None, "entry_slippage_bps": None,
                        "gross_usd": None, "fees_usd": None, "net_usd": None,
                        "fee_confirmed": True, "single_leg": False, "unfilled": False, "orders": {},
                    }
                mark = ev.get("mark") or {}
                trade["status"] = "closed"
                trade["closed_ns"] = ev.get("observed_ns")
                if trade["opened_ns"] is None:
                    trade["opened_ns"] = ev.get("opened_ns")
                trade["exit_reason"] = ev.get("reason")
                trade["gross_usd"] = _float(mark.get("gross_usd"))
                trade["fees_usd"] = _float(mark.get("fees_usd"))
                trade["net_usd"] = _float(mark.get("net_usd"))
            continue

        # Live mode events
        pid = ev.get("position_id")
        cid = ev.get("client_order_id")
        cids = ev.get("client_order_ids") or []
        if isinstance(cids, str):
            cids = [cids]
        if pid is not None:
            keys = [(sess, pid)]
        else:
            keys = list(dict.fromkeys(owners[c] for c in ([cid] if cid else cids) if c in owners))

        for key in keys:
            trade = trades.setdefault(key, {
                "session_id": key[0], "position_id": key[1],
                "symbol": "UNKNOWN", "buy_venue": "UNKNOWN", "sell_venue": "UNKNOWN",
                "route": "UNKNOWN|UNKNOWN->UNKNOWN", "venue_pair": "UNKNOWN->UNKNOWN",
                "opened_ns": None, "closed_ns": None, "status": "open", "exit_reason": None,
                "expected_entry_edge_bps": None, "expected_entry_gross_bps": None,
                "actual_entry_spread_bps": None, "entry_slippage_bps": None,
                "gross_usd": None, "fees_usd": None, "net_usd": None, "provisional_net_usd": None,
                "fee_confirmed": False, "single_leg": False, "unfilled": False,
                "orders": {},
            })
            if ev.get("pair") and trade["symbol"] == "UNKNOWN":
                trade["symbol"] = ev["pair"][0]
            if kind == "orders_prepared":
                for order in ev.get("orders") or []:
                    oid = order.get("client_order_id")
                    if oid:
                        owners[oid] = key
                        trade["orders"].setdefault(oid, {}).update(order)
                    if order.get("purpose") == "entry":
                        venue = order.get("venue") or _venue_from_instrument(order.get("instrument_id"))
                        if order.get("leg") == "buy":
                            trade["buy_venue"] = venue
                        elif order.get("leg") == "sell":
                            trade["sell_venue"] = venue
                trade["route"] = f"{trade['symbol']}|{trade['buy_venue']}->{trade['sell_venue']}"
                trade["venue_pair"] = f"{trade['buy_venue']}->{trade['sell_venue']}"
                econ = ev.get("entry_economics")
                if isinstance(econ, dict):
                    trade["expected_entry_edge_bps"] = _float(econ.get("entry_after_fees_and_reserve_bps"))
                    trade["expected_entry_gross_bps"] = _float(econ.get("entry_gross_bps"))
            elif kind == "entry_imbalance":
                trade["single_leg"] = True
            elif kind == "fill" and cid:
                owners[cid] = key
                order = trade["orders"].setdefault(cid, {"client_order_id": cid})
                if order.get("purpose") == "entry" and trade["opened_ns"] is None:
                    trade["opened_ns"] = ev.get("observed_ns")
            elif kind == "order_terminal" and cid:
                owners[cid] = key
                order = trade["orders"].setdefault(cid, {"client_order_id": cid})
                order["outcome"] = ev.get("outcome")
                order["native_status"] = ev.get("native_status")
                order["reason_code"] = ev.get("reason_code")
                order["filled_quantity"] = ev.get("filled_quantity")
                order["latency_ms"] = _float(ev.get("latency_ms"))
                if ev.get("outcome") == "rejected" or ev.get("native_status") in {"rejected", "denied"}:
                    venue = order.get("venue") or _venue_from_instrument(order.get("instrument_id"))
                    rejections[(venue, ev.get("reason_code") or "unclassified")] += 1
            elif kind == "closed":
                trade["status"] = "closed"
                trade["closed_ns"] = ev.get("observed_ns")
                trade["exit_reason"] = ev.get("reason")
                trade["fee_confirmed"] = ev.get("fee_confirmed") is True
                trade["provisional_net_usd"] = _float(ev.get("provisional_net_usd"))
                econ = ev.get("economics") or {}
                if econ:
                    entry = econ.get("actual_entry") or {}
                    buy_n = _float(entry.get("buy_notional_usd"), 0.0)
                    sell_n = _float(entry.get("sell_notional_usd"), 0.0)
                    buy_f = _float(entry.get("buy_fee_usd"), 0.0)
                    sell_f = _float(entry.get("sell_fee_usd"), 0.0)
                    close_long = _float(econ.get("closing_long_cash_usd"), 0.0)
                    close_short = _float(econ.get("closing_short_cash_usd"), 0.0)
                    close_fees = _float(econ.get("closing_fees_usd"), 0.0)
                    trade["gross_usd"] = (close_long - buy_n) + (sell_n - close_short)
                    trade["fees_usd"] = buy_f + sell_f + close_fees
                    if buy_n > 0 and sell_n > 0:
                        actual_gross_bps = (sell_n - buy_n) / buy_n * 10000.0
                        trade["actual_entry_spread_bps"] = round(actual_gross_bps, 4)
                        if trade["expected_entry_gross_bps"] is not None:
                            trade["entry_slippage_bps"] = round(
                                trade["expected_entry_gross_bps"] - actual_gross_bps, 4)
                    elif buy_n == 0 and sell_n == 0:
                        trade["unfilled"] = True
                net_val = _float(ev.get("net_usd"))
                if trade["fee_confirmed"]:
                    trade["net_usd"] = net_val
                else:
                    trade["net_usd"] = trade["provisional_net_usd"] if trade["provisional_net_usd"] is not None else net_val

    # Aggregate matrices
    trade_list = list(trades.values())
    by_route = defaultdict(list)
    by_venue_pair = defaultdict(list)
    by_exit_reason = defaultdict(list)
    funding_buckets = {"crossed_1h": [], "not_crossed_1h": [], "crossed_4h": [], "crossed_8h": []}
    bilateral_full = single_leg = unfilled = 0

    for t in trade_list:
        by_route[t["route"]].append(t)
        by_venue_pair[t["venue_pair"]].append(t)
        if t["status"] == "closed":
            reason = t["exit_reason"] or "unknown"
            by_exit_reason[reason].append(t)
            if t["unfilled"] or reason == "entry_unfilled":
                unfilled += 1
            elif t["single_leg"] or reason == "entry_imbalance":
                single_leg += 1
            else:
                bilateral_full += 1
            if t["opened_ns"] and t["closed_ns"]:
                crossed = _crossed_funding_boundaries(t["opened_ns"], t["closed_ns"])
                if crossed["1h"]:
                    funding_buckets["crossed_1h"].append(t)
                else:
                    funding_buckets["not_crossed_1h"].append(t)
                if crossed["4h"]:
                    funding_buckets["crossed_4h"].append(t)
                if crossed["8h"]:
                    funding_buckets["crossed_8h"].append(t)

    def _agg_group(group):
        closed = [t for t in group if t["status"] == "closed"]
        nets = [t["net_usd"] for t in closed if t["net_usd"] is not None]
        grosses = [t["gross_usd"] for t in closed if t["gross_usd"] is not None]
        fees = [t["fees_usd"] for t in closed if t["fees_usd"] is not None]
        slips = [t["entry_slippage_bps"] for t in closed if t["entry_slippage_bps"] is not None]
        holds = [(t["closed_ns"] - t["opened_ns"]) / 1e9 for t in closed
                 if t["opened_ns"] and t["closed_ns"] and t["closed_ns"] >= t["opened_ns"]]
        sl_cnt = sum(1 for t in closed if t["single_leg"] or t["exit_reason"] == "entry_imbalance")
        uf_cnt = sum(1 for t in closed if t["unfilled"] or t["exit_reason"] == "entry_unfilled")
        return {
            "trades": len(group),
            "closed": len(closed),
            "single_leg": sl_cnt,
            "single_leg_rate": round(sl_cnt / len(closed), 4) if closed else None,
            "unfilled": uf_cnt,
            "gross_usd": round(sum(grosses), 6),
            "fees_usd": round(sum(fees), 6),
            "net_usd": round(sum(nets), 6),
            "mean_net_usd": round(sum(nets) / len(nets), 6) if nets else None,
            "win_rate": round(sum(1 for n in nets if n > 0) / len(nets), 4) if nets else None,
            "entry_slippage_bps": _quantiles(slips),
            "hold_seconds": _quantiles(holds),
        }

    closed_all = [t for t in trade_list if t["status"] == "closed"]
    confirmed_nets = [t["net_usd"] for t in closed_all if t["fee_confirmed"] and t["net_usd"] is not None]
    provisional_nets = [t["net_usd"] for t in closed_all if not t["fee_confirmed"] and t["net_usd"] is not None]

    return {
        "mode": mode or "unknown",
        "totals": {
            "trades": len(trade_list),
            "closed": len(closed_all),
            "open_or_unclosed": len(trade_list) - len(closed_all),
            "bilateral_completed": bilateral_full,
            "single_leg_imbalance": single_leg,
            "unfilled": unfilled,
            "single_leg_rate": round(single_leg / len(closed_all), 4) if closed_all else None,
            "confirmed_net_usd": round(sum(confirmed_nets), 6),
            "provisional_only_net_usd": round(sum(provisional_nets), 6),
            "confirmed_trades": len(confirmed_nets),
            "provisional_trades": len(provisional_nets),
            "total_gross_usd": round(sum(t["gross_usd"] for t in closed_all if t["gross_usd"] is not None), 6),
            "total_fees_usd": round(sum(t["fees_usd"] for t in closed_all if t["fees_usd"] is not None), 6),
        },
        "rejections_by_venue_reason": {f"{v}|{r}": c for (v, r), c in rejections.most_common()},
        "by_venue_pair": {k: _agg_group(v) for k, v in sorted(by_venue_pair.items())},
        "by_route": {
            k: _agg_group(v)
            for k, v in sorted(by_route.items(), key=lambda kv: (-len(kv[1]), kv[0]))
        },
        "by_exit_reason": {k: _agg_group(v) for k, v in sorted(by_exit_reason.items())},
        "funding_boundary_split": {k: _agg_group(v) for k, v in funding_buckets.items()},
    }


def load_ledger(path: Path, *, session_id: str | None = None) -> dict:
    path = path.resolve(strict=True)
    if not path.is_file():
        raise ValueError("ledger must be a SQLite file")
    try:
        lock = FileLock(path.with_suffix(path.suffix + ".lock"))
    except OSError as exc:
        raise ValueError("ledger is in use; analyze a stopped ledger or consistent offline copy") from exc
    try:
        conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
        try:
            conn.execute("PRAGMA query_only=ON")
            rows = conn.execute("SELECT payload FROM events ORDER BY sequence").fetchall()
            events = [json.loads(r[0]) for r in rows]
            return analyze_ledger_events(events, session_id=session_id)
        finally:
            conn.close()
    finally:
        lock.close()


def render_markdown(report: dict) -> str:
    lines = ["# 多所套利运行分析报告", ""]
    sources = report.get("sources") or {}
    lines.append(f"- 数据来源：`ledger={sources.get('ledger') or '-'}` | `timing={sources.get('timing') or '-'}` | `memory={sources.get('memory') or '-'}`")
    lines.append("")

    # 1. Memory & Hotpath
    mem = report.get("memory")
    tim = report.get("timing")
    lines.append("## 1. 运行健康度、内存与热路径（#107）")
    lines.append("")
    if mem:
        rss = mem.get("rss_mib") or {}
        lines.append(
            f"- 窗口：`{mem['window_seconds']}s`（`{mem['samples']}` 采样，PID `{mem['pid']}`） | "
            f"RSS：`{mem.get('rss_start_mib')} -> {mem.get('rss_end_mib')} MiB`（p95=`{rss.get('p95')}`, max=`{rss.get('max')}`, "
            f"斜率=`{mem.get('rss_slope_mib_per_min')} MiB/min`） | Max Swap：`{mem.get('max_swap_mib')} MiB`"
        )
        if mem.get("entry_pause_samples"):
            lines.append(f"- 入场暂停采样：`{mem['entry_pause_samples']}`")
        if mem.get("halt_samples"):
            lines.append(f"- Halt 采样：`{mem['halt_samples']}`")
        if mem.get("channels"):
            lines.extend(["", "| 原生队列通道 | 深度 p50 / p95 / max | 消费速率 (/s) | Busy 占比 | 无进展停滞窗 |",
                          "| --- | --- | ---: | ---: | ---: |"])
            for ch, row in mem["channels"].items():
                qd = row.get("queue_depth") or {}
                lines.append(
                    f"| `{ch}` | {qd.get('p50')} / {qd.get('p95')} / {row.get('max_depth')} | "
                    f"{row.get('dispatch_per_sec')} | {row.get('busy_fraction')} | {row.get('stalls')} |"
                )
        if mem.get("container_growth"):
            lines.extend(["", "**容器规模变化（首采样 -> 末采样）**："])
            for scope, items in mem["container_growth"].items():
                summary = ", ".join(f"`{k}`: {v['start']}->{v['end']} ({v['delta']:+d})" for k, v in items.items())
                lines.append(f"- `{scope}`: {summary}")
    else:
        lines.append("- 未提供 memory JSONL 数据。")

    if tim:
        analysis = tim.get("analysis") or {}
        hotpath = analysis.get("hotpath") or {}
        eval_modes = hotpath.get("evaluate_symbol_by_position_state") or {}
        persist = hotpath.get("emit_persist_ms") or {}
        lines.append("")
        lines.append(f"- Timing 总时长：`{tim['elapsed_seconds']}s`（状态 `{tim['status']}`）")
        if eval_modes:
            for m_name in ("flat", "holding"):
                row = eval_modes.get(m_name)
                if row:
                    lines.append(
                        f"- `evaluate_symbol` [{m_name}]：调用 `{row['calls']}` 次，全量扫对 `{row['full_scans']}` 次"
                        f"（占比 `{row['full_scan_rate']}`），均值 `{row['mean_ms']} ms`，最大 `{row['max_ms']} ms`"
                    )
        if persist and persist.get("count"):
            rec = persist.get("recent_ms") or {}
            lines.append(
                f"- `emit` SQLite 落盘：`{persist['count']}` 次，总计 `{persist['total_ms']} ms`，"
                f"均值 `{persist['mean_ms']} ms`，p50/p95/p99=`{rec.get('p50')}/{rec.get('p95')}/{rec.get('p99')} ms`，max=`{persist['max_ms']} ms`"
            )
        if tim.get("backpack_health_span"):
            bh = tim["backpack_health_span"]
            lines.append(
                f"- `market.backpack_health`：调用 `{bh['calls']}` 次，self=`{bh['self_ms']} ms`（占 span `{bh['self_share_pct']}%`），p95=`{bh['p95_ms']} ms`"
            )
        if tim.get("hotspots"):
            lines.extend(["", "| Top Span 热点 | Venue | 调用数 | Self (ms) | 占比 (%) | 均值 (ms) | p95 (ms) |",
                          "| --- | --- | ---: | ---: | ---: | ---: | ---: |"])
            for r in tim["hotspots"][:10]:
                lines.append(
                    f"| `{r['name']}` | {r['venue'] or '-'} | {r['calls']} | {r['self_ms']} | "
                    f"{r['self_share_pct']} | {r['mean_ms']} | {r['p95_ms']} |"
                )
    lines.append("")

    # 2. Pre-trade Funnel
    analysis = (tim or {}).get("analysis") or {}
    funnel = analysis.get("funnel") or {}
    lines.append("## 2. 下单前候选漏斗与拦截归因（#108 / #64 / #95）")
    lines.append("")
    if funnel and (funnel.get("totals") or funnel.get("pre_gates")):
        if funnel.get("pre_gates"):
            lines.append("- 计价前前置门禁拦截：" + ", ".join(f"`{k}`={v}" for k, v in sorted(funnel["pre_gates"].items(), key=lambda x: -x[1])))
        totals = funnel.get("totals") or {}
        lines.extend(["", "| 漏斗阶段 / 拦截原因 | 次数 |", "| --- | ---: |"])
        for stage, count in sorted(totals.items(), key=lambda x: -x[1]):
            lines.append(f"| `{stage}` | {count} |")
        by_route = funnel.get("by_route") or {}
        if by_route:
            top_routes = sorted(
                by_route.items(),
                key=lambda kv: -(
                    kv[1].get("bbo_qualified", 0)
                    + kv[1].get("priced_qualified", 0)
                    + kv[1].get("live_rejected:route_max_hold_block", 0)
                    + kv[1].get("live_rejected:route_max_hold_cooldown", 0)
                ),
            )[:15]
            lines.extend(["", "| Top 路由 (`symbol|buy->sell`) | BBO达标 | 深度定容达标 | 已生成订单 | 派发放行 | 主要拦截原因 |",
                          "| --- | ---: | ---: | ---: | ---: | --- |"])
            for route, counts in top_routes:
                blocks = [(k, v) for k, v in counts.items()
                          if k.startswith(("pair_invalid:", "pricing_rejected:", "live_rejected:", "dispatch_blocked:"))]
                blocks.sort(key=lambda x: -x[1])
                top_block = ", ".join(f"{k}={v}" for k, v in blocks[:2]) or "-"
                lines.append(
                    f"| `{route}` | {counts.get('bbo_qualified', 0)} | {counts.get('priced_qualified', 0)} | "
                    f"{counts.get('prepared', 0)} | {counts.get('dispatch_allowed', 0)} | {top_block} |"
                )
    else:
        lines.append("- 无候选漏斗记录（需在运行时开启 `--timing`）。")
    lines.append("")

    # 3. Exit-Side Liquidity
    exit_liq = (analysis.get("exit_liquidity") or {}).get("by_route") or {}
    lines.append("## 3. 反向平仓侧深度与瞬时反转磨损（#108）")
    lines.append("")
    if exit_liq:
        ranked = sorted(
            exit_liq.items(),
            key=lambda kv: (kv[1].get("both_exit_full_rate") if kv[1].get("both_exit_full_rate") is not None else 2.0,
                            -(kv[1].get("mean_instant_reversal_slippage_bps") or 0.0)),
        )[:20]
        lines.extend([
            "| 路由 (`symbol|buy->sell`) | 样本数 | 双侧可平率 | 买腿平仓覆盖 | 卖腿平仓覆盖 | 买/卖盘口点差 (bps) | 瞬时反转磨损均值 / 最大 (bps) |",
            "| --- | ---: | ---: | ---: | ---: | --- | --- |",
        ])
        for route, r in ranked:
            lines.append(
                f"| `{route}` | {r['samples']} | {r['both_exit_full_rate']} | "
                f"{r['mean_buy_exit_coverage']} | {r['mean_sell_exit_coverage']} | "
                f"{r['mean_buy_touch_spread_bps']} / {r['mean_sell_touch_spread_bps']} | "
                f"{r['mean_instant_reversal_slippage_bps']} / {r['max_instant_reversal_slippage_bps']} |"
            )
    else:
        lines.append("- 无反向平仓深度样本。")
    lines.append("")

    # 4. Alpha Decay & Entry Latencies
    decay = analysis.get("alpha_decay") or {}
    by_h = decay.get("by_horizon") or {}
    lines.append("## 4. 价差存活曲线（Alpha Decay）与入场阶段耗时（#64 / #95）")
    lines.append("")
    if any((row.get("samples") or 0) > 0 for row in by_h.values()):
        lines.extend([
            "| 观察窗口 (ms) | 样本数 | 有效盘口数 | 仍 >= 门槛占比 | 仍 > 0 bps 占比 | L2 净边际 p50 (bps) | BBO 净边际 p50 (bps) | 相对初始衰减 p50 (bps) |",
            "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |",
        ])
        for h in decay.get("horizons_ms") or sorted(by_h, key=int):
            row = by_h.get(str(h)) or {}
            l2 = row.get("l2_net_bps") or {}
            bbo = row.get("bbo_net_bps") or {}
            delta = row.get("delta_from_initial_bps") or {}
            lines.append(
                f"| {h} | {row.get('samples', 0)} | {row.get('valid', 0)} | "
                f"{row.get('above_threshold_rate')} | {row.get('positive_rate')} | "
                f"{l2.get('p50')} | {bbo.get('p50')} | {delta.get('p50')} |"
            )
    else:
        lines.append("- 无价差衰减样本。")

    stage_lat = (tim or {}).get("entry_stage_latencies_ms") or {}
    if stage_lat:
        lines.extend(["", "| 入场链路阶段 | 样本数 | p50 (ms) | p95 (ms) | max (ms) |",
                      "| --- | ---: | ---: | ---: | ---: |"])
        for stage, q in stage_lat.items():
            if q:
                lines.append(f"| `{stage}` | {q['count']} | {q['p50']} | {q['p95']} | {q['max']} |")
    lines.append("")

    # 5. Execution Quality & PnL Matrix
    led = report.get("ledger")
    lines.append("## 5. 执行质量与盈亏归因矩阵（账本）")
    lines.append("")
    if led:
        tot = led["totals"]
        lines.append(
            f"- 模式：`{led['mode']}` | 总交易对：`{tot['trades']}`（已平 `{tot['closed']}`，未平 `{tot['open_or_unclosed']}`） | "
            f"双边完成：`{tot['bilateral_completed']}`，单腿不平衡：`{tot['single_leg_imbalance']}`（单腿率 `{tot['single_leg_rate']}`），双未成：`{tot['unfilled']}`"
        )
        lines.append(
            f"- 收益（USD）：毛盈亏 `{tot['total_gross_usd']}` | 手续费 `{tot['total_fees_usd']}` | "
            f"已确认净盈亏 `{tot['confirmed_net_usd']}`（{tot['confirmed_trades']} 笔） | 暂估净盈亏 `{tot['provisional_only_net_usd']}`（{tot['provisional_trades']} 笔）"
        )
        if led.get("rejections_by_venue_reason"):
            lines.append("- 拒单分布：" + ", ".join(f"`{k}`={v}" for k, v in led["rejections_by_venue_reason"].items()))
        if led.get("by_venue_pair"):
            lines.extend(["", "| 交易所对 | 交易数 | 已平 | 单腿率 | 胜率 | 毛盈亏 (USD) | 手续费 (USD) | 净盈亏 (USD) | 入场滑点 p50 (bps) |",
                          "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"])
            for vp, r in led["by_venue_pair"].items():
                slip = (r.get("entry_slippage_bps") or {}).get("p50")
                lines.append(
                    f"| `{vp}` | {r['trades']} | {r['closed']} | {r['single_leg_rate']} | {r['win_rate']} | "
                    f"{r['gross_usd']} | {r['fees_usd']} | {r['net_usd']} | {slip} |"
                )
        if led.get("by_route"):
            lines.extend(["", "| Top 交易路由 | 交易数 | 已平 | 单腿数 | 净盈亏 (USD) | 均值 (USD) | 持仓 p50 (s) |",
                          "| --- | ---: | ---: | ---: | ---: | ---: | ---: |"])
            for route, r in list(led["by_route"].items())[:20]:
                hold = (r.get("hold_seconds") or {}).get("p50")
                lines.append(
                    f"| `{route}` | {r['trades']} | {r['closed']} | {r['single_leg']} | "
                    f"{r['net_usd']} | {r['mean_net_usd']} | {hold} |"
                )
    else:
        lines.append("- 未提供 SQLite 账本。")
    lines.append("")

    # 6. Exit Reason & Funding Boundary
    lines.append("## 6. 退出原因 × 持仓时长 × 资金费整点分布（#109 参数校准）")
    lines.append("")
    if led and led.get("by_exit_reason"):
        lines.extend(["| 退出原因 | 笔数 | 胜率 | 总净盈亏 (USD) | 单笔均值 (USD) | 持仓 p50 / p95 / max (s) |",
                      "| --- | ---: | ---: | ---: | ---: | --- |"])
        for reason, r in led["by_exit_reason"].items():
            h = r.get("hold_seconds") or {}
            lines.append(
                f"| `{reason}` | {r['closed']} | {r['win_rate']} | {r['net_usd']} | {r['mean_net_usd']} | "
                f"{h.get('p50')} / {h.get('p95')} / {h.get('max')} |"
            )
        fb = led.get("funding_boundary_split") or {}
        lines.extend(["", "| 资金费整点跨越分组 | 笔数 | 总净盈亏 (USD) | 单笔均值 (USD) | 持仓 p50 (s) |",
                      "| --- | ---: | ---: | ---: | ---: |"])
        for bucket in ("not_crossed_1h", "crossed_1h", "crossed_4h", "crossed_8h"):
            r = fb.get(bucket) or {}
            h = r.get("hold_seconds") or {}
            lines.append(
                f"| `{bucket}` | {r.get('closed', 0)} | {r.get('net_usd', 0)} | {r.get('mean_net_usd')} | {h.get('p50')} |"
            )
    else:
        lines.append("- 无已平仓退出分布数据。")
    lines.append("")
    return "\n".join(lines)


def build_report(*, ledger: Path | None = None, timing: Path | None = None,
                 memory: Path | None = None, session_id: str | None = None,
                 window_secs: int = 3600) -> dict:
    timing_path = _resolve_timing_path(timing) if timing is not None else None
    timing_doc = json.loads(timing_path.read_text(encoding="utf-8")) if timing_path else None
    memory_rows = _load_memory_rows(memory) if memory is not None else []
    ledger_summary = load_ledger(ledger, session_id=session_id) if ledger is not None else None
    return {
        "schema_version": 1,
        "sources": {
            "ledger": str(ledger.resolve()) if ledger else None,
            "timing": str(timing_path) if timing_path else None,
            "memory": str(memory.resolve()) if memory else None,
        },
        "memory": analyze_memory(memory_rows, window_secs=window_secs),
        "timing": analyze_timing(timing_doc),
        "ledger": ledger_summary,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path, help="stopped live or paper SQLite ledger")
    parser.add_argument("--timing", type=Path, help="timing-*.json file or timing directory")
    parser.add_argument("--memory", type=Path, help="memory-*.jsonl file or memory diagnostics directory")
    parser.add_argument("--session", help="optional session_id filter for ledger events")
    parser.add_argument("--window-secs", type=int, default=3600, help="memory window in seconds (default: 3600)")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path, help="write report to this path instead of stdout")
    args = parser.parse_args(argv)
    if args.ledger is None and args.timing is None and args.memory is None:
        parser.error("provide at least one of --ledger, --timing, or --memory")
    try:
        report = build_report(
            ledger=args.ledger,
            timing=args.timing,
            memory=args.memory,
            session_id=args.session,
            window_secs=args.window_secs,
        )
        content = (json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                   if args.format == "json" else render_markdown(report))
        if args.output:
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(content, encoding="utf-8")
        else:
            print(content)
        return 0
    except (OSError, ValueError, sqlite3.Error, json.JSONDecodeError) as exc:
        print(f"[analyze-session] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

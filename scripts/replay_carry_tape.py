"""Standardized offline tape replay & parameter sweep simulator for Route A Carry."""
from __future__ import annotations

import argparse
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from decimal import Decimal
import heapq
import json
from pathlib import Path
import sys
from typing import Any, Iterator, Mapping
import zlib

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = REPO_ROOT / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from carry.config import (  # noqa: E402
    CarryConfigError,
    CarryPlan,
    CarryPolicy,
    CarryRiskPolicy,
    CarryRuntimePolicy,
    DEX_VENUES,
    load_carry_plan,
)
from carry.funding_feed import LegMarketInfo  # noqa: E402
from carry.ledger import close_reason_priority  # noqa: E402
from carry.risk import BasisStopTracker, CarryInversionTracker, FastRiskEvaluator  # noqa: E402
from carry.signal import (  # noqa: E402
    EntryCandidate,
    estimate_exec_basis_bps,
    evaluate_entry_candidates,
    select_cex_exit_defer_ts,
    should_exit_carry_decay,
)

_PNL_QUANT = Decimal("0.0001")
_HOUR_SECS = 3600


def _q4(val: Decimal) -> Decimal:
    return val.quantize(_PNL_QUANT)


def _utc_day(ts_s: int) -> str:
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d")


def iter_jsonl_gz(path: Path, stats: dict[str, int]) -> Iterator[dict[str, Any]]:
    """Stream JSON objects from a possibly-truncated or concatenated .jsonl.gz file."""
    decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
    buf = b""
    truncated = False
    in_member = False
    try:
        with path.open("rb") as fh:
            while True:
                chunk = fh.read(65536)
                if not chunk:
                    break
                data = chunk
                while data:
                    in_member = True
                    try:
                        out = decomp.decompress(data)
                    except zlib.error:
                        truncated = True
                        data = b""
                        in_member = False
                        break
                    if out:
                        buf += out
                        lines = buf.split(b"\n")
                        buf = lines.pop()
                        for raw_line in lines:
                            text = raw_line.decode("utf-8", errors="replace").strip()
                            if not text:
                                continue
                            try:
                                obj = json.loads(text)
                            except json.JSONDecodeError:
                                stats["skipped_lines"] += 1
                                continue
                            if isinstance(obj, dict):
                                yield obj
                    if decomp.eof:
                        data = decomp.unused_data
                        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
                        in_member = False
                    else:
                        data = b""
        if in_member and not truncated:
            truncated = True
    except OSError:
        truncated = True

    if buf.strip():
        text = buf.decode("utf-8", errors="replace").strip()
        try:
            obj = json.loads(text)
            if isinstance(obj, dict):
                yield obj
        except json.JSONDecodeError:
            stats["skipped_lines"] += 1

    if truncated:
        stats["truncated_files"] += 1


def _merged_tape_events(
    tape_dir: Path,
    stats: dict[str, int],
) -> Iterator[tuple[float, int, dict[str, Any]]]:
    """Merge slow_snapshots_*.jsonl.gz and fast_bbo_*.jsonl.gz chronologically by ts."""
    slow_files = sorted(tape_dir.glob("slow_snapshots_*.jsonl.gz"))
    fast_files = sorted(tape_dir.glob("fast_bbo_*.jsonl.gz"))
    stats["slow_files"] = len(slow_files)
    stats["fast_files"] = len(fast_files)

    def _stream_kind(files: list[Path], kind_order: int, stat_key: str) -> Iterator[tuple[float, int, int, dict[str, Any]]]:
        seq = 0
        for p in files:
            for row in iter_jsonl_gz(p, stats):
                raw_ts = row.get("ts")
                if raw_ts is None:
                    stats["skipped_lines"] += 1
                    continue
                try:
                    ts = float(raw_ts)
                except (TypeError, ValueError):
                    stats["skipped_lines"] += 1
                    continue
                stats[stat_key] += 1
                yield (ts, kind_order, seq, row)
                seq += 1

    merged = heapq.merge(
        _stream_kind(fast_files, 0, "fast_records"),
        _stream_kind(slow_files, 1, "slow_records"),
    )
    for ts, kind_order, _seq, row in merged:
        yield ts, kind_order, row


def _decode_slow_snapshot(row: Mapping[str, Any], ts_s: int) -> dict[str, dict[str, LegMarketInfo]]:
    snap: dict[str, dict[str, LegMarketInfo]] = {}
    for leg in row.get("legs", []):
        if not isinstance(leg, Mapping):
            continue
        sym = str(leg.get("s") or "").upper()
        venue = str(leg.get("v") or "").upper()
        iid = str(leg.get("iid") or "")
        if not sym or not venue or not iid:
            continue
        mp = float(leg.get("mp") or 0.0)
        bid = float(leg.get("b") or mp or 0.0)
        ask = float(leg.get("a") or mp or 0.0)
        if mp <= 0.0 and bid > 0.0 and ask > 0.0:
            mp = 0.5 * (bid + ask)
        if mp <= 0.0 or bid <= 0.0 or ask <= 0.0:
            continue
        info = LegMarketInfo(
            venue=venue,
            symbol=sym,
            raw_symbol=sym,
            instrument_id=iid,
            asset_class=str(leg.get("ac") or "CRYPTO"),
            interval_h=max(1, int(leg.get("ih") or 1)),
            next_settle_ts=int(leg.get("ns") or 0),
            r8_bps_h=float(leg["r8"]) if leg.get("r8") is not None else None,
            r24_bps_h=float(leg["r24"]) if leg.get("r24") is not None else None,
            current_rate_bps_h=float(leg["cr"]) if leg.get("cr") is not None else None,
            mark_price=mp,
            prev_1h_price=None,
            bid=bid,
            ask=ask,
            vol24_usd=float(leg.get("v24") or 0.0),
            vol1h_usd=float(leg["v1"]) if leg.get("v1") is not None else None,
            fetched_ts=ts_s,
        )
        snap.setdefault(sym, {})[venue] = info
    return snap


@dataclass
class _PendingMakerSlice:
    is_opening: bool
    maker_side_is_long: bool
    maker_venue: str
    hedge_venue: str
    maker_iid: str
    hedge_iid: str
    side: str
    limit_price: Decimal
    base_qty: Decimal
    submitted_ts: int
    attempts: int


@dataclass
class _SimPosition:
    position_id: str
    symbol: str
    long_venue: str
    short_venue: str
    iid_long: str
    iid_short: str
    opened_ts: int
    entry_l: float
    entry_s: float
    entry_basis_bps: float
    target_base_qty: Decimal
    status: str = "OPENING"  # OPENING, OPEN, CLOSING, CLOSED
    long_base_qty: Decimal = Decimal("0")
    short_base_qty: Decimal = Decimal("0")
    open_l_qty: Decimal = Decimal("0")
    open_l_notional: Decimal = Decimal("0")
    open_s_qty: Decimal = Decimal("0")
    open_s_notional: Decimal = Decimal("0")
    close_l_qty: Decimal = Decimal("0")
    close_l_notional: Decimal = Decimal("0")
    close_s_qty: Decimal = Decimal("0")
    close_s_notional: Decimal = Decimal("0")
    fee_usd: Decimal = Decimal("0")
    funding_pnl_usd: Decimal = Decimal("0")
    settled_keys: set[tuple[str, int]] = field(default_factory=set)
    pending_maker: _PendingMakerSlice | None = None
    slice_attempts: int = 0
    close_reason: str | None = None
    closed_ts: int | None = None
    close_vacuum_defer_started_ts: int | None = None
    exit_defer_target_ts: int | None = None
    entry_pay_leg_cr_bps_h: float | None = None
    entry_c_live_bps_h: float | None = None
    entry_would_block_d: bool = False

    def as_risk_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "long_venue": self.long_venue,
            "short_venue": self.short_venue,
            "iid_long": self.iid_long,
            "iid_short": self.iid_short,
            "entry_l": self.entry_l,
            "entry_s": self.entry_s,
            "entry_basis_bps": self.entry_basis_bps,
            "long_base_qty": str(self.long_base_qty),
            "short_base_qty": str(self.short_base_qty),
        }


def _choose_maker_is_long(v_long: str, v_short: str, bbo_l: tuple[float, float], bbo_s: tuple[float, float]) -> bool:
    l_dex = v_long in DEX_VENUES
    s_dex = v_short in DEX_VENUES
    if l_dex != s_dex:
        return l_dex
    mid_l = 0.5 * (bbo_l[0] + bbo_l[1])
    mid_s = 0.5 * (bbo_s[0] + bbo_s[1])
    sp_l = ((bbo_l[1] - bbo_l[0]) / mid_l * 1e4) if mid_l > 0 else 0.0
    sp_s = ((bbo_s[1] - bbo_s[0]) / mid_s * 1e4) if mid_s > 0 else 0.0
    return sp_l >= sp_s


def _anchored_maker_px(hedge_mid: float, maker_bid: float, maker_ask: float, side: str) -> Decimal:
    if side == "BUY":
        return Decimal(str(min(hedge_mid, maker_bid)))
    return Decimal(str(max(hedge_mid, maker_ask)))


def _exec_basis_bps_dec(long_px: Decimal, short_px: Decimal) -> Decimal:
    if long_px <= 0 or short_px <= 0:
        return Decimal("0")
    mid = Decimal("0.5") * (long_px + short_px)
    return ((short_px - long_px) / mid * Decimal("10000")) if mid > 0 else Decimal("0")


def replay_tape(
    *,
    tape_dir: Path,
    plan: CarryPlan,
    policy_override: CarryPolicy | None = None,
    risk_override: CarryRiskPolicy | None = None,
    runtime_override: CarryRuntimePolicy | None = None,
    execution_mode: str = "hybrid",
) -> dict[str, Any]:
    """Replay recorded slow snapshots & fast BBO streams with realistic L1 fills and net PnL."""
    if execution_mode not in ("hybrid", "maker_ioc", "dual_taker"):
        raise ValueError(f"unsupported execution_mode: {execution_mode!r}")

    base_policy = policy_override or plan.policy
    effective_policy = (
        replace(base_policy, taker_fast_entry_basis_bps=Decimal("0"))
        if execution_mode == "maker_ioc"
        else base_policy
    )
    risk = risk_override or plan.risk
    runtime = runtime_override or plan.runtime
    maker_fees = dict(effective_policy.maker_fee_bps_by_venue)
    taker_fees = dict(effective_policy.taker_fee_bps_by_venue)

    basis_tracker = BasisStopTracker(
        basis_stop_bps=risk.basis_stop_bps,
        persist_mins=risk.basis_persist_mins,
        max_gap_secs=max(30, runtime.slow_poll_secs + 15),
    )
    inversion_tracker = CarryInversionTracker(
        inversion_stop_bps_h=risk.carry_inversion_stop_bps_h,
        persist_mins=risk.carry_inversion_persist_mins,
        max_gap_secs=max(180, runtime.slow_poll_secs * 2 + 30),
    )
    fast_risk = FastRiskEvaluator(risk)
    vacuum_enabled = runtime.max_close_cross_spread_bps > 0 and runtime.close_vacuum_defer_max_secs > 0

    stats: dict[str, int] = {
        "slow_files": 0,
        "fast_files": 0,
        "slow_records": 0,
        "fast_records": 0,
        "truncated_files": 0,
        "skipped_lines": 0,
    }
    diagnostics: dict[str, int] = {
        "candidates_passed": 0,
        "candidates_would_block_d": 0,
        "vetoed_window_recv_within_h_plus_defer": 0,
        "close_vacuum_defers": 0,
    }

    live_quotes: dict[str, tuple[float, float, float]] = {}
    live_mids: dict[str, tuple[float, float]] = {}
    last_leg_info: dict[tuple[str, str], LegMarketInfo] = {}
    active_positions: dict[str, _SimPosition] = {}
    closed_trades: list[dict[str, Any]] = []
    aborted_entries = 0
    halted_reason: str | None = None
    daily_pnl_by_day: dict[str, Decimal] = {}
    cooldown_until_by_sym: dict[str, int] = {}

    maker_orders_submitted = 0
    maker_orders_filled = 0
    taker_orders_submitted = 0
    pos_seq = 0
    last_basis_check_s = -1
    last_fast_check_s = -1
    last_maker_poll_s = -1

    def _fresh_bbo(iid: str, now_f: float) -> tuple[float, float] | None:
        q = live_quotes.get(iid)
        if q is None:
            return None
        if now_f - q[2] > float(runtime.quote_stale_secs):
            live_quotes.pop(iid, None)
            return None
        return q[0], q[1]

    def _fresh_mid_map(now_f: float) -> dict[str, float]:
        max_age = max(float(runtime.quote_stale_secs), float(runtime.slow_poll_secs) + 15.0)
        return {
            iid: m
            for iid, (m, m_ts) in live_mids.items()
            if (now_f - m_ts) <= max_age and m > 0.0
        }

    def _book_fill(
        pos: _SimPosition,
        *,
        is_long: bool,
        is_opening: bool,
        is_maker: bool,
        venue: str,
        qty: Decimal,
        px: Decimal,
    ) -> None:
        if qty <= 0 or px <= 0:
            return
        if is_opening:
            if is_long:
                pos.long_base_qty += qty
                pos.open_l_qty += qty
                pos.open_l_notional += qty * px
            else:
                pos.short_base_qty += qty
                pos.open_s_qty += qty
                pos.open_s_notional += qty * px
        else:
            if is_long:
                pos.long_base_qty = max(Decimal("0"), pos.long_base_qty - qty)
                pos.close_l_qty += qty
                pos.close_l_notional += qty * px
            else:
                pos.short_base_qty = max(Decimal("0"), pos.short_base_qty - qty)
                pos.close_s_qty += qty
                pos.close_s_notional += qty * px
        bps = maker_fees.get(venue, Decimal("0")) if is_maker else taker_fees.get(venue, Decimal("0"))
        pos.fee_usd += (qty * px * bps) / Decimal("10000")

    def _sim_bbo_spread_bps(bbo: tuple[float, float]) -> Decimal:
        bid_d = Decimal(str(bbo[0]))
        ask_d = Decimal(str(bbo[1]))
        mid_d = Decimal("0.5") * (bid_d + ask_d)
        return ((ask_d - bid_d) / mid_d * Decimal("10000")) if mid_d > 0 else Decimal("0")

    def _is_sim_imbalanced(pos: _SimPosition, now_f: float) -> bool:
        cur_l, cur_s = pos.long_base_qty, pos.short_base_qty
        hi = max(cur_l, cur_s)
        if hi <= 0:
            return False
        if cur_l <= 0 or cur_s <= 0:
            return True
        diff = abs(cur_l - cur_s)
        mids = _fresh_mid_map(now_f)
        ref_px = Decimal(str(mids.get(pos.iid_long) or mids.get(pos.iid_short) or pos.entry_l or pos.entry_s or 0))
        unhedged_usd = diff * ref_px
        return unhedged_usd > risk.max_unhedged_usd or (diff / hi * Decimal("100")) > risk.max_qty_mismatch_pct

    def _vacuum_defer_active(pos: _SimPosition, spread_bps: Decimal, ts_s: int, now_f: float) -> bool:
        if not vacuum_enabled or _is_sim_imbalanced(pos, now_f):
            return False
        if (
            pos.close_vacuum_defer_started_ts is not None
            and (ts_s - pos.close_vacuum_defer_started_ts) >= runtime.close_vacuum_defer_max_secs
        ):
            return False
        if spread_bps <= runtime.max_close_cross_spread_bps:
            return False
        if pos.close_vacuum_defer_started_ts is None:
            pos.close_vacuum_defer_started_ts = ts_s
            diagnostics["close_vacuum_defers"] += 1
        return (ts_s - pos.close_vacuum_defer_started_ts) < runtime.close_vacuum_defer_max_secs

    def _finalize_close(pos: _SimPosition, ts_s: int, reason: str) -> None:
        pos.pending_maker = None
        pos.status = "CLOSED"
        pos.closed_ts = ts_s
        pos.close_reason = reason
        active_positions.pop(pos.position_id, None)
        basis_tracker.clear(pos.position_id)
        inversion_tracker.clear(pos.position_id)

        if reason.startswith(("basis_stop", "carry_inversion", "unhedged")) and risk.stop_loss_cooldown_hours > 0:
            until = ts_s + risk.stop_loss_cooldown_hours * _HOUR_SECS
            cooldown_until_by_sym[pos.symbol] = max(cooldown_until_by_sym.get(pos.symbol, 0), until)
        elif reason.startswith(("margin_guard", "soft_margin_guard")):
            mg_h = risk.stop_loss_cooldown_hours if risk.margin_guard_cooldown_hours is None else risk.margin_guard_cooldown_hours
            if mg_h > 0:
                until = ts_s + mg_h * _HOUR_SECS
                cooldown_until_by_sym[pos.symbol] = max(cooldown_until_by_sym.get(pos.symbol, 0), until)

        cost_l = (pos.open_l_notional / pos.open_l_qty) if pos.open_l_qty > 0 else Decimal(str(pos.entry_l))
        cost_s = (pos.open_s_notional / pos.open_s_qty) if pos.open_s_qty > 0 else Decimal(str(pos.entry_s))
        price_pnl = Decimal("0")
        if pos.close_l_qty > 0:
            price_pnl += pos.close_l_notional - pos.close_l_qty * cost_l
        if pos.close_s_qty > 0:
            price_pnl += pos.close_s_qty * cost_s - pos.close_s_notional
        price_q = _q4(price_pnl)
        funding_q = _q4(pos.funding_pnl_usd)
        fee_q = _q4(pos.fee_usd)
        net_q = _q4(price_q + funding_q - fee_q)

        day = _utc_day(ts_s)
        daily_pnl_by_day[day] = daily_pnl_by_day.get(day, Decimal("0")) + net_q

        closed_trades.append(
            {
                "position_id": pos.position_id,
                "symbol": pos.symbol,
                "long_venue": pos.long_venue,
                "short_venue": pos.short_venue,
                "opened_ts": pos.opened_ts,
                "closed_ts": ts_s,
                "hold_hours": round((ts_s - pos.opened_ts) / 3600.0, 2),
                "entry_basis_bps": round(pos.entry_basis_bps, 4),
                "price_pnl_usd": str(price_q),
                "funding_pnl_usd": str(funding_q),
                "fee_usd": str(fee_q),
                "net_pnl_usd": str(net_q),
                "close_reason": reason,
                "entry_pay_leg_cr_bps_h": pos.entry_pay_leg_cr_bps_h,
                "entry_c_live_bps_h": pos.entry_c_live_bps_h,
                "entry_would_block_d": pos.entry_would_block_d,
            }
        )

    def _emergency_close(pos: _SimPosition, ts_s: int, reason: str, now_f: float) -> None:
        nonlocal taker_orders_submitted
        pos.pending_maker = None
        pos.status = "CLOSING"
        bbo_l = _fresh_bbo(pos.iid_long, now_f)
        bbo_s = _fresh_bbo(pos.iid_short, now_f)
        mids = _fresh_mid_map(now_f)
        if pos.long_base_qty > 0:
            px_l = Decimal(str(bbo_l[0] if bbo_l else mids.get(pos.iid_long, pos.entry_l)))
            _book_fill(
                pos,
                is_long=True,
                is_opening=False,
                is_maker=False,
                venue=pos.long_venue,
                qty=pos.long_base_qty,
                px=px_l,
            )
            taker_orders_submitted += 1
        if pos.short_base_qty > 0:
            px_s = Decimal(str(bbo_s[1] if bbo_s else mids.get(pos.iid_short, pos.entry_s)))
            _book_fill(
                pos,
                is_long=False,
                is_opening=False,
                is_maker=False,
                venue=pos.short_venue,
                qty=pos.short_base_qty,
                px=px_s,
            )
            taker_orders_submitted += 1
        _finalize_close(pos, ts_s, reason)

    def _submit_slice(
        pos: _SimPosition,
        *,
        is_opening: bool,
        bbo_l: tuple[float, float] | None,
        bbo_s: tuple[float, float] | None,
        ts_s: int,
        now_f: float | None = None,
    ) -> bool:
        nonlocal maker_orders_submitted, taker_orders_submitted
        now_f_val = float(ts_s) if now_f is None else now_f
        if is_opening:
            if bbo_l is None or bbo_s is None:
                return False
            bid_l_dec = Decimal(str(bbo_l[0]))
            ask_l_dec = Decimal(str(bbo_l[1]))
            bid_s_dec = Decimal(str(bbo_s[0]))
            ask_s_dec = Decimal(str(bbo_s[1]))
            mid_l = (bid_l_dec + ask_l_dec) / Decimal("2")
            mid_s = (bid_s_dec + ask_s_dec) / Decimal("2")
            mid = (mid_l + mid_s) / Decimal("2")
        else:
            if (pos.long_base_qty > 0 and bbo_l is None) or (pos.short_base_qty > 0 and bbo_s is None):
                return False
            mid_l = ((Decimal(str(bbo_l[0])) + Decimal(str(bbo_l[1]))) / Decimal("2")) if bbo_l is not None else Decimal("0")
            mid_s = ((Decimal(str(bbo_s[0])) + Decimal(str(bbo_s[1]))) / Decimal("2")) if bbo_s is not None else Decimal("0")
            if pos.long_base_qty > 0 and pos.short_base_qty > 0:
                mid = (mid_l + mid_s) / Decimal("2")
            elif pos.long_base_qty > 0:
                mid = mid_l
            else:
                mid = mid_s
        if mid <= 0:
            return False

        if is_opening:
            assert bbo_l is not None and bbo_s is not None
            if (
                bid_l_dec <= 0
                or ask_l_dec < bid_l_dec
                or bid_s_dec <= 0
                or ask_s_dec < bid_s_dec
                or mid_l <= 0
                or mid_s <= 0
            ):
                return False
            ratio = mid_s / mid_l
            dev = effective_policy.max_price_ratio_deviation
            if ratio < Decimal("1") - dev or ratio > Decimal("1") + dev:
                return False
            sp_l = (ask_l_dec - bid_l_dec) / mid_l * Decimal("10000")
            sp_s = (ask_s_dec - bid_s_dec) / mid_s * Decimal("10000")
            if sp_l > effective_policy.max_leg_spread_bps or sp_s > effective_policy.max_leg_spread_bps:
                return False
            rem_base = pos.target_base_qty - min(pos.long_base_qty, pos.short_base_qty)
            slice_usd = min(risk.slice_usd, rem_base * mid)
            if slice_usd <= 0:
                pos.status = "OPEN"
                return False
            slice_qty = _q4(slice_usd / mid)
            if slice_qty <= 0:
                return False
            mid_basis = _exec_basis_bps_dec(mid_l, mid_s)
            if mid_basis < effective_policy.min_entry_basis_bps or mid_basis > effective_policy.max_entry_basis_bps:
                return False
            taker_b = _exec_basis_bps_dec(Decimal(str(bbo_l[1])), Decimal(str(bbo_s[0])))
            use_dual_taker = execution_mode == "dual_taker" or (
                effective_policy.taker_fast_entry_basis_bps > 0
                and effective_policy.taker_fast_entry_basis_bps <= taker_b <= effective_policy.max_entry_basis_bps
            )
            if use_dual_taker:
                if taker_b > effective_policy.max_entry_basis_bps:
                    return False
                _book_fill(
                    pos, is_long=True, is_opening=True, is_maker=False,
                    venue=pos.long_venue, qty=slice_qty, px=Decimal(str(bbo_l[1])),
                )
                _book_fill(
                    pos, is_long=False, is_opening=True, is_maker=False,
                    venue=pos.short_venue, qty=slice_qty, px=Decimal(str(bbo_s[0])),
                )
                taker_orders_submitted += 2
                if min(pos.long_base_qty, pos.short_base_qty) >= pos.target_base_qty * Decimal("0.98"):
                    pos.status = "OPEN"
                    pos.slice_attempts = 0
                return True
        else:
            rem_base = max(pos.long_base_qty, pos.short_base_qty)
            if rem_base <= 0:
                _finalize_close(pos, ts_s, pos.close_reason or "carry_decay")
                return False
            imbalanced = _is_sim_imbalanced(pos, now_f_val)
            vacuum_expired = (
                vacuum_enabled
                and pos.close_vacuum_defer_started_ts is not None
                and (ts_s - pos.close_vacuum_defer_started_ts) >= runtime.close_vacuum_defer_max_secs
            )
            slice_usd = min(risk.slice_usd, rem_base * mid)
            slice_qty = min(rem_base, _q4(slice_usd / mid)) if rem_base * mid > risk.slice_usd else rem_base
            if (
                execution_mode == "dual_taker"
                or pos.slice_attempts >= runtime.maker_max_attempts
                or vacuum_expired
                or imbalanced
            ):
                cross_spreads: list[Decimal] = []
                if pos.long_base_qty > 0 and bbo_l is not None and bbo_l[0] > 0 and bbo_l[1] >= bbo_l[0]:
                    cross_spreads.append(_sim_bbo_spread_bps(bbo_l))
                if pos.short_base_qty > 0 and bbo_s is not None and bbo_s[0] > 0 and bbo_s[1] >= bbo_s[0]:
                    cross_spreads.append(_sim_bbo_spread_bps(bbo_s))
                cross_sp = max(cross_spreads, default=Decimal("0"))
                if _vacuum_defer_active(pos, cross_sp, ts_s, now_f_val):
                    return False
                if pos.long_base_qty > 0:
                    assert bbo_l is not None
                    q_l = min(pos.long_base_qty, slice_qty)
                    _book_fill(
                        pos, is_long=True, is_opening=False, is_maker=False,
                        venue=pos.long_venue, qty=q_l, px=Decimal(str(bbo_l[0])),
                    )
                    taker_orders_submitted += 1
                if pos.short_base_qty > 0:
                    assert bbo_s is not None
                    q_s = min(pos.short_base_qty, slice_qty)
                    _book_fill(
                        pos, is_long=False, is_opening=False, is_maker=False,
                        venue=pos.short_venue, qty=q_s, px=Decimal(str(bbo_s[1])),
                    )
                    taker_orders_submitted += 1
                if pos.long_base_qty <= 0 and pos.short_base_qty <= 0:
                    _finalize_close(pos, ts_s, pos.close_reason or "carry_decay")
                return True

        assert bbo_l is not None and bbo_s is not None
        maker_is_long = _choose_maker_is_long(pos.long_venue, pos.short_venue, bbo_l, bbo_s)
        if (not is_opening) and _vacuum_defer_active(
            pos, _sim_bbo_spread_bps(bbo_s if maker_is_long else bbo_l), ts_s, now_f_val
        ):
            return False
        if maker_is_long:
            maker_side = "BUY" if is_opening else "SELL"
            px = _anchored_maker_px(float(mid_s), bbo_l[0], bbo_l[1], maker_side)
            if is_opening:
                est_b = _exec_basis_bps_dec(px, Decimal(str(bbo_s[0])))
                if est_b < effective_policy.min_exec_basis_bps or est_b > effective_policy.max_entry_basis_bps:
                    return False
            maker_venue, hedge_venue = pos.long_venue, pos.short_venue
            maker_iid, hedge_iid = pos.iid_long, pos.iid_short
        else:
            maker_side = "SELL" if is_opening else "BUY"
            px = _anchored_maker_px(float(mid_l), bbo_s[0], bbo_s[1], maker_side)
            if is_opening:
                est_b = _exec_basis_bps_dec(Decimal(str(bbo_l[1])), px)
                if est_b < effective_policy.min_exec_basis_bps or est_b > effective_policy.max_entry_basis_bps:
                    return False
            maker_venue, hedge_venue = pos.short_venue, pos.long_venue
            maker_iid, hedge_iid = pos.iid_short, pos.iid_long

        pos.pending_maker = _PendingMakerSlice(
            is_opening=is_opening,
            maker_side_is_long=maker_is_long,
            maker_venue=maker_venue,
            hedge_venue=hedge_venue,
            maker_iid=maker_iid,
            hedge_iid=hedge_iid,
            side=maker_side,
            limit_price=px,
            base_qty=slice_qty,
            submitted_ts=ts_s,
            attempts=pos.slice_attempts + 1,
        )
        maker_orders_submitted += 1
        return True

    def _advance_position_slices(pos: _SimPosition, ts_s: int, now_f: float) -> None:
        nonlocal aborted_entries
        if pos.pending_maker is not None or halted_reason is not None:
            return
        bbo_l = _fresh_bbo(pos.iid_long, now_f)
        bbo_s = _fresh_bbo(pos.iid_short, now_f)
        if pos.status == "OPENING":
            if pos.slice_attempts >= runtime.maker_max_attempts:
                if pos.long_base_qty <= 0 and pos.short_base_qty <= 0:
                    active_positions.pop(pos.position_id, None)
                    aborted_entries += 1
                else:
                    pos.status = "OPEN"
                    pos.slice_attempts = 0
                return
            if bbo_l is None or bbo_s is None:
                return
            if not _submit_slice(pos, is_opening=True, bbo_l=bbo_l, bbo_s=bbo_s, ts_s=ts_s, now_f=now_f):
                pos.slice_attempts += 1
                if pos.slice_attempts >= runtime.maker_max_attempts:
                    if pos.long_base_qty <= 0 and pos.short_base_qty <= 0:
                        active_positions.pop(pos.position_id, None)
                        aborted_entries += 1
                    else:
                        pos.status = "OPEN"
                        pos.slice_attempts = 0
        elif pos.status == "CLOSING":
            if pos.long_base_qty <= 0 and pos.short_base_qty <= 0:
                _finalize_close(pos, ts_s, pos.close_reason or "carry_decay")
                return
            imbalanced_now = _is_sim_imbalanced(pos, now_f)
            vacuum_expired = (
                vacuum_enabled
                and pos.close_vacuum_defer_started_ts is not None
                and (ts_s - pos.close_vacuum_defer_started_ts) >= runtime.close_vacuum_defer_max_secs
            )
            missing_leg_bbo = (pos.long_base_qty > 0 and bbo_l is None) or (pos.short_base_qty > 0 and bbo_s is None)
            if missing_leg_bbo:
                if vacuum_enabled and not imbalanced_now and not vacuum_expired:
                    if pos.close_vacuum_defer_started_ts is None:
                        pos.close_vacuum_defer_started_ts = ts_s
                        diagnostics["close_vacuum_defers"] += 1
                    return
                if (
                    execution_mode == "dual_taker"
                    or pos.slice_attempts >= runtime.maker_max_attempts
                    or vacuum_expired
                    or imbalanced_now
                ):
                    mids_now = _fresh_mid_map(now_f)
                    ml = mids_now.get(pos.iid_long)
                    ms = mids_now.get(pos.iid_short)
                    if pos.long_base_qty > 0 and bbo_l is None and ml and ml > 0:
                        bbo_l = (ml, ml)
                    if pos.short_base_qty > 0 and bbo_s is None and ms and ms > 0:
                        bbo_s = (ms, ms)
                if (pos.long_base_qty > 0 and bbo_l is None) or (pos.short_base_qty > 0 and bbo_s is None):
                    return
            _submit_slice(pos, is_opening=False, bbo_l=bbo_l, bbo_s=bbo_s, ts_s=ts_s, now_f=now_f)

    def _start_closing(pos: _SimPosition, ts_s: int, reason: str, now_f: float) -> None:
        if pos.status != "CLOSING":
            if pos.pending_maker is not None and pos.pending_maker.is_opening:
                pos.pending_maker = None
            pos.status = "CLOSING"
            pos.close_reason = reason
            pos.slice_attempts = 0
            pos.close_vacuum_defer_started_ts = None
            pos.exit_defer_target_ts = None
        elif close_reason_priority(reason) > close_reason_priority(pos.close_reason):
            pos.close_reason = reason
        if pos.pending_maker is None:
            _advance_position_slices(pos, ts_s, now_f)

    def _poll_pending_makers_1s(ts_s: int, now_f: float) -> None:
        """Mirror runtime _on_fast_risk_timer -> poll_active_orders closing-vacuum, deadline & timeout checks at 1s cadence."""
        for pos in list(active_positions.values()):
            pm = pos.pending_maker
            if pm is None:
                continue
            if (not pm.is_opening) and vacuum_enabled:
                expired_c = (
                    pos.close_vacuum_defer_started_ts is not None
                    and (ts_s - pos.close_vacuum_defer_started_ts) >= runtime.close_vacuum_defer_max_secs
                )
                if expired_c:
                    pos.pending_maker = None
                    _advance_position_slices(pos, ts_s, now_f)
                    continue
                q_h = _fresh_bbo(pm.hedge_iid, now_f)
                if q_h is not None and _vacuum_defer_active(pos, _sim_bbo_spread_bps(q_h), ts_s, now_f):
                    pos.pending_maker = None
                    continue
            if ts_s - pm.submitted_ts >= runtime.maker_requote_secs:
                pos.pending_maker = None
                pos.slice_attempts += 1
                _advance_position_slices(pos, ts_s, now_f)

    def _check_pending_makers(ts_s: int, now_f: float, trigger_iid: str | None = None) -> None:
        nonlocal maker_orders_filled, taker_orders_submitted, aborted_entries
        for pos in list(active_positions.values()):
            pm = pos.pending_maker
            if pm is None:
                continue
            if trigger_iid is None or trigger_iid == pm.maker_iid:
                q_m = _fresh_bbo(pm.maker_iid, now_f)
                if q_m is not None:
                    bid_f, ask_f = q_m
                    px_f = float(pm.limit_price)
                    crossed = (ask_f <= px_f or bid_f < px_f) if pm.side == "BUY" else (bid_f >= px_f or ask_f > px_f)
                    if crossed:
                        maker_orders_filled += 1
                        _book_fill(
                            pos,
                            is_long=pm.maker_side_is_long,
                            is_opening=pm.is_opening,
                            is_maker=True,
                            venue=pm.maker_venue,
                            qty=pm.base_qty,
                            px=pm.limit_price,
                        )
                        hedge_bbo = _fresh_bbo(pm.hedge_iid, now_f)
                        if hedge_bbo is not None:
                            hedge_is_long = not pm.maker_side_is_long
                            hedge_side_buy = hedge_is_long if pm.is_opening else (not hedge_is_long)
                            hedge_px = Decimal(str(hedge_bbo[1] if hedge_side_buy else hedge_bbo[0]))
                        else:
                            mids_now = _fresh_mid_map(now_f)
                            fb = pos.entry_s if pm.maker_side_is_long else pos.entry_l
                            hedge_px = Decimal(str(mids_now.get(pm.hedge_iid, fb)))
                        _book_fill(
                            pos,
                            is_long=not pm.maker_side_is_long,
                            is_opening=pm.is_opening,
                            is_maker=False,
                            venue=pm.hedge_venue,
                            qty=pm.base_qty,
                            px=hedge_px,
                        )
                        taker_orders_submitted += 1
                        pos.pending_maker = None
                        pos.slice_attempts = 0
                        if pm.is_opening:
                            if min(pos.long_base_qty, pos.short_base_qty) >= pos.target_base_qty * Decimal("0.98"):
                                pos.status = "OPEN"
                            else:
                                _advance_position_slices(pos, ts_s, now_f)
                        else:
                            if pos.long_base_qty <= 0 and pos.short_base_qty <= 0:
                                _finalize_close(pos, ts_s, pos.close_reason or "carry_decay")
                            else:
                                _advance_position_slices(pos, ts_s, now_f)
                        continue

    def _accrue_funding_for_slow(snapshot: Mapping[str, Mapping[str, LegMarketInfo]], ts_s: int) -> None:
        for pos in list(active_positions.values()):
            legs = (
                ("LONG", pos.long_venue, pos.long_base_qty, True, pos.entry_l),
                ("SHORT", pos.short_venue, pos.short_base_qty, False, pos.entry_s),
            )
            for _side_name, venue, qty, is_long, entry_px in legs:
                if qty <= 0:
                    continue
                cur_leg = snapshot.get(pos.symbol, {}).get(venue)
                prev_leg = last_leg_info.get((venue, pos.symbol))
                mark_px = (
                    cur_leg.mark_price
                    if (cur_leg is not None and cur_leg.mark_price > 0)
                    else (prev_leg.mark_price if (prev_leg is not None and prev_leg.mark_price > 0) else entry_px)
                )
                if mark_px <= 0:
                    continue

                candidates_ts: list[tuple[int, float]] = []
                if prev_leg is not None and prev_leg.next_settle_ts > 0:
                    p_rate = prev_leg.current_rate_bps_h if prev_leg.current_rate_bps_h is not None else prev_leg.r8_bps_h
                    if p_rate is not None:
                        step_s = max(1, prev_leg.interval_h) * _HOUR_SECS
                        t_settle = prev_leg.next_settle_ts
                        cur_ns = cur_leg.next_settle_ts if cur_leg is not None else 0
                        while t_settle <= ts_s and (cur_ns <= 0 or t_settle < cur_ns or t_settle == cur_ns):
                            if pos.opened_ts < t_settle <= ts_s:
                                candidates_ts.append((t_settle, float(p_rate) * float(prev_leg.interval_h)))
                            t_settle += step_s
                if cur_leg is not None and cur_leg.next_settle_ts > 0 and pos.opened_ts < cur_leg.next_settle_ts <= ts_s:
                    c_rate = cur_leg.current_rate_bps_h if cur_leg.current_rate_bps_h is not None else cur_leg.r8_bps_h
                    if c_rate is not None:
                        candidates_ts.append((cur_leg.next_settle_ts, float(c_rate) * float(cur_leg.interval_h)))

                for settle_ts, rate_bps in candidates_ts:
                    key = (venue, settle_ts)
                    if key in pos.settled_keys:
                        continue
                    pos.settled_keys.add(key)
                    raw = (Decimal(str(rate_bps)) / Decimal("10000")) * qty * Decimal(str(mark_px))
                    pos.funding_pnl_usd += _q4(-raw if is_long else raw)

        for sym, vmap in snapshot.items():
            for venue, leg in vmap.items():
                last_leg_info[(venue, sym)] = leg

    def _next_maker_due_ts() -> int | None:
        earliest: int | None = None
        for pos in active_positions.values():
            pm = pos.pending_maker
            if pm is None:
                continue
            due = pm.submitted_ts + runtime.maker_requote_secs
            if (not pm.is_opening) and vacuum_enabled and pos.close_vacuum_defer_started_ts is not None:
                due = min(due, pos.close_vacuum_defer_started_ts + runtime.close_vacuum_defer_max_secs)
            if earliest is None or due < earliest:
                earliest = due
        return earliest

    def _advance_maker_timers_before(ts_s: int) -> None:
        nonlocal last_maker_poll_s
        step = max(1, int(runtime.fast_risk_poll_secs))
        if last_maker_poll_s >= 0 and (ts_s - last_maker_poll_s) >= step:
            if _next_maker_due_ts() is not None:
                t_first = last_maker_poll_s + step
                if t_first <= ts_s:
                    _poll_pending_makers_1s(t_first, float(t_first))
                    last_maker_poll_s = t_first
            for _ in range(32):
                due = _next_maker_due_ts()
                if due is None or due > ts_s:
                    break
                t_tick = max(last_maker_poll_s + step, due)
                if t_tick > ts_s:
                    break
                _poll_pending_makers_1s(t_tick, float(t_tick))
                last_maker_poll_s = t_tick
            last_maker_poll_s = max(last_maker_poll_s, ts_s - step)

    def _run_risk_checks(ts_s: int, now_f: float) -> None:
        nonlocal halted_reason, last_basis_check_s, last_fast_check_s, last_maker_poll_s
        mids = _fresh_mid_map(now_f)
        if last_basis_check_s < 0 or (ts_s - last_basis_check_s) >= runtime.basis_sample_secs:
            last_basis_check_s = ts_s
            for pid, pos in list(active_positions.items()):
                ml = mids.get(pos.iid_long)
                ms = mids.get(pos.iid_short)
                ts_l = live_mids.get(pos.iid_long, (0.0, 0.0))[1]
                ts_s_leg = live_mids.get(pos.iid_short, (0.0, 0.0))[1]
                if ml is not None and ms is not None and ml > 0 and ms > 0:
                    b_now = (ms - ml) / (0.5 * (ml + ms)) * 1e4
                    sample_ts = int(min(ts_l, ts_s_leg))
                else:
                    b_now = None
                    sample_ts = None
                hit, _ = basis_tracker.observe(
                    position_id=pid,
                    entry_basis_bps=pos.entry_basis_bps,
                    basis_now_bps=b_now,
                    now_s=ts_s,
                    sample_ts=sample_ts,
                )
                if hit:
                    reason = f"basis_stop_{risk.basis_persist_mins}m"
                    if risk.basis_stop_via_closing:
                        _start_closing(pos, ts_s, reason, now_f)
                    else:
                        _emergency_close(pos, ts_s, reason, now_f)

        if last_fast_check_s < 0 or (ts_s - last_fast_check_s) >= runtime.fast_risk_poll_secs:
            last_fast_check_s = ts_s
            if last_maker_poll_s < 0 or (ts_s - last_maker_poll_s) >= runtime.fast_risk_poll_secs:
                _poll_pending_makers_1s(ts_s, now_f)
                last_maker_poll_s = ts_s
            daily_pnl = daily_pnl_by_day.get(_utc_day(ts_s), Decimal("0"))
            quotes_now = {
                iid: q
                for pos in active_positions.values()
                for iid in (pos.iid_long, pos.iid_short)
                if (q := _fresh_bbo(iid, now_f)) is not None
            }
            dec = fast_risk.evaluate(
                positions={pid: pos.as_risk_row() for pid, pos in active_positions.items()},
                live_mids=mids,
                daily_realized_pnl_usd=daily_pnl,
                consecutive_unhedged=0,
                now_s=ts_s,
                live_quotes=quotes_now,
            )
            if dec.halt_system and halted_reason is None:
                halted_reason = dec.halt_reason
            for pid, reason in dec.close_positions:
                pos = active_positions.get(pid)
                if pos is not None:
                    _emergency_close(pos, ts_s, reason, now_f)
            for pid, reason in dec.soft_close_positions:
                pos = active_positions.get(pid)
                if pos is not None:
                    _start_closing(pos, ts_s, reason, now_f)

    for ts, kind_order, row in _merged_tape_events(tape_dir, stats):
        ts_s = int(ts)
        _advance_maker_timers_before(ts_s)
        if kind_order == 0:
            iid = str(row.get("iid") or "")
            bid = float(row.get("b") or 0.0)
            ask = float(row.get("a") or 0.0)
            if not iid or bid <= 0.0 or ask < bid:
                continue
            mid = float(row.get("m") or float((Decimal(str(bid)) + Decimal(str(ask))) * Decimal("0.5")))
            live_quotes[iid] = (bid, ask, ts)
            live_mids[iid] = (mid, ts)
            _check_pending_makers(ts_s, ts, trigger_iid=iid)
            for pos in list(active_positions.values()):
                if pos.pending_maker is None and pos.status in ("OPENING", "CLOSING"):
                    _advance_position_slices(pos, ts_s, ts)
            _run_risk_checks(ts_s, ts)
            continue

        snap = _decode_slow_snapshot(row, ts_s)
        for vmap in snap.values():
            for v, leg in list(vmap.items()):
                q = _fresh_bbo(leg.instrument_id, ts)
                if q is not None:
                    vmap[v] = LegMarketInfo(**{**leg.__dict__, "bid": q[0], "ask": q[1]})
                else:
                    live_mids[leg.instrument_id] = (leg.mid, ts)

        _accrue_funding_for_slow(snap, ts_s)
        _check_pending_makers(ts_s, ts)
        for pos in list(active_positions.values()):
            if pos.pending_maker is None and pos.status in ("OPENING", "CLOSING"):
                _advance_position_slices(pos, ts_s, ts)
        _run_risk_checks(ts_s, ts)

        for pid, pos in list(active_positions.items()):
            vmap = snap.get(pos.symbol, {})
            ll, ls = vmap.get(pos.long_venue), vmap.get(pos.short_venue)
            if ll is not None and ls is not None:
                rate_l = ll.current_rate_bps_h if ll.current_rate_bps_h is not None else ll.r8_bps_h
                rate_s = ls.current_rate_bps_h if ls.current_rate_bps_h is not None else ls.r8_bps_h
                live_c = (
                    (Decimal(str(rate_s)) - Decimal(str(rate_l)))
                    if (rate_l is not None and rate_s is not None)
                    else None
                )
                inv_hit, _ = inversion_tracker.observe(position_id=pid, live_carry_bps_h=live_c, now_s=ts_s)
                if inv_hit:
                    _start_closing(pos, ts_s, f"carry_inversion_{risk.carry_inversion_persist_mins}m", ts)
                    continue
                if pos.status not in ("OPEN", "OPENING"):
                    continue
                do_exit, rsn = should_exit_carry_decay(
                    opened_ts=pos.opened_ts,
                    leg_l=ll,
                    leg_s=ls,
                    policy=effective_policy,
                    now_s=ts_s,
                    pinned_defer_ts=pos.exit_defer_target_ts,
                )
                if rsn == "carry_healthy":
                    pos.exit_defer_target_ts = None
                elif (
                    rsn == "defer_collect_cex_settlement"
                    and effective_policy.cex_exit_defer_dual_leg_net
                    and pos.exit_defer_target_ts is None
                ):
                    pos.exit_defer_target_ts = select_cex_exit_defer_ts(ll, ls, effective_policy, ts_s)
                if do_exit:
                    _start_closing(pos, ts_s, rsn, ts)
            else:
                inversion_tracker.observe(position_id=pid, live_carry_bps_h=None, now_s=ts_s)

        if halted_reason is not None:
            continue

        open_syms = {p.symbol for p in active_positions.values()}
        venue_used: dict[str, Decimal] = {}
        for p in active_positions.values():
            venue_used[p.long_venue] = venue_used.get(p.long_venue, Decimal("0")) + risk.leg_notional_usd
            venue_used[p.short_venue] = venue_used.get(p.short_venue, Decimal("0")) + risk.leg_notional_usd
        cooldown_syms = {sym for sym, until in cooldown_until_by_sym.items() if ts_s < until}

        vetoed_window_pairs: list[tuple[LegMarketInfo, LegMarketInfo]] = []
        candidates = evaluate_entry_candidates(
            snap,
            open_symbols=open_syms,
            venue_notional_used=venue_used,
            excluded_symbols=set(plan.excluded_symbols) | cooldown_syms,
            policy=effective_policy,
            risk=risk,
            now_s=ts_s,
            respect_capacity=True,
            vetoed_window_pairs_out=vetoed_window_pairs,
        )
        defer_pad_s = int(effective_policy.cex_exit_defer_before_settle_mins) * 60
        if defer_pad_s > 0 and vetoed_window_pairs:
            horizon_s = ts_s + int(effective_policy.min_hold_h) * _HOUR_SECS
            for ll_d, ls_d in vetoed_window_pairs:
                rl = ll_d.current_rate_bps_h if ll_d.current_rate_bps_h is not None else ll_d.r8_bps_h
                rs = ls_d.current_rate_bps_h if ls_d.current_rate_bps_h is not None else ls_d.r8_bps_h
                near_recv = any(
                    rate is not None
                    and ((float(rate) < 0.0) if is_l else (float(rate) > 0.0))
                    and horizon_s < int(lg.next_settle_ts) <= horizon_s + defer_pad_s
                    for lg, is_l, rate in ((ll_d, True, rl), (ls_d, False, rs))
                )
                if near_recv:
                    diagnostics["vetoed_window_recv_within_h_plus_defer"] += 1

        for cand in candidates:
            cr_l = cand.long_leg.current_rate_bps_h
            cr_s = cand.short_leg.current_rate_bps_h
            pay_leg_cr = (
                (max(0.0, float(cr_l)) + max(0.0, -float(cr_s)))
                if (cr_l is not None and cr_s is not None)
                else None
            )
            would_block_d = bool(
                pay_leg_cr is not None and pay_leg_cr >= 8.0 and cand.net_score_bps_h < 0.25 * pay_leg_cr
            )
            diagnostics["candidates_passed"] += 1
            if would_block_d:
                diagnostics["candidates_would_block_d"] += 1

        for cand in candidates:
            cr_l = cand.long_leg.current_rate_bps_h
            cr_s = cand.short_leg.current_rate_bps_h
            pay_leg_cr = (
                (max(0.0, float(cr_l)) + max(0.0, -float(cr_s)))
                if (cr_l is not None and cr_s is not None)
                else None
            )
            c_live_entry = (float(cr_s) - float(cr_l)) if (cr_l is not None and cr_s is not None) else None
            would_block_d = bool(
                pay_leg_cr is not None and pay_leg_cr >= 8.0 and cand.net_score_bps_h < 0.25 * pay_leg_cr
            )
            bbo_l = _fresh_bbo(cand.long_leg.instrument_id, ts) or (cand.long_leg.bid, cand.long_leg.ask)
            bbo_s = _fresh_bbo(cand.short_leg.instrument_id, ts) or (cand.short_leg.bid, cand.short_leg.ask)
            live_quotes[cand.long_leg.instrument_id] = (bbo_l[0], bbo_l[1], ts)
            live_quotes[cand.short_leg.instrument_id] = (bbo_s[0], bbo_s[1], ts)
            mid_entry = Decimal(str(0.5 * (cand.long_leg.mid + cand.short_leg.mid)))
            if mid_entry <= 0:
                continue
            target_qty = _q4(risk.leg_notional_usd / mid_entry)
            if target_qty <= 0:
                continue
            pos_seq += 1
            pid = f"{cand.symbol}-{ts_s}-{pos_seq:04d}"
            pos = _SimPosition(
                position_id=pid,
                symbol=cand.symbol,
                long_venue=cand.long_venue,
                short_venue=cand.short_venue,
                iid_long=cand.long_leg.instrument_id,
                iid_short=cand.short_leg.instrument_id,
                opened_ts=ts_s,
                entry_l=cand.long_leg.mid,
                entry_s=cand.short_leg.mid,
                entry_basis_bps=cand.basis_bps,
                target_base_qty=target_qty,
                entry_pay_leg_cr_bps_h=round(pay_leg_cr, 4) if pay_leg_cr is not None else None,
                entry_c_live_bps_h=round(c_live_entry, 4) if c_live_entry is not None else None,
                entry_would_block_d=would_block_d,
            )
            active_positions[pid] = pos
            if not _submit_slice(pos, is_opening=True, bbo_l=bbo_l, bbo_s=bbo_s, ts_s=ts_s, now_f=ts):
                active_positions.pop(pid, None)
                aborted_entries += 1
                continue
            break

    total_price = Decimal("0")
    total_funding = Decimal("0")
    total_fee = Decimal("0")
    total_net = Decimal("0")
    wins = 0
    cum_net = Decimal("0")
    peak_net = Decimal("0")
    max_dd = Decimal("0")

    by_pair: dict[str, dict[str, Any]] = {}
    by_reason: dict[str, dict[str, Any]] = {}

    for t in closed_trades:
        p_usd = Decimal(t["price_pnl_usd"])
        f_usd = Decimal(t["funding_pnl_usd"])
        fee = Decimal(t["fee_usd"])
        net = Decimal(t["net_pnl_usd"])
        total_price += p_usd
        total_funding += f_usd
        total_fee += fee
        total_net += net
        if net > 0:
            wins += 1
        cum_net += net
        if cum_net > peak_net:
            peak_net = cum_net
        dd = peak_net - cum_net
        if dd > max_dd:
            max_dd = dd

        pair_key = f"{t['long_venue']}->{t['short_venue']}"
        pb = by_pair.setdefault(
            pair_key,
            {"trades": 0, "wins": 0, "price_pnl_usd": Decimal("0"), "funding_pnl_usd": Decimal("0"), "fee_usd": Decimal("0"), "net_pnl_usd": Decimal("0")},
        )
        pb["trades"] += 1
        pb["wins"] += 1 if net > 0 else 0
        pb["price_pnl_usd"] += p_usd
        pb["funding_pnl_usd"] += f_usd
        pb["fee_usd"] += fee
        pb["net_pnl_usd"] += net

        rsn_key = str(t["close_reason"])
        rb = by_reason.setdefault(
            rsn_key,
            {"trades": 0, "wins": 0, "price_pnl_usd": Decimal("0"), "funding_pnl_usd": Decimal("0"), "fee_usd": Decimal("0"), "net_pnl_usd": Decimal("0")},
        )
        rb["trades"] += 1
        rb["wins"] += 1 if net > 0 else 0
        rb["price_pnl_usd"] += p_usd
        rb["funding_pnl_usd"] += f_usd
        rb["fee_usd"] += fee
        rb["net_pnl_usd"] += net

    def _serialize_group(groups: Mapping[str, dict[str, Any]]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for k, g in sorted(groups.items()):
            cnt = g["trades"]
            out[k] = {
                "trades": cnt,
                "win_rate": round(g["wins"] / cnt, 4) if cnt > 0 else 0.0,
                "price_pnl_usd": str(_q4(g["price_pnl_usd"])),
                "funding_pnl_usd": str(_q4(g["funding_pnl_usd"])),
                "fee_usd": str(_q4(g["fee_usd"])),
                "net_pnl_usd": str(_q4(g["net_pnl_usd"])),
            }
        return out

    n_trades = len(closed_trades)
    maker_fill_rate = round(maker_orders_filled / maker_orders_submitted, 4) if maker_orders_submitted > 0 else 0.0
    win_rate = round(wins / n_trades, 4) if n_trades > 0 else 0.0

    open_out = [
        {
            "position_id": p.position_id,
            "symbol": p.symbol,
            "long_venue": p.long_venue,
            "short_venue": p.short_venue,
            "status": p.status,
            "opened_ts": p.opened_ts,
            "long_base_qty": str(p.long_base_qty),
            "short_base_qty": str(p.short_base_qty),
            "funding_pnl_usd": str(_q4(p.funding_pnl_usd)),
            "fee_usd": str(_q4(p.fee_usd)),
        }
        for p in sorted(active_positions.values(), key=lambda x: (x.opened_ts, x.position_id))
    ]

    return {
        "tape_dir": str(tape_dir.resolve()),
        "execution_mode": execution_mode,
        "halted_reason": halted_reason,
        "policy": {
            "entry_carry_bps_h": str(effective_policy.entry_carry_bps_h),
            "exit_carry_8h_bps_h": str(effective_policy.exit_carry_8h_bps_h),
            "min_hold_h": effective_policy.min_hold_h,
            "min_entry_basis_bps": str(effective_policy.min_entry_basis_bps),
            "min_exec_basis_bps": str(effective_policy.min_exec_basis_bps),
            "taker_fast_entry_basis_bps": str(effective_policy.taker_fast_entry_basis_bps),
            "max_entry_basis_bps": str(effective_policy.max_entry_basis_bps),
        },
        "summary": {
            "total_trades": n_trades,
            "open_positions_count": len(open_out),
            "aborted_entries": aborted_entries,
            "win_rate": win_rate,
            "maker_orders_submitted": maker_orders_submitted,
            "maker_orders_filled": maker_orders_filled,
            "maker_fill_rate": maker_fill_rate,
            "taker_orders_submitted": taker_orders_submitted,
            "price_pnl_usd": str(_q4(total_price)),
            "funding_pnl_usd": str(_q4(total_funding)),
            "fee_usd": str(_q4(total_fee)),
            "net_pnl_usd": str(_q4(total_net)),
            "max_drawdown_usd": str(_q4(max_dd)),
        },
        "by_venue_pair": _serialize_group(by_pair),
        "by_close_reason": _serialize_group(by_reason),
        "tape_stats": stats,
        "diagnostics": diagnostics,
        "open_positions": open_out,
        "trades": closed_trades,
    }


def _format_replay_text(report: Mapping[str, Any]) -> str:
    s = report["summary"]
    ts = report["tape_stats"]
    lines = [
        f"Carry Tape Replay: {report['tape_dir']} (mode={report['execution_mode']})",
        (
            f"Files: slow={ts['slow_files']} ({ts['slow_records']} rows) "
            f"fast={ts['fast_files']} ({ts['fast_records']} rows) "
            f"truncated={ts['truncated_files']} skipped_lines={ts['skipped_lines']}"
        ),
        (
            f"Trades: closed={s['total_trades']} open={s['open_positions_count']} "
            f"aborted={s['aborted_entries']} win_rate={s['win_rate']:.2%} "
            f"maker_fill_rate={s['maker_fill_rate']:.2%} ({s['maker_orders_filled']}/{s['maker_orders_submitted']})"
        ),
        (
            f"Net PnL (USD): price={s['price_pnl_usd']} funding={s['funding_pnl_usd']} "
            f"fee=-{s['fee_usd']} net={s['net_pnl_usd']} max_dd={s['max_drawdown_usd']}"
        ),
    ]
    if report["halted_reason"]:
        lines.append(f"Halted: {report['halted_reason']}")
    if report["by_venue_pair"]:
        lines.append("\nBy Venue Pair:")
        for pair, g in report["by_venue_pair"].items():
            lines.append(
                f"  {pair:<14} trades={g['trades']} win={g['win_rate']:.2%} "
                f"price={g['price_pnl_usd']} funding={g['funding_pnl_usd']} fee={g['fee_usd']} net={g['net_pnl_usd']}"
            )
    if report["by_close_reason"]:
        lines.append("\nBy Close Reason:")
        for rsn, g in report["by_close_reason"].items():
            lines.append(
                f"  {rsn:<22} trades={g['trades']} win={g['win_rate']:.2%} net={g['net_pnl_usd']}"
            )
    return "\n".join(lines)


def _apply_policy_overrides(policy: CarryPolicy, args: argparse.Namespace) -> CarryPolicy:
    updates: dict[str, Any] = {}
    for attr in (
        "entry_carry_bps_h",
        "exit_carry_8h_bps_h",
        "min_entry_basis_bps",
        "min_exec_basis_bps",
        "taker_fast_entry_basis_bps",
        "max_entry_basis_bps",
    ):
        val = getattr(args, attr, None)
        if val is not None:
            updates[attr] = Decimal(str(val))
    if getattr(args, "min_hold_h", None) is not None:
        updates["min_hold_h"] = int(args.min_hold_h)
    return replace(policy, **updates) if updates else policy


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Offline gzip tape replay & parameter sweep for Route A Carry")
    parser.add_argument("--tape-dir", type=Path, required=True, help="Directory with slow_snapshots_*.jsonl.gz and fast_bbo_*.jsonl.gz")
    parser.add_argument("--config", type=Path, default=REPO_ROOT / "config" / "carry-paper.toml", help="Baseline carry-*.toml config")
    parser.add_argument("--entry-carry-bps-h", type=str, default=None, help="Override policy.entry_carry_bps_h")
    parser.add_argument("--exit-carry-8h-bps-h", type=str, default=None, help="Override policy.exit_carry_8h_bps_h")
    parser.add_argument("--min-hold-h", type=int, default=None, help="Override policy.min_hold_h")
    parser.add_argument("--min-entry-basis-bps", type=str, default=None, help="Override policy.min_entry_basis_bps")
    parser.add_argument("--min-exec-basis-bps", type=str, default=None, help="Override policy.min_exec_basis_bps")
    parser.add_argument("--taker-fast-entry-basis-bps", type=str, default=None, help="Override policy.taker_fast_entry_basis_bps")
    parser.add_argument("--max-entry-basis-bps", type=str, default=None, help="Override policy.max_entry_basis_bps")
    parser.add_argument("--execution-mode", choices=("hybrid", "maker_ioc", "dual_taker"), default="hybrid")
    parser.add_argument("--json-out", type=Path, default=None, help="Optional output path for JSON replay summary")
    parser.add_argument("--json", action="store_true", help="Print JSON summary to stdout")
    args = parser.parse_args(argv)

    if not args.tape_dir.is_dir():
        print(f"[replay_carry_tape] tape directory not found: {args.tape_dir}", file=sys.stderr)
        return 2
    try:
        plan = load_carry_plan(args.config)
    except CarryConfigError as exc:
        print(f"[replay_carry_tape] ConfigError: {exc}", file=sys.stderr)
        return 2

    policy = _apply_policy_overrides(plan.policy, args)
    report = replay_tape(
        tape_dir=args.tape_dir,
        plan=plan,
        policy_override=policy,
        execution_mode=args.execution_mode,
    )

    if args.json_out is not None:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_format_replay_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

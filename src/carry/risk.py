"""Dual-layer risk engine: slow continuous basis stop (30m) + fast margin/unhedged guards."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Mapping

from .config import CarryRiskPolicy


@dataclass
class _BasisStreak:
    started_ts: int | None = None
    last_breach_ts: int | None = None


class BasisStopTracker:
    """Tracks continuous adverse basis duration per position to ignore brief wicks."""

    def __init__(self, *, basis_stop_bps: Decimal, persist_mins: int, max_gap_secs: int = 30) -> None:
        self.basis_stop_bps = float(basis_stop_bps)
        self.persist_secs = persist_mins * 60
        self.max_gap_secs = max_gap_secs
        self._streaks: dict[str, _BasisStreak] = {}

    def clear(self, position_id: str) -> None:
        self._streaks.pop(position_id, None)

    def observe(
        self,
        *,
        position_id: str,
        entry_basis_bps: float,
        basis_now_bps: float | None,
        now_s: int,
    ) -> tuple[bool, int]:
        """Return (triggered, elapsed_breach_secs). Resets immediately on basis recovery."""
        st = self._streaks.setdefault(position_id, _BasisStreak())
        if basis_now_bps is None:
            # Tolerate brief feed gaps without falsely firing OR discarding an active streak
            if st.started_ts is not None and st.last_breach_ts is not None and (now_s - st.last_breach_ts) > self.max_gap_secs:
                st.started_ts = None
                st.last_breach_ts = None
            return False, (now_s - st.started_ts) if st.started_ts is not None else 0

        adverse_bps = basis_now_bps - entry_basis_bps
        if adverse_bps >= self.basis_stop_bps:
            if st.started_ts is None:
                st.started_ts = now_s
            st.last_breach_ts = now_s
            elapsed = now_s - st.started_ts
            return elapsed >= self.persist_secs, elapsed

        st.started_ts = None
        st.last_breach_ts = None
        return False, 0


@dataclass(frozen=True)
class FastRiskDecision:
    close_positions: tuple[tuple[str, str], ...]  # (position_id, reason)
    halt_system: bool
    halt_reason: str | None


class FastRiskEvaluator:
    """1-second watchdog checking single-leg margin guard, unhedged delta timeout, and daily kill switch."""

    def __init__(self, policy: CarryRiskPolicy) -> None:
        self.policy = policy
        self._unhedged_since: dict[str, int] = {}

    def reset_unhedged(self, position_id: str) -> None:
        self._unhedged_since.pop(position_id, None)

    def evaluate(
        self,
        *,
        positions: Mapping[str, Mapping[str, object]],
        live_mids: Mapping[str, float],
        daily_realized_pnl_usd: Decimal,
        consecutive_unhedged: int,
        now_s: int,
    ) -> FastRiskDecision:
        """Evaluate fast hard-risk triggers across all open positions and account daily PnL."""
        if daily_realized_pnl_usd <= -self.policy.max_daily_loss_usd:
            close_all = tuple((pid, "daily_loss_kill") for pid in sorted(positions))
            return FastRiskDecision(close_positions=close_all, halt_system=True, halt_reason="daily_loss_kill")

        if consecutive_unhedged >= self.policy.max_consecutive_unhedged:
            close_all = tuple((pid, "consecutive_unhedged_kill") for pid in sorted(positions))
            return FastRiskDecision(close_positions=close_all, halt_system=True, halt_reason="consecutive_unhedged_kill")

        to_close: list[tuple[str, str]] = []
        halt = False
        halt_reason: str | None = None
        mg_ratio = float(self.policy.margin_guard_pct) / 100.0
        for stale_pid in list(self._unhedged_since):
            if stale_pid not in positions:
                self._unhedged_since.pop(stale_pid, None)

        for pid, pos in sorted(positions.items()):
            entry_l = float(pos["entry_l"])
            entry_s = float(pos["entry_s"])
            iid_l = str(pos["iid_long"])
            iid_s = str(pos["iid_short"])
            mid_l = live_mids.get(iid_l)
            mid_s = live_mids.get(iid_s)
            if mid_l and mid_s and entry_l > 0 and entry_s > 0:
                adv_l = (entry_l - mid_l) / entry_l
                adv_s = (mid_s - entry_s) / entry_s
                if max(adv_l, adv_s) >= mg_ratio:
                    to_close.append((pid, f"margin_guard_{int(self.policy.margin_guard_pct)}pct"))
                    continue

            # Unhedged delta check: USD exposure cap or significant percentage leg imbalance
            long_base = Decimal(str(pos.get("long_base_qty", "0")))
            short_base = Decimal(str(pos.get("short_base_qty", "0")))
            ref_px = Decimal(str(mid_l or mid_s or entry_l or entry_s or 0))
            diff_base = abs(long_base - short_base)
            unhedged_usd = diff_base * ref_px
            hi_base = max(long_base, short_base)
            qty_imbalanced = (
                hi_base > Decimal("0")
                and unhedged_usd > Decimal("0")
                and (diff_base / hi_base * Decimal("100")) > self.policy.max_qty_mismatch_pct
            )
            if unhedged_usd > self.policy.max_unhedged_usd or qty_imbalanced:
                since = self._unhedged_since.setdefault(pid, now_s)
                if now_s - since >= self.policy.max_unhedged_secs:
                    to_close.append((pid, "unhedged_timeout_kill"))
                    halt = True
                    halt_reason = "unhedged_timeout_kill"
            else:
                self._unhedged_since.pop(pid, None)

        return FastRiskDecision(close_positions=tuple(to_close), halt_system=halt, halt_reason=halt_reason)

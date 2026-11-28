"""Dual-layer risk engine: slow continuous basis stop (30m) + fast margin/unhedged guards."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable, Mapping

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
        sample_ts: int | None = None,
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
            if sample_ts is None:
                if st.started_ts is None:
                    st.started_ts = now_s
                st.last_breach_ts = now_s
                elapsed = now_s - st.started_ts
                return elapsed >= self.persist_secs, elapsed

            if st.started_ts is None:
                st.started_ts = sample_ts
                st.last_breach_ts = sample_ts
                return False, 0
            if st.last_breach_ts is not None and sample_ts <= st.last_breach_ts:
                if (now_s - st.last_breach_ts) > self.max_gap_secs:
                    st.started_ts = None
                    st.last_breach_ts = None
                    return False, 0
                return False, max(0, st.last_breach_ts - st.started_ts)
            if st.last_breach_ts is not None and (sample_ts - st.last_breach_ts) > self.max_gap_secs:
                st.started_ts = sample_ts
            st.last_breach_ts = sample_ts
            elapsed = st.last_breach_ts - st.started_ts
            return elapsed >= self.persist_secs, elapsed

        st.started_ts = None
        st.last_breach_ts = None
        return False, 0


@dataclass
class _InversionStreak:
    started_ts: int | None = None
    last_breach_ts: int | None = None
    healthy_exemptions_used: int = 0


class CarryInversionTracker:
    """Tracks persistent live-carry inversion per position with max 1 healthy-sample glitch exemption."""

    def __init__(
        self,
        *,
        inversion_stop_bps_h: Decimal,
        persist_mins: int,
        max_gap_secs: int = 180,
    ) -> None:
        self.inversion_stop_bps_h = float(inversion_stop_bps_h)
        self.inversion_stop_bps_h_dec = Decimal(str(inversion_stop_bps_h))
        self.persist_secs = int(persist_mins) * 60
        self.max_gap_secs = int(max_gap_secs)
        self._streaks: dict[str, _InversionStreak] = {}

    def clear(self, position_id: str) -> None:
        self._streaks.pop(position_id, None)

    def prune(self, active_position_ids: Iterable[str]) -> None:
        active = set(active_position_ids)
        for pid in list(self._streaks):
            if pid not in active:
                self._streaks.pop(pid, None)

    def observe(
        self,
        *,
        position_id: str,
        live_carry_bps_h: float | Decimal | None,
        now_s: int,
    ) -> tuple[bool, int]:
        """Return (triggered, elapsed_secs). Tolerates at most 1 isolated healthy sample per streak."""
        if self.inversion_stop_bps_h_dec <= Decimal("0") or self.persist_secs <= 0:
            return False, 0
        st = self._streaks.setdefault(position_id, _InversionStreak())
        if live_carry_bps_h is None:
            if (
                st.started_ts is not None
                and st.last_breach_ts is not None
                and (now_s - st.last_breach_ts) > self.max_gap_secs
            ):
                self._streaks[position_id] = _InversionStreak()
                return False, 0
            elapsed = (
                max(0, st.last_breach_ts - st.started_ts)
                if (st.started_ts is not None and st.last_breach_ts is not None)
                else 0
            )
            return False, elapsed

        live_c_dec = live_carry_bps_h if isinstance(live_carry_bps_h, Decimal) else Decimal(str(live_carry_bps_h))
        if live_c_dec <= -self.inversion_stop_bps_h_dec:
            if st.started_ts is None or st.last_breach_ts is None:
                st.started_ts = now_s
                st.last_breach_ts = now_s
                st.healthy_exemptions_used = 0
                return False, 0
            if now_s < st.last_breach_ts:
                return False, max(0, st.last_breach_ts - st.started_ts)
            if (now_s - st.last_breach_ts) > self.max_gap_secs:
                st.started_ts = now_s
                st.last_breach_ts = now_s
                st.healthy_exemptions_used = 0
                return False, 0
            st.last_breach_ts = now_s
            elapsed = st.last_breach_ts - st.started_ts
            return elapsed >= self.persist_secs, elapsed

        if (
            st.started_ts is not None
            and st.last_breach_ts is not None
            and (now_s - st.last_breach_ts) <= self.max_gap_secs
            and st.healthy_exemptions_used == 0
        ):
            st.healthy_exemptions_used = 1
            return False, max(0, st.last_breach_ts - st.started_ts)

        self._streaks[position_id] = _InversionStreak()
        return False, 0


@dataclass(frozen=True)
class FastRiskDecision:
    close_positions: tuple[tuple[str, str], ...]  # (position_id, reason)
    halt_system: bool
    halt_reason: str | None
    soft_close_positions: tuple[tuple[str, str], ...] = ()


def _fmt_pct_label(pct: Decimal) -> str:
    norm = pct.normalize()
    if norm == norm.to_integral_value():
        return str(int(norm))
    return format(norm, "f")


class FastRiskEvaluator:
    """1-second watchdog checking single-leg margin guard, unhedged delta timeout, and daily kill switch."""

    def __init__(self, policy: CarryRiskPolicy) -> None:
        self.policy = policy
        self._unhedged_since: dict[str, int] = {}

    def reset_unhedged(self, position_id: str) -> None:
        self._unhedged_since.pop(position_id, None)

    def evaluate_account_guards(
        self,
        *,
        daily_realized_pnl_usd: Decimal,
        consecutive_unhedged: int,
        position_ids: Iterable[str] = (),
    ) -> FastRiskDecision:
        """Evaluate account-level daily kill switches without altering per-position timers."""
        pids = sorted(position_ids)
        if daily_realized_pnl_usd <= -self.policy.max_daily_loss_usd:
            close_all = tuple((pid, "daily_loss_kill") for pid in pids)
            return FastRiskDecision(close_positions=close_all, halt_system=True, halt_reason="daily_loss_kill")

        if consecutive_unhedged >= self.policy.max_consecutive_unhedged:
            close_all = tuple((pid, "consecutive_unhedged_kill") for pid in pids)
            return FastRiskDecision(close_positions=close_all, halt_system=True, halt_reason="consecutive_unhedged_kill")

        return FastRiskDecision(close_positions=(), halt_system=False, halt_reason=None)

    def evaluate(
        self,
        *,
        positions: Mapping[str, Mapping[str, object]],
        live_mids: Mapping[str, float],
        daily_realized_pnl_usd: Decimal,
        consecutive_unhedged: int,
        now_s: int,
        live_quotes: Mapping[str, tuple[float, float]] | None = None,
    ) -> FastRiskDecision:
        """Evaluate fast hard-risk triggers across all open positions and account daily PnL."""
        account_dec = self.evaluate_account_guards(
            daily_realized_pnl_usd=daily_realized_pnl_usd,
            consecutive_unhedged=consecutive_unhedged,
            position_ids=positions.keys(),
        )
        if account_dec.halt_system:
            return account_dec

        to_close: list[tuple[str, str]] = []
        to_soft_close: list[tuple[str, str]] = []
        halt = False
        halt_reason: str | None = None
        mg_pct = self.policy.margin_guard_pct
        soft_mg_pct = self.policy.soft_margin_guard_pct
        for stale_pid in list(self._unhedged_since):
            if stale_pid not in positions:
                self._unhedged_since.pop(stale_pid, None)

        for pid, pos in sorted(positions.items()):
            entry_l_dec = Decimal(str(pos["entry_l"]))
            entry_s_dec = Decimal(str(pos["entry_s"]))
            iid_l = str(pos["iid_long"])
            iid_s = str(pos["iid_short"])
            mid_l = live_mids.get(iid_l)
            mid_s = live_mids.get(iid_s)
            q_l = live_quotes.get(iid_l) if live_quotes is not None else None
            q_s = live_quotes.get(iid_s) if live_quotes is not None else None
            if q_l is not None and q_l[0] > 0 and q_l[1] >= q_l[0]:
                mid_l_dec = (Decimal(str(q_l[0])) + Decimal(str(q_l[1]))) * Decimal("0.5")
            elif mid_l is not None and mid_l > 0:
                mid_l_dec = Decimal(str(mid_l))
            else:
                mid_l_dec = None
            if q_s is not None and q_s[0] > 0 and q_s[1] >= q_s[0]:
                mid_s_dec = (Decimal(str(q_s[0])) + Decimal(str(q_s[1]))) * Decimal("0.5")
            elif mid_s is not None and mid_s > 0:
                mid_s_dec = Decimal(str(mid_s))
            else:
                mid_s_dec = None
            long_breach = (
                mid_l_dec is not None
                and entry_l_dec > Decimal("0")
                and ((entry_l_dec - mid_l_dec) * Decimal("100")) >= (mg_pct * entry_l_dec)
            )
            short_breach = (
                mid_s_dec is not None
                and entry_s_dec > Decimal("0")
                and ((mid_s_dec - entry_s_dec) * Decimal("100")) >= (mg_pct * entry_s_dec)
            )
            soft_long_breach = (
                soft_mg_pct > Decimal("0")
                and mid_l_dec is not None
                and entry_l_dec > Decimal("0")
                and ((entry_l_dec - mid_l_dec) * Decimal("100")) >= (soft_mg_pct * entry_l_dec)
            )
            soft_short_breach = (
                soft_mg_pct > Decimal("0")
                and mid_s_dec is not None
                and entry_s_dec > Decimal("0")
                and ((mid_s_dec - entry_s_dec) * Decimal("100")) >= (soft_mg_pct * entry_s_dec)
            )

            # Unhedged delta check runs independently so margin_guard never masks halt timers
            long_base = Decimal(str(pos.get("long_base_qty", "0")))
            short_base = Decimal(str(pos.get("short_base_qty", "0")))
            ref_px = (
                mid_l_dec
                if mid_l_dec is not None
                else (mid_s_dec if mid_s_dec is not None else (entry_l_dec if entry_l_dec > 0 else entry_s_dec))
            )
            diff_base = abs(long_base - short_base)
            unhedged_usd = diff_base * ref_px
            hi_base = max(long_base, short_base)
            qty_imbalanced = (
                hi_base > Decimal("0")
                and unhedged_usd > Decimal("0")
                and (diff_base / hi_base * Decimal("100")) > self.policy.max_qty_mismatch_pct
            )
            unhedged_killed = False
            if unhedged_usd > self.policy.max_unhedged_usd or qty_imbalanced:
                since = self._unhedged_since.setdefault(pid, now_s)
                if now_s - since >= self.policy.max_unhedged_secs:
                    unhedged_killed = True
            else:
                self._unhedged_since.pop(pid, None)

            if unhedged_killed:
                to_close.append((pid, "unhedged_timeout_kill"))
                halt = True
                halt_reason = "unhedged_timeout_kill"
            elif long_breach or short_breach:
                to_close.append((pid, f"margin_guard_{_fmt_pct_label(mg_pct)}pct"))
            elif soft_long_breach or soft_short_breach:
                to_soft_close.append((pid, f"soft_margin_guard_{_fmt_pct_label(soft_mg_pct)}pct"))

        return FastRiskDecision(
            close_positions=tuple(to_close),
            halt_system=halt,
            halt_reason=halt_reason,
            soft_close_positions=tuple(to_soft_close),
        )

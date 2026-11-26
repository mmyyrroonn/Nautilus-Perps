"""Signal evaluation, OKX ctVal lot alignment, 8h settlement gates, and dynamic subscriptions."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Mapping, Sequence

from .config import CEX_VENUES, CarryPolicy, CarryRiskPolicy, DEX_VENUES
from .funding_feed import LegMarketInfo


@dataclass(frozen=True)
class InstrumentSpec:
    instrument_id: str
    venue: str
    size_increment: Decimal
    contract_multiplier: Decimal = Decimal("1")
    min_quantity: Decimal = Decimal("0")
    min_notional_usd: Decimal = Decimal("0")
    price_increment: Decimal = Decimal("0.0001")

    @property
    def base_step(self) -> Decimal:
        """Minimum base-asset quantity step (accounts for OKX ctVal contract multiplier)."""
        return self.size_increment * self.contract_multiplier

    def round_price(self, price: Decimal) -> Decimal:
        steps = (price / self.price_increment).to_integral_value(rounding=ROUND_DOWN)
        return steps * self.price_increment


@dataclass(frozen=True)
class AlignedOrderSize:
    long_qty_contracts: Decimal
    short_qty_contracts: Decimal
    long_base_qty: Decimal
    short_base_qty: Decimal
    mismatch_pct: Decimal
    notional_usd: Decimal


def align_leg_quantities(
    spec_long: InstrumentSpec,
    spec_short: InstrumentSpec,
    *,
    target_usd: Decimal,
    mid_price: Decimal,
    max_mismatch_pct: Decimal = Decimal("2.0"),
) -> tuple[AlignedOrderSize | None, str | None]:
    """Align base quantities across two venues with differing size increments and OKX ctVal multipliers."""
    if mid_price <= 0 or target_usd <= 0:
        return None, "invalid_price_or_target"

    step_l = spec_long.base_step
    step_s = spec_short.base_step
    if step_l <= 0 or step_s <= 0:
        return None, "invalid_base_step"

    min_l_usd = max(spec_long.min_notional_usd, spec_long.min_quantity * spec_long.contract_multiplier * mid_price, step_l * mid_price)
    min_s_usd = max(spec_short.min_notional_usd, spec_short.min_quantity * spec_short.contract_multiplier * mid_price, step_s * mid_price)
    if min_l_usd > target_usd or min_s_usd > target_usd:
        return None, "min_lot_exceeds_target_usd"

    raw_base = target_usd / mid_price
    coarse_step = max(step_l, step_s)
    k = int((raw_base / coarse_step).to_integral_value(rounding=ROUND_DOWN))
    if k < 1:
        return None, "target_below_coarse_step"

    best: AlignedOrderSize | None = None
    # Search downward from k for an exact or near-exact base quantity match within tolerance
    for cand_k in range(k, max(0, k - 25), -1):
        target_base = Decimal(cand_k) * coarse_step
        n_l = (target_base / step_l).to_integral_value(rounding=ROUND_DOWN)
        n_s = (target_base / step_s).to_integral_value(rounding=ROUND_DOWN)
        if n_l <= 0 or n_s <= 0:
            continue
        qty_l_contracts = n_l * spec_long.size_increment
        qty_s_contracts = n_s * spec_short.size_increment
        if qty_l_contracts < spec_long.min_quantity or qty_s_contracts < spec_short.min_quantity:
            continue
        base_l = n_l * step_l
        base_s = n_s * step_s
        notional = min(base_l, base_s) * mid_price
        if notional < spec_long.min_notional_usd or notional < spec_short.min_notional_usd:
            continue
        max_b = max(base_l, base_s)
        mismatch_pct = (abs(base_l - base_s) / max_b) * Decimal("100")
        if mismatch_pct <= max_mismatch_pct:
            best = AlignedOrderSize(
                long_qty_contracts=qty_l_contracts,
                short_qty_contracts=qty_s_contracts,
                long_base_qty=base_l,
                short_base_qty=base_s,
                mismatch_pct=mismatch_pct,
                notional_usd=notional,
            )
            break

    if best is None:
        return None, "alignment_mismatch_exceeds_tolerance"
    return best, None


@dataclass(frozen=True)
class EntryCandidate:
    symbol: str
    long_venue: str
    short_venue: str
    long_leg: LegMarketInfo
    short_leg: LegMarketInfo
    route_cat: str
    carry_8h_bps_h: float
    carry_24h_bps_h: float
    basis_bps: float
    rt_cost_bps: float
    net_score_bps_h: float


def _route_cat(v1: str, v2: str) -> str:
    d1, d2 = v1 in DEX_VENUES, v2 in DEX_VENUES
    if d1 and d2:
        return "DEX-DEX"
    if not d1 and not d2:
        return "CEX-CEX"
    return "DEX-CEX"


def _route_allowed(cat: str, route_scope: str) -> bool:
    if route_scope == "ALL_4V":
        return True
    if route_scope == "DEX_CEX_ONLY":
        return cat == "DEX-CEX"
    if route_scope == "DEX_ONLY":
        return cat == "DEX-DEX"
    if route_scope == "CEX_ONLY":
        return cat == "CEX-CEX"
    return False


def _passes_split_and_market_sanity(leg_l: LegMarketInfo, leg_s: LegMarketInfo, policy: CarryPolicy) -> tuple[bool, str | None]:
    m_l, m_s = leg_l.mid, leg_s.mid
    if m_l <= 0 or m_s <= 0:
        return False, "invalid_mid"
    ratio = m_s / m_l
    dev = float(policy.max_price_ratio_deviation)
    if not (1.0 - dev <= ratio <= 1.0 + dev):
        return False, "price_ratio_mismatch"
    sp_l, sp_s = leg_l.spread_bps, leg_s.spread_bps
    max_sp = float(policy.max_leg_spread_bps)
    if sp_l is None or sp_s is None or sp_l > max_sp or sp_s > max_sp:
        return False, "spread_too_wide"
    if leg_l.vol24_usd < float(policy.min_24h_vol_usd) or leg_s.vol24_usd < float(policy.min_24h_vol_usd):
        return False, "low_24h_vol"
    if leg_l.vol1h_usd is not None and leg_l.vol1h_usd <= 0:
        return False, "zero_1h_vol_long"
    if leg_s.vol1h_usd is not None and leg_s.vol1h_usd <= 0:
        return False, "zero_1h_vol_short"
    # Detect staggered split/corporate action where one venue jumped >15% in 1h while the other didn't
    if leg_l.prev_1h_price and leg_s.prev_1h_price and leg_l.prev_1h_price > 0 and leg_s.prev_1h_price > 0:
        r_prev = leg_s.prev_1h_price / leg_l.prev_1h_price
        if abs(ratio / r_prev - 1.0) > float(policy.max_1h_split_divergence):
            return False, "split_1h_divergence"
    return True, None


def _cex_entry_blackout_hit(leg_l: LegMarketInfo, leg_s: LegMarketInfo, policy: CarryPolicy, now_s: int) -> bool:
    """Avoid entering right before an 8h CEX settlement if that CEX leg is paying funding."""
    win_s = policy.cex_entry_blackout_before_settle_mins * 60
    if win_s <= 0:
        return False
    for leg, is_long in ((leg_l, True), (leg_s, False)):
        if leg.venue not in CEX_VENUES or leg.interval_h < 4 or leg.next_settle_ts <= now_s:
            continue
        if leg.next_settle_ts - now_s <= win_s:
            rate = leg.current_rate_bps_h if leg.current_rate_bps_h is not None else (leg.r8_bps_h or 0.0)
            # Positive rate -> longs pay shorts
            leg_paying = (rate > 0) if is_long else (rate < 0)
            if leg_paying:
                return True
    return False


def evaluate_entry_candidates(
    snapshot: Mapping[str, Mapping[str, LegMarketInfo]],
    *,
    open_symbols: set[str],
    venue_notional_used: Mapping[str, Decimal],
    excluded_symbols: set[str],
    policy: CarryPolicy,
    risk: CarryRiskPolicy,
    now_s: int,
    respect_capacity: bool = True,
) -> list[EntryCandidate]:
    """Evaluate all ordered multi-venue pairs deterministically and rank by net carry score."""
    if respect_capacity:
        if len(open_symbols) >= risk.max_slots:
            return []
        gross_used = sum(venue_notional_used.values(), Decimal("0"))
        if gross_used + Decimal("2") * risk.leg_notional_usd > risk.max_gross_notional_usd:
            return []

    candidates: list[EntryCandidate] = []
    allowed_ac = set(policy.asset_classes)
    entry_thresh = float(policy.entry_carry_bps_h)
    min_eb = float(policy.min_entry_basis_bps)
    max_eb = float(policy.max_entry_basis_bps)

    for sym, vmap in sorted(snapshot.items()):
        if sym in open_symbols or sym in excluded_symbols:
            continue
        vs = sorted(vmap.keys())
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                v1, v2 = vs[i], vs[j]
                for lv, sv in ((v1, v2), (v2, v1)):
                    leg_l, leg_s = vmap[lv], vmap[sv]
                    if leg_l.asset_class not in allowed_ac or leg_s.asset_class not in allowed_ac:
                        continue
                    cat = _route_cat(lv, sv)
                    if not _route_allowed(cat, policy.route_scope):
                        continue
                    if respect_capacity:
                        if venue_notional_used.get(lv, Decimal("0")) + risk.leg_notional_usd > risk.max_venue_notional_usd:
                            continue
                        if venue_notional_used.get(sv, Decimal("0")) + risk.leg_notional_usd > risk.max_venue_notional_usd:
                            continue
                    ok, _ = _passes_split_and_market_sanity(leg_l, leg_s, policy)
                    if not ok:
                        continue
                    if leg_l.r8_bps_h is None or leg_s.r8_bps_h is None or leg_l.r24_bps_h is None or leg_s.r24_bps_h is None:
                        continue
                    c8 = leg_s.r8_bps_h - leg_l.r8_bps_h
                    c24 = leg_s.r24_bps_h - leg_l.r24_bps_h
                    if c8 < entry_thresh or c24 < entry_thresh:
                        continue
                    if leg_l.current_rate_bps_h is not None and leg_s.current_rate_bps_h is not None:
                        c_live = leg_s.current_rate_bps_h - leg_l.current_rate_bps_h
                        if c_live < float(policy.exit_carry_8h_bps_h):
                            continue
                    mid = 0.5 * (leg_l.mid + leg_s.mid)
                    basis = (leg_s.mid - leg_l.mid) / mid * 1e4
                    if not (min_eb <= basis <= max_eb):
                        continue
                    if _cex_entry_blackout_hit(leg_l, leg_s, policy, now_s):
                        continue
                    rt_cost = float(policy.fee_pair_rt_bps(lv, sv))
                    score = min(c8, c24) - rt_cost / 24.0
                    if score <= 0:
                        continue
                    candidates.append(
                        EntryCandidate(
                            symbol=sym,
                            long_venue=lv,
                            short_venue=sv,
                            long_leg=leg_l,
                            short_leg=leg_s,
                            route_cat=cat,
                            carry_8h_bps_h=c8,
                            carry_24h_bps_h=c24,
                            basis_bps=basis,
                            rt_cost_bps=rt_cost,
                            net_score_bps_h=score,
                        )
                    )

    candidates.sort(key=lambda c: (-c.net_score_bps_h, c.symbol, c.long_venue, c.short_venue))
    return candidates


def evaluate_watch_candidates(
    snapshot: Mapping[str, Mapping[str, LegMarketInfo]],
    *,
    strict_candidates: Sequence[EntryCandidate],
    open_symbols: set[str],
    excluded_symbols: set[str],
    policy: CarryPolicy,
    top_k: int,
    min_watch_carry_bps_h: float = 0.35,
) -> list[EntryCandidate]:
    """Expand watchlist with relaxed near-miss symbols for telemetry without affecting strict entry."""
    out = list(strict_candidates)
    strict_syms = {c.symbol for c in strict_candidates}
    if len(strict_syms) >= top_k or min_watch_carry_bps_h <= 0:
        return out

    covered = strict_syms | set(open_symbols)
    allowed_ac = set(policy.asset_classes)
    max_eb = float(policy.max_entry_basis_bps)
    extras: list[tuple[float, float, EntryCandidate]] = []

    for sym, vmap in sorted(snapshot.items()):
        if sym in covered or sym in excluded_symbols:
            continue
        vs = sorted(vmap.keys())
        best_rank: tuple[float, float] | None = None
        best_cand: EntryCandidate | None = None
        for i in range(len(vs)):
            for j in range(i + 1, len(vs)):
                v1, v2 = vs[i], vs[j]
                for lv, sv in ((v1, v2), (v2, v1)):
                    leg_l, leg_s = vmap[lv], vmap[sv]
                    if leg_l.asset_class not in allowed_ac or leg_s.asset_class not in allowed_ac:
                        continue
                    cat = _route_cat(lv, sv)
                    if not _route_allowed(cat, policy.route_scope):
                        continue
                    ok, _ = _passes_split_and_market_sanity(leg_l, leg_s, policy)
                    if not ok:
                        continue
                    if (
                        leg_l.r8_bps_h is None
                        or leg_s.r8_bps_h is None
                        or leg_l.r24_bps_h is None
                        or leg_s.r24_bps_h is None
                    ):
                        continue
                    c8 = leg_s.r8_bps_h - leg_l.r8_bps_h
                    c24 = leg_s.r24_bps_h - leg_l.r24_bps_h
                    hi = max(c8, c24)
                    lo = min(c8, c24)
                    if hi < min_watch_carry_bps_h:
                        continue
                    mid = 0.5 * (leg_l.mid + leg_s.mid)
                    basis = (leg_s.mid - leg_l.mid) / mid * 1e4
                    if abs(basis) > max_eb:
                        continue
                    rank = (hi, lo)
                    if best_rank is None or rank > best_rank:
                        rt_cost = float(policy.fee_pair_rt_bps(lv, sv))
                        best_rank = rank
                        best_cand = EntryCandidate(
                            symbol=sym,
                            long_venue=lv,
                            short_venue=sv,
                            long_leg=leg_l,
                            short_leg=leg_s,
                            route_cat=cat,
                            carry_8h_bps_h=c8,
                            carry_24h_bps_h=c24,
                            basis_bps=basis,
                            rt_cost_bps=rt_cost,
                            net_score_bps_h=lo - rt_cost / 24.0,
                        )
        if best_rank is not None and best_cand is not None:
            extras.append((best_rank[0], best_rank[1], best_cand))

    extras.sort(key=lambda item: (-item[0], -item[1], item[2].symbol, item[2].long_venue, item[2].short_venue))
    needed = max(0, top_k - len(strict_syms))
    out.extend(item[2] for item in extras[:needed])
    return out


def should_exit_carry_decay(
    *,
    opened_ts: int,
    leg_l: LegMarketInfo,
    leg_s: LegMarketInfo,
    policy: CarryPolicy,
    now_s: int,
) -> tuple[bool, str]:
    """Check normal carry-decay exit, honoring min_hold_h and CEX settlement collection deferral."""
    if now_s - opened_ts < policy.min_hold_h * 3600:
        return False, "min_hold_active"
    if leg_l.r8_bps_h is None or leg_s.r8_bps_h is None:
        return False, "missing_r8"
    c8 = leg_s.r8_bps_h - leg_l.r8_bps_h
    if c8 >= float(policy.exit_carry_8h_bps_h):
        return False, "carry_healthy"

    # Defer normal carry_decay exit if a 4h/8h CEX leg is due to RECEIVE funding within 20 minutes
    defer_s = policy.cex_exit_defer_before_settle_mins * 60
    if defer_s > 0:
        for leg, is_long in ((leg_l, True), (leg_s, False)):
            if leg.venue not in CEX_VENUES or leg.interval_h < 4 or leg.next_settle_ts <= now_s:
                continue
            if leg.next_settle_ts - now_s <= defer_s:
                rate = leg.current_rate_bps_h if leg.current_rate_bps_h is not None else (leg.r8_bps_h or 0.0)
                leg_receiving = (rate < 0) if is_long else (rate > 0)
                if leg_receiving:
                    return False, "defer_collect_cex_settlement"
    return True, "carry_decay"


class SubscriptionManager:
    """Dynamically maintains Fast-Layer BBO subscriptions with hysteresis to keep memory <300MB."""

    def __init__(self, *, top_k: int = 15, unsubscribe_hysteresis_secs: int = 600) -> None:
        self.top_k = top_k
        self.hysteresis_secs = unsubscribe_hysteresis_secs
        self._subscribed: set[str] = set()
        self._last_wanted_ts: dict[str, int] = {}

    @property
    def active_instruments(self) -> frozenset[str]:
        return frozenset(self._subscribed)

    def reconcile(
        self,
        *,
        open_instrument_ids: set[str],
        ranked_candidates: list[EntryCandidate],
        now_s: int,
    ) -> tuple[list[str], list[str]]:
        """Return (to_subscribe, to_unsubscribe) while pinning open positions immediately."""
        wanted: set[str] = set(open_instrument_ids)
        seen_syms: set[str] = set()
        for c in ranked_candidates:
            wanted.add(c.long_leg.instrument_id)
            wanted.add(c.short_leg.instrument_id)
            seen_syms.add(c.symbol)
            if len(seen_syms) >= self.top_k:
                break

        for iid in wanted:
            self._last_wanted_ts[iid] = now_s

        to_sub = sorted(wanted - self._subscribed)
        to_unsub: list[str] = []
        for iid in sorted(self._subscribed - wanted):
            last_t = self._last_wanted_ts.get(iid, 0)
            if now_s - last_t >= self.hysteresis_secs:
                to_unsub.append(iid)

        self._subscribed.update(to_sub)
        self._subscribed.difference_update(to_unsub)
        return to_sub, to_unsub

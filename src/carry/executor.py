"""Sliced Maker-First + IOC-Hedge execution controller and PaperVenueGateway."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, ROUND_DOWN
import time
from typing import Mapping, Protocol
import uuid

from .config import CarryPolicy, CarryRiskPolicy, CarryRuntimePolicy, DEX_VENUES
from .ledger import CarryLedger
from .signal import EntryCandidate, InstrumentSpec, align_leg_quantities


@dataclass(frozen=True)
class OrderAck:
    client_order_id: str
    venue_order_id: str
    status: str  # "OPEN", "FILLED", "PARTIAL", "CANCELED", "REJECTED"
    filled_base_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    reason: str | None = None
    fee_usd: Decimal | None = None


class VenueGateway(Protocol):
    def submit_limit(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        price: Decimal,
        post_only: bool,
        ioc: bool,
        reduce_only: bool,
    ) -> OrderAck: ...

    def submit_market(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        reduce_only: bool,
    ) -> OrderAck: ...

    def cancel_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck: ...

    def poll_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck: ...


class PaperVenueGateway:
    """Deterministic in-memory simulator for paper mode and behavioral testing."""

    def __init__(
        self,
        *,
        fill_maker_immediately: bool = False,
        maker_partial_ratio: Decimal = Decimal("1"),
        use_realistic_bbo: bool = False,
    ) -> None:
        self.fill_maker_immediately = fill_maker_immediately
        self.maker_partial_ratio = maker_partial_ratio
        self.use_realistic_bbo = use_realistic_bbo
        self.orders: dict[str, dict] = {}
        self.specs: dict[str, InstrumentSpec] = {}
        self.mids: dict[str, Decimal] = {}
        self.quotes: dict[str, tuple[Decimal, Decimal]] = {}

    def register_spec(self, spec: InstrumentSpec, mid: Decimal | None = None) -> None:
        self.specs[spec.instrument_id] = spec
        if mid is not None:
            self.mids[spec.instrument_id] = mid

    def seed_quote(self, instrument_id: str, bid: Decimal, ask: Decimal) -> None:
        if instrument_id not in self.quotes and bid > 0 and ask >= bid:
            self.quotes[instrument_id] = (bid, ask)
            self.mids.setdefault(instrument_id, Decimal("0.5") * (bid + ask))

    def update_quote(self, instrument_id: str, bid: Decimal, ask: Decimal) -> list[str]:
        if bid <= 0 or ask < bid:
            return []
        self.quotes[instrument_id] = (bid, ask)
        self.mids[instrument_id] = Decimal("0.5") * (bid + ask)
        if not self.use_realistic_bbo:
            return []
        filled_coids: list[str] = []
        for coid, rec in self.orders.items():
            if (
                rec["instrument_id"] != instrument_id
                or not rec.get("post_only")
                or rec["status"] not in ("OPEN", "PARTIAL")
                or rec["price"] is None
            ):
                continue
            px = rec["price"]
            crossed = (ask <= px or bid < px) if rec.get("side") == "BUY" else (bid >= px or ask > px)
            if crossed:
                rec["filled_base_qty"] = rec["base_qty"]
                rec["status"] = "FILLED"
                rec["avg_fill_price"] = px
                filled_coids.append(coid)
        return filled_coids

    def _base_qty(self, iid: str, qty_contracts: Decimal) -> Decimal:
        spec = self.specs.get(iid)
        mult = spec.contract_multiplier if spec is not None else Decimal("1")
        return qty_contracts * mult

    def trigger_maker_fill(self, client_order_id: str, *, base_qty: Decimal | None = None, price: Decimal | None = None) -> OrderAck:
        rec = self.orders[client_order_id]
        filled = rec["base_qty"] if base_qty is None else min(rec["base_qty"], base_qty)
        rec["filled_base_qty"] = filled
        rec["status"] = "FILLED" if filled >= rec["base_qty"] else "PARTIAL"
        px = price or rec["price"] or self.mids.get(rec["instrument_id"], Decimal("1"))
        rec["avg_fill_price"] = px
        return OrderAck(client_order_id, rec["venue_order_id"], rec["status"], filled, px)

    def submit_limit(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        price: Decimal,
        post_only: bool,
        ioc: bool,
        reduce_only: bool,
    ) -> OrderAck:
        base_qty = self._base_qty(instrument_id, qty_contracts)
        vid = f"PAPER-{uuid.uuid4().hex[:10]}"
        if ioc:
            if self.use_realistic_bbo:
                mid_fb = self.mids.get(instrument_id, price)
                bid, ask = self.quotes.get(instrument_id, (mid_fb, mid_fb))
                can_fill = (price >= ask and ask > 0) if side == "BUY" else (price <= bid and bid > 0)
                if can_fill:
                    fill_px = ask if side == "BUY" else bid
                    self.orders[client_order_id] = {
                        "venue_order_id": vid, "instrument_id": instrument_id, "side": side, "post_only": False,
                        "base_qty": base_qty, "filled_base_qty": base_qty, "price": price,
                        "avg_fill_price": fill_px, "status": "FILLED",
                    }
                    return OrderAck(client_order_id, vid, "FILLED", base_qty, fill_px)
                self.orders[client_order_id] = {
                    "venue_order_id": vid, "instrument_id": instrument_id, "side": side, "post_only": False,
                    "base_qty": base_qty, "filled_base_qty": Decimal("0"), "price": price,
                    "avg_fill_price": None, "status": "CANCELED",
                }
                return OrderAck(client_order_id, vid, "CANCELED", Decimal("0"), None, reason="ioc_unfilled")
            self.orders[client_order_id] = {
                "venue_order_id": vid, "instrument_id": instrument_id, "side": side, "post_only": False,
                "base_qty": base_qty, "filled_base_qty": base_qty, "price": price,
                "avg_fill_price": price, "status": "FILLED",
            }
            return OrderAck(client_order_id, vid, "FILLED", base_qty, price)

        if post_only and self.fill_maker_immediately:
            filled = base_qty * self.maker_partial_ratio
            status = "FILLED" if filled >= base_qty else "PARTIAL"
            self.orders[client_order_id] = {
                "venue_order_id": vid, "instrument_id": instrument_id, "side": side, "post_only": post_only,
                "base_qty": base_qty, "filled_base_qty": filled, "price": price,
                "avg_fill_price": price, "status": status,
            }
            return OrderAck(client_order_id, vid, status, filled, price)

        self.orders[client_order_id] = {
            "venue_order_id": vid, "instrument_id": instrument_id, "side": side, "post_only": post_only,
            "base_qty": base_qty, "filled_base_qty": Decimal("0"), "price": price,
            "avg_fill_price": None, "status": "OPEN",
        }
        return OrderAck(client_order_id, vid, "OPEN", Decimal("0"), None)

    def submit_market(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        reduce_only: bool,
    ) -> OrderAck:
        base_qty = self._base_qty(instrument_id, qty_contracts)
        vid = f"PAPER-{uuid.uuid4().hex[:10]}"
        if self.use_realistic_bbo and instrument_id in self.quotes:
            bid, ask = self.quotes[instrument_id]
            px = ask if side == "BUY" else bid
        else:
            px = self.mids.get(instrument_id, Decimal("1"))
        self.orders[client_order_id] = {
            "venue_order_id": vid, "instrument_id": instrument_id, "side": side, "post_only": False,
            "base_qty": base_qty, "filled_base_qty": base_qty, "price": px,
            "avg_fill_price": px, "status": "FILLED",
        }
        return OrderAck(client_order_id, vid, "FILLED", base_qty, px)

    def cancel_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        rec = self.orders[client_order_id]
        if rec["status"] in ("OPEN", "PARTIAL"):
            rec["status"] = "CANCELED"
        return OrderAck(client_order_id, rec["venue_order_id"], rec["status"], rec["filled_base_qty"])

    def poll_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        rec = self.orders[client_order_id]
        return OrderAck(client_order_id, rec["venue_order_id"], rec["status"], rec["filled_base_qty"], rec.get("avg_fill_price", rec.get("price")))


TERMINAL_ORDER_STATUSES = frozenset({"FILLED", "CANCELED", "REJECTED"})
_EMERGENCY_CLOSE_RETRY_BACKOFF_SECS = (2, 5, 10)
_OPENING_SLICE_GATE_ERRORS = frozenset(
    {
        "exec_basis_below_min",
        "exec_basis_above_max",
        "entry_basis_below_min",
        "entry_basis_above_max",
        "spread_too_wide",
        "price_ratio_mismatch",
        "dual_taker_ioc_unfilled",
    }
)
_OPENING_UNFILLABLE_DUST_ERRORS = frozenset(
    {
        "min_lot_exceeds_target_usd",
        "target_below_coarse_step",
        "alignment_mismatch_exceeds_tolerance",
    }
)


def make_client_order_id(prefix: str, position_id: str, *, rand_len: int = 8) -> str:
    """Generate an OKX-compatible ASCII-alphanumeric client_order_id in 1..=32 chars."""
    clean_prefix = "".join(ch for ch in prefix if ch.isascii() and ch.isalnum()) or "ord"
    clean_pos = "".join(ch for ch in position_id if ch.isascii() and ch.isalnum())
    rand = uuid.uuid4().hex[: max(6, min(rand_len, 16))]
    if len(clean_prefix) + len(rand) > 32:
        clean_prefix = clean_prefix[: max(1, 32 - len(rand))]
        max_pos_len = 0
    else:
        max_pos_len = min(12, 32 - len(clean_prefix) - len(rand))
    return f"{clean_prefix}{clean_pos[:max_pos_len]}{rand}"[:32]


@dataclass
class ActiveMakerLeg:
    client_order_id: str
    position_id: str
    is_opening: bool
    maker_side_is_long: bool
    maker_venue: str
    hedge_venue: str
    spec_maker: InstrumentSpec
    spec_hedge: InstrumentSpec
    submitted_ts: int
    attempts: int
    target_base_qty: Decimal = Decimal("0")
    accounted_maker_base_qty: Decimal = Decimal("0")
    settled_hedged_base_qty: Decimal = Decimal("0")
    cancel_requested: bool = False


@dataclass
class ActiveDualTakerLeg:
    client_order_id: str
    is_long: bool
    venue: str
    spec: InstrumentSpec
    side: str
    target_base_qty: Decimal
    ref_price: Decimal
    status: str = "OPEN"
    filled_base_qty: Decimal = Decimal("0")
    accounted_base_qty: Decimal = Decimal("0")
    fallback_coid: str | None = None
    fallback_status: str | None = None
    fallback_target_base_qty: Decimal = Decimal("0")
    fallback_filled_base_qty: Decimal = Decimal("0")
    fallback_accounted_base_qty: Decimal = Decimal("0")


@dataclass
class ActiveDualTakerBatch:
    position_id: str
    long_leg: ActiveDualTakerLeg | None
    short_leg: ActiveDualTakerLeg | None
    submitted_ts: int
    is_opening: bool = True
    fallback_submitted: bool = False

    def active_legs(self) -> tuple[ActiveDualTakerLeg, ...]:
        return tuple(leg for leg in (self.long_leg, self.short_leg) if leg is not None)


@dataclass
class ActiveHedgeOrder:
    client_order_id: str
    position_id: str
    is_opening: bool
    hedge_is_long: bool
    venue: str
    spec: InstrumentSpec
    side: str
    target_base_qty: Decimal
    ref_price: Decimal
    submitted_ts: int
    status: str = "OPEN"
    filled_base_qty: Decimal = Decimal("0")
    accounted_base_qty: Decimal = Decimal("0")
    fallback_coid: str | None = None
    fallback_status: str | None = None
    fallback_target_base_qty: Decimal = Decimal("0")
    fallback_filled_base_qty: Decimal = Decimal("0")
    fallback_accounted_base_qty: Decimal = Decimal("0")
    maker_coid: str | None = None


@dataclass
class ActiveCloseLeg:
    client_order_id: str
    is_long: bool
    venue: str
    spec: InstrumentSpec
    ref_price: Decimal | None
    target_base_qty: Decimal = Decimal("0")
    status: str = "OPEN"
    filled_base_qty: Decimal = Decimal("0")
    accounted_base_qty: Decimal = Decimal("0")


@dataclass
class ActiveEmergencyClose:
    position_id: str
    reason: str
    legs: list[ActiveCloseLeg]
    submitted_ts: int
    spec_l: InstrumentSpec | None = None
    spec_s: InstrumentSpec | None = None
    long_venue: str = ""
    short_venue: str = ""
    ref_price_l: Decimal | None = None
    ref_price_s: Decimal | None = None
    long_failures: int = 0
    short_failures: int = 0
    next_retry_long_ts: int = 0
    next_retry_short_ts: int = 0
    manual_takeover: bool = False
    _counted_failed_coids: set[str] = field(default_factory=set)


class CarryExecutionController:
    """Executes sliced Maker-First + IOC-Hedge entries/exits backed by CarryLedger."""

    def __init__(
        self,
        *,
        ledger: CarryLedger,
        gateway: VenueGateway,
        risk: CarryRiskPolicy,
        runtime: CarryRuntimePolicy,
        policy: CarryPolicy | None = None,
    ) -> None:
        self.ledger = ledger
        self.gateway = gateway
        self.risk = risk
        self.runtime = runtime
        self.policy = policy or CarryPolicy()
        self._active_makers: dict[str, ActiveMakerLeg] = {}
        self._active_dual_takers: dict[str, ActiveDualTakerBatch] = {}
        self._active_hedges: dict[str, ActiveHedgeOrder] = {}
        self._active_emergency_closes: dict[str, ActiveEmergencyClose] = {}
        self._slice_attempts: dict[str, int] = {}
        self._stopping: bool = False

    @property
    def is_stopping(self) -> bool:
        return self._stopping

    @staticmethod
    def _exec_basis_bps(long_px: Decimal, short_px: Decimal) -> Decimal:
        if long_px <= 0 or short_px <= 0:
            return Decimal("0")
        mid = Decimal("0.5") * (long_px + short_px)
        return ((short_px - long_px) / mid * Decimal("10000")) if mid > 0 else Decimal("0")

    @staticmethod
    def _valid_bbo(
        quotes: Mapping[str, tuple[float, float]], iid: str
    ) -> tuple[Decimal, Decimal, Decimal, float] | None:
        q = quotes.get(iid)
        if q is None:
            return None
        bid_f, ask_f = q
        if bid_f <= 0 or ask_f < bid_f:
            return None
        bid = Decimal(str(bid_f))
        ask = Decimal(str(ask_f))
        mid = Decimal("0.5") * (bid + ask)
        if mid <= 0:
            return None
        sp = float((ask - bid) / mid * Decimal("10000"))
        return bid, ask, mid, sp

    def _check_daily_kill_guards(self, ts_s: int) -> str | None:
        daily_pnl, streak = self.ledger.daily_stats(ts_s)
        if daily_pnl <= -self.risk.max_daily_loss_usd:
            if not self.ledger.is_kill_halted()[0]:
                self.ledger.set_halt("HALTED_KILL", "daily_loss_kill", ts_s)
            return "daily_loss_kill"
        if streak >= self.risk.max_consecutive_unhedged:
            if not self.ledger.is_kill_halted()[0]:
                self.ledger.set_halt("HALTED_KILL", "consecutive_unhedged_kill", ts_s)
            return "consecutive_unhedged_kill"
        return None

    def _choose_maker_side(self, v_long: str, v_short: str, sp_l: float, sp_s: float) -> bool:
        """Return True if long leg should be Maker (prefer DEX leg, else wider-spread leg)."""
        l_dex, s_dex = v_long in DEX_VENUES, v_short in DEX_VENUES
        if l_dex != s_dex:
            return l_dex
        return sp_l >= sp_s

    def _anchored_maker_price(
        self,
        *,
        hedge_mid: Decimal,
        maker_bid: Decimal,
        maker_ask: Decimal,
        side: str,
        spec_maker: InstrumentSpec,
    ) -> Decimal:
        """Price Maker order anchored to hedge mid, clamped strictly passive inside maker BBO."""
        tick = spec_maker.price_increment
        if side == "BUY":
            raw = min(hedge_mid, maker_ask - tick)
            raw = min(raw, maker_bid)
        else:
            raw = max(hedge_mid, maker_bid + tick)
            raw = max(raw, maker_ask)
        return max(tick, spec_maker.round_price(raw))

    def has_active_maker_for_position(self, position_id: str) -> bool:
        return any(ctx.position_id == position_id for ctx in self._active_makers.values())

    def has_in_flight_orders(self, position_id: str) -> bool:
        return (
            self.has_active_maker_for_position(position_id)
            or position_id in self._active_dual_takers
            or position_id in self._active_emergency_closes
            or any(h.position_id == position_id for h in self._active_hedges.values())
            or bool(self.ledger.open_orders_for_position(position_id))
        )

    def is_tracked_order(self, client_order_id: str) -> bool:
        if client_order_id in self._active_makers:
            return True
        for batch in self._active_dual_takers.values():
            for leg in batch.active_legs():
                if client_order_id in (leg.client_order_id, leg.fallback_coid):
                    return True
        for task in self._active_hedges.values():
            if client_order_id in (task.client_order_id, task.fallback_coid):
                return True
        for close_task in self._active_emergency_closes.values():
            if any(leg.client_order_id == client_order_id for leg in close_task.legs):
                return True
        return False

    def on_cancel_rejected(self, client_order_id: str) -> None:
        ctx = self._active_makers.get(client_order_id)
        if ctx is not None:
            ctx.cancel_requested = False

    def has_any_in_flight_orders(self) -> bool:
        return bool(
            self._active_makers
            or self._active_dual_takers
            or self._active_hedges
            or self._active_emergency_closes
            or self.ledger.open_orders()
        )

    def _has_unhedged_exposure(
        self,
        pos: Mapping[str, object],
        *,
        spec_l: InstrumentSpec | None,
        spec_s: InstrumentSpec | None,
        live_mids: Mapping[str, float],
    ) -> bool:
        cur_l = Decimal(str(pos["long_base_qty"]))
        cur_s = Decimal(str(pos["short_base_qty"]))
        hi = max(cur_l, cur_s)
        if hi <= Decimal("0"):
            return False
        if cur_l <= Decimal("0") or cur_s <= Decimal("0"):
            return True
        diff = abs(cur_l - cur_s)
        ref_px = Decimal(
            str(
                live_mids.get(str(pos["iid_long"]))
                or live_mids.get(str(pos["iid_short"]))
                or pos["entry_l"]
                or pos["entry_s"]
                or 0
            )
        )
        unhedged_usd = diff * ref_px
        return unhedged_usd > self.risk.max_unhedged_usd or (diff / hi * Decimal("100")) > self.risk.max_qty_mismatch_pct

    def reconcile_shutdown_positions(
        self,
        *,
        specs: Mapping[str, InstrumentSpec],
        now_s: int,
        live_mids: Mapping[str, float],
    ) -> None:
        """Settle completed transitions or flatten unhedged exposure before allowing shutdown."""
        for pid, pos in sorted(self.ledger.active_positions().items()):
            if self.has_in_flight_orders(pid):
                continue
            status = str(pos["status"])
            cur_l = Decimal(str(pos["long_base_qty"]))
            cur_s = Decimal(str(pos["short_base_qty"]))
            spec_l = specs.get(str(pos["iid_long"]))
            spec_s = specs.get(str(pos["iid_short"]))
            imbalanced = self._has_unhedged_exposure(pos, spec_l=spec_l, spec_s=spec_s, live_mids=live_mids)
            if imbalanced:
                if not self.ledger.is_reconcile_halted()[0] and spec_l is not None and spec_s is not None:
                    self.emergency_close_position(
                        pid,
                        specs=specs,
                        reason="shutdown_unhedged_flatten",
                        now_s=now_s,
                        live_mids=live_mids,
                    )
            elif cur_l <= Decimal("0") and cur_s <= Decimal("0"):
                if status == "OPENING":
                    self.ledger.update_position_status(
                        pid,
                        status="CLOSED",
                        ts_s=now_s,
                        long_base_qty=Decimal("0"),
                        short_base_qty=Decimal("0"),
                        close_reason="shutdown_aborted",
                    )
                elif status == "CLOSING":
                    self._finalize_closed_position(pid, now_s=now_s)
                else:
                    self.ledger.update_position_status(
                        pid,
                        status="CLOSED",
                        ts_s=now_s,
                        close_reason="shutdown_aborted",
                    )
                self._slice_attempts.pop(pid, None)
            else:
                if status == "OPENING":
                    self.ledger.update_position_status(pid, status="OPEN", ts_s=now_s)
                    self._slice_attempts.pop(pid, None)
                elif status == "CLOSING":
                    if self.ledger.is_kill_halted()[0]:
                        if not self.ledger.is_reconcile_halted()[0] and spec_l is not None and spec_s is not None:
                            self.emergency_close_position(
                                pid,
                                specs=specs,
                                reason="shutdown_kill_flatten",
                                now_s=now_s,
                                live_mids=live_mids,
                            )
                    else:
                        self.ledger.update_position_status(pid, status="OPEN", ts_s=now_s)
                        self._slice_attempts.pop(pid, None)

    def is_shutdown_ready(
        self,
        *,
        specs: Mapping[str, InstrumentSpec],
        live_mids: Mapping[str, float],
    ) -> tuple[bool, list[str]]:
        """Return whether no orders, unhedged exposure, or transitional ledger states remain."""
        blockers: list[str] = []
        if self.has_any_in_flight_orders():
            blockers.append("in_flight_orders")
        for pid, pos in sorted(self.ledger.active_positions().items()):
            status = str(pos["status"])
            if status != "OPEN":
                blockers.append(f"position_not_settled:{pid}:{status}")
            spec_l = specs.get(str(pos["iid_long"]))
            spec_s = specs.get(str(pos["iid_short"]))
            if self._has_unhedged_exposure(pos, spec_l=spec_l, spec_s=spec_s, live_mids=live_mids):
                blockers.append(
                    f"unhedged_exposure:{pid}:L={pos['long_base_qty']},S={pos['short_base_qty']}"
                )
        return len(blockers) == 0, blockers

    def _is_position_closing(self, position_id: str, pos: Mapping[str, object] | None = None) -> bool:
        if position_id in self._active_emergency_closes:
            return True
        if pos is None:
            pos = self.ledger.active_positions().get(position_id)
        return pos is not None and str(pos["status"]) == "CLOSING"

    def _has_in_flight_opening_orders(self, position_id: str) -> bool:
        if any(ctx.position_id == position_id and ctx.is_opening for ctx in self._active_makers.values()):
            return True
        batch = self._active_dual_takers.get(position_id)
        if batch is not None and batch.is_opening:
            return True
        return any(h.position_id == position_id and h.is_opening for h in self._active_hedges.values())

    def _external_in_flight_closing_qty(self, position_id: str) -> tuple[Decimal, Decimal]:
        ext_l = Decimal("0")
        ext_s = Decimal("0")
        for ctx in self._active_makers.values():
            if ctx.position_id == position_id and not ctx.is_opening:
                rem = max(Decimal("0"), ctx.target_base_qty - ctx.accounted_maker_base_qty)
                if ctx.maker_side_is_long:
                    ext_l += rem
                else:
                    ext_s += rem
        for h in self._active_hedges.values():
            if h.position_id == position_id and not h.is_opening:
                if h.status not in TERMINAL_ORDER_STATUSES:
                    rem = max(Decimal("0"), h.target_base_qty - h.accounted_base_qty)
                    if h.hedge_is_long:
                        ext_l += rem
                    else:
                        ext_s += rem
                if h.fallback_coid is not None and h.fallback_status not in TERMINAL_ORDER_STATUSES:
                    rem_fb = max(Decimal("0"), h.fallback_target_base_qty - h.fallback_accounted_base_qty)
                    if h.hedge_is_long:
                        ext_l += rem_fb
                    else:
                        ext_s += rem_fb
        batch = self._active_dual_takers.get(position_id)
        if batch is not None and not batch.is_opening:
            for leg in batch.active_legs():
                if leg.status not in TERMINAL_ORDER_STATUSES:
                    rem = max(Decimal("0"), leg.target_base_qty - leg.accounted_base_qty)
                    if leg.is_long:
                        ext_l += rem
                    else:
                        ext_s += rem
                if leg.fallback_coid is not None and leg.fallback_status not in TERMINAL_ORDER_STATUSES:
                    rem_fb = max(Decimal("0"), leg.fallback_target_base_qty - leg.fallback_accounted_base_qty)
                    if leg.is_long:
                        ext_l += rem_fb
                    else:
                        ext_s += rem_fb
        return ext_l, ext_s

    def _has_external_in_flight_closing_orders(self, position_id: str) -> bool:
        if any(ctx.position_id == position_id and not ctx.is_opening for ctx in self._active_makers.values()):
            return True
        if any(h.position_id == position_id and not h.is_opening for h in self._active_hedges.values()):
            return True
        batch = self._active_dual_takers.get(position_id)
        return batch is not None and not batch.is_opening

    def _reconcile_or_finalize_position(
        self,
        position_id: str,
        *,
        now_s: int,
        live_mids: Mapping[str, float] | None = None,
    ) -> None:
        if position_id in self._active_emergency_closes:
            self._reconcile_emergency_close(
                self._active_emergency_closes[position_id],
                now_s=now_s,
                live_mids=live_mids,
            )
        else:
            self._maybe_finalize_position(position_id, now_s=now_s)

    def _maker_unhedged_qty(self, ctx: ActiveMakerLeg) -> Decimal:
        in_flight = sum(
            (
                max(h.target_base_qty, h.filled_base_qty + h.fallback_filled_base_qty)
                for h in self._active_hedges.values()
                if h.maker_coid == ctx.client_order_id
            ),
            Decimal("0"),
        )
        return ctx.accounted_maker_base_qty - (ctx.settled_hedged_base_qty + in_flight)

    def begin_shutdown(
        self,
        *,
        now_s: int,
        live_mids: Mapping[str, float] | None = None,
        live_quotes: Mapping[str, tuple[float, float]] | None = None,
    ) -> list[str]:
        """Enter stopping mode and cancel all active maker orders while preserving OPEN positions."""
        self._stopping = True
        canceled: list[str] = []
        for coid, ctx in list(self._active_makers.items()):
            if ctx.cancel_requested:
                continue
            c_ack = self.gateway.cancel_order(
                client_order_id=coid,
                venue=ctx.maker_venue,
                instrument_id=ctx.spec_maker.instrument_id,
            )
            ctx.cancel_requested = True
            self._update_order_from_ack(coid, c_ack, now_s=now_s)
            canceled.append(coid)
            if c_ack.filled_base_qty > ctx.accounted_maker_base_qty or (
                c_ack.status in TERMINAL_ORDER_STATUSES and self._maker_unhedged_qty(ctx) > Decimal("0")
            ):
                h_mid, h_bbo = self._hedge_quote_ctx(ctx, live_mids or {}, live_quotes)
                self.on_maker_progress(
                    coid,
                    filled_base_qty=c_ack.filled_base_qty,
                    terminal=(c_ack.status in TERMINAL_ORDER_STATUSES),
                    hedge_mid=h_mid,
                    now_s=now_s,
                    hedge_bbo=h_bbo,
                )
            if coid in self._active_makers and c_ack.status in TERMINAL_ORDER_STATUSES:
                self._active_makers.pop(coid, None)
                self._reconcile_or_finalize_position(ctx.position_id, now_s=now_s, live_mids=live_mids)
        return canceled

    def _update_order_from_ack(
        self,
        coid: str,
        ack: OrderAck,
        *,
        now_s: int,
        fallback_price: Decimal | None = None,
    ) -> None:
        fill_price = ack.avg_fill_price
        if fill_price is None and ack.filled_base_qty > 0:
            fill_price = fallback_price
        self.ledger.update_order_state(
            coid,
            status=ack.status,
            filled_base_qty=ack.filled_base_qty,
            fill_price=fill_price,
            venue_order_id=ack.venue_order_id or None,
            fee_usd=ack.fee_usd,
            ts_s=now_s,
        )

    def _poll_and_update(
        self,
        coid: str,
        *,
        venue: str,
        instrument_id: str,
        now_s: int,
        fallback_price: Decimal | None = None,
    ) -> OrderAck:
        ack = self.gateway.poll_order(client_order_id=coid, venue=venue, instrument_id=instrument_id)
        self._update_order_from_ack(coid, ack, now_s=now_s, fallback_price=fallback_price)
        return ack

    def _apply_leg_fill_delta(
        self,
        position_id: str,
        *,
        is_long: bool,
        is_opening: bool,
        delta_base: Decimal,
        now_s: int,
        clamp_dust_step: Decimal | None = None,
    ) -> tuple[Decimal, Decimal]:
        pos = self.ledger.active_positions().get(position_id)
        if pos is None:
            return Decimal("0"), Decimal("0")
        cur_l = Decimal(str(pos["long_base_qty"]))
        cur_s = Decimal(str(pos["short_base_qty"]))
        if delta_base <= 0:
            return cur_l, cur_s
        if is_opening:
            new_l = cur_l + delta_base if is_long else cur_l
            new_s = cur_s if is_long else cur_s + delta_base
        else:
            if is_long:
                new_l = max(Decimal("0"), cur_l - delta_base)
                if clamp_dust_step is not None and new_l < clamp_dust_step:
                    new_l = Decimal("0")
                new_s = cur_s
            else:
                new_s = max(Decimal("0"), cur_s - delta_base)
                if clamp_dust_step is not None and new_s < clamp_dust_step:
                    new_s = Decimal("0")
                new_l = cur_l
        self.ledger.update_position_status(
            position_id,
            status=str(pos["status"]),
            ts_s=now_s,
            long_base_qty=new_l,
            short_base_qty=new_s,
        )
        return new_l, new_s

    def _finalize_closed_position(
        self,
        position_id: str,
        *,
        now_s: int,
        close_reason: str | None = None,
    ) -> None:
        price_pnl = self.ledger.compute_position_price_pnl(position_id)
        funding_pnl = self.ledger.get_position_funding_pnl(position_id)
        fee_usd = self.ledger.compute_position_fee_usd(
            position_id,
            maker_fee_bps_by_venue=self.policy.maker_fee_bps_by_venue,
            taker_fee_bps_by_venue=self.policy.taker_fee_bps_by_venue,
        )
        net_pnl = (price_pnl + funding_pnl - fee_usd).quantize(Decimal("0.0001"))
        self.ledger.update_position_status(
            position_id,
            status="CLOSED",
            ts_s=now_s,
            price_pnl_usd=price_pnl,
            funding_pnl_usd=funding_pnl,
            fee_usd=fee_usd,
            realized_pnl_usd=net_pnl,
            close_reason=close_reason,
        )

    def _maybe_finalize_position(
        self,
        position_id: str,
        *,
        now_s: int,
        close_reason: str | None = None,
    ) -> None:
        if self.has_in_flight_orders(position_id):
            return
        pos = self.ledger.active_positions().get(position_id)
        if pos is None:
            return
        status = str(pos["status"])
        cur_l = Decimal(str(pos["long_base_qty"]))
        cur_s = Decimal(str(pos["short_base_qty"]))
        if status == "OPENING":
            if cur_l <= Decimal("0") and cur_s <= Decimal("0") and self._stopping:
                self.ledger.update_position_status(
                    position_id,
                    status="CLOSED",
                    ts_s=now_s,
                    long_base_qty=Decimal("0"),
                    short_base_qty=Decimal("0"),
                    close_reason="shutdown_aborted",
                )
                self._slice_attempts.pop(position_id, None)
            elif min(cur_l, cur_s) >= Decimal(str(pos["target_base_qty"])) * Decimal("0.98"):
                self.ledger.update_position_status(position_id, status="OPEN", ts_s=now_s)
                self._slice_attempts.pop(position_id, None)
        elif status == "CLOSING":
            if cur_l <= Decimal("0") and cur_s <= Decimal("0"):
                self._finalize_closed_position(position_id, now_s=now_s, close_reason=close_reason)
                self._slice_attempts.pop(position_id, None)

    def _is_position_imbalanced(
        self,
        pos: Mapping[str, object],
        spec_l: InstrumentSpec,
        spec_s: InstrumentSpec,
    ) -> bool:
        cur_l = Decimal(str(pos["long_base_qty"]))
        cur_s = Decimal(str(pos["short_base_qty"]))
        hi = max(cur_l, cur_s)
        if hi <= Decimal("0"):
            return False
        if cur_l <= Decimal("0") or cur_s <= Decimal("0"):
            return True
        return (abs(cur_l - cur_s) / hi * Decimal("100")) > self.risk.max_qty_mismatch_pct

    def _hedge_quote_ctx(
        self,
        ctx: ActiveMakerLeg,
        live_mids: Mapping[str, float],
        live_quotes: Mapping[str, tuple[float, float]] | None,
    ) -> tuple[Decimal, tuple[Decimal, Decimal] | None]:
        h_mid = Decimal(str(live_mids.get(ctx.spec_hedge.instrument_id, 0) or 1))
        q_h = live_quotes.get(ctx.spec_hedge.instrument_id) if live_quotes is not None else None
        h_bbo = (Decimal(str(q_h[0])), Decimal(str(q_h[1]))) if (q_h is not None and q_h[0] > 0 and q_h[1] >= q_h[0]) else None
        return h_mid, h_bbo

    def start_entry(
        self,
        candidate: EntryCandidate,
        *,
        specs: Mapping[str, InstrumentSpec],
        now_s: int | None = None,
    ) -> tuple[str | None, str | None]:
        ts = int(time.time()) if now_s is None else now_s
        if self._stopping:
            return None, "controller_stopping"
        halted, halt_reason = self.ledger.is_halted()
        if halted:
            return None, f"halted:{halt_reason}"
        kill_reason = self._check_daily_kill_guards(ts)
        if kill_reason is not None:
            return None, f"halted:{kill_reason}"

        spec_l = specs.get(candidate.long_leg.instrument_id)
        spec_s = specs.get(candidate.short_leg.instrument_id)
        if spec_l is None or spec_s is None:
            return None, "missing_instrument_spec"
        if spec_l.close_only or spec_s.close_only:
            return None, "close_only_contract"

        mid = Decimal(str(0.5 * (candidate.long_leg.mid + candidate.short_leg.mid)))
        aligned_full, err = align_leg_quantities(
            spec_l, spec_s,
            target_usd=self.risk.leg_notional_usd,
            mid_price=mid,
            max_mismatch_pct=self.risk.max_qty_mismatch_pct,
        )
        if aligned_full is None:
            return None, err

        pos_id = f"{candidate.symbol}-{ts}-{uuid.uuid4().hex[:6]}"
        self.ledger.create_position(
            position_id=pos_id,
            symbol=candidate.symbol,
            long_venue=candidate.long_venue,
            short_venue=candidate.short_venue,
            iid_long=candidate.long_leg.instrument_id,
            iid_short=candidate.short_leg.instrument_id,
            opened_ts=ts,
            entry_l=candidate.long_leg.mid,
            entry_s=candidate.short_leg.mid,
            entry_basis_bps=candidate.basis_bps,
            target_base_qty=min(aligned_full.long_base_qty, aligned_full.short_base_qty),
        )
        ok, slice_err = self._submit_next_slice(
            position_id=pos_id,
            is_opening=True,
            spec_l=spec_l,
            spec_s=spec_s,
            leg_l_bid=Decimal(str(candidate.long_leg.bid)),
            leg_l_ask=Decimal(str(candidate.long_leg.ask)),
            leg_l_mid=Decimal(str(candidate.long_leg.mid)),
            leg_s_bid=Decimal(str(candidate.short_leg.bid)),
            leg_s_ask=Decimal(str(candidate.short_leg.ask)),
            leg_s_mid=Decimal(str(candidate.short_leg.mid)),
            remaining_base=min(aligned_full.long_base_qty, aligned_full.short_base_qty),
            sp_l=candidate.long_leg.spread_bps or 0.0,
            sp_s=candidate.short_leg.spread_bps or 0.0,
            now_s=ts,
        )
        if not ok:
            pos_row = self.ledger.active_positions().get(pos_id)
            if pos_row is not None and (
                Decimal(str(pos_row["long_base_qty"])) <= Decimal("0")
                and Decimal(str(pos_row["short_base_qty"])) <= Decimal("0")
            ):
                self.ledger.update_position_status(
                    pos_id, status="CLOSED", ts_s=ts, close_reason=f"entry_aborted:{slice_err}"
                )
            return None, slice_err
        return pos_id, None

    def _closing_slice_target_usd(
        self,
        *,
        rem_usd: Decimal,
        spec_l: InstrumentSpec,
        spec_s: InstrumentSpec,
        mid: Decimal,
    ) -> Decimal:
        """Compute closing slice notional, absorbing or evenly splitting dust tails."""
        if rem_usd <= self.risk.slice_usd:
            return rem_usd
        min_venue_usd = max(
            spec_l.min_notional_usd,
            spec_s.min_notional_usd,
            spec_l.min_quantity * spec_l.contract_multiplier * mid,
            spec_s.min_quantity * spec_s.contract_multiplier * mid,
            spec_l.base_step * mid,
            spec_s.base_step * mid,
        )
        dust_usd = max(min_venue_usd, Decimal("10"), self.risk.slice_usd * Decimal("0.20"))
        if rem_usd - self.risk.slice_usd < dust_usd:
            if rem_usd <= self.risk.max_unhedged_usd:
                return rem_usd
            return rem_usd / Decimal("2")
        return self.risk.slice_usd

    def _submit_next_slice(
        self,
        *,
        position_id: str,
        is_opening: bool,
        spec_l: InstrumentSpec,
        spec_s: InstrumentSpec,
        leg_l_bid: Decimal,
        leg_l_ask: Decimal,
        leg_l_mid: Decimal,
        leg_s_bid: Decimal,
        leg_s_ask: Decimal,
        leg_s_mid: Decimal,
        remaining_base: Decimal,
        sp_l: float,
        sp_s: float,
        now_s: int,
        attempt: int = 1,
    ) -> tuple[bool, str | None]:
        pos = self.ledger.active_positions().get(position_id)
        if pos is None:
            return False, "position_not_active"
        if is_opening and self._is_position_closing(position_id, pos):
            return False, "position_closing"
        if is_opening and (spec_l.close_only or spec_s.close_only):
            return False, "close_only_contract"

        if is_opening:
            if (
                leg_l_bid <= 0
                or leg_l_ask < leg_l_bid
                or leg_s_bid <= 0
                or leg_s_ask < leg_s_bid
                or leg_l_mid <= 0
                or leg_s_mid <= 0
            ):
                return False, "invalid_quote_bbo"
            ratio = leg_s_mid / leg_l_mid
            dev = self.policy.max_price_ratio_deviation
            if ratio < Decimal("1") - dev or ratio > Decimal("1") + dev:
                return False, "price_ratio_mismatch"
            leg_sp_l = (leg_l_ask - leg_l_bid) / leg_l_mid * Decimal("10000")
            leg_sp_s = (leg_s_ask - leg_s_bid) / leg_s_mid * Decimal("10000")
            if leg_sp_l > self.policy.max_leg_spread_bps or leg_sp_s > self.policy.max_leg_spread_bps:
                return False, "spread_too_wide"
            sp_l = float(leg_sp_l)
            sp_s = float(leg_sp_s)
            mid_basis = self._exec_basis_bps(leg_l_mid, leg_s_mid)
            if mid_basis < self.policy.min_entry_basis_bps:
                return False, "entry_basis_below_min"
            if mid_basis > self.policy.max_entry_basis_bps:
                return False, "entry_basis_above_max"

        mid = Decimal("0.5") * (leg_l_mid + leg_s_mid)
        rem_usd = remaining_base * mid
        slice_target_usd = (
            min(self.risk.slice_usd, rem_usd)
            if is_opening
            else self._closing_slice_target_usd(rem_usd=rem_usd, spec_l=spec_l, spec_s=spec_s, mid=mid)
        )
        if slice_target_usd <= 0:
            return False, "nothing_remaining"

        aligned, err = align_leg_quantities(
            spec_l, spec_s,
            target_usd=slice_target_usd,
            mid_price=mid,
            max_mismatch_pct=self.risk.max_qty_mismatch_pct,
        )
        if aligned is None:
            return False, err

        if is_opening and self.policy.taker_fast_entry_basis_bps > 0 and leg_l_ask > 0 and leg_s_bid > 0:
            taker_b = self._exec_basis_bps(leg_l_ask, leg_s_bid)
            if taker_b > self.policy.max_entry_basis_bps:
                return False, "exec_basis_above_max"
            if self.policy.taker_fast_entry_basis_bps <= taker_b <= self.policy.max_entry_basis_bps:
                return self._submit_dual_taker_slice(
                    position_id=position_id,
                    pos=pos,
                    spec_l=spec_l,
                    spec_s=spec_s,
                    aligned=aligned,
                    leg_l_ask=leg_l_ask,
                    leg_s_bid=leg_s_bid,
                    now_s=now_s,
                )

        maker_is_long = self._choose_maker_side(str(pos["long_venue"]), str(pos["short_venue"]), sp_l, sp_s)
        if maker_is_long:
            spec_m, spec_h = spec_l, spec_s
            qty_m_contracts = aligned.long_qty_contracts
            base_m = aligned.long_base_qty
            maker_venue, hedge_venue = str(pos["long_venue"]), str(pos["short_venue"])
            maker_side = "BUY" if is_opening else "SELL"
            px = self._anchored_maker_price(hedge_mid=leg_s_mid, maker_bid=leg_l_bid, maker_ask=leg_l_ask, side=maker_side, spec_maker=spec_m)
            est_l, est_s = px, leg_s_bid
        else:
            spec_m, spec_h = spec_s, spec_l
            qty_m_contracts = aligned.short_qty_contracts
            base_m = aligned.short_base_qty
            maker_venue, hedge_venue = str(pos["short_venue"]), str(pos["long_venue"])
            maker_side = "SELL" if is_opening else "BUY"
            px = self._anchored_maker_price(hedge_mid=leg_l_mid, maker_bid=leg_s_bid, maker_ask=leg_s_ask, side=maker_side, spec_maker=spec_m)
            est_l, est_s = leg_l_ask, px

        if is_opening:
            maker_exec_b = self._exec_basis_bps(est_l, est_s)
            if maker_exec_b < self.policy.min_exec_basis_bps:
                return False, "exec_basis_below_min"
            if maker_exec_b > self.policy.max_entry_basis_bps:
                return False, "exec_basis_above_max"

        coid = make_client_order_id("mk", position_id)
        self.ledger.register_order_intent(
            client_order_id=coid,
            position_id=position_id,
            venue=maker_venue,
            instrument_id=spec_m.instrument_id,
            side=maker_side,
            role="MAKER_OPEN" if is_opening else "MAKER_CLOSE",
            order_type="POST_ONLY",
            qty_contracts=qty_m_contracts,
            base_qty=base_m,
            price=px,
            ts_s=now_s,
        )
        ack = self.gateway.submit_limit(
            client_order_id=coid,
            venue=maker_venue,
            instrument_id=spec_m.instrument_id,
            side=maker_side,
            qty_contracts=qty_m_contracts,
            price=px,
            post_only=True,
            ioc=False,
            reduce_only=not is_opening,
        )
        self._update_order_from_ack(coid, ack, now_s=now_s)
        if ack.status == "REJECTED":
            return False, f"maker_rejected:{ack.reason}"

        ctx = ActiveMakerLeg(
            client_order_id=coid,
            position_id=position_id,
            is_opening=is_opening,
            maker_side_is_long=maker_is_long,
            maker_venue=maker_venue,
            hedge_venue=hedge_venue,
            spec_maker=spec_m,
            spec_hedge=spec_h,
            submitted_ts=now_s,
            attempts=attempt,
            target_base_qty=base_m,
        )
        self._active_makers[coid] = ctx
        if ack.filled_base_qty > 0:
            self.on_maker_progress(
                coid, filled_base_qty=ack.filled_base_qty, terminal=(ack.status in TERMINAL_ORDER_STATUSES),
                hedge_mid=leg_s_mid if maker_is_long else leg_l_mid, now_s=now_s,
                hedge_bbo=(leg_s_bid, leg_s_ask) if maker_is_long else (leg_l_bid, leg_l_ask),
            )
        return True, None

    def _submit_dual_taker_slice(
        self,
        *,
        position_id: str,
        pos: Mapping[str, object],
        spec_l: InstrumentSpec,
        spec_s: InstrumentSpec,
        aligned: object,
        leg_l_ask: Decimal,
        leg_s_bid: Decimal,
        now_s: int,
    ) -> tuple[bool, str | None]:
        """Submit simultaneous IOC orders on both legs for wide executable-basis opportunities."""
        slip = self.runtime.hedge_ioc_slippage_bps / Decimal("10000")
        specs_sides = [
            (True, spec_l, str(pos["long_venue"]), "BUY", aligned.long_qty_contracts, aligned.long_base_qty, leg_l_ask, spec_l.round_price(leg_l_ask * (Decimal("1") + slip))),
            (False, spec_s, str(pos["short_venue"]), "SELL", aligned.short_qty_contracts, aligned.short_base_qty, leg_s_bid, spec_s.round_price(leg_s_bid * (Decimal("1") - slip))),
        ]
        legs: list[ActiveDualTakerLeg] = []
        for is_long, spec, venue, side, qty_c, base_q, ref_px, ioc_px in specs_sides:
            coid = make_client_order_id("tk", position_id)
            self.ledger.register_order_intent(
                client_order_id=coid, position_id=position_id, venue=venue,
                instrument_id=spec.instrument_id, side=side, role="TAKER_OPEN",
                order_type="IOC", qty_contracts=qty_c, base_qty=base_q, price=ioc_px, ts_s=now_s,
            )
            ack = self.gateway.submit_limit(
                client_order_id=coid, venue=venue, instrument_id=spec.instrument_id,
                side=side, qty_contracts=qty_c, price=ioc_px, post_only=False, ioc=True, reduce_only=False,
            )
            self._update_order_from_ack(coid, ack, now_s=now_s)
            legs.append(
                ActiveDualTakerLeg(
                    client_order_id=coid,
                    is_long=is_long,
                    venue=venue,
                    spec=spec,
                    side=side,
                    target_base_qty=base_q,
                    ref_price=ref_px,
                    status=ack.status,
                    filled_base_qty=ack.filled_base_qty,
                )
            )

        batch = ActiveDualTakerBatch(
            position_id=position_id,
            long_leg=legs[0],
            short_leg=legs[1],
            submitted_ts=now_s,
        )
        self._active_dual_takers[position_id] = batch
        outcome = self._reconcile_dual_taker_batch(batch, now_s=now_s)
        if outcome == "UNFILLED":
            return False, "dual_taker_ioc_unfilled"
        return True, None

    def _submit_dual_taker_close_slice(
        self,
        *,
        position_id: str,
        pos: Mapping[str, object],
        spec_l: InstrumentSpec,
        spec_s: InstrumentSpec,
        close_l_base: Decimal,
        close_s_base: Decimal,
        leg_l_bid: Decimal,
        leg_s_ask: Decimal,
        now_s: int,
    ) -> tuple[bool, str | None]:
        """Submit reduce-only slippage-protected IOC orders (with async MARKET fallback) on remaining legs."""
        slip = self.runtime.hedge_ioc_slippage_bps / Decimal("10000")
        px_l = max(spec_l.price_increment, spec_l.round_price(leg_l_bid * (Decimal("1") - slip)))
        px_s = max(spec_s.price_increment, spec_s.round_price(leg_s_ask * (Decimal("1") + slip)))
        specs_sides = (
            (True, spec_l, str(pos["long_venue"]), "SELL", close_l_base, leg_l_bid, px_l),
            (False, spec_s, str(pos["short_venue"]), "BUY", close_s_base, leg_s_ask, px_s),
        )
        long_leg_obj: ActiveDualTakerLeg | None = None
        short_leg_obj: ActiveDualTakerLeg | None = None
        for is_long, spec, venue, side, close_base, ref_px, ioc_px in specs_sides:
            if close_base <= 0:
                continue
            qty_c = max(spec.size_increment, (close_base / spec.base_step).to_integral_value() * spec.size_increment)
            base_q = qty_c * spec.contract_multiplier
            coid = make_client_order_id("tc", position_id)
            self.ledger.register_order_intent(
                client_order_id=coid,
                position_id=position_id,
                venue=venue,
                instrument_id=spec.instrument_id,
                side=side,
                role="TAKER_CLOSE",
                order_type="IOC",
                qty_contracts=qty_c,
                base_qty=base_q,
                price=ioc_px,
                ts_s=now_s,
            )
            ack = self.gateway.submit_limit(
                client_order_id=coid,
                venue=venue,
                instrument_id=spec.instrument_id,
                side=side,
                qty_contracts=qty_c,
                price=ioc_px,
                post_only=False,
                ioc=True,
                reduce_only=True,
            )
            self._update_order_from_ack(coid, ack, now_s=now_s, fallback_price=ref_px)
            leg_obj = ActiveDualTakerLeg(
                client_order_id=coid,
                is_long=is_long,
                venue=venue,
                spec=spec,
                side=side,
                target_base_qty=base_q,
                ref_price=ref_px,
                status=ack.status,
                filled_base_qty=ack.filled_base_qty,
            )
            if is_long:
                long_leg_obj = leg_obj
            else:
                short_leg_obj = leg_obj

        if long_leg_obj is None and short_leg_obj is None:
            return False, "nothing_remaining"

        batch = ActiveDualTakerBatch(
            position_id=position_id,
            long_leg=long_leg_obj,
            short_leg=short_leg_obj,
            submitted_ts=now_s,
            is_opening=False,
        )
        self._active_dual_takers[position_id] = batch
        outcome = self._reconcile_dual_taker_batch(batch, now_s=now_s)
        if outcome == "UNFILLED":
            return False, "taker_close_unfilled"
        return True, None

    def _reconcile_dual_taker_batch(self, batch: ActiveDualTakerBatch, *, now_s: int) -> str:
        active_legs = batch.active_legs()
        for leg in active_legs:
            d = leg.filled_base_qty - leg.accounted_base_qty
            if d > 0:
                self._apply_leg_fill_delta(
                    batch.position_id,
                    is_long=leg.is_long,
                    is_opening=batch.is_opening,
                    delta_base=d,
                    now_s=now_s,
                    clamp_dust_step=(leg.spec.base_step if (not batch.is_opening and leg.status == "FILLED") else None),
                )
                leg.accounted_base_qty += d
            df = leg.fallback_filled_base_qty - leg.fallback_accounted_base_qty
            if df > 0:
                self._apply_leg_fill_delta(
                    batch.position_id,
                    is_long=leg.is_long,
                    is_opening=batch.is_opening,
                    delta_base=df,
                    now_s=now_s,
                    clamp_dust_step=(leg.spec.base_step if (not batch.is_opening and leg.fallback_status == "FILLED") else None),
                )
                leg.fallback_accounted_base_qty += df

        if any(leg.status not in TERMINAL_ORDER_STATUSES for leg in active_legs):
            return "PENDING"

        filled_l = batch.long_leg.filled_base_qty if batch.long_leg is not None else Decimal("0")
        filled_s = batch.short_leg.filled_base_qty if batch.short_leg is not None else Decimal("0")
        if batch.is_opening and filled_l <= 0 and filled_s <= 0:
            self._active_dual_takers.pop(batch.position_id, None)
            if batch.position_id in self._active_emergency_closes:
                self._reconcile_emergency_close(self._active_emergency_closes[batch.position_id], now_s=now_s)
            return "UNFILLED"

        suppress_fallback = (
            (batch.is_opening and self._is_position_closing(batch.position_id))
            or (batch.position_id in self._active_emergency_closes)
            or self.ledger.is_reconcile_halted()[0]
        )
        if not batch.fallback_submitted:
            if suppress_fallback:
                batch.fallback_submitted = True
            elif batch.is_opening:
                if filled_l != filled_s and batch.long_leg is not None and batch.short_leg is not None:
                    leading, lagging = (
                        (batch.long_leg, batch.short_leg) if filled_l > filled_s else (batch.short_leg, batch.long_leg)
                    )
                    delta = leading.filled_base_qty - lagging.filled_base_qty
                    if lagging.filled_base_qty < lagging.target_base_qty and delta > Decimal("0"):
                        n_steps = (delta / lagging.spec.base_step).to_integral_value()
                        if n_steps == 0 and delta > leading.filled_base_qty * (self.risk.max_qty_mismatch_pct / Decimal("100")):
                            n_steps = Decimal("1")
                        if n_steps > 0:
                            rem_c = n_steps * lagging.spec.size_increment
                            m_coid = make_client_order_id("tm", batch.position_id)
                            self.ledger.register_order_intent(
                                client_order_id=m_coid,
                                position_id=batch.position_id,
                                venue=lagging.venue,
                                instrument_id=lagging.spec.instrument_id,
                                side=lagging.side,
                                role="TAKER_OPEN_MARKET_FALLBACK",
                                order_type="MARKET",
                                qty_contracts=rem_c,
                                base_qty=rem_c * lagging.spec.contract_multiplier,
                                price=None,
                                ts_s=now_s,
                            )
                            m_ack = self.gateway.submit_market(
                                client_order_id=m_coid,
                                venue=lagging.venue,
                                instrument_id=lagging.spec.instrument_id,
                                side=lagging.side,
                                qty_contracts=rem_c,
                                reduce_only=False,
                            )
                            self._update_order_from_ack(m_coid, m_ack, now_s=now_s, fallback_price=lagging.ref_price)
                            lagging.fallback_coid = m_coid
                            lagging.fallback_status = m_ack.status
                            lagging.fallback_target_base_qty = rem_c * lagging.spec.contract_multiplier
                            lagging.fallback_filled_base_qty = m_ack.filled_base_qty
                            if m_ack.filled_base_qty > 0:
                                self._apply_leg_fill_delta(
                                    batch.position_id,
                                    is_long=lagging.is_long,
                                    is_opening=True,
                                    delta_base=m_ack.filled_base_qty,
                                    now_s=now_s,
                                )
                                lagging.fallback_accounted_base_qty = m_ack.filled_base_qty
            else:
                for leg in active_legs:
                    unfilled = leg.target_base_qty - leg.filled_base_qty
                    if unfilled > Decimal("0") and leg.status != "FILLED":
                        rem_c = max(
                            leg.spec.size_increment,
                            (unfilled / leg.spec.base_step).to_integral_value() * leg.spec.size_increment,
                        )
                        m_coid = make_client_order_id("tmc", batch.position_id)
                        self.ledger.register_order_intent(
                            client_order_id=m_coid,
                            position_id=batch.position_id,
                            venue=leg.venue,
                            instrument_id=leg.spec.instrument_id,
                            side=leg.side,
                            role="TAKER_CLOSE_MARKET_FALLBACK",
                            order_type="MARKET",
                            qty_contracts=rem_c,
                            base_qty=rem_c * leg.spec.contract_multiplier,
                            price=None,
                            ts_s=now_s,
                        )
                        m_ack = self.gateway.submit_market(
                            client_order_id=m_coid,
                            venue=leg.venue,
                            instrument_id=leg.spec.instrument_id,
                            side=leg.side,
                            qty_contracts=rem_c,
                            reduce_only=True,
                        )
                        self._update_order_from_ack(m_coid, m_ack, now_s=now_s, fallback_price=leg.ref_price)
                        leg.fallback_coid = m_coid
                        leg.fallback_status = m_ack.status
                        leg.fallback_target_base_qty = rem_c * leg.spec.contract_multiplier
                        leg.fallback_filled_base_qty = m_ack.filled_base_qty
                        if m_ack.filled_base_qty > 0:
                            self._apply_leg_fill_delta(
                                batch.position_id,
                                is_long=leg.is_long,
                                is_opening=False,
                                delta_base=m_ack.filled_base_qty,
                                now_s=now_s,
                                clamp_dust_step=(leg.spec.base_step if m_ack.status == "FILLED" else None),
                            )
                            leg.fallback_accounted_base_qty = m_ack.filled_base_qty
            batch.fallback_submitted = True

        for leg in active_legs:
            if leg.fallback_coid is not None and leg.fallback_status not in TERMINAL_ORDER_STATUSES:
                return "PENDING"

        self._active_dual_takers.pop(batch.position_id, None)
        tot_l = (batch.long_leg.filled_base_qty + batch.long_leg.fallback_filled_base_qty) if batch.long_leg is not None else Decimal("0")
        tot_s = (batch.short_leg.filled_base_qty + batch.short_leg.fallback_filled_base_qty) if batch.short_leg is not None else Decimal("0")
        if not suppress_fallback:
            if batch.is_opening:
                hi = max(tot_l, tot_s)
                imbalanced = (
                    hi > Decimal("0")
                    and batch.long_leg is not None
                    and batch.short_leg is not None
                    and (
                        tot_l <= Decimal("0")
                        or tot_s <= Decimal("0")
                        or (abs(tot_l - tot_s) / hi * Decimal("100")) > self.risk.max_qty_mismatch_pct
                    )
                )
                if batch.fallback_submitted and imbalanced:
                    self.ledger.increment_unhedged_streak(now_s)
                    self.ledger.record_event(
                        "dual_taker_fallback_unhedged",
                        {"position_id": batch.position_id, "long_filled": str(tot_l), "short_filled": str(tot_s)},
                        now_s,
                    )
                else:
                    self._slice_attempts[batch.position_id] = 0
                    if tot_l > Decimal("0") and tot_s > Decimal("0"):
                        self.ledger.increment_unhedged_streak(now_s, reset=True)
            else:
                shortfall = any(
                    leg.filled_base_qty + leg.fallback_filled_base_qty + leg.spec.base_step <= leg.target_base_qty
                    for leg in active_legs
                )
                if shortfall:
                    self.ledger.increment_unhedged_streak(now_s)
                    self.ledger.record_event(
                        "dual_taker_close_fallback_shortfall",
                        {"position_id": batch.position_id, "long_filled": str(tot_l), "short_filled": str(tot_s)},
                        now_s,
                    )
                else:
                    self._slice_attempts[batch.position_id] = 0
                    if tot_l > Decimal("0") or tot_s > Decimal("0"):
                        self.ledger.increment_unhedged_streak(now_s, reset=True)

        if batch.position_id in self._active_emergency_closes:
            self._reconcile_emergency_close(self._active_emergency_closes[batch.position_id], now_s=now_s)
        else:
            self._maybe_finalize_position(batch.position_id, now_s=now_s)
        return "FILLED" if (tot_l > 0 or tot_s > 0) else "UNFILLED"

    def on_maker_progress(
        self,
        client_order_id: str,
        *,
        filled_base_qty: Decimal,
        terminal: bool,
        hedge_mid: Decimal,
        now_s: int,
        hedge_bbo: tuple[Decimal, Decimal] | None = None,
    ) -> OrderAck | None:
        """Immediately hedge any newly filled maker delta on the hedge venue using protected IOC -> Market."""
        ctx = self._active_makers.get(client_order_id)
        if ctx is None:
            return None
        pos = self.ledger.active_positions().get(ctx.position_id)
        if pos is None:
            if terminal:
                self._active_makers.pop(client_order_id, None)
            return None

        delta_maker = filled_base_qty - ctx.accounted_maker_base_qty
        if delta_maker > Decimal("0"):
            self._slice_attempts[ctx.position_id] = 0
            self._apply_leg_fill_delta(
                ctx.position_id,
                is_long=ctx.maker_side_is_long,
                is_opening=ctx.is_opening,
                delta_base=delta_maker,
                now_s=now_s,
            )
            ctx.accounted_maker_base_qty += delta_maker
            pos = self.ledger.active_positions().get(ctx.position_id)

        if terminal:
            self._active_makers.pop(client_order_id, None)

        if (
            (ctx.is_opening and self._is_position_closing(ctx.position_id, pos))
            or (ctx.position_id in self._active_emergency_closes)
            or self.ledger.is_reconcile_halted()[0]
        ):
            if ctx.position_id in self._active_emergency_closes:
                self._reconcile_emergency_close(
                    self._active_emergency_closes[ctx.position_id],
                    now_s=now_s,
                    live_mids={ctx.spec_hedge.instrument_id: float(hedge_mid)} if hedge_mid > 0 else None,
                )
            elif terminal:
                self._maybe_finalize_position(ctx.position_id, now_s=now_s)
            return None

        if ctx.is_opening and (ctx.spec_hedge.close_only or ctx.spec_maker.close_only):
            spec_l_m, spec_s_m = (
                (ctx.spec_maker, ctx.spec_hedge) if ctx.maker_side_is_long else (ctx.spec_hedge, ctx.spec_maker)
            )
            if ctx.position_id not in self._active_emergency_closes:
                pos_m = self.ledger.active_positions().get(ctx.position_id)
                if pos_m is not None and self._is_position_imbalanced(pos_m, spec_l_m, spec_s_m):
                    self.emergency_close_position(
                        ctx.position_id,
                        specs={spec_l_m.instrument_id: spec_l_m, spec_s_m.instrument_id: spec_s_m},
                        reason="close_only_hedge_race",
                        now_s=now_s,
                        live_mids={ctx.spec_hedge.instrument_id: float(hedge_mid)} if hedge_mid > 0 else None,
                    )
                elif terminal:
                    self._maybe_finalize_position(ctx.position_id, now_s=now_s)
            return None

        unhedged = self._maker_unhedged_qty(ctx)
        if unhedged <= Decimal("0") or pos is None:
            if terminal:
                self._maybe_finalize_position(ctx.position_id, now_s=now_s)
            return None

        step_h = ctx.spec_hedge.base_step
        n_floor = (unhedged / step_h).to_integral_value(rounding=ROUND_DOWN)
        n_h = n_floor
        if terminal and (unhedged - n_floor * step_h) > Decimal("0"):
            cur_l = Decimal(str(pos["long_base_qty"]))
            cur_s = Decimal(str(pos["short_base_qty"]))
            cur_m, cur_h = (cur_l, cur_s) if ctx.maker_side_is_long else (cur_s, cur_l)
            hedge_is_long = not ctx.maker_side_is_long
            in_flight_pos_h_rem = sum(
                (
                    max(Decimal("0"), h.target_base_qty - (h.accounted_base_qty + h.fallback_accounted_base_qty))
                    for h in self._active_hedges.values()
                    if h.position_id == ctx.position_id
                    and h.hedge_is_long == hedge_is_long
                    and h.is_opening == ctx.is_opening
                ),
                Decimal("0"),
            )
            if ctx.is_opening:
                proj_h = cur_h + in_flight_pos_h_rem
                pos_gap = cur_m - proj_h
            else:
                proj_h = max(Decimal("0"), cur_h - in_flight_pos_h_rem)
                pos_gap = proj_h - cur_m
            base_floor = n_floor * step_h
            base_ceil = (n_floor + Decimal("1")) * step_h
            if (
                abs(unhedged - base_ceil) < abs(unhedged - base_floor)
                and abs(pos_gap - base_ceil) < abs(pos_gap - base_floor)
                and (ctx.is_opening or base_ceil <= proj_h)
            ):
                n_h = n_floor + Decimal("1")

        if n_h <= Decimal("0"):
            if terminal:
                self._maybe_finalize_position(ctx.position_id, now_s=now_s)
            return None

        qty_h_contracts = n_h * ctx.spec_hedge.size_increment
        base_h = n_h * step_h

        # Determine hedge side: opposite directional exposure from maker
        if ctx.is_opening:
            hedge_side = "SELL" if ctx.maker_side_is_long else "BUY"
        else:
            hedge_side = "BUY" if ctx.maker_side_is_long else "SELL"

        slip = self.runtime.hedge_ioc_slippage_bps / Decimal("10000")
        ref_px = (hedge_bbo[1] if hedge_side == "BUY" else hedge_bbo[0]) if hedge_bbo is not None else hedge_mid
        ioc_px = ctx.spec_hedge.round_price(ref_px * (Decimal("1") + slip if hedge_side == "BUY" else Decimal("1") - slip))
        h_coid = make_client_order_id("hg", ctx.position_id)
        self.ledger.register_order_intent(
            client_order_id=h_coid,
            position_id=ctx.position_id,
            venue=ctx.hedge_venue,
            instrument_id=ctx.spec_hedge.instrument_id,
            side=hedge_side,
            role="HEDGE_OPEN" if ctx.is_opening else "HEDGE_CLOSE",
            order_type="IOC",
            qty_contracts=qty_h_contracts,
            base_qty=base_h,
            price=ioc_px,
            ts_s=now_s,
        )
        ack = self.gateway.submit_limit(
            client_order_id=h_coid,
            venue=ctx.hedge_venue,
            instrument_id=ctx.spec_hedge.instrument_id,
            side=hedge_side,
            qty_contracts=qty_h_contracts,
            price=ioc_px,
            post_only=False,
            ioc=True,
            reduce_only=not ctx.is_opening,
        )
        self._update_order_from_ack(h_coid, ack, now_s=now_s, fallback_price=hedge_mid)

        task = ActiveHedgeOrder(
            client_order_id=h_coid,
            position_id=ctx.position_id,
            is_opening=ctx.is_opening,
            hedge_is_long=not ctx.maker_side_is_long,
            venue=ctx.hedge_venue,
            spec=ctx.spec_hedge,
            side=hedge_side,
            target_base_qty=base_h,
            ref_price=hedge_mid,
            submitted_ts=now_s,
            status=ack.status,
            filled_base_qty=ack.filled_base_qty,
            maker_coid=client_order_id,
        )
        self._active_hedges[h_coid] = task
        return self._reconcile_hedge_order(task, now_s=now_s)

    def _reconcile_hedge_order(self, task: ActiveHedgeOrder, *, now_s: int) -> OrderAck:
        d = task.filled_base_qty - task.accounted_base_qty
        if d > 0:
            self._apply_leg_fill_delta(
                task.position_id,
                is_long=task.hedge_is_long,
                is_opening=task.is_opening,
                delta_base=d,
                now_s=now_s,
            )
            task.accounted_base_qty += d
        df = task.fallback_filled_base_qty - task.fallback_accounted_base_qty
        if df > 0:
            self._apply_leg_fill_delta(
                task.position_id,
                is_long=task.hedge_is_long,
                is_opening=task.is_opening,
                delta_base=df,
                now_s=now_s,
            )
            task.fallback_accounted_base_qty += df

        if task.status not in TERMINAL_ORDER_STATUSES:
            return OrderAck(task.client_order_id, "", task.status, task.filled_base_qty, None)

        suppress_fallback = (
            (task.is_opening and self._is_position_closing(task.position_id))
            or (task.position_id in self._active_emergency_closes)
            or self.ledger.is_reconcile_halted()[0]
        )
        unfilled_h = task.target_base_qty - task.filled_base_qty
        if unfilled_h > Decimal("0") and task.fallback_coid is None and not suppress_fallback:
            step_h = task.spec.base_step
            rem_contracts = max(task.spec.size_increment, (unfilled_h / step_h).to_integral_value() * task.spec.size_increment)
            m_coid = make_client_order_id("hm", task.position_id)
            self.ledger.register_order_intent(
                client_order_id=m_coid,
                position_id=task.position_id,
                venue=task.venue,
                instrument_id=task.spec.instrument_id,
                side=task.side,
                role="HEDGE_OPEN_MARKET_FALLBACK" if task.is_opening else "HEDGE_CLOSE_MARKET_FALLBACK",
                order_type="MARKET",
                qty_contracts=rem_contracts,
                base_qty=rem_contracts * task.spec.contract_multiplier,
                price=None,
                ts_s=now_s,
            )
            m_ack = self.gateway.submit_market(
                client_order_id=m_coid,
                venue=task.venue,
                instrument_id=task.spec.instrument_id,
                side=task.side,
                qty_contracts=rem_contracts,
                reduce_only=not task.is_opening,
            )
            self._update_order_from_ack(m_coid, m_ack, now_s=now_s, fallback_price=task.ref_price)
            task.fallback_coid = m_coid
            task.fallback_status = m_ack.status
            task.fallback_target_base_qty = rem_contracts * task.spec.contract_multiplier
            task.fallback_filled_base_qty = m_ack.filled_base_qty
            if m_ack.filled_base_qty > 0:
                self._apply_leg_fill_delta(
                    task.position_id,
                    is_long=task.hedge_is_long,
                    is_opening=task.is_opening,
                    delta_base=m_ack.filled_base_qty,
                    now_s=now_s,
                )
                task.fallback_accounted_base_qty = m_ack.filled_base_qty

        total_hedged = task.filled_base_qty + task.fallback_filled_base_qty
        if task.fallback_coid is not None and task.fallback_status not in TERMINAL_ORDER_STATUSES:
            return OrderAck(task.fallback_coid, "", task.fallback_status or "OPEN", total_hedged, None)

        self._active_hedges.pop(task.client_order_id, None)
        if task.maker_coid is not None:
            maker_ctx = self._active_makers.get(task.maker_coid)
            if maker_ctx is not None:
                maker_ctx.settled_hedged_base_qty += total_hedged
        if not suppress_fallback:
            if total_hedged + task.spec.base_step <= task.target_base_qty:
                self.ledger.increment_unhedged_streak(now_s)
                self.ledger.record_event(
                    "hedge_fallback_unhedged",
                    {"position_id": task.position_id, "target": str(task.target_base_qty), "filled": str(total_hedged)},
                    now_s,
                )
            elif total_hedged > Decimal("0"):
                self.ledger.increment_unhedged_streak(now_s, reset=True)
        if task.position_id in self._active_emergency_closes:
            self._reconcile_emergency_close(self._active_emergency_closes[task.position_id], now_s=now_s)
        else:
            self._maybe_finalize_position(task.position_id, now_s=now_s)
        if task.fallback_coid is not None:
            return OrderAck(task.fallback_coid, "", task.fallback_status or "FILLED", total_hedged, None)
        return OrderAck(task.client_order_id, "", task.status, task.filled_base_qty, None)

    def advance_pending_slices(
        self,
        *,
        specs: Mapping[str, InstrumentSpec],
        quotes: Mapping[str, tuple[float, float]],
        mids: Mapping[str, float],
        now_s: int,
    ) -> list[str]:
        """Submit the next $50 slice for OPENING or CLOSING positions that have no in-flight orders."""
        if self._stopping or self.ledger.is_reconcile_halted()[0]:
            return []
        submitted: list[str] = []
        for pid, pos in sorted(self.ledger.active_positions().items()):
            status = str(pos["status"])
            if status not in ("OPENING", "CLOSING"):
                continue
            iid_l, iid_s = str(pos["iid_long"]), str(pos["iid_short"])
            spec_l, spec_s = specs.get(iid_l), specs.get(iid_s)
            if spec_l is None or spec_s is None:
                continue
            if status == "OPENING" and (spec_l.close_only or spec_s.close_only):
                for coid, ctx in list(self._active_makers.items()):
                    if ctx.position_id != pid or not ctx.is_opening:
                        continue
                    ctx.spec_maker = spec_l if ctx.maker_side_is_long else spec_s
                    ctx.spec_hedge = spec_s if ctx.maker_side_is_long else spec_l
                    if not ctx.cancel_requested:
                        c_ack = self.gateway.cancel_order(
                            client_order_id=coid,
                            venue=ctx.maker_venue,
                            instrument_id=ctx.spec_maker.instrument_id,
                        )
                        ctx.cancel_requested = True
                        self._update_order_from_ack(coid, c_ack, now_s=now_s)
                        if c_ack.filled_base_qty > ctx.accounted_maker_base_qty:
                            self._apply_leg_fill_delta(
                                pid,
                                is_long=ctx.maker_side_is_long,
                                is_opening=True,
                                delta_base=c_ack.filled_base_qty - ctx.accounted_maker_base_qty,
                                now_s=now_s,
                            )
                            ctx.accounted_maker_base_qty = c_ack.filled_base_qty
                        if c_ack.status in TERMINAL_ORDER_STATUSES:
                            self._active_makers.pop(coid, None)
                pos_now = self.ledger.active_positions().get(pid)
                if pos_now is not None and self._is_position_imbalanced(pos_now, spec_l, spec_s):
                    self.emergency_close_position(
                        pid,
                        specs=specs,
                        reason="close_only_hedge_race",
                        now_s=now_s,
                        live_mids=mids,
                    )
                    continue
                if pos_now is not None:
                    pos = pos_now
            if self.has_in_flight_orders(pid):
                continue

            cur_l = Decimal(str(pos["long_base_qty"]))
            cur_s = Decimal(str(pos["short_base_qty"]))
            bbo_l = self._valid_bbo(quotes, iid_l)
            bbo_s = self._valid_bbo(quotes, iid_s)

            if status == "OPENING":
                if self._is_position_imbalanced(pos, spec_l, spec_s):
                    continue
                if spec_l.close_only or spec_s.close_only:
                    if cur_l <= Decimal("0") and cur_s <= Decimal("0"):
                        self.ledger.update_position_status(
                            pid,
                            status="CLOSED",
                            ts_s=now_s,
                            long_base_qty=Decimal("0"),
                            short_base_qty=Decimal("0"),
                            close_reason="entry_aborted:close_only_contract",
                        )
                        self._slice_attempts.pop(pid, None)
                    elif cur_l > Decimal("0") and cur_s > Decimal("0") and not self._is_position_imbalanced(pos, spec_l, spec_s):
                        self.ledger.update_position_status(pid, status="OPEN", ts_s=now_s)
                        self._slice_attempts.pop(pid, None)
                    continue
                rem = Decimal(str(pos["target_base_qty"])) - min(cur_l, cur_s)
                if rem <= Decimal("0"):
                    self.ledger.update_position_status(pid, status="OPEN", ts_s=now_s)
                    self._slice_attempts.pop(pid, None)
                    continue
                if self._slice_attempts.get(pid, 0) >= self.runtime.maker_max_attempts:
                    if cur_l <= Decimal("0") and cur_s <= Decimal("0"):
                        self.ledger.update_position_status(
                            pid, status="CLOSED", ts_s=now_s,
                            long_base_qty=Decimal("0"), short_base_qty=Decimal("0"),
                            close_reason="entry_maker_timeout",
                        )
                        self._slice_attempts.pop(pid, None)
                    elif cur_l > Decimal("0") and cur_s > Decimal("0") and not self._is_position_imbalanced(pos, spec_l, spec_s):
                        self.ledger.update_position_status(pid, status="OPEN", ts_s=now_s)
                        self._slice_attempts.pop(pid, None)
                    continue
                if bbo_l is None or bbo_s is None:
                    continue
                bid_l, ask_l, mid_l, sp_l = bbo_l
                bid_s, ask_s, mid_s, sp_s = bbo_s
                ok, err = self._submit_next_slice(
                    position_id=pid, is_opening=True, spec_l=spec_l, spec_s=spec_s,
                    leg_l_bid=bid_l, leg_l_ask=ask_l, leg_l_mid=mid_l,
                    leg_s_bid=bid_s, leg_s_ask=ask_s, leg_s_mid=mid_s,
                    remaining_base=rem, sp_l=sp_l, sp_s=sp_s, now_s=now_s,
                )
                if ok:
                    submitted.append(pid)
                elif err in _OPENING_UNFILLABLE_DUST_ERRORS:
                    if cur_l <= Decimal("0") and cur_s <= Decimal("0"):
                        self.ledger.update_position_status(
                            pid,
                            status="CLOSED",
                            ts_s=now_s,
                            long_base_qty=Decimal("0"),
                            short_base_qty=Decimal("0"),
                            close_reason=f"entry_aborted:{err}",
                        )
                        self._slice_attempts.pop(pid, None)
                    elif cur_l > Decimal("0") and cur_s > Decimal("0") and not self._is_position_imbalanced(pos, spec_l, spec_s):
                        self.ledger.update_position_status(pid, status="OPEN", ts_s=now_s)
                        self._slice_attempts.pop(pid, None)
                elif err in _OPENING_SLICE_GATE_ERRORS:
                    cnt = self._slice_attempts.get(pid, 0) + 1
                    self._slice_attempts[pid] = cnt
                    if cnt >= self.runtime.maker_max_attempts:
                        if cur_l <= Decimal("0") and cur_s <= Decimal("0"):
                            self.ledger.update_position_status(
                                pid, status="CLOSED", ts_s=now_s,
                                long_base_qty=Decimal("0"), short_base_qty=Decimal("0"),
                                close_reason="entry_maker_timeout",
                            )
                            self._slice_attempts.pop(pid, None)
                        elif cur_l > Decimal("0") and cur_s > Decimal("0") and not self._is_position_imbalanced(pos, spec_l, spec_s):
                            self.ledger.update_position_status(pid, status="OPEN", ts_s=now_s)
                            self._slice_attempts.pop(pid, None)
            elif status == "CLOSING":
                rem = max(cur_l, cur_s)
                if rem <= Decimal("0"):
                    self._finalize_closed_position(pid, now_s=now_s)
                    self._slice_attempts.pop(pid, None)
                    continue
                if self._slice_attempts.get(pid, 0) >= self.runtime.maker_max_attempts:
                    raw_ml = mids.get(iid_l)
                    raw_ms = mids.get(iid_s)
                    if bbo_l is not None:
                        bid_l, mid_l = bbo_l[0], bbo_l[2]
                    elif raw_ml is not None and raw_ml > 0:
                        bid_l = mid_l = Decimal(str(raw_ml))
                    else:
                        continue
                    if bbo_s is not None:
                        ask_s, mid_s = bbo_s[1], bbo_s[2]
                    elif raw_ms is not None and raw_ms > 0:
                        ask_s = mid_s = Decimal(str(raw_ms))
                    else:
                        continue
                    mid = Decimal("0.5") * (mid_l + mid_s)
                    slice_usd = self._closing_slice_target_usd(
                        rem_usd=rem * mid, spec_l=spec_l, spec_s=spec_s, mid=mid
                    )
                    aligned, _ = align_leg_quantities(
                        spec_l,
                        spec_s,
                        target_usd=slice_usd,
                        mid_price=mid,
                        max_mismatch_pct=self.risk.max_qty_mismatch_pct,
                    )
                    if aligned is not None and cur_l > Decimal("0") and cur_s > Decimal("0"):
                        close_l = min(cur_l, aligned.long_base_qty)
                        close_s = min(cur_s, aligned.short_base_qty)
                    else:
                        close_l, close_s = cur_l, cur_s
                    ok, _ = self._submit_dual_taker_close_slice(
                        position_id=pid,
                        pos=pos,
                        spec_l=spec_l,
                        spec_s=spec_s,
                        close_l_base=close_l,
                        close_s_base=close_s,
                        leg_l_bid=bid_l,
                        leg_s_ask=ask_s,
                        now_s=now_s,
                    )
                    if ok:
                        submitted.append(pid)
                    continue
                if bbo_l is None or bbo_s is None:
                    continue
                bid_l, ask_l, mid_l, sp_l = bbo_l
                bid_s, ask_s, mid_s, sp_s = bbo_s
                ok, err = self._submit_next_slice(
                    position_id=pid, is_opening=False, spec_l=spec_l, spec_s=spec_s,
                    leg_l_bid=bid_l, leg_l_ask=ask_l, leg_l_mid=mid_l,
                    leg_s_bid=bid_s, leg_s_ask=ask_s, leg_s_mid=mid_s,
                    remaining_base=rem, sp_l=sp_l, sp_s=sp_s, now_s=now_s,
                )
                if ok:
                    submitted.append(pid)
                elif err in ("min_lot_exceeds_target_usd", "target_below_coarse_step", "alignment_mismatch_exceeds_tolerance"):
                    ok_tc, _ = self._submit_dual_taker_close_slice(
                        position_id=pid,
                        pos=pos,
                        spec_l=spec_l,
                        spec_s=spec_s,
                        close_l_base=cur_l,
                        close_s_base=cur_s,
                        leg_l_bid=bid_l,
                        leg_s_ask=ask_s,
                        now_s=now_s,
                    )
                    if ok_tc:
                        submitted.append(pid)
        return submitted

    def start_sliced_close(
        self,
        position_id: str,
        *,
        specs: Mapping[str, InstrumentSpec],
        quotes: Mapping[str, tuple[float, float]],
        mids: Mapping[str, float],
        reason: str,
        now_s: int,
    ) -> bool:
        """Initiate normal Maker-First + IOC-Hedge sliced exit for carry_decay."""
        if self.ledger.is_reconcile_halted()[0]:
            return False
        pos = self.ledger.active_positions().get(position_id)
        if pos is None:
            return False
        if str(pos["status"]) != "CLOSING":
            self._slice_attempts.pop(position_id, None)
            self.ledger.update_position_status(position_id, status="CLOSING", ts_s=now_s, close_reason=reason)
        return bool(self.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s))

    def poll_active_orders(
        self,
        *,
        live_mids: Mapping[str, float],
        now_s: int,
        live_quotes: Mapping[str, tuple[float, float]] | None = None,
    ) -> list[str]:
        """Poll pending makers, dual-takers, hedges, and emergency closes."""
        timed_out: list[str] = []

        for coid, ctx in list(self._active_makers.items()):
            ack = self._poll_and_update(
                coid,
                venue=ctx.maker_venue,
                instrument_id=ctx.spec_maker.instrument_id,
                now_s=now_s,
            )
            if ack.filled_base_qty > ctx.accounted_maker_base_qty or (
                ack.status in TERMINAL_ORDER_STATUSES and self._maker_unhedged_qty(ctx) > Decimal("0")
            ):
                h_mid, h_bbo = self._hedge_quote_ctx(ctx, live_mids, live_quotes)
                self.on_maker_progress(
                    coid,
                    filled_base_qty=ack.filled_base_qty,
                    terminal=(ack.status in TERMINAL_ORDER_STATUSES),
                    hedge_mid=h_mid,
                    now_s=now_s,
                    hedge_bbo=h_bbo,
                )
            if coid in self._active_makers and ack.status in TERMINAL_ORDER_STATUSES:
                self._active_makers.pop(coid, None)
                self._reconcile_or_finalize_position(ctx.position_id, now_s=now_s, live_mids=live_mids)
                if ctx.cancel_requested and ack.status != "FILLED":
                    self._slice_attempts[ctx.position_id] = self._slice_attempts.get(ctx.position_id, 0) + 1
                    timed_out.append(coid)
                elif ack.status in ("CANCELED", "REJECTED") and ack.filled_base_qty <= Decimal("0"):
                    self._slice_attempts[ctx.position_id] = self._slice_attempts.get(ctx.position_id, 0) + 1
            elif (
                coid in self._active_makers
                and not ctx.cancel_requested
                and not self.ledger.is_reconcile_halted()[0]
                and now_s - ctx.submitted_ts >= self.runtime.maker_requote_secs
            ):
                c_ack = self.gateway.cancel_order(
                    client_order_id=coid,
                    venue=ctx.maker_venue,
                    instrument_id=ctx.spec_maker.instrument_id,
                )
                ctx.cancel_requested = True
                self._update_order_from_ack(coid, c_ack, now_s=now_s)
                if c_ack.filled_base_qty > ctx.accounted_maker_base_qty or (
                    c_ack.status in TERMINAL_ORDER_STATUSES and self._maker_unhedged_qty(ctx) > Decimal("0")
                ):
                    h_mid, h_bbo = self._hedge_quote_ctx(ctx, live_mids, live_quotes)
                    self.on_maker_progress(
                        coid,
                        filled_base_qty=c_ack.filled_base_qty,
                        terminal=(c_ack.status in TERMINAL_ORDER_STATUSES),
                        hedge_mid=h_mid,
                        now_s=now_s,
                        hedge_bbo=h_bbo,
                    )
                if coid in self._active_makers and c_ack.status in TERMINAL_ORDER_STATUSES:
                    self._active_makers.pop(coid, None)
                    self._reconcile_or_finalize_position(ctx.position_id, now_s=now_s, live_mids=live_mids)
                    if c_ack.status != "FILLED":
                        self._slice_attempts[ctx.position_id] = self._slice_attempts.get(ctx.position_id, 0) + 1
                        timed_out.append(coid)

        for batch in list(self._active_dual_takers.values()):
            for leg in batch.active_legs():
                if leg.status not in TERMINAL_ORDER_STATUSES:
                    ack = self._poll_and_update(
                        leg.client_order_id,
                        venue=leg.venue,
                        instrument_id=leg.spec.instrument_id,
                        now_s=now_s,
                    )
                    leg.status = ack.status
                    leg.filled_base_qty = ack.filled_base_qty
                if leg.fallback_coid is not None and leg.fallback_status not in TERMINAL_ORDER_STATUSES:
                    f_ack = self._poll_and_update(
                        leg.fallback_coid,
                        venue=leg.venue,
                        instrument_id=leg.spec.instrument_id,
                        now_s=now_s,
                        fallback_price=leg.ref_price,
                    )
                    leg.fallback_status = f_ack.status
                    leg.fallback_filled_base_qty = f_ack.filled_base_qty
            outcome = self._reconcile_dual_taker_batch(batch, now_s=now_s)
            if outcome == "UNFILLED":
                self._slice_attempts[batch.position_id] = self._slice_attempts.get(batch.position_id, 0) + 1

        for task in list(self._active_hedges.values()):
            if task.status not in TERMINAL_ORDER_STATUSES:
                ack = self._poll_and_update(
                    task.client_order_id,
                    venue=task.venue,
                    instrument_id=task.spec.instrument_id,
                    now_s=now_s,
                    fallback_price=task.ref_price,
                )
                task.status = ack.status
                task.filled_base_qty = ack.filled_base_qty
            if task.fallback_coid is not None and task.fallback_status not in TERMINAL_ORDER_STATUSES:
                f_ack = self._poll_and_update(
                    task.fallback_coid,
                    venue=task.venue,
                    instrument_id=task.spec.instrument_id,
                    now_s=now_s,
                    fallback_price=task.ref_price,
                )
                task.fallback_status = f_ack.status
                task.fallback_filled_base_qty = f_ack.filled_base_qty
            self._reconcile_hedge_order(task, now_s=now_s)

        for close_task in list(self._active_emergency_closes.values()):
            for leg in close_task.legs:
                if leg.status not in TERMINAL_ORDER_STATUSES:
                    ack = self._poll_and_update(
                        leg.client_order_id,
                        venue=leg.venue,
                        instrument_id=leg.spec.instrument_id,
                        now_s=now_s,
                        fallback_price=leg.ref_price,
                    )
                    leg.status = ack.status
                    leg.filled_base_qty = ack.filled_base_qty
            self._reconcile_emergency_close(close_task, now_s=now_s, live_mids=live_mids)

        return timed_out

    def poll_active_makers(
        self,
        *,
        live_mids: Mapping[str, float],
        now_s: int,
        live_quotes: Mapping[str, tuple[float, float]] | None = None,
    ) -> list[str]:
        return self.poll_active_orders(live_mids=live_mids, now_s=now_s, live_quotes=live_quotes)

    def _submit_emergency_close_leg(
        self,
        *,
        position_id: str,
        is_long: bool,
        base_qty: Decimal,
        spec: InstrumentSpec,
        venue: str,
        fb_px: Decimal | None,
        now_s: int,
    ) -> ActiveCloseLeg:
        side = "SELL" if is_long else "BUY"
        role = "EMERGENCY_CLOSE_LONG" if is_long else "EMERGENCY_CLOSE_SHORT"
        prefix = "eml" if is_long else "ems"
        qty_c = max(spec.size_increment, (base_qty / spec.base_step).to_integral_value() * spec.size_increment)
        target_base = qty_c * spec.contract_multiplier
        coid = make_client_order_id(prefix, position_id)
        self.ledger.register_order_intent(
            client_order_id=coid,
            position_id=position_id,
            venue=venue,
            instrument_id=spec.instrument_id,
            side=side,
            role=role,
            order_type="MARKET",
            qty_contracts=qty_c,
            base_qty=base_qty,
            price=None,
            ts_s=now_s,
        )
        ack = self.gateway.submit_market(
            client_order_id=coid,
            venue=venue,
            instrument_id=spec.instrument_id,
            side=side,
            qty_contracts=qty_c,
            reduce_only=True,
        )
        self._update_order_from_ack(coid, ack, now_s=now_s, fallback_price=fb_px)
        return ActiveCloseLeg(
            client_order_id=coid,
            is_long=is_long,
            venue=venue,
            spec=spec,
            ref_price=fb_px,
            target_base_qty=target_base,
            status=ack.status,
            filled_base_qty=ack.filled_base_qty,
        )

    def _reconcile_emergency_close(
        self,
        close_task: ActiveEmergencyClose,
        *,
        now_s: int,
        live_mids: Mapping[str, float] | None = None,
    ) -> None:
        if live_mids is not None:
            if close_task.spec_l is not None and close_task.spec_l.instrument_id in live_mids:
                close_task.ref_price_l = Decimal(str(live_mids[close_task.spec_l.instrument_id]))
            if close_task.spec_s is not None and close_task.spec_s.instrument_id in live_mids:
                close_task.ref_price_s = Decimal(str(live_mids[close_task.spec_s.instrument_id]))

        max_retries = len(_EMERGENCY_CLOSE_RETRY_BACKOFF_SECS)
        for _ in range(4):
            for leg in close_task.legs:
                delta = leg.filled_base_qty - leg.accounted_base_qty
                if delta > 0:
                    self._apply_leg_fill_delta(
                        close_task.position_id,
                        is_long=leg.is_long,
                        is_opening=False,
                        delta_base=delta,
                        now_s=now_s,
                        clamp_dust_step=leg.spec.base_step if leg.status == "FILLED" else None,
                    )
                    leg.accounted_base_qty += delta

            pos = self.ledger.active_positions().get(close_task.position_id)
            if pos is None:
                self._active_emergency_closes.pop(close_task.position_id, None)
                return

            cur_l = Decimal(str(pos["long_base_qty"]))
            cur_s = Decimal(str(pos["short_base_qty"]))
            for leg in close_task.legs:
                if leg.client_order_id in close_task._counted_failed_coids:
                    continue
                if leg.status == "REJECTED" or (leg.status == "CANCELED" and leg.filled_base_qty < leg.target_base_qty):
                    close_task._counted_failed_coids.add(leg.client_order_id)
                    if leg.is_long and cur_l > Decimal("0"):
                        close_task.long_failures += 1
                        if close_task.long_failures <= max_retries:
                            backoff = _EMERGENCY_CLOSE_RETRY_BACKOFF_SECS[close_task.long_failures - 1]
                            close_task.next_retry_long_ts = max(close_task.next_retry_long_ts, now_s + backoff)
                    elif (not leg.is_long) and cur_s > Decimal("0"):
                        close_task.short_failures += 1
                        if close_task.short_failures <= max_retries:
                            backoff = _EMERGENCY_CLOSE_RETRY_BACKOFF_SECS[close_task.short_failures - 1]
                            close_task.next_retry_short_ts = max(close_task.next_retry_short_ts, now_s + backoff)

            in_flight_l = sum(
                (
                    max(Decimal("0"), leg.target_base_qty - leg.accounted_base_qty)
                    for leg in close_task.legs
                    if leg.is_long and leg.status not in TERMINAL_ORDER_STATUSES
                ),
                Decimal("0"),
            )
            in_flight_s = sum(
                (
                    max(Decimal("0"), leg.target_base_qty - leg.accounted_base_qty)
                    for leg in close_task.legs
                    if (not leg.is_long) and leg.status not in TERMINAL_ORDER_STATUSES
                ),
                Decimal("0"),
            )
            ext_l, ext_s = self._external_in_flight_closing_qty(close_task.position_id)
            uncovered_l = cur_l - in_flight_l - ext_l
            uncovered_s = cur_s - in_flight_s - ext_s

            if (uncovered_l > Decimal("0") and close_task.long_failures > max_retries) or (
                uncovered_s > Decimal("0") and close_task.short_failures > max_retries
            ):
                if not close_task.manual_takeover:
                    close_task.manual_takeover = True
                    self.ledger.set_halt(
                        "HALTED_RECONCILE",
                        f"emergency_close_failed:{close_task.position_id}",
                        now_s,
                    )
                    self.ledger.record_event(
                        "emergency_close_manual_takeover",
                        {
                            "position_id": close_task.position_id,
                            "reason": close_task.reason,
                            "remaining_long": str(cur_l),
                            "remaining_short": str(cur_s),
                            "long_failures": close_task.long_failures,
                            "short_failures": close_task.short_failures,
                        },
                        now_s,
                    )
                break

            if close_task.manual_takeover or self.ledger.is_reconcile_halted()[0]:
                break

            submitted_any = False
            if (
                uncovered_l > Decimal("0")
                and close_task.spec_l is not None
                and now_s >= close_task.next_retry_long_ts
                and close_task.long_failures <= max_retries
            ):
                close_task.legs.append(
                    self._submit_emergency_close_leg(
                        position_id=close_task.position_id,
                        is_long=True,
                        base_qty=uncovered_l,
                        spec=close_task.spec_l,
                        venue=close_task.long_venue,
                        fb_px=close_task.ref_price_l,
                        now_s=now_s,
                    )
                )
                submitted_any = True
            if (
                uncovered_s > Decimal("0")
                and close_task.spec_s is not None
                and now_s >= close_task.next_retry_short_ts
                and close_task.short_failures <= max_retries
            ):
                close_task.legs.append(
                    self._submit_emergency_close_leg(
                        position_id=close_task.position_id,
                        is_long=False,
                        base_qty=uncovered_s,
                        spec=close_task.spec_s,
                        venue=close_task.short_venue,
                        fb_px=close_task.ref_price_s,
                        now_s=now_s,
                    )
                )
                submitted_any = True
            if not submitted_any:
                break

        if any(leg.status not in TERMINAL_ORDER_STATUSES for leg in close_task.legs):
            return
        if self._has_in_flight_opening_orders(close_task.position_id) or self._has_external_in_flight_closing_orders(
            close_task.position_id
        ):
            return
        pos_end = self.ledger.active_positions().get(close_task.position_id)
        if pos_end is None:
            self._active_emergency_closes.pop(close_task.position_id, None)
            return
        rem_l = Decimal(str(pos_end["long_base_qty"]))
        rem_s = Decimal(str(pos_end["short_base_qty"]))
        if rem_l <= Decimal("0") and rem_s <= Decimal("0"):
            self._active_emergency_closes.pop(close_task.position_id, None)
            self._maybe_finalize_position(close_task.position_id, now_s=now_s, close_reason=close_task.reason)
        elif close_task.manual_takeover or self.ledger.is_reconcile_halted()[0]:
            self._active_emergency_closes.pop(close_task.position_id, None)

    def emergency_close_position(
        self,
        position_id: str,
        *,
        specs: Mapping[str, InstrumentSpec],
        reason: str,
        now_s: int,
        live_mids: Mapping[str, float] | None = None,
    ) -> None:
        """Immediately flatten both legs with reduce-only market orders and settle when filled."""
        if self.ledger.is_reconcile_halted()[0] or position_id in self._active_emergency_closes:
            return
        pos = self.ledger.active_positions().get(position_id)
        if pos is None:
            return
        for coid, ctx in list(self._active_makers.items()):
            if ctx.position_id != position_id:
                continue
            c_ack = self.gateway.cancel_order(
                client_order_id=coid,
                venue=ctx.maker_venue,
                instrument_id=ctx.spec_maker.instrument_id,
            )
            ctx.cancel_requested = True
            self._update_order_from_ack(coid, c_ack, now_s=now_s)
            if c_ack.filled_base_qty > ctx.accounted_maker_base_qty:
                delta_base = c_ack.filled_base_qty - ctx.accounted_maker_base_qty
                self._apply_leg_fill_delta(
                    position_id,
                    is_long=ctx.maker_side_is_long,
                    is_opening=ctx.is_opening,
                    delta_base=delta_base,
                    now_s=now_s,
                )
                ctx.accounted_maker_base_qty += delta_base
            if c_ack.status in TERMINAL_ORDER_STATUSES:
                self._active_makers.pop(coid, None)

        pos = self.ledger.active_positions().get(position_id)
        if pos is None:
            return
        self.ledger.update_position_status(position_id, status="CLOSING", ts_s=now_s, close_reason=reason)
        spec_l = specs[str(pos["iid_long"])]
        spec_s = specs[str(pos["iid_short"])]
        fb_l = Decimal(str(live_mids[spec_l.instrument_id])) if (live_mids and spec_l.instrument_id in live_mids) else None
        fb_s = Decimal(str(live_mids[spec_s.instrument_id])) if (live_mids and spec_s.instrument_id in live_mids) else None

        close_task = ActiveEmergencyClose(
            position_id=position_id,
            reason=reason,
            legs=[],
            submitted_ts=now_s,
            spec_l=spec_l,
            spec_s=spec_s,
            long_venue=str(pos["long_venue"]),
            short_venue=str(pos["short_venue"]),
            ref_price_l=fb_l,
            ref_price_s=fb_s,
        )
        self._active_emergency_closes[position_id] = close_task
        self._reconcile_emergency_close(close_task, now_s=now_s, live_mids=live_mids)

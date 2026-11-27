"""Sliced Maker-First + IOC-Hedge execution controller and PaperVenueGateway."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
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
    hedged_base_qty: Decimal = Decimal("0")
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
    fallback_filled_base_qty: Decimal = Decimal("0")
    fallback_accounted_base_qty: Decimal = Decimal("0")


@dataclass
class ActiveDualTakerBatch:
    position_id: str
    long_leg: ActiveDualTakerLeg
    short_leg: ActiveDualTakerLeg
    submitted_ts: int
    fallback_submitted: bool = False


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
    fallback_filled_base_qty: Decimal = Decimal("0")
    fallback_accounted_base_qty: Decimal = Decimal("0")


@dataclass
class ActiveCloseLeg:
    client_order_id: str
    is_long: bool
    venue: str
    spec: InstrumentSpec
    ref_price: Decimal | None
    status: str = "OPEN"
    filled_base_qty: Decimal = Decimal("0")
    accounted_base_qty: Decimal = Decimal("0")


@dataclass
class ActiveEmergencyClose:
    position_id: str
    reason: str
    legs: list[ActiveCloseLeg]
    submitted_ts: int


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

    @staticmethod
    def _exec_basis_bps(long_px: Decimal, short_px: Decimal) -> Decimal:
        if long_px <= 0 or short_px <= 0:
            return Decimal("0")
        mid = Decimal("0.5") * (long_px + short_px)
        return ((short_px - long_px) / mid * Decimal("10000")) if mid > 0 else Decimal("0")

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
            for leg in (batch.long_leg, batch.short_leg):
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
            if min(cur_l, cur_s) >= Decimal(str(pos["target_base_qty"])) * Decimal("0.98"):
                self.ledger.update_position_status(position_id, status="OPEN", ts_s=now_s)
                self._slice_attempts.pop(position_id, None)
        elif status == "CLOSING":
            if cur_l <= Decimal("0") and cur_s <= Decimal("0"):
                rpnl = self.ledger.compute_position_price_pnl(position_id)
                self.ledger.update_position_status(
                    position_id,
                    status="CLOSED",
                    ts_s=now_s,
                    realized_pnl_usd=rpnl,
                    close_reason=close_reason,
                )
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
        diff = abs(cur_l - cur_s)
        return (
            diff > max(spec_l.base_step, spec_s.base_step)
            and (diff / hi * Decimal("100")) > self.risk.max_qty_mismatch_pct
        )

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
        halted, halt_reason = self.ledger.is_halted()
        if halted:
            return None, f"halted:{halt_reason}"

        spec_l = specs.get(candidate.long_leg.instrument_id)
        spec_s = specs.get(candidate.short_leg.instrument_id)
        if spec_l is None or spec_s is None:
            return None, "missing_instrument_spec"

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
            self.ledger.update_position_status(pos_id, status="CLOSED", ts_s=ts, close_reason=f"entry_aborted:{slice_err}")
            return None, slice_err
        return pos_id, None

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

        mid = Decimal("0.5") * (leg_l_mid + leg_s_mid)
        slice_target_usd = min(self.risk.slice_usd, remaining_base * mid)
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

        if is_opening and self._exec_basis_bps(est_l, est_s) < self.policy.min_exec_basis_bps:
            return False, "exec_basis_below_min"

        coid = f"mk-{position_id[:12]}-{uuid.uuid4().hex[:8]}"
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
            coid = f"tk-{position_id[:12]}-{uuid.uuid4().hex[:8]}"
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

    def _reconcile_dual_taker_batch(self, batch: ActiveDualTakerBatch, *, now_s: int) -> str:
        for leg in (batch.long_leg, batch.short_leg):
            d = leg.filled_base_qty - leg.accounted_base_qty
            if d > 0:
                self._apply_leg_fill_delta(
                    batch.position_id,
                    is_long=leg.is_long,
                    is_opening=True,
                    delta_base=d,
                    now_s=now_s,
                )
                leg.accounted_base_qty += d
            df = leg.fallback_filled_base_qty - leg.fallback_accounted_base_qty
            if df > 0:
                self._apply_leg_fill_delta(
                    batch.position_id,
                    is_long=leg.is_long,
                    is_opening=True,
                    delta_base=df,
                    now_s=now_s,
                )
                leg.fallback_accounted_base_qty += df

        if (
            batch.long_leg.status not in TERMINAL_ORDER_STATUSES
            or batch.short_leg.status not in TERMINAL_ORDER_STATUSES
        ):
            return "PENDING"

        filled_l = batch.long_leg.filled_base_qty
        filled_s = batch.short_leg.filled_base_qty
        if filled_l <= 0 and filled_s <= 0:
            self._active_dual_takers.pop(batch.position_id, None)
            return "UNFILLED"

        if not batch.fallback_submitted and filled_l != filled_s:
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
                    m_coid = f"tm-{batch.position_id[:12]}-{uuid.uuid4().hex[:8]}"
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
            batch.fallback_submitted = True

        for leg in (batch.long_leg, batch.short_leg):
            if leg.fallback_coid is not None and leg.fallback_status not in TERMINAL_ORDER_STATUSES:
                return "PENDING"

        self._active_dual_takers.pop(batch.position_id, None)
        tot_l = batch.long_leg.filled_base_qty + batch.long_leg.fallback_filled_base_qty
        tot_s = batch.short_leg.filled_base_qty + batch.short_leg.fallback_filled_base_qty
        hi = max(tot_l, tot_s)
        imbalanced = (
            hi > Decimal("0")
            and abs(tot_l - tot_s) > max(batch.long_leg.spec.base_step, batch.short_leg.spec.base_step)
            and (abs(tot_l - tot_s) / hi * Decimal("100")) > self.risk.max_qty_mismatch_pct
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

        self._maybe_finalize_position(batch.position_id, now_s=now_s)
        return "FILLED"

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
        delta_base = filled_base_qty - ctx.hedged_base_qty
        if delta_base <= 0:
            if terminal:
                self._active_makers.pop(client_order_id, None)
                self._maybe_finalize_position(ctx.position_id, now_s=now_s)
            return None
        self._slice_attempts[ctx.position_id] = 0

        # Convert delta_base into nearest aligned hedge contracts
        step_h = ctx.spec_hedge.base_step
        n_h = max(Decimal("1"), (delta_base / step_h).to_integral_value())
        qty_h_contracts = n_h * ctx.spec_hedge.size_increment
        base_h = n_h * step_h

        if self.ledger.active_positions().get(ctx.position_id) is None:
            if terminal:
                self._active_makers.pop(client_order_id, None)
            return None

        self._apply_leg_fill_delta(
            ctx.position_id,
            is_long=ctx.maker_side_is_long,
            is_opening=ctx.is_opening,
            delta_base=delta_base,
            now_s=now_s,
        )
        ctx.hedged_base_qty += delta_base
        if terminal:
            self._active_makers.pop(client_order_id, None)

        # Determine hedge side: opposite directional exposure from maker
        if ctx.is_opening:
            hedge_side = "SELL" if ctx.maker_side_is_long else "BUY"
        else:
            hedge_side = "BUY" if ctx.maker_side_is_long else "SELL"

        slip = self.runtime.hedge_ioc_slippage_bps / Decimal("10000")
        ref_px = (hedge_bbo[1] if hedge_side == "BUY" else hedge_bbo[0]) if hedge_bbo is not None else hedge_mid
        ioc_px = ctx.spec_hedge.round_price(ref_px * (Decimal("1") + slip if hedge_side == "BUY" else Decimal("1") - slip))
        h_coid = f"hg-{ctx.position_id[:12]}-{uuid.uuid4().hex[:8]}"
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

        unfilled_h = task.target_base_qty - task.filled_base_qty
        if unfilled_h > Decimal("0") and task.fallback_coid is None:
            step_h = task.spec.base_step
            rem_contracts = max(task.spec.size_increment, (unfilled_h / step_h).to_integral_value() * task.spec.size_increment)
            m_coid = f"hm-{task.position_id[:12]}-{uuid.uuid4().hex[:8]}"
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
        if total_hedged + task.spec.base_step <= task.target_base_qty:
            self.ledger.increment_unhedged_streak(now_s)
            self.ledger.record_event(
                "hedge_fallback_unhedged",
                {"position_id": task.position_id, "target": str(task.target_base_qty), "filled": str(total_hedged)},
                now_s,
            )
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
        submitted: list[str] = []
        for pid, pos in sorted(self.ledger.active_positions().items()):
            if self.has_in_flight_orders(pid):
                continue
            status = str(pos["status"])
            if status not in ("OPENING", "CLOSING"):
                continue
            iid_l, iid_s = str(pos["iid_long"]), str(pos["iid_short"])
            spec_l, spec_s = specs.get(iid_l), specs.get(iid_s)
            if spec_l is None or spec_s is None:
                continue
            ql = quotes.get(iid_l) or (mids.get(iid_l, 0.0), mids.get(iid_l, 0.0))
            qs = quotes.get(iid_s) or (mids.get(iid_s, 0.0), mids.get(iid_s, 0.0))
            mid_l = Decimal(str(mids.get(iid_l) or pos["entry_l"]))
            mid_s = Decimal(str(mids.get(iid_s) or pos["entry_s"]))
            if mid_l <= 0 or mid_s <= 0:
                continue
            bid_l, ask_l = Decimal(str(ql[0] or mid_l)), Decimal(str(ql[1] or mid_l))
            bid_s, ask_s = Decimal(str(qs[0] or mid_s)), Decimal(str(qs[1] or mid_s))
            sp_l = float((ask_l - bid_l) / mid_l * Decimal("10000")) if ask_l >= bid_l else 0.0
            sp_s = float((ask_s - bid_s) / mid_s * Decimal("10000")) if ask_s >= bid_s else 0.0

            cur_l = Decimal(str(pos["long_base_qty"]))
            cur_s = Decimal(str(pos["short_base_qty"]))
            if status == "OPENING":
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
                    else:
                        self.ledger.update_position_status(pid, status="OPEN", ts_s=now_s)
                    self._slice_attempts.pop(pid, None)
                    continue
                if self._is_position_imbalanced(pos, spec_l, spec_s):
                    continue
                ok, err = self._submit_next_slice(
                    position_id=pid, is_opening=True, spec_l=spec_l, spec_s=spec_s,
                    leg_l_bid=bid_l, leg_l_ask=ask_l, leg_l_mid=mid_l,
                    leg_s_bid=bid_s, leg_s_ask=ask_s, leg_s_mid=mid_s,
                    remaining_base=rem, sp_l=sp_l, sp_s=sp_s, now_s=now_s,
                )
                if ok:
                    submitted.append(pid)
                elif err in ("exec_basis_below_min", "dual_taker_ioc_unfilled"):
                    cnt = self._slice_attempts.get(pid, 0) + 1
                    self._slice_attempts[pid] = cnt
                    if cnt >= self.runtime.maker_max_attempts:
                        if cur_l <= Decimal("0") and cur_s <= Decimal("0"):
                            self.ledger.update_position_status(
                                pid, status="CLOSED", ts_s=now_s,
                                long_base_qty=Decimal("0"), short_base_qty=Decimal("0"),
                                close_reason="entry_maker_timeout",
                            )
                        else:
                            self.ledger.update_position_status(pid, status="OPEN", ts_s=now_s)
                        self._slice_attempts.pop(pid, None)
            elif status == "CLOSING":
                rem = max(cur_l, cur_s)
                if rem <= Decimal("0"):
                    self.ledger.update_position_status(
                        pid,
                        status="CLOSED",
                        ts_s=now_s,
                        realized_pnl_usd=self.ledger.compute_position_price_pnl(pid),
                    )
                    self._slice_attempts.pop(pid, None)
                    continue
                ok, _ = self._submit_next_slice(
                    position_id=pid, is_opening=False, spec_l=spec_l, spec_s=spec_s,
                    leg_l_bid=bid_l, leg_l_ask=ask_l, leg_l_mid=mid_l,
                    leg_s_bid=bid_s, leg_s_ask=ask_s, leg_s_mid=mid_s,
                    remaining_base=rem, sp_l=sp_l, sp_s=sp_s, now_s=now_s,
                )
                if ok:
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
        pos = self.ledger.active_positions().get(position_id)
        if pos is None:
            return False
        if str(pos["status"]) != "CLOSING":
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
            if ack.filled_base_qty > ctx.hedged_base_qty:
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
                self._maybe_finalize_position(ctx.position_id, now_s=now_s)
                if ctx.cancel_requested and ack.status != "FILLED":
                    self._slice_attempts[ctx.position_id] = self._slice_attempts.get(ctx.position_id, 0) + 1
                    timed_out.append(coid)
                elif ack.status in ("CANCELED", "REJECTED") and ack.filled_base_qty <= Decimal("0"):
                    self._slice_attempts[ctx.position_id] = self._slice_attempts.get(ctx.position_id, 0) + 1
            elif (
                coid in self._active_makers
                and not ctx.cancel_requested
                and now_s - ctx.submitted_ts >= self.runtime.maker_requote_secs
            ):
                c_ack = self.gateway.cancel_order(
                    client_order_id=coid,
                    venue=ctx.maker_venue,
                    instrument_id=ctx.spec_maker.instrument_id,
                )
                ctx.cancel_requested = True
                self._update_order_from_ack(coid, c_ack, now_s=now_s)
                if c_ack.filled_base_qty > ctx.hedged_base_qty:
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
                    self._maybe_finalize_position(ctx.position_id, now_s=now_s)
                    if c_ack.status != "FILLED":
                        self._slice_attempts[ctx.position_id] = self._slice_attempts.get(ctx.position_id, 0) + 1
                        timed_out.append(coid)

        for batch in list(self._active_dual_takers.values()):
            for leg in (batch.long_leg, batch.short_leg):
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
            self._reconcile_emergency_close(close_task, now_s=now_s)

        return timed_out

    def poll_active_makers(
        self,
        *,
        live_mids: Mapping[str, float],
        now_s: int,
        live_quotes: Mapping[str, tuple[float, float]] | None = None,
    ) -> list[str]:
        return self.poll_active_orders(live_mids=live_mids, now_s=now_s, live_quotes=live_quotes)

    def _reconcile_emergency_close(self, close_task: ActiveEmergencyClose, *, now_s: int) -> None:
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
        if all(leg.status in TERMINAL_ORDER_STATUSES for leg in close_task.legs):
            self._active_emergency_closes.pop(close_task.position_id, None)
            self._maybe_finalize_position(close_task.position_id, now_s=now_s, close_reason=close_task.reason)

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
        if position_id in self._active_emergency_closes:
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
            if c_ack.filled_base_qty > ctx.hedged_base_qty:
                delta_base = c_ack.filled_base_qty - ctx.hedged_base_qty
                self._apply_leg_fill_delta(
                    position_id,
                    is_long=ctx.maker_side_is_long,
                    is_opening=ctx.is_opening,
                    delta_base=delta_base,
                    now_s=now_s,
                )
                ctx.hedged_base_qty += delta_base
            if c_ack.status in TERMINAL_ORDER_STATUSES:
                self._active_makers.pop(coid, None)

        pos = self.ledger.active_positions().get(position_id)
        if pos is None:
            return
        self.ledger.update_position_status(position_id, status="CLOSING", ts_s=now_s, close_reason=reason)
        l_base = Decimal(str(pos["long_base_qty"]))
        s_base = Decimal(str(pos["short_base_qty"]))
        spec_l = specs[str(pos["iid_long"])]
        spec_s = specs[str(pos["iid_short"])]
        fb_l = Decimal(str(live_mids[spec_l.instrument_id])) if (live_mids and spec_l.instrument_id in live_mids) else None
        fb_s = Decimal(str(live_mids[spec_s.instrument_id])) if (live_mids and spec_s.instrument_id in live_mids) else None

        legs: list[ActiveCloseLeg] = []
        close_specs = (
            (True, l_base, spec_l, str(pos["long_venue"]), "SELL", "EMERGENCY_CLOSE_LONG", "em-l", fb_l),
            (False, s_base, spec_s, str(pos["short_venue"]), "BUY", "EMERGENCY_CLOSE_SHORT", "em-s", fb_s),
        )
        for is_long, base_qty, spec, venue, side, role, prefix, fb_px in close_specs:
            if base_qty <= 0:
                continue
            qty_c = max(spec.size_increment, (base_qty / spec.base_step).to_integral_value() * spec.size_increment)
            coid = f"{prefix}-{position_id[:10]}-{uuid.uuid4().hex[:6]}"
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
            legs.append(
                ActiveCloseLeg(
                    client_order_id=coid,
                    is_long=is_long,
                    venue=venue,
                    spec=spec,
                    ref_price=fb_px,
                    status=ack.status,
                    filled_base_qty=ack.filled_base_qty,
                )
            )

        close_task = ActiveEmergencyClose(
            position_id=position_id,
            reason=reason,
            legs=legs,
            submitted_ts=now_s,
        )
        self._active_emergency_closes[position_id] = close_task
        self._reconcile_emergency_close(close_task, now_s=now_s)

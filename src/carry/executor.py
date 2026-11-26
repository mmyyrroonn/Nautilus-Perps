"""Sliced Maker-First + IOC-Hedge execution controller and PaperVenueGateway."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import time
from typing import Mapping, Protocol
import uuid

from .config import CarryRiskPolicy, CarryRuntimePolicy, DEX_VENUES
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

    def __init__(self, *, fill_maker_immediately: bool = False, maker_partial_ratio: Decimal = Decimal("1")) -> None:
        self.fill_maker_immediately = fill_maker_immediately
        self.maker_partial_ratio = maker_partial_ratio
        self.orders: dict[str, dict] = {}
        self.specs: dict[str, InstrumentSpec] = {}
        self.mids: dict[str, Decimal] = {}

    def register_spec(self, spec: InstrumentSpec, mid: Decimal | None = None) -> None:
        self.specs[spec.instrument_id] = spec
        if mid is not None:
            self.mids[spec.instrument_id] = mid

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
        if ioc or (post_only and self.fill_maker_immediately):
            filled = (base_qty * self.maker_partial_ratio) if post_only else base_qty
            status = "FILLED" if filled >= base_qty else "PARTIAL"
            self.orders[client_order_id] = {
                "venue_order_id": vid, "instrument_id": instrument_id, "base_qty": base_qty,
                "filled_base_qty": filled, "price": price, "status": status,
            }
            return OrderAck(client_order_id, vid, status, filled, price)

        self.orders[client_order_id] = {
            "venue_order_id": vid, "instrument_id": instrument_id, "base_qty": base_qty,
            "filled_base_qty": Decimal("0"), "price": price, "status": "OPEN",
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
        px = self.mids.get(instrument_id, Decimal("1"))
        self.orders[client_order_id] = {
            "venue_order_id": vid, "instrument_id": instrument_id, "base_qty": base_qty,
            "filled_base_qty": base_qty, "price": px, "status": "FILLED",
        }
        return OrderAck(client_order_id, vid, "FILLED", base_qty, px)

    def cancel_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        rec = self.orders[client_order_id]
        if rec["status"] in ("OPEN", "PARTIAL"):
            rec["status"] = "CANCELED"
        return OrderAck(client_order_id, rec["venue_order_id"], rec["status"], rec["filled_base_qty"])

    def poll_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        rec = self.orders[client_order_id]
        return OrderAck(client_order_id, rec["venue_order_id"], rec["status"], rec["filled_base_qty"], rec.get("price"))


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


class CarryExecutionController:
    """Executes sliced Maker-First + IOC-Hedge entries/exits backed by CarryLedger."""

    def __init__(
        self,
        *,
        ledger: CarryLedger,
        gateway: VenueGateway,
        risk: CarryRiskPolicy,
        runtime: CarryRuntimePolicy,
    ) -> None:
        self.ledger = ledger
        self.gateway = gateway
        self.risk = risk
        self.runtime = runtime
        self._active_makers: dict[str, ActiveMakerLeg] = {}

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

        maker_is_long = self._choose_maker_side(str(pos["long_venue"]), str(pos["short_venue"]), sp_l, sp_s)
        if maker_is_long:
            spec_m, spec_h = spec_l, spec_s
            qty_m_contracts = aligned.long_qty_contracts
            base_m = aligned.long_base_qty
            maker_venue, hedge_venue = str(pos["long_venue"]), str(pos["short_venue"])
            maker_side = "BUY" if is_opening else "SELL"
            px = self._anchored_maker_price(hedge_mid=leg_s_mid, maker_bid=leg_l_bid, maker_ask=leg_l_ask, side=maker_side, spec_maker=spec_m)
        else:
            spec_m, spec_h = spec_s, spec_l
            qty_m_contracts = aligned.short_qty_contracts
            base_m = aligned.short_base_qty
            maker_venue, hedge_venue = str(pos["short_venue"]), str(pos["long_venue"])
            maker_side = "SELL" if is_opening else "BUY"
            px = self._anchored_maker_price(hedge_mid=leg_l_mid, maker_bid=leg_s_bid, maker_ask=leg_s_ask, side=maker_side, spec_maker=spec_m)

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
        self.ledger.update_order_state(
            coid, status=ack.status, filled_base_qty=ack.filled_base_qty, venue_order_id=ack.venue_order_id, ts_s=now_s
        )
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
                coid, filled_base_qty=ack.filled_base_qty, terminal=(ack.status == "FILLED"),
                hedge_mid=leg_s_mid if maker_is_long else leg_l_mid, now_s=now_s,
            )
        return True, None

    def on_maker_progress(
        self,
        client_order_id: str,
        *,
        filled_base_qty: Decimal,
        terminal: bool,
        hedge_mid: Decimal,
        now_s: int,
    ) -> OrderAck | None:
        """Immediately hedge any newly filled maker delta on the hedge venue using protected IOC -> Market."""
        ctx = self._active_makers.get(client_order_id)
        if ctx is None:
            return None
        delta_base = filled_base_qty - ctx.hedged_base_qty
        if delta_base <= 0:
            if terminal:
                self._active_makers.pop(client_order_id, None)
            return None

        # Convert delta_base into nearest aligned hedge contracts
        step_h = ctx.spec_hedge.base_step
        n_h = max(Decimal("1"), (delta_base / step_h).to_integral_value())
        qty_h_contracts = n_h * ctx.spec_hedge.size_increment
        base_h = n_h * step_h

        pos = self.ledger.active_positions()[ctx.position_id]
        # Determine hedge side: opposite directional exposure from maker
        if ctx.is_opening:
            hedge_side = "SELL" if ctx.maker_side_is_long else "BUY"
        else:
            hedge_side = "BUY" if ctx.maker_side_is_long else "SELL"

        slip = self.runtime.hedge_ioc_slippage_bps / Decimal("10000")
        ioc_px = ctx.spec_hedge.round_price(hedge_mid * (Decimal("1") + slip if hedge_side == "BUY" else Decimal("1") - slip))
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
        self.ledger.update_order_state(
            h_coid,
            status=ack.status,
            filled_base_qty=ack.filled_base_qty,
            fill_price=ack.avg_fill_price or hedge_mid,
            venue_order_id=ack.venue_order_id,
            ts_s=now_s,
        )
        unfilled_h = base_h - ack.filled_base_qty
        if unfilled_h > Decimal("0") and ack.status != "FILLED":
            rem_contracts = max(ctx.spec_hedge.size_increment, (unfilled_h / step_h).to_integral_value() * ctx.spec_hedge.size_increment)
            m_coid = f"hm-{ctx.position_id[:12]}-{uuid.uuid4().hex[:8]}"
            self.ledger.register_order_intent(
                client_order_id=m_coid,
                position_id=ctx.position_id,
                venue=ctx.hedge_venue,
                instrument_id=ctx.spec_hedge.instrument_id,
                side=hedge_side,
                role="HEDGE_OPEN_MARKET_FALLBACK" if ctx.is_opening else "HEDGE_CLOSE_MARKET_FALLBACK",
                order_type="MARKET",
                qty_contracts=rem_contracts,
                base_qty=rem_contracts * ctx.spec_hedge.contract_multiplier,
                price=None,
                ts_s=now_s,
            )
            m_ack = self.gateway.submit_market(
                client_order_id=m_coid,
                venue=ctx.hedge_venue,
                instrument_id=ctx.spec_hedge.instrument_id,
                side=hedge_side,
                qty_contracts=rem_contracts,
                reduce_only=not ctx.is_opening,
            )
            self.ledger.update_order_state(
                m_coid,
                status=m_ack.status,
                filled_base_qty=m_ack.filled_base_qty,
                fill_price=m_ack.avg_fill_price or hedge_mid,
                venue_order_id=m_ack.venue_order_id,
                ts_s=now_s,
            )
            ack = OrderAck(m_coid, m_ack.venue_order_id, m_ack.status, ack.filled_base_qty + m_ack.filled_base_qty, m_ack.avg_fill_price)

        ctx.hedged_base_qty += delta_base
        cur_l = Decimal(str(pos["long_base_qty"]))
        cur_s = Decimal(str(pos["short_base_qty"]))
        if ctx.is_opening:
            if ctx.maker_side_is_long:
                new_l, new_s = cur_l + delta_base, cur_s + ack.filled_base_qty
            else:
                new_l, new_s = cur_l + ack.filled_base_qty, cur_s + delta_base
            target_b = Decimal(str(pos["target_base_qty"]))
            new_status = "OPEN" if min(new_l, new_s) >= target_b * Decimal("0.98") else "OPENING"
            self.ledger.update_position_status(ctx.position_id, status=new_status, ts_s=now_s, long_base_qty=new_l, short_base_qty=new_s)
        else:
            if ctx.maker_side_is_long:
                new_l, new_s = max(Decimal("0"), cur_l - delta_base), max(Decimal("0"), cur_s - ack.filled_base_qty)
            else:
                new_l, new_s = max(Decimal("0"), cur_l - ack.filled_base_qty), max(Decimal("0"), cur_s - delta_base)
            new_status = "CLOSED" if (new_l <= Decimal("0") and new_s <= Decimal("0")) else "CLOSING"
            rpnl = self.ledger.compute_position_price_pnl(ctx.position_id) if new_status == "CLOSED" else None
            self.ledger.update_position_status(
                ctx.position_id, status=new_status, ts_s=now_s,
                long_base_qty=new_l, short_base_qty=new_s, realized_pnl_usd=rpnl,
            )

        if terminal:
            self._active_makers.pop(client_order_id, None)
        return ack

    def has_active_maker_for_position(self, position_id: str) -> bool:
        return any(ctx.position_id == position_id for ctx in self._active_makers.values())

    def advance_pending_slices(
        self,
        *,
        specs: Mapping[str, InstrumentSpec],
        quotes: Mapping[str, tuple[float, float]],
        mids: Mapping[str, float],
        now_s: int,
    ) -> list[str]:
        """Submit the next $50 slice for OPENING or CLOSING positions that have no resting maker."""
        submitted: list[str] = []
        for pid, pos in sorted(self.ledger.active_positions().items()):
            if self.has_active_maker_for_position(pid):
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
                    continue
                ok, _ = self._submit_next_slice(
                    position_id=pid, is_opening=True, spec_l=spec_l, spec_s=spec_s,
                    leg_l_bid=bid_l, leg_l_ask=ask_l, leg_l_mid=mid_l,
                    leg_s_bid=bid_s, leg_s_ask=ask_s, leg_s_mid=mid_s,
                    remaining_base=rem, sp_l=sp_l, sp_s=sp_s, now_s=now_s,
                )
                if ok:
                    submitted.append(pid)
            elif status == "CLOSING":
                rem = max(cur_l, cur_s)
                if rem <= Decimal("0"):
                    self.ledger.update_position_status(
                        pid,
                        status="CLOSED",
                        ts_s=now_s,
                        realized_pnl_usd=self.ledger.compute_position_price_pnl(pid),
                    )
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
        self.ledger.update_position_status(position_id, status="CLOSING", ts_s=now_s, close_reason=reason)
        return bool(self.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s))

    def poll_active_makers(self, *, live_mids: Mapping[str, float], now_s: int) -> list[str]:
        """Poll pending makers, hedge partial fills, and cancel stale unfilled orders."""
        timed_out: list[str] = []
        for coid, ctx in list(self._active_makers.items()):
            ack = self.gateway.poll_order(client_order_id=coid, venue=ctx.maker_venue, instrument_id=ctx.spec_maker.instrument_id)
            self.ledger.update_order_state(
                coid, status=ack.status, filled_base_qty=ack.filled_base_qty, fill_price=ack.avg_fill_price, ts_s=now_s
            )
            if ack.filled_base_qty > ctx.hedged_base_qty:
                h_mid = Decimal(str(live_mids.get(ctx.spec_hedge.instrument_id, 0) or 1))
                self.on_maker_progress(
                    coid,
                    filled_base_qty=ack.filled_base_qty,
                    terminal=(ack.status in ("FILLED", "CANCELED", "REJECTED")),
                    hedge_mid=h_mid,
                    now_s=now_s,
                )
            if coid in self._active_makers and now_s - ctx.submitted_ts >= self.runtime.maker_requote_secs:
                self.gateway.cancel_order(client_order_id=coid, venue=ctx.maker_venue, instrument_id=ctx.spec_maker.instrument_id)
                self.ledger.update_order_state(coid, status="CANCELED", ts_s=now_s)
                self._active_makers.pop(coid, None)
                timed_out.append(coid)
        return timed_out

    def emergency_close_position(
        self,
        position_id: str,
        *,
        specs: Mapping[str, InstrumentSpec],
        reason: str,
        now_s: int,
        live_mids: Mapping[str, float] | None = None,
    ) -> None:
        """Immediately flatten both legs with reduce-only market orders and mark position CLOSED."""
        pos = self.ledger.active_positions().get(position_id)
        if pos is None:
            return
        for coid, ctx in list(self._active_makers.items()):
            if ctx.position_id == position_id:
                self.gateway.cancel_order(client_order_id=coid, venue=ctx.maker_venue, instrument_id=ctx.spec_maker.instrument_id)
                self.ledger.update_order_state(coid, status="CANCELED", ts_s=now_s)
                self._active_makers.pop(coid, None)

        self.ledger.update_position_status(position_id, status="CLOSING", ts_s=now_s, close_reason=reason)
        l_base = Decimal(str(pos["long_base_qty"]))
        s_base = Decimal(str(pos["short_base_qty"]))
        spec_l = specs[str(pos["iid_long"])]
        spec_s = specs[str(pos["iid_short"])]
        fb_l = Decimal(str(live_mids[spec_l.instrument_id])) if (live_mids and spec_l.instrument_id in live_mids) else None
        fb_s = Decimal(str(live_mids[spec_s.instrument_id])) if (live_mids and spec_s.instrument_id in live_mids) else None

        if l_base > 0:
            qty_l = max(spec_l.size_increment, (l_base / spec_l.base_step).to_integral_value() * spec_l.size_increment)
            coid_l = f"em-l-{position_id[:10]}-{uuid.uuid4().hex[:6]}"
            self.ledger.register_order_intent(
                client_order_id=coid_l, position_id=position_id, venue=str(pos["long_venue"]),
                instrument_id=spec_l.instrument_id, side="SELL", role="EMERGENCY_CLOSE_LONG",
                order_type="MARKET", qty_contracts=qty_l, base_qty=l_base, price=None, ts_s=now_s,
            )
            ack_l = self.gateway.submit_market(
                client_order_id=coid_l, venue=str(pos["long_venue"]), instrument_id=spec_l.instrument_id,
                side="SELL", qty_contracts=qty_l, reduce_only=True,
            )
            self.ledger.update_order_state(
                coid_l,
                status=ack_l.status,
                filled_base_qty=ack_l.filled_base_qty,
                fill_price=ack_l.avg_fill_price or fb_l,
                venue_order_id=ack_l.venue_order_id,
                ts_s=now_s,
            )

        if s_base > 0:
            qty_s = max(spec_s.size_increment, (s_base / spec_s.base_step).to_integral_value() * spec_s.size_increment)
            coid_s = f"em-s-{position_id[:10]}-{uuid.uuid4().hex[:6]}"
            self.ledger.register_order_intent(
                client_order_id=coid_s, position_id=position_id, venue=str(pos["short_venue"]),
                instrument_id=spec_s.instrument_id, side="BUY", role="EMERGENCY_CLOSE_SHORT",
                order_type="MARKET", qty_contracts=qty_s, base_qty=s_base, price=None, ts_s=now_s,
            )
            ack_s = self.gateway.submit_market(
                client_order_id=coid_s, venue=str(pos["short_venue"]), instrument_id=spec_s.instrument_id,
                side="BUY", qty_contracts=qty_s, reduce_only=True,
            )
            self.ledger.update_order_state(
                coid_s,
                status=ack_s.status,
                filled_base_qty=ack_s.filled_base_qty,
                fill_price=ack_s.avg_fill_price or fb_s,
                venue_order_id=ack_s.venue_order_id,
                ts_s=now_s,
            )

        rpnl = self.ledger.compute_position_price_pnl(position_id)
        self.ledger.update_position_status(
            position_id, status="CLOSED", ts_s=now_s,
            long_base_qty=Decimal("0"), short_base_qty=Decimal("0"),
            realized_pnl_usd=rpnl, close_reason=reason,
        )

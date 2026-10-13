"""Finite native roundtrips; quantities follow confirmed fills, never intended exposure."""
from __future__ import annotations

from decimal import Decimal
import time


class RoundTripError(ValueError):
    pass


class RoundTrip:
    """Maker or Market entry followed by a confirmed reduce-only Market exit."""

    def __init__(self, command, max_leg_usd=Decimal("5"), *, clock=time.monotonic):
        self.command = command
        self.max_leg_usd = min(Decimal("5"), Decimal(str(max_leg_usd)))
        self.clock = clock
        self.phase = "entry"
        self.order_id = None
        self.sent_at = None
        self.cancel_sent = False
        self.cancel_reason = None
        self.phase_started_at = None
        self.reprice_seconds = command.get("maker_reprice_seconds", 0)
        self.max_reprices = command.get("maker_max_reprices", 0)
        self.reprices = {"entry": 0, "exit": 0}
        self.phase_quantity = {"entry": Decimal("0"), "exit": Decimal("0")}
        self.phase_notional = {"entry": Decimal("0"), "exit": Decimal("0")}
        self.pending_reprice = False
        self.submitted_quantity = None
        self.recorded_ids = set()
        self.entry_quantity = Decimal("0")
        self.legs = []
        self.done = False
        self.failure = None
        self.evidence_failure = None
        self.stop_requested = False
        self.waiting_for = None
        self.wait_reason = None
        self.wait_started = None
        self.observed_filled_quantity = Decimal("0")
        self.observed_position_quantity = None

    @property
    def order_style(self):
        return self.command["style"] if self.phase == "entry" else "market"

    @property
    def repricing(self):
        return self.order_style == "maker" and self.reprice_seconds > 0

    def _wait(self, stage, reason):
        if self.waiting_for != stage:
            self.wait_started = self.clock()
        self.waiting_for = stage
        self.wait_reason = reason
        if self.clock() - self.wait_started >= 30:
            raise RoundTripError(
                f"reconciliation deadline reached at {stage}: {reason or 'evidence pending'}; "
                "existing exposure requires review"
            )

    def _clear_wait(self):
        self.waiting_for = self.wait_reason = self.wait_started = None

    def _submit(self, adapter, side, quantity):
        quote = adapter.quote(self.command["symbol"])
        if quote is None:
            return
        bid, ask = Decimal(str(quote["bid"])), Decimal(str(quote["ask"]))
        if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
            raise RoundTripError("roundtrip requires a valid fresh bid and ask")
        maker = self.order_style == "maker"
        price = bid if maker and side == "buy" else ask
        # The ask values either side conservatively at submission. A Market fill
        # can move from this estimate; its actual amount remains a separate fact.
        if (not quantity.is_finite() or quantity <= 0
                or self.phase_notional[self.phase] + quantity * ask > self.max_leg_usd):
            raise RoundTripError("roundtrip quantity exceeds the fresh submission valuation limit")
        command = {
            "action": "submit", "symbol": self.command["symbol"], "side": side,
            "quantity": str(quantity), "order_type": "limit" if maker else "market",
            "time_in_force": "GTC" if maker else "IOC", "post_only": maker,
            "reduce_only": side == "sell", "cancel_after_accept": False,
        }
        if maker:
            command["price"] = str(price)
        else:
            command["reference_price"] = str(ask)
        if self.stop_requested:
            raise RoundTripError("roundtrip stopped before its next leg; review current exposure")
        if self.repricing and self._expired():
            return
        adapter.submit(command)
        self.order_id = adapter.order_id
        if self.order_id is None:
            raise RoundTripError("native roundtrip order identity is absent")
        self.sent_at = self.clock()
        if self.phase_started_at is None:
            self.phase_started_at = self.sent_at
        if self.pending_reprice:
            self.reprices[self.phase] += 1
        self.pending_reprice = False
        self.submitted_quantity = quantity
        self.cancel_sent = False
        self.cancel_reason = None
        self._clear_wait()

    def _record(self, adapter, order, side):
        identity = str(order.client_order_id)
        if identity in self.recorded_ids:
            raise RoundTripError("terminal order was already recorded; review current exposure")
        filled = Decimal(str(order.filled_qty))
        fills = [row for row in adapter.events
                 if row["kind"] == "filled" and row["client_order_id"] == str(order.client_order_id)]
        quantity = sum((Decimal(row["quantity"]) for row in fills), Decimal("0"))
        notional = sum((Decimal(row["quantity"]) * Decimal(row["price"]) for row in fills), Decimal("0"))
        economics_valid = (filled.is_finite() and quantity.is_finite() and notional.is_finite()
            and 0 <= filled <= self.submitted_quantity and quantity == filled and notional >= 0
            and all(Decimal(row["quantity"]).is_finite() and Decimal(row["price"]).is_finite()
                    and Decimal(row["quantity"]) > 0 and Decimal(row["price"]) > 0 for row in fills))
        expected = "MAKER" if self.order_style == "maker" else "TAKER"
        verified = (economics_valid and filled > 0 and bool(fills) and all(row.get("liquidity_side") == expected
                    and row.get("commission") is not None for row in fills))
        if filled.is_finite() and filled > 0 and not verified:
            self.evidence_failure = "fill quantity, fee or liquidity evidence did not match the scenario"
        if notional.is_finite() and self.phase_notional[self.phase] + notional > self.max_leg_usd:
            self.evidence_failure = "actual fill notional exceeded the submission valuation limit"
        self.legs.append({
            "side": side, "client_order_id": str(order.client_order_id),
            "order_style": self.order_style,
            "venue_order_id": None if order.venue_order_id is None else str(order.venue_order_id),
            "status": str(order.status), "filled_quantity": str(filled),
            "actual_notional_usdc": str(notional), "terminal_reconciled": True,
            "liquidity_verified": verified, "fills": fills,
            "reprice_index": self.reprices[self.phase], "cancel_reason": self.cancel_reason,
        })
        self.recorded_ids.add(identity)
        if not economics_valid:
            raise RoundTripError("terminal fill economics did not reconcile; review current exposure")
        self.phase_quantity[self.phase] += filled
        self.phase_notional[self.phase] += notional
        return filled

    def _expired(self):
        started_at = self.phase_started_at if self.repricing else self.sent_at
        wait_seconds = self.command["wait_seconds"] if self.phase == "entry" else 10
        return (started_at is not None
                and self.clock() - started_at >= wait_seconds)

    def _remaining(self):
        target = Decimal(self.command["quantity"]) if self.phase == "entry" else self.entry_quantity
        return target - self.phase_quantity[self.phase]

    def _finish_phase(self, adapter):
        if self.phase == "entry":
            filled = self.phase_quantity["entry"]
            if filled == 0:
                raise RoundTripError("entry had no fills; no sell was submitted")
            if Decimal(str(adapter.position_quantity(self.command["symbol"]))) != filled:
                raise RoundTripError("owned position differs from the confirmed entry quantity")
            self.entry_quantity = filled
            self.phase = "exit"
            self.phase_started_at = self.clock()
            self.order_id = None
            self.pending_reprice = False
            return
        remaining = Decimal(str(adapter.position_quantity(self.command["symbol"])))
        if self.phase_quantity["exit"] != self.entry_quantity or remaining != 0:
            raise RoundTripError("exit did not flatten the confirmed position; residual requires review")
        self.done = True
        self.failure = self.evidence_failure

    def advance(self, adapter):
        if self.done or self.failure:
            return
        try:
            symbol = self.command["symbol"]
            if self.order_id is None:
                self.observed_position_quantity = Decimal(str(adapter.position_quantity(symbol)))
                if self.stop_requested:
                    raise RoundTripError("roundtrip stopped before its next leg; review current exposure")
                if self.repricing and self._expired():
                    self._clear_wait()
                    self._finish_phase(adapter)
                    return
                if not adapter.ready():
                    if self.phase == "exit" or self.pending_reprice:
                        self._wait(self.phase + "_admission", adapter.admission.get("reason"))
                    return
                position = Decimal(str(adapter.position_quantity(symbol)))
                self.observed_position_quantity = position
                if self.phase == "entry":
                    if position != self.phase_quantity["entry"]:
                        raise RoundTripError("roundtrip requires a confirmed flat owned starting position")
                    self._submit(adapter, "buy", self._remaining())
                else:
                    if position != self.entry_quantity - self.phase_quantity["exit"]:
                        raise RoundTripError("owned position differs from the confirmed entry quantity")
                    self._submit(adapter, "sell", self._remaining())
                if self.order_id is None and (self.phase == "exit" or self.pending_reprice):
                    self._wait(self.phase + "_admission", "fresh_quote_unavailable")
                return

            order = adapter.owned_order(self.order_id)
            self.observed_filled_quantity = Decimal(str(order.filled_qty))
            self.observed_position_quantity = Decimal(str(adapter.position_quantity(symbol)))
            if not order.is_closed:
                # Only accepted owned orders can be canceled; an uncertain submit
                # remains unresolved and never causes another financial submission.
                reprice_due = (self.repricing
                               and self.clock() - self.sent_at >= self.reprice_seconds)
                if ((self.stop_requested or self._expired() or reprice_due)
                        and not self.cancel_sent
                        and str(order.status) in {"ACCEPTED", "PARTIALLY_FILLED"}):
                    adapter.cancel_order(order.client_order_id)
                    self.cancel_sent = True
                    self.cancel_reason = ("stop" if self.stop_requested else "deadline" if self._expired()
                                          else "reprice" if self.reprices[self.phase] < self.max_reprices
                                          else "reprice_budget")
                if self.cancel_sent:
                    self._wait("cancel_confirmation", f"order_status={order.status}")
                elif self.stop_requested or self._expired():
                    self._wait("order_confirmation", f"order_status={order.status}")
                return
            if not adapter.settled(order):
                diagnostic = getattr(adapter, "settlement_diagnostic", None) or {}
                self._wait("terminal_reconciliation", diagnostic.get("reason", "terminal_evidence_pending"))
                return
            self._clear_wait()
            side = "buy" if self.phase == "entry" else "sell"
            self._record(adapter, order, side)
            if self.stop_requested:
                raise RoundTripError("roundtrip stopped after owned cancellation reconciliation; review current exposure")
            if self.repricing and str(order.status) not in {"FILLED", "CANCELED"}:
                raise RoundTripError("unexpected terminal order state; no maker retry was submitted")
            if (self.repricing and str(order.status) == "CANCELED"
                    and self.cancel_reason == "reprice" and not self._expired()
                    and self.reprices[self.phase] < self.max_reprices and self._remaining() > 0
                    and self.evidence_failure is None):
                expected = (self.phase_quantity["entry"] if self.phase == "entry"
                            else self.entry_quantity - self.phase_quantity["exit"])
                if self.observed_position_quantity != expected:
                    raise RoundTripError("owned position differs from reconciled maker fills")
                self.pending_reprice = True
                self.order_id = None
                return
            self._finish_phase(adapter)
        except RoundTripError as error:
            self.failure = str(error)

    def snapshot(self):
        return {
            "style": self.command["style"], "symbol": self.command["symbol"],
            "entry_style": self.command["style"], "exit_style": "market",
            "phase": self.phase, "done": self.done, "failure": self.failure,
            "entry_filled_quantity": str(self.entry_quantity),
            "active_client_order_id": self.order_id,
            "cancel_requested": self.cancel_sent, "legs": self.legs,
            "cancel_reason": self.cancel_reason,
            "maker_reprice_seconds": self.reprice_seconds, "maker_max_reprices": self.max_reprices,
            "maker_reprices": dict(self.reprices), "pending_reprice": self.pending_reprice,
            "phase_started_at": self.phase_started_at,
            "aggregate_filled_quantity": {key: str(value) for key, value in self.phase_quantity.items()},
            "aggregate_notional_usdc": {key: str(value) for key, value in self.phase_notional.items()},
            "waiting_for": self.waiting_for, "wait_reason": self.wait_reason,
            "observed_filled_quantity": str(self.observed_filled_quantity),
            "observed_position_quantity": (None if self.observed_position_quantity is None
                                           else str(self.observed_position_quantity)),
        }


class ClosePosition(RoundTrip):
    """Resume a confirmed owned entry with a reduce-only Market exit and no new entry."""

    def __init__(self, command, max_leg_usd=Decimal("5"), *, clock=time.monotonic):
        super().__init__(command, max_leg_usd, clock=clock)
        self.phase = "exit"
        self.initialized = False

    def advance(self, adapter):
        if self.done or self.failure:
            return
        if not self.initialized:
            try:
                symbol = self.command["symbol"]
                self.observed_position_quantity = Decimal(str(adapter.position_quantity(symbol)))
                order = adapter.owned_order(self.command["entry_client_order_id"])
                if (str(order.side) != "BUY" or order.is_reduce_only
                        or str(order.instrument_id) != symbol + ".BACKPACK"):
                    raise RoundTripError("close-only requires the original owned entry buy")
                self.observed_filled_quantity = Decimal(str(order.filled_qty))
                if not order.is_closed or not adapter.settled(order):
                    self._wait("terminal_reconciliation", "original_entry_not_reconciled")
                    return
                if not adapter.ready():
                    self._wait("exit_admission", adapter.admission.get("reason"))
                    return
                quantity = self.observed_position_quantity
                if quantity == 0:
                    self.done = True
                    self._clear_wait()
                    return
                if (quantity < 0 or quantity > self.observed_filled_quantity
                        or quantity > Decimal(self.command["max_quantity"])):
                    raise RoundTripError("close-only position exceeds the confirmed entry or requested quantity")
                adapter.check_close_origin(symbol, self.command["entry_client_order_id"])
                self.entry_quantity = quantity
                self.initialized = True
                self._clear_wait()
            except RoundTripError as error:
                self.failure = str(error)
                return
        super().advance(adapter)

    def snapshot(self):
        return {**super().snapshot(), "style": "market", "entry_style": None, "close_only": True,
                "entry_client_order_id": self.command["entry_client_order_id"]}

"""Receipt-driven BTC capability checks; this module never submits an order.

The executor handles returned intents and reports authoritative account snapshots
and venue receipts. Each venue performs connect/read -> passive submit -> modify
-> cancel/read -> protected IOC buy -> reduce-only IOC sell -> final flat read.
Acknowledgements are never fills. Failures keep owned residuals visible and stop
the sequence; unsupported capabilities are blocked, never passed.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation, localcontext
from fractions import Fraction as F


HARD_MAX_NOTIONAL_USD = Decimal("50")


def _decimal(value, name, *, positive=False):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name} must be an exact decimal")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError(f"{name} must be an exact decimal") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        raise ValueError(f"{name} is outside its allowed range")
    return result


def _number(value):
    value = F(value)
    with localcontext() as context:
        context.prec = 60
        return Decimal(value.numerator) / Decimal(value.denominator)


def _ceil(value, step):
    return -(-F(value) // F(step)) * F(step)


@dataclass(frozen=True)
class PreflightMarket:
    venue: str
    instrument_id: str
    size_increment: Decimal
    price_increment: Decimal
    min_quantity: Decimal
    min_notional: Decimal
    multiplier: Decimal = Decimal("1")
    quote_to_usd: Decimal = Decimal("1")
    symbol: str = "BTC"
    supports_modify: bool = True
    supports_reduce_only: bool = True
    supports_post_only: bool = True
    supports_ioc: bool = True
    modify_mode: str = "native"

    def __post_init__(self):
        if self.symbol != "BTC" or not self.venue or not self.instrument_id:
            raise ValueError("preflight requires explicit BTC market identity")
        for name in ("size_increment", "price_increment", "multiplier", "quote_to_usd"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name, positive=True))
        for name in ("min_quantity", "min_notional"):
            object.__setattr__(self, name, _decimal(getattr(self, name), name))
        for name in ("supports_modify", "supports_reduce_only", "supports_post_only", "supports_ioc"):
            if type(getattr(self, name)) is not bool:
                raise ValueError(f"{name} must be boolean")
        if self.modify_mode not in {"native", "cancel_replace", "unsupported"}:
            raise ValueError("modify_mode must be native, cancel_replace or unsupported")


@dataclass(frozen=True)
class AccountSnapshot:
    venue: str
    instrument_id: str
    bid: Decimal
    ask: Decimal
    observed_ns: int
    position_quantity: Decimal
    open_order_ids: tuple[str, ...] = ()
    connected: bool = True
    authoritative: bool = True
    book_observed_ns: int | None = None
    # Complete venue response (HL: the queried DEX), including unconfigured markets.
    # None denotes legacy instrument-only evidence, never complete account coverage.
    account_positions: tuple[tuple[str, Decimal], ...] | None = None
    account_open_orders: tuple[tuple[str, str], ...] | None = None

    def __post_init__(self):
        object.__setattr__(self, "bid", _decimal(self.bid, "bid", positive=True))
        object.__setattr__(self, "ask", _decimal(self.ask, "ask", positive=True))
        value = self.position_quantity
        if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
            raise ValueError("position_quantity must be an exact decimal")
        value = Decimal(value)
        if not value.is_finite():
            raise ValueError("position_quantity must be finite")
        object.__setattr__(self, "position_quantity", value)
        object.__setattr__(self, "open_order_ids", tuple(self.open_order_ids))
        if any(not isinstance(i, str) or not i for i in self.open_order_ids):
            raise ValueError("open orders require explicit client order identities")
        if type(self.observed_ns) is not int or self.observed_ns <= 0:
            raise ValueError("observed_ns must be a positive integer")
        if self.book_observed_ns is None:
            object.__setattr__(self, "book_observed_ns", self.observed_ns)
        if type(self.book_observed_ns) is not int or self.book_observed_ns <= 0:
            raise ValueError("book_observed_ns must be a positive integer")
        if type(self.connected) is not bool or type(self.authoritative) is not bool:
            raise ValueError("account status must be boolean")
        if (self.account_positions is None) != (self.account_open_orders is None):
            raise ValueError("complete account positions and orders must be supplied together")
        if self.account_positions is not None:
            positions, seen = [], set()
            for instrument, quantity in self.account_positions:
                if not isinstance(instrument, str) or not instrument or instrument in seen:
                    raise ValueError("invalid or duplicate account position identity")
                if isinstance(quantity, bool) or not isinstance(quantity, (str, int, Decimal)):
                    raise ValueError("account quantity must be exact")
                quantity = Decimal(quantity)
                if not quantity.is_finite():
                    raise ValueError("account quantity must be finite")
                positions.append((instrument, quantity))
                seen.add(instrument)
            orders = tuple(tuple(row) for row in self.account_open_orders)
            if any(len(row) != 2 or any(not isinstance(v, str) or not v for v in row) for row in orders):
                raise ValueError("invalid account order identity")
            object.__setattr__(self, "account_positions", tuple(positions))
            object.__setattr__(self, "account_open_orders", orders)


@dataclass(frozen=True)
class OrderIntent:
    action: str
    venue: str
    instrument_id: str
    request_id: str
    client_order_id: str = ""
    side: str = ""
    quantity: Decimal = Decimal("0")
    price: Decimal = Decimal("0")
    time_in_force: str = ""
    post_only: bool = False
    reduce_only: bool = False
    # Continuous admission checks report lost authority immediately; the optional
    # BTC capability workflow retains its separate startup waiting semantics.
    account_check: bool = False


@dataclass(frozen=True)
class ExecutionReceipt:
    request_id: str
    client_order_id: str
    kind: str
    execution_id: str = ""
    filled_quantity: Decimal = Decimal("0")


class BtcPreflight:
    """One sequential check per venue, driven exclusively by external evidence.

All quantities are native instrument quantities. ``min_notional`` is in the
instrument quote currency. ``max_notional_usd`` is an operator limit which can
only tighten the USD 50 hard cap. Snapshot positions/open orders cover BTC only.
Fill receipts require globally unique execution IDs and incremental quantities;
the executor must send an IOC ``terminal`` receipt after all fill receipts.
"""

    def __init__(self, markets, *, session_id, max_notional_usd=HARD_MAX_NOTIONAL_USD,
                 slippage_bps=Decimal("20"), timeout_secs=10, max_book_age_ms=2000,
                 max_close_attempts=3, max_account_age_ms=15000):
        self.markets = tuple(markets)
        if not self.markets or len({m.venue for m in self.markets}) != len(self.markets):
            raise ValueError("preflight requires unique venues in explicit order")
        if not isinstance(session_id, str) or not session_id or len(session_id) > 40 or not all(
                c.isalnum() or c in "_-" for c in session_id):
            raise ValueError("session_id must be a short unique alphanumeric identifier")
        self.session_id = session_id
        self.max_notional_usd = min(_decimal(max_notional_usd, "max_notional_usd", positive=True),
                                    HARD_MAX_NOTIONAL_USD)
        self.slippage_bps = _decimal(slippage_bps, "slippage_bps")
        if self.slippage_bps > 20:
            raise ValueError("preflight slippage is capped at 20 bps")
        for name, value in (("timeout_secs", timeout_secs), ("max_book_age_ms", max_book_age_ms),
                            ("max_account_age_ms", max_account_age_ms),
                            ("max_close_attempts", max_close_attempts)):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        self.timeout_ns = timeout_secs * 1_000_000_000
        self.max_book_age_ns = max_book_age_ms * 1_000_000
        self.max_account_age_ns = max_account_age_ms * 1_000_000
        self.max_close_attempts = max_close_attempts
        self.index = 0
        self.stage = "not_started"
        self.result = "running"
        self.reason = None
        self.results = []
        self.snapshot = None
        self.owned_quantity = F(0)
        self.planned_quantity = F(0)
        self.close_attempts = 0
        self.sequence = 0
        self.pending = None
        self.orders = {}
        self.requests = {}
        self.executions = {}
        self.ignored_receipts = 0
        self.failure = False
        self.timings = {}
        self.late_residuals = {}

    @property
    def market(self):
        return self.markets[self.index]

    def _emit(self, action, now_ns, **fields):
        self.sequence += 1
        intent = OrderIntent(action, self.market.venue, self.market.instrument_id,
            f"{self.session_id}-r{self.sequence}", **fields)
        self.requests[intent.request_id] = intent
        self.pending = (intent, now_ns)
        self.timings[intent.request_id] = {"venue": intent.venue, "operation": action,
            "stage": self.stage, "request_id": intent.request_id, "client_order_id": intent.client_order_id,
            "started_ns": now_ns, "completed_ns": None, "elapsed_ms": None,
            "accepted_elapsed_ms": None, "first_fill_elapsed_ms": None, "terminal_elapsed_ms": None}
        return (intent,)

    def _timing(self, request_id, now_ns, kind, *, complete=False):
        row = self.timings.get(request_id)
        if row is None or now_ns < row["started_ns"]:
            return
        elapsed = str(_number(F(now_ns-row["started_ns"], 1_000_000)))
        field = {"accepted": "accepted_elapsed_ms", "fill": "first_fill_elapsed_ms",
                 "terminal": "terminal_elapsed_ms"}.get(kind)
        if field and row[field] is None:
            row[field] = elapsed
        if complete and row["completed_ns"] is None:
            row["completed_ns"], row["elapsed_ms"] = now_ns, elapsed
            row["completion"] = kind

    def _read(self, stage, now_ns, client_order_id=""):
        self.stage = stage
        return self._emit("reconcile", now_ns, client_order_id=client_order_id)

    def _blocked(self, reason, now_ns, *, reconcile=False):
        self.result, self.reason = "blocked", reason
        self.failure = True
        self.stage = "blocked"
        if reconcile:
            client_id = self.pending[0].client_order_id if self.pending else ""
            return self._read("uncertain_reconcile", now_ns, client_id)
        return ()

    def _fail(self, reason, now_ns):
        self.failure, self.reason = True, reason
        return self._read("cleanup_reconcile", now_ns,
                          self.pending[0].client_order_id if self.pending else "")

    def start(self, now_ns):
        if self.stage != "not_started":
            return ()
        for market in self.markets:
            if not all((market.supports_reduce_only, market.supports_post_only, market.supports_ioc)) or (
                    market.modify_mode == "unsupported" or (
                        market.modify_mode == "native" and not market.supports_modify)):
                return self._blocked(f"{market.venue} cannot perform all requested preflight capabilities", now_ns)
        self.stage = "connecting"
        return self._emit("connect", now_ns)

    def _fresh(self, snapshot, now_ns):
        return (snapshot.connected and snapshot.authoritative and snapshot.bid < snapshot.ask
                and 0 < snapshot.observed_ns <= now_ns
                and now_ns-snapshot.observed_ns <= self.max_account_age_ns
                and 0 < snapshot.book_observed_ns <= now_ns
                and now_ns-snapshot.book_observed_ns <= self.max_book_age_ns)

    def _prices(self):
        step = F(self.market.price_increment)
        bid, ask = F(self.snapshot.bid), F(self.snapshot.ask)
        probe = (bid // step)*step-step
        modified = probe-step
        opening = _ceil(ask*(1+F(self.slippage_bps)/10000), step)
        closing = (bid*(1-F(self.slippage_bps)/10000)//step)*step
        return probe, modified, opening, closing

    def _probe(self, now_ns):
        market = self.market
        if not all((market.supports_reduce_only,
                    market.supports_post_only, market.supports_ioc)):
            return self._blocked("required BTC order capability is unsupported", now_ns)
        if market.modify_mode == "unsupported" or (
                market.modify_mode == "native" and not market.supports_modify):
            return self._blocked("BTC price adjustment capability is unsupported", now_ns)
        probe, modified, opening, closing = self._prices()
        if min(probe, modified, closing) <= 0:
            return self._blocked("tick size cannot provide positive protected prices", now_ns)
        multiplier, step = F(market.multiplier), F(market.size_increment)
        # The same minimum-sized clip must meet the passive/modified and IOC minima.
        quantity = _ceil(max(F(market.min_quantity), step,
                            F(market.min_notional)/(min(modified, closing)*multiplier)), step)
        if quantity*multiplier*opening*F(market.quote_to_usd) > F(self.max_notional_usd):
            return self._blocked("current BTC minimum exceeds applied per-order USD cap", now_ns)
        self.planned_quantity = quantity
        return self._submit("probe", quantity, probe, now_ns, post_only=True)

    def _submit(self, role, quantity, price, now_ns, *, post_only=False):
        client_id = f"{self.session_id}-v{self.index}-{role}-{self.sequence+1}"
        self.orders[client_id] = {"role": role, "quantity": F(quantity), "filled": F(0),
                                  "terminal": False, "accepted": False, "venue": self.market.venue}
        self.stage = {"probe": "probe_accept", "replacement": "replacement_accept",
                      "open": "open_terminal", "close": "close_terminal"}[role]
        return self._emit("submit", now_ns, client_order_id=client_id,
            side="SELL" if role == "close" else "BUY", quantity=_number(quantity), price=_number(price),
            time_in_force="GTC" if post_only else "IOC", post_only=post_only,
            reduce_only=role == "close")

    def _open(self, now_ns):
        if not self._fresh(self.snapshot, now_ns):
            return self._read("probe_clean_reconcile", now_ns)
        price = self._prices()[2]
        if self.planned_quantity*F(self.market.multiplier)*price < F(self.market.min_notional):
            return self._blocked("fresh IOC price no longer meets the current minimum notional", now_ns)
        if self.planned_quantity*F(self.market.multiplier)*price*F(self.market.quote_to_usd) > F(self.max_notional_usd):
            return self._blocked("fresh IOC price exceeds applied USD cap", now_ns)
        return self._submit("open", self.planned_quantity, price, now_ns)

    def _close(self, now_ns):
        if self.owned_quantity <= 0:
            return self._read("final_reconcile", now_ns)
        if not self._fresh(self.snapshot, now_ns):
            return self._read("close_reconcile", now_ns)
        if self.close_attempts >= self.max_close_attempts:
            return self._blocked("close attempt limit reached; confirmed residual remains", now_ns)
        quantity = self.owned_quantity
        step = F(self.market.size_increment)
        price = self._prices()[3]
        if quantity % step or quantity < F(self.market.min_quantity) or (
                quantity*F(self.market.multiplier)*price < F(self.market.min_notional)):
            return self._blocked("confirmed residual does not meet current conservative close minima", now_ns)
        if quantity*F(self.market.multiplier)*price*F(self.market.quote_to_usd) > F(self.max_notional_usd):
            return self._blocked("close notional exceeds applied USD cap; residual remains", now_ns)
        self.close_attempts += 1
        return self._submit("close", quantity, price, now_ns)

    def on_account(self, snapshot, now_ns):
        if self.result in {"passed", "failed"} or self.stage == "blocked":
            return ()
        if snapshot.venue != self.market.venue or snapshot.instrument_id != self.market.instrument_id:
            return ()
        if (self.pending and self.pending[0].action == "reconcile"
                and snapshot.observed_ns < self.pending[1]):
            # A delayed reply to an earlier read cannot certify a later cancel,
            # fill or final-flat stage, even when its BBO was refreshed afterward.
            return ()
        if not self._fresh(snapshot, now_ns):
            return self._blocked("account or BBO snapshot is stale, crossed, disconnected or non-authoritative",
                                 now_ns)
        self.snapshot = snapshot
        if self.stage not in {"initial_reconcile", "probe_clean_reconcile", "replacement_reconcile", "close_reconcile",
                              "final_reconcile", "cleanup_reconcile", "uncertain_reconcile"}:
            return ()
        if self.pending:
            self._timing(self.pending[0].request_id, now_ns, "account", complete=True)
        orders = set(snapshot.open_order_ids)
        own_ids = {cid for cid, order in self.orders.items() if not order["terminal"]}
        if orders-own_ids:
            return self._blocked("existing or unrelated BTC open orders; no foreign order may be touched", now_ns)
        if F(snapshot.position_quantity) != self.owned_quantity:
            return self._blocked("authoritative BTC position differs from confirmed owned fills", now_ns)
        if self.stage == "initial_reconcile":
            if orders or snapshot.position_quantity:
                return self._blocked("existing BTC exposure prevents startup preflight", now_ns)
            return self._probe(now_ns)
        if orders:
            if len(orders) != 1:
                return self._blocked("multiple owned open orders require operator reconciliation", now_ns)
            self.failure = True
            self.reason = self.reason or "owned BTC order remained open during reconciliation"
            self.stage = "cleanup_cancel"
            return self._emit("cancel", now_ns, client_order_id=next(iter(orders)))
        if self.stage == "replacement_reconcile" and not self.failure and self.owned_quantity == 0:
            price = self._prices()[1]
            cash = self.planned_quantity*F(self.market.multiplier)*price
            if cash < F(self.market.min_notional) or cash*F(self.market.quote_to_usd) > F(self.max_notional_usd):
                return self._blocked("replacement price violates current minimum or applied USD cap", now_ns)
            return self._submit("replacement", self.planned_quantity, self._prices()[1], now_ns, post_only=True)
        if self.stage == "probe_clean_reconcile" and not self.failure and self.owned_quantity == 0:
            return self._open(now_ns)
        if self.owned_quantity > 0:
            return self._close(now_ns)
        if self.stage == "uncertain_reconcile":
            # A flat snapshot cannot establish an unknown submit's terminal state.
            unresolved = [cid for cid, order in self.orders.items() if not order["terminal"]]
            if unresolved:
                return self._blocked("unknown submission requires authoritative order-terminal evidence", now_ns)
        if self.failure:
            self.result, self.stage = "failed", "finished"
            self.results.append({"venue": self.market.venue, "result": "failed", "reason": self.reason})
            return ()
        if self.stage != "final_reconcile":
            return self._blocked("unexpected account snapshot cannot certify a completed check", now_ns)
        self.results.append({"venue": self.market.venue, "result": "passed", "reason": None})
        if self.index+1 == len(self.markets):
            self.result, self.stage = "passed", "finished"
            self.pending = None
            return ()
        self.index += 1
        self.snapshot = None
        self.planned_quantity = F(0)
        self.close_attempts = 0
        self.stage = "connecting"
        return self._emit("connect", now_ns)

    def on_receipt(self, receipt, now_ns):
        intent = self.requests.get(receipt.request_id)
        if intent is None or receipt.client_order_id != intent.client_order_id:
            self.ignored_receipts += 1
            return ()
        if intent.venue != self.market.venue and receipt.kind != "fill":
            self.ignored_receipts += 1
            return ()
        order = self.orders.get(receipt.client_order_id)
        if receipt.kind == "fill":
            if order is None or not receipt.execution_id:
                return self._blocked("fill lacks owned identity or unique execution ID", now_ns, reconcile=True)
            quantity = F(_decimal(receipt.filled_quantity, "filled_quantity", positive=True))
            identity = (receipt.client_order_id, quantity)
            previous = self.executions.get(receipt.execution_id)
            if previous is not None:
                if previous != identity:
                    return self._blocked("conflicting duplicate execution ID", now_ns, reconcile=True)
                return ()
            if order["filled"]+quantity > order["quantity"]:
                return self._blocked("fill quantity exceeds owned order quantity", now_ns, reconcile=True)
            self.executions[receipt.execution_id] = identity
            order["filled"] += quantity
            if intent.venue != self.market.venue:
                sign = -1 if order["role"] == "close" else 1
                self.late_residuals[intent.venue] = self.late_residuals.get(intent.venue, F(0))+sign*quantity
                return self._blocked("late fill on previously completed venue invalidates preflight flat evidence",
                                     now_ns, reconcile=True)
            self.owned_quantity += -quantity if order["role"] == "close" else quantity
            if self.owned_quantity < 0:
                return self._blocked("reduce-only fill exceeded owned position", now_ns, reconcile=True)
            self._timing(receipt.request_id, now_ns, "fill")
            if order["role"] in {"probe", "replacement"}:
                self.failure, self.reason = True, "passive capability probe unexpectedly filled"
            if order["terminal"]:
                return self._blocked("late fill arrived after terminal evidence; reconcile owned residual", now_ns,
                                     reconcile=True)
            return ()
        if self.result in {"passed", "failed"} or self.stage == "blocked":
            return ()
        if receipt.kind in {"unknown", "disconnected"}:
            self._timing(receipt.request_id, now_ns, receipt.kind, complete=True)
            return self._blocked(f"{receipt.kind} receipt; preserve client order identity for reconciliation",
                                 now_ns, reconcile=True)
        if receipt.kind == "rejected":
            self._timing(receipt.request_id, now_ns, "rejected", complete=True)
            if order and intent.action == "submit":
                order["terminal"] = True
            return self._fail("venue rejected a required capability operation", now_ns)
        if receipt.kind in {"terminal", "canceled"} and order:
            order["terminal"] = True
        if self.pending is None or receipt.request_id != self.pending[0].request_id:
            self.ignored_receipts += 1
            return ()
        if self.stage == "connecting" and receipt.kind == "connected":
            self._timing(receipt.request_id, now_ns, "connected", complete=True)
            return self._read("initial_reconcile", now_ns)
        if self.stage == "probe_accept" and receipt.kind == "accepted":
            self._timing(receipt.request_id, now_ns, "accepted", complete=True)
            order["accepted"] = True
            if order["terminal"]:
                return self._fail("acceptance arrived after terminal probe evidence", now_ns)
            if self.failure:
                self.stage = "cleanup_cancel"
                return self._emit("cancel", now_ns, client_order_id=receipt.client_order_id)
            if not self._fresh(self.snapshot, now_ns):
                return self._fail("probe acknowledgement arrived after BBO freshness deadline", now_ns)
            if self.market.modify_mode == "cancel_replace":
                self.stage = "replacement_cancel"
                return self._emit("cancel", now_ns, client_order_id=receipt.client_order_id)
            self.stage = "probe_modify"
            return self._emit("modify", now_ns, client_order_id=receipt.client_order_id,
                side="BUY", quantity=_number(order["quantity"]), price=_number(self._prices()[1]),
                time_in_force="GTC", post_only=True)
        if self.stage == "probe_modify" and receipt.kind == "updated":
            self._timing(receipt.request_id, now_ns, "updated", complete=True)
            self.stage = "probe_cancel"
            return self._emit("cancel", now_ns, client_order_id=receipt.client_order_id)
        if self.stage == "replacement_cancel" and receipt.kind == "canceled":
            self._timing(receipt.request_id, now_ns, "canceled", complete=True)
            return self._read("cleanup_reconcile" if self.failure else "replacement_reconcile", now_ns,
                              receipt.client_order_id)
        if self.stage == "replacement_accept" and receipt.kind == "accepted":
            self._timing(receipt.request_id, now_ns, "accepted", complete=True)
            order["accepted"] = True
            self.stage = "cleanup_cancel" if self.failure else "probe_cancel"
            return self._emit("cancel", now_ns, client_order_id=receipt.client_order_id)
        if self.stage in {"probe_cancel", "cleanup_cancel"} and receipt.kind == "canceled":
            self._timing(receipt.request_id, now_ns, "canceled", complete=True)
            return self._read("cleanup_reconcile" if self.failure else "probe_clean_reconcile", now_ns,
                              receipt.client_order_id)
        if self.stage in {"open_terminal", "close_terminal"} and receipt.kind == "accepted":
            self._timing(receipt.request_id, now_ns, "accepted")
            order["accepted"] = True
            return ()
        if self.stage in {"open_terminal", "close_terminal"} and receipt.kind == "terminal":
            self._timing(receipt.request_id, now_ns, "terminal", complete=True)
            if order["filled"] != order["quantity"]:
                self.failure, self.reason = True, "IOC did not fully fill its requested quantity"
            if order["role"] == "open":
                return self._read("close_reconcile" if self.owned_quantity else "final_reconcile", now_ns,
                                  receipt.client_order_id)
            return self._read("close_reconcile" if self.owned_quantity else "final_reconcile", now_ns,
                              receipt.client_order_id)
        self.ignored_receipts += 1
        return ()

    def on_timer(self, now_ns):
        if self.result in {"passed", "failed"} or self.stage == "blocked" or self.pending is None:
            return ()
        if now_ns-self.pending[1] < self.timeout_ns:
            return ()
        if self.stage == "uncertain_reconcile":
            return self._blocked("reconciliation timed out; owned/unknown state remains unresolved", now_ns)
        return self._blocked("operation timed out; submission is not assumed absent", now_ns, reconcile=True)

    def document(self):
        unresolved = [cid for cid, order in self.orders.items() if not order["terminal"]]
        return {"result": self.result, "stage": self.stage, "reason": self.reason,
                "venue": self.market.venue, "owned_quantity": str(_number(self.owned_quantity)),
                "planned_quantity": str(_number(self.planned_quantity)),
                "pending_request_id": self.pending[0].request_id if self.pending else None,
                "unresolved_client_order_ids": unresolved, "results": list(self.results),
                "ignored_receipts": self.ignored_receipts,
                "max_notional_usd": str(self.max_notional_usd),
                "adjust_modes": {m.venue: m.modify_mode for m in self.markets},
                "late_residuals_by_venue": {venue: str(_number(quantity))
                    for venue, quantity in self.late_residuals.items()},
                "operation_timings": [dict(row) for row in self.timings.values()],
                "session_id": self.session_id,
                "order_journal": {cid: {k: str(_number(v)) if isinstance(v, F) else v
                    for k, v in order.items()} for cid, order in self.orders.items()},
                "intent_journal": {request_id: {k: str(v) if isinstance(v, Decimal) else v
                    for k, v in asdict(intent).items()} for request_id, intent in self.requests.items()},
                "execution_journal": {eid: {"client_order_id": value[0], "quantity": str(_number(value[1]))}
                    for eid, value in self.executions.items()}}

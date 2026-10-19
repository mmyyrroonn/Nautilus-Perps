"""Fill-driven pair ledger and order intents for the multi-venue live runtime.

No network or native order submission occurs here. Intents are durable before
dispatch, acknowledgements never create inventory, and account evidence fences
every instrument between orders. Pair inventory is separate from venue netting.
"""
from __future__ import annotations

from opportunity_timing import record_latency, span, start_latency, timed

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction as F
from itertools import combinations
import sys
import uuid

from opportunity_core import (
    _book_valid, _execution_metadata_valid, _fee_metadata_valid, _number, exact_decimal,
)
from opportunity_paper import PaperTradeState
from opportunity_preflight import OrderIntent
from opportunity_runtime import ScanState


HARD_MAX_ORDER_USD = Decimal("100")
HARD_MAX_GROSS_USD = Decimal("500")
HARD_MAX_PAIRS = 16
HARD_MAX_UNHEDGED_NOTIONAL_USD = Decimal("100")
HARD_MAX_UNHEDGED_SECS = 10
MAX_PENDING_ENTRY_TIMINGS = 1024
_ZERO = F(0)
TERMINALS = {"terminal", "canceled", "rejected", "denied", "expired"}
HARD_EXITS = {"max_hold", "max_loss", "entry_imbalance", "exit_imbalance", "operator_stop", "execution_halt", "exposure_limit",
              "unhedged_notional", "unhedged_timeout"}


def _positive(value, name):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ValueError(f"{name} requires an exact decimal")
    value = Decimal(value)
    if not value.is_finite() or value <= 0:
        raise ValueError(f"{name} must be positive and finite")
    return value


class LiveTradeState(ScanState):
    def __init__(self, plan, policy, *, max_pairs, max_gross_notional_usd,
                 store=None, event_sink=None, account_max_age_ms=5000,
                 order_timeout_secs=10, retry_interval_ms=1000, trade_symbols=None,
                 max_unhedged_notional_usd=HARD_MAX_UNHEDGED_NOTIONAL_USD,
                 max_unhedged_secs=HARD_MAX_UNHEDGED_SECS):
        if type(max_pairs) is not int or not 1 <= max_pairs <= HARD_MAX_PAIRS:
            raise ValueError("max_pairs is outside the live hard cap")
        gross = _positive(max_gross_notional_usd, "max_gross_notional_usd")
        if gross > HARD_MAX_GROSS_USD or policy.order_notional_usd > HARD_MAX_ORDER_USD:
            raise ValueError("live risk exceeds the operator-authorized hard cap")
        unhedged = _positive(max_unhedged_notional_usd, "max_unhedged_notional_usd")
        if (unhedged > HARD_MAX_UNHEDGED_NOTIONAL_USD or type(max_unhedged_secs) is not int
                or not 1 <= max_unhedged_secs <= HARD_MAX_UNHEDGED_SECS):
            raise ValueError("unhedged risk exceeds the operator-authorized hard cap")
        for value, name in ((account_max_age_ms, "account_max_age_ms"),
                            (order_timeout_secs, "order_timeout_secs"),
                            (retry_interval_ms, "retry_interval_ms")):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        plan = replace(plan, output_path=None, settings=replace(plan.settings,
            target_notional=policy.order_notional_usd, min_entry_edge_bps=policy.entry_edge_bps))
        super().__init__(plan)
        self.policy, self.max_pairs, self.max_gross = policy, max_pairs, F(gross)
        self.account_max_age_ns = account_max_age_ms * 1_000_000
        self.order_timeout_ns = order_timeout_secs * 1_000_000_000
        self.max_unhedged_notional = F(unhedged)
        self.max_unhedged_ns = max_unhedged_secs * 1_000_000_000
        self.retry_ns = retry_interval_ms * 1_000_000
        self.trade_symbols = frozenset(trade_symbols if trade_symbols is not None else self.groups)
        if not self.trade_symbols <= self.groups.keys():
            raise ValueError("unconfigured live symbol")
        # Use only pricing methods; never call the paper lifecycle or its fills.
        self.pricer = PaperTradeState(plan, policy)
        self.pricer.books, self.pricer.metadata = self.books, self.metadata
        self.store, self.event_sink = store, event_sink
        self.positions, self.orders, self.executions, self.last_closed = {}, {}, {}, {}
        self.instruments, self.accounts, self.account_requests = {}, {}, {}
        self.account_request_ids, self.account_last_reads, self.account_pauses = {}, {}, {}
        # The runner binds exactly one locked account per venue. Every configured
        # market on that venue shares its admission and recovery domain.
        self.account_domains = {}
        for market in self.markets.values():
            self.account_domains.setdefault(self._account_domain(market.venue), []).append(market.instrument_id)
        self.account_scopes = {}
        for instrument in self.markets:
            self.account_scopes.setdefault(self._account_scope(instrument), []).append(instrument)
        self.account_representatives = {scope: self._account_representative(instruments)
                                        for scope, instruments in self.account_scopes.items()}
        self.account_check_metrics = {}
        # One immutable payload per configured account scope, never an admission
        # result. Freshness, fences, inventory and order ownership stay dynamic.
        self.account_position_values = {}
        self.flat_account_problems = {}
        self.account_read_failures, self.account_retry_after_ns = {}, {}
        self.fences, self.last_attempt = {}, {}
        self.outbox = []
        self.intent_notifier = None
        self.entry_timings = {}  # Process-local only; never persisted or restored.
        self.session_id = uuid.uuid4().hex[:16]
        self.sequence = self.request_sequence = self.completed = 0
        self.realized_net = F(0)
        self.pending_economics = {}
        self.legacy_realized_net = None
        self.event_counts = Counter()
        self.phase, self.halt_reason = "waiting_preflight", None
        self.cleanup_allowed = True
        self.stopping = False
        self.entry_pause_reason = None
        self.full_evaluation_symbols = set(self.trade_symbols)
        saved = store.load() if store else None
        if saved:
            self._restore(saved)
        if store:
            store.save(self.snapshot(), [])

    def _restore(self, saved):
        if saved.get("schema_version") not in {1, 2} or saved.get("mode") != "live":
            raise ValueError("unknown live ledger version")
        self.session_id = saved["session_id"]
        self.sequence, self.request_sequence = saved["sequence"], saved["request_sequence"]
        self.completed = saved["completed"]
        if saved["schema_version"] == 2:
            self.realized_net = F(saved["realized_net_usd"])
            self.pending_economics = deepcopy(saved["pending_economics"])
            self.legacy_realized_net = saved.get("legacy_realized_net_usd")
        elif self.completed:
            # V1 aggregates can include estimated fees and have no per-pair
            # confirmation proof. Preserve them separately, never promote them.
            self.legacy_realized_net = saved["realized_net_usd"]
        self.event_counts.update(saved["event_counts"])
        self.orders, self.executions = deepcopy(saved["orders"]), deepcopy(saved["executions"])
        self.fences = dict(saved["fences"])
        self.account_pauses = deepcopy(saved.get("account_pauses", {}))
        self.last_closed = {tuple(row["pair"]): row["closed_ns"] for row in saved["last_closed"]}
        for position in deepcopy(saved["positions"]):
            key = self.pair_key(position["buy_id"], position["sell_id"])
            if key in self.positions or list(key) != position["pair"]:
                raise ValueError("duplicate or invalid live pair ownership")
            if any(F(position[f"remaining_{s}_base"]) < 0 for s in ("buy", "sell")):
                raise ValueError("negative live owned quantity")
            if saved["schema_version"] == 1 and position["unknown_commission"]:
                position["legacy_fee_accounting"] = True
            position.setdefault("pending_commissions", {})
            # Legacy ledgers must not receive a new grace period on restart.
            delta = F(position["remaining_buy_base"]) - F(position["remaining_sell_base"])
            position.setdefault("unhedged_since_ns", position["opened_ns"] if delta else None)
            position.setdefault("unhedged_base", _number(delta))
            self.positions[key] = position
        # Process-local account evidence and pending dispatches are never replayed.
        if any(o["terminal_ns"] is None for o in self.orders.values()):
            self.phase = "recovery_blocked"
            self.halt_reason = "unresolved persisted order IDs require execution reconciliation"
        else:
            self.phase = "waiting_preflight"
        if saved.get("halt_reason"):
            self.halt_reason = saved["halt_reason"]
            self.phase = "recovery_blocked"

    def pair_key(self, buy_id, sell_id):
        return self.pricer.pair_key(buy_id, sell_id)

    def register_instrument(self, instrument_id, price_increment, native_multiplier, quote_currency):
        if instrument_id not in self.markets or not quote_currency:
            raise ValueError("invalid native instrument identity")
        value = {"price_increment": _positive(price_increment, "price_increment"),
                 "native_multiplier": _positive(native_multiplier, "native_multiplier"),
                 "quote_currency": str(quote_currency)}
        old = self.instruments.get(instrument_id)
        if old and old != value and any(instrument_id in (p["buy_id"], p["sell_id"])
                                      for p in self.positions.values()):
            self.halt_reason = "native instrument economics changed with owned inventory"
        self.instruments[instrument_id] = value
        self.full_evaluation_symbols.update(self.trade_symbols)

    @timed("strategy.snapshot")
    def snapshot(self):
        return deepcopy({"schema_version": 2, "mode": "live", "session_id": self.session_id,
            "sequence": self.sequence, "request_sequence": self.request_sequence,
            "completed": self.completed, "realized_net_usd": _number(self.realized_net),
            "pending_economics": self.pending_economics,
            "legacy_realized_net_usd": self.legacy_realized_net,
            "event_counts": dict(self.event_counts), "positions": list(self.positions.values()),
            "orders": self.orders, "executions": self.executions, "fences": self.fences,
            "last_closed": [{"pair": list(k), "closed_ns": v} for k, v in self.last_closed.items()],
            "phase": self.phase, "halt_reason": self.halt_reason,
            "account_pauses": self.account_pauses, "entry_pause_reason": self.entry_pause_reason})

    def emit(self, kind, now_ns, **fields):
        # Account, order, ownership and lifecycle transitions can reopen admission
        # for an unchanged pair, including a pair on another symbol.
        self.full_evaluation_symbols.update(self.trade_symbols)
        event = deepcopy({"event": kind, "mode": "live", "observed_ns": now_ns, **fields})
        self.event_counts[kind] += 1
        try:
            if self.store:
                self.store.save(self.snapshot(), [event])
            if self.event_sink:
                self.event_sink(event)
        except Exception:
            self.halt_reason = "ledger persistence failed"
            self.cleanup_allowed = False
            self.outbox.clear()
            self.entry_timings.clear()
            raise

    def activate(self, now_ns):
        if self.halt_reason or self.phase == "recovery_blocked":
            return False
        self.phase = "active"
        self.emit("strategy_enabled", now_ns)
        return True

    def set_entry_pause(self, reason, now_ns):
        """Pause new pairs without changing any existing pair's exit policy."""
        if self.entry_pause_reason != reason:
            self.entry_pause_reason = reason
            self.emit("entry_pause_changed", now_ns, reason=reason)

    def flat_accounts_ready(self, now_ns, after_ns):
        """Collect complete proofs for one flat check, retaining completed reads.

        A proof begun after this check's boundary remains usable while the
        other venues finish. Own activity and invalid account observations
        still invalidate it; elapsed time alone does not restart the check.
        """
        representatives = [self._account_representative(instruments) for instruments in self.account_scopes.values()]
        self.flat_account_problems = {}
        ready = not self.positions and not any(o["terminal_ns"] is None for o in self.orders.values())
        for instrument in representatives:
            account = self.accounts.get(instrument)
            reason = self._account_problem(instrument, now_ns, check_age=False)
            valid = (account is not None and account.observed_ns >= after_ns
                     and reason is None
                     and account.account_positions is not None
                     and not any(q for _, q in account.account_positions)
                     and not account.account_open_orders and not account.open_order_ids
                     and not account.position_quantity)
            if not valid:
                ready = False
                domain, scope = self._account_scope(instrument)
                self.flat_account_problems[domain + (":" + scope if scope else "")] = (
                    reason or ("before_check" if account.observed_ns < after_ns else "account_not_flat_or_incomplete"))
                self._request_account(instrument, now_ns)
        return ready

    def _request_id(self, action):
        self.request_sequence += 1
        return f"L{self.session_id}{self.request_sequence:010x}{action}"

    def drain_intents(self):
        intents = tuple(self.outbox)
        self.outbox.clear()
        released = []
        for intent in intents:
            order = self.orders.get(intent.client_order_id)
            if intent.action == "submit" and order is not None and order["status"] == "prepared":
                order["status"] = "dispatch_released"
                released.append(intent.client_order_id)
        if released:
            with span("strategy.persist_dispatch_release"):
                self.emit("dispatch_released", max(self.orders[cid]["sent_ns"] for cid in released),
                          client_order_ids=released)
        return intents

    def take_entry_timing(self, intent):
        return self.entry_timings.pop(intent.request_id, None)

    def expected_native(self, instrument_id):
        result = _ZERO
        for position in self.positions.values():
            for leg, sign in (("buy", 1), ("sell", -1)):
                if position[f"{leg}_id"] == instrument_id:
                    multiplier = F(position["entry"][f"{leg}_metadata"]["multiplier"])
                    result += sign * F(position[f"remaining_{leg}_base"]) / multiplier
        return result

    def discard_paused_entry(self, intent, now_ns):
        """Called by the coordinator immediately before each unsent dispatch.

        An earlier dispatch in the same drained batch can synchronously report
        an account failure. Both entry legs share the pair's admission domains;
        already-sent IDs and all reductions retain their original lifecycle.
        """
        order = self.orders.get(intent.client_order_id)
        if (intent.action != "submit" or order is None or order["purpose"] != "entry"
                or order["status"] != "dispatch_released"):
            return False
        position = self._position(order["position_id"])
        domains = {self._account_domain(self.markets[position[f"{leg}_id"]].venue)
                   for leg in ("buy", "sell")}
        paused = sorted(domains & self.account_pauses.keys())
        if not paused:
            return False
        order["terminal_ns"], order["status"] = now_ns, "local_not_dispatched"
        self.take_entry_timing(intent)
        instrument_id = order["instrument_id"]
        self.fences[instrument_id] = max(self.fences.get(instrument_id, 0), now_ns)
        self.emit("entry_dispatch_blocked", now_ns, client_order_id=intent.client_order_id, domains=paused)
        return True

    def gross_exposure(self, *, reserve=False):
        total = F(0)
        for position in self.positions.values():
            value = F(0)
            for leg in ("buy", "sell"):
                instrument_id = position[f"{leg}_id"]
                quantity = F(position[f"remaining_{leg}_base"])
                book = self.books.get(instrument_id)
                if book and book.asks:
                    value += quantity * F(book.asks[0][0])
                elif F(position[f"entry_{leg}_base"]) > 0:
                    value += quantity * F(position["actual_entry"][f"{leg}_notional_usd"]) / F(position[f"entry_{leg}_base"])
            # An unresolved entry may still fill its entire persisted limit.
            for cid in position["entry_ids"]:
                order = self.orders[cid]
                if order["terminal_ns"] is None:
                    market = self.markets[order["instrument_id"]]
                    multiplier = F(position["entry"][f"{order['leg']}_metadata"]["multiplier"]) / F(market.canonical_multiplier)
                    value += (F(order["quantity"]) - F(order["filled_quantity"])) * F(order["price"]) * multiplier * F(market.quote_to_usd)
            total += max(value, F(position["reserved_gross_usd"])) if reserve else value
        return total

    def _pending(self, instrument_id):
        return [o for o in self.orders.values() if o["instrument_id"] == instrument_id
                and o["terminal_ns"] is None]

    def _request_account(self, instrument_id, now_ns):
        scope = self._account_scope(instrument_id)
        if now_ns < self.account_retry_after_ns.get(scope, 0):
            return
        instruments = self.account_scopes[scope]
        observations = (self.accounts[i] for i in instruments if i in self.accounts)
        first = next(observations, None)
        # Native readers return the entire account (HL: one DEX), not one HTTP
        # query per market. Legacy instrument-only readers retain their scope.
        if (first is None or first.account_positions is not None
                or any(a.account_positions is not None for a in observations)):
            instrument_id = self.account_representatives[scope]
        # The gateway owns the account-read deadline, which can exceed the
        # order timeout. Do not replace a still-pending request with a new ID.
        if instrument_id in self.account_request_ids:
            return
        if now_ns - self.account_last_reads.get(instrument_id, 0) < self.retry_ns:
            return
        self.account_requests[instrument_id] = now_ns
        self.account_last_reads[instrument_id] = now_ns
        request = OrderIntent("reconcile", self.markets[instrument_id].venue, instrument_id,
                              self._request_id("R"), account_check=True)
        self.account_request_ids[instrument_id] = request.request_id
        self.emit("account_read_requested", now_ns, instrument_id=instrument_id, request_id=request.request_id)
        self.outbox.append(request)

    @staticmethod
    def _account_domain(venue):
        return "HL" if venue == "HYPERLIQUID" else venue

    @staticmethod
    def _hl_scope(instrument_id):
        return instrument_id.split(":", 1)[0] if ":" in instrument_id else ""

    def _account_scope(self, instrument_id):
        domain = self._account_domain(self.markets[instrument_id].venue)
        return domain, self._hl_scope(instrument_id) if domain == "HL" else ""

    def _account_representative(self, instruments):
        return min(instruments, key=lambda i: (self.markets[i].symbol != "BTC", i))

    def _account_coverage(self, account):
        if account.account_positions is None:
            return (account.instrument_id,)
        domain = self._account_domain(account.venue)
        scope = self._hl_scope(account.instrument_id) if domain == "HL" else ""
        return self.account_scopes.get((domain, scope), ())

    def _check_account_proof(self, instrument_id, account, expected):
        """Validate complete evidence once within an immutable validation pass."""
        coverage = self._account_coverage(account)
        if any(account.observed_ns < self.fences.get(i, 0) for i in coverage):
            return {}, "predates_activity"
        if account.account_positions is None:
            positions = {instrument_id: F(account.position_quantity)}
        else:
            scope = self._account_scope(instrument_id)
            cached = self.account_position_values.get(scope)
            if cached is None or cached[0] is not account.account_positions:
                cached = (account.account_positions, {i: F(q) for i, q in account.account_positions})
                self.account_position_values[scope] = cached
            positions = cached[1]
        for instrument in set(coverage) | positions.keys():
            if instrument not in expected:
                expected[instrument] = self.expected_native(instrument)
            if positions.get(instrument, 0) != expected[instrument]:
                return positions, "position_mismatch"
        orders = account.account_open_orders if account.account_open_orders is not None else tuple(
            (instrument_id, cid) for cid in account.open_order_ids)
        for instrument, cid in orders:
            owned = self.orders.get(cid)
            if owned is None or owned["instrument_id"] != instrument or owned["terminal_ns"] is not None:
                return positions, "unowned_order"
        return positions, None

    def _account_problem(self, instrument_id, now_ns, *, check_age=True, proofs=None, expected=None):
        account = self.accounts.get(instrument_id)
        if account is None:
            return "missing"
        if not account.authoritative or not account.connected:
            return "not_authoritative"
        age = now_ns - account.observed_ns
        if age < 0 or (check_age and age > self.account_max_age_ns):
            return "stale_or_future"
        # Never retain results across calls which can change inventory, fences,
        # authority or owned orders. A domain pass finishes validation before
        # applying any pause/recovery side effects. Identity avoids hashing the
        # complete payload for every market; snapshots retain both tuples.
        domain = self._account_domain(account.venue)
        scope = self._hl_scope(account.instrument_id) if domain == "HL" else ""
        key = (domain, scope, account.observed_ns, id(account.account_positions), id(account.account_open_orders))
        if account.account_positions is None:
            key = ("instrument", instrument_id)
        if proofs is None:
            proofs = {}
        if expected is None:
            expected = {}
        if key not in proofs:
            proofs[key] = self._check_account_proof(instrument_id, account, expected)
        positions, reason = proofs[key]
        if reason == "predates_activity":
            return "predates_activity"
        if positions.get(instrument_id, 0) != F(account.position_quantity):
            return "inconsistent_coverage"
        return reason

    def _pause_account(self, instrument_id, now_ns, reason, *, new_evidence=False):
        domain = self._account_domain(self.markets[instrument_id].venue)
        previous = self.account_pauses.get(domain)
        if previous is not None and not new_evidence:
            return
        barrier = max(now_ns, previous["recovery_after_ns"] if previous else 0)
        pause = {"since_ns": previous["since_ns"] if previous else now_ns,
                 "recovery_after_ns": barrier, "reason": reason, "instrument_id": instrument_id}
        if previous == pause:
            return
        self.account_pauses[domain] = pause
        # An account update can arrive after preparation but before dispatch.
        # Retire both unsent legs of each affected pair atomically.
        owners = {o["position_id"] for o in self.orders.values() if o["purpose"] == "entry"
                  and o["status"] == "prepared" and o["instrument_id"] in self.account_domains[domain]}
        canceled, retained = [], []
        for intent in self.outbox:
            order = self.orders.get(intent.client_order_id)
            if (intent.action == "submit" and order and order["purpose"] == "entry"
                    and order["status"] == "prepared" and order["position_id"] in owners):
                order["terminal_ns"], order["status"] = now_ns, "local_not_dispatched"
                self.fences[order["instrument_id"]] = now_ns
                canceled.append(intent.client_order_id)
                self.take_entry_timing(intent)
            else:
                retained.append(intent)
        self.outbox = retained
        self.emit("account_domain_paused", now_ns, domain=domain, **pause, local_not_dispatched=canceled)

    @timed("strategy.account_domain", labels=lambda self, domain, *a, **kw: (domain, ""))
    def _account_domain_ready(self, domain, now_ns):
        instruments = self.account_domains[domain]
        proofs, expected = {}, {}
        problems = [(i, self._account_problem(i, now_ns, proofs=proofs, expected=expected)) for i in instruments]
        self.account_check_metrics[domain] = {"checked_at_unix_ns": now_ns, "markets_checked": len(instruments),
            "proofs_checked": len(proofs), "expected_quantities_computed": len(expected),
            "position_rows_checked": sum(len(positions) for positions, _ in proofs.values()),
            "unconfigured_position_rows": sum(i not in self.markets
                for positions, _ in proofs.values() for i in positions)}
        for instrument_id, reason in problems:
            if reason and reason != "predates_activity":
                self._pause_account(instrument_id, now_ns, reason)
            if reason:
                self._request_account(instrument_id, now_ns)
        pause = self.account_pauses.get(domain)
        if pause is None:
            return not any(reason for _, reason in problems)
        # Every market needs a fresh observation begun after the failure, and all
        # original IDs must be terminal. An empty response alone cannot resolve
        # an unknown submission or an ambiguous external order.
        ready = all(reason is None and self.accounts[i].observed_ns > pause["recovery_after_ns"]
                    for i, reason in problems) and not any(self._pending(i) for i in instruments)
        if ready:
            del self.account_pauses[domain]
            self.emit("account_domain_recovered", now_ns, domain=domain,
                      recovery_after_ns=pause["recovery_after_ns"],
                      read_started_ns={i: self.accounts[i].observed_ns for i in instruments})
        else:
            for i, reason in problems:
                if reason or self.accounts[i].observed_ns <= pause["recovery_after_ns"]:
                    self._request_account(i, now_ns)
        return ready

    @timed("strategy.reconcile_accounts")
    def reconcile_accounts(self, now_ns):
        """Schedule read-only work; never wait for a network read on the scan loop."""
        for domain in self.account_domains:
            self._account_domain_ready(domain, now_ns)
        for instrument_id in self.markets:
            account = self.accounts.get(instrument_id)
            if account is None or now_ns - account.observed_ns >= self.account_max_age_ns // 2:
                self._request_account(instrument_id, now_ns)

    def _account_ready(self, instrument_id, now_ns, *, pending_entry_owner=None):
        account = self.accounts.get(instrument_id)
        pending = self._pending(instrument_id)
        # A still-live entry can only add to this pair's owned leg. A hard
        # risk exit may retire confirmed fills after a fresh account proof;
        # pending reductions or orders owned by another pair remain barriers.
        allowed = {o["client_order_id"] for o in pending if pending_entry_owner is not None
                   and o["position_id"] == pending_entry_owner and o["purpose"] == "entry"}
        ready = (account is not None and account.authoritative and account.connected
            and 0 <= now_ns - account.observed_ns <= self.account_max_age_ns
            and account.observed_ns >= self.fences.get(instrument_id, 0)
            and set(account.open_order_ids) <= allowed and len(allowed) == len(pending)
            and F(account.position_quantity) == self.expected_native(instrument_id))
        if not ready:
            self._request_account(instrument_id, now_ns)
        return bool(ready)

    @timed("strategy.account", labels=lambda self, account, *a, **kw: (account.venue, ""))
    def on_account(self, account, now_ns):
        if account.instrument_id not in self.markets or self.markets[account.instrument_id].venue != account.venue:
            return
        self.full_evaluation_symbols.update(self.trade_symbols)
        instrument_id = account.instrument_id
        self.account_requests.pop(instrument_id, None)
        self.account_request_ids.pop(instrument_id, None)
        old = self.accounts.get(instrument_id)
        if account.observed_ns > now_ns or not account.authoritative or not account.connected:
            # Untrusted timestamps must not poison the monotonic-success fence.
            # Neither entries nor reductions can reuse the invalidated target's
            # old observation, even if it arrives after this failure.
            self.accounts.pop(instrument_id, None)
            self.fences[instrument_id] = max(self.fences.get(instrument_id, 0), now_ns + 1)
            self._pause_account(instrument_id, now_ns, "not_authoritative", new_evidence=True)
            return
        if old is not None and account.observed_ns < old.observed_ns:
            return
        self.accounts[instrument_id] = account
        if account.observed_ns >= self.fences.get(instrument_id, 0):
            scope = self._account_scope(instrument_id)
            self.account_read_failures.pop(scope, None)
            self.account_retry_after_ns.pop(scope, None)
        if account.account_positions is not None:
            positions = dict(account.account_positions)
            if F(positions.get(instrument_id, 0)) == F(account.position_quantity):
                # Project only account facts. Another market must never inherit
                # the representative's quote or quote timestamp.
                for covered in self._account_coverage(account):
                    if covered == instrument_id:
                        continue
                    previous = self.accounts.get(covered)
                    if previous is None or previous.observed_ns <= account.observed_ns:
                        self.accounts[covered] = replace(account, instrument_id=covered,
                            position_quantity=positions.get(covered, Decimal(0)),
                            open_order_ids=tuple(cid for i, cid in account.account_open_orders if i == covered),
                            bid=None, ask=None, book_observed_ns=None)
        problem = self._account_problem(instrument_id, now_ns)
        if problem and problem != "predates_activity":
            self._pause_account(instrument_id, now_ns, problem, new_evidence=account != old)
        domain = self._account_domain(account.venue)
        if domain in self.account_pauses and problem is None:
            self._account_domain_ready(domain, now_ns)
        if not self._pending(instrument_id) and account.observed_ns >= self.fences.get(instrument_id, 0):
            expected = self.expected_native(instrument_id)
            self.emit("account_reconciled" if F(account.position_quantity) == expected and not account.open_order_ids
                      else "account_mismatch", now_ns, instrument_id=instrument_id,
                      expected_native=_number(expected), observed_native=str(account.position_quantity),
                      open_orders=len(account.open_order_ids), read_started_ns=account.observed_ns)

    def _limit(self, instrument_id, side, base_quantity, now_ns, *, partial=False,
               reduce_only=False, require_fees=True):
        book, meta = self.books.get(instrument_id), self.metadata.get(instrument_id)
        native = self.instruments.get(instrument_id)
        if (book is None or meta is None or native is None or meta.instrument_id != instrument_id
                or not _execution_metadata_valid(meta) or not _book_valid(book, self.plan.settings, now_ns)):
            return None
        fees_ready = _fee_metadata_valid(meta)
        if require_fees and not fees_ready:
            return None
        market = self.markets[instrument_id]
        if book.venue != market.venue or book.symbol != market.symbol:
            return None
        if F(meta.multiplier) != F(native["native_multiplier"]) * F(market.canonical_multiplier):
            return None
        step, multiplier = F(meta.size_increment), F(meta.multiplier)
        quantity = F(base_quantity) / multiplier
        levels = book.asks if side == "BUY" else book.bids
        available = sum((F(q) for _, q in levels), F(0))
        if partial:
            quantity = min(quantity, (available // step) * step)
        if quantity <= 0 or quantity % step or quantity > available:
            return None
        remaining, price = quantity, None
        for px, size in levels:
            remaining -= min(remaining, F(size))
            price = F(px) * F(market.canonical_multiplier) / F(market.quote_to_usd)
            if remaining == 0:
                break
        factor = 1 + F(self.policy.slippage_bps) / 10000 if side == "BUY" else 1 - F(self.policy.slippage_bps) / 10000
        tick = F(native["price_increment"])
        px = price * factor
        px = -(-px // tick) * tick if side == "BUY" else (px // tick) * tick
        if px <= 0:
            return None
        unit_cash = px * F(native["native_multiplier"]) * F(market.quote_to_usd)
        cap_quantity = (F(self.policy.order_notional_usd) / unit_cash // step) * step
        if quantity > cap_quantity:
            if not partial or cap_quantity <= 0:
                return None
            quantity = cap_quantity
        cash = quantity * unit_cash
        # Some venues waive entry minima for reduce-only dust; try the confirmed
        # residual, never round it up into a new opposite position.
        if not reduce_only and ((meta.min_quantity is not None and quantity < F(meta.min_quantity))
                or (meta.min_notional is not None and cash < F(meta.min_notional))):
            return None
        return {"quantity": exact_decimal(quantity), "price": exact_decimal(px),
                "base_quantity": quantity * multiplier, "cash": cash,
                "fee": cash * F(meta.taker_fee_bps) / 10000 if fees_ready else None,
                "reduce_only": reduce_only}

    def _make_order(self, position, leg, purpose, bound, now_ns):
        instrument_id = position[f"{leg}_id"]
        side = ("BUY" if leg == "buy" else "SELL") if purpose == "entry" else ("SELL" if leg == "buy" else "BUY")
        request_id = self._request_id("S")
        # Short ASCII IDs fit Aster/Lighter and are hashed to a cloid by HL.
        cid = request_id
        intent = OrderIntent("submit", self.markets[instrument_id].venue, instrument_id,
            request_id, cid, side, bound["quantity"], bound["price"], "IOC", False, bound["reduce_only"])
        self.orders[cid] = {"client_order_id": cid, "request_id": request_id,
            "position_id": position["position_id"], "leg": leg, "purpose": purpose,
            "instrument_id": instrument_id, "side": side, "quantity": str(intent.quantity),
            "price": str(intent.price), "reduce_only": intent.reduce_only, "sent_ns": now_ns,
            "accepted_ns": None, "first_fill_ns": None, "terminal_ns": None,
            "status": "prepared", "filled_quantity": "0"}
        self.fences[instrument_id] = now_ns
        self.last_attempt[instrument_id] = now_ns
        position[f"{purpose}_ids"].append(cid)
        return intent

    def _publish_orders(self, intents, now_ns, position):
        with span("strategy.persist_intents"):
            self.emit("orders_prepared", now_ns, position_id=position["position_id"],
                      client_order_ids=[i.client_order_id for i in intents])
        self.outbox.extend(intents)
        if intents and self.intent_notifier is not None:
            self.intent_notifier()

    @timed("strategy.entry")
    def _entry(self, buy_id, sell_id, now_ns):
        if self.entry_pause_reason:
            return False
        key = self.pair_key(buy_id, sell_id)
        if key in self.positions or len(self.positions) >= self.max_pairs:
            return False
        for position in self.positions.values():
            for leg in ("buy", "sell"):
                if F(position[f"remaining_{leg}_base"]) > 0:
                    book = self.books.get(position[f"{leg}_id"])
                    if book is None or not _book_valid(book, self.plan.settings, now_ns):
                        return False
        # Reserve both complete entry budgets until a pair has reconciled flat.
        reserved = self.gross_exposure(reserve=True)
        if reserved + 2 * F(self.policy.order_notional_usd) > self.max_gross:
            return False
        if any(self._pending(i) for i in (buy_id, sell_id)):
            return False
        if key in self.last_closed and any(i not in self.books or self.books[i].ts_event_ns <= self.last_closed[key]
                                          for i in (buy_id, sell_id)):
            return False
        pricing_marker = start_latency()
        estimate = self.pricer.entry(buy_id, sell_id, now_ns)
        if estimate is None:
            return False
        record_latency(pricing_marker, "entry.qualified_pricing")
        candidate_marker = start_latency()
        with span("entry.executable_bounds"):
            bounds = {leg: self._limit(i, side, F(estimate["sizing"]["base_quantity"]), now_ns)
                      for leg, i, side in (("buy", buy_id, "BUY"), ("sell", sell_id, "SELL"))}
        if any(b is None for b in bounds.values()):
            return False
        buy, sell = bounds["buy"], bounds["sell"]
        if max(buy["cash"], sell["cash"]) > F(self.policy.order_notional_usd):
            return False
        net = sell["cash"] - buy["cash"] - buy["fee"] - sell["fee"] - buy["cash"] * F(self.plan.settings.reserve_bps) / 10000
        if net * 10000 < F(self.policy.entry_edge_bps) * buy["cash"]:
            return False
        with span("entry.account_checks"):
            ready = [self._account_ready(i, now_ns) for i in (buy_id, sell_id)]
            ready.extend(self._account_domain_ready(domain, now_ns) for domain in sorted({
                self._account_domain(self.markets[i].venue) for i in (buy_id, sell_id)}))
        if not all(ready):
            return False
        self.sequence += 1
        estimate.pop("paper_fills", None)
        position = {"position_id": self.sequence, "symbol": key[0], "pair": list(key),
            "buy_id": buy_id, "sell_id": sell_id, "opened_ns": None, "status": "entering",
            "exit_reason": None, "exit_requested_ns": None, "entry": estimate,
            "entry_ids": [], "exit_ids": [], "rebalance_ids": [],
            "entry_buy_base": "0", "entry_sell_base": "0",
            "remaining_buy_base": "0", "remaining_sell_base": "0",
            "unhedged_since_ns": None, "unhedged_base": "0", "unhedged_notional_usd": "0",
            "unhedged_duration_ms": "0",
            "closing_long_cash_usd": "0", "closing_short_cash_usd": "0", "closing_fees_usd": "0",
            "actual_entry": {"buy_notional_usd": "0", "sell_notional_usd": "0",
                             "buy_fee_usd": "0", "sell_fee_usd": "0"},
            "pending_commissions": {},
            "native_rules": {leg: {name: str(value) for name, value in self.instruments[i].items()}
                             for leg, i in (("buy", buy_id), ("sell", sell_id))},
            "unknown_commission": False, "reserved_gross_usd": str(2 * self.policy.order_notional_usd)}
        self.positions[key] = position
        with span("entry.prepare_orders"):
            intents = [self._make_order(position, leg, "entry", bounds[leg], now_ns) for leg in ("buy", "sell")]
        self._publish_orders(intents, now_ns, position)
        if candidate_marker is not None:
            record_latency(candidate_marker, "entry.candidate_to_persisted_intents")
            for intent in intents:
                if len(self.entry_timings) >= MAX_PENDING_ENTRY_TIMINGS:
                    self.entry_timings.pop(next(iter(self.entry_timings)))
                self.entry_timings[intent.request_id] = candidate_marker
        return True

    def _position(self, position_id):
        return next((p for p in self.positions.values() if p["position_id"] == position_id), None)

    @timed("strategy.fill")
    def on_fill(self, fill, now_ns):
        cid = fill["client_order_id"]
        order = self.orders.get(cid)
        if order is None:
            self.halt_reason = "fill has no persisted live order owner"
            self.emit("unowned_fill", now_ns, client_order_id=cid)
            return
        instrument_id, leg = order["instrument_id"], order["leg"]
        position = self._position(order["position_id"])
        identity = f"{self.markets[instrument_id].venue}:{cid}:{fill['trade_id']}"
        quantity = F(_positive(fill.get("native_qty", fill.get("quantity")), "fill quantity"))
        price = F(_positive(fill.get("native_price", fill.get("price")), "fill price"))
        signature = {"client_order_id": cid, "quantity": _number(quantity), "price": _number(price),
                     "commission_amount": str(fill.get("commission_amount")),
                     "commission_currency": str(fill.get("commission_currency"))}
        if identity in self.executions:
            if self.executions[identity] != signature:
                self.halt_reason = "conflicting execution replay"
                self.emit("conflicting_fill", now_ns, execution_id=identity)
            return
        if (position is None or fill["instrument_id"] != instrument_id or fill["side"] != order["side"]
                or F(order["filled_quantity"]) + quantity > F(order["quantity"])):
            self.halt_reason = "execution violates persisted order ownership"
            self.emit("invalid_fill", now_ns, client_order_id=cid, execution_id=identity)
            return
        market, native = self.markets[instrument_id], self.instruments[instrument_id]
        base = quantity * F(position["entry"][f"{leg}_metadata"]["multiplier"])
        cash = quantity * price * F(native["native_multiplier"]) * F(market.quote_to_usd)
        remaining_field = f"remaining_{leg}_base"
        if order["purpose"] != "entry" and base > F(position[remaining_field]):
            self.halt_reason = "exit fill exceeds confirmed pair inventory"
            self.emit("invalid_exit_fill", now_ns, client_order_id=cid)
            return
        fee_amount, fee_currency = fill.get("commission_amount"), fill.get("commission_currency")
        stable_parity = (market.quote_to_usd == 1 and
            {fee_currency, native["quote_currency"]} <= {"USD", "USDC", "USDT"})
        try:
            amount = Decimal(str(fee_amount)) if fee_amount is not None else None
        except InvalidOperation:
            amount = None
        known_fee = (amount is not None and amount.is_finite()
                     and (fee_currency == native["quote_currency"] or stable_parity))
        estimated_fee = None
        if known_fee:
            fee = F(amount) * F(market.quote_to_usd)
        else:
            # Only actual commissions enter fee totals. Missing economics must
            # never prevent a valid fill from retiring confirmed inventory.
            fee = F(0)
            meta = self.metadata.get(instrument_id)
            if meta is not None and meta.instrument_id == instrument_id and _fee_metadata_valid(meta):
                estimated_fee = cash * F(meta.taker_fee_bps) / 10000
            position["unknown_commission"] = True
            position["pending_commissions"][identity] = {
                "client_order_id": cid, "instrument_id": instrument_id, "purpose": order["purpose"],
                "commission_amount": None if fee_amount is None else str(fee_amount),
                "commission_currency": fee_currency, "notional_usd": _number(cash),
                "estimated_fee_usd": _number(estimated_fee) if estimated_fee is not None else None,
                "fee_source": meta.fee_source if estimated_fee is not None else None,
            }
        if order["purpose"] == "entry":
            position[remaining_field] = _number(F(position[remaining_field]) + base)
            position[f"entry_{leg}_base"] = _number(F(position[f"entry_{leg}_base"]) + base)
            actual = position["actual_entry"]
            actual[f"{leg}_notional_usd"] = _number(F(actual[f"{leg}_notional_usd"]) + cash)
            actual[f"{leg}_fee_usd"] = _number(F(actual[f"{leg}_fee_usd"]) + fee)
            if position["opened_ns"] is None:
                position["opened_ns"] = now_ns
        else:
            position[remaining_field] = _number(F(position[remaining_field]) - base)
            field = "closing_long_cash_usd" if leg == "buy" else "closing_short_cash_usd"
            position[field] = _number(F(position[field]) + cash)
            position["closing_fees_usd"] = _number(F(position["closing_fees_usd"]) + fee)
        self.executions[identity] = signature
        order["filled_quantity"] = _number(F(order["filled_quantity"]) + quantity)
        order["first_fill_ns"] = order["first_fill_ns"] or now_ns
        self.fences[instrument_id] = now_ns
        if order["terminal_ns"] is not None:
            # Preserve the late inventory, stop admission and demand a new read.
            position["status"], position["exit_reason"] = "exit_pending", "entry_imbalance"
            self.halt_reason = "late fill after order terminal requires reconciliation"
        self._update_unhedged(position, now_ns)
        self.emit("fill", now_ns, execution_id=identity, position_id=position["position_id"],
                  client_order_id=cid, native_quantity=_number(quantity), native_price=_number(price),
                  fee_usd=_number(fee) if known_fee else None, fee_confirmed=known_fee,
                  estimated_fee_usd=_number(estimated_fee) if estimated_fee is not None else None,
                  latency_ms=_number(F(now_ns - order["sent_ns"], 1_000_000)))
        self._request_account(instrument_id, now_ns)

    @timed("strategy.receipt")
    def on_receipt(self, receipt, now_ns):
        if receipt.kind in {"unknown", "rejected"}:
            for instrument_id, request_id in tuple(self.account_request_ids.items()):
                if receipt.request_id == request_id:
                    self.account_requests.pop(instrument_id, None)
                    self.account_request_ids.pop(instrument_id, None)
                    scope = self._account_scope(instrument_id)
                    failures = min(self.account_read_failures.get(scope, 0) + 1, 5)
                    self.account_read_failures[scope] = failures
                    delay = max(min(5 * 2 ** (failures - 1), 60), receipt.retry_after_secs)
                    self.account_retry_after_ns[scope] = now_ns + delay * 1_000_000_000
                    self.accounts.pop(instrument_id, None)
                    self.fences[instrument_id] = max(self.fences.get(instrument_id, 0), now_ns + 1)
                    self._pause_account(instrument_id, now_ns, "read_failed", new_evidence=True)
                    return
        order = self.orders.get(receipt.client_order_id)
        if order is None or receipt.request_id != order["request_id"] or receipt.kind == "fill":
            return
        if receipt.kind == "accepted":
            if order["accepted_ns"] is None:
                order["accepted_ns"] = now_ns
                self.emit("order_accepted", now_ns, client_order_id=receipt.client_order_id,
                          latency_ms=_number(F(now_ns - order["sent_ns"], 1_000_000)))
            return
        if receipt.kind in TERMINALS:
            if order["terminal_ns"] is not None:
                return
            order["terminal_ns"], order["status"] = now_ns, receipt.kind
            self.fences[order["instrument_id"]] = now_ns
            self.emit("order_terminal", now_ns, client_order_id=receipt.client_order_id,
                      outcome=receipt.kind, filled_quantity=order["filled_quantity"],
                      latency_ms=_number(F(now_ns - order["sent_ns"], 1_000_000)))
            self._request_account(order["instrument_id"], now_ns)
        elif receipt.kind == "unknown":
            if order["terminal_ns"] is None and order["status"] != "unknown":
                order["status"] = "unknown"
                self.emit("order_unknown", now_ns, client_order_id=receipt.client_order_id)

    def _pnl(self, position):
        actual = position["actual_entry"]
        gross = (F(actual["sell_notional_usd"]) - F(actual["buy_notional_usd"])
                 + F(position["closing_long_cash_usd"]) - F(position["closing_short_cash_usd"]))
        fees = F(actual["buy_fee_usd"]) + F(actual["sell_fee_usd"]) + F(position["closing_fees_usd"])
        return gross - fees

    def _economics(self, position):
        """Keep incomplete fee evidence after inventory is reconciled flat."""
        accounted_net = self._pnl(position)
        pending = position["pending_commissions"]
        confirmed = not position["unknown_commission"]
        estimated_net = None
        if confirmed:
            estimated_net = accounted_net
        elif pending and not position.get("legacy_fee_accounting") and all(
                row["estimated_fee_usd"] is not None for row in pending.values()):
            estimated_net = accounted_net - sum((F(row["estimated_fee_usd"]) for row in pending.values()), F(0))
        return {"position_id": position["position_id"], "pair": position["pair"],
                "net_usd": _number(accounted_net) if confirmed else None,
                "provisional_net_usd": _number(estimated_net) if estimated_net is not None else None,
                "fee_confirmed": confirmed, "pending_commissions": deepcopy(pending),
                "actual_entry": deepcopy(position["actual_entry"]),
                "closing_long_cash_usd": position["closing_long_cash_usd"],
                "closing_short_cash_usd": position["closing_short_cash_usd"],
                "closing_fees_usd": position["closing_fees_usd"],
                "legacy_fee_accounting": position.get("legacy_fee_accounting", False)}

    def _hard_exit_reason(self, position, now_ns):
        if position["opened_ns"] is None:
            return None
        age = now_ns - position["opened_ns"]
        if self.stopping:
            return "operator_stop"
        if self.halt_reason:
            return "execution_halt"
        if self.gross_exposure() > self.max_gross:
            return "exposure_limit"
        if age >= self.policy.force_close_after_secs * 1_000_000_000:
            return "max_hold"
        if position.get("unhedged_breach"):
            return position["unhedged_breach"]["reason"]
        return None

    def _update_unhedged(self, position, now_ns):
        """Measure confirmed canonical inventory; unknown orders are never zero fills."""
        previous_since = position.get("unhedged_since_ns")
        elapsed = max(0, now_ns - previous_since) if previous_since is not None else 0
        previous_delta = position.get("unhedged_base", "0")
        previous_notional = position.get("unhedged_notional_usd", "0")
        delta = F(position["remaining_buy_base"]) - F(position["remaining_sell_base"])
        notional = F(0)
        if delta:
            leg = "buy" if delta > 0 else "sell"
            # Books are already in canonical base/USD units. Keep the actual
            # entry VWAP as a floor/fallback when the book is absent or stale.
            price = F(position["actual_entry"][f"{leg}_notional_usd"]) / F(position[f"entry_{leg}_base"])
            book = self.books.get(position[f"{leg}_id"])
            if book and _book_valid(book, self.plan.settings, now_ns):
                price = max(price, F(book.asks[0][0]))
            notional = abs(delta) * price
        position.update(unhedged_base=_number(delta), unhedged_notional_usd=_number(notional),
                        unhedged_duration_ms=_number(F(elapsed, 1_000_000)) if delta else "0",
                        unhedged_since_ns=(previous_since if previous_since is not None else now_ns) if delta else None)
        reason = ("unhedged_notional" if notional >= self.max_unhedged_notional else
                  "unhedged_timeout" if previous_since is not None and elapsed >= self.max_unhedged_ns else None)
        if reason and not position.get("unhedged_breach"):
            # Latch even if a fill arriving at the deadline balances inventory.
            # Clearing the current delta must not undo an already expired episode.
            breach = {"reason": reason, "triggered_ns": now_ns,
                      "since_ns": previous_since if previous_since is not None else now_ns,
                      "base": _number(delta) if delta else previous_delta,
                      "notional_usd": _number(notional) if delta else previous_notional,
                      "duration_ms": _number(F(elapsed, 1_000_000)),
                      "max_notional_usd": _number(self.max_unhedged_notional),
                      "max_duration_secs": self.max_unhedged_ns // 1_000_000_000}
            position["unhedged_breach"] = breach
            if position["exit_reason"] not in HARD_EXITS:
                position.update(status="exit_pending", exit_reason=self._hard_exit_reason(position, now_ns),
                                exit_requested_ns=now_ns)
            self.emit("unhedged_limit_breached", now_ns, position_id=position["position_id"], **breach)

    def _exit_reason(self, position, now_ns):
        hard = self._hard_exit_reason(position, now_ns)
        if hard or position["opened_ns"] is None:
            return hard
        age = now_ns - position["opened_ns"]
        if F(position["actual_entry"]["buy_notional_usd"]) <= 0:
            return "entry_imbalance"
        # Keep available fee estimates in loss decisions without booking them
        # as actual commissions. Unknown fees still forbid every soft exit.
        estimated_fees = sum((F(row["estimated_fee_usd"]) for row in position["pending_commissions"].values()
                              if row["estimated_fee_usd"] is not None), F(0))
        pricing_position = {**position, "closing_fees_usd":
                            _number(F(position["closing_fees_usd"]) + estimated_fees)}
        mark = self.pricer.measure(pricing_position, now_ns)
        if mark is None:
            return None
        net, capital = F(mark["net_usd"]), F(mark["capital_usd"])
        if self.policy.loss_stop_enabled and net * 10000 <= -F(self.policy.max_loss_bps) * capital:
            return "max_loss"
        if position["unknown_commission"] or not mark["executable"]:
            return None
        if net * 10000 >= F(self.policy.take_profit_bps) * capital:
            return "take_profit"
        if age >= self.policy.breakeven_after_secs * 1_000_000_000 and net > 0:
            return "positive_after_wait"
        return None

    def _reduction_bound(self, position, leg, qty, now_ns, *, require_fees=True,
                         allow_pending_entry=False):
        instrument_id = position[f"{leg}_id"]
        if not qty or now_ns - self.last_attempt.get(instrument_id, 0) < self.retry_ns:
            return None
        if not self._account_ready(instrument_id, now_ns,
                pending_entry_owner=position["position_id"] if allow_pending_entry else None):
            return None
        current = self.metadata.get(instrument_id)
        original = position["entry"][f"{leg}_metadata"]
        if current is None or current.instrument_id != instrument_id or not _execution_metadata_valid(current):
            return None
        for name in ("multiplier", "size_increment", "min_quantity", "min_notional"):
            old_value = original.get(name)
            if getattr(current, name) != (Decimal(old_value) if old_value is not None else None):
                return None
        original_native = position.get("native_rules", {}).get(leg)
        native = self.instruments.get(instrument_id, {})
        if original_native is not None and any(
                native.get(name) != (value if name == "quote_currency" else Decimal(value))
                for name, value in original_native.items()):
            return None
        native_remaining = qty / F(original["multiplier"])
        delta = -native_remaining if leg == "buy" else native_remaining
        net = self.expected_native(instrument_id)
        reducing = net * delta < 0 and abs(delta) <= abs(net)
        return self._limit(instrument_id, "SELL" if leg == "buy" else "BUY", qty,
                           now_ns, partial=True, reduce_only=reducing, require_fees=require_fees)

    def _rebalance_entry(self, position, now_ns):
        """Trim only the excess leg; never increase either confirmed entry leg."""
        target = F(position["rebalance_target_base"])
        leg = position["rebalance_leg"]
        other = "sell" if leg == "buy" else "buy"
        remaining = F(position[f"remaining_{leg}_base"])
        if target <= 0 or remaining < target or F(position[f"remaining_{other}_base"]) != target:
            self.halt_reason = "entry rebalance inventory differs from its durable target"
            self.emit("rebalance_inventory_mismatch", now_ns, position_id=position["position_id"])
            return
        ready = [self._account_ready(position[f"{side}_id"], now_ns) for side in ("buy", "sell")]
        if not all(ready):
            return
        excess = remaining - target
        if excess == 0:
            position.update(status="open", rebalance_completed_ns=now_ns)
            position.pop("rebalance_blocked", None)
            self.emit("entry_rebalanced", now_ns, position_id=position["position_id"],
                      retained_base=_number(target), reduced_leg=leg,
                      elapsed_ms=_number(F(now_ns-position["rebalance_started_ns"], 1_000_000)))
            self.emit("opened", now_ns, position_id=position["position_id"],
                      buy_base=_number(target), sell_base=_number(target), rebalanced=True)
            return
        instrument_id = position[f"{leg}_id"]
        if now_ns - self.last_attempt.get(instrument_id, 0) < self.retry_ns:
            return
        bound = self._reduction_bound(position, leg, excess, now_ns, require_fees=False)
        if bound is None:
            if not position.get("rebalance_blocked"):
                position["rebalance_blocked"] = True
                self.emit("entry_rebalance_blocked", now_ns, position_id=position["position_id"],
                          reason="excess reduction has no executable bound",
                          remaining_excess_base=_number(excess), target_base=_number(target))
            return
        position.pop("rebalance_blocked", None)
        intent = self._make_order(position, leg, "rebalance", bound, now_ns)
        self._publish_orders([intent], now_ns, position)

    @timed("strategy.position_risk_and_exit")
    def observe(self, key, now_ns):
        position = self.positions[key]
        self._update_unhedged(position, now_ns)
        ids = (position["buy_id"], position["sell_id"])
        entry_done = all(self.orders[cid]["terminal_ns"] is not None for cid in position["entry_ids"])
        if not entry_done:
            # An unknown other venue must not prevent time-based reduction of a
            # separately confirmed leg. Its original ID and pair lock stay live.
            hard_reason = self._hard_exit_reason(position, now_ns)
            if hard_reason:
                if position["exit_reason"] != hard_reason:
                    position.update(status="exit_pending", exit_reason=hard_reason, exit_requested_ns=now_ns)
                    self.emit("exit_requested", now_ns, position_id=position["position_id"], reason=hard_reason)
            elif position["exit_reason"] not in HARD_EXITS:
                return
        remains = [F(position[f"remaining_{leg}_base"]) for leg in ("buy", "sell")]
        if not any(remains):
            position["status"] = "flat_pending"
            ready = [self._account_ready(i, now_ns) for i in ids]
            if not all(ready):
                return
            economics = self._economics(position)
            self.positions.pop(key)
            self.last_closed[key] = now_ns
            self.completed += 1
            if economics["fee_confirmed"]:
                self.realized_net += F(economics["net_usd"])
            else:
                self.pending_economics[str(position["position_id"])] = {
                    **economics, "closed_ns": now_ns, "reason": position["exit_reason"]}
            self.emit("closed", now_ns, position_id=position["position_id"], pair=list(key),
                      net_usd=economics["net_usd"], fee_confirmed=economics["fee_confirmed"],
                      provisional_net_usd=economics["provisional_net_usd"],
                      reason=position["exit_reason"] or "entry_unfilled")
            return
        if position["status"] == "entering" and entry_done:
            balanced = remains[0] == remains[1] and remains[0] > 0
            if not balanced and all(q > 0 for q in remains):
                position.update(status="rebalancing", rebalance_started_ns=now_ns,
                                rebalance_target_base=_number(min(remains)),
                                rebalance_leg="buy" if remains[0] > remains[1] else "sell")
                position.setdefault("rebalance_ids", [])
                self.emit("entry_rebalance_requested", now_ns, position_id=position["position_id"],
                          buy_base=_number(remains[0]), sell_base=_number(remains[1]),
                          target_base=position["rebalance_target_base"], reduced_leg=position["rebalance_leg"])
            elif balanced:
                position["status"] = "open"
                self.emit("opened", now_ns, position_id=position["position_id"],
                          buy_base=_number(remains[0]), sell_base=_number(remains[1]))
            else:
                position["status"] = "exit_pending"
                position["exit_reason"] = "entry_imbalance"
                position["exit_requested_ns"] = now_ns
                self.emit("entry_imbalance", now_ns, position_id=position["position_id"],
                          buy_base=_number(remains[0]), sell_base=_number(remains[1]))
        if position["exit_ids"] and remains[0] != remains[1] and position["exit_reason"] not in HARD_EXITS:
            position["exit_reason"] = "exit_imbalance"
            position["status"] = "exit_pending"
        reason = self._exit_reason(position, now_ns)
        if (position["status"] == "rebalancing" and position["exit_reason"] not in HARD_EXITS
                and reason not in HARD_EXITS):
            self._rebalance_entry(position, now_ns)
            return
        if position["exit_reason"] not in HARD_EXITS:
            if reason is None:
                position["status"], position["exit_reason"] = "open", None
                return
            if position["exit_reason"] != reason:
                position.update(status="exit_pending", exit_reason=reason, exit_requested_ns=now_ns)
                self.emit("exit_requested", now_ns, position_id=position["position_id"], reason=reason)
        hard = position["exit_reason"] in HARD_EXITS
        bounds = {}
        for leg, qty in zip(("buy", "sell"), remains):
            bound = self._reduction_bound(position, leg, qty, now_ns, require_fees=not hard,
                                          allow_pending_entry=bool(position.get("unhedged_breach")))
            if bound is not None:
                bounds[leg] = bound
        if not bounds or (not hard and len(bounds) != sum(q > 0 for q in remains)):
            return
        if not hard:
            # Recheck profit against the actual rounded LIMIT prices, not VWAP.
            pnl = self._pnl(position)
            for leg, bound in bounds.items():
                scale = F(position[f"remaining_{leg}_base"]) / bound["base_quantity"]
                pnl += ((bound["cash"] if leg == "buy" else -bound["cash"]) - bound["fee"]) * scale
            capital = F(position["actual_entry"]["buy_notional_usd"])
            if position["unknown_commission"] or (position["exit_reason"] == "take_profit"
                    and pnl * 10000 < F(self.policy.take_profit_bps) * capital) or (
                    position["exit_reason"] == "positive_after_wait" and pnl <= 0):
                return
            # A rising price can make an owned leg larger than the order cap.
            # Work off equal base-sized batches, retaining the original ledger.
            step = F(position["entry"]["sizing"]["common_base_step"])
            paired_quantity = (min(b["base_quantity"] for b in bounds.values()) // step) * step
            if paired_quantity <= 0:
                return
            for leg, old in tuple(bounds.items()):
                bounds[leg] = self._limit(position[f"{leg}_id"], "SELL" if leg == "buy" else "BUY",
                    paired_quantity, now_ns, reduce_only=old["reduce_only"])
            if any(bound is None for bound in bounds.values()):
                return
        intents = [self._make_order(position, leg, "exit", bound, now_ns) for leg, bound in bounds.items()]
        self._publish_orders(intents, now_ns, position)

    def pending_evaluation_symbols(self):
        if self.phase != "active" or self.entry_pause_reason or self.halt_reason or self.stopping:
            return set()
        return self.full_evaluation_symbols.copy()

    def update_book(self, instrument_id, book, now_ns, *, raw_book=None, evaluate=True):
        # Owned inventory shares a global gross limit. Its valuation/freshness
        # can change admission on every symbol, not just the updated leg.
        if self.positions:
            self.full_evaluation_symbols.update(self.trade_symbols)
        super().update_book(instrument_id, book, now_ns, raw_book=raw_book, evaluate=evaluate)

    def update_metadata(self, instrument_id, metadata, now_ns, *, evaluate=True):
        if self.positions:
            self.full_evaluation_symbols.update(self.trade_symbols)
        super().update_metadata(instrument_id, metadata, now_ns, evaluate=evaluate)

    def invalidate(self, instrument_id, now_ns, *, metadata=False):
        if self.positions:
            self.full_evaluation_symbols.update(self.trade_symbols)
        super().invalidate(instrument_id, now_ns, metadata=metadata)

    @timed("strategy.evaluate_symbol")
    def evaluate_symbol(self, symbol, now_ns, *, changed_id=None, changed_ids=None):
        if self.phase not in {"active", "draining"} or not self.cleanup_allowed:
            return
        for key in tuple(self.positions):
            if key[0] == symbol:
                self.observe(key, now_ns)
        if (self.stopping or self.entry_pause_reason or self.halt_reason or symbol not in self.trade_symbols
                or any(p.get("unhedged_breach") for p in self.positions.values()) or any(
                o["terminal_ns"] is None and (o["status"] == "unknown" or now_ns - o["sent_ns"] >= self.order_timeout_ns)
                for o in self.orders.values())):
            return
        affected = {changed_id} if changed_id is not None else changed_ids
        if self.positions or symbol in self.full_evaluation_symbols:
            affected = None
        self.full_evaluation_symbols.discard(symbol)
        for first, second in combinations(sorted(self.groups[symbol]), 2):
            if affected is not None and first not in affected and second not in affected:
                continue
            if self.markets[first].venue == self.markets[second].venue:
                continue
            for buy_id, sell_id in ((first, second), (second, first)):
                if self._entry(buy_id, sell_id, now_ns):
                    break

    @timed("strategy.evaluation_timer")
    def on_evaluation_timer(self, now_ns):
        if self.phase not in {"active", "draining"} or not self.cleanup_allowed:
            return
        self.reconcile_accounts(now_ns)
        for key in tuple(self.positions):
            self.observe(key, now_ns)

    def stop_entries(self, now_ns, reason="operator_stop"):
        self.stopping = True
        retained, canceled = [], []
        for intent in self.outbox:
            order = self.orders.get(intent.client_order_id)
            if intent.action == "submit" and order is not None and order["purpose"] == "entry" and order["status"] == "prepared":
                order["terminal_ns"], order["status"] = now_ns, "local_not_dispatched"
                self.fences[order["instrument_id"]] = now_ns
                canceled.append(intent.client_order_id)
                self.take_entry_timing(intent)
            else:
                retained.append(intent)
        self.outbox = retained
        if self.phase != "recovery_blocked":
            self.phase = "draining"
        for position in self.positions.values():
            position.update(exit_reason="operator_stop", exit_requested_ns=now_ns)
        self.emit("drain_requested", now_ns, reason=reason, local_not_dispatched=canceled)

    def prune(self, now_ns, blocked_symbols=()):
        pass

    def document(self):
        return {"mode": "live", "phase": self.phase, "halt_reason": self.halt_reason,
                "entry_pause_reason": self.entry_pause_reason,
                "account_pauses": deepcopy(self.account_pauses),
                "policy": self.policy.document(), "max_pairs": self.max_pairs,
                "max_gross_notional_usd": _number(self.max_gross),
                "max_unhedged_notional_usd": _number(self.max_unhedged_notional),
                "max_unhedged_secs": self.max_unhedged_ns // 1_000_000_000,
                "gross_exposure_usd": _number(self.gross_exposure()),
                "reserved_gross_usd": _number(self.gross_exposure(reserve=True)),
                "active_pairs": len(self.positions), "closed_pairs": self.completed,
                "realized_net_usd": _number(self.realized_net), "positions": deepcopy(list(self.positions.values())),
                "realized_net_scope": "closed pairs with confirmed commissions",
                "pending_economics": deepcopy(self.pending_economics),
                "legacy_realized_net_usd": self.legacy_realized_net,
                "pending_orders": sum(o["terminal_ns"] is None for o in self.orders.values()),
                "event_counts": dict(self.event_counts),
                "net_native_by_instrument": {i: _number(self.expected_native(i)) for i in self.markets
                    if self.expected_native(i)}, "pnl_excludes": ["funding", "borrow_costs"],
                "profit_requires_confirmed_opening_commission": True}

    def display(self, stream=sys.stdout):
        monitor = getattr(self.store, "loss_monitor", None) or {}
        startup = ""
        if monitor.get("enabled") and monitor.get("baseline_equity_usd") is None:
            reasons = ",".join(f"{venue}:{reason}" for venue, reason in monitor.get("reasons", {}).items())
            startup = f" startup=equity_baseline monitor={monitor.get('status')} waiting={reasons or '-'}"
        stream.write(f"[live-trade] phase={self.phase} pairs={len(self.positions)}/{self.max_pairs} "
                     f"closed={self.completed} confirmed_net=${float(self.realized_net):.6f} "
                     f"pending_economics={len(self.pending_economics)} "
                     f"halt={self.halt_reason or '-'}{startup}\n")
        stream.flush()

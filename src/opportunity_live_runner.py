"""Operator-started multi-venue execution with optional BTC trading self-tests.

Importing or inspecting a plan does not import native code, load credentials,
connect an account, create state, or submit orders. Default CLI operation is a
dry run. The execution path requires an explicit --execute-live flag.
"""
from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass, replace
from decimal import Decimal
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import sys
import time
import tomllib
import uuid

from opportunity_scan import (ScanConfigError, allowed_keys, decimal_value,
                              integer_value, load_plan)
from opportunity_trade import TradePolicy
from opportunity_loss_monitor import LossMonitorPolicy, SessionLossMonitor
from opportunity_timing import add_timing_arguments, run_timed, span, timed

HARD_ORDER_NOTIONAL_USD = Decimal("100")
HARD_MAX_PAIRS = 16
HARD_GROSS_NOTIONAL_USD = Decimal("500")
HARD_STARTUP_NOTIONAL_USD = Decimal("50")
HARD_MAX_UNHEDGED_NOTIONAL_USD = Decimal("100")
HARD_MAX_UNHEDGED_SECS = 10
SUPPORTED_VENUES = {"HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"}
VENUE_ALIASES = {"HYPERLIQUID": "HL", **{v: v for v in SUPPORTED_VENUES}}
CANCEL_REPLACE_VENUES = {"ASTER", "ONDO", "BACKPACK"}




def _plain(value):
    if isinstance(value, (Decimal, Path)):
        return str(value)
    if isinstance(value, dict):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_plain(item) for item in value]
    return value


@dataclass(frozen=True)
class LiveRunPlan:
    scan: object
    policy: TradePolicy
    state_path: Path
    limits_path: Path
    venues: tuple[str, ...]
    trade_symbols: tuple[str, ...]
    max_pairs: int
    max_gross_notional_usd: Decimal
    startup_max_notional_usd: Decimal
    startup_slippage_bps: Decimal
    duration_secs: int
    drain_timeout_secs: int = 30
    timer_interval_ms: int = 100
    account_max_age_ms: int = 15000
    order_timeout_secs: int = 10
    retry_interval_ms: int = 1000
    startup_timeout_secs: int = 20
    limits_present: bool = True
    environment: str = "mainnet"
    startup_self_test: bool = False
    startup_balance_check: bool = False
    max_unhedged_notional_usd: Decimal = HARD_MAX_UNHEDGED_NOTIONAL_USD
    max_unhedged_secs: int = HARD_MAX_UNHEDGED_SECS
    loss_monitor: LossMonitorPolicy = LossMonitorPolicy()

    @property
    def ready(self):
        return self.limits_present and self.max_pairs > 0 and self.max_gross_notional_usd > 0

    def fingerprint(self, account_binding=None):
        document = {"schema_version": 1, "mode": "live", "environment": self.environment,
            "policy": self.policy.document(), "markets": self.scan.document()["markets"],
            "settings": self.scan.document()["settings"], "venues": self.venues,
            "trade_symbols": self.trade_symbols, "max_pairs": self.max_pairs,
            "max_gross_notional_usd": str(self.max_gross_notional_usd),
            "startup_max_notional_usd": str(self.startup_max_notional_usd)}
        # The mandatory 100 USD / 10 second defaults also protect legacy
        # ledgers without breaking their ownership/recovery fingerprint.
        # Explicit tightening is bound to the ledger like other risk limits.
        if self.max_unhedged_notional_usd != Decimal("100") or self.max_unhedged_secs != 10:
            document["max_unhedged_notional_usd"] = str(self.max_unhedged_notional_usd)
            document["max_unhedged_secs"] = self.max_unhedged_secs
        if account_binding is not None:
            document["schema_version"] = 2
            document["account_binding"] = account_binding
        return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()

    def document(self):
        missing = []
        if not self.limits_present:
            missing.append("limits.toml [opportunity_trade]")
        if self.max_pairs <= 0:
            missing.append("risk.max_pairs")
        if self.max_gross_notional_usd <= 0:
            missing.append("risk.max_gross_notional_usd")
        return _plain({"schema_version": 1, "mode": "live", "environment": self.environment,
            "runtime_started": False, "remote_writes_allowed": False,
            "execution_requires_flag": "--execute-live", "ready_for_operator_start": self.ready,
            "required_configuration": missing, "venues": self.venues,
            "execution_route": "Nautilus LiveNode / Strategy / native ExecutionClientFactory",
            "native_dependency_check": "required at execution startup; dry-run only validates configuration",
            "trade_symbols": self.trade_symbols, "policy": self.policy.document(),
            "risk": {"max_pairs": self.max_pairs, "max_gross_notional_usd": self.max_gross_notional_usd,
                "max_unhedged_notional_usd": self.max_unhedged_notional_usd,
                "max_unhedged_secs": self.max_unhedged_secs},
            "loss_monitor": self.loss_monitor.document(),
            "startup": {"self_test_enabled": self.startup_self_test,
                "balance_check_enabled": self.startup_balance_check,
                "mandatory": self.startup_self_test, "symbol": "BTC",
                "account_gate_required": True,
                "max_notional_usd": self.startup_max_notional_usd,
                "sizing": "current native instrument minimum rounded to its lot size",
                "slippage_bps": self.startup_slippage_bps,
                "adjust_modes": {v: "cancel_replace" if v in CANCEL_REPLACE_VENUES else "native" for v in self.venues}},
            "state_path": self.state_path, "limits_path": self.limits_path,
            "duration_secs": self.duration_secs, "drain_timeout_secs": self.drain_timeout_secs,
            "fingerprint": self.fingerprint(), "fingerprint_scope": "configuration_only",
            "account_binding": "resolved and locked at execution startup",
            "scan": self.scan.document()})


@timed("config.live_plan")
def load_live_plan(path, *, symbols=None, max_pairs=None, max_gross_notional_usd=None,
                   duration_secs=None, startup_self_test=None, startup_balance_check=None):
    path = Path(path).resolve()
    with path.open("rb") as handle:
        document = tomllib.load(handle, parse_float=Decimal)
    allowed_keys(document, {"schema_version", "mode", "environment", "scan_config", "state_path",
        "limits_path", "venues", "trade", "risk", "startup", "runtime", "loss_monitor"}, "live root")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ScanConfigError("live schema_version must be 1")
    if document.get("mode") != "live" or document.get("environment") != "mainnet":
        raise ScanConfigError("live runner requires mode=live and environment=mainnet")
    paths = {}
    for name in ("scan_config", "state_path", "limits_path"):
        value = document.get(name)
        if not isinstance(value, str) or not value.strip():
            raise ScanConfigError(f"{name} must be an explicit path")
        paths[name] = (path.parent/value).resolve()
    if paths["state_path"].suffix != ".sqlite3":
        raise ScanConfigError("live state_path must name a .sqlite3 file")
    raw_venues = document.get("venues")
    if not isinstance(raw_venues, list) or not 2 <= len(raw_venues) <= len(SUPPORTED_VENUES) or any(
            not isinstance(v, str) or v not in VENUE_ALIASES for v in raw_venues):
        raise ScanConfigError("live venues require two to five of HYPERLIQUID, LIGHTER, ASTER, ONDO and BACKPACK")
    venues = tuple(VENUE_ALIASES[v] for v in raw_venues)
    if len(set(venues)) != len(venues):
        raise ScanConfigError("live venues must be unique")
    trade = document.get("trade", {})
    allowed_keys(trade, set(TradePolicy.__dataclass_fields__), "live trade")
    policy = TradePolicy(**trade)
    monitor = document.get("loss_monitor", {})
    allowed_keys(monitor, set(LossMonitorPolicy.__dataclass_fields__), "loss_monitor")
    loss_monitor = LossMonitorPolicy(**monitor)
    risk = document.get("risk", {})
    unhedged_keys = {"max_unhedged_notional_usd", "max_unhedged_secs"}
    allowed_keys(risk, {"max_pairs", "max_gross_notional_usd"} | unhedged_keys, "live risk")
    if not {"max_pairs", "max_gross_notional_usd"} <= risk.keys():
        raise ScanConfigError("risk requires explicit max_pairs and max_gross_notional_usd")
    pairs = integer_value(risk["max_pairs"] if max_pairs is None else max_pairs, "max_pairs", 0, 100000)
    gross = decimal_value(risk["max_gross_notional_usd"] if max_gross_notional_usd is None else max_gross_notional_usd,
                          "max_gross_notional_usd", nonnegative=True)
    unhedged = decimal_value(risk.get("max_unhedged_notional_usd", "100"), "max_unhedged_notional_usd", positive=True)
    unhedged_secs = integer_value(risk.get("max_unhedged_secs", 10), "max_unhedged_secs", 1, 86400)
    startup = document.get("startup", {})
    allowed_keys(startup, {"mandatory", "self_test_enabled", "balance_check_enabled", "max_notional_usd", "slippage_bps", "timeout_secs"}, "live startup")
    for key in ("mandatory", "self_test_enabled", "balance_check_enabled"):
        if key in startup and type(startup[key]) is not bool:
            raise ScanConfigError(f"startup.{key} must be a boolean")
    if "mandatory" in startup and "self_test_enabled" in startup and startup["mandatory"] != startup["self_test_enabled"]:
        raise ScanConfigError("startup.mandatory and startup.self_test_enabled conflict")
    # Keep explicit legacy opt-ins working while new configurations default off.
    self_test = startup.get("self_test_enabled", startup.get("mandatory", False))
    if startup_self_test is not None:
        if type(startup_self_test) is not bool:
            raise ScanConfigError("startup_self_test override must be a boolean")
        self_test = startup_self_test
    balance_check = startup.get("balance_check_enabled", False)
    if startup_balance_check is not None:
        if type(startup_balance_check) is not bool:
            raise ScanConfigError("startup_balance_check override must be a boolean")
        balance_check = startup_balance_check
    startup_cap = decimal_value(startup.get("max_notional_usd", "50"), "startup.max_notional_usd", positive=True)
    startup_slip = decimal_value(startup.get("slippage_bps", "20"), "startup.slippage_bps", nonnegative=True)
    if startup_slip > 20:
        raise ScanConfigError("startup slippage cannot exceed 20 bps")
    startup_timeout = integer_value(startup.get("timeout_secs", 20), "startup.timeout_secs", 1, 30)
    limits = {}
    limits_present = False
    if paths["limits_path"].exists():
        with paths["limits_path"].open("rb") as handle:
            limits_document = tomllib.load(handle, parse_float=Decimal)
        if "opportunity_trade" in limits_document:
            limits = limits_document["opportunity_trade"]
            allowed_keys(limits, {"order_notional_usd", "max_pairs", "max_gross_notional_usd",
                                  "startup_max_notional_usd"} | unhedged_keys, "opportunity_trade limits")
            if not {"order_notional_usd", "max_pairs", "max_gross_notional_usd", "startup_max_notional_usd"} <= limits.keys():
                raise ScanConfigError("opportunity_trade limits require all four limits")
            limits_present = True
    order_cap = decimal_value(limits.get("order_notional_usd", "100"), "limit.order_notional_usd", positive=True)
    pairs_cap = integer_value(limits.get("max_pairs", 16), "limit.max_pairs", 0, 100000)
    gross_cap = decimal_value(limits.get("max_gross_notional_usd", "500"), "limit.max_gross_notional_usd", nonnegative=True)
    startup_limit = decimal_value(limits.get("startup_max_notional_usd", "50"), "limit.startup_max_notional_usd", positive=True)
    unhedged_cap = decimal_value(limits.get("max_unhedged_notional_usd", "100"), "limit.max_unhedged_notional_usd", positive=True)
    unhedged_secs_cap = integer_value(limits.get("max_unhedged_secs", 10), "limit.max_unhedged_secs", 1, 86400)
    unhedged = min(unhedged, unhedged_cap, HARD_MAX_UNHEDGED_NOTIONAL_USD)
    unhedged_secs = min(unhedged_secs, unhedged_secs_cap, HARD_MAX_UNHEDGED_SECS)
    policy = replace(policy, order_notional_usd=min(policy.order_notional_usd, order_cap, HARD_ORDER_NOTIONAL_USD))
    pairs, gross = min(pairs, pairs_cap, HARD_MAX_PAIRS), min(gross, gross_cap, HARD_GROSS_NOTIONAL_USD)
    startup_cap = min(startup_cap, startup_limit, HARD_STARTUP_NOTIONAL_USD)
    scan = load_plan(paths["scan_config"])
    if scan.pair_allowlist is not None:
        raise ScanConfigError("schema_version 2 pair allowlists are observation-only; use a trading market configuration")
    filtered = [m for m in scan.markets if m.venue in venues]
    if loss_monitor.enabled and any(m.venue == "HL" and ":" in m.instrument_id for m in filtered):
        raise ScanConfigError("loss monitor cannot value Hyperliquid DEX collateral from native balances")
    counts = Counter(m.symbol for m in filtered)
    available = {symbol for symbol, count in counts.items() if count >= 2}
    if {m.venue for m in filtered} != set(venues):
        raise ScanConfigError("each requested venue needs a configured market for account checks")
    if self_test and {m.venue for m in filtered if m.symbol == "BTC"} != set(venues):
        raise ScanConfigError("each requested venue needs an explicit BTC market for startup checks")
    requested = sorted(available) if symbols is None else symbols.split(",") if isinstance(symbols, str) else list(symbols)
    if not requested or any(not s or s not in available for s in requested) or len(set(requested)) != len(requested):
        raise ScanConfigError("formal trading symbols must have two configured supported venues")
    runtime = document.get("runtime", {})
    allowed_keys(runtime, {"duration_secs", "drain_timeout_secs", "timer_interval_ms", "account_max_age_ms",
                           "order_timeout_secs", "retry_interval_ms"}, "live runtime")
    duration = integer_value(runtime.get("duration_secs", scan.duration_secs) if duration_secs is None else duration_secs,
                             "duration_secs", 0, 2678400)
    anchors = {min((m for m in filtered if m.venue == venue),
                   key=lambda m: (m.symbol != "BTC", m.symbol not in requested, m.instrument_id)).instrument_id for venue in venues}
    scan = replace(scan, markets=tuple(m for m in filtered if m.symbol in set(requested) or m.instrument_id in anchors), output_path=None,
        evaluation_interval_ms=100, duration_secs=duration,
        settings=replace(scan.settings, target_notional=policy.order_notional_usd,
                         min_entry_edge_bps=policy.entry_edge_bps))
    return LiveRunPlan(scan, policy, paths["state_path"], paths["limits_path"], venues, tuple(requested), pairs, gross,
        startup_cap, startup_slip, duration,
        drain_timeout_secs=integer_value(runtime.get("drain_timeout_secs", 30), "drain_timeout_secs", 1, 30),
        timer_interval_ms=integer_value(runtime.get("timer_interval_ms", 100), "timer_interval_ms", 50, 1000),
        account_max_age_ms=integer_value(runtime.get("account_max_age_ms", 15000), "account_max_age_ms", 1, 15000),
        order_timeout_secs=integer_value(runtime.get("order_timeout_secs", 10), "order_timeout_secs", 1, 30),
        retry_interval_ms=integer_value(runtime.get("retry_interval_ms", 1000), "retry_interval_ms", 100, 10000),
        startup_timeout_secs=startup_timeout, limits_present=limits_present,
        startup_self_test=self_test, startup_balance_check=balance_check,
        max_unhedged_notional_usd=unhedged, max_unhedged_secs=unhedged_secs, loss_monitor=loss_monitor)


class NativeBuildError(RuntimeError):
    """Expose a fixed startup stage/code without leaking native error payloads."""
    def __init__(self, error):
        self.error_type = type(error).__name__
        self.code = ("native_session_binding_mismatch"
                     if str(error) == "continuous recovery binding differs from configured run"
                     else "native_node_construction_failed")
        super().__init__(self.code)


class SessionStoreProxy:
    """Keep startup evidence and live ownership in one transactional state row."""
    def __init__(self, store, *, restart_from_flat=False):
        self.store = store
        saved = store.load()
        if saved and (saved.get("schema_version") != 1 or saved.get("mode", "live") != "live"):
            raise ValueError("unknown live session envelope version")
        self.saved = saved
        self.restart_pending = bool(saved and restart_from_flat)
        self.startup = saved.get("startup") if saved else None
        self.startup_complete = bool(saved and saved.get("startup_complete"))
        self.latest_live = saved.get("live") if saved else None
        self.native_shutdown = saved.get("native_shutdown") if saved else None
        self.native_session = None
        self.account_gate = saved.get("account_gate") if saved else None
        self.account_recovery = saved.get("account_recovery") if saved else None
        self.balance_check = saved.get("balance_check") if saved else None
        self.loss_monitor = saved.get("loss_monitor") if saved else None
        self.account_binding = getattr(store, "account_binding", None)
        if saved and self.account_binding is not None and saved.get("account_binding") != self.account_binding:
            raise ValueError("live envelope account binding does not match its ledger")

    def load(self):
        # Keep the prior ledger durable while a fresh, inactive state reads the
        # exchange accounts. Never replay old trading intents during this check.
        return None if self.restart_pending else self.latest_live

    def save(self, snapshot, events):
        self.latest_live = snapshot
        if self.restart_pending:
            self.store.save({**self.saved, "account_recovery": self.account_recovery,
                             "pending_native_session": self.native_session}, events)
            return
        self.store.save(self._document(snapshot), events)

    def _document(self, snapshot):
        return {"schema_version": 1, "mode": "live", "startup_complete": self.startup_complete,
                         "account_binding": self.account_binding,
                         "startup": self.startup, "account_gate": self.account_gate,
                         "account_recovery": self.account_recovery,
                         "balance_check": self.balance_check,
                         "loss_monitor": self.loss_monitor,
                         "native_shutdown": self.native_shutdown,
                         "native_session": self.native_session,
                         "live": snapshot}

    def complete_restart(self, snapshot, now_ns):
        if not self.restart_pending or (self.account_recovery or {}).get("result") != "passed":
            raise ValueError("restart requires fresh complete flat-account proof")
        document = {**self._document(snapshot), "startup_complete": False, "startup": None,
                    "native_shutdown": None, "account_gate": None, "loss_monitor": None}
        # Archive and replace in the same SQLite transaction. Historical fills
        # and unresolved economics stay evidence, not fabricated closing trades.
        self.store.save(document, [{"event": "previous_session_archived", "observed_ns": now_ns,
            "reason": "current_accounts_confirmed_flat", "previous_session": self.saved,
            "account_recovery": self.account_recovery}])
        self.restart_pending = False
        self.startup_complete = False
        self.startup = self.native_shutdown = self.account_gate = self.loss_monitor = None
        self.latest_live = snapshot

    def checkpoint_loss_monitor(self, document, now_ns):
        if document != self.loss_monitor:
            self.loss_monitor = document
            self.save(self.latest_live or {}, [{"event": "session_loss_check", "observed_ns": now_ns,
                                               **document}])

    def checkpoint_balance_check(self, document):
        if document != self.balance_check:
            self.balance_check = document
            self.save(self.latest_live or {}, [{"event": "startup_balance_check", **document,
                "observed_ns": document.get("completed_ns") or document.get("started_ns") or time.time_ns()}])

    def checkpoint_account_gate(self, result, markets, now_ns):
        self.account_gate = {"result": result, "observed_ns": now_ns,
                             "instrument_ids": [m.instrument_id for m in markets]}
        self.save(self.latest_live or {}, [{"event": "startup_account_gate",
                  "result": result, "observed_ns": now_ns,
                  "instrument_ids": self.account_gate["instrument_ids"]}])

    def checkpoint_account_recovery(self, document, now_ns):
        if document != self.account_recovery:
            self.account_recovery = document
            self.save(self.latest_live or {}, [{"event": "startup_account_recovery",
                                               "observed_ns": now_ns, **document}])

    def checkpoint_native_session(self, run_id, directory):
        self.native_session = {"run_id": run_id, "state_directory": str(directory)}
        self.save(self.latest_live or {}, [{"event": "native_session_prepared", "observed_ns": time.time_ns(),
                                           **self.native_session}])

    @timed("startup.checkpoint")
    def checkpoint_startup(self, preflight):
        document = preflight.document()
        if document == self.startup:
            return
        old_timings = {row["request_id"]: row for row in (self.startup or {}).get("operation_timings", [])}
        events = []
        for row in document.get("operation_timings", []):
            if row.get("completed_ns") and row != old_timings.get(row["request_id"]):
                events.append({"event": "startup_operation", "mode": "live",
                               "observed_ns": row["completed_ns"], **row})
        self.startup = document
        self.startup_complete = self.startup.get("result") == "passed"
        self.save(self.latest_live, events)


def native_quote(state, instrument_id, now_ns):
    from opportunity_core import _book_valid, exact_decimal
    book, market = state.books.get(instrument_id), state.markets.get(instrument_id)
    if book is None or market is None or not _book_valid(book, state.plan.settings, now_ns):
        raise ValueError("fresh source book is unavailable")
    # Normalized price = native price * quote_to_usd / canonical_multiplier.
    factor = Fraction(market.canonical_multiplier)/Fraction(market.quote_to_usd)
    return (exact_decimal(Fraction(book.bids[0][0])*factor),
            exact_decimal(Fraction(book.asks[0][0])*factor), book.ts_event_ns)




class LiveCoordinator:
    """Route callbacks and durable intents across startup and continuous trading."""
    def __init__(self, plan, live, proxy, *, self_test_only=False, stop=None, balance_reporter=None,
                 initial_balance=None, loss_reporter=None, account_reporter=None):
        from opportunity_balances import StartupBalanceCheck
        self.plan, self.live, self.proxy = plan, live, proxy
        self.loss_monitor = SessionLossMonitor(getattr(plan, "loss_monitor", LossMonitorPolicy()),
                                               plan.venues, plan.account_max_age_ms)
        self.loss_reporter = loss_reporter or (lambda document: None)
        self.account_reporter = account_reporter or (lambda document: None)
        self.stop_reason = None
        self.balance_check = StartupBalanceCheck(plan.venues, enabled=plan.startup_balance_check,
            timeout_secs=plan.startup_timeout_secs, max_age_ms=plan.account_max_age_ms)
        # Only this startup's isolated read may precede production construction.
        # A saved ledger's balance report is never reused as current evidence.
        if initial_balance is not None:
            from opportunity_balance_runner import balance_session_passed
            if not plan.startup_balance_check or not balance_session_passed(initial_balance, plan.venues):
                raise ValueError("startup balance session lacks complete read-only proof")
            check = initial_balance["balance_check"]
            self.balance_check.result = "passed"
            self.balance_check.started_ns = check["started_ns"]
            self.balance_check.completed_ns = check["completed_ns"]
            self.balance_check.venues = {venue: dict(row) for venue, row in check["venues"].items()}
            self.proxy.checkpoint_balance_check(self.balance_check.document())
        else:
            self.proxy.balance_check = self.balance_check.document()
        self.balance_reporter = balance_reporter or (lambda document: None)
        self.self_test_only, self.stop = self_test_only, stop or (lambda: None)
        self.startup_self_test = self_test_only or plan.startup_self_test
        self.preflight = None
        self.account_markets = ()
        self.gateway = None
        self.native_controls = {}
        self.phase = "waiting_books"
        self.failure = None
        self.started_ns = None
        self.account_recovery_completed_ns = None
        self.account_recovery_after_ns = None
        self.stop_requested_ns = None
        self.finished = False
        if proxy.saved:
            if not proxy.restart_pending:
                raise ValueError("live restart must stage a fresh state before account reconciliation")
            self.phase = "waiting_account_recovery"
        if plan.startup_balance_check and self.balance_check.result != "passed":
            self.after_balance_phase, self.phase = self.phase, "waiting_balances"

    def attach(self, gateway, preflight=None):
        self.gateway, self.preflight = gateway, preflight
        self.account_markets = tuple(min((m for m in self.plan.scan.markets if m.venue == venue),
            key=lambda m: (m.symbol != "BTC", m.symbol not in self.plan.trade_symbols, m.instrument_id)) for venue in self.plan.venues)
        if self.startup_self_test and preflight is None:
            raise ValueError("explicit BTC self-test needs its preflight controller")

    def _startup_intents(self, intents, now_ns):
        self.proxy.checkpoint_startup(self.preflight)
        for intent in intents:
            self.gateway.dispatch(intent, now_ns)

    def _live_intents(self, now_ns):
        for intent in self.live.drain_intents():
            if self.proxy.restart_pending and intent.action != "reconcile":
                raise RuntimeError("startup account recovery cannot dispatch financial intents")
            if self.live.discard_paused_entry(intent, now_ns):
                continue
            self.gateway.dispatch(intent, now_ns)

    @timed("coordinator.receipt")
    def on_receipt(self, receipt, now_ns=None):
        now_ns = time.time_ns() if now_ns is None else now_ns
        if self.proxy.restart_pending and receipt.request_id not in self.live.account_request_ids.values():
            self._restart_activity(now_ns)
            return
        if self.preflight and receipt.request_id in self.preflight.requests:
            snapshot = getattr(self.preflight, "snapshot", None)
            if snapshot is not None:
                try:
                    bid, ask, stamp = native_quote(self.live, snapshot.instrument_id, now_ns)
                    self.preflight.snapshot = replace(snapshot, bid=bid, ask=ask, book_observed_ns=stamp)
                except ValueError:
                    pass  # The preflight's own freshness check still fails closed.
            self._startup_intents(self.preflight.on_receipt(receipt, now_ns), now_ns)
        else:
            self.live.on_receipt(receipt, now_ns)
            self._live_intents(now_ns)

    @timed("coordinator.fill")
    def on_fill(self, fill, now_ns=None):
        now_ns = time.time_ns() if now_ns is None else now_ns
        if self.proxy.restart_pending:
            self._restart_activity(now_ns)
            return
        if self.preflight and fill["client_order_id"] in self.preflight.orders:
            return  # The preflight receives distinct incremental fill receipts.
        self.live.on_fill(fill, now_ns)
        self._live_intents(now_ns)

    def _restart_activity(self, now_ns):
        # A private order/fill callback invalidates earlier flat observations.
        # Re-read current state instead of replaying an archived order lifecycle.
        self.account_recovery_after_ns = max(self.account_recovery_after_ns or 0, now_ns + 1)
        self.live.emit("startup_account_activity", now_ns)

    @timed("coordinator.account", labels=lambda self, snapshot, *a, **kw: (snapshot.venue, ""))
    def on_account(self, snapshot, now_ns=None):
        now_ns = time.time_ns() if now_ns is None else now_ns
        self.live.on_account(snapshot, now_ns)
        if self.phase in {"preflight", "preflight_draining"}:
            self._startup_intents(self.preflight.on_account(snapshot, now_ns), now_ns)
        self._live_intents(now_ns)

    def request_stop(self, now_ns, reason="operator_stop"):
        if self.stop_requested_ns is None:
            self.stop_reason = reason
            self.stop_requested_ns = now_ns
            if self.proxy.restart_pending:
                self.failure = self.failure or "startup account recovery interrupted; previous session retained"
                self.finished = True
                self.stop()
                return
            if self.phase == "waiting_balances":
                self.balance_check.interrupt(now_ns)
                self.proxy.checkpoint_balance_check(self.balance_check.document())
                self.balance_reporter(self.balance_check.document())
                self.failure = self.failure or "startup balance checks interrupted"
            if self.phase == "preflight":
                self.phase = "preflight_draining"
                self.failure = self.failure or "startup interrupted; owned BTC orders/residuals require cleanup"
                self._startup_intents(self.preflight._fail(reason, now_ns), now_ns)
                return
            self.phase = "draining"
            self.live.stop_entries(now_ns, reason=reason)
            self._live_intents(now_ns)

    def _has_residual(self):
        document = self.live.document()
        startup = self.preflight.document() if self.preflight else self.proxy.startup or {}
        return bool(document.get("active_pairs", len(document.get("positions", []))) or
                    document.get("pending_orders", document.get("unresolved_orders", 0)) or
                    Decimal(startup.get("owned_quantity", "0")) or
                    startup.get("unresolved_client_order_ids", []) or
                    any(Decimal(value) for value in startup.get("late_residuals_by_venue", {}).values()))

    @timed("coordinator.tick")
    def tick(self, now_ns):
        if self.finished:
            return
        signal_flag = getattr(self, "stop_signal", None)
        if signal_flag is not None and signal_flag.is_set() and self.stop_requested_ns is None:
            self.request_stop(now_ns)
            if self.finished:
                return
        if self.started_ns is None:
            self.started_ns = now_ns
        if self.phase == "active" and self.loss_monitor.due(now_ns):
            # Fence quote callbacks before processing any pending receipts.
            self.live.set_entry_pause("session_loss_check", now_ns)
        self.gateway.poll(now_ns)
        if self.phase == "waiting_balances":
            self.balance_check.tick(self.gateway, now_ns)
            self.proxy.checkpoint_balance_check(self.balance_check.document())
            if self.balance_check.result == "running":
                return
            self.balance_reporter(self.balance_check.document())
            if self.balance_check.result != "passed":
                self.failure, self.finished = "startup balance reads failed; trading remains inactive", True
                self.stop()
                return
            self.phase = self.after_balance_phase
        if self.phase == "waiting_account_recovery":
            self.live.set_entry_pause("startup_account_recovery", now_ns)
            after_ns = max(self.started_ns, self.account_recovery_after_ns or 0)
            ready = self.live.flat_accounts_ready(now_ns, after_ns) and not self.live.halt_reason
            self._live_intents(now_ns)
            document = {
                "result": "passed" if ready else "checking", "started_ns": self.started_ns,
                "completed_ns": now_ns if ready else None,
                "required_after_ns": after_ns,
                "account_problems": dict(self.live.flat_account_problems),
                "read_started_ns": {scope[0] + (":" + scope[1] if scope[1] else ""):
                    self.live.accounts[self.live._account_representative(instruments)].observed_ns
                    for scope, instruments in self.live.account_scopes.items()
                    if self.live._account_representative(instruments) in self.live.accounts},
            }
            if document != self.proxy.account_recovery:
                self.proxy.checkpoint_account_recovery(document, now_ns)
                self.account_reporter(document)
            if not ready:
                # Reuse account backoff/Retry-After; no timeout can certify flatness.
                return
            self.account_recovery_completed_ns = now_ns
            self.proxy.complete_restart(self.live.snapshot(), now_ns)
            self.live.set_entry_pause(None, now_ns)
            self.phase = "waiting_books"
        if (self.loss_monitor.policy.enabled and self.stop_requested_ns is None
                and (self.phase == "active" or self.loss_monitor.baseline_usd is None)):
            monitor = self.loss_monitor
            monitor.tick(self.gateway, self.live, now_ns, has_residual=self._has_residual())
            self._live_intents(now_ns)
            document = monitor.document()
            if document != self.proxy.loss_monitor:
                self.proxy.checkpoint_loss_monitor(document, now_ns)
                self.loss_reporter(document)
            if monitor.status == "breached":
                # tick only breaches with all accounts flat and original IDs terminal.
                self.request_stop(now_ns, "session_loss_limit")
            elif monitor.baseline_usd is None:
                # No self-test or continuous entry before this run's own baseline.
                return
        startup_wait_started_ns = max(self.balance_check.completed_ns or 0, self.started_ns)
        startup_wait_started_ns = max(startup_wait_started_ns, self.account_recovery_completed_ns or 0)
        startup_wait_started_ns = max(startup_wait_started_ns, self.loss_monitor.baseline_ns or 0)
        if self.phase == "waiting_books":
            try:
                for market in self.preflight.markets if self.startup_self_test else self.account_markets:
                    native_quote(self.live, market.instrument_id, now_ns)
            except ValueError:
                if now_ns-startup_wait_started_ns > self.plan.scan.connection_timeout_secs*1_000_000_000:
                    self.failure, self.finished = "fresh startup books were unavailable before deadline", True
                    self.stop()
                return
            if self.startup_self_test:
                self.phase = "preflight"
                self._startup_intents(self.preflight.start(now_ns), now_ns)
            else:
                self.phase = "waiting_accounts"
                self.proxy.checkpoint_account_gate("checking", self.account_markets, now_ns)
        if self.phase == "waiting_accounts":
            ready = [self.live._account_ready(m.instrument_id, now_ns) for m in self.account_markets]
            self._live_intents(now_ns)
            if all(ready):
                self.proxy.checkpoint_account_gate("passed", self.account_markets, now_ns)
                if self.live.activate(now_ns) is False:
                    self.failure, self.finished = "live ledger refused activation after account checks", True
                    self.stop()
                    return
                self.phase = "active"
            elif now_ns-startup_wait_started_ns >= self.plan.scan.connection_timeout_secs*1_000_000_000:
                self.proxy.checkpoint_account_gate("failed", self.account_markets, now_ns)
                self.failure, self.finished = "authoritative startup accounts did not reconcile before deadline", True
                self.stop()
                return
        if self.phase in {"preflight", "preflight_draining"}:
            if self.phase == "preflight" and self.plan.duration_secs and now_ns-self.started_ns >= self.plan.duration_secs*1_000_000_000:
                self.request_stop(now_ns, "duration_elapsed")
            self._startup_intents(self.preflight.on_timer(now_ns), now_ns)
            if self.preflight.result == "passed":
                if self.stop_requested_ns is not None:
                    self.finished = True
                    self.stop()
                    return
                if self.self_test_only:
                    self.finished, self.phase = True, "self_test_complete"
                    self.stop()
                    return
                if self.live.activate(now_ns) is False:
                    self.failure, self.finished = "live ledger refused activation after startup checks", True
                    self.stop()
                    return
                self.phase = "active"
                self._live_intents(now_ns)
            elif self.preflight.result in {"failed", "blocked"}:
                self.failure = "BTC startup checks did not pass; trading remains inactive"
                # Do not stop cleanup while its confirmed owned residual is draining.
                expired = (self.stop_requested_ns is not None and
                    now_ns-self.stop_requested_ns >= self.plan.drain_timeout_secs*1_000_000_000)
                if self.preflight.stage in {"blocked", "finished"} or expired:
                    self.finished = True
                    self.stop()
                return
            if self.phase == "preflight_draining" and now_ns-self.stop_requested_ns >= self.plan.drain_timeout_secs*1_000_000_000:
                self.failure, self.finished = "startup cleanup deadline reached; BTC ownership remains durable", True
                self.stop()
                return
        if self.phase in {"active", "draining"}:
            if self.phase == "active" and self.plan.duration_secs and now_ns-self.started_ns >= self.plan.duration_secs*1_000_000_000:
                self.request_stop(now_ns, "duration_elapsed")
            self.live.on_evaluation_timer(now_ns)
            self._live_intents(now_ns)
        if self.phase == "draining":
            if not self._has_residual():
                self.finished = True
                self.stop()
            elif now_ns-self.stop_requested_ns >= self.plan.drain_timeout_secs*1_000_000_000:
                self.failure, self.finished = "drain deadline reached; residual ownership remains durable", True
                self.stop()

    def document(self):
        return {"phase": self.phase, "failure": self.failure, "finished": self.finished,
                "stop_reason": self.stop_reason, "loss_monitor": self.loss_monitor.document(),
                "account_binding": self.proxy.account_binding,
                "last_execution_error": getattr(self.gateway, "last_error", None),
                "native_shutdown": self.proxy.native_shutdown,
                "native_session": self.proxy.native_session,
                "startup_self_test_enabled": self.startup_self_test,
                "balance_check": self.balance_check.document(),
                "account_gate": self.proxy.account_gate,
                "account_recovery": self.proxy.account_recovery,
                "startup": self.proxy.startup if self.proxy.restart_pending or (
                    self.preflight and self.preflight.stage == "not_started" and self.proxy.startup_complete) else
                    self.preflight.document() if self.preflight else self.proxy.startup,
                "live": self.live.document()}


@timed("startup.build_live_node")
def build_live_node(plan, live, coordinator, *, environ=None):
    """Build native data/execution clients and one continuous Strategy, without run()."""
    from datetime import timedelta
    from nautilus_trader.common import CacheConfig, Environment, LoggerConfig, LogLevel
    from nautilus_trader.config import LiveRiskEngineConfig
    from nautilus_trader.live import LiveDataEngineConfig, LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import InstrumentId, OmsType, StrategyId, TraderId
    from nautilus_trader.trading import Strategy, StrategyConfig
    from opportunity_native_execution import NativeOrderGateway, execution_clients
    from opportunity_preflight import BtcPreflight, PreflightMarket
    from opportunity_runtime import create_observer, data_clients, instrument_decimal

    builder = (LiveNode.builder("OPPORTUNITY-TRADE", TraderId.from_str("OPPORTUNITY-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.WARNING, fileout_level=LogLevel.OFF, print_config=False))
        .with_cache_config(CacheConfig(tick_capacity=1, bar_capacity=1, save_market_data=False, persist_account_events=False))
        .with_data_engine_config(LiveDataEngineConfig(emit_quotes_from_book=False, emit_quotes_from_book_depths=False))
        .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=False,
            generate_missing_orders=False, inflight_check_interval_ms=0,
            filter_unclaimed_external_orders=True))
        .with_reconciliation(False)
        .with_risk_engine_config(LiveRiskEngineConfig(bypass=False,
            max_notional_per_order={m.instrument_id: str(max(plan.policy.order_notional_usd, plan.startup_max_notional_usd)
                if m.symbol == "BTC" else plan.policy.order_notional_usd) for m in plan.scan.markets}))
        .with_load_state(False).with_save_state(False)
        .with_timeout_connection(plan.scan.connection_timeout_secs)
        .with_timeout_reconciliation(0).with_timeout_portfolio(0)
        .with_timeout_disconnection_secs(30).with_delay_post_stop_secs(0).with_delay_shutdown_secs(0))
    configs = {}
    backpack_quota = None
    if "BACKPACK" in plan.venues:
        from nautilus_trader.adapters.backpack import BackpackQuota
        backpack_quota = BackpackQuota()
    for client_id, factory, config in data_clients(plan.scan, backpack_quota=backpack_quota):
        builder = builder.add_data_client(client_id, factory, config)
        configs[client_id] = config
    native_controls = {}
    coordinator.native_controls = native_controls
    # Each process checks current accounts before trading. Its native owners must
    # use the same fresh run identity, without reopening older bound journals.
    native_directory = plan.state_path.parent / (plan.state_path.stem + ".native") / "sessions" / live.session_id
    coordinator.proxy.checkpoint_native_session(live.session_id, native_directory)
    for client_id, factory, config in execution_clients(plan.scan, plan.environment,
            environ=environ,
            state_directory=native_directory,
            run_id=live.session_id,
            order_notional_usd=max(plan.policy.order_notional_usd, plan.startup_max_notional_usd),
            max_gross_notional_usd=plan.max_gross_notional_usd,
            duration_secs=plan.duration_secs,
            backpack_public_config=configs.get("BACKPACK"), backpack_quota=backpack_quota,
            native_controls=native_controls):
        builder = builder.add_exec_client(client_id, factory, config)
    with span("startup.native_builder"):
        node = builder.build()
    handle = node.handle()
    coordinator.stop = handle.stop

    class ContinuousStrategy(Strategy):
        def __init__(self):
            super().__init__(StrategyConfig(strategy_id=StrategyId.from_str("OPPORTUNITY-TRADE"),
                                            oms_type=OmsType.NETTING,
                                            log_events=False, log_commands=False))

        @timed("startup.strategy")
        def on_start(self):
            try:
                startup_markets = {}
                for market in plan.scan.markets:
                    instrument = self.cache.instrument(InstrumentId.from_str(market.instrument_id))
                    if instrument is None or getattr(instrument, "is_inverse", False):
                        raise ValueError("live instrument is unavailable or inverse")
                    price_step, multiplier = instrument_decimal(instrument.price_increment), instrument_decimal(instrument.multiplier)
                    live.register_instrument(market.instrument_id, price_step, multiplier, str(instrument.quote_currency))
                    if market.symbol == "BTC" and coordinator.startup_self_test:
                        minima = [instrument_decimal(getattr(instrument, name, None)) for name in ("min_quantity", "min_notional")]
                        size_step = instrument_decimal(instrument.size_increment)
                        # A missing separate minimum quantity imposes no larger
                        # floor than the actual published native lot increment.
                        # The native quote-notional floor must remain explicit.
                        if minima[0] is None:
                            minima[0] = size_step
                        if minima[1] is None and market.venue == "ONDO":
                            # Ondo publishes baseIncrement; its continuous native
                            # writer enforces an extra notional floor if published.
                            minima[1] = Decimal(0)
                        if minima[1] is None and market.venue == "BACKPACK":
                            # Backpack /market publishes quantity/price filters,
                            # not a separate notional filter. The native parser
                            # validates minQuantity and stepSize from that response.
                            # https://docs.backpack.exchange/#tag/Markets
                            if instrument_decimal(getattr(instrument, "min_quantity", None)) is None:
                                raise ValueError("Backpack BTC quantity minimum is unavailable")
                            minima[1] = Decimal(0)
                        if any(value is None for value in minima):
                            raise ValueError("BTC venue minimum metadata must be explicit")
                        startup_markets[market.venue] = PreflightMarket(market.venue, market.instrument_id,
                            instrument_decimal(instrument.size_increment), price_step, minima[0], minima[1],
                            multiplier=multiplier, quote_to_usd=market.quote_to_usd,
                            supports_modify=market.venue not in CANCEL_REPLACE_VENUES,
                            modify_mode="cancel_replace" if market.venue in CANCEL_REPLACE_VENUES else "native")
                preflight = BtcPreflight(tuple(startup_markets[v] for v in plan.venues),
                    session_id="btc"+uuid.uuid4().hex[:12], max_notional_usd=plan.startup_max_notional_usd,
                    slippage_bps=plan.startup_slippage_bps, timeout_secs=plan.startup_timeout_secs,
                    max_account_age_ms=plan.account_max_age_ms) if coordinator.startup_self_test else None
                gateway = NativeOrderGateway(self, plan.scan.markets, coordinator.on_receipt,
                    coordinator.on_account, coordinator.on_fill, environment=plan.environment,
                    environ=environ,
                    native_controls=native_controls,
                    timeout_secs=plan.order_timeout_secs,
                    account_timeout_secs=plan.startup_timeout_secs,
                    quote_provider=lambda instrument_id, now_ns: native_quote(live, instrument_id, now_ns))
                coordinator.attach(gateway, preflight)
                self.clock.set_timer("opportunity-trade", timedelta(milliseconds=plan.timer_interval_ms))
            except Exception as exc:
                coordinator.failure = "native strategy startup failed: "+type(exc).__name__
                coordinator.finished = True
                handle.stop()

        def on_time_event(self, event):
            if coordinator.gateway is None:
                return
            try:
                coordinator.tick(time.time_ns())
            except Exception as exc:
                coordinator.failure = "execution callback failed: "+type(exc).__name__
                coordinator.request_stop(time.time_ns(), "execution_callback_failed")

        def on_stop(self):
            if coordinator.gateway:
                coordinator.gateway.close()

    def native_callback(kind):
        def callback(self, event):
            try:
                coordinator.gateway.on_native_event(kind, event, time.time_ns())
            except Exception as exc:
                coordinator.failure = "native order callback failed: "+type(exc).__name__
                coordinator.request_stop(time.time_ns(), "order_callback_failed")
        return callback
    for kind in ("accepted", "updated", "filled", "canceled", "expired", "rejected", "denied",
                 "modify_rejected", "cancel_rejected"):
        setattr(ContinuousStrategy, "on_order_"+kind, native_callback(kind))
    strategy = ContinuousStrategy()
    node.add_strategy(strategy)
    observer = create_observer(plan.scan, live,
        lambda: coordinator.request_stop(time.time_ns(), "market_data_failure"), configs)
    node.add_actor(observer)
    return node, observer, strategy


@timed("run.live")
def run_live(plan, *, self_test_only=False):
    if not plan.ready:
        raise ScanConfigError("set explicit positive risk limits and complete limits.toml before starting execution")
    from dotenv import load_dotenv
    from opportunity_live import LiveTradeState
    from opportunity_trade_store import TradeStore
    from opportunity_account_scope import AccountRiskGuard, resolve_account_binding, verify_execution_identity
    from opportunity_shutdown import collect_native_shutdown
    import signal
    import threading
    load_dotenv(Path(__file__).resolve().parents[1]/".env", override=False)
    # The identity resolver, writers and independent reader share one immutable
    # startup credential snapshot. No later environment fallback may change scope.
    from types import MappingProxyType
    environ = MappingProxyType(dict(os.environ))
    binding = resolve_account_binding(plan.venues, plan.environment, environ)
    initial_balance = None
    if plan.startup_balance_check:
        from opportunity_balance_runner import balance_session_passed, run_startup_balance_session
        initial_balance = run_startup_balance_session(plan, environ)
        print(json.dumps({"event": "startup_balance_check", **initial_balance}), flush=True)
        if not balance_session_passed(initial_balance, plan.venues):
            return 1
    guard = AccountRiskGuard(binding)
    store = None
    coordinator = None
    node = None
    previous_signal = None
    stop_signal = threading.Event()
    release_claim = False
    try:
        store = TradeStore(plan.state_path, plan.fingerprint(binding.document()),
                           account_binding=binding.document())
        guard.claim(store)
        with span("startup.restore_session"):
            proxy = SessionStoreProxy(store, restart_from_flat=True)
            live = LiveTradeState(plan.scan, plan.policy, max_pairs=plan.max_pairs,
                max_gross_notional_usd=plan.max_gross_notional_usd, store=proxy,
                max_unhedged_notional_usd=plan.max_unhedged_notional_usd, max_unhedged_secs=plan.max_unhedged_secs,
                account_max_age_ms=plan.account_max_age_ms, order_timeout_secs=plan.order_timeout_secs,
                retry_interval_ms=plan.retry_interval_ms, trade_symbols=plan.trade_symbols)
            coordinator = LiveCoordinator(plan, live, proxy, self_test_only=self_test_only,
                initial_balance=initial_balance,
                loss_reporter=lambda document: print(json.dumps(
                    {"event": "session_loss_check", **document}), flush=True),
                account_reporter=lambda document: print(json.dumps(
                    {"event": "startup_account_recovery", **document}), flush=True),
                balance_reporter=lambda document: print(json.dumps(
                    {"event": "startup_balance_check", **document}), flush=True))
        coordinator.stop_signal = stop_signal
        verify_execution_identity(binding, environ)
        try:
            node, observer, strategy = build_live_node(plan, live, coordinator, environ=environ)
        except Exception as exc:
            raise NativeBuildError(exc) from None
        # Some native optional settings (notably an absent HL vault) still have
        # environment fallback. Refuse a changed scope before starting clients.
        if resolve_account_binding(plan.venues, plan.environment, os.environ) != binding:
            raise ValueError("execution account environment changed during native construction")
        previous_signal = signal.getsignal(signal.SIGINT)
        signal.signal(signal.SIGINT, lambda *_: stop_signal.set())
        # A native stop can fail without leaving an application-owned pair. Keep
        # its independent account/order/economics proof in the durable envelope.
        try:
            with span("runtime.native_node"):
                node.run()
        except Exception as exc:
            coordinator.failure = "native node failed: " + type(exc).__name__
        proxy.native_shutdown = collect_native_shutdown({
            venue: coordinator.native_controls.get(venue)
            for venue in plan.venues if venue in {"ONDO", "BACKPACK"}})
        if not proxy.native_shutdown["clean"]:
            coordinator.failure = coordinator.failure or "native shutdown lacks complete clean account/order proof"
        if not coordinator.finished:
            coordinator.failure = coordinator.failure or "native node stopped before strategy shutdown completed"
        if coordinator.preflight and not proxy.restart_pending and coordinator.preflight.stage != "not_started":
            proxy.checkpoint_startup(coordinator.preflight)
        proxy.save(live.snapshot(), [])
        release_claim = not (coordinator.failure or coordinator._has_residual() or observer.failure)
        print(json.dumps(coordinator.document(), default=str, indent=2))
        return 0 if release_claim else 1
    finally:
        try:
            try:
                if previous_signal is not None:
                    signal.signal(signal.SIGINT, previous_signal)
            finally:
                try:
                    if coordinator and coordinator.gateway:
                        coordinator.gateway.close()
                finally:
                    if node is not None:
                        with span("shutdown.native_dispose"):
                            node.dispose()
            # Never release a durable claim on crash, incomplete drain, failed
            # persistence, failed gateway close or failed native disposal.
            if release_claim:
                guard.release_clean()
        finally:
            try:
                if store is not None:
                    store.close()
            finally:
                guard.close()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute-live", action="store_true")
    mode.add_argument("--dry-run", action="store_true")
    parser.add_argument("--self-test-only", action="store_true")
    parser.add_argument("--startup-self-test", action="store_true",
                        help="opt in to one BTC trading self-test per venue before continuous trading")
    parser.add_argument("--startup-balance-check", action=argparse.BooleanOptionalAction, default=None,
                        help="read and report native balances before startup trading checks or continuous trading")
    parser.add_argument("--balance-check-only", action="store_true",
                        help="connect read-only account clients, report balances and exit without trading")
    parser.add_argument("--symbols")
    parser.add_argument("--max-pairs", type=int)
    parser.add_argument("--max-gross-notional-usd")
    parser.add_argument("--duration-secs", type=int)
    add_timing_arguments(parser)
    args = parser.parse_args(argv)
    return run_timed(lambda: _run(args), enabled=args.timing, directory=args.timing_dir)


def _run(args):
    try:
        if args.balance_check_only and (args.execute_live or args.startup_self_test or args.self_test_only
                                       or args.startup_balance_check is False):
            raise ScanConfigError("balance-only mode conflicts with trading or disabled balance flags")
        plan = load_live_plan(args.config, symbols=args.symbols, max_pairs=args.max_pairs,
            max_gross_notional_usd=args.max_gross_notional_usd, duration_secs=args.duration_secs,
            startup_self_test=False if args.balance_check_only else True if args.startup_self_test or args.self_test_only else None,
            startup_balance_check=True if args.balance_check_only else args.startup_balance_check)
        if args.balance_check_only:
            if args.dry_run:
                print(json.dumps({"mode": "balance_check_only", "runtime_started": False,
                    "remote_writes_allowed": False, "venues": plan.venues,
                    "startup": {"balance_check_enabled": True, "self_test_enabled": False}}, indent=2))
                return 0
            from opportunity_balance_runner import run_balance_check
            return run_balance_check(plan)
        if not args.execute_live:
            document = plan.document()
            document["self_test_only"] = args.self_test_only
            print(json.dumps(document, indent=2))
            return 0
        return run_live(plan, self_test_only=args.self_test_only)
    except (OSError, ValueError, ImportError, RuntimeError) as exc:
        if isinstance(exc, NativeBuildError):
            print(f"[live-trade] refused: stage=native_build code={exc.code} error_type={exc.error_type}", file=sys.stderr)
        else:
            print("[live-trade] refused: "+type(exc).__name__+"; inspect the validated configuration and durable state", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

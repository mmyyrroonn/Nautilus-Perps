"""Flat-boundary session loss checks with real ledger/coordinator, no network."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_balances import BalanceUnavailable
from opportunity_core import BookSnapshot
from opportunity_live import LiveTradeState
from opportunity_live_runner import LiveCoordinator, SessionStoreProxy, load_live_plan
from opportunity_loss_monitor import LossMonitorPolicy, SessionLossMonitor, flat_equity
from opportunity_preflight import AccountSnapshot
from test_opportunity_live import MemoryStore, state, open_pair, mark, complete_exits, submissions
from test_opportunity_live_runner import NOW, configs
from test_opportunity_trade import SECOND, publish


VENUES = ("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK")


def balance(venue, now, value="1000", currency="USDC"):
    return {"venue": venue, "observed_ns": now, "balances": [
        {"currency": currency, "total": value, "free": value, "locked": "0"}]}


class Gateway:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.sent, self.pending, self.queries = [], [], []
        self.amounts = {v: "1000" for v in VENUES}
        self.bad_balance = self.foreign_position = self.foreign_order = None
        self.query_error = False
        self.balance_stamp = None

    def dispatch(self, intent, now):
        self.sent.append(intent)
        self.pending.append(intent)

    def poll(self, now):
        pending, self.pending = self.pending, []
        for intent in pending:
            assert intent.action == "reconcile", "monitor must never create a financial order"
            positions = (("EXTERNAL." + intent.venue, D(1)),) if self.foreign_position == intent.venue else ()
            orders = (("EXTERNAL." + intent.venue, "external-order"),) if self.foreign_order == intent.venue else ()
            self.coordinator.on_account(AccountSnapshot(intent.venue, intent.instrument_id, D(100), D("100.02"),
                now, D(0), account_positions=positions, account_open_orders=orders), now)

    def query_balance(self, venue):
        self.queries.append(venue)
        if self.query_error:
            raise RuntimeError("PRIVATE URL AND RESPONSE")

    def read_balance(self, venue, now, max_age_ms):
        if self.bad_balance == venue:
            raise RuntimeError("PRIVATE URL AND RESPONSE")
        return balance(venue, now if self.balance_stamp is None else self.balance_stamp, self.amounts[venue])


def setup(tmp_path, *, saved=None):
    plan = replace(load_live_plan(configs(tmp_path, all_five=True, mandatory="false",
        venues='["HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"]')),
        loss_monitor=LossMonitorPolicy(enabled=True))
    store = MemoryStore(saved)
    proxy = SessionStoreProxy(store)
    live = LiveTradeState(plan.scan, plan.policy, max_pairs=plan.max_pairs,
                         max_gross_notional_usd=plan.max_gross_notional_usd, store=proxy,
                         account_max_age_ms=plan.account_max_age_ms)
    for market in plan.scan.markets:
        live.register_instrument(market.instrument_id, D("0.01"), D(1), "USD")
        live.update_book(market.instrument_id, BookSnapshot(market.venue, market.symbol,
            ((D(100), D(10)),), ((D("100.02"), D(10)),), NOW, NOW), NOW, evaluate=False)
    stopped, reports = [], []
    coordinator = LiveCoordinator(plan, live, proxy, stop=lambda: stopped.append(True), loss_reporter=reports.append)
    gateway = Gateway(coordinator)
    coordinator.attach(gateway)
    return coordinator, live, gateway, store, stopped, reports


def start(coordinator):
    coordinator.tick(NOW)
    assert coordinator.loss_monitor.baseline_usd is None
    coordinator.tick(NOW + 1)
    assert coordinator.loss_monitor.baseline_usd == "5000"
    assert coordinator.phase == "active"


def check_due(coordinator):
    due = coordinator.loss_monitor.next_check_ns
    coordinator.tick(due)
    coordinator.tick(due + 1)
    return due + 1


def test_baseline_is_before_activation_and_uses_every_venue_once(tmp_path):
    coordinator, live, gateway, store, stopped, reports = setup(tmp_path)
    start(coordinator)
    assert not live.entry_pause_reason and not stopped
    assert gateway.queries == list(VENUES)
    assert all(intent.action == "reconcile" for intent in gateway.sent)
    doc = store.saved["loss_monitor"]
    assert doc["baseline_equity_usd"] == doc["current_equity_usd"] == "5000"
    assert doc["loss_usd"] == "0" and len(doc["baseline_venues"]) == 5
    assert reports[-1]["status"] == "ready"
    coordinator.tick(coordinator.loss_monitor.next_check_ns - 1)
    assert gateway.queries == list(VENUES) and not live.entry_pause_reason


@pytest.mark.parametrize("hl_amount,breached", [("700.00000001", False), ("700", True), ("699", True)])
def test_total_net_loss_boundary_counts_other_venue_profit_and_exits_flat(tmp_path, hl_amount, breached):
    coordinator, live, gateway, store, stopped, _ = setup(tmp_path)
    start(coordinator)
    gateway.amounts.update(HL=hl_amount, ASTER="1100")
    check_due(coordinator)
    assert coordinator.finished is breached and bool(stopped) is breached
    assert (coordinator.stop_reason == "session_loss_limit") is breached
    assert bool(live.entry_pause_reason) is breached
    assert not live.positions and not live.orders
    assert all(intent.action == "reconcile" for intent in gateway.sent)
    assert store.saved["loss_monitor"]["status"] == ("breached" if breached else "ready")
    assert coordinator.loss_monitor.baseline_usd == "5000"
    if breached:
        queries = list(gateway.queries)
        coordinator.tick(NOW + 2000 * SECOND)
        assert gateway.queries == queries and len(stopped) == 1


def test_monitor_pauses_entries_without_changing_existing_exit_policy():
    live, _ = state()
    position, _ = open_pair(live)
    reason, stopping, phase = position["exit_reason"], live.stopping, live.phase
    monitor = SessionLossMonitor(LossMonitorPolicy(enabled=True), tuple(live.account_domains), 15000)
    monitor.tick(None, live, NOW + SECOND, has_residual=True)
    assert live.entry_pause_reason and monitor.status == "waiting_flat"
    assert (position["exit_reason"], live.stopping, live.phase) == (reason, stopping, phase)
    # Original profitable exits still run while admission is paused.
    now, exits = mark(live, 5)
    assert exits and all(intent.reduce_only for intent in exits)
    complete_exits(live, exits, now)
    assert not live.positions
    publish(live, now + SECOND)
    assert not submissions(live) and not live.positions
    live.set_entry_pause(None, now + SECOND)
    # No other account or halt gate is cleared by releasing this pause.
    live.halt_reason = "existing execution halt"
    publish(live, now + 2 * SECOND)
    assert not submissions(live)


def test_due_check_waits_for_all_pairs_and_original_order_terminals(tmp_path):
    coordinator, live, gateway, _, stopped, _ = setup(tmp_path)
    start(coordinator)
    monitor = coordinator.loss_monitor
    due = monitor.next_check_ns
    # A real ledger entry with no fills remains reserved until terminal evidence.
    live.orders["pending"] = {"terminal_ns": None, "instrument_id": next(iter(live.markets)),
                              "sent_ns": due, "status": "unknown", "purpose": "entry"}
    monitor.tick(gateway, live, due, has_residual=coordinator._has_residual())
    assert monitor.status == "waiting_flat" and live.entry_pause_reason and not stopped
    assert len(gateway.queries) == 5 and monitor.checked_ns == NOW + 1
    live.orders["pending"]["terminal_ns"] = due + SECOND
    monitor.tick(gateway, live, due + SECOND, has_residual=coordinator._has_residual())
    assert monitor.flat_since_ns == due + SECOND and len(gateway.queries) == 5


@pytest.mark.parametrize("fault", ["balance", "query", "foreign_position", "foreign_order", "stale"])
def test_missing_or_unsafe_evidence_holds_admission_and_retries_without_false_loss(tmp_path, fault):
    coordinator, live, gateway, store, stopped, _ = setup(tmp_path)
    start(coordinator)
    if fault == "balance":
        gateway.bad_balance = "ONDO"
    elif fault == "query":
        gateway.query_error = True
    elif fault == "stale":
        gateway.balance_stamp = NOW
    else:
        setattr(gateway, fault, "ASTER")
    checked = coordinator.loss_monitor.checked_ns
    now = check_due(coordinator)
    coordinator.tick(now + 60 * SECOND)
    monitor = coordinator.loss_monitor
    assert monitor.status == "paused" and live.entry_pause_reason and not stopped
    assert monitor.checked_ns == checked and monitor.loss_usd == "0"
    assert "PRIVATE" not in json.dumps(store.saved["loss_monitor"])
    queries = list(gateway.queries)
    coordinator.tick(monitor.next_check_ns - 1)
    assert gateway.queries == queries and live.entry_pause_reason
    gateway.bad_balance = gateway.foreign_position = gateway.foreign_order = gateway.balance_stamp = None
    gateway.query_error = False
    check_due(coordinator)
    assert monitor.status == "ready" and not live.entry_pause_reason


def test_failed_initial_read_never_activates_and_restart_takes_new_baseline(tmp_path):
    coordinator, live, gateway, store, stopped, _ = setup(tmp_path)
    gateway.bad_balance = "LIGHTER"
    coordinator.tick(NOW)
    coordinator.tick(NOW + 1)
    coordinator.tick(NOW + 60 * SECOND)
    assert coordinator.loss_monitor.baseline_usd is None and live.phase != "active"
    assert not stopped and all(i.action == "reconcile" for i in gateway.sent)
    saved = store.saved
    saved["loss_monitor"]["baseline_equity_usd"] = "999999"
    coordinator, live, gateway, store, _, _ = setup(tmp_path, saved=saved)
    start(coordinator)
    assert coordinator.loss_monitor.baseline_usd == "5000"


@pytest.mark.parametrize("fault", ["empty", "nonfinite", "unknown_currency", "missing_currency",
                                  "predates_activity", "future", "duplicate", "inconsistent"])
def test_equity_never_turns_incomplete_or_invalid_rows_into_a_loss(fault):
    row = balance("HL", NOW)
    activity, required = 0, ()
    if fault == "empty":
        row["balances"] = []
    elif fault == "nonfinite":
        row["balances"][0]["total"] = "NaN"
    elif fault == "unknown_currency":
        row["balances"][0]["currency"] = "BTC"
    elif fault == "missing_currency":
        required = ("USDT",)
    elif fault == "predates_activity":
        activity = NOW + 1
    elif fault == "future":
        row["observed_ns"] = NOW + 1
    elif fault == "duplicate":
        row["balances"].append(dict(row["balances"][0]))
    else:
        row["balances"][0]["free"] = "999"
    with pytest.raises(BalanceUnavailable):
        flat_equity(row, "HL", NOW, 15000, activity, required)


def test_equity_uses_total_not_free_and_retains_negative_and_zero_amounts():
    row = balance("ASTER", NOW, "-2", "USDT")
    row["balances"][0].update(free="-5", locked="3")
    row["balances"].append({"currency": "USD1", "total": "10", "free": "10", "locked": "0"})
    assert flat_equity(row, "ASTER", NOW, 15000, 0)["equity_usd"] == "8"
    assert flat_equity(balance("HL", NOW, "0"), "HL", NOW, 15000, 0)["equity_usd"] == "0"


@pytest.mark.parametrize("setting", ['enabled = "true"', 'max_loss_usd = "201"', 'max_loss_usd = "0"',
                                     'max_loss_usd = "NaN"', 'interval_secs = 0', 'interval_secs = true',
                                     'read_timeout_secs = 0'])
def test_invalid_monitor_configuration_is_rejected(tmp_path, setting):
    path = configs(tmp_path)
    path.write_text(path.read_text() + "\n[loss_monitor]\n" + setting + "\n")
    with pytest.raises(ValueError):
        load_live_plan(path)


def test_shipped_monitor_is_enabled_with_user_selected_frequency_and_legacy_identity(tmp_path):
    root = Path(__file__).resolve().parents[1]
    policy = load_live_plan(root / "config/opportunity-trade.live.toml").loss_monitor
    assert policy.enabled and policy.interval_secs == 600 and policy.max_loss_usd == 200
    path = configs(tmp_path)
    old = load_live_plan(path)
    path.write_text(path.read_text() + '\n[loss_monitor]\nenabled = true\n')
    current = load_live_plan(path)
    assert not old.loss_monitor.enabled and current.loss_monitor.enabled
    assert current.fingerprint() == old.fingerprint()

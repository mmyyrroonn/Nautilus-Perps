"""Offline config, startup gating, durable wrapping and native quote routing."""
import builtins
from dataclasses import replace
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import socket
import sys
from types import ModuleType, SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opportunity_core import BookSnapshot
from opportunity_live_runner import (LiveCoordinator, SessionStoreProxy, build_live_node,
                                      load_live_plan, main, native_quote, run_live)
from opportunity_preflight import AccountSnapshot, ExecutionReceipt, OrderIntent
from opportunity_runtime import ScanState
from opportunity_scan import ScanConfigError

NOW = 1_800_000_000_000_000_000


def configs(tmp_path, *, pairs=2, gross="500", venues='["HYPERLIQUID", "LIGHTER", "ASTER"]',
            mandatory="true", limits=True, extra="", all_five=False):
    market_venues = (("HL", "HYPERLIQUID"), ("LIGHTER", "LIGHTER"), ("ASTER", "ASTER"))
    if all_five:
        market_venues += (("ONDO", "ONDO"), ("BACKPACK", "BACKPACK"))
    (tmp_path/"scan.toml").write_text('''schema_version = 1
[recording]
enabled = false
''' + "".join(f'''
[[markets]]
symbol = "{symbol}"
venue = "{venue}"
instrument_id = "{symbol}-PERP.{client}"
quote_to_usd = "1"
valuation_source = "synthetic parity"
''' + ('''[markets.backpack_economics]
margin_init = "0.1"
margin_maint = "0.05"
maker_fee = "0"
taker_fee = "0.0005"
source = "Configured"
source_reference = "offline fixture"
''' if venue == "BACKPACK" else "") for symbol in ("BTC", "ETH")
        for venue, client in market_venues), encoding="utf-8")
    if limits:
        (tmp_path/"limits.toml").write_text('''[opportunity_trade]
order_notional_usd = "100"
max_pairs = 16
max_gross_notional_usd = "500"
startup_max_notional_usd = "50"
''', encoding="utf-8")
    path = tmp_path/"live.toml"
    path.write_text(f'''schema_version = 1
mode = "live"
environment = "mainnet"
scan_config = "scan.toml"
state_path = "state/live.sqlite3"
limits_path = "limits.toml"
venues = {venues}
[trade]
order_notional_usd = "100"
[risk]
max_pairs = {pairs}
max_gross_notional_usd = "{gross}"
[startup]
mandatory = {mandatory}
max_notional_usd = "50"
[runtime]
duration_secs = 0
{extra}
''', encoding="utf-8")
    return path




def test_default_cli_is_dry_run_without_credentials_native_socket_or_state(tmp_path, monkeypatch, capsys):
    path = configs(tmp_path)
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith(("dotenv", "nautilus_trader", "opportunity_live", "opportunity_native_execution",
                            "opportunity_trade_store")):
            raise AssertionError("inspection attempted an execution import")
        return original_import(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("inspection attempted a side effect")
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    assert main(["--config", str(path), "--symbols", "ETH"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["mode"] == "live" and document["ready_for_operator_start"]
    assert document["runtime_started"] is False and document["remote_writes_allowed"] is False
    assert document["trade_symbols"] == ["ETH"]
    assert {m["symbol"] for m in document["scan"]["markets"]} == {"BTC", "ETH"}
    assert document["risk"] == {"max_pairs": 2, "max_gross_notional_usd": "500",
                                "max_unhedged_notional_usd": "100", "max_unhedged_secs": 10}
    assert not (tmp_path/"state").exists()


def test_five_venues_keep_btc_checks_when_trading_only_eth(tmp_path):
    plan = load_live_plan(configs(tmp_path, all_five=True,
        venues='["HL","LIGHTER","ASTER","ONDO","BACKPACK"]'), symbols="ETH")
    assert plan.venues == ("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK")
    assert plan.trade_symbols == ("ETH",) and len(plan.scan.markets) == 10
    document = plan.document()
    assert document["ready_for_operator_start"]
    assert document["startup"]["adjust_modes"] == {"HL": "native", "LIGHTER": "native",
        "ASTER": "cancel_replace", "ONDO": "cancel_replace", "BACKPACK": "cancel_replace"}
    assert not (tmp_path / "state").exists()


def test_shipped_live_plan_selects_all_five_native_venues():
    plan = load_live_plan(ROOT / "config/opportunity-trade.live.toml")
    assert set(plan.venues) == {"HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"}
    assert len(plan.trade_symbols) > 2 and plan.ready
    assert plan.policy.order_notional_usd == 100 and plan.max_gross_notional_usd == 500
    assert plan.startup_timeout_secs == 20 and plan.order_timeout_secs == 10
    assert plan.startup_self_test is False
    assert plan.document()["startup"]["account_gate_required"] is True








def test_explicit_risk_zero_refuses_execution_before_reading_credentials(tmp_path, monkeypatch):
    path = configs(tmp_path, pairs=0, gross="0")
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith(("dotenv", "nautilus_trader", "opportunity_live", "opportunity_trade_store")):
            raise AssertionError("unresolved risk attempted execution")
        return original_import(name, *args, **kwargs)
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    assert main(["--config", str(path), "--execute-live"]) == 2
    assert not (tmp_path/"state").exists()


@pytest.mark.parametrize("kwargs", [
    {"venues": '["HYPERLIQUID", "ONDO"]'}, {"venues": '["HYPERLIQUID", "BACKPACK"]'},
    {"venues": '["HYPERLIQUID", "HL"]'}, {"mandatory": '"false"'},
    {"pairs": "true"}, {"gross": "NaN"}, {"extra": "private_key = 'forbidden'"},
])
def test_invalid_or_unsupported_config_never_creates_runtime(tmp_path, kwargs):
    with pytest.raises(ScanConfigError):
        load_live_plan(configs(tmp_path, **kwargs))
    assert not (tmp_path/"state").exists()


def test_limits_and_code_caps_can_only_reduce_user_requested_size(tmp_path):
    path = configs(tmp_path, pairs=999, gross="9000")
    (tmp_path/"limits.toml").write_text('''[opportunity_trade]
order_notional_usd = "80"
max_pairs = 3
max_gross_notional_usd = "350"
startup_max_notional_usd = "25"
''', encoding="utf-8")
    plan = load_live_plan(path)
    assert plan.policy.order_notional_usd == 80 and plan.max_pairs == 3
    assert plan.max_gross_notional_usd == 350 and plan.startup_max_notional_usd == 25
    (tmp_path/"limits.toml").write_text('''[opportunity_trade]
order_notional_usd = "1000"
max_pairs = 1000
max_gross_notional_usd = "9000"
startup_max_notional_usd = "1000"
''', encoding="utf-8")
    plan = load_live_plan(path)
    assert plan.policy.order_notional_usd == 100 and plan.max_pairs == 16
    assert plan.max_gross_notional_usd == 500 and plan.startup_max_notional_usd == 50


@pytest.mark.parametrize("requested,cap,expected", [
    (("75", 8), ("60", 6), (D(60), 6)),
    (("40", 3), ("60", 6), (D(40), 3)),
    (("9000", 300), ("9000", 300), (D(100), 10)),
])
def test_unhedged_risk_configuration_can_only_tighten_caps(tmp_path, requested, cap, expected):
    path = configs(tmp_path)
    baseline = load_live_plan(path)
    path.write_text(path.read_text().replace("[startup]", f'''max_unhedged_notional_usd = "{requested[0]}"
max_unhedged_secs = {requested[1]}
[startup]'''), encoding="utf-8")
    limits = tmp_path / "limits.toml"
    limits.write_text(limits.read_text() + f'''max_unhedged_notional_usd = "{cap[0]}"
max_unhedged_secs = {cap[1]}
''', encoding="utf-8")
    plan = load_live_plan(path)
    assert (plan.max_unhedged_notional_usd, plan.max_unhedged_secs) == expected
    assert (plan.fingerprint() == baseline.fingerprint()) == (expected == (D(100), 10))
    assert plan.document()["risk"]["max_unhedged_secs"] == expected[1]


def test_default_unhedged_limits_preserve_legacy_live_fingerprint(tmp_path):
    plan = load_live_plan(configs(tmp_path))
    legacy = {"schema_version": 1, "mode": "live", "environment": plan.environment,
        "policy": plan.policy.document(), "markets": plan.scan.document()["markets"],
        "settings": plan.scan.document()["settings"], "venues": plan.venues,
        "trade_symbols": plan.trade_symbols, "max_pairs": plan.max_pairs,
        "max_gross_notional_usd": str(plan.max_gross_notional_usd),
        "startup_max_notional_usd": str(plan.startup_max_notional_usd)}
    assert plan.fingerprint() == hashlib.sha256(json.dumps(legacy, sort_keys=True).encode()).hexdigest()


@pytest.mark.parametrize("section", ["risk", "limits"])
@pytest.mark.parametrize("setting", [
    'max_unhedged_notional_usd = "0"', 'max_unhedged_notional_usd = "-1"',
    'max_unhedged_notional_usd = "NaN"', 'max_unhedged_notional_usd = "Infinity"',
    'max_unhedged_notional_usd = true', 'max_unhedged_secs = 0',
    'max_unhedged_secs = true', 'max_unhedged_secs = 1.5',
])
def test_unhedged_config_rejects_invalid_thresholds(tmp_path, section, setting):
    path = configs(tmp_path)
    if section == "risk":
        path.write_text(path.read_text().replace("[startup]", setting + "\n[startup]"), encoding="utf-8")
    else:
        limits = tmp_path / "limits.toml"
        limits.write_text(limits.read_text() + setting + "\n", encoding="utf-8")
    with pytest.raises(ScanConfigError):
        load_live_plan(path)


def test_missing_limits_section_can_be_inspected_but_is_not_ready(tmp_path):
    plan = load_live_plan(configs(tmp_path, limits=False))
    assert not plan.ready
    assert "limits.toml [opportunity_trade]" in plan.document()["required_configuration"]


def test_cli_risk_override_and_symbol_filter_keep_required_btc_subscriptions(tmp_path):
    path = configs(tmp_path, pairs=0, gross="0")
    plan = load_live_plan(path, symbols="ETH", max_pairs=2, max_gross_notional_usd="500", duration_secs=10)
    assert plan.ready and plan.trade_symbols == ("ETH",)
    assert {m.symbol for m in plan.scan.markets} == {"ETH", "BTC"}
    assert plan.duration_secs == 10
    with pytest.raises(ScanConfigError):
        load_live_plan(path, symbols="UNKNOWN")


@pytest.mark.parametrize("flag", ["--startup-self-test", "--self-test-only"])
def test_optional_self_test_cli_is_explicit_and_inspection_has_no_side_effects(tmp_path, capsys, flag):
    path = configs(tmp_path, mandatory="false")
    assert main(["--config", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["startup"]["self_test_enabled"] is False
    assert main(["--config", str(path), flag]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["startup"]["self_test_enabled"] is True
    assert document["self_test_only"] == (flag == "--self-test-only")
    assert document["remote_writes_allowed"] is False and not (tmp_path / "state").exists()


def test_new_toggle_defaults_off_and_legacy_explicit_boolean_is_supported(tmp_path):
    path = configs(tmp_path)
    assert load_live_plan(path).startup_self_test is True
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("mandatory = true", ""), encoding="utf-8")
    assert load_live_plan(path).startup_self_test is False
    path.write_text(original.replace("mandatory = true", "self_test_enabled = false"), encoding="utf-8")
    assert load_live_plan(path).startup_self_test is False
    path.write_text(original.replace("mandatory = true", "mandatory = true\nself_test_enabled = false"), encoding="utf-8")
    with pytest.raises(ScanConfigError, match="conflict"):
        load_live_plan(path)


def test_disabling_trading_self_test_does_not_require_btc_markets(tmp_path):
    path = configs(tmp_path, mandatory="false")
    scan_path = tmp_path / "scan.toml"
    scan_path.write_text(scan_path.read_text(encoding="utf-8").replace('symbol = "BTC"', 'symbol = "SOL"'),
                         encoding="utf-8")
    plan = load_live_plan(path, symbols="ETH")
    assert plan.trade_symbols == ("ETH",) and not plan.startup_self_test
    assert {m.symbol for m in plan.scan.markets} == {"ETH"}
    with pytest.raises(ScanConfigError, match="BTC market"):
        load_live_plan(path, symbols="ETH", startup_self_test=True)


def readonly_coordinator(tmp_path, *, saved=None, all_five=False):
    from opportunity_live import LiveTradeState
    plan = load_live_plan(configs(tmp_path, mandatory="false", all_five=all_five,
        venues='["HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"]' if all_five else '["HL", "LIGHTER", "ASTER"]'))
    store = MemoryStore(saved)
    proxy = SessionStoreProxy(store, restart_from_flat=True)
    live = LiveTradeState(plan.scan, plan.policy, max_pairs=plan.max_pairs,
                         max_gross_notional_usd=plan.max_gross_notional_usd, store=proxy,
                         account_max_age_ms=plan.account_max_age_ms)
    for market in plan.scan.markets:
        live.register_instrument(market.instrument_id, D("0.01"), D(1), "USD")
        live.update_book(market.instrument_id, BookSnapshot(market.venue, market.symbol,
            ((D(100), D(10)),), ((D("100.02"), D(10)),), NOW, NOW), NOW, evaluate=False)
    stopped = []
    state = LiveCoordinator(plan, live, proxy, stop=lambda: stopped.append(True))
    gateway = FakeGateway()
    state.attach(gateway)
    return state, live, gateway, store, stopped


def test_no_trading_self_test_still_requires_each_venue_account_proof(tmp_path):
    state, live, gateway, store, stopped = readonly_coordinator(tmp_path)
    state.tick(NOW)
    assert state.phase == "waiting_accounts" and live.phase != "active"
    assert len(gateway.sent) == len(state.plan.venues)
    assert all(i.action == "reconcile" for i in gateway.sent)
    for index, market in enumerate(state.account_markets):
        state.on_account(AccountSnapshot(market.venue, market.instrument_id, D(100), D("100.02"),
                         NOW + 1, D(0)), NOW + 1)
        if index < len(state.account_markets) - 1:
            state.tick(NOW + 1)
            assert state.phase == "waiting_accounts"
    state.tick(NOW + 2)
    assert state.phase == "active" and live.phase == "active" and not stopped
    assert store.saved["startup"] is None and store.saved["startup_complete"] is False
    assert store.saved["account_gate"]["result"] == "passed"
    for stamp in (NOW + 3, NOW + 4, NOW + 5):
        state.tick(stamp)
    assert live.event_counts["strategy_enabled"] == 1
    assert not live.orders and all(i.action == "reconcile" for i in gateway.sent)


@pytest.mark.parametrize("bad", ["stale", "position", "open_orders", "non_authoritative", "disconnected"])
def test_bad_account_proof_cannot_be_skipped_when_trading_self_test_is_off(tmp_path, bad):
    state, live, gateway, store, stopped = readonly_coordinator(tmp_path)
    state.tick(NOW)
    for index, market in enumerate(state.account_markets):
        values = {}
        if index == 0:
            values = {"stale": {"observed_ns": NOW - 20_000_000_000},
                      "position": {"position_quantity": D("0.1")},
                      "open_orders": {"open_order_ids": ("external-order",)},
                      "non_authoritative": {"authoritative": False},
                      "disconnected": {"connected": False}}[bad]
        snapshot = AccountSnapshot(market.venue, market.instrument_id, D(100), D("100.02"), NOW, D(0))
        state.on_account(replace(snapshot, **values), NOW)
    state.tick(NOW + 1)
    assert state.phase == "waiting_accounts" and live.phase != "active"
    state.tick(NOW + state.plan.scan.connection_timeout_secs * 1_000_000_000)
    assert state.finished and state.failure and stopped
    assert store.saved["account_gate"]["result"] == "failed"
    assert not live.orders and all(i.action == "reconcile" for i in gateway.sent)


@pytest.mark.parametrize("journal", [
    {"result": "blocked", "owned_quantity": "0.001"},
    {"result": "running", "owned_quantity": "0", "unresolved_client_order_ids": ["original-btc-id"]},
])
def test_interrupted_btc_journal_waits_for_current_account_proof(tmp_path, journal):
    saved = {"schema_version": 1, "startup_complete": False, "startup": journal,
             "account_gate": {"result": "passed"}, "live": {"positions": [], "orders": {}}}
    path = configs(tmp_path, mandatory="false")
    state, live, preflight, gateway, store, stopped = coordinator(tmp_path, saved=saved, plan_path=path)
    state.tick(NOW)
    assert state.phase == "waiting_account_recovery" and not state.failure and not stopped
    assert not gateway.sent and not live.activated and preflight.started == 0
    assert state.document()["startup"] == journal and store.saved["startup"] == journal


@pytest.mark.parametrize("gate_result", ["checking", "passed"])
def test_readonly_startup_can_restart_without_trading_self_test(tmp_path, gate_result):
    saved = {"schema_version": 1, "startup_complete": False, "startup": None,
             "account_gate": {"result": gate_result},
             "live": {"positions": [], "orders": {}, "executions": {}, "request_sequence": 3}}
    path = configs(tmp_path, mandatory="false")
    state, live, preflight, gateway, _, stopped = coordinator(tmp_path, saved=saved, plan_path=path)
    live._account_ready = lambda instrument_id, now_ns: False
    state.tick(NOW)
    assert state.phase == "waiting_account_recovery" and not state.failure and not stopped
    assert preflight.started == 0 and not live.activated and not gateway.sent


def test_readonly_session_restart_requires_new_process_account_evidence(tmp_path):
    state, live, _, store, _ = readonly_coordinator(tmp_path)
    state.tick(NOW)
    for market in state.account_markets:
        state.on_account(AccountSnapshot(market.venue, market.instrument_id, D(100), D("100.02"), NOW, D(0)), NOW)
    state.tick(NOW + 1)
    assert state.phase == "active" and store.saved["account_gate"]["result"] == "passed"
    resumed, resumed_live, gateway, _, stopped = readonly_coordinator(tmp_path, saved=store.saved)
    resumed.tick(NOW + 2)
    assert resumed.phase == "waiting_account_recovery" and resumed_live.phase != "active"
    assert len(gateway.sent) == len(resumed.plan.venues)
    assert all(i.action == "reconcile" for i in gateway.sent)
    assert not stopped and resumed.preflight is None


@pytest.mark.parametrize("blocker", ["pending_order", "dirty_native", "halted_ledger"])
def test_historical_execution_blockers_wait_for_current_account_proof(tmp_path, blocker):
    saved = {"schema_version": 1, "startup_complete": False, "startup": None,
             "account_gate": {"result": "passed"}, "live": {"positions": [], "orders": {}}}
    if blocker == "pending_order":
        saved["live"]["orders"]["original-order"] = {"terminal_ns": None}
    elif blocker == "dirty_native":
        saved["native_shutdown"] = {"clean": False}
    else:
        saved["live"]["halt_reason"] = "ledger requires reconciliation"
    path = configs(tmp_path, mandatory="false")
    state, live, preflight, gateway, _, stopped = coordinator(tmp_path, saved=saved, plan_path=path)
    state.tick(NOW)
    assert state.phase == "waiting_account_recovery" and not state.failure and not stopped
    assert not live.activated and not gateway.sent and preflight.started == 0


def test_self_test_only_forces_opt_in_even_when_config_defaults_off(tmp_path):
    path = configs(tmp_path, mandatory="false")
    state, live, preflight, gateway, _, stopped = coordinator(tmp_path, self_test_only=True, plan_path=path)
    state.tick(NOW)
    assert state.phase == "preflight" and preflight.started == 1
    assert [i.action for i in gateway.sent] == ["connect"]
    preflight.result, preflight.stage = "passed", "finished"
    state.tick(NOW + 1)
    assert state.phase == "self_test_complete" and stopped and not live.activated


class MemoryStore:
    def __init__(self, saved=None):
        self.saved, self.writes = saved, []
    def load(self):
        return self.saved
    def save(self, snapshot, events):
        self.saved = json.loads(json.dumps(snapshot))
        self.writes.append((self.saved, list(events)))


class FakeLive(ScanState):
    def __init__(self, scan):
        super().__init__(scan)
        self.session_id = "offline-session"
        self.activated = []
        self.accounts = []
        self.receipts = []
        self.fills = []
        self.intents = []
        self.active_pairs = 0
        self.stops = []
        self.instruments = []
        self.account_scopes = {}
        self.flat_account_problems = {}
        self.halt_reason = None
    def set_entry_pause(self, reason, now):
        self.entry_pause_reason = reason
    def flat_accounts_ready(self, now, after):
        return False
    def register_instrument(self, *args):
        self.instruments.append(args)
    def activate(self, now):
        self.activated.append(now)
    def on_account(self, snapshot, now):
        self.accounts.append(snapshot)
    def on_receipt(self, receipt, now):
        self.receipts.append(receipt)
    def on_fill(self, fill, now):
        self.fills.append(fill)
    def drain_intents(self):
        result, self.intents = tuple(self.intents), []
        return result
    def discard_paused_entry(self, intent, now):
        return False
    def stop_entries(self, now, reason):
        self.stops.append((now, reason))
    def document(self):
        return {"active_pairs": self.active_pairs, "positions": []}


class FakePreflight:
    def __init__(self, markets):
        self.markets = markets
        self.result, self.stage = "running", "not_started"
        self.requests, self.orders = {}, {}
        self.started = 0
        self.owned_quantity = "0"
    def start(self, now):
        self.started += 1
        self.stage = "connecting"
        market = self.markets[0]
        intent = OrderIntent("connect", market.venue, market.instrument_id, "self-read")
        self.requests[intent.request_id] = intent
        return (intent,)
    def on_timer(self, now):
        return ()
    def on_receipt(self, receipt, now):
        return ()
    def on_account(self, snapshot, now):
        self.result, self.stage = "passed", "finished"
        return ()
    def document(self):
        return {"result": self.result, "stage": self.stage, "owned_quantity": self.owned_quantity}
    def _fail(self, reason, now):
        self.stage = "cleanup_reconcile"
        market = self.markets[0]
        intent = OrderIntent("reconcile", market.venue, market.instrument_id, "stop-read")
        self.requests[intent.request_id] = intent
        return (intent,)


class FakeGateway:
    def __init__(self):
        self.sent = []
    def dispatch(self, intent, now):
        self.sent.append(intent)
    def poll(self, now):
        pass


def coordinator(tmp_path, *, saved=None, self_test_only=False, plan_path=None):
    plan = load_live_plan(configs(tmp_path) if plan_path is None else plan_path)
    live = FakeLive(plan.scan)
    for market in plan.scan.markets:
        if market.symbol == "BTC":
            live.update_book(market.instrument_id, BookSnapshot(market.venue, "BTC",
                ((D(100), D(10)),), ((D("100.02"), D(10)),), NOW, NOW), NOW, evaluate=False)
    store = MemoryStore(saved)
    proxy = SessionStoreProxy(store, restart_from_flat=True)
    stopped = []
    result = LiveCoordinator(plan, live, proxy, self_test_only=self_test_only, stop=lambda: stopped.append(True))
    preflight = FakePreflight(tuple(m for m in plan.scan.markets if m.symbol == "BTC"))
    gateway = FakeGateway()
    result.attach(gateway, preflight)
    return result, live, preflight, gateway, store, stopped


def test_live_activation_requires_passed_startup_and_account_is_cached_first(tmp_path):
    state, live, preflight, gateway, store, _ = coordinator(tmp_path)
    state.tick(NOW)
    assert state.phase == "preflight" and not live.activated
    assert len(gateway.sent) == 1 and gateway.sent[0].action == "connect"
    assert store.writes[-1][0]["startup"]["result"] == "running"
    market = preflight.markets[0]
    snapshot = AccountSnapshot(market.venue, market.instrument_id, D(100), D("100.02"), NOW+1, D(0))
    state.on_account(snapshot, NOW+1)
    assert live.accounts == [snapshot]
    assert store.saved["startup_complete"]
    assert not live.activated
    state.tick(NOW+2)
    assert live.activated == [NOW+2] and state.phase == "active"


def test_failed_startup_blocks_continuous_trading_without_skipping_venue(tmp_path):
    state, live, preflight, _, _, stopped = coordinator(tmp_path)
    state.tick(NOW)
    preflight.result, preflight.stage = "blocked", "blocked"
    state.tick(NOW+1)
    assert not live.activated and state.failure and stopped


def test_self_test_only_is_optional_and_does_not_activate_continuous_trading(tmp_path):
    state, live, preflight, _, _, stopped = coordinator(tmp_path, self_test_only=True)
    state.tick(NOW)
    preflight.result, preflight.stage = "passed", "finished"
    state.tick(NOW+1)
    assert state.phase == "self_test_complete" and stopped and not live.activated


def test_outer_persistence_preserves_startup_and_live_ownership_together():
    store = MemoryStore()
    proxy = SessionStoreProxy(store)
    preflight = FakePreflight(())
    proxy.save({"positions": [{"client_order_id": "own"}]}, [{"event": "intent"}])
    proxy.checkpoint_startup(preflight)
    resumed = SessionStoreProxy(store)
    assert resumed.load() == {"positions": [{"client_order_id": "own"}]}
    assert resumed.startup["result"] == "running" and not resumed.startup_complete
    assert store.saved["mode"] == "live"


@pytest.mark.parametrize("startup_complete", [False, True])
def test_restart_never_repeats_btc_risk_creation_before_reconciliation(tmp_path, startup_complete):
    saved = {"schema_version": 1, "startup_complete": startup_complete,
             "startup": {"result": "passed" if startup_complete else "blocked", "owned_quantity": "0.001"},
             "live": {"positions": []}}
    state, live, preflight, gateway, _, stopped = coordinator(tmp_path, saved=saved)
    state.tick(NOW)
    assert preflight.started == 0 and not gateway.sent and not live.activated
    assert state.phase == "waiting_account_recovery" and not state.failure and not stopped


def test_stop_drains_owned_residuals_and_keeps_nonzero_result_on_deadline(tmp_path):
    state, live, preflight, _, _, stopped = coordinator(tmp_path)
    state.tick(NOW)
    preflight.result = "passed"
    state.tick(NOW+1)
    live.active_pairs = 1
    state.request_stop(NOW+2)
    assert live.stops == [(NOW+2, "operator_stop")]
    state.tick(NOW+29_000_000_000)
    assert not stopped
    state.tick(NOW+30_000_000_002)
    assert stopped and state.failure and live.active_pairs == 1


def test_interrupt_during_preflight_keeps_btc_residual_and_cleanup_active(tmp_path):
    state, live, preflight, gateway, store, stopped = coordinator(tmp_path)
    state.tick(NOW)
    preflight.owned_quantity = "0.001"
    state.request_stop(NOW+1)
    assert state.phase == "preflight_draining"
    assert gateway.sent[-1].action == "reconcile"
    assert store.saved["startup"]["owned_quantity"] == "0.001"
    assert state._has_residual() and not live.activated and not stopped
    state.tick(NOW+30_000_000_001)
    assert stopped and state.failure and state._has_residual()


def test_resume_document_retains_saved_btc_residual_evidence(tmp_path):
    saved = {"schema_version": 1, "startup_complete": False,
        "startup": {"result": "blocked", "owned_quantity": "0.001"}, "live": {"positions": []}}
    state, _, _, _, store, _ = coordinator(tmp_path, saved=saved)
    state.tick(NOW)
    assert state.document()["startup"]["owned_quantity"] == "0.001"
    assert store.saved["startup"] == saved["startup"]


def test_quote_provider_reverses_fx_and_canonical_normalization(tmp_path):
    plan = load_live_plan(configs(tmp_path))
    first = replace(plan.scan.markets[0], quote_to_usd=D("1.01"), canonical_multiplier=D(1000))
    live = FakeLive(replace(plan.scan, markets=(first,)))
    live.update_book(first.instrument_id, BookSnapshot(first.venue, "BTC",
        ((D("0.101"), D(10)),), ((D("0.10201"), D(10)),), NOW, NOW), NOW, evaluate=False)
    bid, ask, stamp = native_quote(live, first.instrument_id, NOW)
    assert bid == 100 and ask == 101 and stamp == NOW
    with pytest.raises(ValueError):
        native_quote(live, first.instrument_id, NOW+3_000_000_000)


def test_fill_callback_routes_preflight_and_live_ids_separately(tmp_path):
    state, live, preflight, _, _, _ = coordinator(tmp_path)
    preflight.orders["probe"] = {}
    state.on_fill({"client_order_id": "probe"}, NOW)
    assert not live.fills
    state.on_fill({"client_order_id": "live-owned"}, NOW)
    assert live.fills == [{"client_order_id": "live-owned"}]
    state.on_receipt(ExecutionReceipt("live-owned-request", "live-owned", "accepted"), NOW)
    assert len(live.receipts) == 1


def test_receipt_uses_current_bbo_without_retimestamping_native_account(tmp_path):
    state, live, preflight, _, _, _ = coordinator(tmp_path)
    market = preflight.markets[0]
    preflight.requests["probe"] = object()
    preflight.snapshot = AccountSnapshot(market.venue, market.instrument_id,
        D(100), D("100.02"), NOW, D(0))
    current = NOW + 3_000_000_000
    live.update_book(market.instrument_id, BookSnapshot(market.venue, "BTC",
        ((D(101), D(10)),), ((D("101.02"), D(10)),), current, current), current, evaluate=False)
    state.on_receipt(ExecutionReceipt("probe", "probe", "accepted"), current)
    assert preflight.snapshot.observed_ns == NOW
    assert preflight.snapshot.book_observed_ns == current
    assert preflight.snapshot.bid == 101


def test_dirty_native_shutdown_waits_for_current_accounts_even_with_flat_ledger(tmp_path):
    saved = {"schema_version": 1, "startup_complete": True,
        "startup": {"result": "passed", "owned_quantity": "0"},
        "native_shutdown": {"clean": False, "venues": {"BACKPACK": {"clean": False}}},
        "live": {"positions": [], "orders": {}}}
    state, live, preflight, gateway, _, stopped = coordinator(tmp_path, saved=saved)
    state.tick(NOW)
    assert state.phase == "waiting_account_recovery" and not state.failure and not stopped
    assert not gateway.sent and not live.activated and preflight.started == 0


def test_unused_session_after_config_failure_waits_for_current_accounts(tmp_path):
    saved = {"schema_version": 1, "startup_complete": False, "startup": None,
        "live": {"positions": [], "orders": {}, "executions": {}, "request_sequence": 0}}
    state, live, preflight, gateway, _, stopped = coordinator(tmp_path, saved=saved)
    state.tick(NOW)
    assert state.phase == "waiting_account_recovery" and not state.failure and not stopped
    assert not gateway.sent and preflight.started == 0 and not live.activated


@pytest.mark.parametrize("native_clean,node_finished,node_raises", [
    (True, True, False), (False, True, False), (True, False, False), (True, False, True)])
@pytest.mark.parametrize("initial_balance_enabled", [False, True])
def test_operator_run_persists_native_shutdown_and_fails_on_missing_stop_proof(
        tmp_path, monkeypatch, capsys, native_clean, node_finished, node_raises, initial_balance_enabled):
    import dotenv
    import opportunity_live
    import opportunity_live_runner
    import opportunity_shutdown
    import opportunity_account_scope
    import os
    import sqlite3
    from test_opportunity_account_scope import credentials
    path = configs(tmp_path, all_five=True,
        venues='["HL","LIGHTER","ASTER","ONDO","BACKPACK"]')
    plan = replace(load_live_plan(path), max_unhedged_notional_usd=D(50), max_unhedged_secs=5,
                   startup_balance_check=initial_balance_enabled)
    startup_calls = []
    import opportunity_balance_runner
    balance_report = {"mode": "balance_check_only", "remote_writes_allowed": False,
        "failure": None, "shutdown_complete": True, "balance_check": {
            "enabled": True, "result": "passed", "started_ns": NOW, "completed_ns": NOW + 1,
            "venues": {venue: {"result": "passed"} for venue in plan.venues}}}
    def initial_balance(received_plan, environ):
        assert received_plan is plan and dict(environ) == credentials()
        assert not plan.state_path.exists() and not (tmp_path / "risk").exists()
        startup_calls.append("balance")
        return balance_report
    monkeypatch.setattr(opportunity_balance_runner, "run_startup_balance_session", initial_balance)
    fake_live = FakeLive(plan.scan)
    fake_live.snapshot = lambda: {"positions": [], "orders": {}}
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(os, "environ", credentials())
    monkeypatch.setattr(opportunity_account_scope, "account_registry_directory", lambda: tmp_path / "risk")
    monkeypatch.setattr(opportunity_account_scope, "verify_execution_identity", lambda *args: None)
    def create_live(*args, **kwargs):
        assert kwargs["max_unhedged_notional_usd"] == D(50) and kwargs["max_unhedged_secs"] == 5
        return fake_live
    monkeypatch.setattr(opportunity_live, "LiveTradeState", create_live)
    controls = {"ONDO": object(), "BACKPACK": object()}
    calls = []
    proof = {"clean": native_clean, "venues": {"ONDO": {"clean": native_clean}, "BACKPACK": {"clean": True}}}
    def collect(received):
        calls.append(received)
        return proof
    monkeypatch.setattr(opportunity_shutdown, "collect_native_shutdown", collect)
    def build(plan, live, coordinator, *, environ):
        assert startup_calls == (["balance"] if initial_balance_enabled else [])
        assert coordinator.phase != "waiting_balances"
        coordinator.native_controls = controls
        def run():
            if node_raises:
                raise RuntimeError("private error must not be exposed")
            coordinator.finished = node_finished
        return SimpleNamespace(run=run, dispose=lambda: None), SimpleNamespace(failure=None), object()
    monkeypatch.setattr(opportunity_live_runner, "build_live_node", build)
    result = run_live(plan)
    assert result == (0 if native_clean and node_finished and not node_raises else 1)
    with sqlite3.connect(plan.state_path) as connection:
        saved = json.loads(connection.execute("SELECT snapshot FROM state WHERE id=1").fetchone()[0])
        if initial_balance_enabled:
            assert connection.execute("SELECT observed_ns FROM events WHERE kind='startup_balance_check'").fetchall() == [(NOW + 1,)]
    assert calls == [controls] and saved["native_shutdown"] == proof
    output = capsys.readouterr().out
    if initial_balance_enabled:
        first, output = output.split("\n", 1)
        assert json.loads(first)["balance_check"] == balance_report["balance_check"]
        assert saved["balance_check"] == balance_report["balance_check"]
    assert json.loads(output)["native_shutdown"] == proof
    assert "private error" not in output


@pytest.mark.parametrize("five_venues", [False, True])
@pytest.mark.parametrize("startup_self_test", [False, True])
def test_native_node_registers_real_strategy_and_execution_clients_using_mock_transports(
        tmp_path, monkeypatch, five_venues, startup_self_test):
    import opportunity_runtime
    path = configs(tmp_path, all_five=five_venues, mandatory=str(startup_self_test).lower(),
        venues='["HL","LIGHTER","ASTER","ONDO","BACKPACK"]' if five_venues else '["HL","LIGHTER","ASTER"]')
    state, live, _, _, _, stopped = coordinator(tmp_path, plan_path=path)
    plan = state.plan
    native_instruments = {m.instrument_id: SimpleNamespace(price_increment=D("0.01"),
        size_increment=D("0.001"), multiplier=D(1), quote_currency="USDT",
        min_quantity=D("0.001") if m.venue == "BACKPACK" else None,
        min_notional=None if not startup_self_test or m.venue in {"BACKPACK", "ONDO"} else D(5),
        is_inverse=False) for m in plan.scan.markets}
    timers = []
    alerts = []
    class Strategy:
        def __init__(self, config):
            self.config = config
            self.cache = SimpleNamespace(instrument=native_instruments.get)
            self.clock = SimpleNamespace(set_timer=lambda *args: timers.append(args),
                timestamp_ns=lambda: NOW,
                set_time_alert_ns=lambda *args, **kwargs: alerts.append((args, kwargs)))
    class Config:
        def __init__(self, **kwargs):
            self.values = kwargs
    class Identifier:
        @staticmethod
        def from_str(value):
            return value
    class Node:
        def __init__(self):
            self.actors, self.strategies, self.data, self.execution = [], [], [], []
            self.options = {}
        def __getattr__(self, name):
            if name.startswith("with_"):
                def option(*args, **kwargs):
                    self.options[name] = (args, kwargs)
                    return self
                return option
            raise AttributeError(name)
        def add_data_client(self, *args):
            self.data.append(args)
            return self
        def add_exec_client(self, *args):
            self.execution.append(args)
            return self
        def build(self):
            return self
        def handle(self):
            return SimpleNamespace(stop=lambda: stopped.append(True))
        def add_actor(self, actor):
            self.actors.append(actor)
        def add_strategy(self, strategy):
            self.strategies.append(strategy)
    node = Node()
    class Gateway(FakeGateway):
        def __init__(self, strategy, markets, on_receipt, on_account, on_fill, **kwargs):
            super().__init__()
            self.kwargs = kwargs
    def install(name, **attributes):
        module = ModuleType(name)
        module.__dict__.update(attributes)
        monkeypatch.setitem(sys.modules, name, module)
    install("nautilus_trader")
    install("nautilus_trader.common", CacheConfig=Config, Environment=SimpleNamespace(LIVE="LIVE"),
        LoggerConfig=Config, LogLevel=SimpleNamespace(WARNING="WARNING", OFF="OFF"))
    install("nautilus_trader.config", LiveRiskEngineConfig=Config)
    install("nautilus_trader.live", LiveDataEngineConfig=Config, LiveExecutionEngineConfig=Config,
        LiveNode=SimpleNamespace(builder=lambda *args: node))
    install("nautilus_trader.model", InstrumentId=Identifier, StrategyId=Identifier, TraderId=Identifier,
        OmsType=SimpleNamespace(NETTING="NETTING"))
    install("nautilus_trader.trading", Strategy=Strategy, StrategyConfig=Config)
    install("nautilus_trader.adapters.backpack", BackpackQuota=object)
    exec_arguments, data_arguments = {}, {}
    def execution_clients(scan, environment, **kwargs):
        exec_arguments.update(kwargs)
        return [(v, "factory", "config") for v in plan.venues]
    def data_clients(scan, **kwargs):
        data_arguments.update(kwargs)
        return [(v, "data-factory", "data-config") for v in plan.venues]
    install("opportunity_native_execution", NativeOrderGateway=Gateway,
        execution_clients=execution_clients)
    monkeypatch.setattr(opportunity_runtime, "data_clients", data_clients)
    observer = SimpleNamespace(failure=None, memory_diagnostics=None)
    observer_options = {}
    def create_observer(*args, **kwargs):
        observer_options.update(kwargs)
        return observer
    monkeypatch.setattr(opportunity_runtime, "create_observer", create_observer)
    result, observed, strategy = build_live_node(plan, live, state)
    assert result is node and observed is observer
    assert observer_options == {"drive_evaluation_timer": False}
    assert len(node.execution) == len(plan.venues) and len(node.data) == len(plan.venues)
    assert exec_arguments["backpack_quota"] is data_arguments["backpack_quota"]
    assert exec_arguments["run_id"] == live.session_id
    assert exec_arguments["order_notional_usd"] == D(100)
    assert exec_arguments["max_gross_notional_usd"] == D(500)
    assert exec_arguments["state_directory"] == plan.state_path.parent / "live.native" / "sessions" / live.session_id
    assert state.proxy.native_session["state_directory"] == str(exec_arguments["state_directory"])
    if five_venues:
        assert exec_arguments["backpack_public_config"] == "data-config"
    engine = node.options["with_exec_engine_config"][0][0].values
    assert engine["reconciliation"] is False and engine["generate_missing_orders"] is False
    assert engine["inflight_check_interval_ms"] == 0
    assert node.options["with_timeout_disconnection_secs"][0] == (30,)
    assert node.strategies == [strategy] and node.actors == [observer]
    assert strategy.config.values["oms_type"] == "NETTING"
    strategy.on_start()
    assert not state.failure and len(live.instruments) == len(plan.scan.markets)
    if startup_self_test:
        assert state.preflight.market.venue == "HL"
        assert state.preflight.markets[-1].modify_mode == "cancel_replace"
        assert len(state.preflight.markets) == len(plan.venues)
        assert {m.venue for m in state.preflight.markets if m.modify_mode == "cancel_replace"} == (
            {"ASTER", "ONDO", "BACKPACK"} if five_venues else {"ASTER"})
        assert state.preflight.markets[0].min_quantity == D("0.001")
        if five_venues:
            assert state.preflight.markets[-1].min_notional == 0
            assert state.preflight.markets[-2].min_notional == 0
    else:
        assert state.preflight is None
        assert {m.venue for m in state.account_markets} == set(plan.venues)
    assert callable(state.gateway.kwargs["quote_provider"])
    assert state.gateway.kwargs["account_timeout_secs"] == 20
    assert state.gateway.kwargs["timeout_secs"] == 10
    assert timers and not state.gateway.sent and not live.activated
    ticks = []
    monkeypatch.setattr(state, "tick", ticks.append)
    live.outbox = [object()]
    live.intent_notifier()
    live.intent_notifier()
    assert len(alerts) == 1 and not ticks
    event = SimpleNamespace(name=alerts[0][0][0])
    alerts[0][1]["callback"](event)
    assert len(ticks) == 1
    live.outbox.clear()
    live.intent_notifier()
    strategy.on_time_event(event)
    assert len(ticks) == 1  # A receipt may already have drained these intents.


def native_build_credentials():
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    from eth_account import Account
    key = "0x" + "11" * 32
    address = Account.from_key(key).address
    seed = bytes(range(32))
    public = Ed25519PrivateKey.from_private_bytes(seed).public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return {"HYPERLIQUID_PK": key, "HYPERLIQUID_ACCOUNT_ADDRESS": address,
        "LIGHTER_API_SECRET": "01" + "00" * 39, "LIGHTER_API_KEY_INDEX": "2", "LIGHTER_ACCOUNT_INDEX": "42",
        "ASTER_MAINNET_SIGNER_PRIVATE_KEY": key, "ASTER_MAINNET_SIGNER_ADDRESS": address,
        "ASTER_MAINNET_USER_ADDRESS": address, "ONDO_MAINNET_API_KEY": "ondoKeyId_BUILD_TEST_ONLY",
        "ONDO_MAINNET_API_SECRET": "ondoApiSecret_BUILD_TEST_ONLY", "ONDO_MAINNET_ACCOUNT_ID": "fixture-account",
        "BACKPACK_MAINNET_API_SECRET": base64.b64encode(seed).decode(),
        "BACKPACK_MAINNET_API_KEY": base64.b64encode(public).decode()}


def test_complete_live_node_builds_with_installed_five_venue_factories_without_connecting(tmp_path):
    import os
    from unittest.mock import patch
    from opportunity_live import LiveTradeState
    fake = native_build_credentials()
    plan = replace(load_live_plan(ROOT / "config/opportunity-trade.live.toml", symbols="BTC,ETH"),
        state_path=tmp_path / "live.sqlite3")
    proxy = SessionStoreProxy(MemoryStore())
    live = LiveTradeState(plan.scan, plan.policy, max_pairs=plan.max_pairs,
        max_gross_notional_usd=plan.max_gross_notional_usd, store=proxy)
    coordinator = LiveCoordinator(plan, live, proxy)
    # Public deterministic credentials only; no ambient secrets and no run/start.
    with patch.dict(os.environ, fake, clear=True):
        node, observer, strategy = build_live_node(plan, live, coordinator)
        try:
            assert node is not None and observer is not None and strategy is not None
            assert set(coordinator.native_controls) == {"ONDO", "BACKPACK", "LIGHTER"}
            assert coordinator.gateway is None and coordinator.preflight is None
            assert live.phase == "waiting_preflight" and not live.orders
        finally:
            node.dispose()


@pytest.mark.parametrize("legacy_layout", [False, True])
def test_native_restart_with_existing_bound_journals_uses_fresh_session_directory(tmp_path, legacy_layout):
    import os
    from unittest.mock import patch
    import opportunity_native_execution
    from opportunity_live import LiveTradeState
    plan = replace(load_live_plan(ROOT / "config/opportunity-trade.live.toml", symbols="BTC,ETH"),
                   state_path=tmp_path / "live.sqlite3")
    store = MemoryStore()
    original_clients = opportunity_native_execution.execution_clients
    def legacy_clients(*args, **kwargs):
        kwargs["state_directory"] = tmp_path / "live.native"
        return original_clients(*args, **kwargs)
    with patch.dict(os.environ, native_build_credentials(), clear=True):
        for attempt in range(2):
            proxy = SessionStoreProxy(store, restart_from_flat=True)
            live = LiveTradeState(plan.scan, plan.policy, max_pairs=plan.max_pairs,
                                 max_gross_notional_usd=plan.max_gross_notional_usd, store=proxy)
            coordinator = LiveCoordinator(plan, live, proxy)
            with patch.object(opportunity_native_execution, "execution_clients",
                              legacy_clients if legacy_layout and attempt == 0 else original_clients):
                node, _, _ = build_live_node(plan, live, coordinator)
            try:
                directory = tmp_path / "live.native"
                if not (legacy_layout and attempt == 0):
                    directory = directory / "sessions" / live.session_id
                binding = json.loads((directory / "ondo/execution.jsonl.production-run").read_text().splitlines()[0])
                assert binding["run_id"] == live.session_id
                assert coordinator.gateway is None and not live.orders
                if attempt == 0:
                    first_id = live.session_id
                else:
                    assert live.session_id != first_id
                    assert coordinator.phase == "waiting_account_recovery"
                    assert store.saved["live"]["session_id"] == first_id
                    assert store.saved["pending_native_session"]["run_id"] == live.session_id
                    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == value for path, value in old_files.items())
            finally:
                node.dispose()
            if attempt == 0:
                old_files = {path: hashlib.sha256(path.read_bytes()).hexdigest()
                             for path in directory.rglob("*") if path.is_file() and path.suffix != ".lock"}


@pytest.mark.parametrize("message,code", [
    ("continuous recovery binding differs from configured run", "native_session_binding_mismatch"),
    ("SECRET_CREDENTIAL_WITH_PRIVATE_RESPONSE", "native_node_construction_failed")])
def test_native_build_refusal_reports_safe_stage_and_code(tmp_path, monkeypatch, capsys, message, code):
    import opportunity_live_runner as runner
    def fail(*args, **kwargs):
        raise runner.NativeBuildError(RuntimeError(message))
    monkeypatch.setattr(runner, "run_live", fail)
    assert main(["--config", str(configs(tmp_path)), "--execute-live"]) == 2
    output = capsys.readouterr().err
    assert "stage=native_build" in output and f"code={code}" in output
    assert "error_type=RuntimeError" in output and "SECRET_CREDENTIAL" not in output

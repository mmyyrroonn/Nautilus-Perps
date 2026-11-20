"""Regression tests for OI-cap entry blocks, loss cooldowns, circuit breakers, exit-depth gate, and hot-path fixes."""
from __future__ import annotations

from decimal import Decimal as D
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
for entry in (str(ROOT / "src"), str(ROOT / "tests")):
    if entry not in sys.path:
        sys.path.insert(0, entry)

from opportunity_core import BookSnapshot
from opportunity_entry_evidence import entry_dispatch_evidence
from opportunity_native_execution import classify_rejection
from opportunity_preflight import ExecutionReceipt
from opportunity_trade import TradePolicy
from opportunity_trade_store import TradeStore
from test_opportunity_live import accounts, state, submissions
from test_opportunity_trade import NOW, SECOND, make_plan, set_book


def test_classify_rejection_identifies_open_interest_cap_and_reduce_only():
    assert classify_rejection("Cannot increase position when open interest is at cap. asset=198", "HL") == "open_interest_cap"
    assert classify_rejection("Open interest cap exceeded for symbol", "ASTER") == "open_interest_cap"
    assert classify_rejection("Order breaches open interest limit for market", "BACKPACK") == "open_interest_cap"
    assert classify_rejection("Market is in reduce-only mode", "HL") == "reduce_only_market"
    assert classify_rejection("Symbol is delisted", "ASTER") == "reduce_only_market"


def test_open_interest_cap_rejection_blocks_instrument_for_window():
    live, _ = state(entry_reject_block_secs=1800)
    set_book(live, "HL", "99", "100")
    set_book(live, "ASTER", "101", "102")
    live.evaluate_symbol("BTC", NOW)
    intents = submissions(live)
    assert len(intents) == 2
    sell_intent = next(i for i in intents if i.venue == "ASTER")

    # Simulate HL-style open_interest_cap rejection on entry
    receipt = ExecutionReceipt(
        sell_intent.request_id, sell_intent.client_order_id, "rejected", NOW + 1_000_000,
        reason_code="open_interest_cap", reason_source="native_event", native_status="rejected",
    )
    live.on_receipt(receipt, NOW + 1_000_000)
    assert "BTC.ASTER" in live.instrument_entry_blocks
    assert live.instrument_entry_blocks["BTC.ASTER"]["reason"] == "open_interest_cap"

    # Dispatch evidence refuses blocked instrument immediately
    pos = next(iter(live.positions.values()))
    reason, _ = entry_dispatch_evidence(live, pos, NOW + 2_000_000)
    assert reason == "instrument_entry_blocked"

    # Clear position and verify new entry on BTC.ASTER is refused during 1800s block
    live.positions.clear()
    live.orders.clear()
    live._execution_orders.clear()
    t_mid = NOW + 600 * SECOND
    set_book(live, "HL", "99", "100", t_mid)
    set_book(live, "ASTER", "101", "102", t_mid)
    accounts(live, t_mid)
    live.evaluate_symbol("BTC", t_mid)
    assert not live.positions

    # After 1800s expiry, entry is allowed again
    t_after = NOW + 1805 * SECOND
    set_book(live, "HL", "99", "100", t_after)
    set_book(live, "ASTER", "101", "102", t_after)
    accounts(live, t_after)
    live.evaluate_symbol("BTC", t_after)
    assert live.positions


def test_reverse_exit_liquidity_and_reversal_spread_gate():
    policy = TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("10"), max_exit_reversal_bps=D("20"))
    live, _ = state(policy=policy)
    # Wide 60 bps spread on buy leg (bid=99.4, ask=100.0) -> reversal slippage > 20 bps -> blocked
    set_book(live, "HL", "99.4", "100.0")
    set_book(live, "ASTER", "101.0", "101.1")
    live.evaluate_symbol("BTC", NOW)
    assert not live.positions

    # Thin reverse exit depth on sell leg ask (0.01 < ~0.2 BTC needed for $20) -> blocked
    live.update_book("BTC.HL", BookSnapshot(
        "HL", "BTC", ((D("99.95"), D("10")),), ((D("100.0"), D("10")),), NOW + 1, NOW + 1), NOW + 1, evaluate=False)
    live.update_book("BTC.ASTER", BookSnapshot(
        "ASTER", "BTC", ((D("101.0"), D("10")),), ((D("101.05"), D("0.01")),), NOW + 1, NOW + 1), NOW + 1, evaluate=True)
    assert not live.positions

    # Tight spread + full reverse exit depth -> allowed
    live.update_book("BTC.ASTER", BookSnapshot(
        "ASTER", "BTC", ((D("101.0"), D("10")),), ((D("101.05"), D("10")),), NOW + 2, NOW + 2), NOW + 2, evaluate=True)
    assert live.positions


def test_dynamic_exit_reversal_edge_ratio_gate():
    policy = TradePolicy(
        order_notional_usd=D("20"),
        entry_edge_bps=D("20"),
        max_exit_reversal_bps=D("30"),
        max_exit_reversal_edge_ratio=D("1"),
    )
    live, _ = state(policy=policy)

    # Case 1: entry net edge = 22 bps (0 fee/reserve in make_plan), reversal spread ~25 bps (> edge, < 30) -> blocked
    live.update_book("BTC.HL", BookSnapshot(
        "HL", "BTC", ((D("99.85"), D("10")),), ((D("100.00"), D("10")),), NOW, NOW), NOW, evaluate=False)
    live.update_book("BTC.ASTER", BookSnapshot(
        "ASTER", "BTC", ((D("100.22"), D("10")),), ((D("100.32"), D("10")),), NOW, NOW), NOW, evaluate=True)
    assert not live.positions

    # Case 2: high edge (50 bps) with reversal ~35 bps (> 30 bps hard cap, even though < edge) -> blocked
    live.update_book("BTC.HL", BookSnapshot(
        "HL", "BTC", ((D("99.80"), D("10")),), ((D("100.00"), D("10")),), NOW + 1, NOW + 1), NOW + 1, evaluate=False)
    live.update_book("BTC.ASTER", BookSnapshot(
        "ASTER", "BTC", ((D("100.50"), D("10")),), ((D("100.65"), D("10")),), NOW + 1, NOW + 1), NOW + 1, evaluate=True)
    assert not live.positions

    # Case 3: edge = 35 bps with reversal ~25 bps (<= min(30, 35) = 30 bps) -> allowed!
    live.update_book("BTC.HL", BookSnapshot(
        "HL", "BTC", ((D("99.85"), D("10")),), ((D("100.00"), D("10")),), NOW + 2, NOW + 2), NOW + 2, evaluate=False)
    live.update_book("BTC.ASTER", BookSnapshot(
        "ASTER", "BTC", ((D("100.35"), D("10")),), ((D("100.45"), D("10")),), NOW + 2, NOW + 2), NOW + 2, evaluate=True)
    assert live.positions


def test_pair_loss_cooldown_and_circuit_breakers_persist_across_pause_clear():
    policy = TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("10"), loss_cooldown_secs=60)
    live, _ = state(policy=policy, max_consecutive_single_legs=3, max_consecutive_losses=5,
                    max_session_realized_loss_usd=D("10"))
    key = ("BTC", "ASTER", "HL")
    pos_filled = {"entry_buy_base": "0.2", "entry_sell_base": "0"}
    pos_unfilled = {"entry_buy_base": "0", "entry_sell_base": "0"}
    econ_loss = {"fee_confirmed": True, "net_usd": "-0.30", "provisional_net_usd": "-0.30"}
    econ_zero = {"fee_confirmed": True, "net_usd": "0", "provisional_net_usd": "0"}

    # Unfilled close must not trigger cooldown or consecutive counters
    live._update_post_close_guards(key, pos_unfilled, econ_zero, "entry_unfilled", NOW)
    assert key not in live.pair_cooldowns
    assert live.consecutive_single_legs == 0

    # Strike 1 -> 60s cooldown
    live._update_post_close_guards(key, pos_filled, econ_loss, "entry_imbalance", NOW)
    assert live.pair_cooldowns[key]["strikes"] == 1
    assert live.pair_cooldowns[key]["until_ns"] == NOW + 60 * SECOND
    assert live.consecutive_single_legs == 1
    assert live.circuit_breaker_reason is None

    # Strike 2 -> 180s cooldown
    t2 = NOW + 61 * SECOND
    live._update_post_close_guards(key, pos_filled, econ_loss, "entry_imbalance", t2)
    assert live.pair_cooldowns[key]["strikes"] == 2
    assert live.pair_cooldowns[key]["until_ns"] == t2 + 180 * SECOND

    # Strike 3 -> trips consecutive_single_leg_limit circuit breaker
    t3 = t2 + 181 * SECOND
    live._update_post_close_guards(key, pos_filled, econ_loss, "entry_imbalance", t3)
    assert live.circuit_breaker_reason == "consecutive_single_leg_limit"
    assert live.entry_pause_reason == "consecutive_single_leg_limit"

    # External session loss monitor clearing entry pause must NOT clear circuit breaker
    live.set_entry_pause(None, t3 + SECOND)
    assert live.entry_pause_reason == "consecutive_single_leg_limit"


def test_realized_loss_circuit_breaker_triggers_immediately():
    from fractions import Fraction as F
    live, _ = state(max_session_realized_loss_usd=D("10"))
    key = ("BTC", "ASTER", "HL")
    live.realized_net = F("-10.01")
    live._update_post_close_guards(
        key, {"entry_buy_base": "0.2", "entry_sell_base": "0.2"},
        {"fee_confirmed": True, "net_usd": "-0.50", "provisional_net_usd": "-0.50"},
        "max_loss", NOW,
    )
    assert live.circuit_breaker_reason == "session_realized_loss_limit"
    assert live.entry_pause_reason == "session_realized_loss_limit"


def test_hotpath_non_owned_update_does_not_force_full_scan_while_holding(monkeypatch):
    live, _ = state(make_plan(symbols=("BTC", "ETH"), venues=("HL", "ASTER", "LIGHTER")))
    for sym in ("BTC", "ETH"):
        for v in ("HL", "ASTER", "LIGHTER"):
            set_book(live, v, "99", "100", symbol=sym)
    # Open a position on BTC
    set_book(live, "ASTER", "101", "102", symbol="BTC")
    live.evaluate_symbol("BTC", NOW + 1)
    assert live.positions
    live.full_evaluation_symbols.clear()

    # Non-owned symbol ETH updates -> must NOT trigger full scan or pollute full_evaluation_symbols
    set_book(live, "HL", "99", "100", NOW + 2, symbol="ETH")
    assert not live.full_evaluation_symbols
    checks = []
    monkeypatch.setattr(live, "observe", lambda *a: None)
    monkeypatch.setattr(live, "_entry", lambda b, s, n: checks.append((b, s)))
    live.evaluate_symbol("ETH", NOW + 2, changed_id="ETH.HL")
    # 3 venues: only pairs involving ETH.HL (4 directed pairs, not all 6)
    assert len(checks) == 4
    assert all("ETH.HL" in pair for pair in checks)

    # Non-reopening emit events must not mark full_evaluation_symbols
    live.emit("account_read_requested", NOW + 3)
    live.emit("exit_stage", NOW + 4)
    assert not live.full_evaluation_symbols

    # Reopening emit event still marks full_evaluation_symbols
    live.emit("order_terminal", NOW + 5)
    assert live.full_evaluation_symbols == {"BTC", "ETH"}


def test_trade_store_uses_wal_and_full_sync(tmp_path):
    store = TradeStore(tmp_path / "wal.sqlite3", "fp")
    try:
        assert store.serializes_immediately is True
        assert store.connection.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        store.save({"ok": True}, [{"event": "test", "observed_ns": NOW}])
        assert store.load() == {"ok": True}
    finally:
        store.close()


def test_hl_proactive_oi_cap_blocks_entries_recovers_and_preserves_exits(monkeypatch):
    from dataclasses import replace
    from opportunity_analytics import AnalyticsCollector
    from test_opportunity_live import fill

    live, events = state(make_plan(symbols=("BTC", "ETH"), venues=("HL", "ASTER")))
    analytics = AnalyticsCollector()
    monkeypatch.setattr("opportunity_live.current_analytics", lambda: analytics)
    for sym in ("BTC", "ETH"):
        set_book(live, "HL", "99.95", "100.00", symbol=sym)
        set_book(live, "ASTER", "100.30", "100.35", symbol=sym)

    # Proactive HL OI-cap poll marks BTC.HL as capped; ETH.HL remains tradable.
    hl_acct = replace(live.accounts["BTC.HL"], observed_ns=NOW + 1, hl_oi_capped=("BTC.HL",))
    live.on_account(hl_acct, NOW + 1)
    assert live.document()["hl_oi_capped_instruments"] == ["BTC.HL"]
    assert any(e["event"] == "hl_open_interest_cap_updated" and e["added"] == ["BTC.HL"] for e in events)

    # BTC entry is pre-blocked with zero submissions and dedicated pre-gate reason.
    live.evaluate_symbol("BTC", NOW + 2)
    assert not submissions(live) and not live.positions
    assert analytics.pre_gates.get("hl_open_interest_cap", 0) >= 1

    # Transient poll failure (hl_oi_capped=None) keeps existing block intact.
    live.on_account(replace(hl_acct, observed_ns=NOW + 3, hl_oi_capped=None), NOW + 3)
    live.evaluate_symbol("BTC", NOW + 4)
    assert not submissions(live) and not live.positions

    # Open an ETH position while ETH is uncapped, then cap ETH and verify reduce-only exit still works.
    live.evaluate_symbol("ETH", NOW + 5)
    eth_entries = submissions(live)
    assert len(eth_entries) == 2
    for order in eth_entries:
        fill(live, order, NOW + 6)
    pos = next(iter(live.positions.values()))
    assert pos["status"] == "open"

    # ETH subsequently hits HL OI cap -> dispatch evidence refuses new entries, but exit proceeds normally.
    live.on_account(replace(hl_acct, observed_ns=NOW + 7, hl_oi_capped=("BTC.HL", "ETH.HL")), NOW + 7)
    # Trigger exit via take profit (reverse prices) after fresh accounts
    t_exit = NOW + 8
    accounts(live, t_exit)
    live.on_account(replace(live.accounts["ETH.HL"], observed_ns=t_exit, hl_oi_capped=("BTC.HL", "ETH.HL")), t_exit)
    set_book(live, "HL", "102", "102.1", t_exit, symbol="ETH")
    set_book(live, "ASTER", "99", "99.1", t_exit, symbol="ETH")
    live.evaluate_symbol("ETH", t_exit)
    exits = submissions(live)
    assert len(exits) == 2 and all(o.reduce_only for o in exits)

    # When BTC drops off perpsAtOpenInterestCap, entries automatically resume without config edits.
    t_recover = NOW + 9
    live.positions.clear()
    live.orders.clear()
    live._execution_orders.clear()
    accounts(live, t_recover)
    live.on_account(replace(live.accounts["BTC.HL"], observed_ns=t_recover, hl_oi_capped=()), t_recover)
    assert any(e["event"] == "hl_open_interest_cap_updated" and "BTC.HL" in e["removed"] for e in events)
    set_book(live, "HL", "99.95", "100.00", t_recover, symbol="BTC")
    set_book(live, "ASTER", "100.30", "100.35", t_recover, symbol="BTC")
    live.evaluate_symbol("BTC", t_recover)
    assert len(submissions(live)) == 2


def test_entry_dispatch_evidence_blocks_hl_oi_capped_instrument():
    from dataclasses import replace

    live, _ = state(make_plan(venues=("HL", "ASTER")))
    set_book(live, "HL", "99", "100")
    set_book(live, "ASTER", "101", "102")
    live.evaluate_symbol("BTC", NOW)
    pos = next(iter(live.positions.values()))
    live.on_account(replace(live.accounts["BTC.HL"], observed_ns=NOW + 1, hl_oi_capped=("BTC.HL",)), NOW + 1)
    reason, _ = entry_dispatch_evidence(live, pos, NOW + 2)
    assert reason == "hl_open_interest_cap"

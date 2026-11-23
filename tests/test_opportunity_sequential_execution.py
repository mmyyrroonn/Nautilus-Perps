"""Tests for Direction B sequential slow-leg-first execution and L1 depth cushion."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from opportunity_basis import BasisFilterPolicy
from opportunity_core import BookSnapshot, MarketMetadata
from opportunity_entry_admission import EntryAdmissionPolicy
from opportunity_live import LiveTradeState
from opportunity_live_runner import (
    HARD_MAX_SEQUENTIAL_LEG2_SLIPPAGE_BPS,
    LiveCoordinator,
    SessionStoreProxy,
    load_live_plan,
)
from opportunity_preflight import ExecutionReceipt
from opportunity_scan import ScanConfigError
import opportunity_timing as timing
from opportunity_trade import TradePlan, TradePolicy
from test_opportunity_live import MemoryStore, accounts, fill, receipt, state, submissions
from test_opportunity_trade import NOW, SECOND, make_plan, make_state, publish, set_book


def _coordinator(live, proxy):
    return LiveCoordinator(
        NS(
            duration_secs=0,
            drain_timeout_secs=30,
            max_session_realized_loss_usd=None,
            loss_monitor=NS(enabled=False),
            policy=live.policy,
            startup_self_test=False,
            startup_balance_check=False,
            startup_timeout_secs=20,
            account_max_age_ms=15000,
            venues=tuple(sorted({m.venue for m in live.markets.values()})),
        ),
        live,
        proxy,
    )


def _attach_gateway(live, proxy, dispatched):
    coord = _coordinator(live, proxy)
    coord.phase = "active"
    coord.started_ns = NOW

    def _dispatch(intent, now_ns, **kw):
        if intent.action == "submit":
            dispatched.append(intent)

    coord.gateway = NS(
        dispatch=_dispatch,
        refresh_native_admission=lambda now_ns: {"BACKPACK": {"ready": True, "reason": "ok", "checked_ns": now_ns}},
    )
    return coord


def _bp_hl_plan():
    return make_plan(symbols=("BTC",), venues=("HL", "BACKPACK"))


def _seq_policy(**overrides):
    defaults = {
        "order_notional_usd": D("20"),
        "entry_edge_bps": D("20"),
        "take_profit_bps": D("18"),
        "max_loss_bps": D("150"),
        "breakeven_after_secs": 600,
        "force_close_after_secs": 1800,
        "soft_exit_execution_buffer_bps": D("2"),
        "min_l1_depth_multiple": D("3"),
        "entry_execution_mode": "slow_first",
        "soft_exit_execution_mode": "slow_first",
        "sequential_leg2_slippage_bps": D("15"),
        "slow_venues": ("HL", "LIGHTER"),
        "loss_cooldown_secs": 60,
    }
    defaults.update(overrides)
    return TradePolicy(**defaults)


def _setup_live_seq(policy=None, *, store=None):
    pol = policy or _seq_policy()
    proxy = SessionStoreProxy(store or MemoryStore())
    live, events = state(
        _bp_hl_plan(),
        pol,
        store=proxy,
        max_unhedged_notional_usd=D("50"),
        max_unhedged_secs=10,
        entry_admission=EntryAdmissionPolicy(max_source_age_ms=1000, max_receive_age_ms=1000, max_skew_ms=500),
    )
    for market in live.plan.markets:
        fee = D("4.5") if market.venue == "HL" else D("5")
        live.update_metadata(
            market.instrument_id,
            MarketMetadata(market.instrument_id, D("0.01"), taker_fee_bps=fee, fee_source="fixture"),
            NOW,
            evaluate=False,
        )
    live.update_native_admission({"BACKPACK": {"ready": True, "reason": "ok", "checked_ns": NOW}}, NOW)
    return live, proxy, events


def _publish_bp_hl(live, now=NOW, *, hl_bid="99.99", hl_ask="100", bp_bid="100.35", bp_ask="100.36", l1_size="2"):
    set_book(live, "HL", hl_bid, hl_ask, now, bid_size=l1_size, ask_size=l1_size)
    set_book(live, "BACKPACK", bp_bid, bp_ask, now, bid_size=l1_size, ask_size=l1_size)
    live.evaluate_symbol("BTC", now)


# -------------------- 1. TradePolicy & Validation --------------------


def test_policy_validation_and_legacy_fingerprint_compatibility(tmp_path):
    base_scan = make_plan()
    legacy = TradePlan(base_scan, TradePolicy(), tmp_path / "p.sqlite3")
    explicit = TradePlan(
        base_scan,
        TradePolicy(
            min_l1_depth_multiple=D("0"),
            entry_execution_mode="simultaneous",
            soft_exit_execution_mode="simultaneous",
            sequential_leg2_slippage_bps=D("15"),
        ),
        tmp_path / "p.sqlite3",
    )
    assert legacy.policy.document() == explicit.policy.document()
    assert legacy.fingerprint() == explicit.fingerprint()

    seq_pol = _seq_policy()
    doc = seq_pol.document()
    assert doc["min_l1_depth_multiple"] == "3"
    assert doc["entry_execution_mode"] == "slow_first"
    assert doc["soft_exit_execution_mode"] == "slow_first"
    assert doc["sequential_leg2_slippage_bps"] == "15"

    for bad in (
        {"min_l1_depth_multiple": D("-1")},
        {"min_l1_depth_multiple": D("101")},
        {"entry_execution_mode": "fast_first"},
        {"soft_exit_execution_mode": "maker_first"},
        {"sequential_leg2_slippage_bps": D("-0.1")},
        {"sequential_leg2_slippage_bps": D("10000")},
    ):
        with pytest.raises(ScanConfigError):
            TradePolicy(**bad)


def test_select_slow_leg_hierarchy_and_fee_tiebreak():
    pol = TradePolicy(slow_venues=("HL", "LIGHTER"))
    assert pol.select_slow_leg("HL", "BACKPACK", D("4.5"), D("5")) == "buy"
    assert pol.select_slow_leg("BACKPACK", "HL", D("5"), D("4.5")) == "sell"
    assert pol.select_slow_leg("LIGHTER", "BACKPACK", D("0"), D("5")) == "buy"
    assert pol.select_slow_leg("ASTER", "BACKPACK", D("4"), D("5")) == "buy"
    # Both slow: HL (rank 3) beats LIGHTER (rank 2)
    assert pol.select_slow_leg("LIGHTER", "HL", D("0"), D("4.5")) == "sell"
    assert pol.select_slow_leg("HYPERLIQUID", "LIGHTER", D("4.5"), D("0")) == "buy"
    # Same slow tier and venue rank (LIGHTER=2, ASTER=2): higher taker fee goes first
    pol_none = TradePolicy()
    assert pol_none.select_slow_leg("LIGHTER", "ASTER", D("5"), D("4")) == "buy"
    assert pol_none.select_slow_leg("ASTER", "LIGHTER", D("4"), D("5")) == "sell"
    # Exact tie prefers buy
    assert pol_none.select_slow_leg("ASTER", "LIGHTER", D("4"), D("4")) == "buy"


def test_live_runner_caps_sequential_leg2_slippage_bps(tmp_path):
    scan_cfg = (ROOT / "config/opportunity-scan.universe.toml").resolve().as_posix()
    limits_cfg = (ROOT / "config/limits.toml").resolve().as_posix()
    text = (
        (ROOT / "config/opportunity-trade.live.toml")
        .read_text(encoding="utf-8")
        .replace('scan_config = "opportunity-scan.universe.toml"', f'scan_config = "{scan_cfg}"')
        .replace('limits_path = "limits.toml"', f'limits_path = "{limits_cfg}"')
        .replace('state_path = "../reports/opportunity-trading/live.sqlite3"', 'state_path = "live.sqlite3"')
        .replace('sequential_leg2_slippage_bps = "15"', 'sequential_leg2_slippage_bps = "75"')
    )
    cfg = tmp_path / "capped.toml"
    cfg.write_text(text, encoding="utf-8")
    plan = load_live_plan(cfg)
    assert plan.policy.sequential_leg2_slippage_bps == HARD_MAX_SEQUENTIAL_LEG2_SLIPPAGE_BPS


# -------------------- 2. Paper L1 Depth Cushion --------------------


@pytest.mark.parametrize("ask_sz,bid_sz,opens", [("0.59", "2", False), ("2", "0.59", False), ("0.60", "0.60", True)])
def test_paper_entry_enforces_min_l1_depth_multiple(monkeypatch, ask_sz, bid_sz, opens):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    pol = TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("10"), min_l1_depth_multiple=D("3"))
    st, _ = make_state(make_plan(), pol)
    set_book(st, "HL", "99.99", "100", ask_size=ask_sz, bid_size="2")
    set_book(st, "ASTER", "100.3", "100.31", bid_size=bid_sz, ask_size="2")
    st.evaluate_symbol("BTC", NOW)
    assert bool(st.positions) is opens
    if not opens:
        assert "l1_depth_insufficient" in str(recorder.document())


# -------------------- 3. Slow-First Entry Lifecycle --------------------


def test_slow_first_entry_sends_slow_leg_first_and_misses_free(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    store = MemoryStore()
    live, proxy, events = _setup_live_seq(store=store)
    dispatched = []
    coord = _attach_gateway(live, proxy, dispatched)
    live.intent_notifier = lambda: coord._live_intents(NOW)

    _publish_bp_hl(live, NOW)
    assert len(dispatched) == 1
    leg1_intent = dispatched[0]
    assert leg1_intent.venue == "HL" and leg1_intent.side == "BUY"
    assert live.orders[leg1_intent.client_order_id]["sequential_role"] == "entry_leg1"
    pos = next(iter(live.positions.values()))
    assert pos["sequential_entry"]["stage"] == "leg1_in_flight"

    # Slow leg IOC expires unfilled -> zero-cost close, no Leg 2 sent, no cooldown/SL
    coord.on_receipt(receipt(leg1_intent, "rejected"), NOW + 100_000_000)
    accounts(live, NOW + 200_000_000)
    live.on_evaluation_timer(NOW + 200_000_000)
    assert len(dispatched) == 1  # BACKPACK was never touched
    assert not live.positions
    assert not live.pair_cooldowns
    assert live.consecutive_single_legs == 0
    closed = [e for e in events if e["event"] == "closed"]
    assert len(closed) == 1 and closed[0]["reason"] == "entry_unfilled"
    assert recorder.document()["entry_execution"]["pair_outcomes"].get("unfilled") == 1


def test_slow_first_entry_leg1_fill_triggers_leg2_even_if_edge_narrowed_and_opens():
    live, proxy, events = _setup_live_seq()
    dispatched = []
    coord = _attach_gateway(live, proxy, dispatched)
    live.intent_notifier = lambda: coord._live_intents(NOW)

    _publish_bp_hl(live, NOW)
    leg1_intent = dispatched[0]
    assert leg1_intent.venue == "HL"

    # Fast leg spread narrows well below entry_edge_bps while slow leg fills -> Leg 2 hedge must STILL dispatch!
    t1 = NOW + 200_000_000
    set_book(live, "BACKPACK", "100.01", "100.02", t1, bid_size="2", ask_size="2")
    coord.on_fill(
        {
            "client_order_id": leg1_intent.client_order_id,
            "trade_id": "t-hl-1",
            "instrument_id": leg1_intent.instrument_id,
            "venue": "HL",
            "side": "BUY",
            "native_qty": leg1_intent.quantity,
            "native_price": D("100"),
            "commission_amount": D("0.009"),
            "commission_currency": "USD",
        },
        t1,
    )
    coord.on_receipt(receipt(leg1_intent, "terminal"), t1 + 1)

    assert len(dispatched) == 2
    leg2_intent = dispatched[1]
    assert leg2_intent.venue == "BACKPACK" and leg2_intent.side == "SELL"
    assert leg2_intent.quantity == leg1_intent.quantity
    # Aggressive 15 bps marketable slippage below bp_bid=100.01 -> ~99.86
    assert leg2_intent.price <= D("99.86")
    assert live.orders[leg2_intent.client_order_id]["sequential_role"] == "entry_leg2"
    assert live.orders[leg2_intent.client_order_id]["entry_dispatch"]["reason"] == "sequential_leg2_hedge"

    t2 = t1 + 100_000_000
    coord.on_fill(
        {
            "client_order_id": leg2_intent.client_order_id,
            "trade_id": "t-bp-1",
            "instrument_id": leg2_intent.instrument_id,
            "venue": "BACKPACK",
            "side": "SELL",
            "native_qty": leg2_intent.quantity,
            "native_price": D("100.01"),
            "commission_amount": D("0.010"),
            "commission_currency": "USD",
        },
        t2,
    )
    coord.on_receipt(receipt(leg2_intent, "terminal"), t2 + 1)
    pos = next(iter(live.positions.values()))
    assert pos["status"] == "open"
    assert F(pos["remaining_buy_base"]) == F(leg1_intent.quantity)
    assert F(pos["remaining_sell_base"]) == F(leg2_intent.quantity)


def test_slow_first_entry_leg2_miss_triggers_entry_imbalance_and_unwinds():
    live, proxy, events = _setup_live_seq()
    dispatched = []
    coord = _attach_gateway(live, proxy, dispatched)
    live.intent_notifier = lambda: coord._live_intents(NOW)

    _publish_bp_hl(live, NOW)
    leg1 = dispatched[0]
    t1 = NOW + 100_000_000
    fill(live, leg1, t1, fee="0.009")
    coord._live_intents(t1)
    assert len(dispatched) == 2
    leg2 = dispatched[1]

    t2 = t1 + 100_000_000
    coord.on_receipt(receipt(leg2, "rejected"), t2)
    pos = next(iter(live.positions.values()))
    assert pos["status"] == "exit_pending"
    assert pos["exit_reason"] == "entry_imbalance"
    assert any(e["event"] == "entry_imbalance" for e in events)
    assert len(dispatched) == 3 and dispatched[2].venue == "HL" and dispatched[2].side == "SELL"


def test_slow_first_entry_send_check_blocks_leg1_when_leg2_book_moves_before_dispatch():
    live, proxy, events = _setup_live_seq()
    dispatched = []
    coord = _attach_gateway(live, proxy, dispatched)
    # Do not dispatch synchronously yet so we can move BACKPACK before _live_intents runs
    _publish_bp_hl(live, NOW)
    set_book(live, "BACKPACK", "100.01", "100.02", NOW + 1_000_000, bid_size="2", ask_size="2")
    coord._live_intents(NOW + 2_000_000)

    assert not dispatched
    blocked = [e for e in events if e["event"] == "entry_dispatch_blocked"]
    assert len(blocked) == 1 and blocked[0]["reason"] == "net_edge_insufficient"


def test_slow_first_entry_partial_fill_matches_qty_and_rebalances_if_leg2_short():
    live, proxy, events = _setup_live_seq()
    dispatched = []
    coord = _attach_gateway(live, proxy, dispatched)
    live.intent_notifier = lambda: coord._live_intents(NOW)

    _publish_bp_hl(live, NOW)
    leg1 = dispatched[0]  # qty = 0.20
    t1 = NOW + 100_000_000
    fill(live, leg1, t1, quantity="0.12", fee="0.005")
    coord._live_intents(t1)

    assert len(dispatched) == 2
    leg2 = dispatched[1]
    assert leg2.quantity == D("0.12")

    # Leg 2 fills only 0.08 -> transitions to rebalancing and trims 0.04 off HL
    t2 = t1 + 100_000_000
    fill(live, leg2, t2, quantity="0.08", fee="0.004")
    coord._live_intents(t2)
    pos = next(iter(live.positions.values()))
    assert pos["status"] == "rebalancing"
    assert any(e["event"] == "entry_rebalance_requested" and e["target_base"] == "0.08" for e in events)
    assert len(dispatched) == 3 and dispatched[2].venue == "HL" and dispatched[2].quantity == D("0.04")


# -------------------- 4. Slow-First Soft Exit Lifecycle --------------------


def _open_seq_pair():
    live, proxy, events = _setup_live_seq()
    dispatched = []
    coord = _attach_gateway(live, proxy, dispatched)
    clock = [NOW]
    live.intent_notifier = lambda: coord._live_intents(clock[0])
    _publish_bp_hl(live, NOW)
    leg1 = dispatched[0]
    t1 = NOW + 100_000_000
    clock[0] = t1
    fill(live, leg1, t1, fee="0.009")
    coord._live_intents(t1)
    leg2 = dispatched[1]
    t2 = t1 + 100_000_000
    clock[0] = t2
    fill(live, leg2, t2, fee="0.010")
    coord._live_intents(t2)
    accounts(live, t2 + 10_000_000)
    live.on_evaluation_timer(t2 + 10_000_000)
    assert next(iter(live.positions.values()))["status"] == "open"
    dispatched.clear()
    events.clear()
    return live, proxy, coord, dispatched, events, clock, t2 + 10_000_000


def test_slow_first_soft_exit_misses_free_and_later_closes_cleanly():
    live, proxy, coord, dispatched, events, clock, t_open = _open_seq_pair()

    # Convergence creates take_profit (>= 18 bps net) -> only HL slow leg sent
    t3 = t_open + 2 * SECOND
    clock[0] = t3
    accounts(live, t3)
    _publish_bp_hl(live, t3, hl_bid="100.35", hl_ask="100.36", bp_bid="100.00", bp_ask="100.01")
    assert len(dispatched) == 1
    ex1 = dispatched[0]
    assert ex1.venue == "HL" and ex1.side == "SELL"
    assert live.orders[ex1.client_order_id]["sequential_role"] == "soft_exit_leg1"
    pos = next(iter(live.positions.values()))
    assert pos["exit_reason"] == "take_profit"
    assert "sequential_soft_exit" in pos

    # Slow exit leg misses AND spread widens again -> resets cleanly to open with zero exit_imbalance!
    t4 = t3 + 100_000_000
    clock[0] = t4
    set_book(live, "HL", "99.99", "100.00", t4, bid_size="2", ask_size="2")
    set_book(live, "BACKPACK", "100.35", "100.36", t4, bid_size="2", ask_size="2")
    coord.on_receipt(receipt(ex1, "rejected"), t4)
    assert pos["status"] == "open" and pos["exit_reason"] is None
    assert "sequential_soft_exit" not in pos
    assert not any(e["event"] == "exit_requested" and e.get("reason") == "exit_imbalance" for e in events)
    assert len(dispatched) == 1

    # Next convergence after retry interval -> HL fills -> BACKPACK hedge triggers -> closed as take_profit!
    t5 = t4 + 2 * SECOND
    clock[0] = t5
    accounts(live, t5)
    _publish_bp_hl(live, t5, hl_bid="100.35", hl_ask="100.36", bp_bid="100.00", bp_ask="100.01")
    assert len(dispatched) == 2
    ex1_retry = dispatched[1]
    assert ex1_retry.venue == "HL"
    t6 = t5 + 100_000_000
    clock[0] = t6
    fill(live, ex1_retry, t6, fee="0.009")
    coord._live_intents(t6)
    assert len(dispatched) == 3
    ex2 = dispatched[2]
    assert ex2.venue == "BACKPACK" and ex2.side == "BUY"
    assert live.orders[ex2.client_order_id]["sequential_role"] == "soft_exit_leg2"
    # Even if BACKPACK spread jumped before hedge, discard_invalid_soft_exit never blocks Leg 2 hedge
    assert ex2.price >= D("100.16")

    t7 = t6 + 100_000_000
    clock[0] = t7
    fill(live, ex2, t7, fee="0.010")
    accounts(live, t7 + 10_000_000)
    live.on_evaluation_timer(t7 + 10_000_000)
    assert not live.positions
    assert not live.pair_cooldowns
    closed = [e for e in events if e["event"] == "closed"]
    assert len(closed) == 1 and closed[0]["reason"] == "take_profit"


def test_slow_first_soft_exit_leg2_miss_escalates_to_exit_imbalance():
    live, proxy, coord, dispatched, events, clock, t_open = _open_seq_pair()
    t3 = t_open + 2 * SECOND
    clock[0] = t3
    accounts(live, t3)
    _publish_bp_hl(live, t3, hl_bid="100.35", hl_ask="100.36", bp_bid="100.00", bp_ask="100.01")
    ex1 = dispatched[0]

    t4 = t3 + 100_000_000
    clock[0] = t4
    fill(live, ex1, t4, fee="0.009")
    coord._live_intents(t4)
    assert len(dispatched) == 2
    ex2 = dispatched[1]
    assert ex2.venue == "BACKPACK"

    t5 = t4 + 100_000_000
    clock[0] = t5
    coord.on_receipt(receipt(ex2, "rejected"), t5)
    pos = next(iter(live.positions.values()))
    assert pos["exit_reason"] == "exit_imbalance"
    assert "sequential_soft_exit" not in pos
    assert len(dispatched) == 3 and dispatched[2].venue == "BACKPACK"


def test_hard_exit_still_sends_both_legs_simultaneously():
    live, proxy, coord, dispatched, events, clock, t_open = _open_seq_pair()
    t_hard = t_open + 1805 * SECOND  # > force_close_after_secs (1800)
    clock[0] = t_hard
    accounts(live, t_hard)
    _publish_bp_hl(live, t_hard, hl_bid="99.99", hl_ask="100", bp_bid="100.35", bp_ask="100.36")
    assert len(dispatched) == 2
    assert {i.venue for i in dispatched} == {"HL", "BACKPACK"}
    pos = next(iter(live.positions.values()))
    assert pos["exit_reason"] == "max_hold"
    assert "sequential_soft_exit" not in pos


def test_slow_first_soft_exit_send_check_blocks_when_leg2_edge_vanishes():
    live, proxy, events = _setup_live_seq()
    # Trigger sequential open without live _live_intents until manual check
    _publish_bp_hl(live, NOW)
    ex_int = submissions(live)
    assert len(ex_int) == 1
    t1 = NOW + 100_000_000
    fill(live, ex_int[0], t1, fee="0.009")
    l2_int = submissions(live)
    assert len(l2_int) == 1
    fill(live, l2_int[0], t1 + 100_000_000, fee="0.010")
    accounts(live, t1 + 200_000_000)
    live.on_evaluation_timer(t1 + 200_000_000)

    t3 = t1 + 2 * SECOND
    accounts(live, t3)
    _publish_bp_hl(live, t3, hl_bid="100.35", hl_ask="100.36", bp_bid="100.00", bp_ask="100.01")
    pos = next(iter(live.positions.values()))
    assert "sequential_soft_exit" in pos

    # Move BACKPACK away before coordinator dispatches soft_exit_leg1
    set_book(live, "BACKPACK", "100.50", "100.51", t3 + 1_000_000, bid_size="2", ask_size="2")
    dispatched: list[object] = []
    coord = _attach_gateway(live, proxy, dispatched)
    coord._live_intents(t3 + 2_000_000)
    assert not dispatched
    assert pos["status"] == "open" and pos["exit_reason"] is None
    assert "sequential_soft_exit" not in pos
    blocked = [e for e in events if e["event"] == "exit_dispatch_blocked"]
    assert len(blocked) == 1 and blocked[0]["reason"] == "net_edge_insufficient"

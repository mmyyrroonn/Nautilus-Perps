"""Tests for per-venue conservative entry slippage cushion and idle Backpack poll throttling."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from opportunity_basis import BasisFilterPolicy
from opportunity_entry_evidence import entry_dispatch_evidence
from opportunity_live import LiveTradeState
from opportunity_live_runner import LiveCoordinator
from opportunity_native_execution import (
    BACKPACK_IDLE_ADMISSION_INTERVAL_NS,
    BACKPACK_IDLE_RECONCILE_INTERVAL_NS,
    NativeOrderGateway,
)
from opportunity_paper import PaperTradeState
from opportunity_preflight import OrderIntent
from opportunity_trade import TradePolicy
from test_opportunity_live import MemoryStore, accounts, fill, state as live_state, submissions
from test_opportunity_native_execution import FakeStrategy, MODEL
from test_opportunity_trade import NOW, SECOND, make_plan, make_state as paper_state, publish, set_book


MS = 1_000_000


def test_paper_per_venue_entry_slippage_pricing_caps_and_soft_exit_unaffected():
    policy = TradePolicy(
        order_notional_usd=D(100),
        entry_edge_bps=D(20),
        take_profit_bps=D(5),
        slippage_bps=D(0),
        entry_slippage_bps_by_venue={"HL": "4", "ASTER": "0"},
    )
    # Raw 22 bps (100 -> 100.22) clears 20 bps at 0 slippage, but fails once HL=4 bps is deducted (~18 bps).
    borderline, _ = paper_state(make_plan(), policy)
    publish(borderline, sell_bid="100.22", sell_ask="100.23")
    assert not borderline.positions

    # Raw 25 bps (100 -> 100.25) clears 20 bps after HL=4 bps cushion (~20.99 bps);
    # qty is capped to 0.99 so 0.99 * 100.04 <= 100.
    paper, _ = paper_state(make_plan(), policy)
    publish(paper, sell_bid="100.25", sell_ask="100.26")
    assert len(paper.positions) == 1
    pos = next(iter(paper.positions.values()))
    fills = pos["entry"]["paper_fills"]
    assert fills["buy_slippage_bps"] == "4"
    assert fills["sell_slippage_bps"] == "0"
    qty = F(pos["entry"]["sizing"]["base_quantity"])
    assert qty == F("0.99")
    assert F(fills["buy_notional_usd"]) == qty * F("100.04") <= 100
    assert F(fills["sell_notional_usd"]) == qty * F("100.25") <= 100

    # Soft exit valuation uses policy.slippage_bps=0 (no entry cushion deducted on exit).
    exit_now = NOW + SECOND
    publish(paper, exit_now, buy_bid="100.04", buy_ask="100.05", sell_bid="100.18", sell_ask="100.19")
    # At buy_bid=100.04 and sell_ask=100.19 with 0 exit slippage:
    # sale - cover = qty*(100.04 - 100.19) = -0.15*qty; entry was +0.21*qty => net = +0.06*qty (~6 bps >= 5).
    assert not paper.positions


def test_live_per_venue_entry_slippage_conservative_pricing_unhedged_cap_and_exits():
    policy = TradePolicy(
        order_notional_usd=D(100),
        entry_edge_bps=D(20),
        take_profit_bps=D(5),
        slippage_bps=D(0),
        hard_exit_slippage_bps=D(10),
        entry_slippage_bps_by_venue={"HL": "4", "ASTER": "0"},
    )
    # Borderline 22 bps rejected after HL=4 cushion.
    borderline, _ = live_state(
        make_plan(), policy, max_unhedged_notional_usd=D(50), entry_unhedged_reserve_bps=D(10)
    )
    publish(borderline, sell_bid="100.22", sell_ask="100.23")
    assert not borderline.positions and not submissions(borderline)

    # Qualifying 25 bps: HL BUY gets 4 bps cushion (100.04), ASTER SELL stays at touch (100.25).
    # Per-leg unhedged budget = 50 * (1 - 0.001) = 49.95 => 0.49 * 100.04 = 49.0196 <= 49.95 (0.50 exceeds).
    live, _ = live_state(
        make_plan(), policy, max_unhedged_notional_usd=D(50), entry_unhedged_reserve_bps=D(10)
    )
    publish(live, sell_bid="100.25", sell_ask="100.26")
    intents = submissions(live)
    assert len(intents) == 2
    by_venue = {i.venue: i for i in intents}
    assert by_venue["HL"].side == "BUY" and by_venue["HL"].price == D("100.04")
    assert by_venue["ASTER"].side == "SELL" and by_venue["ASTER"].price == D("100.25")
    assert by_venue["HL"].quantity == D("0.49") and by_venue["ASTER"].quantity == D("0.49")

    pos = next(iter(live.positions.values()))
    assert pos["entry"]["economics"]["buy_entry_slippage_bps"] == "4"
    assert pos["entry"]["economics"]["sell_entry_slippage_bps"] == "0"
    assert F(pos["entry"]["entry_risk"]["buy_standalone_usd"]) <= F("49.95")

    # Fill both legs so we can verify soft exits do NOT inherit HL=4 entry cushion.
    for intent in intents:
        fill(live, intent, NOW + 1)
    exit_now = NOW + 2 * SECOND
    accounts(live, exit_now)
    set_book(live, "HL", "100.04", "100.05", exit_now)
    set_book(live, "ASTER", "100.18", "100.19", exit_now)
    live.on_evaluation_timer(exit_now)
    soft_exits = {i.venue: i for i in submissions(live)}
    assert len(soft_exits) == 2
    assert soft_exits["HL"].price == D("100.04")
    assert soft_exits["ASTER"].price == D("100.19")

    # Reverse direction: HL SELL gets 4 bps cushion (floor of 100.30 * 0.9996 = 100.25988 -> 100.25),
    # while ASTER BUY stays at touch (100.00).
    rev_live, _ = live_state(make_plan(), policy)
    set_book(rev_live, "ASTER", "99.99", "100.00", NOW)
    set_book(rev_live, "HL", "100.30", "100.31", NOW)
    rev_live.evaluate_symbol("BTC", NOW)
    rev_intents = {i.venue: i for i in submissions(rev_live)}
    assert len(rev_intents) == 2
    assert rev_intents["ASTER"].side == "BUY" and rev_intents["ASTER"].price == D("100.00")
    assert rev_intents["HL"].side == "SELL" and rev_intents["HL"].price == D("100.25")


def test_entry_dispatch_evidence_allows_move_inside_cushion_and_blocks_outside():
    policy = TradePolicy(
        order_notional_usd=D(100),
        entry_edge_bps=D(20),
        slippage_bps=D(0),
        entry_slippage_bps_by_venue={"HL": "4", "ASTER": "0"},
    )
    live, events = live_state(make_plan(), policy)
    publish(live, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
    pos = next(iter(live.positions.values()))
    assert len(live.outbox) == 2

    # Touch moves +2 bps inside the +4 bps cushion (100.02 <= 100.04 persisted limit):
    # send_check must NOT double-count the 4 bps cushion into a false price_outside_limit block.
    check_now = NOW + 10 * MS
    set_book(live, "HL", "100.01", "100.02", check_now)
    reason, evidence = entry_dispatch_evidence(live, pos, check_now)
    assert reason == "valid"
    assert F(evidence["buy"]["limit_cash_usd"]) == F("0.99") * F("100.04")
    assert F(evidence["net_usd"]) == F("0.99") * (F("100.28") - F("100.04"))

    sent = []
    coord = NS(live=live, proxy=NS(restart_pending=False), gateway=NS(dispatch=lambda i, s, **k: sent.append(i)))
    LiveCoordinator._live_intents(coord, check_now)
    assert len(sent) == 2

    # Touch moves +5 bps outside the +4 bps cushion (100.05 > 100.04 persisted limit): must block.
    live2, events2 = live_state(make_plan(), policy)
    publish(live2, buy_bid="99.99", buy_ask="100.00", sell_bid="100.28", sell_ask="100.29")
    pos2 = next(iter(live2.positions.values()))
    check_now2 = NOW + 10 * MS
    set_book(live2, "HL", "100.04", "100.05", check_now2)
    reason2, _ = entry_dispatch_evidence(live2, pos2, check_now2)
    assert reason2 == "price_outside_limit"

    sent2 = []
    coord2 = NS(live=live2, proxy=NS(restart_pending=False), gateway=NS(dispatch=lambda i, s, **k: sent2.append(i)))
    LiveCoordinator._live_intents(coord2, check_now2)
    assert sent2 == []
    assert [e for e in events2 if e["event"] == "entry_dispatch_blocked"][-1]["reason"] == "price_outside_limit"


def test_basis_filter_receives_post_slippage_gross_bps():
    policy = TradePolicy(
        order_notional_usd=D(100),
        entry_edge_bps=D(10),
        slippage_bps=D(0),
        entry_slippage_bps_by_venue={"HL": "4", "ASTER": "0"},
    )
    basis_filter = BasisFilterPolicy(
        enabled=True,
        window_secs=600,
        warmup_secs=10,
        min_deviation_bps=D(20),
    )

    # Warm up 0 bps baseline in both PaperTradeState and LiveTradeState.
    paper = PaperTradeState(make_plan(), policy, basis_filter=basis_filter)
    live, _ = live_state(make_plan(), policy, basis_filter=basis_filter)
    for engine in (paper, live):
        for market in engine.plan.markets:
            engine.update_metadata(
                market.instrument_id,
                replace(
                    live.metadata[market.instrument_id],
                    taker_fee_bps=D(0),
                    fee_source="synthetic",
                ),
                NOW,
                evaluate=False,
            )
    for sec in range(0, 12, 2):
        t = NOW + sec * SECOND
        for engine in (paper, live):
            set_book(engine, "HL", "99.99", "100.01", t)
            set_book(engine, "ASTER", "99.99", "100.01", t)
            engine.evaluate_symbol("BTC", t)

    # Raw +22 bps spread drops to ~17.99 bps after HL=4 cushion (< 20 bps min_deviation_bps).
    t_reject = NOW + 13 * SECOND
    for engine in (paper, live):
        set_book(engine, "HL", "99.99", "100.00", t_reject)
        set_book(engine, "ASTER", "100.22", "100.23", t_reject)
        engine.evaluate_symbol("BTC", t_reject)
    assert not paper.positions and not live.positions and not submissions(live)

    # Raw +26 bps spread yields ~21.99 bps post-cushion (>= 20 bps min_deviation_bps).
    t_accept = NOW + 14 * SECOND
    accounts(live, t_accept)
    for engine in (paper, live):
        set_book(engine, "HL", "99.99", "100.00", t_accept)
        set_book(engine, "ASTER", "100.26", "100.27", t_accept)
        engine.evaluate_symbol("BTC", t_accept)
    assert len(paper.positions) == 1 and len(submissions(live)) == 2
    for engine in (paper, live):
        basis = next(iter(engine.positions.values()))["entry"]["basis"]
        assert 20 <= basis["current_spread_bps"] < 23
        assert 20 <= basis["deviation_bps"] < 23


@pytest.mark.parametrize("global_only_ready", [False, True])
def test_native_gateway_backpack_idle_throttling_and_active_unthrottling(global_only_ready):
    strategy = FakeStrategy()
    counts = {"reconcile": 0, "admission": 0}
    if global_only_ready:
        admission_payload = json.dumps({
            "ready": False,
            "reason": "unrelated quote stale",
            "global_ready": True,
            "markets": {
                "BTC_USDC_PERP.BACKPACK": {"ready": True, "reason": None},
                "SOL_USDC_PERP.BACKPACK": {"ready": False, "reason": "unrelated quote stale"},
            },
        })
        quotes_fresh = {"BTC_USDC_PERP": True, "SOL_USDC_PERP": False}
    else:
        admission_payload = '{"ready":true,"reason":null}'
        quotes_fresh = {"BTC_USDC_PERP": True, "SOL_USDC_PERP": True}

    def reconcile():
        counts["reconcile"] += 1

    def admission():
        counts["admission"] += 1
        return admission_payload

    control = NS(
        renew_authority=lambda *a: None,
        persist_economics=lambda: None,
        reconcile_terminal_evidence=reconcile,
        admission_status_json=admission,
    )
    markets = [
        NS(venue="BACKPACK", instrument_id="BTC_USDC_PERP.BACKPACK"),
        NS(venue="BACKPACK", instrument_id="SOL_USDC_PERP.BACKPACK"),
    ]
    gateway = NativeOrderGateway(
        strategy,
        markets,
        lambda r: None,
        lambda a: None,
        lambda f: None,
        model=MODEL,
        native_controls={"BACKPACK": control},
        environ={},
        backpack_public_config=NS(
            telemetry_snapshot_json=lambda: json.dumps({
                "connected": True,
                "metadata_ready": True,
                "connection_epoch": 1,
                "quotes_fresh": quotes_fresh,
            })
        ),
    )
    try:
        # First poll always runs both.
        gateway.poll(NOW)
        assert counts == {"reconcile": 1, "admission": 1}

        # Idle polls within <500ms skip both reconcile and admission.
        gateway.poll(NOW + 100 * MS)
        gateway.poll(NOW + 200 * MS)
        assert counts == {"reconcile": 1, "admission": 1}

        # Direct pre-dispatch refresh_native_admission() always runs unconditionally.
        gateway.refresh_native_admission(NOW + 250 * MS)
        assert counts == {"reconcile": 1, "admission": 2}

        # Idle poll at >=500ms refreshes admission while reconcile waits until >=1000ms.
        gateway.poll(NOW + BACKPACK_IDLE_ADMISSION_INTERVAL_NS + 250 * MS)
        assert counts == {"reconcile": 1, "admission": 3}

        gateway.poll(NOW + BACKPACK_IDLE_RECONCILE_INTERVAL_NS)
        assert counts == {"reconcile": 2, "admission": 3}

        # Dispatching a Backpack order immediately unthrottles both until terminal & reconciled.
        bp_order = OrderIntent(
            "submit",
            "BACKPACK",
            "BTC_USDC_PERP.BACKPACK",
            "bp-req-1",
            "bp-cid-1",
            "BUY",
            D("0.01"),
            D("100"),
            "IOC",
        )
        gateway.dispatch(bp_order, NOW + 1100 * MS)
        gateway.poll(NOW + 1150 * MS)
        assert counts == {"reconcile": 3, "admission": 4}
        gateway.poll(NOW + 1200 * MS)
        assert counts == {"reconcile": 4, "admission": 5}

        # Close the Backpack order cleanly; one dirty post-terminal poll runs, then idle throttling resumes.
        cached = strategy.cached["bp-cid-1"]
        cached.is_closed, cached.status, cached.filled_qty = True, "CANCELED", D(0)
        gateway.on_native_event(
            "canceled",
            NS(client_order_id="bp-cid-1", instrument_id="BTC_USDC_PERP.BACKPACK"),
            NOW + 1250 * MS,
        )
        gateway.poll(NOW + 1300 * MS)
        assert counts == {"reconcile": 5, "admission": 6}

        gateway.poll(NOW + 1400 * MS)
        gateway.poll(NOW + 1500 * MS)
        assert counts == {"reconcile": 5, "admission": 6}
    finally:
        gateway.close()

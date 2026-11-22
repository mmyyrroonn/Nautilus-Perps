"""Tests for Maker-priority soft exits, leg selection, passive repricing, partial-fill hedging, and IOC fallback."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from opportunity_core import MarketMetadata
from opportunity_entry_evidence import discard_invalid_soft_exit
from opportunity_native_execution import NativeOrderGateway
from opportunity_preflight import ExecutionReceipt
from opportunity_runtime import market_metadata
from opportunity_scan import MarketPlan, ScanConfigError
from opportunity_trade import TradePlan, TradePolicy
from test_opportunity_live import accounts, fill, open_pair, receipt, state, submissions
from test_opportunity_native_execution import MODEL, FakeStrategy, event, intent as gw_intent
from test_opportunity_trade import NOW, SECOND, make_plan, set_book

MS = 1_000_000


def _drain_orders(live):
    for o in live.outbox:
        order = live.orders.get(o.client_order_id)
        if o.action == "submit" and order is not None and order["status"] == "prepared":
            order["status"] = "dispatch_released"
    return list(live.outbox)


def test_trade_policy_maker_exit_validation_aliases_and_legacy_fingerprint(tmp_path):
    base_scan = make_plan()
    legacy = TradePlan(base_scan, TradePolicy(), tmp_path / "paper.sqlite3")
    same = TradePlan(
        base_scan,
        TradePolicy(maker_exit_venues=(), maker_exit_timeout_ms=0, maker_exit_max_attempts=0, maker_fee_bps_by_venue=()),
        tmp_path / "paper.sqlite3",
    )
    assert legacy.policy.document() == same.policy.document()
    assert legacy.fingerprint() == same.fingerprint()
    assert not legacy.policy.maker_exit_enabled

    pol = TradePolicy(
        maker_exit_venues=["HYPERLIQUID", "BACKPACK", "HYPERLIQUID"],
        maker_exit_timeout_ms=4000,
        maker_exit_max_attempts=2,
        maker_fee_bps_by_venue={"HYPERLIQUID": "1.5", "BACKPACK": "-0.2", "LIGHTER": "0"},
    )
    assert pol.maker_exit_enabled is True
    assert pol.maker_exit_venues == ("BACKPACK", "HL")
    assert pol.is_maker_exit_venue("HYPERLIQUID") is True
    assert pol.is_maker_exit_venue("ASTER") is False
    assert pol.maker_fee_bps("HYPERLIQUID") == D("1.5")
    assert pol.maker_fee_bps("BACKPACK") == D("-0.2")
    assert pol.maker_fee_bps("ASTER") is None
    assert pol.document()["maker_exit_venues"] == ["BACKPACK", "HL"]
    assert pol.document()["maker_exit_timeout_ms"] == 4000
    assert pol.document()["maker_exit_max_attempts"] == 2
    assert pol.document()["maker_fee_bps_by_venue"] == {"BACKPACK": "-0.2", "HL": "1.5", "LIGHTER": "0"}

    for bad in (
        {"maker_exit_venues": "HL", "maker_exit_timeout_ms": 1000},
        {"maker_exit_venues": ["UNKNOWN"], "maker_exit_timeout_ms": 1000},
        {"maker_exit_venues": ["HL", "HYPERLIQUID"], "maker_exit_timeout_ms": 1000},
        {"maker_exit_venues": ["HL"], "maker_exit_timeout_ms": 0},
        {"maker_exit_venues": (), "maker_exit_timeout_ms": 1000},
        {"maker_exit_venues": (), "maker_exit_max_attempts": 1},
        {"maker_exit_venues": ["HL"], "maker_exit_timeout_ms": -1},
        {"maker_exit_venues": ["HL"], "maker_exit_timeout_ms": 60001},
        {"maker_exit_venues": ["HL"], "maker_exit_timeout_ms": 1000, "maker_exit_max_attempts": 21},
        {"maker_fee_bps_by_venue": {"UNKNOWN": "1"}},
        {"maker_fee_bps_by_venue": {"HL": "-100.1"}},
        {"maker_fee_bps_by_venue": {"HL": "10000"}},
        {"maker_fee_bps_by_venue": "invalid"},
    ):
        with pytest.raises(ScanConfigError):
            TradePolicy(**bad)


def test_market_metadata_extracts_maker_fee_and_live_fee_lookup_falls_back():
    bp_market = MarketPlan(
        "BTC", "BACKPACK", "BTC_USDC_PERP.BACKPACK", D(1), "parity",
        backpack_economics={"margin_init": "0.1", "margin_maint": "0.05", "maker_fee": "0",
                            "taker_fee": "0.0005", "source": "Configured", "source_reference": "ref"},
    )
    bp_inst = NS(id="BTC_USDC_PERP.BACKPACK", size_increment=D("0.01"), multiplier=D(1))
    bp_meta = market_metadata(bp_market, bp_inst)
    assert bp_meta.taker_fee_bps == D("5")
    assert bp_meta.maker_fee_bps == D("0")

    hl_market = MarketPlan("BTC", "HL", "BTC-USD-PERP.HYPERLIQUID", D(1), "parity")
    hl_inst = NS(id="BTC-USD-PERP.HYPERLIQUID", size_increment=D("0.01"), multiplier=D(1),
                 taker_fee=D("0.00045"), maker_fee=D("0.00015"))
    hl_meta = market_metadata(hl_market, hl_inst)
    assert hl_meta.maker_fee_bps == D("1.5")

    hl_zero_inst = NS(id="BTC-USD-PERP.HYPERLIQUID", size_increment=D("0.01"), multiplier=D(1),
                      taker_fee=D("0"), maker_fee=D("0"))
    assert market_metadata(hl_market, hl_zero_inst).maker_fee_bps is None

    pol = TradePolicy(
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=4000,
        maker_fee_bps_by_venue={"HL": "1.2"},
    )
    live, _ = state(policy=pol)
    live.update_metadata("BTC.HL", MarketMetadata("BTC.HL", D("0.01"), taker_fee_bps=D("4.5"),
                                                fee_source="test", maker_fee_bps=D("1.5")), NOW, evaluate=False)
    live.update_metadata("BTC.ASTER", MarketMetadata("BTC.ASTER", D("0.01"), taker_fee_bps=D("4"),
                                                      fee_source="test", maker_fee_bps=D("1")), NOW, evaluate=False)
    # Policy override wins over metadata
    assert live._maker_fee_bps("BTC.HL") == D("1.2")
    # Metadata maker_fee_bps used when no policy override
    assert live._maker_fee_bps("BTC.ASTER") == D("1")
    # Falls back to taker_fee_bps when maker_fee_bps is None everywhere
    live.update_metadata("BTC.ASTER", MarketMetadata("BTC.ASTER", D("0.01"), taker_fee_bps=D("4"),
                                                      fee_source="test", maker_fee_bps=None), NOW, evaluate=False)
    assert live._maker_fee_bps("BTC.ASTER") == D("4")


def test_maker_exit_leg_selection_respects_slow_tier_and_fee_saving():
    # Case 1: HL is slow, ASTER is fast -> picks HL (slow leg) as Maker, never ASTER
    pol = TradePolicy(
        slow_venues=("HL",),
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=4000,
        maker_fee_bps_by_venue={"HL": "1.5", "ASTER": "0"},
    )
    live, _ = state(policy=pol)
    open_pair(live)
    pos = next(iter(live.positions.values()))
    assert live._select_maker_exit_leg(pos) == "buy"  # BTC.HL

    # Case 2: Both venues fast -> picks venue with greater fee saving (ASTER taker=5, maker=0 vs HL taker=2, maker=1.5)
    pol_fast = TradePolicy(
        entry_edge_bps=D("15"),
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=4000,
        maker_fee_bps_by_venue={"HL": "1.5", "ASTER": "0"},
    )
    live2, _ = state(policy=pol_fast)
    live2.update_metadata("BTC.HL", MarketMetadata("BTC.HL", D("0.01"), taker_fee_bps=D("2"), fee_source="t"), NOW, evaluate=False)
    live2.update_metadata("BTC.ASTER", MarketMetadata("BTC.ASTER", D("0.01"), taker_fee_bps=D("5"), fee_source="t"), NOW, evaluate=False)
    open_pair(live2)
    pos2 = next(iter(live2.positions.values()))
    assert live2._select_maker_exit_leg(pos2) == "sell"  # BTC.ASTER saves 5 bps vs 0.5 bps

    # Case 3: Slow leg HL is NOT in maker_exit_venues while fast ASTER is -> must NOT make fast ASTER wait for slow HL hedge
    pol_ondo = TradePolicy(
        slow_venues=("HL",),
        maker_exit_venues=("ASTER",),
        maker_exit_timeout_ms=4000,
    )
    live3, _ = state(policy=pol_ondo)
    open_pair(live3)
    pos3 = next(iter(live3.positions.values()))
    assert live3._select_maker_exit_leg(pos3) is None


def test_maker_plus_taker_captures_soft_exit_before_taker_taker_and_closes_without_loss_cooldown():
    pol = TradePolicy(
        entry_edge_bps=D("20"),
        take_profit_bps=D("5"),
        max_loss_bps=D("150"),
        loss_cooldown_secs=60,
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=4000,
        maker_exit_max_attempts=2,
        maker_fee_bps_by_venue={"HL": "0", "ASTER": "1"},
    )
    live, events = state(policy=pol)
    for inst in ("BTC.HL", "BTC.ASTER"):
        live.update_metadata(inst, MarketMetadata(inst, D("0.01"), taker_fee_bps=D("5"), fee_source="t"), NOW, evaluate=False)
    # Entry: buy HL @ 100.00, sell ASTER @ 100.40 (40 bps gross - 10 bps fees = ~30 bps net entry edge)
    set_book(live, "HL", "99.99", "100.00", NOW)
    set_book(live, "ASTER", "100.40", "100.41", NOW)
    live.evaluate_symbol("BTC", NOW)
    entries = submissions(live)
    assert len(entries) == 2
    for e in entries:
        fill(live, e, NOW + 10_000_000, fee=D(e.quantity) * e.price * D("0.0005"))
    accounts(live, NOW + 10_000_002)
    live.on_evaluation_timer(NOW + 10_000_002)
    live.drain_intents()
    pos = next(iter(live.positions.values()))

    # Exit mark: HL 100.12/100.16, ASTER 100.34/100.35.
    # Taker-Taker exit = sell 100.12 (fee 5bps) - buy 100.35 (fee 5bps) -> ~ -3 bps total (< 5 bps) -> NOT executable taker-taker!
    # Maker(HL) + Taker(ASTER) = sell 100.16 (fee 0bps) - buy 100.35 (fee 5bps) -> ~ +6 bps total (>= 5 bps)!
    t1 = NOW + SECOND
    accounts(live, t1)
    set_book(live, "HL", "100.12", "100.16", t1)
    set_book(live, "ASTER", "100.34", "100.35", t1)
    live.evaluate_symbol("BTC", t1)

    exits = submissions(live)
    assert len(exits) == 1
    maker_intent = exits[0]
    assert maker_intent.instrument_id == "BTC.HL"
    assert maker_intent.side == "SELL"
    assert maker_intent.price == D("100.16")
    assert maker_intent.time_in_force == "GTC"
    assert maker_intent.post_only is True
    assert maker_intent.reduce_only is True
    assert live.orders[maker_intent.client_order_id]["maker_exit"] is True
    assert pos["exit_reason"] == "take_profit"

    # Resting order gets accepted
    live.on_receipt(receipt(maker_intent, "accepted"), t1 + 5 * MS)
    assert not submissions(live)

    # Full fill on Maker order immediately triggers IOC hedge on ASTER in the SAME on_fill callback!
    fill(live, maker_intent, t1 + 100 * MS, fee="0", terminal=False)
    hedges = submissions(live)
    assert len(hedges) == 1
    hedge_intent = hedges[0]
    assert hedge_intent.instrument_id == "BTC.ASTER"
    assert hedge_intent.side == "BUY"
    assert hedge_intent.quantity == maker_intent.quantity
    assert hedge_intent.time_in_force == "IOC"
    assert hedge_intent.post_only is False
    assert hedge_intent.reduce_only is True
    # Exit reason MUST stay "take_profit", NEVER falsely flagged as "exit_imbalance"!
    assert pos["exit_reason"] == "take_profit"

    live.on_receipt(receipt(maker_intent, "terminal"), t1 + 101 * MS)
    fill(live, hedge_intent, t1 + 110 * MS, price="100.35", fee=D(hedge_intent.quantity) * D("100.35") * D("0.0005"))
    accounts(live, t1 + 120 * MS)
    live.on_evaluation_timer(t1 + 120 * MS)

    assert not live.positions and live.completed == 1
    assert not live.pair_cooldowns
    assert live.consecutive_losses == 0
    closed = [e for e in events if e["event"] == "closed"][-1]
    assert closed["reason"] == "take_profit"
    assert D(closed["net_usd"]) > 0


def test_maker_partial_fill_cancels_remainder_and_hedges_immediately():
    pol = TradePolicy(
        slow_venues=("HL",),
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=4000,
        take_profit_bps=D("5"),
        maker_fee_bps_by_venue={"HL": "0"},
    )
    live, events = state(policy=pol)
    open_pair(live)
    pos = next(iter(live.positions.values()))
    t1 = NOW + SECOND
    accounts(live, t1)
    # Taker-taker = 100.02 - 100.31 + 0.30 = +1 bps (< 5 bps); Maker(HL @ 100.10) + Taker(ASTER @ 100.31) = +9 bps (>= 5 bps)
    set_book(live, "HL", "100.02", "100.10", t1)
    set_book(live, "ASTER", "100.30", "100.31", t1)
    live.evaluate_symbol("BTC", t1)

    maker_intent, = submissions(live)
    live.on_receipt(receipt(maker_intent, "accepted"), t1 + 5 * MS)

    # Partial fill of 0.40 on HL (0.59 remains open)
    fill(live, maker_intent, t1 + 50 * MS, quantity="0.40", fee="0", terminal=False)
    intents = live.drain_intents()
    cancels = [i for i in intents if i.action == "cancel"]
    submits = [i for i in intents if i.action == "submit"]
    assert len(cancels) == 1 and cancels[0].client_order_id == maker_intent.client_order_id
    assert len(submits) == 1
    hedge_intent = submits[0]
    assert hedge_intent.instrument_id == "BTC.ASTER"
    assert hedge_intent.side == "BUY"
    assert hedge_intent.quantity == D("0.40")
    assert pos["exit_reason"] == "take_profit"

    # Re-observing before hedge fill does NOT double-hedge!
    live.observe(next(iter(live.positions)), t1 + 55 * MS)
    assert not [i for i in live.drain_intents() if i.action in {"submit", "cancel"}]

    # Maker cancel confirms BEFORE hedge fills -> waits for in-flight hedge rather than double-reducing
    live.on_receipt(ExecutionReceipt(cancels[0].request_id, maker_intent.client_order_id, "canceled"), t1 + 60 * MS)
    live.on_receipt(receipt(maker_intent, "terminal"), t1 + 60 * MS)
    assert not submissions(live)

    # Hedge 0.40 fills -> balanced remaining [0.59, 0.59] immediately closes cleanly!
    fill(live, hedge_intent, t1 + 70 * MS, fee="0")
    close_submits = submissions(live)
    assert len(close_submits) == 2
    assert {i.quantity for i in close_submits} == {D("0.59")}
    for c in close_submits:
        fill(live, c, t1 + 80 * MS, fee="0")
    accounts(live, t1 + 90 * MS)
    live.on_evaluation_timer(t1 + 90 * MS)
    assert not live.positions and live.completed == 1
    closed = [e for e in events if e["event"] == "closed"][-1]
    assert closed["reason"] == "take_profit"


def test_maker_adverse_move_reprice_timeout_and_ioc_fallback():
    pol = TradePolicy(
        slow_venues=("HL",),
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=2000,
        maker_exit_max_attempts=2,
        take_profit_bps=D("5"),
        maker_fee_bps_by_venue={"HL": "0"},
    )
    live, events = state(policy=pol)
    open_pair(live)
    pos = next(iter(live.positions.values()))
    t1 = NOW + SECOND
    accounts(live, t1)
    # 1. Start passive Maker @ 100.10 (HL) while taker-taker is only +1 bps (< 5 bps TP)
    set_book(live, "HL", "100.02", "100.10", t1)
    set_book(live, "ASTER", "100.30", "100.31", t1)
    live.evaluate_symbol("BTC", t1)
    m1, = submissions(live)
    assert m1.price == D("100.10") and pos["maker_exit_attempts"] == 1
    live.on_receipt(receipt(m1, "accepted"), t1 + 5 * MS)

    # 2. HL ask steps closer to 100.08 (taker-taker +3 bps < 5 bps; maker@100.08 + taker@100.29 = +9 bps >= 5 bps) -> reprice after 1s
    t_reprice = t1 + 1100 * MS
    accounts(live, t_reprice)
    set_book(live, "HL", "100.02", "100.08", t_reprice)
    set_book(live, "ASTER", "100.28", "100.29", t_reprice)
    live.evaluate_symbol("BTC", t_reprice)
    c1, = [i for i in live.drain_intents() if i.action == "cancel"]
    assert c1.client_order_id == m1.client_order_id
    assert [e for e in events if e["event"] == "maker_exit_cancel_requested"][-1]["reason"] == "maker_exit_reprice"

    # Cancel completes -> post-cancel account snapshot arrives -> Attempt #2 placed at improved passive touch 100.08
    live.on_receipt(ExecutionReceipt(c1.request_id, m1.client_order_id, "canceled"), t_reprice + 5 * MS)
    live.on_receipt(receipt(m1, "terminal"), t_reprice + 5 * MS)
    accounts(live, t_reprice + 10 * MS)
    live.on_evaluation_timer(t_reprice + 10 * MS)
    m2, = submissions(live)
    assert m2.price == D("100.08") and m2.post_only is True and pos["maker_exit_attempts"] == 2
    live.on_receipt(receipt(m2, "accepted"), t_reprice + 15 * MS)

    # 3. Attempt #2 times out at +2000ms -> canceled with reason="maker_exit_timeout"
    t_timeout = t_reprice + 2050 * MS
    accounts(live, t_timeout)
    set_book(live, "HL", "100.02", "100.08", t_timeout)
    set_book(live, "ASTER", "100.28", "100.29", t_timeout)
    live.on_evaluation_timer(t_timeout)
    c2, = [i for i in live.drain_intents() if i.action == "cancel"]
    assert [e for e in events if e["event"] == "maker_exit_cancel_requested"][-1]["reason"] == "maker_exit_timeout"

    # Cancel completes -> taker-taker IS now profitable and attempts == max_attempts (2) -> falls back to paired IOC!
    live.on_receipt(ExecutionReceipt(c2.request_id, m2.client_order_id, "canceled"), t_timeout + 5 * MS)
    live.on_receipt(receipt(m2, "terminal"), t_timeout + 5 * MS)
    set_book(live, "HL", "100.16", "100.18", t_timeout + 10 * MS)
    set_book(live, "ASTER", "100.22", "100.23", t_timeout + 10 * MS)
    accounts(live, t_timeout + 10 * MS)
    live.on_evaluation_timer(t_timeout + 10 * MS)
    ioc_exits = submissions(live)
    assert len(ioc_exits) == 2 and all(o.time_in_force == "IOC" and not o.post_only for o in ioc_exits)

    # Test adverse edge loss: maker_exit_attempts stays incremented (never resets to 0) so max_attempts strictly caps churn
    live2, events2 = state(policy=pol)
    open_pair(live2)
    pos2 = next(iter(live2.positions.values()))
    accounts(live2, t1)
    set_book(live2, "HL", "100.02", "100.10", t1)
    set_book(live2, "ASTER", "100.30", "100.31", t1)
    live2.evaluate_symbol("BTC", t1)
    m3, = submissions(live2)
    live2.on_receipt(receipt(m3, "accepted"), t1 + 5 * MS)
    t_bad = t1 + 200 * MS
    accounts(live2, t_bad)
    # Maker(HL@100.10) + Taker(ASTER@100.36) = +4 bps (< 5 bps TP) -> maker_exit_edge_lost; Taker-Taker = -4 bps (> -20 bps max_loss)
    set_book(live2, "ASTER", "100.35", "100.36", t_bad)
    live2.evaluate_symbol("BTC", t_bad)
    c3, = [i for i in live2.drain_intents() if i.action == "cancel"]
    assert [e for e in events2 if e["event"] == "maker_exit_cancel_requested"][-1]["reason"] == "maker_exit_edge_lost"
    live2.on_receipt(ExecutionReceipt(c3.request_id, m3.client_order_id, "canceled"), t_bad + 5 * MS)
    live2.on_receipt(receipt(m3, "terminal"), t_bad + 5 * MS)
    accounts(live2, t_bad + 10 * MS)
    live2.on_evaluation_timer(t_bad + 10 * MS)
    assert not submissions(live2)
    assert pos2["maker_exit_attempts"] == 1 and pos2["status"] == "open"


def test_hard_exit_and_stop_entries_cancel_resting_maker_and_escalate():
    pol = TradePolicy(
        slow_venues=("HL",),
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=4000,
        take_profit_bps=D("5"),
        max_loss_bps=D("20"),
    )
    live, events = state(policy=pol)
    open_pair(live)
    pos = next(iter(live.positions.values()))
    t1 = NOW + SECOND
    accounts(live, t1)
    set_book(live, "HL", "100.02", "100.10", t1)
    set_book(live, "ASTER", "100.30", "100.31", t1)
    live.evaluate_symbol("BTC", t1)
    maker_order, = submissions(live)
    live.on_receipt(receipt(maker_order, "accepted"), t1 + 5 * MS)

    # Price plunges to trigger max_loss -> must escalate & cancel resting Maker without double-submitting HL SELL
    t_loss = t1 + 50 * MS
    set_book(live, "HL", "98.00", "98.01", t_loss)
    set_book(live, "ASTER", "100.30", "100.31", t_loss)
    live.evaluate_symbol("BTC", t_loss)
    assert pos["exit_reason"] == "max_loss"
    intents = live.drain_intents()
    assert [i.action for i in intents] == ["cancel"]
    assert [e for e in events if e["event"] == "maker_exit_cancel_requested"][-1]["reason"] == "max_loss"

    # Once cancel confirms, paired hard IOC fires immediately
    live.on_receipt(ExecutionReceipt(intents[0].request_id, maker_order.client_order_id, "canceled"), t_loss + 5 * MS)
    live.on_receipt(receipt(maker_order, "terminal"), t_loss + 5 * MS)
    hard_submits = submissions(live)
    assert len(hard_submits) == 2 and all(i.time_in_force == "IOC" for i in hard_submits)

    # stop_entries drops prepared maker exits locally and cancels resting maker exits
    live2, events2 = state(policy=pol)
    open_pair(live2)
    accounts(live2, t1)
    set_book(live2, "HL", "100.02", "100.10", t1)
    set_book(live2, "ASTER", "100.30", "100.31", t1)
    live2.evaluate_symbol("BTC", t1)
    # Still in outbox ("prepared") -> stop_entries drops it locally without cancel request
    live2.stop_entries(t1 + 1 * MS, reason="operator_stop")
    assert not live2.outbox
    drain_ev = [e for e in events2 if e["event"] == "drain_requested"][-1]
    assert len(drain_ev["local_not_dispatched"]) == 1


def test_maker_cancel_failure_retries_after_cooldown():
    pol = TradePolicy(
        slow_venues=("HL",),
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=1000,
        take_profit_bps=D("5"),
    )
    live, events = state(policy=pol)
    open_pair(live)
    t1 = NOW + SECOND
    accounts(live, t1)
    set_book(live, "HL", "100.02", "100.10", t1)
    set_book(live, "ASTER", "100.30", "100.31", t1)
    live.evaluate_symbol("BTC", t1)
    m, = submissions(live)
    live.on_receipt(receipt(m, "accepted"), t1 + 5 * MS)

    # Trigger timeout cancel
    t_to = t1 + 1050 * MS
    accounts(live, t_to)
    set_book(live, "HL", "100.02", "100.10", t_to)
    set_book(live, "ASTER", "100.30", "100.31", t_to)
    live.on_evaluation_timer(t_to)
    c1, = [i for i in live.drain_intents() if i.action == "cancel"]

    # Simulate cancel rejected -> clears cancel_requested_ns, waits retry_ns (1s), then retries
    live.on_receipt(ExecutionReceipt(c1.request_id, m.client_order_id, "rejected", reason_code="network"), t_to + 10 * MS)
    assert [e for e in events if e["event"] == "maker_exit_cancel_failed"]
    # Before retry_ns -> throttled
    t_early = t_to + 200 * MS
    accounts(live, t_early)
    set_book(live, "HL", "100.02", "100.10", t_early)
    set_book(live, "ASTER", "100.30", "100.31", t_early)
    live.on_evaluation_timer(t_early)
    assert not [i for i in live.drain_intents() if i.action == "cancel"]

    # After retry_ns -> retries cancel
    t_retry = t_to + 1020 * MS
    accounts(live, t_retry)
    set_book(live, "HL", "100.02", "100.10", t_retry)
    set_book(live, "ASTER", "100.30", "100.31", t_retry)
    live.on_evaluation_timer(t_retry)
    c2, = [i for i in live.drain_intents() if i.action == "cancel"]
    assert c2.request_id != c1.request_id


def test_soft_exit_dispatch_evidence_checks_maker_crossing_and_hedge_edge():
    pol = TradePolicy(
        slow_venues=("HL",),
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=4000,
        take_profit_bps=D("5"),
        maker_fee_bps_by_venue={"HL": "0"},
    )
    live, _ = state(policy=pol)
    open_pair(live)
    t1 = NOW + SECOND
    accounts(live, t1)
    set_book(live, "HL", "100.02", "100.10", t1)
    set_book(live, "ASTER", "100.30", "100.31", t1)
    live.evaluate_symbol("BTC", t1)
    m, = _drain_orders(live)

    # Valid at send time
    assert discard_invalid_soft_exit(live, m, t1 + 1 * MS) is False

    # Case 1: BBO provider quotes bid >= our passive SELL limit (100.10) -> blocks as maker_would_cross
    live.bbo_provider = lambda iid: (D("100.10"), D("100.11"), t1 + 2 * MS, t1 + 2 * MS) if iid == "BTC.HL" else None
    assert discard_invalid_soft_exit(live, m, t1 + 3 * MS) is True
    assert live.orders[m.client_order_id]["exit_dispatch"]["reason"] == "maker_would_cross"

    # Case 2: Re-arm on fresh position and deteriorate ASTER ask -> blocks as net_edge_insufficient
    live2, _ = state(policy=pol)
    open_pair(live2)
    accounts(live2, t1)
    set_book(live2, "HL", "100.02", "100.10", t1)
    set_book(live2, "ASTER", "100.30", "100.31", t1)
    live2.evaluate_symbol("BTC", t1)
    m2, = _drain_orders(live2)
    set_book(live2, "ASTER", "100.55", "100.56", t1 + 2 * MS)
    assert discard_invalid_soft_exit(live2, m2, t1 + 2 * MS) is True
    assert live2.orders[m2.client_order_id]["exit_dispatch"]["reason"] == "net_edge_insufficient"


def test_native_gateway_stops_polling_cancel_once_submit_is_terminal():
    strategy, receipts, accounts_cb, fills_cb = FakeStrategy(), [], [], []
    markets = [NS(venue="HL", instrument_id="BTC-USD-PERP.HYPERLIQUID")]
    gateway = NativeOrderGateway(strategy, markets, receipts.append, accounts_cb.append, fills_cb.append, model=MODEL)
    try:
        sub = gw_intent(time_in_force="GTC", post_only=True, reduce_only=True)
        gateway.dispatch(sub, 1_000_000_000)
        gateway.dispatch(gw_intent(action="cancel", request_id="cancel-1"), 1_000_000_100)
        cached = strategy.cached["owned-1"]
        cached.is_closed, cached.status, cached.filled_qty = True, "FILLED", D(".01")
        gateway.on_native_event(
            "filled",
            event(trade_id="t1", order_side="SELL" if sub.side == "SELL" else "BUY",
                  last_qty=D(".01"), last_px=D("100"), commission=None),
            1_000_000_200,
        )
        assert ("submit-1", "terminal", "") in gateway.emitted
        receipts.clear()
        # Polling well past gateway.timeout_ns must NOT emit receipt_timeout ("unknown") for cancel-1
        gateway.poll(1_000_000_200 + 20 * SECOND)
        assert not [r for r in receipts if r.request_id == "cancel-1" and r.kind == "unknown"]
    finally:
        gateway.close()


def test_taker_soft_exit_takes_priority_over_maker_when_already_profitable():
    pol = TradePolicy(
        slow_venues=("HL",),
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=4000,
        maker_exit_max_attempts=2,
        take_profit_bps=D("5"),
        maker_fee_bps_by_venue={"HL": "0"},
    )
    live, _ = state(policy=pol)
    open_pair(live)
    pos = next(iter(live.positions.values()))
    t1 = NOW + SECOND
    accounts(live, t1)
    # Taker-taker = 100.16 - 100.31 + 0.30 = +15 bps (>= 5 bps) -> must skip Maker and send 2 IOC orders immediately.
    set_book(live, "HL", "100.16", "100.20", t1)
    set_book(live, "ASTER", "100.30", "100.31", t1)
    live.evaluate_symbol("BTC", t1)
    exits = submissions(live)
    assert len(exits) == 2
    assert all(o.time_in_force == "IOC" and not o.post_only for o in exits)
    assert all(not live.orders[o.client_order_id].get("maker_exit") for o in exits)
    assert pos.get("maker_exit_attempts", 0) == 0


def test_resting_maker_cancels_for_taker_exit_ready_and_caps_edge_lost_attempts():
    pol = TradePolicy(
        slow_venues=("HL",),
        maker_exit_venues=("HL", "ASTER"),
        maker_exit_timeout_ms=4000,
        maker_exit_max_attempts=2,
        take_profit_bps=D("5"),
        maker_fee_bps_by_venue={"HL": "0"},
    )
    # (a) Resting maker in Pre-TP zone cancels immediately for taker_exit_ready once taker-taker satisfies take_profit_bps.
    live, events = state(policy=pol)
    open_pair(live)
    t1 = NOW + SECOND
    accounts(live, t1)
    set_book(live, "HL", "100.02", "100.10", t1)
    set_book(live, "ASTER", "100.30", "100.31", t1)
    live.evaluate_symbol("BTC", t1)
    maker_order, = submissions(live)
    live.on_receipt(receipt(maker_order, "accepted"), t1 + 5 * MS)

    t2 = t1 + 50 * MS
    accounts(live, t2)
    set_book(live, "HL", "100.16", "100.20", t2)
    set_book(live, "ASTER", "100.30", "100.31", t2)
    live.evaluate_symbol("BTC", t2)
    cancels = [i for i in live.drain_intents() if i.action == "cancel"]
    assert len(cancels) == 1 and cancels[0].client_order_id == maker_order.client_order_id
    assert [e for e in events if e["event"] == "maker_exit_cancel_requested"][-1]["reason"] == "taker_exit_ready"

    live.on_receipt(ExecutionReceipt(cancels[0].request_id, maker_order.client_order_id, "canceled"), t2 + 5 * MS)
    live.on_receipt(receipt(maker_order, "terminal"), t2 + 5 * MS)
    t_ioc = t1 + SECOND + 50 * MS
    set_book(live, "HL", "100.16", "100.20", t_ioc)
    set_book(live, "ASTER", "100.30", "100.31", t_ioc)
    accounts(live, t_ioc)
    live.on_evaluation_timer(t_ioc)
    ioc_exits = submissions(live)
    assert len(ioc_exits) == 2 and all(o.time_in_force == "IOC" and not o.post_only for o in ioc_exits)

    # (b) Two maker_exit_edge_lost cycles exhaust maker_exit_max_attempts=2; 3rd Pre-TP window stays open without churning.
    live2, _ = state(policy=pol)
    open_pair(live2)
    pos2 = next(iter(live2.positions.values()))
    cur = t1
    for attempt in (1, 2):
        accounts(live2, cur)
        set_book(live2, "HL", "100.02", "100.10", cur)
        set_book(live2, "ASTER", "100.30", "100.31", cur)
        live2.evaluate_symbol("BTC", cur)
        m_ord, = submissions(live2)
        assert pos2["maker_exit_attempts"] == attempt
        live2.on_receipt(receipt(m_ord, "accepted"), cur + 5 * MS)

        cur += 50 * MS
        accounts(live2, cur)
        set_book(live2, "ASTER", "100.35", "100.36", cur)
        live2.evaluate_symbol("BTC", cur)
        c_ord, = [i for i in live2.drain_intents() if i.action == "cancel"]
        live2.on_receipt(ExecutionReceipt(c_ord.request_id, m_ord.client_order_id, "canceled"), cur + 5 * MS)
        live2.on_receipt(receipt(m_ord, "terminal"), cur + 5 * MS)
        cur += SECOND + 50 * MS

    accounts(live2, cur)
    set_book(live2, "HL", "100.02", "100.10", cur)
    set_book(live2, "ASTER", "100.30", "100.31", cur)
    live2.evaluate_symbol("BTC", cur)
    assert not submissions(live2)
    assert pos2["maker_exit_attempts"] == 2 and pos2["status"] == "open"

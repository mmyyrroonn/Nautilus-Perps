"""Comprehensive behavioral tests for Route A Cross-Exchange Carry MVP."""
from __future__ import annotations

from decimal import Decimal
import gzip
import json
from pathlib import Path
import time

import pytest

from src.carry.cli import main as carry_cli_main
from src.carry.config import CarryConfigError, load_carry_plan
from src.carry.executor import CarryExecutionController, OrderAck, PaperVenueGateway
from src.carry.funding_feed import (
    HOUR,
    LegMarketInfo,
    PublicCarryFeed,
    compute_trailing_rates,
    parse_bybit_tickers,
    parse_hl_meta_and_ctxs,
    parse_lighter_snapshots,
    parse_okx_tickers,
)
from src.carry.ledger import CarryLedger, LedgerOwnershipError
from src.carry.recorder import CarryRecorder
from src.carry.limits import (
    HARD_DAILY_LOSS_USD,
    HARD_GROSS_NOTIONAL_USD,
    HARD_LEG_NOTIONAL_USD,
    HARD_MARGIN_GUARD_MAX_PCT,
    HARD_MAX_SLOTS,
    HARD_MAX_UNHEDGED_SECS,
    HARD_MAX_UNHEDGED_USD,
    HARD_MAX_VENUE_NOTIONAL_USD,
    HARD_MIN_ENTRY_CARRY_BPS_H,
    HARD_SLICE_USD,
)
from src.carry.risk import BasisStopTracker, FastRiskEvaluator
from src.carry.signal import (
    InstrumentSpec,
    SubscriptionManager,
    align_leg_quantities,
    evaluate_entry_candidates,
    evaluate_watch_candidates,
    should_exit_carry_decay,
)

D = Decimal
REPO_ROOT = Path(__file__).resolve().parents[1]


def _make_leg(
    venue: str = "HL",
    symbol: str = "SOL",
    *,
    asset_class: str = "CRYPTO",
    interval_h: int = 1,
    next_settle_ts: int = 1_800_003_600,
    r8: float = 0.2,
    r24: float = 0.2,
    cur_rate: float | None = None,
    bid: float = 100.0,
    ask: float = 100.05,
    prev_1h: float = 100.0,
    vol24: float = 2_000_000.0,
    vol1h: float = 50_000.0,
) -> LegMarketInfo:
    iid_map = {
        "HL": f"{'xyz:' if asset_class == 'RWA' and symbol == 'NVDA' else ''}{symbol}-USD-PERP.HYPERLIQUID",
        "LIGHTER": f"{symbol}-PERP.LIGHTER",
        "OKX": f"{symbol}-USDT-SWAP.OKX",
        "BYBIT": f"{symbol}USDT-LINEAR.BYBIT",
    }
    return LegMarketInfo(
        venue=venue,
        symbol=symbol,
        raw_symbol=symbol,
        instrument_id=iid_map[venue],
        asset_class=asset_class,
        interval_h=interval_h,
        next_settle_ts=next_settle_ts,
        r8_bps_h=r8,
        r24_bps_h=r24,
        current_rate_bps_h=r8 if cur_rate is None else cur_rate,
        mark_price=0.5 * (bid + ask),
        prev_1h_price=prev_1h,
        bid=bid,
        ask=ask,
        vol24_usd=vol24,
        vol1h_usd=vol1h,
        fetched_ts=1_800_000_000,
    )


def test_config_hard_caps_clamp_and_unknown_keys_fail(tmp_path: Path) -> None:
    cfg = tmp_path / "widened.toml"
    cfg.write_text(
        """
schema_version = 1
mode = "live"
environment = "mainnet"
state_path = "state.sqlite3"
venues = ["HL", "LIGHTER", "OKX", "BYBIT"]

[policy]
entry_carry_bps_h = "0.05"
exit_carry_8h_bps_h = "0.2"

[risk]
max_slots = 50
leg_notional_usd = "9999"
slice_usd = "500"
max_venue_notional_usd = "9999"
max_gross_notional_usd = "99999"
margin_guard_pct = "75"
max_unhedged_usd = "999"
max_unhedged_secs = 900
max_daily_loss_usd = "5000"
""",
        encoding="utf-8",
    )
    plan = load_carry_plan(cfg)
    assert plan.risk.max_slots == HARD_MAX_SLOTS
    assert plan.risk.leg_notional_usd == HARD_LEG_NOTIONAL_USD
    assert plan.risk.slice_usd == HARD_SLICE_USD
    assert plan.risk.max_venue_notional_usd == HARD_MAX_VENUE_NOTIONAL_USD
    assert plan.risk.max_gross_notional_usd == HARD_GROSS_NOTIONAL_USD
    assert plan.risk.margin_guard_pct == HARD_MARGIN_GUARD_MAX_PCT
    assert plan.risk.max_unhedged_usd == HARD_MAX_UNHEDGED_USD
    assert plan.risk.max_unhedged_secs == HARD_MAX_UNHEDGED_SECS
    assert plan.risk.max_daily_loss_usd == HARD_DAILY_LOSS_USD
    assert plan.policy.entry_carry_bps_h == HARD_MIN_ENTRY_CARRY_BPS_H

    bad_cfg = tmp_path / "bad.toml"
    bad_cfg.write_text(cfg.read_text(encoding="utf-8") + "\nunknown_top = true\n", encoding="utf-8")
    with pytest.raises(CarryConfigError):
        load_carry_plan(bad_cfg)


def test_okx_ctval_lot_alignment_and_rejections() -> None:
    # OKX contract multiplier = 10 coins per contract (e.g., an altcoin at $0.15 -> $1.50/contract)
    spec_okx = InstrumentSpec("FOO-USDT-SWAP.OKX", "OKX", size_increment=D("1"), contract_multiplier=D("10"), min_quantity=D("1"))
    spec_hl = InstrumentSpec("FOO-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.1"), contract_multiplier=D("1"), min_quantity=D("1"))

    aligned, err = align_leg_quantities(spec_hl, spec_okx, target_usd=D("50"), mid_price=D("0.15"))
    assert err is None and aligned is not None
    assert aligned.long_base_qty == aligned.short_base_qty
    assert aligned.mismatch_pct == D("0")
    # 50 / 0.15 = 333.3 -> floored to 330 base (= 33 OKX contracts = $49.50)
    assert aligned.short_qty_contracts == D("33")
    assert aligned.long_qty_contracts == D("330.0")

    # Min lot exceeding slice_usd rejects cleanly
    spec_huge = InstrumentSpec("HUGE-USDT-SWAP.OKX", "OKX", size_increment=D("1"), contract_multiplier=D("1"), min_quantity=D("1"))
    res_bad, err_bad = align_leg_quantities(spec_hl, spec_huge, target_usd=D("50"), mid_price=D("120"))
    assert res_bad is None and err_bad == "min_lot_exceeds_target_usd"

    # Incompatible step ratio (>2% mismatch at $50) rejects cleanly
    spec_odd = InstrumentSpec("ODD.BYBIT", "BYBIT", size_increment=D("17"), contract_multiplier=D("1"))
    spec_even = InstrumentSpec("ODD.OKX", "OKX", size_increment=D("13"), contract_multiplier=D("1"))
    res_mis, err_mis = align_leg_quantities(spec_odd, spec_even, target_usd=D("50"), mid_price=D("2"))
    assert res_mis is None and err_mis == "alignment_mismatch_exceeds_tolerance"


def test_funding_parsers_zero_lookahead_and_8h_settlement_gates() -> None:
    now_s = 1_800_000_000
    hl_legs = parse_hl_meta_and_ctxs(
        [
            {"universe": [{"name": "BTC"}, {"name": "xyz:NVDA"}]},
            [
                {"markPx": "60000", "midPx": "60000", "impactPxs": ["59999", "60001"], "funding": "0.00001", "dayNtlVlm": "50000000"},
                {"markPx": "140", "midPx": "140", "impactPxs": ["139.9", "140.1"], "funding": "0.00002", "dayNtlVlm": "5000000"},
            ],
        ],
        is_xyz=True,
        now_s=now_s,
    )
    assert hl_legs["BTC"].instrument_id == "BTC-USD-PERP.HYPERLIQUID"
    assert hl_legs["NVDA"].asset_class == "RWA"

    lt_legs = parse_lighter_snapshots(
        {"order_book_details": [{"symbol": "BTC", "market_id": 1, "market_type": "perp", "status": "active", "is_frozen": False, "mark_price": "60000", "daily_quote_token_volume": "1000000"}]},
        {"funding_rates": [{"exchange": "lighter", "symbol": "BTC", "rate": 0.00008}]},
        now_s=now_s,
    )
    assert lt_legs["BTC"].instrument_id == "BTC-PERP.LIGHTER"

    byb_legs = parse_bybit_tickers(
        {"result": {"list": [{"symbol": "BTCUSDT", "markPrice": "60010", "bid1Price": "60009", "ask1Price": "60011", "fundingRate": "0.0008", "fundingIntervalHour": "8", "nextFundingTime": str((now_s + 1200) * 1000), "turnover24h": "10000000"}]}},
        now_s=now_s,
    )
    assert byb_legs["BTC"].instrument_id == "BTCUSDT-LINEAR.BYBIT"
    assert byb_legs["BTC"].current_rate_bps_h == pytest.approx(1.0)

    okx_legs = parse_okx_tickers(
        {"data": [{"instId": "BTC-USDT-SWAP", "state": "live", "settleCcy": "USDT"}]},
        {"data": [{"instId": "BTC-USDT-SWAP", "last": "60000", "bidPx": "59999", "askPx": "60001", "volCcy24h": "1000"}]},
        now_s=now_s,
    )
    assert okx_legs["BTC"].instrument_id == "BTC-USDT-SWAP.OKX"
    assert okx_legs["BTC"].vol24_usd == pytest.approx(60_000_000.0)

    # Zero-lookahead: settlement strictly in future (now_s + 3600) MUST NOT be included
    r8, r24, iv = compute_trailing_rates(
        [(now_s - 8 * HOUR, 8.0), (now_s, 4.0), (now_s + HOUR, 999.0)],
        now_s,
        8,
    )
    assert iv == 8
    assert r8 == pytest.approx(0.5)
    assert r24 == pytest.approx(12.0 / 24.0)

    # Blackout before CEX settlement when CEX leg is PAYING
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    snap_blackout = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, bid=100.0, ask=100.04),
            # Short BYBIT with negative funding -> SHORT leg PAYS funding; settles in 10 minutes!
            "BYBIT": _make_leg("BYBIT", "SOL", interval_h=8, next_settle_ts=now_s + 600, r8=1.5, r24=1.5, cur_rate=-0.8, bid=100.2, ask=100.24),
        }
    }
    assert evaluate_entry_candidates(snap_blackout, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s) == []

    # Exit deferral before CEX settlement when CEX leg is RECEIVING
    do_exit, reason = should_exit_carry_decay(
        opened_ts=now_s - 8 * HOUR,
        leg_l=_make_leg("HL", "SOL", r8=0.1, r24=0.1),
        leg_s=_make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 600, r8=0.2, cur_rate=0.5),
        policy=plan.policy,
        now_s=now_s,
    )
    assert not do_exit and reason == "defer_collect_cex_settlement"


def test_split_zero_vol_spread_and_dynamic_subscriptions() -> None:
    now_s = 1_800_000_000
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")

    # Good pair + 3 poisoned pairs (1h split divergence, zero 1h vol, wide spread)
    snap = {
        "GOOD": {
            "HL": _make_leg("HL", "GOOD", r8=0.1, r24=0.1, bid=100.0, ask=100.05),
            "OKX": _make_leg("OKX", "GOOD", interval_h=8, next_settle_ts=now_s + 7200, r8=1.6, r24=1.5, cur_rate=1.6, bid=100.2, ask=100.24),
        },
        "SPLIT1H": {
            "HL": _make_leg("HL", "SPLIT1H", r8=0.1, r24=0.1, bid=100.0, ask=100.05, prev_1h=100.0),
            "BYBIT": _make_leg("BYBIT", "SPLIT1H", interval_h=8, next_settle_ts=now_s + 7200, r8=1.6, r24=1.5, cur_rate=1.6, bid=100.2, ask=100.24, prev_1h=130.0),
        },
        "FROZEN": {
            "HL": _make_leg("HL", "FROZEN", r8=0.1, r24=0.1, bid=100.0, ask=100.05, vol1h=0.0),
            "OKX": _make_leg("OKX", "FROZEN", interval_h=8, next_settle_ts=now_s + 7200, r8=1.6, r24=1.5, cur_rate=1.6, bid=100.2, ask=100.24),
        },
        "WIDESP": {
            "HL": _make_leg("HL", "WIDESP", r8=0.1, r24=0.1, bid=99.5, ask=100.5),
            "OKX": _make_leg("OKX", "WIDESP", interval_h=8, next_settle_ts=now_s + 7200, r8=1.6, r24=1.5, cur_rate=1.6, bid=100.2, ask=100.24),
        },
        "SNDK": {
            "HL": _make_leg("HL", "SNDK", asset_class="RWA", r8=0.1, r24=0.1, bid=100.0, ask=100.05),
            "OKX": _make_leg("OKX", "SNDK", asset_class="RWA", interval_h=8, next_settle_ts=now_s + 7200, r8=1.6, r24=1.5, cur_rate=1.6, bid=100.2, ask=100.24),
        },
    }
    cands = evaluate_entry_candidates(
        snap,
        open_symbols=set(),
        venue_notional_used={},
        excluded_symbols=set(plan.excluded_symbols),
        policy=plan.policy,
        risk=plan.risk,
        now_s=now_s,
    )
    assert [c.symbol for c in cands] == ["GOOD"]

    # Dynamic subscription rotation with 600s hysteresis
    mgr = SubscriptionManager(top_k=1, unsubscribe_hysteresis_secs=600)
    sub1, unsub1 = mgr.reconcile(open_instrument_ids=set(), ranked_candidates=cands, now_s=now_s)
    assert set(sub1) == {"GOOD-USD-PERP.HYPERLIQUID", "GOOD-USDT-SWAP.OKX"} and unsub1 == []

    # New hot coin replaces GOOD, while HOLD stays open on HL/OKX
    cands_hot = evaluate_entry_candidates(
        {
            "HOT": {
                "LIGHTER": _make_leg("LIGHTER", "HOT", r8=0.0, r24=0.0, bid=100.0, ask=100.02),
                "BYBIT": _make_leg("BYBIT", "HOT", interval_h=8, next_settle_ts=now_s + 7200, r8=3.0, r24=3.0, cur_rate=3.0, bid=100.3, ask=100.34),
            }
        },
        open_symbols=set(),
        venue_notional_used={},
        excluded_symbols=set(),
        policy=plan.policy,
        risk=plan.risk,
        now_s=now_s + 120,
    )
    # GOOD dropped from candidates, but hysteresis 120s < 600s keeps GOOD subscribed
    sub2, unsub2 = mgr.reconcile(open_instrument_ids=set(), ranked_candidates=cands_hot, now_s=now_s + 120)
    assert set(sub2) == {"HOT-PERP.LIGHTER", "HOTUSDT-LINEAR.BYBIT"} and unsub2 == []
    # After 600s, GOOD is cleanly unsubscribed, while HOT stays
    sub3, unsub3 = mgr.reconcile(open_instrument_ids=set(), ranked_candidates=cands_hot, now_s=now_s + 721)
    assert sub3 == [] and set(unsub3) == {"GOOD-USD-PERP.HYPERLIQUID", "GOOD-USDT-SWAP.OKX"}


def test_basis_30m_persistence_vs_spike_and_fast_risk_guards() -> None:
    tracker = BasisStopTracker(basis_stop_bps=D("200"), persist_mins=30)
    # 250 bps adverse spike for 29m -> NO trigger, then recovers -> reset
    for m in range(0, 29 * 60 + 1, 60):
        hit, _ = tracker.observe(position_id="p1", entry_basis_bps=10.0, basis_now_bps=260.0, now_s=1000 + m)
        assert not hit
    hit_rec, elapsed_rec = tracker.observe(position_id="p1", entry_basis_bps=10.0, basis_now_bps=150.0, now_s=1000 + 29 * 60 + 30)
    assert not hit_rec and elapsed_rec == 0

    # Continuous 30m breach triggers at t=30m
    t0 = 10_000
    for dt in (0, 600, 1200, 1799):
        hit, _ = tracker.observe(position_id="p1", entry_basis_bps=10.0, basis_now_bps=215.0, now_s=t0 + dt)
        assert not hit
    hit_30, _ = tracker.observe(position_id="p1", entry_basis_bps=10.0, basis_now_bps=215.0, now_s=t0 + 1800)
    assert hit_30

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    fast = FastRiskEvaluator(plan.risk)
    # 25% single-leg adverse move triggers instant margin guard
    dec_mg = fast.evaluate(
        positions={"p1": {"entry_l": 100.0, "entry_s": 100.2, "iid_long": "L", "iid_short": "S", "long_base_qty": "1", "short_base_qty": "1"}},
        live_mids={"L": 74.9, "S": 75.0},
        daily_realized_pnl_usd=D("0"),
        consecutive_unhedged=0,
        now_s=100,
    )
    assert dec_mg.close_positions == (("p1", "margin_guard_25pct"),)

    # Unhedged > $60 for >= 10s triggers kill + halt
    pos_unhedged = {"p2": {"entry_l": 100.0, "entry_s": 100.0, "iid_long": "L", "iid_short": "S", "long_base_qty": "1.0", "short_base_qty": "0.2"}}
    d1 = fast.evaluate(positions=pos_unhedged, live_mids={"L": 100.0, "S": 100.0}, daily_realized_pnl_usd=D("0"), consecutive_unhedged=0, now_s=1000)
    assert not d1.halt_system
    d2 = fast.evaluate(positions=pos_unhedged, live_mids={"L": 100.0, "S": 100.0}, daily_realized_pnl_usd=D("0"), consecutive_unhedged=0, now_s=1010)
    assert d2.halt_system and d2.halt_reason == "unhedged_timeout_kill"

    # Daily loss kill at -$40
    d3 = fast.evaluate(positions=pos_unhedged, live_mids={"L": 100.0, "S": 100.0}, daily_realized_pnl_usd=D("-40"), consecutive_unhedged=0, now_s=1015)
    assert d3.halt_system and d3.halt_reason == "daily_loss_kill"


def test_sqlite_ledger_persistence_and_startup_reconciliation(tmp_path: Path) -> None:
    db_path = tmp_path / "carry.sqlite3"
    led = CarryLedger(db_path, "fp-v1")
    led.create_position(
        position_id="SOL-1", symbol="SOL", long_venue="HL", short_venue="OKX",
        iid_long="SOL-USD-PERP.HYPERLIQUID", iid_short="SOL-USDT-SWAP.OKX",
        opened_ts=100, entry_l=100.0, entry_s=100.2, entry_basis_bps=20.0, target_base_qty=D("1"),
    )
    led.update_position_status("SOL-1", status="OPEN", ts_s=101, long_base_qty=D("1"), short_base_qty=D("1"))
    # Exclusive lock rejects concurrent second process
    with pytest.raises(LedgerOwnershipError):
        CarryLedger(db_path, "fp-v1")
    led.close()

    # Reopen and verify exact match succeeds
    led2 = CarryLedger(db_path, "fp-v1")
    ok_res = led2.reconcile_startup(
        venue_positions={("HL", "SOL-USD-PERP.HYPERLIQUID"): D("1"), ("OKX", "SOL-USDT-SWAP.OKX"): D("-1")},
        venue_open_orders={},
        ts_s=200,
    )
    assert ok_res.ok and not ok_res.halted

    # Mismatch triggers HALTED_RECONCILE
    bad_res = led2.reconcile_startup(
        venue_positions={("HL", "SOL-USD-PERP.HYPERLIQUID"): D("0.5"), ("OKX", "SOL-USDT-SWAP.OKX"): D("-1")},
        venue_open_orders={("HL", "SOL-USD-PERP.HYPERLIQUID"): 1},
        ts_s=201,
    )
    assert not bad_res.ok and bad_res.halted
    assert led2.is_halted()[0]
    led2.close()


def test_sliced_maker_partial_fill_hedge_progression_and_emergency_close(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    led = CarryLedger(tmp_path / "exec.sqlite3", plan.fingerprint())
    gw = PaperVenueGateway(fill_maker_immediately=False)

    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l, D("100"))
    gw.register_spec(spec_s, D("100.2"))

    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime)
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=1.5, r24=1.5, cur_rate=1.5, bid=100.18, ask=100.22),
        }
    }
    cand = evaluate_entry_candidates(snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s)[0]
    pid, err = ctrl.start_entry(cand, specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}, now_s=now_s)
    assert err is None and pid is not None

    # Slice 1 ($50 cap @ mid=100.1 -> 0.49 SOL) registered in ledger as POST_ONLY Maker on HL (DEX leg)
    mk_coids = list(ctrl._active_makers.keys())
    assert len(mk_coids) == 1
    mk_coid = mk_coids[0]
    db_ord = led.open_orders_for_position(pid)[0]
    assert db_ord["venue"] == "HL" and Decimal(db_ord["base_qty"]) == D("0.49")

    # Partial fill 0.20 -> full fill 0.49 triggers immediate OKX IOC hedge
    gw.trigger_maker_fill(mk_coid, base_qty=D("0.20"))
    ctrl.poll_active_makers(live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2}, now_s=now_s + 2)
    pos = led.active_positions()[pid]
    assert Decimal(pos["long_base_qty"]) == D("0.20") and Decimal(pos["short_base_qty"]) == D("0.20")

    gw.trigger_maker_fill(mk_coid, base_qty=D("0.49"))
    ctrl.poll_active_makers(live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2}, now_s=now_s + 4)
    pos = led.active_positions()[pid]
    assert Decimal(pos["long_base_qty"]) == D("0.49") and Decimal(pos["short_base_qty"]) == D("0.49")
    assert pos["status"] == "OPENING"

    # Advance slice 2 (remaining 0.50 capped at $50 -> 0.49 SOL) and fill -> 0.98 >= 98% of 0.99 -> OPEN
    advanced = ctrl.advance_pending_slices(
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        quotes={spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.18, 100.22)},
        mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2},
        now_s=now_s + 5,
    )
    assert advanced == [pid]
    mk_coid2 = list(ctrl._active_makers.keys())[0]
    gw.trigger_maker_fill(mk_coid2, base_qty=D("0.49"))
    ctrl.poll_active_makers(live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2}, now_s=now_s + 6)
    pos = led.active_positions()[pid]
    assert pos["status"] == "OPEN" and Decimal(pos["long_base_qty"]) == D("0.98") and Decimal(pos["short_base_qty"]) == D("0.98")

    # Emergency dual-leg close flattens to CLOSED
    ctrl.emergency_close_position(pid, specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}, reason="margin_guard_25pct", now_s=now_s + 10)
    assert led.active_positions() == {}
    led.close()


def test_cli_dry_run_and_live_safety_refusal(capsys: pytest.CaptureFixture[str]) -> None:
    for cfg_name in ("carry-paper.toml", "carry-live.toml"):
        rc = carry_cli_main(["--config", str(REPO_ROOT / "config" / cfg_name), "--dry-run"])
        assert rc == 0
        out = json.loads(capsys.readouterr().out)
        assert out["action"] == "dry_run"
        assert out["plan"]["venues"] == ["HL", "LIGHTER", "OKX", "BYBIT"]
        assert out["plan"]["risk"]["max_slots"] == 6
        assert out["plan"]["risk"]["leg_notional_usd"] == "100"
        assert out["plan"]["risk"]["slice_usd"] == "50"
        assert out["plan"]["risk"]["max_venue_notional_usd"] == "400"

    # Must refuse `--mode live --run` without `--execute-live`
    rc_refuse = carry_cli_main(["--config", str(REPO_ROOT / "config" / "carry-live.toml"), "--mode", "live", "--run"])
    assert rc_refuse == 2


def test_recorder_slow_snapshot_fast_bbo_throttle_rotation_and_reopen(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    rec_dir = tmp_path / "tape"
    rec = CarryRecorder(rec_dir, bbo_interval_secs=1)

    t0 = 1_800_000_000  # 2027-01-15 UTC
    snap = {
        "SINGLE_ONLY": {"HL": _make_leg("HL", "SINGLE_ONLY", r8=2.0, r24=2.0)},
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.1, r24=0.1, bid=100.0, ask=100.04),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=t0 + 7200, r8=1.4, r24=1.4, bid=100.2, ask=100.24),
        },
    }
    cands = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=t0
    )
    assert len(cands) == 1
    assert rec.record_slow_snapshot(ts_s=t0, snapshot=snap, candidates=cands, active_count=1, subscribed_count=2)

    assert rec.record_bbo(instrument_id="SOL-USD-PERP.HYPERLIQUID", bid=100.0, ask=100.04, mid=100.02, now_s=float(t0), event_ts_ns=1234)
    assert not rec.record_bbo(instrument_id="SOL-USD-PERP.HYPERLIQUID", bid=100.01, ask=100.05, mid=100.03, now_s=float(t0) + 0.4)
    assert rec.record_bbo(instrument_id="SOL-USD-PERP.HYPERLIQUID", bid=100.02, ask=100.06, mid=100.04, now_s=float(t0) + 1.0)
    # Next UTC day rotates file
    assert rec.record_bbo(instrument_id="SOL-USD-PERP.HYPERLIQUID", bid=100.10, ask=100.14, mid=100.12, now_s=float(t0 + 86400))
    rec.close()

    # Reopen on the same day and append one more BBO line cleanly
    rec2 = CarryRecorder(rec_dir, bbo_interval_secs=1)
    assert rec2.record_bbo(instrument_id="SOL-USDT-SWAP.OKX", bid=100.20, ask=100.24, mid=100.22, now_s=float(t0 + 86401))
    rec2.close()

    slow_files = sorted(rec_dir.glob("slow_snapshots_*.jsonl.gz"))
    fast_files = sorted(rec_dir.glob("fast_bbo_*.jsonl.gz"))
    assert len(slow_files) == 1
    assert len(fast_files) == 2

    with gzip.open(slow_files[0], "rt", encoding="utf-8") as fh:
        slow_rows = [json.loads(line) for line in fh]
    assert len(slow_rows) == 1
    assert {leg["s"] for leg in slow_rows[0]["legs"]} == {"SOL"}
    assert len(slow_rows[0]["legs"]) == 2
    assert slow_rows[0]["candidates"][0]["s"] == "SOL"

    with gzip.open(fast_files[0], "rt", encoding="utf-8") as fh:
        day1_rows = [json.loads(line) for line in fh]
    with gzip.open(fast_files[1], "rt", encoding="utf-8") as fh:
        day2_rows = [json.loads(line) for line in fh]
    assert len(day1_rows) == 2 and day1_rows[0]["ev_ns"] == 1234
    assert len(day2_rows) == 2 and day2_rows[1]["iid"] == "SOL-USDT-SWAP.OKX"


def test_recorder_config_fingerprint_stability_and_watch_capacity_separation(tmp_path: Path) -> None:
    paper_plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    assert paper_plan.runtime.record_dir == (REPO_ROOT / "reports" / "carry" / "tape-paper").resolve()
    assert paper_plan.runtime.bbo_record_interval_secs == 1

    alt_cfg = tmp_path / "carry-norec.toml"
    alt_text = (REPO_ROOT / "config" / "carry-paper.toml").read_text(encoding="utf-8")
    alt_text = alt_text.replace('state_path = "../reports/carry/paper.sqlite3"', f'state_path = "{paper_plan.state_path}"')
    alt_text = alt_text.replace('record_dir = "../reports/carry/tape-paper"', 'record_dir = ""')
    alt_text = alt_text.replace("bbo_record_interval_secs = 1", "bbo_record_interval_secs = 5")
    alt_cfg.write_text(alt_text, encoding="utf-8")
    alt_plan = load_carry_plan(alt_cfg)
    assert alt_plan.runtime.record_dir is None
    assert alt_plan.runtime.bbo_record_interval_secs == 5
    assert alt_plan.fingerprint() == paper_plan.fingerprint()

    bad_cfg = tmp_path / "carry-badrec.toml"
    bad_cfg.write_text(alt_text.replace("bbo_record_interval_secs = 5", "bbo_record_interval_secs = 0"), encoding="utf-8")
    with pytest.raises(CarryConfigError):
        load_carry_plan(bad_cfg)

    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.1, r24=0.1, bid=100.0, ask=100.04),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=1.5, r24=1.5, bid=100.2, ask=100.24),
        }
    }
    full_slots = {"A", "B", "C", "D", "E", "F"}
    assert (
        evaluate_entry_candidates(
            snap,
            open_symbols=full_slots,
            venue_notional_used={"HL": D("400"), "OKX": D("400")},
            excluded_symbols=set(),
            policy=paper_plan.policy,
            risk=paper_plan.risk,
            now_s=now_s,
            respect_capacity=True,
        )
        == []
    )
    watch = evaluate_entry_candidates(
        snap,
        open_symbols=full_slots,
        venue_notional_used={"HL": D("400"), "OKX": D("400")},
        excluded_symbols=set(),
        policy=paper_plan.policy,
        risk=paper_plan.risk,
        now_s=now_s,
        respect_capacity=False,
    )
    assert len(watch) == 1 and watch[0].symbol == "SOL"


def test_watch_fill_expands_subscriptions_without_relaxing_strict_entries(tmp_path: Path) -> None:
    paper_plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    live_plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    assert paper_plan.runtime.watch_top_k == 30
    assert paper_plan.runtime.watch_fill_min_carry_bps_h == D("0.35")
    assert live_plan.runtime.watch_top_k == 15
    assert live_plan.runtime.watch_fill_min_carry_bps_h == D("0.35")

    alt_cfg = tmp_path / "carry-altfill.toml"
    alt_text = (REPO_ROOT / "config" / "carry-paper.toml").read_text(encoding="utf-8")
    alt_text = alt_text.replace('state_path = "../reports/carry/paper.sqlite3"', f'state_path = "{paper_plan.state_path}"')
    alt_text = alt_text.replace('watch_fill_min_carry_bps_h = "0.35"', 'watch_fill_min_carry_bps_h = "0.20"')
    alt_cfg.write_text(alt_text, encoding="utf-8")
    assert load_carry_plan(alt_cfg).fingerprint() == paper_plan.fingerprint()

    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.1, r24=0.1, bid=100.0, ask=100.04),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=1.5, r24=1.5, bid=100.2, ask=100.24),
        },
        "ETH": {
            "HL": _make_leg("HL", "ETH", r8=0.1, r24=0.1, bid=2000.0, ask=2000.4),
            "OKX": _make_leg("OKX", "ETH", interval_h=8, next_settle_ts=now_s + 7200, r8=0.60, r24=0.55, bid=2000.4, ask=2000.8),
        },
        "XRP": {
            "HL": _make_leg("HL", "XRP", r8=0.1, r24=0.1, bid=1.0, ask=1.001),
            "OKX": _make_leg("OKX", "XRP", interval_h=8, next_settle_ts=now_s + 7200, r8=0.20, r24=0.20, bid=1.002, ask=1.003),
        },
    }

    strict = evaluate_entry_candidates(
        snap,
        open_symbols=set(),
        venue_notional_used={},
        excluded_symbols=set(),
        policy=paper_plan.policy,
        risk=paper_plan.risk,
        now_s=now_s,
        respect_capacity=False,
    )
    assert [c.symbol for c in strict] == ["SOL"]

    watch = evaluate_watch_candidates(
        snap,
        strict_candidates=strict,
        open_symbols=set(),
        excluded_symbols=set(),
        policy=paper_plan.policy,
        top_k=paper_plan.runtime.watch_top_k,
        min_watch_carry_bps_h=float(paper_plan.runtime.watch_fill_min_carry_bps_h),
    )
    assert [c.symbol for c in watch] == ["SOL", "ETH"]

    subs = SubscriptionManager(top_k=paper_plan.runtime.watch_top_k, unsubscribe_hysteresis_secs=600)
    to_sub, to_unsub = subs.reconcile(open_instrument_ids=set(), ranked_candidates=watch, now_s=now_s)
    assert to_unsub == []
    assert set(to_sub) == {
        "ETH-USD-PERP.HYPERLIQUID",
        "ETH-USDT-SWAP.OKX",
        "SOL-USD-PERP.HYPERLIQUID",
        "SOL-USDT-SWAP.OKX",
    }


def test_carry_strategy_implements_nautilus_on_quote_and_records_bbo(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from nautilus_trader.trading import Strategy
    from src.carry.runtime import _create_carry_strategy, build_data_clients

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    clients = {cid: cfg for cid, _fac, cfg in build_data_clients(plan)}
    assert clients["HYPERLIQUID"].bbo_cache_only is False
    led = CarryLedger(tmp_path / "strat.sqlite3", plan.fingerprint())
    rec_dir = tmp_path / "tape"
    rec = CarryRecorder(rec_dir, bbo_interval_secs=1)
    strat = _create_carry_strategy(plan, led, mode="paper", recorder=rec)
    try:
        assert hasattr(Strategy, "on_quote")
        assert type(strat).on_quote is not Strategy.on_quote
        assert not hasattr(strat, "on_quote_tick")
        ev_ns = time.time_ns()
        strat.on_quote(SimpleNamespace(instrument_id="SOL-USDT-SWAP.OKX", bid_price=100.0, ask_price=100.04, ts_event=ev_ns))
        assert strat.live_quotes["SOL-USDT-SWAP.OKX"] == (100.0, 100.04)
    finally:
        rec.close()
        led.close()

    fast_files = sorted(rec_dir.glob("fast_bbo_*.jsonl.gz"))
    assert len(fast_files) == 1
    with gzip.open(fast_files[0], "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    assert len(rows) == 1
    assert rows[0]["iid"] == "SOL-USDT-SWAP.OKX" and rows[0]["ev_ns"] == ev_ns


def test_stale_live_quotes_are_evicted_and_never_corrupt_slow_snapshot(tmp_path: Path) -> None:
    from src.carry.runtime import _create_carry_strategy

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    assert plan.runtime.quote_stale_secs == 15
    led = CarryLedger(tmp_path / "stale.sqlite3", plan.fingerprint())
    strat = _create_carry_strategy(plan, led, mode="paper")
    try:
        iid = "BIGTIME-USDT-SWAP.OKX"
        strat.live_quotes[iid] = (0.00639, 0.006393)
        strat.live_mids[iid] = 0.0063915
        strat.live_quote_ts[iid] = 1000.0
        fresh_rest = _make_leg("OKX", "BIGTIME", bid=0.006524, ask=0.006527)
        strat.snapshot = {"BIGTIME": {"OKX": fresh_rest}}

        # Age 5s <= 15s -> fresh WS quote overlays snapshot
        strat._overlay_fresh_quotes(1005.0)
        assert strat.snapshot["BIGTIME"]["OKX"].bid == 0.00639

        # Reset to fresh REST and evaluate at age 20s > 15s -> stale quote evicted, REST kept
        strat.snapshot = {"BIGTIME": {"OKX": fresh_rest}}
        strat._overlay_fresh_quotes(1020.0)
        assert iid not in strat.live_quotes
        assert iid not in strat.live_quote_ts
        assert strat.snapshot["BIGTIME"]["OKX"].bid == 0.006524
        assert strat.live_mids[iid] == pytest.approx(0.0065255)

        strat._evict_quote(iid)
        assert iid not in strat.live_mids
    finally:
        led.close()


def test_live_carry_alignment_okx_cur_rate_and_60bps_basis_cap() -> None:
    paper_plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    live_plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    assert paper_plan.policy.max_entry_basis_bps == D("60")
    assert live_plan.policy.max_entry_basis_bps == D("60")
    assert paper_plan.risk.stop_loss_cooldown_hours == 24
    assert live_plan.risk.stop_loss_cooldown_hours == 24

    now_s = 1_800_000_000
    snap = {
        # High-basis runaway (> 60 bps cap) -> rejected
        "WIDE": {
            "HL": _make_leg("HL", "WIDE", r8=0.0, r24=0.0, cur_rate=0.0, bid=100.0, ask=100.02),
            "BYBIT": _make_leg("BYBIT", "WIDE", interval_h=4, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.89, ask=100.91),
        },
        # Lagging 1h vs 4h trap: trailing r8/r24 look good, but live predicted carry is inverted (-84 < 0.25) -> rejected
        "LAGTRAP": {
            "HL": _make_leg("HL", "LAGTRAP", r8=-29.0, r24=-15.0, cur_rate=-50.0, bid=100.0, ask=100.02),
            "OKX": _make_leg("OKX", "LAGTRAP", interval_h=4, next_settle_ts=now_s + 7200, r8=-12.0, r24=-5.0, cur_rate=-134.0, bid=100.34, ask=100.36),
        },
        # Healthy aligned pair (basis = 35 bps <= 60, c_live = 1.5 >= 0.25) -> accepted
        "GOOD": {
            "HL": _make_leg("HL", "GOOD", r8=0.0, r24=0.0, cur_rate=0.0, bid=100.0, ask=100.02),
            "BYBIT": _make_leg("BYBIT", "GOOD", interval_h=4, next_settle_ts=now_s + 7200, r8=1.5, r24=1.5, cur_rate=1.5, bid=100.34, ask=100.36),
        },
    }
    cands = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(),
        policy=paper_plan.policy, risk=paper_plan.risk, now_s=now_s,
    )
    assert [c.symbol for c in cands] == ["GOOD"]

    # Verify OKX detail parser populates current_rate_bps_h and caches it across ticks
    def fake_http(url: str, _body: dict | None = None):
        if "public/funding-rate?" in url:
            return {"data": [{"fundingRate": "-0.012", "fundingTime": str((now_s + 3600) * 1000), "nextFundingTime": str((now_s + 5 * 3600) * 1000)}]}
        if "funding-rate-history" in url:
            return {"data": [{"fundingTime": str((now_s - 3 * 3600) * 1000), "realizedRate": "-0.004"}]}
        if "market/candles" in url:
            return {"data": [[str((now_s - 7200) * 1000), "1", "1", "1", "10.0", "100", "0", "10000"], [str((now_s - 3600) * 1000), "1", "1", "1", "10.1", "100", "0", "10000"]]}
        return {}

    feed = PublicCarryFeed(venues=("OKX",), http_get=fake_http)
    leg = _make_leg("OKX", "SOL", interval_h=8, r8=0.0, r24=0.0, cur_rate=None)
    leg = LegMarketInfo(**{**leg.__dict__, "current_rate_bps_h": None})
    enriched = feed._fetch_leg_detail(leg, now_s)
    assert enriched.current_rate_bps_h == pytest.approx(-30.0)
    cached_enriched = feed._fetch_leg_detail(leg, now_s + 60)
    assert cached_enriched.current_rate_bps_h == pytest.approx(-30.0)


def test_stop_loss_cooldown_and_realized_pnl_accounting(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    led = CarryLedger(tmp_path / "pnl.sqlite3", plan.fingerprint())
    gw = PaperVenueGateway(fill_maker_immediately=True)
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l, D("100.0"))
    gw.register_spec(spec_s, D("100.2"))

    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime)
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=100.0, ask=100.0),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=1.5, r24=1.5, cur_rate=1.5, bid=100.2, ask=100.2),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]
    pid, err = ctrl.start_entry(cand, specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}, now_s=now_s)
    assert err is None and pid is not None
    ctrl.advance_pending_slices(
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        quotes={spec_l.instrument_id: (100.0, 100.0), spec_s.instrument_id: (100.2, 100.2)},
        mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2},
        now_s=now_s + 2,
    )
    pos = led.active_positions()[pid]
    assert pos["status"] == "OPEN"
    q = D(pos["long_base_qty"])

    # Move short leg up adversely by +3.00 -> actual open VWAPs (99.99 long, 100.04 short) vs close (100.00, 103.20) == -3.15 * q
    gw.register_spec(spec_l, D("100.0"))
    gw.register_spec(spec_s, D("103.2"))
    ctrl.emergency_close_position(
        pid,
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        reason="basis_stop_30m",
        now_s=now_s + 100,
        live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 103.2},
    )
    row = dict(led.conn.execute("SELECT realized_pnl_usd, close_reason FROM positions WHERE position_id=?", (pid,)).fetchone())
    assert row["close_reason"] == "basis_stop_30m"
    assert D(row["realized_pnl_usd"]) == pytest.approx(D("-3.15") * q)
    daily_pnl, _ = led.daily_stats(now_s + 100)
    assert daily_pnl == pytest.approx(D("-3.15") * q)

    assert "SOL" in led.stop_loss_cooldown_symbols(now_s=now_s + 3600, cooldown_hours=24)
    assert "SOL" not in led.stop_loss_cooldown_symbols(now_s=now_s + 25 * 3600, cooldown_hours=24)

    # Normal carry_decay close settles against actual open VWAPs (99.99 / 100.04) and triggers no stop cooldown
    gw.register_spec(spec_l, D("100.0"))
    gw.register_spec(spec_s, D("100.2"))
    pid2, _ = ctrl.start_entry(cand, specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}, now_s=now_s + 26 * 3600)
    ctrl.advance_pending_slices(
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        quotes={spec_l.instrument_id: (100.0, 100.0), spec_s.instrument_id: (100.2, 100.2)},
        mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2},
        now_s=now_s + 26 * 3600 + 2,
    )
    # Maker SELL at 100.01 (+0.02 vs 99.99 open) + IOC BUY at 100.15 (-0.11 vs 100.04 open) -> -0.09 * q
    gw.register_spec(spec_l, D("100.0"))
    gw.register_spec(spec_s, D("100.0"))
    ctrl.start_sliced_close(
        pid2,
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        quotes={spec_l.instrument_id: (100.0, 100.0), spec_s.instrument_id: (100.0, 100.0)},
        mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.0},
        reason="carry_decay",
        now_s=now_s + 26 * 3600 + 10,
    )
    ctrl.advance_pending_slices(
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        quotes={spec_l.instrument_id: (100.0, 100.0), spec_s.instrument_id: (100.0, 100.0)},
        mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.0},
        now_s=now_s + 26 * 3600 + 12,
    )
    row2 = dict(led.conn.execute("SELECT status, realized_pnl_usd FROM positions WHERE position_id=?", (pid2,)).fetchone())
    assert row2["status"] == "CLOSED"
    assert D(row2["realized_pnl_usd"]) == pytest.approx(D("-0.09") * q)
    assert "SOL" not in led.stop_loss_cooldown_symbols(now_s=now_s + 26 * 3600 + 20, cooldown_hours=24)
    led.close()


def test_hybrid_exec_config_validation_and_defaults(tmp_path: Path) -> None:
    for cfg_name in ("carry-paper.toml", "carry-live.toml"):
        plan = load_carry_plan(REPO_ROOT / "config" / cfg_name)
        assert plan.policy.min_exec_basis_bps == D("10")
        assert plan.policy.taker_fast_entry_basis_bps == D("20")

    base = (REPO_ROOT / "config" / "carry-paper.toml").read_text(encoding="utf-8")
    bad_exec = tmp_path / "bad_exec.toml"
    bad_exec.write_text(base.replace('min_exec_basis_bps = "10"', 'min_exec_basis_bps = "60"'), encoding="utf-8")
    with pytest.raises(CarryConfigError):
        load_carry_plan(bad_exec)

    bad_fast = tmp_path / "bad_fast.toml"
    bad_fast.write_text(base.replace('taker_fast_entry_basis_bps = "20"', 'taker_fast_entry_basis_bps = "65"'), encoding="utf-8")
    with pytest.raises(CarryConfigError):
        load_carry_plan(bad_fast)


def test_signal_screens_out_wide_spread_negative_exec_basis() -> None:
    now_s = 1_800_000_000
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    snap = {
        # Wide OKX ask causes maker_exec_basis = +2.99bp < 10bp despite mid_basis = +10.99bp >= 10bp
        "BADEXEC": {
            "OKX": _make_leg("OKX", "BADEXEC", interval_h=8, next_settle_ts=now_s + 7200, r8=0.0, r24=0.0, cur_rate=0.0, bid=99.95, ask=100.19),
            "HL": _make_leg("HL", "BADEXEC", r8=2.0, r24=2.0, cur_rate=2.0, bid=100.14, ask=100.22),
        },
        "GOODEXEC": {
            "OKX": _make_leg("OKX", "GOODEXEC", interval_h=8, next_settle_ts=now_s + 7200, r8=0.0, r24=0.0, cur_rate=0.0, bid=100.00, ask=100.04),
            "HL": _make_leg("HL", "GOODEXEC", r8=2.0, r24=2.0, cur_rate=2.0, bid=100.18, ask=100.22),
        },
    }
    cands = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(),
        policy=plan.policy, risk=plan.risk, now_s=now_s,
    )
    assert [c.symbol for c in cands] == ["GOODEXEC"]


def test_hybrid_dual_taker_fast_path_fills_wide_basis_synchronously(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "taker.sqlite3", plan.fingerprint())
    gw = PaperVenueGateway(fill_maker_immediately=False, use_realistic_bbo=True)
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l, D("100.00"))
    gw.register_spec(spec_s, D("100.30"))
    gw.update_quote(spec_l.instrument_id, D("99.98"), D("100.02"))
    gw.update_quote(spec_s.instrument_id, D("100.28"), D("100.32"))

    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.28, ask=100.32),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(),
        policy=plan.policy, risk=plan.risk, now_s=now_s,
    )[0]
    pid, err = ctrl.start_entry(cand, specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}, now_s=now_s)
    assert err is None and pid is not None
    assert ctrl._active_makers == {}
    ctrl.advance_pending_slices(
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        quotes={spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.28, 100.32)},
        mids={spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.30},
        now_s=now_s + 1,
    )
    pos = led.active_positions()[pid]
    assert pos["status"] == "OPEN"
    rows = led.conn.execute("SELECT role, order_type, price, status FROM orders WHERE position_id=?", (pid,)).fetchall()
    assert rows and all(r["role"] == "TAKER_OPEN" and r["order_type"] == "IOC" and r["status"] == "FILLED" for r in rows)
    assert any(D(r["price"]) == D("100.02") for r in rows)
    assert any(D(r["price"]) == D("100.28") for r in rows)
    led.close()


def test_realistic_bbo_maker_fill_and_max_attempt_timeout_abort(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "realbbo.sqlite3", plan.fingerprint())
    gw = PaperVenueGateway(fill_maker_immediately=False, use_realistic_bbo=True)
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l, D("100.00"))
    gw.register_spec(spec_s, D("100.15"))
    gw.update_quote(spec_l.instrument_id, D("99.98"), D("100.02"))
    gw.update_quote(spec_s.instrument_id, D("100.13"), D("100.17"))

    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(),
        policy=plan.policy, risk=plan.risk, now_s=now_s,
    )[0]
    pid, err = ctrl.start_entry(cand, specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}, now_s=now_s)
    assert err is None and pid is not None
    # Stays pending without optimistic fill when BBO does not cross post price (99.98)
    assert gw.update_quote(spec_l.instrument_id, D("99.99"), D("100.03")) == []
    ctrl.poll_active_makers(live_mids={spec_l.instrument_id: 100.01, spec_s.instrument_id: 100.15}, now_s=now_s + 2)
    assert D(led.active_positions()[pid]["long_base_qty"]) == D("0")

    # Touching/crossing ask <= 99.98 triggers passive Maker fill + realistic OKX IOC fill at bid 100.13
    filled_coids = gw.update_quote(spec_l.instrument_id, D("99.96"), D("99.98"))
    assert len(filled_coids) == 1
    ctrl.poll_active_makers(
        live_mids={spec_l.instrument_id: 99.97, spec_s.instrument_id: 100.15},
        now_s=now_s + 3,
        live_quotes={spec_l.instrument_id: (99.96, 99.98), spec_s.instrument_id: (100.13, 100.17)},
    )
    pos = led.active_positions()[pid]
    assert D(pos["long_base_qty"]) > D("0") and D(pos["short_base_qty"]) == D(pos["long_base_qty"])
    hg_row = led.conn.execute("SELECT price FROM orders WHERE position_id=? AND role='HEDGE_OPEN'", (pid,)).fetchone()
    assert D(hg_row["price"]) == D("100.13")

    # Separate fresh entry with max_attempts=2 times out cleanly to entry_maker_timeout
    gw2 = PaperVenueGateway(fill_maker_immediately=False, use_realistic_bbo=True)
    gw2.register_spec(spec_l, D("100.00"))
    gw2.register_spec(spec_s, D("100.15"))
    gw2.update_quote(spec_l.instrument_id, D("99.98"), D("100.02"))
    gw2.update_quote(spec_s.instrument_id, D("100.13"), D("100.17"))
    rt_fast = plan.runtime.__class__(**{**plan.runtime.__dict__, "maker_requote_secs": 3, "maker_max_attempts": 2})
    ctrl2 = CarryExecutionController(ledger=led, gateway=gw2, risk=plan.risk, runtime=rt_fast, policy=plan.policy)
    pid2, _ = ctrl2.start_entry(cand, specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}, now_s=now_s + 100)
    ctrl2.poll_active_makers(live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.15}, now_s=now_s + 104)
    ctrl2.advance_pending_slices(
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        quotes={spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)},
        mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.15},
        now_s=now_s + 104,
    )
    ctrl2.poll_active_makers(live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.15}, now_s=now_s + 108)
    ctrl2.advance_pending_slices(
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        quotes={spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)},
        mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.15},
        now_s=now_s + 108,
    )
    row2 = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id=?", (pid2,)).fetchone())
    assert row2["status"] == "CLOSED" and row2["close_reason"] == "entry_maker_timeout"
    led.close()


class DelayedAsyncVenueGateway:
    """Async gateway test double mirroring _NautilusStrategyGateway (always acks OPEN with 0 filled)."""

    def __init__(self) -> None:
        self.orders: dict[str, dict] = {}
        self.submitted: list[dict] = []
        self.cancel_requests: list[str] = []
        self.specs: dict[str, InstrumentSpec] = {}

    def register_spec(self, spec: InstrumentSpec) -> None:
        self.specs[spec.instrument_id] = spec

    def _base_qty(self, iid: str, qty_contracts: Decimal) -> Decimal:
        spec = self.specs.get(iid)
        mult = spec.contract_multiplier if spec is not None else Decimal("1")
        return qty_contracts * mult

    def submit_limit(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        price: Decimal,
        post_only: bool,
        ioc: bool,
        reduce_only: bool,
    ) -> OrderAck:
        base_qty = self._base_qty(instrument_id, qty_contracts)
        rec = {
            "client_order_id": client_order_id,
            "venue": venue,
            "instrument_id": instrument_id,
            "side": side,
            "order_type": "POST_ONLY" if post_only else ("IOC" if ioc else "LIMIT"),
            "qty_contracts": qty_contracts,
            "base_qty": base_qty,
            "price": price,
            "reduce_only": reduce_only,
            "status": "OPEN",
            "filled_base_qty": Decimal("0"),
            "avg_fill_price": None,
            "venue_order_id": "",
            "reason": None,
        }
        self.orders[client_order_id] = rec
        self.submitted.append(rec)
        return OrderAck(client_order_id, "", "OPEN", Decimal("0"), None)

    def submit_market(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        reduce_only: bool,
    ) -> OrderAck:
        base_qty = self._base_qty(instrument_id, qty_contracts)
        rec = {
            "client_order_id": client_order_id,
            "venue": venue,
            "instrument_id": instrument_id,
            "side": side,
            "order_type": "MARKET",
            "qty_contracts": qty_contracts,
            "base_qty": base_qty,
            "price": None,
            "reduce_only": reduce_only,
            "status": "OPEN",
            "filled_base_qty": Decimal("0"),
            "avg_fill_price": None,
            "venue_order_id": "",
            "reason": None,
        }
        self.orders[client_order_id] = rec
        self.submitted.append(rec)
        return OrderAck(client_order_id, "", "OPEN", Decimal("0"), None)

    def cancel_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        self.cancel_requests.append(client_order_id)
        return self.poll_order(client_order_id=client_order_id, venue=venue, instrument_id=instrument_id)

    def poll_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        rec = self.orders[client_order_id]
        return OrderAck(
            client_order_id,
            rec["venue_order_id"],
            rec["status"],
            rec["filled_base_qty"],
            rec["avg_fill_price"],
            rec["reason"],
        )

    def deliver_fill(
        self,
        client_order_id: str,
        *,
        filled_base_qty: Decimal,
        avg_fill_price: Decimal,
        partial: bool = False,
        venue_order_id: str = "VID-1",
    ) -> None:
        rec = self.orders[client_order_id]
        rec["filled_base_qty"] = min(rec["base_qty"], filled_base_qty)
        rec["avg_fill_price"] = avg_fill_price
        rec["venue_order_id"] = venue_order_id
        rec["status"] = "PARTIAL" if partial else "FILLED"

    def deliver_cancel(
        self,
        client_order_id: str,
        *,
        filled_base_qty: Decimal | None = None,
        avg_fill_price: Decimal | None = None,
        venue_order_id: str = "VID-1",
    ) -> None:
        rec = self.orders[client_order_id]
        if filled_base_qty is not None:
            rec["filled_base_qty"] = min(rec["base_qty"], filled_base_qty)
        if avg_fill_price is not None:
            rec["avg_fill_price"] = avg_fill_price
        rec["venue_order_id"] = venue_order_id
        rec["status"] = "CANCELED"

    def deliver_reject(self, client_order_id: str, *, reason: str = "rejected") -> None:
        rec = self.orders[client_order_id]
        rec["status"] = "REJECTED"
        rec["reason"] = reason


def test_async_dual_taker_delayed_fills_block_duplicate_orders_and_settle(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "async_dt.sqlite3", plan.fingerprint())
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.28, 100.32)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.30}

    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.28, ask=100.32),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]
    pid, err = ctrl.start_entry(cand, specs=specs, now_s=now_s)
    assert err is None and pid is not None

    # Round 1 submits 2 TAKER_OPEN orders (OPEN, 0 filled) and ledger shows 0/0
    rows = led.conn.execute("SELECT client_order_id, role, status, filled_base_qty FROM orders WHERE position_id=? ORDER BY client_order_id", (pid,)).fetchall()
    assert len(rows) == 2 and all(r["role"] == "TAKER_OPEN" and r["status"] == "OPEN" and D(r["filled_base_qty"]) == D("0") for r in rows)
    pos = led.active_positions()[pid]
    assert pos["status"] == "OPENING" and D(pos["long_base_qty"]) == D("0") and D(pos["short_base_qty"]) == D("0")

    # Repeated advance_pending_slices while both IOCs are OPEN must NOT submit duplicate orders
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 1) == []
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 2) == []
    assert len(gw.submitted) == 2

    # Delayed fill on long leg only -> updates long leg, does NOT trigger fallback while short is still OPEN
    coid_l = next(o["client_order_id"] for o in gw.submitted if o["side"] == "BUY")
    coid_s = next(o["client_order_id"] for o in gw.submitted if o["side"] == "SELL")
    gw.deliver_fill(coid_l, filled_base_qty=D("0.49"), avg_fill_price=D("100.02"), venue_order_id="HL-1")
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    pos = led.active_positions()[pid]
    assert D(pos["long_base_qty"]) == D("0.49") and D(pos["short_base_qty"]) == D("0")
    assert len(gw.submitted) == 2
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 3) == []

    # Delayed fill on short leg completes Slice 1 and updates order fill prices
    gw.deliver_fill(coid_s, filled_base_qty=D("0.49"), avg_fill_price=D("100.28"), venue_order_id="OKX-1")
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 4, live_quotes=quotes)
    pos = led.active_positions()[pid]
    assert D(pos["short_base_qty"]) == D("0.49") and pos["status"] == "OPENING"
    assert ctrl._active_dual_takers == {}
    prices = {r["client_order_id"]: D(r["price"]) for r in led.conn.execute("SELECT client_order_id, price FROM orders WHERE position_id=?", (pid,)).fetchall()}
    assert prices[coid_l] == D("100.02") and prices[coid_s] == D("100.28")

    # Slice 2 submits cleanly and promotes to OPEN upon delayed fills
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 5) == [pid]
    assert len(gw.submitted) == 4
    for rec in gw.submitted[2:]:
        px = D("100.02") if rec["side"] == "BUY" else D("100.28")
        gw.deliver_fill(rec["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=px)
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 6, live_quotes=quotes)
    pos = led.active_positions()[pid]
    assert pos["status"] == "OPEN" and D(pos["long_base_qty"]) == D("0.98") and D(pos["short_base_qty"]) == D("0.98")

    # Delayed double-cancel increments attempts and closes cleanly at maker_max_attempts
    gw2 = DelayedAsyncVenueGateway()
    gw2.register_spec(spec_l)
    gw2.register_spec(spec_s)
    rt_fast = plan.runtime.__class__(**{**plan.runtime.__dict__, "maker_max_attempts": 2})
    ctrl2 = CarryExecutionController(ledger=led, gateway=gw2, risk=plan.risk, runtime=rt_fast, policy=plan.policy)
    pid2, _ = ctrl2.start_entry(cand, specs=specs, now_s=now_s + 20)
    assert pid2 is not None
    for rec in gw2.submitted[:2]:
        gw2.deliver_cancel(rec["client_order_id"], filled_base_qty=D("0"))
    ctrl2.poll_active_orders(live_mids=mids, now_s=now_s + 21, live_quotes=quotes)
    assert ctrl2._slice_attempts[pid2] == 1
    assert ctrl2.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 22) == [pid2]
    assert len(gw2.submitted) == 4
    for rec in gw2.submitted[2:]:
        gw2.deliver_cancel(rec["client_order_id"], filled_base_qty=D("0"))
    ctrl2.poll_active_orders(live_mids=mids, now_s=now_s + 23, live_quotes=quotes)
    assert ctrl2._slice_attempts[pid2] == 2
    assert ctrl2.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 24) == []
    row2 = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id=?", (pid2,)).fetchone())
    assert row2["status"] == "CLOSED" and row2["close_reason"] == "entry_maker_timeout"
    led.close()


def test_async_dual_taker_single_leg_partial_fill_hedges_exact_delta_and_guards_failed_fallback(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "async_partial.sqlite3", plan.fingerprint())
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.28, 100.32)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.30}

    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.28, ask=100.32),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]

    # Scenario A: long partial fill 0.20 + cancel, short partial cancel 0.08 -> fallback only hedges 0.12 delta
    pid, err = ctrl.start_entry(cand, specs=specs, now_s=now_s)
    assert err is None and pid is not None
    coid_l = next(o["client_order_id"] for o in gw.submitted if o["side"] == "BUY")
    coid_s = next(o["client_order_id"] for o in gw.submitted if o["side"] == "SELL")

    gw.deliver_fill(coid_l, filled_base_qty=D("0.20"), avg_fill_price=D("100.02"), partial=True)
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    assert D(led.active_positions()[pid]["long_base_qty"]) == D("0.20")
    assert len(gw.submitted) == 2

    gw.deliver_cancel(coid_l, filled_base_qty=D("0.20"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)
    assert len(gw.submitted) == 2

    gw.deliver_cancel(coid_s, filled_base_qty=D("0.08"), avg_fill_price=D("100.28"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    assert len(gw.submitted) == 3
    fb = gw.submitted[2]
    assert fb["order_type"] == "MARKET" and fb["side"] == "SELL" and fb["base_qty"] == D("0.12")
    fb_row = dict(led.conn.execute("SELECT role, base_qty, status FROM orders WHERE client_order_id=?", (fb["client_order_id"],)).fetchone())
    assert fb_row["role"] == "TAKER_OPEN_MARKET_FALLBACK" and D(fb_row["base_qty"]) == D("0.12") and fb_row["status"] == "OPEN"

    # Cannot submit next slice while fallback order is still in flight
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 4) == []
    gw.deliver_fill(fb["client_order_id"], filled_base_qty=D("0.12"), avg_fill_price=D("100.29"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 5, live_quotes=quotes)
    pos = led.active_positions()[pid]
    assert D(pos["long_base_qty"]) == D("0.20") and D(pos["short_base_qty"]) == D("0.20")
    assert ctrl._active_dual_takers == {}
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 6) == [pid]

    # Scenario B: rejected fallback increments unhedged streak and blocks further slices on imbalanced position
    gw2 = DelayedAsyncVenueGateway()
    gw2.register_spec(spec_l)
    gw2.register_spec(spec_s)
    led2 = CarryLedger(tmp_path / "async_failfb.sqlite3", plan.fingerprint())
    ctrl2 = CarryExecutionController(ledger=led2, gateway=gw2, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    pid2, _ = ctrl2.start_entry(cand, specs=specs, now_s=now_s + 100)
    assert pid2 is not None
    c2_l = next(o["client_order_id"] for o in gw2.submitted if o["side"] == "BUY")
    c2_s = next(o["client_order_id"] for o in gw2.submitted if o["side"] == "SELL")
    gw2.deliver_fill(c2_l, filled_base_qty=D("0.49"), avg_fill_price=D("100.02"))
    gw2.deliver_cancel(c2_s, filled_base_qty=D("0"))
    ctrl2.poll_active_orders(live_mids=mids, now_s=now_s + 101, live_quotes=quotes)
    assert len(gw2.submitted) == 3
    fb2 = gw2.submitted[2]
    gw2.deliver_reject(fb2["client_order_id"], reason="insufficient_margin")
    ctrl2.poll_active_orders(live_mids=mids, now_s=now_s + 102, live_quotes=quotes)
    assert led2.daily_stats(now_s + 102)[1] == 1
    assert ctrl2.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 103) == []
    assert len(gw2.submitted) == 3

    led.close()
    led2.close()


def test_async_in_flight_restart_halts_reconcile_and_blocks_duplicate_resubmission(tmp_path: Path) -> None:
    from src.carry.runtime import _live_exec_engine_config

    assert _live_exec_engine_config("live").reconciliation is True
    assert _live_exec_engine_config("live").reconciliation_lookback_mins == 60
    assert _live_exec_engine_config("live").generate_missing_orders is True
    assert _live_exec_engine_config("readonly").reconciliation is True
    assert _live_exec_engine_config("readonly").generate_missing_orders is True
    assert _live_exec_engine_config("paper").reconciliation is False
    assert _live_exec_engine_config("paper").generate_missing_orders is False

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    db_path = tmp_path / "restart.sqlite3"
    led = CarryLedger(db_path, plan.fingerprint())
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.28, 100.32)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.30}

    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.28, ask=100.32),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]
    pid, _ = ctrl.start_entry(cand, specs=specs, now_s=now_s)
    assert pid is not None
    # Simulate process crash while both IOC orders are in flight in SQLite (and exchange already filled long leg 0.49)
    led.close()

    led_reopened = CarryLedger(db_path, plan.fingerprint())
    res = led_reopened.reconcile_startup(
        venue_positions={("HL", spec_l.instrument_id): D("0.49")},
        venue_open_orders={},
        ts_s=now_s + 10,
    )
    assert not res.ok and res.halted
    assert led_reopened.is_halted()[0]
    assert any(d.startswith(f"in_flight_position:{pid}:OPENING") for d in res.discrepancies)
    assert sum(1 for d in res.discrepancies if d.startswith("in_flight_order:") and ":TAKER_OPEN:OPEN" in d) == 2
    assert any(d.startswith(f"position_mismatch:HL:{spec_l.instrument_id}") for d in res.discrepancies)

    # Fresh controller with empty in-memory maps must also block advance_pending_slices via SQLite open_orders
    gw_restarted = DelayedAsyncVenueGateway()
    gw_restarted.register_spec(spec_l)
    gw_restarted.register_spec(spec_s)
    ctrl_restarted = CarryExecutionController(ledger=led_reopened, gateway=gw_restarted, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    assert ctrl_restarted.has_in_flight_orders(pid)
    assert ctrl_restarted.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 11) == []
    assert gw_restarted.submitted == []
    led_reopened.close()


def test_async_maker_hedge_cancel_race_emergency_close_and_strategy_callbacks(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from nautilus_trader.model import ClientOrderId, InstrumentId, OrderSide, Price, Quantity, StrategyId, TraderId
    from nautilus_trader.trading import Strategy
    from src.carry.runtime import _NautilusStrategyGateway, _create_carry_strategy

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "async_maker.sqlite3", plan.fingerprint())
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.15}

    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]
    pid, err = ctrl.start_entry(cand, specs=specs, now_s=now_s)
    assert err is None and pid is not None
    mk1 = gw.submitted[0]["client_order_id"]

    # Maker fills asynchronously -> HEDGE_OPEN IOC returns (OPEN, 0) -> MUST NOT fire market fallback prematurely
    gw.deliver_fill(mk1, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    assert len(gw.submitted) == 2
    hg1 = gw.submitted[1]
    assert hg1["order_type"] == "IOC"
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 1) == []

    # HEDGE_OPEN IOC fills -> balanced 0.49/0.49 with only 1 hedge order
    gw.deliver_fill(hg1["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=D("100.13"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)
    assert len(gw.submitted) == 2
    pos = led.active_positions()[pid]
    assert D(pos["long_base_qty"]) == D("0.49") and D(pos["short_base_qty"]) == D("0.49")

    # Slice 2: Maker fills, HEDGE_OPEN IOC gets partial 0.10 + CANCELED -> fires ONE fallback for 0.39
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 3) == [pid]
    mk2 = gw.submitted[2]["client_order_id"]
    gw.deliver_fill(mk2, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 4, live_quotes=quotes)
    hg2 = gw.submitted[3]["client_order_id"]
    gw.deliver_cancel(hg2, filled_base_qty=D("0.10"), avg_fill_price=D("100.13"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 5, live_quotes=quotes)
    assert len(gw.submitted) == 5
    hm2 = gw.submitted[4]
    assert hm2["order_type"] == "MARKET" and hm2["base_qty"] == D("0.39")
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 5) == []
    gw.deliver_fill(hm2["client_order_id"], filled_base_qty=D("0.39"), avg_fill_price=D("100.14"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 6, live_quotes=quotes)
    pos = led.active_positions()[pid]
    assert pos["status"] == "OPEN" and D(pos["long_base_qty"]) == D("0.98") and D(pos["short_base_qty"]) == D("0.98")

    # Async emergency_close_position: stays CLOSING while MARKET orders are OPEN, deduplicates, then settles PnL on fill
    ctrl.emergency_close_position(pid, specs=specs, reason="basis_stop_30m", now_s=now_s + 10, live_mids=mids)
    assert len(gw.submitted) == 7
    assert led.active_positions()[pid]["status"] == "CLOSING"
    ctrl.emergency_close_position(pid, specs=specs, reason="basis_stop_30m", now_s=now_s + 11, live_mids=mids)
    assert len(gw.submitted) == 7
    em_l = next(o["client_order_id"] for o in gw.submitted[5:] if o["side"] == "SELL")
    em_s = next(o["client_order_id"] for o in gw.submitted[5:] if o["side"] == "BUY")
    gw.deliver_fill(em_l, filled_base_qty=D("0.98"), avg_fill_price=D("100.00"))
    gw.deliver_fill(em_s, filled_base_qty=D("0.98"), avg_fill_price=D("101.00"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 12, live_quotes=quotes)
    assert pid not in led.active_positions()
    closed_row = dict(led.conn.execute("SELECT status, realized_pnl_usd FROM positions WHERE position_id=?", (pid,)).fetchone())
    assert closed_row["status"] == "CLOSED"
    assert D(closed_row["realized_pnl_usd"]) < D("0")

    # Maker timeout cancel race: keeps order tracked while cancel pending, blocks duplicates, hedges racing fill
    gw_race = DelayedAsyncVenueGateway()
    gw_race.register_spec(spec_l)
    gw_race.register_spec(spec_s)
    rt_race = plan.runtime.__class__(**{**plan.runtime.__dict__, "maker_requote_secs": 3})
    ctrl_race = CarryExecutionController(ledger=led, gateway=gw_race, risk=plan.risk, runtime=rt_race, policy=plan.policy)
    pid_race, _ = ctrl_race.start_entry(cand, specs=specs, now_s=now_s + 50)
    assert pid_race is not None
    mk_race = gw_race.submitted[0]["client_order_id"]
    ctrl_race.poll_active_orders(live_mids=mids, now_s=now_s + 54, live_quotes=quotes)
    assert gw_race.cancel_requests == [mk_race]
    assert mk_race in ctrl_race._active_makers and ctrl_race._active_makers[mk_race].cancel_requested
    assert ctrl_race.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 54) == []
    # Polling again before cancel confirms does not spam cancel_order
    ctrl_race.poll_active_orders(live_mids=mids, now_s=now_s + 55, live_quotes=quotes)
    assert gw_race.cancel_requests == [mk_race]
    # Cancel rejected clears flag, then racing partial fill + cancel hedges 0.20 and frees maker slot
    ctrl_race.on_cancel_rejected(mk_race)
    assert not ctrl_race._active_makers[mk_race].cancel_requested
    gw_race.deliver_cancel(mk_race, filled_base_qty=D("0.20"), avg_fill_price=D("99.98"))
    ctrl_race.poll_active_orders(live_mids=mids, now_s=now_s + 56, live_quotes=quotes)
    assert mk_race not in ctrl_race._active_makers
    assert len(gw_race.submitted) == 2 and gw_race.submitted[1]["order_type"] == "IOC"
    assert gw_race.submitted[1]["base_qty"] == D("0.20")

    # Strategy order lifecycle callbacks + _NautilusStrategyGateway.poll_order avg_px
    strat = _create_carry_strategy(plan, led, mode="paper")
    for hook in (
        "on_order_filled",
        "on_order_canceled",
        "on_order_expired",
        "on_order_rejected",
        "on_order_denied",
        "on_order_cancel_rejected",
    ):
        assert getattr(type(strat), hook) is not getattr(Strategy, hook)
    assert type(strat).on_order_event is Strategy.on_order_event

    gw_strat = DelayedAsyncVenueGateway()
    gw_strat.register_spec(spec_l)
    gw_strat.register_spec(spec_s)
    strat.gateway = gw_strat
    strat.controller.gateway = gw_strat
    strat.reconciled = True
    strat.live_mids = mids
    strat.live_quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.28, 100.32)}
    snap_wide = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.28, ask=100.32),
        }
    }
    cand_wide = evaluate_entry_candidates(
        snap_wide, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s + 100
    )[0]
    pid_s, _ = strat.controller.start_entry(cand_wide, specs=specs, now_s=now_s + 100)
    assert pid_s is not None
    tk_l = next(o["client_order_id"] for o in gw_strat.submitted if o["side"] == "BUY")
    tk_s = next(o["client_order_id"] for o in gw_strat.submitted if o["side"] == "SELL")
    gw_strat.deliver_fill(tk_l, filled_base_qty=D("0.49"), avg_fill_price=D("100.02"))
    strat.on_order_filled(SimpleNamespace(client_order_id=tk_l))
    assert D(led.active_positions()[pid_s]["long_base_qty"]) == D("0.49")
    gw_strat.deliver_cancel(tk_s, filled_base_qty=D("0.49"), avg_fill_price=D("100.28"))
    strat.on_order_canceled(SimpleNamespace(client_order_id=tk_s))
    assert D(led.active_positions()[pid_s]["short_base_qty"]) == D("0.49")

    # _NautilusStrategyGateway.poll_order extracts avg_px and handles CANCELLED spelling
    fake_order = SimpleNamespace(
        instrument_id=InstrumentId.from_str(spec_s.instrument_id),
        filled_qty=Quantity.from_str("5.0"),
        avg_px=100.25,
        status="FILLED",
        venue_order_id="V-99",
    )
    fake_inst = SimpleNamespace(multiplier=D("0.1"))
    fake_strat = SimpleNamespace(
        cache=SimpleNamespace(
            order=lambda _coid: fake_order,
            instrument=lambda _iid: fake_inst,
        )
    )
    nt_gw = _NautilusStrategyGateway(fake_strat)
    nt_ack = nt_gw.poll_order(client_order_id="tk-1", venue="OKX", instrument_id=spec_s.instrument_id)
    assert nt_ack.status == "FILLED"
    assert nt_ack.venue_order_id == "V-99"
    assert nt_ack.filled_base_qty == D("0.50")
    assert nt_ack.avg_fill_price == D("100.25")
    led.close()


def test_funding_history_limit_30_and_latest_interval_inference() -> None:
    now_s = 1_800_000_000
    # Dynamic shift 4h -> 2h: chronological intervals [4, 4, 2, 2, 2] must infer iv=2, not max=4
    shifted_events = [
        (now_s - 14 * HOUR, -10.0),
        (now_s - 10 * HOUR, -10.0),
        (now_s - 6 * HOUR, -10.0),
        (now_s - 4 * HOUR, -10.0),
        (now_s - 2 * HOUR, -10.0),
        (now_s, -10.0),
    ]
    r8, r24, iv = compute_trailing_rates(shifted_events, now_s, 8)
    assert iv == 2
    assert r8 == pytest.approx(-40.0 / 8.0)
    assert r24 == pytest.approx(-60.0 / 24.0)

    seen_urls: list[str] = []

    def fake_http(url: str, _body: dict | None = None):
        seen_urls.append(url)
        if "public/funding-rate?" in url:
            return {
                "data": [
                    {
                        "fundingRate": "-0.0020",
                        "fundingTime": str((now_s + 2 * HOUR) * 1000),
                        "nextFundingTime": str((now_s + 4 * HOUR) * 1000),
                    }
                ]
            }
        if "funding-rate-history" in url:
            return {
                "data": [
                    {"fundingTime": str((now_s - h * HOUR) * 1000), "realizedRate": "-0.001"}
                    for h in range(24)
                ]
            }
        if "market/candles" in url:
            return {
                "data": [
                    [str((now_s - 7200) * 1000), "1", "1", "1", "10.0", "100", "0", "10000"],
                    [str((now_s - 3600) * 1000), "1", "1", "1", "10.1", "100", "0", "10000"],
                ]
            }
        if "funding/history" in url:
            return {
                "result": {
                    "list": [
                        {"fundingRateTimestamp": str((now_s - h * HOUR) * 1000), "fundingRate": "-0.001"}
                        for h in range(24)
                    ]
                }
            }
        if "market/kline" in url:
            return {
                "result": {
                    "list": [
                        [str((now_s - 7200) * 1000), "1", "1", "1", "10.0", "100", "10000"],
                        [str((now_s - 3600) * 1000), "1", "1", "1", "10.1", "100", "10000"],
                    ]
                }
            }
        return {}

    feed = PublicCarryFeed(venues=("OKX", "BYBIT"), http_get=fake_http)
    okx_leg = LegMarketInfo(**{**_make_leg("OKX", "ME", interval_h=8).__dict__, "current_rate_bps_h": None})
    okx_out = feed._fetch_leg_detail(okx_leg, now_s)
    assert okx_out.interval_h == 2
    assert okx_out.r8_bps_h == pytest.approx(-10.0)
    assert okx_out.r24_bps_h == pytest.approx(-10.0)

    byb_leg = _make_leg("BYBIT", "MOVE", interval_h=1)
    byb_out = feed._fetch_leg_detail(byb_leg, now_s)
    assert byb_out.interval_h == 1
    assert byb_out.r8_bps_h == pytest.approx(-10.0)
    assert byb_out.r24_bps_h == pytest.approx(-10.0)

    hist_urls = [u for u in seen_urls if "funding-rate-history" in u or "funding/history" in u]
    assert len(hist_urls) == 2
    assert all("limit=30" in u for u in hist_urls)


def test_closing_absorbs_dust_tail_and_falls_back_to_dual_taker_after_maker_timeout(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    led = CarryLedger(tmp_path / "close_fix.sqlite3", plan.fingerprint())

    # Part A: 2487 @ $0.0416 (~$103.46) closes cleanly in 2 Maker+IOC slices without leaving a $3.31 dust tail
    gw_a = PaperVenueGateway(fill_maker_immediately=True)
    spec_l_me = InstrumentSpec("ME-USD-PERP.HYPERLIQUID", "HL", size_increment=D("1"), price_increment=D("0.00001"))
    spec_s_me = InstrumentSpec("ME-USDT-SWAP.OKX", "OKX", size_increment=D("1"), price_increment=D("0.00001"))
    gw_a.register_spec(spec_l_me, D("0.04010"))
    gw_a.register_spec(spec_s_me, D("0.04020"))
    ctrl_a = CarryExecutionController(ledger=led, gateway=gw_a, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    now_s = 1_800_000_000
    led.create_position(
        position_id="ME-TAIL-1",
        symbol="ME",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l_me.instrument_id,
        iid_short=spec_s_me.instrument_id,
        opened_ts=now_s,
        entry_l=0.04010,
        entry_s=0.04020,
        entry_basis_bps=24.9,
        target_base_qty=D("2487"),
    )
    led.update_position_status("ME-TAIL-1", status="OPEN", ts_s=now_s + 1, long_base_qty=D("2487"), short_base_qty=D("2487"))
    gw_a.register_spec(spec_l_me, D("0.04160"))
    gw_a.register_spec(spec_s_me, D("0.04160"))
    specs_me = {spec_l_me.instrument_id: spec_l_me, spec_s_me.instrument_id: spec_s_me}
    quotes_me = {spec_l_me.instrument_id: (0.04159, 0.04161), spec_s_me.instrument_id: (0.04159, 0.04161)}
    mids_me = {spec_l_me.instrument_id: 0.04160, spec_s_me.instrument_id: 0.04160}
    ctrl_a.start_sliced_close("ME-TAIL-1", specs=specs_me, quotes=quotes_me, mids=mids_me, reason="carry_decay", now_s=now_s + 10)
    ctrl_a.advance_pending_slices(specs=specs_me, quotes=quotes_me, mids=mids_me, now_s=now_s + 11)
    assert "ME-TAIL-1" not in led.active_positions()
    mk_close_rows = led.conn.execute("SELECT base_qty FROM orders WHERE position_id='ME-TAIL-1' AND role='MAKER_CLOSE'").fetchall()
    assert len(mk_close_rows) == 2
    assert all(D(r["base_qty"]) >= D("1200") for r in mk_close_rows)

    spec_l_min = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"), min_notional_usd=D("15"))
    spec_s_min = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"), min_notional_usd=D("15"))
    split_usd = ctrl_a._closing_slice_target_usd(rem_usd=D("62"), spec_l=spec_l_min, spec_s=spec_s_min, mid=D("100"))
    assert split_usd == D("31")

    # Part B: CLOSING maker timeout -> Dual-Taker IOC fallback + repeated start_sliced_close does not reset attempts
    gw_b = PaperVenueGateway(fill_maker_immediately=False, use_realistic_bbo=True)
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw_b.register_spec(spec_l, D("100.00"))
    gw_b.register_spec(spec_s, D("100.20"))
    gw_b.update_quote(spec_l.instrument_id, D("99.99"), D("100.01"))
    gw_b.update_quote(spec_s.instrument_id, D("100.19"), D("100.21"))
    rt_fast = plan.runtime.__class__(**{**plan.runtime.__dict__, "maker_requote_secs": 3, "maker_max_attempts": 2})
    ctrl_b = CarryExecutionController(ledger=led, gateway=gw_b, risk=plan.risk, runtime=rt_fast, policy=plan.policy)

    led.create_position(
        position_id="SOL-CLOSE-FB",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s + 100,
        entry_l=100.00,
        entry_s=100.20,
        entry_basis_bps=20.0,
        target_base_qty=D("0.50"),
    )
    led.update_position_status("SOL-CLOSE-FB", status="OPEN", ts_s=now_s + 101, long_base_qty=D("0.50"), short_base_qty=D("0.50"))
    gw_b.update_quote(spec_l.instrument_id, D("99.99"), D("100.01"))
    gw_b.update_quote(spec_s.instrument_id, D("99.99"), D("100.01"))
    specs_sol = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes_sol = {spec_l.instrument_id: (99.99, 100.01), spec_s.instrument_id: (99.99, 100.01)}
    mids_sol = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.00}

    ctrl_b.start_sliced_close("SOL-CLOSE-FB", specs=specs_sol, quotes=quotes_sol, mids=mids_sol, reason="carry_decay", now_s=now_s + 200)
    ctrl_b.poll_active_orders(live_mids=mids_sol, now_s=now_s + 204, live_quotes=quotes_sol)
    ctrl_b.start_sliced_close("SOL-CLOSE-FB", specs=specs_sol, quotes=quotes_sol, mids=mids_sol, reason="carry_decay", now_s=now_s + 204)
    ctrl_b.poll_active_orders(live_mids=mids_sol, now_s=now_s + 208, live_quotes=quotes_sol)
    assert ctrl_b._slice_attempts.get("SOL-CLOSE-FB") == 2

    ctrl_b.advance_pending_slices(specs=specs_sol, quotes=quotes_sol, mids=mids_sol, now_s=now_s + 208)
    row_b = dict(led.conn.execute("SELECT status, realized_pnl_usd, close_reason FROM positions WHERE position_id='SOL-CLOSE-FB'").fetchone())
    assert row_b["status"] == "CLOSED" and row_b["close_reason"] == "carry_decay"
    assert D(row_b["realized_pnl_usd"]) == D("0.0900")
    tc_rows = led.conn.execute("SELECT role, order_type, status FROM orders WHERE position_id='SOL-CLOSE-FB' AND role='TAKER_CLOSE'").fetchall()
    assert len(tc_rows) == 2 and all(r["order_type"] == "IOC" and r["status"] == "FILLED" for r in tc_rows)

    # Part C: Unalignable dust remainder below min_notional_usd ($3 < $10) closes via Dual-Taker fallback
    gw_c = PaperVenueGateway(fill_maker_immediately=False, use_realistic_bbo=True)
    spec_l_d = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"), min_notional_usd=D("10"))
    spec_s_d = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"), min_notional_usd=D("10"))
    gw_c.register_spec(spec_l_d, D("100.00"))
    gw_c.register_spec(spec_s_d, D("100.00"))
    gw_c.update_quote(spec_l_d.instrument_id, D("99.99"), D("100.01"))
    gw_c.update_quote(spec_s_d.instrument_id, D("99.99"), D("100.01"))
    ctrl_c = CarryExecutionController(ledger=led, gateway=gw_c, risk=plan.risk, runtime=rt_fast, policy=plan.policy)
    led.create_position(
        position_id="SOL-DUST-3",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l_d.instrument_id,
        iid_short=spec_s_d.instrument_id,
        opened_ts=now_s + 300,
        entry_l=100.00,
        entry_s=100.00,
        entry_basis_bps=0.0,
        target_base_qty=D("0.03"),
    )
    led.update_position_status("SOL-DUST-3", status="OPEN", ts_s=now_s + 301, long_base_qty=D("0.03"), short_base_qty=D("0.03"))
    ctrl_c.start_sliced_close(
        "SOL-DUST-3",
        specs={spec_l_d.instrument_id: spec_l_d, spec_s_d.instrument_id: spec_s_d},
        quotes=quotes_sol,
        mids=mids_sol,
        reason="carry_decay",
        now_s=now_s + 302,
    )
    row_c = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id='SOL-DUST-3'").fetchone())
    assert row_c["status"] == "CLOSED" and row_c["close_reason"] == "carry_decay"

    # Part D: Async gateway TAKER_CLOSE IOC blocks duplicates and triggers reduce-only TAKER_CLOSE_MARKET_FALLBACK
    gw_d = DelayedAsyncVenueGateway()
    gw_d.register_spec(spec_l)
    gw_d.register_spec(spec_s)
    ctrl_d = CarryExecutionController(ledger=led, gateway=gw_d, risk=plan.risk, runtime=rt_fast, policy=plan.policy)
    led.create_position(
        position_id="SOL-ASYNC-CLOSE",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s + 400,
        entry_l=100.00,
        entry_s=100.20,
        entry_basis_bps=20.0,
        target_base_qty=D("0.50"),
    )
    led.update_position_status("SOL-ASYNC-CLOSE", status="CLOSING", ts_s=now_s + 401, long_base_qty=D("0.50"), short_base_qty=D("0.50"), close_reason="carry_decay")
    ctrl_d._slice_attempts["SOL-ASYNC-CLOSE"] = 2

    assert ctrl_d.advance_pending_slices(specs=specs_sol, quotes=quotes_sol, mids=mids_sol, now_s=now_s + 402) == ["SOL-ASYNC-CLOSE"]
    assert len(gw_d.submitted) == 2
    assert ctrl_d.has_in_flight_orders("SOL-ASYNC-CLOSE")
    assert ctrl_d.advance_pending_slices(specs=specs_sol, quotes=quotes_sol, mids=mids_sol, now_s=now_s + 403) == []
    assert len(gw_d.submitted) == 2

    tc_l = next(o["client_order_id"] for o in gw_d.submitted if o["side"] == "SELL")
    tc_s = next(o["client_order_id"] for o in gw_d.submitted if o["side"] == "BUY")
    gw_d.deliver_fill(tc_l, filled_base_qty=D("0.50"), avg_fill_price=D("99.99"))
    gw_d.deliver_cancel(tc_s, filled_base_qty=D("0"))
    ctrl_d.poll_active_orders(live_mids=mids_sol, now_s=now_s + 404, live_quotes=quotes_sol)

    # Short leg fallback MARKET reduce-only order must have been submitted and remain in-flight until filled
    tmc_orders = [o for o in gw_d.submitted if o["order_type"] == "MARKET"]
    assert len(tmc_orders) == 1 and tmc_orders[0]["side"] == "BUY" and tmc_orders[0]["reduce_only"] is True
    assert ctrl_d.has_in_flight_orders("SOL-ASYNC-CLOSE")
    assert led.active_positions()["SOL-ASYNC-CLOSE"]["status"] == "CLOSING"

    gw_d.deliver_fill(tmc_orders[0]["client_order_id"], filled_base_qty=D("0.50"), avg_fill_price=D("100.01"))
    ctrl_d.poll_active_orders(live_mids=mids_sol, now_s=now_s + 405, live_quotes=quotes_sol)
    assert "SOL-ASYNC-CLOSE" not in led.active_positions()
    row_d = dict(led.conn.execute("SELECT status, realized_pnl_usd, close_reason FROM positions WHERE position_id='SOL-ASYNC-CLOSE'").fetchone())
    assert row_d["status"] == "CLOSED" and row_d["close_reason"] == "carry_decay"
    assert D(row_d["realized_pnl_usd"]) == D("0.0900")
    led.close()


def test_nautilus_gateway_cancel_order_uses_client_order_id_type() -> None:
    import inspect
    from types import SimpleNamespace
    from nautilus_trader.model import ClientOrderId, InstrumentId, Quantity
    from nautilus_trader.trading import Strategy, StrategyConfig
    from src.carry.runtime import _NautilusStrategyGateway

    sig = inspect.signature(Strategy.cancel_order)
    assert "client_order_id" in sig.parameters

    real_strat = Strategy(StrategyConfig())
    with pytest.raises(TypeError):
        real_strat.cancel_order(object())
    with pytest.raises(RuntimeError):
        real_strat.cancel_order(ClientOrderId("mkabc123"))

    iid = InstrumentId.from_str("SOL-USDT-SWAP.OKX")
    canceled_args: list[object] = []
    cache_orders: dict[str, object] = {}

    open_order = SimpleNamespace(
        instrument_id=iid,
        filled_qty=Quantity.from_str("0.0"),
        avg_px=None,
        status="ACCEPTED",
        venue_order_id="V-1",
        is_closed=False,
    )
    closed_order = SimpleNamespace(
        instrument_id=iid,
        filled_qty=Quantity.from_str("1.0"),
        avg_px=100.0,
        status="FILLED",
        venue_order_id="V-1",
        is_closed=True,
    )
    fake_strat = SimpleNamespace(
        cache=SimpleNamespace(
            order=lambda coid: cache_orders.get(str(coid)),
            instrument=lambda _iid: SimpleNamespace(multiplier=D("0.1")),
        ),
        cancel_order=lambda arg: canceled_args.append(arg),
    )
    gw = _NautilusStrategyGateway(fake_strat)

    # 1) Cache open order -> cancel_order called with real ClientOrderId
    cache_orders["mkabc123"] = open_order
    gw._orders["mkabc123"] = open_order
    ack1 = gw.cancel_order(client_order_id="mkabc123", venue="OKX", instrument_id=str(iid))
    assert ack1.status == "OPEN"
    assert len(canceled_args) == 1
    assert isinstance(canceled_args[0], ClientOrderId)
    assert str(canceled_args[0]) == "mkabc123"

    # 2) Cache says closed even if _orders has stale open snapshot -> must NOT cancel
    cache_orders["mkabc123"] = closed_order
    ack2 = gw.cancel_order(client_order_id="mkabc123", venue="OKX", instrument_id=str(iid))
    assert ack2.status == "FILLED"
    assert len(canceled_args) == 1

    # 3) Cache returns None -> falls back to _orders open snapshot and passes ClientOrderId
    gw._orders["mkfb123"] = open_order
    gw.cancel_order(client_order_id="mkfb123", venue="OKX", instrument_id=str(iid))
    assert len(canceled_args) == 2
    assert isinstance(canceled_args[1], ClientOrderId)
    assert str(canceled_args[1]) == "mkfb123"


def test_all_order_roles_generate_okx_compatible_client_order_ids(tmp_path: Path) -> None:
    from src.carry.executor import make_client_order_id

    for pfx, pid, rlen in [
        ("em-l", "xyz:NVDA-1800000000-ab_cd", 8),
        ("", "---___:::", 8),
        ("verylongprefix_with_hyphens_123456", "pos-with-dashes-0123456789", 16),
    ]:
        coid = make_client_order_id(pfx, pid, rand_len=rlen)
        assert 1 <= len(coid) <= 32
        assert coid.isascii() and coid.isalnum()

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "okx_ids.sqlite3", plan.fingerprint())
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.15}
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    now_s = 1_800_000_000

    # 1) MAKER_OPEN -> HEDGE_OPEN -> HEDGE_OPEN_MARKET_FALLBACK -> MAKER_CLOSE -> HEDGE_CLOSE
    snap_mk = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand_mk = evaluate_entry_candidates(
        snap_mk, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]
    pid_mk, _ = ctrl.start_entry(cand_mk, specs=specs, now_s=now_s)
    assert pid_mk is not None
    mk1 = gw.submitted[-1]["client_order_id"]
    gw.deliver_fill(mk1, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    hg1 = gw.submitted[-1]["client_order_id"]
    gw.deliver_cancel(hg1, filled_base_qty=D("0.10"), avg_fill_price=D("100.13"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)
    hm1 = gw.submitted[-1]["client_order_id"]
    gw.deliver_fill(hm1, filled_base_qty=D("0.39"), avg_fill_price=D("100.14"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    led.update_position_status(pid_mk, status="OPEN", ts_s=now_s + 4)

    ctrl.start_sliced_close(pid_mk, specs=specs, quotes=quotes, mids=mids, reason="carry_decay", now_s=now_s + 5)
    mk_close = gw.submitted[-1]["client_order_id"]
    gw.deliver_fill(mk_close, filled_base_qty=D("0.49"), avg_fill_price=D("100.02"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 6, live_quotes=quotes)
    hg_close = gw.submitted[-1]["client_order_id"]
    gw.deliver_fill(hg_close, filled_base_qty=D("0.49"), avg_fill_price=D("100.13"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 7, live_quotes=quotes)

    # 2) TAKER_OPEN -> TAKER_OPEN_MARKET_FALLBACK -> TAKER_CLOSE -> TAKER_CLOSE_MARKET_FALLBACK
    snap_tk = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.28, ask=100.32),
        }
    }
    cand_tk = evaluate_entry_candidates(
        snap_tk, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s + 10
    )[0]
    pid_tk, _ = ctrl.start_entry(cand_tk, specs=specs, now_s=now_s + 10)
    assert pid_tk is not None
    tk_l, tk_s = gw.submitted[-2]["client_order_id"], gw.submitted[-1]["client_order_id"]
    gw.deliver_fill(tk_l, filled_base_qty=D("0.49"), avg_fill_price=D("100.02"))
    gw.deliver_cancel(tk_s, filled_base_qty=D("0"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 11, live_quotes=quotes)
    tm_s = gw.submitted[-1]["client_order_id"]
    gw.deliver_fill(tm_s, filled_base_qty=D("0.49"), avg_fill_price=D("100.28"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 12, live_quotes=quotes)
    led.update_position_status(pid_tk, status="CLOSING", ts_s=now_s + 13, close_reason="carry_decay")
    ctrl._slice_attempts[pid_tk] = plan.runtime.maker_max_attempts
    ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 14)
    tc_l, tc_s = gw.submitted[-2]["client_order_id"], gw.submitted[-1]["client_order_id"]
    gw.deliver_fill(tc_l, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    gw.deliver_cancel(tc_s, filled_base_qty=D("0"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 15, live_quotes=quotes)
    tmc_s = gw.submitted[-1]["client_order_id"]
    gw.deliver_fill(tmc_s, filled_base_qty=D("0.49"), avg_fill_price=D("100.17"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 16, live_quotes=quotes)

    # 3) EMERGENCY_CLOSE_LONG & EMERGENCY_CLOSE_SHORT with colon/hyphen/underscore position_id
    pid_em = "xyz:NVDA-1800000000-ab_cd"
    led.create_position(
        position_id=pid_em,
        symbol="NVDA",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s + 20,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.49"),
    )
    led.update_position_status(pid_em, status="OPEN", ts_s=now_s + 21, long_base_qty=D("0.49"), short_base_qty=D("0.49"))
    ctrl.emergency_close_position(pid_em, specs=specs, reason="basis_stop_30m", now_s=now_s + 22, live_mids=mids)

    rows = led.conn.execute("SELECT client_order_id, role FROM orders").fetchall()
    roles_seen = {r["role"] for r in rows}
    expected_roles = {
        "MAKER_OPEN",
        "HEDGE_OPEN",
        "HEDGE_OPEN_MARKET_FALLBACK",
        "MAKER_CLOSE",
        "HEDGE_CLOSE",
        "TAKER_OPEN",
        "TAKER_OPEN_MARKET_FALLBACK",
        "TAKER_CLOSE",
        "TAKER_CLOSE_MARKET_FALLBACK",
        "EMERGENCY_CLOSE_LONG",
        "EMERGENCY_CLOSE_SHORT",
    }
    assert expected_roles <= roles_seen
    for r in rows:
        coid = str(r["client_order_id"])
        assert 1 <= len(coid) <= 32, f"invalid len for {r['role']}: {coid}"
        assert coid.isascii() and coid.isalnum(), f"non-alnum for {r['role']}: {coid}"
    led.close()


def test_maker_partial_fills_aggregate_without_hedge_amplification_and_handle_cancel_tails(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    spec_m = InstrumentSpec("FOO-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_h = InstrumentSpec("FOO-USDT-SWAP.OKX", "OKX", size_increment=D("1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_m.instrument_id: spec_m, spec_h.instrument_id: spec_h}
    quotes = {spec_m.instrument_id: (99.98, 100.02), spec_h.instrument_id: (100.13, 100.17)}
    mids = {spec_m.instrument_id: 100.00, spec_h.instrument_id: 100.15}
    now_s = 1_800_000_000
    snap = {
        "FOO": {
            "HL": _make_leg("HL", "FOO", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "FOO", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]

    # Case A: 10 consecutive 0.01 maker fills with hedge step=0.10 must hedge strictly 0.10 total (no 10x amplification)
    led_a = CarryLedger(tmp_path / "case_a.sqlite3", plan.fingerprint())
    gw_a = DelayedAsyncVenueGateway()
    gw_a.register_spec(spec_m)
    gw_a.register_spec(spec_h)
    ctrl_a = CarryExecutionController(ledger=led_a, gateway=gw_a, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    pid_a, err_a = ctrl_a.start_entry(cand, specs=specs, now_s=now_s)
    assert err_a is None and pid_a is not None
    mk_a = gw_a.submitted[0]["client_order_id"]

    for i in range(1, 10):
        gw_a.deliver_fill(mk_a, filled_base_qty=D("0.01") * i, avg_fill_price=D("99.98"), partial=True)
        ctrl_a.poll_active_orders(live_mids=mids, now_s=now_s + i, live_quotes=quotes)
        assert len(gw_a.submitted) == 1
        assert D(led_a.active_positions()[pid_a]["long_base_qty"]) == D("0.01") * i
        assert D(led_a.active_positions()[pid_a]["short_base_qty"]) == D("0")

    gw_a.deliver_fill(mk_a, filled_base_qty=D("0.10"), avg_fill_price=D("99.98"), partial=True)
    ctrl_a.poll_active_orders(live_mids=mids, now_s=now_s + 10, live_quotes=quotes)
    assert len(gw_a.submitted) == 2
    hg_a = gw_a.submitted[1]
    assert hg_a["base_qty"] == D("0.10")
    gw_a.deliver_fill(hg_a["client_order_id"], filled_base_qty=D("0.10"), avg_fill_price=D("100.13"))
    ctrl_a.poll_active_orders(live_mids=mids, now_s=now_s + 11, live_quotes=quotes)
    pos_a = led_a.active_positions()[pid_a]
    assert D(pos_a["long_base_qty"]) == D("0.10") and D(pos_a["short_base_qty"]) == D("0.10")
    led_a.close()

    # Case B: 0.04 partial fill then canceled -> 0 hedge orders submitted (0.04 < 0.05)
    led_b = CarryLedger(tmp_path / "case_b.sqlite3", plan.fingerprint())
    gw_b = DelayedAsyncVenueGateway()
    gw_b.register_spec(spec_m)
    gw_b.register_spec(spec_h)
    ctrl_b = CarryExecutionController(ledger=led_b, gateway=gw_b, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    pid_b, _ = ctrl_b.start_entry(cand, specs=specs, now_s=now_s)
    mk_b = gw_b.submitted[0]["client_order_id"]
    gw_b.deliver_cancel(mk_b, filled_base_qty=D("0.04"), avg_fill_price=D("99.98"))
    ctrl_b.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    assert len(gw_b.submitted) == 1
    pos_b = led_b.active_positions()[pid_b]
    assert D(pos_b["long_base_qty"]) == D("0.04") and D(pos_b["short_base_qty"]) == D("0")
    led_b.close()

    # Case C: 0.06 partial fill buffered, then canceled with no new fill -> flushes 1 step (0.10) shrinking imbalance
    led_c = CarryLedger(tmp_path / "case_c.sqlite3", plan.fingerprint())
    gw_c = DelayedAsyncVenueGateway()
    gw_c.register_spec(spec_m)
    gw_c.register_spec(spec_h)
    ctrl_c = CarryExecutionController(ledger=led_c, gateway=gw_c, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    pid_c, _ = ctrl_c.start_entry(cand, specs=specs, now_s=now_s)
    mk_c = gw_c.submitted[0]["client_order_id"]
    gw_c.deliver_fill(mk_c, filled_base_qty=D("0.06"), avg_fill_price=D("99.98"), partial=True)
    ctrl_c.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    assert len(gw_c.submitted) == 1
    gw_c.deliver_cancel(mk_c, filled_base_qty=D("0.06"), avg_fill_price=D("99.98"))
    ctrl_c.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)
    assert len(gw_c.submitted) == 2 and gw_c.submitted[1]["base_qty"] == D("0.10")
    gw_c.deliver_fill(gw_c.submitted[1]["client_order_id"], filled_base_qty=D("0.10"), avg_fill_price=D("100.13"))
    ctrl_c.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    pos_c = led_c.active_positions()[pid_c]
    assert D(pos_c["long_base_qty"]) == D("0.06") and D(pos_c["short_base_qty"]) == D("0.10")
    led_c.close()

    # Case D: 0.14 partial fill (0.10 hedged + 0.04 buffered), then canceled -> no second hedge
    led_d = CarryLedger(tmp_path / "case_d.sqlite3", plan.fingerprint())
    gw_d = DelayedAsyncVenueGateway()
    gw_d.register_spec(spec_m)
    gw_d.register_spec(spec_h)
    ctrl_d = CarryExecutionController(ledger=led_d, gateway=gw_d, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    pid_d, _ = ctrl_d.start_entry(cand, specs=specs, now_s=now_s)
    mk_d = gw_d.submitted[0]["client_order_id"]
    gw_d.deliver_fill(mk_d, filled_base_qty=D("0.14"), avg_fill_price=D("99.98"), partial=True)
    ctrl_d.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    assert len(gw_d.submitted) == 2 and gw_d.submitted[1]["base_qty"] == D("0.10")
    gw_d.deliver_fill(gw_d.submitted[1]["client_order_id"], filled_base_qty=D("0.10"), avg_fill_price=D("100.13"))
    ctrl_d.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)
    gw_d.deliver_cancel(mk_d, filled_base_qty=D("0.14"), avg_fill_price=D("99.98"))
    ctrl_d.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    assert len(gw_d.submitted) == 2
    pos_d = led_d.active_positions()[pid_d]
    assert D(pos_d["long_base_qty"]) == D("0.14") and D(pos_d["short_base_qty"]) == D("0.10")
    led_d.close()

    # Case E: rejected hedge + fallback contributes 0 settled hedge, so subsequent fill recovers full 0.20 unhedged
    led_e = CarryLedger(tmp_path / "case_e.sqlite3", plan.fingerprint())
    gw_e = DelayedAsyncVenueGateway()
    gw_e.register_spec(spec_m)
    gw_e.register_spec(spec_h)
    ctrl_e = CarryExecutionController(ledger=led_e, gateway=gw_e, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    pid_e, _ = ctrl_e.start_entry(cand, specs=specs, now_s=now_s)
    mk_e = gw_e.submitted[0]["client_order_id"]
    gw_e.deliver_fill(mk_e, filled_base_qty=D("0.10"), avg_fill_price=D("99.98"), partial=True)
    ctrl_e.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    hg_e = gw_e.submitted[1]["client_order_id"]
    gw_e.deliver_reject(hg_e, reason="rejected")
    ctrl_e.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)
    hm_e = gw_e.submitted[2]["client_order_id"]
    gw_e.deliver_reject(hm_e, reason="rejected")
    ctrl_e.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    assert ctrl_e._maker_unhedged_qty(ctrl_e._active_makers[mk_e]) == D("0.10")
    gw_e.deliver_fill(mk_e, filled_base_qty=D("0.20"), avg_fill_price=D("99.98"), partial=True)
    ctrl_e.poll_active_orders(live_mids=mids, now_s=now_s + 4, live_quotes=quotes)
    assert len(gw_e.submitted) == 4 and gw_e.submitted[3]["base_qty"] == D("0.20")
    led_e.close()


def test_naked_49usd_leg_after_fallback_reject_triggers_unhedged_timeout_kill_and_closes(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    assert plan.risk.slice_usd == D("50")
    assert plan.risk.max_unhedged_usd == D("60")
    assert plan.risk.max_unhedged_secs == 10

    led = CarryLedger(tmp_path / "naked49.sqlite3", plan.fingerprint())
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.28, 100.32)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.30}
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)

    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.28, ask=100.32),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]
    pid, err = ctrl.start_entry(cand, specs=specs, now_s=now_s)
    assert err is None and pid is not None
    tk_l = next(o["client_order_id"] for o in gw.submitted if o["side"] == "BUY")
    tk_s = next(o["client_order_id"] for o in gw.submitted if o["side"] == "SELL")
    gw.deliver_fill(tk_l, filled_base_qty=D("0.49"), avg_fill_price=D("100.02"))
    gw.deliver_cancel(tk_s, filled_base_qty=D("0"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    fb_s = gw.submitted[2]
    gw.deliver_reject(fb_s["client_order_id"], reason="insufficient_margin")
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)

    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 3) == []
    active = led.active_positions()
    assert D(active[pid]["long_base_qty"]) == D("0.49") and D(active[pid]["short_base_qty"]) == D("0")

    fast = FastRiskEvaluator(plan.risk)
    # Balanced and within-2%-mismatch positions must NOT trip timeout
    bal_pos = {
        "ok1": {"entry_l": 100.0, "entry_s": 100.0, "iid_long": "L", "iid_short": "S", "long_base_qty": "0.49", "short_base_qty": "0.49"},
        "ok2": {"entry_l": 100.0, "entry_s": 100.0, "iid_long": "L", "iid_short": "S", "long_base_qty": "1.00", "short_base_qty": "0.985"},
    }
    assert not fast.evaluate(positions=bal_pos, live_mids={"L": 100.0, "S": 100.0}, daily_realized_pnl_usd=D("0"), consecutive_unhedged=0, now_s=now_s).halt_system
    assert not fast.evaluate(positions=bal_pos, live_mids={"L": 100.0, "S": 100.0}, daily_realized_pnl_usd=D("0"), consecutive_unhedged=0, now_s=now_s + 20).halt_system

    # $49 naked leg starts timer at now_s+3, stays open at +12 (9s), triggers kill + halt at +13 (10s)
    dec0 = fast.evaluate(positions=active, live_mids=mids, daily_realized_pnl_usd=D("0"), consecutive_unhedged=0, now_s=now_s + 3)
    assert not dec0.halt_system and dec0.close_positions == ()
    dec9 = fast.evaluate(positions=active, live_mids=mids, daily_realized_pnl_usd=D("0"), consecutive_unhedged=0, now_s=now_s + 12)
    assert not dec9.halt_system and dec9.close_positions == ()
    dec10 = fast.evaluate(positions=active, live_mids=mids, daily_realized_pnl_usd=D("0"), consecutive_unhedged=0, now_s=now_s + 13)
    assert dec10.halt_system and dec10.halt_reason == "unhedged_timeout_kill"
    assert dec10.close_positions == ((pid, "unhedged_timeout_kill"),)

    led.set_halt("HALTED_KILL", dec10.halt_reason, now_s + 13)
    ctrl.emergency_close_position(pid, specs=specs, reason="unhedged_timeout_kill", now_s=now_s + 13, live_mids=mids)
    em_orders = [o for o in gw.submitted[3:] if o["order_type"] == "MARKET"]
    assert len(em_orders) == 1
    assert em_orders[0]["side"] == "SELL" and em_orders[0]["reduce_only"] is True and em_orders[0]["base_qty"] == D("0.49")

    gw.deliver_fill(em_orders[0]["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 14, live_quotes=quotes)
    assert pid not in led.active_positions()
    row = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id=?", (pid,)).fetchone())
    assert row["status"] == "CLOSED" and row["close_reason"] == "unhedged_timeout_kill"
    assert led.is_halted() == (True, "unhedged_timeout_kill")
    led.close()


def test_emergency_close_blocks_late_opening_hedges_and_covers_late_fill_to_closed(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.15}
    now_s = 1_800_000_000
    snap_mk = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand_mk = evaluate_entry_candidates(
        snap_mk, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]

    # Case A: Pending Slice 2 MAKER_OPEN gets late CANCELED+0.20 fill during emergency close
    led_a = CarryLedger(tmp_path / "em_race_a.sqlite3", plan.fingerprint())
    gw_a = DelayedAsyncVenueGateway()
    gw_a.register_spec(spec_l)
    gw_a.register_spec(spec_s)
    ctrl_a = CarryExecutionController(ledger=led_a, gateway=gw_a, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    pid_a, _ = ctrl_a.start_entry(cand_mk, specs=specs, now_s=now_s)
    mk1 = gw_a.submitted[0]["client_order_id"]
    gw_a.deliver_fill(mk1, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    ctrl_a.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    gw_a.deliver_fill(gw_a.submitted[1]["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=D("100.13"))
    ctrl_a.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)
    assert ctrl_a.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 3) == [pid_a]
    mk2 = gw_a.submitted[2]["client_order_id"]

    # Enter emergency close (cancel on mk2 not yet confirmed); initial 0.49/0.49 closes first
    ctrl_a.emergency_close_position(pid_a, specs=specs, reason="basis_stop_30m", now_s=now_s + 4, live_mids=mids)
    assert gw_a.cancel_requests == [mk2]
    em1_count = len(gw_a.submitted)
    em_l1 = next(o["client_order_id"] for o in gw_a.submitted[3:] if o["side"] == "SELL")
    em_s1 = next(o["client_order_id"] for o in gw_a.submitted[3:] if o["side"] == "BUY")
    gw_a.deliver_fill(em_l1, filled_base_qty=D("0.49"), avg_fill_price=D("99.99"))
    gw_a.deliver_fill(em_s1, filled_base_qty=D("0.49"), avg_fill_price=D("100.15"))
    ctrl_a.poll_active_orders(live_mids=mids, now_s=now_s + 5, live_quotes=quotes)
    # Must stay CLOSING while mk2 cancel is unconfirmed
    assert pid_a in led_a.active_positions()
    assert led_a.active_positions()[pid_a]["status"] == "CLOSING"

    # Late maker cancel arrives with 0.20 fill -> ZERO reduce_only=False orders; must emit reduce_only=True SELL 0.20
    gw_a.deliver_cancel(mk2, filled_base_qty=D("0.20"), avg_fill_price=D("99.98"))
    ctrl_a.poll_active_orders(live_mids=mids, now_s=now_s + 6, live_quotes=quotes)
    post_em = gw_a.submitted[em1_count:]
    assert all(o["reduce_only"] is True for o in post_em)
    assert len(post_em) == 1
    assert post_em[0]["side"] == "SELL" and post_em[0]["base_qty"] == D("0.20")
    assert led_a.active_positions()[pid_a]["status"] == "CLOSING"

    gw_a.deliver_fill(post_em[0]["client_order_id"], filled_base_qty=D("0.20"), avg_fill_price=D("99.97"))
    ctrl_a.poll_active_orders(live_mids=mids, now_s=now_s + 7, live_quotes=quotes)
    assert pid_a not in led_a.active_positions()
    row_a = dict(led_a.conn.execute("SELECT status, long_base_qty, short_base_qty FROM positions WHERE position_id=?", (pid_a,)).fetchone())
    assert row_a["status"] == "CLOSED" and D(row_a["long_base_qty"]) == D("0") and D(row_a["short_base_qty"]) == D("0")
    led_a.close()

    # Case B1: In-flight HEDGE_OPEN partial fill + cancel while CLOSING -> no reduce_only=False fallback, covered by reduce_only=True close
    led_b1 = CarryLedger(tmp_path / "em_race_b1.sqlite3", plan.fingerprint())
    gw_b1 = DelayedAsyncVenueGateway()
    gw_b1.register_spec(spec_l)
    gw_b1.register_spec(spec_s)
    ctrl_b1 = CarryExecutionController(ledger=led_b1, gateway=gw_b1, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    pid_b1, _ = ctrl_b1.start_entry(cand_mk, specs=specs, now_s=now_s)
    mk_b1 = gw_b1.submitted[0]["client_order_id"]
    gw_b1.deliver_fill(mk_b1, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    ctrl_b1.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    hg_b1 = gw_b1.submitted[1]["client_order_id"]

    ctrl_b1.emergency_close_position(pid_b1, specs=specs, reason="basis_stop_30m", now_s=now_s + 2, live_mids=mids)
    pre_hg_count = len(gw_b1.submitted)
    em_l_b1 = next(o["client_order_id"] for o in gw_b1.submitted[2:] if o["side"] == "SELL")
    gw_b1.deliver_fill(em_l_b1, filled_base_qty=D("0.49"), avg_fill_price=D("99.99"))
    ctrl_b1.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    assert pid_b1 in led_b1.active_positions()

    gw_b1.deliver_cancel(hg_b1, filled_base_qty=D("0.20"), avg_fill_price=D("100.13"))
    ctrl_b1.poll_active_orders(live_mids=mids, now_s=now_s + 4, live_quotes=quotes)
    follow_b1 = gw_b1.submitted[pre_hg_count:]
    assert all(o["reduce_only"] is True for o in follow_b1)
    assert len(follow_b1) == 1 and follow_b1[0]["side"] == "BUY" and follow_b1[0]["base_qty"] == D("0.20")
    gw_b1.deliver_fill(follow_b1[0]["client_order_id"], filled_base_qty=D("0.20"), avg_fill_price=D("100.15"))
    ctrl_b1.poll_active_orders(live_mids=mids, now_s=now_s + 5, live_quotes=quotes)
    assert pid_b1 not in led_b1.active_positions()
    led_b1.close()

    # Case B2: In-flight DualTaker opening batch while CLOSING -> no TAKER_OPEN_MARKET_FALLBACK (reduce_only=False)
    led_b2 = CarryLedger(tmp_path / "em_race_b2.sqlite3", plan.fingerprint())
    gw_b2 = DelayedAsyncVenueGateway()
    gw_b2.register_spec(spec_l)
    gw_b2.register_spec(spec_s)
    ctrl_b2 = CarryExecutionController(ledger=led_b2, gateway=gw_b2, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    snap_tk = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.28, ask=100.32),
        }
    }
    cand_tk = evaluate_entry_candidates(
        snap_tk, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]
    pid_b2, _ = ctrl_b2.start_entry(cand_tk, specs=specs, now_s=now_s)
    tk_l2 = next(o["client_order_id"] for o in gw_b2.submitted if o["side"] == "BUY")
    tk_s2 = next(o["client_order_id"] for o in gw_b2.submitted if o["side"] == "SELL")
    gw_b2.deliver_fill(tk_l2, filled_base_qty=D("0.49"), avg_fill_price=D("100.02"))
    ctrl_b2.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    ctrl_b2.emergency_close_position(pid_b2, specs=specs, reason="unhedged_timeout_kill", now_s=now_s + 2, live_mids=mids)
    pre_tk_count = len(gw_b2.submitted)
    gw_b2.deliver_cancel(tk_s2, filled_base_qty=D("0"))
    ctrl_b2.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    assert gw_b2.submitted[pre_tk_count:] == []
    em_l_b2 = next(o["client_order_id"] for o in gw_b2.submitted[2:] if o["side"] == "SELL")
    gw_b2.deliver_fill(em_l_b2, filled_base_qty=D("0.49"), avg_fill_price=D("99.99"))
    ctrl_b2.poll_active_orders(live_mids=mids, now_s=now_s + 4, live_quotes=quotes)
    assert pid_b2 not in led_b2.active_positions()
    led_b2.close()


def test_strategy_on_stop_and_graceful_drain_cancel_makers_without_closing_open_positions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from types import SimpleNamespace
    from nautilus_trader.model import ClientOrderId, InstrumentId, Quantity
    from nautilus_trader.trading import Strategy, StrategyConfig
    import src.carry.runtime as carry_runtime

    assert StrategyConfig().manage_stop is False
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "stop_safety.sqlite3", plan.fingerprint())
    strat = carry_runtime._create_carry_strategy(plan, led, mode="paper")
    assert strat.config.manage_stop is False
    assert type(strat).on_stop is not Strategy.on_stop

    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.15}
    strat.gateway = gw
    strat.controller.gateway = gw
    strat.reconciled = True
    strat.live_mids = mids
    strat.live_quotes = quotes

    now_s = 1_800_000_000
    # Existing OPEN position must remain untouched across on_stop()
    led.create_position(
        position_id="OPEN-HOLD-1",
        symbol="ETH",
        long_venue="HL",
        short_venue="OKX",
        iid_long="ETH-USD-PERP.HYPERLIQUID",
        iid_short="ETH-USDT-SWAP.OKX",
        opened_ts=now_s - 3600,
        entry_l=2000.0,
        entry_s=2002.0,
        entry_basis_bps=10.0,
        target_base_qty=D("0.05"),
    )
    led.update_position_status("OPEN-HOLD-1", status="OPEN", ts_s=now_s - 3599, long_base_qty=D("0.05"), short_base_qty=D("0.05"))

    # Active MAKER_OPEN on SOL must be canceled by on_stop()
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]
    pid_mk, err_mk = strat.controller.start_entry(cand, specs=specs, now_s=now_s)
    assert err_mk is None and pid_mk is not None
    mk_coid = gw.submitted[0]["client_order_id"]

    strat.on_stop()
    assert gw.cancel_requests == [mk_coid]
    assert len(gw.submitted) == 1
    assert led.active_positions()["OPEN-HOLD-1"]["status"] == "OPEN"
    assert D(led.active_positions()["OPEN-HOLD-1"]["long_base_qty"]) == D("0.05")
    assert strat.controller.start_entry(cand, specs=specs, now_s=now_s + 1) == (None, "controller_stopping")
    assert strat.controller.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 1) == []

    # Verify real _NautilusStrategyGateway + on_stop passes ClientOrderId
    led_nt = CarryLedger(tmp_path / "stop_nt.sqlite3", plan.fingerprint())
    strat_nt = carry_runtime._create_carry_strategy(plan, led_nt, mode="live")
    nt_cancels: list[object] = []
    nt_orders: dict[str, object] = {}
    open_nt = SimpleNamespace(
        instrument_id=InstrumentId.from_str(spec_l.instrument_id),
        filled_qty=Quantity.from_str("0.0"),
        avg_px=None,
        status="ACCEPTED",
        venue_order_id="V-NT",
        is_closed=False,
    )

    def _make_limit(**kwargs):
        nt_orders[str(kwargs["client_order_id"])] = open_nt
        return open_nt

    fake_nt_strat = SimpleNamespace(
        cache=SimpleNamespace(
            order=lambda coid: nt_orders.get(str(coid)),
            instrument=lambda _iid: SimpleNamespace(size_precision=2, price_precision=2, multiplier=D("1")),
        ),
        order_factory=SimpleNamespace(limit=_make_limit),
        submit_order=lambda _order: None,
        cancel_order=lambda arg: nt_cancels.append(arg),
    )
    strat_nt.gateway = carry_runtime._NautilusStrategyGateway(fake_nt_strat)
    strat_nt.controller.gateway = strat_nt.gateway
    strat_nt.reconciled = True
    strat_nt.live_mids = mids
    strat_nt.live_quotes = quotes
    pid_nt, err_nt = strat_nt.controller.start_entry(cand, specs=specs, now_s=now_s + 5)
    assert err_nt is None and pid_nt is not None
    mk_nt_coid = next(iter(strat_nt.controller._active_makers))
    strat_nt.on_stop()
    assert len(nt_cancels) == 1 and isinstance(nt_cancels[0], ClientOrderId) and str(nt_cancels[0]) == mk_nt_coid
    led_nt.close()

    # Graceful drain: racing maker fill during drain is hedged before stop_cb fires
    led_drain = CarryLedger(tmp_path / "stop_drain.sqlite3", plan.fingerprint())
    strat_drain = carry_runtime._create_carry_strategy(plan, led_drain, mode="paper")
    gw_d = DelayedAsyncVenueGateway()
    gw_d.register_spec(spec_l)
    gw_d.register_spec(spec_s)
    strat_drain.gateway = gw_d
    strat_drain.controller.gateway = gw_d
    strat_drain.reconciled = True
    strat_drain.live_mids = mids
    strat_drain.live_quotes = quotes
    strat_drain._specs_from_cache = lambda: specs
    pid_d, _ = strat_drain.controller.start_entry(cand, specs=specs, now_s=now_s + 10)
    assert pid_d is not None
    mk_d = gw_d.submitted[0]["client_order_id"]

    stop_calls: list[str] = []
    strat_drain.request_graceful_shutdown(stop_cb=lambda: stop_calls.append("stopped"), drain_timeout_secs=60.0)
    strat_drain._on_fast_risk_timer(None)
    assert gw_d.cancel_requests == [mk_d]
    assert stop_calls == []

    gw_d.deliver_cancel(mk_d, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    strat_drain.on_order_canceled(SimpleNamespace(client_order_id=mk_d))
    assert len(gw_d.submitted) == 2
    assert stop_calls == []

    hg_d = gw_d.submitted[1]["client_order_id"]
    gw_d.deliver_fill(hg_d, filled_base_qty=D("0.49"), avg_fill_price=D("100.13"))
    strat_drain.on_order_filled(SimpleNamespace(client_order_id=hg_d))
    assert stop_calls == ["stopped"]
    led_drain.close()

    # Bounded drain timeout fires stop_cb even if an order never confirms
    led_to = CarryLedger(tmp_path / "stop_timeout.sqlite3", plan.fingerprint())
    strat_to = carry_runtime._create_carry_strategy(plan, led_to, mode="paper")
    gw_to = DelayedAsyncVenueGateway()
    gw_to.register_spec(spec_l)
    gw_to.register_spec(spec_s)
    strat_to.gateway = gw_to
    strat_to.controller.gateway = gw_to
    strat_to.reconciled = True
    strat_to.live_mids = mids
    strat_to.live_quotes = quotes
    strat_to._specs_from_cache = lambda: specs
    strat_to.controller.start_entry(cand, specs=specs, now_s=now_s + 20)
    to_calls: list[str] = []
    strat_to.request_graceful_shutdown(stop_cb=lambda: to_calls.append("stopped"), drain_timeout_secs=0.0)
    strat_to._on_fast_risk_timer(None)
    assert to_calls == ["stopped"]
    assert led_to.is_reconcile_halted()[0] is True
    led_to.close()

    # run_node invokes request_graceful_shutdown before handle.stop() on duration expiry
    stopped_evt = threading.Event()
    events: list[tuple[str, float]] = []

    class FakeHandle:
        def stop(self) -> None:
            events.append(("handle.stop", time.monotonic()))
            stopped_evt.set()

    class FakeStrategy:
        recorder = None

        def request_graceful_shutdown(self, *, stop_cb=None, drain_timeout_secs=10.0) -> None:
            events.append(("graceful_shutdown", drain_timeout_secs))
            if stop_cb is not None:
                stop_cb()

    class FakeNode:
        def handle(self):
            return FakeHandle()

        def run(self) -> None:
            assert stopped_evt.wait(timeout=5.0)

    led_rn = CarryLedger(tmp_path / "run_node.sqlite3", plan.fingerprint())
    monkeypatch.setattr(carry_runtime, "build_live_node", lambda _plan, mode: (FakeNode(), led_rn, FakeStrategy()))
    rc = carry_runtime.run_node(plan, mode="paper", duration_secs=1)
    assert rc == 0
    assert [e[0] for e in events] == ["graceful_shutdown", "handle.stop"]
    led.close()


def test_specs_from_cache_extracts_real_nautilus_money_min_notional(tmp_path: Path) -> None:
    from types import SimpleNamespace
    from nautilus_trader.model import CryptoPerpetual, Currency, InstrumentId, Money, Price, Quantity, Symbol
    from src.carry.runtime import _create_carry_strategy

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "specs_money.sqlite3", plan.fingerprint())
    strat = _create_carry_strategy(plan, led, mode="paper")

    cp_hl = CryptoPerpetual(
        instrument_id=InstrumentId.from_str("SOL-USD-PERP.HYPERLIQUID"),
        raw_symbol=Symbol("SOL"),
        base_currency=Currency.from_str("SOL"),
        quote_currency=Currency.from_str("USD"),
        settlement_currency=Currency.from_str("USD"),
        is_inverse=False,
        price_precision=2,
        size_precision=2,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_str("0.01"),
        ts_event=0,
        ts_init=0,
        min_notional=Money(10.0, Currency.from_str("USD")),
    )
    assert str(cp_hl.min_notional) == "10.00 USD"

    cp_none = CryptoPerpetual(
        instrument_id=InstrumentId.from_str("ETH-USD-PERP.HYPERLIQUID"),
        raw_symbol=Symbol("ETH"),
        base_currency=Currency.from_str("ETH"),
        quote_currency=Currency.from_str("USD"),
        settlement_currency=Currency.from_str("USD"),
        is_inverse=False,
        price_precision=2,
        size_precision=3,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_str("0.001"),
        ts_event=0,
        ts_init=0,
        min_notional=None,
    )
    mock_dec = SimpleNamespace(
        id=InstrumentId.from_str("BTC-PERP.LIGHTER"),
        size_increment="0.001",
        multiplier="1",
        min_quantity="0.001",
        min_notional=D("5.5"),
        price_increment="0.1",
    )
    mock_int = SimpleNamespace(
        id=InstrumentId.from_str("BTCUSDT-LINEAR.BYBIT"),
        size_increment="0.001",
        multiplier="1",
        min_quantity="0.001",
        min_notional=15,
        price_increment="0.1",
    )
    mock_str = SimpleNamespace(
        id=InstrumentId.from_str("SOL-USDT-SWAP.OKX"),
        size_increment="0.1",
        multiplier="0.1",
        min_quantity="0.1",
        min_notional="20.00 USD",
        price_increment="0.01",
    )

    fake_self = SimpleNamespace(
        cache=SimpleNamespace(instruments=lambda: [cp_hl, cp_none, mock_dec, mock_int, mock_str]),
        paper_gateway=None,
        live_mids={},
    )
    specs = type(strat)._specs_from_cache(fake_self)
    assert specs["SOL-USD-PERP.HYPERLIQUID"].venue == "HL"
    assert specs["SOL-USD-PERP.HYPERLIQUID"].min_notional_usd == D("10.00")
    assert specs["ETH-USD-PERP.HYPERLIQUID"].min_notional_usd == D("0")
    assert specs["BTC-PERP.LIGHTER"].min_notional_usd == D("5.5")
    assert specs["BTCUSDT-LINEAR.BYBIT"].min_notional_usd == D("15")
    assert specs["SOL-USDT-SWAP.OKX"].min_notional_usd == D("20.00")
    led.close()


def test_unhedged_streak_resets_on_successful_hedge_and_dual_taker(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "streak.sqlite3", plan.fingerprint())
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.15}
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    fast = FastRiskEvaluator(plan.risk)

    now_s = 1_800_000_000
    snap_mk = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand_mk = evaluate_entry_candidates(
        snap_mk, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]

    def _run_maker_hedge(*, succeed: bool, t_offset: int) -> None:
        sym_snap = {
            f"SYM{t_offset}": {
                "HL": _make_leg("HL", f"SYM{t_offset}", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
                "OKX": _make_leg("OKX", f"SYM{t_offset}", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
            }
        }
        c = evaluate_entry_candidates(
            sym_snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
        )[0]
        sp_l = InstrumentSpec(c.long_leg.instrument_id, "HL", size_increment=D("0.01"), price_increment=D("0.01"))
        sp_s = InstrumentSpec(c.short_leg.instrument_id, "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
        gw.register_spec(sp_l)
        gw.register_spec(sp_s)
        local_specs = {sp_l.instrument_id: sp_l, sp_s.instrument_id: sp_s}
        local_quotes = {sp_l.instrument_id: (99.98, 100.02), sp_s.instrument_id: (100.13, 100.17)}
        local_mids = {sp_l.instrument_id: 100.00, sp_s.instrument_id: 100.15}
        pid, _ = ctrl.start_entry(c, specs=local_specs, now_s=now_s + t_offset)
        assert pid is not None
        mk = gw.submitted[-1]["client_order_id"]
        gw.deliver_fill(mk, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
        ctrl.poll_active_orders(live_mids=local_mids, now_s=now_s + t_offset + 1, live_quotes=local_quotes)
        hg = gw.submitted[-1]["client_order_id"]
        if succeed:
            gw.deliver_fill(hg, filled_base_qty=D("0.49"), avg_fill_price=D("100.13"))
            ctrl.poll_active_orders(live_mids=local_mids, now_s=now_s + t_offset + 2, live_quotes=local_quotes)
        else:
            gw.deliver_reject(hg, reason="rejected")
            ctrl.poll_active_orders(live_mids=local_mids, now_s=now_s + t_offset + 2, live_quotes=local_quotes)
            hm = gw.submitted[-1]["client_order_id"]
            gw.deliver_reject(hm, reason="rejected")
            ctrl.poll_active_orders(live_mids=local_mids, now_s=now_s + t_offset + 3, live_quotes=local_quotes)

    # 2 failures -> 1 success resets streak to 0 -> 1 failure makes streak == 1 (NOT halted)
    _run_maker_hedge(succeed=False, t_offset=0)
    _run_maker_hedge(succeed=False, t_offset=10)
    assert led.daily_stats(now_s + 20)[1] == 2
    assert not fast.evaluate(positions={}, live_mids={}, daily_realized_pnl_usd=D("0"), consecutive_unhedged=2, now_s=now_s + 20).halt_system

    _run_maker_hedge(succeed=True, t_offset=30)
    assert led.daily_stats(now_s + 35)[1] == 0

    _run_maker_hedge(succeed=False, t_offset=40)
    assert led.daily_stats(now_s + 45)[1] == 1
    assert not fast.evaluate(positions={}, live_mids={}, daily_realized_pnl_usd=D("0"), consecutive_unhedged=1, now_s=now_s + 45).halt_system

    # 2 more consecutive failures hit streak == 3 -> consecutive_unhedged_kill
    _run_maker_hedge(succeed=False, t_offset=50)
    _run_maker_hedge(succeed=False, t_offset=60)
    assert led.daily_stats(now_s + 65)[1] == 3
    dec_kill = fast.evaluate(positions={}, live_mids={}, daily_realized_pnl_usd=D("0"), consecutive_unhedged=3, now_s=now_s + 65)
    assert dec_kill.halt_system and dec_kill.halt_reason == "consecutive_unhedged_kill"

    # DualTaker open (balanced) and DualTaker close (no shortfall) also reset streak to 0
    led.increment_unhedged_streak(now_s + 70, reset=True)
    led.increment_unhedged_streak(now_s + 70)
    led.increment_unhedged_streak(now_s + 70)
    assert led.daily_stats(now_s + 70)[1] == 2
    snap_tk = {
        "TKSYM": {
            "HL": _make_leg("HL", "TKSYM", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "TKSYM", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.28, ask=100.32),
        }
    }
    cand_tk = evaluate_entry_candidates(
        snap_tk, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s + 71
    )[0]
    sp_tl = InstrumentSpec(cand_tk.long_leg.instrument_id, "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    sp_ts = InstrumentSpec(cand_tk.short_leg.instrument_id, "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(sp_tl)
    gw.register_spec(sp_ts)
    t_specs = {sp_tl.instrument_id: sp_tl, sp_ts.instrument_id: sp_ts}
    t_quotes = {sp_tl.instrument_id: (99.98, 100.02), sp_ts.instrument_id: (100.28, 100.32)}
    t_mids = {sp_tl.instrument_id: 100.00, sp_ts.instrument_id: 100.30}
    pid_tk, _ = ctrl.start_entry(cand_tk, specs=t_specs, now_s=now_s + 71)
    assert pid_tk is not None
    gw.deliver_fill(gw.submitted[-2]["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=D("100.02"))
    gw.deliver_fill(gw.submitted[-1]["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=D("100.28"))
    ctrl.poll_active_orders(live_mids=t_mids, now_s=now_s + 72, live_quotes=t_quotes)
    assert led.daily_stats(now_s + 72)[1] == 0

    led.increment_unhedged_streak(now_s + 73)
    assert led.daily_stats(now_s + 73)[1] == 1
    led.update_position_status(pid_tk, status="CLOSING", ts_s=now_s + 74, close_reason="carry_decay")
    ctrl._slice_attempts[pid_tk] = plan.runtime.maker_max_attempts
    ctrl.advance_pending_slices(specs=t_specs, quotes=t_quotes, mids=t_mids, now_s=now_s + 75)
    gw.deliver_fill(gw.submitted[-2]["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    gw.deliver_fill(gw.submitted[-1]["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=D("100.32"))
    ctrl.poll_active_orders(live_mids=t_mids, now_s=now_s + 76, live_quotes=t_quotes)
    assert led.daily_stats(now_s + 76)[1] == 0
    led.close()


def test_cold_start_reconciliation_reads_exchange_ground_truth_and_fails_closed(tmp_path: Path) -> None:
    from src.carry.account_reader import CarryStartupAccountReader, StartupSnapshotError, VenueAccountSnapshot
    from src.carry.runtime import _create_carry_strategy, _live_exec_engine_config

    assert _live_exec_engine_config("live").generate_missing_orders is True
    assert _live_exec_engine_config("readonly").generate_missing_orders is True
    assert _live_exec_engine_config("paper").generate_missing_orders is False

    env = {
        "HYPERLIQUID_ACCOUNT_ADDRESS": "0x" + "11" * 20,
        "LIGHTER_ACCOUNT_INDEX": "42",
        "OKX_API_KEY": "okx-secret-key",
        "OKX_API_SECRET": "okx-secret-val",
        "OKX_API_PASSPHRASE": "okx-secret-pass",
        "BYBIT_API_KEY": "bybit-secret-key",
        "BYBIT_API_SECRET": "bybit-secret-val",
    }

    def fake_http(method: str, url: str, *, headers=None, body=None, timeout=10.0):
        if "hyperliquid.xyz" in url:
            if body["type"] == "clearinghouseState":
                if body.get("dex") == "xyz":
                    return {"assetPositions": [{"position": {"coin": "xyz:NVDA", "szi": "-0.5"}}]}
                return {"assetPositions": [{"position": {"coin": "SOL", "szi": "0.49"}}]}
            if body["type"] == "frontendOpenOrders":
                if body.get("dex") == "xyz":
                    return []
                return [{"coin": "SOL", "oid": 101}]
        if "zklighter.elliot.ai" in url:
            return {
                "code": 200,
                "accounts": [
                    {
                        "account_index": 42,
                        "index": 42,
                        "total_order_count": 2,
                        "total_isolated_order_count": 0,
                        "pending_order_count": 0,
                        "positions": [
                            {
                                "symbol": "ETH",
                                "position": "0.25",
                                "sign": -1,
                                "open_order_count": 1,
                                "pending_order_count": 0,
                                "position_tied_order_count": 0,
                            }
                        ],
                    }
                ],
            }
        if "okx.com/api/v5/account/positions" in url:
            return {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "pos": "-4.9", "posSide": "net"}]}
        if "okx.com/api/v5/public/instruments" in url:
            return {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "ctVal": "0.1"}]}
        if "okx.com/api/v5/trade/orders-pending" in url:
            if "after=" not in url:
                return {
                    "code": "0",
                    "data": [{"instId": "SOL-USDT-SWAP", "ordId": f"okx-{i}"} for i in range(100)],
                }
            return {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "ordId": "okx-100"}]}
        if "okx.com/api/v5/trade/orders-algo-pending" in url:
            return {"code": "0", "data": []}
        if "bybit.com/v5/position/list" in url:
            if "settleCoin=USDT" in url and "cursor=" not in url:
                return {
                    "retCode": 0,
                    "result": {
                        "list": [{"symbol": "BTCUSDT", "size": "0.01", "side": "Buy"}],
                        "nextPageCursor": "p2",
                    },
                }
            if "settleCoin=USDT" in url and "cursor=p2" in url:
                return {
                    "retCode": 0,
                    "result": {
                        "list": [{"symbol": "ETHUSDT", "size": "0.2", "side": "Sell"}],
                        "nextPageCursor": "",
                    },
                }
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        if "bybit.com/v5/order/realtime" in url:
            if "orderFilter=StopOrder" in url:
                return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
            if "settleCoin=USDC" in url:
                return {
                    "retCode": 0,
                    "result": {
                        "list": [{"symbol": "BTCUSDT", "orderId": "byb-1"}],
                        "nextPageCursor": "",
                    },
                }
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        raise AssertionError(f"unexpected request {method} {url}")

    reader = CarryStartupAccountReader(environment="mainnet", environ=env, http_request=fake_http)
    snap = reader.fetch_startup_snapshot(("HL", "LIGHTER", "OKX", "BYBIT"))
    assert snap.positions == {
        ("HL", "SOL-USD-PERP.HYPERLIQUID"): D("0.49"),
        ("HL", "xyz:NVDA-USD-PERP.HYPERLIQUID"): D("-0.5"),
        ("LIGHTER", "ETH-PERP.LIGHTER"): D("-0.25"),
        ("OKX", "SOL-USDT-SWAP.OKX"): D("-0.49"),
        ("BYBIT", "BTCUSDT-LINEAR.BYBIT"): D("0.01"),
        ("BYBIT", "ETHUSDT-LINEAR.BYBIT"): D("-0.2"),
    }
    assert snap.open_orders == {
        ("HL", "SOL-USD-PERP.HYPERLIQUID"): 1,
        ("LIGHTER", "ETH-PERP.LIGHTER"): 1,
        ("LIGHTER", "account.LIGHTER"): 1,
        ("OKX", "SOL-USDT-SWAP.OKX"): 101,
        ("BYBIT", "BTCUSDT-LINEAR.BYBIT"): 1,
    }

    # Any single configured venue failure raises StartupSnapshotError without exposing secrets
    def failing_http(_method: str, url: str, **_kw):
        if "bybit.com" in url:
            raise RuntimeError("bybit-secret-val should not leak")
        return fake_http("GET", url, **_kw) if "zklighter" in url or "okx" in url else fake_http("POST", url, **_kw)

    bad_reader = CarryStartupAccountReader(environment="mainnet", environ=env, http_request=failing_http)
    with pytest.raises(StartupSnapshotError) as exc_info:
        bad_reader.fetch_startup_snapshot(("HL", "BYBIT"))
    assert "bybit-secret" not in str(exc_info.value)

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")

    # 1) Cold cache + empty ledger + old open position on exchange -> MUST HALT_RECONCILE
    class _StaticReader:
        def __init__(self, result: VenueAccountSnapshot | Exception) -> None:
            self._result = result

        def fetch_startup_snapshot(self, _venues, *, okx_multipliers=None):
            if isinstance(self._result, Exception):
                raise self._result
            return self._result

    from types import SimpleNamespace

    led_empty = CarryLedger(tmp_path / "cold_empty.sqlite3", plan.fingerprint(), ledger_domain="live")
    strat_empty = _create_carry_strategy(
        plan,
        led_empty,
        mode="live",
        account_reader=_StaticReader(
            VenueAccountSnapshot(
                positions={("HL", "SOL-USD-PERP.HYPERLIQUID"): D("0.49"), ("OKX", "SOL-USDT-SWAP.OKX"): D("-0.49")},
                open_orders={},
            )
        ),
    )
    strat_empty._reconcile_on_start()
    assert strat_empty.reconciled is False
    assert led_empty.is_reconcile_halted()[0] is True
    led_empty.close()

    # 2) Cold cache (cache.positions_open() == []) + multi-day OPEN ledger + matching exchange position -> succeeds cleanly
    led_open = CarryLedger(tmp_path / "cold_match.sqlite3", plan.fingerprint(), ledger_domain="live")
    led_open.create_position(
        position_id="SOL-MULTIDAY",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long="SOL-USD-PERP.HYPERLIQUID",
        iid_short="SOL-USDT-SWAP.OKX",
        opened_ts=1_799_000_000,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.49"),
    )
    led_open.update_position_status(
        "SOL-MULTIDAY", status="OPEN", ts_s=1_799_000_005, long_base_qty=D("0.49"), short_base_qty=D("0.49")
    )
    strat_open = _create_carry_strategy(
        plan,
        led_open,
        mode="live",
        account_reader=_StaticReader(
            VenueAccountSnapshot(
                positions={("HL", "SOL-USD-PERP.HYPERLIQUID"): D("0.49"), ("OKX", "SOL-USDT-SWAP.OKX"): D("-0.49")},
                open_orders={},
            )
        ),
    )
    fake_cold_strat = SimpleNamespace(
        plan=strat_open.plan,
        ledger=strat_open.ledger,
        account_reader=strat_open.account_reader,
        cache=SimpleNamespace(positions_open=lambda: [], orders_open=lambda: [], instruments=lambda: []),
        reconciled=False,
        log=strat_open.log,
    )
    type(strat_open)._reconcile_on_start(fake_cold_strat)
    assert fake_cold_strat.reconciled is True
    assert led_open.is_halted() == (False, None)
    led_open.close()

    # 3) Venue snapshot failure during startup reconcile -> fail-closed HALTED_RECONCILE
    led_err = CarryLedger(tmp_path / "cold_err.sqlite3", plan.fingerprint(), ledger_domain="live")
    strat_err = _create_carry_strategy(
        plan,
        led_err,
        mode="readonly",
        account_reader=_StaticReader(StartupSnapshotError("OKX credentials missing")),
    )
    strat_err._reconcile_on_start()
    assert strat_err.reconciled is False
    assert led_err.is_reconcile_halted()[0] is True
    led_err.close()


def test_halted_reconcile_blocks_emergency_and_basis_close_orders_while_kill_allows_flatten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.carry.account_reader import VenueAccountSnapshot
    from src.carry.runtime import _create_carry_strategy

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}

    class _EmptyExchangeReader:
        def fetch_startup_snapshot(self, _venues, *, okx_multipliers=None):
            return VenueAccountSnapshot(positions={}, open_orders={})

    # Reconcile failure: ledger thinks 0.49 is OPEN, exchange is flat -> ZERO orders allowed on risk/basis triggers
    led = CarryLedger(tmp_path / "halt_rec.sqlite3", plan.fingerprint(), ledger_domain="live")
    led.create_position(
        position_id="SOL-PHANTOM",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=1_800_000_000,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.49"),
    )
    led.update_position_status(
        "SOL-PHANTOM", status="OPEN", ts_s=1_800_000_001, long_base_qty=D("0.49"), short_base_qty=D("0.49")
    )

    strat = _create_carry_strategy(plan, led, mode="live", account_reader=_EmptyExchangeReader())
    gw = DelayedAsyncVenueGateway()
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    strat.gateway = gw
    strat.controller.gateway = gw
    strat._specs_from_cache = lambda: specs
    strat._reconcile_on_start()
    assert strat.reconciled is False
    assert led.is_reconcile_halted()[0] is True

    # Trigger margin_guard (>25% adverse drop on long leg)
    strat.live_mids = {spec_l.instrument_id: 50.0, spec_s.instrument_id: 100.2}
    strat._on_fast_risk_timer(None)
    assert len(gw.submitted) == 0

    # Trigger basis_stop_30m (250 bps adverse basis sustained over 30m)
    t_base = 1_800_010_000
    monkeypatch.setattr(time, "time", lambda: float(t_base))
    strat.live_mids = {spec_l.instrument_id: 100.0, spec_s.instrument_id: 102.5}
    strat._on_basis_timer(None)
    monkeypatch.setattr(time, "time", lambda: float(t_base + 1805))
    strat._on_basis_timer(None)
    assert len(gw.submitted) == 0

    # Even direct controller close or on_stop must emit zero orders when HALTED_RECONCILE
    strat.controller.emergency_close_position(
        "SOL-PHANTOM", specs=specs, reason="margin_guard_25pct", now_s=t_base + 1810, live_mids=strat.live_mids
    )
    assert strat.controller.start_sliced_close(
        "SOL-PHANTOM",
        specs=specs,
        quotes={spec_l.instrument_id: (99.9, 100.1), spec_s.instrument_id: (102.4, 102.6)},
        mids=strat.live_mids,
        reason="carry_decay",
        now_s=t_base + 1811,
    ) is False
    strat.on_stop()
    assert len(gw.submitted) == 0
    assert led.active_positions()["SOL-PHANTOM"]["status"] == "OPEN"
    led.close()

    # Contrast: HALTED_KILL (reconciled ledger) still permits flattening known OPEN positions
    led_kill = CarryLedger(tmp_path / "halt_kill.sqlite3", plan.fingerprint(), ledger_domain="live")
    led_kill.create_position(
        position_id="SOL-REAL",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=1_800_000_000,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.49"),
    )
    led_kill.update_position_status(
        "SOL-REAL", status="OPEN", ts_s=1_800_000_001, long_base_qty=D("0.49"), short_base_qty=D("0.49")
    )
    strat_kill = _create_carry_strategy(
        plan,
        led_kill,
        mode="live",
        account_reader=type(
            "_MatchReader",
            (),
            {
                "fetch_startup_snapshot": lambda _s, _v, okx_multipliers=None: VenueAccountSnapshot(
                    positions={("HL", spec_l.instrument_id): D("0.49"), ("OKX", spec_s.instrument_id): D("-0.49")},
                    open_orders={},
                )
            },
        )(),
    )
    gw_kill = DelayedAsyncVenueGateway()
    gw_kill.register_spec(spec_l)
    gw_kill.register_spec(spec_s)
    strat_kill.gateway = gw_kill
    strat_kill.controller.gateway = gw_kill
    strat_kill._specs_from_cache = lambda: specs
    strat_kill._reconcile_on_start()
    assert strat_kill.reconciled is True

    led_kill.update_position_status("SOL-REAL", status="OPEN", ts_s=1_800_000_002)
    with led_kill.conn:
        led_kill.conn.execute(
            "INSERT OR REPLACE INTO daily_stats(utc_day, realized_pnl_usd, consecutive_unhedged) VALUES ('2027-01-15', '-45.0', 0)"
        )
    monkeypatch.setattr(time, "time", lambda: 1_800_000_100.0)
    strat_kill.live_mids = {spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2}
    strat_kill._on_fast_risk_timer(None)
    assert led_kill.is_kill_halted() == (True, "daily_loss_kill")
    assert len(gw_kill.submitted) == 2
    assert all(o["reduce_only"] is True for o in gw_kill.submitted)
    led_kill.close()


def test_subsequent_opening_slices_enforce_fresh_bbo_and_basis_spread_gates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.carry.runtime import _create_carry_strategy

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    now_s = 1_800_000_000

    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.23, ask=100.27),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]

    # (a) & (c): Empty quotes/mids AND ~198 bps blowout block slice 2 on both Maker and Dual-Taker
    led = CarryLedger(tmp_path / "slice_gates.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw = DelayedAsyncVenueGateway()
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    rt_two = plan.runtime.__class__(**{**plan.runtime.__dict__, "maker_max_attempts": 2})
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=rt_two, policy=plan.policy)
    pid, err = ctrl.start_entry(cand, specs=specs, now_s=now_s)
    assert err is None and pid is not None
    assert len(gw.submitted) == 2
    for rec in gw.submitted:
        px = D("100.02") if rec["side"] == "BUY" else D("100.23")
        gw.deliver_fill(rec["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=px)
    ctrl.poll_active_orders(live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.25}, now_s=now_s + 1)
    assert led.active_positions()[pid]["status"] == "OPENING"

    # (a) Empty quotes AND empty mids -> MUST NOT fall back to entry_l / entry_s
    assert ctrl.advance_pending_slices(specs=specs, quotes={}, mids={}, now_s=now_s + 2) == []
    assert len(gw.submitted) == 2
    # Also mids-only without real BBO -> MUST NOT submit OPENING slice
    assert ctrl.advance_pending_slices(
        specs=specs,
        quotes={},
        mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.25},
        now_s=now_s + 3,
    ) == []
    assert len(gw.submitted) == 2

    # (c) ~198 bps wide basis -> neither Dual-Taker nor Maker submits; 2 attempts finalize partial OPENING -> OPEN
    wide_quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (101.98, 102.02)}
    assert ctrl.advance_pending_slices(specs=specs, quotes=wide_quotes, mids={}, now_s=now_s + 4) == []
    assert len(gw.submitted) == 2
    assert ctrl._slice_attempts[pid] == 1
    assert ctrl.advance_pending_slices(specs=specs, quotes=wide_quotes, mids={}, now_s=now_s + 5) == []
    assert len(gw.submitted) == 2
    assert led.active_positions()[pid]["status"] == "OPEN"

    # Wide spread (>25 bps) and price ratio deviation (>5%) also blocked on subsequent OPENING slices
    led.create_position(
        position_id="SOL-SP-GATE",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s + 6,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.98"),
    )
    assert ctrl.advance_pending_slices(
        specs=specs,
        quotes={spec_l.instrument_id: (99.50, 100.50), spec_s.instrument_id: (100.23, 100.27)},
        mids={},
        now_s=now_s + 6,
    ) == []
    assert len(gw.submitted) == 2
    assert ctrl._slice_attempts["SOL-SP-GATE"] == 1
    assert ctrl.advance_pending_slices(
        specs=specs,
        quotes={spec_l.instrument_id: (100.00, 100.02), spec_s.instrument_id: (106.00, 106.02)},
        mids={},
        now_s=now_s + 7,
    ) == []
    assert len(gw.submitted) == 2
    row_sp = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id='SOL-SP-GATE'").fetchone())
    assert row_sp["status"] == "CLOSED" and row_sp["close_reason"] == "entry_maker_timeout"

    # Also verify maker_exec_basis > max_entry_basis_bps when taker_fast_entry_basis_bps=0
    pol_mk_only = plan.policy.__class__(**{**plan.policy.__dict__, "taker_fast_entry_basis_bps": D("0")})
    ctrl_mk = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=rt_two, policy=pol_mk_only)
    led.create_position(
        position_id="SOL-MK-CAP",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s + 8,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.98"),
    )
    ok_mk, err_mk = ctrl_mk._submit_next_slice(
        position_id="SOL-MK-CAP",
        is_opening=True,
        spec_l=spec_l,
        spec_s=spec_s,
        leg_l_bid=D("99.91"),
        leg_l_ask=D("100.09"),
        leg_l_mid=D("100.00"),
        leg_s_bid=D("100.55"),
        leg_s_ask=D("100.61"),
        leg_s_mid=D("100.58"),
        remaining_base=D("0.98"),
        sp_l=18.0,
        sp_s=6.0,
        now_s=now_s + 9,
    )
    assert not ok_mk and err_mk == "exec_basis_above_max"
    assert len(gw.submitted) == 2

    # Zero-fill OPENING position hitting max attempts closes as entry_maker_timeout
    pid_zero, _ = ctrl.start_entry(cand, specs=specs, now_s=now_s + 10)
    assert pid_zero is not None
    assert len(gw.submitted) == 4
    for rec in gw.submitted[2:]:
        gw.deliver_cancel(rec["client_order_id"], filled_base_qty=D("0"))
    ctrl.poll_active_orders(live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.25}, now_s=now_s + 11)
    assert ctrl.advance_pending_slices(specs=specs, quotes=wide_quotes, mids={}, now_s=now_s + 12) == []
    assert len(gw.submitted) == 4
    row_z = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id=?", (pid_zero,)).fetchone())
    assert row_z["status"] == "CLOSED" and row_z["close_reason"] == "entry_maker_timeout"

    # (b) Strategy _on_fast_risk_timer evicts stale quotes (> quote_stale_secs) BEFORE advance_pending_slices
    led_st = CarryLedger(tmp_path / "stale_fast.sqlite3", plan.fingerprint(), ledger_domain="paper")
    strat = _create_carry_strategy(plan, led_st, mode="paper")
    gw_st = DelayedAsyncVenueGateway()
    gw_st.register_spec(spec_l)
    gw_st.register_spec(spec_s)
    strat.gateway = gw_st
    strat.controller.gateway = gw_st
    strat._specs_from_cache = lambda: specs
    strat.reconciled = True
    pid_st, _ = strat.controller.start_entry(cand, specs=specs, now_s=now_s + 20)
    assert pid_st is not None and len(gw_st.submitted) == 2
    for rec in gw_st.submitted:
        px = D("100.02") if rec["side"] == "BUY" else D("100.23")
        gw_st.deliver_fill(rec["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=px)
    strat.controller.poll_active_orders(live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.25}, now_s=now_s + 21)

    strat.live_quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.23, 100.27)}
    strat.live_quote_ts = {spec_l.instrument_id: float(now_s + 1), spec_s.instrument_id: float(now_s + 20)}
    strat.live_mids = {spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.25}
    monkeypatch.setattr(time, "time", lambda: float(now_s + 25))
    strat._on_fast_risk_timer(None)
    assert spec_l.instrument_id not in strat.live_quotes
    assert spec_s.instrument_id in strat.live_quotes
    assert len(gw_st.submitted) == 2

    led.close()
    led_st.close()


def test_daily_loss_and_unhedged_streak_halt_even_when_flat_and_block_new_entries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.carry.runtime import _create_carry_strategy

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.23, ask=100.27),
        }
    }

    def _flush_slow_poll(s) -> None:
        deadline = time.monotonic() + 2.0
        while s._slow_poll_inflight and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not s._slow_poll_inflight
        s._consume_pending_slow_snapshot()

    def _run_flat_kill_scenario(name: str, *, pnl_usd: str, streak: int, expected_reason: str) -> None:
        led = CarryLedger(tmp_path / f"{name}.sqlite3", plan.fingerprint(), ledger_domain="paper")
        led.create_position(
            position_id=f"P-{name}",
            symbol="SOL",
            long_venue="HL",
            short_venue="OKX",
            iid_long=spec_l.instrument_id,
            iid_short=spec_s.instrument_id,
            opened_ts=now_s - 100,
            entry_l=100.0,
            entry_s=100.2,
            entry_basis_bps=20.0,
            target_base_qty=D("1"),
        )
        led.update_position_status(
            f"P-{name}", status="CLOSED", ts_s=now_s - 10, realized_pnl_usd=D(pnl_usd), close_reason="basis_stop_30m"
        )
        for _ in range(streak):
            led.increment_unhedged_streak(now_s - 5)
        assert led.active_positions() == {}
        assert led.open_orders() == []
        assert led.is_halted() == (False, None)

        strat = _create_carry_strategy(plan, led, mode="paper")
        gw = DelayedAsyncVenueGateway()
        gw.register_spec(spec_l)
        gw.register_spec(spec_s)
        strat.gateway = gw
        strat.controller.gateway = gw
        strat._specs_from_cache = lambda: specs
        strat.feed = type("_StaticFeed", (), {"poll_snapshot": lambda *_a, **_kw: snap})()
        strat.live_quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.23, 100.27)}
        strat.live_quote_ts = {spec_l.instrument_id: float(now_s), spec_s.instrument_id: float(now_s)}
        strat.live_mids = {spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.25}
        monkeypatch.setattr(time, "time", lambda: float(now_s))

        strat._on_fast_risk_timer(None)
        assert led.is_kill_halted() == (True, expected_reason)
        strat._on_slow_timer(None)
        _flush_slow_poll(strat)
        assert led.active_positions() == {}
        assert gw.submitted == []
        strat.close_slow_poller(wait=True)
        led.close()

    _run_flat_kill_scenario("daily_loss", pnl_usd="-41.65", streak=0, expected_reason="daily_loss_kill")
    _run_flat_kill_scenario("streak_kill", pnl_usd="0", streak=3, expected_reason="consecutive_unhedged_kill")

    # Defense-in-depth: _on_slow_timer and controller.start_entry also enforce daily limits directly
    led_slow = CarryLedger(tmp_path / "slow_defense.sqlite3", plan.fingerprint(), ledger_domain="paper")
    with led_slow.conn:
        led_slow.conn.execute(
            "INSERT INTO daily_stats(utc_day, realized_pnl_usd, consecutive_unhedged) VALUES ('2027-01-15', '-41.65', 0)"
        )
    strat_slow = _create_carry_strategy(plan, led_slow, mode="paper")
    gw_slow = DelayedAsyncVenueGateway()
    gw_slow.register_spec(spec_l)
    gw_slow.register_spec(spec_s)
    strat_slow.gateway = gw_slow
    strat_slow.controller.gateway = gw_slow
    strat_slow._specs_from_cache = lambda: specs
    strat_slow.feed = type("_StaticFeed2", (), {"poll_snapshot": lambda *_a, **_kw: snap})()
    strat_slow.live_quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.23, 100.27)}
    strat_slow.live_quote_ts = {spec_l.instrument_id: float(now_s), spec_s.instrument_id: float(now_s)}
    monkeypatch.setattr(time, "time", lambda: float(now_s))
    strat_slow._on_slow_timer(None)
    _flush_slow_poll(strat_slow)
    assert led_slow.is_kill_halted() == (True, "daily_loss_kill")
    assert led_slow.active_positions() == {}
    assert gw_slow.submitted == []

    led_slow.clear_halt("HALTED_KILL", now_s + 1)
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]
    pid_rej, err_rej = strat_slow.controller.start_entry(cand, specs=specs, now_s=now_s + 2)
    assert pid_rej is None and err_rej == "halted:daily_loss_kill"
    assert led_slow.is_kill_halted() == (True, "daily_loss_kill")
    strat_slow.close_slow_poller(wait=True)
    led_slow.close()


def test_realized_price_pnl_uses_actual_opening_fill_vwap_not_signal_mid(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    led = CarryLedger(tmp_path / "vwap_pnl.sqlite3", plan.fingerprint(), ledger_domain="live")
    now_s = 1_800_000_000
    iid_l = "SOL-USD-PERP.HYPERLIQUID"
    iid_s = "SOL-USDT-SWAP.OKX"

    # Signal mids (100.00 / 100.20) diverge significantly from actual opening VWAPs (99.90 / 100.60)
    led.create_position(
        position_id="VWAP-1",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=iid_l,
        iid_short=iid_s,
        opened_ts=now_s,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.50"),
    )
    open_orders = [
        ("mk1", "HL", iid_l, "BUY", "MAKER_OPEN", "POST_ONLY", D("0.30"), D("99.50")),
        ("hg1", "OKX", iid_s, "SELL", "HEDGE_OPEN", "IOC", D("0.30"), D("100.80")),
        ("tk2", "HL", iid_l, "BUY", "TAKER_OPEN", "IOC", D("0.20"), D("100.50")),
        ("tm2", "OKX", iid_s, "SELL", "TAKER_OPEN_MARKET_FALLBACK", "MARKET", D("0.20"), D("100.30")),
    ]
    for coid, v, iid, side, role, otype, qty, px in open_orders:
        led.register_order_intent(
            client_order_id=coid,
            position_id="VWAP-1",
            venue=v,
            instrument_id=iid,
            side=side,
            role=role,
            order_type=otype,
            qty_contracts=qty,
            base_qty=qty,
            price=px,
            ts_s=now_s + 1,
        )
        led.update_order_state(coid, status="FILLED", filled_base_qty=qty, fill_price=px, ts_s=now_s + 2)

    # Close both legs at exact opening VWAPs -> realized PnL MUST be 0.0000 USD
    close_orders = [
        ("cl1", "HL", iid_l, "SELL", "MAKER_CLOSE", "POST_ONLY", D("0.50"), D("99.90")),
        ("cs1", "OKX", iid_s, "BUY", "HEDGE_CLOSE", "IOC", D("0.50"), D("100.60")),
    ]
    for coid, v, iid, side, role, otype, qty, px in close_orders:
        led.register_order_intent(
            client_order_id=coid,
            position_id="VWAP-1",
            venue=v,
            instrument_id=iid,
            side=side,
            role=role,
            order_type=otype,
            qty_contracts=qty,
            base_qty=qty,
            price=px,
            ts_s=now_s + 10,
        )
        led.update_order_state(coid, status="FILLED", filled_base_qty=qty, fill_price=px, ts_s=now_s + 11)

    rpnl = led.compute_position_price_pnl("VWAP-1")
    assert rpnl == D("0.0000")
    led.update_position_status(
        "VWAP-1", status="CLOSED", ts_s=now_s + 12, long_base_qty=D("0"), short_base_qty=D("0"), realized_pnl_usd=rpnl
    )
    assert led.daily_stats(now_s + 12)[0] == D("0.0000")

    # Fallback compatibility when no opening fill rows exist in orders
    led.create_position(
        position_id="VWAP-FB",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=iid_l,
        iid_short=iid_s,
        opened_ts=now_s + 20,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.50"),
    )
    led.register_order_intent(
        client_order_id="fbcl",
        position_id="VWAP-FB",
        venue="HL",
        instrument_id=iid_l,
        side="SELL",
        role="EMERGENCY_CLOSE_LONG",
        order_type="MARKET",
        qty_contracts=D("0.50"),
        base_qty=D("0.50"),
        price=None,
        ts_s=now_s + 21,
    )
    led.update_order_state("fbcl", status="FILLED", filled_base_qty=D("0.50"), fill_price=D("101.00"), ts_s=now_s + 22)
    assert led.compute_position_price_pnl("VWAP-FB") == D("0.5000")
    led.close()


def test_cli_mode_override_and_ledger_domain_isolation_protect_live_db(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    from src.carry.runtime import build_live_node, run_node

    live_db = tmp_path / "live.sqlite3"
    paper_db = tmp_path / "paper.sqlite3"

    live_cfg = tmp_path / "carry-live.toml"
    live_text = (REPO_ROOT / "config" / "carry-live.toml").read_text(encoding="utf-8")
    live_text = live_text.replace('state_path = "../reports/carry/live.sqlite3"', f'state_path = "{live_db}"')
    live_text = live_text.replace('record_dir = "../reports/carry/tape-live"', 'record_dir = ""')
    live_cfg.write_text(live_text, encoding="utf-8")

    paper_cfg = tmp_path / "carry-paper.toml"
    paper_text = (REPO_ROOT / "config" / "carry-paper.toml").read_text(encoding="utf-8")
    paper_text = paper_text.replace('state_path = "../reports/carry/paper.sqlite3"', f'state_path = "{paper_db}"')
    paper_text = paper_text.replace('record_dir = "../reports/carry/tape-paper"', 'record_dir = ""')
    paper_cfg.write_text(paper_text, encoding="utf-8")

    live_plan = load_carry_plan(live_cfg)
    paper_plan = load_carry_plan(paper_cfg)
    assert live_plan.fingerprint() != paper_plan.fingerprint()

    # Seed live DB with an OPEN position
    led_live = CarryLedger(live_db, live_plan.fingerprint(), ledger_domain="live")
    led_live.create_position(
        position_id="SOL-LIVE-1",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long="SOL-USD-PERP.HYPERLIQUID",
        iid_short="SOL-USDT-SWAP.OKX",
        opened_ts=1_800_000_000,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("1.0"),
    )
    led_live.update_position_status(
        "SOL-LIVE-1", status="OPEN", ts_s=1_800_000_001, long_base_qty=D("1.0"), short_base_qty=D("1.0")
    )
    led_live.close()

    # Direct cross-domain CarryLedger access is hard-rejected without altering the DB
    with pytest.raises(LedgerOwnershipError):
        CarryLedger(live_db, paper_plan.fingerprint(), ledger_domain="paper")

    # runtime APIs reject cross-domain execution
    with pytest.raises(LedgerOwnershipError):
        run_node(live_plan, mode="paper")
    with pytest.raises(LedgerOwnershipError):
        build_live_node(live_plan, mode="paper")

    # CLI `--config carry-live.toml --mode paper --run` MUST refuse with exit code 2
    rc_cross = carry_cli_main(["--config", str(live_cfg), "--mode", "paper", "--run"])
    assert rc_cross == 2
    assert "cross-domain" in capsys.readouterr().err

    # CLI paper config -> live/readonly MUST refuse with exit code 2
    assert carry_cli_main(["--config", str(paper_cfg), "--mode", "live", "--execute-live"]) == 2
    assert carry_cli_main(["--config", str(paper_cfg), "--mode", "readonly", "--run"]) == 2
    capsys.readouterr()

    # live <-> readonly share the "live" domain cleanly
    assert carry_cli_main(["--config", str(live_cfg), "--mode", "readonly", "--dry-run"]) == 0
    capsys.readouterr()

    # Verify live DB OPEN position & runtime_flags remained completely untouched
    led_check = CarryLedger(live_db, live_plan.fingerprint(), ledger_domain="live")
    pos = led_check.active_positions()["SOL-LIVE-1"]
    assert pos["status"] == "OPEN"
    assert D(pos["long_base_qty"]) == D("1.0") and D(pos["short_base_qty"]) == D("1.0")
    assert led_check.is_halted() == (False, None)
    led_check.close()

    # Paper DB rejects live domain open
    led_paper = CarryLedger(paper_db, paper_plan.fingerprint(), ledger_domain="paper")
    led_paper.close()
    with pytest.raises(LedgerOwnershipError):
        CarryLedger(paper_db, live_plan.fingerprint(), ledger_domain="live")


def test_slow_poll_nonblocking_anti_reentrancy_error_isolation_and_clean_shutdown(tmp_path: Path) -> None:
    import threading
    from types import SimpleNamespace
    from src.carry.runtime import _create_carry_strategy

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "nonblocking.sqlite3", plan.fingerprint())
    strat = _create_carry_strategy(plan, led, mode="paper")
    strat._specs_from_cache = lambda: {}

    entered_evt = threading.Event()
    release_evt = threading.Event()
    fail_next = [False]
    poll_calls = [0]

    def fake_http(url: str, body: dict | None = None):
        if (body or {}).get("type") == "metaAndAssetCtxs":
            if body and body.get("dex") == "xyz":
                return [{"universe": []}, []]
            poll_calls[0] += 1
            if fail_next[0]:
                raise RuntimeError("upstream_timeout")
            entered_evt.set()
            assert release_evt.wait(timeout=5.0)
            return [
                {"universe": [{"name": "SOL"}]},
                [{"markPx": "100", "midPx": "100", "funding": "0.0001", "dayNtlVlm": "5000000"}],
            ]
        return {}

    strat.feed = PublicCarryFeed(venues=("HL",), http_get=fake_http)
    try:
        t0 = time.monotonic()
        strat._on_slow_timer(None)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.2
        assert entered_evt.wait(timeout=2.0)

        # Event loop handlers remain responsive and overlapping slow timer does not pile up
        strat._on_fast_risk_timer(None)
        strat.on_quote(
            SimpleNamespace(
                instrument_id="SOL-USD-PERP.HYPERLIQUID",
                bid_price=100.0,
                ask_price=100.02,
                ts_event=time.time_ns(),
            )
        )
        assert strat.live_quotes["SOL-USD-PERP.HYPERLIQUID"] == (100.0, 100.02)
        strat._on_slow_timer(None)
        assert poll_calls[0] == 1

        release_evt.set()
        deadline = time.monotonic() + 2.0
        while strat._slow_poll_inflight and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not strat._slow_poll_inflight

        strat._on_fast_risk_timer(None)
        assert "SOL" in strat.snapshot
        assert strat._last_slow_poll_error is None

        # Background poll failure is surfaced cleanly without corrupting the last good snapshot
        fail_next[0] = True
        strat.feed = type(
            "_FailFeed",
            (),
            {"poll_snapshot": lambda *_a, **_kw: (_ for _ in ()).throw(RuntimeError("upstream_timeout"))},
        )()
        strat._on_slow_timer(None)
        deadline = time.monotonic() + 2.0
        while strat._slow_poll_inflight and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not strat._slow_poll_inflight

        strat._on_fast_risk_timer(None)
        assert strat._last_slow_poll_error is not None and "upstream_timeout" in strat._last_slow_poll_error
        assert "SOL" in strat.snapshot

        strat.on_stop()
        assert strat._slow_poll_executor is None
        assert not any(t.name.startswith("carry-slow-poll") and t.is_alive() for t in threading.enumerate())
    finally:
        release_evt.set()
        strat.close_slow_poller(wait=True)
        led.close()


def test_on_quote_rejects_stale_ts_event_and_clamps_forward_clock_skew(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    from src.carry.runtime import _create_carry_strategy

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    assert plan.runtime.quote_stale_secs == 15
    led = CarryLedger(tmp_path / "quote_ts.sqlite3", plan.fingerprint())
    strat = _create_carry_strategy(plan, led, mode="paper")

    clock = [1_800_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    iid = "SOL-USDT-SWAP.OKX"
    rest_leg = _make_leg("OKX", "SOL", bid=100.0, ask=100.02)
    strat.snapshot = {"SOL": {"OKX": rest_leg}}

    try:
        # 120s-old quote queued during a stall MUST be rejected and never pollute live_quotes or snapshot
        strat.on_quote(
            SimpleNamespace(
                instrument_id=iid,
                bid_price=90.0,
                ask_price=90.02,
                ts_event=int((clock[0] - 120.0) * 1e9),
            )
        )
        assert iid not in strat.live_quotes
        assert iid not in strat.live_quote_ts
        assert iid not in strat.live_mids
        strat._overlay_fresh_quotes(clock[0])
        assert strat.snapshot["SOL"]["OKX"].bid == 100.0

        # Tiny synthetic ts_event=999 is also treated as stale epoch time and rejected
        strat.on_quote(SimpleNamespace(instrument_id=iid, bid_price=91.0, ask_price=91.02, ts_event=999))
        assert iid not in strat.live_quotes

        # 5s-old quote is accepted with its clamped event timestamp and ages out after >15s total
        strat.on_quote(
            SimpleNamespace(
                instrument_id=iid,
                bid_price=100.5,
                ask_price=100.52,
                ts_event=int((clock[0] - 5.0) * 1e9),
            )
        )
        assert strat.live_quotes[iid] == (100.5, 100.52)
        assert strat.live_quote_ts[iid] == pytest.approx(clock[0] - 5.0)
        strat._overlay_fresh_quotes(clock[0])
        assert strat.snapshot["SOL"]["OKX"].bid == 100.5

        clock[0] += 11.0  # 5s + 11s = 16s > 15s stale window
        strat.snapshot = {"SOL": {"OKX": rest_leg}}
        strat._overlay_fresh_quotes(clock[0])
        assert iid not in strat.live_quotes
        assert strat.snapshot["SOL"]["OKX"].bid == 100.0

        # Forward clock skew (+10s) is clamped to local now_s so it cannot linger beyond quote_stale_secs
        strat.on_quote(
            SimpleNamespace(
                instrument_id=iid,
                bid_price=101.0,
                ask_price=101.02,
                ts_event=int((clock[0] + 10.0) * 1e9),
            )
        )
        assert strat.live_quote_ts[iid] == pytest.approx(clock[0])
        strat._overlay_fresh_quotes(clock[0] + 15.0)
        assert iid in strat.live_quotes
        strat.snapshot = {"SOL": {"OKX": rest_leg}}
        strat._overlay_fresh_quotes(clock[0] + 16.0)
        assert iid not in strat.live_quotes
        assert strat.snapshot["SOL"]["OKX"].bid == 100.0
    finally:
        led.close()


def test_okx_short_interval_funding_rate_from_next_funding_time_and_history_fallback() -> None:
    now_s = 1_800_000_000
    t_ms = (now_s + 1800) * 1000
    nxt_ms = (now_s + 5400) * 1000  # 1h settlement interval

    # Subtest A: explicit fundingTime + nextFundingTime 1h apart -> interval_h=1, current_rate_bps_h=8.0
    okx_bulk = parse_okx_tickers(
        {"data": [{"instId": "FAST-USDT-SWAP", "state": "live", "settleCcy": "USDT"}]},
        {"data": [{"instId": "FAST-USDT-SWAP", "last": "10", "bidPx": "9.99", "askPx": "10.01", "volCcy24h": "200000"}]},
        {"data": [{"instId": "FAST-USDT-SWAP", "fundingRate": "0.0008", "fundingTime": str(t_ms), "nextFundingTime": str(nxt_ms)}]},
        now_s=now_s,
    )
    assert okx_bulk["FAST"].interval_h == 1
    assert okx_bulk["FAST"].current_rate_bps_h == pytest.approx(8.0)

    def http_with_next(url: str, _body: dict | None = None):
        if "public/funding-rate?" in url:
            return {"data": [{"instId": "FAST-USDT-SWAP", "fundingRate": "0.0008", "fundingTime": str(t_ms), "nextFundingTime": str(nxt_ms)}]}
        if "funding-rate-history" in url:
            return {"data": [{"fundingTime": str((now_s - h * HOUR) * 1000), "realizedRate": "0.0008"} for h in range(10)]}
        if "market/candles" in url:
            return {"data": [[str((now_s - 7200) * 1000), "10", "10", "10", "10", "100", "0", "10000"], [str((now_s - 3600) * 1000), "10", "10", "10", "10", "100", "0", "10000"]]}
        return {}

    feed_a = PublicCarryFeed(venues=("OKX",), http_get=http_with_next)
    det_a = feed_a._fetch_leg_detail(_make_leg("OKX", "FAST", interval_h=8, cur_rate=None), now_s)
    assert det_a.interval_h == 1
    assert det_a.current_rate_bps_h == pytest.approx(8.0)

    # Subtest B: missing/empty nextFundingTime -> history infers 1h and recalculates current_rate_bps_h=8.0
    bulk_rate = ["0.0008"]

    def http_missing_next(url: str, _body: dict | None = None):
        if "okx.com" in url:
            if "public/instruments" in url:
                return {"data": [{"instId": "FAST-USDT-SWAP", "state": "live", "settleCcy": "USDT"}]}
            if "market/tickers" in url:
                return {"data": [{"instId": "FAST-USDT-SWAP", "last": "10", "bidPx": "9.99", "askPx": "10.01", "volCcy24h": "200000"}]}
            if "instId=ANY" in url:
                return {"data": [{"instId": "FAST-USDT-SWAP", "fundingRate": bulk_rate[0], "fundingTime": str(t_ms), "nextFundingTime": ""}]}
            if "public/funding-rate?" in url:
                return {"data": [{"instId": "FAST-USDT-SWAP", "fundingRate": bulk_rate[0], "fundingTime": str(t_ms)}]}
            if "funding-rate-history" in url:
                return {"data": [{"fundingTime": str((now_s - h * HOUR) * 1000), "realizedRate": "0.0008"} for h in range(10)]}
            if "market/candles" in url:
                return {"data": [[str((now_s - 7200) * 1000), "10", "10", "10", "10", "100", "0", "10000"], [str((now_s - 3600) * 1000), "10", "10", "10", "10", "100", "0", "10000"]]}
        if "api.bybit.com" in url:
            if "instruments-info" in url:
                return {"result": {"list": [{"symbol": "FASTUSDT", "fundingInterval": "60"}]}}
            if "market/tickers" in url:
                return {"result": {"list": [{"symbol": "FASTUSDT", "markPrice": "10", "bid1Price": "9.99", "ask1Price": "10.01", "fundingRate": "0.0001", "fundingIntervalHour": "1", "nextFundingTime": str(t_ms), "turnover24h": "2000000"}]}}
            if "funding/history" in url:
                return {"result": {"list": [{"fundingRateTimestamp": str((now_s - h * HOUR) * 1000), "fundingRate": "0.0001"} for h in range(10)]}}
            if "market/kline" in url:
                return {"result": {"list": [[str((now_s - 7200) * 1000), "10", "10", "10", "10", "100", "10000"], [str((now_s - 3600) * 1000), "10", "10", "10", "10", "100", "10000"]]}}
        return {}

    feed_b = PublicCarryFeed(venues=("OKX",), http_get=http_missing_next)
    det_b = feed_b._fetch_leg_detail(_make_leg("OKX", "FAST", interval_h=8, cur_rate=None), now_s)
    assert det_b.interval_h == 1
    assert det_b.current_rate_bps_h == pytest.approx(8.0)

    feed_snap = PublicCarryFeed(venues=("OKX", "BYBIT"), http_get=http_missing_next)
    snap1 = feed_snap.poll_snapshot(now_s=now_s)
    assert snap1["FAST"]["OKX"].interval_h == 1
    assert snap1["FAST"]["OKX"].current_rate_bps_h == pytest.approx(8.0)

    # Cache hit on next cycle still normalizes fresh bulk fundingRate by cached 1h interval
    bulk_rate[0] = "0.0012"
    snap2 = feed_snap.poll_snapshot(now_s=now_s + 60)
    assert snap2["FAST"]["OKX"].interval_h == 1
    assert snap2["FAST"]["OKX"].current_rate_bps_h == pytest.approx(12.0)


def test_okx_bybit_40_overlap_symbols_all_covered_with_bulk_and_degraded_fallback() -> None:
    now_s = 1_800_000_000
    syms = [f"SYM{i:02d}" for i in range(40)]

    def _build_http(bulk_funding_empty: bool):
        seen_urls: list[str] = []

        def fake_http(url: str, _body: dict | None = None):
            seen_urls.append(url)
            if "okx.com" in url:
                if "public/instruments" in url:
                    return {"data": [{"instId": f"{s}-USDT-SWAP", "state": "live", "settleCcy": "USDT"} for s in syms]}
                if "market/tickers" in url:
                    return {
                        "data": [
                            {"instId": f"{s}-USDT-SWAP", "last": "10", "bidPx": "9.99", "askPx": "10.01", "volCcy24h": "200000"}
                            for s in syms
                        ]
                    }
                if "instId=ANY" in url:
                    if bulk_funding_empty:
                        return {"data": []}
                    rows = []
                    for i, s in enumerate(syms):
                        # Highest spread on SYM39 (tail alphabetically), lowest on SYM35..SYM38
                        if i == 39:
                            fr = "0.0040"
                        elif 35 <= i <= 38:
                            fr = "0.0001"
                        else:
                            fr = f"{0.0005 + i * 0.00005:.6f}"
                        rows.append({
                            "instId": f"{s}-USDT-SWAP",
                            "fundingRate": fr,
                            "fundingTime": str((now_s + 3600) * 1000),
                            "nextFundingTime": str((now_s + 9 * 3600) * 1000),
                        })
                    return {"data": rows}
                if "public/funding-rate?" in url:
                    return {
                        "data": [
                            {
                                "fundingRate": "0.0008",
                                "fundingTime": str((now_s + 3600) * 1000),
                                "nextFundingTime": str((now_s + 9 * 3600) * 1000),
                            }
                        ]
                    }
                if "funding-rate-history" in url:
                    return {"data": [{"fundingTime": str((now_s - k * 8 * HOUR) * 1000), "realizedRate": "0.0008"} for k in range(4)]}
                if "market/candles" in url:
                    return {"data": [[str((now_s - 7200) * 1000), "10", "10", "10", "10", "100", "0", "10000"], [str((now_s - 3600) * 1000), "10", "10", "10", "10", "100", "0", "10000"]]}
            if "bybit.com" in url:
                if "instruments-info" in url:
                    return {"result": {"list": [{"symbol": f"{s}USDT", "fundingInterval": "480"} for s in syms]}}
                if "market/tickers" in url:
                    return {
                        "result": {
                            "list": [
                                {
                                    "symbol": f"{s}USDT",
                                    "markPrice": "10",
                                    "bid1Price": "9.99",
                                    "ask1Price": "10.01",
                                    "fundingRate": "0.0001",
                                    "fundingIntervalHour": "8",
                                    "nextFundingTime": str((now_s + 3600) * 1000),
                                    "turnover24h": "2000000",
                                }
                                for s in syms
                            ]
                        }
                    }
                if "funding/history" in url:
                    return {"result": {"list": [{"fundingRateTimestamp": str((now_s - k * 8 * HOUR) * 1000), "fundingRate": "0.0001"} for k in range(4)]}}
                if "market/kline" in url:
                    return {"result": {"list": [[str((now_s - 7200) * 1000), "10", "10", "10", "10", "100", "10000"], [str((now_s - 3600) * 1000), "10", "10", "10", "10", "100", "10000"]]}}
            return {}

        return fake_http, seen_urls

    # Subtest A: Normal bulk instId=ANY prioritizes tail-named SYM39 immediately & rotates SYM35..SYM38 in round 2
    http_a, urls_a = _build_http(bulk_funding_empty=False)
    feed_a = PublicCarryFeed(venues=("OKX", "BYBIT"), http_get=http_a, max_detail_refreshes_per_cycle=35)
    snap_a1 = feed_a.poll_snapshot(now_s=now_s)
    assert any("public/funding-rate?instId=ANY" in u for u in urls_a)
    assert not any("public/funding-rate?instId=SYM" in u for u in urls_a)
    assert snap_a1["SYM39"]["OKX"].r8_bps_h is not None and snap_a1["SYM39"]["BYBIT"].r8_bps_h is not None
    assert snap_a1["SYM35"]["OKX"].r8_bps_h is None  # lowest spread deferred to round 2

    snap_a2 = feed_a.poll_snapshot(now_s=now_s + 60)
    for s in syms:
        assert snap_a2[s]["OKX"].r8_bps_h is not None and snap_a2[s]["OKX"].r24_bps_h is not None
        assert snap_a2[s]["BYBIT"].r8_bps_h is not None and snap_a2[s]["BYBIT"].r24_bps_h is not None

    # Even if detail TTL expires between cycles (600s > 300s), unattempted symbols rotate first
    http_a_exp, _ = _build_http(bulk_funding_empty=False)
    feed_a_exp = PublicCarryFeed(venues=("OKX", "BYBIT"), http_get=http_a_exp, max_detail_refreshes_per_cycle=35)
    exp1 = feed_a_exp.poll_snapshot(now_s=now_s)
    skipped_r1 = [s for s in syms if exp1[s]["OKX"].r8_bps_h is None]
    assert len(skipped_r1) == 5
    exp2 = feed_a_exp.poll_snapshot(now_s=now_s + 600)
    for s in skipped_r1:
        assert exp2[s]["OKX"].r8_bps_h is not None and exp2[s]["BYBIT"].r8_bps_h is not None

    # Subtest B: Degraded bulk instId=ANY returning empty -> anti-starvation rotation covers SYM35..SYM39
    http_b, _ = _build_http(bulk_funding_empty=True)
    feed_b = PublicCarryFeed(venues=("OKX", "BYBIT"), http_get=http_b, max_detail_refreshes_per_cycle=35)
    snap_b1 = feed_b.poll_snapshot(now_s=now_s)
    assert all(snap_b1[s]["OKX"].r8_bps_h is not None for s in syms[:35])
    assert all(snap_b1[s]["OKX"].r8_bps_h is None for s in syms[35:])

    snap_b2 = feed_b.poll_snapshot(now_s=now_s + 60)
    for s in syms:
        assert snap_b2[s]["OKX"].r8_bps_h is not None and snap_b2[s]["OKX"].r24_bps_h is not None
        assert snap_b2[s]["BYBIT"].r8_bps_h is not None and snap_b2[s]["BYBIT"].r24_bps_h is not None

    http_b_exp, _ = _build_http(bulk_funding_empty=True)
    feed_b_exp = PublicCarryFeed(venues=("OKX", "BYBIT"), http_get=http_b_exp, max_detail_refreshes_per_cycle=35)
    b_exp1 = feed_b_exp.poll_snapshot(now_s=now_s)
    assert all(b_exp1[s]["OKX"].r8_bps_h is None for s in syms[35:])
    b_exp2 = feed_b_exp.poll_snapshot(now_s=now_s + 600)
    for s in syms[35:]:
        assert b_exp2[s]["OKX"].r8_bps_h is not None and b_exp2[s]["BYBIT"].r8_bps_h is not None


def test_testnet_public_carry_feed_urls_and_okx_demo_header(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import io
    import urllib.request
    from src.carry.funding_feed import default_http_json
    from src.carry.runtime import _create_carry_strategy

    now_s = 1_800_000_000

    with pytest.raises(ValueError, match="unsupported environment"):
        PublicCarryFeed(environment="invalid")

    def _make_recorder_http(calls: list[tuple[str, dict | None, dict | None]]):
        def rec_http(url: str, body: dict | None = None, *, headers: dict | None = None):
            calls.append((url, body, headers))
            if "hyperliquid" in url:
                req_type = (body or {}).get("type")
                if req_type == "metaAndAssetCtxs":
                    return [
                        {"universe": [{"name": "BTC"}]},
                        [{"markPx": "60000", "midPx": "60000", "funding": "0.00001", "dayNtlVlm": "5000000"}],
                    ]
                if req_type == "fundingHistory":
                    return [{"time": (now_s - HOUR) * 1000, "fundingRate": "0.00001"}]
                if req_type == "candleSnapshot":
                    return [
                        {"t": (now_s - 7200) * 1000, "c": "60000", "v": "10"},
                        {"t": (now_s - 3600) * 1000, "c": "60000", "v": "10"},
                    ]
            if "zklighter" in url:
                if "orderBookDetails" in url:
                    return {"order_book_details": [{"symbol": "BTC", "market_id": 1, "market_type": "perp", "status": "active", "is_frozen": False, "mark_price": "60000", "daily_quote_token_volume": "5000000"}]}
                if "funding-rates" in url:
                    return {"funding_rates": [{"exchange": "lighter", "symbol": "BTC", "rate": 0.00008}]}
                if "/api/v1/fundings" in url:
                    return {"fundings": [{"timestamp": now_s - HOUR, "rate": "0.001", "direction": "long"}]}
                if "/api/v1/candles" in url:
                    return {"c": [{"t": now_s - 7200, "c": "60000", "v": "10"}, {"t": now_s - 3600, "c": "60000", "v": "10"}]}
            if "bybit" in url:
                if "instruments-info" in url:
                    return {"result": {"list": [{"symbol": "BTCUSDT", "fundingInterval": "480"}]}}
                if "market/tickers" in url:
                    return {"result": {"list": [{"symbol": "BTCUSDT", "markPrice": "60000", "bid1Price": "59999", "ask1Price": "60001", "fundingRate": "0.0001", "fundingIntervalHour": "8", "nextFundingTime": str((now_s + 3600) * 1000), "turnover24h": "5000000"}]}}
                if "funding/history" in url:
                    return {"result": {"list": [{"fundingRateTimestamp": str((now_s - HOUR) * 1000), "fundingRate": "0.0001"}]}}
                if "market/kline" in url:
                    return {"result": {"list": [[str((now_s - 7200) * 1000), "60000", "60000", "60000", "60000", "10", "600000"], [str((now_s - 3600) * 1000), "60000", "60000", "60000", "60000", "10", "600000"]]}}
            if "okx.com" in url:
                if "public/instruments" in url:
                    return {"data": [{"instId": "BTC-USDT-SWAP", "state": "live", "settleCcy": "USDT"}]}
                if "market/tickers" in url:
                    return {"data": [{"instId": "BTC-USDT-SWAP", "last": "60000", "bidPx": "59999", "askPx": "60001", "volCcy24h": "100"}]}
                if "instId=ANY" in url:
                    return {"data": []}  # force single-leg OKX funding-rate fallback too
                if "public/funding-rate?" in url:
                    return {"data": [{"instId": "BTC-USDT-SWAP", "fundingRate": "0.0001", "fundingTime": str((now_s + 3600) * 1000), "nextFundingTime": str((now_s + 9 * 3600) * 1000)}]}
                if "funding-rate-history" in url:
                    return {"data": [{"fundingTime": str((now_s - HOUR) * 1000), "realizedRate": "0.0001"}]}
                if "market/candles" in url:
                    return {"data": [[str((now_s - 7200) * 1000), "60000", "60000", "60000", "60000", "10", "0", "600000"], [str((now_s - 3600) * 1000), "60000", "60000", "60000", "60000", "10", "0", "600000"]]}
            return {}

        return rec_http

    testnet_calls: list[tuple[str, dict | None, dict | None]] = []
    feed_test = PublicCarryFeed(
        venues=("HL", "LIGHTER", "OKX", "BYBIT"),
        environment="testnet",
        http_get=_make_recorder_http(testnet_calls),
    )
    snap_test = feed_test.poll_snapshot(now_s=now_s)
    assert set(snap_test["BTC"].keys()) == {"HL", "LIGHTER", "OKX", "BYBIT"}

    hl_urls = [u for u, _b, _h in testnet_calls if "hyperliquid" in u]
    lt_urls = [u for u, _b, _h in testnet_calls if "zklighter" in u]
    byb_urls = [u for u, _b, _h in testnet_calls if "bybit" in u]
    okx_calls = [(u, h) for u, _b, h in testnet_calls if "okx.com" in u]
    non_okx_calls = [(u, h) for u, _b, h in testnet_calls if "okx.com" not in u]

    assert hl_urls and all(u == "https://api.hyperliquid-testnet.xyz/info" for u in hl_urls)
    assert not any((b or {}).get("dex") == "xyz" for u, b, _h in testnet_calls if "hyperliquid" in u)
    assert lt_urls and all(u.startswith("https://testnet.zklighter.elliot.ai/") for u in lt_urls)
    assert byb_urls and all(u.startswith("https://api-testnet.bybit.com/") for u in byb_urls)
    assert len(okx_calls) == 6  # instruments, tickers, instId=ANY, single-leg fallback, history, candles
    assert all(u.startswith("https://www.okx.com/") and h == {"x-simulated-trading": "1"} for u, h in okx_calls)
    assert all(h is None for _u, h in non_okx_calls)

    # Mainnet uses production domains and omits x-simulated-trading header on OKX
    mainnet_calls: list[tuple[str, dict | None, dict | None]] = []
    feed_main = PublicCarryFeed(
        venues=("HL", "LIGHTER", "OKX", "BYBIT"),
        environment="mainnet",
        http_get=_make_recorder_http(mainnet_calls),
    )
    feed_main.poll_snapshot(now_s=now_s)
    assert all(u == "https://api.hyperliquid.xyz/info" for u, _b, _h in mainnet_calls if "hyperliquid" in u)
    assert all(u.startswith("https://mainnet.zklighter.elliot.ai/") for u, _b, _h in mainnet_calls if "zklighter" in u)
    assert all(u.startswith("https://api.bybit.com/") for u, _b, _h in mainnet_calls if "bybit" in u)
    assert all(h is None for u, _b, h in mainnet_calls if "okx.com" in u)

    # Verify default_http_json forwards headers onto outgoing urllib.request.Request
    captured_reqs: list[urllib.request.Request] = []

    class _FakeResp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            self.close()

    def fake_urlopen(req: urllib.request.Request, timeout: float = 0.0):
        captured_reqs.append(req)
        return _FakeResp(b'{"code":"0","data":[]}')

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    default_http_json("https://www.okx.com/api/v5/public/funding-rate?instId=ANY", None, headers={"x-simulated-trading": "1"})
    assert len(captured_reqs) == 1
    hdrs = {k.lower(): v for k, v in captured_reqs[0].header_items()}
    assert hdrs["x-simulated-trading"] == "1"

    # Verify runtime CarryStrategy propagates plan.environment to PublicCarryFeed
    test_cfg = tmp_path / "carry-testnet.toml"
    test_text = (REPO_ROOT / "config" / "carry-paper.toml").read_text(encoding="utf-8")
    test_text = test_text.replace('environment = "mainnet"', 'environment = "testnet"')
    test_text = test_text.replace('state_path = "../reports/carry/paper.sqlite3"', f'state_path = "{tmp_path / "t.sqlite3"}"')
    test_text = test_text.replace('record_dir = "../reports/carry/tape-paper"', 'record_dir = ""')
    test_cfg.write_text(test_text, encoding="utf-8")
    test_plan = load_carry_plan(test_cfg)
    led = CarryLedger(test_plan.state_path, test_plan.fingerprint())
    try:
        strat = _create_carry_strategy(test_plan, led, mode="paper")
        assert strat.feed.environment == "testnet"
    finally:
        led.close()


def test_graceful_shutdown_refuses_stop_with_naked_leg_and_flattens_or_halts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    import src.carry.runtime as carry_runtime

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.15}
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]

    # Case A: Maker fills 0.49 on shutdown cancel -> IOC hedge & fallback market both rejected ->
    # MUST NOT fire stop_cb; must flatten naked leg via reduce_only emergency close, then stop cleanly.
    clock = [float(now_s)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    led_a = CarryLedger(tmp_path / "sd_naked_a.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw_a = DelayedAsyncVenueGateway()
    gw_a.register_spec(spec_l)
    gw_a.register_spec(spec_s)
    strat_a = carry_runtime._create_carry_strategy(plan, led_a, mode="paper")
    strat_a.gateway = gw_a
    strat_a.controller.gateway = gw_a
    strat_a.reconciled = True
    strat_a.live_mids = dict(mids)
    strat_a.live_quotes = dict(quotes)
    strat_a._specs_from_cache = lambda: specs

    pid_a, err_a = strat_a.controller.start_entry(cand, specs=specs, now_s=now_s)
    assert err_a is None and pid_a is not None
    mk_a = gw_a.submitted[0]["client_order_id"]

    stop_calls_a: list[str] = []
    strat_a.request_graceful_shutdown(stop_cb=lambda: stop_calls_a.append("stopped"), drain_timeout_secs=60.0)
    strat_a._on_fast_risk_timer(None)
    assert gw_a.cancel_requests == [mk_a]

    clock[0] += 1.0
    gw_a.deliver_cancel(mk_a, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    strat_a.on_order_canceled(SimpleNamespace(client_order_id=mk_a))
    hg_a = gw_a.submitted[1]["client_order_id"]
    gw_a.deliver_reject(hg_a, reason="ioc_rejected")
    strat_a.on_order_rejected(SimpleNamespace(client_order_id=hg_a))
    hm_a = gw_a.submitted[2]["client_order_id"]
    gw_a.deliver_reject(hm_a, reason="market_fallback_rejected")
    strat_a.on_order_rejected(SimpleNamespace(client_order_id=hm_a))

    # 1s into 60s drain: stop_cb MUST NOT fire; shutdown_unhedged_flatten MUST submit reduce-only SELL 0.49
    assert stop_calls_a == []
    assert len(gw_a.submitted) == 4
    em_a = gw_a.submitted[3]
    assert em_a["side"] == "SELL" and em_a["reduce_only"] is True and em_a["base_qty"] == D("0.49")

    # Once flatten fills, position settles CLOSED, stop_cb fires, and cold-start reconcile passes
    gw_a.deliver_fill(em_a["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=D("99.97"))
    strat_a.on_order_filled(SimpleNamespace(client_order_id=em_a["client_order_id"]))
    assert stop_calls_a == ["stopped"]
    assert led_a.active_positions() == {}
    assert led_a.reconcile_startup(venue_positions={}, venue_open_orders={}, ts_s=now_s + 5).ok is True
    led_a.close()

    # Case B: Even if flatten keeps rejecting across retries, it never exits silently with an unflagged naked leg;
    # it escalates to HALTED_RECONCILE with manual takeover audit event.
    clock[0] = float(now_s + 100)
    led_b = CarryLedger(tmp_path / "sd_naked_b.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw_b = DelayedAsyncVenueGateway()
    gw_b.register_spec(spec_l)
    gw_b.register_spec(spec_s)
    strat_b = carry_runtime._create_carry_strategy(plan, led_b, mode="paper")
    strat_b.gateway = gw_b
    strat_b.controller.gateway = gw_b
    strat_b.reconciled = True
    strat_b.live_mids = dict(mids)
    strat_b.live_quotes = dict(quotes)
    strat_b._specs_from_cache = lambda: specs

    pid_b, _ = strat_b.controller.start_entry(cand, specs=specs, now_s=int(clock[0]))
    assert pid_b is not None
    mk_b = gw_b.submitted[0]["client_order_id"]
    stop_calls_b: list[str] = []
    strat_b.request_graceful_shutdown(stop_cb=lambda: stop_calls_b.append("stopped"), drain_timeout_secs=60.0)
    strat_b._on_fast_risk_timer(None)
    gw_b.deliver_cancel(mk_b, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
    strat_b.on_order_canceled(SimpleNamespace(client_order_id=mk_b))
    gw_b.deliver_reject(gw_b.submitted[1]["client_order_id"], reason="ioc_rej")
    strat_b.on_order_rejected(SimpleNamespace(client_order_id=gw_b.submitted[1]["client_order_id"]))
    gw_b.deliver_reject(gw_b.submitted[2]["client_order_id"], reason="mkt_rej")
    strat_b.on_order_rejected(SimpleNamespace(client_order_id=gw_b.submitted[2]["client_order_id"]))

    # Reject initial flatten + 3 bounded backoff retries (2s, 5s, 10s)
    for delay in (0, 2, 5, 10):
        clock[0] += delay
        strat_b._on_fast_risk_timer(None)
        last_em = gw_b.submitted[-1]["client_order_id"]
        gw_b.deliver_reject(last_em, reason="venue_reject")
        strat_b.on_order_rejected(SimpleNamespace(client_order_id=last_em))
    assert stop_calls_b == ["stopped"]
    assert led_b.is_reconcile_halted() == (True, f"emergency_close_failed:{pid_b}")
    ev_kinds = [r["kind"] for r in led_b.conn.execute("SELECT kind FROM events").fetchall()]
    assert "emergency_close_manual_takeover" in ev_kinds
    led_b.close()


def test_graceful_shutdown_finalizes_zero_fill_opening_and_balanced_transitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    import src.carry.runtime as carry_runtime

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.15}
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]

    # 1) Zero-fill OPENING maker canceled during shutdown -> CLOSED (shutdown_aborted) + clean restart
    led1 = CarryLedger(tmp_path / "sd_zero.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw1 = DelayedAsyncVenueGateway()
    gw1.register_spec(spec_l)
    gw1.register_spec(spec_s)
    strat1 = carry_runtime._create_carry_strategy(plan, led1, mode="paper")
    strat1.gateway = gw1
    strat1.controller.gateway = gw1
    strat1.reconciled = True
    strat1.live_mids = dict(mids)
    strat1.live_quotes = dict(quotes)
    strat1._specs_from_cache = lambda: specs
    pid1, _ = strat1.controller.start_entry(cand, specs=specs, now_s=now_s)
    assert pid1 is not None
    mk1 = gw1.submitted[0]["client_order_id"]

    calls1: list[str] = []
    strat1.request_graceful_shutdown(stop_cb=lambda: calls1.append("stopped"), drain_timeout_secs=60.0)
    strat1._on_fast_risk_timer(None)
    assert calls1 == []
    gw1.deliver_cancel(mk1, filled_base_qty=D("0"))
    strat1.on_order_canceled(SimpleNamespace(client_order_id=mk1))
    assert calls1 == ["stopped"]
    assert led1.active_positions() == {}
    row1 = dict(led1.conn.execute("SELECT status, close_reason FROM positions WHERE position_id=?", (pid1,)).fetchone())
    assert row1 == {"status": "CLOSED", "close_reason": "shutdown_aborted"}
    assert led1.reconcile_startup(venue_positions={}, venue_open_orders={}, ts_s=now_s + 5).ok is True
    led1.close()

    # 2) Balanced partial OPENING (0.49/0.49) & balanced normal CLOSING (carry_decay) -> both settle to OPEN
    led2 = CarryLedger(tmp_path / "sd_bal.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw2 = DelayedAsyncVenueGateway()
    gw2.register_spec(spec_l)
    gw2.register_spec(spec_s)
    strat2 = carry_runtime._create_carry_strategy(plan, led2, mode="paper")
    strat2.gateway = gw2
    strat2.controller.gateway = gw2
    strat2.reconciled = True
    strat2.live_mids = dict(mids)
    strat2.live_quotes = dict(quotes)
    strat2._specs_from_cache = lambda: specs

    # Balanced partial OPENING 0.49/0.49 (target 0.98)
    led2.create_position(
        position_id="PART-OPEN",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s,
        entry_l=100.0,
        entry_s=100.15,
        entry_basis_bps=15.0,
        target_base_qty=D("0.98"),
    )
    led2.update_position_status("PART-OPEN", status="OPENING", ts_s=now_s + 1, long_base_qty=D("0.49"), short_base_qty=D("0.49"))
    # Balanced CLOSING (carry_decay) with active MAKER_CLOSE that gets 0-fill canceled during shutdown
    led2.create_position(
        position_id="CLOSE-PART",
        symbol="ETH",
        long_venue="HL",
        short_venue="OKX",
        iid_long="ETH-USD-PERP.HYPERLIQUID",
        iid_short="ETH-USDT-SWAP.OKX",
        opened_ts=now_s - 7200,
        entry_l=2000.0,
        entry_s=2002.0,
        entry_basis_bps=10.0,
        target_base_qty=D("0.05"),
    )
    led2.update_position_status("CLOSE-PART", status="OPEN", ts_s=now_s - 7199, long_base_qty=D("0.05"), short_base_qty=D("0.05"))
    eth_l = InstrumentSpec("ETH-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.001"), price_increment=D("0.01"))
    eth_s = InstrumentSpec("ETH-USDT-SWAP.OKX", "OKX", size_increment=D("0.01"), contract_multiplier=D("0.01"), price_increment=D("0.01"))
    specs2 = {**specs, eth_l.instrument_id: eth_l, eth_s.instrument_id: eth_s}
    gw2.register_spec(eth_l)
    gw2.register_spec(eth_s)
    strat2._specs_from_cache = lambda: specs2
    strat2.live_mids[eth_l.instrument_id] = 2000.0
    strat2.live_mids[eth_s.instrument_id] = 2002.0
    strat2.live_quotes[eth_l.instrument_id] = (1999.8, 2000.2)
    strat2.live_quotes[eth_s.instrument_id] = (2001.8, 2002.2)
    assert strat2.controller.start_sliced_close(
        "CLOSE-PART", specs=specs2, quotes=strat2.live_quotes, mids=strat2.live_mids, reason="carry_decay", now_s=now_s + 2
    ) is True
    submitted_coids = [rec["client_order_id"] for rec in gw2.submitted]

    calls2: list[str] = []
    strat2.request_graceful_shutdown(stop_cb=lambda: calls2.append("stopped"), drain_timeout_secs=60.0)
    strat2._on_fast_risk_timer(None)
    assert calls2 == []
    for coid in submitted_coids:
        gw2.deliver_cancel(coid, filled_base_qty=D("0"))
        strat2.on_order_canceled(SimpleNamespace(client_order_id=coid))
    assert calls2 == ["stopped"]
    assert led2.active_positions()["PART-OPEN"]["status"] == "OPEN"
    assert led2.active_positions()["CLOSE-PART"]["status"] == "OPEN"
    assert led2.reconcile_startup(
        venue_positions={
            ("HL", spec_l.instrument_id): D("0.49"),
            ("OKX", spec_s.instrument_id): D("-0.49"),
            ("HL", eth_l.instrument_id): D("0.05"),
            ("OKX", eth_s.instrument_id): D("-0.05"),
        },
        venue_open_orders={},
        ts_s=now_s + 5,
    ).ok is True
    led2.close()


def test_emergency_close_failure_retains_task_retries_with_backoff_and_escalates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.carry.runtime as carry_runtime

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.20}
    now_s = 1_800_000_000

    # Subcase A: Long 0.99 / Short 0.49 -> unhedged_timeout_kill (HALTED_KILL) ->
    # Long fills 0.50 then CANCELED, Short REJECTED -> remaining 0.49 / 0.49 ->
    # task MUST stay active across 10m under HALTED_KILL and succeed when backoff retries fill.
    clock = [float(now_s)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    led_a = CarryLedger(tmp_path / "em_retry_a.sqlite3", plan.fingerprint(), ledger_domain="live")
    led_a.create_position(
        position_id="SOL-KILL-A",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s - 60,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.99"),
    )
    led_a.update_position_status(
        "SOL-KILL-A", status="OPENING", ts_s=now_s - 50, long_base_qty=D("0.99"), short_base_qty=D("0.49")
    )
    gw_a = DelayedAsyncVenueGateway()
    gw_a.register_spec(spec_l)
    gw_a.register_spec(spec_s)
    strat_a = carry_runtime._create_carry_strategy(plan, led_a, mode="live")
    strat_a.gateway = gw_a
    strat_a.controller.gateway = gw_a
    strat_a.reconciled = True
    strat_a.live_mids = dict(mids)
    strat_a._specs_from_cache = lambda: specs

    # Unhedged timeout hits at t + 10s -> sets HALTED_KILL and submits initial emergency close legs
    strat_a._on_fast_risk_timer(None)
    clock[0] += 10.0
    strat_a._on_fast_risk_timer(None)
    assert led_a.is_kill_halted() == (True, "unhedged_timeout_kill")
    assert len(gw_a.submitted) == 2
    em_l1 = next(o["client_order_id"] for o in gw_a.submitted if o["side"] == "SELL")
    em_s1 = next(o["client_order_id"] for o in gw_a.submitted if o["side"] == "BUY")

    # Long closes 0.50 then CANCELED, Short REJECTED -> remaining 0.49 / 0.49
    gw_a.deliver_cancel(em_l1, filled_base_qty=D("0.50"), avg_fill_price=D("100.00"))
    gw_a.deliver_reject(em_s1, reason="okx_rejected")
    strat_a._on_fast_risk_timer(None)
    pos_rem = led_a.active_positions()["SOL-KILL-A"]
    assert pos_rem["status"] == "CLOSING"
    assert D(pos_rem["long_base_qty"]) == D("0.49") and D(pos_rem["short_base_qty"]) == D("0.49")
    assert "SOL-KILL-A" in strat_a.controller._active_emergency_closes
    assert "SOL-KILL-A" not in strat_a.fast_risk._unhedged_since

    # 1s later (< 2s backoff): still retained, no premature retry spam
    clock[0] += 1.0
    strat_a._on_fast_risk_timer(None)
    assert len(gw_a.submitted) == 2
    assert "SOL-KILL-A" in strat_a.controller._active_emergency_closes

    # At +2s backoff: controller retries both remaining 0.49 legs despite HALTED_KILL -> fills -> CLOSED!
    clock[0] += 1.0
    strat_a._on_fast_risk_timer(None)
    assert len(gw_a.submitted) == 4
    for rec in gw_a.submitted[2:]:
        assert rec["reduce_only"] is True and rec["base_qty"] == D("0.49")
        gw_a.deliver_fill(rec["client_order_id"], filled_base_qty=D("0.49"), avg_fill_price=D("100.10"))
    strat_a._on_fast_risk_timer(None)
    assert led_a.active_positions() == {}
    assert "SOL-KILL-A" not in strat_a.controller._active_emergency_closes
    led_a.close()

    # Subcase B: Repeated rejects across all backoff attempts escalate cleanly to HALTED_RECONCILE
    clock[0] = float(now_s + 500)
    led_b = CarryLedger(tmp_path / "em_retry_b.sqlite3", plan.fingerprint(), ledger_domain="live")
    led_b.create_position(
        position_id="SOL-KILL-B",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s + 450,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.99"),
    )
    led_b.update_position_status(
        "SOL-KILL-B", status="OPENING", ts_s=now_s + 460, long_base_qty=D("0.99"), short_base_qty=D("0.49")
    )
    gw_b = DelayedAsyncVenueGateway()
    gw_b.register_spec(spec_l)
    gw_b.register_spec(spec_s)
    strat_b = carry_runtime._create_carry_strategy(plan, led_b, mode="live")
    strat_b.gateway = gw_b
    strat_b.controller.gateway = gw_b
    strat_b.reconciled = True
    strat_b.live_mids = dict(mids)
    strat_b._specs_from_cache = lambda: specs

    strat_b._on_fast_risk_timer(None)
    clock[0] += 10.0
    strat_b._on_fast_risk_timer(None)
    assert len(gw_b.submitted) == 2
    gw_b.deliver_cancel(gw_b.submitted[0]["client_order_id"], filled_base_qty=D("0.50"), avg_fill_price=D("100.00"))
    gw_b.deliver_reject(gw_b.submitted[1]["client_order_id"], reason="reject")
    strat_b._on_fast_risk_timer(None)

    for wait_s in (2, 5, 10):
        clock[0] += wait_s
        strat_b._on_fast_risk_timer(None)
        for rec in gw_b.submitted[-2:]:
            gw_b.deliver_reject(rec["client_order_id"], reason="reject")
        strat_b._on_fast_risk_timer(None)

    assert led_b.is_reconcile_halted() == (True, "emergency_close_failed:SOL-KILL-B")
    assert "SOL-KILL-B" not in strat_b.controller._active_emergency_closes
    assert led_b.active_positions()["SOL-KILL-B"]["status"] == "CLOSING"
    ev_kinds = [r["kind"] for r in led_b.conn.execute("SELECT kind FROM events").fetchall()]
    assert "emergency_close_manual_takeover" in ev_kinds
    count_after = len(gw_b.submitted)
    clock[0] += 600.0
    strat_b._on_fast_risk_timer(None)
    assert len(gw_b.submitted) == count_after
    led_b.close()


def test_run_node_routes_sigint_and_sigterm_to_graceful_shutdown_via_hosted_run_async(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import asyncio
    import os
    import signal
    import threading
    from types import SimpleNamespace
    from nautilus_trader.live import LiveNode
    import src.carry.runtime as carry_runtime

    assert hasattr(LiveNode, "run_async")

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.13, 100.17)}
    mids = {spec_l.instrument_id: 100.00, spec_s.instrument_id: 100.15}
    now_s = 1_800_000_000
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    cand = evaluate_entry_candidates(
        snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=now_s
    )[0]

    for sig in (signal.SIGTERM, signal.SIGINT):
        led = CarryLedger(tmp_path / f"hosted_{sig.name}.sqlite3", plan.fingerprint(), ledger_domain="paper")
        gw = DelayedAsyncVenueGateway()
        gw.register_spec(spec_l)
        gw.register_spec(spec_s)
        strat = carry_runtime._create_carry_strategy(plan, led, mode="paper")
        strat.gateway = gw
        strat.controller.gateway = gw
        strat.reconciled = True
        strat.live_mids = dict(mids)
        strat.live_quotes = dict(quotes)
        strat._specs_from_cache = lambda: specs
        pid, _ = strat.controller.start_entry(cand, specs=specs, now_s=now_s)
        assert pid is not None
        mk_coid = gw.submitted[0]["client_order_id"]

        events: list[str] = []

        class FakeAsyncNode:
            def __init__(self) -> None:
                self._stop_evt: asyncio.Event | None = None
                self._loop: asyncio.AbstractEventLoop | None = None

            def handle(self):
                node_ref = self

                class _H:
                    def stop(self) -> None:
                        events.append("handle.stop")
                        if node_ref._loop is not None and node_ref._stop_evt is not None:
                            node_ref._loop.call_soon_threadsafe(node_ref._stop_evt.set)

                return _H()

            async def run_async(self) -> None:
                self._loop = asyncio.get_running_loop()
                self._stop_evt = asyncio.Event()
                # Drive real signal -> drain -> racing maker fill -> IOC hedge -> clean handle.stop()
                self._loop.call_later(0.02, lambda: os.kill(os.getpid(), sig))

                async def _drive_drain() -> None:
                    while not strat._shutdown_requested.is_set():
                        await asyncio.sleep(0.01)
                    events.append("graceful_requested")
                    strat._on_fast_risk_timer(None)
                    assert gw.cancel_requests == [mk_coid]
                    assert "handle.stop" not in events
                    # Strategy is still running inside run_async so order events still dispatch
                    gw.deliver_cancel(mk_coid, filled_base_qty=D("0.49"), avg_fill_price=D("99.98"))
                    strat.on_order_canceled(SimpleNamespace(client_order_id=mk_coid))
                    hg_coid = gw.submitted[1]["client_order_id"]
                    assert "handle.stop" not in events
                    gw.deliver_fill(hg_coid, filled_base_qty=D("0.49"), avg_fill_price=D("100.13"))
                    strat.on_order_filled(SimpleNamespace(client_order_id=hg_coid))

                driver = asyncio.create_task(_drive_drain())
                await asyncio.wait_for(self._stop_evt.wait(), timeout=5.0)
                await driver

        node = FakeAsyncNode()
        db_path = tmp_path / f"hosted_{sig.name}.sqlite3"
        monkeypatch.setattr(carry_runtime, "build_live_node", lambda _plan, mode: (node, led, strat))
        rc = carry_runtime.run_node(plan, mode="paper", duration_secs=0)
        assert rc == 0
        assert events == ["graceful_requested", "handle.stop"]
        led_verify = CarryLedger(db_path, plan.fingerprint(), ledger_domain="paper")
        pos = led_verify.active_positions()[pid]
        assert pos["status"] == "OPEN"
        assert D(pos["long_base_qty"]) == D("0.49") and D(pos["short_base_qty"]) == D("0.49")
        assert led_verify.reconcile_startup(
            venue_positions={
                ("HL", spec_l.instrument_id): D("0.49"),
                ("OKX", spec_s.instrument_id): D("-0.49"),
            },
            venue_open_orders={},
            ts_s=now_s + 5,
        ).ok is True
        led_verify.close()

    # Second SIGINT during an active drain escalates immediately to handle.stop()
    led2 = CarryLedger(tmp_path / "hosted_double_sigint.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw2 = DelayedAsyncVenueGateway()
    gw2.register_spec(spec_l)
    gw2.register_spec(spec_s)
    strat2 = carry_runtime._create_carry_strategy(plan, led2, mode="paper")
    strat2.gateway = gw2
    strat2.controller.gateway = gw2
    strat2.reconciled = True
    strat2.live_mids = dict(mids)
    strat2.live_quotes = dict(quotes)
    strat2._specs_from_cache = lambda: specs
    strat2.controller.start_entry(cand, specs=specs, now_s=now_s)

    stop_log: list[str] = []

    class DoubleSigNode:
        def __init__(self) -> None:
            self._stop_evt: asyncio.Event | None = None
            self._loop: asyncio.AbstractEventLoop | None = None

        def handle(self):
            node_ref = self

            class _H:
                def stop(self) -> None:
                    stop_log.append("handle.stop")
                    if node_ref._loop is not None and node_ref._stop_evt is not None:
                        node_ref._loop.call_soon_threadsafe(node_ref._stop_evt.set)

            return _H()

        async def run_async(self) -> None:
            self._loop = asyncio.get_running_loop()
            self._stop_evt = asyncio.Event()
            self._loop.call_later(0.02, lambda: os.kill(os.getpid(), signal.SIGINT))
            self._loop.call_later(0.06, lambda: os.kill(os.getpid(), signal.SIGINT))
            await asyncio.wait_for(self._stop_evt.wait(), timeout=5.0)

    monkeypatch.setattr(carry_runtime, "build_live_node", lambda _plan, mode: (DoubleSigNode(), led2, strat2))
    rc2 = carry_runtime.run_node(plan, mode="paper", duration_secs=0)
    assert rc2 == 0
    assert strat2._shutdown_requested.is_set()
    assert stop_log == ["handle.stop"]
    led2.close()


def test_slow_poll_account_guards_do_not_reset_unhedged_risk_timer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import src.carry.runtime as carry_runtime

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    assert plan.risk.max_unhedged_secs == 10
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    now_s = 1_800_000_000
    clock = [float(now_s)]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    led = CarryLedger(tmp_path / "slow_timer_preserve.sqlite3", plan.fingerprint(), ledger_domain="live")
    led.create_position(
        position_id="SOL-NAKED-49",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s,
        entry_l=100.0,
        entry_s=100.2,
        entry_basis_bps=20.0,
        target_base_qty=D("0.98"),
    )
    led.update_position_status(
        "SOL-NAKED-49", status="OPENING", ts_s=now_s, long_base_qty=D("0.49"), short_base_qty=D("0")
    )

    gw = DelayedAsyncVenueGateway()
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    strat = carry_runtime._create_carry_strategy(plan, led, mode="live")
    strat.gateway = gw
    strat.controller.gateway = gw
    strat.reconciled = True
    strat.live_mids = {spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2}
    strat.live_quotes = {spec_l.instrument_id: (99.98, 100.02), spec_s.instrument_id: (100.18, 100.22)}
    strat._specs_from_cache = lambda: specs

    # t = 0: start 10s unhedged exposure timer (~$49 naked leg)
    strat._on_fast_risk_timer(None)
    assert strat.fast_risk._unhedged_since.get("SOL-NAKED-49") == now_s
    assert gw.submitted == []

    # t = 9s: slow snapshot runs account-level daily guards -> MUST NOT clear _unhedged_since
    clock[0] = float(now_s + 9)
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.18, ask=100.22),
        }
    }
    strat._apply_slow_snapshot(snap)
    assert strat.fast_risk._unhedged_since.get("SOL-NAKED-49") == now_s
    assert not led.is_kill_halted()[0]

    # t = 10s: fast risk timer MUST trigger unhedged_timeout_kill immediately (not delayed to t = 20s)
    clock[0] = float(now_s + 10)
    strat._on_fast_risk_timer(None)
    assert led.is_kill_halted() == (True, "unhedged_timeout_kill")
    assert len(gw.submitted) == 1
    assert gw.submitted[0]["side"] == "SELL" and gw.submitted[0]["reduce_only"] is True and gw.submitted[0]["base_qty"] == D("0.49")
    led.close()


def test_basis_stop_and_margin_guard_do_not_fire_on_stale_frozen_mids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace
    import src.carry.runtime as carry_runtime

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    now_s = 1_800_000_000
    clock = [float(now_s)]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def _setup_strat(name: str):
        led = CarryLedger(tmp_path / f"{name}.sqlite3", plan.fingerprint(), ledger_domain="live")
        led.create_position(
            position_id="SOL-BASIS-1",
            symbol="SOL",
            long_venue="HL",
            short_venue="OKX",
            iid_long=spec_l.instrument_id,
            iid_short=spec_s.instrument_id,
            opened_ts=now_s - 600,
            entry_l=100.0,
            entry_s=100.1,
            entry_basis_bps=10.0,
            target_base_qty=D("0.98"),
        )
        led.update_position_status(
            "SOL-BASIS-1", status="OPEN", ts_s=now_s - 590, long_base_qty=D("0.98"), short_base_qty=D("0.98")
        )
        gw = DelayedAsyncVenueGateway()
        gw.register_spec(spec_l)
        gw.register_spec(spec_s)
        strat = carry_runtime._create_carry_strategy(plan, led, mode="live")
        strat.gateway = gw
        strat.controller.gateway = gw
        strat.reconciled = True
        strat._specs_from_cache = lambda: specs
        return led, gw, strat

    # Subcase A: Last quotes 100 / 103 at t0, then NO WS or REST updates for 35 minutes
    led_a, gw_a, strat_a = _setup_strat("basis_both_frozen")
    strat_a.on_quote(SimpleNamespace(instrument_id=spec_l.instrument_id, bid_price=99.99, ask_price=100.01, ts_event=int(clock[0] * 1e9)))
    strat_a.on_quote(SimpleNamespace(instrument_id=spec_s.instrument_id, bid_price=102.99, ask_price=103.01, ts_event=int(clock[0] * 1e9)))
    for step in range(1, 211):
        clock[0] = float(now_s + step * 10)
        strat_a._prune_stale_quotes(clock[0])
        strat_a._on_basis_timer(None)
    assert gw_a.submitted == []
    assert "SOL-BASIS-1" in led_a.active_positions()
    led_a.close()

    # Subcase B: Single-leg outage (long keeps updating, short frozen at 103) -> MUST NOT trigger
    clock[0] = float(now_s)
    led_b, gw_b, strat_b = _setup_strat("basis_one_frozen")
    strat_b.on_quote(SimpleNamespace(instrument_id=spec_l.instrument_id, bid_price=99.99, ask_price=100.01, ts_event=int(clock[0] * 1e9)))
    strat_b.on_quote(SimpleNamespace(instrument_id=spec_s.instrument_id, bid_price=102.99, ask_price=103.01, ts_event=int(clock[0] * 1e9)))
    for step in range(1, 211):
        clock[0] = float(now_s + step * 10)
        strat_b.on_quote(SimpleNamespace(instrument_id=spec_l.instrument_id, bid_price=99.99, ask_price=100.01, ts_event=int(clock[0] * 1e9)))
        strat_b._on_basis_timer(None)
    assert gw_b.submitted == []
    led_b.close()

    # Subcase C: Both legs continue delivering fresh 100 / 103 updates for 30 minutes -> triggers basis_stop_30m
    clock[0] = float(now_s)
    led_c, gw_c, strat_c = _setup_strat("basis_both_fresh")
    for step in range(0, 182):
        clock[0] = float(now_s + step * 10)
        strat_c.on_quote(SimpleNamespace(instrument_id=spec_l.instrument_id, bid_price=99.99, ask_price=100.01, ts_event=int(clock[0] * 1e9)))
        strat_c.on_quote(SimpleNamespace(instrument_id=spec_s.instrument_id, bid_price=102.99, ask_price=103.01, ts_event=int(clock[0] * 1e9)))
        strat_c._on_basis_timer(None)
    assert len(gw_c.submitted) == 2
    assert all(o["reduce_only"] is True for o in gw_c.submitted)
    assert led_c.active_positions()["SOL-BASIS-1"]["close_reason"] == "basis_stop_30m"
    led_c.close()

    # Subcase D: Stale mid (50.0 from 120s ago) does NOT fire spurious margin_guard_25pct
    clock[0] = float(now_s)
    led_d, gw_d, strat_d = _setup_strat("stale_margin")
    strat_d.live_mids = {spec_l.instrument_id: 50.0, spec_s.instrument_id: 100.1}
    strat_d.live_mid_ts = {spec_l.instrument_id: float(now_s - 120), spec_s.instrument_id: float(now_s)}
    strat_d._on_fast_risk_timer(None)
    assert gw_d.submitted == []
    led_d.close()


def test_stale_slow_snapshot_and_past_settlement_are_rejected_without_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import MappingProxyType, SimpleNamespace
    import src.carry.runtime as carry_runtime

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    now_s = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: float(now_s))

    led = CarryLedger(tmp_path / "stale_slow.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw = DelayedAsyncVenueGateway()
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    strat = carry_runtime._create_carry_strategy(plan, led, mode="paper")
    strat.gateway = gw
    strat.controller.gateway = gw
    strat.reconciled = True
    strat._specs_from_cache = lambda: specs

    # Fresh WS quotes at now_s
    strat.on_quote(SimpleNamespace(instrument_id=spec_l.instrument_id, bid_price=99.98, ask_price=100.02, ts_event=now_s * 1_000_000_000))
    strat.on_quote(SimpleNamespace(instrument_id=spec_s.instrument_id, bid_price=100.13, ask_price=100.17, ts_event=now_s * 1_000_000_000))

    # 20-minute-old slow poll result with settlement timestamp already in the past
    old_ts = now_s - 1200
    stale_snap = MappingProxyType(
        {
            "SOL": MappingProxyType(
                {
                    "HL": LegMarketInfo(**{**_make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02).__dict__, "fetched_ts": old_ts}),
                    "OKX": LegMarketInfo(**{**_make_leg("OKX", "SOL", interval_h=8, next_settle_ts=old_ts + 600, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17).__dict__, "fetched_ts": old_ts}),
                }
            )
        }
    )
    strat._pending_slow_result = carry_runtime._SlowPollResult(poll_ts_s=old_ts, snapshot=stale_snap)
    assert strat._consume_pending_slow_snapshot() is False
    assert strat._last_slow_poll_error == "stale_slow_snapshot:age=1200s"
    assert strat.snapshot == {}
    assert gw.submitted == []
    assert led.active_positions() == {}

    # Also verify signal blackout directly blocks CEX legs with past next_settle_ts
    assert (
        evaluate_entry_candidates(
            stale_snap,
            open_symbols=set(),
            venue_notional_used={},
            excluded_symbols=set(),
            policy=plan.policy,
            risk=plan.risk,
            now_s=now_s,
        )
        == []
    )
    led.close()


def test_okx_6h_funding_interval_realtime_and_history_parsing() -> None:
    now_s = 1_800_000_000
    t_ms = (now_s + 3600) * 1000
    nxt_ms = (now_s + 7 * 3600) * 1000  # 6h settlement interval

    # Realtime bulk parsing: 0.0006 over 6h == 6 bps / 6h == 1.0 bps/h (NOT 0.75 bps/h!)
    okx_6h = parse_okx_tickers(
        {"data": [{"instId": "SIX-USDT-SWAP", "state": "live", "settleCcy": "USDT"}]},
        {"data": [{"instId": "SIX-USDT-SWAP", "last": "10", "bidPx": "9.99", "askPx": "10.01", "volCcy24h": "200000"}]},
        {"data": [{"instId": "SIX-USDT-SWAP", "fundingRate": "0.0006", "fundingTime": str(t_ms), "nextFundingTime": str(nxt_ms)}]},
        now_s=now_s,
    )
    assert okx_6h["SIX"].interval_h == 6
    assert okx_6h["SIX"].current_rate_bps_h == pytest.approx(1.0)

    # History fallback when nextFundingTime is absent: 6h-spaced settlements infer interval_h=6 -> 1.0 bps/h
    def fake_http(url: str, _body: dict | None = None):
        if "public/funding-rate?" in url:
            return {"data": [{"instId": "SIX-USDT-SWAP", "fundingRate": "0.0006", "fundingTime": str(t_ms)}]}
        if "funding-rate-history" in url:
            return {
                "data": [
                    {"fundingTime": str((now_s - k * 6 * HOUR) * 1000), "realizedRate": "0.0006"}
                    for k in range(0, 5)
                ]
            }
        if "market/candles" in url:
            return {
                "data": [
                    [str((now_s - 7200) * 1000), "10", "10", "10", "10", "100", "0", "10000"],
                    [str((now_s - 3600) * 1000), "10", "10", "10", "10", "100", "0", "10000"],
                ]
            }
        return {}

    feed = PublicCarryFeed(venues=("OKX",), http_get=fake_http)
    det = feed._fetch_leg_detail(_make_leg("OKX", "SIX", interval_h=8, cur_rate=None), now_s)
    assert det.interval_h == 6
    assert det.current_rate_bps_h == pytest.approx(1.0)
    assert det.r8_bps_h == pytest.approx(12.0 / 8.0)
    assert det.r24_bps_h == pytest.approx(24.0 / 24.0)


def test_slow_poll_all_venues_timeout_raises_and_preserves_prior_snapshot_and_error(tmp_path: Path) -> None:
    from src.carry.funding_feed import SlowPollError
    import src.carry.runtime as carry_runtime

    now_s = 1_800_000_000

    def timeout_http(_url: str, _body: dict | None = None, **_kw):
        raise TimeoutError("timed out")

    feed = PublicCarryFeed(venues=("HL", "LIGHTER", "OKX", "BYBIT"), http_get=timeout_http)
    with pytest.raises(SlowPollError, match="all configured venues failed"):
        feed.poll_snapshot(now_s=now_s)
    assert set(feed.last_venue_errors.keys()) == {"HL", "LIGHTER", "OKX", "BYBIT"}

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "all_timeout.sqlite3", plan.fingerprint(), ledger_domain="paper")
    strat = carry_runtime._create_carry_strategy(plan, led, mode="paper")
    prior_snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.98, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.13, ask=100.17),
        }
    }
    strat.snapshot = prior_snap
    strat.feed = feed
    try:
        assert strat._trigger_slow_poll(now_s=int(time.time()), priority_symbols=frozenset()) is True
        deadline = time.monotonic() + 2.0
        while strat._slow_poll_inflight and time.monotonic() < deadline:
            time.sleep(0.005)
        assert not strat._slow_poll_inflight
        assert strat._consume_pending_slow_snapshot() is False
        assert strat._last_slow_poll_error is not None and "SlowPollError" in strat._last_slow_poll_error
        assert "SOL" in strat.snapshot
    finally:
        strat.close_slow_poller(wait=True)
        led.close()


def _create_6b47a19_legacy_db(
    db_path: Path,
    *,
    legacy_fp: str,
    position_id: str,
    venue_order_id: str,
) -> None:
    import sqlite3

    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE meta (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                fingerprint TEXT NOT NULL,
                created_ts INTEGER NOT NULL
            );
            CREATE TABLE positions (
                position_id TEXT PRIMARY KEY,
                symbol TEXT NOT NULL,
                long_venue TEXT NOT NULL,
                short_venue TEXT NOT NULL,
                iid_long TEXT NOT NULL,
                iid_short TEXT NOT NULL,
                status TEXT NOT NULL,
                opened_ts INTEGER NOT NULL,
                closed_ts INTEGER,
                entry_l REAL NOT NULL,
                entry_s REAL NOT NULL,
                entry_basis_bps REAL NOT NULL,
                target_base_qty TEXT NOT NULL,
                long_base_qty TEXT NOT NULL,
                short_base_qty TEXT NOT NULL,
                realized_pnl_usd TEXT NOT NULL DEFAULT '0',
                close_reason TEXT
            );
            CREATE TABLE orders (
                client_order_id TEXT PRIMARY KEY,
                position_id TEXT NOT NULL,
                venue TEXT NOT NULL,
                instrument_id TEXT NOT NULL,
                side TEXT NOT NULL,
                role TEXT NOT NULL,
                order_type TEXT NOT NULL,
                qty_contracts TEXT NOT NULL,
                base_qty TEXT NOT NULL,
                price TEXT,
                status TEXT NOT NULL,
                filled_base_qty TEXT NOT NULL DEFAULT '0',
                venue_order_id TEXT,
                created_ts INTEGER NOT NULL,
                updated_ts INTEGER NOT NULL
            );
            CREATE TABLE events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                kind TEXT NOT NULL,
                payload TEXT NOT NULL
            );
            CREATE TABLE daily_stats (
                utc_day TEXT PRIMARY KEY,
                realized_pnl_usd TEXT NOT NULL,
                consecutive_unhedged INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE runtime_flags (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        conn.execute("INSERT INTO meta(id, fingerprint, created_ts) VALUES (1, ?, 1800000000)", (legacy_fp,))
        conn.execute(
            """
            INSERT INTO positions(
                position_id, symbol, long_venue, short_venue, iid_long, iid_short,
                status, opened_ts, entry_l, entry_s, entry_basis_bps,
                target_base_qty, long_base_qty, short_base_qty
            ) VALUES (?, 'SOL', 'HL', 'OKX', 'SOL-USD-PERP.HYPERLIQUID', 'SOL-USDT-SWAP.OKX',
                      'OPEN', 1800000000, 100.0, 100.2, 20.0, '0.49', '0.49', '0.49')
            """,
            (position_id,),
        )
        conn.execute(
            """
            INSERT INTO orders(
                client_order_id, position_id, venue, instrument_id, side, role,
                order_type, qty_contracts, base_qty, price, status, filled_base_qty,
                venue_order_id, created_ts, updated_ts
            ) VALUES (?, ?, 'HL', 'SOL-USD-PERP.HYPERLIQUID', 'BUY', 'MAKER_OPEN',
                      'POST_ONLY', '0.49', '0.49', '100.0', 'FILLED', '0.49',
                      ?, 1800000001, 1800000002)
            """,
            (f"coid-{position_id}", position_id, venue_order_id),
        )


def test_legacy_6b47a19_ledger_migration_preserves_positions_and_blocks_cross_domain(tmp_path: Path) -> None:
    from src.carry.runtime import build_live_node

    paper_db = tmp_path / "legacy_paper.sqlite3"
    live_db = tmp_path / "legacy_live.sqlite3"

    paper_cfg = tmp_path / "paper.toml"
    paper_text = (REPO_ROOT / "config" / "carry-paper.toml").read_text(encoding="utf-8")
    paper_text = paper_text.replace('state_path = "../reports/carry/paper.sqlite3"', f'state_path = "{paper_db}"')
    paper_text = paper_text.replace('record_dir = "../reports/carry/tape-paper"', 'record_dir = ""')
    paper_cfg.write_text(paper_text, encoding="utf-8")
    paper_plan = load_carry_plan(paper_cfg)

    live_cfg = tmp_path / "live.toml"
    live_text = (REPO_ROOT / "config" / "carry-live.toml").read_text(encoding="utf-8")
    live_text = live_text.replace('state_path = "../reports/carry/live.sqlite3"', f'state_path = "{live_db}"')
    live_text = live_text.replace('record_dir = "../reports/carry/tape-live"', 'record_dir = ""')
    live_cfg.write_text(live_text, encoding="utf-8")
    live_plan = load_carry_plan(live_cfg)

    assert paper_plan.legacy_fingerprint() != paper_plan.fingerprint()
    assert live_plan.legacy_fingerprint() != live_plan.fingerprint()

    _create_6b47a19_legacy_db(
        paper_db,
        legacy_fp=paper_plan.legacy_fingerprint(),
        position_id="LEG-PAPER-1",
        venue_order_id="PAPER-abc123",
    )
    _create_6b47a19_legacy_db(
        live_db,
        legacy_fp=live_plan.legacy_fingerprint(),
        position_id="LEG-LIVE-1",
        venue_order_id="OKX-real-123",
    )

    # Cross-domain refusal MUST still fire even when legacy_fingerprint matches
    with pytest.raises(LedgerOwnershipError):
        CarryLedger(
            paper_db,
            paper_plan.fingerprint(),
            ledger_domain="live",
            legacy_fingerprint=paper_plan.legacy_fingerprint(),
        )
    with pytest.raises(LedgerOwnershipError):
        CarryLedger(
            live_db,
            live_plan.fingerprint(),
            ledger_domain="paper",
            legacy_fingerprint=live_plan.legacy_fingerprint(),
        )

    # Same-config paper upgrade migrates cleanly & preserves OPEN position
    node_p, led_p, strat_p = build_live_node(paper_plan, mode="paper")
    try:
        assert led_p.is_halted() == (False, None)
        assert led_p.active_positions()["LEG-PAPER-1"]["status"] == "OPEN"
        meta_p = dict(led_p.conn.execute("SELECT fingerprint, ledger_domain FROM meta WHERE id=1").fetchone())
        assert meta_p == {"fingerprint": paper_plan.fingerprint(), "ledger_domain": "paper"}
        ev_p = [r["kind"] for r in led_p.conn.execute("SELECT kind FROM events").fetchall()]
        assert "ledger_migrated_domain" in ev_p
    finally:
        strat_p.close_slow_poller(wait=False)
        node_p.dispose()
        led_p.close()

    # Same-config live upgrade migrates cleanly without fingerprint_mismatch & reconciles with exchange
    led_l = CarryLedger(
        live_db,
        live_plan.fingerprint(),
        ledger_domain="live",
        legacy_fingerprint=live_plan.legacy_fingerprint(),
    )
    try:
        assert led_l.is_halted() == (False, None)
        assert led_l.active_positions()["LEG-LIVE-1"]["status"] == "OPEN"
        meta_l = dict(led_l.conn.execute("SELECT fingerprint, ledger_domain FROM meta WHERE id=1").fetchone())
        assert meta_l == {"fingerprint": live_plan.fingerprint(), "ledger_domain": "live"}
        rec = led_l.reconcile_startup(
            venue_positions={
                ("HL", "SOL-USD-PERP.HYPERLIQUID"): D("0.49"),
                ("OKX", "SOL-USDT-SWAP.OKX"): D("-0.49"),
            },
            venue_open_orders={},
            ts_s=1_800_000_100,
        )
        assert rec.ok and not rec.halted
    finally:
        led_l.close()

    # Different-config legacy DB still halts live / rejects paper
    mismatch_db = tmp_path / "legacy_mismatch.sqlite3"
    _create_6b47a19_legacy_db(
        mismatch_db,
        legacy_fp="deadbeef" * 8,
        position_id="LEG-BAD-1",
        venue_order_id="OKX-real-999",
    )
    with pytest.raises(LedgerOwnershipError):
        CarryLedger(mismatch_db, paper_plan.fingerprint(), ledger_domain="paper", legacy_fingerprint=paper_plan.legacy_fingerprint())
    led_bad = CarryLedger(mismatch_db, live_plan.fingerprint(), ledger_domain="live", legacy_fingerprint=live_plan.legacy_fingerprint())
    assert led_bad.is_reconcile_halted() == (True, "fingerprint_mismatch")
    led_bad.close()


def test_startup_account_reader_catches_conditional_orders_and_position_toctou_races(tmp_path: Path) -> None:
    from src.carry.account_reader import CarryStartupAccountReader, StartupSnapshotError
    from src.carry.config import CarryPlan
    from src.carry.runtime import _create_carry_strategy

    env = {
        "HYPERLIQUID_ACCOUNT_ADDRESS": "0x" + "11" * 20,
        "OKX_API_KEY": "okx-k",
        "OKX_API_SECRET": "okx-s",
        "OKX_API_PASSPHRASE": "okx-p",
        "BYBIT_API_KEY": "byb-k",
        "BYBIT_API_SECRET": "byb-s",
    }
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")

    # 1) OKX untriggered conditional order on flat position MUST fail startup reconcile
    def okx_algo_http(_method: str, url: str, **_kw):
        if "okx.com/api/v5/account/positions" in url:
            return {"code": "0", "data": []}
        if "okx.com/api/v5/trade/orders-pending" in url:
            return {"code": "0", "data": []}
        if "okx.com/api/v5/trade/orders-algo-pending" in url:
            if "ordType=conditional" in url:
                return {"code": "0", "data": [{"instId": "BTC-USDT-SWAP", "algoId": "algo-1"}]}
            return {"code": "0", "data": []}
        raise AssertionError(url)

    reader_algo = CarryStartupAccountReader(environment="mainnet", environ=env, http_request=okx_algo_http)
    snap_algo = reader_algo.fetch_startup_snapshot(("OKX",))
    assert snap_algo.positions == {}
    assert snap_algo.open_orders == {("OKX", "BTC-USDT-SWAP.OKX"): 1}
    led_algo = CarryLedger(tmp_path / "okx_algo.sqlite3", plan.fingerprint(), ledger_domain="live")
    res_algo = led_algo.reconcile_startup(
        venue_positions=snap_algo.positions, venue_open_orders=snap_algo.open_orders, ts_s=1_800_000_000
    )
    assert not res_algo.ok and res_algo.halted
    assert any("orphaned_open_orders:OKX:BTC-USDT-SWAP.OKX:count=1" in d for d in res_algo.discrepancies)
    led_algo.close()

    # 2) OKX TOCTOU race: order outside ledger fills between positions #1 (flat) and orders-pending (flat)
    okx_pos_calls = [0]

    def okx_toctou_http(_method: str, url: str, **_kw):
        if "okx.com/api/v5/account/positions" in url:
            okx_pos_calls[0] += 1
            if okx_pos_calls[0] == 1:
                return {"code": "0", "data": []}
            return {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "pos": "5", "posSide": "net"}]}
        if "okx.com/api/v5/public/instruments" in url:
            return {"code": "0", "data": [{"instId": "SOL-USDT-SWAP", "ctVal": "0.1"}]}
        if "okx.com/api/v5/trade/orders-pending" in url or "okx.com/api/v5/trade/orders-algo-pending" in url:
            return {"code": "0", "data": []}
        raise AssertionError(url)

    reader_race = CarryStartupAccountReader(environment="mainnet", environ=env, http_request=okx_toctou_http)
    with pytest.raises(StartupSnapshotError, match="OKX positions changed"):
        reader_race.fetch_startup_snapshot(("OKX",))

    okx_pos_calls[0] = 0
    led_race = CarryLedger(tmp_path / "okx_race.sqlite3", plan.fingerprint(), ledger_domain="live")
    strat_race = _create_carry_strategy(
        CarryPlan(**{**plan.__dict__, "venues": ("OKX", "HL")}),
        led_race,
        mode="live",
        account_reader=reader_race,
    )
    strat_race._reconcile_on_start()
    assert strat_race.reconciled is False
    assert led_race.is_reconcile_halted()[0] is True
    led_race.close()

    # 3) Bybit StopOrder detection + Bybit TOCTOU detection
    def bybit_stop_http(_method: str, url: str, **_kw):
        if "bybit.com/v5/position/list" in url:
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        if "bybit.com/v5/order/realtime" in url:
            if "settleCoin=USDT" in url and "orderFilter=StopOrder" in url:
                return {"retCode": 0, "result": {"list": [{"symbol": "ETHUSDT", "orderId": "stop-1"}], "nextPageCursor": ""}}
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        raise AssertionError(url)

    snap_byb = CarryStartupAccountReader(environment="mainnet", environ=env, http_request=bybit_stop_http).fetch_startup_snapshot(("BYBIT",))
    assert snap_byb.open_orders == {("BYBIT", "ETHUSDT-LINEAR.BYBIT"): 1}

    byb_calls = [0]

    def bybit_toctou_http(_method: str, url: str, **_kw):
        if "bybit.com/v5/position/list" in url:
            if "settleCoin=USDT" in url:
                byb_calls[0] += 1
                if byb_calls[0] == 2:
                    return {"retCode": 0, "result": {"list": [{"symbol": "ETHUSDT", "size": "0.1", "side": "Buy"}], "nextPageCursor": ""}}
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        if "bybit.com/v5/order/realtime" in url:
            return {"retCode": 0, "result": {"list": [], "nextPageCursor": ""}}
        raise AssertionError(url)

    with pytest.raises(StartupSnapshotError, match="BYBIT positions changed"):
        CarryStartupAccountReader(environment="mainnet", environ=env, http_request=bybit_toctou_http).fetch_startup_snapshot(("BYBIT",))

    # 4) HL frontendOpenOrders trigger order + HL TOCTOU detection
    def hl_trigger_http(_method: str, url: str, *, body=None, **_kw):
        if body["type"] == "clearinghouseState":
            return {"assetPositions": []}
        if body["type"] == "frontendOpenOrders":
            if body.get("dex") is None:
                return [{"coin": "SOL", "oid": 777, "isTrigger": True}]
            return []
        raise AssertionError(body)

    snap_hl = CarryStartupAccountReader(environment="mainnet", environ=env, http_request=hl_trigger_http).fetch_startup_snapshot(("HL",))
    assert snap_hl.open_orders == {("HL", "SOL-USD-PERP.HYPERLIQUID"): 1}

    hl_calls = [0]

    def hl_toctou_http(_method: str, url: str, *, body=None, **_kw):
        if body["type"] == "clearinghouseState":
            if body.get("dex") is None:
                hl_calls[0] += 1
                if hl_calls[0] == 2:
                    return {"assetPositions": [{"position": {"coin": "SOL", "szi": "0.5"}}]}
            return {"assetPositions": []}
        if body["type"] == "frontendOpenOrders":
            return []
        raise AssertionError(body)

    with pytest.raises(StartupSnapshotError, match="HL positions changed"):
        CarryStartupAccountReader(environment="mainnet", environ=env, http_request=hl_toctou_http).fetch_startup_snapshot(("HL",))


def test_opening_below_min_notional_dust_remainder_converges_to_open(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    spec_l = InstrumentSpec(
        "COIN-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.1"), min_notional_usd=D("10"), price_increment=D("0.01")
    )
    spec_s = InstrumentSpec(
        "COIN-USDT-SWAP.OKX",
        "OKX",
        size_increment=D("0.1"),
        contract_multiplier=D("1"),
        min_notional_usd=D("10"),
        price_increment=D("0.01"),
    )
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (31.49, 31.51), spec_s.instrument_id: (31.54, 31.56)}
    mids = {spec_l.instrument_id: 31.50, spec_s.instrument_id: 31.55}
    now_s = 1_800_000_000

    led = CarryLedger(tmp_path / "dust_opening.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw = DelayedAsyncVenueGateway()
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)

    # Target 3.1, filled 3.0 / 3.0 -> remaining 0.1 * ~$31.525 = ~$3.15 < $10 min_notional_usd
    led.create_position(
        position_id="COIN-DUST",
        symbol="COIN",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s,
        entry_l=31.50,
        entry_s=31.55,
        entry_basis_bps=15.8,
        target_base_qty=D("3.1"),
    )
    led.update_position_status(
        "COIN-DUST", status="OPENING", ts_s=now_s + 1, long_base_qty=D("3.0"), short_base_qty=D("3.0")
    )

    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 2) == []
    assert gw.submitted == []
    pos = led.active_positions()["COIN-DUST"]
    assert pos["status"] == "OPEN"
    assert D(pos["long_base_qty"]) == D("3.0") and D(pos["short_base_qty"]) == D("3.0")
    assert led.reconcile_startup(
        venue_positions={("HL", spec_l.instrument_id): D("3.0"), ("OKX", spec_s.instrument_id): D("-3.0")},
        venue_open_orders={},
        ts_s=now_s + 3,
    ).ok is True

    # Zero-fill unfillable dust closes cleanly as entry_aborted
    led.create_position(
        position_id="COIN-ZERO-DUST",
        symbol="COIN",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s + 4,
        entry_l=31.50,
        entry_s=31.55,
        entry_basis_bps=15.8,
        target_base_qty=D("0.1"),
    )
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 5) == []
    row_z = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id='COIN-ZERO-DUST'").fetchone())
    assert row_z["status"] == "CLOSED" and row_z["close_reason"] == "entry_aborted:min_lot_exceeds_target_usd"
    led.close()


def test_subsequent_opening_slice_enforces_min_entry_basis_bps(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    assert plan.policy.min_entry_basis_bps == D("10")
    assert plan.policy.min_exec_basis_bps == D("10")
    pol = plan.policy.__class__(**{**plan.policy.__dict__, "taker_fast_entry_basis_bps": D("0")})
    rt = plan.runtime.__class__(**{**plan.runtime.__dict__, "maker_max_attempts": 2})

    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    now_s = 1_800_000_000

    # Quotes: mid_basis ~ 6 bps (< 10 bps), maker_exec_basis ~ 15 bps (>= 10 bps)
    snap = {
        "SOL": {
            "HL": _make_leg("HL", "SOL", r8=0.0, r24=0.0, cur_rate=0.0, bid=99.85, ask=100.05),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=now_s + 7200, r8=2.0, r24=2.0, cur_rate=2.0, bid=100.00, ask=100.02),
        }
    }
    assert (
        evaluate_entry_candidates(
            snap, open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=pol, risk=plan.risk, now_s=now_s
        )
        == []
    )

    led = CarryLedger(tmp_path / "min_basis_slice.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw = DelayedAsyncVenueGateway()
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=rt, policy=pol)
    led.create_position(
        position_id="SOL-LOW-BASIS",
        symbol="SOL",
        long_venue="HL",
        short_venue="OKX",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s,
        entry_l=99.90,
        entry_s=100.05,
        entry_basis_bps=15.0,
        target_base_qty=D("0.98"),
    )
    led.update_position_status(
        "SOL-LOW-BASIS", status="OPENING", ts_s=now_s + 1, long_base_qty=D("0.49"), short_base_qty=D("0.49")
    )

    quotes = {spec_l.instrument_id: (99.85, 100.05), spec_s.instrument_id: (100.00, 100.02)}
    mids = {spec_l.instrument_id: 99.95, spec_s.instrument_id: 100.01}
    ok_s, err_s = ctrl._submit_next_slice(
        position_id="SOL-LOW-BASIS",
        is_opening=True,
        spec_l=spec_l,
        spec_s=spec_s,
        leg_l_bid=D("99.85"),
        leg_l_ask=D("100.05"),
        leg_l_mid=D("99.95"),
        leg_s_bid=D("100.00"),
        leg_s_ask=D("100.02"),
        leg_s_mid=D("100.01"),
        remaining_base=D("0.49"),
        sp_l=20.0,
        sp_s=2.0,
        now_s=now_s + 2,
    )
    assert not ok_s and err_s == "entry_basis_below_min"

    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 3) == []
    assert ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s + 4) == []
    assert gw.submitted == []
    assert led.active_positions()["SOL-LOW-BASIS"]["status"] == "OPEN"
    led.close()

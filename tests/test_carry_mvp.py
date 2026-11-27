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
        strat.on_quote(SimpleNamespace(instrument_id="SOL-USDT-SWAP.OKX", bid_price=100.0, ask_price=100.04, ts_event=999))
        assert strat.live_quotes["SOL-USDT-SWAP.OKX"] == (100.0, 100.04)
    finally:
        rec.close()
        led.close()

    fast_files = sorted(rec_dir.glob("fast_bbo_*.jsonl.gz"))
    assert len(fast_files) == 1
    with gzip.open(fast_files[0], "rt", encoding="utf-8") as fh:
        rows = [json.loads(line) for line in fh]
    assert len(rows) == 1
    assert rows[0]["iid"] == "SOL-USDT-SWAP.OKX" and rows[0]["ev_ns"] == 999


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
            return {"data": [{"fundingRate": "-0.012", "fundingTime": str((now_s + 3600) * 1000), "prevFundingTime": str((now_s - 3 * 3600) * 1000)}]}
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

    # Move short leg up adversely by +3.00 -> unrealized price loss == -3.00 * q
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
    assert D(row["realized_pnl_usd"]) == pytest.approx(D("-3.0") * q)
    daily_pnl, _ = led.daily_stats(now_s + 100)
    assert daily_pnl == pytest.approx(D("-3.0") * q)

    assert "SOL" in led.stop_loss_cooldown_symbols(now_s=now_s + 3600, cooldown_hours=24)
    assert "SOL" not in led.stop_loss_cooldown_symbols(now_s=now_s + 25 * 3600, cooldown_hours=24)

    # Normal carry_decay close also settles non-zero positive PnL and triggers no stop cooldown
    gw.register_spec(spec_l, D("100.0"))
    gw.register_spec(spec_s, D("100.2"))
    pid2, _ = ctrl.start_entry(cand, specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}, now_s=now_s + 26 * 3600)
    ctrl.advance_pending_slices(
        specs={spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s},
        quotes={spec_l.instrument_id: (100.0, 100.0), spec_s.instrument_id: (100.2, 100.2)},
        mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2},
        now_s=now_s + 26 * 3600 + 2,
    )
    # Basis converges to 0 (both at 100.0) -> profit == +0.20 * q
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
    # Maker SELL at 100.01 (+0.01 vs 100.00) + IOC BUY at 100.15 (15 bps slippage cap vs 100.20) -> +0.06 * q
    row2 = dict(led.conn.execute("SELECT status, realized_pnl_usd FROM positions WHERE position_id=?", (pid2,)).fetchone())
    assert row2["status"] == "CLOSED"
    assert D(row2["realized_pnl_usd"]) == pytest.approx(D("0.06") * q)
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
    assert _live_exec_engine_config("readonly").reconciliation is True
    assert _live_exec_engine_config("paper").reconciliation is False

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
                        "prevFundingTime": str(now_s * 1000),
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

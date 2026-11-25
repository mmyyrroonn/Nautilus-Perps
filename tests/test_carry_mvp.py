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

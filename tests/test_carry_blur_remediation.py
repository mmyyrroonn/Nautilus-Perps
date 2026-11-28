"""Regression tests for PR 1 & PR 2 BLUR incident fixes."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import gzip
import json
from pathlib import Path
import time
from types import SimpleNamespace

import pytest

from scripts.replay_carry_tape import replay_tape
from src.carry.config import (
    CarryConfigError,
    CarryPolicy,
    CarryRiskPolicy,
    CarryRuntimePolicy,
    load_carry_plan,
)
from src.carry.executor import CarryExecutionController
from src.carry.funding_feed import HOUR, LegMarketInfo, PublicCarryFeed, next_hourly_settle_ts
from src.carry.ledger import CarryLedger, close_reason_priority, is_risk_close_reason
from src.carry.risk import CarryInversionTracker, FastRiskEvaluator
from src.carry.runtime import _create_carry_strategy
from src.carry.signal import (
    InstrumentSpec,
    estimate_first_settle_entry_tax_bps,
    estimate_min_hold_window_net_funding_bps,
    evaluate_entry_candidates,
    select_cex_exit_defer_ts,
    should_exit_carry_decay,
)
from tests.test_carry_mvp import DelayedAsyncVenueGateway, _make_leg

D = Decimal
REPO_ROOT = Path(__file__).resolve().parents[1]


def test_fast_risk_unhedged_not_masked_by_margin_guard() -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    evaluator = FastRiskEvaluator(plan.risk)
    positions = {
        "BLUR-NAKED": {
            "symbol": "BLUR",
            "long_venue": "BYBIT",
            "short_venue": "HL",
            "iid_long": "BLURUSDT-LINEAR.BYBIT",
            "iid_short": "BLUR-USD-PERP.HYPERLIQUID",
            "entry_l": 100.0,
            "entry_s": 100.0,
            "long_base_qty": "10",
            "short_base_qty": "0",
        }
    }
    mids = {"BLURUSDT-LINEAR.BYBIT": 73.0, "BLUR-USD-PERP.HYPERLIQUID": 73.0}

    dec0 = evaluator.evaluate(
        positions=positions,
        live_mids=mids,
        daily_realized_pnl_usd=D("0"),
        consecutive_unhedged=0,
        now_s=1_800_000_000,
    )
    assert dec0.close_positions == (("BLUR-NAKED", "margin_guard_25pct"),)
    assert dec0.halt_system is False

    dec10 = evaluator.evaluate(
        positions=positions,
        live_mids=mids,
        daily_realized_pnl_usd=D("0"),
        consecutive_unhedged=0,
        now_s=1_800_000_010,
    )
    assert dec10.close_positions == (("BLUR-NAKED", "unhedged_timeout_kill"),)
    assert dec10.halt_system is True
    assert dec10.halt_reason == "unhedged_timeout_kill"


def test_emergency_close_respects_closing_dual_taker_ioc_and_partial_fill(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    led = CarryLedger(tmp_path / "handoff_dt.sqlite3", plan.fingerprint(), ledger_domain="live")
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOLUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("0.01"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)

    now_s = 1_800_000_000
    led.create_position(
        position_id="SOL-DT",
        symbol="SOL",
        long_venue="HL",
        short_venue="BYBIT",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s - 7200,
        entry_l=100.0,
        entry_s=100.1,
        entry_basis_bps=10.0,
        target_base_qty=D("0.50"),
    )
    led.update_position_status(
        "SOL-DT", status="CLOSING", ts_s=now_s - 60, long_base_qty=D("0.50"), short_base_qty=D("0.50")
    )
    ctrl._slice_attempts["SOL-DT"] = plan.runtime.maker_max_attempts
    quotes = {spec_l.instrument_id: (99.9, 100.0), spec_s.instrument_id: (100.0, 100.1)}
    mids = {spec_l.instrument_id: 99.95, spec_s.instrument_id: 100.05}

    submitted = ctrl.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s)
    assert submitted == ["SOL-DT"]
    assert len(gw.submitted) == 2
    ioc_l = next(o["client_order_id"] for o in gw.submitted if o["side"] == "SELL")
    ioc_s = next(o["client_order_id"] for o in gw.submitted if o["side"] == "BUY")

    # Upgrade while both closing IOC orders are still OPEN -> zero duplicate MARKET orders submitted
    ctrl.emergency_close_position("SOL-DT", specs=specs, reason="margin_guard_25pct", now_s=now_s + 1, live_mids=mids)
    assert len(gw.submitted) == 2

    # Long IOC partially fills 0.20 then cancels; short IOC cancels 0 -> emergency_close covers exact 0.30 and 0.50
    gw.deliver_cancel(ioc_l, filled_base_qty=D("0.20"), avg_fill_price=D("99.90"))
    gw.deliver_cancel(ioc_s, filled_base_qty=D("0"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)

    em_orders = gw.submitted[2:]
    assert len(em_orders) == 2
    by_side = {o["side"]: o for o in em_orders}
    assert D(str(by_side["SELL"]["qty_contracts"])) == D("0.30")
    assert D(str(by_side["BUY"]["qty_contracts"])) == D("0.50")
    assert all(o["order_type"] == "MARKET" and o["reduce_only"] is True for o in em_orders)

    gw.deliver_fill(by_side["SELL"]["client_order_id"], filled_base_qty=D("0.30"), avg_fill_price=D("99.80"))
    gw.deliver_fill(by_side["BUY"]["client_order_id"], filled_base_qty=D("0.50"), avg_fill_price=D("100.10"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)

    row = dict(led.conn.execute("SELECT * FROM positions WHERE position_id='SOL-DT'").fetchone())
    assert row["status"] == "CLOSED"
    assert row["close_reason"] == "margin_guard_25pct"
    assert D(row["long_base_qty"]) == D("0") and D(row["short_base_qty"]) == D("0")
    led.close()


def test_emergency_close_closing_maker_and_hedge_handoff(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    gw = DelayedAsyncVenueGateway()
    gw.defer_cancels = True
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOLUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("0.01"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    quotes = {spec_l.instrument_id: (99.9, 100.0), spec_s.instrument_id: (100.0, 100.1)}
    mids = {spec_l.instrument_id: 99.95, spec_s.instrument_id: 100.05}
    now_s = 1_800_000_000

    # Case A: Pending closing maker cancel (non-terminal) blocks only maker leg; late partial fill reduces emergency qty
    gw = DelayedAsyncVenueGateway()
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    led_a = CarryLedger(tmp_path / "handoff_mk.sqlite3", plan.fingerprint(), ledger_domain="live")
    ctrl_a = CarryExecutionController(ledger=led_a, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    led_a.create_position(
        position_id="SOL-MK",
        symbol="SOL",
        long_venue="HL",
        short_venue="BYBIT",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s - 7200,
        entry_l=100.0,
        entry_s=100.1,
        entry_basis_bps=10.0,
        target_base_qty=D("0.50"),
    )
    led_a.update_position_status(
        "SOL-MK", status="CLOSING", ts_s=now_s - 60, long_base_qty=D("0.50"), short_base_qty=D("0.50")
    )
    assert ctrl_a.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s) == ["SOL-MK"]
    mk_coid = gw.submitted[0]["client_order_id"]

    ctrl_a.emergency_close_position("SOL-MK", specs=specs, reason="basis_stop_30m", now_s=now_s + 1, live_mids=mids)
    assert mk_coid in gw.cancel_requests
    # Short leg has no external order -> submits immediately; long leg waits for pending maker cancel
    assert len(gw.submitted) == 2
    assert gw.submitted[1]["side"] == "BUY" and D(str(gw.submitted[1]["qty_contracts"])) == D("0.50")

    # Maker receives 0.20 late fill on cancel -> long emergency leg submits exact 0.30
    gw.deliver_cancel(mk_coid, filled_base_qty=D("0.20"), avg_fill_price=D("100.05"))
    ctrl_a.poll_active_orders(live_mids=mids, now_s=now_s + 2, live_quotes=quotes)
    assert len(gw.submitted) == 3
    assert gw.submitted[2]["side"] == "SELL" and D(str(gw.submitted[2]["qty_contracts"])) == D("0.30")
    led_a.close()

    # Case B: Closing maker filled 0.20 (hedge 0.20 OPEN on short), remaining 0.30 maker OPEN -> emergency buys only 0.30 short
    gw_b = DelayedAsyncVenueGateway()
    gw_b.register_spec(spec_l)
    gw_b.register_spec(spec_s)
    led_b = CarryLedger(tmp_path / "handoff_hg.sqlite3", plan.fingerprint(), ledger_domain="live")
    ctrl_b = CarryExecutionController(ledger=led_b, gateway=gw_b, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    led_b.create_position(
        position_id="SOL-HG",
        symbol="SOL",
        long_venue="HL",
        short_venue="BYBIT",
        iid_long=spec_l.instrument_id,
        iid_short=spec_s.instrument_id,
        opened_ts=now_s - 7200,
        entry_l=100.0,
        entry_s=100.1,
        entry_basis_bps=10.0,
        target_base_qty=D("0.50"),
    )
    led_b.update_position_status(
        "SOL-HG", status="CLOSING", ts_s=now_s - 60, long_base_qty=D("0.50"), short_base_qty=D("0.50")
    )
    assert ctrl_b.advance_pending_slices(specs=specs, quotes=quotes, mids=mids, now_s=now_s) == ["SOL-HG"]
    mk_b = gw_b.submitted[0]["client_order_id"]
    gw_b.deliver_fill(mk_b, filled_base_qty=D("0.20"), avg_fill_price=D("100.05"), partial=True)
    ctrl_b.poll_active_orders(live_mids=mids, now_s=now_s + 1, live_quotes=quotes)
    hg_b = gw_b.submitted[1]["client_order_id"]

    ctrl_b.emergency_close_position("SOL-HG", specs=specs, reason="margin_guard_25pct", now_s=now_s + 2, live_mids=mids)
    assert mk_b in gw_b.cancel_requests
    # Remaining: long=0.30 (covered by maker), short=0.50 (0.20 covered by open hedge -> only 0.30 submitted)
    assert len(gw_b.submitted) == 3
    assert gw_b.submitted[2]["side"] == "BUY" and D(str(gw_b.submitted[2]["qty_contracts"])) == D("0.30")

    gw_b.deliver_cancel(mk_b, filled_base_qty=D("0.20"))
    gw_b.deliver_cancel(hg_b, filled_base_qty=D("0"))
    ctrl_b.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    tail_orders = gw_b.submitted[3:]
    assert sorted((o["side"], D(str(o["qty_contracts"]))) for o in tail_orders) == [
        ("BUY", D("0.20")),
        ("SELL", D("0.30")),
    ]
    led_b.close()


def test_funding_feed_interval_and_next_settle_consistency() -> None:
    now_s = 1_800_003_540  # 60s before next top-of-hour settle (1_800_003_600)
    cur_ts = [now_s]
    byb_iv = ["1"]
    byb_fr = ["-0.0010"]  # -10 bps over 1h => -10 bps/h
    byb_include_nxt = [True]
    okx_bulk_empty = [False]
    hist_calls = [0]

    def fake_http(url: str, body: dict | None = None):
        if "hyperliquid" in url:
            rt = (body or {}).get("type")
            if rt == "metaAndAssetCtxs":
                if body.get("dex") == "xyz":
                    return [{"universe": []}, []]
                return [
                    {"universe": [{"name": "BLUR"}]},
                    [{"markPx": "1.0", "midPx": "1.0", "funding": "-0.0010", "dayNtlVlm": "5000000"}],
                ]
            if rt == "fundingHistory":
                hist_calls[0] += 1
                base_hr = (cur_ts[0] // HOUR) * HOUR
                return [{"time": (base_hr - k * HOUR) * 1000, "fundingRate": "-0.0010"} for k in range(0, 10)]
            if rt == "candleSnapshot":
                return [{"t": (cur_ts[0] - 7200) * 1000, "c": "1.0", "v": "100"}, {"t": (cur_ts[0] - 3600) * 1000, "c": "1.0", "v": "100"}]
        if "bybit" in url:
            if "instruments-info" in url:
                return {"result": {"list": [{"symbol": "BLURUSDT", "fundingInterval": "480"}]}}
            if "market/tickers" in url:
                step_s = int(byb_iv[0]) * HOUR
                nxt_s = ((cur_ts[0] // step_s) + 1) * step_s
                row = {
                    "symbol": "BLURUSDT",
                    "markPrice": "1.0",
                    "bid1Price": "0.999",
                    "ask1Price": "1.001",
                    "fundingRate": byb_fr[0],
                    "fundingIntervalHour": byb_iv[0],
                    "turnover24h": "5000000",
                }
                if byb_include_nxt[0]:
                    row["nextFundingTime"] = str(nxt_s * 1000)
                return {"result": {"list": [row]}}
            if "funding/history" in url:
                hist_calls[0] += 1
                return {
                    "result": {
                        "list": [
                            {"fundingRateTimestamp": str((cur_ts[0] - k * 8 * HOUR) * 1000), "fundingRate": "-0.0080"}
                            for k in range(1, 5)
                        ]
                    }
                }
            if "market/kline" in url:
                return {
                    "result": {
                        "list": [
                            [str((cur_ts[0] - 7200) * 1000), "1.0", "1.0", "1.0", "1.0", "100", "10000"],
                            [str((cur_ts[0] - 3600) * 1000), "1.0", "1.0", "1.0", "1.0", "100", "10000"],
                        ]
                    }
                }
        if "okx.com" in url:
            if "public/instruments" in url:
                return {"data": [{"instId": "BLUR-USDT-SWAP", "state": "live", "settleCcy": "USDT"}]}
            if "market/tickers" in url:
                return {"data": [{"instId": "BLUR-USDT-SWAP", "last": "1.0", "bidPx": "0.999", "askPx": "1.001", "volCcy24h": "5000000"}]}
            if "instId=ANY" in url:
                if okx_bulk_empty[0]:
                    return {"data": []}
                return {
                    "data": [
                        {
                            "instId": "BLUR-USDT-SWAP",
                            "fundingRate": "0.0020",
                            "fundingTime": str(1_800_003_600 * 1000),
                            "nextFundingTime": str((1_800_003_600 + 2 * HOUR) * 1000),
                        }
                    ]
                }
            if "funding-rate-history" in url:
                hist_calls[0] += 1
                return {
                    "data": [
                        {"fundingTime": str((1_800_003_600 - k * 2 * HOUR) * 1000), "realizedRate": "0.0020"}
                        for k in range(1, 5)
                    ]
                }
            if "market/candles" in url:
                return {
                    "data": [
                        [str((cur_ts[0] - 7200) * 1000), "1.0", "1.0", "1.0", "1.0", "100", "0", "10000"],
                        [str((cur_ts[0] - 3600) * 1000), "1.0", "1.0", "1.0", "1.0", "100", "0", "10000"],
                    ]
                }
        return {}

    feed = PublicCarryFeed(venues=("HL", "BYBIT", "OKX"), http_get=fake_http)
    snap1 = feed.poll_snapshot(now_s=now_s)
    assert hist_calls[0] == 3
    assert snap1["BLUR"]["BYBIT"].interval_h == 1
    assert snap1["BLUR"]["BYBIT"].current_rate_bps_h == pytest.approx(-10.0)
    assert snap1["BLUR"]["BYBIT"].r8_bps_h == pytest.approx(-10.0)
    assert snap1["BLUR"]["HL"].next_settle_ts == 1_800_003_600
    assert snap1["BLUR"]["OKX"].interval_h == 2
    assert snap1["BLUR"]["OKX"].next_settle_ts == 1_800_003_600

    # Poll 120s later (across hour boundary, within 300s detail cache): MUST hit cache (hist_calls stays 3)
    # Also simulate OKX bulk missing & Bybit ticker missing nextFundingTime and missing fundingRate after switching to 4h
    now_s2 = now_s + 120  # 1_800_003_660
    cur_ts[0] = now_s2
    byb_iv[0] = "4"
    byb_fr[0] = ""
    byb_include_nxt[0] = False
    okx_bulk_empty[0] = True
    snap2 = feed.poll_snapshot(now_s=now_s2)
    assert hist_calls[0] == 3
    assert snap2["BLUR"]["HL"].next_settle_ts == 1_800_007_200
    assert snap2["BLUR"]["HL"].r8_bps_h == pytest.approx(-10.0)
    assert snap2["BLUR"]["BYBIT"].interval_h == 4
    assert snap2["BLUR"]["BYBIT"].current_rate_bps_h is None  # do not carry stale 1h rate into 4h period
    assert snap2["BLUR"]["BYBIT"].r8_bps_h == pytest.approx(-10.0)
    assert snap2["BLUR"]["BYBIT"].next_settle_ts == 1_800_014_400
    assert snap2["BLUR"]["OKX"].interval_h == 2
    assert snap2["BLUR"]["OKX"].next_settle_ts == 1_800_010_800  # 1_800_003_600 + 2h, not 8h fallback!


def test_blur_unpaid_window_veto_and_boundaries() -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    now_s = 1_795_795_776  # 2026-11-27T16:09:36Z
    horizon_ts = now_s + plan.policy.min_hold_h * HOUR

    # Exact BLUR setup: BYBIT 8h receiving at 00:00 (>6h), HL 1h paying -11.17364 bps/h
    bybit_blur = _make_leg(
        "BYBIT",
        "BLUR",
        interval_h=8,
        next_settle_ts=now_s + int(7.84 * HOUR),
        r8=-7.5054,
        r24=-2.5832,
        cur_rate=-14.14335,
        bid=0.008124,
        ask=0.008127,
    )
    hl_blur = _make_leg(
        "HL",
        "BLUR",
        interval_h=1,
        next_settle_ts=now_s + int(0.84 * HOUR),
        r8=-4.3396,
        r24=-1.4999,
        cur_rate=-11.17364,
        bid=0.008129,
        ask=0.008141,
    )
    assert estimate_first_settle_entry_tax_bps(bybit_blur, hl_blur, now_s) == 0.0
    win_net, recv_after = estimate_min_hold_window_net_funding_bps(bybit_blur, hl_blur, now_s, plan.policy.min_hold_h)
    assert recv_after is True
    assert win_net == pytest.approx(-67.04184, rel=1e-4)
    assert (
        evaluate_entry_candidates(
            {"BLUR": {"BYBIT": bybit_blur, "HL": hl_blur}},
            open_symbols=set(),
            venue_notional_used={},
            excluded_symbols=set(),
            policy=plan.policy,
            risk=plan.risk,
            now_s=now_s,
        )
        == []
    )

    # Boundary: receiving leg settles at exact now + H -> included in window, passes
    bybit_exact_h = _make_leg(
        "BYBIT",
        "BLUR",
        interval_h=8,
        next_settle_ts=horizon_ts,
        r8=-7.5,
        r24=-2.5,
        cur_rate=-14.0,
        bid=1.0,
        ask=1.0005,
    )
    hl_exact_h = _make_leg(
        "HL",
        "BLUR",
        interval_h=1,
        next_settle_ts=now_s + 1800,
        r8=-4.3,
        r24=-1.4,
        cur_rate=-11.0,
        bid=1.0015,
        ask=1.0020,
    )
    cands_exact = evaluate_entry_candidates(
        {"BLUR": {"BYBIT": bybit_exact_h, "HL": hl_exact_h}},
        open_symbols=set(),
        venue_notional_used={},
        excluded_symbols=set(),
        policy=plan.policy,
        risk=plan.risk,
        now_s=now_s,
    )
    assert len(cands_exact) == 1

    # Boundary: receiving leg settles at now + H + 1 -> excluded, vetoed
    bybit_after_h = _make_leg(
        "BYBIT",
        "BLUR",
        interval_h=8,
        next_settle_ts=horizon_ts + 1,
        r8=-7.5,
        r24=-2.5,
        cur_rate=-14.0,
        bid=1.0,
        ask=1.0005,
    )
    assert (
        evaluate_entry_candidates(
            {"BLUR": {"BYBIT": bybit_after_h, "HL": hl_exact_h}},
            open_symbols=set(),
            venue_notional_used={},
            excluded_symbols=set(),
            policy=plan.policy,
            risk=plan.risk,
            now_s=now_s,
        )
        == []
    )

    # Exact float subtraction boundary: c8 = -1.2 - (-2.2) == 1.0000000000000002 -> 6h gross = 6.0, bleed = 6.0 -> must veto
    exact_tie_l = _make_leg(
        "BYBIT", "TIE", interval_h=8, next_settle_ts=horizon_ts + 600, r8=-2.2, r24=-2.2, cur_rate=-2.2, bid=1.0, ask=1.0005
    )
    exact_tie_s = _make_leg(
        "HL", "TIE", interval_h=1, next_settle_ts=now_s + 1800, r8=-1.2, r24=-1.2, cur_rate=-1.0, bid=1.0015, ask=1.0020
    )
    assert (
        evaluate_entry_candidates(
            {"TIE": {"BYBIT": exact_tie_l, "HL": exact_tie_s}},
            open_symbols=set(),
            venue_notional_used={},
            excluded_symbols=set(),
            policy=plan.policy,
            risk=plan.risk,
            now_s=now_s,
        )
        == []
    )

    # Zero-rate and small bleed (< min(c8, c24) * H) both pass cleanly
    small_l = _make_leg(
        "BYBIT", "TINY", interval_h=8, next_settle_ts=horizon_ts + 600, r8=-2.5, r24=-2.5, cur_rate=-2.5, bid=1.0, ask=1.0005
    )
    small_s = _make_leg(
        "HL", "TINY", interval_h=1, next_settle_ts=now_s + 1800, r8=-0.5, r24=-0.5, cur_rate=-0.5, bid=1.0015, ask=1.0020
    )
    # 6 * 0.5 = 3.0 bleed < min(c8, c24) * 6 = 12.0 -> passes
    assert (
        len(
            evaluate_entry_candidates(
                {"TINY": {"BYBIT": small_l, "HL": small_s}},
                open_symbols=set(),
                venue_notional_used={},
                excluded_symbols=set(),
                policy=plan.policy,
                risk=plan.risk,
                now_s=now_s,
            )
        )
        == 1
    )


def test_replay_opening_slice_rechecks_leg_spread_like_live(tmp_path: Path) -> None:
    tape_dir = tmp_path / "tape_slice_spread"
    tape_dir.mkdir()
    t0 = 1_800_050_400
    hl_iid, okx_iid = "SOL-USD-PERP.HYPERLIQUID", "SOL-USDT-SWAP.OKX"

    def _row(sym: str, v: str, iid: str, ih: int, ns: int, r8: float, cr: float, b: float, a: float) -> dict:
        return {
            "s": sym, "v": v, "ac": "CRYPTO", "iid": iid, "ih": ih, "ns": ns,
            "r8": r8, "r24": r8, "cr": cr, "mp": 0.5 * (b + a), "b": b, "a": a,
            "v24": 2_000_000.0, "v1": 50_000.0,
        }

    (tape_dir / "slow_snapshots_20270115.jsonl.gz").write_bytes(
        gzip.compress(
            (
                json.dumps(
                    {
                        "ts": t0,
                        "legs": [
                            _row("SOL", "HL", hl_iid, 1, t0 + 3600, 0.0, 0.0, 99.98, 100.02),
                            _row("SOL", "OKX", okx_iid, 8, t0 + 7200, 2.0, 2.0, 100.13, 100.17),
                        ],
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
    )
    # Slice 1 maker fills at t0+1 when HL ask=99.98 crosses limit, while HL spread widens to ~33bp (>25bp) so Slice 2 is blocked
    fast_rows = [
        {"ts": t0 + 0.5, "iid": okx_iid, "b": 100.13, "a": 100.17, "m": 100.15},
        {"ts": t0 + 1.0, "iid": hl_iid, "b": 99.65, "a": 99.98, "m": 99.815},
        {"ts": t0 + 2.0, "iid": hl_iid, "b": 99.65, "a": 99.98, "m": 99.815},
        {"ts": t0 + 3.0, "iid": hl_iid, "b": 99.65, "a": 99.98, "m": 99.815},
        {"ts": t0 + 4.0, "iid": hl_iid, "b": 99.65, "a": 99.98, "m": 99.815},
    ]
    (tape_dir / "fast_bbo_20270115.jsonl.gz").write_bytes(
        gzip.compress("".join(json.dumps(r) + "\n" for r in fast_rows).encode("utf-8"))
    )

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    rep = replay_tape(tape_dir=tape_dir, plan=plan, execution_mode="hybrid")
    assert rep["summary"]["open_positions_count"] == 1
    pos = rep["open_positions"][0]
    assert pos["status"] == "OPEN"
    # Half size (~0.50 SOL = $50 slice) instead of full $100
    assert D(pos["long_base_qty"]) == D("0.4996")
    assert D(pos["short_base_qty"]) == D("0.4996")


def test_replay_exact_25bp_spread_boundary_and_candidate_diagnostics(tmp_path: Path) -> None:
    tape_dir = tmp_path / "tape_exact_25bp"
    tape_dir.mkdir()
    t0 = 1_800_050_400
    hl_a, okx_a = "AAA-USD-PERP.HYPERLIQUID", "AAA-USDT-SWAP.OKX"
    hl_b, okx_b = "BBB-USD-PERP.HYPERLIQUID", "BBB-USDT-SWAP.OKX"

    def _row(sym: str, v: str, iid: str, ih: int, ns: int, r8: float, cr: float, b: float, a: float) -> dict:
        return {
            "s": sym, "v": v, "ac": "CRYPTO", "iid": iid, "ih": ih, "ns": ns,
            "r8": r8, "r24": r8, "cr": cr, "mp": 0.5 * (b + a), "b": b, "a": a,
            "v24": 2_000_000.0, "v1": 50_000.0,
        }

    # Slow snapshot admits both AAA (19bp) and BBB (4bp) -> candidates_passed must count both = 2
    # Then fast BBO at t0+1.0 fills AAA Slice 1 (bid 0.02097375 < limit 0.02098) with exact Decimal 25.0000bp spread
    # (0.02097375, 0.02102625), which would fail float mid but must pass Decimal mid and submit Slice 2 -> total 2 makers!
    (tape_dir / "slow_snapshots_20270115.jsonl.gz").write_bytes(
        gzip.compress(
            (
                json.dumps(
                    {
                        "ts": t0,
                        "legs": [
                            _row("AAA", "HL", hl_a, 1, t0 + 3600, 0.0, 0.0, 0.02098, 0.02102),
                            _row("AAA", "OKX", okx_a, 8, t0 + 7200, 3.0, 3.0, 0.021035, 0.021040),
                            _row("BBB", "HL", hl_b, 1, t0 + 3600, 0.0, 0.0, 99.98, 100.02),
                            _row("BBB", "OKX", okx_b, 8, t0 + 7200, 2.0, 2.0, 100.13, 100.17),
                        ],
                    }
                )
                + "\n"
            ).encode("utf-8")
        )
    )
    fast_rows = [
        {"ts": t0 + 0.5, "iid": okx_a, "b": 0.021035, "a": 0.021040, "m": 0.0210375},
        {"ts": t0 + 1.0, "iid": hl_a, "b": 0.02097375, "a": 0.02102625, "m": 0.021},
    ]
    (tape_dir / "fast_bbo_20270115.jsonl.gz").write_bytes(
        gzip.compress("".join(json.dumps(r) + "\n" for r in fast_rows).encode("utf-8"))
    )

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    rep = replay_tape(tape_dir=tape_dir, plan=plan, execution_mode="hybrid")
    assert rep["diagnostics"]["candidates_passed"] == 2
    assert rep["summary"]["maker_orders_submitted"] == 2


def test_bybit_history_inferred_iv_recomputes_missing_next_settle() -> None:
    # 03:59 UTC after aligned 8h epoch 1_800_000_000 (00:00)
    now_s = 1_800_000_000 + 3 * HOUR + 3540
    feed = PublicCarryFeed(venues=("BYBIT", "HL"))
    feed._byb_meta_ts = now_s
    feed._byb_iv_cache = {}
    feed._okx_inst_ts = now_s
    feed._okx_inst_cache = {}

    def fake_req(url: str, body=None, headers=None):
        if "hyperliquid" in url:
            if (body or {}).get("type") == "metaAndAssetCtxs":
                if body.get("dex") == "xyz":
                    return [{"universe": []}, []]
                return [{"universe": [{"name": "FOO"}]}, [{"markPx": "1.0", "midPx": "1.0", "funding": "0.0001", "dayNtlVlm": "2000000"}]]
            if (body or {}).get("type") == "fundingHistory":
                return [{"time": (now_s - HOUR) * 1000, "fundingRate": "0.0001"}]
            if (body or {}).get("type") == "candleSnapshot":
                return [{"t": (now_s - 2 * HOUR) * 1000, "c": "1.0", "v": "100000"}, {"t": (now_s - HOUR) * 1000, "c": "1.0", "v": "100000"}]
            return []
        if "/v5/market/tickers" in url:
            return {
                "result": {
                    "list": [
                        {
                            "symbol": "FOOUSDT",
                            "markPrice": "1.0",
                            "bid1Price": "0.9999",
                            "ask1Price": "1.0001",
                            "fundingRate": "0.0008",
                            "turnover24h": "2000000",
                        }
                    ]
                }
            }
        if "/v5/market/funding/history" in url:
            return {
                "result": {
                    "list": [
                        {"fundingRateTimestamp": str((now_s - 2 * HOUR) * 1000), "fundingRate": "0.0008"},
                        {"fundingRateTimestamp": str((now_s - 3 * HOUR) * 1000), "fundingRate": "0.0008"},
                    ]
                }
            }
        if "/v5/market/kline" in url:
            return {"result": {"list": [[str((now_s - HOUR) * 1000), "1", "1", "1", "1.0", "100", "100000"]]}}
        return {}

    feed._request = fake_req  # type: ignore[method-assign]
    snap = feed.poll_snapshot(now_s=now_s, priority_symbols={"FOO"})
    leg = snap["FOO"]["BYBIT"]
    assert leg.interval_h == 1
    assert leg.next_settle_ts == next_hourly_settle_ts(now_s, 1)
    assert leg.current_rate_bps_h == pytest.approx(8.0)


def test_bybit_explicit_ticker_iv_updates_cache_for_next_round_omission() -> None:
    now_s = 1_800_000_000
    feed = PublicCarryFeed(venues=("BYBIT",))
    feed._byb_meta_ts = now_s
    feed._byb_iv_cache = {"FOOUSDT": 8}
    feed._okx_inst_ts = now_s
    feed._okx_inst_cache = {}
    round_no = {"idx": 1}

    def fake_req(url: str, body=None, headers=None):
        if "/v5/market/tickers" in url:
            row = {
                "symbol": "FOOUSDT",
                "markPrice": "1.0",
                "bid1Price": "0.9999",
                "ask1Price": "1.0001",
                "fundingRate": "0.0001",
                "nextFundingTime": str((now_s + HOUR) * 1000),
                "turnover24h": "2000000",
            }
            if round_no["idx"] == 1:
                row["fundingIntervalHour"] = "1"
            return {"result": {"list": [row]}}
        if "/v5/market/funding/history" in url:
            return {
                "result": {
                    "list": [
                        {"fundingRateTimestamp": str((now_s - HOUR) * 1000), "fundingRate": "0.0001"},
                        {"fundingRateTimestamp": str((now_s - 2 * HOUR) * 1000), "fundingRate": "0.0001"},
                    ]
                }
            }
        if "/v5/market/kline" in url:
            return {"result": {"list": [[str((now_s - HOUR) * 1000), "1", "1", "1", "1.0", "100", "100000"]]}}
        return {}

    feed._request = fake_req  # type: ignore[method-assign]
    snap1 = feed.poll_snapshot(now_s=now_s)
    assert snap1["FOO"]["BYBIT"].interval_h == 1
    assert snap1["FOO"]["BYBIT"].current_rate_bps_h == pytest.approx(1.0)
    assert feed._byb_iv_cache["FOOUSDT"] == 1

    round_no["idx"] = 2
    snap2 = feed.poll_snapshot(now_s=now_s + 60)
    assert snap2["FOO"]["BYBIT"].interval_h == 1
    assert snap2["FOO"]["BYBIT"].current_rate_bps_h == pytest.approx(1.0)


def test_okx_cached_detail_bulk_funding_without_nxt_advances_expired_cached_nxt() -> None:
    t_epoch = 1_800_000_000
    t_settle = t_epoch + 8 * HOUR
    t_cached = t_settle - 120
    t_now = t_settle + 60
    feed = PublicCarryFeed(venues=("OKX",))
    feed._detail_cache[("OKX", "SOL")] = {
        "fetched_ts": t_cached,
        "r8": 1.0,
        "r24": 1.0,
        "current_rate_bps_h": 1.0,
        "iv": 8,
        "next_settle_ts": t_settle,
        "prev_1h_price": 100.0,
        "vol1h_usd": 50_000.0,
        "settle_events": (),
    }
    leg = LegMarketInfo(
        venue="OKX",
        symbol="SOL",
        raw_symbol="SOL-USDT-SWAP",
        instrument_id="SOL-USDT-SWAP.OKX",
        asset_class="CRYPTO",
        interval_h=8,
        next_settle_ts=0,
        r8_bps_h=None,
        r24_bps_h=None,
        current_rate_bps_h=None,
        mark_price=100.0,
        prev_1h_price=100.0,
        bid=99.95,
        ask=100.05,
        vol24_usd=2_000_000.0,
        vol1h_usd=50_000.0,
        fetched_ts=t_now,
    )
    enriched = feed._fetch_leg_detail(
        leg,
        t_now,
        okx_bulk_funding={"SOL-USDT-SWAP": (0.0004, None, None)},
    )
    assert enriched.interval_h == 8
    assert enriched.next_settle_ts == t_settle + 8 * HOUR
    assert enriched.current_rate_bps_h == pytest.approx(0.5)


def test_pr2_config_validation_and_legacy_fingerprint(tmp_path: Path) -> None:
    base_text = (REPO_ROOT / "config" / "carry-paper.toml").read_text(encoding="utf-8")
    paper_plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    live_plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    assert (
        replace(live_plan, state_path=Path("/Volumes/Expansion/perpsarb/Nautilus-Perps/reports/carry/live.sqlite3")).fingerprint()
        == "d90851835399132a5d8631b0957970704301853e4c3b146ff5d4497eb274571c"
    )
    legacy_paper = replace(
        paper_plan,
        state_path=Path("/Volumes/Expansion/perpsarb/Nautilus-Perps/reports/carry/paper.sqlite3"),
        policy=replace(paper_plan.policy, cex_exit_defer_dual_leg_net=CarryPolicy.cex_exit_defer_dual_leg_net),
        risk=replace(
            paper_plan.risk,
            basis_stop_via_closing=CarryRiskPolicy.basis_stop_via_closing,
            soft_margin_guard_pct=CarryRiskPolicy.soft_margin_guard_pct,
            carry_inversion_stop_bps_h=CarryRiskPolicy.carry_inversion_stop_bps_h,
            carry_inversion_persist_mins=CarryRiskPolicy.carry_inversion_persist_mins,
            margin_guard_cooldown_hours=CarryRiskPolicy.margin_guard_cooldown_hours,
        ),
        runtime=replace(
            paper_plan.runtime,
            max_close_cross_spread_bps=CarryRuntimePolicy.max_close_cross_spread_bps,
            close_vacuum_defer_max_secs=CarryRuntimePolicy.close_vacuum_defer_max_secs,
        ),
    )
    assert legacy_paper.fingerprint() == "8247c775c8ac8598f85d92bcc74bfeca8cbac36113e758aa8f70d5b3761ba49e"

    bad_cases = [
        ('soft_margin_guard_pct = "20"', 'soft_margin_guard_pct = "25"'),
        ('soft_margin_guard_pct = "20"', 'soft_margin_guard_pct = "26"'),
        ('carry_inversion_persist_mins = 30', 'carry_inversion_persist_mins = 0'),
        ('carry_inversion_stop_bps_h = "3.0"', 'carry_inversion_stop_bps_h = "0"'),
        ('close_vacuum_defer_max_secs = 180', 'close_vacuum_defer_max_secs = 0'),
        ('max_close_cross_spread_bps = "150"', 'max_close_cross_spread_bps = "0"'),
    ]
    for i, (old, new) in enumerate(bad_cases):
        bad_path = tmp_path / f"bad_{i}.toml"
        bad_path.write_text(base_text.replace(old, new), encoding="utf-8")
        with pytest.raises(CarryConfigError):
            load_carry_plan(bad_path)


def test_soft_margin_guard_routes_closing_and_hard_25pct_unconditional(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    evaluator = FastRiskEvaluator(plan.risk)
    positions = {
        "BLUR-1": {
            "symbol": "BLUR",
            "long_venue": "BYBIT",
            "short_venue": "HL",
            "iid_long": "BLURUSDT-LINEAR.BYBIT",
            "iid_short": "BLUR-USD-PERP.HYPERLIQUID",
            "entry_l": 100.0,
            "entry_s": 100.0,
            "long_base_qty": "1",
            "short_base_qty": "1",
        }
    }
    dec20 = evaluator.evaluate(
        positions=positions,
        live_mids={"BLURUSDT-LINEAR.BYBIT": 80.0, "BLUR-USD-PERP.HYPERLIQUID": 80.0},
        daily_realized_pnl_usd=D("0"),
        consecutive_unhedged=0,
        now_s=1_800_000_000,
    )
    assert dec20.soft_close_positions == (("BLUR-1", "soft_margin_guard_20pct"),)
    assert dec20.close_positions == ()

    # At 25% loss even when opposite leg mid is stale/missing, hard margin_guard_25pct fires unconditionally
    dec25 = evaluator.evaluate(
        positions=positions,
        live_mids={"BLURUSDT-LINEAR.BYBIT": 75.0},
        daily_realized_pnl_usd=D("0"),
        consecutive_unhedged=0,
        now_s=1_800_000_001,
    )
    assert dec25.close_positions == (("BLUR-1", "margin_guard_25pct"),)
    assert dec25.soft_close_positions == ()

    led = CarryLedger(tmp_path / "soft_hard.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("BLURUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("0.1"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("BLUR-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)

    now_s = 1_800_000_000
    led.create_position(
        position_id="BLUR-1", symbol="BLUR", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=now_s - 600,
        entry_l=100.0, entry_s=100.0, entry_basis_bps=0.0, target_base_qty=D("1.0"),
    )
    led.update_position_status("BLUR-1", status="OPEN", ts_s=now_s - 590, long_base_qty=D("1.0"), short_base_qty=D("1.0"))
    led.register_order_intent(
        client_order_id="op-l", position_id="BLUR-1", venue="BYBIT", instrument_id=spec_l.instrument_id,
        side="BUY", role="MAKER_OPEN", order_type="POST_ONLY", qty_contracts=D("1.0"), base_qty=D("1.0"), price=D("100"), ts_s=now_s - 595,
    )
    led.update_order_state("op-l", status="FILLED", filled_base_qty=D("1.0"), fill_price=D("100"), venue_order_id="O1", ts_s=now_s - 594)
    led.register_order_intent(
        client_order_id="op-s", position_id="BLUR-1", venue="HL", instrument_id=spec_s.instrument_id,
        side="SELL", role="HEDGE_OPEN", order_type="IOC", qty_contracts=D("1.0"), base_qty=D("1.0"), price=D("100"), ts_s=now_s - 593,
    )
    led.update_order_state("op-s", status="FILLED", filled_base_qty=D("1.0"), fill_price=D("100"), venue_order_id="O2", ts_s=now_s - 592)

    quotes = {spec_l.instrument_id: (79.9, 80.1), spec_s.instrument_id: (79.8, 80.2)}
    mids = {spec_l.instrument_id: 80.0, spec_s.instrument_id: 80.0}
    assert ctrl.start_sliced_close("BLUR-1", specs=specs, quotes=quotes, mids=mids, reason="soft_margin_guard_20pct", now_s=now_s) is True
    assert led.active_positions()["BLUR-1"]["status"] == "CLOSING"
    assert led.active_positions()["BLUR-1"]["close_reason"] == "soft_margin_guard_20pct"
    ctrl._slice_attempts["BLUR-1"] = 2
    # Repeat soft_margin_guard call while CLOSING does not reset _slice_attempts
    ctrl.start_sliced_close("BLUR-1", specs=specs, quotes=quotes, mids=mids, reason="soft_margin_guard_20pct", now_s=now_s + 1)
    assert ctrl._slice_attempts["BLUR-1"] == 2

    # Hard 25% emergency close cancels closing maker cleanly and flattens via MARKET
    ctrl.emergency_close_position("BLUR-1", specs=specs, reason="margin_guard_25pct", now_s=now_s + 2, live_mids=mids)
    mk_coid = next(rec["client_order_id"] for rec in gw.submitted if rec["order_type"] == "POST_ONLY")
    gw.deliver_cancel(mk_coid, filled_base_qty=D("0"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 3, live_quotes=quotes)
    for rec in list(gw.submitted):
        if rec["order_type"] == "MARKET":
            gw.deliver_fill(rec["client_order_id"], filled_base_qty=rec["qty_contracts"], avg_fill_price=D("75.0"))
    ctrl.poll_active_orders(live_mids=mids, now_s=now_s + 4, live_quotes=quotes)
    row = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id='BLUR-1'").fetchone())
    assert row["status"] == "CLOSED" and row["close_reason"] == "margin_guard_25pct"
    led.close()


def test_basis_stop_via_closing_vs_legacy_runtime_flag(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [1_800_000_000.0]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    spec_l = InstrumentSpec("BLURUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("1"), price_increment=D("0.0001"))
    spec_s = InstrumentSpec("BLUR-USD-PERP.HYPERLIQUID", "HL", size_increment=D("1"), price_increment=D("0.0001"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}

    for via_closing in (True, False):
        plan_base = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
        plan = replace(
            plan_base,
            state_path=tmp_path / f"basis_{via_closing}.sqlite3",
            risk=replace(plan_base.risk, basis_stop_via_closing=via_closing, basis_persist_mins=5),
        )
        led = CarryLedger(plan.state_path, plan.fingerprint(), ledger_domain="paper")
        strat = _create_carry_strategy(plan, led, mode="paper")
        strat._specs_from_cache = lambda: specs
        led.create_position(
            position_id="B-1", symbol="BLUR", long_venue="BYBIT", short_venue="HL",
            iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=int(clock[0]) - 600,
            entry_l=0.10, entry_s=0.10, entry_basis_bps=0.0, target_base_qty=D("1000"),
        )
        led.update_position_status("B-1", status="OPEN", ts_s=int(clock[0]) - 590, long_base_qty=D("1000"), short_base_qty=D("1000"))

        # +300bp adverse basis -> periodic 30s samples across 5m window (within max_gap_secs)
        for step_s in range(0, 330, 30):
            t_val = 1_800_000_000.0 + step_s
            clock[0] = t_val
            strat.live_mids[spec_l.instrument_id] = 0.0970
            strat.live_mids[spec_s.instrument_id] = 0.1000
            strat.live_mid_ts[spec_l.instrument_id] = t_val
            strat.live_mid_ts[spec_s.instrument_id] = t_val
            strat.live_quotes[spec_l.instrument_id] = (0.0969, 0.0971)
            strat.live_quotes[spec_s.instrument_id] = (0.0999, 0.1001)
            strat.live_quote_ts[spec_l.instrument_id] = t_val
            strat.live_quote_ts[spec_s.instrument_id] = t_val
            strat._on_basis_timer(None)

        pos_row = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id='B-1'").fetchone())
        roles = [r["role"] for r in led.conn.execute("SELECT role FROM orders WHERE position_id='B-1'").fetchall()]
        if via_closing:
            assert pos_row["status"] == "CLOSING" and pos_row["close_reason"] == "basis_stop_5m"
            assert "MAKER_CLOSE" in roles
            assert not any(r.startswith("EMERGENCY_CLOSE") for r in roles)
        else:
            assert pos_row["status"] == "CLOSED" and pos_row["close_reason"] == "basis_stop_5m"
            assert any(r.startswith("EMERGENCY_CLOSE") for r in roles)
        led.close()


def test_close_vacuum_gate_defers_cancels_persists_deadline_and_bypasses_imbalance(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "vacuum.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("BLURUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("1"), price_increment=D("0.0001"))
    spec_s = InstrumentSpec("BLUR-USD-PERP.HYPERLIQUID", "HL", size_increment=D("1"), price_increment=D("0.0001"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)

    t0 = 1_800_000_000
    # $100 notional ($50 slice) -> 2000 qty @ 0.05
    led.create_position(
        position_id="VAC-1", symbol="BLUR", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 1000,
        entry_l=0.05, entry_s=0.05, entry_basis_bps=0.0, target_base_qty=D("2000"),
    )
    led.update_position_status("VAC-1", status="CLOSING", ts_s=t0, long_base_qty=D("2000"), short_base_qty=D("2000"), close_reason="soft_margin_guard_20pct")

    # 1. DEX (HL) is Maker so CEX (BYBIT) is Hedge: Hedge spread 500bp > 150bp -> defers maker, attempts stays 0
    wide_hedge_quotes = {spec_l.instrument_id: (0.0480, 0.0505), spec_s.instrument_id: (0.0499, 0.0501)}
    mids = {spec_l.instrument_id: 0.04925, spec_s.instrument_id: 0.05}
    submitted = ctrl.advance_pending_slices(specs=specs, quotes=wide_hedge_quotes, mids=mids, now_s=t0)
    assert submitted == []
    assert len(gw.submitted) == 0
    assert ctrl._slice_attempts.get("VAC-1", 0) == 0
    assert led.get_close_vacuum_defer_ts("VAC-1") == t0

    # 2. Restart controller cleanly -> reads persisted started_ts == t0 from SQLite events
    ctrl2 = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)
    assert ctrl2._get_close_vacuum_defer_ts("VAC-1") == t0

    # 3. Tight BYBIT quotes admit HL maker -> BYBIT then widens > 150bp while HL maker rests -> canceled without incrementing attempts
    tight_quotes = {spec_l.instrument_id: (0.0499, 0.0501), spec_s.instrument_id: (0.0499, 0.0501)}
    assert ctrl2.advance_pending_slices(specs=specs, quotes=tight_quotes, mids=mids, now_s=t0 + 10) == ["VAC-1"]
    mk_coid = gw.submitted[-1]["client_order_id"]
    ctrl2.poll_active_orders(live_mids=mids, now_s=t0 + 26, live_quotes=wide_hedge_quotes)
    gw.deliver_cancel(mk_coid, filled_base_qty=D("0"))
    timed_out = ctrl2.poll_active_orders(live_mids=mids, now_s=t0 + 27, live_quotes=wide_hedge_quotes)
    assert timed_out == []
    assert ctrl2._slice_attempts.get("VAC-1", 0) == 0

    # 4. At 180s deadline expiry, even with > 150bp spread on BOTH legs, forces multi-slice dual-taker close
    both_wide = {spec_l.instrument_id: (0.0480, 0.0505), spec_s.instrument_id: (0.0480, 0.0505)}
    assert ctrl2.advance_pending_slices(specs=specs, quotes=both_wide, mids=mids, now_s=t0 + 180) == ["VAC-1"]
    assert [r["order_type"] for r in gw.submitted[-2:]] == ["IOC", "IOC"]
    for rec in gw.submitted[-2:]:
        gw.deliver_fill(rec["client_order_id"], filled_base_qty=rec["qty_contracts"], avg_fill_price=D("0.0490"))
    ctrl2.poll_active_orders(live_mids=mids, now_s=t0 + 181, live_quotes=both_wide)
    assert ctrl2.advance_pending_slices(specs=specs, quotes=both_wide, mids=mids, now_s=t0 + 182) == ["VAC-1"]
    assert gw.submitted[-2]["order_type"] == "IOC" and gw.submitted[-1]["order_type"] == "IOC"

    # 5. Imbalanced CLOSING position NEVER defers even at 2000bp spread
    led.create_position(
        position_id="VAC-IMB", symbol="SOL", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 1000,
        entry_l=0.05, entry_s=0.05, entry_basis_bps=0.0, target_base_qty=D("1000"),
    )
    led.update_position_status("VAC-IMB", status="CLOSING", ts_s=t0, long_base_qty=D("1000"), short_base_qty=D("0"), close_reason="soft_margin_guard_20pct")
    mega_wide = {spec_l.instrument_id: (0.0400, 0.0500), spec_s.instrument_id: (0.0400, 0.0500)}
    assert ctrl2.advance_pending_slices(specs=specs, quotes=mega_wide, mids=mids, now_s=t0) == ["VAC-IMB"]
    assert gw.submitted[-1]["order_type"] == "IOC"
    assert led.get_close_vacuum_defer_ts("VAC-IMB") is None

    # 6. Missing BBO during use_dual_taker defers instead of substituting live_mids until 180s expiry
    led.create_position(
        position_id="VAC-MISS", symbol="DOGE", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 1000,
        entry_l=0.05, entry_s=0.05, entry_basis_bps=0.0, target_base_qty=D("1000"),
    )
    led.update_position_status("VAC-MISS", status="CLOSING", ts_s=t0, long_base_qty=D("1000"), short_base_qty=D("1000"), close_reason="soft_margin_guard_20pct")
    ctrl2._slice_attempts["VAC-MISS"] = plan.runtime.maker_max_attempts
    partial_quotes = {spec_l.instrument_id: (0.0499, 0.0501)}
    assert ctrl2.advance_pending_slices(specs=specs, quotes=partial_quotes, mids=mids, now_s=t0) == []
    assert led.get_close_vacuum_defer_ts("VAC-MISS") == t0
    assert ctrl2.advance_pending_slices(specs=specs, quotes=partial_quotes, mids=mids, now_s=t0 + 180) == ["VAC-MISS"]
    assert gw.submitted[-1]["order_type"] == "IOC"
    led.close()


def test_carry_inversion_tracker_glitch_exemption_and_min_hold_bypass(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    tr = CarryInversionTracker(inversion_stop_bps_h=D("3.0"), persist_mins=30, max_gap_secs=180)
    t0 = 1_800_000_000
    assert tr.observe(position_id="P", live_carry_bps_h=-3.5, now_s=t0) == (False, 0)
    assert tr.observe(position_id="P", live_carry_bps_h=-3.5, now_s=t0 + 60) == (False, 60)
    # 1st isolated healthy sample exempted, does not advance last_breach_ts
    assert tr.observe(position_id="P", live_carry_bps_h=1.0, now_s=t0 + 120) == (False, 60)
    assert tr.observe(position_id="P", live_carry_bps_h=-3.5, now_s=t0 + 180) == (False, 180)
    # 2nd healthy sample within same streak resets completely
    assert tr.observe(position_id="P", live_carry_bps_h=0.5, now_s=t0 + 240) == (False, 0)
    assert tr.observe(position_id="P", live_carry_bps_h=-3.5, now_s=t0 + 300) == (False, 0)
    # Continuous >= 30m breach (sampled every 120s <= max_gap_secs) fires
    for offset in range(120, 1800, 120):
        assert tr.observe(position_id="P", live_carry_bps_h=-3.0, now_s=t0 + 300 + offset) == (False, offset)
    assert tr.observe(position_id="P", live_carry_bps_h=-3.0, now_s=t0 + 300 + 1800) == (True, 1800)

    # Runtime wiring: bypasses min_hold_h=6 (position opened only 1h ago)
    plan = replace(load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml"), state_path=tmp_path / "inv_rt.sqlite3")
    led = CarryLedger(plan.state_path, plan.fingerprint(), ledger_domain="paper")
    clock = [float(t0)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    strat = _create_carry_strategy(plan, led, mode="paper")
    spec_l = InstrumentSpec("BLURUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("1"), price_increment=D("0.0001"))
    spec_s = InstrumentSpec("BLUR-USD-PERP.HYPERLIQUID", "HL", size_increment=D("1"), price_increment=D("0.0001"))
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    strat._specs_from_cache = lambda: specs
    led.create_position(
        position_id="INV-1", symbol="BLUR", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 3600,
        entry_l=0.10, entry_s=0.10, entry_basis_bps=0.0, target_base_qty=D("1000"),
    )
    led.update_position_status("INV-1", status="OPEN", ts_s=t0 - 3590, long_base_qty=D("1000"), short_base_qty=D("1000"))
    strat.live_quotes[spec_l.instrument_id] = (0.0999, 0.1001)
    strat.live_quotes[spec_s.instrument_id] = (0.0999, 0.1001)
    strat.live_quote_ts[spec_l.instrument_id] = clock[0]
    strat.live_quote_ts[spec_s.instrument_id] = clock[0]
    strat.live_mids[spec_l.instrument_id] = 0.10
    strat.live_mids[spec_s.instrument_id] = 0.10

    for step in range(0, 1860, 120):
        cur_t = t0 + step
        clock[0] = float(cur_t)
        strat.live_mids[spec_l.instrument_id] = 0.10
        strat.live_mids[spec_s.instrument_id] = 0.10
        strat.live_mid_ts[spec_l.instrument_id] = clock[0]
        strat.live_mid_ts[spec_s.instrument_id] = clock[0]
        strat.live_quote_ts[spec_l.instrument_id] = clock[0]
        strat.live_quote_ts[spec_s.instrument_id] = clock[0]
        ll = replace(
            _make_leg("BYBIT", "BLUR", interval_h=8, next_settle_ts=t0 + 8 * HOUR, r8=1.0, r24=1.0, cur_rate=2.0, bid=0.0999, ask=0.1001),
            fetched_ts=cur_t,
        )
        ls = replace(
            _make_leg("HL", "BLUR", interval_h=1, next_settle_ts=cur_t + 1800, r8=1.0, r24=1.0, cur_rate=-2.0, bid=0.0999, ask=0.1001),
            fetched_ts=cur_t,
        )
        strat._apply_slow_snapshot({"BLUR": {"BYBIT": ll, "HL": ls}}, poll_ts_s=cur_t)

    row = dict(led.conn.execute("SELECT status, close_reason FROM positions WHERE position_id='INV-1'").fetchone())
    assert row["status"] == "CLOSING" and row["close_reason"] == "carry_inversion_30m"
    led.close()


def test_dual_leg_net_cex_exit_defer_and_single_pin() -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    t0 = 1_800_000_000
    byb_settle = t0 + 900  # 15m <= 20m defer window

    # Case 1: BYBIT long receives 8h*4bp = +32bp, but HL short pays 1h*40bp = -40bp -> net -8bp <= 0 -> exit now
    ll_recv = _make_leg("BYBIT", "AAA", interval_h=8, next_settle_ts=byb_settle, r8=-4.0, r24=-4.0, cur_rate=-4.0)
    ls_bleed = _make_leg("HL", "AAA", interval_h=1, next_settle_ts=t0 + 600, r8=-5.0, r24=-5.0, cur_rate=-40.0)
    assert should_exit_carry_decay(opened_ts=t0 - 10 * HOUR, leg_l=ll_recv, leg_s=ls_bleed, policy=plan.policy, now_s=t0) == (True, "carry_decay")

    # Case 2: net +31bp > 0 -> defer and pin byb_settle; mid-wait flip to net <= 0 cancels defer immediately
    ls_mild = _make_leg("HL", "AAA", interval_h=1, next_settle_ts=t0 + 600, r8=-5.0, r24=-5.0, cur_rate=-1.0)
    pinned = select_cex_exit_defer_ts(ll_recv, ls_mild, plan.policy, t0)
    assert pinned == byb_settle
    assert should_exit_carry_decay(
        opened_ts=t0 - 10 * HOUR, leg_l=ll_recv, leg_s=ls_mild, policy=plan.policy, now_s=t0 + 60, pinned_defer_ts=pinned
    ) == (False, "defer_collect_cex_settlement")
    assert should_exit_carry_decay(
        opened_ts=t0 - 10 * HOUR, leg_l=ll_recv, leg_s=ls_bleed, policy=plan.policy, now_s=t0 + 120, pinned_defer_ts=pinned
    ) == (True, "carry_decay")

    # Case 3: once pinned_defer_ts passes, never defers a second time even if another CEX leg is in window
    ll_rolled = _make_leg("BYBIT", "AAA", interval_h=8, next_settle_ts=byb_settle + 8 * HOUR, r8=-1.0, r24=-1.0, cur_rate=-1.0)
    okx_short_recv = _make_leg("OKX", "AAA", interval_h=8, next_settle_ts=byb_settle + 600, r8=-0.9, r24=-0.9, cur_rate=4.0)
    assert should_exit_carry_decay(
        opened_ts=t0 - 10 * HOUR, leg_l=ll_rolled, leg_s=okx_short_recv, policy=plan.policy, now_s=byb_settle + 10, pinned_defer_ts=pinned
    ) == (True, "carry_decay")


def test_closing_escalation_shutdown_preserve_and_clean_restart(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    assert close_reason_priority("carry_decay") < close_reason_priority("carry_inversion_30m") < close_reason_priority("basis_stop_30m") < close_reason_priority("soft_margin_guard_20pct") < close_reason_priority("margin_guard_25pct")
    assert not is_risk_close_reason("carry_decay")
    assert all(is_risk_close_reason(r) for r in ("carry_inversion_30m", "basis_stop_30m", "soft_margin_guard_20pct", "margin_guard_25pct"))

    led = CarryLedger(tmp_path / "lifecycle.sqlite3", plan.fingerprint(), ledger_domain="paper")
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOLUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("0.01"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan.risk, runtime=plan.runtime, policy=plan.policy)

    t0 = 1_800_000_000
    quotes = {spec_l.instrument_id: (99.95, 100.05), spec_s.instrument_id: (100.15, 100.25)}
    mids = {spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.2}

    # OPENING with resting maker cancels cleanly on transition to risk CLOSING
    led.create_position(
        position_id="P-ESC", symbol="SOL", long_venue="HL", short_venue="BYBIT",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 100,
        entry_l=100.0, entry_s=100.2, entry_basis_bps=20.0, target_base_qty=D("1.00"),
    )
    led.update_position_status("P-ESC", status="OPEN", ts_s=t0 - 50, long_base_qty=D("1.00"), short_base_qty=D("1.00"))
    ctrl.start_sliced_close("P-ESC", specs=specs, quotes=quotes, mids=mids, reason="carry_decay", now_s=t0)
    ctrl.start_sliced_close("P-ESC", specs=specs, quotes=quotes, mids=mids, reason="carry_inversion_30m", now_s=t0 + 1)
    assert led.active_positions()["P-ESC"]["close_reason"] == "carry_inversion_30m"
    ctrl.start_sliced_close("P-ESC", specs=specs, quotes=quotes, mids=mids, reason="basis_stop_30m", now_s=t0 + 2)
    ctrl.start_sliced_close("P-ESC", specs=specs, quotes=quotes, mids=mids, reason="soft_margin_guard_20pct", now_s=t0 + 3)
    ctrl.start_sliced_close("P-ESC", specs=specs, quotes=quotes, mids=mids, reason="carry_decay", now_s=t0 + 4)
    assert led.active_positions()["P-ESC"]["close_reason"] == "soft_margin_guard_20pct"

    # Separate carry_decay CLOSING position reverts to OPEN on shutdown while risk CLOSING is preserved
    led.create_position(
        position_id="P-DECAY", symbol="ETH", long_venue="HL", short_venue="BYBIT",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 100,
        entry_l=100.0, entry_s=100.2, entry_basis_bps=20.0, target_base_qty=D("0.50"),
    )
    led.update_position_status("P-DECAY", status="CLOSING", ts_s=t0, long_base_qty=D("0.50"), short_base_qty=D("0.50"), close_reason="carry_decay")

    ctrl.begin_shutdown(now_s=t0 + 10, live_mids=mids, live_quotes=quotes)
    mk_coid = gw.submitted[-1]["client_order_id"]
    gw.deliver_cancel(mk_coid, filled_base_qty=D("0"))
    ctrl.poll_active_orders(live_mids=mids, now_s=t0 + 11, live_quotes=quotes)
    ctrl.reconcile_shutdown_positions(specs=specs, now_s=t0 + 12, live_mids=mids)
    assert led.active_positions()["P-ESC"]["status"] == "CLOSING"
    assert led.active_positions()["P-ESC"]["close_reason"] == "soft_margin_guard_20pct"
    assert led.active_positions()["P-DECAY"]["status"] == "OPEN"
    evt_kinds = [r["kind"] for r in led.conn.execute("SELECT kind FROM events").fetchall()]
    assert "closing_reverted_to_open" in evt_kinds
    ready, blockers = ctrl.is_shutdown_ready(specs=specs, live_mids=mids)
    assert ready and blockers == []

    # Clean balanced risk CLOSING resumes on startup; non-risk CLOSING still halts
    vp = {("HL", spec_l.instrument_id): D("1.50"), ("BYBIT", spec_s.instrument_id): D("-1.50")}
    assert led.reconcile_startup(venue_positions=vp, venue_open_orders={}, ts_s=t0 + 20).ok is True
    led.update_position_status("P-DECAY", status="CLOSING", ts_s=t0 + 21, close_reason="carry_decay")
    res_bad = led.reconcile_startup(venue_positions=vp, venue_open_orders={}, ts_s=t0 + 22)
    assert not res_bad.ok and any("in_flight_position:P-DECAY:CLOSING" in d for d in res_bad.discrepancies)
    led.close()


def test_stratified_cooldown_2h_margin_guard_vs_24h_severe(tmp_path: Path) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    led = CarryLedger(tmp_path / "cooldown.sqlite3", plan.fingerprint(), ledger_domain="paper")
    t0 = 1_800_000_000
    for pid, sym, reason in (
        ("P-MG", "BLUR", "margin_guard_25pct"),
        ("P-SMG", "AAVE", "soft_margin_guard_20pct"),
        ("P-BS", "DOGE", "basis_stop_30m"),
        ("P-INV", "PEPE", "carry_inversion_30m"),
        ("P-UNH", "SOL", "unhedged_timeout_kill"),
    ):
        led.create_position(
            position_id=pid, symbol=sym, long_venue="HL", short_venue="BYBIT",
            iid_long=f"{sym}-USD-PERP.HYPERLIQUID", iid_short=f"{sym}USDT-LINEAR.BYBIT",
            opened_ts=t0 - 1000, entry_l=1.0, entry_s=1.0, entry_basis_bps=0.0, target_base_qty=D("10"),
        )
        led.update_position_status(pid, status="CLOSED", ts_s=t0, realized_pnl_usd=D("-1.0"), close_reason=reason)

    cd_1h59 = led.stop_loss_cooldown_symbols(now_s=t0 + 7140, cooldown_hours=24, margin_guard_cooldown_hours=2)
    assert cd_1h59 == {"BLUR", "AAVE", "DOGE", "PEPE", "SOL"}
    cd_exact = led.stop_loss_cooldown_symbols(now_s=t0 + 7200, cooldown_hours=24, margin_guard_cooldown_hours=2)
    assert cd_exact == {"DOGE", "PEPE", "SOL"}
    cd_2h01 = led.stop_loss_cooldown_symbols(now_s=t0 + 7260, cooldown_hours=24, margin_guard_cooldown_hours=2)
    assert cd_2h01 == {"DOGE", "PEPE", "SOL"}
    cd_24h01 = led.stop_loss_cooldown_symbols(now_s=t0 + 24 * 3600 + 60, cooldown_hours=24, margin_guard_cooldown_hours=2)
    assert cd_24h01 == set()
    led.close()


def test_replay_pr2_soft_guard_vacuum_inversion_and_cooldown(tmp_path: Path) -> None:
    tape_dir = tmp_path / "tape_pr2"
    tape_dir.mkdir()
    t0 = 1_800_050_400
    byb_iid, hl_iid = "BLURUSDT-LINEAR.BYBIT", "BLUR-USD-PERP.HYPERLIQUID"

    def _row(sym: str, v: str, iid: str, ih: int, ns: int, r8: float, cr: float, b: float, a: float) -> dict:
        return {
            "s": sym, "v": v, "ac": "CRYPTO", "iid": iid, "ih": ih, "ns": ns,
            "r8": r8, "r24": r8, "cr": cr, "mp": 0.5 * (b + a), "b": b, "a": a,
            "v24": 2_000_000.0, "v1": 50_000.0,
        }

    # Open BLUR at t0, trigger soft_margin_guard_20pct at t0+30 while HL ask is 2000bp vacuum (maker on HL times out 4x
    # without crossing, then dual-taker close is deferred by HL vacuum gate until HL heals at t0+110), then re-enter after 2h cooldown -> inversion stop after 30m!
    slow_rows = [
        {"ts": t0, "legs": [_row("BLUR", "BYBIT", byb_iid, 8, t0 + 8 * HOUR, 0.0, 0.0, 0.9998, 1.0002), _row("BLUR", "HL", hl_iid, 1, t0 + HOUR, 4.0, 4.0, 1.0018, 1.0022)]},
        {"ts": t0 + 7350, "legs": [_row("BLUR", "BYBIT", byb_iid, 8, t0 + 8 * HOUR, 0.0, 0.0, 0.7998, 0.8002), _row("BLUR", "HL", hl_iid, 1, t0 + 3 * HOUR, 4.0, 4.0, 0.8018, 0.8022)]},
    ]
    for step_s in range(t0 + 7410, t0 + 9400, 60):
        slow_rows.append(
            {
                "ts": step_s,
                "legs": [
                    _row("BLUR", "BYBIT", byb_iid, 8, t0 + 16 * HOUR, 4.0, 2.0, 0.7998, 0.8002),
                    _row("BLUR", "HL", hl_iid, 1, step_s + 1800, -4.0, -3.0, 0.8008, 0.8012),
                ],
            }
        )
    (tape_dir / "slow_snapshots_20270115.jsonl.gz").write_bytes(
        gzip.compress("".join(json.dumps(r) + "\n" for r in slow_rows).encode("utf-8"))
    )

    # Wait 100ms past 2h cooldown -> BLUR re-enters at t0+7350; entry fill uses SELL on HL >= 1.0022 / 1.0025
    fast_rows = [
        {"ts": t0 + 0.5, "iid": byb_iid, "b": 0.9998, "a": 1.0002, "m": 1.0000},
        {"ts": t0 + 1.0, "iid": hl_iid, "b": 1.0022, "a": 1.0025, "m": 1.00235},
        {"ts": t0 + 2.0, "iid": hl_iid, "b": 1.0025, "a": 1.0028, "m": 1.00265},
        # ~20.5% crash (0.795 vs 1.0) with 2000bp vacuum on HL ask: 4x maker timeout -> dual-taker blocked by HL vacuum gate!
        {"ts": t0 + 30.0, "iid": byb_iid, "b": 0.7948, "a": 0.7952, "m": 0.7950},
        {"ts": t0 + 30.1, "iid": hl_iid, "b": 0.7930, "a": 0.9600, "m": 0.8765},
        {"ts": t0 + 46.0, "iid": byb_iid, "b": 0.7948, "a": 0.7952, "m": 0.7950},
        {"ts": t0 + 46.1, "iid": hl_iid, "b": 0.7930, "a": 0.9600, "m": 0.8765},
        {"ts": t0 + 62.0, "iid": byb_iid, "b": 0.7948, "a": 0.7952, "m": 0.7950},
        {"ts": t0 + 62.1, "iid": hl_iid, "b": 0.7930, "a": 0.9600, "m": 0.8765},
        {"ts": t0 + 78.0, "iid": byb_iid, "b": 0.7948, "a": 0.7952, "m": 0.7950},
        {"ts": t0 + 78.1, "iid": hl_iid, "b": 0.7930, "a": 0.9600, "m": 0.8765},
        {"ts": t0 + 94.0, "iid": byb_iid, "b": 0.7948, "a": 0.7952, "m": 0.7950},
        {"ts": t0 + 94.1, "iid": hl_iid, "b": 0.7930, "a": 0.9600, "m": 0.8765},
        # HL ask heals at t0+110 and closes remaining position cleanly
        {"ts": t0 + 110.0, "iid": byb_iid, "b": 0.7998, "a": 0.8002, "m": 0.8000},
        {"ts": t0 + 110.1, "iid": hl_iid, "b": 0.7995, "a": 0.8005, "m": 0.8000},
        {"ts": t0 + 111.0, "iid": hl_iid, "b": 0.7995, "a": 0.8005, "m": 0.8000},
        # Re-entry fills around t0+7350 (>2h after t0+111) then closes on inversion at t0+9210
        {"ts": t0 + 7350.5, "iid": byb_iid, "b": 0.7998, "a": 0.8002, "m": 0.8000},
        {"ts": t0 + 7351.0, "iid": hl_iid, "b": 0.8022, "a": 0.8025, "m": 0.80235},
        {"ts": t0 + 7352.0, "iid": hl_iid, "b": 0.8025, "a": 0.8028, "m": 0.80265},
        {"ts": t0 + 9211.0, "iid": byb_iid, "b": 0.7998, "a": 0.8002, "m": 0.8000},
        {"ts": t0 + 9211.1, "iid": hl_iid, "b": 0.7995, "a": 0.7999, "m": 0.7997},
        {"ts": t0 + 9212.0, "iid": hl_iid, "b": 0.7993, "a": 0.7995, "m": 0.7994},
        {"ts": t0 + 9213.0, "iid": hl_iid, "b": 0.7990, "a": 0.7993, "m": 0.79915},
    ]
    (tape_dir / "fast_bbo_20270115.jsonl.gz").write_bytes(
        gzip.compress("".join(json.dumps(r) + "\n" for r in fast_rows).encode("utf-8"))
    )

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    rep = replay_tape(tape_dir=tape_dir, plan=plan, execution_mode="hybrid")
    assert rep["diagnostics"]["close_vacuum_defers"] >= 1
    reasons = [t["close_reason"] for t in rep["trades"]]
    assert "soft_margin_guard_20pct" in reasons
    assert "carry_inversion_30m" in reasons


def test_codex_edge_fixes_decimal_guard_missing_bbo_maker_expiry_and_phase_safety(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    plan_base = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")

    # 1. Exact Decimal 20% loss (1.0 -> 0.8) and fractional percentage formatting (20.5%)
    frac_risk = replace(plan_base.risk, soft_margin_guard_pct=D("20.5"))
    ev_20 = FastRiskEvaluator(plan_base.risk)
    pos_dec = {
        "P-EXACT": {
            "symbol": "BLUR",
            "long_venue": "BYBIT",
            "short_venue": "HL",
            "iid_long": "BLURUSDT-LINEAR.BYBIT",
            "iid_short": "BLUR-USD-PERP.HYPERLIQUID",
            "entry_l": 1.0,
            "entry_s": 1.0,
            "long_base_qty": "10",
            "short_base_qty": "10",
        }
    }
    dec_exact = ev_20.evaluate(
        positions=pos_dec,
        live_mids={"BLURUSDT-LINEAR.BYBIT": 0.8, "BLUR-USD-PERP.HYPERLIQUID": 0.8},
        daily_realized_pnl_usd=D("0"),
        consecutive_unhedged=0,
        now_s=1_800_000_000,
    )
    assert dec_exact.soft_close_positions == (("P-EXACT", "soft_margin_guard_20pct"),)
    dec_frac = FastRiskEvaluator(frac_risk).evaluate(
        positions=pos_dec,
        live_mids={"BLURUSDT-LINEAR.BYBIT": 0.795, "BLUR-USD-PERP.HYPERLIQUID": 0.795},
        daily_realized_pnl_usd=D("0"),
        consecutive_unhedged=0,
        now_s=1_800_000_000,
    )
    assert dec_frac.soft_close_positions == (("P-EXACT", "soft_margin_guard_20.5pct"),)

    # 2. Missing first BBO at attempt 0 starts 180s timer & forces close; exposed single-leg closes without flat-leg quote
    led = CarryLedger(tmp_path / "codex_exec.sqlite3", plan_base.fingerprint(), ledger_domain="paper")
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("BLURUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("1"), price_increment=D("0.0001"))
    spec_s = InstrumentSpec("BLUR-USD-PERP.HYPERLIQUID", "HL", size_increment=D("1"), price_increment=D("0.0001"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan_base.risk, runtime=plan_base.runtime, policy=plan_base.policy)
    t0 = 1_800_000_000
    mids = {spec_l.instrument_id: 0.05, spec_s.instrument_id: 0.05}

    led.create_position(
        position_id="MISS-FIRST", symbol="BLUR", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 100,
        entry_l=0.05, entry_s=0.05, entry_basis_bps=0.0, target_base_qty=D("1000"),
    )
    led.update_position_status("MISS-FIRST", status="CLOSING", ts_s=t0, long_base_qty=D("1000"), short_base_qty=D("1000"), close_reason="soft_margin_guard_20pct")
    assert ctrl.advance_pending_slices(specs=specs, quotes={spec_l.instrument_id: (0.0499, 0.0501)}, mids=mids, now_s=t0) == []
    assert led.get_close_vacuum_defer_ts("MISS-FIRST") == t0
    assert ctrl.advance_pending_slices(specs=specs, quotes={spec_l.instrument_id: (0.0499, 0.0501)}, mids=mids, now_s=t0 + 180) == ["MISS-FIRST"]
    assert [r["order_type"] for r in gw.submitted[-2:]] == ["IOC", "IOC"]
    for rec in gw.submitted[-2:]:
        gw.deliver_fill(rec["client_order_id"], filled_base_qty=rec["qty_contracts"], avg_fill_price=D("0.05"))
    ctrl.poll_active_orders(live_mids=mids, now_s=t0 + 181, live_quotes={})

    # Flat short leg + missing short quote/mid still closes exposed long leg immediately
    led.create_position(
        position_id="ONE-LEG", symbol="SOL", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 100,
        entry_l=0.05, entry_s=0.05, entry_basis_bps=0.0, target_base_qty=D("500"),
    )
    led.update_position_status("ONE-LEG", status="CLOSING", ts_s=t0, long_base_qty=D("500"), short_base_qty=D("0"), close_reason="soft_margin_guard_20pct")
    assert ctrl.advance_pending_slices(specs=specs, quotes={spec_l.instrument_id: (0.0499, 0.0501)}, mids={spec_l.instrument_id: 0.05}, now_s=t0) == ["ONE-LEG"]
    assert gw.submitted[-1]["side"] == "SELL" and gw.submitted[-1]["order_type"] == "IOC"
    gw.deliver_fill(gw.submitted[-1]["client_order_id"], filled_base_qty=gw.submitted[-1]["qty_contracts"], avg_fill_price=D("0.05"))
    ctrl.poll_active_orders(live_mids=mids, now_s=t0 + 1, live_quotes={})

    # 3. Resting closing maker admitted at t+179 is canceled immediately at t+180 deadline expiry
    led.create_position(
        position_id="EXP-MK", symbol="ETH", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 100,
        entry_l=0.05, entry_s=0.05, entry_basis_bps=0.0, target_base_qty=D("1000"),
    )
    led.update_position_status("EXP-MK", status="CLOSING", ts_s=t0, long_base_qty=D("1000"), short_base_qty=D("1000"), close_reason="soft_margin_guard_20pct")
    wide_hedge = {spec_l.instrument_id: (0.0450, 0.0550), spec_s.instrument_id: (0.0499, 0.0501)}
    tight = {spec_l.instrument_id: (0.0499, 0.0501), spec_s.instrument_id: (0.0499, 0.0501)}
    assert ctrl.advance_pending_slices(specs=specs, quotes=wide_hedge, mids=mids, now_s=t0) == []
    assert ctrl.advance_pending_slices(specs=specs, quotes=tight, mids=mids, now_s=t0 + 179) == ["EXP-MK"]
    mk_exp_coid = gw.submitted[-1]["client_order_id"]
    ctrl.poll_active_orders(live_mids=mids, now_s=t0 + 180, live_quotes=tight)
    assert mk_exp_coid in gw.cancel_requests
    gw.deliver_cancel(mk_exp_coid, filled_base_qty=D("0"))
    ctrl.poll_active_orders(live_mids=mids, now_s=t0 + 181, live_quotes=tight)
    assert ctrl.advance_pending_slices(specs=specs, quotes=wide_hedge, mids=mids, now_s=t0 + 181) == ["EXP-MK"]
    assert gw.submitted[-1]["order_type"] == "IOC"
    led.close()

    # 4. Async opening-order cancel does not increment closing _slice_attempts, and start_sliced_close for A does not open B
    led2 = CarryLedger(tmp_path / "codex_open_close.sqlite3", plan_base.fingerprint(), ledger_domain="paper")
    gw2 = DelayedAsyncVenueGateway()
    gw2.register_spec(spec_l)
    gw2.register_spec(spec_s)
    ctrl2 = CarryExecutionController(ledger=led2, gateway=gw2, risk=plan_base.risk, runtime=plan_base.runtime, policy=plan_base.policy)
    tight_open = {spec_l.instrument_id: (0.9999, 1.0001), spec_s.instrument_id: (1.0014, 1.0016)}
    mids_open = {spec_l.instrument_id: 1.0000, spec_s.instrument_id: 1.0015}
    for pid in ("A-POS", "B-POS"):
        led2.create_position(
            position_id=pid, symbol=pid, long_venue="BYBIT", short_venue="HL",
            iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0,
            entry_l=1.0, entry_s=1.0015, entry_basis_bps=15.0, target_base_qty=D("100"),
        )
        led2.update_position_status(pid, status="OPENING", ts_s=t0, long_base_qty=D("50"), short_base_qty=D("50"))
    assert ctrl2.advance_pending_slices(specs=specs, quotes=tight_open, mids=mids_open, now_s=t0, only_position_id="A-POS") == ["A-POS"]
    open_a_coid = gw2.submitted[-1]["client_order_id"]
    assert gw2.submitted[-1]["order_type"] == "POST_ONLY" and gw2.submitted[-1]["side"] == "SELL"
    ctrl2.start_sliced_close("A-POS", specs=specs, quotes=tight_open, mids=mids_open, reason="soft_margin_guard_20pct", now_s=t0 + 1)
    assert open_a_coid in gw2.cancel_requests
    assert led2.active_positions()["A-POS"]["status"] == "CLOSING"
    assert len(gw2.submitted) == 1
    gw2.deliver_cancel(open_a_coid, filled_base_qty=D("0"))
    ctrl2.poll_active_orders(live_mids=mids_open, now_s=t0 + 2, live_quotes=tight_open)
    assert ctrl2._slice_attempts.get("A-POS", 0) == 0
    assert ctrl2.start_sliced_close("A-POS", specs=specs, quotes=tight_open, mids=mids_open, reason="soft_margin_guard_20pct", now_s=t0 + 2) is True
    assert [r["side"] for r in gw2.submitted] == ["SELL", "BUY"]
    led2.close()


def test_codex_restart_safety_persistent_exit_defer_and_replay_parity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 1. Hard margin_guard_25pct CLOSING halts on startup; 1.5% mismatch soft_margin_guard_20pct survives shutdown & startup
    plan_tol = replace(
        load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml"),
        state_path=tmp_path / "restart_safety.sqlite3",
        risk=replace(load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml").risk, reconcile_tolerance_pct=D("1.0"), max_qty_mismatch_pct=D("2.0")),
    )
    led = CarryLedger(plan_tol.state_path, plan_tol.fingerprint(), ledger_domain="paper")
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.001"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOLUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("0.001"), price_increment=D("0.01"))
    ctrl = CarryExecutionController(ledger=led, gateway=gw, risk=plan_tol.risk, runtime=plan_tol.runtime, policy=plan_tol.policy)
    t0 = 1_800_000_000

    led.create_position(
        position_id="SOFT-MIS", symbol="SOL", long_venue="HL", short_venue="BYBIT",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 100,
        entry_l=100.0, entry_s=100.0, entry_basis_bps=0.0, target_base_qty=D("1.000"),
    )
    led.update_position_status("SOFT-MIS", status="CLOSING", ts_s=t0, long_base_qty=D("1.000"), short_base_qty=D("0.985"), close_reason="soft_margin_guard_20pct")
    mids = {spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.0}
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    ctrl.reconcile_shutdown_positions(specs=specs, now_s=t0 + 1, live_mids=mids)
    assert ctrl.is_shutdown_ready(specs=specs, live_mids=mids) == (True, [])
    vp = {("HL", spec_l.instrument_id): D("1.000"), ("BYBIT", spec_s.instrument_id): D("-0.985")}
    assert (
        led.reconcile_startup(
            venue_positions=vp,
            venue_open_orders={},
            tolerance_pct=plan_tol.risk.reconcile_tolerance_pct,
            max_qty_mismatch_pct=plan_tol.risk.max_qty_mismatch_pct,
            basis_stop_via_closing=plan_tol.risk.basis_stop_via_closing,
            ts_s=t0 + 2,
        ).ok
        is True
    )

    # Interrupting a hard margin_guard_25pct emergency close MUST still halt startup reconciliation
    led.update_position_status("SOFT-MIS", status="CLOSING", ts_s=t0 + 3, close_reason="margin_guard_25pct")
    res_hard = led.reconcile_startup(
        venue_positions=vp,
        venue_open_orders={},
        tolerance_pct=plan_tol.risk.reconcile_tolerance_pct,
        max_qty_mismatch_pct=plan_tol.risk.max_qty_mismatch_pct,
        basis_stop_via_closing=plan_tol.risk.basis_stop_via_closing,
        ts_s=t0 + 4,
    )
    assert not res_hard.ok and any("in_flight_position:SOFT-MIS:CLOSING" in d for d in res_hard.discrepancies)
    led.clear_halt("HALTED_RECONCILE", t0 + 5)

    # Atomic revert_closing_to_open clears prior vacuum defer episode inside same commit
    led.record_close_vacuum_defer_start("SOFT-MIS", reason="soft", spread_bps=D("200"), deadline_secs=180, ts_s=t0 + 5)
    assert led.get_close_vacuum_defer_ts("SOFT-MIS") == t0 + 5
    led.revert_closing_to_open("SOFT-MIS", ts_s=t0 + 6)
    assert led.active_positions()["SOFT-MIS"]["status"] == "OPEN"
    assert led.get_close_vacuum_defer_ts("SOFT-MIS") is None
    led.close()

    # 2. Persistent CEX exit defer pin survives strategy restart (no second deferral)
    plan_pin = replace(load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml"), state_path=tmp_path / "pin_restart.sqlite3")
    led_pin = CarryLedger(plan_pin.state_path, plan_pin.fingerprint(), ledger_domain="paper")
    clock = [float(t0)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    strat1 = _create_carry_strategy(plan_pin, led_pin, mode="paper")
    strat1._specs_from_cache = lambda: specs
    led_pin.create_position(
        position_id="PIN-1", symbol="AAA", long_venue="BYBIT", short_venue="OKX",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 10 * HOUR,
        entry_l=100.0, entry_s=100.0, entry_basis_bps=0.0, target_base_qty=D("1.0"),
    )
    led_pin.update_position_status("PIN-1", status="OPEN", ts_s=t0 - 9 * HOUR, long_base_qty=D("1.0"), short_base_qty=D("1.0"))
    strat1.live_quotes = {spec_l.instrument_id: (99.9, 100.1), spec_s.instrument_id: (99.9, 100.1)}
    strat1.live_quote_ts = {spec_l.instrument_id: clock[0], spec_s.instrument_id: clock[0]}
    strat1.live_mids = {spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.0}
    strat1.live_mid_ts = {spec_l.instrument_id: clock[0], spec_s.instrument_id: clock[0]}

    ll1 = replace(_make_leg("BYBIT", "AAA", interval_h=8, next_settle_ts=t0 + 600, r8=-1.0, r24=-1.0, cur_rate=-3.0), fetched_ts=t0)
    ls1 = replace(_make_leg("OKX", "AAA", interval_h=8, next_settle_ts=t0 + 3600, r8=-0.9, r24=-0.9, cur_rate=2.0), fetched_ts=t0)
    strat1._apply_slow_snapshot({"AAA": {"BYBIT": ll1, "OKX": ls1}}, poll_ts_s=t0)
    assert led_pin.active_positions()["PIN-1"]["status"] == "OPEN"
    assert led_pin.get_exit_defer_target_ts("PIN-1") == t0 + 600

    # Restart strategy after BYBIT settled while OKX is now inside defer window -> must exit, not defer again
    clock[0] = float(t0 + 3000)
    strat2 = _create_carry_strategy(plan_pin, led_pin, mode="paper")
    strat2._specs_from_cache = lambda: specs
    strat2.live_quotes = {spec_l.instrument_id: (99.9, 100.1), spec_s.instrument_id: (99.9, 100.1)}
    strat2.live_quote_ts = {spec_l.instrument_id: clock[0], spec_s.instrument_id: clock[0]}
    strat2.live_mids = {spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.0}
    strat2.live_mid_ts = {spec_l.instrument_id: clock[0], spec_s.instrument_id: clock[0]}
    ll2 = replace(ll1, next_settle_ts=t0 + 600 + 8 * HOUR, fetched_ts=t0 + 3000)
    ls2 = replace(ls1, fetched_ts=t0 + 3000)
    strat2._apply_slow_snapshot({"AAA": {"BYBIT": ll2, "OKX": ls2}}, poll_ts_s=t0 + 3000)
    assert led_pin.active_positions()["PIN-1"]["status"] == "CLOSING"
    assert led_pin.active_positions()["PIN-1"]["close_reason"] == "carry_decay"
    led_pin.close()


def test_codex_round2_fixes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    plan_base = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    t0 = 1_800_050_400

    # 1. emergency_close_position persists hard close_reason BEFORE requesting maker cancel
    plan1 = replace(plan_base, state_path=tmp_path / "emg_order.sqlite3")
    led1 = CarryLedger(plan1.state_path, plan1.fingerprint(), ledger_domain="paper")
    gw1 = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("BLURUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("1"), price_increment=D("0.0001"))
    spec_s = InstrumentSpec("BLUR-USD-PERP.HYPERLIQUID", "HL", size_increment=D("1"), price_increment=D("0.0001"))
    gw1.register_spec(spec_l)
    gw1.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    ctrl1 = CarryExecutionController(ledger=led1, gateway=gw1, risk=plan1.risk, runtime=plan1.runtime, policy=plan1.policy)
    quotes = {spec_l.instrument_id: (0.9999, 1.0001), spec_s.instrument_id: (0.9999, 1.0001)}
    mids = {spec_l.instrument_id: 1.0, spec_s.instrument_id: 1.0}
    led1.create_position(
        position_id="P-EMG", symbol="BLUR", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 100,
        entry_l=1.0, entry_s=1.0, entry_basis_bps=0.0, target_base_qty=D("10"),
    )
    led1.update_position_status("P-EMG", status="OPEN", ts_s=t0 - 90, long_base_qty=D("10"), short_base_qty=D("10"))
    assert ctrl1.start_sliced_close("P-EMG", specs=specs, quotes=quotes, mids=mids, reason="soft_margin_guard_20pct", now_s=t0) is True
    seen_before_cancel: list[str] = []
    orig_cancel = gw1.cancel_order

    def _inspect_cancel(*, client_order_id: str, venue: str, instrument_id: str):
        seen_before_cancel.append(str(led1.active_positions()["P-EMG"]["close_reason"]))
        return orig_cancel(client_order_id=client_order_id, venue=venue, instrument_id=instrument_id)

    gw1.cancel_order = _inspect_cancel  # type: ignore[method-assign]
    ctrl1.emergency_close_position("P-EMG", specs=specs, reason="margin_guard_25pct", now_s=t0 + 1, live_mids=mids)
    assert seen_before_cancel == ["margin_guard_25pct"]
    led1.close()

    # 2. reconcile_shutdown_positions preserves hard margin_guard_25pct CLOSING, reverts carry_decay to OPEN
    led2 = CarryLedger(tmp_path / "shutdown_hard.sqlite3", plan_base.fingerprint(), ledger_domain="paper")
    gw2 = DelayedAsyncVenueGateway()
    gw2.register_spec(spec_l)
    gw2.register_spec(spec_s)
    ctrl2 = CarryExecutionController(ledger=led2, gateway=gw2, risk=plan_base.risk, runtime=plan_base.runtime, policy=plan_base.policy)
    for pid, rsn in (("P-MG", "margin_guard_25pct"), ("P-DECAY", "carry_decay")):
        led2.create_position(
            position_id=pid, symbol=pid, long_venue="BYBIT", short_venue="HL",
            iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 100,
            entry_l=1.0, entry_s=1.0, entry_basis_bps=0.0, target_base_qty=D("10"),
        )
        led2.update_position_status(pid, status="CLOSING", ts_s=t0, long_base_qty=D("10"), short_base_qty=D("10"), close_reason=rsn)
    ctrl2.reconcile_shutdown_positions(specs=specs, now_s=t0 + 2, live_mids=mids)
    assert led2.active_positions()["P-MG"]["status"] == "CLOSING"
    assert led2.active_positions()["P-DECAY"]["status"] == "OPEN"
    assert ctrl2.is_shutdown_ready(specs=specs, live_mids=mids) == (False, ["position_not_settled:P-MG:CLOSING"])
    led2.close()

    # 3. reconcile_startup rejects sliced risk CLOSING when max_unhedged_usd breached OR HALTED_KILL active
    led3 = CarryLedger(tmp_path / "startup_usd_kill.sqlite3", plan_base.fingerprint(), ledger_domain="paper")
    led3.create_position(
        position_id="P-USD", symbol="BLUR", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0 - 100,
        entry_l=1.0, entry_s=1.0, entry_basis_bps=0.0, target_base_qty=D("100"),
    )
    led3.update_position_status(
        "P-USD", status="CLOSING", ts_s=t0, long_base_qty=D("100"), short_base_qty=D("98.5"), close_reason="soft_margin_guard_20pct"
    )
    res_usd = led3.reconcile_startup(
        venue_positions={("BYBIT", spec_l.instrument_id): D("100"), ("HL", spec_s.instrument_id): D("-98.5")},
        venue_open_orders={}, tolerance_pct=D("2.0"), max_qty_mismatch_pct=D("2.0"),
        max_unhedged_usd=D("1.0"), basis_stop_via_closing=True, ts_s=t0 + 1,
    )
    assert not res_usd.ok and any("in_flight_position:P-USD:CLOSING" in d for d in res_usd.discrepancies)
    led3.clear_halt("HALTED_RECONCILE", t0 + 2)
    led3.update_position_status("P-USD", status="CLOSING", ts_s=t0 + 2, long_base_qty=D("100"), short_base_qty=D("100"), close_reason="soft_margin_guard_20pct")
    led3.set_halt("HALTED_KILL", "daily_loss_kill", t0 + 2)
    res_kill = led3.reconcile_startup(
        venue_positions={("BYBIT", spec_l.instrument_id): D("100"), ("HL", spec_s.instrument_id): D("-100")},
        venue_open_orders={}, tolerance_pct=D("2.0"), max_qty_mismatch_pct=D("2.0"),
        max_unhedged_usd=D("60"), basis_stop_via_closing=True, ts_s=t0 + 3,
    )
    assert not res_kill.ok and any("in_flight_position:P-USD:CLOSING" in d for d in res_kill.discrepancies)
    led3.close()

    # 4. exit_defer pin cleared atomically on CLOSING / revert / CLOSED; already-CLOSING never creates pin
    led4 = CarryLedger(tmp_path / "pin_clear.sqlite3", plan_base.fingerprint(), ledger_domain="paper")
    led4.create_position(
        position_id="P-PIN", symbol="AAA", long_venue="BYBIT", short_venue="OKX",
        iid_long="AAAUSDT-LINEAR.BYBIT", iid_short="AAA-USDT-SWAP.OKX", opened_ts=t0 - 10 * HOUR,
        entry_l=100.0, entry_s=100.0, entry_basis_bps=0.0, target_base_qty=D("1.0"),
    )
    led4.update_position_status("P-PIN", status="OPEN", ts_s=t0 - 9 * HOUR, long_base_qty=D("1.0"), short_base_qty=D("1.0"))
    led4.record_exit_defer_target("P-PIN", target_ts=t0 + 600, ts_s=t0)
    assert led4.get_exit_defer_target_ts("P-PIN") == t0 + 600
    led4.update_position_status("P-PIN", status="CLOSING", ts_s=t0 + 1, close_reason="soft_margin_guard_20pct")
    assert led4.get_exit_defer_target_ts("P-PIN") is None
    led4.record_exit_defer_target("P-PIN", target_ts=t0 + 700, ts_s=t0 + 2)
    led4.revert_closing_to_open("P-PIN", ts_s=t0 + 3)
    assert led4.get_exit_defer_target_ts("P-PIN") is None
    led4.record_exit_defer_target("P-PIN", target_ts=t0 + 800, ts_s=t0 + 4)
    led4.update_position_status("P-PIN", status="CLOSED", ts_s=t0 + 5, close_reason="carry_decay")
    assert led4.get_exit_defer_target_ts("P-PIN") is None

    clock = [float(t0)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    strat_c = _create_carry_strategy(replace(plan_base, state_path=tmp_path / "pin_closing.sqlite3"), led4, mode="paper")
    spec_okx = InstrumentSpec("AAA-USDT-SWAP.OKX", "OKX", size_increment=D("1"), price_increment=D("0.01"))
    spec_byb = InstrumentSpec("AAAUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("1"), price_increment=D("0.01"))
    strat_c._specs_from_cache = lambda: {spec_byb.instrument_id: spec_byb, spec_okx.instrument_id: spec_okx}
    strat_c.live_quotes = {spec_byb.instrument_id: (99.99, 100.01), spec_okx.instrument_id: (99.99, 100.01)}
    strat_c.live_quote_ts = {spec_byb.instrument_id: clock[0], spec_okx.instrument_id: clock[0]}
    strat_c.live_mids = {spec_byb.instrument_id: 100.0, spec_okx.instrument_id: 100.0}
    strat_c.live_mid_ts = {spec_byb.instrument_id: clock[0], spec_okx.instrument_id: clock[0]}
    led4.create_position(
        position_id="P-CL", symbol="AAA", long_venue="BYBIT", short_venue="OKX",
        iid_long=spec_byb.instrument_id, iid_short=spec_okx.instrument_id, opened_ts=t0 - 10 * HOUR,
        entry_l=100.0, entry_s=100.0, entry_basis_bps=0.0, target_base_qty=D("1.0"),
    )
    led4.update_position_status("P-CL", status="CLOSING", ts_s=t0 - 1, long_base_qty=D("1.0"), short_base_qty=D("1.0"), close_reason="soft_margin_guard_20pct")
    strat_c._apply_slow_snapshot(
        {
            "AAA": {
                "BYBIT": replace(_make_leg("BYBIT", "AAA", interval_h=8, next_settle_ts=t0 + 600, r8=-1.0, cur_rate=-2.0), fetched_ts=t0),
                "OKX": replace(_make_leg("OKX", "AAA", interval_h=8, next_settle_ts=t0 + 3600, r8=-0.9, cur_rate=2.0), fetched_ts=t0),
            }
        },
        poll_ts_s=t0,
    )
    assert led4.get_exit_defer_target_ts("P-CL") is None
    led4.close()

    # 6. Replay advances elapsed 1s maker expiry timers BEFORE checking late crossing quote ticks
    tape_gap = tmp_path / "tape_gap"
    tape_gap.mkdir()
    slow_gap = [
        {
            "ts": t0,
            "legs": [
                {
                    "s": "BLUR", "v": "BYBIT", "ac": "CRYPTO", "iid": spec_l.instrument_id, "ih": 8,
                    "ns": t0 + 8 * HOUR, "r8": 0.0, "r24": 0.0, "cr": 0.0,
                    "mp": 1.0, "b": 0.9999, "a": 1.0001, "v24": 2_000_000.0, "v1": 50_000.0,
                },
                {
                    "s": "BLUR", "v": "HL", "ac": "CRYPTO", "iid": spec_s.instrument_id, "ih": 1,
                    "ns": t0 + HOUR, "r8": 4.0, "r24": 4.0, "cr": 4.0,
                    "mp": 1.0015, "b": 1.0014, "a": 1.0016, "v24": 2_000_000.0, "v1": 50_000.0,
                },
            ],
        }
    ]
    fast_gap = [
        {"ts": t0 + 120.0, "iid": spec_l.instrument_id, "b": 1.0020, "a": 1.0022, "m": 1.0021},
        {"ts": t0 + 120.0, "iid": spec_s.instrument_id, "b": 1.0018, "a": 1.0022, "m": 1.0020},
    ]
    (tape_gap / "slow_snapshots_20270116.jsonl.gz").write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in slow_gap).encode("utf-8")))
    (tape_gap / "fast_bbo_20270116.jsonl.gz").write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in fast_gap).encode("utf-8")))
    rep_gap = replay_tape(tape_dir=tape_gap, plan=plan_base, execution_mode="hybrid")
    assert rep_gap["summary"]["maker_orders_submitted"] >= 1
    assert rep_gap["summary"]["maker_orders_filled"] == 0
    assert all(p["long_base_qty"] == "0" and p["short_base_qty"] == "0" for p in rep_gap["open_positions"])

    # 7 & 8 & 9. Single-leg min_quantity sizing, cancel retry while CLOSING, and dual-taker phase check
    led7 = CarryLedger(tmp_path / "exec_edge789.sqlite3", plan_base.fingerprint(), ledger_domain="paper")
    gw7 = DelayedAsyncVenueGateway()
    spec_l_min = InstrumentSpec("MINUSDT-LINEAR.BYBIT", "BYBIT", size_increment=D("1"), min_quantity=D("60"), price_increment=D("0.01"))
    spec_s_min = InstrumentSpec("MIN-USD-PERP.HYPERLIQUID", "HL", size_increment=D("1"), min_quantity=D("1"), price_increment=D("0.01"))
    gw7.register_spec(spec_l_min)
    gw7.register_spec(spec_s_min)
    gw7.register_spec(spec_l)
    gw7.register_spec(spec_s)
    specs7 = {
        spec_l_min.instrument_id: spec_l_min, spec_s_min.instrument_id: spec_s_min,
        spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s,
    }
    ctrl7 = CarryExecutionController(ledger=led7, gateway=gw7, risk=plan_base.risk, runtime=plan_base.runtime, policy=plan_base.policy)
    led7.create_position(
        position_id="ONE-MIN", symbol="MIN", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l_min.instrument_id, iid_short=spec_s_min.instrument_id, opened_ts=t0 - 100,
        entry_l=1.0, entry_s=1.0, entry_basis_bps=0.0, target_base_qty=D("140"),
    )
    led7.update_position_status("ONE-MIN", status="CLOSING", ts_s=t0, long_base_qty=D("140"), short_base_qty=D("0"), close_reason="soft_margin_guard_20pct")
    q_min = {spec_l_min.instrument_id: (0.9999, 1.0001)}
    m_min = {spec_l_min.instrument_id: 1.0}
    assert ctrl7.advance_pending_slices(specs=specs7, quotes=q_min, mids=m_min, now_s=t0) == ["ONE-MIN"]
    assert gw7.submitted[-1]["qty_contracts"] == D("60")
    gw7.deliver_fill(gw7.submitted[-1]["client_order_id"], filled_base_qty=D("60"), avg_fill_price=D("1.0"))
    ctrl7.poll_active_orders(live_mids=m_min, now_s=t0 + 1, live_quotes=q_min)
    assert ctrl7.advance_pending_slices(specs=specs7, quotes=q_min, mids=m_min, now_s=t0 + 1) == ["ONE-MIN"]
    assert gw7.submitted[-1]["qty_contracts"] == D("80")
    gw7.deliver_fill(gw7.submitted[-1]["client_order_id"], filled_base_qty=D("80"), avg_fill_price=D("1.0"))
    ctrl7.poll_active_orders(live_mids=m_min, now_s=t0 + 2, live_quotes=q_min)

    # 8. Reject cancel first, then retry start_sliced_close while CLOSING
    q_open = {spec_l.instrument_id: (0.9999, 1.0001), spec_s.instrument_id: (1.0014, 1.0016)}
    m_open = {spec_l.instrument_id: 1.0, spec_s.instrument_id: 1.0015}
    led7.create_position(
        position_id="A-REJ", symbol="REJ", long_venue="BYBIT", short_venue="HL",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=t0,
        entry_l=1.0, entry_s=1.0015, entry_basis_bps=15.0, target_base_qty=D("100"),
    )
    led7.update_position_status("A-REJ", status="OPENING", ts_s=t0, long_base_qty=D("50"), short_base_qty=D("50"))
    assert ctrl7.advance_pending_slices(specs=specs7, quotes=q_open, mids=m_open, now_s=t0, only_position_id="A-REJ") == ["A-REJ"]
    open_coid = gw7.submitted[-1]["client_order_id"]
    ctrl7.start_sliced_close("A-REJ", specs=specs7, quotes=q_open, mids=m_open, reason="soft_margin_guard_20pct", now_s=t0 + 1)
    ctrl7.on_cancel_rejected(open_coid)
    cancels_before = gw7.cancel_requests.count(open_coid)
    ctrl7.start_sliced_close("A-REJ", specs=specs7, quotes=q_open, mids=m_open, reason="soft_margin_guard_20pct", now_s=t0 + 2)
    assert gw7.cancel_requests.count(open_coid) == cancels_before + 1
    gw7.deliver_cancel(open_coid, filled_base_qty=D("0"))
    ctrl7.poll_active_orders(live_mids=m_open, now_s=t0 + 3, live_quotes=q_open)

    # 9. Unfilled opening dual-taker batch does NOT increment CLOSING _slice_attempts
    cand_dt = evaluate_entry_candidates(
        {
            "DTPH": {
                "BYBIT": replace(_make_leg("BYBIT", "DTPH", interval_h=8, next_settle_ts=t0 + 8 * HOUR, r8=0.0, r24=0.0, cur_rate=0.0, bid=0.9999, ask=1.0001, prev_1h=1.0), fetched_ts=t0),
                "HL": replace(_make_leg("HL", "DTPH", interval_h=1, next_settle_ts=t0 + HOUR, r8=4.0, r24=4.0, cur_rate=4.0, bid=1.0029, ask=1.0031, prev_1h=1.003), fetched_ts=t0),
            }
        },
        open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan_base.policy, risk=plan_base.risk, now_s=t0,
    )[0]
    spec_dtl = InstrumentSpec(cand_dt.long_leg.instrument_id, "BYBIT", size_increment=D("1"), price_increment=D("0.0001"))
    spec_dts = InstrumentSpec(cand_dt.short_leg.instrument_id, "HL", size_increment=D("1"), price_increment=D("0.0001"))
    gw7.register_spec(spec_dtl)
    gw7.register_spec(spec_dts)
    specs_dt = {**specs7, spec_dtl.instrument_id: spec_dtl, spec_dts.instrument_id: spec_dts}
    dt_pid, dt_err = ctrl7.start_entry(cand_dt, specs=specs_dt, now_s=t0)
    assert dt_err is None and dt_pid is not None
    dt_coids = [r["client_order_id"] for r in gw7.submitted[-2:]]
    led7.update_position_status(dt_pid, status="CLOSING", ts_s=t0 + 1, long_base_qty=D("50"), short_base_qty=D("50"), close_reason="soft_margin_guard_20pct")
    for c in dt_coids:
        gw7.deliver_cancel(c, filled_base_qty=D("0"))
    ctrl7.poll_active_orders(live_mids={spec_dtl.instrument_id: 1.0, spec_dts.instrument_id: 1.003}, now_s=t0 + 2)
    assert ctrl7._slice_attempts.get(dt_pid, 0) == 0
    led7.close()

    # 10 & 11 & 12. Runtime freshness in poll_active_orders, Decimal mid without float noise, and CarryInversionTracker exact Decimal
    clock = [float(t0)]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    led10 = CarryLedger(tmp_path / "rt_fresh.sqlite3", plan_base.fingerprint(), ledger_domain="paper")
    strat10 = _create_carry_strategy(replace(plan_base, state_path=tmp_path / "rt_fresh.sqlite3"), led10, mode="paper")
    strat10._specs_from_cache = lambda: specs
    strat10.reconciled = True
    strat10.live_mids["NO_TS.BYBIT"] = 12.3
    assert strat10._exec_fresh_mids(clock[0]) == {}

    strat10._ingest_quote(spec_l.instrument_id, 0.83999, 0.84001, None)
    assert strat10.live_mids[spec_l.instrument_id] == 0.84
    assert _make_leg("BYBIT", "BLUR", bid=0.83999, ask=0.84001).mid == 0.84

    strat10.live_quotes["STALE.HL"] = (1.0, 1.1)
    strat10.live_quote_ts["STALE.HL"] = clock[0] - 100.0
    strat10.live_mids["STALE.HL"] = 1.05
    strat10.live_mid_ts["STALE.HL"] = clock[0] - 1000.0
    captured_poll: list[tuple[dict[str, float], dict[str, tuple[float, float]]]] = []
    monkeypatch.setattr(
        strat10.controller,
        "poll_active_orders",
        lambda *, live_mids, now_s, live_quotes=None: captured_poll.append((dict(live_mids), dict(live_quotes or {}))) or [],
    )
    strat10._on_fast_risk_timer(None)
    assert captured_poll and "STALE.HL" not in captured_poll[0][0] and "STALE.HL" not in captured_poll[0][1]

    ev_mid = FastRiskEvaluator(plan_base.risk)
    dec_105 = ev_mid.evaluate(
        positions={
            "P-105": {
                "symbol": "BLUR", "long_venue": "BYBIT", "short_venue": "HL",
                "iid_long": spec_l.instrument_id, "iid_short": spec_s.instrument_id,
                "entry_l": 1.05, "entry_s": 1.05, "long_base_qty": "10", "short_base_qty": "10",
            }
        },
        live_mids={spec_l.instrument_id: 0.8400000000000001, spec_s.instrument_id: 1.05},
        daily_realized_pnl_usd=D("0"), consecutive_unhedged=0, now_s=t0,
        live_quotes={spec_l.instrument_id: (0.83999, 0.84001), spec_s.instrument_id: (1.04999, 1.05001)},
    )
    assert dec_105.soft_close_positions == (("P-105", "soft_margin_guard_20pct"),)

    tiny_inv = CarryInversionTracker(inversion_stop_bps_h=D("1e-13"), persist_mins=1, max_gap_secs=180)
    assert tiny_inv.observe(position_id="TINY", live_carry_bps_h=0.0, now_s=t0) == (False, 0)
    assert tiny_inv.observe(position_id="TINY", live_carry_bps_h=0.0, now_s=t0 + 60) == (False, 0)
    exact_inv = CarryInversionTracker(inversion_stop_bps_h=D("3.0"), persist_mins=1, max_gap_secs=180)
    exact_inv.observe(position_id="EX", live_carry_bps_h=D("0.4") - D("3.4"), now_s=t0)
    assert exact_inv.observe(position_id="EX", live_carry_bps_h=D("0.4") - D("3.4"), now_s=t0 + 60) == (True, 60)
    led10.close()





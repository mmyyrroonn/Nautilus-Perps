"""Regression tests for PR 1 BLUR incident fixes: risk precedence, close handoff, funding consistency, window veto, and replay calibration."""
from __future__ import annotations

from decimal import Decimal
import gzip
import json
from pathlib import Path

import pytest

from scripts.replay_carry_tape import replay_tape
from src.carry.config import load_carry_plan
from src.carry.executor import CarryExecutionController
from src.carry.funding_feed import HOUR, PublicCarryFeed
from src.carry.ledger import CarryLedger
from src.carry.risk import FastRiskEvaluator
from src.carry.signal import (
    InstrumentSpec,
    estimate_first_settle_entry_tax_bps,
    estimate_min_hold_window_net_funding_bps,
    evaluate_entry_candidates,
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


"""Tests for Net PnL accounting, 95ba5a1 DB migration, review CLI, and ME #2 entry-tax guard."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
import gzip
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace

import pytest

from src.carry.config import load_carry_plan
from src.carry.executor import CarryExecutionController
from src.carry.funding_feed import HOUR, LegMarketInfo, PublicCarryFeed
from src.carry.ledger import CarryLedger
from src.carry.recorder import CarryRecorder
from src.carry.review import load_review_report, main as review_main
from src.carry.risk import FastRiskEvaluator
from src.carry.runtime import _NautilusStrategyGateway, _create_carry_strategy
from src.carry.signal import (
    InstrumentSpec,
    _cex_entry_blackout_hit,
    estimate_first_settle_entry_tax_bps,
    evaluate_entry_candidates,
    should_exit_carry_decay,
)
from tests.test_carry_mvp import DelayedAsyncVenueGateway, _make_leg

D = Decimal
REPO_ROOT = Path(__file__).resolve().parents[1]


def _create_95ba5a1_db(db_path: Path, *, fingerprint: str, domain: str, pid: str = "POS-95", realized: str = "0") -> None:
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE meta (id INTEGER PRIMARY KEY CHECK (id=1), fingerprint TEXT NOT NULL, created_ts INTEGER NOT NULL, ledger_domain TEXT);
            CREATE TABLE positions (
                position_id TEXT PRIMARY KEY, symbol TEXT NOT NULL, long_venue TEXT NOT NULL, short_venue TEXT NOT NULL,
                iid_long TEXT NOT NULL, iid_short TEXT NOT NULL, status TEXT NOT NULL, opened_ts INTEGER NOT NULL,
                closed_ts INTEGER, entry_l REAL NOT NULL, entry_s REAL NOT NULL, entry_basis_bps REAL NOT NULL,
                target_base_qty TEXT NOT NULL, long_base_qty TEXT NOT NULL, short_base_qty TEXT NOT NULL,
                realized_pnl_usd TEXT NOT NULL DEFAULT '0', close_reason TEXT
            );
            CREATE TABLE orders (
                client_order_id TEXT PRIMARY KEY, position_id TEXT NOT NULL, venue TEXT NOT NULL, instrument_id TEXT NOT NULL,
                side TEXT NOT NULL, role TEXT NOT NULL, order_type TEXT NOT NULL, qty_contracts TEXT NOT NULL,
                base_qty TEXT NOT NULL, price TEXT, status TEXT NOT NULL, filled_base_qty TEXT NOT NULL DEFAULT '0',
                venue_order_id TEXT, created_ts INTEGER NOT NULL, updated_ts INTEGER NOT NULL
            );
            CREATE TABLE events (seq INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL);
            CREATE TABLE daily_stats (utc_day TEXT PRIMARY KEY, realized_pnl_usd TEXT NOT NULL, consecutive_unhedged INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE runtime_flags (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            """
        )
        conn.execute("INSERT INTO meta VALUES (1, ?, 1800000000, ?)", (fingerprint, domain))
        conn.execute(
            "INSERT INTO positions VALUES (?, 'SOL', 'HL', 'OKX', 'SOL-USD-PERP.HYPERLIQUID', 'SOL-USDT-SWAP.OKX', 'OPEN', 1800000000, NULL, 100.0, 100.2, 20.0, '0.49', '0.49', '0.49', ?, NULL)",
            (pid, realized),
        )
        conn.execute(
            "INSERT INTO orders VALUES ('mk1', ?, 'HL', 'SOL-USD-PERP.HYPERLIQUID', 'BUY', 'MAKER_OPEN', 'POST_ONLY', '0.49', '0.49', '100.0', 'FILLED', '0.49', 'PAPER-1', 1800000001, 1800000002)",
            (pid,),
        )


def test_legacy_95ba5a1_ledger_upgrade_and_baseline_fingerprints(tmp_path: Path) -> None:
    paper_plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    live_plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    assert (
        replace(paper_plan, state_path=Path("/Volumes/Expansion/perpsarb/Nautilus-Perps/reports/carry/paper.sqlite3")).fingerprint()
        == "8247c775c8ac8598f85d92bcc74bfeca8cbac36113e758aa8f70d5b3761ba49e"
    )
    assert (
        replace(live_plan, state_path=Path("/Volumes/Expansion/perpsarb/Nautilus-Perps/reports/carry/live.sqlite3")).fingerprint()
        == "d90851835399132a5d8631b0957970704301853e4c3b146ff5d4497eb274571c"
    )

    db_path = tmp_path / "legacy_95ba5a1.sqlite3"
    _create_95ba5a1_db(db_path, fingerprint=paper_plan.fingerprint(), domain="paper")
    led = CarryLedger(db_path, paper_plan.fingerprint(), ledger_domain="paper")
    assert led.is_halted() == (False, None)
    assert led.active_positions()["POS-95"]["status"] == "OPEN"
    pos_cols = {r["name"] for r in led.conn.execute("PRAGMA table_info(positions)").fetchall()}
    ord_cols = {r["name"] for r in led.conn.execute("PRAGMA table_info(orders)").fetchall()}
    assert {"price_pnl_usd", "funding_pnl_usd", "fee_usd"} <= pos_cols
    assert "fee_usd" in ord_cols
    assert led.record_funding_settlement(
        position_id="POS-95", symbol="SOL", venue="OKX", side="SHORT", settle_ts=1_800_003_600,
        rate_bps=10.0, base_qty=D("0.49"), mark_price=100.0, source="okx_public_history", recorded_ts=1_800_003_610,
    )
    assert led.get_position_funding_pnl("POS-95") == D("0.0490")
    led.close()


def test_fees_live_commissions_funding_settlements_and_daily_loss_kill(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from nautilus_trader.model import Currency, InstrumentId, Money, Quantity

    plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")
    now_s = 1_800_000_000
    clock = [float(now_s)]
    monkeypatch.setattr(time, "time", lambda: clock[0])

    def fake_http(url: str, body: dict | None = None):
        if "hyperliquid" in url:
            rt = (body or {}).get("type")
            if rt == "metaAndAssetCtxs":
                return ([{"universe": []}, []] if body.get("dex") == "xyz" else [{"universe": [{"name": "SOL"}]}, [{"markPx": "100", "midPx": "100", "funding": "0.0001", "dayNtlVlm": "5000000"}]])
            if rt == "fundingHistory":
                return [{"time": (now_s - 600) * 1000, "fundingRate": "0.0001"}, {"time": (now_s + 3600) * 1000, "fundingRate": "0.0010"}]
            if rt == "candleSnapshot":
                return [{"t": (now_s - 7200) * 1000, "c": "100", "v": "10"}, {"t": (now_s - 3600) * 1000, "c": "100", "v": "10"}]
        if "okx.com" in url:
            if "public/instruments" in url:
                return {"data": [{"instId": "SOL-USDT-SWAP", "state": "live", "settleCcy": "USDT"}]}
            if "market/tickers" in url:
                return {"data": [{"instId": "SOL-USDT-SWAP", "last": "100", "bidPx": "99.98", "askPx": "100.02", "volCcy24h": "200000"}]}
            if "instId=ANY" in url:
                return {"data": [{"instId": "SOL-USDT-SWAP", "fundingRate": "0.0020", "fundingTime": str((now_s + 3600) * 1000), "nextFundingTime": str((now_s + 9 * 3600) * 1000)}]}
            if "funding-rate-history" in url:
                return {"data": [{"fundingTime": str((now_s + 3600) * 1000), "realizedRate": "0.0020"}]}
            if "market/candles" in url:
                return {"data": [[str((now_s - 7200) * 1000), "100", "100", "100", "100", "100", "0", "10000"], [str((now_s - 3600) * 1000), "100", "100", "100", "100", "100", "0", "10000"]]}
        return {}

    led = CarryLedger(tmp_path / "live_pnl.sqlite3", plan.fingerprint(), ledger_domain="live")
    rec_dir = tmp_path / "rec"
    rec = CarryRecorder(rec_dir)
    strat = _create_carry_strategy(plan, led, mode="live", recorder=rec)
    strat.feed = PublicCarryFeed(venues=("HL", "OKX"), http_get=fake_http)
    gw = DelayedAsyncVenueGateway()
    spec_l = InstrumentSpec("SOL-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_s = InstrumentSpec("SOL-USDT-SWAP.OKX", "OKX", size_increment=D("0.1"), contract_multiplier=D("0.1"), price_increment=D("0.01"))
    gw.register_spec(spec_l)
    gw.register_spec(spec_s)
    specs = {spec_l.instrument_id: spec_l, spec_s.instrument_id: spec_s}
    strat.gateway = gw
    strat.controller.gateway = gw
    strat.reconciled = True
    strat._specs_from_cache = lambda: specs

    # Check recorder does NOT store settle_events
    snap_poll = strat.feed.poll_snapshot(now_s=now_s + 3610)
    assert len(strat.feed.recent_settlements("HL", "SOL")) >= 1
    assert len(strat.feed.recent_settlements("OKX", "SOL")) == 1
    rec.record_slow_snapshot(ts_s=now_s + 3610, snapshot=snap_poll, candidates=[], active_count=0, subscribed_count=0)
    rec.close()
    raw_rec = gzip.open(next(rec_dir.glob("slow_snapshots_*.jsonl.gz")), "rt", encoding="utf-8").read()
    assert "settle_events" not in raw_rec

    # Open position at now_s, settle funding at now_s + 3600 (positive HL long pays, positive OKX short receives)
    led.create_position(
        position_id="SOL-LIVE-PNL", symbol="SOL", long_venue="HL", short_venue="OKX",
        iid_long=spec_l.instrument_id, iid_short=spec_s.instrument_id, opened_ts=now_s,
        entry_l=100.0, entry_s=100.0, entry_basis_bps=0.0, target_base_qty=D("1.00"),
    )
    led.update_position_status("SOL-LIVE-PNL", status="OPEN", ts_s=now_s + 1, long_base_qty=D("1.00"), short_base_qty=D("1.00"))
    clock[0] = float(now_s + 3610)
    strat._apply_slow_snapshot(snap_poll, poll_ts_s=now_s + 3610)
    strat._apply_slow_snapshot(snap_poll, poll_ts_s=now_s + 3610)  # idempotent repeat
    # HL: -10bps * 100 = -0.1000; OKX: +20bps * 100 = +0.2000; net = +0.1000
    assert led.get_position_funding_pnl("SOL-LIVE-PNL") == D("0.1000")
    assert led.conn.execute("SELECT COUNT(*) FROM funding_settlements WHERE position_id='SOL-LIVE-PNL'").fetchone()[0] == 2

    # Live OrderFilled stablecoin commission vs BTC non-stablecoin fallback + emergency close net PnL
    strat.controller.emergency_close_position(
        "SOL-LIVE-PNL", specs=specs, reason="basis_stop_30m", now_s=now_s + 3620,
        live_mids={spec_l.instrument_id: 100.0, spec_s.instrument_id: 100.0},
    )
    em_l = next(o["client_order_id"] for o in gw.submitted if o["side"] == "SELL")
    em_s = next(o["client_order_id"] for o in gw.submitted if o["side"] == "BUY")
    gw.deliver_fill(em_l, filled_base_qty=D("1.00"), avg_fill_price=D("100.00"))
    # Dedup same trade_id; 2nd partial commission adds up -> 0.1234 USDT
    strat.on_order_filled(SimpleNamespace(client_order_id=em_l, trade_id="t1", commission=Money(0.10, Currency.from_str("USDT"))))
    strat.on_order_filled(SimpleNamespace(client_order_id=em_l, trade_id="t1", commission=Money(0.10, Currency.from_str("USDT"))))
    strat.on_order_filled(SimpleNamespace(client_order_id=em_l, trade_id="t2", commission=Money(0.0234, Currency.from_str("USDC"))))
    # Non-stablecoin BTC commission clears & falls back to OKX taker 5.0 bps (= 0.0500)
    gw.deliver_fill(em_s, filled_base_qty=D("1.00"), avg_fill_price=D("100.00"))
    strat.on_order_filled(SimpleNamespace(client_order_id=em_s, trade_id="t3", commission=Money(0.0001, Currency.from_str("BTC"))))

    row = dict(led.conn.execute("SELECT price_pnl_usd, funding_pnl_usd, fee_usd, realized_pnl_usd FROM positions WHERE position_id='SOL-LIVE-PNL'").fetchone())
    assert D(row["price_pnl_usd"]) == D("0.0000")
    assert D(row["funding_pnl_usd"]) == D("0.1000")
    assert D(row["fee_usd"]) == D("0.1734")
    assert D(row["realized_pnl_usd"]) == D("-0.0734")
    assert led.daily_stats(now_s + 3620)[0] == D("-0.0734")
    assert FastRiskEvaluator(plan.risk).evaluate(positions={}, live_mids={}, daily_realized_pnl_usd=D("-40.0000"), consecutive_unhedged=0, now_s=now_s + 3620).halt_reason == "daily_loss_kill"

    # Also test _NautilusStrategyGateway.poll_order extracts order.commissions()
    nt_gw = _NautilusStrategyGateway(SimpleNamespace(cache=SimpleNamespace(
        order=lambda _c: SimpleNamespace(instrument_id=InstrumentId.from_str(spec_s.instrument_id), filled_qty=Quantity.from_str("10.0"), avg_px=100.0, status="FILLED", venue_order_id="V1", commissions=lambda: {Currency.from_str("USDT"): Money(0.05, Currency.from_str("USDT"))}),
        instrument=lambda _i: SimpleNamespace(multiplier=D("0.1")),
    )))
    assert nt_gw.poll_order(client_order_id="x1", venue="OKX", instrument_id=spec_s.instrument_id).fee_usd == D("0.05000000")
    led.close()


def test_readonly_review_cli_locked_and_legacy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    db_path = tmp_path / "locked_review.sqlite3"
    led = CarryLedger(db_path, plan.fingerprint(), ledger_domain="paper")
    led.create_position(
        position_id="SOL-REV-1", symbol="SOL", long_venue="HL", short_venue="OKX",
        iid_long="SOL-USD-PERP.HYPERLIQUID", iid_short="SOL-USDT-SWAP.OKX",
        opened_ts=1_800_000_000, entry_l=100.0, entry_s=100.2, entry_basis_bps=20.0, target_base_qty=D("1.0"),
    )
    led.update_position_status(
        "SOL-REV-1", status="CLOSED", ts_s=1_800_003_600,
        price_pnl_usd=D("0.2000"), funding_pnl_usd=D("0.0500"), fee_usd=D("0.0650"), realized_pnl_usd=D("0.1850"), close_reason="carry_decay",
    )
    # Must read cleanly WHILE CarryLedger holds FileLock
    assert review_main(["--state", str(db_path), "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["summary"]["total_net_pnl_usd"] == "0.1850"
    assert review_main(["--state", str(db_path)]) == 0
    assert "SOL-REV-1" in capsys.readouterr().out
    led.close()

    # Legacy DB without new cols: degraded read with & without --config
    leg_db = tmp_path / "legacy_ro.sqlite3"
    _create_95ba5a1_db(leg_db, fingerprint=plan.fingerprint(), domain="paper", pid="LEG-1", realized="0.5000")
    with sqlite3.connect(leg_db) as c:
        c.execute("UPDATE positions SET status='CLOSED', closed_ts=1800003600 WHERE position_id='LEG-1'")
    rep_no_cfg = load_review_report(leg_db)
    assert rep_no_cfg["schema_legacy"] is True and rep_no_cfg["summary"]["total_fee_usd"] == "0.0000"
    rep_with_cfg = load_review_report(leg_db, config_path=REPO_ROOT / "config" / "carry-paper.toml")
    assert D(rep_with_cfg["summary"]["total_fee_usd"]) > D("0")


def test_me2_cross_cycle_entry_tax_and_2h_cex_protection() -> None:
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    t_0900 = 1_800_003_600  # boundary

    # ME #2 at 09:01: HL 1h long receiving (-47.0 bps/h), OKX 2h short paying (-47.5 -> -95 bps lump at 10:00)
    t_0901 = t_0900 + 60
    hl_0901 = _make_leg("HL", "ME", interval_h=1, next_settle_ts=t_0900 + HOUR, r8=0.0, r24=0.0, cur_rate=-47.0, bid=100.0, ask=100.02)
    okx_0901 = _make_leg("OKX", "ME", interval_h=2, next_settle_ts=t_0900 + HOUR, r8=2.0, r24=2.0, cur_rate=-47.5, bid=100.18, ask=100.20)
    tax_0901 = estimate_first_settle_entry_tax_bps(hl_0901, okx_0901, t_0901)
    assert tax_0901 == pytest.approx(48.0)
    assert evaluate_entry_candidates(
        {"ME": {"HL": hl_0901, "OKX": okx_0901}}, open_symbols=set(), venue_notional_used={},
        excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=t_0901,
    ) == []

    # Same rates at 10:01 right after settlement (next OKX settle = 12:00 -> HL settles 2x for +96 vs OKX -95)
    t_1001 = t_0900 + HOUR + 60
    hl_1001 = _make_leg("HL", "ME", interval_h=1, next_settle_ts=t_0900 + 2 * HOUR, r8=0.0, r24=0.0, cur_rate=-48.0, bid=100.0, ask=100.02)
    okx_1001 = _make_leg("OKX", "ME", interval_h=2, next_settle_ts=t_0900 + 3 * HOUR, r8=2.0, r24=2.0, cur_rate=-47.5, bid=100.18, ask=100.20)
    cands_1001 = evaluate_entry_candidates(
        {"ME": {"HL": hl_1001, "OKX": okx_1001}}, open_symbols=set(), venue_notional_used={},
        excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=t_1001,
    )
    assert len(cands_1001) == 1 and cands_1001[0].entry_tax_bps == 0.0

    # 2h & 3h CEX blackout + 2h carry_decay exit deferral + normal 8h passing
    for iv in (2, 3):
        leg_pay = _make_leg("OKX", "ME", interval_h=iv, next_settle_ts=t_0901 + 600, r8=2.0, r24=2.0, cur_rate=-1.0)
        assert _cex_entry_blackout_hit(hl_0901, leg_pay, plan.policy, t_0901) is True
    leg_recv_2h = _make_leg("OKX", "ME", interval_h=2, next_settle_ts=t_0901 + 600, r8=0.1, cur_rate=2.0)
    do_exit, reason = should_exit_carry_decay(
        opened_ts=t_0901 - 7 * HOUR, leg_l=_make_leg("HL", "ME", r8=0.0), leg_s=leg_recv_2h, policy=plan.policy, now_s=t_0901,
    )
    assert not do_exit and reason == "defer_collect_cex_settlement"

    c_8h = evaluate_entry_candidates(
        {"SOL": {
            "HL": _make_leg("HL", "SOL", interval_h=1, next_settle_ts=t_0900 + HOUR, r8=0.0, r24=0.0, cur_rate=0.0, bid=100.0, ask=100.02),
            "OKX": _make_leg("OKX", "SOL", interval_h=8, next_settle_ts=t_0900 + 6 * HOUR, r8=1.5, r24=1.5, cur_rate=1.5, bid=100.18, ask=100.20),
        }},
        open_symbols=set(), venue_notional_used={}, excluded_symbols=set(), policy=plan.policy, risk=plan.risk, now_s=t_0901,
    )
    assert len(c_8h) == 1 and c_8h[0].entry_tax_bps == 0.0

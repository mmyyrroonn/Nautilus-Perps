"""Tests for bounded strategy analytics telemetry, memory extensions, and session analyzer."""
from __future__ import annotations

from decimal import Decimal
import json
from pathlib import Path
import sys
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
SCRIPTS = ROOT / "scripts"
for entry in (str(SRC), str(SCRIPTS)):
    if entry not in sys.path:
        sys.path.insert(0, entry)

import opportunity_analytics as analytics
import opportunity_memory as memory
import opportunity_timing as timing
from opportunity_core import BookSnapshot, MarketMetadata, ScanSettings
from opportunity_live import LiveTradeState
from opportunity_paper import PaperTradeState
from opportunity_scan import MarketPlan, ScanPlan
from opportunity_trade import TradePolicy
from opportunity_trade_store import TradeStore
import analyze_opportunity_session as analyzer


def _make_plan(tmp_path):
    settings = ScanSettings(
        target_notional=Decimal("100"),
        min_entry_edge_bps=Decimal("10"),
        reserve_bps=Decimal("0"),
        max_age_ms=2000,
        max_skew_ms=500,
        max_receive_age_ms=2000,
    )
    markets = (
        MarketPlan("BTC", "HL", "BTC-USD-PERP.HYPERLIQUID", Decimal("1"), "assumed", Decimal("4.5"), "test"),
        MarketPlan("BTC", "ASTER", "BTCUSDT-PERP.ASTER", Decimal("1"), "assumed", Decimal("4"), "test"),
    )
    return ScanPlan(
        markets=markets, settings=settings, output_path=None,
        cooldown_ms=1000, edge_change_bps=Decimal("1"), quantity_change_fraction=Decimal("0.1"),
        max_interval_ms=None, depth_levels=20, max_levels_per_side=1000, refresh_ms=1000, top_n=10,
        duration_secs=0, connection_timeout_secs=30, evaluation_interval_ms=100,
    )


def _book(venue, symbol, bids, asks, ts_ns):
    return BookSnapshot(
        venue=venue, symbol=symbol,
        bids=tuple((Decimal(str(p)), Decimal(str(q))) for p, q in bids),
        asks=tuple((Decimal(str(p)), Decimal(str(q))) for p, q in asks),
        ts_event_ns=ts_ns, ts_received_ns=ts_ns, valid=True, coverage_limit=20,
    )


def _meta(instrument_id, fee_bps="4"):
    return MarketMetadata(instrument_id, Decimal("0.001"), Decimal("1"), Decimal(fee_bps), "test")


def test_exit_liquidity_detects_full_and_thin_reverse_books():
    t0 = 1_700_000_000_000_000_000
    buy_book = _book("HL", "BTC", [(99.8, 2.0)], [(100.0, 2.0)], t0)
    sell_book = _book("ASTER", "BTC", [(100.5, 2.0)], [(100.7, 2.0)], t0)
    buy_meta = _meta("BTC-USD-PERP.HYPERLIQUID", "4.5")
    sell_meta = _meta("BTCUSDT-PERP.ASTER", "4")

    full = analytics.compute_exit_liquidity(
        buy_book, buy_meta, sell_book, sell_meta, Decimal("1.0"), Decimal("100.0"), Decimal("100.5"))
    assert full["both_exit_full"] is True
    assert full["buy_exit_coverage"] == 1.0
    assert full["sell_exit_coverage"] == 1.0
    # Reversing: buy leg loses 0.2, sell leg loses 0.2 -> 0.4 / 100 = 40 bps
    assert abs(full["instant_reversal_slippage_bps"] - 40.0) < 1e-3

    thin_sell = _book("ASTER", "BTC", [(100.5, 2.0)], [(100.7, 0.25)], t0)
    thin = analytics.compute_exit_liquidity(
        buy_book, buy_meta, thin_sell, sell_meta, Decimal("1.0"), Decimal("100.0"), Decimal("100.5"))
    assert thin["both_exit_full"] is False
    assert thin["sell_exit_full"] is False
    assert thin["sell_exit_coverage"] == 0.25
    assert thin["instant_reversal_slippage_bps"] is None


def test_paper_funnel_exit_liquidity_alpha_decay_and_session_analyzer(tmp_path):
    plan = _make_plan(tmp_path)
    policy = TradePolicy(order_notional_usd=Decimal("100"), entry_edge_bps=Decimal("10"),
                         take_profit_bps=Decimal("5"), max_loss_bps=Decimal("50"))
    store_path = tmp_path / "paper.sqlite3"
    timing_dir = tmp_path / "timings"
    t0 = 1_700_000_000_000_000_000

    def run_session():
        store = TradeStore(store_path, "fp-paper")
        try:
            state = PaperTradeState(plan, policy, store=store)
            state.update_metadata("BTC-USD-PERP.HYPERLIQUID", _meta("BTC-USD-PERP.HYPERLIQUID", "4.5"), t0, evaluate=False)
            state.update_metadata("BTCUSDT-PERP.ASTER", _meta("BTCUSDT-PERP.ASTER", "4"), t0, evaluate=False)

            # 1. Stale sell book -> BBO qualified, pair_invalid:book_stale_source
            state.update_book("BTC-USD-PERP.HYPERLIQUID", _book("HL", "BTC", [(99.9, 2)], [(100.0, 2)], t0), t0, evaluate=False)
            state.update_book("BTCUSDT-PERP.ASTER", _book("ASTER", "BTC", [(100.6, 2)], [(100.8, 2)], t0 - 3_000_000_000), t0, evaluate=True)

            # 2. Fresh wide spread -> opens position, records exit liquidity and starts alpha decay
            t1 = t0 + 100_000_000
            state.update_book("BTC-USD-PERP.HYPERLIQUID", _book("HL", "BTC", [(99.9, 2)], [(100.0, 2)], t1), t1, evaluate=False)
            state.update_book("BTCUSDT-PERP.ASTER", _book("ASTER", "BTC", [(100.6, 2)], [(100.8, 2)], t1), t1, evaluate=True)
            assert state.sequence == 1

            # 3. Advance books through decay horizons and converge spread to trigger take_profit close
            for ms, sell_bid, sell_ask in ((60, 100.4, 100.5), (200, 100.1, 100.15), (600, 100.0, 100.05), (3100, 100.0, 100.02)):
                ts = t1 + ms * 1_000_000
                state.update_book("BTC-USD-PERP.HYPERLIQUID", _book("HL", "BTC", [(100.3, 2)], [(100.35, 2)], ts), ts, evaluate=False)
                state.update_book("BTCUSDT-PERP.ASTER", _book("ASTER", "BTC", [(sell_bid, 2)], [(sell_ask, 2)], ts), ts, evaluate=True)
                state.on_evaluation_timer(ts)
            assert state.completed == 1
        finally:
            store.close()
        return 0

    assert timing.run_timed(run_session, enabled=True, directory=timing_dir) == 0

    timing_files = list(timing_dir.glob("timing-*.json"))
    assert len(timing_files) == 1
    doc = json.loads(timing_files[0].read_text(encoding="utf-8"))
    an = doc["analysis"]
    assert an["diagnostic_errors"] == 0
    assert an["funnel"]["totals"]["bbo_qualified"] >= 2
    assert an["funnel"]["totals"]["pair_invalid:book_stale_source"] >= 1
    assert an["funnel"]["totals"]["priced_qualified"] >= 1
    assert "BTC|HL->ASTER" in an["exit_liquidity"]["by_route"]
    assert an["alpha_decay"]["by_horizon"]["50"]["samples"] >= 1
    assert an["alpha_decay"]["by_horizon"]["3000"]["samples"] >= 1
    assert an["hotpath"]["emit_persist_ms"]["count"] >= 2

    # Verify unified session analyzer consumes both ledger + timing dir cleanly
    report = analyzer.build_report(ledger=store_path, timing=timing_dir)
    assert report["ledger"]["mode"] == "paper"
    assert report["ledger"]["totals"]["closed"] == 1
    assert "take_profit" in report["ledger"]["by_exit_reason"]
    md = analyzer.render_markdown(report)
    assert "下单前候选漏斗" in md
    assert "反向平仓侧深度" in md
    assert "Alpha Decay" in md
    assert "退出原因" in md


def test_live_hotpath_eval_mode_split_and_privacy(tmp_path):
    plan = _make_plan(tmp_path)
    policy = TradePolicy(order_notional_usd=Decimal("100"), entry_edge_bps=Decimal("10"),
                         take_profit_bps=Decimal("5"), max_loss_bps=Decimal("50"))
    timing_dir = tmp_path / "timings"
    t0 = 1_700_000_000_000_000_000

    def run_live_eval():
        state = LiveTradeState(plan, policy, max_pairs=2, max_gross_notional_usd=Decimal("500"))
        state.phase = "active"
        state.register_instrument("BTC-USD-PERP.HYPERLIQUID", Decimal("0.1"), Decimal("1"), "USDC")
        state.register_instrument("BTCUSDT-PERP.ASTER", Decimal("0.1"), Decimal("1"), "USDT")
        state.update_metadata("BTC-USD-PERP.HYPERLIQUID", _meta("BTC-USD-PERP.HYPERLIQUID", "4.5"), t0, evaluate=False)
        state.update_metadata("BTCUSDT-PERP.ASTER", _meta("BTCUSDT-PERP.ASTER", "4"), t0, evaluate=False)
        # Flat evaluation (after clearing initial full_evaluation_symbols)
        state.full_evaluation_symbols.clear()
        state.update_book("BTC-USD-PERP.HYPERLIQUID", _book("HL", "BTC", [(99.9, 2)], [(100.0, 2)], t0), t0, evaluate=True)
        # Holding evaluation -> forces full scan
        state.positions[("BTC", "ASTER", "HL")] = {
            "position_id": 1, "symbol": "BTC", "pair": ["BTC", "ASTER", "HL"],
            "buy_id": "BTC-USD-PERP.HYPERLIQUID", "sell_id": "BTCUSDT-PERP.ASTER",
            "opened_ns": t0, "status": "open", "exit_reason": None, "exit_requested_ns": None,
            "entry": {
                "sizing": {"common_base_step": "0.001"},
                "buy_metadata": {"multiplier": "1", "size_increment": "0.001"},
                "sell_metadata": {"multiplier": "1", "size_increment": "0.001"},
            },
            "entry_ids": [], "exit_ids": [], "rebalance_ids": [],
            "entry_buy_base": "1", "entry_sell_base": "1",
            "remaining_buy_base": "1", "remaining_sell_base": "1",
            "unhedged_since_ns": None, "unhedged_base": "0", "unhedged_notional_usd": "0",
            "unhedged_duration_ms": "0", "closing_long_cash_usd": "0", "closing_short_cash_usd": "0",
            "closing_fees_usd": "0",
            "actual_entry": {"buy_notional_usd": "100", "sell_notional_usd": "100.5", "buy_fee_usd": "0.04", "sell_fee_usd": "0.04"},
            "pending_commissions": {}, "native_rules": {}, "unknown_commission": False, "reserved_gross_usd": "200",
        }
        state.update_book("BTCUSDT-PERP.ASTER", _book("ASTER", "BTC", [(100.0, 2)], [(100.1, 2)], t0 + 1000), t0 + 1000, evaluate=True)
        # Verify collector sanitizes non-allowlisted strings
        timing.current_analytics().record_pre_gate("secret token with spaces")
        return 0

    assert timing.run_timed(run_live_eval, enabled=True, directory=timing_dir) == 0
    doc = json.loads(next(timing_dir.glob("timing-*.json")).read_text(encoding="utf-8"))
    modes = doc["analysis"]["hotpath"]["evaluate_symbol_by_position_state"]
    assert modes["flat"]["calls"] >= 1
    assert modes["flat"]["full_scans"] == 0
    assert modes["holding"]["calls"] >= 1
    assert modes["holding"]["full_scans"] >= 1
    assert "secret token" not in json.dumps(doc)


def test_memory_diagnostics_process_rss_and_container_counts(tmp_path):
    pm = memory.process_memory()
    if sys.platform in {"darwin", "linux"}:
        assert pm.get("VmRSS", 0) > 0

    diag = memory.MemoryDiagnostics(tmp_path)
    gw = SimpleNamespace(
        orders={"o1": {}}, requests={"r1": {}, "r2": {}}, active_request_map={},
        seen_fills={"f1": {}}, seen_events={"e1", "e2", "e3"},
        terminals={}, emitted=set(), evidence_receipts={}, reads={}, native_reads={},
    )
    diag.attach_gateway(gw)
    observer = SimpleNamespace(
        level_prices={"BTC.HL": {"BUY": {10}, "SELL": {11}}},
        markets={"BTC.HL": SimpleNamespace(venue="HL")},
        snapshot_seen={"BTC.HL"}, dirty={}, pending_snapshot=set(),
        subscribed={"BTC.HL"}, pending_aster=[], backpack_public_health={},
    )
    state = SimpleNamespace(
        books={"BTC.HL": object()}, current={}, positions={("BTC", "ASTER", "HL"): {}},
        full_evaluation_symbols={"BTC", "ETH"}, entry_pause_reason=None, halt_reason=None,
        orders={"o1": {}}, executions={}, historical_positions={}, pending_economics={},
        entry_timings={}, outbox=[], last_closed={}, account_check_metrics={}, native_admission={},
    )
    diag.capture_books(observer, state, 1_000_000_000)
    sample = diag.sample()
    assert sample["strategy_hotpath"]["active_positions"] == 1
    assert sample["strategy_hotpath"]["full_evaluation_symbols"] == 2
    assert sample["strategy_hotpath"]["state_containers"]["orders"] == 1
    assert sample["gateway_containers"]["requests"] == 2
    assert sample["gateway_containers"]["seen_events"] == 3

    # Also verify analyze_memory computes RSS slope and container growth
    row1 = {**sample, "unix_ns": 1_000_000_000, "process_bytes": {"VmRSS": 100 * 1024**2, "VmSwap": 0}}
    row2 = {**sample, "unix_ns": 61_000_000_000, "process_bytes": {"VmRSS": 110 * 1024**2, "VmSwap": 0},
            "gateway_containers": {**sample["gateway_containers"], "seen_events": 13}}
    mem_summary = analyzer.analyze_memory([row1, row2])
    assert mem_summary["rss_slope_mib_per_min"] == 10.0
    assert mem_summary["container_growth"]["gateway"]["seen_events"]["delta"] == 10


def test_live_ledger_analyzer_slippage_rejections_and_funding_boundaries(tmp_path):
    store_path = tmp_path / "live.sqlite3"
    store = TradeStore(store_path, "fp-live")
    # Trade 1: crosses an exact 8h UTC funding boundary (1h/4h/8h), bilateral closed with entry slippage
    t_open = (28800 * 10 - 30) * 1_000_000_000   # 30s before exact 8h boundary
    t_close = (28800 * 10 + 90) * 1_000_000_000  # 90s after exact 8h boundary (hold = 120s)
    events = [
        {
            "event": "orders_prepared", "mode": "live", "session_id": "s1", "observed_ns": t_open,
            "position_id": 1, "pair": ["BTC", "ASTER", "HL"],
            "entry_economics": {"entry_gross_bps": "50", "entry_after_fees_and_reserve_bps": "36.5"},
            "orders": [
                {"client_order_id": "c1", "venue": "HL", "instrument_id": "BTC-USD-PERP.HYPERLIQUID", "leg": "buy", "purpose": "entry"},
                {"client_order_id": "c2", "venue": "ASTER", "instrument_id": "BTCUSDT-PERP.ASTER", "leg": "sell", "purpose": "entry"},
            ],
        },
        {"event": "fill", "mode": "live", "session_id": "s1", "observed_ns": t_open, "position_id": 1, "client_order_id": "c1"},
        {"event": "fill", "mode": "live", "session_id": "s1", "observed_ns": t_open + 5_000_000, "position_id": 1, "client_order_id": "c2"},
        {
            "event": "closed", "mode": "live", "session_id": "s1", "observed_ns": t_close,
            "position_id": 1, "pair": ["BTC", "ASTER", "HL"], "reason": "take_profit",
            "fee_confirmed": True, "net_usd": "0.31", "provisional_net_usd": "0.31",
            "economics": {
                "actual_entry": {"buy_notional_usd": "100", "sell_notional_usd": "100.40", "buy_fee_usd": "0.045", "sell_fee_usd": "0.04"},
                "closing_long_cash_usd": "100.20", "closing_short_cash_usd": "100.12", "closing_fees_usd": "0.085",
                "fee_confirmed": True,
            },
        },
        # Trade 2: single-leg imbalance due to HL ioc_no_match, does not cross 1h boundary
        {
            "event": "orders_prepared", "mode": "live", "session_id": "s1", "observed_ns": t_close + 10_000_000_000,
            "position_id": 2, "pair": ["ETH", "ASTER", "HL"],
            "entry_economics": {"entry_gross_bps": "45", "entry_after_fees_and_reserve_bps": "31.5"},
            "orders": [
                {"client_order_id": "c3", "venue": "HL", "instrument_id": "ETH-USD-PERP.HYPERLIQUID", "leg": "buy", "purpose": "entry"},
                {"client_order_id": "c4", "venue": "ASTER", "instrument_id": "ETHUSDT-PERP.ASTER", "leg": "sell", "purpose": "entry"},
            ],
        },
        {
            "event": "order_terminal", "mode": "live", "session_id": "s1", "observed_ns": t_close + 10_500_000_000,
            "position_id": 2, "client_order_id": "c3", "outcome": "rejected", "native_status": "rejected", "reason_code": "ioc_no_match",
        },
        {"event": "fill", "mode": "live", "session_id": "s1", "observed_ns": t_close + 10_600_000_000, "position_id": 2, "client_order_id": "c4"},
        {"event": "entry_imbalance", "mode": "live", "session_id": "s1", "observed_ns": t_close + 10_700_000_000, "position_id": 2},
        {
            "event": "closed", "mode": "live", "session_id": "s1", "observed_ns": t_close + 15_600_000_000,
            "position_id": 2, "pair": ["ETH", "ASTER", "HL"], "reason": "entry_imbalance",
            "fee_confirmed": True, "net_usd": "-0.12", "provisional_net_usd": "-0.12",
            "economics": {
                "actual_entry": {"buy_notional_usd": "0", "sell_notional_usd": "100", "buy_fee_usd": "0", "sell_fee_usd": "0.04"},
                "closing_long_cash_usd": "0", "closing_short_cash_usd": "100.04", "closing_fees_usd": "0.04",
                "fee_confirmed": True,
            },
        },
    ]
    store.save({"schema_version": 1, "mode": "live"}, events)
    store.close()

    out_md = tmp_path / "report.md"
    assert analyzer.main(["--ledger", str(store_path), "--output", str(out_md)]) == 0
    assert "ioc_no_match" in out_md.read_text(encoding="utf-8")

    summary = analyzer.load_ledger(store_path)
    assert summary["mode"] == "live"
    assert summary["totals"]["closed"] == 2
    assert summary["totals"]["single_leg_imbalance"] == 1
    assert summary["totals"]["single_leg_rate"] == 0.5
    assert summary["rejections_by_venue_reason"]["HL|ioc_no_match"] == 1
    # Expected 50 bps vs actual 40 bps -> +10 bps entry slippage
    assert summary["by_venue_pair"]["HL->ASTER"]["entry_slippage_bps"]["p50"] == 10.0
    assert summary["funding_boundary_split"]["crossed_8h"]["closed"] == 1
    assert summary["funding_boundary_split"]["not_crossed_1h"]["closed"] == 1


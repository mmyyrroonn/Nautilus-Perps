"""Stage 2 verification tests: config switches, paper recording compatibility, and native fast-path hooks."""
from __future__ import annotations

from dataclasses import replace
from decimal import Decimal as D
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import opportunity_live_runner
import opportunity_runtime
from opportunity_core import MarketMetadata, ScanSettings
from opportunity_scan import MarketPlan, ScanConfigError, ScanPlan, parse_plan
from opportunity_trade import main as trade_main

HAS_NATIVE = importlib.util.find_spec("nautilus_trader") is not None


def _scan_doc(**runtime_overrides):
    runtime = {
        "depth_levels": 5,
        "max_levels_per_side": 50,
        "refresh_ms": 100,
        "top_n": 3,
        "duration_secs": 10,
        "connection_timeout_secs": 10,
        "evaluation_interval_ms": 20,
    }
    runtime.update(runtime_overrides)
    return {
        "schema_version": 1,
        "scan": {"reserve_bps": "1"},
        "recording": {"enabled": False},
        "runtime": runtime,
        "markets": [
            {
                "symbol": "BTC",
                "venue": "HL",
                "instrument_id": "BTC-USD-PERP.HYPERLIQUID",
                "quote_to_usd": "1",
                "valuation_source": "synthetic parity",
            },
            {
                "symbol": "BTC",
                "venue": "BACKPACK",
                "instrument_id": "BTC_USDC_PERP.BACKPACK",
                "quote_to_usd": "1",
                "valuation_source": "synthetic parity",
                "backpack_economics": {
                    "margin_init": "0.05",
                    "margin_maint": "0.025",
                    "maker_fee": "0.0002",
                    "taker_fee": "0.0005",
                    "source": "Configured",
                    "source_reference": "test",
                },
            },
        ],
    }


def _write_paper_configs(tmp_path: Path, **scan_runtime) -> Path:
    extras = "\n".join(
        f"{k} = {'true' if isinstance(v, bool) and v else 'false' if isinstance(v, bool) else v}"
        for k, v in scan_runtime.items()
    )
    scan_path = tmp_path / "scan.toml"
    scan_path.write_text(
        f"""schema_version = 1
[scan]
reserve_bps = "1"
[recording]
enabled = false
[runtime]
depth_levels = 5
max_levels_per_side = 50
refresh_ms = 100
top_n = 3
duration_secs = 10
connection_timeout_secs = 10
{extras}
[[markets]]
symbol = "BTC"
venue = "HL"
instrument_id = "BTC-USD-PERP.HYPERLIQUID"
quote_to_usd = "1"
valuation_source = "synthetic parity"
[[markets]]
symbol = "BTC"
venue = "BACKPACK"
instrument_id = "BTC_USDC_PERP.BACKPACK"
quote_to_usd = "1"
valuation_source = "synthetic parity"
[markets.backpack_economics]
margin_init = "0.05"
margin_maint = "0.025"
maker_fee = "0.0002"
taker_fee = "0.0005"
source = "Configured"
source_reference = "test"
""",
        encoding="utf-8",
    )
    trade_path = tmp_path / "trade.toml"
    trade_path.write_text(
        """schema_version = 1
mode = "paper"
scan_config = "scan.toml"
state_path = "runtime/paper.sqlite3"
[trade]
order_notional_usd = "100"
entry_edge_bps = "30"
take_profit_bps = "5"
max_loss_bps = "20"
breakeven_after_secs = 180
force_close_after_secs = 300
""",
        encoding="utf-8",
    )
    return trade_path


def test_scan_plan_stage2_defaults_and_validation(tmp_path):
    default_plan = parse_plan(_scan_doc(), tmp_path)
    assert default_plan.backpack_quote_cache_only is False
    assert default_plan.backpack_depth_coalesce_ms == 0
    assert default_plan.coalesce_book_deltas is False
    assert default_plan.backpack_max_buffer_frames == 2048
    assert default_plan.hl_ws_select_candidates == 1
    assert default_plan.hl_ws_select_probe_ms == 2500

    enabled = parse_plan(
        _scan_doc(
            backpack_quote_cache_only=True,
            backpack_depth_coalesce_ms=5,
            coalesce_book_deltas=True,
            backpack_max_buffer_frames=8192,
            hl_ws_select_candidates=8,
            hl_ws_select_probe_ms=3000,
        ),
        tmp_path,
    )
    assert enabled.backpack_quote_cache_only is True
    assert enabled.backpack_depth_coalesce_ms == 5
    assert enabled.coalesce_book_deltas is True
    assert enabled.backpack_max_buffer_frames == 8192
    assert enabled.hl_ws_select_candidates == 8
    assert enabled.hl_ws_select_probe_ms == 3000

    for bad_runtime in (
        {"backpack_quote_cache_only": "true"},
        {"coalesce_book_deltas": 1},
        {"backpack_depth_coalesce_ms": -1},
        {"backpack_depth_coalesce_ms": 1001},
        {"backpack_depth_coalesce_ms": True},
        {"backpack_max_buffer_frames": 0},
        {"backpack_max_buffer_frames": 16385},
        {"backpack_max_buffer_frames": True},
        {"hl_ws_select_candidates": 0},
        {"hl_ws_select_candidates": 13},
        {"hl_ws_select_candidates": True},
        {"hl_ws_select_probe_ms": 99},
        {"hl_ws_select_probe_ms": 15001},
        {"hl_ws_select_probe_ms": True},
    ):
        with pytest.raises(ScanConfigError):
            parse_plan(_scan_doc(**bad_runtime), tmp_path)


def test_live_config_defaults_and_paper_config_untouched():
    live_plan = opportunity_live_runner.load_live_plan(
        ROOT / "config" / "opportunity-trade.live.toml"
    )
    assert live_plan.scan.evaluation_interval_ms == 20
    assert live_plan.timer_interval_ms == 100
    assert live_plan.scan.backpack_quote_cache_only is True
    assert live_plan.scan.backpack_depth_coalesce_ms == 5
    assert live_plan.scan.backpack_max_buffer_frames == 8192
    assert live_plan.scan.coalesce_book_deltas is True
    assert live_plan.scan.hl_ws_select_candidates == 8
    assert live_plan.scan.hl_ws_select_probe_ms == 2500

    paper_cfg = ROOT / "config" / "opportunity-trade.paper.toml"
    if paper_cfg.is_file():
        from opportunity_trade import load_trade_plan

        paper_plan = load_trade_plan(paper_cfg)
        assert paper_plan.scan.backpack_quote_cache_only is False
        assert paper_plan.scan.backpack_depth_coalesce_ms == 0
        assert paper_plan.scan.backpack_max_buffer_frames == 2048
        assert paper_plan.scan.coalesce_book_deltas is False
        assert paper_plan.scan.hl_ws_select_candidates == 1
        assert paper_plan.scan.hl_ws_select_probe_ms == 2500


def test_trade_cli_preserves_raw_tape_recording_and_supports_toggles(tmp_path, capsys):
    cfg = _write_paper_configs(
        tmp_path,
        backpack_quote_cache_only=True,
        backpack_depth_coalesce_ms=5,
        coalesce_book_deltas=True,
    )
    tape_dir = tmp_path / "tape"

    # Without --tape-dir, scan TOML Stage 2 flags are preserved.
    assert trade_main(["--config", str(cfg), "--dry-run"]) == 0
    doc = json.loads(capsys.readouterr().out)["scan"]
    assert doc["evaluation_interval_ms"] == 100
    assert doc["backpack_quote_cache_only"] is True
    assert doc["backpack_depth_coalesce_ms"] == 5
    assert doc["coalesce_book_deltas"] is True

    # With --tape-dir and --evaluation-interval-ms 0, raw uncoalesced recording defaults apply.
    assert (
        trade_main(
            [
                "--config",
                str(cfg),
                "--dry-run",
                "--tape-dir",
                str(tape_dir),
                "--evaluation-interval-ms",
                "0",
            ]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)["scan"]
    assert doc["evaluation_interval_ms"] == 0
    assert doc["coalesce_book_deltas"] is False
    assert doc["backpack_depth_coalesce_ms"] == 0
    assert doc["backpack_quote_cache_only"] is True

    # Explicit CLI switches opt into coalescing even when recording raw ticks.
    assert (
        trade_main(
            [
                "--config",
                str(cfg),
                "--dry-run",
                "--tape-dir",
                str(tape_dir),
                "--evaluation-interval-ms",
                "0",
                "--coalesce-book-deltas",
                "--backpack-depth-coalesce-ms",
                "9",
                "--no-backpack-quote-cache-only",
            ]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)["scan"]
    assert doc["coalesce_book_deltas"] is True
    assert doc["backpack_depth_coalesce_ms"] == 9
    assert doc["backpack_quote_cache_only"] is False

    # Explicit --no-* / 0 flags override config even outside --tape-dir.
    assert (
        trade_main(
            [
                "--config",
                str(cfg),
                "--dry-run",
                "--no-coalesce-book-deltas",
                "--backpack-depth-coalesce-ms",
                "0",
                "--no-backpack-quote-cache-only",
            ]
        )
        == 0
    )
    doc = json.loads(capsys.readouterr().out)["scan"]
    assert doc["coalesce_book_deltas"] is False
    assert doc["backpack_depth_coalesce_ms"] == 0
    assert doc["backpack_quote_cache_only"] is False


def test_build_node_wires_coalesce_book_deltas_without_native(monkeypatch):
    captured = {}

    class FakeConfig:
        def __init__(self, **kwargs):
            captured.setdefault("configs", []).append(kwargs)
            self.kwargs = kwargs

    class FakeNode:
        def __init__(self):
            self.actors = []

        def handle(self):
            return SimpleNamespace(stop=lambda: None)

        def add_actor(self, actor):
            self.actors.append(actor)

    class FakeBuilder:
        def with_logging(self, *_a):
            return self

        def with_cache_config(self, *_a):
            return self

        def with_data_engine_config(self, cfg):
            captured["data_engine"] = cfg.kwargs
            return self

        def with_load_state(self, *_a):
            return self

        def with_save_state(self, *_a):
            return self

        def with_timeout_connection(self, *_a):
            return self

        def with_timeout_reconciliation(self, *_a):
            return self

        def with_timeout_portfolio(self, *_a):
            return self

        def with_timeout_disconnection_secs(self, *_a):
            return self

        def with_delay_post_stop_secs(self, *_a):
            return self

        def with_delay_shutdown_secs(self, *_a):
            return self

        def add_data_client(self, *_a):
            return self

        def build(self):
            return FakeNode()

    def install(name, **attrs):
        mod = SimpleNamespace(**attrs)
        monkeypatch.setitem(sys.modules, name, mod)

    install("nautilus_trader")
    install(
        "nautilus_trader.common",
        CacheConfig=FakeConfig,
        Environment=SimpleNamespace(LIVE="LIVE"),
        LoggerConfig=FakeConfig,
        LogLevel=SimpleNamespace(WARNING="WARNING", OFF="OFF"),
    )
    install(
        "nautilus_trader.live",
        LiveDataEngineConfig=FakeConfig,
        LiveNode=SimpleNamespace(builder=lambda *_: FakeBuilder()),
    )
    install(
        "nautilus_trader.model",
        TraderId=SimpleNamespace(from_str=lambda v: v),
    )
    monkeypatch.setattr(opportunity_runtime, "data_clients", lambda _plan: [])
    monkeypatch.setattr(opportunity_runtime, "create_observer", lambda *a, **kw: object())

    plan = ScanPlan(
        (MarketPlan("BTC", "HL", "BTC-USD-PERP.HYPERLIQUID", D(1), "synthetic parity"),),
        ScanSettings(),
        None,
        coalesce_book_deltas=True,
    )
    opportunity_runtime.build_node(plan)
    assert captured["data_engine"] == {
        "emit_quotes_from_book": False,
        "emit_quotes_from_book_depths": False,
        "coalesce_book_deltas": True,
    }


def _activate_observer(plan, state, *, backpack_health=None):
    from nautilus_trader.model import InstrumentId

    ids = [InstrumentId.from_str(m.instrument_id) for m in plan.markets]
    instruments = {
        iid: SimpleNamespace(
            id=iid,
            size_increment="0.01",
            multiplier="1",
            min_quantity="0.01",
            min_notional="1 USD",
            taker_fee=D("0"),
        )
        for iid in ids
    }
    configs = {}
    if any(m.venue == "BACKPACK" for m in plan.markets):
        health = backpack_health or {
            "connected": True,
            "metadata_ready": True,
            "connection_epoch": 1,
            "books_continuous": {
                m.instrument_id.removesuffix(".BACKPACK"): True
                for m in plan.markets
                if m.venue == "BACKPACK"
            },
            "books_fresh": {
                m.instrument_id.removesuffix(".BACKPACK"): True
                for m in plan.markets
                if m.venue == "BACKPACK"
            },
        }
        configs["BACKPACK"] = SimpleNamespace(
            telemetry_snapshot_json=lambda: json.dumps(health)
        )
    template = opportunity_runtime.create_observer(
        plan, state, lambda: None, configs, drive_evaluation_timer=False
    )

    class UnderTest(type(template)):
        @property
        def cache(self):
            return SimpleNamespace(instrument=lambda i: instruments.get(i))

        @property
        def clock(self):
            return SimpleNamespace(set_timer=lambda *a, **k: None, cancel_timer=lambda *a, **k: None)

        def subscribe_instrument(self, *a, **k):
            pass

        def subscribe_book_deltas(self, *a, **k):
            pass

        def subscribe_quotes(self, *a, **k):
            pass

        def subscribe_instrument_status(self, *a, **k):
            pass

    observer = UnderTest()
    observer.on_start()
    assert observer.failure is None
    return observer


_TEST_BP_ECONOMICS = {
    "margin_init": "0.1",
    "margin_maint": "0.05",
    "maker_fee": "-0.00001",
    "taker_fee": "0.0002",
    "source": "Configured",
    "source_reference": "stage2-test",
}


@pytest.mark.skipif(not HAS_NATIVE, reason="requires native nautilus_trader wheel")
def test_native_stage2_configs_book_counts_and_observer_zero_copy(monkeypatch):
    from nautilus_trader.adapters.backpack import (
        BackpackDataClientConfig,
        BackpackInstrumentEconomics,
    )
    from nautilus_trader.live import LiveDataEngineConfig
    from nautilus_trader.model import (
        BookAction,
        BookOrder,
        InstrumentId,
        OrderBookDelta,
        OrderBookDeltas,
        OrderSide,
        Price,
        Quantity,
        RecordFlag,
    )

    engine_cfg = LiveDataEngineConfig(
        emit_quotes_from_book=False,
        emit_quotes_from_book_depths=False,
        coalesce_book_deltas=True,
    )
    assert engine_cfg.coalesce_book_deltas is True

    bp_cfg = BackpackDataClientConfig(
        ["BTC_USDC_PERP"],
        {
            "BTC_USDC_PERP": BackpackInstrumentEconomics(
                "0.1", "0.05", "-0.00001", "0.0002", "Synthetic", "stage2-test"
            )
        },
        quote_cache_only=True,
        depth_coalesce_ms=5,
    )
    assert bp_cfg.quote_cache_only is True
    assert bp_cfg.depth_coalesce_ms == 5
    assert bp_cfg.latest_quote("BTC_USDC_PERP") is None

    market = MarketPlan(
        "BTC",
        "BACKPACK",
        "BTC_USDC_PERP.BACKPACK",
        D(1),
        "synthetic parity",
        backpack_economics=_TEST_BP_ECONOMICS,
    )
    client_plan = ScanPlan(
        (market,),
        ScanSettings(),
        None,
        depth_levels=5,
        max_levels_per_side=5,
        evaluation_interval_ms=0,
        backpack_quote_cache_only=True,
        backpack_depth_coalesce_ms=5,
        coalesce_book_deltas=True,
    )
    for _, _factory, cfg in opportunity_runtime.data_clients(client_plan):
        if cfg.__class__.__name__ == "BackpackDataClientConfig":
            assert cfg.quote_cache_only is True
            assert cfg.depth_coalesce_ms == 5

    plan = replace(client_plan, max_levels_per_side=2)

    state = opportunity_runtime.ScanState(plan)
    iid = InstrumentId.from_str(market.instrument_id)
    observer = _activate_observer(plan, state)
    assert observer._native_book_counts is True
    state.update_metadata(
        market.instrument_id,
        MarketMetadata(market.instrument_id, D("0.01"), taker_fee_bps=D("2"), fee_source="test"),
        1,
        evaluate=False,
    )

    # Pre-snapshot delta must be ignored before first snapshot/clear.
    inc_before_snap = OrderBookDeltas(
        iid,
        [
            OrderBookDelta(
                iid,
                BookAction.UPDATE,
                BookOrder(OrderSide.BUY, Price.from_str("99.0"), Quantity.from_str("1.0"), 1),
                RecordFlag.F_LAST.value,
                1,
                10,
                10,
            )
        ],
    )
    observer.on_book_deltas(inc_before_snap)
    assert observer.local_books[iid].bid_count() == 0
    assert not observer.level_prices[iid]["BUY"]

    # Snapshot batch summary & O(1) counts match views.
    snap = OrderBookDeltas(
        iid,
        [
            OrderBookDelta.clear(iid, 2, 20, 20),
            OrderBookDelta(
                iid,
                BookAction.ADD,
                BookOrder(OrderSide.BUY, Price.from_str("100.0"), Quantity.from_str("2.0"), 2),
                RecordFlag.F_SNAPSHOT.value,
                2,
                20,
                20,
            ),
            OrderBookDelta(
                iid,
                BookAction.ADD,
                BookOrder(OrderSide.BUY, Price.from_str("99.0"), Quantity.from_str("3.0"), 3),
                RecordFlag.F_SNAPSHOT.value,
                2,
                20,
                20,
            ),
            OrderBookDelta(
                iid,
                BookAction.ADD,
                BookOrder(OrderSide.SELL, Price.from_str("101.0"), Quantity.from_str("4.0"), 4),
                RecordFlag.F_SNAPSHOT.value | RecordFlag.F_LAST.value,
                2,
                20,
                20,
            ),
        ],
    )
    assert snap.batch_summary() == (4, True, True)
    observer.on_book_deltas(snap)
    book = observer.local_books[iid]
    assert book.bid_count() == 2
    assert book.ask_count() == 1
    assert len(observer.level_prices[iid]["BUY"]) == 2
    assert observer.level_prices[iid]["BUY"] == {level.price.raw for level in book.bids()}
    assert Price.from_str("100.0").raw in observer.level_prices[iid]["BUY"]

    # Exceeding max_levels_per_side (2) triggers fail().
    overflow = OrderBookDeltas(
        iid,
        [
            OrderBookDelta(
                iid,
                BookAction.UPDATE,
                BookOrder(OrderSide.BUY, Price.from_str("98.0"), Quantity.from_str("1.0"), 5),
                RecordFlag.F_LAST.value,
                3,
                30,
                30,
            )
        ],
    )
    observer.on_book_deltas(overflow)
    assert observer.failure and "max_levels_per_side" in observer.failure


@pytest.mark.skipif(not HAS_NATIVE, reason="requires native nautilus_trader wheel")
def test_native_observer_zero_copy_clear_partial_hl_guard_and_parity():
    from nautilus_trader.model import (
        BookAction,
        BookOrder,
        InstrumentId,
        OrderBookDelta,
        OrderBookDeltas,
        OrderSide,
        Price,
        Quantity,
        RecordFlag,
    )

    def make_observer(market, *, max_levels=10, hl_fast=False, force_python_counts=False):
        plan = ScanPlan(
            (market,),
            ScanSettings(),
            None,
            depth_levels=5,
            max_levels_per_side=max_levels,
            evaluation_interval_ms=0,
            hyperliquid_fast_book=hl_fast,
        )
        state = opportunity_runtime.ScanState(plan)
        state.update_metadata(
            market.instrument_id,
            MarketMetadata(
                market.instrument_id, D("0.01"), taker_fee_bps=D("2"), fee_source="test"
            ),
            1,
            evaluate=False,
        )
        obs = _activate_observer(plan, state)
        if force_python_counts:
            obs._native_book_counts = False
            obs.level_prices = {i: {"BUY": set(), "SELL": set()} for i in obs.markets}
        return obs, state

    bp_market = MarketPlan(
        "BTC",
        "BACKPACK",
        "BTC_USDC_PERP.BACKPACK",
        D(1),
        "synthetic parity",
        backpack_economics=_TEST_BP_ECONOMICS,
    )
    iid = InstrumentId.from_str(bp_market.instrument_id)

    # 1. Poisoned .deltas proxy before first snapshot must return via batch_summary() without touching .deltas.
    obs, state = make_observer(bp_market, max_levels=2)

    class _PoisonDeltasProxy:
        instrument_id = iid
        flags = RecordFlag.F_LAST.value
        sequence = 1
        ts_event = 10
        ts_init = 10

        @staticmethod
        def batch_summary():
            return (1, False, False)

        @property
        def deltas(self):
            raise AssertionError("native fast path must not touch .deltas when batch_summary drops pre-snapshot")

    obs.on_book_deltas(_PoisonDeltasProxy())
    assert obs.local_books[iid].bid_count() == 0

    # 2. Mid-batch CLEAR with > max_levels updates before CLEAR must succeed cleanly.
    mid_clear = OrderBookDeltas(
        iid,
        [
            OrderBookDelta(iid, BookAction.UPDATE, BookOrder(OrderSide.BUY, Price.from_str("95.0"), Quantity.from_str("1"), 1), 0, 2, 20, 20),
            OrderBookDelta(iid, BookAction.UPDATE, BookOrder(OrderSide.BUY, Price.from_str("96.0"), Quantity.from_str("1"), 2), 0, 2, 20, 20),
            OrderBookDelta(iid, BookAction.UPDATE, BookOrder(OrderSide.BUY, Price.from_str("97.0"), Quantity.from_str("1"), 3), 0, 2, 20, 20),
            OrderBookDelta.clear(iid, 2, 20, 20),
            OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.BUY, Price.from_str("100.0"), Quantity.from_str("2"), 4), RecordFlag.F_SNAPSHOT.value, 2, 20, 20),
            OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.SELL, Price.from_str("101.0"), Quantity.from_str("2"), 5), RecordFlag.F_SNAPSHOT.value | RecordFlag.F_LAST.value, 2, 20, 20),
        ],
    )
    obs.on_book_deltas(mid_clear)
    assert obs.failure is None
    assert obs.local_books[iid].bid_count() == 1
    assert obs.local_books[iid].ask_count() == 1
    assert bp_market.instrument_id in state.books

    # 3. Partial batch (no F_LAST) invalidates published book until F_LAST recovers.
    partial = OrderBookDeltas(
        iid,
        [
            OrderBookDelta(iid, BookAction.UPDATE, BookOrder(OrderSide.BUY, Price.from_str("99.5"), Quantity.from_str("3"), 6), 0, 3, 30, 30),
        ],
    )
    obs.on_book_deltas(partial)
    assert bp_market.instrument_id not in state.books

    complete = OrderBookDeltas(
        iid,
        [
            OrderBookDelta(iid, BookAction.UPDATE, BookOrder(OrderSide.BUY, Price.from_str("99.5"), Quantity.from_str("4"), 7), RecordFlag.F_LAST.value, 4, 40, 40),
        ],
    )
    obs.on_book_deltas(complete)
    assert obs.failure is None
    assert bp_market.instrument_id in state.books
    assert obs.local_books[iid].bid_count() == 2

    # 4. Exact parity vs Stage 1 Python count tracking (seeded 1-level book + 2 pre-clear updates > max_levels=2 + CLEAR).
    seed_snap = OrderBookDeltas(
        iid,
        [
            OrderBookDelta.clear(iid, 1, 10, 10),
            OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.BUY, Price.from_str("99.0"), Quantity.from_str("1"), 1), RecordFlag.F_SNAPSHOT.value, 1, 10, 10),
            OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.SELL, Price.from_str("102.0"), Quantity.from_str("1"), 2), RecordFlag.F_SNAPSHOT.value | RecordFlag.F_LAST.value, 1, 10, 10),
        ],
    )
    py_mid_clear = OrderBookDeltas(
        iid,
        [
            OrderBookDelta(iid, BookAction.UPDATE, BookOrder(OrderSide.BUY, Price.from_str("95.0"), Quantity.from_str("1"), 1), 0, 2, 20, 20),
            OrderBookDelta(iid, BookAction.UPDATE, BookOrder(OrderSide.BUY, Price.from_str("96.0"), Quantity.from_str("1"), 2), 0, 2, 20, 20),
            OrderBookDelta.clear(iid, 2, 20, 20),
            OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.BUY, Price.from_str("100.0"), Quantity.from_str("2"), 4), RecordFlag.F_SNAPSHOT.value, 2, 20, 20),
            OrderBookDelta(iid, BookAction.ADD, BookOrder(OrderSide.SELL, Price.from_str("101.0"), Quantity.from_str("2"), 5), RecordFlag.F_SNAPSHOT.value | RecordFlag.F_LAST.value, 2, 20, 20),
        ],
    )
    obs_nat2, _ = make_observer(bp_market, max_levels=2)
    obs_py, _ = make_observer(bp_market, max_levels=2, force_python_counts=True)
    for b in (seed_snap, py_mid_clear, partial, complete):
        obs_nat2.on_book_deltas(b)
        obs_py.on_book_deltas(b)
    assert obs_py.failure is None
    assert dict(obs_nat2.local_books[iid].bids_to_dict()) == dict(obs_py.local_books[iid].bids_to_dict())
    assert dict(obs_nat2.local_books[iid].asks_to_dict()) == dict(obs_py.local_books[iid].asks_to_dict())
    assert len(obs_nat2.level_prices[iid]["BUY"]) == len(obs_py.level_prices[iid]["BUY"])
    assert len(obs_nat2.level_prices[iid]["SELL"]) == len(obs_py.level_prices[iid]["SELL"])

    # 5. HYPERLIQUID 5-level fast_book guard fails on >5 levels.
    hl_market = MarketPlan("BTC", "HL", "BTC-USD-PERP.HYPERLIQUID", D(1), "synthetic parity")
    hl_iid = InstrumentId.from_str(hl_market.instrument_id)
    hl_obs, _ = make_observer(hl_market, max_levels=20, hl_fast=True)
    six_bids = [
        OrderBookDelta.clear(hl_iid, 1, 10, 10),
        *[
            OrderBookDelta(
                hl_iid,
                BookAction.ADD,
                BookOrder(OrderSide.BUY, Price.from_str(f"{100 - idx}.0"), Quantity.from_str("1"), idx + 1),
                RecordFlag.F_SNAPSHOT.value,
                1,
                10,
                10,
            )
            for idx in range(6)
        ],
        OrderBookDelta(
            hl_iid,
            BookAction.ADD,
            BookOrder(OrderSide.SELL, Price.from_str("101.0"), Quantity.from_str("1"), 99),
            RecordFlag.F_SNAPSHOT.value | RecordFlag.F_LAST.value,
            1,
            10,
            10,
        ),
    ]
    hl_obs.on_book_deltas(OrderBookDeltas(hl_iid, six_bids))
    assert hl_obs.failure and "more than five levels" in hl_obs.failure


@pytest.mark.skipif(not HAS_NATIVE, reason="requires native nautilus_trader wheel")
def test_native_backpack_quote_cache_only_depth_coalesce_and_admission(tmp_path):
    import asyncio
    import time

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from backpack_production_peer import ProductionPeer, SEED, SYMBOLS
    bp = pytest.importorskip("nautilus_trader.adapters.backpack")
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveDataEngineConfig, LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import (
        BookType,
        ClientId,
        InstrumentId,
        OmsType,
        OrderSide,
        Price,
        Quantity,
        StrategyId,
        TimeInForce,
        TraderId,
    )
    from nautilus_trader.trading import Strategy, StrategyConfig

    async def scenario():
        async with ProductionPeer() as peer:
            endpoints = dict(base_url_http=peer.http_url, base_url_ws=peer.ws_url)
            quota = bp.BackpackQuota()
            credential = bp.BackpackCredential(SEED, **endpoints)
            economics = bp.BackpackInstrumentEconomics(
                "0.1", "0.05", "-0.00001", "0.0002", "Synthetic", "stage2-loopback"
            )
            public = bp.BackpackDataClientConfig(
                list(SYMBOLS),
                {symbol: economics for symbol in SYMBOLS},
                http_timeout_secs=3,
                quote_stale_after_ms=1000,
                quote_cache_only=True,
                depth_coalesce_ms=5,
                **endpoints,
            )
            account = bp.BackpackExecutionClientConfig(
                list(SYMBOLS),
                credential,
                "BACKPACK-STAGE2-PEER",
                "stage2-test",
                str(tmp_path / "identity"),
                quota=quota,
                connect_timeout_ms=10000,
                shutdown_timeout_ms=1000,
                recovery_interval_ms=1000,
                page_size=1000,
                max_pages=10,
                max_items=10000,
                read_timeout_ms=30000,
                **endpoints,
            )
            authority = bp.BackpackProductionExecutionAuthority(
                expires_at_ms=time.time_ns() // 1_000_000 + 180_000,
                max_account_age_ms=10000,
                max_market_age_ms=10000,
                max_order_notional="5",
                max_reserved_notional="10",
                max_reserved_margin="10",
                max_unsettled_orders=1,
                allow_new_risk=True,
                allow_reduction=True,
                allow_owned_cancel=True,
            )
            plan = bp.BackpackProductionExecutionClientConfig(
                account,
                public,
                authority=authority,
                mutation_budget_ms=1000,
                receive_window_ms=5000,
                economic_state_directory=str(tmp_path / "economics"),
            )
            control = plan.control

            class Stage2Probe(Strategy):
                def __init__(self):
                    super().__init__(
                        StrategyConfig(
                            strategy_id=StrategyId.from_str("STAGE2-001"),
                            oms_type=OmsType.NETTING,
                            log_events=False,
                            log_commands=False,
                        )
                    )
                    self.quote_callbacks = 0
                    self.book_callbacks = 0
                    self.accepted = None
                    self.canceled = False
                    self.denied = None

                def on_start(self):
                    client_id = ClientId.from_str("BACKPACK")
                    for symbol in SYMBOLS:
                        iid = InstrumentId.from_str(f"{symbol}.BACKPACK")
                        self.subscribe_quotes(iid, client_id=client_id)
                        self.subscribe_book_deltas(
                            iid, book_type=BookType.L2_MBP, client_id=client_id
                        )

                def on_quote_tick(self, _tick):
                    self.quote_callbacks += 1

                def on_book_deltas(self, _deltas):
                    self.book_callbacks += 1

                def on_order_accepted(self, event):
                    self.accepted = event.client_order_id

                def on_order_canceled(self, _event):
                    self.canceled = True

                def on_order_denied(self, event):
                    self.denied = str(event.reason)

            node = (
                LiveNode.builder("STAGE2-BACKPACK", TraderId.from_str("STAGE2-001"), Environment.LIVE)
                .with_logging(
                    LoggerConfig(
                        stdout_level=LogLevel.OFF,
                        fileout_level=LogLevel.OFF,
                        bypass_logging=True,
                        print_config=False,
                    )
                )
                .with_timeout_connection(10)
                .with_reconciliation(False)
                .with_timeout_reconciliation(0)
                .with_timeout_portfolio(0)
                .with_timeout_disconnection_secs(2)
                .with_delay_post_stop_secs(0)
                .with_delay_shutdown_secs(0)
                .with_load_state(False)
                .with_save_state(False)
                .with_data_engine_config(
                    LiveDataEngineConfig(
                        emit_quotes_from_book=False,
                        emit_quotes_from_book_depths=False,
                        coalesce_book_deltas=True,
                    )
                )
                .with_exec_engine_config(
                    LiveExecutionEngineConfig(
                        reconciliation=False,
                        inflight_check_interval_ms=0,
                        open_check_interval_secs=None,
                        position_check_interval_secs=None,
                    )
                )
                .add_data_client(None, bp.BackpackDataClientFactory(quota=quota), public)
                .add_exec_client(None, bp.BackpackProductionExecutionClientFactory(), plan)
                .build()
            )
            strategy = Stage2Probe()
            node.add_strategy(strategy)
            task = asyncio.ensure_future(node.run_async())
            handle = node.handle()
            deadline = time.monotonic() + 20
            submitted = False
            try:
                while time.monotonic() < deadline and not strategy.canceled:
                    assert not peer.errors, peer.errors
                    assert strategy.denied is None, strategy.denied
                    pub_health = json.loads(public.telemetry_snapshot_json())
                    priv_health = json.loads(control.telemetry_snapshot_json())
                    admission = json.loads(control.admission_status_json())
                    if (
                        not submitted
                        and pub_health["quotes_fresh"].get(SYMBOLS[0])
                        and priv_health["transport_connected"]
                        and priv_health["rest_snapshot_observed"]
                        and admission["ready"]
                        and strategy.book_callbacks >= 2
                    ):
                        # Submit through execution admission even though cache.quote stays empty.
                        assert strategy.cache.quote(InstrumentId.from_str(f"{SYMBOLS[0]}.BACKPACK")) is None
                        order = strategy.order_factory.limit(
                            InstrumentId.from_str(f"{SYMBOLS[0]}.BACKPACK"),
                            OrderSide.BUY,
                            Quantity.from_str("0.00002"),
                            Price.from_str("100.1"),
                            time_in_force=TimeInForce.GTC,
                            post_only=True,
                        )
                        strategy.submit_order(order)
                        submitted = True
                    elif strategy.accepted is not None:
                        strategy.cancel_order(strategy.accepted)
                        strategy.accepted = None
                    await asyncio.sleep(0.05)
                assert strategy.canceled, {
                    "book_callbacks": strategy.book_callbacks,
                    "pub_health": json.loads(public.telemetry_snapshot_json()),
                    "admission": json.loads(control.admission_status_json()),
                }
                assert strategy.quote_callbacks == 0
                assert strategy.cache.quote(InstrumentId.from_str(f"{SYMBOLS[0]}.BACKPACK")) is None
                cached = public.latest_quote(SYMBOLS[0])
                assert cached is not None
                assert str(cached.bid_price) == "100.0" and str(cached.ask_price) == "101.0"
                assert len(peer.posts) == 1 and len(peer.deletes) == 1
            finally:
                handle.stop()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 8)
                finally:
                    node.dispose()

    asyncio.run(scenario())


def test_runtime_and_execution_propagate_backpack_buffer_and_hl_ws_selection(monkeypatch):
    import opportunity_native_execution
    import spread_watch

    captured = {}

    class FakeObj:
        def __init__(self, **kwargs):
            self.kwargs = kwargs

    def fake_bp_config(symbols, economics, **kwargs):
        captured["bp_data"] = kwargs
        return FakeObj(**kwargs)

    def fake_hl_data_config(**kwargs):
        captured["hl_data"] = kwargs
        return FakeObj(**kwargs)

    def fake_hl_exec_config(**kwargs):
        captured["hl_exec"] = kwargs
        return FakeObj(**kwargs)

    monkeypatch.setitem(
        sys.modules,
        "nautilus_trader.adapters.backpack",
        SimpleNamespace(
            BackpackDataClientConfig=fake_bp_config,
            BackpackDataClientFactory=lambda **kw: FakeObj(**kw),
            BackpackInstrumentEconomics=lambda **kw: FakeObj(**kw),
        ),
    )
    monkeypatch.setitem(
        sys.modules,
        "nautilus_trader.adapters.hyperliquid",
        SimpleNamespace(
            HyperliquidDataClientConfig=fake_hl_data_config,
            HyperliquidDataClientFactory=lambda: FakeObj(),
            HyperliquidExecutionClientConfig=fake_hl_exec_config,
            HyperliquidExecutionClientFactory=lambda: FakeObj(),
            HyperliquidEnvironment=SimpleNamespace(MAINNET="MAINNET", TESTNET="TESTNET"),
        ),
    )
    monkeypatch.setattr(
        opportunity_native_execution,
        "_native",
        lambda: SimpleNamespace(AccountId=SimpleNamespace(from_str=lambda s: s)),
    )
    # Keep spread_watch lazy import pointed at our fake hyperliquid module.
    _ = spread_watch

    markets = (
        MarketPlan("BTC", "HL", "BTC-USD-PERP.HYPERLIQUID", D(1), "synthetic parity"),
        MarketPlan(
            "BTC",
            "BACKPACK",
            "BTC_USDC_PERP.BACKPACK",
            D(1),
            "synthetic parity",
            backpack_economics=_TEST_BP_ECONOMICS,
        ),
    )
    plan = ScanPlan(
        markets,
        ScanSettings(),
        None,
        hyperliquid_bbo_gate=True,
        backpack_quote_cache_only=True,
        backpack_depth_coalesce_ms=5,
        backpack_max_buffer_frames=8192,
        hl_ws_select_candidates=8,
        hl_ws_select_probe_ms=2500,
    )

    clients = opportunity_runtime.data_clients(plan)
    assert len(clients) == 2
    assert captured["bp_data"]["max_buffer_frames"] == 8192
    assert captured["bp_data"]["quote_cache_only"] is True
    assert captured["bp_data"]["depth_coalesce_ms"] == 5
    assert captured["hl_data"]["bbo_cache_only"] is True
    assert captured["hl_data"]["ws_select_candidates"] == 8
    assert captured["hl_data"]["ws_select_probe_ms"] == 2500

    hl_only_plan = ScanPlan(
        (markets[0],),
        ScanSettings(),
        None,
        hl_ws_select_candidates=8,
        hl_ws_select_probe_ms=2500,
    )
    exec_clients = opportunity_native_execution.execution_clients(
        hl_only_plan,
        "mainnet",
        environ={
            "HYPERLIQUID_ACCOUNT_ADDRESS": "0x1111111111111111111111111111111111111111",
            "HYPERLIQUID_PK": "0x59c6995e998f97a5a0044966f094538a1da6d1310dce3f687da73cf015b05d7e",
        },
    )
    assert len(exec_clients) == 1
    assert captured["hl_exec"]["ws_select_candidates"] == 8
    assert captured["hl_exec"]["ws_select_probe_ms"] == 2500

    # Disabled (1 candidate) omits ws_select_* kwargs for compatibility.
    captured.clear()
    default_plan = ScanPlan((markets[0],), ScanSettings(), None, hyperliquid_bbo_gate=True)
    opportunity_runtime.data_clients(default_plan)
    opportunity_native_execution.execution_clients(
        default_plan,
        "mainnet",
        environ={
            "HYPERLIQUID_ACCOUNT_ADDRESS": "0x1111111111111111111111111111111111111111",
            "HYPERLIQUID_PK": "0x59c6995e998f97a5a0044966f094538a1da6d1310dce3f687da73cf015b05d7e",
        },
    )
    assert "ws_select_candidates" not in captured["hl_data"]
    assert "ws_select_candidates" not in captured["hl_exec"]

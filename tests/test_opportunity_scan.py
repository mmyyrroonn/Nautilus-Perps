"""Public runtime boundaries, native depth lifecycle and event-only integration."""
from __future__ import annotations

import builtins
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal, Inexact, localcontext
from fractions import Fraction
import io
import json
from pathlib import Path
import socket
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opportunity_scan as scan
import opportunity_runtime as runtime
from opportunity_core import BookSnapshot, MarketMetadata, evaluate_opportunity

NS = 1_800_000_000_000_000_000
ROOT = Path(__file__).resolve().parents[1]


def document(tmp_path):
    return {"schema_version": 1,
        "scan": {"target_notional": "100", "reserve_bps": "1"},
        "recording": {"path": str(tmp_path / "events" / "opportunities.jsonl")},
        "markets": [{"symbol": "BTC", "venue": venue, "instrument_id": instrument,
            "quote_to_usd": "1", "valuation_source": "synthetic test parity",
            "taker_fee_bps": "1", "fee_source": "synthetic test rate"}
            for venue, instrument in [("ONDO", "BTC-USD-PERP.ONDO"), ("ASTER", "BTCUSDT-PERP.ASTER")]]}


def plan(tmp_path):
    return scan.parse_plan(document(tmp_path), tmp_path)


def meta(instrument):
    return MarketMetadata(instrument, Decimal("0.01"), Decimal("1"), Decimal("1"), "synthetic")


def book(venue, bid, ask, now=NS):
    return BookSnapshot(venue, "BTC", ((Decimal(bid), Decimal("10")),),
        ((Decimal(ask), Decimal("10")),), now, now, coverage_limit=1)


def test_dry_run_has_no_native_credentials_socket_or_output(tmp_path, monkeypatch, capsys):
    config = tmp_path / "scan.toml"
    config.write_bytes((ROOT / "config/opportunity-scan.example.toml").read_bytes())
    original_import = builtins.__import__
    def guarded_import(name, *args, **kwargs):
        if name.startswith(("nautilus_trader", "dotenv", "opportunity_runtime")):
            raise AssertionError("unexpected runtime import")
        return original_import(name, *args, **kwargs)
    def forbidden(*args, **kwargs):
        raise AssertionError("unexpected side effect")
    monkeypatch.setattr(builtins, "__import__", guarded_import)
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(Path, "mkdir", forbidden)
    assert scan.main(["--config", str(config), "--dry-run", "--no-record", "--symbols", "BTC",
                      "--target-notional", "1000", "--duration-secs", "0"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["settings"]["target_notional"] == "1000"
    assert result["output_path"] is None
    assert result["duration_secs"] == 0
    assert len(result["markets"]) == 5
    assert result["runtime_started"] is False
    assert result["remote_writes_allowed"] is False
    assert list(tmp_path.iterdir()) == [config]


@pytest.mark.parametrize("section,key,value", [
    ("scan", "target_notional", "NaN"), ("scan", "target_notional", 0),
    ("scan", "max_age_ms", True), ("scan", "max_skew_ms", -1),
    ("recording", "enabled", "false"), ("recording", "path", ".env"),
    ("recording", "cooldown_ms", -1), ("recording", "max_interval_ms", 1),
    ("runtime", "depth_levels", 2001), ("runtime", "duration_secs", -1),
    ("runtime", "refresh_ms", 0), ("runtime", "private_key", "forbidden_field"),
])
def test_invalid_runtime_config_is_rejected(tmp_path, section, key, value):
    value_doc = document(tmp_path)
    value_doc.setdefault(section, {})[key] = value
    with pytest.raises(scan.ScanConfigError):
        scan.parse_plan(value_doc, tmp_path)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("key,value", [
    ("venue", "UNKNOWN"), ("instrument_id", "BTC.BACKPACK"),
    ("quote_to_usd", "Infinity"), ("valuation_source", ""),
    ("fee_source", ""), ("canonical_multiplier", "0"),
    ("backpack_economics", None), ("backpack_economics", []), ("backpack_economics", 0),
])
def test_invalid_market_config_is_rejected(tmp_path, key, value):
    d = document(tmp_path)
    d["markets"][0][key] = value
    with pytest.raises(scan.ScanConfigError):
        scan.parse_plan(d, tmp_path)


def test_duplicate_or_single_leg_underlying_is_rejected(tmp_path):
    d = document(tmp_path)
    d["markets"].append(deepcopy(d["markets"][0]))
    with pytest.raises(scan.ScanConfigError):
        scan.parse_plan(d, tmp_path)
    d = document(tmp_path)
    d["markets"][1]["symbol"] = "ETH"
    with pytest.raises(scan.ScanConfigError):
        scan.parse_plan(d, tmp_path)


def test_configured_native_fee_zero_and_unknown_have_distinct_sources(tmp_path):
    m = plan(tmp_path).markets[0]
    instrument = SimpleNamespace(id=m.instrument_id, size_increment="0.01", multiplier="1",
                                 taker_fee=Decimal(0), min_quantity="0.02", min_notional="5 USD")
    assert runtime.market_metadata(m, instrument).fee_source == "synthetic test rate"
    live = replace(m, taker_fee_bps=None, fee_source=None)
    assert runtime.market_metadata(live, instrument).taker_fee_bps is None
    instrument.taker_fee = Decimal("0.00025")
    assert runtime.market_metadata(live, instrument).taker_fee_bps == Decimal("2.5")
    instrument.is_inverse = True
    with pytest.raises(scan.ScanConfigError):
        runtime.market_metadata(m, instrument)


@pytest.mark.parametrize("field,value", [
    ("min_quantity", "NaN"), ("min_quantity", "Infinity"), ("min_quantity", "invalid"),
    ("min_notional", "NaN USD"), ("min_notional", "Infinity USD"), ("min_notional", "-1 USD"),
])
def test_present_invalid_native_minimum_is_rejected(tmp_path, field, value):
    m = plan(tmp_path).markets[0]
    instrument = SimpleNamespace(id=m.instrument_id, size_increment="0.01", multiplier="1")
    setattr(instrument, field, value)
    with pytest.raises(scan.ScanConfigError):
        runtime.market_metadata(m, instrument)


def test_native_multiplier_and_canonical_multiplier_preserve_actual_cash(tmp_path):
    p = plan(tmp_path)
    buy, sell = p.markets
    buy_instrument = SimpleNamespace(id=buy.instrument_id, size_increment="0.01", multiplier="2")
    sell_instrument = SimpleNamespace(id=sell.instrument_id, size_increment="0.01", multiplier="1")
    for canonical_factor, raw_price, sell_price, expected_base in [
            ("1", "100", "102", "1"), ("1000", "100", "0.102", "1000")]:
        buy = replace(buy, canonical_multiplier=Decimal(canonical_factor))
        buy_meta = runtime.market_metadata(buy, buy_instrument)
        sell_meta = runtime.market_metadata(sell, sell_instrument)
        ask = Decimal(raw_price)
        bid = Decimal(sell_price)
        buy_book = BookSnapshot(buy.venue, "BTC", runtime.normalized_levels(buy, ((ask-1, Decimal(10)),)),
            runtime.normalized_levels(buy, ((ask, Decimal(10)),)), NS, NS)
        sell_book = BookSnapshot(sell.venue, "BTC", ((bid, Decimal(10000)),),
            ((bid+1, Decimal(10000)),), NS, NS)
        result = evaluate_opportunity(buy_book, buy_meta, sell_book, sell_meta, p.settings, NS)
        assert result is not None
        assert Decimal(result["sizing"]["base_quantity"]) == Decimal(expected_base)
        assert Decimal(result["sizing"]["buy_quantity"]) == Decimal("0.5")
        assert Decimal(result["sizing"]["sell_quantity"]) == Decimal(expected_base)
        assert Decimal(result["sizing"]["buy_notional"]) == Decimal("100")
        assert Decimal(result["sizing"]["sell_notional"]) == Decimal("102")
        assert Fraction(Decimal(result["sizing"]["buy_notional"])) == (
            Fraction(Decimal(result["sizing"]["buy_quantity"])) * 2 * Fraction(ask))


def test_runtime_normalization_is_exact_or_rejected_independent_of_decimal_context(tmp_path):
    m = replace(plan(tmp_path).markets[0], quote_to_usd=Decimal("1.00000000000000000000000000001"))
    instrument = SimpleNamespace(id=m.instrument_id, size_increment="0.01", multiplier="2",
        taker_fee=Decimal("0.0000000000000000000000000000123"), min_notional="123 USD")
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        metadata = runtime.market_metadata(replace(m, taker_fee_bps=None, fee_source=None), instrument)
        price = runtime.normalized_levels(m, ((Decimal(123), Decimal(10)),))[0][0]
    assert Fraction(price) == Fraction(123) * Fraction(m.quote_to_usd)
    assert Fraction(metadata.min_notional) == Fraction(price)
    assert Fraction(metadata.taker_fee_bps) == Fraction(instrument.taker_fee) * 10000
    with pytest.raises(ValueError, match="exact finite decimal"):
        runtime.normalized_levels(replace(m, quote_to_usd=Decimal(1), canonical_multiplier=Decimal(3)),
                                  ((Decimal(100), Decimal(10)),))


def test_state_writes_only_qualified_paired_snapshots_and_expires_without_output(tmp_path):
    p = plan(tmp_path)
    state = runtime.ScanState(p)
    for m in p.markets:
        state.update_metadata(m.instrument_id, meta(m.instrument_id), NS)
    state.update_book(p.markets[0].instrument_id, book("ONDO", "99", "100"), NS)
    assert not list(tmp_path.iterdir())
    state.update_book(p.markets[1].instrument_id, book("ASTER", "99", "100"), NS)
    assert not list(tmp_path.iterdir())
    raw = {"bids": [["102", "10"]], "sequence": 2}
    state.update_book(p.markets[1].instrument_id, book("ASTER", "102", "103"), NS, raw_book=raw)
    lines = p.output_path.read_text().splitlines()
    assert len(lines) == 1
    event = json.loads(lines[0])["opportunity"]
    assert event["buy_venue"] == "ONDO" and event["sell_venue"] == "ASTER"
    assert event["raw_sell_book"] == raw
    raw["bids"][0][0] = "999"
    assert state.raw_books[p.markets[1].instrument_id]["bids"] == [["102", "10"]]
    assert event["sell_book"]["ts_event_ns"] == NS
    assert len(state.current) == 1
    state.expire(NS + 3_000_000_000)
    assert state.current == {}
    assert p.output_path.read_text().splitlines() == lines
    assert len(state.books) == 2


def test_record_disabled_retains_only_current_state_and_display_is_bounded(tmp_path):
    p = replace(plan(tmp_path), output_path=None, top_n=1)
    state = runtime.ScanState(p)
    for m in p.markets:
        state.update_metadata(m.instrument_id, meta(m.instrument_id), NS)
    for i in range(1000):
        state.update_book(p.markets[0].instrument_id, book("ONDO", "99", "100", NS+i), NS+i)
        state.update_book(p.markets[1].instrument_id, book("ASTER", "102", "103", NS+i), NS+i)
    assert len(state.books) == 2 and len(state.current) == 1 and state.saved == 0
    assert not list(tmp_path.iterdir())
    stream = io.StringIO()
    state.display(stream)
    assert "current opportunities=1" in stream.getvalue()
    assert len(stream.getvalue().splitlines()) == 2
    state.invalidate(p.markets[0].instrument_id, NS+1001, metadata=True)
    assert state.current == {} and len(state.metadata) == 1


def native_observer(tmp_path, monkeypatch, *, backpack=False, **overrides):
    native = pytest.importorskip("nautilus_trader.model")
    p = replace(plan(tmp_path), **overrides)
    if backpack:
        m = replace(p.markets[1], venue="BACKPACK", instrument_id="BTC_USDC_PERP.BACKPACK")
        p = replace(p, markets=(p.markets[0], m))
    state = runtime.ScanState(p)
    ids = [native.InstrumentId.from_str(m.instrument_id) for m in p.markets]
    instruments = {i: SimpleNamespace(id=i, size_increment="0.01", multiplier="1", min_quantity="0.01",
        min_notional="5 USD", taker_fee=Decimal("0.0001")) for i in ids}
    calls = []
    monkeypatch.setattr(runtime.time, "time_ns", lambda: NS)
    health = {"connected": True, "metadata_ready": True, "connection_epoch": 1,
              "books_continuous": {"BTC_USDC_PERP": True}, "books_fresh": {"BTC_USDC_PERP": True}}
    configs = {"BACKPACK": SimpleNamespace(telemetry_snapshot_json=lambda: json.dumps(health))} if backpack else {}
    template = runtime.create_observer(p, state, lambda: calls.append("stop"), configs)
    class UnderTest(type(template)):
        @property
        def cache(self):
            return SimpleNamespace(instrument=lambda i: instruments.get(i))
        @property
        def clock(self):
            return SimpleNamespace(set_timer=lambda *a, **k: calls.append("timer"),
                                   cancel_timer=lambda *a, **k: calls.append("cancel_timer"))
        def subscribe_instrument(self, *a, **k):
            calls.append("instrument")
        def subscribe_book_deltas(self, *a, **k):
            assert k["managed"] is False
            calls.append("deltas")
        def subscribe_instrument_status(self, *a, **k):
            calls.append("status")
        def unsubscribe_instrument(self, *a, **k):
            pass
        def unsubscribe_book_deltas(self, *a, **k):
            pass
        def unsubscribe_instrument_status(self, *a, **k):
            pass
    observer = UnderTest()
    observer.on_start()
    assert observer.failure is None
    return p, state, observer, ids, calls, health


def batch(instrument_id, bid, ask, *, snapshot=True):
    from nautilus_trader.model import BookAction, BookOrder, OrderBookDelta, OrderBookDeltas, OrderSide, Price, Quantity, RecordFlag
    flags = RecordFlag.F_SNAPSHOT.value if snapshot else 0
    deltas = [OrderBookDelta.clear(instrument_id, 1, NS, NS)] if snapshot else []
    for side, price in [(OrderSide.BUY, bid), (OrderSide.SELL, ask)]:
        deltas.append(OrderBookDelta(instrument_id, BookAction.UPDATE,
            BookOrder(side, Price.from_str(price), Quantity.from_str("10.00"), 0),
            flags | (RecordFlag.F_LAST.value if side == OrderSide.SELL else 0), 1, NS, NS))
    return OrderBookDeltas(instrument_id, deltas)


def test_native_complete_snapshot_records_exact_prices_and_no_trades_subscription(tmp_path, monkeypatch):
    p, state, observer, ids, calls, _ = native_observer(tmp_path, monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    observer.on_book_deltas(batch(ids[1], "102.00", "103.00"))
    assert observer.failure is None
    assert state.saved == 1 and len(state.current) == 1
    event = json.loads(p.output_path.read_text())["opportunity"]
    assert event["raw_buy_book"]["asks"] == [["100.00", "10"]]
    assert event["raw_sell_book"]["bids"] == [["102.00", "10"]]
    assert event["buy_book"]["ts_received_ns"] == NS
    assert calls.count("deltas") == 2
    observer.on_stop()
    observer.on_book_deltas(batch(ids[1], "110.00", "111.00"))
    assert state.saved == 1 and state.current == {}


def test_native_initial_incremental_and_partial_snapshot_never_qualify(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader.model")
    from nautilus_trader.model import OrderBookDeltas
    _, state, observer, ids, _, _ = native_observer(tmp_path, monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00", snapshot=False))
    assert not state.books
    full = batch(ids[0], "99.00", "100.00")
    observer.on_book_deltas(OrderBookDeltas(ids[0], full.deltas[:-1]))
    assert not state.books
    observer.on_book_deltas(OrderBookDeltas(ids[0], full.deltas[-1:]))
    assert len(state.books) == 1 and state.saved == 0
    assert state.books[str(ids[0])].bids == ((Decimal("99"), Decimal("10")),)


def test_native_partial_incremental_invalidates_old_published_book(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader.model")
    from nautilus_trader.model import OrderBookDeltas
    _, state, observer, ids, _, _ = native_observer(tmp_path, monkeypatch)
    for i, b, a in [(ids[0], "99.00", "100.00"), (ids[1], "102.00", "103.00")]:
        observer.on_book_deltas(batch(i, b, a))
    assert state.current
    increment = batch(ids[0], "99.00", "101.00", snapshot=False)
    observer.on_book_deltas(OrderBookDeltas(ids[0], increment.deltas[:1]))
    assert not state.current and str(ids[0]) not in state.books
    observer.on_book_deltas(OrderBookDeltas(ids[0], increment.deltas[1:]))
    assert state.current and state.books[str(ids[0])].asks[0][0] == Decimal(100)
    assert state.books[str(ids[0])].asks[1][0] == Decimal(101)


def test_native_depth_overflow_stops_without_publishing_incomplete_book(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader.model")
    from nautilus_trader.model import OrderBookDeltas
    _, state, observer, ids, calls, _ = native_observer(
        tmp_path, monkeypatch, depth_levels=1, max_levels_per_side=1)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    increment = batch(ids[0], "98.00", "101.00", snapshot=False)
    observer.on_book_deltas(OrderBookDeltas(ids[0], increment.deltas))
    assert observer.failure and "max_levels_per_side" in observer.failure
    assert not observer.running and "stop" in calls and not state.current


def test_native_ladder_cap_releases_deleted_prices_and_resets_with_snapshots(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader.model")
    from nautilus_trader.model import BookAction, BookOrder, OrderBookDelta, OrderBookDeltas, OrderSide, Price, Quantity, RecordFlag
    _, state, observer, ids, _, _ = native_observer(
        tmp_path, monkeypatch, depth_levels=2, max_levels_per_side=2)
    instrument_id = ids[0]
    observer.on_book_deltas(batch(instrument_id, "99.00", "100.00"))
    def change(action, price, size):
        return OrderBookDelta(instrument_id, action,
            BookOrder(OrderSide.BUY, Price.from_str(price), Quantity.from_str(size), 0),
            RecordFlag.F_LAST.value, 1, NS, NS)
    for action, price, size in [(BookAction.UPDATE, "98.00", "2.00"),
                                (BookAction.UPDATE, "98.00", "3.00"),
                                (BookAction.DELETE, "99.00", "0.00"),
                                (BookAction.UPDATE, "97.00", "4.00"),
                                (BookAction.DELETE, "98.00", "0.00")]:
        observer.on_book_deltas(OrderBookDeltas(instrument_id, [change(action, price, size)]))
        assert observer.failure is None
        book = observer.local_books[instrument_id]
        assert observer.level_prices[instrument_id]["BUY"] == {level.price.raw for level in book.bids()}
    assert len(observer.level_prices[instrument_id]["BUY"]) == 1
    observer.invalidate(instrument_id)
    assert not observer.level_prices[instrument_id]["BUY"]
    assert str(instrument_id) not in state.books
    observer.on_book_deltas(batch(instrument_id, "95.00", "96.00"))
    assert len(observer.level_prices[instrument_id]["BUY"]) == 1
    assert state.books[str(instrument_id)].bids[0][0] == Decimal("95")


def test_native_halt_is_not_lifted_by_feed_snapshot_ready(tmp_path, monkeypatch):
    _, state, observer, ids, _, _ = native_observer(tmp_path, monkeypatch)
    for i, b, a in [(ids[0], "99.00", "100.00"), (ids[1], "102.00", "103.00")]:
        observer.on_book_deltas(batch(i, b, a))
    assert state.current
    observer.on_instrument_status(SimpleNamespace(instrument_id=ids[0], reason="venue halt", is_trading=False))
    observer.on_instrument_status(SimpleNamespace(instrument_id=ids[0], reason="adapter:snapshot_ready", is_trading=True))
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    assert not state.current and state.saved == 1


def test_native_halt_action_without_boolean_is_respected(tmp_path, monkeypatch):
    _, state, observer, ids, _, _ = native_observer(tmp_path, monkeypatch)
    for i, b, a in [(ids[0], "99.00", "100.00"), (ids[1], "102.00", "103.00")]:
        observer.on_book_deltas(batch(i, b, a))
    assert state.current
    observer.on_instrument_status(SimpleNamespace(instrument_id=ids[0], reason="venue halt",
        is_trading=None, action=SimpleNamespace(name="HALT")))
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    assert not state.current
    observer.on_instrument_status(SimpleNamespace(instrument_id=ids[0], reason="venue resume",
        is_trading=None, action=SimpleNamespace(name="TRADING")))
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    assert state.current


def test_single_market_update_does_not_touch_other_symbols(tmp_path, monkeypatch):
    p = plan(tmp_path)
    other = tuple(replace(m, symbol="ETH", instrument_id=m.instrument_id.replace("BTC", "ETH")) for m in p.markets)
    p = replace(p, markets=p.markets+other, output_path=None)
    state = runtime.ScanState(p)
    for m in p.markets:
        state.update_metadata(m.instrument_id, meta(m.instrument_id), NS)
        state.update_book(m.instrument_id, replace(book(m.venue, "99", "100"), symbol=m.symbol), NS)
    comparisons = []
    original = runtime.evaluate_opportunity
    def observe(buy, buy_meta, sell, sell_meta, settings, now):
        comparisons.append((buy.symbol, sell.symbol))
        return original(buy, buy_meta, sell, sell_meta, settings, now)
    monkeypatch.setattr(runtime, "evaluate_opportunity", observe)
    state.update_book(p.markets[0].instrument_id, book("ONDO", "99", "100"), NS)
    assert comparisons == [("BTC", "BTC"), ("BTC", "BTC")]
    state.expire(NS)
    assert ("ETH", "ETH") in comparisons


def test_backpack_epoch_change_discards_old_book_until_new_snapshot(tmp_path, monkeypatch):
    _, state, observer, ids, _, health = native_observer(tmp_path, monkeypatch, backpack=True)
    for i, b, a in [(ids[0], "99.00", "100.00"), (ids[1], "102.00", "103.00")]:
        observer.on_book_deltas(batch(i, b, a))
    assert state.saved == 1
    health["connection_epoch"] = 2
    observer.on_time_event(None)
    assert not state.current and str(ids[1]) not in state.books
    observer.on_book_deltas(batch(ids[1], "102.00", "103.00", snapshot=False))
    assert state.saved == 1
    observer.on_book_deltas(batch(ids[1], "102.00", "103.00"))
    assert state.saved == 2


def test_recording_error_stops_native_observer(tmp_path, monkeypatch):
    p, state, observer, ids, calls, _ = native_observer(tmp_path, monkeypatch)
    original_open = Path.open
    def failure(path, *args, **kwargs):
        if path == p.output_path:
            raise OSError("synthetic disk failure")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", failure)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    assert observer.failure is None
    observer.on_book_deltas(batch(ids[1], "102.00", "103.00"))
    assert "stop" in calls and observer.failure and not observer.running
    assert state.current == {}


def test_actual_public_factories_and_node_build_without_execution_or_output(tmp_path):
    pytest.importorskip("nautilus_trader.adapters.backpack")
    p = scan.load_plan(ROOT / "config/opportunity-scan.example.toml")
    p = replace(p, output_path=tmp_path / "events" / "opportunities.jsonl")
    clients = runtime.data_clients(p)
    assert len(clients) == 5
    assert {cid for cid, _, _ in clients} == {"HYPERLIQUID", "LIGHTER", "ASTER", "ONDO", "BACKPACK"}
    for cid, _, config in clients:
        if cid == "ONDO":
            assert config.raw_md_path is None
        if cid == "ASTER":
            assert len(config.instrument_provider.load_ids) == 2
    node, observer, state = runtime.build_node(p)
    assert observer.running is False and not state.books
    assert list(tmp_path.iterdir()) == []

"""Book invalidation diagnostics cannot promote historical or partial liquidity."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import json
from types import SimpleNamespace

import pytest

from test_opportunity_scan import NS, batch, native_observer
from test_opportunity_trade import NOW, publish
from test_opportunity_live import state, submissions
from test_opportunity_native_execution import harness, intent, event
from opportunity_entry_evidence import entry_market_snapshot, entry_dispatch_evidence
import opportunity_timing as timing


def enable(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    return recorder


def live_book(monkeypatch):
    recorder = enable(monkeypatch)
    live, _ = state()
    publish(live)
    buy, _ = submissions(live)
    return recorder, live, buy


@pytest.mark.parametrize("reason", ["snapshot_rebuilding", "partial_update", "disconnected",
                                   "metadata_invalid", "halted", "stopped"])
def test_last_complete_preserves_first_invalidation_and_cannot_pass_admission(monkeypatch, reason):
    _, live, buy = live_book(monkeypatch)
    snapshot = live.books[buy.instrument_id]
    context = live.book_evidence[buy.instrument_id]
    assert context.last_complete is snapshot
    published_ns = context.published_ns
    live.invalidate(buy.instrument_id, NOW + 10, reason=reason)
    live.invalidate(buy.instrument_id, NOW + 20, reason=reason)
    before = live.snapshot()
    result = entry_market_snapshot(live, buy, NOW + 30)
    assert result["status"] == reason
    assert "depth_within_limit" not in result
    previous = result["last_complete"]
    assert previous["evidence_state"] == "historical_invalid"
    assert previous["diagnostic_only"] is True
    assert previous["invalidation_reason"] == reason
    assert previous["invalidated_wall_ns"] == NOW + 10
    assert previous["published_wall_ns"] == published_ns
    assert previous["source_wall_ns"] == snapshot.ts_event_ns
    assert previous["received_wall_ns"] == snapshot.ts_received_ns
    assert previous["full_quantity_available"] is True
    assert previous["complete_book_claimed"] is False
    position = live._position(live.orders[buy.client_order_id]["position_id"])
    assert entry_dispatch_evidence(live, position, NOW + 30)[0] == "missing_book"
    assert live.snapshot() == before


def test_stale_and_empty_side_keep_distinct_evidence(monkeypatch):
    _, live, buy = live_book(monkeypatch)
    original = live.books[buy.instrument_id]
    expired = NOW + 10_000_000_000
    result = entry_market_snapshot(live, buy, expired)
    assert result["status"] == "stale"
    assert result["evidence_state"] == "historical_invalid"
    previous = result["last_complete"]
    assert previous["invalidation_reason"] == "stale"
    assert previous["invalidation_time_basis"] == "freshness_deadline"
    assert previous["invalidated_wall_ns"] == min(
        original.ts_event_ns + live.plan.settings.max_age_ms * 1_000_000,
        original.ts_received_ns + live.plan.settings.max_receive_age_ms * 1_000_000) + 1
    live.update_book(buy.instrument_id, replace(original, asks=()), NOW + 5, evaluate=False)
    result = entry_market_snapshot(live, buy, NOW + 10)
    assert result["status"] == "empty_side" and result["ask_levels"] == 0
    assert result["published_wall_ns"] == NOW + 5
    assert result["last_complete"]["ask_levels"] == len(original.asks)
    assert result["last_complete"]["invalidation_reason"] == "empty_side"
    assert live.book_evidence[buy.instrument_id].last_complete is original


def test_history_is_one_reference_per_market_and_replaced_on_publication(monkeypatch):
    _, live, buy = live_book(monkeypatch)
    original = live.books[buy.instrument_id]
    context = live.book_evidence[buy.instrument_id]
    for n in range(2000):
        live.invalidate(buy.instrument_id, NOW + n, reason="snapshot_rebuilding")
        snapshot = replace(original, ts_event_ns=NOW + n, ts_received_ns=NOW + n)
        live.update_book(buy.instrument_id, snapshot, NOW + n + 1, evaluate=False)
        assert context.last_complete is snapshot
        assert context.invalidated_ns is None and context.pending is None
    assert len(live.book_evidence) == len(live.markets)
    result = entry_market_snapshot(live, buy, NOW + 2001)
    assert result["status"] == "valid" and "last_complete" not in result


def test_disabled_retains_no_history_or_pending_metadata(monkeypatch, tmp_path):
    monkeypatch.setattr(timing, "_active", None)
    live, _ = state()
    publish(live)
    assert live.book_evidence is None
    _, scan, observer, ids, _, _ = native_observer(tmp_path, monkeypatch, evaluation_interval_ms=100)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    assert scan.book_evidence is None
    assert ids[0] in observer.dirty


def observer_book(tmp_path, monkeypatch, **kwargs):
    enable(monkeypatch)
    _, scan, observer, ids, _, health = native_observer(
        tmp_path, monkeypatch, evaluation_interval_ms=100, **kwargs)
    order = SimpleNamespace(instrument_id=str(ids[0]), side="BUY", price="100", quantity="1")
    clock = [NS]
    monkeypatch.setattr(timing.time, "time_ns", lambda: clock[0])
    return scan, observer, ids, order, clock, health


def test_clear_fragmented_snapshot_and_delayed_publish_callback_interleaving(tmp_path, monkeypatch):
    native = pytest.importorskip("nautilus_trader.model")
    scan, observer, ids, order, clock, _ = observer_book(tmp_path, monkeypatch)
    assert entry_market_snapshot(scan, order, clock[0])["status"] == "uninitialized"
    initial = batch(ids[0], "99.00", "100.00")
    observer.on_book_deltas(initial)
    pending = entry_market_snapshot(scan, order, clock[0])
    assert pending["status"] == "pending_publish" and "last_complete" not in pending
    assert pending["local_pending"]["evidence_state"] == "complete_unpublished_metadata_only"
    assert pending["local_pending"]["published_wall_ns"] is None
    assert "depth_within_limit" not in pending["local_pending"]
    assert not scan.books
    clock[0] += 100_000_000
    observer.on_time_event(None)
    old_book = scan.books[order.instrument_id]
    assert entry_market_snapshot(scan, order, clock[0])["status"] == "valid"

    clock[0] += 1
    rebuild = batch(ids[0], "100.00", "101.00")
    # CLEAR alone, bid-only snapshot, then F_LAST: callbacks see all boundaries.
    observer.on_book_deltas(native.OrderBookDeltas(ids[0], rebuild.deltas[:1]))
    clear = entry_market_snapshot(scan, order, clock[0])
    assert clear["status"] == "snapshot_rebuilding"
    assert clear["last_complete"]["invalidated_wall_ns"] == clock[0]
    observer.on_book_deltas(native.OrderBookDeltas(ids[0], rebuild.deltas[1:2]))
    partial = entry_market_snapshot(scan, order, clock[0])
    assert partial == clear and "local_pending" not in partial
    assert scan.book_evidence[order.instrument_id].last_complete is old_book
    clock[0] += 1
    observer.on_book_deltas(native.OrderBookDeltas(ids[0], rebuild.deltas[2:]))
    complete = entry_market_snapshot(scan, order, clock[0])
    assert complete["status"] == "pending_publish"
    assert complete["last_complete"] == clear["last_complete"] | {
        "source_age_ms": complete["last_complete"]["source_age_ms"],
        "receive_age_ms": complete["last_complete"]["receive_age_ms"]}
    assert complete["local_pending"]["completed_wall_ns"] == clock[0]
    assert not scan.books
    clock[0] += 100_000_000
    observer.on_time_event(None)
    current = entry_market_snapshot(scan, order, clock[0])
    assert current["status"] == "valid" and not current["full_quantity_available"]
    assert "last_complete" not in current and "local_pending" not in current
    assert current["published_wall_ns"] == clock[0]
    assert D(clear["last_complete"]["depth_within_limit"]) == 10
    assert observer.failure is None


def test_partial_increment_drops_pending_metadata_and_keeps_complete_history(tmp_path, monkeypatch):
    native = pytest.importorskip("nautilus_trader.model")
    scan, observer, ids, order, clock, _ = observer_book(tmp_path, monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    clock[0] += 100_000_000
    observer.on_time_event(None)
    update = batch(ids[0], "99.00", "101.00", snapshot=False)
    observer.on_book_deltas(update)
    before = entry_market_snapshot(scan, order, clock[0])
    assert before["status"] == "valid" and "local_pending" in before
    observer.on_book_deltas(native.OrderBookDeltas(ids[0], update.deltas[:1]))
    result = entry_market_snapshot(scan, order, clock[0])
    assert result["status"] == "partial_update" and "local_pending" not in result
    assert result["last_complete"]["invalidation_reason"] == "partial_update"
    assert order.instrument_id not in scan.books


def test_complete_empty_local_side_is_distinct_from_partial_snapshot(tmp_path, monkeypatch):
    native = pytest.importorskip("nautilus_trader.model")
    scan, observer, ids, order, clock, _ = observer_book(tmp_path, monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    clock[0] += 100_000_000
    observer.on_time_event(None)
    clear = native.OrderBookDelta.clear(ids[0], 2, NS, NS)
    complete_clear = native.OrderBookDelta(ids[0], clear.action, clear.order,
        native.RecordFlag.F_LAST.value, 2, NS, NS)
    observer.on_book_deltas(native.OrderBookDeltas(ids[0], [complete_clear]))
    pending = entry_market_snapshot(scan, order, clock[0])
    assert pending["status"] == "empty_side"
    assert pending["local_pending"]["ask_levels"] == 0
    assert pending["last_complete"]["ask_levels"] == 1
    clock[0] += 100_000_000
    observer.on_time_event(None)
    result = entry_market_snapshot(scan, order, clock[0])
    assert result["status"] == "empty_side" and "local_pending" not in result
    assert "depth_within_limit" not in result
    assert result["last_complete"]["full_quantity_available"]


@pytest.mark.parametrize("native_reason, expected", [
    ("adapter:disconnected", "disconnected"), ("adapter:metadata_stale", "metadata_invalid"),
])
def test_native_invalidation_reasons_do_not_infer_empty_exchange_book(tmp_path, monkeypatch, native_reason, expected):
    scan, observer, ids, order, clock, _ = observer_book(tmp_path, monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    clock[0] += 100_000_000
    observer.on_time_event(None)
    observer.on_instrument_status(SimpleNamespace(instrument_id=ids[0], reason=native_reason))
    result = entry_market_snapshot(scan, order, clock[0])
    assert result["status"] == expected
    assert result["last_complete"]["invalidation_reason"] == expected
    assert "ask_levels" not in result


def test_disconnected_snapshot_stays_invalid_until_ready(tmp_path, monkeypatch):
    scan, observer, ids, order, clock, _ = observer_book(tmp_path, monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    clock[0] += 100_000_000
    observer.on_time_event(None)
    original = scan.books[order.instrument_id]
    observer.on_instrument_status(SimpleNamespace(instrument_id=ids[0], reason="adapter:disconnected"))
    observer.on_book_deltas(batch(ids[0], "100.00", "101.00"))
    result = entry_market_snapshot(scan, order, clock[0])
    assert result["status"] == "disconnected" and "local_pending" in result
    assert result["last_complete"]["invalidation_reason"] == "disconnected"
    clock[0] += 100_000_000
    observer.on_time_event(None)
    assert entry_market_snapshot(scan, order, clock[0])["status"] == "disconnected"
    assert scan.book_evidence[order.instrument_id].last_complete is original
    observer.on_instrument_status(SimpleNamespace(instrument_id=ids[0], reason="adapter:snapshot_ready"))
    observer.on_book_deltas(batch(ids[0], "100.00", "101.00"))
    clock[0] += 100_000_000
    observer.on_time_event(None)
    assert entry_market_snapshot(scan, order, clock[0])["status"] == "valid"


def test_metadata_change_snapshot_without_clear_and_stop(tmp_path, monkeypatch):
    native = pytest.importorskip("nautilus_trader.model")
    scan, observer, ids, order, clock, _ = observer_book(tmp_path, monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    clock[0] += 100_000_000
    observer.on_time_event(None)
    changed = SimpleNamespace(**vars(observer.cache.instrument(ids[0])))
    changed.size_increment = "0.02"
    observer.on_instrument(changed)
    assert entry_market_snapshot(scan, order, clock[0])["status"] == "metadata_invalid"
    rebuild = batch(ids[0], "100.00", "101.00")
    observer.on_book_deltas(native.OrderBookDeltas(ids[0], rebuild.deltas[1:2]))
    result = entry_market_snapshot(scan, order, clock[0])
    assert result["status"] == "snapshot_rebuilding" and "local_pending" not in result
    assert result["last_complete"]["invalidation_reason"] == "metadata_invalid"
    observer.on_book_deltas(native.OrderBookDeltas(ids[0], rebuild.deltas[2:]))
    observer.on_stop()
    result = entry_market_snapshot(scan, order, clock[0])
    assert result["status"] == "stopped" and "local_pending" not in result
    assert not scan.books and not observer.dirty


@pytest.mark.parametrize("enabled", [False, True])
def test_complete_pending_never_materializes_a_ladder(tmp_path, monkeypatch, enabled):
    scan, observer, ids, order, clock, _ = observer_book(tmp_path, monkeypatch)
    if not enabled:
        scan.book_evidence = None
    original = observer.local_books[ids[0]]
    class NoLadderReads:
        def reset(self):
            return original.reset()

        def apply_deltas(self, value):
            return original.apply_deltas(value)

        def bids_to_dict(self, *args):
            pytest.fail("pending diagnostics must not copy a bid ladder")

        def asks_to_dict(self, *args):
            pytest.fail("pending diagnostics must not copy an ask ladder")

    observer.local_books[ids[0]] = NoLadderReads()
    for _ in range(50):
        observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    result = entry_market_snapshot(scan, order, clock[0])
    assert observer.failure is None and not scan.books
    if enabled:
        assert result["status"] == "pending_publish"
        assert len(scan.book_evidence) == len(scan.markets)


@pytest.mark.parametrize("key, value, expected", [
    ("connected", False, "disconnected"), ("metadata_ready", False, "metadata_invalid"),
    ("books_continuous", {"BTC_USDC_PERP": False}, "book_discontinuous"),
    ("books_fresh", {"BTC_USDC_PERP": False}, "stale"),
    ("connection_epoch", 2, "connection_changed"),
])
def test_backpack_health_reason_is_fixed_and_preserves_history(tmp_path, monkeypatch, key, value, expected):
    scan, observer, ids, order, clock, health = observer_book(tmp_path, monkeypatch, backpack=True)
    order.instrument_id = str(ids[1])
    observer.check_backpack()
    observer.on_book_deltas(batch(ids[1], "99.00", "100.00"))
    clock[0] += 100_000_000
    observer.on_time_event(None)
    health[key] = value
    observer.check_backpack()
    result = entry_market_snapshot(scan, order, clock[0])
    assert result["status"] == expected
    assert result["last_complete"]["invalidation_reason"] == expected


def test_recording_interleaved_rejection_keeps_orders_accounts_and_payload_private(harness, monkeypatch):
    gateway, strategy, receipts, accounts, fills = harness
    recorder, live, buy = live_book(monkeypatch)
    order = intent()
    timing.begin_entry_trace(order.request_id, order.venue, timing.start_latency(), NOW)
    gateway.entry_evidence_provider = lambda _, now: entry_market_snapshot(live, buy, now)
    gateway.dispatch(order, NOW)
    gateway.on_native_event("submitted", event(ts_init=NOW), NOW + 1)
    live.invalidate(buy.instrument_id, NOW + 2, reason="snapshot_rebuilding")
    live_before, calls_before = live.snapshot(), deepcopy(strategy.calls)
    strategy.cached[order.client_order_id].is_closed = True
    gateway.on_native_event("rejected", event(reason="IOC could not match private-native-payload",
                                             ts_init=NOW + 3), NOW + 4)
    rows = recorder.document()["entry_execution"]["traces"]
    rows = next(t["native_events"] for t in rows if t["native_events"])
    assert rows[0]["market"]["status"] == "valid"
    assert rows[1]["market"]["status"] == "snapshot_rebuilding"
    assert rows[1]["market"]["last_complete"]["invalidated_wall_ns"] == NOW + 2
    assert live.snapshot() == live_before and strategy.calls == calls_before
    assert len(receipts) == 1 and receipts[0].kind == "rejected"
    assert not accounts and not fills
    assert "private-native-payload" not in json.dumps(recorder.document())


def test_nested_diagnostics_export_only_fixed_fields(monkeypatch):
    recorder = enable(monkeypatch)
    timing.begin_entry_trace("request", "HL", timing.start_latency(), NOW)
    market = {"status": "pending_publish", "native_payload": "do-not-export",
              "last_complete": {"invalidation_reason": "snapshot_rebuilding", "native_payload": "do-not-export"},
              "local_pending": {"sequence": 5, "native_payload": "do-not-export"}}
    timing.record_entry_native_event("request", "rejected", NOW, market_provider=lambda: market)
    report = recorder.document()
    row = report["entry_execution"]["traces"][0]["native_events"][0]["market"]
    assert row["last_complete"] == {"invalidation_reason": "snapshot_rebuilding"}
    assert row["local_pending"] == {"sequence": 5}
    assert "do-not-export" not in json.dumps(report)

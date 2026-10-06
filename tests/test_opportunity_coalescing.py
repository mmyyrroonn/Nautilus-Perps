"""Regressions for current-state publication, native batch boundaries and pacing."""
from dataclasses import replace
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from test_opportunity_scan import NS, batch, native_observer, plan, runtime


def moving_clock(monkeypatch):
    now = [NS]
    monkeypatch.setattr(runtime.time, "time_ns", lambda: now[0])
    return now


def replace_touch(instrument_id, old_bid, old_ask, bid, ask, stamp, sequence=2):
    """Remove the old prices before inserting the new native L2 touch."""
    native = pytest.importorskip("nautilus_trader.model")
    changes = []
    for side, old, new in [(native.OrderSide.BUY, old_bid, bid),
                           (native.OrderSide.SELL, old_ask, ask)]:
        for action, price, size in [(native.BookAction.DELETE, old, "0.00"),
                                   (native.BookAction.UPDATE, new, "10.00")]:
            changes.append(native.OrderBookDelta(instrument_id, action,
                native.BookOrder(side, native.Price.from_str(price),
                                 native.Quantity.from_str(size), 0),
                0, sequence, stamp, stamp))
    last = changes.pop()
    changes.append(native.OrderBookDelta(instrument_id, last.action, last.order,
        native.RecordFlag.F_LAST.value, sequence, stamp, stamp))
    return native.OrderBookDeltas(instrument_id, changes)


def test_dirty_legs_publish_together_without_mixed_old_new_event(tmp_path, monkeypatch):
    p, state, observer, ids, _, _ = native_observer(
        tmp_path, monkeypatch, evaluation_interval_ms=100)
    now = moving_clock(monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    observer.on_book_deltas(batch(ids[1], "100.00", "101.00"))
    now[0] = NS + 100_000_000
    observer.on_time_event(None)
    assert state.saved == 0 and len(state.books) == 2

    now[0] = NS + 150_000_000
    observer.on_book_deltas(replace_touch(ids[0], "99.00", "100.00",
        "79.00", "90.00", now[0]))
    observer.on_book_deltas(replace_touch(ids[1], "100.00", "101.00",
        "80.00", "91.00", now[0]))
    assert len(observer.dirty) == 2
    now[0] = NS + 200_000_000
    observer.on_time_event(None)
    assert observer.failure is None
    assert state.saved == 0 and not state.current
    assert not p.output_path.exists()
    assert state.books[str(ids[0])].asks[0][0] == Decimal("90")
    assert state.books[str(ids[1])].bids[0][0] == Decimal("80")

    now[0] = NS + 250_000_000
    observer.on_book_deltas(replace_touch(ids[1], "80.00", "91.00",
        "95.00", "96.00", now[0], sequence=3))
    now[0] = NS + 300_000_000
    observer.on_time_event(None)
    assert state.saved == 1
    opportunity = json.loads(p.output_path.read_text())["opportunity"]
    assert opportunity["raw_buy_book"]["asks"] == [["90.00", "10"]]
    assert opportunity["raw_sell_book"]["bids"] == [["95.00", "10"]]
    assert opportunity["raw_buy_book"]["ts_received_ns"] == NS + 150_000_000
    assert opportunity["raw_sell_book"]["ts_received_ns"] == NS + 250_000_000
    assert opportunity["raw_sell_book"]["sequence"] == 3


def test_evaluation_deadline_survives_faster_subscription_timer(tmp_path, monkeypatch):
    _, state, observer, ids, _, _ = native_observer(tmp_path, monkeypatch,
        evaluation_interval_ms=1000, aster_subscription_interval_ms=100, refresh_ms=5000)
    now = moving_clock(monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    observer.on_book_deltas(batch(ids[1], "102.00", "103.00"))
    for elapsed_ms in (100, 500, 999):
        now[0] = NS + elapsed_ms * 1_000_000
        observer.on_time_event(None)
        assert not state.books and state.saved == 0
        assert len(observer.dirty) == 2
    now[0] = NS + 1_000_000_000
    observer.on_time_event(None)
    assert len(state.books) == 2 and state.saved == 1 and not observer.dirty


def test_partial_increment_discards_dirty_complete_batch_until_last(tmp_path, monkeypatch):
    native = pytest.importorskip("nautilus_trader.model")
    _, state, observer, ids, _, _ = native_observer(
        tmp_path, monkeypatch, evaluation_interval_ms=100)
    now = moving_clock(monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    observer.on_book_deltas(batch(ids[1], "102.00", "103.00"))
    now[0] = NS + 100_000_000
    observer.on_time_event(None)
    assert state.saved == 1

    now[0] = NS + 150_000_000
    complete = replace_touch(ids[0], "99.00", "100.00",
        "98.00", "99.00", now[0])
    observer.on_book_deltas(complete)
    assert ids[0] in observer.dirty
    pending = replace_touch(ids[0], "98.00", "99.00",
        "97.00", "98.00", now[0], sequence=3)
    observer.on_book_deltas(native.OrderBookDeltas(ids[0], pending.deltas[:-1]))
    assert ids[0] not in observer.dirty and str(ids[0]) not in state.books
    now[0] = NS + 200_000_000
    observer.on_time_event(None)
    assert str(ids[0]) not in state.books and not state.current and state.saved == 1
    now[0] = NS + 250_000_000
    observer.on_book_deltas(native.OrderBookDeltas(ids[0], pending.deltas[-1:]))
    now[0] = NS + 300_000_000
    observer.on_time_event(None)
    assert observer.failure is None
    assert state.books[str(ids[0])].asks == ((Decimal("98"), Decimal("10")),)
    assert state.raw_books[str(ids[0])]["sequence"] == 3
    assert state.saved == 2


def test_metadata_and_status_callbacks_do_not_evaluate_dirty_old_book(tmp_path, monkeypatch):
    _, state, observer, ids, _, _ = native_observer(
        tmp_path, monkeypatch, evaluation_interval_ms=100)
    now = moving_clock(monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    observer.on_book_deltas(batch(ids[1], "100.00", "101.00"))
    now[0] = NS + 100_000_000
    observer.on_time_event(None)
    now[0] = NS + 150_000_000
    observer.on_book_deltas(replace_touch(ids[0], "99.00", "100.00",
        "79.00", "90.00", now[0]))
    observer.on_book_deltas(replace_touch(ids[1], "100.00", "101.00",
        "80.00", "91.00", now[0]))
    observer.on_instrument(observer.cache.instrument(ids[0]))
    observer.on_instrument_status(SimpleNamespace(instrument_id=ids[0],
        reason="venue resume", is_trading=True))
    assert state.saved == 0
    assert observer.pending_symbols == {"BTC"}
    now[0] = NS + 200_000_000
    observer.on_time_event(None)
    assert state.saved == 0 and not state.current and not observer.pending_symbols


def test_display_expiry_does_not_bypass_pending_evaluation_deadline(tmp_path, monkeypatch):
    _, state, observer, ids, _, _ = native_observer(tmp_path, monkeypatch,
        evaluation_interval_ms=1500, refresh_ms=1000)
    now = moving_clock(monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    observer.on_book_deltas(batch(ids[1], "100.00", "101.00"))
    now[0] = NS + 1_500_000_000
    observer.on_time_event(None)
    comparisons = []
    original = state.evaluate_symbol
    def track(*args, **kwargs):
        comparisons.append((args, kwargs))
        return original(*args, **kwargs)
    monkeypatch.setattr(state, "evaluate_symbol", track)
    now[0] = NS + 2_000_000_000
    observer.on_book_deltas(replace_touch(ids[0], "99.00", "100.00",
        "79.00", "90.00", now[0]))
    observer.on_book_deltas(replace_touch(ids[1], "100.00", "101.00",
        "80.00", "91.00", now[0]))
    now[0] = NS + 2_500_000_000
    observer.on_time_event(None)
    assert not comparisons, "display tick must not evaluate dirty previous snapshots"
    assert len(observer.dirty) == 2
    now[0] = NS + 3_000_000_000
    observer.on_time_event(None)
    assert comparisons and not observer.dirty and state.saved == 0


def test_multi_market_aster_pacing_and_stop_cancel_pending_work(tmp_path, monkeypatch):
    base = plan(tmp_path)
    eth = tuple(replace(m, symbol="ETH", instrument_id=m.instrument_id.replace("BTC", "ETH"))
                for m in base.markets)
    _, _, observer, _, calls, _ = native_observer(tmp_path, monkeypatch,
        markets=base.markets + eth, evaluation_interval_ms=100,
        aster_snapshot_depth=100, aster_subscription_interval_ms=500)
    now = moving_clock(monkeypatch)
    assert calls.count("deltas") == 3
    assert len(observer.pending_aster) == 1
    seen = []
    def capture_subscription(instrument_id, book_type, **kwargs):
        seen.append((str(instrument_id), kwargs))
    monkeypatch.setattr(observer, "subscribe_book_deltas", capture_subscription)
    now[0] = NS + 499_000_000
    observer.on_time_event(None)
    assert not seen and len(observer.pending_aster) == 1
    now[0] = NS + 500_000_000
    observer.on_time_event(None)
    assert seen == [("ETHUSDT-PERP.ASTER", {
        "depth": 100, "client_id": pytest.importorskip("nautilus_trader.model").ClientId.from_str("ASTER"),
        "managed": False})]
    assert not observer.pending_aster and len(observer.subscribed) == 4
    observer.pending_aster.append(next(i for i in observer.markets if str(i).endswith(".ASTER")))
    observer.on_stop()
    now[0] = NS + 1_000_000_000
    observer.on_time_event(None)
    assert len(seen) == 1 and not observer.pending_aster and not observer.dirty
    assert "cancel_timer" in calls


def test_stale_hidden_direction_resets_onset_before_fresh_same_edge(tmp_path, monkeypatch):
    _, state, observer, ids, _, _ = native_observer(tmp_path, monkeypatch,
        evaluation_interval_ms=1500, refresh_ms=1000)
    now = moving_clock(monkeypatch)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    observer.on_book_deltas(batch(ids[1], "102.00", "103.00"))
    now[0] = NS + 1_500_000_000
    observer.on_time_event(None)
    assert state.saved == 1 and state.current

    now[0] = NS + 2_400_000_000
    observer.on_book_deltas(replace_touch(ids[0], "99.00", "100.00",
        "99.00", "100.00", now[0]))
    now[0] = NS + 2_500_000_000
    observer.on_time_event(None)
    assert not state.current  # Hidden dirty symbol has an actually stale other leg.
    now[0] = NS + 2_900_000_000
    observer.on_book_deltas(replace_touch(ids[1], "102.00", "103.00",
        "102.00", "103.00", now[0]))
    now[0] = NS + 3_000_000_000
    observer.on_time_event(None)
    assert state.current
    assert state.saved == 2, "staleness ended the old onset despite the hidden display row"

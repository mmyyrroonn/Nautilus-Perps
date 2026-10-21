"""Native event diagnostics remain bounded and never change trading behavior."""
from dataclasses import replace
from decimal import Decimal as D
import json

import pytest

from test_opportunity_native_execution import harness, intent, event
from test_opportunity_live import state, submissions
from test_opportunity_trade import NOW, publish, set_book
from opportunity_entry_evidence import entry_market_snapshot
import opportunity_timing as timing


def traced(monkeypatch, order):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    timing.begin_entry_trace(order.request_id, order.venue, timing.start_latency(), NOW)
    return recorder


def test_submitted_is_observational_and_callback_book_explains_unmatched_limit(harness, monkeypatch):
    gateway, strategy, receipts, accounts, fills = harness
    order = intent()
    recorder = traced(monkeypatch, order)
    live, _ = state()
    publish(live)
    buy, _ = submissions(live)
    gateway.entry_evidence_provider = lambda _, now: entry_market_snapshot(live, buy, now)
    gateway.dispatch(order, NOW)
    calls = list(strategy.calls)
    gateway.on_native_event("submitted", event(ts_init=NOW + 10, ts_event=NOW + 10), NOW + 20)
    first = recorder.document()["entry_execution"]["traces"][0]["native_events"][0]
    assert first["native_init_to_python_ms"] == 10 / 1e6
    assert first["market"]["full_quantity_available"]
    set_book(live, "HL", "100.1", "100.2", NOW + 30)
    strategy.cached[order.client_order_id].is_closed = True
    gateway.on_native_event("rejected", event(reason="IOC could not match",
                            ts_init=NOW + 40, ts_event=NOW + 40), NOW + 80)
    trace = recorder.document()["entry_execution"]["traces"][0]
    rejected = trace["native_events"][-1]
    assert rejected["kind"] == "rejected" and rejected["native_init_to_python_ms"] == 40 / 1e6
    assert not rejected["market"]["full_quantity_available"]
    assert D(rejected["market"]["depth_within_limit"]) == 0
    assert D(rejected["market"]["touch_outside_limit_bps"]) > 0
    assert strategy.calls == calls
    assert len(receipts) == 1 and receipts[0].kind == "rejected"
    assert receipts[0].reason_code == "ioc_no_match"
    assert not fills and not accounts
    assert strategy.cached[order.client_order_id].price == "100"


@pytest.mark.parametrize("stamp", [None, 0, -1, True, NOW + 100])
def test_invalid_native_timestamp_is_not_zero_delivery_latency(monkeypatch, stamp):
    order = intent()
    recorder = traced(monkeypatch, order)
    timing.record_entry_native_event(order.request_id, "submitted", NOW, ts_init=stamp)
    row = recorder.document()["entry_execution"]["traces"][0]["native_events"][0]
    assert row["local_timestamp_status"] == "missing_or_invalid"
    assert "native_init_to_python_ms" not in row


def test_disabled_or_untraced_diagnostics_do_not_read_market(monkeypatch):
    def forbidden():
        raise AssertionError("market should not be read")
    monkeypatch.setattr(timing, "_active", None)
    timing.record_entry_native_event("unused", "submitted", NOW, market_provider=forbidden)
    recorder = traced(monkeypatch, intent())
    timing.record_entry_native_event("exit-not-traced", "submitted", NOW, market_provider=forbidden)
    assert recorder.document()["entry_execution"]["traces"][0]["native_events"] == []


def test_observation_failure_does_not_change_receipts_or_leak_error(harness, monkeypatch):
    gateway, strategy, receipts, _, _ = harness
    order = intent()
    recorder = traced(monkeypatch, order)
    def fail(*args):
        raise ValueError("sensitive-response-do-not-export")
    gateway.entry_evidence_provider = fail
    gateway.dispatch(order, NOW)
    gateway.on_native_event("submitted", event(ts_init=NOW), NOW + 1)
    assert not receipts and len(strategy.calls) == 1
    gateway.on_native_event("accepted", event(ts_init=NOW + 1), NOW + 2)
    assert len(receipts) == 1 and receipts[0].kind == "accepted"
    report = recorder.document()
    assert "sensitive-response" not in json.dumps(report)
    assert report["entry_execution"]["traces"][0]["native_events"][0]["market"] == {
        "status": "observation_failed"}


def test_high_volume_native_callbacks_are_bounded_and_preserve_original_trace(monkeypatch):
    order = intent()
    recorder = traced(monkeypatch, order)
    for n in range(5000):
        timing.record_entry_native_event(order.request_id, "filled", NOW + n, ts_init=NOW + n)
    trace = recorder.document()["entry_execution"]["traces"][0]
    assert trace["native_events_seen"] == 5000
    assert len(trace["native_events"]) == 16
    assert trace["stages"][0]["stage"] == "candidate"
    assert trace["native_events"][-1]["native_init_wall_ns"] == NOW + 4999


def test_market_observation_preserves_stale_evidence_and_native_price_conversion():
    live, _ = state()
    publish(live)
    buy, _ = submissions(live)
    original = live.snapshot()
    market = live.markets[buy.instrument_id]
    live.markets[buy.instrument_id] = replace(market, canonical_multiplier=D(10), quote_to_usd=D(2))
    observed = entry_market_snapshot(live, buy, NOW + 10_000_000_000)
    assert observed["status"] == "stale"
    assert D(observed["ask_price"]) == 500
    assert not observed["full_quantity_available"]
    assert live.snapshot() == original

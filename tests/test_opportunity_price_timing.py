"""Correlate quote versions and stage clocks without changing execution decisions."""
from copy import deepcopy
import importlib.util
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opportunity_timing as timing
from opportunity_price_timing import entry_quotes, pending_stage
from test_opportunity_native_execution import harness, intent
from test_opportunity_trade import NOW, publish
from test_opportunity_live import MemoryStore, state


def enable(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    return recorder


def test_pricing_persistence_and_wakeup_have_distinct_endpoints(monkeypatch):
    recorder = enable(monkeypatch)
    store = MemoryStore()
    live, _ = state(store=store)
    tick = [100]
    monkeypatch.setattr(timing.time, "perf_counter_ns", lambda: tick[0])
    pricing = live.pricer.entry
    def price(*args):
        result = pricing(*args)
        tick[0] += 10
        return result
    monkeypatch.setattr(live.pricer, "entry", price)
    save = store.save
    def durable(*args):
        tick[0] += 20
        save(*args)
    monkeypatch.setattr(store, "save", durable)
    def wake():
        assert len(store.saved["orders"]) == 2
        tick[0] += 7
    live.intent_notifier = wake
    publish(live)
    for trace in recorder.document()["entry_execution"]["traces"]:
        stages = {s["stage"]: s["monotonic_ns"] for s in trace["stages"]}
        assert stages["candidate"] - stages["pricing_started"] == 10
        assert stages["persisted"] - stages["persist_started"] == 20
        assert stages["wakeup_request_returned"] - stages["wakeup_requested"] == 7
        assert stages["persisted"] <= stages["wakeup_requested"]
        assert set(trace["quotes"]) == {"buy", "sell"}
    before = deepcopy(live.snapshot())
    pending_stage(live, "tick_started")
    pending_stage(live, "gateway_poll_completed")
    assert live.snapshot() == before
    assert len(live.outbox) == 2


def test_submit_wall_time_is_the_call_observation_not_callback_start(harness, monkeypatch):
    gateway, strategy, _, _, _ = harness
    recorder = enable(monkeypatch)
    order = intent()
    timing.begin_entry_trace(order.request_id, "HL", timing.start_latency(), NOW,
                             client_order_id=order.client_order_id)
    monkeypatch.setattr(timing.time, "time_ns", lambda: NOW + 123456)
    gateway.dispatch(order, NOW)
    stages = recorder.document()["entry_execution"]["traces"][0]["stages"]
    submit = next(s for s in stages if s["stage"] == "python_submit")
    returned = next(s for s in stages if s["stage"] == "python_submit_returned")
    assert submit["wall_ns"] == NOW + 123456
    assert returned["monotonic_ns"] >= submit["monotonic_ns"]
    assert len(strategy.calls) == 1


def test_quote_timing_tracks_last_complete_version_and_clears_on_invalidation(tmp_path, monkeypatch):
    from test_opportunity_scan import NS as WALL, batch, native_observer
    from test_opportunity_coalescing import replace_touch
    enable(monkeypatch)
    tick = [10]
    monkeypatch.setattr(timing.time, "perf_counter_ns", lambda: tick[0])
    _, live, observer, ids, _, _ = native_observer(tmp_path, monkeypatch, evaluation_interval_ms=100)
    observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
    tick[0] = 40
    observer.on_book_deltas(replace_touch(ids[0], "99.00", "100.00", "98.00", "99.00", WALL + 1))
    tick[0] = 100
    observer.publish_book(ids[0], observer.dirty[ids[0]], evaluate=False)
    observed = entry_quotes(live, str(ids[0]), str(ids[0]))["buy"]
    assert observed["callback_ns"] == observed["complete_ns"] == 40
    assert observed["published_ns"] == 100
    assert observed["source_wall_ns"] == WALL + 1
    observer.invalidate(ids[0])
    assert not live.quote_timings and not observer.pending_quote_timings
    assert entry_quotes(live, str(ids[0]), str(ids[0])) == {}
    observer.on_stop()


def test_disabled_and_untraced_stage_never_reads_clocks(monkeypatch):
    recorder = timing.TimingRecorder()
    def forbidden():
        raise AssertionError("unexpected clock read")
    monkeypatch.setattr(timing.time, "perf_counter_ns", forbidden)
    monkeypatch.setattr(timing.time, "time_ns", forbidden)
    timing.record_entry_stage("unknown", "python_submit", capture_wall=True)
    monkeypatch.setattr(timing, "_active", recorder)
    timing.record_entry_stage("unknown", "python_submit", capture_wall=True)
    pending_stage(NS(entry_timings={}), "tick_started")


def analyzer():
    path = Path(__file__).resolve().parents[1] / "scripts/analyze_price_order_timing.py"
    spec = importlib.util.spec_from_file_location("price_analysis", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fixture():
    app = {"pid": 7, "run_id": "run", "clock_anchor": {
        "platform": "linux", "monotonic_implementation": "clock_gettime(CLOCK_MONOTONIC)",
        "boot_id": "boot", "perf_before_ns": 100, "perf_after_ns": 110, "monotonic_ns": 1000},
        "entry_execution": {"traces": [{"trace_id": "app", "client_order_id": "client",
            "venue": "HL", "stages": [{"stage": "python_submit", "monotonic_ns": 200}],
            "quotes": {"buy": {"callback_ns": 150}}}]}}
    anchor = {"platform": "linux", "clock": "CLOCK_MONOTONIC", "boot_id": "boot", "pid": 7,
        "monotonic_ns": 1000, "elapsed_before_ns": 10, "elapsed_after_ns": 20}
    records = [{"client_order_id": "client", "session_id": "native", "trace_id": 1,
                "stage": stage, "elapsed_ns": stamp, "dropped": 0}
               for stage, stamp in (("native_received", 130), ("socket_write_completed", 180))]
    return app, records, {"native": anchor}


def test_clock_join_exports_bounds_and_quote_to_socket_interval():
    app, records, anchors = fixture()
    result = analyzer().analyze(app, records, anchors)["entries"][0]
    assert result["native_join_status"] == "calibrated"
    assert result["python_submit_to_native_received_ms_bounds"] == [10 / 1e6, 30 / 1e6]
    assert result["quotes"]["pricing_buy"]["callback_to_socket_write_ms_bounds"] == [110 / 1e6, 130 / 1e6]


@pytest.mark.parametrize("key,value", [("pid", 8), ("boot_id", "other"), ("platform", "win32")])
def test_clock_join_refuses_another_process_boot_or_platform(key, value):
    app, records, anchors = fixture()
    anchors["native"][key] = value
    result = analyzer().analyze(app, records, anchors)["entries"][0]
    assert result["native_join_status"] == "clock_unverified"
    assert "python_submit_to_native_received_ms_bounds" not in result


def test_replayed_writes_and_ambiguous_attempts_are_not_fabricated_intervals():
    app, records, anchors = fixture()
    records.append(dict(records[-1], elapsed_ns=500))
    result = analyzer().analyze(app, records, anchors)["entries"][0]
    assert "python_submit_to_socket_write_completed_ms_bounds" not in result
    records.append(dict(records[0], trace_id=2))
    result = analyzer().analyze(app, records, anchors)["entries"][0]
    assert result["native_join_status"] == "ambiguous_attempts"

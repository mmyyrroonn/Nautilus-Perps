"""Checks for bounded diagnostics, sampling semantics, and failure isolation."""
from copy import deepcopy
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opportunity_memory as memory


def native_sample(elapsed=1_000_000_000, sampled=1_000_000_000, dispatched=10, queued=4):
    return {"elapsed_ns": elapsed, "queue_sampled_at_ns": sampled,
            **{channel: {"dispatched": dispatched, "queue_depth": queued,
                         "dispatch_busy_ns": 100, "last_dispatch_at_ns": elapsed}
               for channel in memory.CHANNELS}}


def test_rates_distinguish_queue_sample_time_from_dispatch_time():
    before = native_sample()
    after = native_sample(elapsed=3_000_000_000, sampled=2_000_000_000, dispatched=30, queued=9)
    rates = memory.runner_rates(before, after)["data_events"]
    assert rates == {"dispatch_per_second": 10, "queue_net_growth_per_second": 5,
                     "dispatch_busy_fraction": 0, "mean_dispatch_ms": 0}
    after["queue_sampled_at_ns"] = before["queue_sampled_at_ns"]
    assert "queue_net_growth_per_second" not in memory.runner_rates(before, after)["data_events"]
    after["data_events"]["dispatched"] = 0
    assert memory.runner_rates(before, after) == {}


def test_market_samples_are_bounded_and_unknown_labels_are_ignored(tmp_path, monkeypatch):
    monkeypatch.setattr(memory.time, "time_ns", lambda: 20_000_000)
    diagnostics = memory.MemoryDiagnostics(tmp_path)
    for _ in range(128 * (memory.LATENCY_SAMPLES + 1)):
        diagnostics.record_batch("HL", 3, 10_000_000)
    diagnostics.record_batch("unexpected-symbol-as-venue", 7, 0)
    result = diagnostics.sample()["market_batches"]
    assert set(result) == set(memory.VENUES)
    assert result["HL"]["batches"] == 128 * (memory.LATENCY_SAMPLES + 1)
    assert result["HL"]["price_updates"] == result["HL"]["batches"] * 3
    assert result["HL"]["delivery_age_sample_count"] == memory.LATENCY_SAMPLES
    assert result["HL"]["delivery_age_p95_ms"] == 10


def test_diagnostic_windows_correlate_throughput_and_processing_time(tmp_path, monkeypatch):
    import opportunity_timing as timing

    clock = [1_000_000_000]
    monkeypatch.setattr(memory.time, "monotonic_ns", lambda: clock[0])
    diagnostics = memory.MemoryDiagnostics(tmp_path)
    assert diagnostics.sample()["processing_timings"] is None
    assert timing.current_timing_snapshot() is None

    def measured():
        with timing.span("market.book_deltas", venue="HL"):
            diagnostics.record_batch("HL", 30, 100)
            diagnostics.record_batch("HL", 10, 200)
        clock[0] += 2_000_000_000
        document = diagnostics.sample()
        row = document["market_batches"]["HL"]
        assert row["batches_per_second"] == 1
        assert row["price_updates_per_second"] == 20
        assert row["max_batch_price_updates"] == 30
        assert row["last_processed_batch_ts_init_ns"] == 200
        operations = document["processing_timings"]["operations"]
        assert next(r for r in operations if r["name"] == "market.book_deltas")["count"] == 1
        return 0

    assert timing.run_timed(measured, enabled=True, directory=tmp_path / "timings") == 0
    assert timing.current_timing_snapshot() is None


def test_book_counts_follow_current_levels_and_keep_no_price_history():
    observer = SimpleNamespace(
        level_prices={"BTC.HL": {"BUY": {10, 9}, "SELL": {11}},
                      "ETH.ASTER": {"BUY": {20}, "SELL": {21, 22, 23}}},
        markets={"BTC.HL": SimpleNamespace(venue="HL"), "ETH.ASTER": SimpleNamespace(venue="ASTER")},
        snapshot_seen={"BTC.HL"}, dirty={"BTC.HL": (1, 2, 3)}, pending_snapshot={"ETH.ASTER"},
        subscribed={"BTC.HL"}, pending_aster=["ETH.ASTER"])
    state = SimpleNamespace(books={"BTC.HL": object()}, current={})
    first = memory.summarize_books(observer, state, 100)
    assert first["total_levels"] == 7
    assert first["venues"]["ASTER"]["max_levels_per_side"] == 3
    assert first["venues"]["ASTER"]["snapshot_ready"] == 0
    assert first["venues"]["HL"]["subscriptions_requested"] == 1
    assert first["venues"]["ASTER"]["subscriptions_requested"] == 0
    assert first["pending_aster_subscriptions"] == 1
    observer.level_prices["ETH.ASTER"]["SELL"].clear()
    second = memory.summarize_books(observer, state, 200)
    assert second["total_levels"] == 4
    assert first["total_levels"] == 7
    assert all(set(row) == {"instrument_id", "venue", "bid_levels", "ask_levels"}
               for row in first["largest_books"])


def test_reports_rotate_and_old_native_api_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "MAX_LOG_BYTES", 1)
    diagnostics = memory.MemoryDiagnostics(tmp_path)
    with pytest.raises(RuntimeError, match="metrics_snapshot"):
        diagnostics.attach_handle(SimpleNamespace())
    sample = native_sample()
    diagnostics.attach_handle(SimpleNamespace(metrics_snapshot=lambda: deepcopy(sample)))
    with diagnostics:
        diagnostics.write()
        assert memory.current_diagnostics() is diagnostics
    assert memory.current_diagnostics() is None
    latest = json.loads((tmp_path / "memory-latest.json").read_text())
    assert latest["status"] == "finished"
    assert latest["native_runner"]["data_events"]["queue_depth"] == 4
    assert diagnostics.path.with_suffix(".previous.jsonl").exists()
    assert len(diagnostics.path.read_text().splitlines()) == 1


def test_account_check_metrics_are_copied_without_account_payloads(tmp_path, monkeypatch):
    monkeypatch.setattr(memory, "summarize_books", lambda *args: {"sampled_at_unix_ns": 100})
    state = SimpleNamespace(account_check_metrics={"ASTER": {
        "checked_at_unix_ns": 90, "markets_checked": 160, "proofs_checked": 1,
        "expected_quantities_computed": 634, "position_rows_checked": 634,
        "unconfigured_position_rows": 474}})
    diagnostics = memory.MemoryDiagnostics(tmp_path)
    diagnostics.capture_books(None, state, 100)
    state.account_check_metrics["ASTER"]["proofs_checked"] = 2
    copied = diagnostics.sample()["account_checks"]["ASTER"]
    assert copied["proofs_checked"] == 1
    assert copied["position_rows_checked"] == 634 and copied["unconfigured_position_rows"] == 474


def test_reporting_failure_does_not_change_wrapped_result(tmp_path, monkeypatch):
    def fail(_):
        raise OSError("sensitive diagnostic details must not be printed")
    monkeypatch.setattr(memory.MemoryDiagnostics, "sample", fail)
    args = SimpleNamespace(memory_diagnostics=True, memory_tracemalloc=False,
                           memory_diagnostics_dir=tmp_path)
    assert memory.run_with_memory(lambda: 37, args) == 37
    assert memory.current_diagnostics() is None


def test_batch_diagnostic_failure_is_isolated(tmp_path, monkeypatch):
    diagnostics = memory.MemoryDiagnostics(tmp_path)
    diagnostics.counters["HL"]["batches"] = 127
    def fail():
        raise OSError("clock diagnostic failure")
    monkeypatch.setattr(memory.time, "time_ns", fail)
    diagnostics.record_batch("HL", 4, 0)
    assert diagnostics.errors == 1


def test_timer_age_is_bounded_and_missing_test_events_are_ignored(tmp_path, monkeypatch):
    diagnostics = memory.MemoryDiagnostics(tmp_path)
    monkeypatch.setattr(memory.time, "time_ns", lambda: 20_000_000)
    for _ in range(memory.LATENCY_SAMPLES + 5):
        diagnostics.record_timer(SimpleNamespace(ts_event=15_000_000))
    diagnostics.record_timer(None)
    result = diagnostics.sample()["market_timer"]
    assert result["callbacks"] == memory.LATENCY_SAMPLES + 5
    assert result["age_sample_count"] == memory.LATENCY_SAMPLES
    assert result["age_p95_ms"] == 5
    diagnostics.record_timer(SimpleNamespace())
    assert diagnostics.errors == 1


def test_python_tracing_lifecycle(tmp_path):
    was_tracing = memory.tracemalloc.is_tracing()
    with memory.MemoryDiagnostics(tmp_path, trace_python=True) as diagnostics:
        document = diagnostics.sample()
        assert document["python"]["tracemalloc_enabled"] is True
        assert document["python"]["traced_current_bytes"] >= 0
        assert document["python"]["tracer_overhead_bytes"] > 0
    assert memory.tracemalloc.is_tracing() == was_tracing


def test_failed_start_releases_session_and_nested_session_is_refused(tmp_path):
    invalid = tmp_path / "file"
    invalid.write_text("not a directory")
    with pytest.raises(OSError):
        with memory.MemoryDiagnostics(invalid):
            pytest.fail("started diagnostics with an invalid output directory")
    with memory.MemoryDiagnostics(tmp_path / "valid") as first:
        with pytest.raises(RuntimeError, match="already active"):
            with memory.MemoryDiagnostics(tmp_path / "second"):
                pytest.fail("started a second concurrent diagnostic session")
        assert memory.current_diagnostics() is first
    assert memory.current_diagnostics() is None


def test_native_observer_reports_live_book_counts_without_changing_processing(tmp_path, monkeypatch):
    from test_opportunity_scan import NS, batch, native_observer, runtime
    with memory.MemoryDiagnostics(tmp_path / "memory") as diagnostics:
        _, state, observer, ids, calls, _ = native_observer(tmp_path, monkeypatch,
                                                         evaluation_interval_ms=100)
        assert observer.memory_diagnostics is diagnostics
        observer.on_book_deltas(batch(ids[0], "99.00", "100.00"))
        observer.on_book_deltas(batch(ids[1], "102.00", "103.00"))
        observer.on_time_event(None)
        assert diagnostics.books["total_levels"] == 4
        observer.on_book_deltas(batch(ids[0], "98.00", "101.00", snapshot=False))
        monkeypatch.setattr(runtime.time, "time_ns", lambda: NS + 16_000_000_000)
        observer.on_time_event(None)
        assert diagnostics.books["total_levels"] == 6
        assert diagnostics.counters["ONDO"] == {"batches": 2, "price_updates": 5}
        assert observer.failure is None and observer.running and "stop" not in calls
        def fail(*args):
            raise OSError("diagnostic collection unavailable")
        monkeypatch.setattr(memory, "summarize_books", fail)
        monkeypatch.setattr(runtime.time, "time_ns", lambda: NS + 32_000_000_000)
        observer.on_time_event(None)
        assert diagnostics.errors == 1
        assert observer.failure is None and observer.running and "stop" not in calls
        observer.on_stop()

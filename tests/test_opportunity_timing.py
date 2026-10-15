"""Offline timing validation: clocks, worker threads, reports and trading guards."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opportunity_timing as timing


def rows(recorder):
    return {row["name"]: row for row in recorder.document()["operations"]}


def report(directory):
    files = list(directory.glob("timing-*.json"))
    assert len(files) == 1
    return json.loads(files[0].read_text(encoding="utf-8"))


def test_disabled_has_no_clock_labels_directory_or_thread(monkeypatch, tmp_path):
    def forbidden(*args, **kwargs):
        raise AssertionError("disabled timing attempted work")
    monkeypatch.setattr(timing.time, "perf_counter_ns", forbidden)
    monkeypatch.setattr(timing.threading, "Thread", forbidden)
    @timing.timed("disabled", labels=forbidden)
    def function():
        with timing.span("disabled.child"):
            assert timing.start_latency() is None
            return 7
    assert timing.run_timed(function, directory=tmp_path / "unused") == 7
    assert not (tmp_path / "unused").exists()


def test_nested_self_time_exception_and_monotonic_clock(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    ticks = iter([100, 150, 350, 500])
    monkeypatch.setattr(timing.time, "perf_counter_ns", lambda: next(ticks))
    with pytest.raises(ValueError, match="private-message"):
        with timing.span("outer"):
            with timing.span("inner", venue="HL"):
                raise ValueError("private-message")
    monkeypatch.setattr(timing.time, "perf_counter_ns", lambda: 600)
    measured = rows(recorder)
    assert measured["outer"]["total_ms"] == 400 / 1e6
    assert measured["outer"]["self_ms"] == 200 / 1e6
    assert measured["inner"]["total_ms"] == 200 / 1e6
    assert measured["outer"]["errors"] == measured["inner"]["errors"] == 1
    assert "private-message" not in json.dumps(recorder.document())


def test_bounded_recent_samples_and_exact_lifetime_totals():
    recorder = timing.TimingRecorder()
    for value in range(1, 2049):
        recorder.record("bounded", value)
    row = rows(recorder)["bounded"]
    assert row["count"] == 2048 and row["recent_samples"] == 1024
    assert row["total_ms"] == sum(range(1, 2049)) / 1e6
    assert row["min_ms"] == 1 / 1e6 and row["max_ms"] == 2048 / 1e6
    assert row["recent_p50_ms"] == 1536 / 1e6
    assert row["recent_p95_ms"] == 1997 / 1e6


def test_worker_threads_share_session_without_cross_thread_nesting(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    @timing.timed("worker")
    def worker(_):
        with timing.span("worker.child"):
            pass
    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(worker, range(100)))
    measured = rows(recorder)
    assert measured["worker"]["count"] == measured["worker.child"]["count"] == 100
    assert measured["worker"]["total_ms"] >= measured["worker"]["self_ms"] >= 0


@pytest.mark.parametrize("failure", [None, ValueError, KeyboardInterrupt])
def test_report_preserves_return_and_exception_and_resets_session(tmp_path, failure):
    def function():
        if failure:
            raise failure("secret synthetic payload")
        return 4
    if failure:
        with pytest.raises(failure):
            timing.run_timed(function, enabled=True, directory=tmp_path)
    else:
        assert timing.run_timed(function, enabled=True, directory=tmp_path) == 4
    document = report(tmp_path)
    assert document["finished_at_utc"] and document["elapsed_ms"] >= 0
    assert document["status"] == ("interrupted" if failure is KeyboardInterrupt else "failed")
    assert document["exit_code"] == (None if failure else 4)
    assert "secret synthetic payload" not in json.dumps(document)
    assert timing._active is None and not timing._session_lock.locked()


def test_periodic_snapshot_exists_before_run_finishes(monkeypatch, tmp_path):
    monkeypatch.setattr(timing, "FLUSH_SECONDS", .01)
    written = threading.Event()
    original = timing.TimingRecorder.write
    def write(recorder):
        original(recorder)
        if threading.current_thread().name == "timing-report":
            written.set()
    monkeypatch.setattr(timing.TimingRecorder, "write", write)
    def function():
        with timing.span("completed_work"):
            pass
        assert written.wait(2)
        document = report(tmp_path)
        assert document["status"] == "running" and document["finished_at_utc"] is None
        assert "completed_work" in {row["name"] for row in document["operations"]}
        return 0
    assert timing.run_timed(function, enabled=True, directory=tmp_path) == 0
    assert report(tmp_path)["status"] == "completed"


def test_unwritable_report_does_not_change_return_or_cleanup(monkeypatch, tmp_path, capsys):
    def denied(*args, **kwargs):
        raise OSError("secret path")
    monkeypatch.setattr(Path, "write_text", denied)
    cleaned = []
    def function():
        try:
            with timing.span("work"):
                return 0
        finally:
            cleaned.append(True)
    assert timing.run_timed(function, enabled=True, directory=tmp_path) == 0
    assert cleaned == [True]
    output = capsys.readouterr().err
    assert "report write failed" in output and "secret path" not in output
    assert timing._active is None and not timing._session_lock.locked()


def test_report_thread_start_failure_preserves_result(monkeypatch, tmp_path):
    def unavailable(*args, **kwargs):
        raise RuntimeError("thread unavailable")
    monkeypatch.setattr(timing.threading.Thread, "start", unavailable)
    assert timing.run_timed(lambda: 0, enabled=True, directory=tmp_path) == 0
    assert report(tmp_path)["status"] == "completed"
    assert timing._active is None and not timing._session_lock.locked()


def test_invalid_labels_do_not_break_wrapped_function(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    @timing.timed("labels", labels=lambda: ([], {}))
    def function():
        return 42
    assert function() == 42
    assert rows(recorder)["labels"]["venue"] == rows(recorder)["labels"]["outcome"] == ""


def test_live_dry_run_timing_keeps_stdout_and_identity_side_effects_unchanged(tmp_path, monkeypatch, capsys):
    import opportunity_account_scope as scope
    from opportunity_trade import main
    from test_opportunity_live_runner import configs
    config = configs(tmp_path, all_five=True, venues='["HL","LIGHTER","ASTER","ONDO","BACKPACK"]')
    def forbidden(*args, **kwargs):
        raise AssertionError("dry run attempted account access")
    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(scope, "resolve_account_binding", forbidden)
    monkeypatch.setattr(scope, "AccountRiskGuard", forbidden)
    assert main(["--config", str(config), "--dry-run"]) == 0
    plain = json.loads(capsys.readouterr().out)
    assert main(["--config", str(config), "--dry-run", "--timing", "--timing-dir", str(tmp_path / "timings")]) == 0
    timed = json.loads(capsys.readouterr().out)
    assert plain == timed and timed["remote_writes_allowed"] is False
    document = report(tmp_path / "timings")
    assert "config.live_plan" in {row["name"] for row in document["operations"]}
    assert not (tmp_path / "state").exists()


def test_account_lock_conflict_and_claims_are_measured_and_redacted(tmp_path, monkeypatch):
    import opportunity_account_scope as scope
    from opportunity_trade_store import TradeStore
    from test_opportunity_account_scope import binding, credentials
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    monkeypatch.setattr(scope, "account_registry_directory", lambda: tmp_path / "registry")
    identity = binding(("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"))
    guard = scope.AccountRiskGuard(identity)
    store = TradeStore(tmp_path / "state.sqlite3", "synthetic", account_binding=identity.document())
    try:
        guard.claim(store)
        with pytest.raises(OSError):
            scope.AccountRiskGuard(identity)
        guard.release_clean()
    finally:
        store.close()
        guard.close()
    document = recorder.document()
    locks = [r for r in document["operations"] if r["name"] == "account_lock.acquire"]
    assert {r["venue"] for r in locks} == {"HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"}
    assert sum(r["errors"] for r in locks) == 1
    assert rows(recorder)["account_lock.claim"]["count"] == 1
    assert rows(recorder)["account_lock.release_clean"]["count"] == 1
    serialized = json.dumps(document)
    for key in ("HYPERLIQUID_ACCOUNT_ADDRESS", "ASTER_MAINNET_USER_ADDRESS",
                "ONDO_MAINNET_ACCOUNT_ID", "BACKPACK_MAINNET_API_KEY"):
        assert credentials()[key] not in serialized
    assert all(value not in serialized for _, value in identity.risk_domains)


def test_gateway_response_latency_is_once_per_outcome_and_does_not_resend(monkeypatch):
    from opportunity_native_execution import NativeOrderGateway
    from test_opportunity_native_execution import MODEL, FakeStrategy, event, intent
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    strategy, receipts = FakeStrategy(), []
    gateway = NativeOrderGateway(strategy, [NS(venue="HL", instrument_id="BTC-USD-PERP.HYPERLIQUID")],
        receipts.append, lambda value: None, lambda value: None, model=MODEL, environ={})
    try:
        gateway.dispatch(intent(), 1_000_000_000)
        gateway.dispatch(intent(), 1_000_000_000)
        gateway.on_native_event("accepted", event(), 1_000_000_001)
        gateway.on_native_event("accepted", event(), 1_000_000_002)
        assert len(strategy.calls) == len(receipts) == 1
        response = rows(recorder)["gateway.submit.response"]
        assert response["count"] == 1 and response["category"] == "latency"
        assert response["venue"] == "HL" and response["outcome"] == "accepted"
        assert "owned-1" not in json.dumps(recorder.document())
    finally:
        gateway.close()


def test_closed_recorder_ignores_late_worker_completions():
    recorder = timing.TimingRecorder()
    recorder.finish(0, "completed")
    recorder.record("late", 100)
    assert not recorder.document()["operations"]

"""Latency boundaries must remain bounded, observational and off the ledger."""
from pathlib import Path
from types import SimpleNamespace as NS
import json
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
import opportunity_timing as timing
from test_opportunity_timing import rows


def test_latency_sampling_skips_clocks_and_counts_per_venue(monkeypatch):
    recorder = timing.TimingRecorder()
    def forbidden():
        raise AssertionError('unsampled latency read a clock')
    monkeypatch.setattr(timing.time, 'perf_counter_ns', forbidden)
    monkeypatch.setattr(timing.time, 'time_ns', forbidden)
    assert timing.sampled_start_latency('cycle', venue='HL', every=2) is None
    timing.sampled_delivery_age(1, venue='HL')
    monkeypatch.setattr(timing, '_active', recorder)
    assert timing.sampled_start_latency('cycle', venue='HL', every=2) is None
    assert timing.sampled_start_latency('cycle', venue='ASTER', every=2) is None
    for _ in range(126):
        timing.sampled_delivery_age(1, venue='HL')
    assert not recorder.rows
    monkeypatch.setattr(timing.time, 'perf_counter_ns', lambda: 100)
    marker = timing.sampled_start_latency('cycle', venue='HL', every=2)
    monkeypatch.setattr(timing.time, 'perf_counter_ns', lambda: 140)
    timing.record_latency(marker, 'cycle', venue='HL', sample_every=2)
    monkeypatch.setattr(timing.time, 'time_ns', lambda: 90)
    timing.sampled_delivery_age(10, venue='HL')
    measured = rows(recorder)
    assert measured['cycle']['total_ms'] == 40 / 1e6
    assert measured['cycle']['sample_every'] == 2
    assert measured['market.delivery_age']['total_ms'] == 80 / 1e6
    assert all(r['category'] == 'sampled_latency' for r in measured.values())
    assert len(recorder.local.latency_calls) == 3


def test_shared_endpoint_excludes_recording_cost_from_paired_stages(monkeypatch):
    recorder = timing.TimingRecorder()
    def forbidden():
        raise AssertionError('recording reread an already captured endpoint')
    monkeypatch.setattr(timing.time, 'perf_counter_ns', forbidden)
    timing.record_latency((recorder, 10), 'first', ended_ns=100)
    timing.record_latency((recorder, 40), 'latest', ended_ns=100)
    monkeypatch.setattr(timing.time, 'perf_counter_ns', lambda: 200)
    assert rows(recorder)['first']['total_ms'] == 90 / 1e6
    assert rows(recorder)['latest']['total_ms'] == 60 / 1e6


@pytest.mark.parametrize('stamp', [0, -1, 101, True, None])
def test_invalid_native_clock_is_not_reported_as_zero_queue_latency(monkeypatch, stamp):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    monkeypatch.setattr(timing.time, 'time_ns', lambda: 100)
    for _ in range(127):
        timing.sampled_delivery_age(stamp, venue='HL')
    assert not recorder.rows


@pytest.mark.parametrize('interval', [0, 100])
def test_publication_measures_first_and_latest_updates_and_evaluation_wait(tmp_path, monkeypatch, interval):
    from test_opportunity_scan import NS as NOW, batch, native_observer, runtime
    from test_opportunity_coalescing import replace_touch
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    tick = [0]
    monkeypatch.setattr(timing.time, 'perf_counter_ns', lambda: tick[0])
    monkeypatch.setattr(runtime.time, 'time_ns', lambda: NOW + tick[0])
    # Select each cycle here; periodic selection/disabled clocks are tested above.
    monkeypatch.setattr(runtime, 'sampled_start_latency', lambda *a, **kw: timing.start_latency())
    _, state, observer, ids, _, _ = native_observer(tmp_path, monkeypatch, evaluation_interval_ms=interval)
    monkeypatch.setattr(runtime.time, 'time_ns', lambda: NOW + tick[0])
    observer.on_book_deltas(batch(ids[0], '99.00', '100.00'))
    tick[0] = 40_000_000
    observer.on_book_deltas(replace_touch(ids[0], '99.00', '100.00',
                                        '98.00', '99.00', NOW + tick[0]))
    if interval:
        observer.on_book_deltas(batch(ids[1], '102.00', '103.00'))
        update = state.update_book
        def publish(*args, **kwargs):
            update(*args, **kwargs)
            tick[0] += 5_000_000  # Work publishing each leg before symbol evaluation.
        monkeypatch.setattr(state, 'update_book', publish)
        tick[0] = 100_000_000
        observer.on_time_event(None)
    assert observer.failure is None
    measured = {r['name']: r for r in recorder.document()['operations'] if r['venue'] == 'ONDO'}
    assert measured['market.first_update_to_publish']['max_ms'] == (105 if interval else 0)
    assert measured['market.latest_update_to_publish']['max_ms'] == (65 if interval else 0)
    assert measured['market.publish_to_evaluate']['max_ms'] == (5 if interval else 0)
    assert not observer.book_timings and not observer.publication_timings
    tick[0] += 50_000_000
    observer.on_book_deltas(replace_touch(ids[0], '98.00', '99.00', '97.00', '98.00', NOW + tick[0]))
    observer.invalidate(ids[0])
    assert not observer.book_timings and not observer.publication_timings
    observer.on_stop()


def test_candidate_marker_survives_durability_and_dispatch_wait_but_is_not_persisted(monkeypatch):
    from opportunity_live_runner import LiveCoordinator
    from test_opportunity_live import MemoryStore, state
    from test_opportunity_trade import NOW, publish
    store = MemoryStore()
    live, _ = state(store=store)
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    tick = [100]
    monkeypatch.setattr(timing.time, 'perf_counter_ns', lambda: tick[0])
    save = store.save
    def durable(*args):
        tick[0] += 20
        save(*args)
    monkeypatch.setattr(store, 'save', durable)
    publish(live)
    assert len(live.entry_timings) == 2
    assert rows(recorder)['entry.candidate_to_persisted_intents']['total_ms'] == 20 / 1e6
    tick[0] += 1000
    received = []
    def dispatch(intent, now_ns, *, candidate_marker=None):
        assert store.saved['orders'][intent.client_order_id]['status'] == 'dispatch_released'
        received.append((intent, candidate_marker))
        timing.record_latency(candidate_marker, 'entry.candidate_to_submit', venue=intent.venue)
    coordinator = NS(live=live, proxy=NS(restart_pending=False), gateway=NS(dispatch=dispatch))
    LiveCoordinator._live_intents(coordinator, NOW)
    assert len(received) == 2 and received[0][1] is received[1][1]
    assert not live.entry_timings
    latencies = [r for r in recorder.document()['operations'] if r['name'] == 'entry.candidate_to_submit']
    assert len(latencies) == 2 and all(r['total_ms'] == 1040 / 1e6 for r in latencies)
    assert 'timing' not in json.dumps(store.saved)
    exported = recorder.document()['entry_execution']['traces']
    assert {trace['client_order_id'] for trace in exported} == {
        intent.client_order_id for intent, _ in received}
    assert all('request_id' not in trace for trace in exported)


def test_submit_handoff_excludes_construction_and_records_each_outcome_once(monkeypatch):
    from opportunity_native_execution import NativeOrderGateway
    from test_opportunity_native_execution import MODEL, FakeStrategy, event, intent
    strategy = FakeStrategy()
    gateway = NativeOrderGateway(strategy, [NS(venue='HL', instrument_id='BTC-USD-PERP.HYPERLIQUID')],
        lambda value: None, lambda value: None, lambda value: None, model=MODEL, environ={})
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    tick = [100]
    monkeypatch.setattr(timing.time, 'perf_counter_ns', lambda: tick[0])
    marker = timing.start_latency()
    limit = strategy.limit
    def construct(**kwargs):
        tick[0] += 20
        return limit(**kwargs)
    strategy.order_factory.limit = construct
    try:
        tick[0] += 10
        gateway.dispatch(intent(), 1_000_000_000, candidate_marker=marker)
        tick[0] += 40
        gateway.on_native_event('accepted', event(), 1_000_000_001)
        gateway.on_native_event('accepted', event(), 1_000_000_002)
        gateway.dispatch(intent(), 1_000_000_003, candidate_marker=marker)
        measured = rows(recorder)
        assert measured['entry.candidate_to_submit']['total_ms'] == 30 / 1e6
        assert measured['gateway.construct_order']['total_ms'] == 20 / 1e6
        assert measured['gateway.submit_handoff']['count'] == 1
        assert measured['gateway.submit.response']['total_ms'] == 60 / 1e6
        assert measured['gateway.submit_to_outcome']['total_ms'] == 40 / 1e6
        assert measured['gateway.submit_to_outcome']['count'] == 1
        assert len(strategy.calls) == 1
    finally:
        gateway.close()


def test_local_rejection_has_no_submit_latency_or_fake_receipt(monkeypatch):
    from opportunity_native_execution import NativeOrderGateway
    from test_opportunity_native_execution import MODEL, FakeStrategy, intent
    strategy, receipts = FakeStrategy(), []
    gateway = NativeOrderGateway(strategy, [NS(venue='HL', instrument_id='BTC-USD-PERP.HYPERLIQUID')],
        receipts.append, lambda value: None, lambda value: None, model=MODEL, environ={})
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    try:
        gateway.dispatch(intent(price=0), 1_000_000_000, candidate_marker=timing.start_latency())
        assert [r.kind for r in receipts] == ['rejected'] and not strategy.calls
        assert 'gateway.submit_to_outcome' not in rows(recorder)
        assert 'entry.candidate_to_submit' not in rows(recorder)
    finally:
        gateway.close()


def test_submit_markers_are_capped_without_dropping_orders_or_receipts(monkeypatch):
    import opportunity_native_execution as execution
    from test_opportunity_native_execution import MODEL, FakeStrategy, event, intent
    strategy, receipts = FakeStrategy(), []
    gateway = execution.NativeOrderGateway(strategy, [NS(venue='HL', instrument_id='BTC-USD-PERP.HYPERLIQUID')],
        receipts.append, lambda value: None, lambda value: None, model=MODEL, environ={})
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    monkeypatch.setattr(execution, 'MAX_PENDING_SUBMIT_TIMINGS', 2)
    try:
        for n in range(3):
            gateway.dispatch(intent(request_id=f'r{n}', client_order_id=f'c{n}'), 1_000_000_000)
        assert set(gateway.submit_timings) == {'r1', 'r2'}
        for n in range(3):
            gateway.on_native_event('accepted', event(client_order_id=f'c{n}'), 1_000_000_001)
        assert len(strategy.calls) == len(receipts) == 3
        assert rows(recorder)['gateway.submit_to_outcome']['count'] == 2
        strategy.cached['c1'].is_closed = True
        gateway.on_native_event('expired', event(client_order_id='c1'), 1_000_000_002)
        assert set(gateway.submit_timings) == {'r2'}
    finally:
        gateway.close()
    assert not gateway.submit_timings

"""Offline send-time gates and bounded, correlated execution diagnostics."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
import json
from types import SimpleNamespace as NS

import pytest

from test_opportunity_live import MemoryStore, fill, receipt, state, submissions
from test_opportunity_trade import NOW, SECOND, publish, set_book
import opportunity_timing as timing
from opportunity_live_runner import LiveCoordinator
from opportunity_native_execution import classify_rejection


def coordinator(live, dispatch):
    return NS(live=live, proxy=NS(restart_pending=False), gateway=NS(dispatch=dispatch))


@pytest.mark.parametrize('failure,expected', [
    ('stale', 'stale_or_invalid_book'), ('depth', 'insufficient_depth'),
    ('fees', 'net_edge_insufficient'), ('price', 'price_outside_limit'),
    ('skew', 'book_skew'), ('headroom', 'unhedged_headroom_insufficient')])
def test_invalidated_durable_candidate_never_dispatches(failure, expected):
    store = MemoryStore()
    live, events = state(store=store)
    publish(live)
    now = NOW
    if failure == 'stale':
        now += 3 * SECOND
    elif failure == 'depth':
        set_book(live, 'HL', '99.99', '100', ask_size='0.1')
    elif failure == 'fees':
        meta = live.metadata['BTC.HL']
        live.update_metadata('BTC.HL', replace(meta, taker_fee_bps=D(1)), NOW, evaluate=False)
    elif failure == 'price':
        set_book(live, 'HL', '100', '100.01')
    elif failure == 'headroom':
        set_book(live, 'ASTER', '100.3', '101')
    else:
        now += 600_000_000
        set_book(live, 'HL', '99.99', '100', now)
    calls = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: calls.append(a)), now)
    assert not calls
    assert all(o['status'] == 'local_not_dispatched' and o['terminal_ns'] is not None
               for o in live.orders.values())
    blocked = [e for e in events if e['event'] == 'entry_dispatch_blocked']
    assert len(blocked) == 1 and blocked[0]['reason'] == expected
    assert store.saved['orders'] == live.orders


def test_valid_send_retains_persisted_amounts_fees_and_durability(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    store = MemoryStore()
    live, _ = state(store=store)
    publish(live)
    prepared = {cid: (o['quantity'], o['price']) for cid, o in live.orders.items()}
    calls = []
    def dispatch(intent, now_ns, **kwargs):
        assert store.saved['orders'][intent.client_order_id]['status'] == 'dispatch_released'
        assert (str(intent.quantity), str(intent.price)) == prepared[intent.client_order_id]
        calls.append(intent)
    LiveCoordinator._live_intents(coordinator(live, dispatch), NOW)
    assert len(calls) == 2
    traces = recorder.document()['entry_execution']['traces']
    assert len(traces) == 2
    for trace in traces:
        assert [s['stage'] for s in trace['stages']] == [
            'pricing_started', 'candidate', 'persist_started', 'persisted',
            'dispatch_release_started', 'dispatch_release_completed', 'coordinator', 'send_check']
        evidence = trace['stages'][-1]['evidence']
        assert F(evidence['net_margin_usd']) >= 0
        assert all(evidence[leg]['executable_vwap'] and evidence[leg]['depth_within_limit']
                   for leg in ('buy', 'sell'))
    assert 'timing' not in json.dumps(store.saved)
    assert {trace['client_order_id'] for trace in traces} == {i.client_order_id for i in calls}
    assert all('request_id' not in trace for trace in traces)


def test_second_leg_recheck_preserves_first_dispatched_identity(monkeypatch):
    live, _ = state()
    publish(live)
    calls = []
    def dispatch(intent, now_ns, **kwargs):
        calls.append(intent)
        # A synchronous callback can change the other leg before the loop resumes.
        set_book(live, 'ASTER', '100.3', '100.31', bid_size='0.1')
    LiveCoordinator._live_intents(coordinator(live, dispatch), NOW)
    assert len(calls) == 1
    assert live.orders[calls[0].client_order_id]['terminal_ns'] is None
    other = next(o for cid, o in live.orders.items() if cid != calls[0].client_order_id)
    assert other['status'] == 'local_not_dispatched'


def test_send_check_accounts_for_synchronous_first_dispatch_wait(monkeypatch):
    live, _ = state()
    publish(live)
    tick = [0]
    monkeypatch.setattr('opportunity_live_runner.time.perf_counter_ns', lambda: tick[0])
    calls = []
    def dispatch(intent, now_ns, **kwargs):
        calls.append(intent)
        tick[0] += 3 * SECOND
    LiveCoordinator._live_intents(coordinator(live, dispatch), NOW)
    assert len(calls) == 1
    assert live.orders[calls[0].client_order_id]['terminal_ns'] is None


@pytest.mark.parametrize('outcome', ['bilateral_full', 'single_leg', 'bilateral_partial', 'unfilled'])
def test_pair_metrics_count_only_final_pair_once(monkeypatch, outcome):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    live, _ = state()
    publish(live)
    intents = submissions(live)
    for index, intent in enumerate(intents):
        if outcome == 'bilateral_full':
            fill(live, intent, NOW + 1)
        elif outcome == 'single_leg' and index == 0:
            fill(live, intent, NOW + 1)
        elif outcome == 'bilateral_partial':
            fill(live, intent, NOW + 1, quantity=intent.quantity / 2)
        else:
            live.on_receipt(receipt(intent, 'rejected'), NOW + 1)
        if index == 0:
            assert recorder.document()['entry_execution']['completed_pairs'] == 0
    for intent in intents:
        live.on_receipt(receipt(intent, 'terminal'), NOW + 2)
    result = recorder.document()['entry_execution']
    assert result['completed_pairs'] == 1 and result['pair_outcomes'][outcome] == 1
    assert result['bilateral_completion_rate'] == int(outcome == 'bilateral_full')
    assert result['single_leg_rate'] == int(outcome == 'single_leg')


def test_trace_and_stage_storage_are_bounded_and_allowlisted(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    monkeypatch.setattr(timing, 'SAMPLE_WINDOW', 2)
    for index in range(3):
        rid = 'private-order-' + str(index)
        timing.begin_entry_trace(rid, 'HL', timing.start_latency(), NOW)
        for _ in range(60):
            timing.record_entry_stage(rid, 'send_check', evidence={
                'credentials': 'private', 'buy': {'quantity': '1', 'account': 'private'}},
                reason='private')
    report = recorder.document()['entry_execution']
    assert len(report['traces']) == 2 and report['trace_evictions'] == 1
    assert all(len(trace['stages']) == 48 for trace in report['traces'])
    assert report['missing_native_stages'] == ['native_received', 'signing', 'network_write', 'native_receipt_received']
    assert 'private' not in json.dumps(report)


@pytest.mark.parametrize('raw,expected', [
    ('Order could not immediately match against any resting orders. IOC', 'ioc_no_match'),
    ('rate limit token-secret', 'rate_limit'), ('Insufficient margin', 'insufficient_balance'),
    ('Invalid minimum tick', 'invalid_order'), ('Post only would cross', 'post_only'),
    ('undocumented token-secret', 'venue_other')])
def test_rejection_reason_is_fixed_label(raw, expected):
    assert classify_rejection(raw) == expected



def test_gateway_outcomes_separate_accepted_partial_full_and_rejected(monkeypatch):
    from opportunity_native_execution import NativeOrderGateway
    from test_opportunity_native_execution import MODEL, FakeStrategy, event, intent
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    strategy = FakeStrategy()
    gateway = NativeOrderGateway(strategy, [NS(venue='HL', instrument_id='BTC-USD-PERP.HYPERLIQUID')],
        lambda value: None, lambda value: None, lambda value: None, model=MODEL, environ={})
    try:
        original = intent()
        marker = timing.start_latency()
        timing.begin_entry_trace(original.request_id, 'HL', marker, NOW)
        gateway.dispatch(original, NOW, candidate_marker=marker)
        gateway.on_native_event('accepted', event(), NOW + 1)
        for trade, amount in [('one', '.003'), ('two', '.007')]:
            gateway.on_native_event('filled', event(trade_id=trade, last_qty=D(amount), last_px=D(100),
                                                  order_side='BUY'), NOW + 2)
        refused = intent(request_id='reject', client_order_id='reject')
        timing.begin_entry_trace(refused.request_id, 'HL', timing.start_latency(), NOW)
        gateway.dispatch(refused, NOW)
        strategy.cached['reject'].is_closed = True
        gateway.on_native_event('rejected', event(client_order_id='reject',
            reason='IOC could not immediately match token-secret'), NOW + 3)
        outcomes = {r['outcome']: r for r in recorder.document()['operations']
                    if r['name'] == 'gateway.submit_to_outcome'}
        assert set(outcomes) == {'accepted', 'partial_fill', 'full_fill', 'rejected'}
        assert all(r['count'] == r['recent_samples'] == 1 for r in outcomes.values())
        assert all('recent_p50_ms' in r and 'recent_p95_ms' in r and 'recent_p99_ms' in r
                   for r in outcomes.values())
        report = recorder.document()['entry_execution']
        assert report['traces'][0]['stages'][1]['stage'] == 'python_submit'
        assert any(s.get('reason') == 'ioc_no_match' for s in report['traces'][1]['stages'])
        assert 'token-secret' not in json.dumps(report)
    finally:
        gateway.close()


def test_late_economic_evidence_reclassifies_pair_without_double_count(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    for rid in ('first', 'second'):
        timing.begin_entry_trace(rid, 'HL', timing.start_latency(), NOW)
    timing.record_entry_pair(('first', 'second'), 'unfilled')
    timing.record_entry_pair(('first', 'second'), 'single_leg')
    timing.record_entry_pair(('first', 'second'), 'single_leg')
    result = recorder.document()['entry_execution']
    assert result['completed_pairs'] == 1
    assert result['pair_outcomes']['unfilled'] == 0
    assert result['pair_outcomes']['single_leg'] == result['pair_reclassifications'] == 1


@pytest.mark.parametrize('changed', ['halt', 'reconciliation', 'stopping', 'entry_pause', 'inactive', 'cleanup'])
def test_execution_admission_changes_after_prepare_block_unsent_pair(changed):
    live, events = state()
    publish(live)
    if changed == 'halt':
        live.halt_reason = 'synthetic hard protection'
    elif changed == 'reconciliation':
        live.order_reconciliations['synthetic-order'] = {'since_ns': NOW}
    elif changed == 'stopping':
        live.stopping = True
    elif changed == 'entry_pause':
        live.entry_pause_reason = 'synthetic pause'
    elif changed == 'inactive':
        live.phase = 'waiting_preflight'
    else:
        live.cleanup_allowed = False
    calls = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: calls.append(a)), NOW)
    assert not calls
    assert all(o['status'] == 'local_not_dispatched' for o in live.orders.values())
    assert any(e.get('reason') == 'entry_admission_changed' for e in events)


@pytest.mark.parametrize('changed', ['disconnect', 'market_not_trading'])
def test_market_status_changes_after_prepare_cannot_reuse_valid_old_quote(changed):
    live, _ = state()
    publish(live)
    if changed == 'disconnect':
        # Observer.on_instrument_status(adapter:disconnected) follows this path.
        live.invalidate('BTC.HL', NOW)
    else:
        # Observer publishes a non-tradable snapshot with valid=False.
        live.books['BTC.HL'] = replace(live.books['BTC.HL'], valid=False)
    calls = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: calls.append(a)), NOW)
    assert not calls
    assert all(o['status'] == 'local_not_dispatched' for o in live.orders.values())


@pytest.mark.parametrize('changed', ['halt', 'reconciliation', 'stopping', 'entry_pause'])
def test_synchronous_first_acceptance_then_protection_never_sends_second_leg(changed):
    live, _ = state()
    publish(live)
    calls = []
    def dispatch(intent, now_ns, **kwargs):
        if intent.action != 'submit':
            return
        calls.append(intent)
        live.on_receipt(receipt(intent, 'accepted'), NOW + 1)
        if changed == 'halt':
            live.halt_reason = 'synthetic hard protection'
        elif changed == 'reconciliation':
            live.order_reconciliations[intent.client_order_id] = {'since_ns': NOW + 1}
        elif changed == 'stopping':
            live.stopping = True
        else:
            live.entry_pause_reason = 'synthetic pause'
    LiveCoordinator._live_intents(coordinator(live, dispatch), NOW)
    assert len(calls) == 1
    assert live.orders[calls[0].client_order_id]['terminal_ns'] is None
    assert sum(o['status'] == 'local_not_dispatched' for o in live.orders.values()) == 1


def test_account_can_expire_while_latest_books_stay_fresh_after_prepare():
    live, events = state(account_max_age_ms=1)
    publish(live)
    now = NOW + 2_000_000
    set_book(live, 'HL', '99.99', '100', now)
    set_book(live, 'ASTER', '100.3', '100.31', now)
    calls = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: calls.append(a)), now)
    assert not any(a[0].action == 'submit' for a in calls)
    assert all(o['status'] == 'local_not_dispatched' for o in live.orders.values())
    assert any(e.get('reason') == 'account_admission_changed' for e in events)


def test_own_released_pending_entries_do_not_block_fresh_account_recheck():
    live, _ = state()
    publish(live)
    calls = []
    def dispatch(intent, now_ns, **kwargs):
        calls.append(intent)
        live.on_receipt(receipt(intent, 'accepted'), NOW + 1)
    LiveCoordinator._live_intents(coordinator(live, dispatch), NOW)
    assert len(calls) == 2
    assert all(o['accepted_ns'] == NOW + 1 and o['terminal_ns'] is None for o in live.orders.values())


def test_preparation_fence_allowance_retains_previous_real_activity_boundary():
    live, events = state()
    publish(live)
    # Explicitly retain an earlier real activity fence stronger than the old
    # account observation. A preparation fence must never erase this boundary.
    for order in live.orders.values():
        order['preparation_account_fence_ns'] = NOW + 1
    calls = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: calls.append(a)), NOW + 2)
    assert not any(a[0].action == 'submit' for a in calls)
    assert any(e.get('reason') == 'account_admission_changed' for e in events)


def test_actual_first_leg_fill_does_not_inherit_preparation_fence_allowance():
    live, _ = state()
    publish(live)
    calls = []
    def dispatch(intent, now_ns, **kwargs):
        if intent.action != 'submit':
            return
        calls.append(intent)
        fill(live, intent, NOW + 1, quantity='0.4', terminal=False)
    LiveCoordinator._live_intents(coordinator(live, dispatch), NOW + 2)
    assert len(calls) == 1
    assert live.orders[calls[0].client_order_id]['filled_quantity'] == '0.4'
    assert live.orders[calls[0].client_order_id]['terminal_ns'] is None
    assert sum(o['status'] == 'local_not_dispatched' for o in live.orders.values()) == 1

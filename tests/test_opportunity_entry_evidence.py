"""Offline send-time gates and bounded, correlated execution diagnostics."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
import json
from types import SimpleNamespace as NS

import pytest

from test_opportunity_live import MemoryStore, fill, receipt, state, submissions
from test_opportunity_trade import NOW, SECOND, make_plan, publish, set_book
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


def _open_for_soft_exit(store=None):
    from test_opportunity_live import accounts
    live, events = state(store=store)
    publish(live)
    for intent in submissions(live):
        fill(live, intent, NOW + 1)
    accounts_now = NOW + SECOND
    accounts(live, accounts_now)
    live.on_evaluation_timer(accounts_now)
    live.drain_intents()
    return live, events, accounts_now


def _stage_soft_exit(live, bps=10, age_seconds=2):
    from test_opportunity_live import accounts
    position = next(iter(live.positions.values()))
    now = position["opened_ns"] + age_seconds * SECOND
    cover = D("100.29") - D(str(bps)) / 100
    accounts(live, now)
    live.drain_intents()
    publish(live, now, sell_bid=cover - D("0.01"), sell_ask=cover)
    return now


@pytest.mark.parametrize('failure,expected', [
    ('inactive', 'exit_admission_changed'),
    ('stale', 'stale_or_invalid_book'),
    ('depth', 'insufficient_depth'),
    ('price', 'price_outside_limit'),
    ('fees', 'net_edge_insufficient'),
    ('skew', 'book_skew'),
])
def test_soft_exit_send_check_blocks_invalidated_exit_before_first_leg_and_resets_open(failure, expected):
    store = MemoryStore()
    live, events, _ = _open_for_soft_exit(store=store)
    now = _stage_soft_exit(live, 10, 2)
    assert live.outbox and next(iter(live.positions.values()))['exit_reason'] == 'take_profit'
    if failure == 'inactive':
        live.phase = 'waiting_preflight'
    elif failure == 'stale':
        now += 3 * SECOND
    elif failure == 'depth':
        set_book(live, 'HL', '99.99', '100', now, bid_size='0.1')
    elif failure == 'price':
        set_book(live, 'HL', '99.90', '99.91', now)
    elif failure == 'fees':
        meta = live.metadata['BTC.HL']
        live.update_metadata('BTC.HL', replace(meta, taker_fee_bps=D(20)), now, evaluate=False)
    else:
        now += 600_000_000
        set_book(live, 'HL', '99.99', '100', now)
    calls = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: calls.append(a)), now)
    assert not calls
    position = next(iter(live.positions.values()))
    assert position['status'] == 'open' and position['exit_reason'] is None
    assert all(live.orders[cid]['status'] == 'local_not_dispatched' for cid in position['exit_ids'])
    blocked = [e for e in events if e['event'] == 'exit_dispatch_blocked']
    assert len(blocked) == 1 and blocked[0]['reason'] == expected
    assert store.saved['orders'] == live.orders


def test_positive_after_wait_send_check_blocks_non_positive_limit_before_dispatch():
    live, events, _ = _open_for_soft_exit()
    now = _stage_soft_exit(live, 1, 180)
    assert next(iter(live.positions.values()))['exit_reason'] == 'positive_after_wait'
    meta = live.metadata['BTC.HL']
    live.update_metadata('BTC.HL', replace(meta, taker_fee_bps=D(2)), now, evaluate=False)
    calls = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: calls.append(a)), now)
    assert not calls
    position = next(iter(live.positions.values()))
    assert position['status'] == 'open' and position['exit_reason'] is None
    assert events[-1]['event'] == 'exit_dispatch_blocked' and events[-1]['reason'] == 'net_edge_insufficient'


def test_soft_exit_recheck_never_blocks_second_leg_after_first_dispatches_or_hard_exits():
    live, _, _ = _open_for_soft_exit()
    now = _stage_soft_exit(live, 10, 2)
    calls = []
    def dispatch(intent, stamp, **kwargs):
        if intent.action == 'submit':
            calls.append(intent)
            # Market moves during Leg 1 send: Leg 2 of committed soft exit still sends.
            set_book(live, 'ASTER', '100.3', '100.4', stamp)
    LiveCoordinator._live_intents(coordinator(live, dispatch), now)
    assert len(calls) == 2

    live_hard, _, _ = _open_for_soft_exit()
    hard_now = _stage_soft_exit(live_hard, -25, 2)
    assert next(iter(live_hard.positions.values()))['exit_reason'] == 'max_loss'
    live_hard.phase = 'draining'
    hard_calls = []
    LiveCoordinator._live_intents(coordinator(live_hard, lambda i, *a, **k: hard_calls.append(i)), hard_now)
    assert len(hard_calls) == 2


def test_paired_entry_fast_path_reuses_unchanged_admission_and_blocks_synchronous_zero_fill(monkeypatch):
    import opportunity_entry_evidence as ev
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, '_active', recorder)
    live, _ = state()
    publish(live)
    admission_calls = []
    real_evidence = ev.entry_dispatch_evidence
    def spy(state_arg, position_arg, now_arg):
        admission_calls.append(now_arg)
        return real_evidence(state_arg, position_arg, now_arg)
    monkeypatch.setattr(ev, 'entry_dispatch_evidence', spy)
    dispatched = []
    LiveCoordinator._live_intents(coordinator(live, lambda i, *a, **k: dispatched.append(i)), NOW)
    assert len(dispatched) == 2 and len(admission_calls) == 1
    traces = recorder.document()['entry_execution']['traces']
    assert len(traces) == 2 and all(t['stages'][-1]['stage'] == 'send_check' for t in traces)

    # If Leg 1 synchronously terminates unfilled, Leg 2 is blocked as local_not_dispatched.
    live2, events2 = state()
    publish(live2)
    dispatched2 = []
    def zero_reject_dispatch(intent, stamp, **kwargs):
        dispatched2.append(intent)
        live2.on_receipt(receipt(intent, 'rejected'), stamp + 1)
    LiveCoordinator._live_intents(coordinator(live2, zero_reject_dispatch), NOW)
    assert len(dispatched2) == 1
    other = next(o for cid, o in live2.orders.items() if cid != dispatched2[0].client_order_id)
    assert other['status'] == 'local_not_dispatched'
    blocked = [e for e in events2 if e['event'] == 'entry_dispatch_blocked']
    assert blocked and blocked[-1]['reason'] == 'first_leg_unfilled'


def test_backpack_paired_entry_fast_path_calls_native_refresh_once_unless_expired(monkeypatch):
    live, _ = state(make_plan(venues=('HL', 'BACKPACK')))
    live.update_native_admission({'BACKPACK': {'ready': True, 'reason': None, 'checked_ns': NOW}}, NOW)
    set_book(live, 'HL', '99.99', '100', NOW)
    set_book(live, 'BACKPACK', '100.3', '100.31', NOW)
    live.evaluate_symbol('BTC', NOW)
    assert len(live.outbox) == 2
    refreshes = []
    def refresh(stamp):
        refreshes.append(stamp)
        return {'BACKPACK': {'ready': True, 'reason': None, 'checked_ns': stamp}}
    sent = []
    coord = NS(live=live, proxy=NS(restart_pending=False),
               gateway=NS(refresh_native_admission=refresh, dispatch=lambda i, s, **k: sent.append(i)))
    LiveCoordinator._live_intents(coord, NOW)
    assert len(sent) == 2 and len(refreshes) == 1

    # If native_admission expires during Leg 1 dispatch, Leg 2 falls back to full refresh & recheck.
    live2, events2 = state(make_plan(venues=('HL', 'BACKPACK')))
    live2.native_admission_max_age_ns = 10_000_000
    live2.update_native_admission({'BACKPACK': {'ready': True, 'reason': None, 'checked_ns': NOW}}, NOW)
    set_book(live2, 'HL', '99.99', '100', NOW)
    set_book(live2, 'BACKPACK', '100.3', '100.31', NOW)
    live2.evaluate_symbol('BTC', NOW)
    assert len(live2.outbox) == 2
    tick = [0]
    monkeypatch.setattr('opportunity_live_runner.time.perf_counter_ns', lambda: tick[0])
    refreshes2, sent2 = [], []
    def refresh2(stamp):
        refreshes2.append(stamp)
        if len(refreshes2) == 1:
            return {'BACKPACK': {'ready': True, 'reason': None, 'checked_ns': NOW}}
        return {'BACKPACK': {'ready': False, 'reason': 'native quote expired', 'checked_ns': stamp}}
    def dispatch2(intent, stamp, **kwargs):
        sent2.append(intent)
        tick[0] += 20_000_000  # Exceeds native_admission_max_age_ns (10 ms) while still < 100 ms dispatch delay
    coord2 = NS(live=live2, proxy=NS(restart_pending=False),
                gateway=NS(refresh_native_admission=refresh2, dispatch=dispatch2))
    LiveCoordinator._live_intents(coord2, NOW)
    assert len(sent2) == 1 and len(refreshes2) == 2
    assert any(e.get('reason') == 'native_admission_not_ready' for e in events2)


@pytest.mark.parametrize('venue,expected_reason', [
    ('ASTER', 'leverage_stale'),
    ('LIGHTER', 'lighter_margin_stale'),
])
def test_paired_entry_fast_path_respects_leverage_and_margin_expiry(monkeypatch, venue, expected_reason):
    import opportunity_entry_evidence as ev
    tick = [0]
    monkeypatch.setattr('opportunity_live_runner.time.perf_counter_ns', lambda: tick[0])

    # Within aster_leverage_max_age_ns, Leg 2 reuses cached Leg 1 admission evidence.
    live_ok, _ = state(make_plan(venues=('HL', venue)), aster_leverage_max_age_ms=10)
    set_book(live_ok, 'HL', '99.99', '100', NOW)
    set_book(live_ok, venue, '100.3', '100.31', NOW)
    live_ok.evaluate_symbol('BTC', NOW)
    position_ok = next(iter(live_ok.positions.values()))
    assert ev.entry_pair_valid_until(live_ok, position_ok) == NOW + 10_000_000
    calls_ok, sent_ok = [], []
    real_evidence = ev.entry_dispatch_evidence
    def spy_ok(state_arg, pos_arg, now_arg):
        calls_ok.append(now_arg)
        return real_evidence(state_arg, pos_arg, now_arg)
    monkeypatch.setattr(ev, 'entry_dispatch_evidence', spy_ok)
    def dispatch_ok(intent, stamp, **kwargs):
        sent_ok.append(intent)
        tick[0] += 5_000_000
    LiveCoordinator._live_intents(coordinator(live_ok, dispatch_ok), NOW)
    assert len(sent_ok) == 2 and len(calls_ok) == 1

    # Exceeding aster_leverage_max_age_ns between Leg 1 and Leg 2 invalidates cached fast-path.
    tick[0] = 0
    live, events = state(make_plan(venues=('HL', venue)), aster_leverage_max_age_ms=10)
    set_book(live, 'HL', '99.99', '100', NOW)
    set_book(live, venue, '100.3', '100.31', NOW)
    live.evaluate_symbol('BTC', NOW)
    position = next(iter(live.positions.values()))
    assert ev.entry_pair_valid_until(live, position) == NOW + 10_000_000
    calls, sent = [], []
    def spy_expired(state_arg, pos_arg, now_arg):
        calls.append(now_arg)
        return real_evidence(state_arg, pos_arg, now_arg)
    monkeypatch.setattr(ev, 'entry_dispatch_evidence', spy_expired)
    def dispatch_expired(intent, stamp, **kwargs):
        sent.append(intent)
        tick[0] += 20_000_000  # Exceeds aster_leverage_max_age_ns (10 ms) while < account/book max age
    LiveCoordinator._live_intents(coordinator(live, dispatch_expired), NOW)
    assert len(sent) == 1 and len(calls) == 2
    other = next(o for cid, o in live.orders.items() if cid != sent[0].client_order_id)
    assert other['status'] == 'local_not_dispatched'
    blocked = [e for e in events if e['event'] == 'entry_dispatch_blocked']
    assert blocked and blocked[-1]['reason'] == expected_reason

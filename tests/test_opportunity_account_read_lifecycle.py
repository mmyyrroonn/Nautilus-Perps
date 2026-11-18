"""Offline recovery across native snapshot polling and account-read deadlines."""
from concurrent.futures import Future
from decimal import Decimal as D
import json
from types import SimpleNamespace as NS

import pytest

from test_opportunity_native_execution import FakeStrategy, MODEL
from test_opportunity_trade import NOW, SECOND, make_plan
from opportunity_account_reader import AccountReadError
from opportunity_live import LiveTradeState
from opportunity_native_execution import NativeOrderGateway
from opportunity_preflight import AccountSnapshot
from opportunity_trade import TradePolicy


class AccountWorker:
    """Run captured account validation deterministically without network IO."""

    def __init__(self):
        self.jobs = []

    def submit(self, function, *args):
        future = Future()
        future.set_running_or_notify_cancel()
        self.jobs.append((future, function, args))
        return future

    def finish(self):
        future, function, args = self.jobs.pop(0)
        try:
            future.set_result(function(*args))
        except Exception as exc:
            future.set_exception(exc)

    def shutdown(self, **kwargs):
        pass


@pytest.fixture(params=["BACKPACK", "ONDO"])
def recovery(request):
    venue = request.param
    plan = make_plan(venues=(venue,))
    live = LiveTradeState(plan, TradePolicy(), max_pairs=2,
        max_gross_notional_usd=D(500), account_max_age_ms=15000)
    instrument = plan.markets[0].instrument_id
    state = NS(venue=venue, live=live, instrument=instrument, now=NOW,
               snapshot=None, error=None, captured={}, receipts=[], accounts=[])
    strategy = FakeStrategy()
    strategy.cache.instrument = lambda _: NS(size_increment=D(".001"),
        price_increment=D(".01"), min_quantity=D(".001"), min_notional=None, multiplier=D(1))

    def native_snapshot():
        if state.error:
            raise state.error
        return json.dumps(state.snapshot)

    def capture(_, payload, **kwargs):
        state.captured = dict(payload)

    def read(*args):
        return AccountSnapshot(venue, instrument, None, None,
            state.captured["snapshot_started_ns"], D(0),
            account_positions=(), account_open_orders=())

    def on_receipt(receipt):
        state.receipts.append(receipt)
        live.on_receipt(receipt, state.now)

    def on_account(account):
        state.accounts.append(account)
        live.on_account(account, state.now)

    reader = NS(capture_native_evidence=capture,
        register_native_instrument=lambda *args, **kwargs: None, read=read)
    control = NS(account_snapshot_json=native_snapshot, production_trade_snapshot=native_snapshot,
        renew_authority=lambda *args: None, persist_economics=lambda: None,
        reconcile_terminal_evidence=lambda: None, admission_status_json=lambda: None)
    gateway = NativeOrderGateway(strategy, plan.markets, on_receipt, on_account, lambda _: None,
        reader=reader, model=MODEL, native_controls={venue: control}, account_timeout_secs=20)
    for worker in gateway.workers.values():
        worker.shutdown(wait=True)
    state.worker = gateway.workers[venue] = AccountWorker()
    state.gateway, state.strategy = gateway, strategy
    yield state
    gateway.close()
    assert not strategy.calls and not live.orders and not live.positions


def snapshot(state, started, *, complete=True, connected=True):
    state.snapshot = {"snapshot_started_ns": started,
        "coverage_complete" if state.venue == "ONDO" else "complete": complete,
        "connected_ready" if state.venue == "ONDO" else "connected": connected}


def request_account(state):
    assert not state.live.flat_accounts_ready(state.now, NOW)
    intent, = state.live.drain_intents()
    state.gateway.dispatch(intent, state.now)
    return intent


@pytest.mark.parametrize("pending", ["absent", "incomplete", "exception"])
def test_pending_native_cycle_recovers_without_invalidating_its_read_start(recovery, pending):
    state = recovery
    original = request_account(state)
    started = NOW + 74_000_000
    state.now = NOW + 146_000_000
    if pending == "incomplete":
        snapshot(state, started, complete=False)
    elif pending == "exception":
        state.error = RuntimeError("authenticated account snapshot absent")
    state.gateway.poll(state.now)
    assert not state.receipts and not state.accounts
    assert state.live.account_request_ids[state.instrument] == original.request_id
    assert not state.live.account_read_failures
    state.error = None
    snapshot(state, started)
    state.now = NOW + 200_000_000
    state.gateway.poll(state.now)
    state.worker.finish()
    state.gateway.poll(state.now)
    assert state.live.flat_accounts_ready(state.now, NOW)
    assert state.accounts[0].observed_ns == started
    assert not state.live.account_pauses and not state.live.account_retry_after_ns
    assert not state.receipts


def test_account_read_keeps_original_id_past_order_timeout(recovery):
    state = recovery
    original = request_account(state)
    state.now = NOW + 11 * SECOND
    state.gateway.poll(state.now)
    assert not state.live.flat_accounts_ready(state.now, NOW)
    assert not state.live.drain_intents()
    assert state.live.account_request_ids[state.instrument] == original.request_id
    assert not state.receipts
    snapshot(state, state.now)
    state.gateway.poll(state.now)
    state.worker.finish()
    state.gateway.poll(state.now)
    assert state.live.flat_accounts_ready(state.now, NOW)


def test_native_timeout_retires_old_poll_and_retry_requires_a_new_cycle(recovery):
    state = recovery
    original = request_account(state)
    state.now = NOW + 20 * SECOND
    snapshot(state, state.now - SECOND)
    # A result first consumed at the deadline must not revive the old request.
    state.gateway.poll(state.now)
    assert [(r.request_id, r.kind) for r in state.receipts] == [(original.request_id, "unknown")]
    assert not state.accounts and not state.gateway.native_reads and not state.worker.jobs
    state.now += 5 * SECOND
    retry = request_account(state)
    assert retry.request_id != original.request_id and not state.worker.jobs
    snapshot(state, state.now + 74_000_000)
    state.now += 200_000_000
    state.gateway.poll(state.now)
    state.worker.finish()
    state.gateway.poll(state.now)
    assert state.live.flat_accounts_ready(state.now, NOW)
    assert not state.live.account_read_failures and not state.live.account_retry_after_ns


@pytest.mark.parametrize("done_at_timeout", [False, True])
def test_timed_out_worker_result_is_discarded_before_recovery(recovery, done_at_timeout):
    state = recovery
    original = request_account(state)
    snapshot(state, NOW + SECOND)
    state.now = NOW + 2 * SECOND
    state.gateway.poll(state.now)
    assert len(state.worker.jobs) == 1
    if done_at_timeout:
        state.worker.finish()
    state.now = NOW + 20 * SECOND
    state.gateway.poll(state.now)
    assert [(r.request_id, r.kind) for r in state.receipts] == [(original.request_id, "unknown")]
    assert not state.accounts
    if not done_at_timeout:
        assert len(state.gateway.reads) == 1  # Blocks overlapping worker reads.
        state.worker.finish()
        state.now += SECOND
        state.gateway.poll(state.now)
    assert not state.gateway.reads and not state.accounts
    state.now = NOW + 25 * SECOND
    request_account(state)
    snapshot(state, state.now + SECOND)
    state.now += 2 * SECOND
    state.gateway.poll(state.now)
    state.worker.finish()
    state.gateway.poll(state.now)
    assert len(state.accounts) == 1
    assert state.live.flat_accounts_ready(state.now, NOW)


def test_explicit_disconnect_still_invalidates_and_retires_the_request(recovery):
    state = recovery
    original = request_account(state)
    snapshot(state, NOW + 74_000_000, connected=False)
    state.now = NOW + 146_000_000
    state.gateway.poll(state.now)
    assert [(r.request_id, r.kind) for r in state.receipts] == [(original.request_id, "unknown")]
    assert state.live.account_pauses[state.venue]["reason"] == "read_failed"
    assert not state.gateway.native_reads and not state.accounts
    snapshot(state, NOW + 74_000_000)
    state.now = NOW + 200_000_000
    state.gateway.poll(state.now)
    assert not state.accounts and not state.worker.jobs


def test_invalid_native_evidence_fails_without_reusing_the_old_request(recovery):
    state = recovery
    original = request_account(state)
    snapshot(state, NOW + 74_000_000)

    def invalid_capture(*args, **kwargs):
        raise AccountReadError("native account evidence identity/environment mismatch")

    state.gateway.reader.capture_native_evidence = invalid_capture
    state.now = NOW + 146_000_000
    state.gateway.poll(state.now)
    assert [(r.request_id, r.kind) for r in state.receipts] == [(original.request_id, "unknown")]
    assert state.live.account_pauses[state.venue]["reason"] == "read_failed"
    assert not state.gateway.native_reads and not state.accounts and not state.worker.jobs
    state.gateway.poll(state.now + SECOND)
    assert len(state.receipts) == 1


def test_snapshot_completed_after_poll_time_waits_without_restarting_read(recovery):
    state = recovery
    original = request_account(state)
    started = NOW + 74_000_000
    snapshot(state, started)
    state.now = NOW + 146_000_000
    completed = state.now + 1_000_000
    state.snapshot["snapshot_completed_ns"] = completed
    state.gateway.poll(state.now)
    assert not state.receipts and not state.accounts and not state.worker.jobs
    assert state.live.account_request_ids[state.instrument] == original.request_id
    state.now = completed + 50_000_000
    state.gateway.poll(state.now)
    state.worker.finish()
    state.gateway.poll(state.now)
    assert state.accounts[0].observed_ns == started
    assert state.captured["snapshot_completed_ns"] == completed
    assert state.live.flat_accounts_ready(state.now, NOW)
    assert not state.receipts and not state.live.drain_intents()


def test_persistent_future_snapshot_still_expires_original_account_request(recovery):
    state = recovery
    original = request_account(state)
    snapshot(state, NOW + SECOND)
    state.snapshot["snapshot_completed_ns"] = NOW + 60 * SECOND
    state.now = NOW + 2 * SECOND
    state.gateway.poll(state.now)
    assert not state.worker.jobs and not state.receipts
    state.now = NOW + 20 * SECOND
    state.gateway.poll(state.now)
    assert [(r.request_id, r.kind) for r in state.receipts] == [(original.request_id, "unknown")]
    assert not state.accounts and not state.worker.jobs and not state.gateway.native_reads


def test_multi_venue_account_reads_do_not_head_of_line_block():
    import threading
    from opportunity_preflight import OrderIntent
    hl_started = threading.Event()
    hl_release = threading.Event()
    accounts = []
    aster_snapshot = AccountSnapshot("ASTER", "BTCUSDT-PERP.ASTER", None, None, NOW, D(0),
                                     account_positions=(), account_open_orders=())
    hl_snapshot = AccountSnapshot("HL", "BTC-USD-PERP.HYPERLIQUID", None, None, NOW, D(0),
                                  account_positions=(), account_open_orders=())

    def read(market, bid, ask, now):
        if market.venue == "HL":
            hl_started.set()
            assert hl_release.wait(3)
            return hl_snapshot
        return aster_snapshot

    markets = [NS(venue="HL", instrument_id="BTC-USD-PERP.HYPERLIQUID"),
               NS(venue="ASTER", instrument_id="BTCUSDT-PERP.ASTER")]
    gateway = NativeOrderGateway(FakeStrategy(), markets, lambda _: None, accounts.append,
                                 lambda _: None, reader=NS(read=read), model=MODEL)
    try:
        hl_intent = OrderIntent("reconcile", "HL", "BTC-USD-PERP.HYPERLIQUID",
                                "req-hl", "", account_check=True)
        aster_intent = OrderIntent("reconcile", "ASTER", "BTCUSDT-PERP.ASTER",
                                   "req-aster", "", account_check=True)
        gateway.dispatch(hl_intent, NOW)
        assert hl_started.wait(2)
        gateway.dispatch(aster_intent, NOW)
        assert gateway.reads["req-aster"].result(timeout=2) == aster_snapshot
        gateway.poll(NOW + 1)
        assert accounts == [aster_snapshot]
        assert set(gateway.workers) == {"HYPERLIQUID", "ASTER"}
        hl_release.set()
        assert gateway.reads["req-hl"].result(timeout=2) == hl_snapshot
        gateway.poll(NOW + 2)
        assert accounts == [aster_snapshot, hl_snapshot]
    finally:
        hl_release.set()
        gateway.close()
    assert gateway.workers and all(worker._shutdown for worker in gateway.workers.values())

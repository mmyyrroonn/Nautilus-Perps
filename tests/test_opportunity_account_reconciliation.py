"""Account-domain admission and asynchronous continuous reconciliation (offline)."""
from dataclasses import replace
from decimal import Decimal as D
from threading import Event

import pytest

from test_opportunity_live import accounts, fill, open_pair, receipt, state, submissions
from test_opportunity_trade import NOW, SECOND, make_plan, publish, set_book
from opportunity_preflight import AccountSnapshot, ExecutionReceipt
from opportunity_trade import TradePolicy
from opportunity_trade_store import TradeStore


def observation(live, instrument, now, **changes):
    value = live.accounts[instrument]
    return replace(value, observed_ns=now, **changes)


@pytest.mark.parametrize("problem", ["position", "order", "authority", "disconnect", "future", "stale"])
def test_btc_account_failure_blocks_eth_on_the_same_account(problem):
    live, events = state(make_plan(symbols=("BTC", "ETH")))
    now = NOW + SECOND
    changes = {"position": {"position_quantity": D(1)}, "order": {"open_order_ids": ("foreign",)},
               "authority": {"authoritative": False}, "disconnect": {"connected": False},
               "future": {"observed_ns": now + 1}, "stale": {"observed_ns": now - 6 * SECOND}}[problem]
    live.on_account(replace(live.accounts["BTC.HL"], **({"observed_ns": now} | changes)), now)
    # A delayed stale successful read cannot replace the newer cached proof;
    # expiration is detected even when no BTC book is evaluated.
    if problem == "stale":
        now = NOW + 6 * SECOND
    publish(live, now, symbol="ETH")
    assert not submissions(live)
    assert "HL" in live.account_pauses
    assert any(e["event"] == "account_domain_paused" for e in events)


def test_healthy_disjoint_venue_pair_can_still_enter():
    live, _ = state(make_plan(symbols=("BTC", "ETH"), venues=("HL", "ASTER", "LIGHTER")))
    now = NOW + SECOND
    live.on_account(observation(live, "BTC.HL", now, position_quantity=D(1)), now)
    set_book(live, "ASTER", "99.99", "100", now, symbol="ETH")
    set_book(live, "LIGHTER", "100.3", "100.31", now, symbol="ETH")
    live.evaluate_symbol("ETH", now)
    orders = submissions(live)
    assert len(orders) == 2 and {i.venue for i in orders} == {"ASTER", "LIGHTER"}


def test_resume_needs_post_failure_proof_for_every_market_and_survives_restart(tmp_path):
    store = TradeStore(tmp_path / "ledger.sqlite3", "fixture")
    try:
        live, events = state(make_plan(symbols=("BTC", "ETH")), store=store)
        failed = NOW + SECOND
        live.on_account(observation(live, "BTC.HL", failed, position_quantity=D(1)), failed)
        saved = store.load()
        assert saved["account_pauses"]["HL"]["recovery_after_ns"] == failed
        resumed, _ = state(make_plan(symbols=("BTC", "ETH")), store=store)
        assert "HL" in resumed.account_pauses  # Process-local proof was not restored.
        for instrument in ("BTC.HL", "ETH.HL"):
            resumed.on_account(observation(resumed, instrument, failed), failed + 1)
        assert "HL" in resumed.account_pauses  # Same-start read cannot clear the latch.
        resumed.on_account(observation(resumed, "BTC.HL", failed + 2), failed + 2)
        assert "HL" in resumed.account_pauses
        resumed.on_account(observation(resumed, "ETH.HL", failed + 2), failed + 2)
        assert "HL" not in resumed.account_pauses
        publish(resumed, failed + 2, symbol="ETH")
        assert len(submissions(resumed)) == 2
    finally:
        store.close()


def test_pending_original_order_prevents_recovery_even_with_clean_account_response():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    publish(live)
    original = submissions(live)
    now = NOW + SECOND
    live.on_account(observation(live, "ETH.HL", now, open_order_ids=("foreign",)), now)
    accounts(live, now + 1)
    assert "HL" in live.account_pauses
    publish(live, now + 1, symbol="ETH")
    assert not submissions(live)
    assert set(live.orders) == {i.client_order_id for i in original}
    for intent in original:
        live.on_receipt(receipt(intent, "terminal"), now + 2)
    accounts(live, now + 3)
    assert "HL" not in live.account_pauses


def test_foreign_order_cannot_be_explained_by_an_owned_id_on_another_instrument():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    publish(live)
    original = submissions(live)
    cid = next(i.client_order_id for i in original if i.venue == "HL")
    live.on_account(observation(live, "ETH.HL", NOW + 1, open_order_ids=(cid,)), NOW + 1)
    assert live.account_pauses["HL"]["reason"] == "unowned_order"


def test_account_pause_retires_both_prepared_legs_before_dispatch():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    publish(live)
    assert len(live.orders) == 2
    now = NOW + 1
    live.on_account(observation(live, "ETH.HL", now, position_quantity=D(1)), now)
    assert not submissions(live)
    assert all(o["status"] == "local_not_dispatched" for o in live.orders.values())
    assert len(live.positions) == 1  # Budget remains reserved pending flat proof.


@pytest.mark.parametrize("failure_after_first_leg", [False, True])
def test_synchronous_account_failure_blocks_remaining_entries_in_drained_batch(failure_after_first_leg):
    from opportunity_live_runner import LiveCoordinator
    from types import SimpleNamespace as NS
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    now = NOW + SECOND
    live._request_account("BTC.HL", now)
    publish(live, now, symbol="ETH")
    original = set(live.orders)
    sent = []
    coordinator = LiveCoordinator.__new__(LiveCoordinator)
    coordinator.live = live
    coordinator.proxy = NS(restart_pending=False)
    def dispatch(intent, timestamp):
        if intent.action == "submit":
            sent.append(intent)
            if failure_after_first_leg:
                live.on_account(observation(live, "BTC.HL", now, position_quantity=D(1)), now)
        elif not failure_after_first_leg:
            live.on_receipt(receipt(intent, "rejected"), now)
    coordinator.gateway = NS(dispatch=dispatch)
    coordinator._live_intents(now)
    assert len(sent) == int(failure_after_first_leg)
    assert sum(o["status"] == "local_not_dispatched" for o in live.orders.values()) == 2 - len(sent)
    assert set(live.orders) == original and len(live.positions) == 1
    if sent:
        fill(live, sent[0], now + 1)
        assert live.expected_native(sent[0].instrument_id) != 0


def test_pause_does_not_disable_confirmed_exit_on_other_symbol():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    position, _ = open_pair(live, symbol="ETH")
    now = NOW + SECOND
    accounts(live, now)
    live.on_account(observation(live, "BTC.HL", now, position_quantity=D(1)), now)
    live.stop_entries(now)
    publish(live, now, symbol="ETH")
    orders = submissions(live)
    assert len(orders) == 2 and all(live.orders[i.client_order_id]["purpose"] == "exit" for i in orders)
    assert "HL" in live.account_pauses


def test_timer_checks_dormant_markets_without_trading_or_waiting():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    live.on_evaluation_timer(NOW + live.account_max_age_ns // 2)
    reads = live.drain_intents()
    assert {i.instrument_id for i in reads} == set(live.markets)
    assert all(i.action == "reconcile" for i in reads)
    live.on_evaluation_timer(NOW + live.account_max_age_ns // 2 + 1)
    assert live.drain_intents() == ()  # Bounded outstanding reads.
    live.on_evaluation_timer(NOW + live.account_max_age_ns + 1)
    assert set(live.account_pauses) == {"HL", "ASTER"}
    assert not live.orders


@pytest.mark.parametrize("outcome", ["unknown", "rejected"])
def test_failed_async_read_invalidates_a_previously_healthy_domain(outcome):
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    now = NOW + 3 * SECOND
    live.on_evaluation_timer(now)
    read = next(i for i in live.drain_intents() if i.instrument_id == "BTC.HL")
    live.on_receipt(ExecutionReceipt(read.request_id, "", outcome), now)
    assert "BTC.HL" not in live.accounts
    publish(live, now, symbol="ETH")
    assert not submissions(live) and live.account_pauses["HL"]["reason"] == "read_failed"


@pytest.mark.parametrize("problem", ["outside_position", "outside_order", "missing_owned_position"])
def test_complete_account_coverage_is_checked_against_all_pair_inventory(problem):
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    now = NOW + SECOND
    positions, orders = (), ()
    if problem == "outside_position":
        positions = (("SOL.HL", D(1)),)
    elif problem == "outside_order":
        orders = (("SOL.HL", "external"),)
    else:
        open_pair(live, symbol="ETH")
    live.on_account(observation(live, "BTC.HL", now,
        account_positions=positions, account_open_orders=orders), now)
    assert "HL" in live.account_pauses


def test_whole_account_proof_sums_shared_pair_inventory_without_netting_owners_away():
    live, _ = state(make_plan(venues=("HL", "ASTER", "LIGHTER")),
                    policy=TradePolicy(take_profit_bps=D(1000), max_loss_bps=D(1000)))
    open_pair(live)
    now = NOW + SECOND
    accounts(live, now)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "100.1", "100.11", now)
    set_book(live, "LIGHTER", "100.4", "100.41", now)
    live.evaluate_symbol("BTC", now)
    original = submissions(live)
    assert len(original) == 2
    for order in original:
        fill(live, order, now + 1)
    accounts(live, now + 3)
    quantity = D(str(live.accounts["BTC.HL"].position_quantity))
    assert quantity > 1
    live.on_account(observation(live, "BTC.HL", now + 4,
        account_positions=(("BTC.HL", quantity),), account_open_orders=()), now + 4)
    assert "HL" not in live.account_pauses


def test_late_success_cannot_replace_newer_negative_authority():
    live, _ = state()
    original = live.accounts["BTC.HL"]
    live.on_account(observation(live, "BTC.HL", NOW + 10, authoritative=False), NOW + 10)
    live.on_account(replace(original, observed_ns=NOW + 9), NOW + 11)
    publish(live, NOW + 11)
    assert not submissions(live) and "HL" in live.account_pauses
    assert not live._account_ready("BTC.HL", NOW + 11)


def test_bad_future_timestamp_cannot_poison_later_valid_recovery():
    live, _ = state()
    original = live.accounts["BTC.HL"]
    live.on_account(replace(original, observed_ns=NOW + 1000 * SECOND), NOW + 1)
    assert "BTC.HL" not in live.accounts
    live.on_account(replace(original, observed_ns=NOW + 2), NOW + 2)
    assert "HL" not in live.account_pauses
    publish(live, NOW + 2)
    assert len(submissions(live)) == 2


def test_contradictory_target_and_complete_coverage_cannot_recover():
    live, _ = state()
    now = NOW + 1
    live.on_account(observation(live, "BTC.HL", now, position_quantity=D(1),
        account_positions=(), account_open_orders=()), now)
    assert live.account_pauses["HL"]["reason"] == "inconsistent_coverage"


def test_blocking_account_worker_does_not_block_poll_or_original_id_queries():
    from test_opportunity_native_execution import FakeStrategy, MODEL, intent
    from types import SimpleNamespace as NS
    from opportunity_native_execution import NativeOrderGateway
    started, release = Event(), Event()
    def read(*args):
        started.set()
        assert release.wait(5)
        return AccountSnapshot("HL", "BTC-USD-PERP.HYPERLIQUID", D(100), D(101), SECOND, D(0))
    strategy, receipts = FakeStrategy(), []
    gateway = NativeOrderGateway(strategy, [NS(venue="HL", instrument_id="BTC-USD-PERP.HYPERLIQUID")],
        receipts.append, lambda snapshot: None, lambda fill: None, model=MODEL, reader=NS(read=read))
    try:
        gateway.dispatch(intent(), SECOND)
        gateway.dispatch(intent(action="reconcile", request_id="read1", client_order_id="", account_check=True), SECOND)
        assert started.wait(1)
        gateway.poll(12 * SECOND)
        gateway.dispatch(intent(action="reconcile", request_id="read2", client_order_id="", account_check=True), 12 * SECOND)
        assert len(gateway.reads) == 1
        assert any(call[0:2] == ("query", "owned-1") for call in strategy.calls)
        assert any(r.request_id == "read2" and r.kind == "unknown" for r in receipts)
    finally:
        release.set()
        gateway.close()


def test_whole_account_checks_coalesce_all_symbols_and_do_not_copy_quotes():
    live, _ = state(make_plan(symbols=("0G", "BTC", "ETH"), venues=("HL", "ASTER", "LIGHTER")))
    live.accounts.clear()
    now = NOW + SECOND
    live.reconcile_accounts(now)
    requests = live.drain_intents()
    assert len(requests) == 3 and all(i.instrument_id.startswith("BTC.") for i in requests)
    for intent in requests:
        live.on_account(AccountSnapshot(intent.venue, intent.instrument_id, None, None, now + 1, D(0),
                                       account_positions=(), account_open_orders=()), now + 1)
    assert set(live.accounts) == set(live.markets)
    assert all(a.bid is None and a.ask is None and a.book_observed_ns is None for a in live.accounts.values())
    assert not live.account_pauses
    assert live.flat_accounts_ready(now + 1, now)
    assert not live.drain_intents()
    live.reconcile_accounts(now + live.account_max_age_ns)
    assert len(live.drain_intents()) == 3


def test_full_account_read_keeps_unconfigured_risk_and_pending_original_orders():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    live.accounts.clear()
    now = NOW + SECOND
    live.on_account(AccountSnapshot("HL", "BTC.HL", None, None, now, D(0),
        account_positions=(("OUTSIDE.HL", D(1)),), account_open_orders=()), now)
    assert live.account_pauses["HL"]["reason"] == "position_mismatch"
    assert not live.flat_accounts_ready(now, now)
    live.on_account(AccountSnapshot("HL", "BTC.HL", None, None, now + 1, D(0),
        account_positions=(), account_open_orders=()), now + 1)
    assert "HL" not in live.account_pauses


def test_full_hyperliquid_snapshot_does_not_cover_another_dex():
    live, _ = state(make_plan(symbols=("BTC", "xyz:ABC")))
    live.accounts.clear()
    now = NOW + SECOND
    live.on_account(AccountSnapshot("HL", "BTC.HL", None, None, now, D(0),
                                   account_positions=(), account_open_orders=()), now)
    assert "xyz:ABC.HL" not in live.accounts
    live.flat_accounts_ready(now, now)
    assert any(i.instrument_id == "xyz:ABC.HL" for i in live.drain_intents())


def test_account_failures_back_off_per_account_and_reset_after_recovery():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    live.accounts.clear()
    now = NOW + SECOND
    for delay in (5, 10, 20, 40, 60, 60):
        live._request_account("ETH.HL", now)
        request, = live.drain_intents()
        assert request.instrument_id == "BTC.HL"
        live.on_receipt(ExecutionReceipt(request.request_id, "", "unknown"), now)
        live._request_account("ETH.HL", now + delay * SECOND - 1)
        assert not live.drain_intents()
        now += delay * SECOND
    live.on_account(AccountSnapshot("HL", "BTC.HL", None, None, now, D(0),
                                   account_positions=(), account_open_orders=()), now)
    assert ("HL", "") not in live.account_read_failures
    assert ("HL", "") not in live.account_retry_after_ns
    live._request_account("ETH.HL", now + SECOND)
    request, = live.drain_intents()
    live.on_receipt(ExecutionReceipt(request.request_id, "", "unknown"), now + SECOND)
    assert live.account_retry_after_ns[("HL", "")] == now + 6 * SECOND


def test_rate_limit_wait_blocks_all_symbols_but_leaves_other_venue_available():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    live.accounts.clear()
    now = NOW + SECOND
    live._request_account("BTC.HL", now)
    request, = live.drain_intents()
    live.on_receipt(ExecutionReceipt(request.request_id, "", "unknown", retry_after_secs=45), now)
    for instrument in ("BTC.HL", "ETH.HL"):
        live._request_account(instrument, now + 45 * SECOND - 1)
    assert not live.drain_intents()
    live._request_account("ETH.ASTER", now + 5 * SECOND)
    other, = live.drain_intents()
    assert other.venue == "ASTER"
    live._request_account("ETH.HL", now + 45 * SECOND)
    retry, = live.drain_intents()
    assert retry.instrument_id == "BTC.HL"

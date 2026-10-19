"""Standalone entry valuation and evidence-driven exits without live connectivity."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
from types import SimpleNamespace as NS

import pytest

from test_opportunity_live import accounts, fill, receipt, state, submissions
from test_opportunity_live_runner import configs
from test_opportunity_trade import NOW, SECOND, make_plan, publish, set_book
from opportunity_core import BookSnapshot, exact_decimal
from opportunity_entry_evidence import entry_dispatch_evidence
from opportunity_live_runner import LiveCoordinator, load_live_plan
from opportunity_preflight import AccountSnapshot


def coordinator_callback(live):
    coordinator = object.__new__(LiveCoordinator)
    coordinator.live, coordinator.phase = live, "active"
    sent = []
    coordinator.proxy = NS(restart_pending=False)
    coordinator.gateway = NS(dispatch=lambda intent, now, **kwargs: sent.append(intent))
    return coordinator, sent


def proof(live, instrument, observed):
    market = live.markets[instrument]
    positions = tuple((i, exact_decimal(live.expected_native(i)))
                      for i in live._account_coverage(live.accounts[instrument]))
    return AccountSnapshot(market.venue, instrument, None, None, observed,
        exact_decimal(live.expected_native(instrument)),
        account_positions=positions, account_open_orders=())


@pytest.mark.parametrize("leg", ["buy", "sell"])
def test_each_leg_can_fill_alone_below_conservative_cap_with_ask_headroom(leg):
    live, _ = state()
    publish(live, sell_ask="101")
    entries = submissions(live)
    assert {entry.quantity for entry in entries} == {D("0.98")}
    position = next(iter(live.positions.values()))
    risk = position["entry"]["entry_risk"]
    assert F(risk["per_leg_budget_usd"]) == F("99.9")
    entry = entries[0 if leg == "buy" else 1]
    fill(live, entry, NOW + 1)
    assert F(position["unhedged_notional_usd"]) < 100
    assert "unhedged_breach" not in position
    assert F(risk[leg + "_standalone_usd"]) <= F("99.9")


def test_common_lot_and_rounded_limit_price_both_bound_sizing():
    live, _ = state(price_increment="0.1")
    for instrument in live.metadata:
        live.metadata[instrument] = replace(live.metadata[instrument], size_increment=D("0.03"))
    publish(live, buy_ask="100.01", sell_bid="100.6", sell_ask="100.61")
    entries = submissions(live)
    assert {entry.quantity for entry in entries} == {D("0.99")}
    assert entries[0].price == D("100.1")
    assert all(entry.quantity % D("0.03") == 0 for entry in entries)
    risk = next(iter(live.positions.values()))["entry"]["entry_risk"]
    assert F(risk["buy_standalone_usd"]) == F("99.099")
    assert F(risk["sell_standalone_usd"]) == F("99.6039")


def test_pons_short_cash_below_cap_cannot_hide_ask_valuation_above_cap():
    live, _ = state(price_increment="0.00001")
    for instrument in live.metadata:
        live.metadata[instrument] = replace(live.metadata[instrument], size_increment=D(1))
    set_book(live, "HL", "0.25899", "0.25900", bid_size="10000", ask_size="10000")
    set_book(live, "ASTER", "0.26077", "0.26112", bid_size="10000", ask_size="10000")
    live.evaluate_symbol("BTC", NOW)
    entries = submissions(live)
    assert D(383) * D("0.26077") == D("99.87491")
    assert D(383) * D("0.26112") == D("100.00896")
    assert {entry.quantity for entry in entries} == {D(382)}
    assert D(382) * D("0.26112") < D("99.9")


def test_native_canonical_and_quote_conversions_match_sizing_and_send_headroom():
    plan = make_plan()
    plan = replace(plan, markets=tuple(replace(m,
        canonical_multiplier=D("3") if m.venue == "HL" else D("0.2"),
        quote_to_usd=D("0.5") if m.venue == "HL" else D("2")) for m in plan.markets))
    live, _ = state(plan)
    for instrument, native, canonical in (("BTC.HL", "2", "3"), ("BTC.ASTER", "5", "0.2")):
        live.register_instrument(instrument, D("0.01"), D(native), "USD")
        live.metadata[instrument] = replace(live.metadata[instrument], multiplier=D(native) * D(canonical))
    publish(live)
    buy, sell = submissions(live)
    position = next(iter(live.positions.values()))
    assert (buy.quantity, buy.price, sell.quantity, sell.price) == (D("0.16"), D(600), D("0.96"), D("10.03"))
    assert F(position["entry"]["sizing"]["base_quantity"]) == F("0.96")
    reason, evidence = entry_dispatch_evidence(live, position, NOW + 1)
    assert reason == "valid"
    risk = position["entry"]["entry_risk"]
    for leg, expected in (("buy", "96"), ("sell", "96.2976")):
        assert F(risk[leg + "_standalone_usd"]) == F(evidence[leg]["standalone_risk_usd"]) == F(expected)
    assert F(evidence["sell"]["limit_cash_usd"]) == F("96.288")
    set_book(live, "ASTER", "100.3", "105", NOW + 2)
    reason, evidence = entry_dispatch_evidence(live, position, NOW + 2)
    assert reason == "unhedged_headroom_insufficient"
    assert F(evidence["sell"]["standalone_risk_usd"]) == F("100.8")
    fill(live, sell, NOW + 3)
    assert F(position["remaining_sell_base"]) == F("0.96")
    assert F(position["actual_entry"]["sell_notional_usd"]) == F("96.288")
    assert F(position["unhedged_notional_usd"]) == F("100.8")
    assert position["unhedged_breach"]["reason"] == "unhedged_notional"


def test_headroom_shrink_across_depth_levels_recomputes_limits_fees_and_net():
    live, _ = state(make_plan(reserve="1"))
    for instrument, fee in (("BTC.HL", "2"), ("BTC.ASTER", "3")):
        live.metadata[instrument] = replace(live.metadata[instrument], taker_fee_bps=D(fee))
    for instrument, book in (("BTC.HL", BookSnapshot("HL", "BTC",
            ((D("99.99"), D(10)),), ((D(100), D("0.8")), (D("100.1"), D(10))), NOW, NOW)),
            ("BTC.ASTER", BookSnapshot("ASTER", "BTC",
            ((D(101), D("0.8")), (D("100.9"), D(10))), ((D(130), D(10)),), NOW, NOW))):
        live.update_book(instrument, book, NOW, evaluate=False)
    initial = live.pricer.entry("BTC.HL", "BTC.ASTER", NOW)
    original_quantity = F(initial["sizing"]["base_quantity"])
    assert original_quantity > F("0.8")
    assert live._limit("BTC.HL", "BUY", original_quantity, NOW)["price"] == D("100.1")
    assert live._limit("BTC.ASTER", "SELL", original_quantity, NOW)["price"] == D("100.9")
    assert live._entry("BTC.HL", "BTC.ASTER", NOW)
    buy, sell = submissions(live)
    assert (buy.quantity, sell.quantity, buy.price, sell.price) == (D("0.76"), D("0.76"), D(100), D(101))
    position = next(iter(live.positions.values()))
    sizing, economics = position["entry"]["sizing"], position["entry"]["economics"]
    assert F(sizing["buy_notional"]) == 76 and F(sizing["sell_notional"]) == F("76.76")
    assert F(economics["buy_taker_fee"]) == F("0.0152")
    assert F(economics["sell_taker_fee"]) == F("0.023028")
    assert F(economics["reserve"]) == F("0.0076")
    assert F(economics["entry_after_fees_and_reserve"]) == F("0.714172")
    reason, evidence = entry_dispatch_evidence(live, position, NOW + 1)
    assert reason == "valid"
    assert F(evidence["net_usd"]) == F(economics["entry_after_fees_and_reserve"])


@pytest.mark.parametrize("ask,breach", [("199.999999", False), ("200", True), ("200.000001", True)])
def test_valuation_below_equal_and_above_hard_cap_still_protects(ask, breach):
    live, _ = state()
    publish(live)
    buy, _ = submissions(live)
    fill(live, buy, NOW + 1, quantity="0.5")
    position = next(iter(live.positions.values()))
    set_book(live, "HL", D(ask) - D("0.01"), ask, NOW + 2)
    live._update_unhedged(position, NOW + 2)
    assert bool(position.get("unhedged_breach")) is breach
    assert live.max_unhedged_notional == 100


@pytest.mark.parametrize("reserve", ["0", "9.99", "10000", "NaN", True])
def test_entry_reserve_cannot_be_disabled_or_invalid(reserve):
    with pytest.raises(ValueError):
        state(entry_unhedged_reserve_bps=reserve)


def test_configured_reserve_can_only_increase_limits_floor(tmp_path):
    path = configs(tmp_path)
    baseline = load_live_plan(path)
    path.write_text(path.read_text().replace("[startup]", 'entry_unhedged_reserve_bps = "15"\n[startup]'))
    limits = tmp_path / "limits.toml"
    limits.write_text(limits.read_text() + 'entry_unhedged_reserve_bps = "25"\n')
    plan = load_live_plan(path)
    assert plan.entry_unhedged_reserve_bps == 25
    assert plan.fingerprint() != baseline.fingerprint()
    assert plan.max_unhedged_notional_usd == 100


def test_account_queries_notify_dispatch_and_slow_cross_fill_reads_fail_closed():
    live, events = state()
    wakeups = []
    live.intent_notifier = lambda: wakeups.append(True)
    publish(live)
    buy, sell = submissions(live)
    wakeups.clear()
    fill(live, buy, NOW + SECOND // 10, quantity="0.4")
    live.on_receipt(receipt(sell, "rejected"), NOW + SECOND // 10 + 2)
    assert wakeups  # Account read enters the same immediate dispatch wakeup path.
    requests = live.drain_intents()
    assert any(i.action == "reconcile" for i in requests)
    coordinator, sent = coordinator_callback(live)
    now = NOW + 2 * SECOND
    set_book(live, "HL", "99.99", "100", now)
    # The read began before the fill/terminal fence, even though it completed later.
    old = proof(live, buy.instrument_id, NOW + SECOND // 10)
    coordinator.on_account(old, now)
    assert not any(i.action == "submit" for i in sent)
    rejected = [e for e in events if e["event"] == "account_read_completed" and not e["accepted"]]
    assert rejected[-1]["reason"] == "predates_activity"
    assert rejected[-1]["request_to_complete_ms"] == "1900"
    assert any(i.action == "reconcile" for i in sent)
    sent.clear()
    fresh = proof(live, buy.instrument_id, now + 1)
    coordinator.on_account(fresh, now + 2)
    exits = [i for i in sent if i.action == "submit"]
    assert len(exits) == 1 and exits[0].quantity == D("0.4") and exits[0].reduce_only
    sent.clear()
    coordinator.on_account(fresh, now + 3)
    assert not any(i.action == "submit" for i in sent)
    assert len(next(iter(live.positions.values()))["exit_ids"]) == 1


@pytest.mark.parametrize("problem", ["foreign_position", "foreign_order", "incomplete", "disconnected"])
def test_account_completion_wakeup_retains_complete_ownership_barriers(problem):
    live, _ = state()
    publish(live)
    buy, sell = submissions(live)
    fill(live, buy, NOW + 1, quantity="0.4")
    live.on_receipt(receipt(sell, "rejected"), NOW + 2)
    live.drain_intents()
    now = NOW + SECOND
    set_book(live, "HL", "99.99", "100", now)
    snapshot = proof(live, buy.instrument_id, now)
    if problem == "foreign_position":
        snapshot = replace(snapshot, account_positions=snapshot.account_positions + (("foreign.HL", D(1)),))
    elif problem == "foreign_order":
        snapshot = replace(snapshot, account_open_orders=(("foreign.HL", "external"),))
    elif problem == "incomplete":
        snapshot = replace(snapshot, authoritative=False)
    else:
        snapshot = replace(snapshot, connected=False)
    coordinator, sent = coordinator_callback(live)
    coordinator.on_account(snapshot, now)
    assert not any(i.action == "submit" for i in sent)


def test_retry_wait_is_recorded_without_shortening_one_second_interval():
    live, events = state()
    publish(live)
    buy, sell = submissions(live)
    fill(live, buy, NOW + 1, quantity="0.4")
    live.on_receipt(receipt(sell, "rejected"), NOW + 2)
    accounts(live, NOW + 3)
    position = next(iter(live.positions.values()))
    live.observe(next(iter(live.positions)), NOW + 4)
    waits = [e for e in events if e["event"] == "exit_wait"]
    assert waits[-1]["reason"] == "order_retry_throttled"
    assert waits[-1]["retry_at_ns"] == NOW + SECOND
    before = len(waits)
    live.observe(next(iter(live.positions)), NOW + 5)
    assert sum(e["event"] == "exit_wait" for e in events) == before
    assert live.retry_ns == SECOND


def test_event_woken_exit_books_duplicate_fills_fees_and_pnl_once():
    live, events = state()
    publish(live)
    buy, sell = submissions(live)
    fill(live, buy, NOW + 1, quantity="0.4", fee="0.02")
    live.on_receipt(receipt(sell, "rejected"), NOW + 2)
    live.drain_intents()
    coordinator, sent = coordinator_callback(live)
    now = NOW + SECOND
    set_book(live, "HL", "99.99", "100", now)
    coordinator.on_account(proof(live, buy.instrument_id, now), now)
    exit_order, = [i for i in sent if i.action == "submit"]
    payload = fill(live, exit_order, now + 1, fee="0.01")
    live.on_fill(payload, now + 3)
    for instrument in (buy.instrument_id, sell.instrument_id):
        coordinator.on_account(proof(live, instrument, now + 4), now + 4)
    assert not live.positions and live.completed == 1
    assert live.realized_net == F("-0.034")
    dispatched = [e for e in events if e["event"] == "exit_python_dispatch"]
    assert len(dispatched) == 1 and dispatched[0]["client_order_id"] == exit_order.client_order_id
    assert F(dispatched[0]["prepared_to_dispatch_ms"]) >= 0
    stages = [e["stage"] for e in events if e["event"] == "exit_stage"]
    assert stages.count("reduction_fill_buy") == 1
    assert {"first_unbalanced", "entries_terminal", "account_accepted_buy",
            "reduction_prepared_buy", "reduction_fill_buy", "inventory_flat"} <= set(stages)

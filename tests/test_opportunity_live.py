"""Offline live ownership, fill accounting, fences and execution admission."""
from copy import deepcopy
from decimal import Decimal as D
from fractions import Fraction as F
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from opportunity_core import MarketMetadata
from opportunity_live import LiveTradeState
from opportunity_preflight import AccountSnapshot, ExecutionReceipt
from opportunity_trade import TradePolicy
from test_opportunity_trade import NOW, SECOND, make_plan, publish, set_book


class MemoryStore:
    def __init__(self, saved=None):
        self.saved, self.writes = deepcopy(saved), []
    def load(self):
        return deepcopy(self.saved)
    def save(self, snapshot, events):
        self.saved = deepcopy(snapshot)
        self.writes.append((deepcopy(snapshot), deepcopy(events)))


def state(plan=None, policy=None, *, max_pairs=2, gross="500", store=None, activate=True,
          price_increment="0.01", **risk_kwargs):
    events = []
    result = LiveTradeState(plan or make_plan(), policy or TradePolicy(), max_pairs=max_pairs,
        max_gross_notional_usd=D(gross), store=store, event_sink=events.append, **risk_kwargs)
    for market in result.plan.markets:
        result.register_instrument(market.instrument_id, D(price_increment), D(1), "USD")
        result.update_metadata(market.instrument_id, MarketMetadata(market.instrument_id, D("0.01"),
            taker_fee_bps=D(0), fee_source="synthetic"), NOW, evaluate=False)
    accounts(result, NOW)
    if activate:
        assert result.activate(NOW)
    result.drain_intents()
    return result, events


def accounts(live, now, *, observed_ns=None, authoritative=True, connected=True, open_orders=()):
    for market in live.plan.markets:
        live.on_account(AccountSnapshot(market.venue, market.instrument_id, D(100), D("100.01"),
            now if observed_ns is None else observed_ns,
            D(live.expected_native(market.instrument_id).numerator)/D(live.expected_native(market.instrument_id).denominator),
            open_orders, connected=connected, authoritative=authoritative), now)


def submissions(live):
    return tuple(i for i in live.drain_intents() if i.action == "submit")


def receipt(intent, kind):
    return ExecutionReceipt(intent.request_id, intent.client_order_id, kind)


def fill(live, intent, now, *, quantity=None, price=None, fee="0", currency="USD", trade_id=None, terminal=True):
    payload = {"client_order_id": intent.client_order_id, "trade_id": trade_id or intent.client_order_id+"fill",
        "instrument_id": intent.instrument_id, "venue": intent.venue, "side": intent.side,
        "native_qty": intent.quantity if quantity is None else D(quantity),
        "native_price": intent.price if price is None else D(price),
        "commission_amount": None if fee is None else D(fee), "commission_currency": currency}
    live.on_fill(payload, now)
    if terminal:
        live.on_receipt(receipt(intent, "terminal"), now+1)
    return payload


def open_pair(live, *, symbol="BTC", now=NOW):
    publish(live, now, symbol=symbol)
    intents = submissions(live)
    assert len(intents) == 2
    for intent in intents:
        fill(live, intent, now+100_000_000)
    accounts(live, now+100_000_002)
    live.on_evaluation_timer(now+100_000_002)
    live.drain_intents()
    position = next(p for p in live.positions.values() if p["symbol"] == symbol)
    assert position["status"] == "open"
    return position, intents


def mark(live, bps, age_seconds=1, *, position=None, ask_size="10"):
    position = position or next(iter(live.positions.values()))
    now = position["opened_ns"]+age_seconds*SECOND
    cover = D("100.29")-D(str(bps))/100
    accounts(live, now)
    publish(live, now, sell_bid=cover-D("0.01"), sell_ask=cover, sell_ask_size=ask_size)
    return now, submissions(live)


def complete_exits(live, intents, now):
    for intent in intents:
        fill(live, intent, now+100_000_000)
    accounts(live, now+100_000_002)
    live.on_evaluation_timer(now+100_000_002)
    live.drain_intents()


def test_no_startup_activation_produces_no_order_intents():
    live, _ = state(activate=False)
    publish(live)
    assert live.phase == "waiting_preflight" and not live.positions and not submissions(live)


@pytest.mark.parametrize("bid,qualifies", [("100.299999999999999999", False), ("100.3", True)])
def test_entry_net_thirty_bp_boundary(bid, qualifies):
    live, _ = state()
    publish(live, sell_bid=bid, sell_ask=D(bid)+D("0.01"))
    assert bool(submissions(live)) is qualifies
    assert bool(live.positions) is qualifies


@pytest.mark.parametrize("bid,qualifies", [("100.31", False), ("100.32", True)])
def test_live_entry_deducts_opening_fee_and_reserve_before_thirty_bp(bid, qualifies):
    live, _ = state(make_plan(reserve="1"))
    live.update_metadata("BTC.HL", MarketMetadata("BTC.HL", D("0.01"),
        taker_fee_bps=D(1), fee_source="synthetic"), NOW, evaluate=False)
    publish(live, sell_bid=bid, sell_ask=D(bid)+D("0.01"))
    assert bool(submissions(live)) is qualifies


def test_accepted_orders_reserve_pair_without_creating_inventory():
    live, events = state()
    publish(live)
    intents = submissions(live)
    for intent in intents:
        live.on_receipt(receipt(intent, "accepted"), NOW+1)
    position = next(iter(live.positions.values()))
    assert position["status"] == "entering" and position["opened_ns"] is None
    assert F(position["remaining_buy_base"]) == 0 and F(position["remaining_sell_base"]) == 0
    assert not any(e["event"] == "opened" for e in events)
    assert all(live.expected_native(i.instrument_id) == 0 for i in intents)


def test_prepared_orders_and_pair_lock_are_durable_before_dispatch():
    store = MemoryStore()
    live, _ = state(store=store)
    publish(live)
    snapshot, events = store.writes[-1]
    assert events[-1]["event"] == "orders_prepared"
    assert len(snapshot["positions"]) == 1 and len(snapshot["orders"]) == 2
    assert all(order["status"] == "prepared" for order in snapshot["orders"].values())
    intents = submissions(live)
    assert {i.client_order_id for i in intents} == set(snapshot["orders"])
    assert submissions(live) == ()


def test_failed_prepared_transaction_never_exposes_mutating_dispatch_intents():
    class BrokenStore(MemoryStore):
        def save(self, snapshot, events):
            if any(event["event"] == "orders_prepared" for event in events):
                raise OSError("synthetic durable write failure")
            super().save(snapshot, events)
    live, _ = state(store=BrokenStore())
    with pytest.raises(OSError, match="synthetic durable"):
        publish(live)
    assert not live.drain_intents()
    assert live.halt_reason == "ledger persistence failed"


def test_actual_cash_fees_and_replayed_fill_are_accounted_exactly_once():
    live, _ = state()
    publish(live)
    buy, sell = submissions(live)
    assert buy.side == "BUY" and sell.side == "SELL"
    payload = fill(live, buy, NOW+100_000_000, price="99.99", fee="0.007", trade_id="actual-buy", terminal=False)
    live.on_fill(payload, NOW+100_000_001)
    fill(live, sell, NOW+100_000_002, price="100.31", fee="0.009")
    live.on_receipt(receipt(buy, "terminal"), NOW+100_000_003)
    position = next(iter(live.positions.values()))
    actual = position["actual_entry"]
    assert F(position["remaining_buy_base"]) == F(buy.quantity)
    assert F(actual["buy_notional_usd"]) == F(buy.quantity)*F("99.99")
    assert F(actual["sell_notional_usd"]) == F(sell.quantity)*F("100.31")
    assert F(actual["buy_fee_usd"]) == F("0.007") and F(actual["sell_fee_usd"]) == F("0.009")
    assert len(live.executions) == 2


def test_final_realized_net_includes_every_actual_open_and_close_fee():
    live, _ = state()
    publish(live)
    entries = submissions(live)
    for intent, fee in zip(entries, ("0.01", "0.02")):
        fill(live, intent, NOW+100_000_000, fee=fee)
    accounts(live, NOW+100_000_002)
    live.on_evaluation_timer(NOW+100_000_002)
    live.drain_intents()
    position = next(iter(live.positions.values()))
    now, exits = mark(live, 100)
    assert len(exits) == 2
    for intent, fee in zip(exits, ("0.04", "0.05")):
        fill(live, intent, now+100_000_000, fee=fee)
    accounts(live, now+100_000_002)
    live.on_evaluation_timer(now+100_000_002)
    actual = position["actual_entry"]
    gross = F(actual["sell_notional_usd"])-F(actual["buy_notional_usd"])
    gross += F(position["closing_long_cash_usd"])-F(position["closing_short_cash_usd"])
    assert F(position["closing_fees_usd"]) == F("0.09")
    assert F(live.document()["realized_net_usd"]) == gross-F("0.12")
    assert not live.positions and live.completed == 1


def test_unknown_opening_commission_blocks_soft_profit_but_not_max_hold():
    live, _ = state()
    publish(live)
    for intent in submissions(live):
        fill(live, intent, NOW+100_000_000, fee=None)
    accounts(live, NOW+100_000_002)
    live.on_evaluation_timer(NOW+100_000_002)
    live.drain_intents()
    assert next(iter(live.positions.values()))["unknown_commission"]
    _, soft = mark(live, 100)
    assert not soft
    _, hard = mark(live, 100, 300)
    assert len(hard) == 2 and next(iter(live.positions.values()))["exit_reason"] == "max_hold"


def test_configured_usd_stable_parity_allows_actual_usdc_commission():
    live, _ = state()
    publish(live)
    for intent in submissions(live):
        fill(live, intent, NOW+100_000_000, fee="0.001", currency="USDC")
    position = next(iter(live.positions.values()))
    assert not position["unknown_commission"]
    assert F(position["actual_entry"]["buy_fee_usd"]) == F("0.001")


def test_same_coin_unordered_pair_lock_blocks_duplicate_and_reverse():
    live, _ = state(policy=TradePolicy(take_profit_bps=D(1000), max_loss_bps=D(1000)))
    position, _ = open_pair(live)
    now = position["opened_ns"]+SECOND
    accounts(live, now)
    publish(live, now, buy_bid="100.8", buy_ask="100.81", sell_bid="100", sell_ask="100.01")
    assert not submissions(live)
    assert len(live.positions) == 1 and next(iter(live.positions.values()))["position_id"] == position["position_id"]


def test_other_symbols_can_prepare_positions_concurrently():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    publish(live, symbol="BTC")
    first = submissions(live)
    publish(live, symbol="ETH")
    second = submissions(live)
    assert len(first) == len(second) == 2 and len(live.positions) == 2


@pytest.mark.parametrize("max_pairs,gross", [(2, "500"), (16, "500"), (16, "399")])
def test_pair_and_gross_reservations_cap_concurrent_admission(max_pairs, gross):
    live, _ = state(make_plan(symbols=("BTC", "ETH", "SOL")), max_pairs=max_pairs, gross=gross)
    publish(live, symbol="BTC")
    first = submissions(live)
    publish(live, symbol="ETH")
    second = submissions(live)
    publish(live, symbol="SOL")
    third = submissions(live)
    expected = 1 if D(gross) < 400 else 2
    assert len(live.positions) == expected
    assert len(first) == 2 and not third
    assert bool(second) is (expected == 2)


def test_different_venue_pair_can_share_a_reconciled_leg():
    live, _ = state(make_plan(venues=("HL", "ASTER", "LIGHTER")))
    first, _ = open_pair(live)
    now = first["opened_ns"]+SECOND
    accounts(live, now)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "100.3", "100.31", now)
    set_book(live, "LIGHTER", "100.4", "100.41", now)
    live.evaluate_symbol("BTC", now)
    second = submissions(live)
    assert len(second) == 2 and len(live.positions) == 2
    for intent in second:
        fill(live, intent, now+100_000_000)
    assert live.expected_native("BTC.HL") == F("1.98")


@pytest.mark.parametrize("bps,exits", [("4.999999999999", False), ("5", True)])
def test_net_take_profit_five_bp_boundary(bps, exits):
    live, _ = state()
    open_pair(live)
    _, intents = mark(live, bps)
    assert bool(intents) is exits
    if exits:
        assert len(intents) == 2
        assert next(iter(live.positions.values()))["exit_reason"] == "take_profit"


@pytest.mark.parametrize("bps,age,exits", [("1", 179, False), ("0", 180, False),
    ("0.000001", 180, True), ("-0.000001", 180, False)])
def test_three_minute_exit_requires_strict_positive_net_after_rounding(bps, age, exits):
    live, _ = state(price_increment="0.00000001")
    open_pair(live)
    _, intents = mark(live, bps, age)
    assert bool(intents) is exits
    if exits:
        assert next(iter(live.positions.values()))["exit_reason"] == "positive_after_wait"


@pytest.mark.parametrize("loss_stop,bps,exits", [(True, "-19.999999", False),
    (True, "-20", True), (False, "-20", False), (False, "-50", False)])
def test_loss_twenty_bp_boundary_and_disable(loss_stop, bps, exits):
    live, _ = state(policy=TradePolicy(loss_stop_enabled=loss_stop))
    open_pair(live)
    _, intents = mark(live, bps)
    assert bool(intents) is exits
    if exits:
        assert next(iter(live.positions.values()))["exit_reason"] == "max_loss"


def test_five_minute_partial_reductions_leave_residual_pair_locked():
    live, _ = state(policy=TradePolicy(loss_stop_enabled=False))
    position, _ = open_pair(live)
    now, intents = mark(live, "-1", 300, ask_size="0.01")
    assert len(intents) == 2
    cover = next(i for i in intents if i.side == "BUY")
    sale = next(i for i in intents if i.side == "SELL")
    assert cover.quantity == D("0.01") and sale.quantity == D("0.99")
    complete_exits(live, intents, now)
    assert len(live.positions) == 1
    assert F(position["remaining_buy_base"]) == 0 and F(position["remaining_sell_base"]) == F("0.98")
    later = now+2*SECOND
    accounts(live, later)
    set_book(live, "ASTER", "100.29", "100.30", later)
    live.on_evaluation_timer(later)
    remaining, = submissions(live)
    assert remaining.side == "BUY" and remaining.quantity == D("0.98") and remaining.reduce_only
    complete_exits(live, (remaining,), later)
    assert not live.positions and live.completed == 1


def test_five_minute_reduces_healthy_leg_during_other_venue_book_disconnect():
    live, _ = state(policy=TradePolicy(loss_stop_enabled=False))
    position, _ = open_pair(live)
    deadline = position["opened_ns"]+300*SECOND
    live.invalidate("BTC.HL", deadline)
    accounts(live, deadline)
    set_book(live, "ASTER", "100.3", "100.31", deadline)
    live.on_evaluation_timer(deadline)
    cover, = submissions(live)
    assert cover.instrument_id == "BTC.ASTER" and cover.side == "BUY" and cover.reduce_only
    fill(live, cover, deadline+100_000_000)
    accounts(live, deadline+100_000_002)
    live.on_evaluation_timer(deadline+100_000_002)
    assert len(live.positions) == 1
    assert F(position["remaining_sell_base"]) == 0 and F(position["remaining_buy_base"]) == F("0.99")


def test_one_leg_rejection_unwinds_only_confirmed_other_leg_inventory():
    live, _ = state()
    publish(live)
    buy, sell = submissions(live)
    fill(live, buy, NOW+100_000_000)
    live.on_receipt(receipt(sell, "rejected"), NOW+100_000_001)
    now = NOW+SECOND+100_000_000
    accounts(live, now)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "100.3", "100.31", now)
    live.on_evaluation_timer(now)
    unwind, = submissions(live)
    assert unwind.instrument_id == buy.instrument_id and unwind.side == "SELL"
    assert unwind.quantity == buy.quantity and unwind.reduce_only
    assert next(iter(live.positions.values()))["exit_reason"] == "entry_imbalance"
    complete_exits(live, (unwind,), now)
    assert not live.positions


def test_operator_stop_reduces_confirmed_leg_while_other_submission_is_unknown():
    live, _ = state()
    publish(live)
    buy, sell = submissions(live)
    fill(live, buy, NOW+100_000_000)
    live.on_receipt(receipt(sell, "unknown"), NOW+100_000_002)
    now = NOW+301*SECOND
    accounts(live, now)
    set_book(live, "HL", "99.99", "100", now)
    live.stop_entries(now)
    live.on_evaluation_timer(now)
    unwind, = submissions(live)
    assert unwind.instrument_id == "BTC.HL" and unwind.side == "SELL"
    assert unwind.quantity == buy.quantity and unwind.reduce_only
    assert live.orders[sell.client_order_id]["terminal_ns"] is None
    assert len(live.positions) == 1
    assert set(live.orders) >= {buy.client_order_id, sell.client_order_id}


def test_late_owned_fill_halts_admission_but_permits_reconciled_residual_cleanup():
    live, _ = state(make_plan(symbols=("BTC", "ETH")))
    publish(live)
    buy, sell = submissions(live)
    live.on_receipt(receipt(buy, "rejected"), NOW+1)
    live.on_receipt(receipt(sell, "rejected"), NOW+2)
    fill(live, buy, NOW+100_000_000, terminal=False)
    assert live.halt_reason and live.expected_native("BTC.HL") == F("0.99")
    now = NOW+2*SECOND
    accounts(live, now)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "100.3", "100.31", now)
    live.stop_entries(now)
    live.on_evaluation_timer(now)
    unwind, = submissions(live)
    assert unwind.instrument_id == "BTC.HL" and unwind.side == "SELL" and unwind.reduce_only
    publish(live, now, symbol="ETH")
    assert not submissions(live) and len(live.positions) == 1


def test_forced_close_at_higher_price_clips_each_order_to_one_hundred_usd():
    live, _ = state(policy=TradePolicy(loss_stop_enabled=False))
    position, _ = open_pair(live)
    now = position["opened_ns"]+300*SECOND
    accounts(live, now)
    set_book(live, "HL", "150", "150.01", now)
    set_book(live, "ASTER", "150.02", "150.03", now)
    live.on_evaluation_timer(now)
    intents = submissions(live)
    assert len(intents) == 2
    assert all(0 < intent.quantity*intent.price <= 100 for intent in intents)
    assert all(intent.quantity < D("0.99") for intent in intents)
    complete_exits(live, intents, now)
    assert len(live.positions) == 1
    assert all(F(position[f"remaining_{leg}_base"]) > 0 for leg in ("buy", "sell"))


def test_profitable_soft_exit_uses_equal_chunks_when_full_close_exceeds_order_cap():
    live, _ = state()
    position, _ = open_pair(live)
    now = position["opened_ns"]+SECOND
    accounts(live, now)
    set_book(live, "HL", "149.99", "150", now)
    set_book(live, "ASTER", "149.98", "150", now)
    live.on_evaluation_timer(now)
    intents = submissions(live)
    assert len(intents) == 2
    assert next(iter(live.positions.values()))["exit_reason"] == "take_profit"
    assert all(0 < intent.quantity*intent.price <= 100 for intent in intents)
    assert intents[0].quantity == intents[1].quantity < D("0.99")


def test_unknown_submission_is_never_replaced_with_a_new_client_id():
    live, _ = state()
    publish(live)
    first = submissions(live)
    live.on_receipt(receipt(first[0], "unknown"), NOW+1)
    for seconds in (1, 5, 15):
        now = NOW+seconds*SECOND
        accounts(live, now)
        publish(live, now)
        live.on_evaluation_timer(now)
        assert not submissions(live)
    assert set(live.orders) == {i.client_order_id for i in first}
    assert len(live.positions) == 1


@pytest.mark.parametrize("evidence", ["stale", "non_authoritative", "open_orders"])
def test_unusable_authoritative_account_evidence_blocks_new_orders(evidence):
    live, _ = state()
    now = NOW+6*SECOND
    if evidence == "stale":
        accounts(live, now, observed_ns=NOW)
    elif evidence == "non_authoritative":
        accounts(live, now, authoritative=False)
    else:
        accounts(live, now, open_orders=("foreign-order",))
    publish(live, now)
    intents = live.drain_intents()
    assert not live.positions
    assert intents and all(i.action == "reconcile" for i in intents)


def test_account_read_started_before_fill_or_terminal_cannot_unlock_shared_leg():
    live, _ = state(make_plan(venues=("HL", "ASTER", "LIGHTER")))
    publish(live)
    first_orders = submissions(live)
    for intent in first_orders:
        fill(live, intent, NOW+100_000_000)
    live.on_evaluation_timer(NOW+100_000_002)
    live.drain_intents()
    now = NOW+SECOND
    accounts(live, now, observed_ns=NOW)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "ASTER", "100.1", "100.11", now)
    set_book(live, "LIGHTER", "100.4", "100.41", now)
    live.evaluate_symbol("BTC", now)
    assert not submissions(live) and len(live.positions) == 1
    accounts(live, now+1)
    live.evaluate_symbol("BTC", now+1)
    assert len(submissions(live)) == 2


def test_restore_pending_orders_preserves_lock_and_blocks_replay():
    store = MemoryStore()
    live, _ = state(store=store)
    publish(live)
    original = submissions(live)
    resumed, _ = state(store=store, activate=False)
    assert resumed.phase == "recovery_blocked" and not resumed.activate(NOW+SECOND)
    assert set(resumed.orders) == {i.client_order_id for i in original}
    assert len(resumed.positions) == 1
    publish(resumed, NOW+SECOND)
    assert not submissions(resumed)


def test_restore_confirmed_inventory_preserves_original_open_time_and_lock():
    store = MemoryStore()
    live, _ = state(store=store)
    original, _ = open_pair(live)
    resumed, _ = state(store=store, activate=False)
    restored = next(iter(resumed.positions.values()))
    assert restored["opened_ns"] == original["opened_ns"]
    assert restored["remaining_buy_base"] == original["remaining_buy_base"]
    assert restored["remaining_sell_base"] == original["remaining_sell_base"]
    assert resumed.expected_native("BTC.HL") == F("0.99")
    assert not submissions(resumed)
    now = NOW+SECOND
    accounts(resumed, now)
    assert resumed.activate(now)
    publish(resumed, now)
    assert not submissions(resumed) and len(resumed.positions) == 1


def test_opposite_shared_leg_exits_use_account_net_instead_of_unconditional_reduce_only():
    live, _ = state(make_plan(venues=("HL", "ASTER", "LIGHTER")),
        policy=TradePolicy(take_profit_bps=D(1000), max_loss_bps=D(1000)))
    first, _ = open_pair(live)
    now = first["opened_ns"]+2*SECOND
    accounts(live, now)
    set_book(live, "HL", "100.3", "100.31", now)
    set_book(live, "ASTER", "100.1", "100.11", now)
    set_book(live, "LIGHTER", "99.99", "100", now)
    live.evaluate_symbol("BTC", now)
    second = submissions(live)
    assert len(second) == 2
    for intent in second:
        fill(live, intent, now+100_000_000)
    assert live.expected_native("BTC.HL") == 0
    deadline = now+100_000_000+300*SECOND
    accounts(live, deadline)
    set_book(live, "HL", "100.3", "100.31", deadline)
    set_book(live, "ASTER", "100.1", "100.11", deadline)
    set_book(live, "LIGHTER", "99.99", "100", deadline)
    live.on_evaluation_timer(deadline)
    exits = submissions(live)
    shared = next(i for i in exits if i.instrument_id == "BTC.HL")
    assert shared.side == "SELL" and shared.reduce_only is False
    assert all(i.reduce_only for i in exits if i.instrument_id != "BTC.HL")


@pytest.mark.parametrize("kwargs", [{"max_pairs": 0}, {"max_pairs": True},
    {"max_pairs": 17}, {"gross": "501"}, {"gross": "NaN"}])
def test_live_hard_risk_invalid_configuration_is_rejected(kwargs):
    with pytest.raises(ValueError):
        state(**kwargs)

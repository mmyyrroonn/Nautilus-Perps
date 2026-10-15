"""Offline forced reductions keep execution safety separate from economics."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F

import pytest

from test_opportunity_live import (
    MemoryStore, accounts, fill, mark, open_pair, receipt, state, submissions,
)
from test_opportunity_live_rebalance import enter, refresh
from test_opportunity_trade import NOW, SECOND, publish, set_book
from opportunity_live import HARD_EXITS
from opportunity_trade_store import TradeStore


def lose_fees(live, **changes):
    for instrument_id, metadata in tuple(live.metadata.items()):
        live.update_metadata(instrument_id, replace(metadata, **(
            changes or {"taker_fee_bps": None, "fee_source": "unknown"})), NOW, evaluate=False)


def refresh_exit(live, now):
    accounts(live, now)
    set_book(live, "HL", "149.99", "150", now)
    set_book(live, "ASTER", "150.02", "150.03", now)


@pytest.mark.parametrize("fees", [
    {"taker_fee_bps": None}, {"taker_fee_bps": D("NaN")},
    {"taker_fee_bps": D("Infinity")}, {"taker_fee_bps": D("-1")},
    {"fee_source": "unknown"}, {"fee_source": ""}, {"fee_source": None},
])
def test_max_hold_without_reliable_fees_keeps_bounded_ioc_orders(fees):
    live, _ = state()
    position, _ = open_pair(live)
    lose_fees(live, **fees)
    now = position["opened_ns"] + 300 * SECOND
    refresh_exit(live, now)
    live.on_evaluation_timer(now)
    exits = submissions(live)
    assert len(exits) == 2 and position["exit_reason"] == "max_hold"
    for order in exits:
        assert order.reduce_only and order.time_in_force == "IOC" and not order.post_only
        assert 0 < order.quantity * order.price <= 100
        assert order.quantity <= D("0.99") and order.quantity % D("0.01") == 0
        assert order.price % D("0.01") == 0
    assert live.gross_exposure(reserve=True) <= 500


@pytest.mark.parametrize("reason", sorted(HARD_EXITS))
def test_all_latched_hard_exits_allow_missing_fees(reason):
    live, _ = state()
    position, _ = open_pair(live)
    lose_fees(live)
    position.update(status="exit_pending", exit_reason=reason)
    _, exits = mark(live, 0)
    assert len(exits) == 2


@pytest.mark.parametrize("age", [1, 180])
def test_soft_profit_and_new_entries_still_require_reliable_fees(age):
    live, _ = state()
    open_pair(live)
    lose_fees(live)
    _, exits = mark(live, 100, age)
    assert not exits
    fresh, _ = state()
    lose_fees(fresh)
    publish(fresh)
    assert not submissions(fresh) and not fresh.positions


@pytest.mark.parametrize("change", [
    {"multiplier": D(2)}, {"multiplier": D(0)}, {"multiplier": D("NaN")},
    {"size_increment": D("0.02")}, {"size_increment": None},
    {"size_increment": D("NaN")}, {"size_increment": D(0)},
    {"min_quantity": D("0.02")}, {"min_notional": D(1)},
    {"min_notional": D("NaN")}, {"instrument_id": "OTHER.HL"},
])
def test_rule_change_or_invalid_metadata_blocks_only_affected_leg_and_retains_risk(change):
    live, _ = state()
    position, _ = open_pair(live)
    lose_fees(live)
    live.metadata["BTC.HL"] = replace(live.metadata["BTC.HL"], **change)
    now = position["opened_ns"] + 300 * SECOND
    refresh_exit(live, now)
    live.on_evaluation_timer(now)
    exit_order, = submissions(live)
    assert exit_order.instrument_id == "BTC.ASTER"
    assert F(position["remaining_buy_base"]) == F("0.99")
    assert len(live.positions) == 1 and live.completed == 0


@pytest.mark.parametrize("problem", ["mismatch", "missing", "foreign_order", "stale_book", "unknown_order"])
def test_missing_fees_never_bypass_account_ownership_or_market_evidence(problem):
    live, _ = state()
    position, entries = open_pair(live)
    lose_fees(live)
    now = position["opened_ns"] + 300 * SECOND
    refresh_exit(live, now)
    if problem == "missing":
        live.accounts.pop("BTC.HL")
    elif problem == "mismatch":
        live.on_account(replace(live.accounts["BTC.HL"], position_quantity=D(5)), now)
    elif problem == "foreign_order":
        live.on_account(replace(live.accounts["BTC.HL"], open_order_ids=("foreign",)), now)
    elif problem == "stale_book":
        live.books["BTC.HL"] = replace(live.books["BTC.HL"], ts_event_ns=NOW, ts_received_ns=NOW)
    else:
        order = live.orders[entries[0].client_order_id]
        order.update(status="unknown", terminal_ns=None)
    live.on_evaluation_timer(now)
    exit_order, = submissions(live)
    assert exit_order.instrument_id == "BTC.ASTER"
    assert F(position["remaining_buy_base"]) == F("0.99") and live.positions


def test_unknown_other_entry_keeps_original_id_while_confirmed_leg_reduces_without_fees():
    live, _ = state()
    publish(live)
    buy, sell = submissions(live)
    fill(live, buy, NOW + SECOND // 10)
    live.on_receipt(receipt(sell, "unknown"), NOW + SECOND // 10)
    lose_fees(live)
    now = NOW + 2 * SECOND
    refresh_exit(live, now)
    live.stop_entries(now)
    live.on_evaluation_timer(now)
    reduction, = submissions(live)
    assert reduction.instrument_id == buy.instrument_id and reduction.reduce_only
    assert live.orders[sell.client_order_id]["status"] == "unknown"


def test_confirmed_excess_entry_is_trimmed_without_fees():
    live, _ = state()
    position, _ = enter(live)
    lose_fees(live)
    reduction, = refresh(live, NOW + SECOND)
    assert reduction.quantity == D("0.3") and reduction.side == "SELL"
    assert position["rebalance_target_base"] == "0.5"


@pytest.mark.parametrize("rule,value", [("price_increment", "0.1"), ("native_multiplier", "2"),
                                        ("quote_currency", "BTC")])
def test_native_rule_changes_do_not_unlock_forced_orders(rule, value):
    live, _ = state()
    position, _ = open_pair(live)
    lose_fees(live)
    native = dict(live.instruments["BTC.HL"])
    native[rule] = value
    live.register_instrument("BTC.HL", **native)
    now = position["opened_ns"] + 300 * SECOND
    refresh_exit(live, now)
    live.on_evaluation_timer(now)
    exit_order, = submissions(live)
    assert exit_order.instrument_id == "BTC.ASTER"
    assert live.halt_reason and F(position["remaining_buy_base"]) == F("0.99")


def test_equivalent_native_decimal_rules_do_not_block_reduction():
    live, _ = state()
    open_pair(live)
    lose_fees(live)
    live.register_instrument("BTC.HL", "0.010", "1.00", "USD")
    _, exits = mark(live, 0, 300)
    assert len(exits) == 2


def test_open_pending_commission_survives_restart_and_keeps_soft_exit_blocked():
    store = MemoryStore()
    live, _ = state(store=store)
    publish(live)
    entries = submissions(live)
    lose_fees(live)
    for intent in entries:
        fill(live, intent, NOW + SECOND // 10, fee=None)
    accounts(live, NOW + SECOND // 10 + 2)
    live.on_evaluation_timer(NOW + SECOND // 10 + 2)
    before = next(iter(live.positions.values()))
    resumed, _ = state(store=store)
    position = next(iter(resumed.positions.values()))
    assert position["pending_commissions"] == before["pending_commissions"]
    _, soft = mark(resumed, 100)
    assert not soft
    lose_fees(resumed)
    _, hard = mark(resumed, 100, 300)
    assert len(hard) == 2


def test_partial_missing_fee_close_retries_only_owned_residual_after_new_account_proof():
    live, _ = state()
    position, _ = open_pair(live)
    lose_fees(live)
    now, exits = mark(live, 100, 300)
    for intent in exits:
        fill(live, intent, now + SECOND // 10, quantity="0.3", fee=None)
    assert all(F(position[f"remaining_{leg}_base"]) == F("0.69") for leg in ("buy", "sell"))
    later = now + 2 * SECOND
    set_book(live, "HL", "99.99", "100", later)
    set_book(live, "ASTER", "99.28", "99.29", later)
    live.on_evaluation_timer(later)
    assert not submissions(live)
    accounts(live, later + 1)
    live.on_evaluation_timer(later + 1)
    rest = submissions(live)
    assert len(rest) == 2 and all(intent.quantity == D("0.69") for intent in rest)
    assert len(position["pending_commissions"]) == 2


@pytest.mark.parametrize("commission,currency", [(None, "USD"), ("0.01", "BTC"), ("NaN", "USD")])
def test_unknown_exit_fees_survive_flat_reconciliation_and_sqlite_restart(tmp_path, commission, currency):
    store = TradeStore(tmp_path / "live.sqlite3", "synthetic-policy")
    try:
        live, events = state(store=store)
        position, _ = open_pair(live)
        lose_fees(live)
        now, exits = mark(live, 100, 300)
        assert len(exits) == 2
        # Metadata can disappear after dispatch; fills must still retire risk.
        live.metadata.clear()
        for intent in exits:
            payload = fill(live, intent, now + SECOND // 10, fee=commission, currency=currency)
            live.on_fill(payload, now + SECOND // 10 + 2)
        accounts(live, now + SECOND // 10 + 3)
        live.on_evaluation_timer(now + SECOND // 10 + 3)
        assert not live.positions and live.completed == 1
        assert live.realized_net == 0
        closed, = [event for event in events if event["event"] == "closed"]
        assert closed["net_usd"] is None and not closed["fee_confirmed"]
        pending = live.document()["pending_economics"]
        assert len(pending) == 1
        record = pending[str(position["position_id"])]
        assert record["net_usd"] is None and record["provisional_net_usd"] is None
        assert len(record["pending_commissions"]) == 2
        assert all(item["estimated_fee_usd"] is None for item in record["pending_commissions"].values())
        unknown_fills = [event for event in events if event["event"] == "fill" and not event["fee_confirmed"]]
        assert len(unknown_fills) == 2 and all(event["fee_usd"] is None for event in unknown_fills)
    finally:
        store.close()
    with_store = TradeStore(tmp_path / "live.sqlite3", "synthetic-policy")
    try:
        resumed, _ = state(store=with_store)
        assert resumed.document()["pending_economics"] == pending
        assert resumed.realized_net == 0 and not resumed.positions
    finally:
        with_store.close()


def test_real_fill_commissions_confirm_profit_even_if_fee_metadata_is_missing():
    live, events = state()
    open_pair(live)
    lose_fees(live)
    now, exits = mark(live, 100, 300)
    assert len(exits) == 2
    for intent in exits:
        fill(live, intent, now + SECOND // 10, fee="0.01")
    accounts(live, now + SECOND // 10 + 2)
    live.on_evaluation_timer(now + SECOND // 10 + 2)
    assert live.realized_net > 0 and not live.document()["pending_economics"]
    closed, = [event for event in events if event["event"] == "closed"]
    assert F(closed["net_usd"]) == live.realized_net and closed["fee_confirmed"]


def test_estimated_commission_is_pending_not_confirmed_profit():
    live, _ = state()
    open_pair(live)
    lose_fees(live, taker_fee_bps=D(2), fee_source="synthetic")
    now, exits = mark(live, 100, 300)
    for intent in exits:
        fill(live, intent, now + SECOND // 10, fee=None)
    accounts(live, now + SECOND // 10 + 2)
    live.on_evaluation_timer(now + SECOND // 10 + 2)
    record, = live.document()["pending_economics"].values()
    assert record["net_usd"] is None and F(record["provisional_net_usd"]) > 0
    assert live.realized_net == 0
    assert all(F(item["estimated_fee_usd"]) > 0 for item in record["pending_commissions"].values())


def test_available_pending_fee_estimates_still_contribute_to_loss_stop():
    live, _ = state()
    publish(live)
    entries = submissions(live)
    lose_fees(live, taker_fee_bps=D(10), fee_source="synthetic")
    for intent in entries:
        fill(live, intent, NOW + SECOND // 10, fee=None)
    position = next(iter(live.positions.values()))
    _, exits = mark(live, 5)
    assert len(exits) == 2 and position["exit_reason"] == "max_loss"
    assert F(position["actual_entry"]["buy_fee_usd"]) == 0
    assert len(position["pending_commissions"]) == 2


def test_legacy_realized_total_is_preserved_without_claiming_confirmed_fees():
    live, _ = state()
    saved = live.snapshot()
    saved.update(schema_version=1, completed=1, realized_net_usd="12.34")
    for field in ("pending_economics", "legacy_realized_net_usd"):
        saved.pop(field, None)
    resumed, _ = state(store=MemoryStore(saved))
    assert resumed.realized_net == 0
    assert resumed.document()["legacy_realized_net_usd"] == "12.34"
    again, _ = state(store=MemoryStore(resumed.snapshot()))
    assert again.document()["legacy_realized_net_usd"] == "12.34"


def test_legacy_unknown_opening_fees_remain_pending_after_forced_close():
    live, _ = state()
    position, _ = open_pair(live)
    position["unknown_commission"] = True
    position["actual_entry"]["buy_fee_usd"] = "0.02"
    saved = live.snapshot()
    saved["schema_version"] = 1
    saved["positions"][0].pop("pending_commissions")
    saved["positions"][0].pop("native_rules")
    resumed, _ = state(store=MemoryStore(saved))
    lose_fees(resumed)
    now, exits = mark(resumed, 100, 300)
    assert len(exits) == 2
    for intent in exits:
        fill(resumed, intent, now + SECOND // 10, fee="0.01")
    accounts(resumed, now + SECOND // 10 + 2)
    resumed.on_evaluation_timer(now + SECOND // 10 + 2)
    record, = resumed.document()["pending_economics"].values()
    assert record["legacy_fee_accounting"]
    assert record["actual_entry"]["buy_fee_usd"] == "0.02"
    assert record["net_usd"] is None and record["provisional_net_usd"] is None
    assert resumed.realized_net == 0


def test_restart_preserves_native_rule_fence_for_owned_inventory():
    store = MemoryStore()
    live, _ = state(store=store)
    position, _ = open_pair(live)
    resumed, _ = state(store=store, price_increment="0.1")
    lose_fees(resumed)
    now = position["opened_ns"] + 300 * SECOND
    refresh_exit(resumed, now)
    resumed.on_evaluation_timer(now)
    assert not submissions(resumed)
    assert len(resumed.positions) == 1 and resumed.completed == 0

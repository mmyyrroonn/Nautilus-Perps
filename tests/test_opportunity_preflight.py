"""Pure offline preflight receipts, failure ownership and bounded intents."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_preflight import (AccountSnapshot, BtcPreflight, ExecutionReceipt,
                                    PreflightMarket)

NOW = 1_800_000_000_000_000_000
MS = 1_000_000


def market(venue="HL", **kwargs):
    return PreflightMarket(venue, f"BTC.{venue}", D("0.001"), D("0.01"), D(0), D(5), **kwargs)


def machine(markets=None, **kwargs):
    return BtcPreflight(markets or (market(),), session_id="offline", **kwargs)


def account(state, now=NOW, *, quantity="0", orders=(), **kwargs):
    return AccountSnapshot(state.market.venue, state.market.instrument_id, D(100), D("100.02"),
                            now, D(quantity), orders, **kwargs)


def receipt(intent, kind, *, execution_id="", quantity="0"):
    return ExecutionReceipt(intent.request_id, intent.client_order_id, kind,
                            execution_id, D(quantity))


def start_probe(state):
    connect, = state.start(NOW)
    read, = state.on_receipt(receipt(connect, "connected"), NOW+MS)
    assert read.action == "reconcile"
    probe, = state.on_account(account(state, NOW+2*MS), NOW+2*MS)
    return probe


def reach_open(state):
    probe = start_probe(state)
    adjust, = state.on_receipt(receipt(probe, "accepted"), NOW+3*MS)
    if state.market.modify_mode == "native":
        assert adjust.action == "modify"
        cancel, = state.on_receipt(receipt(adjust, "updated"), NOW+4*MS)
    else:
        assert adjust.action == "cancel"
        read, = state.on_receipt(receipt(adjust, "canceled"), NOW+4*MS)
        assert read.action == "reconcile"
        replacement, = state.on_account(account(state, NOW+5*MS), NOW+5*MS)
        assert replacement.client_order_id != probe.client_order_id
        cancel, = state.on_receipt(receipt(replacement, "accepted"), NOW+6*MS)
    read, = state.on_receipt(receipt(cancel, "canceled"), NOW+7*MS)
    assert read.action == "reconcile"
    opening, = state.on_account(account(state, NOW+8*MS), NOW+8*MS)
    assert opening.action == "submit" and opening.time_in_force == "IOC"
    return opening


def fill_terminal(state, intent, now, *, quantity=None, execution_id="fill"):
    quantity = intent.quantity if quantity is None else D(quantity)
    assert state.on_receipt(receipt(intent, "accepted"), now) == ()
    assert state.on_receipt(receipt(intent, "fill", execution_id=execution_id, quantity=quantity), now+MS) == ()
    read, = state.on_receipt(receipt(intent, "terminal"), now+2*MS)
    return read


def complete_venue(state):
    opening = reach_open(state)
    fill_terminal(state, opening, NOW+10*MS, execution_id=f"{state.market.venue}-open")
    closing, = state.on_account(account(state, NOW+13*MS, quantity=opening.quantity), NOW+13*MS)
    assert closing.side == "SELL" and closing.reduce_only
    assert closing.quantity == opening.quantity
    fill_terminal(state, closing, NOW+14*MS, execution_id=f"{state.market.venue}-close")
    return state.on_account(account(state, NOW+17*MS), NOW+17*MS)


@pytest.mark.parametrize("modify_mode", ["native", "cancel_replace"])
def test_complete_check_requires_every_receipt_and_authoritative_flat(modify_mode):
    state = machine((market(modify_mode=modify_mode, supports_modify=modify_mode == "native"),))
    assert complete_venue(state) == ()
    document = state.document()
    assert document["result"] == "passed" and D(document["owned_quantity"]) == 0
    assert document["unresolved_client_order_ids"] == []
    assert document["adjust_modes"] == {"HL": modify_mode}
    timings = document["operation_timings"]
    assert all(row["elapsed_ms"] is not None for row in timings)
    assert {row["operation"] for row in timings} >= {"connect", "submit", "cancel", "reconcile"}
    if modify_mode == "native":
        assert "modify" in {row["operation"] for row in timings}
    else:
        assert "modify" not in {row["operation"] for row in timings}
        assert sum(row["operation"] == "submit" for row in timings) == 4
    entries = [row for row in timings if row["stage"] in {"open_terminal", "close_terminal"}]
    for row in entries:
        expected = ("2", "3", "4") if row["stage"] == "open_terminal" else ("1", "2", "3")
        assert (row["accepted_elapsed_ms"], row["first_fill_elapsed_ms"],
                row["terminal_elapsed_ms"]) == expected


def test_passive_probe_and_adjustment_never_cross_and_use_actual_minimum():
    state = machine((replace(market(), min_notional=D(15)),))
    probe = start_probe(state)
    assert probe.post_only and probe.side == "BUY"
    assert probe.price < D(100)
    assert probe.quantity*probe.price >= 15
    assert probe.quantity*probe.price > 2
    adjust, = state.on_receipt(receipt(probe, "accepted"), NOW+3*MS)
    assert adjust.price < probe.price and adjust.post_only
    assert adjust.quantity*adjust.price >= 15
    assert adjust.quantity == probe.quantity


def test_native_account_start_fence_and_current_bbo_have_separate_freshness():
    state = machine()
    connect, = state.start(NOW)
    state.on_receipt(receipt(connect, "connected"), NOW + MS)
    now = NOW + 4000 * MS
    snapshot = account(state, NOW + 2 * MS, book_observed_ns=now)
    probe, = state.on_account(snapshot, now)
    assert probe.action == "submit" and state.stage == "probe_accept"
    assert snapshot.observed_ns == NOW + 2 * MS


def test_preflight_final_flat_requires_read_started_after_last_terminal():
    state = machine()
    opening = reach_open(state)
    fill_terminal(state, opening, NOW + 10 * MS, execution_id="open")
    closing, = state.on_account(account(state, NOW + 13 * MS, quantity=opening.quantity), NOW + 13 * MS)
    fill_terminal(state, closing, NOW + 14 * MS, execution_id="close")
    assert state.stage == "final_reconcile"
    stale = account(state, NOW + 13 * MS, book_observed_ns=NOW + 17 * MS)
    assert state.on_account(stale, NOW + 17 * MS) == ()
    assert state.result == "running"
    assert state.on_account(account(state, NOW + 18 * MS), NOW + 18 * MS) == ()
    assert state.result == "passed"


@pytest.mark.parametrize("account_age_ms,book_age_ms", [(15001, 0), (0, 2001)])
def test_old_account_or_old_bbo_still_blocks_probe(account_age_ms, book_age_ms):
    state = machine()
    connect, = state.start(NOW)
    state.on_receipt(receipt(connect, "connected"), NOW + MS)
    now = NOW + 20000 * MS
    snapshot = account(state, now - account_age_ms * MS,
                       book_observed_ns=now - book_age_ms * MS)
    assert state.on_account(snapshot, now) == ()
    assert state.result == "blocked" and not state.orders
    assert state.document()["owned_quantity"] == "0"


def test_only_final_flat_snapshot_advances_to_next_venue():
    state = machine((market("HL"), market("ASTER", modify_mode="cancel_replace", supports_modify=False)))
    next_connect, = complete_venue(state)
    assert next_connect.action == "connect" and next_connect.venue == "ASTER"
    assert state.document()["results"] == [{"venue": "HL", "result": "passed", "reason": None}]
    assert state.document()["result"] == "running"


@pytest.mark.parametrize("quantity,orders", [("0.001", ()), ("-0.001", ()), ("0", ("foreign",))])
def test_existing_btc_position_or_foreign_order_blocks_without_writes(quantity, orders):
    state = machine()
    connect, = state.start(NOW)
    state.on_receipt(receipt(connect, "connected"), NOW+MS)
    assert state.on_account(account(state, NOW+2*MS, quantity=quantity, orders=orders), NOW+2*MS) == ()
    assert state.result == "blocked"
    assert all(intent.action in {"connect", "reconcile"} for intent in state.requests.values())


@pytest.mark.parametrize("cap,minimum", [("4", "5"), ("1000", "51")])
def test_current_minimum_over_applied_or_hard_cap_blocks(cap, minimum):
    state = machine((replace(market(), min_notional=D(minimum)),), max_notional_usd=D(cap))
    connect, = state.start(NOW)
    state.on_receipt(receipt(connect, "connected"), NOW+MS)
    assert state.on_account(account(state, NOW+2*MS), NOW+2*MS) == ()
    assert state.result == "blocked"
    assert D(state.document()["max_notional_usd"]) <= 50


@pytest.mark.parametrize("capability", ["supports_modify", "supports_reduce_only", "supports_post_only", "supports_ioc"])
def test_unsupported_capability_never_becomes_passed(capability):
    state = machine((replace(market(), **{capability: False}),))
    assert state.start(NOW) == ()
    assert state.result == "blocked" and not state.requests


def test_explicit_unsupported_adjustment_cannot_silently_cancel_replace():
    state = machine((market(modify_mode="unsupported"),))
    assert state.start(NOW) == ()
    assert state.result == "blocked" and not state.requests


def test_unsupported_later_venue_blocks_entire_sequence_before_any_write():
    state = machine((market("HL"), market("ONDO", modify_mode="unsupported")))
    assert state.start(NOW) == ()
    assert state.result == "blocked" and "ONDO" in state.reason
    assert not state.requests and not state.results


def test_late_fill_on_prior_venue_is_not_ignored_when_next_venue_connects():
    state = machine((market("HL"), market("ASTER")))
    complete_venue(state)
    probe = next(intent for intent in state.requests.values() if intent.post_only and intent.action == "submit")
    read, = state.on_receipt(receipt(probe, "fill", execution_id="late", quantity="0.001"), NOW+20*MS)
    assert read.action == "reconcile"
    assert state.result == "blocked"
    assert state.document()["late_residuals_by_venue"] == {"HL": "0.001"}
    assert state.owned_quantity == 0


def test_accepted_ioc_is_not_a_fill_and_zero_fill_terminal_cannot_pass():
    state = machine()
    opening = reach_open(state)
    assert state.on_receipt(receipt(opening, "accepted"), NOW+10*MS) == ()
    assert state.owned_quantity == 0
    assert not any(intent.reduce_only for intent in state.requests.values())
    state.on_receipt(receipt(opening, "terminal"), NOW+11*MS)
    assert state.on_account(account(state, NOW+12*MS), NOW+12*MS) == ()
    assert state.result == "failed"


def test_duplicate_fill_does_not_double_inventory_and_disordered_receipt_cannot_advance():
    state = machine()
    probe = start_probe(state)
    assert state.on_receipt(receipt(probe, "updated"), NOW+3*MS) == ()
    assert state.stage == "probe_accept"
    adjust, = state.on_receipt(receipt(probe, "accepted"), NOW+4*MS)
    assert state.on_receipt(receipt(probe, "accepted"), NOW+5*MS) == ()
    cancel, = state.on_receipt(receipt(adjust, "updated"), NOW+6*MS)
    state.on_receipt(receipt(cancel, "canceled"), NOW+7*MS)
    opening, = state.on_account(account(state, NOW+8*MS), NOW+8*MS)
    event = receipt(opening, "fill", execution_id="unique", quantity=opening.quantity)
    assert state.on_receipt(event, NOW+9*MS) == ()
    assert state.on_receipt(event, NOW+10*MS) == ()
    assert state.owned_quantity == F(opening.quantity)
    assert state.stage == "open_terminal"
    assert state.on_receipt(ExecutionReceipt("foreign", "foreign", "fill", "other", D(1)), NOW+11*MS) == ()
    assert state.owned_quantity == F(opening.quantity)


def test_partial_open_below_conservative_close_minimum_stays_visible():
    state = machine((replace(market(), min_notional=D(0)),))
    # An exact two-lot requested minimum allows one confirmed lot to be closed.
    state = machine((replace(state.market, min_quantity=D("0.001"), min_notional=D("0.1")),))
    opening = reach_open(state)
    assert opening.quantity == D("0.002")
    fill_terminal(state, opening, NOW+10*MS, quantity="0.001")
    # Preserve a partial below current notional minimum rather than claiming flat.
    assert state.on_account(account(state, NOW+13*MS, quantity="0.001"), NOW+13*MS) == ()
    assert state.result == "blocked" and state.owned_quantity == F("0.001")
    assert state.document()["results"] == []


def test_multiple_incremental_fills_reaching_requested_quantity_can_pass():
    state = machine((replace(market(), min_notional=D(0), min_quantity=D("0.01")),))
    opening = reach_open(state)
    # A split fill which reaches the original requested quantity is not partial overall.
    state.on_receipt(receipt(opening, "fill", execution_id="part1", quantity="0.004"), NOW+10*MS)
    state.on_receipt(receipt(opening, "fill", execution_id="part2", quantity="0.006"), NOW+11*MS)
    state.on_receipt(receipt(opening, "terminal"), NOW+12*MS)
    closing, = state.on_account(account(state, NOW+13*MS, quantity="0.01"), NOW+13*MS)
    assert closing.reduce_only and closing.quantity == D("0.01")
    fill_terminal(state, closing, NOW+14*MS, execution_id="close")
    state.on_account(account(state, NOW+17*MS), NOW+17*MS)
    assert state.result == "passed"


def test_partial_open_closes_only_confirmed_quantity_and_is_failed_after_flat():
    state = machine()
    opening = reach_open(state)
    assert opening.quantity == D("0.051")
    fill_terminal(state, opening, NOW+10*MS, quantity="0.05")
    snapshot = replace(account(state, NOW+13*MS, quantity="0.05"), bid=D(101), ask=D("101.02"))
    closing, = state.on_account(snapshot, NOW+13*MS)
    assert closing.reduce_only and closing.quantity == D("0.05")
    assert closing.quantity < opening.quantity
    fill_terminal(state, closing, NOW+14*MS, execution_id="closing")
    state.on_account(account(state, NOW+17*MS), NOW+17*MS)
    assert state.result == "failed" and state.owned_quantity == 0


def test_partial_close_terminal_never_claims_flat_and_reconciles_residual():
    state = machine()
    opening = reach_open(state)
    fill_terminal(state, opening, NOW+10*MS, execution_id="opening")
    closing, = state.on_account(account(state, NOW+13*MS, quantity=opening.quantity), NOW+13*MS)
    read = fill_terminal(state, closing, NOW+14*MS, execution_id="partial-close", quantity="0.02")
    assert read.action == "reconcile"
    assert state.owned_quantity == F("0.031")
    assert state.result == "running" and state.document()["results"] == []
    state.on_account(account(state, NOW+17*MS, quantity="0.031"), NOW+17*MS)
    assert state.result == "blocked" and state.owned_quantity == F("0.031")


def test_modify_rejection_requests_only_own_cancel_and_does_not_pass():
    state = machine()
    probe = start_probe(state)
    adjust, = state.on_receipt(receipt(probe, "accepted"), NOW+3*MS)
    reconcile, = state.on_receipt(receipt(adjust, "rejected"), NOW+4*MS)
    assert reconcile.action == "reconcile"
    cancel, = state.on_account(account(state, NOW+5*MS, orders=(probe.client_order_id,)), NOW+5*MS)
    assert cancel.action == "cancel" and cancel.client_order_id == probe.client_order_id
    state.on_receipt(receipt(cancel, "canceled"), NOW+6*MS)
    state.on_account(account(state, NOW+7*MS), NOW+7*MS)
    assert state.result == "failed" and state.owned_quantity == 0


@pytest.mark.parametrize("kind", ["unknown", "disconnected"])
def test_uncertain_submission_preserves_identity_and_requires_reconciliation(kind):
    state = machine()
    opening = reach_open(state)
    read, = state.on_receipt(receipt(opening, kind), NOW+10*MS)
    assert read.action == "reconcile" and read.client_order_id == opening.client_order_id
    assert opening.client_order_id in state.document()["unresolved_client_order_ids"]
    assert state.on_account(account(state, NOW+11*MS), NOW+11*MS) == ()
    assert state.result == "blocked"
    assert sum(intent.action == "submit" and intent.time_in_force == "IOC"
               for intent in state.requests.values()) == 1


def test_timeout_reads_state_without_resubmitting_unknown_order():
    state = machine(timeout_secs=1)
    probe = start_probe(state)
    assert state.on_timer(NOW+2*MS+999*MS) == ()
    read, = state.on_timer(NOW+2*MS+1000*MS)
    assert read.action == "reconcile" and read.client_order_id == probe.client_order_id
    assert state.result == "blocked"
    assert state.on_timer(NOW+3_000*MS) == ()
    assert len([intent for intent in state.requests.values() if intent.action == "submit"]) == 1


@pytest.mark.parametrize("bad", ["stale", "disconnected", "non_authoritative", "crossed"])
def test_initial_account_and_book_must_be_fresh_authoritative_and_connected(bad):
    state = machine()
    connect, = state.start(NOW)
    state.on_receipt(receipt(connect, "connected"), NOW+MS)
    snapshot = account(state, NOW+2*MS)
    received_ns = NOW+2*MS
    if bad == "stale":
        received_ns += 16_000*MS
        snapshot = replace(snapshot, book_observed_ns=received_ns)
    elif bad == "disconnected":
        snapshot = replace(snapshot, connected=False)
    elif bad == "non_authoritative":
        snapshot = replace(snapshot, authoritative=False)
    else:
        snapshot = replace(snapshot, ask=D("99.99"))
    assert state.on_account(snapshot, received_ns) == ()
    assert state.result == "blocked"


def test_conflicting_duplicate_execution_blocks_and_keeps_original_confirmed_quantity():
    state = machine()
    opening = reach_open(state)
    state.on_receipt(receipt(opening, "fill", execution_id="same", quantity="0.01"), NOW+10*MS)
    read, = state.on_receipt(receipt(opening, "fill", execution_id="same", quantity="0.02"), NOW+11*MS)
    assert read.action == "reconcile"
    assert state.result == "blocked" and state.owned_quantity == F("0.01")


@pytest.mark.parametrize("field,value", [("timeout_secs", True), ("max_book_age_ms", 0),
    ("max_notional_usd", "NaN"), ("slippage_bps", 21), ("max_close_attempts", 0)])
def test_invalid_policy_cannot_produce_any_intent(field, value):
    with pytest.raises(ValueError):
        machine(**{field: value})


def test_native_quantity_multiplier_and_quote_conversion_are_included_in_cap():
    state = machine((replace(market(), multiplier=D(10), quote_to_usd=D("1.01")),))
    probe = start_probe(state)
    assert probe.quantity % D("0.001") == 0
    assert probe.quantity * 10 * probe.price >= 5
    assert probe.quantity * 10 * D("100.23") * D("1.01") <= 50

"""Pure controller tests: no native runtime, sockets, credentials or financial calls."""
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_roundtrip import ClosePosition, RoundTrip


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        return self.now


class Adapter:
    def __init__(self):
        self.order_id = None
        self.commands = []
        self.cancels = []
        self.orders = {}
        self.events = []
        self.position = Decimal("0")
        self.admission = {"reason": "snapshot_pending"}
        self.settlement_diagnostic = {"reason": "economics_pending"}
        self.is_ready = True
        self.is_settled = True
        self.prices = {"bid": "100", "ask": "101"}

    def ready(self):
        return self.is_ready

    def quote(self, symbol):
        return self.prices

    def position_quantity(self, symbol):
        return self.position

    def owned_order(self, identity):
        return self.orders[identity]

    def settled(self, order):
        return order.is_closed and self.is_settled

    def submit(self, command):
        assert not any(not order.is_closed for order in self.orders.values())
        self.commands.append(command)
        self.order_id = "owned-" + str(len(self.commands))
        self.orders[self.order_id] = SimpleNamespace(
            client_order_id=self.order_id, venue_order_id="venue-" + self.order_id,
            status="ACCEPTED", is_closed=False, filled_qty=Decimal("0"),
            side=command["side"].upper(), is_reduce_only=command["reduce_only"],
            instrument_id="BTC_USDC_PERP.BACKPACK")

    def cancel_order(self, identity):
        assert not self.orders[identity].is_closed
        self.cancels.append(identity)

    def fill(self, quantity, *, price=None):
        order = self.orders[self.order_id]
        command = self.commands[-1]
        quantity = Decimal(quantity)
        order.filled_qty += quantity
        self.position += quantity if command["side"] == "buy" else -quantity
        order.status = "PARTIALLY_FILLED"
        self.events.append({"kind": "filled", "client_order_id": self.order_id,
            "quantity": str(quantity), "price": price or command.get("price", "101"),
            "liquidity_side": "MAKER" if command["post_only"] else "TAKER",
            "commission": "0 USDC"})

    def terminal(self, status="CANCELED"):
        self.orders[self.order_id].status = status
        self.orders[self.order_id].is_closed = True


def fixture(**changes):
    clock, adapter = Clock(), Adapter()
    command = {"action": "roundtrip", "symbol": "BTC_USDC_PERP", "style": "maker",
        "quantity": "0.04", "wait_seconds": 30, "maker_reprice_seconds": 5,
        "maker_max_reprices": 2, **changes}
    return RoundTrip(command, clock=clock), adapter, clock


def cancel_and_reconcile(trip, adapter, clock, *, filled="0"):
    clock.now += 5
    trip.advance(adapter)
    if Decimal(filled):
        adapter.fill(filled)
    adapter.terminal()
    trip.advance(adapter)


def assert_market_exit(command, quantity):
    assert command["side"] == "sell" and command["quantity"] == quantity
    assert command["order_type"] == "market" and command["time_in_force"] == "IOC"
    assert command["reduce_only"] is True and command["post_only"] is False
    assert "price" not in command and "reference_price" in command


def test_cancel_once_before_entry_repost_at_latest_bid_then_market_exit():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    clock.now = 4
    trip.advance(adapter)
    assert adapter.cancels == []
    clock.now = 5
    trip.advance(adapter)
    trip.advance(adapter)
    assert adapter.cancels == ["owned-1"] and len(adapter.commands) == 1
    adapter.terminal()
    trip.advance(adapter)
    adapter.prices = {"bid": "102", "ask": "103"}
    trip.advance(adapter)
    assert adapter.commands[-1]["price"] == "102"
    assert trip.reprices == {"entry": 1, "exit": 0}
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    trip.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.04")
    assert adapter.commands[-1]["reference_price"] == "103"
    clock.now += 5
    trip.advance(adapter)
    trip.advance(adapter)
    assert adapter.cancels == ["owned-1"] and len(adapter.commands) == 3
    assert trip.reprices == {"entry": 1, "exit": 0}
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.done and trip.failure is None
    assert [leg["client_order_id"] for leg in trip.legs] == ["owned-1", "owned-2", "owned-3"]
    assert trip.legs[0]["liquidity_verified"] is False
    assert [leg["order_style"] for leg in trip.legs] == ["maker", "maker", "market"]
    assert trip.legs[-1]["liquidity_verified"] is True
    assert trip.snapshot()["entry_style"] == "maker" and trip.snapshot()["exit_style"] == "market"


def test_partial_entry_fills_and_late_cancel_fill_aggregate_once_before_market_exit():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    adapter.fill("0.01")
    clock.now = 5
    trip.advance(adapter)
    adapter.fill("0.005")  # A fill arriving after cancel request belongs to the old attempt.
    adapter.terminal()
    adapter.is_settled = False
    trip.advance(adapter)
    trip.advance(adapter)
    assert trip.legs == [] and len(adapter.commands) == 1
    adapter.is_settled = True
    trip.advance(adapter)
    adapter.is_ready = False
    trip.advance(adapter)
    trip.advance(adapter)
    assert len(trip.legs) == 1
    adapter.is_ready = True
    trip.advance(adapter)
    assert adapter.commands[-1]["quantity"] == "0.025"
    adapter.fill("0.025")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    trip.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.040")
    adapter.fill("0.01")
    clock.now += 5
    trip.advance(adapter)
    assert adapter.cancels == ["owned-1"] and len(adapter.commands) == 3
    adapter.fill("0.030")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.done and adapter.position == 0
    assert trip.snapshot()["aggregate_filled_quantity"] == {"entry": "0.040", "exit": "0.040"}
    assert trip.snapshot()["aggregate_notional_usdc"] == {"entry": "4.000", "exit": "4.040"}
    assert trip.reprices == {"entry": 1, "exit": 0}


def test_full_fill_racing_cancel_closes_entry_without_reposting():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    clock.now = 5
    trip.advance(adapter)
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    trip.advance(adapter)
    assert trip.reprices["entry"] == 0 and len(adapter.commands) == 2
    assert_market_exit(adapter.commands[-1], "0.04")


@pytest.mark.parametrize("fill", ["0", "0.01"])
def test_reprice_cap_cancels_last_attempt_and_only_closes_confirmed_entry(fill):
    trip, adapter, clock = fixture(maker_max_reprices=1)
    trip.advance(adapter)
    cancel_and_reconcile(trip, adapter, clock, filled=fill)
    trip.advance(adapter)
    cancel_and_reconcile(trip, adapter, clock)
    assert len(adapter.commands) == 2 and trip.reprices["entry"] == 1
    if Decimal(fill):
        assert trip.phase == "exit" and trip.failure is None
        trip.advance(adapter)
        assert_market_exit(adapter.commands[-1], fill)
        adapter.fill(fill)
        adapter.terminal("FILLED")
        trip.advance(adapter)
        assert trip.done and trip.failure is None and adapter.position == 0
        assert trip.legs[-1]["liquidity_verified"] is True
    else:
        assert "no fills" in trip.failure and not trip.done


def test_total_phase_deadline_is_not_renewed_by_replacement():
    trip, adapter, clock = fixture(wait_seconds=8)
    trip.advance(adapter)
    cancel_and_reconcile(trip, adapter, clock)
    trip.advance(adapter)
    assert trip.sent_at == 5 and trip.phase_started_at == 0
    clock.now = 8
    trip.advance(adapter)
    assert adapter.cancels == ["owned-1", "owned-2"]
    adapter.fill("0.01")
    adapter.terminal()
    trip.advance(adapter)
    assert trip.phase == "exit" and trip.entry_quantity == Decimal("0.01")
    assert trip.legs[-1]["cancel_reason"] == "deadline"
    trip.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.01")
    adapter.fill("0.01")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.done and trip.failure is None and adapter.position == 0
    assert trip.legs[-1]["fills"][0]["liquidity_side"] == "TAKER"
    assert trip.legs[-1]["liquidity_verified"]


def test_two_minute_side_allows_at_most_twenty_three_replacements():
    trip, adapter, clock = fixture(wait_seconds=120, maker_max_reprices=23)
    trip.advance(adapter)
    for _ in range(23):
        cancel_and_reconcile(trip, adapter, clock)
        trip.advance(adapter)
    assert clock.now == 115 and len(adapter.commands) == 24
    assert trip.reprices["entry"] == 23 and trip.phase_started_at == 0
    cancel_and_reconcile(trip, adapter, clock)
    trip.advance(adapter)
    assert clock.now == 120 and len(adapter.commands) == 24
    assert "no fills" in trip.failure and trip.legs[-1]["cancel_reason"] == "deadline"


def test_cancel_reconciliation_delay_consumes_original_side_deadline():
    trip, adapter, clock = fixture(wait_seconds=120, maker_max_reprices=23)
    trip.advance(adapter)
    clock.now = 5
    trip.advance(adapter)
    clock.now = 121
    adapter.fill("0.01")
    adapter.terminal()
    trip.advance(adapter)
    assert len(adapter.commands) == 1 and trip.phase == "exit"
    assert trip.entry_quantity == Decimal("0.01")


def test_clock_crossing_deadline_while_quote_is_read_does_not_submit():
    trip, adapter, clock = fixture(wait_seconds=8)
    trip.advance(adapter)
    cancel_and_reconcile(trip, adapter, clock)
    def slow_quote(symbol):
        clock.now = 8
        return adapter.prices
    adapter.quote = slow_quote
    trip.advance(adapter)
    trip.advance(adapter)
    assert len(adapter.commands) == 1 and "no fills" in trip.failure


@pytest.mark.parametrize("blocked", ["readiness", "quote"])
def test_pending_replacement_wait_does_not_duplicate_audit_or_extend_deadline(blocked):
    trip, adapter, clock = fixture(wait_seconds=10)
    trip.advance(adapter)
    cancel_and_reconcile(trip, adapter, clock, filled="0.01")
    if blocked == "readiness":
        adapter.is_ready = False
    else:
        adapter.prices = None
    trip.advance(adapter)
    trip.advance(adapter)
    assert len(trip.legs) == len(adapter.commands) == 1
    assert trip.snapshot()["pending_reprice"] and trip.waiting_for == "entry_admission"
    clock.now = 10
    trip.advance(adapter)
    assert trip.phase == "exit" and len(adapter.commands) == 1


def test_unknown_cancel_does_not_repost_and_terminal_reconciliation_is_bounded():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    clock.now = 5
    trip.advance(adapter)
    adapter.orders[adapter.order_id].status = "PENDING_CANCEL"
    clock.now = 29
    trip.advance(adapter)
    assert len(adapter.commands) == len(adapter.cancels) == 1
    adapter.terminal()
    adapter.is_settled = False
    trip.advance(adapter)
    clock.now = 60
    trip.advance(adapter)
    assert "reconciliation deadline" in trip.failure and not trip.legs
    assert len(adapter.commands) == 1


def controller_with_active_order(phase):
    trip, adapter, clock = fixture(wait_seconds=120, maker_max_reprices=23)
    trip.advance(adapter)
    if phase == "entry":
        return trip, adapter, clock
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    if phase == "close-only":
        adapter.check_close_origin = lambda symbol, identity: None
        trip = ClosePosition({"action": "close-only", "symbol": "BTC_USDC_PERP", "style": "maker",
            "entry_client_order_id": adapter.order_id, "max_quantity": "0.04", "wait_seconds": 120,
            "maker_reprice_seconds": 5, "maker_max_reprices": 23}, clock=clock)
    else:
        trip.advance(adapter)
    trip.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.04")
    return trip, adapter, clock


@pytest.mark.parametrize("phase", ["entry", "exit", "close-only"])
def test_unconfirmed_submit_times_out_without_cancel_or_resubmit_and_retains_exposure(phase):
    trip, adapter, clock = controller_with_active_order(phase)
    identity = adapter.order_id
    count = len(adapter.commands)
    adapter.fill("0.01")
    adapter.orders[identity].status = "SUBMITTED"
    deadline = 120 if phase == "entry" else 10
    clock.now = deadline
    trip.advance(adapter)
    assert trip.waiting_for == "order_confirmation" and trip.failure is None
    assert trip.wait_reason == "order_status=SUBMITTED"
    clock.now = deadline + 29
    trip.advance(adapter)
    assert trip.failure is None and len(adapter.commands) == count and not adapter.cancels
    clock.now = deadline + 30
    trip.advance(adapter)
    state = trip.snapshot()
    assert "reconciliation deadline reached at order_confirmation" in state["failure"]
    assert state["active_client_order_id"] == identity and not state["done"]
    assert state["observed_filled_quantity"] == "0.01"
    assert Decimal(state["observed_position_quantity"]) == adapter.position
    assert adapter.position == Decimal("0.01" if phase == "entry" else "0.03")
    assert identity not in trip.recorded_ids and not trip.pending_reprice
    clock.now = 600
    trip.advance(adapter)
    assert trip.snapshot() == state and len(adapter.commands) == count and adapter.cancels == []


@pytest.mark.parametrize("phase", ["entry", "exit", "close-only"])
def test_pending_cancel_times_out_after_thirty_seconds_without_repeat_or_replacement(phase):
    trip, adapter, clock = controller_with_active_order(phase)
    identity = adapter.order_id
    count = len(adapter.commands)
    adapter.fill("0.01")
    clock.now = 5 if phase == "entry" else 10
    trip.advance(adapter)
    assert adapter.cancels == [identity] and trip.waiting_for == "cancel_confirmation"
    adapter.orders[identity].status = "PENDING_CANCEL"
    clock.now += 29
    trip.advance(adapter)
    assert trip.failure is None and trip.wait_reason == "order_status=PENDING_CANCEL"
    clock.now += 1
    trip.advance(adapter)
    state = trip.snapshot()
    assert "reconciliation deadline reached at cancel_confirmation" in state["failure"]
    assert "order_status=PENDING_CANCEL" in state["failure"]
    assert state["active_client_order_id"] == identity and state["cancel_requested"]
    assert not state["done"] and not trip.pending_reprice and identity not in trip.recorded_ids
    assert adapter.position == Decimal("0.01" if phase == "entry" else "0.03")
    assert Decimal(state["observed_position_quantity"]) == adapter.position
    clock.now = 600
    trip.advance(adapter)
    assert trip.snapshot() == state and len(adapter.commands) == count and adapter.cancels == [identity]


@pytest.mark.parametrize("pending", ["submit", "cancel"])
@pytest.mark.parametrize("phase", ["exit", "close-only"])
def test_reconciled_terminal_exit_at_confirmation_deadline_wins_over_timeout(phase, pending):
    trip, adapter, clock = controller_with_active_order(phase)
    identity = adapter.order_id
    if pending == "submit":
        adapter.orders[identity].status = "SUBMITTED"
    clock.now = 10
    trip.advance(adapter)
    assert trip.waiting_for == ("order_confirmation" if pending == "submit" else "cancel_confirmation")
    clock.now = 40
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.done and trip.failure is None and adapter.position == 0
    assert trip.waiting_for is None and trip.wait_reason is None
    assert trip.legs[-1]["client_order_id"] == identity and trip.legs[-1]["liquidity_verified"]
    assert len(adapter.commands) == 2 and adapter.cancels == ([] if pending == "submit" else [identity])
    assert trip.reprices == {"entry": 0, "exit": 0}


def test_reconciled_maker_fill_at_cancel_confirmation_deadline_can_start_market_exit():
    trip, adapter, clock = controller_with_active_order("entry")
    clock.now = 5
    trip.advance(adapter)
    assert trip.waiting_for == "cancel_confirmation"
    clock.now = 35
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.failure is None and trip.waiting_for is None and trip.phase == "exit"
    trip.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.04")
    assert adapter.cancels == ["owned-1"] and len(adapter.commands) == 2


@pytest.mark.parametrize("status", ["REJECTED", "DENIED", "EXPIRED", "UNKNOWN"])
def test_unexpected_terminal_never_retries(status):
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    clock.now = 5
    trip.advance(adapter)
    adapter.terminal(status)
    trip.advance(adapter)
    trip.advance(adapter)
    assert "unexpected terminal" in trip.failure and len(adapter.commands) == 1


def test_external_cancel_does_not_authorize_reprice():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    adapter.terminal()
    trip.advance(adapter)
    assert "no fills" in trip.failure and len(adapter.commands) == 1


def test_aggregate_entry_fills_plus_fresh_remaining_valuation_never_exceeds_five():
    trip, adapter, clock = fixture(quantity="0.049")
    trip.advance(adapter)
    cancel_and_reconcile(trip, adapter, clock, filled="0.02")
    adapter.prices = {"bid": "104", "ask": "105"}
    trip.advance(adapter)
    assert "valuation limit" in trip.failure
    assert len(adapter.commands) == 1


def test_fresh_market_exit_valuation_never_exceeds_five():
    trip, adapter, clock = fixture(quantity="0.049")
    trip.advance(adapter)
    adapter.fill("0.049")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    adapter.prices = {"bid": "104", "ask": "105"}
    trip.advance(adapter)
    assert "valuation limit" in trip.failure and len(adapter.commands) == 1
    assert adapter.position == Decimal("0.049") and not trip.done


def test_increased_configured_limit_cannot_exceed_hard_five_usdc_cap():
    trip, adapter, clock = fixture(quantity="0.05")
    trip = RoundTrip(trip.command, max_leg_usd=Decimal("10"), clock=clock)
    trip.advance(adapter)
    assert "valuation limit" in trip.failure and not adapter.commands


def test_actual_notional_is_aggregated_and_failure_is_retained_after_flattening():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    cancel_and_reconcile(trip, adapter, clock, filled="0.02")
    trip.advance(adapter)
    adapter.fill("0.02", price="151")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.snapshot()["aggregate_notional_usdc"]["entry"] == "5.02"
    trip.advance(adapter)
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.done and adapter.position == 0 and "actual fill notional" in trip.failure


@pytest.mark.parametrize("style", ["maker", "market"])
@pytest.mark.parametrize("exit_fill", ["0", "0.01"])
def test_market_ioc_exit_preserves_residual_and_fails_without_retry(style, exit_fill):
    trip, adapter, clock = fixture(style=style)
    trip.advance(adapter)
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    trip.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.04")
    if Decimal(exit_fill):
        adapter.fill(exit_fill)
    adapter.terminal()
    trip.advance(adapter)
    assert "residual" in trip.failure and adapter.position == Decimal("0.04") - Decimal(exit_fill)
    assert not trip.done and len(adapter.commands) == 2
    clock.now = 120
    trip.advance(adapter)
    assert adapter.cancels == [] and len(adapter.commands) == 2
    assert trip.reprices == {"entry": 0, "exit": 0}


@pytest.mark.parametrize("stage", ["open", "pending", "terminal"])
def test_stop_never_submits_replacement_or_exit(stage):
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    if stage in {"pending", "terminal"}:
        clock.now = 5
        trip.advance(adapter)
        adapter.fill("0.01")
        adapter.terminal()
        if stage == "pending":
            trip.advance(adapter)
    trip.stop_requested = True
    trip.advance(adapter)
    if stage == "open":
        trip.advance(adapter)
        assert adapter.cancels == ["owned-1"]
        adapter.terminal()
        trip.advance(adapter)
    trip.advance(adapter)
    assert "stopped" in trip.failure and len(adapter.commands) == 1


def test_position_must_match_confirmed_fills_at_replacement_submission():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    cancel_and_reconcile(trip, adapter, clock, filled="0.01")
    adapter.position = Decimal("0.02")
    trip.advance(adapter)
    assert trip.failure and len(adapter.commands) == 1


def test_economic_fill_mismatch_blocks_replacement():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    clock.now = 5
    trip.advance(adapter)
    adapter.orders[adapter.order_id].filled_qty = Decimal("0.01")
    adapter.position = Decimal("0.01")
    adapter.terminal()
    trip.advance(adapter)
    assert "economics did not reconcile" in trip.failure and len(adapter.commands) == 1


@pytest.mark.parametrize("style", ["maker", "market"])
def test_absent_reprice_options_keep_original_single_attempt_per_side(style):
    trip, adapter, clock = fixture(style=style)
    del trip.command["maker_reprice_seconds"]
    del trip.command["maker_max_reprices"]
    trip = RoundTrip(trip.command, clock=clock)
    trip.advance(adapter)
    clock.now = 5
    trip.advance(adapter)
    assert adapter.cancels == []
    adapter.fill("0.02")
    adapter.terminal()
    trip.advance(adapter)
    trip.advance(adapter)
    assert adapter.commands[-1]["quantity"] == "0.02"
    adapter.fill("0.02")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.done and trip.failure is None and len(adapter.commands) == 2


def test_market_ignores_maker_reprice_options():
    trip, adapter, clock = fixture(style="market", wait_seconds=30)
    trip.advance(adapter)
    clock.now = 5
    trip.advance(adapter)
    assert adapter.cancels == [] and adapter.commands[-1]["order_type"] == "market"
    clock.now = 30
    trip.advance(adapter)
    adapter.terminal()
    trip.advance(adapter)
    assert "no fills" in trip.failure and len(adapter.commands) == 1


@pytest.mark.parametrize("case", ["zero_entry", "partial_exit"])
def test_legacy_zero_fill_and_partial_exit_fail_without_another_order(case):
    trip, adapter, clock = fixture(maker_reprice_seconds=0, maker_max_reprices=0)
    trip.advance(adapter)
    if case == "zero_entry":
        adapter.terminal()
        trip.advance(adapter)
        assert "no fills" in trip.failure and len(adapter.commands) == 1
        return
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    trip.advance(adapter)
    adapter.fill("0.01")
    adapter.terminal()
    trip.advance(adapter)
    assert "residual" in trip.failure and len(adapter.commands) == 2


def test_close_only_already_flat_stays_a_confirmed_noop():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    adapter.fill("0.03")
    adapter.terminal()
    adapter.position = Decimal("0")
    close = ClosePosition({"action": "close-only", "symbol": "BTC_USDC_PERP", "style": "maker",
        "entry_client_order_id": adapter.order_id, "max_quantity": "0.03", "wait_seconds": 5}, clock=clock)
    close.advance(adapter)
    snapshot = close.snapshot()
    close.advance(adapter)
    assert close.done and close.failure is None and snapshot == close.snapshot()
    assert close.legs == [] and len(adapter.commands) == 1


@pytest.mark.parametrize("style", ["maker", "market"])
def test_close_only_uses_market_exit_for_original_proven_owned_amount(style):
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    adapter.fill("0.03")
    adapter.terminal()
    original = adapter.order_id
    command = {"action": "close-only", "symbol": "BTC_USDC_PERP", "style": style,
        "entry_client_order_id": original, "max_quantity": "0.04", "wait_seconds": 30,
        "maker_reprice_seconds": 5, "maker_max_reprices": 2}
    adapter.check_close_origin = lambda symbol, identity: None
    close = ClosePosition(command, clock=clock)
    close.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.03")
    clock.now = 5
    close.advance(adapter)
    close.advance(adapter)
    assert adapter.cancels == [] and len(adapter.commands) == 2
    adapter.fill("0.03")
    adapter.terminal("FILLED")
    close.advance(adapter)
    assert close.done and close.failure is None
    assert close.reprices == {"entry": 0, "exit": 0}
    assert close.legs[0]["order_style"] == "market" and close.legs[0]["liquidity_verified"]


@pytest.mark.parametrize("style", ["maker", "market"])
@pytest.mark.parametrize("entry_fill", ["0.02", "0.04"])
def test_confirmed_entry_automatically_submits_market_reduce_only_exit(style, entry_fill):
    trip, adapter, clock = fixture(style=style)
    trip.advance(adapter)
    assert adapter.commands[0]["order_type"] == ("limit" if style == "maker" else "market")
    assert adapter.commands[0]["post_only"] == (style == "maker")
    adapter.fill(entry_fill)
    adapter.terminal("FILLED" if entry_fill == "0.04" else "CANCELED")
    trip.advance(adapter)
    trip.advance(adapter)
    assert_market_exit(adapter.commands[-1], entry_fill)
    clock.now += 5
    trip.advance(adapter)
    trip.advance(adapter)
    assert adapter.cancels == [] and len(adapter.commands) == 2
    adapter.fill(entry_fill)
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.done and trip.failure is None and adapter.position == 0
    assert [leg["order_style"] for leg in trip.legs] == [style, "market"]
    assert all(leg["liquidity_verified"] for leg in trip.legs)
    assert trip.legs[-1]["fills"][0]["liquidity_side"] == "TAKER"
    assert trip.snapshot()["entry_style"] == style and trip.snapshot()["exit_style"] == "market"


def test_maker_liquidity_report_on_market_exit_retains_failure_even_when_flat():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    trip.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.04")
    adapter.fill("0.04")
    adapter.events[-1]["liquidity_side"] = "MAKER"
    adapter.terminal("FILLED")
    trip.advance(adapter)
    assert trip.done and adapter.position == 0
    assert "liquidity evidence" in trip.failure and not trip.legs[-1]["liquidity_verified"]


@pytest.mark.parametrize("style", ["maker", "market"])
def test_uncertain_market_exit_has_ten_second_deadline_and_never_reposts(style):
    trip, adapter, clock = fixture(style=style, wait_seconds=120, maker_max_reprices=23)
    trip.advance(adapter)
    adapter.fill("0.04")
    adapter.terminal("FILLED")
    trip.advance(adapter)
    clock.now = 7
    trip.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.04")
    clock.now = 12
    trip.advance(adapter)
    assert adapter.cancels == []
    clock.now = 16
    trip.advance(adapter)
    assert adapter.cancels == []
    clock.now = 17
    trip.advance(adapter)
    trip.advance(adapter)
    assert adapter.cancels == ["owned-2"] and len(adapter.commands) == 2
    adapter.fill("0.01")
    adapter.terminal()
    trip.advance(adapter)
    trip.advance(adapter)
    assert "residual" in trip.failure and adapter.position == Decimal("0.03")
    assert len(adapter.commands) == 2 and trip.reprices == {"entry": 0, "exit": 0}
    assert trip.legs[-1]["cancel_reason"] == "deadline"


def test_legacy_maker_close_only_partial_market_ioc_reports_residual_without_repost():
    trip, adapter, clock = fixture()
    trip.advance(adapter)
    adapter.fill("0.03")
    adapter.terminal()
    adapter.check_close_origin = lambda symbol, identity: None
    close = ClosePosition({"action": "close-only", "symbol": "BTC_USDC_PERP", "style": "maker",
        "entry_client_order_id": adapter.order_id, "max_quantity": "0.03", "wait_seconds": 120,
        "maker_reprice_seconds": 5, "maker_max_reprices": 23}, clock=clock)
    close.advance(adapter)
    assert_market_exit(adapter.commands[-1], "0.03")
    adapter.fill("0.01")
    adapter.terminal()
    close.advance(adapter)
    clock.now = 120
    close.advance(adapter)
    assert not close.done and "residual" in close.failure and adapter.position == Decimal("0.02")
    assert len(adapter.commands) == 2 and adapter.cancels == []
    assert close.snapshot()["style"] == "market" and close.snapshot()["entry_style"] is None

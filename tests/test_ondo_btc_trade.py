"""BTC-only offline checks; all quotes, orders and fills here are synthetic.

The public BTC capture on 2026-09-22 reports price/size grids of 1 / 0.0001.
Prices below are deterministic fixtures, never an executable production plan.
Reuse the existing orchestration fixtures without changing DMS or acceptance gates.
"""

from decimal import Decimal
from pathlib import Path
import contextlib
import io
import json
import time

import pytest

from test_ondo_trade_probe import (
    CapableAdapter,
    FakeStrategy,
    clean_final,
    good_readiness,
    resolved_plan,
    trade,
)
from ondo_trade_limits import load_trade_limits
from nautilus_trader.adapters.ondo import OndoExecutionEnvelopeConfig
from nautilus_trader.model import InstrumentId


BTC_LIMITS = Path(__file__).resolve().parents[1] / "config/ondo_btc_test.toml"


@pytest.fixture
def btc_limits():
    limits = load_trade_limits(BTC_LIMITS)
    assert limits.limits_configured, "The explicit BTC profile must exist and be complete"
    assert limits.symbol == "BTC"
    assert limits.instrument == "BTC-USD-PERP.ONDO"
    return limits


def btc_document(side="buy", **overrides):
    values = dict(
        instrument={
            "symbol": "BTC", "instrument_id": "BTC-USD-PERP.ONDO",
            "price_increment": "1", "size_increment": "0.0001",
        },
        entry={
            "side": side, "quantity": "0.0002",
            "limit_price": "50100" if side == "buy" else "49900",
        },
        close={"side": "sell" if side == "buy" else "buy",
               "limit_price": "49900" if side == "buy" else "50100"},
        envelope={"max_notional_per_order_usd": "20",
                  "max_gross_exposure_usd": "20", "deadline_secs": 120},
        cleanup={"stop_budget_secs": 15},
    )
    for key, block in overrides.items():
        values.setdefault(key, {}).update(block)
    return resolved_plan(**values)


def entry_started(limits, side="buy"):
    plan = trade.parse_trade_plan(btc_document(side), limits)
    sequencer = trade.TradeSequencer(plan, limits)
    sequencer.begin()
    sequencer.on_quote(Decimal("49999"), Decimal("50000"), now=0)
    decision = sequencer.decide_entry(now=0)
    assert decision is not None
    assert decision.quantity == Decimal("0.0002")
    assert decision.limit_price % Decimal("1") == 0
    assert decision.reduce_only is False
    sequencer.bind_entry("BTC-ENTRY")
    return sequencer


def fill_entry(sequencer, quantity="0.0002"):
    return sequencer.note_entry(trade.OrderView(
        "entry", "BTC-ENTRY", "FILLED" if quantity == "0.0002" else "CANCELED",
        Decimal(quantity), Decimal("50000"), True,
    ), now=0)


def fill_close(sequencer, quantity="0.0002"):
    sequencer.bind_close("BTC-CLOSE")
    return sequencer.note_close(trade.OrderView(
        "close", "BTC-CLOSE", "FILLED", Decimal(quantity), Decimal("49999"), True,
    ), now=0)


def test_btc_profile_is_explicit_bounded_and_does_not_change_default(btc_limits):
    assert btc_limits.entry_notional_usd == Decimal("15")
    assert btc_limits.max_notional_per_order_usd == Decimal("20")
    assert btc_limits.max_gross_exposure_usd == Decimal("20")
    assert btc_limits.deadline_secs == 120
    assert btc_limits.cleanup_budget_secs == 15
    assert btc_limits.max_new_risk_requests == 1
    assert btc_limits.max_close_attempts == 2
    assert load_trade_limits(BTC_LIMITS.with_name("limits.toml")).symbol == "NVDA"


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_btc_full_cycle_uses_opposite_reduce_only_close(btc_limits, side):
    sequencer = entry_started(btc_limits, side)
    close = fill_entry(sequencer)
    assert close is not None
    assert close.side != side
    assert close.reduce_only is True
    assert close.quantity == Decimal("0.0002")
    assert close.limit_price % Decimal("1") == 0
    assert fill_close(sequencer) is None
    assert sequencer.net_position == 0
    assert sequencer.outcome == trade.OUTCOME_FILLED
    assert sequencer.entry_attempts == 1
    assert sequencer.close_attempts == 1


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_btc_partial_fill_closes_exactly_one_lot(btc_limits, side):
    sequencer = entry_started(btc_limits, side)
    close = fill_entry(sequencer, "0.0001")
    assert close is not None and close.quantity == Decimal("0.0001")
    assert close.reduce_only is True
    fill_close(sequencer, "0.0001")
    assert sequencer.net_position == 0
    assert sequencer.outcome == trade.OUTCOME_PARTIAL


@pytest.mark.parametrize("status,expected", [
    ("REJECTED", trade.OUTCOME_REJECTED), ("CANCELED", trade.OUTCOME_NO_TRADE),
])
def test_btc_zero_fill_never_sends_close(btc_limits, status, expected):
    sequencer = entry_started(btc_limits)
    close = sequencer.note_entry(trade.OrderView(
        "entry", "BTC-ENTRY", status, Decimal("0"), None, True,
        "synthetic rejection detail" if status == "REJECTED" else None,
    ), now=0)
    assert close is None
    assert sequencer.outcome == expected
    assert sequencer.close_attempts == 0
    if status == "REJECTED":
        assert "synthetic rejection detail" in sequencer.terminal_reason


@pytest.mark.parametrize("block,change", [
    ("entry", {"quantity": "0.00015"}),
    ("entry", {"limit_price": "50100.5"}),
    ("close", {"limit_price": "49900.5"}),
    ("close", {"reduce_only": False}),
    ("envelope", {"max_notional_per_order_usd": "21"}),
    ("instrument", {"instrument_id": "NVDA-USD-PERP.ONDO"}),
])
def test_btc_malformed_or_widened_plan_is_refused(btc_limits, block, change):
    with pytest.raises(trade.TradePlanError):
        trade.parse_trade_plan(btc_document(**{block: change}), btc_limits)


def test_btc_unknown_entry_is_never_retried(btc_limits):
    sequencer = entry_started(btc_limits)
    sequencer.note_submit_error(trade.ROLE_ENTRY, "synthetic disconnect")
    assert sequencer.decide_entry(now=1) is None
    assert sequencer.outcome == trade.OUTCOME_UNCERTAIN
    assert sequencer.entry_attempts == 1
    assert sequencer.close_attempts == 0


def test_btc_residual_remains_uncertain_after_bounded_closes(btc_limits):
    sequencer = entry_started(btc_limits)
    fill_entry(sequencer)
    for number in (1, 2):
        client_id = f"BTC-CLOSE-{number}"
        sequencer.bind_close(client_id)
        sequencer.note_close(trade.OrderView(
            "close", client_id, "CANCELED", Decimal("0"), None, True,
        ), now=0)
    assert sequencer.close_attempts == 2
    assert abs(sequencer.net_position) == Decimal("0.0002")
    assert sequencer.outcome == trade.OUTCOME_UNCERTAIN


@pytest.mark.parametrize("shutdown", ["incomplete", "clean"])
def test_btc_fills_do_not_hide_incomplete_shutdown(btc_limits, shutdown):
    sequencer = entry_started(btc_limits)
    fill_entry(sequencer)
    fill_close(sequencer)
    acceptance = trade.acceptance_document(
        FakeStrategy(sequencer), sequencer.plan,
        trade.detect_native_trade_capability(CapableAdapter()), good_readiness(),
        clean_final(instrument_id="BTC-USD-PERP.ONDO", shutdown_status=shutdown),
    )
    assert acceptance["entry_confirmed"] is True
    assert acceptance["close_confirmed"] is True
    assert acceptance["flat_reconciled"] is True
    assert acceptance["clean"] is (shutdown == "clean")


def test_btc_dry_run_never_reads_credentials_or_constructs_client(btc_limits, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("An offline BTC intent must not read credentials or build a node")
    monkeypatch.setattr(trade, "_load_environment", forbidden)
    output = io.StringIO()
    with contextlib.redirect_stdout(output):
        code = trade.main(
            ["--mode", "production-trade", "--side", "buy", "--dry-run",
             "--limits", str(BTC_LIMITS)],
            adapter=CapableAdapter(), environ={}, node_factory=forbidden,
        )
    assert code == 0
    report = json.loads(output.getvalue())
    assert report["client_constructed"] is False
    assert report["env_file_read"] is False
    assert report["requests_sent"] == 0
    assert report["production_execution_verified"] is False
    assert report["live_execution_ready"] is False


def native_envelope_kwargs(side="buy"):
    """Construct only a frozen native DTO: no client, credential, journal or socket."""
    now = time.time_ns()
    return dict(
        instrument_id=InstrumentId.from_str("BTC-USD-PERP.ONDO"),
        entry_side=side, entry_max_quantity="0.0002",
        entry_worst_price="50100" if side == "buy" else "49900",
        entry_max_notional_usd="15", close_side="sell" if side == "buy" else "buy",
        close_max_quantity="0.0002",
        close_worst_price="49900" if side == "buy" else "50100",
        max_close_attempts=2, max_notional_per_order_usd="20",
        max_gross_exposure_usd="20", min_available_margin_usdc="25",
        max_orders=3, max_new_risk_requests=1, max_app_requests=6,
        entry_deadline_unix_nanos=now + 105_000_000_000,
        cleanup_deadline_unix_nanos=now + 120_000_000_000,
        require_flat_start=True,
    )


@pytest.mark.parametrize("side", ["buy", "sell"])
def test_installed_native_envelope_accepts_bounded_btc_without_network(side):
    assert OndoExecutionEnvelopeConfig(**native_envelope_kwargs(side)) is not None


@pytest.mark.parametrize("instrument", ["ETH-USD-PERP.ONDO", "BTC-USD-PERP.ASTER"])
def test_installed_native_envelope_does_not_open_unrequested_markets(instrument):
    values = native_envelope_kwargs()
    values["instrument_id"] = InstrumentId.from_str(instrument)
    with pytest.raises(ValueError, match="production execution envelope"):
        OndoExecutionEnvelopeConfig(**values)


@pytest.mark.parametrize("key,value", [
    ("max_notional_per_order_usd", "51"),
    ("max_new_risk_requests", 2),
    ("require_flat_start", False),
])
def test_installed_native_btc_envelope_retains_hard_safety_rails(key, value):
    values = native_envelope_kwargs()
    values[key] = value
    with pytest.raises(ValueError, match="production execution envelope"):
        OndoExecutionEnvelopeConfig(**values)

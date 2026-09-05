#!/usr/bin/env python3
"""
Offline regression tests for ``src/exec_probe.py`` (Aster testnet execution probe).

Everything here runs without a network, without credentials and without a LiveNode: the
strategy callbacks are driven with fake events and a stubbed cache / order factory.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import contextlib
import io
import os
import sys
import threading
import unittest
from decimal import Decimal
from pathlib import Path
from unittest import mock


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import exec_probe  # noqa: E402
from nautilus_trader.model import ClientOrderId  # noqa: E402
from nautilus_trader.model import CryptoPerpetual  # noqa: E402
from nautilus_trader.model import Currency  # noqa: E402
from nautilus_trader.model import InstrumentId  # noqa: E402
from nautilus_trader.model import Money  # noqa: E402
from nautilus_trader.model import Price  # noqa: E402
from nautilus_trader.model import Quantity  # noqa: E402
from nautilus_trader.model import Symbol  # noqa: E402
from nautilus_trader.trading import Strategy  # noqa: E402


USDT = Currency.from_str("USDT")
BTC = Currency.from_str("BTC")
PROBE_ID = InstrumentId.from_str("BTCUSDT-PERP.ASTER")


# --------------------------------------------------------------------------------- fixtures


def make_instrument(
    *,
    price_increment: str = "0.1",
    size_increment: str = "0.00001",
    min_quantity: str | None = "0.00001",
    min_notional: str | None = "5",
    maker_fee: str = "0.0002",
    taker_fee: str = "0.0005",
    instrument_id: InstrumentId = PROBE_ID,
    info: dict | None = None,
) -> CryptoPerpetual:
    """
    Build a real CryptoPerpetual with the given filters (no network, no adapter).
    """
    return CryptoPerpetual(
        instrument_id=instrument_id,
        raw_symbol=Symbol(str(instrument_id.symbol)),
        base_currency=BTC,
        quote_currency=USDT,
        settlement_currency=USDT,
        is_inverse=False,
        price_precision=len(price_increment.split(".")[-1]) if "." in price_increment else 0,
        size_precision=len(size_increment.split(".")[-1]) if "." in size_increment else 0,
        price_increment=Price.from_str(price_increment),
        size_increment=Quantity.from_str(size_increment),
        ts_event=0,
        ts_init=0,
        min_quantity=Quantity.from_str(min_quantity) if min_quantity is not None else None,
        min_notional=Money(Decimal(min_notional), USDT) if min_notional is not None else None,
        maker_fee=Decimal(maker_fee),
        taker_fee=Decimal(taker_fee),
        info=info,
    )


class FakeStatus:
    """
    Stand-in for ``OrderStatus`` exposing only what the probe reads.
    """

    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name


class FakeOrder:
    """
    Stand-in for a Nautilus order in the cache.
    """

    def __init__(
        self,
        client_order_id: ClientOrderId,
        quantity: Quantity,
        price: Price,
        *,
        status: str = "ACCEPTED",
        filled_qty: str = "0",
        is_closed: bool = False,
    ) -> None:
        self.client_order_id = client_order_id
        self.quantity = quantity
        self.price = price
        self.status = FakeStatus(status)
        self.filled_qty = Quantity.from_str(filled_qty)
        self.is_closed = is_closed
        self.venue_order_id = "V-1"

    def close(self, status: str, filled_qty: str = "0") -> None:
        self.status = FakeStatus(status)
        self.filled_qty = Quantity.from_str(filled_qty)
        self.is_closed = True


class FakeEvent:
    """
    Stand-in for the order events the probe handles.
    """

    def __init__(self, client_order_id: ClientOrderId, **fields: object) -> None:
        self.client_order_id = client_order_id
        self.ts_event = 1_700_000_000_000_000_000
        self.reason = "test reason"
        for key, value in fields.items():
            setattr(self, key, value)


class FakeQuote:
    """
    Stand-in for a QuoteTick (real Price objects, so rounding behaves like production).
    """

    def __init__(self, bid: str, ask: str) -> None:
        self.instrument_id = PROBE_ID
        self.bid_price = Price.from_str(bid)
        self.ask_price = Price.from_str(ask)
        self.ts_event = 1_700_000_000_000_000_000


class FakeCache:
    """
    Minimal cache: instruments by id, orders by client order id, one quote.
    """

    def __init__(self, instruments=(), quote: FakeQuote | None = None) -> None:
        self.instruments = {inst.id: inst for inst in instruments}
        self.orders: dict[ClientOrderId, FakeOrder] = {}
        self._quote = quote
        self.raise_on_order = False

    def instrument(self, instrument_id):
        return self.instruments.get(instrument_id)

    def order(self, client_order_id):
        if self.raise_on_order:
            raise ValueError("cache blew up")
        return self.orders.get(client_order_id)

    def quote(self, instrument_id, index: int = 0):
        return self._quote


class FakeOrderFactory:
    """
    Hands out FakeOrders and records the submitted parameters.
    """

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._counter = 0

    def limit(self, **kwargs) -> FakeOrder:
        self._counter += 1
        self.calls.append(kwargs)
        return FakeOrder(
            ClientOrderId(f"O-{self._counter}"),
            kwargs["quantity"],
            kwargs["price"],
        )


class ProbeUnderTest(exec_probe.AsterProbeStrategy):
    """
    The production strategy with the trader-owned surfaces replaced by stubs.

    Only ``cache`` / ``order_factory`` / ``submit_order`` / ``cancel_order`` /
    ``subscribe_quotes`` are stubbed - every state transition under test is production code.
    """

    def __new__(cls, config, *_args: object, **_kwargs: object):
        # The pyo3 base __new__ only accepts the config; drop the test-double arguments.
        return super().__new__(cls, config)

    def __init__(self, config, cache: FakeCache, factory: FakeOrderFactory) -> None:
        super().__init__(config)
        self._stub_cache = cache
        self._stub_factory = factory
        self.submitted: list[FakeOrder] = []
        self.cancel_calls: list[object] = []
        self.subscriptions: list[InstrumentId] = []
        # Client order ids whose cancel must raise, mirroring the native cancel_order
        # refusing a tracked id that is not in the cache.
        self.cancel_raises_for: set[object] = set()

    @property
    def cache(self) -> FakeCache:
        return self._stub_cache

    @property
    def order_factory(self) -> FakeOrderFactory:
        return self._stub_factory

    def subscribe_quotes(self, instrument_id, client_id=None, params=None) -> None:
        self.subscriptions.append(instrument_id)

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.submitted.append(order)
        self._stub_cache.orders[order.client_order_id] = order

    def cancel_order(self, client_order_id, client_id=None, params=None) -> None:
        if client_order_id in self.cancel_raises_for:
            raise RuntimeError(
                f"Cannot cancel order: order not found in cache: {client_order_id}",
            )
        self.cancel_calls.append(client_order_id)


def build_probe(
    instrument: CryptoPerpetual | None = None,
    *,
    quote: FakeQuote | None = None,
    commission_ids: tuple[InstrumentId, ...] = (),
) -> ProbeUnderTest:
    """
    Return a probe wired to a stub cache holding ``instrument`` and ``quote``.
    """
    instrument = instrument if instrument is not None else make_instrument()
    quote = quote if quote is not None else FakeQuote("100000.0", "100000.1")
    cache = FakeCache(instruments=(instrument,), quote=quote)
    config = exec_probe.AsterProbeConfig(
        instrument_id=instrument.id,
        commission_instrument_ids=commission_ids,
    )
    return ProbeUnderTest(config, cache, FakeOrderFactory())


def run_to_ioc(probe: ProbeUnderTest, quote: FakeQuote | None = None) -> tuple[FakeOrder, FakeOrder]:
    """
    Drive the probe through step 1 (rest, accept, cancel, terminal) into step 2.

    Returns the resting order and the IOC order.
    """
    quote = quote if quote is not None else FakeQuote("100000.0", "100000.1")
    probe.on_start()
    probe.on_quote(quote)
    resting = probe.submitted[0]
    probe.on_order_accepted(FakeEvent(resting.client_order_id))
    resting.close("CANCELED")
    probe.on_order_canceled(FakeEvent(resting.client_order_id))
    return resting, probe.submitted[1]


# ------------------------------------------------------------------- the live cancel bug


class TestCancelOrderApi(unittest.TestCase):
    """
    The live failure: ``cancel_order(order)`` is the ExecutionAlgorithm API, not Strategy's.
    """

    def test_strategy_cancel_order_rejects_an_order_object(self) -> None:
        config = exec_probe.AsterProbeConfig(instrument_id=PROBE_ID)
        strategy = exec_probe.AsterProbeStrategy(config)
        order = FakeOrder(ClientOrderId("O-1"), Quantity.from_str("0.001"), Price.from_str("1.0"))
        with self.assertRaises(TypeError) as ctx:
            Strategy.cancel_order(strategy, order)
        self.assertIn("ClientOrderId", str(ctx.exception))

    def test_strategy_cancel_order_accepts_a_client_order_id(self) -> None:
        config = exec_probe.AsterProbeConfig(instrument_id=PROBE_ID)
        strategy = exec_probe.AsterProbeStrategy(config)
        # Argument extraction passes; it only fails later because we are not registered.
        with self.assertRaises(RuntimeError):
            Strategy.cancel_order(strategy, ClientOrderId("O-1"))

    def test_probe_cancels_with_client_order_id(self) -> None:
        probe = build_probe()
        probe.on_start()
        probe.on_quote(FakeQuote("100000.0", "100000.1"))
        resting = probe.submitted[0]
        probe.on_order_accepted(FakeEvent(resting.client_order_id))
        self.assertEqual(len(probe.cancel_calls), 1)
        self.assertIsInstance(probe.cancel_calls[0], ClientOrderId)
        self.assertEqual(probe.cancel_calls[0], resting.client_order_id)
        self.assertEqual(probe.failures, [])


class TestCallbackGuard(unittest.TestCase):
    """
    Callback exceptions are swallowed by the framework; the probe must log and fail instead.
    """

    def test_exception_in_callback_records_failure_and_finishes(self) -> None:
        probe = build_probe()
        probe.on_start()
        probe.on_quote(FakeQuote("100000.0", "100000.1"))
        probe.cache.raise_on_order = True
        probe.on_order_accepted(FakeEvent(probe.submitted[0].client_order_id))
        self.assertEqual(len(probe.failures), 1)
        self.assertIn("unhandled exception in on_order_accepted", probe.failures[0])
        self.assertIn("ValueError: cache blew up", probe.failures[0])
        self.assertTrue(probe.done_event.is_set())
        self.assertEqual(probe.exit_code, exec_probe.EXIT_PROBE_FAILED)


# ----------------------------------------------------------------------------- F12 sizing


class TestPlanOrder(unittest.TestCase):
    """
    F12: quantity is derived from the rounded limit price actually sent.
    """

    def test_review_counter_example_now_clears_min_notional(self) -> None:
        # Review counter-example: bid 100000, min qty and step both 0.00001, limit = bid * 0.5.
        instrument = make_instrument(size_increment="0.00001", min_quantity="0.00001",
                                     min_notional=None)
        plan = exec_probe.plan_order(instrument, Decimal("100000") * exec_probe.RESTING_PRICE_FACTOR)
        self.assertEqual(plan.price.as_decimal(), Decimal("50000.0"))
        self.assertEqual(plan.quantity.as_decimal(), Decimal("0.00010"))
        self.assertEqual(plan.notional, Decimal("5.00000"))
        self.assertGreaterEqual(plan.notional, exec_probe.MIN_NOTIONAL_FALLBACK_USDT)
        self.assertLessEqual(plan.notional, exec_probe.MAX_NOTIONAL_USDT)

    def test_old_behaviour_would_have_underfunded_the_order(self) -> None:
        # Sizing from the bid instead of the rounded limit gives 0.00005 -> 2.5 USDT.
        instrument = make_instrument(size_increment="0.00001", min_quantity="0.00001",
                                     min_notional=None)
        plan = exec_probe.plan_order(instrument, Decimal("100000") * exec_probe.RESTING_PRICE_FACTOR)
        underfunded = Decimal("0.00005") * plan.price.as_decimal()
        self.assertEqual(underfunded, Decimal("2.50000"))
        self.assertGreater(plan.notional, underfunded)

    def test_btc_like_step_within_budget(self) -> None:
        instrument = make_instrument(size_increment="0.001", min_quantity="0.001",
                                     min_notional="5")
        plan = exec_probe.plan_order(instrument, Decimal("10000") * exec_probe.RESTING_PRICE_FACTOR)
        self.assertEqual(plan.quantity.as_decimal(), Decimal("0.001"))
        self.assertEqual(plan.notional, Decimal("5.000"))

    def test_btc_like_step_above_budget_is_refused(self) -> None:
        # step 0.001 at a 50000 limit is 50 USDT: above the 20 USDT probe cap.
        instrument = make_instrument(size_increment="0.001", min_quantity="0.001",
                                     min_notional="5")
        with self.assertRaises(exec_probe.ProbeSizingError) as ctx:
            exec_probe.plan_order(instrument, Decimal("100000") * exec_probe.RESTING_PRICE_FACTOR)
        self.assertIn("above the probe budget", str(ctx.exception))
        self.assertIn("refusing to size up", str(ctx.exception))

    def test_instrument_min_notional_beats_the_fallback(self) -> None:
        instrument = make_instrument(size_increment="0.00001", min_quantity="0.00001",
                                     min_notional="12")
        plan = exec_probe.plan_order(instrument, Decimal("50000"))
        self.assertGreaterEqual(plan.notional, Decimal("12"))
        self.assertEqual(plan.min_notional, Decimal("12.00000000"))

    def test_min_notional_above_the_cap_is_refused(self) -> None:
        instrument = make_instrument(size_increment="0.00001", min_quantity="0.00001",
                                     min_notional="50")
        with self.assertRaises(exec_probe.ProbeSizingError) as ctx:
            exec_probe.plan_order(instrument, Decimal("50000"))
        self.assertIn("above the probe budget", str(ctx.exception))

    def test_price_is_rounded_before_sizing(self) -> None:
        # price_increment 1 -> the 50000.9 limit rounds to 50001 and sizing uses that.
        instrument = make_instrument(price_increment="1", size_increment="0.00001",
                                     min_quantity="0.00001", min_notional="5")
        plan = exec_probe.plan_order(instrument, Decimal("50000.9"))
        self.assertEqual(plan.price.as_decimal(), Decimal("50001"))
        self.assertEqual(plan.notional, plan.quantity.as_decimal() * Decimal("50001"))
        self.assertGreaterEqual(plan.notional, Decimal("5"))

    def test_non_positive_price_is_refused(self) -> None:
        instrument = make_instrument(price_increment="1")
        with self.assertRaises(exec_probe.ProbeSizingError) as ctx:
            exec_probe.plan_order(instrument, Decimal("0.4"))
        self.assertIn("rounds to", str(ctx.exception))

    def test_probe_refuses_and_fails_when_sizing_is_impossible(self) -> None:
        instrument = make_instrument(size_increment="0.001", min_quantity="0.001",
                                     min_notional="5")
        probe = build_probe(instrument)
        probe.on_start()
        probe.on_quote(FakeQuote("100000.0", "100000.1"))
        self.assertEqual(probe.submitted, [])
        self.assertEqual(len(probe.failures), 1)
        self.assertIn("step 1 sizing refused", probe.failures[0])
        self.assertTrue(probe.done_event.is_set())
        self.assertEqual(probe.exit_code, exec_probe.EXIT_PROBE_FAILED)


# ------------------------------------------------------------------- F10 terminal handling


class TestIocTerminalHandling(unittest.TestCase):
    """
    F10: the state machine follows the order's real terminal state, and finishes once.
    """

    def _commission_ids(self) -> tuple[InstrumentId, ...]:
        return (PROBE_ID,)

    def test_zero_fill_cancel_finishes_once(self) -> None:
        probe = build_probe(commission_ids=self._commission_ids())
        _, ioc = run_to_ioc(probe)
        ioc.close("CANCELED")
        probe.on_order_canceled(FakeEvent(ioc.client_order_id))
        self.assertTrue(probe.finished)
        self.assertEqual(probe.failures, [])
        self.assertEqual(probe.exit_code, exec_probe.EXIT_OK)
        self.assertIn("fills=0", probe.summary_line)
        # A duplicate terminal event must not finish a second time.
        summary = probe.summary_line
        probe.on_order_canceled(FakeEvent(ioc.client_order_id))
        self.assertEqual(probe.summary_line, summary)

    def test_zero_fill_expiry_finishes_once(self) -> None:
        probe = build_probe(commission_ids=self._commission_ids())
        _, ioc = run_to_ioc(probe)
        ioc.close("EXPIRED")
        probe.on_order_expired(FakeEvent(ioc.client_order_id))
        self.assertTrue(probe.finished)
        self.assertEqual(probe.exit_code, exec_probe.EXIT_OK)

    def test_partial_fill_does_not_finish_until_the_remainder_closes(self) -> None:
        probe = build_probe(commission_ids=self._commission_ids())
        _, ioc = run_to_ioc(probe)
        ioc.status = FakeStatus("PARTIALLY_FILLED")
        ioc.filled_qty = Quantity.from_str("0.00002")
        probe.on_order_filled(
            FakeEvent(
                ioc.client_order_id,
                last_qty=Quantity.from_str("0.00002"),
                last_px=Price.from_str("100000.1"),
                commission=Money(Decimal("0.001"), USDT),
                liquidity_side="TAKER",
            ),
        )
        self.assertFalse(probe.finished)  # must not declare done on a partial fill
        ioc.close("CANCELED", filled_qty="0.00002")
        probe.on_order_canceled(FakeEvent(ioc.client_order_id))
        self.assertTrue(probe.finished)
        self.assertEqual(probe.failures, [])
        self.assertIn("fills=1", probe.summary_line)
        self.assertIn("USDT=0.001", probe.summary_line)

    def test_full_fill_finishes_with_accumulated_commissions(self) -> None:
        probe = build_probe(commission_ids=self._commission_ids())
        _, ioc = run_to_ioc(probe)
        ioc.close("FILLED", filled_qty=str(ioc.quantity))
        probe.on_order_filled(
            FakeEvent(
                ioc.client_order_id,
                last_qty=ioc.quantity,
                last_px=Price.from_str("100000.1"),
                commission=Money(Decimal("0.0025"), USDT),
                liquidity_side="TAKER",
            ),
        )
        self.assertTrue(probe.finished)
        self.assertEqual(probe.exit_code, exec_probe.EXIT_OK)
        self.assertIn("fills=1", probe.summary_line)
        self.assertIn("USDT=0.0025", probe.summary_line)
        self.assertIn("fee_lines=1", probe.summary_line)

    def test_rejected_ioc_is_a_failure(self) -> None:
        probe = build_probe()
        _, ioc = run_to_ioc(probe)
        probe.on_order_rejected(FakeEvent(ioc.client_order_id, reason="insufficient margin"))
        self.assertTrue(probe.finished)
        self.assertEqual(probe.exit_code, exec_probe.EXIT_PROBE_FAILED)
        self.assertIn("OrderRejected", probe.failures[0])
        self.assertIn("result=failed", probe.summary_line)

    def test_denied_resting_order_is_a_failure(self) -> None:
        probe = build_probe()
        probe.on_start()
        probe.on_quote(FakeQuote("100000.0", "100000.1"))
        resting = probe.submitted[0]
        probe.on_order_denied(FakeEvent(resting.client_order_id, reason="exceeds max notional"))
        self.assertTrue(probe.finished)
        self.assertEqual(probe.exit_code, exec_probe.EXIT_PROBE_FAILED)
        self.assertIn("OrderDenied", probe.failures[0])

    def test_cancel_rejected_is_a_failure(self) -> None:
        probe = build_probe()
        probe.on_start()
        probe.on_quote(FakeQuote("100000.0", "100000.1"))
        resting = probe.submitted[0]
        probe.on_order_accepted(FakeEvent(resting.client_order_id))
        probe.on_order_cancel_rejected(FakeEvent(resting.client_order_id, reason="unknown order"))
        self.assertTrue(probe.finished)
        self.assertEqual(probe.exit_code, exec_probe.EXIT_PROBE_FAILED)
        self.assertIn("OrderCancelRejected", probe.failures[0])

    def test_open_order_event_does_not_advance(self) -> None:
        probe = build_probe()
        probe.on_start()
        probe.on_quote(FakeQuote("100000.0", "100000.1"))
        resting = probe.submitted[0]
        probe.on_order_accepted(FakeEvent(resting.client_order_id))
        probe.on_order_canceled(FakeEvent(resting.client_order_id))  # still open in the cache
        self.assertEqual(len(probe.submitted), 1)  # no IOC leg yet
        self.assertFalse(probe.finished)


# ----------------------------------------------------------------------------- F11 shutdown


class TestWatchdog(unittest.TestCase):
    """
    F11: finishing stops the node without a Ctrl+C, and a hard timeout always fires.
    """

    def test_done_event_triggers_the_stop_callable(self) -> None:
        done = threading.Event()
        calls: list[int] = []
        thread, state = exec_probe.start_stop_watchdog(done, lambda: calls.append(1), 30.0)
        done.set()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [1])
        self.assertTrue(state["fired"])
        self.assertFalse(state["timed_out"])
        self.assertTrue(state["stopped"])

    def test_timeout_triggers_the_stop_callable(self) -> None:
        done = threading.Event()
        calls: list[int] = []
        thread, state = exec_probe.start_stop_watchdog(done, lambda: calls.append(1), 0.05)
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [1])
        self.assertFalse(state["fired"])
        self.assertTrue(state["timed_out"])

    def test_probe_sets_done_event_on_success(self) -> None:
        probe = build_probe(commission_ids=(PROBE_ID,))
        _, ioc = run_to_ioc(probe)
        self.assertFalse(probe.done_event.is_set())
        ioc.close("CANCELED")
        probe.on_order_canceled(FakeEvent(ioc.client_order_id))
        self.assertTrue(probe.done_event.is_set())


# --------------------------------------------------------------------- F06 testnet-only CLI


class TestEnvironmentGuard(unittest.TestCase):
    """
    F06: the probe has no mainnet path at all.
    """

    def test_no_mainnet_flag(self) -> None:
        actions = {action.dest for action in exec_probe.build_parser()._actions}
        self.assertNotIn("i_know_mainnet", actions)

    def test_testnet_values_resolve_to_testnet(self) -> None:
        for value in (None, "true", "TRUE", "1", "yes", "on", ""):
            env = dict(os.environ)
            env.pop("ASTER_TESTNET", None)
            if value is not None:
                env["ASTER_TESTNET"] = value
            with mock.patch.dict(os.environ, env, clear=True):
                self.assertIsNotNone(exec_probe.resolve_environment(), f"value={value!r}")

    def test_non_testnet_values_refuse(self) -> None:
        for value in ("false", "0", "no", "off", "mainnet", "prod"):
            with mock.patch.dict(os.environ, {"ASTER_TESTNET": value}):
                self.assertIsNone(exec_probe.resolve_environment(), f"value={value!r}")

    @staticmethod
    def _quiet_main(argv: list[str]) -> tuple[int, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = exec_probe.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_main_refuses_non_testnet(self) -> None:
        with mock.patch.dict(os.environ, {"ASTER_TESTNET": "false"}):
            code, output = self._quiet_main(["--dry-run"])
        self.assertEqual(code, exec_probe.EXIT_REFUSED)
        self.assertIn("TESTNET only", output)

    def test_main_dry_run_on_testnet(self) -> None:
        with mock.patch.dict(
            os.environ,
            {"ASTER_TESTNET": "true", "ASTER_SIGNER_PRIVATE_KEY": "dummy"},
        ):
            code, output = self._quiet_main(["--dry-run"])
        self.assertEqual(code, exec_probe.EXIT_OK)
        self.assertIn("dry run", output)
        self.assertNotIn("dummy", output)  # never echo the credential

    def test_main_refuses_without_a_signer_key(self) -> None:
        env = dict(os.environ)
        env.pop("ASTER_SIGNER_PRIVATE_KEY", None)
        env["ASTER_TESTNET"] = "true"
        with mock.patch.dict(os.environ, env, clear=True):
            code, output = self._quiet_main([])
        self.assertEqual(code, exec_probe.EXIT_REFUSED)
        self.assertIn("ASTER_SIGNER_PRIVATE_KEY is not set", output)

    def test_probe_budget_cannot_be_widened_by_config(self) -> None:
        config = exec_probe.AsterProbeConfig(
            instrument_id=PROBE_ID,
            max_notional=Decimal("1000"),
        )
        self.assertEqual(config.max_notional, exec_probe.MAX_NOTIONAL_USDT)


# ------------------------------------------------------------------------- F09 fee labelling


class TestFeeReporting(unittest.TestCase):
    """
    F09: adapter fee values are printed with their provenance, never as verified fees.
    """

    def test_binance_defaults_are_flagged(self) -> None:
        line = exec_probe.fee_line(make_instrument(maker_fee="0.0002", taker_fee="0.0005"))
        self.assertIn("unverified", line)
        self.assertIn("Binance VIP0 defaults", line)
        self.assertNotIn("verified account", line)

    def test_non_default_fees_are_printed_without_the_warning(self) -> None:
        line = exec_probe.fee_line(make_instrument(maker_fee="0.0001", taker_fee="0.00035"))
        self.assertIn("maker_fee=0.0001", line)
        self.assertIn("taker_fee=0.00035", line)
        self.assertIn("commissionRate when available", line)
        self.assertNotIn("Binance VIP0 defaults", line)

    def test_adapter_fee_source_flag_is_used_when_present(self) -> None:
        instrument = make_instrument(
            maker_fee="0.0001",
            taker_fee="0.00035",
            info={"fee_source": "commissionRate"},
        )
        line = exec_probe.fee_line(instrument)
        self.assertIn("source: commissionRate", line)
        self.assertNotIn("unverified", line)

    def test_missing_instrument_is_reported_not_faked(self) -> None:
        missing = InstrumentId.from_str("NVDAUSDT-PERP.ASTER")
        probe = build_probe(commission_ids=(PROBE_ID, missing))
        _, ioc = run_to_ioc(probe)
        ioc.close("CANCELED")
        probe.on_order_canceled(FakeEvent(ioc.client_order_id))
        self.assertIn("fee_lines=2", probe.summary_line)
        self.assertTrue(any("not loaded" in line for line in probe._fee_lines))


# ------------------------------------------------- round-2 review: R2-06 / R2-07 / R2-08


def resting_probe() -> tuple[ProbeUnderTest, FakeOrder]:
    """
    Drive the probe to "GTC accepted, cancel requested" and return it with the resting order.
    """
    probe = build_probe()
    probe.on_start()
    probe.on_quote(FakeQuote("100000.0", "100000.1"))
    resting = probe.submitted[0]
    probe.on_order_accepted(FakeEvent(resting.client_order_id))
    return probe, resting


class TestRound2Regressions(unittest.TestCase):
    """
    The three round-2 review cases, with their assertions unchanged.
    """

    def test_failure_prevents_late_cancel_from_submitting_ioc(self) -> None:
        # R2-06
        probe, resting = resting_probe()
        # Inject an event sequence, not an assertion that the Aster adapter emits
        # OrderCancelRejected for every HTTP cancellation failure.
        probe.on_order_cancel_rejected(
            FakeEvent(resting.client_order_id, reason="cancel outcome unknown"),
        )
        self.assertTrue(probe.finished)
        self.assertTrue(probe.done_event.is_set())
        count = probe.orders_sent
        resting.close("CANCELED")
        probe.on_order_canceled(FakeEvent(resting.client_order_id))
        self.assertEqual(
            probe.orders_sent,
            count,
            f"new order after failure: phase={probe._phase}, orders={probe.orders_sent}",
        )

    def test_timeout_initiates_cleanup_of_unresolved_gtc(self) -> None:
        # R2-07
        probe, resting = resting_probe()
        original_cancel_count = len(probe.cancel_calls)
        # The watchdog only signals from its thread; the node invokes on_stop
        # on the main thread. Keep the same thread boundary for PyStrategy.
        stop_requested = threading.Event()
        thread, state = exec_probe.start_stop_watchdog(
            probe.done_event,
            stop_requested.set,
            0.01,
        )
        thread.join(timeout=1.0)
        self.assertTrue(state["timed_out"])
        self.assertTrue(stop_requested.is_set())
        probe.on_stop()
        self.assertFalse(resting.is_closed)
        self.assertGreater(
            len(probe.cancel_calls),
            original_cancel_count,
            "watchdog exit leaves accepted GTC with no cleanup request",
        )

    def test_filled_gtc_does_not_pass_cancel_probe(self) -> None:
        # R2-08
        probe, resting = resting_probe()
        resting.close("FILLED", filled_qty=str(resting.quantity))
        probe.on_order_filled(
            FakeEvent(
                resting.client_order_id,
                last_qty=resting.quantity,
                last_px=resting.price,
                commission=None,
                liquidity_side="MAKER",
            ),
        )
        if len(probe.submitted) > 1:
            ioc = probe.submitted[1]
            ioc.close("CANCELED")
            probe.on_order_canceled(FakeEvent(ioc.client_order_id))
        self.assertNotEqual(
            probe.exit_code,
            exec_probe.EXIT_OK,
            f"cancel never succeeded: {probe.summary_line}",
        )


class TestCleanupOnStop(unittest.TestCase):
    """
    R2-07 detail: the cleanup is bounded, probe-scoped, and reports what it cannot confirm.
    """

    def test_cleanup_only_cancels_this_probes_own_orders(self) -> None:
        probe, resting = resting_probe()
        # An unrelated order in the same cache must never be touched.
        foreign = FakeOrder(
            ClientOrderId("FOREIGN-1"),
            Quantity.from_str("1.0"),
            Price.from_str("100.0"),
        )
        probe.cache.orders[foreign.client_order_id] = foreign
        probe.on_stop()
        self.assertNotIn(foreign.client_order_id, probe.cancel_calls)
        self.assertEqual(set(probe.cancel_calls), {resting.client_order_id})

    def test_leftovers_are_named_with_their_status(self) -> None:
        probe, resting = resting_probe()
        probe.on_stop()
        self.assertEqual(probe.leftovers, [f"{resting.client_order_id}=ACCEPTED"])
        self.assertFalse(probe.cleanup_done_event.is_set())

    def test_closed_orders_leave_nothing_to_clean_up(self) -> None:
        probe = build_probe(commission_ids=(PROBE_ID,))
        _, ioc = run_to_ioc(probe)
        ioc.close("CANCELED")
        probe.on_order_canceled(FakeEvent(ioc.client_order_id))
        self.assertEqual(probe.leftovers, [])
        self.assertTrue(probe.cleanup_done_event.is_set())
        self.assertIn("outstanding=0", probe.summary_line)
        before = list(probe.cancel_calls)
        probe.on_stop()
        self.assertEqual(probe.cancel_calls, before)

    def test_failure_cleanup_cancels_the_open_resting_order(self) -> None:
        probe, resting = resting_probe()
        before = len(probe.cancel_calls)
        probe.on_order_rejected(FakeEvent(resting.client_order_id, reason="venue error"))
        self.assertGreater(len(probe.cancel_calls), before)
        self.assertIn("outstanding=1", probe.summary_line)
        self.assertFalse(probe.cleanup_done_event.is_set())
        # A late confirmation closes the cleanup out without starting anything new.
        resting.close("CANCELED")
        probe.on_order_canceled(FakeEvent(resting.client_order_id))
        self.assertTrue(probe.cleanup_done_event.is_set())
        self.assertEqual(probe.leftovers, [])
        self.assertEqual(len(probe.submitted), 1)

    def test_watchdog_waits_for_the_cleanup_within_a_bound(self) -> None:
        done = threading.Event()
        cleanup = threading.Event()
        calls: list[int] = []
        thread, state = exec_probe.start_stop_watchdog(
            done,
            lambda: calls.append(1),
            30.0,
            cleanup,
            5.0,
        )
        done.set()
        cleanup.set()
        thread.join(timeout=5.0)
        self.assertEqual(calls, [1])
        self.assertTrue(state["cleanup_confirmed"])

    def test_watchdog_stops_anyway_when_the_cleanup_is_never_confirmed(self) -> None:
        done = threading.Event()
        cleanup = threading.Event()
        calls: list[int] = []
        thread, state = exec_probe.start_stop_watchdog(
            done,
            lambda: calls.append(1),
            30.0,
            cleanup,
            0.05,
        )
        done.set()
        thread.join(timeout=5.0)
        self.assertFalse(thread.is_alive())
        self.assertEqual(calls, [1])
        self.assertFalse(state["cleanup_confirmed"])


# --------------------------------------------------- round-3 review: R3-04 cleanup failure


class TestRound3CleanupFailure(unittest.TestCase):
    """
    R3-04: a cleanup that cannot even request a cancel must not cost the run its result.

    The native ``Strategy.cancel_order`` raises for a tracked id that is not in the cache -
    exactly the state the cleanup exists to report. Before the fix that exception escaped
    ``_finish``, so the summary was never built, ``done_event`` was never set, and only the
    watchdog timeout could end the run.
    """

    def test_native_cancel_error_still_produces_a_result(self) -> None:
        # Real (client-less) LiveNode so the strategy is registered and the real native
        # cancel_order runs. The node is never started and there is no venue connection.
        probe = exec_probe.AsterProbeStrategy(
            exec_probe.AsterProbeConfig(instrument_id=exec_probe.BTC_INSTRUMENT_ID),
        )
        probe._probe_order_ids.append(ClientOrderId("MISSING-1"))
        probe._orders_sent = 1
        node = (
            exec_probe.LiveNode.builder(
                "PROBE-CLEANUP-FAILURE-TEST",
                exec_probe.TraderId.from_str("TESTER-002"),
                exec_probe.Environment.LIVE,
            )
            .with_logging(
                exec_probe.LoggerConfig(
                    stdout_level=exec_probe.LogLevel.OFF,
                    bypass_logging=True,
                ),
            )
            .build()
        )
        node.add_strategy(probe)

        with contextlib.redirect_stderr(io.StringIO()):
            probe.record_failure("injected tracked-id/cache mismatch")

        self.assertTrue(probe.finished)
        self.assertTrue(probe.done_event.is_set(), "cleanup error skipped the done signal")
        self.assertTrue(probe.summary_line, "cleanup error skipped the result summary")
        self.assertEqual(len(probe.cancel_errors), 1)
        self.assertIn("MISSING-1", probe.cancel_errors[0])
        self.assertIn("not found in cache", probe.cancel_errors[0])
        self.assertEqual(probe.exit_code, exec_probe.EXIT_PROBE_FAILED)
        self.assertIn("cancel_errors=1", probe.summary_line)
        # The done signal and the cleanup-confirmed signal stay separate.
        self.assertFalse(probe.cleanup_done_event.is_set())
        self.assertEqual(probe.leftovers, ["MISSING-1=unknown (not in the cache)"])


class TestCleanupErrorsWithDoubles(unittest.TestCase):
    """
    The same contract, driven through the fast test doubles.
    """

    def test_one_failing_cancel_does_not_stop_the_others(self) -> None:
        probe, resting = resting_probe()
        second = FakeOrder(
            ClientOrderId("O-EXTRA"),
            Quantity.from_str("0.0001"),
            Price.from_str("50000.0"),
        )
        probe.cache.orders[second.client_order_id] = second
        probe._probe_order_ids.append(second.client_order_id)
        probe.cancel_raises_for.add(resting.client_order_id)

        probe.on_order_rejected(FakeEvent(resting.client_order_id, reason="venue error"))

        # The refused cancel is recorded and the remaining order is still cancelled.
        self.assertEqual(len(probe.cancel_errors), 1)
        self.assertIn("O-1", probe.cancel_errors[0])
        self.assertIn(second.client_order_id, probe.cancel_calls)
        self.assertTrue(probe.done_event.is_set())
        self.assertIn("cancel_errors=1", probe.summary_line)
        self.assertIn("result=failed", probe.summary_line)
        self.assertEqual(probe.exit_code, exec_probe.EXIT_PROBE_FAILED)

    def test_leftover_report_is_labelled_as_a_stop_time_snapshot(self) -> None:
        probe, resting = resting_probe()
        probe.on_stop()
        self.assertIn("NOT venue-confirmed", probe.leftover_report)
        self.assertIn(f"{resting.client_order_id}=ACCEPTED", probe.leftover_report)

    def test_confirmed_cleanup_is_not_labelled_a_snapshot(self) -> None:
        probe = build_probe(commission_ids=(PROBE_ID,))
        _, ioc = run_to_ioc(probe)
        ioc.close("CANCELED")
        probe.on_order_canceled(FakeEvent(ioc.client_order_id))
        self.assertTrue(probe.cleanup_done_event.is_set())
        self.assertIn("none", probe.leftover_report)
        self.assertNotIn("NOT venue-confirmed", probe.leftover_report)


if __name__ == "__main__":
    unittest.main()

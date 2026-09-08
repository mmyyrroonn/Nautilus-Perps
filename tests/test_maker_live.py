#!/usr/bin/env python3
"""
Offline regression tests for ``src/maker_live.py`` (live Lighter maker, Aster taker hedge).

Everything here runs without a network, without credentials and without a LiveNode: the pure
components (:class:`QuoteEngine`, :class:`PaperBroker`, :class:`HedgeManager`,
:class:`KillSwitch`, :class:`DailyState`, :class:`PnLMonitor`) are driven directly, and the
strategy itself is driven with fake quote / trade / fill events over a stubbed cache, order
factory and clock - the same approach as ``tests/test_exec_probe.py``.

The load-bearing test is :class:`TestQuotingParity`: the live quoting rule and
``analysis/maker_inventory.simulate()`` are replayed over the same synthetic tape and must
produce the same transactions and the same fills, increment for increment.  That is what makes
"the live counterpart of the simulator" a checkable claim rather than a comment.

    .venv\\Scripts\\python.exe -m unittest discover -s tests -v
"""

from __future__ import annotations

import bisect
import contextlib
import io
import json
import math
import sys
import tempfile
import unittest
from datetime import datetime
from datetime import timezone
from decimal import Decimal
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "src" / "analysis"))

import maker_fill  # noqa: E402  (analysis helpers: PairBooks / VenueTrades)
import maker_inventory  # noqa: E402  (the offline simulator we must match)
import maker_live  # noqa: E402
from live_limits import load_limits  # noqa: E402
from maker_live import ASK  # noqa: E402
from maker_live import BID  # noqa: E402
from maker_live import AvgCostBook  # noqa: E402
from maker_live import BookSample  # noqa: E402
from maker_live import DailyState  # noqa: E402
from maker_live import HedgeManager  # noqa: E402
from maker_live import KillSwitch  # noqa: E402
from maker_live import PaperBroker  # noqa: E402
from maker_live import PnLMonitor  # noqa: E402
from maker_live import QuoteEngine  # noqa: E402
from maker_live import QuoteParams  # noqa: E402
from nautilus_trader.model import ClientOrderId  # noqa: E402
from nautilus_trader.model import CryptoPerpetual  # noqa: E402
from nautilus_trader.model import Currency  # noqa: E402
from nautilus_trader.model import InstrumentId  # noqa: E402
from nautilus_trader.model import Money  # noqa: E402
from nautilus_trader.model import OrderSide  # noqa: E402
from nautilus_trader.model import Price  # noqa: E402
from nautilus_trader.model import Quantity  # noqa: E402
from nautilus_trader.model import StrategyId  # noqa: E402
from nautilus_trader.model import Symbol  # noqa: E402


USDT = Currency.from_str("USDT")
BTC = Currency.from_str("BTC")
MAKER_ID = InstrumentId.from_str("PONS-PERP.LIGHTER")
HEDGE_ID = InstrumentId.from_str("PONSUSDT-PERP.ASTER")
TICK = 1e-5
DECIMALS = 5


# --------------------------------------------------------------------------- fixtures


def make_instrument(
    instrument_id: InstrumentId,
    *,
    price_increment: str,
    size_increment: str,
    min_quantity: str,
    min_notional: str | None,
) -> CryptoPerpetual:
    """A real CryptoPerpetual carrying the filters we care about (no network, no adapter).

    The base currency is a stand-in: only the increments and the minimums are under test.
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
        min_quantity=Quantity.from_str(min_quantity),
        min_notional=Money(Decimal(min_notional), USDT) if min_notional is not None else None,
        maker_fee=Decimal("0"),
        taker_fee=Decimal("0.00009"),
        info={"mark_price": 0.73386},
    )


def lighter_pons() -> CryptoPerpetual:
    """Lighter mainnet PONS as read on 2026-09-08: 5 price dp, 1 size dp, min 20 / 10 USD."""
    return make_instrument(
        MAKER_ID, price_increment="0.00001", size_increment="0.1",
        min_quantity="20", min_notional="10",
    )


def aster_pons() -> CryptoPerpetual:
    """Aster mainnet PONSUSDT: 0.00001 tick, 1 PONS step, minQty 1, minNotional 5 USD."""
    return make_instrument(
        HEDGE_ID, price_increment="0.00001", size_increment="1",
        min_quantity="1", min_notional="5",
    )


class FakeClock:
    """A clock the test moves by hand, so the hedge timers are deterministic."""

    def __init__(self, t0: float = 1_757_000_000.0) -> None:
        self._ns = int(t0 * 1e9)
        self.timers: list[str] = []

    def timestamp_ns(self) -> int:
        return self._ns

    def utc_now(self) -> datetime:
        return datetime.fromtimestamp(self._ns / 1e9, timezone.utc)

    def set_timer(self, name, interval, start_time=None, **_kw) -> None:
        self.timers.append(str(name))

    def set_time_alert(self, name, alert_time, **_kw) -> None:
        self.timers.append(str(name))

    def advance(self, seconds: float) -> None:
        self._ns += int(seconds * 1e9)


class FakeStatus:
    def __init__(self, name: str) -> None:
        self.name = name

    def __str__(self) -> str:
        return self.name


class FakeOrder:
    """Stand-in for a Nautilus order in the cache."""

    def __init__(self, client_order_id, quantity, price, side, **kw) -> None:
        self.client_order_id = client_order_id
        self.quantity = quantity
        self.price = price
        self.order_side = side
        self.status = FakeStatus(kw.get("status", "ACCEPTED"))
        self.is_closed = kw.get("is_closed", False)
        self.filled_qty = Quantity.from_str("0")

    def close(self, status: str = "CANCELED") -> None:
        self.status = FakeStatus(status)
        self.is_closed = True


class FakeFill:
    """Stand-in for an OrderFilled event."""

    def __init__(self, client_order_id, instrument_id, side, last_qty, last_px) -> None:
        self.client_order_id = client_order_id
        self.instrument_id = instrument_id
        self.order_side = side
        self.last_qty = Quantity.from_str(last_qty)
        self.last_px = Price.from_str(last_px)
        self.ts_event = 0


class FakeQuote:
    def __init__(self, instrument_id, bid: str, ask: str,
                 bid_size: str = "500", ask_size: str = "500") -> None:
        self.instrument_id = instrument_id
        self.bid_price = Price.from_str(bid)
        self.ask_price = Price.from_str(ask)
        self.bid_size = Quantity.from_str(bid_size)
        self.ask_size = Quantity.from_str(ask_size)
        self.ts_init = 0


class FakeAggressor:
    def __init__(self, name: str) -> None:
        self.name = name


class FakeTrade:
    def __init__(self, instrument_id, price: str, size: str, aggressor: str) -> None:
        self.instrument_id = instrument_id
        self.price = Price.from_str(price)
        self.size = Quantity.from_str(size)
        self.aggressor_side = FakeAggressor(aggressor)
        self.ts_init = 0


class FakeCache:
    def __init__(self, instruments=()) -> None:
        self.instruments = {inst.id: inst for inst in instruments}
        self.orders: dict[object, FakeOrder] = {}

    def instrument(self, instrument_id):
        return self.instruments.get(instrument_id)

    def order(self, client_order_id):
        return self.orders.get(client_order_id)

    def orders_open(self, instrument_id=None, **_kw):
        return [o for o in self.orders.values() if not o.is_closed]


class FakeOrderFactory:
    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._n = 0

    def limit(self, **kwargs) -> FakeOrder:
        self._n += 1
        self.calls.append(kwargs)
        return FakeOrder(
            ClientOrderId(f"O-{self._n}"),
            kwargs["quantity"],
            kwargs["price"],
            kwargs["order_side"],
        )


class MakerUnderTest(maker_live.LighterMaker):
    """The production strategy with the trader-owned surfaces replaced by stubs.

    Only ``cache`` / ``order_factory`` / ``clock`` and the submit / cancel / modify /
    subscribe calls are stubbed; every state transition under test is production code.
    """

    def __new__(cls, config, *_args: object, **_kwargs: object):
        return super().__new__(cls, config)

    def __init__(self, config, cache: FakeCache, factory: FakeOrderFactory,
                 clock: FakeClock) -> None:
        super().__init__(config)
        self._stub_cache = cache
        self._stub_factory = factory
        self._stub_clock = clock
        self.submitted: list[FakeOrder] = []
        self.cancelled: list[object] = []
        self.modified: list[tuple] = []
        self.subscriptions: list[object] = []

    @property
    def cache(self):
        return self._stub_cache

    @property
    def order_factory(self):
        return self._stub_factory

    @property
    def clock(self):
        return self._stub_clock

    def subscribe_quotes(self, instrument_id, client_id=None, params=None) -> None:
        self.subscriptions.append(("quotes", instrument_id))

    def subscribe_trades(self, instrument_id, client_id=None, params=None) -> None:
        self.subscriptions.append(("trades", instrument_id))

    def subscribe_book_deltas(self, instrument_id, book_type, depth=None, client_id=None,
                              managed=False, params=None) -> None:
        self.subscriptions.append(("deltas", instrument_id))

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.submitted.append(order)
        self._stub_cache.orders[order.client_order_id] = order

    def cancel_order(self, client_order_id, client_id=None, params=None) -> None:
        self.cancelled.append(client_order_id)
        order = self._stub_cache.orders.get(client_order_id)
        if order is not None:
            order.close("CANCELED")

    def modify_order(self, client_order_id, quantity=None, price=None, trigger_price=None,
                     client_id=None, params=None) -> None:
        self.modified.append((client_order_id, quantity, price))


def build_strategy(
    *,
    mode: str = "paper",
    hedge: bool = True,
    limits=None,
    out: Path | None = None,
    clock: FakeClock | None = None,
) -> MakerUnderTest:
    """A started-up strategy over stub surfaces, writing its CSVs into a temp directory."""
    out = out if out is not None else Path(tempfile.mkdtemp(prefix="maker-"))
    limits = limits if limits is not None else load_limits(REPO / "config" / "limits.toml")
    clock = clock if clock is not None else FakeClock()
    plan = maker_live.SYMBOLS["mainnet"]["PONS"]
    config = maker_live.MakerLiveConfig(
        strategy_id=StrategyId.from_str("MAKER-TEST-001"),
        plan=plan,
        limits=limits,
        mode=mode,
        env="mainnet",
        maker_client_id="LIGHTER",
        hedge_client_id="ASTER",
        fills_csv=out / "fills.csv",
        pnl_csv=out / "pnl.csv",
        state_path=out / "state.json",
        hedge_enabled=hedge,
        deadline_ts=0.0,
    )
    cache = FakeCache(instruments=(lighter_pons(), aster_pons()))
    strategy = MakerUnderTest(config, cache, FakeOrderFactory(), clock)
    strategy.on_start()
    return strategy


def feed_books(strategy: MakerUnderTest, *, m_bid="0.73000", m_ask="0.73040",
               h_bid="0.72980", h_ask="0.73010", h_bid_size="5000",
               h_ask_size="5000") -> None:
    """Push one quote per leg so the strategy has a complete book sample."""
    strategy.on_quote(FakeQuote(MAKER_ID, m_bid, m_ask))
    strategy.on_quote(FakeQuote(HEDGE_ID, h_bid, h_ask,
                                bid_size=h_bid_size, ask_size=h_ask_size))


# --------------------------------------------------------------------------- accounting


class TestAvgCostBookParity(unittest.TestCase):
    """maker_live's copy of AvgCostBook must still behave like the original."""

    SEQUENCE = [
        (100.0, 0.7300, 0.01), (50.0, 0.7320, 0.005), (-120.0, 0.7350, 0.012),
        (-80.0, 0.7310, 0.008), (60.0, 0.7290, 0.006), (-10.0, 0.7400, 0.001),
    ]

    def test_matches_maker_inventory(self) -> None:
        mine, theirs = AvgCostBook(), maker_inventory.AvgCostBook()
        for base, price, fee in self.SEQUENCE:
            got = mine.trade(base, price, fee)
            want = theirs.trade(base, price, fee)
            self.assertEqual(got, want, f"diverged at {base}@{price}")
            self.assertEqual((mine.pos, mine.avg, mine.cash, mine.fees),
                             (theirs.pos, theirs.avg, theirs.cash, theirs.fees))

    def test_cash_identity_holds(self) -> None:
        """cash + pos*mark == sum(realised) + pos*(mark - avg), the decomposition's anchor."""
        book = AvgCostBook()
        realised = 0.0
        for base, price, fee in self.SEQUENCE:
            realised += book.trade(base, price, fee)[0]
        mark = 0.7333
        self.assertAlmostEqual(
            book.cash + book.pos * mark, realised + book.pos * (mark - book.avg), places=9,
        )


# --------------------------------------------------------------------------- the tape


def build_tape(n: int = 120, tob_scale: float = 1.0):
    """A deterministic 1 s tape both the simulator and the live engine can replay.

    The hedge book is swung slowly across the maker book so the opening edge gate opens and
    closes on both sides, and every second prints two aggressor trades that reach a quote one
    tick inside the touch - enough to exercise fills, the inventory cap, the closing side, the
    re-price cadence and the token bucket in one pass.

    ``tob_scale`` thins the maker top-of-book sizes.  In ``join`` mode the queue ahead of us is
    that size, so a fat book means the tape can never reach our order; the join replay uses a
    thin one to exercise the queue branch of the fill model instead of skipping it.
    """
    books = maker_fill.PairBooks(maker="LIGHTER", hedge="ASTER")
    trades = maker_fill.VenueTrades(decimals=DECIMALS)
    t0 = 1_757_000_000.0
    for i in range(n):
        t = t0 + i
        m_bid = round(0.73000 + 0.00002 * ((i * 7) % 11) - 0.00001 * ((i * 3) % 5), 5)
        m_ask = round(m_bid + 0.00004 + 0.00001 * (i % 3), 5)
        offset = round(0.00060 * math.sin(i / 9.0), 5)
        h_bid = round(m_bid - 0.00003 + offset, 5)
        h_ask = round(h_bid + 0.00005, 5)
        books.t.append(t)
        books.m_bid.append(m_bid)
        books.m_bid_size.append((300.0 + 10.0 * (i % 7)) * tob_scale)
        books.m_ask.append(m_ask)
        books.m_ask_size.append((250.0 + 12.0 * (i % 5)) * tob_scale)
        books.h_bid.append(h_bid)
        books.h_bid_size.append(5000.0)
        books.h_ask.append(h_ask)
        books.h_ask_size.append(5000.0)
        books.mid.append(((m_bid + m_ask) / 2.0 + (h_bid + h_ask) / 2.0) / 2.0)
        for k, frac in enumerate((0.3, 0.7)):
            buy = (i + k) % 2 == 0
            price = m_ask if buy else m_bid
            if (i + k) % 3 == 0:  # print through the touch, reaching an improved quote
                price = round(price + (0.00002 if buy else -0.00002), 5)
            trades.t.append(t + frac)
            trades.price.append(round(price, 5))
            trades.size.append(4.0 + 6.0 * ((i + 2 * k) % 6))
            trades.buy.append(1 if buy else 0)
    return books, trades


def replay_live(books, trades, params: QuoteParams):
    """Replay the tape through QuoteEngine + PaperBroker exactly as ``simulate()`` does.

    Sample ``i`` decides both quotes; the trades of ``(t_i, t_i+1]`` then hit them.
    """
    engine = QuoteEngine(params)
    broker = PaperBroker(engine)
    fills: list[maker_live.SimFill] = []
    q = 0.0
    n = len(books)
    ti = bisect.bisect_right(trades.t, books.t[0])
    ntr = len(trades.t)
    for i in range(n):
        t = books.t[i]
        t_next = books.t[i + 1] if i + 1 < n else t + maker_fill.SAMPLE_S
        sample = BookSample(
            t=t,
            m_bid=books.m_bid[i], m_ask=books.m_ask[i],
            m_bid_size=books.m_bid_size[i], m_ask_size=books.m_ask_size[i],
            h_bid=books.h_bid[i], h_ask=books.h_ask[i],
        )
        engine.step(sample, q)
        while ti < ntr and trades.t[ti] <= t_next:
            for fill in broker.on_trade(
                trades.price[ti], trades.size[ti], trades.buy[ti] == 1, trades.t[ti],
            ):
                q += -fill.base if fill.sell else fill.base
                fills.append(fill)
            ti += 1
    return engine, fills, q


SIM_KWARGS = dict(
    order_usd=20.0, max_inv_usd=45.0, min_edge_bps=3.0, reserve_bps=0.0,
    maker_fee_bps=0.0, hedge_delay_s=1.0,
)
FEE_H = 0.9


def live_params(mode: str = "improve", **over) -> QuoteParams:
    base = dict(
        mode=mode, order_usd=20.0, max_inv_usd=45.0, min_edge_bps=3.0, reserve_bps=0.0,
        maker_fee_bps=0.0, hedge_fee_bps=FEE_H, tick=TICK, decimals=DECIMALS,
        requote_min_s=3.0, tx_per_min=36.0,
    )
    base.update(over)
    params = QuoteParams(**base)
    # The simulator never flips through zero, so parity is only meaningful with the live-only
    # close_min_flip behaviour off.  It defaults off here; assert it rather than assume it.
    assert params.close_min_flip is False or "close_min_flip" in over
    return params


def sim_params(mode: str = "improve", **over):
    kwargs = dict(SIM_KWARGS)
    kwargs.update(mode=mode, tx_per_min=36.0, requote_ticks=1, requote_min_s=3.0)
    kwargs.update(over)
    return maker_inventory.Params(**kwargs)


class TestQuotingParity(unittest.TestCase):
    """The live quoting rule must reproduce maker_inventory.simulate() exactly.

    With the live-only restrictions neutral (no venue minimums, no unhedged suspension) the
    two must agree on every transaction and every fill increment.
    """

    def _compare(self, mode: str, tob_scale: float = 1.0, **over) -> None:
        books, trades = build_tape(tob_scale=tob_scale)
        run = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H,
            p=sim_params(mode, **over),
        )
        engine, fills, q = replay_live(books, trades, live_params(mode, **over))

        with self.subTest("the tape must actually fill, or the test proves nothing"):
            self.assertGreater(len(run.incs), 20)
            self.assertGreater(run.tx_sent, 20)

        sim_fills = [
            (round(ts, 9), run.by_oid[oid].sell, round(run.by_oid[oid].price, 10),
             round(base, 9))
            for ts, oid, base in run.incs
        ]
        live_fills = [
            (round(f.t, 9), f.sell, round(f.price, 10), round(f.base, 9)) for f in fills
        ]
        self.assertEqual(sim_fills, live_fills)
        self.assertAlmostEqual(run.q_final, q, places=9)
        self.assertEqual(run.tx_sent, engine.tx_sent)
        self.assertEqual(run.tx_create, engine.tx_create)
        self.assertEqual(run.tx_modify, engine.tx_modify)
        self.assertEqual(run.tx_cancel, engine.tx_cancel)
        self.assertEqual(sum(run.gated), engine.counters["gated"])
        self.assertEqual(sum(run.capped), engine.counters["capped"])
        self.assertEqual(sum(run.locked), engine.counters["locked"])
        self.assertEqual(sum(run.tx_deferred), engine.counters["deferred"])

    def test_improve_mode_matches(self) -> None:
        self._compare("improve")

    def test_join_mode_matches(self) -> None:
        """Join mode also exercises the queue-ahead branch of the fill model."""
        self._compare("join", tob_scale=0.02)

    def test_join_mode_with_a_fat_book_fills_nothing_in_either_replay(self) -> None:
        """The queue ahead is the whole top of book: neither replay may invent a fill."""
        books, trades = build_tape()
        run = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H, p=sim_params("join"),
        )
        engine, fills, q = replay_live(books, trades, live_params("join"))
        self.assertEqual(len(run.incs), 0)
        self.assertEqual(fills, [])
        self.assertEqual(run.tx_sent, engine.tx_sent)

    def test_matches_without_a_transaction_budget(self) -> None:
        self._compare("improve", tx_per_min=None)

    def test_matches_with_a_tight_budget_that_forces_deferrals(self) -> None:
        books, trades = build_tape()
        run = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H,
            p=sim_params("improve", tx_per_min=4.0, requote_min_s=0.0),
        )
        engine, fills, q = replay_live(
            books, trades, live_params("improve", tx_per_min=4.0, requote_min_s=0.0),
        )
        self.assertGreater(sum(run.tx_deferred), 0, "the budget must actually bite")
        self.assertEqual(sum(run.tx_deferred), engine.counters["deferred"])
        self.assertEqual(run.tx_sent, engine.tx_sent)
        self.assertAlmostEqual(run.q_final, q, places=9)

    def test_matches_with_no_requote_cadence(self) -> None:
        self._compare("improve", requote_min_s=0.0)

    def test_matches_at_a_tiny_inventory_cap(self) -> None:
        """A cap this small keeps the closing side in play for most of the tape."""
        self._compare("improve", max_inv_usd=25.0)


# --------------------------------------------------------------------------- engine only


class TestQuoteEngineRestrictions(unittest.TestCase):
    """The live-only additions: they may only ever remove a quote, never add one."""

    def sample(self, **over) -> BookSample:
        base = dict(t=1000.0, m_bid=0.73000, m_ask=0.73040, m_bid_size=500.0,
                    m_ask_size=500.0, h_bid=0.72900, h_ask=0.72930)
        base.update(over)
        return BookSample(**base)

    def test_opening_side_is_suspended_but_the_closing_side_is_not(self) -> None:
        """While the unhedged delta is over its cap we may shrink the book, never grow it."""
        engine = QuoteEngine(live_params())
        # Flat: both sides would be opening, so suspension must silence both.
        actions = engine.step(self.sample(), q=0.0, opening_suspended=True)
        self.assertEqual(actions, [])
        self.assertGreater(engine.counters["suspended"], 0)

        # Long: the ask now CLOSES, so it must still be quoted while suspended.
        engine = QuoteEngine(live_params())
        actions = engine.step(self.sample(), q=40.0, opening_suspended=True)
        self.assertEqual([a.side for a in actions], [ASK])
        self.assertTrue(actions[0].quote.closing)

    def test_both_sides_rest_when_one_of_them_is_closing(self) -> None:
        """An opening bid and an opening ask can never both clear the gate against one hedge
        book - the edge is on one side at a time.  Two-sided quoting happens because the side
        that shrinks the inventory is quoted unconditionally."""
        engine = QuoteEngine(live_params())
        actions = engine.step(self.sample(), q=-20.0, opening_suspended=False)
        self.assertEqual(sorted(a.side for a in actions), [BID, ASK])
        by_side = {a.side: a.quote for a in actions}
        self.assertTrue(by_side[BID].closing, "short inventory -> the bid closes")
        self.assertFalse(by_side[ASK].closing, "and the ask opens on the cross-venue edge")

    def test_a_resting_quote_is_cancelled_when_the_gate_closes(self) -> None:
        engine = QuoteEngine(live_params())
        engine.step(self.sample(), q=0.0)
        self.assertIsNotNone(engine.orders[ASK])
        # Second sample with the suspension on: the opening quote must be pulled.
        actions = engine.step(self.sample(t=1001.0), q=0.0, opening_suspended=True)
        self.assertTrue(any(a.kind == maker_live.CANCEL for a in actions))
        self.assertIsNone(engine.orders[ASK])

    def test_venue_minimum_blocks_a_sub_minimum_closing_clip(self) -> None:
        """A closing clip smaller than the venue minimum cannot be sent, so it is not quoted."""
        params = live_params(min_size=20.0, min_notional=10.0, size_increment=0.1)
        engine = QuoteEngine(params)
        actions = engine.step(self.sample(), q=0.5, opening_suspended=True)  # 0.5 PONS long
        self.assertEqual(actions, [])
        self.assertGreater(engine.counters["too_small"], 0)

    def test_size_is_floored_to_the_venue_increment(self) -> None:
        params = live_params(min_size=20.0, min_notional=10.0, size_increment=0.1)
        engine = QuoteEngine(params)
        actions = engine.step(self.sample(), q=0.0)
        for action in actions:
            size = action.quote.base
            self.assertAlmostEqual(size, round(size / 0.1) * 0.1, places=9)
            self.assertGreaterEqual(size, 20.0)
            self.assertLessEqual(size * action.quote.price, params.order_usd + 1e-9)

    def test_locked_quote_is_never_sent(self) -> None:
        """A one-tick spread leaves no room to improve without crossing."""
        engine = QuoteEngine(live_params())
        engine.step(self.sample(m_bid=0.73000, m_ask=0.73001), q=0.0)
        self.assertEqual(engine.counters["locked"], 2)
        self.assertEqual(engine.orders, [None, None])

    def test_token_bucket_caps_transactions_and_refills(self) -> None:
        engine = QuoteEngine(live_params(tx_per_min=2.0, requote_min_s=0.0))
        engine.step(self.sample(t=0.0), q=-20.0)  # two quotes, spending both tokens
        self.assertEqual(engine.tx_sent, 2)
        engine.step(self.sample(t=1.0, m_bid=0.73010, m_ask=0.73050), q=-20.0)
        self.assertEqual(engine.tx_sent, 2, "no tokens left, the resting quotes are untouched")
        self.assertGreater(engine.counters["deferred"], 0)
        engine.step(self.sample(t=61.0, m_bid=0.73020, m_ask=0.73060), q=-20.0)
        self.assertGreater(engine.tx_sent, 2, "the bucket refilled a minute later")

    def test_throttle_halves_the_bucket_then_restores_it(self) -> None:
        engine = QuoteEngine(live_params(tx_per_min=36.0))
        self.assertEqual(engine.capacity, 36.0)
        engine.throttle(100.0)
        self.assertEqual(engine.capacity, 18.0)
        engine.step(self.sample(t=100.0 + maker_live.THROTTLE_SECS + 1.0), q=0.0)
        self.assertEqual(engine.capacity, 36.0)

    def test_cancel_all_pulls_every_resting_quote(self) -> None:
        engine = QuoteEngine(live_params())
        engine.step(self.sample(), q=-20.0)  # short: the bid closes, the ask opens
        actions = engine.cancel_all()
        self.assertEqual(len(actions), 2)
        self.assertTrue(all(a.kind == maker_live.CANCEL for a in actions))
        self.assertEqual(engine.orders, [None, None])


# --------------------------------------------------------------------------- min clip


class TestCloseMinFlip(unittest.TestCase):
    """`[quote] close_min_flip`: a residual under the venue minimum must still be closable.

    Lighter's PONS minimum is 20 PONS (~15.6 USD), close to a whole 20 USD clip, so without
    this the closing side simply stops quoting and the residual sits until an opening fill on
    the other side happens to absorb it - 195 such refusals in a ten-minute paper session.
    """

    VENUE = dict(min_size=20.0, min_notional=10.0, size_increment=0.1)

    def sample(self, **over) -> BookSample:
        base = dict(t=1000.0, m_bid=0.73000, m_ask=0.73040, m_bid_size=500.0,
                    m_ask_size=500.0, h_bid=0.72900, h_ask=0.72930)
        base.update(over)
        return BookSample(**base)

    def test_off_by_default_in_quote_params(self) -> None:
        """The dataclass default reproduces the simulator; the config supplies the live value."""
        self.assertFalse(QuoteParams(
            mode="improve", order_usd=20.0, max_inv_usd=45.0, min_edge_bps=3.0,
            reserve_bps=0.0, maker_fee_bps=0.0, hedge_fee_bps=0.9, tick=TICK,
            decimals=DECIMALS,
        ).close_min_flip)
        limits = load_limits(REPO / "config" / "limits.toml")
        self.assertTrue(limits.quote.close_min_flip)
        params = QuoteParams.from_limits(limits, tick=TICK, decimals=DECIMALS)
        self.assertTrue(params.close_min_flip)

    def test_flag_off_refuses_a_sub_minimum_closing_clip(self) -> None:
        engine = QuoteEngine(live_params(close_min_flip=False, **self.VENUE))
        actions = engine.step(self.sample(), q=-5.0, opening_suspended=True)
        self.assertEqual(actions, [])
        self.assertGreater(engine.counters["too_small"], 0)
        self.assertEqual(engine.counters["min_clip"], 0)

    def test_flag_on_quotes_the_venue_minimum_and_flips_through_zero(self) -> None:
        """5 PONS short -> the closing bid quotes 20 -> a full fill leaves q = +15."""
        engine = QuoteEngine(live_params(close_min_flip=True, **self.VENUE))
        broker = PaperBroker(engine)
        q = -5.0

        actions = engine.step(self.sample(), q=q, opening_suspended=True)
        self.assertEqual([a.side for a in actions], [BID])
        quote = actions[0].quote
        self.assertTrue(quote.closing)
        self.assertTrue(quote.min_clip)
        self.assertAlmostEqual(quote.base, 20.0)
        self.assertEqual(engine.counters["min_clip"], 1)

        # A SELL aggressor at our bid fills it in full.
        for fill in broker.on_trade(quote.price, 20.0, aggressor_buy=False, t=1000.5):
            q += -fill.base if fill.sell else fill.base
        self.assertAlmostEqual(q, 15.0)

        # Now long 15, still under the minimum: the ASK is the closing side and quotes 20.
        actions = engine.step(self.sample(t=1001.0), q=q, opening_suspended=True)
        self.assertEqual([a.side for a in actions], [ASK])
        self.assertTrue(actions[0].quote.min_clip)
        self.assertAlmostEqual(actions[0].quote.base, 20.0)

    def test_a_min_clip_order_is_not_re_priced_every_sample(self) -> None:
        """It exceeds |q| by design, so the "clip no longer fits" test must not fire on it."""
        engine = QuoteEngine(live_params(close_min_flip=True, **self.VENUE))
        engine.step(self.sample(), q=-5.0, opening_suspended=True)
        spent = engine.tx_sent
        for i in range(1, 6):
            engine.step(self.sample(t=1000.0 + i), q=-5.0, opening_suspended=True)
        self.assertEqual(engine.tx_sent, spent, "an unchanged book must cost no transactions")

    def test_the_flip_never_breaches_the_inventory_cap(self) -> None:
        """A minimum order that would leave more than max_inv_usd on the far side is refused."""
        engine = QuoteEngine(live_params(close_min_flip=True, max_inv_usd=5.0, **self.VENUE))
        actions = engine.step(self.sample(), q=-0.5, opening_suspended=True)
        self.assertEqual(actions, [])
        self.assertGreater(engine.counters["too_small"], 0)
        self.assertEqual(engine.counters["min_clip"], 0)

    def test_the_flip_never_breaches_the_per_order_cap(self) -> None:
        """When the venue minimum is worth more than one clip, the side stays silent."""
        engine = QuoteEngine(
            live_params(close_min_flip=True, order_usd=5.0, **self.VENUE),
        )
        actions = engine.step(self.sample(), q=-5.0, opening_suspended=True)
        self.assertEqual(actions, [])
        self.assertEqual(engine.counters["min_clip"], 0)

    def test_a_clip_already_over_the_minimum_is_untouched(self) -> None:
        engine = QuoteEngine(live_params(close_min_flip=True, **self.VENUE))
        actions = engine.step(self.sample(), q=-40.0, opening_suspended=True)
        quote = next(a.quote for a in actions if a.side == BID)
        self.assertFalse(quote.min_clip)
        self.assertLessEqual(quote.base, 40.0)
        self.assertEqual(engine.counters["min_clip"], 0)


# --------------------------------------------------------------------------- hedging


class TestHedgeManager(unittest.TestCase):
    """Aggregation, the wait, and the round-up that must never make things worse."""

    def make(self, **over) -> HedgeManager:
        base = dict(min_qty=1.0, size_increment=1.0, min_notional=5.0,
                    aggregate_floor=5.0, max_wait_s=5.0, slippage_bps=20.0)
        base.update(over)
        return HedgeManager(**base)

    def test_a_full_size_delta_is_hedged_at_the_touch_less_slippage(self) -> None:
        hedger = self.make()
        hedger.add(27.0, t=0.0)  # long 27 PONS on Lighter -> sell 27 on Aster
        plan = hedger.plan(0.0, h_bid=0.73000, h_ask=0.73030)
        self.assertIsNotNone(plan)
        self.assertTrue(plan.sell)
        self.assertEqual(plan.qty, 27.0)
        self.assertAlmostEqual(plan.price, 0.73000 * (1 - 20.0 / 1e4), places=9)
        self.assertEqual(plan.reason, "normal")

    def test_a_short_delta_buys_at_the_ask_plus_slippage(self) -> None:
        hedger = self.make()
        hedger.add(-30.0, t=0.0)
        plan = hedger.plan(0.0, h_bid=0.73000, h_ask=0.73030)
        self.assertFalse(plan.sell)
        self.assertAlmostEqual(plan.price, 0.73030 * (1 + 20.0 / 1e4), places=9)

    def test_dust_below_the_aster_minimum_waits(self) -> None:
        """4 PONS is ~2.9 USD, under the 5 USD minimum: nothing may be sent yet."""
        hedger = self.make()
        hedger.add(4.0, t=100.0)
        self.assertIsNone(hedger.plan(100.0, 0.73000, 0.73030))
        self.assertIsNone(hedger.plan(104.0, 0.73000, 0.73030))  # still inside max_wait_s

    def test_dust_is_rounded_up_after_the_wait_when_that_shrinks_the_exposure(self) -> None:
        hedger = self.make()
        hedger.add(4.0, t=100.0)
        hedger.plan(100.0, 0.73000, 0.73030)  # arms the wait
        plan = hedger.plan(106.0, 0.73000, 0.73030)
        self.assertIsNotNone(plan)
        self.assertEqual(plan.reason, "rounded-up")
        # 5 USD / 0.7285 -> 7 PONS, and 7 < 2 * 4 so the leftover (3) is smaller than the dust.
        self.assertEqual(plan.qty, 7.0)
        self.assertLess(abs(4.0 - plan.qty), 4.0)
        self.assertEqual(hedger.rounded_up, 1)

    def test_dust_is_carried_when_rounding_up_would_leave_more_than_it_removes(self) -> None:
        """The oscillation guard: over-hedging 1 PONS into 7 leaves 6 - strictly worse."""
        hedger = self.make()
        hedger.add(1.0, t=100.0)
        hedger.plan(100.0, 0.73000, 0.73030)
        self.assertIsNone(hedger.plan(200.0, 0.73000, 0.73030))
        self.assertEqual(hedger.carried, 1)
        self.assertEqual(hedger.rounded_up, 0)

    def test_less_than_one_venue_step_is_always_carried(self) -> None:
        hedger = self.make()
        hedger.add(0.4, t=0.0)
        hedger.plan(0.0, 0.73000, 0.73030)
        self.assertIsNone(hedger.plan(100.0, 0.73000, 0.73030))
        self.assertEqual(hedger.carried, 1)

    def test_force_hedges_immediately_at_shutdown(self) -> None:
        hedger = self.make()
        hedger.add(9.0, t=0.0)
        self.assertIsNotNone(hedger.plan(0.0, 0.73000, 0.73030, force=True))

    def test_settling_a_hedge_reduces_the_delta(self) -> None:
        hedger = self.make()
        hedger.add(27.0, t=0.0)
        hedger.settle(-27.0, t=1.0)  # sold 27 on the hedge venue
        self.assertEqual(hedger.delta, 0.0)

    def test_partial_hedge_leaves_the_remainder_in_the_delta(self) -> None:
        hedger = self.make()
        hedger.add(27.0, t=0.0)
        hedger.settle(-10.0, t=1.0)
        self.assertAlmostEqual(hedger.delta, 17.0)

    def test_breach_seconds_measures_how_long_the_delta_stayed_over_the_cap(self) -> None:
        hedger = self.make()
        hedger.add(100.0, t=0.0)  # ~73 USD, well over a 15 USD cap
        self.assertEqual(hedger.breach_seconds(0.0, 0.73, 15.0), 0.0)
        self.assertAlmostEqual(hedger.breach_seconds(31.0, 0.73, 15.0), 31.0)
        hedger.settle(-99.0, t=32.0)  # back inside the cap: the clock resets
        self.assertEqual(hedger.breach_seconds(33.0, 0.73, 15.0), 0.0)


# --------------------------------------------------------------------------- daily state


class TestDailyState(unittest.TestCase):
    """The counters exist so a restart cannot hand the run a fresh daily budget."""

    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="state-")) / "state.json"

    def test_counters_survive_a_simulated_restart(self) -> None:
        first = DailyState.load(self.path)
        first.fills = 137
        first.tx = 4_211
        first.realized_net_usd = -1.25
        first.save()

        second = DailyState.load(self.path)  # the "restart"
        self.assertEqual(second.fills, 137)
        self.assertEqual(second.tx, 4_211)
        self.assertAlmostEqual(second.realized_net_usd, -1.25)
        self.assertEqual(second.day, first.day)

    def test_a_new_utc_day_resets_the_budget(self) -> None:
        state = DailyState.load(self.path, day="2026-09-07")
        state.fills = 400
        state.save()
        rolled = DailyState.load(self.path, day="2026-09-08")
        self.assertEqual(rolled.fills, 0)
        self.assertEqual(rolled.day, "2026-09-08")

    def test_a_corrupt_state_file_starts_a_fresh_day_rather_than_crashing(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text("{not json", encoding="utf-8")
        state = DailyState.load(self.path)
        self.assertEqual(state.fills, 0)

    def test_save_is_atomic_and_readable(self) -> None:
        state = DailyState.load(self.path)
        state.fills = 3
        state.save()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["fills"], 3)
        self.assertIn("updated", payload)
        self.assertFalse(self.path.with_suffix(".json.tmp").exists())

    def test_restart_carries_the_budget_into_the_kill_switch(self) -> None:
        """A restart at 199/200 fills must trip on the very next fill, not run another 200."""
        limits = load_limits(REPO / "config" / "limits.toml")
        state = DailyState.load(self.path)
        state.fills = limits.daily.max_fills - 1
        state.save()

        reloaded = DailyState.load(self.path)
        switch = KillSwitch(limits, reloaded)
        pnl = PnLMonitor(maker_fee_bps=0.0, hedge_fee_bps=0.9, trip_window=20)
        switch.evaluate(pnl=pnl, mid_m=0.73, mid_h=0.73, hedge_breach_s=0.0,
                        day_fills=reloaded.fills, day_tx=reloaded.tx)
        self.assertIsNone(switch.reason)
        reloaded.fills += 1
        switch.evaluate(pnl=pnl, mid_m=0.73, mid_h=0.73, hedge_breach_s=0.0,
                        day_fills=reloaded.fills, day_tx=reloaded.tx)
        self.assertIsNotNone(switch.reason)
        self.assertIn("day_fills", switch.reason)


# --------------------------------------------------------------------------- paper hedge


class TestPaperHedgeFill(unittest.TestCase):
    """A simulated hedge must be priced at the touch, not at its own protective limit.

    ``slippage_bps`` is the limit a real IOC carries so a moving touch cannot fill it
    arbitrarily far away.  Filling the paper hedge AT that limit charged 20 bps on every hedge
    and 40 bps on every hedge round trip, which swamped the spread capture and made the paper
    P&L - and the loss kill switch reading it - useless as a go/no-go signal.
    """

    def build(self, **books) -> MakerUnderTest:
        strategy = build_strategy()
        feed_books(strategy, **books)
        return strategy

    def test_a_buy_hedge_fills_at_the_ask_not_at_the_slippage_limit(self) -> None:
        strategy = self.build(h_bid="0.72980", h_ask="0.73010")
        plan = maker_live.HedgePlan(sell=False, qty=10.0, price=0.99, reason="normal")
        filled, price = strategy._paper_hedge_fill(plan)
        self.assertAlmostEqual(filled, 10.0)
        self.assertAlmostEqual(price, 0.73010)

    def test_a_sell_hedge_fills_at_the_bid(self) -> None:
        strategy = self.build(h_bid="0.72980", h_ask="0.73010")
        plan = maker_live.HedgePlan(sell=True, qty=10.0, price=0.01, reason="normal")
        filled, price = strategy._paper_hedge_fill(plan)
        self.assertAlmostEqual(price, 0.72980)

    def test_the_fill_is_capped_by_the_size_resting_on_the_touch(self) -> None:
        """Touch 1.000/1.001 with 10 on the ask; a buy of 15 fills 10 and carries 5."""
        strategy = self.build(
            m_bid="0.99900", m_ask="1.00200",
            h_bid="1.00000", h_ask="1.00100", h_bid_size="10", h_ask_size="10",
        )
        plan = maker_live.HedgePlan(sell=False, qty=15.0, price=1.02, reason="normal")
        filled, price = strategy._paper_hedge_fill(plan)
        self.assertAlmostEqual(filled, 10.0)
        self.assertAlmostEqual(price, 1.00100)

        # And through _pump_hedge the unfilled remainder stays in the delta.
        strategy._hedger.delta = -15.0  # short on Lighter -> buy 15 on Aster
        strategy._hedger.waiting_since = None
        strategy._pump_hedge(strategy.clock.timestamp_ns() / 1e9, force=True)
        self.assertEqual(strategy._pnl.hedge_fills, 1)
        self.assertAlmostEqual(strategy._hedger.delta, -5.0)

    def test_an_unknown_depth_is_treated_as_covering_the_order(self) -> None:
        strategy = self.build(h_bid_size="0", h_ask_size="0")
        plan = maker_live.HedgePlan(sell=False, qty=15.0, price=1.02, reason="normal")
        filled, _ = strategy._paper_hedge_fill(plan)
        self.assertAlmostEqual(filled, 15.0)

    def test_a_partial_paper_hedge_is_labelled_in_the_fills_csv(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="partial-"))
        strategy = build_strategy(out=out)
        feed_books(strategy, h_bid_size="4", h_ask_size="4")
        strategy._hedger.delta = 12.0
        strategy._hedger.waiting_since = None
        strategy._pump_hedge(strategy.clock.timestamp_ns() / 1e9, force=True)
        strategy._fills.close()
        rows = (out / "fills.csv").read_text(encoding="utf-8").strip().splitlines()
        kinds = [r.split(",")[4] for r in rows[1:]]
        self.assertIn("hedge-partial", kinds)

    def test_a_round_trip_hedge_at_the_touch_costs_only_the_spread(self) -> None:
        """Buy then sell the same size: the loss is the Aster spread plus fees, not 40 bps."""
        strategy = self.build(h_bid="0.72980", h_ask="0.73010")
        t = strategy.clock.timestamp_ns() / 1e9
        strategy._hedger.delta = -100.0
        strategy._hedger.waiting_since = None
        strategy._pump_hedge(t, force=True)
        strategy._hedger.delta = 100.0
        strategy._hedger.waiting_since = None
        strategy._pump_hedge(t, force=True)
        self.assertEqual(strategy._pnl.hedge_fills, 2)
        # 100 bought at the ask and sold at the bid = exactly the 0.0003 spread.
        self.assertAlmostEqual(strategy._pnl.hedge_cost, -100.0 * 0.0003, places=8)


# --------------------------------------------------------------------------- kill switch


class TestKillSwitch(unittest.TestCase):
    """Every condition that must stop the run."""

    def setUp(self) -> None:
        self.limits = load_limits(REPO / "config" / "limits.toml")
        self.path = Path(tempfile.mkdtemp(prefix="kill-")) / "state.json"
        self.state = DailyState.load(self.path)
        self.pnl = PnLMonitor(maker_fee_bps=0.0, hedge_fee_bps=0.9, trip_window=20)

    def check(self, **over):
        switch = KillSwitch(self.limits, self.state)
        kwargs = dict(pnl=self.pnl, mid_m=0.73, mid_h=0.73, hedge_breach_s=0.0,
                      day_fills=0, day_tx=0)
        kwargs.update(over)
        checks = switch.evaluate(**kwargs)
        return switch, checks

    def test_nothing_trips_on_a_clean_book(self) -> None:
        switch, checks = self.check()
        self.assertIsNone(switch.reason)
        self.assertTrue(all(not c.tripped for c in checks))

    def test_daily_fill_budget(self) -> None:
        switch, _ = self.check(day_fills=self.limits.daily.max_fills)
        self.assertIn("day_fills", switch.reason or "")

    def test_daily_transaction_budget(self) -> None:
        switch, _ = self.check(day_tx=self.limits.daily.max_tx)
        self.assertIn("day_tx", switch.reason or "")

    def test_cumulative_loss(self) -> None:
        self.state.realized_net_usd = -self.limits.kill.max_loss_usd - 0.01
        switch, _ = self.check()
        self.assertIn("net_usd", switch.reason or "")

    def test_loss_carried_in_from_earlier_today_counts(self) -> None:
        """The loss budget is per UTC day, not per process."""
        self.state.realized_net_usd = -(self.limits.kill.max_loss_usd - 0.001)
        switch, _ = self.check()
        self.assertIsNone(switch.reason)
        self.state.realized_net_usd -= 0.01
        switch, _ = self.check()
        self.assertIsNotNone(switch.reason)

    def test_hedge_failure_timer(self) -> None:
        switch, _ = self.check(hedge_breach_s=self.limits.kill.max_hedge_fail_s + 1.0)
        self.assertIn("hedge_fail_s", switch.reason or "")

    def test_hedge_failure_inside_the_window_does_not_trip(self) -> None:
        switch, _ = self.check(hedge_breach_s=self.limits.kill.max_hedge_fail_s - 1.0)
        self.assertIsNone(switch.reason)

    def test_consecutive_losing_trips(self) -> None:
        self.pnl.consecutive_losses = self.limits.kill.max_consecutive_losing_trips
        switch, _ = self.check()
        self.assertIn("losing_trips", switch.reason or "")

    def test_total_exposure(self) -> None:
        self.pnl.m.pos = 100.0  # 73 USD on the maker leg
        self.pnl.h.pos = -100.0  # and 73 on the hedge leg -> 146 gross
        switch, _ = self.check()
        self.assertIn("exposure_usd", switch.reason or "")

    def _grind(self, count: int) -> None:
        """Two losing round trips then a small winner, so only the EWMA line can fire.

        The pattern keeps the losing streak at 2 (well under max_consecutive_losing_trips)
        while the mean trip sits around -6 bps, far below the -2 bps floor.
        """
        for i in range(count):
            realized = 0.0005 if i % 3 == 2 else -0.01  # +0.5 bps / -10 bps on 10 USD
            self.pnl._record_trip(realized, 0.0, 10.0)

    def test_the_ewma_only_binds_once_the_window_is_full(self) -> None:
        """One bad dust round trip must not stop a session that has barely started."""
        self._grind(5)
        switch, _ = self.check()
        self.assertLess(self.pnl.trip_bps_ewma, self.limits.kill.min_trip_net_bps_ewma)
        self.assertLess(self.pnl.consecutive_losses,
                        self.limits.kill.max_consecutive_losing_trips)
        self.assertIsNone(switch.reason, "under 20 trips the EWMA must not bind yet")

        self._grind(self.limits.kill.trip_window)
        switch, _ = self.check()
        self.assertIn("trip_bps_ewma", switch.reason or "")

    def test_a_healthy_ewma_never_trips(self) -> None:
        for _ in range(40):
            self.pnl._record_trip(0.05, 0.0, 10.0)  # +50 bps per trip
        switch, _ = self.check()
        self.assertIsNone(switch.reason)

    def test_margins_are_reported_for_every_line(self) -> None:
        _, checks = self.check()
        names = {c.name for c in checks}
        self.assertEqual(
            names,
            {"day_fills", "day_tx", "net_usd", "losing_trips", "exposure_usd",
             "hedge_fail_s", "trip_bps_ewma"},
        )
        for check in checks:
            self.assertIn("/", check.margin())


# --------------------------------------------------------------------------- pnl monitor


class TestPnLMonitor(unittest.TestCase):
    """The decomposition must add up and the round trips must be counted."""

    def test_a_round_trip_is_recorded_with_its_net_bps(self) -> None:
        pnl = PnLMonitor(maker_fee_bps=0.0, hedge_fee_bps=0.9, trip_window=20)
        pnl.maker_fill(sell=False, base=100.0, price=0.7300)  # buy 100 on Lighter
        self.assertEqual(pnl.trips, 0)
        pnl.maker_fill(sell=True, base=100.0, price=0.7320)  # sell it back, +0.20 USD
        self.assertEqual(pnl.trips, 1)
        self.assertAlmostEqual(pnl.spread_capture, 0.2, places=9)
        self.assertIsNotNone(pnl.trip_bps_ewma)
        self.assertGreater(pnl.trip_bps_ewma, 0.0)
        self.assertEqual(pnl.consecutive_losses, 0)

    def test_a_losing_round_trip_increments_the_streak(self) -> None:
        pnl = PnLMonitor(maker_fee_bps=0.0, hedge_fee_bps=0.9, trip_window=20)
        pnl.maker_fill(sell=False, base=100.0, price=0.7320)
        pnl.maker_fill(sell=True, base=100.0, price=0.7300)
        self.assertEqual(pnl.consecutive_losses, 1)
        self.assertLess(pnl.trip_bps_ewma, 0.0)

    def test_hedge_fees_land_in_the_net(self) -> None:
        pnl = PnLMonitor(maker_fee_bps=0.0, hedge_fee_bps=0.9, trip_window=20)
        pnl.maker_fill(sell=False, base=100.0, price=0.7300)
        pnl.hedge_fill(sell=True, base=100.0, price=0.7300)
        self.assertGreater(pnl.fees, 0.0)
        self.assertAlmostEqual(pnl.fees, 0.9 / 1e4 * 100.0 * 0.73, places=9)
        # Flat on both legs at the same mid: the net is exactly minus the fee paid.
        self.assertAlmostEqual(pnl.net(0.7300, 0.7300), -pnl.fees, places=9)

    def test_exposure_counts_both_legs(self) -> None:
        pnl = PnLMonitor(maker_fee_bps=0.0, hedge_fee_bps=0.9, trip_window=20)
        pnl.maker_fill(sell=False, base=100.0, price=0.7300)
        pnl.hedge_fill(sell=True, base=100.0, price=0.7300)
        self.assertAlmostEqual(pnl.exposure_usd(0.73, 0.73), 2 * 100.0 * 0.73, places=9)


# --------------------------------------------------------------------------- strategy


class TestStrategy(unittest.TestCase):
    """The wiring: fills move the inventory, arm the hedge, and stop the run when they must."""

    def test_start_subscribes_both_legs(self) -> None:
        strategy = build_strategy()
        kinds = {(k, i) for k, i in strategy.subscriptions}
        for leg in (MAKER_ID, HEDGE_ID):
            for kind in ("quotes", "trades", "deltas"):
                self.assertIn((kind, leg), kinds)

    def test_engine_is_sized_from_the_instrument_filters(self) -> None:
        strategy = build_strategy()
        params = strategy._engine.p
        self.assertAlmostEqual(params.tick, 1e-5)
        self.assertEqual(params.decimals, 5)
        self.assertAlmostEqual(params.size_increment, 0.1)
        self.assertAlmostEqual(params.min_size, 20.0)
        self.assertAlmostEqual(params.min_notional, 10.0)
        # The exposure cap binds before the configured inventory cap on a hedged book.
        self.assertAlmostEqual(params.max_inv_usd, 45.0)

    def test_a_paper_fill_moves_the_inventory_and_arms_the_hedge(self) -> None:
        strategy = build_strategy()
        feed_books(strategy)
        strategy._decide()  # inside the startup grace: nothing is quoted yet
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        self.assertIsNotNone(strategy._engine.orders[ASK])

        # A BUY aggressor through our ask fills the resting sell.
        strategy.on_trade(FakeTrade(MAKER_ID, "0.73040", "30", "BUY"))
        self.assertLess(strategy._q, 0.0, "a maker sell must leave us short")
        self.assertEqual(strategy._state.fills, 1)
        # The hedge is a BUY on Aster.  Aster's step is 1 PONS while Lighter's is 0.1, so a
        # 27.3 PONS maker fill hedges 27 and carries 0.3 - less than one hedge-venue step, so
        # there is nothing legal left to send and the dust is carried, not over-hedged.
        self.assertEqual(strategy._pnl.hedge_fills, 1)
        self.assertLess(abs(strategy._hedger.delta), 1.0)

        # Even after the aggregation wait it stays carried: there is no legal order to send.
        strategy.clock.advance(strategy._limits.hedge.max_wait_s + 1.0)
        strategy._pump_hedge(strategy.clock.timestamp_ns() / 1e9)
        self.assertEqual(strategy._pnl.hedge_fills, 1, "no dust order may be sent")
        self.assertGreater(strategy._hedger.carried, 0)

    def test_fills_and_hedges_are_written_to_the_csv(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="csv-"))
        strategy = build_strategy(out=out)
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        strategy.on_trade(FakeTrade(MAKER_ID, "0.73040", "30", "BUY"))
        strategy._fills.close()
        rows = (out / "fills.csv").read_text(encoding="utf-8").strip().splitlines()
        self.assertEqual(rows[0].split(",")[:5], ["ts", "leg", "venue", "side", "kind"])
        legs = [r.split(",")[1] for r in rows[1:]]
        self.assertIn("maker", legs)
        self.assertIn("hedge", legs)

    def test_opening_side_is_suspended_while_the_delta_is_over_the_cap(self) -> None:
        """A delta the hedge venue cannot absorb must stop the side that would grow it."""
        strategy = build_strategy()
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        self.assertIsNotNone(strategy._engine.orders[ASK], "the opening ask rests to begin with")

        # 60 PONS is ~44 USD, far over the 15 USD unhedged cap.  The hedge venue is made
        # unable to accept anything, so the delta cannot be worked off between samples.
        strategy._hedger.delta = 60.0
        strategy._hedger.waiting_since = None
        strategy._hedger.size_increment = 1e9
        strategy._hedger.min_qty = 1e9
        before = strategy._engine.counters["suspended"]
        strategy.clock.advance(1.0)
        strategy._decide()
        self.assertGreater(strategy._engine.counters["suspended"], before)
        self.assertIsNone(strategy._engine.orders[ASK], "the opening quote must be pulled")

    def test_kill_switch_stops_quoting_and_reports_the_reason(self) -> None:
        strategy = build_strategy()
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        self.assertIsNotNone(strategy._engine.orders[ASK])

        strategy._state.fills = strategy._limits.daily.max_fills
        strategy.clock.advance(1.0)
        strategy._decide()
        self.assertIsNotNone(strategy.kill_reason)
        self.assertIn("day_fills", strategy.kill_reason)
        self.assertEqual(strategy._engine.orders, [None, None], "every quote must be pulled")
        self.assertTrue(strategy.done_event.is_set())
        self.assertEqual(strategy.exit_code, maker_live.EXIT_KILLED)
        self.assertIn("kill=day_fills", strategy.summary())

    def test_kill_switch_on_a_hedge_that_never_completes(self) -> None:
        strategy = build_strategy()
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        strategy._hedger.delta = 60.0  # ~44 USD unhedged, over the 15 USD cap
        strategy._hedger.waiting_since = None
        strategy._hedger.size_increment = 1e9  # nothing legal to send: the hedge can never clear
        strategy._hedger.min_qty = 1e9
        strategy.clock.advance(1.0)
        strategy._decide()
        self.assertIsNone(strategy.kill_reason, "the timer has only just started")
        strategy.clock.advance(strategy._limits.kill.max_hedge_fail_s + 2.0)
        strategy._decide()
        self.assertIn("hedge_fail_s", strategy.kill_reason or "")

    def test_deadline_finishes_cleanly(self) -> None:
        strategy = build_strategy()
        strategy._cfg.deadline_ts = strategy.clock.timestamp_ns() / 1e9 + 5.0
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 10.0)
        strategy._decide()
        self.assertTrue(strategy.done_event.is_set())
        self.assertIsNone(strategy.kill_reason)
        self.assertEqual(strategy.exit_code, maker_live.EXIT_OK)

    def test_live_mode_submits_post_only_gtc_limit_orders(self) -> None:
        strategy = build_strategy(mode="live")
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        self.assertTrue(strategy.submitted)
        for call in strategy.order_factory.calls:
            self.assertTrue(call["post_only"], "maker quotes must be post-only")
            self.assertEqual(call["time_in_force"], maker_live.TimeInForce.GTC)
            self.assertEqual(call["instrument_id"], MAKER_ID)
        self.assertEqual(strategy.orders_sent, len(strategy.submitted))

    def test_live_hedge_is_a_limit_ioc_never_a_market_order(self) -> None:
        strategy = build_strategy(mode="live")
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        quote = strategy._engine.orders[ASK]
        fill = FakeFill(quote.ref, MAKER_ID, OrderSide.SELL, "27.0", "0.73030")
        strategy.on_order_filled(fill)
        hedges = [c for c in strategy.order_factory.calls if c["instrument_id"] == HEDGE_ID]
        self.assertEqual(len(hedges), 1)
        self.assertEqual(hedges[0]["time_in_force"], maker_live.TimeInForce.IOC)
        self.assertEqual(hedges[0]["order_side"], OrderSide.BUY)  # we sold on the maker venue

    def test_live_repricing_uses_modify_order(self) -> None:
        """A re-price must cost one transaction, not a cancel plus a create."""
        strategy = build_strategy(mode="live")
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        placed = len(strategy.submitted)
        strategy.clock.advance(strategy._limits.quote.requote_min_s + 1.0)
        feed_books(strategy, m_bid="0.73010", m_ask="0.73050")
        strategy._decide()
        self.assertTrue(strategy.modified, "the touch moved, so the quote must be re-priced")
        self.assertEqual(len(strategy.submitted), placed, "a modify must not create an order")

    def test_a_venue_rate_limit_throttles_instead_of_failing(self) -> None:
        strategy = build_strategy(mode="live")
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        before = strategy._engine.capacity
        strategy.on_order_rejected(
            FakeFill(ClientOrderId("O-1"), MAKER_ID, OrderSide.SELL, "1", "0.73"),
        )
        # No 23000 in the reason -> a genuine failure.
        self.assertTrue(strategy.failures)

        strategy2 = build_strategy(mode="live")
        feed_books(strategy2)
        strategy2.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy2._decide()
        event = FakeFill(ClientOrderId("O-1"), MAKER_ID, OrderSide.SELL, "1", "0.73")
        event.reason = "23000 Too Many Requests"
        strategy2.on_order_rejected(event)
        self.assertEqual(strategy2.failures, [])
        self.assertLess(strategy2._engine.capacity, before)

    def test_stop_cancels_quotes_and_reports_no_leftovers_in_paper_mode(self) -> None:
        strategy = build_strategy()
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        strategy.on_stop()
        self.assertEqual(strategy._engine.orders, [None, None])
        self.assertEqual(strategy.leftovers, [])
        self.assertEqual(strategy.exit_code, maker_live.EXIT_OK)

    def test_an_unconfirmed_live_order_at_stop_is_a_leftover_and_a_non_zero_exit(self) -> None:
        strategy = build_strategy(mode="live")
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        # A cancel that the venue never confirms: the order stays open in the cache.
        strategy.cancel_order = lambda *a, **k: None  # type: ignore[method-assign]
        strategy.on_stop()
        self.assertTrue(strategy.leftovers)
        self.assertEqual(strategy.exit_code, maker_live.EXIT_FAILED)

    def test_daily_state_is_persisted_as_the_run_goes(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="persist-"))
        strategy = build_strategy(out=out)
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        strategy.on_trade(FakeTrade(MAKER_ID, "0.73040", "30", "BUY"))
        payload = json.loads((out / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["fills"], 1)
        self.assertGreater(payload["tx"], 0)


# --------------------------------------------------------------------------- cli


class TestCli(unittest.TestCase):
    """The mode guards, which are the last thing standing between a typo and a mainnet order."""

    @staticmethod
    def run_cli(argv: list[str]) -> tuple[int | None, str]:
        """Run ``main`` with its banner captured, so the test output stays readable."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = maker_live.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_mainnet_live_without_the_flag_is_refused(self) -> None:
        code, text = self.run_cli(["--live", "--env", "mainnet", "--symbol", "PONS"])
        self.assertEqual(code, maker_live.EXIT_REFUSED)
        self.assertIn("--confirm-mainnet", text)

    def test_mainnet_live_with_the_flag_still_needs_credentials(self) -> None:
        """The flag is not authorisation on its own; without keys the run still refuses."""
        code, text = self.run_cli(
            ["--live", "--env", "mainnet", "--symbol", "PONS", "--confirm-mainnet"],
        )
        self.assertEqual(code, maker_live.EXIT_REFUSED)
        self.assertIn("LIGHTER_", text)

    def test_the_banner_states_the_hard_caps(self) -> None:
        code, text = self.run_cli(["--dry-run", "--env", "mainnet", "--symbol", "PONS"])
        self.assertEqual(code, maker_live.EXIT_OK)
        self.assertIn("HARD cap", text)
        self.assertIn("bypass=False", text)
        self.assertIn("20 triggers/day", text)  # the PROMPT.md deviation is flagged

    def test_exactly_one_mode_is_required(self) -> None:
        for argv in ([], ["--paper", "--live"], ["--dry-run", "--paper"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                self.run_cli(argv)

    def test_unknown_symbol_for_the_environment_is_refused(self) -> None:
        with self.assertRaises(SystemExit):
            self.run_cli(["--dry-run", "--env", "testnet", "--symbol", "PONS"])

    def test_paper_and_live_never_share_a_state_file(self) -> None:
        """A paper session must not spend - or pollute - the live daily budget."""
        common = dict(
            env="mainnet", plan=maker_live.SYMBOLS["mainnet"]["PONS"],
            limits=load_limits(REPO / "config" / "limits.toml"), hedge_enabled=True,
            out_dir=Path("reports/live"), stamp="20260908T000000Z",
            deadline=datetime.now(timezone.utc),
        )
        paper = maker_live.RunPlan(mode="paper", **common)
        live = maker_live.RunPlan(mode="live", **common)
        self.assertEqual(paper.file_mode, "paper")
        self.assertEqual(live.file_mode, "live")
        self.assertNotEqual(paper.state_path, live.state_path)
        self.assertNotEqual(paper.fills_csv, live.fills_csv)
        self.assertNotEqual(paper.pnl_csv, live.pnl_csv)
        for path in (paper.state_path, paper.fills_csv, paper.pnl_csv):
            self.assertIn("paper", path.name)
        for path in (live.state_path, live.fills_csv, live.pnl_csv):
            self.assertIn("live", path.name)
        # A dry run writes nothing but can still name both budgets.
        dry = maker_live.RunPlan(mode="dry-run", **common)
        self.assertEqual(dry.state_path_for("live"), live.state_path)
        self.assertEqual(dry.state_path_for("paper"), paper.state_path)

    def test_paper_fills_do_not_touch_the_live_counters(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="isolate-"))
        common = dict(
            env="mainnet", plan=maker_live.SYMBOLS["mainnet"]["PONS"],
            limits=load_limits(REPO / "config" / "limits.toml"), hedge_enabled=True,
            out_dir=out, stamp="20260908T000000Z", deadline=datetime.now(timezone.utc),
        )
        paper = maker_live.RunPlan(mode="paper", **common)
        live = maker_live.RunPlan(mode="live", **common)
        state = DailyState.load(paper.state_path)
        state.fills = 42
        state.save()
        self.assertEqual(DailyState.load(live.state_path).fills, 0)

    def test_the_dry_run_banner_names_both_state_files(self) -> None:
        code, text = self.run_cli(["--dry-run", "--env", "mainnet", "--symbol", "PONS"])
        self.assertEqual(code, maker_live.EXIT_OK)
        self.assertIn("state_paper_mainnet_PONS.json", text)
        self.assertIn("state_live_mainnet_PONS.json", text)
        self.assertIn("daily state paper", text)
        self.assertIn("daily state live", text)

    def test_symbol_plans_match_the_verified_venue_listings(self) -> None:
        mainnet = maker_live.resolve_plan("mainnet", "PONS")
        self.assertEqual(mainnet.maker_id, "PONS-PERP.LIGHTER")
        self.assertEqual(mainnet.hedge_id, "PONSUSDT-PERP.ASTER")
        testnet = maker_live.resolve_plan("testnet", "DOGE")
        self.assertEqual(testnet.maker_id, "DOGE-PERP.LIGHTER")
        self.assertEqual(testnet.hedge_id, "DOGEUSDT-PERP.ASTER")


if __name__ == "__main__":
    unittest.main()

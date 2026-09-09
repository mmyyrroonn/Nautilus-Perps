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
import os
import sys
import tempfile
import unittest
from unittest import mock
from datetime import datetime
from datetime import timezone
import dataclasses
from dataclasses import replace as dc_replace
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
from quote_gates import GateParams  # noqa: E402
from quote_gates import QuoteGates  # noqa: E402
from quote_placement import PLACEMENTS  # noqa: E402
from quote_placement import place  # noqa: E402
from quote_placement import BasisParams  # noqa: E402
from quote_placement import BasisTracker  # noqa: E402
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


# --------------------------------------------------------------------------- safety


_CREDENTIAL_PREFIXES = ("LIGHTER_", "ASTER_", "HYPERLIQUID_")
_SCRUBBED: dict[str, str] = {}


def setUpModule() -> None:
    """Remove every venue credential from this process before any test runs.

    ``maker_live`` calls ``dotenv.load_dotenv()`` at import time, so in a worktree that has a
    real ``.env`` the credentials are simply present.  A test that invokes ``main()`` with
    ``--live --confirm-mainnet`` to prove it refuses WITHOUT keys then stops refusing: it sails
    past the guard, sleeps through the mainnet countdown and starts connecting to the real
    venue.  That happened here on 2026-09-09; the connect attempts timed out before the
    strategy started, so nothing was ordered, but the test suite must never be one working
    network away from a live session.

    Scrubbing at module scope makes the credential guards deterministic - they refuse in every
    worktree, with or without a .env - and removes any path from the unit tests to a venue.
    """
    for key in list(os.environ):
        if key.startswith(_CREDENTIAL_PREFIXES):
            _SCRUBBED[key] = os.environ.pop(key)


def tearDownModule() -> None:
    os.environ.update(_SCRUBBED)
    _SCRUBBED.clear()


class TestCredentialsAreScrubbedForTheSuite(unittest.TestCase):
    """The suite must be unable to reach a venue even in a worktree holding a real .env."""

    def test_no_venue_credential_is_visible_to_the_tests(self) -> None:
        leaked = [k for k in os.environ if k.startswith(_CREDENTIAL_PREFIXES)]
        self.assertEqual(leaked, [], f"credentials visible to the test process: {leaked}")

    def test_the_credential_helpers_report_absent(self) -> None:
        for env in ("mainnet", "testnet"):
            self.assertFalse(maker_live.lighter_credentials_present(env))
            self.assertFalse(maker_live.aster_credentials_present(env))

    def test_a_live_mainnet_invocation_cannot_get_past_the_guards(self) -> None:
        """Belt and braces: with the flag AND scrubbed keys, main() still refuses."""
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = maker_live.main(
                ["--live", "--env", "mainnet", "--symbol", "PONS", "--confirm-mainnet"],
            )
        self.assertEqual(code, maker_live.EXIT_REFUSED)
        self.assertNotIn("starting node", out.getvalue() + err.getvalue())


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
        self.instrument_id = kw.get("instrument_id", MAKER_ID)
        self.strategy_id = kw.get("strategy_id", "MAKER-TEST-001")
        self.filled_qty = Quantity.from_str(kw.get("filled_qty", "0"))
        self.quantity = quantity
        self.price = price
        self.order_side = side
        self.side = side  # real Nautilus orders expose `.side`; keep the stub in step
        self.status = FakeStatus(kw.get("status", "ACCEPTED"))
        self.is_closed = kw.get("is_closed", False)

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


class FakeDenied:
    """Stand-in for an OrderDenied / OrderRejected event."""

    def __init__(self, client_order_id, reason: str) -> None:
        self.client_order_id = client_order_id
        self.reason = reason


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


class FakePosition:
    """Stand-in for a reconciled Position: what the VENUE carries, whoever opened it."""

    def __init__(self, instrument_id, signed_qty: float, avg_px_open: float,
                 strategy_id: str = "OTHER-001") -> None:
        self.instrument_id = instrument_id
        self.signed_qty = signed_qty
        self.quantity = abs(signed_qty)
        self.avg_px_open = avg_px_open
        self.strategy_id = strategy_id
        self.side = "LONG" if signed_qty > 0 else "SHORT"
        self.is_open = True


class FakeCache:
    def __init__(self, instruments=()) -> None:
        self.instruments = {inst.id: inst for inst in instruments}
        self.orders: dict[object, FakeOrder] = {}
        self.positions: list[FakePosition] = []

    def instrument(self, instrument_id):
        return self.instruments.get(instrument_id)

    def order(self, client_order_id):
        return self.orders.get(client_order_id)

    def orders_open(self, instrument_id=None, **_kw):
        return [
            o for o in self.orders.values()
            if not o.is_closed
            and (instrument_id is None or o.instrument_id == instrument_id)
        ]

    def positions_open(self, instrument_id=None, **_kw):
        return [
            p for p in self.positions
            if p.is_open and (instrument_id is None or p.instrument_id == instrument_id)
        ]


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
            instrument_id=kwargs.get("instrument_id", MAKER_ID),
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
        # When True a cancel is recorded but the order stays OPEN in the cache, which is what
        # a venue that has not confirmed yet looks like.  The stop sequence must wait for it.
        self.cancel_is_silent = False
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
        if order is not None and not self.cancel_is_silent:
            order.close("CANCELED")
        elif order is not None:
            order.status = FakeStatus("PENDING_CANCEL")

    def modify_order(self, client_order_id, quantity=None, price=None, trigger_price=None,
                     client_id=None, params=None) -> None:
        self.modified.append((client_order_id, quantity, price))


SHIPPED_LIMITS = REPO / "config" / "limits.toml"


def test_limits(**quote_overrides):
    """The shipped limits with the QUOTING choice pinned, for the strategy tests.

    A strategy test must measure the strategy, not today's configuration.  When the shipped
    ``placement`` became ``anchor`` on 2026-09-10, twenty-five tests that had nothing to do
    with placement started failing - the anchor holds its opening quotes back for a 30-sample
    basis warm-up, so nothing was quoted inside their three decision ticks.  Every caps,
    budget, kill-switch and order-plumbing limit stays exactly as shipped; only the knobs that
    decide WHERE a quote rests are pinned, and ``TestShippedConfig`` covers what the file
    itself says.
    """
    limits = load_limits(SHIPPED_LIMITS)
    stop_keys = {f.name for f in dataclasses.fields(limits.stop)}
    stop_overrides = {k: quote_overrides.pop(k) for k in list(quote_overrides)
                      if k in stop_keys}
    quote = dc_replace(limits.quote, **{"placement": "improve", **quote_overrides})
    stop = dc_replace(limits.stop, **stop_overrides)
    return dc_replace(limits, quote=quote, stop=stop)


def build_strategy(
    *,
    mode: str = "paper",
    hedge: bool = True,
    limits=None,
    out: Path | None = None,
    clock: FakeClock | None = None,
    adopt_position: bool = False,
    positions: list | None = None,
    account_index: str = "12345",
    skip_cross_check: bool = False,
) -> MakerUnderTest:
    """A started-up strategy over stub surfaces, writing its CSVs into a temp directory."""
    out = out if out is not None else Path(tempfile.mkdtemp(prefix="maker-"))
    limits = limits if limits is not None else test_limits()
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
        adopt_position=adopt_position,
        account_index=account_index,
        skip_cross_check=skip_cross_check,
    )
    cache = FakeCache(instruments=(lighter_pons(), aster_pons()))
    cache.positions.extend(positions or [])
    strategy = MakerUnderTest(config, cache, FakeOrderFactory(), clock)
    strategy.on_start()
    return strategy


def feed_books(strategy: MakerUnderTest, *, m_bid="0.72820", m_ask="0.73220",
               h_bid="0.72980", h_ask="0.73010", h_bid_size="5000",
               h_ask_size="5000") -> None:
    """Push one quote per leg so the strategy has a complete book sample.

    The maker touch is 55 bps wide against a 4 bps hedge touch - the PONS book of the 09-07
    recording, and a book the opening gates accept.  It has to be: the shipped
    ``config/limits.toml`` refuses to open inventory under 12 bps of maker spread, so the
    narrower 5.5 bps book this fixture used to carry (which is the 09-09 book the gates were
    written to refuse) would leave every strategy test with nothing quoted at all.  Both maker
    mid and cross-venue mid are unchanged from that older fixture, so the marks, the P&L and
    the hedge arithmetic are the same numbers as before.

    Note that a book wide enough to pass the spread gate necessarily leaves an opening edge on
    BOTH sides - the gate demands ``maker spread >= 2 x (hedge spread + fees)``, and the two
    opening edges sum to ``maker spread - hedge spread - 2 ticks - 2 fees`` - so the strategy
    rests two quotes here where the old narrow fixture rested one.
    """
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


def replay_live(books, trades, params: QuoteParams, gates: QuoteGates | None = None,
                basis: BasisTracker | None = None):
    """Replay the tape through QuoteEngine + PaperBroker exactly as ``simulate()`` does.

    Sample ``i`` decides both quotes; the trades of ``(t_i, t_i+1]`` then hit them.  ``gates``
    is the live side of the opening gates: ``simulate()`` builds its own from the same knobs,
    so feeding both the same tape is what makes the gate decision checkable, not assumed.
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
        gated = False
        if gates is not None:
            gates.update(
                t,
                m_bid=sample.m_bid, m_ask=sample.m_ask,
                h_bid=sample.h_bid, h_ask=sample.h_ask,
                mid=sample.mid, dur=t_next - t,
            )
            gated = not gates.open
        if basis is not None:
            basis.update(
                t,
                m_bid=sample.m_bid, m_ask=sample.m_ask,
                h_bid=sample.h_bid, h_ask=sample.h_ask,
            )
        engine.step(
            sample, q, opening_gated=gated,
            basis=basis.basis if basis is not None else 0.0,
            basis_ready=basis.ready if basis is not None else True,
        )
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


def live_params(placement: str = "improve", **over) -> QuoteParams:
    base = dict(
        placement=placement, anchor_edge_bps=0.0,
        order_usd=20.0, max_inv_usd=45.0, min_edge_bps=3.0, reserve_bps=0.0,
        maker_fee_bps=0.0, hedge_fee_bps=FEE_H, tick=TICK, decimals=DECIMALS,
        requote_min_s=3.0, tx_per_min=36.0,
    )
    base.update(over)
    params = QuoteParams(**base)
    # The simulator never flips through zero, so parity is only meaningful with the live-only
    # close_min_flip behaviour off.  It defaults off here; assert it rather than assume it.
    assert params.close_min_flip is False or "close_min_flip" in over
    return params


# The opening-gate knobs.  They live on maker_inventory.Params for the replay and on a
# quote_gates.GateParams for the live engine, so _compare() routes them to both.
GATE_KEYS = (
    "min_spread_ratio", "min_maker_spread_bps", "spread_window_s",
    "max_move_bps_per_min", "vol_window_s",
)


# The basis estimator's knobs.  Like the gates they live on maker_inventory.Params for the
# replay and on a quote_placement.BasisParams for the live engine.
BASIS_KEYS = ("basis_window_s", "basis_min_n")


def live_basis(**over) -> BasisTracker | None:
    """The live copy of the estimator ``simulate()`` builds from the same knobs."""
    if not over:
        return None
    return BasisTracker(BasisParams(**over))


def live_gates(**over) -> QuoteGates | None:
    """The live copy of the gates ``simulate()`` builds from the same knobs."""
    if not over:
        return None
    return QuoteGates(GateParams(hedge_fee_bps=FEE_H, maker_fee_bps=0.0, **over))


def sim_params(placement: str = "improve", **over):
    kwargs = dict(SIM_KWARGS)
    kwargs.update(
        placement=placement, anchor_edge_bps=0.0,
        tx_per_min=36.0, requote_ticks=1, requote_min_s=3.0,
    )
    kwargs.update(over)
    return maker_inventory.Params(**kwargs)


class TestQuotingParity(unittest.TestCase):
    """The live quoting rule must reproduce maker_inventory.simulate() exactly.

    With the live-only restrictions neutral (no venue minimums, no unhedged suspension) the
    two must agree on every transaction and every fill increment.
    """

    def _compare(self, placement: str, tob_scale: float = 1.0, min_incs: int = 20,
                 **over) -> None:
        gate = {k: over.pop(k) for k in list(over) if k in GATE_KEYS}
        basis = {k: over.pop(k) for k in list(over) if k in BASIS_KEYS}
        books, trades = build_tape(tob_scale=tob_scale)
        run = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H,
            p=sim_params(placement, **over, **gate, **basis),
        )
        engine, fills, q = replay_live(
            books, trades, live_params(placement, **over), live_gates(**gate),
            live_basis(**{("window_s" if k == "basis_window_s" else "min_n"): v
                          for k, v in basis.items()}),
        )

        with self.subTest("the tape must actually fill, or the test proves nothing"):
            self.assertGreaterEqual(len(run.incs), min_incs)
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
        self.assertEqual(sum(run.gate_blocked), engine.counters["gate"])
        self.assertEqual(sum(run.basis_blocked), engine.counters["basis_warmup"])
        self.assertEqual(run.gated[ASK], engine.counters["gated_ask"])
        self.assertEqual(run.gated[BID], engine.counters["gated_bid"])

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

    # -- the placements -------------------------------------------------------------------
    #
    # The tape's maker touch is only ~5.5 bps wide, so an anchor much beyond 5 bps off the
    # hedge mid rests outside it and stops filling.  That is the point of the placement, and
    # both cases are worth pinning: one that trades and one that only quotes.

    def test_anchor_matches(self) -> None:
        self._compare("anchor", anchor_edge_bps=5.0)

    def test_anchor_matches_with_a_thin_book(self) -> None:
        """A thin top of book lets the queue-ahead branch of an at/outside anchor fill."""
        self._compare("anchor", tob_scale=0.02, anchor_edge_bps=5.0)

    def test_anchor_matches_when_it_rests_outside_the_touch_and_never_fills(self) -> None:
        self._compare("anchor", anchor_edge_bps=12.0, min_incs=0)

    def test_anchor_matches_without_a_requote_cadence(self) -> None:
        self._compare("anchor", anchor_edge_bps=5.0, requote_min_s=0.0)

    def test_anchor_matches_with_a_tight_transaction_budget(self) -> None:
        """A bucket this small defers most of the re-prices; both replays must defer the same
        ones, so the fills that do get through are few."""
        self._compare(
            "anchor", anchor_edge_bps=5.0, tx_per_min=14.0, requote_min_s=0.0, min_incs=3,
        )

    def test_anchor_matches_with_both_gates_on(self) -> None:
        self._compare(
            "anchor", anchor_edge_bps=5.0, min_spread_ratio=0.4, spread_window_s=5.0,
            max_move_bps_per_min=2.5, vol_window_s=6.0,
        )

    def test_anchor_with_a_basis_matches(self) -> None:
        """The estimator is stateful, so parity here also pins the warm-up and the window."""
        self._compare("anchor", anchor_edge_bps=5.0, basis_window_s=30.0, basis_min_n=10)

    def test_anchor_with_a_basis_and_a_skew_matches(self) -> None:
        self._compare(
            "anchor", anchor_edge_bps=5.0, basis_window_s=30.0, basis_min_n=10,
            anchor_skew_bps=8.0,
        )

    def test_anchor_with_a_long_warm_up_opens_nothing_in_either_replay(self) -> None:
        """min_n beyond the tape: never ready, so nothing is opened and nothing is sent.

        Flat throughout means there is no closing side either, so this is the one anchor case
        that produces no transactions at all - hence the direct comparison rather than
        ``_compare``, which insists on a tape that trades.
        """
        books, trades = build_tape()
        kwargs = dict(anchor_edge_bps=5.0, basis_window_s=300.0, basis_min_n=1000)
        run = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H,
            p=sim_params("anchor", **kwargs),
        )
        engine, fills, q = replay_live(
            books, trades, live_params("anchor", anchor_edge_bps=5.0),
            None, live_basis(window_s=300.0, min_n=1000),
        )
        self.assertGreater(sum(run.basis_blocked), 0, "the warm-up must actually bite")
        self.assertEqual((len(run.incs), run.tx_sent), (0, 0))
        self.assertEqual((fills, engine.tx_sent), ([], 0))
        self.assertEqual(sum(run.basis_blocked), engine.counters["basis_warmup"])

    def test_the_basis_knobs_at_zero_reproduce_the_uncorrected_anchor(self) -> None:
        books, trades = build_tape()
        plain = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H,
            p=sim_params("anchor", anchor_edge_bps=5.0),
        )
        zeroed = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H,
            p=sim_params("anchor", anchor_edge_bps=5.0, basis_window_s=0.0,
                         anchor_skew_bps=0.0),
        )
        self.assertEqual(plain.incs, zeroed.incs)
        self.assertEqual(plain.tx_sent, zeroed.tx_sent)
        self.assertEqual(sum(plain.basis_blocked), 0)
        self.assertEqual(plain.basis_bps, [0.0] * len(books))

    def test_the_basis_changes_the_anchor_it_is_applied_to(self) -> None:
        """Guard the regression above: a basis that did nothing would make it vacuous."""
        books, trades = build_tape()
        common = dict(tick=TICK, decimals=DECIMALS, fee_h=FEE_H)
        off = maker_inventory.simulate(
            books, trades, p=sim_params("anchor", anchor_edge_bps=5.0), **common,
        )
        on = maker_inventory.simulate(
            books, trades,
            p=sim_params("anchor", anchor_edge_bps=5.0, basis_window_s=30.0,
                         basis_min_n=10),
            **common,
        )
        self.assertNotEqual(off.incs, on.incs)
        self.assertNotEqual(off.basis_bps, on.basis_bps)

    def test_the_anchor_actually_rests_off_the_maker_touch(self) -> None:
        """Guard the cases above: an anchor that happened to equal improve proves nothing."""
        books, trades = build_tape()
        inside = at = outside = 0
        for i in range(len(books)):
            spot = place(
                "anchor", sell=True, m_bid=books.m_bid[i], m_ask=books.m_ask[i],
                h_bid=books.h_bid[i], h_ask=books.h_ask[i], tick=TICK, decimals=DECIMALS,
                tob=books.m_ask_size[i], anchor_edge_bps=5.0,
            )
            self.assertGreater(spot.price, books.m_bid[i], "an ask may never cross the bid")
            if spot.price < books.m_ask[i] - TICK / 2:
                inside += 1
            elif spot.price < books.m_ask[i] + TICK / 2:
                at += 1
            else:
                outside += 1
        for name, count in (("inside", inside), ("at", at), ("outside", outside)):
            with self.subTest(where=name):
                self.assertGreater(count, 0, "the tape must exercise this branch")

    def test_improve_and_join_are_unchanged_by_the_placement_refactor(self) -> None:
        """``place()`` must reproduce the two literal expressions it replaced, sample by
        sample - that is what makes "byte-identical" checkable rather than asserted."""
        books, trades = build_tape()
        for i in range(len(books)):
            for sell in (True, False):
                touch = books.m_ask[i] if sell else books.m_bid[i]
                opposite = books.m_bid[i] if sell else books.m_ask[i]
                tob = books.m_ask_size[i] if sell else books.m_bid_size[i]
                kw = dict(
                    sell=sell, m_bid=books.m_bid[i], m_ask=books.m_ask[i],
                    h_bid=books.h_bid[i], h_ask=books.h_ask[i],
                    tick=TICK, decimals=DECIMALS, tob=tob,
                )
                want_px = round(touch - TICK if sell else touch + TICK, DECIMALS)
                want_lock = (want_px <= opposite) if sell else (want_px >= opposite)
                got = place("improve", **kw)
                self.assertEqual((got.price, got.queue_ahead, got.locked),
                                 (want_px, 0.0, want_lock))
                got = place("join", **kw)
                self.assertEqual((got.price, got.queue_ahead, got.locked),
                                 (round(touch, DECIMALS), tob, False))

    # -- the opening gates ----------------------------------------------------------------
    #
    # The tape's maker touch spread is 0.55 / 0.62 / 0.69 bps against a 0.69 bps hedge spread
    # plus the 0.9 bps hedge fee, and its mid ranges 1.6 to 2.9 bps over six seconds, so
    # these thresholds sit inside both distributions and make each gate open and close instead
    # of being trivially on or off for all 120 samples.

    def test_matches_with_the_spread_gate_closing_and_reopening(self) -> None:
        self._compare("improve", min_spread_ratio=0.4, spread_window_s=5.0)

    def test_matches_with_the_maker_spread_floor_biting(self) -> None:
        self._compare("improve", min_maker_spread_bps=0.65, spread_window_s=5.0)

    def test_matches_with_the_volatility_gate_closing_and_reopening(self) -> None:
        self._compare("improve", max_move_bps_per_min=2.5, vol_window_s=6.0)

    def test_matches_with_both_gates_on(self) -> None:
        self._compare(
            "improve", min_spread_ratio=0.4, spread_window_s=5.0,
            max_move_bps_per_min=2.5, vol_window_s=6.0,
        )

    def test_the_gates_actually_bite_on_this_tape(self) -> None:
        """Guard the four cases above: a gate that never closes proves nothing."""
        books, trades = build_tape()
        gated = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H,
            p=sim_params("improve", min_spread_ratio=0.4, spread_window_s=5.0,
                         max_move_bps_per_min=2.5, vol_window_s=6.0),
        )
        self.assertGreater(sum(gated.gate_blocked), 0)
        self.assertGreater(gated.gate_spread_closed_s, 0.0)
        self.assertLess(gated.gate_spread_closed_s, gated.twa_den)
        self.assertGreater(gated.gate_vol_closed_s, 0.0)
        self.assertLess(gated.gate_vol_closed_s, gated.twa_den)

    def test_the_gate_knobs_at_zero_leave_the_replay_untouched(self) -> None:
        """The offline default is 0/0/0, and it must reproduce the pre-gate results exactly."""
        books, trades = build_tape()
        plain = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H, p=sim_params("improve"),
        )
        zeroed = maker_inventory.simulate(
            books, trades, tick=TICK, decimals=DECIMALS, fee_h=FEE_H,
            p=sim_params("improve", min_spread_ratio=0.0, min_maker_spread_bps=0.0,
                         max_move_bps_per_min=0.0),
        )
        self.assertEqual(plain.incs, zeroed.incs)
        self.assertEqual(plain.tx_sent, zeroed.tx_sent)
        self.assertEqual(sum(plain.gate_blocked), 0)
        self.assertEqual(plain.gate_closed_s, 0.0)
        self.assertEqual(plain.gate_spread_closed_s, 0.0)
        self.assertEqual(plain.gate_vol_closed_s, 0.0)


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

    def test_the_opening_side_is_gated_but_the_closing_side_is_not(self) -> None:
        """A closed opening gate may only ever remove the side that GROWS the inventory."""
        engine = QuoteEngine(live_params())
        actions = engine.step(self.sample(), q=0.0, opening_gated=True)
        self.assertEqual(actions, [])
        self.assertGreater(engine.counters["gate"], 0)

        # Long: the ask now CLOSES, so a closed gate must not silence it.
        engine = QuoteEngine(live_params())
        actions = engine.step(self.sample(), q=40.0, opening_gated=True)
        self.assertEqual([a.side for a in actions], [ASK])
        self.assertTrue(actions[0].quote.closing)

    def test_a_resting_opening_quote_is_cancelled_when_a_gate_closes(self) -> None:
        """And the cancel is a real transaction, drawn from the same token bucket."""
        engine = QuoteEngine(live_params())
        engine.step(self.sample(), q=0.0)
        self.assertIsNotNone(engine.orders[ASK])
        cancels = engine.tx_cancel
        actions = engine.step(self.sample(t=1001.0), q=0.0, opening_gated=True)
        self.assertTrue(any(a.kind == maker_live.CANCEL for a in actions))
        self.assertIsNone(engine.orders[ASK])
        self.assertEqual(engine.tx_cancel, cancels + 1)

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


# --------------------------------------------------------------------------- placement


def _ceil_tick(price: float) -> float:
    return round(math.ceil(price / TICK - 1e-6) * TICK, DECIMALS)


def _floor_tick(price: float) -> float:
    return round(math.floor(price / TICK + 1e-6) * TICK, DECIMALS)


class TestPlacement(unittest.TestCase):
    """``quote_placement.place()``: the one copy of the pricing rule, unit by unit.

    Numbers are on the PONS grid: tick 1e-5, five decimals.  A hedge mid of 0.735 with a
    12 bps anchor edge gives an ask of 0.735882 and a bid of 0.734118 before rounding, which
    is what every anchor case below starts from.
    """

    ANCHOR_ASK = 0.73589  # ceil(0.735882 / 1e-5) * 1e-5 - rounded AWAY from the hedge mid
    ANCHOR_BID = 0.73411  # floor(0.734118 / 1e-5) * 1e-5 - likewise

    def at(self, placement: str, *, sell: bool, m_bid: float, m_ask: float,
           h_bid: float = 0.73490, h_ask: float = 0.73510, tob: float = 400.0,
           edge: float = 12.0, basis: float = 0.0, skew: float = 0.0, inv: float = 0.0):
        return place(
            placement, sell=sell, m_bid=m_bid, m_ask=m_ask, h_bid=h_bid, h_ask=h_ask,
            tick=TICK, decimals=DECIMALS, tob=tob, anchor_edge_bps=edge,
            basis=basis, anchor_skew_bps=skew, inv_frac=inv,
        )

    # -- the two pre-existing rules, unchanged --------------------------------------------

    def test_improve_rests_one_tick_inside_with_no_queue(self) -> None:
        ask = self.at("improve", sell=True, m_bid=0.73000, m_ask=0.73040)
        self.assertAlmostEqual(ask.price, 0.73039, places=9)
        self.assertEqual(ask.queue_ahead, 0.0)
        self.assertFalse(ask.locked)
        bid = self.at("improve", sell=False, m_bid=0.73000, m_ask=0.73040)
        self.assertAlmostEqual(bid.price, 0.73001, places=9)

    def test_improve_locks_in_a_one_tick_book(self) -> None:
        for sell in (True, False):
            with self.subTest(sell=sell):
                self.assertTrue(
                    self.at("improve", sell=sell, m_bid=0.73000, m_ask=0.73001).locked,
                )

    def test_join_rests_at_the_touch_behind_the_whole_top_of_book(self) -> None:
        ask = self.at("join", sell=True, m_bid=0.73000, m_ask=0.73040, tob=400.0)
        self.assertAlmostEqual(ask.price, 0.73040, places=9)
        self.assertEqual(ask.queue_ahead, 400.0)
        self.assertFalse(ask.locked)

    # -- anchor ---------------------------------------------------------------------------

    def test_anchor_prices_off_the_hedge_mid_and_rounds_away_from_it(self) -> None:
        """Ceil the ask, floor the bid: rounding may only ever ADD edge, never give it away."""
        wide = dict(m_bid=0.73000, m_ask=0.74000)
        ask = self.at("anchor", sell=True, **wide)
        bid = self.at("anchor", sell=False, **wide)
        self.assertAlmostEqual(ask.price, self.ANCHOR_ASK, places=9)
        self.assertAlmostEqual(bid.price, self.ANCHOR_BID, places=9)
        self.assertGreater(ask.price, 0.735 * (1 + 12.0 / 1e4))
        self.assertLess(bid.price, 0.735 * (1 - 12.0 / 1e4))
        # ... and both are on the venue grid.
        for placed in (ask, bid):
            self.assertAlmostEqual(placed.price / TICK, round(placed.price / TICK), places=6)

    def test_a_price_already_on_a_tick_is_not_pushed_a_whole_tick_further(self) -> None:
        """0.7350 * (1 + 20 bps) = 0.73647 exactly; ceil must leave it alone."""
        ask = self.at("anchor", sell=True, m_bid=0.73000, m_ask=0.74000, edge=20.0)
        self.assertAlmostEqual(ask.price, 0.73647, places=9)

    def test_anchor_inside_the_touch_has_no_queue_ahead(self) -> None:
        wide = dict(m_bid=0.73000, m_ask=0.74000)
        self.assertEqual(self.at("anchor", sell=True, **wide).queue_ahead, 0.0)
        self.assertEqual(self.at("anchor", sell=False, **wide).queue_ahead, 0.0)

    def test_anchor_exactly_at_the_touch_queues_behind_the_top_of_book(self) -> None:
        ask = self.at("anchor", sell=True, m_bid=0.73000, m_ask=self.ANCHOR_ASK, tob=400.0)
        self.assertAlmostEqual(ask.price, self.ANCHOR_ASK, places=9)
        self.assertEqual(ask.queue_ahead, 400.0)
        bid = self.at("anchor", sell=False, m_bid=self.ANCHOR_BID, m_ask=0.74000, tob=400.0)
        self.assertAlmostEqual(bid.price, self.ANCHOR_BID, places=9)
        self.assertEqual(bid.queue_ahead, 400.0)

    def test_anchor_outside_the_touch_is_charged_the_whole_top_of_book(self) -> None:
        """The recordings only carry the top of book, so an unknown queue is a full one."""
        ask = self.at("anchor", sell=True, m_bid=0.73000, m_ask=0.73500, tob=400.0)
        self.assertAlmostEqual(ask.price, self.ANCHOR_ASK, places=9)
        self.assertGreater(ask.price, 0.73500)
        self.assertEqual(ask.queue_ahead, 400.0)

    def test_anchor_never_crosses_the_maker_book(self) -> None:
        """A maker book far above the hedge mid clamps the ask to one tick above the bid."""
        ask = self.at("anchor", sell=True, m_bid=0.74000, m_ask=0.74100)
        self.assertAlmostEqual(ask.price, 0.74001, places=9)
        self.assertGreater(ask.price, 0.74000, "an ask at or below the bid would take")
        self.assertEqual(ask.queue_ahead, 0.0, "0.74001 is strictly inside 0.74000/0.74100")
        # ... and a maker book far below clamps the bid to one tick under the ask.
        bid = self.at("anchor", sell=False, m_bid=0.72000, m_ask=0.72100)
        self.assertAlmostEqual(bid.price, 0.72099, places=9)
        self.assertLess(bid.price, 0.72100)
        self.assertEqual(bid.queue_ahead, 0.0)

    def test_anchor_quotes_a_one_tick_book_that_improve_refuses(self) -> None:
        for sell in (True, False):
            with self.subTest(sell=sell):
                placed = self.at("anchor", sell=sell, m_bid=0.73000, m_ask=0.73001)
                self.assertFalse(placed.locked)
        self.assertTrue(self.at("improve", sell=True, m_bid=0.73000, m_ask=0.73001).locked)

    def test_anchor_refuses_without_a_hedge_book(self) -> None:
        """No hedge mid, no anchor - falling back to the maker touch is the bug it avoids."""
        placed = self.at("anchor", sell=True, m_bid=0.73000, m_ask=0.74000,
                         h_bid=0.0, h_ask=0.0)
        self.assertTrue(placed.locked)

    def test_a_bigger_edge_rests_further_out_on_both_sides(self) -> None:
        wide = dict(m_bid=0.70000, m_ask=0.77000)
        asks = [self.at("anchor", sell=True, edge=e, **wide).price for e in (8, 12, 16, 20)]
        bids = [self.at("anchor", sell=False, edge=e, **wide).price for e in (8, 12, 16, 20)]
        self.assertEqual(asks, sorted(asks))
        self.assertEqual(bids, sorted(bids, reverse=True))

    # -- the basis term and the inventory skew ---------------------------------------------

    def test_a_basis_moves_the_whole_anchor_and_keeps_it_symmetric(self) -> None:
        """+25 bps of basis carries both quotes up by 25 bps; the edge around the fair mid is
        unchanged, which is the entire point of the correction."""
        wide = dict(m_bid=0.70000, m_ask=0.77000)
        fair = 0.735 * 1.0025
        ask = self.at("anchor", sell=True, basis=0.0025, **wide)
        bid = self.at("anchor", sell=False, basis=0.0025, **wide)
        self.assertAlmostEqual(ask.price, _ceil_tick(fair * 1.0012), places=9)
        self.assertAlmostEqual(bid.price, _floor_tick(fair * 0.9988), places=9)
        # ... and the mid of our own two quotes is the fair mid, to within a tick.
        self.assertAlmostEqual((ask.price + bid.price) / 2.0, fair, delta=TICK)

    def test_a_zero_basis_is_the_uncorrected_anchor(self) -> None:
        """The b = 0 path must survive: it is what every anchor result before the basis used."""
        wide = dict(m_bid=0.70000, m_ask=0.77000)
        for sell in (True, False):
            with self.subTest(sell=sell):
                self.assertEqual(
                    self.at("anchor", sell=sell, basis=0.0, **wide),
                    self.at("anchor", sell=sell, **wide),
                )
        self.assertAlmostEqual(self.at("anchor", sell=True, **wide).price,
                               self.ANCHOR_ASK, places=9)

    def test_the_skew_pushes_a_long_book_down_and_a_short_book_up(self) -> None:
        wide = dict(m_bid=0.70000, m_ask=0.77000)
        flat_ask = self.at("anchor", sell=True, **wide).price
        flat_bid = self.at("anchor", sell=False, **wide).price
        long_ask = self.at("anchor", sell=True, skew=8.0, inv=1.0, **wide).price
        long_bid = self.at("anchor", sell=False, skew=8.0, inv=1.0, **wide).price
        short_ask = self.at("anchor", sell=True, skew=8.0, inv=-1.0, **wide).price
        short_bid = self.at("anchor", sell=False, skew=8.0, inv=-1.0, **wide).price
        self.assertLess(long_ask, flat_ask, "long: sell cheaper, so the ask is easier to hit")
        self.assertLess(long_bid, flat_bid, "long: bid lower, so it is harder to get longer")
        self.assertGreater(short_ask, flat_ask)
        self.assertGreater(short_bid, flat_bid)

    def test_the_skew_is_proportional_and_clamped_at_a_full_book(self) -> None:
        wide = dict(m_bid=0.70000, m_ask=0.77000)
        half = self.at("anchor", sell=True, skew=20.0, inv=0.5, **wide).price
        full = self.at("anchor", sell=True, skew=20.0, inv=1.0, **wide).price
        over = self.at("anchor", sell=True, skew=20.0, inv=4.0, **wide).price
        flat = self.at("anchor", sell=True, skew=20.0, inv=0.0, **wide).price
        self.assertLess(full, half)
        self.assertLess(half, flat)
        self.assertEqual(over, full, "an over-full book cannot lean further than a full one")

    def test_a_zero_skew_ignores_the_inventory_entirely(self) -> None:
        wide = dict(m_bid=0.70000, m_ask=0.77000)
        for inv in (-1.0, 0.0, 0.7):
            with self.subTest(inv=inv):
                self.assertEqual(self.at("anchor", sell=True, inv=inv, **wide),
                                 self.at("anchor", sell=True, **wide))

    def test_improve_and_join_ignore_the_basis_and_the_skew(self) -> None:
        book = dict(m_bid=0.73000, m_ask=0.73040)
        for placement in ("improve", "join"):
            for sell in (True, False):
                with self.subTest(placement=placement, sell=sell):
                    self.assertEqual(
                        self.at(placement, sell=sell, basis=0.0025, skew=20.0, inv=1.0,
                                **book),
                        self.at(placement, sell=sell, **book),
                    )

    def test_every_placement_name_is_handled(self) -> None:
        for placement in PLACEMENTS:
            with self.subTest(placement=placement):
                placed = self.at(placement, sell=True, m_bid=0.73000, m_ask=0.74000)
                self.assertGreater(placed.price, 0.0)


class TestBasisTracker(unittest.TestCase):
    """``quote_placement.BasisTracker``: the rolling median of ``m_mid / h_mid - 1``."""

    def tracker(self, window_s: float = 300.0, min_n: int = 30) -> BasisTracker:
        return BasisTracker(BasisParams(window_s=window_s, min_n=min_n))

    @staticmethod
    def push(tracker: BasisTracker, t: float, *, basis_bps: float, h_mid: float = 0.78):
        """One sample with the maker mid exactly ``basis_bps`` above the hedge mid."""
        m_mid = h_mid * (1.0 + basis_bps / 1e4)
        return tracker.update(
            t, m_bid=m_mid - 0.001, m_ask=m_mid + 0.001,
            h_bid=h_mid - 0.0005, h_ask=h_mid + 0.0005,
        )

    def test_a_constant_basis_is_recovered_exactly(self) -> None:
        tracker = self.tracker(min_n=3)
        for i in range(5):
            self.push(tracker, 1000.0 + i, basis_bps=25.0)
        self.assertAlmostEqual(tracker.basis_bps, 25.0, places=6)
        self.assertAlmostEqual(tracker.fair(0.78), 0.78 * 1.0025, places=12)

    def test_it_is_a_median_so_one_dislocated_sample_cannot_move_it(self) -> None:
        tracker = self.tracker(min_n=3)
        for i in range(10):
            self.push(tracker, 1000.0 + i, basis_bps=25.0)
        self.push(tracker, 1010.0, basis_bps=900.0)
        self.assertAlmostEqual(tracker.basis_bps, 25.0, places=6)
        self.assertAlmostEqual(tracker.raw * 1e4, 900.0, places=6)

    def test_the_window_forgets_samples_older_than_it(self) -> None:
        tracker = self.tracker(window_s=10.0, min_n=3)
        for i in range(10):
            self.push(tracker, 1000.0 + i, basis_bps=25.0)
        for i in range(10, 30):
            self.push(tracker, 1000.0 + i, basis_bps=40.0)
        self.assertAlmostEqual(tracker.basis_bps, 40.0, places=6)
        self.assertLessEqual(tracker.n, 11)

    def test_it_is_not_ready_until_min_n_samples_have_arrived(self) -> None:
        tracker = self.tracker(min_n=30)
        for i in range(29):
            self.push(tracker, 1000.0 + i, basis_bps=25.0)
            self.assertFalse(tracker.ready)
        self.push(tracker, 1029.0, basis_bps=25.0)
        self.assertTrue(tracker.ready)

    def test_the_warm_up_and_the_ready_line_are_each_said_once(self) -> None:
        tracker = self.tracker(min_n=3)
        said = []
        for i in range(10):
            said += self.push(tracker, 1000.0 + i, basis_bps=25.0)
        self.assertEqual(len(said), 2, "one warming-up line, one ready line, nothing else")
        self.assertIn("basis warming up", said[0])
        self.assertIn("basis ready", said[1])
        self.assertIn("+25.0 bps", said[1])

    def test_a_disabled_window_is_always_ready_at_a_zero_basis(self) -> None:
        """``basis_window_s = 0`` is the uncorrected anchor, and must stay reachable."""
        tracker = self.tracker(window_s=0.0)
        for i in range(50):
            self.assertEqual(self.push(tracker, 1000.0 + i, basis_bps=25.0), [])
        self.assertTrue(tracker.ready)
        self.assertEqual(tracker.basis, 0.0)
        self.assertEqual(tracker.fair(0.78), 0.78)
        self.assertEqual(tracker.n, 0)

    def test_a_half_book_is_ignored(self) -> None:
        tracker = self.tracker(min_n=1)
        tracker.update(1000.0, m_bid=0.779, m_ask=0.781, h_bid=0.0, h_ask=0.0)
        self.assertEqual(tracker.n, 0)
        self.assertEqual(tracker.basis, 0.0)


# --------------------------------------------------------------------------- gates


class TestQuoteGates(unittest.TestCase):
    """``quote_gates.QuoteGates``: the rule shared by the live maker and the replay.

    The numbers come from the sessions that motivated it - a 46 bps Lighter touch on the
    09-07 recording, 5.9 bps against an ~8 bps Aster touch plus a 0.9 bps taker fee on
    2026-09-09 mainnet.
    """

    def gates(self, **over) -> QuoteGates:
        base = dict(
            min_spread_ratio=2.0, min_maker_spread_bps=12.0, spread_window_s=10.0,
            hedge_fee_bps=0.9, maker_fee_bps=0.0,
        )
        base.update(over)
        return QuoteGates(GateParams(**base))

    @staticmethod
    def push(gates: QuoteGates, t: float, *, maker_bps: float, hedge_bps: float = 7.7,
             mid: float = 0.78, dur: float = 1.0) -> list[str]:
        """One sample with both venues centred on ``mid`` and the touches given in bps."""
        half_m = mid * maker_bps / 2e4
        half_h = mid * hedge_bps / 2e4
        return gates.update(
            t, m_bid=mid - half_m, m_ask=mid + half_m,
            h_bid=mid - half_h, h_ask=mid + half_h, mid=mid, dur=dur,
        )

    def test_a_wide_maker_spread_opens_the_gate(self) -> None:
        gates = self.gates()
        self.push(gates, 1000.0, maker_bps=46.0)
        self.assertTrue(gates.open)
        self.assertAlmostEqual(gates.spread_bps, 46.0, places=6)
        self.assertAlmostEqual(gates.cost_bps, 7.7 + 0.9, places=6)

    def test_the_2026_09_09_book_closes_the_gate(self) -> None:
        gates = self.gates()
        lines = self.push(gates, 1000.0, maker_bps=5.9)
        self.assertFalse(gates.open)
        self.assertEqual(len(lines), 1)
        self.assertIn("spread gate closed", lines[0])
        self.assertIn("5.9 bps < 2 x 8.6 bps", lines[0])
        self.assertIn("hedge 7.7 + fees 0.9", lines[0])

    def test_the_absolute_floor_closes_the_gate_on_its_own(self) -> None:
        """A 10 bps spread clears 2 x a cheap hedge and is still not worth quoting."""
        gates = self.gates()
        lines = self.push(gates, 1000.0, maker_bps=10.0, hedge_bps=0.5)
        self.assertFalse(gates.open)
        self.assertIn("floor 12 bps", lines[0])

    def test_the_transition_is_announced_once_not_once_a_sample(self) -> None:
        gates = self.gates()
        self.push(gates, 1000.0, maker_bps=46.0)
        said = 0
        for i in range(1, 11):
            said += len(self.push(gates, 1000.0 + i, maker_bps=5.9))
        self.assertEqual(said, 1, "one line for the close, nothing for the ten samples after")
        self.assertEqual(gates.transitions, 1)
        # ... and one more when it re-opens, not one per sample either.
        said = 0
        for i in range(11, 40):
            said += len(self.push(gates, 1000.0 + i, maker_bps=46.0))
        self.assertEqual(said, 1)
        self.assertIn("spread gate open", gates._spread_line())

    def test_the_median_rides_out_a_single_wide_sample(self) -> None:
        """The gate reads a rolling median precisely so one flickering print cannot open it."""
        gates = self.gates(spread_window_s=10.0)
        for i in range(9):
            self.push(gates, 1000.0 + i, maker_bps=5.9)
        self.assertFalse(gates.open)
        self.push(gates, 1009.0, maker_bps=60.0)
        self.assertFalse(gates.open, "one wide sample must not re-open the gate")
        self.assertAlmostEqual(gates.raw_spread_bps, 60.0, places=6)

    def test_the_window_forgets_samples_older_than_it(self) -> None:
        gates = self.gates(spread_window_s=10.0)
        for i in range(10):
            self.push(gates, 1000.0 + i, maker_bps=5.9)
        for i in range(10, 25):
            self.push(gates, 1000.0 + i, maker_bps=46.0)
        self.assertTrue(gates.open)
        self.assertAlmostEqual(gates.spread_bps, 46.0, places=6)

    def test_the_volatility_gate_closes_while_the_mid_runs(self) -> None:
        gates = self.gates(
            min_spread_ratio=0.0, min_maker_spread_bps=0.0,
            max_move_bps_per_min=25.0, vol_window_s=60.0,
        )
        for i in range(10):
            self.push(gates, 1000.0 + i, maker_bps=46.0, mid=0.78)
        self.assertTrue(gates.open)
        lines = self.push(gates, 1010.0, maker_bps=46.0, mid=0.78 * 1.004)  # a 40 bps jump
        self.assertFalse(gates.open)
        self.assertEqual(len(lines), 1)
        self.assertIn("volatility gate closed", lines[0])
        self.assertGreater(gates.vol_bps, 25.0)
        # Once the jump has aged out of the window the gate re-opens by itself.
        lines = self.push(gates, 1090.0, maker_bps=46.0, mid=0.78 * 1.004)
        self.assertTrue(gates.open)
        self.assertIn("volatility gate open", lines[0])

    def test_both_gates_must_be_open_to_open_inventory(self) -> None:
        gates = self.gates(max_move_bps_per_min=25.0, vol_window_s=60.0)
        self.push(gates, 1000.0, maker_bps=46.0, mid=0.78)
        self.assertTrue(gates.open)
        self.push(gates, 1001.0, maker_bps=46.0, mid=0.78 * 1.01)  # vol only
        self.assertTrue(gates.spread_open)
        self.assertFalse(gates.vol_open)
        self.assertFalse(gates.open)

    def test_knobs_at_zero_never_close_anything(self) -> None:
        gates = QuoteGates(GateParams(hedge_fee_bps=0.9))
        self.assertFalse(gates.p.on)
        for i in range(20):
            self.assertEqual(self.push(gates, 1000.0 + i, maker_bps=0.1, mid=0.78 * (1 + i)), [])
        self.assertTrue(gates.open)
        self.assertEqual(gates.closed_s, 0.0)

    def test_the_closed_share_is_time_weighted(self) -> None:
        # A window shorter than the sample spacing takes the median of one sample, so the
        # share below is the raw book and not the smoothing.
        gates = self.gates(spread_window_s=0.5)
        for i in range(3):
            self.push(gates, 1000.0 + i, maker_bps=46.0, dur=1.0)
        for i in range(3, 5):
            self.push(gates, 1000.0 + i, maker_bps=5.9, dur=1.0)
        self.assertAlmostEqual(gates.total_s, 5.0, places=9)
        self.assertAlmostEqual(gates.closed_pct, 40.0, places=6)
        self.assertAlmostEqual(gates.spread_closed_pct, 40.0, places=6)
        self.assertAlmostEqual(gates.vol_closed_pct, 0.0, places=6)
        self.assertIn("gate=closed", gates.status())

    def test_a_missing_hedge_book_never_talks_the_gate_open(self) -> None:
        """With no hedge touch the round trip still costs its fees, never zero."""
        gates = self.gates()
        gates.update(1000.0, m_bid=0.77, m_ask=0.79, h_bid=0.0, h_ask=0.0, mid=0.78)
        self.assertAlmostEqual(gates.cost_bps, 0.9, places=9)
        self.assertTrue(gates.open)  # 256 bps of maker spread clears 2 x 0.9 and the floor


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
            placement="improve", anchor_edge_bps=0.0, order_usd=20.0, max_inv_usd=45.0,
            min_edge_bps=3.0, reserve_bps=0.0, maker_fee_bps=0.0, hedge_fee_bps=0.9,
            tick=TICK, decimals=DECIMALS,
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


# --------------------------------------------------------------------------- profit gate


class TestProfitGatedFillCap(unittest.TestCase):
    """The extended daily fill budget a profitable day earns, and how it is given back.

    Base 300 fills; 1500 once today's REALISED net clears the 1 USD gate with a trip-net EWMA
    that is not negative; hysteresis holds it while realised net is merely still positive; it
    re-locks at realised net <= 0, at which point a session already past 300 stops.
    """

    def setUp(self) -> None:
        self.limits = load_limits(REPO / "config" / "limits.toml")
        self.path = Path(tempfile.mkdtemp(prefix="gate-")) / "state.json"
        self.state = DailyState.load(self.path)
        self.pnl = PnLMonitor(maker_fee_bps=0.0, hedge_fee_bps=0.9, trip_window=20)

    def check(self, **over):
        switch = KillSwitch(self.limits, self.state)
        kwargs = dict(pnl=self.pnl, mid_m=0.73, mid_h=0.73, hedge_breach_s=0.0,
                      day_fills=0, day_tx=0)
        kwargs.update(over)
        checks = switch.evaluate(**kwargs)
        return switch, {c.name: c for c in checks}

    def trips(self, realized_each: float, count: int = 3) -> None:
        for _ in range(count):
            self.pnl._record_trip(realized_each, 0.0, 10.0)

    # -- unlocking ------------------------------------------------------------------------

    def test_the_base_cap_applies_to_a_flat_day(self) -> None:
        switch, by_name = self.check(day_fills=299)
        self.assertEqual(by_name["day_fills"].threshold, 300)
        self.assertEqual(by_name["day_fills"].detail, "base")
        self.assertFalse(self.state.profit_unlocked)
        self.assertIsNone(switch.reason)

    def test_the_base_cap_still_stops_a_flat_day(self) -> None:
        switch, _ = self.check(day_fills=300)
        self.assertIn("day_fills", switch.reason or "")

    def test_clearing_the_gate_with_a_flat_ewma_unlocks(self) -> None:
        """No trips yet means no EWMA, which counts as not negative."""
        self.state.realized_net_usd = self.limits.daily.profit_gate_usd + 0.01
        switch, by_name = self.check(day_fills=400)
        self.assertTrue(self.state.profit_unlocked)
        self.assertTrue(switch.fill_cap_unlocked)
        self.assertEqual(switch.fill_cap, 1500)
        self.assertEqual(by_name["day_fills"].threshold, 1500)
        self.assertEqual(by_name["day_fills"].detail, "unlocked")
        self.assertIsNone(switch.reason, "400 fills is inside the extended budget")

    def test_clearing_the_gate_with_a_positive_ewma_unlocks(self) -> None:
        self.trips(0.05)  # +50 bps per trip
        self.state.realized_net_usd = 2.0
        switch, _ = self.check(day_fills=400)
        self.assertTrue(self.state.profit_unlocked)
        self.assertEqual(switch.fill_cap, 1500)

    def test_a_negative_ewma_never_unlocks_however_profitable_the_day_looks(self) -> None:
        """Grinding out losing round trips must not buy a bigger budget."""
        self.trips(-0.01)  # -10 bps per trip
        self.assertLess(self.pnl.trip_bps_ewma, 0.0)
        self.state.realized_net_usd = 99.0
        switch, by_name = self.check(day_fills=400)
        self.assertFalse(self.state.profit_unlocked)
        self.assertEqual(by_name["day_fills"].threshold, 300)
        self.assertIn("day_fills", switch.reason or "")

    def test_profit_at_or_below_the_gate_does_not_unlock(self) -> None:
        self.state.realized_net_usd = self.limits.daily.profit_gate_usd
        self.check(day_fills=10)
        self.assertFalse(self.state.profit_unlocked)

    def test_unrealised_profit_does_not_unlock(self) -> None:
        """The gate reads persisted REALISED net; an open position marked up is not profit."""
        self.pnl.maker_fill(sell=False, base=100.0, price=0.7300)  # open, unrealised only
        self.assertEqual(self.state.realized_net_usd, 0.0)
        self.check(mid_m=0.9, mid_h=0.9, day_fills=10)
        self.assertFalse(self.state.profit_unlocked)

    # -- hysteresis -----------------------------------------------------------------------

    def test_the_extended_cap_holds_below_the_gate_once_unlocked(self) -> None:
        """Otherwise a day hovering at the gate would flip caps every second."""
        self.state.realized_net_usd = 1.5
        self.check()
        self.assertTrue(self.state.profit_unlocked)

        self.state.realized_net_usd = 0.5  # under the gate, still a profitable day
        switch, by_name = self.check(day_fills=400)
        self.assertTrue(self.state.profit_unlocked)
        self.assertEqual(by_name["day_fills"].threshold, 1500)
        self.assertIsNone(switch.reason)

    def test_giving_the_profit_back_re_locks_and_stops_a_long_session(self) -> None:
        self.state.realized_net_usd = 1.5
        self.check()
        self.assertTrue(self.state.profit_unlocked)

        self.state.realized_net_usd = 0.0  # the day is no longer paying for the budget
        switch, by_name = self.check(day_fills=400)
        self.assertFalse(self.state.profit_unlocked)
        self.assertEqual(by_name["day_fills"].threshold, 300)
        self.assertIn("day_fills", switch.reason or "")

    def test_re_locking_below_the_base_cap_does_not_stop_the_session(self) -> None:
        self.state.realized_net_usd = 1.5
        self.check()
        self.state.realized_net_usd = -0.2
        switch, by_name = self.check(day_fills=120)
        self.assertFalse(self.state.profit_unlocked)
        self.assertEqual(by_name["day_fills"].threshold, 300)
        self.assertIsNone(switch.reason)

    def test_re_unlocking_needs_the_gate_again_not_merely_a_positive_net(self) -> None:
        self.state.realized_net_usd = 1.5
        self.check()
        self.state.realized_net_usd = -0.1
        self.check()
        self.assertFalse(self.state.profit_unlocked)
        self.state.realized_net_usd = 0.5  # positive, but back under the gate
        self.check()
        self.assertFalse(self.state.profit_unlocked)
        self.state.realized_net_usd = 1.5
        self.check()
        self.assertTrue(self.state.profit_unlocked)

    def test_a_flip_is_reported_so_the_caller_can_persist_it(self) -> None:
        self.state.realized_net_usd = 1.5
        switch, _ = self.check()
        self.assertTrue(switch.unlock_changed)
        switch, _ = self.check()  # a fresh switch, no further transition
        self.assertFalse(switch.unlock_changed)

    # -- the loss kill is untouched --------------------------------------------------------

    def test_the_loss_kill_still_applies_while_unlocked(self) -> None:
        self.state.realized_net_usd = 1.5
        self.check()
        self.assertTrue(self.state.profit_unlocked)
        self.state.realized_net_usd = -self.limits.kill.max_loss_usd - 0.01
        switch, _ = self.check(day_fills=10)
        self.assertIn("net_usd", switch.reason or "")

    # -- persistence ----------------------------------------------------------------------

    def test_the_unlock_survives_a_simulated_restart(self) -> None:
        self.state.realized_net_usd = 1.5
        self.state.fills = 400
        self.check()
        self.assertTrue(self.state.profit_unlocked)
        self.state.save()

        restarted = DailyState.load(self.path)  # the "restart"
        self.assertTrue(restarted.profit_unlocked)
        self.assertTrue(restarted.unlocked_now())
        switch = KillSwitch(self.limits, restarted)
        self.assertEqual(switch.fill_cap, 1500, "the budget the day earned is not re-rolled")
        checks = {c.name: c for c in switch.evaluate(
            pnl=self.pnl, mid_m=0.73, mid_h=0.73, hedge_breach_s=0.0,
            day_fills=restarted.fills, day_tx=0,
        )}
        self.assertEqual(checks["day_fills"].threshold, 1500)
        self.assertIsNone(switch.reason)

    def test_a_new_utc_day_starts_locked_again(self) -> None:
        self.state.realized_net_usd = 5.0
        self.state.profit_unlocked = True
        self.state.save()
        rolled = DailyState.load(self.path, day="2099-01-01")
        self.assertFalse(rolled.profit_unlocked)
        self.assertEqual(rolled.fills, 0)

    def test_a_stale_flag_with_a_non_positive_net_is_ignored_on_load(self) -> None:
        """A process killed between re-locking and saving must not leave the budget open."""
        self.state.profit_unlocked = True
        self.state.realized_net_usd = -1.0
        self.state.save()
        reloaded = DailyState.load(self.path)
        self.assertTrue(reloaded.profit_unlocked)
        self.assertFalse(reloaded.unlocked_now())
        self.assertEqual(KillSwitch(self.limits, reloaded).fill_cap, 300)

    # -- reporting ------------------------------------------------------------------------

    def test_the_margin_names_the_active_budget(self) -> None:
        self.state.realized_net_usd = 1.5
        _, by_name = self.check(day_fills=412)
        self.assertEqual(by_name["day_fills"].margin(), "day_fills 412/1500 (unlocked)")

    def test_the_margin_names_the_base_budget(self) -> None:
        _, by_name = self.check(day_fills=12)
        self.assertEqual(by_name["day_fills"].margin(), "day_fills 12/300 (base)")


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


class TestStrategyPlacement(unittest.TestCase):
    """The placement as the running strategy applies it."""

    @staticmethod
    def _anchor_limits(edge: float = 12.0):
        """The test limits with the placement switched to anchor at a named edge.

        The edge is pinned here rather than inherited from the shipped file: these tests
        assert exact prices, so they have to own the number that produces them.
        """
        return test_limits(placement="anchor", anchor_edge_bps=edge)

    def _quote(self, strategy, seconds: int = 40) -> None:
        """Long enough by default to clear the anchor's 30-sample basis warm-up."""
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        for _ in range(seconds):
            feed_books(strategy)
            strategy._decide()
            strategy.clock.advance(1.0)

    def test_the_improve_fixture_quotes_a_tick_inside_the_touch(self) -> None:
        strategy = build_strategy(limits=test_limits(placement="improve"))
        self.assertEqual(strategy._engine.p.placement, "improve")
        self._quote(strategy)
        # m_ask 0.73220 less one tick.
        self.assertAlmostEqual(strategy._engine.orders[ASK].price, 0.73219, places=9)

    def test_anchor_prices_both_sides_off_the_fair_mid(self) -> None:
        """The fixture's basis is 0.73020 / 0.72995 - 1 = +3.4 bps, so the fair mid is the
        maker mid 0.73020, and the two quotes sit 12 bps either side of THAT - not either
        side of the hedge mid, which is where the uncorrected anchor put them."""
        strategy = build_strategy(limits=self._anchor_limits())
        self.assertEqual(strategy._engine.p.placement, "anchor")
        self.assertEqual(strategy._engine.p.anchor_edge_bps, 12.0)
        self._quote(strategy)
        self.assertAlmostEqual(strategy._basis.basis_bps, 3.4249, places=3)
        self.assertAlmostEqual(strategy._engine.orders[ASK].price, 0.73108, places=9)
        self.assertAlmostEqual(strategy._engine.orders[BID].price, 0.72932, places=9)
        # Strictly inside 0.72820 / 0.73220, so nothing is resting ahead of us.
        self.assertEqual(strategy._engine.orders[ASK].queue, 0.0)

    def test_the_anchor_opens_nothing_until_the_basis_is_estimated(self) -> None:
        strategy = build_strategy(limits=self._anchor_limits())
        self._quote(strategy, seconds=5)
        self.assertFalse(strategy._basis.ready)
        self.assertEqual(strategy._engine.orders, [None, None])
        self.assertGreater(strategy._engine.counters["basis_warmup"], 0)
        self._quote(strategy, seconds=40)
        self.assertTrue(strategy._basis.ready)
        self.assertIsNotNone(strategy._engine.orders[ASK])

    def test_a_wider_anchor_edge_rests_further_out(self) -> None:
        strategy = build_strategy(limits=self._anchor_limits(edge=20.0))
        self._quote(strategy)
        self.assertGreater(strategy._engine.orders[ASK].price, 0.73108)
        self.assertLess(strategy._engine.orders[BID].price, 0.72932)

    def test_the_status_line_and_the_summary_name_the_placement(self) -> None:
        strategy = build_strategy(limits=self._anchor_limits())
        self._quote(strategy)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            strategy._status()
        self.assertIn("place=anchor 12", out.getvalue())
        self.assertIn("basis=+3.4", out.getvalue())
        self.assertIn("placement=anchor 12", strategy.summary())
        self.assertIn("basis_bps=+3.4", strategy.summary())


class TestStrategyOpeningGates(unittest.TestCase):
    """The gates as the running strategy applies them.

    The gate numbers are pinned by the fixture below rather than taken from the shipped file:
    these tests assert exactly which books open and close the gate, so they have to own the
    thresholds that decide it.  ``TestShippedConfig`` covers what the file ships today.
    """

    GATES = dict(min_spread_ratio=2.0, min_maker_spread_bps=12.0, max_move_bps_per_min=25.0)

    @classmethod
    def limits(cls, **over):
        return test_limits(**{**cls.GATES, **over})

    @staticmethod
    def _quote(strategy, *, seconds: int = 3, **book) -> None:
        """Run ``seconds`` decision ticks past the startup grace on the given book."""
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        for _ in range(seconds):
            feed_books(strategy, **book)
            strategy._decide()
            strategy.clock.advance(1.0)

    def test_the_gates_are_built_from_the_limits(self) -> None:
        params = build_strategy(limits=self.limits())._gates.p
        self.assertEqual(params.min_spread_ratio, 2.0)
        self.assertEqual(params.min_maker_spread_bps, 12.0)
        self.assertEqual(params.max_move_bps_per_min, 25.0)
        self.assertEqual(params.hedge_fee_bps, maker_live.ASTER_TAKER_FEE_BPS)

    def test_a_book_that_pays_for_the_hedge_quotes_normally(self) -> None:
        strategy = build_strategy(limits=self.limits())
        self._quote(strategy)
        self.assertTrue(strategy._gates.open)
        self.assertIsNotNone(strategy._engine.orders[ASK])
        self.assertEqual(strategy._gates.closed_pct, 0.0)

    def test_the_2026_09_09_book_stops_the_strategy_opening(self) -> None:
        """5.5 bps of maker spread against a 4 bps hedge touch: nothing may be opened."""
        strategy = build_strategy(limits=self.limits())
        self._quote(strategy, m_bid="0.73000", m_ask="0.73040")
        self.assertFalse(strategy._gates.open)
        self.assertEqual(strategy._engine.orders, [None, None])
        self.assertGreater(strategy._engine.counters["gate"], 0)
        self.assertEqual(strategy._gates.closed_pct, 100.0)

    def test_the_closing_side_keeps_quoting_while_the_gate_is_closed(self) -> None:
        """Inventory taken on before the gate closed must still be workable off passively."""
        strategy = build_strategy(limits=self.limits())
        strategy._q = 40.0  # long on Lighter: the ask closes, the bid would open
        self._quote(strategy, m_bid="0.73000", m_ask="0.73040")
        ask = strategy._engine.orders[ASK]
        self.assertIsNotNone(ask, "the closing ask must survive a closed gate")
        self.assertTrue(ask.closing)
        self.assertIsNone(strategy._engine.orders[BID], "but the opening bid must not")

    def test_a_resting_opening_quote_is_pulled_when_the_book_narrows(self) -> None:
        """Live mode, so the cancel is a real venue call and not just an engine bookkeeping."""
        strategy = build_strategy(mode="live", limits=self.limits())
        self._quote(strategy, seconds=2)
        self.assertTrue(strategy.submitted, "the wide book must have put quotes out")
        cancels = len(strategy.cancelled)
        self._quote(strategy, seconds=2, m_bid="0.73000", m_ask="0.73040")
        self.assertGreater(len(strategy.cancelled), cancels)
        self.assertEqual(strategy._engine.orders, [None, None])
        self.assertGreater(strategy._engine.tx_cancel, 0)

    def test_the_kill_switch_is_untouched_by_a_closed_gate(self) -> None:
        strategy = build_strategy(limits=self.limits())
        self._quote(strategy, m_bid="0.73000", m_ask="0.73040")
        self.assertFalse(strategy._gates.open)
        self.assertIsNone(strategy.kill_reason)
        self.assertEqual(strategy.exit_code, maker_live.EXIT_OK)

    def test_the_status_line_and_the_summary_report_the_gate(self) -> None:
        strategy = build_strategy(limits=self.limits())
        self._quote(strategy, seconds=2, m_bid="0.73000", m_ask="0.73040")
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            strategy._status()
        line = out.getvalue()
        self.assertIn("spread L=", line)
        self.assertIn("vol=", line)
        self.assertIn("gate=closed", line)
        summary = strategy.summary()
        self.assertIn("gate=closed", summary)
        self.assertIn("gate_closed_spread=100.0%", summary)
        self.assertIn("gate_closed_vol=0.0%", summary)
        self.assertIn("gate_closed_any=100.0%", summary)


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
        strategy.on_trade(FakeTrade(MAKER_ID, "0.73220", "30", "BUY"))
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
        strategy.on_trade(FakeTrade(MAKER_ID, "0.73220", "30", "BUY"))
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
        feed_books(strategy, m_bid="0.72830", m_ask="0.73230")
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

    def test_the_status_line_names_the_active_fill_budget(self) -> None:
        """The operator must be able to see which budget is in force without reading the code."""
        strategy = build_strategy()
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()

        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            strategy._status()
        base = out.getvalue()
        self.assertIn("/300f", base)
        self.assertNotIn("UNLOCKED", base)
        self.assertIn("day_fills 0/300 (base)", base)

        # Now earn the extended budget and re-check.
        strategy._state.realized_net_usd = 2.0
        strategy._decide()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            strategy._status()
        unlocked = out.getvalue()
        self.assertIn("/1500f UNLOCKED", unlocked)
        self.assertIn("(unlocked)", unlocked)

    def test_the_unlock_is_persisted_as_soon_as_it_flips(self) -> None:
        """A crash between the flip and the next fill must not lose the earned budget."""
        out_dir = Path(tempfile.mkdtemp(prefix="unlock-"))
        strategy = build_strategy(out=out_dir)
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        # Nothing earned and nothing filled yet, so there is nothing to persist.
        self.assertFalse((out_dir / "state.json").exists())

        strategy._state.realized_net_usd = 2.0
        strategy._decide()
        payload = json.loads((out_dir / "state.json").read_text(encoding="utf-8"))
        self.assertTrue(payload["profit_unlocked"])
        self.assertAlmostEqual(payload["realized_net_usd"], 2.0)

    def test_the_extended_budget_lets_a_long_session_keep_quoting(self) -> None:
        """Past the base cap but profitable: the run continues instead of being killed."""
        strategy = build_strategy()
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        strategy._state.realized_net_usd = 2.0
        strategy._state.fills = 400  # well past the 300 base cap
        strategy.clock.advance(1.0)
        strategy._decide()
        self.assertIsNone(strategy.kill_reason)
        self.assertTrue(strategy._kill.fill_cap_unlocked)

        # Give the profit back: the same fill count now stops the run.
        strategy._state.realized_net_usd = 0.0
        strategy.clock.advance(1.0)
        strategy._decide()
        self.assertIn("day_fills", strategy.kill_reason or "")

    def test_daily_state_is_persisted_as_the_run_goes(self) -> None:
        out = Path(tempfile.mkdtemp(prefix="persist-"))
        strategy = build_strategy(out=out)
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        strategy.on_trade(FakeTrade(MAKER_ID, "0.73220", "30", "BUY"))
        payload = json.loads((out / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(payload["fills"], 1)
        self.assertGreater(payload["tx"], 0)


# --------------------------------------------------------------------------- position side


class TestPositionSideDerivation(unittest.TestCase):
    """The sign must come from `Position.side`, never from `signed_qty`.

    On 2026-09-09 reconciliation handed the flattener a SHORT Lighter position whose
    `signed_qty` read +53.5 and a LONG Aster position whose `signed_qty` read -52.  It sold
    the short and bought the long.  These tests pin the rule that made that impossible.
    """

    def test_a_short_with_a_positive_signed_qty_is_still_short(self) -> None:
        pos = FakePosition(MAKER_ID, -53.5, 0.74719)
        pos.signed_qty = +53.5  # exactly the inverted value reconciliation produced
        self.assertEqual(maker_live.position_side_name(pos), maker_live.SHORT)
        self.assertAlmostEqual(maker_live.signed_from_side(pos), -53.5)
        self.assertFalse(maker_live.closing_side_is_sell(maker_live.SHORT), "a short is BOUGHT")

    def test_a_long_with_a_negative_signed_qty_is_still_long(self) -> None:
        pos = FakePosition(HEDGE_ID, 52.0, 0.74835)
        pos.signed_qty = -52.0
        self.assertEqual(maker_live.position_side_name(pos), maker_live.LONG)
        self.assertAlmostEqual(maker_live.signed_from_side(pos), 52.0)
        self.assertTrue(maker_live.closing_side_is_sell(maker_live.LONG), "a long is SOLD")

    def test_the_enum_repr_form_is_understood(self) -> None:
        pos = FakePosition(MAKER_ID, -1.0, 1.0)
        pos.side = "PositionSide.SHORT"
        self.assertEqual(maker_live.position_side_name(pos), maker_live.SHORT)

    def test_is_long_is_short_are_used_when_side_is_absent(self) -> None:
        class Bare:
            quantity = 7.0
            is_long = False
            is_short = True

        self.assertEqual(maker_live.position_side_name(Bare()), maker_live.SHORT)
        self.assertAlmostEqual(maker_live.signed_from_side(Bare()), -7.0)

    def test_a_short_position_produces_a_buy_clip(self) -> None:
        """The end-to-end rule, stated once: SHORT in, BUY out."""
        pos = FakePosition(MAKER_ID, -53.5, 0.74719)
        pos.signed_qty = +53.5
        side = maker_live.position_side_name(pos)
        clip = maker_live.plan_flatten_clip(
            side=side, qty=maker_live.position_quantity(pos),
            bid=0.74700, ask=0.74800, slippage_bps=20.0,
            size_increment=0.1, min_qty=20.0, min_notional=10.0, max_notional_usd=47.5,
        )
        self.assertIsNotNone(clip)
        self.assertFalse(clip.sell)
        self.assertAlmostEqual(clip.qty, 53.5)


class TestLighterPublicCrossCheck(unittest.TestCase):
    """The second opinion that would have caught the inversion before any order went out."""

    def payload(self, position: str, sign: int) -> dict:
        return {"accounts": [{"positions": [
            {"symbol": "PONS", "position": position, "sign": sign,
             "avg_entry_price": "0.74719"},
            {"symbol": "BTC", "position": "1.0", "sign": 1, "avg_entry_price": "60000"},
        ]}]}

    def test_a_negative_sign_reads_as_short(self) -> None:
        venue = maker_live.parse_lighter_public_account(self.payload("53.5", -1), "PONS")
        self.assertEqual(venue.side, maker_live.SHORT)
        self.assertAlmostEqual(venue.qty, 53.5)
        self.assertAlmostEqual(venue.signed, -53.5)

    def test_a_positive_sign_reads_as_long(self) -> None:
        venue = maker_live.parse_lighter_public_account(self.payload("52", 1), "PONS")
        self.assertEqual(venue.side, maker_live.LONG)
        self.assertAlmostEqual(venue.signed, 52.0)

    def test_a_zero_size_reads_as_flat(self) -> None:
        venue = maker_live.parse_lighter_public_account(self.payload("0", -1), "PONS")
        self.assertEqual(venue.side, maker_live.FLAT)

    def test_an_absent_symbol_reads_as_flat(self) -> None:
        venue = maker_live.parse_lighter_public_account({"accounts": []}, "PONS")
        self.assertEqual(venue.side, maker_live.FLAT)

    def test_agreement_and_disagreement(self) -> None:
        venue = maker_live.VenuePosition(maker_live.SHORT, 53.5)
        agree, detail = maker_live.positions_agree(maker_live.SHORT, 53.5, venue, 0.1)
        self.assertTrue(agree, detail)

        agree, detail = maker_live.positions_agree(maker_live.LONG, 53.5, venue, 0.1)
        self.assertFalse(agree, "an inverted side must never pass")
        self.assertIn("side mismatch", detail)

        agree, detail = maker_live.positions_agree(maker_live.SHORT, 40.0, venue, 0.1)
        self.assertFalse(agree)
        self.assertIn("size mismatch", detail)

    def test_a_size_difference_within_one_step_is_tolerated(self) -> None:
        venue = maker_live.VenuePosition(maker_live.SHORT, 53.5)
        agree, _ = maker_live.positions_agree(maker_live.SHORT, 53.45, venue, 0.1)
        self.assertTrue(agree)


# --------------------------------------------------------------------------- flatten


class TestFlattenPlanner(unittest.TestCase):
    """`plan_flatten_clip`: the arithmetic that closes a position the strategy does not own."""

    LIGHTER = dict(size_increment=0.1, min_qty=20.0, min_notional=10.0)
    ASTER = dict(size_increment=1.0, min_qty=1.0, min_notional=5.0)

    def test_a_short_is_closed_by_buying_at_the_ask_plus_slippage(self) -> None:
        clip = maker_live.plan_flatten_clip(
            side=maker_live.SHORT, qty=53.5, bid=0.74700, ask=0.74800, slippage_bps=20.0,
            max_notional_usd=47.5, **self.LIGHTER,
        )
        self.assertIsNotNone(clip)
        self.assertFalse(clip.sell, "a short is closed by buying")
        self.assertAlmostEqual(clip.qty, 53.5)
        self.assertAlmostEqual(clip.price, 0.74800 * (1 + 20.0 / 1e4), places=9)
        self.assertAlmostEqual(clip.remaining_after, 0.0)

    def test_a_long_is_closed_by_selling_at_the_bid_less_slippage(self) -> None:
        clip = maker_live.plan_flatten_clip(
            side=maker_live.LONG, qty=52.0, bid=0.74800, ask=0.74900, slippage_bps=20.0,
            max_notional_usd=47.5, **self.ASTER,
        )
        self.assertIsNotNone(clip)
        self.assertTrue(clip.sell, "a long is closed by selling")
        self.assertAlmostEqual(clip.qty, 52.0)
        self.assertAlmostEqual(clip.price, 0.74800 * (1 - 20.0 / 1e4), places=9)

    def test_a_flat_book_plans_nothing(self) -> None:
        for side, qty in ((maker_live.FLAT, 10.0), (maker_live.SHORT, 0.0),
                          (maker_live.LONG, 0.05)):
            with self.subTest(side=side, qty=qty):
                self.assertIsNone(maker_live.plan_flatten_clip(
                    side=side, qty=qty, bid=0.747, ask=0.748, slippage_bps=20.0,
                    max_notional_usd=47.5, **self.LIGHTER,
                ))

    def test_a_position_worth_more_than_one_clip_is_split(self) -> None:
        clip = maker_live.plan_flatten_clip(
            side=maker_live.SHORT, qty=200.0, bid=0.74700, ask=0.74800, slippage_bps=20.0,
            max_notional_usd=47.5, **self.LIGHTER,
        )
        self.assertIsNotNone(clip)
        self.assertLessEqual(clip.qty * clip.price, 47.5 + 1e-9)
        self.assertAlmostEqual(clip.qty + clip.remaining_after, 200.0, places=6)

    def test_a_split_never_strands_a_remainder_below_the_venue_minimum(self) -> None:
        price = 0.74800 * (1 + 20.0 / 1e4)
        max_qty = 47.5 / price
        clip = maker_live.plan_flatten_clip(
            side=maker_live.SHORT, qty=max_qty + 5.0, bid=0.74700, ask=0.74800,
            slippage_bps=20.0, max_notional_usd=47.5, **self.LIGHTER,
        )
        self.assertIsNotNone(clip)
        self.assertGreaterEqual(clip.remaining_after, self.LIGHTER["min_qty"] - 1e-6)

    def test_a_residual_under_the_venue_minimum_is_still_sent_as_a_close(self) -> None:
        # 2026-09-09: both venues accepted below-minimum CLOSE orders (the user closed 2.3 PONS
        # on Lighter and 3 PONS on Aster by hand), so the exact remainder goes out.
        clip = maker_live.plan_flatten_clip(
            side=maker_live.SHORT, qty=5.0, bid=0.74700, ask=0.74800, slippage_bps=20.0,
            max_notional_usd=47.5, **self.LIGHTER,
        )
        self.assertIsNotNone(clip)
        self.assertFalse(clip.sell)
        self.assertAlmostEqual(clip.qty, 5.0)
        self.assertAlmostEqual(clip.remaining_after, 0.0)

    def test_no_touch_plans_nothing(self) -> None:
        self.assertIsNone(maker_live.plan_flatten_clip(
            side=maker_live.SHORT, qty=53.5, bid=0.0, ask=0.0, slippage_bps=20.0,
            max_notional_usd=47.5, **self.LIGHTER,
        ))

    def test_the_clip_is_rounded_down_to_the_venue_step(self) -> None:
        clip = maker_live.plan_flatten_clip(
            side=maker_live.LONG, qty=52.7, bid=0.74800, ask=0.74900, slippage_bps=20.0,
            max_notional_usd=47.5, **self.ASTER,
        )
        self.assertAlmostEqual(clip.qty, 52.0, places=9)


class FlattenUnderTest(maker_live.PositionFlattener):
    """The production flattener over stub surfaces."""

    def __new__(cls, config, *_args: object, **_kwargs: object):
        return super().__new__(cls, config)

    def __init__(self, config, cache, factory, clock) -> None:
        super().__init__(config)
        self._stub_cache = cache
        self._stub_factory = factory
        self._stub_clock = clock
        self.submitted: list = []
        self.subscriptions: list = []

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
        self.subscriptions.append(instrument_id)

    def submit_order(self, order, position_id=None, client_id=None, params=None) -> None:
        self.submitted.append(order)
        self._stub_cache.orders[order.client_order_id] = order


VENUE_SHORT_53_5 = maker_live.VenuePosition(maker_live.SHORT, 53.5, 0.74719, "test")


def build_flattener(
    positions: list,
    *,
    clock: FakeClock | None = None,
    venue: object = VENUE_SHORT_53_5,
    skip_cross_check: bool = False,
) -> FlattenUnderTest:
    """A started flattener whose cache reports ``positions`` and whose cross-check is stubbed.

    ``venue`` is what the Lighter public endpoint is made to answer; ``None`` simulates it
    being unreachable.
    """
    clock = clock if clock is not None else FakeClock()
    config = maker_live.FlattenConfig(
        strategy_id=StrategyId.from_str("FLATTEN-TEST-001"),
        plan=maker_live.SYMBOLS["mainnet"]["PONS"],
        limits=load_limits(REPO / "config" / "limits.toml"),
        env="mainnet",
        maker_client_id="LIGHTER",
        hedge_client_id="ASTER",
        deadline_ts=0.0,
        hedge_enabled=True,
        account_index="12345",
        skip_cross_check=skip_cross_check,
    )
    cache = FakeCache(instruments=(lighter_pons(), aster_pons()))
    cache.positions.extend(positions)
    strategy = FlattenUnderTest(config, cache, FakeOrderFactory(), clock)
    strategy.on_start()
    strategy.on_quote(FakeQuote(MAKER_ID, "0.74700", "0.74800"))
    strategy.on_quote(FakeQuote(HEDGE_ID, "0.74800", "0.74900"))
    strategy.clock.advance(maker_live.FLATTEN_CALIBRATE_SECS + 1.0)
    strategy._venue_answer = venue
    return strategy


@contextlib.contextmanager
def stub_public_endpoint(strategy: FlattenUnderTest):
    """Answer the Lighter public cross-check with whatever the test set on the strategy."""
    with mock.patch.object(
        maker_live, "fetch_lighter_public_position",
        side_effect=lambda *a, **k: strategy._venue_answer,
    ):
        yield


class TestPositionFlattener(unittest.TestCase):
    """Closing the exact book of 2026-09-09, and refusing when the sources disagree."""

    def positions(self) -> list:
        """The venue truth: Lighter SHORT 53.5, Aster LONG 52."""
        return [
            FakePosition(MAKER_ID, -53.5, 0.74719),
            FakePosition(HEDGE_ID, 52.0, 0.74835),
        ]

    def inverted_positions(self) -> list:
        """The same book as reconciliation mis-reported it: signs flipped, sides correct."""
        maker = FakePosition(MAKER_ID, -53.5, 0.74719)
        maker.signed_qty = +53.5
        hedge = FakePosition(HEDGE_ID, 52.0, 0.74835)
        hedge.signed_qty = -52.0
        return [maker, hedge]

    def sweep(self, flat: FlattenUnderTest) -> None:
        with stub_public_endpoint(flat):
            flat._sweep()

    def test_it_closes_a_short_by_buying_and_a_long_by_selling(self) -> None:
        flat = build_flattener(self.positions())
        self.sweep(flat)
        self.assertEqual(len(flat.submitted), 2)
        by_instrument = {c["instrument_id"]: c for c in flat.order_factory.calls}

        maker = by_instrument[MAKER_ID]
        self.assertEqual(maker["order_side"], OrderSide.BUY, "closing a SHORT buys")
        self.assertEqual(maker["time_in_force"], maker_live.TimeInForce.IOC)
        self.assertNotIn("reduce_only", maker, "reduce-only is no longer used")
        self.assertAlmostEqual(float(maker["quantity"].as_decimal()), 53.5)

        hedge = by_instrument[HEDGE_ID]
        self.assertEqual(hedge["order_side"], OrderSide.SELL, "closing a LONG sells")
        self.assertAlmostEqual(float(hedge["quantity"].as_decimal()), 52.0)

    def test_an_inverted_signed_qty_does_not_change_the_sides(self) -> None:
        """The regression: the same book with reconciliation's inverted signs must trade the
        same way, because only `side` is read."""
        flat = build_flattener(self.inverted_positions())
        self.sweep(flat)
        by_instrument = {c["instrument_id"]: c for c in flat.order_factory.calls}
        self.assertEqual(by_instrument[MAKER_ID]["order_side"], OrderSide.BUY)
        self.assertEqual(by_instrument[HEDGE_ID]["order_side"], OrderSide.SELL)

    def test_a_side_disagreement_refuses_before_any_order(self) -> None:
        """Cache says SHORT, the venue's own API says LONG: send nothing."""
        flat = build_flattener(
            self.positions(), venue=maker_live.VenuePosition(maker_live.LONG, 53.5),
        )
        self.sweep(flat)
        self.assertEqual(flat.submitted, [], "nothing may be sent on a disagreement")
        self.assertIsNotNone(flat.refused)
        self.assertIn("side mismatch", flat.refused)
        self.assertEqual(flat.exit_code, maker_live.EXIT_NOT_FLAT)
        self.assertTrue(flat.done_event.is_set())

    def test_a_size_disagreement_refuses(self) -> None:
        flat = build_flattener(
            self.positions(), venue=maker_live.VenuePosition(maker_live.SHORT, 10.0),
        )
        self.sweep(flat)
        self.assertEqual(flat.submitted, [])
        self.assertIn("size mismatch", flat.refused or "")

    def test_an_unreachable_endpoint_refuses_unless_overridden(self) -> None:
        flat = build_flattener(self.positions(), venue=None)
        self.sweep(flat)
        self.assertEqual(flat.submitted, [])
        self.assertIn("could not reach", flat.refused or "")

        allowed = build_flattener(self.positions(), venue=None, skip_cross_check=True)
        self.sweep(allowed)
        self.assertIsNone(allowed.refused)
        self.assertEqual(len(allowed.submitted), 2)

    def test_own_fills_are_the_truth_for_what_is_left(self) -> None:
        """The cache never updating must not cause a second clip."""
        flat = build_flattener(self.positions())
        self.sweep(flat)
        maker_order = next(o for o in flat.submitted if o.instrument_id == MAKER_ID)
        flat.on_order_filled(
            FakeFill(maker_order.client_order_id, MAKER_ID, OrderSide.BUY, "53.5", "0.74950"),
        )
        self.assertAlmostEqual(flat._remaining_signed(MAKER_ID), 0.0)
        self.assertEqual(flat._remaining_side(MAKER_ID), maker_live.FLAT)

        # The cache still reports the old position; the flattener must ignore it.
        sent = len(flat.submitted)
        flat._clear_inflight(
            FakeFill(maker_order.client_order_id, MAKER_ID, OrderSide.BUY, "0", "0"), "done",
        )
        self.sweep(flat)
        maker_clips = [o for o in flat.submitted if o.instrument_id == MAKER_ID]
        self.assertEqual(len(maker_clips), 1, "the maker leg is done; no second clip")
        self.assertLessEqual(len(flat.submitted), sent + 1)  # the hedge leg may still work

    def test_it_stops_and_exits_zero_once_both_legs_are_filled(self) -> None:
        flat = build_flattener(self.positions())
        self.sweep(flat)
        for order in list(flat.submitted):
            side = OrderSide.BUY if order.instrument_id == MAKER_ID else OrderSide.SELL
            qty = "53.5" if order.instrument_id == MAKER_ID else "52"
            flat.on_order_filled(
                FakeFill(order.client_order_id, order.instrument_id, side, qty, "0.749"),
            )
            flat._clear_inflight(
                FakeFill(order.client_order_id, order.instrument_id, side, "0", "0"), "done",
            )
        self.sweep(flat)
        self.assertTrue(flat.flat)
        self.assertTrue(flat.done_event.is_set())
        self.assertEqual(flat.exit_code, maker_live.EXIT_OK)
        self.assertIn("flat=True", flat.summary_line)

    def test_a_partial_fill_leaves_the_remainder_to_the_next_clip(self) -> None:
        flat = build_flattener(self.positions())
        self.sweep(flat)
        maker_order = next(o for o in flat.submitted if o.instrument_id == MAKER_ID)
        flat.on_order_filled(
            FakeFill(maker_order.client_order_id, MAKER_ID, OrderSide.BUY, "20", "0.74950"),
        )
        self.assertAlmostEqual(flat._remaining_signed(MAKER_ID), -33.5)
        self.assertEqual(flat._remaining_side(MAKER_ID), maker_live.SHORT)

    def test_one_clip_per_leg_at_a_time(self) -> None:
        flat = build_flattener(self.positions())
        self.sweep(flat)
        self.sweep(flat)
        self.assertEqual(len(flat.submitted), 2)

    def test_a_silent_clip_triggers_a_re_query_not_another_order(self) -> None:
        """The 5 s timeout must re-read the venue, not fire a second clip blindly."""
        flat = build_flattener(self.positions())
        self.sweep(flat)
        self.assertEqual(len(flat.submitted), 2)
        flat.clock.advance(maker_live.HEDGE_INFLIGHT_TIMEOUT_S + 1.0)
        self.sweep(flat)
        self.assertEqual(len(flat.submitted), 2, "a timeout re-queries before sending again")
        self.assertTrue(all(b.inflight is None for b in flat._books.values()))

    def test_a_flat_book_finishes_immediately_without_ordering(self) -> None:
        flat = build_flattener([], venue=maker_live.VenuePosition(maker_live.FLAT, 0.0))
        self.sweep(flat)
        self.assertEqual(flat.submitted, [])
        self.assertTrue(flat.done_event.is_set())
        self.assertEqual(flat.exit_code, maker_live.EXIT_OK)

    def test_a_book_that_never_closes_exits_non_zero(self) -> None:
        flat = build_flattener(self.positions())
        self.sweep(flat)
        flat._cfg.deadline_ts = flat.clock.timestamp_ns() / 1e9 - 1.0
        self.sweep(flat)
        self.assertTrue(flat.done_event.is_set())
        self.assertFalse(flat.flat)
        self.assertEqual(flat.exit_code, maker_live.EXIT_NOT_FLAT)

    def test_a_residual_below_the_venue_minimum_is_closed_with_an_exact_clip(self) -> None:
        flat = build_flattener(
            [FakePosition(MAKER_ID, -5.0, 0.74719)],
            venue=maker_live.VenuePosition(maker_live.SHORT, 5.0),
        )
        self.sweep(flat)
        self.assertEqual(len(flat.submitted), 1, "the exact remainder is sent as a close order")
        self.assertAlmostEqual(float(flat.submitted[0].quantity), 5.0)

    def test_the_summary_records_the_cross_checks(self) -> None:
        flat = build_flattener(self.positions())
        self.sweep(flat)
        summary = flat.summary()
        self.assertIn("cross_checks=", summary)
        self.assertIn("cache and venue agree", summary)
        self.assertIn("only one source available", summary, "the Aster leg says it is unconfirmed")


# --------------------------------------------------------------------------- start guard


class TestStartGuard(unittest.TestCase):
    """A live session must not quote onto a book it does not own."""

    def positions(self) -> list:
        return [
            FakePosition(MAKER_ID, -53.5, 0.74719),
            FakePosition(HEDGE_ID, 52.0, 0.74835),
        ]

    def run_to_guard(self, strategy: MakerUnderTest) -> None:
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()

    def test_a_flat_book_starts_normally(self) -> None:
        strategy = build_strategy(mode="live")
        self.run_to_guard(strategy)
        self.assertIsNone(strategy._not_flat)
        self.assertTrue(strategy.submitted, "a flat book quotes as usual")

    def test_a_position_refuses_the_session(self) -> None:
        strategy = build_strategy(mode="live", positions=self.positions())
        self.run_to_guard(strategy)
        self.assertIsNotNone(strategy._not_flat)
        self.assertEqual(strategy.submitted, [], "no order may be sent")
        self.assertEqual(strategy.exit_code, maker_live.EXIT_NOT_FLAT)
        self.assertTrue(strategy.done_event.is_set())

    def test_a_hedge_only_position_also_refuses(self) -> None:
        strategy = build_strategy(mode="live", positions=[FakePosition(HEDGE_ID, 52.0, 0.748)])
        self.run_to_guard(strategy)
        self.assertEqual(strategy.exit_code, maker_live.EXIT_NOT_FLAT)

    def test_a_foreign_open_order_refuses(self) -> None:
        strategy = build_strategy(mode="live")
        strategy.cache.orders[ClientOrderId("O-OTHER")] = FakeOrder(
            ClientOrderId("O-OTHER"), Quantity.from_str("20"), Price.from_str("0.74"),
            OrderSide.SELL, instrument_id=MAKER_ID, strategy_id="SOMEONE-ELSE",
        )
        self.run_to_guard(strategy)
        self.assertIsNotNone(strategy._not_flat)
        self.assertEqual(strategy.exit_code, maker_live.EXIT_NOT_FLAT)

    def test_adopt_position_takes_the_book_over(self) -> None:
        """q from the maker leg's SIDE, the unhedged delta from maker + hedge."""
        strategy = build_strategy(
            mode="live", positions=self.positions(), adopt_position=True,
        )
        with mock.patch.object(
            maker_live, "fetch_lighter_public_position",
            return_value=maker_live.VenuePosition(maker_live.SHORT, 53.5, 0.74719),
        ):
            self.run_to_guard(strategy)
        self.assertIsNone(strategy._not_flat)
        self.assertTrue(strategy._adopted)
        self.assertAlmostEqual(strategy._q, -53.5)
        self.assertAlmostEqual(strategy._hedger.delta, -1.5)
        self.assertIn("ADOPTED", strategy.summary())

    def test_adoption_ignores_an_inverted_signed_qty(self) -> None:
        """The regression: reconciliation's inverted signs must not reach `q`."""
        maker = FakePosition(MAKER_ID, -53.5, 0.74719)
        maker.signed_qty = +53.5
        hedge = FakePosition(HEDGE_ID, 52.0, 0.74835)
        hedge.signed_qty = -52.0
        strategy = build_strategy(
            mode="live", positions=[maker, hedge], adopt_position=True,
        )
        with mock.patch.object(
            maker_live, "fetch_lighter_public_position",
            return_value=maker_live.VenuePosition(maker_live.SHORT, 53.5, 0.74719),
        ):
            self.run_to_guard(strategy)
        self.assertAlmostEqual(strategy._q, -53.5, msg="q must follow the SIDE, not signed_qty")
        self.assertAlmostEqual(strategy._hedger.delta, -1.5)

    def test_adoption_refuses_when_the_venue_disagrees(self) -> None:
        strategy = build_strategy(
            mode="live", positions=self.positions(), adopt_position=True,
        )
        with mock.patch.object(
            maker_live, "fetch_lighter_public_position",
            return_value=maker_live.VenuePosition(maker_live.LONG, 53.5),
        ):
            self.run_to_guard(strategy)
        self.assertIsNotNone(strategy._not_flat)
        self.assertIn("side mismatch", strategy._not_flat)
        self.assertFalse(strategy._adopted)
        self.assertEqual(strategy.submitted, [], "no order may be sent")
        self.assertEqual(strategy.exit_code, maker_live.EXIT_NOT_FLAT)

    def test_adoption_refuses_when_the_cross_check_is_unreachable(self) -> None:
        strategy = build_strategy(
            mode="live", positions=self.positions(), adopt_position=True,
        )
        with mock.patch.object(
            maker_live, "fetch_lighter_public_position", return_value=None,
        ):
            self.run_to_guard(strategy)
        self.assertIn("unreachable", strategy._not_flat or "")
        self.assertFalse(strategy._adopted)

    def test_skip_cross_check_allows_adoption_without_a_second_source(self) -> None:
        strategy = build_strategy(
            mode="live", positions=self.positions(), adopt_position=True,
            skip_cross_check=True,
        )
        with mock.patch.object(
            maker_live, "fetch_lighter_public_position", return_value=None,
        ):
            self.run_to_guard(strategy)
        self.assertIsNone(strategy._not_flat)
        self.assertTrue(strategy._adopted)
        self.assertAlmostEqual(strategy._q, -53.5)

    def test_adoption_seeds_the_books_so_the_exposure_cap_sees_the_position(self) -> None:
        strategy = build_strategy(
            mode="live", positions=self.positions(), adopt_position=True,
        )
        with mock.patch.object(
            maker_live, "fetch_lighter_public_position",
            return_value=maker_live.VenuePosition(maker_live.SHORT, 53.5, 0.74719),
        ):
            self.run_to_guard(strategy)
        self.assertAlmostEqual(strategy._pnl.m.pos, -53.5)
        self.assertAlmostEqual(strategy._pnl.h.pos, 52.0)
        self.assertGreater(strategy._pnl.exposure_usd(0.747, 0.748), 70.0)

    def test_the_guard_only_cares_about_flat_or_not(self) -> None:
        """The guard must refuse on a position whichever way round the cache has it."""
        for signed in (+53.5, -53.5):
            with self.subTest(signed_qty=signed):
                pos = FakePosition(MAKER_ID, -53.5, 0.74719)
                pos.signed_qty = signed
                strategy = build_strategy(mode="live", positions=[pos])
                self.run_to_guard(strategy)
                self.assertIsNotNone(strategy._not_flat)
                self.assertEqual(strategy.exit_code, maker_live.EXIT_NOT_FLAT)

    def test_the_guard_runs_only_once(self) -> None:
        strategy = build_strategy(mode="live")
        self.run_to_guard(strategy)
        self.assertTrue(strategy._flat_checked)
        placed = len(strategy.submitted)
        strategy.clock.advance(1.0)
        strategy._decide()
        self.assertGreaterEqual(len(strategy.submitted), placed)

    def test_paper_mode_never_runs_the_guard(self) -> None:
        strategy = build_strategy(mode="paper", positions=self.positions())
        self.run_to_guard(strategy)
        self.assertIsNone(strategy._not_flat)


# --------------------------------------------------------------------------- stale refs


class TestStaleOrderReferences(unittest.TestCase):
    """A Quote pointing at an order that has already ended must never be modified.

    Session B (2026-09-09) logged `Cannot create command ModifyOrder: state is Filled` every
    three seconds for its whole life: the engine held a Quote whose order had filled, so the
    ask side re-priced a dead order forever and never placed again.
    """

    def armed(self) -> MakerUnderTest:
        strategy = build_strategy(mode="live")
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        self.assertTrue(strategy.submitted)
        return strategy

    def test_a_filled_order_is_dropped_instead_of_modified(self) -> None:
        strategy = self.armed()
        resting = strategy.submitted[0]
        placed = len(strategy.submitted)
        resting.close("FILLED")  # the venue filled it; no event reached us

        strategy.clock.advance(strategy._limits.quote.requote_min_s + 1.0)
        feed_books(strategy, m_bid="0.72830", m_ask="0.73230")
        strategy._decide()
        # The OTHER side is alive and its touch moved, so a modify for it is expected; what
        # must never be sent is one addressed to the order the venue has already filled.
        self.assertNotIn(
            resting.client_order_id, [cid for cid, _, _ in strategy.modified],
            "a dead order must not be re-priced",
        )
        self.assertEqual(strategy._stale_refs_dropped, 1)

        # And the next tick places a fresh order on that side.
        strategy.clock.advance(1.0)
        strategy._decide()
        self.assertGreater(len(strategy.submitted), placed)

    def test_a_cancel_for_a_closed_order_is_not_sent(self) -> None:
        strategy = self.armed()
        resting = strategy.submitted[0]
        resting.close("CANCELED")
        cancels = len(strategy.cancelled)
        # Close the gate so the engine wants to cancel the resting quote.
        feed_books(strategy, h_bid="0.99000", h_ask="0.99100")
        strategy.clock.advance(1.0)
        strategy._decide()
        self.assertEqual(len(strategy.cancelled), cancels)
        self.assertGreaterEqual(strategy._stale_refs_dropped, 1)

    def test_a_fill_to_the_full_quantity_drops_the_quote(self) -> None:
        strategy = self.armed()
        resting = strategy.submitted[0]
        side = strategy._live_orders[resting.client_order_id]
        resting.filled_qty = resting.quantity
        strategy.on_order_filled(
            FakeFill(resting.client_order_id, MAKER_ID, resting.order_side,
                     str(resting.quantity), "0.73030"),
        )
        self.assertIsNone(strategy._engine.orders[side])
        self.assertNotIn(resting.client_order_id, strategy._live_orders)

    def test_the_counter_reaches_the_status_line(self) -> None:
        strategy = self.armed()
        strategy.submitted[0].close("FILLED")
        strategy.clock.advance(strategy._limits.quote.requote_min_s + 1.0)
        feed_books(strategy, m_bid="0.72830", m_ask="0.73230")
        strategy._decide()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            strategy._status()
        self.assertIn("stale 1", out.getvalue())


class TestClosedOrderCleanup(unittest.TestCase):
    """A cancel refused because the order already ended is not a leftover."""

    def armed(self) -> MakerUnderTest:
        strategy = build_strategy(mode="live")
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._decide()
        return strategy

    def test_a_refused_cancel_on_a_filled_order_is_not_a_leftover(self) -> None:
        strategy = self.armed()
        resting = strategy.submitted[0]

        def refuse(client_order_id, client_id=None, params=None):
            raise RuntimeError("Cannot cancel order: state is Filled, Limit(...)")

        strategy.cancel_order = refuse  # type: ignore[method-assign]
        # The cache still shows it open, so only the refusal text can retire it.
        strategy.on_stop()
        self.assertEqual(strategy.leftovers, [])
        self.assertEqual(strategy.cancel_errors, [])
        self.assertEqual(strategy.exit_code, maker_live.EXIT_OK)
        self.assertIn(resting.client_order_id, strategy._known_closed)

    def test_a_genuine_pending_cancel_is_still_a_leftover(self) -> None:
        strategy = self.armed()
        strategy.submitted[0].status = FakeStatus("PENDING_CANCEL")
        strategy.cancel_order = lambda *a, **k: None  # type: ignore[method-assign]
        strategy.on_stop()
        self.assertTrue(strategy.leftovers)
        self.assertEqual(strategy.exit_code, maker_live.EXIT_FAILED)

    def test_an_unrelated_cancel_failure_is_still_a_failure(self) -> None:
        strategy = self.armed()

        def refuse(client_order_id, client_id=None, params=None):
            raise RuntimeError("connection reset by peer")

        strategy.cancel_order = refuse  # type: ignore[method-assign]
        strategy.on_stop()
        self.assertTrue(strategy.cancel_errors)
        self.assertEqual(strategy.exit_code, maker_live.EXIT_FAILED)


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




class FakeCancel:
    """Stand-in for an OrderCanceled / OrderExpired event."""

    def __init__(self, client_order_id, instrument_id) -> None:
        self.client_order_id = client_order_id
        self.instrument_id = instrument_id
        self.ts_event = 0


class TestHedgeRetryAfterUnfilledIoc(unittest.TestCase):
    """2026-09-09 mainnet: an IOC hedge that cancelled unfilled blocked every later hedge for
    30 s until the hedge_fail kill fired, because the in-flight quantity was only released on
    fills. A cancel / expiry / rejection must release it, and a report that never arrives must
    time out."""

    def _live_with_delta(self):
        strategy = build_strategy(mode="live")
        feed_books(strategy)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        strategy._hedger.add(-30.0, strategy.clock.timestamp_ns() / 1e9)  # 30 base short on the maker
        strategy._hedger.waiting_since = None
        return strategy

    def test_an_unfilled_ioc_cancel_releases_the_hedge_and_the_next_tick_resends(self) -> None:
        strategy = self._live_with_delta()
        strategy.clock.advance(1.0)
        strategy._pump_hedge(strategy.clock.timestamp_ns() / 1e9)
        self.assertEqual(len(strategy.submitted), 1, "the first hedge IOC goes out")
        first = strategy.submitted[0]
        self.assertGreater(strategy._pending_hedge_qty, 0.0)
        strategy.clock.advance(1.0)
        strategy._pump_hedge(strategy.clock.timestamp_ns() / 1e9)
        self.assertEqual(len(strategy.submitted), 1, "while in flight nothing else is sent")
        strategy.on_order_canceled(FakeCancel(first.client_order_id, strategy._hedge_id))
        self.assertEqual(strategy._pending_hedge_qty, 0.0, "the cancel released the in-flight base")
        strategy.clock.advance(1.0)
        strategy._pump_hedge(strategy.clock.timestamp_ns() / 1e9)
        self.assertEqual(len(strategy.submitted), 2, "the delta is re-hedged on the next tick")
        self.assertIsNone(strategy.kill_reason)

    def test_a_partial_fill_then_cancel_releases_only_the_remainder(self) -> None:
        strategy = self._live_with_delta()
        strategy.clock.advance(1.0)
        strategy._pump_hedge(strategy.clock.timestamp_ns() / 1e9)
        first = strategy.submitted[0]
        sent = strategy._pending_hedge_qty
        strategy.on_order_filled(
            FakeFill(first.client_order_id, strategy._hedge_id, OrderSide.BUY, "10", "0.73040"),
        )
        self.assertAlmostEqual(strategy._pending_hedge_qty, sent - 10.0)
        strategy.on_order_canceled(FakeCancel(first.client_order_id, strategy._hedge_id))
        self.assertEqual(strategy._pending_hedge_qty, 0.0)
        self.assertAlmostEqual(strategy._hedger.delta, -20.0, places=6)

    def test_a_report_that_never_arrives_times_out(self) -> None:
        strategy = self._live_with_delta()
        strategy.clock.advance(1.0)
        strategy._pump_hedge(strategy.clock.timestamp_ns() / 1e9)
        self.assertEqual(len(strategy.submitted), 1)
        strategy.clock.advance(maker_live.HEDGE_INFLIGHT_TIMEOUT_S + 1.0)
        strategy._pump_hedge(strategy.clock.timestamp_ns() / 1e9)
        self.assertEqual(len(strategy.submitted), 2, "the stale in-flight hedge was released and resent")


# --------------------------------------------------------------------------- stop sequence


class FakeTimeEvent:
    def __init__(self, name: str) -> None:
        self.name = name


class TestThroughMinimumClip(unittest.TestCase):
    """A remainder the venue will not close exactly is closed by going through zero.

    Lighter denied a 0.37 VVV close locally on 2026-09-10 - 164 denials in one flatten run -
    because its minimum is 0.60.  The web UI closes it; the API path cannot.  Two legal orders
    do what one illegal one could not: sell 0.97 (leaving exactly 0.60 SHORT), then buy 0.60.
    """

    LIGHTER = dict(bid=27.0, ask=27.0, slippage_bps=20.0, size_increment=0.01,
                   min_qty=0.6, min_notional=0.0, max_notional_usd=47.5)
    ASTER = dict(bid=100.0, ask=100.0, slippage_bps=20.0, size_increment=0.001,
                 min_qty=0.0, min_notional=5.0, max_notional_usd=47.5)

    def test_the_exact_close_is_what_is_tried_first(self) -> None:
        clip = maker_live.plan_flatten_clip(side="LONG", qty=0.37, **self.LIGHTER)
        self.assertAlmostEqual(clip.qty, 0.37, places=9)
        self.assertFalse(clip.through_minimum)

    def test_a_lighter_like_minimum_is_crossed_in_two_legal_clips(self) -> None:
        first = maker_live.plan_flatten_clip(
            side="LONG", qty=0.37, through_minimum=True, **self.LIGHTER,
        )
        self.assertTrue(first.sell)
        self.assertAlmostEqual(first.qty, 0.97, places=9)  # 0.60 + 0.37
        self.assertTrue(first.through_minimum)
        self.assertGreaterEqual(first.qty, self.LIGHTER["min_qty"])
        self.assertAlmostEqual(first.remaining_after, 0.6, places=9)
        # ... and what it leaves is exactly the minimum, which closes normally.
        second = maker_live.plan_flatten_clip(side="SHORT", qty=0.6, **self.LIGHTER)
        self.assertFalse(second.sell)
        self.assertAlmostEqual(second.qty, 0.6, places=9)
        self.assertFalse(second.through_minimum)

    def test_an_aster_like_notional_minimum_is_crossed_too(self) -> None:
        clip = maker_live.plan_flatten_clip(
            side="SHORT", qty=0.021, through_minimum=True, **self.ASTER,
        )
        self.assertFalse(clip.sell, "a short is closed by buying")
        # 5 USD / 100.2 = 0.0499 -> 0.05 on the step, plus the 0.021 remainder.
        self.assertAlmostEqual(clip.qty, 0.071, places=9)
        self.assertGreater(clip.qty * clip.price, self.ASTER["min_notional"])
        self.assertAlmostEqual(clip.remaining_after, 0.05, places=9)

    def test_both_halves_stay_inside_the_hard_per_order_cap(self) -> None:
        for kwargs in (self.LIGHTER, self.ASTER):
            with self.subTest(venue=kwargs["min_qty"]):
                clip = maker_live.plan_flatten_clip(
                    side="LONG", qty=0.01, through_minimum=True, **kwargs,
                )
                self.assertLessEqual(clip.qty * clip.price,
                                     maker_live.CAP_ORDER_NOTIONAL_USD)

    def test_it_refuses_when_two_minimums_would_breach_the_cap(self) -> None:
        expensive = dict(self.LIGHTER, bid=200.0, ask=200.0, min_qty=1.0)
        self.assertIsNone(maker_live.plan_flatten_clip(
            side="LONG", qty=0.5, through_minimum=True, **expensive,
        ))

    def test_only_a_min_size_reason_arms_the_fallback(self) -> None:
        for reason in (
            "quantity 0.37 invalid (< minimum trade size of 0.6)",
            "MIN_QUANTITY: order size below the minimum",
            "min_notional 5 USD not met",
            "order too small",
        ):
            with self.subTest(reason=reason):
                self.assertTrue(maker_live.is_min_size_denial(reason))
        for reason in (
            "", "price band exceeded", "insufficient margin",
            "ReduceOnly Order is rejected", "notional exceeds the maximum",
            "Too Many Requests",
        ):
            with self.subTest(reason=reason):
                self.assertFalse(maker_live.is_min_size_denial(reason))


class TestStopSequence(unittest.TestCase):
    """cancel -> confirm -> flatten, the one path the deadline, the kill and SIGINT share."""

    def build(self, *, mode: str = "live", q: float = 0.0, hedge_base: float = 0.0,
              **limit_overrides):
        limits = test_limits(placement="improve", **limit_overrides)
        strategy = build_strategy(mode=mode, limits=limits)
        strategy.clock.advance(maker_live.STARTUP_GRACE_SECS + 1.0)
        feed_books(strategy)
        if q:
            # A maker position, and the hedge that mirrors it, as a real session would hold.
            strategy._q = q
            strategy._pnl.maker_fill(q < 0, abs(q), 0.73)
        if hedge_base:
            strategy._pnl.hedge_fill(hedge_base < 0, abs(hedge_base), 0.73)
        return strategy

    @staticmethod
    def tick(strategy, seconds: float = 1.0) -> None:
        strategy.clock.advance(seconds)
        feed_books(strategy)
        strategy.on_time_event(FakeTimeEvent("maker-decide"))

    @staticmethod
    def quote_once(strategy) -> None:
        strategy._decide()

    # -- phase 1 ---------------------------------------------------------------------------

    def test_the_deadline_starts_the_sequence_instead_of_exiting(self) -> None:
        strategy = self.build(q=-30.0, hedge_base=30.0)
        strategy._finish("deadline")
        self.assertEqual(strategy.stop_phase, maker_live.STOP_CANCEL)
        self.assertFalse(strategy.done_event.is_set(), "the run may not end before the flatten")

    def test_the_kill_switch_runs_the_same_sequence(self) -> None:
        strategy = self.build(q=-30.0, hedge_base=30.0)
        strategy._trigger_kill("max_loss_usd breached")
        self.assertEqual(strategy.stop_phase, maker_live.STOP_CANCEL)
        self.assertEqual(strategy.kill_reason, "max_loss_usd breached")

    def test_a_sigint_request_is_picked_up_on_the_next_tick(self) -> None:
        strategy = self.build(q=-30.0, hedge_base=30.0)
        strategy.request_stop("sigint")
        self.assertEqual(strategy.stop_phase, maker_live.STOP_IDLE, "not on the caller thread")
        self.tick(strategy)
        self.assertEqual(strategy._stop_reason, "sigint")
        self.assertNotEqual(strategy.stop_phase, maker_live.STOP_IDLE)

    def test_it_waits_for_the_venue_and_re_sends_the_cancel(self) -> None:
        strategy = self.build()
        self.quote_once(strategy)
        self.assertTrue(strategy.submitted)
        strategy.cancel_is_silent = True  # the venue never confirms
        strategy._finish("deadline")
        self.assertEqual(strategy.stop_phase, maker_live.STOP_CANCEL)

        self.tick(strategy, 4.0)
        self.assertEqual(strategy.stop_phase, maker_live.STOP_CANCEL, "still waiting")
        self.assertEqual(strategy._cancel_resends, 0, "under the re-send interval")
        self.tick(strategy, 2.0)  # past CANCEL_RESEND_S
        self.assertGreater(strategy._cancel_resends, 0)

        # The venue finally confirms: the wait ends and the sequence moves on.
        for order in strategy.submitted:
            order.close("CANCELED")
        self.tick(strategy)
        self.assertNotEqual(strategy.stop_phase, maker_live.STOP_CANCEL)
        self.assertIsNotNone(strategy._cancel_confirm_s)

    def test_a_fill_during_the_wait_is_booked(self) -> None:
        """The 2026-09-10 session lost one of these: the order filled after we had gone."""
        strategy = self.build()
        self.quote_once(strategy)
        resting = strategy.submitted[0]
        strategy.cancel_is_silent = True
        strategy._finish("deadline")
        self.assertEqual(strategy.stop_phase, maker_live.STOP_CANCEL)

        before_q, before_fills = strategy._q, strategy._state.fills
        strategy.on_order_filled(FakeFill(
            resting.client_order_id, MAKER_ID, resting.order_side,
            str(resting.quantity), "0.73100",
        ))
        self.assertNotEqual(strategy._q, before_q, "the fill must move the inventory")
        self.assertEqual(strategy._state.fills, before_fills + 1)

    def test_an_unconfirmed_order_at_the_timeout_is_still_a_leftover(self) -> None:
        strategy = self.build(cancel_confirm_s=5.0)
        self.quote_once(strategy)
        strategy.cancel_is_silent = True
        strategy._finish("deadline")
        self.tick(strategy, 6.0)  # past cancel_confirm_s
        self.assertGreater(strategy._orders_unconfirmed, 0)
        self.assertNotEqual(strategy.stop_phase, maker_live.STOP_CANCEL)
        strategy.on_stop()
        self.assertTrue(strategy.leftovers)
        self.assertNotEqual(strategy.exit_code, maker_live.EXIT_OK)

    # -- phase 2 ---------------------------------------------------------------------------

    def test_it_closes_both_legs_and_exits_clean(self) -> None:
        strategy = self.build(q=-30.0, hedge_base=30.0)
        strategy._finish("deadline")
        self.tick(strategy)  # nothing rests, so the confirm wait ends at once
        self.assertEqual(strategy.stop_phase, maker_live.STOP_FLATTEN)
        clips = list(strategy.submitted)
        self.assertEqual(len(clips), 2, "one clip per leg, in flight together")
        by_leg = {order.instrument_id: order for order in clips}
        self.assertEqual(by_leg[MAKER_ID].order_side, OrderSide.BUY, "a short closes by buying")
        self.assertEqual(by_leg[HEDGE_ID].order_side, OrderSide.SELL)

        for instrument_id, order in by_leg.items():
            strategy.on_order_filled(FakeFill(
                order.client_order_id, instrument_id, order.order_side,
                str(order.quantity), "0.73000",
            ))
        self.tick(strategy)
        self.assertEqual(strategy.stop_phase, maker_live.STOP_DONE)
        self.assertEqual(strategy.stop_residuals, [])
        self.assertTrue(strategy.done_event.is_set())
        self.assertEqual(strategy.exit_code, maker_live.EXIT_OK)

    def test_one_clip_per_leg_at_a_time(self) -> None:
        strategy = self.build(q=-30.0, hedge_base=30.0)
        strategy._finish("deadline")
        self.tick(strategy)
        sent = len(strategy.submitted)
        self.tick(strategy)
        self.assertEqual(len(strategy.submitted), sent, "nothing until the clip reports back")

    def test_a_leg_left_open_exits_non_zero_with_the_reason(self) -> None:
        strategy = self.build(q=-30.0, hedge_base=30.0, flatten_timeout_s=10.0)
        strategy._finish("deadline")
        self.tick(strategy)
        self.tick(strategy, 12.0)  # the clips never report; the flatten times out
        self.assertEqual(strategy.stop_phase, maker_live.STOP_DONE)
        self.assertTrue(strategy.stop_residuals)
        self.assertEqual(strategy.exit_code, maker_live.EXIT_NOT_FLAT)
        self.assertIn("position left open", strategy.exit_reason)

    def test_flatten_on_stop_off_leaves_the_book_alone(self) -> None:
        strategy = self.build(q=-30.0, hedge_base=30.0, flatten_on_stop=False)
        strategy._finish("deadline")
        self.tick(strategy)
        self.assertEqual(strategy.stop_phase, maker_live.STOP_DONE)
        self.assertEqual(strategy.submitted, [], "no clip may be sent")
        self.assertIn("flatten_on_stop is off", " ".join(strategy._stop_notes))

    def test_the_residual_delta_is_closed_by_the_flatten_not_hedged_first(self) -> None:
        """Hedging the delta and THEN flattening is the double-hedge fixed on 2026-09-09."""
        strategy = self.build(q=-30.0)  # unhedged: delta is armed
        strategy._hedger.add(-30.0, strategy.clock.timestamp_ns() / 1e9)
        hedge_fills_before = strategy._pnl.hedge_fills
        strategy._finish("deadline")
        self.tick(strategy)
        self.assertEqual(strategy.stop_phase, maker_live.STOP_FLATTEN)
        self.assertEqual(strategy._pnl.hedge_fills, hedge_fills_before,
                         "no separate hedge order before the flatten")
        legs = {order.instrument_id for order in strategy.submitted}
        self.assertIn(MAKER_ID, legs, "the maker leg is closed on the maker venue")

    # -- the through-minimum fallback, end to end -------------------------------------------

    def test_a_min_size_denial_switches_the_leg_to_the_through_minimum_close(self) -> None:
        # 5 PONS against a 20 PONS venue minimum: the exact close is the one Lighter denies.
        strategy = self.build(q=-5.0, hedge_base=30.0)
        strategy._finish("deadline")
        self.tick(strategy)
        clip = next(o for o in strategy.submitted if o.instrument_id == MAKER_ID)
        book = strategy._stop_books[MAKER_ID]
        self.assertFalse(book.through_minimum)
        self.assertAlmostEqual(float(clip.quantity.as_decimal()), 5.0, places=6)
        strategy.on_order_denied(FakeDenied(
            clip.client_order_id, "quantity 5 invalid (< minimum trade size of 20)",
        ))
        self.assertTrue(book.through_minimum)
        self.assertEqual(strategy.failures, [], "a minimum is not a failure, it is a detour")
        self.tick(strategy)
        replacement = [o for o in strategy.submitted if o.instrument_id == MAKER_ID][-1]
        self.assertAlmostEqual(float(replacement.quantity.as_decimal()), 25.0, places=6,
                               msg="20 (the minimum) + 5 (the remainder), both legal")
        self.assertGreaterEqual(float(replacement.quantity.as_decimal()), 20.0)

    def test_another_denial_reason_does_not_arm_the_fallback(self) -> None:
        strategy = self.build(q=-5.0, hedge_base=30.0)
        strategy._finish("deadline")
        self.tick(strategy)
        clip = next(o for o in strategy.submitted if o.instrument_id == MAKER_ID)
        strategy.on_order_denied(FakeDenied(clip.client_order_id, "price band exceeded"))
        self.assertFalse(strategy._stop_books[MAKER_ID].through_minimum)
        self.assertTrue(strategy.failures, "an unrelated denial is still a failure")

    # -- reporting ---------------------------------------------------------------------------

    def test_the_summary_carries_the_stop_block(self) -> None:
        strategy = self.build(q=-30.0, hedge_base=30.0)
        strategy._finish("deadline")
        self.tick(strategy)
        for order in list(strategy.submitted):
            strategy.on_order_filled(FakeFill(
                order.client_order_id, order.instrument_id, order.order_side,
                str(order.quantity), "0.73000",
            ))
        self.tick(strategy)
        summary = strategy.summary_line
        self.assertIn("stop=[reason=deadline", summary)
        self.assertIn("phase=done", summary)
        self.assertIn("cancel_confirm=", summary)
        self.assertIn("unconfirmed=0", summary)
        self.assertIn("clips=1,fills=1", summary)
        self.assertIn("cost_usd=", summary)
        self.assertIn("exit_reason=[clean", summary)

    def test_a_run_that_never_traded_does_not_run_the_sequence(self) -> None:
        strategy = self.build()
        strategy._finish("deadline")
        self.assertEqual(strategy.stop_phase, maker_live.STOP_IDLE)
        self.assertTrue(strategy.done_event.is_set())
        self.assertIn("stop=[not run]", strategy.summary_line)

    # -- paper -------------------------------------------------------------------------------

    def test_paper_mode_flattens_locally_and_exits_clean(self) -> None:
        strategy = self.build(mode="paper", q=-30.0, hedge_base=30.0)
        strategy._finish("deadline")
        self.tick(strategy)
        self.assertEqual(strategy.stop_phase, maker_live.STOP_DONE)
        self.assertAlmostEqual(strategy._q, 0.0, places=9)
        self.assertEqual(strategy.stop_residuals, [])
        self.assertEqual(strategy.exit_code, maker_live.EXIT_OK)
        self.assertIn("fills=1", strategy.summary_line)


if __name__ == "__main__":
    unittest.main()

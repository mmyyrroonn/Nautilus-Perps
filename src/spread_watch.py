#!/usr/bin/env python3
"""
Stage 1 - read-only cross-venue spread + depth watch (Hyperliquid / Lighter / Aster).

One process streams top-of-book for N symbols from up to N venues each, computes
the taker-taker spread for every venue pair in both directions on every update,
and appends every net-positive moment to a CSV. In parallel it samples each leg's
order book once per second and records how much notional sits within 2 / 5 / 10
bps of the touch. Data clients only: no execution client, no keys, no signing,
no orders.

    --symbols NVDA,TSLA --venues HL,LIGHTER,ASTER     multi-symbol, N venues
    --pair NVDA:HL-ASTER                              legacy single-pair alias
    --symbol NVDA                                     alias for NVDA:HL-LIGHTER
"""

from __future__ import annotations

import argparse
import csv
import statistics
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nautilus_trader.common import Environment, LogColor, LogLevel, LoggerConfig, TimeEvent
from nautilus_trader.config import StrategyConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import (
    BookType,
    ClientId,
    FundingRateUpdate,
    InstrumentId,
    OrderBook,
    OrderBookDeltas,
    OrderBookDepth10,
    QuoteTick,
    StrategyId,
    TraderId,
)
from nautilus_trader.trading import Strategy

HL_TAKER_FEE_BPS = 0.9  # xyz HIP-3 taker, PROMPT.md section 2
HL_MAIN_TAKER_FEE_BPS = 4.5  # HL main-dex perps, tier-0 taker (no HIP-3 discount)
LIGHTER_TAKER_FEE_BPS = 0.0  # Lighter standard taker
# Verified on mainnet 2026-09-05 via the signed /fapi/v3/commissionRate endpoint for
# NVDAUSDT, XAUUSDT and XAUUSD1: takerCommissionRate 0.000090 (0.9 bps), maker 0.
# The older "20 bps" figure for stock perps is wrong for this account.
ASTER_TAKER_FEE_BPS = 0.9
RESERVE_BPS = 5.0  # one-leg failure reserve
MAX_AGE_MS = 2_000  # a leg older than this is not tradable
STATUS_SECS = 30
FALLBACK_SECS = 20  # no quotes by then -> fall back to depth10
DEPTH_SAMPLE_SECS = 1  # one depth-capacity row per leg per second
DEPTH_SILENT_SECS = 60  # empty deltas book by then -> warn once, fall back to depth10
LOCAL_BOOK_AFTER = 20  # deltas batches with no cache book before we maintain our own
LEG_STALE_SECS = 120  # per-leg staleness warning on the status tick
ALL_SILENT_SECS = 600  # every leg of every symbol silent this long -> restart node
ALL_SAMPLE_MS = 1_000
RESTART_PAUSE_SECS = 15
MAX_RESTARTS = 20
CAPACITY_BPS = (2, 5, 10)  # depth buckets, in bps away from the touch

# Hits csv and _all.csv: one row per pair-direction.
SPREAD_HEADER = [
    "ts_utc", "sell_venue", "buy_venue", "gross_bps", "net_bps",
    "sell_bid", "sell_bid_size", "buy_ask", "buy_ask_size",
    "sell_ask", "buy_bid", "funding_sell", "funding_buy",
    "age_sell_ms", "age_buy_ms",
]
DEPTH_HEADER = [
    "ts_utc", "venue", "bid", "ask", "mid", "levels_bid", "levels_ask",
    *[f"bid_usd_{bps}bps" for bps in CAPACITY_BPS],
    *[f"ask_usd_{bps}bps" for bps in CAPACITY_BPS],
]


# ---------------------------------------------------------------- venue registry


def _adapter_missing(venue_key: str, module: str) -> str:
    return (
        f"[stage1] {venue_key} adapter not installed: cannot import '{module}'. "
        f"Install a nautilus_trader build that ships the {venue_key} adapter into "
        f"this repo's .venv, or pick venues that do not include {venue_key}."
    )


def _hyperliquid_client(instrument_ids: Sequence[str]) -> tuple[object, object]:
    """Hyperliquid mainnet data client (public market data, no keys)."""
    try:
        from nautilus_trader.adapters.hyperliquid import (
            HyperliquidDataClientConfig,
            HyperliquidDataClientFactory,
            HyperliquidEnvironment,
        )
    except ImportError as exc:
        raise SystemExit(_adapter_missing("HL", "nautilus_trader.adapters.hyperliquid")) from exc
    return (
        HyperliquidDataClientFactory(),
        HyperliquidDataClientConfig(environment=HyperliquidEnvironment.MAINNET),
    )


def _lighter_client(instrument_ids: Sequence[str]) -> tuple[object, object]:
    """Lighter mainnet data client (public market data, no keys)."""
    try:
        from nautilus_trader.adapters.lighter import (
            LighterDataClientConfig,
            LighterDataClientFactory,
            LighterEnvironment,
        )
    except ImportError as exc:
        raise SystemExit(_adapter_missing("LIGHTER", "nautilus_trader.adapters.lighter")) from exc
    return (
        LighterDataClientFactory(),
        LighterDataClientConfig(environment=LighterEnvironment.MAINNET),
    )


def _aster_client(instrument_ids: Sequence[str]) -> tuple[object, object]:
    """Aster mainnet data client. Binance-derived adapter, lives in a local fork."""
    try:
        from nautilus_trader.adapters.aster import (
            AsterDataClientConfig,
            AsterDataClientFactory,
            AsterEnvironment,
        )
        from nautilus_trader.adapters.binance import BinanceInstrumentProviderConfig
    except ImportError as exc:
        raise SystemExit(_adapter_missing("ASTER", "nautilus_trader.adapters.aster")) from exc
    return (
        AsterDataClientFactory(),
        AsterDataClientConfig(
            environment=AsterEnvironment.MAINNET,
            # Aster rate-limits exchangeInfo hard: never load_all, only what we watch.
            instrument_provider=BinanceInstrumentProviderConfig(load_ids=list(instrument_ids)),
        ),
    )


@dataclass(frozen=True)
class VenueSpec:
    """One venue: how to name it, and how to build its data client."""

    key: str  # short tag used on the CLI: HL / LIGHTER / ASTER
    venue: str  # Nautilus venue string, also the ClientId
    build_client: Callable[[Sequence[str]], tuple[object, object]]
    supports_depth10: bool = True  # Aster's Binance-derived path has no depth10 sub


VENUES: dict[str, VenueSpec] = {
    "HL": VenueSpec("HL", "HYPERLIQUID", _hyperliquid_client),
    "LIGHTER": VenueSpec("LIGHTER", "LIGHTER", _lighter_client),
    "ASTER": VenueSpec("ASTER", "ASTER", _aster_client, supports_depth10=False),
}
ALL_VENUES = ("HL", "LIGHTER", "ASTER")
DEFAULT_PAIR = ("HL", "LIGHTER")  # what bare --symbol means


# ---------------------------------------------------------------- symbol table


def crypto(base: str) -> dict[str, tuple[str, float]]:
    """Main-dex crypto perp: the three venues name it by rule, no per-name table."""
    return {
        "HL": (f"{base}-USD-PERP.HYPERLIQUID", HL_MAIN_TAKER_FEE_BPS),
        "LIGHTER": (f"{base}-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": (f"{base}USDT-PERP.ASTER", ASTER_TAKER_FEE_BPS),
    }


# Three-venue crypto perps chosen 2026-09-07 from the venue instrument lists: all three
# list them, ranked by the weakest venue's 24h volume (SOL kept as a liquidity control).
CRYPTO_SYMBOLS = ["BTC", "ETH", "SOL", "HYPE", "ZEC", "PONS", "LIT", "ASTER", "DASH", "PUMP", "ARB"]


def stock(base: str, aster_symbol: str) -> dict[str, tuple[str, float]]:
    """HL xyz HIP-3 stock/commodity perp + Lighter + Aster (Aster symbol given explicitly)."""
    return {
        "HL": (f"xyz:{base}-USD-PERP.HYPERLIQUID", HL_TAKER_FEE_BPS),
        "LIGHTER": (f"{base}-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": (f"{aster_symbol}-PERP.ASTER", ASTER_TAKER_FEE_BPS),
    }


# symbol -> venue key -> (instrument id, taker fee bps).
# Funding: HL and Lighter settle hourly, Aster every 8 hours. The CSV stores the
# raw rate as reported by each venue; no per-hour normalisation is done here.
# Aster stock perps: only the USD1-margined listings carry volume (SNDKUSD1 39.8M vs
# SNDKUSDT 3.0M on 2026-09-07); NVDA / TSLA / HOOD exist only as USDT and are thin there.
INSTRUMENTS: dict[str, dict[str, tuple[str, float]]] = {
    "NVDA": stock("NVDA", "NVDAUSDT"),
    "TSLA": stock("TSLA", "TSLAUSDT"),
    "HOOD": stock("HOOD", "HOODUSDT"),
    "SNDK": stock("SNDK", "SNDKUSD1"),
    "MU": stock("MU", "MUUSD1"),
    "SPCX": stock("SPCX", "SPCXUSD1"),
    "GOLD": {  # Aster lists gold as XAUUSDT
        "HL": ("xyz:GOLD-USD-PERP.HYPERLIQUID", HL_TAKER_FEE_BPS),
        "LIGHTER": ("XAU-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": ("XAUUSDT-PERP.ASTER", ASTER_TAKER_FEE_BPS),
    },
    "GOLD1": {  # Aster's USD1-margined gold perp (XAUUSD1); same HL / Lighter legs as GOLD
        "HL": ("xyz:GOLD-USD-PERP.HYPERLIQUID", HL_TAKER_FEE_BPS),
        "LIGHTER": ("XAU-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": ("XAUUSD1-PERP.ASTER", ASTER_TAKER_FEE_BPS),
    },
}
for _base in CRYPTO_SYMBOLS:  # main-dex crypto perps: always live, smoke-test the plumbing
    INSTRUMENTS[_base] = crypto(_base)


# ---------------------------------------------------------------- leg / plan


@dataclass(frozen=True)
class LegSpec:
    """One venue leg: what to subscribe to and what it costs to cross."""

    venue_key: str  # key into VENUES
    instrument_id: str
    client_id: str
    taker_fee_bps: float

    @property
    def label(self) -> str:
        return self.venue_key


def parse_pair(spec: str) -> tuple[str, list[str]]:
    """`NVDA:HL-LIGHTER` -> ("NVDA", ["HL", "LIGHTER"]). Raises SystemExit on bad input."""
    symbol, sep, venues = spec.upper().partition(":")
    if not sep or not venues:
        raise SystemExit(f"[stage1] bad --pair {spec!r}: expected SYMBOL:VENUE_A-VENUE_B")
    keys = [v for v in venues.split("-") if v]
    if len(keys) < 2:
        raise SystemExit(f"[stage1] bad --pair {spec!r}: expected SYMBOL:VENUE_A-VENUE_B")
    if len(set(keys)) != len(keys):
        raise SystemExit(f"[stage1] --pair {spec!r}: venues must differ")
    return symbol, keys


def build_plan(symbols: Sequence[str], venue_keys: Sequence[str]) -> dict[str, list[LegSpec]]:
    """symbol -> legs, one per requested venue that has a mapping. Fails fast."""
    for key in venue_keys:
        if key not in VENUES:
            raise SystemExit(f"[stage1] unknown venue {key!r}; known: {sorted(VENUES)}")
    plan: dict[str, list[LegSpec]] = {}
    for symbol in symbols:
        if symbol not in INSTRUMENTS:
            raise SystemExit(f"[stage1] unknown symbol {symbol!r}; known: {sorted(INSTRUMENTS)}")
        legs: list[LegSpec] = []
        for key in venue_keys:
            mapping = INSTRUMENTS[symbol].get(key)
            if mapping is None:
                print(
                    f"[stage1] WARNING {symbol}: no instrument mapped for {key}; "
                    f"watching the remaining legs",
                    flush=True,
                )
                continue
            legs.append(LegSpec(key, mapping[0], VENUES[key].venue, mapping[1]))
        if len(legs) < 2:
            mapped = sorted(INSTRUMENTS[symbol])
            raise SystemExit(
                f"[stage1] {symbol}: needs at least 2 mapped venues out of "
                f"{list(venue_keys)}; mapped for this symbol: {mapped}",
            )
        plan[symbol] = legs
    return plan


def csv_stem(symbol: str, legs: Sequence[LegSpec], stamp: str) -> str:
    return f"{symbol}_{'-'.join(leg.venue_key for leg in legs)}_{stamp}"


# ---------------------------------------------------------------- csv sink


class CsvSink:
    """Append-only csv; writes the header only when the file is new (restart-safe)."""

    def __init__(self, path: Path, header: Sequence[str]) -> None:
        self.path = path
        self.header = list(header)
        self._handle = None
        self._writer = None
        self.rows = 0

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        self._handle = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        if fresh:
            self._writer.writerow(self.header)
            self._handle.flush()

    def write(self, row: Sequence[object]) -> None:
        if self._writer is None:
            return
        self._writer.writerow(row)
        self._handle.flush()
        self.rows += 1

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None
            self._writer = None


# ---------------------------------------------------------------- leg state


@dataclass
class LegState:
    """Latest top-of-book and latest book snapshot for one leg."""

    spec: LegSpec
    instrument_id: InstrumentId
    client_id: ClientId
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    ts_ns: int = 0
    funding: float | None = None
    updates: int = 0
    source: str = "quotes"  # top-of-book source: quotes / depth10
    depth10: OrderBookDepth10 | None = None
    local_book: OrderBook | None = None
    book_mode: str = "-"  # capacity source actually in use: deltas-cache / deltas-local / depth10
    depth_updates: int = 0  # book messages seen (deltas batches or depth10 snapshots)
    depth_warned: bool = False
    depth10_subscribed: bool = False

    def ready(self) -> bool:
        return self.bid > 0.0 and self.ask > 0.0

    def age_ms(self, now_ns: int) -> float:
        return (now_ns - self.ts_ns) / 1e6


# ---------------------------------------------------------------- capacity math


def _capacity(levels: Sequence[tuple[float, float]], best: float, is_bid: bool) -> list[float]:
    """Cumulative notional (price * size) within each CAPACITY_BPS bucket of the touch."""
    out: list[float] = []
    for bps in CAPACITY_BPS:
        if is_bid:
            limit = best * (1.0 - bps / 1e4)
            total = sum(p * s for p, s in levels if p >= limit)
        else:
            limit = best * (1.0 + bps / 1e4)
            total = sum(p * s for p, s in levels if p <= limit)
        out.append(total)
    return out


def _levels_from_depth10(depth: OrderBookDepth10) -> tuple[list, list]:
    bids = [(float(o.price), float(o.size)) for o in depth.bids]
    asks = [(float(o.price), float(o.size)) for o in depth.asks]
    return ([x for x in bids if x[1] > 0.0], [x for x in asks if x[1] > 0.0])


def _levels_from_book(book: OrderBook) -> tuple[list, list]:
    bids = [(float(lvl.price), lvl.size()) for lvl in book.bids()]
    asks = [(float(lvl.price), lvl.size()) for lvl in book.asks()]
    return ([x for x in bids if x[1] > 0.0], [x for x in asks if x[1] > 0.0])


# ---------------------------------------------------------------- run state


class RunState:
    """Shared across the strategies of one node: the all-silent watchdog."""

    def __init__(self) -> None:
        self.stop_node: Callable[[], None] | None = None
        self.started_ns = 0
        self.last_seen: dict[str, int] = {}
        self.stop_requested = False

    def note_start(self, now_ns: int) -> None:
        if self.started_ns == 0:
            self.started_ns = now_ns

    def report(self, symbol: str, last_ns: int, now_ns: int, log) -> None:
        self.last_seen[symbol] = last_ns
        if self.stop_requested or self.stop_node is None:
            return
        newest = max(self.last_seen.values(), default=0)
        base = newest if newest > 0 else self.started_ns
        if base and (now_ns - base) >= ALL_SILENT_SECS * 1_000_000_000:
            log.error(
                f"[stage1] every leg of every symbol silent for {ALL_SILENT_SECS}s "
                f"-> stopping the node so the restart loop rebuilds it",
            )
            self.stop_requested = True
            self.stop_node()


# ---------------------------------------------------------------- strategy


class SpreadWatchConfig(StrategyConfig):
    """Configuration for the N-leg spread + depth watcher of one symbol."""

    def __init__(
        self,
        *,
        symbol: str,
        legs: list[LegSpec],
        csv_path: Path,
        all_csv_path: Path,
        depth_csv_path: Path,
        run_state: RunState,
        reserve_bps: float = RESERVE_BPS,
        book_depth_levels: int = 10,
        max_age_ms: int = MAX_AGE_MS,
        all_sample_ms: int = ALL_SAMPLE_MS,
        **_kwargs: object,
    ) -> None:
        super().__init__()  # pyo3 base: strategy_id must travel through __new__ kwargs
        self.symbol = symbol
        self.legs = legs
        self.csv_path = csv_path
        self.all_csv_path = all_csv_path
        self.depth_csv_path = depth_csv_path
        self.run_state = run_state
        self.reserve_bps = reserve_bps
        self.book_depth_levels = book_depth_levels
        self.max_age_ms = max_age_ms
        self.all_sample_ms = all_sample_ms


class SpreadWatch(Strategy):
    """Cross-venue taker-taker spread for every venue pair, plus depth capacity."""

    def __init__(self, config: SpreadWatchConfig) -> None:
        super().__init__(config)
        self._cfg = config
        self.symbol = config.symbol
        self._legs = [
            LegState(spec, InstrumentId.from_str(spec.instrument_id),
                     ClientId.from_str(spec.client_id))
            for spec in config.legs
        ]
        self._by_id = {leg.instrument_id: leg for leg in self._legs}
        # every ordered pair (sell venue, buy venue)
        self._pairs = [
            (sell, buy)
            for sell in self._legs
            for buy in self._legs
            if sell is not buy
        ]
        self._fee: dict[tuple[str, str], float] = {}
        self._gross: dict[tuple[str, str], list[float]] = {}
        self._positive: dict[tuple[str, str], int] = {}
        self._last_gross: dict[tuple[str, str], float] = {}
        self._last_all_ns: dict[tuple[str, str], int] = {}
        for sell, buy in self._pairs:
            key = (sell.spec.venue_key, buy.spec.venue_key)
            self._fee[key] = sell.spec.taker_fee_bps + buy.spec.taker_fee_bps + config.reserve_bps
            self._gross[key] = []
            self._positive[key] = 0
            self._last_gross[key] = float("nan")
            self._last_all_ns[key] = 0
        self._samples = 0
        self._stale = 0
        self._start_ns = 0
        self._hits = CsvSink(config.csv_path, SPREAD_HEADER)
        self._all = CsvSink(config.all_csv_path, SPREAD_HEADER)
        self._depth = CsvSink(config.depth_csv_path, DEPTH_HEADER)

    # ---------------------------------------------------------------- lifecycle

    def on_start(self) -> None:
        for leg in self._legs:
            venue = leg.instrument_id.venue
            ids = self.cache.instrument_ids(venue)
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] {venue} instruments loaded: {len(ids)}",
                LogColor.BLUE,
            )
            if self.cache.instrument(leg.instrument_id) is None:
                base = str(leg.instrument_id.symbol).split("-")[0].split(":")[-1]
                near = [str(i) for i in ids if base in str(i)][:20]
                self.log.error(
                    f"[{self.symbol}/{leg.spec.label}] {leg.instrument_id} NOT loaded; "
                    f"candidates: {near}",
                )
                self.stop()
                return
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] instrument OK: {leg.instrument_id}",
                LogColor.GREEN,
            )

        for sink in (self._hits, self._all, self._depth):
            sink.open()

        for leg in self._legs:
            self.subscribe_quotes(leg.instrument_id, client_id=leg.client_id)
            self.subscribe_funding_rates(leg.instrument_id, client_id=leg.client_id)
            # Managed deltas on EVERY venue: the data engine maintains a full L2
            # OrderBook in the cache, which is the capacity source. depth10 caps at
            # 10 levels - far inside 2 bps on a liquid book - so it is only a
            # fallback, engaged from _sample_depth if the deltas book stays empty.
            # This runs in ADDITION to quotes: quotes stay the top-of-book source
            # for the spread, so the spread path is unchanged.
            self.subscribe_book_deltas(
                leg.instrument_id, BookType.L2_MBP,
                client_id=leg.client_id, managed=True,
            )
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] subscribed quotes+funding"
                f"+deltas(managed) {leg.instrument_id}",
                LogColor.GREEN,
            )

        now = self.clock.utc_now()
        self._start_ns = self.clock.timestamp_ns()
        self._cfg.run_state.note_start(self._start_ns)
        self.clock.set_time_alert(f"fallback-{self.symbol}",
                                  now + timedelta(seconds=FALLBACK_SECS))
        self.clock.set_timer(f"status-{self.symbol}", timedelta(seconds=STATUS_SECS),
                             start_time=now)
        self.clock.set_timer(f"depth-{self.symbol}", timedelta(seconds=DEPTH_SAMPLE_SECS),
                             start_time=now)

    def on_stop(self) -> None:
        for sink in (self._hits, self._all, self._depth):
            sink.close()

    # ------------------------------------------------------------------ handlers

    def on_quote(self, quote: QuoteTick) -> None:
        leg = self._by_id.get(quote.instrument_id)
        if leg is None or leg.source != "quotes":
            return
        leg.bid, leg.ask = float(quote.bid_price), float(quote.ask_price)
        leg.bid_size, leg.ask_size = float(quote.bid_size), float(quote.ask_size)
        leg.ts_ns = quote.ts_init
        leg.updates += 1
        self._evaluate(leg)

    def on_book_depth(self, depth: OrderBookDepth10) -> None:
        leg = self._by_id.get(depth.instrument_id)
        if leg is None:
            return
        leg.depth10 = depth
        leg.depth_updates += 1
        if leg.source != "depth10" or not depth.bids or not depth.asks:
            return
        leg.bid, leg.bid_size = float(depth.bids[0].price), float(depth.bids[0].size)
        leg.ask, leg.ask_size = float(depth.asks[0].price), float(depth.asks[0].size)
        leg.ts_ns = depth.ts_init
        leg.updates += 1
        self._evaluate(leg)

    def on_book_deltas(self, deltas: OrderBookDeltas) -> None:
        leg = self._by_id.get(deltas.instrument_id)
        if leg is None:
            return
        leg.depth_updates += 1
        if leg.local_book is None:
            if self.cache.order_book(deltas.instrument_id) is not None:
                return  # managed=True: the engine keeps the book for us
            if leg.depth_updates < LOCAL_BOOK_AFTER:
                return  # the engine may just not have created it yet
            leg.local_book = OrderBook(deltas.instrument_id, BookType.L2_MBP)
            self.log.warning(
                f"[{self.symbol}/{leg.spec.label}] cache holds no order book after "
                f"{leg.depth_updates} managed deltas -> maintaining a local OrderBook",
            )
        leg.local_book.apply_deltas(deltas)

    def on_funding_rate(self, funding_rate: FundingRateUpdate) -> None:
        # Raw venue rate; HL/Lighter settle hourly, Aster every 8 hours.
        leg = self._by_id.get(funding_rate.instrument_id)
        if leg is not None:
            leg.funding = float(funding_rate.rate)

    def on_time_event(self, event: TimeEvent) -> None:
        name = event.name
        if name.startswith("fallback-"):
            self._maybe_fallback()
        elif name.startswith("status-"):
            self._log_status()
        elif name.startswith("depth-"):
            self._sample_depth()

    def _ensure_depth10(self, leg: LegState, reason: str) -> bool:
        """Subscribe depth10 for this leg once, if the venue offers it."""
        if not VENUES[leg.spec.venue_key].supports_depth10:
            self.log.warning(
                f"[{self.symbol}/{leg.spec.label}] {reason} and {leg.spec.venue_key} "
                f"has no depth10 subscription -> no fallback",
            )
            return False
        if not leg.depth10_subscribed:
            leg.depth10_subscribed = True
            self.subscribe_book_depth10(
                leg.instrument_id, BookType.L2_MBP, client_id=leg.client_id,
            )
            self.log.warning(
                f"[{self.symbol}/{leg.spec.label}] {reason} -> subscribing depth10",
            )
        return True

    def _maybe_fallback(self) -> None:
        """Top-of-book fallback: quotes silent -> drive the spread from depth10."""
        for leg in self._legs:
            if leg.updates:
                continue
            if self._ensure_depth10(leg, f"no quotes in {FALLBACK_SECS}s"):
                leg.source = "depth10"

    # ------------------------------------------------------------------ depth

    def _book_levels(self, leg: LegState) -> tuple[list, list]:
        """Capacity source: the managed L2 book first, depth10 only as a fallback."""
        cached = self.cache.order_book(leg.instrument_id)
        for book, mode in ((cached, "deltas-cache"), (leg.local_book, "deltas-local")):
            if book is None:
                continue
            bids, asks = _levels_from_book(book)
            if bids and asks:
                leg.book_mode = mode
                return bids, asks
        if leg.depth10 is not None:
            bids, asks = _levels_from_depth10(leg.depth10)
            if bids and asks:
                leg.book_mode = "depth10"
                return bids, asks
        return [], []

    def _sample_depth(self) -> None:
        now_ns = self.clock.timestamp_ns()
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        silent = self._start_ns and (now_ns - self._start_ns) >= DEPTH_SILENT_SECS * 1_000_000_000
        for leg in self._legs:
            bids, asks = self._book_levels(leg)
            if silent and not (bids and asks) and not leg.depth_warned:
                # The managed deltas book never filled: try depth10 for this leg,
                # and keep writing empty cells until something arrives.
                leg.depth_warned = True
                self._ensure_depth10(
                    leg, f"deltas book still empty after {DEPTH_SILENT_SECS}s "
                         f"({leg.depth_updates} book messages)",
                )
            if not bids or not asks:
                self._depth.write([ts, leg.spec.venue_key, "", "", "", len(bids), len(asks),
                                   *[""] * (2 * len(CAPACITY_BPS))])
                continue
            best_bid, best_ask = bids[0][0], asks[0][0]
            mid = (best_bid + best_ask) / 2.0
            bid_caps = _capacity(bids, best_bid, is_bid=True)
            ask_caps = _capacity(asks, best_ask, is_bid=False)
            self._depth.write([
                ts, leg.spec.venue_key,
                f"{best_bid:.8f}", f"{best_ask:.8f}", f"{mid:.8f}",
                len(bids), len(asks),
                *[f"{v:.2f}" for v in bid_caps],
                *[f"{v:.2f}" for v in ask_caps],
            ])

    # ------------------------------------------------------------------ status

    def _last_update_ns(self) -> int:
        return max((leg.ts_ns for leg in self._legs), default=0)

    def _log_status(self) -> None:
        now_ns = self.clock.timestamp_ns()
        legs = " ".join(
            f"{leg.spec.venue_key}={leg.updates}({leg.source}/{leg.book_mode}:{leg.depth_updates})"
            for leg in self._legs
        )
        gross = " ".join(
            f"{sell}>{buy}={self._last_gross[(sell, buy)]:.2f}"
            for (sell, buy) in self._last_gross
        )
        hits = sum(self._positive.values())
        self.log.info(
            f"STATUS [{self.symbol}] {legs} | samples={self._samples} stale={self._stale} "
            f"| last gross {gross} bps | net+ rows={self._hits.rows} (dirs {hits}) "
            f"| depth rows={self._depth.rows}",
            LogColor.CYAN,
        )
        for leg in self._legs:
            if leg.ts_ns == 0:
                age_s = (now_ns - self._start_ns) / 1e9 if self._start_ns else 0.0
                never = True
            else:
                age_s = (now_ns - leg.ts_ns) / 1e9
                never = False
            if age_s >= LEG_STALE_SECS:
                self.log.warning(
                    f"[{self.symbol}/{leg.spec.label}] no top-of-book update for "
                    f"{age_s:.0f}s" + (" (never received one)" if never else ""),
                )
        self._cfg.run_state.report(self.symbol, self._last_update_ns(), now_ns, self.log)

    # ------------------------------------------------------------------ spread

    def _evaluate(self, updated: LegState) -> None:
        if not updated.ready():
            return
        now_ns = self.clock.timestamp_ns()
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        counted = False
        for sell, buy in self._pairs:
            if updated is not sell and updated is not buy:
                continue
            if not (sell.ready() and buy.ready()):
                continue
            age_sell, age_buy = sell.age_ms(now_ns), buy.age_ms(now_ns)
            if age_sell > self._cfg.max_age_ms or age_buy > self._cfg.max_age_ms:
                self._stale += 1
                continue
            mid = ((sell.bid + sell.ask) / 2.0 + (buy.bid + buy.ask) / 2.0) / 2.0
            if mid <= 0.0:
                continue
            key = (sell.spec.venue_key, buy.spec.venue_key)
            gross_bps = (sell.bid - buy.ask) / mid * 1e4
            net_bps = gross_bps - self._fee[key]
            self._last_gross[key] = gross_bps
            self._gross[key].append(gross_bps)
            if not counted:
                self._samples += 1
                counted = True
            row = self._row(ts, sell, buy, gross_bps, net_bps, age_sell, age_buy)
            if net_bps > 0.0:
                self._positive[key] += 1
                self._hits.write(row)
            if now_ns - self._last_all_ns[key] >= self._cfg.all_sample_ms * 1_000_000:
                self._last_all_ns[key] = now_ns
                self._all.write(row)

    @staticmethod
    def _row(ts: str, sell: LegState, buy: LegState, gross_bps: float, net_bps: float,
             age_sell: float, age_buy: float) -> list:
        return [
            ts, sell.spec.venue_key, buy.spec.venue_key,
            f"{gross_bps:.4f}", f"{net_bps:.4f}",
            sell.bid, sell.bid_size, buy.ask, buy.ask_size,
            sell.ask, buy.bid,
            "" if sell.funding is None else f"{sell.funding:.10f}",
            "" if buy.funding is None else f"{buy.funding:.10f}",
            f"{age_sell:.1f}", f"{age_buy:.1f}",
        ]

    # ------------------------------------------------------------------ summary

    def received(self) -> bool:
        return any(leg.updates for leg in self._legs)

    def summary(self) -> str:
        lines = [
            "",
            "=" * 78,
            f"SUMMARY [{self.symbol}]",
        ]
        for leg in self._legs:
            lines.append(
                f"  leg {leg.spec.venue_key:<8} top-of-book updates={leg.updates} "
                f"({leg.source})  book updates={leg.depth_updates} ({leg.book_mode})  "
                f"{leg.spec.instrument_id}",
            )
        lines.append(
            f"  evaluated samples={self._samples}  skipped-stale={self._stale}",
        )
        lines.append(
            f"  net-positive rows written={self._hits.rows}   "
            f"all-sample rows={self._all.rows}   depth rows={self._depth.rows}",
        )
        for (sell, buy) in self._gross:
            series = self._gross[(sell, buy)]
            fee = self._fee[(sell, buy)]
            if not series:
                lines.append(f"  {sell}>{buy}: no samples (fee+reserve={fee:.2f} bps)")
                continue
            pct = 100.0 * self._positive[(sell, buy)] / len(series)
            lines.append(
                f"  {sell}>{buy}: net-positive {self._positive[(sell, buy)]}/{len(series)} "
                f"({pct:.2f}%)  gross median={statistics.median(series):.2f} "
                f"max={max(series):.2f} min={min(series):.2f} bps  "
                f"fee+reserve={fee:.2f}",
            )
        for path in (self._cfg.csv_path, self._cfg.all_csv_path, self._cfg.depth_csv_path):
            lines.append(f"  csv: {path}")
        lines.append("=" * 78)
        return "\n".join(lines)


# ---------------------------------------------------------------- node


@dataclass
class NodePlan:
    """Everything the node builder needs; fixed once at process start."""

    plan: dict[str, list[LegSpec]]
    out_dir: Path
    stamp: str
    depth_levels: int
    all_sample_ms: int
    stems: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for symbol, legs in self.plan.items():
            self.stems[symbol] = csv_stem(symbol, legs, self.stamp)


def build_node(np: NodePlan) -> tuple[LiveNode, list[SpreadWatch], RunState]:
    run_state = RunState()
    builder = (
        LiveNode.builder("STAGE1-SPREAD-WATCH", TraderId.from_str("STAGE1-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.INFO))
        .with_timeout_connection(60)
        .with_delay_post_stop_secs(2)
    )
    # Exactly one data client per venue, carrying the union of the instrument ids
    # we watch there (Aster's load_ids must cover every Aster symbol in the run).
    ids_by_venue: dict[str, list[str]] = {}
    for legs in np.plan.values():
        for leg in legs:
            ids_by_venue.setdefault(leg.venue_key, [])
            if leg.instrument_id not in ids_by_venue[leg.venue_key]:
                ids_by_venue[leg.venue_key].append(leg.instrument_id)
    for venue_key, ids in ids_by_venue.items():
        factory, client_config = VENUES[venue_key].build_client(ids)
        builder = builder.add_data_client(None, factory, client_config)
    node = builder.build()

    strategies: list[SpreadWatch] = []
    for symbol, legs in np.plan.items():
        stem = np.stems[symbol]
        config = SpreadWatchConfig(
            strategy_id=StrategyId.from_str(f"SPREAD-WATCH-{symbol}"),
            symbol=symbol,
            legs=legs,
            csv_path=np.out_dir / f"spread_{stem}.csv",
            all_csv_path=np.out_dir / f"spread_{stem}_all.csv",
            depth_csv_path=np.out_dir / f"depth_{stem}.csv",
            run_state=run_state,
            book_depth_levels=np.depth_levels,
            all_sample_ms=np.all_sample_ms,
        )
        strategy = SpreadWatch(config)
        node.add_strategy(strategy)
        strategies.append(strategy)
    return node, strategies, run_state


# ---------------------------------------------------------------- cli


def parse_until(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"[stage1] bad --until {value!r}: expected UTC ISO, "
                         f"e.g. 2026-09-08T20:05:00Z") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def resolve_targets(args, parser) -> tuple[list[str], list[str]]:
    given = [bool(args.pair), bool(args.symbol), bool(args.symbols)]
    if sum(given) > 1:
        parser.error("use only one of --pair / --symbol / --symbols")
    if args.pair:
        symbol, venue_keys = parse_pair(args.pair)
        return [symbol], venue_keys
    if args.symbol:
        return [args.symbol.upper()], list(DEFAULT_PAIR)
    venue_keys = [v.strip().upper() for v in args.venues.split(",") if v.strip()]
    if len(venue_keys) < 2:
        parser.error("--venues needs at least two venues")
    if len(set(venue_keys)) != len(venue_keys):
        parser.error("--venues must not repeat a venue")
    source = args.symbols or "NVDA"
    symbols: list[str] = []
    for name in source.split(","):
        name = name.strip().upper()
        if name and name not in symbols:
            symbols.append(name)
    if not symbols:
        parser.error("--symbols is empty")
    return symbols, venue_keys


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 1 read-only cross-venue spread watch")
    parser.add_argument("--minutes", type=float, default=30.0, help="run duration in minutes")
    parser.add_argument("--until", default=None,
                        help="stop at this UTC ISO instant, e.g. 2026-09-08T20:05:00Z "
                             "(alternative to --minutes)")
    parser.add_argument("--out", type=Path, default=Path("reports/stage1"),
                        help="output directory")
    parser.add_argument("--symbols", default=None,
                        help=f"comma list of symbols, e.g. NVDA,TSLA; known: {sorted(INSTRUMENTS)}")
    parser.add_argument("--venues", default=",".join(ALL_VENUES),
                        help=f"comma list of venues to watch per symbol; known: {sorted(VENUES)}")
    parser.add_argument("--pair", default=None,
                        help=f"legacy alias: SYMBOL:VENUE_A-VENUE_B, e.g. NVDA:HL-ASTER; "
                             f"venues: {sorted(VENUES)}")
    parser.add_argument("--symbol", default=None, choices=sorted(INSTRUMENTS),
                        help="legacy alias for SYMBOL:HL-LIGHTER")
    parser.add_argument("--all-sample-ms", type=int, default=ALL_SAMPLE_MS,
                        help="min interval between _all.csv rows per pair-direction; "
                             "0 = every evaluation")
    parser.add_argument("--depth-levels", type=int, default=10,
                        help="book depth levels for the depth10 fallback "
                             "(v2 fixes this at 10; kept for config parity)")
    parser.add_argument("--max-restarts", type=int, default=MAX_RESTARTS,
                        help="rebuild the node at most this many times before the deadline")
    args = parser.parse_args()

    symbols, venue_keys = resolve_targets(args, parser)
    plan = build_plan(symbols, venue_keys)

    started = datetime.now(timezone.utc)
    if args.until:
        deadline = parse_until(args.until)
        if deadline <= started:
            raise SystemExit(f"[stage1] --until {args.until} is already in the past")
    else:
        deadline = started + timedelta(minutes=args.minutes)

    try:  # optional; no credentials are needed for read-only market data
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    node_plan = NodePlan(
        plan=plan,
        out_dir=args.out,
        stamp=started.strftime("%Y%m%dT%H%M%SZ"),  # fixed at process start, not per restart
        depth_levels=args.depth_levels,
        all_sample_ms=args.all_sample_ms,
    )
    describe = "  ".join(
        f"{symbol}[{'-'.join(leg.venue_key for leg in legs)}]" for symbol, legs in plan.items()
    )
    print(
        f"[stage1] watching {describe} until {deadline.isoformat(timespec='seconds')} "
        f"-> {args.out}",
        flush=True,
    )

    strategies: list[SpreadWatch] = []
    restarts = 0
    ever_received = False
    while True:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 1.0:
            break
        node, strategies, run_state = build_node(node_plan)
        handle = node.handle()
        run_state.stop_node = handle.stop
        timer = threading.Timer(remaining, handle.stop)
        timer.daemon = True
        timer.start()
        interrupted = False
        failure: BaseException | None = None
        try:
            node.run()
        except KeyboardInterrupt:
            print("[stage1] interrupted, stopping", flush=True)
            handle.stop()
            interrupted = True
        except Exception as exc:  # noqa: BLE001 - any adapter fault restarts the node
            failure = exc
        finally:
            timer.cancel()

        for strategy in strategies:
            ever_received = ever_received or strategy.received()
            print(strategy.summary(), flush=True)
        if failure is not None:
            print(f"[stage1] node run failed: {failure!r}", flush=True)
        if interrupted:
            break
        left = (deadline - datetime.now(timezone.utc)).total_seconds()
        if left <= 5.0:
            break
        restarts += 1
        if restarts > args.max_restarts:
            print(f"[stage1] giving up after {args.max_restarts} restarts", flush=True)
            break
        print(
            f"[stage1] node stopped {left:.0f}s before the deadline "
            f"-> restart {restarts}/{args.max_restarts} in {RESTART_PAUSE_SECS}s",
            flush=True,
        )
        time.sleep(RESTART_PAUSE_SECS)

    if restarts:
        print(f"[stage1] node restarts during this run: {restarts}", flush=True)
    if not ever_received:
        raise SystemExit(1)


if __name__ == "__main__":
    main()

#!/usr/bin/env python3
"""
Stage 1 - read-only cross-venue spread watch (Hyperliquid / Lighter / Aster).

Streams top-of-book for one symbol from two mainnet venues, computes the
taker-taker spread in both directions on every update, and appends every
net-positive moment to a CSV. Data clients only: no execution client, no keys,
no signing, no orders.

Any two of the registered venues can be paired:
    --pair NVDA:HL-LIGHTER      (symbol : venue A - venue B)
    --pair NVDA:HL-ASTER
    --symbol NVDA               (alias for NVDA:HL-LIGHTER, the stage-1 default)
"""

from __future__ import annotations

import argparse
import csv
import statistics
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
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
ALL_SAMPLE_MS = 1_000
CONNECT_ATTEMPTS = 3  # venue connects are occasionally flaky; a failed connect writes no CSV

CSV_HEADER = [
    "ts_utc", "direction", "gross_bps", "net_bps",
    "bid_A", "ask_A", "bid_size_A", "ask_size_A",
    "bid_B", "ask_B", "bid_size_B", "ask_size_B",
    "funding_A", "funding_B", "age_A_ms", "age_B_ms",
]
DIR_AB = "A_sell_B_buy"  # sell on A, buy on B
DIR_BA = "B_sell_A_buy"


# ---------------------------------------------------------------- venue registry


def _adapter_missing(venue_key: str, module: str) -> str:
    return (
        f"[stage1] {venue_key} adapter not installed: cannot import '{module}'. "
        f"Install a nautilus_trader build that ships the {venue_key} adapter into "
        f"this repo's .venv, or pick a pair that does not use {venue_key}."
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

# Legacy stage-1 pair: keeps the old CSV file names so reports/stage1 stays uniform.
DEFAULT_PAIR = ("HL", "LIGHTER")

# symbol -> venue key -> (instrument id, taker fee bps).
# Funding: HL and Lighter settle hourly, Aster every 8 hours. The CSV stores the
# raw rate as reported by each venue; no per-hour normalisation is done here.
# MSFT / HOOD / MU / SNDK / CRCL exist on HL xyz (xyz:MSFT-USD-PERP.HYPERLIQUID
# etc.) but their Aster / Lighter listings are not verified yet - add them once
# the instrument ids are confirmed against each venue's instrument list.
INSTRUMENTS: dict[str, dict[str, tuple[str, float]]] = {
    "NVDA": {
        "HL": ("xyz:NVDA-USD-PERP.HYPERLIQUID", HL_TAKER_FEE_BPS),
        "LIGHTER": ("NVDA-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": ("NVDAUSDT-PERP.ASTER", ASTER_TAKER_FEE_BPS),
    },
    "TSLA": {
        "HL": ("xyz:TSLA-USD-PERP.HYPERLIQUID", HL_TAKER_FEE_BPS),
        "LIGHTER": ("TSLA-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": ("TSLAUSDT-PERP.ASTER", ASTER_TAKER_FEE_BPS),
    },
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
    "BTC": {  # main-dex crypto perp: always live, used to smoke-test the plumbing
        "HL": ("BTC-USD-PERP.HYPERLIQUID", HL_MAIN_TAKER_FEE_BPS),
        "LIGHTER": ("BTC-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": ("BTCUSDT-PERP.ASTER", ASTER_TAKER_FEE_BPS),
    },
}


@dataclass(frozen=True)
class LegSpec:
    """One venue leg: what to subscribe to and what it costs to cross."""

    tag: str  # "A" / "B"; fixes the CSV column suffixes
    venue_key: str  # key into VENUES
    instrument_id: str
    client_id: str
    taker_fee_bps: float

    @property
    def label(self) -> str:
        return f"{self.tag}:{self.venue_key}"


def parse_pair(spec: str) -> tuple[str, str, str]:
    """`NVDA:HL-LIGHTER` -> ("NVDA", "HL", "LIGHTER"). Raises SystemExit on bad input."""
    symbol, sep, venues = spec.upper().partition(":")
    if not sep or not venues:
        raise SystemExit(f"[stage1] bad --pair {spec!r}: expected SYMBOL:VENUE_A-VENUE_B")
    venue_a, sep, venue_b = venues.partition("-")
    if not sep or not venue_b:
        raise SystemExit(f"[stage1] bad --pair {spec!r}: expected SYMBOL:VENUE_A-VENUE_B")
    if symbol not in INSTRUMENTS:
        raise SystemExit(f"[stage1] unknown symbol {symbol!r}; known: {sorted(INSTRUMENTS)}")
    for venue in (venue_a, venue_b):
        if venue not in VENUES:
            raise SystemExit(f"[stage1] unknown venue {venue!r}; known: {sorted(VENUES)}")
        if venue not in INSTRUMENTS[symbol]:
            raise SystemExit(
                f"[stage1] no {symbol} instrument mapped for {venue}; "
                f"mapped: {sorted(INSTRUMENTS[symbol])}",
            )
    if venue_a == venue_b:
        raise SystemExit(f"[stage1] --pair {spec!r}: the two venues must differ")
    return symbol, venue_a, venue_b


def build_legs(symbol: str, venue_a: str, venue_b: str) -> tuple[LegSpec, LegSpec]:
    legs = []
    for tag, venue_key in (("A", venue_a), ("B", venue_b)):
        instrument_id, fee_bps = INSTRUMENTS[symbol][venue_key]
        legs.append(LegSpec(tag, venue_key, instrument_id, VENUES[venue_key].venue, fee_bps))
    return legs[0], legs[1]


def csv_stem(symbol: str, venue_a: str, venue_b: str, stamp: str) -> str:
    if (venue_a, venue_b) == DEFAULT_PAIR:
        return f"spread_{symbol}_{stamp}"  # legacy name, kept for reports/stage1
    return f"spread_{symbol}_{venue_a}-{venue_b}_{stamp}"


@dataclass
class LegState:
    """Latest top-of-book for one leg."""

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
    source: str = "quotes"

    def ready(self) -> bool:
        return self.bid > 0.0 and self.ask > 0.0

    def age_ms(self, now_ns: int) -> float:
        return (now_ns - self.ts_ns) / 1e6


class SpreadWatchConfig(StrategyConfig):
    """Configuration for the two-leg spread watcher."""

    def __init__(
        self,
        *,
        leg_a: LegSpec,
        leg_b: LegSpec,
        csv_path: Path,
        all_csv_path: Path,
        reserve_bps: float = RESERVE_BPS,
        book_depth_levels: int = 10,
        max_age_ms: int = MAX_AGE_MS,
        all_sample_ms: int = ALL_SAMPLE_MS,
        **_kwargs: object,
    ) -> None:
        super().__init__()  # pyo3 base: strategy_id must travel through __new__ kwargs
        self.leg_a = leg_a
        self.leg_b = leg_b
        self.csv_path = csv_path
        self.all_csv_path = all_csv_path
        self.reserve_bps = reserve_bps
        self.book_depth_levels = book_depth_levels
        self.max_age_ms = max_age_ms
        self.all_sample_ms = all_sample_ms


class SpreadWatch(Strategy):
    """Compute the cross-venue taker-taker spread and log net-positive moments."""

    def __init__(self, config: SpreadWatchConfig) -> None:
        super().__init__(config)
        self._cfg = config
        self._a = LegState(config.leg_a, InstrumentId.from_str(config.leg_a.instrument_id),
                           ClientId.from_str(config.leg_a.client_id))
        self._b = LegState(config.leg_b, InstrumentId.from_str(config.leg_b.instrument_id),
                           ClientId.from_str(config.leg_b.client_id))
        self._by_id = {self._a.instrument_id: self._a, self._b.instrument_id: self._b}
        self._fee_total = config.leg_a.taker_fee_bps + config.leg_b.taker_fee_bps + config.reserve_bps
        self._last_gross = {DIR_AB: float("nan"), DIR_BA: float("nan")}
        self._gross: dict[str, list[float]] = {DIR_AB: [], DIR_BA: []}
        self._positive = {DIR_AB: 0, DIR_BA: 0}
        self._samples = 0
        self._stale = 0
        self._rows = 0
        self._all_rows = 0
        self._last_all_ns = 0
        self._files: list = []
        self._hit_writer: csv.writer | None = None  # type: ignore[valid-type]
        self._all_writer: csv.writer | None = None  # type: ignore[valid-type]

    # ---------------------------------------------------------------- lifecycle

    def on_start(self) -> None:
        for leg in (self._a, self._b):
            venue = leg.instrument_id.venue
            ids = self.cache.instrument_ids(venue)
            self.log.info(f"[{leg.spec.label}] {venue} instruments loaded: {len(ids)}", LogColor.BLUE)
            if self.cache.instrument(leg.instrument_id) is None:
                base = str(leg.instrument_id.symbol).split("-")[0].split(":")[-1]
                near = [str(i) for i in ids if base in str(i)][:20]
                self.log.error(f"[{leg.spec.label}] {leg.instrument_id} NOT loaded; candidates: {near}")
                self.stop()
                return
            self.log.info(f"[{leg.spec.label}] instrument OK: {leg.instrument_id}", LogColor.GREEN)

        self._open_csv()
        for leg in (self._a, self._b):
            self.subscribe_quotes(leg.instrument_id, client_id=leg.client_id)
            self.subscribe_funding_rates(leg.instrument_id, client_id=leg.client_id)
            self.log.info(f"[{leg.spec.label}] subscribed quotes+funding {leg.instrument_id}", LogColor.GREEN)

        now = self.clock.utc_now()
        self.clock.set_time_alert("fallback", now + timedelta(seconds=FALLBACK_SECS))
        self.clock.set_timer("status", timedelta(seconds=STATUS_SECS), start_time=now)

    def on_stop(self) -> None:
        for handle in self._files:
            handle.flush()
            handle.close()
        self._files.clear()

    def _open_csv(self) -> None:
        for path, attr in ((self._cfg.csv_path, "_hit_writer"), (self._cfg.all_csv_path, "_all_writer")):
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("w", newline="", encoding="utf-8")
            writer = csv.writer(handle)
            writer.writerow(CSV_HEADER)
            handle.flush()
            self._files.append(handle)
            setattr(self, attr, writer)

    # ------------------------------------------------------------------ handlers

    def on_quote(self, quote: QuoteTick) -> None:
        leg = self._by_id.get(quote.instrument_id)
        if leg is None or leg.source != "quotes":
            return
        leg.bid, leg.ask = float(quote.bid_price), float(quote.ask_price)
        leg.bid_size, leg.ask_size = float(quote.bid_size), float(quote.ask_size)
        leg.ts_ns = quote.ts_init
        leg.updates += 1
        self._evaluate()

    def on_book_depth(self, depth: OrderBookDepth10) -> None:
        leg = self._by_id.get(depth.instrument_id)
        if leg is None or leg.source != "depth10" or not depth.bids or not depth.asks:
            return
        leg.bid, leg.bid_size = float(depth.bids[0].price), float(depth.bids[0].size)
        leg.ask, leg.ask_size = float(depth.asks[0].price), float(depth.asks[0].size)
        leg.ts_ns = depth.ts_init
        leg.updates += 1
        self._evaluate()

    def on_funding_rate(self, funding_rate: FundingRateUpdate) -> None:
        # Raw venue rate; HL/Lighter settle hourly, Aster every 8 hours.
        leg = self._by_id.get(funding_rate.instrument_id)
        if leg is not None:
            leg.funding = float(funding_rate.rate)

    def on_time_event(self, event: TimeEvent) -> None:
        if event.name == "fallback":
            self._maybe_fallback()
        elif event.name == "status":
            self._log_status()

    def _maybe_fallback(self) -> None:
        for leg in (self._a, self._b):
            if leg.updates == 0:
                if not VENUES[leg.spec.venue_key].supports_depth10:
                    self.log.warning(
                        f"[{leg.spec.label}] no quotes in {FALLBACK_SECS}s and "
                        f"{leg.spec.venue_key} has no depth10 subscription -> no fallback",
                    )
                    continue
                leg.source = "depth10"
                self.subscribe_book_depth10(leg.instrument_id, BookType.L2_MBP, client_id=leg.client_id)
                self.log.warning(f"[{leg.spec.label}] no quotes in {FALLBACK_SECS}s -> depth10 fallback")

    def _log_status(self) -> None:
        self.log.info(
            f"STATUS updates A={self._a.updates}({self._a.source}) B={self._b.updates}({self._b.source}) "
            f"| samples={self._samples} stale={self._stale} "
            f"| last gross {DIR_AB}={self._last_gross[DIR_AB]:.2f} {DIR_BA}={self._last_gross[DIR_BA]:.2f} bps "
            f"| net+ rows={self._rows} ({DIR_AB}={self._positive[DIR_AB]}, {DIR_BA}={self._positive[DIR_BA]})",
            LogColor.CYAN,
        )

    # ------------------------------------------------------------------ spread

    def _evaluate(self) -> None:
        a, b = self._a, self._b
        if not (a.ready() and b.ready()):
            return
        now_ns = self.clock.timestamp_ns()
        age_a, age_b = a.age_ms(now_ns), b.age_ms(now_ns)
        if age_a > self._cfg.max_age_ms or age_b > self._cfg.max_age_ms:
            self._stale += 1
            return

        mid = ((a.bid + a.ask) / 2.0 + (b.bid + b.ask) / 2.0) / 2.0
        if mid <= 0.0:
            return
        gross = {
            DIR_AB: (a.bid - b.ask) / mid * 1e4,
            DIR_BA: (b.bid - a.ask) / mid * 1e4,
        }
        self._samples += 1
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        sample_all = now_ns - self._last_all_ns >= self._cfg.all_sample_ms * 1_000_000

        for direction, gross_bps in gross.items():
            net_bps = gross_bps - self._fee_total
            self._last_gross[direction] = gross_bps
            self._gross[direction].append(gross_bps)
            row = self._row(ts, direction, gross_bps, net_bps, age_a, age_b)
            if net_bps > 0.0:
                self._positive[direction] += 1
                self._rows += 1
                self._write(self._hit_writer, self._files[0], row)
            if sample_all:
                self._all_rows += 1
                self._write(self._all_writer, self._files[1], row)
        if sample_all:
            self._last_all_ns = now_ns

    def _row(self, ts: str, direction: str, gross_bps: float, net_bps: float,
             age_a: float, age_b: float) -> list:
        a, b = self._a, self._b
        return [
            ts, direction, f"{gross_bps:.4f}", f"{net_bps:.4f}",
            a.bid, a.ask, a.bid_size, a.ask_size,
            b.bid, b.ask, b.bid_size, b.ask_size,
            "" if a.funding is None else f"{a.funding:.10f}",
            "" if b.funding is None else f"{b.funding:.10f}",
            f"{age_a:.1f}", f"{age_b:.1f}",
        ]

    @staticmethod
    def _write(writer, handle, row: list) -> None:
        writer.writerow(row)
        handle.flush()

    # ------------------------------------------------------------------ summary

    def received(self) -> bool:
        return (self._a.updates + self._b.updates) > 0

    def summary(self) -> str:
        lines = [
            "",
            "=" * 78,
            f"SUMMARY  updates A={self._a.updates} ({self._a.spec.venue_key}, {self._a.source})  "
            f"B={self._b.updates} ({self._b.spec.venue_key}, {self._b.source})",
            f"  evaluated samples={self._samples}  skipped-stale={self._stale}  "
            f"fee+reserve={self._fee_total:.2f} bps",
            f"  net-positive rows written={self._rows}   all-sample rows={self._all_rows}",
        ]
        for direction in (DIR_AB, DIR_BA):
            series = self._gross[direction]
            if not series:
                lines.append(f"  {direction}: no samples")
                continue
            pct = 100.0 * self._positive[direction] / len(series)
            lines.append(
                f"  {direction}: net-positive {self._positive[direction]}/{len(series)} "
                f"({pct:.2f}%)  gross median={statistics.median(series):.2f} "
                f"max={max(series):.2f} min={min(series):.2f} bps",
            )
        lines.append(f"  csv: {self._cfg.csv_path}")
        lines.append(f"  csv: {self._cfg.all_csv_path}")
        lines.append("=" * 78)
        return "\n".join(lines)


def build_node(symbol: str, venue_a: str, venue_b: str, out_dir: Path, depth_levels: int,
               all_sample_ms: int) -> tuple[LiveNode, SpreadWatch]:
    leg_a, leg_b = build_legs(symbol, venue_a, venue_b)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    stem = csv_stem(symbol, venue_a, venue_b, stamp)
    config = SpreadWatchConfig(
        strategy_id=StrategyId.from_str("SPREAD-WATCH-001"),
        leg_a=leg_a,
        leg_b=leg_b,
        csv_path=out_dir / f"{stem}.csv",
        all_csv_path=out_dir / f"{stem}_all.csv",
        book_depth_levels=depth_levels,
        all_sample_ms=all_sample_ms,
    )
    strategy = SpreadWatch(config)
    builder = (
        LiveNode.builder("STAGE1-SPREAD-WATCH", TraderId.from_str("STAGE1-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.INFO))
        .with_timeout_connection(60)
        .with_delay_post_stop_secs(2)
    )
    for leg in (leg_a, leg_b):  # only the two venues of this pair are registered
        factory, client_config = VENUES[leg.venue_key].build_client([leg.instrument_id])
        builder = builder.add_data_client(None, factory, client_config)
    node = builder.build()
    node.add_strategy(strategy)
    return node, strategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 1 read-only cross-venue spread watch")
    parser.add_argument("--minutes", type=float, default=30.0, help="run duration in minutes")
    parser.add_argument("--out", type=Path, default=Path("reports/stage1"), help="output directory")
    parser.add_argument("--pair", default=None,
                        help=f"SYMBOL:VENUE_A-VENUE_B, e.g. NVDA:HL-ASTER; venues: {sorted(VENUES)}")
    parser.add_argument("--symbol", default=None, choices=sorted(INSTRUMENTS),
                        help="alias for SYMBOL:HL-LIGHTER (legacy stage-1 pair)")
    parser.add_argument("--all-sample-ms", type=int, default=ALL_SAMPLE_MS, help="min interval between _all.csv rows; 0 = every evaluation")
    parser.add_argument("--depth-levels", type=int, default=10, help="book depth levels for the depth10 fallback (v2 fixes this at 10; kept for config parity)")
    args = parser.parse_args()

    if args.pair and args.symbol:
        parser.error("use either --pair or --symbol, not both")
    pair_spec = args.pair or f"{args.symbol or 'NVDA'}:{DEFAULT_PAIR[0]}-{DEFAULT_PAIR[1]}"
    symbol, venue_a, venue_b = parse_pair(pair_spec)

    try:  # optional; no credentials are needed for read-only market data
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    print(f"[stage1] watching {symbol} {venue_a}-{venue_b} for {args.minutes} min -> {args.out}", flush=True)
    strategy: SpreadWatch | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        # Venue connects are occasionally flaky (TLS handshake eof / instrument
        # bootstrap timeout). A failed connect writes no CSV, so retrying is safe.
        node, strategy = build_node(symbol, venue_a, venue_b, args.out, args.depth_levels,
                                    args.all_sample_ms)
        handle = node.handle()
        timer = threading.Timer(args.minutes * 60.0, handle.stop)
        timer.daemon = True
        timer.start()
        try:
            node.run()
        except KeyboardInterrupt:
            print("[stage1] interrupted, stopping", flush=True)
            handle.stop()
        except RuntimeError as exc:
            print(f"[stage1] attempt {attempt}/{CONNECT_ATTEMPTS} failed: {exc}", flush=True)
            timer.cancel()
            if not strategy.received() and attempt < CONNECT_ATTEMPTS:
                continue
        timer.cancel()
        break

    print(strategy.summary(), flush=True)
    if not strategy.received():
        raise SystemExit(1)


if __name__ == "__main__":
    main()

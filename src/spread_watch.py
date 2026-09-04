#!/usr/bin/env python3
"""
Stage 1 - read-only cross-venue spread watch (Hyperliquid xyz HIP-3 vs Lighter).

Streams top-of-book for one symbol from both mainnet venues, computes the
taker-taker spread in both directions on every update, and appends every
net-positive moment to a CSV. Data clients only: no execution client, no keys,
no signing, no orders.
"""

from __future__ import annotations

import argparse
import csv
import statistics
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from nautilus_trader.adapters.hyperliquid import (
    HyperliquidDataClientConfig,
    HyperliquidDataClientFactory,
    HyperliquidEnvironment,
)
from nautilus_trader.adapters.lighter import (
    LighterDataClientConfig,
    LighterDataClientFactory,
    LighterEnvironment,
)
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


@dataclass(frozen=True)
class LegSpec:
    """One venue leg: what to subscribe to and what it costs to cross."""

    tag: str
    instrument_id: str
    client_id: str
    taker_fee_bps: float


# symbol -> (leg A, leg B). Explicit by design; a third venue is a config change only.
SYMBOLS: dict[str, tuple[LegSpec, LegSpec]] = {
    "NVDA": (
        LegSpec("A", "xyz:NVDA-USD-PERP.HYPERLIQUID", "HYPERLIQUID", HL_TAKER_FEE_BPS),
        LegSpec("B", "NVDA-PERP.LIGHTER", "LIGHTER", LIGHTER_TAKER_FEE_BPS),
    ),
    "GOLD": (
        LegSpec("A", "xyz:GOLD-USD-PERP.HYPERLIQUID", "HYPERLIQUID", HL_TAKER_FEE_BPS),
        LegSpec("B", "XAU-PERP.LIGHTER", "LIGHTER", LIGHTER_TAKER_FEE_BPS),
    ),
    "TSLA": (
        LegSpec("A", "xyz:TSLA-USD-PERP.HYPERLIQUID", "HYPERLIQUID", HL_TAKER_FEE_BPS),
        LegSpec("B", "TSLA-PERP.LIGHTER", "LIGHTER", LIGHTER_TAKER_FEE_BPS),
    ),
    "BTC": (
        LegSpec("A", "BTC-USD-PERP.HYPERLIQUID", "HYPERLIQUID", HL_MAIN_TAKER_FEE_BPS),
        LegSpec("B", "BTC-PERP.LIGHTER", "LIGHTER", LIGHTER_TAKER_FEE_BPS),
    ),
}


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
            self.log.info(f"[{leg.spec.tag}] {venue} instruments loaded: {len(ids)}", LogColor.BLUE)
            if self.cache.instrument(leg.instrument_id) is None:
                base = str(leg.instrument_id.symbol).split("-")[0].split(":")[-1]
                near = [str(i) for i in ids if base in str(i)][:20]
                self.log.error(f"[{leg.spec.tag}] {leg.instrument_id} NOT loaded; candidates: {near}")
                self.stop()
                return
            self.log.info(f"[{leg.spec.tag}] instrument OK: {leg.instrument_id}", LogColor.GREEN)

        self._open_csv()
        for leg in (self._a, self._b):
            self.subscribe_quotes(leg.instrument_id, client_id=leg.client_id)
            self.subscribe_funding_rates(leg.instrument_id, client_id=leg.client_id)
            self.log.info(f"[{leg.spec.tag}] subscribed quotes+funding {leg.instrument_id}", LogColor.GREEN)

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
                leg.source = "depth10"
                self.subscribe_book_depth10(leg.instrument_id, BookType.L2_MBP, client_id=leg.client_id)
                self.log.warning(f"[{leg.spec.tag}] no quotes in {FALLBACK_SECS}s -> depth10 fallback")

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
            f"SUMMARY  updates A={self._a.updates} ({self._a.source})  "
            f"B={self._b.updates} ({self._b.source})",
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


def build_node(symbol: str, out_dir: Path, depth_levels: int, all_sample_ms: int) -> tuple[LiveNode, SpreadWatch]:
    leg_a, leg_b = SYMBOLS[symbol]
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    config = SpreadWatchConfig(
        strategy_id=StrategyId.from_str("SPREAD-WATCH-001"),
        leg_a=leg_a,
        leg_b=leg_b,
        csv_path=out_dir / f"spread_{symbol}_{stamp}.csv",
        all_csv_path=out_dir / f"spread_{symbol}_{stamp}_all.csv",
        book_depth_levels=depth_levels,
        all_sample_ms=all_sample_ms,
    )
    strategy = SpreadWatch(config)
    node = (
        LiveNode.builder("STAGE1-SPREAD-WATCH", TraderId.from_str("STAGE1-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.INFO))
        .with_timeout_connection(60)
        .with_delay_post_stop_secs(2)
        .add_data_client(
            None,
            HyperliquidDataClientFactory(),
            HyperliquidDataClientConfig(environment=HyperliquidEnvironment.MAINNET),
        )
        .add_data_client(
            None,
            LighterDataClientFactory(),
            LighterDataClientConfig(environment=LighterEnvironment.MAINNET),
        )
        .build()
    )
    node.add_strategy(strategy)
    return node, strategy


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 1 read-only cross-venue spread watch")
    parser.add_argument("--minutes", type=float, default=30.0, help="run duration in minutes")
    parser.add_argument("--out", type=Path, default=Path("reports/stage1"), help="output directory")
    parser.add_argument("--symbol", default="NVDA", choices=sorted(SYMBOLS), help="symbol to watch")
    parser.add_argument("--all-sample-ms", type=int, default=ALL_SAMPLE_MS, help="min interval between _all.csv rows; 0 = every evaluation")
    parser.add_argument("--depth-levels", type=int, default=10, help="book depth levels for the depth10 fallback (v2 fixes this at 10; kept for config parity)")
    args = parser.parse_args()

    try:  # optional; no credentials are needed for read-only market data
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    print(f"[stage1] watching {args.symbol} for {args.minutes} min -> {args.out}", flush=True)
    strategy: SpreadWatch | None = None
    for attempt in range(1, CONNECT_ATTEMPTS + 1):
        # Venue connects are occasionally flaky (TLS handshake eof / instrument
        # bootstrap timeout). A failed connect writes no CSV, so retrying is safe.
        node, strategy = build_node(args.symbol, args.out, args.depth_levels, args.all_sample_ms)
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

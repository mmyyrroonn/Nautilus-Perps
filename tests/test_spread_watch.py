#!/usr/bin/env python3
"""
Offline regression tests for ``src/spread_watch.py`` (read-only cross-venue watcher).

Everything here runs without a network and without a LiveNode: the strategy is built
with a real config, its trader-owned surfaces (cache / log / clock / subscribe_*) are
stubbed, and the real ``on_start`` / ``on_trade`` / ``on_stop`` write real CSV files.

    .venv\\Scripts\\python.exe -m pytest tests -q
"""

from __future__ import annotations

import contextlib
import csv
import io
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import spread_watch  # noqa: E402
from nautilus_trader.model import AggressorSide  # noqa: E402
from nautilus_trader.model import InstrumentId  # noqa: E402
from nautilus_trader.model import Price  # noqa: E402
from nautilus_trader.model import Quantity  # noqa: E402
from nautilus_trader.model import StrategyId  # noqa: E402
from nautilus_trader.model import TradeId  # noqa: E402
from nautilus_trader.model import TradeTick  # noqa: E402


# --------------------------------------------------------------------------------- fixtures


class FakeCache:
    """Stand-in for the Nautilus cache: every watched instrument is "loaded"."""

    def __init__(self, instrument_ids: tuple[InstrumentId, ...]) -> None:
        self._ids = list(instrument_ids)

    def instrument_ids(self, venue) -> list[InstrumentId]:
        return [i for i in self._ids if i.venue == venue]

    def instrument(self, instrument_id: InstrumentId) -> object | None:
        return object() if instrument_id in self._ids else None

    def order_book(self, instrument_id: InstrumentId) -> None:
        return None


class FakeLog:
    """Collects log lines instead of writing them."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _record(self, level: str, message: str, *_args: object) -> None:
        self.lines.append((level, message))

    def info(self, message: str, *args: object) -> None:
        self._record("INFO", message)

    def warning(self, message: str, *args: object) -> None:
        self._record("WARNING", message)

    def error(self, message: str, *args: object) -> None:
        self._record("ERROR", message)

    def text(self) -> str:
        return "\n".join(f"{level} {msg}" for level, msg in self.lines)


class FakeClock:
    """Fixed clock; timers are recorded, never fired."""

    def __init__(self) -> None:
        self.now = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
        self.timers: list[str] = []
        self.alerts: list[str] = []

    def utc_now(self) -> datetime:
        return self.now

    def timestamp_ns(self) -> int:
        return int(self.now.timestamp() * 1_000_000_000)

    def set_timer(self, name: str, interval: timedelta, start_time=None) -> None:
        self.timers.append(name)

    def set_time_alert(self, name: str, alert_time) -> None:
        self.alerts.append(name)


class WatchUnderTest(spread_watch.SpreadWatch):
    """
    The production strategy with the trader-owned surfaces replaced by stubs.

    Only ``cache`` / ``log`` / ``clock`` / ``subscribe_*`` are stubbed; the sink
    handling, the trade row and the status line under test are production code.
    """

    def __new__(cls, config, *_args: object, **_kwargs: object):
        # The pyo3 base __new__ only accepts the config; drop the test-double arguments.
        return super().__new__(cls, config)

    def __init__(self, config) -> None:
        super().__init__(config)
        self._stub_cache = FakeCache(tuple(leg.instrument_id for leg in self._legs))
        self._stub_log = FakeLog()
        self._stub_clock = FakeClock()
        self.subscribed: dict[str, list[InstrumentId]] = {
            "quotes": [], "funding": [], "trades": [], "deltas": [], "depth10": [],
        }

    @property
    def cache(self) -> FakeCache:
        return self._stub_cache

    @property
    def log(self) -> FakeLog:
        return self._stub_log

    @property
    def clock(self) -> FakeClock:
        return self._stub_clock

    def subscribe_quotes(self, instrument_id, client_id=None, params=None) -> None:
        self.subscribed["quotes"].append(instrument_id)

    def subscribe_funding_rates(self, instrument_id, client_id=None, params=None) -> None:
        self.subscribed["funding"].append(instrument_id)

    def subscribe_trades(self, instrument_id, client_id=None, params=None) -> None:
        self.subscribed["trades"].append(instrument_id)

    def subscribe_book_deltas(self, instrument_id, book_type, client_id=None,
                              managed=False, **_kw) -> None:
        self.subscribed["deltas"].append(instrument_id)

    def subscribe_book_depth10(self, instrument_id, book_type, client_id=None, **_kw) -> None:
        self.subscribed["depth10"].append(instrument_id)


def build_watch(out_dir: Path, symbol: str = "NVDA",
                venue_keys: tuple[str, ...] = ("HL", "LIGHTER_RH")) -> WatchUnderTest:
    """A started watcher for ``symbol`` writing its CSVs under ``out_dir``."""
    with contextlib.redirect_stdout(io.StringIO()):
        legs = spread_watch.build_plan([symbol], list(venue_keys))[symbol]
    stem = spread_watch.csv_stem(symbol, legs, "20260907T120000Z")
    config = spread_watch.SpreadWatchConfig(
        strategy_id=StrategyId.from_str(f"SPREAD-WATCH-TEST-{symbol}"),
        symbol=symbol,
        legs=legs,
        csv_path=out_dir / f"spread_{stem}.csv",
        all_csv_path=out_dir / f"spread_{stem}_all.csv",
        depth_csv_path=out_dir / f"depth_{stem}.csv",
        trades_csv_path=out_dir / f"trades_{stem}.csv",
        run_state=spread_watch.RunState(),
    )
    return WatchUnderTest(config)


def make_trade(instrument_id: InstrumentId, price: str, size: str,
               side: AggressorSide, trade_id: str, ts: int) -> TradeTick:
    return TradeTick(
        instrument_id=instrument_id,
        price=Price.from_str(price),
        size=Quantity.from_str(size),
        aggressor_side=side,
        trade_id=TradeId(trade_id),
        ts_event=ts,
        ts_init=ts + 1_000_000,
    )


def read_csv(path: Path) -> list[list[str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.reader(handle))


# ------------------------------------------------------------------------ trades csv


class TestTradesCsv(unittest.TestCase):
    """``on_trade`` must append one well-formed row per trade tick, per leg."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.out = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)

    def test_header_written_once_and_rows_appended(self) -> None:
        watch = build_watch(self.out)
        watch.on_start()
        hl, rh = watch._legs[0].instrument_id, watch._legs[1].instrument_id
        watch.on_trade(make_trade(hl, "180.25", "3.5", AggressorSide.BUY, "T-1", 1_000))
        watch.on_trade(make_trade(rh, "180.31", "0.4", AggressorSide.SELL, "T-2", 2_000))
        watch.on_trade(
            make_trade(rh, "180.30", "1.0", AggressorSide.NO_AGGRESSOR, "T-3", 3_000),
        )
        watch.on_stop()

        path = self.out / "trades_NVDA_HL-LIGHTER_RH_20260907T120000Z.csv"
        self.assertTrue(path.exists(), f"missing {path}")
        rows = read_csv(path)
        self.assertEqual(rows[0], spread_watch.TRADES_HEADER)
        self.assertEqual(rows[0], [
            "ts_utc", "venue", "price", "size", "aggressor_side", "trade_id",
            "ts_event_ns", "ts_init_ns",
        ])
        self.assertEqual(len(rows), 4)
        self.assertEqual(rows[1][1:], ["HL", "180.25", "3.5", "BUY", "T-1", "1000", "1001000"])
        self.assertEqual(
            rows[2][1:], ["LIGHTER_RH", "180.31", "0.4", "SELL", "T-2", "2000", "1002000"],
        )
        self.assertEqual(rows[3][4], "NO_AGGRESSOR")
        for row in rows[1:]:  # ts_utc: same ISO shape as the other sinks
            self.assertRegex(row[0], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+00:00$")

    def test_reopening_appends_without_a_second_header(self) -> None:
        watch = build_watch(self.out)
        watch.on_start()
        hl = watch._legs[0].instrument_id
        watch.on_trade(make_trade(hl, "1.0", "1", AggressorSide.BUY, "T-1", 1_000))
        watch.on_stop()

        restarted = build_watch(self.out)  # same stem -> same file, restart-safe
        restarted.on_start()
        restarted.on_trade(make_trade(hl, "2.0", "2", AggressorSide.SELL, "T-2", 2_000))
        restarted.on_stop()

        rows = read_csv(self.out / "trades_NVDA_HL-LIGHTER_RH_20260907T120000Z.csv")
        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[0], spread_watch.TRADES_HEADER)
        self.assertEqual([row[5] for row in rows[1:]], ["T-1", "T-2"])

    def test_every_leg_subscribes_trades_and_status_counts_them(self) -> None:
        watch = build_watch(self.out)
        watch.on_start()
        ids = [leg.instrument_id for leg in watch._legs]
        self.assertEqual(watch.subscribed["trades"], ids)
        self.assertEqual(watch.subscribed["quotes"], ids)  # unchanged subscriptions

        watch.on_trade(make_trade(ids[1], "1.0", "1", AggressorSide.BUY, "T-1", 1_000))
        watch.on_trade(make_trade(ids[1], "1.0", "1", AggressorSide.BUY, "T-2", 2_000))
        watch._log_status()
        watch.on_stop()

        status = [msg for level, msg in watch.log.lines if msg.startswith("STATUS")]
        self.assertEqual(len(status), 1)
        self.assertIn("HL=0(quotes/-:0,trades=0)", status[0])
        self.assertIn("LIGHTER_RH=0(quotes/-:0,trades=2)", status[0])
        self.assertIn("trade rows=2", status[0])

    def test_trade_for_an_unwatched_instrument_is_ignored(self) -> None:
        watch = build_watch(self.out)
        watch.on_start()
        other = InstrumentId.from_str("SOL-PERP.LIGHTER")
        watch.on_trade(make_trade(other, "1.0", "1", AggressorSide.BUY, "T-X", 1_000))
        watch.on_stop()
        rows = read_csv(self.out / "trades_NVDA_HL-LIGHTER_RH_20260907T120000Z.csv")
        self.assertEqual(rows, [spread_watch.TRADES_HEADER])

    def test_existing_csv_schemas_are_unchanged(self) -> None:
        self.assertEqual(spread_watch.SPREAD_HEADER[:5], [
            "ts_utc", "sell_venue", "buy_venue", "gross_bps", "net_bps",
        ])
        self.assertEqual(spread_watch.DEPTH_HEADER[:7], [
            "ts_utc", "venue", "bid", "ask", "mid", "levels_bid", "levels_ask",
        ])


# ------------------------------------------------------------------------- build_plan


class TestBuildPlanSkipsUnmappedVenues(unittest.TestCase):
    """A symbol a venue does not list drops that leg instead of failing the run."""

    def test_hood_skips_lighter_rh(self) -> None:
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            plan = spread_watch.build_plan(["HOOD"], ["HL", "LIGHTER", "LIGHTER_RH", "ASTER"])
        self.assertEqual([leg.venue_key for leg in plan["HOOD"]], ["HL", "LIGHTER", "ASTER"])
        self.assertIn("HOOD: no instrument mapped for LIGHTER_RH", buffer.getvalue())
        self.assertIn("INFO", buffer.getvalue())

    def test_mapped_symbol_keeps_every_requested_venue(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()) as buffer:
            plan = spread_watch.build_plan(["NVDA"], ["HL", "LIGHTER", "LIGHTER_RH", "ASTER"])
        self.assertEqual(
            [leg.venue_key for leg in plan["NVDA"]],
            ["HL", "LIGHTER", "LIGHTER_RH", "ASTER"],
        )
        self.assertEqual(buffer.getvalue(), "")
        rh = plan["NVDA"][2]
        self.assertEqual(rh.instrument_id, "NVDA-PERP.LIGHTER_ROBINHOOD")
        self.assertEqual(rh.client_id, "LIGHTER_ROBINHOOD")
        self.assertEqual(rh.taker_fee_bps, 0.0)

    def test_fewer_than_two_mapped_venues_still_fails(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            with self.assertRaises(SystemExit) as ctx:
                spread_watch.build_plan(["ARB"], ["HL", "LIGHTER_RH"])
        self.assertIn("needs at least 2 mapped venues", str(ctx.exception))

    def test_unknown_venue_is_rejected(self) -> None:
        with self.assertRaises(SystemExit):
            spread_watch.build_plan(["NVDA"], ["HL", "NOPE"])


# ---------------------------------------------------------------------- venue registry


class TestLighterRobinhoodVenue(unittest.TestCase):
    """LIGHTER_RH is a known, zero-fee venue - and stays opt-in."""

    def test_registered_with_the_robinhood_venue_string(self) -> None:
        spec = spread_watch.VENUES["LIGHTER_RH"]
        self.assertEqual(spec.key, "LIGHTER_RH")
        self.assertEqual(spec.venue, "LIGHTER_ROBINHOOD")
        self.assertIn("LIGHTER_RH", spread_watch.ALL_VENUES)

    def test_matches_the_adapter_venue_constant(self) -> None:
        from nautilus_trader.adapters.lighter import LIGHTER_ROBINHOOD_VENUE

        self.assertEqual(spread_watch.VENUES["LIGHTER_RH"].venue, str(LIGHTER_ROBINHOOD_VENUE))

    def test_taker_fee_is_zero_everywhere_it_is_mapped(self) -> None:
        self.assertEqual(spread_watch.LIGHTER_RH_TAKER_FEE_BPS, 0.0)
        mapped = [
            (symbol, mapping["LIGHTER_RH"])
            for symbol, mapping in spread_watch.INSTRUMENTS.items()
            if "LIGHTER_RH" in mapping
        ]
        self.assertTrue(mapped)
        for symbol, (instrument_id, fee) in mapped:
            self.assertEqual(fee, 0.0, symbol)
            self.assertTrue(instrument_id.endswith("-PERP.LIGHTER_ROBINHOOD"), instrument_id)

    def test_stays_out_of_the_default_venues(self) -> None:
        self.assertNotIn("LIGHTER_RH", spread_watch.DEFAULT_VENUES)
        self.assertNotIn("LIGHTER_RH", spread_watch.DEFAULT_PAIR)
        self.assertEqual(spread_watch.DEFAULT_VENUES, ("HL", "LIGHTER", "ASTER"))

    def test_client_builder_uses_the_robinhood_deployment(self) -> None:
        from nautilus_trader.adapters.lighter import LighterDeployment, LighterEnvironment

        _factory, config = spread_watch.VENUES["LIGHTER_RH"].build_client(
            ["NVDA-PERP.LIGHTER_ROBINHOOD"],
        )
        self.assertEqual(config.deployment, LighterDeployment.ROBINHOOD)
        self.assertEqual(config.environment, LighterEnvironment.MAINNET)
        self.assertEqual(str(config.venue), "LIGHTER_ROBINHOOD")

    def test_analysis_knows_the_funding_units(self) -> None:
        from analysis import opportunities

        self.assertEqual(
            opportunities.FUNDING_SCALE["LIGHTER_RH"], opportunities.FUNDING_SCALE["LIGHTER"],
        )
        self.assertEqual(
            opportunities.FUNDING_HOURS["LIGHTER_RH"], opportunities.FUNDING_HOURS["LIGHTER"],
        )


if __name__ == "__main__":
    unittest.main()

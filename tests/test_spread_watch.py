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
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest


sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import spread_watch  # noqa: E402
from nautilus_trader.model import AggressorSide  # noqa: E402
from nautilus_trader.model import FundingRateUpdate  # noqa: E402
from nautilus_trader.model import InstrumentId  # noqa: E402
from nautilus_trader.model import Price  # noqa: E402
from nautilus_trader.model import Quantity  # noqa: E402
from nautilus_trader.model import QuoteTick  # noqa: E402
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
        # Same subscriptions, but with the client they were routed through: with two
        # HYPERLIQUID legs the instrument alone no longer says which client was used.
        self.subscribed_routes: list[tuple[str, InstrumentId, str]] = []
        self.stops: list[int] = []

    def stop(self, *args: object, **_kwargs: object) -> None:
        """Production ``stop()`` needs a running strategy state; record the call."""
        self.stops.append(1)

    @property
    def cache(self) -> FakeCache:
        return self._stub_cache

    @property
    def log(self) -> FakeLog:
        return self._stub_log

    @property
    def clock(self) -> FakeClock:
        return self._stub_clock

    def _route(self, kind: str, instrument_id, client_id) -> None:
        self.subscribed_routes.append((kind, instrument_id, str(client_id)))

    def subscribe_quotes(self, instrument_id, client_id=None, params=None) -> None:
        self.subscribed["quotes"].append(instrument_id)
        self._route("quotes", instrument_id, client_id)

    def subscribe_funding_rates(self, instrument_id, client_id=None, params=None) -> None:
        self.subscribed["funding"].append(instrument_id)
        self._route("funding", instrument_id, client_id)

    def subscribe_trades(self, instrument_id, client_id=None, params=None) -> None:
        self.subscribed["trades"].append(instrument_id)
        self._route("trades", instrument_id, client_id)

    def subscribe_book_deltas(self, instrument_id, book_type, client_id=None,
                              managed=False, **_kw) -> None:
        self.subscribed["deltas"].append(instrument_id)
        self._route("deltas", instrument_id, client_id)

    def subscribe_book_depth10(self, instrument_id, book_type, client_id=None, **_kw) -> None:
        self.subscribed["depth10"].append(instrument_id)
        self._route("depth10", instrument_id, client_id)


def build_watch(out_dir: Path, symbol: str = "NVDA",
                venue_keys: tuple[str, ...] = ("HL", "LIGHTER_RH")) -> WatchUnderTest:
    """A started watcher for ``symbol`` writing its CSVs under ``out_dir``."""
    with contextlib.redirect_stderr(io.StringIO()):
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
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            plan = spread_watch.build_plan(["HOOD"], ["HL", "LIGHTER", "LIGHTER_RH", "ASTER"])
        self.assertEqual([leg.venue_key for leg in plan["HOOD"]], ["HL", "LIGHTER", "ASTER"])
        # The skip note goes to stderr: stdout stays parseable for --dry-run.
        self.assertEqual(out.getvalue(), "")
        self.assertIn("HOOD: no instrument mapped for LIGHTER_RH", err.getvalue())
        self.assertIn("INFO", err.getvalue())

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


# ------------------------------------------------------------------- entropy venue


class TestEntropyVenue(unittest.TestCase):
    """ENTROPY is a logical leg of its own that rides the one HYPERLIQUID client."""

    def test_registered_on_the_hyperliquid_venue(self) -> None:
        spec = spread_watch.VENUES["ENTROPY"]
        self.assertEqual(spec.key, "ENTROPY")
        self.assertEqual(spec.venue, "HYPERLIQUID")
        self.assertIs(spec.build_client, spread_watch.VENUES["HL"].build_client)
        self.assertIn("ENTROPY", spread_watch.ALL_VENUES)

    def test_maps_the_io_instruments_with_its_own_fee(self) -> None:
        self.assertEqual(
            spread_watch.INSTRUMENTS["SNDK"]["ENTROPY"],
            ("io:SNDK-USD-PERP.HYPERLIQUID", spread_watch.ENTROPY_TAKER_FEE_BPS),
        )
        self.assertEqual(spread_watch.INSTRUMENTS["GPRO"], {
            "ENTROPY": ("io:GPRO-USD-PERP.HYPERLIQUID", spread_watch.ENTROPY_TAKER_FEE_BPS),
            "ASTER": ("GPROUSD1-PERP.ASTER", spread_watch.ASTER_TAKER_FEE_BPS),
        })
        # Its own constant (Tier-0 Entropy taker, checked 2026-09-14), even though it
        # happens to equal the xyz rate today: an xyz fee change must not move io:.
        self.assertEqual(spread_watch.ENTROPY_TAKER_FEE_BPS, 0.9)
        self.assertIn("ENTROPY_TAKER_FEE_BPS", spread_watch.__dict__)

    def test_gpro_only_has_the_entropy_and_aster_legs(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            plan = spread_watch.build_plan(["GPRO"], ["HL", "LIGHTER", "ENTROPY", "ASTER"])
        self.assertEqual([leg.venue_key for leg in plan["GPRO"]], ["ENTROPY", "ASTER"])
        self.assertEqual(plan["GPRO"][0].instrument_id, "io:GPRO-USD-PERP.HYPERLIQUID")
        self.assertEqual(plan["GPRO"][0].client_id, "HYPERLIQUID")
        self.assertEqual(plan["GPRO"][1].instrument_id, "GPROUSD1-PERP.ASTER")

    def test_stays_out_of_the_defaults(self) -> None:
        self.assertNotIn("ENTROPY", spread_watch.DEFAULT_VENUES)
        self.assertNotIn("ENTROPY", spread_watch.DEFAULT_PAIR)
        self.assertEqual(spread_watch.DEFAULT_VENUES, ("HL", "LIGHTER", "ASTER"))
        self.assertEqual(spread_watch.DEFAULT_PAIR, ("HL", "LIGHTER"))


# ------------------------------------------------------------------- client groups


def test_entropy_and_xyz_share_one_client_group():
    with contextlib.redirect_stderr(io.StringIO()):
        plan = spread_watch.build_plan(["SNDK", "GPRO"], ["HL", "ENTROPY", "ASTER"])
    groups = {g.client_id: g for g in spread_watch.build_client_groups(plan)}
    assert set(groups) == {"HYPERLIQUID", "ASTER"}
    assert groups["HYPERLIQUID"].instrument_ids == [
        "xyz:SNDK-USD-PERP.HYPERLIQUID",
        "io:SNDK-USD-PERP.HYPERLIQUID",
        "io:GPRO-USD-PERP.HYPERLIQUID",
    ]
    assert groups["ASTER"].instrument_ids == [
        "SNDKUSD1-PERP.ASTER", "GPROUSD1-PERP.ASTER",
    ]
    assert len(plan["SNDK"]) == 3
    assert len(plan["GPRO"]) == 2
    assert spread_watch.DEFAULT_VENUES == ("HL", "LIGHTER", "ASTER")
    assert spread_watch.DEFAULT_PAIR == ("HL", "LIGHTER")


def test_five_legs_use_four_clients_without_merging_lighter():
    with contextlib.redirect_stderr(io.StringIO()):
        plan = spread_watch.build_plan(
            ["SNDK"], ["HL", "ENTROPY", "LIGHTER", "LIGHTER_RH", "ASTER"],
        )
    assert len(plan["SNDK"]) == 5
    groups = spread_watch.build_client_groups(plan)
    assert [g.client_id for g in groups] == [
        "HYPERLIQUID", "LIGHTER", "LIGHTER_ROBINHOOD", "ASTER",
    ]
    assert groups[0].instrument_ids == [
        "xyz:SNDK-USD-PERP.HYPERLIQUID", "io:SNDK-USD-PERP.HYPERLIQUID",
    ]


def test_build_node_registers_shared_hyperliquid_once(tmp_path, monkeypatch):
    from dataclasses import replace
    from unittest.mock import MagicMock

    loaded = {}

    def hl_client(ids):
        loaded["HL"] = list(ids)
        return object(), object()

    def aster_client(ids):
        loaded["ASTER"] = list(ids)
        return object(), object()

    for key in ("HL", "ENTROPY"):
        monkeypatch.setitem(spread_watch.VENUES, key,
                            replace(spread_watch.VENUES[key], build_client=hl_client))
    monkeypatch.setitem(spread_watch.VENUES, "ASTER",
                        replace(spread_watch.VENUES["ASTER"], build_client=aster_client))
    live = MagicMock()
    builder = live.builder.return_value
    for method in ("with_logging", "with_timeout_connection",
                   "with_delay_post_stop_secs", "add_data_client"):
        getattr(builder, method).return_value = builder
    monkeypatch.setattr(spread_watch, "LiveNode", live)
    with contextlib.redirect_stderr(io.StringIO()):
        plan = spread_watch.build_plan(["SNDK", "GPRO"], ["HL", "ENTROPY", "ASTER"])
    np = spread_watch.NodePlan(plan, tmp_path, "20260914T000000Z", 10, 1000)
    node, strategies, state = spread_watch.build_node(np)
    assert [c.args[0] for c in builder.add_data_client.call_args_list] == ["HYPERLIQUID", "ASTER"]
    assert loaded["ASTER"] == ["SNDKUSD1-PERP.ASTER", "GPROUSD1-PERP.ASTER"]
    assert len(loaded["HL"]) == 3
    assert len(strategies) == 2
    assert node.add_strategy.call_count == 2
    node.run.assert_not_called()


# ------------------------------------------------------------------------- dry run


def _run_dry_run(argv: list[str], monkeypatch, tmp_path) -> tuple[dict, str]:
    """main() with --dry-run: returns (parsed stdout, stderr) and forbids side effects."""
    import dotenv
    from dataclasses import replace
    from unittest.mock import patch

    def boom(ids):
        raise AssertionError("dry-run must not build a data client")

    for key in list(spread_watch.VENUES):
        monkeypatch.setitem(spread_watch.VENUES, key,
                            replace(spread_watch.VENUES[key], build_client=boom))
    out_dir = tmp_path / "must-not-be-created"
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sys, "argv", ["spread_watch.py", *argv, "--out", str(out_dir)]), \
            patch.object(spread_watch, "build_node") as build, \
            patch.object(dotenv, "load_dotenv") as load:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            spread_watch.main()
    build.assert_not_called()
    load.assert_not_called()
    assert not out_dir.exists(), "dry-run must not write anything"
    return json.loads(out.getvalue()), err.getvalue()


def test_dry_run_is_json_and_never_builds_node(tmp_path, monkeypatch):
    result, err = _run_dry_run(
        ["--symbols", "SNDK,GPRO", "--venues", "HL,ENTROPY,ASTER", "--dry-run"],
        monkeypatch, tmp_path,
    )
    assert result["mode"] == "read-only"
    assert result["market_availability_checked"] is False
    assert [x["client_id"] for x in result["data_clients"]] == ["HYPERLIQUID", "ASTER"]
    assert "GPRO" in err
    assert sorted(result["symbols"]) == ["GPRO", "SNDK"]
    assert [leg["venue_key"] for leg in result["symbols"]["SNDK"]] == [
        "HL", "ENTROPY", "ASTER",
    ]
    assert [leg["venue_key"] for leg in result["symbols"]["GPRO"]] == ["ENTROPY", "ASTER"]
    assert result["symbols"]["SNDK"][1] == {
        "venue_key": "ENTROPY",
        "instrument_id": "io:SNDK-USD-PERP.HYPERLIQUID",
        "client_id": "HYPERLIQUID",
        "taker_fee_bps": spread_watch.ENTROPY_TAKER_FEE_BPS,
    }
    assert result["data_clients"][0]["instrument_ids"] == [
        "xyz:SNDK-USD-PERP.HYPERLIQUID",
        "io:SNDK-USD-PERP.HYPERLIQUID",
        "io:GPRO-USD-PERP.HYPERLIQUID",
    ]


def test_dry_run_works_through_the_legacy_pair_alias(tmp_path, monkeypatch):
    result, err = _run_dry_run(["--pair", "SNDK:ENTROPY-ASTER", "--dry-run"],
                               monkeypatch, tmp_path)
    assert [x["client_id"] for x in result["data_clients"]] == ["HYPERLIQUID", "ASTER"]
    assert err == ""


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


# ------------------------------------------------------- event isolation and coverage


def make_quote(instrument_id: InstrumentId, bid: str, ask: str, ts: int) -> QuoteTick:
    return QuoteTick(
        instrument_id=instrument_id,
        bid_price=Price.from_str(bid), ask_price=Price.from_str(ask),
        bid_size=Quantity.from_str("1.0000"), ask_size=Quantity.from_str("2.0000"),
        ts_event=ts, ts_init=ts,
    )


def test_xyz_and_io_trades_keep_distinct_labels(tmp_path):
    watch = build_watch(tmp_path, "SNDK", ("HL", "ENTROPY", "ASTER"))
    watch.on_start()
    try:
        for n, leg in enumerate(watch._legs):
            watch.on_trade(make_trade(leg.instrument_id, "100.00", "1.0",
                                      AggressorSide.BUY, f"T-{n}", 1_000 + n))
        other = InstrumentId.from_str("io:GPRO-USD-PERP.HYPERLIQUID")
        watch.on_trade(make_trade(other, "1.00", "1.0", AggressorSide.BUY, "OTHER", 2_000))
    finally:
        watch.on_stop()
    rows = read_csv(next(tmp_path.glob("trades_*.csv")))
    assert [row[1] for row in rows[1:]] == ["HL", "ENTROPY", "ASTER"]


def test_entropy_quote_and_funding_do_not_change_xyz(tmp_path):
    watch = build_watch(tmp_path, "SNDK", ("HL", "ENTROPY", "ASTER"))
    watch.on_start()
    try:
        now = watch.clock.timestamp_ns()
        xyz, entropy, aster = watch._legs
        watch.on_quote(make_quote(entropy.instrument_id, "100.00", "100.01", now))
        assert entropy.bid == 100.0 and entropy.updates == 1
        assert xyz.updates == 0 and aster.updates == 0
        for leg, rate, interval in ((xyz, "0.0001", 60), (entropy, "-0.0002", 60),
                                    (aster, "0.0008", 480)):
            watch.on_funding_rate(FundingRateUpdate(
                instrument_id=leg.instrument_id, rate=Decimal(rate),
                ts_event=now, ts_init=now, interval=interval,
            ))
        assert xyz.funding == 0.0001
        assert entropy.funding == -0.0002
        assert aster.funding == 0.0008
        assert len(watch._pairs) == 6
    finally:
        watch.on_stop()


def test_each_leg_subscribes_through_its_own_client(tmp_path):
    watch = build_watch(tmp_path, "SNDK", ("HL", "ENTROPY", "ASTER"))
    watch.on_start()
    try:
        xyz, entropy, aster = (leg.instrument_id for leg in watch._legs)
        routes = set(watch.subscribed_routes)
        for kind in ("quotes", "funding", "trades", "deltas"):
            assert (kind, xyz, "HYPERLIQUID") in routes
            assert (kind, entropy, "HYPERLIQUID") in routes
            assert (kind, aster, "ASTER") in routes
        assert watch.subscribed["depth10"] == []
    finally:
        watch.on_stop()


def test_depth10_fallback_covers_both_hyperliquid_legs_but_never_aster(tmp_path):
    watch = build_watch(tmp_path, "SNDK", ("HL", "ENTROPY", "ASTER"))
    watch.on_start()
    try:
        watch._maybe_fallback()
        assert watch.subscribed["depth10"] == [
            watch._legs[0].instrument_id, watch._legs[1].instrument_id,
        ]
    finally:
        watch.on_stop()


def test_coverage_needs_every_leg_not_just_one_venue(tmp_path):
    with contextlib.redirect_stderr(io.StringIO()):
        plan = spread_watch.build_plan(["SNDK", "GPRO"], ["HL", "ENTROPY", "ASTER"])
    sndk = build_watch(tmp_path / "sndk", "SNDK", ("HL", "ENTROPY", "ASTER"))
    gpro = build_watch(tmp_path / "gpro", "GPRO", ("ENTROPY", "ASTER"))
    sndk.on_start()
    gpro.on_start()

    def coverage(*watches) -> set[tuple[str, str, str]]:
        """What main() accumulates from every node's strategies."""
        received: set[tuple[str, str, str]] = set()
        for watch in watches:
            received |= watch.received_leg_keys()
        return received

    try:
        every_leg = {
            ("SNDK", "HL", "xyz:SNDK-USD-PERP.HYPERLIQUID"),
            ("SNDK", "ENTROPY", "io:SNDK-USD-PERP.HYPERLIQUID"),
            ("SNDK", "ASTER", "SNDKUSD1-PERP.ASTER"),
            ("GPRO", "ENTROPY", "io:GPRO-USD-PERP.HYPERLIQUID"),
            ("GPRO", "ASTER", "GPROUSD1-PERP.ASTER"),
        }
        expected_missing = {  # what must still be missing once Aster alone has quoted
            ("SNDK", "HL", "xyz:SNDK-USD-PERP.HYPERLIQUID"),
            ("SNDK", "ENTROPY", "io:SNDK-USD-PERP.HYPERLIQUID"),
            ("GPRO", "ENTROPY", "io:GPRO-USD-PERP.HYPERLIQUID"),
        }
        missing = spread_watch.missing_leg_keys(plan, coverage(sndk, gpro))
        assert set(missing) == every_leg, "a fresh run has no covered leg at all"
        assert missing == sorted(missing), "the report is ordered"

        now = sndk.clock.timestamp_ns()
        for watch in (sndk, gpro):  # the two Aster legs alone are not a run
            aster = watch._legs[-1]
            watch.on_quote(make_quote(aster.instrument_id, "100.00", "100.01", now))
            watch.on_quote(make_quote(aster.instrument_id, "100.02", "100.03", now))
            watch.on_funding_rate(FundingRateUpdate(
                instrument_id=aster.instrument_id, rate=Decimal("0.0"),
                ts_event=now, ts_init=now, interval=480,
            ))
        missing = spread_watch.missing_leg_keys(plan, coverage(sndk, gpro))
        assert set(missing) == expected_missing, "the Aster book alone is not a run"

        for watch in (sndk, gpro):  # every leg of both symbols now has a quote
            for leg in watch._legs:
                watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01", now))
        received = coverage(sndk, gpro)
        assert spread_watch.missing_leg_keys(plan, received) == []
        assert received == every_leg, "SNDK x3 + GPRO x2, no phantom or double legs"
    finally:
        sndk.on_stop()
        gpro.on_stop()


# --------------------------------------------------------- analyser over real output


def test_opportunities_reads_watcher_files_with_entropy_labels(tmp_path):
    """The existing analyser must digest this run's real files, labels included."""
    from types import SimpleNamespace

    from analysis import opportunities

    stamp = "20260907T120000Z"
    for symbol, venues in (("SNDK", ("HL", "ENTROPY", "ASTER")),
                           ("GPRO", ("ENTROPY", "ASTER"))):
        watch = build_watch(tmp_path, symbol, venues)
        watch.on_start()
        try:
            now = watch.clock.timestamp_ns()
            for leg in watch._legs:  # one quote each: real rows from production code
                watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01", now))
        finally:
            watch.on_stop()

    files = {found.symbol: found for found in opportunities.discover(tmp_path, stamp)}
    assert sorted(files) == ["GPRO", "SNDK"]
    assert files["SNDK"].venues == "HL-ENTROPY-ASTER"
    assert files["GPRO"].venues == "ENTROPY-ASTER"

    # Two legs, each 0.9 bps taker, plus the 5 bps reserve: the direction threshold.
    fees, note = opportunities.venue_fees("GPRO")
    assert note == ""
    assert set(fees) == {"ENTROPY", "ASTER"}
    assert fees["ENTROPY"] + fees["ASTER"] + opportunities.RESERVE_BPS == pytest.approx(6.8)
    sndk_fees, _ = opportunities.venue_fees("SNDK")  # every mapped venue, not just this run
    assert {"HL", "ENTROPY", "ASTER"} <= set(sndk_fees)
    assert sndk_fees["ENTROPY"] == pytest.approx(0.9), "ENTROPY must not default to free"

    data = opportunities.load_all(files["SNDK"].all, None, None)
    assert data.rows > 0
    assert ("ENTROPY", "ASTER") in data.series
    assert data.fee_seen[("ENTROPY", "ASTER")] == pytest.approx(6.8)

    args = SimpleNamespace(t_from=None, t_to=None, gap_s=2.0, hold_s=30.0, min_usd=1000.0)
    for symbol in ("SNDK", "GPRO"):
        text = "\n".join(opportunities.analyse(files[symbol], args))
        assert f"## {symbol}" in text
        assert "ENTROPY>" in text and ">ENTROPY" in text
        assert "fee mismatch" not in text, "INSTRUMENTS and the csv must agree"
        assert "no _all.csv samples in the window" not in text


# -------------------------------------------------------------------- startup errors


def test_fail_startup_stops_once_and_keeps_every_message():
    state = spread_watch.RunState()
    stops: list[int] = []
    state.stop_node = lambda: stops.append(1)
    state.fail_startup("a")
    state.fail_startup("a")
    assert state.startup_errors == ["a"]
    assert state.stop_requested
    assert stops == [1]
    state.fail_startup("b")
    assert state.startup_errors == ["a", "b"]
    assert stops == [1], "an already requested stop must not be asked twice"

    bare = spread_watch.RunState()  # stop_node is only wired up by main()
    bare.fail_startup("c")
    assert bare.startup_errors == ["c"]


def test_on_start_records_a_startup_error_for_a_missing_instrument(tmp_path):
    watch = build_watch(tmp_path, "SNDK", ("HL", "ENTROPY", "ASTER"))
    missing_id = InstrumentId.from_str("io:SNDK-USD-PERP.HYPERLIQUID")
    watch._stub_cache._ids.remove(missing_id)
    stops: list[int] = []
    watch._cfg.run_state.stop_node = lambda: stops.append(1)
    try:
        watch.on_start()
        assert watch._cfg.run_state.startup_errors == [
            "SNDK/ENTROPY: instrument not loaded: io:SNDK-USD-PERP.HYPERLIQUID",
        ]
        assert watch._cfg.run_state.stop_requested
        assert stops == [1], "the node is asked to stop once, through the run state"
        assert watch.stops == [1], "the strategy also stops itself"
        assert watch.subscribed["quotes"] == [], "nothing is subscribed after a miss"
    finally:
        watch.on_stop()


def _run_main_with_a_fake_node(monkeypatch, tmp_path, received, startup_errors):
    """Drive main() once through a mocked node; returns the SystemExit code."""
    import dotenv
    from unittest.mock import MagicMock, patch

    run_state = spread_watch.RunState()
    run_state.startup_errors = list(startup_errors)
    strategy = MagicMock()
    strategy.summary.return_value = "SUMMARY [fake]"
    strategy.received_leg_keys.return_value = set(received)
    node = MagicMock()
    node.handle.return_value.stop = lambda: None
    monkeypatch.setattr(spread_watch, "build_node",
                        lambda np: (node, [strategy], run_state))
    args = ["spread_watch.py", "--symbols", "SNDK,GPRO", "--venues", "HL,ENTROPY,ASTER",
            "--minutes", "1", "--max-restarts", "0", "--out", str(tmp_path / "out")]
    out, err = io.StringIO(), io.StringIO()
    with patch.object(sys, "argv", args), patch.object(dotenv, "load_dotenv"):
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                spread_watch.main()
            except SystemExit as exit_exc:
                return exit_exc.code, err.getvalue(), out.getvalue()
    return None, err.getvalue(), out.getvalue()


def test_main_exits_1_when_a_startup_error_was_recorded(monkeypatch, tmp_path):
    code, err, out = _run_main_with_a_fake_node(
        monkeypatch, tmp_path, [],
        ["SNDK/ENTROPY: instrument not loaded: io:SNDK-USD-PERP.HYPERLIQUID"],
    )
    assert code == 1
    assert "STARTUP FAILED SNDK/ENTROPY: instrument not loaded" in err
    assert "SUMMARY [fake]" in out


def test_main_exits_1_when_a_leg_never_received_data(monkeypatch, tmp_path):
    code, err, out = _run_main_with_a_fake_node(
        monkeypatch, tmp_path,
        [("SNDK", "HL", "xyz:SNDK-USD-PERP.HYPERLIQUID")],
        [],
    )
    assert code == 1
    assert "INCOMPLETE SNDK/ENTROPY: no top-of-book data for " in err
    assert "INCOMPLETE GPRO/ASTER: no top-of-book data for GPROUSD1-PERP.ASTER" in err


def test_reference_columns_and_node_stay_off_without_reference(tmp_path):
    """--reference none must not add a Futu feed or a reference csv, io: included."""
    with contextlib.redirect_stderr(io.StringIO()):
        plan = spread_watch.build_plan(["SNDK"], ["HL", "ENTROPY", "ASTER"])
    header = spread_watch.ref_header(plan["SNDK"])
    for venue in ("HL", "ENTROPY", "ASTER"):
        assert f"{venue}_bid" in header and f"{venue}_sell_edge_bps" in header
    node_plan = spread_watch.NodePlan(plan, tmp_path, "20260914T000000Z", 10, 1000)
    assert node_plan.ref_codes == {}, "no reference code may be resolved when it is off"


def test_main_reports_the_funding_channel_per_leg(tmp_path):
    watch = build_watch(tmp_path, "SNDK", ("HL", "ENTROPY", "ASTER"))
    watch.on_start()
    try:
        now = watch.clock.timestamp_ns()
        watch.on_funding_rate(FundingRateUpdate(
            instrument_id=watch._legs[1].instrument_id, rate=Decimal("0.0001"),
            ts_event=now, ts_init=now, interval=60,
        ))
        text = watch.summary()
    finally:
        watch.on_stop()
    assert "funding_seen=False" in text   # HL and Aster legs: no funding update yet
    assert "funding_seen=True" in text    # the io: leg got one
    assert text.count("funding_seen=") == 3


if __name__ == "__main__":
    unittest.main()

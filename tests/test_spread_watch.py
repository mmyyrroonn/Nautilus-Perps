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
from market_tape import read_manifest, read_tape, read_run_manifest  # noqa: E402
from nautilus_trader.common import TimeEvent  # noqa: E402
from nautilus_trader.core import UUID4  # noqa: E402
from nautilus_trader.model import AggressorSide  # noqa: E402
from nautilus_trader.model import BookAction  # noqa: E402
from nautilus_trader.model import BookOrder  # noqa: E402
from nautilus_trader.model import FundingRateUpdate  # noqa: E402
from nautilus_trader.model import InstrumentId  # noqa: E402
from nautilus_trader.model import InstrumentStatus  # noqa: E402
from nautilus_trader.model import MarketStatusAction  # noqa: E402
from nautilus_trader.model import OrderBookDelta  # noqa: E402
from nautilus_trader.model import OrderBookDeltas  # noqa: E402
from nautilus_trader.model import OrderBookDepth10  # noqa: E402
from nautilus_trader.model import OrderSide  # noqa: E402
from nautilus_trader.model import Price  # noqa: E402
from nautilus_trader.model import Quantity  # noqa: E402
from nautilus_trader.model import QuoteTick  # noqa: E402
from nautilus_trader.model import StrategyId  # noqa: E402
from nautilus_trader.model import TradeId  # noqa: E402
from nautilus_trader.model import TradeTick  # noqa: E402


# --------------------------------------------------------------------------------- fixtures


class FakeLevel:
    """One book level: only what ``spread_watch._levels_from_book`` reads."""

    def __init__(self, price: Decimal, size: Decimal) -> None:
        self.price = price
        self._size = size

    def size(self) -> Decimal:
        return self._size


class FakeBook:
    """Minimal OrderBook stand-in: bids() / asks() of FakeLevel objects."""

    def __init__(self, bids: list[FakeLevel], asks: list[FakeLevel]) -> None:
        self._bids = list(bids)
        self._asks = list(asks)

    def bids(self) -> list[FakeLevel]:
        return list(self._bids)

    def asks(self) -> list[FakeLevel]:
        return list(self._asks)


class FakeCache:
    """Stand-in for the Nautilus cache: every watched instrument is "loaded".

    ``instruments`` optionally maps an InstrumentId to the instrument object the
    cache returns for it (``object()`` otherwise); ``books`` does the same for
    ``order_book`` (``None`` otherwise).
    """

    def __init__(self, instrument_ids: tuple[InstrumentId, ...],
                 instruments: dict[InstrumentId, object] | None = None) -> None:
        self._ids = list(instrument_ids)
        self._instruments = dict(instruments or {})
        self.books: dict[InstrumentId, object] = {}

    def instrument_ids(self, venue) -> list[InstrumentId]:
        return [i for i in self._ids if i.venue == venue]

    def instrument(self, instrument_id: InstrumentId) -> object | None:
        if instrument_id not in self._ids:
            return None
        return self._instruments.get(instrument_id, object())

    def order_book(self, instrument_id: InstrumentId) -> object | None:
        return self.books.get(instrument_id)


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

    def __init__(self, config, instruments: dict[InstrumentId, object] | None = None) -> None:
        super().__init__(config)
        self._stub_cache = FakeCache(
            tuple(leg.instrument_id for leg in self._legs), instruments,
        )
        self._stub_log = FakeLog()
        self._stub_clock = FakeClock()
        self.subscribed: dict[str, list[InstrumentId]] = {
            "quotes": [], "funding": [], "trades": [], "deltas": [], "depth10": [],
            "status": [], "instrument": [],
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

    def subscribe_instrument_status(self, instrument_id, client_id=None, params=None) -> None:
        self.subscribed["status"].append(instrument_id)
        self._route("status", instrument_id, client_id)

    def subscribe_instrument(self, instrument_id, client_id=None, params=None) -> None:
        self.subscribed["instrument"].append(instrument_id)
        self._route("instrument", instrument_id, client_id)


def build_watch(out_dir: Path, symbol: str = "NVDA",
                venue_keys: tuple[str, ...] = ("HL", "LIGHTER_RH"),
                instruments: dict[InstrumentId, object] | None = None,
                record_l2: bool = False,
                stamp: str = "20260907T120000Z",
                coverage_limits: dict[str, int | None] | None = None) -> WatchUnderTest:
    """A started watcher for ``symbol`` writing its CSVs under ``out_dir``."""
    with contextlib.redirect_stderr(io.StringIO()):
        legs = spread_watch.build_plan([symbol], list(venue_keys))[symbol]
    stem = spread_watch.csv_stem(symbol, legs, stamp)
    config = spread_watch.SpreadWatchConfig(
        strategy_id=StrategyId.from_str(f"SPREAD-WATCH-TEST-{symbol}"),
        symbol=symbol,
        legs=legs,
        csv_path=out_dir / f"spread_{stem}.csv",
        all_csv_path=out_dir / f"spread_{stem}_all.csv",
        depth_csv_path=out_dir / f"depth_{stem}.csv",
        trades_csv_path=out_dir / f"trades_{stem}.csv",
        run_state=spread_watch.RunState(),
        record_l2=record_l2,
        tape_path=(out_dir / "l2" / f"l2_{stem}.jsonl") if record_l2 else None,
        run_id=stamp,
        coverage_limits=coverage_limits,
    )
    return WatchUnderTest(config, instruments)


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
    import socket
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
            patch.object(socket.socket, "connect",
                         side_effect=AssertionError("dry-run must not open a socket")), \
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


def _run_main_with_a_fake_node(monkeypatch, tmp_path, received, startup_errors,
                               symbols: str = "SNDK,GPRO", venues: str = "HL,ENTROPY,ASTER",
                               recording_errors: tuple[str, ...] = (),
                               record_l2: bool = False):
    """Drive main() once through a mocked node; returns the SystemExit code."""
    import dotenv
    from unittest.mock import MagicMock, patch

    run_state = spread_watch.RunState()
    run_state.startup_errors = list(startup_errors)
    run_state.recording_errors = list(recording_errors)
    strategy = MagicMock()
    strategy.summary.return_value = "SUMMARY [fake]"
    strategy.received_leg_keys.return_value = set(received)
    node = MagicMock()
    node.handle.return_value.stop = lambda: None
    monkeypatch.setattr(spread_watch, "build_node",
                        lambda np: (node, [strategy], run_state))
    args = ["spread_watch.py", "--symbols", symbols, "--venues", venues,
            "--minutes", "1", "--max-restarts", "0", "--out", str(tmp_path / "out")]
    if record_l2:
        args.append("--record-l2")
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


# The two ONDO + ASTER legs of NVDA, which is the plan's acceptance venue pair.
ONDO_ID = "NVDA-USD-PERP.ONDO"
TSLA_ONDO_ID = "TSLA-USD-PERP.ONDO"
NVDA_ONDO_RECEIVED = [("NVDA", "ONDO", ONDO_ID), ("NVDA", "ASTER", "NVDAUSDT-PERP.ASTER")]


def test_main_exits_1_when_a_recording_failed(monkeypatch, tmp_path):
    """Minor 5: a failed recording fails the *run's* exit status (plan 5.1/8).

    The spread run itself kept going - it is only the recording's acceptance that failed -
    but an orchestrator keyed on the exit code must not read it as a passing run.
    """
    code, err, out = _run_main_with_a_fake_node(
        monkeypatch, tmp_path, NVDA_ONDO_RECEIVED, [],
        symbols="NVDA", venues="ONDO,ASTER",
        recording_errors=[
            "NVDA: the L2 tape closed with 3 dropped record(s) in 1 gap(s): this "
            "recording is incomplete and must not be replayed across a gap",
        ],
    )
    assert code == 1, "a recording failure must be visible in the exit status"
    assert "RECORDING FAILED NVDA: the L2 tape closed with 3 dropped record(s)" in err
    assert "SUMMARY [fake]" in out, "the spread run still reported normally"


def test_main_exits_1_when_record_l2_wrote_no_tape(monkeypatch, tmp_path):
    """The other recording failure: --record-l2 was asked for and nothing was recorded."""
    code, err, _out = _run_main_with_a_fake_node(
        monkeypatch, tmp_path, NVDA_ONDO_RECEIVED, [],
        symbols="NVDA", venues="ONDO,ASTER", record_l2=True,
    )
    assert code == 1
    assert "RECORDING FAILED no tape fragment was written under " in err


def test_main_does_not_fail_a_run_that_recorded_cleanly(monkeypatch, tmp_path):
    code, err, _out = _run_main_with_a_fake_node(
        monkeypatch, tmp_path, NVDA_ONDO_RECEIVED, [], symbols="NVDA", venues="ONDO,ASTER",
    )
    assert code is None, "a clean run returns normally"
    assert err == ""


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


# ------------------------------------------------------------------------ ondo venue


def make_status(instrument_id: InstrumentId, reason: str, *, is_trading=None,
                is_quoting=None, action=MarketStatusAction.NONE, ts: int = 1_000):
    """One InstrumentStatus as the adapter publishes it (plan 4.2)."""
    return InstrumentStatus(
        instrument_id=instrument_id,
        action=action,
        reason=reason,
        is_quoting=is_quoting,
        is_trading=is_trading,
        ts_event=ts,
        ts_init=ts,
    )


class FakeInstrument:
    """A loaded instrument carrying the provider's runtime fee metadata.

    ``size_increment`` / ``price_increment`` are Nautilus ``Quantity`` / ``Price`` objects
    on a real instrument; whatever they render as is what the tape must carry, so a test
    can hand in an exact decimal string just as well. ``info`` is the adapter's metadata
    carrier (version / availability) and ``ts_init`` the local receive time.
    """

    def __init__(self, taker_fee, *, instrument_id=None, size_increment=None,
                 price_increment=None, info=None, ts_init=None, price_precision=None,
                 size_precision=None, tick_size=None) -> None:
        self.taker_fee = taker_fee
        if instrument_id is not None:
            self.instrument_id = instrument_id
        if size_increment is not None:
            self.size_increment = size_increment
        if price_increment is not None:
            self.price_increment = price_increment
        if info is not None:
            self.info = info
        if ts_init is not None:
            self.ts_init = ts_init
        if price_precision is not None:
            self.price_precision = price_precision
        if size_precision is not None:
            self.size_precision = size_precision
        if tick_size is not None:
            self.tick_size = tick_size


class TestOndoVenueRegistry(unittest.TestCase):
    """ONDO is a known venue with exactly the two NVDA/TSLA mappings (plan 4.1)."""

    def test_registered_on_the_ondo_venue(self) -> None:
        spec = spread_watch.VENUES["ONDO"]
        self.assertEqual(spec.key, "ONDO")
        self.assertEqual(spec.venue, "ONDO")
        self.assertIn("ONDO", spread_watch.ALL_VENUES)

    def test_only_nvda_and_tsla_are_mapped(self) -> None:
        mapped = sorted(s for s, m in spread_watch.INSTRUMENTS.items() if "ONDO" in m)
        self.assertEqual(mapped, ["NVDA", "TSLA"])
        self.assertEqual(spread_watch.INSTRUMENTS["NVDA"]["ONDO"],
                         (ONDO_ID, spread_watch.ONDO_TAKER_FEE_BPS))
        self.assertEqual(spread_watch.INSTRUMENTS["TSLA"]["ONDO"],
                         (TSLA_ONDO_ID, spread_watch.ONDO_TAKER_FEE_BPS))

    def test_registry_fee_is_the_labelled_documentation_assumption(self) -> None:
        # plan 5.2: 2.5 bps is a dated assumption for --dry-run / old csv only.
        self.assertEqual(spread_watch.ONDO_TAKER_FEE_BPS, 2.5)
        self.assertIn("2026-09-14", spread_watch.ONDO_FEE_SOURCE)

    def test_stays_out_of_the_defaults(self) -> None:
        self.assertNotIn("ONDO", spread_watch.DEFAULT_VENUES)
        self.assertNotIn("ONDO", spread_watch.DEFAULT_PAIR)
        self.assertEqual(spread_watch.DEFAULT_VENUES, ("HL", "LIGHTER", "ASTER"))
        self.assertEqual(spread_watch.DEFAULT_PAIR, ("HL", "LIGHTER"))

    def test_no_depth10_fallback_is_claimed_for_p1(self) -> None:
        # plan 4.2: on-demand depth10 is not implemented in P1 -> registry says so.
        self.assertFalse(spread_watch.VENUES["ONDO"].supports_depth10)

    def test_the_ondo_leg_is_two_venues_off_the_aster_leg(self) -> None:
        with contextlib.redirect_stderr(io.StringIO()):
            plan = spread_watch.build_plan(["NVDA", "TSLA"], ["ONDO", "ASTER"])
        self.assertEqual([leg.venue_key for leg in plan["NVDA"]], ["ONDO", "ASTER"])
        self.assertEqual(plan["NVDA"][0].instrument_id, ONDO_ID)
        self.assertEqual(plan["NVDA"][0].client_id, "ONDO")
        self.assertEqual(plan["TSLA"][0].instrument_id, TSLA_ONDO_ID)
        self.assertEqual(plan["NVDA"][1].instrument_id, "NVDAUSDT-PERP.ASTER")


def test_hl_and_entropy_share_one_client_while_ondo_and_aster_are_two():
    """The Ondo client count must not disturb the existing HYPERLIQUID sharing."""
    with contextlib.redirect_stderr(io.StringIO()):
        plan = spread_watch.build_plan(["SNDK", "NVDA"], ["HL", "ENTROPY", "ONDO", "ASTER"])
    groups = {g.client_id: g for g in spread_watch.build_client_groups(plan)}
    assert set(groups) == {"HYPERLIQUID", "ONDO", "ASTER"}
    assert groups["HYPERLIQUID"].instrument_ids == [
        "xyz:SNDK-USD-PERP.HYPERLIQUID",
        "io:SNDK-USD-PERP.HYPERLIQUID",
        "xyz:NVDA-USD-PERP.HYPERLIQUID",
    ]
    assert groups["ONDO"].instrument_ids == [ONDO_ID]
    assert groups["ASTER"].instrument_ids == [
        "SNDKUSD1-PERP.ASTER", "NVDAUSDT-PERP.ASTER",
    ]


def test_ondo_client_is_a_production_config_with_instrument_id_load_ids():
    """`load_ids` takes InstrumentId objects (Stage 3a caveat) built from the plan."""
    import importlib

    try:
        importlib.import_module("nautilus_trader.adapters.ondo")
    except ImportError:
        pytest.skip("the candidate ondo wheel is not installed in this venv")
    factory, config = spread_watch._ondo_client([ONDO_ID, TSLA_ONDO_ID])
    assert factory.name() == "ONDO"
    assert [str(i) for i in config.load_ids] == [ONDO_ID, TSLA_ONDO_ID]


def test_ondo_client_reports_adapter_missing_with_a_build_pointer(monkeypatch):
    """Without the wheel the factory raises SystemExit, never a bare ImportError."""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.startswith("nautilus_trader.adapters.ondo"):
            raise ImportError("simulated: this venv has no ondo adapter")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(SystemExit) as caught:
        spread_watch._ondo_client([ONDO_ID])
    assert str(caught.value) == spread_watch._adapter_missing(
        "ONDO", "nautilus_trader.adapters.ondo",
    )


def test_dry_run_lists_ondo_and_aster_clients_without_side_effects(tmp_path, monkeypatch):
    """`--symbols NVDA,TSLA --venues ONDO,ASTER --dry-run` is plan 8's offline command."""
    result, err = _run_dry_run(
        ["--symbols", "NVDA,TSLA", "--venues", "ONDO,ASTER", "--dry-run"],
        monkeypatch, tmp_path,
    )
    assert result["mode"] == "read-only"
    assert [x["client_id"] for x in result["data_clients"]] == ["ONDO", "ASTER"]
    assert result["data_clients"][0]["instrument_ids"] == [ONDO_ID, TSLA_ONDO_ID]
    assert [leg["venue_key"] for leg in result["symbols"]["NVDA"]] == ["ONDO", "ASTER"]
    assert [leg["instrument_id"] for leg in result["symbols"]["NVDA"]] == [
        ONDO_ID, "NVDAUSDT-PERP.ASTER",
    ]
    assert err == ""


def test_the_watcher_never_builds_an_execution_client():
    """P1 is a read-only data watcher: no exec client, no add_exec_client."""
    source = (Path(__file__).resolve().parents[1] / "src" / "spread_watch.py").read_text(
        encoding="utf-8",
    )
    assert "add_exec_client" not in source
    assert "ExecutionClient" not in source


# ------------------------------------------------------------------ ondo fee source


def test_metadata_fee_is_read_as_a_rate_through_decimal():
    assert spread_watch.ondo_metadata_fee_bps(FakeInstrument(Decimal("0.00025"))) == 2.5
    assert spread_watch.ondo_metadata_fee_bps(FakeInstrument("0.0001")) == 1.0
    assert spread_watch.ondo_metadata_fee_bps(FakeInstrument(Decimal("0.00007"))) == 0.7


def test_metadata_fee_is_unknown_rather_than_free():
    """A zero/absent/negative rate is "not published", never a free Ondo leg."""
    for instrument in (object(), FakeInstrument(None), FakeInstrument(Decimal("0")),
                       FakeInstrument("0.0"), FakeInstrument(Decimal("-0.001")),
                       FakeInstrument("not a number")):
        assert spread_watch.ondo_metadata_fee_bps(instrument) is None, instrument


def test_ondo_leg_takes_its_fee_from_the_runtime_metadata(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), instruments={
        InstrumentId.from_str(ONDO_ID): FakeInstrument(Decimal("0.00025")),
    })
    watch.on_start()
    try:
        ondo = watch._legs[0]
        assert ondo.spec.taker_fee_bps == pytest.approx(2.5)
        assert ondo.fee_source == "instrument_metadata"
        assert watch._fee[("ONDO", "ASTER")] == pytest.approx(2.5 + 0.9 + 5.0)
        assert watch.subscribed["status"] == [watch._legs[0].instrument_id]
        assert watch.subscribed["instrument"] == [watch._legs[0].instrument_id], (
            "a runtime metadata refresh only reaches on_instrument if it is subscribed"
        )
    finally:
        watch.on_stop()


def test_ondo_leg_without_a_published_fee_does_not_use_the_2_5_assumption(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch.on_start()
    try:
        ondo = watch._legs[0]
        assert ondo.fee_source == "missing"
        assert ondo.spec.taker_fee_bps is None, "the 2.5 assumption must not survive"
        assert watch._fee[("ONDO", "ASTER")] is None
        assert watch._fee[("ASTER", "ONDO")] is None

        now = watch.clock.timestamp_ns()  # quotes alone cannot make a cost judgement
        for leg in watch._legs:
            watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01", now))
        assert watch._hits.rows == 0
        assert watch._all.rows == 0
        watch._log_status()  # the status line and the summary must survive a None fee
        text = watch.summary()
        assert "fee" in text.lower()
    finally:
        watch.on_stop()


def test_ondo_leg_warns_once_about_the_missing_fee(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch.on_start()
    try:
        warnings = [msg for level, msg in watch.log.lines if level == "WARNING"]
        fee_warnings = [m for m in warnings if "ONDO" in m and "fee" in m]
        assert fee_warnings, warnings
        assert "2.5" not in fee_warnings[0]
        assert "unknown" in fee_warnings[0]
    finally:
        watch.on_stop()


def test_missing_ondo_instrument_fails_startup_without_subscribing(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch._stub_cache._ids.remove(InstrumentId.from_str(ONDO_ID))
    stops: list[int] = []
    watch._cfg.run_state.stop_node = lambda: stops.append(1)
    try:
        watch.on_start()
        assert watch._cfg.run_state.startup_errors == [
            f"NVDA/ONDO: instrument not loaded: {ONDO_ID}",
        ]
        assert watch._cfg.run_state.stop_requested
        assert stops == [1]
        assert watch.subscribed["quotes"] == []
        assert watch.subscribed["status"] == []
    finally:
        watch.on_stop()


def test_main_exits_1_when_the_ondo_leg_never_received_a_bbo(monkeypatch, tmp_path):
    code, err, _out = _run_main_with_a_fake_node(
        monkeypatch, tmp_path, [], [], symbols="NVDA,TSLA", venues="ONDO,ASTER",
    )
    assert code == 1
    assert f"INCOMPLETE NVDA/ONDO: no top-of-book data for {ONDO_ID}" in err
    assert f"INCOMPLETE TSLA/ONDO: no top-of-book data for {TSLA_ONDO_ID}" in err


# ------------------------------------------------------- ondo feed / market status


def test_ondo_disconnect_invalidates_the_quote_and_the_book_at_once(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch.on_start()
    try:
        ondo, aster = watch._legs
        now = watch.clock.timestamp_ns()
        watch.on_quote(make_quote(ondo.instrument_id, "100.00", "100.01", now))
        watch.on_quote(make_quote(aster.instrument_id, "100.00", "100.02", now))
        assert ondo.ready() and aster.ready()
        # The book is present in the cache: losing the feed must make it unusable.
        watch.cache.books[ondo.instrument_id] = FakeBook(
            [FakeLevel(Decimal("100.00"), Decimal("1"))],
            [FakeLevel(Decimal("100.01"), Decimal("2"))],
        )
        assert watch._book_levels(ondo)[0], "the book is readable before the disconnect"

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:disconnected", is_trading=None, is_quoting=False,
        ))
        # Same event-loop turn, no waiting for MAX_AGE_MS:
        assert ondo.bid == 0.0 and ondo.ask == 0.0 and ondo.ts_ns == 0
        assert not ondo.feed_ready and not ondo.market_ready
        assert not ondo.book_valid
        assert watch._book_levels(ondo) == ([], []), "the cached book is unusable"
        assert not ondo.ready()
        assert aster.ready(), "a non-ONDO leg keeps its old behaviour"
        assert aster.feed_ready and aster.market_ready
    finally:
        watch.on_stop()


def test_ondo_readiness_needs_a_new_snapshot_not_a_socket_or_a_quote(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch.on_start()
    try:
        ondo = watch._legs[0]
        now = watch.clock.timestamp_ns()
        watch.on_quote(make_quote(ondo.instrument_id, "100.00", "100.01", now))
        assert ondo.ready()

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:disconnected", is_trading=None, is_quoting=False,
        ))
        assert not ondo.ready()
        # "socket connected" alone is not recovery.
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:connected", is_trading=None, is_quoting=True,
        ))
        assert not ondo.feed_ready and not ondo.ready()
        # A brand-new quote is still not enough: the snapshot is what is missing.
        watch.on_quote(make_quote(ondo.instrument_id, "100.00", "100.01",
                                  watch.clock.timestamp_ns()))
        assert ondo.bid > 0.0 and ondo.ask > 0.0
        assert not ondo.ready(), "a new snapshot is required for readiness"

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:snapshot_ready", is_trading=True, is_quoting=True,
        ))
        assert ondo.feed_ready and ondo.market_ready and ondo.book_valid
        assert ondo.ready(), "the new snapshot plus the fresh quote is a usable leg"
    finally:
        watch.on_stop()


def test_ondo_real_market_status_is_a_separate_axis(tmp_path):
    """A real venue status (not an adapter:* reason) drives market_ready on its own."""
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch.on_start()
    try:
        ondo = watch._legs[0]
        watch.on_quote(make_quote(ondo.instrument_id, "100.00", "100.01",
                                  watch.clock.timestamp_ns()))
        assert ondo.ready()

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "market halts", is_trading=False,
            action=MarketStatusAction.HALT,
        ))
        assert not ondo.market_ready and ondo.feed_ready
        assert not ondo.ready(), "a halted market is not tradable"
        assert ondo.bid > 0.0, "a halt does not clear the quote, it invalidates it"

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "regular trading hours", is_trading=True,
            action=MarketStatusAction.TRADING,
        ))
        assert ondo.market_ready and ondo.ready()
    finally:
        watch.on_stop()


def test_non_ondo_legs_never_subscribe_or_react_to_status(tmp_path):
    watch = build_watch(tmp_path, "SNDK", ("HL", "ENTROPY", "ASTER"))
    watch.on_start()
    try:
        assert watch.subscribed["status"] == []
        assert watch.subscribed["instrument"] == []
        for leg in watch._legs:  # a stray status event must not change a non-ONDO leg
            watch.on_instrument_status(make_status(
                leg.instrument_id, "adapter:disconnected", is_trading=False,
            ))
            assert leg.feed_ready and leg.market_ready and leg.book_valid
    finally:
        watch.on_stop()


def test_a_real_halt_survives_a_local_reconnect(tmp_path):
    """A venue halt is not cleared by a local feed snapshot: the axes stay apart."""
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch.on_start()
    try:
        ondo = watch._legs[0]
        watch.on_quote(make_quote(ondo.instrument_id, "100.00", "100.01",
                                  watch.clock.timestamp_ns()))
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "market halts", is_trading=False,
            action=MarketStatusAction.HALT,
        ))
        assert not ondo.ready() and ondo.market_halted

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:disconnected", is_trading=None, is_quoting=False,
        ))
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:snapshot_ready", is_trading=None, is_quoting=True,
        ))
        assert ondo.feed_ready and ondo.book_valid, "the local feed is back"
        assert not ondo.market_ready and not ondo.ready(), (
            "a snapshot must not clear a halt the venue announced itself"
        )

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "regular trading hours", is_trading=True,
            action=MarketStatusAction.TRADING,
        ))
        assert ondo.market_ready and not ondo.market_halted
    finally:
        watch.on_stop()


def test_the_metadata_axis_is_its_own_and_stale_withdraws_the_fee(tmp_path):
    """F07: feed / market / metadata are three axes; only their own event moves each.

    `metadata_stale` says the venue metadata is no longer acceptable, so the fee this leg's
    cost judgement came from is withdrawn - the direction is withheld rather than priced
    against a rate nobody trusts. It does not clear the quote, it does not touch the feed
    and it is not a venue halt.
    """
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True, instruments={
        InstrumentId.from_str(ONDO_ID): FakeInstrument(Decimal("0.00025")),
    })
    watch.on_start()
    try:
        ondo = watch._legs[0]
        watch.on_quote(make_quote(ondo.instrument_id, "100.00", "100.01",
                                  watch.clock.timestamp_ns()))
        assert ondo.ready() and ondo.fee_source == "instrument_metadata"
        assert watch._fee[("ONDO", "ASTER")] == pytest.approx(2.5 + 0.9 + 5.0)

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:metadata_stale", is_trading=None, is_quoting=False,
        ))
        assert not ondo.metadata_ready and ondo.metadata_stale_reason == "adapter:metadata_stale"
        assert ondo.feed_ready and ondo.book_valid, "the feed axis is not the metadata axis"
        assert ondo.market_ready, "stale metadata is not a venue halt"
        assert ondo.bid > 0.0, "the quote is not cleared by a metadata notice"
        assert not ondo.ready(), "a leg whose metadata is untrusted is not tradable"
        assert ondo.unusable_reasons() == ["metadata-stale"]
        assert ondo.spec.taker_fee_bps is None and ondo.fee_source == "metadata_stale"
        assert watch._fee[("ONDO", "ASTER")] is None
        assert watch._fee[("ASTER", "ONDO")] is None
        assert "metadata=STALE" in watch._leg_status_text(ondo)
    finally:
        watch.on_stop()

    records = [
        r for r in read_tape([tmp_path / "l2"])
        if r["event_kind"] == "instrument" and r["venue"] == "ONDO"
    ]
    assert [r["source"] for r in records] == ["instrument_metadata", "instrument_update"]
    stale = records[-1]
    assert stale["valid"] is False and stale["invalid_reason"] == "metadata_stale"
    assert stale["metadata"]["taker_fee_bps"] == "2.50", (
        "the last known payload is kept for traceability, with its fee, and marked unusable"
    )


def test_metadata_ready_restores_the_axis_without_touching_feed_or_market(tmp_path):
    """F07: only `metadata_ready` clears stale metadata, and it moves nothing else."""
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), instruments={
        InstrumentId.from_str(ONDO_ID): FakeInstrument(
            Decimal("0.00030"), info={"metadata_version": "9"},
        ),
    })
    watch.on_start()
    try:
        ondo = watch._legs[0]
        watch.on_quote(make_quote(ondo.instrument_id, "100.00", "100.01",
                                  watch.clock.timestamp_ns()))
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "market halts", is_trading=False,
            action=MarketStatusAction.HALT,
        ))
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:metadata_stale", is_trading=None, is_quoting=False,
        ))
        assert not ondo.metadata_ready and ondo.spec.taker_fee_bps is None

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:metadata_ready", is_trading=None, is_quoting=True,
        ))
        assert ondo.metadata_ready and ondo.metadata_stale_reason is None
        assert ondo.spec.taker_fee_bps == pytest.approx(3.0), (
            "the fee is re-read from the metadata in force now, not restored from memory"
        )
        assert ondo.metadata_version == "9"
        assert watch._fee[("ONDO", "ASTER")] == pytest.approx(3.0 + 0.9 + 5.0)
        assert not ondo.market_ready and ondo.market_halted, (
            "a metadata refresh is not a venue halt being lifted"
        )
        assert ondo.feed_ready and ondo.book_valid
        assert not ondo.ready(), "the halt still stands"
    finally:
        watch.on_stop()


def test_a_recovery_snapshot_does_not_clear_stale_metadata(tmp_path):
    """F07: the axes stay apart across a reconnect - neither clears the other."""
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), instruments={
        InstrumentId.from_str(ONDO_ID): FakeInstrument(Decimal("0.00025")),
    })
    watch.on_start()
    try:
        ondo = watch._legs[0]
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:metadata_stale", is_trading=None, is_quoting=False,
        ))
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:disconnected", is_trading=None, is_quoting=False,
        ))
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:snapshot_ready", is_trading=None, is_quoting=True,
        ))
        assert ondo.feed_ready and ondo.book_valid, "the local feed is back"
        assert not ondo.metadata_ready, (
            "a new snapshot says nothing about whether the metadata is trustworthy"
        )
        assert ondo.spec.taker_fee_bps is None
    finally:
        watch.on_stop()


def test_an_instrument_update_while_stale_is_recorded_as_unusable(tmp_path):
    """F07: a payload that arrives while the metadata is stale is not silently adopted.

    The adapter only republishes on a successful refresh, but nothing has said the metadata
    is acceptable again until `metadata_ready` does. Until then the new payload is on the
    tape with the stale marker, the last *acceptable* payload stays the remembered one, and
    the leg stays unusable.
    """
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True, instruments={
        InstrumentId.from_str(ONDO_ID): FakeInstrument(Decimal("0.00025")),
    })
    watch.on_start()
    try:
        ondo = watch._legs[0]
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:metadata_stale", is_trading=None, is_quoting=False,
        ))
        watch.on_instrument(FakeInstrument(
            Decimal("0.00060"), instrument_id=InstrumentId.from_str(ONDO_ID),
            info={"metadata_version": "12"},
        ))
        assert not ondo.metadata_ready
        assert ondo.spec.taker_fee_bps is None, "a stale leg does not adopt a new fee"
        assert ondo.metadata_version is None, "nor the version of a payload it cannot use"
    finally:
        watch.on_stop()

    records = [
        r for r in read_tape([tmp_path / "l2"])
        if r["event_kind"] == "instrument" and r["venue"] == "ONDO"
    ]
    assert [(r["source"], r["valid"], r["invalid_reason"]) for r in records] == [
        ("instrument_metadata", True, None),
        ("instrument_update", False, "metadata_stale"),
        ("instrument_update", False, "metadata_stale"),
    ]
    assert records[1]["metadata"]["taker_fee_bps"] == "2.50", (
        "the stale marker republishes the last acceptable payload"
    )
    assert records[2]["metadata"]["metadata_version"] == "12", (
        "the unusable refresh is on the tape and identified"
    )
    assert records[2]["metadata"]["fee_source"] == "metadata_stale", (
        "…and it says the fee the leg prices against is withdrawn, not that this payload's "
        "fee became the leg's rate"
    )
    assert records[2]["metadata"]["taker_fee_bps"] == "unknown"


def test_a_runtime_instrument_update_lands_on_the_tape_and_moves_the_fee(tmp_path):
    """F07/F08: a refresh mid-run is an arrival on the tape, not a start-up-only fact."""
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True, instruments={
        InstrumentId.from_str(ONDO_ID): FakeInstrument(Decimal("0.00025")),
    })
    watch.on_start()
    try:
        ondo = watch._legs[0]
        assert watch._fee[("ONDO", "ASTER")] == pytest.approx(2.5 + 0.9 + 5.0)

        # The adapter republishes the instrument with a new fee, step and version.
        watch.on_instrument(FakeInstrument(
            Decimal("0.00060"), instrument_id=InstrumentId.from_str(ONDO_ID),
            size_increment=Quantity.from_str("0.003"),
            price_increment=Price.from_str("0.01"), ts_init=1_700_000_000_000_000_000,
            info={"metadata_version": "12"},
        ))
        assert ondo.spec.taker_fee_bps == pytest.approx(6.0)
        assert ondo.metadata_version == "12"
        assert watch._fee[("ONDO", "ASTER")] == pytest.approx(6.0 + 0.9 + 5.0)
        assert ondo.metadata_updates == 2
    finally:
        watch.on_stop()

    records = [
        r for r in read_tape([tmp_path / "l2"])
        if r["event_kind"] == "instrument" and r["venue"] == "ONDO"
    ]
    assert [r["source"] for r in records] == ["instrument_metadata", "instrument_update"]
    assert records[0]["metadata"]["taker_fee_bps"] == "2.50"
    assert records[1]["metadata"]["taker_fee_bps"] == "6.00"
    assert records[1]["metadata"]["size_increment"] == "0.003", (
        "the real quantity step travels with the update, never 10**-size_precision"
    )
    assert records[1]["metadata"]["metadata_version"] == "12"
    assert records[1]["metadata"]["metadata_available_ns"] == 1_700_000_000_000_000_000
    assert records[1]["valid"] is True and records[1]["invalid_reason"] is None


def test_a_missing_size_increment_is_unknown_and_never_derived_from_precision(tmp_path):
    """Contract E: no real `size_increment` means unknown - a precision is not a step.

    The venue's own instruments measured 0.002/0.003 while ``10**-size_precision`` says
    0.001; deriving one from the other is off by a factor of six on a real pair.
    """
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True, instruments={
        InstrumentId.from_str(ONDO_ID): FakeInstrument(
            Decimal("0.00025"), price_precision=2, size_precision=3,  # a precision, no step
        ),
    })
    watch.on_start()
    watch.on_stop()
    records = [
        r for r in read_tape([tmp_path / "l2"])
        if r["event_kind"] == "instrument" and r["venue"] == "ONDO"
    ]
    assert records[0]["metadata"]["size_increment"] is None
    assert records[0]["metadata"]["price_increment"] is None
    assert records[0]["metadata"]["size_precision"] == 3, "the precision is still recorded"
    assert records[0]["metadata"]["metadata_available_ns"] is None, (
        "an instrument that stamps no receive time leaves the availability unknown"
    )


def test_the_first_book_after_a_recovery_snapshot_is_valid_on_the_tape(tmp_path):
    """F18: the replacement snapshot arrives *before* the ready notice and is good depth.

    The adapter publishes the new Deltas and only then `adapter:snapshot_ready`, so the
    first book of the recovery lands while `feed_ready` is still False. Folding that feed
    state into the record's `valid` wrote the one usable book of the recovery as invalid,
    and a replay held an unusable book until a second frame arrived.
    """
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True)
    watch.on_start()
    try:
        ondo = watch._legs[0]
        now = watch.clock.timestamp_ns()
        watch.on_book_deltas(make_batch(
            ondo.instrument_id, bids=[("100.05", "0.001")], asks=[("100.10", "0.002")],
            ts=now,
        ))
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:disconnected", is_trading=None, is_quoting=False,
        ))
        # One recovery frame, then the ready notice - the adapter's own order.
        watch.on_book_deltas(make_batch(
            ondo.instrument_id, bids=[("100.06", "0.003")], asks=[("100.11", "0.004")],
            ts=now + 1,
        ))
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:snapshot_ready", is_trading=None, is_quoting=True,
        ))
        assert ondo.feed_ready and ondo.book_valid
    finally:
        watch.on_stop()

    books = [r for r in read_tape([tmp_path / "l2"]) if r["event_kind"] == "book"]
    assert len(books) == 2, "one record per fully applied batch"
    recovery = books[1]
    assert recovery["bids"] == [["100.06", "0.003"]]
    assert recovery["valid"] is True, (
        "the replacement snapshot is good depth: the feed state belongs to the replay, "
        "not to this record's own verdict"
    )
    assert recovery["invalid_reason"] is None


def test_a_book_record_never_carries_the_feed_state_as_its_validity(tmp_path):
    """Contract D: feed / market / metadata eligibility is replay state, never a record flag.

    A two-sided uncrossed book that arrives while the feed is marked down, and while the
    venue says the market is halted, is still a perfectly good snapshot: what makes it
    *unusable* is the state of the axes at that arrival, and that is what a replay reads
    the status records for. Writing ``feed-invalidated`` into the record instead put the
    same fact in two places and made one snapshot's verdict depend on an unrelated event.
    """
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True)
    watch.on_start()
    try:
        ondo = watch._legs[0]
        now = watch.clock.timestamp_ns()
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:disconnected", is_trading=None, is_quoting=False,
        ))
        watch.on_instrument_status(make_status(
            ondo.instrument_id, "market halts", is_trading=False,
            action=MarketStatusAction.HALT,
        ))
        watch.on_book_deltas(make_batch(
            ondo.instrument_id, bids=[("100.05", "0.001")], asks=[("100.10", "0.002")],
            ts=now,
        ))
        watch.on_book_depth(make_depth10(
            ondo.instrument_id, bids=[("100.05", "0.001")], asks=[("100.10", "0.002")],
            ts=now,
        ))
    finally:
        watch.on_stop()

    rows = list(read_tape([tmp_path / "l2"]))
    assert "feed-invalidated" not in json.dumps(rows), (
        "the feed's state is replayed from the status records, it is not a record's verdict"
    )
    books = [r for r in rows if r["event_kind"] == "book"]
    assert len(books) == 2
    for record in books:
        assert record["valid"] is True and record["invalid_reason"] is None, (
            "a two-sided uncrossed book is valid depth whatever the axes say"
        )
    statuses = [r for r in rows if r["event_kind"] == "status"]
    assert [r["reason"] for r in statuses] == ["adapter:disconnected", "market halts"]
    assert all(r["valid"] is True and r["invalid_reason"] is None for r in statuses), (
        "a status notice's own validity is not the feed's state either"
    )


def test_the_depth_timer_drains_the_tape_without_new_market_data(tmp_path):
    """F10: the watcher drives the writer's deadline from the event loop it already runs."""
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True)
    watch.on_start()
    tape = watch._tape
    try:
        assert tape is not None
        clock = [tape._last_flush]  # a hand-cranked monotonic clock for the deadline
        tape._clock = lambda: clock[0]
        tape.flush_secs = 1.0
        now = watch.clock.timestamp_ns()
        for leg in watch._legs:
            watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01", now))
        path = tmp_path / "l2" / "l2_NVDA_ONDO-ASTER_20260907T120000Z.jsonl"
        on_disk = lambda: [  # noqa: E731 - the header is written directly, data is queued
            r for r in read_tape([path]) if r["event_kind"] in ("quote", "book")
        ]
        assert on_disk() == [], "the records are queued, not on the disk"

        clock[0] += 2.0
        watch.on_time_event(TimeEvent("depth-NVDA", UUID4(), now, now))
        assert [r["venue"] for r in on_disk()] == ["ONDO", "ASTER"], (
            "no market data arrived, and the deadline still had to be met"
        )
    finally:
        watch.on_stop()


def test_the_summary_names_the_legs_that_are_not_usable(tmp_path):
    """plan 8 judges a run leg by leg: 'no valid two-sided book' must be visible."""
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch.on_start()
    try:
        for leg in watch._legs:  # both legs quoted, so neither is unusable yet
            watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01",
                                      watch.clock.timestamp_ns()))
        ondo = watch._legs[0]
        assert ondo.unusable_reasons() == []
        assert "not usable at the end" not in watch.summary()

        watch.on_instrument_status(make_status(
            ondo.instrument_id, "adapter:disconnected", is_trading=None, is_quoting=False,
        ))
        assert ondo.unusable_reasons() == [
            "local-feed-disconnected", "market-not-trading", "book-invalidated",
            "no-two-sided-bbo",
        ]
        text = watch.summary()
        assert "legs not usable at the end of this run: ONDO(" in text
        # The other leg still has its quote and is not listed.
        assert "ASTER(" not in text
        assert "feed_ready=False market_ready=False book_valid=False disconnects=1" in text
    finally:
        watch.on_stop()


# ------------------------------------------------------------------ csv compatibility


def test_every_csv_header_and_its_order_are_unchanged():
    assert spread_watch.SPREAD_HEADER == [
        "ts_utc", "sell_venue", "buy_venue", "gross_bps", "net_bps",
        "sell_bid", "sell_bid_size", "buy_ask", "buy_ask_size",
        "sell_ask", "buy_bid", "funding_sell", "funding_buy",
        "age_sell_ms", "age_buy_ms",
    ]
    assert spread_watch.TRADES_HEADER == [
        "ts_utc", "venue", "price", "size", "aggressor_side", "trade_id",
        "ts_event_ns", "ts_init_ns",
    ]
    assert spread_watch.DEPTH_HEADER == [
        "ts_utc", "venue", "bid", "ask", "mid", "levels_bid", "levels_ask",
        "bid_usd_2bps", "bid_usd_5bps", "bid_usd_10bps",
        "ask_usd_2bps", "ask_usd_5bps", "ask_usd_10bps",
    ]
    assert spread_watch.REF_HEAD == [
        "ts_utc", "event", "ref_ts_src_utc", "ref_last", "ref_bid", "ref_ask",
        "ref_mid", "ref_age_ms", "ref_src_to_srv_ms", "ref_book_mode",
    ]


def test_a_real_ondo_run_writes_the_same_columns_as_before(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch.on_start()
    try:
        now = watch.clock.timestamp_ns()
        for leg in watch._legs:
            watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01", now))
            watch.on_trade(make_trade(leg.instrument_id, "100.00", "1.0",
                                      AggressorSide.BUY, "T", now))
    finally:
        watch.on_stop()
    spread_rows = read_csv(tmp_path / "spread_NVDA_ONDO-ASTER_20260907T120000Z_all.csv")
    depth_rows = read_csv(tmp_path / "depth_NVDA_ONDO-ASTER_20260907T120000Z.csv")
    trade_rows = read_csv(tmp_path / "trades_NVDA_ONDO-ASTER_20260907T120000Z.csv")
    assert spread_rows[0] == spread_watch.SPREAD_HEADER
    assert depth_rows[0] == spread_watch.DEPTH_HEADER
    assert trade_rows[0] == spread_watch.TRADES_HEADER
    assert {row[1] for row in spread_rows[1:]} <= {"ONDO", "ASTER"}


# ----------------------------------------------------------------- l2 tape (plan 5.2)


def make_batch(instrument_id: InstrumentId, *, bids, asks, clear=True,
               ts: int = 1_000) -> OrderBookDeltas:
    """A real Nautilus deltas message: a CLEAR plus the ADDs of one book state."""
    deltas = []
    if clear:
        deltas.append(OrderBookDelta.clear(instrument_id, 0, ts, ts + 1))
    for price, size in bids:
        deltas.append(OrderBookDelta(
            instrument_id, BookAction.ADD,
            BookOrder(OrderSide.BUY, Price.from_str(price), Quantity.from_str(size), 0),
            0, 0, ts, ts + 1,
        ))
    for price, size in asks:
        deltas.append(OrderBookDelta(
            instrument_id, BookAction.ADD,
            BookOrder(OrderSide.SELL, Price.from_str(price), Quantity.from_str(size), 0),
            0, 0, ts, ts + 1,
        ))
    return OrderBookDeltas(instrument_id, deltas)


def make_depth10(instrument_id: InstrumentId, *, bids, asks, ts: int = 1_000
                 ) -> OrderBookDepth10:
    """A depth10 snapshot: Nautilus wants exactly ten levels per side."""

    def side(levels, order_side):
        orders = [
            BookOrder(order_side, Price.from_str(price), Quantity.from_str(size), 0)
            for price, size in levels
        ]
        while len(orders) < 10:  # an empty depth slot: size 0
            orders.append(BookOrder(order_side, Price.from_str(levels[-1][0]),
                                    Quantity.from_str("0"), 0))
        return orders

    bid_orders, ask_orders = side(bids, OrderSide.BUY), side(asks, OrderSide.SELL)
    return OrderBookDepth10(
        instrument_id, bid_orders, ask_orders, [len(bids)] + [0] * 9,
        [len(asks)] + [0] * 9, 0, 0, ts, ts + 1,
    )


def test_record_l2_is_off_by_default_and_on_with_the_switch(tmp_path, monkeypatch):
    """`--record-l2` is explicit and default off (plan 5.2)."""
    off, _err = _run_dry_run(
        ["--symbols", "NVDA", "--venues", "ONDO,ASTER", "--dry-run"], monkeypatch, tmp_path,
    )
    assert off["record_l2"] is False
    on, _err = _run_dry_run(
        ["--symbols", "NVDA", "--venues", "ONDO,ASTER", "--dry-run", "--record-l2"],
        monkeypatch, tmp_path,
    )
    assert on["record_l2"] is True
    assert "l2" in on["record_l2_note"]
    assert "raw_ondo" in on["record_l2_note"]


def test_without_record_l2_no_tape_is_opened_or_written(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"))
    watch.on_start()
    try:
        now = watch.clock.timestamp_ns()
        for leg in watch._legs:
            watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01", now))
            watch.on_book_deltas(make_batch(
                leg.instrument_id, bids=[("100.05", "0.001")], asks=[("100.10", "0.002")],
            ))
        assert watch._tape is None
        assert all(leg.book_tape is None for leg in watch._legs)
    finally:
        watch.on_stop()
    assert not (tmp_path / "l2").exists(), "the switch off means no tape at all"


def test_a_record_l2_run_records_every_leg_with_a_manifest(tmp_path):
    """Every observed leg gets complete L2 - never only Ondo with a top-of-book."""
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True)
    watch.on_start()
    try:
        now = watch.clock.timestamp_ns()
        for leg in watch._legs:
            watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01", now))
            watch.on_book_deltas(make_batch(
                leg.instrument_id,
                bids=[("100.05", "0.001"), ("100.04", "0.002")],
                asks=[("100.10", "0.003")],
                ts=now,
            ))
        watch.on_funding_rate(FundingRateUpdate(
            instrument_id=watch._legs[0].instrument_id, rate=Decimal("0.0000063"),
            ts_event=now, ts_init=now, interval=60,
        ))
        watch.on_instrument_status(make_status(
            watch._legs[0].instrument_id, "adapter:snapshot_ready", is_trading=True,
            is_quoting=True,
        ))
        summary = watch.summary()
    finally:
        watch.on_stop()

    assert not watch._cfg.run_state.recording_errors, "a clean run records cleanly"
    assert watch._tape_error is None
    l2 = tmp_path / "l2"
    assert (l2 / "l2_NVDA_ONDO-ASTER_20260907T120000Z.jsonl").exists()
    reader = read_tape([l2])
    rows = list(reader)
    books = [r for r in rows if r["event_kind"] == "book"]
    assert {r["venue"] for r in books} == {"ONDO", "ASTER"}, (
        "each observed leg records its own L2"
    )
    for record in books:
        assert record["bids"] == [["100.05", "0.001"], ["100.04", "0.002"]]
        assert record["asks"] == [["100.10", "0.003"]]
        assert isinstance(record["bids"][0][0], str)
        assert record["source"] == "snapshot"
    assert {r["instrument_id"] for r in books} == {
        ONDO_ID, "NVDAUSDT-PERP.ASTER",
    }
    assert [r["venue"] for r in rows if r["event_kind"] == "quote"] == ["ONDO", "ASTER"]
    assert [r["coverage_limit"] for r in books if r["venue"] == "ONDO"] == [100]
    assert [r["coverage_limit"] for r in books if r["venue"] == "ASTER"] == [None]
    kinds = {r["event_kind"] for r in rows}
    assert {"run_start", "instrument", "book", "quote", "funding", "status", "run_end"} <= kinds
    assert reader.status.complete is True
    assert reader.status.dropped == 0
    assert "l2 tape" in summary

    manifest = read_manifest(l2 / "l2_NVDA_ONDO-ASTER_20260907T120000Z.manifest.json")
    assert [entry["segment_index"] for entry in manifest["segments"]] == [1]
    assert manifest["segments"][0]["closed"] is True


def test_the_ondo_leg_records_the_feeds_own_fee_metadata(tmp_path):
    """Contract C: the fee, the real steps, the version and the availability all travel.

    This assertion used to name the five-key record of schema 1 (fee, tick, precisions).
    The record now also carries the venue's *real* quantity and price steps and the
    adapter's metadata version/availability, so the expected shape moved with the
    contract; nothing about the fee itself changed.
    """
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True, instruments={
        InstrumentId.from_str(ONDO_ID): FakeInstrument(
            Decimal("0.00025"), size_increment=Quantity.from_str("0.002"),
            price_increment=Price.from_str("0.01"), price_precision=2, size_precision=3,
            tick_size=Price.from_str("0.01"), ts_init=1_700_000_000_000_000_000,
            info={"metadata_version": "7"},
        ),
    })
    watch.on_start()
    watch.on_stop()
    rows = list(read_tape([tmp_path / "l2"]))
    records = [
        r for r in rows if r["event_kind"] == "instrument" and r["venue"] == "ONDO"
    ]
    assert [r["metadata"] for r in records] == [{
        "venue": "ONDO",
        "client_id": "ONDO",
        "taker_fee_bps": "2.50",
        "fee_source": "instrument_metadata",
        "price_precision": 2,
        "size_precision": 3,
        "tick_size": "0.01",
        "size_increment": "0.002",
        "price_increment": "0.01",
        "metadata_version": "7",
        "metadata_available_ns": 1_700_000_000_000_000_000,
    }]
    assert records[0]["source"] == "instrument_metadata"
    assert records[0]["valid"] is True and records[0]["invalid_reason"] is None


def test_the_depth10_fallback_is_recorded_as_a_limited_source(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True)
    watch.on_start()
    try:
        now = watch.clock.timestamp_ns()
        watch.on_book_depth(make_depth10(
            watch._legs[1].instrument_id, bids=[("200.50", "0.100")],
            asks=[("200.51", "0.200")], ts=now,
        ))
    finally:
        watch.on_stop()
    books = [r for r in read_tape([tmp_path / "l2"]) if r["event_kind"] == "book"]
    assert len(books) == 1
    assert books[0]["venue"] == "ASTER"
    assert books[0]["source"] == "depth10"
    assert books[0]["coverage_limit"] == 10
    assert books[0]["bids"] == [["200.50", "0.100"]], "the zero-size slots are not levels"


def test_a_run_with_ondo_points_raw_md_path_at_the_run_raw_ondo_dir(tmp_path, monkeypatch):
    """Plan 5.2: only the path is plumbed; the fork's recorder owns the layout."""
    from dataclasses import replace
    from unittest.mock import MagicMock

    captured: dict[str, object] = {}

    def fake_ondo(ids, **kwargs):
        captured["ids"] = list(ids)
        captured["kwargs"] = dict(kwargs)
        return object(), object()

    def fake_aster(ids):
        return object(), object()

    monkeypatch.setitem(spread_watch.VENUES, "ONDO",
                        replace(spread_watch.VENUES["ONDO"], build_client=fake_ondo))
    monkeypatch.setitem(spread_watch.VENUES, "ASTER",
                        replace(spread_watch.VENUES["ASTER"], build_client=fake_aster))
    live = MagicMock()
    builder = live.builder.return_value
    for method in ("with_logging", "with_timeout_connection",
                   "with_delay_post_stop_secs", "add_data_client"):
        getattr(builder, method).return_value = builder
    monkeypatch.setattr(spread_watch, "LiveNode", live)
    with contextlib.redirect_stderr(io.StringIO()):
        plan = spread_watch.build_plan(["NVDA"], ["ONDO", "ASTER"])

    off = spread_watch.NodePlan(plan, tmp_path, "20260914T000000Z", 10, 1000)
    _node, strategies, _state = spread_watch.build_node(off)
    assert captured["kwargs"] == {}, "record-l2 off: the ONDO config is untouched"
    assert not (tmp_path / "raw_ondo").exists()
    assert all(not strategy._cfg.record_l2 for strategy in strategies)
    assert all(strategy._cfg.tape_path is None for strategy in strategies)

    on = spread_watch.NodePlan(plan, tmp_path, "20260914T000000Z", 10, 1000,
                               record_l2=True)
    _node, strategies, _state = spread_watch.build_node(on)
    assert captured["ids"] == [ONDO_ID]
    assert captured["kwargs"] == {
        "raw_md_path": str(tmp_path / "raw_ondo"),
        "raw_md_run_id": "20260914T000000Z",
    }
    # The recorder's run id is passed explicitly, not inferred from the directory
    # name, so the raw frames and the tape join on the same id even when --out is
    # not stamped (plan 8 uses a stamped runDir, a default --out does not).
    assert captured["kwargs"]["raw_md_run_id"] == on.stamp
    assert (tmp_path / "raw_ondo").is_dir()
    assert [strategy._cfg.tape_path for strategy in strategies] == [
        tmp_path / "l2" / "l2_NVDA_ONDO-ASTER_20260914T000000Z.jsonl",
    ]
    assert all(strategy._cfg.run_id == "20260914T000000Z" for strategy in strategies)


def test_the_run_manifest_names_the_fragments_in_order(tmp_path):
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True)
    watch.on_start()
    try:
        now = watch.clock.timestamp_ns()
        for leg in watch._legs:
            watch.on_book_deltas(make_batch(
                leg.instrument_id, bids=[("100.05", "0.001")], asks=[("100.10", "0.002")],
                ts=now,
            ))
    finally:
        watch.on_stop()
    from market_tape import write_run_manifest

    manifest = write_run_manifest(tmp_path / "l2")
    assert manifest == tmp_path / "l2" / "manifest.json"
    document = read_run_manifest(tmp_path / "l2")
    assert [fragment["tape"] for fragment in document["fragments"]] == [
        "l2_NVDA_ONDO-ASTER_20260907T120000Z.jsonl",
    ]
    assert document["tapes"][0]["complete"] is True
    assert list(read_tape([manifest])) == list(read_tape([tmp_path / "l2"]))


def test_a_recording_gap_fails_the_recording_acceptance_but_not_the_spread(tmp_path):
    """Plan 5.1: a gap is a failed *recording*, never a silent omission.

    The queue is bounded, so a burst can drop records. The spread deliberately keeps
    running, but the tape must name the gap and the run's own verdict must say the
    recording failed - otherwise a caller reads "complete" off a tape with a hole in it.
    """
    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True)
    watch.on_start()
    try:
        tape = watch._tape
        assert tape is not None
        # Nothing flushes on its own and the queue holds one record: every leg's quote
        # after the first is dropped rather than queued without bound.
        tape.queue_max, tape.flush_secs = 1, 1e9
        now = watch.clock.timestamp_ns()
        for _ in range(4):
            for leg in watch._legs:
                watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01", now))
        assert tape.dropped > 0
        assert not watch._cfg.run_state.recording_errors, "learned when the tape closes"
    finally:
        watch.on_stop()

    assert watch._cfg.run_state.recording_errors, "a gap fails this recording"
    assert "gap" in watch._cfg.run_state.recording_errors[0]
    assert "incomplete" in watch._cfg.run_state.recording_errors[0]
    assert not watch._cfg.run_state.startup_errors, "the spread run itself keeps going"

    reader = read_tape([tmp_path / "l2"])
    rows = list(reader)
    assert reader.status.complete is False
    assert reader.status.dropped > 0 and reader.status.failed is True
    marker = next(r for r in rows if r["event_kind"] == "gap")
    assert marker["valid"] is False and marker["invalid_reason"].startswith("recording_gap")
    assert rows[-1]["event_kind"] == "run_end" and rows[-1]["complete"] is False
    assert "FAILED" in watch.summary(), "the run prints the recording's failed acceptance"


# ------------------------------------------------- the adapter's raw public recorder


def write_raw_session(directory: Path, *, session_id: str, run_id: str, frames: int = 3,
                      clean: bool = True, end: bool = True, part: int = 1,
                      truncate: bool = False) -> Path:
    """One session of the fork's raw public-frame recording, as it writes it."""
    name = "raw_md.jsonl" if part <= 1 else f"raw_md_part{part:04d}.jsonl"
    path = directory / name
    directory.mkdir(parents=True, exist_ok=True)
    lines = [
        {"schema_version": 1, "kind": "run_start", "run_id": run_id,
         "run_id_source": "configured", "session_id": session_id},
        *({"kind": "frame", "session_id": session_id, "recv_seq": n,
           "payload": "{}"} for n in range(1, frames + 1)),
    ]
    if end:
        lines.append({
            "schema_version": 1, "kind": "run_end", "run_id": run_id,
            "session_id": session_id, "clean": clean, "dropped": 0 if clean else 1,
            "gaps": 0 if clean else 1, "records": frames, "markers": 2,
            "bytes": 100, "rotations": 0, "segments": 1, "last_recv_seq": frames,
            "reason": None if clean else "1 frame(s) were dropped when the queue was full",
        })
    # Compact separators: the adapters write with serde_json's `to_string`, and the
    # fixture is read back by a scanner that assumes no particular spacing.
    path.write_text(
        "".join(json.dumps(line, separators=(",", ":")) + "\n" for line in lines),
        encoding="utf-8",
    )
    if truncate:
        with path.open("ab") as handle:
            handle.write(b'{"schema_version": 1, "kind": "frame", "recv')
    return path


def test_the_raw_recorder_stats_are_read_from_its_own_run_end(tmp_path):
    """R2.1: the acceptance reads the adapter's own drop/gap/finalize statistics."""
    raw_dir = tmp_path / "raw_ondo"
    write_raw_session(raw_dir, session_id="s1", run_id="20260915T000000Z", frames=7)

    report = spread_watch.raw_recorder_status(raw_dir, run_id="20260915T000000Z")
    assert report["complete"] is True, report["problem"]
    assert report["run_id_matches"] is True
    assert report["records"] == 7 and report["dropped"] == 0 and report["gaps"] == 0
    assert [s["session_id"] for s in report["sessions"]] == ["s1"]
    assert report["sessions"][0]["clean"] is True
    assert report["sessions"][0]["last_recv_seq"] == 7
    assert report["segments"][0]["truncated"] is False
    assert spread_watch.raw_recorder_text(report).startswith("complete:")


def test_a_raw_recording_that_dropped_frames_is_never_reported_complete(tmp_path):
    raw_dir = tmp_path / "raw_ondo"
    write_raw_session(raw_dir, session_id="s1", run_id="r", frames=4, clean=False)

    report = spread_watch.raw_recorder_status(raw_dir, run_id="r")
    assert report["complete"] is False
    assert report["dropped"] == 1 and report["gaps"] == 1
    assert "not clean" in report["problem"]
    assert spread_watch.raw_recorder_text(report).startswith("INCOMPLETE:")


def test_a_raw_recording_that_never_finalized_is_incomplete(tmp_path):
    """R2.1: 'the feed was still flowing' is never evidence that the recording is complete.

    A session with no ``run_end`` (the process died, or the recorder never finished), a
    segment whose last line has no newline, an empty directory and a run id that does not
    join the tape are all incomplete - each with its own reason.
    """
    unfinalized = tmp_path / "unfinalized"
    write_raw_session(unfinalized, session_id="s1", run_id="r", end=False)
    report = spread_watch.raw_recorder_status(unfinalized, run_id="r")
    assert report["complete"] is False and "never wrote its run_end" in report["problem"]
    assert report["sessions"][0]["finalized"] is False

    truncated = tmp_path / "truncated"
    write_raw_session(truncated, session_id="s1", run_id="r", truncate=True)
    report = spread_watch.raw_recorder_status(truncated, run_id="r")
    assert report["complete"] is False and "no newline" in report["problem"]

    empty = tmp_path / "empty"
    empty.mkdir()
    report = spread_watch.raw_recorder_status(empty, run_id="r")
    assert report["complete"] is False and "no raw public-frame segment" in report["problem"]

    wrong_id = tmp_path / "wrong-id"
    write_raw_session(wrong_id, session_id="s1", run_id="someone-else")
    report = spread_watch.raw_recorder_status(wrong_id, run_id="20260915T000000Z")
    assert report["complete"] is False and "do not join" in report["problem"]
    assert report["run_id_matches"] is False

    silent = tmp_path / "silent"
    write_raw_session(silent, session_id="s1", run_id="r", frames=0)
    report = spread_watch.raw_recorder_status(silent, run_id="r")
    assert report["complete"] is False and "without a single public frame" in report["problem"]


def test_main_reports_an_unconfirmable_raw_recording_as_a_recording_failure(
    monkeypatch, tmp_path,
):
    code, err, out = _run_main_with_a_fake_node(
        monkeypatch, tmp_path, NVDA_ONDO_RECEIVED, [],
        symbols="NVDA", venues="ONDO,ASTER", record_l2=True,
    )
    assert code == 1
    assert "raw_ondo recording" in out
    assert "RECORDING FAILED the raw public-frame recording under " in err


def test_a_tape_write_failure_is_surfaced_and_does_not_kill_the_spread_run(
    tmp_path, monkeypatch,
):
    """A disk failure aborts the recorder and reaches the watcher (plan 5.1)."""
    import market_tape

    watch = build_watch(tmp_path, "NVDA", ("ONDO", "ASTER"), record_l2=True)
    watch.on_start()
    try:
        def boom(_self, _lines):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(market_tape.TapeWriter, "_write_lines", boom)
        now = watch.clock.timestamp_ns()
        for leg in watch._legs:  # every leg keeps quoting after the recorder died
            watch.on_quote(make_quote(leg.instrument_id, "100.00", "100.01", now))
        watch._log_status()  # the status tick flushes the recorder
        assert watch._tape_error is not None
        assert "No space left" in watch._tape_error
        assert watch._cfg.run_state.recording_errors
        assert "No space left" in watch._cfg.run_state.recording_errors[0]
        assert watch._tape is None and all(leg.book_tape is None for leg in watch._legs)
        summary = watch.summary()
    finally:
        watch.on_stop()
    assert "FAILED" in summary
    assert not watch._cfg.run_state.startup_errors, "the spread run itself keeps going"


if __name__ == "__main__":
    unittest.main()

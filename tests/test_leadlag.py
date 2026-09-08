#!/usr/bin/env python3
"""
Offline tests for ``src/analysis/leadlag.py``.

The fixture is a synthetic ``_ref.csv``: a reference price that random-walks, and
a perp whose quote is exactly the reference of 800 ms ago. That is the situation
the analysis is meant to detect, so the cross-correlation must find +800 ms, the
edge windows must be there, and the naive follower must come out ahead.

    .venv\\Scripts\\python.exe -m pytest tests/test_leadlag.py -q
    .venv\\Scripts\\python.exe -m unittest tests.test_leadlag -v
"""

from __future__ import annotations

import csv
import random
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "analysis"))

import leadlag  # noqa: E402  (needs sys.path above)

LAG_MS = 800
STEP_MS = 100
FEE_BPS = 0.9  # HL taker, the leg the fixture uses
SPREAD_BPS = 1.0  # perp half-spread is 0.5 bps either side of its mid


def write_fixture(path: Path, minutes: float = 20.0, seed: int = 11,
                  vol_bps: float = 4.0, start: datetime | None = None) -> Path:
    """A reference walk plus one perp leg that repeats it ``LAG_MS`` later.

    Rows alternate ``ref:book`` and ``quote:HL`` at ``STEP_MS``, which is what the
    watcher produces when both sides update; each row carries both sides, so the
    analysis sees a complete picture on every line.
    """
    rng = random.Random(seed)
    steps = int(minutes * 60 * 1000 / STEP_MS)
    lag_steps = LAG_MS // STEP_MS
    prices = [100.0]
    for _ in range(steps + lag_steps):
        prices.append(prices[-1] * (1.0 + rng.gauss(0.0, vol_bps / 1e4)))
    begin = start or datetime(2026, 9, 8, 14, 0, tzinfo=timezone.utc)  # inside US RTH

    columns = [
        "ts_utc", "event", "ref_ts_src_utc", "ref_last", "ref_bid", "ref_ask",
        "ref_mid", "ref_age_ms",
        "HL_bid", "HL_ask", "HL_age_ms", "HL_buy_edge_bps", "HL_sell_edge_bps",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(columns)
        for i in range(lag_steps, steps):
            moment = begin + timedelta(milliseconds=i * STEP_MS)
            ref_mid = prices[i]
            perp_mid = prices[i - lag_steps]  # the perp is the reference of 800 ms ago
            half = perp_mid * SPREAD_BPS / 2e4
            bid, ask = perp_mid - half, perp_mid + half
            buy_edge = (ref_mid - ask) / ask * 1e4 - FEE_BPS
            sell_edge = (bid - ref_mid) / bid * 1e4 - FEE_BPS
            for event in ("ref:book", "quote:HL"):
                writer.writerow([
                    moment.isoformat(timespec="milliseconds"),
                    event,
                    moment.isoformat(timespec="milliseconds"),
                    f"{ref_mid:.6f}", f"{ref_mid - 0.005:.6f}", f"{ref_mid + 0.005:.6f}",
                    f"{ref_mid:.6f}", "5.0",
                    f"{bid:.8f}", f"{ask:.8f}", "3.0",
                    f"{buy_edge:.4f}", f"{sell_edge:.4f}",
                ])
                moment = moment + timedelta(milliseconds=STEP_MS / 2)
    return path


class LeadLagFixture(unittest.TestCase):
    """Shared synthetic file: reference leads, perp follows 800 ms later."""

    @classmethod
    def setUpClass(cls) -> None:
        cls._dir = tempfile.TemporaryDirectory()
        cls.path = write_fixture(Path(cls._dir.name) / "SYNTH_HL_ref.csv")
        cls.frame, cls.venues = leadlag.load_ref_csv(cls.path)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._dir.cleanup()


class TestLoading(LeadLagFixture):
    def test_finds_the_leg_from_the_header(self):
        self.assertEqual(self.venues, ["HL"])
        self.assertGreater(len(self.frame), 1000)

    def test_rth_filter_keeps_a_session_and_drops_the_night(self):
        with tempfile.TemporaryDirectory() as tmp:
            night = write_fixture(
                Path(tmp) / "night_ref.csv", minutes=2.0,
                start=datetime(2026, 9, 8, 3, 0, tzinfo=timezone.utc),
            )
            frame, _ = leadlag.load_ref_csv(night, rth_only=True)
            self.assertTrue(frame.empty)
            frame_all, _ = leadlag.load_ref_csv(night, rth_only=False)
            self.assertFalse(frame_all.empty)
        frame_rth, _ = leadlag.load_ref_csv(self.path, rth_only=True)
        self.assertEqual(len(frame_rth), len(self.frame))


class TestCrossCorrelation(LeadLagFixture):
    """The headline number: how far behind the perp trades."""

    def test_best_lag_is_the_injected_800ms(self):
        ref_mid = self.frame.set_index("ts")["ref_mid"]
        ref_mid = ref_mid[~ref_mid.index.duplicated(keep="last")].sort_index()
        perp = leadlag.mid_frame(self.frame, "HL")
        perp = perp[~perp.index.duplicated(keep="last")].sort_index()
        correlations = leadlag.cross_correlation(ref_mid, perp)
        lag, corr = leadlag.best_lag(correlations)
        self.assertGreaterEqual(lag, 700.0)
        self.assertLessEqual(lag, 900.0)
        self.assertGreater(corr, 0.5)
        zero = correlations.loc[correlations["lag_ms"] == 0, "corr"].iloc[0]
        self.assertGreater(corr, zero, "the lagged correlation must beat the contemporaneous one")

    def test_lag_range_covers_five_seconds_each_way(self):
        ref_mid = self.frame.set_index("ts")["ref_mid"]
        ref_mid = ref_mid[~ref_mid.index.duplicated(keep="last")].sort_index()
        perp = leadlag.mid_frame(self.frame, "HL")
        perp = perp[~perp.index.duplicated(keep="last")].sort_index()
        correlations = leadlag.cross_correlation(ref_mid, perp)
        self.assertEqual(correlations["lag_ms"].min(), -5000)
        self.assertEqual(correlations["lag_ms"].max(), 5000)


class TestEdgeWindows(LeadLagFixture):
    """Windows are what a taker would actually have to react inside."""

    def test_windows_are_found_and_have_a_duration(self):
        stats = leadlag.edge_stats(self.frame, "HL", "buy", threshold=2.0)
        self.assertGreater(stats.windows, 5)
        self.assertGreater(stats.per_hour, 0.0)
        self.assertGreater(stats.dur_p50_ms, 0.0)
        self.assertGreaterEqual(stats.dur_p90_ms, stats.dur_p50_ms)
        self.assertGreater(stats.max, 2.0)

    def test_time_share_is_monotone_in_the_threshold(self):
        stats = leadlag.edge_stats(self.frame, "HL", "buy", threshold=2.0)
        shares = [stats.share[t] for t in leadlag.THRESHOLDS]
        self.assertEqual(shares, sorted(shares, reverse=True))
        for share in shares:
            self.assertGreaterEqual(share, 0.0)
            self.assertLessEqual(share, 1.0)
        self.assertGreater(shares[0], 0.0)

    def test_time_accounting_matches_the_wall_clock_span(self):
        """Weights are real seconds.

        pandas 3 parses these timestamps at microsecond resolution, so an int64
        view divided by 1e9 would under-count every duration by 1000x - and the
        per-hour rates and time shares with it.
        """
        stats = leadlag.edge_stats(self.frame, "HL", "buy", threshold=2.0)
        span = (self.frame["ts"].iloc[-1] - self.frame["ts"].iloc[0]).total_seconds()
        self.assertAlmostEqual(stats.seconds, span, delta=max(1.0, 0.02 * span))
        self.assertGreater(span, 60.0)

    def test_a_threshold_nothing_reaches_yields_no_windows(self):
        stats = leadlag.edge_stats(self.frame, "HL", "buy", threshold=10_000.0)
        self.assertEqual(stats.windows, 0)
        self.assertEqual(stats.share[5.0] >= 0.0, True)

    def test_weighted_quantile_matches_a_hand_computed_case(self):
        import numpy as np

        values = np.array([1.0, 2.0, 3.0])
        equal = np.array([1.0, 1.0, 1.0])
        self.assertAlmostEqual(leadlag.weighted_quantile(values, equal, 0.5), 2.0)
        # Weight the first value ten times: the median moves down to it.
        skewed = np.array([10.0, 1.0, 1.0])
        self.assertLess(leadlag.weighted_quantile(values, skewed, 0.5), 1.5)


class TestFollowSimulation(LeadLagFixture):
    """A follower that reacts to a real lag should make money before slippage."""

    def test_following_the_lagging_leg_is_profitable(self):
        buy = leadlag.simulate_follow(self.frame, "HL", "buy", threshold=2.0,
                                      hold_secs=5.0, fee_bps=FEE_BPS)
        sell = leadlag.simulate_follow(self.frame, "HL", "sell", threshold=2.0,
                                       hold_secs=5.0, fee_bps=FEE_BPS)
        self.assertGreater(buy.trades, 5)
        self.assertGreater(sell.trades, 5)
        self.assertGreater(buy.mean_bps, 0.0)
        self.assertGreater(sell.mean_bps, 0.0)
        self.assertGreater(buy.win_rate, 0.5)
        self.assertGreater(buy.total_bps, 0.0)

    def test_an_unreachable_threshold_trades_nothing(self):
        result = leadlag.simulate_follow(self.frame, "HL", "buy", threshold=10_000.0,
                                         hold_secs=5.0, fee_bps=FEE_BPS)
        self.assertEqual(result.trades, 0)
        self.assertEqual(result.total_bps, 0.0)

    def test_a_huge_fee_turns_the_same_windows_negative(self):
        result = leadlag.simulate_follow(self.frame, "HL", "buy", threshold=2.0,
                                         hold_secs=5.0, fee_bps=50.0)
        self.assertGreater(result.trades, 0)
        self.assertLess(result.mean_bps, 0.0)


class TestReport(LeadLagFixture):
    """The markdown the CLI prints must mention every section and the leg."""

    def test_report_has_all_four_sections(self):
        fees = leadlag.parse_fees(["HL=0.9"], self.venues)
        report = leadlag.build_report(self.path, self.frame, self.venues, 2.0, 5.0,
                                      fees, rth_only=False)
        for marker in ("## 1. edge distribution", "## 2. persistence",
                       "## 3. lead-lag", "## 4. naive follower"):
            self.assertIn(marker, report)
        self.assertIn("| HL | buy |", report)
        self.assertIn("reference update rows", report)

    def test_fee_defaults_come_from_the_venue_table(self):
        fees = leadlag.parse_fees(None, ["HL", "LIGHTER", "ASTER"])
        self.assertEqual(fees["HL"], 0.9)
        self.assertEqual(fees["LIGHTER"], 0.0)
        self.assertEqual(fees["ASTER"], 0.9)
        self.assertEqual(leadlag.parse_fees(["HL=3.5"], ["HL"])["HL"], 3.5)
        with self.assertRaises(SystemExit):
            leadlag.parse_fees(["HL"], ["HL"])


if __name__ == "__main__":
    unittest.main()

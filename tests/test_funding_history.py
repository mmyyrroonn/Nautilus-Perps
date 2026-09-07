#!/usr/bin/env python3
"""
Offline tests for ``src/analysis/funding_history.py`` (public funding-carry study).

No network: every test runs on synthetic data. Two things are worth pinning down --
the per-venue unit normalisation (the part that is easy to get wrong by 100x) and
the carry statistics (the part that decides the ranking).

    .venv\\Scripts\\python.exe -m unittest tests.test_funding_history -v
    .venv\\Scripts\\python.exe -m pytest tests/test_funding_history.py -q   (if pytest is installed)
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src" / "analysis"))

import funding_history as fh  # noqa: E402  (needs sys.path above)

HOUR = fh.HOUR


class TestUnitNormalisation(unittest.TestCase):
    """Each venue reports funding in a different unit; bps/h is the common ground."""

    def test_hl_rate_is_a_fraction_per_hour(self):
        # Live sample 2026-09-07: BTC fundingRate "0.0000125" -> 0.125 bps/h.
        self.assertAlmostEqual(fh.hl_bps_per_hour("0.0000125"), 0.125, places=9)
        self.assertAlmostEqual(fh.hl_bps_per_hour(-7.5526e-06), -0.075526, places=9)
        self.assertEqual(fh.hl_bps_per_hour(0), 0.0)

    def test_lighter_rate_is_a_percent_per_hour_signed_by_direction(self):
        # Live sample 2026-09-07: BTC rate "0.0010" direction "long" -> 0.10 bps/h.
        self.assertAlmostEqual(fh.lighter_bps_per_hour("0.0010", "long"), 0.10, places=9)
        self.assertAlmostEqual(fh.lighter_bps_per_hour("0.0010", "short"), -0.10, places=9)
        # The venue sends an unsigned magnitude; direction is the only sign carrier.
        self.assertAlmostEqual(fh.lighter_bps_per_hour("-0.0012", "short"), -0.12, places=9)

    def test_lighter_unknown_direction_is_rejected_not_guessed(self):
        with self.assertRaises(ValueError):
            fh.lighter_bps_per_hour("0.0010", "flat")

    def test_lighter_percent_reading_is_consistent_with_the_value_field(self):
        # A Lighter funding row carries `value` = price * rate / 100. Reading `rate`
        # as a percent reproduces it; reading it as a fraction is 100x off.
        rate, value, price = 0.0010, 0.79433800, 79283.2
        self.assertAlmostEqual(price * rate / 100, value, delta=0.005 * value)
        self.assertGreater(abs(price * rate - value), 0.005 * value)
        self.assertAlmostEqual(fh.lighter_bps_per_hour(rate, "long"), 0.10, places=9)

    def test_aster_rate_is_a_fraction_per_interval_spread_backwards(self):
        settle = 8 * fh.HOUR_MS  # 08:00 UTC, an 8 h settlement
        rows = fh.aster_hourly_bps("0.0008", settle, 8)  # 0.0008 fraction = 8 bps
        self.assertEqual(len(rows), 8)
        # 8 bps over 8 hours -> 1 bps in each hour of the interval that ENDED at
        # 08:00, i.e. hours 00:00 through 07:00 inclusive.
        for _hour, bps in rows:
            self.assertAlmostEqual(bps, 1.0, places=9)
        hours = sorted(h for h, _ in rows)
        self.assertEqual(hours[0], 0)
        self.assertEqual(hours[-1], 7 * HOUR)
        # The settlement hour itself belongs to the NEXT interval.
        self.assertNotIn(settle // 1000, hours)

    def test_aster_interval_scaling_and_sign(self):
        one = fh.aster_hourly_bps("0.0001", 4 * fh.HOUR_MS, 1)
        self.assertEqual(len(one), 1)
        self.assertEqual(one[0][0], 3 * HOUR)
        self.assertAlmostEqual(one[0][1], 1.0, places=9)
        four = fh.aster_hourly_bps("-0.0001", 4 * fh.HOUR_MS, 4)
        self.assertEqual(len(four), 4)
        for _h, bps in four:
            self.assertAlmostEqual(bps, -0.25, places=9)
        self.assertAlmostEqual(sum(bps for _h, bps in four), -1.0, places=9)
        with self.assertRaises(ValueError):
            fh.aster_hourly_bps("0.0001", 0, 0)

    def test_floor_hour_helpers(self):
        self.assertEqual(fh.floor_hour_ms(3_600_000 + 59_000), HOUR)
        self.assertEqual(fh.floor_hour_ms(3_599_999), 0)
        self.assertEqual(fh.floor_hour_s(HOUR + 59), HOUR)

    def test_raw_symbol_strips_nautilus_suffixes(self):
        self.assertEqual(fh.raw_symbol("HL", "xyz:NVDA-USD-PERP.HYPERLIQUID"), "xyz:NVDA")
        self.assertEqual(fh.raw_symbol("HL", "BTC-USD-PERP.HYPERLIQUID"), "BTC")
        self.assertEqual(fh.raw_symbol("LIGHTER", "XAU-PERP.LIGHTER"), "XAU")
        self.assertEqual(fh.raw_symbol("ASTER", "XAUUSD1-PERP.ASTER"), "XAUUSD1")
        with self.assertRaises(ValueError):
            fh.raw_symbol("BINANCE", "BTCUSDT-PERP.BINANCE")

    def test_sanitize_makes_windows_safe_cache_names(self):
        # "xyz:NVDA" would be an illegal filename on Windows.
        self.assertEqual(fh.sanitize("hl_funding_xyz:NVDA_1_2_p0"), "hl_funding_xyz_NVDA_1_2_p0")


class TestSmallStatistics(unittest.TestCase):
    def test_percentile_interpolates(self):
        self.assertIsNone(fh.percentile([], 0.5))
        self.assertEqual(fh.percentile([7.0], 0.9), 7.0)
        self.assertAlmostEqual(fh.percentile([0.0, 10.0], 0.5), 5.0, places=9)
        self.assertAlmostEqual(fh.percentile([0.0, 1.0, 2.0, 3.0], 0.10), 0.3, places=9)

    def test_longest_same_sign_run_breaks_on_gaps_and_flips(self):
        values = [1.0, 2.0, 3.0, -1.0, 4.0, 5.0, None, 6.0, 7.0, 8.0, 9.0]
        self.assertEqual(fh.longest_same_sign_run(values, 1.0), 4)  # the trailing 6,7,8,9
        self.assertEqual(fh.longest_same_sign_run(values, -1.0), 1)
        # A missing hour is not a collected hour: it must break the run.
        self.assertEqual(fh.longest_same_sign_run([1.0, 1.0, None, 1.0, 1.0], 1.0), 2)
        self.assertEqual(fh.longest_same_sign_run([1.0, 1.0], 0.0), 0)
        self.assertEqual(fh.longest_same_sign_run([0.0, 0.0, 1.0], 1.0), 1)

    def test_rolling_sums_only_over_fully_populated_windows(self):
        self.assertEqual(fh.rolling_sums([1.0, 2.0, 3.0, 4.0], 2), [3.0, 5.0, 7.0])
        self.assertEqual(fh.rolling_sums([1.0, None, 3.0, 4.0], 2), [7.0])
        self.assertEqual(fh.rolling_sums([1.0, 2.0], 3), [])
        self.assertEqual(fh.rolling_sums([None, None], 1), [])

    def test_rolling_changes_absolute_and_gap_aware(self):
        self.assertEqual(fh.rolling_changes([0.0, 5.0, 2.0, 1.0], 2), [2.0, 4.0])
        self.assertEqual(fh.rolling_changes([0.0, None, 2.0, 1.0], 2), [2.0])


def _grid(n: int) -> list[int]:
    return [i * HOUR for i in range(n)]


def _flat(grid: list[int], value: float) -> dict[int, float]:
    return {h: value for h in grid}


class TestCarryStatistics(unittest.TestCase):
    def test_sign_convention_long_pays_short_receives(self):
        """Long A / short B earns f_B - f_A."""
        grid = _grid(200)
        funding = {
            "HL": _flat(grid, 0.5),  # HL longs pay 0.5 bps/h
            "LIGHTER": _flat(grid, 2.0),  # Lighter longs pay 2.0 bps/h
        }
        # Long the cheap leg (HL), short the expensive one (Lighter): +1.5 bps/h.
        good = fh.pair_stats("X", "HL", "LIGHTER", grid, funding, None, 6.0)
        self.assertIsNotNone(good)
        self.assertAlmostEqual(good.mean_bps_h, 1.5, places=9)
        self.assertAlmostEqual(good.median_bps_h, 1.5, places=9)
        self.assertAlmostEqual(good.mean_funding_long, 0.5, places=9)
        self.assertAlmostEqual(good.mean_funding_short, 2.0, places=9)
        # The reverse leg is the mirror image.
        bad = fh.pair_stats("X", "LIGHTER", "HL", grid, funding, None, 6.0)
        self.assertAlmostEqual(bad.mean_bps_h, -1.5, places=9)
        self.assertIsNone(bad.hours_to_hurdle)  # a losing pair never repays the hurdle

    def test_rollups_hurdle_and_annualisation(self):
        grid = _grid(400)
        funding = {"A": _flat(grid, 0.0), "B": _flat(grid, 1.0)}  # constant +1 bps/h
        s = fh.pair_stats("X", "A", "B", grid, funding, None, 6.0)
        self.assertEqual(s.hours, 400)
        self.assertAlmostEqual(s.coverage_pct, 100.0, places=9)
        self.assertAlmostEqual(s.same_sign_frac, 1.0, places=9)
        self.assertEqual(s.longest_run_h, 400)
        self.assertAlmostEqual(s.carry_24h_p50, 24.0, places=9)
        self.assertAlmostEqual(s.carry_7d_p50, 168.0, places=9)
        self.assertAlmostEqual(s.carry_7d_hit_frac, 1.0, places=9)
        self.assertAlmostEqual(s.hours_to_hurdle, 6.0, places=9)
        self.assertAlmostEqual(s.annualised_pct, 1.0 * 24 * 365 / 100, places=9)
        # No basis series -> no shock term: edge is carry minus hurdle only.
        self.assertAlmostEqual(s.edge_7d_bps, 168.0 - 6.0, places=9)
        self.assertEqual(s.windows_7d, 400 - 168 + 1)
        self.assertTrue(s.rankable)

    def test_hours_where_one_leg_is_missing_are_dropped(self):
        grid = _grid(100)
        a = _flat(grid, 0.0)
        b = _flat(grid, 1.0)
        for h in grid[40:60]:  # a 20 h hole on one leg
            del b[h]
        s = fh.pair_stats("X", "A", "B", grid, {"A": a, "B": b}, None, 6.0)
        self.assertEqual(s.hours, 80)
        self.assertAlmostEqual(s.coverage_pct, 80.0, places=9)
        # 24 h rolling windows must not straddle the hole.
        self.assertAlmostEqual(s.carry_24h_p50, 24.0, places=9)
        self.assertEqual(s.longest_run_h, 40)  # 0..39, the hole, then 60..99
        self.assertIsNone(s.carry_7d_p50)  # no fully populated 168 h window survives
        # Never a complete week -> scored, but not eligible for the ranked table.
        self.assertEqual(s.windows_7d, 0)
        self.assertFalse(s.rankable)

    def test_a_short_sample_is_scored_but_never_rankable(self):
        """A market listed 5 days ago must not be ranked on an extrapolated mean."""
        grid = _grid(120)  # 5 days, so no 168 h window can ever close
        funding = {"A": _flat(grid, 0.0), "B": _flat(grid, 1.0)}
        s = fh.pair_stats("NEW", "A", "B", grid, funding, None, 6.0)
        self.assertEqual(s.hours, 120)
        self.assertAlmostEqual(s.carry_24h_p50, 24.0, places=9)  # 24 h stats are fine
        self.assertEqual(s.windows_7d, 0)
        self.assertFalse(s.rankable)
        # The score is still computed, but the caller must segregate it.
        self.assertAlmostEqual(s.edge_7d_bps, 168.0 - 6.0, places=9)

    def test_requires_a_day_of_overlap(self):
        grid = _grid(50)
        a = _flat(grid[:10], 0.0)
        b = _flat(grid[:10], 1.0)
        self.assertIsNone(fh.pair_stats("X", "A", "B", grid, {"A": a, "B": b}, None, 6.0))
        self.assertIsNone(fh.pair_stats("X", "A", "MISSING", grid, {"A": a}, None, 6.0))

    def test_basis_shock_is_subtracted_from_the_ranking_score(self):
        grid = _grid(400)
        funding = {"A": _flat(grid, 0.0), "B": _flat(grid, 1.0)}
        # A basis that walks 10 bps every 168 h: the p90 shock term is 10.
        basis = {h: 10.0 * (i / 168.0) for i, h in enumerate(grid)}
        s = fh.pair_stats("X", "A", "B", grid, funding, basis, 6.0)
        self.assertEqual(s.basis_hours, 400)
        self.assertAlmostEqual(s.basis_7d_move_p90, 10.0, places=6)
        self.assertAlmostEqual(s.basis_7d_move_max, 10.0, places=6)
        self.assertAlmostEqual(s.edge_7d_bps, 168.0 - 6.0 - 10.0, places=6)
        # A flat basis costs nothing.
        flat = fh.pair_stats("X", "A", "B", grid, funding, _flat(grid, 3.0), 6.0)
        self.assertAlmostEqual(flat.basis_std_bps, 0.0, places=9)
        self.assertAlmostEqual(flat.edge_7d_bps, 168.0 - 6.0, places=9)

    def test_alternating_carry_has_low_persistence_despite_a_positive_mean(self):
        grid = _grid(400)
        a = _flat(grid, 0.0)
        b = {h: (3.0 if i % 2 == 0 else -1.0) for i, h in enumerate(grid)}
        s = fh.pair_stats("X", "A", "B", grid, {"A": a, "B": b}, None, 6.0)
        self.assertAlmostEqual(s.mean_bps_h, 1.0, places=9)
        self.assertAlmostEqual(s.same_sign_frac, 0.5, places=9)
        self.assertEqual(s.longest_run_h, 1)  # never two good hours in a row
        self.assertAlmostEqual(s.carry_24h_p50, 24.0, places=9)

    def test_summary_csv_fields_all_exist_on_the_dataclass(self):
        s = fh.PairStats()
        for name in fh.SUMMARY_FIELDS:
            self.assertTrue(hasattr(s, name), name)


if __name__ == "__main__":
    unittest.main()

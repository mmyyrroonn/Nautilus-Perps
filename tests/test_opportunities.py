#!/usr/bin/env python3
"""
Offline tests for ``src/analysis/opportunities.py`` (the CSV analyser).

The analyser is unchanged in the Entropy round: what is new here is that the
funding units of every venue it can meet are registered explicitly instead of
falling back to a default that happens to be right today.

    .venv\\Scripts\\python.exe -m pytest tests/test_opportunities.py -q
"""

from __future__ import annotations

import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import opportunities  # noqa: E402


@pytest.mark.parametrize("raw,expected", [(0.0001, 1.0), (-0.0001, -1.0), (0.0, 0.0)])
def test_entropy_funding_is_an_hourly_fraction(raw, expected):
    assert opportunities.FUNDING_SCALE["ENTROPY"] == 1e4
    assert opportunities.FUNDING_HOURS["ENTROPY"] == 1.0
    assert opportunities.hourly_bps(raw, "ENTROPY", "GPRO") == pytest.approx(expected)


def test_gpro_aster_period_and_pair_fees_are_explicit():
    assert opportunities.ASTER_FUNDING_HOURS["GPRO"] == 8
    assert opportunities.hourly_bps(0.0008, "ASTER", "GPRO") == pytest.approx(1.0)
    fees, note = opportunities.venue_fees("GPRO")
    assert not note
    assert fees["ENTROPY"] == pytest.approx(0.9)
    assert fees["ASTER"] == pytest.approx(0.9)


def test_every_venue_the_watcher_maps_has_registered_funding_units():
    """A venue missing from the tables would silently fall back to 1e4 / 1 h."""
    import spread_watch

    for symbol, mapping in spread_watch.INSTRUMENTS.items():
        for venue in mapping:
            assert venue in opportunities.FUNDING_SCALE, (symbol, venue)
            if venue == "ASTER":
                continue  # per instrument: registered in ASTER_FUNDING_HOURS
            assert venue in opportunities.FUNDING_HOURS, (symbol, venue)
    assert opportunities.ASTER_FUNDING_HOURS["SNDK"] == 8


def test_funding_report_prints_both_legs_in_bps_per_hour():
    """ENTROPY 1 bp/h (hourly fraction) against ASTER 3 bp/h (every 8 h)."""
    data = opportunities.AllData()
    data.funding_venue["ENTROPY"] = Counter({"0.0001": 4})
    data.funding_venue["ASTER"] = Counter({"0.0024": 4})
    data.funding_pair[("ENTROPY", "ASTER")] = Counter({("0.0001", "0.0024"): 4})
    rows, pairs, flags = opportunities.funding_report(
        data, {"ENTROPY": 0.9, "ASTER": 0.9}, "GPRO",
    )
    by_venue = {row[0]: row for row in rows}
    assert by_venue["ENTROPY"][2] == "1"       # settle interval, hours
    assert by_venue["ENTROPY"][3] == "1.0000"  # bps/h
    assert by_venue["ASTER"][2] == "8"
    assert by_venue["ASTER"][3] == "3.0000"
    assert not flags
    assert len(pairs) == 1
    assert pairs[0].short == "ASTER" and pairs[0].long == "ENTROPY"
    assert pairs[0].carry_bps_h == pytest.approx(2.0)

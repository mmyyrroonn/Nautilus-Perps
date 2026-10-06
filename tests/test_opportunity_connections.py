"""Coverage reports must describe usable feeds without retaining price history."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opportunity_connections import ConnectionHealth
from opportunity_core import BookSnapshot, MarketMetadata
from opportunity_scan import load_plan


def health_state():
    plan = load_plan(Path(__file__).resolve().parents[1] / "config/opportunity-scan.example.toml")
    markets = tuple(m for m in plan.markets if m.symbol == "BTC" and m.venue in {"HL", "LIGHTER"})
    plan = replace(plan, markets=markets, output_path=None)
    state = SimpleNamespace(plan=plan, books={}, metadata={}, saved=0,
                            groups={"BTC": [m.instrument_id for m in markets]})
    for m in markets:
        state.metadata[m.instrument_id] = MarketMetadata(m.instrument_id, Decimal("0.001"),
                                                        taker_fee_bps=Decimal("1"), fee_source="test")
    return state


def book(market, event, received, **kwargs):
    return BookSnapshot(market.venue, market.symbol, ((Decimal("100"), Decimal("1")),),
                        ((Decimal("101"), Decimal("1")),), event, received, **kwargs)


def test_coverage_expires_and_does_not_count_unknown_fees_or_future_timestamps():
    state = health_state()
    health = ConnectionHealth(state)
    now = 10_000_000_000
    a, b = state.plan.markets
    state.books = {a.instrument_id: book(a, now - 100_000_000, now - 90_000_000),
                   b.instrument_id: book(b, now - 110_000_000, now - 80_000_000)}
    health.sample(now)
    assert health.document()["peak_comparable_symbols"] == 1
    assert len(health.last_fresh) == 2
    state.metadata[b.instrument_id] = replace(state.metadata[b.instrument_id], taker_fee_bps=None)
    health.sample(now)
    assert health.last_fresh == {a.instrument_id}
    state.books[a.instrument_id] = book(a, now + 1, now + 1)
    health.sample(now)
    assert health.last_fresh == set()
    state.books[a.instrument_id] = book(a, now - 100_000_000, now - 90_000_000)
    health.sample(now + 3_000_000_000)
    assert health.last_fresh == set()
    assert len(health.seen_fresh) == 2  # Only identifiers, never the past books.


def test_individually_fresh_books_still_need_pair_timestamp_alignment():
    state = health_state()
    health = ConnectionHealth(state)
    now = 10_000_000_000
    a, b = state.plan.markets
    state.books = {a.instrument_id: book(a, now - 100_000_000, now - 90_000_000),
                   b.instrument_id: book(b, now - 1_000_000_000, now - 80_000_000)}
    health.sample(now)
    assert len(health.last_fresh) == 2
    assert health.peak_comparable_symbols == 0
    state.books[b.instrument_id] = replace(state.books[b.instrument_id], valid=False)
    health.sample(now)
    assert health.last_fresh == {a.instrument_id}


def test_aggregate_json_has_no_book_levels_and_missing_coverage_is_explicit():
    state = health_state()
    health = ConnectionHealth(state)
    health.sample(10_000_000_000)
    report = health.document()
    assert report["configured_symbols"] == 1
    assert report["configured_markets"] == 2
    assert report["historical_book_samples_retained"] == 0
    assert report["opportunities_recorded"] == 0
    assert all(row["ever_fresh"] == 0 and len(row["missing_fresh_instruments"]) == 1
               for row in report["venues"].values())
    assert not ({"bids", "asks", "raw_buy_book", "raw_sell_book"} & report.keys())
    assert not hasattr(health, "history")

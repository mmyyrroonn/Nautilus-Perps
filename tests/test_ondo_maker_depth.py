"""Depth visibility, queue ambiguity, exact thresholds and cancel/fill races."""

import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ondo_maker import SECOND, MakerConfig
from ondo_maker_depth import DepthBook
from test_ondo_maker import depth_levels, ready, settle


def test_ahead_excludes_same_price_and_requires_visible_far_boundary():
    book = DepthBook()
    assert book.update(1, [(D(100), D(1)), (D(99), D(1000)), (D(98), D(1))], [(D(101), D(1)), (D(102), D(1))])
    assert book.ahead(1, D(99), D(110)) == 1
    assert book.ahead(1, D(98), D(110)) is None
    assert book.nearby(1, D(99), D(110), D(400)) is None
    assert book.nearby(1, D(99), D(100), D(400)) == 600


@pytest.mark.parametrize("bids,asks", [
    ([], [("101", "1")]),
    ([("100", "0")], [("101", "1")]),
    ([("100", "1"), ("100", "2")], [("101", "1")]),
    ([("99", "1"), ("100", "2")], [("101", "1")]),
    ([("102", "1")], [("101", "1")]),
    ([("NaN", "1")], [("101", "1")]),
])
def test_invalid_snapshot_clears_previous_depth(bids, asks):
    book = DepthBook()
    book.update(1, *depth_levels())
    assert not book.update(2, [(D(p), D(q)) for p, q in bids], [(D(p), D(q)) for p, q in asks])
    assert not book.fresh(2, SECOND) and not book.sides


def test_depth_is_bounded_and_time_regression_invalidates():
    book = DepthBook(limit=2)
    assert not book.update(2, *depth_levels())
    assert book.update(2, [(D(99), D(1))], [(D(101), D(1))])
    assert not book.update(1, [(D(99), D(1))], [(D(101), D(1))])


def test_missing_stale_and_short_depth_block_new_orders_but_not_reduction():
    e = ready()
    e.invalidate_depth("BTC")
    assert all(c.order.market == "ETH" for c in e.step(SECOND))
    e = ready()
    bids, asks = depth_levels()
    e.on_depth("BTC", SECOND, bids[:2], asks)
    commands = e.step(SECOND)
    assert not any(c.order.market == "BTC" and c.order.side == 1 for c in commands)
    assert any(c.order.market == "BTC" and c.order.side == -1 for c in commands)
    e = ready()
    makers = e.step(SECOND)
    now = 5 * SECOND
    for market in e.markets:
        e.on_binance(market, now - SECOND, 100, 100)
        e.on_binance(market, now, 100, 100)
        e.on_ondo_heartbeat(market, now)
    assert [c.kind for c in e.step(now)] == ["cancel"] * 4
    assert all(s.reason == "depth_unavailable" for s in e.markets.values())
    e.update_order(makers[0].order.client_id, filled=D("0.1"), terminal=True, now=now)
    assert e.step(now)[-1].order.reduce_only


def test_thin_ahead_blocks_only_that_side():
    e = ready()
    bids, asks = depth_levels()
    e.on_depth("BTC", SECOND, [(p, D("0.001")) for p, _ in bids], asks)
    orders = e.step(SECOND)
    assert len(orders) == 3
    assert e.markets["BTC"].sides[1].reason == "depth_ahead_insufficient"


def test_exact_thirty_percent_drop_cancels_only_affected_side_and_enforces_cooldown():
    e = ready()
    makers = e.step(SECOND)
    buy = next(c.order for c in makers if c.order.market == "BTC" and c.order.side == 1)
    # Price between depth levels avoids ambiguity from our own quantity at a level.
    buy.price = D("99.955")
    bids, asks = depth_levels()
    e.on_depth("BTC", SECOND + 1, bids, asks)
    assert not e.step(SECOND + 1)
    e.on_depth("BTC", SECOND + 2, [(p, q * D("0.7")) for p, q in bids], asks)
    cancels = e.step(SECOND + 2)
    assert [(c.kind, c.order.client_id) for c in cancels] == [("cancel", buy.client_id)]
    assert e.markets["BTC"].sides[1].reason == "depth_drop"
    settle(e, [buy], SECOND + 3)
    e.set_account(ready=True, reduction_ready=True, flat=True)
    assert not e.step(SECOND + 4)
    resume = 11 * SECOND + 2
    for market in e.markets:
        e.on_binance(market, resume - SECOND, 100, 100)
        e.on_binance(market, resume, 100, 100)
        e.on_ondo_heartbeat(market, resume)
        e.on_depth(market, resume, bids, asks)
    orders = e.step(resume)
    assert len(orders) == 1 and orders[0].order.side == 1


def test_drop_fill_race_cancels_other_markets_and_closes():
    e = ready()
    orders = e.step(SECOND)
    buy = orders[0].order
    e.on_depth("BTC", SECOND + 1, *depth_levels())
    e.step(SECOND + 1)
    bids, asks = depth_levels()
    e.on_depth("BTC", SECOND + 2, [(p, q / 2) for p, q in bids], asks)
    assert len(e.step(SECOND + 2)) == 1
    e.update_order(buy.client_id, filled=D("0.1"), terminal=True, now=SECOND + 3)
    commands = e.step(SECOND + 3)
    assert [c.kind for c in commands] == ["cancel"] * 3 + ["create"]
    assert commands[-1].order.reduce_only


@pytest.mark.parametrize("kwargs", [
    {"depth_window_bps": D(0)}, {"depth_drop_pct": D(101)},
    {"depth_min_multiple": D("NaN")}, {"depth_max_levels": 1001},
    {"depth_stale_ns": 0}, {"depth_cooldown_ns": 0},
])
def test_invalid_depth_configuration(kwargs):
    with pytest.raises(ValueError):
        MakerConfig(D(20), D(20), **kwargs)

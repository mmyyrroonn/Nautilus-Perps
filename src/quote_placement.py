#!/usr/bin/env python3
"""Where a quote rests: the three placements, shared by the live maker and the replay.

Like :mod:`quote_gates`, this is imported by BOTH ``maker_live.QuoteEngine.step()`` and
``analysis/maker_inventory.simulate()`` rather than copied into each, so the live price and
the replayed price cannot drift apart.  It is a pure function of one book sample.

    improve   one tick inside the maker touch, queue ahead 0.  Refuses (``locked``) when that
              tick would cross the other side of the book.  This is what the maker ran until
              2026-09-09.
    join      at the maker touch, queue ahead = the size already resting there.
    anchor    priced off the HEDGE venue mid, ``anchor_edge_bps`` away from it, and only then
              clamped so it cannot cross the maker book.

Why ``anchor`` exists.  Quoting one tick inside Lighter's touch means the quote follows
Lighter, and Lighter's touch follows the last trade - so in a fast market the resting order is
whatever the market has just moved away from, and it gets hit precisely when it is wrong.  The
2026-09-09 mainnet PONS session made that concrete: 40 fills, a MEDIAN edge against the hedge
of +5.5 bps but a MEAN of -8.5 bps, worst -85 bps.  The median fill was fine; a handful of
stale-quote fills during jumps ate the whole session.  The 60 s volatility gate is the wrong
instrument for that - a hedge lands within ~300 ms, so the exposure is the 3 s requote
interval, not the minute - and switching the gate off does not help either.  ``anchor`` attacks
it at the source: the price is derived from the venue we hedge on, so a jump on Lighter alone
does not leave us quoting at a price the hedge cannot fill against, and the distance
``anchor_edge_bps`` is the edge we demand rather than whatever the touch happens to offer.

Queue ahead under ``anchor`` is deliberately pessimistic: 0 only when the price is strictly
inside the maker touch (nothing can be resting there), and the whole top-of-book size when it
is at the touch OR outside it.  Outside the touch the true queue is unknown - the recordings
carry only the top of book - so charging the full top-of-book size cannot flatter the result.

Stdlib only.
"""

from __future__ import annotations

import math
from dataclasses import dataclass


IMPROVE, JOIN, ANCHOR = "improve", "join", "anchor"
PLACEMENTS = (IMPROVE, JOIN, ANCHOR)

# Tolerance, as a fraction of one tick, for the ceil / floor that put an anchor price back on
# the venue's grid: a price already sitting on a tick must not be pushed a whole tick further
# out by float noise.
TICK_EPS = 1e-6


@dataclass(frozen=True)
class Placed:
    """One side's decision: where to rest, and behind how much size."""

    price: float
    queue_ahead: float
    locked: bool = False  # this placement cannot produce a quotable price on this sample


def _ceil_tick(price: float, tick: float, decimals: int) -> float:
    return round(math.ceil(price / tick - TICK_EPS) * tick, decimals)


def _floor_tick(price: float, tick: float, decimals: int) -> float:
    return round(math.floor(price / tick + TICK_EPS) * tick, decimals)


def place(
    placement: str,
    *,
    sell: bool,
    m_bid: float,
    m_ask: float,
    h_bid: float,
    h_ask: float,
    tick: float,
    decimals: int,
    tob: float,
    anchor_edge_bps: float = 0.0,
) -> Placed:
    """Price one side of one sample.

    ``tob`` is the size resting on the maker touch of THIS side (``m_ask_size`` for a sell,
    ``m_bid_size`` for a buy), i.e. the queue we would join at the touch.

    ``improve`` and ``join`` are the pre-existing rules, expressed exactly as they were:
    ``round(touch -/+ tick)`` with a lock test against the opposite touch, and ``round(touch)``
    with the top of book ahead of us.  Anything that is not ``improve`` or ``anchor`` joins,
    which is how the original two-way branch behaved.
    """
    touch = m_ask if sell else m_bid
    opposite = m_bid if sell else m_ask

    if placement == IMPROVE:
        price = round(touch - tick if sell else touch + tick, decimals)
        locked = (price <= opposite) if sell else (price >= opposite)
        return Placed(price, 0.0, locked)

    if placement == ANCHOR:
        h_mid = (h_bid + h_ask) / 2.0 if h_bid > 0.0 and h_ask > 0.0 else 0.0
        if h_mid <= 0.0:
            # No hedge book, no anchor: refuse rather than fall back to the maker touch, which
            # is the very thing this placement exists not to follow.
            return Placed(0.0, 0.0, True)
        edge = anchor_edge_bps / 1e4
        if sell:
            price = _ceil_tick(h_mid * (1.0 + edge), tick, decimals)
            # Never cross the maker book: an ask at or below the bid would take, not make.
            price = max(price, round(m_bid + tick, decimals))
            # Strictly inside the touch there is nothing resting ahead of us; at the touch, or
            # outside it, charge the whole top of book.
            queue = 0.0 if price < m_ask - tick * 0.5 else tob
        else:
            price = _floor_tick(h_mid * (1.0 - edge), tick, decimals)
            price = min(price, round(m_ask - tick, decimals))
            queue = 0.0 if price > m_bid + tick * 0.5 else tob
        return Placed(price, queue, False)

    return Placed(round(touch, decimals), tob, False)


def describe(placement: str, anchor_edge_bps: float = 0.0) -> str:
    """The label the reports and the status line share."""
    if placement == ANCHOR:
        return f"anchor {anchor_edge_bps:g} bps off the hedge mid"
    if placement == IMPROVE:
        return "improve (one tick inside the maker touch)"
    return "join (at the maker touch)"


def label(placement: str, anchor_edge_bps: float = 0.0) -> str:
    """The short form for a table cell."""
    return f"anchor {anchor_edge_bps:g}" if placement == ANCHOR else placement

#!/usr/bin/env python3
"""Where a quote rests: the three placements, shared by the live maker and the replay.

Like :mod:`quote_gates`, this is imported by BOTH ``maker_live.QuoteEngine.step()`` and
``analysis/maker_inventory.simulate()`` rather than copied into each, so the live price and
the replayed price cannot drift apart.  It is a pure function of one book sample.

    improve   one tick inside the maker touch, queue ahead 0.  Refuses (``locked``) when that
              tick would cross the other side of the book.  This is what the maker ran until
              2026-09-09.
    join      at the maker touch, queue ahead = the size already resting there.
    anchor    priced off a FAIR maker mid - the hedge mid carried across the persistent
              cross-venue basis - ``anchor_edge_bps`` away from it, and only then clamped so
              it cannot cross the maker book.

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

Why the basis term.  The first cut of ``anchor`` priced off the raw hedge mid, and the
2026-09-07 / 09-08 replays showed why that is not enough: Lighter's PONS mid trades a median
+28.8 / +25.7 bps ABOVE Aster's, so a symmetric anchor on the Aster mid put the ask inside
Lighter's book (it filled 99-100% of the time, selling ~12 bps over Aster while Lighter's own
ask was ~52 bps over) and the bid below Lighter's bid, where it could never fill.  Every anchor
row was one-sided - 10 to 20 times more ask fills than bid fills - and the inventory sat pinned
at its cap.  So the anchor is now centred on

    f = h_mid * (1 + b),   b = median over ``basis_window_s`` of (m_mid / h_mid - 1)

which is the maker venue's own fair value expressed through the venue we hedge on: it follows
the hedge tick by tick, but it sits where the maker book actually is.  ``b`` is a median, not a
mean, so a single dislocated sample cannot move it, and the anchor refuses to OPEN until
``basis_min_n`` samples have been seen - an anchor on a basis estimated from nothing is the raw
hedge mid again.  ``basis_window_s = 0`` switches the correction off and restores exactly the
old behaviour.

``anchor_skew_bps`` then leans the whole quote against the inventory: both prices shift by
``-anchor_skew_bps * q / q_cap`` bps, so a long book quotes lower on both sides (its ask is
easier to hit, its bid harder) and works itself back towards flat.  0 leaves the anchor
symmetric.

Queue ahead under ``anchor`` is deliberately pessimistic: 0 only when the price is strictly
inside the maker touch (nothing can be resting there), and the whole top-of-book size when it
is at the touch OR outside it.  Outside the touch the true queue is unknown - the recordings
carry only the top of book - so charging the full top-of-book size cannot flatter the result.

Stdlib only.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass


IMPROVE, JOIN, ANCHOR = "improve", "join", "anchor"
PLACEMENTS = (IMPROVE, JOIN, ANCHOR)

# Tolerance, as a fraction of one tick, for the ceil / floor that put an anchor price back on
# the venue's grid: a price already sitting on a tick must not be pushed a whole tick further
# out by float noise.
TICK_EPS = 1e-6


@dataclass(frozen=True)
class BasisParams:
    """How the cross-venue basis is estimated.  ``window_s = 0`` switches it off."""

    window_s: float = 0.0  # seconds of rolling median; 0 = no correction, raw hedge mid
    min_n: int = 30  # samples the window must hold before the anchor may OPEN

    @property
    def on(self) -> bool:
        return self.window_s > 0.0


class BasisTracker:
    """Rolling median of ``m_mid / h_mid - 1``: where the maker book sits, in the hedge's units.

    Stateful and fed one sample a second, exactly like :class:`quote_gates.QuoteGates`, and
    kept here so the live maker and the replay share one estimator rather than two.

    ``basis`` is the median of whatever the window holds - usable from the first sample, so a
    CLOSING quote is never priced off a knowingly wrong mid - while ``ready`` stays False until
    ``min_n`` samples have arrived.  The caller uses ``ready`` to hold the OPENING side back:
    taking on inventory against an estimate built from three samples is the raw-hedge-mid bug
    with extra steps.
    """

    def __init__(self, params: BasisParams) -> None:
        self.p = params
        self._window: deque[tuple[float, float]] = deque()  # (t, m_mid / h_mid - 1)
        self.basis = 0.0  # fractional, e.g. 0.00288 for +28.8 bps
        self.raw = 0.0  # this sample's uncorrected ratio, for the log line
        self.samples = 0
        self._ready = False
        self._announced = False

    @property
    def ready(self) -> bool:
        """False only while an ENABLED estimator is still short of ``min_n`` samples."""
        return True if not self.p.on else self._ready

    @property
    def basis_bps(self) -> float:
        return self.basis * 1e4

    @property
    def n(self) -> int:
        return len(self._window)

    def fair(self, h_mid: float) -> float:
        """The hedge mid carried across the basis: where the maker book's mid should be."""
        return h_mid * (1.0 + self.basis)

    def update(self, t: float, *, m_bid: float, m_ask: float, h_bid: float,
               h_ask: float) -> list[str]:
        """Push one sample.  Returns the state-change lines to log, at most one per call."""
        p = self.p
        if not p.on:
            return []
        m_mid = (m_bid + m_ask) / 2.0 if m_bid > 0.0 and m_ask > 0.0 else 0.0
        h_mid = (h_bid + h_ask) / 2.0 if h_bid > 0.0 and h_ask > 0.0 else 0.0
        if m_mid > 0.0 and h_mid > 0.0:
            self.samples += 1
            self.raw = m_mid / h_mid - 1.0
            self._window.append((t, self.raw))
            cut = t - p.window_s
            while len(self._window) > 1 and self._window[0][0] < cut:
                self._window.popleft()
            self.basis = statistics.median([v for _, v in self._window])

        lines: list[str] = []
        if not self._announced:
            # Said once, at the very first sample: the operator has to know the opening side
            # is deliberately silent rather than broken.
            self._announced = True
            lines.append(
                f"basis warming up: {self.n}/{p.min_n} samples over {p.window_s:g} s; "
                f"anchor will not OPEN until it is estimated",
            )
        elif not self._ready and self.n >= p.min_n:
            self._ready = True
            lines.append(
                f"basis ready: {self.basis_bps:+.1f} bps (median of {self.n} samples "
                f"over {p.window_s:g} s)",
            )
        return lines


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
    basis: float = 0.0,
    anchor_skew_bps: float = 0.0,
    inv_frac: float = 0.0,
) -> Placed:
    """Price one side of one sample.

    ``tob`` is the size resting on the maker touch of THIS side (``m_ask_size`` for a sell,
    ``m_bid_size`` for a buy), i.e. the queue we would join at the touch.

    ``basis`` is :attr:`BasisTracker.basis` - fractional, 0 for the uncorrected anchor.
    ``inv_frac`` is the SIGNED inventory as a fraction of its cap, clamped to [-1, 1]; with
    ``anchor_skew_bps`` set it leans both anchor prices by ``-anchor_skew_bps * inv_frac``
    bps, so a long book quotes lower and works itself back to flat.

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
        # The fair maker mid: the hedge mid carried across the basis, then leaned against the
        # inventory.  Both sides move together, so the edge stays symmetric around it.
        fair = h_mid * (1.0 + basis)
        if anchor_skew_bps:
            fair *= 1.0 - anchor_skew_bps * max(-1.0, min(1.0, inv_frac)) / 1e4
        edge = anchor_edge_bps / 1e4
        if sell:
            price = _ceil_tick(fair * (1.0 + edge), tick, decimals)
            # Never cross the maker book: an ask at or below the bid would take, not make.
            price = max(price, round(m_bid + tick, decimals))
            # Strictly inside the touch there is nothing resting ahead of us; at the touch, or
            # outside it, charge the whole top of book.
            queue = 0.0 if price < m_ask - tick * 0.5 else tob
        else:
            price = _floor_tick(fair * (1.0 - edge), tick, decimals)
            price = min(price, round(m_ask - tick, decimals))
            queue = 0.0 if price > m_bid + tick * 0.5 else tob
        return Placed(price, queue, False)

    return Placed(round(touch, decimals), tob, False)


def describe(placement: str, anchor_edge_bps: float = 0.0, *, basis: BasisParams | None = None,
             skew_bps: float = 0.0) -> str:
    """The label the reports and the status line share."""
    if placement == ANCHOR:
        centre = (
            f"a fair mid (basis: median of {basis.window_s:g} s, at least {basis.min_n} "
            f"samples)" if basis is not None and basis.on else "the raw hedge mid"
        )
        skew = f", skewed {skew_bps:g} bps by inventory" if skew_bps else ""
        return f"anchor {anchor_edge_bps:g} bps off {centre}{skew}"
    if placement == IMPROVE:
        return "improve (one tick inside the maker touch)"
    return "join (at the maker touch)"


def label(placement: str, anchor_edge_bps: float = 0.0) -> str:
    """The short form for a table cell."""
    return f"anchor {anchor_edge_bps:g}" if placement == ANCHOR else placement

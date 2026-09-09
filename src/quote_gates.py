#!/usr/bin/env python3
"""The two OPENING-side gates, shared by the live maker and the offline replay.

``maker_live.QuoteEngine`` and ``analysis/maker_inventory.simulate()`` are two copies of one
quoting rule kept in step by ``tests/test_maker_live.TestQuotingParity``.  These gates are the
one piece deliberately NOT copied: both import :class:`QuoteGates` from here, so the live run
and the replay cannot drift on the arithmetic that decides whether opening inventory is
allowed at all.

Why they exist, both from mainnet / recorded evidence:

*Spread gate.*  On 2026-09-09 the Lighter PONS touch spread collapsed from the ~46 bps of the
09-07 recording to 5.9 bps while Aster's stayed near 8 bps plus a 0.9 bps taker fee.  A maker
that keeps quoting there captures no spread (the session's sells averaged 0.77911 against buys
of 0.77914) and pays ~10 bps on every hedge round trip; the trip-EWMA kill stopped it, but
only after 23 trips.  The rule is therefore: do not OPEN inventory unless the maker venue's
own spread pays for the hedge round trip.

*Volatility gate.*  A wide spread is not sufficient.  On the 09-08 recording the Lighter
spread was still 30-35 bps through the US morning and the replay still lost 47 USD for the day
with -101 USD of spread capture, the damage concentrated in 15:00-16:59 UTC (-16, -23); the
09-07 replay made +84 USD overall yet lost 10.7 and 8.8 USD in those same two hours.  That is
adverse selection in a fast market: the resting quote is picked off by the side that is about
to be right.  The rule is: do not OPEN inventory while the maker mid is moving faster than
``max_move_bps_per_min`` over ``vol_window_s``.

Both gates behave the same way, and neither is a kill switch:

* they gate only the side that would GROW ``|q|``.  The closing side keeps quoting, so a
  position taken on before the gate closed can still be worked off passively;
* a resting opening quote is cancelled when a gate closes (one cancel transaction);
* a state change is announced once per transition, never once per sample;
* the time-weighted share of the session each gate spent closed is reported.

Opening is allowed only when BOTH gates are open.

Stdlib only, no venue and no I/O: it is fed one book sample a second and answers.
"""

from __future__ import annotations

import statistics
from collections import deque
from dataclasses import dataclass


EPS = 1e-12


@dataclass(frozen=True)
class GateParams:
    """The knobs.  Zero disables a gate, which is what the offline default replay uses.

    ``live_limits.QuoteLimits`` supplies these in a live run and enforces hard floors on them
    (a configuration file can tighten a gate, never switch one off); the offline simulator
    takes them from ``--min-spread-ratio`` / ``--min-maker-spread-bps`` /
    ``--max-move-bps-per-min``, all defaulting to 0 so every existing replay result stands.
    """

    # -- spread gate: the maker spread must pay for the hedge round trip
    min_spread_ratio: float = 0.0  # required s_m / hedge round-trip cost; 0 = off
    min_maker_spread_bps: float = 0.0  # absolute floor under s_m; 0 = off
    spread_window_s: float = 10.0  # rolling median window, to ride out single-sample flicker
    # -- volatility gate: the maker mid must not be running
    max_move_bps_per_min: float = 0.0  # max |range| of the maker mid over the window; 0 = off
    vol_window_s: float = 60.0
    # -- the costs the spread gate measures against; bps of notional
    hedge_fee_bps: float = 0.0  # taker fee on the hedge venue
    maker_fee_bps: float = 0.0  # maker fee on the quoting venue

    @property
    def spread_on(self) -> bool:
        return self.min_spread_ratio > 0.0 or self.min_maker_spread_bps > 0.0

    @property
    def vol_on(self) -> bool:
        return self.max_move_bps_per_min > 0.0

    @property
    def on(self) -> bool:
        return self.spread_on or self.vol_on

    def describe(self) -> str:
        """One line for the report header / the dry-run banner."""
        if not self.on:
            return "opening gates off"
        parts = []
        if self.spread_on:
            parts.append(
                f"spread gate {self.min_spread_ratio:g}x hedge cost and >= "
                f"{self.min_maker_spread_bps:g} bps, median over {self.spread_window_s:g} s",
            )
        if self.vol_on:
            parts.append(
                f"volatility gate <= {self.max_move_bps_per_min:g} bps range over "
                f"{self.vol_window_s:g} s",
            )
        return "; ".join(parts)


class QuoteGates:
    """Rolling spread and volatility gates over 1 s book samples.

    Feed it one sample per decision with :meth:`update`, then read :attr:`open`.  ``update``
    returns the state-change lines to log - empty on every sample that did not flip a gate.
    """

    def __init__(self, params: GateParams) -> None:
        self.p = params
        self._spread: deque[tuple[float, float]] = deque()  # (t, maker touch spread bps)
        self._mid: deque[tuple[float, float]] = deque()  # (t, maker mid)
        self.raw_spread_bps = 0.0  # this sample's maker touch spread
        self.spread_bps = 0.0  # ... smoothed over spread_window_s: what the gate reads
        self.hedge_bps = 0.0  # hedge venue touch spread, same denominator
        self.cost_bps = 0.0  # hedge round trip: hedge spread + both fees
        self.vol_bps = 0.0  # maker mid range over vol_window_s
        self.spread_open = True
        self.vol_open = True
        self.samples = 0
        self.transitions = 0
        # Time-weighted accounting, in seconds of the session.
        self.total_s = 0.0
        self.spread_closed_s = 0.0
        self.vol_closed_s = 0.0
        self.closed_s = 0.0  # either gate closed: the share that actually stopped opening

    # -- state ----------------------------------------------------------------------------

    @property
    def open(self) -> bool:
        """Opening is allowed only while BOTH gates are open."""
        return self.spread_open and self.vol_open

    @property
    def state(self) -> str:
        return "open" if self.open else "closed"

    def _share(self, closed_s: float) -> float:
        return 100.0 * closed_s / self.total_s if self.total_s > 0.0 else 0.0

    @property
    def spread_closed_pct(self) -> float:
        return self._share(self.spread_closed_s)

    @property
    def vol_closed_pct(self) -> float:
        return self._share(self.vol_closed_s)

    @property
    def closed_pct(self) -> float:
        return self._share(self.closed_s)

    # -- one sample -----------------------------------------------------------------------

    def update(
        self,
        t: float,
        *,
        m_bid: float,
        m_ask: float,
        h_bid: float,
        h_ask: float,
        mid: float,
        dur: float = 1.0,
    ) -> list[str]:
        """Push one book sample and re-decide both gates.

        ``mid`` is the cross-venue mid both the live gate and the replay divide by (see
        ``maker_fill.merge_pair`` / ``BookSample.mid``), so the two spreads are measured
        against the same denominator and the ratio is unaffected by which venue is quoted.
        ``dur`` is the sample's forward duration, used only for the time-weighted shares.

        Returns one line per gate that changed state, ready to log.
        """
        p = self.p
        self.samples += 1

        if mid > 0.0 and m_ask > m_bid > 0.0:
            self.raw_spread_bps = (m_ask - m_bid) / mid * 1e4
            self._spread.append((t, self.raw_spread_bps))
            self._trim(self._spread, t - p.spread_window_s)
            self.spread_bps = statistics.median([v for _, v in self._spread])
        if mid > 0.0 and h_ask > h_bid > 0.0:
            self.hedge_bps = (h_ask - h_bid) / mid * 1e4
        # A hedge leg we cannot see costs at least its fees; never less, so the gate cannot
        # be talked into opening by a missing book.
        self.cost_bps = self.hedge_bps + p.hedge_fee_bps + p.maker_fee_bps

        m_mid = (m_bid + m_ask) / 2.0 if m_bid > 0.0 and m_ask > 0.0 else 0.0
        if m_mid > 0.0:
            self._mid.append((t, m_mid))
            self._trim(self._mid, t - p.vol_window_s)
            lo = min(v for _, v in self._mid)
            hi = max(v for _, v in self._mid)
            self.vol_bps = (hi - lo) / m_mid * 1e4

        spread_open = True
        if p.spread_on:
            spread_open = (
                self.spread_bps >= p.min_spread_ratio * self.cost_bps - EPS
                and self.spread_bps >= p.min_maker_spread_bps - EPS
            )
        vol_open = True
        if p.vol_on:
            vol_open = self.vol_bps <= p.max_move_bps_per_min + EPS

        lines: list[str] = []
        if spread_open != self.spread_open:
            self.spread_open = spread_open
            self.transitions += 1
            lines.append(self._spread_line())
        if vol_open != self.vol_open:
            self.vol_open = vol_open
            self.transitions += 1
            lines.append(self._vol_line())

        if dur > 0.0:
            self.total_s += dur
            if not spread_open:
                self.spread_closed_s += dur
            if not vol_open:
                self.vol_closed_s += dur
            if not (spread_open and vol_open):
                self.closed_s += dur
        return lines

    @staticmethod
    def _trim(window: deque[tuple[float, float]], cut: float) -> None:
        """Drop samples older than ``cut``, always keeping the newest one."""
        while len(window) > 1 and window[0][0] < cut:
            window.popleft()

    # -- messages -------------------------------------------------------------------------

    def _spread_line(self) -> str:
        p = self.p
        fees = p.hedge_fee_bps + p.maker_fee_bps
        closed = not self.spread_open
        floor_binds = self.spread_bps < p.min_maker_spread_bps - EPS
        ratio_binds = self.spread_bps < p.min_spread_ratio * self.cost_bps - EPS
        if closed and floor_binds and not ratio_binds:
            body = (
                f"maker {self.spread_bps:.1f} bps < floor {p.min_maker_spread_bps:g} bps"
            )
        else:
            body = (
                f"maker {self.spread_bps:.1f} bps {'<' if closed else '>='} "
                f"{p.min_spread_ratio:g} x {self.cost_bps:.1f} bps "
                f"(hedge {self.hedge_bps:.1f} + fees {fees:.1f})"
            )
        return f"spread gate {'closed' if closed else 'open'}: {body}"

    def _vol_line(self) -> str:
        p = self.p
        closed = not self.vol_open
        return (
            f"volatility gate {'closed' if closed else 'open'}: maker mid range "
            f"{self.vol_bps:.1f} bps over {p.vol_window_s:g} s "
            f"{'>' if closed else '<='} {p.max_move_bps_per_min:g} bps"
        )

    def status(self) -> str:
        """The fragment the 60 s status line and the SUMMARY share."""
        return (
            f"spread L={self.spread_bps:.1f} H={self.hedge_bps:.1f} "
            f"vol={self.vol_bps:.1f} gate={self.state} "
            f"(closed sp {self.spread_closed_pct:.0f}% vol {self.vol_closed_pct:.0f}% "
            f"any {self.closed_pct:.0f}%)"
        )

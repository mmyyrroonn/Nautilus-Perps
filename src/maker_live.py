#!/usr/bin/env python3
"""Stage 3 - live two-sided maker on Lighter, taker-hedged on Aster, with hard limits.

The live counterpart of the offline simulator ``src/analysis/maker_inventory.py``.  The
quoting rule is that simulator's ``simulate()`` loop, lifted out into :class:`QuoteEngine` so
the same code drives the paper replay, the testnet rehearsal and (only with the user's
explicit consent) a mainnet session:

    closing side   the side that would SHRINK the inventory is quoted unconditionally, for at
                   most |q|, so a closing order can never flip us through zero.
    opening side   the side that would GROW it must clear the cross-venue edge gate
                   (our price vs the hedge venue touch, after the hedge taker fee, the maker
                   fee and a reserve) AND leave room under the inventory cap.
    improve        one tick inside the touch (queue ahead 0), unless that would lock the book.
    cadence        a side is re-priced at most every ``requote_min_s`` seconds, and every
                   place / re-price / cancel draws one token from a bucket refilled at
                   ``tx_per_min``; an action that finds it empty is retried next tick and the
                   resting order is left exactly as it is.

Everything the live version adds on top of the simulator is a *restriction*, never a
loosening: venue minimum sizes, the unhedged-delta suspension of the opening side, the
per-order / inventory / exposure / daily caps of :mod:`live_limits`, and the kill switch.

Modes
-----
``--dry-run``   Build every config object, print the resolved limits (configured vs hard cap)
                and the instrument ids, then exit before the node runs.  No network.
``--paper``     Public MAINNET data feeds only - no execution clients, no credentials.  The
                identical strategy logic runs, fills are simulated locally from the Lighter
                trade tape with ``maker_inventory``'s fill model (queue ahead = the venue top
                of book when we join, 0 when we improve; a trade with the matching aggressor
                at or through our price eats the queue and then fills us).  The hedge fills at
                the Aster touch, capped by the size resting on it, with any remainder carried
                in the unhedged delta - ``slippage_bps`` is the protective limit a real order
                carries, not a cost a filled one pays.  Produces the
                same CSVs and the same P&L monitor output as a live run.  This is the only
                mode that can be verified without credentials.
``--env testnet --live``
                Lighter TESTNET execution on ``DOGE-PERP.LIGHTER`` (PONS is not listed on
                Lighter testnet), hedged on Aster testnet ``DOGEUSDT-PERP.ASTER``.  Pass
                ``--hedge none`` to rehearse maker-only.
``--env mainnet --live --confirm-mainnet``
                Refuses without the flag, prints the limits and sleeps 10 s before connecting.
                **Per CLAUDE.md the user must say 「上主网」 in the current conversation before
                anyone runs this.**  No amount of configuration substitutes for that.

DEVIATION FROM PROMPT.md STAGE 3 - NEEDS THE USER'S APPROVAL
------------------------------------------------------------
PROMPT.md caps the mainnet stage at "单日最多 20 次触发".  That budget was written for the
taker/taker arbitrage, where one trigger is one whole round trip.  A two-sided maker earns
from many small passive fills - a ten minute session already prints dozens - so the daily
budget here is counted in FILLS: ``[daily] max_fills`` defaults to 200 with a hard cap of 500,
plus a separate ``max_tx`` budget over Lighter order transactions.  The per-order notional
(50 USD) and total exposure (100 USD) hard caps are unchanged.  See ``src/live_limits.py``.

Safety
------
* Hard caps are module constants in :mod:`live_limits`; ``config/limits.toml`` can only lower
  them (``min(config, CAP)``, the ``exec_probe.py:347`` pattern).
* The Nautilus risk engine is the second line: ``LiveRiskEngineConfig(bypass=False,
  max_notional_per_order={...})`` on both instruments.
* Reconciliation is on, and on start every open order of ours on the Lighter instrument is
  cancelled before the first quote goes out.
* A kill-switch breach, the ``--minutes`` / ``--until`` deadline and SIGINT all run the same
  cleanup: cancel our quotes, hedge the residual delta IOC, print the summary.  An order left
  unconfirmed at shutdown exits non-zero with the leftover list (``exec_probe`` pattern).
* Hedges are LIMIT IOC, priced at the Aster touch offset by ``slippage_bps`` so a moving
  touch cannot fill us arbitrarily far away - never MARKET.  The offset is protection, not an
  expected cost: paper mode therefore fills a hedge at the touch itself, not at the limit.

    python src/maker_live.py --dry-run --env mainnet --symbol PONS
    python src/maker_live.py --paper --env mainnet --symbol PONS --minutes 10
    python src/maker_live.py --live --env testnet --symbol DOGE --minutes 30
"""

from __future__ import annotations

import argparse
import csv
import functools
import json
import math
import os
import sys
import threading
import time
import traceback
from collections import Counter
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field
from datetime import datetime
from datetime import timedelta
from datetime import timezone
from decimal import ROUND_CEILING
from decimal import ROUND_FLOOR
from decimal import Decimal
from pathlib import Path
from typing import Any
from typing import Self


_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from live_limits import CAP_ORDER_NOTIONAL_USD  # noqa: E402
from live_limits import CAP_TOTAL_NOTIONAL_USD  # noqa: E402
from live_limits import Limits  # noqa: E402
from live_limits import LimitsError  # noqa: E402
from live_limits import load_limits  # noqa: E402


try:  # Optional: load a local .env when python-dotenv is available.
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is a convenience, not a requirement
    pass


# ------------------------------------------------------------------ constants

ASK, BID = 1, 0  # side index: ASK = we sell on the maker venue, BID = we buy
EPS = 1e-12

# Fees in bps of notional, as recorded by spread_watch.py / maker_inventory.py on 2026-09-08.
# Lighter charges no maker and no taker fee on the symbols probed; Aster mainnet published
# maker 0 / taker 0.9 bps.  These feed the opening-side edge gate and the P&L decomposition.
LIGHTER_MAKER_FEE_BPS = 0.0
ASTER_TAKER_FEE_BPS = 0.9

# Sampling / reporting cadence.
DECIDE_SECS = 1.0  # the maker_inventory sample interval: both sides are re-decided once a second
STATUS_SECS = 60.0
STARTUP_GRACE_SECS = 15.0  # both books must be up before the first quote is allowed out
MAINNET_COUNTDOWN_SECS = 10.0
CLEANUP_GRACE_SECS = 10.0
DEFAULT_CONNECT_TIMEOUT_SECS = 60
# Venue connects are flaky through this host's proxy (the Aster/Binance market WebSocket times
# out at connect).  A node that never connected has sent nothing, so rebuilding it is safe;
# once any order has gone out the run is never repeated.  Same reasoning as
# exec_probe.CONNECT_ATTEMPTS.
CONNECT_ATTEMPTS = 3
CONNECT_RETRY_SECS = 15

# Lighter venue error for "Too Many Requests" on the sendTx path.  Seen after ~40 modifies in
# one minute; the response is to halve the local token bucket for a minute, not to retry.
LIGHTER_RATE_LIMIT_CODE = "23000"
THROTTLE_FACTOR = 0.5
THROTTLE_SECS = 60.0

# Exit codes (exec_probe.py:120-123, plus a dedicated one for the kill switch).
EXIT_OK = 0
EXIT_FAILED = 1
EXIT_REFUSED = 2
EXIT_TIMEOUT = 3
EXIT_KILLED = 4


@dataclass(frozen=True)
class SymbolPlan:
    """One tradable symbol: the maker leg on Lighter and the hedge leg on Aster.

    Kept local rather than imported from ``spread_watch.INSTRUMENTS`` so the live path has no
    dependency on the stage-1 watcher's CLI globals.  The naming rules are the same ones
    ``spread_watch.crypto()`` applies.
    """

    symbol: str
    maker_id: str
    hedge_id: str
    note: str = ""


# Verified 2026-09-08.  Lighter mainnet PONS = market 231, 5 price dp / 1 size dp, min 20 PONS
# and min quote 10 USD, maker and taker fee 0.  Aster mainnet PONSUSDT is USDT-margined with a
# 0.00001 tick, a 1 PONS step and a 5 USD min notional.  PONS is NOT listed on Lighter testnet
# (176 markets, no PONS), so the testnet rehearsal uses DOGE, which exists on both Lighter
# testnet (market 3) and Aster testnet (18 symbols, DOGEUSDT among them).
SYMBOLS: dict[str, dict[str, SymbolPlan]] = {
    "mainnet": {
        "PONS": SymbolPlan("PONS", "PONS-PERP.LIGHTER", "PONSUSDT-PERP.ASTER"),
    },
    "testnet": {
        "DOGE": SymbolPlan(
            "DOGE", "DOGE-PERP.LIGHTER", "DOGEUSDT-PERP.ASTER",
            note="PONS is not listed on Lighter testnet; DOGE rehearses the mechanics",
        ),
    },
}


def resolve_plan(env: str, symbol: str) -> SymbolPlan:
    table = SYMBOLS.get(env)
    if table is None:
        raise SystemExit(f"[maker] unknown --env {env!r}; known: {sorted(SYMBOLS)}")
    plan = table.get(symbol.upper())
    if plan is None:
        raise SystemExit(
            f"[maker] {symbol!r} is not mapped for --env {env}; known: {sorted(table)}",
        )
    return plan


# ------------------------------------------------------------------ accounting


class AvgCostBook:
    """Average-cost position book: realises pnl, and the fees, on the matched base.

    Copied verbatim from ``src/analysis/maker_inventory.py`` (class ``AvgCostBook``) so the
    live P&L decomposes exactly the way the offline report does and the two can be compared
    number for number.  ``tests/test_maker_live.py`` asserts this copy still matches the
    original on a shared trade sequence.

    ``cash`` is the raw cash flow (a sell adds, a buy subtracts), so the identity

        cash + pos * mark == sum(realised) + pos * (mark - avg)

    holds exactly and lets the decomposition be checked against the cash flows.
    """

    __slots__ = ("pos", "avg", "fee_pool", "cash", "fees")

    def __init__(self) -> None:
        self.pos = 0.0  # signed base
        self.avg = 0.0  # average cost of the open position
        self.fee_pool = 0.0  # fees paid to open what is still open
        self.cash = 0.0
        self.fees = 0.0

    def trade(self, base: float, price: float, fee: float) -> tuple[float, float, float]:
        """Apply a signed trade; return (realised pnl, round-trip fee, matched base)."""
        self.cash -= base * price
        self.fees += fee
        if self.pos == 0.0 or (self.pos > 0.0) == (base > 0.0):
            total = abs(self.pos) + abs(base)
            if total > 0.0:
                self.avg = (self.avg * abs(self.pos) + price * abs(base)) / total
            self.pos += base
            self.fee_pool += fee
            return 0.0, 0.0, 0.0
        matched = min(abs(self.pos), abs(base))
        sign = 1.0 if self.pos > 0.0 else -1.0
        realized = (price - self.avg) * matched * sign
        released = self.fee_pool * (matched / abs(self.pos))
        self.fee_pool -= released
        close_fee = fee * (matched / abs(base)) if abs(base) > 0.0 else 0.0
        rest = abs(base) - matched
        self.pos += base
        if rest > EPS:  # flipped through zero: the remainder opens a fresh position
            self.avg = price
            self.fee_pool = fee - close_fee
        elif abs(self.pos) <= EPS:
            self.pos = 0.0
            self.avg = 0.0
            self.fee_pool = 0.0
        return realized, released + close_fee, matched


# ------------------------------------------------------------------ quoting rule


@dataclass
class BookSample:
    """One 1 s sample: both touches, the maker top-of-book sizes, and the timestamp."""

    t: float
    m_bid: float = 0.0
    m_ask: float = 0.0
    m_bid_size: float = 0.0
    m_ask_size: float = 0.0
    h_bid: float = 0.0
    h_ask: float = 0.0
    h_bid_size: float = 0.0
    h_ask_size: float = 0.0

    @property
    def m_mid(self) -> float:
        """The maker venue mid; what the maker leg is marked at."""
        return (self.m_bid + self.m_ask) / 2.0 if self.m_bid > 0.0 and self.m_ask > 0.0 else 0.0

    @property
    def h_mid(self) -> float:
        """The hedge venue mid; what the hedge leg is marked at."""
        return (self.h_bid + self.h_ask) / 2.0 if self.h_bid > 0.0 and self.h_ask > 0.0 else 0.0

    @property
    def mid(self) -> float:
        """The cross-venue mid the edge gate is measured against.

        ``maker_fill.merge_pair`` builds it as the mean of the two venue mids, and
        ``maker_inventory.simulate()`` divides the raw price difference by exactly this - so
        the live gate uses the same denominator as the offline replay, not the maker mid.
        """
        m, h = self.m_mid, self.h_mid
        if m > 0.0 and h > 0.0:
            return (m + h) / 2.0
        return m or h

    def ready(self) -> bool:
        return self.m_bid > 0.0 and self.m_ask > 0.0 and self.h_bid > 0.0 and self.h_ask > 0.0


@dataclass(frozen=True)
class QuoteParams:
    """Every knob :class:`QuoteEngine` reads.  Built from :class:`live_limits.Limits`."""

    mode: str  # "improve" (one tick inside) or "join" (at the touch)
    order_usd: float
    max_inv_usd: float
    min_edge_bps: float
    reserve_bps: float
    maker_fee_bps: float
    hedge_fee_bps: float
    tick: float
    decimals: int
    requote_min_s: float = 0.0
    tx_per_min: float | None = None
    # Venue minimums.  All zero reproduces maker_inventory, which does not model them.
    min_size: float = 0.0
    min_notional: float = 0.0
    size_increment: float = 0.0
    # Quote a closing clip that would be under the venue minimum AT the venue minimum, rather
    # than not quoting it.  Defaults to False here because that is what maker_inventory does -
    # from_limits() supplies the configured value (true in the shipped config).
    close_min_flip: bool = False

    @classmethod
    def from_limits(
        cls,
        limits: Limits,
        *,
        tick: float,
        decimals: int,
        min_size: float = 0.0,
        min_notional: float = 0.0,
        size_increment: float = 0.0,
        maker_fee_bps: float = LIGHTER_MAKER_FEE_BPS,
        hedge_fee_bps: float = ASTER_TAKER_FEE_BPS,
        hedged: bool = True,
    ) -> QuoteParams:
        q = limits.quote
        return cls(
            mode=q.mode,
            order_usd=limits.order.max_notional_usd,
            # NOT limits.inventory.max_inventory_usd directly: the hedge doubles the
            # gross two-leg notional, so [exposure] normally binds first.  See
            # live_limits.Limits.effective_inventory_usd.
            max_inv_usd=limits.effective_inventory_usd(hedged=hedged),
            min_edge_bps=q.edge_min_bps,
            reserve_bps=q.reserve_bps,
            maker_fee_bps=maker_fee_bps,
            hedge_fee_bps=hedge_fee_bps,
            tick=tick,
            decimals=decimals,
            requote_min_s=q.requote_min_s,
            tx_per_min=q.tx_per_min,
            min_size=min_size,
            min_notional=min_notional,
            size_increment=size_increment,
            close_min_flip=q.close_min_flip,
        )


@dataclass
class Quote:
    """One resting order, as the engine understands it.

    ``ref`` carries whatever the executor needs to address it again - a ``ClientOrderId`` in a
    live run, nothing in the paper replay.  ``queue`` is only used by :class:`PaperBroker`.
    """

    side: int
    sell: bool
    closing: bool
    price: float
    base: float
    placed_t: float
    queue: float = 0.0
    queue0: float = 0.0
    filled: float = 0.0
    ref: Any = None
    # True when a closing clip was raised to the venue minimum (see close_min_flip).  Such an
    # order deliberately exceeds |q|, so the "the closing clip no longer fits" re-price test
    # must not fire on it every sample.
    min_clip: bool = False

    @property
    def remaining(self) -> float:
        return max(0.0, self.base - self.filled)


PLACE, MODIFY, CANCEL = "place", "modify", "cancel"


@dataclass(frozen=True)
class Action:
    """One venue transaction the engine decided to send."""

    kind: str
    side: int
    quote: Quote | None  # the NEW resting quote for place/modify; None for cancel
    previous: Quote | None = None  # what it replaces (modify/cancel)

    @property
    def sell(self) -> bool:
        return self.side == ASK


class QuoteEngine:
    """The ``maker_inventory.simulate()`` decision loop, one sample at a time.

    ``step()`` is the whole quoting rule.  It owns the resting quotes, the re-price cadence and
    the transaction token bucket, and returns the actions the caller must execute; it never
    touches a venue and never decides the position - ``q`` is passed in, sourced from real
    fills in a live run and from :class:`PaperBroker` in the paper replay.

    Differences from the simulator, all of them restrictions:
      * ``opening_suspended`` switches the opening side off while the unhedged delta is over
        its cap (the simulator hedges every fill instantly and has no such state);
      * ``min_size`` / ``min_notional`` / ``size_increment`` enforce the venue minimums the
        simulator explicitly does not model.
    With those neutral (0 / False) the decisions are identical, which
    ``tests/test_maker_live.py`` asserts fill-for-fill against ``simulate()``.
    """

    def __init__(self, params: QuoteParams) -> None:
        self.p = params
        self.orders: list[Quote | None] = [None, None]
        self.quoted = [0.0, 0.0]  # the touch we last quoted against
        self.last_act = [-1e18, -1e18]
        self.counters: Counter[str] = Counter()
        self.tx_sent = 0
        self.tx_create = 0
        self.tx_modify = 0
        self.tx_cancel = 0
        self._oid = 0
        self._cap = float(params.tx_per_min) if params.tx_per_min else 0.0
        self._base_cap = self._cap
        self._tokens = self._cap
        self._last_refill: float | None = None
        self._throttle_until = 0.0

    # -- transaction budget ---------------------------------------------------------------

    @property
    def tokens(self) -> float:
        return self._tokens

    @property
    def capacity(self) -> float:
        return self._cap

    def throttle(self, t: float, factor: float = THROTTLE_FACTOR,
                 secs: float = THROTTLE_SECS) -> None:
        """Halve the bucket for a while after the venue said "too many requests"."""
        self._cap = max(1.0, self._base_cap * factor)
        self._tokens = min(self._tokens, self._cap)
        self._throttle_until = t + secs

    def _refill(self, t: float) -> None:
        if self._base_cap <= 0.0:
            return
        if self._throttle_until and t >= self._throttle_until:
            self._cap = self._base_cap
            self._throttle_until = 0.0
        if self._last_refill is None:
            self._last_refill = t
            return
        if t > self._last_refill:
            self._tokens = min(self._cap, self._tokens + (t - self._last_refill) * self._cap / 60.0)
            self._last_refill = t

    # -- sizing ---------------------------------------------------------------------------

    def _round_size(self, size: float) -> float:
        """Floor to the venue size increment; flooring keeps a closing clip inside |q|."""
        inc = self.p.size_increment
        if inc <= 0.0:
            return size
        steps = math.floor(size / inc + 1e-9)
        return max(0.0, steps * inc)

    def _venue_minimum(self, price: float) -> float:
        """The smallest order the maker venue accepts at ``price``, rounded up to a step."""
        p = self.p
        need = p.min_size
        if price > 0.0 and p.min_notional > 0.0:
            need = max(need, p.min_notional / price)
        inc = p.size_increment
        if inc <= 0.0:
            return need
        return math.ceil(need / inc - 1e-9) * inc

    def _min_clip_size(self, price: float, q: float) -> float:
        """The venue minimum for a closing side too small to quote, or 0.0 when unsafe.

        ``close_min_flip``.  A residual under the venue minimum cannot be closed passively at
        all - on Lighter the PONS minimum (20 PONS) is close to a whole clip - so the position
        would otherwise sit there until an opening fill on the other side happened to absorb
        it.  Quoting the minimum closes it, carrying the position through zero by at most
        ``venue_min - |q|``, which is itself below the venue minimum and is hedged like any
        other fill.

        Returns 0.0 - i.e. do not quote - when the minimum order would breach the per-order
        notional cap, or when the residual it leaves on the far side of zero would breach the
        inventory cap.  The caps are never widened to make a flip possible.
        """
        p = self.p
        size = self._venue_minimum(price)
        if size <= EPS or size * price > p.order_usd + EPS:
            return 0.0
        residual = size - abs(q)  # what is left on the OTHER side of zero after a full fill
        if residual > 0.0 and residual * price > p.max_inv_usd + EPS:
            return 0.0
        return size

    # -- one sample -----------------------------------------------------------------------

    def step(
        self,
        book: BookSample,
        q: float,
        *,
        opening_suspended: bool = False,
    ) -> list[Action]:
        """Decide both sides for this sample and return the transactions to send.

        Mirrors ``maker_inventory.simulate()`` sample by sample: decide the price, the side
        role (opening / closing) and the clip, compare with what rests, then spend a token.
        An action that finds the bucket empty is dropped and retried at the next sample - the
        resting order keeps its price, its remaining size and its queue position.
        """
        p = self.p
        t = book.t
        self._refill(t)
        actions: list[Action] = []
        min_gap = p.requote_min_s

        for side in (ASK, BID):
            sell = side == ASK
            if sell:
                touch, opposite = book.m_ask, book.m_bid
                hedge_px, tob = book.h_ask, book.m_ask_size
            else:
                touch, opposite = book.m_bid, book.m_ask
                hedge_px, tob = book.h_bid, book.m_bid_size

            mid = book.mid
            ok = touch > 0.0 and opposite > 0.0 and hedge_px > 0.0 and mid > 0.0
            price = 0.0
            closing = False
            size = 0.0
            if ok:
                if p.mode == "improve":
                    price = round(touch - p.tick if sell else touch + p.tick, p.decimals)
                    if (price <= opposite) if sell else (price >= opposite):
                        self.counters["locked"] += 1  # one tick inside would lock the book
                        ok = False
                else:
                    price = round(touch, p.decimals)
            if ok:
                closing = (q > EPS) if sell else (q < -EPS)
                if closing:
                    # Never flip through zero: the clip is capped by what we hold.
                    size = min(p.order_usd / price, abs(q))
                    ok = size > EPS
                else:
                    raw = (price - hedge_px) if sell else (hedge_px - price)
                    edge = raw / mid * 1e4 - p.hedge_fee_bps - p.maker_fee_bps - p.reserve_bps
                    if edge < p.min_edge_bps:
                        self.counters["gated"] += 1
                        ok = False
                    elif abs(q) * price + p.order_usd > p.max_inv_usd + EPS:
                        self.counters["capped"] += 1
                        ok = False
                    elif opening_suspended:
                        # Live-only: the unhedged delta is over its cap, so stop GROWING it.
                        self.counters["suspended"] += 1
                        ok = False
                    else:
                        size = p.order_usd / price
            min_clip = False
            if ok:
                # Live-only: the venue minimums maker_inventory does not model.
                size = self._round_size(size)
                if size <= EPS or size < p.min_size - EPS or size * price < p.min_notional - EPS:
                    bumped = (
                        self._min_clip_size(price, q)
                        if closing and p.close_min_flip
                        else 0.0
                    )
                    if bumped > 0.0:
                        size = bumped
                        min_clip = True
                        self.counters["min_clip"] += 1
                    else:
                        self.counters["too_small"] += 1
                        ok = False

            order = self.orders[side]
            want = 0  # 0 nothing, 1 place, 2 re-price (modify), 3 cancel
            if not ok:
                if order is not None:
                    want = 3
            elif order is None:
                want = 1
            elif (
                order.closing != closing
                or (
                    closing
                    and not order.min_clip  # a min-clip order exceeds |q| on purpose
                    and order.base - order.filled > abs(q) + EPS
                )
            ):
                want = 2  # the side flipped, or the closing clip no longer fits |q|
            elif order.price != price:
                ready = min_gap <= 0.0 or t - self.last_act[side] >= min_gap - EPS
                if ready:
                    want = 2
                elif (order.price <= opposite) if sell else (order.price >= opposite):
                    want = 3  # the cadence would hold a quote that now locks the book

            if want and self._cap > 0.0:
                if self._tokens < 1.0:
                    self.counters["deferred"] += 1  # retried at the next sample
                    want = 0
                else:
                    self._tokens -= 1.0

            if not want:
                continue

            self.tx_sent += 1
            if want == 3:
                self.tx_cancel += 1
                self.orders[side] = None
                actions.append(Action(CANCEL, side, None, order))
                continue

            self._oid += 1
            fresh = Quote(
                side=side,
                sell=sell,
                closing=closing,
                price=price,
                base=size,
                placed_t=t,
                queue=tob if p.mode == "join" else 0.0,
                queue0=tob if p.mode == "join" else 0.0,
                ref=order.ref if want == 2 else None,
                min_clip=min_clip,
            )
            if want == 1:
                self.tx_create += 1
                actions.append(Action(PLACE, side, fresh, None))
            else:
                self.tx_modify += 1
                actions.append(Action(MODIFY, side, fresh, order))
            self.orders[side] = fresh
            self.quoted[side] = touch
            self.last_act[side] = t
        return actions

    def drop(self, side: int) -> Quote | None:
        """Forget the resting quote on ``side`` (it completed, or the venue closed it)."""
        order = self.orders[side]
        self.orders[side] = None
        return order

    def cancel_all(self) -> list[Action]:
        """Actions that pull every resting quote; used by the cleanup path."""
        out: list[Action] = []
        for side in (ASK, BID):
            order = self.orders[side]
            if order is not None:
                self.orders[side] = None
                self.tx_sent += 1
                self.tx_cancel += 1
                out.append(Action(CANCEL, side, None, order))
        return out


# ------------------------------------------------------------------ paper fills


@dataclass(frozen=True)
class SimFill:
    """One simulated maker fill: what the tape gave us at our resting price."""

    t: float
    side: int
    sell: bool
    closing: bool
    price: float
    base: float
    complete: bool


class PaperBroker:
    """``maker_inventory``'s fill model, driven by the live trade tape instead of a CSV.

    A trade with the matching aggressor (a BUY aggressor hits our ask) at or through our price
    first eats whatever queue was ahead of us when we joined, then fills us; partials
    accumulate and a completed order is replaced at the next sample, exactly as the simulator
    replays it.  The engine and the broker share the same :class:`Quote` objects.
    """

    def __init__(self, engine: QuoteEngine) -> None:
        self._engine = engine

    def on_trade(self, price: float, size: float, aggressor_buy: bool, t: float) -> list[SimFill]:
        side = ASK if aggressor_buy else BID  # a BUY aggressor hits our ask
        order = self._engine.orders[side]
        if order is None:
            return []
        through = (price >= order.price) if order.sell else (price <= order.price)
        if not through:
            return []
        rest = size
        if order.queue > 0.0:  # makers ahead of us take it first
            eaten = order.queue if order.queue < rest else rest
            order.queue -= eaten
            rest -= eaten
        if rest <= 0.0:
            return []
        room = order.base - order.filled
        got = rest if rest < room else room
        if got <= 0.0:
            return []
        order.filled += got
        complete = order.filled >= order.base - EPS
        if complete:
            self._engine.drop(side)
        return [SimFill(t, side, order.sell, order.closing, order.price, got, complete)]


# ------------------------------------------------------------------ hedging


@dataclass(frozen=True)
class HedgePlan:
    """One hedge order to send: LIMIT IOC on the hedge venue, never MARKET."""

    sell: bool
    qty: float
    price: float
    reason: str  # "normal" | "rounded-up"


class HedgeManager:
    """Carries the delta filled on the maker venue but not yet offset on the hedge venue.

    ``delta`` is signed maker-venue base: positive means we are long on Lighter and must SELL
    that much on Aster.  A residual below the hedge venue's minimum notional is aggregated
    rather than sent as dust; if it is still there after ``max_wait_s`` it is rounded UP to
    the venue minimum - but only when doing so actually shrinks the exposure.

    That last condition is a deliberate refinement of the brief.  Rounding a residual of
    ``d`` up to the venue minimum ``m`` leaves ``m - d`` of the OPPOSITE sign, which is
    smaller than ``d`` only when ``m < 2d``.  Without the test, a dust residual would be
    over-hedged into a slightly different dust residual every ``max_wait_s`` forever, burning
    taker fees on both legs.  When the round-up would not help, the dust is carried - it is by
    construction below the hedge venue minimum, i.e. far inside ``max_unhedged_usd``.
    """

    def __init__(
        self,
        *,
        min_qty: float,
        size_increment: float,
        min_notional: float,
        aggregate_floor: float,
        max_wait_s: float,
        slippage_bps: float,
    ) -> None:
        self.delta = 0.0
        self.min_qty = max(min_qty, size_increment)
        self.size_increment = size_increment if size_increment > 0.0 else min_qty
        self.min_notional = min_notional
        self.aggregate_floor = aggregate_floor
        self.max_wait_s = max_wait_s
        self.slippage_bps = slippage_bps
        self.waiting_since: float | None = None
        self.over_since: float | None = None
        self.rounded_up = 0
        self.carried = 0

    # -- state ----------------------------------------------------------------------------

    def add(self, signed_base: float, t: float) -> None:
        """Record a maker fill that now needs hedging."""
        self.delta += signed_base
        if abs(self.delta) <= EPS:
            self.delta = 0.0
            self.waiting_since = None
        elif self.waiting_since is None:
            self.waiting_since = t

    def settle(self, signed_hedge_base: float, t: float) -> None:
        """Record a hedge fill; ``signed_hedge_base`` is signed in HEDGE venue terms."""
        # A SELL of x on the hedge venue offsets +x of long maker exposure: delta += x.
        self.delta += signed_hedge_base
        if abs(self.delta) <= EPS:
            self.delta = 0.0
            self.waiting_since = None
        else:
            self.waiting_since = t

    def notional(self, mid_h: float) -> float:
        return abs(self.delta) * mid_h

    def breach_seconds(self, t: float, mid_h: float, cap_usd: float) -> float:
        """How long the unhedged delta has been over ``cap_usd``; 0 while it is inside."""
        if self.notional(mid_h) > cap_usd + EPS:
            if self.over_since is None:
                self.over_since = t
            return t - self.over_since
        self.over_since = None
        return 0.0

    # -- planning -------------------------------------------------------------------------

    def _ceil_to_minimum(self, price: float) -> float:
        """The smallest hedge order the venue accepts at ``price``."""
        inc = self.size_increment
        need = self.min_qty
        if price > 0.0 and self.min_notional > 0.0:
            need = max(need, self.min_notional / price)
        steps = math.ceil(need / inc - 1e-9) if inc > 0.0 else 1.0
        return max(inc, steps * inc)

    def _floor_to_step(self, qty: float) -> float:
        inc = self.size_increment
        if inc <= 0.0:
            return qty
        return math.floor(qty / inc + 1e-9) * inc

    def plan(self, t: float, h_bid: float, h_ask: float, *, force: bool = False) -> HedgePlan | None:
        """The hedge order to send now, or ``None`` while the residual is still waiting.

        ``force`` (shutdown / kill path) skips the aggregation wait and hedges anything that
        can legally be sent.
        """
        if abs(self.delta) <= EPS or h_bid <= 0.0 or h_ask <= 0.0:
            return None
        sell = self.delta > 0.0  # long on the maker venue -> sell on the hedge venue
        touch = h_bid if sell else h_ask
        price = touch * (1.0 - self.slippage_bps / 1e4) if sell else touch * (
            1.0 + self.slippage_bps / 1e4
        )
        if price <= 0.0:
            return None

        want = abs(self.delta)
        qty = self._floor_to_step(want)
        if qty >= self.min_qty - EPS and qty * price >= self.min_notional - EPS:
            mid = (h_bid + h_ask) / 2.0
            if force or qty * mid >= self.aggregate_floor - EPS:
                return HedgePlan(sell=sell, qty=qty, price=price, reason="normal")
            if self.waiting_since is None:
                self.waiting_since = t
            if t - self.waiting_since >= self.max_wait_s - EPS:
                return HedgePlan(sell=sell, qty=qty, price=price, reason="normal")
            return None

        # Below the venue minimum.  Wait, then round up - but only when that shrinks |delta|.
        if self.waiting_since is None:
            self.waiting_since = t
            return None
        if not force and t - self.waiting_since < self.max_wait_s - EPS:
            return None
        if want < self.size_increment - EPS:
            self.carried += 1  # less than one venue step: nothing legal to send at all
            return None
        up = self._ceil_to_minimum(price)
        if up >= 2.0 * want - EPS:
            self.carried += 1  # rounding up would leave a bigger residual than the dust
            return None
        self.rounded_up += 1
        return HedgePlan(sell=sell, qty=up, price=price, reason="rounded-up")


# ------------------------------------------------------------------ daily state


@dataclass
class DailyState:
    """Fill and transaction counters that survive a restart.

    Persisted to ``reports/live/state_<env>_<symbol>.json``.  The counters roll over only when
    the UTC day recorded in the file changes, so restarting the process inside a day cannot
    hand the run a fresh budget - which is the entire point of the file.
    """

    path: Path
    day: str
    fills: int = 0
    tx: int = 0
    realized_net_usd: float = 0.0
    started: str = ""

    @staticmethod
    def today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    @classmethod
    def load(cls, path: Path, *, day: str | None = None) -> DailyState:
        day = day or cls.today()
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls(path=path, day=day, started=datetime.now(timezone.utc).isoformat())
        if str(raw.get("day")) != day:  # a new UTC day: the budget resets
            return cls(path=path, day=day, started=datetime.now(timezone.utc).isoformat())
        return cls(
            path=path,
            day=day,
            fills=int(raw.get("fills", 0)),
            tx=int(raw.get("tx", 0)),
            realized_net_usd=float(raw.get("realized_net_usd", 0.0)),
            started=str(raw.get("started", "")),
        )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "day": self.day,
            "fills": self.fills,
            "tx": self.tx,
            "realized_net_usd": round(self.realized_net_usd, 8),
            "started": self.started,
            "updated": datetime.now(timezone.utc).isoformat(),
        }
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)


# ------------------------------------------------------------------ p&l monitor


class PnLMonitor:
    """Average-cost round-trip matching on both legs, plus the mark of what is open.

        net = spread_capture + hedge_cost - fees + residual_mtm

    exactly as ``maker_inventory`` decomposes it, so a live session and an offline replay of
    the same window are directly comparable.
    """

    def __init__(self, *, maker_fee_bps: float, hedge_fee_bps: float, trip_window: int) -> None:
        self.maker_fee_bps = maker_fee_bps
        self.hedge_fee_bps = hedge_fee_bps
        self.m = AvgCostBook()
        self.h = AvgCostBook()
        self.spread_capture = 0.0
        self.hedge_cost = 0.0
        self.trips = 0
        self.matched_usd = 0.0
        self.trip_window = max(1, trip_window)
        self.trip_bps: deque[float] = deque(maxlen=self.trip_window)
        self.consecutive_losses = 0
        self.fills = [0, 0]  # BID, ASK counts on the maker venue
        self.filled_usd = [0.0, 0.0]
        self.hedge_fills = 0
        self.hedge_usd = 0.0
        self._ewma: float | None = None

    # -- fills ----------------------------------------------------------------------------

    def maker_fill(self, sell: bool, base: float, price: float) -> tuple[float, float, float]:
        """Book one maker-venue fill; returns (realised, round-trip fee, matched base)."""
        signed = -base if sell else base
        fee = self.maker_fee_bps / 1e4 * base * price
        realized, rt_fee, matched = self.m.trade(signed, price, fee)
        self.spread_capture += realized
        side = ASK if sell else BID
        self.fills[side] += 1
        self.filled_usd[side] += base * price
        if matched > EPS:
            self._record_trip(realized, rt_fee, matched * price)
        return realized, rt_fee, matched

    def hedge_fill(self, sell: bool, base: float, price: float) -> tuple[float, float, float]:
        """Book one hedge-venue fill (``sell`` is the side on the HEDGE venue)."""
        signed = -base if sell else base
        fee = self.hedge_fee_bps / 1e4 * base * price
        realized, rt_fee, matched = self.h.trade(signed, price, fee)
        self.hedge_cost += realized
        self.hedge_fills += 1
        self.hedge_usd += base * price
        return realized, rt_fee, matched

    def _record_trip(self, realized: float, rt_fee: float, notional: float) -> None:
        if notional <= 0.0:
            return
        self.trips += 1
        self.matched_usd += notional
        net_bps = (realized - rt_fee) / notional * 1e4
        self.trip_bps.append(net_bps)
        alpha = 2.0 / (self.trip_window + 1.0)
        self._ewma = net_bps if self._ewma is None else alpha * net_bps + (1 - alpha) * self._ewma
        if net_bps < 0.0:
            self.consecutive_losses += 1
        else:
            self.consecutive_losses = 0

    # -- marks ----------------------------------------------------------------------------

    @property
    def fees(self) -> float:
        return self.m.fees + self.h.fees

    @property
    def trip_bps_ewma(self) -> float | None:
        return self._ewma

    def residual_mtm(self, mid_m: float, mid_h: float) -> float:
        out = 0.0
        if mid_m > 0.0:
            out += self.m.pos * (mid_m - self.m.avg)
        if mid_h > 0.0:
            out += self.h.pos * (mid_h - self.h.avg)
        return out

    def realized_net(self) -> float:
        """Everything already closed, after fees; what the state file carries across a restart."""
        return self.spread_capture + self.hedge_cost - self.fees

    def net(self, mid_m: float, mid_h: float) -> float:
        return self.realized_net() + self.residual_mtm(mid_m, mid_h)

    def exposure_usd(self, mid_m: float, mid_h: float) -> float:
        """|maker position| + |hedge position| in USD, the [exposure] cap's subject."""
        return abs(self.m.pos) * mid_m + abs(self.h.pos) * mid_h


# ------------------------------------------------------------------ kill switch


@dataclass(frozen=True)
class KillCheck:
    """One kill-switch line: its name, how close we are, and whether it tripped."""

    name: str
    value: float
    threshold: float
    tripped: bool
    detail: str = ""

    def margin(self) -> str:
        return f"{self.name} {self.value:.4g}/{self.threshold:.4g}"


class KillSwitch:
    """Every condition that must stop the run, evaluated together so the log shows margins."""

    def __init__(self, limits: Limits, state: DailyState) -> None:
        self.k = limits.kill
        self.daily = limits.daily
        self.exposure = limits.exposure
        self.state = state
        self.reason: str | None = None

    def evaluate(
        self,
        *,
        pnl: PnLMonitor,
        mid_m: float,
        mid_h: float,
        hedge_breach_s: float,
        day_fills: int,
        day_tx: int,
    ) -> list[KillCheck]:
        net = self.state.realized_net_usd + pnl.net(mid_m, mid_h) - pnl.realized_net()
        # ``state.realized_net_usd`` already holds this process's realised total (it is written
        # back on every fill), so the sum above is "everything since the state file's day
        # start" without double counting this session.
        checks = [
            KillCheck("day_fills", day_fills, self.daily.max_fills,
                      day_fills >= self.daily.max_fills),
            KillCheck("day_tx", day_tx, self.daily.max_tx, day_tx >= self.daily.max_tx),
            KillCheck("net_usd", net, -self.k.max_loss_usd, net <= -self.k.max_loss_usd),
            KillCheck("losing_trips", pnl.consecutive_losses,
                      self.k.max_consecutive_losing_trips,
                      pnl.consecutive_losses >= self.k.max_consecutive_losing_trips),
            KillCheck("exposure_usd", pnl.exposure_usd(mid_m, mid_h),
                      self.exposure.max_total_notional_usd,
                      pnl.exposure_usd(mid_m, mid_h) > self.exposure.max_total_notional_usd),
            KillCheck("hedge_fail_s", hedge_breach_s, self.k.max_hedge_fail_s,
                      hedge_breach_s > self.k.max_hedge_fail_s),
        ]
        ewma = pnl.trip_bps_ewma
        # The EWMA only binds once the window is full: one bad dust round trip must not stop a
        # session that has barely started.
        tripped = (
            ewma is not None
            and pnl.trips >= self.k.trip_window
            and ewma < self.k.min_trip_net_bps_ewma
        )
        checks.append(
            KillCheck("trip_bps_ewma", ewma if ewma is not None else 0.0,
                      self.k.min_trip_net_bps_ewma, tripped,
                      detail=f"{pnl.trips} trips"),
        )
        for check in checks:
            if check.tripped and self.reason is None:
                self.reason = (
                    f"{check.name} breached: {check.value:.4g} vs limit {check.threshold:.4g}"
                    + (f" ({check.detail})" if check.detail else "")
                )
        return checks


# ------------------------------------------------------------------ csv sink


class CsvSink:
    """Append-only csv; writes the header only when the file is new (restart-safe)."""

    def __init__(self, path: Path, header: list[str]) -> None:
        self.path = path
        self.header = header
        self._handle = None
        self._writer = None
        self.rows = 0

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        self._handle = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        if fresh:
            self._writer.writerow(self.header)
            self._handle.flush()

    def write(self, row: list[object]) -> None:
        if self._writer is None:
            return
        self._writer.writerow(row)
        self._handle.flush()
        self.rows += 1

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None
            self._writer = None


FILLS_HEADER = [
    "ts", "leg", "venue", "side", "kind", "price", "base", "usd", "q_after",
    "delta_after", "realized", "rt_fee", "mode",
]
PNL_HEADER = [
    "ts", "fills_ask", "fills_bid", "filled_usd", "q", "q_usd", "delta", "delta_usd",
    "trips", "trip_bps_ewma", "spread_capture", "hedge_cost", "fees", "resid_mtm",
    "net_usd", "tx_sent", "tx_per_min", "tokens", "day_fills", "day_tx",
]


# ------------------------------------------------------------------ strategy


def _guarded(method: Callable[..., None]) -> Callable[..., None]:
    """Log-and-fail wrapper for strategy callbacks (``exec_probe._guarded``).

    NautilusTrader swallows exceptions raised inside ``on_*`` handlers.  Every handler is
    wrapped so an exception is logged with its traceback and moves the run into its failure
    state instead of vanishing.
    """

    @functools.wraps(method)
    def wrapper(self: LighterMaker, *args: object, **kwargs: object) -> None:
        try:
            return method(self, *args, **kwargs)
        except Exception:
            self.record_failure(
                f"unhandled exception in {method.__name__}:\n{traceback.format_exc()}",
            )
            return None

    return wrapper


try:  # The strategy base class is only needed when a node is actually built.
    from nautilus_trader.trading import Strategy
    from nautilus_trader.trading import StrategyConfig
except ImportError as _exc:  # pragma: no cover
    raise SystemExit(
        "[maker] nautilus_trader is not importable from this interpreter; "
        "use the repo .venv (see CLAUDE.md)",
    ) from _exc

from nautilus_trader.model import BookType  # noqa: E402
from nautilus_trader.model import ClientId  # noqa: E402
from nautilus_trader.model import InstrumentId  # noqa: E402
from nautilus_trader.model import OrderSide  # noqa: E402
from nautilus_trader.model import TimeInForce  # noqa: E402


class MakerLiveConfig(StrategyConfig):
    """Configuration for :class:`LighterMaker`."""

    _CUSTOM_FIELDS = (
        "plan", "limits", "mode", "env", "maker_client_id", "hedge_client_id",
        "fills_csv", "pnl_csv", "state_path", "hedge_enabled", "deadline_ts",
    )

    def __new__(cls, *args: object, **kwargs: object) -> Self:
        """Create a new instance, hiding the custom fields from the pyo3 base config."""
        for name in cls._CUSTOM_FIELDS:
            kwargs.pop(name, None)
        return super().__new__(cls, *args, **kwargs)

    def __init__(
        self,
        *,
        plan: SymbolPlan,
        limits: Limits,
        mode: str,
        env: str,
        maker_client_id: str,
        hedge_client_id: str,
        fills_csv: Path,
        pnl_csv: Path,
        state_path: Path,
        hedge_enabled: bool = True,
        deadline_ts: float = 0.0,
        **_kwargs: object,
    ) -> None:
        """Initialize the configuration."""
        super().__init__()
        self.plan = plan
        self.limits = limits
        self.mode = mode  # "paper" or "live"
        self.env = env
        self.maker_client_id = maker_client_id
        self.hedge_client_id = hedge_client_id
        self.fills_csv = fills_csv
        self.pnl_csv = pnl_csv
        self.state_path = state_path
        self.hedge_enabled = hedge_enabled
        self.deadline_ts = deadline_ts


class LighterMaker(Strategy):
    """Two-sided maker on Lighter, taker-hedged on Aster, under the stage-3 hard limits."""

    def __init__(self, config: MakerLiveConfig) -> None:
        super().__init__(config)
        self._cfg = config
        self._limits: Limits = config.limits
        self._plan: SymbolPlan = config.plan
        self._paper = config.mode == "paper"
        self._hedge_on = config.hedge_enabled
        self._maker_id = InstrumentId.from_str(config.plan.maker_id)
        self._hedge_id = InstrumentId.from_str(config.plan.hedge_id)
        self._maker_client = ClientId.from_str(config.maker_client_id)
        self._hedge_client = ClientId.from_str(config.hedge_client_id)

        self._book = BookSample(t=0.0)
        self._maker_inst: Any = None
        self._hedge_inst: Any = None
        self._engine: QuoteEngine | None = None
        self._broker: PaperBroker | None = None
        self._hedger: HedgeManager | None = None
        self._pnl: PnLMonitor | None = None
        self._kill: KillSwitch | None = None
        self._state = DailyState.load(config.state_path)
        self._state_realized_at_start = self._state.realized_net_usd

        self._q = 0.0  # signed base position on the maker venue, from real fills
        self._started_t = 0.0
        self._tx_at_last_status = 0
        self._t_last_status = 0.0
        self._failures: list[str] = []
        self._frozen = False
        self._finished = False
        self._kill_reason: str | None = None
        self._live_orders: dict[Any, int] = {}  # ClientOrderId -> side, our Lighter quotes
        self._hedge_orders: list[Any] = []  # ClientOrderId of every hedge order we sent
        self._leftovers: list[str] = []
        self._leftover_source = "no cleanup has run"
        self._cancel_errors: list[str] = []
        self._last_checks: list[KillCheck] = []
        self._pending_hedge_qty = 0.0  # hedge base already sent but not yet reported filled
        self._orders_sent = 0  # real orders submitted; a retry is only safe while this is 0
        self._data_seen = 0  # quote/trade messages received, for the connect-failure test

        self._fills = CsvSink(config.fills_csv, FILLS_HEADER)
        self._pnl_csv = CsvSink(config.pnl_csv, PNL_HEADER)
        self.done_event = threading.Event()
        self.cleanup_done_event = threading.Event()
        self.summary_line = ""

    # -- results ---------------------------------------------------------------------------

    @property
    def failures(self) -> list[str]:
        return list(self._failures)

    @property
    def leftovers(self) -> list[str]:
        return list(self._leftovers)

    @property
    def cancel_errors(self) -> list[str]:
        return list(self._cancel_errors)

    @property
    def kill_reason(self) -> str | None:
        return self._kill_reason

    @property
    def orders_sent(self) -> int:
        """Real orders submitted to a venue.  Zero means the run can safely be retried."""
        return self._orders_sent

    @property
    def received(self) -> bool:
        return self._data_seen > 0

    @property
    def exit_code(self) -> int:
        if self._leftovers or self._failures:
            return EXIT_FAILED
        if self._kill_reason is not None:
            return EXIT_KILLED
        return EXIT_OK

    def _log_safe(self, level: str, message: str) -> None:
        """Log without letting a re-entrant borrow abort the caller (exec_probe pattern)."""
        flat = " | ".join(message.splitlines())
        try:
            getattr(self.log, level)(flat)
        except RuntimeError:
            print(message, file=sys.stderr, flush=True)

    def _note_failure(self, reason: str) -> None:
        self._failures.append(reason)
        print(f"[maker] FAILED: {reason}", file=sys.stderr, flush=True)
        self._log_safe("error", f"[maker] FAILED: {reason}")

    def record_failure(self, reason: str) -> None:
        """Record a failure, log it, and finish.  Public: ``_guarded`` calls it."""
        self._note_failure(reason)
        self._finish("failure")

    # -- lifecycle -------------------------------------------------------------------------

    @_guarded
    def on_start(self) -> None:
        """Load both instruments, size the engine against their filters, then subscribe."""
        self._maker_inst = self.cache.instrument(self._maker_id)
        if self._maker_inst is None:
            self.record_failure(f"maker instrument {self._maker_id} not in the cache")
            return
        self._hedge_inst = self.cache.instrument(self._hedge_id)
        if self._hedge_inst is None and self._hedge_on:
            self.record_failure(f"hedge instrument {self._hedge_id} not in the cache")
            return

        tick = float(self._maker_inst.price_increment.as_decimal())
        decimals = int(self._maker_inst.price_precision)
        size_inc = float(self._maker_inst.size_increment.as_decimal())
        min_qty = (
            float(self._maker_inst.min_quantity.as_decimal())
            if self._maker_inst.min_quantity is not None
            else size_inc
        )
        min_notional = (
            float(self._maker_inst.min_notional.as_decimal())
            if self._maker_inst.min_notional is not None
            else 0.0
        )
        # Refuse rather than size up when the smallest legal order breaks the per-order cap.
        mark = float(getattr(self._maker_inst, "info", {}).get("mark_price", 0.0) or 0.0)
        probe_px = mark if mark > 0.0 else 0.0
        venue_min_usd = max(min_notional, min_qty * probe_px)
        try:
            self._limits.order.check_venue_minimum(
                venue_min_usd, what=f"{self._maker_id} minimum order",
            )
        except LimitsError as exc:
            self.record_failure(str(exc))
            return

        self._engine = QuoteEngine(
            QuoteParams.from_limits(
                self._limits,
                tick=tick,
                decimals=decimals,
                min_size=min_qty,
                min_notional=min_notional,
                size_increment=size_inc,
                hedged=self._hedge_on,
            ),
        )
        self._broker = PaperBroker(self._engine) if self._paper else None
        if self._hedge_on and self._hedge_inst is not None:
            h_inc = float(self._hedge_inst.size_increment.as_decimal())
            h_min_qty = (
                float(self._hedge_inst.min_quantity.as_decimal())
                if self._hedge_inst.min_quantity is not None
                else h_inc
            )
            h_min_notional = (
                float(self._hedge_inst.min_notional.as_decimal())
                if self._hedge_inst.min_notional is not None
                else 0.0
            )
            self._hedger = HedgeManager(
                min_qty=h_min_qty,
                size_increment=h_inc,
                min_notional=h_min_notional,
                aggregate_floor=self._limits.hedge.aggregate_floor(h_min_notional),
                max_wait_s=self._limits.hedge.max_wait_s,
                slippage_bps=self._limits.hedge.slippage_bps,
            )
        self._pnl = PnLMonitor(
            maker_fee_bps=LIGHTER_MAKER_FEE_BPS,
            hedge_fee_bps=ASTER_TAKER_FEE_BPS if self._hedge_on else 0.0,
            trip_window=self._limits.kill.trip_window,
        )
        self._kill = KillSwitch(self._limits, self._state)

        for sink in (self._fills, self._pnl_csv):
            sink.open()

        self.log.info(
            f"[maker] maker {self._maker_id} tick={tick} size_inc={size_inc} "
            f"min_qty={min_qty} min_notional={min_notional}",
        )
        if self._hedge_inst is not None:
            self.log.info(
                f"[maker] hedge {self._hedge_id} tick={self._hedge_inst.price_increment} "
                f"size_inc={self._hedge_inst.size_increment} "
                f"min_qty={self._hedge_inst.min_quantity} "
                f"min_notional={self._hedge_inst.min_notional}",
            )
        self.log.info(
            f"[maker] effective |inventory| cap "
            f"{self._engine.p.max_inv_usd:.2f} USD "
            f"(configured {self._limits.inventory.max_inventory_usd:g}, "
            f"exposure cap {self._limits.exposure.max_total_notional_usd:g} over 2 legs); "
            f"clip {self._engine.p.order_usd:g} USD",
        )
        self.log.info(
            f"[maker] daily budget carried in: fills={self._state.fills}/"
            f"{self._limits.daily.max_fills} tx={self._state.tx}/{self._limits.daily.max_tx} "
            f"realized={self._state.realized_net_usd:.4f} USD (day {self._state.day})",
        )

        legs = [(self._maker_id, self._maker_client)]
        if self._hedge_inst is not None:
            legs.append((self._hedge_id, self._hedge_client))
        for instrument_id, client_id in legs:
            self.subscribe_quotes(instrument_id, client_id=client_id)
            self.subscribe_trades(instrument_id, client_id=client_id)
            self.subscribe_book_deltas(
                instrument_id, BookType.L2_MBP, client_id=client_id, managed=True,
            )
        self.log.info("[maker] subscribed quotes + trades + managed deltas on both legs")

        if not self._paper:
            # Nothing of ours may be resting when the first decision runs.
            self._cancel_stale_orders()

        now = self.clock.utc_now()
        self._started_t = self.clock.timestamp_ns() / 1e9
        self._t_last_status = self._started_t
        self.clock.set_timer("maker-decide", timedelta(seconds=DECIDE_SECS), start_time=now)
        self.clock.set_timer("maker-status", timedelta(seconds=STATUS_SECS), start_time=now)

    def _cancel_stale_orders(self) -> None:
        """Cancel any order of ours still open on the maker instrument at start."""
        try:
            open_orders = self.cache.orders_open(instrument_id=self._maker_id)
        except Exception:
            self._log_safe("warning", "[maker] could not read open orders at start")
            return
        for order in open_orders or ():
            self._log_safe(
                "warning",
                f"[maker] start-up cancel of a pre-existing order {order.client_order_id} "
                f"status={order.status}",
            )
            try:
                self.cancel_order(order.client_order_id)
            except Exception as exc:  # noqa: BLE001
                self._cancel_errors.append(f"{order.client_order_id}: {exc}")

    @_guarded
    def on_stop(self) -> None:
        """Freeze, cancel our quotes, hedge the residual, and report anything left open."""
        self._frozen = True
        self._log_safe("info", "[maker] stopping")
        self._run_cleanup("stop")
        outstanding = self._outstanding_orders()
        self._leftovers = [f"{coid}={status}" for coid, status in outstanding]
        self._leftover_source = "local cache snapshot taken at stop - NOT venue-confirmed"
        if outstanding:
            message = (
                f"[maker] LEFTOVER: {len(outstanding)} order(s) not confirmed closed at stop: "
                f"{', '.join(self._leftovers)} ({self._leftover_source})"
            )
            print(message, file=sys.stderr, flush=True)
            self._log_safe("error", message)
        for sink in (self._fills, self._pnl_csv):
            sink.close()
        self._persist_state()

    # -- market data -----------------------------------------------------------------------

    @_guarded
    def on_quote(self, quote) -> None:
        """Keep the newest touch of each leg; the decision timer reads this sample."""
        self._data_seen += 1
        if quote.instrument_id == self._maker_id:
            self._book.m_bid = float(quote.bid_price)
            self._book.m_ask = float(quote.ask_price)
            self._book.m_bid_size = float(quote.bid_size)
            self._book.m_ask_size = float(quote.ask_size)
        elif quote.instrument_id == self._hedge_id:
            self._book.h_bid = float(quote.bid_price)
            self._book.h_ask = float(quote.ask_price)
            self._book.h_bid_size = float(quote.bid_size)
            self._book.h_ask_size = float(quote.ask_size)

    @_guarded
    def on_trade(self, trade) -> None:
        """Paper mode only: the maker-venue tape is what fills our simulated quotes."""
        if not self._paper or self._frozen or trade.instrument_id != self._maker_id:
            return
        if self._broker is None:
            return
        aggressor = trade.aggressor_side.name
        if aggressor == "BUY":
            buy = True
        elif aggressor == "SELL":
            buy = False
        else:  # NO_AGGRESSOR: maker_fill counts a side-less print as a SELL aggressor
            buy = False
        t = self.clock.timestamp_ns() / 1e9
        for fill in self._broker.on_trade(float(trade.price), float(trade.size), buy, t):
            self._on_maker_fill(fill.sell, fill.base, fill.price, t, closing=fill.closing)

    @_guarded
    def on_book_deltas(self, deltas) -> None:
        """Managed deltas keep a full L2 book in the cache; the touch still comes from quotes."""
        return

    # -- the decision tick -----------------------------------------------------------------

    @_guarded
    def on_time_event(self, event) -> None:
        name = event.name
        if name == "maker-decide":
            self._decide()
        elif name == "maker-status":
            self._status()

    def _decide(self) -> None:
        """One 1 s sample: re-decide both sides, hedge what is pending, check the kill switch."""
        if self._frozen or self._engine is None or self._pnl is None or self._kill is None:
            return
        t = self.clock.timestamp_ns() / 1e9
        self._book.t = t

        # The kill switch runs first, so a breach never gets another quote out.
        mid_m, mid_h = self._marks()
        breach_s = 0.0
        if self._hedger is not None and mid_h > 0.0:
            breach_s = self._hedger.breach_seconds(
                t, mid_h, self._limits.inventory.max_unhedged_usd,
            )
        self._last_checks = self._kill.evaluate(
            pnl=self._pnl,
            mid_m=mid_m,
            mid_h=mid_h,
            hedge_breach_s=breach_s,
            day_fills=self._day_fills(),
            day_tx=self._day_tx(),
        )
        if self._kill.reason is not None:
            self._trigger_kill(self._kill.reason)
            return

        if self._cfg.deadline_ts and t >= self._cfg.deadline_ts:
            self._log_safe("info", "[maker] deadline reached; flattening and stopping")
            self._finish("deadline")
            return

        # Hedge before quoting: the delta gates the opening side.
        self._pump_hedge(t)

        if not self._book.ready():
            return
        if t - self._started_t < STARTUP_GRACE_SECS:
            return  # both books must have settled before the first quote goes out

        suspended = False
        if self._hedger is not None and mid_h > 0.0:
            suspended = (
                self._hedger.notional(mid_h) > self._limits.inventory.max_unhedged_usd + EPS
            )
        elif self._hedge_on and self._hedger is None:
            suspended = True  # hedging was requested but no hedge instrument: never open

        for action in self._engine.step(self._book, self._q, opening_suspended=suspended):
            self._execute(action, t)

    def _marks(self) -> tuple[float, float]:
        """(maker mid, hedge mid) - each leg is marked on its own venue."""
        m_mid = self._book.m_mid
        return m_mid, self._book.h_mid or m_mid

    def _day_fills(self) -> int:
        return self._state.fills

    def _day_tx(self) -> int:
        return self._state.tx

    # -- executing the engine's actions ----------------------------------------------------

    def _execute(self, action: Action, t: float) -> None:
        """Send one engine action, or simulate it in paper mode."""
        self._state.tx += 1
        if self._paper:
            return  # the engine already holds the new quote; PaperBroker fills it
        try:
            if action.kind == CANCEL:
                previous = action.previous
                if previous is not None and previous.ref is not None:
                    self.cancel_order(previous.ref)
                return
            if action.kind == MODIFY and action.previous is not None and action.quote is not None:
                # One L2ModifyOrder transaction, which is why re-pricing costs the same as a
                # place rather than a cancel plus a create.
                #
                # UNVERIFIED (needs a testnet run with credentials): whether Lighter reads the
                # new `quantity` as the order's TOTAL size or as the size still to fill, when
                # the order has already partially filled.  We send the full fresh clip.  If the
                # venue treats it as a remainder the resting size would be larger than intended
                # - still inside the per-order risk cap, because the clip itself is capped, but
                # it must be checked on testnet before mainnet.  The position is never derived
                # from this number: `q` comes only from OrderFilled events.
                ref = action.previous.ref
                if ref is not None:
                    action.quote.ref = ref
                    self.modify_order(
                        ref,
                        quantity=self._maker_inst.make_qty(_dec(action.quote.base)),
                        price=self._maker_inst.make_price(_dec(action.quote.price)),
                    )
                    return
                # No id to modify (a place that never reached the venue): fall through.
            quote = action.quote
            if quote is None:
                return
            order = self.order_factory.limit(
                instrument_id=self._maker_id,
                order_side=OrderSide.SELL if quote.sell else OrderSide.BUY,
                quantity=self._maker_inst.make_qty(_dec(quote.base)),
                price=self._maker_inst.make_price(_dec(quote.price)),
                time_in_force=TimeInForce.GTC,
                post_only=True,
            )
            quote.ref = order.client_order_id
            self._live_orders[order.client_order_id] = quote.side
            self._orders_sent += 1
            self.submit_order(order, client_id=self._maker_client)
        except Exception as exc:  # noqa: BLE001 - recorded, never swallowed
            self._note_failure(f"{action.kind} on side {action.side} failed: {exc!r}")

    # -- fills -----------------------------------------------------------------------------

    def _on_maker_fill(
        self, sell: bool, base: float, price: float, t: float, *, closing: bool | None = None,
    ) -> None:
        """One maker-venue fill: move the inventory, book it, and arm the hedge."""
        if self._pnl is None:
            return
        realized, rt_fee, _matched = self._pnl.maker_fill(sell, base, price)
        self._q += -base if sell else base
        if abs(self._q) <= EPS:
            self._q = 0.0
        if self._hedger is not None:
            self._hedger.add(-base if sell else base, t)
        # NOTE: this counts fill EVENTS, so a partially filled clip contributes one per
        # partial rather than one per order (maker_inventory aggregates partials into a single
        # FillEvent instead).  Counting partials makes [daily] max_fills bind sooner, which is
        # the safe direction, but it means the budget is consumed faster than "orders filled"
        # would suggest - watch the day_fills margin in the 60 s status line.
        self._state.fills += 1
        self._state.realized_net_usd = self._state_realized_at_start + self._pnl.realized_net()
        delta = self._hedger.delta if self._hedger is not None else 0.0
        self._fills.write([
            _iso(t), "maker", str(self._maker_id.venue), "SELL" if sell else "BUY",
            "close" if closing else "open", f"{price:.10g}", f"{base:.10g}",
            f"{base * price:.6f}", f"{self._q:.10g}", f"{delta:.10g}",
            f"{realized:.8f}", f"{rt_fee:.8f}", self._cfg.mode,
        ])
        self._persist_state()
        self._pump_hedge(t)

    def _on_hedge_fill(
        self, sell: bool, base: float, price: float, t: float, *, partial: bool = False,
    ) -> None:
        """One hedge-venue fill: shrink the unhedged delta and book the taker leg."""
        if self._pnl is None:
            return
        realized, rt_fee, _matched = self._pnl.hedge_fill(sell, base, price)
        if self._hedger is not None:
            # Selling ``base`` on the hedge venue removes ``base`` of long maker exposure.
            self._hedger.settle(-base if sell else base, t)
        self._pending_hedge_qty = max(0.0, self._pending_hedge_qty - base)
        self._state.realized_net_usd = self._state_realized_at_start + self._pnl.realized_net()
        delta = self._hedger.delta if self._hedger is not None else 0.0
        self._fills.write([
            _iso(t), "hedge", str(self._hedge_id.venue), "SELL" if sell else "BUY",
            "hedge-partial" if partial else "hedge",
            f"{price:.10g}", f"{base:.10g}", f"{base * price:.6f}", f"{self._q:.10g}",
            f"{delta:.10g}", f"{realized:.8f}", f"{rt_fee:.8f}", self._cfg.mode,
        ])
        self._persist_state()

    def _pump_hedge(self, t: float, *, force: bool = False) -> None:
        """Send the hedge the delta currently asks for, if any."""
        if self._hedger is None or (self._frozen and not force):
            return
        if self._pending_hedge_qty > EPS and not force:
            return  # one hedge order in flight at a time
        plan = self._hedger.plan(t, self._book.h_bid, self._book.h_ask, force=force)
        if plan is None:
            return
        if self._paper:
            filled, price = self._paper_hedge_fill(plan)
            if filled > EPS:
                self._on_hedge_fill(
                    plan.sell, filled, price, t, partial=filled < plan.qty - EPS,
                )
            return
        try:
            order = self.order_factory.limit(
                instrument_id=self._hedge_id,
                order_side=OrderSide.SELL if plan.sell else OrderSide.BUY,
                quantity=self._hedge_inst.make_qty(_dec(plan.qty)),
                price=self._hedge_inst.make_price(_dec(plan.price)),
                time_in_force=TimeInForce.IOC,
            )
            self._hedge_orders.append(order.client_order_id)
            self._pending_hedge_qty += plan.qty
            self._orders_sent += 1
            self._log_safe(
                "info",
                f"[maker] hedge {'SELL' if plan.sell else 'BUY'} {plan.qty} @ {plan.price} "
                f"IOC ({plan.reason})",
            )
            self.submit_order(order, client_id=self._hedge_client)
        except Exception as exc:  # noqa: BLE001
            self._note_failure(f"hedge order failed: {exc!r}")

    def _paper_hedge_fill(self, plan: HedgePlan) -> tuple[float, float]:
        """What a real IOC would have got: the touch, up to the size resting on it.

        ``slippage_bps`` is the limit price we send as PROTECTION against the touch moving
        between decision and arrival - it is not a cost the order pays when the touch is
        there.  Filling the paper hedge at that protective limit charged 20 bps on every hedge
        and 40 bps on every hedge round trip, which swamped the spread capture and made both
        the paper P&L and the loss kill switch useless as a go/no-go signal.

        So the fill price is the Aster touch, exactly as ``maker_inventory.settle()`` prices a
        hedge (``h_ask[j]`` for a buy, ``h_bid[j]`` for a sell).  This goes one step further
        than the simulator, which always fills in full and only reports whether the top of
        book covered it: here the fill is capped at the resting size, and an IOC leaves
        nothing behind, so the remainder stays in the unhedged delta and is retried on the
        next decision tick.  A quote with no size reported is treated as covering the order -
        an unknown depth is not evidence of a thin book.
        """
        touch = self._book.h_bid if plan.sell else self._book.h_ask
        depth = self._book.h_bid_size if plan.sell else self._book.h_ask_size
        if touch <= 0.0:
            return 0.0, 0.0
        filled = plan.qty if depth <= 0.0 else min(plan.qty, depth)
        return filled, touch

    # -- order events ----------------------------------------------------------------------

    @_guarded
    def on_order_filled(self, event) -> None:
        """Route a live fill to the maker or the hedge book."""
        t = self.clock.timestamp_ns() / 1e9
        base = float(event.last_qty.as_decimal())
        price = float(event.last_px.as_decimal())
        sell = event.order_side == OrderSide.SELL
        if event.instrument_id == self._maker_id:
            side = self._live_orders.get(event.client_order_id)
            quote = self._engine.orders[side] if (self._engine and side is not None) else None
            closing = quote.closing if quote is not None else None
            if quote is not None and quote.ref == event.client_order_id:
                quote.filled += base
                if quote.filled >= quote.base - EPS:
                    self._engine.drop(side)
            self._on_maker_fill(sell, base, price, t, closing=closing)
        elif event.instrument_id == self._hedge_id:
            self._on_hedge_fill(sell, base, price, t)

    @_guarded
    def on_order_canceled(self, event) -> None:
        self._forget(event.client_order_id)
        self._recheck_cleanup()

    @_guarded
    def on_order_expired(self, event) -> None:
        self._forget(event.client_order_id)
        self._recheck_cleanup()

    @_guarded
    def on_order_rejected(self, event) -> None:
        """A venue rejection; a Lighter 23000 throttles the bucket instead of failing."""
        reason = str(getattr(event, "reason", ""))
        self._forget(event.client_order_id)
        if self._is_rate_limit(reason):
            self._throttle(reason)
            return
        self._note_failure(f"OrderRejected {event.client_order_id} reason={reason}")

    @_guarded
    def on_order_denied(self, event) -> None:
        """A local denial (risk engine, validation).  Never widen anything in response."""
        reason = str(getattr(event, "reason", ""))
        self._forget(event.client_order_id)
        self._note_failure(f"OrderDenied {event.client_order_id} reason={reason}")

    @_guarded
    def on_order_modify_rejected(self, event) -> None:
        reason = str(getattr(event, "reason", ""))
        if self._is_rate_limit(reason):
            self._throttle(reason)
            return
        self._log_safe("warning", f"[maker] modify rejected {event.client_order_id}: {reason}")

    @_guarded
    def on_order_cancel_rejected(self, event) -> None:
        reason = str(getattr(event, "reason", ""))
        if self._is_rate_limit(reason):
            self._throttle(reason)
            return
        self._log_safe("warning", f"[maker] cancel rejected {event.client_order_id}: {reason}")

    @staticmethod
    def _is_rate_limit(reason: str) -> bool:
        low = reason.lower()
        return LIGHTER_RATE_LIMIT_CODE in reason or "too many requests" in low

    def _throttle(self, reason: str) -> None:
        if self._engine is None:
            return
        t = self.clock.timestamp_ns() / 1e9
        self._engine.throttle(t)
        self._log_safe(
            "warning",
            f"[maker] venue rate limit ({reason.strip()[:80]}) -> token bucket halved to "
            f"{self._engine.capacity:g}/min for {THROTTLE_SECS:g}s",
        )

    def _forget(self, client_order_id) -> None:
        side = self._live_orders.pop(client_order_id, None)
        if side is None or self._engine is None:
            return
        quote = self._engine.orders[side]
        if quote is not None and quote.ref == client_order_id:
            self._engine.drop(side)

    # -- status ----------------------------------------------------------------------------

    def _status(self) -> None:
        if self._pnl is None or self._engine is None:
            return
        t = self.clock.timestamp_ns() / 1e9
        mid_m, mid_h = self._marks()
        pnl = self._pnl
        delta = self._hedger.delta if self._hedger is not None else 0.0
        elapsed = max(1e-9, t - self._t_last_status)
        tx_min = (self._engine.tx_sent - self._tx_at_last_status) / elapsed * 60.0
        self._tx_at_last_status = self._engine.tx_sent
        self._t_last_status = t
        ewma = pnl.trip_bps_ewma
        net = pnl.net(mid_m, mid_h)
        margins = "  ".join(c.margin() for c in self._last_checks)
        print(
            f"[maker] {self._plan.symbol} fills A/B {pnl.fills[ASK]}/{pnl.fills[BID]} "
            f"filled {pnl.filled_usd[ASK] + pnl.filled_usd[BID]:.2f} USD  "
            f"q={self._q:.4g} ({abs(self._q) * mid_m:.2f} USD)  "
            f"delta={delta:.4g} ({abs(delta) * mid_h:.2f} USD)  "
            f"trips={pnl.trips} ewma={'-' if ewma is None else f'{ewma:.2f}'} bps  "
            f"net={net:.4f} USD  tx/min={tx_min:.1f} tokens={self._engine.tokens:.1f}  "
            f"day {self._state.fills}f/{self._state.tx}tx  | {margins}",
            flush=True,
        )
        self._pnl_csv.write([
            _iso(t), pnl.fills[ASK], pnl.fills[BID],
            f"{pnl.filled_usd[ASK] + pnl.filled_usd[BID]:.6f}",
            f"{self._q:.10g}", f"{abs(self._q) * mid_m:.6f}",
            f"{delta:.10g}", f"{abs(delta) * mid_h:.6f}",
            pnl.trips, "" if ewma is None else f"{ewma:.6f}",
            f"{pnl.spread_capture:.8f}", f"{pnl.hedge_cost:.8f}", f"{pnl.fees:.8f}",
            f"{pnl.residual_mtm(mid_m, mid_h):.8f}", f"{net:.8f}",
            self._engine.tx_sent, f"{tx_min:.3f}", f"{self._engine.tokens:.3f}",
            self._state.fills, self._state.tx,
        ])

    def summary(self) -> str:
        if self._pnl is None or self._engine is None:
            return "[maker] SUMMARY unavailable: the strategy never started"
        mid_m, mid_h = self._marks()
        pnl = self._pnl
        elapsed = max(1e-9, (self.clock.timestamp_ns() / 1e9) - self._started_t)
        delta = self._hedger.delta if self._hedger is not None else 0.0
        ewma = pnl.trip_bps_ewma
        counters = " ".join(f"{k}={v}" for k, v in sorted(self._engine.counters.items()))
        return (
            f"[maker] SUMMARY mode={self._cfg.mode} env={self._cfg.env} "
            f"symbol={self._plan.symbol} ran={elapsed:.0f}s "
            f"fills_ask={pnl.fills[ASK]} fills_bid={pnl.fills[BID]} "
            f"filled_usd={pnl.filled_usd[ASK] + pnl.filled_usd[BID]:.2f} "
            f"hedge_fills={pnl.hedge_fills} hedge_usd={pnl.hedge_usd:.2f} "
            f"trips={pnl.trips} matched_usd={pnl.matched_usd:.2f} "
            f"trip_bps_ewma={'-' if ewma is None else f'{ewma:.2f}'} "
            f"spread_capture={pnl.spread_capture:.4f} hedge_cost={pnl.hedge_cost:.4f} "
            f"fees={pnl.fees:.4f} resid_mtm={pnl.residual_mtm(mid_m, mid_h):.4f} "
            f"net_usd={pnl.net(mid_m, mid_h):.4f} "
            f"q={self._q:.4g} delta={delta:.4g} "
            f"tx={self._engine.tx_sent} (create={self._engine.tx_create} "
            f"modify={self._engine.tx_modify} cancel={self._engine.tx_cancel}) "
            f"tx_per_min={self._engine.tx_sent / elapsed * 60.0:.1f} "
            f"rounded_up_hedges={self._hedger.rounded_up if self._hedger else 0} "
            f"dust_carry_decisions={self._hedger.carried if self._hedger else 0} "
            f"day_fills={self._state.fills}/{self._limits.daily.max_fills} "
            f"day_tx={self._state.tx}/{self._limits.daily.max_tx} "
            f"[{counters}] kill={self._kill_reason or 'none'} "
            f"failures={len(self._failures)} leftovers={len(self._leftovers)} "
            f"exit_code={self.exit_code}"
        )

    # -- cleanup ---------------------------------------------------------------------------

    def _outstanding_orders(self) -> list[tuple[Any, Any]]:
        """``(client_order_id, status)`` for every order of ours not confirmed closed."""
        out: list[tuple[Any, Any]] = []
        if self._paper:
            return out
        for client_order_id in list(self._live_orders) + list(self._hedge_orders):
            try:
                order = self.cache.order(client_order_id)
            except Exception:
                order = None
            if order is None:
                out.append((client_order_id, "unknown (not in the cache)"))
            elif not order.is_closed:
                out.append((client_order_id, order.status))
        return out

    def _run_cleanup(self, trigger: str) -> None:
        """Cancel our quotes, hedge whatever delta is left, and stop quoting.

        Only orders this strategy created are touched - never an account-wide cancel.
        """
        self._frozen = True
        t = self.clock.timestamp_ns() / 1e9
        if self._engine is not None:
            for action in self._engine.cancel_all():
                self._state.tx += 1
                previous = action.previous
                if self._paper or previous is None or previous.ref is None:
                    continue
                try:
                    self.cancel_order(previous.ref)
                except Exception as exc:  # noqa: BLE001
                    detail = f"{previous.ref}: {type(exc).__name__}: {exc}"
                    self._cancel_errors.append(detail)
                    self._note_failure(f"cleanup ({trigger}) could not cancel {detail}")
        if self._hedger is not None and abs(self._hedger.delta) > EPS:
            self._log_safe(
                "warning",
                f"[maker] cleanup ({trigger}): hedging residual delta "
                f"{self._hedger.delta:.6g} IOC",
            )
            self._pump_hedge(t, force=True)
        if not self._outstanding_orders():
            self.cleanup_done_event.set()

    def _recheck_cleanup(self) -> None:
        if not self._frozen or self.cleanup_done_event.is_set():
            return
        if not self._outstanding_orders():
            self.cleanup_done_event.set()

    def _trigger_kill(self, reason: str) -> None:
        if self._kill_reason is not None:
            return
        self._kill_reason = reason
        message = f"[maker] KILL SWITCH: {reason}"
        print(message, file=sys.stderr, flush=True)
        self._log_safe("error", message)
        self._finish("kill")

    def _persist_state(self) -> None:
        try:
            self._state.save()
        except OSError as exc:
            self._log_safe("warning", f"[maker] could not persist the daily state: {exc}")

    def _finish(self, reason: str) -> None:
        """End the run exactly once, always with a summary and always with the done signal."""
        if self._finished:
            return
        self._finished = True
        self._frozen = True
        try:
            try:
                self._run_cleanup(f"finish:{reason}")
            except Exception:  # noqa: BLE001
                self._note_failure(f"cleanup raised while finishing:\n{traceback.format_exc()}")
            self._status()
            self.summary_line = self.summary()
            self._log_safe("error" if self._failures else "info", self.summary_line)
        finally:
            if not self.summary_line:
                self.summary_line = f"[maker] SUMMARY reason={reason} result=failed"
            self._persist_state()
            self.done_event.set()


def _dec(value: float) -> Decimal:
    """Float to Decimal without dragging binary noise into the venue's rounding."""
    return Decimal(repr(round(value, 12)))


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).isoformat(timespec="milliseconds")


# ------------------------------------------------------------------ node wiring


def _lighter_configs(env: str, *, live: bool, account_id: str):
    """Data (and, when ``live``, execution) client configs for the Lighter leg."""
    from nautilus_trader.adapters.lighter import LighterDataClientConfig
    from nautilus_trader.adapters.lighter import LighterDataClientFactory
    from nautilus_trader.adapters.lighter import LighterEnvironment

    environment = (
        LighterEnvironment.MAINNET if env == "mainnet" else LighterEnvironment.TESTNET
    )
    data = (LighterDataClientFactory(), LighterDataClientConfig(environment=environment))
    if not live:
        return data, None
    from nautilus_trader.adapters.lighter import LighterExecutionClientConfig
    from nautilus_trader.adapters.lighter import LighterExecutionClientFactory
    from nautilus_trader.model import AccountId

    exec_pair = (
        LighterExecutionClientFactory(),
        # Credentials are NOT passed here: the adapter resolves them from the
        # deployment+environment namespace (LIGHTER_* on mainnet, LIGHTER_TESTNET_* on
        # testnet), so no secret ever passes through this process's argv or logs.
        LighterExecutionClientConfig(
            account_id=AccountId.from_str(account_id),
            environment=environment,
            sendtx_quota_per_min=36,  # below the venue's 40/min per L1 address
        ),
    )
    return data, exec_pair


def _aster_configs(env: str, *, live: bool, load_ids: list[str]):
    """Data (and, when ``live``, execution) client configs for the Aster hedge leg."""
    from nautilus_trader.adapters.aster import AsterDataClientConfig
    from nautilus_trader.adapters.aster import AsterDataClientFactory
    from nautilus_trader.adapters.aster import AsterEnvironment
    from nautilus_trader.adapters.binance import BinanceInstrumentProviderConfig

    environment = AsterEnvironment.MAINNET if env == "mainnet" else AsterEnvironment.TESTNET
    # Aster rate-limits exchangeInfo hard: never load_all, only what we trade.
    provider = BinanceInstrumentProviderConfig(load_all=False, load_ids=load_ids)
    data = (
        AsterDataClientFactory(),
        AsterDataClientConfig(environment=environment, instrument_provider=provider),
    )
    if not live:
        return data, None
    from nautilus_trader.adapters.aster import AsterExecutionClientConfig
    from nautilus_trader.adapters.aster import AsterExecutionClientFactory

    prefix = "ASTER_MAINNET_" if env == "mainnet" else "ASTER_"
    exec_pair = (
        AsterExecutionClientFactory(),
        AsterExecutionClientConfig(
            environment=environment,
            user_address=os.environ.get(f"{prefix}USER_ADDRESS"),
            signer_address=os.environ.get(f"{prefix}SIGNER_ADDRESS"),
            signer_private_key=os.environ.get(f"{prefix}SIGNER_PRIVATE_KEY"),
            instrument_provider=provider,
        ),
    )
    return data, exec_pair


def aster_credentials_present(env: str) -> bool:
    prefix = "ASTER_MAINNET_" if env == "mainnet" else "ASTER_"
    return bool(os.environ.get(f"{prefix}SIGNER_PRIVATE_KEY"))


def lighter_credentials_present(env: str) -> bool:
    prefix = "LIGHTER_" if env == "mainnet" else "LIGHTER_TESTNET_"
    return all(
        os.environ.get(prefix + name)
        for name in ("ACCOUNT_INDEX", "API_KEY_INDEX", "API_SECRET")
    )


@dataclass
class RunPlan:
    """Everything ``main`` resolved: what to run, where to write it, under which limits."""

    mode: str  # dry-run | paper | live
    env: str
    plan: SymbolPlan
    limits: Limits
    hedge_enabled: bool
    out_dir: Path
    stamp: str
    deadline: datetime
    log_level: str = "INFO"
    connection_timeout_secs: int = DEFAULT_CONNECT_TIMEOUT_SECS

    @property
    def live(self) -> bool:
        return self.mode == "live"

    @property
    def file_mode(self) -> str:
        """``paper`` or ``live``, and it is part of every artefact name.

        Without it a paper session and a real one share a state file, so simulated fills and
        simulated losses would eat the live daily budget - and a live day could be polluted by
        a paper run.  A dry-run writes nothing, and reports both files.
        """
        return "live" if self.live else "paper"

    def state_path_for(self, mode: str) -> Path:
        return self.out_dir / f"state_{mode}_{self.env}_{self.plan.symbol}.json"

    @property
    def fills_csv(self) -> Path:
        return self.out_dir / (
            f"fills_{self.file_mode}_{self.env}_{self.plan.symbol}_{self.stamp}.csv"
        )

    @property
    def pnl_csv(self) -> Path:
        return self.out_dir / (
            f"pnl_{self.file_mode}_{self.env}_{self.plan.symbol}_{self.stamp}.csv"
        )

    @property
    def state_path(self) -> Path:
        return self.state_path_for(self.file_mode)


def build_node(rp: RunPlan):
    """Build the LiveNode and its single strategy; returns ``(node, strategy)``."""
    from nautilus_trader.common import Environment
    from nautilus_trader.common import LoggerConfig
    from nautilus_trader.common import LogLevel
    from nautilus_trader.config import LiveExecutionEngineConfig
    from nautilus_trader.config import LiveRiskEngineConfig
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import StrategyId
    from nautilus_trader.model import TraderId

    maker_venue = "LIGHTER"
    hedge_venue = "ASTER"
    instrument_ids = [rp.plan.maker_id]
    if rp.hedge_enabled:
        instrument_ids.append(rp.plan.hedge_id)

    # Second line of defence behind the engine's own sizing: the risk engine refuses anything
    # over the HARD cap, not the configured one, and it is never bypassed.
    risk_config = LiveRiskEngineConfig(
        bypass=False,
        max_notional_per_order={i: str(CAP_ORDER_NOTIONAL_USD) for i in instrument_ids},
    )
    builder = (
        LiveNode.builder(
            f"MAKER-LIVE-{rp.plan.symbol}",
            TraderId.from_str("MAKER-001"),
            Environment.LIVE,
        )
        .with_logging(LoggerConfig(stdout_level=LogLevel.from_str(rp.log_level)))
        .with_risk_engine_config(risk_config)
        .with_timeout_connection(rp.connection_timeout_secs)
        .with_delay_post_stop_secs(2)
    )
    if rp.live:
        builder = builder.with_reconciliation(reconciliation=True).with_exec_engine_config(
            LiveExecutionEngineConfig(
                reconciliation_lookback_mins=60,
                reconciliation_instrument_ids=instrument_ids,
            ),
        )

    lighter_data, lighter_exec = _lighter_configs(
        rp.env, live=rp.live, account_id=f"{maker_venue}-001",
    )
    builder = builder.add_data_client(maker_venue, *lighter_data)
    if lighter_exec is not None:
        builder = builder.add_exec_client(maker_venue, *lighter_exec)
    if rp.hedge_enabled:
        aster_data, aster_exec = _aster_configs(
            rp.env, live=rp.live, load_ids=[rp.plan.hedge_id],
        )
        builder = builder.add_data_client(hedge_venue, *aster_data)
        if aster_exec is not None:
            builder = builder.add_exec_client(hedge_venue, *aster_exec)
    node = builder.build()

    config = MakerLiveConfig(
        strategy_id=StrategyId.from_str(f"MAKER-LIVE-{rp.plan.symbol}"),
        plan=rp.plan,
        limits=rp.limits,
        mode="live" if rp.live else "paper",
        env=rp.env,
        maker_client_id=maker_venue,
        hedge_client_id=hedge_venue,
        fills_csv=rp.fills_csv,
        pnl_csv=rp.pnl_csv,
        state_path=rp.state_path,
        hedge_enabled=rp.hedge_enabled,
        deadline_ts=rp.deadline.timestamp(),
    )
    strategy = LighterMaker(config)
    node.add_strategy(strategy)
    return node, strategy


# ------------------------------------------------------------------ cli


def parse_until(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(
            f"[maker] bad --until {value!r}: expected UTC ISO, e.g. 2026-09-08T20:05:00Z",
        ) from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="maker_live",
        description=(
            "Two-sided maker on Lighter hedged taker on Aster, under config/limits.toml and "
            "the hard caps in src/live_limits.py. --dry-run builds and prints; --paper runs "
            "the identical logic on public mainnet feeds with locally simulated fills; --live "
            "trades, and on mainnet also needs --confirm-mainnet AND the user saying "
            "上主网 in the current conversation."
        ),
    )
    parser.add_argument("--env", default="mainnet", choices=sorted(SYMBOLS),
                        help="which venue environment to target (default: mainnet)")
    parser.add_argument("--symbol", default="PONS",
                        help="symbol to quote; mainnet: PONS, testnet: DOGE")
    parser.add_argument("--dry-run", action="store_true",
                        help="build every config, print the resolved limits, exit before running")
    parser.add_argument("--paper", action="store_true",
                        help="public data feeds only; fills and hedges are simulated locally")
    parser.add_argument("--live", action="store_true",
                        help="connect the execution clients and place real orders")
    parser.add_argument("--confirm-mainnet", action="store_true",
                        help="required by --live --env mainnet; the user must also have said "
                             "上主网 in the current conversation (CLAUDE.md)")
    parser.add_argument("--hedge", default="aster", choices=("aster", "none"),
                        help="hedge venue; 'none' quotes maker-only (default: aster)")
    parser.add_argument("--limits", type=Path, default=Path("config/limits.toml"),
                        help="operator limits file (default: config/limits.toml)")
    parser.add_argument("--out", type=Path, default=Path("reports/live"),
                        help="output directory for the fills / pnl CSVs and the state file")
    parser.add_argument("--minutes", type=float, default=30.0, help="run duration in minutes")
    parser.add_argument("--until", default=None,
                        help="stop at this UTC ISO instant instead of --minutes")
    parser.add_argument("--log-level", default="INFO",
                        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
                        help="stdout log level for the node (default: INFO)")
    parser.add_argument("--connection-timeout-secs", type=int,
                        default=DEFAULT_CONNECT_TIMEOUT_SECS,
                        help="abort startup if the clients do not connect in time")
    return parser


def resolve_mode(args, parser) -> str:
    chosen = [name for name, on in
              (("dry-run", args.dry_run), ("paper", args.paper), ("live", args.live)) if on]
    if len(chosen) != 1:
        parser.error("choose exactly one of --dry-run / --paper / --live")
    return chosen[0]


def start_stop_watchdog(
    done_event: threading.Event,
    stop_callable: Callable[[], None],
    timeout_secs: float,
    cleanup_event: threading.Event | None = None,
    cleanup_grace_secs: float = 0.0,
) -> tuple[threading.Thread, dict[str, bool]]:
    """Stop the node when the strategy signals done, or when ``timeout_secs`` elapses.

    ``Strategy.stop()`` only stops the strategy; the node keeps blocking in ``run()``.  Copied
    from ``exec_probe.start_stop_watchdog`` so the process always terminates on its own.
    """
    state = {"fired": False, "timed_out": False, "stopped": False, "cleanup_confirmed": False}

    def _run() -> None:
        fired = done_event.wait(timeout_secs)
        state["fired"] = fired
        state["timed_out"] = not fired
        if fired and cleanup_event is not None:
            state["cleanup_confirmed"] = cleanup_event.wait(cleanup_grace_secs)
        try:
            stop_callable()
        finally:
            state["stopped"] = True

    thread = threading.Thread(target=_run, name="maker-live-watchdog", daemon=True)
    thread.start()
    return thread, state


def main(argv: list[str] | None = None) -> int:
    """Run the maker; returns the process exit code."""
    parser = build_parser()
    args = parser.parse_args(argv)
    mode = resolve_mode(args, parser)
    plan = resolve_plan(args.env, args.symbol)
    hedge_enabled = args.hedge == "aster"

    try:
        limits = load_limits(args.limits)
    except LimitsError as exc:
        print(f"[maker] refusing to start: {exc}", file=sys.stderr)
        return EXIT_REFUSED
    if not limits.hedge.enabled:
        hedge_enabled = False

    started = datetime.now(timezone.utc)
    if args.until:
        deadline = parse_until(args.until)
        if deadline <= started:
            raise SystemExit(f"[maker] --until {args.until} is already in the past")
    else:
        deadline = started + timedelta(minutes=args.minutes)

    rp = RunPlan(
        mode=mode,
        env=args.env,
        plan=plan,
        limits=limits,
        hedge_enabled=hedge_enabled,
        out_dir=args.out,
        stamp=started.strftime("%Y%m%dT%H%M%SZ"),
        deadline=deadline,
        log_level=args.log_level,
        connection_timeout_secs=args.connection_timeout_secs,
    )

    print(f"mode              : {mode}")
    print(f"environment       : {args.env}")
    print(f"maker instrument  : {plan.maker_id}  (LIGHTER, post-only GTC)")
    print(f"hedge instrument  : {plan.hedge_id if hedge_enabled else '(disabled)'}"
          f"{'  (ASTER, LIMIT IOC)' if hedge_enabled else ''}")
    if plan.note:
        print(f"symbol note       : {plan.note}")
    print(f"deadline          : {deadline.isoformat(timespec='seconds')}")
    print(f"outputs           : {rp.fills_csv}")
    print(f"                    {rp.pnl_csv}")
    # Both budgets, so it is obvious that a paper session cannot spend the live one.
    for name in ("paper", "live"):
        path = rp.state_path_for(name)
        loaded = DailyState.load(path)
        active = "  <- this run" if name == rp.file_mode and mode != "dry-run" else ""
        print(f"daily state {name:<5} : {path} "
              f"(day={loaded.day} fills={loaded.fills}/{limits.daily.max_fills} "
              f"tx={loaded.tx}/{limits.daily.max_tx} "
              f"realized={loaded.realized_net_usd:.4f} USD){active}")
    print(f"risk engine       : bypass=False, max_notional_per_order="
          f"{CAP_ORDER_NOTIONAL_USD:g} USD (HARD cap) on "
          f"{[plan.maker_id] + ([plan.hedge_id] if hedge_enabled else [])}")
    print(f"total exposure cap: {CAP_TOTAL_NOTIONAL_USD:g} USD (HARD cap)")
    print(f"lighter creds set : {lighter_credentials_present(args.env)}")
    print(f"aster creds set   : {aster_credentials_present(args.env)}")
    print()
    print(limits.report())
    print()

    if mode == "live":
        if args.env == "mainnet" and not args.confirm_mainnet:
            print(
                "Refusing to start: --live --env mainnet requires --confirm-mainnet, and per "
                "CLAUDE.md the user must have said 上主网 in the current "
                "conversation. Neither this flag nor any config file substitutes for that.",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        if not lighter_credentials_present(args.env):
            prefix = "LIGHTER_" if args.env == "mainnet" else "LIGHTER_TESTNET_"
            print(
                f"Refusing to start: {prefix}ACCOUNT_INDEX / {prefix}API_KEY_INDEX / "
                f"{prefix}API_SECRET must all be set for --live.",
                file=sys.stderr,
            )
            return EXIT_REFUSED
        if hedge_enabled and not aster_credentials_present(args.env):
            prefix = "ASTER_MAINNET_" if args.env == "mainnet" else "ASTER_"
            print(
                f"Refusing to start: {prefix}SIGNER_PRIVATE_KEY must be set to hedge on Aster "
                f"(or pass --hedge none).",
                file=sys.stderr,
            )
            return EXIT_REFUSED

    # Everything above is buildable without a network; --dry-run stops here.
    if mode == "dry-run":
        build_node(rp)
        print("dry run: node and strategy built, exiting without connecting")
        return EXIT_OK

    if mode == "live" and args.env == "mainnet":
        print(
            f"MAINNET LIVE in {MAINNET_COUNTDOWN_SECS:g}s - Ctrl+C to abort",
            flush=True,
        )
        time.sleep(MAINNET_COUNTDOWN_SECS)

    exit_code = EXIT_FAILED
    attempt = 0
    while True:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 5.0:
            print("[maker] deadline reached before the node could run", file=sys.stderr)
            break
        attempt += 1
        node, strategy = build_node(rp)
        handle = node.handle()
        timeout = remaining + 60.0
        watchdog, watchdog_state = start_stop_watchdog(
            strategy.done_event, handle.stop, timeout,
            strategy.cleanup_done_event, CLEANUP_GRACE_SECS,
        )
        deadline_timer = threading.Timer(max(1.0, remaining), handle.stop)
        deadline_timer.daemon = True
        deadline_timer.start()

        print(
            f"starting node ({mode}, {args.env}) attempt {attempt}/{CONNECT_ATTEMPTS} until "
            f"{deadline.isoformat(timespec='seconds')}",
            flush=True,
        )
        run_error: BaseException | None = None
        interrupted = False
        try:
            node.run()
        except KeyboardInterrupt as exc:
            run_error = exc
            interrupted = True
            print("[maker] interrupted, stopping", file=sys.stderr, flush=True)
            handle.stop()
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            run_error = exc
            traceback.print_exc()
        finally:
            deadline_timer.cancel()
            strategy.done_event.set()
            watchdog.join(timeout=5.0)

        # A node that never connected sent nothing and saw nothing, so rebuilding it cannot
        # duplicate an order.  Anything else is reported as it stands.
        connect_failed = (
            run_error is not None
            and not interrupted
            and strategy.orders_sent == 0
            and not strategy.received
        )
        left = (deadline - datetime.now(timezone.utc)).total_seconds()
        if connect_failed and attempt < CONNECT_ATTEMPTS and left > CONNECT_RETRY_SECS + 30.0:
            print(
                f"[maker] attempt {attempt}/{CONNECT_ATTEMPTS} did not connect: {run_error}; "
                f"retrying in {CONNECT_RETRY_SECS}s",
                file=sys.stderr,
                flush=True,
            )
            time.sleep(CONNECT_RETRY_SECS)
            continue

        print(strategy.summary_line or strategy.summary(), flush=True)
        for reason in strategy.failures:
            print(f"[maker] failure detail: {ascii(reason)}", file=sys.stderr, flush=True)

        if strategy.kill_reason:
            exit_code = EXIT_KILLED
        elif run_error is not None and not interrupted:
            exit_code = EXIT_FAILED
        elif watchdog_state["timed_out"]:
            print(f"[maker] watchdog fired after {timeout:.0f}s", file=sys.stderr)
            exit_code = EXIT_TIMEOUT
        else:
            exit_code = strategy.exit_code
        if strategy.cancel_errors:
            print(f"[maker] cleanup could not request {len(strategy.cancel_errors)} cancel(s): "
                  f"{'; '.join(strategy.cancel_errors)}", file=sys.stderr, flush=True)
        if strategy.leftovers:
            print(f"[maker] LEFTOVER unconfirmed order(s): {', '.join(strategy.leftovers)}",
                  file=sys.stderr, flush=True)
            if exit_code == EXIT_OK:
                exit_code = EXIT_FAILED
        break

    print(f"[maker] exit code: {exit_code}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

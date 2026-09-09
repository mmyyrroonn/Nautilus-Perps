#!/usr/bin/env python3
"""Two-sided, inventory-aware market making on one venue, hedged taker on another.

``maker_fill.py`` simulated the ask and the bid as two independent runs and gated
*every* fill as a fresh cross-venue basis entry.  Where a basis persists - and on
Lighter it does - that gate silently switches off the side that would reduce the
position, so the book never closes: its reported pnl is the mark of a one-sided
open position, not a realised round trip.  This module fixes that by carrying an
inventory through the replay:

    q          signed base position on the maker venue M; the hedge venue H is held
               at -q (each fill is hedged individually, hedge failures are not
               modelled - only counted).
    opening    a quote that would grow |q|.  Quoted only when the edge against H
               clears the gate and the position cap has room:

                   (our_px - H_ask) / mid * 1e4 - taker_fee_H - reserve >= min_edge
                   |q| * px + order_usd <= max_inv_usd

    closing    a quote that would shrink |q| (ask while long, bid while short).
               Always quoted, for at most |q|, so a closing order can never flip us
               through zero.  This is the side ``maker_fill`` was throwing away.

Inventory can still change sign, and the ``flips`` counter says how often: quotes
are only re-decided once a second, so an order placed as *opening* while flat can
fill after the other side has already pushed us past zero in the same second.  A
real quoter has the same hole; the average-cost books simply open a fresh position
on the remainder.

Fills use the ``maker_fill`` model unchanged: join the touch (queue = the venue
top-of-book size) or improve it by one tick (queue = 0), trade ticks with the
matching aggressor side at or through our price eat the queue and then fill us,
partials accumulate, a completed order is replaced at the next 1 s sample.  Both
sides rest at the same time; a tick only touches the side its aggressor matches.

Accounting is average-cost matched per venue, so the total decomposes exactly:

    total = spread_capture + hedge_cost - fees + residual_mtm

    spread_capture  realised on M: (sell px - avg buy px) * base over matched base
    hedge_cost      realised on H, the mirror trade - normally negative
    fees            taker fee on every hedge plus the maker venue fee (MAKER_FEE_BPS table)
    residual_mtm    the still-open q marked at the M mid and -q at the H mid

A quote is re-priced whenever the maker touch moves, and on Lighter the touch of a
listed perp moves ~50 times a minute, so the unconstrained replay above sends ~60
order transactions a minute.  The venue allows 40 sendTx per minute per L1 address,
shared across creates, modifies and cancels, so three knobs model the real budget:

    --tx-per-min N     token bucket shared by both sides, capacity N, refilled
                       continuously at N per minute.  Placing, re-pricing (one
                       ``L2ModifyOrder``) or cancelling an order costs one token;
                       with the bucket empty the resting order is left exactly as it
                       is - same price, same remaining size, same queue position -
                       and the action is retried at the next sample.  A stale order
                       still fills under the rules above.
    --requote-ticks K  re-price only once the touch is K ticks away from the touch we
                       last quoted against.
    --requote-min-s S  re-price a side at most every S seconds; a held quote that
                       would lock the book is cancelled instead.

Two OPENING gates, off by default and shared with the live maker (``src/quote_gates.py``),
stop the replay taking on inventory in conditions the 2026-09-07 / 09-08 recordings and the
09-09 mainnet session showed do not pay:

    --min-spread-ratio R      the maker touch spread, as a rolling median over
    --min-maker-spread-bps B  ``--spread-window-s``, must be at least R times the hedge
                              round trip (hedge touch spread + both fees) and at least B bps.
    --max-move-bps-per-min M  the maker mid's range over ``--vol-window-s`` must stay under
                              M bps: a fast market picks a resting quote off.

Both gate only the side that would GROW the inventory; the closing side keeps quoting.  With
the defaults (0) they are inert and every earlier result reproduces exactly.

Creates and modifies also spend Lighter's separate volume quota (1000 at account
opening, +1 per 2 USD of filled volume, +1 free per 15 s); the summary reports the
``quota ratio`` of what the quoting would spend against what the window allows.

Read-only, stdlib only, streams every file once; loaders, book merge and table
helpers come from ``maker_fill.py`` / ``opportunities.py``.

    python src/analysis/maker_inventory.py --dir reports/stage1 \
        --stamp 20260907T135945Z [--symbols PONS,ASTER] [--maker LIGHTER] \
        [--hedge ASTER,HL] [--quote improve] [--order-usd 500] \
        [--max-inv-usd 2000] [--min-edge-bps 3] [--reserve-bps 0] \
        [--hedge-delay-s 1] [--no-sensitivity] \
        [--from 2026-09-07T14:00:00Z --to 2026-09-08T00:00:00Z] \
        [--tx-per-min 40] [--requote-ticks 2] [--requote-min-s 3] \
        [--min-spread-ratio 2 --min-maker-spread-bps 12 --spread-window-s 10] \
        [--max-move-bps-per-min 25 --vol-window-s 60] \
        [--trace PONS:LIGHTER:HL --trace-n 8] [--md reports/stage1/inv.md]
"""

from __future__ import annotations

import argparse
import bisect
import statistics
import sys
import time
from collections import deque
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from maker_fill import (  # noqa: E402  (needs sys.path above)
    EPS,
    SAMPLE_S,
    PairBooks,
    VenueTrades,
    at_or_after,
    load_dirs,
    load_trades,
    merge_pair,
)
from quote_gates import GateParams, QuoteGates  # noqa: E402
from opportunities import (  # noqa: E402
    SymbolFiles,
    discover,
    fmt,
    fmt_usd,
    iso,
    parse_iso,
    pct,
    table,
    venue_fees,
)

ASK, BID = 1, 0  # side index: ASK = we sell on M, BID = we buy on M
# Maker fee per maker venue, bps of notional. Lighter charges none (2026-09); Aster
# mainnet commissionRate showed maker 0 / taker 0.9 bps on the symbols probed
# 2026-09-05; HL main dex base tier is 1.5 bps. Override with --maker-fee-bps.
MAKER_FEE_BPS = {"LIGHTER": 0.0, "LIGHTER_RH": 0.0, "ASTER": 0.0, "HL": 1.5}


# ---------------------------------------------------------------- parameters


@dataclass(frozen=True)
class Params:
    """One replay of the knobs.  ``label`` names the row in the sensitivity table."""

    mode: str
    order_usd: float
    max_inv_usd: float
    min_edge_bps: float
    reserve_bps: float
    hedge_delay_s: float
    one_sided: bool = False
    maker_fee_bps: float = 0.0
    label: str = "base"
    tx_per_min: float | None = None  # shared token bucket, None = unlimited
    requote_ticks: int = 1  # re-price only once the touch moved this many ticks
    requote_min_s: float = 0.0  # minimum seconds between two re-prices of one side
    # The two opening gates of ``quote_gates.GateParams``, shared with the live maker.
    # All zero = both off, which is the default: every result produced before they existed
    # reproduces exactly.
    min_spread_ratio: float = 0.0
    min_maker_spread_bps: float = 0.0
    spread_window_s: float = 10.0
    max_move_bps_per_min: float = 0.0
    vol_window_s: float = 60.0

    def gates(self, fee_h: float) -> GateParams:
        """The gate knobs, with the fees the spread gate measures the hedge round trip by."""
        return GateParams(
            min_spread_ratio=self.min_spread_ratio,
            min_maker_spread_bps=self.min_maker_spread_bps,
            spread_window_s=self.spread_window_s,
            max_move_bps_per_min=self.max_move_bps_per_min,
            vol_window_s=self.vol_window_s,
            hedge_fee_bps=fee_h,
            maker_fee_bps=self.maker_fee_bps,
        )

    @property
    def budgeted(self) -> bool:
        """True while any transaction-budget knob is off its default."""
        return (
            self.tx_per_min is not None
            or self.requote_ticks > 1
            or self.requote_min_s > 0.0
        )

    def budget_note(self) -> str:
        cap = "unlimited" if self.tx_per_min is None else f"{self.tx_per_min:g}/min"
        return (
            f"tx budget {cap}, re-price after {self.requote_ticks:g} tick(s) and at "
            f"most every {self.requote_min_s:g} s"
        )

    def variants(self) -> list[Params]:
        """Sensitivity: cap and clip halved / doubled, then the transaction budget."""
        out: list[Params] = []
        for scale in (0.5, 2.0):
            out.append(replace(
                self, max_inv_usd=self.max_inv_usd * scale, label=f"max-inv x{scale:g}",
            ))
        for scale in (0.5, 2.0):
            out.append(replace(
                self, order_usd=self.order_usd * scale, label=f"order x{scale:g}",
            ))
        out += [
            replace(self, tx_per_min=40.0, label="tx-per-min 40"),
            replace(self, tx_per_min=20.0, label="tx-per-min 20"),
            replace(self, tx_per_min=40.0, requote_min_s=3.0,
                    label="requote-min-s 3 + tx 40"),
            replace(self, tx_per_min=40.0, requote_ticks=2,
                    label="requote-ticks 2 + tx 40"),
            replace(self, tx_per_min=40.0, requote_min_s=5.0,
                    label="requote-min-s 5 + tx 40"),
            # The two opening gates at the numbers config/limits.toml ships.
            replace(self, min_spread_ratio=2.0, min_maker_spread_bps=12.0,
                    label="spread gate 2.0/12"),
            replace(self, max_move_bps_per_min=25.0, label="vol gate 25"),
            replace(self, min_spread_ratio=2.0, min_maker_spread_bps=12.0,
                    max_move_bps_per_min=25.0, label="both gates 2.0/12 + 25"),
        ]
        return out


# ---------------------------------------------------------------- fills


@dataclass
class FillEvent:
    """One fill event: a completed order, or the filled part of one we pulled.

    ``q_after`` is the inventory the instant the last trade tick that touched us
    printed, i.e. the position the hedge has to neutralise.
    """

    sell: bool
    closing: bool
    join_t: float
    price: float
    queue_ahead: float
    order_base: float
    t_first: float
    t_last: float
    base: float
    complete: bool
    q_after: float = 0.0
    # settlement, filled in by settle()
    hedge_t: float | None = None
    hedge_px: float | None = None
    covered: bool | None = None
    realized_m: float = 0.0
    realized_h: float = 0.0
    rt_fee: float = 0.0  # fees released by the round trip this event closed
    fee_paid: float = 0.0  # fees this event paid on its own two trades
    matched_base: float = 0.0
    m_cash_after: float = 0.0
    h_cash_after: float = 0.0

    @property
    def usd(self) -> float:
        return self.base * self.price

    @property
    def pnl(self) -> float:
        """Realised pnl attributed to this fill time; the residual is excluded."""
        return self.realized_m + self.realized_h - self.fee_paid


class _Order:
    """The resting order on one side, mutated in place by the trade tape."""

    __slots__ = (
        "oid", "sell", "closing", "price", "queue0", "queue", "base", "filled",
        "join_t", "t_first", "t_last", "q_last",
    )

    def __init__(
        self, oid: int, sell: bool, closing: bool, price: float, queue: float,
        base: float, join_t: float,
    ) -> None:
        self.oid = oid
        self.sell = sell
        self.closing = closing
        self.price = price
        self.queue0 = queue  # queue ahead when we joined, kept for the trace
        self.queue = queue
        self.base = base
        self.filled = 0.0
        self.join_t = join_t
        self.t_first = 0.0
        self.t_last = 0.0
        self.q_last = 0.0


@dataclass
class Run:
    """Everything one replay produced, before it is priced."""

    events: list[FillEvent] = field(default_factory=list)
    by_oid: dict[int, FillEvent] = field(default_factory=dict)
    incs: list[tuple[float, int, float]] = field(default_factory=list)  # ts, oid, base
    quotes: list[int] = field(default_factory=lambda: [0, 0])
    quoting_s: list[float] = field(default_factory=lambda: [0.0, 0.0])
    flow_usd: list[float] = field(default_factory=lambda: [0.0, 0.0])
    flow_all_usd: list[float] = field(default_factory=lambda: [0.0, 0.0])
    gated: list[int] = field(default_factory=lambda: [0, 0])
    locked: list[int] = field(default_factory=lambda: [0, 0])
    capped: list[int] = field(default_factory=lambda: [0, 0])
    max_inv_usd: float = 0.0
    twa_num: float = 0.0
    twa_den: float = 0.0
    hour_inv: dict[int, float] = field(default_factory=dict)  # hour -> last |q| USD
    q_final: float = 0.0
    flips: int = 0  # fills that carried the inventory across zero (see the docstring)
    # transaction budget: every place / re-price / cancel we would send to the venue
    tx_sent: int = 0
    tx_create: int = 0
    tx_modify: int = 0
    tx_cancel: int = 0
    tx_deferred: list[int] = field(default_factory=lambda: [0, 0])
    tx_hour: dict[int, int] = field(default_factory=dict)  # hour -> transactions sent
    stale_s: list[float] = field(default_factory=lambda: [0.0, 0.0])
    stale_any_s: float = 0.0  # time with at least one side off its intended price
    live_s: float = 0.0  # time with at least one side resting: the stale denominator
    # opening gates (quote_gates): samples the opening side was refused, and the
    # time-weighted share of the replay each gate spent closed
    gate_blocked: list[int] = field(default_factory=lambda: [0, 0])
    gate_closed_s: float = 0.0  # either gate closed: what actually stopped opening
    gate_spread_closed_s: float = 0.0
    gate_vol_closed_s: float = 0.0
    gate_hour: dict[int, float] = field(default_factory=dict)  # hour -> seconds closed
    hour_s: dict[int, float] = field(default_factory=dict)  # hour -> seconds sampled

    def fills(self, side: int) -> list[FillEvent]:
        want = side == ASK
        return [e for e in self.events if e.sell == want]


def _close(run: Run, order: _Order, complete: bool) -> None:
    """Turn a finished or pulled order into a fill event, if it filled at all."""
    if order.filled <= EPS:
        return
    event = FillEvent(
        sell=order.sell, closing=order.closing, join_t=order.join_t, price=order.price,
        queue_ahead=order.queue0, order_base=order.base, t_first=order.t_first,
        t_last=order.t_last, base=order.filled, complete=complete, q_after=order.q_last,
    )
    run.events.append(event)
    run.by_oid[order.oid] = event


# ---------------------------------------------------------------- simulation


def simulate(
    books: PairBooks,
    trades: VenueTrades,
    *,
    tick: float,
    decimals: int,
    fee_h: float,
    p: Params,
) -> Run:
    """Replay both sides at once, carrying the inventory across the whole window.

    Sample ``i`` decides both quotes; the trades of ``(t_i, t_i+1]`` then hit them.
    A completed order is replaced only at the following sample, as a real venue
    would need at least one round trip to do.

    Each intended place, re-price or cancel is one venue transaction.  With
    ``p.tx_per_min`` set they are drawn from a token bucket shared by both sides; an
    action that finds it empty is dropped for this sample - the order rests on
    unchanged, keeping its price, its remaining size and its queue position - and is
    reconsidered at the next sample.  ``p.requote_ticks`` and ``p.requote_min_s``
    thin the re-pricing itself, before the bucket is ever consulted.
    """
    run = Run()
    n = len(books)
    if n == 0 or len(trades) == 0:
        return run
    tt, tp, tsz, tbuy = trades.t, trades.price, trades.size, trades.buy
    ntr = len(tt)
    ti = bisect.bisect_right(tt, books.t[0])  # trades before the first sample: ignored
    orders: list[_Order | None] = [None, None]
    live = [False, False]
    stale = [False, False]
    q = 0.0
    oid = 0
    # Token bucket: capacity ``cap``, refilled continuously at ``cap`` per minute.
    cap = float(p.tx_per_min) if p.tx_per_min else 0.0
    refill = cap / 60.0
    tokens = cap
    last_fill_t = books.t[0]
    # Cheaper cadence: the touch we last quoted against, and when we last acted.
    quoted = [0.0, 0.0]
    last_act = [-1e18, -1e18]
    min_move = (p.requote_ticks * tick - tick * 1e-6) if p.requote_ticks > 1 else 0.0
    min_gap = p.requote_min_s
    # The opening gates, the same object the live maker runs (src/quote_gates.py).  With the
    # knobs at their 0 defaults both stay open for every sample and nothing below changes.
    gates = QuoteGates(p.gates(fee_h))

    for i in range(n):
        t = books.t[i]
        t_next = books.t[i + 1] if i + 1 < n else t + SAMPLE_S
        mid = books.mid[i]
        dur = t_next - t
        if cap > 0.0 and t > last_fill_t:
            tokens += (t - last_fill_t) * refill
            if tokens > cap:
                tokens = cap
            last_fill_t = t
        hour = int(t // 3600.0)
        stale[ASK] = stale[BID] = False

        gates.update(
            t,
            m_bid=books.m_bid[i], m_ask=books.m_ask[i],
            h_bid=books.h_bid[i], h_ask=books.h_ask[i],
            mid=mid, dur=dur,
        )
        gate_open = gates.open
        run.hour_s[hour] = run.hour_s.get(hour, 0.0) + dur
        if not gate_open:
            run.gate_closed_s += dur
            run.gate_hour[hour] = run.gate_hour.get(hour, 0.0) + dur
        if not gates.spread_open:
            run.gate_spread_closed_s += dur
        if not gates.vol_open:
            run.gate_vol_closed_s += dur

        for side in (ASK, BID):
            sell = side == ASK
            if sell:
                touch, opposite = books.m_ask[i], books.m_bid[i]
                hedge_px, tob = books.h_ask[i], books.m_ask_size[i]
            else:
                touch, opposite = books.m_bid[i], books.m_ask[i]
                hedge_px, tob = books.h_bid[i], books.m_bid_size[i]

            ok = touch > 0.0 and opposite > 0.0 and hedge_px > 0.0 and mid > 0.0
            if p.one_sided and not sell:
                ok = False
            price = 0.0
            closing = False
            size = 0.0
            if ok:
                if p.mode == "improve":
                    price = round(touch - tick if sell else touch + tick, decimals)
                    if (price <= opposite) if sell else (price >= opposite):
                        run.locked[side] += 1  # one tick inside would lock the book
                        ok = False
                else:
                    price = round(touch, decimals)
            if ok:
                closing = (q > EPS) if sell else (q < -EPS)
                if closing:
                    # Never flip through zero: the clip is capped by what we hold.
                    size = min(p.order_usd / price, abs(q))
                    ok = size > EPS
                else:
                    raw = (price - hedge_px) if sell else (hedge_px - price)
                    if raw / mid * 1e4 - fee_h - p.maker_fee_bps - p.reserve_bps < p.min_edge_bps:
                        run.gated[side] += 1
                        ok = False
                    elif abs(q) * price + p.order_usd > p.max_inv_usd + EPS:
                        run.capped[side] += 1
                        ok = False
                    elif not gate_open:
                        # The maker spread no longer pays for the hedge round trip, or the
                        # maker mid is running.  Same rule in maker_live.QuoteEngine.step().
                        run.gate_blocked[side] += 1
                        ok = False
                    else:
                        size = p.order_usd / price

            order = orders[side]
            want = 0  # 0 nothing, 1 place, 2 re-price (modify), 3 cancel
            if not ok:
                if order is not None:
                    want = 3
            elif order is None:
                want = 1
            elif (
                order.closing != closing
                or (closing and order.base - order.filled > abs(q) + EPS)
            ):
                want = 2  # the side flipped, or the closing clip no longer fits |q|
            elif order.price != price:
                moved = min_move <= 0.0 or abs(touch - quoted[side]) >= min_move
                ready = min_gap <= 0.0 or t - last_act[side] >= min_gap - EPS
                if moved and ready:
                    want = 2
                elif (order.price <= opposite) if sell else (order.price >= opposite):
                    want = 3  # the cadence would hold a quote that now locks the book
            if want and cap > 0.0:
                if tokens < 1.0:
                    run.tx_deferred[side] += 1  # retried at the next sample
                    want = 0
                else:
                    tokens -= 1.0
            if want:
                run.tx_sent += 1
                run.tx_hour[hour] = run.tx_hour.get(hour, 0) + 1
                if want == 3:
                    run.tx_cancel += 1
                    _close(run, order, False)
                    orders[side] = None
                else:
                    if want == 1:
                        run.tx_create += 1
                    else:
                        run.tx_modify += 1
                        _close(run, order, False)
                    oid += 1
                    orders[side] = _Order(
                        oid, sell, closing, price,
                        tob if p.mode == "join" else 0.0, size, t,
                    )
                    run.quotes[side] += 1
                    quoted[side] = touch
                    last_act[side] = t
            # The window flow is credited to the quote that was live when it opened:
            # an order completing mid-window is replaced only at the next sample.
            resting = orders[side]
            live[side] = resting is not None
            if live[side]:
                run.quoting_s[side] += dur
                if not ok or resting.price != price:
                    stale[side] = True  # resting away from what we would quote now
                    run.stale_s[side] += dur

        if live[ASK] or live[BID]:
            run.live_s += dur
            if stale[ASK] or stale[BID]:
                run.stale_any_s += dur

        inv_usd = abs(q) * mid
        run.twa_num += inv_usd * (t_next - t)
        run.twa_den += t_next - t
        if inv_usd > run.max_inv_usd:
            run.max_inv_usd = inv_usd
        run.hour_inv[int(t // 3600.0)] = inv_usd

        while ti < ntr and tt[ti] <= t_next:
            side = ASK if tbuy[ti] == 1 else BID  # a BUY aggressor hits our ask
            if live[side]:
                run.flow_usd[side] += tp[ti] * tsz[ti]
            run.flow_all_usd[side] += tp[ti] * tsz[ti]
            order = orders[side]
            if order is not None:
                through = (
                    (tp[ti] >= order.price) if order.sell else (tp[ti] <= order.price)
                )
                if through:
                    rest = tsz[ti]
                    if order.queue > 0.0:  # makers ahead of us take it first
                        eaten = order.queue if order.queue < rest else rest
                        order.queue -= eaten
                        rest -= eaten
                    if rest > 0.0:
                        room = order.base - order.filled
                        got = rest if rest < room else room
                        if got > 0.0:
                            if order.filled <= EPS:
                                order.t_first = tt[ti]
                            order.filled += got
                            order.t_last = tt[ti]
                            was = q
                            q += -got if order.sell else got
                            if was != 0.0 and (was > 0.0) != (q > 0.0) and abs(q) > EPS:
                                run.flips += 1
                            order.q_last = q
                            run.incs.append((tt[ti], order.oid, got))
                            held = abs(q) * tp[ti]
                            if held > run.max_inv_usd:
                                run.max_inv_usd = held
                        if order.filled >= order.base - EPS:
                            # ``live`` stays set: the rest of this second belongs to the
                            # quote that opened it, exactly as maker_fill credits it.
                            _close(run, order, True)
                            orders[side] = None
            ti += 1

    for side in (ASK, BID):
        if orders[side] is not None:
            _close(run, orders[side], False)
    run.q_final = q
    return run


# ---------------------------------------------------------------- accounting


class AvgCostBook:
    """Average-cost position book: realises pnl, and the fees, on the matched base.

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


@dataclass
class Result:
    """One replay, priced: the pnl decomposition plus the risk and flow stats."""

    params: Params
    run: Run
    total: float = 0.0
    spread_capture: float = 0.0
    hedge_cost: float = 0.0
    fees: float = 0.0
    residual_mtm: float = 0.0
    check: float = 0.0  # cash-flow total minus the decomposed total; must be ~0
    m_cash: float = 0.0
    h_cash: float = 0.0
    q_final: float = 0.0
    resid_usd: float = 0.0
    flatten_usd: float = 0.0
    unhedged: int = 0
    trips: int = 0
    matched_usd: float = 0.0
    rt_bps: list[float] = field(default_factory=list)
    rt_bps_net: list[float] = field(default_factory=list)
    hold_s: list[float] = field(default_factory=list)
    hours: dict[int, list[float]] = field(default_factory=dict)

    @property
    def round_trip_net(self) -> float:
        return self.spread_capture + self.hedge_cost

    @property
    def rt_bps_agg(self) -> float | None:
        """Round-trip net over all matched notional: the size-weighted median twin.

        The median counts a 0.1 USD dust round trip and a 500 USD one alike, and
        partial fills make plenty of the former, so this is the number to read when
        the two disagree.
        """
        if self.matched_usd <= 0.0:
            return None
        return self.round_trip_net / self.matched_usd * 1e4


def settle(books: PairBooks, run: Run, *, fee_h: float, p: Params) -> Result:
    """Hedge every fill, match both legs at average cost, mark what is left open."""
    res = Result(params=p, run=run)
    delay = p.hedge_delay_s
    n = len(books)

    for ev in run.events:
        j = at_or_after(books.t, ev.t_last + delay)
        if j is None:
            res.unhedged += 1
            continue
        px = books.h_ask[j] if ev.sell else books.h_bid[j]
        if px <= 0.0:
            res.unhedged += 1
            continue
        ev.hedge_t, ev.hedge_px = books.t[j], px
        size = books.h_ask_size[j] if ev.sell else books.h_bid_size[j]
        ev.covered = size >= ev.base

    # Maker leg, in tape order: this is exactly the sequence the inventory saw.
    m_book = AvgCostBook()
    lots: deque[tuple[float, float]] = deque()  # (ts, signed base) for holding times
    for ts, key, base in run.incs:
        ev = run.by_oid.get(key)
        if ev is None:
            continue
        signed = -base if ev.sell else base
        fee = p.maker_fee_bps / 1e4 * base * ev.price
        realized, rt_fee, matched = m_book.trade(signed, ev.price, fee)
        ev.realized_m += realized
        ev.rt_fee += rt_fee
        ev.fee_paid += fee
        ev.matched_base += matched
        ev.m_cash_after = m_book.cash
        rest = base
        while rest > EPS and lots and (lots[0][1] > 0.0) != (signed > 0.0):
            lot_t, lot_base = lots[0]
            take = min(rest, abs(lot_base))
            res.hold_s.append(ts - lot_t)
            rest -= take
            if abs(lot_base) - take <= EPS:
                lots.popleft()
            else:
                lots[0] = (lot_t, lot_base - take * (1.0 if lot_base > 0.0 else -1.0))
        if rest > EPS:
            lots.append((ts, rest if signed > 0.0 else -rest))

    # Hedge leg, in hedge-time order: H is held at -q, one taker trade per fill.
    h_book = AvgCostBook()
    hedged = [e for e in run.events if e.hedge_t is not None]
    hedged.sort(key=lambda e: e.hedge_t or 0.0)
    for ev in hedged:
        signed = ev.base if ev.sell else -ev.base  # we sold on M, so we buy on H
        px = ev.hedge_px or 0.0
        fee = fee_h / 1e4 * ev.base * px
        realized, rt_fee, _ = h_book.trade(signed, px, fee)
        ev.realized_h += realized
        ev.rt_fee += rt_fee
        ev.fee_paid += fee
        ev.h_cash_after = h_book.cash

    res.spread_capture = sum(e.realized_m for e in run.events)
    res.hedge_cost = sum(e.realized_h for e in run.events)
    res.fees = m_book.fees + h_book.fees
    res.m_cash, res.h_cash = m_book.cash, h_book.cash
    res.q_final = run.q_final

    mid_m = (books.m_bid[n - 1] + books.m_ask[n - 1]) / 2.0
    mid_h = books.h_mid(n - 1)
    res.residual_mtm = (
        m_book.pos * (mid_m - m_book.avg) + h_book.pos * (mid_h - h_book.avg)
    )
    res.total = res.spread_capture + res.hedge_cost - res.fees + res.residual_mtm
    cash_total = (
        m_book.cash + h_book.cash - res.fees + m_book.pos * mid_m + h_book.pos * mid_h
    )
    res.check = cash_total - res.total
    res.resid_usd = abs(m_book.pos) * mid_m
    # Flattening the residual taker on M pays half of M spread at the final touch.
    touch = books.m_bid[n - 1] if m_book.pos > 0.0 else books.m_ask[n - 1]
    res.flatten_usd = abs(m_book.pos) * abs(mid_m - touch)

    for ev in run.events:
        if ev.matched_base > EPS:
            res.trips += 1
            notional = ev.matched_base * ev.price
            res.matched_usd += notional
            net = ev.realized_m + ev.realized_h
            res.rt_bps.append(net / notional * 1e4)
            res.rt_bps_net.append((net - ev.rt_fee) / notional * 1e4)
        bucket = int(ev.t_last // 3600.0)
        row = res.hours.get(bucket)
        if row is None:
            row = res.hours[bucket] = [0.0, 0.0, 0.0, 0.0]  # ask, bid, usd, pnl
        row[0 if ev.sell else 1] += 1.0
        row[2] += ev.usd
        row[3] += ev.pnl
    return res


# ---------------------------------------------------------------- reporting


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _p90(values: list[float]) -> float | None:
    return pct(sorted(values), 0.9) if values else None


def _share(flags: list[bool | None]) -> str:
    known = [f for f in flags if f is not None]
    if not known:
        return "-"
    return f"{100.0 * sum(1 for f in known if f) / len(known):.0f}%"


def _px(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def _rate(usd: float, flow: float) -> str:
    return f"{100.0 * usd / flow:.2f}%" if flow > 0.0 else "-"


def _time_share(closed_s: float, total_s: float) -> str:
    """Time-weighted share of the replay a gate spent closed."""
    return f"{100.0 * closed_s / total_s:.0f}%" if total_s > 0.0 else "-"


SUMMARY_HEADERS = [
    "run", "total USD", "spread cap", "hedge cost", "fees", "resid mtm", "rt net",
    "rt bps", "rt bps-f", "rt bps-w", "trips", "fills A", "fills B", "USD A", "USD B",
    "rate A", "rate B", "max inv", "twa inv", "hold med", "hold p90", "H cov",
    "tx/min", "deferred", "stale %", "quota ratio", "gate sp%", "gate vol%", "gate any%",
]
SUMMARY_ALIGN = "l" + "r" * (len(SUMMARY_HEADERS) - 1)

# Lighter's volume quota: 1000 at account opening, +1 per 2 USD filled, +1 per 15 s.
QUOTA_BASE = 1000.0
QUOTA_PER_USD = 0.5
QUOTA_PER_MIN = 4.0


def quota_ratio(run: Run, filled_usd: float) -> float | None:
    """Creates + modifies over the quota the window itself grants."""
    allowance = (
        QUOTA_BASE + QUOTA_PER_USD * filled_usd + QUOTA_PER_MIN * run.twa_den / 60.0
    )
    if allowance <= 0.0:
        return None
    return (run.tx_create + run.tx_modify) / allowance


def summary_row(res: Result, label: str) -> list[object]:
    """One replay as a table row; the pnl columns sum to ``total USD`` exactly."""
    run = res.run
    ask, bid = run.fills(ASK), run.fills(BID)
    usd_a = sum(e.usd for e in ask)
    usd_b = sum(e.usd for e in bid)
    twa = run.twa_num / run.twa_den if run.twa_den > 0.0 else 0.0
    tx_min = run.tx_sent / (run.twa_den / 60.0) if run.twa_den > 0.0 else None
    stale = 100.0 * run.stale_any_s / run.live_s if run.live_s > 0.0 else None
    return [
        label,
        fmt(res.total, 1), fmt(res.spread_capture, 1), fmt(res.hedge_cost, 1),
        fmt(res.fees, 1), fmt(res.residual_mtm, 1), fmt(res.round_trip_net, 1),
        fmt(_median(res.rt_bps)), fmt(_median(res.rt_bps_net)), fmt(res.rt_bps_agg),
        res.trips,
        len(ask), len(bid), fmt_usd(usd_a), fmt_usd(usd_b),
        _rate(usd_a, run.flow_usd[ASK]), _rate(usd_b, run.flow_usd[BID]),
        fmt_usd(run.max_inv_usd), fmt_usd(twa),
        fmt(_median(res.hold_s), 1), fmt(_p90(res.hold_s), 1),
        _share([e.covered for e in run.events]),
        fmt(tx_min, 1), run.tx_deferred[ASK] + run.tx_deferred[BID],
        f"{stale:.0f}%" if stale is not None else "-",
        fmt(quota_ratio(run, usd_a + usd_b), 2),
        _time_share(run.gate_spread_closed_s, run.twa_den),
        _time_share(run.gate_vol_closed_s, run.twa_den),
        _time_share(run.gate_closed_s, run.twa_den),
    ]


def hourly_rows(res: Result) -> list[list[object]]:
    """Fills, filled notional, realised pnl and end-of-hour inventory, per UTC hour."""
    buckets = sorted(
        set(res.hours) | set(res.run.hour_inv) | set(res.run.tx_hour) | set(res.run.hour_s),
    )
    rows: list[list[object]] = []
    for bucket in buckets:
        ask, bid, usd, pnl = res.hours.get(bucket, [0.0, 0.0, 0.0, 0.0])
        stamp = datetime.fromtimestamp(bucket * 3600, timezone.utc)
        rows.append([
            stamp.strftime("%Y-%m-%d %H:00"), int(ask), int(bid), fmt_usd(usd),
            fmt(pnl, 1), fmt_usd(res.run.hour_inv.get(bucket)),
            res.run.tx_hour.get(bucket, 0),
            _time_share(
                res.run.gate_hour.get(bucket, 0.0), res.run.hour_s.get(bucket, 0.0),
            ),
        ])
    return rows


def trace_block(res: Result, label: str, count: int) -> list[str]:
    """The first fills spelled out, so the arithmetic can be checked against the CSVs."""
    lines = [f"_trace {label}: first {count} fills_", ""]
    rows = [
        [
            "SELL" if e.sell else "BUY", "close" if e.closing else "open",
            iso(e.join_t), _px(e.price), fmt(e.queue_ahead, 2), iso(e.t_last),
            fmt(e.base, 4), fmt(e.usd, 1), "full" if e.complete else "part",
            fmt(e.q_after, 4), iso(e.hedge_t), _px(e.hedge_px),
            fmt(e.m_cash_after, 1), fmt(e.h_cash_after, 1),
        ]
        for e in res.run.events[:count]
    ]
    if not rows:
        lines += ["_no fills_", ""]
        return lines
    lines += table(
        ["side", "kind", "join ts", "our px", "queue", "fill ts", "base", "USD",
         "done", "q after", "hedge ts", "H px", "M cash", "H cash"],
        rows, align="llllrlrrlrlrrr",
    )
    lines.append("")
    return lines


# ---------------------------------------------------------------- per pair


def run_pair(
    books: PairBooks, trades: VenueTrades, *, fee_h: float, p: Params,
) -> Result:
    """Simulate and price one parameter set on one (maker, hedge) pair."""
    run = simulate(
        books, trades, tick=trades.tick, decimals=trades.decimals, fee_h=fee_h, p=p,
    )
    return settle(books, run, fee_h=fee_h, p=p)


def maker_fee(venue: str, override: str | None) -> float:
    """Maker fee in bps for ``venue``: the table above unless --maker-fee-bps names it."""
    fee = MAKER_FEE_BPS.get(venue, 0.0)
    for item in (override or "").split(","):
        if ":" in item:
            name, value = item.split(":", 1)
            if name.strip() == venue:
                fee = float(value)
    return fee


def pair_block(
    symbol: str, books: PairBooks, trades: VenueTrades, fees: dict[str, float], args,
) -> list[str]:
    """One (maker, hedge) pair: every quote mode, with its hourly and sensitivity split."""
    maker, hedge = books.maker, books.hedge
    fee_h = fees.get(hedge, 0.0)
    fee_m = maker_fee(maker, args.maker_fee_bps)
    lines = [
        f"### maker {maker} -> hedge {hedge}  (taker fee {hedge} {fmt(fee_h)} bps, "
        f"maker fee {maker} {fmt(fee_m)} bps, "
        f"{len(books)} mirrored samples"
        + (f", {books.unmatched} unmatched" if books.unmatched else "") + ")",
        "",
    ]
    for mode in args.quote_modes:
        base = Params(
            mode=mode, order_usd=args.order_usd, max_inv_usd=args.max_inv_usd,
            min_edge_bps=args.min_edge_bps, reserve_bps=args.reserve_bps,
            hedge_delay_s=args.hedge_delay_s, one_sided=args.one_sided,
            maker_fee_bps=fee_m, tx_per_min=args.tx_per_min,
            requote_ticks=args.requote_ticks, requote_min_s=args.requote_min_s,
            min_spread_ratio=args.min_spread_ratio,
            min_maker_spread_bps=args.min_maker_spread_bps,
            spread_window_s=args.spread_window_s,
            max_move_bps_per_min=args.max_move_bps_per_min,
            vol_window_s=args.vol_window_s,
        )
        res = run_pair(books, trades, fee_h=fee_h, p=base)
        run = res.run
        lines.append(
            f"**quote = {mode}**  (order {args.order_usd:g} USD, max inventory "
            f"{args.max_inv_usd:g} USD, opening gate >= {args.min_edge_bps:g} bps after "
            f"the {fmt(fee_h)} bps {hedge} taker fee, the {fmt(fee_m)} bps {maker} maker fee "
            f"and a {args.reserve_bps:g} bps "
            f"reserve, hedge delay {args.hedge_delay_s:g} s"
            + (", ONE-SIDED (bid disabled)" if args.one_sided else "")
            + (f", {base.budget_note()}" if base.budgeted else "")
            + (f", {base.gates(fee_h).describe()}"
               if base.gates(fee_h).on else "") + ")",
        )
        lines.append("")
        lines += table(SUMMARY_HEADERS, [summary_row(res, "base")], align=SUMMARY_ALIGN)
        lines.append("")
        lines.append(
            f"_residual: {fmt(res.q_final, 4)} base = {fmt_usd(res.resid_usd)} USD on "
            f"{maker} ({'long' if res.q_final >= 0 else 'short'}), marked "
            f"{fmt(res.residual_mtm, 1)} USD; flattening it taker at the final {maker} "
            f"touch costs {fmt(res.flatten_usd, 1)} USD.  cash-flow cross-check "
            f"{fmt(res.check, 6)} USD"
            + (f"; {res.unhedged} fill(s) left unhedged, filled too late in the window"
               if res.unhedged else "") + "_",
        )
        lines.append("")
        lines.append(
            f"_quoting: ask {run.quoting_s[ASK] / 3600.0:.2f} h ({run.gated[ASK]} gated, "
            f"{run.capped[ASK]} inventory-capped, {run.locked[ASK]} locked), BUY-aggressor "
            f"flow {fmt_usd(run.flow_usd[ASK])} of {fmt_usd(run.flow_all_usd[ASK])} USD  |  "
            f"bid {run.quoting_s[BID] / 3600.0:.2f} h ({run.gated[BID]} gated, "
            f"{run.capped[BID]} capped, {run.locked[BID]} locked), SELL-aggressor flow "
            f"{fmt_usd(run.flow_usd[BID])} of {fmt_usd(run.flow_all_usd[BID])} USD  |  "
            f"{run.flips} fills carried the inventory across zero_",
        )
        lines.append("")
        if mode == args.trace_mode and args.trace_match(symbol, maker, hedge):
            lines += trace_block(
                res, f"{symbol} {maker}>{hedge} quote={mode}", args.trace_n,
            )
        lines.append("_by UTC hour_")
        lines.append("")
        rows = hourly_rows(res)
        if rows:
            lines += table(
                ["hour UTC", "fills A", "fills B", "filled USD", "USD pnl",
                 "end inv USD", "tx", "gate%"],
                rows, align="lrrrrrrr",
            )
        else:
            lines.append("_no samples_")
        lines.append("")
        if not args.no_sensitivity:
            rows = [summary_row(res, "base")]
            for variant in base.variants():
                rows.append(summary_row(
                    run_pair(books, trades, fee_h=fee_h, p=variant), variant.label,
                ))
            lines.append("_sensitivity_")
            lines.append("")
            lines += table(SUMMARY_HEADERS, rows, align=SUMMARY_ALIGN)
            lines.append("")
    return lines


# ---------------------------------------------------------------- per symbol


def analyse(files: SymbolFiles, stamp: str, args) -> list[str]:
    """One symbol: load its book and trade CSVs, then replay every (maker, hedge) pair."""
    fees, fee_note = venue_fees(files.symbol)
    venues = [v for v in files.venues.split("-") if v]
    makers = [v for v in args.makers if v in venues]
    hedges = [v for v in args.hedges if v in venues]

    lines = [f"## {files.symbol}  [{files.venues.replace('-', ', ')}]", ""]
    if not makers or not hedges:
        lines += [f"_neither maker {args.makers} nor hedge {args.hedges} is complete for "
                  f"this symbol venue set_", ""]
        return lines

    wanted: set[tuple[str, str]] = set()
    for maker in makers:
        for hedge in hedges:
            wanted.add((maker, hedge))
            wanted.add((hedge, maker))
    dirs, rows, skipped = load_dirs(files.all, wanted, args.t_from, args.t_to)
    trades, tr_skipped = load_trades(
        files.all.parent / f"trades_{files.symbol}_{files.venues}_{stamp}.csv",
        set(makers), args.t_from, args.t_to,
    )
    if not dirs:
        lines += ["_no samples in the window_", ""]
        return lines

    t_min = min(d.t[0] for d in dirs.values() if len(d))
    t_max = max(d.t[-1] for d in dirs.values() if len(d))
    lines.append(
        f"window {iso(t_min)} .. {iso(t_max)}  ({(t_max - t_min) / 3600.0:.2f} h)  |  "
        f"book rows {rows}",
    )
    lines.append("")

    notes: list[str] = [fee_note] if fee_note else []
    if skipped:
        notes.append(f"{skipped} unparsable rows skipped in {files.all.name}")
    if tr_skipped:
        notes.append(f"{tr_skipped} unparsable trade rows skipped")

    for maker in makers:
        tr = trades.get(maker)
        if tr is None or len(tr) == 0:
            notes.append(f"{maker}: no trade ticks recorded, a maker order can never fill")
            continue
        if tr.blank_side:
            notes.append(f"{maker}: {tr.blank_side} trades carry no aggressor side and "
                         f"were counted as SELL")
        for hedge in hedges:
            books = merge_pair(dirs, maker, hedge)
            if books is None or len(books) == 0:
                notes.append(f"{maker}/{hedge}: no mirrored samples, pair skipped")
                continue
            lines += pair_block(files.symbol, books, tr, fees, args)

    for note in notes:
        lines.append(f"> note: {note}")
    if notes:
        lines.append("")
    return lines


# ---------------------------------------------------------------- cli


def _make_trace_match(spec: str | None):
    """--trace SYMBOL:MAKER:HEDGE - both sides are traced, in fill order."""
    if not spec:
        return lambda *_: False
    parts = spec.split(":")
    if len(parts) != 3:
        raise SystemExit("[inv] --trace wants SYMBOL:MAKER:HEDGE")
    symbol, maker, hedge = (p.strip().upper() for p in parts)

    def match(sym: str, m: str, h: str) -> bool:
        return sym == symbol and m == maker and h == hedge

    return match


def _venues(text: str, flag: str) -> list[str]:
    out = [v.strip().upper() for v in text.split(",") if v.strip()]
    if not out:
        raise SystemExit(f"[inv] {flag} is empty")
    return out


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Two-sided inventory-aware maker simulation with a taker hedge",
    )
    parser.add_argument("--dir", type=Path, default=Path("reports/stage1"),
                        help="directory holding the csv files")
    parser.add_argument("--stamp", required=True, help="run stamp, e.g. 20260907T135945Z")
    parser.add_argument("--symbols", default=None,
                        help="comma list to restrict to; default: every symbol of the stamp")
    parser.add_argument("--maker", default="LIGHTER,LIGHTER_RH",
                        help="venues we quote both sides on")
    parser.add_argument("--hedge", default="ASTER,HL",
                        help="venues we hedge every fill on, taker")
    parser.add_argument("--quote", default="improve",
                        help="quote placement: join (at the touch) and/or improve (a tick)")
    parser.add_argument("--order-usd", type=float, default=500.0,
                        help="notional of one resting clip")
    parser.add_argument("--max-inv-usd", type=float, default=2000.0,
                        help="cap on |inventory| + one clip before the opening side stops")
    parser.add_argument("--min-edge-bps", type=float, default=3.0,
                        help="minimum edge after the hedge fee before the opening side quotes")
    parser.add_argument("--reserve-bps", type=float, default=0.0,
                        help="extra bps demanded by the gate (hedge slippage reserve)")
    parser.add_argument("--hedge-delay-s", type=float, default=1.0,
                        help="latency from fill to hedge")
    parser.add_argument("--maker-fee-bps", default=None,
                        help="override maker fees, e.g. HL:1.5,ASTER:0.2 (bps); default table in MAKER_FEE_BPS")
    parser.add_argument("--tx-per-min", type=float, default=None,
                        help="venue transaction budget per minute, shared by both sides "
                             "(token bucket, capacity = the same number); default unlimited")
    parser.add_argument("--requote-ticks", type=int, default=1,
                        help="re-price only once the touch moved this many ticks")
    parser.add_argument("--requote-min-s", type=float, default=0.0,
                        help="minimum seconds between two re-prices of one side")
    parser.add_argument("--min-spread-ratio", type=float, default=0.0,
                        help="opening spread gate: the maker touch spread must be at least "
                             "this multiple of the hedge round trip (hedge spread + both "
                             "fees); 0 = off")
    parser.add_argument("--min-maker-spread-bps", type=float, default=0.0,
                        help="opening spread gate: absolute floor under the maker touch "
                             "spread, in bps; 0 = off")
    parser.add_argument("--spread-window-s", type=float, default=10.0,
                        help="seconds the spread gate takes its rolling median over")
    parser.add_argument("--max-move-bps-per-min", type=float, default=0.0,
                        help="opening volatility gate: stop opening while the maker mid's "
                             "range over --vol-window-s exceeds this many bps; 0 = off")
    parser.add_argument("--vol-window-s", type=float, default=60.0,
                        help="seconds the volatility gate measures the maker mid range over")
    parser.add_argument("--no-sensitivity", action="store_true",
                        help="skip the re-runs at 0.5x / 2x cap and clip and the tx budget")
    parser.add_argument("--one-sided", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--trace", default=None,
                        help="dump the first fills of one pair: SYMBOL:MAKER:HEDGE")
    parser.add_argument("--trace-n", type=int, default=8, help="how many traced fills")
    parser.add_argument("--trace-quote", default=None,
                        help="quote mode the trace applies to (default: the first one)")
    parser.add_argument("--from", dest="ts_from", default=None,
                        help="UTC ISO start of the analysis window")
    parser.add_argument("--to", dest="ts_to", default=None,
                        help="UTC ISO end of the analysis window")
    parser.add_argument("--md", type=Path, default=None, help="also write the report here")
    return parser


def main() -> None:
    args = build_parser().parse_args()

    if not args.dir.is_dir():
        raise SystemExit(f"[inv] not a directory: {args.dir}")
    args.makers = _venues(args.maker, "--maker")
    args.hedges = _venues(args.hedge, "--hedge")
    args.quote_modes = [m.strip().lower() for m in args.quote.split(",") if m.strip()]
    bad = [m for m in args.quote_modes if m not in ("join", "improve")]
    if bad or not args.quote_modes:
        raise SystemExit(f"[inv] --quote takes join and/or improve, got {args.quote!r}")
    if args.order_usd <= 0.0 or args.max_inv_usd <= 0.0:
        raise SystemExit("[inv] --order-usd and --max-inv-usd must be positive")
    if args.tx_per_min is not None and args.tx_per_min < 1.0:
        raise SystemExit("[inv] --tx-per-min must be at least 1 (omit it for unlimited)")
    if args.requote_ticks < 1:
        raise SystemExit("[inv] --requote-ticks must be at least 1")
    if args.requote_min_s < 0.0:
        raise SystemExit("[inv] --requote-min-s cannot be negative")
    if args.min_spread_ratio < 0.0 or args.min_maker_spread_bps < 0.0:
        raise SystemExit("[inv] the spread gate knobs cannot be negative (0 = off)")
    if args.max_move_bps_per_min < 0.0:
        raise SystemExit("[inv] --max-move-bps-per-min cannot be negative (0 = off)")
    if args.spread_window_s <= 0.0 or args.vol_window_s <= 0.0:
        raise SystemExit("[inv] --spread-window-s and --vol-window-s must be positive")
    args.trace_match = _make_trace_match(args.trace)
    args.trace_mode = (args.trace_quote or args.quote_modes[0]).strip().lower()
    try:
        args.t_from = parse_iso(args.ts_from).timestamp() if args.ts_from else None
        args.t_to = parse_iso(args.ts_to).timestamp() if args.ts_to else None
    except ValueError as exc:
        raise SystemExit(f"[inv] bad --from/--to: {exc}") from exc
    if args.t_from is not None and args.t_to is not None and args.t_to <= args.t_from:
        raise SystemExit("[inv] --to must be after --from")

    found = discover(args.dir, args.stamp)
    if not found:
        raise SystemExit(f"[inv] no spread_*_{args.stamp}*.csv under {args.dir}")
    if args.symbols:
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        missing = [s for s in wanted if s not in {f.symbol for f in found}]
        if missing:
            raise SystemExit(
                f"[inv] no files for {missing} at stamp {args.stamp}; "
                f"present: {[f.symbol for f in found]}",
            )
        found = [f for f in found if f.symbol in wanted]

    window = ""
    if args.ts_from or args.ts_to:
        window = f"  |  window filter {args.ts_from or 'start'} .. {args.ts_to or 'end'}"
    started = time.monotonic()
    lines = [
        f"# Two-sided maker with inventory - stamp {args.stamp}",
        "",
        f"source `{args.dir}`  |  symbols {', '.join(f.symbol for f in found)}",
        "",
        f"maker {', '.join(args.makers)}  |  hedge {', '.join(args.hedges)}  |  "
        f"quote {', '.join(args.quote_modes)}  |  order {args.order_usd:g} USD  |  "
        f"max inventory {args.max_inv_usd:g} USD  |  min edge {args.min_edge_bps:g} bps  |  "
        f"reserve {args.reserve_bps:g} bps  |  hedge delay {args.hedge_delay_s:g} s"
        + (f"  |  tx budget {args.tx_per_min:g}/min" if args.tx_per_min else "")
        + (f"  |  requote after {args.requote_ticks:g} tick(s)"
           if args.requote_ticks > 1 else "")
        + (f"  |  requote at most every {args.requote_min_s:g} s"
           if args.requote_min_s > 0.0 else "")
        + (f"  |  spread gate {args.min_spread_ratio:g}x hedge cost and >= "
           f"{args.min_maker_spread_bps:g} bps over {args.spread_window_s:g} s"
           if args.min_spread_ratio > 0.0 or args.min_maker_spread_bps > 0.0 else "")
        + (f"  |  volatility gate <= {args.max_move_bps_per_min:g} bps over "
           f"{args.vol_window_s:g} s"
           if args.max_move_bps_per_min > 0.0 else "")
        + f"{window}",
        "",
        "_Both sides rest at once on the maker venue and every fill is hedged taker on "
        "the hedge venue, so the hedge venue is held at minus the maker inventory. The "
        "side that would shrink the inventory (`closing`) is quoted unconditionally for "
        "at most what we hold; the side that would grow it (`opening`) must clear the "
        "cross-venue edge gate and leave room under `max inventory`. "
        "`total USD` = `spread cap` + `hedge cost` - `fees` + `resid mtm`, all in USD "
        "over the window: `spread cap` is the average-cost realised pnl of the maker "
        "leg, `hedge cost` the same for the hedge leg (negative when the hedge gives "
        "back what the spread earned), `resid mtm` the open pnl of whatever inventory "
        "is left, marked at the maker mid against the hedge mid. `rt net` = `spread "
        "cap` + `hedge cost`; `rt bps` is the median round trip in bps of its matched "
        "notional, `rt bps-f` the same after the two hedge fees, and `rt bps-w` the "
        "notional-weighted version of `rt bps` (`rt net` over all matched notional) - "
        "read that one when it disagrees with the median, because partial fills leave "
        "many sub-dollar round trips that the median counts as full ones. `A` = ask "
        "side (we "
        "sell on the maker venue), `B` = bid side. `rate` = filled USD / same-direction "
        "aggressor USD printed while that side was resting. `max inv` / `twa inv` are "
        "the peak and the time-weighted mean of |inventory| in USD, `hold med/p90` how "
        "long a filled lot waits (FIFO) before an opposite fill offsets it, `H cov` how "
        "often the hedge venue top of book covered the fill. `tx/min` is how many order "
        "transactions - places, re-prices, cancels - the quoting would send to the "
        "maker venue per minute, `deferred` how many times a wanted action found the "
        "`--tx-per-min` bucket empty and was postponed a sample, `stale %` the share "
        "of the time at least one side was resting away from the price we would quote "
        "now, and `quota ratio` the creates plus modifies over Lighter's volume "
        "quota for the window (1000 + filled USD / 2 + 4 per minute); above 1 the "
        "quota runs out. `gate sp%` / `gate vol%` / `gate any%` are the time-weighted "
        "shares of the window each opening gate spent closed and the share either was "
        "(`--min-spread-ratio` / `--min-maker-spread-bps` and `--max-move-bps-per-min`, "
        "all off by default); while a gate is closed only the side that would SHRINK the "
        "inventory is quoted, and the hourly `gate%` is the combined share per hour. "
        "The hourly `USD pnl` "
        "columns sum to `total USD` minus `resid mtm`. Minimum order size on the maker "
        "venue is not in the recordings and is not enforced._",
        "",
    ]
    for files in found:
        mark = time.monotonic()
        lines += analyse(files, args.stamp, args)
        print(f"[inv] {files.symbol} done in {time.monotonic() - mark:.1f} s",
              file=sys.stderr)
    elapsed = time.monotonic() - started
    lines.append(f"_runtime {elapsed:.1f} s_")
    text = "\n".join(lines).rstrip() + "\n"
    print(text, end="")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(text, encoding="utf-8")
        print(f"[inv] markdown written to {args.md}  ({elapsed:.1f} s)")


if __name__ == "__main__":
    main()

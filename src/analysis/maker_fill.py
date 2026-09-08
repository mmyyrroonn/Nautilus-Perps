#!/usr/bin/env python3
"""Paper-fill simulation of a resting maker order, hedged taker on a second venue.

``basis_reversion.py`` showed that the persistent Lighter/Aster basis cannot be
taken with two taker legs: crossing both spreads costs more than the basis is
worth.  The remaining candidate is to *be* the spread on the cheap-fee venue -
rest a limit order on Lighter (0 bps maker fee, no spread crossed) and hedge the
fill with a taker order on Aster or Hyperliquid.  This module replays that idea
against the stage-1 recordings:

    spread_<SYM>_<VENUES>_<stamp>_all.csv    1 s book snapshots per direction
    trades_<SYM>_<VENUES>_<stamp>.csv        every public trade tick, with aggressor
    depth_<SYM>_<VENUES>_<stamp>.csv         per-venue notional inside 2 / 5 / 10 bps

Model, per (maker venue M, hedge venue H) and per side:

    quote      SELL side rests an ask on M at M's best ask (``--quote join``) or one
               tick inside it (``--quote improve``); BUY side mirrors on the bid.
               Only quoted while the edge against H clears the gate

                   (our_price - H_ask) / mid * 1e4 - taker_fee_H - reserve >= min_edge

               When M's touch moves we re-quote and lose our queue position; while it
               does not move we keep it.
    queue      joining a level puts M's top-of-book size (base units) ahead of us;
               improving puts nobody ahead.  Trade ticks on M with the right aggressor
               side and a price at or through ours eat the queue first, then fill us.
    hedge      the first 1 s sample at or after ``fill + --hedge-delay-s`` seconds; we
               pay H's ask (SELL side) or hit H's bid (BUY side) plus H's taker fee.
    mark-out   H's mid 10 s / 60 s after the fill against H's mid at the hedge sample,
               signed so positive = the market moved against us.

Read-only, stdlib only, streams every file once.  ``opportunities.py`` supplies the
csv / table / formatting helpers.

    python src/analysis/maker_fill.py --dir reports/stage1 \
        --stamp 20260907T135945Z [--symbols ASTER,PUMP] [--maker LIGHTER] \
        [--hedge ASTER,HL] [--quote join,improve] [--order-usd 500] \
        [--min-edge-bps 3] [--reserve-bps 0] [--hedge-delay-s 1] \
        [--from 2026-09-07T14:00:00Z --to 2026-09-08T00:00:00Z] \
        [--trace ASTER:LIGHTER:ASTER:sell] [--md reports/stage1/maker.md]
"""

from __future__ import annotations

import argparse
import bisect
import statistics
import sys
import time
from array import array
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_HERE = Path(__file__).resolve().parent
for _p in (str(_HERE), str(_HERE.parent)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from opportunities import (  # noqa: E402  (needs sys.path above)
    CAP_BPS,
    DEPTH_TOL_S,
    DepthSeries,
    _reader,
    discover,
    fmt,
    fmt_usd,
    iso,
    load_depth,
    nearest_index,
    parse_iso,
    parse_ts,
    table,
    venue_fees,
)

DEPTH_COL_2BPS = CAP_BPS.index(2)  # the 2 bps bucket column of depth_*.csv
MARKOUT_S = (10.0, 60.0)
SAMPLE_S = 1.0  # nominal spacing of the _all.csv samples, used for the last window
EPS = 1e-12

BOOK_COLS = [
    "ts_utc", "sell_venue", "buy_venue",
    "sell_bid", "sell_bid_size", "buy_ask", "buy_ask_size", "sell_ask", "buy_bid",
]
TRADE_COLS = ["ts_utc", "venue", "price", "size", "aggressor_side"]


# ---------------------------------------------------------------- book loading


@dataclass
class DirRows:
    """One ordered direction (sell_venue, buy_venue) of ``*_all.csv``."""

    t: array = field(default_factory=lambda: array("d"))
    sell_bid: array = field(default_factory=lambda: array("d"))
    sell_bid_size: array = field(default_factory=lambda: array("d"))
    buy_ask: array = field(default_factory=lambda: array("d"))
    buy_ask_size: array = field(default_factory=lambda: array("d"))
    sell_ask: array = field(default_factory=lambda: array("d"))
    buy_bid: array = field(default_factory=lambda: array("d"))

    def __len__(self) -> int:
        return len(self.t)


def load_dirs(
    path: Path,
    wanted: set[tuple[str, str]],
    t_from: float | None,
    t_to: float | None,
) -> tuple[dict[tuple[str, str], DirRows], int, int]:
    """Stream one ``*_all.csv``, keeping only the ordered directions in ``wanted``."""
    out: dict[tuple[str, str], DirRows] = {}
    rows = skipped = 0
    if not path.exists():
        return out, rows, skipped
    stream = _reader(path, BOOK_COLS)
    idx = next(stream, None)
    if idx is None:
        return out, rows, skipped
    c_ts, c_sell, c_buy = idx["ts_utc"], idx["sell_venue"], idx["buy_venue"]
    c_sb, c_sbs = idx["sell_bid"], idx["sell_bid_size"]
    c_ba, c_bas = idx["buy_ask"], idx["buy_ask_size"]
    c_sa, c_bb = idx["sell_ask"], idx["buy_bid"]
    for row in stream:
        key = (row[c_sell], row[c_buy])
        if key not in wanted:
            continue
        try:
            t = parse_ts(row[c_ts])
            sell_bid, sell_bid_size = float(row[c_sb]), float(row[c_sbs])
            buy_ask, buy_ask_size = float(row[c_ba]), float(row[c_bas])
            sell_ask, buy_bid = float(row[c_sa]), float(row[c_bb])
        except (ValueError, IndexError):
            skipped += 1
            continue
        if (t_from is not None and t < t_from) or (t_to is not None and t > t_to):
            continue
        cur = out.get(key)
        if cur is None:
            cur = out[key] = DirRows()
        cur.t.append(t)
        cur.sell_bid.append(sell_bid)
        cur.sell_bid_size.append(sell_bid_size)
        cur.buy_ask.append(buy_ask)
        cur.buy_ask_size.append(buy_ask_size)
        cur.sell_ask.append(sell_ask)
        cur.buy_bid.append(buy_bid)
        rows += 1
    return out, rows, skipped


@dataclass
class PairBooks:
    """Both venues' full top of book at every shared 1 s timestamp."""

    maker: str
    hedge: str
    t: array = field(default_factory=lambda: array("d"))
    m_bid: array = field(default_factory=lambda: array("d"))
    m_bid_size: array = field(default_factory=lambda: array("d"))
    m_ask: array = field(default_factory=lambda: array("d"))
    m_ask_size: array = field(default_factory=lambda: array("d"))
    h_bid: array = field(default_factory=lambda: array("d"))
    h_bid_size: array = field(default_factory=lambda: array("d"))
    h_ask: array = field(default_factory=lambda: array("d"))
    h_ask_size: array = field(default_factory=lambda: array("d"))
    mid: array = field(default_factory=lambda: array("d"))
    unmatched: int = 0  # rows of either direction with no partner at the same ts

    def __len__(self) -> int:
        return len(self.t)

    def h_mid(self, i: int) -> float:
        return (self.h_bid[i] + self.h_ask[i]) / 2.0


def merge_pair(
    dirs: dict[tuple[str, str], DirRows], maker: str, hedge: str,
) -> PairBooks | None:
    """Join direction (M, H) with its mirror (H, M) to recover all four tob sizes.

    Direction (M, H) carries M's bid + bid size and H's ask + ask size; the mirror
    row, written in the same evaluation with the same timestamp string, carries H's
    bid size and M's ask size.  ``mid`` is the mean of the two venue mids, the same
    mid ``spread_watch`` used to turn a price difference into bps.
    """
    fwd = dirs.get((maker, hedge))
    rev = dirs.get((hedge, maker))
    if fwd is None or rev is None or len(fwd) == 0 or len(rev) == 0:
        return None
    out = PairBooks(maker=maker, hedge=hedge)
    i = j = 0
    n, m = len(fwd), len(rev)
    while i < n and j < m:
        ta, tb = fwd.t[i], rev.t[j]
        if ta < tb - EPS:
            out.unmatched += 1
            i += 1
            continue
        if tb < ta - EPS:
            out.unmatched += 1
            j += 1
            continue
        m_bid, m_ask = fwd.sell_bid[i], fwd.sell_ask[i]
        h_bid, h_ask = fwd.buy_bid[i], fwd.buy_ask[i]
        out.t.append(ta)
        out.m_bid.append(m_bid)
        out.m_bid_size.append(fwd.sell_bid_size[i])
        out.m_ask.append(m_ask)
        out.m_ask_size.append(rev.buy_ask_size[j])
        out.h_bid.append(h_bid)
        out.h_bid_size.append(rev.sell_bid_size[j])
        out.h_ask.append(h_ask)
        out.h_ask_size.append(fwd.buy_ask_size[i])
        out.mid.append(((m_bid + m_ask) / 2.0 + (h_bid + h_ask) / 2.0) / 2.0)
        i += 1
        j += 1
    out.unmatched += (n - i) + (m - j)
    return out


# ---------------------------------------------------------------- trade loading


@dataclass
class VenueTrades:
    """Every public trade tick of one venue, in time order."""

    t: array = field(default_factory=lambda: array("d"))
    price: array = field(default_factory=lambda: array("d"))
    size: array = field(default_factory=lambda: array("d"))
    buy: bytearray = field(default_factory=bytearray)  # 1 = aggressor BUY
    decimals: int = 0  # max decimals seen in the price strings -> tick = 10 ** -d
    blank_side: int = 0

    def __len__(self) -> int:
        return len(self.t)

    @property
    def tick(self) -> float:
        return 10.0 ** -self.decimals


def _decimals(text: str) -> int:
    """Decimals of a price string; '0.80691' -> 5, '12' -> 0."""
    dot = text.find(".")
    if dot < 0:
        return 0
    frac = text[dot + 1:]
    if "e" in frac or "E" in frac:
        return 0
    return len(frac)


def load_trades(
    path: Path, venues: set[str], t_from: float | None, t_to: float | None,
) -> tuple[dict[str, VenueTrades], int]:
    """Stream one ``trades_*.csv``, keeping only the venues we may quote on."""
    out: dict[str, VenueTrades] = {}
    skipped = 0
    if not path.exists():
        return out, skipped
    stream = _reader(path, TRADE_COLS)
    idx = next(stream, None)
    if idx is None:
        return out, skipped
    c_ts, c_venue = idx["ts_utc"], idx["venue"]
    c_price, c_size, c_side = idx["price"], idx["size"], idx["aggressor_side"]
    for row in stream:
        venue = row[c_venue]
        if venue not in venues:
            continue
        try:
            t = parse_ts(row[c_ts])
            price_txt = row[c_price]
            price = float(price_txt)
            size = float(row[c_size])
        except (ValueError, IndexError):
            skipped += 1
            continue
        if (t_from is not None and t < t_from) or (t_to is not None and t > t_to):
            continue
        cur = out.get(venue)
        if cur is None:
            cur = out[venue] = VenueTrades()
        side = row[c_side].strip().upper()
        if side not in ("BUY", "SELL"):
            cur.blank_side += 1
        cur.t.append(t)
        cur.price.append(price)
        cur.size.append(size)
        cur.buy.append(1 if side == "BUY" else 0)
        dec = _decimals(price_txt)
        if dec > cur.decimals:
            cur.decimals = dec
    for cur in out.values():  # append order is time order, but never assume
        if any(cur.t[i] > cur.t[i + 1] for i in range(len(cur.t) - 1)):
            order = sorted(range(len(cur.t)), key=cur.t.__getitem__)
            cur.t = array("d", (cur.t[i] for i in order))
            cur.price = array("d", (cur.price[i] for i in order))
            cur.size = array("d", (cur.size[i] for i in order))
            cur.buy = bytearray(cur.buy[i] for i in order)
    return out, skipped


# ---------------------------------------------------------------- simulation


@dataclass
class Fill:
    """One fill event: a completed order, or the filled part of a cancelled one."""

    join_t: float
    price: float
    queue_ahead: float
    order_base: float
    t_first: float  # first trade tick that touched us
    t_last: float  # last trade tick that touched us: the moment we hold the position
    base: float
    usd: float
    complete: bool

    @property
    def time_to_fill(self) -> float:
        return self.t_first - self.join_t


class _Order:
    """The resting order, mutated in place by the trade tape."""

    __slots__ = (
        "price", "queue0", "queue", "base", "filled", "join_t", "t_first", "t_last",
    )

    def __init__(self, price: float, queue: float, base: float, join_t: float) -> None:
        self.price = price
        self.queue0 = queue  # queue ahead at the moment we joined, kept for the trace
        self.queue = queue
        self.base = base
        self.filled = 0.0
        self.join_t = join_t
        self.t_first = 0.0
        self.t_last = 0.0


@dataclass
class SideRun:
    """Everything the replay of one side produced."""

    fills: list[Fill] = field(default_factory=list)
    quotes: int = 0  # orders placed; every re-quote counts again
    quoting_s: float = 0.0
    flow_usd: float = 0.0  # aggressor USD in our direction inside the quoting windows
    flow_all_usd: float = 0.0  # ... and over the whole run, quoting or not
    gated: int = 0  # samples the edge gate rejected
    locked: int = 0  # samples where improving by a tick would have locked the book


def _close(run: SideRun, order: _Order, complete: bool) -> None:
    """Turn a finished or cancelled order into a fill event, if it filled at all."""
    if order.filled > EPS:
        run.fills.append(Fill(
            join_t=order.join_t, price=order.price, queue_ahead=order.queue0,
            order_base=order.base, t_first=order.t_first, t_last=order.t_last,
            base=order.filled, usd=order.filled * order.price, complete=complete,
        ))


def simulate(
    books: PairBooks,
    trades: VenueTrades,
    *,
    sell_side: bool,
    mode: str,
    tick: float,
    decimals: int,
    order_usd: float,
    fee_h: float,
    min_edge: float,
    reserve: float,
) -> SideRun:
    """Replay one side: maker ask + taker buy on H, or maker bid + taker sell on H.

    Sample ``i`` decides the quote; the trades of ``(t_i, t_i+1]`` then hit it.  A
    completed order is re-quoted at the following sample, as a real venue would need
    at least one round trip to replace it.
    """
    run = SideRun()
    n = len(books)
    if n == 0 or len(trades) == 0:
        return run
    tt, tp, tsz, tbuy = trades.t, trades.price, trades.size, trades.buy
    ntr = len(tt)
    ti = bisect.bisect_right(tt, books.t[0])  # trades before the first sample: ignored
    order: _Order | None = None

    for i in range(n):
        t = books.t[i]
        t_next = books.t[i + 1] if i + 1 < n else t + SAMPLE_S
        mid = books.mid[i]
        if sell_side:
            touch, opposite = books.m_ask[i], books.m_bid[i]
            hedge_px, tob = books.h_ask[i], books.m_ask_size[i]
        else:
            touch, opposite = books.m_bid[i], books.m_ask[i]
            hedge_px, tob = books.h_bid[i], books.m_bid_size[i]

        ok = touch > 0.0 and opposite > 0.0 and hedge_px > 0.0 and mid > 0.0
        price = 0.0
        if ok:
            if mode == "improve":
                price = round(touch - tick if sell_side else touch + tick, decimals)
                if (price <= opposite) if sell_side else (price >= opposite):
                    run.locked += 1  # one tick inside would lock or cross the book
                    ok = False
            else:
                price = round(touch, decimals)
        if ok:
            raw = (price - hedge_px) if sell_side else (hedge_px - price)
            if raw / mid * 1e4 - fee_h - reserve < min_edge:
                run.gated += 1
                ok = False

        if not ok:
            if order is not None:
                _close(run, order, False)
                order = None
        elif order is None or order.price != price:
            if order is not None:
                _close(run, order, False)
            order = _Order(price, tob if mode == "join" else 0.0, order_usd / price, t)
            run.quotes += 1

        # The window's flow is credited to the quote that was live when it opened: an
        # order that completes mid-window is replaced only at the next sample, and
        # dropping the rest of that second would flatter the fill rate badly.
        live = order is not None
        if live:
            run.quoting_s += t_next - t

        while ti < ntr and tt[ti] <= t_next:
            if (tbuy[ti] == 1) == sell_side:
                run.flow_all_usd += tp[ti] * tsz[ti]
                if live:
                    run.flow_usd += tp[ti] * tsz[ti]
            if order is not None and (tbuy[ti] == 1) == sell_side:
                through = (tp[ti] >= order.price) if sell_side else (tp[ti] <= order.price)
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
                        if order.filled >= order.base - EPS:
                            _close(run, order, True)
                            order = None
            ti += 1

    if order is not None:
        _close(run, order, False)
    return run


# ---------------------------------------------------------------- hedge + mark-out


def at_or_after(times: array, t: float) -> int | None:
    """Index of the first sample at or after t, or None past the end of the run."""
    i = bisect.bisect_left(times, t)
    return i if i < len(times) else None


@dataclass
class FillEval:
    """A fill priced against the hedge venue at every hedge delay."""

    fill: Fill
    hedge_i: int | None = None
    hedge_t: float | None = None
    hedge_px: float | None = None
    pnl_bps: dict[float, float] = field(default_factory=dict)
    covered: bool | None = None  # hedge venue top of book covered the filled base
    depth_covered: bool | None = None  # its 2 bps bucket covered the filled notional
    markout: dict[float, float] = field(default_factory=dict)

    def usd_pnl(self, delay: float) -> float | None:
        bps = self.pnl_bps.get(delay)
        return None if bps is None else bps * self.fill.usd / 1e4


def evaluate(
    fills: list[Fill],
    books: PairBooks,
    depth_h: DepthSeries | None,
    *,
    sell_side: bool,
    fee_h: float,
    delays: list[float],
    primary: float,
) -> list[FillEval]:
    """Hedge every fill at each delay, then measure how the hedge venue drifted."""
    out: list[FillEval] = []
    sign = 1.0 if sell_side else -1.0
    for fill in fills:
        ev = FillEval(fill=fill)
        for delay in delays:
            j = at_or_after(books.t, fill.t_last + delay)
            if j is None:
                continue
            px = books.h_ask[j] if sell_side else books.h_bid[j]
            mid = books.mid[j]
            if px <= 0.0 or mid <= 0.0:
                continue
            raw = (fill.price - px) if sell_side else (px - fill.price)
            ev.pnl_bps[delay] = raw / mid * 1e4 - fee_h
            if delay == primary:
                ev.hedge_i, ev.hedge_t, ev.hedge_px = j, books.t[j], px
        j = ev.hedge_i
        if j is not None:
            size = books.h_ask_size[j] if sell_side else books.h_bid_size[j]
            ev.covered = size >= fill.base
            if depth_h is not None:
                k = nearest_index(depth_h.t, books.t[j], DEPTH_TOL_S)
                if k is not None:
                    bucket = (depth_h.ask if sell_side else depth_h.bid)[DEPTH_COL_2BPS]
                    ev.depth_covered = bucket[k] >= fill.usd
            base_mid, ref_mid = books.h_mid(j), books.mid[j]
            for horizon in MARKOUT_S:
                k = at_or_after(books.t, fill.t_last + horizon)
                if k is None or ref_mid <= 0.0:
                    continue
                ev.markout[horizon] = sign * (books.h_mid(k) - base_mid) / ref_mid * 1e4
        out.append(ev)
    return out


# ---------------------------------------------------------------- reporting


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def _share(flags: list[bool | None]) -> str:
    known = [f for f in flags if f is not None]
    if not known:
        return "-"
    return f"{100.0 * sum(1 for f in known if f) / len(known):.0f}%"


def _px(value: float | None) -> str:
    if value is None:
        return "-"
    return f"{value:.8f}".rstrip("0").rstrip(".")


def side_headers(delays: list[float]) -> tuple[list[str], str]:
    headers = ["side", "quotes", "fills", "filled USD", "fill rate", "pnl med", "pnl mean"]
    headers += [f"USD {d:g}s" for d in delays]
    headers += ["hedge cov", "d2bps cov", "mo10 bps", "mo60 bps", "ttf med s"]
    return headers, "l" + "r" * (len(headers) - 1)


def side_row(
    label: str, run: SideRun, evals: list[FillEval], delays: list[float], primary: float,
) -> list[object]:
    if not evals:
        return [label, run.quotes, 0, "-", "-", "-", "-",
                *["-"] * len(delays), "-", "-", "-", "-", "-"]
    usd = sum(e.fill.usd for e in evals)
    pnls = [e.pnl_bps[primary] for e in evals if primary in e.pnl_bps]
    ttf = [e.fill.time_to_fill for e in evals if e.fill.t_first > 0.0]
    row: list[object] = [
        label, run.quotes, len(evals), fmt_usd(usd),
        f"{100.0 * usd / run.flow_usd:.2f}%" if run.flow_usd > 0.0 else "-",
        fmt(_median(pnls)), fmt(statistics.fmean(pnls)) if pnls else "-",
    ]
    for delay in delays:
        values = [v for v in (e.usd_pnl(delay) for e in evals) if v is not None]
        row.append(fmt(sum(values), 1) if values else "-")
    row += [
        _share([e.covered for e in evals]),
        _share([e.depth_covered for e in evals]),
        fmt(_median([e.markout[MARKOUT_S[0]] for e in evals if MARKOUT_S[0] in e.markout])),
        fmt(_median([e.markout[MARKOUT_S[1]] for e in evals if MARKOUT_S[1] in e.markout])),
        fmt(_median(ttf), 1),
    ]
    return row


def hourly_rows(evals: list[FillEval], primary: float) -> list[list[object]]:
    buckets: dict[str, list[FillEval]] = {}
    for ev in evals:
        hour = datetime.fromtimestamp(ev.fill.t_last, timezone.utc).strftime("%Y-%m-%d %H")
        buckets.setdefault(hour, []).append(ev)
    rows: list[list[object]] = []
    for hour in sorted(buckets):
        group = buckets[hour]
        usd = [v for v in (e.usd_pnl(primary) for e in group) if v is not None]
        rows.append([
            hour + ":00", len(group), fmt_usd(sum(e.fill.usd for e in group)),
            fmt(sum(usd), 1) if usd else "-",
        ])
    return rows


def trace_block(
    evals: list[FillEval], label: str, count: int, primary: float,
) -> list[str]:
    """The first fills spelled out, so the arithmetic can be checked against the CSVs."""
    lines = [f"_trace {label}: first {count} fills_", ""]
    rows = [
        [iso(e.fill.join_t), _px(e.fill.price), fmt(e.fill.queue_ahead, 2),
         iso(e.fill.t_last), _px(e.fill.price), fmt(e.fill.base, 4), fmt(e.fill.usd, 1),
         "full" if e.fill.complete else "part", iso(e.hedge_t), _px(e.hedge_px),
         fmt(e.pnl_bps.get(primary))]
        for e in evals[:count]
    ]
    if not rows:
        lines.append("_no fills_")
        lines.append("")
        return lines
    lines += table(
        ["join ts", "our price", "queue ahead", "fill ts", "fill price", "filled base",
         "filled USD", "done", "hedge ts", "H px", f"pnl bps @{primary:g}s"],
        rows, align="lrrlrrrlrrr",
    )
    lines.append("")
    return lines


# ---------------------------------------------------------------- per symbol


def analyse(files, stamp: str, args) -> list[str]:
    """One symbol: load its three CSVs, then replay every (maker, hedge) pair."""
    fees, fee_note = venue_fees(files.symbol)
    venues = [v for v in files.venues.split("-") if v]
    makers = [v for v in args.makers if v in venues]
    hedges = [v for v in args.hedges if v in venues]

    lines = [f"## {files.symbol}  [{files.venues.replace('-', ', ')}]", ""]
    if not makers or not hedges:
        lines.append(f"_neither maker {args.makers} nor hedge {args.hedges} is complete "
                     f"for this symbol's venue set_")
        lines.append("")
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
    depth: dict[str, DepthSeries] = {}
    depth_note = "skipped"
    if not args.skip_depth:
        depth, depth_rows, depth_empty = load_depth(files.depth, args.t_from, args.t_to)
        depth_note = f"{depth_rows} rows ({depth_empty} empty)"

    if not dirs:
        lines.append("_no samples in the window_")
        lines.append("")
        return lines

    t_min = min(d.t[0] for d in dirs.values() if len(d))
    t_max = max(d.t[-1] for d in dirs.values() if len(d))
    lines.append(
        f"window {iso(t_min)} .. {iso(t_max)}  ({(t_max - t_min) / 3600.0:.2f} h)  |  "
        f"book rows {rows}  |  depth {depth_note}",
    )
    lines.append("")
    lines.append("### Maker venue tick size (max decimals of the trade price strings)")
    lines.append("")
    tick_rows = [
        [venue, tr.decimals, f"{tr.tick:g}", len(tr),
         sum(tr.buy), len(tr) - sum(tr.buy), tr.blank_side]
        for venue, tr in sorted(trades.items())
    ]
    if tick_rows:
        lines += table(
            ["maker", "decimals", "tick", "trades", "aggr BUY", "aggr SELL", "blank side"],
            tick_rows, align="lrrrrrr",
        )
    else:
        lines.append("_no trades recorded on any maker venue_")
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
            notes.append(f"{maker}: {tr.blank_side} trades carry no aggressor side "
                         f"and were counted as SELL")
        for hedge in hedges:
            books = merge_pair(dirs, maker, hedge)
            if books is None or len(books) == 0:
                notes.append(f"{maker}/{hedge}: no mirrored samples, pair skipped")
                continue
            lines += pair_block(files.symbol, books, tr, depth.get(hedge), fees, args)

    for note in notes:
        lines.append(f"> note: {note}")
    if notes:
        lines.append("")
    return lines


def pair_block(
    symbol: str,
    books: PairBooks,
    tr: VenueTrades,
    depth_h: DepthSeries | None,
    fees: dict[str, float],
    args,
) -> list[str]:
    """One (maker, hedge) pair: both sides, every quote mode, hourly split, inventory."""
    maker, hedge = books.maker, books.hedge
    fee_h = fees.get(hedge, 0.0)
    spread = sorted(
        (books.m_ask[i] - books.m_bid[i]) / books.mid[i] * 1e4
        for i in range(len(books))
        if books.m_bid[i] > 0.0 and books.m_ask[i] > 0.0 and books.mid[i] > 0.0
    )
    lines = [
        f"### maker {maker} -> hedge {hedge}  (taker fee {hedge} {fmt(fee_h)} bps, "
        f"{len(books)} mirrored samples"
        + (f", {books.unmatched} unmatched" if books.unmatched else "") + ")",
        "",
    ]
    for mode in args.quote_modes:
        headers, align = side_headers(args.delays)
        rows: list[list[object]] = []
        traced: list[str] = []
        sell_evals: list[FillEval] = []
        runs: dict[bool, SideRun] = {}
        filled_usd: dict[bool, float] = {}
        for sell_side in (True, False):
            run = simulate(
                books, tr, sell_side=sell_side, mode=mode, tick=tr.tick,
                decimals=tr.decimals, order_usd=args.order_usd, fee_h=fee_h,
                min_edge=args.min_edge_bps, reserve=args.reserve_bps,
            )
            evals = evaluate(
                run.fills, books, depth_h, sell_side=sell_side, fee_h=fee_h,
                delays=args.delays, primary=args.hedge_delay_s,
            )
            label = "SELL (ask on M)" if sell_side else "BUY (bid on M)"
            rows.append(side_row(label, run, evals, args.delays, args.hedge_delay_s))
            runs[sell_side] = run
            filled_usd[sell_side] = sum(e.fill.usd for e in evals)
            if sell_side:
                sell_evals = evals
            if mode == args.trace_mode and args.trace_match(
                symbol, maker, hedge, sell_side,
            ):
                traced = trace_block(
                    evals,
                    f"{symbol} {maker}>{hedge} {'SELL' if sell_side else 'BUY'} "
                    f"quote={mode}",
                    args.trace_n, args.hedge_delay_s,
                )
        lines.append(
            f"**quote = {mode}**  (gate: edge >= {args.min_edge_bps:g} bps after the "
            f"{fmt(fee_h)} bps {hedge} taker fee and a {args.reserve_bps:g} bps reserve; "
            f"order {args.order_usd:g} USD)",
        )
        lines.append("")
        lines += table(headers, rows, align=align)
        lines.append("")
        lines.append(
            f"_quoting time: SELL {runs[True].quoting_s / 3600.0:.2f} h "
            f"({runs[True].gated} samples gated, {runs[True].locked} locked), maker-venue "
            f"BUY-aggressor flow {fmt_usd(runs[True].flow_usd)} of "
            f"{fmt_usd(runs[True].flow_all_usd)} USD  |  "
            f"BUY {runs[False].quoting_s / 3600.0:.2f} h "
            f"({runs[False].gated} gated, {runs[False].locked} locked), SELL-aggressor flow "
            f"{fmt_usd(runs[False].flow_usd)} of {fmt_usd(runs[False].flow_all_usd)} USD_",
        )
        lines.append("")
        lines += traced
        lines.append("_SELL side by UTC hour_")
        lines.append("")
        rows_h = hourly_rows(sell_evals, args.hedge_delay_s)
        if rows_h:
            lines += table(
                ["hour UTC", "fills", "filled USD", f"USD pnl @{args.hedge_delay_s:g}s"],
                rows_h, align="lrrr",
            )
        else:
            lines.append("_no SELL-side fills_")
        lines.append("")
        sell_usd, buy_usd = filled_usd[True], filled_usd[False]
        net = buy_usd - sell_usd
        half = (_median(spread) or 0.0) / 2.0
        lines.append(
            f"_inventory: SELL filled {fmt_usd(sell_usd)} USD, BUY filled "
            f"{fmt_usd(buy_usd)} USD, net {'long' if net >= 0 else 'short'} "
            f"{fmt_usd(abs(net))} USD on {maker}; {maker} median spread "
            f"{fmt(_median(spread))} bps, so flattening the net taker on {maker} costs "
            f"~{fmt(half)} bps = {fmt_usd(abs(net) * half / 1e4)} USD (0 maker/taker fee)_",
        )
        lines.append("")
    return lines


# ---------------------------------------------------------------- cli


def _make_trace_match(spec: str | None):
    """--trace SYMBOL:MAKER:HEDGE:SIDE, SIDE in {sell, buy}."""
    if not spec:
        return lambda *_: False
    parts = spec.split(":")
    if len(parts) != 4:
        raise SystemExit("[maker] --trace wants SYMBOL:MAKER:HEDGE:SIDE (side sell|buy)")
    symbol, maker, hedge, side = (p.strip().upper() for p in parts)
    if side not in ("SELL", "BUY"):
        raise SystemExit("[maker] --trace side must be 'sell' or 'buy'")
    want_sell = side == "SELL"

    def match(sym: str, m: str, h: str, sell_side: bool) -> bool:
        return sym == symbol and m == maker and h == hedge and sell_side == want_sell

    return match


def _venues(text: str, flag: str) -> list[str]:
    out = [v.strip().upper() for v in text.split(",") if v.strip()]
    if not out:
        raise SystemExit(f"[maker] {flag} is empty")
    return out


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Maker-order fill simulation with a taker hedge on a second venue",
    )
    parser.add_argument("--dir", type=Path, default=Path("reports/stage1"),
                        help="directory holding the csv files")
    parser.add_argument("--stamp", required=True, help="run stamp, e.g. 20260907T135945Z")
    parser.add_argument("--symbols", default=None,
                        help="comma list to restrict to; default: every symbol of the stamp")
    parser.add_argument("--maker", default="LIGHTER,LIGHTER_RH",
                        help="venues we rest the maker order on")
    parser.add_argument("--hedge", default="ASTER,HL",
                        help="venues we hedge the fill on, taker")
    parser.add_argument("--quote", default="join,improve",
                        help="quote placement: join (at the touch) and/or improve (one tick)")
    parser.add_argument("--order-usd", type=float, default=500.0,
                        help="notional of the resting order")
    parser.add_argument("--min-edge-bps", type=float, default=3.0,
                        help="minimum edge after the hedge fee before we quote at all")
    parser.add_argument("--reserve-bps", type=float, default=0.0,
                        help="extra bps demanded by the gate (hedge slippage reserve)")
    parser.add_argument("--hedge-delay-s", type=float, default=1.0,
                        help="latency from fill to hedge; also the primary pnl column")
    parser.add_argument("--skip-depth", action="store_true",
                        help="do not read depth_*.csv (drops the 2 bps coverage column)")
    parser.add_argument("--trace", default=None,
                        help="dump the first fills of one run: SYMBOL:MAKER:HEDGE:SIDE")
    parser.add_argument("--trace-n", type=int, default=5, help="how many traced fills")
    parser.add_argument("--trace-quote", default=None,
                        help="quote mode the trace applies to (default: the first one)")
    parser.add_argument("--from", dest="ts_from", default=None,
                        help="UTC ISO start of the analysis window")
    parser.add_argument("--to", dest="ts_to", default=None,
                        help="UTC ISO end of the analysis window")
    parser.add_argument("--md", type=Path, default=None, help="also write the report here")
    args = parser.parse_args()

    if not args.dir.is_dir():
        raise SystemExit(f"[maker] not a directory: {args.dir}")
    args.makers = _venues(args.maker, "--maker")
    args.hedges = _venues(args.hedge, "--hedge")
    args.quote_modes = [m.strip().lower() for m in args.quote.split(",") if m.strip()]
    bad = [m for m in args.quote_modes if m not in ("join", "improve")]
    if bad or not args.quote_modes:
        raise SystemExit(f"[maker] --quote takes join and/or improve, got {args.quote!r}")
    args.delays = sorted({0.0, float(args.hedge_delay_s), 3.0})
    args.trace_match = _make_trace_match(args.trace)
    args.trace_mode = (args.trace_quote or args.quote_modes[0]).strip().lower()
    try:
        args.t_from = parse_iso(args.ts_from).timestamp() if args.ts_from else None
        args.t_to = parse_iso(args.ts_to).timestamp() if args.ts_to else None
    except ValueError as exc:
        raise SystemExit(f"[maker] bad --from/--to: {exc}") from exc
    if args.t_from is not None and args.t_to is not None and args.t_to <= args.t_from:
        raise SystemExit("[maker] --to must be after --from")

    found = discover(args.dir, args.stamp)
    if not found:
        raise SystemExit(f"[maker] no spread_*_{args.stamp}*.csv under {args.dir}")
    if args.symbols:
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        missing = [s for s in wanted if s not in {f.symbol for f in found}]
        if missing:
            raise SystemExit(
                f"[maker] no files for {missing} at stamp {args.stamp}; "
                f"present: {[f.symbol for f in found]}",
            )
        found = [f for f in found if f.symbol in wanted]

    window = ""
    if args.ts_from or args.ts_to:
        window = f"  |  window filter {args.ts_from or 'start'} .. {args.ts_to or 'end'}"
    started = time.monotonic()
    lines = [
        f"# Maker fill simulation - stamp {args.stamp}",
        "",
        f"source `{args.dir}`  |  symbols {', '.join(f.symbol for f in found)}",
        "",
        f"maker {', '.join(args.makers)}  |  hedge {', '.join(args.hedges)}  |  "
        f"quote {', '.join(args.quote_modes)}  |  order {args.order_usd:g} USD  |  "
        f"min edge {args.min_edge_bps:g} bps  |  reserve {args.reserve_bps:g} bps  |  "
        f"hedge delay {args.hedge_delay_s:g} s "
        f"(sensitivity {', '.join(f'{d:g}' for d in args.delays)} s){window}",
        "",
        "_The SELL side rests an ask on the maker venue and buys the hedge venue's ask; "
        "the BUY side mirrors it. `quotes` counts orders placed (every re-quote counts "
        "again); `fills` counts fill events - a completed order, or the filled part of "
        "one that was cancelled or re-quoted. `fill rate` = filled USD / same-direction "
        "aggressor USD printed on the maker venue while we were resting. `pnl med/mean`, "
        "the coverage flags, the mark-outs and the hourly table all use the primary hedge "
        "delay; the `USD Ns` columns are the summed USD pnl at each delay. `hedge cov` = "
        "the hedge venue's top of book covered the filled base; `d2bps cov` = its 2 bps "
        "depth bucket covered the filled notional. `mo10 / mo60` = median mark-out: the "
        "hedge venue's mid 10 s / 60 s after the fill against its mid at the hedge "
        "sample, signed so positive means the market moved against us. `ttf` = join to "
        "first fill._",
        "",
    ]
    for files in found:
        mark = time.monotonic()
        lines += analyse(files, args.stamp, args)
        print(f"[maker] {files.symbol} done in {time.monotonic() - mark:.1f} s",
              file=sys.stderr)
    elapsed = time.monotonic() - started
    lines.append(f"_runtime {elapsed:.1f} s_")
    text = "\n".join(lines).rstrip() + "\n"
    print(text, end="")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(text, encoding="utf-8")
        print(f"[maker] markdown written to {args.md}  ({elapsed:.1f} s)")


if __name__ == "__main__":
    main()

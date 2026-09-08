#!/usr/bin/env python3
"""Basis-reversion backtest on the stage-1 multi-venue spread watch CSVs.

The absolute-threshold round trip in ``opportunities.py`` waits for the cross-venue
basis to collapse to zero, so a persistent basis (Lighter's bid parked ~8 bps above
Aster's ask for eleven hours, say) makes every trade lose.  This module trades the
*deviation around a rolling baseline* instead - the stage-1 idea of
``mean_reversion.py``, ported to the multi-venue ``_all.csv`` format:

    baseline(t) = mean of gross_bps over the previous W seconds (current sample
                  excluded, at least --min-window-n samples required)
    dev(t)      = gross_bps(t) - baseline(t)
    entry       = taker both legs when dev > theta and the venue pair is flat
    exit        = first reverse-direction sample within H seconds whose round trip
                  pnl = entry_gross + exit_gross - fee_rt - reserve >= --target-bps,
                  else the last reverse sample inside H (a forced exit)

A venue pair {A, B} holds one position at a time; both ordered directions share that
slot, as in ``mean_reversion.py``.  Samples whose book is older than --max-age-ms on
either leg are dropped before anything else, so a frozen feed cannot manufacture a
deviation.

Read-only, stdlib only, streams every file once.  ``opportunities.py`` supplies the
csv/percentile/table helpers; the sample loader lives here because ``AllData`` keeps
only (t, gross) and this backtest also needs sizes, mids and book ages.

    python src/analysis/basis_reversion.py --dir reports/stage1 \
        --stamp 20260907T135945Z [--symbols ASTER,PUMP] [--window-s 60,300] \
        [--hold-s 30,120] [--thetas 1,2,4] [--reserve-bps 5] [--max-age-ms 5000] \
        [--from 2026-09-07T14:00:00Z --to 2026-09-08T00:00:00Z] \
        [--trace ASTER:LIGHTER-ASTER:60:30:2] [--md reports/stage1/basis.md]
"""

from __future__ import annotations

import argparse
import bisect
import math
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

SAMPLE_COLS = [
    "ts_utc", "sell_venue", "buy_venue", "gross_bps", "net_bps",
    "sell_bid", "sell_bid_size", "buy_ask", "buy_ask_size",
    "sell_ask", "buy_bid", "age_sell_ms", "age_buy_ms",
]
DEPTH_COL_2BPS = CAP_BPS.index(2)  # the 2 bps bucket of depth_*.csv


# ---------------------------------------------------------------- sample loading


@dataclass
class DirSamples:
    """One ordered direction's 1 s samples, stale rows already removed."""

    t: array = field(default_factory=lambda: array("d"))
    gross: array = field(default_factory=lambda: array("d"))
    cap: array = field(default_factory=lambda: array("d"))  # top-of-book USD
    stale: int = 0  # rows dropped because a leg's book was older than --max-age-ms
    raw: int = 0  # rows seen for this direction inside the window
    fee_seen: float | None = None  # gross - net of the first row = legs + RESERVE_BPS

    def __len__(self) -> int:
        return len(self.t)


@dataclass
class SampleData:
    dirs: dict[tuple[str, str], DirSamples] = field(default_factory=dict)
    rows: int = 0
    skipped: int = 0


def load_samples(
    path: Path, t_from: float | None, t_to: float | None, max_age_ms: float,
) -> SampleData:
    """Stream one ``*_all.csv``: per-direction time / gross / top-of-book capacity.

    ``mid`` is the mean of the two venue mids - ``((sell_bid + sell_ask) / 2 +
    (buy_bid + buy_ask) / 2) / 2`` - the same mid ``spread_watch`` used to turn the
    price difference into bps, so bps and USD stay on one scale.
    """
    data = SampleData()
    if not path.exists():
        return data
    stream = _reader(path, SAMPLE_COLS)
    idx = next(stream, None)
    if idx is None:
        return data
    c_ts, c_sell, c_buy = idx["ts_utc"], idx["sell_venue"], idx["buy_venue"]
    c_gross, c_net = idx["gross_bps"], idx["net_bps"]
    c_sb, c_sbs = idx["sell_bid"], idx["sell_bid_size"]
    c_ba, c_bas = idx["buy_ask"], idx["buy_ask_size"]
    c_sa, c_bb = idx["sell_ask"], idx["buy_bid"]
    c_as, c_ab = idx["age_sell_ms"], idx["age_buy_ms"]
    for row in stream:
        try:
            t = parse_ts(row[c_ts])
            gross = float(row[c_gross])
        except (ValueError, IndexError):
            data.skipped += 1
            continue
        if (t_from is not None and t < t_from) or (t_to is not None and t > t_to):
            continue
        key = (row[c_sell], row[c_buy])
        cur = data.dirs.get(key)
        if cur is None:
            cur = data.dirs[key] = DirSamples()
            try:
                cur.fee_seen = gross - float(row[c_net])
            except ValueError:
                pass
        cur.raw += 1
        data.rows += 1
        age_sell = _age(row[c_as])
        age_buy = _age(row[c_ab])
        if age_sell > max_age_ms or age_buy > max_age_ms:
            cur.stale += 1
            continue
        try:
            sell_bid, sell_bid_size = float(row[c_sb]), float(row[c_sbs])
            buy_ask, buy_ask_size = float(row[c_ba]), float(row[c_bas])
            sell_ask, buy_bid = float(row[c_sa]), float(row[c_bb])
        except ValueError:
            data.skipped += 1
            continue
        if sell_ask > 0.0 and buy_bid > 0.0:
            mid = ((sell_bid + sell_ask) / 2.0 + (buy_bid + buy_ask) / 2.0) / 2.0
        else:  # one side's opposite quote is missing: fall back to the traded pair
            mid = (sell_bid + buy_ask) / 2.0
        cur.t.append(t)
        cur.gross.append(gross)
        cur.cap.append(min(sell_bid_size, buy_ask_size) * mid)
    for cur in data.dirs.values():  # append order is time order, but never assume
        if any(cur.t[i] > cur.t[i + 1] for i in range(len(cur.t) - 1)):
            order = sorted(range(len(cur.t)), key=cur.t.__getitem__)
            cur.t = array("d", (cur.t[i] for i in order))
            cur.gross = array("d", (cur.gross[i] for i in order))
            cur.cap = array("d", (cur.cap[i] for i in order))
    return data


def _age(text: str) -> float:
    """Book age in ms; a blank cell counts as fresh (the watcher never wrote one)."""
    try:
        return float(text)
    except ValueError:
        return 0.0


# ---------------------------------------------------------------- signal


def deviations(samples: DirSamples, window_s: float, min_n: int) -> array:
    """dev(t) = gross(t) - mean(gross over [t - W, t)), NaN where the window is thin."""
    n = len(samples)
    out = array("d", [math.nan]) * n if n else array("d")
    if n == 0:
        return out
    prefix = [0.0] * (n + 1)
    gross = samples.gross
    for i in range(n):
        prefix[i + 1] = prefix[i] + gross[i]
    times = samples.t
    for i in range(n):
        k = bisect.bisect_left(times, times[i] - window_s)
        span = i - k
        if span < min_n:
            continue
        out[i] = gross[i] - (prefix[i] - prefix[k]) / span
    return out


# ---------------------------------------------------------------- backtest


@dataclass
class Trade:
    key: tuple[str, str]  # entry direction (sell, buy)
    t_in: float
    t_out: float
    entry_gross: float
    baseline: float
    exit_gross: float
    pnl_bps: float
    cap_tob: float
    cap_depth: float | None
    forced: bool

    @property
    def hold(self) -> float:
        return self.t_out - self.t_in


@dataclass
class RunResult:
    trades: list[Trade] = field(default_factory=list)
    unresolved: int = 0  # signal fired but no reverse sample inside the hold window


def _candidates(
    dirs: dict[tuple[str, str], DirSamples],
    devs: dict[tuple[str, str], array],
    keys: tuple[tuple[str, str], tuple[str, str]],
    min_theta: float,
) -> list[tuple[float, int, int, float]]:
    """Both directions' armed samples (dev > min theta) merged into one time order."""
    out: list[tuple[float, int, int, float]] = []
    for slot, key in enumerate(keys):
        samples, dev = dirs[key], devs[key]
        times = samples.t
        for i in range(len(samples)):
            value = dev[i]
            if value > min_theta:
                out.append((times[i], slot, i, value))
    out.sort(key=lambda c: c[0])
    return out


def backtest(
    dirs: dict[tuple[str, str], DirSamples],
    keys: tuple[tuple[str, str], tuple[str, str]],
    candidates: list[tuple[float, int, int, float]],
    devs: dict[tuple[str, str], array],
    theta: float,
    hold_s: float,
    fee_rt: dict[tuple[str, str], float],
    reserve: float,
    target: float,
    depth: dict[str, DepthSeries],
) -> RunResult:
    """One position per venue pair; both directions compete for that single slot."""
    result = RunResult()
    busy_until = -math.inf
    for t_in, slot, i, dev in candidates:
        if t_in <= busy_until or dev <= theta:
            continue
        key = keys[slot]
        rev_key = keys[1 - slot]
        entry = dirs[key]
        reverse = dirs[rev_key]
        cost = fee_rt[key] + reserve
        lo = bisect.bisect_right(reverse.t, t_in)
        limit = t_in + hold_s
        entry_gross = entry.gross[i]
        chosen = None
        forced = True
        j = lo
        while j < len(reverse) and reverse.t[j] <= limit:
            pnl = entry_gross + reverse.gross[j] - cost
            if pnl >= target:
                chosen, forced = j, False
                break
            chosen = j
            j += 1
        if chosen is None:
            result.unresolved += 1
            continue
        exit_gross = reverse.gross[chosen]
        result.trades.append(Trade(
            key=key,
            t_in=t_in,
            t_out=reverse.t[chosen],
            entry_gross=entry_gross,
            baseline=entry_gross - dev,
            exit_gross=exit_gross,
            pnl_bps=entry_gross + exit_gross - cost,
            cap_tob=entry.cap[i],
            cap_depth=depth_capacity(depth, key, t_in),
            forced=forced,
        ))
        busy_until = reverse.t[chosen]
    return result


def depth_capacity(
    depth: dict[str, DepthSeries], key: tuple[str, str], t: float,
) -> float | None:
    """min(sell venue bid_usd_2bps, buy venue ask_usd_2bps) within DEPTH_TOL_S of t."""
    sell_series = depth.get(key[0])
    buy_series = depth.get(key[1])
    if sell_series is None or buy_series is None:
        return None
    i = nearest_index(sell_series.t, t, DEPTH_TOL_S)
    j = nearest_index(buy_series.t, t, DEPTH_TOL_S)
    if i is None or j is None:
        return None
    return min(sell_series.bid[DEPTH_COL_2BPS][i], buy_series.ask[DEPTH_COL_2BPS][j])


# ---------------------------------------------------------------- reporting


def _median(values: list[float]) -> float | None:
    return statistics.median(values) if values else None


def theta_row(
    theta: float, result: RunResult, keys: tuple[tuple[str, str], tuple[str, str]],
) -> list[object]:
    trades = result.trades
    if not trades:
        return [fmt(theta, 1), 0, "0/0", "-", "-", "-", "-", "-", 0, "-", "-", "-", "-"]
    pnls = sorted(t.pnl_bps for t in trades)
    holds = [t.hold for t in trades]
    tob = [t.cap_tob for t in trades]
    dep = [t.cap_depth for t in trades if t.cap_depth is not None]
    usd = sum(t.pnl_bps * t.cap_tob / 1e4 for t in trades)
    n_first = sum(1 for t in trades if t.key == keys[0])
    return [
        fmt(theta, 1),
        len(trades),
        f"{n_first}/{len(trades) - n_first}",
        f"{100.0 * sum(1 for p in pnls if p >= 0.0) / len(pnls):.1f}%",
        fmt(statistics.median(pnls)),
        fmt(statistics.fmean(pnls)),
        fmt(sum(pnls), 1),
        fmt(pnls[0]),
        sum(1 for t in trades if t.forced),
        fmt(statistics.median(holds), 1),
        fmt_usd(_median(tob)),
        fmt_usd(_median(dep)),
        fmt_usd(usd),
    ]


THETA_HEADERS = [
    "theta", "trades", "split", "pos%", "pnl med", "pnl mean", "pnl sum", "worst",
    "forced", "hold med s", "tob med USD", "d2bps med USD", "USD pnl",
]
THETA_ALIGN = "rrlrrrrrrrrrr"


def hourly_rows(trades: list[Trade]) -> list[list[object]]:
    per_hour: dict[str, list[float]] = {}
    for trade in trades:
        hour = datetime.fromtimestamp(trade.t_in, timezone.utc).strftime("%Y-%m-%d %H")
        per_hour.setdefault(hour, []).append(trade.pnl_bps)
    rows: list[list[object]] = []
    for hour in sorted(per_hour):
        pnls = per_hour[hour]
        rows.append([
            hour + ":00", len(pnls), fmt(sum(pnls), 1), fmt(statistics.median(pnls)),
        ])
    return rows


# ---------------------------------------------------------------- per symbol


def analyse(files, args) -> list[str]:
    fees, fee_note = venue_fees(files.symbol)
    data = load_samples(files.all, args.t_from, args.t_to, args.max_age_ms)
    depth: dict[str, DepthSeries] = {}
    depth_note = "skipped"
    if not args.skip_depth:
        depth, depth_rows, depth_empty = load_depth(files.depth, args.t_from, args.t_to)
        depth_note = f"{depth_rows} rows ({depth_empty} empty)"

    lines: list[str] = []
    lines.append(f"## {files.symbol}  [{files.venues.replace('-', ', ')}]")
    lines.append("")
    if not data.dirs:
        lines.append("_no samples in the window_")
        lines.append("")
        return lines

    t_min = min(s.t[0] for s in data.dirs.values() if len(s))
    t_max = max(s.t[-1] for s in data.dirs.values() if len(s))
    span_h = (t_max - t_min) / 3600.0
    lines.append(
        f"window {iso(t_min)} .. {iso(t_max)}  ({span_h:.2f} h)  |  "
        f"samples {data.rows}  |  depth {depth_note}",
    )
    lines.append("")

    # ---- stale legs
    stale_rows = [
        [f"{k[0]}>{k[1]}", d.raw, d.stale,
         f"{100.0 * d.stale / d.raw:.2f}%" if d.raw else "-", len(d)]
        for k, d in sorted(data.dirs.items())
    ]
    lines.append(f"### Stale rows dropped (age > {args.max_age_ms:g} ms on either leg)")
    lines.append("")
    lines += table(["direction", "rows", "dropped", "%", "kept"], stale_rows, align="lrrrr")
    lines.append("")

    # fee per direction: INSTRUMENTS first, the csv (gross - net - reserve) as fallback
    fee_rt: dict[tuple[str, str], float] = {}
    notes: list[str] = [fee_note] if fee_note else []
    for key, samples in data.dirs.items():
        if fees:
            legs = fees.get(key[0], 0.0) + fees.get(key[1], 0.0)
        elif samples.fee_seen is not None:
            legs = samples.fee_seen - 5.0  # spread_watch.RESERVE_BPS
        else:
            legs = 0.0
        fee_rt[key] = 2.0 * legs

    pairs: dict[frozenset[str], tuple[tuple[str, str], tuple[str, str]]] = {}
    for key in data.dirs:
        tag = frozenset(key)
        if len(tag) == 2 and (key[1], key[0]) in data.dirs:
            pairs.setdefault(tag, (key, (key[1], key[0])))

    for tag in sorted(pairs, key=lambda s: tuple(sorted(s))):
        keys = pairs[tag]
        a, b = keys[0]
        if len(data.dirs[keys[0]]) == 0 or len(data.dirs[keys[1]]) == 0:
            continue
        lines.append(f"### {a}/{b}  (fee round trip {fmt(fee_rt[keys[0]])} bps"
                     f" + reserve {args.reserve_bps:g})")
        lines.append("")
        best: tuple[float, str, list[Trade]] | None = None
        for window_s in args.windows:
            devs = {k: deviations(data.dirs[k], window_s, args.min_window_n) for k in keys}
            candidates = _candidates(data.dirs, devs, keys, min(args.thetas))
            for hold_s in args.holds:
                rows: list[list[object]] = []
                traced: list[str] = []
                unresolved = 0
                for theta in args.thetas:
                    result = backtest(
                        data.dirs, keys, candidates, devs, theta, hold_s,
                        fee_rt, args.reserve_bps, args.target_bps, depth,
                    )
                    rows.append(theta_row(theta, result, keys))
                    unresolved += result.unresolved
                    total = sum(t.pnl_bps for t in result.trades)
                    label = f"W={window_s:g}s H={hold_s:g}s theta={theta:g}"
                    if result.trades and (best is None or total > best[0]):
                        best = (total, label, result.trades)
                    if args.trace and args.trace_match(
                        files.symbol, a, b, window_s, hold_s, theta,
                    ):
                        traced += trace_block(result.trades, label, args.trace_n)
                lines.append(f"**W={window_s:g}s, H={hold_s:g}s**"
                             f"  (unresolved signals, no reverse sample in H: {unresolved})")
                lines.append("")
                lines += table(THETA_HEADERS, rows, align=THETA_ALIGN)
                lines.append("")
                lines += traced
        if best is not None:
            lines.append(f"_best by pnl sum: {best[1]}  ->  {best[0]:.1f} bps over "
                         f"{len(best[2])} trades; hourly breakdown:_")
            lines.append("")
            lines += table(
                ["hour UTC", "trades", "pnl sum", "pnl med"],
                hourly_rows(best[2]), align="lrrr",
            )
            lines.append("")
        else:
            lines.append("_no theta produced a trade for this pair_")
            lines.append("")

    if data.skipped:
        notes.append(f"{data.skipped} unparsable rows skipped in {files.all.name}")
    for note in notes:
        lines.append(f"> note: {note}")
    if notes:
        lines.append("")
    return lines


def trace_block(trades: list[Trade], label: str, count: int) -> list[str]:
    lines = [f"_trace {label}: first {count} trades_", ""]
    rows = [
        [iso(t.t_in), f"{t.key[0]}>{t.key[1]}", fmt(t.entry_gross), fmt(t.baseline),
         fmt(t.entry_gross - t.baseline), iso(t.t_out), fmt(t.exit_gross),
         fmt(t.pnl_bps), "yes" if t.forced else "no"]
        for t in trades[:count]
    ]
    if not rows:
        lines.append("_no trades_")
        lines.append("")
        return lines
    lines += table(
        ["entry ts", "direction", "entry gross", "baseline", "dev", "exit ts",
         "exit gross", "pnl bps", "forced"],
        rows, align="llrrrlrrl",
    )
    lines.append("")
    return lines


# ---------------------------------------------------------------- cli


def _floats(text: str, flag: str) -> list[float]:
    try:
        values = [float(v) for v in text.split(",") if v.strip()]
    except ValueError as exc:
        raise SystemExit(f"[basis] bad {flag}: {exc}") from exc
    if not values:
        raise SystemExit(f"[basis] {flag} is empty")
    return values


def _make_trace_match(spec: str | None):
    """--trace SYMBOL:VENUE_A-VENUE_B:W:H:THETA (venue order is ignored)."""
    if not spec:
        return lambda *_: False
    parts = spec.split(":")
    if len(parts) != 5:
        raise SystemExit("[basis] --trace wants SYMBOL:VENUE_A-VENUE_B:W:H:THETA")
    symbol, venues, w_txt, h_txt, theta_txt = parts
    want = frozenset(v for v in venues.split("-") if v)
    if len(want) != 2:
        raise SystemExit(f"[basis] --trace needs two venues, got {venues!r}")
    w, h, theta = float(w_txt), float(h_txt), float(theta_txt)

    def match(sym: str, a: str, b: str, window_s: float, hold_s: float, th: float) -> bool:
        return (sym == symbol.upper() and frozenset((a, b)) == want
                and window_s == w and hold_s == h and th == theta)

    return match


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rolling-baseline basis reversion backtest on the spread watch CSVs",
    )
    parser.add_argument("--dir", type=Path, default=Path("reports/stage1"),
                        help="directory holding the csv files")
    parser.add_argument("--stamp", required=True, help="run stamp, e.g. 20260907T135945Z")
    parser.add_argument("--symbols", default=None,
                        help="comma list to restrict to; default: every symbol of the stamp")
    parser.add_argument("--window-s", default="60",
                        help="baseline window(s) in seconds, comma list, e.g. 60,300")
    parser.add_argument("--min-window-n", type=int, default=20,
                        help="minimum samples inside the window before a signal is armed")
    parser.add_argument("--thetas", default="1,1.5,2,3,4,6",
                        help="entry thresholds on dev, in bps")
    parser.add_argument("--hold-s", default="30",
                        help="max holding time(s) in seconds, comma list, e.g. 30,120")
    parser.add_argument("--target-bps", type=float, default=0.0,
                        help="round-trip pnl that ends the hold early")
    parser.add_argument("--reserve-bps", type=float, default=0.0,
                        help="extra bps charged once per round trip (leg-failure reserve)")
    parser.add_argument("--max-age-ms", type=float, default=5000.0,
                        help="drop samples whose book is older than this on either leg")
    parser.add_argument("--skip-depth", action="store_true",
                        help="do not read depth_*.csv (drops the 2 bps capacity column)")
    parser.add_argument("--trace", default=None,
                        help="dump the first trades of one run: SYMBOL:VENUE_A-VENUE_B:W:H:THETA")
    parser.add_argument("--trace-n", type=int, default=5, help="how many traced trades")
    parser.add_argument("--from", dest="ts_from", default=None,
                        help="UTC ISO start of the analysis window")
    parser.add_argument("--to", dest="ts_to", default=None,
                        help="UTC ISO end of the analysis window")
    parser.add_argument("--md", type=Path, default=None, help="also write the report here")
    args = parser.parse_args()

    if not args.dir.is_dir():
        raise SystemExit(f"[basis] not a directory: {args.dir}")
    args.windows = _floats(args.window_s, "--window-s")
    args.holds = _floats(args.hold_s, "--hold-s")
    args.thetas = sorted(_floats(args.thetas, "--thetas"))
    args.trace_match = _make_trace_match(args.trace)
    try:
        args.t_from = parse_iso(args.ts_from).timestamp() if args.ts_from else None
        args.t_to = parse_iso(args.ts_to).timestamp() if args.ts_to else None
    except ValueError as exc:
        raise SystemExit(f"[basis] bad --from/--to: {exc}") from exc
    if args.t_from is not None and args.t_to is not None and args.t_to <= args.t_from:
        raise SystemExit("[basis] --to must be after --from")

    found = discover(args.dir, args.stamp)
    if not found:
        raise SystemExit(f"[basis] no spread_*_{args.stamp}*.csv under {args.dir}")
    if args.symbols:
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        missing = [s for s in wanted if s not in {f.symbol for f in found}]
        if missing:
            raise SystemExit(
                f"[basis] no files for {missing} at stamp {args.stamp}; "
                f"present: {[f.symbol for f in found]}",
            )
        found = [f for f in found if f.symbol in wanted]

    window = ""
    if args.ts_from or args.ts_to:
        window = f"  |  window filter {args.ts_from or 'start'} .. {args.ts_to or 'end'}"
    started = time.monotonic()
    lines = [
        f"# Basis reversion - stamp {args.stamp}",
        "",
        f"source `{args.dir}`  |  symbols {', '.join(f.symbol for f in found)}",
        "",
        f"baseline windows {args.window_s}s (min {args.min_window_n} samples)  |  "
        f"holds {args.hold_s}s  |  thetas {', '.join(f'{t:g}' for t in args.thetas)} bps  |  "
        f"target {args.target_bps:g} bps  |  reserve {args.reserve_bps:g} bps  |  "
        f"max age {args.max_age_ms:g} ms{window}",
        "",
        "_signal `dev = gross - trailing W-second mean of gross` (current sample excluded); "
        "entry takes both legs when `dev > theta` and the pair is flat; exit is the first "
        "reverse-direction sample inside H with `entry_gross + exit_gross - fee_rt - reserve "
        ">= target`, else the last reverse sample inside H (forced). One position per venue "
        "pair, shared by both directions. `pos%` = share of trades with pnl >= 0. `split` = "
        "trades of direction 1 / direction 2 in the pair heading order. `tob med USD` = median "
        "`min(sell_bid_size, buy_ask_size) x mid` at entry, mid = mean of the two venue mids; "
        "`d2bps med USD` = median `min(sell bid_usd_2bps, buy ask_usd_2bps)` within 2 s of "
        "entry; `USD pnl` = sum(pnl_bps x tob / 1e4)._",
        "",
    ]
    for files in found:
        lines += analyse(files, args)
    elapsed = time.monotonic() - started
    lines.append(f"_runtime {elapsed:.1f} s_")
    text = "\n".join(lines).rstrip() + "\n"
    print(text, end="")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(text, encoding="utf-8")
        print(f"[basis] markdown written to {args.md}  ({elapsed:.1f} s)")


if __name__ == "__main__":
    main()

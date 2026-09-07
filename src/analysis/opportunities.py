#!/usr/bin/env python3
"""Offline opportunity analysis for the stage-1 read-only spread watch CSVs.

Reads one run stamp's files produced by ``src/spread_watch.py``

    spread_<SYM>_<VENUES>_<stamp>.csv        hits: every evaluation with net_bps > 0
    spread_<SYM>_<VENUES>_<stamp>_all.csv    1 s samples per direction, any sign
    depth_<SYM>_<VENUES>_<stamp>.csv         per-venue book capacity, 1 row/venue/s

and reports, per symbol and per direction (sell_venue -> buy_venue):

    1. basis        gross bps distribution from the 1 s samples, vs fee+reserve
    2. episodes     consecutive hits merged into one deduplicated opportunity
    3. capacity     top-of-book and depth-bucket fillable notional per episode
    4. round trip   taker in at the episode start, taker out within --hold-s
    5. funding      hourly-normalised carry per venue pair

Read-only, stdlib only, streams every file once.

    python src/analysis/opportunities.py --dir reports/stage1 \
        --stamp 20260907T094928Z [--symbols SOL,HYPE] [--gap-s 2] [--hold-s 30] \
        [--min-usd 1000] [--from 2026-09-07T13:30:00Z --to 2026-09-07T20:00:00Z] \
        [--md reports/stage1/opps.md]
"""

from __future__ import annotations

import argparse
import bisect
import csv
import statistics
import sys
from array import array
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

_SRC_DIR = Path(__file__).resolve().parents[1]
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from spread_watch import INSTRUMENTS, RESERVE_BPS  # noqa: E402  (needs sys.path above)

CAP_BPS = (2, 5, 10)  # depth buckets written by spread_watch, in bps off the touch
DEPTH_TOL_S = 2.0  # a depth row this far from the episode start still counts
# Funding settlement interval per venue: the CSV stores the RAW venue rate.
# HL and Lighter settle hourly, Aster every 8 hours. The unit of the raw number
# was never verified against a settlement, so the hourly figure below is an
# assumption, not a measurement.
FUNDING_HOURS = {"HL": 1.0, "LIGHTER": 1.0, "ASTER": 8.0}
DEFAULT_FUNDING_HOURS = 1.0
SUSPICIOUS_BPS_H = 3.0  # |hourly funding| above this is flagged, not trusted


# ---------------------------------------------------------------- small helpers


def pct(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of an already sorted list."""
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def weighted_median(counter: Counter) -> float | None:
    """Median over a value -> count map (funding rates repeat, so this stays small)."""
    items = sorted(counter.items())
    total = sum(c for _, c in items)
    if total == 0:
        return None
    lo_i, hi_i = (total - 1) // 2, total // 2
    cum = 0
    lo = hi = None
    for value, count in items:
        cum += count
        if lo is None and cum > lo_i:
            lo = value
        if cum > hi_i:
            hi = value
            break
    return (lo + hi) / 2.0


def fmt(value: float | None, nd: int = 2, na: str = "-") -> str:
    return na if value is None else f"{value:.{nd}f}"


def fmt_usd(value: float | None) -> str:
    if value is None:
        return "-"
    if abs(value) >= 1e6:
        return f"{value / 1e6:.2f}M"
    if abs(value) >= 1e3:
        return f"{value / 1e3:.1f}k"
    return f"{value:.0f}"


def table(headers: list[str], rows: list[list[object]], align: str = "") -> list[str]:
    """Markdown table that also reads as an aligned console table."""
    align = align or "r" * len(headers)
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(3, len(h)) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))

    def line(row: list[str]) -> str:
        out = [
            cell.ljust(widths[i]) if align[i] == "l" else cell.rjust(widths[i])
            for i, cell in enumerate(row)
        ]
        return "| " + " | ".join(out) + " |"

    sep = "| " + " | ".join(
        ("-" * widths[i] if align[i] == "l" else "-" * (widths[i] - 1) + ":")
        for i in range(len(headers))
    ) + " |"
    return [line(headers), sep, *[line(row) for row in cells]]


# ---------------------------------------------------------------- timestamps

_HOUR_EPOCH: dict[str, float] = {}


def _hour_epoch(key: str) -> float:
    moment = datetime(
        int(key[0:4]), int(key[5:7]), int(key[8:10]), int(key[11:13]), tzinfo=timezone.utc,
    )
    return moment.timestamp()


def parse_ts(text: str) -> float:
    """'2026-09-07T09:49:32.898+00:00' -> epoch seconds. Fast path + ISO fallback."""
    if (
        len(text) == 29
        and text[10] == "T"
        and text[13] == ":"
        and text[16] == ":"
        and text[19] == "."
        and text[23:] == "+00:00"
    ):
        key = text[:13]
        base = _HOUR_EPOCH.get(key)
        if base is None:
            base = _hour_epoch(key)
            _HOUR_EPOCH[key] = base
        return base + int(text[14:16]) * 60.0 + float(text[17:23])
    return parse_iso(text).timestamp()


def parse_iso(text: str) -> datetime:
    value = text.strip()
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    moment = datetime.fromisoformat(value)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def iso(epoch: float | None) -> str:
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- csv streaming


def _reader(path: Path, needed: list[str]):
    """Yield (row list, column index map). Tolerates a truncated final line."""
    handle = path.open("r", newline="", encoding="utf-8", errors="replace")
    try:
        rows = csv.reader(handle)
        try:
            header = next(rows)
        except StopIteration:
            return
        idx = {name: i for i, name in enumerate(header)}
        missing = [c for c in needed if c not in idx]
        if missing:
            raise SystemExit(f"[opps] {path.name}: missing columns {missing}")
        width = len(header)
        yield idx
        for row in rows:
            if len(row) != width:
                continue  # truncated tail row (the watcher may still be appending)
            yield row
    finally:
        handle.close()


@dataclass
class Series:
    """One direction's 1 s samples from _all.csv: time + gross bps, nothing else."""

    t: array = field(default_factory=lambda: array("d"))
    gross: array = field(default_factory=lambda: array("d"))

    def __len__(self) -> int:
        return len(self.t)


@dataclass
class AllData:
    series: dict[tuple[str, str], Series] = field(default_factory=dict)
    funding_venue: dict[str, Counter] = field(default_factory=dict)
    funding_pair: dict[tuple[str, str], Counter] = field(default_factory=dict)
    rows: int = 0
    skipped: int = 0
    fee_seen: dict[tuple[str, str], float] = field(default_factory=dict)


SPREAD_COLS = [
    "ts_utc", "sell_venue", "buy_venue", "gross_bps", "net_bps",
    "sell_bid", "sell_bid_size", "buy_ask", "buy_ask_size",
    "sell_ask", "buy_bid", "funding_sell", "funding_buy",
]


def load_all(path: Path, t_from: float | None, t_to: float | None) -> AllData:
    """Stream _all.csv once: per-direction (t, gross) arrays + funding counters."""
    data = AllData()
    if not path.exists():
        return data
    stream = _reader(path, SPREAD_COLS)
    idx = next(stream, None)
    if idx is None:
        return data
    c_ts, c_sell, c_buy = idx["ts_utc"], idx["sell_venue"], idx["buy_venue"]
    c_gross, c_net = idx["gross_bps"], idx["net_bps"]
    c_fs, c_fb = idx["funding_sell"], idx["funding_buy"]
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
        series = data.series.get(key)
        if series is None:
            series = data.series[key] = Series()
            try:
                data.fee_seen[key] = gross - float(row[c_net])
            except ValueError:
                pass
        series.t.append(t)
        series.gross.append(gross)
        data.rows += 1
        raw_sell, raw_buy = row[c_fs], row[c_fb]
        if raw_sell:
            data.funding_venue.setdefault(key[0], Counter())[raw_sell] += 1
        if raw_buy:
            data.funding_venue.setdefault(key[1], Counter())[raw_buy] += 1
        if raw_sell and raw_buy:
            data.funding_pair.setdefault(key, Counter())[(raw_sell, raw_buy)] += 1
    for series in data.series.values():  # append order is time order, but never assume
        if any(series.t[i] > series.t[i + 1] for i in range(len(series.t) - 1)):
            order = sorted(range(len(series.t)), key=series.t.__getitem__)
            series.t = array("d", (series.t[i] for i in order))
            series.gross = array("d", (series.gross[i] for i in order))
    return data


@dataclass
class DepthSeries:
    t: array = field(default_factory=lambda: array("d"))
    bid: list[array] = field(default_factory=lambda: [array("d") for _ in CAP_BPS])
    ask: list[array] = field(default_factory=lambda: [array("d") for _ in CAP_BPS])


def load_depth(
    path: Path, t_from: float | None, t_to: float | None,
) -> tuple[dict[str, DepthSeries], int, int]:
    """Stream depth_*.csv once: per-venue time + cumulative USD per bps bucket."""
    out: dict[str, DepthSeries] = {}
    rows = empty = 0
    if not path.exists():
        return out, rows, empty
    needed = ["ts_utc", "venue", *[f"bid_usd_{b}bps" for b in CAP_BPS],
              *[f"ask_usd_{b}bps" for b in CAP_BPS]]
    stream = _reader(path, needed)
    idx = next(stream, None)
    if idx is None:
        return out, rows, empty
    c_ts, c_venue = idx["ts_utc"], idx["venue"]
    c_bid = [idx[f"bid_usd_{b}bps"] for b in CAP_BPS]
    c_ask = [idx[f"ask_usd_{b}bps"] for b in CAP_BPS]
    for row in stream:
        try:
            t = parse_ts(row[c_ts])
        except (ValueError, IndexError):
            continue
        if (t_from is not None and t < t_from) or (t_to is not None and t > t_to):
            continue
        rows += 1
        if not row[c_bid[0]] or not row[c_ask[0]]:
            empty += 1  # the book was not populated on this tick
            continue
        try:
            bids = [float(row[c]) for c in c_bid]
            asks = [float(row[c]) for c in c_ask]
        except ValueError:
            empty += 1
            continue
        series = out.get(row[c_venue])
        if series is None:
            series = out[row[c_venue]] = DepthSeries()
        series.t.append(t)
        for i in range(len(CAP_BPS)):
            series.bid[i].append(bids[i])
            series.ask[i].append(asks[i])
    return out, rows, empty


def nearest_index(times: array, t: float, tol: float) -> int | None:
    """Index of the sample closest to t, or None if the closest is more than tol away."""
    n = len(times)
    if n == 0:
        return None
    i = bisect.bisect_left(times, t)
    best = None
    best_gap = tol
    for j in (i - 1, i):
        if 0 <= j < n:
            gap = abs(times[j] - t)
            if gap <= best_gap:
                best, best_gap = j, gap
    return best


# ---------------------------------------------------------------- episodes


@dataclass
class Episode:
    """Consecutive hits of one direction, merged into one opportunity."""

    key: tuple[str, str]
    t_start: float
    t_end: float
    hits: int
    entry_gross: float
    entry_net: float
    entry_mid: float
    best_net: float
    cap_top_first: float
    cap_top_median: float
    cap_depth: float | None = None
    depth_bucket: int | None = None
    depth_note: str = ""

    @property
    def duration(self) -> float:
        return self.t_end - self.t_start


class _OpenEpisode:
    __slots__ = ("key", "t_start", "t_end", "hits", "entry_gross", "entry_net",
                 "entry_mid", "best_net", "cap_first", "caps")

    def __init__(self, key, t, gross, net, mid, cap):
        self.key = key
        self.t_start = self.t_end = t
        self.hits = 1
        self.entry_gross = gross
        self.entry_net = net
        self.entry_mid = mid
        self.best_net = net
        self.cap_first = cap
        self.caps = [cap]

    def update(self, t, net, cap):
        self.t_end = t
        self.hits += 1
        if net > self.best_net:
            self.best_net = net
        self.caps.append(cap)

    def close(self) -> Episode:
        return Episode(
            key=self.key, t_start=self.t_start, t_end=self.t_end, hits=self.hits,
            entry_gross=self.entry_gross, entry_net=self.entry_net, entry_mid=self.entry_mid,
            best_net=self.best_net, cap_top_first=self.cap_first,
            cap_top_median=statistics.median(self.caps),
        )


def build_episodes(
    path: Path, t_from: float | None, t_to: float | None, gap_s: float,
) -> tuple[list[Episode], int]:
    """Stream the hits csv once, merging consecutive same-direction rows."""
    episodes: list[Episode] = []
    hits = 0
    if not path.exists():
        return episodes, hits
    stream = _reader(path, SPREAD_COLS)
    idx = next(stream, None)
    if idx is None:
        return episodes, hits
    c_ts, c_sell, c_buy = idx["ts_utc"], idx["sell_venue"], idx["buy_venue"]
    c_gross, c_net = idx["gross_bps"], idx["net_bps"]
    c_sb, c_sbs = idx["sell_bid"], idx["sell_bid_size"]
    c_ba, c_bas = idx["buy_ask"], idx["buy_ask_size"]
    c_sa, c_bb = idx["sell_ask"], idx["buy_bid"]
    live: dict[tuple[str, str], _OpenEpisode] = {}
    for row in stream:
        try:
            t = parse_ts(row[c_ts])
            gross, net = float(row[c_gross]), float(row[c_net])
            sell_bid, sell_bid_size = float(row[c_sb]), float(row[c_sbs])
            buy_ask, buy_ask_size = float(row[c_ba]), float(row[c_bas])
            sell_ask, buy_bid = float(row[c_sa]), float(row[c_bb])
        except (ValueError, IndexError):
            continue
        if (t_from is not None and t < t_from) or (t_to is not None and t > t_to):
            continue
        hits += 1
        # same mid as spread_watch._evaluate: mean of the two venue mids
        mid = ((sell_bid + sell_ask) / 2.0 + (buy_bid + buy_ask) / 2.0) / 2.0
        cap = min(sell_bid_size, buy_ask_size) * mid
        key = (row[c_sell], row[c_buy])
        current = live.get(key)
        if current is not None and t - current.t_end > gap_s:
            episodes.append(current.close())
            current = None
        if current is None:
            live[key] = _OpenEpisode(key, t, gross, net, mid, cap)
        else:
            current.update(t, net, cap)
    for current in live.values():
        episodes.append(current.close())
    episodes.sort(key=lambda e: (e.t_start, e.key))
    return episodes, hits


def attach_depth_capacity(
    episodes: list[Episode], depth: dict[str, DepthSeries], min_bucket_net: float = 2.0,
) -> None:
    """Fillable notional that still clears the fee: min(sell bid_usd_N, buy ask_usd_N)."""
    for ep in episodes:
        bucket = None
        for bps in CAP_BPS:
            if bps <= ep.best_net:
                bucket = bps
        if bucket is None:
            ep.depth_note = "tob-only"  # best net below the 2 bps bucket
            continue
        sell_series = depth.get(ep.key[0])
        buy_series = depth.get(ep.key[1])
        if sell_series is None or buy_series is None:
            ep.depth_note = "no-depth"
            continue
        i = nearest_index(sell_series.t, ep.t_start, DEPTH_TOL_S)
        j = nearest_index(buy_series.t, ep.t_start, DEPTH_TOL_S)
        if i is None or j is None:
            ep.depth_note = "no-depth"
            continue
        col = CAP_BPS.index(bucket)
        ep.depth_bucket = bucket
        ep.cap_depth = min(sell_series.bid[col][i], buy_series.ask[col][j])


# ---------------------------------------------------------------- round trip


@dataclass
class Trade:
    key: tuple[str, str]
    t_in: float
    t_out: float
    pnl_bps: float
    cap_usd: float
    forced: bool  # True: hold-s expired before the round trip cleared the fees

    @property
    def hold(self) -> float:
        return self.t_out - self.t_in


def round_trips(
    episodes: list[Episode], data: AllData, fees: dict[str, float], hold_s: float,
) -> tuple[list[Trade], int]:
    """Taker in at the episode start, taker out on the reverse direction within hold_s."""
    trades: list[Trade] = []
    unresolved = 0
    for ep in episodes:
        sell, buy = ep.key
        reverse = data.series.get((buy, sell))
        if reverse is None or len(reverse) == 0:
            unresolved += 1
            continue
        leg_fees = fees.get(sell, 0.0) + fees.get(buy, 0.0)
        entry_fee = leg_fees + RESERVE_BPS  # reserve is charged once, at entry
        exit_fee = leg_fees
        lo = bisect.bisect_right(reverse.t, ep.t_start)
        limit = ep.t_start + hold_s
        chosen = None
        forced = True
        j = lo
        while j < len(reverse) and reverse.t[j] <= limit:
            pnl = ep.entry_gross + reverse.gross[j] - entry_fee - exit_fee
            if pnl >= 0.0:
                chosen, forced = j, False
                break
            chosen = j
            j += 1
        if chosen is None:
            unresolved += 1
            continue
        pnl = ep.entry_gross + reverse.gross[chosen] - entry_fee - exit_fee
        trades.append(Trade(ep.key, ep.t_start, reverse.t[chosen], pnl, ep.cap_top_first, forced))
    return trades, unresolved


# ---------------------------------------------------------------- funding


def hourly_bps(raw: float, venue: str) -> float:
    return raw / FUNDING_HOURS.get(venue, DEFAULT_FUNDING_HOURS) * 1e4


@dataclass
class FundingPair:
    venues: tuple[str, str]
    short: str
    long: str
    carry_bps_h: float
    samples: int


def funding_report(
    data: AllData, fees: dict[str, float],
) -> tuple[list[list[object]], list[FundingPair], list[str]]:
    """Per-venue raw/hourly medians plus the per-pair carry of short-high/long-low."""
    venue_rows: list[list[object]] = []
    flags: list[str] = []
    for venue in sorted(data.funding_venue):
        counter = Counter()
        for raw, count in data.funding_venue[venue].items():
            try:
                counter[float(raw)] += count
            except ValueError:
                continue
        median_raw = weighted_median(counter)
        if median_raw is None:
            continue
        interval = FUNDING_HOURS.get(venue, DEFAULT_FUNDING_HOURS)
        per_h = hourly_bps(median_raw, venue)
        venue_rows.append([
            venue, f"{median_raw:.10f}".rstrip("0").rstrip("."), f"{interval:g}",
            fmt(per_h, 4), fmt(per_h * 24 * 365 / 100.0, 1), len(counter),
        ])
        if abs(per_h) > SUSPICIOUS_BPS_H:
            flags.append(
                f"{venue} funding {median_raw:.8f} raw -> {per_h:.2f} bps/h "
                f"({per_h * 24 * 365 / 100.0:.0f}%/yr) is implausibly large: the raw unit "
                f"was never verified, do NOT trade on this number",
            )
        elif median_raw == 0.0:
            flags.append(f"{venue} funding is exactly 0 in every sample (feed may be idle)")

    seen: set[frozenset[str]] = set()
    pairs: list[FundingPair] = []
    for key in sorted(data.funding_pair):
        tag = frozenset(key)
        if len(tag) != 2 or tag in seen:
            continue
        seen.add(tag)
        sell, buy = key
        diff = Counter()
        for (raw_sell, raw_buy), count in data.funding_pair[key].items():
            try:
                delta = hourly_bps(float(raw_sell), sell) - hourly_bps(float(raw_buy), buy)
            except ValueError:
                continue
            diff[delta] += count
        median_diff = weighted_median(diff)
        if median_diff is None:
            continue
        short, long_ = (sell, buy) if median_diff >= 0 else (buy, sell)
        pairs.append(FundingPair(
            venues=(sell, buy), short=short, long=long_,
            carry_bps_h=abs(median_diff), samples=sum(diff.values()),
        ))
    return venue_rows, pairs, flags


# ---------------------------------------------------------------- per symbol


@dataclass
class SymbolFiles:
    symbol: str
    venues: str
    hits: Path
    all: Path
    depth: Path


def discover(directory: Path, stamp: str) -> list[SymbolFiles]:
    found: dict[str, SymbolFiles] = {}
    for path in sorted(directory.glob(f"spread_*_{stamp}*.csv")):
        name = path.name
        if name.endswith(f"_{stamp}_all.csv"):
            stem = name[len("spread_"):-len(f"_{stamp}_all.csv")]
        elif name.endswith(f"_{stamp}.csv"):
            stem = name[len("spread_"):-len(f"_{stamp}.csv")]
        else:
            continue
        symbol, _, venues = stem.rpartition("_")
        if not symbol or not venues:
            continue
        found.setdefault(symbol, SymbolFiles(
            symbol=symbol,
            venues=venues,
            hits=directory / f"spread_{symbol}_{venues}_{stamp}.csv",
            all=directory / f"spread_{symbol}_{venues}_{stamp}_all.csv",
            depth=directory / f"depth_{symbol}_{venues}_{stamp}.csv",
        ))
    return [found[s] for s in sorted(found)]


def venue_fees(symbol: str) -> tuple[dict[str, float], str]:
    mapping = INSTRUMENTS.get(symbol)
    if mapping is None:
        return {}, f"{symbol} is not in spread_watch.INSTRUMENTS: fees taken from the csv"
    return {venue: spec[1] for venue, spec in mapping.items()}, ""


def analyse(files: SymbolFiles, args) -> list[str]:
    t_from = args.t_from
    t_to = args.t_to
    fees, fee_note = venue_fees(files.symbol)
    data = load_all(files.all, t_from, t_to)
    depth, depth_rows, depth_empty = load_depth(files.depth, t_from, t_to)
    episodes, hit_rows = build_episodes(files.hits, t_from, t_to, args.gap_s)
    attach_depth_capacity(episodes, depth)

    notes: list[str] = []
    if fee_note:
        notes.append(fee_note)
    # fee+reserve per direction: INSTRUMENTS is the source, the csv is the cross-check
    threshold: dict[tuple[str, str], float] = {}
    for key in sorted(set(data.series) | {e.key for e in episodes}):
        if fees:
            value = fees.get(key[0], 0.0) + fees.get(key[1], 0.0) + RESERVE_BPS
        else:
            value = data.fee_seen.get(key, float("nan"))
        threshold[key] = value
        seen = data.fee_seen.get(key)
        if seen is not None and abs(seen - value) > 0.01:
            notes.append(
                f"fee mismatch {key[0]}>{key[1]}: INSTRUMENTS says {value:.2f} bps, "
                f"the csv rows imply {seen:.2f} bps",
            )
    if not fees:
        for key, value in data.fee_seen.items():
            threshold[key] = value
            fees.setdefault(key[0], 0.0)

    t_min = min((s.t[0] for s in data.series.values() if len(s)), default=None)
    t_max = max((s.t[-1] for s in data.series.values() if len(s)), default=None)
    if episodes:
        t_min = min(t_min, episodes[0].t_start) if t_min is not None else episodes[0].t_start
        last = max(e.t_end for e in episodes)
        t_max = max(t_max, last) if t_max is not None else last
    span_h = (t_max - t_min) / 3600.0 if (t_min is not None and t_max is not None) else 0.0

    lines: list[str] = []
    lines.append(f"## {files.symbol}  [{files.venues.replace('-', ', ')}]")
    lines.append("")
    lines.append(
        f"window {iso(t_min)} .. {iso(t_max)}  ({span_h:.2f} h)  |  "
        f"samples {data.rows}  hits {hit_rows}  episodes {len(episodes)}  "
        f"depth rows {depth_rows} ({depth_empty} empty)",
    )
    lines.append("")

    # ---- 1. basis
    lines.append("### 1. Basis (1 s samples, gross bps)")
    lines.append("")
    rows = []
    for key in sorted(data.series):
        series = data.series[key]
        values = sorted(series.gross)
        fee = threshold.get(key, float("nan"))
        positive = sum(1 for g in values if g - fee > 0.0)
        rows.append([
            f"{key[0]}>{key[1]}", len(values),
            fmt(statistics.median(values)), fmt(pct(values, 0.95)),
            fmt(values[-1]), fmt(values[0]), fmt(fee),
            f"{100.0 * positive / len(values):.2f}%",
        ])
    if rows:
        lines += table(
            ["direction", "n", "median", "p95", "max", "min", "fee+res", "net>0"],
            rows, align="lrrrrrrr",
        )
    else:
        lines.append("_no _all.csv samples in the window_")
    lines.append("")

    by_dir: dict[tuple[str, str], list[Episode]] = {}
    for ep in episodes:
        by_dir.setdefault(ep.key, []).append(ep)

    # ---- 2. episodes
    lines.append(f"### 2. Episodes (hits merged, gap <= {args.gap_s:g} s)")
    lines.append("")
    if by_dir:
        rows = []
        for key in sorted(by_dir):
            eps = by_dir[key]
            durations = sorted(e.duration for e in eps)
            bests = sorted(e.best_net for e in eps)
            total = sum(durations)
            rows.append([
                f"{key[0]}>{key[1]}", len(eps),
                fmt(len(eps) / span_h, 1) if span_h > 0 else "-",
                fmt(total, 1), f"{100.0 * total / (span_h * 3600.0):.2f}%" if span_h > 0 else "-",
                fmt(statistics.median(durations)), fmt(durations[-1]),
                fmt(statistics.median(bests)), fmt(bests[-1]),
                sum(e.hits for e in eps),
            ])
        lines += table(
            ["direction", "episodes", "eps/h", "net+ s", "% of window",
             "dur med", "dur max", "best net med", "best net max", "raw hits"],
            rows, align="lrrrrrrrrr",
        )
        lines.append("")
        lines.append(
            "_a single-row episode has duration 0 s: the book was net-positive on one "
            "evaluation only_",
        )
    else:
        lines.append("_no net-positive rows in the window: no episodes, no round trips_")
    lines.append("")

    # ---- 3. capacity
    if by_dir:
        lines.append(f"### 3. Capacity per episode (USD, min-usd {args.min_usd:g})")
        lines.append("")
        rows = []
        for key in sorted(by_dir):
            eps = by_dir[key]
            tob = sorted(e.cap_top_first for e in eps)
            tob_med = sorted(e.cap_top_median for e in eps)
            dep = sorted(e.cap_depth for e in eps if e.cap_depth is not None)
            buckets = Counter(e.depth_bucket for e in eps if e.depth_bucket is not None)
            notes_c = Counter(e.depth_note for e in eps if e.depth_note)
            rows.append([
                f"{key[0]}>{key[1]}",
                fmt_usd(statistics.median(tob)), fmt_usd(pct(tob, 0.90)),
                fmt_usd(statistics.median(tob_med)),
                fmt_usd(statistics.median(dep) if dep else None),
                fmt_usd(pct(dep, 0.90) if dep else None),
                sum(1 for v in tob if v >= args.min_usd),
                sum(1 for v in dep if v >= args.min_usd),
                len(dep),
                " ".join(f"{b}bps:{c}" for b, c in sorted(buckets.items())) or "-",
                " ".join(f"{n}:{c}" for n, c in sorted(notes_c.items())) or "-",
            ])
        lines += table(
            ["direction", "tob med", "tob p90", "tob epi-med", "depth med", "depth p90",
             f"tob>={args.min_usd:g}", f"depth>={args.min_usd:g}", "depth n", "bucket", "skipped"],
            rows, align="lrrrrrrrrll",
        )
        lines.append("")
        lines.append(
            "_tob = min(sell_bid_size, buy_ask_size) x mid at the episode's first row; "
            "depth = min(sell bid_usd_N, buy ask_usd_N) at the episode start, N = largest "
            "of 2/5/10 bps at or below the episode's best net; tob-only = best net < 2 bps, "
            "no-depth = no depth row within 2 s_",
        )
        lines.append("")

    # ---- 4. round trip
    if by_dir:
        trades, unresolved = round_trips(episodes, data, fees, args.hold_s)
        lines.append(f"### 4. Round trip (taker in / taker out, hold <= {args.hold_s:g} s)")
        lines.append("")
        by_trade: dict[tuple[str, str], list[Trade]] = {}
        for trade in trades:
            by_trade.setdefault(trade.key, []).append(trade)
        rows = []
        for key in sorted(by_trade):
            group = by_trade[key]
            pnls = sorted(t.pnl_bps for t in group)
            holds = sorted(t.hold for t in group)
            usd = sum(t.pnl_bps * t.cap_usd / 1e4 for t in group)
            rows.append([
                f"{key[0]}>{key[1]}", len(group),
                f"{100.0 * sum(1 for p in pnls if p >= 0.0) / len(pnls):.1f}%",
                fmt(statistics.median(pnls)), fmt(statistics.fmean(pnls)),
                fmt(sum(pnls), 1), fmt(statistics.median(holds), 1),
                fmt_usd(usd), sum(1 for t in group if t.forced),
            ])
        if rows:
            lines += table(
                ["direction", "trades", "pos%", "pnl med", "pnl mean", "pnl total",
                 "hold med s", "USD pnl", "forced exits"],
                rows, align="lrrrrrrrr",
            )
        else:
            lines.append("_no episode had a reverse-direction sample inside the hold window_")
        lines.append("")
        lines.append(
            f"_entry = episode's first hit (gross - taker both legs - {RESERVE_BPS:g} bps "
            "reserve); exit = first reverse 1 s sample whose total pnl >= 0, else the last "
            "sample within the hold; USD pnl = sum(pnl_bps x tob capacity / 1e4); "
            f"unresolved (no reverse sample in the window, excluded): {unresolved}_",
        )
        lines.append("")

    # ---- 5. funding
    venue_rows, pairs, flags = funding_report(data, fees)
    lines.append("### 5. Funding carry (raw -> hourly, UNVERIFIED unit)")
    lines.append("")
    if venue_rows:
        lines += table(
            ["venue", "raw median", "settle h", "bps/h", "%/yr", "distinct raw"],
            venue_rows, align="lrrrrr",
        )
        lines.append("")
    rows = []
    for pair in pairs:
        fee_rt = 2.0 * (fees.get(pair.short, 0.0) + fees.get(pair.long, 0.0))
        breakeven = fee_rt / pair.carry_bps_h if pair.carry_bps_h > 1e-9 else None
        rows.append([
            f"{pair.venues[0]}/{pair.venues[1]}", f"short {pair.short}", f"long {pair.long}",
            fmt(pair.carry_bps_h, 4), fmt(pair.carry_bps_h * 24 * 365 / 100.0, 1),
            fmt(fee_rt), fmt(breakeven, 1) if breakeven is not None else "inf",
            pair.samples,
        ])
    if rows:
        lines += table(
            ["pair", "short leg", "long leg", "carry bps/h", "%/yr",
             "round-trip fee bps", "hours to b/e", "samples"],
            rows, align="lllrrrrr",
        )
    else:
        lines.append("_no funding values in the window_")
    lines.append("")
    for flag in flags:
        lines.append(f"> **FLAG** {flag}")
    if flags:
        lines.append("")
    for note in notes:
        lines.append(f"> note: {note}")
    if notes:
        lines.append("")
    if data.skipped:
        lines.append(f"> note: {data.skipped} unparsable rows skipped in {files.all.name}")
        lines.append("")
    return lines


# ---------------------------------------------------------------- cli


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Offline opportunity analysis of the stage-1 spread watch CSVs",
    )
    parser.add_argument("--dir", type=Path, default=Path("reports/stage1"),
                        help="directory holding the csv files")
    parser.add_argument("--stamp", required=True, help="run stamp, e.g. 20260907T094928Z")
    parser.add_argument("--symbols", default=None,
                        help="comma list to restrict to; default: every symbol of the stamp")
    parser.add_argument("--gap-s", type=float, default=2.0,
                        help="max gap between hits of one episode (seconds)")
    parser.add_argument("--hold-s", type=float, default=30.0,
                        help="max round-trip holding time (seconds)")
    parser.add_argument("--min-usd", type=float, default=1000.0,
                        help="capacity threshold for the episode counts")
    parser.add_argument("--from", dest="ts_from", default=None,
                        help="UTC ISO start of the analysis window, e.g. 2026-09-07T13:30:00Z")
    parser.add_argument("--to", dest="ts_to", default=None,
                        help="UTC ISO end of the analysis window, e.g. 2026-09-07T20:00:00Z")
    parser.add_argument("--md", type=Path, default=None, help="also write the report here")
    args = parser.parse_args()

    if not args.dir.is_dir():
        raise SystemExit(f"[opps] not a directory: {args.dir}")
    try:
        args.t_from = parse_iso(args.ts_from).timestamp() if args.ts_from else None
        args.t_to = parse_iso(args.ts_to).timestamp() if args.ts_to else None
    except ValueError as exc:
        raise SystemExit(f"[opps] bad --from/--to: {exc}") from exc
    if args.t_from is not None and args.t_to is not None and args.t_to <= args.t_from:
        raise SystemExit("[opps] --to must be after --from")

    found = discover(args.dir, args.stamp)
    if not found:
        raise SystemExit(f"[opps] no spread_*_{args.stamp}*.csv under {args.dir}")
    if args.symbols:
        wanted = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        missing = [s for s in wanted if s not in {f.symbol for f in found}]
        if missing:
            raise SystemExit(
                f"[opps] no files for {missing} at stamp {args.stamp}; "
                f"present: {[f.symbol for f in found]}",
            )
        found = [f for f in found if f.symbol in wanted]

    window = ""
    if args.ts_from or args.ts_to:
        window = f"  |  window filter {args.ts_from or 'start'} .. {args.ts_to or 'end'}"
    lines = [
        f"# Opportunity analysis - stamp {args.stamp}",
        "",
        f"source `{args.dir}`  |  symbols {', '.join(f.symbol for f in found)}"
        f"  |  gap {args.gap_s:g}s, hold {args.hold_s:g}s, min-usd {args.min_usd:g}{window}",
        "",
    ]
    for files in found:
        lines += analyse(files, args)
    text = "\n".join(lines).rstrip() + "\n"
    print(text, end="")
    if args.md:
        args.md.parent.mkdir(parents=True, exist_ok=True)
        args.md.write_text(text, encoding="utf-8")
        print(f"[opps] markdown written to {args.md}")


if __name__ == "__main__":
    main()

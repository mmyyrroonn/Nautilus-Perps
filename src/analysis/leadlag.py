#!/usr/bin/env python3
"""
Lead-lag analysis of ``<stem>_ref.csv``: does the perp quote lag the real stock,
and is the lag worth taking?

The watcher (``src/spread_watch.py --reference ...``) writes one row per reference
update and one per perp quote update, each row carrying the reference price and
every perp leg's touch at that instant, plus the fee-netted edges:

    buy_edge_bps  = (ref_mid - ask) / ask * 1e4 - taker_fee_bps
    sell_edge_bps = (bid - ref_mid) / bid * 1e4 - taker_fee_bps

This script answers four questions per leg:

1. How large are those edges - time-weighted p50 / p90 / p99 / max, and what
   share of the session sits above 0 / 2 / 5 bps.
2. How long does one opportunity last - contiguous windows above the threshold,
   how many per hour, and the p50 / p90 of their duration.
3. Does the perp actually follow the stock - cross-correlation of 100 ms return
   series over -5 s .. +5 s; a positive lag means the perp moves later.
4. What a naive follower would have made - take the perp side when the edge
   opens, unwind at that leg's mid T seconds later, two taker fees paid.

    python src/analysis/leadlag.py reports/stage1/NVDA_HL-LIGHTER_..._ref.csv \\
        --rth-only --threshold-bps 2 --out reports/leadlag.md

pandas / numpy only.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pandas as pd

# US regular trading hours in UTC (13:30-20:00; the watcher writes UTC timestamps).
RTH_START = pd.Timedelta(hours=13, minutes=30)
RTH_END = pd.Timedelta(hours=20)
# Taker fees the watcher used when it computed the edge columns; the CSV does not
# carry them, so the round-trip simulation re-applies them from here. Keep in sync
# with the *_TAKER_FEE_BPS constants in src/spread_watch.py.
DEFAULT_FEE_BPS = {
    "HL": 0.9,
    "LIGHTER": 0.0,
    "LIGHTER_RH": 0.0,
    "ASTER": 0.9,
}
RESAMPLE_MS = 100
MAX_LAG_MS = 5_000
THRESHOLDS = (0.0, 2.0, 5.0)


# --------------------------------------------------------------------------------- loading


def venues_in(columns) -> list[str]:
    """Leg names, in file order, from the ``<venue>_buy_edge_bps`` columns."""
    return [c[: -len("_buy_edge_bps")] for c in columns if c.endswith("_buy_edge_bps")]


def load_ref_csv(path: Path, rth_only: bool = False) -> tuple[pd.DataFrame, list[str]]:
    """Read a ``_ref.csv``, parse timestamps, optionally keep only US RTH rows."""
    frame = pd.read_csv(path)
    if "ts_utc" not in frame.columns:
        raise SystemExit(f"[leadlag] {path}: no ts_utc column; is this a _ref.csv?")
    frame["ts"] = pd.to_datetime(frame["ts_utc"], utc=True, format="mixed")
    frame = frame.sort_values("ts").reset_index(drop=True)
    if rth_only:
        since_midnight = frame["ts"] - frame["ts"].dt.normalize()
        frame = frame[(since_midnight >= RTH_START) & (since_midnight < RTH_END)]
        frame = frame.reset_index(drop=True)
    venues = venues_in(frame.columns)
    if not venues:
        raise SystemExit(f"[leadlag] {path}: no <venue>_buy_edge_bps columns found")
    return frame, venues


def _gap_seconds(ts: pd.Series) -> np.ndarray:
    """Gaps between consecutive rows, in seconds.

    Uses ``dt.total_seconds()`` rather than an int64 view: pandas 3 parses these
    timestamps at microsecond resolution, so dividing the raw integers by 1e9
    would silently under-count every duration by 1000x.
    """
    return ts.diff().dt.total_seconds().to_numpy()[1:]


def _weights(ts: pd.Series) -> np.ndarray:
    """Seconds each row 'owns': until the next row, last row gets the median gap."""
    if len(ts) < 2:
        return np.ones(len(ts))
    gaps = _gap_seconds(ts)
    tail = float(np.median(gaps)) if len(gaps) else 1.0
    return np.append(gaps, tail)


def weighted_quantile(values: np.ndarray, weights: np.ndarray, q: float) -> float:
    """Time-weighted quantile: the value at which cumulative time crosses q."""
    if len(values) == 0:
        return float("nan")
    order = np.argsort(values)
    v, w = values[order], weights[order]
    total = w.sum()
    if total <= 0:
        return float(np.quantile(values, q))
    cumulative = (np.cumsum(w) - 0.5 * w) / total
    return float(np.interp(q, cumulative, v))


# --------------------------------------------------------------------------------- edges


@dataclass
class EdgeStats:
    """One leg, one direction: how big the edge gets and how often it is open."""

    venue: str
    side: str  # buy / sell
    samples: int
    seconds: float
    p50: float
    p90: float
    p99: float
    max: float
    share: dict[float, float] = field(default_factory=dict)  # threshold bps -> time share
    windows: int = 0
    per_hour: float = 0.0
    dur_p50_ms: float = float("nan")
    dur_p90_ms: float = float("nan")
    dur_max_ms: float = float("nan")


def edge_series(frame: pd.DataFrame, venue: str, side: str) -> pd.DataFrame:
    """Rows where this leg's edge exists, with the time weight of each row."""
    column = f"{venue}_{side}_edge_bps"
    if column not in frame.columns:
        return pd.DataFrame(columns=["ts", "edge", "w"])
    out = frame[["ts", column]].dropna()
    out = out.rename(columns={column: "edge"}).reset_index(drop=True)
    if out.empty:
        return pd.DataFrame(columns=["ts", "edge", "w"])
    out["w"] = _weights(out["ts"])
    return out


def find_windows(series: pd.DataFrame, threshold: float,
                 gap_ms: float = 2_000.0) -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    """Contiguous stretches with edge > threshold.

    A window ends at the first row that falls back to or below the threshold (that
    row's timestamp is the end, so a one-row window still has a real duration), or
    when the feed goes quiet for longer than ``gap_ms``.
    """
    if series.empty:
        return []
    above = (series["edge"] > threshold).to_numpy()
    ts = series["ts"].to_numpy()
    gaps_ok = np.append(True, _gap_seconds(series["ts"]) * 1000.0 <= gap_ms)
    windows: list[tuple[pd.Timestamp, pd.Timestamp]] = []
    start: int | None = None
    for i, flag in enumerate(above):
        if flag and start is None:
            start = i
        elif start is not None and (not flag or not gaps_ok[i]):
            windows.append((pd.Timestamp(ts[start]), pd.Timestamp(ts[i])))
            start = i if (flag and not gaps_ok[i]) else None
    if start is not None:
        windows.append((pd.Timestamp(ts[start]), pd.Timestamp(ts[-1])))
    return windows


def edge_stats(frame: pd.DataFrame, venue: str, side: str, threshold: float) -> EdgeStats:
    series = edge_series(frame, venue, side)
    if series.empty:
        return EdgeStats(venue, side, 0, 0.0, *( [float("nan")] * 4 ))
    values = series["edge"].to_numpy(dtype=float)
    weights = series["w"].to_numpy(dtype=float)
    total = float(weights.sum())
    stats = EdgeStats(
        venue=venue,
        side=side,
        samples=len(series),
        seconds=total,
        p50=weighted_quantile(values, weights, 0.50),
        p90=weighted_quantile(values, weights, 0.90),
        p99=weighted_quantile(values, weights, 0.99),
        max=float(values.max()),
    )
    for bps in THRESHOLDS:
        stats.share[bps] = float(weights[values > bps].sum() / total) if total else 0.0
    windows = find_windows(series, threshold)
    stats.windows = len(windows)
    hours = total / 3600.0
    stats.per_hour = len(windows) / hours if hours > 0 else 0.0
    if windows:
        durations = np.array([(end - start).total_seconds() * 1000.0 for start, end in windows])
        stats.dur_p50_ms = float(np.quantile(durations, 0.50))
        stats.dur_p90_ms = float(np.quantile(durations, 0.90))
        stats.dur_max_ms = float(durations.max())
    return stats


# --------------------------------------------------------------------------------- lead-lag


def mid_frame(frame: pd.DataFrame, venue: str) -> pd.Series:
    """Perp mid from the leg's touch columns, indexed by timestamp."""
    bid, ask = f"{venue}_bid", f"{venue}_ask"
    if bid not in frame.columns or ask not in frame.columns:
        return pd.Series(dtype=float)
    mid = (frame[bid] + frame[ask]) / 2.0
    return pd.Series(mid.to_numpy(), index=frame["ts"]).dropna()


def cross_correlation(ref_mid: pd.Series, perp_mid: pd.Series,
                      resample_ms: int = RESAMPLE_MS,
                      max_lag_ms: int = MAX_LAG_MS) -> pd.DataFrame:
    """Correlation of 100 ms returns at lags -5 s .. +5 s.

    ``lag_ms > 0`` means the perp return at t+lag matches the reference return at
    t, i.e. the perp follows the stock.
    """
    rule = f"{resample_ms}ms"
    ref = ref_mid.resample(rule).last().ffill()
    perp = perp_mid.resample(rule).last().ffill()
    joined = pd.concat({"ref": ref, "perp": perp}, axis=1).dropna()
    if len(joined) < 10:
        return pd.DataFrame(columns=["lag_ms", "corr"])
    returns = np.log(joined).diff().dropna()
    steps = int(max_lag_ms / resample_ms)
    rows = []
    for k in range(-steps, steps + 1):
        shifted = returns["perp"].shift(-k)
        pair = pd.concat({"a": returns["ref"], "b": shifted}, axis=1).dropna()
        if len(pair) < 10 or pair["a"].std() == 0 or pair["b"].std() == 0:
            rows.append((k * resample_ms, float("nan")))
            continue
        rows.append((k * resample_ms, float(pair["a"].corr(pair["b"]))))
    return pd.DataFrame(rows, columns=["lag_ms", "corr"])


def best_lag(correlations: pd.DataFrame) -> tuple[float, float]:
    """(lag_ms, corr) of the strongest positive correlation; NaN when unusable."""
    if correlations.empty or correlations["corr"].isna().all():
        return float("nan"), float("nan")
    row = correlations.loc[correlations["corr"].idxmax()]
    return float(row["lag_ms"]), float(row["corr"])


# --------------------------------------------------------------------------------- follow


@dataclass
class FollowResult:
    """Crude follower: take the stale side, unwind at mid T seconds later."""

    venue: str
    side: str
    trades: int = 0
    wins: int = 0
    mean_bps: float = float("nan")
    total_bps: float = 0.0
    fee_bps: float = 0.0

    @property
    def win_rate(self) -> float:
        return self.wins / self.trades if self.trades else float("nan")


def simulate_follow(frame: pd.DataFrame, venue: str, side: str, threshold: float,
                    hold_secs: float, fee_bps: float) -> FollowResult:
    """One entry per opportunity window; exit at that leg's mid ``hold_secs`` later."""
    result = FollowResult(venue=venue, side=side, fee_bps=fee_bps)
    series = edge_series(frame, venue, side)
    if series.empty:
        return result
    mid = mid_frame(frame, venue)
    if mid.empty:
        return result
    mid = mid[~mid.index.duplicated(keep="last")].sort_index()
    entry_col = f"{venue}_ask" if side == "buy" else f"{venue}_bid"
    prices = pd.Series(frame[entry_col].to_numpy(), index=frame["ts"])
    prices = prices[~prices.index.duplicated(keep="last")].sort_index().dropna()

    pnl: list[float] = []
    for start, _end in find_windows(series, threshold):
        if start not in prices.index:
            candidates = prices.index[prices.index >= start]
            if len(candidates) == 0:
                continue
            start = candidates[0]
        entry = float(prices.loc[start])
        exit_ts = start + pd.Timedelta(seconds=hold_secs)
        later = mid.index[mid.index >= exit_ts]
        if len(later) == 0 or entry <= 0:
            continue
        exit_mid = float(mid.loc[later[0]])
        gross = ((exit_mid - entry) if side == "buy" else (entry - exit_mid)) / entry * 1e4
        pnl.append(gross - 2.0 * fee_bps)
    if pnl:
        array = np.array(pnl)
        result.trades = len(array)
        result.wins = int((array > 0).sum())
        result.mean_bps = float(array.mean())
        result.total_bps = float(array.sum())
    return result


# --------------------------------------------------------------------------------- report


def _fmt(value: float, digits: int = 2) -> str:
    return "-" if value is None or (isinstance(value, float) and np.isnan(value)) else f"{value:.{digits}f}"


def build_report(path: Path, frame: pd.DataFrame, venues: list[str], threshold: float,
                 hold_secs: float, fees: dict[str, float], rth_only: bool) -> str:
    lines: list[str] = []
    span = ""
    if not frame.empty:
        span = (f"{frame['ts'].iloc[0].isoformat(timespec='seconds')} .. "
                f"{frame['ts'].iloc[-1].isoformat(timespec='seconds')}")
    lines.append(f"# lead-lag: {path.name}")
    lines.append("")
    lines.append(f"- rows: {len(frame)}  span: {span}  rth_only: {rth_only}")
    lines.append(f"- threshold: {threshold:g} bps   hold: {hold_secs:g}s   "
                 f"fees(bps): {', '.join(f'{v}={fees[v]:g}' for v in venues)}")
    ref_updates = int(frame["event"].astype(str).str.startswith("ref:").sum()) \
        if "event" in frame.columns else 0
    lines.append(f"- reference update rows: {ref_updates}   perp quote rows: "
                 f"{len(frame) - ref_updates}")
    lines.append("")

    lines.append("## 1. edge distribution (time-weighted)")
    lines.append("")
    lines.append("| leg | side | rows | secs | p50 | p90 | p99 | max | >0 | >2bps | >5bps |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    stats: dict[tuple[str, str], EdgeStats] = {}
    for venue in venues:
        for side in ("buy", "sell"):
            item = edge_stats(frame, venue, side, threshold)
            stats[(venue, side)] = item
            lines.append(
                f"| {venue} | {side} | {item.samples} | {_fmt(item.seconds, 0)} | "
                f"{_fmt(item.p50)} | {_fmt(item.p90)} | {_fmt(item.p99)} | {_fmt(item.max)} | "
                + " | ".join(f"{100 * item.share.get(t, 0.0):.2f}%" for t in THRESHOLDS)
                + " |",
            )
    lines.append("")

    lines.append(f"## 2. persistence of windows above {threshold:g} bps")
    lines.append("")
    lines.append("| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: | ---: |")
    for venue in venues:
        for side in ("buy", "sell"):
            item = stats[(venue, side)]
            lines.append(
                f"| {venue} | {side} | {item.windows} | {_fmt(item.per_hour, 1)} | "
                f"{_fmt(item.dur_p50_ms, 0)} | {_fmt(item.dur_p90_ms, 0)} | "
                f"{_fmt(item.dur_max_ms, 0)} |",
            )
    lines.append("")

    lines.append("## 3. lead-lag (100 ms returns, -5s..+5s; positive lag = perp follows)")
    lines.append("")
    lines.append("| leg | best lag ms | corr | corr at 0 |")
    lines.append("| --- | ---: | ---: | ---: |")
    ref_mid = pd.Series(frame["ref_mid"].to_numpy(), index=frame["ts"]).dropna() \
        if "ref_mid" in frame.columns else pd.Series(dtype=float)
    ref_mid = ref_mid[~ref_mid.index.duplicated(keep="last")].sort_index()
    for venue in venues:
        perp = mid_frame(frame, venue)
        perp = perp[~perp.index.duplicated(keep="last")].sort_index()
        correlations = cross_correlation(ref_mid, perp)
        lag, corr = best_lag(correlations)
        zero = correlations.loc[correlations["lag_ms"] == 0, "corr"]
        lines.append(
            f"| {venue} | {_fmt(lag, 0)} | {_fmt(corr, 3)} | "
            f"{_fmt(float(zero.iloc[0]) if len(zero) else float('nan'), 3)} |",
        )
    lines.append("")

    lines.append(f"## 4. naive follower (enter on window open, exit at mid +{hold_secs:g}s, "
                 f"two taker fees)")
    lines.append("")
    lines.append("| leg | side | trades | win rate | mean bps | total bps |")
    lines.append("| --- | --- | ---: | ---: | ---: | ---: |")
    for venue in venues:
        for side in ("buy", "sell"):
            sim = simulate_follow(frame, venue, side, threshold, hold_secs, fees[venue])
            lines.append(
                f"| {venue} | {side} | {sim.trades} | "
                f"{'-' if sim.trades == 0 else f'{100 * sim.win_rate:.1f}%'} | "
                f"{_fmt(sim.mean_bps)} | {_fmt(sim.total_bps, 1)} |",
            )
    lines.append("")
    return "\n".join(lines)


# --------------------------------------------------------------------------------- cli


def parse_fees(values: list[str] | None, venues: list[str]) -> dict[str, float]:
    fees = {venue: DEFAULT_FEE_BPS.get(venue, 0.0) for venue in venues}
    for item in values or []:
        key, sep, raw = item.partition("=")
        if not sep:
            raise SystemExit(f"[leadlag] bad --fee-bps {item!r}: expected VENUE=BPS")
        fees[key.strip().upper()] = float(raw)
    return fees


def main() -> None:
    parser = argparse.ArgumentParser(description="Lead-lag of the reference stock vs perps")
    parser.add_argument("csv", type=Path, help="a <stem>_ref.csv written by spread_watch.py")
    parser.add_argument("--rth-only", action="store_true",
                        help="keep only US regular hours rows (13:30-20:00 UTC)")
    parser.add_argument("--threshold-bps", type=float, default=2.0,
                        help="edge above which an opportunity window is open")
    parser.add_argument("--hold-secs", type=float, default=5.0,
                        help="how long the naive follower holds before unwinding at mid")
    parser.add_argument("--fee-bps", action="append", default=None,
                        help="override a leg's taker fee, e.g. --fee-bps HL=0.9 "
                             "(repeatable; the CSV does not record fees)")
    parser.add_argument("--out", type=Path, default=None, help="also write markdown here")
    args = parser.parse_args()

    if not args.csv.exists():
        raise SystemExit(f"[leadlag] no such file: {args.csv}")
    frame, venues = load_ref_csv(args.csv, rth_only=args.rth_only)
    if frame.empty:
        raise SystemExit(f"[leadlag] {args.csv}: no rows left after filtering")
    fees = parse_fees(args.fee_bps, venues)
    report = build_report(args.csv, frame, venues, args.threshold_bps,
                          args.hold_secs, fees, args.rth_only)
    print(report)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report + "\n", encoding="utf-8")
        print(f"[leadlag] wrote {args.out}")


if __name__ == "__main__":
    main()

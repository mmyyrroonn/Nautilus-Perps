#!/usr/bin/env python3
"""aster_msq.py - Aster-focused millisecond analysis of the 2026-09-09 US RTH run.

Scratch analysis script (not part of src/). One symbol per invocation so the
3.4 GB box never holds two of the ~900 MB ``_ref.csv`` files at once.

Answers, per symbol, RTH only, with the Futu outage windows and the reference
print artefacts removed:

  a. basis of every leg vs the Futu reference mid, by RTH hour
  b. reverse-basis windows on ASTER (and HL): the quote sits on the wrong side of
     the basis-corrected fair by more than theta
  c. naive follower P&L on those windows, two exit rules, two fee assumptions,
     plus a notional-capped USD version using the 2 bps depth bucket
  d. cross-correlation of 100 ms returns, perp vs reference, -2 s .. +2 s

Usage:
    python aster_msq.py SYM --ref <ref.csv> --depth <depth.csv> --out-dir /tmp/ll
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import pandas as pd

DAY = "2026-09-09"
# all timestamps are naive UTC (tz dropped at load) so numpy comparisons work
RTH_START = pd.Timestamp(f"{DAY} 13:30:00")
RTH_END = pd.Timestamp(f"{DAY} 20:00:00")
VENUES = ["ASTER", "HL", "LIGHTER", "LIGHTER_RH"]
FEE_BPS = {"ASTER": 0.9, "HL": 0.9, "LIGHTER": 0.0, "LIGHTER_RH": 0.0}
THETAS = (1.0, 2.0, 3.0, 5.0)
NOTIONAL_CAP_USD = 10_000.0

# Futu websocket drops, read from logs/pm2-stocks-ref.out.log
# ("[ref/futu] connection ... lost" -> "[ref/futu] subscribed ...").
FUTU_OUTAGES = [
    ("13:33:45.911", "13:33:48.020"),
    ("13:39:31.721", "13:39:33.700"),
    ("13:42:58.112", "13:42:59.640"),
    ("13:46:43.431", "13:46:45.571"),
    ("13:58:56.051", "13:58:58.101"),
    ("14:52:23.630", "14:52:25.591"),
    ("15:00:26.751", "15:00:28.710"),
]
OUTAGE_GRACE_MS = 1_000      # keep dropping this long after the re-subscribe line
REF_JUMP_BPS = 50.0          # reference print artefact: >50 bps move inside 1 s
REF_JUMP_GUARD_MS = 1_000    # drop this much either side of an artefact print
REF_STALE_MS = 1_000         # reference older than this -> not a usable quote


# ------------------------------------------------------------------ loading


def load_ref(path: Path) -> pd.DataFrame:
    cols = ["ts_utc", "ref_mid", "ref_age_ms"]
    for v in VENUES:
        cols += [f"{v}_bid", f"{v}_ask", f"{v}_age_ms"]
    frame = pd.read_csv(path, usecols=cols, engine="c")
    frame["ts"] = pd.to_datetime(frame["ts_utc"], utc=True, format="mixed").dt.tz_localize(None)
    frame = frame.drop(columns=["ts_utc"])
    frame = frame.sort_values("ts", kind="stable").reset_index(drop=True)
    return frame


def rth_slice(frame: pd.DataFrame) -> pd.DataFrame:
    mask = (frame["ts"] >= RTH_START) & (frame["ts"] < RTH_END)
    return frame.loc[mask].reset_index(drop=True)


def outage_intervals() -> list[tuple[pd.Timestamp, pd.Timestamp]]:
    out = []
    for lost, back in FUTU_OUTAGES:
        a = pd.Timestamp(f"{DAY}T{lost}")
        b = pd.Timestamp(f"{DAY}T{back}") + pd.Timedelta(milliseconds=OUTAGE_GRACE_MS)
        out.append((a, b))
    return out


def build_mask(frame: pd.DataFrame) -> dict:
    """Rows to drop: Futu outage, stale reference, reference print artefact."""
    ts = frame["ts"].to_numpy()
    n = len(frame)

    drop_outage = np.zeros(n, dtype=bool)
    for a, b in outage_intervals():
        drop_outage |= (ts >= np.datetime64(a)) & (ts <= np.datetime64(b))

    drop_stale = frame["ref_age_ms"].to_numpy() > REF_STALE_MS

    # Artefact: the reference mid moves > REF_JUMP_BPS between two consecutive
    # *distinct* reference prints less than a second apart.
    ref = frame["ref_mid"].to_numpy(dtype=float)
    changed = np.flatnonzero(np.r_[True, ref[1:] != ref[:-1]])
    rv = ref[changed]
    rt = ts[changed]
    with np.errstate(invalid="ignore", divide="ignore"):
        step_bps = np.abs(np.r_[0.0, np.diff(rv) / rv[:-1]]) * 1e4
    dt_s = np.r_[np.inf, (rt[1:] - rt[:-1]) / np.timedelta64(1, "s")]
    bad = (step_bps > REF_JUMP_BPS) & (dt_s <= 1.0)
    drop_jump = np.zeros(n, dtype=bool)
    guard = np.timedelta64(REF_JUMP_GUARD_MS, "ms")
    for t in rt[bad]:
        drop_jump |= (ts >= t - guard) & (ts <= t + guard)

    keep = ~(drop_outage | drop_stale | drop_jump)
    return {
        "keep": keep,
        "n_rows": int(n),
        "n_drop_outage": int(drop_outage.sum()),
        "n_drop_stale": int(drop_stale.sum()),
        "n_drop_jump": int(drop_jump.sum()),
        "n_jump_prints": int(bad.sum()),
        "jump_times": [str(pd.Timestamp(t)) for t in rt[bad][:20]],
        "n_keep": int(keep.sum()),
    }


# ------------------------------------------------------------------ 3a basis


def hour_buckets(ts: pd.Series) -> pd.Series:
    return ts.dt.floor("h")


def basis_table(frame: pd.DataFrame) -> pd.DataFrame:
    """Per-leg basis vs reference in bps, on a 1 s grid, by RTH hour."""
    idx = pd.DatetimeIndex(frame["ts"])
    ref = pd.Series(frame["ref_mid"].to_numpy(dtype=float), index=idx)
    ref = ref[~ref.index.duplicated(keep="last")].resample("1s").last()
    rows = []
    for v in VENUES:
        mid = (frame[f"{v}_bid"].to_numpy(dtype=float)
               + frame[f"{v}_ask"].to_numpy(dtype=float)) / 2.0
        s = pd.Series(mid, index=idx)
        s = s[~s.index.duplicated(keep="last")].resample("1s").last()
        joined = pd.concat({"ref": ref, "perp": s}, axis=1).dropna()
        if joined.empty:
            continue
        bps = (joined["perp"] / joined["ref"] - 1.0) * 1e4
        for label, sel in bucket_selectors(bps.index):
            part = bps[sel]
            if len(part) < 30:
                continue
            rows.append({
                "leg": v, "bucket": label, "n": int(len(part)),
                "median_bps": float(part.median()), "sd_bps": float(part.std()),
            })
    return pd.DataFrame(rows)


def bucket_selectors(index: pd.DatetimeIndex):
    out = [("13:30-14:00 (open 30m)",
            (index >= RTH_START) & (index < RTH_START + pd.Timedelta(minutes=30))),
           ("13:30-13:35 (open 5m)",
            (index >= RTH_START) & (index < RTH_START + pd.Timedelta(minutes=5)))]
    for h in range(13, 20):
        lo = pd.Timestamp(f"{DAY} {h:02d}:00:00")
        hi = lo + pd.Timedelta(hours=1)
        out.append((f"{h:02d}h", (index >= max(lo, RTH_START)) & (index < min(hi, RTH_END))))
    return out


# ------------------------------------------------- 3b reverse-basis windows


def rolling_fair(frame: pd.DataFrame, venue: str) -> np.ndarray:
    """fair = ref_mid * (1 + basis); basis = rolling 5 min median of perp/ref - 1."""
    idx = pd.DatetimeIndex(frame["ts"])
    ref = pd.Series(frame["ref_mid"].to_numpy(dtype=float), index=idx)
    mid = pd.Series((frame[f"{venue}_bid"].to_numpy(dtype=float)
                     + frame[f"{venue}_ask"].to_numpy(dtype=float)) / 2.0, index=idx)
    r1 = ref[~ref.index.duplicated(keep="last")].resample("1s").last().ffill()
    m1 = mid[~mid.index.duplicated(keep="last")].resample("1s").last().ffill()
    basis1 = (m1 / r1 - 1.0)
    basis1 = basis1.rolling("300s", min_periods=60).median()
    aligned = basis1.reindex(idx, method="ffill").to_numpy(dtype=float)
    return frame["ref_mid"].to_numpy(dtype=float) * (1.0 + aligned), aligned


def find_windows(ts: np.ndarray, flag: np.ndarray, max_gap_s: float = 2.0):
    """(start_idx, last_idx, end_ts) for each contiguous run of ``flag``."""
    idx = np.flatnonzero(flag)
    if idx.size == 0:
        return np.array([], dtype=int), np.array([], dtype=int), np.array([], dtype="datetime64[ns]")
    gaps = (ts[idx[1:]] - ts[idx[:-1]]) / np.timedelta64(1, "s")
    brk = np.flatnonzero((np.diff(idx) != 1) | (gaps > max_gap_s))
    starts = np.r_[idx[0], idx[brk + 1]]
    lasts = np.r_[idx[brk], idx[-1]]
    nxt = np.minimum(lasts + 1, len(ts) - 1)
    end_ts = ts[nxt]
    # a run that ends at the very last row has no "first row back below"
    end_ts = np.where(nxt > lasts, end_ts, ts[lasts])
    return starts, lasts, end_ts


def depth_lookup(depth: pd.DataFrame, venue: str, when: np.ndarray, side: str) -> np.ndarray:
    """USD resting inside 2 bps of the touch at the window start (1 s depth csv)."""
    col = "ask_usd_2bps" if side == "buy" else "bid_usd_2bps"
    d = depth[depth["venue"] == venue][["ts", col]].dropna().sort_values("ts")
    if d.empty or len(when) == 0:
        return np.full(len(when), np.nan)
    left = pd.DataFrame({"ts": pd.to_datetime(when)}).sort_values("ts")
    merged = pd.merge_asof(left, d, on="ts", direction="backward",
                           tolerance=pd.Timedelta(seconds=5))
    return merged[col].to_numpy(dtype=float)


def analyse_venue(frame: pd.DataFrame, depth: pd.DataFrame, venue: str,
                  span_hours: float) -> dict:
    ts = frame["ts"].to_numpy()
    bid = frame[f"{venue}_bid"].to_numpy(dtype=float)
    ask = frame[f"{venue}_ask"].to_numpy(dtype=float)
    mid = (bid + ask) / 2.0
    fair, basis = rolling_fair(frame, venue)
    ok = np.isfinite(fair) & np.isfinite(mid) & (fair > 0)

    out = {"venue": venue, "span_hours": span_hours, "windows": [], "pnl": []}
    for theta in THETAS:
        th = theta / 1e4
        for side in ("buy", "sell"):
            if side == "buy":
                flag = ok & (ask < fair * (1.0 - th))
            else:
                flag = ok & (bid > fair * (1.0 + th))
            starts, lasts, end_ts = find_windows(ts, flag)
            dur_ms = (end_ts - ts[starts]) / np.timedelta64(1, "ms")
            size = depth_lookup(depth, venue, ts[starts], side)
            rec = {
                "theta_bps": theta, "side": side, "count": int(len(starts)),
                "per_hour": float(len(starts) / span_hours) if span_hours else float("nan"),
                "dur_p50_ms": float(np.quantile(dur_ms, 0.50)) if len(starts) else float("nan"),
                "dur_p90_ms": float(np.quantile(dur_ms, 0.90)) if len(starts) else float("nan"),
                "dur_max_ms": float(dur_ms.max()) if len(starts) else float("nan"),
                "touch_usd_p50": float(np.nanmedian(size)) if len(starts) and np.isfinite(size).any() else float("nan"),
                "touch_usd_p10": float(np.nanquantile(size, 0.10)) if len(starts) and np.isfinite(size).any() else float("nan"),
                "seconds_in_window": float(dur_ms.sum() / 1000.0) if len(starts) else 0.0,
            }
            out["windows"].append(rec)
            if len(starts):
                out["pnl"].extend(simulate(ts, bid, ask, mid, fair, starts, size,
                                           side, theta, venue))
    return out


def simulate(ts, bid, ask, mid, fair, starts, size, side, theta, venue) -> list[dict]:
    """Two exit rules on the same entries; fees applied afterwards."""
    n = len(ts)
    entry = ask[starts] if side == "buy" else bid[starts]
    sign = 1.0 if side == "buy" else -1.0

    # rule (i): fixed 5 s hold, exit at the leg mid
    j5 = np.searchsorted(ts, ts[starts] + np.timedelta64(5, "s"), side="left")
    ok5 = j5 < n
    gross5 = np.full(len(starts), np.nan)
    gross5[ok5] = sign * (mid[j5[ok5]] - entry[ok5]) / entry[ok5] * 1e4

    # rule (ii): exit when the mid comes back within theta/2 of fair, else 30 s;
    # exit crosses the touch (sell into the bid / buy from the ask).
    half = theta / 2e4
    j30 = np.searchsorted(ts, ts[starts] + np.timedelta64(30, "s"), side="left")
    gross2 = np.full(len(starts), np.nan)
    hold_ms = np.full(len(starts), np.nan)
    for k, i0 in enumerate(starts):
        hi = min(int(j30[k]), n - 1)
        if hi <= i0:
            continue
        seg = slice(i0 + 1, hi + 1)
        f = fair[seg]
        m = mid[seg]
        if side == "buy":
            hit = np.flatnonzero(np.isfinite(f) & (m >= f * (1.0 - half)))
            px = bid
        else:
            hit = np.flatnonzero(np.isfinite(f) & (m <= f * (1.0 + half)))
            px = ask
        j = (i0 + 1 + hit[0]) if hit.size else hi
        if not np.isfinite(px[j]) or entry[k] <= 0:
            continue
        gross2[k] = sign * (px[j] - entry[k]) / entry[k] * 1e4
        hold_ms[k] = (ts[j] - ts[i0]) / np.timedelta64(1, "ms")

    rows = []
    notional = np.clip(np.nan_to_num(size, nan=0.0), 0.0, NOTIONAL_CAP_USD)
    for rule, gross, hold in (("5s_mid", gross5, None), ("revert_30s_touch", gross2, hold_ms)):
        good = np.isfinite(gross)
        for fee in (1.8, 0.0):
            net = gross[good] - fee
            w = notional[good]
            rows.append({
                "venue": venue, "theta_bps": theta, "side": side, "rule": rule,
                "fee_bps_round_trip": fee, "trades": int(good.sum()),
                "win_rate": float((net > 0).mean()) if good.sum() else float("nan"),
                "mean_bps": float(net.mean()) if good.sum() else float("nan"),
                "total_bps": float(net.sum()) if good.sum() else 0.0,
                "notional_usd": float(w.sum()),
                "pnl_usd": float((net * w / 1e4).sum()),
                "hold_p50_ms": float(np.nanmedian(hold[good])) if hold is not None and good.sum() else None,
            })
    return rows


# ------------------------------------------------------------------ 3d xcorr


def xcorr(frame: pd.DataFrame, venue: str, resample_ms: int = 100,
          max_lag_ms: int = 2_000) -> dict:
    idx = pd.DatetimeIndex(frame["ts"])
    ref = pd.Series(frame["ref_mid"].to_numpy(dtype=float), index=idx)
    mid = pd.Series((frame[f"{venue}_bid"].to_numpy(dtype=float)
                     + frame[f"{venue}_ask"].to_numpy(dtype=float)) / 2.0, index=idx)
    rule = f"{resample_ms}ms"
    a = ref[~ref.index.duplicated(keep="last")].resample(rule).last().ffill()
    b = mid[~mid.index.duplicated(keep="last")].resample(rule).last().ffill()
    joined = pd.concat({"ref": a, "perp": b}, axis=1).dropna()
    if len(joined) < 100:
        return {"venue": venue, "best_lag_ms": None, "best_corr": None, "corr_at_0": None}
    rets = np.log(joined).diff().dropna()
    steps = max_lag_ms // resample_ms
    lags, corrs = [], []
    r = rets["ref"]
    p = rets["perp"]
    for k in range(-steps, steps + 1):
        pair = pd.concat({"a": r, "b": p.shift(-k)}, axis=1).dropna()
        c = float(pair["a"].corr(pair["b"])) if len(pair) > 100 else float("nan")
        lags.append(k * resample_ms)
        corrs.append(c)
    arr = np.array(corrs)
    best = int(np.nanargmax(arr))
    return {
        "venue": venue, "best_lag_ms": lags[best], "best_corr": float(arr[best]),
        "corr_at_0": float(arr[lags.index(0)]),
        "curve": {str(l): (None if not np.isfinite(c) else round(float(c), 4))
                  for l, c in zip(lags, corrs)},
    }


# ------------------------------------------------------------------ main


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("symbol")
    ap.add_argument("--ref", type=Path, required=True)
    ap.add_argument("--depth", type=Path, required=True)
    ap.add_argument("--out-dir", type=Path, default=Path("/tmp/ll"))
    args = ap.parse_args()
    args.out_dir.mkdir(parents=True, exist_ok=True)

    print(f"[{args.symbol}] loading {args.ref}", flush=True)
    frame = rth_slice(load_ref(args.ref))
    info = build_mask(frame)
    frame = frame.loc[info.pop("keep")].reset_index(drop=True)
    print(f"[{args.symbol}] rows kept {info['n_keep']} / {info['n_rows']}", flush=True)

    span_s = float((frame["ts"].iloc[-1] - frame["ts"].iloc[0]).total_seconds())
    gaps = frame["ts"].diff().dt.total_seconds().to_numpy()[1:]
    covered_s = float(np.nansum(np.clip(gaps, 0, 2.0)))
    span_hours = covered_s / 3600.0

    depth = pd.read_csv(args.depth, usecols=["ts_utc", "venue", "bid_usd_2bps", "ask_usd_2bps"])
    depth["ts"] = pd.to_datetime(depth["ts_utc"], utc=True, format="mixed").dt.tz_localize(None)
    depth = depth.drop(columns=["ts_utc"])

    result = {
        "symbol": args.symbol,
        "ref_csv": str(args.ref),
        "filter": info,
        "rth_span_seconds": span_s,
        "covered_seconds": covered_s,
        "span_hours": span_hours,
        "first_ts": str(frame["ts"].iloc[0]),
        "last_ts": str(frame["ts"].iloc[-1]),
    }

    print(f"[{args.symbol}] 3a basis", flush=True)
    result["basis"] = basis_table(frame).to_dict("records")

    print(f"[{args.symbol}] 3d xcorr", flush=True)
    result["xcorr"] = [xcorr(frame, v) for v in VENUES]

    for venue in ("ASTER", "HL"):
        print(f"[{args.symbol}] 3b/3c {venue}", flush=True)
        result[f"rev_{venue}"] = analyse_venue(frame, depth, venue, span_hours)

    out = args.out_dir / f"aster_ms_{args.symbol}.json"
    out.write_text(json.dumps(result, indent=1), encoding="utf-8")
    print(f"[{args.symbol}] wrote {out}", flush=True)
    print(f"DONE_{args.symbol}", flush=True)


if __name__ == "__main__":
    main()

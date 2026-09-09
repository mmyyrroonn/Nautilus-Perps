"""Join the Futu reference (from the stocks-ref run) with the 1 s depth samples of the
parallel stocks run, to get Aster's (and, as a cross-check, HL's) edge vs the stock at
1 s resolution. Usage: aster_vs_ref.py <ref.csv> <depth.csv> <out.md>"""
import sys
import numpy as np
import pandas as pd

ref_path, depth_path, out_path = sys.argv[1:4]
FEES = {"ASTER": 0.9, "HL": 0.9, "LIGHTER": 0.0, "LIGHTER_RH": 0.0}

ref = pd.read_csv(ref_path, usecols=["ts_utc", "ref_mid", "ref_book_mode"])
ref = ref[ref.ref_book_mode != "last"].dropna(subset=["ref_mid"])
ref["ts"] = pd.to_datetime(ref.ts_utc, utc=True).dt.floor("1s")
ref_s = ref.groupby("ts").ref_mid.last()
ref_s = ref_s[(ref_s.index >= "2026-09-08T13:30:00Z") & (ref_s.index < "2026-09-08T20:00:00Z")]

depth = pd.read_csv(depth_path, usecols=["ts_utc", "venue", "bid", "ask", "bid_usd_2bps", "ask_usd_2bps"])
depth["ts"] = pd.to_datetime(depth.ts_utc, utc=True).dt.floor("1s")

lines = [f"# Aster vs Futu reference at 1 s: {ref_path.split('/')[-1]}", "",
         f"- reference seconds: {len(ref_s)}  span {ref_s.index.min()} .. {ref_s.index.max()}", "",
         "| venue | secs | buy p50 | buy p90 | buy p99 | buy >0 | buy >2 | buy >5 | sell p50 | sell p90 | sell p99 | sell >0 | sell >2 | sell >5 | ask usd@2bps p50 | bid usd@2bps p50 |",
         "| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |"]
for venue in ["ASTER", "HL", "LIGHTER", "LIGHTER_RH"]:
    d = depth[depth.venue == venue].groupby("ts").last()
    j = d.join(ref_s, how="inner").dropna(subset=["bid", "ask", "ref_mid"])
    if j.empty:
        lines.append(f"| {venue} | 0 | | | | | | | | | | | | | | |")
        continue
    fee = FEES[venue]
    buy = (j.ref_mid - j.ask) / j.ask * 1e4 - fee
    sell = (j.bid - j.ref_mid) / j.bid * 1e4 - fee
    q = lambda s, p: f"{np.percentile(s, p):.2f}"
    pct = lambda s, t: f"{(s > t).mean() * 100:.1f}%"
    lines.append(
        f"| {venue} | {len(j)} | {q(buy,50)} | {q(buy,90)} | {q(buy,99)} | {pct(buy,0)} | {pct(buy,2)} | {pct(buy,5)} "
        f"| {q(sell,50)} | {q(sell,90)} | {q(sell,99)} | {pct(sell,0)} | {pct(sell,2)} | {pct(sell,5)} "
        f"| {j.ask_usd_2bps.median():.0f} | {j.bid_usd_2bps.median():.0f} |")
    # hourly basis drift: mid offset perp vs stock
    off = ((j.bid + j.ask) / 2 - j.ref_mid) / j.ref_mid * 1e4
    hourly = off.groupby(off.index.floor("1h")).median()
    lines.append(f"|  {venue} hourly mid offset bps | " + " ".join(f"{t.strftime('%H')}h={v:.1f}" for t, v in hourly.items()) + " |" + " |" * 14)
open(out_path, "w").write("\n".join(lines) + "\n")
print("\n".join(lines))

"""Event-level round trip on the hit CSV (every update where B_sell_A_buy was 'net+').
entry at event t: gross_in = (bid_B - ask_A)/mid ; exit at first event >= t+H: gross_out = (bid_A - ask_B)/mid.
Also: basis-adjusted signal = gap - rolling mean(gap over last W seconds)."""
import csv, sys, bisect, statistics as st
from datetime import datetime
f, FEE_RT = sys.argv[1], float(sys.argv[2])
rows = [r for r in csv.DictReader(open(f)) if r['direction']=='B_sell_A_buy']
T=[datetime.fromisoformat(r['ts_utc']).timestamp() for r in rows]
def gi(r): 
    bA,aA,bB,aB=map(float,(r['bid_A'],r['ask_A'],r['bid_B'],r['ask_B'])); mid=((bA+aA)/2+(bB+aB)/2)/2
    return (bB-aA)/mid*1e4, (bA-aB)/mid*1e4
G=[gi(r) for r in rows]
print(f.split('/')[-1], f"events={len(rows)} span={T[-1]-T[0]:.0f}s fee_rt={FEE_RT}")
for H in (0.5,1,2,5,10,30):
    forced=[];best=[]
    for i,t in enumerate(T):
        j=bisect.bisect_left(T,t+H)
        if j>=len(T): break
        forced.append(G[i][0]+G[j][1]-FEE_RT)
        best.append(G[i][0]+max(g[1] for g in G[i+1:j+1])-FEE_RT)
    if not forced: continue
    fs=sorted(forced); bs=sorted(best)
    print(f"  H={H:>4}s forced: med={st.median(fs):6.2f} p90={fs[int(.9*len(fs))]:6.2f} max={fs[-1]:6.2f} pos={100*sum(x>0 for x in fs)/len(fs):4.1f}% | best: med={st.median(bs):6.2f} max={bs[-1]:6.2f} pos={100*sum(x>0 for x in bs)/len(bs):4.1f}%")
# basis-adjusted: deviation of entry gap from trailing 60s mean
W=60; dev=[]
for i,t in enumerate(T):
    k=bisect.bisect_left(T,t-W)
    if i-k<10: continue
    m=st.mean(g[0] for g in G[k:i]); dev.append(G[i][0]-m)
ds=sorted(dev)
print(f"  gap deviation from trailing {W}s mean: n={len(ds)} std={st.pstdev(ds):.2f} p95={ds[int(.95*len(ds))]:.2f} p99={ds[int(.99*len(ds))]:.2f} max={ds[-1]:.2f}  (need > {FEE_RT} to pay round trip)")

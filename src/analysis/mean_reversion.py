"""Conditional mean-reversion backtest on event-level rows (both directions).
Signal: dev = gap_dir(t) - trailing-W-second mean of gap_dir. Enter dir when dev > theta (and not in position).
Exit: first later event where round-trip pnl (entry gross + exit gross - fee_rt) >= target, else forced at H.
One position at a time. Reports per theta: trades, pos%, median/sum pnl (bps)."""
import csv, sys, bisect, statistics as st
from datetime import datetime
f, FEE_RT = sys.argv[1], float(sys.argv[2]); H=30.0; W=60.0; TARGET=0.0
by = {'A_sell_B_buy':[], 'B_sell_A_buy':[]}
for r in csv.DictReader(open(f)):
    bA,aA,bB,aB=map(float,(r['bid_A'],r['ask_A'],r['bid_B'],r['ask_B'])); mid=((bA+aA)/2+(bB+aB)/2)/2
    t=datetime.fromisoformat(r['ts_utc']).timestamp()
    by[r['direction']].append((t,(bA-aB)/mid*1e4,(bB-aA)/mid*1e4))  # t, grossAB, grossBA
print(f.split('/')[-1], f"fee_rt={FEE_RT} H={H}s W={W}s")
for theta in (1.0,1.5,2.0,2.5,3.0,4.0):
    tot=[]; 
    for d,idx_in,idx_out in (('A_sell_B_buy',1,2),('B_sell_A_buy',2,1)):
        ev=by[d]; T=[e[0] for e in ev]; i=0; pnl=[]
        while i<len(ev):
            t=T[i]; k=bisect.bisect_left(T,t-W)
            if i-k<10: i+=1; continue
            m=st.mean(e[idx_in] for e in ev[k:i]); dev=ev[i][idx_in]-m
            if dev<=theta: i+=1; continue
            entry=ev[i][idx_in]; j=i+1; done=None
            while j<len(ev) and T[j]<=t+H:
                p=entry+ev[j][idx_out]-FEE_RT
                if p>=TARGET: done=p; break
                j+=1
            if done is None:
                if j>=len(ev): break
                done=entry+ev[j][idx_out]-FEE_RT
            pnl.append(done); i=j+1
        tot+=pnl
    if tot:
        s=sorted(tot); print(f"  theta={theta:3.1f}: trades={len(s):4d} pos={100*sum(x>0 for x in s)/len(s):5.1f}% median={st.median(s):6.2f} mean={st.mean(s):6.2f} sum={sum(s):8.1f} worst={s[0]:6.2f}")
    else: print(f"  theta={theta:3.1f}: no triggers")

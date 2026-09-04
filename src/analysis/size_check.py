import csv, sys, bisect, statistics as st
from datetime import datetime, timezone
f, FEE_RT, THETA = sys.argv[1], float(sys.argv[2]), float(sys.argv[3]); H=30.0; W=60.0
by={'A_sell_B_buy':[], 'B_sell_A_buy':[]}
for r in csv.DictReader(open(f)):
    v={k:float(r[k]) for k in ('bid_A','ask_A','bid_size_A','ask_size_A','bid_B','ask_B','bid_size_B','ask_size_B')}
    mid=((v['bid_A']+v['ask_A'])/2+(v['bid_B']+v['ask_B'])/2)/2
    v['t']=datetime.fromisoformat(r['ts_utc']).timestamp(); v['mid']=mid
    v['gAB']=(v['bid_A']-v['ask_B'])/mid*1e4; v['gBA']=(v['bid_B']-v['ask_A'])/mid*1e4
    by[r['direction']].append(v)
# tick sizes from price decimals: HL xyz TSLA (5 sig fig -> 0.01 at ~368), Lighter TSLA price decimals 2 -> 0.01
TICK=0.01
print(f"{f.split('/')[-1]} theta={THETA} fee_rt={FEE_RT}")
hdr=f"{'entry(UTC)':>12} {'dir':>12} {'gap_in':>6} {'hold_s':>6} {'pnl_bps':>7} | {'entA':>7} {'entB':>7} {'cap_sh':>6} {'cap_USD':>8} | {'exitA':>7} {'exitB':>7} {'exit_ok':>7} {'short%':>6} {'pnl_adj':>7}"
VERBOSE=len(sys.argv)>4
if VERBOSE: print(hdr)
tot=[];tot_adj=[];caps=[];short=0
for d,gin,gout,inA,inB,outA,outB in (
    ('A_sell_B_buy','gAB','gBA','bid_size_A','ask_size_B','ask_size_A','bid_size_B'),
    ('B_sell_A_buy','gBA','gAB','ask_size_A','bid_size_B','bid_size_A','ask_size_B')):
    ev=by[d]; T=[e['t'] for e in ev]; i=0
    P=[0.0]
    for e in ev: P.append(P[-1]+e[gin])
    while i<len(ev):
        t=T[i]; k=bisect.bisect_left(T,t-W)
        if i-k<10: i+=1; continue
        m=(P[i]-P[k])/(i-k); dev=ev[i][gin]-m
        if dev<=THETA: i+=1; continue
        e=ev[i]; entry=e[gin]; j=i+1; done=None
        while j<len(ev) and T[j]<=t+H:
            p=entry+ev[j][gout]-FEE_RT
            if p>=0: done=p; break
            j+=1
        if done is None:
            if j>=len(ev): break
            done=entry+ev[j][gout]-FEE_RT
        x=ev[j]; cap=min(e[inA],e[inB]); capusd=cap*e['mid']
        exit_avail=min(x[outA],x[outB]); ok=exit_avail>=cap
        shortfall=max(0.0,cap-exit_avail)/cap
        # unfilled fraction walks >=1 tick on the short leg: penalty >= tick/mid bps on that fraction
        pnl_adj=done-shortfall*(TICK/x['mid']*1e4)
        if not ok: short+=1
        tot.append(done); tot_adj.append(pnl_adj); caps.append(capusd)
        if VERBOSE: print(f"{datetime.fromtimestamp(t,timezone.utc).strftime('%H:%M:%S.%f')[:-3]:>12} {d:>12} {entry:6.2f} {T[j]-t:6.1f} {done:7.2f} | {e[inA]:7.3f} {e[inB]:7.3f} {cap:6.3f} {capusd:8.0f} | {x[outA]:7.3f} {x[outB]:7.3f} {str(ok):>7} {100*shortfall:6.0f} {pnl_adj:7.2f}")
        i=j+1
n=len(tot)
print(f"\ntrades={n} exit-size-insufficient={short} ({100*short/max(n,1):.0f}%)")
print(f"capacity USD per trade: median={st.median(caps):.0f} min={min(caps):.0f} max={max(caps):.0f}")
print(f"pnl bps: sum={sum(tot):.2f} -> with >=1-tick penalty on unfilled exit fraction: sum={sum(tot_adj):.2f}")
print(f"USD per trade at capacity, median: {st.median([p*c/1e4 for p,c in zip(tot_adj,caps)]):.4f}; total USD over run at full capacity: {sum(p*c/1e4 for p,c in zip(tot_adj,caps)):.3f}")

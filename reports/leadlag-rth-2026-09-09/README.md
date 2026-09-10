# 2026-09-09 US RTH lead-lag, US-east box, four perp legs incl. Aster

Measurement only. Every number below is **computed** from the recording unless it is
marked **read** (taken from a log line, a config value or a previous report).
Definitions follow `reports/leadlag-rth-2026-09-08.md`:

- `buy_edge  = (stock mid - perp ask) / perp ask` minus one taker fee: the perp offer is
  cheaper than the stock, lift it long.
- `sell_edge = (perp bid - stock mid) / perp bid` minus one taker fee: the perp bid is
  richer than the stock, hit it short.
- **basis** = a persistent offset of the perp mid from the stock mid that lasts minutes to
  hours; **stale quote** = the perp touch sitting on the wrong side of the *basis-corrected*
  fair for a few hundred milliseconds. Only the second one is a lead-lag signal.

## 1. What was recorded (read)

| item | value |
| --- | --- |
| box | `vultr-us`, New Jersey (2 vCPU / 3.4 GB), root, repo at `~/Nautilus-Perps` |
| window | 2026-09-09 12:00:46 -> 20:05:00 UTC; analysis clipped to RTH 13:30-20:00 UTC |
| symbols | SPCX, MU, SNDK |
| perp legs | HL (xyz HIP-3), LIGHTER, LIGHTER_RH (Robinhood Chain), ASTER (USD1-margined, e.g. MUUSD1) |
| reference | Futu OPEN API WebSocket, `order_book` + `ticker`, Nasdaq + NYSE Arca |
| files | `reports/stage1/<SYM>_HL-LIGHTER-LIGHTER_RH-ASTER_20260909T120046Z_ref.csv` (831-911 MB each), plus `depth_`, `trades_`, `spread_` at 1 s |
| log | `logs/pm2-stocks-ref.out.log`, 6415 lines, 0 ERROR, 0 pm2 restarts (`pm2 jlist` `restart_time=0`); `pm2-stocks-ref.err.log` holds 3 ERROR lines, all at 20:05:02 shutdown (`Failed to send time event message: channel closed`) |

Row counts inside RTH (computed, from the `leadlag.py` headers):

| symbol | rows | reference update rows | perp quote rows |
| --- | ---: | ---: | ---: |
| SPCX | 2,327,092 | 1,101,949 | 1,225,143 |
| MU | 2,354,512 | 854,350 | 1,500,162 |
| SNDK | 2,410,332 | 633,513 | 1,776,819 |

Reference latency this run (computed, whole RTH window):

| symbol | exchange -> Futu server `ref_src_to_srv_ms` p50 / p90 / p99 | Futu -> us-east `ref_age_ms` p50 / p90 / p99 | book mode composite / freshest |
| --- | --- | --- | --- |
| SPCX | 57 / 499 / 1117 | 11.6 / 131.1 / 233.0 | 77.9% / 22.1% |
| MU | 58 / 475 / 1093 | 29.6 / 150.5 / 301.6 | 93.6% / 6.4% |
| SNDK | 55 / 392 / 1081 | 47.3 / 172.7 / 417.2 | 94.9% / 5.1% |

For comparison, the 09-08 Singapore run measured the exchange -> Futu hop at ~240 ms p50
(read, `reports/leadlag-rth-2026-09-08.md` section 3). Moving the box to New Jersey cut the
visible reference latency by roughly 4x.

## 2. Run quality: the Futu reference outages

Seven WebSocket closes, all `code=1008 reason='slow_consumer'`, all inside RTH (read, from
the `[ref/futu] connection ... lost` / `[ref/futu] subscribed ...` pairs):

| # | lost (UTC) | re-subscribed (UTC) | gap |
| ---: | --- | --- | ---: |
| 1 | 13:33:45.911 | 13:33:48.020 | 2.109 s |
| 2 | 13:39:31.721 | 13:39:33.700 | 1.979 s |
| 3 | 13:42:58.112 | 13:42:59.640 | 1.528 s |
| 4 | 13:46:43.431 | 13:46:45.571 | 2.140 s |
| 5 | 13:58:56.051 | 13:58:58.101 | 2.050 s |
| 6 | 14:52:23.630 | 14:52:25.591 | 1.961 s |
| 7 | 15:00:26.751 | 15:00:28.710 | 1.959 s |

**Total RTH downtime 13.726 s out of 23,400 s = 0.059%** (computed from the log stamps).
Five of the seven fall in the first 30 minutes of RTH, the other two around 14:52-15:00;
nothing after 15:00:28. Four non-Binance WARN lines cover the perp-side WebSocket
reconnects (12:51:58 peer close, 14:52:26 and 17:41:44 close frame code=1000, 19:20:45
connection reset); those are ordinary reconnects and do not blank the reference.

**`leadlag.py` does not exclude these windows.** It reads the whole csv, filters on nothing
but the RTH clock, and never looks at `ref_age_ms` or `ref_book_mode` (verified by reading
`src/analysis/leadlag.py`: the only filter is the `RTH_START`/`RTH_END` slice in
`load_ref_csv`). During an outage the watcher keeps writing one row per perp quote update
with the last reference price carried forward, so those rows show a growing `ref_age_ms` and
a frozen stock price - exactly the shape of a fake 'stale perp quote'. Affected share of the
RTH window: 0.059% by wall clock, 0.11%-0.16% by row count (computed, see the filter table
in section 3). The Aster analysis below drops them; the six `leadlag_*.md` files do not.

## 3. Aster-specific analysis

Script: `aster_msq.py` (also at `/tmp/ll/aster_msq.py` on the box). Rows removed before any
statistic: the seven Futu outage windows plus 1 s of grace after each re-subscribe;
`ref_age_ms > 1000`; and +/-1 s around every reference print that moved the mid more than
50 bps within one second.

| sym | rows in RTH | dropped: outage | dropped: stale ref | dropped: ref jump | artefact prints | rows kept | covered hours |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SPCX | 2,327,092 | 2,543 | 9,395 | 698 | 8 | 2,315,784 | 6.482 |
| MU | 2,354,512 | 3,793 | 12,622 | 1,182 | 8 | 2,338,544 | 6.482 |
| SNDK | 2,410,332 | 3,378 | 14,150 | 363 | 5 | 2,394,299 | 6.482 |

### 3a. Basis vs the Futu reference (bps, 1 s grid, median / sd)

Positive = the perp trades above the stock. `13h*` is 13:30-14:00 by construction (RTH
starts at 13:30), so it is the same population as the `open 30m` column; the `open 5m`
column is the extra detail.

**SPCX**

| leg | open 5m | open 30m | 13h* | 14h | 15h | 16h | 17h | 18h | 19h |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ASTER | -1.0 / 3.2 | 4.9 / 8.4 | 4.9 / 8.4 | 16.2 / 3.9 | 13.2 / 1.2 | 10.3 / 1.2 | 9.9 / 0.7 | 6.7 / 1.8 | 7.5 / 1.3 |
| HL | -4.0 / 8.2 | -2.3 / 5.4 | -2.3 / 5.4 | -0.3 / 2.6 | -2.4 / 1.7 | -3.8 / 1.4 | -2.7 / 1.4 | -4.0 / 1.4 | -2.7 / 1.9 |
| LIGHTER | -1.0 / 4.0 | 3.3 / 6.8 | 3.3 / 6.8 | 8.6 / 3.7 | 6.4 / 1.5 | 5.1 / 1.2 | 4.8 / 0.8 | 2.7 / 1.2 | 4.1 / 2.0 |
| LIGHTER_RH | -7.6 / 5.8 | -2.6 / 6.4 | -2.6 / 6.4 | 1.7 / 3.4 | -0.3 / 1.5 | -1.0 / 1.3 | 0.0 / 1.1 | -1.7 / 1.3 | -0.3 / 2.0 |

**MU**

| leg | open 5m | open 30m | 13h* | 14h | 15h | 16h | 17h | 18h | 19h |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ASTER | 14.6 / 2.6 | 9.2 / 3.2 | 9.2 / 3.2 | 8.2 / 1.8 | 7.9 / 1.5 | 10.1 / 1.4 | 10.5 / 1.4 | 8.3 / 1.3 | 7.1 / 1.0 |
| HL | 8.8 / 5.2 | 4.2 / 3.9 | 4.2 / 3.9 | 2.3 / 2.5 | 1.2 / 1.6 | 2.8 / 2.2 | 4.7 / 1.3 | 1.9 / 1.2 | 0.7 / 1.2 |
| LIGHTER | 16.3 / 2.6 | 10.2 / 3.7 | 10.2 / 3.7 | 9.8 / 1.9 | 8.4 / 1.4 | 10.3 / 1.3 | 10.5 / 1.3 | 8.8 / 1.1 | 7.5 / 1.0 |
| LIGHTER_RH | 10.6 / 3.0 | 7.2 / 2.5 | 7.2 / 2.5 | 6.7 / 1.5 | 6.0 / 1.3 | 7.3 / 1.0 | 6.9 / 1.2 | 5.3 / 1.0 | 4.4 / 0.9 |

**SNDK**

| leg | open 5m | open 30m | 13h* | 14h | 15h | 16h | 17h | 18h | 19h |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| ASTER | 3.3 / 3.1 | 5.4 / 3.3 | 5.4 / 3.3 | -2.5 / 2.1 | 1.2 / 3.1 | -0.4 / 2.8 | 0.4 / 2.0 | 3.1 / 1.9 | 3.5 / 2.7 |
| HL | -1.0 / 5.5 | -0.7 / 4.0 | -0.7 / 4.0 | -5.8 / 2.2 | -1.8 / 2.9 | -2.8 / 1.9 | -1.2 / 1.6 | -0.1 / 1.5 | -1.0 / 2.7 |
| LIGHTER | 7.3 / 3.6 | 9.2 / 3.5 | 9.2 / 3.5 | 3.0 / 2.2 | 7.8 / 3.2 | 5.4 / 2.7 | 6.2 / 1.8 | 8.9 / 1.9 | 8.3 / 2.6 |
| LIGHTER_RH | 1.0 / 3.7 | 2.4 / 3.1 | 2.4 / 3.1 | -3.5 / 1.7 | -0.1 / 2.6 | -1.3 / 2.3 | -0.6 / 1.5 | 1.5 / 1.7 | 0.9 / 2.2 |

### 3b. Windows against the basis-corrected fair

`basis` = rolling 5-minute median of `perp mid / ref mid - 1` on a 1 s grid, carried forward
onto every millisecond row; `fair = ref mid x (1 + basis)`. A **buy window** is
`perp ask < fair x (1 - theta)`, a **sell window** is `perp bid > fair x (1 + theta)`. Touch
size is the USD resting within 2 bps of the touch on the relevant side, taken from the 1 s
`depth_*.csv` as of the window start (the `_ref.csv` carries no size columns - verified
against `REF_HEAD` / `ref_header()` in `src/spread_watch.py`).

| sym | leg | theta bps | side | count | per RTH hour | dur p50 ms | p90 ms | max ms | touch USD p50 | touch USD p10 | secs in window |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SPCX | ASTER | 1 | buy | 4,648 | 717.1 | 106 | 1,002 | 58,705 | 45,527 | 11,020 | 2,324.4 |
| SPCX | ASTER | 1 | sell | 4,279 | 660.2 | 103 | 1,184 | 165,684 | 35,094 | 5,015 | 2,619.7 |
| SPCX | ASTER | 2 | buy | 2,011 | 310.3 | 89 | 938 | 37,492 | 48,822 | 9,235 | 933.3 |
| SPCX | ASTER | 2 | sell | 2,163 | 333.7 | 93 | 1,096 | 143,894 | 47,005 | 5,549 | 1,417.3 |
| SPCX | ASTER | 3 | buy | 1,035 | 159.7 | 84 | 846 | 23,658 | 53,273 | 8,999 | 437.1 |
| SPCX | ASTER | 3 | sell | 1,539 | 237.4 | 95 | 984 | 141,900 | 61,220 | 7,945 | 997.9 |
| SPCX | ASTER | 5 | buy | 345 | 53.2 | 80 | 616 | 7,695 | 78,241 | 10,443 | 87.5 |
| SPCX | ASTER | 5 | sell | 856 | 132.1 | 91 | 1,206 | 94,794 | 67,514 | 7,917 | 558.8 |
| SPCX | HL | 1 | buy | 4,435 | 684.2 | 445 | 1,852 | 53,640 | 140,537 | 48,564 | 3,929.6 |
| SPCX | HL | 1 | sell | 4,192 | 646.7 | 490 | 2,315 | 41,451 | 90,974 | 17,015 | 4,321.5 |
| SPCX | HL | 2 | buy | 2,415 | 372.6 | 360 | 1,207 | 28,548 | 145,872 | 41,646 | 1,531.8 |
| SPCX | HL | 2 | sell | 2,419 | 373.2 | 406 | 1,554 | 26,199 | 85,221 | 12,002 | 1,804.2 |
| SPCX | HL | 3 | buy | 1,266 | 195.3 | 326 | 998 | 12,801 | 145,013 | 35,173 | 671.9 |
| SPCX | HL | 3 | sell | 1,450 | 223.7 | 345 | 1,164 | 18,089 | 84,205 | 12,527 | 884.9 |
| SPCX | HL | 5 | buy | 448 | 69.1 | 282 | 823 | 4,105 | 135,091 | 17,542 | 182.5 |
| SPCX | HL | 5 | sell | 582 | 89.8 | 308 | 926 | 10,938 | 83,243 | 6,910 | 282.9 |
| MU | ASTER | 1 | buy | 5,611 | 865.7 | 53 | 275 | 5,454 | 28,998 | 3,986 | 735.9 |
| MU | ASTER | 1 | sell | 7,392 | 1,140.4 | 53 | 251 | 4,695 | 12,096 | 3,087 | 894.2 |
| MU | ASTER | 2 | buy | 2,114 | 326.1 | 45 | 185 | 1,840 | 16,875 | 3,147 | 186.0 |
| MU | ASTER | 2 | sell | 2,344 | 361.6 | 46 | 200 | 2,631 | 10,918 | 2,832 | 222.6 |
| MU | ASTER | 3 | buy | 760 | 117.3 | 43 | 142 | 1,065 | 14,049 | 2,898 | 55.2 |
| MU | ASTER | 3 | sell | 676 | 104.3 | 42 | 150 | 2,560 | 10,070 | 2,174 | 53.0 |
| MU | ASTER | 5 | buy | 111 | 17.1 | 38 | 89 | 244 | 20,241 | 2,674 | 4.9 |
| MU | ASTER | 5 | sell | 87 | 13.4 | 39 | 139 | 2,560 | 7,205 | 2,227 | 7.2 |
| MU | HL | 1 | buy | 3,673 | 566.7 | 353 | 1,333 | 20,920 | 179,137 | 55,666 | 2,432.3 |
| MU | HL | 1 | sell | 3,395 | 523.8 | 355 | 1,408 | 30,187 | 211,147 | 64,462 | 2,297.7 |
| MU | HL | 2 | buy | 1,767 | 272.6 | 302 | 1,000 | 20,920 | 259,705 | 45,530 | 911.6 |
| MU | HL | 2 | sell | 1,610 | 248.4 | 274 | 937 | 9,796 | 284,329 | 71,603 | 765.4 |
| MU | HL | 3 | buy | 994 | 153.4 | 298 | 915 | 8,698 | 294,109 | 34,104 | 465.3 |
| MU | HL | 3 | sell | 829 | 127.9 | 251 | 829 | 7,277 | 350,206 | 70,170 | 343.3 |
| MU | HL | 5 | buy | 368 | 56.8 | 250 | 794 | 2,750 | 245,552 | 20,745 | 129.6 |
| MU | HL | 5 | sell | 280 | 43.2 | 250 | 703 | 3,986 | 390,357 | 64,123 | 103.4 |
| SNDK | ASTER | 1 | buy | 6,805 | 1,049.9 | 76 | 656 | 26,516 | 54,720 | 2,489 | 2,059.7 |
| SNDK | ASTER | 1 | sell | 6,812 | 1,050.9 | 72 | 540 | 23,134 | 19,213 | 1,092 | 1,722.1 |
| SNDK | ASTER | 2 | buy | 3,538 | 545.8 | 59 | 496 | 15,294 | 53,680 | 1,097 | 829.5 |
| SNDK | ASTER | 2 | sell | 3,444 | 531.3 | 57 | 392 | 9,050 | 16,876 | 213 | 612.5 |
| SNDK | ASTER | 3 | buy | 1,585 | 244.5 | 50 | 295 | 4,497 | 51,243 | 1,149 | 219.6 |
| SNDK | ASTER | 3 | sell | 1,582 | 244.1 | 48 | 270 | 7,397 | 15,091 | 208 | 203.3 |
| SNDK | ASTER | 5 | buy | 202 | 31.2 | 44 | 122 | 651 | 43,034 | 3,133 | 12.7 |
| SNDK | ASTER | 5 | sell | 320 | 49.4 | 42 | 137 | 2,562 | 13,900 | 140 | 26.6 |
| SNDK | HL | 1 | buy | 4,918 | 758.7 | 380 | 1,804 | 32,797 | 186,104 | 49,525 | 4,097.6 |
| SNDK | HL | 1 | sell | 4,781 | 737.6 | 390 | 1,706 | 56,606 | 230,280 | 81,275 | 3,881.4 |
| SNDK | HL | 2 | buy | 2,876 | 443.7 | 328 | 1,134 | 20,321 | 185,138 | 49,127 | 1,748.7 |
| SNDK | HL | 2 | sell | 2,715 | 418.9 | 304 | 989 | 30,753 | 219,696 | 62,490 | 1,460.7 |
| SNDK | HL | 3 | buy | 1,647 | 254.1 | 259 | 784 | 14,604 | 180,516 | 47,730 | 682.1 |
| SNDK | HL | 3 | sell | 1,520 | 234.5 | 252 | 778 | 28,180 | 205,993 | 46,438 | 649.5 |
| SNDK | HL | 5 | buy | 474 | 73.1 | 241 | 658 | 3,386 | 170,746 | 42,397 | 151.6 |
| SNDK | HL | 5 | sell | 485 | 74.8 | 220 | 669 | 2,562 | 177,986 | 34,214 | 146.6 |

Aster's own quoted spread is wide compared with these thresholds (computed, RTH p10 / p50 /
p90 in bps): SPCX 0.67 / 1.32 / 2.73, MU 1.55 / 2.54 / 7.06, SNDK 1.98 / 2.22 / 3.84. HL for
comparison: SPCX 0.66 / 0.68 / 1.37, MU 0.97 / 0.98 / 1.95, SNDK 0.56 / 0.57 / 1.68. A
theta = 1 or 2 bps window on Aster is therefore inside the noise of its own quote width,
which is the main reason to read the theta = 3 and theta = 5 rows instead.

The multi-second `max ms` entries (SPCX Aster 59-166 s, HL 41-54 s) are not single stale
quotes: they are stretches where the basis itself moved faster than the 5-minute rolling
median could follow, so `fair` is mis-centred for a while. They are a small share of the
total time in window (`secs in window` column) but they inflate `max`.

### 3c. Naive follower on those windows

Entry always crosses the touch that opened the window (buy at the ask, sell at the bid),
one entry per window. Rule (i): hold 5 s, mark out at the leg mid. Rule (ii): exit when the
leg mid comes back within theta/2 of fair, capped at 30 s, exiting across the touch (sell
into the bid / buy from the ask). Fees are applied as a round trip: 1.8 bps (0.9 taker each
side, **read** from `DEFAULT_FEE_BPS` in `leadlag.py` and the project notes) or 0 bps (the
Aster campaign reward assumed to offset the fee). `P&L USD/day` uses a per-window notional
of `min(touch size, 10,000 USD)`; it is one RTH day.

Buy and sell aggregated:

| sym | leg | theta bps | rule | fee bps rt | trades | win rate | mean bps | total bps | notional USD | P&L USD/day |
| --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SPCX | ASTER | 1 | 5s_mid | 1.8 | 8,919 | 30.9% | -1.61 | -14,396 | 82,075,933 | -13,036 |
| SPCX | ASTER | 1 | 5s_mid | 0 | 8,919 | 55.3% | 0.19 | 1,658 | 82,075,933 | 1,738 |
| SPCX | ASTER | 1 | revert_30s_touch | 1.8 | 8,927 | 14.0% | -1.75 | -15,590 | 82,132,018 | -14,291 |
| SPCX | ASTER | 1 | revert_30s_touch | 0 | 8,927 | 31.7% | 0.05 | 479 | 82,132,018 | 493 |
| SPCX | ASTER | 2 | 5s_mid | 1.8 | 4,172 | 37.0% | -1.58 | -6,607 | 38,756,182 | -5,870 |
| SPCX | ASTER | 2 | 5s_mid | 0 | 4,172 | 55.9% | 0.22 | 903 | 38,756,182 | 1,107 |
| SPCX | ASTER | 2 | revert_30s_touch | 1.8 | 4,174 | 23.9% | -1.40 | -5,860 | 38,767,684 | -5,424 |
| SPCX | ASTER | 2 | revert_30s_touch | 0 | 4,174 | 45.3% | 0.40 | 1,654 | 38,767,684 | 1,554 |
| SPCX | ASTER | 3 | 5s_mid | 1.8 | 2,573 | 38.1% | -1.75 | -4,504 | 24,297,125 | -4,335 |
| SPCX | ASTER | 3 | 5s_mid | 0 | 2,573 | 53.8% | 0.05 | 127 | 24,297,125 | 38 |
| SPCX | ASTER | 3 | revert_30s_touch | 1.8 | 2,574 | 31.9% | -1.19 | -3,076 | 24,298,626 | -2,917 |
| SPCX | ASTER | 3 | revert_30s_touch | 0 | 2,574 | 49.9% | 0.61 | 1,558 | 24,298,626 | 1,457 |
| SPCX | ASTER | 5 | 5s_mid | 1.8 | 1,201 | 41.5% | -1.54 | -1,851 | 11,306,752 | -1,676 |
| SPCX | ASTER | 5 | 5s_mid | 0 | 1,201 | 53.0% | 0.26 | 311 | 11,306,752 | 360 |
| SPCX | ASTER | 5 | revert_30s_touch | 1.8 | 1,201 | 43.5% | -0.89 | -1,074 | 11,306,752 | -1,007 |
| SPCX | ASTER | 5 | revert_30s_touch | 0 | 1,201 | 56.1% | 0.91 | 1,087 | 11,306,752 | 1,028 |
| SPCX | HL | 1 | 5s_mid | 1.8 | 8,616 | 30.8% | -0.87 | -7,495 | 83,892,251 | -7,013 |
| SPCX | HL | 1 | 5s_mid | 0 | 8,616 | 60.9% | 0.93 | 8,014 | 83,892,251 | 8,087 |
| SPCX | HL | 1 | revert_30s_touch | 1.8 | 8,627 | 25.0% | -0.65 | -5,571 | 83,985,925 | -5,553 |
| SPCX | HL | 1 | revert_30s_touch | 0 | 8,627 | 53.3% | 1.15 | 9,958 | 83,985,925 | 9,564 |
| SPCX | HL | 2 | 5s_mid | 1.8 | 4,827 | 42.4% | -0.12 | -558 | 46,614,418 | -418 |
| SPCX | HL | 2 | 5s_mid | 0 | 4,827 | 68.9% | 1.68 | 8,131 | 46,614,418 | 7,973 |
| SPCX | HL | 2 | revert_30s_touch | 1.8 | 4,834 | 38.4% | 0.09 | 446 | 46,672,173 | 317 |
| SPCX | HL | 2 | revert_30s_touch | 0 | 4,834 | 70.6% | 1.89 | 9,148 | 46,672,173 | 8,718 |
| SPCX | HL | 3 | 5s_mid | 1.8 | 2,713 | 53.3% | 0.63 | 1,722 | 25,950,408 | 1,715 |
| SPCX | HL | 3 | 5s_mid | 0 | 2,713 | 73.2% | 2.43 | 6,606 | 25,950,408 | 6,386 |
| SPCX | HL | 3 | revert_30s_touch | 1.8 | 2,716 | 51.5% | 0.91 | 2,480 | 25,976,327 | 2,240 |
| SPCX | HL | 3 | revert_30s_touch | 0 | 2,716 | 78.7% | 2.71 | 7,369 | 25,976,327 | 6,916 |
| SPCX | HL | 5 | 5s_mid | 1.8 | 1,030 | 67.6% | 2.77 | 2,855 | 9,639,735 | 2,754 |
| SPCX | HL | 5 | 5s_mid | 0 | 1,030 | 77.7% | 4.57 | 4,709 | 9,639,735 | 4,490 |
| SPCX | HL | 5 | revert_30s_touch | 1.8 | 1,030 | 69.9% | 2.34 | 2,410 | 9,639,735 | 2,172 |
| SPCX | HL | 5 | revert_30s_touch | 0 | 1,030 | 85.4% | 4.14 | 4,264 | 9,639,735 | 3,907 |
| MU | ASTER | 1 | 5s_mid | 1.8 | 13,003 | 28.7% | -2.56 | -33,307 | 104,382,082 | -26,746 |
| MU | ASTER | 1 | 5s_mid | 0 | 13,003 | 43.2% | -0.76 | -9,901 | 104,382,082 | -7,957 |
| MU | ASTER | 1 | revert_30s_touch | 1.8 | 13,003 | 2.3% | -3.85 | -50,063 | 104,382,082 | -39,989 |
| MU | ASTER | 1 | revert_30s_touch | 0 | 13,003 | 8.7% | -2.05 | -26,657 | 104,382,082 | -21,200 |
| MU | ASTER | 2 | 5s_mid | 1.8 | 4,458 | 33.1% | -2.36 | -10,503 | 34,746,381 | -8,305 |
| MU | ASTER | 2 | 5s_mid | 0 | 4,458 | 45.2% | -0.56 | -2,478 | 34,746,381 | -2,051 |
| MU | ASTER | 2 | revert_30s_touch | 1.8 | 4,458 | 3.4% | -3.71 | -16,534 | 34,746,381 | -12,884 |
| MU | ASTER | 2 | revert_30s_touch | 0 | 4,458 | 12.0% | -1.91 | -8,509 | 34,746,381 | -6,630 |
| MU | ASTER | 3 | 5s_mid | 1.8 | 1,436 | 37.6% | -1.76 | -2,520 | 10,871,751 | -2,108 |
| MU | ASTER | 3 | 5s_mid | 0 | 1,436 | 49.1% | 0.04 | 65 | 10,871,751 | -151 |
| MU | ASTER | 3 | revert_30s_touch | 1.8 | 1,436 | 7.0% | -3.67 | -5,269 | 10,871,751 | -3,980 |
| MU | ASTER | 3 | revert_30s_touch | 0 | 1,436 | 15.9% | -1.87 | -2,684 | 10,871,751 | -2,023 |
| MU | ASTER | 5 | 5s_mid | 1.8 | 198 | 55.1% | 1.20 | 238 | 1,400,457 | 102 |
| MU | ASTER | 5 | 5s_mid | 0 | 198 | 62.6% | 3.00 | 594 | 1,400,457 | 354 |
| MU | ASTER | 5 | revert_30s_touch | 1.8 | 198 | 14.1% | -2.71 | -536 | 1,400,457 | -373 |
| MU | ASTER | 5 | revert_30s_touch | 0 | 198 | 23.2% | -0.91 | -180 | 1,400,457 | -120 |
| MU | HL | 1 | 5s_mid | 1.8 | 7,067 | 36.9% | -0.76 | -5,398 | 69,607,141 | -5,149 |
| MU | HL | 1 | 5s_mid | 0 | 7,067 | 58.9% | 1.04 | 7,322 | 69,607,141 | 7,380 |
| MU | HL | 1 | revert_30s_touch | 1.8 | 7,068 | 27.2% | -0.88 | -6,203 | 69,617,141 | -6,116 |
| MU | HL | 1 | revert_30s_touch | 0 | 7,068 | 49.1% | 0.92 | 6,519 | 69,617,141 | 6,415 |
| MU | HL | 2 | 5s_mid | 1.8 | 3,377 | 47.9% | 0.19 | 629 | 33,054,432 | 764 |
| MU | HL | 2 | 5s_mid | 0 | 3,377 | 65.1% | 1.99 | 6,708 | 33,054,432 | 6,714 |
| MU | HL | 2 | revert_30s_touch | 1.8 | 3,377 | 41.5% | -0.10 | -349 | 33,054,432 | -358 |
| MU | HL | 2 | revert_30s_touch | 0 | 3,377 | 65.9% | 1.70 | 5,729 | 33,054,432 | 5,592 |
| MU | HL | 3 | 5s_mid | 1.8 | 1,823 | 55.6% | 1.21 | 2,210 | 17,752,350 | 2,269 |
| MU | HL | 3 | 5s_mid | 0 | 1,823 | 69.1% | 3.01 | 5,492 | 17,752,350 | 5,465 |
| MU | HL | 3 | revert_30s_touch | 1.8 | 1,823 | 57.2% | 0.85 | 1,552 | 17,752,350 | 1,520 |
| MU | HL | 3 | revert_30s_touch | 0 | 1,823 | 75.4% | 2.65 | 4,834 | 17,752,350 | 4,715 |
| MU | HL | 5 | 5s_mid | 1.8 | 648 | 63.9% | 3.30 | 2,141 | 6,215,079 | 2,096 |
| MU | HL | 5 | 5s_mid | 0 | 648 | 75.2% | 5.10 | 3,308 | 6,215,079 | 3,215 |
| MU | HL | 5 | revert_30s_touch | 1.8 | 648 | 70.7% | 2.15 | 1,392 | 6,215,079 | 1,368 |
| MU | HL | 5 | revert_30s_touch | 0 | 648 | 82.7% | 3.95 | 2,558 | 6,215,079 | 2,486 |
| SNDK | ASTER | 1 | 5s_mid | 1.8 | 13,614 | 27.8% | -2.65 | -36,017 | 109,134,990 | -29,512 |
| SNDK | ASTER | 1 | 5s_mid | 0 | 13,614 | 42.4% | -0.85 | -11,511 | 109,134,990 | -9,867 |
| SNDK | ASTER | 1 | revert_30s_touch | 1.8 | 13,617 | 6.7% | -3.76 | -51,140 | 109,164,990 | -41,469 |
| SNDK | ASTER | 1 | revert_30s_touch | 0 | 13,617 | 17.7% | -1.96 | -26,629 | 109,164,990 | -21,820 |
| SNDK | ASTER | 2 | 5s_mid | 1.8 | 6,982 | 32.3% | -2.47 | -17,252 | 54,352,186 | -13,684 |
| SNDK | ASTER | 2 | 5s_mid | 0 | 6,982 | 45.4% | -0.67 | -4,684 | 54,352,186 | -3,901 |
| SNDK | ASTER | 2 | revert_30s_touch | 1.8 | 6,982 | 10.6% | -3.53 | -24,647 | 54,352,186 | -19,583 |
| SNDK | ASTER | 2 | revert_30s_touch | 0 | 6,982 | 25.0% | -1.73 | -12,080 | 54,352,186 | -9,800 |
| SNDK | ASTER | 3 | 5s_mid | 1.8 | 3,167 | 34.1% | -2.55 | -8,062 | 24,137,002 | -6,057 |
| SNDK | ASTER | 3 | 5s_mid | 0 | 3,167 | 47.6% | -0.75 | -2,361 | 24,137,002 | -1,712 |
| SNDK | ASTER | 3 | revert_30s_touch | 1.8 | 3,167 | 11.6% | -3.54 | -11,209 | 24,137,002 | -8,600 |
| SNDK | ASTER | 3 | revert_30s_touch | 0 | 3,167 | 27.6% | -1.74 | -5,508 | 24,137,002 | -4,255 |
| SNDK | ASTER | 5 | 5s_mid | 1.8 | 522 | 41.0% | -3.45 | -1,801 | 3,717,299 | -1,080 |
| SNDK | ASTER | 5 | 5s_mid | 0 | 522 | 49.8% | -1.65 | -861 | 3,717,299 | -411 |
| SNDK | ASTER | 5 | revert_30s_touch | 1.8 | 522 | 13.0% | -3.83 | -2,002 | 3,717,299 | -1,430 |
| SNDK | ASTER | 5 | revert_30s_touch | 0 | 522 | 24.5% | -2.03 | -1,062 | 3,717,299 | -760 |
| SNDK | HL | 1 | 5s_mid | 1.8 | 9,696 | 39.5% | -0.77 | -7,427 | 95,613,891 | -7,244 |
| SNDK | HL | 1 | 5s_mid | 0 | 9,696 | 59.4% | 1.03 | 10,026 | 95,613,891 | 9,967 |
| SNDK | HL | 1 | revert_30s_touch | 1.8 | 9,699 | 24.2% | -0.57 | -5,481 | 95,643,891 | -5,427 |
| SNDK | HL | 1 | revert_30s_touch | 0 | 9,699 | 60.2% | 1.23 | 11,977 | 95,643,891 | 11,789 |
| SNDK | HL | 2 | 5s_mid | 1.8 | 5,591 | 48.5% | 0.07 | 375 | 55,096,105 | 400 |
| SNDK | HL | 2 | 5s_mid | 0 | 5,591 | 64.8% | 1.87 | 10,439 | 55,096,105 | 10,318 |
| SNDK | HL | 2 | revert_30s_touch | 1.8 | 5,591 | 36.6% | 0.12 | 643 | 55,096,105 | 625 |
| SNDK | HL | 2 | revert_30s_touch | 0 | 5,591 | 71.0% | 1.92 | 10,707 | 55,096,105 | 10,542 |
| SNDK | HL | 3 | 5s_mid | 1.8 | 3,167 | 55.9% | 0.79 | 2,491 | 31,176,326 | 2,533 |
| SNDK | HL | 3 | 5s_mid | 0 | 3,167 | 69.5% | 2.59 | 8,191 | 31,176,326 | 8,144 |
| SNDK | HL | 3 | revert_30s_touch | 1.8 | 3,167 | 48.2% | 0.71 | 2,258 | 31,176,326 | 2,203 |
| SNDK | HL | 3 | revert_30s_touch | 0 | 3,167 | 77.4% | 2.51 | 7,959 | 31,176,326 | 7,815 |
| SNDK | HL | 5 | 5s_mid | 1.8 | 959 | 68.4% | 2.77 | 2,654 | 9,356,923 | 2,675 |
| SNDK | HL | 5 | 5s_mid | 0 | 959 | 76.1% | 4.57 | 4,381 | 9,356,923 | 4,359 |
| SNDK | HL | 5 | revert_30s_touch | 1.8 | 959 | 70.1% | 2.18 | 2,095 | 9,356,923 | 2,016 |
| SNDK | HL | 5 | revert_30s_touch | 0 | 959 | 86.2% | 3.98 | 3,821 | 9,356,923 | 3,701 |

### 3d. Cross-correlation of 100 ms returns, perp vs reference, -2 s .. +2 s

Positive lag = the perp moves after the stock.

| sym | leg | peak lag ms | peak corr | corr at lag 0 |
| --- | --- | ---: | ---: | ---: |
| SPCX | ASTER | 100 | 0.357 | 0.254 |
| SPCX | HL | 700 | 0.225 | 0.021 |
| SPCX | LIGHTER | 0 | 0.293 | 0.293 |
| SPCX | LIGHTER_RH | 0 | 0.214 | 0.214 |
| MU | ASTER | 0 | 0.179 | 0.179 |
| MU | HL | 500 | 0.189 | 0.047 |
| MU | LIGHTER | 0 | 0.293 | 0.293 |
| MU | LIGHTER_RH | 0 | 0.244 | 0.244 |
| SNDK | ASTER | 0 | 0.299 | 0.299 |
| SNDK | HL | 500 | 0.212 | 0.060 |
| SNDK | LIGHTER | -100 | 0.276 | 0.259 |
| SNDK | LIGHTER_RH | 0 | 0.258 | 0.258 |

Peak coefficients are 0.18-0.36, three to five times what the 09-08 Singapore run measured
(0.02-0.11, read). Aster and both Lighter books peak at 0 or +/-100 ms; HL peaks at
+500 to +700 ms with almost no correlation at lag 0.

### 3e. Verdict per symbol

| symbol | verdict |
| --- | --- |
| SPCX | **Basis only on Aster.** Aster sits 5-16 bps above the stock all day and its reverse-basis windows lose 1.4-1.8 bps net of 1.8 bps fees at every theta; at zero fee they are +0.05 to +0.4 bps, i.e. break-even. The exploitable ms signal on this symbol is on HL (+1.7k to +2.8k USD/day at theta 3-5 with fees paid), not Aster. |
| MU | **Basis only on Aster.** Aster is 7-10 bps rich all day. Every theta loses money at 1.8 bps of fees and every theta except 5 also loses at zero fee; the one positive cell is theta = 5 under the 5 s rule (198 trades, +102 USD with fees, +354 without), which is too small to call and flips negative under the revert rule (-373 USD). HL is positive from theta = 2 up (+0.8k to +2.3k USD/day with fees). |
| SNDK | **Basis only on Aster, and Aster is the worst of the three.** Aster hovers around the stock (-2.5 to +5.4 bps) so windows are frequent (up to 1,050/h at theta = 1) but every single bucket loses money under both exit rules and both fee assumptions. HL again positive from theta = 2 (+0.4k to +2.7k USD/day with fees). |

Across all three symbols there is **no millisecond-level stale-quote signal on Aster from a
US-east box**: at every theta the naive follower is negative after 1.8 bps of fees, and at
zero fee it is between -30k and +1.7k USD/day - i.e. inside the noise, and negative on two of
the three symbols. What Aster does have is a large, persistent, decaying basis (3a). HL is
the only leg where crossing the touch against a basis-corrected fair pays after fees, and it
does so on all three symbols.

## 4. Command lines used

```bash
# recording (pm2, on vultr-us) - read from the log header line
pm2 start .venv/bin/python --name stocks-ref --cwd ~/Nautilus-Perps --no-autorestart --time \
  -o logs/pm2-stocks-ref.out.log -e logs/pm2-stocks-ref.err.log -- \
  src/spread_watch.py --symbols SPCX,MU,SNDK --venues HL,LIGHTER,LIGHTER_RH,ASTER \
  --reference FUTU --until 2026-09-09T20:05:00Z --out reports/stage1

# existing tool, per symbol, both thresholds (run on the box)
cd ~/Nautilus-Perps
S=20260909T120046Z
for SYM in MU SPCX SNDK; do
  F=reports/stage1/${SYM}_HL-LIGHTER-LIGHTER_RH-ASTER_${S}_ref.csv
  .venv/bin/python src/analysis/leadlag.py "$F" --rth-only --out /tmp/ll/leadlag_${SYM}.md
  .venv/bin/python src/analysis/leadlag.py "$F" --rth-only --threshold-bps 1 \
      --out /tmp/ll/leadlag_${SYM}_t1.md
done

# Aster analysis (this directory's aster_msq.py, run on the box)
for SYM in MU SPCX SNDK; do
  .venv/bin/python /tmp/ll/aster_msq.py $SYM \
    --ref  reports/stage1/${SYM}_HL-LIGHTER-LIGHTER_RH-ASTER_${S}_ref.csv \
    --depth reports/stage1/depth_${SYM}_HL-LIGHTER-LIGHTER_RH-ASTER_${S}.csv \
    --out-dir /tmp/ll
done
```

Cost on the box (computed, `/usr/bin/time -v`): `leadlag.py` 2:20 to 4:15 wall per run,
peak RSS 1.65 GB; `aster_msq.py` 0:17 to 0:20 wall per symbol, peak RSS 0.79 GB (it reads
13 of the 30 columns).

## 5. Files in this directory

| file | what |
| --- | --- |
| `leadlag_<SYM>.md` | `src/analysis/leadlag.py --rth-only`, threshold 2 bps (default) |
| `leadlag_<SYM>_t1.md` | same, `--threshold-bps 1` |
| `aster_msq.py` | the scratch script behind section 3 |
| `aster_ms_<SYM>.json` | its raw output, including the full cross-correlation curves |

The three ~900 MB `_ref.csv` files and the 1 s `depth_` / `trades_` / `spread_` files stay on
the box under `~/Nautilus-Perps/reports/stage1/`.

## 6. Limitations

1. **The reference is still Futu, still two exchanges.** Nasdaq + NYSE Arca, not the SIP/NBBO.
   The composite crossed and fell back to the single freshest exchange 5% (MU, SNDK) to 22%
   (SPCX) of rows.
2. **A periodic reference artefact.** Every symbol shows bad reference prints on a strict
   20-minute cadence from 18:02 UTC onwards (18:02, 18:22, 18:42, 19:02, 19:22, 19:42), each
   one a jump of -79 to +362 bps that coincides with `ref_book_mode` flipping from
   `composite` to `freshest`, and each one reverting within a second. 5 to 8 such prints per
   symbol. They are the source of the 380-392 bps `max` values in the `leadlag_*.md` edge
   tables. Section 3 drops them; `leadlag.py` does not.
3. **`leadlag.py` does not exclude the Futu outages** (section 2), nor the artefact prints.
4. **The basis estimator is a 5-minute rolling median.** When the basis itself moves fast
   (the open, and SPCX generally) `fair` lags and manufactures long windows. See 3b.
5. **Touch size is 1 s resolution.** The `_ref.csv` has no size columns, so the USD figures
   come from the nearest earlier 1 s depth snapshot, up to 1 s stale relative to a window
   whose median life is 40-110 ms.
6. **No queue, no latency, no adverse selection.** The follower assumes it crosses the
   displayed touch instantly for up to 10k USD. Our own order round trip, partial fills, and
   the market maker's reaction to being picked off are all absent, and all push the same way.
7. **One session.** 2026-09-09 only, three symbols, one box.

## Appendix: 3c per side, ungrouped

| sym | leg | theta bps | side | rule | fee bps rt | trades | win rate | mean bps | total bps | notional USD | P&L USD | hold p50 ms |
| --- | --- | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| SPCX | ASTER | 1 | buy | 5s_mid | 1.8 | 4,643 | 28.3% | -1.51 | -7,010.7 | 44,727,571 | -6,475 | - |
| SPCX | ASTER | 1 | buy | 5s_mid | 0 | 4,643 | 53.8% | 0.29 | 1,346.7 | 44,727,571 | 1,576 | - |
| SPCX | ASTER | 1 | buy | revert_30s_touch | 1.8 | 4,648 | 12.7% | -1.75 | -8,122.0 | 44,774,458 | -7,856 | 748 |
| SPCX | ASTER | 1 | buy | revert_30s_touch | 0 | 4,648 | 30.0% | 0.05 | 244.4 | 44,774,458 | 203 | 748 |
| SPCX | ASTER | 1 | sell | 5s_mid | 1.8 | 4,276 | 33.6% | -1.73 | -7,385.3 | 37,348,362 | -6,561 | - |
| SPCX | ASTER | 1 | sell | 5s_mid | 0 | 4,276 | 57.0% | 0.07 | 311.5 | 37,348,362 | 162 | - |
| SPCX | ASTER | 1 | sell | revert_30s_touch | 1.8 | 4,279 | 15.4% | -1.75 | -7,467.5 | 37,357,559 | -6,435 | 582 |
| SPCX | ASTER | 1 | sell | revert_30s_touch | 0 | 4,279 | 33.6% | 0.05 | 234.7 | 37,357,559 | 290 | 582 |
| SPCX | ASTER | 2 | buy | 5s_mid | 1.8 | 2,010 | 35.2% | -1.22 | -2,455.2 | 19,100,253 | -2,085 | - |
| SPCX | ASTER | 2 | buy | 5s_mid | 0 | 2,010 | 56.5% | 0.58 | 1,162.8 | 19,100,253 | 1,353 | - |
| SPCX | ASTER | 2 | buy | revert_30s_touch | 1.8 | 2,011 | 20.7% | -1.50 | -3,022.0 | 19,110,253 | -2,849 | 773 |
| SPCX | ASTER | 2 | buy | revert_30s_touch | 0 | 2,011 | 42.4% | 0.30 | 597.8 | 19,110,253 | 591 | 773 |
| SPCX | ASTER | 2 | sell | 5s_mid | 1.8 | 2,162 | 38.6% | -1.92 | -4,151.8 | 19,655,929 | -3,784 | - |
| SPCX | ASTER | 2 | sell | 5s_mid | 0 | 2,162 | 55.3% | -0.12 | -260.2 | 19,655,929 | -246 | - |
| SPCX | ASTER | 2 | sell | revert_30s_touch | 1.8 | 2,163 | 26.8% | -1.31 | -2,837.6 | 19,657,431 | -2,575 | 1,162 |
| SPCX | ASTER | 2 | sell | revert_30s_touch | 0 | 2,163 | 47.9% | 0.49 | 1,055.8 | 19,657,431 | 964 | 1,162 |
| SPCX | ASTER | 3 | buy | 5s_mid | 1.8 | 1,035 | 39.7% | -0.84 | -872.6 | 9,825,705 | -812 | - |
| SPCX | ASTER | 3 | buy | 5s_mid | 0 | 1,035 | 56.4% | 0.96 | 990.4 | 9,825,705 | 957 | - |
| SPCX | ASTER | 3 | buy | revert_30s_touch | 1.8 | 1,035 | 27.1% | -1.18 | -1,223.6 | 9,825,705 | -1,175 | 741 |
| SPCX | ASTER | 3 | buy | revert_30s_touch | 0 | 1,035 | 46.8% | 0.62 | 639.4 | 9,825,705 | 594 | 741 |
| SPCX | ASTER | 3 | sell | 5s_mid | 1.8 | 1,538 | 37.1% | -2.36 | -3,631.7 | 14,471,420 | -3,524 | - |
| SPCX | ASTER | 3 | sell | 5s_mid | 0 | 1,538 | 52.0% | -0.56 | -863.3 | 14,471,420 | -919 | - |
| SPCX | ASTER | 3 | sell | revert_30s_touch | 1.8 | 1,539 | 35.2% | -1.20 | -1,852.1 | 14,472,921 | -1,742 | 2,400 |
| SPCX | ASTER | 3 | sell | revert_30s_touch | 0 | 1,539 | 52.0% | 0.60 | 918.1 | 14,472,921 | 863 | 2,400 |
| SPCX | ASTER | 5 | buy | 5s_mid | 1.8 | 345 | 39.1% | -0.31 | -105.5 | 3,281,702 | -88 | - |
| SPCX | ASTER | 5 | buy | 5s_mid | 0 | 345 | 52.8% | 1.49 | 515.5 | 3,281,702 | 503 | - |
| SPCX | ASTER | 5 | buy | revert_30s_touch | 1.8 | 345 | 38.3% | -1.48 | -512.1 | 3,281,702 | -488 | 1,469 |
| SPCX | ASTER | 5 | buy | revert_30s_touch | 0 | 345 | 50.4% | 0.32 | 108.9 | 3,281,702 | 103 | 1,469 |
| SPCX | ASTER | 5 | sell | 5s_mid | 1.8 | 856 | 42.5% | -2.04 | -1,745.4 | 8,025,050 | -1,588 | - |
| SPCX | ASTER | 5 | sell | 5s_mid | 0 | 856 | 53.2% | -0.24 | -204.6 | 8,025,050 | -143 | - |
| SPCX | ASTER | 5 | sell | revert_30s_touch | 1.8 | 856 | 45.6% | -0.66 | -562.4 | 8,025,050 | -519 | 4,550 |
| SPCX | ASTER | 5 | sell | revert_30s_touch | 0 | 856 | 58.4% | 1.14 | 978.4 | 8,025,050 | 925 | 4,550 |
| SPCX | HL | 1 | buy | 5s_mid | 1.8 | 4,431 | 29.4% | -0.81 | -3,607.5 | 43,530,128 | -3,677 | - |
| SPCX | HL | 1 | buy | 5s_mid | 0 | 4,431 | 60.5% | 0.99 | 4,368.3 | 43,530,128 | 4,158 | - |
| SPCX | HL | 1 | buy | revert_30s_touch | 1.8 | 4,435 | 23.6% | -0.69 | -3,061.1 | 43,570,128 | -3,102 | 1,000 |
| SPCX | HL | 1 | buy | revert_30s_touch | 0 | 4,435 | 51.6% | 1.11 | 4,921.9 | 43,570,128 | 4,740 | 1,000 |
| SPCX | HL | 1 | sell | 5s_mid | 1.8 | 4,185 | 32.4% | -0.93 | -3,887.2 | 40,362,123 | -3,336 | - |
| SPCX | HL | 1 | sell | 5s_mid | 0 | 4,185 | 61.3% | 0.87 | 3,645.8 | 40,362,123 | 3,929 | - |
| SPCX | HL | 1 | sell | revert_30s_touch | 1.8 | 4,192 | 26.4% | -0.60 | -2,509.8 | 40,415,797 | -2,451 | 1,002 |
| SPCX | HL | 1 | sell | revert_30s_touch | 0 | 4,192 | 55.0% | 1.20 | 5,035.8 | 40,415,797 | 4,824 | 1,002 |
| SPCX | HL | 2 | buy | 5s_mid | 1.8 | 2,412 | 40.9% | -0.03 | -77.8 | 23,582,267 | -171 | - |
| SPCX | HL | 2 | buy | 5s_mid | 0 | 2,412 | 68.1% | 1.77 | 4,263.8 | 23,582,267 | 4,073 | - |
| SPCX | HL | 2 | buy | revert_30s_touch | 1.8 | 2,415 | 35.7% | 0.01 | 19.8 | 23,612,267 | -57 | 980 |
| SPCX | HL | 2 | buy | revert_30s_touch | 0 | 2,415 | 68.2% | 1.81 | 4,366.8 | 23,612,267 | 4,194 | 980 |
| SPCX | HL | 2 | sell | 5s_mid | 1.8 | 2,415 | 43.8% | -0.20 | -479.9 | 23,032,151 | -246 | - |
| SPCX | HL | 2 | sell | 5s_mid | 0 | 2,415 | 69.6% | 1.60 | 3,867.1 | 23,032,151 | 3,900 | - |
| SPCX | HL | 2 | sell | revert_30s_touch | 1.8 | 2,419 | 41.1% | 0.18 | 426.7 | 23,059,906 | 374 | 1,001 |
| SPCX | HL | 2 | sell | revert_30s_touch | 0 | 2,419 | 73.1% | 1.98 | 4,780.9 | 23,059,906 | 4,525 | 1,001 |
| SPCX | HL | 3 | buy | 5s_mid | 1.8 | 1,264 | 53.6% | 0.99 | 1,250.5 | 12,263,152 | 1,073 | - |
| SPCX | HL | 3 | buy | 5s_mid | 0 | 1,264 | 75.3% | 2.79 | 3,525.7 | 12,263,152 | 3,281 | - |
| SPCX | HL | 3 | buy | revert_30s_touch | 1.8 | 1,266 | 51.2% | 1.10 | 1,394.8 | 12,283,152 | 1,245 | 811 |
| SPCX | HL | 3 | buy | revert_30s_touch | 0 | 1,266 | 79.7% | 2.90 | 3,673.6 | 12,283,152 | 3,456 | 811 |
| SPCX | HL | 3 | sell | 5s_mid | 1.8 | 1,449 | 52.9% | 0.33 | 472.0 | 13,687,256 | 642 | - |
| SPCX | HL | 3 | sell | 5s_mid | 0 | 1,449 | 71.4% | 2.13 | 3,080.2 | 13,687,256 | 3,106 | - |
| SPCX | HL | 3 | sell | revert_30s_touch | 1.8 | 1,450 | 51.7% | 0.75 | 1,085.1 | 13,693,174 | 995 | 940 |
| SPCX | HL | 3 | sell | revert_30s_touch | 0 | 1,450 | 77.9% | 2.55 | 3,695.1 | 13,693,174 | 3,460 | 940 |
| SPCX | HL | 5 | buy | 5s_mid | 1.8 | 448 | 71.9% | 3.59 | 1,606.4 | 4,230,146 | 1,411 | - |
| SPCX | HL | 5 | buy | 5s_mid | 0 | 448 | 81.9% | 5.39 | 2,412.8 | 4,230,146 | 2,172 | - |
| SPCX | HL | 5 | buy | revert_30s_touch | 1.8 | 448 | 73.2% | 2.60 | 1,164.9 | 4,230,146 | 997 | 820 |
| SPCX | HL | 5 | buy | revert_30s_touch | 0 | 448 | 88.6% | 4.40 | 1,971.3 | 4,230,146 | 1,759 | 820 |
| SPCX | HL | 5 | sell | 5s_mid | 1.8 | 582 | 64.3% | 2.15 | 1,248.9 | 5,409,589 | 1,344 | - |
| SPCX | HL | 5 | sell | 5s_mid | 0 | 582 | 74.4% | 3.95 | 2,296.5 | 5,409,589 | 2,317 | - |
| SPCX | HL | 5 | sell | revert_30s_touch | 1.8 | 582 | 67.4% | 2.14 | 1,244.6 | 5,409,589 | 1,175 | 942 |
| SPCX | HL | 5 | sell | revert_30s_touch | 0 | 582 | 83.0% | 3.94 | 2,292.2 | 5,409,589 | 2,149 | 942 |
| MU | ASTER | 1 | buy | 5s_mid | 1.8 | 5,611 | 30.7% | -2.88 | -16,137.7 | 46,783,283 | -13,349 | - |
| MU | ASTER | 1 | buy | 5s_mid | 0 | 5,611 | 43.1% | -1.08 | -6,037.9 | 46,783,283 | -4,928 | - |
| MU | ASTER | 1 | buy | revert_30s_touch | 1.8 | 5,611 | 3.8% | -3.76 | -21,088.2 | 46,783,283 | -17,549 | 368 |
| MU | ASTER | 1 | buy | revert_30s_touch | 0 | 5,611 | 13.1% | -1.96 | -10,988.4 | 46,783,283 | -9,128 | 368 |
| MU | ASTER | 1 | sell | 5s_mid | 1.8 | 7,392 | 27.1% | -2.32 | -17,168.8 | 57,598,800 | -13,397 | - |
| MU | ASTER | 1 | sell | 5s_mid | 0 | 7,392 | 43.2% | -0.52 | -3,863.2 | 57,598,800 | -3,029 | - |
| MU | ASTER | 1 | sell | revert_30s_touch | 1.8 | 7,392 | 1.2% | -3.92 | -28,974.5 | 57,598,800 | -22,440 | 230 |
| MU | ASTER | 1 | sell | revert_30s_touch | 0 | 7,392 | 5.3% | -2.12 | -15,668.9 | 57,598,800 | -12,072 | 230 |
| MU | ASTER | 2 | buy | 5s_mid | 1.8 | 2,114 | 35.2% | -2.97 | -6,273.4 | 16,948,214 | -4,956 | - |
| MU | ASTER | 2 | buy | 5s_mid | 0 | 2,114 | 44.3% | -1.17 | -2,468.2 | 16,948,214 | -1,905 | - |
| MU | ASTER | 2 | buy | revert_30s_touch | 1.8 | 2,114 | 4.9% | -3.66 | -7,747.7 | 16,948,214 | -6,283 | 303 |
| MU | ASTER | 2 | buy | revert_30s_touch | 0 | 2,114 | 16.7% | -1.86 | -3,942.5 | 16,948,214 | -3,233 | 303 |
| MU | ASTER | 2 | sell | 5s_mid | 1.8 | 2,344 | 31.3% | -1.80 | -4,229.4 | 17,798,167 | -3,349 | - |
| MU | ASTER | 2 | sell | 5s_mid | 0 | 2,344 | 45.9% | -0.00 | -10.2 | 17,798,167 | -146 | - |
| MU | ASTER | 2 | sell | revert_30s_touch | 1.8 | 2,344 | 2.1% | -3.75 | -8,786.1 | 17,798,167 | -6,601 | 246 |
| MU | ASTER | 2 | sell | revert_30s_touch | 0 | 2,344 | 7.8% | -1.95 | -4,566.9 | 17,798,167 | -3,397 | 246 |
| MU | ASTER | 3 | buy | 5s_mid | 1.8 | 760 | 35.4% | -2.99 | -2,272.8 | 5,909,840 | -1,900 | - |
| MU | ASTER | 3 | buy | 5s_mid | 0 | 760 | 45.9% | -1.19 | -904.8 | 5,909,840 | -837 | - |
| MU | ASTER | 3 | buy | revert_30s_touch | 1.8 | 760 | 9.9% | -3.57 | -2,715.9 | 5,909,840 | -2,134 | 246 |
| MU | ASTER | 3 | buy | revert_30s_touch | 0 | 760 | 19.2% | -1.77 | -1,347.9 | 5,909,840 | -1,070 | 246 |
| MU | ASTER | 3 | sell | 5s_mid | 1.8 | 676 | 40.1% | -0.37 | -247.5 | 4,961,912 | -207 | - |
| MU | ASTER | 3 | sell | 5s_mid | 0 | 676 | 52.7% | 1.43 | 969.3 | 4,961,912 | 686 | - |
| MU | ASTER | 3 | sell | revert_30s_touch | 1.8 | 676 | 3.8% | -3.78 | -2,553.4 | 4,961,912 | -1,846 | 194 |
| MU | ASTER | 3 | sell | revert_30s_touch | 0 | 676 | 12.1% | -1.98 | -1,336.6 | 4,961,912 | -953 | 194 |
| MU | ASTER | 5 | buy | 5s_mid | 1.8 | 111 | 54.1% | -0.19 | -20.8 | 806,071 | -57 | - |
| MU | ASTER | 5 | buy | 5s_mid | 0 | 111 | 61.3% | 1.61 | 179.0 | 806,071 | 88 | - |
| MU | ASTER | 5 | buy | revert_30s_touch | 1.8 | 111 | 16.2% | -2.68 | -297.9 | 806,071 | -213 | 138 |
| MU | ASTER | 5 | buy | revert_30s_touch | 0 | 111 | 29.7% | -0.88 | -98.1 | 806,071 | -67 | 138 |
| MU | ASTER | 5 | sell | 5s_mid | 1.8 | 87 | 56.3% | 2.97 | 258.6 | 594,385 | 159 | - |
| MU | ASTER | 5 | sell | 5s_mid | 0 | 87 | 64.4% | 4.77 | 415.2 | 594,385 | 266 | - |
| MU | ASTER | 5 | sell | revert_30s_touch | 1.8 | 87 | 11.5% | -2.74 | -238.1 | 594,385 | -160 | 167 |
| MU | ASTER | 5 | sell | revert_30s_touch | 0 | 87 | 14.9% | -0.94 | -81.5 | 594,385 | -53 | 167 |
| MU | HL | 1 | buy | 5s_mid | 1.8 | 3,672 | 37.0% | -0.83 | -3,042.1 | 36,039,852 | -2,828 | - |
| MU | HL | 1 | buy | 5s_mid | 0 | 3,672 | 59.6% | 0.97 | 3,567.5 | 36,039,852 | 3,659 | - |
| MU | HL | 1 | buy | revert_30s_touch | 1.8 | 3,673 | 27.7% | -0.79 | -2,907.7 | 36,049,852 | -2,855 | 835 |
| MU | HL | 1 | buy | revert_30s_touch | 0 | 3,673 | 49.0% | 1.01 | 3,703.7 | 36,049,852 | 3,633 | 835 |
| MU | HL | 1 | sell | 5s_mid | 1.8 | 3,395 | 36.9% | -0.69 | -2,356.3 | 33,567,290 | -2,321 | - |
| MU | HL | 1 | sell | 5s_mid | 0 | 3,395 | 58.1% | 1.11 | 3,754.7 | 33,567,290 | 3,721 | - |
| MU | HL | 1 | sell | revert_30s_touch | 1.8 | 3,395 | 26.6% | -0.97 | -3,295.4 | 33,567,290 | -3,261 | 760 |
| MU | HL | 1 | sell | revert_30s_touch | 0 | 3,395 | 49.1% | 0.83 | 2,815.6 | 33,567,290 | 2,781 | 760 |
| MU | HL | 2 | buy | 5s_mid | 1.8 | 1,767 | 48.7% | 0.15 | 259.1 | 17,131,996 | 408 | - |
| MU | HL | 2 | buy | 5s_mid | 0 | 1,767 | 66.3% | 1.95 | 3,439.7 | 17,131,996 | 3,492 | - |
| MU | HL | 2 | buy | revert_30s_touch | 1.8 | 1,767 | 40.8% | -0.10 | -177.5 | 17,131,996 | -182 | 733 |
| MU | HL | 2 | buy | revert_30s_touch | 0 | 1,767 | 65.8% | 1.70 | 3,003.1 | 17,131,996 | 2,902 | 733 |
| MU | HL | 2 | sell | 5s_mid | 1.8 | 1,610 | 47.1% | 0.23 | 370.0 | 15,922,436 | 357 | - |
| MU | HL | 2 | sell | 5s_mid | 0 | 1,610 | 63.9% | 2.03 | 3,268.0 | 15,922,436 | 3,223 | - |
| MU | HL | 2 | sell | revert_30s_touch | 1.8 | 1,610 | 42.2% | -0.11 | -171.9 | 15,922,436 | -176 | 660 |
| MU | HL | 2 | sell | revert_30s_touch | 0 | 1,610 | 66.1% | 1.69 | 2,726.1 | 15,922,436 | 2,690 | 660 |
| MU | HL | 3 | buy | 5s_mid | 1.8 | 994 | 53.8% | 0.79 | 789.9 | 9,573,091 | 864 | - |
| MU | HL | 3 | buy | 5s_mid | 0 | 994 | 67.7% | 2.59 | 2,579.1 | 9,573,091 | 2,587 | - |
| MU | HL | 3 | buy | revert_30s_touch | 1.8 | 994 | 54.3% | 0.60 | 599.5 | 9,573,091 | 605 | 715 |
| MU | HL | 3 | buy | revert_30s_touch | 0 | 994 | 72.2% | 2.40 | 2,388.7 | 9,573,091 | 2,328 | 715 |
| MU | HL | 3 | sell | 5s_mid | 1.8 | 829 | 57.7% | 1.71 | 1,420.5 | 8,179,260 | 1,405 | - |
| MU | HL | 3 | sell | 5s_mid | 0 | 829 | 70.7% | 3.51 | 2,912.7 | 8,179,260 | 2,878 | - |
| MU | HL | 3 | sell | revert_30s_touch | 1.8 | 829 | 60.6% | 1.15 | 952.6 | 8,179,260 | 915 | 625 |
| MU | HL | 3 | sell | revert_30s_touch | 0 | 829 | 79.3% | 2.95 | 2,444.8 | 8,179,260 | 2,387 | 625 |
| MU | HL | 5 | buy | 5s_mid | 1.8 | 368 | 61.7% | 2.78 | 1,022.9 | 3,459,357 | 1,000 | - |
| MU | HL | 5 | buy | 5s_mid | 0 | 368 | 72.8% | 4.58 | 1,685.3 | 3,459,357 | 1,623 | - |
| MU | HL | 5 | buy | revert_30s_touch | 1.8 | 368 | 67.9% | 2.18 | 801.8 | 3,459,357 | 782 | 673 |
| MU | HL | 5 | buy | revert_30s_touch | 0 | 368 | 80.7% | 3.98 | 1,464.2 | 3,459,357 | 1,405 | 673 |
| MU | HL | 5 | sell | 5s_mid | 1.8 | 280 | 66.8% | 3.99 | 1,118.5 | 2,755,722 | 1,096 | - |
| MU | HL | 5 | sell | 5s_mid | 0 | 280 | 78.2% | 5.79 | 1,622.5 | 2,755,722 | 1,592 | - |
| MU | HL | 5 | sell | revert_30s_touch | 1.8 | 280 | 74.3% | 2.11 | 590.0 | 2,755,722 | 585 | 636 |
| MU | HL | 5 | sell | revert_30s_touch | 0 | 280 | 85.4% | 3.91 | 1,094.0 | 2,755,722 | 1,081 | 636 |
| SNDK | ASTER | 1 | buy | 5s_mid | 1.8 | 6,802 | 28.7% | -2.60 | -17,710.4 | 56,795,127 | -15,242 | - |
| SNDK | ASTER | 1 | buy | 5s_mid | 0 | 6,802 | 43.1% | -0.80 | -5,466.8 | 56,795,127 | -5,018 | - |
| SNDK | ASTER | 1 | buy | revert_30s_touch | 1.8 | 6,805 | 7.7% | -3.63 | -24,675.4 | 56,825,127 | -21,043 | 1,150 |
| SNDK | ASTER | 1 | buy | revert_30s_touch | 0 | 6,805 | 19.1% | -1.83 | -12,426.4 | 56,825,127 | -10,815 | 1,150 |
| SNDK | ASTER | 1 | sell | 5s_mid | 1.8 | 6,812 | 26.9% | -2.69 | -18,306.2 | 52,339,863 | -14,270 | - |
| SNDK | ASTER | 1 | sell | 5s_mid | 0 | 6,812 | 41.6% | -0.89 | -6,044.6 | 52,339,863 | -4,849 | - |
| SNDK | ASTER | 1 | sell | revert_30s_touch | 1.8 | 6,812 | 5.7% | -3.88 | -26,464.4 | 52,339,863 | -20,426 | 1,186 |
| SNDK | ASTER | 1 | sell | revert_30s_touch | 0 | 6,812 | 16.3% | -2.08 | -14,202.8 | 52,339,863 | -11,005 | 1,186 |
| SNDK | ASTER | 2 | buy | 5s_mid | 1.8 | 3,538 | 33.1% | -2.06 | -7,300.0 | 28,640,273 | -6,269 | - |
| SNDK | ASTER | 2 | buy | 5s_mid | 0 | 3,538 | 47.1% | -0.26 | -931.6 | 28,640,273 | -1,114 | - |
| SNDK | ASTER | 2 | buy | revert_30s_touch | 1.8 | 3,538 | 11.3% | -3.03 | -10,704.4 | 28,640,273 | -9,081 | 1,358 |
| SNDK | ASTER | 2 | buy | revert_30s_touch | 0 | 3,538 | 25.9% | -1.23 | -4,336.0 | 28,640,273 | -3,926 | 1,358 |
| SNDK | ASTER | 2 | sell | 5s_mid | 1.8 | 3,444 | 31.5% | -2.89 | -9,951.6 | 25,711,913 | -7,416 | - |
| SNDK | ASTER | 2 | sell | 5s_mid | 0 | 3,444 | 43.6% | -1.09 | -3,752.4 | 25,711,913 | -2,787 | - |
| SNDK | ASTER | 2 | sell | revert_30s_touch | 1.8 | 3,444 | 9.9% | -4.05 | -13,942.9 | 25,711,913 | -10,502 | 1,210 |
| SNDK | ASTER | 2 | sell | revert_30s_touch | 0 | 3,444 | 24.0% | -2.25 | -7,743.7 | 25,711,913 | -5,874 | 1,210 |
| SNDK | ASTER | 3 | buy | 5s_mid | 1.8 | 1,585 | 35.6% | -1.80 | -2,858.4 | 12,720,960 | -2,374 | - |
| SNDK | ASTER | 3 | buy | 5s_mid | 0 | 1,585 | 50.5% | -0.00 | -5.4 | 12,720,960 | -84 | - |
| SNDK | ASTER | 3 | buy | revert_30s_touch | 1.8 | 1,585 | 12.1% | -3.06 | -4,848.2 | 12,720,960 | -4,010 | 1,240 |
| SNDK | ASTER | 3 | buy | revert_30s_touch | 0 | 1,585 | 28.7% | -1.26 | -1,995.2 | 12,720,960 | -1,720 | 1,240 |
| SNDK | ASTER | 3 | sell | 5s_mid | 1.8 | 1,582 | 32.7% | -3.29 | -5,203.6 | 11,416,041 | -3,683 | - |
| SNDK | ASTER | 3 | sell | 5s_mid | 0 | 1,582 | 44.8% | -1.49 | -2,356.0 | 11,416,041 | -1,628 | - |
| SNDK | ASTER | 3 | sell | revert_30s_touch | 1.8 | 1,582 | 11.2% | -4.02 | -6,360.9 | 11,416,041 | -4,590 | 1,000 |
| SNDK | ASTER | 3 | sell | revert_30s_touch | 0 | 1,582 | 26.5% | -2.22 | -3,513.3 | 11,416,041 | -2,535 | 1,000 |
| SNDK | ASTER | 5 | buy | 5s_mid | 1.8 | 202 | 48.5% | -1.59 | -320.3 | 1,547,907 | -245 | - |
| SNDK | ASTER | 5 | buy | 5s_mid | 0 | 202 | 61.4% | 0.21 | 43.3 | 1,547,907 | 33 | - |
| SNDK | ASTER | 5 | buy | revert_30s_touch | 1.8 | 202 | 16.3% | -2.93 | -590.9 | 1,547,907 | -473 | 310 |
| SNDK | ASTER | 5 | buy | revert_30s_touch | 0 | 202 | 29.2% | -1.13 | -227.3 | 1,547,907 | -194 | 310 |
| SNDK | ASTER | 5 | sell | 5s_mid | 1.8 | 320 | 36.2% | -4.63 | -1,480.8 | 2,169,392 | -835 | - |
| SNDK | ASTER | 5 | sell | 5s_mid | 0 | 320 | 42.5% | -2.83 | -904.8 | 2,169,392 | -444 | - |
| SNDK | ASTER | 5 | sell | revert_30s_touch | 1.8 | 320 | 10.9% | -4.41 | -1,410.7 | 2,169,392 | -957 | 577 |
| SNDK | ASTER | 5 | sell | revert_30s_touch | 0 | 320 | 21.6% | -2.61 | -834.7 | 2,169,392 | -566 | 577 |
| SNDK | HL | 1 | buy | 5s_mid | 1.8 | 4,915 | 39.7% | -0.75 | -3,691.9 | 48,230,333 | -3,563 | - |
| SNDK | HL | 1 | buy | 5s_mid | 0 | 4,915 | 59.2% | 1.05 | 5,155.1 | 48,230,333 | 5,118 | - |
| SNDK | HL | 1 | buy | revert_30s_touch | 1.8 | 4,918 | 23.9% | -0.55 | -2,716.5 | 48,260,333 | -2,676 | 779 |
| SNDK | HL | 1 | buy | revert_30s_touch | 0 | 4,918 | 59.9% | 1.25 | 6,135.9 | 48,260,333 | 6,011 | 779 |
| SNDK | HL | 1 | sell | 5s_mid | 1.8 | 4,781 | 39.2% | -0.78 | -3,734.6 | 47,383,558 | -3,680 | - |
| SNDK | HL | 1 | sell | 5s_mid | 0 | 4,781 | 59.7% | 1.02 | 4,871.2 | 47,383,558 | 4,849 | - |
| SNDK | HL | 1 | sell | revert_30s_touch | 1.8 | 4,781 | 24.4% | -0.58 | -2,764.2 | 47,383,558 | -2,751 | 758 |
| SNDK | HL | 1 | sell | revert_30s_touch | 0 | 4,781 | 60.5% | 1.22 | 5,841.6 | 47,383,558 | 5,778 | 758 |
| SNDK | HL | 2 | buy | 5s_mid | 1.8 | 2,876 | 47.4% | 0.02 | 58.4 | 28,189,988 | 78 | - |
| SNDK | HL | 2 | buy | 5s_mid | 0 | 2,876 | 64.5% | 1.82 | 5,235.2 | 28,189,988 | 5,152 | - |
| SNDK | HL | 2 | buy | revert_30s_touch | 1.8 | 2,876 | 36.3% | 0.09 | 258.1 | 28,189,988 | 245 | 759 |
| SNDK | HL | 2 | buy | revert_30s_touch | 0 | 2,876 | 70.5% | 1.89 | 5,434.9 | 28,189,988 | 5,319 | 759 |
| SNDK | HL | 2 | sell | 5s_mid | 1.8 | 2,715 | 49.7% | 0.12 | 316.4 | 26,906,118 | 323 | - |
| SNDK | HL | 2 | sell | 5s_mid | 0 | 2,715 | 65.2% | 1.92 | 5,203.4 | 26,906,118 | 5,166 | - |
| SNDK | HL | 2 | sell | revert_30s_touch | 1.8 | 2,715 | 36.9% | 0.14 | 385.3 | 26,906,118 | 380 | 680 |
| SNDK | HL | 2 | sell | revert_30s_touch | 0 | 2,715 | 71.6% | 1.94 | 5,272.3 | 26,906,118 | 5,223 | 680 |
| SNDK | HL | 3 | buy | 5s_mid | 1.8 | 1,647 | 55.6% | 0.82 | 1,347.5 | 16,151,466 | 1,362 | - |
| SNDK | HL | 3 | buy | 5s_mid | 0 | 1,647 | 69.0% | 2.62 | 4,312.1 | 16,151,466 | 4,269 | - |
| SNDK | HL | 3 | buy | revert_30s_touch | 1.8 | 1,647 | 48.0% | 0.72 | 1,184.3 | 16,151,466 | 1,131 | 701 |
| SNDK | HL | 3 | buy | revert_30s_touch | 0 | 1,647 | 77.7% | 2.52 | 4,148.9 | 16,151,466 | 4,038 | 701 |
| SNDK | HL | 3 | sell | 5s_mid | 1.8 | 1,520 | 56.3% | 0.75 | 1,143.3 | 15,024,860 | 1,170 | - |
| SNDK | HL | 3 | sell | 5s_mid | 0 | 1,520 | 70.0% | 2.55 | 3,879.3 | 15,024,860 | 3,875 | - |
| SNDK | HL | 3 | sell | revert_30s_touch | 1.8 | 1,520 | 48.4% | 0.71 | 1,074.1 | 15,024,860 | 1,072 | 631 |
| SNDK | HL | 3 | sell | revert_30s_touch | 0 | 1,520 | 77.0% | 2.51 | 3,810.1 | 15,024,860 | 3,777 | 631 |
| SNDK | HL | 5 | buy | 5s_mid | 1.8 | 474 | 70.5% | 3.12 | 1,477.6 | 4,555,067 | 1,505 | - |
| SNDK | HL | 5 | buy | 5s_mid | 0 | 474 | 78.9% | 4.92 | 2,330.8 | 4,555,067 | 2,325 | - |
| SNDK | HL | 5 | buy | revert_30s_touch | 1.8 | 474 | 72.4% | 2.32 | 1,100.5 | 4,555,067 | 1,024 | 559 |
| SNDK | HL | 5 | buy | revert_30s_touch | 0 | 474 | 87.3% | 4.12 | 1,953.7 | 4,555,067 | 1,844 | 559 |
| SNDK | HL | 5 | sell | 5s_mid | 1.8 | 485 | 66.4% | 2.43 | 1,176.8 | 4,801,856 | 1,170 | - |
| SNDK | HL | 5 | sell | 5s_mid | 0 | 485 | 73.4% | 4.23 | 2,049.8 | 4,801,856 | 2,034 | - |
| SNDK | HL | 5 | sell | revert_30s_touch | 1.8 | 485 | 67.8% | 2.05 | 994.3 | 4,801,856 | 992 | 559 |
| SNDK | HL | 5 | sell | revert_30s_touch | 0 | 485 | 85.2% | 3.85 | 1,867.3 | 4,801,856 | 1,856 | 559 |

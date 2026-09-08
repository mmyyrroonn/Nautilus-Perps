# lead-lag: TSLA_HL-LIGHTER-LIGHTER_RH_20260908T144407Z_ref.csv

- rows: 1205843  span: 2026-09-08T14:44:11+00:00 .. 2026-09-08T19:59:59+00:00  rth_only: True
- threshold: 2 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0
- reference update rows: 827437   perp quote rows: 378406

## 1. edge distribution (time-weighted)

| leg | side | rows | secs | p50 | p90 | p99 | max | >0 | >2bps | >5bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 1205843 | 18949 | -3.21 | -1.45 | 0.19 | 25.55 | 1.20% | 0.16% | 0.02% |
| HL | sell | 1205843 | 18949 | 0.86 | 2.92 | 4.53 | 371.48 | 68.76% | 24.77% | 0.51% |
| LIGHTER | buy | 1205843 | 18949 | -6.70 | -5.30 | -4.09 | 4.10 | 0.00% | 0.00% | 0.00% |
| LIGHTER | sell | 1205843 | 18949 | 5.73 | 7.50 | 9.01 | 375.26 | 99.89% | 99.35% | 72.63% |
| LIGHTER_RH | buy | 1205843 | 18949 | -7.48 | -6.12 | -4.38 | 11.85 | 0.00% | 0.00% | 0.00% |
| LIGHTER_RH | sell | 1205843 | 18949 | 4.24 | 5.73 | 7.03 | 374.47 | 99.94% | 99.12% | 25.14% |

## 2. persistence of windows above 2 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 135 | 25.6 | 155 | 518 | 1059 |
| HL | sell | 2702 | 513.3 | 314 | 3809 | 175492 |
| LIGHTER | buy | 2 | 0.4 | 1 | 2 | 2 |
| LIGHTER | sell | 1368 | 259.9 | 1369 | 28355 | 829525 |
| LIGHTER_RH | buy | 4 | 0.8 | 48 | 101 | 103 |
| LIGHTER_RH | sell | 1100 | 209.0 | 2420 | 32829 | 971107 |

## 3. lead-lag (100 ms returns, -5s..+5s; positive lag = perp follows)

| leg | best lag ms | corr | corr at 0 |
| --- | ---: | ---: | ---: |
| HL | 300 | 0.022 | 0.005 |
| LIGHTER | -200 | 0.035 | 0.004 |
| LIGHTER_RH | -200 | 0.036 | 0.013 |

## 4. naive follower (enter on window open, exit at mid +5s, two taker fees)

| leg | side | trades | win rate | mean bps | total bps |
| --- | --- | ---: | ---: | ---: | ---: |
| HL | buy | 135 | 79.3% | 3.14 | 423.5 |
| HL | sell | 2702 | 20.7% | -1.41 | -3814.4 |
| LIGHTER | buy | 2 | 50.0% | 3.42 | 6.8 |
| LIGHTER | sell | 1366 | 24.9% | -1.75 | -2392.1 |
| LIGHTER_RH | buy | 4 | 100.0% | 15.55 | 62.2 |
| LIGHTER_RH | sell | 1100 | 20.1% | -2.08 | -2285.3 |


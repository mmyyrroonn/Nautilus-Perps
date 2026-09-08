# lead-lag: SNDK_HL-LIGHTER-LIGHTER_RH_20260908T144407Z_ref.csv

- rows: 1371043  span: 2026-09-08T14:44:11+00:00 .. 2026-09-08T19:59:59+00:00  rth_only: True
- threshold: 2 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0
- reference update rows: 499102   perp quote rows: 871941

## 1. edge distribution (time-weighted)

| leg | side | rows | secs | p50 | p90 | p99 | max | >0 | >2bps | >5bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 1371043 | 18949 | 3.64 | 7.08 | 9.64 | 43.41 | 90.12% | 66.80% | 32.59% |
| HL | sell | 1371043 | 18949 | -6.19 | -2.55 | -0.67 | 210.08 | 0.60% | 0.10% | 0.03% |
| LIGHTER | buy | 1371043 | 18949 | -4.16 | -1.59 | 0.82 | 35.89 | 2.04% | 0.29% | 0.03% |
| LIGHTER | sell | 1371043 | 18949 | 2.78 | 5.62 | 7.50 | 217.40 | 92.04% | 62.86% | 16.78% |
| LIGHTER_RH | buy | 1371043 | 18949 | 1.73 | 4.12 | 6.82 | 43.97 | 76.88% | 45.46% | 4.20% |
| LIGHTER_RH | sell | 1371043 | 18949 | -4.64 | -1.80 | 0.00 | 210.55 | 0.96% | 0.09% | 0.01% |

## 2. persistence of windows above 2 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 2404 | 456.7 | 497 | 7914 | 437397 |
| HL | sell | 99 | 18.8 | 120 | 466 | 1037 |
| LIGHTER | buy | 318 | 60.4 | 60 | 462 | 2291 |
| LIGHTER | sell | 8727 | 1658.0 | 115 | 2276 | 195337 |
| LIGHTER_RH | buy | 5999 | 1139.7 | 256 | 3401 | 139582 |
| LIGHTER_RH | sell | 137 | 26.0 | 79 | 237 | 1572 |

## 3. lead-lag (100 ms returns, -5s..+5s; positive lag = perp follows)

| leg | best lag ms | corr | corr at 0 |
| --- | ---: | ---: | ---: |
| HL | 300 | 0.079 | 0.031 |
| LIGHTER | -200 | 0.106 | 0.027 |
| LIGHTER_RH | -200 | 0.101 | 0.045 |

## 4. naive follower (enter on window open, exit at mid +5s, two taker fees)

| leg | side | trades | win rate | mean bps | total bps |
| --- | --- | ---: | ---: | ---: | ---: |
| HL | buy | 2400 | 22.5% | -2.10 | -5048.2 |
| HL | sell | 97 | 63.9% | 2.38 | 231.1 |
| LIGHTER | buy | 317 | 33.1% | -1.69 | -536.1 |
| LIGHTER | sell | 8722 | 39.9% | -0.65 | -5702.1 |
| LIGHTER_RH | buy | 5993 | 32.1% | -1.54 | -9236.2 |
| LIGHTER_RH | sell | 137 | 30.7% | -2.68 | -366.7 |


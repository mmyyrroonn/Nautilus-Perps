# lead-lag: NVDA_HL-LIGHTER-LIGHTER_RH_20260908T144407Z_ref.csv

- rows: 1688541  span: 2026-09-08T14:44:11+00:00 .. 2026-09-08T19:59:59+00:00  rth_only: True
- threshold: 2 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0
- reference update rows: 1413537   perp quote rows: 275004

## 1. edge distribution (time-weighted)

| leg | side | rows | secs | p50 | p90 | p99 | max | >0 | >2bps | >5bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 1688541 | 18949 | -2.01 | -0.68 | 0.65 | 258.86 | 3.21% | 0.04% | 0.01% |
| HL | sell | 1688541 | 18949 | -0.24 | 1.53 | 3.06 | 10.64 | 41.39% | 5.18% | 0.07% |
| LIGHTER | buy | 1688541 | 18949 | -10.21 | -8.76 | -7.57 | 249.17 | 0.00% | 0.00% | 0.00% |
| LIGHTER | sell | 1688541 | 18949 | 9.43 | 12.01 | 13.03 | 26.77 | 100.00% | 100.00% | 99.99% |
| LIGHTER_RH | buy | 1688541 | 18949 | -11.05 | -8.84 | -6.42 | 247.49 | 0.00% | 0.00% | 0.00% |
| LIGHTER_RH | sell | 1688541 | 18949 | 9.52 | 11.24 | 12.56 | 21.50 | 100.00% | 99.99% | 99.66% |

## 2. persistence of windows above 2 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 28 | 5.3 | 202 | 497 | 945 |
| HL | sell | 1355 | 257.4 | 278 | 1805 | 26446 |
| LIGHTER | buy | 9 | 1.7 | 91 | 116 | 140 |
| LIGHTER | sell | 16 | 3.0 | 354020 | 1200790 | 11834810 |
| LIGHTER_RH | buy | 9 | 1.7 | 91 | 116 | 140 |
| LIGHTER_RH | sell | 37 | 7.0 | 29983 | 1200782 | 5245075 |

## 3. lead-lag (100 ms returns, -5s..+5s; positive lag = perp follows)

| leg | best lag ms | corr | corr at 0 |
| --- | ---: | ---: | ---: |
| HL | 300 | 0.020 | 0.002 |
| LIGHTER | -200 | 0.037 | 0.016 |
| LIGHTER_RH | -200 | 0.026 | 0.022 |

## 4. naive follower (enter on window open, exit at mid +5s, two taker fees)

| leg | side | trades | win rate | mean bps | total bps |
| --- | --- | ---: | ---: | ---: | ---: |
| HL | buy | 28 | 46.4% | 0.76 | 21.3 |
| HL | sell | 1355 | 24.7% | -1.04 | -1411.3 |
| LIGHTER | buy | 9 | 11.1% | -0.63 | -5.6 |
| LIGHTER | sell | 16 | 31.2% | -0.26 | -4.1 |
| LIGHTER_RH | buy | 9 | 22.2% | -0.93 | -8.4 |
| LIGHTER_RH | sell | 37 | 13.5% | -1.62 | -60.0 |


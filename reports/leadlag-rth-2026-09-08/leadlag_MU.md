# lead-lag: MU_HL-LIGHTER-LIGHTER_RH_20260908T144407Z_ref.csv

- rows: 1253337  span: 2026-09-08T14:44:11+00:00 .. 2026-09-08T19:59:59+00:00  rth_only: True
- threshold: 2 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0
- reference update rows: 622408   perp quote rows: 630929

## 1. edge distribution (time-weighted)

| leg | side | rows | secs | p50 | p90 | p99 | max | >0 | >2bps | >5bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 1253337 | 18949 | -6.39 | -3.86 | -2.38 | 265.63 | 0.04% | 0.01% | 0.01% |
| HL | sell | 1253337 | 18949 | 3.49 | 5.93 | 9.51 | 22.38 | 97.14% | 79.04% | 17.80% |
| LIGHTER | buy | 1253337 | 18949 | -11.48 | -8.53 | -6.92 | 260.72 | 0.00% | 0.00% | 0.00% |
| LIGHTER | sell | 1253337 | 18949 | 10.39 | 12.47 | 14.16 | 19.79 | 99.99% | 99.97% | 99.32% |
| LIGHTER_RH | buy | 1253337 | 18949 | -9.32 | -6.49 | -4.71 | 263.27 | 0.00% | 0.00% | 0.00% |
| LIGHTER_RH | sell | 1253337 | 18949 | 6.39 | 8.79 | 10.77 | 24.75 | 99.99% | 99.88% | 77.95% |

## 2. persistence of windows above 2 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 14 | 2.7 | 154 | 505 | 799 |
| HL | sell | 2299 | 436.8 | 668 | 9793 | 706363 |
| LIGHTER | buy | 6 | 1.1 | 54 | 180 | 220 |
| LIGHTER | sell | 108 | 20.5 | 9568 | 459820 | 3910850 |
| LIGHTER_RH | buy | 6 | 1.1 | 54 | 180 | 220 |
| LIGHTER_RH | sell | 195 | 37.0 | 15399 | 127984 | 2614413 |

## 3. lead-lag (100 ms returns, -5s..+5s; positive lag = perp follows)

| leg | best lag ms | corr | corr at 0 |
| --- | ---: | ---: | ---: |
| HL | 400 | 0.030 | 0.014 |
| LIGHTER | -200 | 0.049 | -0.001 |
| LIGHTER_RH | -100 | 0.035 | 0.029 |

## 4. naive follower (enter on window open, exit at mid +5s, two taker fees)

| leg | side | trades | win rate | mean bps | total bps |
| --- | --- | ---: | ---: | ---: | ---: |
| HL | buy | 14 | 50.0% | -0.65 | -9.1 |
| HL | sell | 2292 | 19.5% | -2.09 | -4780.8 |
| LIGHTER | buy | 6 | 16.7% | -2.91 | -17.5 |
| LIGHTER | sell | 108 | 43.5% | -1.00 | -108.3 |
| LIGHTER_RH | buy | 6 | 16.7% | -3.68 | -22.1 |
| LIGHTER_RH | sell | 195 | 27.7% | -1.47 | -285.7 |


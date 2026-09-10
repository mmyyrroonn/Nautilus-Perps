# lead-lag: MU_HL-LIGHTER-LIGHTER_RH-ASTER_20260909T120046Z_ref.csv

- rows: 2354512  span: 2026-09-09T13:30:00+00:00 .. 2026-09-09T19:59:59+00:00  rth_only: True
- threshold: 2 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0, ASTER=0.9
- reference update rows: 854350   perp quote rows: 1500162

## 1. edge distribution (time-weighted)

| leg | side | rows | secs | p50 | p90 | p99 | max | >0 | >2bps | >5bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 2354512 | 23400 | -3.63 | -1.49 | 0.32 | 16.01 | 1.34% | 0.30% | 0.09% |
| HL | sell | 2354512 | 23400 | 0.66 | 4.27 | 7.69 | 194.96 | 62.64% | 31.37% | 5.06% |
| LIGHTER | buy | 2354512 | 23400 | -9.89 | -7.65 | -6.03 | 9.12 | 0.03% | 0.01% | 0.00% |
| LIGHTER | sell | 2354512 | 23400 | 8.59 | 11.23 | 14.72 | 201.29 | 99.94% | 99.88% | 97.72% |
| LIGHTER_RH | buy | 2354512 | 23400 | -7.52 | -5.18 | -3.31 | 8.74 | 0.05% | 0.02% | 0.01% |
| LIGHTER_RH | sell | 2354512 | 23400 | 4.73 | 7.32 | 9.53 | 197.86 | 99.83% | 98.44% | 44.32% |
| ASTER | buy | 2354512 | 23400 | -10.81 | -8.76 | -7.20 | 7.52 | 0.02% | 0.01% | 0.00% |
| ASTER | sell | 2354512 | 23400 | 6.44 | 9.19 | 11.29 | 199.44 | 99.07% | 97.15% | 78.38% |

## 2. persistence of windows above 2 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 235 | 36.2 | 188 | 647 | 2767 |
| HL | sell | 3364 | 517.5 | 440 | 3323 | 446847 |
| LIGHTER | buy | 15 | 2.3 | 32 | 550 | 1290 |
| LIGHTER | sell | 401 | 61.7 | 2556 | 73707 | 7255131 |
| LIGHTER_RH | buy | 29 | 4.5 | 98 | 312 | 1702 |
| LIGHTER_RH | sell | 1616 | 248.6 | 2086 | 26938 | 2030928 |
| ASTER | buy | 43 | 6.6 | 41 | 92 | 205 |
| ASTER | sell | 30806 | 4739.4 | 45 | 658 | 1280796 |

## 3. lead-lag (100 ms returns, -5s..+5s; positive lag = perp follows)

| leg | best lag ms | corr | corr at 0 |
| --- | ---: | ---: | ---: |
| HL | 500 | 0.076 | 0.011 |
| LIGHTER | 0 | 0.109 | 0.109 |
| LIGHTER_RH | 0 | 0.090 | 0.090 |
| ASTER | 100 | 0.067 | 0.066 |

## 4. naive follower (enter on window open, exit at mid +5s, two taker fees)

| leg | side | trades | win rate | mean bps | total bps |
| --- | --- | ---: | ---: | ---: | ---: |
| HL | buy | 235 | 64.3% | 3.47 | 815.4 |
| HL | sell | 3364 | 32.2% | -1.59 | -5363.2 |
| LIGHTER | buy | 15 | 60.0% | 6.54 | 98.1 |
| LIGHTER | sell | 401 | 31.7% | -2.55 | -1023.7 |
| LIGHTER_RH | buy | 29 | 82.8% | 9.41 | 272.8 |
| LIGHTER_RH | sell | 1612 | 29.9% | -2.29 | -3691.8 |
| ASTER | buy | 43 | 51.2% | 9.33 | 401.3 |
| ASTER | sell | 30753 | 23.1% | -3.35 | -103128.7 |


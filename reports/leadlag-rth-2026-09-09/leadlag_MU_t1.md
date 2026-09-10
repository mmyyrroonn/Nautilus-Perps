# lead-lag: MU_HL-LIGHTER-LIGHTER_RH-ASTER_20260909T120046Z_ref.csv

- rows: 2354512  span: 2026-09-09T13:30:00+00:00 .. 2026-09-09T19:59:59+00:00  rth_only: True
- threshold: 1 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0, ASTER=0.9
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

## 2. persistence of windows above 1 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 394 | 60.6 | 240 | 693 | 2767 |
| HL | sell | 4393 | 675.8 | 451 | 3435 | 828290 |
| LIGHTER | buy | 15 | 2.3 | 59 | 700 | 1686 |
| LIGHTER | sell | 225 | 34.6 | 4803 | 111816 | 7634589 |
| LIGHTER_RH | buy | 42 | 6.5 | 92 | 304 | 2046 |
| LIGHTER_RH | sell | 606 | 93.2 | 4182 | 73598 | 4258314 |
| ASTER | buy | 59 | 9.1 | 40 | 94 | 684 |
| ASTER | sell | 19687 | 3028.8 | 46 | 1148 | 1280796 |

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
| HL | buy | 394 | 61.9% | 2.59 | 1020.6 |
| HL | sell | 4390 | 29.1% | -1.74 | -7640.7 |
| LIGHTER | buy | 15 | 33.3% | -1.10 | -16.5 |
| LIGHTER | sell | 225 | 29.8% | -3.04 | -684.9 |
| LIGHTER_RH | buy | 42 | 81.0% | 8.56 | 359.7 |
| LIGHTER_RH | sell | 606 | 30.0% | -2.93 | -1774.7 |
| ASTER | buy | 59 | 50.8% | 8.40 | 495.5 |
| ASTER | sell | 19634 | 22.9% | -3.53 | -69301.6 |


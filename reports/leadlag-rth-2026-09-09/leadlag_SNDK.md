# lead-lag: SNDK_HL-LIGHTER-LIGHTER_RH-ASTER_20260909T120046Z_ref.csv

- rows: 2410332  span: 2026-09-09T13:30:00+00:00 .. 2026-09-09T19:59:59+00:00  rth_only: True
- threshold: 2 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0, ASTER=0.9
- reference update rows: 633513   perp quote rows: 1776819

## 1. edge distribution (time-weighted)

| leg | side | rows | secs | p50 | p90 | p99 | max | >0 | >2bps | >5bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 2410332 | 23400 | 0.48 | 4.87 | 7.80 | 30.49 | 56.54% | 33.41% | 9.14% |
| HL | sell | 2410332 | 23400 | -3.02 | -0.25 | 2.06 | 83.60 | 7.29% | 1.03% | 0.22% |
| LIGHTER | buy | 2410332 | 23400 | -7.56 | -3.32 | -0.98 | 28.24 | 0.33% | 0.07% | 0.03% |
| LIGHTER | sell | 2410332 | 23400 | 6.12 | 9.71 | 11.88 | 92.39 | 97.83% | 89.01% | 60.20% |
| LIGHTER_RH | buy | 2410332 | 23400 | -1.02 | 2.76 | 4.90 | 27.84 | 38.33% | 17.35% | 0.88% |
| LIGHTER_RH | sell | 2410332 | 23400 | -1.67 | 1.56 | 4.08 | 84.22 | 28.49% | 6.48% | 0.52% |
| ASTER | buy | 2410332 | 23400 | -3.27 | 1.13 | 3.12 | 25.39 | 21.36% | 4.23% | 0.11% |
| ASTER | sell | 2410332 | 23400 | -0.87 | 2.73 | 6.39 | 84.72 | 42.33% | 18.56% | 2.35% |

## 2. persistence of windows above 2 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 3245 | 499.2 | 513 | 6420 | 82859 |
| HL | sell | 747 | 114.9 | 217 | 688 | 4567 |
| LIGHTER | buy | 191 | 29.4 | 20 | 160 | 3638 |
| LIGHTER | sell | 7495 | 1153.1 | 104 | 2382 | 1319510 |
| LIGHTER_RH | buy | 5208 | 801.2 | 165 | 2098 | 29869 |
| LIGHTER_RH | sell | 3136 | 482.5 | 101 | 1280 | 25142 |
| ASTER | buy | 5020 | 772.3 | 49 | 449 | 10890 |
| ASTER | sell | 9652 | 1484.9 | 59 | 997 | 27581 |

## 3. lead-lag (100 ms returns, -5s..+5s; positive lag = perp follows)

| leg | best lag ms | corr | corr at 0 |
| --- | ---: | ---: | ---: |
| HL | 500 | 0.193 | 0.026 |
| LIGHTER | -100 | 0.238 | 0.209 |
| LIGHTER_RH | 0 | 0.201 | 0.201 |
| ASTER | 0 | 0.245 | 0.245 |

## 4. naive follower (enter on window open, exit at mid +5s, two taker fees)

| leg | side | trades | win rate | mean bps | total bps |
| --- | --- | ---: | ---: | ---: | ---: |
| HL | buy | 3245 | 34.5% | -1.35 | -4375.2 |
| HL | sell | 747 | 59.0% | 1.26 | 944.3 |
| LIGHTER | buy | 191 | 45.5% | -1.48 | -281.8 |
| LIGHTER | sell | 7484 | 42.4% | -0.67 | -5036.0 |
| LIGHTER_RH | buy | 5202 | 38.5% | -1.18 | -6116.4 |
| LIGHTER_RH | sell | 3136 | 39.8% | -0.76 | -2373.5 |
| ASTER | buy | 5016 | 26.1% | -3.10 | -15562.2 |
| ASTER | sell | 9652 | 19.9% | -3.24 | -31315.5 |


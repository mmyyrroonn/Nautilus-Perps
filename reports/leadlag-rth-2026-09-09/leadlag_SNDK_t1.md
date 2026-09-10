# lead-lag: SNDK_HL-LIGHTER-LIGHTER_RH-ASTER_20260909T120046Z_ref.csv

- rows: 2410332  span: 2026-09-09T13:30:00+00:00 .. 2026-09-09T19:59:59+00:00  rth_only: True
- threshold: 1 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0, ASTER=0.9
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

## 2. persistence of windows above 1 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 3589 | 552.2 | 489 | 6341 | 171872 |
| HL | sell | 1257 | 193.4 | 258 | 825 | 7861 |
| LIGHTER | buy | 398 | 61.2 | 31 | 184 | 3721 |
| LIGHTER | sell | 5720 | 880.0 | 116 | 3153 | 1744146 |
| LIGHTER_RH | buy | 5229 | 804.5 | 200 | 3510 | 81104 |
| LIGHTER_RH | sell | 4252 | 654.2 | 138 | 2259 | 52117 |
| ASTER | buy | 8417 | 1294.9 | 64 | 740 | 23394 |
| ASTER | sell | 11034 | 1697.5 | 74 | 1393 | 57549 |

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
| HL | buy | 3589 | 33.5% | -1.30 | -4665.5 |
| HL | sell | 1257 | 52.3% | 0.47 | 591.8 |
| LIGHTER | buy | 398 | 44.7% | -1.54 | -612.2 |
| LIGHTER | sell | 5712 | 42.2% | -0.65 | -3735.5 |
| LIGHTER_RH | buy | 5228 | 38.0% | -1.09 | -5719.5 |
| LIGHTER_RH | sell | 4252 | 36.7% | -1.10 | -4667.5 |
| ASTER | buy | 8408 | 24.6% | -3.09 | -25955.8 |
| ASTER | sell | 11034 | 20.2% | -3.26 | -36025.4 |


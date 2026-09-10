# lead-lag: SPCX_HL-LIGHTER-LIGHTER_RH-ASTER_20260909T120046Z_ref.csv

- rows: 2327092  span: 2026-09-09T13:30:00+00:00 .. 2026-09-09T19:59:59+00:00  rth_only: True
- threshold: 2 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0, ASTER=0.9
- reference update rows: 1101949   perp quote rows: 1225143

## 1. edge distribution (time-weighted)

| leg | side | rows | secs | p50 | p90 | p99 | max | >0 | >2bps | >5bps |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 2327092 | 23400 | 1.50 | 3.81 | 6.63 | 392.33 | 76.67% | 42.11% | 2.63% |
| HL | sell | 2327092 | 23400 | -4.27 | -1.23 | 3.45 | 41.24 | 5.79% | 2.12% | 0.54% |
| LIGHTER | buy | 2327092 | 23400 | -5.47 | -2.63 | 1.31 | 385.48 | 1.55% | 0.66% | 0.21% |
| LIGHTER | sell | 2327092 | 23400 | 4.44 | 8.25 | 15.84 | 60.06 | 96.91% | 86.97% | 40.17% |
| LIGHTER_RH | buy | 2327092 | 23400 | -0.68 | 1.69 | 6.25 | 389.70 | 33.49% | 8.34% | 1.64% |
| LIGHTER_RH | sell | 2327092 | 23400 | -1.68 | 1.36 | 8.02 | 38.15 | 20.27% | 7.55% | 2.77% |
| ASTER | buy | 2327092 | 23400 | -11.49 | -7.29 | -0.57 | 380.35 | 0.82% | 0.29% | 0.06% |
| ASTER | sell | 2327092 | 23400 | 8.67 | 14.28 | 22.86 | 47.40 | 97.52% | 96.42% | 85.44% |

## 2. persistence of windows above 2 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 5003 | 769.7 | 543 | 4710 | 124303 |
| HL | sell | 768 | 118.2 | 259 | 1197 | 18089 |
| LIGHTER | buy | 842 | 129.5 | 50 | 404 | 8903 |
| LIGHTER | sell | 4111 | 632.5 | 152 | 6891 | 1241421 |
| LIGHTER_RH | buy | 2543 | 391.2 | 165 | 2045 | 44533 |
| LIGHTER_RH | sell | 1626 | 250.2 | 170 | 2104 | 176500 |
| ASTER | buy | 596 | 91.7 | 50 | 216 | 2596 |
| ASTER | sell | 2347 | 361.1 | 94 | 1947 | 3885158 |

## 3. lead-lag (100 ms returns, -5s..+5s; positive lag = perp follows)

| leg | best lag ms | corr | corr at 0 |
| --- | ---: | ---: | ---: |
| HL | 700 | 0.057 | 0.004 |
| LIGHTER | 0 | 0.073 | 0.073 |
| LIGHTER_RH | 0 | 0.053 | 0.053 |
| ASTER | 100 | 0.090 | 0.063 |

## 4. naive follower (enter on window open, exit at mid +5s, two taker fees)

| leg | side | trades | win rate | mean bps | total bps |
| --- | --- | ---: | ---: | ---: | ---: |
| HL | buy | 4997 | 23.3% | -1.55 | -7764.5 |
| HL | sell | 768 | 54.2% | 0.95 | 728.3 |
| LIGHTER | buy | 842 | 47.0% | -2.52 | -2124.3 |
| LIGHTER | sell | 4102 | 38.9% | -0.97 | -3962.0 |
| LIGHTER_RH | buy | 2540 | 43.0% | -0.13 | -317.9 |
| LIGHTER_RH | sell | 1626 | 48.3% | -0.13 | -216.6 |
| ASTER | buy | 596 | 39.6% | -5.94 | -3540.2 |
| ASTER | sell | 2345 | 17.1% | -2.71 | -6352.0 |


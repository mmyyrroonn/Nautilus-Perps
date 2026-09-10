# lead-lag: SPCX_HL-LIGHTER-LIGHTER_RH-ASTER_20260909T120046Z_ref.csv

- rows: 2327092  span: 2026-09-09T13:30:00+00:00 .. 2026-09-09T19:59:59+00:00  rth_only: True
- threshold: 1 bps   hold: 5s   fees(bps): HL=0.9, LIGHTER=0, LIGHTER_RH=0, ASTER=0.9
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

## 2. persistence of windows above 1 bps

| leg | side | windows | per hour | dur p50 ms | dur p90 ms | dur max ms |
| --- | --- | ---: | ---: | ---: | ---: | ---: |
| HL | buy | 4599 | 707.5 | 717 | 6673 | 213397 |
| HL | sell | 1104 | 169.8 | 299 | 1331 | 26199 |
| LIGHTER | buy | 1141 | 175.5 | 59 | 518 | 8909 |
| LIGHTER | sell | 2714 | 417.5 | 180 | 8930 | 1315666 |
| LIGHTER_RH | buy | 4905 | 754.6 | 171 | 2412 | 64460 |
| LIGHTER_RH | sell | 2571 | 395.5 | 208 | 2760 | 206751 |
| ASTER | buy | 882 | 135.7 | 50 | 259 | 3822 |
| ASTER | sell | 1078 | 165.8 | 93 | 6492 | 6274974 |

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
| HL | buy | 4594 | 22.0% | -1.86 | -8566.4 |
| HL | sell | 1104 | 51.1% | 0.62 | 681.6 |
| LIGHTER | buy | 1141 | 48.4% | -0.93 | -1059.1 |
| LIGHTER | sell | 2709 | 39.7% | -1.13 | -3068.1 |
| LIGHTER_RH | buy | 4900 | 35.2% | -0.43 | -2096.3 |
| LIGHTER_RH | sell | 2571 | 45.8% | -0.20 | -508.3 |
| ASTER | buy | 882 | 40.0% | -3.98 | -3513.7 |
| ASTER | sell | 1078 | 29.2% | -2.65 | -2852.6 |


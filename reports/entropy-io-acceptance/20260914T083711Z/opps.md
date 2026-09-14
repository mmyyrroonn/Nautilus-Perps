# Opportunity analysis - stamp 20260914T083711Z

source `reports\entropy-io-acceptance\20260914T083711Z`  |  symbols GPRO, SNDK  |  gap 2s, hold 30s, min-usd 1000

## GPRO  [ENTROPY, ASTER]

window 2026-09-14T08:37:24+00:00 .. 2026-09-14T08:37:26+00:00  (0.00 h)  |  samples 4  hits 0  episodes 0  depth rows 232 (9 empty)

### 1. Basis (1 s samples, gross bps)

| direction     |   n | median |    p95 |    max |    min | fee+res | net>0 |
| ------------- | --: | -----: | -----: | -----: | -----: | ------: | ----: |
| ASTER>ENTROPY |   2 | -23.63 | -22.62 | -22.50 | -24.75 |    6.80 | 0.00% |
| ENTROPY>ASTER |   2 | -47.25 | -47.25 | -47.25 | -47.26 |    6.80 | 0.00% |

### 2. Episodes (hits merged, gap <= 2 s)

_no net-positive rows in the window: no episodes, no round trips_

### 5. Funding carry (raw -> bps/h; HL + Entropy fraction/h, Lighter percent/h, Aster fraction per the instrument's 1/4/8 h interval)

| venue   |   raw median | settle h |  bps/h |  %/yr | distinct raw |
| ------- | -----------: | -------: | -----: | ----: | -----------: |
| ASTER   |            0 |        8 | 0.0000 |   0.0 |            1 |
| ENTROPY | 0.0002473804 |        1 | 2.4738 | 216.7 |            1 |

| pair          | short leg     | long leg   | carry bps/h |  %/yr | round-trip fee bps | hours to b/e | samples |
| ------------- | ------------- | ---------- | ----------: | ----: | -----------------: | -----------: | ------: |
| ASTER/ENTROPY | short ENTROPY | long ASTER |      2.4738 | 216.7 |               3.60 |          1.5 |       2 |

> **FLAG** ASTER funding is exactly 0 in every sample (feed may be idle)

## SNDK  [HL, ENTROPY, ASTER]

window 2026-09-14T08:37:15+00:00 .. 2026-09-14T08:39:11+00:00  (0.03 h)  |  samples 614  hits 5331  episodes 5  depth rows 348 (0 empty)

### 1. Basis (1 s samples, gross bps)

| direction     |   n | median |    p95 |    max |    min | fee+res |  net>0 |
| ------------- | --: | -----: | -----: | -----: | -----: | ------: | -----: |
| ASTER>ENTROPY | 100 |   8.13 |   9.71 |  12.60 |   5.46 |    6.80 | 87.00% |
| ASTER>HL      | 103 |   9.45 |  10.74 |  11.38 |   6.68 |    6.80 | 97.09% |
| ENTROPY>ASTER | 100 | -12.18 | -10.99 |  -9.32 | -16.07 |    6.80 |  0.00% |
| ENTROPY>HL    | 104 |   0.00 |   1.29 |   2.57 |  -3.21 |    6.80 |  0.00% |
| HL>ASTER      | 103 | -12.86 | -11.64 | -10.29 | -14.78 |    6.80 |  0.00% |
| HL>ENTROPY    | 104 |  -1.93 |  -0.64 |   1.93 |  -4.50 |    6.80 |  0.00% |

### 2. Episodes (hits merged, gap <= 2 s)

| direction     | episodes | eps/h | net+ s | % of window | dur med | dur max | best net med | best net max | raw hits |
| ------------- | -------: | ----: | -----: | ----------: | ------: | ------: | -----------: | -----------: | -------: |
| ASTER>ENTROPY |        2 |  61.9 |  113.5 |      97.67% |   56.76 |   73.46 |         5.83 |         5.86 |     2303 |
| ASTER>HL      |        3 |  92.9 |  110.6 |      95.19% |   37.57 |   72.65 |         5.21 |         5.48 |     3028 |

_a single-row episode has duration 0 s: the book was net-positive on one evaluation only_

### 3. Capacity per episode (USD, min-usd 1000)

| direction     | tob med | tob p90 | tob epi-med | depth med | depth p90 | tob>=1000 | depth>=1000 | depth n | bucket        | skipped |
| ------------- | ------: | ------: | ----------: | --------: | --------: | --------: | ----------: | ------: | ------------- | ------- |
| ASTER>ENTROPY |     594 |    1.1k |         306 |      8.9k |     13.6k |         1 |           2 |       2 | 5bps:2        | -       |
| ASTER>HL      |    4.4k |   14.3k |        4.5k |     64.4k |     69.3k |         3 |           3 |       3 | 2bps:1 5bps:2 | -       |

_tob = min(sell_bid_size, buy_ask_size) x mid at the episode's first row; depth = min(sell bid_usd_N, buy ask_usd_N) at the episode start, N = largest of 2/5/10 bps at or below the episode's best net; tob-only = best net < 2 bps, no-depth = no depth row within 2 s_

### 4. Round trip (taker in / taker out, hold <= 30 s)

| direction     | trades | pos% | pnl med | pnl mean | pnl total | hold med s | USD pnl | forced exits |
| ------------- | -----: | ---: | ------: | -------: | --------: | ---------: | ------: | -----------: |
| ASTER>ENTROPY |      2 | 0.0% |  -10.85 |   -10.85 |     -21.7 |       29.7 |      -2 |            2 |
| ASTER>HL      |      2 | 0.0% |  -12.68 |   -12.68 |     -25.4 |       29.7 |     -28 |            2 |

_entry = episode's first hit (gross - taker both legs - 5 bps reserve); exit = first reverse 1 s sample whose total pnl >= 0, else the last sample within the hold; USD pnl = sum(pnl_bps x tob capacity / 1e4); unresolved (no reverse sample in the window, excluded): 1_

### 5. Funding carry (raw -> bps/h; HL + Entropy fraction/h, Lighter percent/h, Aster fraction per the instrument's 1/4/8 h interval)

| venue   |   raw median | settle h |  bps/h | %/yr | distinct raw |
| ------- | -----------: | -------: | -----: | ---: | -----------: |
| ASTER   |   0.00032661 |        8 | 0.4083 | 35.8 |            3 |
| ENTROPY | 0.0000017352 |        1 | 0.0174 |  1.5 |           24 |
| HL      |   0.00000625 |        1 | 0.0625 |  5.5 |            1 |

| pair          | short leg   | long leg     | carry bps/h | %/yr | round-trip fee bps | hours to b/e | samples |
| ------------- | ----------- | ------------ | ----------: | ---: | -----------------: | -----------: | ------: |
| ASTER/ENTROPY | short ASTER | long ENTROPY |      0.3901 | 34.2 |               3.60 |          9.2 |      99 |
| ASTER/HL      | short ASTER | long HL      |      0.3458 | 30.3 |               3.60 |         10.4 |     102 |
| ENTROPY/HL    | short HL    | long ENTROPY |      0.0451 |  4.0 |               3.60 |         79.7 |     104 |

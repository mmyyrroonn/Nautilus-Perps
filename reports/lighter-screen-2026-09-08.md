# Lighter maker-spread screen

- Sampling window (UTC): **2026-09-08 09:57:27** to **2026-09-08 10:03:54** (6.4 min wall clock)
- Passes: 1 broad pass over every market + 5 deep passes, 60s apart, `--depth 10`
- **Low-activity hours.** The window above is 09:00 UTC, about 04:00 US Eastern - the middle of the US night. US equities are closed, equity-linked and stock-perp flow is thin, and crypto books are wider than during the US/EU sessions. Treat the absolute spread numbers as an upper bound on what a US-session recording would show; the 24h volume and trade-count columns are session-independent and are the reliable half of the table.
- Fees assumed (bps, taker): Aster 0.90, Hyperliquid main 4.50, Hyperliquid `xyz` HIP-3 dex 4.50 (assumption: the HIP-3 deployer fee share is not exposed by the info API, so the real cost there is >= this number).
- Lighter maker fee 0.0 bps, taker 0 bps (both confirmed in the `orderBooks` payload).
- `room_bps = lighter_spread - (hedge_spread + hedge_taker_fee)`: the gross width available to a two-sided maker on Lighter who hedges every fill as a taker. A full round trip earns roughly the whole number minus adverse selection and inventory drift.
- `flow proxy = min(lighter 24h vol, hedge 24h vol)` - both legs must be liquid for the trade to exist at size.

## Coverage

| item | count |
|---|---|
| Lighter `LIGHTER` perp markets (active) | 216 |
| ...`LIGHTER` markets skipped as inactive/delisted | 17 (SAMSUNG, HYUNDAI, YZY, MKR, SKHYNIX, SOXX, SPACEX, HANMI, DUSK, LAUNCHCOIN, MAGS, DIA, PIPPIN, AI16Z, KRCOMP, BIRB, 1000TOSHI) |
| Lighter `LIGHTER_RH` perp markets (active) | 57 |
| ...with an Aster match | 223 |
| ...with a Hyperliquid main-dex match | 111 |
| ...with a Hyperliquid `xyz` HIP-3 match | 84 |
| ...with no match on any hedge venue | 38 |
| passed both volume filters | 108 |
| filtered out | 127 |
| deep-sampled Lighter markets | 113 |
| HTTP requests `aster` (retries) | 14 (2) |
| HTTP requests `hl` (retries) | 467 (0) |
| HTTP requests `lighter_main` (retries) | 497 (15) |
| HTTP requests `lighter_rh` (retries) | 343 (0) |

## Ranked candidates (passed filters: Lighter 24h vol >= 200.0k, hedge 24h vol >= 1.00M)

| # | Lighter mkt | L spread bps | n | L TOB bid $ | L TOB ask $ | L 24h vol | L trades | hedge | H spread bps | fee bps | **room bps** | H 24h vol | flow proxy |
|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|
| 1 | ANSEM | 58.22 | 6 | 298 | 266 | 696.7k | 14,699 | ASTER:ANSEMUSDT | 6.85 | 0.90 | **+50.47** | 1.18M | 696.7k |
| 2 | PONS (RH) | 35.74 | 6 | 397 | 100 | 807.8k | 6,179 | HL:PONS | 4.01 | 4.50 | **+27.23** | 105.54M | 807.8k |
| 3 | PONS | 32.01 | 6 | 252 | 250 | 3.72M | 16,812 | HL:PONS | 4.01 | 4.50 | **+23.50** | 105.54M | 3.72M |
| 4 | ORCL | 24.31 | 6 | 25.0k | 25.0k | 395.7k | 152 | HL_XYZ:xyz:ORCL | 1.22 | 4.50 | **+18.60** | 11.32M | 395.7k |
| 5 | XPL | 24.40 | 6 | 11.0k | 1.9k | 1.18M | 1,584 | HL:XPL | 2.70 | 4.50 | **+17.20** | 12.67M | 1.18M |
| 6 | ZRO | 16.40 | 6 | 922 | 848 | 206.7k | 682 | HL:ZRO | 2.64 | 4.50 | **+9.26** | 3.22M | 206.7k |
| 7 | MON | 15.46 | 6 | 4.5k | 117 | 698.4k | 1,554 | HL:MON | 1.74 | 4.50 | **+9.22** | 9.65M | 698.4k |
| 8 | TIA | 13.08 | 6 | 729 | 1.3k | 354.2k | 1,122 | HL:TIA | 0.82 | 4.50 | **+7.76** | 1.82M | 354.2k |
| 9 | ASTER | 9.35 | 6 | 3.1k | 778 | 709.8k | 2,392 | ASTER:ASTERUSDT | 1.30 | 0.90 | **+7.15** | 87.72M | 709.8k |
| 10 | FIL | 11.55 | 6 | 565 | 2.5k | 260.3k | 927 | HL:FIL | 0.87 | 4.50 | **+6.18** | 2.34M | 260.3k |
| 11 | VVV (RH) | 11.59 | 6 | 209 | 1.9k | 387.3k | 683 | HL:VVV | 1.11 | 4.50 | **+5.98** | 10.64M | 387.3k |
| 12 | ONDO | 10.86 | 6 | 9.3k | 1.5k | 414.2k | 752 | HL:ONDO | 1.05 | 4.50 | **+5.32** | 9.88M | 414.2k |
| 13 | SUI (RH) | 8.34 | 6 | 4.0k | 5.5k | 411.3k | 1,128 | ASTER:SUIUSDT | 2.41 | 0.90 | **+5.02** | 1.23M | 411.3k |
| 14 | EIGEN | 13.85 | 6 | 705 | 2.2k | 294.8k | 2,734 | HL:EIGEN | 4.54 | 4.50 | **+4.81** | 1.10M | 294.8k |
| 15 | MSFT | 10.36 | 6 | 330 | 2.0k | 8.67M | 1,033 | HL_XYZ:xyz:MSFT | 1.41 | 4.50 | **+4.45** | 8.59M | 8.59M |
| 16 | CRWV (RH) | 9.94 | 6 | 3.0k | 4.0k | 203.0k | 264 | HL_XYZ:xyz:CRWV | 1.27 | 4.50 | **+4.17** | 3.01M | 203.0k |
| 17 | JTO | 9.51 | 6 | 150 | 152 | 215.4k | 559 | HL:JTO | 1.57 | 4.50 | **+3.44** | 1.49M | 215.4k |
| 18 | XRP (RH) | 4.29 | 6 | 3.0k | 867 | 617.4k | 2,130 | ASTER:XRPUSDT | 0.72 | 0.90 | **+2.67** | 30.87M | 617.4k |
| 19 | DOGE | 4.64 | 1 | 150 | 1.1k | 749.5k | 1,384 | ASTER:DOGEUSDT | 1.11 | 0.90 | **+2.63** | 19.69M | 749.5k |
| 20 | VVV | 7.75 | 6 | 2.2k | 5.5k | 1.58M | 3,014 | HL:VVV | 1.11 | 4.50 | **+2.15** | 10.64M | 1.58M |
| 21 | ARB | 8.01 | 6 | 150 | 150 | 3.72M | 10,696 | HL:ARB | 1.42 | 4.50 | **+2.08** | 35.20M | 3.72M |
| 22 | PLTR (RH) | 7.45 | 6 | 250 | 2.0k | 515.4k | 656 | HL_XYZ:xyz:PLTR | 1.15 | 4.50 | **+1.80** | 8.60M | 515.4k |
| 23 | STRK | 9.36 | 1 | 1.8k | 1.4k | 313.4k | 468 | HL:STRK | 3.12 | 4.50 | **+1.75** | 1.30M | 313.4k |
| 24 | DASH | 7.24 | 6 | 4.3k | 1.3k | 395.0k | 764 | HL:DASH | 1.03 | 4.50 | **+1.70** | 5.20M | 395.0k |
| 25 | ADA | 7.03 | 6 | 2.7k | 3.9k | 285.2k | 505 | HL:ADA | 0.91 | 4.50 | **+1.62** | 7.11M | 285.2k |
| 26 | SUI | 4.83 | 1 | 64 | 150 | 924.8k | 3,050 | ASTER:SUIUSDT | 2.41 | 0.90 | **+1.51** | 1.23M | 924.8k |
| 27 | BNB | 2.38 | 6 | 5.9k | 14.2k | 3.47M | 5,102 | ASTER:BNBUSDT | 0.13 | 0.90 | **+1.34** | 21.79M | 3.47M |
| 28 | SKHY (RH) | 4.13 | 6 | 4.5k | 3.5k | 1.70M | 2,626 | ASTER:SKHYUSDT | 1.93 | 0.90 | **+1.30** | 1.11M | 1.11M |
| 29 | NEAR | 6.82 | 6 | 465 | 1.7k | 4.75M | 10,090 | HL:NEAR | 1.50 | 4.50 | **+0.82** | 34.17M | 4.75M |
| 30 | XMR | 5.50 | 6 | 2.1k | 6.4k | 1.96M | 4,549 | HL:XMR | 0.19 | 4.50 | **+0.81** | 23.68M | 1.96M |
| 31 | MORPHO | 6.96 | 1 | 2.6k | 508 | 297.0k | 875 | HL:MORPHO | 1.66 | 4.50 | **+0.80** | 2.74M | 297.0k |
| 32 | TAO | 4.30 | 1 | 250 | 3.6k | 1.39M | 4,167 | ASTER:TAOUSDT | 2.73 | 0.90 | **+0.67** | 1.10M | 1.10M |
| 33 | AERO | 8.64 | 1 | 5.2k | 4.9k | 1.14M | 2,286 | HL:AERO | 3.57 | 4.50 | **+0.57** | 14.31M | 1.14M |
| 34 | LTC | 5.20 | 1 | 2.5k | 2.5k | 352.6k | 781 | HL:LTC | 0.18 | 4.50 | **+0.52** | 15.52M | 352.6k |
| 35 | GRAM | 6.73 | 6 | 1.1k | 995 | 1.68M | 1,934 | HL:GRAM | 1.79 | 4.50 | **+0.44** | 3.17M | 1.68M |
| 36 | SOL (RH) | 2.17 | 6 | 10.1k | 811 | 6.08M | 16,025 | ASTER:SOLUSDT | 0.97 | 0.90 | **+0.31** | 86.53M | 6.08M |
| 37 | PENGU | 7.17 | 1 | 1.9k | 5.1k | 535.2k | 922 | HL:PENGU | 2.39 | 4.50 | **+0.28** | 4.23M | 535.2k |
| 38 | ETH (RH) | 1.20 | 6 | 1.8k | 1.8k | 52.23M | 73,615 | ASTER:ETHUSDT | 0.04 | 0.90 | **+0.26** | 279.27M | 52.23M |
| 39 | HYPE (RH) | 2.61 | 6 | 1000 | 1.5k | 9.79M | 17,190 | ASTER:HYPEUSDT | 1.54 | 0.90 | **+0.17** | 20.42M | 9.79M |
| 40 | XRP | 1.76 | 6 | 250 | 799 | 2.08M | 4,989 | ASTER:XRPUSDT | 0.72 | 0.90 | **+0.15** | 30.87M | 2.08M |
| 41 | XAG (RH) | 2.55 | 6 | 10.0k | 2.0k | 11.16M | 14,785 | ASTER:XAGUSDT | 1.51 | 0.90 | **+0.14** | 3.39M | 3.39M |
| 42 | SNDK (RH) | 3.59 | 6 | 1.0k | 1.8k | 8.91M | 25,924 | ASTER:SNDKUSD1 | 2.57 | 0.90 | **+0.12** | 9.68M | 8.91M |
| 43 | FARTCOIN | 7.41 | 1 | 976 | 1.1k | 449.4k | 754 | HL:FARTCOIN | 2.86 | 4.50 | **+0.06** | 15.08M | 449.4k |
| 44 | ICP | 6.45 | 1 | 3.9k | 718 | 489.8k | 1,216 | HL:ICP | 1.93 | 4.50 | **+0.01** | 4.48M | 489.8k |
| 45 | ZEC (RH) | 3.74 | 6 | 2.9k | 4.5k | 3.04M | 8,926 | ASTER:ZECUSDT | 2.83 | 0.90 | **+0.01** | 15.14M | 3.04M |
| 46 | APT | 7.50 | 1 | 3.8k | 2.0k | 357.5k | 769 | HL:APT | 3.00 | 4.50 | **-0.00** | 1.48M | 357.5k |
| 47 | XAU | 0.81 | 6 | 1.4k | 4.5k | 40.34M | 35,735 | ASTER:XAUUSD1 | 0.02 | 0.90 | **-0.12** | 62.57M | 40.34M |
| 48 | WLD | 4.85 | 6 | 1.5k | 100 | 2.70M | 5,198 | HL:WLD | 0.52 | 4.50 | **-0.17** | 42.00M | 2.70M |
| 49 | NEAR (RH) | 5.78 | 6 | 1.6k | 2.0k | 426.5k | 1,538 | HL:NEAR | 1.50 | 4.50 | **-0.22** | 34.17M | 426.5k |
| 50 | SPCX (RH) | 1.34 | 6 | 2.5k | 1.5k | 4.18M | 10,573 | ASTER:SPCXUSD1 | 0.67 | 0.90 | **-0.23** | 9.54M | 4.18M |
| 51 | XLM | 5.75 | 1 | 4.6k | 5.5k | 798.2k | 955 | HL:XLM | 1.57 | 4.50 | **-0.32** | 2.48M | 798.2k |
| 52 | BTC (RH) | 0.58 | 6 | 47 | 215 | 65.46M | 119,875 | ASTER:BTCUSDT | 0.01 | 0.90 | **-0.33** | 564.87M | 65.46M |
| 53 | XAU (RH) | 0.56 | 6 | 2.5k | 5.3k | 26.36M | 24,968 | ASTER:XAUUSD1 | 0.02 | 0.90 | **-0.37** | 62.57M | 26.36M |
| 54 | BTC | 0.48 | 6 | 2.1k | 2.4k | 422.33M | 524,667 | ASTER:BTCUSDT | 0.01 | 0.90 | **-0.44** | 564.87M | 422.33M |
| 55 | SPCX | 1.00 | 6 | 20.5k | 16.0k | 2.85M | 11,751 | ASTER:SPCXUSD1 | 0.67 | 0.90 | **-0.57** | 9.54M | 2.85M |
| 56 | UNI | 4.28 | 6 | 150 | 466 | 1.46M | 3,112 | HL:UNI | 0.35 | 4.50 | **-0.57** | 35.57M | 1.46M |
| 57 | LIT (RH) | 3.85 | 6 | 1.7k | 1.5k | 7.76M | 26,009 | ASTER:LITUSDT | 3.53 | 0.90 | **-0.58** | 3.69M | 3.69M |
| 58 | ETH | 0.36 | 6 | 1.4k | 555 | 193.14M | 211,074 | ASTER:ETHUSDT | 0.04 | 0.90 | **-0.58** | 279.27M | 193.14M |
| 59 | SKHY | 2.20 | 1 | 756 | 415 | 968.4k | 2,441 | ASTER:SKHYUSDT | 1.93 | 0.90 | **-0.63** | 1.11M | 968.4k |
| 60 | COIN (RH) | 5.49 | 6 | 1.2k | 5.0k | 423.6k | 875 | HL_XYZ:xyz:COIN | 1.65 | 4.50 | **-0.66** | 4.95M | 423.6k |
| 61 | AMD (RH) | 3.91 | 6 | 2.5k | 2.2k | 954.4k | 1,260 | HL_XYZ:xyz:AMD | 0.21 | 4.50 | **-0.79** | 11.96M | 954.4k |
| 62 | CRV | 5.19 | 1 | 5.2k | 6.6k | 379.5k | 999 | HL:CRV | 1.64 | 4.50 | **-0.95** | 8.10M | 379.5k |
| 63 | TSLA (RH) | 3.81 | 6 | 627 | 3.6k | 1.91M | 4,849 | HL_XYZ:xyz:TSLA | 0.57 | 4.50 | **-1.25** | 18.03M | 1.91M |
| 64 | INTC | 3.83 | 6 | 8.3k | 10.6k | 1.71M | 3,281 | HL_XYZ:xyz:INTC | 0.61 | 4.50 | **-1.28** | 41.36M | 1.71M |
| 65 | HYPE | 1.15 | 6 | 3.0k | 2.3k | 31.09M | 46,532 | ASTER:HYPEUSDT | 1.54 | 0.90 | **-1.29** | 20.42M | 20.42M |
| 66 | ZEC | 2.38 | 6 | 351 | 4.0k | 24.24M | 26,506 | ASTER:ZECUSDT | 2.83 | 0.90 | **-1.35** | 15.14M | 15.14M |
| 67 | DOT | 5.88 | 1 | 2.3k | 2.3k | 624.4k | 1,196 | HL:DOT | 2.76 | 4.50 | **-1.39** | 7.89M | 624.4k |
| 68 | ORCL (RH) | 4.26 | 6 | 1.1k | 5.0k | 821.3k | 826 | HL_XYZ:xyz:ORCL | 1.22 | 4.50 | **-1.46** | 11.32M | 821.3k |
| 69 | SKHYNIXUSD | 0.97 | 6 | 1.1k | 2.1k | 6.43M | 5,302 | ASTER:SKHYNIXUSDT | 1.60 | 0.90 | **-1.53** | 12.65M | 6.43M |
| 70 | BRENTOIL | 3.06 | 6 | 1.3k | 11.4k | 10.91M | 10,065 | HL_XYZ:xyz:BRENTOIL | 0.10 | 4.50 | **-1.54** | 155.29M | 10.91M |
| 71 | AVAX | 2.96 | 1 | 5.5k | 4.2k | 585.1k | 941 | HL:AVAX | 0.12 | 4.50 | **-1.67** | 4.66M | 585.1k |
| 72 | SNDK | 1.78 | 6 | 2.8k | 7.7k | 10.77M | 39,042 | ASTER:SNDKUSD1 | 2.57 | 0.90 | **-1.70** | 9.68M | 9.68M |
| 73 | SOL | 0.10 | 6 | 1.1k | 2.8k | 43.73M | 83,916 | ASTER:SOLUSDT | 0.97 | 0.90 | **-1.77** | 86.53M | 43.73M |
| 74 | BABA (RH) | 4.45 | 6 | 2.2k | 2.7k | 350.3k | 3,266 | HL_XYZ:xyz:BABA | 1.78 | 4.50 | **-1.83** | 1.42M | 350.3k |
| 75 | TRUMP | 3.07 | 1 | 150 | 189 | 711.1k | 2,028 | HL:TRUMP | 0.44 | 4.50 | **-1.86** | 10.56M | 711.1k |
| 76 | XAG | 0.54 | 6 | 20.1k | 4.5k | 12.06M | 9,841 | ASTER:XAGUSDT | 1.51 | 0.90 | **-1.87** | 3.39M | 3.39M |
| 77 | VIRTUAL | 3.86 | 1 | 1.7k | 985 | 247.5k | 1,094 | HL:VIRTUAL | 1.24 | 4.50 | **-1.88** | 3.07M | 247.5k |
| 78 | INTC (RH) | 3.02 | 6 | 250 | 2.1k | 2.21M | 3,228 | HL_XYZ:xyz:INTC | 0.61 | 4.50 | **-2.08** | 41.36M | 2.21M |
| 79 | BE (RH) | 3.37 | 6 | 3.0k | 2.0k | 1.22M | 1,701 | HL_XYZ:xyz:BE | 1.12 | 4.50 | **-2.25** | 4.51M | 1.22M |
| 80 | AAVE | 3.72 | 1 | 150 | 150 | 375.5k | 898 | HL:AAVE | 1.52 | 4.50 | **-2.30** | 4.50M | 375.5k |
| 81 | CRCL | 3.97 | 1 | 1.4k | 255 | 423.6k | 523 | HL_XYZ:xyz:CRCL | 1.99 | 4.50 | **-2.51** | 11.05M | 423.6k |
| 82 | MU (RH) | 2.13 | 6 | 375 | 1.6k | 4.70M | 11,872 | ASTER:MUUSD1 | 3.82 | 0.90 | **-2.59** | 2.82M | 2.82M |
| 83 | META (RH) | 2.37 | 6 | 4.0k | 1.2k | 461.0k | 586 | HL_XYZ:xyz:META | 0.57 | 4.50 | **-2.70** | 10.26M | 461.0k |
| 84 | AMD | 1.85 | 1 | 3.0k | 13.4k | 706.3k | 456 | HL_XYZ:xyz:AMD | 0.21 | 4.50 | **-2.85** | 11.96M | 706.3k |
| 85 | JUP | 2.85 | 1 | 150 | 150 | 553.9k | 884 | HL:JUP | 1.22 | 4.50 | **-2.87** | 6.25M | 553.9k |
| 86 | HOOD | 4.80 | 6 | 338 | 2.1k | 1.91M | 5,020 | HL_XYZ:xyz:HOOD | 3.26 | 4.50 | **-2.96** | 17.68M | 1.91M |
| 87 | CRCL (RH) | 3.53 | 6 | 1.0k | 1.2k | 1.23M | 4,688 | HL_XYZ:xyz:CRCL | 1.99 | 4.50 | **-2.96** | 11.05M | 1.23M |
| 88 | CXMT | 4.21 | 1 | 8.7k | 8.7k | 243.8k | 305 | HL_XYZ:xyz:CXMT | 2.92 | 4.50 | **-3.21** | 11.71M | 243.8k |
| 89 | AAPL (RH) | 2.04 | 6 | 1.6k | 3.5k | 2.21M | 5,015 | HL_XYZ:xyz:AAPL | 0.78 | 4.50 | **-3.25** | 15.25M | 2.21M |
| 90 | MU | 1.45 | 6 | 11.5k | 10.3k | 3.02M | 6,430 | ASTER:MUUSD1 | 3.82 | 0.90 | **-3.27** | 2.82M | 2.82M |
| 91 | MSFT (RH) | 2.41 | 6 | 2.5k | 2.0k | 314.0k | 6,087 | HL_XYZ:xyz:MSFT | 1.41 | 4.50 | **-3.50** | 8.59M | 314.0k |
| 92 | NATGAS | 1.67 | 1 | 5.0k | 15.0k | 960.6k | 1,817 | HL_XYZ:xyz:NATGAS | 0.67 | 4.50 | **-3.50** | 7.17M | 960.6k |
| 93 | AMZN (RH) | 2.14 | 6 | 4.0k | 2.5k | 227.1k | 425 | HL_XYZ:xyz:AMZN | 1.17 | 4.50 | **-3.53** | 8.41M | 227.1k |
| 94 | TSLA | 1.41 | 1 | 18.8k | 10.3k | 987.3k | 3,910 | HL_XYZ:xyz:TSLA | 0.57 | 4.50 | **-3.65** | 18.03M | 987.3k |
| 95 | DRAM | 1.14 | 1 | 4.4k | 35.8k | 1.21M | 1,359 | HL_XYZ:xyz:DRAM | 0.33 | 4.50 | **-3.68** | 77.30M | 1.21M |
| 96 | AAPL | 1.60 | 6 | 12.8k | 12.8k | 1.41M | 3,316 | HL_XYZ:xyz:AAPL | 0.78 | 4.50 | **-3.69** | 15.25M | 1.41M |
| 97 | LINK | 1.44 | 1 | 190 | 1.8k | 974.3k | 1,437 | ASTER:LINKUSDT | 4.32 | 0.90 | **-3.78** | 1.41M | 974.3k |
| 98 | ENA | 1.82 | 6 | 308 | 389 | 2.46M | 6,178 | HL:ENA | 1.21 | 4.50 | **-3.89** | 24.36M | 2.46M |
| 99 | GOOGL (RH) | 1.19 | 6 | 1.8k | 3.9k | 2.99M | 1,368 | HL_XYZ:xyz:GOOGL | 0.59 | 4.50 | **-3.91** | 10.67M | 2.99M |
| 100 | GOOGL | 1.04 | 6 | 13.4k | 15.0k | 7.61M | 1,795 | HL_XYZ:xyz:GOOGL | 0.59 | 4.50 | **-4.06** | 10.67M | 7.61M |
| 101 | LIT | 0.21 | 6 | 516 | 883 | 24.20M | 100,296 | ASTER:LITUSDT | 3.53 | 0.90 | **-4.22** | 3.69M | 3.69M |
| 102 | PUMP | 2.31 | 6 | 1.2k | 846 | 12.54M | 20,525 | HL:PUMP | 2.31 | 4.50 | **-4.50** | 165.26M | 12.54M |
| 103 | NBIS | 1.33 | 6 | 1.7k | 1.5k | 2.23M | 3,874 | HL_XYZ:xyz:NBIS | 1.33 | 4.50 | **-4.50** | 14.70M | 2.23M |
| 104 | COIN | 1.26 | 1 | 7.9k | 4.9k | 879.3k | 1,115 | HL_XYZ:xyz:COIN | 1.65 | 4.50 | **-4.89** | 4.95M | 879.3k |
| 105 | CBRS | 1.89 | 1 | 673 | 677 | 499.3k | 860 | HL_XYZ:xyz:CBRS | 2.36 | 4.50 | **-4.97** | 5.41M | 499.3k |
| 106 | AMZN | 0.51 | 1 | 367 | 5.0k | 287.8k | 391 | HL_XYZ:xyz:AMZN | 1.17 | 4.50 | **-5.16** | 8.41M | 287.8k |
| 107 | PENDLE | 2.65 | 1 | 5.7k | 278 | 350.7k | 759 | HL:PENDLE | 3.53 | 4.50 | **-5.38** | 7.00M | 350.7k |
| 108 | CASHCAT | 2.72 | 1 | 69 | 71 | 1.19M | 20,343 | HL:CASHCAT | 6.01 | 4.50 | **-7.79** | 22.45M | 1.19M |

## Shortlist to record next

Passing rows with positive room and a flow proxy of at least 300.0k, best first. `hedges` lists every venue that carries the base, not just the cheapest one - a name on two hedge venues can be re-routed when one of them widens.

| # | Lighter mkt | room bps | L spread bps | L 24h vol | L trades | flow proxy | best hedge | hedges available |
|---:|---|---:|---:|---:|---:|---:|---|---|
| 1 | ANSEM | **+50.47** | 58.22 | 696.7k | 14,699 | 696.7k | ASTER:ANSEMUSDT | ASTER (1.18M) |
| 2 | PONS (RH) | **+27.23** | 35.74 | 807.8k | 6,179 | 807.8k | HL:PONS | ASTER (8.83M), HL (105.54M) |
| 3 | PONS | **+23.50** | 32.01 | 3.72M | 16,812 | 3.72M | HL:PONS | ASTER (8.83M), HL (105.54M) |
| 4 | ORCL | **+18.60** | 24.31 | 395.7k | 152 | 395.7k | HL_XYZ:xyz:ORCL | ASTER (1.8k), HL_XYZ (11.32M) |
| 5 | XPL | **+17.20** | 24.40 | 1.18M | 1,584 | 1.18M | HL:XPL | ASTER (225.0k), HL (12.67M) |
| 6 | MON | **+9.22** | 15.46 | 698.4k | 1,554 | 698.4k | HL:MON | ASTER (87.5k), HL (9.65M) |
| 7 | TIA | **+7.76** | 13.08 | 354.2k | 1,122 | 354.2k | HL:TIA | ASTER (227.9k), HL (1.82M) |
| 8 | ASTER | **+7.15** | 9.35 | 709.8k | 2,392 | 709.8k | ASTER:ASTERUSDT | ASTER (87.72M), HL (11.68M) |

## Calibration: the 9 symbols we already recorded

Expected from the 11.6 h recording: PONS near the top; ZEC / LIT / HYPE / SOL near the bottom (Lighter spread 1-2.4 bps, narrower than the hedge, so negative room); ASTER wide but too thin to carry inventory.

| symbol | deployment | rank | L spread bps | hedge | H spread bps | room bps | L 24h vol | verdict |
|---|---|---:|---:|---|---:|---:|---:|---|
| SOL | LIGHTER | 73 | 0.10 | ASTER:SOLUSDT | 0.97 | -1.77 | 43.73M | rank 73 / 108 |
| SOL | LIGHTER_RH | 36 | 2.17 | ASTER:SOLUSDT | 0.97 | +0.31 | 6.08M | rank 36 / 108 |
| HYPE | LIGHTER | 65 | 1.15 | ASTER:HYPEUSDT | 1.54 | -1.29 | 31.09M | rank 65 / 108 |
| HYPE | LIGHTER_RH | 39 | 2.61 | ASTER:HYPEUSDT | 1.54 | +0.17 | 9.79M | rank 39 / 108 |
| ZEC | LIGHTER | 66 | 2.38 | ASTER:ZECUSDT | 2.83 | -1.35 | 24.24M | rank 66 / 108 |
| ZEC | LIGHTER_RH | 45 | 3.74 | ASTER:ZECUSDT | 2.83 | +0.01 | 3.04M | rank 45 / 108 |
| PONS | LIGHTER | 3 | 32.01 | HL:PONS | 4.01 | +23.50 | 3.72M | rank 3 / 108 |
| PONS | LIGHTER_RH | 2 | 35.74 | HL:PONS | 4.01 | +27.23 | 807.8k | rank 2 / 108 |
| LIT | LIGHTER | 101 | 0.21 | ASTER:LITUSDT | 3.53 | -4.22 | 24.20M | rank 101 / 108 |
| LIT | LIGHTER_RH | 57 | 3.85 | ASTER:LITUSDT | 3.53 | -0.58 | 7.76M | rank 57 / 108 |
| ASTER | LIGHTER | 9 | 9.35 | ASTER:ASTERUSDT | 1.30 | +7.15 | 709.8k | rank 9 / 108 |
| DASH | LIGHTER | 24 | 7.24 | HL:DASH | 1.03 | +1.70 | 395.0k | rank 24 / 108 |
| PUMP | LIGHTER | 102 | 2.31 | HL:PUMP | 2.31 | -4.50 | 12.54M | rank 102 / 108 |
| ARB | LIGHTER | 21 | 8.01 | HL:ARB | 1.42 | +2.08 | 3.72M | rank 21 / 108 |

## Filtered out

| Lighter mkt | L spread bps | n | L 24h vol | best hedge | room bps | reason |
|---|---:|---:|---:|---|---:|---|
| AI (RH) | 214.39 | 6 | 134.0k | ASTER:AIUSDT | +32.99 | Lighter 24h vol 134.0k < 200.0k |
| ANSEM (RH) | 143.20 | 6 | 97.4k | ASTER:ANSEMUSDT | +135.44 | Lighter 24h vol 97.4k < 200.0k |
| XIAOMI | 112.98 | 1 | 2.8k | ASTER:XIAOMIUSDT | +53.55 | Lighter 24h vol 2.8k < 200.0k |
| TENCENT | 103.34 | 1 | 443 | ASTER:TENCENTUSDT | +91.69 | Lighter 24h vol 443 < 200.0k |
| OPENAI | 100.40 | 6 | 267.6k | ASTER:OPENAIUSDT | +23.89 | ASTER 24h vol 658 < 1.00M |
| HYUNDAIUSD | 94.98 | 1 | 2.5k | HL_XYZ:xyz:HYUNDAI | +86.66 | Lighter 24h vol 2.5k < 200.0k |
| BMNR | 91.90 | 1 | 114.9k | HL_XYZ:xyz:BMNR | +63.60 | Lighter 24h vol 114.9k < 200.0k |
| BOT | 86.82 | 1 | 8.8k | HL_XYZ:xyz:BOT | +78.68 | Lighter 24h vol 8.8k < 200.0k |
| PROVE | 83.94 | 1 | 214 | HL:PROVE | +72.80 | Lighter 24h vol 214 < 200.0k |
| AAOI | 83.31 | 1 | 0 | HL_XYZ:xyz:AAOI | +76.96 | Lighter 24h vol 0 < 200.0k |
| 1000NOT | 80.94 | 1 | 28.2k | ASTER:NOTUSDT | +61.04 | Lighter 24h vol 28.2k < 200.0k |
| FOLKS | 80.32 | 1 | 52.6k | ASTER:FOLKSUSDT | +44.29 | Lighter 24h vol 52.6k < 200.0k |
| APEX | 69.49 | 1 | 3.9k | HL:APEX | +56.62 | Lighter 24h vol 3.9k < 200.0k |
| GMX | 69.02 | 1 | 4.7k | HL:GMX | +60.96 | Lighter 24h vol 4.7k < 200.0k |
| ANTHROPIC | 68.98 | 1 | 45.5k | ASTER:ANTHROPICUSDT | -47.92 | Lighter 24h vol 45.5k < 200.0k |
| CTR | 68.66 | 1 | 869 | ASTER:CTRUSDT | +24.25 | Lighter 24h vol 869 < 200.0k |
| RAIL | 63.12 | 1 | 10.9k | - | - | hedge listed but no hedge book sampled |
| CAP | 60.69 | 1 | 12.7k | ASTER:CAPUSDT | +35.08 | Lighter 24h vol 12.7k < 200.0k |
| ZHIPU | 58.10 | 1 | 26.5k | HL_XYZ:xyz:ZHIPU | +51.04 | Lighter 24h vol 26.5k < 200.0k |
| GEV | 57.82 | 1 | 927 | HL_XYZ:xyz:GEV | +40.41 | Lighter 24h vol 927 < 200.0k |
| ASML | 53.12 | 1 | 329 | HL_XYZ:xyz:ASML | +44.65 | Lighter 24h vol 329 < 200.0k |
| AI | 47.53 | 1 | 494.5k | ASTER:AIUSDT | -133.87 | ASTER 24h vol 492.2k < 1.00M |
| BIO | 46.20 | 1 | 91 | HL:BIO | +39.15 | Lighter 24h vol 91 < 200.0k |
| USELESS | 44.77 | 6 | 1.69M | ASTER:USELESSUSDT | +27.53 | ASTER 24h vol 766.5k < 1.00M |
| ARC | 42.76 | 1 | 6.8k | ASTER:ARCUSDT | +10.43 | Lighter 24h vol 6.8k < 200.0k |
| IWM | 40.26 | 1 | 7.0k | ASTER:IWMUSDT | -78.85 | Lighter 24h vol 7.0k < 200.0k |
| MINIMAX | 40.16 | 1 | 45.1k | HL_XYZ:xyz:MINIMAX | +31.64 | Lighter 24h vol 45.1k < 200.0k |
| CASHCAT (RH) | 39.89 | 6 | 198.4k | HL:CASHCAT | +29.38 | Lighter 24h vol 198.4k < 200.0k |
| TSM | 36.72 | 1 | 22.7k | HL_XYZ:xyz:TSM | +30.27 | Lighter 24h vol 22.7k < 200.0k |
| NOW | 33.98 | 1 | 2.7k | HL_XYZ:xyz:NOW | +23.70 | Lighter 24h vol 2.7k < 200.0k |
| DELL | 32.32 | 1 | 5.0k | HL_XYZ:xyz:DELL | +24.94 | Lighter 24h vol 5.0k < 200.0k |
| BABA | 31.11 | 1 | 8.9k | HL_XYZ:xyz:BABA | +24.83 | Lighter 24h vol 8.9k < 200.0k |
| SHEIN | 30.57 | 6 | 479.7k | HL_XYZ:xyz:SHEIN | +25.87 | HL_XYZ 24h vol 381.1k < 1.00M |
| DOLO | 29.13 | 1 | 0 | ASTER:DOLOUSDT | +8.19 | Lighter 24h vol 0 < 200.0k |
| SHEIN (RH) | 28.60 | 6 | 53.9k | HL_XYZ:xyz:SHEIN | +23.91 | Lighter 24h vol 53.9k < 200.0k |
| STRC | 27.20 | 6 | 3.32M | ASTER:STRCUSDT | +19.15 | ASTER 24h vol 10.8k < 1.00M |
| BB | 26.65 | 1 | 4.0k | HL_XYZ:xyz:BB | +15.07 | Lighter 24h vol 4.0k < 200.0k |
| ETHFI | 24.93 | 1 | 176.1k | HL:ETHFI | +17.59 | Lighter 24h vol 176.1k < 200.0k |
| CRWV | 24.28 | 1 | 14.1k | HL_XYZ:xyz:CRWV | +18.51 | Lighter 24h vol 14.1k < 200.0k |
| EDEN | 23.76 | 1 | 3.1k | ASTER:EDENUSDT | +12.79 | Lighter 24h vol 3.1k < 200.0k |
| RKLB | 23.09 | 1 | 3.2k | HL_XYZ:xyz:RKLB | +16.12 | Lighter 24h vol 3.2k < 200.0k |
| 0G | 20.34 | 1 | 12.0k | HL:0G | +12.28 | Lighter 24h vol 12.0k < 200.0k |
| SKY | 19.96 | 1 | 41.7k | HL:SKY | +13.55 | Lighter 24h vol 41.7k < 200.0k |
| AZTEC | 17.48 | 1 | 10.2k | HL:AZTEC | +8.94 | Lighter 24h vol 10.2k < 200.0k |
| ZORA | 17.04 | 1 | 12.8k | HL:ZORA | +6.86 | Lighter 24h vol 12.8k < 200.0k |
| ASTS (RH) | 16.70 | 6 | 34.5k | ASTER:ASTSUSDT | -79.22 | Lighter 24h vol 34.5k < 200.0k |
| DYDX | 16.29 | 1 | 107.5k | HL:DYDX | +6.07 | Lighter 24h vol 107.5k < 200.0k |
| AVNT | 16.06 | 1 | 38.8k | HL:AVNT | +6.17 | Lighter 24h vol 38.8k < 200.0k |
| WLFI | 16.03 | 1 | 150.5k | ASTER:WLFIUSDT | +9.80 | Lighter 24h vol 150.5k < 200.0k |
| EDGE | 15.89 | 1 | 41.6k | ASTER:EDGEUSDT | +6.44 | Lighter 24h vol 41.6k < 200.0k |
| RIVER | 15.82 | 1 | 8.3k | ASTER:RIVERUSDT | -0.84 | Lighter 24h vol 8.3k < 200.0k |
| PYTH | 15.60 | 6 | 203.9k | HL:PYTH | +9.08 | HL 24h vol 583.6k < 1.00M |
| USAR (RH) | 15.20 | 6 | 500.2k | HL_XYZ:xyz:USAR | +9.27 | HL_XYZ 24h vol 500.3k < 1.00M |
| STABLE | 14.86 | 1 | 32.6k | HL:STABLE | +7.94 | Lighter 24h vol 32.6k < 200.0k |
| MNT | 14.48 | 1 | 118.6k | HL:MNT | +6.00 | Lighter 24h vol 118.6k < 200.0k |
| XPD | 14.37 | 1 | 8.1k | ASTER:XPDUSDT | +8.45 | Lighter 24h vol 8.1k < 200.0k |
| MEGA | 14.24 | 1 | 15.9k | HL:MEGA | +5.24 | Lighter 24h vol 15.9k < 200.0k |
| MYX | 13.82 | 1 | 1.6k | ASTER:MYXUSDT | -3.66 | Lighter 24h vol 1.6k < 200.0k |
| ROBO | 13.43 | 1 | 11.0k | ASTER:ROBOUSDT | -12.62 | Lighter 24h vol 11.0k < 200.0k |
| S | 13.18 | 1 | 18.3k | HL:S | +6.04 | Lighter 24h vol 18.3k < 200.0k |
| SKR | 12.78 | 1 | 107.0k | HL:SKR | +5.09 | Lighter 24h vol 107.0k < 200.0k |
| 1000FLOKI | 12.77 | 1 | 29.9k | HL:kFLOKI | +4.51 | Lighter 24h vol 29.9k < 200.0k |
| ZK | 12.31 | 1 | 158.2k | HL:ZK | +4.97 | Lighter 24h vol 158.2k < 200.0k |
| MET | 11.62 | 1 | 182.4k | HL:MET | +4.60 | Lighter 24h vol 182.4k < 200.0k |
| POPCAT | 11.53 | 1 | 21.5k | HL:POPCAT | +4.72 | Lighter 24h vol 21.5k < 200.0k |
| OPENAI (RH) | 11.36 | 6 | 6.52M | ASTER:OPENAIUSDT | -65.15 | ASTER 24h vol 658 < 1.00M |
| IREN (RH) | 11.18 | 6 | 65.6k | HL_XYZ:xyz:IREN | -0.81 | Lighter 24h vol 65.6k < 200.0k |
| XCU | 11.16 | 1 | 167.4k | ASTER:XCUUSDT | +6.59 | Lighter 24h vol 167.4k < 200.0k |
| SPX | 10.41 | 1 | 139.4k | HL:SPX | +2.38 | Lighter 24h vol 139.4k < 200.0k |
| UNITREE | 10.39 | 1 | 128.8k | ASTER:UNITREEUSDT | +6.12 | Lighter 24h vol 128.8k < 200.0k |
| RESOLV | 10.37 | 1 | 6.9k | HL:RESOLV | +0.42 | Lighter 24h vol 6.9k < 200.0k |
| LDO | 10.13 | 1 | 131.0k | HL:LDO | +3.85 | Lighter 24h vol 131.0k < 200.0k |
| CHIP | 9.73 | 1 | 115.5k | HL:CHIP | +1.92 | Lighter 24h vol 115.5k < 200.0k |
| FOGO | 9.60 | 1 | 16.1k | HL:FOGO | +0.99 | Lighter 24h vol 16.1k < 200.0k |
| AXS | 9.22 | 1 | 7.8k | HL:AXS | +1.34 | Lighter 24h vol 7.8k < 200.0k |
| OP | 9.02 | 1 | 315.5k | HL:OP | +0.00 | HL 24h vol 877.1k < 1.00M |
| MRVL | 8.43 | 1 | 38.7k | HL_XYZ:xyz:MRVL | +1.71 | Lighter 24h vol 38.7k < 200.0k |
| MRNA | 8.30 | 1 | 45.3k | HL_XYZ:xyz:MRNA | -1.74 | Lighter 24h vol 45.3k < 200.0k |
| 1000PEPE | 8.22 | 1 | 130.1k | ASTER:1000PEPEUSDT | +3.61 | Lighter 24h vol 130.1k < 200.0k |
| STBL | 8.15 | 1 | 4.9k | HL:STBL | -3.28 | Lighter 24h vol 4.9k < 200.0k |
| SEI | 8.10 | 1 | 131.6k | HL:SEI | -0.45 | Lighter 24h vol 131.6k < 200.0k |
| BERA | 8.04 | 1 | 9.5k | HL:BERA | +1.93 | Lighter 24h vol 9.5k < 200.0k |
| KAITO | 7.94 | 1 | 75.8k | ASTER:KAITOUSDT | +1.15 | Lighter 24h vol 75.8k < 200.0k |
| HBAR | 7.45 | 1 | 199.3k | HL:HBAR | +1.71 | Lighter 24h vol 199.3k < 200.0k |
| 1000SHIB | 7.29 | 1 | 113.0k | ASTER:1000SHIBUSDT | +0.92 | Lighter 24h vol 113.0k < 200.0k |
| LINEA | 7.27 | 1 | 53.3k | HL:LINEA | -0.86 | Lighter 24h vol 53.3k < 200.0k |
| TSM (RH) | 6.90 | 6 | 100.8k | HL_XYZ:xyz:TSM | +0.44 | Lighter 24h vol 100.8k < 200.0k |
| EWY | 6.86 | 1 | 31.6k | HL_XYZ:xyz:EWY | +1.30 | Lighter 24h vol 31.6k < 200.0k |
| WIF | 6.77 | 1 | 174.9k | HL:WIF | -0.44 | Lighter 24h vol 174.9k < 200.0k |
| FF | 6.71 | 1 | 25.8k | ASTER:FFUSDT | -13.51 | Lighter 24h vol 25.8k < 200.0k |
| XPT | 6.54 | 1 | 15.3k | ASTER:XPTUSDT | +2.20 | Lighter 24h vol 15.3k < 200.0k |
| BCH | 6.23 | 1 | 135.2k | HL:BCH | -0.60 | Lighter 24h vol 135.2k < 200.0k |
| SYRUP | 5.97 | 1 | 73.5k | HL:SYRUP | +0.62 | Lighter 24h vol 73.5k < 200.0k |
| 2Z | 5.88 | 1 | 11.2k | HL:2Z | +0.01 | Lighter 24h vol 11.2k < 200.0k |
| CC | 5.70 | 1 | 175.8k | HL:CC | -0.70 | Lighter 24h vol 175.8k < 200.0k |
| GRASS | 5.33 | 1 | 182.2k | HL:GRASS | -1.84 | Lighter 24h vol 182.2k < 200.0k |
| SOXL (RH) | 4.08 | 6 | 467.9k | ASTER:SOXLUSDT | -0.90 | ASTER 24h vol 445.0k < 1.00M |
| SAMSUNGUSD | 3.98 | 6 | 2.81M | ASTER:SAMSUNGUSDT | -1.38 | ASTER 24h vol 162.7k < 1.00M |
| 1000BONK | 3.17 | 1 | 165.6k | HL:kBONK | -7.66 | Lighter 24h vol 165.6k < 200.0k |
| IBM | 2.57 | 1 | 7.7k | HL_XYZ:xyz:IBM | -4.93 | Lighter 24h vol 7.7k < 200.0k |
| SOXL | 2.45 | 1 | 699.1k | ASTER:SOXLUSDT | -2.53 | ASTER 24h vol 445.0k < 1.00M |
| PAXG | 2.18 | 1 | 233.7k | ASTER:PAXGUSDT | +1.26 | ASTER 24h vol 109.7k < 1.00M |
| MSTR | 2.14 | 1 | 590.5k | ASTER:MSTRUSDT | -1.97 | ASTER 24h vol 100.7k < 1.00M |
| LITE | 2.04 | 1 | 40.5k | HL_XYZ:xyz:LITE | -4.16 | Lighter 24h vol 40.5k < 200.0k |
| META | 1.80 | 1 | 143.2k | HL_XYZ:xyz:META | -3.27 | Lighter 24h vol 143.2k < 200.0k |
| PLTR | 1.73 | 1 | 136.5k | HL_XYZ:xyz:PLTR | -3.92 | Lighter 24h vol 136.5k < 200.0k |
| QQQ | 1.60 | 6 | 1.71M | ASTER:QQQUSDT | -0.07 | ASTER 24h vol 208.8k < 1.00M |
| SPY | 1.37 | 6 | 3.04M | ASTER:SPYUSDT | -6.29 | ASTER 24h vol 229.8k < 1.00M |
| NVDA (RH) | 1.29 | 6 | 3.09M | ASTER:NVDAUSDT | -1.12 | ASTER 24h vol 196.3k < 1.00M |
| NVDA | 1.25 | 6 | 7.01M | ASTER:NVDAUSDT | -1.16 | ASTER 24h vol 196.3k < 1.00M |
| POL | 1.14 | 1 | 384.3k | HL:POL | -5.02 | HL 24h vol 622.3k < 1.00M |
| ANTHROPIC (RH) | 0.97 | 6 | 12.00M | ASTER:ANTHROPICUSDT | -115.93 | ASTER 24h vol 14.4k < 1.00M |
| TRX | 0.59 | 1 | 81.7k | ASTER:TRXUSDT | -0.60 | Lighter 24h vol 81.7k < 200.0k |
| QQQ (RH) | 0.28 | 6 | 24.20M | ASTER:QQQUSDT | -1.39 | ASTER 24h vol 208.8k < 1.00M |
| SPY (RH) | 0.13 | 6 | 53.80M | ASTER:SPYUSDT | -7.52 | ASTER 24h vol 229.8k < 1.00M |
| POPMART | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| KIOXIA | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| KORU | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| GME | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| AXTI | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| ARM | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| NOK | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| BE | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| AVGO | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| WDC | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| QNT | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |
| QCOM | - | 0 | 0 | - | - | no Lighter two-sided quote sampled |

## Appendix: Lighter markets with no hedge on Aster or Hyperliquid

Kept here rather than dropped silently - these are Lighter-only listings (Korean and Chinese equities, pre-IPO names, FX crosses, rates) where the hedge would have to come from somewhere else entirely.

| Lighter mkt | deployment | L spread bps | n | L 24h vol | L trades |
|---|---|---:|---:|---:|---:|
| US100 | LIGHTER | 0.68 | 6 | 11.08M | 3,477 |
| WTI | LIGHTER | 1.66 | 6 | 8.16M | 10,265 |
| US500 | LIGHTER | 0.13 | 6 | 3.15M | 904 |
| USDJPY | LIGHTER | 2.92 | 1 | 577.4k | 401 |
| SGOV | LIGHTER_RH | 1.99 | 6 | 445.3k | 70 |
| SLV | LIGHTER_RH | 9.87 | 6 | 382.0k | 527 |
| USO | LIGHTER_RH | 7.21 | 6 | 249.2k | 161 |
| AMC | LIGHTER_RH | 25.57 | 6 | 129.6k | 365 |
| DATA | LIGHTER | 15.50 | 1 | 115.9k | 161 |
| USDKRW | LIGHTER | 21.58 | 1 | 108.2k | 120 |
| EURUSD | LIGHTER | 3.01 | 1 | 101.0k | 140 |
| SMIC | LIGHTER | 45.82 | 1 | 95.7k | 656 |
| H100 | LIGHTER | 287.02 | 1 | 78.4k | 306 |
| SMCI | LIGHTER_RH | 17.16 | 6 | 53.4k | 50 |
| WHEAT | LIGHTER | 382.11 | 1 | 49.5k | 21 |
| AUDUSD | LIGHTER | 3.88 | 1 | 44.1k | 23 |
| SOFI | LIGHTER_RH | 26.51 | 6 | 43.9k | 460 |
| WULF | LIGHTER_RH | 159.39 | 6 | 28.6k | 141 |
| CRO | LIGHTER | 11.78 | 1 | 27.8k | 85 |
| GBPUSD | LIGHTER | 2.51 | 1 | 23.6k | 57 |
| QBTS | LIGHTER_RH | 49.29 | 6 | 20.1k | 65 |
| RGTI | LIGHTER_RH | 69.93 | 6 | 14.5k | 33 |
| LUNR | LIGHTER_RH | 78.98 | 6 | 11.7k | 72 |
| US10Y | LIGHTER | 20.20 | 1 | 6.2k | 6 |
| CLSK | LIGHTER_RH | 40.32 | 6 | 4.2k | 25 |
| NMR | LIGHTER | 26.39 | 1 | 3.9k | 38 |
| USDCHF | LIGHTER | 19.50 | 1 | 1.7k | 112 |
| USDCAD | LIGHTER | 3.55 | 1 | 1.5k | 2,516 |
| STABLECOINX | LIGHTER | 123.54 | 1 | 307 | 3 |
| BOTZ | LIGHTER | 179.39 | 1 | 182 | 2 |
| WEN | LIGHTER | 96.12 | 1 | 173 | 8 |
| NZDUSD | LIGHTER | 4.11 | 1 | 75 | 1 |
| URA | LIGHTER | 525.69 | 1 | 0 | 0 |
| TTWO | LIGHTER | - | 0 | 0 | 0 |
| ADI | LIGHTER | 51.63 | 1 | 0 | 0 |
| BYD | LIGHTER | - | 0 | 0 | 0 |
| USDHKD | LIGHTER | - | 0 | 0 | 0 |
| SOXS | LIGHTER | - | 0 | 0 | 0 |

## Errors and failed endpoints

None - every endpoint answered on every pass.

## Method

- Lighter market list and 24h stats: `GET /api/v1/orderBookDetails`. Called without `market_id` it returns every market in one response (`daily_quote_token_volume`, `daily_trades_count`, `open_interest`, `last_trade_price`).
- Lighter top of book: `GET /api/v1/orderBookOrders?market_id=N&limit=K`. This returns raw resting orders, not aggregated price levels, so sizes at the best price are summed. There is no all-market book endpoint (the call 400s without `market_id`), which is why the run is split into a broad pass over every market plus deep passes over a smaller set.
- Lighter Robinhood Chain deployment: base `https://api.rh.lighter.xyz`, same API shape. The URL is not in any Python source; it is a string constant inside the Rust adapter (`_libnautilus` binary, alongside `https://mainnet.zklighter.elliot.ai`).
- Hyperliquid: one `POST /info {"type":"metaAndAssetCtxs"}` per dex for the universe and 24h notional, then `POST /info {"type":"l2Book","coin":C}` per matched coin. The HIP-3 stock dex is reached with `{"dex":"xyz"}` and coin names prefixed `xyz:` - both work on the public info endpoint with no extra setup, so its markets are included as a hedge venue (`HL_XYZ`).
- Aster: `GET /fapi/v1/ticker/24hr` and `GET /fapi/v1/ticker/bookTicker` - two calls cover every symbol. `exchangeInfo` is deliberately not called (strict rate limit).
- Symbol matching normalises `USDT`/`USDC`/`USD1`/`USD`/`-PERP` suffixes, the `xyz:` prefix, and the two scaled-token notations (Hyperliquid `kPEPE` vs Lighter/Aster `1000PEPE`). Six-letter FX pairs are whitelisted so `AUDUSD` is not truncated to `AUD`. When a venue lists several contracts on the same base (e.g. Aster `XAUUSDT` and `XAUUSD1`) the higher-volume one is used.
- Raw responses for every endpoint and pass are written outside the repo, one file per endpoint per pass (`C:/Users/myron/AppData/Local/Temp/claude/E--Nautilus-Perps/54d1ed36-9500-4edb-80b4-3e654fe131e8/scratchpad/lighter_screen`).

Generated by `src/analysis/lighter_screen.py` in 6.4 min.

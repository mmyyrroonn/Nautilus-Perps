# Symbol discovery: Hyperliquid (main + xyz/HIP-3) x Lighter x Aster — mainnet, 2026-09-07

Raw JSON saved in this directory (all fetched once, single call each):
`hl_meta.json`, `hl_xyz_meta.json`, `lighter_orderbooks.json`, `lighter_orderbookdetails.json`, `aster_exchangeinfo.json`, `aster_ticker24hr.json`.

Universe sizes: HL main dex 233 assets, HL `xyz` dex 119 assets, Lighter 244 order books, Aster 583 symbols (578 with a 24h ticker; 578 `PERPETUAL`, 5 with empty `contractType`).

Normalisation used: HL xyz `xyz:XXX` -> base `XXX`; HL xyz `xyz:GOLD` <-> Lighter `XAU` <-> Aster `XAUUSDT`/`XAUUSD1` treated as base `XAU`. HL k-prefixed (`kPEPE`, `kSHIB`, `kBONK`, `kLUNC`, `kFLOKI`, `kDOGS`, `kNEIRO`) matched to Lighter/Aster `1000XXX` (confirmed these are the only k-prefixed names on HL main; Lighter/Aster use `1000BONK`, `1000FLOKI`, `1000NOT`, `1000PEPE`, `1000SHIB`, `1000TOSHI` etc — matched by symmetric-multiplier convention, both sides scale 1000 base units per contract). Aster quote suffix stripped (`USDT`/`USD1`/`USDC`/`USD`); where a base has both `XUSDT` and `XUSD1` on Aster, both are shown (their volumes are NOT summed). No base-name collisions found on any venue after normalisation.

---

## Table A — Stock / commodity perps on all three venues (HL `xyz` + Lighter + Aster)

54 bases matched. Sorted by HL xyz 24h notional volume (dayNtlVlm, USD) descending. Lighter volume = `daily_quote_token_volume` from orderBookDetails (USD-quoted). Aster volume = `quoteVolume` from ticker/24hr per listed quote-asset variant. Mark price: HL `markPx`, Lighter `mark_price` (orderBookDetails). Aster mark price not separately fetched (only `lastPrice` from ticker, not shown here to avoid mixing with mark).

| Base | HL symbol | HL 24h vol (USD) | HL mark | Lighter symbol | Lighter 24h vol (USD) | Lighter mark | Lighter status | Aster symbol(s) : 24h quoteVolume (USD) |
|---|---|---:|---:|---|---:|---:|---|---|
| SNDK | xyz:SNDK | 20,071,237 | 1766.4 | SNDK | 7,544,934 | 1768.51 | active | SNDKUSD1=39,848,665; SNDKUSDT=3,004,040 |
| MU | xyz:MU | 18,016,366 | 1018.5 | MU | 1,733,487 | 1019.26 | active | MUUSD1=16,572,059; MUUSDT=209,632 |
| HOOD | xyz:HOOD | 16,900,335 | 125.06 | HOOD | 2,203,481 | 125.145 | active | HOODUSDT=141,095 |
| CRCL | xyz:CRCL | 12,912,321 | 103.52 | CRCL | 326,803 | 103.583 | active | CRCLUSDT=435,334 |
| DRAM | xyz:DRAM | 11,482,008 | 59.845 | DRAM | 455,590 | 59.915 | active | DRAMUSDT=14,038 |
| SPCX | xyz:SPCX | 10,145,795 | 150.73 | SPCX | 1,313,437 | 150.77 | active | SPCXUSD1=11,169,594; SPCXUSDT=707,058 |
| SKHY | xyz:SKHY | 9,114,963 | 175.88 | SKHY | 150,874 | 175.85 | active | SKHYUSDT=82,749 |
| NVDA | xyz:NVDA | 9,084,518 | 231.12 | NVDA | 1,545,469 | 231.310 | active | NVDAUSDT=47,521 |
| INTC | xyz:INTC | 5,775,631 | 97.045 | INTC | 277,343 | 97.139 | active | INTCUSDT=5,626 |
| TSLA | xyz:TSLA | 4,797,149 | 355.59 | TSLA | 3,458,235 | 355.89 | active | TSLAUSDT=13,868 |
| XAU (gold) | xyz:GOLD | 4,672,191 | 4426.5 | XAU | 5,805,912 | 4431.75 | active | XAUUSD1=497,987,652; XAUUSDT=715,071 |
| MSTR | xyz:MSTR | 3,596,195 | 143.54 | MSTR | 350,443 | 143.50 | active | MSTRUSDT=78,625 |
| GOOGL | xyz:GOOGL | 3,555,509 | 338.75 | GOOGL | 4,227,804 | 339.13 | active | GOOGLUSDT=5,410 |
| EWY | xyz:EWY | 3,170,957 | 188.0 | EWY | 55,479 | 187.87 | active | EWYUSDT=695 |
| SOXL | xyz:SOXL | 2,913,821 | 121.56 | SOXL | 167,722 | 121.77 | active | SOXLUSDT=58,468 |
| NBIS | xyz:NBIS | 2,067,367 | 221.73 | NBIS | 639,090 | 221.77 | active | NBISUSDT=6,659 |
| COIN | xyz:COIN | 2,025,525 | 185.7 | COIN | 577,724 | 185.741 | active | COINUSDT=4,816 |
| MSFT | xyz:MSFT | 1,874,993 | 501.67 | MSFT | 7,354,358 | 502.66 | active | MSFTUSDT=8,327 |
| AAPL | xyz:AAPL | 1,843,489 | 321.47 | AAPL | 1,209,052 | 321.835 | active | AAPLUSDT=13,361 |
| META | xyz:META | 1,518,431 | 614.57 | META | 198,035 | 614.79 | active | METAUSDT=5,606 |
| MRVL | xyz:MRVL | 1,272,736 | 221.46 | MRVL | 22,078 | 221.43 | active | MRVLUSDT=17,456 |
| ORCL | xyz:ORCL | 992,701 | 160.8 | ORCL | 21,348 | 160.98 | active | ORCLUSDT=250 |
| AMZN | xyz:AMZN | 961,218 | 258.83 | AMZN | 140,239 | 259.008 | active | AMZNUSDT=13,608 |
| NATGAS | xyz:NATGAS | 869,344 | 2.9351 | NATGAS | 2,140 | 2.9350 | active | NATGASUSDT=40,232 |
| DELL | xyz:DELL | 854,854 | 523.86 | DELL | 9,003 | 524.35 | active | DELLUSDT=78,317 |
| CXMT | xyz:CXMT | 781,551 | 8.3985 | CXMT | 73,217 | 8.4165 | active | CXMTUSDT=9,693 |
| AMD | xyz:AMD | 775,108 | 477.77 | AMD | 110,028 | 477.93 | active | AMDUSDT=1,272 |
| CRWV | xyz:CRWV | 765,934 | 90.084 | CRWV | 5,081 | 90.07 | active | CRWVUSDT=954 |
| PLTR | xyz:PLTR | 704,298 | 175.8 | PLTR | 62,610 | 175.854 | active | PLTRUSDT=3,829 |
| BE | xyz:BE | 587,858 | 267.14 | BE | **0** | 266.10 | active | BEUSDT=454 |
| TSM | xyz:TSM | 491,613 | 427.85 | TSM | 18,068 | 428.66 | active | TSMUSDT=632 |
| MINIMAX | xyz:MINIMAX | 480,363 | 46.412 | MINIMAX | 2,319 | 46.332 | active | MINIMAXUSDT=109 |
| LITE | xyz:LITE | 449,744 | 882.25 | LITE | 11,561 | 883.17 | active | LITEUSDT=1,343 |
| RKLB | xyz:RKLB | 421,017 | 64.26 | RKLB | 21,436 | 64.36 | active | RKLBUSDT=1,070 |
| UNITREE | xyz:UNITREE | 389,478 | 80.165 | UNITREE | 123,369 | 80.314 | active | UNITREEUSDT=100,034 |
| MRNA | xyz:MRNA | 380,144 | 146.0 | MRNA | 58,018 | 146.22 | active | MRNAUSDT=16,837 |
| CBRS | xyz:CBRS | 371,444 | 211.7 | CBRS | 56,269 | 211.58 | active | CBRSUSDT=647 |
| AVGO | xyz:AVGO | 363,033 | 361.16 | AVGO | **0** | 357.04 | active | AVGOUSDT=13,948 |
| BABA | xyz:BABA | 357,232 | 113.23 | BABA | 150,875 | 113.46 | active | BABAUSDT=318 |
| KORU | xyz:KORU | 319,487 | 23.119 | KORU | **0** | 22.885 | active | KORUUSDT=8,615 |
| ARM | xyz:ARM | 308,944 | 252.37 | ARM | **0** | 252.27 | active | ARMUSDT=22,538 |
| WDC | xyz:WDC | 262,018 | 466.04 | WDC | **0** | 466.62 | active | WDCUSDT=2,617 |
| ZHIPU | xyz:ZHIPU | 247,358 | 142.07 | ZHIPU | **0** | 142.04 | active | ZHIPUUSDT=402 |
| QCOM | xyz:QCOM | 217,225 | 169.04 | QCOM | **0** | 168.51 | active | QCOMUSDT=17,291 |
| BMNR | xyz:BMNR | 168,141 | 25.597 | BMNR | 23,656 | 25.622 | active | BMNRUSDT=4,064 |
| GME | xyz:GME | 165,620 | 19.198 | GME | **0** | 19.199 | active | GMEUSDT=8,376 |
| AAOI | xyz:AAOI | 148,542 | 106.05 | AAOI | **0** | 106.17 | active | AAOIUSDT=350 |
| HYUNDAI | xyz:HYUNDAI | 140,425 | 289.53 | HYUNDAI | 515,564 | — | **inactive** | HYUNDAIUSDT=182 |
| BB | xyz:BB | 91,597 | 7.6863 | BB | 2,988 | 7.6898 | active | BBUSDT=493 |
| ASML | xyz:ASML | 69,330 | 1717.1 | ASML | 391 | 1717.6 | active | ASMLUSDT=2,064 |
| IBM | xyz:IBM | 35,740 | 234.47 | IBM | 19,627 | 234.64 | active | IBMUSDT=19,278 |
| STRC | xyz:STRC | 28,515 | 97.47 | STRC | 2,265,725 | 97.348 | active | STRCUSDT=2,934 |
| NOW | xyz:NOW | 12,993 | 141.4 | NOW | 363 | 141.38 | active | NOWUSDT=6,714 |
| NOK | xyz:NOK | 10,830 | 10.187 | NOK | **0** | 10.070 | active | NOKUSDT=10,779 |

No HL xyz entries in this table are flagged `isDelisted`.

---

## Table B — Crypto perps on all three venues (HL main + Lighter + Aster), excluding BTC/ETH

94 bases matched three-way. Sorted descending by MIN(HL 24h vol, Lighter 24h vol, Aster 24h vol) — top 25 shown. All volumes USD.

| Rank | Base | HL symbol | HL 24h vol | Lighter symbol | Lighter 24h vol | Aster symbol | Aster 24h vol | MIN(3) |
|---:|---|---|---:|---|---:|---|---:|---:|
| 1 | SOL | SOL | 197,969,271 | SOL | 52,712,845 | SOLUSDT | 83,279,459 | 52,712,845 |
| 2 | HYPE | HYPE | 391,941,128 | HYPE | 37,783,267 | HYPEUSDT | 31,296,870 | 31,296,870 |
| 3 | ZEC | ZEC | 407,218,480 | ZEC | 25,169,976 | ZECUSDT | 34,222,778 | 25,169,976 |
| 4 | BNB | BNB | 34,150,263 | BNB | 7,953,691 | BNBUSDT | 30,189,999 | 7,953,691 |
| 5 | UNI | UNI | 63,994,896 | UNI | 5,631,615 | UNIUSDT | 5,983,802 | 5,631,615 |
| 6 | PONS | PONS | 200,424,815 | PONS | 4,784,950 | PONSUSDT | 11,873,722 | 4,784,950 |
| 7 | XRP | XRP | 52,831,803 | XRP | 4,356,214 | XRPUSDT | 31,689,440 | 4,356,214 |
| 8 | LIT | LIT | 53,633,241 | LIT | 33,721,221 | LITUSDT | 3,753,028 | 3,753,028 |
| 9 | ASTER | ASTER | 22,167,109 | ASTER | 3,494,071 | ASTERUSDT | 142,525,219 | 3,494,071 |
| 10 | DASH | DASH | 16,463,298 | DASH | 2,697,444 | DASHUSDT | 3,034,534 | 2,697,444 |
| 11 | PUMP | PUMP | 124,966,533 | PUMP | 8,101,278 | PUMPUSDT | 2,582,796 | 2,582,796 |
| 12 | ARB | ARB | 106,250,244 | ARB | 11,037,572 | ARBUSDT | 2,498,162 | 2,498,162 |
| 13 | NEAR | NEAR | 63,342,634 | NEAR | 5,482,986 | NEARUSDT | 2,016,373 | 2,016,373 |
| 14 | DOGE | DOGE | 36,344,297 | DOGE | 1,725,731 | DOGEUSDT | 31,617,395 | 1,725,731 |
| 15 | SUI | SUI | 9,160,262 | SUI | 1,381,943 | SUIUSDT | 998,762 | 998,762 |
| 16 | CASHCAT | CASHCAT | 21,504,241 | CASHCAT | 1,504,663 | CASHCATUSDT | 961,013 | 961,013 |
| 17 | XMR | XMR | 25,637,428 | XMR | 2,835,131 | XMRUSDT | 959,050 | 959,050 |
| 18 | LINK | LINK | 13,766,180 | LINK | 1,055,094 | LINKUSDT | 941,593 | 941,593 |
| 19 | ENA | ENA | 33,313,934 | ENA | 4,462,656 | ENAUSDT | 909,155 | 909,155 |
| 20 | TAO | TAO | 22,403,156 | TAO | 893,401 | TAOUSDT | 1,777,670 | 893,401 |
| 21 | LTC | LTC | 13,164,543 | LTC | 692,261 | LTCUSDT | 1,595,580 | 692,261 |
| 22 | ADA | ADA | 6,370,720 | ADA | 584,160 | ADAUSDT | 710,049 | 584,160 |
| 23 | WLD | WLD | 9,036,477 | WLD | 1,037,377 | WLDUSDT | 511,736 | 511,736 |
| 24 | PEPE | kPEPE | 8,100,216 | 1000PEPE | 501,592 | 1000PEPEUSDT | 893,183 | 501,592 |
| 25 | AAVE | AAVE | 10,891,242 | AAVE | 472,664 | AAVEUSDT | 808,328 | 472,664 |

Note: `AI` is technically matched on all three venues (HL `AI`, Lighter `AI`, Aster `AIUSDT`) but HL flags it `isDelisted: true` with `dayNtlVlm = 0` — excluded from ranking (falls to the bottom, min=0), not a real trading candidate.

---

## Table C — Crypto perps present on exactly two of the three venues (top 10 by min volume)

Of 94 three-way matches, coverage is not thin, but for completeness: 75 crypto bases (excl. BTC/ETH) matched on exactly two venues. Breakdown: 67 on (HL, Aster) only [not on Lighter], 8 on (HL, Lighter) only [not on Aster]. **Zero** bases were found on (Lighter, Aster) without also being on HL main — HL main has by far the broadest crypto listing of the three.

| Rank | Base | Venues present | Vol @ venue 1 | Vol @ venue 2 | MIN |
|---:|---|---|---:|---:|---:|
| 1 | SUSHI | HL, Aster | HL 3,618,632 | Aster 602,740 | 602,740 |
| 2 | ZEN | HL, Aster | HL 2,456,444 | Aster 312,569 | 312,569 |
| 3 | INJ | HL, Aster | HL 1,874,321 | Aster 283,964 | 283,964 |
| 4 | BOME | HL, Aster | HL 1,034,179 | Aster 224,971 | 224,971 |
| 5 | APEX | HL, Lighter | HL 216,474 | Lighter 230,354 | 216,474 |
| 6 | MNT | HL, Lighter | HL 463,653 | Lighter 215,860 | 215,860 |
| 7 | CAKE | HL, Aster | HL 2,017,998 | Aster 172,932 | 172,932 |
| 8 | HEMI | HL, Aster | HL 3,175,870 | Aster 122,571 | 122,571 |
| 9 | FET | HL, Aster | HL 1,799,048 | Aster 115,904 | 115,904 |
| 10 | ORDI | HL, Aster | HL 134,710 | Aster 111,657 | 111,657 |

---

## Notes / anomalies

- **Lighter volume field**: `orderBookDetails` exists and returns `daily_quote_token_volume` (already USD-quoted, since Lighter perps are USD-margined) plus `mark_price`, `last_trade_price`, `open_interest`. Used directly, no conversion needed.
- **Zero-volume Lighter listings in Table A**: BE, AVGO, KORU, ARM, WDC, ZHIPU, QCOM, GME, AAOI, NOK all show `daily_quote_token_volume = 0` on Lighter despite being listed and `status: active` — likely listed but untraded in the last 24h. These are nominally "on all three" but have no real Lighter liquidity right now.
- **HYUNDAI** is an outlier: Lighter reports `status: inactive` in `orderBooks` but `orderBookDetails` still returns a non-zero `daily_quote_token_volume` (515,564) and no `mark_price` — treat this venue/symbol as unreliable, don't use for capacity sizing.
- **Delisted flags**: HL main dex has 56 `isDelisted: true` entries (e.g. MATIC, RNDR, FTM, MKR, FXS...); HL xyz dex has 15 (e.g. xyz:URANIUM, xyz:ALUMINIUM, xyz:DXY, xyz:VIX, xyz:CORN, xyz:WHEAT, xyz:TTF, xyz:VOL, xyz:KRW, xyz:H100...). None of the delisted HL main names appear in Table B's top 25 except `AI` (see note above, its volume is 0 and it's excluded from ranking). None of the delisted HL xyz names appear in Table A.
- **Multiplier symbols**: confirmed 7 k-prefixed bases on HL main (kPEPE, kSHIB, kBONK, kLUNC, kFLOKI, kDOGS, kNEIRO) all correspond 1:1 to `1000XXX` bases on both Lighter and Aster (both use the same "1000 base units per contract" convention as HL's "k" = 1000x). Only PEPE (kPEPE/1000PEPE) made the crypto top-25 three-way list; none of the others cleared the volume bar for top 25 but they do exist as valid three-way matches at lower rank.
- **Aster quote-asset duplicates**: XAU, SNDK, MU, SPCX list both a `...USDT` and a `...USD1` contract on Aster; volumes are reported separately and NOT summed in the tables above (using the higher-volume one, and noting both). Some other stock tickers may have additional dormant duplicates not surfacing in the 578-symbol ticker list; not exhaustively checked beyond what appears in `aster_ticker24hr.json`.
- **Aster non-perp/inactive noise**: exchangeInfo has 583 symbol entries; 578 are `contractType: PERPETUAL` and 5 have empty `contractType` (excluded/ignored). 20 symbols have `status` other than `TRADING` (e.g. MBLUSDT, TONUSDT, AFEEUSDT...) — none of these landed in the final matched tables above based on spot-check, but not individually cross-verified against every Table A/B/C row.
- **No base-name collisions** were found on any single venue after normalisation (i.e., stripping `xyz:`, `1000`, `k`, and quote suffixes never caused two different original symbols to map to the same base on the same venue).
- Aster **mark price** was not separately fetched for Table A (only `lastPrice` is in `ticker/24hr`); if a true mark price is needed for Aster, a further call to `fapi/v1/premiumIndex` would be required (not made, per the "call each endpoint once" instruction — out of scope here).

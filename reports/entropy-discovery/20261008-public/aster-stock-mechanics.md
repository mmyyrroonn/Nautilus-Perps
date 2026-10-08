> For the complete documentation index, see [llms.txt](https://docs.asterdex.com/llms.txt). Markdown versions of documentation pages are available by appending `.md` to page URLs; this page is available as [Markdown](https://docs.asterdex.com/trading/perpetuals/stock-perpetuals.md).

# Stock Perpetuals

## Aster Stock Perpetual Contracts

Aster Perpetuals now supports stock perpetuals — enabling perpetual futures trading for U.S. and HK-listed stocks, fully settled in USDT. Trade with up to 50x leverage, long or short, with flexible leverage selection at the time of order placement.

To improve trading experience — particularly during off-hours when stock market liquidity is lower — Aster has introduced an **EWMA-Smoothed Mode** for stock perpetuals.

### Mark Price

On Aster Perps, the Mark Price serves as the reference for calculating unrealized PnL and triggering liquidations. Its primary purpose is to prevent unnecessary forced liquidations during periods of abnormal volatility and to ensure fair, stable pricing — distinct from the last traded price, which can be subject to sudden swings or manipulation.

### Index Price

Index prices are sourced from multiple providers including the Pyth Oracle, ensuring a comprehensive and reliable data set for mark price calculations and settlement.

### Standard Mode

The Mark Price is the median of three values:

* Price 1: Price index × (1 + Funding rate × (Time to next funding / Funding interval))
* Price 2: Price index + 5-min moving average of ((Bid 1 + Ask 1) ÷ 2 − Price index)
* Contract Price: The current contract price

The 5-minute moving average is sampled every minute over a 5-minute window:

Moving average = Average of ((Bid 1 + Ask 1) ÷ 2 − Price index)

The median of the three values is selected as the Mark Price. For example, if Price 1 < Price 2 < Contract Price, then Price 2 is the Mark Price.

### EWMA-Smoothed Mode

During low-liquidity periods (overnight, weekends, and public holidays), Aster applies an Exponentially Weighted Moving Average (EWMA) to the last traded price, replacing the raw Contract Price with a smoothed version to reduce the impact of thin-market price spikes.

The Mark Price is the median of three values:

* Price 1: Price index × (1 + Funding rate × (Time to next funding / Funding interval))
* Price 2: Price index + 5-min moving average of ((Bid 1 + Ask 1) ÷ 2 − Price index)
* EWMA Price: (1 − β) · LastTrade(t) + β ·EWMA price(t − 1), where β = exp(−1/1600)

If no trade is filled at a given second, the previous contract filled price EWMA(t−1) is carried forward.

The median of the three values is selected as the Mark Price. For example, if Price 1 < EWMA Price< Price 2, then **EWMA Price** is the Mark Price.

### **Risk Reminder**

Aster reserves the right to adjust trading parameters — including funding rates, minimum price increments, maximum leverage, risk limits, and maintenance margin requirements — at any time and at its sole discretion. Please refer to the [link](https://www.asterdex.com/en/futures/trading-rules/leverage-and-margin) for full trading rules and limitations.

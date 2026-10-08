> For the complete documentation index, see [llms.txt](https://docs.entropy.io/llms.txt). Markdown versions of documentation pages are available by appending `.md` to page URLs; this page is available as [Markdown](https://docs.entropy.io/market-types/equity-perpetuals.md).

# Equity Perpetuals

Equity Perpetuals are cash-settled perpetual contracts referencing publicly listed equities. They extend continuous, leveraged exposure to stocks whose primary venues trade only during limited sessions, remaining live around the clock while the underlying's public market is closed.

An Equity Perpetual is not equity. It does not represent ownership, voting rights, dividends, information rights, or any claim on the referenced issuer. Holders have no relationship with the referenced company.

### Trading Hours and Regimes

Equity Perpetuals trade 24/7 with USDC collateral. The underlying's primary listing venue, by contrast, trades only during its regular session. The oracle, mark, and funding conventions therefore distinguish two regimes:

* **Market hours.** The regular trading session of the underlying's primary public listing venue, per that venue's published calendar. The oracle is pinned to the public reference price.
* **Outside market hours.** All other times, including venue-declared holidays and intraday halts. The oracle is built from the exchange's own book, anchored to the last public print of the session.

Venue-declared intraday halts and stale-feed conditions during the regular session are treated as outside market hours until trading resumes or the feed recovers.

### Per-Asset Parameters

The following are configured per asset:

* $$\ell\_{max}$$ — maximum allowed leverage. Parameterizes the outside-hours mark bounds.
* $$a$$ — depth normalization constant used in the liquidity-driven oracle weight.
* Approved after-hours venue, if any — an external venue deemed sufficient for oracle use outside regular hours, designated per asset.

Per-market values are listed under Assets.

### Foreign-Currency Underlyings

All oracle, mark, and settlement prices are expressed in USD. For an underlying whose primary venue quotes in another currency, all externally sourced prices are converted to USD at the prevailing forex rate before entering any calculation. The equity leg of the last print is frozen in local currency and converted at the current rate, so its USD value floats with the forex rate while the market is closed.

### Lifecycle and Corporate Actions

Equity Perpetuals do not have a scheduled terminal date. A market terminates only on a corporate action that ends the public reference feed:

* **M\&A or delisting.** A completed acquisition or a delisting settles the market to the last price one day before the underlying's final trading day on its primary venue, via haltTrading.
* **Stock splits.** Because position sizes cannot be resized on this venue, the market settles to the last price one day before the primary venue's ex-date via haltTrading. A successor market on the post-split basis may be listed once post-split public prices are observable.
* **Cash dividends.** No adjustment is applied. The oracle follows the public reference price through the ex-dividend date and the perpetual reprices with the underlying.

### Mechanics

* [Oracle Price](/equity-perp-mechanics/oracle-price.md)
* [Mark Price](/equity-perp-mechanics/mark-price.md)
* [Funding Rate](/equity-perp-mechanics/funding-rate.md)
* [Fees](/equity-perp-mechanics/fees.md)
* [Limit Up Limit Down (LULD)](/equity-perp-mechanics/limit-up-limit-down-luld.md)

The market-hours reference price and the after-hours external price described above are sourced and delivered onchain by [RedStone](https://www.redstone.finance/).

***

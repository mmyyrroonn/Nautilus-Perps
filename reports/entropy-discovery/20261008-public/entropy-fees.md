> For the complete documentation index, see [llms.txt](https://docs.entropy.io/llms.txt). Markdown versions of documentation pages are available by appending `.md` to page URLs; this page is available as [Markdown](https://docs.entropy.io/equity-perp-mechanics/fees.md).

# Fees

Fees follow the Hyperliquid HIP-3 fee schedule. Standard maker and taker rates apply, scaled by the HIP-3 deployer multiplier.

| Role  | Base rate (perps) | HIP-3 effective rate |
| ----- | ----------------- | -------------------- |
| Maker | $$0.015%$$        | $$0.030%$$           |
| Taker | $$0.045%$$        | $$0.090%$$           |

Fees on HIP-3 markets are 2x the standard validator-operated perp rate. The protocol fee is split evenly between Hyperliquid and the deployer.

Volume tiers, staking discounts, and referral rebates apply on the same basis as all other Hyperliquid markets. Fee tier is computed across a user's combined 14-day weighted volume across spot, perps, and HIP-3 perps.

When growth mode is enabled on a market, all fees, rebates, and volume contribution scale by $$0.1$$ (a 90% reduction). Growth mode status is listed under Assets.

***

> For the complete documentation index, see [llms.txt](https://docs.entropy.io/llms.txt). Markdown versions of documentation pages are available by appending `.md` to page URLs; this page is available as [Markdown](https://docs.entropy.io/equity-perp-mechanics/funding-rate.md).

# Funding Rate

The funding rate is a periodic payment exchanged between traders on opposite sides of the market. It anchors the perpetual price to the oracle by creating a continuous economic incentive to trade against the dominant imbalance.

* If the perpetual is trading above the oracle: longs pay shorts.
* If the perpetual is trading below the oracle: shorts pay longs.

Funding is peer-to-peer. The exchange takes no fee on funding payments.

### Mechanics

Funding accrues continuously and is paid hourly. The hourly rate is computed from the premium of the perpetual mid against the oracle using the standard Hyperliquid funding formula, scaled by a per-market multiplier $$m$$:

<p align="center"><span class="math">f(t) = m \cdot f_{HL}(t)</span></p>

During market hours, $$m=0.5$$.&#x20;

Outside market hours, $$m=0.125$$.

The funding payment for a position is:

<p align="center"><span class="math">\text{Funding Payment} = Q \cdot O(t) \cdot f(t)</span></p>

where $$Q$$ is the position size and $$O(t)$$ is the oracle price (not the mark) used to convert the position to notional.

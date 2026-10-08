> For the complete documentation index, see [llms.txt](https://docs.entropy.io/llms.txt). Markdown versions of documentation pages are available by appending `.md` to page URLs; this page is available as [Markdown](https://docs.entropy.io/equity-perp-mechanics/limit-up-limit-down-luld.md).

# Limit Up Limit Down (LULD)

For a publicly listed underlying, the external price is the public-equity price, which prints only during the underlying's regular cash session. The perpetual trades continuously, but outside that session (overnight, weekends, and exchange holidays) there is no live external reference against which to validate the internal book. In this window, internal order flow alone should not be able to move the mark arbitrarily far with no external price supporting the move. Limit up / limit down (LULD) bounds the closed-hours excursion of the market to the initial margin fraction.

### Reference Price

Let $$P\_{\mathrm{ref}}$$ be the last published oracle value while the external feed was live, that is, the price at the moment the underlying's cash session closed. $$P\_{\mathrm{ref}}$$ is held fixed for the duration of the non-trading window and does not update from internal flow.

### Band Derivation

Let $$\Lambda$$ be the market's maximum leverage, so the initial margin fraction is $$1/\Lambda$$. The non-trading-hours bands are placed one initial-margin fraction above and below the reference:

<p align="center"><span class="math">P^{+} = P_{\mathrm{ref}}\left(1 + \frac{1}{\Lambda}\right), \qquad P^{-} = P_{\mathrm{ref}}\left(1 - \frac{1}{\Lambda}\right)</span></p>

For example, a market with maximum leverage $$\Lambda = 5$$ has an initial margin fraction of 20%, and its closed-hours market is limited to a 20% band above and below the session close.

### Mechanic

During non-trading hours, trading is permitted only within $$\[P^{-}, P^{+}]$$. Orders that would execute through a band are rejected, and the market rests limit-up at $$P^{+}$$ or limit-down at $$P^{-}$$ until flow re-enters the band or the underlying's session reopens. Because the mark is the smoothed internal price, constraining the book to the band constrains the mark to the band as well.

### Reopen

When the underlying's cash session reopens and a live external price is observable again, the bands are released and the oracle returns to its standard construction on the next tick.

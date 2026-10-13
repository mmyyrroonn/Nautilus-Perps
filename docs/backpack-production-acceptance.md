# Backpack production validation

This change adds finite application commands for Maker entry / reduce-only Market
exit and Market entry / reduce-only Market exit. Financial requests pass through
Nautilus Strategy and the native execution engine. Independent REST cleanup is
outside this PR. The existing native candidate lock is unchanged and lacks the new
production API; use a source-bound wheel from the accompanying native changes and
the explicit [local-override workflow](native-candidate.md).

## Sanitized mainnet observation, 2026-10-13

The user operated the real mainnet run. Native results showed these four legs:

| Group | Leg | BTC quantity | Approximate notional | Observed status | Liquidity |
| --- | --- | --- | --- | --- | --- |
| Maker entry / Market exit | Post-only limit buy | 0.00005 | 4.16 USDC | FILLED, terminal reconciled | MAKER |
| Maker entry / Market exit | Reduce-only Market sell | 0.00005 | 4.16 USDC | FILLED, terminal reconciled | TAKER |
| Market entry / Market exit | Market buy | 0.00005 | 4.16 USDC | FILLED, terminal reconciled | TAKER |
| Market entry / Market exit | Reduce-only Market sell | 0.00005 | 4.16 USDC | FILLED, terminal reconciled | TAKER |

Actual native fill and commission evidence supported the expected Maker/Taker
classification. Subsequent authenticated REST observations showed a flat account
and no open orders. Approximate notionals above are a sanitized description of
observed fills, not a price promise or a future sizing input.

The final wrapper still reported `completed=false` and `account_reconciled=false`.
A preceding standalone cleanup fill, outside the new native session, fell inside
the five-second backward padding of the fresh-session history window and remained
pending attribution. That known harness boundary is retained: the user requested
merging the trading code without repairing the cleanup-to-session handoff now.
The four reconciled native legs and later exchange flatness do not turn the wrapper
result into a passing complete acceptance run. This record does not establish that
all requirements of native issue 123 are closed.

Raw account responses, credentials, venue order/trade identifiers, deployment
details, logs and native state remain local and are excluded from publication.

## Planning and acceptance boundaries

`scripts/backpack_acceptance_plan.py` uses public market/depth GETs to generate
finite command files. It validates an open USDC perpetual, fresh two-sided depth,
venue quantity/price grids and min/max bounds without credentials or a trading node.
Public planning and operator dry runs are not execution evidence.

```text
<app-python> scripts/backpack_acceptance_plan.py --symbol BTC_USDC_PERP --scenario maker-market --max-leg-usdc 4.5 --output-dir <local-plan-directory>
<app-python> scripts/backpack_operator.py --symbol BTC_USDC_PERP --command-file <local-plan-directory>/BTC_USDC_PERP.maker-market.commands.json --max-leg-usd 5 --max-total-usd 5 --state-dir <absolute-native-state>
```

These operator arguments perform validation only. See the
[Maker/Market launcher](backpack-maker-market-acceptance.md) for finite live execution
and original-state recovery. Fresh generation targets 4.5 USDC with valuation
headroom; native admission checks current account, ownership, quote and capacity
again. The 5 USDC cap applies to submission valuation. A Market fill price can move,
and actual fill notional and fees are checked and reported separately.

Complete acceptance requires actual matching fills for both groups, reconciled
terminal economics, a flat position, a later complete account/history/fill recovery
and honest shutdown evidence. Zero entry fills, partial exit fills, unexpected
liquidity, inconsistent economics and unknown outcomes fail acceptance and stop
later entries. Preserve original state and residual evidence before any recovery.

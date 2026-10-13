# Ondo BTC MARKET mainnet acceptance, 2026-10-13

One human-operated mainnet run completed both MARKET round trips on the reviewed
native candidate. The [aggregate summary](summary.json) records actual fills,
fees, account reconciliation and clean shutdown. Original private results and
recovery journals remain gitignored locally; account balances and order/trade
identifiers are excluded from this report.

| Operation | Filled BTC | Fill price (USD) | Reduce-only |
| --- | --- | --- | --- |
| Open long / buy | 0.0001 | 83535 | No |
| Close long / sell | 0.0001 | 83555 | Yes |
| Open short / sell | 0.0001 | 83536 | No |
| Close short / buy | 0.0001 | 83535 | Yes |

The run started at 07:06:25 UTC and completed in 87.406 seconds without a timeout
or recorded failure. All four orders were terminal and fully filled. Native and
application positions were Flat, with zero own/foreign open orders and unknown
submissions. DMS release was acknowledged and native shutdown was clean.

Recorded commissions total 0.00835300 USDC. Reconciled funding is zero; account-wide
wallet and margin changes are both -0.006253 USDC. These account-wide facts are not
attributed as strategy PnL. Flat describes the historical run end, not a fresh
account query.

The source result, wheel, installed native binary, Strategy and launcher digests
are retained in the summary. The run used the launcher before its redundant input
prompt was removed. The subsequent prompt-only revision has offline preparation
and mocked CLI dispatch checks; native and Strategy source are unchanged.

This verifies one BTC long and short round trip, not sustained or cross-venue
trading. MARKET limits use quote estimates and do not bound execution prices.
The candidate remains a local Windows installation. See the
[offline acceptance](../20261013-market-offline/README.md) and
[launch instructions](../../../docs/ondo-mainnet-launch.md).

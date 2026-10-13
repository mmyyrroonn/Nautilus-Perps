# Ondo BTC mainnet startup acceptance, 2026-10-13

One human-operated BTC startup check completed on the reviewed continuous native
candidate. The process returned normally after confirmed Flat. The published
[summary.json](summary.json) contains only allowlisted aggregate evidence; the
original private result and recovery journals remain gitignored locally.

This mainnet result covers one BTC placement/cancel/replacement/IOC/reduce-only
startup sequence. BTC/TSLA follow-up cycles through the same continuous Strategy
remain [installed-wheel loopback evidence](../20261013-offline/acceptance.json).
No sustained or unattended trading policy ran in this mainnet process.

| Observation | Historical result |
| --- | --- |
| Run stamp | 2026-10-13 04:55:39 UTC |
| Process elapsed | 103.953 seconds, rounded to a millisecond |
| Startup / final completion | Both true; no failure recorded or timeout |
| Placement and replacement | One post-only maker order and one replacement; two confirmed cancels |
| Confirmed entry | 0.0001 BTC buy at 83353 USD |
| Confirmed reduce-only close | 0.0001 BTC sell at 83352 USD |
| Native fill commissions | Two fills; total 0.00416800 USDC |
| Same-run funding | 0 USDC; native history reconciled |
| Account wallet / margin change | Both -0.004268 USDC, account-wide facts |
| Final native account | Flat; own/foreign open orders, unknown submissions and late fills all zero |
| Application Cache / Portfolio | No open BTC position; net BTC quantity zero |
| DMS release / native shutdown | Release attempted, frame sent and matching ACK confirmed; shutdown clean |
| Resolved budgets | 20 USD per order, 40 USD Ondo gross, 25 USDC minimum available margin |

Balances and account/order/trade identifiers are omitted. The account-wide cashflow
is not claimed as strategy PnL. Flat and clean shutdown describe this historical
run's end; preparing this report did not query the current account or start a node.

## Candidate and original evidence binding

- Wheel SHA-256: `6d63d32c70e42bef4a5bd783887f889b7c222f18fe14d6ef00025f42edd863de`.
- Installed native binary SHA-256: `53c6ea7e9436b1686297ee5baec62852df9dfafbc553a1483a08b874dbe139e4`.
- Strategy SHA-256: `aef4e2030d6e476feeb72c5651fcbf44ce4b8594f03e1be48de237b20af6a488`.
- Launcher SHA-256: `345fcae2db810e9a3e80a207f04db47dd3b51305db8b8b47f626fbe84abaaf27`.
- Original local `result.json` SHA-256: `2087d935dd0cc0ca406af4f51ed0a83ca1be8d23cc18bc209dc9afdef99b3beb`.

The original source-bound candidate identity was retained by the launcher. The
published summary retains its wheel/binary and application code digests without
copying local account data or full private logs. A subsequent source change,
interpreter move, wheel replacement or new run needs fresh review. This historical
authorization does not authorize another mainnet run.

## Delivery boundary

The candidate is a Windows CPython 3.12.9 local build, rather than a formal portable
release. The launcher intentionally binds its reviewed Strategy, local interpreter,
wheel and provenance; a fresh checkout cannot launch using a PyPI wheel with the
same version number. It stops after startup acceptance and installs no trading
policy. Cross-venue allocation and long-duration journal maintenance remain
application/deferred work. See the [launcher runbook](../../../docs/ondo-mainnet-launch.md).

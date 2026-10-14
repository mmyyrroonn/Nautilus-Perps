# Ondo application/native interface

Reviewed 2026-10-13 for native Issue 122. Runtime support depends on the installed
wheel, not its `2.0.0rc4` version string or the presence of Rust source. The dated
[2026-09-19 required interface](../reports/ondo-acceptance/20260919-production-native/trade-app/required-native-interface.md)
and [2026-09-28 public acceptance summary](../reports/app-issues-20260928/acceptance.md)
remain historical. This maintained navigation carries forward PR #7's useful
contract documentation without promoting its old local candidate.

## Continuous execution

[ondo_continuous.py](../src/ondo_continuous.py) registers the real native
`OndoDataClientFactory`, `OndoExecutionClientFactory`, `LiveNode` and Strategy. The
installed factory must advertise the exact booleans
`supports_continuous_execution=True` and `supports_native_amend=False`, and the
module must export `OndoContinuousExecutionConfig`. A marker is a capability fact,
not authorization or venue acceptance.

`OndoExecutionClientConfig.continuous_execution` accepts the following limits:

| Field | Contract |
| --- | --- |
| `instrument_ids` | Explicit unique configured Ondo perpetual InstrumentIds; native metadata remains authoritative |
| `max_notional_per_order_usd` | Exact decimal string, positive and at most 100 USD |
| `max_gross_exposure_usd` | Exact decimal string, at least the per-order cap and at most 500 USD; allocated Ondo account budget |
| `min_available_margin_usdc` | Positive exact decimal string; authoritative available-margin floor for new risk |
| `max_live_orders` | Positive integer limiting simultaneous prepared/outstanding/unresolved orders |
| `max_requests_per_second` | Positive integer bounding application creates and owned cancels; does not increase the shared REST transport quota |
| `max_unresolved_orders` | Positive integer no greater than `max_live_orders`; unresolved work suspends additional risk |

Continuous writing configuration requires an expected venue account identity,
diagnostic run id, durable journal, DMS timeout in 1..30 seconds and one-second
reconciliation. Production additionally requires `allow_production_orders=True`;
read-only configuration cannot enable continuous writes. The application requires
explicit `execution_enabled=True` for a write-enabled session.
Default construction remains sandbox read-only, with no automatic signal or order.

The native account budget covers owned positions and prepared/outstanding/unresolved
new risk. A shared application allocator must reserve all venue budgets against the
global multi-venue cap; the Ondo adapter cannot inspect another venue's exposure.
Trading thresholds and allocations belong in the application. Changing a limit,
body or identity after approval is not an automatic continuation of old authority.

## Existing bounded probe

[ondo_trade_probe.py](../src/ondo_trade_probe.py) retains the historical
`OndoExecutionEnvelopeConfig` path. It binds one planned instrument, sides,
quantities, worst prices, notionals, counts, entry deadline and absolute cleanup
deadline. `execution_envelope` and `continuous_execution` are mutually exclusive;
continuous admission does not reuse the probe's single-entry price/quantity/count
restrictions.

The bounded probe checks `supports_production_trade_envelope`,
`production_trade_snapshot()` and its required config fields before building a
production writing client. Both modes retain native identity, metadata, margin,
account ownership, exact arithmetic, journal, uncertainty and DMS checks. Native
amend is unavailable: confirmed cancellation and a new client order identity are
required for price replacement.

## Account and lifecycle evidence

`production_trade_snapshot()` is bound to the supplied run token. In continuous
mode it projects account ownership, position quantities, active/unknown orders,
metadata freshness, reconciliation and latest-activity generations, plus separate
`native_ready` and `reduction_ready` facts. New risk can be blocked while an exact
owned reduction remains available. The adapter revalidates dispatch using current
native evidence; a Python callback or cached marker cannot authorize it.

Current native DataEngine quotes reach send-time admission after Cache publication,
including during shared REST budget waits. The original serialized command body,
limit price and order identity do not change. Missing, future or stale quotes
refuse dispatch; the two-second freshness ceiling is retained.

Only native complete/final account and shutdown proof can certify completion.
Application reports keep confirmed fills, Cache/Portfolio quantity, account Flat,
clean shutdown, DMS release and economics separate. `OrderFilled.commission` is
native Money in its own currency; duplicate fills cannot charge it twice. Native
account cashflows remain account-wide balance/funding facts rather than strategy PnL.

`production_shutdown_diagnostics()` returns bounded fixed release classifications
and stage observations from the fresh factory. An attempted release, a sent frame
and a matching ACK are distinct. It must not turn a missing account proof, an
ambiguous update, another run's ACK or a flat read into a clean result. Incomplete
shutdown retains original order identities and journal uncertainty.

## Acceptance navigation

- [Current evidence index](acceptance-status.md): separate current candidate, formal lock and historical reports.
- [Continuous construction and policy](ondo-continuous.md): configuration and startup/cancel-replace integration.
- [Installed wheel loopback acceptance](../reports/ondo-continuous/20261013-offline/README.md): BTC/TSLA Engine/Cache/Portfolio and failure-path evidence.
- [Mainnet BTC startup summary](../reports/ondo-continuous/20261013-mainnet/README.md): one historical human-operated BTC startup sequence, not sustained multi-symbol trading.
- [Mainnet launcher](ondo-mainnet-launch.md): explicit human entry point, pinned local candidate, no automatic retry or policy.
- [Formal candidate workflow](native-candidate.md): maintained source-bound install tooling and no PyPI fallback.

Record source/artifact hashes, interpreter/platform, exact test scope and unresolved
limits for every new candidate. No historical report or capability flag authorizes
a new private write, DMS operation or live trade.

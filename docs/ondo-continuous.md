# Ondo native continuous execution

`src/ondo_continuous.py` constructs a real `LiveNode` with the installed native
`OndoDataClientFactory`, `OndoExecutionClientFactory`, and one reusable
`OndoContinuousStrategy`. It never reads `.env`, starts a node during construction,
signs a request, creates a Python exchange client, or writes an order through Python
HTTP. All order commands use the native Strategy/order factory path.

The default `ContinuousSession()` is sandbox read-only. Enabling execution requires
explicit instruments, account identity, diagnostic run id, durable journal, DMS
timeout in 1..30 seconds and one-second reconciliation. Execution must be explicitly
enabled with `execution_enabled=True`; native production configuration also uses
`allow_production_orders=True`. Local protocol fixtures exercise the production
configuration contract with synthetic credentials.

## Native contract and limits

The installed wheel must expose `OndoContinuousExecutionConfig`,
`supports_continuous_execution=True`, and `supports_native_amend=False`. An old wheel
is refused before node construction. The configuration uses exact decimal strings:

```python
from ondo_continuous import ContinuousLimits, ContinuousSession, build_session

limits = ContinuousLimits(
    max_notional_per_order_usd="20",
    max_gross_exposure_usd="40",
    min_available_margin_usdc="25",
    max_live_orders=2,
    max_requests_per_second=2,
    max_unresolved_orders=1,
)
# Constructing this default session does not permit orders or access a credential file.
session = ContinuousSession(limits=limits)
node, execution_factory, strategy = build_session(session)
# The caller separately controls credential use, authorization, and node.run().
```

Native admission caps per-order notional at USD 100 and the allocated Ondo gross
budget at USD 500. Lower caller limits are passed unchanged. The gross limit covers
the Ondo account's owned positions plus outstanding/prepared/unresolved new risk;
it cannot observe another exchange. A trading application's shared risk allocator
must reserve each venue's allocation against the cross-venue global USD 500 cap.
Setting Ondo's limit to 500 does not establish a global 500 limit across Hyperliquid,
Lighter, Aster and Ondo. User signal thresholds and per-venue allocations belong in
that application, never the adapter. This module accepts the allocated Ondo budget.

`continuous_execution` and the historical one-trade `execution_envelope` are mutually
exclusive. Continuous operation has no fixed entry price, quantity, single-entry
request count or probe deadline. It keeps native identity, current metadata,
whole-account ownership, margin, quote age, journal durability, DMS and unresolved
submission checks for every order. Application request throttling does not increase
the native shared REST transport quota.

Send-time admission observes quotes published by the native DataEngine after Cache
updates. A request waiting for the shared REST budget therefore uses the latest
native mark without changing its original order ID, limit price or body. Missing
or stale quotes still refuse dispatch; the two-second freshness bound is unchanged.

## Startup check and continued policy

`build_session(..., startup_mode="market")` selects a bounded two-direction MARKET
check instead of the default maker check below. It sends buy/open-long,
sell/reduce-only-close-long, sell/open-short and buy/reduce-only-close-short through
the native order factory. It requires native and Cache/Portfolio Flat before each
opening, terminal entry fills plus signed position proof before each close, and
exact fee/fill coverage and confirmed Flat after each round trip. Partial terminal
entries close only their confirmed quantity; partial exits stop the sequence.
Reports include `market_completed_round_trips`, signed opening position evidence,
order types/sides and the observed bid/ask at each submission.

MARKET bodies omit price and time-in-force. Native admission checks current quote
estimates against the caller's notional budgets, retaining the existing metadata,
identity, margin, ownership, quote age, journal, unresolved-submission and DMS
checks. Market fills have no limit-price protection. The new native candidate and
the loopback peer's four actual market request bodies must be verified before
using this mode. See [the launcher runbook](ondo-mainnet-launch.md).

The startup check is a small real execution sequence, initiated only in an explicitly
enabled session:

1. Wait for current native continuous account proof, Flat in native proof and in the
   Strategy's real Cache/Portfolio, and a valid native QuoteTick no older than two seconds.
2. Compute the minimum quantity from native size increment, minimum quantity and any
   published minimum notional. Round upward in base units using Decimal. Check the
   worst protected entry against the caller's notional budget; do not increase it.
3. Place a non-crossing post-only GTC buy and wait for acceptance. Request a native
   cancellation, then wait for the cached order's confirmed terminal zero-fill state.
4. Adjust the price and create a replacement with a new client order id. It is a
   cancel/replacement, never native amend. Confirm the second cancellation before
   continuing. A fill during either cancellation fails the check and preserves
   native ownership; it does not replay the order or guess an offsetting quantity.
5. Send one protected limit IOC entry. After terminal fill consumption and current
   native account reconciliation, close only the confirmed filled quantity with
   reduce-only limit IOC. An observed partial IOC entry can close its confirmed
   terminal quantity; missing fills cannot be guessed.
6. Require fill/commission coverage, real Cache/Portfolio Flat and current native
   whole-account Flat, no own/foreign active order and no unresolved submission.

The same Strategy then enters `phase="continuous"` and calls the caller's
`on_continuous_quote(strategy, quote)` policy. The policy can use
`strategy.submit_limit(..., instrument_id=quote.instrument_id)`, the real order
factory and native commands. All configured instruments are subscribed; after
startup the policy receives every valid fresh quote in that allowed set. Quotes for
other symbols never replace the BTC startup instrument, quote or tick-size context.
Policy orders use their own cached native instrument metadata. No clients
are rebuilt and DMS is not released/rearmed between cycles. There is no default
profit threshold, market-making allocation or automatic post-check trade. The
startup timeout is an application observation bound; it does not widen native
shutdown, absolute safety limits or order permissions.

Reports retain each create/cancel request time, native event timestamps, callback
latency, exact quantities/prices, native trade ids, Money commission by currency,
Cache/Portfolio net position, authoritative account free/locked/total balances,
and the native run snapshot. A venue balance change is an account fact, not strategy
PnL. Completion of this synthetic startup test does not prove real venue liquidity
or long-running reliability.

## Installed wheel acceptance

`tests/test_ondo_continuous.py` starts fresh child interpreters using the actual
installed native extension. `tests/support/ondo_continuous_peer.py` provides authored
synthetic BTC/TSLA metadata, public top-of-book frames, private order/fill reports and
one stateful account on loopback HTTP/WS. It never records authentication headers
or login bodies. The success case proves confirmed post-only cancel/replacement,
IOC entry, exact reduce-only close, fee/Cache/Portfolio/account consumption, then
additional BTC and TSLA IOC entry/close cycles through the same continuous Strategy.
It checks each symbol's actual fills, zero cached positions and Portfolio net quantity,
six native commissions totaling 0.06 USDC, and authoritative free balance 4999.94 USDC.
The cancel/fill-race
case refuses replacement and preserves the journal and incomplete result.

The suite requires four explicit candidate inputs and fails rather than skipping
when they are absent or the wheel/provenance/native capability does not match:

```powershell
$env:ONDO_CONTINUOUS_WHEEL = '<newly-built-wheel>'
$env:ONDO_CONTINUOUS_WHEEL_SHA256 = '<reviewed-wheel-sha256>'
$env:ONDO_CONTINUOUS_PROVENANCE = '<native-provenance.json>'
$env:ONDO_CONTINUOUS_PROVENANCE_SHA256 = '<reviewed-provenance-sha256>'
& '<explicit-new-candidate-venv>\Scripts\python.exe' -m pytest tests/test_ondo_continuous.py -q
```

Run from `E:\persarb\worktrees\issue122-ondo-app` during development, or the final
application checkout after integration. Parent execution sanitizes venue credentials
and disables dotenv discovery in each child. All test endpoints are numeric
loopback; production class configuration in the peer does not contact a mainnet
host. The approved wheel identity, synthetic writes, native shutdown result and
final account/position observations remain available in the child result.

The candidate identity, commands, test results and synthetic order/account evidence
are recorded in `reports/ondo-continuous/20261013-offline/`. That historical report
remains an offline report: BTC/TSLA continuous follow-up cycles were exercised only
against the synthetic peer.

## Mainnet BTC startup observation

The later human-operated BTC startup check on 2026-10-13 completed in 103.953 seconds
on the same reviewed wheel and Strategy. The post-only placement/cancel/replacement
sequence was followed by a confirmed 0.0001 BTC buy at 83353 USD and a reduce-only
0.0001 BTC sell at 83352 USD. Native fill commissions totaled 0.00416800 USDC;
same-run account wallet/margin changes were -0.004268 USDC, with reconciled funding
of zero. Native and application evidence confirmed Flat, zero open/unknown orders,
matching DMS release ACK and clean shutdown.

The process then stopped; it did not run a continuous trading policy. This mainnet
result verifies one BTC startup sequence. Multi-symbol BTC/TSLA follow-up cycles
remain offline evidence, and no sustained trading or multi-venue global allocation
claim follows from the single mainnet run. The cashflow remains an account-wide
fact rather than strategy PnL. See the
[sanitized mainnet report](../reports/ondo-continuous/20261013-mainnet/README.md)
and [human launcher runbook](ondo-mainnet-launch.md). The report records historical
facts; it does not authorize a subsequent run or assert the current account state.

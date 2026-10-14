# Five-venue continuous trading implementation — 2026-10-14

## Delivered behavior

The trading worktree is `E:\persarb\worktrees\opportunity-scan`, based on app
commit `d5e732f46e49198843bf56003707c11b711c6b0e`. The existing local scanner,
episode and trading changes were preserved. The implementation was subsequently
committed locally at the user's request; no push was made. Existing episode
experiments, raw operational artifacts and the unused REST prototype remain local.

Hyperliquid, Lighter, Aster, Ondo and Backpack use one native
`LiveNode -> Strategy -> NativeOrderGateway -> ExecutionClientFactory` path.
Ondo uses its continuous execution configuration; Backpack uses its production
factory with shared public data/quota, durable order identity and economic
acknowledgements. There is no REST mutation fallback in this runtime.

Startup performs BTC minimum-size connection/account verification, passive
submission, price adjustment, cancellation, IOC opening, IOC reduction and final
flat reconciliation on every selected venue. Aster/Ondo/Backpack adjust by
confirmed cancel/replacement. Only a fully passed startup activates continuous
multi-symbol pair trading. Operation timings are persisted in SQLite.

The operator defaults remain:

- Net scanner entry edge at least 30 bp; at most USD 100 per leg.
- Net exit estimate at least 5 bp; after 180 seconds, strictly positive net exit.
- After 300 seconds, independent best-effort partial reductions until reconciled.
- Loss exit at -20 bp, independently disabled with `loss_stop_enabled = false`.
- Maximum gross notional USD 500 and two simultaneous pairs.
- One position per symbol/unordered venue pair, including reverse direction;
  different pairs can share venue legs with separate ownership accounting.

Complete account reads and startup operations have a 20-second deadline. Order
responses retain a separate 10-second deadline. Account evidence uses its true
request-start fence (maximum age 15 seconds); market books must remain at most
two seconds old. The native disconnection deadline is 30 seconds.

Native stop proof is persisted as `native_shutdown` and participates in the exit
code and restart gate. Missing/dirty native account, order, economic or stop
evidence cannot be reported as a successful stop, even with an empty pair ledger.
Unstarted sessions left by configuration failures can retry; interrupted order
ownership is never discarded or resubmitted under fresh IDs.

## Native dependency

Source additions are isolated in `E:\persarb\worktrees\opportunity-five-native`,
based on `f0d34e6f8da875f79c37db079ce201f866a90576`. Native main remains clean.
The native additions are committed as `0ed98a0e3fc37382686efbfeb0809d31944be2b5`
on `feat/opportunity-five-native-controls-20261014`.
The changes expose complete original-ID account evidence, conservative request
timestamps, capability detection and bounded Backpack authority renewal without
changing its original risk limits or permissions.

Installed scanner wheel SHA-256:
`05c69d748e0d7f12ffe23eaa9801f386fcaf9f490360481ff70619e9e482055f`.
Source binding and binary identity were verified. This is a Windows `nextest`
local candidate, not a release-profile or cross-platform certification.
See [native build and validation](../20261014-native-candidate/README.md).

## Validation

- `application-tests.xml`: **604 passed**, no failures/errors/skips. Includes
  policy, shared-leg ownership, independent hard reductions, persistence,
  native account evidence, startup fences, stop proof and recovery gates.
  This broader working-tree sweep also included existing episode/prototype tests;
  those separate experiment files are not included in the trading commit.
- The actual installed five-venue factories, data clients, observer and strategy
  build together without calling `run`, using only deterministic test credentials.
- Dry-run for BTC/ETH selects all five venues, preserves the above risk limits,
  reports readiness for operator startup, and performs no network/state mutation.
- `native-gateway-preflight-junit.xml`: **27 passed** (25 gateway checks plus
  both complete automatic native startup checks), with the shipped 20-second
  account/startup and 10-second order budgets. The 25 gateway checks also appear
  in the application suite above and must not be counted twice.
- Backpack and Ondo each completed all 12 startup operations, including four
  submissions, two confirmed cancels, actual opening/closing fill receipts and
  final account reconciliation. Both produced `native_shutdown.clean = true`,
  no remaining owned BTC quantity, and no unknown/unreconciled orders. End-to-end
  local node times including connection/stop were 6.827 and 100.542 seconds.
- Native candidate checks: **27 Ondo continuous**, **2 Backpack control**, and
  **2 Rust focused** checks passed. These include repeated two-symbol trading,
  owned/foreign order distinction, actual native fill and fee callbacks, authority
  bounds and shutdown behavior. Prior failed clock-boundary assertions remain
  recorded with their correction in the native candidate report.

The complete automatic startup tests use real installed Rust execution clients
and local protocol peers. Their account evidence goes through the same native
snapshot API, independent reader, gateway request fence and `BtcPreflight`; they
do not manually script the preflight intents or use REST writer substitutes.
Both native shutdown proofs are checked after the node stops.
The corresponding `*-automatic-preflight.json` and `*-native-shutdown.json`
files contain synthetic protocol evidence and operation timings. Ondo account
reads took roughly 8–10 seconds locally; that measured latency motivated the
separate 20-second account budget instead of weakening order timeout handling.

## Operator handoff and limits

Use the worktree's `.venv\Scripts\python.exe` and
`config\opportunity-trade.live.toml`. Commands, credential variable names,
state handling and the optional self-test-only switch are in
[the trading runbook](../../../docs/opportunity-trading.md).

No real account connection or mainnet order was performed for this implementation.
Local peers prove application/native integration, not actual venue acceptance,
current trading eligibility, account permission or a profitable execution price.
Runtime net PnL excludes funding and borrowing costs and labels that limitation.
Unknown submissions or incomplete prior startup/stop evidence require original-ID
reconciliation before new trading; the program does not silently reset its ledger.

# Guarded native loopback execution acceptance: 2026-10-02

This is owned signed numeric-loopback synthetic execution only. No credential
value, authentication signature, identity journal or real account data is archived.
Production writes remain unsupported; default account execution remains readonly.
Application source was clean at `587c67cd27d9435545ec4147e0f952df07976993`.
Native candidate source was `caedfc2e21907cd7c61e3ef1ed651578a061c57f`;
installed wheel SHA256 was
`e978feb6c6d0962a329ae94710ed42fe4c85c2fa3afd53019656b9149e73ea20`.

## Finite authority and actual native execution

The actual `src/backpack_loopback.py` CLI exited 0 and independently verified its
source-bound installed wheel. It used the normal native LiveNode, risk/execution
engines and Strategy submit/cancel interfaces. There was no factory monkeypatch,
Python HTTP order client, economic ledger or consumer economic acknowledgement.
Public and account clients shared the actual native REST quota.

- HTTP and WS were paired numeric `127.0.0.1` loopback origins.
- Runtime: 10 seconds; requests: 2 seconds; mutation budget: 1,000 milliseconds;
  receive window: 5,000 milliseconds. Expiry was anchored once for 10,000
  milliseconds and never renewed.
- Account and metadata age limits: 10,000 milliseconds each; quote freshness:
  500 milliseconds. New risk and independently owned cancellation were explicitly
  permitted; position reduction was not enabled in this scenario.
- Exact budgets: 0.01 USDC order notional, 0.02 reserved notional, 1 reserved
  margin, two unsettled orders. Explicit synthetic facts asserted 100 available
  margin, 0.1 margin per notional, 0.001 fee buffer and initial flat position.
  Borrow/lend/repay/liquidation flags were false, complete was explicitly true.
  These are local fixture assertions, not verified venue/account economics.
- Strategy attempts: at most two BUY LIMIT GTC submits, quantity 0.00002 at
  price 100.1 each; at most one independently owned cancellation.
- Startup reconciliation, inflight checks, open-order checks and position checks
  were all explicitly disabled only for this bounded loopback entry.
  `native_inflight_checks_enabled=false`, `continuous_reconciliation=false`.
  Continuous reconciliation is not accepted; this timer restriction does not
  demonstrate repair of the separately tracked terminal-order defect. That fix
  was independently accepted in
  [native PR #79](https://github.com/mmyyrroonn/nautilus_trader/pull/79), closing
  [native issue #78](https://github.com/mmyyrroonn/nautilus_trader/issues/78).
  Expected query evidence gaps were separately addressed by
  [native PR #77](https://github.com/mmyyrroonn/nautilus_trader/pull/77).

## True events and unresolved execution

The peer verified six authenticated GET signatures, one private subscription
signature, one immutable-intent POST signature and one owned-selector DELETE
signature. POST returned 200; DELETE returned 202. Subscription signature checking
is not subscription ACK verification.

After the first order was accepted, the Strategy waited 1,500 milliseconds,
reasserted current synthetic flat account facts without refreshing native market
admission, and attempted a stale-quote probe. Native denied it with
`SUBMIT_FAILED: Backpack execution refused: Readiness`; no second POST occurred.
Actual events retain both quote timestamps, fresh account assertion, metadata
readiness and unexpired authority. The quote was about 1,635 milliseconds old;
metadata age remained below 10,000 milliseconds and authority had about 7,804
milliseconds remaining. Two-order capacity and reserved budgets remained available.

The CLI peer released a true fill and its exact duplicate with a **peer-only fixed
three-second delay after POST**. This was not a realtime Denied-event signal.
Final actual events were independently checked for `Accepted < Denied < Filled`.
Failed timing or economic checks would preserve failure, without replacing the
original run through retry. Installed-wheel tests separately release fills from
an actual Strategy Denied callback.

Native engine/cache observed exactly one trade ID 701, filled quantity 0.00001
and rebate -0.00000100 USDC despite two identical fill frames. The actual position
remained LONG 0.00001 and the order PENDING_CANCEL. `cancel202_observed=true`
comes from the native matched-response `CancelPending` marker.
`cancel_unsettled_observed=true` separately comes from a shutdown count combining
Unknown/Pending/ResponseObserved, which alone does not prove receipt of 202.
HTTP 202 is not terminal cancellation or proof that no more fills can arrive.

`scenario_completed=true` means the finite steps were observed. Execution was
**not settled**: one true fill remained pending durable economic acknowledgement;
the authoritative shutdown was dirty, with one observed-unreconciled order, one
pending cancellation and `positions_unknown_or_nonzero=true`.
`execution_settled=false`, `flat_verified=false` and
`durable_economic_acknowledgement=false` remain explicit. Shutdown completed with
zero active peer sockets. This does not prove a flat portfolio, real-account
readiness or safe production execution.

## Archive and validation

`summary.json` and `events.jsonl` preserve original sanitized CLI observations
byte for byte. `peer-evidence.json` records protocol checks, immutable commands,
synthetic duplicate frames, actual engine observations and native shutdown without
authentication headers, signatures or keys. Archive-local `.gitattributes`
disables text conversion so Git preserves these evidence bytes across checkouts.
`evidence-index.json` binds their
hashes, original CLI/configuration/harness/provenance hashes and frozen source
hashes. It distinguishes Windows working-file CRLF hashes from Git blob hashes;
source equality was verified after newline normalization and the exact runtime
source-content hash was independently matched.

Original artifacts remain under
`E:/persarb/.backpack-artifacts/app23-cli-caedfc2e21/`.
Its identity journal is intentionally not archived. Before source freeze,
115 scoped tests passed, including five actual installed-wheel cases: true
fill/duplicate/202, ambiguous POST without retry or numeric-ID adoption, budget
exhaustion before POST, external cancellation with same-namespace readonly
reopening, and the minimum 8 KiB authoritative shutdown fallback. Ruff F/E9 and
diff checks passed. Root independently executed and validated this final
source-bound CLI candidate. Root also reran the complete eight-file Backpack
suite against this final wheel: **163 passed, no skips**, in 88.31 seconds.
The detailed local log is
`E:/persarb/.backpack-work/app23-primary-final-suite.log`.

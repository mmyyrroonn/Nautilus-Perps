# Startup lifecycle follow-up, 2026-10-16

The startup account-evidence change was merged in application PR [77](https://github.com/mmyyrroonn/Nautilus-Perps/pull/77), merge commit `d992454fa919b8a83ea3f9d96c3c8f9ff2e62ab0`. CI passed. This report records the follow-up implementation and pre-merge validation; deployment acceptance is a separate result.

## Backpack public connection

The generic WebSocket idle timer started as soon as the socket connected, before the application subscribed. Ping/Pong deliberately did not reset this business-data timer. Waiting for other venues therefore caused a healthy, unsubscribed Backpack socket to reconnect every 30 seconds.

The public actor now owns the business-data deadline. It arms after a successful subscription send, advances only on a current subscription's decoded business frame using its original monotonic receipt time, clears when the last subscription is removed, and resets across connection epochs. Heartbeat timeout remains active throughout. Subscription acknowledgements are not claimed: if a sent subscription produces no data, its deadline still triggers recovery.

A loopback regression spans the complete idle window before subscription and after unsubscribe, verifies that business frames renew the deadline, and verifies that silent subscribed streams reconnect despite healthy Pong traffic.

## Backpack shutdown account proof

The durable execution owner previously used its last financial-admission account facts for its shutdown report. An idle startup could have a complete signed flat account snapshot without ever requesting financial admission, leaving shutdown positions unknown. Admission snapshots are also not the right source for an independently invalidated or refreshed account at shutdown.

Before stopping, the native owner consumes terminal/economic evidence and verifies the current authenticated complete account snapshot while holding the private-event gate. It requires no open orders, no unconsumed fills, matching native positions, current revision and no queued private frames. It derives a shutdown-only position proof, with the original observation time, without requiring public quotes or renewing financial authority. A missing, expired, future or invalid replacement proof remains dirty. Original unresolved-order counts and sticky dirty shutdown behavior remain enforced. Repeated framework stop/dispose calls retain the first report.

The old installed wheel reproduces the idle-flat false positive. Installed-wheel regression cases cover idle flat, stale snapshot, foreign open order, nonzero position, invalid policy and policy invalidation after a previously valid admission. These are synthetic local peers and perform no venue requests.

## Ondo native self-test timing

The intermittent native preflight failure reproduced before any order: `native account evidence timestamp/epoch is invalid` during initial reconciliation. CPython 3.12 on Windows uses a coarser wall-clock API than the native timestamp source. A local 10,000-sample comparison measured the precise system clock ahead of `time.time_ns()` by 100 to 1,000,300 ns. Python documents the Windows clock API change in [3.13](https://docs.python.org/3.13/library/time.html#time.time).

The gateway now leaves a structurally ordered but future-completed snapshot pending until its completion time is no later than the poll's sampled time. It does not rewrite evidence, accept future timestamps, restart the account request or relax its deadline. A persistent future timestamp still expires the original request. The reader retains its strict timestamp, identity, generation and scope validation. This protects both native account adapters at the same boundary.

## Validation

- Application strategy/recovery/gateway selection: **403 passed**; separate account-reader/preflight selection: **127 passed**. The earlier 58-test timing selection overlaps the 403-test selection.
- Backpack Rust: **340 passed** (214 library, 18 public client, 51 execution-owner, 57 execution-client). This includes subscribed silence, unsubscribe disarming, missing heartbeats without subscriptions, fresh/expired/future/nonzero shutdown proof, and sticky repeated stop.
- Installed Release wheel, complete Backpack production-peer module: **22 passed**, including all six shutdown cases. The idle-flat case failed against the old wheel and passes with this build.
- Installed Release wheel, complete native BTC preflight module: **2 passed** (Backpack and Ondo). The Ondo timing fix also passed three consecutive standalone runs before replacing the old wheel.
- Targeted Rust formatting and both repository diff checks passed.

Two stale test assumptions surfaced during broader regression and were corrected without relaxing production guards. The strict synthetic market-order acknowledgement now explicitly returns `postOnly=false`, although the request correctly omits that unsupported market parameter. The close-only replay test preserves the entire original fill report and durable receipt while allowing newly recorded REST-source timestamp evidence; restart must restore the complete enriched record exactly. Native reconciliation tests independently require this multi-source evidence behavior.

The application environment now contains the source-bound local **Release** wheel at `.native-cache/startup-lifecycle-final-20261016/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`:

- Wheel SHA-256: `4ac0bc04726b6e18e83e2c7ca46bd065e8e4c32d4aae5f01747247a2d8ffe837`.
- Native base: `502efebcb21f4ca55f714e5f6267d89cb25eb4b5` plus the six local modified files. Pre/post source fingerprints match; installer strict source binding passed.
- Provenance: `.native-cache/startup-lifecycle-final-20261016/native-provenance.json`; installed identity: `.native-cache/installed.json`.
- Raw local evidence: `E:/persarb/_tmp/startup-audit-20261016/`, including the pre-fix reproductions and final test logs.

The application branch is `fix/native-lifecycle-followup-20261016`. At this validation checkpoint, no Linux wheel had been built and no live deployment, process restart or venue order had been performed. The user subsequently authorized commits, pull requests, merging and deployment to `vultr-jp`.

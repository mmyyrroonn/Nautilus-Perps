# Lighter balance refresh investigation, 2026-10-19

## Failure and trade accounting

The Vultr EU process started at 11:11:46 UTC (PID 10635). It remained alive
with no positions or pending orders, but new entries were paused by the
session loss monitor with `LIGHTER: balance_stale_or_invalid_time`.

The four current-session fills belong to one APT entry attempt and its
imbalance cleanup. Lighter bought 61.83 at 0.765 and 68.22 at 0.7649; the
Hyperliquid IOC sell was rejected because it could not immediately match.
Lighter then sold 61.9 at 0.7642 and 68.15 at 0.7644. Confirmed fees were
zero. Buy cash was USD 99.481428 and sell cash was USD 99.397840, giving
**USD -0.083588 realized net**. There were no pending economics rows.

The last Lighter order boundary was 1792408539394499677 ns. The historical
runtime did not persist account-state events or log their timestamps, so
the precise rejected cached timestamp could not be recovered. The startup
precheck timestamp is independent evidence and must not be presented as the
later native cache timestamp.

Source inspection established that the native `query_account` implementation
only replayed its cached account event. Repeated application queries therefore
could not request a new observation after the trade boundary. A synthetic
reproduction of the monitor confirmed that the old cache remains rejected
and a genuinely fresh observation passes; that reproduction is not a
measurement of the missing live cache timestamp.

## Change

- Refresh the authenticated `account_all_assets` and `user_stats` subscriptions
  on a balance query. Coalesce concurrent queries and bound the refresh by
  the existing WebSocket timeout. Do not touch order/position/trade channels
  or send order writes.
- Track each stream's observation time. The merged account event uses the
  older component time; a new margin frame cannot make old balances fresh.
  Full asset snapshots replace the set, while partial updates retain omitted
  balances and their conservative age.
- Preserve the application's post-activity freshness and currency-coverage
  checks. Record bounded, sanitized diagnostics for observation time, required
  boundary, read time, query time and timestamp failure category. Update these
  on query cadence or a result/boundary change, rather than every 100 ms.

The two streams describe the same account. Per-asset balances and margin
summary fields are merged into one state; their equity is not added twice.

## Validation and deployment evidence

- Windows application regression against the existing Windows binary: 300 passed, including balances, loss monitor,
  live runner, execution, batching, reconciliation, restart and startup evidence.
- Native Lighter unit tests: 1,178 passed, 3 preexisting ignored tests.
- Native execution integration: 105 passed, 2 preexisting ignored tests.
  The new loopback case verifies fresh post-query observations from both
  balance streams, concurrent-query coalescing and absence of order writes.
- Production-library Clippy with Python features and `-D warnings`: passed.
- Linux application regression against the new installed wheel, under the
  loopback-only network guard: 858 passed, zero failures/errors/skips.
- Real Lighter read-only probe: three consecutive balance queries each produced
  a merged observation after the query boundary. Completion was about 500 ms
  per query, including two subscription refreshes and 100 ms reader polling.
  No order methods were called; the probe shut down cleanly.

Base application commit: `a87b9df585f3c3c9de6551618041b00ccc98f86e`.
Base native commit: `7a156f7ed2d7c3d72d5fc3c36d7f72c296e021ba`.
The deployed artifact was built from a local source overlay on these bases;
the base commits alone do not contain the fix. The validated overlay hashes
are recorded in `validated.json` in the artifact directory below.

Build and validation artifacts are under
`E:/persarb/_tmp/vultr-eu-20261019/lighter-balance-fix/`.
The server staging directory is
`/opt/persarb/deployments/eu-lighter-balance-20261019/`.
Linux wheel SHA-256:
`76624f1b7ce9ba197d5c7a9dda1a512c0ba4dc397071b009b1ce98ccc4134650`.
Native source fingerprint:
`dbb2e096b28eda46453e3d70e186df2e365d3c09f195f03cca905f5263d991ca`.
The controlled builder verified unchanged pre/post sources. Server staging
independently reconstructed the same fingerprint and installed the wheel via
`scripts/install_native.py`; dependency checks passed.

Production switched at 13:10:04 UTC (21:10:04 CST), new PID 15904. The old
process had zero positions and pending orders before/after stop. It exited
with status 1, while the persisted native shutdown proof was clean for both
Backpack and Ondo. Configuration hashes, service unit, ledger and existing
file permissions were preserved. The old wheel/source and a remote-only
ledger backup remain in the deployment backup path recorded by `deployed.json`.

The first new process timed out after 300 seconds in native client readiness,
before any balance-read callback or trading stage. It reported
`clients_not_ready` and clean read-only shutdown. Retrying the identical
deployment at 13:16:24 UTC started PID 17083. All five startup balance reads
passed at 13:16:32 UTC. No configuration or guard was relaxed for the retry.

Fresh account recovery and the account gate passed at 13:19:01 UTC. The first
600-second periodic loss check completed at 13:29:06 UTC, with all five venue
balance diagnostics passing, no remaining entry pause and no halt reason.
This run had no new fills, open positions or pending orders at acceptance.
The periodic Lighter read accepted a cached observation newer than the
required account boundary; this quiet-account check is not independent proof
of a post-query refresh. The separate three-query real probe above tested
that stricter boundary explicitly.

The post-baseline observation window ran from 13:19:09 to 13:29:25 UTC
(616 seconds, 42 samples). RSS was 455.4 MiB initially, 493.8 MiB at the end,
and ranged from 451.2 to 494.8 MiB, with zero swap. The sampled data-event
queue had median 98, p95 452, maximum 1,007 and final depth 98; intervening
samples returned to zero. These are sampled depths, not event-level peaks.
No sustained queue accumulation or memory exhaustion appeared in this window;
the short observation does not rule out a slower memory leak.

At acceptance, ready-book counts were Hyperliquid 149, Lighter 108, Aster 160,
Backpack 67 and Ondo 9. The remaining three configured Backpack subscriptions
were not counted as ready; their existing readiness guards remain in force.
No diagnostic sampling errors were recorded. Sanitized evidence is saved in
`periodic-acceptance.json`, `runtime-summary.json`, `health-latest.json` and
`health-history.jsonl` in the artifact directory above. The live service and
existing background memory monitor remain running.

Deployment validation did not build a new Windows wheel, run the formal
two-platform candidate workflow or change the published native lock.

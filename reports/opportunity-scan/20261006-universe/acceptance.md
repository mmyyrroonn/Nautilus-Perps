# Multi-symbol public-feed acceptance — 2026-10-06

Issue: [#34](https://github.com/mmyyrroonn/Nautilus-Perps/issues/34).
Worktree: `E:\persarb\worktrees\opportunity-scan`, branch `feature/opportunity-universe`.

## Registry

Public market catalogs were fetched once on the user-provided `dg-us` host. The
generator selects explicit active crypto classifications, exact base identities,
and at least two venues per canonical symbol. It does not merge unverified
`k`/`1000` units. All five public HTTP endpoints responded successfully.

| Venue | Five-venue registry | Linux profile | Fresh book observed in Linux check |
|---|---:|---:|---:|
| Hyperliquid | 149 | 147 | 147 |
| Lighter | 109 | 109 | 109 |
| Aster | 161 | 160 | 160 |
| Ondo | 9 | 9 | 9 |
| Backpack | 70 | excluded from native profile | public depth messages on 70 |
| Total market mappings | 498 | 425 | 425 native + 70 separate public WS |
| Canonical symbols | 167 | 164 | configured count; simultaneous comparison is lower |

The checked-in TOMLs contain identities, not quote history. Fees and USD stablecoin
parity are explicit assumptions. Aster general crypto uses 4 bps and the frozen
published Group B list uses 10 bps. The prior small sample's0.9 bps crypto assumption
was corrected. Account tiers and actual order execution were not verified.

## Actual public feeds on dg-us

The isolated directory is `/root/opportunity-scan-20261006`. The host has 2 vCPUs
and about 3.82 GiB RAM. CPython 3.12.9 was installed with pinned uv 0.12.6 and locked
application dependencies. The source-bound published Linux wheel is
`native-candidate-eeeb8eefc7`, source commit
`eeeb8eefc7921b7f81cb611cb3c851c665cd1ff1`, wheel SHA-256
`c8a09a3e9c04e735a8b3177fe2d85925afaf56ecaa2658089a6834c66c4dfa73`.
It exports Hyperliquid/Lighter/Aster/Ondo and does not export Backpack.

1. [BTC/ETH smoke](smoke-four-venues.json): 90 seconds, 8/8 markets observed fresh.
2. [Initial full-universe attempt](universe-four-venues.json): incomplete. Concurrent
   Aster 1000-level initial snapshots exceeded the HTTP budget and caused a
   temporary IP ban. Freshness degraded while processing queued updates.
3. [Paced full-universe check](universe-four-venues-paced.json): 240.183 seconds,
   all 425 markets observed fresh, actor started successfully with no callback
   failure. No snapshot throttle errors were observed in this repeat.
4. [Backpack public WS check](backpack-public-websocket.json): all 70 configured
   markets emitted depth messages,225150messages,0error messages. This checks
   the public transport, not the absent Linux native adapter or its continuity.

The repeat explicitly requests 100-level Aster snapshots, starts one Aster market
each 500 ms, and publishes current books for evaluation at a configured 100 ms
interval. Every native delta is still applied. Dirty legs are published as a
batch before any cross-venue evaluation; source/receive timestamps are preserved.
Native ordered Decimal maps avoid Python Fraction conversion per level, and
bounded current price-key sets avoid cloning entire ladders just to count levels.
An exact best-quote fee/reserve bound rejects spreads that cannot meet the entry
threshold before computing depth fills.

The successful repeat observed:

- peak simultaneously fresh markets: 413/425;
- peak symbols with at least two fresh, time-aligned legs: 100/164;
- average process CPU: 69.78% of one CPU, or 34.89% of the two-vCPU host;
- highest refresh-interval process CPU: 98.8% of one CPU;
- peak RSS: 381403136 bytes, about 363.7 MiB;
- largest observed receive-to-publish age: 405.133 ms, including coalescing;
- final sampled fresh books: HL 147, Lighter 74, Aster 156, Ondo 0.

Fresh coverage fluctuates with source updates and configured 2 second age/500 ms
pair-skew limits. “Ever fresh” does not mean all legs remained fresh continuously.
The duration includes bootstrap and staggered subscriptions. The reported CPU
and memory include the connection-check collector; they are short-run evidence,
not a long soak capacity promise. Shutdown generated native Ondo unsubscribe
diagnostics after its client disconnected; the observer returned without failure.

The checks registered public data clients only, disabled event recording, retained
zero historical book samples, and created no opportunity directory or CSV. These
checks do not assert that an arbitrage opportunity or real fill occurred. The
native dispatch queue is unbounded; application book limits alone do not establish
an end-to-end memory bound during a prolonged processing backlog.

## Regression validation and scope

257 tests passed locally in the isolated Python environment, covering scanner
core/config/runtime/discovery/health, seven actual native coalescing/pacing
regressions, Backpack public lifecycle and candidate verification, and reused
public client factories. Tests cover mixed-old/new-leg false records, evaluation
deadlines, partial batch invalidation, metadata/status deferral, stale dedup onset,
price-key deletion/reset, exact threshold equality and disabled recording.

The Linux connection run preceded the final stale-hidden dedup cleanup; that
cleanup affects event deduplication and is covered by the local native regression.
No native adapter source, private endpoint, account state, order, or wheel
promotion was changed. The original dirty application checkout was left intact.
The four-minute check is complete; long-duration restart/reconnect soak and a
Backpack-equipped Linux native candidate remain unverified.

## Reproduce on dg-us

From the tested application directory, using its installed `.venv`:

```bash
.venv/bin/python src/opportunity_connections.py --config config/opportunity-scan.dg-us.toml --duration-secs 240
.venv/bin/python src/opportunity_scan.py --config config/opportunity-scan.dg-us.toml --duration-secs 0
```

The first command saves no opportunities. The second records only qualified
opportunity events and their paired current books; `--no-record` also disables
those event writes. Linux Backpack must use a separately available native build;
HTTP/WebSocket reachability is not a substitute for that build's acceptance.

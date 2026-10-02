# Backpack bounded native public load: 2026-10-02

Application issue #21; parent #5 and native fault acceptance
[mmyyrroonn/nautilus_trader#21](https://github.com/mmyyrroonn/nautilus_trader/issues/21).
This short synthetic numeric-loopback run uses the installed native LiveNode,
normal adapter factory and public observer. It never opens a private client,
reads credentials, writes an identity journal or contacts a venue.

## Pinned evidence

- Application source: `5dc9b4a` (full SHA and source-content hash in the report).
- Native source: `e738cd58d31c54e79de859672af37e9002162e5a`.
- Windows CPython 3.12 development wheel, `nextest` profile; SHA256
  `7ddb1ccc4bbe780f71520c7bc134b2e1b593162c0679f1f87cbb5280ee82f9ce`.
- [Measurement report](load-summary.json) records the binary/stub hashes,
  configuration hashes, application identity, harness hash and hashes/byte sizes
  of each original events/summary artifact retained under
  `E:/persarb/.backpack-work/public-load-5dc9b4a/`.

Each of three sequential cycles offers 600 additional quotes at 100 quotes/s
for six seconds, plus the initial shared-fixture quote. Each run has an
8-second runtime budget. The fixture also sends one trade, one mark and a
bounded depth snapshot/update. Each cycle uses one WS connection and two public
GETs, and finishes with zero active peer connections and confirmed native close.
No quote is dropped in these three completed cycles.

| Cycle | Result    | Offered / observed quotes | Monotonic p50 / p95 / p99 (us) | Resident first / last (MiB) |
| ----- | --------- | ------------------------- | ------------------------------ | --------------------------- |
| 0     | completed | 601 / 601                 | 317.0 / 640.1 / 755.3          | 68.55 / 136.54              |
| 1     | completed | 601 / 601                 | 319.8 / 667.6 / 829.1          | 137.23 / 138.52             |
| 2     | completed | 601 / 601                 | 323.0 / 690.6 / 889.7          | 138.53 / 140.14             |
| 3     | failed    | 74 / 69                   | 343.3 / 714.5 / 860.8          | 140.41 / 140.71             |

Cycle 3 deliberately limits the report to 80 records, including health and
non-quote observations. It stops early, reports `failed / report_limit`, records
dropped observations and closes its transport about 234 ms after the first
limit signal. Offered quotes can exceed observed quotes during shutdown.
The harness verifies the configured byte/event bounds against actual files.

## Defect and metric limits

The load scenario exposed a real runner bug: reaching the report limit set a
stop reason but still returned `completed`. The [retained pre-fix summary](before-fix-report-limit.json)
shows that inconsistent result. The runner now returns `failed / report_limit`,
with an actual installed-LiveNode regression. All 11 focused public tests passed.
The same task updates an outdated CLI-message assertion from the replay task.

The headline latency uses one shared monotonic clock from fixture send to the
actual Python domain observer; it excludes the initial fixture quote. Signed
native-receipt/Python-wall deltas are retained separately. Those clocks differed
by up to about 0.92 ms in this run, so negative values are not clamped or treated
as meaningful hop durations. No private ACK or production latency is measured.

The first cycle includes lazy engine initialization. Later working-set growth
and the short sampling interval cannot prove absence of a memory leak. These
measurements include allocator retention and bounded report buffers. Long
soak, journal growth, execution/recovery latency and multi-account/venue
isolation remain separate acceptance items.

## Reproduce

Use an interpreter with the pinned wheel and application test dependencies:

```powershell
.venv/Scripts/python.exe scripts/backpack_public_load.py `
  --output-root E:/persarb/.backpack-work/public-load-repeat `
  --candidate-wheel E:/persarb/.backpack-artifacts/replay-e738cd58d3/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl `
  --candidate-sha256 7ddb1ccc4bbe780f71520c7bc134b2e1b593162c0679f1f87cbb5280ee82f9ce `
  --native-provenance E:/persarb/.backpack-artifacts/replay-e738cd58d3/native-provenance.json `
  --cycles 3 --messages 600 --rate 100
```

The harness caps cycles, messages, rate and offered time; it reuses the existing
synthetic peer and requires explicit candidate verification. It does not
provide a production benchmark or establish execution readiness.

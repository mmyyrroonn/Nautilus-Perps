# Issue 96: bounded book invalidation evidence

Application baseline: `a95805e` (PR #97), isolated worktree
`E:\persarb\worktrees\issue96-book-diagnostics`.

PR #98 (`033881e`, issue 95 entry freshness/deadlines) landed during validation.
The PR incorporates that main update, retaining its publication ownership and
invalidation cleanup alongside this separate diagnostic history. The performance
comparison below isolates issue 96 against its original `a95805e` baseline.

## Diagnostic contract

With CLI timing enabled before state construction, each configured market owns
one small lifecycle record and at most one reference to its last complete,
nonempty published `BookSnapshot`. This reuses the immutable configured-depth
snapshot; it neither clones the native ladder nor claims full exchange depth.
When timing is disabled, there is no history map or additional ladder copy.

Native entry callbacks distinguish uninitialized, snapshot rebuilding, partial
update, pending publication, disconnect, connection-epoch change, invalid
metadata, empty side, staleness, halt, shutdown, observer failure, and other
invalid evidence. Reasons come from observed application events or validity
checks, not inferred network or exchange behavior.

`last_complete` exports a fixed scalar summary of retained depth at the original
order limit, source/receive/publication wall timestamps, and the first observed
invalidation reason/time for that retained snapshot. Repeated invalidations do
not move that time. It is explicitly `historical_invalid` and `diagnostic_only`.
Freshness expiry reports the computed deadline with `freshness_deadline` rather
than pretending a callback observed an invalidation at that instant.

`local_pending` contains only F_LAST-complete batch metadata: source, receive,
completion timestamps, sequence and side counts. Its publication time is null,
and its evidence state is `complete_unpublished_metadata_only`. No pending price
ladder or depth is read or exported. A partial batch clears pending evidence;
missing published depth is never described as an empty exchange book.

The trading book map, publication cadence, validity gate, order transitions,
account state and dispatch rules are unchanged. History and pending evidence
cannot satisfy admission. JSON export keeps an explicit field allowlist and the
existing limits of 1,024 entry traces and 16 native events per trace. No new I/O,
locks, unbounded queues, native payloads or credentials enter the market path.

This instrumentation cannot retrospectively establish the cause of the TIA/CRV
missing-book callbacks on 2026-10-20.

## Validation

- After merging PR #98, focused diagnostics/admission/live-runner regressions:
  **387 passed**; exact pure-Python CI command: **813 passed, 76 skipped**.
  The invalidation test also verifies that issue 95 publication ownership is
  removed while issue 96 historical evidence remains diagnostic-only.
- Focused lifecycle, callback, admission, runtime, coalescing, gateway and timing
  regression: **216 passed**. The 25 new cases include CLEAR alone, fragmented
  snapshots, snapshot replacement without CLEAR, complete empty sides, delayed
  publication, partial increments, disconnect/recovery, metadata changes,
  Backpack health causes, historical bounds, disabled copy behavior, and
  submitted/rejected callback interleaving with unchanged orders/accounts.
- Exact pure-Python workflow command, CPython 3.12.9 and pytest 9.1.1:
  **774 passed, 76 skipped**. Skips require the intentionally absent native
  dependency. Both diagnostic test modules are included in PR CI.
- Wider opportunity/terminal regression with the installed native dependency:
  **1,279 passed, 2 failed** before the final empty-side test was added. One
  failure lacked candidate identity environment variables because this wider
  run used pytest directly; the integration runner supplies them. The other
  is the existing Backpack assertion that every native callback has a valid
  local timestamp.
- The unmodified `a95805e` baseline was rerun through `test_integration.py` with
  candidate identity: Ondo passed; the same Backpack timestamp assertion failed
  (**1 passed, 1 failed**). This change does not modify native timestamp handling.
- The issue 96 branch was also rerun through the same integration runner:
  **1 passed, 1 failed**, with Ondo passing and the identical Backpack timestamp
  assertion failing. This confirms the initial missing-identity failure was a
  direct-pytest invocation issue, while the timestamp failure is present on both
  revisions with the installed local candidate.
- Installed wheel/binary and source binding verified with
  `scripts/verify_native_install.py`: SHA-256
  `c545e32f799d5a9022adf079e192d9b80eadc5baac5720bdb5b7271a555f8d1d`.
  The existing local candidate records native `ec2f4c3` plus three Backpack
  source modifications; no native rebuild or installation was performed here.
- `git diff --check` passed. No mainnet execution was performed.

## Performance

Reproducible local benchmark:

```powershell
& E:/persarb/Nautilus-Perps/.venv/Scripts/python.exe scripts/benchmark_opportunity_book_diagnostics.py --root E:/persarb/worktrees/issue96-baseline
& E:/persarb/Nautilus-Perps/.venv/Scripts/python.exe scripts/benchmark_opportunity_book_diagnostics.py --root E:/persarb/worktrees/issue96-book-diagnostics
```

Windows, the same verified native wheel, 20 levels per side, 1,000 warm-up calls
and 10,000 measured calls per operation. Final baseline/current runs were
sequential after the test processes finished. Timing-enabled comparisons enable
the pre-existing timing recorder on both revisions.

| Timing | Operation | Median before/after (us) | p99 before/after (us) | Calls/s before/after |
| --- | --- | --- | --- | --- |
| off | incremental update | 2.0 / 2.0 | 2.1 / 2.1 | 475,812 / 472,451 |
| off | snapshot replacement | 19.1 / 19.1 | 25.1 / 21.3 | 51,862 / 51,939 |
| off | publication | 25.5 / 25.9 | 30.1 / 32.8 | 38,768 / 37,963 |
| on | incremental update | 3.6 / 4.1 | 6.5 / 6.5 | 255,713 / 235,726 |
| on | snapshot replacement | 22.1 / 24.3 | 48.2 / 44.3 | 41,403 / 39,832 |
| on | publication | 27.1 / 29.2 | 56.1 / 53.6 | 33,263 / 32,694 |

Enabled diagnostics add bounded scalar/reference work; this run measured up to
0.5 us extra median incremental cost and 2.2 us replacement cost. Disabled
increments were unchanged at the timer resolution; publication increased 0.4 us.
These are local microbenchmarks, not exchange end-to-end latency or live-load
throughput guarantees. Tail variation includes scheduler and timing-recorder
noise; the lower p99 values are not claimed as an optimization.

After warm-up, 10,000 additional replace/publish cycles retained 28 extra Python
bytes with timing off on both revisions, and 8,180 bytes with timing on on both
revisions (including the pre-existing bounded timing samples). Traced peak was
23,044 bytes off on both revisions and 119,415 / 121,255 bytes on. Tracing begins
after state construction, excluding the per-configured-market lifecycle objects.
`tracemalloc` does not measure native allocations. Process CPU averages are included in the
benchmark JSON, but the Windows CPU clock's coarse resolution limits their
precision for these short operations. End-to-end and mainnet CPU/latency were
not measured.

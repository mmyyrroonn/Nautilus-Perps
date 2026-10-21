# Issue 95 acceptance

Worktree: `E:/persarb/worktrees/issue95-entry-freshness`.
Base: `a95805e8a2d7dcdd4fcc1ea4437eaf6963820fac`, including the Aster leverage gate.
The main working directory's pre-existing modifications were preserved.

## Historical and boundary replay

The fixture extracts eight original send checks from the four pairs in the
2026-10-20 incident. Its source-file SHA-256 values identify the original
`session-analysis.json` and `session-analysis-evidence.json`. No account secret,
private payload or inferred venue arrival snapshot is included.

The measurements below are from the **first send check** of each pair, which
already inspected both books. The issue's HL-only ages refer to the slightly
later HL leg check; the two observations are not interchangeable.

| Pair | HL source age (ms) | Source skew (ms) | Candidate to check (ms) |
| --- | ---: | ---: | ---: |
| TIA | 572.516181 | 393 | 13.061342 |
| CRV | 525.662781 | 331 | 37.341561 |
| STX | 671.374151 | 397 | 15.551855 |
| JUP | 533.164214 | 332 | 29.370892 |

| Isolated timing policy | Checks blocked / 8 | Pairs blocked before first send / 4 |
| --- | ---: | ---: |
| Historical 2000 ms age, 500 ms skew | 0 | 0 |
| Source age 500 ms only | 8 | 4 |
| Source age 600 ms only | 2 | 1 |
| Source age 750 ms only | 0 | 0 |
| Source skew 250 ms only | 8 | 4 |
| New default measured timing gates | 8 | 4 |

Historical publication timestamps are unavailable, so a full new-policy allow
cannot be asserted. The table projects measured gates only; the source-age and
skew violations are independently sufficient to refuse these four pairs before
any send. Actual receive ages at first checks were 67.98–247.09 ms and candidate
delays were 13.06–37.34 ms; neither alone explains all four misses.

Synthetic replay checks all five thresholds at minus one, exactly zero and plus
one nanosecond relative to the boundary. A separate publication sweep holds
source-to-receive at 350 ms and post-publication work at 20 ms: publication waits
of 0/20/100 ms pass; 150 ms exceeds the final source-age budget, and 151 ms also
violates the publication budget. The report records thresholds, actual times,
each violation and result for every case.

Full gateway-coordinator tests additionally cover changed price/depth/fees,
slow preparation/release/economic checks, missing or future publication clocks,
callbacks older than actual wall time, refreshed books without deadline renewal,
one-leg dispatch followed by expiry, original-ID native queries and confirmed
single-leg reduce-only recovery. Slippage cases recalculate both leg prices,
fees and net return; the live configuration retains zero extra slippage.

## Validation

- Broad opportunity regression selection: **1228 passed in 18.56 seconds**.
  All `test_opportunity_*.py` except `_native.py`/`_native_` names, plus native
  gateway/account-reader unit tests and execution-terminal reliability.
  JUnit: `E:/persarb/tmp/issue95-regression.xml`.
- Final targeted selection after the current-wall-time recheck: **277 passed
  in 4.65 seconds**, including all **39** new admission tests, entry evidence,
  Backpack admission, native entry diagnostics, Aster leverage, timing and runner.
  JUnit: `E:/persarb/tmp/issue95-final-targeted.xml`.
- Exact pull-request workflow selection (29 files): **854 passed in 15.43
  seconds**, with no skips. JUnit: `E:/persarb/tmp/issue95-ci-selection.xml`.
- `git diff --check`: passed. New tests are included in the existing pure-Python
  pull-request workflow.

## Performance and storage

The checked-in benchmark alternates baseline/candidate order across three runs
of 3000 pairs, with two send checks per pair. Median results on this Windows
CPython 3.12.9 host, with optional timing disabled:

| Metric | Base | Candidate |
| --- | ---: | ---: |
| p50 per pair | 398.6 us | 440.5 us |
| p95 per pair | 423.9 us | 455.6 us |
| p99 per pair | 527.5 us | 570.9 us |
| CPU per pair | 401.04 us | 437.50 us |
| Pairs/second | 2481.16 | 2250.83 |
| Retained traced bytes after 100 pairs and GC | 480 | 7336 |

The added checks/evidence cost about 42 us per pair at p50 and 43 us at p99;
microbenchmark throughput falls about 9.3%. Retained memory adds about 6.7 KiB
for the two current order decisions. Publication adds one bounded dictionary
replacement per complete published book, no ladder copy or per-delta history.
Candidate timing adds a monotonic read and two pending ID references. No new
network call, native queue or synchronous pre-send ledger transaction is added.

These measurements exclude real persistence, native transport, exchange delay,
OS scheduling under production load and end-to-end trading throughput. They do
not establish a production tail-latency improvement. Existing receipt snapshots
carry extra diagnostic bytes; long-session persistence cost was not measured.

## Live boundary

This change performed no live order, live account query or deployment. The four
negative samples cannot estimate the fill rate or opportunity loss under the new
defaults. Submitted-to-rejection timings (858–1019 ms) include native/venue
processing and are not network RTT. STX/JUP rejection-callback books show zero
depth inside the original limit, but are not venue-arrival snapshots. TIA/CRV
callback books are missing. No positive buffer or new execution-profit claim is
inferred. Existing notionals, total exposure and native risk protections remain.

Artifacts: [replay](issue95-entry-admission-replay.json),
[benchmark](issue95-entry-admission-benchmark.json),
[policy documentation](../../docs/entry-admission.md).

# Ownership follow-up

Date: 2026-09-19. Native changes are frozen for root review and packaging after the checks below.
Worktree: `E:/nautilus_trader/.worktrees/ondo-r52-cleanup`, base `ff57243`.

## Outcome and scope

Fixed the three verified P1s from `../../review/shutdown-review-2.md`:

- Reset invalidates the prior generation's shutdown record. A second lifecycle cannot skip its
  owned-order cleanup using a previous successful result.
- A canceled request-drain future forces cancellation through an RAII guard. Synchronous stop
  checkpoints uncertainty before forcing remaining requests to stop; a later async hook still owns
  and joins the generation.
- The execution client retains its private-stream owner across release and stop awaits. The stream
  awaits its JoinHandle by mutable reference, aborts after its graceful timeout, then joins within
  a one-second forced bound. An unfinished task stays owned and reportable. Sync stop signals abort
  while retaining the owner for a later join; no moved JoinHandle is silently detached.

Only these source/test files were changed in this follow-up:

| Path below `crates/adapters/ondo` | Follow-up line ranges in frozen source |
| --- | --- |
| `src/execution.rs` | 1633-1646 request-drain guard; 2275-2364 retained stream release/close owner; 4235-4279 synchronous forced fallback; 4293-4303 generation record reset; 4410-4418 guarded request drainage |
| `src/websocket/private/stream.rs` | 351-389 retained-handle stop and internal abort; 1214-1279 ownership regressions |
| `tests/private_runtime.rs` | 2968-3074 two-cycle and caller-abandoned-drain regressions |

These ranges identify this follow-up rather than the entire uncommitted Pi implementation. Prior
Pi terminal/fills gates, read-only handling, request budget, uncertainty pre-registration and tests
were retained. The separately owned Python capability getter was not edited.

## RED and GREEN

Four new regressions failed on the intended behavior before production edits:

| Evidence | Command suffix after `cargo +1.98.0 test -p nautilus-ondo --locked --offline` | Result |
| --- | --- | --- |
| `red.txt` | `shutdown_ownership -- --nocapture` | exit 101; 2 stream unit regressions failed: lost abort owner after caller cancellation, task still running after stop timeout |
| `red-lifecycle.txt` | `--test private_runtime shutdown_ownership -- --nocapture` | exit 101; 2 hook regressions failed: post-stop POST answer applied, second lifecycle returned old Ok without DELETE |
| `green-targeted.txt` | `shutdown_ownership -- --nocapture` | exit 0; all 4 new regressions passed |
| `green-full-crate.txt` | no suffix | exit 0; **901 passed, 0 failed** |

Full count: 440 unit + 71 execution + 39 http_client + 77 http_contract + 43 market_data +
49 private_runtime + 118 reconciliation + 63 signing + 1 doc-test = 901.

`cargo +1.98.0 test -p nautilus-ondo --features python --locked --offline --test python`:
exit 0, **6 passed**, `green-python-feature.txt`.

`cargo +1.98.0 fmt -p nautilus-ondo -- --check`: exit 0, `fmt-check.txt`.
`git -c safe.directory=E:/nautilus_trader/.worktrees/ondo-r52-cleanup diff --check`: exit 0.

Each Cargo test process used:

```powershell
$env:CARGO_TARGET_DIR='E:/nautilus_trader/target'
$env:CARGO_BUILD_WARNINGS='allow'
$env:CC='clang'
$env:CXX='clang++'
$env:PYO3_PYTHON='E:/nautilus_trader/.venv/Scripts/python.exe'
```

The warning allowance is per command, for the documented Windows MSVC `linker_messages` exception;
these are not strict-warning successes. Existing strict failure evidence remains at
`../correction-round-1/correction-strict-windows-linker-failure.txt`. Stable rustfmt also reports
unsupported nightly formatting options; formatting completed successfully without diffs.

The first default-sandbox Cargo attempt could not open the external target lock. Authorized
elevated execution was then used; it did not change test configuration or task scope. One initial
new-test type mismatch was corrected before capturing the behavioral RED files above. Cargo's
targeted/full/feature logs show this worktree's Ondo source actually compiled; no source restoration
or mtime/cache shortcut was used.

## Frozen hashes

SHA256 immediately before the final report:

| Path below `crates/adapters/ondo` | SHA256 |
| --- | --- |
| `src/execution.rs` | `4AA6A7234883004F64C8EE629E0168385E34BBFD541A2EE1F0FFF7A09C1C09B1` |
| `src/websocket/private/stream.rs` | `055A0C511AC18BFBBE3373877D323DD5ECA9A1E6AC804AF1D3240A3699C8D9C8` |
| `tests/private_runtime.rs` (after the test-only acceptance extension below) | `49C909163F18E47F51A215B404DA6A4EAB93B6C3BBA26B800F7E639D96B863A3` |
| `src/python/factories.rs` (untouched integration owner) | `9D2DA4304B065460D659CC05CC668EE04C60CACCE3DE0DBAA05A3F72C515073D` |

The Pi handoff snapshot before this follow-up was execution
`7823160A5B97F70E5D64211A73B6E0AB56086EA5E0AC9867BD4422A653634183`, private_runtime
`00FFD9C5B437297A308917F55F604E8D71B7206DE6C7C4CA00CE2F82FEF3BB18`, and stream
`CA801839D5067395A34820FF8B66611CA027ADAFE177AAA8E0139E1AF7C0C961`.

## Limits and handoff

The two lifecycle regressions use production clients with local HTTP/WS mocks. The stream tests
exercise the production ownership/stop methods with a real Tokio task whose external teardown is
deliberately blocked; they introduce no test-only production API or behavior. Native
`pub(crate) abort` exists for the production synchronous stop hook.

No full LiveNode integration or real venue protocol acceptance is claimed. Caller cancellation can
still prevent synchronous proof of completion, but the task owner is retained and forced
cancellation can be joined by the next async finalizer. The stream's worst-case termination budget
is now two graceful seconds plus one forced second, after the adapter's shared five-second work
budget; a smaller caller deadline remains an explicitly interrupted shutdown.

No wheel built/installed, no commits, no remote venue requests, no credentials accessed. Source
ownership and the shared Cargo target are released to root; do not change these hashes until root
finishes its final review or explicitly resumes implementation.

## Test-only acceptance extension

The final reviewer requested that the unknown-submission restart test drive the actual lifecycle
hook and prove successful recovery, not only persisted refusal. The existing
`test_an_unknown_submission_survives_shutdown_and_restart` now calls the first client's
`disconnect().await.expect_err(...)`, asserts that the error names the original client order ID
and reports drained tasks, verifies the stream ended and only one initial POST occurred, then
destroys that client and reconstructs another from the journal.

All prior journal/restore/fail-closed assertions remain. Only afterwards the local mock supplies a
terminal canceled order under the original client and venue IDs. Probes and reconciliation must
reach Ready, with the same order settled, both uncertainty maps empty, new-risk refusal cleared,
and no replacement POST. The recovered client's actual disconnect must succeed.

Evidence (same per-command Windows warning exception as above):

- `cargo +1.98.0 test -p nautilus-ondo --locked --offline --test private_runtime test_an_unknown_submission_survives_shutdown_and_restart -- --exact --nocapture`:
  exit 0, 1 passed, `green-lifecycle-restart.txt`.
- `cargo +1.98.0 test -p nautilus-ondo --locked --offline --test private_runtime`:
  exit 0, 49 passed, `green-private-runtime-restart.txt`.
- `cargo +1.98.0 fmt -p nautilus-ondo -- --check`: exit 0, `fmt-check-restart.txt`.
- Targeted fork `git diff --check`: exit 0.

Only this test changed; execution, reconciliation, stream and Python getter hashes were verified
unchanged. The previous full 901-test and Python 6-test gates still describe the identical
production snapshot; unrelated suites were not repeated. The new private_runtime hash in the
table supersedes `EF8F6F73D56245B57AF43883671DC2884C700210C07A274E53E123BE8318F869`.
Source and target are frozen and released again after this evidence-only extension.

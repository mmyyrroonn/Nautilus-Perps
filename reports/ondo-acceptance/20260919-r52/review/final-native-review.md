# Ondo shutdown final independent native review

Date: 2026-09-19. Final status: **accepted** for the reviewed native offline shutdown correction. No concrete in-scope blocker remains after the targeted actual-hook restart regression was added and inspected.

Scope: read-only review of `E:/nautilus_trader/.worktrees/ondo-r52-cleanup` against `ff57243`, including both prior shutdown reviews and ownership-followup evidence. Only this review artifact was written. This reviewer ran no Cargo, build, network or credential operation. Root independently reran the four ownership regressions in `native-independent.txt`.

## Production findings

No additional concrete P1/P2 correctness blocker was found in the reviewed patch. The three review-2 ownership blockers are resolved in the inspected control flow:

- `execution.rs:4293-4302` clears `last_shutdown` when reset opens the next request generation. The two-cycle regression at `private_runtime.rs:2969` proves second-generation DELETE and subsequent drainage.
- `execution.rs:1633-1646,4410-4418` forces cancellation when the caller drops request drainage. Sync stop checkpoints uncertainty before signaling force (`4251-4267`). The original request owner remains available to `finish_shutdown`; the held-POST regression at `private_runtime.rs:3007` releases its answer after final stop and detects any late acceptance.
- `execution.rs:2310-2364` retains `private_stream` through release and close; `stream.rs:351-389` retains the JoinHandle through caller cancellation and both bounded waits. Its own timeout now aborts then joins; a task still alive remains owned. Sync stop aborts the retained owner instead of discarding it.

Earlier safety gates remain intact: restored read-only state sends no DELETE or DMS writes; terminal status plus matching/applied fills is required before release; every working identity is registered/checkpointed before cancellation awaits; request drainage and REST cancel/confirm use the shared five-second work budget, followed by the explicit bounded transport allowance; dirty outcomes propagate as `Err`; repeated dirty shutdown does not reuse an absent socket as proof of success. Direct terminal cancel acknowledgement may settle the cancel registration, but the independent order/fills predicate still prevents premature DMS release.

The transport allowance is two graceful seconds plus one forced second. The DMS send has a minimum 500 ms allowance. Consequently this is bounded cleanup under the default ten-second outer deadline, not a claim that every shutdown phase completes within exactly five seconds. A shorter caller deadline is explicitly interrupted cleanup whose owners remain available to finalization.

## Combined lifecycle evidence closed

The pre-follow-up test used direct `stop_and_wait` to create the checkpoint and stopped after the reconstructed client refused new risk. That was a required evidence gap, not a demonstrated production defect.

The final test at `private_runtime.rs:3230-3358` now uses the first client's actual `disconnect`, asserts a dirty result naming the original identity with drained tasks and an ended stream, then reconstructs a second client from that checkpoint. Its existing fail-closed assertions remain. Terminal evidence is supplied under the same original client/venue IDs; probes and reconciliation reach Ready, the original order is settled, both uncertainty maps are empty, no replacement POST exists, and the recovered client's actual disconnect succeeds. The changed test body and its complete targeted output were inspected. This was test-only: all four production source hashes and `tests/reconciliation.rs` remain unchanged.

## Evidence inspected

- `../cleanup/ownership-followup/red.txt` and `red-lifecycle.txt`: the four ownership regressions fail on their intended behavioral assertions, not setup or compilation.
- `../cleanup/ownership-followup/green-targeted.txt`: all four pass after the fixes.
- `native-independent.txt`: root's independent rerun also passes all four.
- `../cleanup/ownership-followup/green-lifecycle-restart.txt`: the final combined actual-hook/restart/recovery regression passes (1/1). `green-private-runtime-restart.txt`: all 49 private-runtime cases pass after that test-only change.
- `../cleanup/ownership-followup/green-full-crate.txt`: logged 900 unit/integration tests plus one doc-test passed; `green-python-feature.txt`: six passed. These are execution-owner logs inspected by this reviewer, not tests rerun here.
- `../cleanup/ownership-followup/fmt-check.txt` contains only the documented stable-rustfmt option warnings; its accompanying README records exit 0. This reviewer independently ran `git -c safe.directory=E:/nautilus_trader/.worktrees/ondo-r52-cleanup diff --check`, exit 0.

The Cargo runs use the documented per-command Windows warning allowance. This does not claim a strict-warning build passed.

## Source boundary

Production hashes were checked initially and again after the final test-only change. The final test hash was independently verified against the implementer's report. Paths are below `crates/adapters/ondo`:

| File | SHA256 |
| --- | --- |
| `src/execution.rs` | `4AA6A7234883004F64C8EE629E0168385E34BBFD541A2EE1F0FFF7A09C1C09B1` |
| `src/reconciliation.rs` | `0AC8338632A5677D62E6531276A87D54DF946E7AC1CBBF2CB3F704E288488CD3` |
| `src/websocket/private/stream.rs` | `055A0C511AC18BFBBE3373877D323DD5ECA9A1E6AC804AF1D3240A3699C8D9C8` |
| `src/python/factories.rs` | `9D2DA4304B065460D659CC05CC668EE04C60CACCE3DE0DBAA05A3F72C515073D` |
| `tests/private_runtime.rs` final | `49C909163F18E47F51A215B404DA6A4EAB93B6C3BBA26B800F7E639D96B863A3` |
| `tests/reconciliation.rs` | `7BC6E150FB9728E8A22C588A60D02CF814C37FA245E1703CDB873EA288309A03` |

## Limits

This is native offline correctness review. No real sandbox, authentication, venue DMS behavior, mainnet, wheel installation or financial outcome is accepted by it. The Python capability getter is only a build capability marker.

The stream cutoff tests exercise production stop/ownership methods around a real held Tokio task constructed inside the stream's unit module; they do not drive an actual `ExecutionClient::disconnect` into a blocked production socket close. Full LiveNode/kernel shutdown, caller-cut-short batch response after final stop, and late-fill-arrival-then-release are not independently established by the inspected new cases. These are explicit coverage limits; no additional production defect was inferred solely from their absence.

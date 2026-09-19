# Ondo shutdown independent review 2

Date: 2026-09-19. Artifact completed at 18:00 +08:00. Result: three concrete P1 lifecycle blockers remain.

Scope: current uncommitted corrective implementation in
`E:/nautilus_trader/.worktrees/ondo-r52-cleanup` against review 1. Read-only inspection except this
authorized document; no Cargo/test/build or network operations. Pi's suite was running, so this
review does not independently claim its test results. The Python capability getter is not cleanup
acceptance evidence and was not treated as such.

## Stable source boundary

The implementation and test hashes below were checked twice during this review and were unchanged:

| File below `crates/adapters/ondo` | SHA256 |
| --- | --- |
| `src/execution.rs` | `52777A8F80AF84830D726966E5E7DBEF908024178792C61483D52B73DCAC911B` |
| `src/reconciliation.rs` | `0AC8338632A5677D62E6531276A87D54DF946E7AC1CBBF2CB3F704E288488CD3` |
| `tests/private_runtime.rs` | `F5307EEEFE209250481ED7798867ADA42878C7ECF4B83B9356ED74C9AD0D0EE1` |
| `tests/reconciliation.rs` | `7BC6E150FB9728E8A22C588A60D02CF814C37FA245E1703CDB873EA288309A03` |

Unmodified but relevant `src/websocket/private/stream.rs` hash:
`CA801839D5067395A34820FF8B66611CA027ADAFE177AAA8E0139E1AF7C0C961`.

At artifact completion root reported Pi was temporarily reverting source for a RED/counterproof
run (`correction-red.txt`, backup under `TEMP/ondo-correction-src-backup`). That transient tree is
not the reviewed correction or a final implementation. This report applies to the stable hash
snapshot above, inspected immediately before artifact creation; recheck hashes after Pi restores
the correction before beginning implementation or final acceptance.

## Disposition of review 1

| Earlier finding | Current disposition | Evidence |
| --- | --- | --- |
| 1. DMS released over working orders/missing fills | Resolved for the identified gate defects | Confirming GET checks `is_settled`; probe Found carries settlement; pending-order polling uses `is_settled`; release requires no timeout/uncertainty/unresolved orders. Tests at private_runtime.rs:2566,2603,2642 cover the negative cases. |
| 2. Read-only journal restart sends DELETE | Resolved | Stop skips cancel registration/requests for read-only, and explicitly disallows release. Restored-journal read-only regression at private_runtime.rs:2700 checks no DELETE/DMS and preserved uncertainty. |
| 3. Requests not drained/reset impossible | Partially resolved | Normal disconnect calls begin_shutdown + bounded finish_shutdown; blocked-submit test at private_runtime.rs:2846 proves reset/new submission after that completed path. Caller cancellation during drainage still bypasses forced termination; see blocker B. |
| 4. Sync stop then disconnect skips cleanup | Resolved for a first lifecycle; still incorrect across generations | First-cycle regression at private_runtime.rs:2931 runs REST cleanup and reports unavailable DMS release. Old success survives reset and skips the next cycle; see blocker A. |
| 5. Cancel requests outside shutdown budget | Request-budget/pre-registration defect resolved; cancellation-safe termination still unresolved | Every working order is registered before awaits; DELETE/confirming GET uses remaining shared budget. Two-order cutoff test at private_runtime.rs:2793 verifies both journal entries. Stream ownership across the later await remains unsafe; see blocker C. |
| 6. Dirty report becomes Ok | Resolved on normal completion and the tested first-cycle retry | Disconnect returns ShutdownRecord error; StopReport clean now requires owed switch release. Cross-generation stale-success early return still defeats the result; see blocker A. |

No new claim of a synchronous emitter/RefCell callback defect: the mpsc-based event path remains
the checked non-finding from review 1.

## A. P1: a previous generation's success skips the next generation's cleanup

**New corrective-patch defect.** `execution.rs:4278-4285` reset reopens the TaskGroup but never clears
`last_shutdown`; neither start nor connect invalidates that record. Sync stop writes an unfinished
record only if `last_shutdown.is_none()` (`4257-4262`). Disconnect early-returns when the retained
record is complete, transport is absent, and the task tracker is empty (`4353-4360`). Empty tasks
does not mean a closed generation is Drained or that no accepted orders are working.

Concrete reproduction using existing production objects/mocks:

1. Cycle 1 connects and cleanly disconnects; `last_shutdown` is complete.
2. Reset/start/connect cycle 2; submit and acknowledge an owned open order, letting its request task
   finish so the tracker is empty.
3. Call synchronous stop. It registers the owned cancel and closes the new generation, drops the
   stream, but retains cycle 1's complete record.
4. Call disconnect. It returns Ok immediately without DELETE/drain. The owned order remains working,
   and a subsequent reset can fail because this generation stayed Closing.

**Minimum fix/acceptance:** bind shutdown evidence to its generation or invalidate it when a new
lifecycle starts. Add the exact two-cycle regression above. The second disconnect must do all
remaining bounded cleanup or return its own dirty result, never reuse cycle 1's success. Assert
owned order treatment and subsequent generation drainage, not merely a returned error string.

## B. P1: outer cancellation during graceful drain leaves request tasks running after stop

**Corrective-patch regression/incomplete cancellation handling.** Disconnect awaits
`finish_shutdown` at `execution.rs:4393` with a one-second graceful slice. Dropping that future before
the slice expires prevents its forced-cancel phase from running. The subsequent synchronous stop now
only calls `begin_shutdown` (`4240-4246`); it deliberately removed abort.

TaskGroup's spawned wrapper listens to `force.cancelled()` or completion of the task future
(`crates/live/src/task.rs:968-972`), not the cooperative cancellation token. Ondo's request futures
also do not observe the cooperative token. A held POST therefore continues after final stop and can
apply an answer/write the journal later. Its TaskGroup remains in the client, so this is retained
but un-terminated work, not a detached request task; that distinction does not meet the lifecycle's
termination requirement. LiveNode permits a configurable outer deadline and invokes sync stop after
the canceled disconnect, so this path is reachable.

**Minimum regression:** hold a real submitted POST at the existing mock barrier; timeout actual
disconnect after about 100 ms (before its graceful slice ends); call sync stop; release the POST.
Verify no late account/event/journal mutation by an old request, preserved unknown/unfinished
identity, and that a later async cleanup can drain/reopen. Also cover a blocked batch containing
multiple items, because only a single blocked POST currently exercises forced drainage.

**Fix constraint:** preserve the pre-registered uncertainty before forcing a drop. Arrange a
cancellation-safe forced fallback/owned drain; do not replace it with an unowned background task.
If synchronous stop cannot await completion, keep the remaining owner and explicit incomplete
result for an async finalizer, while actually signaling forced cancellation. A guard that only
checkpoints the journal is not a task-termination guard.

## C. P1: stream join ownership is lost across close timeout/caller cancellation

**Pre-existing stream-stop defect operationally relevant to this corrective lifecycle.**
`execution.rs:2278` takes the stream out of the client before awaiting release and close.

- If the caller cancels during release, the local stream's Drop signals cancellation and aborts its
  still-held JoinHandle, but the client has lost the owner needed to await/verify that abortion.
- If cancellation occurs during `stream.stop().await` (`execution.rs:2339`), the more serious existing
  bug is reached: `websocket/private/stream.rs:355-360` first takes its JoinHandle and passes it by
  value into `tokio::time::timeout`. Cancellation of that await, or the stream's own two-second
  timeout, drops the JoinHandle and detaches the transport task. Stream Drop then finds `task=None`
  (`216-222`), so it cannot abort that task despite its documentation claiming it can.
- A later hook has `private_stream=None`; `stop_and_wait_within` treats that as
  `stream_ended=true` (`2346`). It cannot recover or prove termination of the old task. The local
  cancellation token does not fix a transport wedged in an awaited connect/disconnect operation.

**Minimum fix/acceptance:** retain the stream/join owner across interruptible awaits. On stream's
own deadline signal abort and await the retained handle within a forced bound; never detach by
dropping a moved JoinHandle. Add deterministic teardown blocking and cancel the real disconnect
after it enters stream close, then invoke the final lifecycle hook. Verify the original transport
task ends, no reconnect/frames occur afterwards, and absence of a field is never used as proof of
task completion. Test the stream's own two-second timeout path as well as the caller cutoff.

## Remaining evidence requirements

The correction substantially improves real-hook coverage, but the new restart test
`test_an_unknown_submission_survives_shutdown_and_restart` still creates its shutdown journal via
direct `stop_and_wait` (`private_runtime.rs:3005-3008`), while the separate actual-hook unknown test
only checks a caller-cut-short checkpoint and does not reconstruct a second client. Change/add one
end-to-end case to source the restart journal from the actual disconnect hook and then prove
fail-closed admission and eventual recovery from terminal evidence under the original identity.

The terminal/missing-fills test covers refusing release; add late fill arrival completing that same
order to show the corrected gate can eventually release rather than only refuse. A real engine/kernel
shutdown test remains absent from the inspected new tests. At minimum verify the hook sequences
above, caller cancellation in each phase, and post-stop event/journal stability before advertising
full lifecycle acceptance. These are bounded follow-ups to the requested cleanup, not a request for
unrelated refactoring or venue/live tests.

The three blockers above were established from actual ownership/state/control flow. Proposed
regressions were not run by this reviewer; Pi/root must produce the corresponding RED/GREEN evidence
after the current shared-target run is released.

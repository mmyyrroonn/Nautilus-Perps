# Ondo shutdown independent review 1

Date: 2026-09-19. Reviewer: independent Astra subagent. Result: changes required.

Scope: read-only code review of `E:/nautilus_trader/.worktrees/ondo-r52-cleanup`, base
`ff57243`, plus this authorized review artifact. No implementation edits, Cargo/test/build,
credentials, authenticated requests or venue operations were performed.

## Source boundary

Initial reviewed snapshot:

- `crates/adapters/ondo/src/execution.rs` SHA256
  `B5E73B6DC9AA36C5D31FA30F77C28A7CD2B68EF91CFE0C4B58F56D327A06BCC4`.
- `crates/adapters/ondo/tests/private_runtime.rs` SHA256
  `F2A8F141C77F95DDF092505001355B2CF9B14C21FAA520422EACEB0167822461`.

On artifact preparation Pi had added tests/documentation changes; current hashes were
`4BBD243733020C311FCC9100F14194A2963E2A6B0870D0A859F700F7673DF29B` and
`F9C23A49C15974E0B992AC5376655A9ED57CCAD94B048BF6394E07CC9FB30690` respectively.
The disconnect/stop implementation hunks were reread and still contain the reviewed behavior.
Line references below are to the initial snapshot. This is a corrective handoff, not a verdict
on future Pi changes or newly added tests.

## Findings

### 1. P1: release gate permits working orders and missing fills

**Origin:** pre-existing `stop_and_wait`/reconciliation defects newly made operational by wiring
that helper into `ExecutionClient::disconnect`.

`execution.rs:3189-3199` clears the unconfirmed cancel on any successful confirming GET,
including an `open` or `pending` order. The background probe has the same problem:
`reconciliation.rs:3512-3516` clears both maps on any `ProbeOutcome::Found`.
`execution.rs:2154-2157` releases DMS whenever the two uncertainty maps are empty, without
requiring settlement or an empty unresolved list. DELETE accepted without an order, followed
by GET open, therefore reaches the polling timeout and still sends the release frame.

A terminal cancel response with `filledSize` greater than applied fills is another concrete
path: `pending_orders` tests only status terminal (`2254-2260`), and the DMS gate ignores
`report.unresolved_orders`. A terminal status alone is not fill reconciliation.

**Minimum acceptance:** actual disconnect tests for confirming GET open, background probe
Found/open, and terminal status with missing fills. No DMS unsubscribe until owned orders are
terminal and fills agree; otherwise retain named outstanding work and a dirty result. Include
late-fill completion that genuinely permits release, to avoid a permanently stuck gate.

### 2. P1: read-only restart can send DELETE from a restored journal

**Origin:** pre-existing unconditional stop helper newly exposed by real disconnect wiring;
this changes previously side-effect-free read-only disconnect behavior.

`execution.rs:2705` restores tracked orders from the journal. `2104-2113` cancels all tracked
orders without checking `account_read_only`. Neither runtime cancel nor HTTP cancel supplies
that guard. The existing read-only disconnect test has no restored orders and misses this.

**Minimum acceptance:** persist owned open/unknown work, construct a read-only client from that
journal, connect and disconnect. Assert zero DELETE, zero DMS arm/renew/release frames, and no
new-risk admission. Keep the inherited state reportable without performing cleanup writes.

### 3. P1: request tasks are neither drained nor awaited; reset cannot reopen

**Origin:** existing incomplete task ownership/reset handling remains unresolved by the new
lifecycle integration; now part of the claimed graceful shutdown path.

`stop_and_wait`/disconnect never closes or awaits `self.tasks`; sync stop only calls abort at
`execution.rs:4030`. An already running POST/batch/read task can still complete and update the
ledger after disconnect returned. Forced abort can drop a request before the code which records
its outcome runs (`4246-4321`). An ordinary checkpoint alone is not proof that all such outcomes
were registered.

`reset` calls `start_generation`, but that requires `Drained` (`crates/live/src/task.rs:1093-1094`),
which is assigned only by `finish_shutdown` completion (`1008-1010`). Ondo never calls
`finish_shutdown`; merely waiting for aborted tasks does not advance that phase.

**Minimum acceptance:** block actual single POST and batch POST independently, initiate shutdown,
then prove every owned request task has finished or been boundedly aborted, every ambiguous item
survives restart, and no task mutates state after completed shutdown. Exercise
stop -> disconnect -> reset -> start -> connect on the same object; assert the new generation
can execute permitted work after genuine recovery.

### 4. P1: stop-before-disconnect silently skips all asynchronous cleanup

**Origin:** new early-return behavior in the new graceful entry point, combined with the existing
synchronous stop teardown. Base HEAD also returned early after stop, but had no graceful cleanup
contract; the new entry point incorrectly treats transport absence as completion evidence.

`execution.rs:4035-4037` drops the transport and marks disconnected. Then `4123-4124` returns Ok
without inspecting owned orders, unfinished tasks or the previous shutdown outcome. The sync
checkpoint persists the existing ledger but does not itself register cleanup intent for every
working/in-flight order.

**Required semantics:** prefer that the later async disconnect drains tasks and completes whatever
bounded REST cleanup remains possible, while honestly recording that the original stream was
already stopped. If stream loss prevents proof of completion, preserve all unfinished work and
return an explicit dirty result. Returning Ok solely because the transport is absent is not
acceptable. Do not claim a DMS release reached the wire when no stream exists.

**Minimum acceptance:** actual stop -> disconnect with working and in-flight owned orders,
repeated stop/disconnect, restart from the resulting journal. Verify no dropped work, no repeated
release, no falsely clean result, and stable identity throughout.

### 5. P1: the five-second bound excludes the slowest shutdown operations

**Origin:** pre-existing unbounded cancel phase exposed under the real LiveNode timeout; the new
constant does not bound the whole shutdown it is intended to protect.

`execution.rs:2105-2136` awaits every DELETE/confirming GET serially before starting the five-second
polling budget. Default HTTP timeout is 15 seconds (`common/consts.rs:94`), while LiveNode's default
disconnect deadline is 10 seconds. The first blocked request can consume the entire outer budget,
before later owned orders receive unconfirmed-cancel registrations. `JournalCheckpoint` preserves
existing records but cannot create missing uncertainty or finish task teardown.

**Minimum acceptance:** at least two owned orders, first DELETE held behind a deterministic mock
barrier, actual caller timeout. Every unfinished order/request must be durably represented;
transport and request tasks must terminate or remain explicitly owned with a dirty result. Use a
single bounded overall shutdown budget, with allowance for forced task/transport termination.

### 6. P2: dirty shutdown result is converted into success

**Origin:** newly introduced result-discarding behavior around the newly invoked stop helper.

`execution.rs:4138-4152` logs dirty StopReport but always returns Ok. LiveNode already aggregates
disconnect errors (`crates/live/src/node/mod.rs:2299-2333`), so cancel timeout, unknown work or
transport failure disappear from its returned result. A repeated disconnect also returns Ok
without preserving the prior dirty result.

**Minimum acceptance:** after performing bounded cleanup, dirty disconnect returns an error with
outstanding stable identities/reason. Complete cleanup returns Ok. Repeated lifecycle calls must
not relabel an unresolved prior outcome as clean simply because the socket is gone.

## Practical TaskGroup guidance

Existing APIs in `crates/live/src/task.rs` are sufficient building blocks:

1. `begin_shutdown()` (`158-161`) closes generation admission and signals cooperative cancellation.
   The underlying tracker is closed; this is the public close-admission API.
2. `finish_shutdown(graceful_timeout, abort_timeout)` (`168-231`) requires a closed generation,
   awaits graceful completion, signals forced cancellation at its bound, awaits forced completion,
   and marks a fully drained generation `Drained`. Failures/timeouts are returned, not erased.
3. `abort()` (`163-166`) closes admission and requests immediate force cancellation. It is not a
   substitute for awaiting `finish_shutdown`.
4. `start_generation()` is only legal after drainage. `is_open`, `is_empty` and `len` are useful
   observation APIs, not proof that a closing generation has transitioned to Drained.

Suggested ordering to adapt, not a demanded implementation: latch shutdown/new-risk refusal;
close request-task admission; capture/register all in-flight work before any forced drop;
boundedly settle or drain existing writers while the account transport remains available;
cancel/reconcile the stable owned set with the remaining shared budget; release DMS only on the
full safety predicate; stop and await the transport and every owned task; checkpoint final
unfinished state; return the actual result. If cancellation of the outer future interrupts this,
preserve ownership and journal evidence and let the next lifecycle hook finish or report it.
Keep recovery allowed to read evidence, but prevent it from reopening new-risk admission during
shutdown. Avoid two independent account owners or a detached fire-and-forget cleanup task.

## Coverage and checked non-findings

The four initially added tests establish successful cancel/release, one caller-cut-short case,
foreign-order exclusion, and factory no-order release. Existing direct `stop_and_wait` tests do
not establish real-hook unknown submission -> shutdown -> restart, normal cancel timeout,
stop-before-disconnect, repeated lifecycle calls, in-flight task ownership or read-only journal
recovery. Pi was adding tests when this artifact was written; independently review those results.

No synchronous RefCell reentrant emitter defect was found: `ExecutionEventEmitter` sends through
mpsc (`crates/live/src/execution/emitter.rs:502-519`). LiveNode drains queued execution events after
disconnect (`crates/live/src/node/mod.rs:2371-2373`). Do not manufacture a callback-panic finding.
A test through the real kernel/engine lifecycle is still valuable to verify the complete ordering.

This review is static evidence and concrete reproduction guidance, not a claim that these proposed
regression cases were executed. Preserve existing test coverage and obtain RED/GREEN evidence for
the corrective implementation before acceptance.

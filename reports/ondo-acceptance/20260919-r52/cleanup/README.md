# Ondo shutdown wired into the real client lifecycle (R5.2 cleanup)

Run id `20260919-r52-cleanup`. Fork worktree `E:/nautilus_trader/.worktrees/ondo-r52-cleanup`,
branch `task/ondo-r52-cleanup`, base `ff57243` (R5.1). The application worktree was not touched.
No commit, merge, push or remote action was performed.

> **Status:** correction round complete. The first-round implementation was **not accepted**; Astra's
> review (`control/cleanup-review-findings.md`, `review/shutdown-review-1.md`) is addressed below
> with executed RED/GREEN evidence. The first round's records are preserved in this directory; the
> correction round's evidence is in `correction-round-1/`.

## 1. Cause

`OndoAccountRuntime::stop_and_wait` existed but its only callers were tests. The real
`ExecutionClient` lifecycle never awaited it: `stop` (sync) aborted the task group and dropped
`private_stream`, and `disconnect` (async) only awaited the transport stop. A real shutdown stopped
the socket without cancelling this run's orders, confirming terminal state, or applying the ordered
DMS release (plan §R3.3).

## 2. Real lifecycle and ordering

`LiveNode::stop` -> `finalize_stop`:

1. `kernel.disconnect_clients()` (async, bounded by `timeout_disconnection`, default 10 s) ->
   `ExecutionEngine::disconnect()` -> every client's `disconnect()`;
2. `kernel.finalize_stop()` -> `stop_engines()` -> `ExecutionEngine::stop()` -> every client's
   `stop()` (synchronous);
3. `LiveNode::dispose()` may reach `stop()` again.

`disconnect()` is the async hook that carries the ordered stop; `stop()` is the synchronous hook.

## 3. What the review caught, and what changed

| # | Verified blocker | Fix |
|---|---|---|
| 1 | A successful confirming GET cleared cancel uncertainty even for an `open`/`pending` order; the background `ProbeOutcome::Found` did the same; the DMS gate ignored the settle outcome and `unresolved_orders`. A terminal order with missing fills could also release. | `OndoAccountRuntime::confirm_cancel` clears only when the applied order `is_settled()` (terminal + fills agree + no pending fills). `ProbeOutcome::Found { settled }` lets a working order settle an **unknown submission** but not an **unconfirmed cancel**. `pending_orders` waits on `!is_settled()`. The release gate now requires `outcome == Complete`, empty unconfirmed/unknown **and** empty `unresolved_orders`. `StopReport::is_clean` also requires `released_switch` when the release step is present. |
| 2 | A journal-restored order made the unconditional stop loop send DELETE even for `account_read_only=true`. | The read-only stop removes the cancel/confirm steps, skips pre-registration and the cancel loop, and reports the inherited work without side effects. |
| 3 | Request tasks were neither drained nor awaited; `abort()` never advanced the generation to `Drained`, so `reset -> start_generation` could not resume. | `disconnect` closes admission (`begin_shutdown`) and drains with a bounded `finish_shutdown(graceful, abort)`, which reaches `Drained`. `stop` uses `begin_shutdown` (not `abort`) so in-flight requests can register their own outcomes and the generation can be drained by the async hook. A later `reset -> start -> connect` executes permitted work after genuine recovery. |
| 4 | `stop()` then `disconnect()` returned `Ok` by early return, losing unresolved work and reporting clean. | `last_shutdown` records that the ordered cleanup is owed. The later `disconnect` runs the bounded REST cleanup anyway, cannot claim a DMS release without a socket, and preserves the dirty result. Repeated hooks are idempotent and never relabel. |
| 5 | The 5 s bound started only after sequential DELETE/GET waits; the default HTTP timeout (15 s) already exceeds the node's 10 s. | One `ShutdownBudget` covers task drain, cancel requests, confirmations, release and transport close. Every owned working order is registered as an unconfirmed cancel and the ledger is checkpointed **before** the first request, so a blocked first request cannot erase a later order. |
| 6 | A dirty `StopReport` was logged then converted to `Ok`, bypassing LiveNode error aggregation. | `disconnect` returns `Err` with outcome, `tasks_drained`, `switch_released` and the outstanding ids, **after** cleanup and persistence. |

Also added `DeadMansSwitch::release_frame()` (compose without releasing): the stop writes the frame
first and releases the switch locally only after the write succeeded, and a missing stream leaves the
switch armed rather than claiming a release (`switch_released=false`).

One side effect of pre-registering before the drain is handled at the source: when a definitive
refusal proves an order never rested (single or batch), the submission path clears the cancel
pre-registered for it (`reconciliation.write().confirm_cancel(...)`), so a never-rested order cannot
leave a registration that locks the account. Covered by
`test_a_definitive_refusal_during_shutdown_clears_its_registration`.

`stop_sequence()` still describes the full plan; for a read-only session the executed report omits
the cancel/confirm steps.

## 4. Changed paths

| Path | Owner | Change |
|---|---|---|
| `crates/adapters/ondo/src/execution.rs` | this session | shutdown budget, `ShutdownRecord`, `JournalCheckpoint`, pre-registration, `disconnect`/`stop`, `confirm_cancel`, probe settle flag |
| `crates/adapters/ondo/src/reconciliation.rs` | this session | `ProbeOutcome::Found { settled }`, `note_probe`, `is_clean`, `DeadMansSwitch::release_frame` |
| `crates/adapters/ondo/tests/private_runtime.rs` | this session | correction-round lifecycle tests + mock barriers |
| `crates/adapters/ondo/tests/reconciliation.rs` | this session | machine-level `Found { settled }` tests |
| `crates/adapters/ondo/src/python/factories.rs` | **parallel app-integration task** | `supports_ordered_shutdown` getter; not authored or modified here |

## 5. Tests — how each scenario reaches the real client lifecycle

All hook-level tests drive the concrete `OndoExecutionClient` through the same
`ExecutionClient` trait methods the engine calls (`start`, `connect`, `submit_order`,
`submit_order_list`, `disconnect`, `stop`, `reset`). The factory test builds the client through
`OndoExecutionClientFactory::create`.

| Scenario | Test | Reaches lifecycle via |
|---|---|---|
| normal stop | `test_disconnect_cancels_this_runs_order_and_releases_the_switch` | `disconnect()` |
| cancel/confirmation timeout (working GET) | `test_a_working_confirming_get_keeps_the_switch_armed` | `disconnect()` |
| background probe finds a working order | `test_a_background_probe_that_finds_a_working_order_keeps_the_cancel` | `account().probe_unknown_submissions()` (the transport's probe seam) |
| terminal order with missing fills | `test_a_terminal_order_with_missing_fills_keeps_the_switch_armed` | `disconnect()` |
| read-only restored journal | `test_a_read_only_restart_never_cancels_from_a_restored_journal` | `connect()` / `disconnect()` |
| unknown submission through shutdown | `test_a_disconnect_over_an_unknown_submission_stays_armed_and_journaled` | `disconnect()` (cut short) + `stop()` |
| unknown submission + restart | `test_an_unknown_submission_survives_shutdown_and_restart` | `stop_and_wait()` then a fresh client `connect()`/`reconcile_account()` |
| multiple owned orders, first blocked | `test_a_shutdown_cut_short_registers_every_owned_order` | `disconnect()` (outer timeout) |
| in-flight single submit | `test_a_blocked_submission_drains_and_the_generation_can_reopen` | `submit_order()` -> `disconnect()` -> `reset()` -> `connect()` |
| in-flight batch submit | `test_a_blocked_batch_submission_drains_and_preserves_every_item` | `submit_order_list()` -> `disconnect()` |
| definitive refusal during shutdown | `test_a_definitive_refusal_during_shutdown_clears_its_registration` | `submit_order()` -> `disconnect()` (join with the refusal) |
| synchronous stop before disconnect | `test_a_synchronous_stop_before_disconnect_still_reports_dirty` | `stop()` then `disconnect()` twice |
| repeated disconnect | same test | `disconnect()` twice |
| external ownership | `test_disconnect_never_cancels_an_order_this_run_does_not_own` | `disconnect()` |
| read-only, no journal | `test_a_read_only_session_triggers_no_switch_side_effect_and_no_cancel` | `disconnect()` |
| factory-built client | `test_the_factory_built_client_runs_the_ordered_stop_on_disconnect` | `OndoExecutionClientFactory::create` + `disconnect()` |
| restart inherits unresolved cancels | `test_a_restart_inherits_the_stops_outstanding_cancels` | `stop_and_wait()` then fresh client `connect()` |

Machine-level `reconciliation.rs` tests also cover `Found { settled: true }` settling a cancel and
`Found { settled: false }` keeping it.

## 6. RED / GREEN evidence

**Correction RED** (`correction-round-1/correction-red.txt`): reintroduced the reviewed first-round
behaviour, then ran the suite:

```
private_runtime: 39 passed; 7 failed
```

The seven failures are the correction tests: working confirming GET, background probe, terminal
missing fills, read-only restored journal, blocked single submit, blocked batch submit,
stop-before-disconnect. They fail on behaviour, not on compile/setup.

**Correction GREEN:**

| Command | Result |
|---|---|
| `cargo +1.98.0 test -p nautilus-ondo --locked --offline` | exit 0, **897 passed / 0 failed** (438 unit + 71 execution + 39 http_client + 77 http_contract + 43 market_data + **47 private_runtime** + 118 reconciliation + 63 signing + 1 doc-test) — `correction-round-1/correction-full-crate.txt` |
| `cargo +1.98.0 test -p nautilus-ondo --features python --locked --offline --test python` | exit 0, 6 passed / 0 failed — `correction-round-1/correction-python-feature.txt` |
| `cargo +1.98.0 fmt -p nautilus-ondo -- --check` | exit 0, no diffs |
| strict `cargo +1.98.0 test -p nautilus-ondo --locked --offline --test private_runtime` | exit 101 — known MSVC `linker_messages` platform exception, `correction-round-1/correction-strict-windows-linker-failure.txt` |

The first round's original RED/GREEN files remain in `cleanup/` unchanged
(`red-lifecycle-hooks*.txt`, `green-*.txt`, `fmt-check.txt`, `strict-windows-linker-failure.txt`).

## 7. Platform exception (Windows)

`cargo test` fails **strict** only on the MSVC `linker_messages` stdout warning refused by
`build.warnings = "deny"` (linker creates `.lib`/`.exp`). The strict failure is retained; the
per-command `CARGO_BUILD_WARNINGS=allow` is never claimed as strict success and no warning is
globally disabled.

## 8. Limits and remaining risks

- **A wheel rebuild is required** for the app/Python path to observe the change. No binding, config
  member or `.pyi` stub was changed by this session. The parallel integration task added the
  `supports_ordered_shutdown` getter so the app can distinguish an old wheel from the candidate; a
  build is still needed to verify the generated stub and the runtime capability.
- Real venue protocol remains unverified (REST header names, WS login order, private frame shapes,
  DMS renewal/release semantics; plan §R5.2 items 1-6). This work changes *when* the already-built
  frames are sent.
- No live/sandbox request was made; no credential was read.
- The ordered stop is bounded by one 5 s adapter budget plus the transport's own 2 s stop. A node
  `timeout_disconnection` below that, or a wedged venue, can still cut it short; that path is
  fail-closed, pre-registered and journaled.
- The kernel/engine-level ordering is verified by tracing plus hook-level tests; no full `LiveNode`
  integration test was added (it would need a kernel and a live runner). The tests call the exact
  `ExecutionClient` trait methods the engine calls.
- A read-only session with inherited unresolved work returns a dirty result (it does not clean it);
  this is intentional and side-effect free.

## 9. Application-side updates (done by the parallel integration task, verified here)

`docs/ondo.md` §6/§8 and `src/ondo_probe.py` were updated by the parallel app-integration task to
detect `supports_ordered_shutdown` and to keep `protocol_verified=false` and
`exit_code_zero_means_clean_account=false`. That task's own evidence is in
`../integration/README.md`; this report only confirms the getter compiles and the Python feature
tests pass alongside the correction. The app must still install a rebuilt wheel before claiming the
capability.

# Progress — wire Ondo shutdown into the real client lifecycle

> Correction round in progress (review findings 1-6). The first round's RED/GREEN files remain in
> this directory; the correction round's evidence is being added alongside and will be moved to
> `correction-round-1/` at the end. The parallel integration task owns
> `crates/adapters/ondo/src/python/factories.rs` (`supports_ordered_shutdown`); this session did not
> author it and does not modify it.

Run: 20260919-r52-cleanup. Fork worktree `E:/nautilus_trader/.worktrees/ondo-r52-cleanup` @ ff57243.

## 2026-09-19 — reconnaissance

- Traced the real lifecycle: `LiveNode::stop` -> `finalize_stop` -> `kernel.disconnect_clients()` (async,
  bounded by `timeout_disconnection`, default 10 s) -> `exec_engine.disconnect()` -> each client
  `disconnect()`; then `kernel.finalize_stop()` -> `stop_engines()` -> `exec_engine.stop()` -> each
  client `stop()` (synchronous). `LiveNode::dispose` also reaches `stop()`.
- Confirmed the gap: `OndoExecutionClient::stop` (execution.rs ~3963) aborts tasks and drops the
  private stream; `disconnect` (~4056) only awaits the transport stop. `stop_and_wait` is never
  awaited by the lifecycle.
- Planned fix: `disconnect()` runs the owned bounded `stop_and_wait`; the synchronous `stop()` is a
  fail-closed fallback that checkpoints the ledger and leaves the switch armed; `cancel_order`
  pre-registers the cancel before the request so a stop cut short still journals it; a drop guard
  checkpoints the ledger if the caller's timeout drops the stop future.

## 2026-09-19 — RED evidence

Added four lifecycle tests to `crates/adapters/ondo/tests/private_runtime.rs` that drive the real
`ExecutionClient` hooks (`disconnect()` / `stop()`):

- `test_disconnect_cancels_this_runs_order_and_releases_the_switch`
- `test_a_disconnect_the_caller_cuts_short_checkpoints_the_unconfirmed_cancel`
- `test_disconnect_never_cancels_an_order_this_run_does_not_own`
- `test_the_factory_built_client_runs_the_ordered_stop_on_disconnect`

RED run (`CARGO_BUILD_WARNINGS=allow`, Windows linker-message platform exception): all four fail on
behaviour, not on compile/setup:

- cancel/release tests: `left: 0, right: 1` DELETEs; no `cancelAllOrdersAfterPerps` unsubscribe
- cut-short test: `disconnect()` returned before the caller's 1 s bound (no cancel at all)
- factory test: `[]` switch releases

Evidence: `cleanup/red-lifecycle-hooks.txt`, `cleanup/red-lifecycle-hooks-2.txt`,
`cleanup/red-lifecycle-hooks-3.txt`.

Status: implementing.

## 2026-09-19 — GREEN + full crate regression

Implemented in `crates/adapters/ondo/src/execution.rs`:

- `ONDO_DISCONNECT_SETTLE_TIMEOUT` (5 s) bounds the wait the client owns, inside the node's 10 s
  `timeout_disconnection`.
- `JournalCheckpoint` drop guard checkpoints the ledger if the caller's timeout drops the stop
  future.
- `OndoAccountRuntime::cancel_order` registers the cancel as unconfirmed **before** the request and
  clears it from the venue's answer.
- `ExecutionClient::disconnect` now runs the ordered `stop_and_wait` (cancel own orders -> confirm ->
  release switch only when clean -> close transport).
- `ExecutionClient::stop` (sync) is a fail-closed fallback: checkpoints the ledger and leaves the
  switch armed instead of dropping an unordered account.

GREEN:

- `private_runtime` 36 passed / 0 failed (all four new tests pass).
- `execution` 71 / `reconciliation` 117 passed.
- full `cargo +1.98.0 test -p nautilus-ondo --locked --offline`: 885 passed / 0 failed (438 unit +
  71 + 39 + 77 + 43 + 36 + 117 + 63 + 1 doc-test).
- `cargo +1.98.0 fmt -p nautilus-ondo -- --check`: no diffs.
- Platform exception retained: `cargo test` strict fails on the MSVC `linker_messages` warning;
  `CARGO_BUILD_WARNINGS=allow` used per command only (not strict success).

Evidence: `cleanup/green-lifecycle-hooks-*.txt`, `cleanup/green-private-runtime.txt`,
`cleanup/green-reconciliation-execution.txt`, `cleanup/green-full-crate.txt`, `cleanup/fmt-check.txt`.

Status: complete. See `README.md` for the full report.

## 2026-09-19 — final

- `cargo +1.98.0 test -p nautilus-ondo --features python --locked --offline --test python`: 6
  passed / 0 failed (native factory surface).
- Added `test_a_disconnect_over_an_unknown_submission_stays_armed_and_journaled`; final full crate
  run: **886 passed / 0 failed**, `private_runtime` **37 passed**.
- `cargo +1.98.0 fmt -p nautilus-ondo -- --check`: exit 0, no diffs.
- Strict `cargo test` failure retained as the known MSVC `linker_messages` platform exception;
  `CARGO_BUILD_WARNINGS=allow` used per command only.
- No wheel built/installed; no bindings/stubs/config changed. A wheel rebuild is required to carry
  the behaviour to the app; `docs/ondo.md` §6/§8 and the `ondo_probe.py` runbook need a later
  capability/report update against the rebuilt wheel.

## 2026-09-19 — correction round (review findings 1-6)

Astra's review (`control/cleanup-review-findings.md`, `review/shutdown-review-1.md`) is **not
accepted yet**; this round reproduces and fixes each verified blocker. The first round's RED/GREEN
records in `cleanup/` are preserved untouched; this round's evidence is in
`cleanup/correction-round-1/`.

### Cause / fixes

| # | Review finding | Fix |
|---|---|---|
| 1 | confirming GET / background probe cleared cancel uncertainty for a working order; DMS released over unresolved work | `confirm_cancel` clears only when the applied order `is_settled()`; `ProbeOutcome::Found { settled }` keeps a cancel over a working order; `pending_orders` uses `!is_settled()`; release gate now also requires `outcome == Complete` and empty `unresolved_orders`; `is_clean` requires `released_switch` when the release step is present |
| 2 | read-only restart could DELETE journal-restored orders | the stop skips cancel/confirm steps and pre-registration for `account_read_only`, and reports the inherited work |
| 3 | request tasks were neither drained nor awaited; `reset` could not reopen | `disconnect` calls `tasks.begin_shutdown()` then bounded `finish_shutdown(graceful, abort)` (reaches `Drained`); a later `reset -> start -> connect` works |
| 4 | `stop` then `disconnect` returned Ok by early return | `last_shutdown` records an owed/dirty state; the later async hook runs the bounded REST cleanup, cannot claim a release with no socket, and preserves the dirty result across repeats |
| 5 | the 5 s bound started only after sequential DELETE/GET waits | one `ShutdownBudget` covers task drain, cancel requests, confirmations, release and transport close; every owned order is pre-registered and checkpointed **before** the first request |
| 6 | dirty `StopReport` converted to `Ok` | `disconnect` returns `Err` naming outcome/tasks/switch/outstanding after cleanup and persistence |

Also `DeadMansSwitch::release_frame()` composes the release without releasing, so the switch is
released locally only after the write succeeded (no stream => no false release). When a definitive
refusal proves an order never rested, the submission path clears the cancel the shutdown
pre-registered for it, so a never-rested order cannot leave a permanent registration
(`test_a_definitive_refusal_during_shutdown_clears_its_registration`).

### RED

Reintroduced the reviewed first-round behaviour in a temp copy of the sources, ran the suite, then
restored:

```
private_runtime: 39 passed; 7 failed  (all seven are correction tests)
```

Failing on behaviour (not compile/setup): working confirming GET, background probe working order,
terminal missing fills, read-only restored journal, blocked single submit, blocked batch submit,
stop-before-disconnect dirty result. Evidence `correction-round-1/correction-red.txt`.

### GREEN

- `private_runtime` **47 passed / 0 failed** (`correction-private-runtime-final.txt`).
- full `cargo +1.98.0 test -p nautilus-ondo --locked --offline`: **897 passed / 0 failed** (438 unit
  + 71 execution + 39 http_client + 77 http_contract + 43 market_data + 47 private_runtime + 118
  reconciliation + 63 signing + 1 doc-test) — `correction-full-crate.txt`.
- `cargo +1.98.0 test -p nautilus-ondo --features python --locked --offline --test python`: 6 passed
  / 0 failed — `correction-python-feature.txt`. This also compiles the parallel integration task's
  `supports_ordered_shutdown` getter.
- `cargo +1.98.0 fmt -p nautilus-ondo -- --check`: no diffs.
- Strict `cargo test` still fails only on the MSVC `linker_messages` platform exception
  (`correction-strict-windows-linker-failure.txt`); `CARGO_BUILD_WARNINGS=allow` per command only.

### Cross-session note

`crates/adapters/ondo/src/python/factories.rs` was changed by the parallel app-integration task
(`control/pi-app-integration-task.md`), which owns that file. This session did not author it; it is
included in the python-feature build/verification above and was not otherwise modified.

# Pi task: wire Ondo shutdown into the real client lifecycle

You are the trusted implementation agent. Astra is the coordinator and final reviewer. The user explicitly authorized Pi delegation and providing necessary project code, documents, tests, reports and logs to Pi and its configured model (E:/Nautilus-Perps/AGENTS.md). The user requested BOTH shutdown cleanup and R5.2 data acceptance. Your bounded assignment in this invocation is the shutdown implementation and its offline tests. Do the work, not just a proposal.

## Workspace and scope

- Edit only E:/nautilus_trader/.worktrees/ondo-r52-cleanup, principally crates/adapters/ondo, and your evidence directory E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/cleanup.
- Fork base: ff57243; isolated branch task/ondo-r52-cleanup. App base: aef07e4. Main checkouts have unrelated changes; do not edit, clean, reset, stage, commit, merge or push them. No git commits or remote actions in this assignment.
- Read the fork AGENTS.md, AI_POLICY.md, CONTRIBUTING.md, relevant developer coding/testing guides and BUILD_WINDOWS.md. Read the app AGENTS.md as the delegation contract. This is local project work, not an upstream submission.
- Read the app continuation plan E:/Nautilus-Perps/docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md (R3.3, R4, R5), docs/ondo.md, and reports/ondo-acceptance/20260919T062234Z-r5/README.md.
- Do not read .env, real credentials, user auth files or unrelated private data. No remote authenticated requests, orders, cancellations, DMS, funds or deployments. Local HTTP/WS mocks with synthetic credentials are authorized. Cargo dependency/build operations are authorized. Do not disable TLS or weaken warnings globally.

## Confirmed gap and intended behavior

OndoAccountRuntime::stop_and_wait exists in crates/adapters/ondo/src/execution.rs around line 2059 and has six call sites only in tests/private_runtime.rs. The actual native client fn stop around 3963 aborts tasks/drops the private stream; async disconnect around 4056 only awaits the transport stop. Consequently the real lifecycle never cancels owned orders, confirms cancellation or applies the ordered DMS release gate.

Trace the actual LiveNode/kernel/execution engine lifecycle and its timeout/order of stop/disconnect, including the synchronous hook. Integrate an owned, bounded shutdown into the real client path. Do not merely expose a function that no lifecycle caller awaits, change a report flag, or duplicate a second account state owner. Keep transport and event processing alive for cancellation acknowledgements/reconciliation as needed, then terminate and await every owned task. Avoid RefCell/reentrant borrow panics, deadlock, detached tasks and losing the journal when the caller times out.

Stop new risk first; cancel ONLY this run's owned orders by stable client/venue identity; confirm terminal status and reconcile fills/unknown submissions. Never use market/account-wide cancellation to clean unrelated orders. Unknown/cancel-timeout must remain explicit and journaled, with DMS unreleased until the existing safety gate is satisfied. Preserve restart recovery and fail-closed admission. Read-only must never send DELETE, arm/release DMS or become trading-ready. Real venue protocol remains unverified.

## Required tests and evidence

1. Write real client/factory lifecycle regression tests first. Save a RED run showing the actual missing behavior (not a compiler/setup failure), then implement and save GREEN. Exercise the actual ExecutionClient hooks used by LiveNode, not only OndoAccountRuntime directly.
2. Cover successful ordered cleanup; cancellation timeout/unconfirmed cancel; unknown submission; restart recovering unresolved journal state before accepting risk. Also cover repeated stop/disconnect, synchronous-stop-before-disconnect ordering if applicable, read-only no side effects and external order ownership. Use actual production objects and local protocol mocks, with deterministic waits rather than sleep races.
3. Preserve all existing meaningful tests. Do not add test-only public production APIs or weaken checks to get green. Follow the existing Rust test conventions. If an existing frozen contract changes intentionally, explain the old/new behavior rather than deleting coverage.
4. Run targeted lifecycle tests and full `cargo +1.98.0 test -p nautilus-ondo --locked --offline`, cargo fmt --check for this crate, and Python feature check/tests relevant to native factory. Capture exact commands, exit codes and test counts. Cache reuse is permitted: inspect existing target locations; avoid a needless fresh full build. Use CARGO_TARGET_DIR=E:/nautilus_trader/target for cached cargo tests unless another existing cache is demonstrably appropriate. Shared target usage must be sequential. Consult BUILD_WINDOWS.md for clang and Python environment.
5. If Windows build warnings block strict tests, retain the strict failure and perform only a per-command diagnostic allowance; never claim it is strict success. Resolve ordinary environment issues yourself without installing unrelated tools or broad configuration changes.
6. Before finishing, create cleanup/README.md with cause, design/lifecycle ordering, exact changed paths, test results, red/green evidence, limits and remaining risks. Add a concise cleanup/progress.md after each major step so Astra can monitor. Include any application-side capability/report updates needed later. Do not build/install a new wheel in this first invocation; report whether bindings require changes so Astra can schedule the build.

Implement only this shutdown task. The data replay/observation task has a different owner. Never modify the other app worktree. Return a concise Chinese summary with evidence paths and unresolved items, not an unsupported completion claim.

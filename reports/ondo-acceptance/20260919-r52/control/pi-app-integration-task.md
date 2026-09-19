# Pi task: make the app report the installed shutdown capability truthfully

You are still the trusted Pi execution agent. R5.2 data acceptance is complete and reviewed; leave its raw inputs and finished numeric outputs unchanged. Astra is coordinating the ongoing native shutdown correction in another Pi session. Your new bounded task is preparing the application/native-capability integration. The new wheel will be built only after native review passes.

## Ownership / files

- App worktree E:/Nautilus-Perps/.worktrees/ondo-r52-app: own src/ondo_probe.py, tests/test_ondo_probe.py, docs/ondo.md, and a narrowly scoped note in the continuation plan if useful.
- Fork worktree E:/nautilus_trader/.worktrees/ondo-r52-cleanup: you may edit ONLY crates/adapters/ondo/src/python/factories.rs for a production capability property. Do not edit execution.rs, reconciliation.rs, private_runtime tests or any other file owned by the native Pi agent.
- Evidence: E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/integration. Keep progress.md and README.md.
- Do not run cargo, maturin, stub generation or install anything yet: native Pi is compiling against the shared target. Do not modify generated stubs by hand. Do not change pyproject wheel until the built candidate is independently verified. No commits/staging/merge/push.
- Same no-credential/no-private-network/no-order rules. App tests use PYTHON_DOTENV_DISABLED=1 and the canonical app venv, with worktree src imported. Read app AGENTS.md, project rules and the latest native review artifact to understand that native acceptance is still pending.

## Problem

Current ondo_probe.py hardcodes converging_stop_available=false and says the real native disconnect never invokes ordered cleanup. After the native fix is compiled that statement becomes stale, but flipping it unconditionally would lie for the still-installed R5 wheel. Do not guess support from version 2.0.0rc4, a filename, a worktree path or a Git commit.

## Required behavior and minimal design

1. Add a minimal read-only production capability getter `supports_ordered_shutdown` to the Python OndoExecutionClientFactory in python/factories.rs, through the existing pyo3/stub generator annotations. It returns true in the candidate native build and describes implemented native lifecycle support, NOT an assertion of venue protocol acceptance or that a particular account was cleaned. The old installed factory has no such attribute, so it must be treated as unsupported/unknown. Do not add a duplicate execution implementation or test-only interface.
2. App capability detection must be read-only/offline: absence/false/wrong type/failing lookup stays unavailable with a useful reason. Do not construct authenticated configs, clients, or load credentials just to detect it. Public mode still registers no execution factory; introspecting the native factory TYPE/object without constructing an execution client is permitted.
3. Update all relevant report/manifest/unverified/status/log paths consistently. Distinguish adapter capability from observed per-run cleanup: protocol_verified remains false; exit_code_zero_means_clean_account remains false; synthetic paper account status never proves a real account; the probe still sends zero orders. Do not set actual cancellation/confirmation/release flags true merely because the adapter is capable. No new sandbox submission behavior.
4. Regression tests FIRST: old/missing capability stays false; supported adapter reports implemented capability; errors/ambiguous marker fail closed; native capability does not promote sandbox protocol or clean-account verdicts and preserves no-side-effect modes. Update only genuinely obsolete frozen assertions, retaining their original safety intent. Save RED/GREEN commands/results.
5. docs/ondo.md must describe old-wheel fallback vs the new native lifecycle, bounded incomplete/error outcomes and read-only behavior, while explicitly keeping real sandbox auth/order/DMS semantics unverified. Do not state the native review has passed before Astra says so. Use wording like 'candidate capability, enabled only by a wheel exposing ...' until delivery is finalized.
6. Run targeted probe tests and full app tests if changes pass; no external network calls in this task. The current installed R5 wheel is useful evidence for unavailable fallback. Document the one native property that must appear in generated stubs and candidate runtime checks in the later build task.

Finish with concise Chinese results, changed paths, exact test evidence and remaining build/acceptance dependency. Avoid redoing the already-accepted public observation.

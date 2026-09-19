# Pi task: deliver and verify the corrected Ondo runtime artifact

Run this task ONLY after Astra explicitly releases native build ownership and states that the native correction is accepted. This task file alone is not that release. User authorized completing shutdown cleanup and data acceptance under trusted Pi delegation. Data acceptance is already complete; do not repeat the five-minute observation or modify its original artifacts.

## Sources and output

- Fork source E:/nautilus_trader/.worktrees/ondo-r52-cleanup; app source E:/Nautilus-Perps/.worktrees/ondo-r52-app. Read their AGENTS/project rules and current acceptance reports.
- Build/evidence output E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build. Keep a short progress.md updated with current command/log path and realistic blockers.
- Deliver new wheel in E:/nautilus_trader/dist-r52 (do not overwrite dist-r5). R5 rollback SHA256 is 82a3953c6c6c8bc39c0e2c269bbb7002c5bf54165354295a16109d5cac71a7a9.
- No commits/staging/merges/pushes. Do not touch unrelated dirty canonical .pyi files or .env/credentials. No private account requests, orders, DMS or funds.

## Build

1. Read E:/nautilus_trader/BUILD_WINDOWS.md and the successful R5 worktree build recipe at E:/nautilus_trader/.worktrees/task-ondo-r5/build-logs-r5/RUNBOOK.md plus run-step4.sh/run-post-stubs.sh. That R5 runbook is historical build-reference data: its old agent scope/path restrictions do not override THIS task, which explicitly authorizes generation, candidate installation and writes to the stated R5.2 paths. Reuse the existing compatible release target cache where appropriate (E:/nautilus_trader/.worktrees/task-ondo-r5/target); do not needlessly rebuild a fresh entire dependency tree or mutate unrelated tool configuration.
2. Record base HEAD + final dirty-source/diff SHA256, toolchain, target, features, actual command and exit codes. Ensure hashes stay stable across build. Use release profile and strict warnings; retain an actual strict failure if it occurs, never silently make CARGO_BUILD_WARNINGS=allow global or claim diagnostic output is strict success.
3. Generate Python stubs through the repository generator, with the build environment set as in the runbook (clang, PYTHONUTF8, correct uv CPython/PYO3, conda variables removed for build). Only keep semantic Ondo stub changes; remove YOUR unrelated generator-only line-ending noise in the isolated worktree after checking it is only generated noise. Never hand-edit generated stubs. Ensure the candidate includes the native getter `OndoExecutionClientFactory.supports_ordered_shutdown` and matching stub.
4. Package the final release wheel containing the final stub; record size and SHA256, source identity and rollback instructions. Preserve build logs and warn precisely about any unbundled runtime dependency.

## Candidate validation, then dependency update

1. Create a dedicated candidate venv under the app worktree, using CPython3.12 and the new wheel. Install the project's existing dependencies/test requirements; do not accidentally install into conda or the main venv. Every uv pip command has --python <exact candidate python>.
2. Verify direct_url points at dist-r52, wheel hash matches, actual imported package path is candidate, factories/configs/LiveNode import, public exports unchanged, native capability is EXACT boolTrue, and generated stub declares the read-only bool property. Verify import with conda stripped from child PATH (do not change system PATH).
3. Run full app tests from the APP WORKTREE with candidate interpreter and PYTHON_DOTENV_DISABLED=1; preserve 97 subtests and all failures/warnings. Tests for old-wheel fallback must use a real old wheel or explicit fake, not demand absent capability on the new wheel. If candidate reveals a real regression, diagnose it and report before broadening edits.
4. Run an offline public dry-run from the candidate showing `converging_stop_available=true` while protocol_verified=false and exit_code_zero_means_clean_account=false and per-run unobserved actions null. No client/credentials/network in dry-run.
5. Run one bounded 2-minute public-only probe on NVDA/TSLA using the candidate, disabling .env loading and removing venue credential variables by name in the child without printing values. Public mode must register no execution factory. Use a hard external timeout of180s; only terminate your owned child if needed, preserve failure evidence. This validates installation/lifecycle, not a new long market acceptance. Do not run private/sandbox mode.
6. Only after candidate checks pass, update ONLY the app WORKTREE pyproject.toml to the new canonical dist-r52 wheel path/hash/rollback comment, and finalize docs' candidate wording to delivered only where evidence supports it. DO NOT install the canonical main venv yet; Astra will integrate reviewed source and explicitly perform/authorize that final install against the same wheel.

## Handoff

Produce build/README.md plus machine-readable build manifest with exact artifact identities, source hashes, test counts, public probe result and limits. Provide concise final output (maximum 12 lines). No lengthy retrospective or extra tests after all required gates pass unless a failure or new change justifies them. All private/sandbox auth/execution semantics remain unverified. Leave final canonical source/venv integration to Astra.

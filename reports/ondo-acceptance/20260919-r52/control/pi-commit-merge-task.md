# Commit and locally merge the completed Ondo work

User explicitly requested `提交 合并` after accepting the completed shutdown/R5.2 work. Execute local commits and fast-forward merges for BOTH repositories; no push, pull, fetch, PR, remote messages, deployments or trading actions. You are trusted Pi execution agent; Astra reviews final commit trees and merged state. This is existing completed work, not a new implementation round.

## Exact repositories and branches

- App canonical E:/Nautilus-Perps, base main at aef07e4e7d7e32fedf9acd8a6cba63700d092b6e. Task worktree E:/Nautilus-Perps/.worktrees/ondo-r52-app, branch task/ondo-r52-app.
- Fork canonical E:/nautilus_trader, base onde-perps at ff57243f74d730b1ababca7ee8e7b37e7c840287. Task worktree E:/nautilus_trader/.worktrees/ondo-r52-cleanup, branch task/ondo-r52-cleanup. Use per-command git safe.directory overrides if required, never change global config.
- All approved changes have already been copied into the canonical checkouts and their bytes matched the worktrees in the prior task. The main app venv already has the validated dist-r52 wheel. Preserve these exact source bytes.
- Read AGENTS.md and existing review/canonical-sync.md, top-level report, build manifest. No credentials/.env or unrelated personal data access.

## Commit scope

Fork: exactly these seven paths:
crates/adapters/ondo/src/execution.rs
crates/adapters/ondo/src/python/factories.rs
crates/adapters/ondo/src/reconciliation.rs
crates/adapters/ondo/src/websocket/private/stream.rs
crates/adapters/ondo/tests/private_runtime.rs
crates/adapters/ondo/tests/reconciliation.rs
python/nautilus_trader/adapters/ondo/__init__.pyi

App: AGENTS.md plus src/ondo_probe.py, tests/test_ondo_probe.py, docs/ondo.md,
docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md, pyproject.toml, and the current
reports/ondo-acceptance/20260919-r52 evidence package.

Copy the evidence package from canonical app into the app task worktree BEFORE staging. Preserve
raw evidence bytes. Include reports, manifests, public raw/tape data, test/build logs and helpers,
but exclude every control/pi-*-session.jsonl and control/pi-*-events.jsonl (about95MB of model
transcripts), __pycache__, *.pyc and all wheel/pyd/exe binaries. Do not add old untracked P0/P1/P2
directories or other surveys/stage1 data; they remain local source references. Do not use git add -A.
The expected evidence package is roughly19MB before Git compression. Check actual staged sizes.

## Verification and sequence

1. Verify branch/HEAD/index and canonical-versus-worktree hashes for every source path. Preserve
   unrelated dirty fork stubs and untracked files. Both indexes must initially be empty; do not
   accidentally include pre-staged work. Keep a concise scope manifest in control/merge-scope.json.
2. Run fresh appropriate checks before commit: full app tests from app worktree using its candidate
   venv (or canonical venv with worktree cwd), PYTHON_DOTENV_DISABLED=1; full nautilus-ondo crate
   tests from fork worktree using E:/nautilus_trader/target. Use the already documented per-command
   Windows linker-message exception only, capture it as such. Do not rebuild the wheel or rerun
   public observation. Preserve source hashes before/after tests; no implementation changes.
3. Stage only the explicit approved files and selected evidence. Run staged source/document
   whitespace checks. Raw logs/tapes are exact evidence and may retain original whitespace; do not
   rewrite raw evidence to satisfy source-style checks. Inspect staged name list, total sizes and
   ensure no wheel/.env/model-session transcript slips in. Respect existing commit hooks; never use
   --no-verify. If a hook fails, diagnose/report the exact failure rather than bypassing it.
4. Commit on task branches with concise English subjects, no Conventional Commit prefixes and no
   AI coauthor/branding footer. Suggested: `Complete bounded Ondo execution shutdown and recovery`
   (fork); `Deliver Ondo shutdown capability and R5.2 data acceptance` (app). One source/evidence
   commit per repo is sufficient. Include the new user-authorized AGENTS.md in app.
5. Canonical checkouts contain duplicate uncommitted copies of the same source. ONLY after each
   task commit is verified to contain exactly those bytes, clear ONLY the seven fork tracked paths
   or five app tracked paths back to their canonical HEAD with an explicit path-limited git restore,
   then immediately `git merge --ff-only <task branch>` into the stated base. This restore is safe
   only because the exact bytes are already committed and remain in the task worktree; compare
   hashes immediately before doing it. Never reset/restore/stash unrelated paths. New app AGENTS
   and evidence files already exist untracked in canonical; verify their bytes equal the incoming
   commit. Git may accept identical untracked files; if it refuses, stop that merge and report the
   exact paths instead of deleting or force-overwriting them. Do not alter merge strategy silently.
6. Verify task commit is ancestor of base and ideally base HEAD equals task HEAD (fast-forward);
   verify merged source byte hashes equal accepted worktree and relevant scope is clean. Run main
   app tests on merged result. Native merged bytes identical to just-tested native commit are
   sufficient; no expensive source-path rebuild needed unless any byte differs.
7. Keep both worktrees: they hold candidate/build venvs and local evidence required for rollback,
   so no worktree/branch removal or broad cleanup. Leave unrelated dirty/untracked files alone.

Write a concise merge result JSON/MD with both full commit SHAs, base/task branches, test results,
scope, remaining unrelated changes, and `pushed=false`. Avoid a circular record: the exact final
commit IDs can be written to a local post-merge record rather than amending commits repeatedly.
Update top-level report with a short dated note about this new commit/merge authorization before
committing; preserve historical statements that no Git actions occurred during earlier acceptance.
Do not claim commit or merge until actual commands succeeded. Final response <=10 lines.

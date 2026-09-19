# R5.2 build progress (Pi build-release task)

Run root: `E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build`.
Fork worktree (source): `E:/nautilus_trader/.worktrees/ondo-r52-cleanup` @ `ff57243`.
App worktree (candidate + tests): `E:/Nautilus-Perps/.worktrees/ondo-r52-app` @ `aef07e4`.
New wheel output: `E:/nautilus_trader/dist-r52` (do not overwrite `dist-r5`).
R5 rollback SHA256: `82a3953c6c6c8bc39c0e2c269bbb7002c5bf54165354295a16109d5cac71a7a9`.

## Current status

- [x] Step 0: environment + frozen source hashes recorded
- [x] Step 1: uv sync in fork worktree
- [x] Step 2: strict release `maturin build` -> real failure (documented Windows `linker_messages` denial at `nautilus-persistence-macros`; retained as evidence)
- [x] Step 3: release build with per-invocation allowance -> exit 0, 1689s, 0 warnings (intermediate wheel sha256 `4c0cac31b01414a57436617868e38cc054e8853b754f3393bd3802562ae7d70a`)
- [x] Step 4: stubs via repository generator (nextest, separate cache) exit 0, 295s; normalized CRLF noise -> only `python/nautilus_trader/adapters/ondo/__init__.pyi` (+2 lines: `@property def supports_ordered_shutdown`)
- [x] Step 5: final wheel exit 0, 5s (warm release target), 63,621,311 bytes, sha256 `4d19c5df5bec73391a760d4b8643e2f8eaa53ce183dca88ee917454e2b91d642`; bundled Ondo stub confirmed; known unbundled dep: `zlib.dll` (resolved from `C:\ProgramData\miniconda3\zlib.dll` at build time, not bundled)
- [x] Step 6: candidate venv + capability/stub/import checks -> all pass
- [x] Step 7: full app tests -> 836 passed, 97 subtests, 1 warning, exit 0
- [x] Step 8: offline public dry-run -> capability true, protocol_verified false, per-run null, zero requests
- [x] Step 9: bounded public-only probe -> exit 0, 131s, complete, exec_client null, zero orders
- [x] Step 10: app worktree pyproject + docs delivery update
- [x] Step 11: README + machine-readable manifest

## Final

Delivered wheel sha256 `4d19c5df5bec73391a760d4b8643e2f8eaa53ce183dca88ee917454e2b91d642` (63,621,311 bytes) at
`E:/nautilus_trader/dist-r52/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`. Candidate venv verified, app
tests green (836 + 97 subtests, 1 existing warning), dry-run and bounded public probe clean. Fork final status:
7 modified files (6 accepted native paths + the 2-line Ondo stub); app worktree final status: 5 modified files
(`docs/ondo.md`, continuation plan, `pyproject.toml`, `src/ondo_probe.py`, `tests/test_ondo_probe.py`) plus the
pre-existing untracked `AGENTS.md`. No commits/staging/merges/pushes; no canonical main checkout/venv touched;
`dist-r5` untouched. See `README.md` and `manifest.json`.

## Coordinator note acknowledged (2026-09-19)

Read `control/build-stage-coordination.md` (Astra, authorized). Decision: stub generation will use
`NAUTILUS_STUB_PROFILE=nextest` with the separate existing cache `E:/nautilus_trader/target`, leaving
`E:/nautilus_trader/.worktrees/task-ondo-r5/target` reserved for the release wheel. Verified from the
repository itself that this is the canonical stub profile: `Makefile` sets `CARGO_CI_PROFILE ?= nextest`
and the `py-stubs` target passes `NAUTILUS_STUB_PROFILE=$(CARGO_CI_PROFILE)`; `BUILD_WINDOWS.md`'s
successful procedure also used `NAUTILUS_STUB_PROFILE=nextest`. Stub output comes from the pyo3
macro/inventory declarations, not optimization level, so the generated API is unchanged. The final
wheel remains `--release`. Generator used unchanged; no manual stub edits; no reduction in candidate
tests. The initial strict failure and platform-exception classification are retained.

Blockers: none yet.

## Log index

(updated as logs are produced)

# R5.2 DATA acceptance — progress

Worktree: `E:/Nautilus-Perps/.worktrees/ondo-r52-app` (base `aef07e4`, branch `task/ondo-r52-app`).
Output root: `E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/data`.
Interpreter: `E:/Nautilus-Perps/.venv/Scripts/python.exe` (nautilus_trader 2.0.0rc4, R5 release wheel
`82a3953c6c6c8bc39c0e2c269bbb7002c5bf54165354295a16109d5cac71a7a9`).
Isolation: every script sets `PYTHON_DOTENV_DISABLED=1` and `PYTHONPATH=<worktree>/src` so no `.env`
is discovered and the worktree source is what is imported.

## Done

1. **Immutability frozen.** `provenance/input_manifest_before.json` (25 files, tree
   `7f0b4855...c24d5`) captured before replay; `provenance/input_manifest_after.json` is byte-equal.
   `provenance/source_manifest.json` covers worktree `src/`, `tests/`, `pyproject.toml`,
   `docs/ondo.md`, `AGENTS.md`. Dirty files: untracked `AGENTS.md` only.
   `provenance/runtime_identity.json` records interpreter, wheel, python-dotenv 1.2.3
   (`PYTHON_DOTENV_DISABLED` present in `dotenv/main.py`), git HEAD, status.
2. **Historical replay** of the P2 ONDO-HL tape with the current analyzer, exit 0:
   `historical/replay/` (`ondo_depth.json`, `ondo_depth.md`, `ondo_depth_summary.csv`,
   `ondo_depth_hits.csv`), raw output in `historical/replay_stdout.txt`.
3. **Old-vs-new comparison** (`historical/comparison/old_vs_new.{json,csv}`): 12/12 buckets shift
   *exactly* the old `pass` count into the new `quantity_step_unknown` count; **zero** non-step
   reject-counter differences. That isolates the whole outcome difference to the F08 quantity-step
   rule. Event-age maxima also differ because the definition changed from venue-to-venue to
   local-receipt-minus-venue-event (F09); fees are unchanged.
4. **Tape content inventory** (`provenance/tape_content_summary.json`): single session, no halt, no
   disconnect/snapshot_ready, 0 gaps/dropped, no `size_increment` anywhere.
5. **Synthetic fixtures** (`historical/synthetic/`, all marked SYNTHETIC) with the real analyzer:
   halt-survives-disconnect, first-frame-recovery, metadata-fee-update, legacy-no-size-increment,
   gap-incomplete. `historical/synthetic/verification.json` — all 14 checks passed.
6. **App data tests** — `244 passed in 2.32s`, exit 0, no warnings
   (`tests/commands.json`, `tests/app_data_tests_output.txt`).
7. **Public preflight** NVDA/TSLA: exit 0, `complete=true`, `missing=[]`
   (`public/preflight/`).
8. **Public observation**: one bounded 5-minute ONDO/HL watcher, exit 0 in 303.898 s; both venue
   legs observed for both symbols; L2 dropped=0 gaps=0 complete; raw_ondo complete
   (`public/observation_run_meta.json`, `public/evaluation.json`).
9. **New-tape replay** with the same criteria, exit 0; real increments ONDO 0.01 / HL 0.001;
   `executable=false` everywhere (`public/replay/`, `public/README.md`).
10. **Deliverables written**: `README.md`, `summary.json`, `public/README.md`,
    `historical/replay/README.md`; superseded old reports referenced, originals untouched.

## Final status

All in-scope items complete for the **data scope only**. `sandbox_auth_verified=false`,
`sandbox_execution_verified=false` by design (no private/sandbox request was in scope). No app
source file was edited; no fork file touched; no cargo run; no commit/stage/merge/push.

### Correction pass (review feedback)

The report was corrected against the machine artifacts without touching any raw evidence or
numerical CSV:

- `README.md` now states the historical reject counters **per bucket** (411/336 `stale_book`,
  207/196 `event_skew`, 63/62 `quantity_step_unknown`, plus `no_book` 2/2/1/2 and
  `future_event_time` 1 for TSLA ONDO>HL) with the exact denominator "12 buckets = symbol ×
  direction × notional", and verifies each row sums to `samples`.
- `README.md` adds a "Renderer boilerplate that does NOT apply to this run" note for the
  generated markdown's generic `Zero opportunities is a result...` and closing ONDO–ASTER
  "What P2 acceptance still needs" text; historical economic output is unassessable (unknown
  quantity step), not a verified zero-opportunity market, and the observed pair is ONDO–HL.
- `summary.json` carries the same notes as machine-readable fields
  (`historical_replay.renderer_boilerplate_inapplicable`,
  `historical_replay.historical_economic_output_assessable=false`,
  `historical_replay.historical_zero_opportunity_verified=false`, plus
  `historical_replay.per_bucket`).
- Artifact shorthand fixed to the real filenames `ondo_depth_summary.csv` / `ondo_depth_hits.csv`.

## Notes / boundaries

- No fork files touched, no cargo run. No app probe/test file edited.
- No profitability or real-fill claim anywhere; mapping stays unverified and `executable=false`.
- History is a research sample; missing phenomena are named as unknown/absent, never zero.
- `__pycache__`/`.pyc` are excluded from the source manifests so a test run is not mistaken for
  a source change.

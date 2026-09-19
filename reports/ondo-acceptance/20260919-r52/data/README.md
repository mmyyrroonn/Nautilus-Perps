# R5.2 DATA acceptance — historical replay + finite public observation

Run date 2026-09-19 (UTC), Saturday. Scope: **public unauthenticated data only**, plus offline
analysis. No sandbox/private request, no credential, no order, no cancel, no deployment. App
worktree `E:/Nautilus-Perps/.worktrees/ondo-r52-app` (base `aef07e4`), current R5 wheel
`nautilus_trader 2.0.0rc4` sha256 `82a3953c…a7a9`.

Result layers (never conflated):

| layer | result |
|---|---|
| implemented | current `ondo_depth.py`, `spread_watch.py`, `ondo_preflight.py` run as-is |
| offline_verified | 5 app data-test modules: **244 passed**, exit 0; 5 synthetic fixtures: **14/14 checks pass** |
| public_observed | public preflight complete; one bounded 5-minute ONDO/HL NVDA/TSLA watcher exit 0 in 303.9 s; both venue legs live for both symbols |
| sandbox_auth_verified | **false** |
| sandbox_execution_verified | **false** |

Machine-readable roll-up: [`summary.json`](summary.json).

## 1. Immutability and identity

- Historical source `E:/Nautilus-Perps/reports/ondo-acceptance/20260915T024236Z-p2` was **read
  only**. `provenance/input_manifest_before.json` (25 files, tree
  `7f0b4855a46318f1abff4f3f690c3f1853ee21a37970b02f23cc30dfbc1c24d5`) and
  `input_manifest_after.json` are byte-equal: **immutable**.
- Source + runtime identity: `provenance/source_manifest.json`,
  `provenance/runtime_identity.json`. App HEAD `aef07e4e…`, branch `task/ondo-r52-app`; the only
  dirty file is untracked `AGENTS.md`. python-dotenv 1.2.3 and `PYTHON_DOTENV_DISABLED` support
  confirmed in `dotenv/main.py`; every script ran with `PYTHON_DOTENV_DISABLED=1` and
  `PYTHONPATH=<worktree>/src` so no `.env` is discovered and the worktree source is imported.

### Superseded reports (originals preserved byte-for-byte)

- `reports/ondo-acceptance/20260915T024236Z-p2/depth-analysis-ondo-hl/` — the old ONDO-HL analysis
  of the same immutable tape. **Superseded for ONDO-HL by
  [`historical/replay/`](historical/replay/)** and
  [`historical/comparison/`](historical/comparison/). It is **not** overwritten or deleted.
- `reports/ondo-acceptance/20260915T024236Z-p2/depth-analysis/` — an ONDO–ASTER analysis of the
  same run directory. It is a **different venue pair** and is **not** replayed or superseded here;
  an ONDO–HL sample never substitutes for ONDO–ASTER (plan R5.2).

## 2. Historical replay (Deliverable A)

Command (exit 0; full stdout `historical/replay_stdout.txt`):

```
python src/analysis/ondo_depth.py --dir <p2> --symbols NVDA,TSLA --venues ONDO,HL \
  --notionals 100,500,1000 --max-age-ms 2000 --max-skew-ms 500 --out historical/replay
```

New outputs: `historical/replay/ondo_depth.json`, `historical/replay/ondo_depth.md`,
`historical/replay/ondo_depth_summary.csv`, `historical/replay/ondo_depth_hits.csv`.

### What the P2 tape actually contains

From `provenance/tape_content_summary.json` (and `observation`-independent `inspect_tape.py`):

- NVDA: 1 session, 716 lines — books 484, quotes 199, funding 28, instruments 2, status 1,
  run_end complete, dropped 0, gaps 0.
- TSLA: 1 session, 629 lines — books 484, quotes 112, funding 28, instruments 2, status 1,
  run_end complete, dropped 0, gaps 0.
- **No** `size_increment` anywhere (only `size_precision` 2/3), **no** halt, **no**
  disconnect/`snapshot_ready`, **no** metadata change/`metadata_stale`, **no** second session,
  **no** gap. These are absent, i.e. unknown, **never zero**.

### Old vs new — every difference is one rule change

`historical/comparison/old_vs_new.{json,csv}` compares the old saved summary with the new replay
(12 buckets: 2 symbols × 2 directions × 3 notionals):

- **12/12 buckets shift exactly the old `pass` count into the new `quantity_step_unknown` count**
  (NVDA 63→63, TSLA 62→62 per bucket). The old analyzer derived a step from `size_precision`
  (`10**-size_precision`); the current analyzer refuses it (F08) and withholds the priced row.
- **All non-step reject counters are identical** (`non_step_reject_mismatches: []`): `stale_book`,
  `event_skew`, `future_event_time`, `no_book` counts are unchanged. So the entire outcome
  difference is the quantity-step rule. HALT and completeness rules changed nothing here
  **because the sample contains no halt and is complete** (dropped 0, gaps 0, single session);
  that is absence of evidence, not evidence they behave the same on a halted/incomplete tape.
- Fees are **unchanged**: ONDO 2.50 bps `instrument_metadata`, HL 0.90 bps `registry`; the old
  "step 0.01 / origin instrument_metadata" row is now "step unknown / quantity_step_unknown".
- Event-age maxima differ **by definition**, not by data: the old field was venue-to-venue
  (`ts_event_ns − book ts_event_ns`), the current field is local-receipt-minus-venue-event
  (`ts_init_ns − book ts_event_ns`, F09). NVDA ONDO>HL old 240.00/5829.54 ms → new
  1158.69/6732.58 ms; TSLA 264.00/5829.54 → 1139.09/6716.35. The new value carries the
  unmeasured local clock offset (`clock_offset_unknown`), which the old one cancelled.

### Current historical notional outputs and rejection reasons

Denominator: **12 buckets**, each bucket = one (symbol × direction × notional). Within a
(symbol, direction) the three notionals repeat the same gate counts, because the base quantity
is chosen only after the gate and here no quantity is ever chosen.

Every bucket: `common_step = unknown`, `step_origin = quantity_step_unknown`, `pass = 0`,
`hits = 0`, and gross/after-fees medians are **`unknown`** (not negative, not zero) because no
quantity was computed. Exact **per-bucket** reject counters (identical across the three
notionals of a direction) as they appear in `historical/replay/ondo_depth_summary.csv`:

| symbol | direction | samples | quality_pass | stale_book | event_skew | quantity_step_unknown | future_event_time | no_book |
|---|---|---|---|---|---|---|---|---|
| NVDA | ONDO>HL | 683 | 63 | 411 | 207 | 63 | 0 | 2 |
| NVDA | HL>ONDO | 683 | 63 | 411 | 207 | 63 | 0 | 2 |
| TSLA | ONDO>HL | 596 | 62 | 336 | 196 | 62 | 1 | 1 |
| TSLA | HL>ONDO | 596 | 62 | 336 | 196 | 62 | 0 | 2 |

The reject columns sum to `samples` in every row (NVDA 411+207+63+2 = 683; TSLA ONDO>HL
336+196+62+1+1 = 596; TSLA HL>ONDO 336+196+62+2 = 596). The figures previously quoted as
"1233/1008" and "621/588" and "189/186" were 3× aggregates across the three notionals; the
per-bucket values above are the ones in the CSV and are **not** recomputed by prose.
**No profitability or fill is claimed.**

### Renderer boilerplate that does NOT apply to this run

The generated `historical/replay/ondo_depth.md` still carries generic renderer text from the
P2/ONDO–ASTER era. It is **not** a statement about this ONDO–HL replay and is left in the
generated file unedited (a renderer/source refactor was out of scope for this correction):

- `"No hit ... Zero opportunities is a result, not a missing section."` — inapplicable here.
  Zero hits is **not** a verified zero-opportunity market: every quality-pass moment was
  *withheld* with `quantity_step_unknown` because the P2 tape carries no real `size_increment`,
  so the historical economic output is **unassessable**, not measured as zero.
- The closing `"What P2 acceptance still needs"` items name `ONDO,ASTER` and an ONDO–ASTER
  recording/mapping. This acceptance replays and observes the **ONDO–HL** pair; the live
  evidence is `public/observation` (pair `ONDO-HL`), and ONDO–HL is not a substitute for
  ONDO–ASTER in the separate ONDO–ASTER analysis.

### Missing phenomena → regression tests + synthetic fixtures

Existing offline regressions (`tests/test_ondo_depth.py`, see `summary.json`
`existing_regression_tests`): halt/resume, disconnect + first-frame recovery, metadata fee
change/staleness, multi-session and metadata-less session, recording gap/incomplete, unknown
quantity step, local-clock event age.

Because the P2 sample does not contain those phenomena, five **explicitly synthetic** fixtures were
built and replayed through the real analyzer (`historical/synthetic/`, each with `SYNTHETIC.md`):

| fixture | expected / observed |
|---|---|
| `halt-survives-disconnect` | pass before halt; `disconnected=2` and `market_halted=1` separately; `snapshot_ready` never lifts the halt |
| `first-frame-recovery` | one recovery frame usable; its own ages seq4 `0/0`, seq9 `1000/1000`; a later quote refreshes neither |
| `metadata-fee-update` | seq4 `3.4 bps`, seq6/seq7 `100.9 bps`; the update never reprices earlier arrivals; snapshot lists `["2.5","100"]` |
| `legacy-no-size-increment` | `pass=0`, `quantity_step_unknown=1`, `step_origin=quantity_step_unknown` |
| `gap-incomplete` | `recording_gap=1` then `no_book=2`; tape `complete=false`, `gaps=1`, `dropped=3` |

`historical/synthetic/verification.json`: **all 14 checks passed**.

## 3. Finite public observation (Deliverable B)

### Preflight (no `.env`)

`python src/ondo_preflight.py --symbols NVDA,TSLA --out public/preflight` → exit 0,
`complete=true`, `missing=[]`, `NVDA→NVDA-USD-PERP.ONDO`, `TSLA→TSLA-USD-PERP.ONDO`
(`public/preflight/meta.json`).

### Bounded watcher

One run, public only:

```
spread_watch.py --venues ONDO,HL --symbols NVDA,TSLA --minutes 5 --max-restarts 0 \
  --record-l2 --reference none --out public/observation
```

`public/observation_run_meta.json`: **exit 0, elapsed 303.898 s**; venue credential environment
variables were removed by name before the child ran; no `.env`.

Observed live subscription/update counts (from the run's own SUMMARY block):

| symbol | venue | quotes | book updates | trades | funding | fee |
|---|---|---|---|---|---|---|
| NVDA | ONDO | 186 | 1154 | 4 | yes | 2.50 (instrument_metadata) |
| NVDA | HL | 119 | 56 | 54 | yes | 0.90 (registry) |
| TSLA | ONDO | 46 | 1154 | 1 | yes | 2.50 (instrument_metadata) |
| TSLA | HL | 104 | 56 | 54 | yes | 0.90 (registry) |

Both venue legs and both symbols are genuinely observed
(`evaluation.json: both_venues_observed_for_every_symbol=true`). ONDO legs report
`feed_ready=True market_ready=True book_valid=True disconnects=0 metadata_ready=True`.

L2 tape completeness: NVDA 1 fragment/1533 records; TSLA 1 fragment/1378 records; **dropped 0,
gaps 0, complete true** for both (`public/observation/l2/manifest.json`). Raw adapter frames
`raw_ondo/raw_md.jsonl`: 1 segment, 1 session, 2624 records, dropped 0, gaps 0 (from the run's
final log line; there is no separate raw manifest). Instrument metadata carries real increments
ONDO `0.01`, HL `0.001`. The tape status records show `adapter:snapshot_ready` at seq 4 and
`adapter:metadata_ready` notices — no halt and no disconnect.

### New-tape replay (same data-quality criteria)

```
python src/analysis/ondo_depth.py --dir public/observation --symbols NVDA,TSLA --venues ONDO,HL \
  --notionals 100,500,1000 --max-age-ms 2000 --max-skew-ms 500 --out public/replay
```

Exit 0. `common_step=0.01`, `step_origin=instrument_metadata` (real increments now present):

| symbol | direction | $100 samples / pass / hits | median gross bps | median after fees bps | top reject |
|---|---|---|---|---|---|
| NVDA | ONDO>HL | 1515 / 128 / 0 | −5.855 | −9.255 | stale_book |
| NVDA | HL>ONDO | 1515 / 128 / 104 | 3.604 | 0.204 | stale_book |
| TSLA | ONDO>HL | 1360 / 135 / 0 | −8.505 | −11.905 | stale_book |
| TSLA | HL>ONDO | 1360 / 135 / 89 | 4.939 | 1.539 | stale_book |

`hits` are **nominal book comparisons with positive entry-after-fees before the one-leg failure
reserve**; the watcher charges 5.0 bps reserve and reports **0 net-positive rows** for every
direction/symbol. `executable_hits=0`, `executable=false`, `mapping_verified=false`,
`exit_status=unclosed` on every row. **No real fill or profitability is claimed.**

## 4. Tests, gaps, unverified

- Exact data-test command/result: [`tests/commands.json`](tests/commands.json) —
  `244 passed in 2.32s`, exit 0, no warnings (`tests/app_data_tests_output.txt`).
- Gaps/unknowns and unverified items are enumerated in `summary.json`
  (`gaps_and_unknowns`, `unverified_items`). In short: no private/sandbox request was made, no
  protocol item was confirmed, contract mapping stays `executable=false`, the Saturday 5-minute
  sample is not a 24–72 h reliability claim, and local clock offsets remain unmeasured.

## 5. Reproduce

```
cd E:/Nautilus-Perps/.worktrees/ondo-r52-app
$env:PYTHON_DOTENV_DISABLED="1"; $env:PYTHONPATH="E:/Nautilus-Perps/.worktrees/ondo-r52-app/src"
python -m pytest tests/test_market_tape.py tests/test_ondo_depth.py tests/test_ondo_preflight.py \
  tests/test_spread_watch.py tests/test_opportunities.py -q -p no:cacheprovider
# then, from this data/ directory:
python scripts/freeze_manifest.py --label historical-source-before --out provenance/input_manifest_before.json <p2>
python scripts/compare_historical.py --old <p2>/depth-analysis-ondo-hl/ondo_depth_summary.csv \
  --new historical/replay/ondo_depth_summary.csv --out-dir historical/comparison
python scripts/make_synthetic_fixtures.py --out-root historical/synthetic
python scripts/verify_synthetic_fixtures.py --root historical/synthetic
# public: ondo_preflight.py then the 5-minute spread_watch.py then ondo_depth.py (commands in §3)
python scripts/evaluate_public_observation.py --data-root <this data dir>
python scripts/build_summary.py
```

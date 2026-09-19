# Pi task: R5.2 historical replay and finite public data acceptance

You are a trusted Pi execution agent. The user explicitly authorized delegating this project's necessary code/docs/reports to Pi and its configured model, and now requested completion of R5.2 DATA acceptance. Astra handles final review. Implement and execute this bounded task, not merely a plan.

## Scope and ownership

- Work in E:/Nautilus-Perps/.worktrees/ondo-r52-app (base aef07e4). Read its AGENTS.md and CLAUDE.md, and the continuation plan R2/R5.2, docs/ondo.md, and R5.1 report.
- Write acceptance outputs and helper scripts into E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/data. Keep progress.md there after major steps.
- Another Pi session is changing the fork's execution lifecycle. DO NOT touch fork files or run cargo. DO NOT edit app ondo_probe.py, test_ondo_probe.py or pyproject.toml yet; those are reserved for subsequent lifecycle integration. Existing analyzer/tape/watcher data code can be fixed only if your acceptance reveals a concrete bug, with failing regression before implementation. Preserve unrelated changes. No commits/staging/merges/pushes/deployments.
- Historical source (immutable): E:/Nautilus-Perps/reports/ondo-acceptance/20260915T024236Z-p2, including l2/, raw_ondo/, prior depth-analysis and depth-analysis-ondo-hl outputs. Other public evidence can be read as needed. Do not rewrite, move or delete original evidence, including its manifests/reports.
- Use E:/Nautilus-Perps/.venv/Scripts/python.exe (current R5 release wheel, 2.0.0rc4). Point scripts/imports explicitly at YOUR app worktree so tests do not silently use main checkout source. Record installed wheel identity/hash and source hashes.

## Safety boundary

Only public unauthenticated data network requests are allowed, plus local/offline tests. No sandbox/private account requests, remote orders/cancels/DMS, real credentials or funds. Do not read .env, auth files, other sessions or unrelated private data.

The installed python-dotenv supports PYTHON_DOTENV_DISABLED=1 (verified in site-packages/dotenv/main.py). Set it before importing any app scripts, including spread_watch, so find_dotenv cannot walk up to the main checkout's .env. For the observation child process, remove venue credential environment variables by NAME without printing their values. Do not mutate global settings. The public data path must register no execution factory and load no trading credentials.

## Deliverable A: immutable historical replay

1. Freeze SHA256+size manifest of all historical inputs before running. Record original report identity, current code SHA plus dirty file hashes and wheel identity. Recheck hashes after; zero original modifications.
2. Replay the old ONDO-HL L2 tape with current src/analysis/ondo_depth.py into a NEW output directory. Use NVDA,TSLA; notionals 100,500,1000; max-age-ms 2000; max-skew-ms 500; no invented increments or fee overrides. Invoke --help if necessary. Preserve fees at event/arrival time, market HALT, first-frame recovery, unknown quantity step, local-vs-event age, and drop/gap/session completeness semantics.
3. Compare current results with old saved analysis, separating changes caused by fee versions/HALT/unknown step/delay/completeness rules. Quantify what actual historical data contains vs missing. Do not imply a historical sample exercised a scenario it does not contain. For missing phenomena, point to meaningful targeted regression tests, or add acceptance fixtures explicitly marked synthetic with real analyzer execution. Missing data stays unknown/incomplete, never zero.
4. Write superseded-report references in the NEW report/index; preserve original reports byte-for-byte. Explain current notional outputs/rejection reasons and contract-mapping executable status; no profitability or real fill claim.

## Deliverable B: finite public observation

1. Run fresh public preflight for NVDA,TSLA and preserve complete/failure status. No .env loading.
2. Run a single bounded 5-minute ONDO-HL NVDA/TSLA watcher using current source and R5 data adapter: --venues ONDO,HL --symbols NVDA,TSLA --minutes 5 --max-restarts 0 --record-l2 --reference none --out <NEW public-observation directory>. Check CLI validity. The run is public-only. There is a hard external process timeout of 420 seconds; target only your spawned child if it hangs, preserve incomplete evidence, diagnose rather than silently treating termination as normal completion. Never kill unrelated python/node processes.
3. Preserve raw public frames, L2 tape manifests/drop/gap stats, instrument metadata, starts/stops, elapsed time and process exit. Replay new tape into a separate output and evaluate it with the same data quality criteria. Today is Saturday; low/no trade activity is an observation, not assumed pass or failure. Confirm actual subscription/update counts, both venue legs and symbols; startup/exit alone is insufficient. No indefinite retry or claim about 24-72h reliability.
4. If the existing watcher cannot stop safely, capture evidence first and fix the specific issue with a regression, then perform one bounded replacement observation explicitly linked to the failed attempt. Do not hide failed runs.

## Validation and handoff

- Run app data tests: test_market_tape.py, test_ondo_depth.py, test_ondo_preflight.py, test_spread_watch.py, test_opportunities.py; preserve exact command/output/exit and any warnings. Do not weaken tests to pass.
- Produce data/README.md and a machine-readable summary with input immutability, source/runtime identity, historical replay comparisons, public observation completeness per symbol and venue, gaps/unknowns, exact artifacts and unverified items. Distinguish implemented/offline_verified/public_observed. sandbox_auth_verified and sandbox_execution_verified must remain false.
- Return a concise Chinese result. If an actual limitation prevents acceptance, explain the evidence and actionable remaining work instead of claiming completion.

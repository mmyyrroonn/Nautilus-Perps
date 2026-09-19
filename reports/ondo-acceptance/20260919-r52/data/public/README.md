# Finite public ONDO/HL observation — 2026-09-19 (Saturday)

Public, unauthenticated only. No `.env` (every process ran with `PYTHON_DOTENV_DISABLED=1`), and
venue credential environment variables were removed by name before the watcher child started.
No private/sandbox request, no order, no cancel.

- `preflight/` — `ondo_preflight.py --symbols NVDA,TSLA`, exit 0, `complete=true`, `missing=[]`,
  `NVDA→NVDA-USD-PERP.ONDO`, `TSLA→TSLA-USD-PERP.ONDO`.
- `observation/` — one bounded run: `spread_watch.py --venues ONDO,HL --symbols NVDA,TSLA
  --minutes 5 --max-restarts 0 --record-l2 --reference none`. Exit 0 in 303.898 s
  (`../observation_run_meta.json`). `l2/` holds the tapes and manifests; `raw_ondo/raw_md.jsonl`
  holds the adapter's public frames.
- `observation_tape_content.json` — record kinds/venues/sessions/increments per tape.
- `replay/` — `ondo_depth.py` over the new tape; `evaluation.json` — per-symbol/venue completeness,
  L2 drop/gap verdict, replay summary and the explicit `sandbox_*_verified: false` markers.
- `observation_stdout.txt`, `preflight_stdout.txt`, `replay_stdout.txt` — raw logs.

Saturday context: low/no trade activity is an observation, not a pass or a failure. Both venue legs
and both symbols produced real subscription/update counts, which is what makes the run a genuine
observation rather than a startup-only event. No 24–72 h reliability is claimed.

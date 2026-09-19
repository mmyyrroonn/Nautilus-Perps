# Historical replay — current analyzer over the immutable P2 ONDO–HL tape

Generated 2026-09-19 by `src/analysis/ondo_depth.py` from the worktree
`E:/Nautilus-Perps/.worktrees/ondo-r52-app` (HEAD `aef07e4`), with
`PYTHON_DOTENV_DISABLED=1` and `PYTHONPATH=<worktree>/src`.

Input tape (read only, never modified):
`E:/Nautilus-Perps/reports/ondo-acceptance/20260915T024236Z-p2/l2/`
SHA256 manifest of the whole input tree before and after this run:
`../../provenance/input_manifest_before.json` / `input_manifest_after.json`
(tree `7f0b4855a46318f1abff4f3f690c3f1853ee21a37970b02f23cc30dfbc1c24d5`, byte-equal).

Command:

```
python src/analysis/ondo_depth.py --dir <p2> --symbols NVDA,TSLA --venues ONDO,HL \
  --notionals 100,500,1000 --max-age-ms 2000 --max-skew-ms 500 --out historical/replay
```

Exit code 0. Files in this directory: `ondo_depth.json`, `ondo_depth.md`,
`ondo_depth_summary.csv`, `ondo_depth_hits.csv`.

## Superseded

- `reports/ondo-acceptance/20260915T024236Z-p2/depth-analysis-ondo-hl/` is the **superseded**
  ONDO–HL analysis of this same tape. It is preserved byte-for-byte; do not cite its `pass`
  counts or its median bps without the step-rule correction below.
- `reports/ondo-acceptance/20260915T024236Z-p2/depth-analysis/` is an ONDO–**ASTER** analysis of
  the same run directory and is **not** superseded or replayed here: ONDO–HL never substitutes for
  ONDO–ASTER.

## Correction to the superseded numbers

The old report's `pass` rows and medians were produced while `10**-size_precision` was accepted as
a quantity step. The P2 tape carries **no `size_increment`**, so the current analyzer rejects every
quality-pass moment with `quantity_step_unknown` and withholds the priced numbers. The comparison
in `../comparison/old_vs_new.json` shows the shift is exact and that every non-step reject counter
is unchanged. The old report's medians are therefore **not** a current result and **not** evidence
of a tradeable spread.

## Generated-markdown boilerplate that does not apply

`ondo_depth.md` in this directory is rendered by `ondo_depth.py` and still contains generic text
from the P2/ONDO–ASTER era. It is not a statement about this ONDO–HL run and is intentionally left
unedited (no renderer refactor was in scope for this correction):

- `"No hit ... Zero opportunities is a result, not a missing section."` — inapplicable: zero hits
  here is not a verified zero-opportunity market. Every quality-pass moment was withheld with
  `quantity_step_unknown`, so the historical economic output is **unassessable**.
- The closing `"What P2 acceptance still needs"` items name `ONDO,ASTER`; this replay is
  **ONDO–HL**, and the live evidence for that pair is in `../../public/observation/`.

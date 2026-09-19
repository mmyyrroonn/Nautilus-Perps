# R5.2 data report review corrections

Continue your existing data task/session. Astra independently reran the historical analyzer and both generated CSV files are byte-identical to your output. The public observation completed successfully and direct tape counts agree with your per-symbol/venue update table. Keep all source tapes and observation outputs unchanged. No new observation is needed.

Before final acceptance, correct these report issues:

1. README Current historical notional outputs says `stale_book NVDA 1233 / TSLA 1008 per notional×direction` etc. The actual per-bucket CSV values are 411/336, 207/196, 63/62; your listed 3x numbers are aggregates across three notionals for each direction. Verify every stated counter and state the denominator exactly, preferably show per-bucket counts directly. Do not edit numerical CSVs to match prose.
2. The generated historical analyzer markdown contains old generic wording `Zero opportunities is a result, not a missing section` despite every priced row being withheld for unknown quantity step; it also ends with generic ONDO–ASTER/P2 required-observation wording even for this ONDO–HL run. For this bounded correction, add an explicit limitation/interpretation note in data/README.md and summary.json identifying these renderer boilerplate statements as inapplicable. Historical economic output is unassessable due missing increments, not a verified zero-opportunity market. The actual public observation evidence and pair are ONDO-HL. Do not undertake a renderer/source refactor in this correction.
3. Fix artifact filename shorthand to actual `_summary.csv` / `_hits.csv` names. Update progress to completed only for this data scope; sandbox still false.

Preserve all raw evidence and initial run records. Verify corrected prose against the machine artifacts and return the exact changed report paths. Only reports/helpers in your data directory may be edited. No app/fork source edits, builds, orders or commits.

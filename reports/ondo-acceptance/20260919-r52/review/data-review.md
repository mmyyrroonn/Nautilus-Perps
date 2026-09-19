# Astra independent R5.2 data review

Result: accepted for the requested historical replay and finite public DATA scope. Sandbox authentication/execution remain unverified. No source dataset was changed.

- Independently reran the current worktree analyzer with the same immutable P2 input, NVDA/TSLA, ONDO/HL, notionals 100/500/1000, receive-age 2000 ms and skew 500 ms. Exit 0. Output: `historical-independent/`.
- Compared output bytes with Pi's historical replay: `ondo_depth_summary.csv` SHA256 `f73a311639f4794d0166471dbf2c6adc603605329cbc8e2ed8971d5ea516946a`; `ondo_depth_hits.csv` SHA256 `2ef6dc74a07cca45038e64e6f22e7bd47b57244be76c3a20516e8e8e9a0fe2a2`. Both identical.
- Recomputed every original input hash against Astra's independently frozen manifest: 25 original files, 25 current files, 0 changed (`historical-immutability.json`).
- Independently reran five data test modules from the app worktree using the canonical app interpreter and disabled dotenv: **244 passed in 2.04 s**, exit 0 (`data-tests.txt`).
- Checked public observation metadata: 303.898 s, exit 0. Directly counted book and quote records per tape/venue; they match the report. NVDA quotes ONDO186/HL119, TSLA ONDO46/HL104; each tape has ONDO1154/HL56 book records. L2 and raw recording final records report complete, dropped0/gaps0.
- Required correction of the report's initial 3x aggregated reject counts to per-bucket values. Corrected table now sums exactly to samples. Required explicit qualifications for obsolete analyzer renderer boilerplate. Generated/raw artifacts were preserved.

Interpretation: old data cannot establish economic opportunities because true size increments are absent; quantities and priced statistics are unknown. The new public tape has actual increments, but all results remain nominal comparisons with mapping unverified, exit unclosed, and executable=false. The five-minute observation does not establish long-duration reliability. Synthetic scenario checks remain explicitly separate from observed history.

Data acceptance used the installed R5 release data adapter. The shutdown source fix and its replacement wheel are a separate acceptance gate.

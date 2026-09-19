# SYNTHETIC fixture: legacy-no-size-increment

This tape is generated, not recorded. It exists only to exercise an analyzer rule the historical 20260915T024236Z-p2 tape does not contain.

Expected behaviour: quantity_step_unknown withholds every priced row; 10**-size_precision is not used as a step.

Built by scripts/make_synthetic_fixtures.py in the R5.2 data directory; replayed with the current src/analysis/ondo_depth.py.

# SYNTHETIC fixture: first-frame-recovery

This tape is generated, not recorded. It exists only to exercise an analyzer rule the historical 20260915T024236Z-p2 tape does not contain.

Expected behaviour: one recovery frame is usable, keeps its own receipt and venue times, and a later quote refreshes neither.

Built by scripts/make_synthetic_fixtures.py in the R5.2 data directory; replayed with the current src/analysis/ondo_depth.py.

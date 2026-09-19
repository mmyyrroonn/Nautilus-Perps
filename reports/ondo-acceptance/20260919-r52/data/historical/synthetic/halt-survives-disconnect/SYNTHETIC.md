# SYNTHETIC fixture: halt-survives-disconnect

This tape is generated, not recorded. It exists only to exercise an analyzer rule the historical 20260915T024236Z-p2 tape does not contain.

Expected behaviour: market_halted and disconnected reject moments separately; a pass before the halt; snapshot_ready never lifts the halt.

Built by scripts/make_synthetic_fixtures.py in the R5.2 data directory; replayed with the current src/analysis/ondo_depth.py.

# SYNTHETIC fixture: metadata-fee-update

This tape is generated, not recorded. It exists only to exercise an analyzer rule the historical 20260915T024236Z-p2 tape does not contain.

Expected behaviour: seq4 costs 3.4 bps, seq6/seq7 cost 100.9 bps; the update never reprices earlier arrivals.

Built by scripts/make_synthetic_fixtures.py in the R5.2 data directory; replayed with the current src/analysis/ondo_depth.py.

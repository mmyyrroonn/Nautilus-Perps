# Final v6 loader and mainnet-report review

Date: 2026-09-20 Asia/Shanghai.

## Verdict

**PASS.** No blocker was found in the bounded real-loader fix or in the classification of the
recorded mainnet attempt. This is acceptance of the loader/reporting scope only. It is not evidence
of a successful private session, DMS operation, order, fill, close, fee result, or profit.

This review made no source change, ran no test or build, used no network, and accessed no credential.

## Loader fix

`src/ondo_probe.py` keeps `OndoExecutionEnvelopeConfig` optional on the shared `OndoAdapter` holder
and loads it with `getattr(..., None)`. Older wheels can therefore still load for the read-only
probe even when they do not expose the candidate-only envelope class. The production-trade probe
continues to require a callable envelope class and the exact native capability contract, so absence
fails closed rather than weakening the write gate.

`test_real_loader_carries_the_installed_trade_envelope` is meaningful candidate integration
coverage: it calls the real `load_adapter()` instead of a fake adapter, requires the installed
envelope class to be callable, and requires capability detection to return supported from the
native contract. The recorded final application log
`build/trade-v6-loader-app-tests.txt` contains **493 passed, zero failed**. The actual run report also
records `native_trade_capability.supported=true` with source `native-trade-contract`, confirming the
installed v6 surface was resolved for that run.

## Mainnet evidence classification

The controlled evidence is internally consistent. `meta.json` records the exact byte count and
SHA256 of `trade.json`; both independently match (9,236 bytes and
`a1736c333d67a93993caa210f264bc4db45d3ceb6fba93329655e3fdf0ed8ebf`).

The supported classification is **No Trade / `not_started`**:

- the process exited 1 on engine readiness timeout before a native start snapshot;
- the strategy submitted zero orders, with no entry or close object and no fills;
- the local sequencer reports net position `0`, while whole-account flatness remains unknown because
  no account-state event or recovery completed;
- the REST/native diagnostic identity result is matched, but the full start acceptance identity
  gate is false because readiness never completed;
- private login is false, subscription acknowledgements are empty, and account-state events and
  recoveries are zero;
- DMS activation and release are only **not observed**: the stop fields remain null, so the report
  does not infer that either action occurred;
- production execution is unverified, fees are unknown, and no economic claim is made.

`trade.json` has `accounting.no_trade=false`, which is consistent with this wording: that bucket is
reserved for a venue-established rejection, while this attempt never reached submission. The
session-level outcome remains `not_started`, so the human-facing `No Trade` label means no trade
occurred, not that the venue rejected one.

`20260920-ondo-final-status.md` preserves these boundaries and does not overclaim. Its statement
that account identity matched is explicitly the REST identity observation; it separately says
whole-account flatness is unverified. Its DMS wording is observational, and it correctly leaves
real order/fill/close behavior and economic outcome unverified.

## Residual limitation

The blocker is operational, not a defect in this reviewed loader/report scope: a future attempt must
first resolve private/engine readiness and run a new fully priced plan during underlying-market
hours. The present evidence cannot be promoted to live execution acceptance.

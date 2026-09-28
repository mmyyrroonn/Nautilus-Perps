# Ondo application/native interface (current)

Version: application contract 1, reviewed 2026-09-28. Owner: Nautilus-Perps #4 and #6; native implementation: nautilus_trader #10. The historical [2026-09-19 requirements](../reports/ondo-acceptance/20260919-production-native/trade-app/required-native-interface.md) stated that the producer was absent **at that date**. This document describes the current source contract; runtime support still depends on the installed wheel.

## Admission

`src/ondo_trade_probe.py` requires `OndoExecutionEnvelopeConfig`, the exact boolean `OndoExecutionClientFactory.supports_production_trade_envelope`, and `production_trade_snapshot()`. The config must accept `execution_envelope`, `expected_venue_account_id`, `diagnostics_run_id`, `dms_timeout_secs`, `reconcile_interval_secs`, and `allow_production_orders`. The app rejects missing or mismatched fields before building a production client. A marker alone is insufficient.

The envelope carries exact Decimal quantities/prices/notional bounds, instrument and sides, entry and absolute cleanup deadlines, request counts and a flat-start requirement. Its source of truth is `NATIVE_REQUIRED_CONFIG_FIELDS` and `ENVELOPE_REQUIRED_FIELDS` in [ondo_trade_probe.py](../src/ondo_trade_probe.py); the implementation is [production.rs](https://github.com/mmyyrroonn/nautilus_trader/blob/3831bda220692664df925c750507285b7424608b/crates/adapters/ondo/src/production.rs). The app passes the planned DMS timeout to the native config, and a 30-second outer disconnection allowance. The native production stop uses at most 15 seconds and cannot extend the approved cleanup deadline.

## Evidence

`production_trade_snapshot()` is tied to the app run token and contains native start, reconciliation and final account evidence. Only a clean **final** snapshot may certify a writing run; a reconciled or absent final snapshot cannot be promoted to clean. The app keeps entry fill, close fill, local flat, account flat, clean shutdown and economic result as separate claims.

`production_shutdown_diagnostics()` was added in native [3831bda](https://github.com/mmyyrroonn/nautilus_trader/commit/3831bda220692664df925c750507285b7424608b). It exposes fixed release states, frame classes and bounded stage timestamps even when account proof is missing. The application copies only explicit allowlisted fields into `shutdown_diagnostics`; no raw private frame is published. This accessor has no run token, so the application reads it only from the fresh factory created for this process's single run. Diagnostics observe the transport; they do not certify a venue ACK, account state or clean shutdown.

`dms_release` remains the historical run-token-checked snapshot projection. A sent release frame is not an acknowledgement. An unknown frame stays unclassified; zero classified DMS updates does not establish that the socket received no frames.

## Acceptance and change control

Before promoting a new candidate, record native/app SHA, dirty source hashes, wheel/binary/stub SHA-256, ABI/platform, installed import path, build profile/features/toolchain and test output. Run native offline tests and the application installed-wheel LiveNode test against one candidate. Live venue claims need fresh per-run authorization and direct observations. See [acceptance-status.md](acceptance-status.md) and [test-coverage.md](test-coverage.md).

# Default-off production-trade candidate acceptance

Date: 2026-09-20 Asia/Shanghai.

## Result

The restricted native implementation and app integration are **offline verified**. After the user
explicitly authorized mainnet testing, the production capability marker was changed to `true`,
independently reviewed, rebuilt and verified. The marker advertises the bounded implementation; it
does not itself opt into orders.

This is not production account, DMS, order, fill, close or economic acceptance.

## Frozen native evidence

- Native frozen source: `native-trade/source-sha256-v5.json`, 63/63 hashes independently matched.
- Complete native suite: 1,031 passed, 0 failed (`native-trade/full-native-python-v5.txt`).
- Format and scoped diff checks: exit 0.
- Independent review: N1-N6 resolved (`review/native-trade-review.md`).
- Actual mock HTTP/WS/native-command coverage includes one entry/two close limits, concurrent
  reservations, unknown original-id recovery, journal failure, post-budget expiry/stale quote,
  whole-account startup, owned reconnect residual, genuine DMS send-time/ACK deadlines, acknowledged
  release, final REST contradiction, metadata disabled/closed state and the 25 USDC margin floor.

## Candidate artifact and installed identity

- Wheel: `dist-trade-v6/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`.
- Wheel SHA256: `2cea63a07b5969ecf11558bc28a5021f4f89dd609f9ebe29d11b9278ef384db3`.
- Installed binary SHA256: `2bc8a20baba7346c2dd37064af3a6eb6e60e93f7f661a4e37ac683436aca0e75`.
- Generated/installed Ondo stub SHA256:
  `9251ed9a23adeb8f16f5cc7766f3c9dc2a2404bb0ec84a435edaa2cd46aa3972`.
- The wheel, installed binary, generated stubs and 49 build-source files were matched by
  `verify-trade-candidate.py`.
- Real installed `OndoExecutionEnvelopeConfig` and `OndoExecutionClientConfig` construction passed
  with the final exact fields, including `min_available_margin_usdc="25"`. Factory snapshots were
  initially absent, as required. No credential or network was used.
- Build limitation: the release build reports a local `C:\ProgramData\miniconda3\zlib.dll`
  dependency. Local import and tests pass; portability to a machine without that DLL is unverified.

## App evidence

- Final candidate app suite after the real-loader correction: 493 passed, 0 failed
  (`trade-v6-loader-app-tests.txt`).
- Safe dry-run: exit 0; `client_constructed=false`, `env_file_read=false`, `requests_sent=0`,
  `limits_configured=true`, `live_execution_ready=false`, `native_write_capable=false`, and
  `production_execution_verified=false`.
- USD order rails and the 25 USDC available-margin precondition remain separate; no USD/USDC parity
  or FX conversion is claimed.

## Runtime gate

`OndoExecutionClientFactory.supports_production_trade_envelope` is `true` in the installed v6
candidate. `allow_production_orders` separately remains default `false`; a run still requires
explicit opt-in, the complete immutable envelope, account identity, journal, whole-account/DMS
readiness and current market evidence.

Actual account connection or execution remains separately gated by the project phrase `上主网` and
a current, fully priced order plan. Neither occurred here.

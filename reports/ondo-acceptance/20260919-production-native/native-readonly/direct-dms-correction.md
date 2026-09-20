# Read-only direct and unsolicited DMS correction

Result: implemented and offline verified. Production trading remains disabled. Source/cache ownership released after the checks below.

## Reproduced defects

Four regression cases failed before the fix, for both Sandbox and Production readonly sessions:

- Direct `OndoPrivateStream::send_switch_frame` accepted arm, renewal and release frames. All three calls returned `Ok(())` on the loopback connection.
- An unsolicited `subscribed(cancelAllOrdersAfterPerps)` acknowledgement armed local DMS state, and the subsequent one-second renewal tick emitted an account-changing DMS frame.

Evidence: `logs/direct-dms-red.txt`. This is an actual mock WS wire observation, not a source-only hypothesis. All credentials were fixed synthetic fixture strings; both servers were literal loopback endpoints.

## Changes

- `src/websocket/private/stream.rs`: retain immutable stream mode; reject direct switch sends before serialization/socket access; gate DMS action construction, renewal and confirmation by mode; refuse attempting a Trading stream over a readonly account runtime.
- `src/websocket/private/session.rs`: reject subscription acknowledgements for channels outside the requested mode and acknowledgements arriving before the subscribing/subscribed phase. Such frames confirm nothing.
- `src/execution.rs`: readonly runtime arm helpers compose no state transition; readonly confirmation and renewal mutators cannot arm/prolong protection; readonly renewal frame is absent. The existing frame-returning arm signature is retained, while the actual transport refuses its send.
- `src/reconciliation.rs`: a confirmation cannot turn a switch that was never required into Armed.
- `tests/private_runtime.rs`: six new parametrized cases across Sandbox/Production prove direct arm/renew/release rejection, zero DMS wire frames after unsolicited ACK crossing the renewal tick, no REST writes, readonly runtime state immutability and rejection before connection of a Trading-mode upgrade.
- `tests/reconciliation.rs` and session inline tests: unrequested DMS confirmation, unrequested channel ACK and pre-login ACK regressions.

No trade envelope, ProductionTrading scope, production capability marker, new endpoint, credential read or live operation was added. Existing readonly snapshot keys remain unchanged.

## Verification

Commands use CARGO_TARGET_DIR=E:/nautilus_trader/target, PYO3_PYTHON=E:/nautilus_trader/.venv/Scripts/python.exe, CC=clang, CXX=clang++, per-process CARGO_BUILD_WARNINGS=allow. This preserves the existing Windows linker-warning exception; it is not a strict-warning pass.

| Command | Actual result | Evidence |
|---|---|---|
| `cargo test -p nautilus-ondo --features python --locked --offline --test private_runtime test_readonly_ -- --test-threads=1` before fix | 1 passed, 4 failed | `logs/direct-dms-red.txt` |
| Same command after fix | 5 passed, 0 failed | `logs/direct-dms-green.txt` |
| `cargo test -p nautilus-ondo --features python --locked --offline` after added boundary tests | Exit 0, **955 passed**, 0 failed | `logs/direct-dms-full.txt` |
| `cargo fmt -p nautilus-ondo -- --check` | Exit 0 | `logs/direct-dms-fmt-check.txt` |
| `git -c safe.directory=E:/nautilus_trader/.worktrees/ondo-production-native diff --check -- crates/adapters/ondo` | Exit 0 | `logs/direct-dms-diff-check.txt` |

Full counts: lib 467; execution 77; HTTP client 44; HTTP contract 77; market data 43; private runtime 56; Python 8; reconciliation 119; signing 63; doc tests 1. The full suite retains sandbox arm/renew/release, account recovery and shutdown coverage.

Exactly six files differ from the N2 readonly manifest; `direct-dms-changed-paths.json` lists them. `direct-dms-source-sha256.json` records all 60 current source hashes. No commit, build, stub generation, installation, external account access or production operation was performed. Independent review/packaging/live readonly acceptance remain parent-owned.

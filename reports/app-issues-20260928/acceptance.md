# Application issues #2/#3/#4/#6: local candidate check, 2026-09-28

This is a local candidate acceptance record: offline build/tests plus one bounded production read-only session. No DMS operation or live order was used, and no credential was published. The application project's formal `pyproject.toml` wheel binding and existing `.venv` were not replaced.

## Identity

- Native checkout: `3831bda220692664df925c750507285b7424608b`; native tree `139ffd9eb0949a54850253d61bd050cffbc57cc6`; Cargo.lock SHA-256 `53bc4722129e781288afe62478dd5170fb56b6ce2627230199ed49254535a156`.
- Native checkout had only pre-existing `BUILD_WINDOWS.md` and `docs/plans/` dirty entries. The native provenance inventory records their diff/content hashes. The build wrapper did not yet bind the pre-build source fingerprint to the artifact; `source_binding=unknown` and strict promotion was refused.
- Application base: `aff9530799e70423207bb9e4f5b14e8dd274fba1`, with local application edits. The identity JSON in `nautilus_trader/target/app-issues-20260928/installed-identity.json` records its then-current tracked diff SHA-256. That local target is an ignored test artifact, not a portable download.
- Wheel: CPython 3.12, Windows amd64, nextest profile, SHA-256 `637fce2d8603eacdaacb5eedbb82cf74effc03a0767b5b57cf0ca7c07f94f894`.
- Embedded and imported native binary SHA-256: `53a78bd6fe27139711447a8fe5ba6445a42f0c8d3346a2a0e80c0ec11ea4fbc8`.
- Isolated interpreter: uv-managed CPython 3.12.9 under `nautilus_trader/target/app-issues-20260928/app-venv`. The verifier checked direct wheel origin, installed import location, binary bytes, ABI tag and Ondo production/shutdown diagnostic capability.

## Checks run

| Check | Result |
| --- | --- |
| Native wheel build from current native checkout, `maturin build --profile nextest --offline --locked` | Passed; local build log retained under ignored native target |
| Application tests on that installed wheel, `python -m pytest tests -q -p no:cacheprovider` | 1185 passed, 97 subtests passed, no warnings after renaming a helper that pytest previously collected as a test |
| App Ondo subset (`test_ondo_dms_diagnostics`, `test_ondo_trade_probe`, `test_ondo_btc_operator`) | 237 passed |
| `scripts/verify_native_install.py` with correct wheel hash and `--require-ondo` | Passed; imported binary equals embedded wheel binary |
| Wrong wheel SHA-256 | Refused with exit 2; no success record |
| `--require-source-binding` against the native inventory marked unknown | Refused with exit 2; no success record |
| Native import with PATH restricted to Windows system directories (no Conda path) | Passed on this Windows host |
| Acceptance document link/identity check | Passed |

The first run collected 1186 items and emitted a return-value warning because `test_maker_live.py::test_limits` was a data factory named like a test. Renaming it to `make_test_limits` removed that false test; the final run collected 1185 real tests with no warnings. The application test run does **not** replace a real installed-wheel LiveNode + local simulated venue scenario, and Windows import on this host does not prove Linux or clean Windows portability. Native #10's [offline shutdown tests](https://github.com/mmyyrroonn/nautilus_trader/blob/3831bda220692664df925c750507285b7424608b/crates/adapters/ondo/reports/dms-shutdown-20260928.md) cover missing, late, malformed and wrong-channel ACKs. They do not prove the private venue's actual ACK contract.

## Bounded production read-only observation

The installed local wheel ran `src/ondo_probe.py --mode production-readonly --symbols BTC --minutes 0.5 --log-level WARNING` on 2026-09-28. Run `20260928T030916000729Z` lasted 40.617 seconds including setup and shutdown. It exited 0 at its normal deadline. Native diagnostics reported login true, two private subscriptions acknowledged, two account-state events, identity matched and shutdown complete. The probe submitted zero orders and `dms_armed=false`. `production_readonly_support_verified=true`; the separate protocol/DMS predicate remained false. This was one observation on the current wheel, not production-write or DMS-release acceptance.

The raw sanitized `probe.json` and `meta.json` remain only under ignored `nautilus_trader/target/app-issues-20260928/production-readonly/`. Their SHA-256 values are `59a9e35948d9765fc9d6d1c3bfbc60e7376f2d6b590b5cd6a1df67335271dcb3` and `1cc29922cc522e1f4abcbc8999fb4c83d7cf43affb44d169a6ec613fcc135178`, respectively. A post-run scan of five report/log files found zero occurrences of the configured API key, secret or account id. No raw private frame was copied into this report.

## Ondo shutdown and economics

The 2026-09-23 historical BTC entry, reduce-only close and flat checks remain valid for the older wheel. Its release was sent but unacknowledged; exit 1 meant incomplete shutdown. The recorded plan allowed 120 seconds overall, reserved 15 seconds for cleanup and configured a 30-second DMS timeout. The app builder passes a 30-second outer disconnect allowance; native stop uses at most 15 seconds or the remaining absolute cleanup time, whichever is shorter. The historic run lasted 68.341 seconds from start to finish, but has no stage timestamps, so that duration cannot identify the release failure cause. The later native code now supplies safe frame classes and stage times, and the application report reads them through an allowlist. This check did not observe a new DMS release or production write session. The historical run still has unknown fees, funding and balance delta.

## Remaining gates

- Native #11 / application #2: controlled source-to-wheel build binding, portable artifact source with pinned SHA, locked dependency environment, clean Windows and Linux acceptance. The historical `file://` binding cannot be considered portable.
- Application #3: explicit two-ref CI, same-candidate installed-wheel LiveNode and loopback venue case, matrix and skip/count reporting.
- Application #6: venue DMS release semantics, same-candidate DMS venue observation, read-only interruption recovery and reliable economic ledger. Do not relax the full production predicate.

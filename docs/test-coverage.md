# Test coverage and joint acceptance map

Checked 2026-09-28. This maps existing assertions before adding tests. A test file name alone is not evidence of a native LiveNode lifecycle. Historic counts apply only to their recorded candidate.

| Scenario / owner issue | Native test evidence | Application test evidence | Proven layer | Remaining assertion |
| --- | --- | --- | --- | --- |
| Aster balance and free funds, native #2 | `crates/adapters/aster/tests/exec_client.rs`: zero balance, refresh failure; `crates/portfolio/tests/portfolio.rs` | `tests/test_exec_probe.py`, `tests/test_maker_live.py` | Native loopback and isolated installed-wheel LiveNode in [2026-09-28 report](https://github.com/mmyyrroonn/nautilus_trader/blob/9f80aa7b2c62b99af4af5fefa51c15356e031a43/docs/aster-account-position-acceptance-2026-09-28.md) | Formal app binding still points to an older wheel |
| Aster signing after rate wait, native #3 | Aster execution tests in PR #35 | `tests/test_exec_probe.py` | Native loopback | Same-candidate app workflow |
| Aster readiness gate, native #4 | Aster execution tests in PR #34 | `tests/test_live_limits.py`, `tests/test_maker_live.py` | Native loopback and app mocks | Same-candidate installed-wheel shutdown |
| Aster real-fill recovery, native #5 | `exec_client.rs` startup/compensation and repeated-fill tests | `tests/test_exec_probe.py` | Native loopback | Joint workflow on promoted candidate |
| Aster omitted Flat snapshot, native #6 | `exec_client.rs` complete/incomplete/reconnect/fill-debt cases | `tests/test_exec_probe.py` | Native loopback and isolated installed-wheel LiveNode in #37 report | Formal app binding |
| Ondo Prepared cancellation, native #7 | `crates/adapters/ondo/tests/private_runtime.rs` blocked/cancelled Prepared tests, PR #40 | `tests/test_ondo_trade_probe.py` TradeSequencer tests | Native barrier plus app mock, separate evidence | Installed-wheel lifecycle |
| Ondo cleanup minimum, native #8 | `private_runtime.rs` production cleanup refusal, PR #38 | `tests/test_ondo_trade_probe.py` limits and deadline tests | Native loopback plus app mock | Fresh candidate integration |
| Ondo reconciliation tail, native #9 | `private_runtime.rs` pass ownership and tail handoff, PR #39 | `tests/test_ondo_trade_probe.py` pre-stop reconciliation | Native barrier plus app mock, separate evidence | Installed-wheel lifecycle |
| Ondo DMS/shutdown, native #10 / app #6 | `private_runtime.rs` caller timeout, wrong/missing/late ACK, repeated stop; [#41 report](https://github.com/mmyyrroonn/nautilus_trader/blob/3831bda220692664df925c750507285b7424608b/crates/adapters/ondo/reports/dms-shutdown-20260928.md) | `tests/test_ondo_dms_diagnostics.py`, `tests/test_ondo_trade_probe.py`, `tests/test_ondo_btc_operator.py` | Native loopback and app mocks; historical live result on older wheel | Same-candidate installed-wheel LiveNode, private host ACK contract, economics |
| Ondo public/read-only/DMS side effects | `private_runtime.rs` read-only no-arm, login/subscription and reconnect | `tests/test_ondo_probe.py`, `tests/test_ondo_depth.py` | App mock plus historical authenticated read-only | A bounded authenticated read-only run on the isolated 2026-09-28 wheel passed; DMS release remains unobserved |
| Identity and portable install, app #2 / native #11 | `scripts/adapter-evidence/native_checks.py`, `wheel_provenance.py` | Historical `verify-candidate.py` | Source and wheel inventory | Controlled source-to-wheel binding, portable Windows/Linux installation |
| Cross-repo CI, app #3 | Native `nautilus-adapter-checks.yml` | Existing app pytest suite | 1185 tests + 97 subtests on isolated current wheel, [local check](../reports/app-issues-20260928/acceptance.md) | Explicit-ref two-repo workflow, installed wheel, machine-readable result |

Historic application candidate [2026-09-23 report](https://github.com/mmyyrroonn/Nautilus-Perps/blob/aff9530799e70423207bb9e4f5b14e8dd274fba1/reports/ondo-acceptance/20260923-freshness/acceptance.md): 1183 app tests + 97 subtests, 1073 native tests, 42 launcher tests. Native #41 records 1107 Ondo tests for a later SHA. The Aster #37 report records 421 Aster + 227 Portfolio tests and 385 app tests + 97 subtests. These counts are never combined into one green candidate.

## Command layers

- App mock / isolated Python: `<app-interpreter> -m pytest tests/test_ondo_dms_diagnostics.py tests/test_ondo_trade_probe.py tests/test_ondo_probe.py tests/test_ondo_btc_operator.py -q -p no:cacheprovider`.
- Native Ondo: from the fork, `cargo nextest run -p nautilus-ondo --profile ci --retries 3 --no-fail-fast` and `cargo test --doc -p nautilus-ondo`.
- Native Aster: use `scripts/adapter-evidence/native_checks.py`, which records fmt, nextest, doctest and Clippy with source identity.
- Installed-wheel LiveNode: requires the *same* wheel identified by app #2, a fresh interpreter and a local simulated venue. A mock factory cannot satisfy this layer.
- Venue acceptance: public, authenticated read-only, private DMS and live write are distinct levels. Networked private and live tests are isolated from default CI.

The joint workflow must take full native/app refs as inputs, record resolved SHAs, build on the native side, verify wheel and installed module hashes, then run app tests against that exact wheel. It must make skips and test-count changes visible. No workflow result yet satisfies that contract.

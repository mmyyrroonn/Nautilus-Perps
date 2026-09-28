# Test coverage and joint acceptance map

Checked 2026-09-28 (Asia/Shanghai). This is an evidence map for application issue #3 and the
native issues it consumes. It records which layer each assertion reaches; a test filename, a
passing unit suite, or an installed package version by itself is not evidence of a native
`LiveNode` lifecycle.

## Candidate identity and result boundaries

The current formal candidate is the source-bound native release
[`native-candidate-eeeb8eefc7`](https://github.com/mmyyrroonn/nautilus_trader/releases/tag/native-candidate-eeeb8eefc7),
native commit `eeeb8eefc7921b7f81cb611cb3c851c665cd1ff1`, tree
`348ec51bb837297ee4b706c7cc9dcbab6a9621f9`, pinned by
[`config/native-candidate.lock.json`](../config/native-candidate.lock.json). Its platform assets
are Windows `802e8eead8ca24ca52324eade773b20ea9dd5f9c8701ccfd628c32bca42a0783` and Linux
`c8a09a3e9c04e735a8b3177fe2d85925afaf56ecaa2658089a6834c66c4dfa73`. Both published
provenance records report a clean source and verified source-to-wheel binding.

The [Issue 2 promotion record](../reports/issue2-20260928-promotion.md) records the hosted
Windows/Linux install and offline application checks against that published candidate: 1188
application tests and 97 subtests on each final portable run, with one existing pytest warning.
Those records prove candidate installation and the offline suite. They do not contain the
installed-wheel `LiveNode` + local venue test required by issue #3.

The following artifacts remain separate and must not be combined with the formal candidate:

| Artifact | What it proves | What it does not prove |
| --- | --- | --- |
| Local [application issue report](https://github.com/mmyyrroonn/Nautilus-Perps/blob/b66e83279d4440843217f2ce390b79060ce9877b/reports/app-issues-20260928/acceptance.md), native `3831bda220692664df925c750507285b7424608b`, wheel `637fce2d8603eacdaacb5eedbb82cf74effc03a0767b5b57cf0ca7c07f94f894`, binary `53a78bd6fe27139711447a8fe5ba6445a42f0c8d3346a2a0e80c0ec11ea4fbc8` | 1185 tests and 97 subtests on an isolated local wheel; one bounded authenticated read-only observation | The provenance was `source_binding=unknown`; this is not the locked published candidate and has no same-candidate `LiveNode` result |
| Historical Aster [account/position report](https://github.com/mmyyrroonn/nautilus_trader/blob/9f80aa7b2c62b99af4af5fefa51c15356e031a43/docs/aster-account-position-acceptance-2026-09-28.md), native `9f80aa7b2c62b99af4af5fefa51c15356e031a43` | 421 Aster tests, 227 Portfolio tests, and an installed wheel through a scripted loopback `LiveNode` | The wheel and application identity predate the formal `eeeb8eefc7` candidate |
| Historical Ondo BTC candidate, application `aff9530799e70423207bb9e4f5b14e8dd274fba1`, wheel `8f20735b299ef204464d8a104ca9fdbcec92a5b20d1a5795f474818d56e12985` | Historical entry, reduce-only close, flat checks, and a DMS release attempt | It was an older wheel; the release frame was sent without a confirmed ACK, exit was 1, and fees/funding/balance delta remained unknown |

The current formal lock is therefore a usable candidate identity, while the issue #3 workflow
result is still a separate required record. A local wheel built from a different native SHA,
even when its application tests pass, cannot fill that gap.

## Evidence levels

| Level | Test shape | It can establish | It cannot establish |
| --- | --- | --- | --- |
| Application mock / pure Python | `tests/` doubles, fake factories, `TradeSequencer`, config and report tests | Application guards, state transitions, report schema, limits, and refusal behavior | Native extension import, native task lifecycle, venue wire behavior |
| Native loopback | Rust adapter tests with loopback HTTP/WS and synthetic credentials | Native adapter state machines, recovery, barriers, signing order, and shutdown classification | A real venue's interpretation of a frame or a real wheel installed into the application |
| Installed wheel + local simulated venue | A fresh interpreter imports the candidate wheel; a real `LiveNode` talks to a local HTTP/WS server | Wheel origin/hash, Python bindings, native module, factory wiring, connect/fault/stop/dispose lifecycle | Private venue semantics, real account state, DMS release ACK, economics |
| Authenticated read-only venue | A bounded network session with credentials and zero writes | Observed login, subscriptions, identity and account events for that exact wheel/run | DMS release ACK, write behavior, fills, or economic reconciliation |
| Private DMS / live write | Isolated venue operation with explicit authorization and a fresh plan | Actual venue ACK/renewal and write observations when run and recorded | Nothing beyond the exact run; it is not supplied by default CI |

The new [`tests/test_installed_wheel_livenode.py`](../tests/test_installed_wheel_livenode.py)
belongs to the third level. It starts a real loopback Aster HTTP/WS server, checks the installed
wheel against the runner's verified wheel hash, builds the real Aster execution client and
`LiveNode`, observes a WS fault, and asserts stop/dispose, listen-key deletion, and zero order
requests. A direct pytest run defaults to the formal lock and skips a different wheel. In the
joint workflow the integration runner supplies its just-built wheel hash; a mismatch fails, and
any skip is visible and rejected by the zero-skip gate.

## Native issues #2–#10 and application coverage

The native rows below refer to the source and loopback reports included in the formal candidate's
ancestry. Report counts belong to the commit named by each report; they are not one aggregate
count for `eeeb8eefc7`.

| Native issue | Native assertions and source | Application assertions | Strongest evidence currently recorded | Remaining assertion for issue #3 |
| --- | --- | --- | --- | --- |
| #2 Aster available/free balance mapping | `crates/adapters/aster/tests/exec_client.rs`: explicit zero, failed verification, owed refresh and stale-generation cases; `crates/portfolio/tests/portfolio.rs`: reported balance remains distinct from local margin estimate | `tests/test_exec_probe.py`, `tests/test_maker_live.py` | Native loopback; historical installed-wheel account/position acceptance | Run the current locked wheel through the real `LiveNode` fixture and retain balance/readiness observations under the same app/native refs |
| #3 Aster signing after rate wait | Aster execution/HTTP tests from PR #35 exercise quota admission before signed submission | `tests/test_exec_probe.py` covers the application order path; the installed-wheel fixture exercises the real factory's signed control requests without an order | Native loopback | Same-candidate wheel lifecycle on both matrix platforms; no live signed order is required for this offline gate |
| #4 Aster readiness gate | `exec_client.rs` reconnect, incomplete snapshot, unverified mode, stream loss, cooldown and readiness-generation cases refuse new risk until evidence is complete | `tests/test_live_limits.py`, `tests/test_maker_live.py`, `tests/test_exec_probe.py` | Native loopback plus application mocks | The locked wheel must pass the same-candidate `LiveNode` lifecycle; a mock factory cannot substitute for this |
| #5 Aster real-fill recovery | `exec_client.rs` startup/compensation, ambiguous submit, repeated fill and downstream-consumption cases preserve venue trade IDs and commissions exactly once | `tests/test_exec_probe.py` terminal/recovery assertions and `tests/test_maker_live.py` fill/account replay cases | Native loopback; application mock recovery | No current formal-candidate installed-wheel fill workflow is recorded; the issue #3 fixture intentionally sends no order |
| #6 Aster omitted Flat snapshot | `exec_client.rs` complete/incomplete/reconnect/filled-debt cases infer Flat only from a complete valid snapshot and after real fill evidence is consumed | `tests/test_exec_probe.py`, `tests/test_maker_live.py` | Native loopback; historical Aster installed-wheel loopback report | Keep the current fixture's empty `positionRisk` and no-order lifecycle separate from a claim of venue Flat or economic reconciliation |
| #7 Ondo Prepared cancellation | `crates/adapters/ondo/tests/private_runtime.rs`; [Prepared cancellation report](https://github.com/mmyyrroonn/nautilus_trader/blob/eeeb8eefc7921b7f81cb611cb3c851c665cd1ff1/crates/adapters/ondo/reports/prepared-cancellation-20260928.md) covers cancellation before first poll, during shared-budget wait, and after HTTP send | `tests/test_ondo_trade_probe.py` exercises the application envelope, sequencer and refusal paths with doubles | Native loopback barrier plus application mock, kept as separate evidence | The native report proves cancellation/uncertainty semantics only. A same-candidate installed wheel has no real venue Prepared lifecycle result, and no venue write is implied |
| #8 Ondo cleanup minimum | `private_runtime.rs`; [cleanup minimum report](https://github.com/mmyyrroonn/nautilus_trader/blob/eeeb8eefc7921b7f81cb611cb3c851c665cd1ff1/crates/adapters/ondo/reports/cleanup-minimum-20260928.md) checks a legal close under the published minimum and both notional ceilings | `tests/test_ondo_trade_probe.py` limits, deadline, close and refusal assertions | Native loopback HTTP plus application mock | A legal close is not liquidity or a real cleanup; current candidate wheel integration and venue observations remain separate |
| #9 Ondo reconciliation tail | `private_runtime.rs`; [reconciliation tail report](https://github.com/mmyyrroonn/nautilus_trader/blob/eeeb8eefc7921b7f81cb611cb3c851c665cd1ff1/crates/adapters/ondo/reports/reconciliation-tail-20260928.md) holds account ownership from drain/replay through commit, rejects a changed generation, and records loss on drop | `tests/test_ondo_trade_probe.py` checks that final proof follows reconciliation and that later activity invalidates a clean result | Native reconciliation barrier plus application mock, separate evidence | This barrier is an offline native ownership/publication guarantee. It does not observe a private venue DMS ACK, and the current wheel fixture does not claim a live reconciliation result |
| #10 Ondo DMS/shutdown | `private_runtime.rs`; [DMS shutdown report](https://github.com/mmyyrroonn/nautilus_trader/blob/3831bda220692664df925c750507285b7424608b/crates/adapters/ondo/reports/dms-shutdown-20260928.md) covers missing, old, malformed, wrong-channel and delayed matching ACKs, budget expiry, checkpoint ordering, and repeated stop | `tests/test_ondo_dms_diagnostics.py`, `tests/test_ondo_trade_probe.py`, `tests/test_ondo_btc_operator.py` | Native synthetic frames plus application mocks; one bounded authenticated read-only observation on a local unpromoted wheel | Synthetic ACKs prove parser/state handling, not the private host's ACK contract. A read-only run's login/subscription ACKs, `dms_armed=false`, zero writes, or clean stop do not prove a DMS release ACK. No current formal-candidate DMS operation is recorded |

### #7 Prepared and #9 reconciliation are different barriers

For #7, `Prepared` is the native execution submission state before a request is irrevocably
sent. The three loopback cases distinguish cancellation before the first worker poll, during the
shared-budget wait, and after the POST is sent. In the third case exactly one POST is observed and
the order remains uncertain; a flat REST read cannot turn that uncertainty into a known zero. This
is evidence for native cancellation semantics, not a real venue write result.

For #9, the reconciliation transaction owns the account claim through buffer drain, replay,
publication and commit. A tail order/fill can arrive while the pass is paused without being
published behind the pass; the same generation can commit once, while a changed generation refuses
the stale publication and records lost reports. Dropping the transaction publishes nothing and
marks uncertainty. This is an ownership/publication barrier and is independent of whether a venue
later acknowledges a DMS release.

### DMS ACK boundary

Native #10's loopback server sends synthetic text frames to exercise the classification and state
machine. It intentionally does not assert what the private venue will send in production. The
2026-09-28 bounded read-only observation recorded a real login and two private subscription ACKs;
those are subscription observations, not a DMS release ACK. The historical 2026-09-23 write run
recorded a release attempt with `frame_sent=true` and `acknowledged=false` on an older wheel. No
claim about a current candidate's venue DMS release, renewal, or shutdown ACK follows from either
the offline tests or the read-only observation.

## Commands and layers

Run the native checks from `E:\persarb\nautilus_trader` with the exact native checkout used for
the candidate:

```text
python scripts/adapter-evidence/native_checks.py --crates nautilus-aster nautilus-portfolio --output target/aster-acceptance.json
cargo nextest run -p nautilus-ondo --profile ci --retries 3 --no-fail-fast
cargo test --doc -p nautilus-ondo
```

The issue-specific native reports contain their focused commands and counts. Native loopback
commands do not contact a venue.

Run application tests with the explicit CPython 3.12.9 interpreter for the installed wheel:

```text
<app-python> scripts/install_native.py --python <app-python> --lock config/native-candidate.lock.json --output .native-cache/installed.json
<app-python> scripts/test_integration.py --python <app-python> --wheel <verified-wheel> --provenance <verified-provenance> --sha256 <wheel-sha256> --config config/limits.toml --output .native-cache/integration.json tests
<app-python> -m pytest tests/test_installed_wheel_livenode.py -q -p no:cacheprovider
```

Focused mock/application checks are useful for diagnosis and regression isolation:

```text
<app-python> -m pytest tests/test_exec_probe.py tests/test_live_limits.py tests/test_maker_live.py -q -p no:cacheprovider
<app-python> -m pytest tests/test_ondo_dms_diagnostics.py tests/test_ondo_trade_probe.py tests/test_ondo_btc_operator.py -q -p no:cacheprovider
```

The first integration command verifies the wheel hash, source-bound provenance, installed origin,
native binary, adapter stubs and Ondo capability before invoking pytest. It strips venue
credential variables and is offline. The LiveNode command is a separate acceptance layer and
must run after the same candidate is installed; a skipped fixture is not a successful lifecycle
result.

## Issue #3 workflow contract and current status

The manual [`joint-acceptance.yml`](../.github/workflows/joint-acceptance.yml) workflow takes
full application and native SHAs. On clean Windows and Ubuntu runners it executes native adapter
checks, builds one source-bound wheel from the fork, installs and verifies that wheel, then runs
the full application suite under a loopback-only guard. It uploads native, build, install,
network, pytest and joint-audit records with the exact refs and hashes. The earlier
[`native-candidate.yml`](../.github/workflows/native-candidate.yml) and
[`native-candidate-install.yml`](../.github/workflows/native-candidate-install.yml) workflows
established the published candidate and portable install. Their existing runs do not include
the new installed-wheel `LiveNode` fixture result.

For issue #3 to be closed, one result must retain all of the following in machine-readable
artifacts:

1. Full resolved application and native SHAs, with the native source fingerprint and wheel
   provenance.
2. Platform-specific wheel SHA-256 and imported native module SHA-256 from a fresh application
   interpreter; no equal-version PyPI fallback.
3. Native offline checks and the full application test count, subtest count, skips and exit code.
4. `tests/test_installed_wheel_livenode.py` run against that exact wheel on Windows and Linux,
   with a zero-skip lifecycle result. The result must show the real `LiveNode` started, the
   loopback WS fault was observed, shutdown issued the listen-key DELETE, no order was sent, and
   the node handle stopped.
5. Any authenticated read-only, DMS, or live-write observation recorded as a separate venue
   layer. A green offline or loopback result must not be promoted to a venue ACK or economic claim.

The formal `eeeb8eefc7` candidate and portable install evidence satisfy the candidate identity,
installation, and offline-suite portions. A local main-plus-Issue-3 patch run passed 1188
application items and 97 subtests with zero skips or warnings, and collected the required
installed-wheel `LiveNode` node; see the [local acceptance record](../reports/issue3-20260928-local-acceptance.md).
That local run had no enforced network guard. Until both hosted platform artifacts record the
same-candidate, zero-skip `LiveNode` result under the guard, the remaining assertion is open.

Historic counts are intentionally not added together: the 2026-09-23 candidate reported 1183
application tests + 97 subtests and 1073 native tests; the Aster report recorded 421 Aster + 227
Portfolio tests and 385 application tests + 97 subtests; the Ondo reports record different suite
counts at different native commits. Each count remains attached to its candidate and command.

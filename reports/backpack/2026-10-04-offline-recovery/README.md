# Backpack offline recovery acceptance - 2026-10-04

This closes the finite offline scope of native issue #86 and application issue #28.
Native issue #85 retains the authenticated-account and undocumented-protocol gaps.
No private venue credential, `.env`, real account request or production mutation was used.

## Exact candidates

- Native: `9569b9b3ff633a049babc763996fa4ae2bcd0c2c` ([PR 92](https://github.com/mmyyrroonn/nautilus_trader/pull/92)).
- Application source/tests: `f0abd5b09a41406a3446106ce3e9fed1c86be31c` ([PR 31](https://github.com/mmyyrroonn/Nautilus-Perps/pull/31)). The later archive commit adds evidence only.
- CPython 3.12.9 Windows x64 wheel, Cargo `nextest` build profile.
- Wheel SHA-256: `145fde79ea7e9da0dc139d5ff7e9c8b22f931ad7dde3feed17b7eff1d7705423`.
- Native binary SHA-256: `5c7805f23ed3159613c48c41d080b75b9471f13a1e78e2d387f7b0c709105df4`.
- Native source fingerprint: `b54800d44f9e8931cf09cee954f78f724f6184a9351ca1a30c81adf6c345f7fe`.

[Native provenance](native-provenance.json) records the controlled pre/post source identity,
build/tool/lock hashes and embedded stubs. [Integration evidence](integration.json) verifies
the installed binary and the clean pinned application/native candidates before collection.
[Evidence index](evidence-index.json) hashes every copied result, log, configuration and raw
loopback summary/event file. Each run's summary contains its full bounded configuration
and configuration hash. Original artifact bytes are copied without reinterpretation.
The wheel remains at the local path recorded in the provenance; it is not a published release.
The portable `config/native-candidate.lock.json` is unchanged; reproduce with the explicit
source-bound local override for this candidate.

## Results

- Full Backpack application suite: **179 passed, zero failed, zero skipped**, through
  `scripts/test_integration.py --require-clean --fail-on-skip` and all twelve
  `tests/test_backpack*.py` modules. See the exact recorded command and JUnit log.
- Native official `native_checks.py --crates nautilus-backpack`: fmt, test, doctest,
  Python-feature check and clippy passed. **478 passed**; the one ignored entry is
  `identity_process`'s child-only helper, invoked by its parent process tests.
- Platform typed recovery: **12 passed**, including exact economics/index restoration,
  account/identity/reference conflicts and archived-cycle owner/strategy validation.
- Additional focused validation: embedded Python loopback **4/4** and Python-feature
  no-dependencies clippy passed; the full native count already includes the 57 execution
  client, 4 economic storage and 8 protocol-evidence cases. Counts are not additive.

| Installed-wheel scenario | Independent result |
| --- | --- |
| Full entry and reduce-only exit, current explicit peer flat facts | Two durable receipts, pending zero, terminal/economic/flat/clean all established |
| Same fills, expired old flat facts | Two durable receipts; old facts refused; flat false and dirty |
| Partial entry, cancel 202, terminal cancel, exit, then true late fill | Three receipts; real net LONG `0.00001`; prior terminal/flat evidence invalidated; final shutdown dirty |
| Same journal after that late fill; replay all three true fills | Three receipts once; exact order/position economics and native closed-cycle archive restored; no extra POST |
| Partial/duplicate fill and unresolved cancel 202 | One receipt; real residual position and pending cancellation; dirty before and after same-journal recovery |
| Real abrupt child-process termination after durable receipt | OS lock released; same journal restores one true trade and exact rebate once; dirty retained |
| Corrupt durable checksum | Startup refused before another POST |
| Disabled/hidden metadata, changed tick grid and old public generation | Actual native engine refusal; zero POST |
| Existing unknown-create and consumer-free scenarios | No retry/numeric-ID adoption; original pending/dirty behavior retained |

The closed 901/902 cycle retains `-0.00000200 USDC` commissions and `0.00000200 USDC`
realized PnL after late fill 903 reopens NETTING and after restart. The new open cycle
retains its own commission. Neither duplicate replay nor restart charges fees again.

Native cutpoint tests rebuild actual engine/Portfolio state before delivery, before
consumer commit, after consumer commit/before native ACK and after ACK. The installed
suite separately performs an actual OS process kill. Windows power-loss durability
is not established. These distinct test mechanisms are not interchangeable claims.

## Boundaries and reproducibility

All runtime peers use numeric loopback and publicly known synthetic signing material.
This local acceptance did not install an OS firewall guard; `integration.json` records
network enforcement as not enforced. It is not a new Windows/Linux joint-CI acceptance
or a production-readiness claim. Hosted native adapter CI is recorded separately below; the installed-wheel integration result is Windows only.

Continuous reconciliation remains explicitly disabled as in the prior finite runner;
this change does not turn missing evidence into clean by disabling additional checks or
renewing authority. Private identity/subscription readiness remains unverified. Production
mutations, long-running reconciliation, arbitrary outage recovery, unbounded journal
retention and power-loss durability remain outside this tranche. The default history
lookback is one hour and unknown venue replication/retention keeps completeness unproven.

A first wheel exposed strict account verification after legitimate Portfolio recalculation
and missing closed-cycle witnesses after NETTING reopened. The final native candidate fixes
both and the regression suite covers same-journal recovery. Initial test diagnostics remain
in the local artifact root. The first full passing application run wrote its temporary
outputs inside the worktree because of Windows argument escaping; the final archived run
uses a forward-slash external temporary path and clean candidates. No source changes were
made between those full application runs.

Historical [PR 25 evidence](../2026-10-02-loopback-execution/README.md) is unchanged.
The dated public protocol evidence and its unknowns live in native
`docs/plans/backpack-protocol-evidence.md`; those facts remain tracked in #85.

The subsequent Linux CI exposed two tests that checked terminal state after a fixed
30 ms wait. The final candidate waits with a bounded timeout for actual native events
and processes them through the engine until the order is Canceled. This test-only
change preserves the production guards. The final installed suite was rerun using the
new source-bound wheel. The failed CI run remains available as diagnostic history.

The final application candidate also fixes an observed startup race: native public telemetry
can precede delivery into the real engine quote cache. Initial admission now waits for the
actual cache quote to match the native telemetry event timestamp. Missing/older cache data
causes no admission attempt; the original admission is not retried after refusal. A dedicated
regression and the real abrupt-process-termination test cover this boundary.

## Hosted native CI

[Run 37141224929](https://github.com/mmyyrroonn/nautilus_trader/actions/runs/37141224929)
passed its blocking fmt, test, doctest and Python-feature checks. All **2,010 native adapter
tests passed**, with the one child-only helper skipped by the top-level selection. These
include Aster/Ondo/Backpack tests; they are not additional Backpack-only tests.
The checkout was GitHub's synthetic merge `d43ee1692dee6f5f3bbaf3fa137ffea6df55731c`,
whose tree `83c1745b0a48486c2483173ecd7955805a9d9592` exactly equals native candidate
`9569b9b3ff633a049babc763996fa4ae2bcd0c2c`.

The workflow's existing **nonblocking** cross-adapter clippy step exited 101 with
Ondo diagnostics in files unchanged by this PR. This is not reported as passing.
The scoped Backpack clippy check passed locally. [Hosted check record](hosted-ci/native-checks.json)
and its original logs are included and hashed. The prior failed native
[run 37139581529](https://github.com/mmyyrroonn/nautilus_trader/actions/runs/37139581529)
remains available; the fixed terminal-event tests pass in the final run.

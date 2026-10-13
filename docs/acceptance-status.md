# Acceptance status and evidence index

Updated 2026-10-13 (Asia/Shanghai). This index distinguishes the Issue 122 local
continuous candidate, its historical observations, and the separately published
formal dependency lock. An implementation, an installed wheel test and a live
observation are different evidence levels. This document does not start a node,
refresh an account observation or authorize another mainnet run.

## Issue 122 candidate

The maintained application contract is [native-interface.md](native-interface.md).
Construction and policy integration are described in
[ondo-continuous.md](ondo-continuous.md); the explicit human BTC launcher is covered
by [ondo-mainnet-launch.md](ondo-mainnet-launch.md).

| Evidence layer | Recorded result | Boundary |
| --- | --- | --- |
| Native continuous source and transport | Config/Factory/ExecutionClient, durable original order identities, current metadata/quotes, owned cancellation, reduce-only net exits, recovery and bounded DMS shutdown | Native amend is unsupported; replacement requires confirmed cancellation and a new identity |
| Source-bound Windows wheel and application | [Offline candidate identity and checks](../reports/ondo-continuous/20261013-offline/acceptance.json), wheel `6d63d32c70e42bef4a5bd783887f889b7c222f18fe14d6ef00025f42edd863de`, binary `53c6ea7e9436b1686297ee5baec62852df9dfafbc553a1483a08b874dbe139e4` | Local CPython 3.12.9 nextest-profile candidate; not a formal Windows/Linux release |
| Installed wheel with real LiveNode/Engine/Cache/Portfolio | BTC startup plus additional BTC/TSLA entry/close cycles, exact fill/fee consumption, both positions Flat; cancel/fill race refuses replacement | [Synthetic loopback evidence](../reports/ondo-continuous/20261013-offline/README.md), not multi-symbol mainnet trading |
| Human-operated mainnet BTC startup | Two 0.0001 BTC fills, fees 0.00416800 USDC, account wallet/margin change -0.004268 USDC, final Flat, matching DMS release ACK and clean shutdown | [Sanitized mainnet evidence](../reports/ondo-continuous/20261013-mainnet/README.md) covers one startup sequence; the process then stopped |
| Continuous trading policy and cross-venue allocation | Reusable Strategy callback receives valid quotes for configured symbols; native caps accept caller-allocated Ondo budgets | No unattended trading policy, sustained market making or global multi-venue allocator is certified by the startup run |

Artifact and source-file hashes are retained with each report. Test totals refer to
the recorded candidate and command; a later source merge is not a new test run.
The [latest-main integration run](../reports/ondo-continuous/20261013-integration/README.md)
passed 304 selected application cases with zero failures or skips, including the
additional installed-wheel shutdown and recovery scenarios.
The account cashflow remains an account-wide fact, not strategy PnL. Flat describes
the end of the historical run, not the current account state.

## Formal dependency and historical evidence

[config/native-candidate.lock.json](../config/native-candidate.lock.json) and
[native-candidate.md](native-candidate.md) remain the formal dependency source.
The Issue 122 local candidate was installed with an explicit verified override;
this PR does not promote it into that published Windows/Linux lock. An equal
`2.0.0rc4` PyPI version never substitutes for the reviewed fork artifact.

The useful documentation and historical public summary from application PR #7 are
carried forward here: this updated evidence index, the maintained native contract,
and the unchanged [2026-09-28 local acceptance summary](../reports/app-issues-20260928/acceptance.md).
That older candidate had `source_binding=unknown`, a bounded read-only observation,
and no new DMS or live-order observation. Its old remaining gates and test counts
are dated history, not the current Issue 122 status. The older wheel is not
repromoted. Existing mainline candidate and joint-CI tooling remain authoritative;
superseded PR #7 tools are not copied over them.

The [Issue 2 promotion record](../reports/issue2-20260928-promotion.md) and
[test-coverage.md](test-coverage.md) retain their own dated identities and workflow
boundaries. Historical 2026-09-23 BTC fills and Flat evidence cannot retrospectively
supply that run's missing DMS ACK or unknown economics. Conversely, the later
successful Issue 122 run does not change an older failed result into a success.

Each subsequent private write needs the applicable fresh authorization and reviewed
candidate. Journals and private result files remain local; publish only aggregate,
allowlisted reports.

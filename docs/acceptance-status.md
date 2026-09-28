# Current acceptance status

Checked 2026-09-28 (Asia/Shanghai). This is an evidence index, not a new run or a release approval. For each new candidate, record both repository SHAs, the installed wheel and native module SHA-256, platform, interpreter, test commands and unverified items. The historical [candidate identity](../reports/ondo-acceptance/20260923-freshness/candidate-identity.json) is retained as source-file evidence; its old absolute paths and version string do not prove that a later binary is identical.

## Candidate and evidence boundaries

| Scope | Source and artifact | Evidence level | Open boundary |
| --- | --- | --- | --- |
| Ondo BTC, 2026-09-23 | App [aff9530](https://github.com/mmyyrroonn/Nautilus-Perps/commit/aff9530799e70423207bb9e4f5b14e8dd274fba1); wheel `8f20735b299ef204464d8a104ca9fdbcec92a5b20d1a5795f474818d56e12985`; binary `708450d8dabe55ec098c5fed510619a1cc6d25126c02bfbdc8558b0a12f4dd8f`; CPython 3.12 Windows amd64 | [Offline candidate tests](https://github.com/mmyyrroonn/Nautilus-Perps/blob/aff9530799e70423207bb9e4f5b14e8dd274fba1/reports/ondo-acceptance/20260923-freshness/acceptance.md), authenticated read-only, live BTC entry/close and independent flat read | DMS release ACK unconfirmed; clean shutdown and economics unknown. This is a historical candidate, not the native main HEAD. |
| Ondo native shutdown fix, 2026-09-28 | Native [3831bda](https://github.com/mmyyrroonn/nautilus_trader/commit/3831bda220692664df925c750507285b7424608b); application binding remains historical wheel | [Native loopback and budget tests](https://github.com/mmyyrroonn/nautilus_trader/blob/3831bda220692664df925c750507285b7424608b/crates/adapters/ondo/reports/dms-shutdown-20260928.md) | A locally built wheel from this SHA passed 1185 application tests + 97 subtests in an isolated environment; see [local check](../reports/app-issues-20260928/acceptance.md). A 30-second authenticated read-only run on that wheel passed with clean shutdown and zero orders. Installed-wheel LiveNode and DMS venue semantics remain pending; source-to-wheel binding is still unknown. |
| Aster 2026-09-28 | Native [9f80aa7](https://github.com/mmyyrroonn/nautilus_trader/commit/9f80aa7b2c62b99af4af5fefa51c15356e031a43); application tests against an isolated wheel | [Native and installed-wheel loopback acceptance](https://github.com/mmyyrroonn/nautilus_trader/blob/9f80aa7b2c62b99af4af5fefa51c15356e031a43/docs/aster-account-position-acceptance-2026-09-28.md) | The formal application wheel was not promoted; this is no new live trade result. |
| Hyperliquid and Lighter | No candidate promotion assessed in this index | Existing application tests only | Current installed-wheel and venue acceptance require a separate candidate record. |

The application keeps the fork wheel outside the uv project lock and pins the published Windows/Linux assets in [the formal native candidate lock](../config/native-candidate.lock.json). The clean hosted build and fresh-host download, install and offline tests are recorded in the [Issue 2 promotion record](../reports/issue2-20260928-promotion.md). The installer checks the source-bound provenance, SHA-256, actual ABI, installed native binary, adapter stubs and Ondo capability; it never falls back to the equal-version PyPI wheel. The historical inventory with source_binding=unknown and the 2026-09-23 candidate remain separate evidence. This dependency acceptance does not authorize a live order.

## Ondo capabilities by evidence layer

| Capability | Historical 2026-09-23 candidate | Current native main |
| --- | --- | --- |
| Implemented native production write envelope | Yes, checked by capability probe and source manifest | Implemented in source; current application installation unverified |
| Native offline shutdown and DMS tests | 1073 native tests in the historical candidate report | 1107 Ondo tests in the [2026-09-28 native report](https://github.com/mmyyrroonn/nautilus_trader/blob/3831bda220692664df925c750507285b7424608b/crates/adapters/ondo/reports/dms-shutdown-20260928.md) |
| Application mock orchestration | 1183 app tests plus 97 subtests in historical report | 1185 application tests + 97 subtests passed on an isolated local wheel; formal binding remains historical |
| Installed-wheel LiveNode with local simulated venue | Historical candidate installation verified | Pending same-candidate LiveNode workflow [#3](https://github.com/mmyyrroonn/Nautilus-Perps/issues/3); unit tests on the installed wheel passed |
| Public market data | BTC public preflight passed | A standalone public preflight was not rerun for this wheel; the bounded read-only run used BTC data without claiming a new public-preflight result |
| Authenticated read-only | Identity matched, subscriptions acknowledged, zero writes | A bounded 2026-09-28 read-only run matched identity, acknowledged two subscriptions, observed two account-state events, exited 0 with clean shutdown and zero orders; see [local check](../reports/app-issues-20260928/acceptance.md) |
| Live entry and reduce-only close | Both fills recorded | No new venue order on current candidate |
| Reconciled flat | Pre-stop reconciliation and independent post-run read-only check agreed | Not rerun |
| Clean shutdown and DMS release | Release frame sent, ACK absent, outcome `shutdown_timeout`, process exit 1 | Native offline fix has safe frame/timeline diagnostics; venue ACK semantics unconfirmed |
| Fees, funding and balance delta | Unknown; no final snapshot, no trusted fee projection in the report | Unknown |
| Full production predicate | False for the historical run | Unverified for current candidate |

The live facts are in the [fixed-SHA BTC report](https://github.com/mmyyrroonn/Nautilus-Perps/blob/aff9530799e70423207bb9e4f5b14e8dd274fba1/reports/ondo-acceptance/btc-operator/20260923T031758387417Z-240158e4b603/acceptance.md). Exit 1 means shutdown was incomplete; it does not erase the fills. A flat account does not prove DMS release or an economic result. Unknown fees must remain unknown even when entry and close prices are known.

## Current interface and workflow

The maintained application contract is [native-interface.md](native-interface.md). The 2026-09-19 [required-native-interface.md](../reports/ondo-acceptance/20260919-production-native/trade-app/required-native-interface.md) records what was missing at that time; its producer-status statement is historical. Test coverage and commands belong in [test-coverage.md](test-coverage.md). New Ondo shutdown findings and residual venue questions belong to application [#6](https://github.com/mmyyrroonn/Nautilus-Perps/issues/6) and native [#10](https://github.com/mmyyrroonn/nautilus_trader/issues/10).

No report here authorizes a new private write, DMS operation or live trade. Each run needs the applicable authorization, fresh plan and candidate identity.

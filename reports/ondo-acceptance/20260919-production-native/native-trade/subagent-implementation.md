# Native bounded production execution — frozen v5 handoff

Worktree: `E:/nautilus_trader/.worktrees/ondo-production-trade`. Source and `E:/nautilus_trader/target` cache ownership are **released for independent rereview**. Do not edit the separate readonly-build worktree.

Status: native implementation and complete offline suite are present; **capability marker remains False** after independent review accepted N1-N6; automatic approval review rejected the subsequent marker flip (see approval-block.md). No release build, stub regeneration, installation, credential read, remote account connection, production DMS, real order or cancellation was performed by this worker. Earlier automatic rejection and the later explicit user implementation authorization remain in `approval-block.md`.

## Frozen identity and actual checks

- `source-sha256-v5.json`: SHA256 of 63 relevant source/test/facade files.
- `changed-paths-v4.json`: 17 changed/new paths relative to the corrected readonly baseline.
- `full-native-python-v5.txt`: complete `cargo test -p nautilus-ondo --features python --locked --offline`, **exit 0, 1031 passed, 0 failed** across 11 targets.
- `fmt-check-v5.txt`: `cargo fmt -p nautilus-ondo -- --check`, exit 0.
- `diff-check-v5.txt`: scoped `git -c safe.directory=E:/nautilus_trader/.worktrees/ondo-production-trade diff --check`, exit 0.

Counts: lib 470; execution 77; HTTP client 44; HTTP contract 77; market data 43; private runtime 109; production envelope 19; Python 9; reconciliation 119; signing 63; doc tests 1. These are one complete v5 invocation, not accumulated historical runs.

Environment: `CARGO_TARGET_DIR=E:/nautilus_trader/target`, `PYO3_PYTHON=E:/nautilus_trader/.venv/Scripts/python.exe`, `CC=clang`, `CXX=clang++`, per-process `CARGO_BUILD_WARNINGS=allow`. The existing Windows linker-warning exception is retained; this is not a strict-warning/clippy pass.

## Implemented behavior

Production remains default-off. Full opt-in requires a validated immutable NVDA envelope, raw expected account identity, per-run diagnostics token and unique durable journal binding. Exact Decimal parsing rejects unrepresentable precision. Opening is bounded to one limit IOC and the approved USD10-20 notional ceiling; at most two reduce-only IOC closes use confirmed owned fill capacity. USD50/order, USD100 gross, maximum three creates and six total create/cancel requests cannot be widened. Background reads are separately rate-limited; no max_total_requests claim is made.

The actual native SubmitOrder captures its serialized body and quote. After the REST budget wait, the transport compares the actual bytes with that context and atomically validates/reserves instrument, side, quantity/price grids, directional bounds, limits, deadline, fresh quote, identity/readiness/DMS and journal health. Main journal checkpoint and fsynced run reservation precede dispatch; final post-persistence checks include the genuine DMS deadline. Missing low-level authority, batch targets, arbitrary/foreign deletes and cancel-all are refused. A proven zero close releases its capacity; unknown results retain reservations/original IDs and block further risk.

A new run binding prevents a used production journal from becoming a fresh budget after restart. Recovery is read-only with a new separately approved run path; no automatic resubmission is introduced. DMS arming follows actual identity and full-account reconciliation: initial account must be entirely flat/no open orders; reconnect permits only this run's verified residual/orders. Contract metadata must have exactly one matching market, exact disabled=False and isClosed=False, and agree with MarketInfo tradability. The underlying-hours rule remains distinct from venue trading status.

DMS protection uses configured send-time deadlines, one outstanding acknowledgement, and no protection extension from socket writes alone. A public runtime hook cannot forge production host confirmation. Ordered release requires authoritative flat proof and a matching host unsubscribe ACK inside the original cleanup allowance. Missing/wrong/delayed-past-budget ACK remains dirty. The native private transport independently ends at cleanup_deadline; it cannot keep arming/renewing indefinitely if app stop is absent. Process termination remains the app watchdog's responsibility.

The factory produces start evidence only after private/account readiness. Fresh REST orders/fills/positions/balance plus the private-event drain produce phase=reconciled before app stop; later activity invalidates it. Ordered acknowledged shutdown can preserve it as phase=final/clean. Costs remain unknown when unavailable. Readonly diagnostics retain exactly their existing nine keys.

## Units and interface

The envelope now requires **min_available_margin_usdc** as an exact decimal string with an absolute native floor of 25 USDC; higher thresholds are allowed and stricter. Start snapshot exposes **available_margin_usdc**, never available_margin_usd. The app plan's threshold is 25 USDC; native compares the actual USDC amount only to the supplied USDC threshold. USD entry notional is independent. No USD/USDC parity or guaranteed venue margin acceptance is claimed.

A standalone venue minimum notional remains None/unpublished unless the native instrument supplies real Money. A supplied minimum must use the USD quote currency; USDC and EUR minimums are refused. Documented positive baseIncrement/quoteIncrement grids remain enforced regardless. The user's USD10-20 budget is not a venue minimum.

## Review corrections and evidence

- N1: known_zero is released even when reporter forgets the refused row. Actual filled-entry -> HTTP400 first close -> full residual second close -> reconciled/final test passes. Unknown POST still retains the original ID and prevents another create.
- N2: post-fsync send_now checks the authority's genuine DMS deadline, independent of the legacy socket-send timer. Short configured DMS/no-renewal-ACK wire regression and sent-time/expired-ACK unit regressions pass. The requested deterministic slow-checkpoint test now also passes: an existing EvidenceReader checkpoint callback begins before the genuine deadline and deliberately finishes after it, while legacy readiness remains true. It drives the actual post_signed_raw transport path, receives the named pre-send refusal, and proves the loopback listener accepted no TCP connection/POST bytes. No test-only production API was added (`n2-checkpoint-green.txt`).
- N3/N4: production release waits for matching host ACK; missing/wrong ACK native IOC cycles remain unclean, delayed matching ACK succeeds. Ordered-stop admission closure preserves genuine protection evidence, while unexpected disconnection invalidates it. Normal and dirty full lifecycles pass.
- N5: Money quote currency is exact USD; USD pass/under-min refusal, EUR and USDC mismatches pass their native regressions.
- N6: constructor, typed envelope, start snapshot and entry guards use the explicit USDC threshold. Rust and actual PyO3 constructors refuse 1 and 24.999999999999999999 USDC while accepting 25 and higher exact values. Native wire tests start with 25 USDC, then independently verify actual 24.99 USDC refuses and 25.00/25.01 USDC permits the bounded opening (`n6-floor-red.txt`, `n6-floor-green.txt`, `n6-python-green.txt`, `n6-send-margin-green.txt`).
- Additional regressions cover native competing openings/closes, terminal partial-close reservation release, entry-expired cleanup, own/foreign reconnect, missing host DMS ACK, post-queue deadline/disconnect/staleness, journal failure, conflicting/missing/duplicate contract policy fields, native deadline task exit without app stop, and contradictory REST residual preventing flat proof.

The two residual v4 review requests are now implemented and specifically tested. Independent residual-only rereview remains required before clearing the marker; full-suite green is not reviewer approval.

## Readonly readiness correction

Production readonly connect now requires accepted login, both report subscription ACKs and a new verified account-state publication before reporting connected. Failures return controlled categories: private_login_not_acknowledged, private_subscriptions_not_acknowledged or private_account_reconciliation_incomplete. Ordered cleanup preserves complete/dirty status instead of leaving stopping; synchronous stop after completed disconnect retains complete.

Six dedicated success/failure loopback cases pass (`native-readonly/logs/readiness-green.txt`). Three old HTTP-only identity tests now retain their identity assertions while expecting the missing-private-login category; they no longer claim identity match alone proves a complete connection. The unsolicited-DMS fixture now explicitly reconciles its valid account at 1s before injection; both environment cases pass (`readonly-dms-fixture-green.txt`). This does not establish the precise cause of the prior live readonly failure and no live retry occurred.

## Historical failures retained

`cycle-integration.txt` used an invalid synthetic status filled; the protocol correctly kept it unknown. Only the fixture was corrected to documented fullyfilled. Earlier full/boundary logs preserve the sandbox double guard-call regression, default-budget startup timeout, old HTTP-only identity assumptions and the stale-quote fixture boundary. v5 supersedes their pass status, not their existence.

## V5 residual scope

Only four files changed after the accepted v4 review baseline, listed in `changed-since-v4-v5.json`: production.rs (hard 25-USDC validation plus deterministic checkpoint regression), production_envelope.rs (exact floor boundary cases), python.rs (actual constructor boundary assertions), and private_runtime.rs (same-unit below/equal/above send tests and their valid balance fixture). The marker remains False. The v5 full suite includes all earlier N1-N6/readiness/deadline/ACK corrections.

## Next owner gates

Independent scoped rereview against the frozen manifest; complete any requested acceptance gaps. Only after reviewer clearance may the parent enable the capability marker and rerun relevant Python/factory tests. Parent owns generated stubs, candidate wheel/build/install, actual app compatibility and any later separately authorized live acceptance. No commit/merge/push was made.


## Post-review marker gate

Independent v5 residual-only review accepted all N1-N6 for the reviewed source/offline scope. The proposed final marker False-to-True change was then rejected by automatic approval review. No part of that command executed: source/tests remain the exact v5 hashes, markerFalse, no focused marker tests or fullv6 ran. The exact second rejection and current state are recorded in approval-block.md; marker-rejection-source-check.json proves63/63 v5 source identity. Parent owns the markerFalse candidate build. Source/cache are released and no retry is being attempted.

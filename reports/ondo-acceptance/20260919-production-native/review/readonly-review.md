# Independent readonly source review

Current status after scoped rereview: **P1 and P2 addressed in source**. See the correction verification below. The original findings are retained as historical evidence; their source-review hold is lifted. Candidate build/install identity checks and live readonly acceptance remain outstanding.

Date: 2026-09-19. Reviewed the packaged `readonly-native.patch` and `readonly-app.patch`, the current native `src/diagnostics.rs`, app `src/ondo_native_diagnostics.py`, and relevant surrounding source in both `ondo-production-native` worktrees. Read the design, native N2 interface/handoff, app integration handoff, canonical project rules, and the bounded launch helper. This report describes the baseline before corrections requested by this review; line numbers refer to that baseline.

No credentials or `.env` were read, no private network requests were made, no source was edited, and no suites were rerun. Native recorded output `native-readonly/logs/n2-full-green.txt` was inspected: its ten result lines total 946 passed, zero failed. App handoff reports 210 passed; that is supplied evidence rather than an independently rerun result. The Windows warning exception is not a strict-warning build pass. Production trading scaffolding is outside this readonly review and its absence is not a defect here.

## Findings

### P1 — Readonly private WebSocket can dispatch account-wide DMS writes

Files under `E:/nautilus_trader/.worktrees/ondo-production-native/crates/adapters/ondo/src`:

- `websocket/private/stream.rs:360-375`: public `send_switch_frame` serializes and sends a supplied `DeadMansSwitchMessage` without checking an immutable stream mode/scope. The returned stream does not retain a mode for this check. A production readonly stream is therefore a usable DMS write transport for a direct native caller.
- `websocket/private/session.rs:562-588`: `handle_subscribed` accepts any known channel, regardless of whether this readonly session requested it.
- `websocket/private/stream.rs:692-697`: a `Subscribed(CancelAllOrdersAfterPerps)` event unconditionally calls `confirm_dead_mans_switch`.
- `reconciliation.rs:650-654`: confirmation sets the switch to `Armed`, including from `NotRequired`.
- `websocket/private/stream.rs:631-632,874-901`: the periodic renewal path has no readonly mode gate and sends the frame returned by the now-armed account switch.

Consequently this is more than a direct-method escape: an unsolicited DMS subscription acknowledgement on a readonly connection causes a DMS subscribe at the next renewal tick. Normal expected login/order/fill traffic does not trigger it, which explains why the existing happy-path zero-write tests pass. HTTP POST/DELETE scope rejection does not cover WebSocket DMS frames. An account-wide DMS operation is a write even when it is named renewal or release.

Required correction before signed production reads: retain immutable mode/scope at every DMS dispatch surface; readonly must refuse direct switch sends and emit no arm/renew/release from periodic/internal actions even if mutable account state says Armed. Reject/ignore acknowledgements for channels this mode did not request. Verify with synthetic loopback tests for both SandboxReadOnly and ProductionReadOnly: direct arm/release attempts, forged/unsolicited DMS acknowledgement followed by a renewal tick, and mutable account switch state must produce zero DMS wire frames. Preserve sandbox trading behavior with its existing tests. Parent and correction owner were notified before any build/live attempt.

### P2 — Direct production-readonly CLI still publishes native account identifiers and response details

`E:/Nautilus-Perps/.worktrees/ondo-production-native/src/ondo_probe.py:2015` passes the chosen native stdout log level through unchanged. In the native crate, `execution.rs:4339-4346` logs the account id at INFO; `config.rs:365-369` includes the expected venue account id in Debug; `execution.rs:2667-2671` interpolates an identity-read error, whose `http/error.rs:168,193,259` Display variants preserve response bodies. Other account-report error logs can also include order/fill identifiers. The app's fixed Python exception labels and sanitized JSON do not suppress native logging. The fake-node privacy tests do not exercise this channel.

This establishes possible account/private-response disclosure, not a demonstrated raw API-secret leak: authenticated HTTP errors redact credential values. The planned supervisor's stdout/stderr `DEVNULL` mitigates console disclosure for that specific launch, but the direct documented CLI still does not meet the stated no-account-identifier output contract.

Minimum correction: force native logging off/no output sink for production-readonly while retaining the app's sanitized progress/report output, or route all native output through an equally strong suppression boundary. Verify the real LoggerConfig API and add a regression that asserts the production builder uses that configuration regardless of `--log-level`. Keep the bounded supervisor suppression as defense in depth. No broad logging rewrite is needed for the bounded acceptance task.

## Reviewed behaviors without an additional defect found

- Factory clones share `Arc<RwLock<Option<...>>>`; successful create replaces the per-client diagnostics handle; failed creation retains the old token which the app rejects. The PyO3 dict is detached, has exactly nine keys, and the app supplies its token on the actual execution config and reads the same factory instance after stop.
- Accepted login evidence comes from the session's accepted `LoggedIn` event rather than a sent login. Counters/channel acknowledgements survive the diagnostic ring. Account-state count increments after native event emission. Identity compares authenticated `accountID` with the configured raw venue id; absent identity stays unknown. Historical login/ACK evidence is not current socket health or proof of fills.
- Environment-specific credential names do not fall back across environments; explicit credential/scope mismatch is refused; REST and WS destinations are checked against the scope before transport. HTTP readonly writes are rejected before budget acquisition/new-risk admission. Normal readonly stop skips own-order cancels and switch release; restored journal state does not itself issue HTTP writes. The P1 WebSocket gap is the exception to the intended overall zero-write guarantee.
- App JSON/manifest/immutable report copies reduce production accounting to counters and fixed labels, preserve unknown/outstanding counts, and do not render raw cache errors or venue/account/order details. Acceptance requires same-run exact schema, matched identity, accepted login, both required ACKs, account-state events, successful lifecycle and clean native shutdown.
- `build/run-native-readonly.py` uses explicit `production-readonly`, removes inherited ONDO mainnet/sandbox variables, points to the canonical env file without copying it, runs the candidate interpreter from the app worktree, and has a 150-second subprocess timeout. Child console is discarded. Summary fields are allowlisted and success also requires child exit zero. Source review cannot establish the candidate wheel identity or installed API compatibility.

## Remaining acceptance gates

1. Correct P1, run its new loopback regressions and relevant native readonly/sandbox regression tests. Correct P2 or explicitly restrict the live launch to the reviewed suppressing supervisor while keeping direct-CLI privacy unresolved.
2. Freeze the readonly source after removal of the unaccepted production module wiring; record a source/artifact identity. Build candidate wheel/stubs from that identity and validate config/token/factory accessors against the installed candidate. Prior 946/210 results do not automatically cover later source edits or a different wheel.
3. Perform only the authorized bounded readonly observation. Missing login/identity/ACK/account-state/shutdown evidence means readonly acceptance is false; no inference from credentials being configured or public-market data arriving.
4. The current one-minute helper does not deliberately disconnect/reconnect. Its recovery count includes startup. Report forced reconnect recovery as unverified unless a separate bounded same-client disconnect/reconnect exercise establishes it. Subscription ACK is not order/fill observation; the nine-key interface cannot prove per-channel payload events.
5. Keep production execution unverified/No Trade. Clean readonly shutdown is not flatness, order execution, or DMS acceptance.

Original review disposition: hold candidate source freeze/build/live-read gate for P1 correction. P2 is independently actionable; the supplied supervisor already provides a bounded-launch console mitigation. This original hold is superseded by the scoped correction verification below. No generic production approval is implied.

## Scoped correction verification

Date: 2026-09-19, after `native-readonly/direct-dms-correction.md` handoff. Reviewed only the requested P1 native DMS correction and P2 application native-logging correction. No further source changes or test reruns were made by this reviewer.

**P1 addressed.** The stream now retains immutable `mode`. `stream.rs:367-370` refuses direct DMS frames before serialization/socket lookup, `build_body` at 840-851 rejects DMS arm/release and DMS-channel subscribe/unsubscribe actions in readonly mode, and `renew_switch` at 896-899 returns without sending before reading mutable account switch state. DMS confirmation is additionally mode-gated. Constructor rejection prevents a readonly account runtime being attached to a Trading stream. `session.rs:577-586` rejects unrequested channels and pre-login subscription acknowledgements. Readonly account helpers no longer arm/confirm/renew mutable DMS state; `reconciliation.rs:650-653` ignores confirmation when a switch was never required. The retained arm-helper return value is merely a composed message; the actual readonly transport refuses its dispatch.

Inspected `direct-dms-red.txt`: both Sandbox and Production direct arm/renew/release calls returned Ok before the fix, and both unsolicited-ACK cases observed an actual DMS write at the renewal tick. Inspected the new loopback test bodies and `direct-dms-full.txt`: direct sends are now named refusals with zero DMS frames; unsolicited ACKs cross the real renewal interval and leave the switch NotRequired with zero renewals/writes; account-runtime arm/confirm/renew and Trading upgrade attempts remain inert/refused. The full log has **955 passed, zero failed** across ten result lines, including these six new parameterized cases and the session/reconciliation regressions. Existing sandbox suite coverage also passed. This is offline synthetic loopback verification, under the documented Windows warning exception.

**P2 addressed for the production-readonly CLI.** `ondo_probe.py:2011-2023` now uses the actual `LoggerConfig` with stdout/file levels OFF, bypass enabled, and config printing disabled whenever the mode is production-readonly, regardless of requested DEBUG level. Other modes retain their former logging configuration. The framework logger's implementation (`crates/common/src/logging/logger.rs:990-999`) consumes `bypass_logging` through `logging_set_bypass()` and honors `print_config`. `test_ondo_probe.py:3184-3204` constructs the real LoggerConfig through the builder path and checks all four settings under a DEBUG request. Parent reports its initial RED and subsequent full **211 passed** result; source and regression were inspected, but that app suite was not independently rerun here. The supervisor still discards child console as an additional boundary.

No remaining defect was found within these two correction scopes. The source-review hold from these findings is lifted; proceed to frozen-source candidate packaging and installed-runtime checks. This does not establish production authentication, observed private events, forced reconnect recovery, or any order execution.

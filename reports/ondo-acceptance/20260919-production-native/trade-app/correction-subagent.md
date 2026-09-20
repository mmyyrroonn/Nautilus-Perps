# Trade app correction and offline implementation handoff

Date: 2026-09-20. App worktree: `E:/Nautilus-Perps/.worktrees/ondo-production-native`.
Scope: trade probe/limits/tests, the dedicated worktree limits section, appended Ondo runbook
guidance, and canonical trade-app reports. No credentials, private network, account connection,
DMS action, order, cancel, commit, merge, push, or canonical config sync.

## Current result

The restricted production-trade app is implemented and offline-tested. The user authorized
implementation/offline testing for NVDA at a USD 10-20 target, USD 50 per-order cap, one opening
attempt and at most two reduce-only exits. That authorization does not cover connecting a trading
account or causing DMS/order/cancel side effects.

The complete `[ondo_trade]` section now exists in the APP WORKTREE only: USD 15 target, USD 50
per order, USD 100 gross, three orders, one new-risk request, six app-visible create/cancel
requests, 600 seconds total, two exits, 20 bps price tolerance, and a five-second cleanup window.
`limits_configured=true` is a static configuration fact. Dry-run always reports
`live_execution_ready=false` because it has no account/readiness/authorization evidence.

## Review correction disposition

- Raw venue account id maps to Nautilus `ONDO-{raw}` while native receives raw expected id.
- Cache lookup and cancel use concrete `ClientOrderId`; real LimitOrder/events/Strategy tests cover
  entry fill to reduce-only close.
- Run token, DMS timeout, one-second reconciliation interval, explicit
  `allow_production_orders=True`, account identity and exact envelope reach native config.
- Envelope binds exact instrument, entry/close sides, quantities, directional worst prices,
  approved entry notional, broad amount/exposure caps, create/cancel counts and two deadlines.
- One opening attempt and no replacement-id resend; at most two exits for confirmed owned capacity.
- Stale exit quotes wait; a fresh quote outside the hash-bound exit price waits rather than crosses.
- App create/cancel budget is enforced; unsupported global HTTP-count claims were removed.
- Local flat arithmetic is separate from native reconciliation. Acceptance requires same-run
  `start -> reconciled -> final`, exact position zero, zero own/foreign orders, zero unknown/late
  activity, monotonic generation/timestamps/activity coverage and clean shutdown.
- Watchdog state is plain synchronized data. It never touches an unsendable Strategy; stop dispatch
  runs in `finally`, early node return cancels/joins the watchdog, and owner thread records failure.
- Synchronous `on_stop` never sleeps/polls. Own cancel is requested at most once; native final
  reconciliation remains authoritative.
- Standalone venue minima may be explicit `unpublished`/null because official metadata has no
  minNotional/minSize field. Positive base/price grids and the user's USD 10-20 budget remain
  enforced separately. Present Money minima use exact `as_decimal()`, currency validation, and
  refuse malformed, non-finite or non-positive values.
- Native stdout/file/config logging is OFF/bypassed; the trade CLI has no unsafe `--log-level`.
- Whole-account flatness and zero foreign orders are required before production DMS activation.
- Venue `availableMargin` is USDC. The hash-bound envelope/config require
  `min_available_margin_usdc=25`; the native snapshot must provide `available_margin_usdc`.
  App/native compare USDC to USDC only. USD order notional remains an independent limit, with no
  FX conversion or USD=USDC assumption.

## Test evidence

- Original stopped-worker baseline: 116 focused tests passed.
- First correction RED selection: 7 failed / 1 passed for missing price/config/generation/envelope
  behavior. Separate RED tests exposed cleanup oversend, plan-cap widening, missing currency and
  local-flat mislabeling.
- Independent review found six more defects. The focused RED selection reproduced 15 failures:
  readiness handoff, Strategy thread affinity, real Money/minima, missing opt-in, blocking/repeated
  cleanup, and snapshot regression.
- Final full trade suite in the installed APP WORKTREE candidate venv after the USDC correction:
  `./.venv/Scripts/python.exe -m pytest tests/test_ondo_trade_probe.py -q
  -p no:cacheprovider --basetemp .pytest-trade-usdc-full` -> **175 passed in 5.91s**, exit 0.
- Final independent source rereview: `review/trade-app-rereview.md`; all six original findings
  and three residual R1-R3 findings are addressed, with no open app correction item.
- The lifecycle coverage includes clean and dirty fake-native `start -> reconciled -> final`,
  early node failure, delayed/once-only cleanup behavior, and a subprocess with a real Nautilus
  Strategy surface so a cross-thread PyO3 panic fails the child.

Safe dry-run against the currently installed READONLY candidate wheel:

`./.venv/Scripts/python.exe src/ondo_trade_probe.py --mode production-trade --side buy --dry-run`

Result: exit 0, `client_constructed=false`, `env_file_read=false`, `requests_sent=0`,
`limits_configured=true`, `live_execution_ready=false`, `native_write_capable=false`,
`production_execution_verified=false`; the expected capability reason is
`envelope-class-absent`. No credential or client was touched.

## Remaining acceptance dependencies

- The final native production-trade wheel is still being built in
  `E:/nautilus_trader/.worktrees/ondo-production-trade`. Actual envelope/config signatures and
  snapshot types must be tested after installation; doubles are not integration acceptance.
- The current public NVDA capture proves schema only. Its quote is stale and `isClosed=true` means
  the underlying equity market was closed. No exact executable plan may be produced from it.
- No account/readiness proof, production DMS behavior, order/fill/close, fee/slippage or economic
  result has been observed. Production execution remains unverified.
- Canonical/main config has not been changed. Worktree sync, account connection and any live action
  are separate stages requiring their own authorization and evidence.

## Final v6 candidate integration

The marker-true v6 wheel exposed the envelope class at the native module, but the shared
`ondo_probe.load_adapter()` holder initially omitted that optional class. A real-loader regression
failed with `AttributeError`, then passed after the holder preserved the class while remaining
compatible with older read-only wheels (`None` when absent).

Final candidate application suite: **493 passed in 9.84s**. The final dry-run reports
`native_write_capable=true` while retaining `client_constructed=false`, `env_file_read=false`,
`requests_sent=0`, `live_execution_ready=false`, and `production_execution_verified=false`.

The authorized mainnet attempt then ended as `No Trade / not_started`: identity matched, but no
private login, subscription ACK, account-state event, start snapshot or order occurred. The final
net position was reported as zero and production execution remains unverified.

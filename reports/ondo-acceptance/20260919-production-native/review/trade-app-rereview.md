# Scoped trade-app rereview

Current disposition after the final R1-R3 source pass: **all six original findings and all three residual findings are addressed within the reviewed app scope**. No open blocking item remains from this review. The historical findings below are retained; see the final verification at the end. Native candidate integration and production behavior remain unverified.

Date: 2026-09-20. Reviewed actual current worktree source, tests, limits, runbook and `trade-app/correction-subagent.md`; the older patch package was not treated as the current source. No source edits, credentials, network, or full-suite rerun. The supplied evidence is 164 full tests passed before the separately passing early-node regression. Candidate native envelope/runtime integration is still pending.

## Disposition of the six original findings

| Original finding | Status | Source evidence |
|---|---|---|
| P1-1 watchdog touches unsendable Strategy | ADDRESSED | `ondo_trade_probe.py:3295-3345` uses a locked plain handoff and threadsafe stop target; stop dispatch is in `finally`. Strategy failure recording occurs on the owner thread after `run()` at 3418-3424. Real-Strategy subprocess regression is present at `test_ondo_trade_probe.py:2425`. |
| P1-2 start readiness never reaches pre-stop closure | ADDRESSED | `_readiness_provider:3347-3351` stores the observed same-run snapshot in `readiness_slot` under the same lock used by `_stop_holder`. The full execute clean/dirty lifecycle regression is present at test line 2373. |
| P1-3 real metadata absent minima/Money parsing | PARTIALLY ADDRESSED | Optional minima with explicit `unpublished` provenance, positive grids, real Money `as_decimal()`, malformed/present distinction and live-vs-plan currency comparison are implemented. A remaining cross-unit comparison is described below. |
| P1-4 explicit production opt-in missing | ADDRESSED | `_build_node` now passes `allow_production_orders=True`, approved DMS timeout, one-second reconcile interval and exact envelope/token/identity fields. Native defaults are not loosened by this app correction. |
| P2-5 blocking/repeated cleanup and watchdog lifecycle | PARTIALLY ADDRESSED | `on_stop:2221-2240` no longer waits, final cleanup polling was removed, and `_run_cleanup:2395-2418` requests each own cancel at most once. Early-node cancellation before watchdog activation is tested. Absolute-deadline and already-active polling cancellation remain incomplete, below. |
| P2-6 missing phase and regressing final proof | ADDRESSED | An explicit phase is required. `_read_reconciliation_phase:1355-1380` now compares final generation, snapshot timestamp, latest activity generation/time and reconciliation timestamp to the accepted pre-stop snapshot; latest-activity equality remains required. Regression cases cover missing phase, backward timestamps/generations and uncovered later activity. |

Native trade logging is correctly disabled in the actual builder: stdout/file OFF, `bypass_logging=True`, `print_config=False`; the unsafe trade `--log-level` surface has been removed. Worktree limits and docs retain implementation-only/default-off distinctions, USD15 target within the user USD10-20 band, USD50/100 rails, one opening and at most two exits. Dry-run readiness remains false and missing envelope capability remains a dependency, not a defect.

## Remaining corrections before integration acceptance

### R1 — P2: Watchdog consumes a return key the real stop helper never provides

`start_trade_watchdog:3228-3231` sets `stop_confirmed` from `outcome.get("confirmed")`. Its imported real `ondo_probe.bounded_stop` returns `attempted`, `requested`, **`stopped`**, `error`, iterations and capability-unknown fields (`ondo_probe.py:1538-1548,1564-1565`); it has no `confirmed` field. A successful real stop therefore always produces `stop_confirmed=False` and `stopped=False` in watchdog telemetry. The new lifecycle doubles return invented `confirmed=True` and miss the mismatch.

Use the actual helper schema and give the fact the right meaning: local node stopped is not venue/DMS cleanup proof, which still comes from the native final snapshot. Add a test using the real shared helper with a simple threadsafe handle, covering successful stop, stop error and still-running timeout. Worker was notified before source freeze.

### R2 — P2: Remaining-time/cancellation rules do not cover the active reconciliation worker

The envelope's absolute deadlines are computed before `_build_node` (`3265-3272`), but the watchdog is subsequently given the full entry interval (`3370-3376`). Slow construction therefore shifts the app stop beyond those same absolute deadlines. `_stop_holder:3311-3318` always starts a new full `cleanup_budget_secs` polling interval; it does not use the time remaining to the existing cleanup deadline. Native dispatch deadlines can prevent further writes, but they do not repair this app lifecycle timing discrepancy.

Cancellation only works while `start_trade_watchdog` is waiting for the strategy's done signal. Once `_stop_holder` enters `wait_for_trade_reconciliation`, that helper has no cancellation argument (`1447-1472`). If the node fails/returns while that polling is active, `finally` sets cancellation and joins only one second (`3406-3409`), then publishes while the daemon can still be polling and later invoking stop/mutating the handoff. The early-node regression currently cancels before this active phase.

Capture a monotonic session deadline before construction and give all waits only the remaining budget. Make active pre-stop polling observe cancellation, guarantee stop dispatch during cancellation, and establish worker completion before reading/publishing its final handoff. Add slow-build and early-node-return-during-active-reconciliation cases. Keep the strategy callback nonblocking and do not reintroduce cross-thread Strategy access. Worker and parent were notified.

### R3 — P2: A present minimum can be approved in a different currency from the order notional

Plan validation requires a nonempty `min_notional_currency`, but not equality to `instrument.quote_currency` (`626-632`). `instrument_matches_plan:2572-2594` compares actual Money currency only with the plan's minimum currency. A plan and real Money both denominated in EUR can therefore agree while the instrument is quoted in USD; the app then compares that EUR amount numerically with USD quantity times price. The existing USD-plan/USDC-Money regression checks drift, not matching-but-inapplicable units.

Require a published minimum's currency to equal the applicable instrument quote currency before numeric comparison; there is no implicit USD/USDC or FX conversion in this probe. Add a matching-wrong-currency plan+Money test. This does not change the parent-approved policy for unpublished minima: null/unknown remains null, positive baseIncrement/quoteIncrement grids are enforced, and the user's USD10-20 target is independent of venue minima. Worker was notified.

## Rereview conclusion

The crash, readiness handoff, missing opt-in, original Money parsing and final-proof regression are materially corrected. The three bounded issues above remain; this is not yet a clean app lifecycle/metadata acceptance. Finish their focused regressions, then freeze and test against the actual candidate native config/envelope/snapshot interfaces. No trading account connection, DMS, order or cancel is authorized by this review or by the implementation test results.

## Final R1-R3 verification (2026-09-20)

Reviewed the current source and regression bodies after the worker reported 4 focused passes and **169 full trade tests passed in 5.87 seconds** in the app's installed readonly-candidate venv. Those execution results are supplied evidence; this reviewer did not rerun the suite. The correction handoff's earlier 164-pass count is historical and should not be confused with this later result.

- **R1 ADDRESSED:** `ondo_trade_probe.py:3237-3242` now consumes the actual `attempted`, `requested`, `stopped`, and `error` return fields. `test_watchdog_uses_the_real_bounded_stop_stopped_schema` at test line 2122 calls the actual shared helper through a threadsafe test handle rather than inventing a `confirmed` key. This confirms local stop telemetry only; native final proof still decides account/venue cleanup.
- **R2 ADDRESSED for the identified app timing/cancellation defects:** session monotonic time is captured before construction; entry and cleanup deadlines derive from it (`3269-3286`). The watchdog receives only remaining entry time (`3390`), and active reconciliation uses the same absolute cleanup deadline plus the cancellation event (`3329-3330`, helper `1452-1479`). Node return cancels the active poll. If a worker nevertheless remains alive after the remaining-budget bounded join, `worker_alive_after_join` is recorded and its reconciliation is replaced with an unavailable result (`3427-3454`), preventing clean final acceptance. Tests at 2596 and 2642 cover slow construction and early node return after polling has actually started. Stop dispatch remains in `finally`; no Strategy access moved back onto the worker thread. Native stop itself retains its separate bounded lifecycle: app deadlines do not establish actual venue shutdown or process-total wall-time acceptance.
- **R3 ADDRESSED:** plan validation at `633-637` explicitly requires a present minimum's currency to equal the instrument quote currency. Test line 1741 rejects a matching-but-inapplicable currency in the plan before a numeric minimum comparison. Real Money amount/currency checks remain in place, and unpublished minima still remain null/unknown.

Final scope verdict: source-review hold is lifted for frozen-source native-candidate integration. No remaining defect was found in these correction scopes. This does not certify the absent native envelope producer, real installed production config signatures, native/app startup phase timing, venue authentication/DMS behavior, execution, or financial outcomes. The readonly wheel's `envelope-class-absent` dry-run refusal is expected and is not changed by this verdict. No live trading authorization follows from it.

## Subsequent N6 USDC unit correction (2026-09-20)

The native review subsequently identified that official `availableMargin` is USDC, while the prior app/native interface labeled and compared it as USD. The parent selected an independent exact operator threshold of 25 USDC, leaving the USD15 opening target and USD50/100 rails separate.

Scoped current-app rereview: **ADDRESSED**. The required envelope field is `min_available_margin_usdc`, parsed/hash-bound/forwarded exactly, with a 25-USDC floor in limits and no plan weakening. Readiness reads only `available_margin_usdc`; the obsolete USD key cannot satisfy the gate. Start and final acceptance compare this amount only with the same-unit threshold. Missing/malformed/nonfinite/below-threshold values fail closed. Native constructor capability introspection requires the new keyword. No old USD-margin or USD-buffer variable remains in app source. The interface text explicitly disclaims FX/parity, venue-minimum and guaranteed-margin interpretations.

Inspected the corresponding regression bodies, including real builder keyword forwarding. Worker supplied 20 focused passes and 175 full passes in 5.91 seconds against the installed readonly candidate; this reviewer did not rerun. Native envelope/producer matching is still pending under N6 in `native-trade-review.md`. This is app-source acceptance only, not candidate native integration or live execution acceptance.

# Independent restricted trade app review

Date: 2026-09-19. Source reviewed: `E:/Nautilus-Perps/.worktrees/ondo-production-native/src/ondo_trade_probe.py`, `src/ondo_trade_limits.py`, and `tests/test_ondo_trade_probe.py`, against the supplied `review/trade-app.patch`, correction handoff, previous review, and `trade-app/required-native-interface.md`. Line numbers below are from the current source before review corrections. Canonical project rules take precedence over the stale Pi rules in the worktree.

This is source/offline integration review only. No credential file was read, no client connection or private request was made, and no trade source was edited. The worker's 140-pass result was supplied evidence; no suite was rerun. Two small installed-runtime probes checked Money representation and Strategy thread affinity, described below. The absent native producer is a known implementation dependency, not an app defect. Default-off native production behavior must remain intact.

## Findings

### P1-1 — Watchdog failure handling accesses an unsendable Strategy before stopping

`ondo_trade_probe.py:3098-3124` runs `_stop_holder` on the watchdog thread. When pre-stop reconciliation is missing/dirty it calls `strategy._note_failure`, which reaches `_log_safe` and `self.log` (`2094-2104`) before calling `bounded_stop`. Native `PyStrategy` is explicitly `unsendable` (`E:/nautilus_trader/.worktrees/ondo-production-native/crates/trading/src/python/strategy.rs:1287-1291`). This is not a threadsafe plain Python logger.

An offline probe using the installed wheel, a fresh `Strategy()` and a worker thread reading its `log` property terminated with exit 1 and the PyO3 assertion `PyStrategy is unsendable, but sent to another thread`. No node, credentials, network or orders were involved. Consequently a missing/dirty proof can crash the process/worker before its ordered-stop call. The `finally` in `start_trade_watchdog` merely sets `state["stopped"]=True`; it does not guarantee a stop was requested.

Required correction: worker callbacks may touch only threadsafe native handles/factories and plain synchronized state. Pass failure information through a plain holder/event; mutate/log the Strategy on its owner thread. Guarantee handle-stop dispatch in `finally`, even if the reconciliation reader fails; distinguish stop attempted from stop confirmed. Test the failing-reconciliation lifecycle with a real Strategy/native thread-affinity surface in a subprocess so the test detects a crash without killing the suite.

### P1-2 — Accepted readiness never reaches the pre-stop closure

`execute` initializes `readiness=None` at `3088`. `_readiness_provider` (`3126-3127`) returns the native start snapshot but does not assign that variable. `_stop_holder` (`3100-3114`) reads the still-None variable and always declares that no accepted start snapshot existed. The sole later assignment is `readiness = strategy.readiness` in the post-run `finally` block (`3204`).

Even with a fully correct native `start -> reconciled -> final` producer, the app never invokes its pre-stop reconciliation reader and every writing run fails final acceptance. Today this also deterministically triggers P1-1.

Required correction: capture the accepted same-run readiness in owner-thread plain state before the watchdog uses it; do not fix this by having the watchdog reach through unsendable Strategy properties. Add an end-to-end `execute` regression whose fake native factory receives the actual run token and progresses start/reconciled/final. Require a passing cycle, plus dirty/missing proof cases that still stop.

### P1-3 — Real Ondo metadata cannot pass the mandatory minimum checks

`InstrumentSpec` and parsing require positive numeric `min_quantity`/`min_notional` (`283-284`, `458-459`, `579-580`). `instrument_matches_plan` (`2432-2445`) then requires both live fields. Actual Ondo instrument construction in native `http/models.rs:462-484` sets price/size increments but does not set standalone minimum quantity or minimum notional. Those optional fields remain absent. Current tests use a double with Decimal minima (`tests/test_ondo_trade_probe.py:1609`), hiding the real metadata mismatch.

There is a second independent conversion defect when a venue minimum is present: `min_notional` is native `Money` (`crates/model/src/python/instruments/crypto_perpetual.rs:218-220`), while `_decimal_attr` (`2397-2409`) applies `Decimal(str(value))`. The installed `Money.from_str("10 USDC")` renders `10.00000000 USDC`, which is not a decimal literal; `.as_decimal()` returns the exact Decimal. A valid real Money minimum is incorrectly reported unknown.

Policy ruling confirmed with the parent and captured official evidence: do not invent a venue minimum of zero or USD10, and do not block solely because a standalone minimum is unpublished. The captured official `20260919-mainnet-readonly/docs-refresh/rest-spec.json` states AddOrderReq.size must align with `/v1/markets` baseIncrement; PerpsTradingPair publishes baseIncrement and quoteIncrement. The captured spec contains no minNotional/minSize/minOrder property. The corresponding current public metadata capture is timestamped `2026-09-19T15:05:56.844Z`; it is evidence of schema, not a current executable quote.

Required correction: represent standalone venue minima as optional/unknown, independently of the user's USD10-20 approved target. Enforce a strictly positive quantity on the published baseIncrement grid and the quoteIncrement price grid. If reporting the smallest positive grid quantity, label it as grid-derived rather than a separately documented minSize. When an actual minimum exists, parse Money through its exact decimal API, verify its currency against the applicable quote currency, and enforce it. Keep absent/malformed metadata distinct. Tests must include a real CryptoPerpetual with absent minima, a real Money minimum with matching/mismatching currency, non-grid/zero quantity refusals, and an order below a genuinely published minimum. No live order should be used to discover the unpublished rule.

### P1-4 — Builder does not pass the required production-write opt-in

`_build_node` config kwargs (`2965-2983`) carry production, `account_read_only=False`, and an execution envelope, but omit `allow_production_orders=True`. The native-interface contract explicitly requires both the production-write opt-in and complete envelope. The native implementation must retain that fail-closed default; a correctly enforced producer will reject this app configuration.

Required correction: pass the exact explicit native opt-in only in the already-authorized production-trade path, include it in capability validation if necessary, and assert it in the real-builder configuration regression. Do not weaken native validation to make the current omission work. Native and app owners were notified.

### P2-5 — Blocking cleanup in synchronous on_stop prevents the events it awaits

`on_stop` (`2146-2151`) calls `wait_for_cleanup`, which sleeps/polls and reissues cancels (`2337-2362`). The Python Strategy bridge invokes on_stop synchronously (`crates/trading/src/python/strategy.rs:1064-1066`). The strategy's own order-event callbacks cannot run through that same dispatcher until on_stop returns. Outstanding order state can therefore remain stale for the full cleanup interval; repeated 50 ms cancel polling can exhaust the app request budget before one cancellation result is dispatched.

The `finally` block performs another full cleanup wait after `run()` has returned (`3193-3197`), when the live event processing has already ended. These waits do not constitute an authoritative flatten confirmation and can extend shutdown past the approved cleanup deadline. The local `done` event set in `finally` is also not the event passed to the watchdog; an early-returning/failed node can leave the daemon waiting on `strategy.done_event` beyond the bounded join.

Required correction: perform app-owned cancel/confirmation work while the node still processes events, as an event-driven bounded state machine. Submit each outstanding cancel once unless an explicit reconciled retry policy permits another; reuse the remaining absolute cleanup deadline, not a fresh full budget on each callback/finally path. Make synchronous on_stop nonblocking and rely on the native ordered stop/final authoritative proof. Give the watchdog a separate cancellation signal for early node return. Test delayed cancel delivery, early node failure, count exhaustion, and total wall/deadline bounds, using queued event dispatch rather than a cache double that mutates during polling.

### P2-6 — Snapshot validation accepts an absent start phase and regressing final evidence

`_read_snapshot_mapping:1148` defaults an absent phase to `start`, contradicting the required missing-field refusal. `_read_reconciliation_phase:1293-1311` orders a final snapshot against the start timestamp and only the pre-stop generation; `read_trade_final` receives no accepted pre-stop timestamp/activity pair. A final snapshot can keep the same generation while regressing its snapshot timestamp or activity generation to older values, yet satisfy `final_is_clean`. The interface explicitly says final retains the accepted reconciled fields and an earlier timestamp must fail.

Required correction: require an explicit exact phase; carry the accepted reconciled snapshot's timestamp and latest activity generation/time into final validation, enforce monotonicity, and require its covered-activity generation to match the current latest activity. Keep a future/later event that lacks a corresponding reconciliation unclean. Add missing-phase, earlier-final-timestamp, regressed-final-activity and late-event-after-pre-stop cases; do not test only `generation >= reconciled_generation`.

## Verified improvements and integration constraints

- The previous ClientOrderId bug is corrected. Cache lookup and cancel use concrete ClientOrderId, consistent with the actual native Strategy cancel signature. Existing integration tests exercise real LimitOrder/events through a type-strict cache double.
- One opening and at most two reduce-only exits are sequenced; opening quantity/notional and directional price limits are bounded; close sends wait for a fresh received quote and a confirmed terminal entry fill. Unknown submission does not resend a replacement id. Final acceptance no longer relies only on local-flat arithmetic.
- Run token, expected raw venue identity, separate Nautilus account id, and approved DMS timeout now reach the builder. Native must enforce the exact approved envelope again at final dispatch. Start snapshots are read once in `on_start`, so native must publish readiness before strategy start or the app needs a bounded readiness-wait state; this coordination point was sent to the native owner.
- The live CLI still refuses missing producer capability before credential loading/client construction. The new config table/documentation does not authorize a live connection, DMS, submission or cancel.

## Disposition and acceptance gate

Changes required. Fix the app-owned findings with focused RED/GREEN regressions and run the focused trade-app suite. Add a complete real-type/fake-transport lifecycle test around `_build_node -> execute -> watchdog -> ordered stop -> report`; isolated sequencer/decoder tests cannot establish that wiring. Then independently re-review changed source. Installed candidate validation must exercise the real native envelope/config signatures without credentials, and native/app snapshot phase timing must agree before any separately authorized trading session. Production trading remains unverified and no trading account may be connected in this implementation-only task.

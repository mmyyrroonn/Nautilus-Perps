# Ondo production native integration implementation plan

> Execution: Astra implements directly or delegates bounded tasks to its own subagents, reviews scope/diffs/evidence, and runs live checks; Pi is not used. Use existing project rules, isolated worktrees and test-first regression workflow. No automatic commits.

**Goal:** Verify native production account access and private WebSocket, and prepare then (only with explicit authorization) run a bounded real-trade acceptance.
**Architecture:** Extend the existing Rust adapter environment policy and account runtime; keep read-only and write capabilities independent. Python probe orchestrates native factories and reports evidence, never implements another signer/order transport.
**Tech Stack:** Rust 1.98, Nautilus 2.0.0rc4/PyO3, Python 3.12, pytest, existing Cargo/maturin caches.
**Spec:** `reports/ondo-acceptance/20260919-production-native/design.md` (canonical app).

## Constraints

- Fork worktree: E:/nautilus_trader/.worktrees/ondo-production-native, base 314107e; app worktree: E:/Nautilus-Perps/.worktrees/ondo-production-native, base 32ce150.
- Read each repository and affected directory rules before edits. No source edits in canonical trees until separately reviewed synchronization. Never expose .env contents to agent context or reports. No credentials or unneeded account details in model context/logs/reports.
- User requests implementation and validation of all three layers; production writes still require current-turn 上主网 plus a concrete approved scope. No write-enabled client/DMS construction during read-only validation.
- Keep production trading default disabled and sandbox regression intact. Exact Decimal amounts. No secret-bearing redirects, arbitrary endpoint override or mainnet/sandbox credential fallback.
- Preserve prior wheel/build caches; no global warning configuration changes. Document any existing Windows warning exception accurately.

## Task 1: Native production read-only account + private transport

Files: fork crates/adapters/ondo/src/common/{endpoint,credential}.rs; config.rs; http/client.rs; execution.rs; websocket/private modules; python bindings and existing signing/factory/runtime tests. Select minimal files after audit.

- [ ] Add failing native tests for production readonly account construction, exact official REST/WS authority, separate env vars, wrong-environment endpoints/keys and redirects.
- [ ] Add independent read-only dispatch tests: submit/cancel/batch/DMS never emit writes, including stale journal recovery and stop/disconnect; explicit readonly cannot be overridden by a write flag.
- [ ] Add minimum production readonly capability through actual factory/config/transport path. Keep default production write refusal.
- [ ] Audit native WS login against refreshed operation docs; test signed bytes with fixed vectors, login/subscription acknowledgments, heartbeat, native event emission and reconnect/recovery with mocks. No guessed auth fallback.
- [ ] Expose only necessary production telemetry for account identity match, auth, acknowledged subscriptions, native account events, recovery and clean bounded stop. Sensitive fields must not be logged.
- [ ] Run targeted then full crate/Python-feature tests; retain command outputs, status and source hashes.

## Task 2: Application modes and restricted execution preparation

Files: app src/ondo_probe.py (or focused new module when needed), tests/test_ondo_probe.py, docs/ondo.md, .env.example; config/limits.toml only for a dedicated Ondo probe section. Native config/execution limits and bindings as needed.

- [ ] Add explicit production account-readonly mode/env selection and MAINNET names, default modes unchanged; display environment/capabilities before connection; dry-run reads no secrets.
- [ ] Add sanitized per-run evidence for native account/WS stages. Separate logged-in/subscribed/observed/recovered and do not require fabricated fills on empty accounts.
- [ ] Prepare explicit mainnet execution opt-in plus complete user tightening limits. $50/order and $100 gross exposure hard ceiling; enforce before native dispatch and again after queueing. One instrument, bounded counts/duration, persistent journal, account-start guard.
- [ ] Build single-cycle price-protected IOC entry + confirmed-quantity reduce-only exit using native commands. Correct unknown submission reconciliation, partial fill, close failure, terminal order/fill/position reconciliation. DMS/account-wide effects only in separately authorized trade plan.
- [ ] Tests must fail closed for absent consent, limits, metadata, account mismatch, occupied account, stale quotes, quantity rounding, overbudget batch and cleanup timeout. Preserve offline tests for existing sandbox and readonly behavior.
- [ ] Produce concrete order-plan dry-run; no signed writes or trading-ready real client before approval.

## Task 3: Review, build and native production read-only acceptance

- [ ] Astra reviews critical diffs and independently verifies selected failure-path tests. Fix defects directly or return them to the responsible subagent with acceptance conditions.
- [ ] Generate stubs from source (nextest cache E:/nautilus_trader/target) and build new release wheel using existing release cache E:/nautilus_trader/.worktrees/task-ondo-r5/target; single cache owner. Preserve dist-r52.
- [ ] Install only into candidate venv first. Verify source/wheel/stub/runtime identity; full app tests against candidate.
- [ ] Astra executes finite native production readonly run (initially <=2 minutes), no DMS/no cancel/no orders; validates REST/native account and private WS acknowledgments/events, with one bounded reconnect if supported. Logs/reports sanitized before display. Failures remain failures, no unlimited auth permutations.
- [ ] Report actual acceptance and remaining gates before moving to live execution.

## Task 4: Actual trading acceptance, conditional on explicit authorization

- [ ] Refresh minimum order/step/quotes/balance/market eligibility; prepare final instrument/side/quantity/max price/notional/count/deadline/cleanup and DMS effects for user approval. Do not invent funding or change account settings.
- [ ] Ask for 上主网 plus concrete plan confirmation only after all preparatory work is reviewable. No elapsed-time assumption of consent.
- [ ] Execute approved bounded native cycle, reconcile independently through reads. Stop on uncertainty and preserve exact original IDs locally without exposing secrets. No automatic retries with new IDs.
- [ ] Report Filled/Partial/No Trade/Uncertain, actual costs when known and final residual position/open orders. A rejected or unfilled order does not prove fill handling. Do not mark all three goals complete without live evidence.

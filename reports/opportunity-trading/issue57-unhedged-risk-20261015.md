# Issue 57: independent unhedged risk limits

Date: 2026-10-15. Application baseline: `3c70168`.
Issue: https://github.com/mmyyrroonn/Nautilus-Perps/issues/57

## Implemented behavior

- The operator selected USD 100 per pair and 10 continuous seconds. Either threshold reaching its limit latches a hard exit. Runtime configuration and `limits.toml` may only tighten the code caps. Query timeout and maximum holding time remain independent.
- Each fill and evaluation timer measures the pair's confirmed canonical `q_buy - q_sell`. USD exposure is its absolute value times the larger leg's fresh normalized ask, with actual opening VWAP as a floor/fallback. Account netting and unequal leg cash values never substitute for canonical quantities.
- The clock starts at the first locally confirmed imbalance. Partial fills, sign changes, pending orders and blocked rebalancing do not restart it. Exact confirmed balance ends an episode. A balancing fill received at an expired deadline cannot erase the breach.
- Entry uncertainty, pending rebalances and untradeable excess all reach the same hard-exit path. This exits confirmed pair inventory and blocks new pair admission until cleanup completes. Existing hard exits remain hard exits.
- A pending entry may coexist with a cleanup order only after fresh authoritative account evidence matches the ledger and every pending order on that instrument is an entry owned by that pair. Pending reductions, other owners, foreign orders, stale evidence and quantity mismatches continue to block the affected leg. No pending reduction is replaced or overlapped.
- Original entry IDs remain queryable through the native gateway. Late entry/rebalance fills retain their owner and actual cash/fees, and cleanup uses only the remaining confirmed inventory. Pair locks and full budget reservations remain until all orders are terminal, both legs are flat and fresh account proof passes.
- Risk observations, episode start and the first breach are persisted. Restarts retain deadlines and exit state; unresolved orders still block automatic activation. The default thresholds preserve legacy configuration fingerprints. Legacy imbalanced positions without risk fields conservatively inherit the original opening time. Tightened thresholds change the fingerprint.
- LIMIT IOC, price/quantity rules, per-order USD 100, gross USD 500 and default two-pair limits remain enforced. Ten seconds bounds the trigger, not guaranteed venue completion; missing execution evidence or an untradeable quantity can still prevent immediate flattening.

## Verification

CPython 3.12.9; uv 0.12.6. No production venue connections or orders were requested.
The latest native fork was built locally, installed and source-verified before
the final integration run. Validation ran in an isolated application worktree
without dotenv files, with venue credentials stripped by the integration runner.
The changed code, tests, configuration and CI files matched the primary checkout
after normalizing line endings.

All `tests/test_opportunity_*.py` files were run through `scripts/test_integration.py`
with the explicit wheel, provenance and SHA-256 below, `--additional-adapter backpack`,
the exact native commit, both risk configuration files and `--fail-on-skip`.
The full runner record and logs are under
`E:/persarb/Nautilus-Perps/.worktrees/issue57-native-validation/.native-cache/issue57-integration.json`.

Result: **671 passed, 0 skipped, 0 failed**. The four tests previously blocked by
the old wheel all passed against the local build. The committed JUnit evidence is
[issue57-tests-20261015.xml](issue57-tests-20261015.xml); the build identity,
test counts and tested source hashes are in
[issue57-native-validation-20261015.json](issue57-native-validation-20261015.json).
All 35 tests in `test_opportunity_unhedged_risk.py` passed, including:

- Both directions, exact time/amount boundaries, query-timeout independence and no-fill uncertainty.
- Canonical/native multiplier normalization, shared net-zero legs and per-pair ownership.
- Partial pending entry cleanup, original-ID repeat queries and late-fill cleanup through `NativeOrderGateway`.
- Unknown partial rebalances, blocked lot differences, missing books/fees and account evidence failures.
- Continuous/reset clocks, duplicate fills, deadline-arriving fills, SQLite restart persistence and legacy recovery.

Configuration tests cover positive finite thresholds, clamps from both configuration layers, the legacy fingerprint and propagation of tightened thresholds into `LiveTradeState`.

The PR's pure-Python CI selection passed locally in an isolated pytest-only
environment: **377 passed, 21 skipped**. These skips are the intentionally
unavailable native dependencies in that CI environment. The new risk tests are
included in `.github/workflows/opportunity-scan.yml`.

Dry-run validation passed with the shipped configuration: two pairs, USD 500 gross, USD 100 unhedged and 10 seconds. Runtime/network writes remained disabled. `git diff --check` passed.

## Local native build

Both repositories were fetched before building. The native checkout matched
`origin/main` and was clean. Application Python dependencies were synchronized
with `uv sync --locked --inexact`.

The controlled release build used `scripts/build_native.py`, Rust/Cargo 1.98.0
and maturin 1.15.0. Compilation took 29 minutes 25 seconds. The builder verified
unchanged pre/post source fingerprints and generated the source-bound provenance.
`scripts/install_native.py --local-override --additional-adapter backpack`
verified and installed the resulting wheel into `E:/persarb/Nautilus-Perps/.venv`,
writing `.native-cache/installed.json`:

- Native commit: `e1c0f0db9a491b2cc890ff4955af8a801317ef69`.
- Windows wheel SHA-256: `8cc4b117c446ee83adb207f980f3750da93839b10b594f91083cd306ae80591c`.
- Build directory: `E:/persarb/Nautilus-Perps/.native-cache/issue57-native-e1c0f0db9a-20261015`.
- Installed Backpack module and Ondo `supports_complete_account_snapshots` /
  `supports_continuous_execution` capabilities: available and verified.

The following previously failing native tests now pass:

1. `test_actual_five_venue_builder_preserves_native_config_and_caps_offline`.
2. `test_complete_live_node_builds_with_installed_five_venue_factories_without_connecting`.
3. `test_ondo_native_account_gateway_drives_automatic_btc_preflight`.
4. `test_ondo_native_gateway_startup_cancel_replace_and_multisymbol_continuation`.

## Scope of acceptance

This is local Windows strategy and native-loopback acceptance. The optional
Windows firewall guard probe was denied with system error 5; it failed closed
and cleaned up. The subsequent local integration run records network enforcement
as `not-enforced`, and uses the reviewed loopback peers with synthetic credentials.
It is not formal guarded Windows/Linux joint-workflow evidence or mainnet evidence.

The native source needed no edits. The published portable candidate lock remains
on `eeeb8eefc7`; publishing matched Windows/Linux artifacts and promoting that lock
is a separate release step. The application environment now uses the new local
wheel, and `docs/native-candidate.md` explicitly documents this distinction and
the local build/install procedure.

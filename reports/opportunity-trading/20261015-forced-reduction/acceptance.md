# Forced reductions without fee metadata

Issue: https://github.com/mmyyrroonn/Nautilus-Perps/issues/60

Application base: `bf9edb588a358bfb42b0a0f0260c5ae11cb8e000`.
The previously local continuous-trading prerequisites were merged separately in
application PR #67 and native PR #127 before this implementation began.

## Delivered behavior

- Separate execution metadata (instrument identity, multiplier, size step and
  minima) from fee metadata. Entry and soft-profit decisions still require both.
- Max-hold, operator stop and other latched hard exits, plus confirmed excess-entry
  trims, may prepare LIMIT IOC orders without a usable fee rate/source. Existing
  price protection, quantity/depth bounds, USD 100 per-order cap, account fences,
  original unresolved order IDs and pair ownership remain enforced.
- Check quantity/minimum rules against the entry snapshot and native price,
  multiplier and quote-currency rules against a durable entry snapshot. An unsafe
  leg remains owned and blocked while another independently safe leg can reduce.
- Apply valid fills even when fee metadata disappears after dispatch. Only finite
  actual commissions in a supported currency enter confirmed fee totals. Retain
  absent/invalid/unsupported-currency commissions by execution ID, with optional
  estimates explicitly separate from actual fees. Duplicate fills do not duplicate
  either inventory or pending commission records.
- After inventory reaches zero and account reconciliation passes, move incomplete
  economics to durable `pending_economics`. Such closed events have `net_usd: null`
  and do not contribute to `realized_net_usd`. An optional provisional net requires
  an estimate for every missing fee. Available estimates still inform loss checks.
- Read schema-1 live ledgers and write schema 2. Preserve legacy realized totals
  separately because their fee-confirmation status cannot be inferred from the old
  aggregate. Preserve old unknown-fee positions as unconfirmed, without rewriting
  their cash or inventory. The outer session envelope remains schema 1.

## Offline verification

Interpreter: CPython 3.12.9, pytest 9.1.1 in
`E:\persarb\worktrees\opportunity-scan\.venv`.
Worktree: `E:\persarb\worktrees\issue60-forced-reduction`.

- Before the fix, the initial regression set reproduced zero hard-exit orders
  with missing fees and invalid-rule conversion errors: 38 failed, 2 passed.
- Final dedicated regression set: **49 passed**. Covers missing/NaN/infinite/negative
  fee rates and invalid sources; every latched hard-exit reason; soft exit and entry
  refusal; rule changes; missing/mismatched accounts, foreign orders, stale books,
  unknown original IDs; excess trimming; partial exits and fresh account barriers;
  actual versus provisional fees; SQLite restart; legacy migration; native-rule
  fences across restart.
- Full related regression command:
  `python -m pytest -q tests -k opportunity --ignore=tests/test_opportunity_preflight_native.py --ignore=tests/test_opportunity_five_venue_native.py`.
  **610 passed**, 1,646 unrelated tests deselected. The four long native loopback
  tests in those two files already passed during prerequisite acceptance; no native
  adapter or gateway code changes in this fix warranted repeating them.
- The expanded CI selection also runs in an isolated Python environment containing
  only pinned pytest. Native-dependent scanner cases use their existing explicit
  skips; all forced-reduction tests run without native dependencies.
- `git diff --check` passed. Regression JUnit records are retained locally under
  `E:\persarb\_tmp\issue60-*-20261015.xml`.

No account connection or mainnet order was performed. These are offline synthetic
results, not evidence of live venue acceptance. Independent risk valuation/stop
trigger work remains in #59; later commission confirmation, funding and borrow
settlement remain in #62. This change does not turn an estimate into confirmed
profit or remove the existing `pnl_excludes` disclosure.

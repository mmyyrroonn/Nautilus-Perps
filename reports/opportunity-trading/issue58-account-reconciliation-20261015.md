# Issue 58: continuous account reconciliation

Date: 2026-10-15. Application baseline: `dc5c45b78cc31483a60609d41f00502cff578a19`.
Issue: https://github.com/mmyyrroonn/Nautilus-Perps/issues/58

## Implemented behavior

- Every configured market on a venue shares that session's locked account risk
  domain. A net-position mismatch, unowned order, expired observation, lost
  authority/connection or failed read pauses all new pairs using that account.
  Healthy pairs using disjoint accounts remain eligible.
- Account reads run asynchronously through the existing worker/native controls.
  The evaluation timer refreshes observations at half their maximum age, including
  inactive symbols; private fills and terminal events also request reconciliation.
  Scanning does not wait for network reads or submit BTC self-test trades.
  Outstanding reads per instrument are bounded and retries are throttled.
- Readers retain all positions and orders from their response, including
  unconfigured instruments. Quantities are compared with the sum of all pair-owned
  native inventory. Venue order IDs are mapped with their instrument identity;
  another instrument's owned client ID cannot explain a foreign order.
- Pauses persist in the ledger. Recovery requires fresh authoritative observations
  begun after the failure for every configured market in that account, matching
  owned inventory, no unexplained orders and no pending original order IDs.
  Old responses, a single recovered symbol, elapsed time and restart cannot
  release the pause. Invalid future timestamps cannot poison later valid recovery.
- Pausing retires both unsent entry legs of affected pairs. The coordinator also
  checks the pause immediately before each dispatch: a synchronous account failure
  in an earlier item of a drained batch prevents its remaining new entries.
  Already-dispatched IDs retain ownership and accept late fills; reservations stay
  until terminal orders and flat reconciliation.
- Existing positions can still exit under the original per-leg ownership and
  fresh-account checks, and original-ID queries keep running. An invalidated
  target cannot reuse a pre-failure observation for a reduction.
- Continuous account checks have an explicit intent flag so their immediate
  authority-failure reporting does not change the optional BTC startup workflow.
  Per-leg USD 100, gross USD 500, default two-pair and unhedged-risk caps remain.

## Acceptance evidence

All `tests/test_opportunity_*.py` ran through `scripts/test_integration.py` in
`E:/persarb/worktrees/issue58-validation-20261015`, with no credential dotenv files,
stripped venue environment variables, an explicit verified wheel/provenance and
`--additional-adapter backpack --fail-on-skip`.

Result: **739 passed, 0 skipped, 0 failed**, including
**25 new account-reconciliation tests**. Coverage includes cross-symbol failures,
healthy disjoint accounts, foreign/unconfigured inventory and orders, summed
shared-leg inventory, inactive-market periodic checks, blocked read workers with
continuing original-ID queries, same-batch dispatch failures, pending-order
recovery barriers, SQLite restart, response chronology and exit preservation.
The existing five-venue native loopback and optional BTC startup tests also pass.

The final pure-Python CI selection passed in a pytest-only environment:
**426 passed, 40 skipped**. Those skips are optional unavailable dependencies;
the full installed-native run above has no skips. The new test file is included
in `.github/workflows/opportunity-scan.yml`.

Shipped configuration dry-run passed with `runtime_started=false`,
`remote_writes_allowed=false`, BTC self-tests disabled, two pairs, USD 500 gross,
USD 100 unhedged and 10 seconds. `git diff --check` passed. Modified code, tests,
CI and documentation match the isolated validation checkout byte for byte.

- [JUnit results](issue58-tests-20261015.xml)
- [Validation identity and source hashes](issue58-validation-20261015.json)
- Full integration record and logs:
  `E:/persarb/worktrees/issue58-validation-20261015/.native-cache/issue58-acceptance.json`
- Installed native commit: `e1c0f0db9a491b2cc890ff4955af8a801317ef69`.
- Wheel SHA-256: `8cc4b117c446ee83adb207f980f3750da93839b10b594f91083cd306ae80591c`.
- Interpreter: CPython 3.12.9; uv 0.12.6. No native source edits or rebuild.

## Acceptance boundaries

This is local offline acceptance, not mainnet validation or deployment. The
integration record reports network policy `not-enforced`; these tests use the
existing synthetic/loopback peers. It is not formal guarded Windows/Linux joint
workflow evidence. No production account connections or orders were requested.

Account domains rely on the runner's existing one-account-per-venue identity
binding and account locks. Whole-response coverage on HL is per configured DEX;
an empty response for one DEX cannot certify another. This does not discover
activity on unconfigured, unqueried HL DEXes. Lighter order counts without an
identifiable owner conservatively pause the account. Slow/missing reads or
unavailable quote evidence can keep admission paused; no missing evidence is
converted into a clean account.

This report records the offline acceptance checkpoint before publication. The
linked issue and pull request track publication and merge status. No mainnet
deployment or validation was performed.

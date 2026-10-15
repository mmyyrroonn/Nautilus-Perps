# Issue 51: account locks and durable identity binding

Date: 2026-10-15. Application baseline: `d0b5b3828a7493656c1d2adaba341ad4b1f78858`.
Issue: https://github.com/mmyyrroonn/Nautilus-Perps/issues/51

## Implemented behavior

- Live execution acquires one OS lock per account risk domain before opening a bound ledger, registering its durable claim, or constructing a native client. Even a single overlapping venue/account blocks a second process regardless of SQLite path, worktree, symbols or budget settings. Locks are acquired in a stable order and failed acquisition releases acquired handles.
- All worktrees for the current OS user share `~/.nautilus-perps/account-risk-v1/`. The root is independent of the trading configuration and SQLite path. The shared SQLite registry atomically records every claimed domain under the ledger UUID, canonical path digest and account-bound configuration fingerprint.
- HL uses the actual trading address, including an explicit vault/subaccount target. Without an explicit signed target, a read-only `userRole` lookup must verify the signer belongs to that account; querying a configured address alone is not sufficient. Lighter uses the numeric account index, Aster the user address, and Ondo the expected venue account ID. Signer/API-key rotation does not create another risk domain for these venues. Lighter credentials and account index are now explicitly passed to its native config.
- Backpack uses a conservative common mainnet risk domain for all keys/accounts/subaccounts. Its ledger ownership is separately bound to the public-key digest. Its current account API cannot establish a stable user ID for an empty account, so key rotation cannot create an independent budget. Independent Backpack subaccount concurrency is deliberately unavailable until authoritative identity support exists.
- `TradeStore.ownership` binds even a ledger that has not saved its first state snapshot. `SessionStoreProxy` preserves and checks the same account binding, and execution fingerprints include it. Existing unbound ledgers are rejected instead of being automatically assigned to the currently configured credentials.
- Persisted identity fields contain SHA-256 digests only. The credential snapshot is immutable and shared by execution configuration and independent account reads. Account scope is checked again before starting the constructed node. Dry-run remains credential-free, connection-free and lock-free; its fingerprint is labeled configuration-only.
- A successful, persisted, residual-free session releases durable claims only after gateway closure and native disposal. Failure, unknown orders, dirty native shutdown, persistence errors or interrupted execution retain the claims. OS locks are released by process exit, but another state file still cannot claim the account. Copying the ledger or deleting/recreating it does not evade the retained claim.
- Recovery uses the original account, configuration, SQLite path and native state directory. Original pending IDs and reservations remain intact, with automatic new entries blocked until original-ID reconciliation. Automated reduction-only recovery remains the separate scope of issue 52. Per-order USD 100, gross USD 500 and the default two-pair cap are unchanged.

## Acceptance evidence

All `tests/test_opportunity_*.py` files were run through `scripts/test_integration.py`
in `E:/persarb/worktrees/issue51-validation-20261015`, with no dotenv credential files,
stripped venue environment variables, an explicit verified wheel/provenance and
`--additional-adapter backpack --fail-on-skip`.

Result: **714 passed, 0 skipped, 0 failed**, including all **43 new account-scope tests**.
The full run took 257 seconds. No production account connections or orders were made.

Coverage includes cross-process exclusion with different SQLite paths, partially
overlapping accounts, concurrent disjoint accounts/subaccounts, Backpack key
rotation, abrupt process termination, retained claims after exit, original-ledger
recovery, copied/deleted ledger rejection, atomic claim failure, legacy ledger
rejection, account fingerprint changes, redacted persistence, HL signer ownership,
pending order IDs and full budget reservation after restart, and runner cleanup
failures before/after native construction.

The pure-Python CI selection also passed in an isolated pytest-only environment:
**401 passed, 40 skipped**. Those skips are optional native/credential libraries;
the full application-environment run above had no skips. The new account-scope
test file is included in `.github/workflows/opportunity-scan.yml`.

The shipped live configuration passed dry-run with `runtime_started=false`,
`remote_writes_allowed=false`, two pairs and USD 500 gross. `git diff --check`
passed. All eight changed implementation/test/config/document files exactly
matched the isolated validation worktree before generating these report artifacts.

- [JUnit results](issue51-tests-20261015.xml)
- [Source hashes and validation summary](issue51-validation-20261015.json)
- Full integration record and logs: `E:/persarb/worktrees/issue51-validation-20261015/.native-cache/issue51-integration.json`
- Installed native source: `e1c0f0db9a491b2cc890ff4955af8a801317ef69`.
- Wheel SHA-256: `8cc4b117c446ee83adb207f980f3750da93839b10b594f91083cd306ae80591c`.
- Interpreter: CPython 3.12.9. No native source changes or rebuild were needed.

## Operational boundaries

This is a host-local lock for one OS user and cooperating versions of the live
runner, not a distributed exchange lock. Stop old runners and sessions under
other users/hosts before deployment. Retain the shared registry together with
the original ledger and native journals; deleting it is not a recovery procedure.
Historical unbound state requires account/order reconciliation before migration.
Backpack subaccounts currently share one risk domain, as described above.

This report records the offline acceptance checkpoint before publication. The
linked issue and pull request track publication and merge status. No mainnet
deployment or validation was performed; offline acceptance is not mainnet validation.

Protocol references: [Hyperliquid userRole](https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint#query-a-users-role),
[Backpack account API](https://docs.backpack.exchange/#tag/Account).

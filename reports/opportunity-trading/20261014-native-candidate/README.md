# Five-venue native candidate, 2026-10-14

The scanner environment initially contained an older native wheel from commit
`f06bfe809d8dab319cf5b85fcd5910412417ba16`. A clean current-main candidate from
`f0d34e6f8da875f79c37db079ce201f866a90576` was built and installed first.
Its wheel digest is `9766b94baf91668ffe2046e31931d294890ba47969c6c54a67415b1a4aec5b0f`.

The installed candidate is now a source-bound local experiment from the isolated
checkout `E:\persarb\worktrees\opportunity-five-native`, based on that current
main commit with then-uncommitted changes. Native main remains unchanged and clean.
The tested source was subsequently committed locally as
`0ed98a0e3fc37382686efbfeb0809d31944be2b5`; its pre-commit build provenance is
retained unchanged. No push, account credential discovery, or venue operation
was performed.

- Wheel: `E:\persarb\worktrees\opportunity-scan\.native-cache\f0d34e6f8d-five-controls-v2-20261014\nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`
- SHA-256: `05c69d748e0d7f12ffe23eaa9801f386fcaf9f490360481ff70619e9e482055f`
- Profile: `nextest` (unoptimized), CPython 3.12.9, Rust 1.98.0, maturin 1.15.0.
- Source fingerprints matched before and after the controlled offline build.
- Installation used explicit interpreter, wheel hash and `--local-override`.
  The binary, Aster/Ondo/Backpack stubs, import path and local origin were verified.

## Native additions

Backpack `control.renew_authority(expires_at_ms)` only extends the existing expiry;
it cannot change account/symbol scope, limits, freshness windows or permissions.
An extension is bounded to five minutes from the native current clock and refuses
unchanged/shorter expiries and stopped/poisoned owners. Existing reservations remain
held. Application polling renews its bounded continuous execution lease.

Backpack `control.account_snapshot_json()` projects the entire authenticated REST
position and open-order reading. It retains external/outside-scope rows, exact signed
quantities, matched original application IDs or null, unknown count, connection epoch
and conservative request-start/completion timestamps. Unknown fields mark coverage
incomplete; private revision changes, pending frames or disconnected owners refuse
the getter. It does not manufacture account facts or perform a request.

Ondo `production_trade_snapshot()` now includes schema version, venue, the real
account traversal start, its completion, and every current open order with a proven
owned application ID or null. Its existing coverage and readiness fields retain
their meaning. `supports_complete_account_snapshots` allows callers to refuse older
installed extensions before connection. Native account traversal starts before the
first order request; the prior snapshot timestamp denoted completion.

Both clients already perform periodic background account reconciliation. Application
configuration requests one-second intervals. Endpoint environments are bound from
the actual native configuration by the account reader, rather than inferred from
instrument names or readiness flags.

## Verification

The native expiry permission-preservation unit test and the Ondo request-start fence
unit test passed. Generated stubs were regenerated with the repository generator.
Windows MSVC import-library notices require the same scoped `CARGO_BUILD_WARNINGS=allow`
exception as the existing wheel builder; no Rust diagnostic lint was weakened.

The installed Backpack control tests passed (2 cases): repeated two-symbol native
round trips, current full-account snapshots, expiry bounds, external order retention
with absent application ownership, native refusal to write against external orders,
and refusal to access/renew a disposed owner. See `pytest-controls-final.xml`.
The synthetic snapshots in this directory contain only the owned protocol peer's
test data and are provided for independent account-reader shape checks.

All 27 Ondo continuous application checks passed against this installed wheel,
including startup/follow-up through the same Strategy and client, two instruments,
confirmed cancel/fill races, market long/short opening and closing, and empty-entry
handling. The combined initial JUnit report (`pytest-native.xml`) retains two failures
in the newly added Backpack test's clock-sensitive `now + 300001 ms` rejection
assertion: native time advanced by a millisecond between constructing and checking
that deadline. The assertion was corrected to a clearly out-of-range deadline and
both cases passed in `pytest-controls-final.xml`. Native code was unchanged for this
correction; the 27 passing Ondo checks were not unnecessarily rerun.

Provenance lists 52 dirty paths: the 8 Rust source changes and 2 changed generated
adapter stubs, plus Windows CRLF rewrites of 42 other generated stubs with no logical
Git diff. The actual change is 195 inserted and 5 removed lines in 10 files.

This is local Windows offline/loopback evidence. It is not a release-profile candidate,
two-platform CI acceptance or proof of mainnet acknowledgements.

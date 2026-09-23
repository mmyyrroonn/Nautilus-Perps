# BTC functional checks — 2026-09-22

## Scope and verdict

**Completed:** BTC-specific native permission, release build, isolated installation, source/
binary identity checks, offline regression, and new-candidate public/private read-only checks.
`production_execution_verified=false`: no real BTC order was submitted by this task.

The user requested that DMS work pause with its current implementation retained, while other
main functionality is checked using BTC. No DMS implementation, release predicate, risk or
reconciliation gate, or full-production acceptance predicate was changed. DMS release semantics
remain unverified; implementation availability is not complete host verification.

BTC public metadata, authenticated read-only lifecycle, and offline order orchestration checks
passed. **BTC real order submission/fill/close acceptance remains unverified.** No production
order, cancellation, DMS arm/release or funding operation was performed. The read-only session
does not establish full account coverage or current flatness.

## Changes

- Added `config/ondo_btc_test.toml`, explicitly selected through `--limits`. The default NVDA
  profile remains unchanged. BTC target USD 15; per-order and gross caps USD 20; 1 opening
  attempt, at most 2 reduce-only closing attempts; 6 app write requests; total 120 seconds,
  including 15 seconds of cleanup; 25 USDC available-margin floor. No authorization is implied.
- Added `tests/test_ondo_btc_trade.py`: 25 offline cases, all with synthetic quotes/events.
  Coverage includes buy/sell cycles, partial and zero fills, rejection details, BTC increments,
  mismatched instruments, widened limits, unknown submissions, bounded unsuccessful closes,
  incomplete-shutdown reporting and credential-free/client-free dry run. Seven additional
  cases construct the actual installed native envelope offline; unlike the mocked app cases,
  these catch a package that still rejects BTC (two expected failures on the old wheel).
- Changed only the exact native instrument allowlist in `production.rs` from NVDA-only to
  NVDA-or-BTC; the frozen plan still permits one instrument per run. Added 16 native cases
  in `tests/production_envelope.rs`, including unrelated instrument and excessive-bound refusal.
- The entire production evidence/DMS/authority suffix is byte-identical to the previous
  candidate source: SHA-256 `b569675d92e2eea7801c47f0542134db068612046a350422246cf7d64b2c84f6`.
  Independent read-only review confirmed exact per-request instrument matching and native
  metadata-grid checks remain enforced. No critical finding was raised.
- Updated `docs/ondo.md` with the paused DMS scope and BTC profile usage.
- No application runtime source changed. Native rebuilding is required for the BTC allowlist;
  the old DMS-observation wheel is retained as rollback material.
- Rebuilt and installed the BTC release wheel in this worktree only; `pyproject.toml` now pins
  its local path and hash. The main repository's environment was not replaced.

## Actual results

| Check | Result |
| --- | --- |
| BTC public metadata, old candidate baseline | Passed, run `20260922T104024325721Z` |
| BTC production read-only, old candidate baseline | Passed, run `20260922T104101572230Z` |
| Targeted BTC/Ondo app regression, before native constructor cases | 467 passed |
| Full app suite, before native constructor cases | 1134 passed, 97 subtests passed, 1 pre-existing warning |
| Installed BTC native constructor regression on old wheel | 2 expected failures, 5 passed |
| Native envelope regression after BTC permission | 35 passed |
| Full Ondo native regression | 1067 passed, 0 failed |
| Native scope verification | Exactly 2 files changed since DMS candidate; DMS/authority suffix unchanged |
| New release build and isolated installation | Passed; build exit 0 |
| New wheel/source/imported binary identity | Passed, 49 native files plus stub/binary checks |
| BTC-specific tests on new installed wheel | 25 passed |
| Full app suite on new installed wheel | 1141 passed, 97 subtests passed, 1 pre-existing warning |
| BTC public metadata, new candidate | Passed, run `20260922T111933182214Z` |
| BTC production read-only, new candidate | Passed, run `20260922T111906146977Z` |
| BTC order intent dry run | No credentials/client/network requests; not live-ready |
| BTC real order / fill / close / cancellation | Not exercised, not verified |
| DMS release host contract | Paused, still unverified |

The public BTC mapping is `BTC-USD.P` to `BTC-USD-PERP.ONDO`; price increment `1`, size increment
`0.0001`, quote USD, settlement USDC. These grids are not evidence of a standalone minimum
order notional. Do not invent a venue minimum from the user budget or the increments.

The baseline read-only session ran 10:41:01.572–10:42:12.183 UTC including startup/cleanup, with a
60-second observation deadline. Same-run native evidence confirms matched identity, login,
`ordersPerps` and `fillsPerps` subscriptions, 3 account-state events, 3 recoveries, no reconnects,
and complete shutdown. `write_capable=false`, `cancel_capable=false`, `dms_armed=false`,
`account_coverage_proven=false`, and `protocol_verified=false` remain explicit.

The new candidate's read-only session ran 11:19:06.146–11:20:16.721 UTC including startup/
cleanup, also with a 60-second observation deadline. It independently confirmed matched
identity, login, both required subscriptions, 2 account-state events, 2 recoveries, no
reconnects, and complete shutdown. Orders submitted by the probe: 0. The same false write,
cancel, DMS-armed, full-account-coverage and full-protocol flags remain explicit.

Release build ran 10:54:46.345–11:16:58.623 UTC (about 22 minutes). The existing process-local
Windows linker warning exception was retained. Maturin warned that `zlib.dll` is resolved from
the local conda installation and is not bundled; this is a locally tested Windows candidate,
not a verified portable wheel for another machine. Installation used normal copies after a
cross-filesystem hardlink warning. Neither warning was silently converted into a portability claim.

The full suite warning is the existing `PytestReturnNotNoneWarning` in
`tests/test_maker_live.py::test_limits`; no new warning was observed in targeted tests.
The initial BTC profile test failed as expected before the profile existed. The first full
BTC-only run exposed a test helper spelling mistake (`load_environment` vs `_load_environment`);
only the new test was corrected, then all targeted and full-suite tests passed. Failed evidence
was retained. The first public preflight was blocked by sandbox networking; the explicit
read-only network-approved retry succeeded. Its failed run was not relabeled as success.

## Reporting and next boundary

Existing trade reports separate actual entry/close confirmation and pre-stop reconciliation
from DMS diagnostics and final clean shutdown. Those component facts can be reviewed without
claiming the entire production session passed. Pausing DMS work does not permit changing a
missing acknowledgement to success or bypassing its existing admission/shutdown logic.

No priced BTC plan or new approval was fabricated. `btc-dry-run.json` has no plan hash and is
not executable authorization. Old consumed hashes and historical reports are untouched.
A real BTC order test requires a fresh exact BTC plan and user-initiated execution. It must
preserve one opening attempt, price/quantity bounds, reduce-only closing and authoritative
same-run reconciliation. Actual exchange acceptance remains unknown until such evidence exists;
the earlier NVDA rejection does not prove or disprove BTC acceptance.

## Evidence

- `public-preflight/meta.json` and its immutable `runs/` captures
- `public-preflight-cli.txt` (network refusal), `public-preflight-network-cli.txt` (successful read)
- `production-readonly/probe.json`, `production-readonly/meta.json`, `production-readonly-cli.txt`
- `btc-red.txt`, `btc-tests.txt`, `targeted-tests.txt`, `app-all-tests.txt`
- `installed-btc-native-red.txt`, `native-envelope-tests.txt`, `native-all-tests.txt`
- `btc-dry-run.json`, `identity-verification.json` (before native changes)
- `native-scope-verification.json`, `verify-candidate.py`
- `candidate-install.txt`, `candidate-verification.txt`, `candidate-identity.json`
- `btc-candidate-tests.txt`, `app-all-tests-candidate.txt`, `btc-candidate-dry-run.json`
- `public-preflight-candidate/meta.json`, `public-preflight-candidate-cli.txt`
- `production-readonly-candidate/probe.json`, `production-readonly-candidate-cli.txt`

Pre-build binary SHA-256: `2f0244a8dc5514118151c66542873dfebe659bb1b1dec9adbdc6333ac1e23769`.
Release artifacts use `20260922-btc-functional-` and `dist-btc-functional` under the existing
`20260919-production-native/build` folder, leaving prior candidate artifacts untouched.

- New wheel SHA-256: `4fe1507cc3882b181fdf2c1b2b99e5c32e1a3875c31554d844bcb95ac4b877bd`.
- New installed/imported binary SHA-256: `bfa5a8cee84f144ed3e2d978dcb6dddd39c3158e71d9fcf23ca070d422a99300`.
- Stub SHA-256 remains `9251ed9a23adeb8f16f5cc7766f3c9dc2a2404bb0ec84a435edaa2cd46aa3972`.

No commit or push was performed; pre-existing dirty changes were preserved.

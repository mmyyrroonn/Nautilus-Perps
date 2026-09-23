# Ondo freshness-diagnostics candidate — 2026-09-23

## Verdict

A new native candidate is built, installed in the isolated worktree, and offline verified. It
fixes the two defects that stopped the 2026-09-22 BTC attempt **before its order reached the
venue**, and it makes the production refusal name the condition that failed.

After that verification, the operator authorized a fresh bounded BTC attempt (`上主网 buy`). **The
live entry and close are proven**: the entry was accepted by the venue and filled, the reduce-only
close filled, and the position is flat. The run still exited 1 **only** because the DMS release was
not acknowledged at shutdown, so `production_execution_verified` remains false (the full predicate
includes a clean final shutdown). See
`../btc-operator/20260923T031758387417Z-240158e4b603/acceptance.md` for the run itself.

## What the 2026-09-22 BTC attempt actually hit

The attempt logged in, saw the account flat with 50 USDC, and submitted one entry. The entry was
refused **inside this process** by the native production gate
(`production.rs::authorize_post`) with the conflated sentence
`production native readiness, identity, journal or protection is unverified`. The refusal was a
fail-closed conjunction of seven conditions and named none of them, so the failing condition could
not be told from the report.

Reading the code against the run's own timestamps narrowed it to the account-reading freshness
condition: `evidence.ready` required the last reconciliation reading to be at most five seconds
old **measured from `AccountReading::read_at`**, which is stamped when a pass *begins*. A pass makes
several rate-limited reads (the shared REST budget is one request per second), so that window was
shorter than one reconciliation cycle: the gate was satisfiable only for a fraction of a second
after each pass concluded, and the strategy's submit - which waits for its own fresh quote - landed
outside it. `accounting.submitted` shows one submission attempt, `fills` is empty, and the pre-stop
reconciliation shows a flat account: nothing rested at the venue.

A separate, environment-level defect was found while re-verifying the candidate: this host's clock
is free-running (`w32tm` source is the local CMOS clock) and ran about one second **ahead** of the
venue. The adapter's first signed request is the one that teaches it the venue's `Date` header, and
the venue rejects a future timestamp far sooner than its documented 30-second window. Four
consecutive read-only production runs therefore reported `identity_match=unknown`, which in
production trading mode is fatal (`connect` refuses an unverified identity). This was reproduced
with native logging enabled for one bounded read-only run and is fixed below.

## Changes (all isolated in `E:\Nautilus-Perps\.worktrees\ondo-btc-native`)

1. **The refusal names its conditions.** `unverified_production_conditions` returns a fixed label
   vocabulary (`identity`, `ready`, `identity_matched`, `journal`, `dms`, `unknown`, `deadline`)
   and `authorize_post` appends it to the same refusal. The decision itself is unchanged: the same
   conjunction of the same conditions, evaluated at the same point.
2. **Reading freshness is measured from the pass's conclusion.** `ReconciliationMachine` now
   records `last_concluded_at` in `conclude_pass` and exposes `reading_is_fresh(now)` with a
   15-second window (`ONDO_PRODUCTION_READING_FRESHNESS_NANOS`). A failed pass never moves the
   instant, so a reconciliation that stalls still closes new risk within one window. The
   `read_at` field is untouched: it keeps its funding-window meaning.
3. **The identity read tolerates the one rejection that teaches the clock.**
   `read_authenticated_account` retries `GET /v1/account` exactly once when, and only when, the
   venue answers `OndoAuthFailure::TimestampTooFar`; the rejection itself carries the `Date` header
   the retry signs with. Every other answer, including a second clock rejection, is returned
   unchanged. This is a read, and it is bounded to one retry - never a loop.
4. **The execution test harness can script a `Date` header** (`Reply::with_header`), which is what
   makes the clock retry testable against a real offset.

No DMS release predicate, DMS state machine, risk bound or reconciliation judgment was changed.

## Verification

| Check | Result |
| --- | --- |
| Native Ondo suite | 1073 passed, 0 failed (unit, integration and doc tests) |
| New native tests | production gate labels; the gate's own refusal through `authorize_post`; concluded-pass freshness; failed pass does not refresh; the clock-rejected identity read retried once with the learned offset |
| `cargo +nightly fmt -p nautilus-ondo -- --check` | clean |
| Application suite on the installed candidate | 1183 passed, 97 subtests passed, one pre-existing warning |
| Launcher suite | 42 passed, offline, private dispatch mocked |
| Candidate identity, wheel/binary/stub, 49 frozen source files | verified (`verify-candidate.py`) |
| Production public preflight | passed (BTC) |
| Production private read-only | passed: identity `matched`, login accepted, `fillsPerps`/`ordersPerps` acknowledged, 3 account-state events, 0 reconnects, clean native shutdown, zero writes |
| Clock-retry observation | the read-only run's native log shows the two `Date` observations 0.2–0.7 s apart (first attempt rejected, retry signed with the learned offset) and no unresolved identity warning |
| Production execution | **live entry and close filled** on 2026-09-23 (`915af89c...`); run exited 1 on the DMS release acknowledgement only |

Read-only runs with the **previous** candidate reported `identity_match=unknown` in 4/4 attempts;
with this candidate, `identity_match=matched` in 4/4 runs, with
`production_readonly_support_verified=true`.

Evidence for the identity defect and its fix:

- `production-readonly/`, `production-readonly-btc/`, `readonly-r1/`, `readonly-r2/`, `readonly-r3/`:
  five bounded read-only runs on the intermediate wheel (`3db8edf7...`, before the clock retry).
  Four of them reported `identity_match=unknown`; one reported a session runtime error. The first
  (`production-readonly/`) used the default symbol set, the rest `--symbols BTC`; the identity read
  is independent of the symbol set.
- `production-readonly-final/`: the bounded read-only run on the final wheel (`8f20735b...`),
  `identity_match=matched`, `production_readonly_support_verified=true`, zero writes, clean native
  shutdown. Three further runs of the same binary (with native logging enabled) were made to
  confirm the retry signature; they are not retained.
- One bounded read-only run with native logging enabled (in a temporary, since-deleted copy of
  `ondo_probe.py`) captured the venue's own answer verbatim: HTTP 401
  `timestamp_too_far`, with the venue's clock 1038 ms behind this host's timestamp. That log was
  deleted after extracting the classification; no raw private frame or credential was retained.

## Candidate identity

- Wheel SHA-256: `8f20735b299ef204464d8a104ca9fdbcec92a5b20d1a5795f474818d56e12985`
  (`dist-freshness-final`, built 2026-09-23 02:xx UTC, exit 0)
- Installed/imported binary SHA-256: `708450d8dabe55ec098c5fed510619a1cc6d25126c02bfbdc8558b0a12f4dd8f`
- Stub SHA-256 (unchanged): `9251ed9a23adeb8f16f5cc7766f3c9dc2a2404bb0ec84a435edaa2cd46aa3972`
- Candidate identity SHA-256: `1a8441b650857b0d8a2972cd36def69e37d856b085c33085c580e43a93d67211`
- Native source manifest: `../20260919-production-native/build/20260923-freshness-final-release-source.json` (49 files)
- DMS/authority suffix SHA-256: `8bcbab81e6d628fe28831276a3b0aad4008312eb4b1a3d45cc0b4e019f277253`
  (previous candidate: `b569675d92e2eea7801c47f0542134db068612046a350422246cf7d64b2c84f6`).
  The suffix **changed by design** with change 1; unlike the previous candidate's checker, this
  candidate's checker records the new value and the change list instead of asserting a frozen one.
- An intermediate build (`dist-freshness`, wheel `3db8edf7...`) contains changes 1, 2 and 4 but not
  the clock retry (change 3). It was never dispatched and is retained only as build evidence.

The release build used the existing process-local `CARGO_BUILD_WARNINGS=allow` Windows
linker-warning exception and the existing cargo caches; no global toolchain setting was changed.
The resulting module still resolves `zlib.dll` from the local conda installation, so portability to
another machine remains unverified.

## Remaining gates

- **DMS release host contract**: still unverified. The 2026-09-22 attempt sent the release frame,
  received no `unsubscribed` acknowledgement and no `update` on the DMS channel, and timed out;
  `switch_released=false` is what makes a shutdown incomplete. The existing release diagnostics do
  not show frames the adapter could not classify, so the next step (if the user wants it) is a
  bounded frame-kind observation, not a semantic guess.
- **Production execution**: requires the exact current-turn authorization and a fresh reviewed
  plan; the previous attempt's claim is still present and blocks the launcher until it is
  deliberately superseded.
- **Host clock**: the machine's clock is free-running and about one second ahead of the venue. The
  retry makes the first signed request survive it; an offset beyond the venue's tolerance would
  still stop signing (fail-closed), and fixing the host clock needs administrator rights this
  session does not have.

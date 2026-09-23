# DMS observation candidate — 2026-09-22

## Status

Native release build, isolated installation, source/binary identity verification, offline tests,
and production public/private read-only checks are complete.
`production_execution_verified=false`. No new production order, cancel, DMS arm/release,
or funding operation has been executed by this task. Previously consumed plans are unchanged.

## Implemented and bounded

- Preserve the existing rejection-reason fix and 15-second native production stop budget.
- Publish same-run DMS release observations separately from authoritative account proof:
  attempted, transport write completed, acknowledgement accepted, fixed failure stage, and
  allowlisted update shapes. Never retain raw private frames or arbitrary payload fields.
- Keep ambiguous `update` responses fail-closed, including missing `data`. Fields such as
  `disabled`, `success`, or timeout zero are observations, not protocol semantics.
- Require both a successful release write and its accepted ACK, in either arrival order.
  Connection loss, send failure, timeout, and abandoned shutdown cannot commit an early ACK.
- Serialize arm/renew against release using the pending-operation state lock. A permanent
  release-start latch prevents retry, renewal, or reconnect/rearm after release begins.
  A pending renewal without its actual ACK remains unconfirmed and blocks release; a socket
  write alone does not extend the confirmed protection deadline.
- Reserve 15 seconds for application cleanup, instead of 5. The total plan deadline and
  exposure caps are unchanged; entry admission ends earlier. Old plans are not rewritten.

## Verification

| Check | Actual result |
| --- | --- |
| Native library unit tests | 494 passed |
| Private runtime integration tests | 114 passed |
| All Ondo native unit/integration tests | 1051 passed, 0 failed |
| App targeted diagnostics/trade tests before build | 193 passed, 1 PyStrategy cross-thread release warning |
| App full suite before build | 1116 passed, 97 subtests passed, 1 pre-existing return-value warning |
| Cleanup reserve TDD | 1 expected failure at 5 seconds, then 1 passed at 15 seconds |
| New release wheel / isolated installation | Passed; 49 frozen native files, stub and binary verified |
| Full app suite on rebuilt wheel | 1116 passed, 97 subtests passed, 1 pre-existing return-value warning |
| Production public preflight | Passed for NVDA; fresh capture and hashes recorded |
| Production private read-only | Passed; matched identity, login, both subscriptions, clean shutdown |
| Production execution / DMS host semantics | Not verified |

The first app full-suite invocation hit Windows permission errors in pytest's default external
temporary directory: 776 passed and 340 setup errors. Re-running with a new project-local
`--basetemp` passed without a code workaround. Both outputs are retained.

The first rebuilt-wheel run found one obsolete test expectation: a test named "defaults"
actually loaded the canonical configuration and still expected a 5-second reserve. It now
explicitly reads a missing config to test the unchanged fallback default; the separate canonical
config test asserts 15 seconds. The final full suite passed. The failed run is also retained.

Native commands used the existing Windows process-local `CARGO_BUILD_WARNINGS=allow`
linker-warning exception and existing Rust caches. No global toolchain setting was changed.

Native regression coverage includes delayed/missing/wrong-channel release acknowledgements,
ambiguous/missing-data updates, a release that crosses a renewal tick, connection loss during
release, ACK-before-send completion, send failure, absolute deadlines, and redaction.

Independent read-only review found no current wrong-channel ACK, early-ACK/send-failure,
post-release reconnect/rearm, or terminal-reason overwrite defect. It identified a remaining
availability window **before** release starts: cleanup may still renew/reconnect for protection;
if a renewal ACK remains pending when release is requested, the operation is refused safely.
This candidate does not add automatic retries or reinterpret socket-send success as an ACK.
It is not a claim that every teardown timing now completes successfully. Legacy reconciliation
state can also advance on a successful renewal write, while the separate production authority
still requires the actual ACK; production order admission remains fail-closed.

## Remaining contract limit

See [contract-review.md](contract-review.md). The official DMS endpoint describes `update`
with unstructured data, but gives no sufficient release-specific contract. This candidate does
not guess one. The previous live rejection and release failure cannot be reconstructed from
raw frames because those frames were intentionally not retained.

A public or authenticated read-only session can verify connectivity/lifecycle only. It does not
exercise the DMS endpoint, demonstrate complete account coverage, prove a flat account, or
pass the production trading acceptance criteria. Actual trading must be initiated by the user
after resolving the contract and approving a fresh exact plan; no consumed hash is reusable.

## Candidate and live read-only evidence

Release build: 2026-09-22 09:40:27–10:04:41 UTC, exit code 0.

- Wheel SHA-256: `ec589e09cf6e38d2a6d53132807a342c122598390a847391eb34f9e1c886b16d`
- Installed binary SHA-256: `2f0244a8dc5514118151c66542873dfebe659bb1b1dec9adbdc6333ac1e23769`
- Full identity and application source hashes: [candidate-identity.json](candidate-identity.json).
- Isolated worktree `pyproject.toml` points to this wheel. The prior `dist-live-fix` wheel remains
  available for rollback; the main repository's environment was not replaced.
- Installation fell back from hardlinks to normal file copies; identity verification passed.

Public run `20260922T100848634743Z`: [public-preflight/meta.json](public-preflight/meta.json).
Private read-only run `20260922T100830258738Z`: [production-readonly/probe.json](production-readonly/probe.json),
from 10:08:30 to 10:09:40 UTC including startup/cleanup. The 60-second run deadline was enforced.
Same-run native evidence: matched identity, accepted login, `ordersPerps` and `fillsPerps`
acknowledged, 3 account-state events, 0 reconnects, shutdown complete. The report explicitly
has `account_coverage_proven=false` and `protocol_verified=false`; it does not establish flatness.
Write/cancel capabilities were false and DMS was not armed.

[draft-plan.json](draft-plan.json) has a fresh, unconsumed hash:
`0c4ef5638c53e100f6e4d0a0b13f83b881b79252515520a5025585a5407a4391`.
It preserves the previous quantity/exposure caps and 120-second total bound, reserves 15 seconds
for cleanup, and binds this candidate. **Authorization is false, no approved hash is assigned,
and mainnet-test readiness is false.** This is historical price evidence/draft, not a current
executable recommendation. Regenerate from fresh evidence after resolving DMS; do not approve
or run this draft merely because a hash exists.

Three independent public client implementations were examined in
[github-sdk-review.md](github-sdk-review.md). None supplied a verified DMS release lifecycle.
No external SDK was installed, no support message was sent, and no private raw frame was saved.

## Evidence

- `native-unit-tests.txt`, `native-private-runtime-tests.txt`, `native-all-tests.txt`
- `app-tests-before-build.txt`, `app-all-tests-before-build.txt`,
  `app-all-tests-before-build-workspace-temp.txt`
- `cleanup-reserve-red.txt`, `cleanup-reserve-green.txt`
- `candidate-install.txt`, `candidate-verification.txt`, `candidate-dry-run.json`
- `app-all-tests-candidate.txt`, `app-all-tests-candidate-final.txt`
- `public-preflight-cli.txt`, `production-readonly-cli.txt`, `draft-plan-generation.txt`
- `verify-candidate.py`: wheel/install/source identity check, without credentials or networking
- `prepare-plan.py`: fresh public-evidence draft only; authorization reset and no requests sent
- Release build artifacts: `../20260919-production-native/build/20260922-dms-observation-*`

No commit or push has been performed. Existing unrelated changes and old acceptance evidence
have been preserved.

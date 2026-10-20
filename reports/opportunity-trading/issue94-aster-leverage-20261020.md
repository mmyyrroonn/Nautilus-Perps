# Issue 94: Aster leverage admission

Implementation is local to `E:/persarb/worktrees/issue94-aster-leverage`, branch
`fix/issue94-aster-leverage`, based on application
`e2290da685858da8ce11ab323afd2a8082550527`. The main working tree's existing
changes were preserved. Initial validation used uncommitted local changes and
performed no native source change, wheel build, or live financial request.

## Acceptance

- Both candidate creation and each entry send check actual leverage, explicit
  target, the exposure-dependent maximum, and observation age for the Aster leg.
  Missing, stale, failed, ambiguous or mismatched proofs block either-leg sending.
  Targets now default to validated account leverage during initialization;
  manual targets are optional overrides.
- Numeric native Aster error codes and the fixed `leverage_configuration`
  category survive receipt processing, pending terminal evidence, late native
  events after a cache terminal, durable order state and trade-review JSON.
  Raw rejection text is discarded.
- A per-instrument fence persists immediately on configuration rejection.
  Duplicate receipts do not repeat the durable write or advance the fence.
  Successful checks based on a read started after rejection can clear it.
  Both ledger restoration and flat-account restart preserve this fence.
- Tests cover changed maximums at creation and after durability, tier boundaries,
  stale/future data, read failure, cache age, missing targets, mismatched leverage,
  malformed proof, one leg already dispatched, original-ID queries and restoration,
  late diagnostics, and reduce-only cleanup with a leverage block.
- Existing notional, unhedged and native risk limits remain in effect. No leverage
  mutation endpoint is called. Production configuration requires no manually
  populated target table; initialization resolves targets from live account proof,
  not from guesses about the incident.

Configuration and operational semantics:
[Aster leverage admission](../../docs/aster-leverage-admission.md).

## Validation

Interpreter: `E:/persarb/Nautilus-Perps/.venv/Scripts/python.exe` (CPython 3.12.9).
All tests below used synthetic data or existing local fixtures; no mainnet
account proof or trading acceptance is claimed.

1. Opportunity regression selection: **1147 passed in 15.12 seconds**.
   Selected `tests/test_opportunity_*.py`, excluding files matching
   `_native\.py$|_native_`, then explicitly included
   `test_opportunity_native_execution.py` and
   `test_opportunity_native_account_reader.py`.
   JUnit: `E:/persarb/tmp/issue94-opportunity-tests.xml`.
2. After the final late-event diagnostic/idempotence change: **166 passed in
   3.54 seconds** across `test_opportunity_leverage.py`,
   `test_opportunity_live.py`, `test_opportunity_entry_evidence.py`,
   `test_opportunity_native_execution.py`, and `test_opportunity_review.py`.
   JUnit: `E:/persarb/tmp/issue94-final-targeted-tests.xml`.
3. `git diff --check`: passed.
4. Final leverage suite with exact tier-boundary assertions: **36 passed in
   0.29 seconds**.
5. After automatic initialization replaced mandatory manual targets: **238 passed
   in 5.60 seconds**, covering leverage, live state, live flow, live runner, entry
   evidence and restart. Added checks cover automatic startup, optional explicit
   overrides, invalid initial proof, frozen targets after account-setting changes,
   same-ledger restoration and late instrument registration.
   JUnit: `E:/persarb/tmp/issue94-auto-initialization-tests.xml`.
6. Before PR publication, rebased onto `bb443a0` (PR 93) and verified the
   opportunity regression selection plus native routing/account-reader/event
   diagnostics and terminal reliability: **1200 passed in 16.80 seconds**.
   JUnit: `E:/persarb/tmp/issue94-pr-tests.xml`. The new leverage tests are also
   included in the existing pure-Python PR workflow.

## Runtime cost

No new network request occurs on a valid entry's send path. Aster's existing
signed account refresh adds one bulk leverage-bracket GET, with the existing
cache, invalidation and retry/backoff. Cached response objects reuse the parsed
proof. A per-snapshot instrument index avoids scanning all account symbols on
each candidate/send check.
Target initialization checks only unresolved instruments in the account callback.
Normally initialized instruments do not repeat initialization on later account
refreshes or send checks. Initialized targets are saved through the existing
account/entry ledger writes.

Local microbenchmark of `leverage_check`, 20,000 calls per sample, three samples:

| Proof size | Best time per check |
| --- | ---: |
| 1 instrument | 5.954 microseconds |
| 1,000 instruments | 5.935 microseconds |

Measurements: `E:/persarb/tmp/issue94-leverage-timing.json`. These measure the
in-memory check only, not network, persistence, or end-to-end trading latency.
Checks at candidate creation and before each send remain necessary because
proof freshness or rejection state can change while a durable pair waits or
after the first leg is sent.

## Remaining operational boundary

The incident's actual STX leverage and maximum have not been inferred or read
from a live account. Runtime automatically resolves and records targets from
fresh venue evidence unless manually overridden. The new gate covers continuous pair entry; the
optional explicit BTC capability self-test remains separate. Validation of the
changed HTTP endpoint used injected read-only transport responses and the
official API contract, not a live authenticated request.

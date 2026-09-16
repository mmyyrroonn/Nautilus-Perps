# R3.2 mutation counterproofs

Every mutation below was injected into the **uncommitted** R3.2 working tree of
`E:\nautilus_trader` (HEAD `7729e9e`, branch `task/ondo-r3-private-lifecycle`), run, and then
restored **from a byte copy held outside the repository** — never with `git checkout`, which would
have destroyed the uncommitted work. Each restore was confirmed with `sha256sum -c` and printed
`RESTORE_OK`. After all four, `git diff --numstat` reproduced the pre-mutation numbers exactly, so
the tree under acceptance is the tree that was measured first.

Method note: this repository denies warnings for `cargo test` too. A mutation that leaves an unused
binding fails to compile (`error: unused variable: now`) rather than failing the test, which would
have been a false "the test caught it". The first attempt at mutation 1 was discarded for exactly
that reason and redone warning-clean.

## 1. Remove the account-state emission — does a verified balance really reach the cache?

`src/execution.rs`, `publish_account_state`: replaced the
`self.reporter.emitter.emit_account_state(balances, margins, true, now, None)` call with
`let _ = (balances, margins, now);`.

| test | result |
|---|---|
| `private_runtime::test_the_account_state_travels_from_the_emitter_into_the_cache` | **FAILED** — panicked at `tests\private_runtime.rs:2108` |
| `execution::test_a_verified_balance_reaches_the_engine_as_a_nautilus_account_state` | **FAILED** — panicked at `tests\execution.rs:4514` |

F05's core claim — that the balance was internal-only and never reached Nautilus — is closed by a
test that fails when the emission is removed, not by a prose assertion.

## 2. Send the restored ledger to a throwaway — is "no replay after restart" really the journal's doing?

`src/execution.rs`, `load_journal`: `journal.restore(&mut state.fills, …)` →
`journal.restore(&mut Default::default(), …)`, so the journal is read but nothing is put back.

| test | result |
|---|---|
| `private_runtime::test_a_fill_recorded_in_the_checkpoint_is_not_replayed_after_a_restart` | **FAILED** — panicked at `tests\private_runtime.rs:1892` |
| `reconciliation::test_a_journal_round_trips_the_ledger_the_orders_and_the_unsettled_writes` | **ok** |

The second line is the point: the round-trip test keeps passing under the same mutation, so the two
tests measure different things and this counterproof is specific rather than a blanket breakage.

## 3. Suppress the funding gap — is an unreconciled funding total actually reported?

`src/reconciliation.rs`, `judge_funding`: the `FundingReconciliation::Unreconciled(gap)` arm
reduced to `let _ = gap;`.

| test | result |
|---|---|
| `reconciliation::test_a_cumulative_total_that_moved_with_no_payment_record_is_reported_not_booked` | **FAILED** — panicked at `tests\reconciliation.rs:4997` |
| `execution::test_a_funding_read_that_failed_books_nothing_and_is_reported` | **ok** |

Again the surviving test is the discrimination: the *unreadable* path travels through a different
`Finding` variant and is unaffected, so the counterproof isolates the unreconciled-reporting claim.

## 4. Book nothing at all — are the funding tests vacuous?

`src/reconciliation.rs`, `judge_funding`: `self.funding.account_all(reading.funding.iter().cloned())`
→ `self.funding.account_all(std::iter::empty())`.

| test | result |
|---|---|
| `reconciliation::test_only_a_stated_payment_is_accounted_and_a_rate_is_never_multiplied_into_one` | **FAILED** — `tests\reconciliation.rs:4946` |
| `reconciliation::test_a_payment_from_before_the_baseline_is_recorded_but_not_counted_against_it` | **FAILED** — `tests\reconciliation.rs:5038` |
| `execution::test_a_stated_funding_payment_is_accounted_and_a_rate_is_never_multiplied_into_one` | **FAILED** — `tests\execution.rs:4735` |

This one is a **vacuity check**, and it is the reason it was run. A test named "a rate is never
multiplied into one" passes for free if the implementation books nothing at all. Booking nothing
kills three tests, so the funding tests require stated payments to be accounted for and are not
satisfied by an empty implementation.

## 5. Make a degraded journal block — is the "report, do not block" ruling actually pinned?

Run after the `JournalStatus::Degraded` follow-up landed (`+4521/−89`, 855 passed). This mutation
deliberately targets the **policy** rather than the reporting: `src/reconciliation.rs`,
`JournalStatus::permits_new_orders` changed from `!matches!(self, Self::Failed { .. })` to
`!matches!(self, Self::Failed { .. } | Self::Degraded { .. })`, i.e. the opposite of the ruling that a
journal which stopped accepting writes keeps trading.

| test | result |
|---|---|
| `reconciliation::test_a_journal_that_stopped_accepting_writes_is_stated_and_refuses_no_order` | **FAILED** |
| every other test, whole suite | **ok** |

So the ruling is pinned — but by exactly one test, and that test exercises
`JournalStatus::permits_new_orders()` **directly**. It does not travel through admission.

**What this mutation turned up.** `new_risk_refusal()` (`src/reconciliation.rs:2810`) matches
`JournalStatus::Failed` itself rather than asking `permits_new_orders()`, while
`set_journal` (`:2935`, deciding whether to invalidate admissions already issued) asks the helper.
The same policy therefore has **two independent encodings**. They agree today — both exclude only
`Failed` — so behaviour is correct, but an edit to one silently misses the other. This is the same
shape R3.1 rejected when it refused a second flag for "the private session is down": a second
declaration does not add safety, it adds a copy that can disagree with the truth. Folded into the
follow-up as a single-encoding requirement.

It also corrected a claim in the delivery report. The report said the end-to-end test in
`tests/private_runtime.rs` "proves this is a report and not a refusal"; that test does assert
`new_risk_refusal() == None` (`:2050`) and it **passed under this mutation**, because the mutation
never touched the real gate. The admission behaviour is correct; the sentence was wrong about which
code proves it. Reports were checked against the tree rather than accepted, here and at the test
names (the report listed `test_an_absent_journal_is_a_first_run_than_a_failure`; the test is named
`..._rather_than_a_failure`).

## Verified by inspection, not by mutation

- `provides_bulk_position_coverage` returns `false` (`src/execution.rs:3322-3324`).
- `tests/python.rs` has 3 deleted lines and all three are doc-comment prose; no assertion was
  removed or weakened.
- Only one cancel call site exists in `src/execution.rs` (`:3936`, inside the `cancel_order` trait
  impl). Recovery, reconciliation, the unknown-outcome probe and the dead-man's-switch trigger
  contain no cancel call — the module documentation's "never cancelled and never modified" claim
  matches the code.

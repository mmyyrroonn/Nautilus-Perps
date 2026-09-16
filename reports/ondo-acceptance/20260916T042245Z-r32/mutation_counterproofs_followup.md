# R3.2 follow-up — mutation counterproofs

Three mutations against the **uncommitted** follow-up tree of `E:\nautilus_trader`
(HEAD `1895978`, branch `task/ondo-r3-private-lifecycle`). Each was injected, run, and then
restored **from a byte copy held outside the repository** (`/tmp/r3-mut/`, with `SHA256SUMS`) —
never with `git checkout`, which would have destroyed uncommitted work. After every restore the
file was re-hashed (`f862b8cc…` for `src/execution.rs`, `b709e8b7…` for `src/reconciliation.rs`)
and `git diff --numstat` reproduced `76/4`, `27/9`, `251/1`, `19/0`, `64/0` exactly.

## The trap this run re-confirmed

The repository denies warnings for `cargo test` too. Mutation 6's first attempt failed to
**compile** — `error: variable 'page' is assigned to, but never used` — because deleting the early
break left its counter write-only. That is not the test catching the mutation, and it is not the
code being wrong either: it is the build refusing to produce a binary. The mutation was redone
warning-clean as `let _ = (page, before_the_window);` and re-run. Only the second result is
evidence.

## 6. Remove the funding walk's bound — does the read really end at the baseline?

`src/execution.rs`, `read_funding`: the early break replaced with `let _ = (page, before_the_window);`,
so the walk follows `nextCursor` until the history ends or [`REPORT_MAX_PAGES`] is reached — the
behaviour before the bound existed.

Run: `cargo +1.98.0 test -p nautilus-ondo --locked --offline --test execution -- funding`

| test | result |
|---|---|
| `test_a_funding_history_deeper_than_the_page_cap_is_read_without_reaching_it` | **FAILED** |
| `test_a_funding_walk_stops_on_the_first_page_that_lies_before_the_baseline` | **FAILED** |
| `test_an_empty_funding_page_does_not_end_the_walk` | **ok** |
| `test_a_stated_funding_payment_is_accounted_and_a_rate_is_never_multiplied_into_one` | **ok** |
| `test_a_funding_read_that_failed_books_nothing_and_is_reported` | **ok** |

The third line is the discrimination. `test_an_empty_funding_page_does_not_end_the_walk` exercises
the same walk over the same fixture shape and **survives**, because an unbounded walk also follows
an empty page. So the two failures measure the bound and not "the funding tests break whenever this
function changes". `test result: FAILED. 3 passed; 2 failed` — no other test in the target moved.

## 7. Remove the watermark restore — does a restart really keep the coverage?

`src/execution.rs`, `load_journal`: the `if let Some(watermark) = journal.watermark() { state.observe_fill(watermark); }`
block deleted. The line that builds `JournalStatus::Restored.watermark` still calls
`journal.watermark()`, so nothing becomes unused and the mutation compiles clean.

Run: `cargo +1.98.0 test -p nautilus-ondo --locked --offline --test private_runtime`

| test | result |
|---|---|
| `test_a_fill_recorded_in_the_checkpoint_is_not_replayed_after_a_restart` | **FAILED** |
| the other 22 tests of the target, including both other journal tests | **ok** |

`test result: FAILED. 22 passed; 1 failed`. The failure is the new assertion at the end of the test:
the restart rewrites the checkpoint with no coverage at all, so the claim that the watermark
survives a restart fails exactly where the doc says it holds. The 22 survivors include
`test_a_run_whose_journal_cannot_be_restored_refuses_new_risk` and
`test_a_run_with_no_journal_path_says_so_and_keeps_reading_the_account`, so this counterproof is
about the watermark and not about journal restore in general.

## 8. Restore the second encoding — is the single-encoding ruling pinned?

`src/reconciliation.rs`, `JournalStatus::permits_new_orders`: `self.new_risk_refusal().is_none()`
replaced with a **second** match over the same variants,
`!matches!(self, Self::Failed { .. } | Self::Degraded { .. })` — i.e. the shape mutation 5 found and
this follow-up removed.

Run: `cargo +1.98.0 test -p nautilus-ondo --locked --offline --test reconciliation`

| test | result |
|---|---|
| `test_a_journal_status_permits_exactly_what_it_raises_no_refusal_for::case_3_degraded` | **FAILED** — `crates\adapters\ondo\tests\reconciliation.rs:4747` |
| `…::case_0_failed`, `…::case_1_restored`, `…::case_2_not_configured` | **ok** |
| `test_a_journal_that_stopped_accepting_writes_is_stated_and_refuses_no_order` | **FAILED** — `tests\reconciliation.rs:4657` |
| the other 105 tests | **ok** |

`test result: FAILED. 105 passed; 2 failed`. The three surviving cases are the specificity evidence:
a second match agrees with the single one on `Failed`, `Restored` and `NotConfigured`, and diverges
only on `Degraded`. So the new test is not a blanket veto over the enum — it isolates the one
variant where two encodings can disagree, which is the variant the disagreement was found on.
`case_3` fails on its **second** assertion (`permits_new_orders`) while its first
(`new_risk_refusal().is_none()`) still passes, which is what "the two came apart" looks like when
the single encoding is gone.

## Why mutations 3 and 4 were not re-run

The follow-up does not touch `judge_funding`, `FundingLedger::account_all` or
`FundingLedger::accounted_since_baseline` — the `src/reconciliation.rs` hunks are confined to
`JournalStatus::new_risk_refusal` / `permits_new_orders` and `ReconciliationMachine::new_risk_refusal`
(`git diff` hunk membership). Mutation 3's test and two of mutation 4's three are unit tests that
call `judge_funding` directly, with no HTTP read path in between, so neither can be affected.

The one mutation-4 test that does travel through `read_funding` —
`execution::test_a_stated_funding_payment_is_accounted_and_a_rate_is_never_multiplied_into_one` —
runs both of its passes at `UnixNanos::default()`, so the baseline instant is `0` and every fixture
payment is at or after it. The bound cannot bind there, which is confirmed by mutation 6 above:
that same test **passed** with the bound removed.

## Verified by inspection, not by mutation

- The follow-up's five files are all under `crates/adapters/ondo/`; `Cargo.toml`, `Cargo.lock`,
  every other crate and CI are untouched (`git status --porcelain`, non-`.pyi`).
- Suite deltas against the pre-follow-up `855`: `tests/execution.rs` 68 → 71 (the three funding
  tests), `tests/reconciliation.rs` 103 → 107 (the four `rstest` cases), and the other seven
  targets count-identical — so nothing was deleted or weakened to make room.

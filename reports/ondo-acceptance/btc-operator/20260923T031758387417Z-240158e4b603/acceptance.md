# Ondo BTC live attempt — 2026-09-23

## Verdict

**The live BTC open and close are proven.** One entry order was submitted, accepted by the venue
and filled; the contingent reduce-only close was submitted and filled; the position is flat and
two independent reconciliations agree. The process still exited 1, and **only** because the
dead-man's-switch release acknowledgement was not confirmed at shutdown.

This is the first successful live entry on this venue through this adapter. The two previous NVDA
attempts never filled and the 2026-09-22 BTC attempt never reached the venue.

## Authorization

The operator said `上主网 buy` in the current conversation. Dispatch used the launcher's
policy-confirmed mode (`-Execute -SupersedeClaim -OperatorConfirmedPolicy`), which records:

- `authorization.source = chat_operator_confirmation`
- `authorization.user_phrase_上主网_present = true`
- `authorization.confirmed_policy` — the instrument, side and the whole
  `config/ondo_btc_test.toml` envelope the authorization covered
- `authorization.supersedes` — the 2026-09-22 claim, archived (not deleted) into its own run
  directory as `MAINNET_ATTEMPT_CLAIMED.superseded-20260923T031758387417Z-240158e4b603.json`

- Approved plan SHA-256: `915af89c6e2012d9d18e611f34f2ac90c1753b1a48f69f9e0ef702f15f8376cb`
- Candidate wheel SHA-256: `8f20735b299ef204464d8a104ca9fdbcec92a5b20d1a5795f474818d56e12985`
- Installed binary SHA-256: `708450d8dabe55ec098c5fed510619a1cc6d25126c02bfbdc8558b0a12f4dd8f`
- Run: `20260923T031802285729Z`

## What happened

| Fact | Observed |
| --- | --- |
| Outcome / terminal reason | `filled` / `the confirmed position is flat` |
| Readiness | identity `matched`, DMS verified, native ready, available margin 50 USDC, flat start |
| Entry | buy `0.0001` BTC, IOC limit, worst price `86681`; **filled `0.0001` @ `86533`** |
| Close | reduce-only sell `0.0001` BTC, IOC limit, worst price `86330`; **filled `0.0001` @ `86530`**, one attempt |
| Fills | 2 (entry `78f334e0a5693f5b27ddf793de4081a1`, close `a4c08be31f98cd5005c8e78b43251c52`) |
| Net position | `0` |
| Requests | 2 app-visible orders, 0 cancels, 6-request budget not approached |
| Pre-stop reconciliation | available, complete, fresh, `reconciled_flat=true`, position `0`, own/foreign open orders `0`, unknown submissions `0`, late fills `0` |
| Post-run read-only check | `20260923-freshness/post-trade-readonly`: identity matched, login and both subscriptions acknowledged, clean native shutdown, zero writes |
| Exit code | `1` — the DMS release was not acknowledged |

The entry and close prices differ by 3 USD/BTC (about 0.0003 USD on this size) before fees.

## Why the exit code is still 1

`dms_release` reports `attempted=true`, `frame_sent=true`, `acknowledged=false`,
`outcome=shutdown_timeout`, with **zero** `update` frames observed on the switch channel before or
after the release. The native stop therefore leaves `released_switch=false`, the shutdown record
is not clean, no *final* snapshot is produced (`final_reconciliation.available=false`; the
available snapshot is phase `reconciled`), and the app's disconnect raises
`Failed to disconnect execution clients: the Ondo shutdown left work unresolved`.

This is the same host-contract gap the 2026-09-22 attempt hit. It is a **shutdown-completeness**
failure, not a trade failure: `entry_confirmed`, `close_confirmed`, `account_flat`,
`local_sequencer_flat`, `own_open_orders_zero` and `no_unknown_submission` are all true, and the
venue-side account is flat per the pre-stop reconciliation and the independent post-run read-only
check.

The venue-side timer is the safety fallback: an unreleased switch fires after its timeout and
cancels resting orders. It does not close positions, and there were no resting orders left.

## Fees and economic result

`fees=unknown` and `fees_known=false`: a fee is only recorded when the native **final** snapshot
carries one, and that snapshot does not exist for this run. No fee, cost or profit is invented
here. The account is flat; the exact balance change is not published by these reports.

## Not verified

- DMS release acknowledgement semantics against the real host (unchanged from 2026-09-22).
- Any economic result: fees, funding and the balance delta are unknown.
- The full production acceptance predicate (`production_execution_verified=false`), because it
  includes a clean final shutdown.

## Evidence

- `trade/trade.json`, `trade/meta.json`, `trade/runs/20260923T031802285729Z/`
- `approved-plan.json`, `draft-plan.json`, `public/`
- `journal.json`, `journal.json.production-run`
- `operator-result.json`
- Archived previous claim: `../20260922T115835918127Z-6f5a48b3d7bc/MAINNET_ATTEMPT_CLAIMED.superseded-20260923T031758387417Z-240158e4b603.json`
- Post-run read-only check: `../../20260923-freshness/post-trade-readonly/probe.json`

Credentials, account id and raw private frames were not published; the reports carry counters and
fixed labels only.

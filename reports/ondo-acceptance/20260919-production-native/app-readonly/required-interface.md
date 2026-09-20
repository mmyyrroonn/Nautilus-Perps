# App-required native interface for production read-only

Status: **confirmed and integrated** against `native-readonly/interface.md` (N2 frozen
contract). The app passes `diagnostics_run_id` through `OndoExecutionClientConfig`, reads the
same factory instance after stop, and accepts only the frozen nine-key snapshot shape.

Owner of this contract: app probe (`src/ondo_native_diagnostics.py`,
`src/ondo_probe.py`). Owner of the native side: native-readonly task N1.

## 1. Diagnostics snapshot accessor

The app reads the snapshot from the **execution factory instance** it built the client
with, via a zero-argument method or a mapping attribute named exactly:

```
read_only_snapshot() -> Mapping[str, object]
```

Requirements the app relies on:

- **Per run / per client.** The snapshot carries the run token the app minted. The app
  rejects and does not merge any snapshot whose token is absent or different, so a stale
  snapshot from another run can never be read as this run's recovery.
- **Bounded and immutable from Python.** Counters and fixed labels only; not a live global
  that grows. The app reads it after the stop.
- **No payload.** It must not carry frame bytes, a credential, a key id, an account id, an
  order id, a balance or any monetary field. The app renders only what it normalizes, but
  the snapshot itself must not contain them.
- **Fail closed.** If the accessor is absent (the installed wheel), raises, or returns a
  non-mapping, the app reports the whole snapshot unavailable.

### Required keys (app vocabulary)

| Key | Type | Meaning | Unknown when |
|---|---|---|---|
| `run_id` | `str` | The token the app passed for this run | absent/empty → not merged |
| `logged_in` | `bool` | Private WS login accepted | not exact bool |
| `subscriptions_acked` | `{label: bool}` | Exactly `ordersPerps` and `fillsPerps`; venue-acknowledged private channels | wrong type or wrong keys |
| `run_state` | `str` | Native run state enum label (e.g. `read_only_synced`) | not in the fixed vocabulary |
| `reconnects` | `int >= 0` | Reconnect count observed this run | negative/non-int |
| `recoveries` | `int >= 0` | Recovery count observed this run | negative/non-int |
| `account_state_events` | `int >= 0` | Verified account-state events published | negative/non-int |
| `identity_match` | `"matched" \| "mismatch" \| "unknown"` | Configured account vs authenticated identity | other value |
| `shutdown_status` | `str` | Owned shutdown status enum label | not in the fixed vocabulary |

Every string field is checked against an explicit allowlist (`RUN_STATE_VALUES`,
`SHUTDOWN_STATUS_VALUES`, `REPORT_CHANNEL_LABELS` in `src/ondo_native_diagnostics.py`); an
unrecognized value is dropped to `null`, never echoed, so the snapshot cannot smuggle a key,
account id or frame text into the report through a label. The run-state spellings are the
adapter's own (`stream.rs::PrivateRunState::as_str`); the channel labels are the two report
channels (`ordersPerps`, `fillsPerps` — the dead-man-switch channel is deliberately absent).
The shutdown-status vocabulary is the app's declared set and must be confirmed in
`interface.md`; until then an unrecognized status is dropped rather than echoed.

Any key the native side cannot supply is omitted and stays `null`; it must **not** be
defaulted to a positive.

`schema_confirmed` is true only when the mapping has exactly these nine keys and every value
has its frozen safe type. Extra keys, missing keys, wrong types and unknown enum labels keep it
false; unrecognized content is never copied into the report.

## 2. Run token

The app passes its `run_id` to the snapshot read (`read_native_diagnostics(target,
run_id=...)`) and requires the snapshot to echo it under `run_id`. If the native side cannot
echo a token, the app reports the snapshot unavailable rather than risk cross-run state.
The confirmed handoff is `OndoExecutionClientConfig(diagnostics_run_id=run_id)`. The native
factory's zero-argument `read_only_snapshot()` returns that bound token. Missing, empty and
cross-run tokens remain unavailable and are not merged.

## 2.1 Production read-only verdict

`production_readonly_support_verified` becomes true only for a successful bounded session
whose same-run snapshot has `schema_confirmed=true`, `identity_match=matched`, `logged_in=true`,
both required subscriptions acknowledged, `account_state_events > 0`, and
`shutdown_status=complete`. Every missing, unsupported or unknown fact keeps the verdict false.

## 3. Account identity field

The app needs an explicit expected-identity input, not a guessed prefix rule:

- Native config field: `expected_venue_account_id: Option<String>` (as named in N1's
  progress).
- **`ONDO_MAINNET_ACCOUNT_ID` is the venue's raw `accountID`, not a Nautilus `AccountId`.**
  The app validates it only as non-empty, bounded, and free of whitespace/control
  characters; it does **not** require it to parse as a Nautilus `AccountId`.
- The app passes the raw value **verbatim** to `expected_venue_account_id` (an existing
  `ONDO-` prefix is neither stripped nor added) and constructs the separate Nautilus
  `AccountId` the config requires as **`ONDO-{raw}`**, documented in `src/ondo_probe.py`
  (`PRODUCTION_ACCOUNT_ID_PREFIX`).
- Sandbox modes are unchanged: `ONDO_SANDBOX_ACCOUNT_ID` is the Nautilus `AccountId`, passed
  through as before, with no expected-venue id.
- The adapter compares the raw expected id to the documented `GET /v1/account` account
  member and reports `matched` / `mismatch` / `unknown`. Absence of a verified identity is
  never `matched`; a `mismatch` fails the run's production-support verdict.

## 4. What the app does not do

- It does not sign anything, open a second HTTP client, or retry a submission.
- It does not construct a writing client in production-readonly.
- It does not infer support from a version, wheel filename, worktree path or commit.
- It does not read `.env` for a dry run or for `public`/`paper`.

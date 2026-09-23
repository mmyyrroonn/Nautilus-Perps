# Human-operated BTC test

This launcher prepares a fresh plan by default. It does not reuse any previous approved
or consumed hash. The human, not the agent, must start and confirm the live branch.

## Run from an interactive PowerShell terminal

```powershell
powershell -NoProfile -File "E:\Nautilus-Perps\.worktrees\ondo-mainnet-resume\scripts\run_ondo_btc_test.ps1" -Execute -SupersedeClaim
```

1. Choose `buy` or `sell`; there is no default direction.
2. Review the displayed quantity, entry/close price boundaries, budget and warnings.
3. Only if you want this exact live attempt, enter `上主网 <the displayed full SHA256>`.
   This records your local operator confirmation, not an agent's claim of authorization.
   Any other answer, EOF, noninteractive input, or a capture older than 60 seconds stops.
4. Leave the terminal open for the bounded probe to finish. Ctrl+C is passed through
   the shared console; the launcher waits for the probe instead of killing/restarting it.
   Read the printed run's
   `trade/trade.json`. Share that report path, not credentials or private raw frames.

Without `-Execute`, the command only reads public metadata/quotes and writes an
**unapproved** draft. It does not read `.env`, log into the account, send an order or
operate DMS. A harmless preparation example is:

```powershell
powershell -NoProfile -File "E:\Nautilus-Perps\.worktrees\ondo-mainnet-resume\scripts\run_ondo_btc_test.ps1" -Side buy
```

## Two operator-confirmed dispatch modes

Both modes require the operator to say `上主网` in the current conversation or prompt. They
differ in what is bound:

- **Interactive (default, strongest):** the operator types `上主网 <plan SHA256>` locally for
  the exact plan displayed. Use `-Execute` alone.
- **Policy-confirmed:** for a dispatch driven from the conversation, `-Execute
  -OperatorConfirmedPolicy` dispatches one *fresh* plan under the reviewed bounds and records
  `authorization.source=chat_operator_confirmation` plus
  `authorization.confirmed_policy` — the instrument, side and the whole
  `config/ondo_btc_test.toml` envelope the authorization covered. The live prices and the plan
  hash are the market's at dispatch and are bound into the approved plan. This is a weaker
  binding than typing the hash, and it is recorded as such. The two modes cannot be combined,
  and neither one skips the claim, the candidate identity check or the public-capture freshness
  bound.

## One attempt, including unsuccessful starts

Immediately before dispatch, exclusive creation of `MAINNET_ATTEMPT_CLAIMED.json` claims
the attempt. This claim persists after **every** outcome, including a process-start error,
rejection, incomplete shutdown or successful run. It blocks concurrent invocations and
all future `-Execute` calls through this launcher. It is not proof that an order was sent.

There is no auto-retry and no automatic claim deletion. `-SupersedeClaim` is the one way
past a claim, and it is deliberate: it moves the old claim into the old run's directory as
`MAINNET_ATTEMPT_CLAIMED.superseded-<new-run>.json` (evidence is preserved, never deleted)
and records it under the new plan's `authorization.supersedes`. Use it only after reviewing
the recorded run, journal and authoritative account state — any further test needs a
separately reviewed new plan. The claim does not block other tools or account users, so it
is not an account-wide lock.

## Bounds and remaining risks

- Explicit BTC profile: 15 USD entry budget, 20 USD per-order/gross caps, at least
  25 USDC available margin, one opening attempt, at most two opposite reduce-only exits,
  three total orders, six app create/cancel requests, 120-second run deadline with a
  15-second cleanup reserve. These are not a guaranteed maximum loss.
- Quantity is rounded **down** to the published lot size; never raised to meet an assumed
  minimum. The successful preparation below produced 0.0001 BTC, approximately 8.62 USD.
  That is a historical sizing example, not the next run's price or a proven venue minimum.
- The installed candidate, source manifest and new launcher hashes are checked and bound
  into each new plan. After confirmation they are checked again, without repricing the plan.
  The existing native account-identity, flat-start, metadata, quote-age and risk gates remain.
- Only the live child loads the canonical `E:\Nautilus-Perps\.env`; inherited `ONDO_*`
  settings are removed so another shell's account/endpoint cannot silently take precedence.
- DMS code is **unchanged**, not disabled. It can cancel resting orders account-wide if
  its timer expires; it does not flatten positions. Do not run another strategy on the
  same account during this test. Its release acknowledgment remains unverified.
- A bounded exit may fail and leave exposure. `production_execution_verified=false`
  remains false if the full acceptance gates, including final DMS/shutdown, are unmet.
  Inspect confirmed entry/close and authoritative reconciliation separately; public data
  and the prior private read-only check do not prove current account flatness.

## One attempt, including unsuccessful starts

Immediately before dispatch, exclusive creation of `MAINNET_ATTEMPT_CLAIMED.json` claims
the attempt. This claim persists after **every** outcome, including a process-start error,
rejection, incomplete shutdown or successful run. It blocks concurrent invocations and
all future `-Execute` calls through this launcher. It is not proof that an order was sent.

There is no auto-retry, reset option or automatic claim deletion. Do not delete the claim
or bypass the launcher to retry. First review the recorded run, journal and authoritative
account state; any further test needs a separately reviewed new plan. The claim does not
block other tools or account users, so it is not an account-wide lock.

## Validation on 2026-09-22

- New launcher tests: **36 passed**, entirely offline with private dispatch mocked,
  including the Ctrl+C wait-without-kill path.
- Full application suite: **1177 passed, 97 subtests passed**, one existing
  `PytestReturnNotNoneWarning` in `tests/test_maker_live.py::test_limits`.
- Full command: `.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
  --basetemp .pytest-tmp-btc-operator-final`.
- First prepare-only attempt was blocked by sandbox networking, retained at
  `20260922T114935069873Z-311102b40769/public` (incomplete; no fallback to stale data).
- Public-only retry after network approval succeeded at
  `20260922T115017803821Z-3c6072fb85a9/public`; draft SHA256
  `0935b1881fd88595d57d8e32fd9d4ac74b714aa3a653b829be63c3a24d9a8b87`.
  That draft is **unapproved and expired**; the launcher always generates a new one.
  This capture preceded the launcher-only Ctrl+C fix; the final launcher and unchanged
  candidate identity were checked offline again after that fix.
- No live branch was run by the agent. No live attempt claim, approved plan, private
  session, DMS operation or order was created by this validation.
- Existing BTC wheel and DMS implementation were not changed by this launcher task.

## Validation on 2026-09-23 (claim supersession and policy confirmation)

- Launcher tests: **42 passed**, entirely offline with private dispatch mocked: the
  2026-09-22 cases plus the claim-supersession path (the old claim is archived, never
  deleted), the chat hash confirmation, the policy confirmation, and the refusal to
  combine the two.
- Full application suite on the 2026-09-23 freshness-diagnostics candidate:
  **1183 passed, 97 subtests passed**, one existing `PytestReturnNotNoneWarning` in
  `tests/test_maker_live.py::test_limits`.
- The previous attempt's claim is still present and unconsumed: no new claim was created
  by this validation, and no live branch was run.

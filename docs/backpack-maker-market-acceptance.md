# Maker and Market roundtrip launcher

The finite BTC perpetual sequence is a Maker post-only limit buy followed by a
reduce-only Market IOC sell, then a Market IOC buy followed by a reduce-only
Market IOC sell. All financial mutations use Nautilus Strategy and native execution.
Independent REST cleanup and its handoff tools are not included in this PR.

The existing native candidate lock does not export the new production API.
Install and verify a source-bound wheel from the accompanying native production
changes using [native-candidate.md](native-candidate.md)'s local-override workflow.
This application change does not replace the published candidate lock.

## Launching

The launchers resolve the application root from their own file location, generate
fresh public sizing and create a unique local report directory. Use the application
interpreter and replace path placeholders with local absolute paths. On Linux:

```text
<app-python> scripts/backpack_roundtrip_5u.py --scenario both --fresh-acceptance
<app-python> scripts/backpack_roundtrip_5u.py --scenario both --fresh-acceptance --env-file <absolute-env-path> --execute --confirm-mainnet
```

The first invocation is a dry run: no credentials or native trading node. Financial
execution requires both flags in the second invocation. Choose `--scenario maker`
or `--scenario market` for one group; `both` runs Maker first. Live execution also
requires `--fresh-acceptance` or an existing absolute `--state-dir` selection.

On Windows, the PowerShell wrapper uses the selected local application environment
and credential file:

```powershell
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<absolute-app-path>\scripts\backpack_roundtrip_5u.ps1" -Scenario both -FreshAcceptance -DryRun
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "<absolute-app-path>\scripts\backpack_roundtrip_5u.ps1" -Scenario both -FreshAcceptance
```

The PowerShell wrapper preserves its existing live default: it performs execution
when `-DryRun` is absent and supplies the native operator's two execution flags
itself. It uses the application's `.env` by default; add
`-EnvFile "<absolute-env-path>"` to select another local credential file.
The Python launcher defaults to read-only planning and validation and requires
both `--execute` and `--confirm-mainnet` for live mode. It also uses the application's
`.env` by default, or its explicit `--env-file` override.

## Native lifecycle

Fresh acceptance first performs signed GETs requiring a flat account and no open
orders. A failed or unknown preflight submits nothing. It saves a credential-bound
`state/session-baseline.json`; the history lower bound includes five seconds before
preflight and is pinned when native identity is initialized. Restarts preserve that
bound. This padding can include a just-preceding out-of-session fill; see the
[sanitized validation record](backpack-production-acceptance.md) for the known
handoff limitation. Current external orders, unknown fills, position mismatches and
economic conflicts still block native admission. A fresh session cannot bypass
unresolved exposure.

Maker entry posts at the current bid. By default, after five seconds it requests
one cancellation of an accepted owned order, waits for terminal confirmation and
fill reconciliation, then reposts only the remaining quantity at the latest bid.
The entry permits at most 23 replacements within a 120-second total budget.
Reconciliation consumes that budget; final reconciliation can finish later. No
terminal rejection or unknown outcome is retried. Zero fills end the run without
a sell; partial entry fills become the exact confirmed exit quantity.

Every exit is one reduce-only Market IOC order for the confirmed position. Market
requests have no limit price, quote quantity or post-only field. Exit has a separate
ten-second order deadline and never uses Maker repricing. A partial or unfilled
Market exit leaves a reported residual and fails acceptance without replacement.

`--maker-wait-seconds` accepts 5 to 120 seconds, `--maker-reprice-seconds` defaults
to 5 and `--maker-max-reprices` defaults to 23. Set the two repricing options to
zero to disable replacements. Windows uses the corresponding
`-MakerWaitSeconds`, `-MakerRepriceSeconds` and `-MakerMaxReprices` options.
The whole native run is bounded to 600 seconds; the final 15 seconds stop further
roundtrip submissions and request cancellation of an accepted active owned order.
An interruption or connectivity failure can leave exposure requiring review.

Sizing targets 4.5 USDC with further headroom. Prior actual fills plus remaining
quantity valued at the fresh ask must fit the side's 5 USDC submission valuation
cap. A Market execution amount may differ; actual fill amounts, fees and liquidity
are recorded separately. Unexpected economics or liquidity fail acceptance, while
the controller can still close a reconciled entry before reporting that failure.

## Original-state close-only recovery

Select the original persistent state and original owned buy ID; replace the
placeholders from retained local evidence:

```text
<app-python> scripts/backpack_roundtrip_5u.py --scenario market --close-only --state-dir <absolute-original-state> --entry-client-order-id ORIGINAL_ENTRY_ID --max-close-quantity 0.00005 --env-file <absolute-env-path> --execute --confirm-mainnet
```

Close-only enables no new-risk authority. It verifies the original owned buy,
terminal true fills, position origin, confirmed remaining quantity, absence of
outstanding orders and fresh native account/quote evidence. An already-flat owned
position causes no sell. It always closes at Market, including legacy `maker`
selections, and cannot create a fresh session. Unconfirmed submission or cancellation
has a bounded reconciliation wait and retains its original identity.

## Results

Live launchers hold a local session lock to serialize cooperating launchers in the
same application. This does not serialize another application or direct operator
commands. Results retain all canceled attempts, real fills and fees, aggregate
quantities/notionals, terminal evidence, residual positions and shutdown status.
After both groups, successful completion additionally requires a later complete
account/order/fill recovery and `account_reconciled=true`.

Reports, logs and persistent state are local operational artifacts, excluded from
Git. Inspect the saved final result and original state before any repeat. The
[validation record](backpack-production-acceptance.md) distinguishes the four real
reconciled legs from the remaining final-wrapper failure.

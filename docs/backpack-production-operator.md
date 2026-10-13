# Backpack native operator

`scripts/backpack_operator.py` runs finite commands through a registered Nautilus
Strategy and the native data, risk and execution engines. All financial mutations
use the native execution client. Public sizing and signed read-only preflight
helpers do not submit orders. Independent REST cleanup tools are outside this PR
and are not part of the production API.

## Required native dependency

The existing `config/native-candidate.lock.json` is unchanged and does not contain
the new Backpack production API. These commands require a source-bound wheel built
from the accompanying native production changes. An equal package version or the
existing locked wheel is insufficient. Build, install and verify the local wheel
with the explicit local-override workflow in [native-candidate.md](native-candidate.md).
The override requires the native source root, wheel, provenance and wheel SHA-256;
a changed native source requires a new build. This PR does not publish a replacement
cross-platform wheel or update the candidate lock.

## Command input and authority

The default is a dry run: it validates symbols, exact decimal quantities and prices,
flags and configured limits without resolving credentials or constructing a node.
From the application root, replace `<app-python>` with its interpreter:

```text
<app-python> scripts/backpack_operator.py --symbol BTC_USDC_PERP --command-file config/backpack-operator.example.json
```

Financial execution requires both `--execute` and `--confirm-mainnet`. Select an
explicit local credential file with `--env-file <absolute-env-path>`, or provide
`BACKPACK_MAINNET_API_SECRET` and `BACKPACK_MAINNET_API_KEY` in the process environment.
The Ed25519 seed must derive the supplied public key. Credential values and native
exception payloads are not printed.

`--limits` selects the TOML envelope, defaulting to `config/limits.toml`.
Its `[backpack_trade]` acceptance values are 5 USD per order and 5 USD aggregate.
The native outer caps are 100 USD per leg and 500 USD aggregate; CLI limits may
only shrink the configured values. Roundtrip commands independently enforce a
5 USDC submission valuation cap. `--timeout-secs` bounds the complete run to at
most 600 seconds. Provide 1 to 32 sequential commands as a JSON array in
`--command-file`, or repeat `--command`. Repeat `--symbol` for an explicit allowlist.

Supported actions are `submit`, `query`, `cancel`, `cancel-replace`, `roundtrip`
and `close-only`. Quantities and prices are exact decimal strings; flags are JSON
booleans. Limit submits require `price`; Market submits require
`order_type="market"`, `reference_price`, IOC and no limit price or post-only flag.
The reference price is a local valuation input; native admission checks a fresh quote.
Post-only limits require GTC.

`last` refers to the most recent submission in the current run. Across restarts,
select the original client order ID and preserve the same state directory,
credential scope and symbol allowlist. Query returns an asynchronous request plus
a cache observation, labeled `query_requested`; it is not a synchronous venue ACK.
Cancel-replace waits for the old order's terminal evidence and durable actual
economics before submitting a new UUID identity. An unknown outcome never
authorizes an automatic replacement.

## Roundtrips and recovery

The [Maker/Market launcher](backpack-maker-market-acceptance.md) runs Maker limit
entry followed by a reduce-only Market exit, then Market entry followed by a
reduce-only Market exit. Each exit uses only the confirmed filled quantity.
Maker repricing applies only to entry. The native `close-only` action restores
an original owned entry and submits no new entry; see the launcher guide for
a placeholder recovery command.

The operator does not bulk-cancel or adopt external orders. Its default persistent
state is `reports/backpack/operator-state`. Retain this state after failures,
interruptions or residual positions; an initialized state cannot be replaced to
bypass unresolved ownership or economics.

## Results and inspection

Results include native events, actual fill quantities, prices, fees and liquidity,
terminal reconciliation, native cache positions, admission and shutdown evidence.
An accepted resting order can remain open after a submit-only command. Inspect
dirty shutdown, unknown submissions, pending cancellations and position residuals
before continuing; a cache observation alone is not a fresh exchange snapshot.

After the last command, a newer complete account/order-history/fill-history recovery
must finish within the original deadline. `account_reconciled` reports this boundary;
its absence makes `completed=false`. A snapshot or `recovery_count` alone does not
prove complete history recovery. Historical dirty evidence remains persistent.

For native connectivity and admission inspection with mutation authority disabled:

```text
<app-python> scripts/backpack_native_inspect.py --symbol BTC_USDC_PERP --env-file <absolute-env-path> --state-dir <absolute-inspection-state> --output <local-inspection-result.json>
```

Use a separate inspection state for a new connectivity check; recovery must select
the original state. Readiness does not establish a filled roundtrip. See the
[sanitized validation record](backpack-production-acceptance.md) for the observed
mainnet legs and the unresolved final wrapper boundary.

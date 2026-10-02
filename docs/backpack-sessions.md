# Backpack public sessions

Validate the credential-free session plan from `E:\persarb\Nautilus-Perps`:

```powershell
.venv\Scripts\python.exe src/backpack_probe.py --config config/backpack-public.example.toml --dry-run
```

This validates a bounded session and prints JSON. It reads only the named TOML file, does not
import the native adapter, read `.env`, construct a client, open a socket or create output/state
files. Without `--dry-run`, `mode = "public"` starts a bounded native LiveNode;
`replay` and `paper` start their bounded offline native owners. Explicit `account-readonly` starts the native readonly owner. Public runtime integration is tracked
in issue #14. Native recorded replay and offline paper are implemented in issue #15;
see [the offline runner](backpack-offline.md). Readonly account observation is implemented in issue #18; execution remains a separate slice under issue #5.

The exact native symbols are an explicit nonempty allowlist. Each needs four decimal-string
margin/fee inputs and a source (`Configured`, `VenueObserved` or `Synthetic`) with a reference.
The sample deliberately supplies synthetic economics for planning. No source label proves
account verification or grants execution readiness. The native parser still validates market
eligibility, currency facts, tick/size grids and economic ranges before streaming.

Modes are `public`, `account-readonly`, `paper` and `replay`. A successful dry run only
proves configuration validity. Both `paper` and `replay` require `environment = "offline"`
and an explicit recorded-session manifest in `replay_file`; `paper` additionally requires
an explicit synthetic starting balance and per-symbol quantities.
No real-order mode, sandbox assumption, DMS or account mutation is exposed.

Only `account-readonly` accepts and requires an `[account]` section containing `account_id`
(with `BACKPACK-` prefix), `venue_account`, `credential_env`, and optional `subaccount`.
`credential_env` names a variable; never put its secret value in TOML. Account identity is
operator-supplied, not verified by this configuration. Dry-run output omits account identifiers
and the credential reference. It never resolves the reference or opens a dotenv file.

Production URLs are fixed. An explicit `environment = "loopback"` requires both
`base_url_http` and `base_url_ws` to be numeric loopback origins. Remote overrides,
URL credentials, query strings, non-origin paths and DNS aliases are refused.

Output and durable state are scoped by a SHA256 of venue, environment, endpoint pair and exact
account/subaccount identity. Mode separates reports, but does not reset durable order identity;
API-key rotation, symbol changes and new run IDs must not allocate a fresh order namespace.
Keep the configured state root stable across runs. Deleting or rolling it back is not recovery.
Relative paths resolve against the TOML directory; dry-run does not inspect or open replay data.

Durations are bounded to 600 seconds, each request to 60 seconds within that duration,
staleness to 30 seconds, report events to 100,000 and report bytes to 64 MiB. Future runtimes
must enforce these values. The public runner enforces duration, event and total report-byte
limits and records actual observations. Dry-run never claims that a session ran.

## Native public runtime

```powershell
.venv\Scripts\python.exe src/backpack_probe.py --config config/backpack-public.example.toml
```

The runner constructs the public `BackpackInstrumentEconomics`, `BackpackDataClientConfig` and
`BackpackDataClientFactory` facade and registers only that data factory on the normal LiveNode
builder. It subscribes to exact allowlisted instruments, quotes, trades, mark prices and L2
book deltas. It creates no execution client, reads no environment credentials or `.env`, and
makes no private requests. Public production endpoints require no account authorization.

Each run creates a unique directory under the planned output directory containing bounded
`events.jsonl` and `summary.json`. Prices, quantities, sequences and native event/receipt
nanoseconds remain exact strings. Counts include events omitted when a configured report cap
is reached; reaching a cap requests a native stop. A very small byte budget produces an
explicitly truncated summary. No durable order journal or state directory is opened.

Native health records are observed from the same config used by this run's factory. Quote
freshness, book continuity, book freshness and snapshot price coverage are independent fields.
A repeated or stale frame cannot acquire freshness from the report timestamp. Disconnect,
recovery and idle data remain visible in health records even when the transport is connected.
The final summary observes health after shutdown. Subscription acknowledgements remain
unverified, depth coverage is bounded, and the mark stream's funding-unit interpretation is
not projected as a native fractional funding rate. Capability support and supplied economic
provenance grant no execution readiness; every public report keeps `execution_ready = false`.

The duration budget begins immediately before the native hosted run, after local module loading,
identity checks and runtime construction. The summary separately records `preparation_ms`,
`runtime_elapsed_ms` (including shutdown) and total `elapsed_ms`. Network bootstrap and active
observation share the configured duration; local preparation is outside that network budget.

The caller-owned asyncio run captures the native stop handle before starting. Duration limits,
repeated stop and external cancellation use that handle and the native hosted cancellation
path. Startup failures and cancellation still publish sanitized evidence; exception strings,
raw network frames and request bodies are never written. Failed startup is not a successful
observation, and a failure to finish shutdown is reported explicitly.

A normal local run labels its installed native module `unverified_local_development` and records
its binary hash, distribution version, application commit/dirty state, application source hash
and configuration hash. To verify an explicitly pinned candidate, supply all three arguments:

```powershell
.venv\Scripts\python.exe src/backpack_probe.py --config config/backpack-public.example.toml `
  --candidate-wheel <wheel.whl> --candidate-sha256 <sha256> --native-provenance <provenance.json>
```

This reuses `scripts/verify_native_install.py` to check the exact installed binary and wheel
hash against explicit source-bound provenance before runtime construction. It explicitly
requires and compares the installed Backpack adapter stub as well as the default Aster/Ondo
stubs, and records the checked Backpack stub hash. A local import
alone never proves source binding. Application source identity remains observational and is
not promoted to a verified release merely by a candidate wheel check.

`tests/test_backpack_public_native.py` uses the actually installed adapter and real numeric
loopback HTTP/WS servers. It covers instrument-before-data ordering, exact quote/trade/mark
and depth messages, stale BBO, depth gaps, socket replacement, cancellation, repeated stop
and failed metadata bootstrap. It checks that only public GET endpoints and public stream
subscriptions were requested. A skipped test means the required candidate wheel is absent;
it is not acceptance evidence and no Python client double is used as native proof.

## Recorded public acceptance

The [2026-10-02 public observation](../reports/backpack/2026-10-02-public/README.md) records
a source-bound native wheel, actual public data and bounded shutdown, preserving both a failed
bootstrap and a successful retry. Private account, paper and execution acceptance remain separate.

## Native readonly account observation

`account-readonly` uses the actual native execution factory and shares one native REST quota
with the public data factory. The account configuration requires a `BACKPACK-...` engine label,
explicit venue account/subaccount labels and the name of one credential environment value.
These labels remain caller claims; they do not establish authenticated venue identity.

The runtime resolves that named value only when account observation is invoked. If absent from
the process environment, it reads only this application's `.env`, with interpolation disabled.
Dry-run, public sessions and offline modes do not load account credentials. Secrets, signature
headers and raw private frames are not included in reports or errors.

The same CLI accepts an account-readonly configuration and the three candidate-verification
arguments shown above. Import and configuration are read-only; building the native account client
opens its isolated identity directory. The native client rejects submission, cancellation, transfers and borrowing.
Production account observation requires its own explicit operator invocation and authorization;
the implementation acceptance uses only synthetic keys and numeric loopback peers.

Account evidence includes sanitized public/account telemetry plus exact native engine cache
observations. Wallet trading balances do not establish usable margin. Empty orders or positions
are incomplete cache observations and never prove Flat. Real trade IDs, fees and rebates come
from the native reports/engine; the application implements no signer, protocol parser or ledger.
Native enqueue/cache updates are not a durable consumer commit, so pending fill delivery remains
visible and `durable_economic_acknowledgement` is false.

Startup mass reconciliation is disabled because Backpack cannot yet attest complete account
coverage. Native bounded REST/private-stream recovery still operates. Generic subscription
responses, successful transport and completed observation do not imply private subscription
confirmation, complete history, verified identity or execution readiness. Shutdown and output
bounds use the same native owner lifecycle as public observation.

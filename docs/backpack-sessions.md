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

Use the owned numeric-loopback example to validate the configuration without
credentials or sockets:

```powershell
.venv/Scripts/python.exe src/backpack_probe.py --config config/backpack-account-readonly.example.toml --dry-run
```

To run against the owned peer, supply only its explicitly configured local seed
and invoke the same command without `--dry-run`, optionally with all three pinned
candidate arguments. The example does not authorize production observation.
[Source-bound synthetic acceptance](../reports/backpack/2026-10-02-account-loopback/README.md)
records the actual CLI, native cache, signatures and bounded shutdown.

The report preserves `native_health_before_stop` separately from final stopped
health. Unresolved recovery, parse/delivery, private input or subscription
transport failures produce fixed safe failure reasons. Expected identity/ACK /
retention gaps stay unverified; successful recovery can finish an observation
without promoting those gaps to readiness. A published wallet snapshot can
precede failed fill-history pagination and therefore does not prove complete
history. A report cap with dropped events is a failed limited observation.

Cancellation explicitly releases native node, hosted task and observation
closure references, even when an outer caller keeps its cancelled Task alive.
Successful terminal disposal and actual same-namespace reopening demonstrate
socket/identity owner shutdown; no garbage collection is required for release.
The tests use different synthetic credentials and local account/subaccount
labels for separate namespaces. These labels do not supply an additional
subaccount routing protocol or authenticate a venue identity.


## Synthetic loopback order scenario

`src/backpack_loopback.py` is a separate entry point for owned local protocol fixtures.
It uses the installed native Backpack factories and a normal native `Strategy`; orders
pass through native risk and execution engines. Ordinary public, readonly account,
replay and paper routes retain their existing behavior. This entry cannot enable
production writes, connect credentials to remote origins or attest a real account.

Validate the explicit plan without native imports, credential lookup, socket creation,
identity-state creation or report output:

```powershell
.\.venv\Scripts\python.exe src/backpack_loopback.py --config config/backpack-loopback-execution.example.toml --dry-run
```

Actual runtime also requires `--candidate-wheel`, `--candidate-sha256` and
`--native-provenance`; the installed wheel must pass the shared source-binding check.
Start your owned numeric-loopback HTTP and WS peers first, and set only the explicitly
named synthetic credential environment value. This runner never falls back to `.env`.
HTTP and WS must name the same numeric loopback address with paired schemes; separate
local ports are supported. Every instrument economics source must be `Synthetic`.

The sample grants finite new-risk Buy Limit and owned-cancel permissions, exact decimal
quantity/price/notional/margin limits and an explicit timeout. Authority expiry is
anchored once when the native configuration is created and never renewed. Policy flags,
complete initial positions and the local margin model are explicit caller assertions.
Their local assertion timestamp is neither a venue receipt timestamp nor proof of
account identity, private subscription acknowledgement or production readiness.
Native metadata validates products and quantity/price grids before any order bytes.

The bounded scenario submits one Buy Limit. If enabled, it makes one additional native
submission after the public BBO becomes stale while preserving metadata freshness,
account freshness and spare reservation capacity. Before that probe it explicitly
reasserts the fixture's current flat account facts without refreshing market admission.
The peer must withhold every economic update until the probe has been refused. Observed
fills, native pending fills or an open cached position contradict this assertion and
stop the scenario. The application records the native denial reason; a generic
`GuardedLocalRefusal` alone does not identify the cause. Acceptance tests independently
verify the fixture ordering, fresh remaining gates, actual stale public telemetry and
zero additional POST requests.

After a true partial fill the Strategy can request one independently authorized owned
cancel, even when public quotes are stale. The signed test peer sends HTTP 202 and no
terminal order event. Native shutdown's `pending_cancellations` counts Unknown, Pending
and ResponseObserved together; `cancel_unsettled_observed` therefore means unresolved
native cancellation evidence, and does **not** prove the client received 202. Independent
peer evidence of `202 sent` remains a separate observation. `cancel202_observed`
is projected from the actual native current-session `CancelPending` health marker,
which is set only after a matching HTTP 202 receipt. It is independent of the shutdown
count and is sampled again before native disposal.

`scenario_completed` describes observed finite scenario steps plus native unresolved
cancel evidence where requested. It does not mean the execution is settled or that exit
is safe. Full evidence retains `execution_settled=false`, `flat_verified=false`, actual
engine/cache balances, orders, position exposure and fees, pending true fill reports and
the native owner's authoritative sticky shutdown report. A duplicate true trade updates
native economics once, but the fill remains pending: this entry exports no Python
economic acknowledgement callback and implements no ledger. No cache or portfolio
observation releases native capacity or produces a durable economic receipt.

At a report limit the compact summary retains the native shutdown snapshot, actual
pending fill report count, `execution_settled=false`, `flat_verified=false` and an explicit
`exposure_unknown=true`. It cannot preserve every domain object or exact exposure detail;
missing details never mean zero positions or a clean stop. Loopback plans require at least
8 KiB of report budget.

The overall observation status can be `completed` while native shutdown is dirty and a
position remains open. Inspect `scenario_completed`, `native_health.loopback.pending_fills`,
`native_health.loopback.shutdown_report` and the recorded `engine_account_observation`
together. Unknown POST responses are never reported as rejected, resent with a new ID,
or adopted from a matching numeric client ID. They fail the scenario and remain dirty.
A report cap stops new risk before another submit; a once-only owned exit retains its
independent native permission, and its evidence line may be dropped at the cap.

The installed-wheel tests in `tests/test_backpack_loopback_native.py` use publicly known
synthetic signing material and owned local peers. They verify real Strategy events,
engine economics, duplicates, cancel202, Unknown POST and bounded shutdown. Without the
explicit loopback API wheel these tests skip; static configuration checks do not claim
transport or production validation.

The bounded loopback runner explicitly disables native inflight, open-order and position reconciliation timers (`native_inflight_checks_enabled=false`, `continuous_reconciliation=false`). Continuous reconciliation is outside this scenario's accepted capability; generic reconciliation can synthesize terminal events after cancellation timeouts. This restriction does not claim that the independently tracked native Denied-order watchdog issue is repaired. The ordinary readonly account entry keeps its existing engine configuration.

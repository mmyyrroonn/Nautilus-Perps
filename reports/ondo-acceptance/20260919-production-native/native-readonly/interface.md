# Native read-only interface contract (N2)

Source: `E:/nautilus_trader/.worktrees/ondo-production-native`, native Ondo crate and PyO3 bindings.
Status: source contract frozen; new N2 focused/full verification is recorded separately in
`subagent-readonly.md`. This is not host, installed-wheel, or production verification.

## Configuration and run ownership

```python
config = OndoExecutionClientConfig(
    environment=OndoEnvironment.PRODUCTION,
    account_id=AccountId("ONDO-example-venue-id"),
    account_read_only=True,
    expected_venue_account_id="example-venue-id",
    diagnostics_run_id=run_id,
)
factory = OndoExecutionClientFactory()
# Register this factory and config with the native node before running it.
# After stopping the node, read the same factory instance:
snapshot = factory.read_only_snapshot()
```

- `diagnostics_run_id: str | None` is bound from config when the native client is constructed.
  `read_only_snapshot()` takes **zero arguments** and cannot assign or relabel a token.
  Omission yields `run_id == ""`; consumers must reject missing/empty/mismatched tokens.
  Supply a bounded, nonsecret unique token per run. Do not reuse a client for another run.
- `expected_venue_account_id: str | None` is the raw venue `accountID`, copied verbatim;
  it is independent of the required Nautilus `account_id`. The app may construct the latter
  as `ONDO-{raw}`. Native code never strips prefixes or guesses identity.
- Production requires `account_read_only=True`; `allow_production_orders=True` is refused.
  `ONDO_MAINNET_API_KEY`/`ONDO_MAINNET_API_SECRET` and sandbox equivalents remain separate,
  with no environment fallback. Explicit credentials must match their scope/environment.
- Config exposes the run token and expected identity, but no readable key/secret properties.
- Factory clones (including PyO3 extraction used by node registration) share the diagnostic
  handle. Each successful client creation installs a new per-client store. Previous clients
  cannot write into that new store. Independent factory instances have independent stores.
  A failed create leaves the prior snapshot in place; its prior token must be rejected by
  the new run's reader. One factory/client pair per run is the recommended lifecycle.

## Snapshot

`read_only_snapshot() -> dict[str, object] | None`; `None` means no client has been created.
The returned dict is a detached copy. Mutating it cannot mutate native state.

| Key | Exact type | Meaning |
|---|---|---|
| `run_id` | `str` | Token bound at client creation |
| `logged_in` | `bool` | At least one host `loggedIn` acknowledgement in this client's stream |
| `subscriptions_acked` | `dict[str, bool]` | Exactly `ordersPerps` and `fillsPerps`; accepted subscriptions, not sent frames |
| `run_state` | `str` | `disconnected`, `authenticating`, `recovering`, `read_only_synced`, `trading_ready`, `uncertain`, `stopping`, `stopped` |
| `reconnects` | nonnegative `int` | Connection attempts after the initial attempt |
| `recoveries` | nonnegative `int` | Concluded account reconciliation passes, including startup; alone does not prove recovery after a forced disconnect |
| `account_state_events` | nonnegative `int` | Verified native `AccountState` events published |
| `identity_match` | `str` | `matched`, `mismatch`, `unknown` |
| `shutdown_status` | `str` | `not_attempted`, `running`, `stopping`, `complete`, `incomplete` |

The above nine keys are the full Python contract. No frame, credential, venue account/order id,
monetary amount, or raw error text is included. There is no per-channel event count: subscription
acknowledgement does not establish that an order/fill event was observed.

Accepted-login counters and the bounded acknowledged-channel set survive eviction from the
512-record diagnostic ring. An acknowledged unsubscription removes that channel. `logged_in`
is historical evidence for this run, not proof the socket remains connected. New client creation
resets all counters, acknowledgements, run state and owned-shutdown state.

## Identity and shutdown semantics

`matched` requires a configured expected identity and an authenticated `GET /v1/account` answer
whose documented `accountID` equals it. A mismatch makes connect fail. Missing/unreadable identity,
failed reads, or absent expected identity remain `unknown`.

`complete` is the native `OwnedShutdownStatus::Clean` / ordered stop completion;
`incomplete` is `Dirty`. `not_attempted`, `running`, and `stopping` remain distinct and must not
be normalized to clean. Shutdown success does not prove account flatness or production trading.

## Enforcement and limits

Read-only native HTTP dispatch refuses signed POST/DELETE before waiting on rate budget or new-risk
admission. Private WS subscribes only to orders/fills; no DMS arm/renew/release in read-only mode.
Credential/scope environment equality is checked before transport construction. WS derives its scope
from credential environment and mode, checks the official authority (or explicit literal loopback
mock), and refuses production trading. Sandbox trading behavior remains independently supported.

No live credentials, signed remote requests, installation, wheel build, stub generation, or
production order/DMS action form part of this native correction task.

## Direct DMS correction

The readonly guarantee also applies to direct Rust `send_switch_frame` calls: arm, renewal and release are rejected before serialization/socket access. Unrequested DMS subscription acknowledgements cannot arm the runtime or cause a later renewal tick to write. This correction preserves the exact nine-key readonly snapshot contract. Offline wire evidence and the 955-test full result are in `direct-dms-correction.md`; the updated source manifest is `direct-dms-source-sha256.json`.

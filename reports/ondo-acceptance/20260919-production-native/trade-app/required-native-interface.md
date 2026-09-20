# App-required native interface for the restricted production trade

Status: **final app contract; native producer not implemented yet.** The app remains fail-closed
until the installed wheel exposes every item below. This is an interface specification, not
production-write evidence, order authorization, or DMS authorization.

## 1. Capability and execution config

The adapter must expose:

```python
OndoExecutionClientFactory.supports_production_trade_envelope is True
OndoExecutionClientFactory.production_trade_snapshot() -> Mapping | None

OndoExecutionClientConfig(
    ...,
    execution_envelope=OndoExecutionEnvelopeConfig(...),
    expected_venue_account_id="<raw venue accountID>",
    diagnostics_run_id="<app-minted run id>",
    dms_timeout_secs=30,
    reconcile_interval_secs=1,
    allow_production_orders=True,
)
```

The marker must be the exact boolean `True`. Any missing field, a false/non-boolean marker, a
missing snapshot accessor, or a constructor signature that proves a required field is absent makes
the app refuse before reading credentials or building a client.

`expected_venue_account_id` is the raw venue value, passed verbatim. The separate Nautilus
`account_id` is `AccountId("ONDO-<raw>")`. `diagnostics_run_id` binds native state to one app run.
The native config accepts `dms_timeout_secs`; native code derives renewal as
`max(1, timeout_secs // 2)`. The app refuses a plan that claims a different renewal interval.
The app passes `reconcile_interval_secs=1` so the fresh pre-stop proof can complete inside the
five-second cleanup budget. `allow_production_orders=True` is the explicit native opt-in; it is
necessary but never sufficient without the complete envelope and every runtime gate.

Production writes additionally require native `account_read_only=False`, production-write opt-in,
and a complete envelope. Production without the envelope and read-only plus an envelope are named
refusals. The app never passes API credentials into the constructor.

## 2. Exact envelope constructor

The adapter must export a class accepting these exact keyword parameters:

```python
OndoExecutionEnvelopeConfig(
    instrument_id,                    # InstrumentId; only allowed instrument
    entry_side,                       # "buy" or "sell"
    entry_max_quantity,               # exact decimal string
    entry_worst_price,                # exact decimal string; max buy / min sell
    entry_max_notional_usd,           # exact decimal string; approved 10-20 target
    close_side,                       # opposite of entry_side
    close_max_quantity,               # exact decimal string; <= approved entry quantity
    close_worst_price,                # exact decimal string; max buy / min sell
    max_close_attempts,               # int, <= 2
    max_notional_per_order_usd,       # exact decimal string, <= 50
    max_gross_exposure_usd,           # exact decimal string, <= 100
    max_orders,                       # int, <= 3
    max_new_risk_requests,            # int, exactly 1
    max_app_requests,                 # int; creates plus own-order cancels
    min_available_margin_usdc,        # exact decimal string; configured USDC threshold
    entry_deadline_unix_nanos,        # absolute UTC deadline for opening new risk
    cleanup_deadline_unix_nanos,      # absolute UTC deadline for exits/cancels/reconciliation
    require_flat_start,               # exact True
)
```

Every decimal is parsed exactly. Invalid precision, a non-tick quantity/price, inconsistent sides,
or a value above a hard cap is refused, never rounded or clamped. Native must enforce the approved
target values, including `entry_max_notional_usd`, quantity, and both directional price bounds.
Enforcing only the broad USD50 cap is insufficient.

`min_available_margin_usdc` is a same-unit operator threshold, currently 25 USDC for the
USD15 target. It is not an FX conversion, a USD=USDC assertion, or a venue minimum. The native
send gate compares the actual USDC balance only to this immutable USDC threshold. Missing,
malformed or below-threshold values refuse; the USD order-notional checks remain independent.

The official order contract exposes `size`, and market metadata exposes `baseIncrement`; current
NVDA public metadata exposes no separate `minNotional`. A missing standalone venue minimum stays
nullable with explicit `unpublished` provenance and is never defaulted to USD 0 or the user's
USD 10 target. The positive `baseIncrement`/quantity grid and `quoteIncrement`/price grid remain
mandatory. If typed instrument metadata supplies `min_notional`, native/app parse the exact Money
amount, validate its currency, and enforce it; malformed, non-finite, non-positive or wrong-currency
present values are refusals, not downgraded to unpublished.

The first create is the only opening attempt: exact instrument and entry side, limit IOC,
non-reduce-only, at or below the approved quantity/notional, and no worse than
`entry_worst_price`. Later creates must be exact instrument and close side, limit IOC, reduce-only,
and no worse than `close_worst_price`. Confirmed close fills plus currently unsettled close
reservations may not exceed the native-confirmed owned position. A terminal, authoritatively proven
unfilled remainder releases its reservation for the second attempt; an unknown result never
releases capacity. There are at most `max_close_attempts`.

`max_orders` counts all creates. `max_new_risk_requests` counts opening creates and is exactly one.
`max_app_requests` counts every app-originated create and cancel. The app and native layer both
enforce that same counter. This contract deliberately has **no `max_total_requests` claim**: the
app cannot observe native background reads, and inventing one global HTTP budget could consume the
only cleanup reconciliation path. Native transport rate limiting remains separate. Any exhausted
create/cancel budget or cleanup deadline yields `Uncertain`, never a fabricated flat result.

`entry_deadline_unix_nanos` freezes opening risk. `cleanup_deadline_unix_nanos` is later but still
bounded; only reduce-only exits, own-order cancels, and reconciliation may occur in that interval.
Neither deadline can be extended after construction.

A cancel is permitted only for an original client order id created under this
`diagnostics_run_id`; it consumes `max_app_requests`. Native ordered shutdown has the same own-order
restriction. No cancel-all, foreign-order cancel, batch, DMS expiry test, or account-setting write
belongs to this envelope.

## 3. Start snapshot

`production_trade_snapshot()` initially returns `None`. Once this factory's client completes start
reconciliation it returns a bounded mapping:

| Key | Required value |
|---|---|
| `run_id` | exact `diagnostics_run_id` |
| `phase` | `"start"` |
| `generation` | integer >= 1, monotonic within this run |
| `snapshot_unix_nanos` | integer > 0, time start reconciliation completed |
| `identity_match` | `"matched"` |
| `account_flat` | exact `True` for the whole dedicated account, including non-NVDA/unmapped positions |
| `coverage_complete` | exact `True`; absent data is not flat |
| `foreign_open_orders` | integer `0` |
| `own_open_orders` | integer `0` |
| `native_ready` | exact `True` |
| `metadata_fresh` | exact `True` for this session |
| `trading_enabled` | exact `True` from the venue's actual trading-enabled/disabled status |
| `underlying_market_closed` | exact `False` from the documented contract `isClosed` flag |
| `dms_verified` | exact `True` after account-wide DMS arm acknowledgement |
| `available_margin_usdc` | exact decimal string, >= hash-bound `min_available_margin_usdc` |

The app records the accepted start `generation` and `snapshot_unix_nanos`. Missing, unknown,
wrong-type, wrong-run, wrong-phase, or unsatisfied data blocks entry. Whole-account flatness and
zero foreign orders must be established before production DMS is armed; arming first and then
rejecting an occupied account is outside this contract. Official docs define contract `isClosed`
as the **underlying market** being closed, not as the Ondo perpetual being disabled.
Requiring it to be false is this probe's conservative equity-hours policy; it is not a claim that
Ondo rejects perpetual orders whenever the underlying is closed. `trading_enabled` is a separate
native fact and must come from the actual trading status.

## 4. Reconciled-before-stop and final-after-stop snapshots

There are two gates so the strategy does not deadlock waiting for a snapshot that is published only
after it asks the node to stop.

After the last create/order/fill activity, while the node is still running, native drains private
events and performs a fresh authoritative REST reconciliation. The accessor then returns
`phase="reconciled"`. The app waits only until `cleanup_deadline_unix_nanos`, then asks the node to
stop whether this gate passed or failed. Failure or timeout is `Uncertain`.

The reconciled mapping contains:

| Key | Requirement |
|---|---|
| `run_id` | same exact token |
| `phase` | `"reconciled"` |
| `generation` | integer > accepted start generation |
| `snapshot_unix_nanos` | integer >= accepted start timestamp |
| `latest_activity_generation` | native monotonic generation after every create result, order event, or fill |
| `latest_activity_unix_nanos` | timestamp of that latest activity |
| `reconciled_activity_generation` | exactly equal to `latest_activity_generation` |
| `reconciliation_unix_nanos` | strictly greater than or equal to `latest_activity_unix_nanos` |
| `complete` | exact `True` |
| `snapshot_fresh` | exact `True` |
| `instrument_id` | exact approved instrument id |
| `position_qty` | exact decimal string equal to zero |
| `reconciled_flat` | exact `True` |
| `own_open_orders` | integer `0` |
| `foreign_open_orders` | integer `0` |
| `unknown_submissions` | integer `0` |
| `late_fills` | integer `0` |
| `execution_cost` | bounded mapping or `None`; unavailable values stay unknown |

Equal activity generations mean the REST proof includes the latest native activity; a start-only
or pre-fill reconciliation cannot pass. Native freezes further creates before publishing this
phase. If a later private event arrives, it increments `latest_activity_generation`, invalidates the
snapshot, and requires another reconciliation before the gate can pass again.

After native ordered stop completes, the same accessor returns `phase="final"`. It retains all
reconciliation fields above, with generation not lower than the accepted reconciled generation,
and adds `shutdown_status="clean"`. `clean` means own-order cancellation/confirmation and DMS
release policy completed without unknown work. A dirty/unknown shutdown, a later activity not
covered by reconciliation, an earlier timestamp, residual quantity, open order, unknown
submission, or late fill makes `production_execution_verified=false` and the process non-zero.

Suggested `execution_cost` keys are `entry_avg_px`, `close_avg_px`, `fees_usd`, and
`slippage_bps`, each an exact decimal string or absent. Native never invents zero for unavailable
costs.

## 5. Other native invariants

- Reserve envelope capacity atomically at the final send point after any rate-budget wait.
- A timeout/ambiguous create enters `SubmissionUnknown` under the original client order id, blocks
  further creates, and is queried by that id; never resend under a replacement id.
- Batch success requires per-item inspection; this probe does not submit a batch.
- Native stop never creates a flattening order. The strategy sends at most two bounded reduce-only
  exits from confirmed fills; native shutdown cancels and reconciles only.
- Failed reconciliation leaves DMS/recovery state conservative and reports dirty/uncertain; it
  never fabricates clean.

## 6. App boundary

The app does not sign, encode an order body, open a second HTTP client, retry with a new id, cancel
account-wide, batch, change leverage/settings, transfer funds, or infer support from a version or
wheel filename. Dry-run constructs no client and reads no credential. Actual production execution
still requires current-turn `上主网` authorization plus approval of the exact hash-bound plan.

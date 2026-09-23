# Ondo mainnet acceptance — 2026-09-22

## Verdict

**Not accepted.** Two separately authorized hash-bound plans were each executed once. Both single
entry requests were rejected with zero fills and zero net position. Each run produced a fresh,
complete and flat authoritative reconciliation immediately before stop, but neither produced final
shutdown proof because the DMS release was not confirmed. Production execution therefore remains
unverified.

Neither authorization was reused. Both approved hashes are consumed and must not be executed again.

## Authorized execution

- Approved plan SHA-256: `cb4154b0033f0532f1e0708c3e1b9f62e26af0b51f4978459736847e39f1a4b0`
- Run: `20260922T065754074659Z`
- Entry: buy `0.06` NVDA perpetual, IOC, worst price `226.84`, USD 14 target/cap
- Contingent close: reduce-only sell of confirmed filled quantity, worst price `225.91`, at most two attempts
- Outer rails: USD 20 per order and gross exposure, 25 USDC available-margin floor, one new-risk request, six app-visible requests, 120-second absolute deadline
- DMS: 30-second timeout, renewed every 15 seconds

## Observed result

- Process result: exit `1`, outcome `rejected`, report `complete=false`
- One entry request was submitted; no close request was submitted because the entry filled `0`
- Fills: `0`; entry filled quantity: `0`; closed quantity: `0`; net position: `0`
- Local journal checkpoint: orders `0`, fills `0`, unsettled `0`
- Private readiness succeeded before submission: account identity matched, login completed,
  `fillsPerps` and `ordersPerps` subscriptions were acknowledged, native readiness was true,
  available margin was 50 USDC, and the start snapshot was flat with no own or foreign orders
- The report version used for this run did not retain the venue's terminal rejection message, so
  the specific rejection reason is unknown and is not inferred here

## Reconciliation and shutdown

The pre-stop authoritative reconciliation was available, complete, fresh, and covered the latest
activity. It reported position `0`, own open orders `0`, foreign open orders `0`, unknown
submissions `0`, and late fills `0`.

The final reconciliation was not available as a final-phase snapshot. Shutdown reported:

```text
outcome=Complete, tasks_drained=true, switch_released=false, outstanding=[]
```

This means the observed flat account proof is the fresh pre-stop proof, not a clean final shutdown
proof. The DMS release was not confirmed; the venue-side DMS timeout remained the safety fallback.

## Diagnosed defects and corrections

1. The native production disconnect reused the generic five-second shutdown budget. Production
   stop must invalidate the earlier snapshot, perform a new signed multi-read reconciliation, and
   only then release the DMS. The observed reconciliation took longer than five seconds.
2. The app report did not preserve the `TradeSequencer` terminal reason for non-uncertain outcomes,
   which lost the specific order rejection message.

Corrections in the isolated worktrees:

- Native production disconnect now receives up to 15 seconds, always tightened by the remaining
  absolute cleanup deadline. Non-production behavior remains at five seconds.
- The app node allows 30 seconds for bounded disconnection so native final reconciliation, DMS
  release acknowledgement, and private-stream close can finish.
- The app report now publishes the first terminal reason. The native authenticated HTTP path
  redacts the credential before retaining a rejected response body.
- A native regression holds final order reconciliation for six seconds and requires a clean final
  snapshot after DMS release acknowledgement.

## Corrected candidate

- Wheel SHA-256: `ab8b922ac371cff73062f6b0a6afbeec16c94396cac4e9f9cf15c318cf7b0c7f`
- Native binary SHA-256: `8668c5ac1ce92ab4e3e9618f8de80526f7edaaf706590e655499c23e5ab4cf91`
- Stub SHA-256: `9251ed9a23adeb8f16f5cc7766f3c9dc2a2404bb0ec84a435edaa2cd46aa3972`
- Frozen build-source rows checked: 49, mismatches: 0
- Post-fix production-readonly run `20260922T075556070661Z`: exit `0`, complete, identity matched,
  login and both subscription acknowledgements observed, recovery/account state observed, zero orders,
  native shutdown complete

That read-only report explicitly does not prove complete account coverage. It supports the corrected
private lifecycle and shutdown only; it is not used in place of the live run's authoritative
pre-stop flat reconciliation.

## Second authorized execution

The user explicitly approved the fresh same-size plan with SHA-256
`4c63fc1e5ddc6c3c81ff8971c0b3be1feac335eac86efb80c381437a4233c316`. Immediately before the
run, public preflight `20260922T081702627979Z` found NVDA enabled/open with bid `225.99` and ask
`226.06`. Runtime price construction tightened the approved worst entry price using the fresh ask
and the 20 bps slippage ceiling.

- Run: `20260922T081855805659Z`
- Entry request: one buy `0.06` NVDA perpetual IOC; approved worst price `226.62`; USD 14 target
- Result: exit `1`, outcome `rejected`, terminal reason `entry rejected: REJECTED`
- Fills `0`; entry filled quantity `0`; close attempts `0`; net position `0`
- App requests `1`; orders submitted `1`; own open orders `0`; foreign open orders `0`
- Unknown submissions `0`; late fills `0`; duplicate fills `0`
- Journal checkpoint: orders `0`, fills `0`, unsettled `0`
- Credentials, account id and raw frames were not published

The native rejection event supplied no more specific message than `REJECTED`; no cause is inferred.
The pre-stop native reconciliation was available, complete, fresh, ordered after the run start and
covered the latest activity. It reported position `0`, own open orders `0`, foreign open orders `0`,
unknown submissions `0` and late fills `0`.

Final shutdown again reported:

```text
outcome=Complete, tasks_drained=true, switch_released=false, outstanding=[]
```

The 15-second native production disconnect correction therefore did not resolve the real-host DMS
release acknowledgement failure. No additional write or retry was attempted.

The first post-run production-readonly check failed before login with a sanitized runtime error.
The single allowed bounded reconnect, run `20260922T082204857080Z`, then completed with exit `0`:
identity matched, login succeeded, `fillsPerps` and `ordersPerps` were acknowledged, one recovery and
one account-state event were observed, no writes were possible, DMS was not armed, and native
shutdown was complete. As explicitly stated by that report, it does not prove complete account
coverage and is only supporting lifecycle evidence.

The second authorization is consumed. Another mainnet attempt requires a newly prepared plan and a
new current-turn confirmation; this report does not request or imply such authorization.

## Post-second-run diagnosis

The repeated shutdown failure disproves the earlier single-cause hypothesis that the generic
five-second disconnect budget alone prevented release. The corrected live run used the native
15-second production budget and the app's 30-second disconnection window and still ended with
`switch_released=false`.

The native release path sets its confirmation flag only after parsing a WebSocket
`type=unsubscribed` event for `cancelAllOrdersAfterPerps`. Existing loopback tests fabricate that
event. The current official Ondo WebSocket specification accepts `op=unsubscribe` for the DMS
channel, but its endpoint-specific response schema describes a `type=update` channel response; its
generic response enum contains both `unsubscribed` and `update`. The live runner intentionally did
not retain raw private frames, so the exact host frame cannot be recovered from these runs. The
adapter therefore must not guess that an ambiguous `update` means a confirmed release. No DMS
semantic change was made in this step.

The missing rejection detail has a separate, confirmed application cause. Nautilus stores the
reason on the terminal `OrderRejected` event, while the strategy attempted to read a nonexistent
`reason` property from the cached order object. A real-event regression failed with a missing reason,
then passed after the strategy selected the terminal `OrderRejected` event. The complete trade-probe
module result is 179 passed with one PyStrategy cross-thread drop warning from the watchdog test. This correction
was not present in the consumed live run and does not recover its unavailable venue message.

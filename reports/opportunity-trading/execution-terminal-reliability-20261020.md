# Terminal evidence and reconciliation recovery

## Contract

Lighter application terminals require the execution client's explicit terminal
cumulative-fill proof, matching instrument/client identity and the exact sum of
distinct consumed trades. Cache closure and `0 == 0` never establish finality.
The native control records terminal WebSocket orders or original-order HTTP
reports; conflicting evidence fences admission. Missing proof retains the original
order for queries without resubmission. An older wheel lacking the control cannot
construct the new Lighter execution client.

Late fills still change inventory and fees exactly once. Their recoverable fence
is persisted independently of hard failures. Recovery requires current-process
order proof, agreement with the persisted execution ledger, no pending/unknown
orders or unknown economics, and complete authoritative position/open-order reads
after the latest activity. `order_reconciliation_recovered` records the proof and
read boundaries. Other hard protections survive this event. Reductions remain
subject to existing ownership, account freshness and reduce-only checks.

Restart does not count as reconciliation: restored final-fill proof must be
revalidated, and the flat-account session archive cannot erase this fence or a
known hard execution fault. The normal restart-from-flat workflow remains blocked
for such sessions; it does not silently reconstruct or resend archived orders.

## Validation

- 15 new offline replays cover cache-zero, explicit zero, terminal/fill ordering,
  multiple partial fills, duplicates, identity/quantity conflicts, query failure,
  nonfinite quantities, fresh account proof, unknown fees, hard-protection
  preservation and restart fencing/revalidation.
- Existing live, restart, reconciliation and forced-reduction regression tests
  pass with the new recovery behavior.
- Native factory integration requires the accompanying Lighter control wheel;
  build identity and final installed-wheel results are recorded after integration.

No venue orders, deployment or production restart were performed. This change
does not infer exchange matching order from the historical application callbacks.

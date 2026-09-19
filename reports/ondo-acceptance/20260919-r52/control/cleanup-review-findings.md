# Astra review: shutdown acceptance blockers

The first Pi implementation is under review. Findings must be reproduced before correction; no tests or gates may be weakened. Reviewer is read-only.

## P1: a still-open confirming GET can release DMS

Independent reviewer traced `OndoAccountRuntime::confirm_cancel` (execution.rs around 3189): it clears the unconfirmed cancel after any successful order GET, including open/pending. `stop_and_wait` then gates DMS release only on empty uncertainty maps, not whether settling succeeded or unresolved orders remain.

Reproduction required through the actual ExecutionClient::disconnect hook: DELETE accepted without a terminal payload (or ambiguous result); confirming GET returns OPEN. The order remains working, the settle wait times out, but current code can send DMS unsubscribe. A successful query is not a confirmed cancellation. Terminal fill/cancel truth and all unresolved work must gate release. Dirty shutdown must have an observable non-success result; an error must still close/await owned transports/tasks and preserve the journal.

## Required lifecycle coverage not established by only the first four tests

Exercise actual hooks for: normal stop; cancel/confirmation timeout; unknown submission; restart inheriting unresolved work; repeated disconnect; synchronous stop before disconnect; read-only and foreign-order behavior. Existing tests that only call stop_and_wait directly do not establish the newly wired real lifecycle for unknown/restart. Include future cancellation/outer timeout checkpointing, because the node's timeout can drop disconnect mid-await.

Further independently verified findings will be appended here before the correction handoff.

## P1: read-only restart can cancel journal-restored orders

The new disconnect calls the unconditional tracked_orders cancellation loop. Journal restoration can populate tracked owned orders even with account_read_only=true; the read-only flag gates new risk, but not account.cancel_order or the HTTP cancellation path. Existing read-only test has no restored orders. Reproduce by creating a journal with an owned open/uncertain order, reopening the account read-only, then disconnecting: assert ZERO DELETE and ZERO DMS arm/release frames and retained unresolved journal state.

## P1: terminal order status is insufficient if fills remain unreconciled

pending_orders currently only checks status.is_terminal. A canceled or filled order can still have missing/reconciled-fill discrepancies; unresolved_orders already represents these. DMS release and clean stop must consider unresolved_orders as well as cancel/submission maps and deadline outcome. Reproduce through the real hook, without treating an incomplete fill ledger as settled.

# DMS contract evidence

Official source: <https://docs.ondoperps.xyz/api-reference/ws-spec.json>, read on
2026-09-22 through an unauthenticated HTTPS GET. No account endpoint or credential was used.

The `/ws/cancelAllOrdersAfterPerps` request schema permits `subscribe` and `unsubscribe`
with `timeout_seconds`. Its endpoint response schema describes `type=update` and an
unstructured `data` object. It does not define a disabled-state field, an operation
correlation token, or a release-specific acknowledgement example.

This is insufficient evidence to treat an arbitrary DMS update as a release acknowledgement.
It also does not prove that the host never returns the generic `unsubscribed` event.
The shared `WebSocketResponse` schema explicitly includes `unsubscribed` and a channel field;
the endpoint's update schema is not evidence that generic acknowledgements are forbidden.
The two previous live runs retained no raw private frames, so their exact response and the
stage at which release failed remain unknown. A timeout or protocol mismatch is a hypothesis,
not an established venue-side cause.

The new candidate retains the existing release predicate. It adds fixed-label observations
of release attempts, transport writes, acknowledgements, ambiguous updates, and failures.
These observations are not account reconciliation evidence and cannot change
`production_execution_verified` or certify a clean final snapshot.

Only the following DMS data fields may be classified: `op`, `timeout_seconds`, `status`,
and `enabled`. Their values are reduced to a fixed vocabulary; arbitrary keys, account
identifiers, credentials, messages, and raw payloads are not retained. An absent field is
unknown rather than success. Recognizing a payload shape does not establish its semantics.

A host observation can narrow the remaining question, but a newly observed `disabled`,
`success`, or zero timeout is not automatically a documented release contract. Any semantic
change still needs evidence that distinguishes release from arming, renewal, and timer expiry.

The additional [GitHub SDK review](github-sdk-review.md) inspected three independent clients.
None supplied a DMS release-ACK state machine or verified fixture. Public search did not resolve
the missing semantic contract. No third-party code was installed or executed.

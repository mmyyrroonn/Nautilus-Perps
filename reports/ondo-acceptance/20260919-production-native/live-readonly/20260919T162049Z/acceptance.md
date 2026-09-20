# Native production read-only acceptance

Date: 2026-09-20 Asia/Shanghai (run timestamps are UTC). Candidate wheel SHA256:
`df6c440170ad47fd1d8497a7c4da5cdddab69059d499bbddfb5d36f504f2c1d2`.

## Result

**Failed / not verified.** The bounded child exited with code 1 after about 13 seconds, before the
60-second observation window. The 150-second supervisor limit did not fire. No retry or alternate
authentication variant was attempted.

The native snapshot was available and matched the expected nine-key schema and this run token.
Authenticated REST identity comparison returned `matched`. The private WebSocket did not produce an
accepted login, either required subscription acknowledgement, a recovery, or an account-state
event. Shutdown reached private run state `stopped`, but the owned shutdown status remained
`stopping`, so clean shutdown was not proven.

The application published the controlled failure category `session_runtime_error`. Native console
and file logging were disabled and the supervisor discarded the child streams, so no account detail
was retained. This also means the exact startup exception is unavailable. The roughly ten-second
failure timing is consistent with the configured node connection timeout, but that is an inference,
not a demonstrated root cause. It must not be reported as a WebSocket authentication rejection,
Cloudflare response, or public-data failure without additional evidence.

## Boundaries

- Production execution verified: `false`.
- Production read-only support verified: `false`.
- Orders, cancels, transfers, settings changes and DMS messages: prohibited by the candidate mode;
  none are evidenced by this run.
- Subscription acknowledgement is not an order/fill event, and none was observed here.
- REST identity matching alone does not establish private transport or native account integration.

Controlled machine-readable evidence is in `supervisor.json`, `probe.json`, and `meta.json`. Raw
console output was not retained by design.

# Aster leverage admission

Issue [94](https://github.com/mmyyrroonn/Nautilus-Perps/issues/94) adds a
read-only leverage gate to continuous pair entries. It never changes account
leverage, margin mode, notional limits, or native risk authority.

At initialization, the first fresh account proof automatically establishes a
target for each configured Aster instrument whose current leverage fits its
existing exposure tier. This reads the account setting without changing it.
The target is fixed for the session, persisted with its source/observation time,
and checked against each proposed order's exposure tier before entry.
Late instrument registration can initialize from the same cached proof.

Manual per-instrument targets remain optional overrides in the live TOML file.
The following is a **configuration example**, not an assertion about STX's
current or permitted leverage:

```toml
[aster_leverage]
max_age_ms = 5000

[aster_leverage.targets]
"STXUSDT-PERP.ASTER" = 5
```

Manual targets accept integers from 1 to 125, subject to the current venue limit.
The freshness budget defaults to 5000 ms and can be set from 1 to 15000 ms.
Absent manual targets are resolved automatically. A missing, stale, failed, or
already over-limit account proof cannot initialize a target; entry remains
blocked until a valid proof is available. Dry-run output lists instruments using
automatic initialization. Account leverage changes after initialization produce
a target mismatch; they do not silently replace the session target.

The independent signed reader uses `GET /fapi/v3/positionRisk` and
`GET /fapi/v3/leverageBrackets`, as documented in the
[official Aster V3 API](https://asterdex.github.io/aster-api-website/futures-v3/account&trades/).
The latter is the currently recommended plural endpoint. Bulk responses share
the existing account cache and retain the oldest request-start timestamp.
Reusing a response never renews its age. Unknown/malformed tier coverage and
failed reads cannot authorize an entry. A bracket-query failure retains
position/order evidence for reconciliation and reduce-only cleanup.
Parsed proofs are also reused while their underlying cached responses are the
same. Each proof builds its instrument index once; entry checks use an O(1)
lookup and scan only that instrument's small tier list. Normal candidate and
dispatch checks perform no network I/O. Rejection/block events are persisted
on state changes rather than on every evaluation tick.

Both candidate creation and each actual entry dispatch check the original two
legs. The gate requires actual leverage to equal the explicit target and both
to fit the tier for conservative projected exposure: existing absolute position
plus the proposed quantity, valued at the greater of mark and order-limit price.
An exact tier boundary uses the next tier; exceeding coverage blocks admission.
The existing book, account, notional, unhedged-risk and native checks still run.
No HTTP observation can guarantee that the venue will not change a limit after
the observation; the dispatch check enforces the configured freshness budget.

Admission events and entry evidence retain target, actual leverage, allowed
maximum, observation/check times and projected quote-currency notional.
Block reasons distinguish missing target/proof/symbol, stale data, read failure,
target mismatch, maximum exceeded, unsupported notional and required recheck.
The runtime document exposes `leverage_blocks` and `leverage_rejections`.

The native Aster error envelope is reduced to its numeric `exchange_error_code`
and a fixed `leverage_configuration` reason. Raw venue text is never persisted.
Codes -2027, -2028 and -4028 are configuration refusals. A configuration receipt
immediately persists a per-instrument rejection fence even if final fill evidence
is still pending. Terminal diagnostics and trade-review JSON retain the code.
New entries remain blocked until a successful read **started after** the
rejection passes all checks. Read failures use existing account retry/backoff.

The rejection fence survives both order-ledger restoration and flat-session
restart. Same-ledger restoration also retains initialized targets; a new
flat-account session initializes its own targets from fresh proof. Freshness
never comes from persisted evidence. Leverage targets are
excluded from the ledger ownership fingerprint so an operator can correct a
target without abandoning original order IDs. Changed targets still undergo
the full gate. A blocked second leg is marked locally undispatched; the already
sent leg retains its original ID and normal query/fill/reduction lifecycle.

This gate covers continuous pair entry. Optional standalone BTC capability
self-tests remain a separate explicit workflow.

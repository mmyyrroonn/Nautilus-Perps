# Entry freshness and dispatch budgets

Continuous pair entries now apply a separate `[entry_admission]` policy at
each local gateway handoff. Missing publication evidence, invalid clocks,
expired candidates, stale books and excessive cross-venue source skew block
unsent entry legs. Both original legs are checked before either send.

| Setting | Default | Measurement |
| --- | ---: | --- |
| `max_source_age_ms` | 500 | Check time minus venue source timestamp |
| `max_receive_age_ms` | 250 | Check time minus native receive timestamp |
| `max_skew_ms` | 250 | Absolute difference between the two source timestamps |
| `max_publication_wait_ms` | 150 | Native receive to strategy publication |
| `max_dispatch_delay_ms` | 100 | Candidate timestamp to final local send check |

Each value must be an integer from 1 to 2000 ms. Source/receive/skew thresholds
are also capped by the corresponding existing scan settings. Exact equality
passes; one nanosecond over a threshold fails. These defaults are conservative
offline controls, not a calibrated execution probability or a venue guarantee.
Publication wait includes native delivery queues and batching; it is not pure
timer wait. The configured 100 ms publication/evaluation interval is unchanged.

The coordinator uses current wall time plus monotonic elapsed time anchored to
candidate preparation. This covers pricing, persistence, dispatch release,
native admission refresh and a synchronous first-leg dispatch. A final temporal
check includes time spent checking depth, economics and accounts. Optional
timing sampling cannot disable the deadline. Fresh replacement books do not
renew an existing candidate's deadline. Source-to-receive age is not network RTT.

The gate never changes persisted price, quantity, client ID, slippage or fees.
Existing limit-depth, both-leg fees, reserve, net edge, account, leverage,
unhedged and native checks still apply. A blocked second leg retains the first
leg's original ID and query/fill/reconciliation lifecycle. Reduce-only recovery,
cancels and account reads do not use these entry deadlines. Admission settings
are excluded from the ownership fingerprint so changing a budget cannot prevent
recovery of an existing ledger; every new handoff uses the current policy.

`entry_dispatch` on each checked order records `allowed`/`blocked`, the reason,
both legs' observed timestamps, exact ages, measured skew, candidate deadline,
effective thresholds and available price/depth/economic evidence. Early existing
risk refusals can omit economic fields not reached by that check. Local block
events persist the decision immediately. Allowed decisions piggyback on existing
receipt/state transactions and accepted/terminal/unknown events; no new disk I/O
is inserted between validation and sending. Consequently a process crash before
the next receipt/state write can lose the last allowed diagnostic, while the
original durable dispatch identity remains available for recovery.

Publication records retain one snapshot identity and time per configured market;
candidate clocks retain pending entry IDs only. Neither process-local clock is
restored. There is one latest decision per retained order, not a new unbounded
per-tick history. Optional timing reports retain their existing bounded window.

Reproduce the incident timing projection and local benchmark with Python 3.12.9:

```powershell
E:/persarb/Nautilus-Perps/.venv/Scripts/python.exe scripts/replay_entry_admission.py --output reports/opportunity-trading/issue95-entry-admission-replay.json
E:/persarb/Nautilus-Perps/.venv/Scripts/python.exe scripts/benchmark_entry_admission.py --output reports/opportunity-trading/issue95-entry-admission-benchmark.json
```

The fixture contains allowlisted public book/order measurements and hashes of the
two original incident files. It contains no credentials or account addresses.
The historical replay is a timing projection, not a reconstructed matching engine
or full historical L2/account simulation. Historical publication timestamps are
unknown and remain explicit unknowns; synthetic cases supply known timestamps.

See [acceptance and limits](../reports/opportunity-trading/issue95-entry-admission.md).

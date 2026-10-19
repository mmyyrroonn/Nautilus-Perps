# Memory and market-data queue diagnostics

Enable diagnostics for a live runner with `--memory-diagnostics --timing`.
The flags do not change subscriptions, book depth, order limits, queue policy,
or shutdown behavior. They require the fork's
`LiveNodeHandle.metrics_snapshot()` API. An older wheel fails before the live
node runs. The memory report defaults to
`reports/opportunity-trading/memory/`; use `--memory-diagnostics-dir` to override.

`memory-latest.json` is replaced atomically every 15 seconds. Each process has a
unique `memory-<pid>-<run-id>.jsonl` history, rotating at approximately 8 MiB with
one previous file. Keep both files when collecting evidence. Separate timing
reports are written under `reports/opportunity-trading/timings/` every 10 seconds.
The memory history also includes bounded timing snapshots, so rates and memory
can be compared over the same interval. Neither report contains credentials,
prices, order IDs, account IDs, or full messages. Largest-book rows contain
public instrument IDs only.

| Field | Meaning and limits |
| --- | --- |
| `process_bytes` | Linux process RSS, anonymous/file RSS, swap, high-water mark, and virtual size, in bytes. |
| `native_runner.*.queue_depth` | Actual receiver lengths for data events, data commands, time events, execution events, and execution commands. Sampled on the native maintenance tick. One event can contain many price updates. |
| `native_runner.queue_sampled_at_ns` | Native elapsed time of the receiver-length sample. Compare to `elapsed_ns`; an old sample is not evidence of an empty queue. Fields are independent atomic reads, not a transaction. |
| `native_rates` | Per-channel dispatched events/second, net queue growth/second, dispatch busy fraction, and mean dispatch duration. Dispatch and queue rates use their respective sampling clocks. Reset or nonadvancing intervals omit rates. |
| `market_batches` | Per-venue callback counts, price-update counts, processing rates, largest batch seen, and timestamp of the last processed batch. Counts include callbacks received before a snapshot is ready; they are not producer-side ingress counts. |
| `market_batches.*.delivery_age_*` | Callback time minus native `ts_init`, sampled once per 128 batches, retaining the latest 128 samples per venue. These ages include delay after `ts_init`; they are not the exact oldest queued age or exchange network latency. Quiet venues can retain old samples. |
| `market_timer` | Scheduled market timer callback age, retaining the latest 128 completions. Sustained lag can expose a busy event loop. |
| `books.venues` | Configured markets, requests issued, snapshots ready, and current bid/ask level counts per venue. Requests are not exchange ACKs. `pending_aster_subscriptions` counts requests still waiting for pacing. |
| `books.total_levels` | Current strategy-side native book levels, counted from the companion price-key sets without cloning ladders. This excludes adapter-owned book copies and other caches. `book_sample_age_ms` shows whether this callback-side summary is stale. |
| `processing_timings` | Existing fixed-name wall-time spans: book callbacks, snapshot publication, opportunity evaluation, account reads, coordinator work, and SQLite persistence. Counts/totals are cumulative; percentiles cover the latest 1024 completions. Nested totals overlap. Compare `self_ms` deltas to locate work, and distinguish worker threads from the event-loop thread. |
| `account_checks` | Latest validation pass per account domain: timestamp, markets checked, distinct proofs checked, expected quantities computed, position rows checked, and unconfigured position rows. Copied on the 15-second callback sampling interval. These are last-pass counts, not cumulative counters or raw account data. |
| `python` | Allocated block count and GC counters. Optional tracing fields are present when tracing is enabled. |

Native data events also include metadata/status and other data types; their
dispatch count is not identical to the Python book callback count. The
`price_updates` counter counts delta records, including a snapshot's CLEAR
record, and must not be interpreted as the number of retained price levels.

`strategy.account_domain` spans identify per-domain validation and recovery work
inside `strategy.reconcile_accounts`. Use the parent's inclusive time when
comparing versions; moving work into a child span reduces parent self time.
`account_checks.position_rows_checked` sums distinct payloads actually inspected
in that pass. Mixed observations can contribute more than one proof; observations
rejected for authority, age, or an activity fence can contribute no inspected
position rows. A zero count does not certify an empty account. Shared results
exist only during one validation pass, before pause/recovery side effects; fresh
passes recheck inventory, order ownership and activity fences. Full evidence
includes unconfigured instruments. Live reconciliation/risk timer work is owned
by the execution coordinator; market publication keeps its independent timer.

Pricing stages also expose `sampled_span` rows named `pricing.entry`,
`pricing.prepare_pair`, and `pricing.remaining_book`. With `--timing` enabled,
each stage measures every 127th call on each thread. The interval is recorded as
`sample_every`; skipped calls read no clocks and do not acquire the recorder
lock. Fixed-name counters retain only a bounded ordinal, and reports use the
existing 1024-completion sample window. Arguments, book contents and rejection
payloads are not captured. Disabled timing does not advance the sample counter.

These rows contain **only sampled completions**, not exact lifetime call counts
or a statistical estimate of total work. Sampling is periodic and can correlate
with traffic patterns. `self_ms` equals inclusive `total_ms` for sampled rows;
they neither enter the regular nesting stack nor subtract from the full
`strategy.entry` span. Do not sum them with full spans or use them as exact CPU
shares. Compare sampled per-call means/percentiles to distinguish pricing work,
pair validation and remaining-liquidity construction. Use the full parent span
and native dispatch counters to assess overall load and regressions.

Untouched immutable pricing snapshots are reused when there is no simulated
consumption on either side. Paper fills still record and subtract consumed
liquidity, and a new source generation resets that consumption. Immutable book
structure and metadata validity are cached on each object after their first
validation. Replacing a snapshot or metadata object starts a new validation;
there is no global cache retaining past books. Cached properties are excluded
from dataclass evidence, equality and persistence. Freshness, pair skew, current
validity, sizing and live account/risk admission still run against current state.
The conservative best-quote rejection now also runs before pair preparation;
it cannot admit a trade and bounds even partially consumed paper liquidity.

## Pipeline latency boundaries

Timing rows now include `recent_p50_ms`, `recent_p95_ms`, and `recent_p99_ms`,
using nearest ranks in the most recent 1024 completions **per row**. These are
not full-run quantiles, and percentiles from successive reports cannot be
averaged to reconstruct a window's quantiles. The memory report also adds p99
to its existing 128-sample delivery-age and market-timer windows.

| Timing name | Start → end | Sampling |
| --- | --- | --- |
| `market.delivery_age` | Native batch `ts_init` → Python book callback entry | Every 127th callback per venue/thread; wall clock |
| `market.first_update_to_publish` | First complete local book update in a publication cycle → snapshot stored in strategy state | Every 127th cycle per venue/thread; monotonic clock |
| `market.latest_update_to_publish` | Last complete local book update in that same cycle → snapshot stored | Same selected cycles |
| `market.publish_to_evaluate` | Snapshot stored → symbol evaluation starts | Same selected cycles; includes publication of other legs and earlier symbol work |
| `entry.candidate_to_persisted_intents` | Pricer returns a qualifying candidate → both entry intents durably prepared | Every successful preparation |
| `entry.candidate_to_submit` | Same candidate → each leg's Python `submit_order` call | Every entry leg reaching the handoff |
| `gateway.submit_to_outcome` | Python `submit_order` call → first observed result of each outcome kind | Each submitted request/outcome, deduplicated |

The first four are `sampled_latency` rows (`sample_every=127`); counts and totals
include only selected completions. Selection is periodic, not random. Whole
publication cycles are selected when the first complete update arrives; further
updates refresh only the latest-update marker, so a busy book cannot continually
erase its first-update wait. Incomplete batches, reset/invalidation and stop
discard pending markers. State holds at most one cycle and one publication per
configured market; successful entry markers have a fixed cap of 1024, are taken
on dispatch/discard, and are never written to or restored from the ledger.
New submit-to-outcome markers also have a fixed cap of 1024 and are retired on
terminal outcomes/close. At capacity only the oldest diagnostic marker is
evicted; the order and its receipts are still processed in full.
There are no per-message diagnostic logs, IDs or book payloads in timing rows.

`ts_init` is **not** a queue enqueue timestamp. Delivery age includes work before
and after native enqueue; invalid/future timestamps are omitted, not clamped to
zero. The timing row uses `time.time_ns() - ts_init`; other new latency stages use
`perf_counter_ns()`. Wall-clock adjustment can affect delivery age. Exact native
queue residence requires a producer-side timestamp which this Python interface
does not expose.

Likewise, `submit_order` is a command handoff, not proof of network transmission.
Submission-to-outcome includes native queueing, risk processing, signing,
transport and callback delivery. Outcomes include local unknown/rejection and
must be inspected separately from acceptance/fill. A rejection before handoff
does not create a submit latency. Existing `gateway.submit.response` starts
earlier, on gateway entry, so also includes order construction. Restored orders
have no reconstructed monotonic markers. Missing candidate/submit rows mean no
qualifying completions were observed, not zero latency.

Early-rejected combinations no longer enter `pricing.prepare_pair` or
`pricing.remaining_book`; compare their counts as well as their means. The
successful-candidate rows cannot be inferred from average `strategy.entry` time.
Keep the 100 ms evaluation interval while validating this first round; see the
[optimization and acceptance sequence](entry-latency-optimization.md).

To investigate an unbounded queue, compare consecutive records with the same
PID and run ID. Rising `data_events.queue_depth`, rising delivery age, and
roughly stable book levels alongside rising RSS support producer/consumer
imbalance. Inspect per-channel busy fractions and timing deltas to distinguish
expensive data dispatch from timer, strategy, account, or persistence work.
If the queue drains while RSS remains high, retained allocator pages are one
possibility; RSS alone does not establish unreachable live allocations.

`--memory-tracemalloc` additionally starts one-frame Python allocation tracing
and requires `--memory-diagnostics`. It adds overhead and can change throughput;
leave it off in the first queue diagnostic run. Traced memory excludes Rust's
mimalloc heap. RSS minus traced bytes is not an exact measurement of native
live allocations. A stable visible runner queue also does not rule out an
upstream adapter queue or a separate memory leak.

Counter updates use fixed venue labels and bounded sample windows. Report I/O
runs on a separate thread. Diagnostic collection and write failures are counted
and do not request shutdown or change trading decisions. The timing option does
add per-span measurement cost; compare instrumented results accordingly.

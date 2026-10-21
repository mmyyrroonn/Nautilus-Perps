# Price-to-order timing

Use the existing live runner's `--timing` option. Trading decisions, limits,
durable state and transport behavior are unchanged by this diagnostic.

Each bounded entry trace now exports its original client order ID. This is the
join key for native Hyperliquid records; request IDs and account identities are
not additional exported fields. Keep reports with the execution evidence.

## Quote and application boundaries

The pricing trace and each send check retain both legs' observed quote versions:
instrument, venue, native event/init timestamps, sequence, and local monotonic
callback/completion/publication timestamps when available. Only the latest
complete version per configured instrument is retained. Invalidation removes it.
An unobserved or replaced version has no invented callback timestamp.

`callback_ns` starts the Python callback carrying the final complete batch.
It is not the first packet, first fragment, or native socket read. `complete_ns`
follows local order-book application; `published_ns` follows publication to
strategy state. The latter includes configured coalescing, currently 100 ms in
the live configuration. The original candidate quote and send-check quote can
differ; reports retain both rather than silently attributing a newer quote to
the earlier decision.

The ordered stages cover:

1. `pricing_started` → `candidate`: initial pricing of the qualified opportunity.
2. `candidate` → `persist_started`: bounds, sizing, risk/account checks and order preparation.
3. `persist_started` → `persisted`: durable intent transaction.
4. `wakeup_requested` → `wakeup_request_returned`: notifier call, which can coalesce.
5. `wakeup_consumed` or `timer_callback` → `tick_started`: callback entry.
6. `gateway_poll_completed` / `loss_check_completed` / `evaluation_completed`:
   coordinator phases which ran while an entry remained pending.
7. `dispatch_release_started` → `dispatch_release_completed`: durable dispatch-release transaction.
8. `coordinator` → `send_check`: admission and fresh quote validation for this leg.
9. `send_check` → `python_submit`: native order construction and local handoff preparation.
10. `python_submit` → `python_submit_returned`: synchronous `submit_order` call.

`python_submit.wall_ns` is now a fresh clock observation at the handoff marker.
Other supplied wall times retain their original callback/check semantics.
Instrumentation between a marker and its operation is included in measured time.
The native receive boundary occurs downstream of Python, risk and execution
command queues; it is not guaranteed to precede the Python call's return.

Storage remains bounded: 1,024 entry traces, 48 stages and 16 native callbacks
per trace, plus at most two quote-timing records per configured market
(pending and published). No extra book ladder, order queue or disk write is
added to the market callback. Report serialization stays on the existing writer.

## Native correlation

Set `NAUTILUS_HYPERLIQUID_TIMING_DIR` to an existing writable directory before
client construction. The native collector writes its existing seven-field
JSONL records plus one `hyperliquid-clock-<session>.json` calibration on Linux.
The calibration brackets `CLOCK_MONOTONIC` with the session's `Instant` clock.
The application similarly brackets `monotonic_ns` with `perf_counter_ns`.

```text
python scripts/analyze_price_order_timing.py --application <timing.json> --native-directory <capture-directory> --output <analysis.json>
```

The analyzer requires matching Linux clock implementation, boot ID and PID
before joining clocks, and exports interval bounds reflecting both calibration
windows. Another process/boot/platform, missing stages, repeated writes, and
ambiguous attempts remain explicitly unjoined or absent. These bounds are not
a guarantee that total diagnostic overhead is that small.

The new `read_wake_observed` → `read_poll_started` interval measures time from
the first forwarded wake after the preceding poll to the final stream poll.
A wake may originate from readiness or the split-stream lock; it is not proof
of packet arrival. `read_poll_started` → `socket_read_completed` includes the
final poll's read/TLS/WebSocket work. Earlier partial reads are outside that
interval. Buffered frames and canceled reads never inherit a previous wake.

## Optional Linux TCP observation

This bounded probe runs separately from trading and is never enabled by the
service. It requires root, compatible kernel BTF/kprobes and bpftrace 0.25.
The server's kernel support was verified with a short bounded capture.

```text
python3 scripts/capture_tcp_timing.py --pid <live-pid> --seconds 60 --local-port <port> --max-events 50000 --output-directory <new-directory>
```

The duration cannot exceed 300 seconds; event output cannot exceed 100,000
records. The default event cap is 50,000. Socket/read maps have at most 256
keys. `--local-port 0` observes all discovered TCP sockets of the specified
process and may reach the cap quickly under market load.

Records contain only time, local port, thread ID and byte counts/return values.
`tcp_input` observes established TCP input before reassembly; sequence coverage
can include FIN. `tcp_read_returned` counts bytes copied or a negative errno.
`tcp_send_called` is a send-function entry, not NIC transmission. Discovery
starts on the process's first observed send/read, so earlier input is missing.
Stderr preserves compatibility failures and event-loss warnings.

TLS may split or combine messages and connections may carry unrelated frames.
The tool deliberately makes no automatic packet-to-order claim. Use connection
identity and the calibrated order window, retaining ambiguity. Neither this
probe nor the application timestamps reveal exchange receipt or internal
processing. Network and exchange time cannot be exactly separated without
exchange-side timestamps.

References: [Python clocks](https://docs.python.org/3.12/library/time.html),
[bpftrace language](https://bpftrace.org/docs/release_025/language),
[Linux timestamp boundaries](https://docs.kernel.org/networking/timestamping.html).

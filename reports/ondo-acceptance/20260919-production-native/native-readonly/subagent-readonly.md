# Native readonly N2 handoff

Completed: 2026-09-19. Source worktree: `E:/nautilus_trader/.worktrees/ondo-production-native`.
Scope: N2 readonly corrections only. No trade implementation, app edits, credentials, remote signed
requests, commits, merge/push, installation, wheel/stub/release work. Existing changes preserved.

## Findings and changes

The inherited partial N2 source already contained HTTP credential/scope environment equality,
execution credential equality, `diagnostics_run_id`, accepted-login counters and acknowledged-channel
storage outside the bounded diagnostic ring, and factory-shared stores. These were verified rather
than replaced. Private WS derives scope from credential environment + mode and validates endpoint;
production Trading mode is refused before task/socket construction.

This continuation changed:

- `src/websocket/private/diagnostics.rs`: initial `Connecting { attempt: 1 }` no longer counts as a
  reconnect; attempts greater than one do. Red factory regression observed 1 instead of 0 before fix.
- `src/websocket/private/stream.rs`: Unsupported/ProtocolError/VenueError/SwitchChannelUpdate logs
  emit controlled labels, never raw venue reason strings. Existing private diagnostic redaction and
  handling remain. No log-capture test was added: project testing rules explicitly prohibit asserting
  logs; the exact four statements were inspected, and actual protocol/lifecycle tests ran.
- `tests/private_runtime.rs`: real production-readonly factory clone -> native client -> loopback
  HTTP/WS lifecycle verifies accepted login, both acknowledged subscriptions, account-state emission,
  reconciliation, clean stop, no DMS/POST/DELETE, independent factory and second-run reset.
- `tests/python.rs`: Python config `diagnostics_run_id` and actual PyO3 extraction clone share the
  original factory store across two creations. Corrected inherited expected-key ordering: sorted
  `reconnects` precedes `recoveries`; no behavioral assertion removed.
- `tests/execution.rs`: explicit credential mismatch in both environment directions refuses before
  transport, with no requests and no credential material in the controlled error.
- `cargo fmt -p nautilus-ondo` also normalized existing partial N2 formatting in `src/diagnostics.rs`.
- `interface.md` now describes actual nine Python keys and creation-time token binding. Corrected
  N1 summary's REST auth-page statement and incorrect WS hash-manifest reference. Historical N1
  totals/hashes remain historical; use this report for the current verification result.

## Actual verification

Environment per test invocation:

```powershell
$env:CARGO_TARGET_DIR='E:/nautilus_trader/target'
$env:CARGO_BUILD_WARNINGS='allow'
$env:PYO3_PYTHON='E:/nautilus_trader/.venv/Scripts/python.exe'
$env:CC='clang'
$env:CXX='clang++'
```

All tests use synthetic credentials and literal loopback endpoints. Target cache ownership is now
released. The warning exception matches the existing documented Windows linker exception and is
not a strict-warning pass. No clippy rerun was performed against the known unrelated baseline.

| Command | Result | Evidence |
|---|---|---|
| `cargo test -p nautilus-ondo --features python --locked --offline --test private_runtime test_readonly_factory_clones_share_live_telemetry_and_isolate_runs` | Initial test setup used default 30s interval with 10s wait; failed before convergence. Corrected test config to 1s. | `logs/n2-factory-red.txt` |
| Same focused command after test setup correction | Expected RED: initial reconnect count 1, expected 0. | `logs/n2-factory-red-counter.txt` |
| Same focused command after counter fix | GREEN: 1 passed. | `logs/n2-factory-green.txt` |
| `cargo test -p nautilus-ondo --features python --locked --offline` | First full run failed only inherited Python sorted expected-key order; all preceding binaries passed. | `logs/n2-full.txt` |
| Same full command after attempted test edit | Same test failed because a PowerShell replacement syntax error prevented edit; preserved evidence. | `logs/n2-final-full.txt` |
| `cargo fmt -p nautilus-ondo -- --check` | Exit 0; stable rustfmt warns nightly-only config options cannot apply. | `logs/n2-fmt-check.txt` |
| `cargo test -p nautilus-ondo --features python --locked --offline` after verified correction | Exit 0, **946 passed**, 0 failed. | `logs/n2-full-green.txt` |
| `git -c safe.directory=E:/nautilus_trader/.worktrees/ondo-production-native diff --check -- crates/adapters/ondo` | Exit 0, no whitespace errors. | Executed directly at handoff. |

Final counts: lib 465; execution 77; HTTP client 44; HTTP contract 77; market data 43;
private runtime 50; Python 8; reconciliation 118; signing 63; doc tests 1 = 946.

## Remaining work and native trading gap

Readonly source corrections and offline verification are complete. Parent review, candidate packaging,
generated stubs, installed-wheel checks and authorized bounded production readonly observation remain.
No host login/account identity or real production order result is claimed. `GET /v1/account` accountID
and DMS semantics remain host-unverified here. Full readonly snapshot exposes no order/fill event
counts; subscription ACK is not fill observation.

Trading requires substantial separate implementation, not toggling a flag:

- `config.rs:460-485`: production supports readonly only and rejects production order opt-in;
  there is no production execution envelope/config class or write scope.
- `http/client.rs:247-279`: NewRiskPermit carries generation and revalidation, not serialized-body
  notional reservation, exact instrument/side/quantity/price plan, concurrent opening/closing budget,
  absolute order/exposure caps, or entry/cleanup deadlines.
- Existing sandbox create/batch/cancel paths are broader than the requested IOC-only, one-opening,
  at-most-two-closes production probe. Direct production POST/DELETE must retain fail-closed guards.
- `reconciliation.rs:2825-2830`: NotConfigured/Degraded journal status currently does not refuse
  new risk; production requires durable persistent recovery data before new risk.
- Existing lifecycle/DMS/unknown-submission machinery is useful groundwork, but production must bind
  genuine protection ACK, owned capacity, foreign-order/whole-account coverage and clean final REST
  proof to the same run and serialized commands. Current readonly diagnostics have no final
  reconciliation generation/timestamp or trade envelope snapshot surface.
- Trade app's latest exact interface is `trade-app/required-native-interface.md`; implementation and
  adversarial concurrency/failure tests remain unstarted in this task.

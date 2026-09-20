# App readonly native integration handoff

Date: 2026-09-19. Scope: application-side integration only in
`E:/Nautilus-Perps/.worktrees/ondo-production-native`. No credentials, `.env` read, sockets,
native writes, trading, build, install, commit, merge or push occurred.

## Integrated native dependency

The application is pinned to the frozen N2 contract in
`native-readonly/interface.md`:

- `OndoExecutionClientConfig.diagnostics_run_id: str | None` binds the app-minted nonsecret
  run token to the native client.
- The same `OndoExecutionClientFactory` instance is registered with the node and read after
  stop through zero-argument `read_only_snapshot()`.
- The detached snapshot has exactly nine keys: `run_id`, `logged_in`,
  `subscriptions_acked`, `run_state`, `reconnects`, `recoveries`,
  `account_state_events`, `identity_match`, and `shutdown_status`.
- Shutdown labels are exactly `not_attempted`, `running`, `stopping`, `complete`, and
  `incomplete`; only `complete` is clean.
- Subscription ACKs do not imply an order or fill event. The native surface exposes account
  state event count, not private payloads or per-channel event counts.

## App behavior

- `src/ondo_probe.py` passes the current `run_id` as `diagnostics_run_id` on the actual
  execution config and reads the same factory after bounded stop.
- `src/ondo_native_diagnostics.py` validates exact keys and safe types. Missing/old accessor,
  lookup failure, non-mapping, missing token, and cross-run token remain unavailable with all
  telemetry fields null. Extra/missing keys or wrong types keep `schema_confirmed=false`.
- Only fixed labels and nonnegative counters are published. Account/key/order identifiers,
  raw frames, raw errors and monetary values are ignored and never rendered.
- Production read-only accounting buckets publish `{count, details: "withheld"}` rather than
  order records. This preserves submitted/acked/filled/partial/canceled/unknown/no-trade,
  pending, outstanding, lookup-failure and cache-error distinctions without publishing an
  identifier, status, reason, amount or price. Public, paper, sandbox account-readonly and
  sandbox reports retain their detailed existing shape.
- A production session exception is rendered as the fixed `session_runtime_error` label;
  traceback text is not printed. Late refusals and report-write failures likewise use fixed
  labels, and stop exceptions render only `error: "withheld"` while retaining stop booleans
  and iteration counts.
- `production_readonly_support_verified=true` requires all of: successful bounded session,
  same-run snapshot, confirmed schema, matched identity, accepted login, ACKs for both
  `ordersPerps` and `fillsPerps`, at least one native account-state event, and clean native
  shutdown. Any missing/unsupported/null fact keeps the verdict false.

## Test-first evidence

The canonical repository launcher `E:\Nautilus-Perps\.venv\Scripts\python.exe` works when
the managed sandbox grants the required escalated access. The sandbox user cannot access that
interpreter directly; this is an access boundary, not a missing uv base interpreter. No
package was installed.

RED command:

```powershell
$env:PYTHONPATH='E:\Nautilus-Perps\.venv\Lib\site-packages'
python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider `
  --basetemp=.pytest-tmp-readonly-red -k "native_diagnostics_document_carries_only_counters_and_labels or native_snapshot_schema_requires_exact_confirmed_keys_and_types or production_readonly_support_requires_all_native_acceptance_evidence or production_readonly_support_fails_when_native_acceptance_evidence_is_missing or production_readonly_report_reads_a_matching_native_snapshot or production_readonly_passes_this_run_id_to_the_native_config"
```

Result before implementation: 8 failed, 2 passed. Failures identified the absent schema
confirmation, login-only false positives, missing strict acceptance gates, and missing config
run-token wiring.

GREEN commands:

```powershell
$env:PYTHONPATH='E:\Nautilus-Perps\.venv\Lib\site-packages'
python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider `
  --basetemp=.pytest-tmp-readonly-green2 -k "native_diagnostics_document_carries_only_counters_and_labels or native_snapshot_schema_requires_exact_confirmed_keys_and_types or production_readonly_support_requires_all_native_acceptance_evidence or production_readonly_support_fails_when_native_acceptance_evidence_is_missing or production_readonly_report_reads_a_matching_native_snapshot or production_readonly_passes_this_run_id_to_the_native_config"

python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider `
  --basetemp=.pytest-tmp-readonly-full1
```

Results: 10 passed, 198 deselected; then 208 passed. `git diff --check` on the owned app files
returned no whitespace errors. These are offline application tests with injected native
snapshots; they do not establish installed-wheel, host, credential, private-WS or production
acceptance. Candidate build/install and bounded live validation remain parent-owned.

## Production read-only privacy correction

The regression plants private-looking values independently in a runtime exception, cache
order id/status/amount fields, cache exception, account id and stop exception. It verifies the
value is absent from stdout, stderr, captured logs, the published `probe.json`/`meta.json`, and
the immutable run copies. It also verifies `unknown=1`, `no_trade=0`, `outstanding_orders=1`
and `cache_errors=1`, so withholding details cannot look like an empty account or no trade.

RED command (canonical venv, escalated because of the managed sandbox access boundary):

```powershell
E:\Nautilus-Perps\.venv\Scripts\python.exe -m pytest tests/test_ondo_probe.py -q `
  -p no:cacheprovider --basetemp=.pytest-tmp-readonly-privacy-red3 `
  -k "production_readonly_runtime_error_never_echoes_private_payload or production_readonly_withholds_stop_cache_and_order_details_but_keeps_counts"
```

Result before implementation: **2 failed, 208 deselected**. Both failures showed the planted
exception payload in the captured stream, including the traceback.

GREEN commands:

```powershell
E:\Nautilus-Perps\.venv\Scripts\python.exe -m pytest tests/test_ondo_probe.py -q `
  -p no:cacheprovider --basetemp=.pytest-tmp-readonly-privacy-green `
  -k "production_readonly_runtime_error_never_echoes_private_payload or production_readonly_withholds_stop_cache_and_order_details_but_keeps_counts"

E:\Nautilus-Perps\.venv\Scripts\python.exe -m pytest tests/test_ondo_probe.py -q `
  -p no:cacheprovider --basetemp=.pytest-tmp-readonly-privacy-full
```

Results: **2 passed, 208 deselected**; then **210 passed**. These remain offline injected-fake
tests. They prove application output redaction and count preservation, not native console
redaction, credentialed authentication, private WebSocket behavior or production acceptance.

## Parent independent native-log correction

Independent reviewer found that native LoggerConfig still allowed account/error output despite Python report sanitization. Production-readonly now forces native stdout/file levels OFF, bypass_logging=True, print_config=False; other modes retain requested logging. The new regression uses the actual native LoggerConfig class even when the operator requests DEBUG. Canonical venv RED:1 failed/210 deselected; GREEN full test file:211 passed in2.93s; diff check passed. Scoped rereview pending. Planned supervisor still discards child console as defense in depth.

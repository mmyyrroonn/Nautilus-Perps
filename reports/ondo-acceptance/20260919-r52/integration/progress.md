# Progress — app/native shutdown capability integration (R5.2)

Run id `20260919-r52-integration`. Owner: Pi execution agent, bounded task
`control/pi-app-integration-task.md`. App worktree `E:/Nautilus-Perps/.worktrees/ondo-r52-app`;
fork worktree `E:/nautilus_trader/.worktrees/ondo-r52-cleanup`.

Constraints honored: no cargo / maturin / stub generation / install; no factory wheel change; no
generated-stub hand edit; no commit/staging/merge/push; no credentials, private network or orders;
only `factories.rs` touched on the fork; app edits limited to `src/ondo_probe.py`,
`tests/test_ondo_probe.py`, `docs/ondo.md` and one narrow continuation-plan note.

## 2026-09-19 — reconnaissance

- Read the task, `AGENTS.md`, `CLAUDE.md`, the shutdown review (`review/shutdown-review-1.md`,
  conclusion **changes required**) and the cleanup `progress.md` / `README.md`. Native acceptance
  is still pending.
- Confirmed the gap: `ondo_probe.py` hardcoded `converging_stop_available=false`; the installed
  R5.1 wheel exposes no ordered-shutdown lifecycle (base `ff57243`).
- Confirmed the real factory class has no `supports_ordered_shutdown` attribute in the installed
  wheel, and that PyO3 getters appear at class level as `getset_descriptor` (so a class-level bool
  read fails closed, and an instance read is required for the candidate).

## 2026-09-19 — tests first (RED)

Added section 8 to `tests/test_ondo_probe.py`: 18 cases (5 parametrized) covering

- old/missing attribute → unavailable, reason names the attribute;
- a capable factory (Python `property`, the PyO3 descriptor shape) → capability reported;
- `False` / `"true"` / `1` / `0` / `None` → fail closed;
- raising getter, factory construction failure, raising class-level lookup → fail closed;
- detection constructs only the factory, no config/client/import/credentials;
- absent attribute decided without constructing the factory;
- dry-run carries capability with no client/node and publishes nothing;
- capable wheel does **not** promote `protocol_verified`, `exit_code_zero_means_clean_account`,
  per-run cancel/confirm/release, `orders_submitted_by_probe` or `synthetic`;
- meta carries capability.

RED:

```
python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider
# 11 failed, 118 passed
```

Evidence `red-capability-tests.txt`.

## 2026-09-19 — implementation (GREEN)

`src/ondo_probe.py`:

- `NATIVE_SHUTDOWN_CAPABILITY_ATTRIBUTE = "supports_ordered_shutdown"` and capability source
  constants; `ShutdownCapability`; `detect_ordered_shutdown_capability(adapter)`;
  `_dry_run_capability(adapter)`.
- `plan_document`, `report_document`, `converging_stop_document`, `unverified_document` accept the
  capability; `execute` and `main` wire it in after the credential/adapter gate; meta and exit-0 log
  updated.
- Docstring item 2 rewritten: capability is an installed-wheel fact, not a venue verdict and not a
  cleaned account.

`crates/adapters/ondo/src/python/factories.rs` (fork, own file only):

```rust
#[getter]
#[pyo3(name = "supports_ordered_shutdown")]
#[must_use]
pub const fn py_supports_ordered_shutdown(&self) -> bool { true }
```

`docs/ondo.md` §opening item 2, §4 exit-code note and group table, §6 DMS row, new §6.1, §8 bullet.
One narrow R5.2 note in the continuation plan.

GREEN:

```
python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider
# 130 passed

python -m pytest tests -q -p no:cacheprovider
# 834 passed, 97 subtests passed, 1 warning
```

Evidence `green-probe-tests.txt`, `green-full-app-tests.txt`.

## 2026-09-19 — installed R5.1 fallback evidence

Offline real-CLI dry run (imports the installed adapter, constructs no client, no network):

```
python src/ondo_probe.py --mode public --symbols NVDA --dry-run
```

`supports_ordered_shutdown=false`, source `attribute-absent`, `converging_stop_available=false`,
`protocol_verified=false`, `exit_code_zero_means_clean_account=false`, `client_constructed=false`.
Evidence `installed-r5-dry-run.json`.

## 2026-09-19 — Astra review correction (2nd pass)

Review `control/pi-integration-review.md` required two factual corrections; first-pass evidence
kept.

1. **Zero submitted orders does not imply no DMS release.** A trading-mode sandbox session arms
   DMS on connect and can release it on a clean disconnect with zero orders; a restored journal
   may carry prior owned orders. The probe has no native `StopReport` telemetry, so the per-run
   cancel/confirm/release are now `null` (unobserved), never `false` (did not occur):
   `bounded_stop` outcome flags, `converging_stop_document`, and the `unverified` item (now with
   `per_run_actions_observed: false`). `observed_this_run` stays `false`. public/paper/
   account-readonly no-side-effect banner facts and the capability/observation split are
   unchanged. Added `test_zero_orders_does_not_prove_no_dms_release`.
2. **`PROTOCOL_VERIFIED_REASON` narrowed.** It no longer says no request ever reached the venue
   (public reads/recordings are observed); it now denies host-confirmed private/sandbox protocol
   acceptance. `protocol_verified` stays `false`. Added
   `test_protocol_reason_does_not_deny_the_observed_public_reads`.

Also updated the module docstring, the exit-0 log line, and `docs/ondo.md` opening items 1/2,
§4 group table and exit-code note, §6.1.

GREEN (correction pass):

```
python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider
# 132 passed

python -m pytest tests -q -p no:cacheprovider
# 836 passed, 97 subtests passed, 1 warning
```

Evidence `green-probe-tests-correction.txt`, `green-full-app-tests-correction.txt`,
`installed-r5-dry-run-correction.json`. First-pass `red-capability-tests.txt`,
`green-probe-tests.txt`, `green-full-app-tests.txt` preserved. No cargo/build run.

## 2026-09-19 — final

- Changed paths (app): `src/ondo_probe.py`, `tests/test_ondo_probe.py`, `docs/ondo.md`,
  `docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md`.
- Changed paths (fork, own file): `crates/adapters/ondo/src/python/factories.rs`.
- No commit/staging. `AGENTS.md` untracked in the app worktree predates this task and was not touched.
- Remaining dependency: native review is **changes required**; after it passes, build the candidate
  wheel and confirm the generated `.pyi` contains
  `@property def supports_ordered_shutdown(self) -> bool: ...` under `OndoExecutionClientFactory`,
  then run the runtime checks in `README.md` §6. No native acceptance is claimed here.

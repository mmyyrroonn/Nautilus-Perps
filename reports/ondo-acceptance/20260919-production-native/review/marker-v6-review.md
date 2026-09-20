# Independent v6 capability-marker review

Date: 2026-09-20 Asia/Shanghai.

## Verdict

**PASS for the two-file v5-to-v6 capability-marker change.** No blocking defect was found in this
scope. The new current-turn user instruction to proceed with mainnet testing resolves the earlier
implementation-only authorization gap for changing this metadata marker. It is authorization
evidence, not evidence that the adapter connected, armed DMS, submitted an order, received a fill,
closed exposure, or made money.

This review did not edit fork source, run tests or builds, access credentials, or use the network.
It independently inspected the frozen manifests, the two changed files, the recorded test/format
logs, and the applicable default-off configuration gates. A read-only scoped `git diff --check` was
also clean.

## Frozen identity and change scope

- Every entry in `native-trade/source-sha256-v6.json` was hashed against
  `E:/nautilus_trader/.worktrees/ondo-production-trade`: **63 checked, zero mismatches**.
- Both v5 and v6 manifests contain 63 paths. Comparing their hashes yields exactly two changes,
  matching `native-trade/changed-since-v5-v6.json`:
  - `crates/adapters/ondo/src/python/factories.rs`
  - `crates/adapters/ondo/tests/python.rs`
- The reviewed patch changes `py_supports_production_trade_envelope()` from `false` to `true` and
  changes the Python assertion from checking only that bool extraction succeeds to unwrapping and
  asserting the extracted bool value. The current source returns `true`; the current test therefore
  fails on `false` and requires True.
- No config, transport, execution, reconciliation, DMS, journal, rate-limit, order-envelope, or
  cancellation source changed between v5 and v6.

## Safety-boundary check

The marker advertises that the reviewed native production-envelope interface exists; it does not
turn on production orders by itself.

- `OndoExecutionClientConfig.allow_production_orders` still uses the Rust builder default for bool,
  so it remains `false` by default (`config.rs:322-324`). The Python constructor still defaults the
  argument to `None` and falls back to that same false default (`python/config.rs:153-169,190-212`).
- Production-trading scope still requires explicit `allow_production_orders=true` and a present,
  valid execution envelope (`config.rs:453-468`).
- Envelope validation still rejects any non-production environment, read-only account, or missing
  explicit write opt-in, and still requires expected account identity, run token, durable journal,
  bounded DMS, one-second reconciliation, and a valid immutable envelope
  (`config.rs:471-509`). General config validation retains the same fail-closed combination checks
  (`config.rs:522-540`).

The earlier v5 independent review already cleared N1-N6 for source/offline scope. The two v6 files
do not alter those corrected paths or weaken their gates.

## Recorded verification evidence

- `native-trade/full-native-python-v6.txt` contains 11 successful result blocks totaling
  **1,031 passed, zero failed**: 470 + 77 + 44 + 77 + 43 + 109 + 19 + 9 + 119 + 63 + 1.
- `native-trade/marker-python-green.txt`: **9 passed, zero failed**.
- `native-trade/marker-factory-green.txt`: **1 passed, zero failed** for the production factory run
  binding/journal-reuse regression.
- `native-trade/fmt-check-v6.txt` contains only the existing stable-toolchain warnings about nightly
  rustfmt options and no formatting diff. The reviewer-run scoped `git diff --check` produced no
  output and exited zero.
- The existing `CARGO_BUILD_WARNINGS=allow` Windows exception remains; this evidence is not a
  strict-warning or clippy pass.

## Remaining gates and limitations

The existing wheel and installed candidate described in `build/trade-candidate-acceptance.md` were
built from v5 with the marker still false. A fresh v6 candidate must be rebuilt, its generated
bindings/stubs and installed interface reverified, and the app gate retested before any live run.
The recorded local `C:/ProgramData/miniconda3/zlib.dll` dependency also remains a portability
limitation.

No production connection, credential read, DMS activation, order, cancellation, fill, close, or
financial outcome occurred in this review. Before a live action, the owner must still bind the
50-USDC account to a current fully priced plan and verify that the selected limits, market evidence,
identity, journal, whole-account reconciliation, and DMS state satisfy the unchanged gates. These
are execution-stage acceptance items, not blockers to accepting the two-file marker change.

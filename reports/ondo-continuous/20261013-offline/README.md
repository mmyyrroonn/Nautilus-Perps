# Ondo continuous execution: offline acceptance

Issue: `mmyyrroonn/nautilus_trader#122`. Recorded 2026-10-13.

The native continuous execution implementation and its application integration are
complete for this local candidate. All venue endpoints in this acceptance were
numeric loopback services using synthetic credentials. No mainnet run is claimed.

## Delivered behavior

- Explicit native continuous Config / Factory / ExecutionClient and generated Python
  exports/stubs. Existing sandbox, read-only and bounded-probe defaults remain valid.
- Multiple configured perpetual instruments and repeated IOC/GTC/post-only cycles;
  owned cancellation and exact reduce-only net-position exits. Native amend remains
  unsupported; replacement waits for confirmed cancellation and uses a new identity.
- Durable pre-dispatch intent, original-ID queries after an unknown response, fill
  and fee deduplication, complete account reconciliation, DMS proof and residual-aware
  shutdown/restart. Verified reduction can remain available while new risk is blocked.
- Current native DataEngine quotes reach send-time admission after REST budget waits;
  order body, limit price and the two-second freshness bound remain unchanged.
- The same LiveNode and Strategy perform BTC startup checks and then continue trading
  BTC and TSLA through ExecutionEngine, native Cache and Portfolio.

The source is in `E:/persarb/worktrees/issue122-ondo-continuous` and the application is
in `E:/persarb/worktrees/issue122-ondo-app`. Changes are local and uncommitted. The
source archives preserve the tracked patch and exact changed/untracked files;
`acceptance.json` records their SHA-256 values and file manifests.

## Candidate identity

- Native base: `f2965180f074dc12fbc1c45509465f2d551541ac` plus the recorded source diff.
- App base: `18d72503f6bf0e89efe0a6f4b61b861a4a2a4ed5` plus the recorded source files.
- Wheel: `E:/persarb/_tmp/issue122-acceptance/wheel-v2/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`.
- Wheel SHA-256: `6d63d32c70e42bef4a5bd783887f889b7c222f18fe14d6ef00025f42edd863de`.
- Installed binary SHA-256: `53c6ea7e9436b1686297ee5baec62852df9dfafbc553a1483a08b874dbe139e4`.
- Interpreter: `E:/persarb/_tmp/issue122-acceptance/venv/Scripts/python.exe`.
- Toolchain: CPython 3.12.9, uv 0.12.6, Rust 1.98.0, maturin 1.15.0.
- Profile: `nextest`, local Windows candidate. This is not a published cross-platform release.

`native-provenance.json` binds the unchanged source fingerprint, build inputs,
wheel, embedded native binary and adapter stubs. `installed.json` verifies the exact
installed wheel. The final application source fingerprint matches installation.
The earlier `wheel/` candidate in the temporary run directory is superseded by
`wheel-v2/`; only the latter passed the installed continuous execution acceptance.

## Results

| Scope | Passed | Evidence |
|---|---:|---|
| Native unit | 534 | `native-final.log` |
| Existing native integration targets | 604 | `native-regression.log`; execution target rechecked in `native-final.log` |
| Native continuous transport | 9 | `native-final.log` |
| Native Python projection | 9 | `native-python.log` |
| Existing application probe/operator | 256 | Original combined run: `E:/persarb/_tmp/issue122-app-tests.log` |
| Installed continuous application | 12 | `continuous-junit.xml`, `continuous.log` |

The original application run passed its 256 existing cases and 10 limit/configuration
cases but exposed two new integration failures. After fixing the new constructor and
send-time quote handling, the continuous target passed all 12 cases. Passing existing
targets were not repeatedly rerun. The final totals are 1,156 native and 268 application
cases, with no skips in these selected targets.

`startup-and-continuous.json` records eight native creates, two confirmed cancels,
six actual native fill events, 0.06 USDC commission and 4999.94 USDC final free balance.
BTC and TSLA each match Cache quantity, Portfolio net quantity and confirmed fills
while open; both are flat at completion. Shutdown is complete with no unknown
submissions or owned open orders. This is synthetic account evidence, not strategy PnL.

`cancel-fill-race.json` records the original maker filling during cancellation. The
application refuses replacement and retains the original journal and 0.0001 BTC
residual. Native shutdown raises an unresolved-work result instead of declaring flat.

## Commands

Native commands ran in the native worktree, with `CARGO_TARGET_DIR` set to
`E:/persarb/.backpack-target/native`. Windows builds used scoped
`CARGO_BUILD_WARNINGS=allow` for MSVC import-library notices; Rust compiler warnings
and lints were not globally disabled.

```text
cargo +nightly fmt -p nautilus-ondo
python python/generate_stubs.py
cargo test -p nautilus-ondo --profile nextest --locked --offline --lib --test execution --test continuous_execution
cargo test -p nautilus-ondo --profile nextest --locked --offline --test execution --test http_client --test http_contract --test market_data --test private_runtime --test production_envelope --test reconciliation --test signing
cargo test -p nautilus-ondo --profile nextest --features python --test python --locked --offline
python scripts/adapter-evidence/build_native.py --python E:/persarb/nautilus_trader/.venv/Scripts/python.exe --native-root E:/persarb/worktrees/issue122-ondo-continuous --output-dir E:/persarb/_tmp/issue122-acceptance/wheel-v2 --profile nextest
```

Stub generation used the exact native interpreter, `NAUTILUS_STUB_PROFILE=nextest`,
and its Scripts and CPython base directory on PATH. The application was created
with `uv sync --locked --inexact --managed-python --python 3.12.9`, using an explicit
`UV_PROJECT_ENVIRONMENT`, then installed through `scripts/install_native.py
--local-override` with the exact wheel, SHA-256, provenance and native source root.

Final application command, with the four exact candidate variables documented in
`docs/ondo-continuous.md` set to the `wheel-v2` artifacts:

```text
E:/persarb/_tmp/issue122-acceptance/venv/Scripts/python.exe -m pytest tests/test_ondo_continuous.py -q --junitxml=E:/persarb/_tmp/issue122-acceptance/continuous-v2-junit.xml --basetemp=E:/persarb/_tmp/issue122-acceptance/pytest-continuous-v2
```

## Limits and saved prior work

Continuous native admission accepts up to USD 100 per order and an allocated Ondo
gross budget up to USD 500. The application must allocate this against its global
cross-venue cap. Long-running journal growth remains separate issue 89 work. A real
venue run must separately verify private protocol, DMS and execution behavior for
this exact candidate; these offline results do not replace that evidence.

Previously useful workspace changes were saved separately, without pushing:
native `eebe776249` (shutdown/account proof and safety request priority), app
`b94d2b1` (reviewed candidate and economic evidence) and `18d7250` (workspace paths
and candidate workflow). Other pre-existing drafts were left in their original directories.

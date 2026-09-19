# R5.2 release build and candidate validation

Run root: `E:/Nautilus-Perps/reports/ondo-acceptance/20260919-r52/build`.
Fork source worktree: `E:/nautilus_trader/.worktrees/ondo-r52-cleanup` (base `ff57243`).
App worktree: `E:/Nautilus-Perps/.worktrees/ondo-r52-app` (base `aef07e4`).
Deliverable wheel: `E:/nautilus_trader/dist-r52/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`.

Status: release wheel built from the frozen accepted native source, generated stub bundled,
dedicated candidate venv verified, full app tests green, offline dry-run correct, one bounded
public-only probe clean. No canonical main checkout or main venv was touched. All private/sandbox
auth and execution semantics remain unverified.

## 1. Frozen source identity

- Base HEAD: `ff57243f74d730b1ababca7ee8e7b37e7c840287` (`task/ondo-r52-cleanup`).
- Final tracked diff SHA256 (includes the generated Ondo stub): `d234f8f6ec9fe38d8f79e484e9e315a56711694e008c51637eeeb10e4336e1ef`.
- Final `git status --porcelain` in the fork worktree: 7 modified files, 0 untracked.
- The six accepted native paths were hashed at start and re-hashed after the build; all unchanged and
  equal to `review/final-native-review.md` (`00-source-hashes.txt`, `00-final-hashes.txt`):

| Path | SHA256 |
| --- | --- |
| `crates/adapters/ondo/src/execution.rs` | `4AA6A7234883004F64C8EE629E0168385E34BBFD541A2EE1F0FFF7A09C1C09B1` |
| `crates/adapters/ondo/src/reconciliation.rs` | `0AC8338632A5677D62E6531276A87D54DF946E7AC1CBBF2CB3F704E288488CD3` |
| `crates/adapters/ondo/src/websocket/private/stream.rs` | `055A0C511AC18BFBBE3373877D323DD5ECA9A1E6AC804AF1D3240A3699C8D9C8` |
| `crates/adapters/ondo/src/python/factories.rs` | `9D2DA4304B065460D659CC05CC668EE04C60CACCE3DE0DBAA05A3F72C515073D` |
| `crates/adapters/ondo/tests/private_runtime.rs` | `49C909163F18E47F51A215B404DA6A4EAB93B6C3BBA26B800F7E639D96B863A3` |
| `crates/adapters/ondo/tests/reconciliation.rs` | `7BC6E150FB9728E8A22C588A60D02CF814C37FA245E1703CDB873EA288309A03` |

## 2. Toolchain and caches

- rustc / cargo `1.98.0` (repo `rust-toolchain.toml`), clang `20.1.3`, uv `0.12.6`, maturin `1.15.0`.
- Build Python: uv-managed CPython `3.12.9` at `E:/nautilus_trader/.worktrees/ondo-r52-cleanup/.venv`.
- Release wheel cache: `E:/nautilus_trader/.worktrees/task-ondo-r5/target` (R5-compatible; same
  `Cargo.lock`).
- Stub generator cache/profile: `E:/nautilus_trader/target` with `NAUTILUS_STUB_PROFILE=nextest`
  (the repository's canonical stub profile; `Makefile` `CARGO_CI_PROFILE ?= nextest`). This separate
  cache kept the release cache intact, so the final wheel build reused the release artifacts.
- Features (from `python/pyproject.toml`): `extension-module, arrow, betfair, high-precision,
  mimalloc, redis, postgres, defi, hypersync, tracing-bridge`.

## 3. Build steps

| Step | Command (summary) | Exit | Wall | Evidence |
| --- | --- | --- | --- | --- |
| uv sync | `uv sync --all-groups --all-extras --no-install-package nautilus-trader --inexact --managed-python --python 3.12` | 0 | 3s | `01-uv-sync.log/.status` |
| strict release | `uv run --no-sync maturin build --release --out dist-r52 -i <fork python>` | **1** | 16s | `02-strict-build.log/.status` |
| release build | same + `CARGO_BUILD_WARNINGS=allow` (this invocation only) | 0 | 1689s | `03-release-build.log/.status` |
| stubs | `NAUTILUS_STUB_PROFILE=nextest CARGO_BUILD_WARNINGS=allow uv run --no-sync python generate_stubs.py` | 0 | 295s | `04-stubs.log/.status` |
| stub normalization | CRLF -> LF on generated `.pyi` (43 files) | 0 | - | `04-stubs-normalize.log` |
| final wheel | same release build, now bundling the normalized stub | 0 | 5s | `05-final-wheel.log/.status` |

### Strict warning exception (retained, not hidden)

The strict release build stopped at the documented Windows platform exception in
`nautilus-persistence-macros`. Compilation had already started for several Nautilus crates;
the strict attempt did not produce a completed native wheel:

```
warning: linker stdout: 正在创建库 ...nautilus_persistence_macros-....dll.lib 和对象 ....dll.exp
  = note: `#[warn(linker_messages)]` on by default
  = note: the `linker_messages` lint ignores `-D warnings`
error: `nautilus-persistence-macros` (lib) generated 1 warning
error: warnings are denied by `build.warnings` configuration
```

`CARGO_BUILD_WARNINGS=allow` was used per-invocation for the release build and the stub generator
only. No global warning configuration was changed; the scoped settings are recorded in the build
scripts. The release build then logged **0** lowercase `warning:` lines
(`03-release-build.status`).

## 4. Artifacts

- Final wheel: `nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`
  - bytes: `63,621,311`
  - SHA256: `4d19c5df5bec73391a760d4b8643e2f8eaa53ce183dca88ee917454e2b91d642`
  - bundled `nautilus_trader/_libnautilus.cp312-win_amd64.pyd`: `165,002,752` bytes
  - bundled Ondo stub contains `class OndoExecutionClientFactory` and
    `def supports_ordered_shutdown(self) -> bool: ...` (`05-final-wheel-stub-check.txt`)
- Superseded intermediate wheel (release build before stub bundling): SHA256
  `4c0cac31b01414a57436617868e38cc054e8853b754f3393bd3802562ae7d70a` (`03-intermediate-wheel.txt`).
- Unbundled runtime dependency: `zlib.dll` (resolved at build time from
  `C:\ProgramData\miniconda3\zlib.dll`, not copied into the wheel). The candidate import still
  succeeds with conda stripped from the child `PATH` (`06-verify-candidate.json`). No extra DLL
  installation was needed for the checks performed; other runtime paths and portability are not
  established by this import check.

## 5. Generated stub

- Only semantic change: `python/nautilus_trader/adapters/ondo/__init__.pyi` (+2 lines), the
  read-only `supports_ordered_shutdown` property on `OndoExecutionClientFactory`.
  Diff SHA256 `33c5cf784f3a671b8399d94cc6e47da755f1b4d3ae44e2f416e25190d75d7898`.
- Generator-only CRLF noise on 43 `.pyi` files was normalized back to LF; after that
  `git status` under the stub tree shows only the Ondo stub (`04-stubs-git-after.txt`). No `.rs`
  file was changed by `generate_docstrings.py`.

## 6. Candidate validation

Dedicated venv `E:/Nautilus-Perps/.worktrees/ondo-r52-app/.venv` (CPython 3.12.9), built from the
exact final wheel plus the app/test dependency set. All checks passed (exit 0,
`06-verify-candidate.json`):

- `direct_url.json` -> `file:///E:/nautilus_trader/dist-r52/...win_amd64.whl`.
- Imported package path is the candidate venv, not conda or the main venv.
- `OndoExecutionClientFactory` / `OndoExecutionClientConfig` / `OndoDataClientFactory` / `LiveNode`
  import; Ondo `__all__` is the unchanged 8-name set.
- `OndoExecutionClientFactory().supports_ordered_shutdown` is the exact boolean `True`
  (`type is bool`), exposed as a read-only `getset_descriptor`.
- Installed generated stub declares the read-only `bool` property.
- Import succeeds with conda stripped from the child `PATH` (system `PATH` not modified).

## 7. Tests, dry-run and public probe

- Full app tests from the app worktree with the candidate interpreter and
  `PYTHON_DOTENV_DISABLED=1`:
  `836 passed, 1 warning, 97 subtests passed`, exit 0 (`06-app-tests.log`). The single warning is
  the pre-existing `tests/test_maker_live.py::test_limits` `PytestReturnNotNoneWarning`.
- Offline public dry-run (`07-dry-run.json`, exit 0, no client/credentials/network):
  `converging_stop_available: true`, `supports_ordered_shutdown: true` (source
  `native-factory`), `protocol_verified: false`, `exit_code_zero_means_clean_account: false`,
  `client_constructed: false`, `requests_sent: 0`, per-run `cancels_own_orders` /
  `confirms_cancels` / `releases_dead_mans_switch` all `null`.
- Bounded public-only probe, NVDA/TSLA, `--minutes 2`, hard external timeout 180s, `.env` loading
  disabled and `ONDO_*` variables removed by name from the child (`08-public-probe.*`):
  exit 0, wall `131s` (timeout not hit), `complete: true`, run
  `20260919T111054953616Z`, `session.exec_client: null` (public mode registered no execution
  factory; only `DataClient-ONDO`), `orders_submitted_by_probe: 0`, `outstanding_orders: []`,
  `write_capable: false`, `dms_armed: false`, capability `true`, `protocol_verified: false`,
  per-run actions `null`.

## 8. Rollback

Previous accepted wheel (R5, `E:/nautilus_trader/dist-r5`):
`82a3953c6c6c8bc39c0e2c269bbb7002c5bf54165354295a16109d5cac71a7a9`.
To roll back, point the app worktree dependency back at the `dist-r5` wheel path and reinstall.
`dist-r52` is a new directory; `dist-r5` was not overwritten.

## 9. Limits / not accepted here

- No private or sandbox mode was run; no credentials were read; no orders, cancels, DMS or funds.
- REST auth headers, WS login signature order, real private frame shapes and DMS renewal/release
  semantics remain documented, not host-confirmed. `protocol_verified` and
  `exit_code_zero_means_clean_account` are `false` by construction.
- The public probe validated installation and lifecycle only; it is not a new long market
  acceptance.
- Canonical main source/venv integration is Astra's final gate and was not performed here.

## 10. Subsequent canonical integration

After this build task finished, Astra completed the canonical source and main-venv integration and
independently reran the full app suite (836 passed, 97 subtests). See
[`../review/canonical-sync.md`](../review/canonical-sync.md) and the runtime identity JSON linked
there. The earlier statements about untouched canonical checkouts describe the build task's scope.

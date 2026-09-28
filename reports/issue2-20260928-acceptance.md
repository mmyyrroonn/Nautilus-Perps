# Issue 2 native candidate acceptance, 2026-09-28

This record distinguishes a local source-bound build from a published dependency.
It does not authorize a live order or change any historical acceptance result.

## Input identity

- Native source: `mmyyrroonn/nautilus_trader` commit
  `3831bda220692664df925c750507285b7424608b`, tree
  `139ffd9eb0949a54850253d61bd050cffbc57cc6`. Separate clean
  Windows and WSL/Linux checkouts were used; both reported no source changes
  to the native build tool.
- Native Cargo.lock SHA-256:
  `53bc4722129e781288afe62478dd5170fb56b6ce2627230199ed49254535a156`.
- CPython 3.12.9, uv 0.12.6, Rust 1.98.0, maturin 1.15.0.
  The maturin features come from `python/pyproject.toml`. Every output
  provenance records the selected profile, source fingerprint, wheel, native
  binary and adapter stub hashes.
- The application input is `pyproject.toml` + `uv.lock` for Python packages
  and a separate native candidate manifest for the wheel. App Git identity
  hashes code, config, tests and lock inputs; result JSON under ignored
  `.native-cache/` is outside that fingerprint.

## Windows local candidate

- The controlled `nextest` build from the clean native checkout produced
  `nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`,
  SHA-256 `dbc9f32cd6f598e6c3c5533e77a081a83f18335254b1bf08a9e32705ff78add1`.
  Its provenance has `source_binding=verified`, `dirty_count=0` and the
  builder file hash equals the file used by that build.
- An isolated uv-managed CPython 3.12.9 environment installed this wheel.
  The installer verified wheel origin, interpreter, import location, embedded
  and installed native binary hashes, Aster/Ondo stubs, ABI and Ondo capability.
  A later `uv sync --locked --inexact` preserved the installed fork wheel.
- Full offline application run on that exact wheel: **1188 passed, 97 subtests
  passed**. No venue connection was used. With PATH restricted to the venv
  and Windows system directories (no Conda directory), native import and Ondo
  production-envelope capability succeeded with exit 0.
- Wrong wheel SHA, wrong interpreter, stale native checkout and an older
  `source_binding=unknown` provenance each refused with exit 2. Unit
  regressions also cover wrong ABI, altered embedded binary, dirty source
  promotion, uv shadow dependency and report/source fingerprint separation.
- Both release provenance files record builder SHA-256
  `79aa0a9cecb1d8da6edd6a87ae992aafbed8acb4a9f9155f443f15820f1baa28`.
  After these local builds, an import-order-only Ruff fix changed the working
  builder file hash to
  `f788a93ffb525cf35b0ebf59d963de3676c6c5ff15b42fdb7e3785da10da382b`.
  The release wheels are identified by their recorded builder bytes; the
  hosted workflow must build fresh artifacts from the committed builder.
- The first strict Windows attempt failed on the known MSVC import-library
  `linker_messages` warning under Cargo `build.warnings=deny`. The builder
  then scoped `CARGO_BUILD_WARNINGS=allow` to its maturin subprocess and
  recorded the exception; source and repository lint settings were not edited.

## Release profile and Linux

The Linux `release` build from the same clean native commit produced
`nautilus_trader-2.0.0rc4-cp312-cp312-manylinux_2_39_x86_64.whl`,
SHA-256 `cd62c6f2cd440189cd772f427e9a8bd0fbb50ac50261fcf62fa9037c75a9d75f`.
The embedded/imported native binary SHA-256 was
`a5cdc94737ac9ea4149c7ba0631fcd2542c0ea5a6987c1a073436bd319b505dc`.
Its source binding is verified, native dirty count is zero, and its features
match the Windows build. In an isolated uv-managed Linux CPython 3.12.9
environment, the installer checked the wheel origin, actual ABI/platform,
imported binary, stubs and Ondo capability. Full offline application tests
passed: **1190 passed, 97 subtests passed** (exit 0). This WSL Ubuntu 24.04
test is local; the new hosted Ubuntu runner has not run. The wheel's
`manylinux_2_39_x86_64` tag limits this candidate to glibc 2.39+ x86_64.

The Windows `release` build from the same clean native commit produced
`nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`, SHA-256
`df6fa316a862e3cb21ff84913f70ffcccdefa4f3c87e9bfa4dc1b6861395967a`.
The embedded/imported native binary SHA-256 was
`00b184088e1742d39d1b5fd6deb3bbeabe54b9bb1e710d9dbfc3553a26baec01`.
The native commit, builder SHA-256, features and `release` profile match
the Linux artifact. Its source binding is verified and native dirty count
is zero. The isolated application CPython 3.12.9 installation verified the
wheel origin, imported binary, stubs, ABI and Ondo capability. Full offline
application tests passed: **1190 passed, 97 subtests passed** (exit 0).
With PATH restricted to the venv and Windows system directories, native
import and Ondo capability also succeeded (exit 0). This was a local
Windows host with Conda removed from PATH; the new clean hosted Windows
runner has not run.


## Result manifests

| Platform | Run ID | Config SHA-256 | Evidence |
| --- | --- | --- | --- |
| Windows amd64 | `8b6d4f66d24645718846282c6f133ca7` | `a2aa8283bb035b164538db9202528bd5cdc0ec04c09488ff090a679592bb2d21` | [provenance](issue2-20260928/windows-provenance.json), [installed identity](issue2-20260928/windows-installed.json), [integration result](issue2-20260928/windows-integration.json) |
| Linux x86_64 | `93600cc2ba724749a52a3c95e90149da` | `a2aa8283bb035b164538db9202528bd5cdc0ec04c09488ff090a679592bb2d21` | [provenance](issue2-20260928/linux-provenance.json), [installed identity](issue2-20260928/linux-installed.json), [integration result](issue2-20260928/linux-integration.json) |

Both integration records name their test collection, exact command, exit code,
application source-scope identity and the installed native wheel/binary hash.
The final Windows and Linux records have the same application tracked-content
SHA-256 `13c45f257f0194c9e8de1a0d167f709fe3a0b8bb4c2c15e64f8d6e47ac4b7a1d`
and untracked-content SHA-256
`af2a21ae373b5e23b72903a147a8c654cfdbedb35c954e324dffdb346540efb1`.
The Git commit and tree match as well.
No wheel binary or credential is in Git. The six JSON evidence files were
scanned for private-key and token shapes with zero matches.

## Historical evidence and promotion boundary

The 2026-09-23 BTC candidate identity, its 49-file source manifest and
`8f20735b299ef204464d8a104ca9fdbcec92a5b20d1a5795f474818d56e12985`
wheel SHA remain unchanged. Their old absolute paths and matching
`2.0.0rc4` version cannot establish a relationship to these new binaries.
No later commit association is made without first checking the frozen source
files. The 2026-09-28 earlier inventory with `source_binding=unknown` also
remains an inventory, not a promoted binary.

The staged GitHub Actions matrix checks a full native SHA on clean hosted
Windows and Ubuntu runners without venue secrets. It has not run because
the local code has not been committed or pushed. No stable, published
Windows/Linux wheel URLs exist yet, so `config/native-candidate.lock.json`
has not been written. Creating a lock with invented or expiring URLs would
misrepresent a portable dependency. The installer fails closed without a
formal lock or explicit local override. The formal promotion gate is:
commit the builder/workflow, run the hosted matrix, publish the reviewed
artifacts, generate and verify the two-platform URL/SHA lock, then retest the
download installation path. Keep a previous accepted lock for rollback; no
PyPI equal-version fallback is allowed.

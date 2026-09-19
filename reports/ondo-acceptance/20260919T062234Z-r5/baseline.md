# R5.1 baseline (recorded before any R5 change, from the MAIN checkouts)

Recorded by the main session on 2026-09-19 before dispatching the build.

## Repositories

| repo | branch | HEAD |
|---|---|---|
| app  E:/Nautilus-Perps            | main        | 29a0adf (reports: record the R4 follow-up...) |
| fork E:/nautilus_trader           | onde-perps  | 1057738 (ondo: fix a stop-test race...) |

## Currently installed wheel (app main venv)

- File: `E:/nautilus_trader/dist-r4/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`
- sha256: `2e7b0b6e0511b08fe19628b81b17a25497d939de1aa72bb44c776fd4740498f0`
- Built: R4, `maturin build --profile nextest` (unoptimized), fork HEAD 4356f1f
- This is the rollback target for R5.1.

## Main-venv baseline test run

```
$ .venv/Scripts/python.exe -m pytest -q -p no:cacheprovider
816 passed, 1 warning, 97 subtests passed in 12.38s
```

(The 1 warning is the known `test_maker_live.py::test_limits` returns-a-value issue.)

## Toolchain (same for the worktree build)

| tool | version |
|---|---|
| rustc / cargo | 1.98.0 (pinned by fork rust-toolchain.toml) |
| clang | 20.1.3 |
| uv | 0.12.6 |
| Python (app venv) | 3.12.9 |

## Known platform exception (drives R5.1 task 2)

`.cargo/config.toml` sets `build.warnings = "deny"`; Rust 1.98's new `linker_messages` lint
(which ignores `-D warnings`) treats the MSVC linker's normal stdout message ("Creating
library...", Chinese locale text) as a warning. On any fresh link of the proc-macro dependency
`nautilus-persistence-macros` the strict build fails with exit 101 before any nautilus crate is
compiled. Prior evidence: `reports/ondo-acceptance/20260915T153817Z-r2/fork_python_feature.txt`
(three-point attribution: error names the dependency; zero `Compiling` lines; zero Cargo.toml
changes by the ondo phases). R5.1 re-captures this failure on a fresh target dir and builds the
release wheel with `CARGO_BUILD_WARNINGS=allow` **for the build invocation only**, clearly
labelled as not-a-strict-gate pass.

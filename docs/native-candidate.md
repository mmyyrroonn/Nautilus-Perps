# Native candidate dependency (Issue 2)

The native fork is an external binary dependency. `pyproject.toml` locks the
application's Python packages; `config/native-candidate.lock.json`, once
generated from published artifacts, locks the fork wheel separately. Neither
`uv sync` nor an equal `2.0.0rc4` version is permission to substitute PyPI's
upstream wheel. A missing candidate is an installation failure.

## Toolchain and source

- Native source: `mmyyrroonn/nautilus_trader` at a full commit SHA and tree.
- CPython 3.12.9, uv 0.12.6, Rust/Cargo 1.98.0, maturin 1.15.0.
- Native `Cargo.lock`, `python/uv.lock`, maturin features/profile and the
  builder digest are recorded by `scripts/adapter-evidence/build_native.py`.
- Use a clean native checkout for a formal candidate. A dirty checkout is
  recorded in the provenance and can only be installed as an explicit local
  experiment.
- The build tool lives only in the native repository. The application
  `scripts/build_native.py` delegates to it.

On a new host, check out the locked full native SHA. Install uv 0.12.6 and
uv-managed Python 3.12.9, then create the native environment from
`python/uv.lock`. For Windows, use `<native>\\.venv\\Scripts\\python.exe`;
for Linux, use `<native>/.venv/bin/python`. In the native `python/`
directory, run `uv sync --locked --all-groups --all-extras
--no-install-package nautilus-trader --inexact --managed-python --python
3.12.9` with `UV_PROJECT_ENVIRONMENT` set to the native `.venv`.
Then run:

```text
<app-python> scripts/build_native.py --native-root <native> --native-python <native-python> --output-dir <empty-output-dir> --profile release
```

The default Cargo build is locked and offline. Use `--online` only if the
locked Cargo registry is not cached. The builder removes Conda paths on
Windows and records its scoped MSVC linker-warning exception. A changed
source fingerprint during the build, wrong tool version or multiple wheels
fails the build.

## Install and test

Create the application environment with uv 0.12.6 and Python 3.12.9:

```text
uv sync --locked --inexact
<app-python> scripts/install_native.py --python <app-python> --lock config/native-candidate.lock.json --output .native-cache/installed.json
<app-python> scripts/test_integration.py --python <app-python> --wheel <verified-wheel> --provenance <verified-provenance> --sha256 <wheel-sha256> --output .native-cache/integration.json tests
```

The lock contains credential-free HTTPS URLs and SHA-256 values for the wheel
and provenance for Windows amd64 and supported Linux x86_64. The current
Linux wheel targets manylinux_2_39 (glibc 2.39+, including Ubuntu 24.04);
older glibc or another architecture is refused by platform tags. The installer selects the
wheel by the actual interpreter ABI and platform, verifies both downloads,
the source identity, embedded binary and stubs, then installs with
`uv pip install --python <app-python> --no-deps`. It verifies the installed
wheel origin, import path, binary hash and Ondo capability. `uv sync
--locked --inexact` can be repeated afterward: there is no PyPI native
dependency to overwrite the candidate, and `--inexact` preserves the
separately installed wheel. Re-run the install verifier after any environment
change. An editable native checkout never replaces compilation.

A local build has no published URL. To test it, explicitly pass
`--local-override --native-root <native> --wheel <wheel> --provenance
<native-provenance.json> --sha256 <wheel-sha256>` instead of `--lock`.
The local source fingerprint is checked at install time; changing the native
source requires a new build.

To create the formal input lock after publishing *both* platform artifacts,
run `scripts/lock_native_candidate.py` with one `--artifact
<provenance> <local-wheel-file> <wheel-https-url> <provenance-https-url>`
for each platform.
The generator requires matching clean source identities, disjoint platform
tags and both target platforms. It verifies local wheel bytes before writing
the lock. Keep the input lock separate from the install and integration
result JSON. A run result records its app Git identity, candidate hashes,
run ID, config hashes, test selection and outcome; the app commit cannot
recursively pin its own report.

## Evidence and rollback

The 2026-09-23 Ondo candidate identity and source-file manifest remain
historical evidence. Check those frozen source file hashes before associating
that wheel with a later commit. A matching package version or CRLF-only source
difference proves neither binary equality nor inequality. The old wheel SHA
`8f20735b299ef204464d8a104ca9fdbcec92a5b20d1a5795f474818d56e12985`
belongs to that historical run, not to current native main.

To roll back, restore a previously accepted candidate lock and its exact
published wheel/provenance pair, reinstall with the explicit interpreter and
repeat verification and offline tests. If that artifact is unavailable,
stop; never fall back to the equal-version PyPI package. Record native and
application SHAs and compatibility checks in a separate dependency-upgrade
change. No command here contacts a venue or authorizes a live order.

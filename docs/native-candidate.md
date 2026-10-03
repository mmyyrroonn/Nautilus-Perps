# Native candidate dependency (Issue 2)

The native fork is an external binary dependency. `pyproject.toml` locks the
application's Python packages; `config/native-candidate.lock.json` pins the published fork wheel separately. The active candidate is
[native-candidate-eeeb8eefc7](https://github.com/mmyyrroonn/nautilus_trader/releases/tag/native-candidate-eeeb8eefc7)
from native commit `eeeb8eefc7921b7f81cb611cb3c851c665cd1ff1`. Neither
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

## Joint two-repository acceptance (Issue 3)

The manual workflow `.github/workflows/joint-acceptance.yml` takes full, lowercase
`app_sha` and `native_sha` commit IDs. It resolves both immutable refs on each clean
`windows-latest` and `ubuntu-24.04` runner, runs the native adapter checks, invokes
the native repository's single wheel builder, installs that exact wheel, and audits
the application suite including the installed-wheel `LiveNode` loopback test.
Default jobs have no venue secrets or `.env`; the application test command runs
through `scripts/offline_guard.py`, which fails closed if its loopback-only guard
cannot be installed. Native dependency fetch and wheel build finish before that
guarded application test stage. The native check manifest, wheel provenance,
installed identity, pytest JUnit/collection logs, guard record, detailed joint
audit and job-layer summary are uploaded even when a later stage fails.

Only a run with both platform jobs passing, zero skipped application tests,
at least 1188 collected items and 97 passing subtests is Issue 3 workflow
evidence for this application candidate. The workflow records failures by
native checks, build, install, identity, collection, pytest or network guard
layer. The accepted pair is the two resolved SHAs plus the wheel SHA-256;
a later floating `main` is a different pair. Venue ACK and live trade
acceptance remain separate from this offline workflow.
## Backpack candidate checks (native issue 84)

The joint workflow uses the native repository's single `native_checks.py` entry
for Aster, Ondo, Backpack and Portfolio. Format, nextest, doctest and Python-feature
checks are independent blocking rows; the existing Clippy debt remains visible
as a nonblocking row. Native stdout/stderr files are uploaded with their hashes.

Both the installer and integration runner pass `--additional-adapter backpack`.
They compare the installed Backpack stub with the actual wheel and require its
public, readonly-account and restricted-loopback exports. The application source
manifest includes `.github` and a per-file SHA-256 map in addition to its full
commit/tree and aggregate content identity. Native provenance retains the full
commit/tree, Cargo/Python locks, build inputs and unchanged pre/post fingerprints.
The final audit independently reads the wheel to compare the native binary and
Aster/Ondo/Backpack stubs across build, install and integration evidence.

Required application tests include the installed Backpack extension identity,
public reconnect/stale-book behavior, readonly balances and fills, restricted
order ACK/fill/cancel behavior, unknown-submit ownership, and native refusal of
new risk without an HTTP write. The existing Aster LiveNode test is also required.
Missing tests or skipped tests fail the joint audit; no fixture substitutes for
the installed extension. All transport peers use owned numeric loopback addresses.

A local dirty experiment is useful for review but cannot pass the formal joint
checkout/source gates. After explicitly authorized commits, run the existing
Windows/Linux workflow with the new full SHA pair, then link its artifacts to
[native issue 84](https://github.com/mmyyrroonn/nautilus_trader/issues/84).
An unavailable platform or network guard is recorded as a failure, and earlier
wheel hashes or test counts cannot stand in for that run. This CI evidence does
not authorize venue accounts or production writes.

The optional `native_wheel_run_id` dispatch input reuses only the platform wheel,
unchanged native provenance and native build input from an earlier joint run.
Leave it empty for the normal controlled build. A donor must be a same-repository
`workflow_dispatch` run of this workflow whose app SHA is an ancestor of the
selected app SHA. Its matching platform build and install must have succeeded;
a later application test failure does not invalidate that built artifact.
The workflow waits up to 30 minutes for the platform artifact, checks its GitHub
archive digest, and refuses missing, expired, ambiguous or mismatched artifacts
without rebuilding as a fallback.

Reused wheels still pass the current native checkout fingerprint, wheel/binary/stub
checks and source manifest audit. Native checks, installation and the complete
guarded application suite run afresh for the selected full SHA pair. Donor test
records and counts are never imported. The existing `evidence/job-layer.json`
records the donor run/job, artifact identity and archive/wheel digests. The automatic
GitHub token is available only to the build step for repository artifact access;
no venue credentials are used.

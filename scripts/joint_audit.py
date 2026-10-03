"""Write the final machine-readable audit for one two-repository acceptance run.

This command is safe to call from an ``always()`` workflow step.  Every layer is recorded
even when an earlier command did not create its expected file, so a failed job leaves an
artifact that distinguishes checkout, source binding, build, install, network guard and
pytest failures.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import re
import subprocess
import tomllib
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
REQUIRED_LIVENODE_TEST = "tests/test_installed_wheel_livenode.py::test_installed_wheel_livenode_loopback_lifecycle"
REQUIRED_TESTS = (
    REQUIRED_LIVENODE_TEST,
    "tests/test_backpack_installed_wheel.py::test_installed_backpack_extension_matches_verified_wheel",
    "tests/test_backpack_installed_wheel.py::test_installed_restricted_authority_denies_new_risk_without_http_write",
    "tests/test_backpack_public_native.py::test_installed_native_messages_stale_gap_reconnect_and_bounded_stop",
    "tests/test_backpack_account_native.py::test_actual_readonly_node_balances_true_fills_and_no_writes",
    "tests/test_backpack_loopback_native.py::test_actual_strategy_ack_quote_stale_denial_fill_duplicate_cancel202_and_dirty_stop",
    "tests/test_backpack_loopback_native.py::test_unknown_post_is_not_rejected_retried_or_adopted_from_numeric_client_id",
)
REQUIRED_STUBS = tuple(
    f"nautilus_trader/adapters/{adapter}/__init__.pyi"
    for adapter in ("aster", "ondo", "backpack")
)
APP_SOURCE_SCOPE = (".github", "src", "scripts", "tests", "config", "pyproject.toml", "uv.lock", ".python-version")
BACKPACK_EXPORTS = (
    "BackpackDataClientConfig", "BackpackDataClientConfig.telemetry_snapshot_json",
    "BackpackDataClientFactory", "BackpackDataClientFactory.name", "BackpackDataClientFactory.capabilities_json",
    "BackpackExecutionClientConfig", "BackpackExecutionClientConfig.telemetry_snapshot_json",
    "BackpackExecutionClientFactory", "BackpackExecutionClientFactory.name", "BackpackExecutionClientFactory.capabilities_json",
    "BackpackLoopbackExecutionClientConfig", "BackpackLoopbackExecutionClientConfig.control",
    "BackpackLoopbackExecutionClientFactory", "BackpackLoopbackExecutionClientFactory.name", "BackpackLoopbackExecutionClientFactory.capabilities_json",
    "BackpackLoopbackControl", "BackpackLoopbackControl.begin_session", "BackpackLoopbackControl.accept_account",
    "BackpackLoopbackControl.refresh_market", "BackpackLoopbackControl.invalidate",
    "BackpackLoopbackControl.pending_fills_json", "BackpackLoopbackControl.telemetry_snapshot_json",
    "BackpackLoopbackControl.shutdown_report_json", "BackpackLoopbackSession", "BackpackLoopbackSession.generation",
    "BackpackCredential", "BackpackQuota", "BackpackQuota.shares_scope", "BackpackInstrumentEconomics",
    "BackpackLoopbackAccountFacts", "BackpackLoopbackExecutionAuthority", "BackpackPublicReplay",
    "BackpackPublicReplay.instrument", "BackpackPublicReplay.apply_record",
)


def _digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _safe_digest(path: Path) -> str | None:
    try:
        return _digest(path)
    except OSError:
        return None


def _file_record(path: Path | None) -> dict[str, object] | None:
    if path is None:
        return None
    try:
        if not path.is_file():
            return None
        return {"path": str(path), "bytes": path.stat().st_size, "sha256": _digest(path)}
    except OSError as exc:
        return {"path": str(path), "error": type(exc).__name__}


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _count(value: Any) -> int:
    return value if type(value) is int and value >= 0 else -1


def _zero(value: Any) -> bool:
    return type(value) is int and value == 0


def _hash(value: Any) -> bool:
    return isinstance(value, str) and SHA256.fullmatch(value) is not None


def _load(path: Path | None) -> tuple[dict[str, Any] | None, str | None]:
    if path is None:
        return None, "not supplied"
    try:
        if not path.is_file():
            return None, "record is missing"
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return None, f"record is unreadable: {type(exc).__name__}"
    if not isinstance(value, dict):
        return None, "record is not a JSON object"
    return value, None


def _git(root: Path, *args: str) -> str | None:
    if not root.is_dir():
        return None
    try:
        result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
    except OSError:
        return None
    if result.returncode:
        return None
    return result.stdout.strip()


def _env_files(root: Path) -> list[str]:
    excluded = {".git", ".venv", ".native-cache", ".worktrees", "__pycache__", "target"}
    result: list[str] = []
    if not root.is_dir():
        return result
    for path in root.rglob("*"):
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            result.append(str(path))
    return sorted(result)


def _layer(ok: bool, reason: str, **details: object) -> dict[str, object]:
    return {"status": "passed" if ok else "failed", "reason": reason, **details}


def _sha(value: str, name: str) -> tuple[str | None, str | None]:
    normalized = value.strip().lower()
    if not FULL_SHA.fullmatch(normalized):
        return None, f"{name} is not a full lowercase commit SHA"
    return normalized, None



def _fingerprint(identity: dict[str, Any]) -> str:
    fields = ("commit", "tree", "tracked_diff_sha256", "untracked_sha256", "cargo_lock_sha256")
    canonical = {field: identity.get(field) for field in fields}
    return hashlib.sha256(json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _native_identity_ok(identity: Any, root: Path, commit: str | None) -> bool:
    record = _object(identity)
    empty_digest = hashlib.sha256(b"").hexdigest()
    return (
        commit is not None
        and record.get("commit") == commit
        and record.get("tree") == _git(root, "rev-parse", "HEAD^{tree}")
        and isinstance(record.get("tree"), str)
        and FULL_SHA.fullmatch(record["tree"]) is not None
        and record.get("dirty") == []
        and _zero(record.get("dirty_count"))
        and record.get("tracked_diff_sha256") == empty_digest
        and record.get("untracked_sha256") == empty_digest
        and _hash(record.get("cargo_lock_sha256"))
        and record["cargo_lock_sha256"] == _safe_digest(root / "Cargo.lock")
    )


def _app_identity_ok(identity: Any, root: Path, commit: str | None) -> bool:
    record = _object(identity)
    scope = record.get("source_scope")
    if (
        commit is None or record.get("commit") != commit
        or record.get("tree") != _git(root, "rev-parse", "HEAD^{tree}")
        or not isinstance(record.get("tree"), str)
        or FULL_SHA.fullmatch(record["tree"]) is None
        or record.get("dirty") is not False
        or not isinstance(scope, list) or any(not isinstance(path, str) for path in scope)
        or len(scope) != len(APP_SOURCE_SCOPE) or set(scope) != set(APP_SOURCE_SCOPE)
    ):
        return False
    tracked = hashlib.sha256()
    untracked = hashlib.sha256()
    files: dict[str, str] = {}
    try:
        for options, accumulator in (((), tracked), (("--others", "--exclude-standard"), untracked)):
            result = subprocess.run(
                ["git", "ls-files", *options, "-z", "--", *scope],
                cwd=root, capture_output=True, check=False,
            )
            if result.returncode:
                return False
            for raw in sorted(filter(None, result.stdout.split(b"\0"))):
                name = raw.decode("utf-8", errors="surrogateescape")
                path = root / name
                if not path.is_file():
                    return False
                digest = _digest(path)
                accumulator.update(raw + b"\0")
                accumulator.update(bytes.fromhex(digest))
                files[name] = digest
    except (OSError, ValueError):
        return False
    return (
        record.get("source_files_sha256") == files
        and record.get("tracked_content_sha256") == tracked.hexdigest()
        and record.get("untracked_sha256") == untracked.hexdigest()
    )


def _wheel_inventory(wheel: Path | None) -> tuple[dict[str, str], dict[str, str]]:
    if wheel is None:
        return {}, {}
    try:
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
            if len(names) != len(set(names)):
                return {}, {}
            binaries = {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in names
                if name.startswith("nautilus_trader/") and name.count("/") == 1
                and name.endswith((".pyd", ".so"))
            }
            stubs = {name: hashlib.sha256(archive.read(name)).hexdigest() for name in REQUIRED_STUBS}
            return binaries, stubs
    except (OSError, KeyError, ValueError, RuntimeError, zipfile.BadZipFile):
        return {}, {}


def _inventory_rows(rows: Any) -> dict[str, str]:
    if not isinstance(rows, list) or not rows:
        return {}
    result: dict[str, str] = {}
    for row in rows:
        if (
            not isinstance(row, dict) or not isinstance(row.get("path"), str)
            or not _hash(row.get("sha256")) or row["path"] in result
        ):
            return {}
        result[row["path"]] = row["sha256"]
    return result




def _absolute_program(value: Any) -> Path | None:
    if not isinstance(value, str) or not value or "\0" in value:
        return None
    try:
        path = Path(value)
        return path.resolve() if path.is_absolute() else None
    except (OSError, ValueError, RuntimeError):
        return None



def _guard_binding_ok(
    guard: dict[str, Any], integration: Any, installed: Any,
    *, app_root: Path, native_root: Path, guard_path: Path | None,
) -> bool:
    integration = _object(integration)
    command = guard.get("command")
    runner_command = integration.get("runner_command")
    if (
        not isinstance(command, list) or len(command) < 2
        or any(not isinstance(argument, str) or not argument or "\0" in argument for argument in command)
        or not isinstance(runner_command, list) or command != runner_command
    ):
        return False
    interpreter = _absolute_program(command[0])
    candidate = _object(integration.get("candidate"))
    if (
        interpreter is None
        or interpreter != _absolute_program(_object(_object(installed).get("installed")).get("interpreter"))
        or interpreter != _absolute_program(_object(candidate.get("installed")).get("interpreter"))
        or _absolute_program(command[1]) != (app_root / "scripts" / "test_integration.py").resolve()
    ):
        return False
    network = _object(integration.get("network"))
    raw_guard = network.get("guard_record")
    if (
        network.get("policy") != "loopback-only"
        or network.get("enforcement") != "offline_guard.py"
        or guard_path is None or not isinstance(raw_guard, str) or not raw_guard or "\0" in raw_guard
    ):
        return False
    try:
        recorded_guard = Path(raw_guard)
        if not recorded_guard.is_absolute():
            recorded_guard = app_root / recorded_guard
        if recorded_guard.resolve() != guard_path.resolve():
            return False
    except (OSError, ValueError, RuntimeError):
        return False
    workspaces = guard.get("workspaces")
    if not isinstance(workspaces, list) or len(workspaces) != 2:
        return False
    roots = {_absolute_program(workspace) for workspace in workspaces}
    return roots == {app_root.resolve(), native_root.resolve()}


def _program_firewall_ok(guard: dict[str, Any], installed: Any, integration_candidate: Any) -> bool:
    enforcement = _object(guard.get("enforcement"))
    discovery = _object(guard.get("interpreter_discovery"))
    if (
        enforcement.get("interpreter_discovery") != discovery
        or discovery.get("status") != "verified"
        or not _zero(discovery.get("exit_code"))
    ):
        return False
    paths = {
        field: _absolute_program(discovery.get(field))
        for field in ("launcher", "executable", "base_executable", "process_executable")
    }
    if any(path is None for path in paths.values()):
        return False
    command = guard.get("command")
    if not isinstance(command, list) or not command:
        return False
    selected = paths["launcher"]
    if (
        paths["executable"] != selected or _absolute_program(command[0]) != selected
        or _absolute_program(_object(_object(installed).get("installed")).get("interpreter")) != selected
        or _absolute_program(_object(_object(integration_candidate).get("installed")).get("interpreter")) != selected
    ):
        return False
    programs = enforcement.get("programs")
    if not isinstance(programs, list) or not programs:
        return False
    blocked = {_absolute_program(program) for program in programs}
    return None not in blocked and set(paths.values()).issubset(blocked)


def _maturin_features(root: Path) -> str | None:
    try:
        config = tomllib.loads((root / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    features = _object(_object(config.get("tool")).get("maturin")).get("features")
    if (
        not isinstance(features, list) or not features
        or any(not isinstance(feature, str) or not feature for feature in features)
        or len(set(features)) != len(features)
        or "extension-module" not in features
    ):
        return None
    return ",".join(features)


def _candidate_ok(
    candidate: Any, *, provenance: dict[str, Any], provenance_path: Path | None,
    wheel: Path | None, wheel_digest: str | None, binaries: dict[str, str],
    stubs: dict[str, str], app_root: Path, native_root: Path,
    app_sha: str | None, native_sha: str | None,
) -> bool:
    candidate = _object(candidate)
    origin = _object(candidate.get("native_provenance"))
    installed = _object(candidate.get("installed"))
    exports = _object(_object(installed.get("adapter_exports")).get("backpack"))
    stub_hashes = _object(installed.get("adapter_stub_sha256"))
    declared_native = _object(provenance.get("declared_native"))
    return (
        candidate.get("strict_source_binding_passed") is True
        and _object(candidate.get("wheel")).get("sha256") == wheel_digest
        and _hash(wheel_digest)
        and wheel is not None and installed.get("direct_url") == wheel.as_uri()
        and len(binaries) == 1
        and installed.get("native_module_sha256") == next(iter(binaries.values()), None)
        and all(stub_hashes.get(name) == digest for name, digest in stubs.items())
        and set(stubs) == set(REQUIRED_STUBS)
        and all(exports.get(name) is True for name in BACKPACK_EXPORTS)
        and _app_identity_ok(candidate.get("app"), app_root, app_sha)
        and origin.get("source_binding") == "verified"
        and provenance_path is not None
        and _hash(origin.get("sha256")) and origin["sha256"] == _safe_digest(provenance_path)
        and origin.get("declared_native") == declared_native
        and _native_identity_ok(origin.get("declared_native"), native_root, native_sha)
        and origin.get("declared_build") == provenance.get("declared_build")
        and origin.get("source_fingerprint_sha256") == _fingerprint(declared_native)
        and origin.get("build") == provenance.get("build")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--app-root", type=Path, required=True)
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--app-sha", required=True)
    parser.add_argument("--native-sha", required=True)
    parser.add_argument("--wheel", type=Path)
    parser.add_argument("--provenance", type=Path)
    parser.add_argument("--installed", type=Path)
    parser.add_argument("--integration", type=Path)
    parser.add_argument("--offline-guard", type=Path)
    parser.add_argument("--native-checks", type=Path)
    parser.add_argument("--log", type=Path, action="append", default=[])
    args = parser.parse_args()

    app_root = args.app_root.resolve()
    native_root = args.native_root.resolve()
    app_sha, app_sha_error = _sha(args.app_sha, "app SHA")
    native_sha, native_sha_error = _sha(args.native_sha, "native SHA")
    app_actual = _git(app_root, "rev-parse", "HEAD")
    native_actual = _git(native_root, "rev-parse", "HEAD")
    app_dirty = _git(app_root, "status", "--porcelain", "--untracked-files=normal")
    native_dirty = _git(native_root, "status", "--porcelain", "--untracked-files=normal")
    app_env = _env_files(app_root)
    native_env = _env_files(native_root)

    provenance, provenance_error = _load(args.provenance.resolve() if args.provenance else None)
    installed, installed_error = _load(args.installed.resolve() if args.installed else None)
    integration, integration_error = _load(args.integration.resolve() if args.integration else None)
    guard, guard_error = _load(args.offline_guard.resolve() if args.offline_guard else None)
    native_checks, native_checks_error = _load(
        args.native_checks.resolve() if args.native_checks else None,
    )

    wheel = args.wheel.resolve() if args.wheel else None
    if wheel is None and provenance:
        raw_wheel = _object(provenance.get("wheel")).get("path")
        if raw_wheel:
            wheel = Path(str(raw_wheel))
    provenance_path = args.provenance.resolve() if args.provenance else None

    layers: dict[str, dict[str, object]] = {}
    checkout_ok = (
        app_sha_error is None
        and native_sha_error is None
        and app_actual == app_sha
        and native_actual == native_sha
        and app_dirty == ""
        and native_dirty == ""
    )
    layers["checkout"] = _layer(
        checkout_ok,
        "both checkouts match requested full SHAs and are clean"
        if checkout_ok
        else "checkout SHA or clean-source assertion failed",
        expected={"app": app_sha, "native": native_sha},
        resolved={"app": app_actual, "native": native_actual},
        dirty={"app": bool(app_dirty), "native": bool(native_dirty)},
        errors=[error for error in (app_sha_error, native_sha_error) if error],
    )
    env_ok = not app_env and not native_env
    layers["dotenv"] = _layer(
        env_ok,
        "no credential dotenv files found in either checkout" if env_ok else "credential dotenv file found",
        app_files=app_env,
        native_files=native_env,
    )

    raw_native_rows = native_checks.get("checks") if native_checks else None
    native_check_rows = raw_native_rows if isinstance(raw_native_rows, list) else []
    blocking_rows = [row for row in native_check_rows if isinstance(row, dict) and row.get("blocking")]
    required_checks = {"fmt", "test", "doctest", "python"}
    required_crates = {"nautilus-aster", "nautilus-ondo", "nautilus-backpack", "nautilus-portfolio"}
    checked_names = {row.get("name") for row in blocking_rows if isinstance(row.get("name"), str)}
    checked_crates = native_checks.get("crates") if native_checks else None
    native_checks_ok = (
        native_checks is not None
        and isinstance(raw_native_rows, list)
        and len(native_check_rows) == sum(isinstance(row, dict) for row in native_check_rows)
        and required_checks.issubset(checked_names)
        and isinstance(checked_crates, list)
        and all(isinstance(item, str) and item for item in checked_crates)
        and len(set(checked_crates)) == len(checked_crates)
        and required_crates.issubset(checked_crates)
        and _native_identity_ok(native_checks.get("native"), native_root, native_sha)
        and native_checks.get("identity_before_fingerprint") == _fingerprint(_object(native_checks.get("native")))
        and native_checks.get("identity_after_fingerprint") == native_checks.get("identity_before_fingerprint")
        and native_checks.get("identity_changed_during_checks") is False
        and all(_zero(row.get("exit_code")) for row in blocking_rows)
        and all(type(row.get("blocking")) is bool and isinstance(row.get("name"), str)
                and type(row.get("exit_code")) is int for row in native_check_rows)
        and all(row.get("blocking") is True for row in native_check_rows if row.get("name") in required_checks)
        and len(checked_names) == len(blocking_rows)
    )
    layers["native_checks"] = _layer(
        native_checks_ok,
        "native blocking checks passed against the requested SHA"
        if native_checks_ok
        else native_checks_error or "native check manifest failed",
        checks=native_check_rows,
        manifest=_file_record(args.native_checks.resolve() if args.native_checks else None),
    )

    provenance_wheel = _object(provenance.get("wheel")) if provenance else {}
    declared_native = _object(provenance.get("declared_native")) if provenance else {}
    wheel_digest = _safe_digest(wheel) if wheel else None
    binaries, stubs = _wheel_inventory(wheel)
    build = _object(provenance.get("build")) if provenance else {}
    declared_build = _object(provenance.get("declared_build")) if provenance else {}
    provenance_ok = (
        provenance is not None
        and provenance.get("source_binding") == "verified"
        and _native_identity_ok(declared_native, native_root, native_sha)
        and provenance.get("source_fingerprint_sha256") == _fingerprint(declared_native)
        and build.get("started_source") == declared_native
        and build.get("finished_source") == declared_native
        and build.get("cargo_lock_sha256") == declared_native.get("cargo_lock_sha256")
        and _hash(build.get("python_uv_lock_sha256"))
        and build["python_uv_lock_sha256"] == _safe_digest(native_root / "python" / "uv.lock")
        and _hash(build.get("maturin_config_sha256"))
        and build["maturin_config_sha256"] == _safe_digest(native_root / "python" / "pyproject.toml")
        and _hash(build.get("builder_sha256"))
        and build["builder_sha256"] == _safe_digest(native_root / "scripts" / "adapter-evidence" / "build_native.py")
        and isinstance(declared_build.get("features"), str)
        and declared_build["features"] == _maturin_features(native_root)
        and declared_build.get("profile") in ("release", "nextest")
        and len(binaries) == 1 and set(stubs) == set(REQUIRED_STUBS)
        and all(_inventory_rows(provenance.get("embedded_native_objects")).get(name) == digest
                for name, digest in binaries.items())
        and all(_inventory_rows(provenance.get("embedded_adapter_stubs")).get(name) == digest
                for name, digest in stubs.items())
        and wheel is not None
        and wheel.is_file()
        and _hash(wheel_digest)
        and provenance_wheel.get("sha256") == wheel_digest
    )
    layers["build"] = _layer(
        provenance_ok,
        "source-bound provenance and wheel digest match the requested native SHA"
        if provenance_ok
        else provenance_error or "build provenance, wheel, or source binding failed",
        wheel=_file_record(wheel),
        provenance=_file_record(provenance_path),
        native={
            "commit": declared_native.get("commit") if provenance else None,
            "tree": declared_native.get("tree") if provenance else None,
            "dirty_count": declared_native.get("dirty_count") if provenance else None,
        },
        build=provenance.get("declared_build") if provenance else None,
    )

    installed_wheel = _object(installed.get("wheel")) if installed else {}
    candidate_inputs = {
        "provenance": provenance or {}, "provenance_path": provenance_path,
        "wheel": wheel, "wheel_digest": wheel_digest, "binaries": binaries, "stubs": stubs,
        "app_root": app_root, "native_root": native_root, "app_sha": app_sha, "native_sha": native_sha,
    }
    installed_ok = (
        installed is not None and provenance_ok
        and ("exit_code" not in installed or _zero(installed.get("exit_code")))
        and _candidate_ok(installed, **candidate_inputs)
    )
    layers["install_identity"] = _layer(
        installed_ok,
        "installed distribution identity matches the built wheel" if installed_ok else installed_error or "installed identity failed",
        installed=_file_record(args.installed.resolve() if args.installed else None),
        wheel_sha256=installed_wheel.get("sha256") if installed else None,
        native_module_sha256=_object(installed.get("installed")).get("native_module_sha256") if installed else None,
    )

    enforcement = _object(guard.get("enforcement")) if guard else {}
    guard_ok = (
        guard is not None
        and guard.get("status") == "passed"
        and _zero(guard.get("exit_code"))
        and guard.get("policy") == "loopback-only"
        and _guard_binding_ok(guard, integration, installed, app_root=app_root, native_root=native_root,
                              guard_path=args.offline_guard.resolve() if args.offline_guard else None)
        and enforcement.get("kind") in ("network-namespace", "program-firewall")
        and (
            (enforcement.get("kind") == "network-namespace" and _zero(enforcement.get("probe_exit_code")))
            or (
                enforcement.get("kind") == "program-firewall"
                and _zero(enforcement.get("install_exit_code"))
                and guard.get("firewall_cleanup") is True
                and _program_firewall_ok(guard, installed, _object(integration).get("candidate"))
            )
        )
    )
    layers["offline_network"] = _layer(
        guard_ok,
        "offline test command ran under a verified loopback-only guard" if guard_ok else guard_error or "offline guard failed or was not proven",
        guard=_file_record(args.offline_guard.resolve() if args.offline_guard else None),
        enforcement=guard.get("enforcement") if guard else None,
        guard_status=guard.get("status") if guard else None,
    )

    pytest_record = _object(integration.get("pytest")) if integration else {}
    pytest_counts = _object(pytest_record.get("counts"))
    collection = _object(integration.get("collection")) if integration else {}
    required_tests = _object(collection.get("required_tests"))
    source_refs = _object(integration.get("source_refs")) if integration else {}
    skips = _count(pytest_counts.get("skipped"))
    test_ok = (
        integration is not None
        and integration.get("status") == "passed"
        and _zero(integration.get("exit_code"))
        and _zero(pytest_record.get("exit_code"))
        and _zero(collection.get("exit_code"))
        and provenance_ok and installed_ok
        and _candidate_ok(integration.get("candidate"), **candidate_inputs)
        and _object(integration.get("candidate")).get("app") == _object(installed).get("app")
        and _object(source_refs.get("app")).get("expected") == app_sha
        and _object(source_refs.get("native")).get("expected") == native_sha
        and _object(source_refs.get("app")).get("resolved") == app_sha
        and _object(source_refs.get("native")).get("resolved") == native_sha
        and not skips
        and _zero(pytest_record.get("skipped"))
        and _zero(pytest_counts.get("failed"))
        and _zero(pytest_counts.get("errors", pytest_counts.get("error")))
        and _count(collection.get("items")) >= 1188
        and all(required_tests.get(node) is True for node in REQUIRED_TESTS)
        and _count(pytest_record.get("item_passed")) >= 1188
        and _count(pytest_record.get("subtests_passed")) >= 97
    )
    layers["integration_tests"] = _layer(
        test_ok,
        "pytest passed with visible zero skips and recorded subtests" if test_ok else integration_error or "integration test audit failed",
        integration=_file_record(args.integration.resolve() if args.integration else None),
        counts=pytest_counts,
        collected=collection.get("items"),
        required_livenode_test=required_tests.get(REQUIRED_LIVENODE_TEST),
        required_tests={node: required_tests.get(node) for node in REQUIRED_TESTS},
        item_passed=pytest_record.get("item_passed") if isinstance(pytest_record, dict) else None,
        subtests_passed=pytest_record.get("subtests_passed") if isinstance(pytest_record, dict) else None,
        skipped=skips,
        logs=integration.get("logs") if integration else None,
    )

    artifacts = {
        "wheel": _file_record(wheel),
        "provenance": _file_record(provenance_path),
        "installed": _file_record(args.installed.resolve() if args.installed else None),
        "integration": _file_record(args.integration.resolve() if args.integration else None),
        "offline_guard": _file_record(args.offline_guard.resolve() if args.offline_guard else None),
        "native_checks": _file_record(args.native_checks.resolve() if args.native_checks else None),
        "logs": [_file_record(path.resolve()) for path in args.log if path.resolve().is_file()],
    }
    overall = all(layer["status"] == "passed" for layer in layers.values())
    record = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "passed" if overall else "failed",
        "overall": "success" if overall else "failure",
        "platform": platform.platform(),
        "source_refs": {
            "app": {"expected": app_sha, "resolved": app_actual},
            "native": {"expected": native_sha, "resolved": native_actual},
        },
        "layers": layers,
        "failure_layers": [name for name, layer in layers.items() if layer["status"] != "passed"],
        "artifacts": artifacts,
    }
    output = args.output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"joint audit: {record['overall']} ({output})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

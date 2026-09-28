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
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
REQUIRED_LIVENODE_TEST = "tests/test_installed_wheel_livenode.py::test_installed_wheel_livenode_loopback_lifecycle"


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
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return -1


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
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, text=True, check=False)
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
        and not app_dirty
        and not native_dirty
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
    required_checks = {"fmt", "test", "doctest"}
    required_crates = {"nautilus-aster", "nautilus-ondo", "nautilus-portfolio"}
    checked_names = {row.get("name") for row in blocking_rows if isinstance(row.get("name"), str)}
    checked_crates = native_checks.get("crates") if native_checks else None
    native_checks_ok = (
        native_checks is not None
        and isinstance(raw_native_rows, list)
        and len(native_check_rows) == sum(isinstance(row, dict) for row in native_check_rows)
        and required_checks.issubset(checked_names)
        and isinstance(checked_crates, list)
        and required_crates.issubset({item for item in checked_crates if isinstance(item, str)})
        and _object(native_checks.get("native")).get("commit") == native_sha
        and native_checks.get("identity_changed_during_checks") is False
        and all(row.get("exit_code") == 0 for row in blocking_rows)
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
    provenance_ok = (
        provenance is not None
        and provenance.get("source_binding") == "verified"
        and declared_native.get("commit") == native_sha
        and not declared_native.get("dirty_count")
        and wheel is not None
        and wheel.is_file()
        and provenance_wheel.get("sha256") == _safe_digest(wheel)
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
    installed_ok = (
        installed is not None
        and installed.get("exit_code", 0) == 0
        and installed.get("strict_source_binding_passed") is True
        and wheel is not None
        and installed_wheel.get("sha256") == _safe_digest(wheel)
        and _object(installed.get("native_provenance")).get("source_binding") == "verified"
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
        and guard.get("exit_code") == 0
        and guard.get("policy") == "loopback-only"
        and enforcement.get("kind") in {"network-namespace", "program-firewall"}
        and (
            (enforcement.get("kind") == "network-namespace" and enforcement.get("probe_exit_code") == 0)
            or (
                enforcement.get("kind") == "program-firewall"
                and enforcement.get("install_exit_code") == 0
                and guard.get("firewall_cleanup") is True
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
    skips = _count(pytest_counts.get("skipped", pytest_record.get("skipped", 0)))
    test_ok = (
        integration is not None
        and integration.get("status") == "passed"
        and integration.get("exit_code") == 0
        and _object(source_refs.get("app")).get("resolved") == app_sha
        and _object(source_refs.get("native")).get("resolved") == native_sha
        and not skips
        and not _count(pytest_counts.get("failed", 0))
        and not _count(pytest_counts.get("error", 0))
        and _count(collection.get("items")) >= 1188
        and required_tests.get(REQUIRED_LIVENODE_TEST) is True
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

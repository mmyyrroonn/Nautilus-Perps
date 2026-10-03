"""Fail-closed coverage for the two-repository installed-wheel acceptance audit."""

from __future__ import annotations

import copy
import hashlib
import importlib.util
import json
import subprocess
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

SPEC = importlib.util.spec_from_file_location("joint_audit", Path(__file__).parents[1] / "scripts/joint_audit.py")
audit = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(audit)


def _write(path, value):
    path.write_text(json.dumps(value), encoding="utf-8")


@pytest.fixture
def acceptance(tmp_path, monkeypatch):
    app = tmp_path / "app"
    native = tmp_path / "native"
    app.mkdir()
    native.mkdir()
    (app / "scripts").mkdir()
    (app / "scripts/example.py").write_bytes(b"source")
    for name in ("Cargo.lock", "python/uv.lock", "python/pyproject.toml", "scripts/adapter-evidence/build_native.py"):
        path = native / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(name.encode())
    features = ["extension-module", "arrow", "betfair", "high-precision", "mimalloc",
                "redis", "postgres", "defi", "hypersync", "tracing-bridge"]
    (native / "python/pyproject.toml").write_text(
        "[tool.maturin]\nfeatures = " + json.dumps(features) + "\n", encoding="utf-8",
    )
    app_sha, native_sha, tree = "a" * 40, "b" * 40, "c" * 40

    def git(root, *args):
        if args == ("rev-parse", "HEAD"):
            return app_sha if root == app else native_sha
        if args == ("rev-parse", "HEAD^{tree}"):
            return tree
        return ""

    monkeypatch.setattr(audit, "_git", git)
    monkeypatch.setattr(audit, "_env_files", lambda root: [])
    def listing(command, **kwargs):
        return subprocess.CompletedProcess(
            command, 0, b"" if "--others" in command else b"scripts/example.py\0", b"",
        )

    monkeypatch.setattr(audit, "subprocess", SimpleNamespace(run=listing))
    native_identity = {
        "commit": native_sha, "tree": tree, "dirty": [], "dirty_count": 0,
        "tracked_diff_sha256": hashlib.sha256(b"").hexdigest(),
        "untracked_sha256": hashlib.sha256(b"").hexdigest(),
        "cargo_lock_sha256": audit._digest(native / "Cargo.lock"),
    }
    app_file_digest = audit._digest(app / "scripts/example.py")
    app_identity = {
        "commit": app_sha, "tree": tree, "dirty": False,
        "source_scope": list(audit.APP_SOURCE_SCOPE),
        "source_files_sha256": {"scripts/example.py": app_file_digest},
        "tracked_content_sha256": hashlib.sha256(b"scripts/example.py\0" + bytes.fromhex(app_file_digest)).hexdigest(),
        "untracked_sha256": hashlib.sha256(b"").hexdigest(),
    }
    wheel = tmp_path / "candidate.whl"
    binary = "nautilus_trader/_libnautilus.pyd"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr(binary, b"native binary")
        for stub in audit.REQUIRED_STUBS:
            archive.writestr(stub, stub.encode())
    binaries, stubs = audit._wheel_inventory(wheel)
    fingerprint = audit._fingerprint(native_identity)
    provenance = {
        "schema_version": 1, "source_binding": "verified", "declared_native": native_identity,
        "source_fingerprint_sha256": fingerprint,
        "declared_build": {"features": ",".join(features), "profile": "release"},
        "wheel": {"path": str(wheel), "sha256": audit._digest(wheel)},
        "embedded_native_objects": [{"path": path, "sha256": digest} for path, digest in binaries.items()],
        "embedded_adapter_stubs": [{"path": path, "sha256": digest} for path, digest in stubs.items()],
        "build": {
            "started_source": native_identity, "finished_source": native_identity,
            "cargo_lock_sha256": native_identity["cargo_lock_sha256"],
            "python_uv_lock_sha256": audit._digest(native / "python/uv.lock"),
            "maturin_config_sha256": audit._digest(native / "python/pyproject.toml"),
            "builder_sha256": audit._digest(native / "scripts/adapter-evidence/build_native.py"),
        },
    }
    provenance_path = tmp_path / "provenance.json"
    _write(provenance_path, provenance)
    candidate = {
        "app": app_identity, "strict_source_binding_passed": True,
        "wheel": provenance["wheel"],
        "installed": {
            "direct_url": wheel.as_uri(), "native_module_sha256": binaries[binary],
            "adapter_stub_sha256": stubs,
            "adapter_exports": {"backpack": {name: True for name in audit.BACKPACK_EXPORTS}},
        },
        "native_provenance": {
            "path": str(provenance_path), "sha256": audit._digest(provenance_path),
            "source_binding": "verified", "declared_native": native_identity,
            "declared_build": provenance["declared_build"],
            "source_fingerprint_sha256": fingerprint, "build": provenance["build"],
        },
    }
    records = {
        "provenance": provenance, "installed": candidate,
        "native-checks": {
            "native": native_identity, "crates": ["nautilus-aster", "nautilus-ondo", "nautilus-backpack", "nautilus-portfolio"],
            "identity_before_fingerprint": fingerprint, "identity_after_fingerprint": fingerprint,
            "identity_changed_during_checks": False,
            "checks": [{"name": name, "blocking": True, "exit_code": 0}
                       for name in ("fmt", "test", "doctest", "python")],
        },
        "offline-guard": {
            "status": "passed", "exit_code": 0, "policy": "loopback-only",
            "enforcement": {"kind": "network-namespace", "probe_exit_code": 0},
        },
        "integration": {
            "status": "passed", "exit_code": 0, "candidate": copy.deepcopy(candidate),
            "source_refs": {"app": {"expected": app_sha, "resolved": app_sha},
                            "native": {"expected": native_sha, "resolved": native_sha}},
            "collection": {"exit_code": 0, "items": 1188,
                           "required_tests": {node: True for node in audit.REQUIRED_TESTS}},
            "pytest": {"exit_code": 0, "counts": {"skipped": 0, "failed": 0, "errors": 0},
                       "item_passed": 1188, "subtests_passed": 97, "skipped": 0},
        },
    }

    def run():
        args = ["joint_audit.py", "--output", str(tmp_path / "audit.json"),
                "--app-root", str(app), "--native-root", str(native),
                "--app-sha", app_sha, "--native-sha", native_sha, "--wheel", str(wheel)]
        for name, record in records.items():
            path = tmp_path / f"{name}.json"
            _write(path, record)
            args.extend((f"--{name}", str(path)))
        monkeypatch.setattr(sys, "argv", args)
        assert audit.main() == 0
        return json.loads((tmp_path / "audit.json").read_text(encoding="utf-8"))

    return records, run


def test_complete_installed_backpack_candidate_passes(acceptance):
    _, run = acceptance
    assert run()["status"] == "passed"


@pytest.mark.parametrize("change", [
    "missing_backpack_crate", "missing_python_check", "nonblocking_python",
    "boolean_exit_code", "malformed_check", "duplicate_check", "missing_native_source",
])
def test_native_manifest_refuses_incomplete_or_malformed_checks(acceptance, change):
    records, run = acceptance
    checks = records["native-checks"]
    if change == "missing_backpack_crate":
        checks["crates"].remove("nautilus-backpack")
    elif change == "missing_python_check":
        checks["checks"].pop()
    elif change == "nonblocking_python":
        checks["checks"][-1]["blocking"] = False
    elif change == "boolean_exit_code":
        checks["checks"][0]["exit_code"] = False
    elif change == "malformed_check":
        checks["checks"].append(None)
    elif change == "duplicate_check":
        checks["checks"].append(checks["checks"][0])
    else:
        del checks["native"]["tree"]
    assert "native_checks" in run()["failure_layers"]


@pytest.mark.parametrize("change", [
    "missing_source_binding", "missing_prebuild", "changed_postbuild", "missing_fingerprint",
    "missing_backpack_stub", "wrong_binary_inventory", "missing_build_digest", "wrong_features",
])
def test_build_requires_actual_wheel_and_complete_source_manifest(acceptance, change):
    records, run = acceptance
    provenance = records["provenance"]
    if change == "missing_source_binding":
        del provenance["source_binding"]
    elif change == "missing_prebuild":
        del provenance["build"]["started_source"]
    elif change == "changed_postbuild":
        provenance["build"]["finished_source"] = {"commit": "d" * 40}
    elif change == "missing_fingerprint":
        del provenance["source_fingerprint_sha256"]
    elif change == "missing_backpack_stub":
        provenance["embedded_adapter_stubs"].pop()
    elif change == "wrong_binary_inventory":
        provenance["embedded_native_objects"][0]["sha256"] = "f" * 64
    elif change == "missing_build_digest":
        del provenance["build"]["builder_sha256"]
    else:
        provenance["declared_build"]["features"] += ",backpack"
    assert "build" in run()["failure_layers"]


@pytest.mark.parametrize("layer", ["installed", "integration"])
@pytest.mark.parametrize("change", [
    "binary", "backpack_stub", "backpack_exports", "export_truthy", "wheel",
    "source_files", "source_scope", "source_tree", "missing_native_fingerprint", "missing_native_build",
])
def test_both_candidate_records_must_bind_actual_binary_stubs_exports_and_sources(acceptance, layer, change):
    records, run = acceptance
    candidate = records[layer] if layer == "installed" else records[layer]["candidate"]
    if change == "binary":
        candidate["installed"]["native_module_sha256"] = "f" * 64
    elif change == "backpack_stub":
        del candidate["installed"]["adapter_stub_sha256"][audit.REQUIRED_STUBS[-1]]
    elif change == "backpack_exports":
        candidate["installed"]["adapter_exports"]["backpack"] = {}
    elif change == "export_truthy":
        candidate["installed"]["adapter_exports"]["backpack"][audit.BACKPACK_EXPORTS[0]] = 1
    elif change == "wheel":
        candidate["wheel"]["sha256"] = "f" * 64
    elif change == "source_files":
        candidate["app"]["source_files_sha256"] = {}
    elif change == "source_scope":
        candidate["app"]["source_scope"].remove(".github")
    elif change == "source_tree":
        candidate["app"]["tree"] = "f" * 40
    elif change == "missing_native_fingerprint":
        del candidate["native_provenance"]["source_fingerprint_sha256"]
    else:
        del candidate["native_provenance"]["build"]
    expected_layer = "install_identity" if layer == "installed" else "integration_tests"
    assert expected_layer in run()["failure_layers"]


@pytest.mark.parametrize("node", audit.REQUIRED_TESTS)
def test_every_required_native_behavior_test_must_be_collected(acceptance, node):
    records, run = acceptance
    del records["integration"]["collection"]["required_tests"][node]
    assert "integration_tests" in run()["failure_layers"]


@pytest.mark.parametrize("change", ["missing_skips", "missing_errors", "missing_exit", "boolean_exit", "skips", "errors"])
def test_integration_counts_fail_closed(acceptance, change):
    records, run = acceptance
    integration = records["integration"]
    if change == "missing_skips":
        del integration["pytest"]["counts"]["skipped"]
    elif change == "missing_errors":
        del integration["pytest"]["counts"]["errors"]
    elif change == "missing_exit":
        del integration["pytest"]["exit_code"]
    elif change == "boolean_exit":
        integration["exit_code"] = False
    elif change == "skips":
        integration["pytest"]["counts"]["skipped"] = 1
    else:
        integration["pytest"]["counts"]["errors"] = 1
    assert "integration_tests" in run()["failure_layers"]


@pytest.mark.parametrize("record", ["provenance", "installed", "integration", "native-checks", "offline-guard"])
def test_nonobject_records_leave_a_failed_machine_readable_audit(acceptance, record):
    records, run = acceptance
    records[record] = []
    assert run()["status"] == "failed"



@pytest.mark.parametrize("record", ["provenance", "installed", "integration", "native-checks", "offline-guard"])
def test_missing_records_fail_closed(acceptance, record):
    records, run = acceptance
    del records[record]
    assert run()["status"] == "failed"


def test_corrupt_actual_wheel_fails_despite_matching_declared_hashes(acceptance):
    records, run = acceptance
    wheel = Path(records["provenance"]["wheel"]["path"])
    wheel.write_bytes(b"not a zip archive")
    new_digest = audit._digest(wheel)
    records["provenance"]["wheel"]["sha256"] = new_digest
    records["installed"]["wheel"]["sha256"] = new_digest
    records["integration"]["candidate"]["wheel"]["sha256"] = new_digest
    assert "build" in run()["failure_layers"]


def test_credential_dotenv_causes_failed_audit_without_reading_it(acceptance, monkeypatch):
    _, run = acceptance
    monkeypatch.setattr(audit, "_env_files", lambda root: [str(root / ".env")])
    assert "dotenv" in run()["failure_layers"]


def test_unavailable_git_clean_status_cannot_pass_checkout(acceptance, monkeypatch):
    _, run = acceptance
    original = audit._git
    monkeypatch.setattr(audit, "_git", lambda root, *args: None if args[0] == "status" else original(root, *args))
    assert "checkout" in run()["failure_layers"]


@pytest.mark.parametrize("payload", ["{", "[]", "null"])
def test_unreadable_or_nonobject_json_cannot_be_loaded(tmp_path, payload):
    path = tmp_path / "record.json"
    path.write_text(payload, encoding="utf-8")
    record, error = audit._load(path)
    assert record is None
    assert error



def _program_firewall_record(records):
    root = Path(records["provenance"]["wheel"]["path"]).parent
    launcher = str(root / "venv" / "python.exe")
    host = str(root / "python" / "python.exe")
    discovery = {
        "status": "verified", "exit_code": 0, "launcher": launcher,
        "executable": launcher, "base_executable": host, "process_executable": host,
    }
    records["installed"]["installed"]["interpreter"] = launcher
    records["integration"]["candidate"]["installed"]["interpreter"] = launcher
    records["offline-guard"] = {
        "status": "passed", "exit_code": 0, "policy": "loopback-only",
        "command": [launcher, "scripts/test_integration.py"],
        "firewall_cleanup": True, "interpreter_discovery": discovery,
        "enforcement": {
            "kind": "program-firewall", "install_exit_code": 0,
            "interpreter_discovery": copy.deepcopy(discovery),
            "programs": [launcher, host],
        },
    }
    return records["offline-guard"]


def test_program_firewall_covers_selected_launcher_and_actual_host(acceptance):
    records, run = acceptance
    _program_firewall_record(records)
    assert run()["status"] == "passed"


@pytest.mark.parametrize("change", [
    "missing_discovery", "different_discovery", "unverified", "boolean_exit",
    "missing_host_path", "relative_path", "different_executable", "different_command",
    "different_installed_interpreter", "different_integration_interpreter",
    "launcher_only", "unblocked_process",
])
def test_program_firewall_requires_verified_and_fully_blocked_interpreter(acceptance, change):
    records, run = acceptance
    guard = _program_firewall_record(records)
    discovery = guard["interpreter_discovery"]
    if change == "missing_discovery":
        del guard["interpreter_discovery"]
    elif change == "different_discovery":
        guard["enforcement"]["interpreter_discovery"] = {}
    elif change == "unverified":
        discovery["status"] = "failed"
    elif change == "boolean_exit":
        discovery["exit_code"] = False
    elif change == "missing_host_path":
        del discovery["base_executable"]
    elif change == "relative_path":
        discovery["process_executable"] = "python.exe"
    elif change == "different_executable":
        discovery["executable"] = discovery["base_executable"]
    elif change == "different_command":
        guard["command"][0] = discovery["base_executable"]
    elif change == "different_installed_interpreter":
        records["installed"]["installed"]["interpreter"] = discovery["base_executable"]
    elif change == "different_integration_interpreter":
        records["integration"]["candidate"]["installed"]["interpreter"] = discovery["base_executable"]
    elif change == "launcher_only":
        guard["enforcement"]["programs"] = [discovery["launcher"]]
    else:
        discovery["process_executable"] = str(Path(discovery["base_executable"]).with_name("other-python.exe"))
    if change not in ("missing_discovery", "different_discovery"):
        guard["enforcement"]["interpreter_discovery"] = copy.deepcopy(discovery)
    assert "offline_network" in run()["failure_layers"]

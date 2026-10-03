"""Failure gates for the native candidate installer."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import install_native
from install_native import check_provenance, choose_artifact
from verify_native_install import git_identity


def candidate(tmp_path: Path) -> tuple[Path, Path, str]:
    wheel = tmp_path / "nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl"
    from packaging.tags import sys_tags
    tag = str(next(sys_tags()))
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("nautilus_trader/_libnautilus.pyd", b"native")
        archive.writestr("nautilus_trader/adapters/ondo/__init__.pyi", b"stub")
        archive.writestr("nautilus_trader-2.0.0rc4.dist-info/WHEEL", f"Wheel-Version: 1.0\nTag: {tag}\n")
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    manifest = {
        "source_binding": "verified",
        "declared_native": {"dirty_count": 0},
        "wheel": {
            "sha256": digest,
            "name": "nautilus-trader",
            "version": "2.0.0rc4",
            "tags": [tag],
        },
        "embedded_native_objects": [{
            "path": "nautilus_trader/_libnautilus.pyd",
            "sha256": hashlib.sha256(b"native").hexdigest(),
        }],
        "embedded_adapter_stubs": [{
            "path": "nautilus_trader/adapters/ondo/__init__.pyi",
            "sha256": hashlib.sha256(b"stub").hexdigest(),
        }],
    }
    provenance = tmp_path / "native-provenance.json"
    provenance.write_text(json.dumps(manifest), encoding="utf-8")
    return wheel, provenance, digest


def test_rejects_wrong_hash_and_dirty_source(tmp_path: Path) -> None:
    wheel, provenance, digest = candidate(tmp_path)
    check_provenance(provenance, wheel, digest, allow_dirty=False)
    with pytest.raises(ValueError, match="SHA-256"):
        check_provenance(provenance, wheel, "0" * 64, allow_dirty=False)
    record = json.loads(provenance.read_text(encoding="utf-8"))
    record["declared_native"]["dirty_count"] = 1
    provenance.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="dirty"):
        check_provenance(provenance, wheel, digest, allow_dirty=False)
    check_provenance(provenance, wheel, digest, allow_dirty=True)


def test_rejects_wrong_abi_and_embedded_binary(tmp_path: Path) -> None:
    wheel, provenance, digest = candidate(tmp_path)
    record = json.loads(provenance.read_text(encoding="utf-8"))
    record["wheel"]["tags"] = ["cp314-cp314-manylinux_2_99_aarch64"]
    provenance.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="ABI"):
        check_provenance(provenance, wheel, digest, allow_dirty=False)
    record["wheel"]["tags"] = [str(next(__import__("packaging.tags", fromlist=["sys_tags"]).sys_tags()))]
    record["embedded_native_objects"][0]["sha256"] = "0" * 64
    provenance.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError, match="embedded"):
        check_provenance(provenance, wheel, digest, allow_dirty=False)


def test_lock_rejects_missing_platform() -> None:
    with pytest.raises(ValueError, match="one artifact"):
        choose_artifact({"schema_version": 1, "artifacts": [{"tags": ["cp314-cp314-win_arm64"]}]})


def test_rejects_uv_shadow_dependency(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(install_native, "ROOT", tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "app"\nversion = "0.1"\ndependencies = ["nautilus_trader==2.0.0rc4"]\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match="competing native dependency"):
        install_native.reject_shadow_dependency()


def test_result_file_does_not_change_app_source_identity(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    source = tmp_path / "src" / "engine.py"
    source.parent.mkdir()
    source.write_text("VALUE = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "src/engine.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    before = git_identity(tmp_path)
    report = tmp_path / "reports" / "result.json"
    report.parent.mkdir()
    report.write_text('{"run_id": "one"}', encoding="utf-8")
    assert git_identity(tmp_path) == before
    source.write_text("VALUE = 2\n", encoding="utf-8")
    assert git_identity(tmp_path)["tracked_content_sha256"] != before["tracked_content_sha256"]


@pytest.mark.parametrize("names", [("../backpack",), ("Backpack",), "backpack", ("",)])
def test_additional_adapter_names_reject_ambiguous_paths(tmp_path, names):
    from verify_native_install import verify
    with pytest.raises(ValueError, match="additional adapter"):
        verify(tmp_path / "missing.whl", "0" * 64, provenance=None,
               require_source_binding=False, require_ondo=False, additional_adapters=names)


def test_optional_backpack_stub_gate_is_backward_compatible_and_detects_tampering(tmp_path, monkeypatch):
    """Synthetic file verification only; the real installed LiveNode tests prove runtime behavior."""
    from types import SimpleNamespace

    import verify_native_install as verifier
    from packaging.tags import sys_tags
    wheel = tmp_path / "candidate.whl"
    environment = tmp_path / "isolated"
    binaries = {"nautilus_trader/_libnautilus.pyd": b"synthetic-native",
                **{f"nautilus_trader/adapters/{name}/__init__.pyi": name.encode()
                   for name in ("aster", "ondo", "backpack")}}
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, payload in binaries.items():
            archive.writestr(name, payload)
            installed = environment / name
            installed.parent.mkdir(parents=True, exist_ok=True)
            installed.write_bytes(payload)
        archive.writestr("nautilus_trader.dist-info/WHEEL", f"Tag: {next(sys_tags())}\n")
    class Distribution:
        def read_text(self, name):
            assert name == "direct_url.json"
            return json.dumps({"url": wheel.as_uri()})
        def locate_file(self, name):
            return environment / name
    monkeypatch.setattr(verifier.metadata, "distribution", lambda _: Distribution())
    backpack = SimpleNamespace()
    for symbol in verifier.BACKPACK_EXPORTS:
        parts = symbol.split(".")
        if len(parts) == 1:
            setattr(backpack, symbol, type(symbol, (), {}))
        else:
            setattr(getattr(backpack, parts[0]), parts[1], object())
    monkeypatch.setattr(verifier.importlib, "import_module", lambda name: backpack
        if name == "nautilus_trader.adapters.backpack" else SimpleNamespace(
            __file__=str(environment / "nautilus_trader/_libnautilus.pyd")))
    monkeypatch.setattr(sys, "prefix", str(environment))
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    def check(additional=()):
        return verifier.verify(wheel, digest, provenance=None, require_source_binding=False,
                               require_ondo=False, additional_adapters=additional)
    assert len(check()["installed"]["adapter_stub_sha256"]) == 2
    verified = check(("backpack",))
    assert len(verified["installed"]["adapter_stub_sha256"]) == 3
    assert all(verified["installed"]["adapter_exports"]["backpack"].values())
    control = backpack.BackpackLoopbackControl
    del backpack.BackpackLoopbackControl
    with pytest.raises(ValueError, match="Backpack export is absent"):
        check(("backpack",))
    backpack.BackpackLoopbackControl = control
    (environment / "nautilus_trader/adapters/backpack/__init__.pyi").write_bytes(b"tampered")
    assert len(check()["installed"]["adapter_stub_sha256"]) == 2
    with pytest.raises(ValueError, match="installed adapter stub"):
        check(("backpack",))


def test_workflow_content_is_part_of_application_source_manifest(tmp_path: Path) -> None:
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    workflow = tmp_path / ".github" / "workflows" / "joint.yml"
    workflow.parent.mkdir(parents=True)
    workflow.write_text("name: original\n", encoding="utf-8")
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.invalid"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", ".github"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "baseline"], cwd=tmp_path, check=True)
    before = git_identity(tmp_path)
    key = ".github/workflows/joint.yml"
    assert before["source_files_sha256"][key] == hashlib.sha256(workflow.read_bytes()).hexdigest()
    workflow.write_text("name: changed\n", encoding="utf-8")
    after = git_identity(tmp_path)
    assert after["dirty"] is True
    assert after["tracked_content_sha256"] != before["tracked_content_sha256"]
    assert after["source_files_sha256"][key] != before["source_files_sha256"][key]

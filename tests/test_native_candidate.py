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

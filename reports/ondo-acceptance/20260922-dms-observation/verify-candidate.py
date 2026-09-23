"""Verify a rebuilt candidate against its source manifest and isolated installation."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys
import zipfile


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    args = parser.parse_args()
    report = Path(__file__).resolve().parent
    app = report.parents[2]
    fork = app.parent / "ondo-btc-native"
    assert Path(sys.prefix).resolve() == (app / ".venv").resolve()
    wheel = args.wheel.resolve()
    dist = metadata.distribution("nautilus_trader")
    direct_url = json.loads(dist.read_text("direct_url.json"))
    assert direct_url["url"] == wheel.as_uri()

    with zipfile.ZipFile(wheel) as archive:
        names = [name for name in archive.namelist()
                 if name.startswith("nautilus_trader/") and name.endswith(".pyd")]
        assert len(names) == 1
        binary = archive.read(names[0])
        assert Path(dist.locate_file(names[0])).read_bytes() == binary
        assert b"updates_after_release" in binary
        stub_name = "nautilus_trader/adapters/ondo/__init__.pyi"
        stub = archive.read(stub_name)
        assert stub == (fork / "python" / stub_name).read_bytes()
        assert stub == Path(dist.locate_file(stub_name)).read_bytes()

    source_rows = json.loads(args.source_manifest.read_text(encoding="utf-8-sig"))
    for row in source_rows:
        assert sha((fork / row["path"]).read_bytes()).lower() == row["sha256"].lower(), row["path"]

    from nautilus_trader.adapters.ondo import OndoExecutionClientFactory

    factory = OndoExecutionClientFactory()
    assert factory.supports_production_trade_envelope is True
    assert factory.production_trade_snapshot() is None
    assert factory.read_only_snapshot() is None
    app_paths = sorted((app / "src").glob("*.py")) + [app / "config/limits.toml", app / "pyproject.toml"]
    app_rows = [{"path": path.relative_to(app).as_posix(), "sha256": sha(path.read_bytes())}
                for path in app_paths]
    result = {
        "candidate_kind": "dms-observation",
        "version": dist.version,
        "wheel": str(wheel), "wheel_sha256": sha(wheel.read_bytes()),
        "binary_sha256": sha(binary), "stub_sha256": sha(stub),
        "native_source_manifest": str(args.source_manifest.resolve()),
        "frozen_source_files_verified": len(source_rows),
        "app_source_files": app_rows,
        "factory_snapshot_initially_none": True,
        "credentials_read": False, "network_started": False,
        "production_execution_verified": False,
        "dms_release_host_contract_verified": False,
    }
    (report / "candidate-identity.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps({key: value for key, value in result.items() if key != "app_source_files"},
                     sort_keys=True))


if __name__ == "__main__":
    main()

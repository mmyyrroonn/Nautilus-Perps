"""Bind the BTC wheel, isolated installation and frozen source without any network use."""

from __future__ import annotations

import argparse
import hashlib
from importlib import metadata
import importlib
import json
from pathlib import Path
import sys
import zipfile


REPORT = Path(__file__).resolve().parent
APP = REPORT.parents[2]
FORK = APP.parent / "ondo-btc-native"
DMS_SUFFIX_SHA256 = "b569675d92e2eea7801c47f0542134db068612046a350422246cf7d64b2c84f6"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    args = parser.parse_args()
    assert Path(sys.prefix).resolve() == (APP / ".venv").resolve()
    wheel = args.wheel.resolve()
    dist = metadata.distribution("nautilus_trader")
    assert json.loads(dist.read_text("direct_url.json"))["url"] == wheel.as_uri()
    with zipfile.ZipFile(wheel) as archive:
        binaries = [name for name in archive.namelist()
                    if name.startswith("nautilus_trader/") and name.endswith(".pyd")]
        assert len(binaries) == 1
        binary = archive.read(binaries[0])
        installed_binary = Path(dist.locate_file(binaries[0])).resolve()
        assert installed_binary.is_relative_to((APP / ".venv").resolve())
        assert installed_binary.read_bytes() == binary
        imported_binary = importlib.import_module("nautilus_trader._libnautilus")
        assert Path(imported_binary.__file__).resolve() == installed_binary
        stub_name = "nautilus_trader/adapters/ondo/__init__.pyi"
        stub = archive.read(stub_name)
        assert stub == (FORK / "python" / stub_name).read_bytes()
        assert stub == Path(dist.locate_file(stub_name)).read_bytes()

    source_rows = json.loads(args.source_manifest.read_text(encoding="utf-8-sig"))
    for row in source_rows:
        assert sha((FORK / row["path"]).read_bytes()).lower() == row["sha256"].lower(), row["path"]

    production = (FORK / "crates/adapters/ondo/src/production.rs").read_bytes()
    suffix = production[production.index(b"#[derive(Clone, Debug, Default)]"):]
    assert sha(suffix) == DMS_SUFFIX_SHA256, "DMS/authority suffix changed"
    from nautilus_trader.adapters.ondo import OndoExecutionClientFactory
    factory = OndoExecutionClientFactory()
    assert factory.supports_production_trade_envelope is True
    assert factory.production_trade_snapshot() is None
    assert factory.read_only_snapshot() is None

    paths = sorted((APP / "src").glob("*.py")) + [
        APP / "config/limits.toml", APP / "config/ondo_btc_test.toml",
        APP / "pyproject.toml", APP / "tests/test_ondo_btc_trade.py",
    ]
    result = {
        "candidate_kind": "btc-functional-dms-unchanged", "version": dist.version,
        "wheel": str(wheel), "wheel_sha256": sha(wheel.read_bytes()),
        "binary_sha256": sha(binary), "stub_sha256": sha(stub),
        "native_source_manifest": str(args.source_manifest.resolve()),
        "frozen_source_files_verified": len(source_rows),
        "dms_authority_suffix_sha256": sha(suffix),
        "dms_authority_suffix_unchanged": True,
        "app_source_files": [{"path": path.relative_to(APP).as_posix(),
                              "sha256": sha(path.read_bytes())} for path in paths],
        "credentials_read": False, "network_started": False,
        "production_execution_verified": False,
        "dms_release_host_contract_verified": False,
    }
    with (REPORT / "candidate-identity.json").open("x", encoding="utf-8") as output:
        json.dump(result, output, indent=2, sort_keys=True)
        output.write("\n")
    print(json.dumps({key: value for key, value in result.items() if key != "app_source_files"},
                     sort_keys=True))


if __name__ == "__main__":
    main()

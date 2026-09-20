"""Verify the installed readonly candidate against its wheel and frozen source."""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys
import zipfile


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    root = Path("E:/Nautilus-Perps")
    app = root / ".worktrees/ondo-production-native"
    fork = Path("E:/nautilus_trader/.worktrees/ondo-production-native")
    build = Path(__file__).resolve().parent
    assert Path(sys.prefix).resolve() == (app / ".venv").resolve()
    wheels = list((build / "dist").glob("nautilus_trader-*.whl"))
    assert len(wheels) == 1, "Expected one candidate wheel"
    wheel = wheels[0]
    wheel_hash = digest(wheel.read_bytes())
    dist = metadata.distribution("nautilus_trader")
    assert dist.version == "2.0.0rc4"
    direct_url = json.loads(dist.read_text("direct_url.json"))
    assert direct_url["url"] == wheel.resolve().as_uri()
    recorded_hash = direct_url.get("archive_info", {}).get("hashes", {}).get("sha256")
    if recorded_hash is not None:
        assert recorded_hash == wheel_hash

    with zipfile.ZipFile(wheel) as archive:
        binary_names = [name for name in archive.namelist()
                        if name.startswith("nautilus_trader/") and name.endswith(".pyd")]
        assert len(binary_names) == 1
        binary_name = binary_names[0]
        binary_hash = digest(archive.read(binary_name))
        assert digest(Path(dist.locate_file(binary_name)).read_bytes()) == binary_hash
        stub_name = "nautilus_trader/adapters/ondo/__init__.pyi"
        stub = archive.read(stub_name)
        assert stub == (fork / "python" / stub_name).read_bytes()
        assert stub == Path(dist.locate_file(stub_name)).read_bytes()
        for required in (b"diagnostics_run_id", b"expected_venue_account_id", b"read_only_snapshot"):
            assert required in stub

    source_manifest = json.loads((build / "release-source.json").read_text(encoding="utf-8-sig"))
    for row in source_manifest:
        # The build manifest stores paths relative to the fork root.
        source = fork / row["path"]
        assert digest(source.read_bytes()).upper() == row["sha256"].upper(), row["path"]

    from nautilus_trader.adapters.ondo import (
        OndoEnvironment, OndoExecutionClientConfig, OndoExecutionClientFactory,
    )
    from nautilus_trader.model import AccountId

    config = OndoExecutionClientConfig(
        environment=OndoEnvironment.PRODUCTION,
        account_id=AccountId("ONDO-offline-check"),
        account_read_only=True,
        expected_venue_account_id="offline-check",
        diagnostics_run_id="candidate-identity-check",
    )
    assert config.account_read_only is True
    assert config.diagnostics_run_id == "candidate-identity-check"
    assert config.expected_venue_account_id == "offline-check"
    factory = OndoExecutionClientFactory()
    assert factory.read_only_snapshot() is None
    assert getattr(factory, "supports_production_trade_envelope", False) is not True
    assert factory.supports_ordered_shutdown is True

    result = {
        "version": dist.version, "wheel": str(wheel), "wheel_sha256": wheel_hash,
        "binary_sha256": binary_hash, "stub_sha256": digest(stub),
        "installer_recorded_archive_hash": recorded_hash is not None,
        "frozen_source_files_verified": len(source_manifest),
        "config_and_factory_api_verified": True,
        "production_trade_capability": False,
        "credentials_read": False, "network_started": False,
    }
    (build / "candidate-identity.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

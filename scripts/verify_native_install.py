"""Verify an installed native candidate without contacting a venue or reading credentials.

Run with the exact application interpreter. This inventory is not proof that a wheel
was built from its declared source; --require-source-binding enforces that separate gate.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import re
import subprocess
import sys
import zipfile
from importlib import metadata
from pathlib import Path

from packaging.tags import sys_tags


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


SOURCE_PATHS = ("src", "scripts", "tests", "config", ".github", "pyproject.toml", "uv.lock", ".python-version")

BACKPACK_EXPORTS = (
    "BackpackCredential",
    "BackpackInstrumentEconomics",
    "BackpackLoopbackAccountFacts",
    "BackpackLoopbackExecutionAuthority",
    "BackpackDataClientConfig",
    "BackpackDataClientConfig.telemetry_snapshot_json",
    "BackpackDataClientFactory",
    "BackpackDataClientFactory.name",
    "BackpackDataClientFactory.capabilities_json",
    "BackpackExecutionClientConfig",
    "BackpackExecutionClientConfig.telemetry_snapshot_json",
    "BackpackExecutionClientFactory",
    "BackpackExecutionClientFactory.name",
    "BackpackExecutionClientFactory.capabilities_json",
    "BackpackLoopbackExecutionClientConfig",
    "BackpackLoopbackExecutionClientConfig.control",
    "BackpackLoopbackExecutionClientFactory",
    "BackpackLoopbackExecutionClientFactory.name",
    "BackpackLoopbackExecutionClientFactory.capabilities_json",
    "BackpackLoopbackControl",
    "BackpackLoopbackControl.begin_session",
    "BackpackLoopbackControl.accept_account",
    "BackpackLoopbackControl.refresh_market",
    "BackpackLoopbackControl.invalidate",
    "BackpackLoopbackControl.pending_fills_json",
    "BackpackLoopbackControl.telemetry_snapshot_json",
    "BackpackLoopbackControl.shutdown_report_json",
    "BackpackLoopbackSession",
    "BackpackLoopbackSession.generation",
    "BackpackQuota",
    "BackpackQuota.shares_scope",
    "BackpackPublicReplay",
    "BackpackPublicReplay.instrument",
    "BackpackPublicReplay.apply_record",
)


def git_identity(root: Path) -> dict[str, object]:
    def git(*args: str) -> bytes:
        return subprocess.check_output(["git", *args], cwd=root)

    tracked = hashlib.sha256()
    source_files: dict[str, str | None] = {}
    for raw_path in sorted(filter(None, git("ls-files", "-z", "--", *SOURCE_PATHS).split(b"\0"))):
        path = root / raw_path.decode("utf-8", errors="surrogateescape")
        tracked.update(raw_path + b"\0")
        if path.is_file():
            file_digest = digest(path)
            tracked.update(bytes.fromhex(file_digest))
            source_files[raw_path.decode("utf-8", errors="surrogateescape")] = file_digest
        else:
            tracked.update(b"missing")
            source_files[raw_path.decode("utf-8", errors="surrogateescape")] = None
    untracked = hashlib.sha256()
    for raw_path in sorted(filter(None, git("ls-files", "--others", "--exclude-standard", "-z", "--", *SOURCE_PATHS).split(b"\0"))):
        path = root / raw_path.decode("utf-8", errors="surrogateescape")
        if not path.is_file():
            raise ValueError("an untracked source path is unreadable")
        untracked.update(raw_path + b"\0")
        file_digest = digest(path)
        untracked.update(bytes.fromhex(file_digest))
        source_files[raw_path.decode("utf-8", errors="surrogateescape")] = file_digest
    return {
        "commit": git("rev-parse", "HEAD").decode().strip(),
        "tree": git("rev-parse", "HEAD^{tree}").decode().strip(),
        "dirty": bool(git("status", "--porcelain", "--untracked-files=normal", "--", *SOURCE_PATHS)),
        "tracked_content_sha256": tracked.hexdigest(),
        "untracked_sha256": untracked.hexdigest(),
        "source_scope": list(SOURCE_PATHS),
        "source_files_sha256": source_files,
    }


def verify(
    wheel: Path,
    expected_sha256: str,
    *,
    provenance: Path | None,
    require_source_binding: bool,
    require_ondo: bool,
    additional_adapters: tuple[str, ...] = (),
) -> dict[str, object]:
    if (not isinstance(additional_adapters, (tuple, list)) or len(additional_adapters) > 16
            or any(not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name)
                   for name in additional_adapters)):
        raise ValueError("invalid additional adapter names")
    wheel = wheel.resolve(strict=True)
    if wheel.suffix != ".whl":
        raise ValueError("candidate is not a wheel")
    if sys.prefix == sys.base_prefix:
        raise ValueError("run with an isolated application interpreter")
    wheel_sha256 = digest(wheel)
    if wheel_sha256 != expected_sha256.lower():
        raise ValueError("candidate wheel SHA-256 mismatch")
    native_provenance = None
    if provenance is not None:
        native_provenance = json.loads(provenance.read_text(encoding="utf-8"))
        if native_provenance.get("wheel", {}).get("sha256") != wheel_sha256:
            raise ValueError("native provenance names another wheel")
        if native_provenance.get("source_binding") != "verified" and require_source_binding:
            raise ValueError("native source-to-wheel binding is not verified")
    elif require_source_binding:
        raise ValueError("native provenance is required for source binding")
    with zipfile.ZipFile(wheel) as archive:
        native_names = [
            name for name in archive.namelist()
            if name.startswith("nautilus_trader/")
            and name.endswith((".pyd", ".so"))
            and name.count("/") == 1
        ]
        if len(native_names) != 1:
            raise ValueError("expected exactly one native module in wheel")
        native_name = native_names[0]
        native_sha256 = hashlib.sha256(archive.read(native_name)).hexdigest()
        stub_names = tuple(f"nautilus_trader/adapters/{name}/__init__.pyi"
                           for name in sorted({"aster", "ondo", *additional_adapters}))
        try:
            stub_hashes = {
                name: hashlib.sha256(archive.read(name)).hexdigest()
                for name in stub_names
            }
        except KeyError as exc:
            raise ValueError("candidate wheel lacks a required adapter stub") from exc
        wheel_files = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
        if len(wheel_files) != 1:
            raise ValueError("invalid wheel metadata")
        tags = {
            line.removeprefix("Tag:").strip()
            for line in archive.read(wheel_files[0]).decode("utf-8").splitlines()
            if line.startswith("Tag:")
        }
    supported_tags = {str(tag) for tag in sys_tags()}
    if not tags or tags.isdisjoint(supported_tags):
        raise ValueError("wheel ABI or platform is incompatible with this interpreter")
    distribution = metadata.distribution("nautilus-trader")
    direct_url_raw = distribution.read_text("direct_url.json")
    if not direct_url_raw:
        raise ValueError("installed distribution has no direct wheel origin")
    direct_url = json.loads(direct_url_raw).get("url")
    if direct_url != wheel.as_uri():
        raise ValueError("installed wheel origin differs from requested candidate")
    imported = Path(importlib.import_module("nautilus_trader._libnautilus").__file__).resolve()
    environment = Path(sys.prefix).resolve()
    if not imported.is_relative_to(environment):
        raise ValueError("native module imported from outside the selected environment")
    if digest(imported) != native_sha256:
        raise ValueError("installed native binary SHA-256 differs from wheel")
    for name, expected in stub_hashes.items():
        installed_stub = Path(distribution.locate_file(name)).resolve()
        if not installed_stub.is_relative_to(environment) or digest(installed_stub) != expected:
            raise ValueError("installed adapter stub differs from wheel")
    adapter_exports: dict[str, dict[str, bool]] = {}
    if "backpack" in additional_adapters:
        backpack = importlib.import_module("nautilus_trader.adapters.backpack")
        exports = {}
        for symbol in BACKPACK_EXPORTS:
            value = backpack
            for part in symbol.split("."):
                value = getattr(value, part, None)
            if value is None:
                raise ValueError(f"installed Backpack export is absent: {symbol}")
            exports[symbol] = True
        adapter_exports["backpack"] = exports
    if require_ondo:
        from nautilus_trader.adapters.ondo import OndoExecutionClientFactory

        factory = OndoExecutionClientFactory()
        if factory.supports_production_trade_envelope is not True:
            raise ValueError("installed Ondo production capability is absent")
        if not callable(getattr(factory, "production_shutdown_diagnostics", None)):
            raise ValueError("installed Ondo shutdown diagnostics are absent")
    app = Path(__file__).resolve().parents[1]
    return {
        "schema_version": 1,
        "app": git_identity(app),
        "wheel": {
            "sha256": wheel_sha256,
            "path": str(wheel),
            "tags": sorted(tags),
        },
        "installed": {
            "interpreter": sys.executable,
            "python_version": sys.version.split()[0],
            "native_module": str(imported),
            "native_module_sha256": native_sha256,
            "adapter_stub_sha256": stub_hashes,
            "adapter_exports": adapter_exports,
            "direct_url": direct_url,
        },
        "native_provenance": {
            "path": str(provenance) if provenance else None,
            "sha256": digest(provenance) if provenance else None,
            "source_binding": native_provenance.get("source_binding") if native_provenance else None,
            "declared_native": native_provenance.get("declared_native") if native_provenance else None,
            "declared_build": native_provenance.get("declared_build") if native_provenance else None,
            "source_fingerprint_sha256": native_provenance.get("source_fingerprint_sha256") if native_provenance else None,
            "build": native_provenance.get("build") if native_provenance else None,
        },
        "strict_source_binding_passed": (
            native_provenance is not None
            and native_provenance.get("source_binding") == "verified"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--native-provenance", type=Path)
    parser.add_argument("--require-source-binding", action="store_true")
    parser.add_argument("--require-ondo", action="store_true")
    parser.add_argument("--additional-adapter", action="append", default=[],
                        help="also compare this adapter's installed stub with the candidate wheel")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        record = verify(
            args.wheel, args.sha256,
            provenance=args.native_provenance,
            require_source_binding=args.require_source_binding,
            require_ondo=args.require_ondo,
            additional_adapters=tuple(args.additional_adapter),
        )
    except (OSError, ValueError, KeyError, ImportError, zipfile.BadZipFile) as exc:
        parser.exit(2, f"native candidate refused: {exc}\n")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"verified installed wheel and binary: {record['wheel']['sha256']}")
    print(f"source binding: {record['native_provenance']['source_binding'] or 'unavailable'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

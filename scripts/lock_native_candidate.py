"""Create an immutable two-platform input lock for published native artifacts."""

from __future__ import annotations

import argparse
import json
import zipfile
from pathlib import Path
from urllib.parse import urlparse

from verify_native_install import digest


def https_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise ValueError("artifact URL must be a credential-free HTTPS URL")
    return value


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--artifact", nargs=4, action="append", required=True,
        metavar=("PROVENANCE", "WHEEL_FILE", "WHEEL_URL", "PROVENANCE_URL"),
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    source = None
    build = None
    artifacts = []
    for manifest_path, wheel_file, wheel_url, provenance_url in args.artifact:
        path = Path(manifest_path)
        manifest = json.loads(path.read_text(encoding="utf-8"))
        native = manifest["declared_native"]
        if manifest.get("source_binding") != "verified" or native.get("dirty_count") != 0:
            parser.error("formal lock requires a clean, source-bound native build")
        identity = {
            "repository": "https://github.com/mmyyrroonn/nautilus_trader.git",
            "commit": native["commit"],
            "tree": native["tree"],
            "fingerprint_sha256": manifest["source_fingerprint_sha256"],
        }
        if source is None:
            source = identity
        elif source != identity:
            parser.error("all platform wheels must come from the same native source")
        build_inputs = {**manifest["declared_build"], "builder_sha256": manifest["build"]["builder_sha256"]}
        if build is None:
            build = build_inputs
        elif build != build_inputs:
            parser.error("platform wheels must use identical features and profile")
        wheel = manifest["wheel"]
        wheel_path = Path(wheel_file).resolve(strict=True)
        if wheel_path.name != wheel["filename"]:
            parser.error("wheel filename differs from provenance")
        if digest(wheel_path) != wheel["sha256"]:
            parser.error("local wheel bytes differ from provenance")
        with zipfile.ZipFile(wheel_path) as archive:
            metadata = [name for name in archive.namelist() if name.endswith(".dist-info/WHEEL")]
            if len(metadata) != 1:
                parser.error("wheel has no unique WHEEL metadata")
            tags_in_wheel = {
                line.removeprefix("Tag:").strip()
                for line in archive.read(metadata[0]).decode("utf-8").splitlines()
                if line.startswith("Tag:")
            }
            if tags_in_wheel != set(wheel["tags"]):
                parser.error("provenance wheel tags differ from artifact")
        if wheel["name"].replace("_", "-").lower() != "nautilus-trader":
            parser.error("artifact is not the fork package")
        artifacts.append({
            "filename": wheel["filename"],
            "tags": wheel["tags"],
            "sha256": wheel["sha256"],
            "url": https_url(wheel_url),
            "provenance_sha256": digest(path),
            "provenance_url": https_url(provenance_url),
        })
    tags = [tag for artifact in artifacts for tag in artifact["tags"]]
    if len(tags) != len(set(tags)):
        parser.error("artifact platform tags overlap")
    if not any("win_amd64" in tag for tag in tags):
        parser.error("Windows amd64 wheel is required")
    if not any("manylinux" in tag and "x86_64" in tag for tag in tags):
        parser.error("Linux x86_64 wheel is required")
    lock = {"schema_version": 1, "native": source, "build": build, "artifacts": artifacts}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(lock, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

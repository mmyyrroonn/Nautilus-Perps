"""Check acceptance entry points, immutable evidence links and candidate identity shape."""

from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DOCUMENTS = (
    ROOT / "docs/acceptance-status.md",
    ROOT / "docs/native-interface.md",
    ROOT / "docs/test-coverage.md",
    ROOT / "docs/ondo.md",
)
HISTORICAL_IDENTITY = ROOT / "reports/ondo-acceptance/20260923-freshness/candidate-identity.json"
LINK = re.compile(r"\]\(([^)]+)\)")
SHA = re.compile(r"[0-9a-f]{40}")
DIGEST = re.compile(r"[0-9a-f]{64}")


def main() -> int:
    failures: list[str] = []
    for document in DOCUMENTS:
        for target in LINK.findall(document.read_text(encoding="utf-8-sig")):
            path = target.split("#", 1)[0]
            if path.startswith("https://github.com/"):
                parts = path.removeprefix("https://github.com/").split("/")
                if (
                    len(parts) >= 4
                    and parts[2] in {"blob", "commit", "tree"}
                    and SHA.fullmatch(parts[3]) is None
                ):
                    failures.append(f"{document.name}: evidence link lacks a full SHA: {target}")
            elif "://" not in path and path:
                resolved = (document.parent / path).resolve()
                if not resolved.is_relative_to(ROOT) or not resolved.is_file():
                    failures.append(f"{document.name}: missing local link: {target}")
    identity = json.loads(HISTORICAL_IDENTITY.read_text(encoding="utf-8-sig"))
    for field in ("wheel_sha256", "binary_sha256", "stub_sha256"):
        if DIGEST.fullmatch(str(identity.get(field, ""))) is None:
            failures.append(f"historical identity lacks valid {field}")
    if not identity.get("app_source_files"):
        failures.append("historical candidate has no source-file manifest")
    if failures:
        for failure in failures:
            print(failure)
        return 1
    print("acceptance links and historical candidate identity: valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

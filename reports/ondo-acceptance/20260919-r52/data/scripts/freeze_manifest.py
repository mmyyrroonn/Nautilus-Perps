#!/usr/bin/env python3
"""Freeze a SHA256+size manifest of a set of files or directory trees.

Read-only. Writes one JSON document. Used to prove that historical inputs were
not modified by the R5.2 replay: the same listing is captured before and after
the analyzer runs, and the two documents must compare equal.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


def iter_files(root: Path):
    if root.is_file():
        yield root
        return
    excluded_dirs = {"__pycache__", ".pytest_cache", ".mypy_cache", ".git"}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if name not in excluded_dirs)
        for name in sorted(filenames):
            if name.endswith((".pyc", ".pyo")):
                continue
            yield Path(dirpath) / name


def collect(roots: list[Path]) -> list[dict]:
    entries: list[dict] = []
    for root in roots:
        if not root.exists():
            raise SystemExit(f"path does not exist: {root}")
        for path in iter_files(root):
            digest, size = sha256_file(path)
            entries.append(
                {
                    "root": str(root),
                    "path": str(path),
                    "relpath": str(path.relative_to(root)) if root.is_dir() else path.name,
                    "size": size,
                    "sha256": digest,
                }
            )
    entries.sort(key=lambda item: item["path"].lower())
    return entries


def tree_digest(entries: list[dict]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(entry["path"].encode("utf-8"))
        digest.update(b"\0")
        digest.update(entry["sha256"].encode("ascii"))
        digest.update(b"\0")
        digest.update(str(entry["size"]).encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--label", default="")
    parser.add_argument("paths", nargs="+", type=Path)
    args = parser.parse_args(argv)

    entries = collect(args.paths)
    document = {
        "label": args.label,
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "roots": [str(path) for path in args.paths],
        "file_count": len(entries),
        "tree_sha256": tree_digest(entries),
        "entries": entries,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    tmp = args.out.with_suffix(args.out.suffix + ".tmp")
    tmp.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, args.out)
    print(f"[manifest] {len(entries)} files -> {args.out}")
    print(f"[manifest] tree_sha256={document['tree_sha256']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

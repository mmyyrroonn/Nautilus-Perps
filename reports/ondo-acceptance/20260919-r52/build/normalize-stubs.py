#!/usr/bin/env python3
"""Normalize generated .pyi files to LF.

The Windows stub generator writes text files in text mode (CRLF). The repository
stores LF. This rewrites only files that actually contain CRLF, so noise-only
regeneration output becomes byte-identical to the previous LF content while the
one semantic Ondo stub change is preserved.
"""
from __future__ import annotations

import sys
from pathlib import Path

root = Path(sys.argv[1])
changed: list[str] = []
for path in sorted(root.rglob("*.pyi")):
    data = path.read_bytes()
    normalized = data.replace(b"\r\n", b"\n")
    if normalized != data:
        path.write_bytes(normalized)
        changed.append(str(path))

print(f"normalized_crlf_files={len(changed)}")
for name in changed[:80]:
    print(f"  {name}")

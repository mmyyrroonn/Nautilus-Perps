"""Invoke the native repository's single source of wheel build logic."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--native-root", type=Path, required=True)
    parser.add_argument("--native-python", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--profile", choices=("release", "nextest"), default="release")
    parser.add_argument("--online", action="store_true")
    args = parser.parse_args()
    root = args.native_root.resolve(strict=True)
    script = root / "scripts" / "adapter-evidence" / "build_native.py"
    if not script.is_file():
        parser.error("native build entry is missing")
    command = [
        sys.executable, str(script), "--native-root", str(root),
        "--python", os.path.abspath(args.native_python),
        "--output-dir", str(args.output_dir.resolve()), "--profile", args.profile,
    ]
    if args.online:
        command.append("--online")
    return subprocess.run(command, check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())

"""Run offline application tests only after verifying the installed fork candidate."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

from verify_native_install import digest, verify

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, action="append", default=[])
    parser.add_argument("tests", nargs="*", default=["tests"])
    args = parser.parse_args()
    try:
        python = Path(os.path.abspath(args.python))
        if not python.is_file():
            raise ValueError("application interpreter is missing")
        if python != Path(os.path.abspath(sys.executable)):
            raise ValueError("run with the explicit application interpreter")
        if sys.version_info[:3] != (3, 12, 9):
            raise ValueError("application CPython 3.12.9 is required")
        identity = verify(
            args.wheel, args.sha256.lower(), provenance=args.provenance,
            require_source_binding=True, require_ondo=True,
        )
        env = os.environ.copy()
        for name in tuple(env):
            if name.startswith(("CONDA_", "ASTER_", "ONDO_", "HYPERLIQUID_", "LIGHTER_")):
                env.pop(name, None)
        command = [str(python), "-m", "pytest", *args.tests, "-q", "-p", "no:cacheprovider"]
        result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True, text=True, check=False)
        summary = re.findall(r"(?m)^.{0,180}(?:passed|failed|error|skipped).{0,180}$", result.stdout)
        record = {
            "schema_version": 1,
            "run_id": uuid.uuid4().hex,
            "generated_at": datetime.now(UTC).isoformat(),
            "candidate": identity,
            "config_sha256": {str(path): digest(path) for path in args.config},
            "test_collection": args.tests,
            "test_command": command,
            "exit_code": result.returncode,
            "summary": summary[-5:],
        }
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print("\n".join(record["summary"]) or f"pytest exited {result.returncode}")
        return result.returncode
    except (OSError, ValueError, KeyError, ImportError) as exc:
        parser.exit(2, f"integration refused: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())

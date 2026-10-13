"""Inspect Backpack native connectivity without financial commands or mutation authority."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "src"))
from backpack_production import OperatorError, OperatorPlan, inspect_native, load_limits, resolve_credentials


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", action="append", required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--limits", type=Path, default=APP_ROOT / "config/limits.toml")
    parser.add_argument("--timeout-secs", type=int, default=30)
    args = parser.parse_args()
    try:
        leg, total = load_limits(args.limits)
        plan = OperatorPlan(tuple(args.symbol), args.state_dir, leg, total, args.timeout_secs)
        result = asyncio.run(inspect_native(plan, resolve_credentials(args.env_file)))
    except OperatorError as error:
        result = {"mode": "native-read-only", "mutations": 0, "ready": False, "failure": str(error)}
    except Exception:
        result = {"mode": "native-read-only", "mutations": 0, "ready": False,
                  "failure": "native inspection failed; private error omitted"}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, separators=(",", ":")))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

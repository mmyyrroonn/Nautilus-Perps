"""Verify a flat account with signed GETs before a new isolated acceptance session."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_session import validate_flat
from backpack_readonly_contract import collect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists() or (args.output.parent.exists() and any(args.output.parent.iterdir())):
        parser.error("a new empty session directory is required; existing baselines cannot be replaced")
    # Include the signed request window so a small local clock lead cannot hide first fills.
    started_ms = time.time_ns() // 1_000_000 - 5000
    report = collect(args.env_file, ("positions", "orders"))
    report.update(mode="new-session-flat-preflight", native_node_created=False, ready=False,
                  recovery_start_ms=started_ms, completed_at_ms=time.time_ns() // 1_000_000)
    try:
        validate_flat(report)
        report["ready"] = True
    except ValueError as error:
        report["failure"] = str(error)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        output.write(json.dumps(report, indent=2, default=str) + "\n")
    print(json.dumps({key: report.get(key) for key in
        ("mode", "ready", "failure", "mutations", "native_node_created")}))
    return 0 if report["ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

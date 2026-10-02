#!/usr/bin/env python3
"""Validate a Backpack plan or run bounded credential-free native public observations."""
from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

from backpack_config import BackpackConfigError, load_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Validate without native imports, clients or sockets")
    parser.add_argument("--candidate-wheel", type=Path)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--native-provenance", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.config)
        if args.dry_run:
            print(json.dumps(plan.document(), indent=2))
            return 0
        if plan.mode != "public":
            raise BackpackConfigError("only public runtime is implemented; use --dry-run for other modes")
        candidate = (args.candidate_wheel, args.candidate_sha256, args.native_provenance)
        if any(value is not None for value in candidate) and not all(value is not None for value in candidate):
            raise BackpackConfigError("candidate verification requires wheel, SHA256 and native provenance")
        from backpack_public import run_public
        summary, path = asyncio.run(run_public(plan, candidate=candidate if candidate[0] else None))
        print(json.dumps({"status": summary["status"], "failure": summary["failure"],
                          "summary_path": str(path), "execution_ready": False}))
        return 0 if summary["status"] == "completed" else 1
    except (BackpackConfigError, OSError) as e:
        message = str(e) if isinstance(e, BackpackConfigError) else "cannot read or publish session files"
        print(f"Backpack configuration refused: {message}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Backpack public session interrupted; inspect bounded run evidence", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

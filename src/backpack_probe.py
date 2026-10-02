#!/usr/bin/env python3
"""Print a validated Backpack session plan without starting a runtime."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from backpack_config import BackpackConfigError, load_plan


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true", help="Validate and print without credentials, clients or sockets")
    args = parser.parse_args(argv)
    if not args.dry_run:
        parser.error("runtime execution is unavailable in this configuration-only entry point; use --dry-run")
    try:
        plan = load_plan(args.config)
    except (BackpackConfigError, OSError) as exc:
        # Do not echo source lines or arbitrary file/credential content.
        message = str(exc) if isinstance(exc, BackpackConfigError) else "cannot read configuration file"
        print(f"Backpack configuration refused: {message}", file=sys.stderr)
        return 2
    print(json.dumps(plan.document(), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

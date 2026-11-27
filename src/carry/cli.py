"""CLI entry point for Route A Cross-Exchange Carry (defaults safely to --dry-run)."""
from __future__ import annotations

import argparse
from dataclasses import asdict
from decimal import Decimal
import json
from pathlib import Path
import sys

from .config import CarryConfigError, ledger_domain_for_mode, load_carry_plan
from .ledger import LedgerOwnershipError


def _serialize(obj: object) -> object:
    if isinstance(obj, (Decimal, Path)):
        return str(obj)
    if isinstance(obj, tuple):
        return [_serialize(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _serialize(v) for k, v in obj.items()}
    if hasattr(obj, "__dataclass_fields__"):
        return {k: _serialize(getattr(obj, k)) for k in obj.__dataclass_fields__}
    return obj


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Route A Cross-Exchange Dynamic Funding Carry")
    parser.add_argument("--config", type=Path, required=True, help="Path to carry-*.toml config")
    parser.add_argument("--mode", choices=("paper", "readonly", "live"), help="Override configured mode")
    parser.add_argument("--dry-run", action="store_true", help="Validate config & print resolved caps without connecting (default)")
    parser.add_argument("--run", action="store_true", help="Actually start the runtime (paper/readonly)")
    parser.add_argument("--execute-live", action="store_true", help="Required explicit flag to permit mainnet order execution")
    parser.add_argument("--duration-secs", type=int, default=None, help="Optional hard runtime cap in seconds")
    args = parser.parse_args(argv)

    try:
        plan = load_carry_plan(args.config)
    except CarryConfigError as exc:
        print(f"[carry] ConfigError: {exc}", file=sys.stderr)
        return 2

    mode = args.mode or plan.mode
    if ledger_domain_for_mode(plan.mode) != ledger_domain_for_mode(mode):
        print(
            f"[carry] Refusing cross-domain mode override ({plan.mode} -> {mode}) "
            f"on state_path {plan.state_path}; use the matching mode config file.",
            file=sys.stderr,
        )
        return 2

    if mode == "live" and not args.execute_live and not args.dry_run:
        if args.run:
            print("[carry] Refusing --mode live without explicit --execute-live", file=sys.stderr)
            return 2

    should_run = (args.run or args.execute_live) and not args.dry_run
    summary = {
        "action": "run" if should_run else "dry_run",
        "mode": mode,
        "fingerprint": plan.fingerprint(),
        "plan": _serialize(asdict(plan)),
    }
    print(json.dumps(summary, indent=2, sort_keys=True), flush=True)

    if not should_run:
        return 0

    from .runtime import run_node

    try:
        return run_node(plan, mode=mode, execute_live=args.execute_live, duration_secs=args.duration_secs)
    except LedgerOwnershipError as exc:
        print(f"[carry] LedgerOwnershipError: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

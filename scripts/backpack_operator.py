#!/usr/bin/env python3
"""Dry-run first; execute finite real native Backpack commands only with both explicit flags."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
import sys

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT / "src"))
from backpack_production import (
    OperatorError,
    OperatorPlan,
    checked_command,
    exact_positive,
    execute,
    load_limits,
    resolve_credentials,
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--symbol",
        action="append",
        required=True,
        help="native USDC perpetual symbol; repeat for a finite allowlist",
    )
    command_input = parser.add_mutually_exclusive_group(required=True)
    command_input.add_argument(
        "--command",
        action="append",
        help="JSON submit/query/cancel/cancel-replace/roundtrip; repeat for sequential commands",
    )
    command_input.add_argument(
        "--command-file", type=Path, help="UTF-8 JSON array of sequential commands"
    )
    parser.add_argument(
        "--state-dir",
        type=Path,
        default=APP_ROOT / "reports" / "backpack" / "operator-state",
    )
    parser.add_argument(
        "--limits",
        type=Path,
        default=APP_ROOT / "config" / "limits.toml",
        help="explicit TOML operator envelope",
    )
    parser.add_argument(
        "--max-leg-usd", help="exact positive decimal; may shrink configured leg limit"
    )
    parser.add_argument(
        "--max-total-usd",
        help="exact positive decimal; may shrink configured total limit",
    )
    parser.add_argument(
        "--timeout-secs",
        type=int,
        default=60,
        help="finite whole-run deadline, at most 600 seconds",
    )
    parser.add_argument(
        "--env-file",
        type=Path,
        help="explicit dotenv path; default resolves process environment only",
    )
    parser.add_argument(
        "--execute", action="store_true", help="run native live financial commands"
    )
    parser.add_argument(
        "--confirm-mainnet",
        action="store_true",
        help="explicitly confirm Backpack production endpoints for this invocation",
    )
    args = parser.parse_args(argv)
    try:
        try:
            if args.command_file is not None:
                if args.command_file.stat().st_size > 65536:
                    raise OperatorError("command file exceeds 64 KiB")
                raw_commands = json.loads(
                    args.command_file.read_text(encoding="utf-8-sig")
                )
            else:
                raw_commands = [json.loads(raw) for raw in args.command]
        except (OSError, UnicodeError, json.JSONDecodeError):
            raise OperatorError("command input is not readable JSON") from None
        if not isinstance(raw_commands, list) or not 1 <= len(raw_commands) <= 32:
            raise OperatorError("provide between 1 and 32 finite commands")
        configured_leg, configured_total = load_limits(args.limits)
        leg = (
            configured_leg
            if args.max_leg_usd is None
            else exact_positive(args.max_leg_usd)
        )
        total = (
            configured_total
            if args.max_total_usd is None
            else exact_positive(args.max_total_usd)
        )
        if leg > configured_leg or total > configured_total:
            raise OperatorError(
                "CLI limits may only shrink the configured Backpack envelope"
            )
        plan = OperatorPlan(
            tuple(args.symbol), args.state_dir, leg, total, args.timeout_secs
        )
        try:
            commands = tuple(checked_command(raw, plan) for raw in raw_commands)
        except json.JSONDecodeError:
            raise OperatorError("command is not valid JSON") from None
        if not args.execute:
            print(
                json.dumps(
                    {
                        "schema_version": 1,
                        "mode": "dry-run",
                        "symbols": list(plan.symbols),
                        "commands": commands,
                        "max_leg_usd": str(plan.max_leg_usd),
                        "max_total_usd": str(plan.max_total_usd),
                        "timeout_secs": plan.timeout_secs,
                        "credentials_resolved": False,
                        "native_node_created": False,
                    },
                    separators=(",", ":"),
                )
            )
            return 0
        if not args.confirm_mainnet:
            raise OperatorError("execute requires the explicit confirm-mainnet flag")
        seed = resolve_credentials(args.env_file)
        result = asyncio.run(execute(plan, commands, seed,
                                   progress=lambda row: print(json.dumps(row, separators=(",", ":")), flush=True)))
        print(json.dumps(result, separators=(",", ":")))
        return 0 if result["completed"] else 1
    except OperatorError as e:
        print(
            json.dumps({"completed": False, "failure": str(e)}, separators=(",", ":")),
            file=sys.stderr,
        )
        return 2
    except KeyboardInterrupt:
        print(
            json.dumps({"completed": False, "failure": "operator interrupted"}),
            file=sys.stderr,
        )
        return 130
    except Exception:
        # Native exceptions may contain private payloads. Only the fixed safe code is emitted.
        print(
            json.dumps({"completed": False, "failure": "native operator failed"}),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

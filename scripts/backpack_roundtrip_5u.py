#!/usr/bin/env python3
"""Launch finite Backpack acceptance; live execution requires two explicit flags."""

from __future__ import annotations

import argparse
from contextlib import nullcontext
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
import json
import os
from pathlib import Path
from queue import Empty, Queue
import signal
import subprocess
import sys
from threading import Thread
import time
from uuid import uuid4

APP_ROOT = Path(__file__).resolve().parents[1]
SYMBOL = "BTC_USDC_PERP"


class LauncherError(RuntimeError):
    pass


class SessionLock:
    """Hold a nonblocking Linux advisory lock for the entire live invocation."""

    def __init__(self, path):
        self.path = path
        self.handle = None

    def __enter__(self):
        if os.name != "posix":
            raise LauncherError("Live launcher requires Linux; use the PowerShell launcher on Windows")
        import fcntl

        self.handle = self.path.open("a+b")
        try:
            fcntl.flock(self.handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.handle.close()
            self.handle = None
            raise LauncherError("Another acceptance launcher is running; no commands were executed") from None
        return self

    def __exit__(self, *_):
        if self.handle is not None:
            self.handle.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("both", "maker", "market"), default="both",
                        help="entry style; every exit is reduce-only Market (both runs maker then market)")
    parser.add_argument("--maker-wait-seconds", type=int, default=120)
    parser.add_argument("--maker-reprice-seconds", type=int, default=5)
    parser.add_argument("--maker-max-reprices", type=int, default=23)
    parser.add_argument("--close-only", action="store_true")
    parser.add_argument("--entry-client-order-id")
    parser.add_argument("--max-close-quantity", default="0.00005")
    session = parser.add_mutually_exclusive_group()
    session.add_argument("--fresh-acceptance", action="store_true",
                         help="require signed flat preflight and create a new isolated state")
    session.add_argument("--state-dir", type=Path, help="existing absolute native state directory")
    parser.add_argument("--env-file", type=Path, default=APP_ROOT / ".env",
                        help="absolute credential file path; only live child scripts read it")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--confirm-mainnet", action="store_true")
    args = parser.parse_args(argv)
    if args.execute != args.confirm_mainnet:
        parser.error("live execution requires both --execute and --confirm-mainnet")
    if not 5 <= args.maker_wait_seconds <= 120:
        parser.error("maker wait must be between 5 and 120 seconds")
    if not ((args.maker_reprice_seconds == 0 and args.maker_max_reprices == 0)
            or (5 <= args.maker_reprice_seconds <= args.maker_wait_seconds and 1 <= args.maker_max_reprices <= 23)):
        parser.error("maker repricing requires 0/0 to disable, or a 5-second minimum interval within the leg deadline and 1 to 23 replacements")
    if not args.env_file.is_absolute():
        parser.error("--env-file must be an absolute path")
    if args.state_dir is not None:
        if not args.state_dir.is_absolute() or not args.state_dir.is_dir():
            parser.error("--state-dir must be an existing absolute native state directory")
        args.state_dir = args.state_dir.resolve()
    if args.close_only:
        if args.scenario == "both" or not args.entry_client_order_id or args.state_dir is None:
            parser.error("close-only requires maker or market, an entry client order ID, and --state-dir")
        if args.fresh_acceptance:
            parser.error("close-only cannot create a fresh acceptance session")
        try:
            quantity = Decimal(args.max_close_quantity)
            if not quantity.is_finite() or quantity <= 0:
                raise InvalidOperation
        except InvalidOperation:
            parser.error("max-close-quantity must be an exact positive decimal")
    elif args.entry_client_order_id is not None:
        parser.error("entry-client-order-id is only valid with close-only")
    if args.execute and not (args.fresh_acceptance or args.state_dir is not None):
        parser.error("live execution requires explicit --fresh-acceptance or --state-dir")
    return args


def stop_child(child):
    """Allow native interruption cleanup before escalating a stuck child."""
    if child.poll() is not None:
        return
    child.send_signal(signal.SIGINT)
    try:
        child.wait(timeout=20)
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def run_child(command, log_path, *, timeout):
    """Stream only existing child diagnostics, preserving its final JSON row."""
    child = subprocess.Popen(command, cwd=APP_ROOT, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, encoding="utf-8",
                             errors="replace", bufsize=1)
    rows = Queue()

    def read_lines():
        try:
            for line in child.stdout:
                rows.put(line)
        finally:
            child.stdout.close()
            rows.put(None)

    reader = Thread(target=read_lines, daemon=True)
    reader.start()
    result = None
    deadline = time.monotonic() + timeout
    try:
        with log_path.open("w", encoding="utf-8") as log:
            while True:
                if time.monotonic() >= deadline:
                    raise LauncherError("Child deadline exceeded; inspect persistent state before any repeat")
                try:
                    line = rows.get(timeout=0.25)
                except Empty:
                    continue
                if line is None:
                    break
                log.write(line)
                log.flush()
                print(line, end="", flush=True)
                try:
                    document = json.loads(line)
                    if isinstance(document, dict) and (
                        type(document.get("completed")) is bool or document.get("mode") == "dry-run"
                    ):
                        result = document
                except ValueError:
                    pass
        return child.wait(timeout=max(0.01, deadline - time.monotonic())), result
    finally:
        stop_child(child)
        reader.join(timeout=1)


def launch(args, run, state):
    python = APP_ROOT / ".venv" / "bin" / "python"
    if not python.is_file():
        raise LauncherError("Application Linux environment .venv/bin/python is missing")
    if args.execute and not args.env_file.is_file():
        raise LauncherError("Configured credential environment file is missing")
    mode = "maker-market" if args.scenario == "both" else args.scenario
    if args.close_only:
        mode = "close-market"
    command_file = run / f"{SYMBOL}.{mode}.commands.json"
    lock = SessionLock(run.parent / "operator-session.lock") if args.execute else nullcontext()
    with lock:
        if args.fresh_acceptance and args.execute:
            print("READ ONLY preflight: require no open orders and a flat account.", flush=True)
            code, _ = run_child([
                str(python), str(APP_ROOT / "scripts" / "backpack_acceptance_session.py"),
                "--env-file", str(args.env_file), "--output", str(state / "session-baseline.json"),
            ], run / "preflight.log", timeout=60)
            if code:
                raise LauncherError("Flat preflight failed; no financial commands were executed")
        if args.close_only:
            close_command = {
                "action": "close-only", "symbol": SYMBOL, "style": "market",
                "max_quantity": args.max_close_quantity,
                "entry_client_order_id": args.entry_client_order_id,
                "wait_seconds": 10,
            }
            command_file.write_text(json.dumps([close_command], indent=2) + "\n", encoding="utf-8")
        else:
            code, _ = run_child([
                str(python), str(APP_ROOT / "scripts" / "backpack_acceptance_plan.py"),
                "--symbol", SYMBOL, "--scenario", mode, "--max-leg-usdc", "4.5",
                "--maker-wait-secs", str(args.maker_wait_seconds),
                "--maker-reprice-seconds", str(args.maker_reprice_seconds),
                "--maker-max-reprices", str(args.maker_max_reprices), "--output-dir", str(run),
            ], run / "plan.log", timeout=60)
            if code:
                raise LauncherError("Fresh sizing failed; no financial commands were executed")
        command = [
            str(python), str(APP_ROOT / "scripts" / "backpack_operator.py"),
            "--symbol", SYMBOL, "--command-file", str(command_file),
            "--limits", str(APP_ROOT / "config" / "limits.toml"),
            "--max-leg-usd", "5", "--max-total-usd", "5",
            "--timeout-secs", "600", "--state-dir", str(state),
        ]
        if args.execute:
            print(f"MAINNET: {mode}; selected entry style -> reduce-only Market exit.", flush=True)
            print("Sizing target: 4.5 USDC; submission valuation cap: 5 USDC.", flush=True)
            command += ["--env-file", str(args.env_file), "--execute", "--confirm-mainnet"]
        else:
            print("DRY RUN: public sizing and command validation only; no credentials or native node.", flush=True)
        print(f"Maker entry only: reprice interval={args.maker_reprice_seconds}s; "
              f"maximum replacements={args.maker_max_reprices}; total entry deadline={args.maker_wait_seconds}s.", flush=True)
        print("Every replacement waits for cancellation and fill reconciliation; incomplete groups stop later entries.", flush=True)
        code, result = run_child(command, run / "operator.log", timeout=630)
        if result is None:
            raise LauncherError("Operator produced no final result; inspect persistent state before any repeat")
        return code, result


def main(argv=None):
    args = parse_args(argv)
    mode = "maker-market" if args.scenario == "both" else args.scenario
    if args.close_only:
        mode = "close-market"
    stamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S-%f")
    run = APP_ROOT / "reports" / "backpack" / "acceptance" / f"run-{mode}-{stamp}-{uuid4().hex[:8]}"
    state = (run / "state" if args.fresh_acceptance else args.state_dir
             or APP_ROOT / "reports" / "backpack" / "operator-state")
    run.mkdir(parents=True, exist_ok=False)
    code = 1
    result = {"completed": False}
    try:
        code, result = launch(args, run, state)
    except LauncherError as error:
        result["failure"] = str(error)
    except KeyboardInterrupt:
        code = 130
        result["failure"] = "Launcher interrupted; inspect persistent state before any repeat"
    except (OSError, subprocess.SubprocessError):
        result["failure"] = "Launcher I/O or child process failed; inspect saved reports and persistent state"
    finally:
        result["launcher"] = {
            "execute_requested": args.execute, "fresh_acceptance": args.fresh_acceptance,
            "scenario": mode, "run_directory": str(run), "state_directory": str(state),
            "exit_code": code,
        }
        (run / "result.json").write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    for trip in result.get("roundtrips", []):
        for leg in trip.get("legs", []):
            print(f"LEG: {leg.get('order_style', trip.get('style'))} {leg.get('side')}; "
                  f"filled_qty={leg.get('filled_quantity')}; USDC={leg.get('actual_notional_usdc')}; "
                  f"liquidity_verified={leg.get('liquidity_verified')}; order={leg.get('venue_order_id')}")
    if args.execute:
        print(f"Acceptance completed={result.get('completed', False)}; "
              f"account_reconciled={result.get('account_reconciled', False)}; "
              f"failure={result.get('failure')}")
    print(f"Result: {run / 'result.json'}")
    print(f"Persistent state: {state}")
    if result.get("failure"):
        print(f"Failure: {result['failure']}")
    print("Inspect the saved result before any repeat, especially residual quantities or unknown orders.")
    return code


if __name__ == "__main__":
    raise SystemExit(main())

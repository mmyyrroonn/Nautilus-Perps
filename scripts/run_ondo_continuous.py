"""Prepare the reviewed candidate, or let a human launch its mainnet startup check.

The default path is entirely offline. --execute is for an interactive operator;
it runs the existing native Strategy startup sequence and stops after confirmed Flat.
It does not install a trading policy or retry a failed/ambiguous sequence.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sys
import threading
import time
import tomllib
import uuid

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))
from ondo_continuous import ContinuousLimits, ContinuousSession, build_session
from verify_native_install import digest, verify

ACCEPTANCE = APP / "reports/ondo-continuous/20261013-offline/acceptance.json"
WHEEL_SHA = "6d63d32c70e42bef4a5bd783887f889b7c222f18fe14d6ef00025f42edd863de"
PROVENANCE_SHA = "cd81154b9aa9a287e653a1bca34e7e91d08ed2935619f69bf0235735c70fb5d0"
STRATEGY_SHA = "aef4e2030d6e476feeb72c5651fcbf44ce4b8594f03e1be48de237b20af6a488"
DEFAULT_ENV = Path("E:/persarb/Nautilus-Perps/.env")


def prepare() -> tuple[ContinuousLimits, dict]:
    accepted = json.loads(ACCEPTANCE.read_text(encoding="utf-8"))
    provenance = Path(accepted["provenance_path"])
    if digest(provenance) != PROVENANCE_SHA:
        raise ValueError("native provenance differs from the accepted candidate")
    if digest(APP / "src/ondo_continuous.py") != STRATEGY_SHA:
        raise ValueError("continuous Strategy differs from offline acceptance")
    if Path(sys.executable).resolve() != Path(accepted["installed"]["interpreter"]).resolve():
        raise ValueError("use the isolated interpreter recorded in acceptance.json")
    identity = verify(
        Path(accepted["wheel"]["path"]), WHEEL_SHA,
        provenance=provenance, require_source_binding=True, require_ondo=True,
    )
    # Honor tighter account budgets without inheriting the historical probe's
    # single-entry, symbol, quantity or USD 10-20 target-band restrictions.
    with (APP / "config/limits.toml").open("rb") as stream:
        table = tomllib.load(stream)["ondo_trade"]
    order = Decimal(str(table["max_notional_per_order_usd"]))
    gross = Decimal(str(table["max_gross_exposure_usd"]))
    margin = Decimal(str(table["min_available_margin_usdc"]))
    if not all(value.is_finite() and value > 0 for value in (order, gross, margin)):
        raise ValueError("account budgets must be positive finite decimals")
    limits = ContinuousLimits(
        max_notional_per_order_usd=str(min(order, Decimal("20"))),
        max_gross_exposure_usd=str(min(gross, Decimal("40"))),
        min_available_margin_usdc=str(max(margin, Decimal("25"))),
    )
    limits.validate()
    return limits, {
        "mode": "offline-preparation",
        "credentials_read": False,
        "venue_contacted": False,
        "orders_sent": False,
        "candidate": identity,
        "strategy_sha256": STRATEGY_SHA,
        "launcher_sha256": digest(Path(__file__)),
        "limits": asdict(limits),
        "instrument": "BTC-USD-PERP.ONDO",
        "sequence": "post-only cancel/replace, protected IOC entry, exact reduce-only exit",
        "after_startup": "stop after confirmed Flat; no automatic trading policy",
        "residual_handling": "retain journal and report incomplete; no guessed close or replay",
        "launcher_live_verified": False,
    }


def execute(env_file: Path, limits: ContinuousLimits, prepared: dict) -> int:
    if not sys.stdin.isatty():
        raise ValueError("execution requires an interactive human operator")
    print(f"REAL MAINNET: BTC startup check; USD {limits.max_notional_per_order_usd}/order, "
          f"USD {limits.max_gross_exposure_usd} gross maximum.")
    print("A maker cancel/fill race or partial exit can leave a real position.")
    print("No automatic retry. Review residual exposure in the venue if incomplete.")
    if input("Type 上主网 to start this run: ").strip() != "上主网":
        raise ValueError("operator did not confirm this run")

    # Explicit file only; do not discover another checkout or inherit another account.
    from dotenv import dotenv_values

    values = dotenv_values(env_file, interpolate=False)
    names = ("ONDO_MAINNET_API_KEY", "ONDO_MAINNET_API_SECRET", "ONDO_MAINNET_ACCOUNT_ID")
    if any(not isinstance(values.get(name), str) or not values[name] for name in names):
        raise ValueError("explicit .env lacks the three required ONDO_MAINNET entries")
    key, secret, account = (values[name] for name in names)
    if len(account) > 128 or any(character.isspace() for character in account):
        raise ValueError("invalid venue account identity")
    run_id = datetime.now(timezone.utc).strftime("continuous-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    output = APP / "reports/live/ondo-continuous" / run_id
    output.mkdir(parents=True, exist_ok=False)
    session = ContinuousSession(
        environment="production", execution_enabled=True, mainnet_authorized=True,
        account_id="ONDO-" + account, expected_venue_account_id=account,
        api_key=key, api_secret=secret, diagnostics_run_id=run_id,
        journal_path=output / "journal.jsonl", limits=limits,
    )
    session.validate()

    def save(document: dict) -> None:
        encoded = json.dumps(document, indent=2, sort_keys=True, default=str)
        # Native diagnostics are sanitized; also redact exact credential values
        # (including JSON-escaped forms) before writing application evidence.
        for value in sorted((key, secret, account), key=len, reverse=True):
            encoded = encoded.replace(json.dumps(value)[1:-1], "[REDACTED]")
        temporary = output / "result.tmp"
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(output / "result.json")

    result = {**prepared, "mode": "mainnet-operator-run", "run_id": run_id,
              "credentials_read": True, "venue_contacted": "not_yet_started",
              "orders_sent": "derive_from_native_events", "complete": False}
    save(result)
    print(f"Run: {run_id}\nEvidence: {output / 'result.json'}")
    node = factory = strategy = None
    finished = threading.Event()
    timed_out = threading.Event()
    observer = None
    begin = time.monotonic()
    try:
        node, factory, strategy = build_session(session, startup_timeout_secs=240)
        handle = node.handle()

        def monitor() -> None:
            while not finished.wait(0.1):
                if strategy.failures or strategy.startup_verified:
                    handle.stop()
                    return
                if time.monotonic() - begin > 300:
                    timed_out.set()
                    handle.stop()
                    return

        observer = threading.Thread(target=monitor, daemon=True)
        observer.start()
        result["venue_contacted"] = "native_connection_attempted"
        save(result)
        node.run()
    except (Exception, KeyboardInterrupt) as exc:
        # Raw SDK exceptions can contain remote response data. Keep only the type.
        result["node_error_type"] = type(exc).__name__
    finally:
        finished.set()
        if observer is not None:
            observer.join(2)
        if strategy is not None:
            try:
                result["observation"] = strategy.report()
                from nautilus_trader.model import ClientOrderId

                result["orders"] = []
                for operation in result["observation"]["operations"]:
                    if operation["operation"] != "create":
                        continue
                    order = strategy.cache.order(ClientOrderId.from_str(operation["client_order_id"]))
                    if order is not None:
                        result["orders"].append({
                            "client_order_id": str(order.client_order_id),
                            "venue_order_id": str(order.venue_order_id) if order.venue_order_id else None,
                            "instrument_id": str(order.instrument_id),
                            "filled_quantity": str(order.filled_qty.as_decimal()),
                            "closed": order.is_closed,
                        })
            except Exception as exc:
                result["observation_error_type"] = type(exc).__name__
        if factory is not None:
            try:
                result["final_snapshot"] = factory.production_trade_snapshot()
                result["shutdown"] = factory.production_shutdown_diagnostics()
            except Exception as exc:
                result["snapshot_error_type"] = type(exc).__name__
        result["timed_out"] = timed_out.is_set()
        result["elapsed_secs"] = time.monotonic() - begin
        snapshot = result.get("final_snapshot") or {}
        observation = result.get("observation") or {}
        result["complete"] = bool(
            observation.get("startup_verified") and not observation.get("failures")
            and snapshot.get("complete") is True
            and snapshot.get("reconciled_flat") is True
            and snapshot.get("unknown_submissions") == 0
            and not result.get("node_error_type") and not timed_out.is_set()
        )
        result["launcher_live_verified"] = result["complete"]
        save(result)
        if node is not None:
            try:
                node.dispose()
            except Exception as exc:
                result["dispose_error_type"] = type(exc).__name__
                result["complete"] = False
                result["launcher_live_verified"] = False
                save(result)
    print(f"Complete: {result['complete']}; evidence: {output / 'result.json'}")
    if not result["complete"]:
        print("INCOMPLETE: inspect real venue orders/positions; retain journal; do not blindly rerun.")
    return 0 if result["complete"] else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--execute", action="store_true", help="interactive human mainnet launch")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV)
    args = parser.parse_args()
    try:
        limits, prepared = prepare()
        if args.execute:
            return execute(args.env_file, limits, prepared)
        print(json.dumps(prepared, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        # Never expose credentials or SDK exception bodies to a terminal.
        print(f"Launcher refused ({type(exc).__name__}); no automatic retry.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

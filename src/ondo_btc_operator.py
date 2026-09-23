"""Human-operated, one-attempt BTC test launcher; preparation is the default.

No private connection is opened here. Only an interactive operator can dispatch the
existing trade probe. A durable, exclusive claim blocks concurrent runs and ALL later
live attempts through this launcher, even after a refusal or crash. Never auto-reset it.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP / "src"))
IDENTITY = APP / "reports/ondo-acceptance/20260923-freshness/candidate-identity.json"
IDENTITY_SHA256 = "1a8441b650857b0d8a2972cd36def69e37d856b085c33085c580e43a93d67211"
LIMITS = APP / "config/ondo_btc_test.toml"
OUTPUT = APP / "reports/ondo-acceptance/btc-operator"
CLAIM = OUTPUT / "MAINNET_ATTEMPT_CLAIMED.json"
ENV_FILE = APP.parents[1] / ".env"
MAX_CAPTURE_AGE_SECS = 60
PHRASE = "上主网"


class Refused(ValueError):
    """Fail closed without printing private data."""


def now() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def digest(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def write_once(path: Path, document: dict) -> None:
    # Exclusive creation is also the dispatch claim. A partial file still blocks reuse.
    with path.open("x", encoding="utf-8") as output:
        json.dump(document, output, indent=2, ensure_ascii=False, sort_keys=True)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())


def verify_candidate() -> dict:
    if Path(sys.prefix).resolve() != (APP / ".venv").resolve():
        raise Refused("Use this worktree's isolated .venv, not the root or conda interpreter")
    if digest(IDENTITY) != IDENTITY_SHA256:
        raise Refused("The reviewed candidate identity changed; rebuild/review is required")
    identity = read_json(IDENTITY)
    if digest(Path(identity["wheel"])) != identity["wheel_sha256"]:
        raise Refused("Candidate wheel hash mismatch")
    # Locate the actual imported native module; no client/factory/session is created.
    native = Path(importlib.import_module("nautilus_trader._libnautilus").__file__).resolve()
    if not native.is_relative_to((APP / ".venv").resolve()):
        raise Refused("The native module is outside the isolated environment")
    if digest(native) != identity["binary_sha256"]:
        raise Refused("Installed native binary hash mismatch")
    for row in identity["app_source_files"]:
        if digest(APP / row["path"]) != row["sha256"]:
            raise Refused(f"Reviewed source changed: {row['path']}")
    return {
        "candidate_identity_sha256": IDENTITY_SHA256,
        "wheel_sha256": identity["wheel_sha256"],
        "binary_sha256": identity["binary_sha256"],
        "operator_script_sha256": digest(Path(__file__)),
        "launcher_sha256": digest(APP / "scripts/run_ondo_btc_test.ps1"),
        "dms_release_host_contract_verified": False,
    }


def child_environment(*, private: bool = False) -> dict[str, str]:
    # Do not inherit another account/endpoint or load credentials in this process.
    env = {key: value for key, value in os.environ.items()
           if not key.upper().startswith(("ONDO_", "PYTHON"))}
    if private:
        env["ONDO_PROBE_ENV_FILE"] = str(ENV_FILE)
    return env


def fetch_public(capture: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-I", str(APP / "src/ondo_preflight.py"),
         "--symbols", "BTC", "--out", str(capture)],
        cwd=APP, env=child_environment(), check=False,
    )
    if result.returncode != 0:
        raise Refused("Public preflight failed; no private connection or order was started")


def one(rows, key: str, value: str) -> dict:
    found = [row for row in rows if row.get(key) == value]
    if len(found) != 1:
        raise Refused(f"Expected exactly one public {key}={value} row")
    return found[0]


def positive(value) -> Decimal:
    if not isinstance(value, str):
        raise Refused("Public decimal fields must be exact strings")
    number = Decimal(value)
    if not number.is_finite() or number <= 0:
        raise Refused("Invalid positive public decimal")
    return number


def fresh(meta: dict) -> None:
    fetched = datetime.fromisoformat(meta["fetched_at_utc"])
    started = datetime.fromisoformat(meta["started_at_utc"])
    current = now()
    if fetched.tzinfo is None or started.tzinfo is None:
        raise Refused("Public capture timestamps must include a timezone")
    if not started <= fetched <= current or not 0 <= (current - started).total_seconds() <= MAX_CAPTURE_AGE_SECS:
        raise Refused("Public capture expired (60 seconds); stop and prepare a NEW plan")


def load_capture(capture: Path) -> tuple[dict, dict, dict, dict]:
    meta = read_json(capture / "meta.json")
    if (meta.get("complete") is not True or meta.get("environment") != "production"
            or meta.get("requested_symbols") != ["BTC"]
            or meta.get("run_dir") != f"runs/{meta.get('run_id')}" or not meta.get("run_id")):
        raise Refused("A complete BTC production public capture is required")
    fresh(meta)
    payloads = {}
    for name in ("markets", "contracts", "instruments"):
        raw = (capture / f"{name}.json").read_bytes()
        expected = meta["files"][name]
        if (hashlib.sha256(raw).hexdigest() != expected["sha256"]
                or len(raw) != expected["bytes"]):
            raise Refused(f"Public {name} payload differs from its manifest")
        payloads[name] = json.loads(raw)
    market = one(payloads["markets"]["perps"]["tradingPairs"], "market", "BTC-USD.P")
    contract = one(payloads["contracts"], "market", "BTC-USD.P")
    instrument = one(payloads["instruments"], "symbol", "BTC")
    if (contract.get("disabled") is not False or contract.get("isClosed") is not False
            or market.get("disabled", False) is not False
            or instrument.get("market_status") != "active"):
        raise Refused("BTC market is disabled/closed or its status is unknown")
    if (instrument.get("instrument_id") != "BTC-USD-PERP.ONDO"
            or instrument.get("raw_market") != "BTC-USD.P"
            or instrument.get("quote_currency") != "USD"
            or instrument.get("settlement_currency") != "USDC"):
        raise Refused("BTC instrument identity/currency mismatch")
    if (positive(instrument["price_increment"]) != positive(market["quoteIncrement"])
            or positive(instrument["size_increment"]) != positive(market["baseIncrement"])):
        raise Refused("BTC public metadata increments disagree")
    if positive(contract["bid"]) > positive(contract["ask"]):
        raise Refused("Crossed public quote")
    return meta, market, contract, instrument


def make_plan(capture: Path, run_dir: Path, side: str, candidate: dict) -> dict:
    from ondo_trade_limits import load_trade_limits
    from ondo_trade_probe import parse_trade_plan, plan_sha256

    if side not in ("buy", "sell"):
        raise Refused("Choose buy or sell explicitly")
    limits = load_trade_limits(LIMITS)
    if not limits.limits_configured or limits.symbol != "BTC":
        raise Refused("A complete explicit BTC limits profile is required")
    meta, market, contract, instrument = load_capture(capture)
    tick, step = positive(instrument["price_increment"]), positive(instrument["size_increment"])
    bid, ask = positive(contract["bid"]), positive(contract["ask"])
    slip = limits.max_slippage_bps / Decimal("10000")
    buy = (ask * (1 + slip) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    sell = (bid * (1 - slip) / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    # Floor size, never increase it to meet an assumed venue minimum. Cover both prices.
    quantity = (limits.entry_notional_usd / max(buy, sell) / step).to_integral_value(rounding=ROUND_FLOOR) * step
    if quantity <= 0 or buy < ask or not 0 < sell <= bid:
        raise Refused("No executable lot fits the unchanged budget and price boundaries")
    document = {
        "plan_version": 1,
        "instrument": {
            "symbol": "BTC", "instrument_id": "BTC-USD-PERP.ONDO",
            "price_increment": str(tick), "size_increment": str(step),
            "min_quantity": None, "min_quantity_source": "unpublished",
            "min_notional": None, "min_notional_currency": None,
            "min_notional_source": "unpublished",
            "max_quantity": str(positive(market["maxPositionBaseSize"])), "quote_currency": "USD",
        },
        "entry": {
            "side": side, "order_type": "limit", "time_in_force": "IOC", "reduce_only": False,
            "quantity": str(quantity), "limit_price": str(buy if side == "buy" else sell),
            "max_slippage_bps": str(limits.max_slippage_bps),
            "notional_usd": str(limits.entry_notional_usd), "max_quote_age_secs": "5",
        },
        "close": {
            "side": "sell" if side == "buy" else "buy",
            "order_type": "limit", "time_in_force": "IOC", "reduce_only": True,
            "limit_price": str(sell if side == "buy" else buy),
            "max_close_attempts": limits.max_close_attempts, "max_quote_age_secs": "5",
        },
        "envelope": {
            "max_notional_per_order_usd": str(limits.max_notional_per_order_usd),
            "max_gross_exposure_usd": str(limits.max_gross_exposure_usd),
            "max_orders": limits.max_orders, "max_new_risk_requests": limits.max_new_risk_requests,
            "max_app_requests": limits.max_app_requests,
            "min_available_margin_usdc": str(limits.min_available_margin_usdc),
            "deadline_secs": limits.deadline_secs, "require_flat_start": True,
        },
        "account": {
            "environment": "production", "journal_path": str(run_dir / "journal.json"),
            # Required at runtime, not a claim that the credential has already been read.
            "expected_venue_account_id_present": True,
        },
        "dms": {
            "mode": "trading", "activation": "subscribe cancelAllOrdersAfterPerps on login",
            "timeout_secs": 30, "renewal_interval_secs": 15, "renewal_message_verified": False,
            "release_on_stop": "only when nothing is unconfirmed",
            "account_wide_effect": "a lapsed timer cancels every resting order account-wide",
        },
        "cleanup": {
            "cancel_own_orders_only": True, "confirm_cancels": True,
            "confirm_flat_position": True, "stop_budget_secs": limits.cleanup_budget_secs,
        },
        "authorization": {"user_phrase_上主网_present": False, "approved_plan_sha256": None},
        "operator_context": {
            "run_id": run_dir.name, "confirmation_source": "interactive_local_operator",
            "agent_executed": False, "account_readiness_requires_live_reconciliation": True,
        },
        "public_evidence": {
            "run_id": meta["run_id"], "fetched_at_utc": meta["fetched_at_utc"],
            "started_at_utc": meta["started_at_utc"], "manifest_sha256": digest(capture / "meta.json"),
            "bid": str(bid), "ask": str(ask),
        },
        "candidate_evidence": candidate,
    }
    document["plan_sha256"] = plan_sha256(document)
    parse_trade_plan(document, limits)
    return document


def show_plan(plan: dict, run_dir: Path) -> None:
    entry, close = plan["entry"], plan["close"]
    actual = Decimal(entry["quantity"]) * Decimal(entry["limit_price"])
    print(f"\nBTC MAINNET / {entry['side']} -> reduce-only {close['side']}")
    print(f"Quantity: {entry['quantity']} BTC; entry/close price boundaries: "
          f"{entry['limit_price']} / {close['limit_price']} USD")
    print(f"Quantity x entry limit: {actual} USD; entry budget: 15 USD; order/gross caps: 20/20 USD")
    print("1 entry, at most 2 reduce-only exits; 120s deadline; 25 USDC available-margin floor.")
    print("Venue minimum notional is unpublished. No automatic size increase or entry retry.")
    print("WARNING: DMS is UNCHANGED, not disabled; its release acknowledgment is still unverified.")
    print("DMS can cancel ALL account resting orders; it does NOT close positions or limit losses.")
    print("A failed close may leave BTC exposure. Do not run another strategy on this account.")
    print("Wait for the report. Incomplete shutdown is NOT a full mainnet acceptance pass.")
    print(f"Evidence directory: {run_dir}")
    print(f"Plan SHA256: {plan['plan_sha256']}")


def archive_claim(new_run_dir: Path) -> dict:
    """Move a reviewed claim into its own run directory; never delete it.

    The claim still blocks every *unreviewed* retry: this only runs when the operator passes
    `--supersede-claim`, and it preserves the old claim as evidence inside the old run.
    """
    claim = read_json(CLAIM)
    old_run_dir = Path(str(claim.get("run_dir", "")))
    if not old_run_dir.is_dir() or old_run_dir.resolve() == new_run_dir.resolve():
        raise Refused("The previous claim names no existing run directory; review it manually")
    target = old_run_dir / f"MAINNET_ATTEMPT_CLAIMED.superseded-{new_run_dir.name}.json"
    if target.exists():
        raise Refused("A superseded claim record already exists; review before proceeding")
    os.replace(CLAIM, target)
    return {
        "superseded_claim_path": str(target),
        "superseded_plan_sha256": claim.get("plan_sha256"),
        "superseded_run_dir": str(old_run_dir),
    }


def reviewed_policy(plan: dict) -> dict:
    """The reviewed, hash-excluded bounds a chat-sourced confirmation authorizes.

    This is what a policy-level confirmation binds: the instrument, the direction and the whole
    immutable envelope the launcher derives from `config/ondo_btc_test.toml`. It deliberately does
    not carry the prices or the plan hash, which are the live market's at dispatch.
    """
    return {
        "instrument_id": plan["instrument"]["instrument_id"],
        "entry_side": plan["entry"]["side"],
        "entry_notional_usd": plan["entry"]["notional_usd"],
        "max_notional_per_order_usd": plan["envelope"]["max_notional_per_order_usd"],
        "max_gross_exposure_usd": plan["envelope"]["max_gross_exposure_usd"],
        "max_orders": plan["envelope"]["max_orders"],
        "max_new_risk_requests": plan["envelope"]["max_new_risk_requests"],
        "max_app_requests": plan["envelope"]["max_app_requests"],
        "min_available_margin_usdc": plan["envelope"]["min_available_margin_usdc"],
        "deadline_secs": plan["envelope"]["deadline_secs"],
        "cleanup_stop_budget_secs": plan["cleanup"]["stop_budget_secs"],
        "dms_timeout_secs": plan["dms"]["timeout_secs"],
    }


def execute_once(plan: dict, run_dir: Path, *, supersede: bool,
                 confirmed_plan_sha256: str | None = None,
                 confirmed_policy: bool = False) -> int:
    interactive = sys.stdin.isatty() and sys.stdout.isatty()
    if confirmed_plan_sha256 is None and not confirmed_policy and not interactive:
        raise Refused("Live dispatch requires an interactive terminal; piped approval is refused")
    if CLAIM.exists() and not supersede:
        raise Refused("An attempt is already claimed. Review its report/account; do not delete the claim or retry")
    confirmation_source = "interactive_local_operator"
    if confirmed_plan_sha256 is not None:
        if confirmed_plan_sha256 != plan["plan_sha256"]:
            raise Refused("The confirmed plan hash is not this plan; nothing was dispatched")
        confirmation_source = "chat_operator_confirmation"
        print(f"Operator confirmed plan {plan['plan_sha256']} in the current conversation.")
    elif confirmed_policy:
        # The operator authorized the reviewed *policy*, not a hash that did not exist when they
        # spoke: the plan and its prices are the live market's, and the approved plan records both
        # the source and the exact bounds the authorization covered.
        confirmation_source = "chat_operator_confirmation"
        print(f"Operator confirmed the reviewed policy in the current conversation; this plan "
              f"({plan['plan_sha256']}) is the fresh plan under it.")
    else:
        expected = f"{PHRASE} {plan['plan_sha256']}"
        print(f"Type exactly: {expected}\nAnything else aborts. Confirmation expires with the public capture.")
        if input("> ").strip() != expected:
            raise Refused("Confirmation did not match; nothing was dispatched")

    # Recheck after operator think-time; no auto-refresh of approved prices or hashes.
    candidate = verify_candidate()
    draft = read_json(run_dir / "draft-plan.json")
    rebuilt = make_plan(run_dir / "public", run_dir, plan["entry"]["side"], candidate)
    if draft != plan or rebuilt != plan:
        raise Refused("Plan, public evidence or candidate changed after display")
    fresh(plan["public_evidence"])
    approved = copy.deepcopy(plan)
    approved["authorization"] = {
        "user_phrase_上主网_present": True,
        "approved_plan_sha256": plan["plan_sha256"],
        "source": confirmation_source, "confirmed_at_utc": now().isoformat(),
    }
    if confirmed_policy and confirmed_plan_sha256 is None:
        approved["authorization"]["confirmed_policy"] = reviewed_policy(plan)
    if CLAIM.exists():
        if not supersede:
            raise Refused(
                "An attempt was claimed while this plan was being confirmed; nothing was dispatched"
            )
        approved["authorization"]["supersedes"] = archive_claim(run_dir)
        print(f"Previous claim archived as evidence: {approved['authorization']['supersedes']['superseded_claim_path']}")
    from ondo_trade_limits import load_trade_limits
    from ondo_trade_probe import authorization_problems, parse_trade_plan
    parsed = parse_trade_plan(approved, load_trade_limits(LIMITS))
    if authorization_problems(parsed):
        raise Refused("Approved plan failed authorization validation")
    approved_path = run_dir / "approved-plan.json"
    write_once(approved_path, approved)
    # Fixed claim, not just per-hash: new plans cannot race or auto-retry this test.
    write_once(CLAIM, {
        "plan_sha256": plan["plan_sha256"], "run_dir": str(run_dir),
        "claimed_at_utc": now().isoformat(), "state": "dispatch_claimed_not_proof_of_submission",
        "review_required_before_any_further_live_attempt": True,
    })
    print("Attempt claimed. Starting the existing bounded probe ONCE; no automatic restart.", flush=True)
    process = subprocess.Popen(
        [sys.executable, "-I", str(APP / "src/ondo_trade_probe.py"),
         "--mode", "production-trade", "--limits", str(LIMITS),
         "--plan", str(approved_path), "--confirm-production-trade",
         "--acknowledge-mainnet-authorization", "--out", str(run_dir / "trade")],
        cwd=APP, env=child_environment(private=True),
    )
    # Ctrl+C reaches the child through the shared console. subprocess.run would kill
    # it while unwinding KeyboardInterrupt, truncating its bounded cleanup. Do not
    # kill, restart, or background it; let the existing probe handle its shutdown.
    while True:
        try:
            exit_code = process.wait()
            break
        except KeyboardInterrupt:
            print("Interrupt received; waiting for the probe's own shutdown. Do not force-close the terminal.", flush=True)
    write_once(run_dir / "operator-result.json", {
        "plan_sha256": plan["plan_sha256"], "child_exit_code": exit_code,
        "finished_at_utc": now().isoformat(), "automatic_retry": False,
        "report": str(run_dir / "trade/trade.json"),
    })
    print(f"Probe exit code: {exit_code}; report: {run_dir / 'trade/trade.json'}")
    print("Claim retained. Do not retry until the report and authoritative account state are reviewed.")
    return exit_code


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--side", choices=("buy", "sell"), required=True)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--execute", action="store_true", help="require interactive approval before one live attempt")
    mode.add_argument("--prepare-only", action="store_true", help="default; public data and unapproved draft only")
    parser.add_argument(
        "--supersede-claim", action="store_true",
        help="archive a reviewed previous claim and claim this fresh plan instead",
    )
    parser.add_argument(
        "--operator-confirmed-plan-sha256", metavar="SHA256",
        help="dispatch after the operator confirmed this exact plan hash in the current "
             "conversation; recorded with source=chat_operator_confirmation",
    )
    parser.add_argument(
        "--operator-confirmed-policy", action="store_true",
        help="dispatch one fresh plan under the reviewed bounds after the operator authorized "
             "that policy in the current conversation; recorded with source="
             "chat_operator_confirmation and the bounds the authorization covered",
    )
    args = parser.parse_args(argv)
    try:
        if args.supersede_claim and not args.execute:
            raise Refused("--supersede-claim only applies to --execute")
        if args.operator_confirmed_plan_sha256 is not None and not args.execute:
            raise Refused("--operator-confirmed-plan-sha256 only applies to --execute")
        if args.operator_confirmed_policy and not args.execute:
            raise Refused("--operator-confirmed-policy only applies to --execute")
        if args.operator_confirmed_policy and args.operator_confirmed_plan_sha256 is not None:
            raise Refused("Confirm the plan hash or the policy, never both")
        if args.execute and not args.operator_confirmed_policy \
                and args.operator_confirmed_plan_sha256 is None \
                and (not sys.stdin.isatty() or not sys.stdout.isatty()):
            raise Refused("Live dispatch requires an interactive terminal")
        if args.execute and CLAIM.exists() and not args.supersede_claim:
            raise Refused("This one-attempt launcher is already claimed; review before any further live attempt")
        candidate = verify_candidate()
        run_id = now().strftime("%Y%m%dT%H%M%S%fZ") + "-" + uuid.uuid4().hex[:12]
        run_dir = OUTPUT / run_id
        run_dir.mkdir(parents=True, exist_ok=False)
        fetch_public(run_dir / "public")
        plan = make_plan(run_dir / "public", run_dir, args.side, candidate)
        write_once(run_dir / "draft-plan.json", plan)
        show_plan(plan, run_dir)
        if not args.execute:
            print("PREPARE ONLY: unapproved draft; no .env read, private session or order dispatch.")
            return 0
        return execute_once(
            plan, run_dir, supersede=args.supersede_claim,
            confirmed_plan_sha256=args.operator_confirmed_plan_sha256,
            confirmed_policy=args.operator_confirmed_policy,
        )
    except (Refused, OSError, ValueError, KeyError, TypeError, ArithmeticError, EOFError) as exc:
        # Public failures may be detailed; never interpolate an exception from a private child.
        print(f"Stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        print("No automatic retry. If an attempt claim exists, inspect its run before any next action.", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Interrupted. A started probe may have acted; do NOT assume a flat account or retry.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

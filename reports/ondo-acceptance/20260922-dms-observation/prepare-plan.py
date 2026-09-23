"""Prepare an unapproved, one-use plan from fresh public evidence; never send a request."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
from pathlib import Path
import sys


REPORT = Path(__file__).resolve().parent
APP = REPORT.parents[2]
sys.path.insert(0, str(APP / "src"))

from ondo_trade_limits import load_trade_limits  # noqa: E402
from ondo_trade_probe import parse_trade_plan, plan_sha256  # noqa: E402


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def one(rows, key, value):
    selected = [row for row in rows if row.get(key) == value]
    if len(selected) != 1:
        raise ValueError(f"expected one {key}={value} row")
    return selected[0]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--capture", type=Path, required=True)
    parser.add_argument("--out", type=Path, default=REPORT / "draft-plan.json")
    args = parser.parse_args()
    meta = read(args.capture / "meta.json")
    if meta.get("complete") is not True or meta.get("environment") != "production":
        raise ValueError("a complete production public capture is required")
    fetched = datetime.fromisoformat(meta["fetched_at_utc"])
    age = (datetime.now(timezone.utc) - fetched).total_seconds()
    if not 0 <= age <= 60:
        raise ValueError("public evidence must be at most 60 seconds old")
    for name in ("markets", "contracts", "instruments"):
        data = (args.capture / f"{name}.json").read_bytes()
        if hashlib.sha256(data).hexdigest() != meta["files"][name]["sha256"]:
            raise ValueError(f"{name} differs from its capture manifest")

    market = one(read(args.capture / "markets.json")["perps"]["tradingPairs"],
                 "market", "NVDA-USD.P")
    contract = one(read(args.capture / "contracts.json"), "market", "NVDA-USD.P")
    instrument = one(read(args.capture / "instruments.json"), "symbol", "NVDA")
    if contract.get("disabled") is not False or contract.get("isClosed") is not False:
        raise ValueError("NVDA must be enabled and its underlying market open")
    if market.get("disabled", False) is not False or instrument["market_status"] != "active":
        raise ValueError("NVDA metadata is not active")
    tick = Decimal(instrument["price_increment"])
    step = Decimal(instrument["size_increment"])
    if tick != Decimal(market["quoteIncrement"]) or step != Decimal(market["baseIncrement"]):
        raise ValueError("metadata increments disagree")
    ask, bid = Decimal(contract["ask"]), Decimal(contract["bid"])
    if not ask.is_finite() or not bid.is_finite() or not 0 < bid <= ask:
        raise ValueError("executable public quote is invalid")
    entry_limit = (ask * Decimal("1.002") / tick).to_integral_value(rounding=ROUND_FLOOR) * tick
    close_limit = (bid * Decimal("0.998") / tick).to_integral_value(rounding=ROUND_CEILING) * tick
    # Preserve size and exposure caps; shorten entry admission to reserve native cleanup time
    document = read(APP / "reports/ondo-acceptance/20260922-mainnet-resume/retry-plan/plan.json")
    document["instrument"].update({
        "price_increment": str(tick), "size_increment": str(step),
        "max_quantity": market["maxPositionBaseSize"],
    })
    document["entry"]["limit_price"] = str(entry_limit)
    document["close"]["limit_price"] = str(close_limit)
    limits = load_trade_limits(APP / "config/limits.toml")
    document["cleanup"]["stop_budget_secs"] = limits.cleanup_budget_secs
    document["account"]["journal_path"] = (
        "reports/ondo-acceptance/20260922-dms-observation/"
        f"journal-{meta['run_id']}.json"
    )
    document["authorization"] = {
        "user_phrase_上主网_present": False, "approved_plan_sha256": None,
    }
    document["public_evidence"] = {
        "run_id": meta["run_id"], "fetched_at_utc": meta["fetched_at_utc"],
        "bid": str(bid), "ask": str(ask), "contract_disabled": False,
        "underlying_market_closed": False,
    }
    identity = read(REPORT / "candidate-identity.json")
    document["candidate_evidence"] = {
        "candidate_identity_sha256": hashlib.sha256(
            (REPORT / "candidate-identity.json").read_bytes(),
        ).hexdigest(),
        "wheel_sha256": identity["wheel_sha256"],
        "binary_sha256": identity["binary_sha256"],
        "dms_release_host_contract_verified": False,
        "mainnet_test_ready": False,
        "reason": "draft only; DMS release semantics remain unresolved",
    }
    document["plan_sha256"] = plan_sha256(document)
    parse_trade_plan(document, limits)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    with args.out.open("x", encoding="utf-8") as output:
        json.dump(document, output, indent=2, sort_keys=True, ensure_ascii=False)
        output.write("\n")
    print(json.dumps({
        "plan": str(args.out), "plan_sha256": document["plan_sha256"],
        "approved": False, "mainnet_test_ready": False,
        "entry_limit": str(entry_limit), "close_limit": str(close_limit),
        "quantity": document["entry"]["quantity"], "requests_sent": 0,
    }, sort_keys=True))


if __name__ == "__main__":
    main()

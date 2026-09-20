"""Generate the hash-bound, user-authorized NVDA mainnet test plan from one public capture."""

from __future__ import annotations

import json
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from pathlib import Path
import sys


APP = Path("E:/Nautilus-Perps/.worktrees/ondo-production-native")
CAPTURE = APP / "reports/ondo-acceptance/20260920-mainnet-trade-preflight"
OUT = APP / "reports/ondo-acceptance/20260920-mainnet-trade-plan"
sys.path.insert(0, str(APP / "src"))

from ondo_trade_probe import plan_sha256  # noqa: E402


def main() -> None:
    contracts = json.loads((CAPTURE / "contracts.json").read_text(encoding="utf-8"))
    rows = [row for row in contracts if row.get("market") == "NVDA-USD.P"]
    if len(rows) != 1:
        raise RuntimeError("current public capture must contain exactly one NVDA contract")
    contract = rows[0]
    if contract.get("disabled") is not False:
        raise RuntimeError("NVDA contract is disabled or its disabled state is unknown")
    tick = Decimal("0.01")
    step = Decimal("0.01")
    ask = Decimal(str(contract["ask"]))
    bid = Decimal(str(contract["bid"]))
    slippage = Decimal("0.002")
    entry_limit = (ask * (Decimal(1) + slippage) / tick).to_integral_value(
        rounding=ROUND_CEILING,
    ) * tick
    close_limit = (bid * (Decimal(1) - slippage) / tick).to_integral_value(
        rounding=ROUND_FLOOR,
    ) * tick
    quantity = Decimal("0.06")
    if quantity * entry_limit > Decimal("14"):
        raise RuntimeError("the proposed quantity exceeds the USD14 entry ceiling")

    run_id = json.loads((CAPTURE / "meta.json").read_text(encoding="utf-8"))["run_id"]
    document = {
        "plan_version": 1,
        "instrument": {
            "symbol": "NVDA",
            "instrument_id": "NVDA-USD-PERP.ONDO",
            "price_increment": str(tick),
            "size_increment": str(step),
            "min_quantity": None,
            "min_quantity_source": "unpublished",
            "min_notional": None,
            "min_notional_currency": None,
            "min_notional_source": "unpublished",
            "max_quantity": "4500",
            "quote_currency": "USD",
        },
        "entry": {
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "IOC",
            "reduce_only": False,
            "quantity": str(quantity),
            "limit_price": str(entry_limit),
            "max_slippage_bps": "20",
            "notional_usd": "14",
            "max_quote_age_secs": "2",
        },
        "close": {
            "side": "sell",
            "order_type": "limit",
            "time_in_force": "IOC",
            "reduce_only": True,
            "limit_price": str(close_limit),
            "max_close_attempts": 2,
            "max_quote_age_secs": "2",
        },
        "envelope": {
            "max_notional_per_order_usd": "20",
            "max_gross_exposure_usd": "20",
            "max_orders": 3,
            "max_new_risk_requests": 1,
            "max_app_requests": 6,
            "min_available_margin_usdc": "25",
            "deadline_secs": 120,
            "require_flat_start": True,
        },
        "account": {
            "environment": "production",
            "journal_path": (
                f"reports/ondo-acceptance/20260920-mainnet-trade-plan/journal-{run_id}.json"
            ),
            "expected_venue_account_id_present": True,
        },
        "dms": {
            "mode": "trading",
            "activation": "subscribe cancelAllOrdersAfterPerps after flat-account verification",
            "timeout_secs": 30,
            "renewal_interval_secs": 15,
            "renewal_message_verified": False,
            "release_on_stop": "only after authoritative flat proof and matching host ACK",
            "account_wide_effect": (
                "a lapsed timer cancels every resting order on the dedicated account; it does "
                "not close a position"
            ),
        },
        "cleanup": {
            "cancel_own_orders_only": True,
            "confirm_cancels": True,
            "confirm_flat_position": True,
            "stop_budget_secs": 5,
        },
        "authorization": {
            "user_phrase_上主网_present": True,
            "approved_plan_sha256": None,
        },
        "plan_sha256": None,
        "public_evidence": {
            "run_id": run_id,
            "fetched_at_utc": json.loads(
                (CAPTURE / "meta.json").read_text(encoding="utf-8")
            )["fetched_at_utc"],
            "bid": str(bid),
            "ask": str(ask),
            "contract_disabled": False,
            "underlying_market_closed": contract.get("isClosed"),
        },
    }
    # public_evidence is intentionally hash-bound too. The parser ignores unknown top-level keys,
    # while the hash prevents this evidence from being silently replaced.
    digest = plan_sha256(document)
    document["plan_sha256"] = digest
    document["authorization"]["approved_plan_sha256"] = digest
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "plan.json").write_text(
        json.dumps(document, indent=2, sort_keys=True) + "\n", encoding="utf-8",
    )
    print(json.dumps({
        "plan": str(OUT / "plan.json"),
        "plan_sha256": digest,
        "entry_side": "buy",
        "quantity": str(quantity),
        "entry_limit": str(entry_limit),
        "entry_notional_ceiling_usd": "14",
        "close_limit": str(close_limit),
        "underlying_market_closed": contract.get("isClosed"),
    }, sort_keys=True))


if __name__ == "__main__":
    main()

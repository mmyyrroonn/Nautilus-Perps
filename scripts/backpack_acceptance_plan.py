#!/usr/bin/env python3
"""Generate finite Backpack command files from public GET data; never execute orders."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR, localcontext
import json
from pathlib import Path
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ORIGIN = "https://api.backpack.exchange"
HARD_CAP = Decimal("5")
ROOT = Path(__file__).resolve().parents[1]


class PlanError(ValueError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise PlanError("public endpoint redirect refused")


def public_get(path, **parameters):
    request = Request(
        ORIGIN + path + "?" + urlencode(parameters),
        headers={"Accept": "application/json"},
        method="GET",
    )
    try:
        with build_opener(NoRedirect()).open(request, timeout=10) as response:
            payload = response.read(1_048_577)
    except HTTPError as error:
        raise PlanError(f"public GET {path}: HTTP {error.code}") from None
    except (TimeoutError, URLError) as error:
        reason = error.reason if isinstance(error, URLError) else error
        category = "timeout" if isinstance(reason, TimeoutError) else "network transport failed"
        code = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
        suffix = f"; OS error {code}" if isinstance(code, int) else ""
        raise PlanError(f"public GET {path}: {category}{suffix}") from None
    if len(payload) > 1_048_576:
        raise PlanError("public response exceeds 1 MiB")
    try:
        return json.loads(payload, parse_float=Decimal)
    except (UnicodeError, ValueError):
        raise PlanError(f"public GET {path}: invalid JSON response") from None


def number(value, *, zero=False):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+)?", value):
        raise PlanError("market numbers must be exact decimal strings")
    result = Decimal(value)
    if result < 0 or (not zero and result == 0):
        raise PlanError("market value is outside its supported range")
    return result


def grid(value, increment, *, upward=False):
    rounding = ROUND_CEILING if upward else ROUND_FLOOR
    return (value / increment).to_integral_value(rounding=rounding) * increment


def text(value):
    return format(value, "f")


def command(symbol, side, quantity, price, *, passive=False, close=False, cancel=False):
    return {
        "action": "submit", "symbol": symbol, "side": side,
        "quantity": text(quantity), "price": text(price),
        "time_in_force": "GTC" if passive else "IOC",
        "post_only": passive, "reduce_only": close, "cancel_after_accept": cancel,
    }


def generate(symbol, market, depth, cap, price_buffer_bps, close_quantity=None, maker_wait_secs=120,
             maker_reprice_seconds=0, maker_max_reprices=0):
    if type(maker_wait_secs) is not int or not 5 <= maker_wait_secs <= 120:
        raise PlanError("maker wait must be between 5 and 120 seconds")
    if (type(maker_reprice_seconds) is not int or type(maker_max_reprices) is not int
            or not ((maker_reprice_seconds == 0 and maker_max_reprices == 0)
                    or (5 <= maker_reprice_seconds <= maker_wait_secs and 1 <= maker_max_reprices <= 23))):
        raise PlanError("maker repricing requires a 5-second minimum interval within the leg deadline and 1 to 23 replacements")
    if (
        market.get("symbol") != symbol or market.get("marketType") != "PERP"
        or market.get("quoteSymbol") != "USDC" or market.get("orderBookState") != "Open"
    ):
        raise PlanError("symbol is not an open USDC perpetual market")
    now_us = time.time_ns() // 1000
    timestamp = depth.get("timestamp")
    if type(timestamp) is not int or not -5_000_000 <= now_us - timestamp <= 10_000_000:
        raise PlanError("depth timestamp is missing, future, or older than 10 seconds")
    bids = [(number(p), number(q)) for p, q in depth["bids"]]
    asks = [(number(p), number(q)) for p, q in depth["asks"]]
    if not bids or not asks:
        raise PlanError("a two-sided positive depth quote is required")
    bid, ask = max(p for p, _ in bids), min(p for p, _ in asks)
    if bid >= ask:
        raise PlanError("crossed public quote")
    price_filter, quantity_filter = market["filters"]["price"], market["filters"]["quantity"]
    tick, step = number(price_filter["tickSize"]), number(quantity_filter["stepSize"])
    minimum = number(quantity_filter["minQuantity"])
    maximum = number(quantity_filter["maxQuantity"]) if quantity_filter.get("maxQuantity") else None
    low = number(price_filter["minPrice"], zero=True)
    high = number(price_filter["maxPrice"]) if price_filter.get("maxPrice") else None
    passive = grid(bid * Decimal("0.9975"), tick)
    replacement = passive - tick
    entry = grid(ask * Decimal("1.0005"), tick, upward=True)
    exit_price = grid(bid * Decimal("0.9995"), tick)
    # Size both legs against a higher future exit valuation, not the lower sell limit
    valuation = max(entry, grid(ask * (1 + price_buffer_bps / 10_000), tick, upward=True))
    quantity = close_quantity if close_quantity is not None else grid(cap / valuation, step)
    if maximum is not None and close_quantity is None:
        quantity = min(quantity, grid(maximum, step))
    if quantity < minimum or quantity <= 0 or quantity != grid(quantity, step):
        raise PlanError("venue minimum quantity cannot fit the USD 5 envelope")
    if quantity * valuation > cap or (maximum is not None and quantity > maximum):
        raise PlanError("confirmed close quantity exceeds the buffered leg or venue limit")
    for price in (passive, replacement, entry, exit_price):
        if price <= 0 or price < low or (high is not None and price > high) or quantity * price > cap:
            raise PlanError("rounded command price or notional exceeds venue or plan limits")
    if (close_quantity is None and quantity > sum(q for p, q in asks if p <= entry)) or quantity > sum(q for p, q in bids if p >= exit_price):
        raise PlanError("observed depth does not cover the finite IOC quantity")
    plans = {
        "cancel-replace": [
            command(symbol, "buy", quantity, passive, passive=True),
            {"action": "query", "client_order_id": "last"},
            {"action": "cancel-replace", "client_order_id": "last", "replacement":
                command(symbol, "buy", quantity, replacement, passive=True, cancel=True)},
        ],
        "roundtrip-entry": [
            command(symbol, "buy", quantity, entry),
        ],
        "maker": [{
            "action": "roundtrip", "symbol": symbol, "style": "maker",
            "quantity": text(quantity), "reference_price": text(ask), "wait_seconds": maker_wait_secs,
        }],
        "market": [{
            "action": "roundtrip", "symbol": symbol, "style": "market",
            "quantity": text(quantity), "reference_price": text(ask), "wait_seconds": 10,
        }],
    }
    if maker_reprice_seconds:
        plans["maker"][0].update(maker_reprice_seconds=maker_reprice_seconds,
                                  maker_max_reprices=maker_max_reprices)
    exit_command = {
        "action": "submit", "symbol": symbol, "side": "sell", "quantity": text(quantity),
        "order_type": "market", "reference_price": text(ask), "time_in_force": "IOC",
        "post_only": False, "reduce_only": True, "cancel_after_accept": False,
    }
    if close_quantity is not None:
        plans = {"roundtrip-exit": [exit_command]}
    evidence = {
        "schema_version": 1, "mode": "public-data-command-generation",
        "credentials_resolved": False, "financial_execution": False,
        "symbol": symbol, "generated_at_utc": datetime.now(UTC).isoformat(),
        "depth_timestamp_us": timestamp, "regenerate_before_execution": True,
        "bid": text(bid), "ask": text(ask), "tick_size": text(tick), "step_size": text(step),
        "quantity": text(quantity), "max_leg_usdc": text(cap),
        "exit_valuation_upper_price": text(valuation), "exit_price_buffer_bps": text(price_buffer_bps),
        "buffered_notional_usdc": text(quantity * valuation),
        "close_quantity_source": "caller_confirmed_native_fill" if close_quantity is not None else None,
        "maker_reprice_seconds": maker_reprice_seconds,
        "maker_max_reprices": maker_max_reprices,
        "exit_style": "market",
        "maker_leg_deadline_seconds": maker_wait_secs,
        "exit_template": None if close_quantity is not None else exit_command,
    }
    return plans, evidence


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", required=True, help="one native USDC perpetual per invocation")
    parser.add_argument("--scenario", choices=("both", "cancel-replace", "roundtrip", "maker", "market", "maker-market"), default="both")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "reports" / "backpack" / "acceptance")
    parser.add_argument("--max-leg-usdc", default="5", help="may shrink the immutable USD 5 cap")
    parser.add_argument("--exit-price-buffer-bps", default="100", help="future exit sizing buffer, 1 to 200 bps")
    parser.add_argument("--close-quantity", help="confirmed actual owned quantity; generate only one reduce-only exit")
    parser.add_argument("--maker-wait-secs", type=int, default=120, help="total maker side deadline across all attempts, 5 to 120 seconds")
    parser.add_argument("--maker-reprice-seconds", type=int, default=5)
    parser.add_argument("--maker-max-reprices", type=int, default=23)
    args = parser.parse_args(argv)
    try:
        if not re.fullmatch(r"[A-Z0-9]+_USDC_PERP", args.symbol):
            raise PlanError("one explicit USDC perpetual symbol is required")
        cap, buffer = number(args.max_leg_usdc), number(args.exit_price_buffer_bps)
        if cap > HARD_CAP or not 1 <= buffer <= 200:
            raise PlanError("cap must be <= USD 5 and exit buffer between 1 and 200 bps")
        if not 5 <= args.maker_wait_secs <= 120:
            raise PlanError("maker wait must be between 5 and 120 seconds")
        market = public_get("/api/v1/market", symbol=args.symbol)
        depth = public_get("/api/v1/depth", symbol=args.symbol, limit=5)
        with localcontext() as context:
            context.prec = 80
            plans, evidence = generate(args.symbol, market, depth, cap, buffer,
                                       None if args.close_quantity is None else number(args.close_quantity),
                                       args.maker_wait_secs, args.maker_reprice_seconds, args.maker_max_reprices)
        if args.close_quantity is not None:
            selected = plans
        elif args.scenario == "both":
            selected = {key: plans[key] for key in ("cancel-replace", "roundtrip-entry")}
        elif args.scenario == "maker-market":
            selected = {"maker-market": plans["maker"] + plans["market"]}
        else:
            scenario = "roundtrip-entry" if args.scenario == "roundtrip" else args.scenario
            selected = {scenario: plans[scenario]}
        args.output_dir.mkdir(parents=True, exist_ok=True)
        paths = {}
        for scenario, commands in selected.items():
            path = args.output_dir / f"{args.symbol}.{scenario}.commands.json"
            path.write_text(json.dumps(commands, indent=2) + "\n", encoding="utf-8")
            paths[scenario] = str(path.resolve())
        evidence["command_files"] = paths
        evidence["scenario"] = args.scenario
        evidence["market_notional_is_submission_estimate"] = (
            args.close_quantity is not None or "roundtrip-entry" in selected
            or any(command["action"] == "roundtrip" for commands in selected.values() for command in commands)
        )
        evidence["roundtrip_styles"] = [
            {"entry": command["style"], "exit": "market"}
            for commands in selected.values() for command in commands if command["action"] == "roundtrip"
        ]
        template = evidence.pop("exit_template")
        if template is not None and "roundtrip-entry" in selected:
            template_path = args.output_dir / f"{args.symbol}.roundtrip-exit.template.json"
            template_path.write_text(json.dumps([template], indent=2) + "\n", encoding="utf-8")
            evidence["exit_template_file"] = str(template_path.resolve())
        (args.output_dir / f"{args.symbol}.public-plan.json").write_text(
            json.dumps(evidence, indent=2) + "\n", encoding="utf-8",
        )
        print(json.dumps(evidence, separators=(",", ":")))
        return 0
    except PlanError as error:
        failure = {"generated": False, "failure": str(error), "financial_execution": False}
        args.output_dir.mkdir(parents=True, exist_ok=True)
        (args.output_dir / "public-plan-failure.json").write_text(
            json.dumps(failure, indent=2) + "\n", encoding="utf-8")
        print(json.dumps(failure), file=sys.stderr)
        return 2
    except (KeyError, TypeError, ValueError, OSError):
        print(json.dumps({"generated": False, "failure": "public plan generation failed"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

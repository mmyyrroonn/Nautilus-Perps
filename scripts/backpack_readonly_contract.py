"""Collect bounded, sanitized Backpack protocol observations using signed GET only."""
from __future__ import annotations

import argparse
import base64
from datetime import UTC, datetime
from decimal import Decimal
import hashlib
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

ORIGIN = "https://api.backpack.exchange"
OPERATIONS = {
    "account": ("/api/v1/account", "accountQuery"),
    "balances": ("/api/v1/capital", "balanceQuery"),
    "collateral": ("/api/v1/capital/collateral", "collateralQuery"),
    "positions": ("/api/v1/position", "positionQuery"),
    "orders": ("/api/v1/orders", "orderQueryAll"),
    "order-history": ("/wapi/v1/history/orders", "orderHistoryQueryAll"),
    "fill-history": ("/wapi/v1/history/fills", "fillHistoryQueryAll"),
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise RuntimeError("redirect refused")


def project(name, value):
    if name in {"order-history", "fill-history"}:
        if not isinstance(value, list):
            raise ValueError("history response must be an array")
        return {"count": len(value), "complete_history": False, "maximum_requested": 20}
    if name == "account":
        names = ("autoBorrowSettlements", "autoLend", "autoRealizePnl", "autoRepayBorrows",
                 "borrowLimit", "futuresMakerFee", "futuresTakerFee", "leverageLimit",
                 "limitOrders", "liquidating", "positionLimit", "triggerOrders")
        return {key: value.get(key) for key in names}
    if name == "balances":
        return {key: {field: row.get(field) for field in ("available", "locked", "staked")}
                for key, row in value.items()}
    if name == "collateral":
        names = ("assetsValue", "borrowLiability", "imf", "liabilitiesValue", "marginFraction",
                 "mmf", "netEquity", "netEquityAvailable", "netEquityLocked",
                 "netExposureFutures", "pnlUnrealized", "unsettledEquity")
        result = {key: value.get(key) for key in names}
        result["assets"] = [{key: row.get(key) for key in
            ("symbol", "collateralWeight", "collateralValue", "availableQuantity", "lendQuantity")}
            for row in value.get("collateral", [])]
        return result
    if name == "positions":
        return {"count": len(value), "positions": [{key: row.get(key) for key in
            ("symbol", "netQuantity", "netExposureNotional", "imf", "mmf")}
            for row in value], "numeric_identity_present": bool(value) and all(
                "userId" in row and "subaccountId" in row for row in value)}
    if name == "orders":
        return {"count": len(value), "orders": [{key: row.get(key) for key in
            ("symbol", "side", "status", "quantity", "executedQuantity", "reduceOnly")}
            for row in value]}
    if name == "market":
        return {key: value.get(key) for key in
            ("symbol", "baseSymbol", "quoteSymbol", "marketType", "orderBookState",
             "filters", "imfFunction", "mmfFunction", "fundingInterval")}
    raise ValueError("unsupported observation")


def collect(env_file, operations=None):
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from dotenv import dotenv_values

    values = dotenv_values(env_file, interpolate=False) if env_file else {}
    seed = os.environ.get("BACKPACK_MAINNET_API_SECRET") or values.get("BACKPACK_MAINNET_API_SECRET")
    public = os.environ.get("BACKPACK_MAINNET_API_KEY") or values.get("BACKPACK_MAINNET_API_KEY")
    report = {"schema_version": 1, "observed_at": datetime.now(UTC).isoformat(),
              "evidence_level": "private-read-only", "mutations": 0, "results": {}}
    try:
        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed, validate=True))
        derived = base64.b64encode(key.public_key().public_bytes_raw()).decode()
        if derived != public:
            raise ValueError("key mismatch")
    except Exception:
        report["credential_valid"] = False
        return report
    report["credential_valid"] = True
    report["credential_scope_sha256"] = hashlib.sha256(base64.b64decode(public)).hexdigest()
    opener = build_opener(NoRedirect())
    selected = set(operations) if operations else {"account", "balances", "collateral", "positions", "orders", "market"}
    requests = [(name, path, instruction,
                 {"limit": 20, "sortDirection": "Desc", "symbol": "BTC_USDC_PERP"}
                 if name in {"order-history", "fill-history"} else {})
                for name, (path, instruction) in OPERATIONS.items()
                if name in selected]
    if "market" in selected:
        requests.append(("market", "/api/v1/market", None, {"symbol": "BTC_USDC_PERP"}))
    for name, path, instruction, parameters in requests:
        headers = {"Accept": "application/json"}
        if instruction:
            stamp = str(time.time_ns() // 1_000_000)
            canonical = "&".join(["instruction=" + instruction,
                *[f"{k}={v}" for k, v in sorted(parameters.items())],
                "timestamp=" + stamp, "window=5000"])
            headers.update({"X-API-Key": public, "X-Signature": base64.b64encode(
                key.sign(canonical.encode())).decode(), "X-Timestamp": stamp, "X-Window": "5000"})
        url = ORIGIN + path + ("?" + urlencode(parameters) if parameters else "")
        started = time.monotonic_ns()
        row = {"method": "GET", "path": path}
        try:
            with opener.open(Request(url, method="GET", headers=headers), timeout=10) as response:
                raw = response.read(2_000_001)
                if len(raw) > 2_000_000 or response.geturl() != url:
                    raise ValueError("response bounds")
                payload = json.loads(raw, parse_float=Decimal)
                row.update(status=response.status, observation=project(name, payload))
        except HTTPError as e:
            row.update(status=e.code, error="HTTP request refused; private body omitted")
        except URLError as error:
            reason = error.reason
            row["error"] = "transport failed; private error omitted"
            row["error_category"] = "network_transport"
            code = getattr(reason, "winerror", None) or getattr(reason, "errno", None)
            if isinstance(code, int):
                row["os_error_code"] = code
        except (ValueError, TypeError, KeyError):
            row["error"] = "response validation failed; private payload omitted"
            row["error_category"] = "response_validation"
        except Exception:
            row["error"] = "read or validation failed; private error omitted"
        row["elapsed_ms"] = (time.monotonic_ns() - started) // 1_000_000
        report["results"][name] = row
        time.sleep(0.05)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--operation", action="append", choices=(*OPERATIONS, "market"),
                        help="collect only selected GET observations; repeat as needed")
    args = parser.parse_args()
    report = collect(args.env_file, args.operation)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, default=str) + "\n", encoding="utf-8")
    print(json.dumps({"credential_valid": report["credential_valid"], "mutations": 0,
        "results": {name: {key: value for key, value in row.items()
            if key in ("status", "error", "error_category", "os_error_code", "elapsed_ms")}
            for name, row in report["results"].items()}}))
    return 0 if report["credential_valid"] and all(
        row.get("status") == 200 for row in report["results"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Read-only exchange account snapshot reader for cold-start reconciliation ground truth.

Importing or instantiating CarryStartupAccountReader performs no network I/O and
reads no credentials. All error messages are sanitized to omit credentials,
auth headers, URLs, and raw private payloads.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import hmac
import json
import os
import time
from typing import Any, Callable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from .bitget_gateway import (
    BITGET_MARGIN_COIN,
    BITGET_MARGIN_MODE,
    BITGET_PRODUCT_TYPE,
    BITGET_REST_BASE,
    bitget_instrument_id,
    bitget_rest_headers,
    bitget_signed_get_path,
    is_bitget_uta_error,
)

_MAX_PAGES = 20
_USER_AGENT = "Mozilla/5.0 (compatible; NautilusPerpsCarry/1.0)"
_OKX_ALGO_ORD_TYPES = ("conditional", "oco", "trigger", "move_order_stop", "twap", "chase")
_BITGET_PLAN_TYPES = ("normal_plan", "profit_loss", "track_plan")
_BITGET_PENDING_STATUSES = ("live", "partially_filled")


class StartupSnapshotError(RuntimeError):
    """Raised when startup exchange snapshot retrieval or validation fails."""


@dataclass(frozen=True)
class VenueAccountSnapshot:
    positions: dict[tuple[str, str], Decimal]
    open_orders: dict[tuple[str, str], int]


def _exact_decimal(raw: Any, name: str) -> Decimal:
    if isinstance(raw, bool) or raw is None or not isinstance(raw, (str, int, float, Decimal)):
        raise StartupSnapshotError(f"invalid {name}")
    try:
        val = raw if isinstance(raw, Decimal) else Decimal(str(raw))
    except Exception:
        raise StartupSnapshotError(f"invalid {name}") from None
    if not val.is_finite():
        raise StartupSnapshotError(f"non-finite {name}")
    return val


def default_signed_http_json(
    method: str,
    url: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: dict[str, Any] | None = None,
    timeout: float = 10.0,
) -> Any:
    """Execute a read-only GET/POST JSON request with sanitized errors."""
    if method not in ("GET", "POST"):
        raise StartupSnapshotError(f"unsupported HTTP method {method}")
    req_headers: dict[str, str] = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
    }
    if headers:
        req_headers.update(headers)
    data: bytes | None = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        try:
            err_raw = exc.read(4096)
            err_obj = json.loads(err_raw) if err_raw else None
        except Exception:
            err_obj = None
        if isinstance(err_obj, dict) and is_bitget_uta_error(err_obj.get("code"), err_obj.get("msg")):
            code_s = str(err_obj.get("code") or "uta")
            raise StartupSnapshotError(
                f"BITGET UTA account not supported (code={code_s}); Classic v2 account required"
            ) from None
        raise StartupSnapshotError(f"HTTP {exc.code} during startup account read") from None
    except Exception as exc:
        raise StartupSnapshotError(f"startup account read failed: {type(exc).__name__}") from None
    try:
        return json.loads(raw, parse_float=Decimal)
    except Exception:
        raise StartupSnapshotError("startup account read returned invalid JSON") from None


class CarryStartupAccountReader:
    """Direct REST snapshot reader across HL, LIGHTER, OKX, and BYBIT for startup reconcile."""

    def __init__(
        self,
        *,
        environment: str = "mainnet",
        environ: Mapping[str, str] | None = None,
        http_request: Callable[..., Any] | None = None,
        timeout_secs: float = 10.0,
    ) -> None:
        if environment not in ("mainnet", "testnet"):
            raise ValueError(f"unsupported environment: {environment!r}")
        self.environment = environment
        self._environ = environ
        self._http = http_request or default_signed_http_json
        self.timeout_secs = float(timeout_secs)

    def _env_map(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _require_env(self, keys: tuple[str, ...], venue: str) -> dict[str, str]:
        env = self._env_map()
        out: dict[str, str] = {}
        missing: list[str] = []
        for k in keys:
            val = env.get(k)
            if not isinstance(val, str) or not val.strip():
                missing.append(k)
            else:
                out[k] = val.strip()
        if missing:
            raise StartupSnapshotError(f"{venue} credentials missing from environment: {', '.join(missing)}")
        return out

    def _request_json(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> Any:
        try:
            return self._http(method, url, headers=headers, body=body, timeout=self.timeout_secs)
        except StartupSnapshotError:
            raise
        except Exception as exc:
            raise StartupSnapshotError(f"startup account read failed: {type(exc).__name__}") from None

    def fetch_startup_snapshot(
        self,
        venues: Sequence[str],
        *,
        okx_multipliers: Mapping[str, Decimal] | None = None,
    ) -> VenueAccountSnapshot:
        positions: dict[tuple[str, str], Decimal] = {}
        open_orders: dict[tuple[str, str], int] = {}
        for venue in venues:
            if venue == "HL":
                self._read_hl(positions, open_orders)
            elif venue == "LIGHTER":
                self._read_lighter(positions, open_orders)
            elif venue == "OKX":
                self._read_okx(positions, open_orders, okx_multipliers=okx_multipliers)
            elif venue == "BYBIT":
                self._read_bybit(positions, open_orders)
            elif venue == "BITGET":
                self._read_bitget(positions, open_orders)
            else:
                raise StartupSnapshotError(f"unsupported venue {venue}")
        return VenueAccountSnapshot(positions=positions, open_orders=open_orders)

    def _read_hl(
        self,
        positions: dict[tuple[str, str], Decimal],
        open_orders: dict[tuple[str, str], int],
    ) -> None:
        is_main = self.environment == "mainnet"
        pfx = "HYPERLIQUID_" if is_main else "HYPERLIQUID_TESTNET_"
        creds = self._require_env((pfx + "ACCOUNT_ADDRESS",), "HL")
        vault = (self._env_map().get(pfx + "VAULT") or "").strip()
        addr = vault or creds[pfx + "ACCOUNT_ADDRESS"]
        if len(addr) != 42 or not addr.startswith("0x"):
            raise StartupSnapshotError("HL invalid account address")
        try:
            int(addr[2:], 16)
        except ValueError:
            raise StartupSnapshotError("HL invalid account address") from None

        url = "https://api.hyperliquid.xyz/info" if is_main else "https://api.hyperliquid-testnet.xyz/info"
        dexes: tuple[str | None, ...] = (None, "xyz") if is_main else (None,)

        def _read_hl_positions() -> dict[tuple[str, str], Decimal]:
            out: dict[tuple[str, str], Decimal] = {}
            seen_pos: set[str] = set()
            for dex in dexes:
                common: dict[str, Any] = {"user": addr}
                if dex:
                    common["dex"] = dex
                state = self._request_json("POST", url, body={"type": "clearinghouseState", **common})
                if not isinstance(state, dict) or not isinstance(state.get("assetPositions"), list):
                    raise StartupSnapshotError("HL clearinghouseState missing assetPositions")
                for row in state["assetPositions"]:
                    if not isinstance(row, dict) or not isinstance(row.get("position"), dict):
                        raise StartupSnapshotError("HL invalid assetPositions row")
                    item = row["position"]
                    coin = item.get("coin")
                    if not isinstance(coin, str) or not coin:
                        raise StartupSnapshotError("HL invalid position coin")
                    szi = _exact_decimal(item.get("szi"), "HL position szi")
                    full_coin = coin if (not dex or ":" in coin) else f"{dex}:{coin}"
                    iid = f"{full_coin}-USD-PERP.HYPERLIQUID"
                    if iid in seen_pos:
                        raise StartupSnapshotError(f"HL duplicate position row for {iid}")
                    seen_pos.add(iid)
                    if szi != 0:
                        out[("HL", iid)] = szi
            return out

        pos_before = _read_hl_positions()
        seen_oids: set[tuple[str, str]] = set()
        for dex in dexes:
            common = {"user": addr}
            if dex:
                common["dex"] = dex
            orders = self._request_json("POST", url, body={"type": "frontendOpenOrders", **common})
            if not isinstance(orders, list):
                raise StartupSnapshotError("HL frontendOpenOrders invalid payload")
            for row in orders:
                if not isinstance(row, dict):
                    raise StartupSnapshotError("HL invalid frontendOpenOrders row")
                coin = row.get("coin")
                if not isinstance(coin, str) or not coin or "oid" not in row:
                    raise StartupSnapshotError("HL frontendOpenOrders missing coin or oid")
                oid_key = (dex or "", str(row["oid"]))
                if oid_key in seen_oids:
                    continue
                seen_oids.add(oid_key)
                full_coin = coin if (not dex or ":" in coin) else f"{dex}:{coin}"
                iid = f"{full_coin}-USD-PERP.HYPERLIQUID"
                key = ("HL", iid)
                open_orders[key] = open_orders.get(key, 0) + 1

        pos_after = _read_hl_positions()
        if pos_before != pos_after:
            raise StartupSnapshotError("HL positions changed during startup snapshot read")
        positions.update(pos_after)

    def _read_lighter(
        self,
        positions: dict[tuple[str, str], Decimal],
        open_orders: dict[tuple[str, str], int],
    ) -> None:
        is_main = self.environment == "mainnet"
        pfx = "LIGHTER_" if is_main else "LIGHTER_TESTNET_"
        creds = self._require_env((pfx + "ACCOUNT_INDEX",), "LIGHTER")
        idx_str = creds[pfx + "ACCOUNT_INDEX"]
        if not idx_str.isdigit():
            raise StartupSnapshotError("LIGHTER invalid account index")
        expected_idx = str(int(idx_str))

        base = "https://mainnet.zklighter.elliot.ai" if is_main else "https://testnet.zklighter.elliot.ai"
        qs = urllib.parse.urlencode({"by": "index", "value": expected_idx, "active_only": "false"})
        payload = self._request_json("GET", f"{base}/api/v1/account?{qs}")
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise StartupSnapshotError("LIGHTER account query failed")
        accounts = payload.get("accounts")
        if not isinstance(accounts, list) or len(accounts) != 1 or not isinstance(accounts[0], dict):
            raise StartupSnapshotError("LIGHTER invalid accounts payload")
        account = accounts[0]
        acct_id = account.get("account_index", account.get("index"))
        if acct_id is None or str(acct_id) != expected_idx:
            raise StartupSnapshotError("LIGHTER account index mismatch")
        if "index" in account and str(account["index"]) != expected_idx:
            raise StartupSnapshotError("LIGHTER account index mismatch")

        pos_rows = account.get("positions")
        if not isinstance(pos_rows, list):
            raise StartupSnapshotError("LIGHTER missing positions list")
        seen_syms: set[str] = set()
        row_order_total = 0
        for row in pos_rows:
            if not isinstance(row, dict):
                raise StartupSnapshotError("LIGHTER invalid position row")
            sym = row.get("symbol")
            if not isinstance(sym, str) or not sym:
                raise StartupSnapshotError("LIGHTER missing position symbol")
            if sym in seen_syms:
                raise StartupSnapshotError(f"LIGHTER duplicate position row {sym}")
            seen_syms.add(sym)

            qty = _exact_decimal(row.get("position"), "Lighter position")
            sign = row.get("sign")
            if qty < 0 or type(sign) is not int or sign not in (-1, 0, 1) or (qty > 0 and sign == 0):
                raise StartupSnapshotError("LIGHTER invalid position quantity or sign")

            row_cnt = 0
            for field in ("open_order_count", "pending_order_count", "position_tied_order_count"):
                cnt = row.get(field)
                if type(cnt) is not int or cnt < 0:
                    raise StartupSnapshotError(f"LIGHTER invalid {field}")
                row_cnt += cnt
            row_order_total += row_cnt

            iid = f"{sym}-PERP.LIGHTER"
            if qty > 0:
                positions[("LIGHTER", iid)] = -qty if sign < 0 else qty
            if row_cnt > 0:
                key = ("LIGHTER", iid)
                open_orders[key] = open_orders.get(key, 0) + row_cnt

        if type(account.get("total_order_count")) is not int or account["total_order_count"] < 0:
            raise StartupSnapshotError("LIGHTER missing or invalid total_order_count")
        global_cnt = 0
        for gkey in ("total_order_count", "total_isolated_order_count", "pending_order_count"):
            if gkey in account:
                gval = account[gkey]
                if type(gval) is not int or gval < 0:
                    raise StartupSnapshotError(f"LIGHTER invalid {gkey}")
                global_cnt += gval
        if global_cnt > row_order_total:
            key = ("LIGHTER", "account.LIGHTER")
            open_orders[key] = open_orders.get(key, 0) + (global_cnt - row_order_total)

    def _okx_headers(
        self,
        *,
        api_key: str,
        api_secret: str,
        api_passphrase: str,
        method: str,
        path_and_query: str,
    ) -> dict[str, str]:
        now = datetime.now(timezone.utc)
        ts = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"
        msg = f"{ts}{method}{path_and_query}".encode("utf-8")
        sig = base64.b64encode(hmac.new(api_secret.encode("utf-8"), msg, hashlib.sha256).digest()).decode("ascii")
        headers = {
            "OK-ACCESS-KEY": api_key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": api_passphrase,
        }
        if self.environment != "mainnet":
            headers["x-simulated-trading"] = "1"
        return headers

    def _read_okx(
        self,
        positions: dict[tuple[str, str], Decimal],
        open_orders: dict[tuple[str, str], int],
        *,
        okx_multipliers: Mapping[str, Decimal] | None = None,
    ) -> None:
        is_main = self.environment == "mainnet"
        pfx = "OKX_" if is_main else "OKX_DEMO_"
        creds = self._require_env((pfx + "API_KEY", pfx + "API_SECRET", pfx + "API_PASSPHRASE"), "OKX")
        base = "https://www.okx.com"

        def _signed_get(path_and_query: str) -> Any:
            headers = self._okx_headers(
                api_key=creds[pfx + "API_KEY"],
                api_secret=creds[pfx + "API_SECRET"],
                api_passphrase=creds[pfx + "API_PASSPHRASE"],
                method="GET",
                path_and_query=path_and_query,
            )
            resp = self._request_json("GET", f"{base}{path_and_query}", headers=headers)
            if not isinstance(resp, dict) or resp.get("code") != "0" or not isinstance(resp.get("data"), list):
                code = resp.get("code") if isinstance(resp, dict) else "invalid"
                raise StartupSnapshotError(f"OKX API error code={code}")
            return resp["data"]

        mult_map: dict[str, Decimal] = {}
        if okx_multipliers:
            for k, v in okx_multipliers.items():
                if isinstance(v, Decimal) and v > 0:
                    mult_map[k] = v

        def _read_okx_positions() -> dict[tuple[str, str], Decimal]:
            out: dict[tuple[str, str], Decimal] = {}
            pos_rows = _signed_get("/api/v5/account/positions?instType=SWAP")
            nonzero_rows: list[tuple[str, Decimal, str]] = []
            for row in pos_rows:
                if not isinstance(row, dict):
                    raise StartupSnapshotError("OKX invalid position row")
                inst_id = row.get("instId")
                if not isinstance(inst_id, str) or not inst_id:
                    raise StartupSnapshotError("OKX position missing instId")
                pos_raw = row.get("pos")
                if pos_raw in (None, ""):
                    continue
                pos_dec = _exact_decimal(pos_raw, "OKX position pos")
                if pos_dec == 0:
                    continue
                pos_side = str(row.get("posSide") or "")
                nonzero_rows.append((inst_id, pos_dec, pos_side))

            missing_ctval = [
                inst_id
                for inst_id, _, _ in nonzero_rows
                if f"{inst_id}.OKX" not in mult_map and inst_id not in mult_map
            ]
            if missing_ctval:
                inst_resp = self._request_json("GET", f"{base}/api/v5/public/instruments?instType=SWAP")
                if not isinstance(inst_resp, dict) or inst_resp.get("code") != "0" or not isinstance(inst_resp.get("data"), list):
                    raise StartupSnapshotError("OKX public instruments fetch failed")
                for item in inst_resp["data"]:
                    if isinstance(item, dict) and isinstance(item.get("instId"), str) and item.get("ctVal") not in (None, ""):
                        ct = _exact_decimal(item["ctVal"], "OKX ctVal")
                        if ct > 0:
                            mult_map[item["instId"]] = ct

            for inst_id, pos_dec, pos_side in nonzero_rows:
                full_iid = f"{inst_id}.OKX"
                ct_val = mult_map.get(full_iid) or mult_map.get(inst_id)
                if ct_val is None or ct_val <= 0:
                    raise StartupSnapshotError(f"OKX missing positive ctVal for {inst_id}")
                if pos_side == "long":
                    signed_contracts = abs(pos_dec)
                elif pos_side == "short":
                    signed_contracts = -abs(pos_dec)
                elif pos_side in ("net", ""):
                    signed_contracts = pos_dec
                else:
                    raise StartupSnapshotError(f"OKX unsupported posSide {pos_side!r}")
                base_qty = signed_contracts * ct_val
                key = ("OKX", full_iid)
                if key in out:
                    key = ("OKX", f"{full_iid}:{pos_side}")
                out[key] = base_qty
            return out

        pos_before = _read_okx_positions()

        seen_ord_ids: set[str] = set()
        seen_cursors: set[str] = set()
        after: str | None = None
        for _ in range(_MAX_PAGES):
            qs = "instType=SWAP&limit=100" + (f"&after={urllib.parse.quote(after)}" if after else "")
            page = _signed_get(f"/api/v5/trade/orders-pending?{qs}")
            for row in page:
                if not isinstance(row, dict):
                    raise StartupSnapshotError("OKX invalid open order row")
                inst_id = row.get("instId")
                ord_id = row.get("ordId")
                if not isinstance(inst_id, str) or not inst_id or not isinstance(ord_id, str) or not ord_id:
                    raise StartupSnapshotError("OKX open order missing instId or ordId")
                if ord_id in seen_ord_ids:
                    continue
                seen_ord_ids.add(ord_id)
                key = ("OKX", f"{inst_id}.OKX")
                open_orders[key] = open_orders.get(key, 0) + 1
            if len(page) < 100:
                break
            next_after = str(page[-1]["ordId"])
            if next_after in seen_cursors:
                raise StartupSnapshotError("OKX orders-pending pagination loop")
            seen_cursors.add(next_after)
            after = next_after
        else:
            raise StartupSnapshotError("OKX orders-pending pagination exceeded max pages")

        seen_algo_ids: set[str] = set()
        for ord_type in _OKX_ALGO_ORD_TYPES:
            algo_after: str | None = None
            seen_algo_cursors: set[str] = set()
            for _ in range(_MAX_PAGES):
                qs = f"instType=SWAP&ordType={urllib.parse.quote(ord_type)}&limit=100" + (
                    f"&after={urllib.parse.quote(algo_after)}" if algo_after else ""
                )
                page = _signed_get(f"/api/v5/trade/orders-algo-pending?{qs}")
                for row in page:
                    if not isinstance(row, dict):
                        raise StartupSnapshotError("OKX invalid algo order row")
                    inst_id = row.get("instId")
                    algo_id = row.get("algoId")
                    if not isinstance(inst_id, str) or not inst_id or not isinstance(algo_id, str) or not algo_id:
                        raise StartupSnapshotError("OKX algo order missing instId or algoId")
                    if algo_id in seen_algo_ids:
                        continue
                    seen_algo_ids.add(algo_id)
                    key = ("OKX", f"{inst_id}.OKX")
                    open_orders[key] = open_orders.get(key, 0) + 1
                if len(page) < 100:
                    break
                next_after = str(page[-1]["algoId"])
                if next_after in seen_algo_cursors:
                    raise StartupSnapshotError("OKX orders-algo-pending pagination loop")
                seen_algo_cursors.add(next_after)
                algo_after = next_after
            else:
                raise StartupSnapshotError("OKX orders-algo-pending pagination exceeded max pages")

        pos_after = _read_okx_positions()
        if pos_before != pos_after:
            raise StartupSnapshotError("OKX positions changed during startup snapshot read")
        positions.update(pos_after)

    def _bybit_headers(self, *, api_key: str, api_secret: str, query_string: str) -> dict[str, str]:
        ts_ms = str(int(time.time() * 1000))
        recv_window = "5000"
        payload = f"{ts_ms}{api_key}{recv_window}{query_string}".encode("utf-8")
        sig = hmac.new(api_secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        return {
            "X-BAPI-API-KEY": api_key,
            "X-BAPI-TIMESTAMP": ts_ms,
            "X-BAPI-SIGN": sig,
            "X-BAPI-RECV-WINDOW": recv_window,
        }

    def _read_bybit(
        self,
        positions: dict[tuple[str, str], Decimal],
        open_orders: dict[tuple[str, str], int],
    ) -> None:
        is_main = self.environment == "mainnet"
        pfx = "BYBIT_" if is_main else "BYBIT_TESTNET_"
        creds = self._require_env((pfx + "API_KEY", pfx + "API_SECRET"), "BYBIT")
        base = "https://api.bybit.com" if is_main else "https://api-testnet.bybit.com"

        def _signed_get(path: str, params: list[tuple[str, str]]) -> dict[str, Any]:
            qs = urllib.parse.urlencode(params)
            headers = self._bybit_headers(
                api_key=creds[pfx + "API_KEY"],
                api_secret=creds[pfx + "API_SECRET"],
                query_string=qs,
            )
            resp = self._request_json("GET", f"{base}{path}?{qs}", headers=headers)
            if not isinstance(resp, dict) or resp.get("retCode") != 0 or not isinstance(resp.get("result"), dict):
                code = resp.get("retCode") if isinstance(resp, dict) else "invalid"
                raise StartupSnapshotError(f"BYBIT API error retCode={code}")
            result = resp["result"]
            if not isinstance(result.get("list"), list):
                raise StartupSnapshotError("BYBIT response missing result.list")
            return result

        def _read_bybit_positions() -> dict[tuple[str, str], Decimal]:
            out: dict[tuple[str, str], Decimal] = {}
            for settle_coin in ("USDT", "USDC"):
                cursor: str | None = None
                seen_pos_cursors: set[str] = set()
                for _ in range(_MAX_PAGES):
                    params = [("category", "linear"), ("settleCoin", settle_coin), ("limit", "200")]
                    if cursor:
                        params.append(("cursor", cursor))
                    result = _signed_get("/v5/position/list", params)
                    for row in result["list"]:
                        if not isinstance(row, dict):
                            raise StartupSnapshotError("BYBIT invalid position row")
                        symbol = row.get("symbol")
                        if not isinstance(symbol, str):
                            raise StartupSnapshotError("BYBIT invalid position symbol")
                        if not symbol:
                            continue
                        size = _exact_decimal(row.get("size"), "Bybit position size")
                        if size < 0:
                            raise StartupSnapshotError("BYBIT negative position size")
                        if size == 0:
                            continue
                        side = row.get("side")
                        if side == "Buy":
                            signed_qty = size
                        elif side == "Sell":
                            signed_qty = -size
                        else:
                            raise StartupSnapshotError(f"BYBIT non-zero position has invalid side {side!r}")
                        iid = f"{symbol}-LINEAR.BYBIT"
                        key = ("BYBIT", iid)
                        if key in out:
                            key = ("BYBIT", f"{iid}:{side.lower()}")
                        out[key] = signed_qty
                    next_cursor = str(result.get("nextPageCursor") or "")
                    if not next_cursor:
                        break
                    if next_cursor in seen_pos_cursors:
                        raise StartupSnapshotError("BYBIT position pagination loop")
                    seen_pos_cursors.add(next_cursor)
                    cursor = next_cursor
                else:
                    raise StartupSnapshotError("BYBIT position pagination exceeded max pages")
            return out

        pos_before = _read_bybit_positions()

        seen_ord_ids: set[str] = set()
        for settle_coin in ("USDT", "USDC"):
            for order_filter in ("Order", "StopOrder"):
                ord_cursor: str | None = None
                seen_ord_cursors: set[str] = set()
                for _ in range(_MAX_PAGES):
                    params = [
                        ("category", "linear"),
                        ("settleCoin", settle_coin),
                        ("openOnly", "0"),
                        ("orderFilter", order_filter),
                        ("limit", "50"),
                    ]
                    if ord_cursor:
                        params.append(("cursor", ord_cursor))
                    result = _signed_get("/v5/order/realtime", params)
                    for row in result["list"]:
                        if not isinstance(row, dict):
                            raise StartupSnapshotError("BYBIT invalid open order row")
                        symbol = row.get("symbol")
                        order_id = row.get("orderId")
                        if not isinstance(symbol, str) or not symbol or not isinstance(order_id, str) or not order_id:
                            raise StartupSnapshotError("BYBIT open order missing symbol or orderId")
                        if order_id in seen_ord_ids:
                            continue
                        seen_ord_ids.add(order_id)
                        key = ("BYBIT", f"{symbol}-LINEAR.BYBIT")
                        open_orders[key] = open_orders.get(key, 0) + 1
                    next_cursor = str(result.get("nextPageCursor") or "")
                    if not next_cursor:
                        break
                    if next_cursor in seen_ord_cursors:
                        raise StartupSnapshotError("BYBIT order pagination loop")
                    seen_ord_cursors.add(next_cursor)
                    ord_cursor = next_cursor
                else:
                    raise StartupSnapshotError("BYBIT order pagination exceeded max pages")

        pos_after = _read_bybit_positions()
        if pos_before != pos_after:
            raise StartupSnapshotError("BYBIT positions changed during startup snapshot read")
        positions.update(pos_after)

    def _read_bitget(
        self,
        positions: dict[tuple[str, str], Decimal],
        open_orders: dict[tuple[str, str], int],
    ) -> None:
        is_main = self.environment == "mainnet"
        pfx = "BITGET_" if is_main else "BITGET_DEMO_"
        creds = self._require_env((pfx + "API_KEY", pfx + "API_SECRET", pfx + "API_PASSPHRASE"), "BITGET")
        base = BITGET_REST_BASE

        def _signed_get(path_and_query: str) -> Any:
            headers = bitget_rest_headers(
                api_key=creds[pfx + "API_KEY"],
                api_secret=creds[pfx + "API_SECRET"],
                api_passphrase=creds[pfx + "API_PASSPHRASE"],
                method="GET",
                request_path_and_query=path_and_query,
                environment=self.environment,
            )
            resp = self._request_json("GET", f"{base}{path_and_query}", headers=headers)
            if not isinstance(resp, dict):
                raise StartupSnapshotError("BITGET invalid response payload")
            code = str(resp.get("code") or "")
            msg = resp.get("msg")
            if is_bitget_uta_error(code, msg):
                raise StartupSnapshotError(
                    f"BITGET UTA account not supported (code={code}); Classic v2 account required"
                )
            if code != "00000":
                raise StartupSnapshotError(f"BITGET API error code={code}")
            if "data" not in resp:
                raise StartupSnapshotError("BITGET response missing data")
            return resp["data"]

        acct_data = _signed_get(
            bitget_signed_get_path(
                "/api/v2/mix/account/account",
                {
                    "symbol": "BTCUSDT",
                    "productType": BITGET_PRODUCT_TYPE,
                    "marginCoin": BITGET_MARGIN_COIN,
                },
            )
        )
        if isinstance(acct_data, list):
            if not acct_data or not isinstance(acct_data[0], dict):
                raise StartupSnapshotError("BITGET invalid account mode payload")
            acct_data = acct_data[0]
        if not isinstance(acct_data, dict):
            raise StartupSnapshotError("BITGET invalid account mode payload")
        pos_mode = str(acct_data.get("posMode") or "").strip().lower()
        if pos_mode != "one_way_mode":
            raise StartupSnapshotError(f"BITGET posMode must be one_way_mode, got {pos_mode or 'unknown'}")
        margin_mode = str(acct_data.get("marginMode") or "").strip().lower()
        if margin_mode != BITGET_MARGIN_MODE:
            raise StartupSnapshotError(f"BITGET marginMode must be crossed, got {margin_mode or 'missing'}")

        iso_rows = _signed_get(
            bitget_signed_get_path(
                "/api/v2/mix/account/isolated-symbols",
                {"productType": BITGET_PRODUCT_TYPE},
            )
        )
        if not isinstance(iso_rows, list):
            raise StartupSnapshotError("BITGET isolated-symbols response missing list")
        if iso_rows:
            raise StartupSnapshotError(
                f"BITGET account has {len(iso_rows)} isolated-margin symbols configured; all symbols must be crossed"
            )

        def _read_bitget_positions() -> dict[tuple[str, str], Decimal]:
            rows = _signed_get(
                bitget_signed_get_path(
                    "/api/v2/mix/position/all-position",
                    {
                        "productType": BITGET_PRODUCT_TYPE,
                        "marginCoin": BITGET_MARGIN_COIN,
                    },
                )
            )
            if not isinstance(rows, list):
                raise StartupSnapshotError("BITGET all-position response missing list")
            out: dict[tuple[str, str], Decimal] = {}
            for row in rows:
                if not isinstance(row, dict):
                    raise StartupSnapshotError("BITGET invalid position row")
                row_pm = row.get("posMode")
                if row_pm is not None and str(row_pm).strip().lower() != "one_way_mode":
                    raise StartupSnapshotError(f"BITGET position posMode must be one_way_mode, got {row_pm!r}")
                symbol = row.get("symbol")
                if not isinstance(symbol, str) or not symbol.strip():
                    raise StartupSnapshotError("BITGET position missing symbol")
                total = _exact_decimal(row.get("total"), "BITGET position total")
                if total < 0:
                    raise StartupSnapshotError("BITGET negative position total")
                if row.get("available") not in (None, ""):
                    avail = _exact_decimal(row.get("available"), "BITGET position available")
                    if avail < 0:
                        raise StartupSnapshotError("BITGET negative position available")
                if total == 0:
                    continue
                row_mm = row.get("marginMode")
                if row_mm is not None and str(row_mm).strip() != "":
                    row_mm_norm = str(row_mm).strip().lower()
                    if row_mm_norm != BITGET_MARGIN_MODE:
                        raise StartupSnapshotError(
                            f"BITGET position marginMode must be crossed, got {row_mm_norm!r}"
                        )
                hold_side = str(row.get("holdSide") or "").strip().lower()
                if hold_side == "long":
                    signed_qty = total
                elif hold_side == "short":
                    signed_qty = -total
                else:
                    raise StartupSnapshotError(f"BITGET unsupported holdSide {hold_side!r}")
                try:
                    iid = bitget_instrument_id(symbol)
                except ValueError:
                    raise StartupSnapshotError("BITGET invalid position symbol") from None
                key = ("BITGET", iid)
                out[key] = out.get(key, Decimal("0")) + signed_qty
                if out[key] == 0:
                    out.pop(key, None)
            return out

        def _unpack_order_page(data: Any, endpoint_name: str) -> tuple[list[Any], str]:
            if not isinstance(data, dict) or "entrustedList" not in data:
                raise StartupSnapshotError(f"BITGET {endpoint_name} invalid payload")
            rows = data["entrustedList"]
            if rows is None:
                rows = []
            elif not isinstance(rows, list):
                raise StartupSnapshotError(f"BITGET {endpoint_name} invalid entrustedList")
            end_id = str(data.get("endId") or "")
            return rows, end_id

        pos_before = _read_bitget_positions()

        seen_ord_ids: set[str] = set()
        for order_status in _BITGET_PENDING_STATUSES:
            seen_cursors: set[str] = set()
            id_less_than: str | None = None
            for _ in range(_MAX_PAGES):
                ord_params: dict[str, Any] = {
                    "productType": BITGET_PRODUCT_TYPE,
                    "status": order_status,
                    "limit": "100",
                }
                if id_less_than:
                    ord_params["idLessThan"] = id_less_than
                data = _signed_get(bitget_signed_get_path("/api/v2/mix/order/orders-pending", ord_params))
                page_rows, end_id = _unpack_order_page(data, "orders-pending")
                for row in page_rows:
                    if not isinstance(row, dict):
                        raise StartupSnapshotError("BITGET invalid open order row")
                    symbol = row.get("symbol")
                    ord_id = row.get("orderId")
                    if not isinstance(symbol, str) or not symbol.strip() or not isinstance(ord_id, str) or not ord_id:
                        raise StartupSnapshotError("BITGET open order missing symbol or orderId")
                    if ord_id in seen_ord_ids:
                        continue
                    seen_ord_ids.add(ord_id)
                    try:
                        iid = bitget_instrument_id(symbol)
                    except ValueError:
                        raise StartupSnapshotError("BITGET open order invalid symbol") from None
                    key = ("BITGET", iid)
                    open_orders[key] = open_orders.get(key, 0) + 1
                if len(page_rows) < 100 or not end_id:
                    break
                if end_id in seen_cursors:
                    raise StartupSnapshotError("BITGET orders-pending pagination loop")
                seen_cursors.add(end_id)
                id_less_than = end_id
            else:
                raise StartupSnapshotError("BITGET orders-pending pagination exceeded max pages")

        seen_plan_ids: set[str] = set()
        for plan_type in _BITGET_PLAN_TYPES:
            plan_cursor: str | None = None
            seen_plan_cursors: set[str] = set()
            for _ in range(_MAX_PAGES):
                plan_params: dict[str, Any] = {
                    "productType": BITGET_PRODUCT_TYPE,
                    "planType": plan_type,
                    "limit": "100",
                }
                if plan_cursor:
                    plan_params["idLessThan"] = plan_cursor
                data = _signed_get(bitget_signed_get_path("/api/v2/mix/order/orders-plan-pending", plan_params))
                page_rows, end_id = _unpack_order_page(data, "orders-plan-pending")
                for row in page_rows:
                    if not isinstance(row, dict):
                        raise StartupSnapshotError("BITGET invalid plan order row")
                    symbol = row.get("symbol")
                    plan_id = row.get("orderId") or row.get("planOrderId")
                    if (
                        not isinstance(symbol, str)
                        or not symbol.strip()
                        or not isinstance(plan_id, str)
                        or not plan_id
                    ):
                        raise StartupSnapshotError("BITGET plan order missing symbol or orderId")
                    if plan_id in seen_plan_ids:
                        continue
                    seen_plan_ids.add(plan_id)
                    try:
                        iid = bitget_instrument_id(symbol)
                    except ValueError:
                        raise StartupSnapshotError("BITGET plan order invalid symbol") from None
                    key = ("BITGET", iid)
                    open_orders[key] = open_orders.get(key, 0) + 1
                if len(page_rows) < 100 or not end_id:
                    break
                if end_id in seen_plan_cursors:
                    raise StartupSnapshotError("BITGET orders-plan-pending pagination loop")
                seen_plan_cursors.add(end_id)
                plan_cursor = end_id
            else:
                raise StartupSnapshotError("BITGET orders-plan-pending pagination exceeded max pages")

        pos_after = _read_bitget_positions()
        if pos_before != pos_after:
            raise StartupSnapshotError("BITGET positions changed during startup snapshot read")
        positions.update(pos_after)

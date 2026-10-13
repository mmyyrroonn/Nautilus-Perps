"""Signed numeric loopback venue implementing the production account and order protocol."""
from __future__ import annotations

import asyncio
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import json
from pathlib import Path
import time
from urllib.parse import parse_qsl, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from websockets.asyncio.server import serve

SYMBOLS = ("BTC_USDC_PERP", "SOL_USDC_PERP")
# Public deterministic synthetic test material; never a venue credential.
SEED = base64.b64encode(bytes([7]) * 32).decode()
KEY = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
PUBLIC = base64.b64encode(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
FIXTURES = Path(__file__).parent / "fixtures" / "backpack"
ACCOUNT = json.loads((FIXTURES / "account_synthetic.json").read_text())
MARKET = json.loads((FIXTURES / "btc_market.json").read_text())
INSTRUCTIONS = {
    "/api/v1/account": "accountQuery", "/api/v1/capital": "balanceQuery",
    "/api/v1/capital/collateral": "collateralQuery", "/api/v1/position": "positionQuery",
    "/api/v1/orders": "orderQueryAll", "/wapi/v1/history/fills": "fillHistoryQueryAll",
    "/wapi/v1/history/orders": "orderHistoryQueryAll",
}


def micros():
    return time.time_ns() // 1000


def timestamp(value):
    return datetime.fromtimestamp(value // 1000000, timezone.utc).replace(
        microsecond=value % 1000000).isoformat(timespec="microseconds").replace("+00:00", "")


class ProductionPeer:
    """A complete signed account scope with deterministic economic and transport faults."""
    def __init__(self, *, fault=None, external=False, rest_fill_time_offset_us=0,
        rest_fill_timestamp_mode="exact"):
        self.fault = fault
        self.external = external
        self.rest_fill_time_offset_us = rest_fill_time_offset_us
        self.rest_fill_timestamp_mode = rest_fill_timestamp_mode
        self.truncated_fill_history_reads = 0
        self.fill_history_windows = []
        self.requests = []
        self.posts = []
        self.deletes = []
        self.queries = []
        self.orders = {}
        self.history = []
        self.fills = []
        self.private = set()
        self.workers = set()
        self.errors = []
        self.private_verified = 0
        self.signed_reads = 0
        self.connections = 0
        self.net = {symbol: Decimal("0") for symbol in SYMBOLS}
        self.entry_prices = {symbol: Decimal("0") for symbol in SYMBOLS}
        self.execution_prices = {}
        self.invalid_collateral_reads = 0
        self.trade = {symbol: 9007199254740993 for symbol in SYMBOLS}
        self.last_fill = None
        self.last_fill_event_micros = 0
        self.first_post = asyncio.Event()
        self.delete_received = asyncio.Event()
        self.release_fill = asyncio.Event()
        self.fault_used = False

    async def __aenter__(self):
        self.http = await asyncio.start_server(self.request, "127.0.0.1", 0)
        self.ws = await serve(self.stream, "127.0.0.1", 0, max_size=65536)
        self.http_url = f"http://127.0.0.1:{self.http.sockets[0].getsockname()[1]}"
        self.ws_url = f"ws://127.0.0.1:{self.ws.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *args):
        self.ws.close()
        await self.ws.wait_closed()
        self.http.close()
        await self.http.wait_closed()
        remaining = list(self.workers)
        for task in remaining:
            task.cancel()
        await asyncio.gather(*remaining, return_exceptions=True)

    def spawn(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.workers.add(task)
        def complete(task):
            self.workers.discard(task)
            if not task.cancelled() and task.exception() is not None:
                self.errors.append(type(task.exception()).__name__)
        task.add_done_callback(complete)
        return task

    def verify(self, headers, instruction, params):
        def scalar(value):
            return str(value).lower() if type(value) is bool else str(value)
        canonical = "&".join(["instruction=" + instruction,
            *[f"{key}={scalar(value)}" for key, value in sorted(params.items())],
            "timestamp=" + headers["x-timestamp"], "window=" + headers["x-window"]])
        KEY.public_key().verify(base64.b64decode(headers["x-signature"]), canonical.encode())
        assert headers["x-api-key"] == PUBLIC

    def position_rows(self):
        rows = []
        for symbol, quantity in self.net.items():
            if quantity == 0:
                continue
            row = deepcopy(ACCOUNT["position"])
            row.update(symbol=symbol, netQuantity=str(quantity), netExposureQuantity=str(abs(quantity)),
                netExposureNotional=str(abs(quantity) * Decimal("100.1")),
                netCost=str(quantity * self.entry_prices[symbol]),
                entryPrice=str(self.entry_prices[symbol]), breakEvenPrice=str(self.entry_prices[symbol]),
                positionId="position-" + symbol)
            rows.append(row)
        return rows

    def account_body(self, path):
        if path == "/api/v1/account":
            body = deepcopy(ACCOUNT["policy"])
            if self.fault == "postfill_policy" and any(self.net.values()):
                # A newly read signed policy may become inadmissible after a valid fill.
                body["autoBorrowSettlements"] = True
            return body
        if path == "/api/v1/capital":
            return {"USDC": {"available": "1000", "locked": "0", "staked": "0"}}
        if path == "/api/v1/capital/collateral":
            body = deepcopy(ACCOUNT["collateral"])
            body.update(assetsValue="1000", netEquity="1000", netEquityAvailable="1000",
                netEquityLocked="0", pnlUnrealized="0", netExposureFutures="0")
            body["collateral"][0].update(totalQuantity="1000", balanceNotional="1000",
                collateralValue="1000", openOrderQuantity="0", availableQuantity="1000")
            if self.fault == "postfill_borrow" and any(self.net.values()):
                self.invalid_collateral_reads += 1
                body.update(borrowLiability="1", liabilitiesValue="1")
            return body
        if path == "/api/v1/position":
            return self.position_rows()
        if path == "/api/v1/orders":
            rows = [deepcopy(order) for order in self.orders.values()
                if order["status"] not in {"Filled", "Cancelled"}]
            if self.external:
                row = deepcopy(ACCOUNT["resting_order"])
                row.update(id="external-untouchable", clientId=424242, executedQuantity="0",
                    executedQuoteQuantity="0", status="New", quantity="0.00002")
                rows.append(row)
            return rows
        raise AssertionError(path)

    async def response(self, writer, body, status="200 OK", extra=""):
        payload = json.dumps(body).encode()
        writer.write((f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\n"
            f"Content-Length: {len(payload)}\r\n" + extra + "Connection: close\r\n\r\n").encode() + payload)
        await writer.drain()

    def order_dto(self, row):
        dto = deepcopy(row)
        if row["orderType"] == "Market":
            for name in ("clientId", "quantity", "price", "postOnly", "reduceOnly"):
                dto.pop(name, None)
            dto["quoteQuantity"] = None
        return dto

    async def request(self, reader, writer):
        self.workers.add(asyncio.current_task())
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = raw.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers = {key.lower(): value for key, value in
                (line.split(": ", 1) for line in lines[1:] if ": " in line)}
            path = urlsplit(target).path
            params = dict(parse_qsl(urlsplit(target).query))
            self.requests.append((method, path))
            if method != "GET":
                params = json.loads(await reader.readexactly(int(headers["content-length"])))
            if path in INSTRUCTIONS:
                self.verify(headers, INSTRUCTIONS[path], params)
                self.signed_reads += 1
            elif path == "/api/v1/order":
                self.verify(headers, {"GET": "orderQuery", "POST": "orderExecute", "DELETE": "orderCancel"}[method], params)
            else:
                assert not any(key.startswith("x-") for key in headers)
            if path == "/api/v1/markets":
                rows = []
                for symbol in SYMBOLS:
                    row = deepcopy(MARKET)
                    row.update(symbol=symbol, baseSymbol=symbol.split("_")[0])
                    rows.append(row)
                return await self.response(writer, rows)
            if path == "/api/v1/depth":
                return await self.response(writer, {"lastUpdateId": "100", "timestamp": micros(),
                    "bids": [["100.0", "1.00000"]], "asks": [["101.0", "2.00000"]]})
            if path in INSTRUCTIONS and "/history/" not in path:
                return await self.response(writer, self.account_body(path))
            if "/history/" in path:
                rows = self.fills if path.endswith("fills") else self.history
                symbol = params.get("symbol")
                rows = [deepcopy(row) for row in rows if symbol is None or row["symbol"] == symbol]
                if path.endswith("fills"):
                    lower, upper = int(params["from"]), int(params["to"])
                    self.fill_history_windows.append((lower, upper))
                    epoch = datetime(1970, 1, 1)
                    def fill_millis(row):
                        delta = datetime.fromisoformat(row["timestamp"]) - epoch
                        return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000
                    rows = [row for row in rows if lower <= fill_millis(row) < upper]
                if path.endswith("fills") and (self.rest_fill_time_offset_us or self.rest_fill_timestamp_mode != "exact"):
                    for row in rows:
                        # REST and the private engine timestamp are independent source facts.
                        # Preserve the original private evidence and change only the REST instant.
                        instant = datetime.fromisoformat(row["timestamp"])
                        if self.rest_fill_timestamp_mode == "truncate_ms":
                            if instant.microsecond % 1000:
                                self.truncated_fill_history_reads += 1
                            instant = instant.replace(microsecond=instant.microsecond // 1000 * 1000)
                        else:
                            assert self.rest_fill_timestamp_mode == "exact"
                        row["timestamp"] = (instant + timedelta(microseconds=self.rest_fill_time_offset_us)).isoformat(timespec="microseconds")
                offset, limit = int(params.get("offset", 0)), int(params.get("limit", 1000))
                page = rows[offset:offset + limit]
                extra = (f"X-Page-Count: {(len(rows) + limit - 1) // limit}\r\n"
                    f"X-Current-Page: {offset // limit}\r\nX-Page-Size: {limit}\r\nX-Total: {len(rows)}\r\n")
                return await self.response(writer, page, extra=extra)
            assert path == "/api/v1/order"
            if method == "GET":
                self.queries.append(params)
                found = next((row for row in self.orders.values() if row["symbol"] == params["symbol"]
                    and (row["id"] == params.get("orderId")
                         or str(row["clientId"]) == str(params.get("clientId")))), None)
                return await self.response(writer, self.order_dto(found) if found else {"code": "ORDER_NOT_FOUND"},
                    "200 OK" if found else "404 Not Found")
            if method == "DELETE":
                self.deletes.append(params)
                self.delete_received.set()
                row = self.orders[params["orderId"]]
                assert row["symbol"] == params["symbol"]
                if self.fault == "cancel202":
                    self.spawn(self.fill_after_cancel(row))
                    return await self.response(writer, {}, "202 Accepted")
                row["status"] = "Cancelled"
                self.record_history(row)
                await self.response(writer, deepcopy(row))
                await self.broadcast(self.frame(row, "orderCancelled"))
                return
            assert method == "POST"
            common = {"symbol", "side", "orderType", "timeInForce", "quantity", "clientId", "reduceOnly",
                "autoBorrow", "autoBorrowRepay", "autoLend", "autoLendRedeem"}
            assert all(params[name] is False for name in ("autoBorrow", "autoBorrowRepay", "autoLend", "autoLendRedeem"))
            assert params["symbol"] in SYMBOLS
            if params["orderType"] == "Market":
                assert set(params) == common
                assert params["timeInForce"] == "IOC" and "price" not in params and "quoteQuantity" not in params
                assert "postOnly" not in params
            else:
                assert params["orderType"] == "Limit" and set(params) == common | {"price", "postOnly"}
                if params["side"] == "Ask":
                    assert params["reduceOnly"] is True
                    if params["postOnly"]:
                        assert params["timeInForce"] == "GTC" and Decimal(params["price"]) > Decimal("100.0")
                    else:
                        assert params["timeInForce"] == "IOC" and Decimal(params["price"]) <= Decimal("100.0")
                else:
                    assert params["side"] == "Bid" and params["timeInForce"] == "GTC" and params["postOnly"] is True
            assert all(row["status"] in {"Filled", "Cancelled"} for row in self.orders.values())
            self.posts.append(params)
            row = {**params, "id": f"owned-{len(self.posts)}", "createdAt": micros(),
                "executedQuantity": "0", "executedQuoteQuantity": "0", "status": "New",
                "selfTradePrevention": "RejectTaker"}
            if params["orderType"] == "Market":
                row.update(price=None, postOnly=None)
            self.execution_prices[row["id"]] = ("101.0" if row["side"] == "Bid" else "100.0") if params["orderType"] == "Market" else row["price"]
            self.orders[row["id"]] = row
            self.record_history(row)
            self.first_post.set()
            if self.fault == "preack" and not self.fault_used:
                self.fault_used = True
                await self.fill(row)
                await asyncio.sleep(0.2)
            if self.fault == "lostpost" and not self.fault_used:
                self.fault_used = True
                self.spawn(self.fill_delayed(row, 0.4))
                return
            await self.response(writer, self.order_dto(row))
            if row["price"] != "99.9" and row["status"] != "Filled":
                self.spawn(self.fill_delayed(row, 0.15))
        except asyncio.CancelledError:
            raise
        except Exception as e:
            self.errors.append(type(e).__name__)
            raise
        finally:
            writer.close()
            await writer.wait_closed()
            self.workers.discard(asyncio.current_task())

    def record_history(self, row):
        # The history DTO is distinct from the acknowledged POST/GET order DTO.
        # In particular, omission of reduceOnly is not an assertion that it is false.
        historic = {name: deepcopy(row[name]) for name in (
            "clientId", "executedQuantity", "executedQuoteQuantity", "id", "orderType",
            "postOnly", "price", "quantity", "selfTradePrevention", "side", "status",
            "symbol", "timeInForce",
        )}
        historic["createdAt"] = timestamp(row["createdAt"])
        historic["quoteQuantity"] = None if row["price"] is None else str(Decimal(row["quantity"]) * Decimal(row["price"]))
        if row["orderType"] == "Market":
            for name in ("clientId", "quantity", "price", "postOnly"):
                historic.pop(name, None)
        historic.update({name: None for name in (
            "expiryReason", "slippageTolerance", "slippageToleranceType",
            "stopLossLimitPrice", "stopLossTriggerBy", "stopLossTriggerPrice", "strategyId",
            "systemOrderType", "takeProfitLimitPrice", "takeProfitTriggerBy",
            "takeProfitTriggerPrice", "triggerBy", "triggerPrice", "triggerQuantity",
        )})
        self.history = [old for old in self.history if old["id"] != row["id"]] + [historic]

    def frame(self, row, event, *, trade=None, fee=None):
        at = micros() - 1000
        if trade is not None:
            # Preserve exact event instants while exercising equivalent REST/WS formatting.
            at = max(at // 10 * 10, self.last_fill_event_micros + 10)
            if at % 1000 == 0:
                at += 10
            self.last_fill_event_micros = at
        data = {"e": event, "E": at, "T": at, "s": row["symbol"], "c": row["clientId"],
            "S": row["side"], "o": row["orderType"].upper(), "f": row["timeInForce"], "q": row["quantity"],
            "p": row["price"], "r": row["reduceOnly"], "X": row["status"], "i": row["id"],
            "z": row["executedQuantity"], "Z": row["executedQuoteQuantity"],
            "V": "RejectTaker", "O": "USER", "y": row["postOnly"], "t": trade}
        if trade is not None:
            data.update(l=row["quantity"], L=self.execution_prices[row["id"]], m=row["postOnly"] is True, n=fee, N="USDC")
        return {"stream": "account.orderUpdate." + row["symbol"], "data": data}

    async def broadcast(self, frame):
        for ws in tuple(self.private):
            await ws.send(json.dumps(frame))

    async def fill_delayed(self, row, delay):
        await asyncio.sleep(delay)
        await self.fill(row)

    async def fill_after_cancel(self, row):
        await self.release_fill.wait()
        await self.fill(row)

    async def fill(self, row):
        assert row["status"] != "Filled"
        quantity = Decimal(row["quantity"])
        symbol = row["symbol"]
        previous = self.net[symbol]
        price = Decimal(self.execution_prices[row["id"]])
        self.net[symbol] += quantity if row["side"] == "Bid" else -quantity
        if row["side"] == "Bid":
            self.entry_prices[symbol] = (previous * self.entry_prices[symbol] + quantity * price) / self.net[symbol]
        row.update(status="Filled", executedQuantity=row["quantity"],
            executedQuoteQuantity=str(quantity * price))
        self.record_history(row)
        trade = self.trade[symbol]
        self.trade[symbol] += 1
        fee = "-0.000001" if row["postOnly"] is True else "0.000003"
        frame = self.frame(row, "orderFill", trade=trade, fee=fee)
        self.last_fill = frame
        self.fills.append({"clientId": str(row["clientId"]), "fee": fee, "feeSymbol": "USDC",
            "isMaker": row["postOnly"] is True, "orderId": row["id"], "price": self.execution_prices[row["id"]], "quantity": row["quantity"],
            "side": row["side"], "symbol": symbol, "systemOrderType": None,
            "timestamp": timestamp(frame["data"]["T"]), "tradeId": trade})
        # WS replay followed by repeated REST history must apply each true economic trade once.
        await self.broadcast(frame)
        await self.broadcast(frame)

    async def stream(self, ws):
        self.connections += 1
        producer = None
        desired = set()
        try:
            async for raw in ws:
                command = json.loads(raw)
                if command["method"] != "SUBSCRIBE":
                    continue
                if "signature" in command:
                    signature = command["signature"]
                    assert signature[0] == PUBLIC
                    KEY.public_key().verify(base64.b64decode(signature[1]),
                        f"instruction=subscribe&timestamp={signature[2]}&window={signature[3]}".encode())
                    assert set(command["params"]) == {"account.balanceUpdate", "account.orderUpdate", "account.positionUpdate"}
                    self.private_verified += 1
                    self.private.add(ws)
                else:
                    desired.update(command["params"])
                    if producer is None:
                        producer = asyncio.create_task(self.public_frames(ws, desired))
        finally:
            self.private.discard(ws)
            if producer is not None:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

    async def public_frames(self, ws, desired):
        sequence = 100
        while True:
            sequence += 1
            for topic in tuple(desired):
                channel, symbol = topic.split(".", 1)
                at = micros()
                fields = {"bookTicker": {"u": sequence, "b": "100.0", "B": "1.00000", "a": "101.0", "A": "2.00000"},
                    "trade": {"t": sequence, "p": "100.1", "q": "0.10000", "b": "1", "a": "2", "m": True},
                    "markPrice": {"p": "100.2", "i": "100.3", "f": "0.01", "n": at // 1000 + 3600000},
                    "depth": {"U": sequence, "u": sequence, "b": [["100.0", "1.10000"]], "a": []}}[channel]
                await ws.send(json.dumps({"stream": topic, "data": {"e": channel, "s": symbol,
                    "E": at, "T": at - 1000, **fields}}))
            await asyncio.sleep(0.1)

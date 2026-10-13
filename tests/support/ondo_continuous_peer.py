"""Stateful synthetic Ondo HTTP/WS peer for installed-native application acceptance.

The schema follows the native adapter's authored fixtures, not observed venue data.
All writes originate from the installed Rust adapter. Authentication values are never
read or recorded. This module can only listen on loopback.
"""

from __future__ import annotations

import asyncio
import copy
import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlsplit

from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


MARKET = "BTC-USD.P"
INSTRUMENT = "BTC-USD-PERP.ONDO"
SECOND_MARKET = "TSLA-USD.P"
SECOND_INSTRUMENT = "TSLA-USD-PERP.ONDO"
CHANNEL = "cancelAllOrdersAfterPerps"


def timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class ContinuousPeer:
    """A shared account, REST order history, fill history and two real socket surfaces."""

    def __init__(
        self,
        *,
        fill_on_cancel: bool = False,
        missing_release: bool = False,
        empty_market_entry: bool = False,
    ) -> None:
        self.fill_on_cancel = fill_on_cancel
        self.missing_release = missing_release
        self.empty_market_entry = empty_market_entry
        self.orders: dict[str, dict] = {}
        self.fills: list[dict] = []
        self.quantities = {MARKET: Decimal(0), SECOND_MARKET: Decimal(0)}
        self.requests: list[dict] = []
        self.frames: list[dict] = []
        self._lock = threading.Lock()
        self._private: dict[object, set[str]] = {}
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        venue = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args) -> None:
                return

            def do_GET(self):  # noqa: N802
                self.answer()

            def do_POST(self):  # noqa: N802
                self.answer()

            def do_DELETE(self):  # noqa: N802
                self.answer()

            def answer(self):
                size = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(size)) if size else None
                path = unquote(urlsplit(self.path).path)
                with venue._lock:
                    # Order command bodies contain only synthetic order data. No headers logged.
                    venue.requests.append(
                        {"method": self.command, "path": path, "body": body}
                    )
                    status, result, updates = venue._answer(self.command, path, body)
                raw = json.dumps({"success": status == 200, "result": result}).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)
                if updates:
                    asyncio.run_coroutine_threadsafe(
                        venue._publish(updates), venue.loop
                    )

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()

        async def websocket(socket):
            subscriptions: dict[str, list[str]] = {}
            public_task = None
            try:
                async for raw in socket:
                    message = json.loads(raw)
                    op, channel = message.get("op"), message.get("channel", "")
                    # Never retain login or signatures.
                    with venue._lock:
                        venue.frames.append(
                            {
                                "op": op,
                                "channel": channel,
                                "has_timeout": "timeout_seconds" in message,
                            }
                        )
                    if op == "login":
                        venue._private[socket] = set()
                        response = {"type": "loggedIn", "msg": "Login successful"}
                    elif op == "subscribe":
                        if socket in venue._private:
                            venue._private[socket].add(channel)
                        subscriptions[channel] = message.get("markets", [])
                        response = {"type": "subscribed", "channel": channel}
                        if socket not in venue._private:
                            response["data"] = {
                                "channel": channel,
                                "markets": message.get("markets", []),
                            }
                        if channel == "topOfBooksPerps" and public_task is None:
                            public_task = asyncio.create_task(venue._quotes(socket))
                    elif op == "unsubscribe":
                        if channel == CHANNEL and venue.missing_release:
                            continue
                        response = {"type": "unsubscribed", "channel": channel}
                    elif op == "ping":
                        response = {"type": "pong"}
                    else:
                        continue
                    await socket.send(json.dumps(response))
            except ConnectionClosed:
                pass
            finally:
                venue._private.pop(socket, None)
                if public_task is not None:
                    public_task.cancel()
                    await asyncio.gather(public_task, return_exceptions=True)

        async def start():
            self.ws = await serve(websocket, "127.0.0.1", 0)
            self.ws_port = self.ws.sockets[0].getsockname()[1]
            self.ready.set()

        def run():
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(start())
            self.loop.run_forever()

        self.ws_thread = threading.Thread(target=run, daemon=True)
        self.ws_thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("loopback websocket failed to start")

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.http.server_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}/ws"

    @property
    def quantity(self) -> Decimal:
        return self.quantities[MARKET]

    def _fill(self, order: dict) -> dict:
        held = self.quantities[order["market"]]
        quantity = Decimal(order["size"]) - Decimal(order["filledSize"])
        order.update(
            status="fullyfilled",
            filledSize=order["size"],
            lastFillSize=str(quantity),
            filledAt=timestamp(),
            fee="0.01",
        )
        self.quantities[order["market"]] += (
            quantity if order["side"] == "buy" else -quantity
        )
        fill = {
            "id": f"fill-{len(self.fills)}",
            "orderId": order["orderId"],
            "clientOrderId": order["clientOrderId"],
            "market": order["market"],
            "price": order["price"],
            "size": str(quantity),
            "side": order["side"],
            "direction": ("closeShort" if held < 0 else "openLong")
            if order["side"] == "buy"
            else ("closeLong" if held > 0 else "openShort"),
            "fee": "0.01",
            "time": timestamp(),
            "isMaker": False,
        }
        self.fills.append(fill)
        return copy.deepcopy(fill)

    def _answer(self, method: str, path: str, body: dict | None):
        updates = []
        if method == "GET" and path == "/v1/markets":
            result = {
                "perps": {
                    "tradingPairs": [
                        {
                            "market": MARKET,
                            "baseIncrement": "0.0001",
                            "quoteIncrement": "0.1",
                            "disabled": False,
                            "makerFee": "0.0001",
                            "takerFee": "0.00025",
                        },
                        {
                            "market": SECOND_MARKET,
                            "baseIncrement": "0.01",
                            "quoteIncrement": "0.01",
                            "disabled": False,
                            "makerFee": "0.0001",
                            "takerFee": "0.00025",
                        },
                    ]
                },
                "tokenConfig": [
                    {
                        "id": "USDC",
                        "name": "USD Coin",
                        "decimals": 2,
                        "tokenDecimals": 6,
                        "networks": {},
                    }
                ],
            }
        elif method == "GET" and path == "/v1/account":
            result = {"accountID": "unit-account"}
        elif method == "GET" and path == "/v1/perps/contracts":
            result = [
                {"market": market, "disabled": False, "isClosed": False}
                for market in self.quantities
            ]
        elif method == "GET" and path == "/v1/perps/balance":
            # Real native account events consume these exact venue-provided values.
            result = dict.fromkeys(
                (
                    "realizedPnl",
                    "unrealizedPnl",
                    "usedMargin",
                    "maintenanceMarginRequirement",
                    "totalMaintenanceMargin",
                    "marginRatio",
                    "leverage",
                    "totalFundingPayments",
                    "totalPnL",
                ),
                "0.00",
            )
            cash = Decimal("5000.00") - Decimal("0.01") * len(self.fills)
            result.update(
                dict.fromkeys(
                    (
                        "walletBalance",
                        "marginBalance",
                        "availableMargin",
                        "withdrawableMargin",
                    ),
                    str(cash),
                )
            )
            result.update(
                underLiquidation=False,
                totalTradingFees=str(Decimal("0.01") * len(self.fills)),
                netInvested="5000.00",
            )
        elif method == "GET" and path == "/v1/perps/orders":
            result = list(self.orders.values())
        elif method == "GET" and path == "/v1/perps/fills":
            result = self.fills
        elif method == "GET" and path == "/v1/perps/funding_fees":
            result = []
        elif method == "GET" and path == "/v1/perps/positions":
            result = [
                {
                    "market": market,
                    "direction": "long" if quantity > 0 else "short",
                    "netQuantity": str(abs(quantity)),
                    "averageEntryPrice": "85000.0" if market == MARKET else "227.50",
                    "usedMargin": "5.00",
                    "unrealizedPnl": "0.00",
                    "markPrice": "85000.0" if market == MARKET else "227.50",
                    "liquidationPrice": "180.00",
                    "bankruptcyPrice": "170.00",
                    "maintenanceMargin": "1.00",
                    "notionalValue": "8.50",
                    "leverage": "2.0",
                    "netFundingSinceNeutral": "0.00",
                    "returnOnEquity": "0.00",
                }
                for market, quantity in self.quantities.items()
                if quantity != 0
            ]
        elif method == "POST" and path == "/v1/perps/orders":
            market_order = body["type"] == "market"
            if market_order and (
                "price" in body or "timeInForce" in body or body["postOnly"]
            ):
                return 400, None, []
            name = body["clientOrderId"]
            order = {
                "orderId": "venue-" + name,
                "clientOrderId": name,
                "market": body["market"],
                "side": body["side"],
                "price": body.get(
                    "price", "85000.0" if body["market"] == MARKET else "227.50"
                ),
                "size": body["size"],
                "filledSize": "0",
                "lastFillSize": "0",
                "filledCost": "0",
                "fee": "0",
                "status": "open",
                "createdAt": timestamp(),
                "type": body["type"],
                "reduceOnly": body["reduceOnly"],
            }
            if not market_order:
                order["timeInForce"] = body["timeInForce"]
            self.orders[name] = order
            if market_order or body["timeInForce"] == "IOC":
                held = self.quantities[body["market"]]
                if body["reduceOnly"] and (
                    held == 0
                    or Decimal(body["size"]) > abs(held)
                    or (held > 0 and body["side"] != "sell")
                    or (held < 0 and body["side"] != "buy")
                ):
                    return 400, None, []
                if market_order and self.empty_market_entry and not body["reduceOnly"]:
                    order["status"] = "canceled"
                else:
                    fill = self._fill(order)
                    updates.append(("fillsPerps", fill))
            updates.insert(0, ("ordersPerps", copy.deepcopy(order)))
            result = order
        elif path.startswith("/v1/perps/orders/"):
            name = path.split("/v1/perps/orders/", 1)[1]
            if name.startswith("client:"):
                name = name[7:]
            elif name.startswith("venue-"):
                name = name[6:]
            if name not in self.orders:
                return 404, None, []
            order = self.orders[name]
            if method == "DELETE":
                if self.fill_on_cancel:
                    self.fill_on_cancel = False
                    fill = self._fill(order)
                    updates = [
                        ("ordersPerps", copy.deepcopy(order)),
                        ("fillsPerps", fill),
                    ]
                    return 400, None, updates
                order["status"] = "canceled"
                updates = [("ordersPerps", copy.deepcopy(order))]
            result = order
        else:
            return 404, None, []
        return 200, copy.deepcopy(result), updates

    async def _publish(self, updates: list[tuple[str, dict]]) -> None:
        # HTTP result must reach the adapter before the same genuine stream report.
        await asyncio.sleep(0.05)
        for channel, item in updates:
            for socket, subscribed in tuple(self._private.items()):
                if channel in subscribed:
                    try:
                        await socket.send(
                            json.dumps(
                                {"type": "update", "channel": channel, "data": [item]}
                            )
                        )
                    except ConnectionClosed:
                        pass

    async def _quotes(self, socket) -> None:
        try:
            await asyncio.sleep(0.05)
            while True:
                stamp = timestamp()
                await socket.send(
                    json.dumps(
                        {
                            "type": "update",
                            "channel": "topOfBooksPerps",
                            "timestamp": stamp,
                            "data": [
                                {
                                    "market": MARKET,
                                    "time": stamp,
                                    "bids": [["85000.0", "1.0"]],
                                    "asks": [["85001.0", "1.0"]],
                                },
                                {
                                    "market": SECOND_MARKET,
                                    "time": stamp,
                                    "bids": [["227.49", "10.0"]],
                                    "asks": [["227.50", "10.0"]],
                                },
                            ],
                        }
                    )
                )
                await asyncio.sleep(0.1)
        except ConnectionClosed:
            pass

    def close(self) -> None:
        async def stop():
            self.ws.close()
            await self.ws.wait_closed()

        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.ws_thread.join(5)
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(5)
        self.loop.close()

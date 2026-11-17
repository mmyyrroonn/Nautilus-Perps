"""Installed Lighter factories replay correlated margin errors through the application gateway."""
import asyncio
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_native_execution import NativeOrderGateway
from opportunity_preflight import OrderIntent


class LighterPeer:
    def __init__(self):
        self.data = json.loads((Path(__file__).parent / "support/lighter_protocol.json").read_text())
        self.requests, self.creates, self.errors = [], [], []
        self.wire_orders = []
        self.unidentified_sent = asyncio.Event()
        self.release_rejections = asyncio.Event()

    async def http(self, reader, writer):
        try:
            headers = await reader.readuntil(b"\r\n\r\n")
            method, target, _ = headers.split(b"\r\n", 1)[0].decode().split()
            route = target.split("?", 1)[0]
            self.requests.append((method, route))
            assert method == "GET", "loopback test never needs an HTTP mutation"
            if route.endswith("orderBookDetails"):
                payload = self.data["metadata"]
            elif route.endswith("account"):
                payload = self.data["account"]
            elif route.endswith("nextNonce"):
                payload = {"code": 200, "nonce": 9999}
            elif route.endswith(("accountActiveOrders", "accountInactiveOrders")):
                payload = {"code": 200, "orders": []}
            elif route.endswith("trades"):
                payload = {"code": 200, "trades": []}
            else:
                raise AssertionError("unexpected endpoint " + route)
            body = json.dumps(payload).encode()
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nConnection: close\r\nContent-Length: "
                         + str(len(body)).encode() + b"\r\n\r\n" + body)
            await writer.drain()
        except Exception as error:
            self.errors.append(type(error).__name__ + ": " + str(error))
        finally:
            writer.close()
            await writer.wait_closed()

    async def stream(self, ws):
        await ws.send(json.dumps({"type": "connected"}))
        async for raw in ws:
            request = json.loads(raw)
            kind = request.get("type")
            if kind in {"subscribe", "unsubscribe"}:
                channel = request["channel"].replace("/", ":")
                await ws.send(json.dumps({"type": "subscribed" if kind == "subscribe" else "unsubscribed", "channel": channel}))
                if kind == "unsubscribe":
                    continue
                topic = channel.split(":")[0]
                data = {"type": "subscribed/" + topic, "channel": channel, "timestamp": time.time_ns() // 1000000}
                if topic == "account_all_orders": data["orders"] = {}
                elif topic == "account_all_trades": data.update(trades=[], total_volume="0", monthly_volume="0", weekly_volume="0", daily_volume="0")
                elif topic == "account_all_positions": data.update(positions={}, shares=[])
                elif topic == "account_all_assets": data["assets"] = {}
                elif topic == "user_stats": data["stats"] = self.data["stats"]
                else: continue
                await ws.send(json.dumps(data))
            elif kind == "jsonapi/sendtx":
                data = request["data"]
                assert data["tx_type"] == 14 and isinstance(data["id"], str)
                self.creates.append(data["id"])
                assert isinstance(data["tx_info"], dict)
                self.wire_orders.append(data["tx_info"])
                if len(self.creates) == 2:
                    await ws.send(json.dumps({"error": {"code": 21613, "message": "invalid margin mode"}}))
                    self.unidentified_sent.set()
                    await self.release_rejections.wait()
                    for index in (1, 1, 0):
                        await ws.send(json.dumps({"error": {"code": 21613, "message": "invalid margin mode"}, "id": self.creates[index]}))
                    await ws.send(json.dumps({"type": "jsonapi/sendtx", "code": 200, "id": self.creates[1]}))
            elif kind == "ping":
                await ws.send(json.dumps({"type": "pong"}))

    async def __aenter__(self):
        from websockets.asyncio.server import serve
        self.http_server = await asyncio.start_server(self.http, "127.0.0.1", 0)
        self.ws_server = await serve(self.stream, "127.0.0.1", 0)
        self.http_url = "http://127.0.0.1:" + str(self.http_server.sockets[0].getsockname()[1])
        self.ws_url = "ws://127.0.0.1:" + str(self.ws_server.sockets[0].getsockname()[1])
        return self

    async def __aexit__(self, *args):
        self.release_rejections.set()
        self.http_server.close()
        self.ws_server.close()
        await self.http_server.wait_closed()
        await self.ws_server.wait_closed()


def test_installed_lighter_margin_error_finishes_original_ids_without_resend():
    from nautilus_trader.adapters import lighter as native
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import AccountId, OmsType, StrategyId, TraderId
    from nautilus_trader.trading import Strategy, StrategyConfig
    control = native.LighterExecutionControl()

    class Scenario(Strategy):
        def __init__(self):
            super().__init__(StrategyConfig(strategy_id=StrategyId.from_str("LIGHTER-145"),
                oms_type=OmsType.NETTING, log_events=False, log_commands=False))
            self.receipts, self.fills = [], []
            self.gateway = None

        def on_start(self):
            market = NS(venue="LIGHTER", instrument_id="ETH-PERP.LIGHTER")
            self.gateway = NativeOrderGateway(self, [market], self.receipts.append, lambda _: None,
                self.fills.append, native_controls={"LIGHTER": control})
            for index in range(2):
                self.gateway.dispatch(OrderIntent(action="submit", request_id=f"request-{index}",
                    venue="LIGHTER", instrument_id=market.instrument_id, client_order_id=f"margin-{index}",
                    side="SELL", quantity=D("0.0050"), price=D("2361.31"), time_in_force="IOC"), time.time_ns())

        def on_order_rejected(self, event):
            self.gateway.on_native_event("rejected", event, time.time_ns())

        def on_order_denied(self, event):
            self.gateway.on_native_event("denied", event, time.time_ns())

    async def scenario():
        async with LighterPeer() as peer:
            endpoints = dict(base_url_http=peer.http_url, base_url_ws=peer.ws_url,
                environment=native.LighterEnvironment.TESTNET, http_timeout_secs=3, ws_timeout_secs=3)
            # Public deterministic scalar for the owned loopback peer, never a venue credential.
            execution = native.LighterExecutionClientConfig(AccountId.from_str("LIGHTER-001"),
                account_index=12345, api_key_index=5, private_key="01" + "00" * 39,
                control=control, **endpoints)
            node = (LiveNode.builder("LIGHTER-PROTOCOL", TraderId.from_str("LIGHTER-001"), Environment.LIVE)
                .with_logging(LoggerConfig(stdout_level=LogLevel.OFF, fileout_level=LogLevel.OFF,
                    bypass_logging=True, print_config=False))
                .with_timeout_connection(10).with_reconciliation(False).with_timeout_reconciliation(0)
                .with_timeout_portfolio(0).with_timeout_disconnection_secs(2)
                .with_delay_post_stop_secs(0).with_delay_shutdown_secs(0)
                .with_load_state(False).with_save_state(False)
                .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=False,
                    inflight_check_interval_ms=0, open_check_interval_secs=None, position_check_interval_secs=None))
                .add_data_client(None, native.LighterDataClientFactory(), native.LighterDataClientConfig(**endpoints))
                .add_exec_client(None, native.LighterExecutionClientFactory(), execution).build())
            strategy = Scenario()
            node.add_strategy(strategy)
            task = asyncio.ensure_future(node.run_async())
            try:
                await asyncio.wait_for(peer.unidentified_sent.wait(), 15)
                await asyncio.sleep(0.1)
                assert not strategy.receipts, "unidentified error must not reject either original ID"
                peer.release_rejections.set()
                deadline = time.monotonic() + 5
                while len(strategy.receipts) < 2 and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert not peer.errors, peer.errors
                assert len(strategy.receipts) == 2
                assert {r.client_order_id for r in strategy.receipts} == {"margin-0", "margin-1"}
                assert all(r.kind == "rejected" and r.final_filled_quantity == 0
                    and r.final_fill_source == "lighter_submission_rejected" for r in strategy.receipts)
                assert all(r.reason_code == "margin_configuration" and r.exchange_error_code == 21613
                    for r in strategy.receipts)
                strategy.gateway.poll(time.time_ns() + 30_000_000_000)
                await asyncio.sleep(0.1)
                assert len(strategy.receipts) == len(peer.creates) == 2 and not strategy.fills
                assert not any("Orders" in route for _, route in peer.requests)
            finally:
                if strategy.gateway is not None:
                    strategy.gateway.close()
                node.handle().stop()
                await asyncio.wait_for(task, 10)
    asyncio.run(scenario())

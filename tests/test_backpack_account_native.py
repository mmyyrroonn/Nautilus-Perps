"""Actual native readonly node against authenticated synthetic loopback peers."""
from __future__ import annotations

import asyncio
import base64
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qsl, urlsplit

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_account import run_account
from backpack_config import parse_plan
from test_backpack_config import private_document
from test_backpack_public_native import MARKET, PublicPeer, SYMBOL

FIXTURE = json.loads((Path(__file__).parent / "fixtures/backpack/account_synthetic.json").read_text())
# Public synthetic test material, identical to the native account fixture key.
KEY = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
SEED = base64.b64encode(bytes([7]) * 32).decode()
PUBLIC = base64.b64encode(KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
INSTRUCTIONS = {"/api/v1/account": "accountQuery", "/api/v1/capital": "balanceQuery",
    "/api/v1/capital/collateral": "collateralQuery", "/api/v1/position": "positionQuery",
    "/api/v1/orders": "orderQueryAll", "/wapi/v1/history/fills": "fillHistoryQueryAll",
    "/wapi/v1/history/orders": "orderHistoryQueryAll"}


def require_account_native():
    module = pytest.importorskip("nautilus_trader.adapters.backpack")
    assert hasattr(module, "BackpackExecutionClientFactory"), "install the explicit readonly candidate"


def order_frame(event, trade=0, *, filled=False):
    micros = time.time_ns() // 1000 - 1000
    fill = event == "orderFill"
    data = {"e": event, "E": micros, "T": micros, "s": SYMBOL, "c": 1,
        "S": "Bid", "o": "LIMIT", "f": "GTC", "q": "0.00002", "p": "100.1", "r": False,
        "X": "Filled" if filled else ("PartiallyFilled" if fill else "New"),
        "i": "synthetic-order-A", "z": "0.00002" if filled else ("0.00001" if fill else "0"),
        "Z": "0.002002" if filled else ("0.001001" if fill else "0"),
        "V": "RejectTaker", "O": "USER", "y": True, "t": None}
    if fill:
        data.update(t=trade, l="0.00001", L="100.1", m=True, n="-0.000001", N="USDC")
    return {"stream": f"account.orderUpdate.{SYMBOL}", "data": data}


class AccountPeer(PublicPeer):
    def __init__(self, *, malformed_history=False, private_reconnect=False, conflicting_reconnect=False,
                 emit_orders=True, balance_free="111"):

        super().__init__()
        self.malformed_history = malformed_history
        self.private_reconnect = private_reconnect
        self.conflicting_reconnect = conflicting_reconnect
        self.emit_orders = emit_orders
        self.balance_free = balance_free
        self.auth_key = KEY
        self.public_key = PUBLIC
        self.private_connections = 0
        self.private_verified = []
        self.private_ready = asyncio.Event()
        self.authenticated_reads = []
        self.fixture_sent = False
        self.fixture_ready = asyncio.Event()
        self.recovery_ready = asyncio.Event()
        self.last_fill = None
        self.accepted_frame = None

    async def __aenter__(self):
        self.http = await asyncio.start_server(self.request, "127.0.0.1", 0)
        self.ws = await serve(self.stream, "127.0.0.1", 0, max_size=65_536)
        self.http_url = f"http://127.0.0.1:{self.http.sockets[0].getsockname()[1]}"
        self.ws_url = f"ws://127.0.0.1:{self.ws.sockets[0].getsockname()[1]}"
        return self

    async def request(self, reader, writer):
        worker = asyncio.current_task()
        self.workers.add(worker)
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
            lines = raw.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers = {key.lower(): value for key, value in
                       (line.split(": ", 1) for line in lines[1:] if ": " in line)}
            path = urlsplit(target).path
            self.requests.append((method, path, {}))  # Never retain authentication headers.
            assert method == "GET"
            if path in INSTRUCTIONS:
                params = dict(parse_qsl(urlsplit(target).query))
                canonical = "&".join(["instruction=" + INSTRUCTIONS[path],
                    *[f"{key}={value}" for key, value in sorted(params.items())],
                    "timestamp=" + headers["x-timestamp"], "window=" + headers["x-window"]])
                self.auth_key.public_key().verify(base64.b64decode(headers["x-signature"]), canonical.encode())
                assert headers["x-api-key"] == self.public_key
                self.authenticated_reads.append(path)
            else:
                assert path in {"/api/v1/markets", "/api/v1/depth"}
                assert not any(key.startswith("x-") for key in headers)
            extra = ""
            if path == "/api/v1/markets": body = [MARKET]
            elif path == "/api/v1/depth":
                body = {"lastUpdateId": "100", "timestamp": time.time_ns() // 1000,
                        "bids": [["100.0", "1.00000"]], "asks": [["101.0", "2.00000"]]}
            elif path == "/api/v1/account": body = FIXTURE["policy"]
            elif path == "/api/v1/capital": body = FIXTURE["balances"]
            elif path == "/api/v1/capital/collateral": body = FIXTURE["collateral"]
            elif path == "/api/v1/orders": body = [] if self.fixture_sent or not self.emit_orders else [FIXTURE["resting_order"]]
            else: body = []
            if "/history/" in path and not self.malformed_history:
                extra = "X-Page-Count: 0\r\nX-Current-Page: 0\r\nX-Page-Size: 1000\r\nX-Total: 0\r\n"
            payload = json.dumps(body).encode()
            writer.write((f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\n"
                          + extra + "Connection: close\r\n\r\n").encode() + payload)
            await writer.drain()
            if path == "/wapi/v1/history/fills" and self.fixture_sent:
                self.recovery_ready.set()
        finally:
            writer.close()
            await writer.wait_closed()
            self.workers.discard(worker)

    async def stream(self, ws):
        self.active += 1
        producer = None
        desired = set()
        try:
            async for raw in ws:
                command = json.loads(raw)
                if command.get("method") != "SUBSCRIBE": continue
                if "signature" in command:
                    self.private_connections += 1
                    signature = command["signature"]
                    assert signature[0] == self.public_key
                    message = f"instruction=subscribe&timestamp={signature[2]}&window={signature[3]}"
                    self.auth_key.public_key().verify(base64.b64decode(signature[1]), message.encode())
                    assert set(command["params"]) == {
                        "account.balanceUpdate", "account.orderUpdate", "account.positionUpdate"}
                    self.private_verified.append(int(signature[2]))
                    self.private_ready.set()
                    if producer is None:
                        producer = asyncio.create_task(self.send_private(ws, self.private_connections))
                else:
                    desired.update(command["params"])
                    if len(desired) == 4 and producer is None:
                        self.connections += 1
                        producer = asyncio.create_task(self.send_frames(ws, self.connections))
        finally:
            self.active -= 1
            if producer is not None:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

    async def send_private(self, ws, connection):
        await self.ready.wait()
        if connection > 1:
            await asyncio.wait_for(self.recovery_ready.wait(), 5)
            await asyncio.sleep(0.1)
        if self.emit_orders:
            if connection == 1:
                self.accepted_frame = order_frame("orderAccepted")
                self.first_fill = order_frame("orderFill", 77)
                self.last_fill = order_frame("orderFill", 78, filled=True)
            if connection == 1 or self.conflicting_reconnect:
                await ws.send(json.dumps(self.accepted_frame))
                await asyncio.sleep(0.1)
            frames = ([self.first_fill, self.last_fill, self.last_fill]
                      if connection == 1 else [self.last_fill, self.last_fill])
            if connection > 1 and self.conflicting_reconnect:
                # Same true trade ID/time with a changed price is contradictory economic evidence.
                contradictory = {**self.last_fill, "data": {**self.last_fill["data"], "L": "100.2"}}
                frames = [contradictory]
            for frame in frames:
                await ws.send(json.dumps(frame))
                await asyncio.sleep(0.1)
        micros = time.time_ns() // 1000
        await ws.send(json.dumps({"stream": "account.balanceUpdate", "data": {
            "e": "balanceUpdate", "E": micros, "T": micros, "a": "USDC",
            "A": self.balance_free, "L": "10", "S": "5"}}))
        self.fixture_sent = True
        self.fixture_ready.set()
        if self.private_reconnect and connection == 1:
            await asyncio.sleep(0.1)
            await ws.close()


def plan_for(peer, tmp_path, monkeypatch, duration=6):
    data = private_document()
    data.update(environment="loopback", base_url_http=peer.http_url, base_url_ws=peer.ws_url,
                duration_secs=duration, request_timeout_secs=5)
    plan = parse_plan(data, tmp_path)
    monkeypatch.setenv(plan.account.credential_env, SEED)
    return plan


def test_actual_readonly_node_balances_true_fills_and_no_writes(tmp_path, monkeypatch):
    require_account_native()
    async def run():
        async with AccountPeer() as peer:
            plan = plan_for(peer, tmp_path, monkeypatch)
            summary, path = await run_account(plan)
            assert summary["status"] == "completed", summary["failure"]
            assert summary["shutdown_complete"] and peer.active == 0
            assert peer.fixture_sent and peer.private_verified and peer.authenticated_reads
            assert all(method == "GET" for method, _, _ in peer.requests)
            assert summary["private_client_registered"] and summary["shared_native_rest_quota"]
            assert not summary["flat_verified"] and not summary["durable_economic_acknowledgement"]
            assert not summary["native_health"]["account"]["private_subscription_confirmed"]
            events = [json.loads(line) for line in path.with_name("events.jsonl").read_text().splitlines()]
            observations = [e for e in events if e["kind"] == "engine_account_observation"]
            assert any(e["account_observed"] for e in observations)
            assert any(any(b["free"] == "111.00000000 USDC" for b in e["wallet_trading_balances"])
                       for e in observations)
            orders = [o for e in observations for o in e["orders"] if o["status"] == "FILLED"]
            assert orders and orders[-1]["filled_quantity"] == "0.00002"
            assert orders[-1]["trade_ids"] == ["77", "78"]
            assert orders[-1]["commissions"] == ["-0.00000200 USDC"]
            assert summary["native_health"]["account"]["pending_fills"] == 2
            assert SEED not in path.read_text() and PUBLIC not in path.read_text()
    asyncio.run(run())


def account_records(path):
    return [json.loads(line) for line in path.with_name("events.jsonl").read_text().splitlines()]


def test_actual_account_cancel_closes_sockets_and_releases_identity_owner(tmp_path, monkeypatch):
    require_account_native()
    async def scenario():
        from dataclasses import replace
        async with AccountPeer() as peer:
            plan = plan_for(peer, tmp_path, monkeypatch, duration=10)
            task = asyncio.create_task(run_account(plan))
            await asyncio.wait_for(peer.fixture_ready.wait(), 6)
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 12)
            path = next(plan.output_dir.glob("*/summary.json"))
            summary = json.loads(path.read_text())
            assert summary["failure"] == "cancelled" and summary["shutdown_complete"]
            assert peer.active == 0
            assert not summary["native_health"]["account"]["transport_connected"]
            assert not summary["flat_verified"] and not summary["durable_economic_acknowledgement"]
            assert (plan.journal_dir / "identity.lock").is_file()
            # Reopen the same native namespace through another actual factory/node after teardown.
            second, _ = await asyncio.wait_for(run_account(replace(plan, duration_secs=5)), 16)
            assert second["status"] == "completed", second["failure"]
            assert second["shutdown_complete"] and peer.active == 0
            assert second["native_health"]["account"]["run_id"] != summary["native_health"]["account"]["run_id"]
            assert all(method == "GET" for method, _, _ in peer.requests)
    asyncio.run(scenario())


def test_actual_failed_pagination_never_claims_observation_complete_and_releases_owner(tmp_path, monkeypatch):
    require_account_native()
    async def scenario():
        from dataclasses import replace
        async with AccountPeer(malformed_history=True) as peer:
            plan = plan_for(peer, tmp_path, monkeypatch)
            summary, path = await asyncio.wait_for(run_account(plan), 17)
            assert summary["status"] == "failed", summary
            assert not summary["actor_started"]
            # Wallet REST snapshot publication precedes history validation; it is not history coverage.
            assert not summary["native_health"]["account"]["private_subscription_confirmed"]
            assert summary["shutdown_complete"] and peer.active == 0
            assert not summary["flat_verified"] and not summary["durable_economic_acknowledgement"]
            assert not peer.private_verified
            assert "/wapi/v1/history/fills" in peer.authenticated_reads
            assert all(not observation["flat_verified"] for observation in account_records(path)
                if observation["kind"] == "engine_account_observation")
            # Correct the peer pagination contract and reopen the existing durable identity.
            peer.malformed_history = False
            second, _ = await asyncio.wait_for(run_account(replace(plan, duration_secs=5)), 16)
            assert second["status"] == "completed", second["failure"]
            assert second["shutdown_complete"] and peer.active == 0
    asyncio.run(scenario())


def test_actual_private_reconnect_resigns_recovers_and_deduplicates_engine_economics(tmp_path, monkeypatch):
    require_account_native()
    async def scenario():
        async with AccountPeer(private_reconnect=True) as peer:
            plan = plan_for(peer, tmp_path, monkeypatch, duration=7)
            summary, path = await asyncio.wait_for(run_account(plan), 18)
            assert summary["status"] == "completed", summary["failure"]
            assert summary["shutdown_complete"] and peer.active == 0
            assert peer.private_connections >= 2
            assert peer.private_verified[-1] > peer.private_verified[0]
            health = [record["health"]["account"] for record in account_records(path)
                if record["kind"] == "native_health"]
            assert any(record["connection_epoch"] >= 1 for record in health if record["connection_epoch"] is not None)
            assert any(record["recovery_count"] >= 2 for record in health)
            assert any("PrivateReconnectGap" in record["evidence_gaps"] for record in health)
            assert all(not record["private_subscription_confirmed"] for record in health)
            orders = [order for record in account_records(path) if record["kind"] == "engine_account_observation"
                for order in record["orders"] if order["status"] == "FILLED"]
            assert orders and orders[-1]["trade_ids"] == ["77", "78"]
            assert orders[-1]["filled_quantity"] == "0.00002"
            assert orders[-1]["commissions"] == ["-0.00000200 USDC"]
            assert summary["native_health"]["account"]["pending_fills"] == 2
            assert not summary["flat_verified"] and not summary["durable_economic_acknowledgement"]
            assert all(method == "GET" for method, _, _ in peer.requests)
    asyncio.run(scenario())


def test_actual_conflicting_fill_after_reconnect_is_failed_without_duplicate_economics(tmp_path, monkeypatch):
    require_account_native()
    async def scenario():
        async with AccountPeer(private_reconnect=True, conflicting_reconnect=True) as peer:
            plan = plan_for(peer, tmp_path, monkeypatch, duration=7)
            summary, path = await asyncio.wait_for(run_account(plan), 18)
            assert summary["status"] == "failed"
            assert summary["failure"] == "account_private_event_failed"
            assert summary["native_health_before_stop"]["account"]["parse_failures"] > 0
            assert summary["shutdown_complete"] and peer.active == 0
            assert summary["native_health"]["account"]["pending_fills"] == 2
            assert not summary["flat_verified"] and not summary["durable_economic_acknowledgement"]
            orders = [order for record in account_records(path) if record["kind"] == "engine_account_observation"
                for order in record["orders"] if order["status"] == "FILLED"]
            assert orders[-1]["trade_ids"] == ["77", "78"]
            assert orders[-1]["commissions"] == ["-0.00000200 USDC"]
    asyncio.run(scenario())


def test_actual_different_account_subaccount_scope_has_no_old_engine_economics(tmp_path, monkeypatch):
    require_account_native()
    async def scenario():
        async with AccountPeer() as peer:
            first_plan = plan_for(peer, tmp_path, monkeypatch, duration=5)
            first, _ = await asyncio.wait_for(run_account(first_plan), 16)
            assert first["status"] == "completed"
            peer.emit_orders = False
            peer.balance_free = "222"
# A different public synthetic API key represents the second owned peer scope.
            peer.auth_key = Ed25519PrivateKey.from_private_bytes(bytes([8]) * 32)
            peer.public_key = base64.b64encode(peer.auth_key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()
            data = private_document()
            data.update(environment="loopback", base_url_http=peer.http_url, base_url_ws=peer.ws_url,
                        duration_secs=5, request_timeout_secs=5)
            data["account"].update(account_id="BACKPACK-OTHER-SYNTHETIC", venue_account="other-synthetic-owner", subaccount="1")
            second_plan = parse_plan(data, tmp_path)
            monkeypatch.setenv(second_plan.account.credential_env, base64.b64encode(bytes([8]) * 32).decode())
            assert second_plan.namespace != first_plan.namespace
            second, path = await asyncio.wait_for(run_account(second_plan), 16)
            assert second["status"] == "completed", second["failure"]
            observations = [record for record in account_records(path) if record["kind"] == "engine_account_observation"]
            assert any(any(balance["free"] == "222.00000000 USDC" for balance in observation["wallet_trading_balances"])
                for observation in observations)
            assert all(not observation["orders"] and not observation["positions"] for observation in observations)
            assert all(not observation["flat_verified"] for observation in observations)
            assert second["native_health"]["account"]["pending_fills"] == 0
            assert (first_plan.journal_dir / "identity.lock").is_file()
            assert (second_plan.journal_dir / "identity.lock").is_file()
            assert peer.active == 0 and second["shutdown_complete"]
    asyncio.run(scenario())

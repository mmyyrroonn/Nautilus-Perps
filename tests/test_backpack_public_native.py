"""Actual installed-wheel public LiveNode acceptance against owned numeric loopback peers."""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.server import serve

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_config import parse_plan
from backpack_public import run_public
from test_backpack_config import document

SYMBOL = "BTC_USDC_PERP"
MARKET = json.loads((Path(__file__).parent / "fixtures/backpack/btc_market.json").read_text())


def require_native():
    adapter = pytest.importorskip("nautilus_trader.adapters.backpack",
                                 reason="installed source-bound Backpack candidate wheel is required")
    assert hasattr(adapter, "BackpackDataClientFactory")
    assert hasattr(adapter.BackpackDataClientConfig, "telemetry_snapshot_json")


class PublicPeer:
    def __init__(self, *, reconnect=False, empty_metadata=False):
        self.reconnect = reconnect
        self.empty_metadata = empty_metadata
        self.requests = []
        self.commands = []
        self.connections = 0
        self.active = 0
        self.depth_reads = 0
        self.ready = asyncio.Event()
        self.workers = set()

    async def __aenter__(self):
        self.http = await asyncio.start_server(self.request, "127.0.0.1", 0)
        self.ws = await serve(self.stream, "127.0.0.1", 0, max_size=65_536)
        self.http_url = f"http://127.0.0.1:{self.http.sockets[0].getsockname()[1]}"
        self.ws_url = f"ws://127.0.0.1:{self.ws.sockets[0].getsockname()[1]}"
        return self

    async def __aexit__(self, *args):
        self.ws.close()
        await self.ws.wait_closed()
        self.http.close()
        await self.http.wait_closed()
        for worker in list(self.workers):
            worker.cancel()
        await asyncio.gather(*list(self.workers), return_exceptions=True)

    async def request(self, reader, writer):
        self.workers.add(asyncio.current_task())
        try:
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            lines = raw.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            self.requests.append((method, target, {k.lower(): v for k, v in headers.items()}))
            path = urlsplit(target).path
            if method != "GET":
                status, body = "405 Method Not Allowed", {}
            elif path == "/api/v1/markets":
                status, body = "200 OK", [] if self.empty_metadata else [MARKET]
            elif path == "/api/v1/depth":
                self.depth_reads += 1
                sequence = "102" if self.reconnect and self.connections == 1 and self.depth_reads >= 2 else "100"
                await asyncio.sleep(0.1)
                status, body = "200 OK", {"lastUpdateId": sequence, "timestamp": time.time_ns() // 1000,
                    "bids": [["100.0", "1.00000"]], "asks": [["101.0", "2.00000"]]}
            else:
                status, body = "404 Not Found", {}
            payload = json.dumps(body).encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.workers.discard(asyncio.current_task())

    async def stream(self, ws):
        self.connections += 1
        connection = self.connections
        self.active += 1
        desired = set()
        producer = None
        try:
            async for raw in ws:
                command = json.loads(raw)
                self.commands.append(command)
                if command.get("method") == "SUBSCRIBE":
                    desired.update(command["params"])
                if len(desired) == 4 and producer is None:
                    producer = asyncio.create_task(self.send_frames(ws, connection))
        finally:
            self.active -= 1
            if producer is not None:
                producer.cancel()
                await asyncio.gather(producer, return_exceptions=True)

    async def send_frames(self, ws, connection):
        async def send(channel, fields):
            micros = time.time_ns() // 1000
            await ws.send(json.dumps({"stream": f"{channel}.{SYMBOL}", "data":
                {"e": channel, "s": SYMBOL, "E": micros, "T": micros - 1000, **fields}}))
        await send("bookTicker", {"u": connection, "b": "100.0", "B": "1.00000",
                                  "a": "101.0", "A": "2.00000"})
        await send("trade", {"t": connection, "p": "100.1", "q": "0.10000",
                             "b": "1", "a": "2", "m": True})
        await send("markPrice", {"p": "100.2", "i": "100.3", "f": "1.70",
                                  "n": int(time.time() * 1000) + 3600000})
        await send("depth", {"U": 101, "u": 101, "b": [["100.0", "1.10000"]], "a": []})
        self.ready.set()
        if self.reconnect and connection == 1:
            # Idle BBO must become stale despite a still-live socket; then actual socket replacement.
            await asyncio.sleep(0.5)
            await send("depth", {"U": 103, "u": 103, "b": [], "a": []})
            await asyncio.sleep(0.2)
            await ws.close()

    def assert_public_only(self):
        assert self.requests
        assert all(method == "GET" and urlsplit(path).path in {"/api/v1/markets", "/api/v1/depth"}
                   for method, path, _ in self.requests)
        assert all(not any(name.startswith("x-") for name in headers)
                   and "authorization" not in headers for _, _, headers in self.requests)
        assert self.commands
        assert all(command["method"] in {"SUBSCRIBE", "UNSUBSCRIBE"} for command in self.commands)
        assert all(topic.split(".")[0] in {"bookTicker", "trade", "markPrice", "depth"}
                   and topic.endswith(SYMBOL) for command in self.commands for topic in command["params"])


def plan_for(peer, tmp_path, *, duration=3):
    value = document()
    value.update(environment="loopback", base_url_http=peer.http_url, base_url_ws=peer.ws_url,
                 duration_secs=duration, request_timeout_secs=1, stale_after_ms=100)
    value["economics"][SYMBOL]["source"] = "Synthetic"
    return parse_plan(value, tmp_path)


def records(path):
    return [json.loads(line) for line in (path.parent / "events.jsonl").read_text().splitlines()]


def test_installed_native_messages_stale_gap_reconnect_and_bounded_stop(tmp_path):
    require_native()
    async def scenario():
        async with PublicPeer(reconnect=True) as peer:
            plan = plan_for(peer, tmp_path)
            summary, path = await asyncio.wait_for(run_public(plan), 15)
            assert summary["status"] == "completed", summary
            assert summary["shutdown_complete"] and summary["actor_started"] and summary["actor_stopped"]
            assert summary["native"]["status"] == "unverified_local_development"
            assert summary["execution_ready"] is False
            assert summary["private_client_registered"] is False
            rows = records(path)
            kinds = {row["kind"] for row in rows}
            assert {"instrument", "quote", "trade", "mark", "book_deltas", "native_health"} <= kinds
            assert next(i for i, row in enumerate(rows) if row["kind"] == "instrument") < next(
                i for i, row in enumerate(rows) if row["kind"] == "quote")
            assert all(type(row["bid_price"]) is str for row in rows if row["kind"] == "quote")
            assert all(row["complete_book_claimed"] is False for row in rows if row["kind"] == "book_deltas")
            health = [row["health"] for row in rows if row["kind"] == "native_health"]
            first_fresh = next(i for i, h in enumerate(health) if h["quotes_fresh"].get(SYMBOL) is True)
            assert any(h["connected"] and h["quotes_fresh"].get(SYMBOL) is False for h in health[first_fresh + 1:])
            assert any(row["kind"] == "book_deltas" and row["sequence"] == "102" for row in rows)
            assert peer.depth_reads >= 3
            assert any(h["connection_epoch"] >= 1 for h in health)
            assert any(h["books_continuous"].get(SYMBOL) is True for h in health)
            assert all(h["subscription_acknowledgements_verified"] is False for h in health)
            assert not summary["native_health"]["connected"]
            assert peer.connections >= 2 and peer.active == 0
            peer.assert_public_only()
            assert sum(p.stat().st_size for p in path.parent.iterdir()) <= plan.max_report_bytes
            assert not plan.journal_dir.exists()
    asyncio.run(scenario())


def test_installed_native_external_cancellation_writes_evidence_and_closes_transport(tmp_path):
    require_native()
    async def scenario():
        async with PublicPeer() as peer:
            plan = plan_for(peer, tmp_path, duration=10)
            task = asyncio.create_task(run_public(plan))
            await asyncio.wait_for(peer.ready.wait(), 4)
            await asyncio.sleep(0.2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 12)
            summaries = list(plan.output_dir.glob("*/summary.json"))
            assert len(summaries) == 1
            summary = json.loads(summaries[0].read_text())
            assert summary["failure"] == "cancelled"
            assert summary["stop_reason"] == "cancelled"
            assert summary["shutdown_complete"]
            assert peer.active == 0
            assert summary["native_health"]["connected"] is False
            peer.assert_public_only()
    asyncio.run(scenario())


def test_installed_native_failed_startup_publishes_failure_without_private_requests(tmp_path):
    require_native()
    async def scenario():
        async with PublicPeer(empty_metadata=True) as peer:
            plan = plan_for(peer, tmp_path)
            summary, path = await asyncio.wait_for(run_public(plan), 12)
            assert summary["status"] == "failed"
            assert summary["failure"] == "native_runtime_failed"
            assert summary["actor_started"] is False
            assert summary["native_health"]["metadata_ready"] is False
            assert peer.connections == 0 and peer.active == 0
            assert all(method == "GET" and urlsplit(target).path == "/api/v1/markets"
                       for method, target, _ in peer.requests)
            assert path.is_file()
    asyncio.run(scenario())

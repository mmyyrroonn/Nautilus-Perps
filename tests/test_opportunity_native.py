"""Actual scanner actor lifecycle on an installed wheel and public loopback peer."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
from pathlib import Path
import sys
import threading
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opportunity_runtime as runtime
from opportunity_scan import load_plan


def test_scanner_actual_node_subscribes_only_depth_refreshes_and_stops(tmp_path, monkeypatch):
    native = pytest.importorskip("nautilus_trader.adapters.backpack")
    pytest.importorskip("websockets")
    from test_backpack_public_native import PublicPeer, SYMBOL

    class DepthPeer(PublicPeer):
        async def stream(self, ws):
            self.connections += 1
            self.active += 1
            producer = None
            try:
                async for raw in ws:
                    command = json.loads(raw)
                    self.commands.append(command)
                    if command.get("method") == "SUBSCRIBE" and producer is None:
                        producer = asyncio.create_task(self.send_frames(ws, self.connections))
            finally:
                self.active -= 1
                if producer is not None:
                    producer.cancel()
                    await asyncio.gather(producer, return_exceptions=True)

        async def send_frames(self, ws, connection):
            self.ready.set()
            for sequence in range(101, 131):
                micros = time.time_ns() // 1000
                await ws.send(json.dumps({"stream": f"depth.{SYMBOL}", "data": {
                    "e": "depth", "s": SYMBOL, "E": micros, "T": micros-1000,
                    "U": sequence, "u": sequence, "b": [["100.0", "1.10000"]], "a": []}}))
                await asyncio.sleep(0.1)

    async def scenario():
        async with DepthPeer() as peer:
            original = load_plan(Path(__file__).resolve().parents[1] / "config/opportunity-scan.example.toml")
            market = next(m for m in original.markets if m.symbol == "BTC" and m.venue == "BACKPACK")
            # One configured leg isolates the actual actor's lifecycle. Paired qualification
            # is covered by the native delta tests; no execution client is registered here.
            plan = replace(original, markets=(market,), output_path=tmp_path / "events" / "events.jsonl",
                           duration_secs=2, refresh_ms=100, connection_timeout_secs=3)
            config = native.BackpackDataClientConfig([SYMBOL], {
                SYMBOL: native.BackpackInstrumentEconomics(**market.backpack_economics)},
                base_url_http=peer.http_url, base_url_ws=peer.ws_url, http_timeout_secs=1,
                ws_connect_timeout_secs=1, shutdown_timeout_secs=2)
            monkeypatch.setattr(runtime, "data_clients", lambda p: [("BACKPACK", native.BackpackDataClientFactory(), config)])
            node, observer, state = runtime.build_node(plan)
            displays = []
            monkeypatch.setattr(state, "display", lambda: displays.append(len(state.books)))
            task = asyncio.ensure_future(node.run_async())
            timer = threading.Timer(1.5, node.handle().stop)
            timer.daemon = True
            timer.start()
            try:
                await asyncio.wait_for(peer.ready.wait(), 5)
                await asyncio.wait_for(asyncio.shield(task), 8)
            finally:
                timer.cancel()
                node.handle().stop()
                await asyncio.wait_for(task, 8)
            assert observer.failure is None, observer.failure
            assert observer.started and not observer.running
            assert displays and max(displays) == 1
            assert len(state.books) == 1 and state.saved == 0 and not state.current
            assert peer.active == 0
            assert list(tmp_path.iterdir()) == []
            peer.assert_public_only()
            assert all(topic == f"depth.{SYMBOL}" for command in peer.commands for topic in command["params"])
    asyncio.run(scenario())

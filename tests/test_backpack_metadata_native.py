"""Installed engine metadata eligibility and old-generation rule-change refusal."""
from __future__ import annotations

import asyncio
from copy import deepcopy
import json
from pathlib import Path
import sys
import time
from urllib.parse import urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import backpack_loopback
from backpack_loopback_config import parse_loopback_plan
from test_backpack_account_native import SEED
from test_backpack_loopback import loopback_document
from test_backpack_loopback_native import OrderPeer, PrefixReader, event_records, require_loopback_native
from test_backpack_public_native import MARKET, SYMBOL


class MetadataPeer(OrderPeer):
    def __init__(self, mode):
        super().__init__()
        self.mode = mode
        self.changed = False
        self.change_requested = asyncio.Event()
        self.metadata_documents = []

    async def request(self, reader, writer):
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        method, target, _ = raw.decode("ascii").split("\r\n", 1)[0].split(" ")
        if urlsplit(target).path != "/api/v1/markets":
            return await super().request(PrefixReader(reader, raw), writer)
        assert method == "GET"
        worker = asyncio.current_task()
        self.workers.add(worker)
        try:
            market = deepcopy(MARKET)
            if self.mode == "disabled":
                market["orderBookState"] = "Closed"
            elif self.mode == "hidden":
                market["visible"] = False
            elif self.changed:
                market["filters"]["price"].update(tickSize="0.5", minPrice="0.5")
            self.metadata_documents.append(market)
            self.requests.append((method, "/api/v1/markets", {}))
            payload = json.dumps([market]).encode()
            writer.write(f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.workers.discard(worker)

    async def send_frames(self, ws, connection):
        async def send(channel, fields):
            micros = time.time_ns() // 1000
            await ws.send(json.dumps({"stream": f"{channel}.{SYMBOL}", "data": {
                "e": channel, "s": SYMBOL, "E": micros, "T": micros - 1000, **fields}}))
        await send("bookTicker", {"u": connection, "b": "100.0", "B": "1.00000", "a": "101.0", "A": "2.00000"})
        await send("trade", {"t": connection, "p": "100.5", "q": "0.10000", "b": "1", "a": "2", "m": True})
        await send("markPrice", {"p": "100.5", "i": "100.5", "f": "1.70", "n": int(time.time() * 1000) + 3600000})
        await send("depth", {"U": 101, "u": 101, "b": [["100.0", "1.10000"]], "a": []})
        self.ready.set()
        if connection == 1:
            await self.change_requested.wait()
            self.changed = True
            await ws.close()
            return
        sequence = connection + 1
        while True:
            await asyncio.sleep(0.1)
            await send("bookTicker", {"u": sequence, "b": "100.0", "B": "1.00000", "a": "101.0", "A": "2.00000"})
            sequence += 1


def metadata_plan(peer, tmp_path, monkeypatch):
    document = loopback_document()
    document["session"].update(base_url_http=peer.http_url, base_url_ws=peer.ws_url,
        duration_secs=6, request_timeout_secs=1, stale_after_ms=1000,
        output_dir="reports", state_dir="state")
    document["authority"].update(valid_for_ms=6000, max_account_age_ms=6000,
        max_market_age_ms=6000)
    document["scenario"].update(cancel_after_fill=False, stale_probe=False)
    plan = parse_loopback_plan(document, tmp_path)
    monkeypatch.setenv(plan.account.credential_env, SEED)
    return plan


@pytest.mark.parametrize("mode", ["disabled", "hidden"])
def test_installed_ineligible_metadata_refuses_before_any_order(tmp_path, monkeypatch, mode):
    require_loopback_native()

    async def scenario():
        async with MetadataPeer(mode) as peer:
            plan = metadata_plan(peer, tmp_path, monkeypatch)
            started = time.monotonic()
            summary, path = await asyncio.wait_for(backpack_loopback.run_loopback(plan), 12)
            assert summary["status"] == "failed"
            assert summary["failure"] in {"startup_failed", "native_runtime_failed", "account_snapshot_unavailable"}
            assert time.monotonic() - started < plan.session.duration_secs
            assert peer.metadata_documents and not peer.posts and not peer.deletes
            assert peer.active == 0
            rows = event_records(path)
            assert not any(row["kind"] in {"scenario_submit", "order_submitted", "order_accepted"} for row in rows)
            if summary["native_health"] is not None:
                assert not summary["native_health"]["public"]["metadata_ready"]
            assert not summary["execution_ready"] and not summary["account_identity_verified"]
            assert all(market["orderBookState"] == "Closed" if mode == "disabled" else not market["visible"]
                       for market in peer.metadata_documents)
    asyncio.run(scenario())


def old_generation_strategy(peer):
    def factory(plan, evidence, control):
        from nautilus_trader.adapters.backpack import BackpackLoopbackAccountFacts
        from nautilus_trader.model import InstrumentId, OrderSide, Price, Quantity, TimeInForce
        from nautilus_trader.trading import Strategy

        class OldGenerationStrategy(Strategy):
            def __init__(self):
                super().__init__()
                self.started = False
                self.session = None
                self.old_epoch = None
                self.order = None
                self.instrument = InstrumentId.from_str(f"{SYMBOL}.BACKPACK")

            def on_start(self):
                self.started = True

            def on_stop(self):
                self.started = False

            def advance(self, health):
                if not self.started or evidence.actor_failure or self.order is not None:
                    return
                public = health["public"]
                if (not public["connected"] or not public["metadata_ready"]
                        or not public["quotes_fresh"].get(SYMBOL, False)
                        or not health["account"]["rest_snapshot_observed"]):
                    return
                if self.session is None:
                    self.session = control.begin_session()
                    control.accept_account(self.session, BackpackLoopbackAccountFacts(
                        observed_at_ms=time.time_ns() // 1_000_000, **plan.facts.document()))
                    control.refresh_market(self.session, str(self.instrument))
                    self.old_epoch = public["connection_epoch"]
                    evidence.record("admitted_original_metadata", {"connection_epoch": self.old_epoch,
                        "price_increment": str(self.cache.instrument(self.instrument).price_increment)})
                    peer.change_requested.set()
                    return
                if public["connection_epoch"] <= self.old_epoch:
                    return
                increment = str(self.cache.instrument(self.instrument).price_increment)
                if increment != "0.5":
                    return
                with pytest.raises(RuntimeError):
                    control.refresh_market(self.session, str(self.instrument))
                evidence.record("old_session_market_refresh_refused", {
                    "old_connection_epoch": self.old_epoch, "new_connection_epoch": public["connection_epoch"],
                    "price_increment": increment, "quotes_fresh": True})
                self.order = self.order_factory.limit(self.instrument, OrderSide.BUY,
                    Quantity.from_str("0.00002"), Price.from_str("100.1"),
                    time_in_force=TimeInForce.GTC, post_only=False, reduce_only=False)
                self.submit_order(self.order)

            def on_order_denied(self, event):
                evidence.record("old_generation_order_denied", {"reason": str(event.reason),
                    "client_order_id": str(event.client_order_id)})
                evidence.extra_summary["scenario_steps_observed"] = True

            def on_order_accepted(self, event):
                evidence.actor_failure = "old_generation_order_was_accepted"
                evidence.handle.stop()

        return OldGenerationStrategy()
    return factory


def test_installed_rule_refresh_reaches_engine_and_old_generation_order_is_denied(tmp_path, monkeypatch):
    require_loopback_native()

    async def scenario():
        async with MetadataPeer("rules") as peer:
            monkeypatch.setattr(backpack_loopback, "_strategy", old_generation_strategy(peer))
            summary, path = await asyncio.wait_for(backpack_loopback.run_loopback(
                metadata_plan(peer, tmp_path, monkeypatch)), 18)
            assert summary["status"] == "completed", {"failure": summary["failure"], "native": summary["native_health"]}
            assert summary["scenario_steps_observed"] and summary["shutdown_complete"]
            assert peer.changed and peer.connections >= 2 and peer.active == 0
            assert not peer.posts and not peer.deletes
            assert {market["filters"]["price"]["tickSize"] for market in peer.metadata_documents} == {"0.1", "0.5"}
            rows = event_records(path)
            metadata = [row["price_increment"] for row in rows if row["kind"] == "instrument"]
            assert "0.1" in metadata and "0.5" in metadata
            refusal = next(row for row in rows if row["kind"] == "old_session_market_refresh_refused")
            assert refusal["new_connection_epoch"] > refusal["old_connection_epoch"]
            assert refusal["quotes_fresh"] and refusal["price_increment"] == "0.5"
            denials = [row for row in rows if row["kind"] == "old_generation_order_denied"]
            assert len(denials) == 1 and denials[0]["reason"]
            assert not any(method != "GET" for method, _, _ in peer.requests)
            assert not summary["account_identity_verified"] and not summary["execution_ready"]
    asyncio.run(scenario())

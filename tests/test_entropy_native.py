"""Actual scanner factories/cache/L2 on owned numeric-loopback public peers.

Synthetic prices and metadata exercise official wire schemas, not venue behavior.
Only public data clients are registered. No environment credentials are loaded.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys
import threading
import time
from urllib.parse import parse_qs, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import opportunity_runtime as runtime
from opportunity_scan import load_plan, parse_plan
from opportunity_universe import render_toml

FIXTURE = json.loads((Path(__file__).parent / "fixtures/entropy-public/synthetic-metadata.json").read_text())
IO_ID = "io:SNDK-USD-PERP.HYPERLIQUID"
XYZ_ID = "xyz:SNDK-USD-PERP.HYPERLIQUID"
ASTER_ID = "SNDKUSD1-PERP.ASTER"
PAIR = ("SNDK", "ENTROPY", "ASTER")


class EquityPeer:
    """Bounded HTTP and WS servers with strictly public request allowlists."""

    def __init__(self, venue):
        self.venue = venue
        self.mode = "normal"
        self.requests = []
        self.commands = []
        self.errors = []
        self.workers = set()
        self.connections = 0
        self.active = 0
        self.sequence = 100
        self.frames = 0
        self.stopping = False
        self.sessions = set()
        self.intentional_closes = set()

    async def __aenter__(self):
        from websockets.asyncio.server import serve
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
        workers = list(self.workers)
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)

    def levels(self):
        if self.venue == "HL":
            bids = [["99.98", "0.6"], ["99.97", "3"]]
            asks = [["100.00", "0.6"], ["100.02", "3"]]
        else:
            bids = [["101.00", "0.5"], ["100.90", "3"]]
            asks = [["101.10", "0.5"], ["101.20", "3"]]
        if self.mode == "empty":
            return [], []
        if self.mode == "shallow":
            return [bids[0][:1] + ["0.02"]], [asks[0][:1] + ["0.02"]]
        return bids, asks

    def stamp(self):
        now = time.time_ns() // 1_000_000
        return now - (5000 if self.mode == "stale" else 500 if self.mode == "skew" else 0)

    def hl_book(self, coin):
        return {"coin": coin, "time": self.stamp(), "levels": [
            [{"px": px, "sz": sz, "n": 1} for px, sz in rows] for rows in self.levels()]}

    def response(self, method, target, body):
        path = urlsplit(target).path
        if self.venue == "HL":
            assert method == "POST" and path == "/info", (method, path)
            kind = body["type"]
            assert kind in {"meta", "spotMeta", "allPerpMetas", "perpDexs", "outcomeMeta", "metaAndAssetCtxs", "l2Book"}
            if kind == "spotMeta":
                return FIXTURE["spot_meta"]
            if kind == "allPerpMetas":
                return FIXTURE["all_perp_metas"]
            if kind == "perpDexs":
                return FIXTURE["perp_dexs"]
            if kind == "outcomeMeta":
                return {"outcomes": [], "questions": []}
            if kind == "l2Book":
                assert body["coin"] in {"io:SNDK", "xyz:SNDK"}
                return self.hl_book(body["coin"])
            meta = FIXTURE["all_perp_metas"][{"": 0, "xyz": 1, "io": 3}[body.get("dex", "")]]
            return meta if kind == "meta" else [meta, [{} for _ in meta["universe"]]]
        assert method == "GET" and path in {"/fapi/v1/exchangeInfo", "/fapi/v1/time", "/fapi/v1/depth"}
        if path == "/fapi/v1/time":
            return {"serverTime": self.stamp()}
        if path == "/fapi/v1/exchangeInfo":
            return {**FIXTURE["aster"], "serverTime": self.stamp()}
        assert parse_qs(urlsplit(target).query)["symbol"] == ["SNDKUSD1"]
        bids, asks = self.levels()
        return {"lastUpdateId": self.sequence, "E": self.stamp(), "T": self.stamp(), "bids": bids, "asks": asks}

    async def request(self, reader, writer):
        worker = asyncio.current_task()
        self.workers.add(worker)
        try:
            header = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 2)
            assert len(header) <= 16_384
            lines = header.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ")
            headers = {key.lower(): value.strip() for key, value in
                       (line.split(":", 1) for line in lines[1:] if ":" in line)}
            assert "authorization" not in headers, tuple(headers)
            assert not any(key in headers for key in ("x-api-key", "x-signature", "x-mbx-apikey")), tuple(headers)
            assert headers["host"].startswith("127.0.0.1:")
            length = int(headers.get("content-length", "0"))
            assert 0 <= length <= 8192
            payload = await asyncio.wait_for(reader.readexactly(length), 2) if length else b""
            body = json.loads(payload) if payload else None
            self.requests.append((method, target, body))
            result = self.response(method, target, body)
            status = "200 OK"
        except Exception as exc:
            self.errors.append(repr(exc))
            print("PUBLIC_LOOPBACK_REQUEST_FAILED", self.venue, repr(exc))
            status, result = "400 Bad Request", {"error": type(exc).__name__}
        try:
            raw = json.dumps(result).encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(raw)}\r\nConnection: close\r\n\r\n".encode() + raw)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            self.workers.discard(worker)

    async def stream(self, ws):
        self.connections += 1
        self.active += 1
        self.sessions.add(ws)
        subscriptions = set()
        producer = asyncio.create_task(self.produce(ws, subscriptions))
        try:
            async for raw in ws:
                command = json.loads(raw)
                self.commands.append(command)
                if self.venue == "HL":
                    assert command.get("method") in {"subscribe", "unsubscribe", "ping"}
                    if command["method"] == "ping":
                        await ws.send(json.dumps({"channel": "pong"}))
                        continue
                    sub = command["subscription"]
                    assert sub["type"] == "l2Book" and sub["coin"] in {"io:SNDK", "xyz:SNDK"}
                    if command["method"] == "subscribe":
                        subscriptions.add(sub["coin"])
                    else:
                        subscriptions.discard(sub["coin"])
                    await ws.send(json.dumps({"channel": "subscriptionResponse", "data": command}))
                else:
                    assert command["method"] in {"SUBSCRIBE", "UNSUBSCRIBE"}
                    assert all(topic == "sndkusd1@depth@0ms" for topic in command["params"])
                    if command["method"] == "SUBSCRIBE":
                        subscriptions.update(command["params"])
                    else:
                        subscriptions.difference_update(command["params"])
                    await ws.send(json.dumps({"result": None, "id": command["id"]}))
        except Exception as exc:
            from websockets.exceptions import ConnectionClosed
            if not ((self.stopping or ws in self.intentional_closes) and isinstance(exc, ConnectionClosed)):
                self.errors.append(repr(exc))
        finally:
            producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)
            self.active -= 1
            self.sessions.discard(ws)

    async def disconnect_once(self):
        assert self.sessions and not self.intentional_closes
        sessions = tuple(self.sessions)
        self.intentional_closes.update(sessions)
        await asyncio.wait_for(asyncio.gather(*(ws.close(code=1012, reason="owned synthetic reconnect")
                                               for ws in sessions)), 3)

    async def produce(self, ws, subscriptions):
        while True:
            await asyncio.sleep(0.08)
            if not subscriptions or self.mode == "pause":
                continue
            if self.venue == "HL":
                for coin in sorted(subscriptions):
                    await ws.send(json.dumps({"channel": "l2Book", "data": self.hl_book(coin)}))
            else:
                bids, asks = self.levels()
                self.sequence += 1
                # Explicit zeros remove old levels during empty/shallow transitions.
                zeros = ([['101.00', '0'], ['100.90', '0']], [['101.10', '0'], ['101.20', '0']])
                frame = {"e": "depthUpdate", "E": self.stamp(), "T": self.stamp(), "s": "SNDKUSD1",
                         "U": self.sequence, "u": self.sequence, "pu": self.sequence - 1,
                         "b": list({px: sz for px, sz in zeros[0] + bids}.items()),
                         "a": list({px: sz for px, sz in zeros[1] + asks}.items())}
                await ws.send(json.dumps(frame))
            self.frames += 1

    def assert_public_only(self):
        assert self.requests and self.commands and self.frames
        assert not self.errors, self.errors
        assert self.active == 0
        assert len(self.requests) < 100 and len(self.commands) < 100


def plan_for(tmp_path):
    legs = [("ENTROPY", IO_ID, "0.9", "io:SNDK", "USD", "USDC", "0.0001"),
            ("HL", XYZ_ID, "0.9", "xyz:SNDK", "USD", "USDC", "0.001"),
            ("ASTER", ASTER_ID, "1.25", "SNDKUSD1", "USD1", "USD1", "0.01")]
    markets = [{"symbol": "SNDK", "venue": venue, "instrument_id": instrument_id,
        "quote_to_usd": "1", "canonical_multiplier": "1",
        "valuation_source": "Synthetic USD parity and explicit issuer/native-unit assumption",
        "taker_fee_bps": fee, "fee_source": "Synthetic configured public-rate assumption",
        "expected_instrument": {"raw_symbol": raw, "quote_currency": quote,
            "settlement_currency": settlement, "size_increment": step, "multiplier": "1"}}
        for venue, instrument_id, fee, raw, quote, settlement, step in legs]
    document = {"schema_version": 1, "markets": markets,
        "scan": {"target_notional": "100", "reserve_bps": "0", "max_age_ms": 1000,
                 "max_receive_age_ms": 1000, "max_skew_ms": 150},
        "recording": {"path": "events.jsonl", "enabled": True},
        "runtime": {"depth_levels": 20, "refresh_ms": 100, "duration_secs": 20,
            "connection_timeout_secs": 3, "aster_snapshot_depth": 20, "aster_subscription_interval_ms": 1}}
    expected = parse_plan(document, tmp_path)
    config_path = tmp_path / "scanner.toml"
    config_path.write_text(render_toml(document), encoding="utf-8")
    plan = load_plan(config_path)
    assert plan == expected
    return plan


def loopback_clients(plan, hl_peer, aster_peer):
    """Keep runtime's actual factories/config options; change transport URLs only."""
    from nautilus_trader.adapters.hyperliquid import HyperliquidDataClientConfig
    from nautilus_trader.adapters.aster import AsterDataClientConfig
    clients = runtime.data_clients(plan)
    assert [name for name, _, _ in clients] == ["HYPERLIQUID", "ASTER"]
    patched = []
    for name, factory, config in clients:
        if name == "HYPERLIQUID":
            assert not config.has_proxy_url
            fields = ("environment", "http_timeout_secs", "ws_timeout_secs", "update_instruments_interval_mins",
                "transport_backend", "stale_stream_receive_timeout_secs", "stream_health_check_interval_secs",
                "stale_stream_warning_cooldown_secs", "stale_stream_recovery_enabled",
                "stale_stream_recovery_cooldown_secs", "stale_stream_max_targeted_resubscribes")
            replacement = HyperliquidDataClientConfig(**{field: getattr(config, field) for field in fields},
                base_url_http=hl_peer.http_url + "/info", base_url_ws=hl_peer.ws_url)
        else:
            assert config.proxy_url is None
            fields = ("environment", "instrument_provider", "instrument_refresh_interval_secs", "instrument_status_poll_secs", "venue")
            replacement = AsterDataClientConfig(**{field: getattr(config, field) for field in fields},
                base_url_http=aster_peer.http_url, base_url_ws=aster_peer.ws_url)
        patched.append((name, factory, replacement))
    return patched


async def until(predicate, *, timeout=5):
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "actual native scanner condition timed out"
        await asyncio.sleep(0.03)


def assert_instruments(observer, state):
    from nautilus_trader.model import InstrumentId
    io = observer.cache.instrument(InstrumentId.from_str(IO_ID))
    xyz = observer.cache.instrument(InstrumentId.from_str(XYZ_ID))
    gpro = observer.cache.instrument(InstrumentId.from_str("io:GPRO-USD-PERP.HYPERLIQUID"))
    aster = observer.cache.instrument(InstrumentId.from_str(ASTER_ID))
    assert str(io.raw_symbol) == "io:SNDK" and str(xyz.raw_symbol) == "xyz:SNDK"
    assert io.price_precision == 2 and gpro.price_precision == 5
    assert Decimal(str(io.size_increment)) == Decimal("0.0001")
    assert Decimal(str(gpro.size_increment)) == Decimal("0.1")
    assert Decimal(str(xyz.size_increment)) == Decimal("0.001")
    assert Decimal(str(aster.size_increment)) == Decimal("0.01")
    assert str(io.quote_currency) == "USD" and str(io.settlement_currency) == "USDC"
    assert str(aster.quote_currency) == "USD1" and str(aster.settlement_currency) == "USD1"
    assert all(metadata.multiplier == 1 for metadata in state.metadata.values())
    for instrument_id, increment in ((IO_ID, Decimal("0.0001")), (XYZ_ID, Decimal("0.001"))):
        instrument = observer.cache.instrument(InstrumentId.from_str(instrument_id))
        assert instrument.price_precision == 6 - instrument.size_precision
        for price, quantity in state.books[instrument_id].bids + state.books[instrument_id].asks:
            decimals = max(0, -price.normalize().as_tuple().exponent)
            assert decimals <= instrument.price_precision
            assert price == price.to_integral_value() or len(price.normalize().as_tuple().digits) <= 5
            assert quantity % increment == 0


def assert_multilevel_event(path):
    events = [json.loads(line)["opportunity"] for line in path.read_text().splitlines()]
    event = next(row for row in events if (row["buy_venue"], row["sell_venue"]) == ("ENTROPY", "ASTER"))
    sizing = event["sizing"]
    assert sizing["base_quantity"] == sizing["buy_quantity"] == sizing["sell_quantity"] == "0.99"
    assert Decimal(sizing["common_base_step"]) == Decimal("0.01")
    assert Decimal(sizing["buy_notional"]) == Decimal("99.0078")
    assert Decimal(sizing["sell_notional"]) == Decimal("99.941")
    assert Decimal(sizing["buy_vwap"]) > Decimal("100")
    assert Decimal(sizing["sell_vwap"]) < Decimal("101")
    assert event["raw_buy_book"]["instrument_id"] == IO_ID
    assert event["raw_sell_book"]["instrument_id"] == ASTER_ID
    assert len(event["raw_buy_book"]["asks"]) == len(event["raw_sell_book"]["bids"]) == 2
    assert abs(event["buy_book"]["ts_event_ns"] - event["sell_book"]["ts_event_ns"]) <= 150_000_000
    assert all(event["evaluated_at_ns"] - event[side]["ts_event_ns"] <= 1_000_000_000
               for side in ("buy_book", "sell_book"))
    assert event["full_round_trip_profitability"] == "unknown"


def test_actual_shared_hyperliquid_aster_factory_cache_l2_vwap_and_recovery(tmp_path, monkeypatch):
    pytest.importorskip("nautilus_trader.adapters.hyperliquid")
    pytest.importorskip("nautilus_trader.adapters.aster")
    pytest.importorskip("websockets.asyncio.server")

    async def scenario():
        async with EquityPeer("HL") as hl, EquityPeer("ASTER") as aster:
            plan = plan_for(tmp_path)
            clients = loopback_clients(plan, hl, aster)
            monkeypatch.setattr(runtime, "data_clients", lambda _: clients)
            node, observer, state = runtime.build_node(plan)
            task = asyncio.ensure_future(node.run_async())
            timer = threading.Timer(plan.duration_secs, node.handle().stop)
            timer.daemon = True
            timer.start()
            try:
                await until(lambda: PAIR in state.current)
                assert observer.started and observer.failure is None
                assert len(state.books) == 3 and len(state.metadata) == 3
                assert_instruments(observer, state)
                assert_multilevel_event(plan.output_path)
                for mode in ("shallow", "empty", "stale", "skew"):
                    previous_frames = hl.frames
                    hl.mode = mode
                    await until(lambda: hl.frames >= previous_frames + 3 and PAIR not in state.current)
                    if mode == "shallow":
                        assert sum(quantity for _, quantity in state.books[IO_ID].asks) == Decimal("0.02")
                    elif mode == "empty":
                        assert IO_ID not in state.books or not state.books[IO_ID].asks
                    elif mode == "stale":
                        assert time.time_ns() - state.books[IO_ID].ts_event_ns > 4_000_000_000
                    else:
                        age = time.time_ns() - state.books[IO_ID].ts_event_ns
                        assert 400_000_000 < age < 1_000_000_000
                        assert abs(state.books[IO_ID].ts_event_ns - state.books[ASTER_ID].ts_event_ns) > 150_000_000
                    before = state.saved
                    await asyncio.sleep(0.22)
                    assert PAIR not in state.current and state.saved == before
                    hl.mode = "normal"
                    await until(lambda: PAIR in state.current)
                # A still-connected but silent socket must expire, then resume only on fresh L2.
                hl.mode = "pause"
                await until(lambda: IO_ID in state.books and
                    time.time_ns() - state.books[IO_ID].ts_event_ns > 1_000_000_000
                    and PAIR not in state.current, timeout=3)
                assert hl.active == 1
                before = state.saved
                await asyncio.sleep(0.22)
                assert state.saved == before
                hl.mode = "normal"
                await until(lambda: PAIR in state.current)
                # Physically replace the HL socket while Aster continues updating.
                hl.mode = "pause"
                previous_connections = hl.connections
                await hl.disconnect_once()
                await until(lambda: hl.connections > previous_connections, timeout=6)
                await until(lambda: IO_ID in state.books and
                    time.time_ns() - state.books[IO_ID].ts_event_ns > 1_000_000_000
                    and PAIR not in state.current, timeout=3)
                assert time.time_ns() - state.books[ASTER_ID].ts_event_ns < 500_000_000
                resumed_at_ns = time.time_ns()
                hl.mode = "normal"
                await until(lambda: PAIR in state.current and
                    state.books[IO_ID].ts_event_ns >= resumed_at_ns)
                assert hl.connections == previous_connections + 1
                summary = {"source": FIXTURE["source"], "actor_started": observer.started,
                    "registered_data_clients": [name for name, _, _ in clients], "books": len(state.books),
                    "native_metadata": len(state.metadata), "qualified_base_quantity": state.current[PAIR]["base_quantity"],
                    "hl_connections": hl.connections, "aster_connections": aster.connections,
                    "hl_frames": hl.frames, "aster_frames": aster.frames,
                    "public_request_counts": {"HL": len(hl.requests), "ASTER": len(aster.requests)},
                    "saved_pre_stop": state.saved, "transport_reconnect": True, "execution_registered": False}
            except Exception:
                print("NATIVE_LOOPBACK_DIAGNOSTICS", {
                    "failure": observer.failure, "books": list(state.books),
                    "hl_commands": hl.commands, "aster_commands": aster.commands,
                    "aster_requests": aster.requests, "peer_errors": (hl.errors, aster.errors)})
                raise
            finally:
                timer.cancel()
                hl.stopping = aster.stopping = True
                node.handle().stop()
                await asyncio.wait_for(task, 15)
            assert observer.failure is None, observer.failure
            assert observer.started and not observer.running and not state.current
            hl.assert_public_only()
            aster.assert_public_only()
            assert len(observer.subscribed) == 3
            assert not list(tmp_path.glob("*.csv"))
            final_rows = [json.loads(line) for line in plan.output_path.read_text(encoding="utf-8").splitlines()]
            assert len(final_rows) == state.saved
            summary.update(shutdown_complete=True, active_peer_sockets=hl.active + aster.active,
                           actor_stopped=not observer.running, saved=state.saved,
                           event_file_rows=len(final_rows))
            (tmp_path / "native-summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    asyncio.run(scenario())

"""The actual opportunity observer must feed the native production quote cache."""
from __future__ import annotations

import asyncio
from collections import deque
from decimal import Decimal
import json
from pathlib import Path
import sys
import time

import pytest

from backpack_production_peer import ProductionPeer, SEED, SYMBOLS, micros
from test_backpack_production_native import native_adapter

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


class QuoteCachePeer(ProductionPeer):
    """Keep signed account/depth traffic live while withholding selected quotes."""

    def __init__(self, missing):
        super().__init__()
        self.quote_modes = {symbol: "missing" if symbol in missing else "fresh" for symbol in SYMBOLS}
        self.public_sockets = set()
        self.public_connections = 0
        self.invalid_quote_frames = 0
        self.public_diagnostics = deque(maxlen=128)
        self.topics = set()
        self.sent_quotes = {symbol: deque(maxlen=256) for symbol in SYMBOLS}

    async def public_frames(self, ws, desired):
        self.public_sockets.add(ws)
        self.public_connections += 1
        connection = self.public_connections
        self.public_diagnostics.append({"event": "public_connected", "connection": connection})
        sequence = 100
        depth_sequences = {}
        observed_topics = set()
        try:
            while True:
                sequence += 1
                self.topics.update(desired)
                if set(desired) != observed_topics:
                    observed_topics = set(desired)
                    self.public_diagnostics.append({"event": "subscribed_topics", "connection": connection,
                        "topics": sorted(observed_topics)})
                for topic in tuple(desired):
                    channel, symbol = topic.split(".", 1)
                    at = micros()
                    if channel == "bookTicker":
                        mode = self.quote_modes[symbol]
                        if mode == "missing":
                            continue
                        fields = {"u": sequence, "b": "100.0", "B": "1.00000",
                                  "a": "101.0", "A": "2.00000"}
                        if mode == "empty":
                            fields.update(b=None, B=None)
                        elif mode == "invalid":
                            fields.update(b="100.05")
                            # Inject one fault, then await the scenario's explicit
                            # recovery. Repeating malformed frames can overlap
                            # reconnects with the later order dispatch phase.
                            self.invalid_quote_frames += 1
                            self.quote_modes[symbol] = "missing"
                        elif mode == "stale":
                            at -= 5_000_000
                        self.sent_quotes[symbol].append((at - 1000) * 1000)
                    else:
                        assert channel == "depth", topic
                        # The inherited REST snapshot is anchored at 100. A late
                        # depth subscription must still begin with 101, even if
                        # bookTicker has already produced several updates.
                        first_depth = topic not in depth_sequences
                        depth_sequences[topic] = depth_sequences.get(topic, 100) + 1
                        if first_depth:
                            self.public_diagnostics.append({"event": "first_depth", "connection": connection,
                                "topic": topic, "U": depth_sequences[topic], "u": depth_sequences[topic],
                                "snapshot_id": 100})
                        fields = {"U": depth_sequences[topic], "u": depth_sequences[topic],
                                  "b": [["100.0", "1.10000"]], "a": []}
                    await ws.send(json.dumps({"stream": topic, "data": {
                        "e": channel, "s": symbol, "E": at, "T": at - 1000, **fields}}))
                await asyncio.sleep(0.05)
        finally:
            self.public_sockets.discard(ws)
            self.public_diagnostics.append({"event": "public_closed", "connection": connection,
                "close_code": getattr(ws, "close_code", None)})


def test_late_depth_subscriptions_bridge_actual_snapshot_on_each_connection():
    """Quote-only iterations must not consume the depth snapshot's next sequence."""
    class Writer:
        def __init__(self):
            self.payload = bytearray()
        def write(self, payload):
            self.payload.extend(payload)
        async def drain(self):
            pass
        def close(self):
            pass
        async def wait_closed(self):
            pass

    async def scenario():
        peer = QuoteCachePeer(())
        for _ in range(2):
            snapshot_ids = {}
            for symbol in SYMBOLS:
                reader, writer = asyncio.StreamReader(), Writer()
                reader.feed_data(f"GET /api/v1/depth?symbol={symbol} HTTP/1.1\r\nHost: fixture\r\n\r\n".encode())
                reader.feed_eof()
                await peer.request(reader, writer)
                snapshot = json.loads(writer.payload.split(b"\r\n\r\n", 1)[1])
                snapshot_ids["depth." + symbol] = int(snapshot["lastUpdateId"])
            desired = {"bookTicker." + SYMBOLS[0]}
            depths = {"depth." + symbol: [] for symbol in SYMBOLS}
            done = asyncio.Event()

            class Socket:
                quote_count = 0
                async def send(self, raw):
                    frame = json.loads(raw)
                    if frame["stream"].startswith("bookTicker."):
                        self.quote_count += 1
                        if self.quote_count == 3:
                            desired.add("depth." + SYMBOLS[0])
                    else:
                        topic = frame["stream"]
                        depths[topic].append(frame["data"])
                        if topic == "depth." + SYMBOLS[0] and len(depths[topic]) == 3:
                            desired.add("depth." + SYMBOLS[1])
                        if all(len(rows) >= 3 for rows in depths.values()):
                            done.set()

            socket = Socket()
            task = asyncio.create_task(peer.public_frames(socket, desired))
            try:
                await asyncio.wait_for(done.wait(), 3)
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            assert socket.quote_count >= 3
            for topic, rows in depths.items():
                snapshot_id = snapshot_ids[topic]
                assert rows[0]["U"] == rows[0]["u"] == snapshot_id + 1, json.dumps({
                    "topic": topic, "snapshot_id": snapshot_id, "first_U": rows[0]["U"],
                    "first_u": rows[0]["u"], "quote_count": socket.quote_count})
                assert all(row["U"] == row["u"] == snapshot_id + index
                           for index, row in enumerate(rows, 1))
        assert not peer.errors
        assert peer.public_connections == 2 and not peer.public_sockets

    asyncio.run(scenario())


def build_observer_node(peer, directory):
    bp = native_adapter()
    from nautilus_trader.common import CacheConfig, Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveDataEngineConfig, LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import (InstrumentId, OmsType, OrderSide, Price, Quantity,
                                       StrategyId, TimeInForce, TraderId)
    from nautilus_trader.trading import Strategy, StrategyConfig
    from opportunity_core import ScanSettings
    from opportunity_runtime import ScanState, create_observer
    from opportunity_scan import MarketPlan, ScanPlan

    economics_values = dict(margin_init="0.1", margin_maint="0.05", maker_fee="-0.00001",
                           taker_fee="0.0002", source="Synthetic", source_reference="signed-production-peer")
    scan_plan = ScanPlan(tuple(MarketPlan(symbol.split("_")[0], "BACKPACK", symbol + ".BACKPACK",
        Decimal("1"), "synthetic parity", backpack_economics=economics_values) for symbol in SYMBOLS),
        ScanSettings(max_age_ms=1000), None, refresh_ms=60000, evaluation_interval_ms=20)
    state = ScanState(scan_plan)
    endpoints = dict(base_url_http=peer.http_url, base_url_ws=peer.ws_url)
    quota = bp.BackpackQuota()
    public = bp.BackpackDataClientConfig(list(SYMBOLS),
        {symbol: bp.BackpackInstrumentEconomics(**economics_values) for symbol in SYMBOLS},
        http_timeout_secs=3, quote_stale_after_ms=1000, **endpoints)
    credential = bp.BackpackCredential(SEED, **endpoints)
    account = bp.BackpackExecutionClientConfig(list(SYMBOLS), credential, "BACKPACK-QUOTE-PEER",
        "caller-label-not-authority", str(directory / "identity"), quota=quota,
        connect_timeout_ms=10000, shutdown_timeout_ms=1000, recovery_interval_ms=1000,
        page_size=1000, max_pages=10, max_items=10000, read_timeout_ms=30000, **endpoints)
    authority = bp.BackpackProductionExecutionAuthority(expires_at_ms=time.time_ns() // 1_000_000 + 180000,
        max_account_age_ms=10000, max_market_age_ms=1000, max_order_notional="5",
        max_reserved_notional="10", max_reserved_margin="10", max_unsettled_orders=1,
        allow_new_risk=True, allow_reduction=True, allow_owned_cancel=True)
    execution = bp.BackpackProductionExecutionClientConfig(account, public, authority=authority,
        mutation_budget_ms=1000, receive_window_ms=5000, economic_state_directory=str(directory / "economics"))

    class Probe(Strategy):
        def __init__(self):
            super().__init__(StrategyConfig(strategy_id=StrategyId.from_str("QUOTE-CACHE-001"),
                oms_type=OmsType.NETTING, log_events=False, log_commands=False))
            self.events = []

        def submit_probe(self, symbol=SYMBOLS[0]):
            order = self.order_factory.limit(InstrumentId.from_str(symbol + ".BACKPACK"),
                OrderSide.BUY, Quantity.from_str("0.00002"), Price.from_str("99.9"),
                time_in_force=TimeInForce.GTC, post_only=True)
            self.submit_order(order)
            return order

        def on_order_denied(self, event):
            self.events.append(("denied", str(event.client_order_id), str(event.reason)))

        def on_order_accepted(self, event):
            self.events.append(("accepted", str(event.client_order_id), None))

        def on_order_canceled(self, event):
            self.events.append(("canceled", str(event.client_order_id), None))

    node = (LiveNode.builder("QUOTE-CACHE-TEST", TraderId.from_str("QUOTE-CACHE-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.OFF, fileout_level=LogLevel.OFF,
            bypass_logging=True, print_config=False))
        .with_cache_config(CacheConfig(tick_capacity=1, bar_capacity=1, save_market_data=False,
            persist_account_events=False))
        .with_data_engine_config(LiveDataEngineConfig(emit_quotes_from_book=False,
            emit_quotes_from_book_depths=False))
        .with_timeout_connection(10).with_reconciliation(False).with_timeout_reconciliation(0)
        .with_timeout_portfolio(0).with_timeout_disconnection_secs(2)
        .with_delay_post_stop_secs(0).with_delay_shutdown_secs(0)
        .with_load_state(False).with_save_state(False)
        .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=False,
            inflight_check_interval_ms=0, open_check_interval_secs=None, position_check_interval_secs=None))
        .add_data_client("BACKPACK", bp.BackpackDataClientFactory(quota=quota), public)
        .add_exec_client(None, bp.BackpackProductionExecutionClientFactory(), execution).build())
    observer = create_observer(scan_plan, state, node.handle().stop, {"BACKPACK": public})
    probe = Probe()
    node.add_actor(observer)
    node.add_strategy(probe)
    return node, observer, state, probe, execution.control, public


async def eventually(predicate, *, node_task, observer, peer, control, timeout=20):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        assert not node_task.done(), "native node stopped unexpectedly"
        assert observer.failure is None, observer.failure
        assert not peer.errors, peer.errors
        result = predicate()
        if result:
            return result
        await asyncio.sleep(0.02)
    pytest.fail(json.dumps({"health": json.loads(control.telemetry_snapshot_json()),
        "topics": sorted(peer.topics), "quote_modes": peer.quote_modes,
        "admission": json.loads(control.admission_status_json())}))


@pytest.mark.parametrize("missing", [SYMBOLS, (SYMBOLS[1],)], ids=["all-quotes-empty", "second-scope-symbol-empty"])
def test_actual_observer_populates_native_cache_and_recovers_production_admission(tmp_path, missing, request):
    native_adapter()

    async def scenario():
        from nautilus_trader.model import InstrumentId, QuoteTick
        from opportunity_native_execution import NativeOrderGateway

        async with QuoteCachePeer(missing) as peer:
            node, observer, state, probe, control, public = build_observer_node(peer, tmp_path)
            cache, handle = node.cache, node.handle()
            gateway = NativeOrderGateway(probe, state.plan.markets, lambda _: None, lambda _: None,
                lambda _: None, native_controls={"BACKPACK": control}, backpack_public_config=public,
                environ={})
            task = asyncio.ensure_future(node.run_async())
            ids = {symbol: InstrumentId.from_str(symbol + ".BACKPACK") for symbol in SYMBOLS}
            last_health = None
            async def wait(predicate):
                return await eventually(predicate, node_task=task, observer=observer,
                    peer=peer, control=control)
            def admission():
                return json.loads(control.admission_status_json())
            def health():
                nonlocal last_health
                value = json.loads(public.telemetry_snapshot_json())
                observation = {key: value.get(key) for key in (
                    "connection_epoch", "connected", "metadata_ready", "stale_reason",
                    "quotes_fresh", "books_continuous", "books_fresh")}
                if observation != last_health:
                    peer.public_diagnostics.append({"event": "public_health", **observation})
                    last_health = observation
                return value
            def gateway_status():
                gateway.poll(time.time_ns())
                return gateway.native_admission["BACKPACK"]
            def ready():
                return all(health()["quotes_fresh"].get(symbol, False) for symbol in SYMBOLS) and gateway_status()["ready"]
            def no_writes():
                assert not [request for request in peer.requests if request[0] != "GET"]
            try:
                await wait(lambda: observer.started and len(state.books) == len(SYMBOLS)
                    and json.loads(control.telemetry_snapshot_json())["rest_snapshot_observed"])
                await wait(lambda: all(cache.quote(ids[symbol]) is not None
                    for symbol in SYMBOLS if symbol not in missing))
                assert all(book.bids and book.asks for book in state.books.values())
                for symbol in missing:
                    assert cache.quote(ids[symbol]) is None
                status = admission()
                assert status["ready"] is False and "native quote cache is empty" in status["reason"]
                assert gateway_status()["ready"] is False
                denied = probe.submit_probe()
                await wait(lambda: any(row[:2] == ("denied", str(denied.client_order_id)) for row in probe.events))
                assert "native quote cache is empty" in probe.events[-1][2]
                no_writes()

                peer.quote_modes.update({symbol: "fresh" for symbol in SYMBOLS})
                await wait(lambda: all(cache.quote(i) is not None for i in ids.values()) and ready())
                assert {"bookTicker." + symbol for symbol in SYMBOLS} <= peer.topics
                for symbol, instrument in ids.items():
                    quote = cache.quote(instrument)
                    assert isinstance(quote, QuoteTick)
                    assert str(quote.bid_price) == "100.0" and str(quote.ask_price) == "101.0"
                    assert quote.ts_event in peer.sent_quotes[symbol]
                    assert time.time_ns() - quote.ts_event < 1_000_000_000

                # An invalid or old wire quote cannot leave a usable stale native quote behind.
                for mode in ("missing", "empty", "stale", "invalid"):
                    peer.quote_modes[SYMBOLS[1]] = mode
                    await wait(lambda: not health()["quotes_fresh"].get(SYMBOLS[1], False))
                    assert cache.quote(ids[SYMBOLS[1]]) is not None
                    assert gateway_status()["ready"] is False
                    blocked = probe.submit_probe()
                    await wait(lambda: any(row[:2] == ("denied", str(blocked.client_order_id)) for row in probe.events))
                    no_writes()
                    peer.quote_modes[SYMBOLS[1]] = "fresh"
                    await wait(ready)
                assert peer.invalid_quote_frames == 1
                request.node.user_properties.append(("invalid_quote_frames", peer.invalid_quote_frames))

                accepted = probe.submit_probe()
                outcome = await wait(lambda: next((row for row in probe.events if row[1] == str(accepted.client_order_id)), None))
                assert outcome[0] == "accepted", {"outcome": outcome, "public": health(), "admission": admission()}
                assert len(peer.posts) == 1
                assert peer.posts[0]["symbol"] == SYMBOLS[0]
                probe.cancel_order(accepted.client_order_id)
                await wait(lambda: any(row[:2] == ("canceled", str(accepted.client_order_id)) for row in probe.events))
                assert len(peer.deletes) == 1 and not peer.fills

                # A previous epoch's cached quote must not become authority for the new transport.
                old_epoch = health()["connection_epoch"]
                old_connections = peer.public_connections
                request.node.user_properties.extend([("public_epoch_before_close", old_epoch),
                    ("public_connections_before_close", old_connections)])
                def assert_single_reconnect():
                    current_epoch = health()["connection_epoch"]
                    assert (peer.public_connections, current_epoch) == (old_connections + 1, old_epoch + 1), {
                        "public_connections_before_close": old_connections,
                        "public_connections": peer.public_connections,
                        "public_epoch_before_close": old_epoch, "public_epoch": current_epoch}
                peer.quote_modes[SYMBOLS[1]] = "missing"
                for ws in tuple(peer.public_sockets):
                    await ws.close()
                await wait(lambda: health()["connection_epoch"] != old_epoch and health()["connected"])
                assert cache.quote(ids[SYMBOLS[1]]) is not None
                assert gateway_status()["ready"] is False
                blocked = probe.submit_probe()
                await wait(lambda: any(row[:2] == ("denied", str(blocked.client_order_id)) for row in probe.events))
                assert len(peer.posts) == len(peer.deletes) == 1
                peer.quote_modes[SYMBOLS[1]] = "fresh"
                await wait(ready)
                assert_single_reconnect()
                recovered_quote_time = cache.quote(ids[SYMBOLS[1]]).ts_event
                recovered = probe.submit_probe()
                outcome = await wait(lambda: next((row for row in probe.events if row[1] == str(recovered.client_order_id)), None))
                assert outcome[0] == "accepted", json.dumps({"outcome": outcome,
                    "public": health(), "admission": admission()})
                assert len(peer.posts) == 2
                probe.cancel_order(recovered.client_order_id)
                await wait(lambda: any(row[:2] == ("canceled", str(recovered.client_order_id)) for row in probe.events))
                assert len(peer.deletes) == 2 and not peer.fills
                await wait(lambda: cache.quote(ids[SYMBOLS[1]]).ts_event > recovered_quote_time and ready())
                assert_single_reconnect()
                request.node.user_properties.extend([("order_posts", len(peer.posts)),
                    ("order_deletes", len(peer.deletes)), ("fills", len(peer.fills)),
                    ("explicit_reconnects", peer.public_connections - old_connections),
                    ("explicit_epoch_changes", health()["connection_epoch"] - old_epoch),
                    ("recovered_public_epoch", health()["connection_epoch"])])
            except BaseException:
                print("quote-cache-public-diagnostics=" + json.dumps(list(peer.public_diagnostics)))
                raise
            finally:
                gateway.close()
                handle.stop()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 8)
                finally:
                    node.dispose()
            assert not peer.errors
            assert peer.private_verified and peer.signed_reads
            assert not observer.running and not any(observer.subscription_ids.values())

    asyncio.run(scenario())

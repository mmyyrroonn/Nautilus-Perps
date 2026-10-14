"""Automatic BTC startup checks against real Rust clients and signed local peers."""
from dataclasses import replace
from decimal import Decimal
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))

from opportunity_account_reader import ReadOnlyAccountReader
from opportunity_native_execution import NativeOrderGateway
from opportunity_preflight import BtcPreflight, PreflightMarket


def automatic_backpack_peer():
    """Extend the reviewed maker-only peer with ordinary IOC entry matching.

    Authentication, complete account reads, history and private subscriptions
    remain the same signed peer. Passive reprices below the bid remain resting.
    """
    import asyncio
    import json
    from backpack_production_peer import ProductionPeer, SYMBOLS, micros

    class ReplayHeaders:
        def __init__(self, raw, reader):
            self.raw, self.reader = raw, reader
        async def readuntil(self, delimiter):
            return self.raw
        async def readexactly(self, size):
            return await self.reader.readexactly(size)

    class AutomaticPeer(ProductionPeer):
        async def request(self, reader, writer):
            raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 5)
            lines = raw.decode("ascii").split("\r\n")
            method, target, _ = lines[0].split(" ")
            if method != "POST":
                return await super().request(ReplayHeaders(raw, reader), writer)
            self.workers.add(asyncio.current_task())
            try:
                assert target == "/api/v1/order"
                headers = {key.lower(): value for key, value in
                    (line.split(": ", 1) for line in lines[1:] if ": " in line)}
                body = json.loads(await reader.readexactly(int(headers["content-length"])))
                self.verify(headers, "orderExecute", body)
                self.requests.append((method, target))
                common = {"symbol", "side", "orderType", "timeInForce", "quantity", "clientId", "reduceOnly",
                    "autoBorrow", "autoBorrowRepay", "autoLend", "autoLendRedeem", "price", "postOnly"}
                assert set(body) == common and body["symbol"] in SYMBOLS and body["orderType"] == "Limit"
                assert all(body[name] is False for name in ("autoBorrow", "autoBorrowRepay", "autoLend", "autoLendRedeem"))
                if body["postOnly"]:
                    assert body["side"] == "Bid" and not body["reduceOnly"] and body["timeInForce"] == "GTC"
                    assert Decimal(body["price"]) < 100
                else:
                    assert body["timeInForce"] == "IOC"
                    assert (body["side"] == "Bid" and not body["reduceOnly"] and Decimal(body["price"]) >= Decimal("100.1")) or (
                        body["side"] == "Ask" and body["reduceOnly"] and Decimal(body["price"]) <= 100)
                assert all(row["status"] in {"Filled", "Cancelled"} for row in self.orders.values())
                self.posts.append(body)
                row = {**body, "id": f"owned-{len(self.posts)}", "createdAt": micros(),
                    "executedQuantity": "0", "executedQuoteQuantity": "0", "status": "New", "selfTradePrevention": "RejectTaker"}
                self.orders[row["id"]] = row
                self.execution_prices[row["id"]] = row["price"]
                self.record_history(row)
                self.first_post.set()
                await self.response(writer, self.order_dto(row))
                if not body["postOnly"]:
                    self.spawn(self.fill_delayed(row, 0.15))
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self.errors.append(type(error).__name__)
                raise
            finally:
                writer.close()
                await writer.wait_closed()
                self.workers.discard(asyncio.current_task())
    return AutomaticPeer()


class AutomaticCheck:
    """The actual preflight chooses all commands from native receipts and account evidence."""
    def __init__(self, strategy, venue, instrument_id, native_control):
        from nautilus_trader.model import InstrumentId
        self.strategy = strategy
        self.venue = venue
        self.instrument_id = instrument_id
        self.native_id = InstrumentId.from_str(instrument_id)
        self.preflight = None
        self.commands = []
        self.accounts = []
        self.receipts = []
        self.fills = []
        market = SimpleNamespace(venue=venue, instrument_id=instrument_id,
            quote_to_usd=Decimal(1), canonical_multiplier=Decimal(1))
        reader = ReadOnlyAccountReader(environ={},
            transport=lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("native account reader must perform no HTTP")))
        self.gateway = NativeOrderGateway(strategy, [market], self.receipt, self.account,
            self.fills.append, reader=reader, quote_provider=self.quote,
            native_controls={venue: native_control}, timeout_secs=10, account_timeout_secs=20)

    def quote(self, instrument_id, now_ns):
        quote = self.strategy.cache.quote(self.native_id)
        if quote is None:
            raise ValueError("native BTC quote is not ready")
        return quote.bid_price.as_decimal(), quote.ask_price.as_decimal(), quote.ts_event

    def dispatch(self, intents, now_ns):
        for intent in intents:
            self.commands.append(intent)
            self.gateway.dispatch(intent, now_ns)

    def receipt(self, receipt):
        now_ns = time.time_ns()
        self.receipts.append(receipt)
        if self.preflight.snapshot is not None:
            bid, ask, stamp = self.quote(self.instrument_id, now_ns)
            self.preflight.snapshot = replace(self.preflight.snapshot, bid=bid, ask=ask, book_observed_ns=stamp)
        self.dispatch(self.preflight.on_receipt(receipt, now_ns), now_ns)

    def account(self, account):
        now_ns = time.time_ns()
        self.accounts.append(account)
        self.dispatch(self.preflight.on_account(account, now_ns), now_ns)

    def event(self, kind, event):
        self.gateway.on_native_event(kind, event, time.time_ns())

    def tick(self):
        now_ns = time.time_ns()
        if self.preflight is None:
            instrument = self.strategy.cache.instrument(self.native_id)
            if instrument is None:
                return
            try:
                self.quote(self.instrument_id, now_ns)
            except ValueError:
                return
            minimum = instrument.min_quantity
            notional = instrument.min_notional
            market = PreflightMarket(self.venue, self.instrument_id,
                size_increment=instrument.size_increment.as_decimal(),
                price_increment=instrument.price_increment.as_decimal(),
                min_quantity=(instrument.size_increment if minimum is None else minimum).as_decimal(),
                min_notional=Decimal(0) if notional is None else notional.as_decimal(),
                supports_modify=False, modify_mode="cancel_replace")
            self.preflight = BtcPreflight([market], session_id="native-check-" + self.venue.lower(),
                max_notional_usd=Decimal(20), timeout_secs=20)
            self.dispatch(self.preflight.start(now_ns), now_ns)
        self.gateway.poll(now_ns)
        self.dispatch(self.preflight.on_timer(now_ns), now_ns)


def assert_automatic_flat(check):
    import json
    document = check.preflight.document()
    assert document["result"] == "passed", json.dumps({"stage": document["stage"], "reason": document["reason"], "gateway": check.gateway.last_error,
        "receipts": [(r.request_id, r.kind, str(r.filled_quantity)) for r in check.receipts],
        "accounts": [(str(a.position_quantity), a.open_order_ids, a.observed_ns) for a in check.accounts]}, default=str)
    assert Decimal(document["owned_quantity"]) == 0
    assert check.accounts and check.accounts[-1].position_quantity == 0
    assert check.accounts[-1].open_order_ids == ()
    assert check.fills and all(fill["commission_amount"] is not None for fill in check.fills)
    assert sum(command.action == "submit" for command in check.commands) == 4
    assert sum(command.action == "cancel" for command in check.commands) == 2
    assert not any(command.action == "modify" for command in check.commands)
    assert any(command.reduce_only for command in check.commands)
    assert all(snapshot.book_observed_ns >= snapshot.observed_ns for snapshot in check.accounts)
    assert all(row["completed_ns"] is not None for row in document["operation_timings"])


def test_backpack_native_account_gateway_drives_automatic_btc_preflight(tmp_path):
    import asyncio
    import json
    import test_backpack_production_native as reviewed

    async def scenario():
        async with automatic_backpack_peer() as peer:
            node, strategy, control, config, public = reviewed.build_node(peer, tmp_path, [])
            check = AutomaticCheck(strategy, "BACKPACK", reviewed.SYMBOLS[0] + ".BACKPACK", control)
            original_event = strategy.event
            def event(kind, event):
                check.event(kind, event)
                original_event(kind, event)
            strategy.event = event
            task = asyncio.ensure_future(node.run_async())
            handle = node.handle()
            deadline = time.monotonic() + 90
            try:
                while not task.done() and time.monotonic() < deadline:
                    if strategy.started:
                        check.tick()
                    assert not peer.errors, peer.errors
                    if check.preflight is not None and check.preflight.result != "running":
                        assert_automatic_flat(check)
                        break
                    await asyncio.sleep(0.05)
                else:
                    assert False, {"preflight": None if check.preflight is None else check.preflight.document(),
                        "gateway": check.gateway.last_error, "health": json.loads(control.telemetry_snapshot_json()),
                        "posts": peer.posts, "deletes": peer.deletes}
                assert len(peer.posts) == 4 and len(peer.deletes) == 2
                assert sum(body["reduceOnly"] for body in peer.posts) == 1
                assert all(quantity == 0 for quantity in peer.net.values())
                from dataclasses import asdict
                (tmp_path / "backpack-automatic-preflight.json").write_text(json.dumps({
                    "preflight": check.preflight.document(), "accounts": [asdict(account) for account in check.accounts],
                    "fills": check.fills, "native": json.loads(control.account_snapshot_json())}, default=str, indent=2))
            finally:
                handle.stop()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 8)
                    from opportunity_shutdown import collect_native_shutdown
                    shutdown = collect_native_shutdown({"BACKPACK": control})
                    (tmp_path / "backpack-native-shutdown.json").write_text(json.dumps(shutdown, default=str, indent=2))
                    assert shutdown["clean"], shutdown
                finally:
                    check.gateway.close()
                    node.dispose()
    asyncio.run(scenario())


def test_ondo_native_account_gateway_drives_automatic_btc_preflight(tmp_path):
    import json
    import threading
    from datetime import timedelta
    from dataclasses import asdict
    from ondo_continuous_peer import ContinuousPeer, INSTRUMENT
    from nautilus_trader.adapters import ondo
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import AccountId, ClientId, InstrumentId, OmsType, StrategyId, TraderId
    from nautilus_trader.trading import Strategy, StrategyConfig

    peer = ContinuousPeer()
    node = None
    check = None
    def scenario():
        nonlocal node, check
        environment = ondo.OndoEnvironment.PRODUCTION
        instrument = InstrumentId.from_str(INSTRUMENT)
        endpoints = dict(base_url_http=peer.http_url, base_url_ws=peer.ws_url)
        factory = ondo.OndoExecutionClientFactory()
        assert factory.supports_complete_account_snapshots
        continuous = ondo.OndoContinuousExecutionConfig(instrument_ids=[instrument],
            max_notional_per_order_usd="20", max_gross_exposure_usd="40",
            min_available_margin_usdc="25", max_live_orders=8, max_requests_per_second=8,
            max_unresolved_orders=1)
        execution = ondo.OndoExecutionClientConfig(environment=environment,
            account_id=AccountId.from_str("ONDO-AUTOMATIC-001"),
            api_key="ondoKeyId_AUTOMATIC_TEST_ONLY", api_secret="ondoApiSecret_AUTOMATIC_TEST_ONLY",
            account_read_only=False, expected_venue_account_id="unit-account",
            diagnostics_run_id="automatic-btc-preflight", journal_path=str(tmp_path / "ondo.jsonl"),
            allow_production_orders=True, continuous_execution=continuous,
            dms_timeout_secs=30, reconcile_interval_secs=1, http_timeout_secs=3, **endpoints)

        class AutomaticStrategy(Strategy):
            def __init__(self):
                super().__init__(StrategyConfig(strategy_id=StrategyId.from_str("AUTOMATIC-001"),
                    oms_type=OmsType.NETTING, log_events=False, log_commands=False))
                self.started = False
            def on_start(self):
                self.started = True
                self.subscribe_quotes(instrument, client_id=ClientId.from_str("ONDO"))
                self.clock.set_timer("automatic-preflight", timedelta(milliseconds=50), callback=self.tick)
            def tick(self, event):
                try:
                    check.tick()
                    if check.preflight is not None and check.preflight.result != "running":
                        handle.stop()
                except Exception as error:
                    errors.append(str(error))
                    handle.stop()
            def on_stop(self):
                self.started = False
                if "automatic-preflight" in self.clock.timer_names():
                    self.clock.cancel_timer("automatic-preflight")
            def on_order_accepted(self, event):
                check.event("accepted", event)
            def on_order_filled(self, event):
                check.event("filled", event)
            def on_order_canceled(self, event):
                check.event("canceled", event)
            def on_order_rejected(self, event):
                check.event("rejected", event)
            def on_order_denied(self, event):
                check.event("denied", event)
            def on_order_expired(self, event):
                check.event("expired", event)

        node = (LiveNode.builder("ONDO-AUTOMATIC-TEST", TraderId.from_str("AUTOMATIC-001"), Environment.LIVE)
            .with_logging(LoggerConfig(stdout_level=LogLevel.OFF, fileout_level=LogLevel.OFF,
                bypass_logging=True, print_config=False))
            .with_timeout_connection(60).with_reconciliation(False).with_timeout_reconciliation(0)
            .with_timeout_portfolio(0).with_timeout_disconnection_secs(30)
            .with_delay_post_stop_secs(0).with_delay_shutdown_secs(0)
            .with_load_state(False).with_save_state(False)
            .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=False,
                inflight_check_interval_ms=0, open_check_interval_secs=None, position_check_interval_secs=None))
            .add_data_client("ONDO", ondo.OndoDataClientFactory(),
                ondo.OndoDataClientConfig(environment=environment, load_ids=[instrument], **endpoints))
            .add_exec_client("ONDO", factory, execution).build())
        strategy = AutomaticStrategy()
        node.add_strategy(strategy)
        check = AutomaticCheck(strategy, "ONDO", INSTRUMENT, factory)
        handle = node.handle()
        errors = []
        watchdog = threading.Timer(240, handle.stop)
        watchdog.daemon = True
        watchdog.start()
        try:
            node.run()
            from opportunity_shutdown import collect_native_shutdown
            shutdown = collect_native_shutdown({"ONDO": factory})
            (tmp_path / "ondo-native-shutdown.json").write_text(json.dumps(shutdown, default=str, indent=2))
            assert shutdown["clean"], shutdown
            assert not errors, errors
            assert check.preflight is not None, {"readonly": factory.read_only_snapshot(), "requests": peer.requests, "frames": peer.frames}
            assert_automatic_flat(check)
            assert peer.quantity == 0
            (tmp_path / "ondo-automatic-preflight.json").write_text(json.dumps({
                "preflight": check.preflight.document(), "accounts": [asdict(account) for account in check.accounts],
                "fills": check.fills, "native": factory.production_trade_snapshot()}, default=str, indent=2))
        finally:
            watchdog.cancel()
            handle.stop()
            (tmp_path / "ondo-debug.json").write_text(json.dumps({"requests": peer.requests, "frames": peer.frames,
                "readonly": factory.read_only_snapshot(), "native": factory.production_trade_snapshot(),
                "preflight": None if check.preflight is None else check.preflight.document(), "errors": errors,
                "receipts": [(r.request_id, r.kind, str(r.filled_quantity)) for r in check.receipts]}, default=str, indent=2))
            check.gateway.close()
            node.dispose()
    try:
        scenario()
    finally:
        peer.close()

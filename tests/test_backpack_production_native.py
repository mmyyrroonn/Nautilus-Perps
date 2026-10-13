"""Production factories through normal LiveNode, registered Strategy, risk and execution engines."""
from __future__ import annotations

import asyncio
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from types import SimpleNamespace

import pytest

from backpack_production_peer import ProductionPeer, SEED, SYMBOLS
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


def native_adapter():
    module = pytest.importorskip("nautilus_trader.adapters.backpack")
    assert hasattr(module, "BackpackProductionExecutionClientConfig"), "install the source-bound production candidate wheel"
    return module


def roundtrips(cycles=2):
    return [(symbol, side, "100.1" if side == "buy" else "100.0")
        for _ in range(cycles) for symbol in SYMBOLS for side in ("buy", "close")]


def build_node(peer, directory, steps):
    bp = native_adapter()
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import ClientId, InstrumentId, OmsType, OrderSide, Price, Quantity, StrategyId, TimeInForce, TraderId
    from nautilus_trader.trading import Strategy, StrategyConfig

    endpoints = dict(base_url_http=peer.http_url, base_url_ws=peer.ws_url)
    quota = bp.BackpackQuota()
    credential = bp.BackpackCredential(SEED, **endpoints)
    economics = bp.BackpackInstrumentEconomics("0.1", "0.05", "-0.00001", "0.0002", "Synthetic", "signed-production-peer")
    public = bp.BackpackDataClientConfig(list(SYMBOLS), {symbol: economics for symbol in SYMBOLS},
        http_timeout_secs=3, quote_stale_after_ms=1000, **endpoints)
    account = bp.BackpackExecutionClientConfig(list(SYMBOLS), credential, "BACKPACK-SIGNED-PEER",
        "caller-label-not-authority", str(directory / "identity"), quota=quota,
        connect_timeout_ms=10000, shutdown_timeout_ms=1000, recovery_interval_ms=1000,
        page_size=1000, max_pages=10, max_items=10000, read_timeout_ms=30000, **endpoints)
    authority = bp.BackpackProductionExecutionAuthority(expires_at_ms=time.time_ns() // 1000000 + 180000,
        max_account_age_ms=10000, max_market_age_ms=10000, max_order_notional="5",
        max_reserved_notional="10", max_reserved_margin="10", max_unsettled_orders=1,
        allow_new_risk=True, allow_reduction=True, allow_owned_cancel=True)
    plan = bp.BackpackProductionExecutionClientConfig(account, public, authority=authority,
        mutation_budget_ms=1000, receive_window_ms=5000, economic_state_directory=str(directory / "economics"))
    control = plan.control

    class ProductionScenario(Strategy):
        def __init__(self):
            super().__init__(StrategyConfig(strategy_id=StrategyId.from_str("PRODUCTION-001"), oms_type=OmsType.NETTING,
                log_events=False, log_commands=False))
            self.started = False
            self.current = None
            self.accepted = False
            self.cancel_sent = False
            self.index = 0
            self.events = []
            self.failure = None
            self.cancel202_pending = False
            self.order_ids = []
            self.receipts = None
            self.terminals = None
            self.recovery_seen = 0
            self.last_financial_recovery_count = 0
            self.waiting_reason = None

        def on_start(self):
            self.started = True
            for symbol in SYMBOLS:
                self.subscribe_quotes(InstrumentId.from_str(symbol + ".BACKPACK"), client_id=ClientId.from_str("BACKPACK"))

        def on_stop(self):
            self.started = False

        def advance(self):
            if not self.started or self.failure:
                return
            health = json.loads(control.telemetry_snapshot_json())
            self.recovery_seen = max(self.recovery_seen, health["recovery_count"])
            if self.current is not None:
                current = self.cache.order(self.current.client_order_id)
                if not current.is_closed:
                    if peer.fault == "cancel202" and self.cancel_sent and not peer.release_fill.is_set():
                        if "CancelPending" in health["evidence_gaps"]:
                            assert len(peer.posts) == 1
                            self.cancel202_pending = True
                            peer.release_fill.set()
                    # Wait for the actual command outcome before preparing another admission.
                    # Readiness preparation refreshes native facts and the mutation fence.
                    if not self.accepted or self.cancel_sent or steps[self.index][1] != "cancel":
                        return
            try:
                admission = json.loads(control.admission_status_json())
                self.waiting_reason = admission.get("reason")
                self.receipts = json.loads(control.persist_economics())
                self.terminals = json.loads(control.reconcile_terminal_evidence())
            except RuntimeError as e:
                self.waiting_reason = {"admission": self.waiting_reason, "reconciliation": str(e)}
                return
            if not health["rest_snapshot_observed"] or not health["transport_connected"]:
                return
            if self.current is not None:
                row = self.cache.order(self.current.client_order_id)
                step = steps[self.index]
                if step[1] == "cancel" and self.accepted and not self.cancel_sent:
                    self.cancel_order(row.client_order_id)
                    self.cancel_sent = True
                    return
                if peer.fault == "cancel202" and peer.delete_received.is_set() and not peer.release_fill.is_set():
                    if "CancelPending" in health["evidence_gaps"]:
                        assert not row.is_closed and len(peer.posts) == 1
                        self.cancel202_pending = True
                        peer.release_fill.set()
                    return
                if not row.is_closed:
                    return
                terminal = next((record for record in self.terminals.get("orders", [])
                    if record["client_order_id"] == str(row.client_order_id)), None)
                if terminal is None or not terminal["reconciled"]:
                    return
                self.index += 1
                self.current = None
                self.accepted = self.cancel_sent = False
            if self.index == len(steps):
                return
            if not peer.external and not json.loads(control.admission_status_json())["ready"]:
                return
            symbol, side, price = steps[self.index]
            instrument = InstrumentId.from_str(symbol + ".BACKPACK")
            quote = self.cache.quote(instrument)
            if quote is None or not json.loads(public.telemetry_snapshot_json())["quotes_fresh"].get(symbol, False):
                return
            order = self.order_factory.limit(instrument,
                OrderSide.SELL if side == "close" else OrderSide.BUY,
                Quantity.from_str("0.00002"), Price.from_str(price),
                time_in_force=TimeInForce.IOC if side == "close" else TimeInForce.GTC,
                post_only=side != "close", reduce_only=side == "close")
            self.current = order
            self.order_ids.append(str(order.client_order_id))
            self.submit_order(order)

        def event(self, kind, event):
            record = {"kind": kind, "client_order_id": str(event.client_order_id),
                "instrument_id": str(event.instrument_id)}
            if hasattr(event, "trade_id"):
                record.update(trade_id=str(event.trade_id), commission=str(event.commission),
                    quantity=str(event.last_qty), price=str(event.last_px), liquidity_side=str(event.liquidity_side))
            if hasattr(event, "reason"):
                record["reason"] = str(event.reason)
            self.events.append(record)
            if kind in {"filled", "canceled"}:
                self.last_financial_recovery_count = json.loads(control.telemetry_snapshot_json())["completed_recovery_count"]

        def on_order_accepted(self, event):
            self.event("accepted", event)
            if self.current is not None and event.client_order_id == self.current.client_order_id:
                self.accepted = True

        def on_order_filled(self, event):
            self.event("filled", event)

        def on_order_canceled(self, event):
            self.event("canceled", event)

        def on_order_denied(self, event):
            self.event("denied", event)
            self.failure = "denied"

        def on_order_rejected(self, event):
            self.event("rejected", event)
            self.failure = "rejected"

    node = (LiveNode.builder("BACKPACK-PRODUCTION-TEST", TraderId.from_str("PRODUCTION-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.OFF, fileout_level=LogLevel.OFF,
            bypass_logging=True, print_config=False))
        .with_timeout_connection(10).with_reconciliation(False).with_timeout_reconciliation(0)
        .with_timeout_portfolio(0).with_timeout_disconnection_secs(2)
        .with_delay_post_stop_secs(0).with_delay_shutdown_secs(0)
        .with_load_state(False).with_save_state(False)
        .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=False,
            inflight_check_interval_ms=0, open_check_interval_secs=None, position_check_interval_secs=None))
        .add_data_client(None, bp.BackpackDataClientFactory(quota=quota), public)
        .add_exec_client(None, bp.BackpackProductionExecutionClientFactory(), plan).build())
    strategy = ProductionScenario()
    node.add_strategy(strategy)
    return node, strategy, control, plan, public


async def run_node(peer, directory, steps, *, expect_denial=False, recovery=False):
    node, strategy, control, plan, public = build_node(peer, directory, steps)
    cache = node.cache
    task = asyncio.ensure_future(node.run_async())
    handle = node.handle()
    deadline = time.monotonic() + 120
    result = None
    try:
        while not task.done() and time.monotonic() < deadline:
            strategy.advance()
            assert not peer.errors, {"peer_errors": peer.errors, "waiting_reason": strategy.waiting_reason}
            if strategy.failure:
                assert expect_denial and strategy.failure == "denied", json.dumps({
                    "failure": strategy.failure, "events": strategy.events,
                    "health": json.loads(control.telemetry_snapshot_json()), "peer_errors": peer.errors})
                break
            complete = strategy.index == len(steps) and strategy.receipts is not None
            if complete and steps:
                complete = json.loads(control.telemetry_snapshot_json())["completed_recovery_count"] > strategy.last_financial_recovery_count
            if complete and (not recovery or strategy.recovery_seen >= 2):
                break
            await asyncio.sleep(0.05)
        assert not task.done(), "native run terminated before bounded scenario stop"
        assert time.monotonic() < deadline, json.dumps({"index": strategy.index, "events": strategy.events,
            "health": json.loads(control.telemetry_snapshot_json()), "peer_errors": peer.errors,
            "posts": peer.posts, "queries": peer.queries, "waiting_reason": strategy.waiting_reason})
        from nautilus_trader.model import Venue
        positions = sorted(cache.positions(venue=Venue.from_str("BACKPACK")), key=lambda row: str(row.id))
        orders = sorted(cache.orders(venue=Venue.from_str("BACKPACK")), key=lambda row: str(row.client_order_id))
        result = {"events": strategy.events, "order_ids": strategy.order_ids,
            "receipts": strategy.receipts, "terminals": strategy.terminals,
            "cancel202_pending": strategy.cancel202_pending,
            "orders": [{"client_order_id": str(order.client_order_id), "status": str(order.status),
                "filled": str(order.filled_qty), "trades": [str(trade) for trade in order.trade_ids],
                "commissions": [str(fee) for fee in order.commissions().values()]} for order in orders],
            "positions": [{"instrument_id": str(position.instrument_id), "open": position.is_open,
                "quantity": str(position.quantity), "commissions": [str(fee) for fee in position.commissions()],
                "pnl": None if position.realized_pnl is None else str(position.realized_pnl)} for position in positions],
            "health": json.loads(control.telemetry_snapshot_json())}
    finally:
        handle.stop()
        try:
            await asyncio.wait_for(asyncio.shield(task), 8)
        finally:
            node.dispose()
    result["shutdown"] = json.loads(control.shutdown_report_json())
    assert not peer.errors, peer.errors
    assert peer.private_verified and peer.signed_reads
    assert not hasattr(control, "accept_account")
    return result


def assert_economics(result, count):
    assert len([row for row in result["events"] if row["kind"] == "filled"]) == count
    assert result["receipts"]["durable_receipts"] == count
    assert not result["health"]["pending_fills"]
    assert all(len(row["trades"]) == 1 and row["filled"] == "0.00002" for row in result["orders"] if row["status"] == "FILLED")
    assert all(not row["open"] for row in result["positions"])
    assert not result["shutdown"]["dirty"], result["shutdown"]


def test_two_symbols_repeated_roundtrips_exact_rebates_history_replay_and_clean_restart(tmp_path):
    native_adapter()
    async def scenario():
        async with ProductionPeer() as peer:
            result = await run_node(peer, tmp_path, roundtrips(2))
            assert_economics(result, 8)
            assert len(peer.posts) == 8 and len({row["clientId"] for row in peer.posts}) == 8
            assert all(row["postOnly"] and row["timeInForce"] == "GTC" for row in peer.posts if not row["reduceOnly"])
            assert all(not row["postOnly"] and row["timeInForce"] == "IOC" for row in peer.posts if row["reduceOnly"])
            assert sum(row["reduceOnly"] for row in peer.posts) == 4
            fills = [row for row in result["events"] if row["kind"] == "filled"]
            assert sum(Decimal(row["commission"].split()[0]) for row in fills) == Decimal("0.000008")
            assert {row["commission"] for row in fills} == {"-0.00000100 USDC", "0.00000300 USDC"}
            old_orders = result["orders"]
            restored = await run_node(peer, tmp_path, [], recovery=True)
            assert restored["orders"] == old_orders
            assert restored["positions"] == result["positions"]
            assert restored["receipts"]["durable_receipts"] == 8
            assert not restored["health"]["pending_fills"] and not restored["shutdown"]["dirty"]
            assert len(peer.posts) == 8
    asyncio.run(scenario())


def test_gtc_post_only_owned_cancel_confirmed_replacement(tmp_path):
    native_adapter()
    async def scenario():
        async with ProductionPeer() as peer:
            steps = [(SYMBOLS[0], "cancel", "99.9"), *roundtrips(1)]
            result = await run_node(peer, tmp_path, steps)
            assert_economics(result, 4)
            assert len(peer.deletes) == 1 and len(peer.posts) == 5
            assert peer.deletes[0] == {"symbol": SYMBOLS[0], "orderId": "owned-1"}
            assert len([row for row in result["events"] if row["kind"] == "canceled"]) == 1
            assert all("reduceOnly" not in row for row in peer.history)
            assert sum(row["reduceOnly"] for row in peer.posts) == 2
            assert all(Decimal(row["quoteQuantity"]) == Decimal(row["quantity"]) * Decimal(row["price"])
                and row["selfTradePrevention"] == "RejectTaker" for row in peer.history)
            assert peer.history[0]["status"] == "Cancelled"
            assert all(row["systemOrderType"] is None and row["triggerQuantity"] is None for row in peer.history)
            assert all(len(row["timestamp"].rsplit(".", 1)[1]) == 6 and row["timestamp"].endswith("0") for row in peer.fills)
            fill_times = [row["timestamp"] for row in peer.fills]
            assert fill_times == sorted(fill_times) and len(set(fill_times)) == len(fill_times)
            assert "RecoveryIncomplete" not in result["health"]["evidence_gaps"]
    asyncio.run(scenario())


def test_pre_ack_fill_preserves_original_binding_without_resubmit(tmp_path):
    native_adapter()
    async def scenario():
        async with ProductionPeer(fault="preack") as peer:
            result = await run_node(peer, tmp_path, roundtrips(1))
            assert_economics(result, 4)
            assert len(peer.posts) == 4 and len({row["clientId"] for row in peer.posts}) == 4
            assert len({row["client_order_id"] for row in result["events"] if row["kind"] == "accepted"}) == 4
    asyncio.run(scenario())


def test_cancel202_waits_for_late_fill_and_preserves_exact_economics(tmp_path):
    native_adapter()
    async def scenario():
        async with ProductionPeer(fault="cancel202") as peer:
            result = await run_node(peer, tmp_path, [(SYMBOLS[0], "cancel", "99.9"), (SYMBOLS[0], "close", "100.0")])
            assert_economics(result, 2)
            assert result["cancel202_pending"] and len(peer.deletes) == 1 and len(peer.posts) == 2
            assert not any(row["kind"] == "canceled" for row in result["events"])
    asyncio.run(scenario())


def test_external_orders_block_admission_and_are_retained_as_dirty_residual(tmp_path):
    native_adapter()
    async def scenario():
        async with ProductionPeer(external=True) as peer:
            result = await run_node(peer, tmp_path, [(SYMBOLS[0], "buy", "100.1")], expect_denial=True)
            assert not peer.posts and not peer.deletes
            assert result["shutdown"]["dirty"]
            assert any(row["kind"] == "denied" for row in result["events"])
    asyncio.run(scenario())


def test_app_maker_and_market_roundtrips_exact_native_economics(tmp_path):
    """Exercise the operator entry point and its real registered RoundTrip strategy."""
    _app_maker_and_market_roundtrips(tmp_path)


def test_app_fresh_flat_session_excludes_old_fills_and_restores_its_own_fills(tmp_path):
    """A manually flattened account can start a new bounded, durable acceptance session."""
    _app_maker_and_market_roundtrips(tmp_path, fresh_session=True)


def _app_maker_and_market_roundtrips(tmp_path, *, fresh_session=False):
    native_adapter()
    from backpack_production import OperatorPlan, execute, build_node as build_operator_node

    async def scenario():
        async with ProductionPeer() as peer:
            if fresh_session:
                import hashlib
                from backpack_production_peer import KEY, micros, timestamp

                old_at = micros() - 3_600_000_000
                # Prior terminal orders deliberately reuse the next local numeric slots.
                for number, side in enumerate(("Bid", "Ask"), 1):
                    row = {"id": f"old-{number}", "clientId": number, "symbol": SYMBOLS[0],
                        "side": side, "orderType": "Limit", "timeInForce": "GTC", "postOnly": True,
                        "reduceOnly": side == "Ask", "quantity": "0.00005", "price": "100.0",
                        "createdAt": old_at, "status": "Filled", "executedQuantity": "0.00005",
                        "executedQuoteQuantity": "0.005", "selfTradePrevention": "RejectTaker"}
                    peer.record_history(row)
                    peer.fills.append({"clientId": str(number), "fee": "0.000001", "feeSymbol": "USDC",
                        "isMaker": True, "orderId": row["id"], "price": "100.0", "quantity": "0.00005",
                        "side": side, "symbol": SYMBOLS[0], "systemOrderType": None,
                        "timestamp": timestamp(old_at), "tradeId": 100 + number})
                baseline_start = time.time_ns() // 1_000_000 - 100
                baseline = {"schema_version": 1, "mode": "new-session-flat-preflight", "ready": True,
                    "credential_valid": True, "recovery_start_ms": baseline_start,
                    "completed_at_ms": baseline_start + 1,
                    "credential_scope_sha256": hashlib.sha256(KEY.public_key().public_bytes_raw()).hexdigest(),
                    "results": {name: {"status": 200, "observation": {"count": 0, name: []}}
                                for name in ("positions", "orders")}}
                (tmp_path / "session-baseline.json").write_text(json.dumps(baseline), encoding="utf-8")
            plan = OperatorPlan((SYMBOLS[0],), tmp_path, Decimal("5"), Decimal("5"), 60)
            commands = [{"action": "roundtrip", "symbol": SYMBOLS[0], "style": style,
                "quantity": "0.00002", "reference_price": "101", "wait_seconds": 5}
                for style in ("maker", "market")]
            result = await execute(plan, commands, SEED,
                endpoint_overrides={"base_url_http": peer.http_url, "base_url_ws": peer.ws_url})
            assert not peer.errors, {"peer_errors": peer.errors, "result": result}
            assert result["completed"] and result["failure"] is None, result
            assert result["commands_completed"] == 2 and result["account_reconciled"]
            assert [row["style"] for row in result["roundtrips"]] == ["maker", "market"]
            for trip in result["roundtrips"]:
                assert trip["done"] and trip["failure"] is None, trip
                assert trip["entry_style"] == trip["style"] and trip["exit_style"] == "market"
                assert [leg["side"] for leg in trip["legs"]] == ["buy", "sell"]
                assert [leg["order_style"] for leg in trip["legs"]] == [trip["entry_style"], "market"]
                for leg in trip["legs"]:
                    assert leg["filled_quantity"] == "0.00002"
                    assert leg["terminal_reconciled"] and leg["liquidity_verified"]
                    assert Decimal(leg["actual_notional_usdc"]) <= 5
                    assert len(leg["fills"]) == 1
                    fill = leg["fills"][0]
                    assert fill["liquidity_side"] == ("MAKER" if leg["order_style"] == "maker" else "TAKER")
                    assert fill["commission"] == ("-0.00000100 USDC" if leg["order_style"] == "maker" else "0.00000300 USDC")
                    assert int(fill["trade_id"]) >= 9007199254740993
            assert result["native_positions"] and all(not row["is_open"] for row in result["native_positions"])
            assert result["economic_consumer"]["durable_receipts"] == 4
            assert result["health"]["pending_fills"] == 0
            assert result["health"]["completed_recovery_count"] > 0
            assert "RecoveryIncomplete" not in result["health"]["evidence_gaps"]
            assert not result["shutdown"]["dirty"], result["shutdown"]
            assert peer.private_verified and peer.signed_reads
            assert len(peer.posts) == 4 and len({row["clientId"] for row in peer.posts}) == 4
            maker, market = peer.posts[:2], peer.posts[2:]
            assert [row["side"] for row in maker] == ["Bid", "Ask"]
            assert [row["reduceOnly"] for row in maker] == [False, True]
            assert maker[0]["orderType"] == "Limit" and maker[0]["timeInForce"] == "GTC" and maker[0]["postOnly"]
            assert maker[0]["price"] == "100.0"
            assert [row["side"] for row in market] == ["Bid", "Ask"]
            assert [row["reduceOnly"] for row in market] == [False, True]
            assert all(row["orderType"] == "Market" and row["timeInForce"] == "IOC"
                and not {"price", "quoteQuantity", "postOnly"}.intersection(row) for row in [maker[1], *market])
            market_history = [row for row in peer.history if row["orderType"] == "Market"]
            assert len(market_history) == 3
            assert all(row["quoteQuantity"] is None and not {"clientId", "quantity", "price", "postOnly", "reduceOnly"}.intersection(row)
                for row in market_history)
            assert len(peer.fills) == (6 if fresh_session else 4)
            assert all(row["timestamp"].endswith("0") for row in peer.fills[-4:])
            if fresh_session:
                import gc
                gc.collect()
                node, strategy, control = build_operator_node(plan, [], SEED, read_only=True,
                    endpoint_overrides={"base_url_http": peer.http_url, "base_url_ws": peer.ws_url})
                handle = node.handle()
                task = asyncio.ensure_future(node.run_async())
                try:
                    deadline = time.monotonic() + 12
                    while not task.done() and time.monotonic() < deadline:
                        if strategy.started and strategy.ready():
                            break
                        await asyncio.sleep(0.05)
                    assert strategy.started and strategy.ready(), json.loads(control.telemetry_snapshot_json())
                    assert strategy.position_quantity(SYMBOLS[0]) == 0
                    assert json.loads(control.persist_economics())["durable_receipts"] == 4
                    assert json.loads(control.telemetry_snapshot_json())["pending_fills"] == 0
                    assert len(peer.posts) == 4 and not peer.deletes
                    assert peer.fill_history_windows and all(start == baseline_start
                        for start, _ in peer.fill_history_windows)
                    assert not peer.errors
                finally:
                    handle.stop()
                    try:
                        await asyncio.wait_for(asyncio.shield(task), 12)
                    finally:
                        node.dispose()
    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["partial_entry", "zero_entry", "partial_exit"])
def test_roundtrip_controller_uses_confirmed_fill_quantity_and_retains_residual(case):
    from backpack_roundtrip import RoundTrip

    class Adapter:
        def __init__(self):
            self.order_id = None
            self.commands = []
            self.orders = {}
            self.events = []
            self.position = Decimal("0")

        def ready(self):
            return True

        def quote(self, symbol):
            return {"bid": Decimal("100"), "ask": Decimal("101")}

        def position_quantity(self, symbol):
            return self.position

        def submit(self, command):
            self.commands.append(command)
            self.order_id = f"original-{len(self.commands)}"
            self.orders[self.order_id] = SimpleNamespace(client_order_id=self.order_id,
                venue_order_id=f"venue-{len(self.commands)}", status="ACCEPTED",
                is_closed=False, filled_qty=Decimal("0"))

        def owned_order(self, order_id):
            return self.orders[order_id]

        def settled(self, order):
            return order.is_closed

        def cancel_order(self, order_id):
            raise AssertionError("terminal fixture must not issue cancellation")

        def finish(self, quantity, remaining):
            quantity = Decimal(quantity)
            order = self.orders[self.order_id]
            order.status = "FILLED" if quantity == Decimal(self.commands[-1]["quantity"]) else "CANCELED"
            order.is_closed = True
            order.filled_qty = quantity
            self.position = Decimal(remaining)
            if quantity:
                maker = self.commands[-1]["post_only"]
                self.events.append({"kind": "filled", "client_order_id": self.order_id,
                    "quantity": str(quantity), "price": self.commands[-1].get("price", "101"),
                    "liquidity_side": "MAKER" if maker else "TAKER",
                    "commission": "-0.000001 USDC" if maker else "0.000003 USDC"})

    adapter = Adapter()
    trip = RoundTrip({"action": "roundtrip", "symbol": SYMBOLS[0], "style": "maker",
        "quantity": "0.03", "wait_seconds": 5}, clock=lambda: 0)
    trip.advance(adapter)
    entry = "0" if case == "zero_entry" else "0.02" if case == "partial_entry" else "0.03"
    adapter.finish(entry, entry)
    trip.advance(adapter)
    if case == "zero_entry":
        assert not trip.done and "no fills" in trip.failure
        assert len(adapter.commands) == 1 and trip.snapshot()["phase"] == "entry"
        assert trip.legs[0]["liquidity_verified"] is False
        return
    trip.advance(adapter)
    assert len(adapter.commands) == 2
    assert adapter.commands[-1]["side"] == "sell" and adapter.commands[-1]["reduce_only"]
    assert adapter.commands[-1]["quantity"] == entry
    assert adapter.commands[-1]["order_type"] == "market" and adapter.commands[-1]["time_in_force"] == "IOC"
    assert not adapter.commands[-1]["post_only"] and "price" not in adapter.commands[-1]
    adapter.finish("0.01" if case == "partial_exit" else entry, "0.02" if case == "partial_exit" else "0")
    trip.advance(adapter)
    if case == "partial_exit":
        assert not trip.done and "residual" in trip.failure
        assert adapter.position == Decimal("0.02")
        assert not trip.snapshot()["done"]
    else:
        assert trip.done and trip.failure is None and adapter.position == 0
        assert [leg["filled_quantity"] for leg in trip.legs] == ["0.02", "0.02"]
    trip.advance(adapter)
    assert len(adapter.commands) == 2


@pytest.mark.parametrize("blocked_stage", ["terminal_reconciliation", "exit_admission"])
def test_postfill_snapshot_invalidation_has_bounded_safe_roundtrip_diagnostic(blocked_stage):
    """Use only local account DTOs and a fake clock; no sockets or financial calls."""
    from backpack_roundtrip import RoundTrip

    peer = ProductionPeer(fault="postfill_policy")
    initial_policy = peer.account_body("/api/v1/account")
    assert initial_policy["autoBorrowSettlements"] is False
    now = [0.0]

    class Adapter:
        def __init__(self):
            self.order_id = None
            self.commands = []
            self.events = []
            self.position = Decimal("0")
            self.order = None
            self.admission = {"ready": True, "reason": None}
            self.settlement_diagnostic = {"reason": None}

        def ready(self):
            invalid = peer.account_body("/api/v1/account")["autoBorrowSettlements"]
            self.admission = {"ready": not invalid,
                "reason": "UnsupportedAccountPolicy" if invalid else None}
            return self.admission["ready"]

        def quote(self, symbol):
            return {"bid": Decimal("100"), "ask": Decimal("101")}

        def position_quantity(self, symbol):
            return self.position

        def submit(self, command):
            self.commands.append(command)
            self.order_id = "original-confirmed-entry"
            self.order = SimpleNamespace(client_order_id=self.order_id, venue_order_id="independent-ack-venue",
                status="ACCEPTED", is_closed=False, filled_qty=Decimal("0"))

        def owned_order(self, reference):
            assert reference == self.order_id
            return self.order

        def settled(self, order):
            self.settlement_diagnostic = {"reason": "UnsupportedAccountPolicy"
                if blocked_stage == "terminal_reconciliation" else None}
            return blocked_stage != "terminal_reconciliation"

        def cancel_order(self, reference):
            raise AssertionError("the already filled entry must not be canceled")

    adapter = Adapter()
    trip = RoundTrip({"action": "roundtrip", "symbol": SYMBOLS[0], "style": "maker",
        "quantity": "0.00005", "wait_seconds": 5}, clock=lambda: now[0])
    trip.advance(adapter)
    adapter.order.status = "FILLED"
    adapter.order.is_closed = True
    adapter.order.filled_qty = Decimal("0.00005")
    adapter.position = Decimal("0.00005")
    peer.net[SYMBOLS[0]] = adapter.position
    peer.entry_prices[SYMBOLS[0]] = Decimal("100")
    adapter.events.append({"kind": "filled", "client_order_id": adapter.order_id,
        "quantity": "0.00005", "price": "100", "liquidity_side": "MAKER",
        "commission": "0.000001 USDC", "trade_id": "exact-real-fill-evidence"})
    assert peer.account_body("/api/v1/account")["autoBorrowSettlements"] is True
    assert initial_policy["autoBorrowSettlements"] is False
    assert Decimal(peer.account_body("/api/v1/position")[0]["netQuantity"]) == adapter.position
    trip.advance(adapter)
    if blocked_stage == "exit_admission":
        trip.advance(adapter)
    state = trip.snapshot()
    assert state["waiting_for"] == blocked_stage
    assert state["wait_reason"] == "UnsupportedAccountPolicy"
    assert Decimal(state["observed_filled_quantity"]) == Decimal("0.00005")
    assert Decimal(state["observed_position_quantity"]) == adapter.position
    assert not state["done"] and state["failure"] is None
    assert len(adapter.commands) == 1 and adapter.commands[0]["side"] == "buy"
    now[0] = 31.0
    trip.advance(adapter)
    state = trip.snapshot()
    assert state["failure"] == (
        f"reconciliation deadline reached at {blocked_stage}: UnsupportedAccountPolicy; "
        "existing exposure requires review"
    )
    assert state["waiting_for"] == blocked_stage and state["wait_reason"] == "UnsupportedAccountPolicy"
    assert not state["done"] and adapter.position == Decimal("0.00005")
    trip.advance(adapter)
    assert len(adapter.commands) == 1


def test_app_close_only_recovers_original_entry_and_only_reduces(tmp_path):
    native_adapter()
    from backpack_production import OperatorPlan, build_node as build_operator_node, execute

    async def scenario():
        async with ProductionPeer() as peer:
            plan = OperatorPlan((SYMBOLS[0],), tmp_path, Decimal("5"), Decimal("5"), 60)
            endpoints = {"base_url_http": peer.http_url, "base_url_ws": peer.ws_url}
            entry = {"action": "submit", "symbol": SYMBOLS[0], "side": "buy",
                "quantity": "0.00005", "price": "100.0", "time_in_force": "GTC",
                "post_only": True, "reduce_only": False, "cancel_after_accept": False}
            node, strategy, control = build_operator_node(plan, [entry], SEED, endpoint_overrides=endpoints)
            task = asyncio.ensure_future(node.run_async())
            handle = node.handle()
            try:
                deadline = time.monotonic() + 20
                while time.monotonic() < deadline:
                    assert not task.done() and not peer.errors
                    strategy.advance()
                    if any(row["kind"] == "filled" for row in strategy.events):
                        receipts = json.loads(control.persist_economics())
                        if receipts["durable_receipts"] == 1:
                            break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("original private entry did not reach durable native consumption")
                original = next(row for row in strategy.events if row["kind"] == "filled")
                original_id = original["client_order_id"]
                assert original["ts_event"] % 1_000_000 != 0
                before = json.loads((tmp_path / "economics" / "economics.json").read_text())["state"]["fills"][0]
                recovery_before = json.loads(control.telemetry_snapshot_json())["completed_recovery_count"]
                peer.rest_fill_timestamp_mode = "truncate_ms"
                while time.monotonic() < deadline:
                    health = json.loads(control.telemetry_snapshot_json())
                    if (peer.truncated_fill_history_reads > 0
                            and health["completed_recovery_count"] > recovery_before
                            and json.loads(control.admission_status_json())["ready"]):
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("same-node millisecond REST replay did not complete native recovery")
                assert len(peer.posts) == 1 and len(peer.fills) == 1
                assert strategy.position_quantity(SYMBOLS[0]) == Decimal("0.00005")
                assert json.loads(control.persist_economics())["durable_receipts"] == 1
                after = json.loads((tmp_path / "economics" / "economics.json").read_text())["state"]["fills"][0]
                assert after == before, "coarser REST replay must preserve the original precise report and receipt"
            finally:
                handle.stop()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 12)
                finally:
                    node.dispose()
            first_shutdown = json.loads(control.shutdown_report_json())
            assert first_shutdown["dirty"] and first_shutdown["positions_unknown_or_nonzero"]
            # A retained control owns the namespace even after stop. Release the entire
            # first runtime before recreating the client from its durable checkpoint.
            del node, strategy, control, task, handle
            import gc
            gc.collect()
            command = {"action": "close-only", "symbol": SYMBOLS[0], "style": "maker",
                "max_quantity": "0.00005", "entry_client_order_id": original_id, "wait_seconds": 5}
            second = await execute(plan, [command], SEED, endpoint_overrides=endpoints)
            assert second["completed"] and second["failure"] is None, second
            assert second["account_reconciled"] and second["commands_completed"] == 1
            assert len(second["roundtrips"]) == 1
            close = second["roundtrips"][0]
            assert close["close_only"] and close["done"] and close["failure"] is None
            assert close["entry_client_order_id"] == original_id
            assert len(close["legs"]) == 1 and close["legs"][0]["side"] == "sell"
            assert close["legs"][0]["filled_quantity"] == "0.00005"
            assert close["exit_style"] == "market" and close["legs"][0]["order_style"] == "market"
            assert close["legs"][0]["liquidity_verified"]
            assert close["legs"][0]["fills"][0]["liquidity_side"] == "TAKER"
            assert len(peer.posts) == 2 and [row["side"] for row in peer.posts] == ["Bid", "Ask"]
            assert peer.posts[1]["reduceOnly"] and peer.posts[1]["orderType"] == "Market"
            assert peer.posts[1]["timeInForce"] == "IOC"
            assert not {"price", "quoteQuantity", "postOnly"}.intersection(peer.posts[1])
            assert peer.posts[1]["quantity"] == "0.00005"
            assert peer.posts[1]["clientId"] != peer.posts[0]["clientId"]
            assert second["economic_consumer"]["durable_receipts"] == 2
            restored = json.loads((tmp_path / "economics" / "economics.json").read_text())["state"]["fills"]
            assert next(row for row in restored if row["report"]["trade_id"] == original["trade_id"]) == before
            assert all(not row["is_open"] for row in second["native_positions"])
            assert not second["shutdown"]["positions_unknown_or_nonzero"], second["shutdown"]
            assert second["shutdown"]["unknown"] == 0 and second["shutdown"]["pending_cancellations"] == 0
            assert not peer.errors
    asyncio.run(scenario())


def test_native_terminal_evidence_survives_invalid_snapshot_but_reduction_stays_gated(tmp_path):
    native_adapter()
    from backpack_production import OperatorPlan, build_node as build_operator_node

    async def scenario():
        async with ProductionPeer() as peer:
            plan = OperatorPlan((SYMBOLS[0],), tmp_path, Decimal("5"), Decimal("5"), 60)
            entry = {"action": "submit", "symbol": SYMBOLS[0], "side": "buy", "quantity": "0.00005",
                "price": "100.0", "time_in_force": "GTC", "post_only": True, "reduce_only": False,
                "cancel_after_accept": False}
            node, strategy, control = build_operator_node(plan, [entry], SEED,
                endpoint_overrides={"base_url_http": peer.http_url, "base_url_ws": peer.ws_url})
            task = asyncio.ensure_future(node.run_async())
            handle = node.handle()
            deadline = time.monotonic() + 20
            try:
                while time.monotonic() < deadline:
                    assert not task.done() and not peer.errors
                    strategy.advance()
                    if any(row["kind"] == "filled" for row in strategy.events):
                        receipts = json.loads(control.persist_economics())
                        if receipts["durable_receipts"] == 1 and json.loads(control.admission_status_json())["ready"]:
                            break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("signed peer entry did not reach durable fill and ready snapshot")
                original = next(row for row in strategy.events if row["kind"] == "filled")
                assert original["quantity"] == "0.00005" and original["commission"] == "-0.00000100 USDC"
                before = json.loads(control.reconcile_terminal_evidence())
                terminal = next(row for row in before["orders"] if row["client_order_id"] == original["client_order_id"])
                assert terminal["reconciled"]
                peer.fault = "postfill_borrow"
                while time.monotonic() < deadline:
                    admission = json.loads(control.admission_status_json())
                    if peer.invalid_collateral_reads and not admission["ready"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("fresh signed borrowed collateral did not close native admission")
                assert admission["reason"]
                after = json.loads(control.reconcile_terminal_evidence())
                assert next(row for row in after["orders"] if row["client_order_id"] == original["client_order_id"]) == terminal
                assert json.loads(control.persist_economics())["durable_receipts"] == 1
                assert strategy.position_quantity(SYMBOLS[0]) == Decimal("0.00005")
                # Deliberately exercise the native financial guard through the real Strategy.
                strategy.submit({"action": "submit", "symbol": SYMBOLS[0], "side": "sell",
                    "quantity": "0.00005", "order_type": "market", "reference_price": "101",
                    "time_in_force": "IOC", "post_only": False, "reduce_only": True,
                    "cancel_after_accept": False})
                rejected_id = strategy.order_id
                while time.monotonic() < deadline:
                    if any(row["client_order_id"] == rejected_id and row["kind"] in {"denied", "rejected"}
                        for row in strategy.events):
                        break
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("inadmissible reduction did not report its native financial rejection")
                assert len(peer.posts) == 1 and not peer.deletes
                assert peer.invalid_collateral_reads and not peer.errors
                assert len(peer.fills) == 1 and strategy.position_quantity(SYMBOLS[0]) == Decimal("0.00005")
            finally:
                handle.stop()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 12)
                finally:
                    node.dispose()
    asyncio.run(scenario())


def test_close_only_already_flat_is_a_confirmed_noop():
    from backpack_roundtrip import ClosePosition

    original_id = "original-recovered-entry"
    order = SimpleNamespace(client_order_id=original_id, venue_order_id="independent-venue-ack",
        side="BUY", is_reduce_only=False, instrument_id=SYMBOLS[0] + ".BACKPACK",
        is_closed=True, filled_qty=Decimal("0.00005"))

    class FlatAdapter:
        admission = {"ready": True, "reason": None}
        settlement_diagnostic = {"reason": None}

        def position_quantity(self, symbol):
            assert symbol == SYMBOLS[0]
            return Decimal("0")

        def ready(self):
            return True

        def owned_order(self, reference):
            assert reference == original_id
            return order

        def settled(self, candidate):
            assert candidate is order
            return True

        def submit(self, command):
            raise AssertionError("confirmed flat close-only must not submit any order")

        def cancel_order(self, reference):
            raise AssertionError("confirmed flat close-only must not cancel any order")

    adapter = FlatAdapter()
    controller = ClosePosition({"action": "close-only", "symbol": SYMBOLS[0], "style": "maker",
        "max_quantity": "0.00005", "entry_client_order_id": original_id, "wait_seconds": 5}, clock=lambda: 0)
    controller.advance(adapter)
    state = controller.snapshot()
    assert state["done"] and state["failure"] is None and state["close_only"]
    assert state["observed_filled_quantity"] == "0.00005" and state["observed_position_quantity"] == "0"
    assert state["legs"] == [] and state["active_client_order_id"] is None
    controller.advance(adapter)
    assert controller.snapshot() == state


def test_native_restart_reports_exact_timestamp_conflict_without_reapplying_trade(tmp_path):
    """A real one-microsecond source difference remains a fail-closed conflict."""
    native_adapter()
    from backpack_production import OperatorPlan, build_node as build_operator_node, execute

    async def scenario():
        async with ProductionPeer() as peer:
            plan = OperatorPlan((SYMBOLS[0],), tmp_path, Decimal("5"), Decimal("5"), 60)
            endpoints = {"base_url_http": peer.http_url, "base_url_ws": peer.ws_url}
            first = await execute(plan, [{"action": "submit", "symbol": SYMBOLS[0], "side": "buy",
                "quantity": "0.00005", "price": "100.0", "post_only": True,
                "reduce_only": False, "time_in_force": "GTC", "cancel_after_accept": False}],
                SEED, endpoint_overrides=endpoints)
            assert first["completed"] and first["economic_consumer"]["durable_receipts"] == 1, first
            original_fill = next(row for row in first["events"] if row["kind"] == "filled")
            original_raw = dict(peer.fills[0])
            peer.rest_fill_time_offset_us = 1
            node, strategy, control = build_operator_node(plan, [], SEED, read_only=True, endpoint_overrides=endpoints)
            task = asyncio.ensure_future(node.run_async())
            handle = node.handle()
            try:
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    health = json.loads(control.telemetry_snapshot_json())
                    if health.get("last_recovery_error_category") == "AccountEconomicConflict":
                        break
                    assert not task.done() and not peer.errors
                    await asyncio.sleep(0.05)
                else:
                    pytest.fail("real REST timestamp difference did not produce a bounded native diagnostic")
                assert health["last_recovery_error_stage"] == "FillHistoryAttribution", health
                assert health["last_fill_conflict_fields"] == ["ts_event"], health
                assert not json.loads(control.admission_status_json())["ready"]
                assert json.loads(control.persist_economics())["durable_receipts"] == 1
                recovered = strategy.owned_order(original_fill["client_order_id"])
                assert str(recovered.filled_qty) == "0.00005"
                assert strategy.position_quantity(SYMBOLS[0]) == Decimal("0.00005")
                assert peer.fills == [original_raw] and len(peer.posts) == 1 and not peer.deletes
                assert not any(row["kind"] == "filled" for row in strategy.events)
                assert not peer.errors
            finally:
                handle.stop()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 12)
                finally:
                    node.dispose()
    asyncio.run(scenario())

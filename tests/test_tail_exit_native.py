"""Real RiskEngine -> Lighter signer -> owned loopback peer residual regression."""
import asyncio
from decimal import Decimal as D
import time
from types import SimpleNamespace as NS

from test_lighter_protocol_native import LighterPeer
from opportunity_native_execution import NativeOrderGateway
from opportunity_preflight import OrderIntent


def test_installed_lighter_dust_reduction_passes_both_minima_and_entry_denial_finishes():
    from nautilus_trader.adapters import lighter as native
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import AccountId, OmsType, StrategyId, TraderId
    from nautilus_trader.trading import Strategy, StrategyConfig
    control = native.LighterExecutionControl()

    class Scenario(Strategy):
        def __init__(self):
            super().__init__(StrategyConfig(strategy_id=StrategyId.from_str("TAIL-EXIT"),
                oms_type=OmsType.NETTING, log_events=False, log_commands=False))
            self.receipts, self.fills = [], []
            self.gateway = None

        def on_start(self):
            market = NS(venue="LIGHTER", instrument_id="ETH-PERP.LIGHTER")
            self.gateway = NativeOrderGateway(self, [market], self.receipts.append, lambda _: None,
                self.fills.append, native_controls={"LIGHTER": control})
            for reduce_only in (False, True):
                cid = "dust-close" if reduce_only else "dust-entry"
                self.gateway.dispatch(OrderIntent(action="submit", request_id=cid,
                    venue="LIGHTER", instrument_id=market.instrument_id, client_order_id=cid,
                    side="BUY", quantity=D("0.0600"), price=D("2.59"),
                    time_in_force="IOC", reduce_only=reduce_only), time.time_ns())

        def on_order_denied(self, event):
            self.gateway.on_native_event("denied", event, time.time_ns())

        def on_order_rejected(self, event):
            self.gateway.on_native_event("rejected", event, time.time_ns())

    async def scenario():
        async with LighterPeer() as peer:
            peer.data["metadata"]["order_book_details"][0]["min_base_amount"] = "2.5000"
            endpoints = dict(base_url_http=peer.http_url, base_url_ws=peer.ws_url,
                environment=native.LighterEnvironment.TESTNET, http_timeout_secs=3, ws_timeout_secs=3)
            execution = native.LighterExecutionClientConfig(AccountId.from_str("LIGHTER-001"),
                account_index=12345, api_key_index=5, private_key="01" + "00" * 39,
                control=control, **endpoints)
            node = (LiveNode.builder("TAIL-EXIT", TraderId.from_str("TAIL-001"), Environment.LIVE)
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
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if strategy.gateway is not None:
                        strategy.gateway.poll(time.time_ns())
                    if len(peer.creates) == 1 and any(r.client_order_id == "dust-entry" for r in strategy.receipts):
                        break
                    await asyncio.sleep(0.01)
                assert not peer.errors, peer.errors
                assert len(peer.wire_orders) == 1, strategy.receipts
                wire = peer.wire_orders[0]
                assert wire["BaseAmount"] == 600 and wire["ReduceOnly"] == 1
                assert wire["Price"] == 259 and wire["IsAsk"] == 0
                entry, = [r for r in strategy.receipts if r.client_order_id == "dust-entry"]
                assert entry.kind == "rejected" and entry.final_filled_quantity == 0
                assert entry.final_fill_source == "native_order_denied" and entry.native_status == "denied"
                assert not strategy.fills
                # The unacknowledged wire order remains uncertain. The local denial must not.
                strategy.gateway.poll(time.time_ns() + 30_000_000_000)
                await asyncio.sleep(0.05)
                assert len([r for r in strategy.receipts if r.client_order_id == "dust-entry"]) == 1
                assert any(r.client_order_id == "dust-close" and r.kind == "unknown" for r in strategy.receipts)
                assert len(peer.creates) == 1
            finally:
                if strategy.gateway is not None:
                    strategy.gateway.close()
                node.handle().stop()
                await asyncio.wait_for(task, 10)
    asyncio.run(scenario())

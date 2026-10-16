from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_native_execution import NativeOrderGateway, execution_clients
from opportunity_preflight import AccountSnapshot, OrderIntent


class NativeValue:
    @staticmethod
    def from_str(value):
        return value


MODEL = NS(ClientId=NativeValue, ClientOrderId=NativeValue, InstrumentId=NativeValue,
           Quantity=NativeValue, Price=NativeValue, OrderSide=NS(BUY="BUY", SELL="SELL"),
           TimeInForce=NS(GTC="GTC", IOC="IOC"))


class FakeStrategy:
    def __init__(self):
        self.cached = {}
        self.calls = []
        self.cache = NS(order=lambda cid: self.cached.get(cid),
            quote_tick=lambda iid: NS(ts_event=1_000_000_000, bid_price=D(100), ask_price=D(101)))
        self.order_factory = NS(limit=self.limit)

    def limit(self, **kwargs):
        order = NS(**kwargs, is_closed=False, filled_qty=D(0), status="INITIALIZED", venue_order_id=None)
        self.cached[order.client_order_id] = order
        return order

    def submit_order(self, order, **kwargs):
        self.calls.append(("submit", order.client_order_id, kwargs))

    def modify_order(self, cid, **kwargs):
        self.calls.append(("modify", cid, kwargs))

    def cancel_order(self, cid, **kwargs):
        self.calls.append(("cancel", cid, kwargs))

    def query_order(self, order, **kwargs):
        self.calls.append(("query", order.client_order_id, kwargs))


def intent(**kwargs):
    values = dict(action="submit", venue="HL", instrument_id="BTC-USD-PERP.HYPERLIQUID",
        request_id="submit-1", client_order_id="owned-1", side="BUY", quantity=D(".01"),
        price=D("100"), time_in_force="IOC")
    values.update(kwargs)
    return OrderIntent(**values)


@pytest.fixture
def harness():
    strategy, receipts, accounts, fills = FakeStrategy(), [], [], []
    markets = [NS(venue="HL", instrument_id="BTC-USD-PERP.HYPERLIQUID"),
               NS(venue="ASTER", instrument_id="BTCUSDT-PERP.ASTER"),
               NS(venue="ONDO", instrument_id="BTC-USD-PERP.ONDO"),
               NS(venue="BACKPACK", instrument_id="BTC_USDC_PERP.BACKPACK")]
    gateway = NativeOrderGateway(strategy, markets, receipts.append, accounts.append, fills.append, model=MODEL)
    yield gateway, strategy, receipts, accounts, fills
    gateway.close()


def event(**kwargs):
    values = dict(client_order_id="owned-1", instrument_id="BTC-USD-PERP.HYPERLIQUID")
    values.update(kwargs)
    return NS(**values)


def test_explicit_persisted_id_and_repeated_dispatch_do_not_resend(harness):
    gateway, strategy, receipts, _, _ = harness
    original = intent()
    gateway.dispatch(original, 1_000_000_000)
    gateway.dispatch(original, 1_000_000_000)
    assert len(strategy.calls) == 1 and D(strategy.cached["owned-1"].quantity) == D(".01")
    assert strategy.cached["owned-1"].client_order_id == "owned-1"
    with pytest.raises(ValueError, match="reused"):
        gateway.dispatch(replace(original, quantity=D(".02")), 1_000_000_000)


def test_partial_fill_cancel_terminal_waits_for_distinct_fills(harness):
    gateway, strategy, receipts, _, fills = harness
    gateway.dispatch(intent(), 1_000_000_000)
    gateway.dispatch(intent(action="cancel", request_id="cancel-1"), 1_000_000_001)
    cached = strategy.cached["owned-1"]
    cached.is_closed, cached.status, cached.filled_qty = True, "CANCELED", D(".007")
    gateway.on_native_event("canceled", event(), 1_000_000_002)
    assert receipts == []
    gateway.on_native_event("filled", event(trade_id="trade-1", order_side="BUY",
        last_qty=D(".003"), last_px=D("100"), commission=None), 1_000_000_003)
    gateway.on_native_event("filled", event(trade_id="trade-1", order_side="BUY",
        last_qty=D(".003"), last_px=D("100"), commission=None), 1_000_000_004)
    assert [r.kind for r in receipts] == ["fill"]
    gateway.on_native_event("filled", event(trade_id="trade-2", order_side="BUY",
        last_qty=D(".004"), last_px=D("101"), commission=None), 1_000_000_005)
    assert [r.kind for r in receipts] == ["fill", "fill", "canceled", "terminal"]
    assert receipts[-2].request_id == "cancel-1" and receipts[-1].request_id == "submit-1"
    assert len(fills) == 2 and fills[0]["quantity"] == D(".003")
    gateway.poll(1_000_000_006)
    assert len(receipts) == 4


def test_conflicting_duplicate_fill_is_not_silently_accepted(harness):
    gateway, strategy, _, _, _ = harness
    gateway.dispatch(intent(), 1_000_000_000)
    gateway.on_native_event("filled", event(trade_id="x", order_side="BUY",
        last_qty=D(".001"), last_px=D(100)), 1_000_000_001)
    with pytest.raises(ValueError, match="conflicting"):
        gateway.on_native_event("filled", event(trade_id="x", order_side="BUY",
            last_qty=D(".002"), last_px=D(100)), 1_000_000_002)


def test_accepted_ioc_without_terminal_queries_original_id_without_resend(harness):
    gateway, strategy, receipts, _, _ = harness
    gateway.dispatch(intent(), 1_000_000_000)
    gateway.on_native_event("accepted", event(), 1_000_000_001)
    gateway.poll(11_000_000_001)
    assert [c[0] for c in strategy.calls] == ["submit", "query"]
    assert [r.kind for r in receipts] == ["accepted", "unknown"]


def test_modify_cancel_receipts_have_action_request_ids(harness):
    gateway, strategy, receipts, _, _ = harness
    gateway.dispatch(intent(time_in_force="GTC"), 1_000_000_000)
    gateway.dispatch(intent(action="modify", request_id="modify-1", price=D(99)), 1_000_000_001)
    gateway.on_native_event("OrderUpdated", event(), 1_000_000_002)
    assert receipts[-1].kind == "updated" and receipts[-1].request_id == "modify-1"
    gateway.on_native_event("OrderCancelRejected", event(), 1_000_000_003)
    assert len(receipts) == 1


@pytest.mark.parametrize("venue,iid", [("ASTER", "BTCUSDT-PERP.ASTER"),
    ("ONDO", "BTC-USD-PERP.ONDO"), ("BACKPACK", "BTC_USDC_PERP.BACKPACK")])
def test_native_cancel_replace_venues_do_not_dispatch_amend(harness, venue, iid):
    gateway, strategy, receipts, _, _ = harness
    gateway.dispatch(intent(venue=venue, instrument_id=iid, request_id="amend", action="modify"), 1_000_000_000)
    assert strategy.calls == [] and all(r.kind == "rejected" for r in receipts)
    assert "cancel/replace" in gateway.last_error


def test_continuous_execution_requires_durable_state_before_loading_credentials():
    with pytest.raises(ValueError, match="state directory"):
        execution_clients(NS(markets=[NS(venue="ONDO", instrument_id="BTC-USD-PERP.ONDO")]), environ={})


@pytest.mark.parametrize("cid", ["x" * 65, "pair:entry", "币种"])
def test_ondo_invalid_client_id_never_crosses_native_dispatch(harness, cid):
    gateway, strategy, receipts, _, _ = harness
    gateway.dispatch(intent(venue="ONDO", instrument_id="BTC-USD-PERP.ONDO", client_order_id=cid), 1_000_000_000)
    assert strategy.calls == [] and receipts[-1].kind == "rejected"


def test_only_independent_account_read_produces_authoritative_snapshot(harness):
    gateway, _, receipts, accounts, _ = harness
    snapshot = AccountSnapshot("HL", "BTC-USD-PERP.HYPERLIQUID", D(100), D(101), 1_000_000_000, D("-.01"))
    gateway.reader = NS(read=lambda *args: snapshot)
    gateway.dispatch(intent(action="connect", request_id="connect"), 1_000_000_000)
    for future in gateway.reads.values():
        future.result()
    gateway.poll(1_000_000_001)
    assert receipts[-1].kind == "connected" and accounts == []
    gateway.dispatch(intent(action="reconcile", request_id="reconcile"), 1_000_000_002)
    for future in gateway.reads.values():
        future.result()
    gateway.poll(1_000_000_003)
    assert accounts == [snapshot] and accounts[0].position_quantity == D("-.01")


def test_account_check_needs_no_market_subscription_or_current_quote(harness):
    gateway, _, receipts, accounts, _ = harness
    def no_quote(*args):
        raise AssertionError("whole-account queries must not read market prices")
    gateway.quote_provider = no_quote
    def read(market, bid, ask, now):
        assert bid is None and ask is None
        return AccountSnapshot(market.venue, market.instrument_id, None, None, now, D(0),
                               account_positions=(), account_open_orders=())
    gateway.reader = NS(read=read)
    gateway.dispatch(intent(action="reconcile", request_id="account-only", account_check=True), 1_000_000_000)
    for future in gateway.reads.values():
        future.result()
    gateway.poll(4_000_000_000)
    assert len(accounts) == 1 and accounts[0].book_observed_ns is None
    assert not receipts


def test_async_account_failure_carries_venue_retry_delay(harness):
    from opportunity_account_reader import AccountReadError
    gateway, strategy, receipts, accounts, _ = harness
    def read(*args):
        raise AccountReadError("rate limited", retry_after_secs=45)
    gateway.reader = NS(read=read)
    gateway.dispatch(intent(action="reconcile", request_id="rate-limit", account_check=True), 1_000_000_000)
    for future in gateway.reads.values():
        with pytest.raises(AccountReadError):
            future.result()
    gateway.poll(1_000_000_001)
    assert len(receipts) == 1 and receipts[0].kind == "unknown" and receipts[0].retry_after_secs == 45
    assert not accounts and not strategy.calls


def test_native_periodic_account_read_waits_for_request_fence_and_current_bbo(harness):
    import threading
    gateway, strategy, receipts, accounts, _ = harness
    iid = "BTC-USD-PERP.ONDO"
    captured = []
    payload = {"coverage_complete": True, "connected_ready": True, "snapshot_started_ns": 999_999_999}
    gateway.native_controls["ONDO"] = NS(production_trade_snapshot=lambda: payload)
    strategy.cache.instrument = lambda key: NS(size_increment=D(".001"), price_increment=D(1),
        min_quantity=None, min_notional=None, multiplier=D(1))
    release_read = threading.Event()
    def read(*args):
        assert release_read.wait(3)
        return AccountSnapshot("ONDO", iid, D(100), D(101), 1_100_000_000, D(0))
    gateway.reader = NS(capture_native_evidence=lambda *args, **kw: captured.append(args),
        register_native_instrument=lambda *args, **kw: None,
        read=read)
    gateway.dispatch(intent(action="reconcile", venue="ONDO", instrument_id=iid, request_id="native-read"), 1_000_000_000)
    assert gateway.native_reads and not gateway.reads and not captured
    payload["snapshot_started_ns"] = 1_100_000_000
    gateway.poll(1_100_000_001)
    release_read.set()
    for future in gateway.reads.values():
        future.result()
    gateway.poll(3_100_000_001)  # Account is complete, but original BBO is now stale.
    assert not accounts and gateway.reads
    gateway.quote_provider = lambda *args: (D(102), D(103), 3_100_000_001)
    gateway.poll(3_100_000_002)
    assert len(accounts) == 1
    assert accounts[0].observed_ns == 1_100_000_000
    assert accounts[0].book_observed_ns == 3_100_000_001
    assert (accounts[0].bid, accounts[0].ask) == (D(102), D(103))


def test_native_startup_without_account_cycle_remains_pending(harness):
    gateway, _, receipts, accounts, _ = harness
    gateway.native_controls["ONDO"] = NS(production_trade_snapshot=lambda: None)
    gateway.dispatch(intent(action="connect", venue="ONDO", instrument_id="BTC-USD-PERP.ONDO"), 1_000_000_000)
    gateway.poll(2_000_000_000)
    assert gateway.native_reads and not receipts and not accounts
    gateway.poll(11_000_000_001)
    assert [receipt.kind for receipt in receipts] == ["unknown"]


def test_native_account_budget_is_separate_from_order_deadline(harness):
    gateway, _, receipts, _, _ = harness
    gateway.account_timeout_ns = 20_000_000_000
    gateway.native_controls["ONDO"] = NS(production_trade_snapshot=lambda: None)
    gateway.dispatch(intent(action="connect", venue="ONDO", instrument_id="BTC-USD-PERP.ONDO",
        request_id="account-read", client_order_id=""), 1_000_000_000)
    gateway.dispatch(intent(request_id="original-order"), 1_000_000_000)
    gateway.poll(11_000_000_001)
    assert [(receipt.request_id, receipt.kind) for receipt in receipts] == [("original-order", "unknown")]
    assert gateway.timeout_ns == 10_000_000_000
    gateway.poll(21_000_000_001)
    assert [(receipt.request_id, receipt.kind) for receipt in receipts] == [("original-order", "unknown"), ("account-read", "unknown")]


def test_restart_missing_cache_never_constructs_or_resends_pending_order(harness):
    gateway, strategy, receipts, _, _ = harness
    assert gateway.restore_pending(intent(), [], 1_000_000_000) is False
    assert strategy.calls == [] and strategy.cached == {}
    assert receipts[-1].kind == "unknown"
    gateway.dispatch(intent(), 1_000_000_001)
    assert strategy.calls == []


def test_restart_cached_original_is_only_queried_and_missing_fills_block_terminal(harness):
    gateway, strategy, receipts, _, _ = harness
    strategy.cached["owned-1"] = NS(client_order_id="owned-1", instrument_id="BTC-USD-PERP.HYPERLIQUID",
        filled_qty=D(".003"), is_closed=True, status="FILLED", venue_order_id="77")
    assert gateway.restore_pending(intent(), [], 1_000_000_000) is True
    gateway.poll(1_000_000_001)
    assert [c[0] for c in strategy.calls] == ["query"]
    assert [r.kind for r in receipts] == ["unknown"]


def test_restart_decimal_strings_deduplicate_native_replay(harness):
    gateway, strategy, receipts, _, fills = harness
    strategy.cached["owned-1"] = NS(client_order_id="owned-1", instrument_id="BTC-USD-PERP.HYPERLIQUID",
        filled_qty=D(".003"), is_closed=True, status="FILLED", venue_order_id="77")
    saved = dict(client_order_id="owned-1", trade_id="trade-1", instrument_id="BTC-USD-PERP.HYPERLIQUID",
        venue="HL", side="BUY", native_qty=".003", native_price="100", commission_amount=None,
        commission_currency=None, received_ns=100)
    gateway.restore_pending(intent(), [saved], 1_000_000_000)
    gateway.on_native_event("filled", event(trade_id="trade-1", order_side="BUY",
        last_qty=D(".003"), last_px=D("100")), 1_000_000_001)
    assert fills == []
    gateway.poll(1_000_000_002)
    assert [r.kind for r in receipts] == ["unknown", "terminal"]


def test_real_pinned_order_factory_constructs_exact_ioc_without_submission(harness):
    from nautilus_trader.common import Clock, OrderFactory
    from nautilus_trader.model import ClientOrderId, InstrumentId, OrderSide, Price, Quantity, StrategyId, TimeInForce, TraderId
    factory = OrderFactory(TraderId.from_str("TEST-001"), StrategyId.from_str("TEST-001"), Clock.new_test())
    order = factory.limit(instrument_id=InstrumentId.from_str("BTC-USD-PERP.HYPERLIQUID"),
        order_side=OrderSide.BUY, quantity=Quantity.from_str("0.00013"), price=Price.from_str("60123.4"),
        time_in_force=TimeInForce.IOC, post_only=False, reduce_only=True,
        client_order_id=ClientOrderId.from_str("fixture-owned-1"))
    assert order.quantity.as_decimal() == D("0.00013") and order.price.as_decimal() == D("60123.4")
    assert order.is_reduce_only and str(order.client_order_id) == "fixture-owned-1"


def test_real_native_filled_event_money_commission_is_exact(harness):
    from nautilus_trader.core import UUID4
    from nautilus_trader.model import (AccountId, ClientOrderId, Currency, InstrumentId, LiquiditySide,
        Money, OrderFilled, OrderSide, OrderType, Price, Quantity, StrategyId, TradeId, TraderId, VenueOrderId)
    gateway, strategy, receipts, _, fills = harness
    gateway.dispatch(intent(), 1_000_000_000)
    cached = strategy.cached["owned-1"]
    cached.is_closed, cached.status, cached.filled_qty = True, "FILLED", D(".01")
    native_event = OrderFilled(trader_id=TraderId.from_str("TEST-001"), strategy_id=StrategyId.from_str("TEST-001"),
        instrument_id=InstrumentId.from_str("BTC-USD-PERP.HYPERLIQUID"),
        client_order_id=ClientOrderId.from_str("owned-1"), venue_order_id=VenueOrderId.from_str("77"),
        account_id=AccountId.from_str("HYPERLIQUID-001"), trade_id=TradeId.from_str("fixture-trade"),
        order_side=OrderSide.BUY, order_type=OrderType.LIMIT, last_qty=Quantity.from_str(".01"),
        last_px=Price.from_str("100"), currency=Currency.from_str("USDC"), liquidity_side=LiquiditySide.TAKER,
        event_id=UUID4(), ts_event=1_000_000_001, ts_init=1_000_000_001, reconciliation=False,
        commission=Money.from_str("0.000003 USDC"))
    gateway.on_native_event("OrderFilled", native_event, 1_000_000_002)
    assert fills[0]["commission_amount"] == D(".000003")
    assert fills[0]["commission_currency"] == "USDC"
    assert [r.kind for r in receipts] == ["fill", "terminal"]


def test_ioc_remainder_cancel_reports_terminal_original_submit(harness):
    gateway, strategy, receipts, _, _ = harness
    gateway.dispatch(intent(), 1_000_000_000)
    cached = strategy.cached["owned-1"]
    cached.is_closed, cached.status, cached.filled_qty = True, "CANCELED", D(0)
    gateway.on_native_event("canceled", event(), 1_000_000_001)
    assert [(r.request_id, r.kind) for r in receipts] == [("submit-1", "terminal")]


@pytest.mark.parametrize("venue,iid", [("ONDO", "BTC-USD-PERP.ONDO"), ("BACKPACK", "BTC_USDC_PERP.BACKPACK")])
def test_extra_venues_use_same_native_strategy_route(harness, venue, iid):
    gateway, strategy, receipts, _, _ = harness
    gateway.dispatch(intent(venue=venue, instrument_id=iid), 1_000_000_000)
    gateway.dispatch(intent(venue=venue, instrument_id=iid, action="cancel", request_id="cancel"), 1_000_000_001)
    assert [row[0] for row in strategy.calls] == ["submit", "cancel"]
    assert all(row[2]["client_id"] == venue for row in strategy.calls)
    assert not hasattr(gateway, "external_drivers")


def test_actual_five_venue_builder_preserves_native_config_and_caps_offline(tmp_path):
    import os
    from unittest.mock import patch
    from eth_account import Account
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import TraderId
    from nautilus_trader.adapters import backpack as bp
    from backpack_production_peer import SEED, PUBLIC
    # Public deterministic test credentials. No ambient environment is visible
    # while factories resolve credentials, and the node is never run/connected.
    key = "0x" + "11"*32
    address = Account.from_key(key).address
    fake = {"ASTER_MAINNET_SIGNER_PRIVATE_KEY": key, "ASTER_MAINNET_SIGNER_ADDRESS": address,
        "ASTER_MAINNET_USER_ADDRESS": address, "HYPERLIQUID_PK": key,
        "HYPERLIQUID_ACCOUNT_ADDRESS": address, "LIGHTER_API_KEY_INDEX": "2",
        "LIGHTER_ACCOUNT_INDEX": "42", "LIGHTER_API_SECRET": "01" + "00"*39,
        "ONDO_MAINNET_API_KEY": "ondoKeyId_NATIVE_TEST_ONLY", "ONDO_MAINNET_API_SECRET": "ondoApiSecret_NATIVE_TEST_ONLY",
        "ONDO_MAINNET_ACCOUNT_ID": "fixture-account",
        "BACKPACK_MAINNET_API_SECRET": SEED, "BACKPACK_MAINNET_API_KEY": PUBLIC}
    scan = NS(markets=[NS(venue="ASTER", instrument_id="BTCUSDT-PERP.ASTER"),
        NS(venue="HL", instrument_id="BTC-USD-PERP.HYPERLIQUID"),
        NS(venue="LIGHTER", instrument_id="BTC-USD-PERP.LIGHTER"),
        NS(venue="ONDO", instrument_id="BTC-USD-PERP.ONDO"), NS(venue="ONDO", instrument_id="SOL-USD-PERP.ONDO"),
        NS(venue="BACKPACK", instrument_id="BTC_USDC_PERP.BACKPACK"), NS(venue="BACKPACK", instrument_id="SOL_USDC_PERP.BACKPACK")])
    quota = bp.BackpackQuota()
    economics = bp.BackpackInstrumentEconomics("1", "1", "0.01", "0.01", "Configured", "offline configured bounds")
    public = bp.BackpackDataClientConfig(["BTC_USDC_PERP", "SOL_USDC_PERP"],
        {symbol: economics for symbol in ["BTC_USDC_PERP", "SOL_USDC_PERP"]})
    controls = {}
    with patch.dict(os.environ, fake, clear=True):
        builder = LiveNode.builder("OFFLINE-CREDENTIAL-TEST", TraderId.from_str("TEST-001"), Environment.LIVE)
        builder = builder.with_logging(LoggerConfig(stdout_level=LogLevel.OFF, fileout_level=LogLevel.OFF,
                                                    print_config=False))
        configs = execution_clients(scan, environ=fake, state_directory=tmp_path, run_id="fixture-continuous",
            backpack_public_config=public, backpack_quota=quota, native_controls=controls)
        assert next(c for name, _, c in configs if name == "ASTER").has_explicit_credentials()
        ondo = next(c for name, _, c in configs if name == "ONDO")
        assert ondo.continuous_execution.max_notional_per_order_usd == D(100)
        assert ondo.continuous_execution.max_gross_exposure_usd == D(500)
        assert len(ondo.continuous_execution.instrument_ids) == 2
        backpack = next(c for name, _, c in configs if name == "BACKPACK")
        assert backpack.authority.max_order_notional == "100" and backpack.authority.max_reserved_notional == "500"
        assert set(controls) == {"ONDO", "BACKPACK"}
        builder = builder.add_data_client("BACKPACK", bp.BackpackDataClientFactory(quota=quota), public)
        for values in configs:
            builder = builder.add_exec_client(*values)
        node = builder.build()
        assert node is not None  # Deliberately no node.run(), start(), submit or account read.

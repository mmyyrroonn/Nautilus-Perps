"""One program: all venue startup workflows, then actual-receipt pair trading.

This test uses an in-memory exchange transport, never sockets or credentials.
The real coordinator, preflight, ledger and durable session proxy run unchanged.
"""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from opportunity_core import BookSnapshot, MarketMetadata
from opportunity_live import LiveTradeState
from opportunity_live_runner import LiveCoordinator, SessionStoreProxy
from opportunity_preflight import AccountSnapshot, BtcPreflight, ExecutionReceipt, PreflightMarket
from opportunity_trade import TradePolicy
from test_opportunity_live import MemoryStore, leverage_snapshot
from test_opportunity_trade import NOW, make_plan


VENUES = ("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK")


class ExchangeTransport:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.position = {venue: D(0) for venue in VENUES}
        self.orders, self.sent, self.pending = {}, [], []
        self.now = NOW
        self.first_live_startup = None
        self.balance_queries = []

    def query_balance(self, venue):
        self.balance_queries.append(venue)

    def read_balance(self, venue, now_ns, max_age_ms):
        return {"venue": venue, "observed_ns": now_ns, "balances": [
            {"currency": "USDC", "total": "100", "free": "100", "locked": "0"}]}

    def dispatch(self, intent, now_ns):
        if self.coordinator.plan.startup_balance_check:
            assert self.coordinator.balance_check.result == "passed"
        self.sent.append(intent)
        self.pending.append(intent)
        if intent.client_order_id.startswith("L") and self.first_live_startup is None:
            self.first_live_startup = (self.coordinator.preflight.document() if self.coordinator.preflight else
                                       {"result": "disabled"})

    def poll(self, now_ns):
        self.now = now_ns
        work, self.pending = self.pending, []
        c = self.coordinator
        for intent in work:
            receipt = lambda kind: c.on_receipt(ExecutionReceipt(intent.request_id, intent.client_order_id, kind), now_ns)
            if intent.action == "connect":
                receipt("connected")
            elif intent.action == "reconcile":
                current = tuple(cid for cid, order in self.orders.items() if order.venue == intent.venue)
                c.on_account(AccountSnapshot(intent.venue, intent.instrument_id, D("99.99"), D("100"),
                    now_ns, self.position[intent.venue], current,
                    aster_leverage=leverage_snapshot(c.live.plan.markets, now_ns)
                        if intent.venue == "ASTER" else None), now_ns)
            elif intent.action == "modify":
                self.orders[intent.client_order_id] = intent
                receipt("updated")
            elif intent.action == "cancel":
                self.orders.pop(intent.client_order_id)
                receipt("canceled")
            elif intent.action == "submit":
                if intent.post_only:
                    self.orders[intent.client_order_id] = intent
                    receipt("accepted")
                    continue
                receipt("accepted")
                self.position[intent.venue] += intent.quantity if intent.side == "BUY" else -intent.quantity
                payload = {"client_order_id": intent.client_order_id, "trade_id": intent.client_order_id+"-fill",
                    "instrument_id": intent.instrument_id, "venue": intent.venue, "side": intent.side,
                    "native_qty": intent.quantity, "native_price": intent.price,
                    "commission_amount": D(0), "commission_currency": "USD"}
                c.on_fill(payload, now_ns)
                c.on_receipt(ExecutionReceipt(intent.request_id, intent.client_order_id, "fill",
                    intent.client_order_id+"-fill", intent.quantity), now_ns)
                receipt("terminal")


@pytest.mark.parametrize("startup_self_test", [False, True])
@pytest.mark.parametrize("startup_balance_check", [False, True])
def test_five_startup_checks_are_integrated_before_continuous_roundtrip(startup_self_test, startup_balance_check):
    scan = make_plan(venues=VENUES)
    store = MemoryStore()
    proxy = SessionStoreProxy(store)
    live = LiveTradeState(scan, TradePolicy(), max_pairs=1, max_gross_notional_usd="500", store=proxy,
        aster_leverage_targets={m.instrument_id: 5 for m in scan.markets if m.venue == "ASTER"})
    markets = []
    for market in scan.markets:
        live.register_instrument(market.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(market.instrument_id, MarketMetadata(market.instrument_id, D("0.01"),
            taker_fee_bps=D(0), fee_source="offline fixture", min_quantity=D("0.01"), min_notional=D(5)), NOW, evaluate=False)
        mode = "native" if market.venue in {"HL", "LIGHTER"} else "cancel_replace"
        markets.append(PreflightMarket(market.venue, market.instrument_id, D("0.01"), D("0.01"),
            D("0.01"), D(5), supports_modify=mode == "native", modify_mode=mode))
    plan = SimpleNamespace(scan=replace(scan, connection_timeout_secs=120), duration_secs=0, drain_timeout_secs=30,
                           venues=VENUES, trade_symbols=("BTC",), startup_self_test=startup_self_test,
                           startup_balance_check=startup_balance_check, startup_timeout_secs=20, account_max_age_ms=15000)
    stopped = []
    coordinator = LiveCoordinator(plan, live, proxy, stop=lambda: stopped.append(True))
    transport = ExchangeTransport(coordinator)
    preflight = BtcPreflight(markets, session_id="fixture", slippage_bps=D(20)) if startup_self_test else None
    coordinator.attach(transport, preflight)

    def books(now_ns, *, closing=False):
        for market in scan.markets:
            bid, ask = (D("100.3"), D("100.31")) if market.venue == "ASTER" else (D("99.99"), D("100"))
            if closing and market.venue == "ASTER":
                bid, ask = D("100.10"), D("100.11")
            live.update_book(market.instrument_id, BookSnapshot(market.venue, "BTC",
                ((bid, D(100)),), ((ask, D(100)),), now_ns, now_ns), now_ns, evaluate=False)

    now = NOW
    for _ in range(200):
        now += 100_000_000
        books(now)
        coordinator.tick(now)
        if coordinator.phase == "active":
            break
        assert not live.positions
        assert not any(intent.client_order_id.startswith("L") for intent in transport.sent)
    assert coordinator.phase == "active"
    assert transport.balance_queries == (list(VENUES) if startup_balance_check else [])
    assert proxy.balance_check["result"] == ("passed" if startup_balance_check else "disabled")
    if startup_self_test:
        assert preflight.result == "passed"
        assert {r["venue"] for r in preflight.results} == set(VENUES)
    else:
        assert coordinator.preflight is None and proxy.account_gate["result"] == "passed"
        assert all(intent.action == "reconcile" for intent in transport.sent)
    assert all(q == 0 for q in transport.position.values())
    assert proxy.startup_complete == startup_self_test and not stopped
    for _ in range(10):
        now += 100_000_000
        books(now)
        live.evaluate_symbol("BTC", now)
        coordinator.tick(now)
        if any(p["status"] == "open" for p in live.positions.values()):
            break
    assert len(live.positions) == 1
    assert transport.first_live_startup["result"] == ("passed" if startup_self_test else "disabled")
    assert all(o["filled_quantity"] != "0" for o in live.orders.values())
    for _ in range(30):
        now += 100_000_000
        books(now, closing=True)
        coordinator.tick(now)
        if live.completed:
            break
    assert live.completed == 1 and not live.positions and not stopped
    assert all(q == 0 for q in transport.position.values())
    assert any(e["event"] == "startup_operation" for _, events in store.writes for e in events) == startup_self_test
    assert any(e["event"] == "closed" for _, events in store.writes for e in events)
    assert store.saved["live"]["mode"] == "live" and store.saved["startup_complete"] == startup_self_test
    startup_mutations = [i for i in transport.sent if i.client_order_id.startswith("fixture")]
    for _ in range(10):
        now += 100_000_000
        books(now)
        live.evaluate_symbol("BTC", now)
        coordinator.tick(now)
        if any(p["status"] == "open" for p in live.positions.values()):
            break
    assert len(live.positions) == 1
    for _ in range(30):
        now += 100_000_000
        books(now, closing=True)
        coordinator.tick(now)
        if live.completed == 2:
            break
    assert live.completed == 2 and not live.positions
    assert [i for i in transport.sent if i.client_order_id.startswith("fixture")] == startup_mutations
    assert all(i.time_in_force == "IOC" and not i.post_only for i in transport.sent
               if i.action == "submit" and i.client_order_id.startswith("L"))

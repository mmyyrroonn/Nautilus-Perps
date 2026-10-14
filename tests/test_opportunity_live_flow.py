"""One program: all venue startup workflows, then actual-receipt pair trading.

This test uses an in-memory exchange transport, never sockets or credentials.
The real coordinator, preflight, ledger and durable session proxy run unchanged.
"""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
from types import SimpleNamespace
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"src"))
from opportunity_core import BookSnapshot, MarketMetadata
from opportunity_live import LiveTradeState
from opportunity_live_runner import LiveCoordinator, SessionStoreProxy
from opportunity_preflight import AccountSnapshot, BtcPreflight, ExecutionReceipt, PreflightMarket
from opportunity_trade import TradePolicy
from test_opportunity_live import MemoryStore
from test_opportunity_trade import NOW, make_plan


VENUES = ("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK")


class ExchangeTransport:
    def __init__(self, coordinator):
        self.coordinator = coordinator
        self.position = {venue: D(0) for venue in VENUES}
        self.orders, self.sent, self.pending = {}, [], []
        self.now = NOW
        self.first_live_startup = None

    def dispatch(self, intent, now_ns):
        self.sent.append(intent)
        self.pending.append(intent)
        if intent.client_order_id.startswith("L") and self.first_live_startup is None:
            self.first_live_startup = self.coordinator.preflight.document()

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
                    now_ns, self.position[intent.venue], current), now_ns)
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


def test_five_startup_checks_are_integrated_before_continuous_roundtrip():
    scan = make_plan(venues=VENUES)
    store = MemoryStore()
    proxy = SessionStoreProxy(store)
    live = LiveTradeState(scan, TradePolicy(), max_pairs=1, max_gross_notional_usd="500", store=proxy)
    markets = []
    for market in scan.markets:
        live.register_instrument(market.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(market.instrument_id, MarketMetadata(market.instrument_id, D("0.01"),
            taker_fee_bps=D(0), fee_source="offline fixture", min_quantity=D("0.01"), min_notional=D(5)), NOW, evaluate=False)
        mode = "native" if market.venue in {"HL", "LIGHTER"} else "cancel_replace"
        markets.append(PreflightMarket(market.venue, market.instrument_id, D("0.01"), D("0.01"),
            D("0.01"), D(5), supports_modify=mode == "native", modify_mode=mode))
    plan = SimpleNamespace(scan=replace(scan, connection_timeout_secs=120), duration_secs=0, drain_timeout_secs=30)
    stopped = []
    coordinator = LiveCoordinator(plan, live, proxy, stop=lambda: stopped.append(True))
    transport = ExchangeTransport(coordinator)
    preflight = BtcPreflight(markets, session_id="fixture", slippage_bps=D(20))
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
    assert coordinator.phase == "active" and preflight.result == "passed"
    assert {r["venue"] for r in preflight.results} == set(VENUES)
    assert all(q == 0 for q in transport.position.values())
    assert proxy.startup_complete and not stopped
    for _ in range(10):
        now += 100_000_000
        books(now)
        live.evaluate_symbol("BTC", now)
        coordinator.tick(now)
        if any(p["status"] == "open" for p in live.positions.values()):
            break
    assert len(live.positions) == 1
    assert transport.first_live_startup["result"] == "passed"
    assert all(o["filled_quantity"] != "0" for o in live.orders.values())
    for _ in range(30):
        now += 100_000_000
        books(now, closing=True)
        coordinator.tick(now)
        if live.completed:
            break
    assert live.completed == 1 and not live.positions and not stopped
    assert all(q == 0 for q in transport.position.values())
    assert any(e["event"] == "startup_operation" for _, events in store.writes for e in events)
    assert any(e["event"] == "closed" for _, events in store.writes for e in events)
    assert store.saved["live"]["mode"] == "live" and store.saved["startup_complete"]

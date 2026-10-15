"""Offline native callback integration for paper position lifetimes."""
from fractions import Fraction
from dataclasses import replace
from pathlib import Path

import pytest

from test_opportunity_scan import NS, batch, native_observer, runtime
from test_opportunity_coalescing import replace_touch
from opportunity_paper import PaperTradeState
from opportunity_trade import TradePolicy, load_trade_plan


def test_paper_state_builds_with_all_five_public_native_clients_without_connecting():
    pytest.importorskip("nautilus_trader.adapters.backpack")
    root = Path(__file__).resolve().parents[1]
    trade = load_trade_plan(root / "config/opportunity-trade.paper.toml")
    scan = replace(trade.scan, markets=tuple(m for m in trade.scan.markets if m.symbol == "BTC"))
    node, observer, state = runtime.build_node(scan,
        state_factory=lambda plan: PaperTradeState(plan, trade.policy))
    assert not observer.running and not state.books and not state.positions
    assert len({m.venue for m in scan.markets}) == 5
    assert state.document()["orders_sent"] == 0


def test_native_timer_requests_close_without_quotes_and_recovers_on_fresh_depth(tmp_path, monkeypatch):
    events = []
    monkeypatch.setattr(runtime, "ScanState",
                        lambda plan: PaperTradeState(plan, TradePolicy(), event_sink=events.append))
    _, state, observer, ids, calls, _ = native_observer(
        tmp_path, monkeypatch, evaluation_interval_ms=100)
    now = [NS]
    monkeypatch.setattr(runtime.time, "time_ns", lambda: now[0])
    observer.on_book_deltas(batch(ids[0], "99.99", "100.00"))
    observer.on_book_deltas(batch(ids[1], "100.60", "100.61"))
    assert not state.positions
    now[0] += 100_000_000
    observer.on_time_event(None)
    assert observer.failure is None
    assert len(state.positions) == 1
    opened = next(iter(state.positions.values()))["opened_ns"]
    quantity = next(iter(state.positions.values()))["entry"]["sizing"]["base_quantity"]

    now[0] = opened + 299_900_000_000
    observer.on_time_event(None)
    assert next(iter(state.positions.values()))["status"] == "open"
    now[0] = opened + 300_000_000_000
    observer.on_time_event(None)
    assert observer.failure is None
    position = next(iter(state.positions.values()))
    assert position["status"] == "exit_pending"
    assert position["exit_reason"] == "max_hold"
    assert len([event for event in events if event["event"] == "closed"]) == 0

    now[0] += 100_000_000
    observer.on_book_deltas(replace_touch(ids[0], "99.99", "100.00", "99.99", "100.00", now[0]))
    observer.on_book_deltas(replace_touch(ids[1], "100.60", "100.61", "100.60", "100.61", now[0]))
    observer.on_time_event(None)
    assert observer.failure is None
    assert not state.positions
    assert state.sequence == state.completed == 1
    closed = [event for event in events if event["event"] == "closed"]
    assert len(closed) == 1 and closed[0]["reason"] == "max_hold"
    assert closed[0]["base_quantity"] == quantity
    assert Fraction(closed[0]["mark"]["net_usd"]) < 0
    assert state.document()["orders_sent"] == 0
    assert "stop" not in calls

"""Immutable account conversion must never cache dynamic trading admission."""
from dataclasses import replace
from decimal import Decimal as D
import sqlite3

import pytest

from test_opportunity_live import open_pair, state
from test_opportunity_trade import NOW, SECOND, publish
from opportunity_preflight import AccountSnapshot
from opportunity_trade_store import TradeStore
import opportunity_timing as timing
from test_opportunity_timing import rows


def proof(now=NOW, quantity="0", orders=()):
    return AccountSnapshot("HL", "BTC.HL", None, None, now, D(quantity),
        account_positions=(("BTC.HL", D(quantity)), ("OUTSIDE.HL", D(0))),
        account_open_orders=orders)


def test_account_conversion_cache_changes_with_payload_not_timestamp():
    live, _ = state()
    live.on_account(proof(), NOW)
    assert live._account_problem("BTC.HL", NOW) is None
    cached = live.account_position_values[("HL", "")]
    live.on_account(proof(quantity="1"), NOW)
    assert live._account_problem("BTC.HL", NOW) == "position_mismatch"
    assert live.account_position_values[("HL", "")][0] is not cached[0]
    for n in range(1, 100):
        live.on_account(proof(NOW + n), NOW + n)
    assert len(live.account_position_values) == 1
    assert "account_position_values" not in live.snapshot()


@pytest.mark.parametrize("change,reason", [
    ("fence", "predates_activity"), ("age", "stale_or_future"),
    ("disconnect", "not_authoritative"), ("authority", "not_authoritative"),
    ("order", "unowned_order"), ("inventory", "position_mismatch")])
def test_cached_quantities_still_check_all_dynamic_constraints(change, reason):
    live, _ = state()
    live.on_account(proof(), NOW)
    assert live._account_problem("BTC.HL", NOW) is None
    cached = live.account_position_values[("HL", "")]
    now = NOW
    if change == "fence":
        live.fences["BTC.HL"] = NOW + 1
    elif change == "age":
        now += live.account_max_age_ns + 1
    elif change in {"disconnect", "authority"}:
        live.accounts["BTC.HL"] = replace(live.accounts["BTC.HL"], **{
            "connected" if change == "disconnect" else "authoritative": False})
    elif change == "order":
        live.accounts["BTC.HL"] = replace(live.accounts["BTC.HL"], account_open_orders=(("BTC.HL", "foreign"),))
    else:
        open_pair(live)
        now = NOW + SECOND
        live.accounts["BTC.HL"] = replace(proof(now), account_positions=cached[0])
    assert live._account_problem("BTC.HL", now) == reason


def test_qualified_timings_exclude_unprofitable_checks(monkeypatch):
    live, _ = state()
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    publish(live, sell_bid="99.99", sell_ask="100")
    assert "entry.qualified_pricing" not in rows(recorder)
    publish(live)
    measured = rows(recorder)
    for name in ("entry.qualified_pricing", "entry.executable_bounds", "entry.account_checks",
                 "entry.prepare_orders", "strategy.persist_intents"):
        assert measured[name]["count"] == 1
    document = recorder.document()
    traces = document["entry_execution"]["traces"]
    assert {trace.pop("client_order_id") for trace in traces} == set(live.orders)
    assert not any(cid in str(document) for cid in live.orders)


def test_sqlite_serialization_and_commit_remain_atomic_and_full(tmp_path):
    store = TradeStore(tmp_path / "state.sqlite3", "synthetic")
    try:
        store.save({"version": 1}, [{"event": "first", "observed_ns": NOW}])
        assert store.connection.execute("PRAGMA synchronous").fetchone()[0] == 2
        with pytest.raises(TypeError):
            store.save({"bad": object()}, [{"event": "not_committed", "observed_ns": NOW}])
        assert store.load() == {"version": 1}
        assert store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
        store.connection.execute("CREATE TRIGGER reject_update BEFORE UPDATE ON state "
                                 "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
        with pytest.raises(sqlite3.IntegrityError):
            store.save({"version": 2}, [{"event": "rolled_back", "observed_ns": NOW}])
        assert store.load() == {"version": 1}
        assert store.connection.execute("SELECT count(*) FROM events").fetchone()[0] == 1
    finally:
        store.close()

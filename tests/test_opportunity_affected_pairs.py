"""Incremental admission must retain shared-risk and non-price invalidations."""
from dataclasses import replace
from decimal import Decimal as D

import pytest

from test_opportunity_live import accounts, state, submissions
from test_opportunity_trade import NOW, make_plan, set_book
from test_opportunity_scan import NS, batch, native_observer
from test_opportunity_coalescing import moving_clock, replace_touch


VENUES = ("HL", "ASTER", "LIGHTER", "BACKPACK", "ONDO")


def flat_state(symbols=("BTC",)):
    live, _ = state(make_plan(symbols=symbols, venues=VENUES))
    for symbol in symbols:
        for venue in VENUES:
            set_book(live, venue, "99", "100", symbol=symbol)
        live.evaluate_symbol(symbol, NOW)
    return live


def test_one_changed_book_checks_eight_directions_instead_of_twenty(monkeypatch):
    live = flat_state()
    checks = []
    monkeypatch.setattr(live, "_entry", lambda b, s, n: checks.append((b, s)))
    live.evaluate_symbol("BTC", NOW + 1, changed_ids={"BTC.HL"})
    assert len(checks) == 8
    assert all("BTC.HL" in pair for pair in checks)
    checks.clear()
    live.evaluate_symbol("BTC", NOW + 2, changed_ids={"BTC.HL", "BTC.ASTER"})
    assert len(checks) == 14 and len(set(checks)) == 14


@pytest.mark.parametrize("transition", ["account", "pause", "native_rules", "order"])
def test_non_market_changes_recheck_unchanged_pairs_and_symbols(monkeypatch, transition):
    live = flat_state(("BTC", "ETH"))
    if transition == "account":
        accounts(live, NOW + 1)
    elif transition == "pause":
        live.set_entry_pause("test", NOW + 1)
        assert not live.pending_evaluation_symbols()
        live.set_entry_pause(None, NOW + 2)
    elif transition == "native_rules":
        live.register_instrument("BTC.HL", D("0.1"), D(1), "USD")
    else:
        live.emit("order_terminal", NOW + 1)
    assert live.pending_evaluation_symbols() == {"BTC", "ETH"}
    checks = []
    monkeypatch.setattr(live, "_entry", lambda b, s, n: checks.append((b, s)))
    for symbol in sorted(live.pending_evaluation_symbols()):
        live.evaluate_symbol(symbol, NOW + 3, changed_ids={"BTC.HL"})
    assert len(checks) == 40
    assert not live.pending_evaluation_symbols()


def test_owned_inventory_forces_shared_risk_recheck(monkeypatch):
    live = flat_state(("BTC", "ETH"))
    set_book(live, "ASTER", "101", "102")
    live.evaluate_symbol("BTC", NOW + 1, changed_id="BTC.ASTER")
    assert live.positions
    live.full_evaluation_symbols.clear()
    set_book(live, "HL", "98", "99", NOW + 2)
    assert live.pending_evaluation_symbols() == {"BTC", "ETH"}
    checks = []
    monkeypatch.setattr(live, "observe", lambda *a: None)
    monkeypatch.setattr(live, "_entry", lambda b, s, n: checks.append((b, s)))
    live.evaluate_symbol("BTC", NOW + 3, changed_id="BTC.HL")
    assert len(checks) == 20


@pytest.mark.parametrize("trigger", ["quantity", "depth", "metadata", "status", "account"])
def test_same_event_replay_matches_full_scan_for_non_price_changes(trigger):
    states = [flat_state(), flat_state()]
    for live in states:
        live.session_id = "deterministic"
        # Only HL/ASTER can qualify; other venues are excluded by the allowlist.
        live.allowed_pairs = {frozenset(("BTC.HL", "BTC.ASTER"))}
        set_book(live, "ASTER", "101", "102", bid_size="0.001")
        if trigger == "metadata":
            live.metadata["BTC.HL"] = replace(live.metadata["BTC.HL"], taker_fee_bps=D(1000))
        if trigger == "account":
            accounts(live, NOW, connected=False)
        live.evaluate_symbol("BTC", NOW)
        live.drain_intents()
    for step in range(1, 7):
        now = NOW + step * 1_000_000
        changed = {"BTC.HL" if step % 2 else "BTC.ASTER"}
        for index, live in enumerate(states):
            if step == 4:
                changed = {"BTC.ASTER"}
                book = live.books["BTC.ASTER"]
                bids = ((D(101), D(10)),) if trigger != "depth" else (
                    (D(101), D("0.001")), (D("100.9"), D(10)))
                live.update_book("BTC.ASTER", replace(book, bids=bids), now, evaluate=False)
                if trigger == "metadata":
                    changed.add("BTC.HL")
                    live.update_metadata("BTC.HL", replace(live.metadata["BTC.HL"], taker_fee_bps=D(0)),
                                         now, evaluate=False)
                elif trigger == "status":
                    live.invalidate("BTC.ASTER", now)
                    live.update_book("BTC.ASTER", replace(book, bids=bids), now, evaluate=False)
                elif trigger == "account":
                    accounts(live, now)
            live.evaluate_symbol("BTC", now, changed_ids=changed if index == 0 else None)
        assert states[0].snapshot() == states[1].snapshot()
        assert states[0].drain_intents() == states[1].drain_intents()
    assert states[0].positions


def test_observer_unions_books_metadata_and_status_before_evaluation(tmp_path, monkeypatch):
    _, scan, observer, ids, _, _ = native_observer(tmp_path, monkeypatch, evaluation_interval_ms=100)
    now = moving_clock(monkeypatch)
    for instrument in ids:
        observer.on_book_deltas(batch(instrument, "99.00", "100.00"))
    now[0] += 100_000_000
    observer.on_time_event(None)
    calls = []
    monkeypatch.setattr(scan, "evaluate_symbol", lambda s, n, **kw: calls.append(kw))
    observer.on_book_deltas(replace_touch(ids[0], "99.00", "100.00", "99.00", "100.00", now[0]))
    observer.on_instrument(observer.cache.instrument(ids[1]))
    now[0] += 100_000_000
    observer.on_time_event(None)
    assert calls == [{"changed_id": None, "changed_ids": {str(i) for i in ids}}]
    assert not observer.pending_instruments and observer.failure is None

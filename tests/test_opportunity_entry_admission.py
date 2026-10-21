"""Exact handoff budgets, durable identity and recovery; no venue connections."""
from dataclasses import replace
from decimal import Decimal as D
from fractions import Fraction as F
import json
from pathlib import Path
import sys

import pytest

from test_opportunity_live import MemoryStore, accounts, fill, receipt, state
from test_opportunity_entry_evidence import coordinator
from test_opportunity_live_runner import configs
from test_opportunity_trade import NOW, SECOND, publish, set_book
from opportunity_entry_admission import EntryAdmissionPolicy
from opportunity_entry_evidence import entry_dispatch_evidence
from opportunity_live_runner import LiveCoordinator, load_live_plan
from opportunity_scan import ScanConfigError
import opportunity_timing as timing

MS = 1_000_000


@pytest.fixture
def clock(monkeypatch):
    tick = [0]
    monkeypatch.setattr("opportunity_live.time.perf_counter_ns", lambda: tick[0])
    return tick


def update_times(live, venue, source, received, published=NOW):
    instrument = "BTC." + venue
    book = replace(live.books[instrument], ts_event_ns=source, ts_received_ns=received)
    live.update_book(instrument, book, published, evaluate=False)


@pytest.mark.parametrize("budget,limit,reason", [
    ("source", 500, "entry_source_age"), ("receive", 250, "entry_receive_age"),
    ("skew", 250, "entry_book_skew"), ("publication", 150, "entry_publication_wait"),
    ("dispatch", 100, "entry_dispatch_expired")])
@pytest.mark.parametrize("offset", [-1, 0, 1])
def test_exact_nanosecond_boundaries(clock, budget, limit, reason, offset):
    live, _ = state()
    publish(live)
    delta = limit * MS + offset
    if budget == "source":
        for venue in ("HL", "ASTER"):
            update_times(live, venue, NOW - delta, NOW)
    elif budget == "receive":
        for venue in ("HL", "ASTER"):
            update_times(live, venue, NOW - delta, NOW - delta, NOW - delta)
    elif budget == "skew":
        update_times(live, "HL", NOW - delta, NOW)
    elif budget == "publication":
        for venue in ("HL", "ASTER"):
            update_times(live, venue, NOW - delta, NOW - delta)
    else:
        for order in live.orders.values():
            order["sent_ns"] = NOW - delta
    sent = []
    LiveCoordinator._live_intents(coordinator(live, lambda i, *a, **k: sent.append(i)), NOW)
    assert len(sent) == (0 if offset > 0 else 2)
    for order in live.orders.values():
        decision = order["entry_dispatch"]
        assert decision["reason"] == (reason if offset > 0 else "valid")
        assert decision["result"] == ("blocked" if offset > 0 else "allowed")
        assert decision["evidence"]["thresholds"] == live.entry_admission.document()
        assert set(decision["evidence"]) >= {"buy", "sell", "dispatch_delay_ns", "source_skew_ns"}


@pytest.mark.parametrize("failure", ["future_source", "future_receive", "future_publish", "missing_publish", "replacement"])
def test_unknown_or_inconsistent_publication_never_sends(clock, failure):
    live, _ = state()
    publish(live)
    if failure == "future_source":
        update_times(live, "HL", NOW + 1, NOW)
    elif failure == "future_receive":
        update_times(live, "HL", NOW, NOW + 1)
    elif failure == "future_publish":
        update_times(live, "HL", NOW, NOW, NOW + 1)
    elif failure == "missing_publish":
        live.book_publications.pop("BTC.HL")
    else:
        live.books["BTC.HL"] = replace(live.books["BTC.HL"])
    sent = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: sent.append(a)), NOW)
    assert not sent
    assert all(order["status"] == "local_not_dispatched" for order in live.orders.values())


def test_publication_storage_is_bounded_and_invalidation_clears_identity(clock):
    live, _ = state()
    for index in range(100):
        set_book(live, "HL", "99.99", "100", NOW + index)
    assert len(live.book_publications) == 1
    live.invalidate("BTC.HL", NOW + 100)
    assert not live.book_publications


def test_callback_timestamp_cannot_hide_work_before_candidate_clock(clock, monkeypatch):
    live, _ = state()
    publish(live)
    monkeypatch.setattr("opportunity_live_runner.time.time_ns", lambda: NOW + 100 * MS + 1)
    sent = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: sent.append(a)), NOW)
    assert not sent
    assert all(o["entry_dispatch"]["reason"] == "entry_dispatch_expired" for o in live.orders.values())


@pytest.mark.parametrize("stage", ["prepare", "release", "economics"])
def test_slow_work_expires_without_optional_timing(clock, monkeypatch, stage):
    monkeypatch.setattr(timing, "_active", None)
    store = MemoryStore()
    live, _ = state(store=store)
    save = store.save
    def slow_save(snapshot, events):
        if events[0]["event"] == {"prepare": "orders_prepared", "release": "dispatch_released"}.get(stage):
            clock[0] += 100 * MS + 1
        save(snapshot, events)
    store.save = slow_save
    publish(live)
    if stage == "economics":
        limit = live._limit
        def slow_limit(*args, **kwargs):
            result = limit(*args, **kwargs)
            clock[0] += 60 * MS
            return result
        monkeypatch.setattr(live, "_limit", slow_limit)
    sent = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: sent.append(a)), NOW)
    assert not sent
    assert all(order["entry_dispatch"]["reason"] == "entry_dispatch_expired" for order in live.orders.values())
    assert not live.entry_clocks
    assert "monotonic" not in json.dumps(store.saved)


@pytest.mark.parametrize("recovery", ["unknown", "filled"])
def test_second_leg_expiry_keeps_first_id_and_existing_recovery(clock, recovery):
    live, _ = state()
    publish(live)
    original = set(live.orders)
    sent = []
    def dispatch(intent, *args, **kwargs):
        sent.append(intent)
        clock[0] += 100 * MS + 1
    LiveCoordinator._live_intents(coordinator(live, dispatch), NOW)
    first, = sent
    first_order = live.orders[first.client_order_id]
    assert first_order["terminal_ns"] is None
    blocked = next(o for cid, o in live.orders.items() if cid != first.client_order_id)
    assert blocked["status"] == "local_not_dispatched"
    assert blocked["entry_dispatch"]["reason"] == "entry_dispatch_expired"
    assert set(live.orders) == original
    now = NOW + SECOND
    if recovery == "unknown":
        from opportunity_native_execution import NativeOrderGateway
        from test_opportunity_native_execution import MODEL, FakeStrategy
        strategy = FakeStrategy()
        gateway = NativeOrderGateway(strategy, live.plan.markets, lambda r: live.on_receipt(r, now),
            lambda *a: None, lambda *a: None, model=MODEL, environ={})
        try:
            gateway.dispatch(first, NOW)
            gateway.poll(NOW + 11 * SECOND)
            assert ("query", first.client_order_id) in [c[:2] for c in strategy.calls]
        finally:
            gateway.close()
        live.on_receipt(receipt(first, "unknown"), now)
        live.on_evaluation_timer(now + 3 * SECOND)
        pending = live.drain_intents()
        assert any(i.action == "reconcile" for i in pending)
        assert first_order["terminal_ns"] is None
        assert not any(i.action == "submit" and not i.reduce_only for i in pending)
    else:
        fill(live, first, now)
        accounts(live, now + 2)
        set_book(live, "HL", "99.99", "100", now + 2)
        set_book(live, "ASTER", "100.3", "100.31", now + 2)
        live.on_evaluation_timer(now + 2)
        exits = []
        # Entry deadlines and age budgets must never stop reduce-only recovery.
        LiveCoordinator._live_intents(coordinator(live, lambda i, *a, **k: exits.append(i)), now + 2)
        unwind, = [i for i in exits if i.action == "submit"]
        assert unwind.reduce_only and unwind.instrument_id == first.instrument_id
        assert unwind.quantity == first.quantity
        assert next(iter(live.positions.values()))["exit_reason"] == "entry_imbalance"


def test_allowed_evidence_piggybacks_existing_receipt_and_block_has_both_legs(clock):
    store = MemoryStore()
    live, events = state(store=store)
    publish(live)
    writes = len(store.writes)
    sent = []
    LiveCoordinator._live_intents(coordinator(live, lambda i, *a, **k: sent.append(i)), NOW)
    # Only the existing dispatch-release transaction, no new send-time write.
    assert len(store.writes) == writes + 1
    live.on_receipt(receipt(sent[0], "accepted"), NOW + 1)
    assert events[-1]["entry_dispatch"]["result"] == "allowed"
    assert all("entry_dispatch" in o for o in store.saved["orders"].values())


@pytest.mark.parametrize("slip,allowed", [("0", True), ("10", True), ("40", False)])
def test_slippage_still_requires_both_fees_and_net_profit(clock, slip, allowed):
    from opportunity_trade import TradePolicy
    live, _ = state(policy=TradePolicy(slippage_bps=D(slip)))
    for instrument, meta in live.metadata.items():
        live.update_metadata(instrument, replace(meta, taker_fee_bps=D(2)), NOW, evaluate=False)
    publish(live, sell_bid="101", sell_ask="101.01")
    assert bool(live.positions) is allowed
    if allowed:
        reason, evidence = entry_dispatch_evidence(live, next(iter(live.positions.values())), NOW)
        assert reason == "valid"
        net = F(evidence["sell"]["limit_cash_usd"]) - F(evidence["buy"]["limit_cash_usd"])
        net -= sum(F(evidence[leg]["fee_usd"]) for leg in ("buy", "sell")) + F(evidence["reserve_usd"])
        assert net == F(evidence["net_usd"]) >= F(evidence["minimum_net_usd"])


@pytest.mark.parametrize("bad", [0, -1, 2001, True, "500", 1.5])
def test_invalid_budgets_refuse_configuration(bad):
    with pytest.raises(ScanConfigError):
        EntryAdmissionPolicy(max_source_age_ms=bad)


def test_config_is_visible_tightens_scan_and_preserves_recovery_fingerprint(tmp_path):
    path = configs(tmp_path)
    baseline = load_live_plan(path)
    with path.open("a", encoding="utf-8") as handle:
        handle.write("\n[entry_admission]\nmax_source_age_ms = 200\nmax_dispatch_delay_ms = 50\n")
    plan = load_live_plan(path)
    assert plan.document()["entry_admission"]["max_source_age_ms"] == 200
    assert plan.fingerprint() == baseline.fingerprint()
    settings = replace(plan.scan.settings, max_age_ms=100, max_skew_ms=50)
    assert plan.entry_admission.thresholds(settings)["max_source_age_ms"] == 100
    assert plan.entry_admission.thresholds(settings)["max_skew_ms"] == 50
    with path.open("a", encoding="utf-8") as handle:
        handle.write("disable = true\n")
    with pytest.raises(ScanConfigError):
        load_live_plan(path)


def test_incident_projection_and_synthetic_replay_use_production_timing_gate():
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "scripts"))
    from replay_entry_admission import replay
    report = replay(json.loads((root / "tests/fixtures/entry-admission-20261020.json").read_text()))
    assert report["sensitivity"]["legacy_timing_projection"] == dict(checks_blocked=0, pairs_blocked_before_first_send=0)
    assert report["sensitivity"]["entry_default"] == dict(checks_blocked=8, pairs_blocked_before_first_send=4)
    assert report["sensitivity"]["source_500_only"]["pairs_blocked_before_first_send"] == 4
    assert report["sensitivity"]["source_750_only"]["pairs_blocked_before_first_send"] == 0
    for row in report["historical"]:
        assert row["decisions"]["entry_default"]["unmeasured"] == ["publication_wait"]
    for row in report["synthetic"][:15]:
        assert row["result"] == ("blocked" if "+1ns" in row["case"] else "allowed")
    assert [row["result"] for row in report["synthetic"][15:]] == ["allowed"] * 3 + ["blocked"] * 2

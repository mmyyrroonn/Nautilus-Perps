"""Durability, coalescing and timer fallback for event-driven intent dispatch."""
from types import SimpleNamespace

import pytest

from test_opportunity_live import MemoryStore, state
from test_opportunity_trade import NOW, publish
from test_opportunity_live_runner import configs, load_live_plan
from opportunity_wakeup import IntentWakeup


def test_durable_intents_schedule_one_async_wakeup_and_can_rearm():
    store = MemoryStore()
    live, _ = state(store=store)
    alerts = []
    clock = SimpleNamespace(timestamp_ns=lambda: NOW,
        set_time_alert_ns=lambda *a, **kw: alerts.append((a, kw)), cancel_timer=lambda name: None)
    wakeup = IntentWakeup(clock, lambda e: None)
    observed = []
    def notify():
        observed.append((store.saved["sequence"], len(live.outbox), store.writes[-1][1][0]["event"]))
        wakeup.request()
    live.intent_notifier = notify
    publish(live)
    assert observed == [(1, 2, "orders_prepared")]
    assert wakeup.pending and len(alerts) == 1
    for _ in range(100):
        wakeup.request()
    assert len(alerts) == 1 and len(live.outbox) == 2
    assert alerts[0][1]["allow_past"] is True
    assert wakeup.consume() and not wakeup.consume()
    wakeup.request()
    assert len(alerts) == 2
    wakeup.close()
    assert not wakeup.consume()
    wakeup.request()
    assert len(alerts) == 2


def test_failed_persistence_never_notifies_or_releases_intents():
    store = MemoryStore()
    live, _ = state(store=store)
    notifications = []
    live.intent_notifier = lambda: notifications.append(True)
    def fail(*a):
        raise OSError("synthetic disk failure")
    store.save = fail
    with pytest.raises(OSError):
        publish(live)
    assert not notifications and not live.outbox and not live.cleanup_allowed


def test_failed_optional_alert_leaves_durable_orders_for_timer():
    def fail(*a, **kw):
        raise RuntimeError("clock stopped")
    live, _ = state()
    wakeup = IntentWakeup(SimpleNamespace(timestamp_ns=lambda: NOW, set_time_alert_ns=fail), None)
    live.intent_notifier = wakeup.request
    publish(live)
    assert len(live.drain_intents()) == 2 and not wakeup.pending


@pytest.mark.parametrize("interval", [0, 20, 100])
def test_evaluation_schedule_is_independent_of_risk_timer_and_fingerprint(tmp_path, interval):
    path = configs(tmp_path)
    baseline = load_live_plan(path)
    path.write_text(path.read_text().replace("[runtime]", f"[runtime]\nevaluation_interval_ms = {interval}"))
    plan = load_live_plan(path)
    assert plan.scan.evaluation_interval_ms == interval
    assert plan.timer_interval_ms == baseline.timer_interval_ms
    assert plan.fingerprint() == baseline.fingerprint()


@pytest.mark.parametrize("interval", ["true", "-1", "1001", "20.5"])
def test_invalid_evaluation_schedule_is_rejected(tmp_path, interval):
    path = configs(tmp_path)
    path.write_text(path.read_text().replace("[runtime]", f"[runtime]\nevaluation_interval_ms = {interval}"))
    with pytest.raises(ValueError):
        load_live_plan(path)

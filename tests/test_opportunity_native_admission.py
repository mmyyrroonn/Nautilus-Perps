"""Fail-closed, pair-wide admission using the real coordinator and gateway."""
from dataclasses import replace
import json
from types import SimpleNamespace as NS

import pytest

from test_opportunity_live import accounts, fill, state
from test_opportunity_trade import NOW, SECOND, make_plan, set_book
from test_opportunity_native_execution import FakeStrategy, MODEL
from opportunity_live_runner import LiveCoordinator
from opportunity_native_execution import NativeOrderGateway


def status(ready=True, reason=None, checked_ns=NOW):
    return {"BACKPACK": {"ready": ready, "reason": reason, "checked_ns": checked_ns}}


def prepared(*, backpack_first=False):
    live, events = state(make_plan(venues=("HL", "BACKPACK")))
    live.update_native_admission(status(), NOW)
    set_book(live, "HL", "100.3" if backpack_first else "99.99",
             "100.31" if backpack_first else "100")
    set_book(live, "BACKPACK", "99.99" if backpack_first else "100.3",
             "100" if backpack_first else "100.31")
    live.evaluate_symbol("BTC", NOW)
    assert len(live.outbox) == 2
    return live, events


def run_dispatch(live, refresh, calls, *, dispatch=None, now=NOW):
    coordinator = NS(live=live, proxy=NS(restart_pending=False),
        gateway=NS(refresh_native_admission=refresh,
                   dispatch=dispatch or (lambda intent, stamp, **kw: calls.append(intent))))
    LiveCoordinator._live_intents(coordinator, now)


@pytest.mark.parametrize("backpack_first", [False, True])
@pytest.mark.parametrize("result", [
    status(False, "native quote cache is empty"), {},
    status(checked_ns=NOW - 2 * SECOND), status(checked_ns=NOW + SECOND),
    status(False, "native_admission_api_error"), status(False, "native_admission_invalid_json"),
])
def test_known_not_ready_blocks_both_legs(result, backpack_first):
    live, events = prepared(backpack_first=backpack_first)
    live.native_admission.clear()
    calls = []
    run_dispatch(live, lambda now: result, calls)
    assert calls == []
    assert all(o["status"] == "local_not_dispatched" for o in live.orders.values())
    blocked = [e for e in events if e["event"] == "entry_dispatch_blocked"]
    assert len(blocked) == 1
    assert blocked[0]["reason"] == "native_admission_not_ready"
    assert len(blocked[0]["client_order_ids"]) == 2


def test_second_leg_rechecks_and_preserves_first_order_and_real_fill():
    live, _ = prepared()
    calls = []
    def refresh(now):
        return status(not calls, "native quote expired" if calls else None, now)
    def dispatch(intent, now, **kw):
        calls.append(intent)
        fill(live, intent, now, terminal=False)
    run_dispatch(live, refresh, calls, dispatch=dispatch)
    assert len(calls) == 1
    sent = live.orders[calls[0].client_order_id]
    assert sent["terminal_ns"] is None
    assert sent["filled_quantity"] == sent["quantity"]
    assert sum(o["status"] == "local_not_dispatched" for o in live.orders.values()) == 1
    assert next(iter(live.positions.values()))["remaining_buy_base"] != "0"


def test_slow_admission_read_cannot_reuse_expired_books(monkeypatch):
    live, _ = prepared()
    tick = [0]
    monkeypatch.setattr("opportunity_live_runner.time.perf_counter_ns", lambda: tick[0])
    def refresh(now):
        tick[0] += 3 * SECOND
        return status(checked_ns=now)
    calls = []
    run_dispatch(live, refresh, calls)
    assert not calls


def test_missing_admission_blocks_preparation_and_recovery_reevaluates_without_old_ids():
    live, events = prepared()
    calls = []
    run_dispatch(live, lambda now: status(False, "missing quote", now), calls)
    old_ids = set(live.orders)
    live.on_evaluation_timer(NOW + 1)
    assert not live.positions
    # Native readiness alone never lifts account/loss pauses.
    live.set_entry_pause("session_loss_check", NOW + 2)
    live.update_native_admission(status(checked_ns=NOW + 2), NOW + 2)
    assert live.entry_pause_reason == "session_loss_check"
    assert not live.native_entry_problem(("BTC.HL", "BTC.BACKPACK"), NOW + 2)
    live.set_entry_pause(None, NOW + 3)
    now = NOW + 10_000_000
    accounts(live, now)
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "BACKPACK", "100.3", "100.31", now)
    live.evaluate_symbol("BTC", now)
    run_dispatch(live, lambda stamp: status(checked_ns=stamp), calls, now=now)
    assert len(calls) == 2
    assert not old_ids.intersection(i.client_order_id for i in calls)
    assert any(e["event"] == "native_admission_recovered" for e in events)


def test_no_result_or_expired_ready_prevents_preparing_new_orders():
    live, _ = state(make_plan(venues=("HL", "BACKPACK")))
    set_book(live, "HL", "99.99", "100")
    set_book(live, "BACKPACK", "100.3", "100.31")
    live.evaluate_symbol("BTC", NOW)
    assert not live.positions and not live.outbox
    live.update_native_admission(status(checked_ns=NOW - 2 * SECOND), NOW)
    live.evaluate_symbol("BTC", NOW)
    assert not live.positions and not live.outbox
    live.update_native_admission(status(), NOW)
    assert "BTC" in live.pending_evaluation_symbols()
    live.evaluate_symbol("BTC", NOW)
    assert len(live.outbox) == 2


def test_non_backpack_pair_and_cancel_reduce_reconcile_ignore_admission():
    from test_opportunity_trade import publish
    live, _ = state()
    publish(live)
    live.update_native_admission(status(False, "missing quote"), NOW)
    calls = []
    def forbidden(now):
        raise AssertionError("unrelated operation queried Backpack admission")
    run_dispatch(live, forbidden, calls)
    assert len(calls) == 2
    entry = calls[0]
    close = replace(entry, request_id="reduce", client_order_id="reduce", reduce_only=True)
    live.orders["reduce"] = dict(live.orders[entry.client_order_id],
                                purpose="exit", dispatch_started_ns=NOW)
    others = [replace(entry, action="cancel", request_id="cancel"),
              replace(entry, action="reconcile", request_id="reconcile"), close]
    live.outbox.extend(others)
    run_dispatch(live, forbidden, calls)
    assert calls[-3:] == others


def test_backpack_admission_failure_preserves_real_fill_driven_exit():
    live, _ = prepared()
    entries = []
    run_dispatch(live, lambda now: status(checked_ns=now), entries)
    for entry in entries:
        fill(live, entry, NOW + 1)
    now = NOW + SECOND + 1
    accounts(live, now)
    live.on_evaluation_timer(now)
    live.drain_intents()
    assert next(iter(live.positions.values()))["status"] == "open"
    set_book(live, "HL", "99.99", "100", now)
    set_book(live, "BACKPACK", "100.3", "100.31", now)
    live.update_native_admission(status(False, "unrelated quote missing", now), now)
    live.stop_entries(now)
    live.on_evaluation_timer(now)
    exits = []
    def forbidden(stamp):
        raise AssertionError("exit must not query the new-entry gate")
    run_dispatch(live, forbidden, exits, now=now)
    assert len(exits) == 2 and all(intent.reduce_only for intent in exits)
    assert {intent.venue for intent in exits} == {"HL", "BACKPACK"}
    for intent in exits:
        fill(live, intent, now + 1)
    accounts(live, now + 3)
    live.on_evaluation_timer(now + 3)
    assert not live.positions and live.completed == 1


@pytest.fixture
def native_gateway():
    control = NS(renew_authority=lambda *args: None, persist_economics=lambda: None,
                 reconcile_terminal_evidence=lambda: None,
                 admission_status_json=lambda: '{"ready":true,"reason":null}')
    gateway = NativeOrderGateway(FakeStrategy(), [NS(venue="BACKPACK", instrument_id="BTC.BACKPACK")],
        lambda r: None, lambda a: None, lambda f: None, model=MODEL,
        native_controls={"BACKPACK": control}, environ={},
        backpack_public_config=NS(telemetry_snapshot_json=lambda: json.dumps({
            "connected": True, "metadata_ready": True, "connection_epoch": 1,
            "quotes_fresh": {"BTC": True}})))
    yield gateway, control
    gateway.close()


@pytest.mark.parametrize("payload,reason", [
    ('{"ready":false,"reason":"native quote cache is empty"}', "native quote cache is empty"),
    ('null', "native_admission_invalid_json"), ('[]', "native_admission_invalid_json"),
    ('{', "native_admission_invalid_json"), ('{}', "native_admission_invalid_json"),
    ('{"ready":1}', "native_admission_invalid_json"),
    ('{"ready":true}', "native_admission_invalid_json"),
    ('{"ready":true,"reason":"failure"}', "native_admission_invalid_json"),
    ('{"ready":false}', "native_admission_invalid_json"),
    (None, "native_admission_invalid_json"),
])
def test_gateway_parses_and_retains_failures(native_gateway, payload, reason):
    gateway, control = native_gateway
    control.admission_status_json = lambda: payload
    gateway.poll(NOW)
    assert gateway.native_admission == status(False, reason)


def test_gateway_api_error_checkpoint_failure_missing_control_and_recovery(native_gateway):
    gateway, control = native_gateway
    gateway.poll(NOW)
    assert gateway.native_admission == status()
    def fail():
        raise RuntimeError("private transport detail")
    control.admission_status_json = fail
    assert gateway.refresh_native_admission(NOW) == status(False, "native_admission_api_error")
    control.admission_status_json = lambda: '{"ready":true,"reason":null}'
    control.persist_economics = fail
    gateway.poll(NOW)
    assert gateway.native_admission == status(False, "native_checkpoint_not_ready")
    assert gateway.refresh_native_admission(NOW) == gateway.native_admission
    control.persist_economics = lambda: None
    gateway.poll(NOW + 1)
    assert gateway.native_admission == status(checked_ns=NOW + 1)
    gateway.native_controls.clear()
    assert gateway.refresh_native_admission(NOW) == status(False, "native_admission_unavailable")
    gateway.close()
    assert gateway.refresh_native_admission(NOW) == status(False, "native_gateway_closed")


@pytest.mark.parametrize("health,reason", [
    (None, "native_public_health_unavailable"),
    ({}, "native_admission_invalid_json"),
    ({"connected": False, "metadata_ready": True, "quotes_fresh": {"BTC": True}},
     "native_public_session_not_ready"),
    ({"connected": True, "metadata_ready": False, "quotes_fresh": {"BTC": True}},
     "native_public_session_not_ready"),
    ({"connected": True, "metadata_ready": True, "quotes_fresh": {}}, "native_public_quotes_not_ready"),
    ({"connected": True, "metadata_ready": True, "quotes_fresh": {"BTC": False}},
     "native_public_quotes_not_ready"),
])
def test_public_quote_health_fences_even_when_preparation_api_is_ready(native_gateway, health, reason):
    gateway, _ = native_gateway
    gateway.backpack_public_config = None if health is None else NS(
        telemetry_snapshot_json=lambda: json.dumps(health))
    gateway.poll(NOW)
    assert gateway.native_admission == status(False, reason)


def test_every_symbol_in_scope_must_have_fresh_public_quote(native_gateway):
    gateway, _ = native_gateway
    gateway.markets["SOL.BACKPACK"] = NS(venue="BACKPACK", instrument_id="SOL.BACKPACK")
    gateway.poll(NOW)
    assert gateway.native_admission == status(False, "native_public_quotes_not_ready")
    assert gateway.backpack_market_health["unavailable_symbols"] == ["SOL"]
    assert gateway.backpack_market_health["scope_markets"] == 2
    gateway.backpack_public_config.telemetry_snapshot_json = lambda: json.dumps({
        "connected": True, "metadata_ready": True, "quotes_fresh": {"BTC": True, "SOL": True}})
    gateway.poll(NOW)
    assert gateway.native_admission == status()

"""Issue 94: two-leg gates, safe diagnostics and original-ID recovery."""
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_account_reader import ReadOnlyAccountReader, AccountReadError
from opportunity_leverage import parse_leverage, leverage_check
from opportunity_live_runner import LiveCoordinator
from opportunity_native_execution import classify_rejection, exchange_error_code
from opportunity_preflight import ExecutionReceipt
from test_opportunity_live import state, submissions, MemoryStore, leverage_snapshot
from test_opportunity_entry_evidence import coordinator
from test_opportunity_trade import NOW, SECOND, publish
from test_opportunity_native_execution import harness, intent, event


def payloads(actual="5", maximum=20):
    return ([{"symbol": "STXUSDT", "positionSide": "BOTH", "positionAmt": "0",
              "markPrice": "0.3365", "leverage": actual}],
            [{"symbol": "STXUSDT", "brackets": [
                {"initialLeverage": maximum, "notionalFloor": 0, "notionalCap": 1000},
                {"initialLeverage": 2, "notionalFloor": 1000, "notionalCap": 10000}]}])


def check(snapshot, target=5, now=NOW, quantity="295", rejected=0):
    return leverage_check(snapshot, "STXUSDT-PERP.ASTER", target, D("0.3365"), D(quantity),
                          D(1), now, 5 * SECOND, rejected)[0]


@pytest.mark.parametrize("actual,maximum,target,expected", [
    ("5", 20, 5, None), ("25", 20, 5, "leverage_above_maximum"),
    ("5", 3, 5, "leverage_above_maximum"),
    ("10", 20, 5, "leverage_target_mismatch"),
    ("5", 20, None, "leverage_target_pending")])
def test_actual_target_and_changed_maximum(actual, maximum, target, expected):
    assert check(parse_leverage(*payloads(actual, maximum), NOW), target) == expected


def test_freshness_tiers_and_post_rejection_fence():
    snapshot = parse_leverage(*payloads(), NOW)
    assert check(None) == "leverage_unavailable"
    assert check(snapshot, now=NOW - 1) == "leverage_stale"
    assert check(snapshot, now=NOW + 5 * SECOND + 1) == "leverage_stale"
    assert check(snapshot, quantity="3000") == "leverage_above_maximum"
    assert check(snapshot, quantity="30000") == "leverage_notional_unsupported"
    for quantity, expected in (("999", None), ("1000", "leverage_above_maximum")):
        assert leverage_check(snapshot, "STXUSDT-PERP.ASTER", 5, D(1), D(quantity), D(1),
                              NOW, 5 * SECOND)[0] == expected
    assert check(snapshot, rejected=NOW) == "leverage_recheck_required"
    assert check(replace(snapshot, started_ns=NOW + 1), now=NOW + 2, rejected=NOW) is None


@pytest.mark.parametrize("change", ["duplicate", "hedge", "gap", "overlap", "float", "missing", "nan"])
def test_malformed_proof_fails_closed(change):
    positions, brackets = payloads()
    if change == "duplicate":
        positions.append(positions[0].copy())
    elif change == "hedge":
        positions[0]["positionSide"] = "LONG"
    elif change == "gap":
        brackets[0]["brackets"][1]["notionalFloor"] = 1001
    elif change == "overlap":
        brackets[0]["brackets"][1]["notionalFloor"] = 999
    elif change == "float":
        positions[0]["leverage"] = 5.0
    elif change == "missing":
        positions[0].pop("leverage")
    else:
        positions[0]["markPrice"] = "NaN"
    with pytest.raises((ValueError, AccountReadError)):
        parse_leverage(positions, brackets, NOW)


def reader_fixture(fail_brackets=False):
    calls = []
    positions, brackets = payloads()
    clock = [NOW]
    def transport(method, url):
        path = url.split("?")[0].split(".com")[-1]
        calls.append((method, path))
        if path.endswith("positionRisk"):
            return positions
        if path.endswith("openOrders"):
            return []
        assert path.endswith("leverageBrackets")
        if fail_brackets:
            raise RuntimeError("synthetic-private-response")
        return brackets
    reader = ReadOnlyAccountReader(clock_ns=lambda: clock[0], transport=transport,
        environ={"ASTER_MAINNET_" + k: "synthetic" for k in
                 ("USER_ADDRESS", "SIGNER_ADDRESS", "SIGNER_PRIVATE_KEY")},
        aster_signer=lambda *args: "synthetic")
    return reader, calls, clock


def test_reader_uses_signed_read_only_bulk_evidence_and_preserves_cache_age():
    reader, calls, clock = reader_fixture()
    market = NS(venue="ASTER", instrument_id="STXUSDT-PERP.ASTER")
    snapshot = reader.read(market, None, None, NOW)
    assert check(snapshot.aster_leverage) is None
    clock[0] += 1
    cached = reader.read(market, None, None, NOW + 1)
    assert cached.aster_leverage.started_ns == NOW
    assert cached.aster_leverage is snapshot.aster_leverage
    assert calls == [("GET", "/fapi/v3/positionRisk"), ("GET", "/fapi/v3/openOrders"),
                     ("GET", "/fapi/v3/leverageBrackets")]
    reader.invalidate("ASTER")
    fresh = reader.read(market, None, None, NOW + 1)
    assert fresh.aster_leverage.started_ns == NOW + 1


def test_leverage_read_failure_preserves_reduction_account_proof():
    reader, _, _ = reader_fixture(fail_brackets=True)
    snapshot = reader.read(NS(venue="ASTER", instrument_id="STXUSDT-PERP.ASTER"), None, None, NOW)
    assert snapshot.authoritative and snapshot.position_quantity == 0
    assert snapshot.aster_leverage is None and snapshot.aster_leverage_error == "leverage_read_failed"
    assert "synthetic-private" not in repr(snapshot)


@pytest.mark.parametrize("failure,expected", [
    ("pending", "leverage_unavailable"), ("unknown", "leverage_unavailable"),
    ("stale", "leverage_stale"), ("mismatch", "leverage_target_mismatch"),
    ("cap", "leverage_above_maximum"), ("read", "leverage_read_failed")])
def test_pair_never_creates_either_leg_without_valid_leverage(failure, expected):
    live, events = state()
    if failure == "pending":
        live.aster_leverage_targets.clear()
        live.aster_leverage = None
    elif failure == "unknown":
        live.aster_leverage = None
    elif failure == "stale":
        live.aster_leverage = replace(live.aster_leverage, started_ns=NOW - 6 * SECOND)
    elif failure == "read":
        live.aster_leverage, live.aster_leverage_error = None, "leverage_read_failed"
    else:
        row = live.aster_leverage.symbols[0]
        row = replace(row, actual=10) if failure == "mismatch" else replace(
            row, brackets=((D(0), D(1000000), 3),))
        live.aster_leverage = replace(live.aster_leverage, symbols=(row,))
    publish(live)
    assert not submissions(live) and not live.orders
    assert any(e.get("reason") == expected for e in events)


def test_changed_cap_after_durability_blocks_both_sends():
    live, _ = state()
    publish(live)
    row = replace(live.aster_leverage.symbols[0], brackets=((D(0), D(1000000), 3),))
    live.aster_leverage = replace(live.aster_leverage, symbols=(row,))
    calls = []
    LiveCoordinator._live_intents(coordinator(live, lambda *a, **k: calls.append(a)), NOW)
    assert not [c for c in calls if c[0].action == "submit"]
    assert all(o["status"] == "local_not_dispatched" for o in live.orders.values())


def test_first_sent_id_survives_second_leg_block_and_restart():
    store = MemoryStore()
    live, _ = state(store=store)
    publish(live)
    calls = []
    def dispatch(order, now, **kwargs):
        if order.action == "submit":
            calls.append(order)
            live.aster_leverage = None
    LiveCoordinator._live_intents(coordinator(live, dispatch), NOW)
    assert len(calls) == 1
    cid = calls[0].client_order_id
    assert live.orders[cid]["terminal_ns"] is None
    assert any(o["status"] == "local_not_dispatched" for o in live.orders.values())
    restored, _ = state(store=MemoryStore(store.saved), activate=False)
    assert restored.orders[cid]["terminal_ns"] is None
    assert restored.phase == "recovery_blocked"
    assert not submissions(restored)


def test_permanent_rejection_latches_durably_until_new_valid_read():
    store = MemoryStore()
    live, events = state(store=store)
    publish(live)
    order = next(i for i in submissions(live) if i.venue == "ASTER")
    rejection = ExecutionReceipt(order.request_id, order.client_order_id, "configuration_rejected",
        reason_code="leverage_configuration", exchange_error_code=-2027)
    live.on_receipt(rejection, NOW + 1)
    assert live.orders[order.client_order_id]["terminal_ns"] is None
    assert store.saved["leverage_rejections"][order.instrument_id] == NOW + 1
    assert live.leverage_problem(order.instrument_id, order.price, order.quantity, NOW + 2)[0] == "leverage_recheck_required"
    live.on_receipt(replace(rejection, kind="rejected"), NOW + 2)
    assert store.saved["orders"][order.client_order_id]["terminal_diagnostic"]["exchange_error_code"] == -2027
    restored, _ = state(store=MemoryStore(store.saved), activate=False)
    assert restored.leverage_rejections[order.instrument_id] == NOW + 1
    live.aster_leverage = leverage_snapshot(live.plan.markets, NOW + 3)
    assert live.leverage_problem(order.instrument_id, order.price, order.quantity, NOW + 4)[0] is None
    assert order.instrument_id not in store.saved["leverage_rejections"]
    assert any(e["event"] == "leverage_admission_recovered" for e in events)


def test_native_leverage_rejection_keeps_code_and_pending_original_id(harness):
    gateway, strategy, receipts, _, _ = harness
    original = intent(venue="ASTER", instrument_id="BTCUSDT-PERP.ASTER")
    gateway.dispatch(original, NOW)
    reason = "submit-order-error: Aster error -2027 (HTTP 400): maximum leverage exceeded synthetic-private"
    gateway.on_native_event("rejected", event(instrument_id=original.instrument_id, reason=reason), NOW + 1)
    assert [r.kind for r in receipts] == ["configuration_rejected"]
    assert receipts[0].exchange_error_code == -2027
    assert "synthetic-private" not in repr(receipts) + repr(gateway.orders)
    gateway.poll(NOW + 11 * SECOND)
    assert [c[:2] for c in strategy.calls] == [("submit", "owned-1"), ("query", "owned-1")]
    strategy.cached["owned-1"].is_closed = True
    gateway.poll(NOW + 11 * SECOND + 1)
    assert receipts[-1].kind == "rejected"
    assert receipts[-1].exchange_error_code == -2027
    assert receipts[-1].reason_code == "leverage_configuration"


def test_code_parser_is_venue_scoped_and_does_not_keep_arbitrary_text():
    raw = "submit-order-error: Aster error -2027 (HTTP 400): rejected"
    assert exchange_error_code(raw, "ASTER") == -2027
    assert exchange_error_code(raw, "HL") is None
    assert exchange_error_code("id=-2027 synthetic", "ASTER") is None
    assert classify_rejection(raw, "ASTER") == "leverage_configuration"
    assert classify_rejection(raw, "HL") == "venue_other"


def test_plan_explicit_targets_and_recovery_fingerprint(tmp_path):
    from opportunity_live_runner import load_live_plan
    from test_opportunity_live_runner import configs
    path = configs(tmp_path)
    before = load_live_plan(path)
    assert before.aster_leverage_targets == ()
    assert before.document()["aster_leverage"]["automatic_targets"]
    assert before.document()["aster_leverage"]["default_target_source"] == "initial_verified_account"
    with path.open("a") as file:
        file.write('\n[aster_leverage]\nmax_age_ms = 3000\n[aster_leverage.targets]\n'
                   '"BTC-PERP.ASTER" = 5\n"ETH-PERP.ASTER" = 3\n')
    after = load_live_plan(path)
    assert dict(after.aster_leverage_targets) == {"BTC-PERP.ASTER": 5, "ETH-PERP.ASTER": 3}
    assert after.aster_leverage_max_age_ms == 3000
    assert after.fingerprint() == before.fingerprint()
    assert after.policy == before.policy and after.max_gross_notional_usd == before.max_gross_notional_usd


@pytest.mark.parametrize("value", ["true", "0", "126", '"5"', "5.0"])
def test_config_refuses_non_integer_or_out_of_range_target(tmp_path, value):
    from opportunity_live_runner import load_live_plan
    from opportunity_scan import ScanConfigError
    from test_opportunity_live_runner import configs
    path = configs(tmp_path)
    with path.open("a") as file:
        file.write(f'\n[aster_leverage.targets]\n"BTC-PERP.ASTER" = {value}\n')
    with pytest.raises(ScanConfigError):
        load_live_plan(path)


def test_account_failure_invalidates_previous_leverage_even_while_fresh():
    live, _ = state()
    aster = next(m.instrument_id for m in live.plan.markets if m.venue == "ASTER")
    live._request_account(aster, NOW + 1)
    request_id = live.account_request_ids[aster]
    live.on_receipt(ExecutionReceipt(request_id, "", "unknown"), NOW + 2)
    assert live.aster_leverage is None
    assert live.leverage_problem(aster, D(100), D(1), NOW + 3)[0] == "leverage_read_failed"


def test_complete_flat_restart_retains_rejection_fence(tmp_path):
    from test_opportunity_live_runner import readonly_coordinator
    coordinator_state, live, _, _, _ = readonly_coordinator(tmp_path)
    saved = coordinator_state.proxy._document(live.snapshot())
    instrument = next(m.instrument_id for m in live.plan.markets if m.venue == "ASTER")
    saved["live"]["leverage_rejections"] = {instrument: NOW}
    _, fresh_live, _, _, _ = readonly_coordinator(tmp_path, saved=saved)
    assert fresh_live.leverage_rejections == {instrument: NOW}
    assert fresh_live.aster_leverage is None


def test_leverage_block_does_not_disable_owned_reduce_only_cleanup():
    from test_opportunity_live import open_pair, accounts
    live, _ = state()
    position, _ = open_pair(live)
    now = NOW + SECOND
    live.aster_leverage_targets.clear()
    live.aster_leverage = None
    live.leverage_rejections[position["sell_id"]] = now
    live.stop_entries(now)
    accounts(live, now + 1)
    publish(live, now + 1)
    live.on_evaluation_timer(now + 1)
    orders = submissions(live)
    assert len(orders) == 2 and all(order.reduce_only for order in orders)
    assert live.leverage_rejections[position["sell_id"]] == now


def test_late_configuration_event_enriches_cached_terminal_without_reopening_order():
    store = MemoryStore()
    live, events = state(store=store)
    publish(live)
    order = next(i for i in submissions(live) if i.venue == "ASTER")
    live.on_receipt(ExecutionReceipt(order.request_id, order.client_order_id, "rejected",
        reason_source="native_cache"), NOW + 1)
    live.on_receipt(ExecutionReceipt(order.request_id, order.client_order_id, "configuration_rejected",
        reason_code="leverage_configuration", reason_source="native_event", native_status="rejected",
        exchange_error_code=-2027), NOW + 2)
    stored = store.saved["orders"][order.client_order_id]
    assert stored["terminal_ns"] == NOW + 1
    assert stored["terminal_diagnostic"]["exchange_error_code"] == -2027
    assert stored["terminal_diagnostic"]["reason_code"] == "leverage_configuration"
    assert store.saved["leverage_rejections"][order.instrument_id] == NOW + 2
    from opportunity_review import review_events
    report = review_events(events)["trades"][0]
    assert report["orders"][order.client_order_id]["result"]["exchange_error_code"] == -2027
    assert report["orders"][order.client_order_id]["result"]["outcome"] == "rejected"
    writes = len(store.writes)
    live.on_receipt(ExecutionReceipt(order.request_id, order.client_order_id, "configuration_rejected",
        reason_code="leverage_configuration", exchange_error_code=-2027), NOW + 3)
    assert len(store.writes) == writes


def test_startup_automatically_resolves_and_persists_targets_without_manual_config():
    store = MemoryStore()
    live, _ = state(store=store, aster_leverage_targets=())
    aster = next(m.instrument_id for m in live.plan.markets if m.venue == "ASTER")
    assert live.aster_leverage_targets == {aster: 5}
    assert live.aster_leverage_pending == set()
    evidence = store.saved["aster_leverage_initializations"][aster]
    assert evidence["target"] == 5 and evidence["allowed_maximum"] == 20
    assert evidence["source"] == "initial_account" and evidence["observed_ns"] == NOW
    publish(live)
    assert len(submissions(live)) == 2


def test_automatic_target_does_not_follow_later_account_leverage_changes():
    from opportunity_preflight import AccountSnapshot
    live, _ = state(aster_leverage_targets=())
    aster = next(m.instrument_id for m in live.plan.markets if m.venue == "ASTER")
    proof = replace(live.aster_leverage, started_ns=NOW + 1,
                    symbols=(replace(live.aster_leverage.symbols[0], actual=10),))
    live.on_account(AccountSnapshot("ASTER", aster, D(100), D(101), NOW + 1, D(0),
                                    aster_leverage=proof), NOW + 1)
    assert live.aster_leverage_targets[aster] == 5
    assert live.leverage_problem(aster, D(100), D(1), NOW + 2)[0] == "leverage_target_mismatch"


@pytest.mark.parametrize("failure", ["above_maximum", "stale", "missing"])
def test_initialization_never_adopts_invalid_current_settings(failure):
    from opportunity_live import LiveTradeState
    from opportunity_trade import TradePolicy
    from opportunity_preflight import AccountSnapshot
    from test_opportunity_trade import make_plan
    live = LiveTradeState(make_plan(), TradePolicy(), max_pairs=2, max_gross_notional_usd=D(500))
    aster = next(m.instrument_id for m in live.plan.markets if m.venue == "ASTER")
    live.register_instrument(aster, D(".01"), D(1), "USD")
    proof = leverage_snapshot(live.plan.markets, NOW)
    if failure == "above_maximum":
        proof = replace(proof, symbols=(replace(proof.symbols[0], actual=25),))
    elif failure == "stale":
        proof = replace(proof, started_ns=NOW - 6 * SECOND)
    else:
        proof = None
    live.on_account(AccountSnapshot("ASTER", aster, D(100), D(101), NOW, D(0),
                                    aster_leverage=proof), NOW)
    assert not live.aster_leverage_targets and not live.aster_leverage_initializations
    expected = {"above_maximum": "leverage_above_maximum", "stale": "leverage_stale",
                "missing": "leverage_unavailable"}[failure]
    assert live.leverage_problem(aster, D(100), D(1), NOW + 1)[0] == expected


def test_explicit_target_overrides_automatic_initialization():
    live, _ = state(aster_leverage_targets={"BTC.ASTER": 3})
    assert live.aster_leverage_targets["BTC.ASTER"] == 3
    assert not live.aster_leverage_initializations
    assert live.leverage_problem("BTC.ASTER", D(100), D(1), NOW)[0] == "leverage_target_mismatch"


def test_same_ledger_restores_automatic_target_but_not_freshness():
    from opportunity_live import LiveTradeState
    store = MemoryStore()
    live, _ = state(store=store, aster_leverage_targets=())
    restored = LiveTradeState(live.plan, live.policy, max_pairs=2,
        max_gross_notional_usd=D(500), store=MemoryStore(store.saved))
    assert restored.aster_leverage_targets == live.aster_leverage_targets
    assert restored.aster_leverage_initializations == live.aster_leverage_initializations
    assert restored.aster_leverage is None


def test_late_instrument_registration_resolves_from_existing_proof():
    from opportunity_live import LiveTradeState
    from opportunity_trade import TradePolicy
    from opportunity_preflight import AccountSnapshot
    from test_opportunity_trade import make_plan
    live = LiveTradeState(make_plan(), TradePolicy(), max_pairs=2, max_gross_notional_usd=D(500))
    aster = next(m.instrument_id for m in live.plan.markets if m.venue == "ASTER")
    live.on_account(AccountSnapshot("ASTER", aster, D(100), D(101), NOW, D(0),
        aster_leverage=leverage_snapshot(live.plan.markets, NOW)), NOW)
    assert not live.aster_leverage_targets
    live.register_instrument(aster, D(".01"), D(1), "USD")
    assert live.leverage_problem(aster, D(100), D(1), NOW + 1)[0] is None
    assert live.aster_leverage_targets[aster] == 5

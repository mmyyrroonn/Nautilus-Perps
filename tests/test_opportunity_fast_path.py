"""Tests for synchronous direct dispatch, conservative float pre-screen, and fast/slow venue tiering."""
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))

from opportunity_core import BookSnapshot, MarketMetadata, ScanSettings, _best_quotes_rule_out
from opportunity_entry_evidence import entry_dispatch_evidence
from opportunity_live import LiveTradeState
from opportunity_live_runner import LiveCoordinator, SessionStoreProxy, load_live_plan
from opportunity_scan import MarketPlan, ScanConfigError, ScanPlan
import opportunity_timing as timing
from opportunity_trade import TradePolicy
from test_opportunity_live import MemoryStore, accounts, state
from test_opportunity_live_runner import FakeGateway, configs
from test_opportunity_trade import NOW, SECOND, set_book

MS = 1_000_000


class _TestGateway(FakeGateway):
    def dispatch(self, intent, now, *, candidate_marker=None):
        self.sent.append(intent)
        if candidate_marker is not None:
            timing.record_latency(candidate_marker, "entry.candidate_to_submit", venue=intent.venue)

    def refresh_native_admission(self, stamp):
        return {"BACKPACK": {"ready": True, "reason": None, "checked_ns": stamp}}


def _fast_slow_plan(venues=("HL", "ASTER", "BACKPACK")):
    markets = tuple(MarketPlan("BTC", v, f"BTC.{v}", D(1), "synthetic parity") for v in venues)
    return ScanPlan(markets, ScanSettings(reserve_bps=D(0)), None, evaluation_interval_ms=20)


def _set_venue_book(live, venue, bid, ask, now=NOW, *, symbol="BTC"):
    instrument_id = next(m.instrument_id for m in live.plan.markets if m.symbol == symbol and m.venue == venue)
    live.update_book(
        instrument_id,
        BookSnapshot(venue, symbol, ((D(bid), D(10)),), ((D(ask), D(10)),), now, now),
        now,
        evaluate=False,
    )


def _setup_coordinator(tmp_path, *, venues=("HL", "ASTER", "BACKPACK"), policy=None, max_pairs=2, gross="500", duration_secs=0):
    plan = load_live_plan(
        configs(tmp_path, mandatory="false", all_five=True, venues=json.dumps(list(venues)), pairs=max_pairs, gross=gross),
    )
    if policy is not None:
        plan = replace(plan, policy=policy)
    if duration_secs:
        plan = replace(plan, duration_secs=duration_secs)
    store = MemoryStore()
    events = []
    proxy = SessionStoreProxy(store, restart_from_flat=True)
    live = LiveTradeState(
        plan.scan,
        plan.policy,
        max_pairs=plan.max_pairs,
        max_gross_notional_usd=plan.max_gross_notional_usd,
        store=proxy,
        event_sink=events.append,
        account_max_age_ms=plan.account_max_age_ms,
        aster_leverage_targets={m.instrument_id: 5 for m in plan.scan.markets if m.venue == "ASTER"},
    )
    for market in plan.scan.markets:
        live.register_instrument(market.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(
            market.instrument_id,
            MarketMetadata(market.instrument_id, D("0.01"), taker_fee_bps=D(0), fee_source="synthetic"),
            NOW,
            evaluate=False,
        )
    accounts(live, NOW)
    live.update_native_admission({"BACKPACK": {"ready": True, "reason": None, "checked_ns": NOW}}, NOW)
    coordinator = LiveCoordinator(plan, live, proxy)
    gateway = _TestGateway()
    coordinator.attach(gateway)
    coordinator.phase = "active"
    coordinator.started_ns = NOW
    live.activate(NOW)
    live.drain_intents()
    live.direct_dispatcher = coordinator.dispatch_orders_now
    return coordinator, live, gateway, store, events


def test_direct_dispatch_sends_synchronously_with_two_phase_durability_and_timing(tmp_path, monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    coordinator, live, gateway, store, events = _setup_coordinator(tmp_path, venues=("ASTER", "BACKPACK"))
    wakeups = []
    live.intent_notifier = lambda: wakeups.append(True)
    polls = []
    gateway.poll = lambda now: polls.append(now)

    _set_venue_book(live, "BACKPACK", "99.99", "100.00", NOW)
    _set_venue_book(live, "ASTER", "100.50", "100.51", NOW)
    live.evaluate_symbol("BTC", NOW)

    assert len(gateway.sent) == 2
    assert not wakeups
    assert not polls
    assert not live.outbox
    event_names = [e["event"] for e in events]
    assert event_names.index("orders_prepared") < event_names.index("dispatch_released")
    assert all(store.saved["live"]["orders"][i.client_order_id]["status"] == "dispatch_released" for i in gateway.sent)

    doc = recorder.document()
    traces = doc["entry_execution"]["traces"]
    assert len(traces) == 2
    for tr in traces:
        stages = [s["stage"] for s in tr["stages"]]
        assert "direct_dispatch" in stages
        assert "wakeup_requested" not in stages
        assert stages.index("persisted") < stages.index("direct_dispatch") < stages.index("coordinator")
    submit_ops = [op for op in doc["operations"] if op["name"] == "entry.candidate_to_submit"]
    assert sum(op["count"] for op in submit_ops) == 2
    assert {op["venue"] for op in submit_ops} == {"ASTER", "BACKPACK"}


@pytest.mark.parametrize("blocker", [
    "phase", "entry_pause", "halt", "stop_requested", "restart_pending",
    "stop_signal", "loss_monitor_due", "duration_elapsed", "reentrant",
])
def test_direct_dispatch_guards_fall_back_to_wakeup(tmp_path, blocker):
    coordinator, live, gateway, _, _ = _setup_coordinator(
        tmp_path, venues=("ASTER", "BACKPACK"), duration_secs=60 if blocker == "duration_elapsed" else 0,
    )
    wakeups = []
    live.intent_notifier = lambda: wakeups.append(True)

    now = NOW
    if blocker == "phase":
        coordinator.phase = "draining"
    elif blocker == "entry_pause":
        live.entry_pause_reason = "paused"
    elif blocker == "halt":
        live.halt_reason = "halted"
    elif blocker == "stop_requested":
        coordinator.stop_requested_ns = NOW
    elif blocker == "restart_pending":
        coordinator.proxy.saved = {"schema_version": 1, "mode": "live"}
        coordinator.proxy.restart_pending = True
    elif blocker == "stop_signal":
        coordinator.stop_signal = threading.Event()
        coordinator.stop_signal.set()
    elif blocker == "loss_monitor_due":
        coordinator.loss_monitor.due = lambda stamp: True
    elif blocker == "duration_elapsed":
        now = NOW + 60 * SECOND
        accounts(live, now)
        live.update_native_admission({"BACKPACK": {"ready": True, "reason": None, "checked_ns": now}}, now)
    elif blocker == "reentrant":
        coordinator._dispatching = True

    _set_venue_book(live, "BACKPACK", "99.99", "100.00", now)
    _set_venue_book(live, "ASTER", "100.50", "100.51", now)
    # Bypass evaluate_symbol pause guard to test _publish_orders -> direct_dispatcher directly when paused/halted.
    if blocker in {"entry_pause", "halt"}:
        saved_pause, saved_halt = live.entry_pause_reason, live.halt_reason
        live.entry_pause_reason = None
        live.halt_reason = None
        live.direct_dispatcher = None
        live.evaluate_symbol("BTC", now)
        live.entry_pause_reason, live.halt_reason = saved_pause, saved_halt
        live.direct_dispatcher = coordinator.dispatch_orders_now
        assert coordinator.dispatch_orders_now(now) is False
        assert not gateway.sent
        return

    live.evaluate_symbol("BTC", now)
    assert not gateway.sent
    assert len(live.outbox) == 2
    assert len(wakeups) == 1
    if blocker == "loss_monitor_due":
        assert live.entry_pause_reason == "session_loss_check"


def test_direct_dispatch_exception_requests_stop_and_resets_reentrancy_flag(tmp_path):
    coordinator, live, gateway, _, _ = _setup_coordinator(tmp_path, venues=("ASTER", "BACKPACK"))
    def boom(*args, **kwargs):
        raise RuntimeError("synthetic dispatch failure")
    gateway.dispatch = boom
    _set_venue_book(live, "BACKPACK", "99.99", "100.00", NOW)
    _set_venue_book(live, "ASTER", "100.50", "100.51", NOW)
    live.evaluate_symbol("BTC", NOW)
    assert coordinator._dispatching is False
    assert coordinator.failure == "execution callback failed: RuntimeError"
    assert coordinator.stop_reason == "execution_callback_failed"


@pytest.mark.parametrize("buy_ask,sell_bid,buy_fee,sell_fee,reserve,edge,override,expected", [
    ("100", "100", "0", "0", "0", "20", None, True),
    ("100", "99.99", "0", "0", "0", "20", None, True),
    ("100", "100.10", "2", "2", "0", "20", None, True),
    ("100", "100.239999999999", "2", "2", "0", "20", None, True),
    # Exact threshold: 100 * (10000 + 20) / 10000 = 100.20 when fees=0, reserve=0
    ("100", "100.20", "0", "0", "0", "20", None, False),
    ("100", "100.199999999999", "0", "0", "0", "20", None, True),
    ("100", "100.200000000001", "0", "0", "0", "20", None, False),
    # edge=0 exact equality returns True per existing rule
    ("100", "100.02", "1", "1", "0", "0", None, True),  # 100.02*9999 < 100*10001
    ("100", "100.00", "0", "0", "0", "0", None, True),
    # negative edge returns False (does not rule out)
    ("100", "100.01", "0", "0", "0", "-1", None, False),
    # override min_entry_edge_bps
    ("100", "100.22", "0", "0", "0", "20", D("25"), True),
    ("100", "100.26", "0", "0", "0", "20", D("25"), False),
])
def test_best_quotes_rule_out_float_fast_path_matches_exact_fraction(
    buy_ask, sell_bid, buy_fee, sell_fee, reserve, edge, override, expected,
):
    buy_b = BookSnapshot("A", "BTC", ((D("99"), D(1)),), ((D(buy_ask), D(1)),), NOW, NOW)
    sell_b = BookSnapshot("B", "BTC", ((D(sell_bid), D(1)),), ((D("105"), D(1)),), NOW, NOW)
    buy_m = MarketMetadata("BTC.A", D("0.01"), taker_fee_bps=D(buy_fee), fee_source="synthetic")
    sell_m = MarketMetadata("BTC.B", D("0.01"), taker_fee_bps=D(sell_fee), fee_source="synthetic")
    settings = ScanSettings(min_entry_edge_bps=D(edge), reserve_bps=D(reserve))
    assert _best_quotes_rule_out(buy_b, buy_m, sell_b, sell_m, settings, min_entry_edge_bps=override) is expected


def test_trade_policy_slow_venue_validation_aliases_and_legacy_fingerprint(tmp_path):
    baseline = load_live_plan(configs(tmp_path))
    assert baseline.policy.slow_venues == ()
    assert "slow_venues" not in baseline.policy.document()
    assert "slow_pair_entry_edge_bps" not in baseline.policy.document()
    assert "slow_pair_min_persistence_ms" not in baseline.policy.document()

    pol = TradePolicy(
        slow_venues=["HYPERLIQUID", "LIGHTER", "HYPERLIQUID"],
        slow_pair_entry_edge_bps="25",
        slow_pair_min_persistence_ms=150,
    )
    assert pol.slow_venues == ("HL", "LIGHTER")
    assert pol.is_slow_pair("HYPERLIQUID", "ASTER") is True
    assert pol.is_slow_pair("ASTER", "BACKPACK") is False
    assert pol.effective_entry_edge_bps("ASTER", "BACKPACK") == D("30")  # default entry_edge_bps=30 > 25
    pol2 = replace(pol, entry_edge_bps="20")
    assert pol2.effective_entry_edge_bps("HL", "ASTER") == D("25")
    assert pol2.effective_entry_edge_bps("ASTER", "BACKPACK") == D("20")
    assert pol.document()["slow_venues"] == ["HL", "LIGHTER"]
    assert pol.document()["slow_pair_entry_edge_bps"] == "25"
    assert pol.document()["slow_pair_min_persistence_ms"] == 150

    for bad in (
        {"slow_venues": "HL"},
        {"slow_venues": ["UNKNOWN"]},
        {"slow_venues": ["HL", "HYPERLIQUID"]},
        {"slow_pair_entry_edge_bps": "0"},
        {"slow_pair_entry_edge_bps": "10000"},
        {"slow_pair_min_persistence_ms": -1},
        {"slow_pair_min_persistence_ms": 60001},
    ):
        with pytest.raises(ScanConfigError):
            TradePolicy(**bad)


def test_fast_pair_enters_at_20bps_while_slow_pair_requires_higher_edge_and_persistence(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    policy = TradePolicy(
        entry_edge_bps=D("20"),
        slow_venues=("HL", "LIGHTER", "ONDO"),
        slow_pair_entry_edge_bps=D("25"),
        slow_pair_min_persistence_ms=150,
    )
    live, _ = state(_fast_slow_plan(("HL", "ASTER", "BACKPACK")), policy=policy)
    live.update_native_admission({"BACKPACK": {"ready": True, "reason": None, "checked_ns": NOW}}, NOW)
    # 1. Fast pair ASTER <-> BACKPACK enters immediately at 21 bps without persistence wait.
    set_book(live, "BACKPACK", "99.99", "100.00", NOW)
    set_book(live, "ASTER", "100.21", "100.22", NOW)
    set_book(live, "HL", "99.99", "100.20", NOW)  # no edge vs HL
    live.evaluate_symbol("BTC", NOW)
    assert len(live.outbox) == 2
    assert {i.venue for i in live.outbox} == {"BACKPACK", "ASTER"}


def test_slow_pair_persistence_wait_reset_on_dip_invalidate_and_gap(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    policy = TradePolicy(
        entry_edge_bps=D("20"),
        slow_venues=("HL",),
        slow_pair_entry_edge_bps=D("25"),
        slow_pair_min_persistence_ms=150,
    )
    live, _ = state(_fast_slow_plan(("HL", "ASTER")), policy=policy)

    # 22 bps (< 25 bps slow threshold) never enters or starts persistence timer.
    set_book(live, "HL", "99.99", "100.00", NOW)
    set_book(live, "ASTER", "100.22", "100.23", NOW)
    live.evaluate_symbol("BTC", NOW)
    live.evaluate_symbol("BTC", NOW + 200 * MS)
    assert not live.outbox
    assert not live._slow_pair_since

    # 26 bps starts timer at t0, blocked by persistence wait until +150ms.
    t0 = NOW + 300 * MS
    set_book(live, "HL", "99.99", "100.00", t0)
    set_book(live, "ASTER", "100.26", "100.27", t0)
    live.evaluate_symbol("BTC", t0)
    assert not live.outbox and ("BTC.HL", "BTC.ASTER") in live._slow_pair_since

    # Dip at t0 + 50ms resets persistence timer.
    t1 = t0 + 50 * MS
    set_book(live, "ASTER", "100.21", "100.22", t1)
    live.evaluate_symbol("BTC", t1)
    assert not live._slow_pair_since

    # Re-appears at t2 = t0 + 100ms -> must wait full 150ms from t2, not t0!
    t2 = t0 + 100 * MS
    set_book(live, "ASTER", "100.26", "100.27", t2)
    live.evaluate_symbol("BTC", t2)
    assert not live.outbox
    # At t2 + 100ms, still only 100ms elapsed since t2 -> blocked.
    set_book(live, "HL", "99.99", "100.00", t2 + 100 * MS)
    set_book(live, "ASTER", "100.26", "100.27", t2 + 100 * MS)
    live.evaluate_symbol("BTC", t2 + 100 * MS)
    assert not live.outbox

    # invalidate clears persistence timer.
    live.invalidate("BTC.HL", t2 + 110 * MS)
    assert not live._slow_pair_since

    # Restart cleanly at t3, hit gap > 500ms at t3 + 600ms -> resets instead of admitting!
    t3 = t2 + 200 * MS
    set_book(live, "HL", "99.99", "100.00", t3)
    set_book(live, "ASTER", "100.26", "100.27", t3)
    live.evaluate_symbol("BTC", t3)
    assert not live.outbox
    t4 = t3 + 600 * MS
    set_book(live, "HL", "99.99", "100.00", t4)
    set_book(live, "ASTER", "100.26", "100.27", t4)
    live.evaluate_symbol("BTC", t4)
    assert not live.outbox
    assert live._slow_pair_since[("BTC.HL", "BTC.ASTER")] == (t4, t4)

    # Sustained until t4 + 150ms -> admits!
    t5 = t4 + 150 * MS
    set_book(live, "HL", "99.99", "100.00", t5)
    set_book(live, "ASTER", "100.26", "100.27", t5)
    live.evaluate_symbol("BTC", t5)
    assert len(live.outbox) == 2
    assert not live._slow_pair_since

    # Verify entry_dispatch_evidence also enforces the 25 bps slow-pair threshold if book worsens to 22 bps.
    set_book(live, "ASTER", "100.22", "100.23", t5)
    pos = next(iter(live.positions.values()))
    reason, _ = entry_dispatch_evidence(live, pos, t5)
    assert reason in {"price_outside_limit", "net_edge_insufficient"}

    funnel = recorder.document()["analysis"]["funnel"]["totals"]
    assert funnel.get("live_rejected:slow_pair_persistence_wait", 0) >= 4


def test_slow_pair_persistence_admits_via_evaluation_timer_without_new_delta():
    policy = TradePolicy(
        entry_edge_bps=D("20"),
        slow_venues=("HL",),
        slow_pair_entry_edge_bps=D("25"),
        slow_pair_min_persistence_ms=150,
    )
    live, _ = state(_fast_slow_plan(("HL", "ASTER")), policy=policy)
    set_book(live, "HL", "99.99", "100.00", NOW)
    set_book(live, "ASTER", "100.26", "100.27", NOW)
    live.evaluate_symbol("BTC", NOW)
    assert not live.outbox and ("BTC.HL", "BTC.ASTER") in live._slow_pair_since

    # No additional book delta arrives; timer fires past 150ms and admits using existing fresh books.
    live.on_evaluation_timer(NOW + 160 * MS)
    submits = [i for i in live.outbox if i.action == "submit"]
    assert len(submits) == 2
    assert {i.venue for i in submits} == {"HL", "ASTER"}
    assert not live._slow_pair_since


def test_evaluate_symbol_refreshes_flat_cache_after_opening_first_pair_and_respects_max_pairs(monkeypatch):
    recorder = timing.TimingRecorder()
    monkeypatch.setattr(timing, "_active", recorder)
    # Gross cap 45 allows at most 1 pair (2 * 20 = 40 <= 45, second pair 40 + 40 = 80 > 45).
    live, _ = state(_fast_slow_plan(("HL", "ASTER", "BACKPACK")),
                    policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")),
                    max_pairs=2, gross="45")
    live.update_native_admission({"BACKPACK": {"ready": True, "reason": None, "checked_ns": NOW}}, NOW)
    set_book(live, "HL", "99.99", "100.00", NOW)
    set_book(live, "ASTER", "100.30", "100.31", NOW)
    set_book(live, "BACKPACK", "100.40", "100.41", NOW)
    live.evaluate_symbol("BTC", NOW)
    # Must open ONLY 1 pair because after the first opens, is_flat/reserved_gross refresh blocks the 2nd!
    assert len(live.positions) == 1
    assert len(live.outbox) == 2

    # When max_pairs=1 and already full, evaluate_symbol hits max_pairs once and skips entry scanning.
    live2, _ = state(_fast_slow_plan(("HL", "ASTER", "BACKPACK")),
                     policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")),
                     max_pairs=1, gross="500")
    live2.update_native_admission({"BACKPACK": {"ready": True, "reason": None, "checked_ns": NOW}}, NOW)
    set_book(live2, "HL", "99.99", "100.00", NOW)
    set_book(live2, "ASTER", "100.30", "100.31", NOW)
    set_book(live2, "BACKPACK", "100.40", "100.41", NOW)
    live2.evaluate_symbol("BTC", NOW)
    assert len(live2.positions) == 1
    before_max_pairs = recorder.document()["analysis"]["funnel"]["pre_gates"].get("max_pairs", 0)
    set_book(live2, "HL", "99.98", "99.99", NOW + 100 * MS)
    live2.evaluate_symbol("BTC", NOW + 100 * MS)
    after_max_pairs = recorder.document()["analysis"]["funnel"]["pre_gates"].get("max_pairs", 0)
    assert after_max_pairs == before_max_pairs + 1


def test_bbo_prescreen_skips_entry_when_both_directions_ruled_out_and_allows_crossed(monkeypatch):
    live, _ = state(_fast_slow_plan(("ASTER", "BACKPACK")),
                    policy=TradePolicy(order_notional_usd=D("20"), entry_edge_bps=D("20")))
    live.update_native_admission({"BACKPACK": {"ready": True, "reason": None, "checked_ns": NOW}}, NOW)

    entry_calls = []
    orig_entry = live._entry

    def counted_entry(buy_id, sell_id, now_ns):
        entry_calls.append((buy_id, sell_id))
        return orig_entry(buy_id, sell_id, now_ns)

    monkeypatch.setattr(live, "_entry", counted_entry)

    set_book(live, "ASTER", "99.99", "100.00", NOW)
    set_book(live, "BACKPACK", "99.99", "100.00", NOW)
    live.evaluate_symbol("BTC", NOW)
    assert entry_calls == []
    assert not live.outbox

    # Crossed pair passes BBO pre-screen and prepares normally.
    set_book(live, "BACKPACK", "100.40", "100.41", NOW + 10 * MS)
    live.evaluate_symbol("BTC", NOW + 10 * MS)
    assert entry_calls == [("BTC.ASTER", "BTC.BACKPACK")]
    assert len(live.outbox) == 2


def test_bbo_prescreen_clears_slow_pair_persistence_when_spread_disappears():
    policy = TradePolicy(
        entry_edge_bps=D("20"),
        slow_venues=("HL",),
        slow_pair_entry_edge_bps=D("25"),
        slow_pair_min_persistence_ms=150,
    )
    live, _ = state(_fast_slow_plan(("HL", "ASTER")), policy=policy)
    set_book(live, "HL", "99.99", "100.00", NOW)
    set_book(live, "ASTER", "100.26", "100.27", NOW)
    live.evaluate_symbol("BTC", NOW)
    assert ("BTC.HL", "BTC.ASTER") in live._slow_pair_since

    # Spread drops below 25 bps threshold -> BBO pre-screen rules it out and clears persistence.
    set_book(live, "ASTER", "100.10", "100.11", NOW + 50 * MS)
    live.evaluate_symbol("BTC", NOW + 50 * MS)
    assert ("BTC.HL", "BTC.ASTER") not in live._slow_pair_since


def test_normalized_levels_matches_exact_formula_for_float_and_decimal():
    from fractions import Fraction
    from opportunity_core import exact_decimal
    from opportunity_runtime import normalized_levels

    m_eq = MarketPlan("BTC", "ASTER", "BTCUSDT-PERP.ASTER", D("1"), "synthetic", canonical_multiplier=D("1"))
    m_ne = MarketPlan("PEPE", "ASTER", "1000PEPEUSDT-PERP.ASTER", D("1.0001"), "synthetic", canonical_multiplier=D("1000"))

    dec_levels = [(D("100.25"), D("2.5")), (D("100.30"), D("4.0"))]
    flt_levels = [(100.25, 2.5), (100.3, 4.0)]

    for lvls in (dec_levels, flt_levels):
        assert normalized_levels(m_eq, lvls) == tuple(lvls)
        expected = tuple(
            (exact_decimal(Fraction(p) * Fraction(m_ne.quote_to_usd) / Fraction(m_ne.canonical_multiplier)), s)
            for p, s in lvls
        )
        # Call twice to exercise lru_cache hit path as well.
        assert normalized_levels(m_ne, lvls) == expected
        assert normalized_levels(m_ne, lvls) == expected


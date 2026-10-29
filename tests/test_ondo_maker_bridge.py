"""Callback failure and account-proof fences around the native strategy bridge."""

import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_ondo_maker import ready

from ondo_maker_live import OndoMakerStrategy
from ondo_maker_diagnostics import OrderLatency


def test_latency_reports_acceptance_once_and_excludes_unacknowledged_orders():
    timing = OrderLatency()
    timing.begin("one", 1_000_000)
    timing.submitted("one", 100_000_000)
    timing.accepted_event("one", 13_000_000, 110_000_000)
    timing.accepted_event("one", 20_000_000, 120_000_000)
    timing.begin("rejected", 20_000_000)
    timing.finish("rejected")
    timing.begin("unknown", 30_000_000)
    report = timing.report()
    assert report["accepted_total"] == 1 and report["pending"] == 1
    assert report["submit_call_to_accept_callback"]["p50_ms"] == 12
    assert report["native_submitted_to_accepted"]["p50_ms"] == 10
    for n in range(3000):
        timing.begin(str(n), n)
    assert len(timing.pending) == 64


def test_failed_dispatch_does_not_leave_never_sent_sibling_reservations():
    engine = ready()
    seen = []

    def submit(order):
        seen.append(order)
        raise RuntimeError("synthetic failure")

    host = NS(
        engine=engine,
        driving=False,
        ids={"BTC": "btc", "ETH": "eth"},
        cache=NS(instrument=lambda _: NS(make_qty=lambda x: x, make_price=lambda x: x)),
        order_factory=NS(limit=lambda **kwargs: kwargs),
        submit_order=submit,
        command_counts={"cancel": 0, "maker": 0, "close": 0},
        clock=NS(timestamp_ns=lambda: 1),
        diagnostics=None,
    )
    # Use a fixed engine clock while exercising the real bridge dispatch loop.
    step = engine.step
    engine.step = lambda _: step(1_000_000_000)
    OndoMakerStrategy.drive.__wrapped__(host)
    assert len(seen) == 1
    assert sum(len(s.orders) for s in engine.markets.values()) == 1
    assert engine.fault == "order_dispatch_outcome_unknown"
    assert engine.stopping and not engine.can_stop


def test_private_disconnect_revokes_new_risk_even_with_fresh_flat_snapshot():
    engine = ready()
    account = {
        "run_id": "test",
        "mode": "continuous",
        "snapshot_unix_nanos": 10,
        "snapshot_started_ns": 5,
        "reconciled_activity_generation": 0,
        "latest_activity_generation": 0,
        "open_orders": [],
        "unknown_submissions": 0,
        "account_positions_owned": True,
        "foreign_open_orders": 0,
        "connected_ready": True,
        "reconciled_flat": True,
        "native_ready": True,
        "reduction_ready": True,
    }
    host = NS(
        engine=engine,
        factory=NS(
            production_trade_snapshot=lambda: account,
            read_only_snapshot=lambda: {"run_id": "test", "run_state": "disconnected"},
        ),
        session=NS(diagnostics_run_id="test"),
        clock=NS(timestamp_ns=lambda: 10),
        last_activity_wall_ns=0,
        ids={"BTC": "btc", "ETH": "eth"},
        account="test",
        portfolio=NS(net_position=lambda *_: Decimal(0)),
    )
    OndoMakerStrategy.refresh_account(host)
    assert not engine.native_ready and not engine.account_healthy
    engine.stop()
    assert not engine.can_stop


def test_flat_snapshot_started_before_last_activity_cannot_finish_shutdown():
    engine = ready()
    engine.stop()
    snapshot = {
        "run_id": "test",
        "mode": "continuous",
        "snapshot_unix_nanos": 20,
        "snapshot_started_ns": 10,
        "reconciled_activity_generation": 0,
        "latest_activity_generation": 0,
        "open_orders": [],
        "unknown_submissions": 0,
        "account_positions_owned": True,
        "foreign_open_orders": 0,
        "connected_ready": True,
        "reconciled_flat": True,
        "native_ready": True,
        "reduction_ready": True,
    }
    host = NS(
        engine=engine,
        factory=NS(
            production_trade_snapshot=lambda: snapshot,
            read_only_snapshot=lambda: {"run_id": "test", "run_state": "trading_ready"},
        ),
        session=NS(diagnostics_run_id="test"),
        clock=NS(timestamp_ns=lambda: 20),
        last_activity_wall_ns=15,
        ids={"BTC": "btc", "ETH": "eth"},
        account="test",
        portfolio=NS(net_position=lambda *_: Decimal(0)),
    )
    OndoMakerStrategy.refresh_account(host)
    assert not engine.can_stop
    snapshot["snapshot_started_ns"] = 16
    OndoMakerStrategy.refresh_account(host)
    assert engine.can_stop


def test_own_activity_proof_invalidation_keeps_only_bounded_health():
    engine = ready()
    snapshot = {
        "run_id": "test",
        "mode": "continuous",
        "snapshot_unix_nanos": 10,
        "snapshot_started_ns": 5,
        "reconciled_activity_generation": 0,
        "latest_activity_generation": 0,
        "open_orders": [],
        "unknown_submissions": 0,
        "account_positions_owned": True,
        "foreign_open_orders": 0,
        "connected_ready": True,
        "reconciled_flat": True,
        "native_ready": True,
        "reduction_ready": True,
    }
    transport = {"run_id": "test", "run_state": "trading_ready"}
    host = NS(
        engine=engine,
        factory=NS(
            production_trade_snapshot=lambda: snapshot,
            read_only_snapshot=lambda: transport,
        ),
        session=NS(diagnostics_run_id="test"),
        clock=NS(timestamp_ns=lambda: 20),
        last_activity_wall_ns=0,
        account_health_until_ns=0,
        last_proof_started_ns=0,
        ids={"BTC": "btc", "ETH": "eth"},
        account="test",
        portfolio=NS(net_position=lambda *_: Decimal(0)),
    )
    OndoMakerStrategy.refresh_account(host)
    assert engine.native_ready
    host.last_activity_wall_ns = 15
    snapshot = None
    OndoMakerStrategy.refresh_account(host)
    assert engine.account_healthy
    assert not engine.native_ready and not engine.reduction_ready
    engine.stop()
    assert not engine.can_stop
    host.clock.timestamp_ns = lambda: 3_000_000_011
    OndoMakerStrategy.refresh_account(host)
    assert not engine.account_healthy
    host.clock.timestamp_ns = lambda: 20
    host.account_health_until_ns = 3_000_000_010
    transport["run_state"] = "disconnected"
    OndoMakerStrategy.refresh_account(host)
    assert not engine.account_healthy and host.account_health_until_ns == 0


@pytest.mark.parametrize("next_proof", ["reconciled", "expired", "disconnected"])
def test_cancel_invalidation_preserves_keepers_only_within_existing_health_lease(next_proof, monkeypatch):
    engine = ready()
    engine.step(1_000_000_000)
    buy = next(o for o in engine.markets["BTC"].orders.values() if o.side == 1)
    keepers = {
        o.client_id for s in engine.markets.values() for o in s.orders.values()
        if o is not buy
    }
    snapshot = {
        "run_id": "test", "mode": "continuous", "snapshot_unix_nanos": 20,
        "snapshot_started_ns": 10, "reconciled_activity_generation": 4,
        "latest_activity_generation": 4,
        "open_orders": [{"client_order_id": cid} for cid in keepers | {buy.client_id}],
        "unknown_submissions": 0, "account_positions_owned": True,
        "foreign_open_orders": 0, "connected_ready": True,
        "reconciled_flat": True, "native_ready": True, "reduction_ready": True,
    }
    transport = {"run_id": "test", "run_state": "trading_ready"}
    cached = {
        cid: NS(is_closed=False, filled_qty=NS(as_decimal=lambda: Decimal(0)))
        for cid in keepers | {buy.client_id}
    }
    sent = []

    def cancel(client_id):
        nonlocal snapshot
        sent.append(str(client_id))
        # Native cancellation clears the proof before its terminal callback.
        snapshot = None
        OndoMakerStrategy.refresh_account(host)

    host = NS(
        engine=engine, driving=False, diagnostics=None,
        factory=NS(
            production_trade_snapshot=lambda: snapshot,
            read_only_snapshot=lambda: transport,
        ),
        session=NS(diagnostics_run_id="test"), clock=NS(timestamp_ns=lambda: 21),
        last_activity_wall_ns=5, account_health_until_ns=0, last_proof_started_ns=0,
        ids={"BTC": "btc", "ETH": "eth"}, account="test",
        portfolio=NS(net_position=lambda *_: Decimal(0)),
        cache=NS(order=lambda cid: cached[str(cid)]), cancel_order=cancel,
        command_counts={"cancel": 0, "maker": 0, "close": 0},
    )
    step = engine.step
    monkeypatch.setattr("ondo_maker_live.time.monotonic_ns", lambda: 1_000_000_001)
    host.drive = lambda: OndoMakerStrategy.drive.__wrapped__(host)
    OndoMakerStrategy.refresh_account(host)
    assert engine.native_ready and engine.account_flat
    lease_until = host.account_health_until_ns

    buy.price = Decimal("99.94")
    host.drive()
    assert sent == [buy.client_id]
    assert engine.account_healthy
    assert not engine.native_ready and not engine.reduction_ready and not engine.account_flat
    assert host.account_health_until_ns == lease_until
    assert not step(1_000_000_002)

    if next_proof != "reconciled":
        if next_proof == "expired":
            host.clock.timestamp_ns = lambda: lease_until + 1
        else:
            transport["run_state"] = "disconnected"
        OndoMakerStrategy.refresh_account(host)
        assert not engine.account_healthy and host.account_health_until_ns == 0
        commands = step(1_000_000_003)
        assert {c.order.client_id for c in commands if c.kind == "cancel"} == keepers
        assert all(c.kind == "cancel" for c in commands)
        return

    # Even the cancel acknowledgement cannot authorize a replacement without a new proof.
    cached[buy.client_id].is_closed = True
    host.clock.timestamp_ns = lambda: 22
    OndoMakerStrategy.order_event.__wrapped__(host, NS(client_order_id=buy.client_id))
    assert sent == [buy.client_id]
    assert not step(1_000_000_003)
    assert not engine.native_ready and not engine.account_flat
    snapshot = {
        "run_id": "test", "mode": "continuous", "snapshot_unix_nanos": 30,
        "snapshot_started_ns": 25, "reconciled_activity_generation": 6,
        "latest_activity_generation": 6,
        "open_orders": [{"client_order_id": cid} for cid in keepers],
        "unknown_submissions": 0, "account_positions_owned": True,
        "foreign_open_orders": 0, "connected_ready": True,
        "reconciled_flat": True, "native_ready": True, "reduction_ready": True,
    }
    host.clock.timestamp_ns = lambda: 31
    OndoMakerStrategy.refresh_account(host)
    commands = step(1_000_000_004)
    assert len(commands) == 1 and commands[0].kind == "create"
    assert commands[0].order.market == "BTC" and commands[0].order.side == 1
    assert all(not o.cancel_requested for s in engine.markets.values() for o in s.orders.values())
    assert keepers.issubset({cid for s in engine.markets.values() for cid in s.orders})


def test_single_side_replacement_accepts_matching_live_keeper_but_not_stale_or_mismatched_proof():
    engine = ready()
    engine.step(1_000_000_000)
    buy = next(o for o in engine.markets["BTC"].orders.values() if o.side == 1)
    buy.price = Decimal("99.94")
    assert len(engine.step(1_000_000_001)) == 1
    engine.update_order(buy.client_id, filled=Decimal(0), terminal=True, now=1_000_000_002)
    observed = [
        {"client_order_id": o.client_id}
        for state in engine.markets.values() for o in state.orders.values()
    ]
    snapshot = {
        "run_id": "test", "mode": "continuous", "snapshot_unix_nanos": 20,
        "snapshot_started_ns": 10, "reconciled_activity_generation": 5,
        "latest_activity_generation": 5, "open_orders": observed,
        "position_quantities": {}, "unknown_submissions": 0,
        "account_positions_owned": True, "foreign_open_orders": 0,
        "connected_ready": True, "reconciled_flat": True,
        "native_ready": True, "reduction_ready": True,
    }
    host = NS(
        engine=engine, factory=NS(
            production_trade_snapshot=lambda: snapshot,
            read_only_snapshot=lambda: {"run_id": "test", "run_state": "trading_ready"},
        ),
        session=NS(diagnostics_run_id="test"), clock=NS(timestamp_ns=lambda: 20),
        last_activity_wall_ns=15, ids={"BTC": "btc", "ETH": "eth"}, account="test",
        portfolio=NS(net_position=lambda *_: Decimal(0)),
    )
    OndoMakerStrategy.refresh_account(host)
    assert not engine.step(1_000_000_003)
    snapshot["snapshot_started_ns"] = 16
    snapshot["open_orders"] = observed[:-1]
    OndoMakerStrategy.refresh_account(host)
    assert not engine.step(1_000_000_004)
    snapshot["open_orders"] = observed
    OndoMakerStrategy.refresh_account(host)
    replacement = engine.step(1_000_000_005)
    assert len(replacement) == 1 and replacement[0].order.side == 1
    assert replacement[0].order.market == "BTC"


@pytest.mark.parametrize("fault", [None, "stale", "no_clear", "no_last", "incremental", "oversized"])
def test_native_depth_callback_requires_bounded_complete_fresh_snapshot(monkeypatch, fault):
    from nautilus_trader.model import BookAction, OrderSide, RecordFlag
    from test_ondo_maker import depth_levels

    engine = ready()
    levels = depth_levels()
    value = lambda x: NS(as_decimal=lambda: x)
    deltas = [NS(action=BookAction.CLEAR, flags=int(RecordFlag.F_SNAPSHOT))]
    for side, rows in ((OrderSide.BUY, levels[0]), (OrderSide.SELL, levels[1])):
        deltas.extend(NS(
            action=BookAction.ADD, flags=int(RecordFlag.F_SNAPSHOT),
            order=NS(side=side, price=value(p), size=value(q)),
        ) for p, q in rows)
    deltas[-1].flags |= int(RecordFlag.F_LAST)
    batch = NS(instrument_id="btc", ts_init=10_000_000_000, deltas=deltas)
    if fault == "stale":
        batch.ts_init = 1
    elif fault == "no_clear":
        deltas[0].action = BookAction.ADD
    elif fault == "no_last":
        deltas[-1].flags = int(RecordFlag.F_SNAPSHOT)
    elif fault == "incremental":
        deltas[1].flags = 0
    elif fault == "oversized":
        batch.deltas = deltas * 30
    host = NS(
        engine=engine, symbols={"btc": "BTC"}, last_native_depth_ns={},
        clock=NS(timestamp_ns=lambda: 10_000_000_000), drive=lambda: None,
    )
    monkeypatch.setattr("ondo_maker_live.time.monotonic_ns", lambda: 2_000_000_000)
    OndoMakerStrategy.on_book_deltas.__wrapped__(host, batch)
    assert engine.markets["BTC"].depth.fresh(2_000_000_000, engine.config.depth_stale_ns) is (fault is None)

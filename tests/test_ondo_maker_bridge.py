"""Callback failure and account-proof fences around the native strategy bridge."""

import sys
from collections import deque
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from test_ondo_maker import ready

from ondo_maker_live import OndoMakerStrategy


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
        latencies_ns=deque(maxlen=8),
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

"""Diagnostic failures and disabled instrumentation cannot change order decisions."""

import asyncio
import json
import subprocess
import sys
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from ondo_maker_diagnostics import CAPACITY, CancelLatency, MakerDiagnostics, format_diagnostics
from ondo_maker_live import OndoMakerStrategy
from run_ondo_maker import publish_status
from test_ondo_maker import SECOND, ready


def test_cancel_latency_preserves_first_attempt_and_requires_canceled_confirmation():
    timing = CancelLatency()
    timing.begin("retry", 1_000_000, 2_000_000)
    timing.rejected("retry")
    timing.begin("retry", 50_000_000, 51_000_000)
    timing.canceled("retry", 62_000_000)
    timing.canceled("retry", 70_000_000)
    timing.begin("filled", 1, 2)
    timing.finish("filled")
    timing.begin("unknown", 1, 2)
    timing.dispatch_failed("unknown")
    state = timing.snapshot()
    assert state["confirmed_total"] == 1 and state["pending"] == 1
    assert state["samples_ns"] == ((1_000_000, 60_000_000, 61_000_000, 2),)
    assert state["rejections"] == 1 and state["retries"] == 1
    assert state["closed_without_cancel_confirmation"] == 1
    assert state["dispatch_errors"] == 1
    for i in range(100):
        timing.begin(str(i), 1, 2)
    assert len(timing.pending) == 64
    for i in range(3000):
        timing.begin(str(i), 1, 2)
        timing.canceled(str(i), 3)
    assert len(timing.samples) == CAPACITY


def test_snapshot_is_detached_before_background_formatting():
    diagnostics = MakerDiagnostics()
    diagnostics.cancel.begin("one", 1_000_000, 2_000_000)
    diagnostics.cancel.canceled("one", 12_000_000)
    snapshot = diagnostics.snapshot()
    diagnostics.cancel.begin("two", 1_000_000, 2_000_000)
    diagnostics.cancel.canceled("two", 22_000_000)
    report = format_diagnostics(snapshot)
    assert report["cancel_latency"]["confirmed_total"] == 1
    assert report["cancel_latency"]["first_cancel_call_to_canceled"]["p50_ms"] == 10
    assert diagnostics.report()["cancel_latency"]["confirmed_total"] == 2


def host(diagnostics, *, filled=False):
    engine = ready()
    orders = engine.step(SECOND)
    if filled:
        engine.update_order(orders[0].order.client_id, filled=Decimal("0.1"), terminal=False, now=SECOND)
    engine.disconnect("binance")
    commands = []
    step = engine.step
    engine.step = lambda _: step(SECOND + 1)
    return NS(
        engine=engine, driving=False, diagnostics=diagnostics,
        cache=NS(
            order=lambda _: NS(is_closed=False),
            instrument=lambda _: NS(make_qty=lambda x: x, make_price=lambda x: x),
        ),
        cancel_order=lambda client_id: commands.append(str(client_id)),
        ids={"BTC": "btc", "ETH": "eth"},
        order_factory=NS(limit=lambda **kwargs: kwargs),
        submit_order=lambda order: commands.append("close" if order["reduce_only"] else "maker"),
        clock=NS(timestamp_ns=lambda: SECOND),
        command_counts={"cancel": 0, "maker": 0, "close": 0},
        commands=commands,
    )


def test_disabled_diagnostics_add_no_clock_reads_or_samples(monkeypatch):
    value = host(None)
    calls = []
    monkeypatch.setattr("ondo_maker_live.time.monotonic_ns", lambda: calls.append(1) or SECOND)
    OndoMakerStrategy.drive.__wrapped__(value)
    assert calls == [1]  # The strategy's existing decision clock is still required.
    assert len(value.commands) == 4 and value.engine.fault is None


def test_collector_exception_does_not_block_cancels_or_close(monkeypatch):
    diagnostics = MakerDiagnostics()
    value = host(diagnostics, filled=True)

    def broken(*_):
        raise RuntimeError("synthetic observation failure")

    monkeypatch.setattr(diagnostics.cancel, "begin", broken)
    OndoMakerStrategy.drive.__wrapped__(value)
    assert len(value.commands) == 5 and value.commands[-1] == "close"
    assert value.engine.fault is None and not value.engine.stopping
    assert diagnostics.report() == {"enabled": False, "error_type": "RuntimeError"}


def test_status_output_failure_disables_only_diagnostics(monkeypatch):
    diagnostics = MakerDiagnostics()
    shutdown = []
    value = NS(
        diagnostics=diagnostics,
        status=lambda **_: {"diagnostics": diagnostics.snapshot()},
        request_stop=lambda: shutdown.append(True),
    )

    def broken(_):
        raise OSError("synthetic output failure")

    monkeypatch.setattr("ondo_maker_diagnostics.write_status", broken)
    asyncio.run(publish_status(value))
    assert not shutdown
    assert diagnostics.report() == {"enabled": False, "error_type": "OSError"}


@pytest.mark.parametrize("enabled", [False, True])
def test_cli_diagnostics_are_opt_in_without_network_or_credentials(enabled):
    script = Path(__file__).resolve().parents[1] / "scripts/run_ondo_maker.py"
    args = [sys.executable, str(script), "--btc-usd", "500", "--eth-usd", "500", "--gross-usd", "2000"]
    if enabled:
        args.append("--diagnostics")
    result = subprocess.run(args, capture_output=True, text=True, check=True)
    config = json.loads(result.stdout)
    assert config["diagnostics"] is enabled
    assert config["network"] is False and config["credentials"] is False

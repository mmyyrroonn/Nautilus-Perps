"""Real installed native execution against a synthetic loopback account, in a child process."""

from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
import time
from decimal import Decimal
from functools import partial
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))


def fixture_peer(fill_on_cancel):
    # Give this test its own module globals; existing BTC/TSLA fixtures stay unchanged.
    spec = importlib.util.spec_from_file_location(
        "maker_peer", ROOT / "tests/support/ondo_continuous_peer.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.SECOND_MARKET = "ETH-USD.P"
    module.SECOND_INSTRUMENT = "ETH-USD-PERP.ONDO"
    return module.ContinuousPeer(fill_on_cancel=fill_on_cancel)


def child(mode, journal):
    from test_ondo_continuous import _identity

    from ondo_continuous import ContinuousLimits, ContinuousSession, build_session
    from ondo_maker import MARKETS, NATIVE_UNPACED_REQUEST_BOUND, MakerConfig
    from ondo_maker_live import make_strategy

    identity = _identity()
    peer = fixture_peer(mode in {"cancel-fill", "quiet-fill"})
    if mode.startswith("quiet") or mode == "heartbeat-loss":
        peer.quote_interval = 3600
    config = MakerConfig(
        Decimal(500), Decimal(500),
        max_order_usd=Decimal(500), max_gross_usd=Decimal(2000),
    )
    session = ContinuousSession(
        instrument_ids=tuple(f"{m}-USD-PERP.ONDO" for m in MARKETS),
        environment="production",
        execution_enabled=True,
        expected_venue_account_id="unit-account",
        diagnostics_run_id="maker-" + mode,
        journal_path=journal,
        base_url_http=peer.http_url,
        base_url_ws=peer.ws_url,
        api_key="ondoKeyId_MAKER_TEST_ONLY",
        api_secret="ondoApiSecret_MAKER_TEST_ONLY",
        limits=ContinuousLimits(
            max_notional_per_order_usd="500",
            max_gross_exposure_usd="2000",
            max_live_orders=6,
            max_requests_per_second=NATIVE_UNPACED_REQUEST_BOUND,
            max_unresolved_orders=4,
        ),
    )
    node, _factory, strategy = build_session(
        session,
        strategy_factory=partial(
            make_strategy, maker=config, run_id="testmaker", diagnostics=mode != "diagnostics-off"
        ),
    )
    handle = node.handle()
    trace = []
    original_step = strategy.engine.step

    def traced_step(now):
        commands = original_step(now)
        if commands:
            snapshot = strategy.factory.production_trade_snapshot() or {}
            trace.append(
                {
                    "commands": [
                        (c.kind, c.order.client_id, c.order.reduce_only)
                        for c in commands
                    ],
                    "reasons": {
                        m: s.reason for m, s in strategy.engine.markets.items()
                    },
                    "native": {
                        k: snapshot.get(k)
                        for k in (
                            "native_ready",
                            "reduction_ready",
                            "connected_ready",
                            "unknown_submissions",
                            "latest_activity_generation",
                            "reconciled_activity_generation",
                            "snapshot_started_ns",
                        )
                    },
                    "transport": (strategy.factory.read_only_snapshot() or {}).get(
                        "run_state"
                    ),
                    "last_activity": strategy.last_activity_wall_ns,
                }
            )
        return commands

    strategy.engine.step = traced_step

    async def run():
        native = asyncio.ensure_future(node.run_async())
        began = time.monotonic()
        requested = False
        failure = None
        while not native.done():
            now = time.monotonic_ns()
            if not requested or mode in {"cancel-fill", "quiet-fill", "heartbeat-loss"}:
                for m in MARKETS:
                    strategy.binance_quote(m, now, 100, 100)
            with peer._lock:
                open_makers = [
                    o
                    for o in peer.orders.values()
                    if o["status"] == "open" and not o["reduceOnly"]
                ]
            if len(open_makers) == 4 and not requested:
                if mode.startswith("quiet") and time.monotonic() - began < 9:
                    await asyncio.sleep(0.05)
                    continue
                if mode == "heartbeat-loss":
                    peer.public_heartbeat = False
                    if time.monotonic() - began < 12:
                        await asyncio.sleep(0.05)
                        continue
                if mode == "feed-loss":
                    strategy.binance_disconnect()
                strategy.request_stop()
                requested = True
            if mode == "heartbeat-loss" and not open_makers and time.monotonic() - began >= 12:
                strategy.request_stop()
                requested = True
            if strategy.engine.can_stop:
                handle.stop()
                break
            if time.monotonic() - began > 45:
                failure = {
                    "status": strategy.status(),
                    "snapshot": strategy.factory.production_trade_snapshot(),
                }
                # Test-only bounded watchdog; the production launcher has no forced drain timeout.
                handle.stop()
                break
            await asyncio.sleep(0.05)
        await native
        return failure

    try:
        failure = asyncio.run(run())
        result = {
            "status": strategy.status(),
            "failure": failure,
            "positions": {k: str(v) for k, v in peer.quantities.items()},
            "orders": list(peer.orders.values()),
            "writes": [r for r in peer.requests if r["method"] != "GET"],
            "identity": identity.get("wheel_sha256", "verified"),
            "trace": trace,
        }
        print("ONDO_MAKER_RESULT " + json.dumps(result))
    finally:
        peer.close()


@pytest.mark.parametrize("mode", [
    "feed-loss", "cancel-fill", "quiet-heartbeat", "quiet-fill", "heartbeat-loss", "diagnostics-off",
])
def test_native_cancel_first_and_drains_to_flat(mode, tmp_path):
    from test_ondo_continuous import _environment

    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            mode,
            str(tmp_path / "journal.jsonl"),
        ],
        env=_environment(),
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        timeout=100,
    )
    lines = [
        line
        for line in result.stdout.splitlines()
        if line.startswith("ONDO_MAKER_RESULT ")
    ]
    assert result.returncode == 0 and lines, (
        result.stdout[-5000:],
        result.stderr[-2500:],
    )
    data = json.loads(lines[-1].split(" ", 1)[1])
    (tmp_path / "native-result.json").write_text(
        json.dumps(data, indent=2), encoding="utf-8"
    )
    assert data["failure"] is None, data
    assert data["status"]["safe_to_stop"], data
    assert all(Decimal(q) == 0 for q in data["positions"].values()), data
    assert all(o["status"] != "open" for o in data["orders"]), data
    creates = [r["body"] for r in data["writes"] if r["method"] == "POST"]
    makers = [o for o in creates if not o["reduceOnly"]]
    closes = [o for o in creates if o["reduceOnly"]]
    assert len(makers) == 4 and all(
        o["postOnly"] and o["timeInForce"] == "GTC" for o in makers
    ), data["trace"]
    notionals = [Decimal(o["size"]) * Decimal(o["price"]) for o in makers]
    # The synthetic BTC lot is worth about USD 8.50, so a USD 1 tolerance is invalid.
    fixture_lots = {
        "BTC-USD.P": (Decimal("0.0001"), Decimal("85001")),
        "ETH-USD.P": (Decimal("0.01"), Decimal("227.50")),
    }
    for order in makers:
        lot, ask = fixture_lots[order["market"]]
        quantity = Decimal(order["size"])
        valuation = max(Decimal(order["price"]), ask)
        assert 0 < quantity * valuation <= Decimal(500)
        assert (quantity + lot) * valuation > Decimal(500)
    assert sum(notionals) <= Decimal(2000)
    diagnostics = data["status"]["diagnostics"]
    if mode == "diagnostics-off":
        assert diagnostics == {"enabled": False}
    else:
        assert diagnostics["enabled"]
        latency = diagnostics["maker_ack_latency"]
        assert latency["accepted_total"] == 4
        assert latency["submit_call_to_accept_callback"]["samples"] == 4
        assert latency["native_submitted_to_accepted"]["samples"] == 4
        canceled = sum(o["status"] == "canceled" for o in data["orders"] if not o["reduceOnly"])
        cancel = diagnostics["cancel_latency"]
        assert cancel["confirmed_total"] == canceled
        assert cancel["first_cancel_call_to_canceled"]["samples"] == canceled
        assert cancel["pending"] == 0 and cancel["dropped"] == 0
    if mode in {"cancel-fill", "quiet-fill"}:
        assert closes and all(
            not o["postOnly"] and o["timeInForce"] == "IOC" for o in closes
        )
        first_close = next(
            i
            for i, r in enumerate(data["writes"])
            if r["method"] == "POST" and r["body"]["reduceOnly"]
        )
        assert sum(r["method"] == "DELETE" for r in data["writes"][:first_close]) >= 4
    elif mode == "feed-loss":
        assert not closes
        assert any(
            row["commands"]
            and all(c[0] == "cancel" for c in row["commands"])
            and set(row["reasons"].values()) == {"binance_unavailable"}
            for row in data["trace"]
        )
    elif mode == "heartbeat-loss":
        assert not closes
        assert any(set(row["reasons"].values()) == {"ondo_unavailable"} for row in data["trace"])
    else:
        assert not closes


if __name__ == "__main__":
    child(sys.argv[1], Path(sys.argv[2]))

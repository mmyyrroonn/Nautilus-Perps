"""Continuous application checks with an explicitly supplied installed native wheel.

Critical lifecycle tests fail when the candidate identity/capability is absent; they
never skip or substitute an old wheel. Each real LiveNode runs in a fresh process.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import replace
from decimal import Decimal

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from ondo_continuous import ContinuousLimits, ContinuousSession


RESULT = "ONDO_CONTINUOUS_RESULT "
CANDIDATE_VARIABLES = (
    "ONDO_CONTINUOUS_WHEEL",
    "ONDO_CONTINUOUS_WHEEL_SHA256",
    "ONDO_CONTINUOUS_PROVENANCE",
    "ONDO_CONTINUOUS_PROVENANCE_SHA256",
)


def _identity() -> dict:
    assert all(os.environ.get(name) for name in CANDIDATE_VARIABLES), (
        "continuous acceptance requires the newly built installed wheel and both reviewed digests"
    )
    wheel = Path(os.environ[CANDIDATE_VARIABLES[0]])
    provenance = Path(os.environ[CANDIDATE_VARIABLES[2]])
    assert (
        hashlib.sha256(provenance.read_bytes()).hexdigest()
        == os.environ[CANDIDATE_VARIABLES[3]]
    )
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_native_install import verify

    identity = verify(
        wheel,
        os.environ[CANDIDATE_VARIABLES[1]],
        provenance=provenance,
        require_source_binding=True,
        require_ondo=True,
    )
    from nautilus_trader.adapters import ondo

    factory = ondo.OndoExecutionClientFactory()
    assert factory.supports_continuous_execution is True
    assert factory.supports_native_amend is False
    assert hasattr(ondo, "OndoContinuousExecutionConfig")
    return identity


def _environment() -> dict[str, str]:
    result = os.environ.copy()
    for name in tuple(result):
        if name in CANDIDATE_VARIABLES:
            continue
        if name.startswith(
            (
                "ONDO_",
                "ASTER_",
                "HYPERLIQUID_",
                "LIGHTER_",
                "FUTU_",
                "BINANCE_",
                "CONDA_",
            )
        ):
            result.pop(name, None)
        elif name.endswith(
            ("_API_KEY", "_API_SECRET", "_PRIVATE_KEY", "_PASSWORD", "_TOKEN")
        ):
            result.pop(name, None)
    result.pop("PYTHONPATH", None)
    result.pop("PYTHONHOME", None)
    result["PYTHONNOUSERSITE"] = "1"
    result["PYTHON_DOTENV_DISABLED"] = "1"
    return result


def _child(mode: str, journal: Path) -> dict:
    identity = _identity()
    from nautilus_trader.model import (
        ClientOrderId,
        InstrumentId,
        OrderSide,
        TimeInForce,
    )
    from ondo_continuous import build_session, minimum_quantity

    sys.path.insert(0, str(ROOT / "tests" / "support"))
    from ondo_continuous_peer import ContinuousPeer, INSTRUMENT, SECOND_INSTRUMENT

    peer = ContinuousPeer(fill_on_cancel=mode == "cancel-race")
    session = ContinuousSession(
        instrument_ids=(INSTRUMENT, SECOND_INSTRUMENT),
        environment="production",
        execution_enabled=True,
        expected_venue_account_id="unit-account",
        diagnostics_run_id="continuous-app-" + mode,
        journal_path=journal,
        base_url_http=peer.http_url,
        base_url_ws=peer.ws_url,
        api_key="ondoKeyId_CONTINUOUS_TEST_ONLY",
        api_secret="ondoApiSecret_CONTINUOUS_TEST_ONLY",
    )
    stages: dict = {
        "phase": "waiting",
        "index": 0,
        "completed_instruments": [],
        "entry_observations": [],
    }
    observation: dict = {}
    watchdog_fired = threading.Event()
    monitor_done = threading.Event()
    handle = None

    def policy(strategy, quote) -> None:
        # The same production Strategy proceeds after startup, without rebuilding
        # clients, releasing/rearming DMS, or replacing continuous native authority.
        target = InstrumentId.from_str(session.instrument_ids[stages["index"]])
        if quote.instrument_id != target:
            return
        if stages["phase"] == "waiting":
            if not strategy._native_ready(flat=True):
                return
            price = strategy._protected_price(OrderSide.BUY, target)
            quantity = minimum_quantity(
                strategy.cache.instrument(target), price, Decimal("20")
            )
            stages["phase"] = "entry"
            stages["entry"] = strategy.submit_limit(
                f"policy_entry_{target.symbol}",
                OrderSide.BUY,
                quantity,
                price,
                TimeInForce.IOC,
                instrument_id=target,
            )
        elif stages["phase"] == "entry":
            order = strategy.cache.order(ClientOrderId.from_str(stages["entry"]))
            if (
                order is not None
                and order.is_closed
                and order.filled_qty.as_decimal() > 0
                and strategy._native_ready(reduce_only=True)
            ):
                filled = order.filled_qty.as_decimal()
                positions = strategy.cache.positions_open(
                    instrument_id=target, account_id=strategy.config.account_id
                )
                cached = sum(
                    (position.quantity.as_decimal() for position in positions),
                    Decimal(0),
                )
                net = strategy.portfolio.net_position(
                    target, strategy.config.account_id
                )
                if cached != filled or net != filled:
                    return
                stages["entry_observations"].append(
                    {
                        "instrument_id": str(target),
                        "cached_open_quantity": str(cached),
                        "portfolio_net_position": str(net),
                        "confirmed_filled_quantity": str(filled),
                    }
                )
                stages["phase"] = "close"
                stages["close"] = strategy.submit_limit(
                    f"policy_close_{target.symbol}",
                    OrderSide.SELL,
                    order.filled_qty.as_decimal(),
                    strategy._protected_price(OrderSide.SELL, target),
                    TimeInForce.IOC,
                    reduce_only=True,
                    instrument_id=target,
                )
        elif stages["phase"] == "close":
            order = strategy.cache.order(ClientOrderId.from_str(stages["close"]))
            if (
                order is not None
                and order.is_closed
                and strategy._native_ready(flat=True)
            ):
                if strategy.portfolio.net_position(
                    target, strategy.config.account_id
                ) != Decimal(0) or strategy.cache.positions_open(
                    instrument_id=target, account_id=strategy.config.account_id
                ):
                    return
                stages["completed_instruments"].append(str(target))
                if stages["index"] + 1 < len(session.instrument_ids):
                    stages["index"] += 1
                    stages["phase"] = "waiting"
                    return
                stages["phase"] = "done"
                observation.update(strategy.report())
                handle.stop()

    node = None
    try:
        node, factory, strategy = build_session(
            session,
            startup_timeout_secs=120,
            on_continuous_quote=policy,
        )
        handle = node.handle()

        def monitor() -> None:
            while not monitor_done.wait(0.05):
                if strategy.failures:
                    handle.stop()
                    return

        observer = threading.Thread(target=monitor, daemon=True)
        observer.start()

        def timeout() -> None:
            watchdog_fired.set()
            handle.stop()

        watchdog = threading.Timer(150, timeout)
        watchdog.daemon = True
        watchdog.start()
        begin = time.monotonic()
        node_error = None
        try:
            node.run()
        except RuntimeError as exc:
            node_error = str(exc)
        finally:
            watchdog.cancel()
            monitor_done.set()
            observer.join(2)
        if not observation:
            observation.update(strategy.report())
        before_dispose = factory.production_trade_snapshot()
        shutdown = factory.production_shutdown_diagnostics()
        node.dispose()
        return {
            "identity": identity,
            "node_error": node_error,
            "observation": observation,
            "final_snapshot": before_dispose,
            "shutdown": shutdown,
            "readonly": factory.read_only_snapshot(),
            "watchdog_fired": watchdog_fired.is_set(),
            "elapsed_secs": time.monotonic() - begin,
            "requests": peer.requests,
            "frames": peer.frames,
            "venue_quantity": str(peer.quantity),
            "venue_fills": peer.fills,
            "venue_quantities": {
                name: str(quantity) for name, quantity in peer.quantities.items()
            },
            "continued_in_same_strategy": stages.get("phase") == "done",
            "continued_instruments": stages["completed_instruments"],
            "entry_observations": stages["entry_observations"],
            "journal_exists": journal.is_file(),
            "all_endpoints_loopback": True,
            "raw_authentication_recorded": False,
        }
    finally:
        monitor_done.set()
        if node is not None:
            node.dispose()
        peer.close()


def _run(mode: str, journal: Path) -> dict:
    result = subprocess.run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--continuous-child",
            mode,
            str(journal),
        ],
        cwd=ROOT,
        env=_environment(),
        capture_output=True,
        text=True,
        timeout=210,
    )
    records = [
        line[len(RESULT) :]
        for line in result.stdout.splitlines()
        if line.startswith(RESULT)
    ]
    assert records, (
        f"installed native child exited {result.returncode}: {result.stderr[-4000:]}"
    )
    record = json.loads(records[-1])
    journal.with_suffix(".evidence.json").write_text(
        json.dumps(record, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    assert result.returncode == 0, record
    return record


def test_installed_continuous_startup_and_followup_use_same_native_strategy(
    tmp_path,
) -> None:
    result = _run("matching", tmp_path / "ledger.json")
    report = result["observation"]
    assert result["node_error"] is None, result
    assert result["watchdog_fired"] is False
    assert result["continued_in_same_strategy"] is True
    assert report["startup_verified"] is True and report["failures"] == []
    assert report["phase"] == "continuous"
    assert report["open_positions"] == 0 and Decimal(report["net_position"]) == 0
    assert Decimal(result["venue_quantity"]) == 0
    assert result["continued_instruments"] == [
        "BTC-USD-PERP.ONDO",
        "TSLA-USD-PERP.ONDO",
    ]
    assert all(
        Decimal(quantity) == 0 for quantity in result["venue_quantities"].values()
    )
    assert set(report["net_positions_by_instrument"]) == set(
        result["continued_instruments"]
    )
    assert all(
        Decimal(quantity) == 0
        for quantity in report["net_positions_by_instrument"].values()
    )
    assert all(count == 0 for count in report["open_positions_by_instrument"].values())
    entry_observations = {
        item["instrument_id"]: item for item in result["entry_observations"]
    }
    assert set(entry_observations) == set(result["continued_instruments"])
    for name, expected in (
        ("BTC-USD-PERP.ONDO", Decimal("0.0001")),
        ("TSLA-USD-PERP.ONDO", Decimal("0.01")),
    ):
        entry = entry_observations[name]
        assert Decimal(entry["cached_open_quantity"]) == expected
        assert Decimal(entry["portfolio_net_position"]) == expected
        assert Decimal(entry["confirmed_filled_quantity"]) == expected
    assert result["journal_exists"] is True
    assert result["readonly"]["identity_match"] == "matched"
    assert result["readonly"]["account_state_events"] >= 2
    assert result["final_snapshot"]["complete"] is True
    assert result["final_snapshot"]["reconciled_flat"] is True
    assert result["final_snapshot"]["unknown_submissions"] == 0
    assert result["final_snapshot"]["own_open_orders"] == 0

    writes = [request for request in result["requests"] if request["method"] != "GET"]
    creates = [request for request in writes if request["method"] == "POST"]
    cancels = [request for request in writes if request["method"] == "DELETE"]
    assert len(creates) == 8 and len(cancels) == 2
    bodies = [request["body"] for request in creates]
    ids = [body["clientOrderId"] for body in bodies]
    assert len(set(ids)) == 8
    assert bodies[0]["postOnly"] is True and bodies[0]["timeInForce"] == "GTC"
    assert bodies[1]["postOnly"] is True and bodies[1]["timeInForce"] == "GTC"
    assert Decimal(bodies[0]["price"]) != Decimal(bodies[1]["price"])
    assert ids[0] in cancels[0]["path"] and ids[1] in cancels[1]["path"]
    assert all(Decimal(body["size"]) == Decimal("0.0001") for body in bodies[:6])
    assert all(Decimal(body["size"]) == Decimal("0.01") for body in bodies[6:])
    assert all(body["market"] == "BTC-USD.P" for body in bodies[:6])
    assert all(body["market"] == "TSLA-USD.P" for body in bodies[6:])
    assert bodies[2]["timeInForce"] == "IOC" and bodies[2]["reduceOnly"] is False
    assert bodies[3]["timeInForce"] == "IOC" and bodies[3]["reduceOnly"] is True
    assert bodies[4]["reduceOnly"] is False and bodies[5]["reduceOnly"] is True
    assert bodies[6]["reduceOnly"] is False and bodies[7]["reduceOnly"] is True
    assert all(Decimal(body["size"]) * Decimal(body["price"]) <= 20 for body in bodies)

    operations = report["operations"]
    cancellation = next(
        op for op in operations if op["operation"] == "cancel" and op["role"] == "maker"
    )
    replacement = next(
        op
        for op in operations
        if op["operation"] == "create" and op["role"] == "replacement"
    )
    confirmation = next(
        event for event in cancellation["events"] if event["kind"] == "canceled"
    )
    assert replacement["requested_monotonic"] >= confirmation["observed_monotonic"]
    for op in operations:
        assert op["requested_unix_nanos"] > 0 and op["events"]
        assert all(
            event["elapsed_secs"] >= 0 and event["ts_init"] > 0
            for event in op["events"]
        )
    assert len(report["fills"]) == 6 and len(result["venue_fills"]) == 6
    btc_fills = [
        fill for fill in report["fills"] if fill["instrument_id"] == "BTC-USD-PERP.ONDO"
    ]
    tsla_fills = [
        fill
        for fill in report["fills"]
        if fill["instrument_id"] == "TSLA-USD-PERP.ONDO"
    ]
    assert len(btc_fills) == 4 and len(tsla_fills) == 2
    assert all(Decimal(fill["quantity"]) == Decimal("0.0001") for fill in btc_fills)
    assert all(Decimal(fill["quantity"]) == Decimal("0.01") for fill in tsla_fills)
    assert Decimal(report["fees_by_currency"]["USDC"]) == Decimal("0.06")
    assert Decimal(report["account_balances"]["USDC"]["free"]) == Decimal("4999.94")
    assert report["strategy_pnl_claimed"] is False
    assert any(
        frame["op"] == "unsubscribe"
        and frame["channel"] == "cancelAllOrdersAfterPerps"
        and not frame["has_timeout"]
        for frame in result["frames"]
    )
    assert (
        result["all_endpoints_loopback"] and not result["raw_authentication_recorded"]
    )


def test_installed_continuous_cancel_fill_race_refuses_replacement(tmp_path) -> None:
    result = _run("cancel-race", tmp_path / "ledger.json")
    report = result["observation"]
    assert result["watchdog_fired"] is False
    assert report["startup_verified"] is False and report["failures"]
    assert result["continued_in_same_strategy"] is False
    creates = [request for request in result["requests"] if request["method"] == "POST"]
    assert len(creates) == 1
    assert creates[0]["body"]["postOnly"] is True
    assert result["journal_exists"] is True
    assert "shutdown left work unresolved" in result["node_error"]
    assert Decimal(result["venue_quantity"]) == Decimal("0.0001")
    assert (
        result["final_snapshot"] is None
        or result["final_snapshot"]["complete"] is False
    )


def test_continuous_defaults_do_not_enable_production() -> None:
    session = ContinuousSession()
    session.validate()
    assert session.environment == "sandbox" and session.execution_enabled is False
    assert session.api_key is None and session.api_secret is None
    assert session.mainnet_authorized is False


@pytest.mark.parametrize(
    "changes",
    [
        {"max_notional_per_order_usd": "100.01"},
        {"max_gross_exposure_usd": "500.01"},
        {"max_notional_per_order_usd": 20.0},
        {"min_available_margin_usdc": "0"},
        {"max_live_orders": 0},
        {"max_requests_per_second": True},
        {"max_unresolved_orders": 3},
        {"max_gross_exposure_usd": "NaN"},
    ],
)
def test_application_preserves_explicit_native_limit_boundaries(changes) -> None:
    with pytest.raises((ValueError, TypeError)):
        replace(ContinuousLimits(), **changes).validate()


def test_production_execution_requires_fresh_explicit_binding_and_authorization(
    tmp_path,
) -> None:
    session = ContinuousSession(environment="production", execution_enabled=True)
    with pytest.raises(ValueError, match="identity"):
        session.validate()
    session = replace(
        session,
        expected_venue_account_id="reviewed-account",
        diagnostics_run_id="reviewed-run",
        journal_path=tmp_path / "ledger.json",
    )
    with pytest.raises(ValueError, match="authorization"):
        session.validate()
    # Loopback peer construction carries no mainnet permission and cannot resolve a remote host.
    replace(
        session,
        base_url_http="http://127.0.0.1:1234",
        base_url_ws="ws://127.0.0.1:5678/ws",
    ).validate()
    with pytest.raises(ValueError, match="authorization"):
        replace(
            session,
            base_url_http="http://127.0.0.1.evil.example",
            base_url_ws="ws://127.0.0.1:5678/ws",
        ).validate()


if __name__ == "__main__":
    try:
        if len(sys.argv) != 4 or sys.argv[1] != "--continuous-child":
            raise ValueError(
                "expected explicit continuous loopback child mode and journal"
            )
        print(
            RESULT + json.dumps(_child(sys.argv[2], Path(sys.argv[3])), sort_keys=True)
        )
    except BaseException:
        traceback.print_exc()
        raise SystemExit(1)

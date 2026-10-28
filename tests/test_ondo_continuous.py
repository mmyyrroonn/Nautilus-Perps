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

    market_test = mode.startswith("market")
    peer = ContinuousPeer(
        fill_on_cancel=mode == "cancel-race",
        empty_market_entry=mode == "market-empty",
    )
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
            startup_mode="market" if market_test else "maker",
            on_continuous_quote=None if market_test else policy,
        )
        handle = node.handle()

        def monitor() -> None:
            while not monitor_done.wait(0.05):
                if strategy.failures or (market_test and strategy.startup_verified):
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


def test_installed_market_opens_and_closes_long_and_short(tmp_path) -> None:
    result = _run("market", tmp_path / "market-ledger.json")
    report = result["observation"]
    assert result["node_error"] is None, result
    assert not result["watchdog_fired"] and not report["failures"]
    assert report["startup_verified"] is True
    assert report["startup_mode"] == "market"
    assert report["market_completed_round_trips"] == ["long", "short"]
    writes = [row for row in result["requests"] if row["method"] != "GET"]
    assert len(writes) == 4 and all(row["method"] == "POST" for row in writes)
    bodies = [row["body"] for row in writes]
    assert [row["side"] for row in bodies] == ["buy", "sell", "sell", "buy"]
    assert [row["reduceOnly"] for row in bodies] == [False, True, False, True]
    assert all(row["type"] == "market" and not row["postOnly"] for row in bodies)
    assert all("price" not in row and "timeInForce" not in row for row in bodies)
    assert all(row["market"] == "BTC-USD.P" for row in bodies)
    assert len({row["clientOrderId"] for row in bodies}) == 4
    assert all(Decimal(row["size"]) == Decimal("0.0001") for row in bodies)
    assert [
        Decimal(row["portfolio_net_position"])
        for row in report["market_position_observations"]
    ] == [
        Decimal("0.0001"),
        Decimal("-0.0001"),
    ]
    assert [row["direction"] for row in result["venue_fills"]] == [
        "openLong",
        "closeLong",
        "openShort",
        "closeShort",
    ]
    assert len(report["fills"]) == 4
    assert Decimal(report["fees_by_currency"]["USDC"]) == Decimal("0.04")
    assert report["open_positions"] == 0 and Decimal(report["net_position"]) == 0
    assert Decimal(result["venue_quantity"]) == 0
    assert result["final_snapshot"]["complete"] is True
    assert result["final_snapshot"]["reconciled_flat"] is True
    assert result["final_snapshot"]["unknown_submissions"] == 0
    assert result["final_snapshot"]["own_open_orders"] == 0


def test_installed_empty_market_entry_never_opens_next_direction(tmp_path) -> None:
    result = _run("market-empty", tmp_path / "empty-ledger.json")
    report = result["observation"]
    assert result["watchdog_fired"] is False
    assert report["startup_verified"] is False and report["failures"]
    assert report["market_completed_round_trips"] == []
    assert report["fills"] == []
    writes = [row for row in result["requests"] if row["method"] != "GET"]
    assert len(writes) == 1 and writes[0]["body"]["type"] == "market"
    assert Decimal(result["venue_quantity"]) == 0
    assert any("without a fill" in reason for reason in report["failures"])


def _market_state(
    step, filled, *, entry_filled="0.0002", net="0", ready=True, fill_coverage=True
):
    from types import SimpleNamespace
    from unittest.mock import Mock
    from ondo_continuous import OndoContinuousStrategy

    def amount(value):
        return SimpleNamespace(as_decimal=lambda: Decimal(value))

    roles = (
        "market_open_long",
        "market_close_long",
        "market_open_short",
        "market_close_short",
    )
    orders = {
        "open-long": SimpleNamespace(is_closed=True, filled_qty=amount(entry_filled)),
        "open-short": SimpleNamespace(is_closed=True, filled_qty=amount(entry_filled)),
        "current": SimpleNamespace(is_closed=True, filled_qty=amount(filled)),
    }
    state = SimpleNamespace(
        config=SimpleNamespace(
            instrument_id="BTC-USD-PERP.ONDO", account_id="ONDO-TEST"
        ),
        _market_step=step,
        _startup_ids={
            "market_open_long": "open-long",
            "market_open_short": "open-short",
            roles[step]: "current",
        },
        _market_completed=[],
        _market_position_observations=[],
        cache=SimpleNamespace(
            order=lambda name: orders[str(name)],
            positions_open=lambda **kwargs: (
                []
                if Decimal(net) == 0
                else [SimpleNamespace(quantity=amount(str(abs(Decimal(net)))))]
            ),
        ),
        portfolio=SimpleNamespace(net_position=lambda *args: Decimal(net)),
        _native_ready=lambda **kwargs: ready,
        _fills_complete=lambda *args: fill_coverage,
        submit_market=Mock(),
        _fail=Mock(),
        startup_verified=False,
        phase="pending",
    )
    state._application_flat = lambda: OndoContinuousStrategy._application_flat(state)
    return state


@pytest.mark.parametrize(
    "step,net,close_side", [(0, "0.0001", "SELL"), (2, "-0.0001", "BUY")]
)
def test_terminal_partial_market_entry_closes_only_proven_quantity(
    step, net, close_side
):
    from ondo_continuous import OndoContinuousStrategy

    state = _market_state(step, "0.0001", net=net)
    OndoContinuousStrategy._advance_market(state)
    assert state._market_step == step + 1
    assert Decimal(
        state._market_position_observations[0]["portfolio_net_position"]
    ) == Decimal(net)
    OndoContinuousStrategy._advance_market(state)
    args, kwargs = state.submit_market.call_args
    assert str(args[1]) == close_side and args[2] == Decimal("0.0001")
    assert kwargs == {"reduce_only": True}
    state._fail.assert_not_called()


def test_partial_market_close_stops_before_another_opening():
    from ondo_continuous import OndoContinuousStrategy

    state = _market_state(1, "0.0001", entry_filled="0.0002", net="0.0001")
    OndoContinuousStrategy._advance_market(state)
    state._fail.assert_called_once()
    assert state._market_step == 1 and state._market_completed == []
    state.submit_market.assert_not_called()


@pytest.mark.parametrize(
    "changes", [{"ready": False}, {"fill_coverage": False}, {"net": "0.0002"}]
)
def test_market_transition_waits_for_native_fills_and_portfolio_agreement(changes):
    from ondo_continuous import OndoContinuousStrategy

    state = _market_state(0, "0.0001", **({"net": "0.0001"} | changes))
    OndoContinuousStrategy._advance_market(state)
    assert state._market_step == 0
    state.submit_market.assert_not_called()


@pytest.mark.parametrize(
    "changes", [{"ready": False}, {"net": "0.0001"}, {"fill_coverage": False}]
)
def test_market_next_direction_requires_reconciled_flat(changes):
    from ondo_continuous import OndoContinuousStrategy

    state = _market_state(1, "0.0002", **changes)
    OndoContinuousStrategy._advance_market(state)
    assert state._market_step == 1 and state._market_completed == []
    state.submit_market.assert_not_called()


@pytest.mark.parametrize(
    "change,complete",
    [
        ({}, True),
        ({"commission": None}, False),
        ({"currency": None}, False),
        ({"quantity": "0.00005"}, False),
    ],
)
def test_market_fill_coverage_uses_all_fills_and_requires_fees(change, complete):
    from types import SimpleNamespace
    from ondo_continuous import OndoContinuousStrategy

    state = SimpleNamespace(
        fills={
            ("entry", "first"): {
                "role": "market_open_long",
                "quantity": "0.0001",
                "commission": "0.001",
                "currency": "USDC",
            },
            ("entry", "second"): {
                "role": "market_open_long",
                "quantity": "0.0001",
                "commission": "0.001",
                "currency": "USDC",
                **change,
            },
            ("other", "fill"): {
                "role": "market_close_long",
                "quantity": "0.0002",
                "commission": "0.002",
                "currency": "USDC",
            },
        }
    )
    order = SimpleNamespace(
        filled_qty=SimpleNamespace(as_decimal=lambda: Decimal("0.0002"))
    )
    assert (
        OndoContinuousStrategy._fills_complete(state, "market_open_long", order)
        is complete
    )


def test_continuous_defaults_do_not_enable_production() -> None:
    session = ContinuousSession()
    session.validate()
    assert session.environment == "sandbox" and session.execution_enabled is False
    assert session.api_key is None and session.api_secret is None


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


def test_crypto_capacity_is_exact_and_scoped_to_btc_eth() -> None:
    limits = ContinuousLimits(max_notional_per_order_usd="500", max_gross_exposure_usd="2000")
    instruments = ("BTC-USD-PERP.ONDO", "ETH-USD-PERP.ONDO")
    ContinuousSession(instrument_ids=instruments, limits=limits).validate()
    for changes in (
        {"max_notional_per_order_usd": "500.00000000000000000001"},
        {"max_gross_exposure_usd": "2000.00000000000000000001"},
    ):
        with pytest.raises(ValueError):
            replace(limits, **changes).validate(instrument_ids=instruments)
    with pytest.raises(ValueError):
        limits.validate(instrument_ids=instruments + ("NVDA-USD-PERP.ONDO",))


def test_production_execution_requires_explicit_account_binding(
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
    session.validate()


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

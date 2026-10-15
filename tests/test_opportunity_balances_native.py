"""Installed native balances and a real cache with synthetic loopback account data."""
from decimal import Decimal
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from opportunity_balances import native_balance_snapshot
from opportunity_native_execution import NativeOrderGateway


@pytest.mark.parametrize("venue", ["HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"])
def test_real_native_account_amounts_and_query_routing(venue):
    from nautilus_trader import model
    from nautilus_trader.core import UUID4

    client = "HYPERLIQUID" if venue == "HL" else venue
    account_id = model.AccountId.from_str(client + "-001")
    stamp = 1_800_000_000_000_000_000
    balance = model.AccountBalance(model.Money.from_str("123.456789 USDC"),
        model.Money.from_str("3.000000 USDC"), model.Money.from_str("120.456789 USDC"))
    event = model.AccountState(account_id, model.AccountType.MARGIN, [balance], [],
                               True, UUID4(), 0, stamp)
    account = model.MarginAccount(event, False)
    cache = SimpleNamespace(account=lambda requested: account if requested == account_id else None)
    queries = []
    strategy = SimpleNamespace(cache=cache, query_account=lambda *a, **kw: queries.append((a, kw)))
    gateway = NativeOrderGateway(strategy, [], lambda _: None, lambda _: None, lambda _: None)
    try:
        gateway.query_balance(venue)
        snapshot = gateway.read_balance(venue, stamp + 1, 15000)
        assert Decimal(snapshot["balances"][0]["total"]) == Decimal("123.456789")
        assert snapshot["observed_ns"] == stamp
        assert snapshot == native_balance_snapshot(cache, venue, account_id, stamp + 1, 15000)
        if venue == "ONDO":
            assert queries == []  # Its continuous account cycle supplies updates.
        else:
            assert queries == [((account_id,), {"client_id": model.ClientId.from_str(client)})]
    finally:
        gateway.close()


def test_readonly_loopback_balance_reaches_real_engine_cache(tmp_path, monkeypatch):
    import time
    import backpack_account
    import test_backpack_account_native as native
    from opportunity_balances import BalanceUnavailable

    observed = []
    original = backpack_account.engine_observation

    def inspect(cache, account_id, venue):
        try:
            observed.append(native_balance_snapshot(cache, "BACKPACK", account_id, time.time_ns(), 15000))
        except BalanceUnavailable:
            pass  # No event has arrived yet during initial startup polling.
        return original(cache, account_id, venue)

    monkeypatch.setattr(backpack_account, "engine_observation", inspect)
    native.test_actual_readonly_node_balances_true_fills_and_no_writes(tmp_path, monkeypatch)
    assert observed
    assert any(any(Decimal(row["free"]) == 111 for row in item["balances"]) for item in observed)
    assert all(item["source"] == "native_reported_account_state" for item in observed)


def test_balance_only_node_reads_loopback_without_market_data_or_mutations(tmp_path, monkeypatch):
    import asyncio
    from dataclasses import replace
    from nautilus_trader.adapters import backpack as bp
    import opportunity_native_execution
    from opportunity_balance_runner import build_balance_node
    from opportunity_live_runner import load_live_plan
    from test_backpack_account_native import AccountPeer, SEED, PUBLIC, SYMBOL

    plan = load_live_plan(Path(__file__).resolve().parents[1] / "config/opportunity-trade.live.toml")
    plan = replace(plan, venues=("BACKPACK",))
    async def scenario():
        async with AccountPeer(emit_orders=False) as peer:
            def clients(scan, environment, **kwargs):
                assert kwargs["read_only"] is True
                assert len(scan.markets) == 1 and scan.markets[0].venue == "BACKPACK"
                endpoints = {"base_url_http": peer.http_url, "base_url_ws": peer.ws_url}
                credential = bp.BackpackCredential(SEED, **endpoints)
                config = bp.BackpackExecutionClientConfig([SYMBOL], credential, "BACKPACK-001", PUBLIC,
                    str(tmp_path / "identity"), connect_timeout_ms=5000, read_timeout_ms=3000,
                    shutdown_timeout_ms=3000, quota=bp.BackpackQuota(), **endpoints)
                return [("BACKPACK", bp.BackpackExecutionClientFactory(), config)]
            monkeypatch.setattr(opportunity_native_execution, "execution_clients", clients)
            node, strategy, check = build_balance_node(plan, {}, tmp_path / "balance-only")
            try:
                await asyncio.wait_for(node.run_async(), 30)
                assert strategy.failure is None
                result = check.document()
            finally:
                node.dispose()
            assert result["result"] == "passed", result
            rows = result["venues"]["BACKPACK"]["balances"]
            assert any(row["currency"] == "USDC" and Decimal(row["total"]) > 0 for row in rows)
            assert peer.authenticated_reads
            assert all(method == "GET" for method, _, _ in peer.requests)
    asyncio.run(scenario())


def test_readonly_factory_configs_do_not_create_production_writers(tmp_path):
    from nautilus_trader.adapters import backpack as bp, ondo
    from test_backpack_account_native import SEED, PUBLIC
    from opportunity_native_execution import execution_clients
    from opportunity_live_runner import load_live_plan
    from dataclasses import replace
    plan = load_live_plan(Path(__file__).resolve().parents[1] / "config/opportunity-trade.live.toml")
    scan = replace(plan.scan, markets=tuple(m for m in plan.scan.markets
                                           if m.venue in {"ONDO", "BACKPACK"} and m.symbol == "BTC"))
    env = {"ONDO_MAINNET_API_KEY": "readonly-fixture", "ONDO_MAINNET_API_SECRET": "fixture-secret",
           "ONDO_MAINNET_ACCOUNT_ID": "fixture-account", "BACKPACK_MAINNET_API_SECRET": SEED,
           "BACKPACK_MAINNET_API_KEY": PUBLIC}
    configs = execution_clients(scan, environ=env, state_directory=tmp_path, run_id="readonly",
                                order_notional_usd="0", max_gross_notional_usd="0", read_only=True)
    assert len(configs) == 2
    for client, factory, config in configs:
        if client == "ONDO":
            assert isinstance(factory, ondo.OndoExecutionClientFactory)
            assert config.account_read_only and not config.allow_production_orders
            assert config.continuous_execution is None
        else:
            assert isinstance(factory, bp.BackpackExecutionClientFactory)
            assert isinstance(config, bp.BackpackExecutionClientConfig)
            assert not isinstance(config, bp.BackpackProductionExecutionClientConfig)


@pytest.mark.parametrize("role", ["user", "agent"])
def test_balance_account_discovery_resolves_agent_to_owner_without_exposing_key(role):
    from eth_account import Account
    from opportunity_balance_runner import prepare_balance_environment
    key = "0x" + "11" * 32  # Public synthetic fixture, not operator material.
    signer = Account.from_key(key).address.lower()
    owner = "0x" + "ab" * 20
    calls = []
    def transport(method, url, payload):
        calls.append(payload)
        return {"role": role, "data": {"user": owner}}
    env = prepare_balance_environment(("HL",), "mainnet", {"HYPERLIQUID_PRIVATE_KEY": key}, transport=transport)
    assert env["HYPERLIQUID_ACCOUNT_ADDRESS"] == (signer if role == "user" else owner)
    assert calls == [{"type": "userRole", "user": signer}]
    assert all(key not in str(call) for call in calls)


def test_balance_account_discovery_never_guesses_owner_on_private_failure():
    from opportunity_balance_runner import prepare_balance_environment, BalanceSetupError
    key = "0x" + "11" * 32
    def failed(*a):
        raise RuntimeError("private transport payload " + key)
    with pytest.raises(BalanceSetupError) as caught:
        prepare_balance_environment(("HL",), "mainnet", {"HYPERLIQUID_PRIVATE_KEY": key}, transport=failed)
    assert "account discovery failed" in str(caught.value)
    assert key not in str(caught.value) and "private transport payload" not in str(caught.value)

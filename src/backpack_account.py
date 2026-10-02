"""Bounded account observation through native readonly clients and engine cache."""
from __future__ import annotations

import json
import os

from backpack_config import BackpackConfigError
from backpack_public import APP_ROOT, SHUTDOWN_SECS, _observer, encoded, run_native_observation


def credential_seed(plan):
    """Resolve the one explicit runtime credential; never perform implicit dotenv discovery."""
    reference = plan.account.credential_env
    seed = os.environ.get(reference)
    if seed is None:
        path = APP_ROOT / ".env"
        if path.is_file():
            from dotenv import dotenv_values
            seed = dotenv_values(path, interpolate=False).get(reference)
    if not isinstance(seed, str) or not seed:
        raise BackpackConfigError("the configured Backpack account credential is unavailable")
    return seed


class AccountTelemetry:
    def __init__(self, public, account):
        self.public = public
        self.account = account

    def telemetry_snapshot_json(self):
        return json.dumps({"schema_version": 1,
            "public": json.loads(self.public.telemetry_snapshot_json()),
            "account": json.loads(self.account.telemetry_snapshot_json()),
            "execution_ready": False, "account_identity_verified": False,
            "durable_economic_acknowledgement": False})


def engine_observation(cache, account_id, venue):
    """Project exact engine-observed values without interpreting cache absence as flat."""
    account = cache.account(account_id)
    balances = []
    if account is not None:
        values = account.balances()
        if len(values) > 256:
            raise BackpackConfigError("account balance evidence exceeds observation bound")
        balances = [{"currency": str(currency), "total": str(balance.total),
                     "free": str(balance.free), "locked": str(balance.locked)}
                    for currency, balance in sorted(values.items(), key=lambda item: str(item[0]))]
    orders = cache.orders(venue=venue, account_id=account_id)
    positions = cache.positions(venue=venue, account_id=account_id)
    if len(orders) > 1024 or len(positions) > 1024:
        raise BackpackConfigError("account cache evidence exceeds observation bound")
    return {"account_observed": account is not None, "wallet_trading_balances": balances,
        "orders": [{"client_order_id": str(order.client_order_id),
                    "instrument_id": str(order.instrument_id), "status": str(order.status),
                    "quantity": str(order.quantity), "filled_quantity": str(order.filled_qty),
                    "trade_ids": [str(value) for value in order.trade_ids],
                    "commissions": [str(value) for value in order.commissions().values()]}
                   for order in sorted(orders, key=lambda value: str(value.client_order_id))],
        "positions": [{"instrument_id": str(position.instrument_id),
                       "position_id": str(position.id), "side": str(position.side),
                       "quantity": str(position.quantity), "is_open": position.is_open,
                       "last_trade_id": None if position.last_trade_id is None else str(position.last_trade_id),
                       "realized_pnl": None if position.realized_pnl is None else str(position.realized_pnl),
                       "commissions": [str(value) for value in position.commissions()]}
                      for position in sorted(positions, key=lambda value: str(value.id))],
        "complete_account_coverage": False, "margin_coverage_verified": False,
        "flat_verified": False, "durable_economic_acknowledgement": False}



def account_failure(health):
    """Fixed safe reasons from observational native health; expected evidence gaps stay unverified."""
    account = health["account"]
    gaps = set(account["evidence_gaps"])
    if account["parse_failures"] or "PrivateParseOrDeliveryFailure" in gaps:
        return "account_private_event_failed"
    if "PrivateSubscriptionTransportFailure" in gaps:
        return "account_private_subscription_transport_failed"
    if "RecoveryIncomplete" in gaps:
        return "account_recovery_incomplete"
    if gaps.intersection({"PrivateQueueOverflow", "PrivateFrameBound", "UnexpectedPrivateBinary"}):
        return "account_private_input_failed"
    if not account["rest_snapshot_observed"]:
        return "account_snapshot_unavailable"
    if not account["transport_connected"]:
        return "account_transport_disconnected"
    return None


def _account_configuration(plan, seed):
    from nautilus_trader.adapters.backpack import (
        BackpackCredential, BackpackDataClientConfig, BackpackDataClientFactory,
        BackpackExecutionClientConfig,
        BackpackInstrumentEconomics, BackpackQuota)
    endpoints = ({"base_url_http": plan.http_origin, "base_url_ws": plan.ws_origin}
                 if plan.environment == "loopback" else {})
    quota = BackpackQuota()
    credential = BackpackCredential(seed, **endpoints)
    public = BackpackDataClientConfig(list(plan.symbols),
        {symbol: BackpackInstrumentEconomics(**economics.document())
         for symbol, economics in plan.economics},
        http_timeout_secs=plan.request_timeout_secs,
        ws_connect_timeout_secs=min(10, plan.request_timeout_secs),
        shutdown_timeout_secs=SHUTDOWN_SECS, quote_stale_after_ms=plan.stale_after_ms, **endpoints)
    account = BackpackExecutionClientConfig(list(plan.symbols), credential, plan.account.account_id,
        plan.account.venue_account, str(plan.journal_dir), quota=quota,
        subaccount=plan.account.subaccount, connect_timeout_ms=plan.request_timeout_secs * 1000,
        shutdown_timeout_ms=SHUTDOWN_SECS * 1000,
        read_timeout_ms=plan.request_timeout_secs * 1000, input_capacity=256,
        fill_capacity=10000, page_size=1000, max_pages=10, max_items=10000, **endpoints)
    data_factory = BackpackDataClientFactory(quota=quota)
    if not account.quota.shares_scope(quota) or not data_factory.quota.shares_scope(quota):
        raise BackpackConfigError("native account quota is not the shared session scope")
    return public, account, data_factory


def _account_node(plan, evidence, public, account, data_factory, execution_factory, *, name, exec_config=None):
    """Share the actual native owner lifecycle across explicitly selected factories."""
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import AccountId, TraderId, Venue

    evidence.private_client_registered = True
    evidence.durable_state_opened = None  # A failed native build may already have opened its directory.
    builder = (LiveNode.builder(name, TraderId.from_str(name), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.OFF, fileout_level=LogLevel.OFF,
                                   bypass_logging=True, print_config=False))
        .with_timeout_connection(plan.request_timeout_secs).with_reconciliation(False)
        .with_timeout_reconciliation(0).with_timeout_portfolio(0)
        .with_timeout_disconnection_secs(SHUTDOWN_SECS)
        .with_delay_post_stop_secs(0).with_delay_shutdown_secs(0)
        .with_load_state(False).with_save_state(False)
        .add_data_client(None, data_factory, public)
        .add_exec_client(None, execution_factory, account))
    if exec_config is not None:
        builder = builder.with_exec_engine_config(exec_config)
    node = builder.build()
    evidence.durable_state_opened = True
    evidence.handle = node.handle()
    node.add_actor(_observer(plan, evidence))
    cache = node.cache
    account_id = AccountId.from_str(plan.account.account_id)
    venue = Venue.from_str("BACKPACK")
    previous = None
    def poll(_node, writer):
        nonlocal previous
        observation = engine_observation(cache, account_id, venue)
        payload = encoded(observation)
        if payload != previous:
            writer.record("engine_account_observation", observation)
            previous = payload
    evidence.poll = poll
    evidence.failure_check = account_failure
    evidence.extra_summary = {"account_state_semantics": "wallet trading balances; incomplete coverage",
        "native_reconciliation_enabled": False, "durable_economic_acknowledgement": False,
        "flat_verified": False, "shared_native_rest_quota": True}
    return node


def build_account(plan, evidence, seed):
    from nautilus_trader.adapters.backpack import BackpackExecutionClientFactory

    public, account, data_factory = _account_configuration(plan, seed)
    execution_factory = BackpackExecutionClientFactory()
    node = _account_node(plan, evidence, public, account, data_factory, execution_factory,
                         name="BACKPACK-READONLY")
    return node, AccountTelemetry(public, account), {
        "schema_version": 1, "public": json.loads(data_factory.capabilities_json()),
        "account": json.loads(execution_factory.capabilities_json())}


async def run_account(plan, *, candidate=None):
    """Execute an explicitly requested readonly account plan; no mutations are configured."""
    if plan.mode != "account-readonly" or plan.account is None or plan.environment == "offline":
        raise BackpackConfigError("account runtime requires an explicit account-readonly plan")
    seed = credential_seed(plan)
    return await run_native_observation(plan, candidate=candidate, extra_sources=("backpack_account.py",),
        builder=lambda checked_plan, evidence: build_account(checked_plan, evidence, seed))

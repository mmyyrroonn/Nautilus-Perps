"""Finite balance-only session using native account clients, without a trading ledger."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import time
from types import MappingProxyType
import uuid

from opportunity_balances import NativeBalanceReader, StartupBalanceCheck


def build_balance_node(plan, environ, state_directory):
    from nautilus_trader.common import CacheConfig, Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import StrategyId, TraderId
    from nautilus_trader.trading import Strategy, StrategyConfig
    from opportunity_native_execution import execution_clients

    check = StartupBalanceCheck(plan.venues, enabled=True, timeout_secs=plan.startup_timeout_secs,
                                max_age_ms=plan.account_max_age_ms)
    # One configured market per venue suffices for account startup. No public
    # book subscriptions, paired strategy, trading journal, or writer authority.
    markets = tuple(min((m for m in plan.scan.markets if m.venue == venue),
                        key=lambda m: (m.symbol != "BTC", m.instrument_id)) for venue in plan.venues)
    builder = (LiveNode.builder("BALANCE-CHECK", TraderId.from_str("BALANCE-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.OFF, fileout_level=LogLevel.OFF,
                                   bypass_logging=True, print_config=False))
        .with_cache_config(CacheConfig(save_market_data=False, persist_account_events=False))
        .with_exec_engine_config(LiveExecutionEngineConfig(reconciliation=False,
            generate_missing_orders=False, inflight_check_interval_ms=0, filter_unclaimed_external_orders=True))
        .with_reconciliation(False).with_load_state(False).with_save_state(False)
        .with_timeout_connection(plan.scan.connection_timeout_secs)
        .with_timeout_reconciliation(0).with_timeout_portfolio(0)
        .with_timeout_disconnection_secs(10).with_delay_post_stop_secs(0).with_delay_shutdown_secs(0))
    for client_id, factory, config in execution_clients(replace(plan.scan, markets=markets), plan.environment,
            environ=environ, state_directory=state_directory, run_id=state_directory.name, read_only=True):
        builder = builder.add_exec_client(client_id, factory, config)
    node = builder.build()
    handle = node.handle()

    class BalanceStrategy(Strategy):
        def __init__(self):
            super().__init__(StrategyConfig(strategy_id=StrategyId.from_str("BALANCE-CHECK"),
                                            log_events=False, log_commands=False))
            self.reader = NativeBalanceReader(self)
            self.failure = None

        def on_start(self):
            self.clock.set_timer("balance-check", timedelta(milliseconds=100))

        def on_time_event(self, event):
            try:
                check.tick(self.reader, time.time_ns())
                if check.result in {"passed", "failed"}:
                    handle.stop()
            except Exception as exc:
                self.failure = "balance callback failed: " + type(exc).__name__
                handle.stop()

    strategy = BalanceStrategy()
    node.add_strategy(strategy)
    return node, strategy, check


def run_balance_check(plan):
    from dotenv import load_dotenv
    from opportunity_account_scope import resolve_account_binding, verify_execution_identity

    root = Path(__file__).resolve().parents[1]
    load_dotenv(root / ".env", override=False)
    environ = MappingProxyType(dict(os.environ))
    binding = resolve_account_binding(plan.venues, plan.environment, environ)
    verify_execution_identity(binding, environ)
    directory = root / ".native-cache" / "balance-check" / uuid.uuid4().hex
    node = None
    failure = None
    shutdown_complete = False
    check = StartupBalanceCheck(plan.venues, enabled=True, timeout_secs=plan.startup_timeout_secs,
                                max_age_ms=plan.account_max_age_ms)
    try:
        node, strategy, check = build_balance_node(plan, environ, directory)
        if resolve_account_binding(plan.venues, plan.environment, os.environ) != binding:
            raise ValueError("account environment changed during native construction")
        node.run()
        failure = strategy.failure
    except KeyboardInterrupt:
        failure = "balance check interrupted"
    except Exception as exc:
        failure = "balance session failed: " + type(exc).__name__
    finally:
        if node is not None:
            try:
                node.handle().stop()
                node.dispose()
                shutdown_complete = True
            except Exception as exc:
                failure = failure or "balance shutdown failed: " + type(exc).__name__
    if check.result not in {"passed", "failed"}:
        check.interrupt(time.time_ns())
        failure = failure or "balance session stopped before all reads completed"
    report = {"mode": "balance_check_only", "remote_writes_allowed": False,
              "failure": failure, "shutdown_complete": shutdown_complete,
              "balance_check": check.document()}
    print(json.dumps(report, indent=2))
    return 0 if check.result == "passed" and failure is None and shutdown_complete else 1

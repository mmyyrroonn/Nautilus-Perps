"""Finite balance-only session using native account clients, without a trading ledger."""
from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
import json
import os
from pathlib import Path
import re
import time
from types import MappingProxyType
import uuid

from opportunity_balances import NativeBalanceReader, StartupBalanceCheck


class BalanceSetupError(ValueError):
    """An actionable message built only from fixed text and setting names."""


def hyperliquid_owner_address(private_key, environment, transport=None):
    """Resolve an agent to its actual owner instead of reading the agent's funds."""
    from eth_account import Account
    from opportunity_account_reader import http_json
    try:
        signer = Account.from_key(private_key).address.lower()
    except Exception:
        raise BalanceSetupError("Hyperliquid private key is invalid; check HYPERLIQUID_PK or HYPERLIQUID_PRIVATE_KEY") from None
    url = ("https://api.hyperliquid.xyz/info" if environment == "mainnet"
           else "https://api.hyperliquid-testnet.xyz/info")
    try:
        role = (transport or http_json)("POST", url, {"type": "userRole", "user": signer})
        owner = signer if role.get("role") == "user" else (
            role["data"]["user"] if role.get("role") == "agent" else None)
        if not isinstance(owner, str) or not re.fullmatch(r"0[xX][0-9a-fA-F]{40}", owner):
            raise ValueError
        return owner.lower()
    except Exception:
        raise BalanceSetupError("Hyperliquid account discovery failed; check connectivity or set HYPERLIQUID_ACCOUNT_ADDRESS explicitly") from None


def prepare_balance_environment(venues, environment, environ, *, transport=None):
    """Normalize legacy settings only for this read-only session, without editing .env."""
    env = dict(environ)
    if "HL" in venues:
        prefix = "HYPERLIQUID_" if environment == "mainnet" else "HYPERLIQUID_TESTNET_"
        key_name, address_name = prefix + "PK", prefix + "ACCOUNT_ADDRESS"
        if not env.get(key_name) and env.get(prefix + "PRIVATE_KEY"):
            env[key_name] = env[prefix + "PRIVATE_KEY"]
        if not env.get(key_name):
            raise BalanceSetupError("missing Hyperliquid private key: set " + key_name + " or " + prefix + "PRIVATE_KEY")
        if not env.get(address_name):
            env[address_name] = env.get(prefix + "VAULT") or hyperliquid_owner_address(
                env[key_name], environment, transport)
    return MappingProxyType(env)


def verify_balance_identity(binding, environ):
    from opportunity_account_scope import verify_execution_identity
    try:
        verify_execution_identity(binding, environ)
    except ValueError as exc:
        # Match only our own fixed diagnostics. Never expose arbitrary upstream
        # exception text, which can contain request parameters or credentials.
        explanations = {
            "Hyperliquid signing identity is unavailable or invalid":
                "Hyperliquid private key is invalid; check HYPERLIQUID_PK or HYPERLIQUID_PRIVATE_KEY",
            "Hyperliquid signer ownership could not be verified for the bound account":
                "Hyperliquid account ownership could not be verified: check connectivity and ensure HYPERLIQUID_ACCOUNT_ADDRESS is the owning account, not the API agent address. For balance-only auto discovery, omit HYPERLIQUID_ACCOUNT_ADDRESS",
            "Hyperliquid signed target differs from the account binding":
                "HYPERLIQUID_VAULT must match HYPERLIQUID_ACCOUNT_ADDRESS",
        }
        message = explanations.get(str(exc))
        if message is not None:
            raise BalanceSetupError(message) from None
        raise


def _account_settings(environ):
    return {k: v for k, v in environ.items() if k.startswith(
        ("HYPERLIQUID_", "LIGHTER_", "ASTER_", "ONDO_", "BACKPACK_"))}


def balance_session_error(exc):
    """Classify native lifecycle errors without copying private adapter details."""
    message = str(exc)
    diagnostics = {
        "exec-connect timeout": (
            "connection_timeout",
            "native account clients exceeded the connection timeout; check connectivity and venue availability"),
        "readiness timeout while waiting for engine connections": (
            "clients_not_ready",
            "one or more native account clients did not become ready before the connection deadline; check connectivity, venue availability and API permissions"),
        "disconnect readiness timeout while waiting for engine disconnections": (
            "disconnection_timeout",
            "native account clients did not finish disconnecting before the shutdown deadline"),
    }
    for prefix, diagnostic in diagnostics.items():
        if message == prefix or message.startswith(prefix + "; failed to finalize startup abort:"):
            return diagnostic
    return "native_runtime_error", "balance session failed: " + type(exc).__name__


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


def collect_balance_check(plan, *, environ=None):
    from opportunity_account_scope import resolve_account_binding

    root = Path(__file__).resolve().parents[1]
    directory = root / ".native-cache" / "balance-check" / uuid.uuid4().hex
    node = None
    failure = None
    failure_code = None
    shutdown_complete = False
    check = StartupBalanceCheck(plan.venues, enabled=True, timeout_secs=plan.startup_timeout_secs,
                                max_age_ms=plan.account_max_age_ms)
    stage = "credentials"
    try:
        if environ is None:
            from dotenv import load_dotenv
            load_dotenv(root / ".env", override=False)
            environ = os.environ
        original_settings = _account_settings(os.environ)
        environ = prepare_balance_environment(plan.venues, plan.environment, environ)
        stage = "account_binding"
        binding = resolve_account_binding(plan.venues, plan.environment, environ)
        stage = "signer_identity"
        verify_balance_identity(binding, environ)
        stage = "native_build"
        node, strategy, check = build_balance_node(plan, environ, directory)
        if _account_settings(os.environ) != original_settings:
            raise BalanceSetupError("account settings changed during native construction; restart the balance check")
        stage = "native_startup"
        node.run()
        failure = strategy.failure
    except BalanceSetupError as exc:
        failure = str(exc)
    except KeyboardInterrupt:
        failure = "balance check interrupted"
    except Exception as exc:
        failure_code, failure = balance_session_error(exc)
    finally:
        if stage == "native_startup" and check.started_ns is not None:
            stage = "account_reads"
        if node is not None:
            try:
                node.handle().stop()
                node.dispose()
                shutdown_complete = True
            except Exception as exc:
                if failure is None:
                    stage = "shutdown"
                    failure_code, failure = balance_session_error(exc)
    if check.result not in {"passed", "failed"}:
        check.interrupt(time.time_ns())
        failure = failure or "balance session stopped before all reads completed"
    balances = check.document()
    if check.started_ns is None:
        for row in balances["venues"].values():
            row.update(result="not_run", reason="balance_check_not_started")
    report = {"mode": "balance_check_only", "remote_writes_allowed": False,
              "failure": failure, "failure_stage": stage if failure else None,
              "failure_code": failure_code,
              "shutdown_complete": shutdown_complete,
              "balance_check": balances}
    return report


def balance_session_passed(report, venues):
    """Require a complete, disposed read-only session for exactly these venues."""
    if not isinstance(report, dict):
        return False
    check = report.get("balance_check", {})
    if not isinstance(check, dict):
        return False
    rows = check.get("venues", {})
    if not isinstance(rows, dict) or not all(isinstance(row, dict) for row in rows.values()):
        return False
    return (report.get("mode") == "balance_check_only" and report.get("remote_writes_allowed") is False
        and report.get("failure") is None and report.get("shutdown_complete") is True
        and check.get("enabled") is True and check.get("result") == "passed"
        and type(check.get("started_ns")) is int and type(check.get("completed_ns")) is int
        and 0 < check["started_ns"] <= check["completed_ns"]
        and set(rows) == set(venues) and all(row.get("result") == "passed" for row in rows.values()))


def run_balance_check(plan):
    report = collect_balance_check(plan)
    print(json.dumps(report, indent=2), flush=True)
    return 0 if balance_session_passed(report, plan.venues) else 1


def _balance_worker(plan, environ, sender):
    # A separate process prevents native global runtime/logging state from the
    # finite account reader from leaking into the later production node.
    try:
        os.environ.clear()
        os.environ.update(environ)
        sender.send(collect_balance_check(plan, environ=environ))
    except BaseException:
        sender.send({"mode": "balance_check_only", "remote_writes_allowed": False,
                     "failure": "isolated balance worker failed", "shutdown_complete": False})
    finally:
        sender.close()


def run_startup_balance_session(plan, environ):
    """Complete balance reads before constructing any production client or ledger."""
    import multiprocessing
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=_balance_worker, args=(plan, dict(environ), sender))
    # Include connection, account read, native disposal and process startup budgets.
    timeout = plan.scan.connection_timeout_secs + plan.startup_timeout_secs + 60
    report = {"mode": "balance_check_only", "remote_writes_allowed": False,
              "failure": "isolated balance session did not complete", "shutdown_complete": False}
    started = False
    try:
        process.start()
        started = True
        sender.close()
        if receiver.poll(timeout):
            report = receiver.recv()
            process.join(timeout=15)
            if process.exitcode != 0:
                report = {**report, "failure": "isolated balance worker did not exit cleanly",
                          "shutdown_complete": False}
    except (EOFError, OSError, KeyboardInterrupt):
        report = {**report, "failure": "isolated balance session interrupted",
                  "shutdown_complete": False}
    finally:
        sender.close()
        receiver.close()
        if started:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            if process.is_alive():
                process.kill()
                process.join(timeout=5)
            if not process.is_alive():
                process.close()
    return report

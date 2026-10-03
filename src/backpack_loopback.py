#!/usr/bin/env python3
"""Run one explicitly synthetic numeric-loopback native Strategy scenario."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import sys
import time

from backpack_config import BackpackConfigError
from backpack_loopback_config import BackpackLoopbackPlan, load_loopback_plan


def loopback_seed(plan):
    """Resolve only the explicitly named synthetic environment value; never read dotenv."""
    seed = os.environ.get(plan.account.credential_env)
    if not isinstance(seed, str) or not seed:
        raise BackpackConfigError("explicit synthetic credential environment variable is missing")
    return seed


def _strategy(plan, evidence, control):
    from nautilus_trader.adapters.backpack import BackpackLoopbackAccountFacts
    from nautilus_trader.model import InstrumentId, OrderSide, Price, Quantity, TimeInForce
    from nautilus_trader.trading import Strategy

    class LoopbackScenario(Strategy):
        def __init__(self):
            super().__init__()
            self.started = False
            self.session = None
            self.primary = None
            self.probe = None
            self.accepted_at = None
            self.fills = 0
            self.cancel_sent = False
            self.probe_denied = False
            self.instrument = InstrumentId.from_str(f"{plan.scenario.symbol}.BACKPACK")

        def on_start(self):
            self.started = True

        def on_stop(self):
            self.started = False

        def order(self):
            return self.order_factory.limit(self.instrument, OrderSide.BUY,
                Quantity.from_str(format(plan.scenario.quantity, "f")),
                Price.from_str(format(plan.scenario.limit_price, "f")),
                time_in_force=TimeInForce.GTC, post_only=False, reduce_only=False)

        def advance(self, health):
            if not self.started or evidence.limit_reached or evidence.actor_failure:
                return
            if plan.recovery_only:
                consumer = health["loopback"].get("economic_consumer")
                evidence.extra_summary["scenario_steps_observed"] = bool(
                    consumer and consumer["durable_receipts"] > 0
                    and health["account"]["rest_snapshot_observed"])
                return
            if self.primary is None:
                if plan.durable_economics and self.cache.orders(venue=self.instrument.venue):
                    evidence.actor_failure = "loopback_existing_state_requires_recovery_only"
                    evidence.handle.stop()
                    return
                if (not health["public"]["quotes_fresh"].get(plan.scenario.symbol, False)
                        or not health["account"]["rest_snapshot_observed"]
                        or not health["account"]["transport_connected"]
                        or not health["public"]["connected"]):
                    return
                try:
                    self.session = control.begin_session()
                    observed_at_ms = time.time_ns() // 1_000_000
                    control.accept_account(self.session, BackpackLoopbackAccountFacts(
                        observed_at_ms=observed_at_ms, **plan.facts.document()))
                    evidence.record("synthetic_account_assertion", {"observed_at_ms": observed_at_ms,
                        "source": "explicit local fixture assertion; not venue evidence",
                        "net_positions": plan.facts.document()["net_positions"]})
                    control.refresh_market(self.session, str(self.instrument))
                except RuntimeError:
                    evidence.record("scenario_admission_refused", {"synthetic_only": True})
                    evidence.actor_failure = "loopback_initial_admission_refused"
                    evidence.handle.stop()
                    return
                self.primary = self.order()
                evidence.record("scenario_submit", {"client_order_id": str(self.primary.client_order_id),
                    "instrument_id": str(self.instrument), "quantity": str(self.primary.quantity),
                    "limit_price": str(self.primary.price), "synthetic_only": True})
                if evidence.limit_reached or evidence.actor_failure:
                    return
                self.submit_order(self.primary)
            elif (plan.scenario.stale_probe and self.probe is None and self.accepted_at is not None
                  and (time.monotonic() - self.accepted_at) * 1000 >= plan.scenario.stale_probe_delay_ms):
                # Deliberately keep the original native market admission. The adapter must reject
                # stale event AND receipt timestamps; application code never fabricates a quote.
                # This separate fixture assertion is explicit in the plan. Observed economics
                # must never be overwritten with the configured initial flat state.
                if (self.fills or health["loopback"]["pending_fills"]
                        or any(p.is_open for p in self.cache.positions(venue=self.instrument.venue))):
                    evidence.actor_failure = "loopback_probe_flat_assertion_contradicted"
                    evidence.handle.stop()
                    return
                observed_at_ms = time.time_ns() // 1_000_000
                control.accept_account(self.session, BackpackLoopbackAccountFacts(
                    observed_at_ms=observed_at_ms, **plan.facts.document()))
                self.probe = self.order()
                evidence.record("scenario_stale_probe", {"client_order_id": str(self.probe.client_order_id),
                    "public_quotes_fresh": health["public"]["quotes_fresh"].get(plan.scenario.symbol, False),
                    "public_health": health["public"], "synthetic_facts_observed_at_ms": observed_at_ms,
                    "authority_expires_at_ms": evidence.extra_summary["authority_expires_at_ms"],
                    "account_reasserted_without_market_refresh": True,
                    "synthetic_only": True})
                if evidence.limit_reached or evidence.actor_failure:
                    return
                self.submit_order(self.probe)
            if plan.scenario.cancel_after_fill and self.fills and not self.cancel_sent:
                current = self.cache.order(self.primary.client_order_id)
                if current is None or current.is_closed:
                    evidence.actor_failure = "loopback_owned_order_not_cancelable"
                    evidence.handle.stop()
                    return
                evidence.record("scenario_owned_cancel", {"client_order_id": str(current.client_order_id)})
                # A report limit blocks new risk. This once-only owned exit remains independently
                # authorized by the native session/identity guard even if its record was dropped.
                try:
                    self.cancel_order(current.client_order_id)
                    self.cancel_sent = True
                except Exception:
                    evidence.actor_failure = "loopback_owned_cancel_call_refused"
                    evidence.handle.stop()
                    return

            complete = (self.accepted_at is not None and self.fills > 0
                and (not plan.scenario.stale_probe or self.probe_denied)
                and (not plan.scenario.cancel_after_fill or self.cancel_sent))
            evidence.extra_summary["scenario_steps_observed"] = complete
            evidence.extra_summary["cancel_requested"] = self.cancel_sent
            evidence.extra_summary["cancel202_observed"] = "CancelPending" in health["account"]["evidence_gaps"]

        def event(self, name, event):
            evidence.record(name, {"client_order_id": str(event.client_order_id),
                "instrument_id": str(event.instrument_id), "ts_event_ns": str(event.ts_event),
                **({"reason": str(event.reason)[:256]} if hasattr(event, "reason") else {})})

        def on_order_submitted(self, event):
            self.event("order_submitted", event)

        def on_order_accepted(self, event):
            self.event("order_accepted", event)
            if self.primary is not None and event.client_order_id == self.primary.client_order_id:
                if self.accepted_at is None:
                    self.accepted_at = time.monotonic()

        def on_order_denied(self, event):
            self.event("order_denied", event)
            if self.probe is not None and event.client_order_id == self.probe.client_order_id:
                self.probe_denied = True
            else:
                evidence.actor_failure = "loopback_primary_denied"
                evidence.handle.stop()

        def on_order_rejected(self, event):
            self.event("order_rejected", event)
            evidence.actor_failure = "loopback_order_rejected"
            evidence.handle.stop()

        def on_order_filled(self, event):
            self.event("order_filled", event)
            if plan.recovery_only:
                self.fills += 1
                return
            if self.primary is None or event.client_order_id != self.primary.client_order_id:
                evidence.actor_failure = "loopback_unexpected_fill"
                evidence.handle.stop()
                return
            self.fills += 1
        def on_order_canceled(self, event):
            self.event("order_canceled", event)

    return LoopbackScenario()


class LoopbackTelemetry:
    def __init__(self, public, account, control, evidence):
        self.public, self.account, self.control = public, account, control
        self.evidence = evidence

    def telemetry_snapshot_json(self):
        from backpack_account import AccountTelemetry
        snapshot = json.loads(AccountTelemetry(self.public, self.account).telemetry_snapshot_json())
        cancel202 = "CancelPending" in snapshot["account"]["evidence_gaps"]
        self.evidence.extra_summary["cancel202_observed"] = cancel202
        shutdown = self.control.shutdown_report_json()
        report = None if shutdown is None else json.loads(shutdown)
        pending_fills = json.loads(self.control.pending_fills_json())
        consumer = self.evidence.extra_summary.get("economic_consumer")
        acknowledged = bool(consumer and consumer["durable_receipts"] > 0 and not pending_fills)
        self.evidence.extra_summary["durable_economic_acknowledgement"] = acknowledged
        self.evidence.compact_summary.update(durable_economic_acknowledgement=acknowledged,
            economic_consumer=consumer, pending_fill_count=len(pending_fills),
            native_shutdown_report=report,
            dirty_shutdown=None if report is None else report["dirty"], cancel202_observed=cancel202)
        if report is not None:
            settled = not report["dirty"] and not pending_fills
            flat = not report["positions_unknown_or_nonzero"]
            self.evidence.extra_summary.update(execution_settled=settled, flat_verified=flat)
            self.evidence.compact_summary.update(execution_settled=settled, flat_verified=flat,
                                                 exposure_unknown=not flat)
            terminal = self.evidence.extra_summary.get("terminal_evidence") or {}
            reconciled = any(row["reconciled"] for row in terminal.get("orders", []))
            pending = report["pending_cancellations"] > 0
            self.evidence.extra_summary["cancel_unsettled_observed"] = pending
            self.evidence.extra_summary["scenario_completed"] = (
                self.evidence.extra_summary.get("scenario_steps_observed", False)
                and (self.evidence.plan.recovery_only
                     or not self.evidence.plan.scenario.cancel_after_fill
                     or pending or settled or reconciled))
            self.evidence.compact_summary.update(
                scenario_completed=self.evidence.extra_summary["scenario_completed"],
                cancel_unsettled_observed=pending)
            if not self.evidence.extra_summary["scenario_completed"]:
                self.evidence.actor_failure = self.evidence.actor_failure or "loopback_scenario_incomplete"
        snapshot["durable_economic_acknowledgement"] = acknowledged
        snapshot["loopback"] = {"health": json.loads(self.control.telemetry_snapshot_json()),
            "pending_fills": pending_fills,
            "shutdown_report": report,
            "durable_economic_acknowledgement": acknowledged,
            "economic_consumer": consumer}
        return json.dumps(snapshot)


def loopback_final_failure(health):
    """Require authoritative evidence only after the shared owner has stopped."""
    if health["loopback"]["shutdown_report"] is None:
        return "loopback_shutdown_report_unavailable"
    return None


def build_loopback(plan, evidence, seed):
    from backpack_account import _account_configuration, _account_node
    from nautilus_trader.adapters.backpack import (
        BackpackLoopbackExecutionAuthority, BackpackLoopbackExecutionClientConfig,
        BackpackLoopbackExecutionClientFactory)

    evidence.compact_summary = {"mode": "loopback-execution", "scenario_completed": False,
        "execution_settled": False, "flat_verified": False, "exposure_unknown": True,
        "pending_fill_count": None, "native_shutdown_report": None, "dirty_shutdown": None,
        "cancel_unsettled_observed": False, "cancel202_observed": False,
        "durable_economic_acknowledgement": False,
        "native_inflight_checks_enabled": False, "continuous_reconciliation": False}
    from nautilus_trader.live import LiveExecutionEngineConfig

    public, account, data_factory = _account_configuration(plan, seed)
    authority = plan.authority.document()
    validity = authority.pop("valid_for_ms")
    # This one local expiry never extends on quote, reconnect or account refresh.
    authority["expires_at_ms"] = time.time_ns() // 1_000_000 + validity
    evidence.extra_summary["authority_expires_at_ms"] = authority["expires_at_ms"]
    restricted = BackpackLoopbackExecutionClientConfig(account, public,
        authority=BackpackLoopbackExecutionAuthority(**authority),
        mutation_budget_ms=plan.mutation_budget_ms, receive_window_ms=plan.receive_window_ms,
        **({"economic_state_directory": str(plan.economic_state_directory)}
           if plan.durable_economics else {}))
    factory = BackpackLoopbackExecutionClientFactory()
    node = _account_node(plan, evidence, public, restricted, data_factory, factory,
                         name="BACKPACK-LOOPBACK", exec_config=LiveExecutionEngineConfig(
                             reconciliation=False, inflight_check_interval_ms=0,
                             open_check_interval_secs=None, position_check_interval_secs=None))
    control = restricted.control
    strategy = _strategy(plan, evidence, control)
    node.add_strategy(strategy)
    account_poll = evidence.poll
    def poll(owner, writer):
        account_poll(owner, writer)
        if writer.limit_reached or writer.actor_failure:
            return
        telemetry = LoopbackTelemetry(public, account, control, evidence)
        health = json.loads(telemetry.telemetry_snapshot_json())
        if (plan.durable_economics and health["account"]["rest_snapshot_observed"]
                and health["account"]["transport_connected"]):
            try:
                consumer = json.loads(control.persist_economics())
            except RuntimeError:
                writer.actor_failure = "loopback_economic_checkpoint_failed"
                writer.handle.stop()
                return
            if consumer != writer.extra_summary.get("economic_consumer"):
                writer.record("native_economic_checkpoint", consumer)
            writer.extra_summary["economic_consumer"] = consumer
            health = json.loads(telemetry.telemetry_snapshot_json())
        strategy.advance(health)
        if plan.durable_economics and strategy.session is not None:
            try:
                terminal = json.loads(control.reconcile_terminal_evidence(strategy.session))
            except RuntimeError:
                # A disconnected/replaced session carries no current terminal proof.
                terminal = None
            if terminal != writer.extra_summary.get("terminal_evidence"):
                writer.record("native_terminal_evidence", {"evidence": terminal})
            writer.extra_summary["terminal_evidence"] = terminal
    evidence.poll = poll
    evidence.final_failure_check = loopback_final_failure
    previous_failure = evidence.failure_check
    evidence.failure_check = lambda health: previous_failure(health) or (
        None if evidence.extra_summary["scenario_steps_observed"] else "loopback_scenario_incomplete")
    evidence.extra_summary.update(authority_expires_at_ms=authority["expires_at_ms"],
        scenario_completed=False, scenario_steps_observed=False,
        cancel_requested=False, cancel_unsettled_observed=False, cancel202_observed=False, execution_settled=False,
        local_synthetic_mutations=True, production_writes_supported=False,
        native_inflight_checks_enabled=False, continuous_reconciliation=False,
        durable_economic_acknowledgement=False, flat_verified=False,
        durable_economics_enabled=plan.durable_economics, recovery_only=plan.recovery_only,
        economic_consumer=None,
        shutdown_semantics="native owner report is authoritative; scenario completion is not clean settlement",
        cancel_observation_semantics="native unsettled count combines Unknown/Pending/ResponseObserved; does not prove 202 receipt")
    return node, LoopbackTelemetry(public, account, control, evidence), {
        "public": json.loads(data_factory.capabilities_json()),
        "execution": json.loads(factory.capabilities_json())}


async def run_loopback(plan, *, candidate=None):
    if not isinstance(plan, BackpackLoopbackPlan):
        raise BackpackConfigError("runtime requires the independently validated loopback execution plan")
    from backpack_public import run_native_observation
    seed = loopback_seed(plan)
    return await run_native_observation(plan, candidate=candidate,
        extra_sources=("backpack_account.py", "backpack_loopback_config.py", "backpack_loopback.py"),
        builder=lambda checked, evidence: build_loopback(checked, evidence, seed))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="Validate without credentials, native imports or output")
    parser.add_argument("--candidate-wheel", type=Path)
    parser.add_argument("--candidate-sha256")
    parser.add_argument("--native-provenance", type=Path)
    args = parser.parse_args(argv)
    try:
        plan = load_loopback_plan(args.config)
        if args.dry_run:
            print(json.dumps(plan.document(), indent=2))
            return 0
        candidate = (args.candidate_wheel, args.candidate_sha256, args.native_provenance)
        if not all(value is not None for value in candidate):
            raise BackpackConfigError("loopback execution CLI requires a source-bound candidate wheel, SHA256 and provenance")
        summary, path = asyncio.run(run_loopback(plan, candidate=candidate))
        print(json.dumps({"status": summary["status"], "failure": summary["failure"],
            "scenario_completed": summary.get("scenario_completed", False),
            "execution_settled": summary.get("execution_settled", False),
            "flat_verified": summary.get("flat_verified", False), "summary_path": str(path)}))
        return 0 if summary["status"] == "completed" else 1
    except (BackpackConfigError, OSError) as error:
        reason = str(error) if isinstance(error, BackpackConfigError) else "cannot read or publish session files"
        print(f"Backpack loopback configuration refused: {reason}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Loopback interrupted; inspect native pending fills, dirty shutdown and actual exposure", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

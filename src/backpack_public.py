"""Bounded public observation through the installed native Backpack LiveNode."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
from importlib import metadata
import json
from pathlib import Path
import subprocess
import sys
import time
import uuid

from backpack_config import BackpackConfigError, BackpackSessionPlan

APP_ROOT = Path(__file__).resolve().parents[1]
POLL_SECS = 0.05
SHUTDOWN_SECS = 5


def encoded(document):
    return (json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=True) + "\n").encode()


def configuration_hash(plan):
    return hashlib.sha256(encoded(plan.document())).hexdigest()


def _file_hash(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def application_identity(*, extra_sources=()):
    """Record source identity without reading environment or arbitrary report files."""
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=APP_ROOT,
                                         stderr=subprocess.DEVNULL, timeout=5).decode().strip()
        dirty = bool(subprocess.check_output(
            ["git", "status", "--porcelain", "--", "src", "tests", "config", "pyproject.toml"],
            cwd=APP_ROOT, stderr=subprocess.DEVNULL, timeout=5))
    except (OSError, subprocess.SubprocessError):
        return {"commit": None, "dirty": None, "identity_verified": False}
    content = hashlib.sha256()
    for name in ("backpack_config.py", "backpack_probe.py", "backpack_public.py", *extra_sources):
        content.update(name.encode() + b"\0")
        content.update(bytes.fromhex(_file_hash(APP_ROOT / "src" / name)))
    return {"commit": commit, "dirty": dirty, "source_content_sha256": content.hexdigest(),
            "identity_verified": False}


def native_identity(candidate=None):
    """A local import is unverified until an explicit source-bound candidate is checked."""
    import nautilus_trader._libnautilus as native
    result = {"status": "unverified_local_development", "source_binding_verified": False,
              "distribution_version": metadata.version("nautilus-trader"),
              "native_module_sha256": _file_hash(native.__file__), "wheel_sha256": None,
              "native_commit": None, "backpack_stub_sha256": None}
    if candidate is not None:
        sys.path.insert(0, str(APP_ROOT / "scripts"))
        try:
            from verify_native_install import verify
            record = verify(candidate[0], candidate[1], provenance=candidate[2],
                            require_source_binding=True, require_ondo=False,
                            additional_adapters=("backpack",))
        finally:
            sys.path.remove(str(APP_ROOT / "scripts"))
        result.update(status="verified_candidate", source_binding_verified=True,
                      wheel_sha256=record["wheel"]["sha256"],
                      backpack_stub_sha256=record["installed"]["adapter_stub_sha256"][
                          "nautilus_trader/adapters/backpack/__init__.pyi"])
        declared = record["native_provenance"].get("declared_native") or {}
        commit = declared.get("commit") if isinstance(declared, dict) else None
        if isinstance(commit, str) and len(commit) == 40 and all(c in "0123456789abcdef" for c in commit):
            result["native_commit"] = commit
    return result


class PublicEvidence:
    """Bound encoded public events and aggregate counts without retaining raw frames."""

    def __init__(self, plan):
        self.plan = plan
        self.lines = []
        self.bytes = 0
        self.reserve = min(65_536, max(512, plan.max_report_bytes // 2))
        self.counts = {}
        self.dropped = 0
        self.limit_reached = False
        self.actor_started = False
        self.actor_stopped = False
        self.actor_failure = None
        self.handle = None

    def record(self, kind, fields):
        self.counts[kind] = self.counts.get(kind, 0) + 1
        line = encoded({"kind": kind, **fields})
        if (len(self.lines) >= self.plan.max_report_events
                or self.bytes + len(line) > self.plan.max_report_bytes - self.reserve):
            self.dropped += 1
            self.limit_reached = True
            if self.handle is not None:
                self.handle.stop()
            return
        self.lines.append(line)
        self.bytes += len(line)

    def publish(self, summary):
        payload = encoded(summary)
        if len(payload) + self.bytes > self.plan.max_report_bytes:
            # Even the smallest accepted configuration can bound final failure evidence.
            payload = encoded({"schema_version": 1, "run_id": summary["run_id"],
                "configuration_sha256": summary["configuration_sha256"],
                "status": summary["status"], "failure": summary["failure"],
                "summary_truncated": True, "execution_ready": False,
                "remote_writes_allowed": False, "event_count": len(self.lines),
                "dropped_events": self.dropped})
            summary = json.loads(payload)
        if len(payload) + self.bytes > self.plan.max_report_bytes:
            raise BackpackConfigError("report limit cannot contain minimal run evidence")
        directory = self.plan.output_dir / summary["run_id"]
        directory.mkdir(parents=True, exist_ok=False)
        with (directory / "events.jsonl").open("xb") as stream:
            for line in self.lines:
                stream.write(line)
        with (directory / "summary.json").open("xb") as stream:
            stream.write(payload)
        return summary, directory / "summary.json"


def _event_fields(event):
    return {"instrument_id": str(event.instrument_id),
            "ts_event_ns": str(event.ts_event), "ts_received_ns": str(event.ts_init)}


def _observer(plan, evidence):
    # Native imports are deliberately confined to an authorized runtime.
    from nautilus_trader.common import DataActor, DataActorConfig
    from nautilus_trader.model import BookType, ClientId, InstrumentId

    def guarded(callback):
        def observe(self, *args):
            try:
                return callback(self, *args)
            except Exception:
                evidence.actor_failure = "public_observer_failed"
                evidence.handle.stop()
        return observe

    class PublicObserver(DataActor):
        def __init__(self):
            super().__init__(DataActorConfig())
            self.ids = [InstrumentId.from_str(f"{symbol}.BACKPACK") for symbol in plan.symbols]
            self.client = ClientId.from_str("BACKPACK")

        @guarded
        def on_start(self):
            evidence.actor_started = True
            for instrument_id in self.ids:
                instrument = self.cache.instrument(instrument_id)
                if instrument is None:
                    evidence.actor_failure = "instrument_unavailable"
                    evidence.handle.stop()
                    return
                evidence.record("instrument", {"instrument_id": str(instrument_id),
                    "price_increment": str(instrument.price_increment),
                    "size_increment": str(instrument.size_increment)})
                self.subscribe_instrument(instrument_id, client_id=self.client)
                self.subscribe_quotes(instrument_id, client_id=self.client)
                self.subscribe_trades(instrument_id, client_id=self.client)
                self.subscribe_mark_prices(instrument_id, client_id=self.client)
                self.subscribe_book_deltas(instrument_id, BookType.L2_MBP,
                                           client_id=self.client, managed=False)

        @guarded
        def on_stop(self):
            evidence.actor_stopped = True
            for instrument_id in self.ids:
                self.unsubscribe_quotes(instrument_id, client_id=self.client)
                self.unsubscribe_trades(instrument_id, client_id=self.client)
                self.unsubscribe_mark_prices(instrument_id, client_id=self.client)
                self.unsubscribe_book_deltas(instrument_id, client_id=self.client)
                self.unsubscribe_instrument(instrument_id, client_id=self.client)

        @guarded
        def on_instrument(self, instrument):
            evidence.record("instrument", {"instrument_id": str(instrument.id),
                "price_increment": str(instrument.price_increment),
                "size_increment": str(instrument.size_increment)})

        @guarded
        def on_quote(self, quote):
            evidence.record("quote", {**_event_fields(quote),
                "bid_price": str(quote.bid_price), "ask_price": str(quote.ask_price),
                "bid_size": str(quote.bid_size), "ask_size": str(quote.ask_size)})

        @guarded
        def on_trade(self, trade):
            evidence.record("trade", {**_event_fields(trade), "price": str(trade.price),
                "size": str(trade.size), "trade_id": str(trade.trade_id),
                "aggressor_side": str(trade.aggressor_side)})

        @guarded
        def on_mark_price(self, mark):
            evidence.record("mark", {**_event_fields(mark), "price": str(mark.value)})

        @guarded
        def on_book_deltas(self, batch):
            # Native deltas are exact validated records. Only bounded coverage is claimed.
            levels = [{"action": str(delta.action), "side": str(delta.order.side),
                       "price": str(delta.order.price), "size": str(delta.order.size)}
                      for delta in batch.deltas]
            evidence.record("book_deltas", {**_event_fields(batch),
                "sequence": str(batch.sequence), "flags": batch.flags, "deltas": levels,
                "complete_book_claimed": False})

    return PublicObserver()


def _build(plan, evidence):
    from nautilus_trader.adapters.backpack import (
        BackpackDataClientConfig, BackpackDataClientFactory, BackpackInstrumentEconomics)
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import TraderId

    economics = {symbol: BackpackInstrumentEconomics(**values.document())
                 for symbol, values in plan.economics}
    overrides = ({"base_url_http": plan.http_origin, "base_url_ws": plan.ws_origin}
                 if plan.environment == "loopback" else {})
    config = BackpackDataClientConfig(list(plan.symbols), economics,
        http_timeout_secs=plan.request_timeout_secs,
        ws_connect_timeout_secs=min(10, plan.request_timeout_secs),
        shutdown_timeout_secs=SHUTDOWN_SECS,
        quote_stale_after_ms=plan.stale_after_ms, **overrides)
    factory = BackpackDataClientFactory()
    node = (LiveNode.builder("BACKPACK-PUBLIC", TraderId.from_str("BACKPACK-PUBLIC"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.OFF, fileout_level=LogLevel.OFF,
                                   bypass_logging=True, print_config=False))
        .with_timeout_connection(plan.request_timeout_secs)
        .with_timeout_reconciliation(0).with_timeout_portfolio(0)
        .with_timeout_disconnection_secs(SHUTDOWN_SECS)
        .with_delay_post_stop_secs(0).with_delay_shutdown_secs(0)
        .with_load_state(False).with_save_state(False)
        .add_data_client(None, factory, config).build())
    evidence.handle = node.handle()
    node.add_actor(_observer(plan, evidence))
    return node, config, json.loads(factory.capabilities_json())


async def run_public(plan: BackpackSessionPlan, *, candidate=None):
    """Run one native public session; cancellation publishes evidence and propagates."""
    if plan.mode != "public" or plan.account is not None or plan.environment == "offline":
        raise BackpackConfigError("only public runtime is implemented; use --dry-run for other modes")
    started = datetime.now(UTC).isoformat()
    began = time.monotonic()
    run_began = None
    run_id = uuid.uuid4().hex
    evidence = PublicEvidence(plan)
    node = config = task = None
    identity = {"status": "unavailable", "source_binding_verified": False}
    capabilities = None
    failure = None
    stop_reason = "not_started"
    cancelled = False
    runtime_started = False
    shutdown_complete = False
    last_health = None
    final_health = None
    try:
        identity = native_identity(candidate)
        node, config, capabilities = _build(plan, evidence)
        run_began = time.monotonic()
        deadline = run_began + plan.duration_secs
        task = asyncio.ensure_future(node.run_async())
        runtime_started = True
        stop_reason = "duration_limit"
        while not task.done() and time.monotonic() < deadline and not evidence.limit_reached:
            health = json.loads(config.telemetry_snapshot_json())
            if health != last_health:
                evidence.record("native_health", {"observed_at_ns": str(time.time_ns()), "health": health})
                last_health = health
            await asyncio.sleep(min(POLL_SECS, max(0, deadline - time.monotonic())))
        if evidence.limit_reached:
            stop_reason = "report_limit"
        elif task.done():
            stop_reason = "native_run_returned"
        evidence.handle.stop()
        await asyncio.wait_for(asyncio.shield(task), SHUTDOWN_SECS * 2 + 1)
        shutdown_complete = True
    except asyncio.CancelledError:
        cancelled = True
        failure = "cancelled"
        stop_reason = "cancelled"
    except Exception:
        # No exception strings, request bodies, native payloads or traceback enter evidence.
        failure = "native_runtime_failed" if runtime_started else "startup_failed"
        stop_reason = "failed"
    finally:
        if evidence.handle is not None:
            evidence.handle.stop()
            evidence.handle.stop()  # Repeated stop uses the supported idempotent control handle.
        if task is not None and not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(asyncio.shield(task), SHUTDOWN_SECS * 2 + 1)
                shutdown_complete = True
            except asyncio.CancelledError:
                shutdown_complete = task.done()
            except Exception:
                failure = "shutdown_failed"
        if task is not None and task.done() and not task.cancelled():
            try:
                task.result()
                shutdown_complete = True
            except Exception:
                failure = failure or "native_runtime_failed"
        if config is not None:
            try:
                final_health = json.loads(config.telemetry_snapshot_json())
            except Exception:
                failure = "native_telemetry_failed"
        if node is not None:
            try:
                node.dispose()
            except Exception:
                failure = "dispose_failed"
        failure = failure or evidence.actor_failure
        if runtime_started and not evidence.actor_started:
            failure = failure or "public_observer_not_started"
        summary = {"schema_version": 1, "run_id": run_id, "venue": "BACKPACK", "mode": "public",
            "started_at": started, "finished_at": datetime.now(UTC).isoformat(),
            "elapsed_ms": int((time.monotonic() - began) * 1000),
            "preparation_ms": None if run_began is None else int((run_began - began) * 1000),
            "runtime_elapsed_ms": None if run_began is None else int((time.monotonic() - run_began) * 1000),
            "configuration_sha256": configuration_hash(plan), "configuration": plan.document(),
            "application": application_identity(), "native": identity, "capabilities": capabilities,
            "status": "failed" if failure else "completed", "failure": failure,
            "stop_reason": stop_reason, "runtime_started": runtime_started,
            "actor_started": evidence.actor_started, "actor_stopped": evidence.actor_stopped,
            "shutdown_complete": shutdown_complete, "native_health": final_health,
            "event_count": len(evidence.lines), "event_bytes": evidence.bytes,
            "event_counts": evidence.counts, "dropped_events": evidence.dropped,
            "report_limit_reached": evidence.limit_reached,
            "account_identity_verified": False, "economics_account_verified": False,
            "execution_ready": False, "remote_writes_allowed": False,
            "private_client_registered": False, "durable_state_opened": False,
            "complete_book_claimed": False, "funding_fraction_unit_verified": False}
        result = evidence.publish(summary)
    if cancelled:
        raise asyncio.CancelledError
    return result

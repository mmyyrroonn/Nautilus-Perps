"""Installed native consumer receipts and same-journal engine recovery."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backpack_loopback_config import parse_loopback_plan
from backpack_loopback import run_loopback
from backpack_public import PublicEvidence
from test_backpack_loopback_native import (
    OrderPeer, event_records, plan_for, require_loopback_native,
)
from test_backpack_public_native import SYMBOL


class DurablePeer(OrderPeer):
    def __init__(self, *, refresh_until_post=True):
        super().__init__()
        self.refresh_until_post = refresh_until_post
        self.admission_quote_times_ns = []
        self.admission_quotes_stopped = asyncio.Event()
        self.admission_directory = None

    async def send_frames(self, ws, connection):
        try:
            await super().send_frames(ws, connection)
            sequence = connection
            answered = 0
            while self.refresh_until_post and not self.post_received.is_set():
                try:
                    await asyncio.wait_for(self.post_received.wait(), 0.025)
                    break
                except TimeoutError:
                    pass
                if self.post_received.is_set():
                    break
                if self.admission_directory is None:
                    continue
                try:
                    requested = int((self.admission_directory / "request").read_text())
                except (OSError, ValueError):
                    continue
                if requested <= answered:
                    continue
                # Answer one explicit pre-admission request, then keep this quote stable.
                # Restricted native admission binds exact quote timestamps, so a stream
                # that keeps changing through signing would itself invalidate admission.
                sequence += 1
                observed = time.time_ns()
                micros = observed // 1000
                await ws.send(json.dumps({"stream": f"bookTicker.{SYMBOL}", "data": {
                    "e": "bookTicker", "s": SYMBOL, "E": micros, "T": micros - 1000,
                    "u": sequence, "b": "100.0", "B": "1.00000",
                    "a": "101.0", "A": "2.00000"}}))
                self.admission_quote_times_ns.append(observed)
                (self.admission_directory / "quote.json").write_text(json.dumps({
                    "request": requested, "quote_event_ns": str((micros - 1000) * 1000)}))
                answered = requested
        finally:
            self.admission_quotes_stopped.set()

    async def send_private(self, ws, connection):
        if connection == 1:
            await super().send_private(ws, connection)
            return
        await self.ready.wait()
        await asyncio.sleep(0.25)
        for frame in self.fill_frames:
            await ws.send(json.dumps(frame))
            await asyncio.sleep(0.1)


def release_stale_probe(monkeypatch, peer):
    original = PublicEvidence.record
    def record(evidence, kind, fields):
        original(evidence, kind, fields)
        if kind == "order_denied":
            peer.probe_denied.set()
    monkeypatch.setattr(PublicEvidence, "record", record)


def economic_observation(path):
    observations = [row for row in event_records(path)
                    if row["kind"] == "engine_account_observation" and row["positions"]]
    assert observations
    return observations[-1]


def safe_run_diagnostics(summary, path):
    keys = ("status", "failure", "stop_reason", "elapsed_ms", "preparation_ms",
        "runtime_elapsed_ms", "runtime_started", "actor_started", "actor_stopped",
        "shutdown_complete", "report_limit_reached", "summary_truncated", "event_count",
        "event_counts", "dropped_events", "native_health_before_stop", "native_health",
        "scenario_steps_observed", "scenario_completed", "economic_consumer")
    kinds = {"native_health", "engine_account_observation", "instrument", "quote",
        "synthetic_account_assertion", "scenario_submit", "scenario_stale_probe",
        "scenario_owned_cancel", "order_submitted", "order_accepted", "order_denied",
        "order_rejected", "order_filled", "order_canceled", "native_economic_checkpoint",
        "native_terminal_evidence"}
    events = []
    try:
        lines = path.with_name("events.jsonl").read_text().splitlines()
    except OSError:
        lines = []
    for line in lines:
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue  # A live child can still be writing its final record.
        if row.get("kind") in kinds:
            events.append(row)
    return {"summary": {key: summary.get(key) for key in keys}, "events": events}


def peer_diagnostics(peer):
    # No request headers, signed commands, credentials or child stdout/stderr.
    return {"post_count": len(peer.posts), "delete_count": len(peer.deletes),
        "responses": peer.responses, "requests": [(method, path) for method, path, _ in peer.requests],
        "active_connections": peer.active, "private_connections": peer.private_connections,
        "private_subscription_observed": peer.private_ready.is_set(),
        "public_fixture_sent": peer.ready.is_set(), "fill_fixture_sent": peer.fixture_sent,
        "post_at_ns": peer.post_at_ns, "metadata_started_at_ns": peer.metadata_started_at_ns,
        "admission_quote_times_ns": peer.admission_quote_times_ns,
        "admission_quotes_stopped": peer.admission_quotes_stopped.is_set()}


def diagnostic_json(summary, path, peer):
    return json.dumps({"run": safe_run_diagnostics(summary, path), "peer": peer_diagnostics(peer)},
                      sort_keys=True, indent=2)


def admission_gated_factory(original_strategy, directory):
    def factory(plan, *args):
        strategy = original_strategy(plan, *args)
        original_advance = type(strategy).advance
        requested = 0
        def advance(self, health):
            nonlocal requested
            if plan.recovery_only or self.primary is not None:
                return original_advance(self, health)
            if not self.started or not health["account"]["transport_connected"]:
                return
            if not requested:
                requested = 1
                (directory / "request").write_text(str(requested))
                return
            try:
                quote = json.loads((directory / "quote.json").read_text())
            except (OSError, json.JSONDecodeError):
                return
            if (quote["request"] != requested
                    or health["public"].get("quote_event_ns", {}).get(plan.scenario.symbol)
                       != quote["quote_event_ns"]):
                return
            if not health["public"]["quotes_fresh"].get(plan.scenario.symbol, False):
                requested += 1
                (directory / "request").write_text(str(requested))
                return
            # The peer is now quiet, and native telemetry has processed its final quote.
            # The original Strategy still requires the actual cache to hold that quote.
            original_advance(self, health)
        type(strategy).advance = advance
        return strategy
    return factory


def configure_admission(monkeypatch, peer, directory):
    import backpack_loopback
    directory.mkdir()
    peer.admission_directory = directory
    monkeypatch.setattr(backpack_loopback, "_strategy",
        admission_gated_factory(backpack_loopback._strategy, directory))


@pytest.mark.parametrize("refresh_until_post", [False, True])
def test_delayed_admission_needs_live_quotes_without_refreshing_stale_probe(
        tmp_path, monkeypatch, refresh_until_post):
    require_loopback_native()
    import backpack_loopback

    async def scenario():
        async with DurablePeer(refresh_until_post=refresh_until_post) as peer:
            release_stale_probe(monkeypatch, peer)
            plan = plan_for(peer, tmp_path, monkeypatch)
            plan = replace(plan, durable_economics=True,
                           session=replace(plan.session, duration_secs=4))
            if refresh_until_post:
                configure_admission(monkeypatch, peer, tmp_path / "fixture-admission")
            admission_ready = asyncio.Event()
            original_strategy = backpack_loopback._strategy

            def delayed_strategy(*args):
                strategy = original_strategy(*args)
                original_advance = type(strategy).advance
                def advance(self, health):
                    # Defer only initial admission. Native health, cached quote times and
                    # the real 500 ms stale gate remain unchanged throughout this delay.
                    if self.primary is None and not admission_ready.is_set():
                        return
                    original_advance(self, health)
                monkeypatch.setattr(type(strategy), "advance", advance)
                return strategy

            monkeypatch.setattr(backpack_loopback, "_strategy", delayed_strategy)

            async def release_admission():
                await peer.ready.wait()
                await asyncio.sleep(0.75)
                admission_ready.set()

            release = asyncio.create_task(release_admission())
            try:
                summary, path = await asyncio.wait_for(run_loopback(plan), 23)
            finally:
                release.cancel()
                await asyncio.gather(release, return_exceptions=True)
            assert admission_ready.is_set() and summary["actor_started"], diagnostic_json(summary, path, peer)
            if not refresh_until_post:
                assert summary["failure"] == "loopback_scenario_incomplete", diagnostic_json(summary, path, peer)
                assert not peer.posts and not peer.fill_frames
                return

            assert summary["status"] == "completed", diagnostic_json(summary, path, peer)
            assert len(peer.posts) == 1 and len(peer.deletes) == 1
            assert peer.admission_quotes_stopped.is_set() and peer.admission_quote_times_ns
            assert max(peer.admission_quote_times_ns) <= peer.post_at_ns
            rows = event_records(path)
            probe = next(row for row in rows if row["kind"] == "scenario_stale_probe")
            denial = next(row for row in rows if row["kind"] == "order_denied")
            assert probe["public_quotes_fresh"] is False
            assert denial["reason"] == "SUBMIT_FAILED: Backpack execution refused: Readiness"
            assert summary["economic_consumer"]["durable_receipts"] == 1
            assert summary["economic_consumer"]["pending_fills"] == 0
    asyncio.run(scenario())


def test_durable_receipt_restores_actual_order_position_and_rebate_once(tmp_path, monkeypatch):
    require_loopback_native()
    async def scenario():
        async with DurablePeer() as peer:
            configure_admission(monkeypatch, peer, tmp_path / "fixture-admission")
            release_stale_probe(monkeypatch, peer)
            plan = replace(plan_for(peer, tmp_path, monkeypatch), durable_economics=True)
            first, first_path = await asyncio.wait_for(run_loopback(plan), 23)
            assert first["status"] == "completed", diagnostic_json(first, first_path, peer)
            assert first["durable_economic_acknowledgement"]
            assert first["economic_consumer"]["durable_receipts"] == 1
            assert first["economic_consumer"]["pending_fills"] == 0
            assert first["native_health"]["loopback"]["shutdown_report"]["dirty"]
            assert not first["flat_verified"]
            original = economic_observation(first_path)
            recovery = replace(plan, recovery_only=True,
                               session=replace(plan.session, duration_secs=3))
            second, second_path = await asyncio.wait_for(run_loopback(recovery), 16)
            assert second["status"] == "completed", diagnostic_json(second, second_path, peer)
            assert second["durable_economic_acknowledgement"]
            assert second["economic_consumer"]["durable_receipts"] == 1
            assert second["economic_consumer"]["pending_fills"] == 0
            restored = economic_observation(second_path)
            assert restored["orders"] == original["orders"]
            assert restored["positions"] == original["positions"]
            assert restored["orders"][0]["trade_ids"] == ["701"]
            assert restored["orders"][0]["commissions"] == ["-0.00000100 USDC"]
            assert len(peer.posts) == 1 and len(peer.deletes) == 1
            assert peer.active == 0
            assert not any(row["kind"] == "scenario_submit" for row in event_records(second_path))
            assert second["native_health"]["loopback"]["shutdown_report"]["dirty"]
            assert not second["flat_verified"]
    asyncio.run(scenario())


def test_corrupt_economic_checkpoint_refuses_startup_without_another_post(tmp_path, monkeypatch):
    require_loopback_native()
    async def scenario():
        async with DurablePeer() as peer:
            configure_admission(monkeypatch, peer, tmp_path / "fixture-admission")
            release_stale_probe(monkeypatch, peer)
            plan = replace(plan_for(peer, tmp_path, monkeypatch), durable_economics=True)
            first, first_path = await asyncio.wait_for(run_loopback(plan), 23)
            assert first["status"] == "completed", diagnostic_json(first, first_path, peer)
            checkpoint = plan.economic_state_directory / "economics.json"
            document = json.loads(checkpoint.read_text())
            document["checksum"] = "invalid-checksum"
            checkpoint.write_text(json.dumps(document))
            recovery = replace(plan, recovery_only=True)
            failed, _ = await asyncio.wait_for(run_loopback(recovery), 10)
            assert failed["status"] == "failed" and failed["failure"] == "startup_failed"
            assert not failed["runtime_started"]
            assert len(peer.posts) == 1 and len(peer.deletes) == 1 and peer.active == 0
    asyncio.run(scenario())


def test_terminated_process_releases_journal_and_recovers_real_engine_once(tmp_path, monkeypatch):
    require_loopback_native()
    from test_backpack_account_native import SEED
    from test_backpack_loopback import loopback_document

    async def scenario():
        async with DurablePeer() as peer:
            admission_directory = tmp_path / "fixture-admission"
            admission_directory.mkdir()
            peer.admission_directory = admission_directory
            document = loopback_document()
            document.update(durable_economics=True)
            document["session"].update(base_url_http=peer.http_url, base_url_ws=peer.ws_url,
                                       duration_secs=10, request_timeout_secs=2)
            document["scenario"]["stale_probe"] = False
            document["mutation_budget_ms"] = 1000
            plan = parse_loopback_plan(document, tmp_path)
            config_path = tmp_path / "crash-input.json"
            config_path.write_text(json.dumps(document))
            source = str(Path(__file__).resolve().parents[1] / "src")
            code = """import asyncio,json,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
sys.path.insert(0,str(Path(sys.argv[1]).parent / "tests"))

from backpack_loopback_config import parse_loopback_plan
from backpack_loopback import run_loopback
from test_backpack_durable_native import admission_gated_factory
import backpack_loopback
path=Path(sys.argv[2])
backpack_loopback._strategy=admission_gated_factory(backpack_loopback._strategy,path.parent / "fixture-admission")
asyncio.run(run_loopback(parse_loopback_plan(json.loads(path.read_text()),path.parent)))
"""
            environment = {key: value for key, value in os.environ.items()
                           if key.upper() in {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PATH"}}
            environment.update(PYTHONUTF8="1")
            environment[plan.account.credential_env] = SEED
            monkeypatch.setenv(plan.account.credential_env, SEED)
            peer.probe_denied.set()
            process = await asyncio.create_subprocess_exec(
                sys.executable, "-c", code, source, str(config_path),
                env=environment, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                deadline = time.monotonic() + 15
                stored = None
                checkpoint = plan.economic_state_directory / "economics.json"
                while time.monotonic() < deadline and process.returncode is None:
                    if checkpoint.is_file():
                        stored = json.loads(checkpoint.read_text())
                        if len(stored["state"]["fills"]) == 1:
                            break
                    await asyncio.sleep(0.02)
                if not stored or len(stored["state"]["fills"]) != 1 or process.returncode is not None:
                    diagnostics = {
                        "durable_receipts": len(stored["state"]["fills"]) if stored else None,
                        "peer": peer_diagnostics(peer), "process_returncode": process.returncode,
                        "child_reports": [],
                    }
                    for path in plan.output_dir.glob("*/summary.json"):
                        try:
                            summary = json.loads(path.read_text())
                        except (OSError, json.JSONDecodeError):
                            continue
                        diagnostics["child_reports"].append(safe_run_diagnostics(summary, path))
                    assert False, json.dumps(diagnostics, sort_keys=True, indent=2)
                process.kill()
                await asyncio.wait_for(process.wait(), 5)
            finally:
                if process.returncode is None:
                    process.kill()
                await asyncio.wait_for(process.communicate(), 5)
            # TerminateProcess/SIGKILL bypasses Python and native owner cleanup.
            deadline = time.monotonic() + 3
            while peer.active and time.monotonic() < deadline:
                await asyncio.sleep(0.02)
            assert peer.active == 0
            recovery = replace(plan, recovery_only=True,
                               session=replace(plan.session, duration_secs=3))
            resumed, path = await asyncio.wait_for(run_loopback(recovery), 16)
            assert resumed["status"] == "completed", diagnostic_json(resumed, path, peer)
            assert resumed["economic_consumer"]["durable_receipts"] == 1
            assert resumed["economic_consumer"]["pending_fills"] == 0
            observation = economic_observation(path)
            assert observation["orders"][0]["trade_ids"] == ["701"]
            assert observation["orders"][0]["commissions"] == ["-0.00000100 USDC"]
            assert observation["positions"][0]["quantity"] == "0.00001"
            assert len(peer.posts) == 1 and peer.active == 0
            assert resumed["native_health"]["loopback"]["shutdown_report"]["dirty"]
    asyncio.run(scenario())

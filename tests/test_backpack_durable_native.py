"""Installed native consumer receipts and same-journal engine recovery."""
from __future__ import annotations

import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from backpack_loopback_config import parse_loopback_plan
from backpack_loopback import run_loopback
from backpack_public import PublicEvidence
from test_backpack_loopback_native import (
    OrderPeer, event_records, plan_for, require_loopback_native,
)


class DurablePeer(OrderPeer):
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


def test_durable_receipt_restores_actual_order_position_and_rebate_once(tmp_path, monkeypatch):
    require_loopback_native()
    async def scenario():
        async with DurablePeer() as peer:
            release_stale_probe(monkeypatch, peer)
            plan = replace(plan_for(peer, tmp_path, monkeypatch), durable_economics=True)
            first, first_path = await asyncio.wait_for(run_loopback(plan), 23)
            assert first["status"] == "completed", first["failure"]
            assert first["durable_economic_acknowledgement"]
            assert first["economic_consumer"]["durable_receipts"] == 1
            assert first["economic_consumer"]["pending_fills"] == 0
            assert first["native_health"]["loopback"]["shutdown_report"]["dirty"]
            assert not first["flat_verified"]
            original = economic_observation(first_path)
            recovery = replace(plan, recovery_only=True,
                               session=replace(plan.session, duration_secs=3))
            second, second_path = await asyncio.wait_for(run_loopback(recovery), 16)
            assert second["status"] == "completed", second["failure"]
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
            release_stale_probe(monkeypatch, peer)
            plan = replace(plan_for(peer, tmp_path, monkeypatch), durable_economics=True)
            first, _ = await asyncio.wait_for(run_loopback(plan), 23)
            assert first["status"] == "completed", first["failure"]
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

from backpack_loopback_config import parse_loopback_plan
from backpack_loopback import run_loopback
path=Path(sys.argv[2])
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
                        "post_count": len(peer.posts), "process_returncode": process.returncode,
                        "child_reports": [], "child_event_kinds": [],
                    }
                    for path in plan.output_dir.glob("*/summary.json"):
                        try:
                            summary = json.loads(path.read_text())
                        except (OSError, json.JSONDecodeError):
                            continue
                        diagnostics["child_reports"].append({
                            "status": summary.get("status"), "failure": summary.get("failure")})
                    for path in plan.output_dir.glob("*/events.jsonl"):
                        try:
                            lines = path.read_text().splitlines()
                        except OSError:
                            continue
                        for line in lines:
                            try:
                                row = json.loads(line)
                            except json.JSONDecodeError:
                                continue  # A live child can still be writing its final line.
                            diagnostics["child_event_kinds"].append(row.get("kind"))
                    assert False, diagnostics
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
            assert resumed["status"] == "completed", resumed["failure"]
            assert resumed["economic_consumer"]["durable_receipts"] == 1
            assert resumed["economic_consumer"]["pending_fills"] == 0
            observation = economic_observation(path)
            assert observation["orders"][0]["trade_ids"] == ["701"]
            assert observation["orders"][0]["commissions"] == ["-0.00000100 USDC"]
            assert observation["positions"][0]["quantity"] == "0.00001"
            assert len(peer.posts) == 1 and peer.active == 0
            assert resumed["native_health"]["loopback"]["shutdown_report"]["dirty"]
    asyncio.run(scenario())

"""Slow authenticated history must not starve the strict public metadata deadline."""
import asyncio
import json
import time

from backpack_production_peer import ProductionPeer
from test_backpack_production_native import build_node


def test_metadata_refresh_is_independent_and_still_expires(tmp_path):
    async def scenario():
        async with ProductionPeer() as peer:
            node, strategy, control, plan, public = build_node(
                peer, tmp_path, [], account_max_age_ms=15000, market_max_age_ms=2000)
            task = asyncio.ensure_future(node.run_async())
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if strategy.started and json.loads(control.admission_status_json())["ready"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("initial native admission did not become ready")
                peer.history_gate = asyncio.Event()
                await asyncio.wait_for(peer.history_waiting.wait(), 5)
                await asyncio.sleep(2.4)
                health = json.loads(public.telemetry_snapshot_json())
                assert all(health["quotes_fresh"].values())
                admission = json.loads(control.admission_status_json())
                assert admission["ready"], admission
                assert not peer.posts and not peer.deletes

                # No clock rewrite or relaxed deadline may mask a failed metadata refresh.
                peer.metadata_gate = asyncio.Event()
                await asyncio.sleep(2.4)
                admission = json.loads(control.admission_status_json())
                assert not admission["ready"] and admission["global_ready"], admission
                assert all(not row["ready"] for row in admission["markets"].values())
                health = json.loads(public.telemetry_snapshot_json())
                assert all(health["quotes_fresh"].values())
                peer.metadata_gate.set()
                peer.history_gate.set()
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    if json.loads(control.admission_status_json())["ready"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("metadata refresh did not restore admission")
                assert not peer.posts and not peer.deletes and not peer.errors
            finally:
                if peer.metadata_gate is not None:
                    peer.metadata_gate.set()
                if peer.history_gate is not None:
                    peer.history_gate.set()
                node.handle().stop()
                await asyncio.wait_for(asyncio.shield(task), 12)
                node.dispose()
    asyncio.run(scenario())


def test_throttled_history_recovery_preserves_strict_market_freshness(tmp_path):
    async def scenario():
        async with ProductionPeer() as peer:
            node, strategy, control, plan, public = build_node(
                peer, tmp_path, [], account_max_age_ms=15000, market_max_age_ms=2000,
                recovery_interval_ms=5000)
            task = asyncio.ensure_future(node.run_async())
            try:
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    if strategy.started and json.loads(control.admission_status_json())["ready"]:
                        break
                    await asyncio.sleep(0.05)
                else:
                    raise AssertionError("initial native admission did not become ready")
                initial = len(peer.requests)
                deadline = time.monotonic() + 12
                while time.monotonic() < deadline:
                    admission = json.loads(control.admission_status_json())
                    assert admission["ready"], admission
                    await asyncio.sleep(0.1)
                paths = [path for _, path in peer.requests[initial:]]
                assert 2 <= paths.count("/wapi/v1/history/orders") <= 3
                assert 2 <= paths.count("/wapi/v1/history/fills") <= 3
                assert paths.count("/api/v1/markets") >= 10
                assert not peer.posts and not peer.deletes and not peer.errors
            finally:
                node.handle().stop()
                await asyncio.wait_for(asyncio.shield(task), 12)
                node.dispose()
    asyncio.run(scenario())

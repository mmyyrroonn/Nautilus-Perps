"""Account history must not amplify production economic checkpoint work."""
from __future__ import annotations

import asyncio
import json
import statistics
import time

from backpack_production_peer import micros
from test_backpack_quote_cache_native import (
    QuoteCachePeer,
    build_observer_node,
    eventually,
)


def test_production_checkpoint_stays_bounded_after_many_account_updates(tmp_path, request):
    async def scenario():
        from nautilus_trader.model import AccountId
        from opportunity_native_execution import NativeOrderGateway

        async with QuoteCachePeer(()) as peer:
            node, observer, state, probe, control, public = build_observer_node(peer, tmp_path)
            gateway = NativeOrderGateway(
                probe, state.plan.markets, lambda _: None, lambda _: None, lambda _: None,
                native_controls={"BACKPACK": control}, backpack_public_config=public, environ={},
            )
            cache, handle = node.cache, node.handle()
            task = asyncio.ensure_future(node.run_async())
            account_id = AccountId.from_str("BACKPACK-QUOTE-PEER")
            samples = []

            async def wait(predicate):
                return await eventually(
                    predicate, node_task=task, observer=observer, peer=peer, control=control,
                )

            def account():
                return cache.account(account_id)

            def measure(sent):
                timings = []
                for _ in range(20):
                    start = time.perf_counter_ns()
                    gateway.poll(time.time_ns())
                    timings.append((time.perf_counter_ns() - start) / 1_000_000)
                path = tmp_path / "economics" / "economics.json"
                checkpoint = json.loads(path.read_text())["state"]["account"]["Margin"]["base"]
                assert len(checkpoint["events"]) == 1
                assert path.stat().st_size < 20_000
                live = account()
                assert live.event_count >= sent + 1
                samples.append({
                    "injected_updates": sent,
                    "native_account_events": live.event_count,
                    "checkpoint_bytes": path.stat().st_size,
                    "gateway_poll_p50_ms": statistics.median(timings),
                    "gateway_poll_max_ms": max(timings),
                })

            try:
                await wait(lambda: observer.started and account() is not None
                    and json.loads(control.telemetry_snapshot_json())["rest_snapshot_observed"])
                gateway.poll(time.time_ns())
                measure(0)
                for sent in range(0, 5000, 50):
                    last = 0
                    for _ in range(50):
                        last = micros()
                        await peer.broadcast({"stream": "account.balanceUpdate", "data": {
                            "e": "balanceUpdate", "E": last, "T": last, "a": "USDC",
                            "A": "100", "L": "0", "S": "0",
                        }})
                    await wait(lambda: account().last_event.ts_event >= last * 1000
                        and account().event_count >= sent + 51)
                    if sent + 50 in (1000, 2500, 5000):
                        measure(sent + 50)
                assert not peer.posts and not peer.deletes and not peer.fills
                assert not [row for row in peer.requests if row[0] != "GET"]
                request.node.user_properties.append(("checkpoint_scaling", json.dumps(samples)))
            finally:
                gateway.close()
                handle.stop()
                try:
                    await asyncio.wait_for(asyncio.shield(task), 8)
                finally:
                    node.dispose()
            assert not peer.errors

    asyncio.run(scenario())

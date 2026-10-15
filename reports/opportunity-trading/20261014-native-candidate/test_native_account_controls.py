"""Installed native controls exercised against the owned signed protocol peer."""
import asyncio
import json
from pathlib import Path
import sys
import time

import pytest

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT / "tests"))
import test_backpack_production_native as scenarios
from backpack_production_peer import ProductionPeer


@pytest.mark.parametrize("external", [False, True])
def test_authenticated_snapshot_and_expiry_renewal_preserve_owned_scope(tmp_path, monkeypatch, external):
    snapshots = []
    controls = []
    original = scenarios.build_node

    def instrumented(peer, directory, steps):
        node, strategy, control, plan, public = original(peer, directory, steps)
        controls.append(control)
        advance = strategy.advance
        renewed = False

        def observe():
            nonlocal renewed
            advance()
            try:
                snapshot = json.loads(control.account_snapshot_json())
            except RuntimeError:
                return
            if not renewed:
                expiry = plan.authority.expires_at_ms + 1
                control.renew_authority(expiry)
                with pytest.raises(ValueError):
                    control.renew_authority(expiry)
                with pytest.raises(ValueError):
                    control.renew_authority(time.time_ns() // 1_000_000 + 600_000)
                renewed = True
            snapshots.append(snapshot)

        strategy.advance = observe
        return node, strategy, control, plan, public

    monkeypatch.setattr(scenarios, "build_node", instrumented)

    async def scenario():
        async with ProductionPeer(external=external) as peer:
            result = await scenarios.run_node(peer, tmp_path, scenarios.roundtrips(1),
                expect_denial=external)
            assert not peer.errors
            if external:
                assert not peer.posts
            else:
                scenarios.assert_economics(result, 4)

    asyncio.run(scenario())
    assert snapshots
    Path(__file__).with_name("backpack-synthetic-" + str(external).lower() + ".json").write_text(
        json.dumps(snapshots[-1], indent=2) + "\n", encoding="utf-8")
    for snapshot in snapshots:
        assert snapshot["complete"] and snapshot["connected"]
        assert 0 < snapshot["snapshot_started_ns"] <= snapshot["snapshot_completed_ns"]
        assert snapshot["epoch"] >= 0
        assert {row["instrument_id"] for row in snapshot["positions"]} >= {
            symbol + ".BACKPACK" for symbol in scenarios.SYMBOLS}
        assert snapshot["own_open_orders"] + snapshot["foreign_open_orders"] == len(snapshot["open_orders"])
    if external:
        assert any(row["venue_order_id"] == "external-untouchable"
            and row["client_order_id"] is None for snapshot in snapshots for row in snapshot["open_orders"])
    else:
        assert len({snapshot["snapshot_started_ns"] for snapshot in snapshots}) > 1
    with pytest.raises(RuntimeError):
        controls[0].account_snapshot_json()
    with pytest.raises(ValueError):
        controls[0].renew_authority(time.time_ns() // 1_000_000 + 300_000)

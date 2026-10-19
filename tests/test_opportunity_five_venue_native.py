"""Real native loopback orders crossing the application gateway and receipt barrier."""
from decimal import Decimal
from pathlib import Path
import sys
import time
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent / "support"))
from opportunity_native_execution import NativeOrderGateway
from opportunity_preflight import OrderIntent


def attach_gateway(strategy, venue, instruments, observations):
    """Adapt the existing reviewed native peer scenarios at their command boundary."""
    from nautilus_trader.trading import Strategy
    native = SimpleNamespace(cache=strategy.cache, order_factory=strategy.order_factory,
        submit_order=lambda order, **kw: Strategy.submit_order(strategy, order, **kw),
        cancel_order=lambda cid, **kw: Strategy.cancel_order(strategy, cid, **kw),
        modify_order=lambda cid, **kw: Strategy.modify_order(strategy, cid, **kw),
        query_order=lambda order, **kw: Strategy.query_order(strategy, order, **kw))
    gateway = NativeOrderGateway(native,
        [SimpleNamespace(venue=venue, instrument_id=name) for name in instruments],
        observations["receipts"].append, lambda account: None, observations["fills"].append)
    strategy.gateway = gateway
    return gateway


def route_submit(strategy, order, observations, venue, instruments):
    gateway = getattr(strategy, "gateway", None) or attach_gateway(strategy, venue, instruments, observations)
    cid = str(order.client_order_id)
    command = OrderIntent("submit", venue, str(order.instrument_id), "submit-" + cid, cid,
        str(order.side).split(".")[-1].upper(), order.quantity.as_decimal(), order.price.as_decimal(),
        str(order.time_in_force).split(".")[-1].upper(), order.is_post_only, order.is_reduce_only)
    observations["commands"].append(command)
    gateway.dispatch(command, time.time_ns())


def route_cancel(strategy, cid, observations):
    original = strategy.gateway.orders[str(cid)]["intent"]
    from dataclasses import replace
    command = replace(original, action="cancel", request_id="cancel-" + str(cid))
    observations["commands"].append(command)
    strategy.gateway.dispatch(command, time.time_ns())


def test_backpack_native_gateway_confirmed_cancel_replace_entry_and_close(tmp_path, monkeypatch):
    import asyncio
    import test_backpack_production_native as reviewed
    observed = {"receipts": [], "fills": [], "commands": []}
    original_build = reviewed.build_node
    gateways = []

    def build(*args):
        node, strategy, control, config, public = original_build(*args)
        gateway = attach_gateway(strategy, "BACKPACK", [s + ".BACKPACK" for s in reviewed.SYMBOLS], observed)
        gateways.append(gateway)
        strategy.submit_order = lambda order, **kw: route_submit(strategy, order, observed, "BACKPACK", [])
        strategy.cancel_order = lambda cid, **kw: route_cancel(strategy, cid, observed)
        original_event = strategy.event
        def event(kind, event):
            gateway.on_native_event(kind, event, time.time_ns())
            gateway.poll(time.time_ns())
            original_event(kind, event)
        strategy.event = event
        return node, strategy, control, config, public

    monkeypatch.setattr(reviewed, "build_node", build)
    async def scenario():
        async with reviewed.ProductionPeer() as peer:
            result = await reviewed.run_node(peer, tmp_path,
                [(reviewed.SYMBOLS[0], "cancel", "99.9"), *reviewed.roundtrips(1)])
            reviewed.assert_economics(result, 4)
            assert len(peer.deletes) == 1 and len(peer.posts) == 5
    try:
        asyncio.run(scenario())
    finally:
        for gateway in gateways:
            gateway.close()
    assert len(observed["fills"]) == 4
    assert sum(fill["commission_amount"] for fill in observed["fills"]) == Decimal("0.000004")
    assert sum(command.reduce_only for command in observed["commands"]) == 2
    assert len([receipt for receipt in observed["receipts"] if receipt.kind == "terminal"]) == 5
    assert len([receipt for receipt in observed["receipts"] if receipt.kind == "canceled"]) == 1


def test_ondo_native_gateway_startup_cancel_replace_and_multisymbol_continuation(tmp_path, monkeypatch):
    import test_ondo_continuous as reviewed
    from ondo_continuous import OndoContinuousStrategy
    from ondo_continuous_peer import INSTRUMENT, SECOND_INSTRUMENT
    observed = {"receipts": [], "fills": [], "commands": []}
    # The integration runner supplies candidate identity; _child verifies it again.
    gateways = []
    def submit(strategy, order, **kw):
        route_submit(strategy, order, observed, "ONDO", [INSTRUMENT, SECOND_INSTRUMENT])
        if strategy.gateway not in gateways:
            gateways.append(strategy.gateway)
    monkeypatch.setattr(OndoContinuousStrategy, "submit_order", submit)
    monkeypatch.setattr(OndoContinuousStrategy, "cancel_order", lambda strategy, cid, **kw: route_cancel(strategy, cid, observed))
    for kind in ("accepted", "filled", "canceled", "rejected", "denied", "expired"):
        original = getattr(OndoContinuousStrategy, "on_order_" + kind)
        def callback(strategy, event, kind=kind, original=original):
            gateway = getattr(strategy, "gateway", None)
            if gateway is not None:
                gateway.on_native_event(kind, event, time.time_ns())
                gateway.poll(time.time_ns())
            original(strategy, event)
        monkeypatch.setattr(OndoContinuousStrategy, "on_order_" + kind, callback)
    try:
        result = reviewed._child("maker", tmp_path / "ondo.jsonl")
    finally:
        for gateway in gateways:
            gateway.close()
    assert result["continued_in_same_strategy"], result["observation"]
    assert Decimal(result["venue_quantity"]) == 0
    assert {fill["instrument_id"] for fill in observed["fills"]} == {INSTRUMENT, SECOND_INSTRUMENT}
    assert any(command.action == "cancel" for command in observed["commands"])
    assert any(command.reduce_only for command in observed["commands"])
    assert all(fill["commission_amount"] is not None for fill in observed["fills"])
    assert len([receipt for receipt in observed["receipts"] if receipt.kind == "fill"]) == len(observed["fills"])

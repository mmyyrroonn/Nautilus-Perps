"""Actual installed engine terminal reconciliation with explicit synthetic account facts."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qsl, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import backpack_loopback
from backpack_loopback_config import parse_loopback_plan
from test_backpack_account_native import AccountPeer, SEED, order_frame
from test_backpack_loopback import loopback_document
from test_backpack_loopback_native import PrefixReader, event_records, require_loopback_native
from test_backpack_public_native import SYMBOL


class TerminalPeer(AccountPeer):
    """Two finite owned orders; the late-fill branch uses a canceled partial entry."""

    def __init__(self, *, late_fill=False):
        super().__init__(emit_orders=False)
        self.late_fill = late_fill
        self.posts = []
        self.deletes = []
        self.responses = []
        self.post_received = [asyncio.Event(), asyncio.Event()]
        self.accepted = [asyncio.Event(), asyncio.Event()]
        self.delete_received = asyncio.Event()
        self.flat_reconciled = asyncio.Event()
        self.late_sent = asyncio.Event()
        self.position = Decimal("0")
        self.last_order = {}
        self.refresh_quote = asyncio.Event()
        self.refreshed_event_ns = None

    def account_snapshot(self):
        # These facts come from this explicitly synthetic protocol scenario, never the cache.
        return {"observed_at_ms": time.time_ns() // 1_000_000,
                "net_positions": {f"{SYMBOL}.BACKPACK": format(self.position, "f")}}

    async def send_frames(self, ws, connection):
        await super().send_frames(ws, connection)
        await self.refresh_quote.wait()
        micros = time.time_ns() // 1000
        self.refreshed_event_ns = (micros - 1000) * 1000
        await ws.send(json.dumps({"stream": f"bookTicker.{SYMBOL}", "data": {
            "e": "bookTicker", "s": SYMBOL, "E": micros, "T": micros - 1000,
            "u": 2, "b": "100.0", "B": "1.00000", "a": "101.0", "A": "2.00000"}}))
        # Keep the admitted market immutable throughout the only reduction attempt.
        await asyncio.Event().wait()

    async def request(self, reader, writer):
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        lines = raw.decode("ascii").split("\r\n")
        method, target, _ = lines[0].split(" ")
        if urlsplit(target).path != "/api/v1/order":
            assert method == "GET"
            return await super().request(PrefixReader(reader, raw), writer)
        worker = asyncio.current_task()
        self.workers.add(worker)
        try:
            headers = {key.lower(): value for key, value in
                       (line.split(": ", 1) for line in lines[1:] if ": " in line)}
            if method == "GET":
                body = dict(parse_qsl(urlsplit(target).query))
            else:
                body = json.loads(await reader.readexactly(int(headers["content-length"])))
            instruction = {"POST": "orderExecute", "DELETE": "orderCancel", "GET": "orderQuery"}[method]
            def scalar(value):
                return str(value).lower() if type(value) is bool else str(value)
            canonical = "&".join(["instruction=" + instruction,
                *[f"{key}={scalar(value)}" for key, value in sorted(body.items())],
                "timestamp=" + headers["x-timestamp"], "window=" + headers["x-window"]])
            self.auth_key.public_key().verify(base64.b64decode(headers["x-signature"]), canonical.encode())
            assert headers["x-api-key"] == self.public_key
            self.requests.append((method, "/api/v1/order", {}))
            if method == "POST":
                index = len(self.posts)
                assert index < 2
                assert set(body) == {"symbol", "side", "orderType", "timeInForce", "quantity",
                                     "price", "clientId", "postOnly", "reduceOnly"}
                assert body["symbol"] == SYMBOL and body["orderType"] == "Limit"
                assert body["timeInForce"] == "GTC" and body["price"] == "100.1"
                assert body["postOnly"] is False
                assert body["side"] == ("Bid" if index == 0 else "Ask")
                assert body["reduceOnly"] is (index == 1)
                expected = "0.00002" if index == 0 or not self.late_fill else "0.00001"
                assert body["quantity"] == expected
                self.posts.append(body)
                response = {**body, "id": f"terminal-owned-{index}", "executedQuantity": "0", "status": "New"}
                self.last_order[response["id"]] = response
                self.post_received[index].set()
                status = "200 OK"
            elif method == "DELETE":
                assert self.late_fill and body == {"symbol": SYMBOL, "orderId": "terminal-owned-0"}
                self.deletes.append(body)
                self.delete_received.set()
                status, response = "202 Accepted", {}
            else:
                assert body["symbol"] == SYMBOL
                if "orderId" in body:
                    response = self.last_order[body["orderId"]]
                else:
                    response = next(value for value in self.last_order.values()
                                    if str(value["clientId"]) == body["clientId"])
                response = {**response, "createdAt": time.time_ns() // 1000}
                status = "200 OK"
            payload = json.dumps(response).encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
            self.responses.append(status)
        finally:
            writer.close()
            await writer.wait_closed()
            self.workers.discard(worker)

    def fill(self, index, trade, last, cumulative, *, terminal):
        frame = order_frame("orderFill", trade, filled=terminal)
        body = self.posts[index]
        frame["data"].update(c=body["clientId"], i=f"terminal-owned-{index}", S=body["side"],
            q=body["quantity"], r=body["reduceOnly"], y=False, l=last, z=cumulative,
            Z=format(Decimal(cumulative) * Decimal("100.1"), "f"), X="Filled" if terminal else "PartiallyFilled")
        self.last_order[f"terminal-owned-{index}"] = {**body, "id": f"terminal-owned-{index}",
            "executedQuantity": cumulative, "status": frame["data"]["X"]}
        return frame

    async def send_private(self, ws, connection):
        assert connection == 1
        await self.ready.wait()
        await self.post_received[0].wait()
        await self.accepted[0].wait()
        entry_quantity = "0.00001" if self.late_fill else "0.00002"
        self.position = Decimal(entry_quantity)
        await ws.send(json.dumps(self.fill(0, 901, entry_quantity, entry_quantity, terminal=not self.late_fill)))
        if self.late_fill:
            await self.delete_received.wait()
            terminal = order_frame("orderCancelled")
            terminal["data"].update(c=self.posts[0]["clientId"], i="terminal-owned-0", y=False,
                X="Cancelled", z=entry_quantity, Z="0.001001")
            self.last_order["terminal-owned-0"].update(status="Cancelled")
            await ws.send(json.dumps(terminal))
        await self.post_received[1].wait()
        await self.accepted[1].wait()
        self.position = Decimal("0")
        await ws.send(json.dumps(self.fill(1, 902, entry_quantity, entry_quantity, terminal=True)))
        if self.late_fill:
            await self.flat_reconciled.wait()
            self.position = Decimal("0.00001")
            await ws.send(json.dumps(self.fill(0, 903, "0.00001", "0.00002", terminal=True)))
            self.late_sent.set()


def terminal_plan(peer, tmp_path, monkeypatch):
    data = loopback_document()
    data["session"].update(base_url_http=peer.http_url, base_url_ws=peer.ws_url, duration_secs=6,
        request_timeout_secs=1, stale_after_ms=1000, output_dir="reports", state_dir="state")
    data["authority"].update(valid_for_ms=6000, max_account_age_ms=4000, max_market_age_ms=6000,
        allow_reduction=True)
    data["scenario"].update(cancel_after_fill=peer.late_fill, stale_probe_delay_ms=1500)
    plan = replace(parse_loopback_plan(data, tmp_path), durable_economics=True)
    monkeypatch.setenv(plan.account.credential_env, SEED)
    # Shorten the finite run after validating the original authority; never renew it.
    return replace(plan, session=replace(plan.session, duration_secs=5))


def terminal_strategy(peer, mode, observations):
    def factory(plan, evidence, control):
        from nautilus_trader.adapters.backpack import BackpackLoopbackAccountFacts
        from nautilus_trader.model import InstrumentId, OrderSide, Price, Quantity, TimeInForce
        from nautilus_trader.trading import Strategy

        class TerminalStrategy(Strategy):
            def __init__(self):
                super().__init__()
                self.started = False
                self.session = self.entry = self.reduction = self.initial_flat = None
                self.fills = 0
                self.cancel_sent = False
                self.reconciled = False
                self.old_flat_refused = False
                self.instrument = InstrumentId.from_str(f"{SYMBOL}.BACKPACK")

            def on_start(self):
                self.started = True

            def on_stop(self):
                self.started = False

            def facts(self, snapshot):
                fields = {**plan.facts.document(), **snapshot}
                control.accept_account(self.session, BackpackLoopbackAccountFacts(**fields))
                evidence.record("explicit_peer_account_snapshot", {**snapshot,
                    "source": "current complete synthetic peer fixture; never inferred from cache"})

            def order(self, side, quantity, *, reduce_only):
                return self.order_factory.limit(self.instrument, side, Quantity.from_str(quantity),
                    Price.from_str("100.1"), time_in_force=TimeInForce.GTC,
                    post_only=False, reduce_only=reduce_only)

            def reconcile(self):
                result = json.loads(control.reconcile_terminal_evidence(self.session))
                evidence.record("native_terminal_reconciliation", result)
                observations.append(result)
                return result

            def advance(self, health):
                if not self.started or evidence.actor_failure:
                    return
                consumer = health["loopback"].get("economic_consumer") or {}
                if self.entry is None:
                    if (not health["public"]["quotes_fresh"].get(SYMBOL, False)
                            or not health["account"]["rest_snapshot_observed"]
                            or not health["account"]["transport_connected"]):
                        return
                    self.session = control.begin_session()
                    self.initial_flat = peer.account_snapshot()
                    self.facts(self.initial_flat)
                    control.refresh_market(self.session, str(self.instrument))
                    self.entry = self.order(OrderSide.BUY, "0.00002", reduce_only=False)
                    self.submit_order(self.entry)
                    return
                entry = self.cache.order(self.entry.client_order_id)
                if peer.late_fill and self.fills == 1 and not self.cancel_sent:
                    self.cancel_order(self.entry.client_order_id)
                    self.cancel_sent = True
                if (self.reduction is None and entry.is_closed and consumer.get("durable_receipts", 0) >= 1):
                    peer.refresh_quote.set()
                    current_quote = health["public"].get("quote_event_ns", {}).get(SYMBOL)
                    if peer.refreshed_event_ns is None or current_quote != str(peer.refreshed_event_ns):
                        return
                    assert self.reconcile()["orders"][0]["reconciled"]
                    snapshot = peer.account_snapshot()
                    assert Decimal(snapshot["net_positions"][str(self.instrument)]) > 0
                    self.facts(snapshot)
                    control.refresh_market(self.session, str(self.instrument))
                    self.reduction = self.order(OrderSide.SELL,
                        snapshot["net_positions"][str(self.instrument)], reduce_only=True)
                    self.submit_order(self.reduction)
                    return
                if (self.reduction is not None and self.cache.order(self.reduction.client_order_id).is_closed
                        and consumer.get("durable_receipts", 0) >= 2 and not self.reconciled):
                    result = self.reconcile()
                    assert len(result["orders"]) == 2 and all(row["reconciled"] for row in result["orders"])
                    assert result["flat_snapshot_current"] is False
                    self.reconciled = True
                    evidence.record("actual_portfolio_position", {
                        "net_quantity": str(self.portfolio.net_position(self.instrument)),
                        "source": "actual native Portfolio observation; not flat authority"})
                    if mode != "old_flat":
                        self.facts(peer.account_snapshot())
                        assert self.reconcile()["flat_snapshot_current"] is True
                    peer.flat_reconciled.set()
                if self.reconciled:
                    if mode == "old_flat":
                        age = time.time_ns() // 1_000_000 - self.initial_flat["observed_at_ms"]
                        if age <= plan.authority.max_account_age_ms or self.old_flat_refused:
                            return
                        with pytest.raises(RuntimeError):
                            self.facts(self.initial_flat)
                        self.old_flat_refused = True
                        evidence.record("old_peer_flat_snapshot_refused", {"observed_at_ms": self.initial_flat["observed_at_ms"]})
                    elif mode == "fresh":
                        self.facts(peer.account_snapshot())
                    if mode != "late_fill" or (self.fills == 3 and consumer.get("durable_receipts", 0) == 3):
                        evidence.extra_summary["scenario_steps_observed"] = True
                    evidence.extra_summary["cancel_requested"] = self.cancel_sent
                    evidence.record("actual_portfolio_position", {
                        "net_quantity": str(self.portfolio.net_position(self.instrument)),
                        "source": "actual native Portfolio observation; not flat authority"})

            def on_order_accepted(self, event):
                index = 0 if event.client_order_id == self.entry.client_order_id else 1
                peer.accepted[index].set()

            def on_order_filled(self, event):
                self.fills += 1
                evidence.record("terminal_true_fill", {"client_order_id": str(event.client_order_id),
                    "trade_id": str(event.trade_id), "quantity": str(event.last_qty)})

            def on_order_denied(self, event):
                evidence.record("terminal_unexpected_denial", {"reason": str(event.reason)})
                evidence.actor_failure = "terminal_order_denied"
                evidence.handle.stop()

            def on_order_rejected(self, event):
                evidence.actor_failure = "terminal_order_rejected"
                evidence.handle.stop()

        return TerminalStrategy()
    return factory


@pytest.mark.parametrize("mode", ["fresh", "old_flat", "late_fill"])
def test_installed_terminal_receipts_and_explicit_flat_authority(tmp_path, monkeypatch, mode):
    require_loopback_native()

    async def scenario():
        async with TerminalPeer(late_fill=mode == "late_fill") as peer:
            reconciliations = []
            monkeypatch.setattr(backpack_loopback, "_strategy", terminal_strategy(peer, mode, reconciliations))
            plan = terminal_plan(peer, tmp_path, monkeypatch)
            summary, path = await asyncio.wait_for(backpack_loopback.run_loopback(plan), 18)
            assert summary["status"] == "completed", {"failure": summary["failure"],
                "requests": peer.requests, "posts": peer.posts, "native": summary["native_health"]}
            assert summary["scenario_steps_observed"] and summary["shutdown_complete"]
            assert len(peer.posts) == 2 and len(peer.deletes) == (1 if mode == "late_fill" else 0)
            assert peer.active == 0
            assert summary["economic_consumer"]["durable_receipts"] == (3 if mode == "late_fill" else 2)
            assert summary["economic_consumer"]["pending_fills"] == 0
            report = summary["native_health"]["loopback"]["shutdown_report"]
            assert report is not None
            rows = event_records(path)
            fills = [row for row in rows if row["kind"] == "terminal_true_fill"]
            assert [row["trade_id"] for row in fills] == (["901", "902", "903"] if mode == "late_fill" else ["901", "902"])
            positions = [Decimal(row["net_quantity"]) for row in rows if row["kind"] == "actual_portfolio_position"]
            assert Decimal("0") in positions
            if mode == "fresh":
                assert not report["dirty"] and not report["positions_unknown_or_nonzero"]
                assert summary["execution_settled"] and summary["flat_verified"]
                assert summary["durable_economic_acknowledgement"]
            else:
                assert report["dirty"] and report["positions_unknown_or_nonzero"]
                assert not summary["flat_verified"]
                if mode == "old_flat":
                    assert any(row["kind"] == "old_peer_flat_snapshot_refused" for row in rows)
                    assert not summary["execution_settled"]
                else:
                    assert peer.late_sent.is_set() and Decimal("0.00001") in positions
                    assert not summary["execution_settled"]
            # Native cache and Portfolio values substantiate delivery; only the explicit
            # current peer account snapshot can authorize the independent flat conclusion.
            actual = [row for row in rows if row["kind"] == "engine_account_observation" and len(row["orders"]) == 2]
            assert actual
            assert {trade for order in actual[-1]["orders"] for trade in order["trade_ids"]} == set(row["trade_id"] for row in fills)
            commissions = sorted(order["commissions"] for order in actual[-1]["orders"])
            assert commissions == sorted([["-0.00000200 USDC"], ["-0.00000100 USDC"]]
                                         if mode == "late_fill" else [["-0.00000100 USDC"], ["-0.00000100 USDC"]])
            assert any(row["terminal_observed"] and row["durable_economic_complete"]
                       for snapshot in reconciliations for row in snapshot["orders"])
            assert SEED not in path.read_text()
    asyncio.run(scenario())

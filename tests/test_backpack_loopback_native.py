"""Installed-wheel real Strategy/risk/engine against signed owned synthetic peers."""
from __future__ import annotations

import asyncio
import base64
from decimal import Decimal
import json
from pathlib import Path
import sys
import time
from urllib.parse import parse_qsl, urlsplit

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from backpack_loopback import run_loopback
from backpack_loopback_config import parse_loopback_plan
from backpack_public import PublicEvidence
from test_backpack_account_native import AccountPeer, SEED, order_frame
from test_backpack_loopback import loopback_document
from test_backpack_public_native import SYMBOL


def require_loopback_native():
    module = pytest.importorskip("nautilus_trader.adapters.backpack")
    if not hasattr(module, "BackpackLoopbackExecutionClientConfig"):
        pytest.skip("install the explicitly source-bound #73 loopback candidate")


class PrefixReader:
    """Hand an already-read GET header to the existing signed readonly fixture unchanged."""
    def __init__(self, reader, header):
        self.reader, self.header = reader, header

    async def readuntil(self, separator):
        header, self.header = self.header, None
        return header if header is not None else await self.reader.readuntil(separator)


class OrderPeer(AccountPeer):
    def __init__(self, *, unknown_post=False):
        super().__init__(emit_orders=False)
        self.unknown_post = unknown_post
        self.posts = []
        self.deletes = []
        self.responses = []
        self.post_received = asyncio.Event()
        self.probe_denied = asyncio.Event()
        self.primary_accepted = asyncio.Event()
        self.delete_received = asyncio.Event()
        self.post_at_ns = None
        self.metadata_started_at_ns = None
        self.order_queries = []
        self.fill_frames = []

    async def request(self, reader, writer):
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 3)
        lines = raw.decode("ascii").split("\r\n")
        method, target, _ = lines[0].split(" ")
        if method == "GET" and urlsplit(target).path != "/api/v1/order":
            if urlsplit(target).path == "/api/v1/markets":
                self.metadata_started_at_ns = time.time_ns()
            return await super().request(PrefixReader(reader, raw), writer)
        worker = asyncio.current_task()
        self.workers.add(worker)
        try:
            assert method in {"GET", "POST", "DELETE"} and urlsplit(target).path == "/api/v1/order"
            headers = {key.lower(): value for key, value in
                (line.split(": ", 1) for line in lines[1:] if ": " in line)}
            if method == "GET":
                body = dict(parse_qsl(urlsplit(target).query))
            else:
                length = int(headers["content-length"])
                assert 0 < length <= 4096
                body = json.loads(await asyncio.wait_for(reader.readexactly(length), 2))
            def scalar(value):
                return str(value).lower() if type(value) is bool else str(value)
            instruction = {"GET": "orderQuery", "POST": "orderExecute", "DELETE": "orderCancel"}[method]
            canonical = "&".join(["instruction=" + instruction,
                *[f"{key}={scalar(value)}" for key, value in sorted(body.items())],
                "timestamp=" + headers["x-timestamp"], "window=" + headers["x-window"]])
            self.auth_key.public_key().verify(base64.b64decode(headers["x-signature"]), canonical.encode())
            assert headers["x-api-key"] == self.public_key
            self.requests.append((method, "/api/v1/order", {}))
            if method == "GET":
                assert self.posts
                assert body in ({"symbol": SYMBOL, "orderId": "local-owned-A"},
                    {"symbol": SYMBOL, "clientId": str(self.posts[0]["clientId"])})
                self.order_queries.append(time.time_ns())
                status = "200 OK"
                response = {**self.posts[0], "id": "local-owned-A",
                    "createdAt": self.post_at_ns // 1000,
                    "executedQuantity": "0.00001" if self.fill_frames else "0",
                    "executedQuoteQuantity": "0.001001" if self.fill_frames else "0",
                    "status": "PartiallyFilled" if self.fill_frames else "New",
                    "selfTradePrevention": "RejectTaker"}
            elif method == "POST":
                assert set(body) == {"symbol", "side", "orderType", "timeInForce", "quantity", "price", "clientId", "postOnly", "reduceOnly"}
                assert body["symbol"] == SYMBOL and body["side"] == "Bid"
                assert body["orderType"] == "Limit" and body["timeInForce"] == "GTC"
                assert body["quantity"] == "0.00002" and body["price"] == "100.1"
                assert not body["postOnly"] and not body["reduceOnly"]
                self.posts.append(body)
                self.post_at_ns = time.time_ns()
                self.post_received.set()
                if self.unknown_post:
                    return  # Accepted bytes followed by a lost response must stay Unknown.
                status = "200 OK"
                response = {**body, "id": "local-owned-A", "executedQuantity": "0", "status": "New"}
            else:
                assert body == {"symbol": SYMBOL, "orderId": "local-owned-A"}
                self.deletes.append(body)
                self.delete_received.set()
                status, response = "202 Accepted", {}
            payload = json.dumps(response).encode()
            writer.write(f"HTTP/1.1 {status}\r\nContent-Type: application/json\r\nContent-Length: {len(payload)}\r\nConnection: close\r\n\r\n".encode() + payload)
            await writer.drain()
            if method != "GET":
                self.responses.append(status)
        finally:
            writer.close()
            await writer.wait_closed()
            self.workers.discard(worker)

    async def send_private(self, ws, connection):
        if connection > 1:
            return  # The later readonly reopen observes REST only and emits no new private economics.
        await self.ready.wait()
        await self.post_received.wait()
        if self.unknown_post:
            # Matching numeric c from a private observation cannot independently prove ownership.
            frame = order_frame("orderFill", 701)
            frame["data"].update(c=self.posts[0]["clientId"], i="local-owned-A", y=False)
            await ws.send(json.dumps(frame))
            return
        # The test observes the actual Strategy Denied event before releasing any true fill.
        # No intervening wallet/order/position event can invalidate the explicit account facts.
        await self.probe_denied.wait()
        frame = order_frame("orderFill", 701)
        frame["data"].update(c=self.posts[0]["clientId"], i="local-owned-A", y=False)
        self.fill_frames = [frame, frame]
        for fill in self.fill_frames:
            await ws.send(json.dumps(fill))
            await asyncio.sleep(0.1)
        self.fixture_sent = True
        self.fixture_ready.set()
        await self.delete_received.wait()
        # Keep order open: HTTP202 is only pending cancellation, never a terminal order event.


def plan_for(peer, tmp_path, monkeypatch):
    data = loopback_document()
    data["session"].update(base_url_http=peer.http_url, base_url_ws=peer.ws_url,
        duration_secs=10, request_timeout_secs=2, stale_after_ms=500)
    data["authority"].update(valid_for_ms=10000, max_account_age_ms=10000, max_market_age_ms=10000)
    data["scenario"]["stale_probe_delay_ms"] = 1500
    data["mutation_budget_ms"] = 1000
    plan = parse_loopback_plan(data, tmp_path)
    monkeypatch.setenv(plan.account.credential_env, SEED)
    return plan


def event_records(path):
    return [json.loads(line) for line in path.with_name("events.jsonl").read_text().splitlines()]


def test_actual_strategy_ack_quote_stale_denial_fill_duplicate_cancel202_and_dirty_stop(tmp_path, monkeypatch):
    require_loopback_native()
    import backpack_account
    account_node = backpack_account._account_node
    configured = []
    def checked_node(*args, **kwargs):
        config = kwargs["exec_config"]
        assert config.reconciliation is False
        assert config.inflight_check_interval_ms == 0
        assert config.open_check_interval_secs is None
        assert config.position_check_interval_secs is None
        configured.append(True)
        return account_node(*args, **kwargs)
    monkeypatch.setattr(backpack_account, "_account_node", checked_node)
    original = PublicEvidence.record
    async def scenario():
        async with OrderPeer() as peer:
            def record(evidence, kind, fields):
                original(evidence, kind, fields)
                if kind == "order_accepted":
                    peer.primary_accepted.set()
                if kind == "order_denied":
                    peer.probe_denied.set()
            monkeypatch.setattr(PublicEvidence, "record", record)
            plan = plan_for(peer, tmp_path, monkeypatch)
            summary, path = await asyncio.wait_for(run_loopback(plan), 23)
            assert summary["status"] == "completed", {"failure": summary["failure"],
                "account": summary["native_health"]["account"], "requests": peer.requests,
                "responses": peer.responses, "order_queries": peer.order_queries}
            assert configured
            assert summary["native_inflight_checks_enabled"] is False
            assert summary["continuous_reconciliation"] is False
            assert summary["scenario_completed"] and summary["scenario_steps_observed"]
            assert summary["cancel_requested"] and summary["cancel_unsettled_observed"]
            assert summary["cancel202_observed"]
            assert not summary["execution_settled"] and not summary["flat_verified"]
            assert summary["shutdown_complete"] and peer.active == 0
            assert len(peer.posts) == 1 and len(peer.deletes) == 1
            assert peer.responses == ["200 OK", "202 Accepted"]
            rows = event_records(path)
            kinds = [row["kind"] for row in rows]
            assert kinds.count("scenario_submit") == 1
            assert kinds.count("scenario_stale_probe") == 1
            assert kinds.count("order_denied") == 1
            assert kinds.count("order_filled") == 1
            assert "order_canceled" not in kinds
            assert kinds.index("order_accepted") < kinds.index("order_denied") < kinds.index("order_filled")
            probe = next(row for row in rows if row["kind"] == "scenario_stale_probe")
            assert probe["public_quotes_fresh"] is False
            denial = next(row for row in rows if row["kind"] == "order_denied")
            assert denial["reason"] == "SUBMIT_FAILED: Backpack execution refused: Readiness"
            # Verify elapsed time leaves public metadata/account/authority alive and remaining
            # two-order notional/margin/capacity; exact native reason is asserted after wheel run.
            denied_ns = int(denial["ts_event_ns"])
            assert 0 < (denied_ns - peer.post_at_ns) / 1_000_000 < plan.authority.max_account_age_ms
            assert 0 < (denied_ns - peer.metadata_started_at_ns) // 1_000_000 < plan.authority.max_market_age_ms
            assert denied_ns // 1_000_000 < probe["authority_expires_at_ms"]
            assert 0 <= denied_ns // 1_000_000 - probe["synthetic_facts_observed_at_ms"] < plan.authority.max_account_age_ms
            assert probe["account_reasserted_without_market_refresh"]
            assert probe["public_health"]["metadata_ready"]
            assert probe["public_health"]["connected"]
            assert not peer.fill_frames or kinds.index("order_denied") < kinds.index("order_filled")
            assert plan.authority.max_unsettled_orders == 2
            assert Decimal(peer.posts[0]["quantity"]) * Decimal(peer.posts[0]["price"]) * 2 < plan.authority.max_reserved_notional
            native = summary["native_health"]["loopback"]
            assert len(native["pending_fills"]) == 1
            shutdown = native["shutdown_report"]
            assert shutdown["dirty"] and shutdown["pending_cancellations"] == 1
            assert shutdown["positions_unknown_or_nonzero"]
            observations = [row for row in rows if row["kind"] == "engine_account_observation"]
            actual = next(row for row in reversed(observations) if row["positions"])
            assert actual["positions"][0]["is_open"] and actual["positions"][0]["quantity"] == "0.00001"
            order = next(order for order in actual["orders"] if order["trade_ids"])
            assert order["trade_ids"] == ["701"] and order["filled_quantity"] == "0.00001"
            assert order["commissions"] == ["-0.00000100 USDC"]
            assert not actual["durable_economic_acknowledgement"]
            assert summary["native_health"]["account"]["private_subscription_confirmed"] is False
    asyncio.run(scenario())


def test_unknown_post_is_not_rejected_retried_or_adopted_from_numeric_client_id(tmp_path, monkeypatch):
    require_loopback_native()
    async def scenario():
        async with OrderPeer(unknown_post=True) as peer:
            plan = plan_for(peer, tmp_path, monkeypatch)
            summary, path = await asyncio.wait_for(run_loopback(plan), 23)
            assert summary["status"] == "failed" and not summary["scenario_completed"]
            assert len(peer.posts) == 1 and not peer.deletes
            rows = event_records(path)
            assert not any(row["kind"] in {"order_rejected", "order_accepted", "order_filled"} for row in rows)
            assert not any(row["positions"] for row in rows if row["kind"] == "engine_account_observation")
            shutdown = summary["native_health"]["loopback"]["shutdown_report"]
            assert shutdown["dirty"] and shutdown["unknown"] >= 1
            assert summary["shutdown_complete"] and peer.active == 0
    asyncio.run(scenario())


def test_report_budget_exhaustion_on_submit_record_stops_before_any_post(tmp_path, monkeypatch):
    require_loopback_native()
    from dataclasses import replace
    original = PublicEvidence.record
    reached_submit = []
    def record(evidence, kind, fields):
        if kind == "scenario_submit":
            # Deterministically place the actual encoded report cap on this intent line,
            # independent of concurrent native health notifications before admission.
            reached_submit.append(True)
            evidence.plan = replace(evidence.plan,
                session=replace(evidence.plan.session, max_report_events=len(evidence.lines)))
        original(evidence, kind, fields)
    monkeypatch.setattr(PublicEvidence, "record", record)
    async def scenario():
        async with OrderPeer() as peer:
            plan = plan_for(peer, tmp_path, monkeypatch)
            summary, path = await asyncio.wait_for(run_loopback(plan), 23)
            assert reached_submit
            assert summary["failure"] == "report_limit" and summary["status"] == "failed"
            assert summary["report_limit_reached"] and not summary["scenario_completed"]
            assert not peer.posts and not peer.deletes
            assert summary["shutdown_complete"] and peer.active == 0
            assert path.is_file()
    asyncio.run(scenario())


def test_external_cancellation_publishes_dirty_native_stop_and_releases_identity(tmp_path, monkeypatch):
    require_loopback_native()
    from dataclasses import replace
    from backpack_account import run_account
    async def scenario():
        async with OrderPeer() as peer:
            plan = plan_for(peer, tmp_path, monkeypatch)
            task = asyncio.create_task(run_loopback(plan))
            await asyncio.wait_for(peer.post_received.wait(), 12)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(task, 13)
            reports = list(plan.output_dir.glob("*/summary.json"))
            assert len(reports) == 1
            stopped = json.loads(reports[0].read_text())
            assert stopped["failure"] == "cancelled" and stopped["shutdown_complete"]
            assert stopped["native_health"]["loopback"]["shutdown_report"]["dirty"]
            assert peer.active == 0 and len(peer.posts) == 1
            # A fresh readonly factory opens the exact same durable identity namespace.
            # The stopped loopback runner's telemetry/control/poll closure must retain no lock.
            readonly = replace(plan.session, duration_secs=2, request_timeout_secs=1)
            result, _ = await asyncio.wait_for(run_account(readonly), 13)
            assert result["runtime_started"] and result["shutdown_complete"]
            assert result["durable_state_opened"] is True
            assert len(peer.posts) == 1 and peer.active == 0
    asyncio.run(scenario())


def test_minimum_real_report_budget_keeps_authoritative_native_stop_counts(tmp_path, monkeypatch):
    require_loopback_native()
    from dataclasses import replace
    async def scenario():
        async with OrderPeer() as peer:
            plan = plan_for(peer, tmp_path, monkeypatch)
            plan = replace(plan, session=replace(plan.session, max_report_bytes=8192))
            summary, path = await asyncio.wait_for(run_loopback(plan), 23)
            assert summary["status"] == "failed" and summary["failure"] == "report_limit"
            assert summary["summary_truncated"]
            assert summary["native_shutdown_report"] is not None
            assert type(summary["native_shutdown_report"]["dirty"]) is bool
            assert summary["dirty_shutdown"] == summary["native_shutdown_report"]["dirty"]
            assert type(summary["pending_fill_count"]) is int
            assert summary["exposure_unknown"] and not summary["flat_verified"]
            assert not summary["execution_settled"]
            assert sum(p.stat().st_size for p in path.parent.iterdir()) <= 8192
            assert not peer.posts and not peer.deletes and peer.active == 0
    asyncio.run(scenario())

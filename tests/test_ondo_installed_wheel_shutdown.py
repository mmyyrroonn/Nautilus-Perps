"""Exercise the installed Ondo wheel through a real LiveNode and loopback venue.

The venue uses synthetic frames. Passing these tests proves native/application
integration, never the real venue's release contract or a live economic result.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed

from test_installed_wheel_livenode import (
    ROOT,
    _child_environment,
    _formal_candidate_wheel,
)


RESULT_PREFIX = "ONDO_SHUTDOWN_RESULT "
READY_PREFIX = "ONDO_RECOVERY_READY "
CHANNEL = "cancelAllOrdersAfterPerps"
INSTRUMENT = "NVDA-USD-PERP.ONDO"
PRIVATE_MARKER = "SYNTHETIC_PRIVATE_DO_NOT_REPORT"


class LoopbackOndo:
    """Script only account reads and private ACKs, and record every write."""

    def __init__(self, release: str) -> None:
        self.release = release
        self.requests: list[tuple[str, str]] = []
        self.frames: list[tuple[str, str]] = []
        self.lock = threading.Lock()
        self.loop = asyncio.new_event_loop()
        self.ready = threading.Event()
        venue = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self.answer()

            def do_POST(self) -> None:  # noqa: N802
                self.answer()

            def do_DELETE(self) -> None:  # noqa: N802
                self.answer()

            def answer(self) -> None:
                path = urlsplit(self.path).path
                with venue.lock:
                    venue.requests.append((self.command, path))
                # Authentication headers are deliberately neither inspected nor recorded
                if self.command != "GET":
                    self.send_error(400)
                    return
                if path == "/v1/account":
                    result = {"accountID": "unit-account"}
                elif path == "/v1/markets":
                    result = {
                        "perps": {"tradingPairs": [{
                            "market": "NVDA-USD.P", "baseIncrement": "0.01",
                            "quoteIncrement": "0.01", "disabled": False,
                            "makerFee": "0.0001", "takerFee": "0.00025",
                        }]},
                        "tokenConfig": [{
                            "id": "USDC", "name": "USD Coin", "decimals": 2,
                            "tokenDecimals": 6, "networks": {},
                        }],
                    }
                elif path == "/v1/perps/contracts":
                    result = [{"market": "NVDA-USD.P", "isClosed": False, "disabled": False}]
                elif path == "/v1/perps/balance":
                    result = dict.fromkeys((
                        "realizedPnl", "unrealizedPnl", "usedMargin",
                        "maintenanceMarginRequirement", "totalMaintenanceMargin",
                        "marginRatio", "leverage", "totalFundingPayments",
                        "totalTradingFees", "totalPnL",
                    ), "0.00")
                    result.update(dict.fromkeys((
                        "walletBalance", "marginBalance", "availableMargin",
                        "withdrawableMargin", "netInvested",
                    ), "5000.00"))
                    result["underLiquidation"] = False
                elif path == "/v1/perps/orders" and venue.release == "history":
                    result = [{
                        "orderId": "historical-venue", "clientOrderId": "historical-client",
                        "side": "buy", "price": "227.50", "size": "1.00",
                        "market": "NVDA-USD.P", "filledSize": "1.00",
                        "lastFillSize": "0.00", "filledCost": "227.50", "fee": "0.00",
                        "status": "fullyfilled", "createdAt": "2025-03-05T14:30:00Z",
                        "type": "limit", "timeInForce": "GTC", "reduceOnly": False,
                    }]
                elif path in {
                    "/v1/perps/orders", "/v1/perps/fills", "/v1/perps/positions",
                    "/v1/perps/funding_fees",
                }:
                    result = []
                else:
                    self.send_error(404)
                    return
                body = json.dumps({"success": True, "result": result}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.http_thread.start()

        async def websocket(socket) -> None:
            try:
                async for text in socket:
                    message = json.loads(text)
                    op, channel = message.get("op"), message.get("channel", "")
                    with venue.lock:
                        venue.frames.append((op, channel))
                    if op == "login":
                        reply = {"type": "loggedIn", "msg": "Login successful"}
                    elif op == "subscribe":
                        reply = {"type": "subscribed", "channel": channel}
                    elif op == "unsubscribe" and channel == CHANNEL:
                        # Real venue rejects timeout_seconds on unsubscribe (2026-10-01).
                        if "timeout_seconds" in message:
                            await socket.send(json.dumps({"type": "error", "msg": "timeout_seconds not permitted on unsubscribe"}))
                            continue
                        if release in {"missing", "crash", "outer_timeout"}:
                            continue
                        if release == "connection_loss":
                            await socket.close()
                            return
                        if release == "delayed":
                            await asyncio.sleep(0.08)
                        reply = {"type": "unsubscribed", "channel": channel}
                        if release == "wrong_channel":
                            reply["channel"] = "ordersPerps"
                        elif release == "old_arm":
                            reply["type"] = "subscribed"
                        elif release == "ambiguous":
                            reply = {"type": "update", "channel": channel, "data": {
                                "status": "disabled", "enabled": False,
                                "timeout_seconds": 0, "op": "unsubscribe",
                                "private": PRIVATE_MARKER,
                            }}
                    elif op == "ping":
                        reply = {"type": "pong"}
                    else:
                        continue
                    await socket.send(json.dumps(reply))
            except ConnectionClosed:
                pass

        async def start() -> None:
            self.ws = await serve(websocket, "127.0.0.1", 0)
            self.ws_port = self.ws.sockets[0].getsockname()[1]
            self.ready.set()

        def run() -> None:
            asyncio.set_event_loop(self.loop)
            self.loop.run_until_complete(start())
            self.loop.run_forever()

        self.ws_thread = threading.Thread(target=run, daemon=True)
        self.ws_thread.start()
        assert self.ready.wait(5), "loopback WS did not start"

    def close(self) -> None:
        async def stop() -> None:
            self.ws.close()
            await self.ws.wait_closed()

        asyncio.run_coroutine_threadsafe(stop(), self.loop).result(5)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.ws_thread.join(5)
        self.http.shutdown()
        self.http.server_close()
        self.http_thread.join(5)


def _identity() -> dict:
    import os
    sys.path.insert(0, str(ROOT / "scripts"))
    from verify_native_install import verify

    if "ONDO_TEST_WHEEL" in os.environ:
        # Local candidates are opt-in and bind both artifact and sidecar digests.
        wheel = Path(os.environ["ONDO_TEST_WHEEL"])
        expected_wheel = os.environ["ONDO_TEST_WHEEL_SHA256"]
        provenance = Path(os.environ["ONDO_TEST_PROVENANCE"])
        expected_provenance = os.environ["ONDO_TEST_PROVENANCE_SHA256"]
        expected_native = None
    else:
        wheel = _formal_candidate_wheel()
        lock = json.loads((ROOT / "config/native-candidate.lock.json").read_text())
        artifact = next(item for item in lock["artifacts"] if item["filename"] == wheel.name)
        expected_wheel = artifact["sha256"]
        expected_provenance = artifact["provenance_sha256"]
        provenance = Path(os.environ.get("ONDO_TEST_PROVENANCE", str(
            ROOT / ".native-cache/release-stage/native-provenance-windows.json")))
        expected_native = lock["native"]["commit"]
    assert hashlib.sha256(provenance.read_bytes()).hexdigest() == expected_provenance
    identity = verify(
        wheel, expected_wheel, provenance=provenance,
        require_source_binding=True, require_ondo=True,
    )
    if expected_native is not None:
        assert identity["native_provenance"]["declared_native"]["commit"] == expected_native
    return identity


def _run_child(release: str, journal: Path, readonly: bool = False) -> dict:
    identity = _identity()
    from nautilus_trader.adapters.ondo import (
        OndoEnvironment, OndoExecutionClientConfig, OndoExecutionClientFactory,
        OndoExecutionEnvelopeConfig,
    )
    from nautilus_trader.common import Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveExecutionEngineConfig, LiveNode
    from nautilus_trader.model import AccountId, InstrumentId, TraderId
    from nautilus_trader.trading import Strategy

    sys.path.insert(0, str(ROOT / "src"))
    from ondo_dms_diagnostics import read_dms_release_diagnostics, read_shutdown_diagnostics
    from ondo_trade_probe import PRODUCTION_TRADE_DISCONNECTION_TIMEOUT_SECS, _native_account_cashflows

    venue = LoopbackOndo(release)
    factory = OndoExecutionClientFactory()
    run_id = f"offline-{release}-{'readonly' if readonly else 'production'}"
    timeline: dict[str, float] = {}
    started = threading.Event()
    stopped = threading.Event()
    watchdog_fired = threading.Event()
    now = time.time_ns()
    envelope = None if readonly else OndoExecutionEnvelopeConfig(
        instrument_id=InstrumentId.from_str(INSTRUMENT), entry_side="buy",
        entry_max_quantity="0.05", entry_worst_price="230", entry_max_notional_usd="15",
        close_side="sell", close_max_quantity="0.05", close_worst_price="225",
        max_close_attempts=2, max_notional_per_order_usd="50", max_gross_exposure_usd="100",
        min_available_margin_usdc="25", max_orders=3, max_new_risk_requests=1,
        max_app_requests=6, entry_deadline_unix_nanos=now + 60_000_000_000,
        cleanup_deadline_unix_nanos=now + 120_000_000_000, require_flat_start=True,
    )
    config = OndoExecutionClientConfig(
        environment=OndoEnvironment.PRODUCTION, account_id=AccountId.from_str("ONDO-OFFLINE-001"),
        api_key="ondoKeyId_UNIT_TEST_ONLY", api_secret="ondoApiSecret_UNIT_TEST_ONLY",
        base_url_http=f"http://127.0.0.1:{venue.http.server_port}",
        base_url_ws=f"ws://127.0.0.1:{venue.ws_port}/ws", account_read_only=readonly,
        expected_venue_account_id="unit-account", diagnostics_run_id=run_id,
        http_timeout_secs=15, dms_timeout_secs=30, reconcile_interval_secs=1,
        journal_path=str(journal), allow_production_orders=not readonly,
        execution_envelope=envelope,
    )
    node = (
        LiveNode.builder("ONDO-OFFLINE", TraderId.from_str("TESTER-006"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.ERROR))
        .with_timeout_connection(45)
        .with_timeout_disconnection_secs(1 if release == "outer_timeout" else PRODUCTION_TRADE_DISCONNECTION_TIMEOUT_SECS)
        .with_exec_engine_config(LiveExecutionEngineConfig(filter_unclaimed_external_orders=False))
        .add_exec_client(None, factory, config).build()
    )
    handle = node.handle()
    timers: list[threading.Timer] = []

    class Observer(Strategy):
        def on_start(self) -> None:
            timeline["strategy_started_monotonic"] = time.monotonic()
            started.set()
            if release == "crash":
                print(READY_PREFIX + json.dumps({"journal_exists": journal.is_file()}), flush=True)
            else:
                timer = threading.Timer(0.1, handle.stop)
                timer.daemon = True
                timer.start()
                timers.append(timer)

        def on_stop(self) -> None:
            timeline["strategy_stopped_monotonic"] = time.monotonic()
            stopped.set()

    node.add_strategy(Observer())

    def watchdog_stop() -> None:
        watchdog_fired.set()
        handle.stop()

    watchdog = threading.Timer(65, watchdog_stop)
    watchdog.daemon = True
    watchdog.start()
    begin = time.monotonic()
    begin_utc = time.time_ns()
    node_raised = False
    try:
        try:
            node.run()
        except RuntimeError:
            node_raised = True
        # Capture before dispose and again afterwards; Drop must not rewrite dirty to clean
        before_dispose = read_shutdown_diagnostics(factory)
        snapshot = factory.production_trade_snapshot()
        release_report = read_dms_release_diagnostics(factory, run_id=run_id)
        readonly_report = factory.read_only_snapshot()
        cashflows = _native_account_cashflows(factory, run_id)
    finally:
        watchdog.cancel()
        for timer in timers:
            timer.cancel()
        node.dispose()
        venue.close()

    after_dispose = read_shutdown_diagnostics(factory)
    assert started.is_set(), "real LiveNode never started"
    assert stopped.is_set(), "strategy stop callback was not delivered"
    assert not watchdog_fired.is_set(), "watchdog masked lifecycle failure"
    assert not handle.is_running
    assert all(method == "GET" for method, _path in venue.requests), venue.requests
    result = {
        "identity": identity, "scenario": release, "readonly": readonly,
        "elapsed_seconds": time.monotonic() - begin, "node_raised": node_raised,
        "timeline": timeline, "run_started_unix_nanos": begin_utc,
        "run_finished_unix_nanos": time.time_ns(),
        "configured_node_disconnect_secs": 1 if release == "outer_timeout" else PRODUCTION_TRADE_DISCONNECTION_TIMEOUT_SECS,
        "configured_native_shutdown_cap_secs": 15,
        "envelope_cleanup_deadline_unix_nanos": None if readonly else now + 120_000_000_000,
        "requests": venue.requests,
        "frames": venue.frames, "shutdown_before_dispose": before_dispose,
        "shutdown_after_dispose": after_dispose, "release_report": release_report,
        "snapshot": snapshot, "readonly_report": readonly_report,
        "account_cashflows_report": cashflows,
        "journal_exists": journal.is_file(),
    }
    assert PRIVATE_MARKER not in json.dumps(result)
    return result


def _command(scenario: str, journal: Path, readonly: bool = False) -> list[str]:
    return [sys.executable, str(Path(__file__).resolve()), "--ondo-child", scenario,
            str(journal), *( ["--readonly"] if readonly else [])]


def _child_env() -> dict[str, str]:
    import os
    env = _child_environment()
    # Only explicit artifact paths/digests cross into the credential-free child.
    for name in ("ONDO_TEST_WHEEL", "ONDO_TEST_WHEEL_SHA256", "ONDO_TEST_PROVENANCE",
                 "ONDO_TEST_PROVENANCE_SHA256"):
        if name in os.environ:
            env[name] = os.environ[name]
    return env


def _subprocess(scenario: str, journal: Path, readonly: bool = False) -> dict:
    _identity()
    result = subprocess.run(
        _command(scenario, journal, readonly), cwd=ROOT, env=_child_env(),
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=80,
    )
    output = result.stdout + "\n" + result.stderr
    assert result.returncode == 0, output[-10000:]
    records = [line[len(RESULT_PREFIX):] for line in output.splitlines()
               if line.startswith(RESULT_PREFIX)]
    assert len(records) == 1, output[-10000:]
    record = json.loads(records[0])
    import os
    evidence_dir = os.environ.get("ONDO_TEST_EVIDENCE_DIR")
    if evidence_dir:
        output_dir = Path(evidence_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / f"{scenario}-{'readonly' if readonly else 'production'}.json").write_text(
            json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return record


@pytest.mark.parametrize("scenario,clean", [
    ("matching", True), ("history", True), ("delayed", True), ("missing", False),
    ("wrong_channel", False), ("old_arm", False), ("ambiguous", False),
    ("connection_loss", False),
])
def test_installed_ondo_release_ack_controls_clean_shutdown(tmp_path, scenario, clean):
    record = _subprocess(scenario, tmp_path / "journal.json")
    before = record["shutdown_before_dispose"]
    after = record["shutdown_after_dispose"]
    assert record["node_raised"] is (not clean), record
    assert before["available"] is True
    assert before["release"]["attempted"] is True
    assert before["release"]["frame_sent"] is True
    assert before["release"]["acknowledged"] is clean
    assert after["release"]["acknowledged"] is clean
    snapshot = record["snapshot"]
    assert (snapshot["phase"] == "final") is clean
    assert (snapshot["shutdown_status"] == "clean") is clean
    assert snapshot["reconciled_flat"] is True
    assert snapshot["execution_cost"] is None
    cashflows = record["account_cashflows_report"]
    assert cashflows["settlement_currency"] == "USDC"
    assert cashflows["wallet_balance_change_usdc"] == "0.00"
    assert cashflows["margin_balance_change_usdc"] == "0.00"
    assert cashflows["funding_history_reconciled"] is True
    assert cashflows["funding_payments_usdc"] == "0"
    assert cashflows["strategy_pnl_claimed"] is False
    assert record["release_report"]["acknowledged"] is clean
    trace = before["trace"]
    assert trace["shutdown_started_unix_nanos"] <= trace["release_sent_unix_nanos"]
    if clean:
        assert trace["release_sent_unix_nanos"] <= trace["release_ack_observed_unix_nanos"]
        assert trace["release_ack_observed_unix_nanos"] <= trace["checkpoint_finished_unix_nanos"]
    else:
        assert trace["release_ack_observed_unix_nanos"] is None
    frames = [tuple(item) for item in record["frames"] if item[1] == CHANNEL]
    assert frames[-1] == ("unsubscribe", CHANNEL)
    assert record["journal_exists"] is True


def test_installed_ondo_readonly_recovery_never_arms_or_writes(tmp_path):
    record = _subprocess("matching", tmp_path / "journal.json", readonly=True)
    assert record["readonly_report"]["identity_match"] == "matched"
    assert record["readonly_report"]["shutdown_status"] == "complete"
    assert not any(channel == CHANNEL for _op, channel in record["frames"])
    assert record["snapshot"] is None


def test_installed_ondo_outer_timeout_keeps_shutdown_unconfirmed_after_dispose(tmp_path):
    record = _subprocess("outer_timeout", tmp_path / "journal.json")
    assert record["node_raised"] is True
    for name in ("shutdown_before_dispose", "shutdown_after_dispose"):
        diagnostics = record[name]
        assert diagnostics["available"] is True
        assert diagnostics["release"]["acknowledged"] is False
    snapshot = record["snapshot"]
    assert snapshot is None or snapshot.get("phase") != "final"
    assert record["journal_exists"] is True
    assert record["configured_node_disconnect_secs"] == 1
    assert record["shutdown_after_dispose"]["trace"]["shutdown_budget_nanos"] == 15_000_000_000


def test_installed_ondo_process_interruption_then_readonly_recovery(tmp_path):
    _identity()
    journal = tmp_path / "interrupted-journal.json"
    process = subprocess.Popen(
        _command("crash", journal), cwd=ROOT, env=_child_env(),
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    import queue
    lines: queue.Queue[str] = queue.Queue()

    def read_output() -> None:
        for line in process.stdout:
            lines.put(line)

    reader = threading.Thread(target=read_output, daemon=True)
    reader.start()
    deadline = time.monotonic() + 65
    ready = None
    output = []
    try:
        while time.monotonic() < deadline:
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                if process.poll() is not None:
                    break
                continue
            output.append(line)
            if line.startswith(READY_PREFIX):
                ready = json.loads(line[len(READY_PREFIX):])
                break
        assert ready is not None, "".join(output)[-5000:]
        assert ready["journal_exists"] is True
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=10)
        reader.join(5)
        process.stdout.close()
    assert process.returncode != 0
    inherited = journal.read_bytes()
    assert inherited
    recovered = _subprocess("matching", journal, readonly=True)
    assert recovered["readonly_report"]["identity_match"] == "matched"
    assert recovered["readonly_report"]["shutdown_status"] == "complete"
    assert recovered["snapshot"] is None
    assert not any(channel == CHANNEL for _op, channel in recovered["frames"])
    assert all(method == "GET" for method, _path in recovered["requests"])


if __name__ == "__main__" and "--ondo-child" in sys.argv:
    try:
        result = _run_child(sys.argv[2], Path(sys.argv[3]), "--readonly" in sys.argv)
        print(RESULT_PREFIX + json.dumps(result, sort_keys=True), flush=True)
    except BaseException:
        traceback.print_exc()
        raise SystemExit(1)

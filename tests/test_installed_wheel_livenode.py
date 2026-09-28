"""Installed-wheel LiveNode acceptance against a loopback Aster venue.

The test is intentionally skipped when the interpreter does not contain the wheel pinned by
``config/native-candidate.lock.json``.  The regular application venv points at historical
artifacts, so running this file there must not be reported as current-candidate evidence.

The venue is a real HTTP and WebSocket server and the node uses the real Aster factory from the
installed wheel.  No credentials, dotenv file, external network, or order side effect is used.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import subprocess
import sys
import threading
import time
import traceback
from datetime import timedelta
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs
from urllib.parse import unquote
from urllib.parse import urlparse
from urllib.parse import urlsplit

import pytest
from packaging.tags import sys_tags
from websockets.asyncio.server import serve
from websockets.exceptions import ConnectionClosed


ROOT = Path(__file__).resolve().parents[1]
LOCK = ROOT / "config" / "native-candidate.lock.json"
BTC = "BTCUSDT-PERP.ASTER"


def _path_from_file_url(value: str) -> Path:
    """Convert the file URL recorded by wheel direct_url metadata to a Windows path."""
    parsed = urlparse(value)
    if parsed.scheme != "file" or parsed.netloc:
        raise ValueError(f"wheel origin is not a local file URL: {value!r}")
    path = unquote(parsed.path)
    if path.startswith("/") and len(path) >= 3 and path[2] == ":":
        path = path[1:]
    return Path(path)


def _formal_candidate_wheel() -> Path:
    """Return the installed formal candidate or skip this test with an explicit reason."""
    from importlib.metadata import PackageNotFoundError
    from importlib.metadata import distribution

    try:
        dist = distribution("nautilus-trader")
    except PackageNotFoundError:
        pytest.skip("nautilus-trader is not installed; issue #3 needs an installed wheel")

    direct_url = dist.read_text("direct_url.json")
    if not direct_url:
        pytest.skip("installed nautilus-trader has no direct_url.json")
    try:
        wheel = _path_from_file_url(json.loads(direct_url)["url"])
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        pytest.skip(f"installed wheel origin is not a local candidate: {exc}")
    if not wheel.is_file():
        pytest.skip(f"installed wheel source is unavailable: {wheel}")

    lock = json.loads(LOCK.read_text(encoding="utf-8"))
    compatible = [
        artifact
        for artifact in lock["artifacts"]
        if artifact["filename"] == wheel.name
        and set(artifact["tags"]).intersection(str(tag) for tag in sys_tags())
    ]
    if len(compatible) != 1:
        pytest.skip("installed wheel is not the platform artifact in native-candidate.lock.json")

    expected = compatible[0]["sha256"]
    actual = hashlib.sha256(wheel.read_bytes()).hexdigest()
    if actual != expected:
        pytest.skip(
            "installed wheel is a different candidate; "
            f"expected {expected}, got {actual}",
        )
    return wheel


EXCHANGE_INFO = {
    "timezone": "UTC",
    "serverTime": 1_788_571_663_397,
    "rateLimits": [],
    "exchangeFilters": [],
    "assets": [],
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "pair": "BTCUSDT",
            "contractType": "PERPETUAL",
            "deliveryDate": 4_133_404_800_000,
            "onboardDate": 1_569_398_400_000,
            "status": "TRADING",
            "maintMarginPercent": "2.5000",
            "requiredMarginPercent": "5.0000",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "marginAsset": "USDT",
            "pricePrecision": 2,
            "quantityPrecision": 3,
            "baseAssetPrecision": 8,
            "quotePrecision": 8,
            "underlyingType": "COIN",
            "underlyingSubType": [],
            "settlePlan": 0,
            "triggerProtect": "0.0500",
            "filters": [
                {
                    "filterType": "PRICE_FILTER",
                    "minPrice": "0.10",
                    "maxPrice": "1000000",
                    "tickSize": "0.10",
                },
                {
                    "filterType": "LOT_SIZE",
                    "minQty": "0.001",
                    "maxQty": "1000",
                    "stepSize": "0.001",
                },
                {"filterType": "MIN_NOTIONAL", "notional": "5"},
            ],
            "orderTypes": ["LIMIT", "MARKET"],
            "timeInForce": ["GTC", "IOC", "FOK", "GTX"],
        },
    ],
}


class LoopbackAster:
    """A tiny loopback Aster HTTP/WS server for the real execution client."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.requests: list[tuple[str, str]] = []
        self.connections = 0
        self._sockets: set[object] = set()
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        venue = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args: object) -> None:
                return

            def do_GET(self) -> None:  # noqa: N802
                self._answer()

            def do_POST(self) -> None:  # noqa: N802
                self._answer()

            def do_PUT(self) -> None:  # noqa: N802
                self._answer()

            def do_DELETE(self) -> None:  # noqa: N802
                self._answer()

            def _answer(self) -> None:
                parsed = urlsplit(self.path)
                endpoint = parsed.path.rsplit("/", 1)[-1]
                params = parse_qs(parsed.query)
                length = int(self.headers.get("Content-Length", "0"))
                if length:
                    self.rfile.read(length)
                with venue.lock:
                    venue.requests.append((self.command, endpoint))

                if endpoint == "exchangeInfo":
                    body = EXCHANGE_INFO
                elif endpoint == "dual":
                    body = {"dualSidePosition": False}
                elif endpoint == "commissionRate":
                    body = {
                        "symbol": params.get("symbol", ["BTCUSDT"])[0],
                        "makerCommissionRate": "0.00005",
                        "takerCommissionRate": "0.0004",
                    }
                elif endpoint == "balance":
                    body = [
                        {
                            "accountAlias": "issue3-loopback",
                            "asset": "USDT",
                            "balance": "100",
                            "crossWalletBalance": "100",
                            "availableBalance": "100",
                            "maxWithdrawAmount": "100",
                            "marginAvailable": True,
                            "updateTime": 1_788_571_663_397,
                        },
                    ]
                elif endpoint == "positionRisk":
                    body = []
                elif endpoint in {"openOrders", "allOrders", "userTrades"}:
                    body = []
                elif endpoint == "listenKey":
                    body = {"listenKey": "issue3-loopback"}
                elif endpoint == "order" and self.command == "POST":
                    # The strategy never submits an order. Keep this response deterministic if
                    # a future lifecycle change accidentally reaches the venue.
                    body = {"code": -2010, "msg": "issue3 test forbids orders"}
                elif endpoint == "order" and self.command == "GET":
                    body = {"code": -2013, "msg": "Order does not exist."}
                else:
                    body = {}

                payload = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.http_thread = threading.Thread(
            target=self.http.serve_forever,
            name="issue3-loopback-http",
            daemon=True,
        )
        self.http_thread.start()

        async def websocket(socket) -> None:
            with venue.lock:
                venue.connections += 1
            venue._sockets.add(socket)
            try:
                async for _ in socket:
                    pass
            except ConnectionClosed:
                pass
            finally:
                venue._sockets.discard(socket)

        async def start() -> None:
            self.ws = await serve(websocket, "127.0.0.1", 0)
            self.ws_port = self.ws.sockets[0].getsockname()[1]
            self._ready.set()

        def run_loop() -> None:
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(start())
            self._loop.run_forever()

        self.ws_thread = threading.Thread(
            target=run_loop,
            name="issue3-loopback-ws",
            daemon=True,
        )
        self.ws_thread.start()
        assert self._ready.wait(5), "loopback websocket did not start"

    @property
    def http_url(self) -> str:
        return f"http://127.0.0.1:{self.http.server_port}"

    @property
    def ws_url(self) -> str:
        return f"ws://127.0.0.1:{self.ws_port}"

    def close_sockets(self) -> None:
        async def close() -> None:
            for socket in tuple(self._sockets):
                await socket.close()

        asyncio.run_coroutine_threadsafe(close(), self._loop).result(5)

    def close(self) -> None:
        try:
            self.close_sockets()
        finally:
            self.ws.close()
            asyncio.run_coroutine_threadsafe(self.ws.wait_closed(), self._loop).result(5)
            self._loop.call_soon_threadsafe(self._loop.stop)
            self.ws_thread.join(timeout=5)
            self.http.shutdown()
            self.http.server_close()
            self.http_thread.join(timeout=5)


class LifecycleStrategyMixin:
    """Behavior mixed into the native Strategy class after the wheel is imported."""

    def initialize_issue3(self, venue: LoopbackAster) -> None:
        self.issue3_venue = venue
        self.issue3_started = threading.Event()
        self.issue3_fault = threading.Event()
        self.issue3_error: BaseException | None = None
        self.issue3_timers: list[threading.Timer] = []
        self.issue3_handle = None
        self.issue3_reporter = None
        self.issue3_report: list[str] = []
        self.issue3_stopped = threading.Event()

    def on_start(self) -> None:
        self.issue3_started.set()
        self.clock.set_timer(
            "issue3-application-report",
            timedelta(milliseconds=200),
            callback=self._issue3_application_report,
        )
        timer = threading.Timer(0.5, self._issue3_disconnect)
        timer.daemon = True
        timer.start()
        self.issue3_timers.append(timer)

    def _issue3_application_report(self, _event) -> None:
        try:
            self.issue3_report = self.issue3_reporter(self)
        except BaseException as exc:  # pragma: no cover - only reports a native callback error
            self.issue3_error = exc

    def _issue3_disconnect(self) -> None:
        try:
            self.issue3_venue.close_sockets()
            self.issue3_fault.set()
            stop = threading.Timer(0.2, self.issue3_handle.stop)
            stop.daemon = True
            stop.start()
            self.issue3_timers.append(stop)
        except BaseException as exc:  # pragma: no cover - only reports teardown failures
            self.issue3_error = exc

    def on_stop(self) -> None:
        for timer in self.issue3_timers:
            timer.cancel()
        self.issue3_stopped.set()


CHILD_ARGUMENT = "--issue3-child"
CHILD_RESULT_PREFIX = "ISSUE3_RESULT "


def _run_livenode_child() -> dict[str, object]:
    """Drive a real installed candidate through connect, WS fault, stop, and dispose."""
    wheel = _formal_candidate_wheel()

    from nautilus_trader.adapters.aster import AsterEnvironment
    from nautilus_trader.adapters.aster import AsterExecutionClientConfig
    from nautilus_trader.adapters.aster import AsterExecutionClientFactory
    from nautilus_trader.adapters.binance import BinanceInstrumentProviderConfig
    from nautilus_trader.common import Environment
    from nautilus_trader.common import LoggerConfig
    from nautilus_trader.common import LogLevel
    from nautilus_trader.live import LiveExecutionEngineConfig
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import InstrumentId
    from nautilus_trader.model import TraderId
    from nautilus_trader.trading import Strategy

    class LifecycleStrategy(LifecycleStrategyMixin, Strategy):
        pass

    def report_application(strategy: LifecycleStrategy) -> list[str]:
        """Consume the native portfolio through the existing application reporter."""
        source = str(ROOT / "src")
        if source not in sys.path:
            sys.path.insert(0, source)
        import dotenv

        original_load_dotenv = dotenv.load_dotenv
        # ``maker_live`` normally loads a project .env at import time. Patch that call while
        # importing the application module so this acceptance test cannot inspect credentials.
        dotenv.load_dotenv = lambda *_args, **_kwargs: False
        try:
            from maker_live import LighterMaker
        finally:
            dotenv.load_dotenv = original_load_dotenv

        output: list[str] = []
        proxy = SimpleNamespace(
            _maker_id=InstrumentId.from_str(BTC),
            _hedge_inst=None,
            portfolio=strategy.portfolio,
            cache=strategy.cache,
            _marks=lambda: (50000.0, 50000.0),
            _log_safe=lambda _level, message: output.append(message),
        )
        LighterMaker._report_accounts(proxy)
        return output

    venue = LoopbackAster()
    node = None
    strategy = None
    handle = None
    watchdog_fired = threading.Event()
    run_started = time.monotonic()
    run_elapsed = None
    try:
        config = AsterExecutionClientConfig(
            environment=AsterEnvironment.TESTNET,
            signer_private_key=hashlib.sha256(b"issue3 offline signer").hexdigest(),
            base_url_http=venue.http_url,
            base_url_ws=venue.ws_url,
            instrument_provider=BinanceInstrumentProviderConfig(
                load_all=False,
                load_ids=[BTC],
            ),
        )
        node = (
            LiveNode.builder(
                "ISSUE3-LIVE-NODE",
                TraderId.from_str("TESTER-003"),
                Environment.LIVE,
            )
            .with_logging(LoggerConfig(stdout_level=LogLevel.ERROR))
            .with_exec_engine_config(
                LiveExecutionEngineConfig(filter_unclaimed_external_orders=False),
            )
            .add_exec_client(None, AsterExecutionClientFactory(), config)
            .build()
        )
        strategy = LifecycleStrategy()
        strategy.initialize_issue3(venue)
        strategy.issue3_reporter = report_application
        node.add_strategy(strategy)
        handle = node.handle()
        strategy.issue3_handle = handle

        def watchdog_stop() -> None:
            watchdog_fired.set()
            handle.stop()

        watchdog = threading.Timer(20, watchdog_stop)
        watchdog.daemon = True
        watchdog.start()
        node.run()
        run_elapsed = time.monotonic() - run_started
    finally:
        if node is not None:
            if "watchdog" in locals():
                watchdog.cancel()
            node.dispose()
        venue.close()

    assert run_elapsed is not None, "the native LiveNode did not return from run()"
    assert not watchdog_fired.is_set(), "the watchdog masked a failed native lifecycle"
    assert run_elapsed < 15, f"normal shutdown exceeded the bounded test window: {run_elapsed:.3f}s"
    assert strategy is not None
    assert handle is not None
    assert strategy.issue3_started.is_set(), "the real LiveNode never started the strategy"
    assert strategy.issue3_stopped.is_set(), "the native Strategy stop callback did not run"
    assert any("free 100" in line for line in strategy.issue3_report), strategy.issue3_report
    assert strategy.issue3_fault.is_set(), "the loopback WS fault was not observed"
    assert strategy.issue3_error is None, repr(strategy.issue3_error)
    assert venue.connections >= 1
    assert ("POST", "listenKey") in venue.requests
    assert ("DELETE", "listenKey") in venue.requests
    assert not any(endpoint == "order" for _method, endpoint in venue.requests)
    assert not handle.is_running

    return {
        "wheel_sha256": hashlib.sha256(wheel.read_bytes()).hexdigest(),
        "run_elapsed_seconds": run_elapsed,
        "watchdog_fired": watchdog_fired.is_set(),
        "started": strategy.issue3_started.is_set(),
        "stopped": strategy.issue3_stopped.is_set(),
        "ws_fault": strategy.issue3_fault.is_set(),
        "connections": venue.connections,
        "requests": venue.requests,
        "application_report": strategy.issue3_report,
    }


def _child_environment() -> dict[str, str]:
    """Run the child without venue credentials or dotenv discovery."""
    environment = os.environ.copy()
    prefixes = (
        "ASTER_",
        "BINANCE_",
        "CONDA_",
        "FUTU_",
        "HYPERLIQUID_",
        "LIGHTER_",
        "ONDO_",
    )
    suffixes = ("_API_KEY", "_API_SECRET", "_PASSWORD", "_PRIVATE_KEY", "_TOKEN")
    for name in tuple(environment):
        if name.startswith(prefixes) or name.endswith(suffixes):
            environment.pop(name, None)
    environment["PYTHON_DOTENV_DISABLED"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    environment.pop("PYTHONPATH", None)
    return environment


def _run_livenode_subprocess(wheel: Path) -> dict[str, object]:
    """Run native imports in a fresh process so earlier LiveNode tests cannot poison them."""
    command = [sys.executable, str(Path(__file__).resolve()), CHILD_ARGUMENT]
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            env=_child_environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=40,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"issue #3 LiveNode child timed out: {exc}")

    output = result.stdout + "\n" + result.stderr
    records = [
        line.removeprefix(CHILD_RESULT_PREFIX)
        for line in output.splitlines()
        if line.startswith(CHILD_RESULT_PREFIX)
    ]
    assert result.returncode == 0, (
        f"issue #3 LiveNode child failed with exit {result.returncode}\n{output[-12000:]}"
    )
    assert records, f"child did not emit a machine-readable result\n{output[-12000:]}"
    record = json.loads(records[-1])
    assert record["wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    return record


def test_installed_wheel_livenode_loopback_lifecycle() -> None:
    """Run the real installed-wheel lifecycle in a clean child interpreter."""
    wheel = _formal_candidate_wheel()
    record = _run_livenode_subprocess(wheel)
    assert record["started"] is True
    assert record["stopped"] is True
    assert record["ws_fault"] is True
    assert record["watchdog_fired"] is False
    assert any("free 100" in line for line in record["application_report"])
    assert ("POST", "listenKey") in [tuple(item) for item in record["requests"]]
    assert ("DELETE", "listenKey") in [tuple(item) for item in record["requests"]]


def _child_main() -> int:
    """Entry point for the subprocess-only native lifecycle driver."""
    try:
        record = _run_livenode_child()
    except BaseException:
        traceback.print_exc()
        return 1
    print(CHILD_RESULT_PREFIX + json.dumps(record, sort_keys=True), flush=True)
    return 0


if __name__ == "__main__" and CHILD_ARGUMENT in sys.argv:
    raise SystemExit(_child_main())

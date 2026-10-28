"""BTC/ETH cancel-first maker. Default invocation validates without network or credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import signal
import sys
import time
import urllib.request
import uuid
from decimal import Decimal
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ondo_maker import MARKETS, NATIVE_UNPACED_REQUEST_BOUND, SECOND, MakerConfig

BINANCE_INFO = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_WS = "wss://fstream.binance.com/public/stream?streams=btcusdt@bookTicker/ethusdt@bookTicker"


def check_binance_contracts():
    with urllib.request.urlopen(BINANCE_INFO, timeout=10) as response:
        raw = response.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("Binance metadata exceeds size bound")
    contracts = {s["symbol"]: s for s in json.loads(raw)["symbols"]}
    for market in MARKETS:
        item = contracts.get(market + "USDT", {})
        if not (
            item.get("status") == "TRADING"
            and item.get("contractType") == "PERPETUAL"
            and item.get("baseAsset") == market
            and item.get("quoteAsset") == "USDT"
        ):
            raise ValueError("Binance perpetual metadata mismatch")


async def binance_feed(strategy, finished):
    from websockets.asyncio.client import connect

    delay = 0.25
    while not finished.is_set():
        strategy.binance_disconnect()
        try:
            await asyncio.to_thread(check_binance_contracts)
            async with connect(
                BINANCE_WS,
                max_queue=16,
                max_size=65_536,
                open_timeout=10,
                ping_interval=10,
                ping_timeout=3,
                close_timeout=1,
            ) as socket:
                previous = {}
                delay = 0.25
                async for raw in socket:
                    received = time.monotonic_ns()
                    data = json.loads(raw).get("data", {})
                    market = data.get("s", "").removesuffix("USDT")
                    if market not in MARKETS or data.get("e") != "bookTicker":
                        continue
                    identifier = int(data["u"])
                    old = previous.get(market, -1)
                    if identifier == old:
                        continue
                    if identifier < old:
                        raise ValueError("Binance sequence regressed")
                    age = time.time_ns() - int(data["E"]) * 1_000_000
                    if not -SECOND <= age <= 2 * SECOND:
                        raise ValueError("Binance stale event")
                    previous[market] = identifier
                    if Decimal(data["B"]) <= 0 or Decimal(data["A"]) <= 0:
                        raise ValueError("Binance invalid size")
                    strategy.binance_quote(
                        market, received, float(data["b"]), float(data["a"])
                    )
                    if finished.is_set():
                        return
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - a failed callback must not stop cleanup
            strategy.binance_disconnect()
            try:
                await asyncio.wait_for(finished.wait(), delay)
            except TimeoutError:
                pass
            delay = min(delay * 2, 5)
        finally:
            strategy.binance_disconnect()


async def serve(node, strategy, *, seconds=0):
    """Signals request draining; only proven flatness stops the native node."""
    finished = asyncio.Event()
    handle = node.handle()
    loop = asyncio.get_running_loop()
    previous = {}
    for signum in (signal.SIGINT, signal.SIGTERM):
        previous[signum] = signal.getsignal(signum)
        signal.signal(
            signum, lambda *_args: loop.call_soon_threadsafe(strategy.request_stop)
        )
    native = asyncio.ensure_future(node.run_async())
    reference = asyncio.create_task(binance_feed(strategy, finished))
    started, reported = time.monotonic(), 0.0
    try:
        while not native.done():
            now = time.monotonic()
            if seconds and now - started >= seconds:
                strategy.request_stop()
            if strategy.engine.can_stop:
                handle.stop()
                break
            if now - reported >= 5:
                status = json.dumps(strategy.status(), sort_keys=True)
                try:
                    await asyncio.to_thread(print, status, flush=True)
                except OSError:
                    strategy.request_stop()
                reported = now
            await asyncio.sleep(0.05)
        await native
    finally:
        finished.set()
        reference.cancel()
        await asyncio.gather(reference, return_exceptions=True)
        for signum, handler in previous.items():
            signal.signal(signum, handler)
    return 0 if strategy.engine.can_stop else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--btc-usd", type=Decimal, required=True, help="USD per BTC side"
    )
    parser.add_argument(
        "--eth-usd", type=Decimal, required=True, help="USD per ETH side"
    )
    parser.add_argument("--gross-usd", type=Decimal, default=Decimal(400))
    parser.add_argument(
        "--pause-both", action="store_true", help="Binance trigger cancels both markets"
    )
    parser.add_argument(
        "--live", action="store_true", help="enable native production execution"
    )
    parser.add_argument(
        "--seconds", type=float, default=0, help="request draining after this duration"
    )
    args = parser.parse_args()
    config = MakerConfig(
        args.btc_usd,
        args.eth_usd,
        max_order_usd=max(Decimal(100), args.btc_usd, args.eth_usd),
        max_gross_usd=args.gross_usd,
        ondo_stale_ns=3 * SECOND,
        pause_both=args.pause_both,
    )
    if args.seconds < 0 or not __import__("math").isfinite(args.seconds):
        parser.error("seconds must be finite and nonnegative")
    if not args.live:
        print(
            json.dumps(
                {
                    "mode": "validation_only",
                    "network": False,
                    "credentials": False,
                    "btc_usd_per_side": str(args.btc_usd),
                    "eth_usd_per_side": str(args.eth_usd),
                    "max_gross_usd": str(config.max_gross_usd),
                    "quote_bps": str(config.quote_bps),
                    "cancel_bps": str(config.cancel_bps),
                    "priority": "cancel_then_close",
                    "binance_pause": "both" if args.pause_both else "per_market",
                    "startup_test_trades": False,
                },
                indent=2,
            )
        )
        return 0
    from dotenv import load_dotenv

    from ondo_continuous import ContinuousLimits, ContinuousSession, build_session
    from ondo_maker_live import make_strategy

    env_file = ROOT / ".env"
    if not env_file.is_file():
        raise ValueError("repository .env is missing")
    load_dotenv(env_file, override=False)
    required = (
        "ONDO_MAINNET_API_KEY",
        "ONDO_MAINNET_API_SECRET",
        "ONDO_MAINNET_ACCOUNT_ID",
    )
    if any(not os.environ.get(name) for name in required):
        raise ValueError("required ONDO_MAINNET credentials are missing")
    run_id = "mm" + uuid.uuid4().hex[:20]
    journal = ROOT / "reports" / "live" / "ondo-maker" / run_id / "native-journal.jsonl"
    journal.parent.mkdir(parents=True, exist_ok=False)
    identity = os.environ["ONDO_MAINNET_ACCOUNT_ID"]
    session = ContinuousSession(
        instrument_ids=tuple(f"{m}-USD-PERP.ONDO" for m in MARKETS),
        account_id="ONDO-" + identity,
        environment="production",
        execution_enabled=True,
        expected_venue_account_id=identity,
        diagnostics_run_id=run_id,
        journal_path=journal,
        dms_timeout_secs=10,
        limits=ContinuousLimits(
            max_notional_per_order_usd=str(config.max_order_usd),
            max_gross_exposure_usd=str(config.max_gross_usd),
            max_live_orders=6,
            max_requests_per_second=NATIVE_UNPACED_REQUEST_BOUND,
            max_unresolved_orders=4,
        ),
    )
    node, _factory, strategy = build_session(
        session, strategy_factory=partial(make_strategy, maker=config, run_id=run_id)
    )
    return asyncio.run(serve(node, strategy, seconds=args.seconds))


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as e:  # noqa: BLE001 - keep cleanup alive; never print SDK payloads
        # Raw native errors may contain authenticated response data.
        print(f"Ondo maker error: {type(e).__name__}", file=sys.stderr)
        raise SystemExit(2) from None

"""Read-only account and public-feed check; native order execution is disabled."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


async def probe(seconds):
    from dotenv import dotenv_values
    from nautilus_trader.model import AccountId, InstrumentId
    from nautilus_trader.trading import Strategy

    from ondo_continuous import ContinuousSession, ContinuousStrategyConfig, build_session
    from run_ondo_maker import binance_feed

    values = dotenv_values(ROOT / ".env", interpolate=False)
    names = ("ONDO_MAINNET_API_KEY", "ONDO_MAINNET_API_SECRET", "ONDO_MAINNET_ACCOUNT_ID")
    if any(not values.get(name) for name in names):
        raise ValueError("required credentials missing")
    key, secret, account = (values[name] for name in names)
    ids = tuple(f"{market}-USD-PERP.ONDO" for market in ("BTC", "ETH"))
    session = ContinuousSession(
        instrument_ids=ids,
        account_id="ONDO-" + account,
        environment="production",
        execution_enabled=False,
        expected_venue_account_id=account,
        api_key=key,
        api_secret=secret,
    )

    class Observer(Strategy):
        def __init__(self, config):
            super().__init__(config)
            self.ondo = {name: 0 for name in ids}
            self.binance = {market: 0 for market in ("BTC", "ETH")}
            self.binance_connected = False

        def on_start(self):
            for name in ids:
                self.subscribe_quotes(InstrumentId.from_str(name))

        def on_quote(self, quote):
            self.ondo[str(quote.instrument_id)] += 1

        def binance_quote(self, market, *_args):
            self.binance[market] += 1
            self.binance_connected = True

        def binance_disconnect(self):
            self.binance_connected = False

    def make_observer(_session, _factory):
        return Observer(ContinuousStrategyConfig(
            instrument_id=InstrumentId.from_str(ids[0]),
            account_id=AccountId.from_str(session.account_id),
        ))

    node, factory, strategy = build_session(session, strategy_factory=make_observer)
    handle = node.handle()
    finished = asyncio.Event()
    native = asyncio.ensure_future(node.run_async())
    reference = asyncio.create_task(binance_feed(strategy, finished))
    began = time.monotonic()
    snapshot = {}
    try:
        while not native.done() and time.monotonic() - began < seconds:
            snapshot = factory.read_only_snapshot() or {}
            await asyncio.sleep(0.1)
        result = {
            "mode": "native_account_read_only",
            "execution_enabled": False,
            "order_commands": 0,
            "logged_in": snapshot.get("logged_in", False),
            "identity_match": snapshot.get("identity_match"),
            "run_state": snapshot.get("run_state"),
            "subscriptions_acked": snapshot.get("subscriptions_acked"),
            "account_state_events": snapshot.get("account_state_events", 0),
            "ondo_quotes": strategy.ondo,
            "binance_quotes": strategy.binance,
            "binance_connected": strategy.binance_connected,
        }
    finally:
        finished.set()
        reference.cancel()
        await asyncio.gather(reference, return_exceptions=True)
        handle.stop()
        await native
    result["ready"] = bool(
        result["logged_in"]
        and result["identity_match"] == "matched"
        and result["run_state"] == "read_only_synced"
        and all((result["subscriptions_acked"] or {}).values())
        and result["account_state_events"]
        and all(result["ondo_quotes"].values())
        and all(result["binance_quotes"].values())
        and result["binance_connected"]
    )
    print(json.dumps(result, indent=2))
    return 0 if result["ready"] else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=int, default=30)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 120:
        parser.error("seconds must be within 5..120")
    try:
        raise SystemExit(asyncio.run(probe(args.seconds)))
    except Exception as error:
        print(json.dumps({"ready": False, "error_type": type(error).__name__}))
        raise SystemExit(1) from None

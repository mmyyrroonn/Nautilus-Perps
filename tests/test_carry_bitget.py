"""Comprehensive unit, loopback WebSocket, and strategy integration tests for Bitget Classic v2."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import replace
from decimal import Decimal
import hashlib
import hmac
import http.client
import json
import os
from pathlib import Path
import queue
import threading
import time
from types import SimpleNamespace
from typing import Any, Callable

import pytest
import websockets

from src.carry.account_reader import CarryStartupAccountReader, StartupSnapshotError, VenueAccountSnapshot
from src.carry.bitget_gateway import (
    BitgetGatewayEvent,
    BitgetPreflightError,
    BitgetPublicQuoteFeed,
    BitgetTransportError,
    BitgetVenueGateway,
    RoutingVenueGateway,
    _BitgetKeepAliveRestClient,
    bitget_instrument_id,
    bitget_raw_symbol,
    bitget_rest_headers,
    bitget_ws_login_payload,
    is_bitget_uta_error,
    parse_bitget_contracts,
)
from src.carry.config import CarryPolicy, load_carry_plan
from src.carry.executor import CarryExecutionController, OrderAck
from src.carry.funding_feed import HOUR, LegMarketInfo, PublicCarryFeed, SlowPollError, parse_bitget_tickers
from src.carry.ledger import CarryLedger
from src.carry.runtime import _create_carry_strategy, build_data_clients, build_execution_clients
from src.carry.signal import (
    InstrumentSpec,
    _cex_entry_blackout_hit,
    evaluate_entry_candidates,
    should_exit_carry_decay,
)
from tests.test_carry_mvp import DelayedAsyncVenueGateway, _make_leg

D = Decimal
REPO_ROOT = Path(__file__).resolve().parents[1]


def _wait_until(pred: Callable[[], bool], *, timeout: float = 3.0, interval: float = 0.01) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        time.sleep(interval)
    raise AssertionError(f"condition not met within {timeout}s")


class _LoopbackWsServer:
    """Thread-hosted local websockets==17.1 server for deterministic Bitget WS integration tests."""

    def __init__(self, on_message: Callable[[Any, str | bytes], Any] | None = None) -> None:
        self.on_message = on_message
        self.received_messages: list[str | bytes] = []
        self.received_pings: int = 0
        self.connection_count: int = 0
        self._connections: set[Any] = set()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Any = None
        self._stop_evt: asyncio.Event | None = None
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.port: int = 0

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.port}"

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="test-ws-loopback", daemon=True)
        self._thread.start()
        assert self._ready.wait(timeout=3.0)

    def stop(self) -> None:
        loop = self._loop
        if loop is not None and not loop.is_closed() and self._stop_evt is not None:
            loop.call_soon_threadsafe(self._stop_evt.set)
        if self._thread is not None:
            self._thread.join(timeout=3.0)

    def send_json(self, obj: dict[str, Any]) -> None:
        raw = json.dumps(obj, separators=(",", ":"))
        loop = self._loop
        assert loop is not None
        async def _broadcast() -> None:
            for ws in list(self._connections):
                await ws.send(raw)

        asyncio.run_coroutine_threadsafe(_broadcast(), loop).result(timeout=2.0)

    def kick_all(self) -> None:
        loop = self._loop
        assert loop is not None
        async def _close_all() -> None:
            for ws in list(self._connections):
                await ws.close(code=1012, reason="test_restart")

        asyncio.run_coroutine_threadsafe(_close_all(), loop).result(timeout=2.0)

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve(loop))
        finally:
            loop.close()

    async def _serve(self, loop: asyncio.AbstractEventLoop) -> None:
        self._stop_evt = asyncio.Event()

        async def _handler(ws: Any) -> None:
            self.connection_count += 1
            self._connections.add(ws)
            try:
                async for msg in ws:
                    if msg == "ping":
                        self.received_pings += 1
                        await ws.send("pong")
                        continue
                    self.received_messages.append(msg)
                    if self.on_message is not None:
                        res = self.on_message(ws, msg)
                        if asyncio.iscoroutine(res):
                            await res
            except Exception:
                pass
            finally:
                self._connections.discard(ws)

        self._server = await websockets.serve(_handler, "127.0.0.1", 0)
        self.port = int(self._server.sockets[0].getsockname()[1])
        self._ready.set()
        await self._stop_evt.wait()
        for ws in list(self._connections):
            try:
                await ws.close()
            except Exception:
                pass
        self._server.close()
        await self._server.wait_closed()


def test_four_venue_fingerprint_invariance_and_bitget_config(tmp_path: Path) -> None:
    paper_plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")
    live_plan = load_carry_plan(REPO_ROOT / "config" / "carry-live.toml")

    # Cross-platform fingerprint invariance checks using relative/tmp_path-normalized paths
    p_copy = tmp_path / "carry-paper.toml"
    p_copy.write_text((REPO_ROOT / "config" / "carry-paper.toml").read_text(encoding="utf-8"), encoding="utf-8")
    l_copy = tmp_path / "carry-live.toml"
    l_copy.write_text((REPO_ROOT / "config" / "carry-live.toml").read_text(encoding="utf-8"), encoding="utf-8")
    p_loaded = load_carry_plan(p_copy)
    l_loaded = load_carry_plan(l_copy)
    assert dict(p_loaded.policy.maker_fee_bps_by_venue).keys() == {"BYBIT", "HL", "LIGHTER", "OKX"}
    assert dict(l_loaded.policy.taker_fee_bps_by_venue).keys() == {"BYBIT", "HL", "LIGHTER", "OKX"}
    assert paper_plan.venues == p_loaded.venues == ("HL", "LIGHTER", "OKX", "BYBIT")
    assert live_plan.venues == l_loaded.venues == ("HL", "LIGHTER", "OKX", "BYBIT")
    # Fingerprint on identical relative/platform paths matches a plan loaded independently
    norm_paper = replace(paper_plan, state_path=Path("reports/carry/paper.sqlite3"))
    norm_paper_copy = replace(p_loaded, state_path=Path("reports/carry/paper.sqlite3"))
    assert norm_paper.fingerprint() == norm_paper_copy.fingerprint()
    if os.name != "nt":
        assert (
            replace(paper_plan, state_path=Path("/Volumes/Expansion/perpsarb/Nautilus-Perps/reports/carry/paper.sqlite3")).fingerprint()
            == "8247c775c8ac8598f85d92bcc74bfeca8cbac36113e758aa8f70d5b3761ba49e"
        )
        assert (
            replace(live_plan, state_path=Path("/Volumes/Expansion/perpsarb/Nautilus-Perps/reports/carry/live.sqlite3")).fingerprint()
            == "d90851835399132a5d8631b0957970704301853e4c3b146ff5d4497eb274571c"
        )
    for p in (paper_plan, live_plan):
        assert "BITGET" not in dict(p.policy.maker_fee_bps_by_venue)
        assert "BITGET" not in dict(p.policy.taker_fee_bps_by_venue)

    bg_plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper-bitget.toml")
    assert bg_plan.venues == ("HL", "LIGHTER", "OKX", "BYBIT", "BITGET")
    assert bg_plan.policy.route_scope == "ALL"
    assert bg_plan.state_path.name == "paper-bitget.sqlite3"
    assert bg_plan.runtime.record_dir is not None and bg_plan.runtime.record_dir.name == "tape-paper-bitget"
    assert dict(bg_plan.policy.maker_fee_bps_by_venue)["BITGET"] == D("2.0")
    assert dict(bg_plan.policy.taker_fee_bps_by_venue)["BITGET"] == D("6.0")

    # Default CarryPolicy() resolves BITGET without KeyError via fallback maps
    default_pol = CarryPolicy()
    assert default_pol.fee_pair_rt_bps("HL", "BITGET") == D("2") * (D("1.5") + D("6.0")) + D("8")
    assert default_pol.fee_pair_rt_bps("BITGET", "OKX") == D("2") * (D("2.0") + D("5.0")) + D("4")

    # Signal routing ALL & CEX multi-hour blackout/deferral vs 1h Bitget contracts
    now_s = 1_800_000_000
    bg_4h_pay = LegMarketInfo(
        venue="BITGET", symbol="TSLA", raw_symbol="TSLAUSDT", instrument_id="TSLAUSDT-PERP.BITGET",
        asset_class="RWA", interval_h=4, next_settle_ts=now_s + 600, r8_bps_h=2.0, r24_bps_h=2.0,
        current_rate_bps_h=-1.5, mark_price=250.0, prev_1h_price=250.0, bid=249.9, ask=250.1,
        vol24_usd=5_000_000.0, vol1h_usd=200_000.0, fetched_ts=now_s,
    )
    hl_rwa = _make_leg("HL", "TSLA", asset_class="RWA", r8=0.0, r24=0.0, bid=249.5, ask=249.7, prev_1h=250.0)
    assert _cex_entry_blackout_hit(hl_rwa, bg_4h_pay, bg_plan.policy, now_s) is True

    bg_1h_pay = LegMarketInfo(**{**bg_4h_pay.__dict__, "interval_h": 1})
    assert _cex_entry_blackout_hit(hl_rwa, bg_1h_pay, bg_plan.policy, now_s) is False

    bg_8h_recv = LegMarketInfo(**{**bg_4h_pay.__dict__, "interval_h": 8, "r8_bps_h": 0.1, "current_rate_bps_h": 1.5})
    do_exit, rsn = should_exit_carry_decay(
        opened_ts=now_s - 8 * HOUR, leg_l=hl_rwa, leg_s=bg_8h_recv, policy=bg_plan.policy, now_s=now_s
    )
    assert not do_exit and rsn == "defer_collect_cex_settlement"

    bg_ok_entry = LegMarketInfo(**{**bg_4h_pay.__dict__, "next_settle_ts": now_s + 3 * HOUR, "current_rate_bps_h": 2.0, "bid": 250.3, "ask": 250.5})
    cands = evaluate_entry_candidates(
        {"TSLA": {"HL": hl_rwa, "BITGET": bg_ok_entry}},
        open_symbols=set(), venue_notional_used={}, excluded_symbols=set(),
        policy=bg_plan.policy, risk=bg_plan.risk, now_s=now_s,
    )
    assert len(cands) == 1 and cands[0].short_venue == "BITGET" and cands[0].route_cat == "DEX-CEX"


def test_bitget_signing_vectors_and_contract_parser() -> None:
    hdrs = bitget_rest_headers(
        api_key="k1",
        api_secret="sec1",
        api_passphrase="pass1",
        method="post",
        request_path_and_query="/api/v2/mix/order/place-order",
        body_str='{"symbol":"BTCUSDT"}',
        environment="mainnet",
        ts_ms="1700000000123",
    )
    expected_rest_sig = base64.b64encode(
        hmac.new(b"sec1", b'1700000000123POST/api/v2/mix/order/place-order{"symbol":"BTCUSDT"}', hashlib.sha256).digest()
    ).decode("ascii")
    assert hdrs["ACCESS-KEY"] == "k1"
    assert hdrs["ACCESS-PASSPHRASE"] == "pass1"
    assert hdrs["ACCESS-TIMESTAMP"] == "1700000000123"
    assert hdrs["ACCESS-SIGN"] == expected_rest_sig
    assert "paptrading" not in hdrs

    demo_hdrs = bitget_rest_headers(
        api_key="k1", api_secret="sec1", api_passphrase="pass1",
        method="GET", request_path_and_query="/api/v2/mix/account/account?symbol=BTCUSDT",
        environment="testnet", ts_ms=1700000000000,
    )
    assert demo_hdrs["paptrading"] == "1"

    ws_login = bitget_ws_login_payload(api_key="k1", api_secret="sec1", api_passphrase="pass1", ts_sec="1700000000")
    expected_ws_sig = base64.b64encode(
        hmac.new(b"sec1", b"1700000000GET/user/verify", hashlib.sha256).digest()
    ).decode("ascii")
    assert ws_login == {
        "op": "login",
        "args": [{"apiKey": "k1", "passphrase": "pass1", "timestamp": "1700000000", "sign": expected_ws_sig}],
    }

    assert bitget_instrument_id("btcusdt") == "BTCUSDT-PERP.BITGET"
    assert bitget_raw_symbol("BTCUSDT-PERP.BITGET") == "BTCUSDT"
    with pytest.raises(ValueError):
        bitget_raw_symbol("BTC-USDT-SWAP.OKX")
    with pytest.raises(ValueError):
        bitget_instrument_id("BAD/SYM")

    assert is_bitget_uta_error("40085", "") is True
    assert is_bitget_uta_error("40001", "You are in Unified Account mode") is True
    assert is_bitget_uta_error("40768", "Order does not exist") is False

    contracts = parse_bitget_contracts(
        {
            "code": "00000",
            "data": [
                {
                    "symbol": "ENJUSDT", "symbolType": "perpetual", "symbolStatus": "normal",
                    "sizeMultiplier": "0.1", "minTradeNum": "3", "minTradeUSDT": "5",
                    "pricePlace": "5", "priceEndStep": "1", "isRwa": "NO", "fundInterval": "4",
                },
                {
                    "symbol": "CHZUSDT", "symbolType": "perpetual", "symbolStatus": "normal",
                    "sizeMultiplier": "1", "minTradeNum": "20", "minTradeUSDT": "5",
                    "pricePlace": "4", "priceEndStep": "5", "isRwa": "NO", "fundInterval": "8",
                },
                {
                    "symbol": "TSLAUSDT", "symbolType": "perpetual", "symbolStatus": "normal",
                    "sizeMultiplier": "0.01", "minTradeNum": "0.01", "minTradeUSDT": "5",
                    "pricePlace": "2", "priceEndStep": "1", "isRwa": "YES", "fundInterval": "8",
                },
                {
                    "symbol": "DEADUSDT", "symbolType": "perpetual", "symbolStatus": "maintain",
                    "sizeMultiplier": "1", "minTradeNum": "1", "pricePlace": "2", "priceEndStep": "1",
                },
                {
                    "symbol": "BTCUSD_2606", "symbolType": "delivery", "symbolStatus": "normal",
                    "sizeMultiplier": "1", "minTradeNum": "1", "pricePlace": "2", "priceEndStep": "1",
                },
                {
                    "symbol": "BTCUSDC", "symbolType": "perpetual", "symbolStatus": "normal",
                    "sizeMultiplier": "0.001", "minTradeNum": "0.001", "pricePlace": "1", "priceEndStep": "1",
                },
            ],
        }
    )
    assert set(contracts.keys()) == {"ENJUSDT", "CHZUSDT", "TSLAUSDT"}
    enj_spec, enj_meta = contracts["ENJUSDT"]
    assert enj_spec.size_increment == D("0.1")
    assert enj_spec.min_quantity == D("3")
    assert enj_spec.contract_multiplier == D("1")
    assert enj_spec.price_increment == D("0.00001")
    assert enj_spec.min_notional_usd == D("5")
    assert enj_meta["fund_interval_h"] == 4 and enj_meta["asset_class"] == "CRYPTO"

    chz_spec, _ = contracts["CHZUSDT"]
    assert chz_spec.size_increment == D("1") and chz_spec.min_quantity == D("20")
    assert chz_spec.price_increment == D("0.0005")

    tsla_spec, tsla_meta = contracts["TSLAUSDT"]
    assert tsla_spec.instrument_id == "TSLAUSDT-PERP.BITGET"
    assert tsla_meta["is_rwa"] is True and tsla_meta["asset_class"] == "RWA"


def test_public_carry_feed_bitget_parsing_enrichment_and_error_isolation() -> None:
    now_s = 1_800_000_000
    contracts_payload = {
        "code": "00000",
        "data": [
            {
                "symbol": "BTCUSDT", "symbolType": "perpetual", "symbolStatus": "normal",
                "sizeMultiplier": "0.0001", "minTradeNum": "0.0001", "minTradeUSDT": "5",
                "pricePlace": "1", "priceEndStep": "1", "isRwa": "NO", "fundInterval": "8",
            },
            {
                "symbol": "TSLAUSDT", "symbolType": "perpetual", "symbolStatus": "normal",
                "sizeMultiplier": "0.01", "minTradeNum": "0.01", "minTradeUSDT": "5",
                "pricePlace": "2", "priceEndStep": "1", "isRwa": "YES", "fundInterval": "4",
            },
            {
                "symbol": "DELISTEDUSDT", "symbolType": "perpetual", "symbolStatus": "off",
                "sizeMultiplier": "1", "minTradeNum": "1", "minTradeUSDT": "5",
                "pricePlace": "2", "priceEndStep": "1", "isRwa": "NO", "fundInterval": "8",
            },
        ],
    }
    tickers_payload = {
        "code": "00000",
        "data": [
            {"symbol": "BTCUSDT", "markPrice": "60000", "bidPr": "59999", "askPr": "60001", "usdtVolume": "100000000"},
            {"symbol": "TSLAUSDT", "markPrice": "250.0", "bidPr": "249.95", "askPr": "250.05", "usdtVolume": "4000000"},
            {"symbol": "DELISTEDUSDT", "markPrice": "10", "bidPr": "9.9", "askPr": "10.1", "usdtVolume": "9999999"},
        ],
    }
    fund_payload = {
        "code": "00000",
        "data": [
            {"symbol": "BTCUSDT", "fundingRate": "0.0008", "fundingRateInterval": "8", "nextUpdate": str((now_s + 3600) * 1000)},
            {"symbol": "TSLAUSDT", "fundingRate": "0.0004", "fundingRateInterval": "4", "nextUpdate": str((now_s + 1800) * 1000)},
            {"symbol": "EXTRAUSDT", "fundingRate": "0.01", "fundingRateInterval": "8", "nextUpdate": str((now_s + 1800) * 1000)},
        ],
    }
    legs = parse_bitget_tickers(contracts_payload, tickers_payload, fund_payload, now_s=now_s)
    assert set(legs.keys()) == {"BTC", "TSLA"}
    assert legs["BTC"].instrument_id == "BTCUSDT-PERP.BITGET"
    assert legs["BTC"].current_rate_bps_h == pytest.approx(1.0)
    assert legs["BTC"].next_settle_ts == now_s + 3600
    assert legs["TSLA"].asset_class == "RWA"
    assert legs["TSLA"].interval_h == 4 and legs["TSLA"].current_rate_bps_h == pytest.approx(1.0)

    def fake_http(url: str, body: dict | None = None):
        if "hyperliquid.xyz" in url:
            if (body or {}).get("dex") == "xyz":
                return [{"universe": []}, []]
            return [
                {"universe": [{"name": "TSLA"}]},
                [{"markPx": "250", "midPx": "250", "funding": "0.00001", "dayNtlVlm": "5000000"}],
            ] if (body or {}).get("type") == "metaAndAssetCtxs" else (
                [{"time": (now_s - 3600) * 1000, "fundingRate": "0.0001"}]
                if (body or {}).get("type") == "fundingHistory"
                else [{"t": (now_s - 7200) * 1000, "c": "250", "v": "10"}, {"t": (now_s - 3600) * 1000, "c": "250", "v": "20"}]
            )
        if "mix/market/contracts" in url:
            return contracts_payload
        if "mix/market/tickers" in url:
            return tickers_payload
        if "mix/market/current-fund-rate" in url:
            return fund_payload
        if "mix/market/history-fund-rate" in url:
            # Test fundingTime +60_000 ms boundary rounding
            return {"code": "00000", "data": [{"symbol": "TSLAUSDT", "fundingRate": "0.0008", "fundingTime": str((now_s - 4 * HOUR) * 1000 - 500)}]}
        if "mix/market/candles" in url:
            return {
                "code": "00000",
                "data": [
                    [str((now_s - 3600) * 1000), "250", "251", "249", "250.5", "100", "25050"],
                    [str((now_s - 7200) * 1000), "248", "250", "247", "249.0", "80", "19920"],
                ],
            }
        raise AssertionError(f"unexpected URL {url}")

    feed = PublicCarryFeed(venues=("HL", "BITGET"), http_get=fake_http)
    snap = feed.poll_snapshot(now_s=now_s)
    assert feed.last_venue_errors == {}
    assert "TSLA" in snap and set(snap["TSLA"].keys()) == {"HL", "BITGET"}
    bg_tsla = snap["TSLA"]["BITGET"]
    assert bg_tsla.prev_1h_price == pytest.approx(249.0)
    assert bg_tsla.vol1h_usd == pytest.approx(25050.0)
    assert bg_tsla.r8_bps_h == pytest.approx(1.0)
    assert feed.recent_settlements("BITGET", "TSLA") == ((now_s - 4 * HOUR, 8.0),)
    specs = feed.bitget_instrument_specs()
    assert "TSLAUSDT-PERP.BITGET" in specs and specs["TSLAUSDT-PERP.BITGET"].size_increment == D("0.01")

    # Partial error isolation when Bitget fails while HL succeeds
    def failing_bitget_http(url: str, body: dict | None = None):
        if "bitget.com" in url:
            raise RuntimeError("bitget timeout")
        return fake_http(url, body)

    err_feed = PublicCarryFeed(venues=("HL", "BITGET"), http_get=failing_bitget_http)
    err_snap = err_feed.poll_snapshot(now_s=now_s, include_details=False)
    assert "TSLA" in err_snap and "HL" in err_snap["TSLA"]
    assert "BITGET" in err_feed.last_venue_errors


def test_startup_account_reader_bitget_positions_orders_uta_and_hedge_fail_closed() -> None:
    env = {
        "BITGET_API_KEY": "bg-key-secret",
        "BITGET_API_SECRET": "bg-secret-value",
        "BITGET_API_PASSPHRASE": "bg-pass-secret",
    }

    def ok_http(method: str, url: str, *, headers=None, body=None, timeout=10.0):
        assert headers and "ACCESS-SIGN" in headers and "paptrading" not in headers
        if "/api/v2/mix/account/account" in url:
            return {"code": "00000", "data": {"posMode": "one_way_mode", "marginMode": "crossed"}}
        if "/api/v2/mix/account/isolated-symbols" in url:
            return {"code": "00000", "data": []}
        if "/api/v2/mix/position/all-position" in url:
            return {
                "code": "00000",
                "data": [
                    {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.02", "available": "0.02", "posMode": "one_way_mode", "marginMode": "crossed"},
                    {"symbol": "TSLAUSDT", "holdSide": "short", "total": "1.5", "available": "1.5", "posMode": "one_way_mode", "marginMode": "crossed"},
                ],
            }
        if "/api/v2/mix/order/orders-pending" in url:
            if "idLessThan=" not in url:
                return {
                    "code": "00000",
                    "data": {
                        "entrustedList": [{"symbol": "BTCUSDT", "orderId": f"bg-{i}"} for i in range(100)],
                        "endId": "bg-99",
                    },
                }
            return {
                "code": "00000",
                "data": {"entrustedList": [{"symbol": "BTCUSDT", "orderId": "bg-100"}], "endId": ""},
            }
        if "/api/v2/mix/order/orders-plan-pending" in url:
            if "planType=profit_loss" in url:
                return {
                    "code": "00000",
                    "data": {"entrustedList": [{"symbol": "TSLAUSDT", "orderId": "plan-1"}], "endId": ""},
                }
            return {"code": "00000", "data": {"entrustedList": [], "endId": ""}}
        raise AssertionError(f"unexpected URL {method} {url}")

    reader = CarryStartupAccountReader(environment="mainnet", environ=env, http_request=ok_http)
    snap = reader.fetch_startup_snapshot(("BITGET",))
    assert snap.positions == {
        ("BITGET", "BTCUSDT-PERP.BITGET"): D("0.02"),
        ("BITGET", "TSLAUSDT-PERP.BITGET"): D("-1.5"),
    }
    assert snap.open_orders == {
        ("BITGET", "BTCUSDT-PERP.BITGET"): 101,
        ("BITGET", "TSLAUSDT-PERP.BITGET"): 1,
    }

    # Hedge mode fail-closed
    hedge_reader = CarryStartupAccountReader(
        environment="mainnet",
        environ=env,
        http_request=lambda *_a, **_kw: {"code": "00000", "data": {"posMode": "hedge_mode", "marginMode": "crossed"}},
    )
    with pytest.raises(StartupSnapshotError, match="one_way_mode"):
        hedge_reader.fetch_startup_snapshot(("BITGET",))

    # UTA fail-closed + credential sanitization
    uta_reader = CarryStartupAccountReader(
        environment="mainnet",
        environ=env,
        http_request=lambda *_a, **_kw: {"code": "40085", "msg": "Unified Account bg-secret-value"},
    )
    with pytest.raises(StartupSnapshotError) as uta_exc:
        uta_reader.fetch_startup_snapshot(("BITGET",))
    assert "UTA" in str(uta_exc.value)
    assert "bg-secret-value" not in str(uta_exc.value)

    # TOCTOU position mismatch fail-closed
    pos_calls = [0]

    def race_http(method: str, url: str, **kw):
        if "/api/v2/mix/position/all-position" in url:
            pos_calls[0] += 1
            qty = "0.01" if pos_calls[0] == 1 else "0.02"
            return {"code": "00000", "data": [{"symbol": "BTCUSDT", "holdSide": "long", "total": qty, "posMode": "one_way_mode"}]}
        return ok_http(method, url, **kw)

    with pytest.raises(StartupSnapshotError, match="positions changed"):
        CarryStartupAccountReader(environment="mainnet", environ=env, http_request=race_http).fetch_startup_snapshot(("BITGET",))


def test_bitget_public_quote_feed_loopback_ws_ping_pong_and_reconnect() -> None:
    sub_requests: list[dict[str, Any]] = []

    def on_msg(_ws: Any, msg: str | bytes) -> None:
        obj = json.loads(msg)
        if obj.get("op") in ("subscribe", "unsubscribe"):
            sub_requests.append(obj)

    srv = _LoopbackWsServer(on_message=on_msg)
    srv.start()
    feed = BitgetPublicQuoteFeed(
        ws_url=srv.url,
        ping_interval_secs=0.12,
        recv_timeout_secs=1.0,
        reconnect_backoffs=(0.05,),
    )
    try:
        feed.subscribe("BTCUSDT-PERP.BITGET")
        feed.start()
        _wait_until(lambda: len(sub_requests) >= 1)
        assert sub_requests[0]["args"] == [{"instType": "USDT-FUTURES", "channel": "books1", "instId": "BTCUSDT"}]

        srv.send_json(
            {
                "action": "snapshot",
                "arg": {"instType": "USDT-FUTURES", "channel": "books1", "instId": "BTCUSDT"},
                "data": [{"asks": [["61001.5", "2.0"]], "bids": [["61000.5", "3.0"]], "ts": "1700000000123"}],
            }
        )
        _wait_until(lambda: srv.received_pings >= 1)
        quotes = feed.drain_quotes()
        assert len(quotes) == 1
        assert quotes[0] == ("BTCUSDT-PERP.BITGET", 61000.5, 61001.5, 1700000000123 * 1_000_000)

        # Dynamic subscribe + reconnect replay
        feed.subscribe("TSLAUSDT-PERP.BITGET")
        _wait_until(lambda: len(sub_requests) >= 2)
        prev_subs = len(sub_requests)
        srv.kick_all()
        _wait_until(lambda: srv.connection_count >= 2 and len(sub_requests) > prev_subs)
        replayed = {a["instId"] for req in sub_requests[prev_subs:] for a in req.get("args", [])}
        assert replayed == {"BTCUSDT", "TSLAUSDT"}
    finally:
        feed.stop(timeout=2.0)
        srv.stop()
    assert not feed.alive()


def test_bitget_venue_gateway_async_submit_resync_cancel_race_and_fees() -> None:
    env = {
        "BITGET_API_KEY": "bg-key-secret",
        "BITGET_API_SECRET": "bg-secret-value",
        "BITGET_API_PASSPHRASE": "bg-pass-secret",
    }

    # 1. Preflight checks (one_way_mode + crossed + empty isolated-symbols ok; hedge, isolated, UTA fail closed)
    assert BitgetVenueGateway(
        environ=env,
        rest_request=lambda _m, p, _b: (
            {"code": "00000", "data": []}
            if "/isolated-symbols" in p
            else {"code": "00000", "data": {"posMode": "one_way_mode", "marginMode": "crossed"}}
        ),
    ).preflight_check() is None

    for bad_resp, match_txt in (
        ({"code": "00000", "data": {"posMode": "hedge_mode", "marginMode": "crossed"}}, "one_way_mode"),
        ({"code": "00000", "data": {"posMode": "one_way_mode", "marginMode": "isolated"}}, "crossed"),
        ({"code": "40085", "msg": "Unified Account bg-secret-value"}, "UTA"),
    ):
        with pytest.raises(BitgetPreflightError, match=match_txt) as exc_info:
            BitgetVenueGateway(environ=env, rest_request=lambda _m, _p, _b, r=bad_resp: r).preflight_check()
        assert "bg-secret-value" not in str(exc_info.value)

    # 2. Private WS loopback + non-blocking submit + uncertain submit recovery + cancel race + fee normalization
    ws_events: list[dict[str, Any]] = []

    def on_priv_msg(ws: Any, msg: str | bytes) -> Any:
        obj = json.loads(msg)
        ws_events.append(obj)
        if obj.get("op") == "login":
            return ws.send(json.dumps({"event": "login", "code": "0"}))
        if obj.get("op") == "subscribe":
            return ws.send(json.dumps({"event": "subscribe", "arg": obj["args"][0]}))
        return None

    priv_srv = _LoopbackWsServer(on_message=on_priv_msg)
    priv_srv.start()

    rest_calls: list[tuple[str, str, dict[str, Any] | None]] = []
    mode_state: dict[str, str] = {"place_mode": "ok", "cancel_mode": "ok"}

    def fake_rest(method: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        rest_calls.append((method, path, body))
        if path.endswith("/place-order"):
            time.sleep(0.25)  # verify caller thread does NOT block
            if mode_state["place_mode"] == "uncertain_live":
                raise BitgetTransportError("simulated timeout")
            if mode_state["place_mode"] == "uncertain_missing":
                raise BitgetTransportError("simulated timeout")
            coid = (body or {}).get("clientOid", "")
            return {"code": "00000", "data": {"orderId": f"VID-{coid}", "clientOid": coid}}
        if path.endswith("/cancel-order"):
            if mode_state["cancel_mode"] == "already_filled":
                return {"code": "40768", "msg": "order does not exist"}
            if mode_state["cancel_mode"] == "reject_still_open":
                return {"code": "40099", "msg": "temporary error"}
            return {"code": "00000", "data": {"orderId": (body or {}).get("orderId", "")}}
        if "/api/v2/mix/order/detail" in path:
            if "coid-missing" in path:
                return {"code": "40768", "msg": "order does not exist"}
            if "coid-uncertain" in path:
                return {"code": "00000", "data": {"orderId": "VID-uncertain", "clientOid": "coid-uncertain", "state": "live", "baseVolume": "0"}}
            if "coid-race-fill" in path:
                return {
                    "code": "00000",
                    "data": {"orderId": "VID-race-fill", "clientOid": "coid-race-fill", "state": "filled", "baseVolume": "1.0", "priceAvg": "250.0"},
                }
            if "coid-cancel-rej" in path:
                return {
                    "code": "00000",
                    "data": {"orderId": "VID-coid-cancel-rej", "clientOid": "coid-cancel-rej", "state": "live", "baseVolume": "0"},
                }
            if "coid-resync" in path:
                return {
                    "code": "00000",
                    "data": {"orderId": "VID-coid-resync", "clientOid": "coid-resync", "state": "filled", "baseVolume": "0.5", "priceAvg": "60000"},
                }
            return {"code": "00000", "data": {"clientOid": "other", "state": "live", "baseVolume": "0"}}
        if "/api/v2/mix/order/fills" in path:
            if "VID-race-fill" in path:
                return {
                    "code": "00000",
                    "data": {
                        "fillList": [
                            {
                                "tradeId": "tr-r1",
                                "orderId": "VID-race-fill",
                                "baseVolume": "1.0",
                                "price": "250.0",
                                "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.12"}],
                            }
                        ]
                    },
                }
            if "VID-coid-resync" in path:
                return {
                    "code": "00000",
                    "data": {
                        "fillList": [
                            {
                                "tradeId": "tr-s1",
                                "orderId": "VID-coid-resync",
                                "baseVolume": "0.5",
                                "price": "60000",
                                "feeDetail": [{"feeCoin": "BGB", "totalFee": "-0.01"}],
                            }
                        ]
                    },
                }
        return {"code": "00000", "data": {}}

    gw = BitgetVenueGateway(
        environ=env,
        ws_url=priv_srv.url,
        rest_request=fake_rest,
        ping_interval_secs=0.15,
        recv_timeout_secs=1.0,
        resync_interval_secs=5.0,
        reconnect_backoffs=(0.05,),
    )
    try:
        gw.start()
        _wait_until(lambda: any(m.get("op") == "subscribe" for m in ws_events))
        sub_msg = next(m for m in ws_events if m.get("op") == "subscribe")
        assert sub_msg["args"] == [
            {"instType": "USDT-FUTURES", "channel": "orders", "instId": "default"},
            {"instType": "USDT-FUTURES", "channel": "fill", "instId": "default"},
        ]

        # Non-blocking submit despite 0.25s HTTP sleep
        t0 = time.monotonic()
        ack = gw.submit_limit(
            client_order_id="coid-1",
            venue="BITGET",
            instrument_id="BTCUSDT-PERP.BITGET",
            side="BUY",
            qty_contracts=D("0.5"),
            price=D("60000"),
            post_only=True,
            ioc=False,
            reduce_only=False,
        )
        assert (time.monotonic() - t0) < 0.08
        assert ack.status == "OPEN"
        # Ensure place body has one_way_mode fields and NO tradeSide
        _wait_until(lambda: any(p.endswith("/place-order") and (b or {}).get("clientOid") == "coid-1" for _, p, b in rest_calls))
        place_body = next(b for _, p, b in rest_calls if p.endswith("/place-order") and (b or {}).get("clientOid") == "coid-1")
        assert place_body is not None and "tradeSide" not in place_body
        assert place_body["force"] == "post_only" and place_body["reduceOnly"] == "NO" and place_body["marginMode"] == "crossed"

        # Deduplicated Classic v2 negative fill fees + monotonic filled qty via WS
        priv_srv.send_json(
            {
                "action": "push",
                "arg": {"instType": "USDT-FUTURES", "channel": "fill", "instId": "default"},
                "data": [
                    {"clientOid": "coid-1", "orderId": "VID-coid-1", "tradeId": "t1", "baseVolume": "0.2", "price": "60000", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.10"}]},
                    {"clientOid": "coid-1", "orderId": "VID-coid-1", "tradeId": "t1", "baseVolume": "0.2", "price": "60000", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.10"}]},
                    {"clientOid": "coid-1", "orderId": "VID-coid-1", "tradeId": "t2", "baseVolume": "0.3", "price": "60000", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.05"}]},
                ],
            }
        )
        _wait_until(lambda: gw.poll_order(client_order_id="coid-1", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").status == "FILLED")
        polled = gw.poll_order(client_order_id="coid-1", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET")
        assert polled.filled_base_qty == D("0.5")
        assert polled.fee_usd == D("0.15")

        # Uncertain submit -> live order preserved via order/detail
        mode_state["place_mode"] = "uncertain_live"
        gw.submit_limit(
            client_order_id="coid-uncertain", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
            side="BUY", qty_contracts=D("0.1"), price=D("60000"), post_only=False, ioc=False, reduce_only=False,
        )
        _wait_until(lambda: any("coid-uncertain" in p for _, p, _ in rest_calls))
        assert gw.poll_order(client_order_id="coid-uncertain", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").status == "OPEN"

        # Uncertain submit -> 40768 not found -> REJECTED
        mode_state["place_mode"] = "uncertain_missing"
        gw.submit_limit(
            client_order_id="coid-missing", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
            side="BUY", qty_contracts=D("0.1"), price=D("60000"), post_only=False, ioc=False, reduce_only=False,
        )
        _wait_until(lambda: gw.poll_order(client_order_id="coid-missing", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").status == "REJECTED")

        # Cancel race: cancel fails 40768 because order filled -> advances to FILLED + backfill fee
        mode_state["place_mode"] = "ok"
        mode_state["cancel_mode"] = "already_filled"
        gw.submit_limit(
            client_order_id="coid-race-fill", venue="BITGET", instrument_id="TSLAUSDT-PERP.BITGET",
            side="SELL", qty_contracts=D("1.0"), price=D("250"), post_only=True, ioc=False, reduce_only=True,
        )
        _wait_until(lambda: any(p.endswith("/place-order") and (b or {}).get("clientOid") == "coid-race-fill" for _, p, b in rest_calls))
        gw.drain_events()
        gw.cancel_order(client_order_id="coid-race-fill", venue="BITGET", instrument_id="TSLAUSDT-PERP.BITGET")
        _wait_until(lambda: gw.poll_order(client_order_id="coid-race-fill", venue="BITGET", instrument_id="TSLAUSDT-PERP.BITGET").status == "FILLED")
        race_ack = gw.poll_order(client_order_id="coid-race-fill", venue="BITGET", instrument_id="TSLAUSDT-PERP.BITGET")
        assert race_ack.fee_usd == D("0.12")
        race_evts = [e for e in gw.drain_events() if e.client_order_id == "coid-race-fill"]
        assert not any(e.cancel_rejected for e in race_evts)

        # Cancel rejected while resting -> emits cancel_rejected=True
        mode_state["cancel_mode"] = "reject_still_open"
        gw.submit_limit(
            client_order_id="coid-cancel-rej", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
            side="BUY", qty_contracts=D("0.1"), price=D("60000"), post_only=True, ioc=False, reduce_only=False,
        )
        _wait_until(lambda: any(p.endswith("/place-order") and (b or {}).get("clientOid") == "coid-cancel-rej" for _, p, b in rest_calls))
        gw.cancel_order(client_order_id="coid-cancel-rej", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET")
        rej_evts: list[BitgetGatewayEvent] = []

        def _got_cancel_rej() -> bool:
            rej_evts.extend(gw.drain_events())
            return any(e.client_order_id == "coid-cancel-rej" and e.cancel_rejected for e in rej_evts)

        _wait_until(_got_cancel_rej)

        # Reconnect + login replay + REST resync + non-stablecoin BGB fee_invalid=True
        gw.submit_limit(
            client_order_id="coid-resync", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
            side="BUY", qty_contracts=D("0.5"), price=D("60000"), post_only=True, ioc=False, reduce_only=False,
        )
        _wait_until(lambda: any(p.endswith("/place-order") and (b or {}).get("clientOid") == "coid-resync" for _, p, b in rest_calls))
        gw.drain_events()
        prev_conns = priv_srv.connection_count
        priv_srv.kick_all()
        _wait_until(lambda: priv_srv.connection_count > prev_conns)
        _wait_until(lambda: gw.poll_order(client_order_id="coid-resync", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").status == "FILLED")
        resync_ack = gw.poll_order(client_order_id="coid-resync", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET")
        assert resync_ack.fee_usd is None
        resync_evts = [e for e in gw.drain_events() if e.client_order_id == "coid-resync"]
        assert any(e.fee_invalid for e in resync_evts)
    finally:
        gw.stop(timeout=2.0)
        priv_srv.stop()


def test_keepalive_rest_client_reconnects_once_on_remote_disconnect(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    class _FakeResp:
        status = 200

        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def getheader(self, _name: str) -> str:
            return "keep-alive"

    class _FakeConn:
        def __init__(self, idx: int) -> None:
            self.idx = idx
            self.req_count = 0

        def request(self, *_a, **_kw) -> None:
            self.req_count += 1
            calls.append(self.idx)
            # First connection succeeds once, then dies with RemoteDisconnected on reused 2nd request
            if self.idx == 1 and self.req_count == 2:
                raise http.client.RemoteDisconnected("closed by peer")

        def getresponse(self) -> _FakeResp:
            return _FakeResp(b'{"code":"00000","data":{"ok":true}}')

        def close(self) -> None:
            pass

    created = [0]
    client = _BitgetKeepAliveRestClient("https://api.bitget.com", timeout_secs=2.0)

    def _make_conn() -> _FakeConn:
        created[0] += 1
        return _FakeConn(created[0])

    monkeypatch.setattr(client, "_new_conn", _make_conn)
    assert client.request_json("GET", "/ping", headers={})["code"] == "00000"
    assert client.request_json("GET", "/ping", headers={})["code"] == "00000"
    assert calls == [1, 1, 2]
    client.close()


def test_carry_strategy_routing_gateway_paper_and_live_e2e(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # build_data_clients & build_execution_clients skip BITGET cleanly
    bg_plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper-bitget.toml")
    only_bg_plan = replace(bg_plan, venues=("BITGET",))
    assert build_data_clients(only_bg_plan) == []
    assert build_execution_clients(only_bg_plan, environ={}) == []

    # Paper mode: BitgetPublicQuoteFeed quotes drive PaperVenueGateway BBO fill + _specs_from_cache registers Bitget specs
    now_s = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: float(now_s))
    led_paper = CarryLedger(tmp_path / "paper_bg.sqlite3", bg_plan.fingerprint(), ledger_domain="paper")
    pub_feed = BitgetPublicQuoteFeed(ws_url="ws://127.0.0.1:9")
    strat_paper = _create_carry_strategy(bg_plan, led_paper, mode="paper", bitget_quote_feed=pub_feed)

    main_tid = threading.get_ident()
    logged_lines: list[str] = []

    class _MainThreadLogger:
        def info(self, msg: str) -> None:
            assert threading.get_ident() == main_tid
            logged_lines.append(msg)

        def warning(self, msg: str) -> None:
            assert threading.get_ident() == main_tid
            logged_lines.append(msg)

        def error(self, msg: str) -> None:
            assert threading.get_ident() == main_tid
            logged_lines.append(msg)

        def debug(self, msg: str) -> None:
            assert threading.get_ident() == main_tid

    monkeypatch.setattr(type(strat_paper), "log", property(lambda _s: _MainThreadLogger()))

    spec_hl = InstrumentSpec("TSLA-USD-PERP.HYPERLIQUID", "HL", size_increment=D("0.01"), price_increment=D("0.01"))
    spec_bg = InstrumentSpec("TSLAUSDT-PERP.BITGET", "BITGET", size_increment=D("0.01"), price_increment=D("0.01"))
    strat_paper.feed._bitget_specs = {spec_bg.instrument_id: spec_bg}
    assert strat_paper.paper_gateway is not None
    strat_paper.live_mids[spec_bg.instrument_id] = 250.4
    real_specs_merge = type(strat_paper)._specs_from_cache(
        SimpleNamespace(
            cache=SimpleNamespace(instruments=lambda: []),
            feed=strat_paper.feed,
            paper_gateway=strat_paper.paper_gateway,
            live_mids=strat_paper.live_mids,
        )
    )
    assert real_specs_merge == {spec_bg.instrument_id: spec_bg}
    assert spec_bg.instrument_id in strat_paper.paper_gateway.specs
    strat_paper._specs_from_cache = lambda: {
        spec_hl.instrument_id: spec_hl,
        spec_bg.instrument_id: spec_bg,
    }
    strat_paper.paper_gateway.register_spec(spec_hl, D("250.0"))
    strat_paper.paper_gateway.update_quote(spec_hl.instrument_id, D("249.98"), D("250.02"))
    strat_paper.live_quotes[spec_hl.instrument_id] = (249.98, 250.02)
    strat_paper.live_mids[spec_hl.instrument_id] = 250.0

    # Push Bitget BBO into quote queue and drain on main thread
    pub_feed._enqueue_log("INFO", "[test] queued log line")
    pub_feed._enqueue_quote((spec_bg.instrument_id, 250.38, 250.42, now_s * 1_000_000_000))
    strat_paper._drain_external_events()
    assert strat_paper.live_quotes[spec_bg.instrument_id] == (250.38, 250.42)
    assert "[test] queued log line" in logged_lines

    cand = evaluate_entry_candidates(
        {
            "TSLA": {
                "HL": _make_leg("HL", "TSLA", asset_class="RWA", r8=0.0, r24=0.0, bid=249.98, ask=250.02, prev_1h=250.0),
                "BITGET": LegMarketInfo(
                    venue="BITGET", symbol="TSLA", raw_symbol="TSLAUSDT", instrument_id=spec_bg.instrument_id,
                    asset_class="RWA", interval_h=8, next_settle_ts=now_s + 7200, r8_bps_h=2.0, r24_bps_h=2.0,
                    current_rate_bps_h=2.0, mark_price=250.4, prev_1h_price=250.0, bid=250.38, ask=250.42,
                    vol24_usd=5_000_000.0, vol1h_usd=200_000.0, fetched_ts=now_s,
                ),
            }
        },
        open_symbols=set(), venue_notional_used={}, excluded_symbols=set(),
        policy=bg_plan.policy, risk=bg_plan.risk, now_s=now_s,
    )[0]
    pid, err = strat_paper.controller.start_entry(cand, specs=strat_paper._specs_from_cache(), now_s=now_s)
    assert err is None and pid is not None
    # Crossing maker quote on HL fills HL maker + Bitget IOC via PaperVenueGateway
    pub_feed._enqueue_quote((spec_bg.instrument_id, 250.40, 250.44, (now_s + 1) * 1_000_000_000))
    strat_paper._drain_external_events()
    strat_paper._ingest_quote(spec_hl.instrument_id, 249.96, 249.98, (now_s + 1) * 1_000_000_000)
    pos = led_paper.active_positions()[pid]
    assert D(pos["long_base_qty"]) > 0 and D(pos["short_base_qty"]) == D(pos["long_base_qty"])
    strat_paper.on_stop()
    led_paper.close()

    # Live mode: preflight fail-closed + RoutingVenueGateway + fee_usd / fee_invalid / cancel_rejected
    live_bg_plan = replace(bg_plan, mode="live")
    led_fail = CarryLedger(tmp_path / "live_fail.sqlite3", live_bg_plan.fingerprint(), ledger_domain="live")
    bad_gw = BitgetVenueGateway(
        environ={"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"},
        rest_request=lambda *_a, **_kw: {"code": "00000", "data": {"posMode": "hedge_mode", "marginMode": "crossed"}},
        enable_ws=False,
    )
    strat_fail = _create_carry_strategy(
        live_bg_plan,
        led_fail,
        mode="live",
        account_reader=type("_R", (), {"fetch_startup_snapshot": lambda *_a, **_kw: VenueAccountSnapshot({}, {})})(),
        bitget_gateway=bad_gw,
    )
    monkeypatch.setattr(type(strat_fail), "log", property(lambda _s: _MainThreadLogger()))
    strat_fail._reconcile_on_start()
    assert strat_fail.reconciled is False and led_fail.is_reconcile_halted()[0] is True
    strat_fail.on_stop()
    led_fail.close()

    # RoutingVenueGateway + live strategy fee & cancel_rejected event drain
    native_stub = DelayedAsyncVenueGateway()
    native_stub.register_spec(spec_hl)
    live_gw = BitgetVenueGateway(
        environ={"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"},
        rest_request=lambda _m, p, _b: (
            {"code": "00000", "data": {"posMode": "one_way_mode", "marginMode": "crossed"}}
            if "/account/account" in p
            else (
                {"code": "00000", "data": []}
                if "/isolated-symbols" in p
                else {"code": "00000", "data": {"orderId": "VID-LIVE"}}
            )
        ),
        enable_ws=False,
    )
    router = RoutingVenueGateway(native_gateway=native_stub, bitget_gateway=live_gw)
    router.submit_limit(
        client_order_id="hl-1", venue="HL", instrument_id=spec_hl.instrument_id,
        side="BUY", qty_contracts=D("0.2"), price=D("250"), post_only=True, ioc=False, reduce_only=False,
    )
    assert len(native_stub.submitted) == 1
    assert live_gw.poll_order(client_order_id="hl-1", venue="BITGET", instrument_id=spec_bg.instrument_id).venue_order_id == ""

    led_live = CarryLedger(tmp_path / "live_ok.sqlite3", live_bg_plan.fingerprint(), ledger_domain="live")
    strat_live = _create_carry_strategy(live_bg_plan, led_live, mode="live", bitget_gateway=live_gw)
    monkeypatch.setattr(type(strat_live), "log", property(lambda _s: _MainThreadLogger()))
    strat_live.gateway = router
    strat_live.controller.gateway = router
    strat_live.reconciled = True
    strat_live._specs_from_cache = lambda: {spec_hl.instrument_id: spec_hl, spec_bg.instrument_id: spec_bg}
    live_gw.start()
    try:
        pid2, err2 = strat_live.controller.start_entry(cand, specs=strat_live._specs_from_cache(), now_s=now_s)
        assert err2 is None and pid2 is not None
        # HL maker fills -> controller submits IOC hedge to BITGET through RoutingVenueGateway
        mk_coid = native_stub.submitted[-1]["client_order_id"]
        native_stub.deliver_fill(mk_coid, filled_base_qty=D("0.2"), avg_fill_price=D("249.98"))
        strat_live.controller.poll_active_orders(
            live_mids={spec_hl.instrument_id: 250.0, spec_bg.instrument_id: 250.4},
            now_s=now_s + 1,
            live_quotes={spec_hl.instrument_id: (249.98, 250.02), spec_bg.instrument_id: (250.38, 250.42)},
        )
        hg_row = led_live.conn.execute("SELECT client_order_id, venue FROM orders WHERE position_id=? AND role='HEDGE_OPEN'", (pid2,)).fetchone()
        assert hg_row is not None and hg_row["venue"] == "BITGET"
        hg_coid = str(hg_row["client_order_id"])

        # Push realized fee from Bitget fill into _drain_external_events()
        live_gw._apply_fill_rows(
            [{"clientOid": hg_coid, "orderId": "VID-H1", "tradeId": "t-live-1", "baseVolume": "0.2", "price": "250.40", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.03"}]}],
            emit_event=True,
        )
        strat_live._drain_external_events()
        fee_row = led_live.conn.execute("SELECT status, fee_usd FROM orders WHERE client_order_id=?", (hg_coid,)).fetchone()
        assert fee_row["status"] == "FILLED" and D(fee_row["fee_usd"]) == D("0.03")

        # Test fee_invalid clears order fee so ledger falls back to 6 bps Bitget taker map
        live_gw._apply_fill_rows(
            [{"clientOid": hg_coid, "orderId": "VID-H1", "tradeId": "t-live-2", "baseVolume": "0.0", "price": "250.40", "feeDetail": [{"feeCoin": "BGB", "fee": "-0.01"}]}],
            emit_event=True,
        )
        strat_live._drain_external_events()
        fee_cleared = led_live.conn.execute("SELECT fee_usd FROM orders WHERE client_order_id=?", (hg_coid,)).fetchone()
        assert fee_cleared["fee_usd"] is None

        # Test cancel_rejected event resets maker cancel_requested
        fake_mk_coid = "mk-cancel-test"
        led_live.register_order_intent(
            client_order_id=fake_mk_coid,
            position_id=pid2,
            venue="BITGET",
            instrument_id=spec_bg.instrument_id,
            side="BUY",
            role="MAKER_OPEN",
            order_type="POST_ONLY",
            qty_contracts=D("0.1"),
            base_qty=D("0.1"),
            price=D("250.30"),
            ts_s=now_s + 2,
        )
        live_gw.submit_limit(
            client_order_id=fake_mk_coid,
            venue="BITGET",
            instrument_id=spec_bg.instrument_id,
            side="BUY",
            qty_contracts=D("0.1"),
            price=D("250.30"),
            post_only=True,
            ioc=False,
            reduce_only=False,
        )
        strat_live.controller._active_makers[fake_mk_coid] = SimpleNamespace(
            client_order_id=fake_mk_coid,
            position_id=pid2,
            is_opening=True,
            maker_side_is_long=True,
            maker_venue="BITGET",
            hedge_venue="HL",
            spec_maker=spec_bg,
            spec_hedge=spec_hl,
            submitted_ts=now_s + 2,
            attempts=1,
            accounted_maker_base_qty=D("0"),
            settled_hedged_base_qty=D("0"),
            cancel_requested=True,
        )
        live_gw._enqueue_event(BitgetGatewayEvent(client_order_id=fake_mk_coid, cancel_rejected=True))
        strat_live._drain_external_events()
        assert strat_live.controller._active_makers[fake_mk_coid].cancel_requested is False
        strat_live.controller._active_makers.pop(fake_mk_coid, None)
    finally:
        strat_live.on_stop()
        led_live.close()
    assert not live_gw.alive()


def test_keepalive_rest_client_no_retry_on_post(monkeypatch: pytest.MonkeyPatch) -> None:
    get_conns: list[int] = []
    post_conns: list[int] = []

    class _FakeResp:
        status = 200

        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def getheader(self, _name: str) -> str:
            return "keep-alive"

    class _FakeConn:
        def __init__(self, idx: int) -> None:
            self.idx = idx
            self.req_count = 0
            self.last_method = "GET"

        def request(self, method: str, *_a, **_kw) -> None:
            self.req_count += 1
            self.last_method = method
            if method == "POST":
                post_conns.append(self.idx)
                # Die with RemoteDisconnected on reused connection -> POST must NEVER replay!
                if self.req_count >= 2:
                    raise http.client.RemoteDisconnected("closed on post")
            else:
                get_conns.append(self.idx)
                if self.idx == 1 and self.req_count == 2:
                    raise http.client.RemoteDisconnected("closed on get")

        def getresponse(self) -> _FakeResp:
            return _FakeResp(b'{"code":"00000","data":{"ok":true}}')

        def close(self) -> None:
            pass

    created = [0]
    client = _BitgetKeepAliveRestClient("https://api.bitget.com", timeout_secs=2.0)

    def _make_conn() -> _FakeConn:
        created[0] += 1
        return _FakeConn(created[0])

    monkeypatch.setattr(client, "_new_conn", _make_conn)
    assert client.request_json("GET", "/ping", headers={})["code"] == "00000"
    # GET on reused conn #1 drops conn #1 and retries once on conn #2
    assert client.request_json("GET", "/ping", headers={})["code"] == "00000"
    assert get_conns == [1, 1, 2]
    # Next POST reuses conn #2, hits RemoteDisconnected, drops conn #2, and raises without replaying
    with pytest.raises(BitgetTransportError, match="RemoteDisconnected"):
        client.request_json("POST", "/api/v2/mix/order/place-order", headers={}, body_str="{}")
    assert post_conns == [2]
    assert client._conn is None
    client.close()


def test_bitget_place_error_classification_and_uncertain_submit_resync() -> None:
    env = {"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}
    detail_calls: list[str] = []
    detail_mode: dict[str, list[dict[str, Any]]] = {
        "coid-40786-live": [
            {"code": "00000", "data": {"orderId": "VID-40786", "clientOid": "coid-40786-live", "state": "live", "baseVolume": "0"}}
        ],
        "coid-unc-2nf": [
            {"code": "50000", "msg": "detail busy"},
            {"code": "40768", "msg": "not found 1"},
            {"code": "40768", "msg": "not found 2"},
        ],
        "coid-unc-ws": [
            {"code": "50000", "msg": "detail busy"},
            {"code": "40768", "msg": "not found once before ws"},
        ],
        "coid-unc-err": [
            {"code": "50000", "msg": "detail down 1"},
            {"code": "50000", "msg": "detail down 2"},
            {"code": "50000", "msg": "detail down 3"},
        ],
    }

    def fake_rest(_m: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if path.endswith("/place-order"):
            coid = str((body or {}).get("clientOid") or "")
            if coid == "coid-def-45110":
                return {"code": "45110", "msg": "less than minimum notional 5 USDT"}
            if coid == "coid-def-40762":
                return {"code": "40762", "msg": "order qty exceeds max open"}
            if coid == "coid-40786-live":
                return {"code": "40786", "msg": "duplicate clientOid or state conflict"}
            if coid in ("coid-unc-2nf", "coid-unc-ws", "coid-unc-err"):
                raise BitgetTransportError("timeout")
            return {"code": "00000", "data": {"orderId": f"VID-{coid}", "clientOid": coid}}
        if "/api/v2/mix/order/detail" in path:
            for key, queue_resps in detail_mode.items():
                if key in path:
                    detail_calls.append(key)
                    return queue_resps.pop(0) if queue_resps else {"code": "50000", "msg": "exhausted"}
        return {"code": "00000", "data": {}}

    gw = BitgetVenueGateway(
        environ=env,
        rest_request=fake_rest,
        enable_ws=False,
        resync_interval_secs=0.05,
        uncertain_error_max_checks=3,
    )
    # 1. Definite reject codes mark REJECTED immediately without querying order/detail
    for coid in ("coid-def-45110", "coid-def-40762"):
        gw.submit_limit(
            client_order_id=coid, venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
            side="BUY", qty_contracts=D("0.1"), price=D("60000"), post_only=False, ioc=False, reduce_only=False,
        )
        gw._exec_place_sync(coid)
        ack = gw.poll_order(client_order_id=coid, venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET")
        assert ack.status == "REJECTED" and "bitget_code_" in (ack.reason or "")
        assert coid not in detail_calls

    # 2. Uncertain non-00000 (40786) reconciles via order/detail and preserves live order
    gw.submit_limit(
        client_order_id="coid-40786-live", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("0.1"), price=D("60000"), post_only=False, ioc=False, reduce_only=False,
    )
    gw._exec_place_sync("coid-40786-live")
    assert "coid-40786-live" in detail_calls
    ack_live = gw.poll_order(client_order_id="coid-40786-live", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET")
    assert ack_live.status == "OPEN" and ack_live.venue_order_id == "VID-40786"
    assert gw._orders["coid-40786-live"].submit_uncertain is False

    # 3. Uncertain submit + immediate ERROR -> requires 2 consecutive NOT_FOUND in resync to reject
    gw.submit_market(
        client_order_id="coid-unc-2nf", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("0.1"), reduce_only=False,
    )
    gw._exec_place_sync("coid-unc-2nf")
    assert gw._orders["coid-unc-2nf"].submit_uncertain is True
    assert gw.poll_order(client_order_id="coid-unc-2nf", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").status == "OPEN"

    gw._resync_non_terminal_sync(force=True)
    assert gw._orders["coid-unc-2nf"].not_found_confirmations == 1
    assert gw.poll_order(client_order_id="coid-unc-2nf", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").status == "OPEN"

    gw._resync_non_terminal_sync(force=True)
    ack_2nf = gw.poll_order(client_order_id="coid-unc-2nf", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET")
    assert ack_2nf.status == "REJECTED" and ack_2nf.reason == "not_found_after_uncertain_submit"
    assert "coid-unc-2nf" not in gw._active_coids

    # 4. Uncertain submit cleared when WS confirms order after 1 NOT_FOUND
    gw.submit_market(
        client_order_id="coid-unc-ws", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("0.1"), reduce_only=False,
    )
    gw._exec_place_sync("coid-unc-ws")
    gw._resync_non_terminal_sync(force=True)
    assert gw._orders["coid-unc-ws"].not_found_confirmations == 1 and gw._orders["coid-unc-ws"].submit_uncertain is True
    gw._handle_private_ws_message(
        json.dumps(
            {
                "action": "push",
                "arg": {"instType": "USDT-FUTURES", "channel": "orders", "instId": "default"},
                "data": [{"clientOid": "coid-unc-ws", "orderId": "VID-WS-OK", "state": "live", "accBaseVolume": "0"}],
            }
        )
    )
    assert gw._orders["coid-unc-ws"].submit_uncertain is False
    assert gw._orders["coid-unc-ws"].not_found_confirmations == 0

    # 5. Repeated detail ERROR emits bounded ERROR alert log
    gw.submit_market(
        client_order_id="coid-unc-err", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("0.1"), reduce_only=False,
    )
    gw._exec_place_sync("coid-unc-err")  # uncertain_error_checks = 1
    gw._resync_non_terminal_sync(force=True)  # uncertain_error_checks = 2
    gw._resync_non_terminal_sync(force=True)  # uncertain_error_checks = 3 -> triggers ERROR alert
    logs = gw.drain_logs()
    assert any(lvl == "ERROR" and "coid-unc-err" in msg for lvl, msg in logs)


def test_bitget_gateway_shutdown_drains_place_and_bounds_stalled_place() -> None:
    env = {"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}
    placed_coids: list[str] = []

    def fast_rest(_m: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        if path.endswith("/place-order"):
            coid = str((body or {}).get("clientOid") or "")
            placed_coids.append(coid)
            return {"code": "00000", "data": {"orderId": f"VID-{coid}"}}
        return {"code": "00000", "data": {}}

    # A. Even if stop_evt is already set, _command_loop finally drains BOTH cancel and place orders
    gw = BitgetVenueGateway(environ=env, rest_request=fast_rest, enable_ws=False, shutdown_drain_timeout_secs=1.0)
    gw.submit_market(
        client_order_id="coid-emergency-hedge", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="SELL", qty_contracts=D("0.2"), reduce_only=True,
    )

    async def _run_stop_drain() -> None:
        wake = asyncio.Event()
        stop_evt = asyncio.Event()
        stop_evt.set()
        await gw._command_loop(wake, stop_evt)

    asyncio.run(_run_stop_drain())
    assert placed_coids == ["coid-emergency-hedge"]

    # B. Stalled place during shutdown times out at shutdown_drain_timeout_secs and logs ERROR
    release = threading.Event()

    def slow_rest(_m: str, path: str, body: dict[str, Any] | None) -> dict[str, Any]:
        release.wait(timeout=1.5)
        return {"code": "00000", "data": {"orderId": "VID-SLOW"}}

    gw_slow = BitgetVenueGateway(
        environ=env,
        rest_request=slow_rest,
        enable_ws=False,
        shutdown_drain_timeout_secs=0.12,
    )
    gw_slow.start()
    try:
        gw_slow.submit_market(
            client_order_id="coid-stalled-close", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
            side="SELL", qty_contracts=D("0.2"), reduce_only=True,
        )
        t0 = time.monotonic()
        gw_slow.stop(timeout=0.5)
        elapsed = time.monotonic() - t0
        assert elapsed < 0.7
        logs = gw_slow.drain_logs()
        assert any(lvl == "ERROR" and "coid-stalled-close" in msg for lvl, msg in logs)
    finally:
        release.set()
        gw_slow.stop(timeout=2.0)


def test_bitget_gateway_stop_timeout_not_blocked_by_rest_lock() -> None:
    env = {"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}
    gw = BitgetVenueGateway(environ=env, enable_ws=False, http_timeout_secs=5.0)
    assert gw._rest_client is not None
    lock_acquired = threading.Event()
    release_lock = threading.Event()
    close_calls: list[float] = []
    orig_close = gw._rest_client.close

    def tracked_close() -> None:
        close_calls.append(time.monotonic())
        orig_close()

    gw._rest_client.close = tracked_close  # type: ignore[method-assign]

    def _hold_rest_lock() -> None:
        assert gw._rest_client is not None
        with gw._rest_client._lock:
            lock_acquired.set()
            release_lock.wait(timeout=2.0)

    # Simulate background gateway thread still alive and holding _rest_client._lock
    fake_worker = threading.Thread(target=_hold_rest_lock, daemon=True)
    fake_worker.start()
    assert lock_acquired.wait(timeout=1.0)
    with gw._loop_lock:
        gw._thread = fake_worker

    t0 = time.monotonic()
    gw.stop(timeout=0.12)
    elapsed = time.monotonic() - t0
    assert elapsed < 0.35
    # Must NOT have blocked in stop() calling close() while thread remained alive!
    assert close_calls == []

    release_lock.set()
    fake_worker.join(timeout=2.0)
    with gw._loop_lock:
        gw._thread = None
    gw.stop(timeout=0.5)
    assert len(close_calls) == 1


def test_bitget_gateway_cancel_rejected_survives_full_event_queue() -> None:
    env = {"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}

    def rest_req(_m: str, path: str, _b: dict[str, Any] | None) -> dict[str, Any]:
        if path.endswith("/cancel-order"):
            return {"code": "40099", "msg": "temporary cancel reject"}
        if "/order/detail" in path:
            return {"code": "00000", "data": {"orderId": "VID-C1", "clientOid": "coid-cr", "state": "live", "baseVolume": "0"}}
        return {"code": "00000", "data": {"orderId": "VID-C1"}}

    gw = BitgetVenueGateway(environ=env, rest_request=rest_req, enable_ws=False, event_queue_maxsize=2)
    gw.submit_limit(
        client_order_id="coid-cr", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("0.1"), price=D("60000"), post_only=True, ioc=False, reduce_only=False,
    )
    gw.drain_events()
    # Saturate _event_queue completely
    gw._enqueue_event(BitgetGatewayEvent(client_order_id="fill-1"))
    gw._enqueue_event(BitgetGatewayEvent(client_order_id="fill-2"))
    assert gw._event_queue.full()

    # Cancel rejected must still reach drain_events despite full _event_queue
    gw._exec_cancel_sync("coid-cr")
    evts = gw.drain_events()
    cr_evts = [e for e in evts if e.client_order_id == "coid-cr" and e.cancel_rejected]
    assert len(cr_evts) == 1

    # Also verify non-full queue does not emit duplicate cancel_rejected events
    gw.drain_events()
    gw._exec_cancel_sync("coid-cr")
    evts2 = [e for e in gw.drain_events() if e.client_order_id == "coid-cr" and e.cancel_rejected]
    assert len(evts2) == 1


def test_bitget_gateway_multifill_vwap_partial_fees_and_backfill() -> None:
    env = {"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}
    fills_fetch_calls: list[str] = []

    def rest_req(_m: str, path: str, _b: dict[str, Any] | None) -> dict[str, Any]:
        if "/api/v2/mix/order/fills" in path:
            fills_fetch_calls.append(path)
            if "VID-BF1" in path:
                return {
                    "code": "00000",
                    "data": {
                        "fillList": [
                            {"tradeId": "t2", "orderId": "VID-BF1", "baseVolume": "0.3", "price": "100", "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.15"}]}
                        ]
                    },
                }
        return {"code": "00000", "data": {}}

    gw = BitgetVenueGateway(environ=env, rest_request=rest_req, enable_ws=False)

    # 1. Fix #7: Dynamic VWAP recomputation across multiple priced fills (ignores unpriced fills in denominator)
    gw.submit_limit(
        client_order_id="coid-vwap", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("1.0"), price=D("105"), post_only=False, ioc=False, reduce_only=False,
    )
    gw._apply_fill_rows(
        [{"clientOid": "coid-vwap", "orderId": "VID-VWAP", "tradeId": "v1", "baseVolume": "0.2", "price": "100", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.02"}]}],
        emit_event=False,
    )
    assert gw.poll_order(client_order_id="coid-vwap", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").avg_fill_price == D("100")

    gw._apply_fill_rows(
        [
            {"clientOid": "coid-vwap", "orderId": "VID-VWAP", "tradeId": "v2", "baseVolume": "0.6", "price": "104", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.06"}]},
            {"clientOid": "coid-vwap", "orderId": "VID-VWAP", "tradeId": "v3", "baseVolume": "0.2", "price": "", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.02"}]},
        ],
        emit_event=False,
    )
    ack_vwap = gw.poll_order(client_order_id="coid-vwap", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET")
    assert ack_vwap.filled_base_qty == D("1.0")
    # (0.2*100 + 0.6*104) / 0.8 = 82.4 / 0.8 = 103
    assert ack_vwap.avg_fill_price == D("103")
    assert ack_vwap.fee_usd == D("0.10")

    # 2. Fix #8: Partial trade fee does NOT hide complete order-level fee, and full coverage triggers once
    gw.submit_limit(
        client_order_id="coid-ol", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("0.5"), price=D("100"), post_only=True, ioc=False, reduce_only=False,
    )
    gw._apply_fill_rows(
        [{"clientOid": "coid-ol", "orderId": "VID-OL", "tradeId": "o1", "baseVolume": "0.2", "price": "100", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.10"}]}],
        emit_event=False,
    )
    assert gw.poll_order(client_order_id="coid-ol", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").fee_usd == D("0.10")

    _, need_bf_ol, _ = gw._apply_order_snapshot_dict(
        {"clientOid": "coid-ol", "orderId": "VID-OL", "state": "filled", "accBaseVolume": "0.5", "priceAvg": "100", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.25"}]},
        emit_event=True,
    )
    assert gw.poll_order(client_order_id="coid-ol", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").fee_usd == D("0.25")
    assert need_bf_ol is True

    # 3. Fix #8: Partial fill fee is cleared (not treated as authoritative) if FILLED snapshot has no full feeDetail
    gw.submit_limit(
        client_order_id="coid-bf1", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("0.5"), price=D("100"), post_only=True, ioc=False, reduce_only=False,
    )
    gw._apply_fill_rows(
        [{"clientOid": "coid-bf1", "orderId": "VID-BF1", "tradeId": "t1", "baseVolume": "0.2", "price": "100", "feeDetail": [{"feeCoin": "USDT", "fee": "-0.10"}]}],
        emit_event=False,
    )
    gw.drain_events()
    coid_out, need_bf, _ = gw._apply_order_snapshot_dict(
        {"clientOid": "coid-bf1", "orderId": "VID-BF1", "state": "filled", "accBaseVolume": "0.5", "priceAvg": "100"},
        emit_event=True,
    )
    assert coid_out == "coid-bf1" and need_bf is True
    assert gw.poll_order(client_order_id="coid-bf1", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").fee_usd is None
    evts = gw.drain_events()
    assert any(e.client_order_id == "coid-bf1" and e.fee_cleared for e in evts)

    # Drain backfill once -> fetches missing fill t2 -> complete coverage -> fee_usd == 0.25
    gw._backfill_queue.put("coid-bf1")
    gw._drain_backfills_sync()
    assert gw.poll_order(client_order_id="coid-bf1", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET").fee_usd == D("0.25")
    assert len(fills_fetch_calls) == 1
    # Repeating backfill for the same order does NOT refetch due to fee_backfill_attempted
    gw._backfill_queue.put("coid-bf1")
    gw._drain_backfills_sync()
    assert len(fills_fetch_calls) == 1


def test_bitget_gateway_bounded_retention_and_active_coids() -> None:
    env = {"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}
    gw = BitgetVenueGateway(
        environ=env,
        rest_request=lambda *_a, **_kw: {"code": "00000", "data": {"state": "live", "accBaseVolume": "0"}},
        enable_ws=False,
        resync_interval_secs=0.05,
        terminal_retention_secs=10.0,
        terminal_prune_threshold=5,
    )
    now_m = time.monotonic()
    for i in range(8):
        coid = f"old-term-{i}"
        gw.submit_limit(
            client_order_id=coid, venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
            side="BUY", qty_contracts=D("0.1"), price=D("60000"), post_only=True, ioc=False, reduce_only=False,
        )
        with gw._orders_lock:
            rec = gw._orders[coid]
            rec.venue_order_id = f"VID-OLD-{i}"
            gw._vid_to_coid[f"VID-OLD-{i}"] = coid
            gw._mark_terminal_locked(rec, "CANCELED")
            rec.terminal_ts = now_m - 20.0
        gw._order_async_locks[coid] = asyncio.Lock()

    gw.submit_limit(
        client_order_id="recent-term", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("0.1"), price=D("60000"), post_only=True, ioc=False, reduce_only=False,
    )
    with gw._orders_lock:
        rec_r = gw._orders["recent-term"]
        gw._mark_terminal_locked(rec_r, "REJECTED")
        rec_r.terminal_ts = now_m
    gw.submit_limit(
        client_order_id="active-open", venue="BITGET", instrument_id="BTCUSDT-PERP.BITGET",
        side="BUY", qty_contracts=D("0.1"), price=D("60000"), post_only=True, ioc=False, reduce_only=False,
    )

    # Snapshot only traverses _active_coids (O(active))
    snap = gw._non_terminal_orders_snapshot()
    assert [row[0] for row in snap] == ["active-open"]

    gw._resync_non_terminal_sync(force=True)
    for i in range(8):
        coid = f"old-term-{i}"
        assert coid not in gw._orders
        assert f"VID-OLD-{i}" not in gw._vid_to_coid
        assert coid not in gw._order_async_locks
    assert "recent-term" in gw._orders and "active-open" in gw._orders


def test_bitget_public_ws_drops_bad_ts_and_retries_unconfirmed_subs() -> None:
    # 1. Fix #10: Missing/invalid/non-positive ms timestamps are dropped cleanly
    unit_feed = BitgetPublicQuoteFeed(ws_url="ws://127.0.0.1:9")
    for bad_ts in (None, "", "bad", "0", "-100", True):
        msg = {
            "action": "snapshot",
            "arg": {"instType": "USDT-FUTURES", "channel": "books1", "instId": "BTCUSDT"},
            "data": [{"asks": [["60001", "1"]], "bids": [["60000", "1"]], **({"ts": bad_ts} if bad_ts is not None else {})}],
        }
        unit_feed._handle_public_message(json.dumps(msg))
    assert unit_feed.drain_quotes() == []
    unit_feed._handle_public_message(
        json.dumps(
            {
                "action": "snapshot",
                "arg": {"instType": "USDT-FUTURES", "channel": "books1", "instId": "BTCUSDT"},
                "ts": "1700000000456",
                "data": [{"asks": [["60001", "1"]], "bids": [["60000", "1"]]}],
            }
        )
    )
    assert unit_feed.drain_quotes() == [("BTCUSDT-PERP.BITGET", 60000.0, 60001.0, 1700000000456 * 1_000_000)]

    # 2. Fix #11: Rejected / unconfirmed subscriptions retry with backoff until confirmed
    sub_attempts: dict[str, int] = {"BTCUSDT": 0, "ETHUSDT": 0}
    confirm_ready = {"ok": False}

    def on_msg(ws: Any, raw: str | bytes) -> Any:
        obj = json.loads(raw)
        if obj.get("op") != "subscribe":
            return None
        replies: list[str] = []
        for a in obj.get("args", []):
            sym = a["instId"]
            sub_attempts[sym] = sub_attempts.get(sym, 0) + 1
            if sub_attempts[sym] == 1 and sym == "BTCUSDT":
                replies.append(json.dumps({"event": "error", "code": "30001", "arg": a}))
            elif sub_attempts[sym] >= 2 and confirm_ready["ok"]:
                if sym == "BTCUSDT":
                    replies.append(json.dumps({"event": "subscribe", "arg": a}))
                else:
                    replies.append(
                        json.dumps(
                            {
                                "action": "snapshot",
                                "arg": a,
                                "data": [{"asks": [["3001", "1"]], "bids": [["3000", "1"]], "ts": "1700000001000"}],
                            }
                        )
                    )

        async def _send_replies() -> None:
            for r in replies:
                await ws.send(r)

        return _send_replies()

    srv = _LoopbackWsServer(on_message=on_msg)
    srv.start()
    feed = BitgetPublicQuoteFeed(
        ws_url=srv.url,
        ping_interval_secs=5.0,
        recv_timeout_secs=5.0,
        sub_retry_backoffs=(0.06, 0.06),
    )
    try:
        feed.subscribe("BTCUSDT-PERP.BITGET")
        feed.subscribe("ETHUSDT-PERP.BITGET")
        feed.start()
        _wait_until(lambda: sub_attempts["BTCUSDT"] >= 1 and sub_attempts["ETHUSDT"] >= 1)
        confirm_ready["ok"] = True
        _wait_until(lambda: sub_attempts["BTCUSDT"] >= 2 and sub_attempts["ETHUSDT"] >= 2)
        _wait_until(lambda: any(q[0] == "ETHUSDT-PERP.BITGET" for q in feed.drain_quotes()))
        b_cnt, e_cnt = sub_attempts["BTCUSDT"], sub_attempts["ETHUSDT"]
        time.sleep(0.18)
        # Once confirmed by event="subscribe" / books1 frame, no further unnecessary retries
        assert sub_attempts["BTCUSDT"] == b_cnt and sub_attempts["ETHUSDT"] == e_cnt
    finally:
        feed.stop(timeout=2.0)
        srv.stop()


def test_bitget_funding_feed_limit_open_interval_preservation_and_error_envelopes() -> None:
    now_s = 1_800_000_000
    contracts_payload = {
        "code": "00000",
        "data": [
            {
                "symbol": "BTCUSDT", "symbolType": "perpetual", "symbolStatus": "normal",
                "sizeMultiplier": "0.001", "minTradeNum": "0.001", "minTradeUSDT": "5",
                "pricePlace": "1", "priceEndStep": "1", "isRwa": "NO", "fundInterval": "8",
            },
            {
                "symbol": "OLDUSDT", "symbolType": "perpetual", "symbolStatus": "limit_open",
                "sizeMultiplier": "1", "minTradeNum": "1", "minTradeUSDT": "5",
                "pricePlace": "2", "priceEndStep": "1", "isRwa": "NO", "fundInterval": "8",
            },
        ],
    }
    tickers_payload = {
        "code": "00000",
        "data": [
            {"symbol": "BTCUSDT", "markPrice": "60000", "bidPr": "59999", "askPr": "60001", "usdtVolume": "100000000"},
            {"symbol": "OLDUSDT", "markPrice": "10", "bidPr": "9.99", "askPr": "10.01", "usdtVolume": "5000000"},
        ],
    }

    # 1. Fix #12: limit_open in parse_bitget_contracts & specs, excluded from default entry, kept for priority/close_only
    parsed = parse_bitget_contracts(contracts_payload)
    assert parsed["BTCUSDT"][1]["close_only"] is False and parsed["OLDUSDT"][1]["close_only"] is True
    assert set(parse_bitget_tickers(contracts_payload, tickers_payload, now_s=now_s).keys()) == {"BTC"}
    assert set(parse_bitget_tickers(contracts_payload, tickers_payload, now_s=now_s, close_only_symbols={"OLD"}).keys()) == {"BTC", "OLD"}

    # Minimal {"data": [...]} without "code" key remains supported
    assert "OLDUSDT" in parse_bitget_contracts({"data": contracts_payload["data"]})

    # 2. Fix #13: Live interval_h & next_settle_ts preserved against historical spacing & 5-minute detail cache
    live_state = {"iv": "1", "nxt": now_s + 1800, "contracts_err": False}

    def fake_http(url: str, body: dict | None = None):
        if "hyperliquid.xyz" in url:
            if (body or {}).get("dex") == "xyz":
                return [{"universe": []}, []]
            if (body or {}).get("type") == "metaAndAssetCtxs":
                return [
                    {"universe": [{"name": "BTC"}]},
                    [{"markPx": "60000", "midPx": "60000", "funding": "0.0001", "dayNtlVlm": "5000000"}],
                ]
            if (body or {}).get("type") == "fundingHistory":
                return [{"time": (now_s - 3600) * 1000, "fundingRate": "0.0001"}]
            return [{"t": (now_s - 7200) * 1000, "c": "60000", "v": "1"}, {"t": (now_s - 3600) * 1000, "c": "60000", "v": "2"}]
        if "mix/market/contracts" in url:
            return {"code": "40015", "data": None} if live_state["contracts_err"] else contracts_payload
        if "mix/market/tickers" in url:
            return tickers_payload
        if "mix/market/current-fund-rate" in url:
            return {
                "code": "00000",
                "data": [{"symbol": "BTCUSDT", "fundingRate": "0.0004", "fundingRateInterval": live_state["iv"], "nextUpdate": str(live_state["nxt"] * 1000)}],
            }
        if "mix/market/history-fund-rate" in url:
            # Historical settlements spaced 4h apart -> must NOT overwrite live_state["iv"] == 1
            return {
                "code": "00000",
                "data": [
                    {"symbol": "BTCUSDT", "fundingRate": "0.0004", "fundingTime": str((now_s - 8 * HOUR) * 1000)},
                    {"symbol": "BTCUSDT", "fundingRate": "0.0004", "fundingTime": str((now_s - 4 * HOUR) * 1000)},
                ],
            }
        if "mix/market/candles" in url:
            return {"code": "00000", "data": [[str((now_s - 7200) * 1000), "60000", "60100", "59900", "60000", "1", "60000"], [str((now_s - 3600) * 1000), "60000", "60100", "59900", "60050", "2", "120100"]]}
        raise AssertionError(url)

    feed = PublicCarryFeed(venues=("HL", "BITGET"), http_get=fake_http)
    snap1 = feed.poll_snapshot(now_s=now_s, priority_symbols={"OLD"})
    assert snap1["BTC"]["BITGET"].interval_h == 1
    assert snap1["BTC"]["BITGET"].next_settle_ts == now_s + 1800
    assert "OLDUSDT-PERP.BITGET" in feed.bitget_instrument_specs()

    # Within 300s detail cache window, live bulk changes to interval_h=4 & next_settle_ts are preserved
    live_state["iv"] = "4"
    live_state["nxt"] = now_s + 3600
    snap2 = feed.poll_snapshot(now_s=now_s + 60)
    assert snap2["BTC"]["BITGET"].interval_h == 4
    assert snap2["BTC"]["BITGET"].next_settle_ts == now_s + 3600

    # 3. Fix #14: Bitget {"code": "40015", "data": None} is treated as failure, not cached, and raises SlowPollError if only venue
    live_state["contracts_err"] = True
    err_feed = PublicCarryFeed(venues=("HL", "BITGET"), http_get=fake_http)
    err_feed.poll_snapshot(now_s=now_s, include_details=False)
    assert "BITGET" in err_feed.last_venue_errors
    assert err_feed._bitget_contracts_cache is None

    solo_err_feed = PublicCarryFeed(venues=("BITGET",), http_get=fake_http)
    with pytest.raises(SlowPollError, match="BITGET="):
        solo_err_feed.poll_snapshot(now_s=now_s, include_details=False)


def test_bitget_startup_reader_and_preflight_strict_validation() -> None:
    env = {"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}

    def base_responses() -> dict[str, Any]:
        return {
            "account": {"code": "00000", "data": {"posMode": "one_way_mode", "marginMode": "crossed"}},
            "isolated": {"code": "00000", "data": []},
            "positions": {"code": "00000", "data": []},
            "orders-pending": {"code": "00000", "data": {"entrustedList": None, "endId": None}},
            "orders-plan-pending": {"code": "00000", "data": {"entrustedList": None, "endId": None}},
        }

    def make_http(resps: dict[str, Any]):
        def _http(_m: str, url: str, **_kw):
            if "/account/account" in url:
                return resps["account"]
            if "/account/isolated-symbols" in url:
                return resps["isolated"]
            if "/position/all-position" in url:
                return resps["positions"]
            if "/order/orders-pending" in url:
                return resps["orders-pending"]
            if "/order/orders-plan-pending" in url:
                return resps["orders-plan-pending"]
            raise AssertionError(url)
        return _http

    # Real Bitget entrustedList: null is valid empty open orders
    snap = CarryStartupAccountReader(environment="mainnet", environ=env, http_request=make_http(base_responses())).fetch_startup_snapshot(("BITGET",))
    assert snap.positions == {} and snap.open_orders == {}

    # Fail closed on missing data / bad all-position / missing entrustedList / bad holdSide / missing marginMode / isolated position or isolated-symbols
    bad_cases: list[tuple[str, Any, str]] = [
        ("account", {"code": "00000"}, "missing data"),
        ("positions", {"code": "00000", "data": None}, "missing list"),
        ("orders-pending", {"code": "00000", "data": {}}, "invalid payload"),
        ("orders-pending", {"code": "00000", "data": None}, "invalid payload"),
        ("positions", {"code": "00000", "data": [{"symbol": "BTCUSDT", "holdSide": "net", "total": "0.1", "posMode": "one_way_mode"}]}, "holdSide"),
        ("positions", {"code": "00000", "data": [{"symbol": "BTCUSDT", "holdSide": "", "total": "0.1", "posMode": "one_way_mode"}]}, "holdSide"),
        ("account", {"code": "00000", "data": {"posMode": "one_way_mode"}}, "crossed"),
        ("positions", {"code": "00000", "data": [{"symbol": "BTCUSDT", "holdSide": "long", "total": "0.1", "posMode": "one_way_mode", "marginMode": "isolated"}]}, "crossed"),
        ("isolated", {"code": "00000", "data": [{"symbol": "ETHUSDT", "marginMode": "isolated"}]}, "isolated-margin"),
    ]
    for key, bad_val, match_txt in bad_cases:
        resps = base_responses()
        resps[key] = bad_val
        with pytest.raises(StartupSnapshotError, match=match_txt):
            CarryStartupAccountReader(environment="mainnet", environ=env, http_request=make_http(resps)).fetch_startup_snapshot(("BITGET",))

    # Gateway preflight_check rejects missing marginMode and non-empty isolated-symbols
    with pytest.raises(BitgetPreflightError, match="crossed"):
        BitgetVenueGateway(
            environ=env,
            rest_request=lambda _m, p, _b: {"code": "00000", "data": []} if "/isolated-symbols" in p else {"code": "00000", "data": {"posMode": "one_way_mode"}},
        ).preflight_check()

    with pytest.raises(BitgetPreflightError, match="BTCUSDT"):
        BitgetVenueGateway(
            environ=env,
            rest_request=lambda _m, p, _b: {"code": "00000", "data": [{"symbol": "BTCUSDT"}]} if "/isolated-symbols" in p else {"code": "00000", "data": {"posMode": "one_way_mode", "marginMode": "crossed"}},
        ).preflight_check()


def test_closed_position_late_fee_and_fee_invalid_reconciliation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    now_s = 1_800_000_000
    monkeypatch.setattr(time, "time", lambda: float(now_s))
    bg_plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper-bitget.toml")
    led = CarryLedger(tmp_path / "late_fee.sqlite3", bg_plan.fingerprint(), ledger_domain="paper")
    pub_feed = BitgetPublicQuoteFeed(ws_url="ws://127.0.0.1:9")
    gw = BitgetVenueGateway(environ={"BITGET_API_KEY": "k", "BITGET_API_SECRET": "s", "BITGET_API_PASSPHRASE": "p"}, enable_ws=False)
    strat = _create_carry_strategy(bg_plan, led, mode="paper", bitget_gateway=gw)
    strat.reconciled = True
    try:
        pid = "pos-closed-1"
        led.create_position(
            position_id=pid, symbol="BTC", long_venue="HL", short_venue="BITGET",
            iid_long="BTC-USD-PERP.HYPERLIQUID", iid_short="BTCUSDT-PERP.BITGET",
            opened_ts=now_s - 600, entry_l=100.0, entry_s=100.0, entry_basis_bps=0.0,
            target_base_qty=D("1"),
        )
        for coid, venue, iid, side, role, px, fee in (
            ("mk-open", "HL", "BTC-USD-PERP.HYPERLIQUID", "BUY", "MAKER_OPEN", D("100"), D("0.015")),
            ("hg-open", "BITGET", "BTCUSDT-PERP.BITGET", "SELL", "HEDGE_OPEN", D("100"), None),
            ("mk-close", "HL", "BTC-USD-PERP.HYPERLIQUID", "SELL", "MAKER_CLOSE", D("101"), D("0.0152")),
            ("hg-close", "BITGET", "BTCUSDT-PERP.BITGET", "BUY", "HEDGE_CLOSE", D("100"), D("0.06")),
        ):
            led.register_order_intent(
                client_order_id=coid, position_id=pid, venue=venue, instrument_id=iid,
                side=side, role=role, order_type="LIMIT", qty_contracts=D("1"), base_qty=D("1"),
                price=px, ts_s=now_s - 500,
            )
            led.update_order_state(coid, status="FILLED", filled_base_qty=D("1"), fill_price=px, fee_usd=fee, ts_s=now_s - 400)

        init_fee = led.compute_position_fee_usd(
            pid,
            maker_fee_bps_by_venue=bg_plan.policy.maker_fee_bps_by_venue,
            taker_fee_bps_by_venue=bg_plan.policy.taker_fee_bps_by_venue,
        )
        assert init_fee == D("0.1502")  # hg-open fell back to 6 bps = 0.0600
        led.update_position_status(
            pid, status="CLOSED", ts_s=now_s - 10,
            long_base_qty=D("1"), short_base_qty=D("1"),
            price_pnl_usd=D("1.0000"), funding_pnl_usd=D("0.2000"),
            fee_usd=init_fee, realized_pnl_usd=D("1.2000") - init_fee,
            close_reason="carry_decay",
        )
        assert led.daily_stats(now_s - 10) == (D("1.0498"), 0)

        # Late fee event arrives AFTER close and removal from controller active maps
        gw._enqueue_event(BitgetGatewayEvent(client_order_id="hg-open", fee_usd=D("0.1100")))
        strat._drain_external_events()
        pos_row = led.conn.execute("SELECT fee_usd, realized_pnl_usd FROM positions WHERE position_id=?", (pid,)).fetchone()
        assert D(pos_row["fee_usd"]) == D("0.2002")
        assert D(pos_row["realized_pnl_usd"]) == D("0.9998")
        assert led.daily_stats(now_s - 10) == (D("0.9998"), 0)
        evt_rows = led.conn.execute("SELECT kind, payload FROM events WHERE kind='position_fee_reconciled'").fetchall()
        assert len(evt_rows) == 1

        # Re-running reconciliation cleanly is strictly idempotent
        assert led.reconcile_closed_position_fee_for_order(
            "hg-open",
            maker_fee_bps_by_venue=bg_plan.policy.maker_fee_bps_by_venue,
            taker_fee_bps_by_venue=bg_plan.policy.taker_fee_bps_by_venue,
        ) is False
        assert led.daily_stats(now_s - 10) == (D("0.9998"), 0)

        # Late fee_invalid clears order fee back to fallback 6 bps and adjusts realized PnL & daily_stats
        gw._enqueue_event(BitgetGatewayEvent(client_order_id="hg-open", fee_invalid=True))
        strat._drain_external_events()
        pos_row2 = led.conn.execute("SELECT fee_usd, realized_pnl_usd FROM positions WHERE position_id=?", (pid,)).fetchone()
        assert D(pos_row2["fee_usd"]) == D("0.1502")
        assert D(pos_row2["realized_pnl_usd"]) == D("1.0498")
        assert led.daily_stats(now_s - 10) == (D("1.0498"), 0)
    finally:
        pub_feed.stop(timeout=1.0)
        strat.on_stop()
        led.close()

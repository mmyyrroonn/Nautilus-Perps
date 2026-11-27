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
from src.carry.funding_feed import HOUR, LegMarketInfo, PublicCarryFeed, parse_bitget_tickers
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

    # Identical parent path check on copies in tmp_path
    p_copy = tmp_path / "carry-paper.toml"
    p_copy.write_text((REPO_ROOT / "config" / "carry-paper.toml").read_text(encoding="utf-8"), encoding="utf-8")
    l_copy = tmp_path / "carry-live.toml"
    l_copy.write_text((REPO_ROOT / "config" / "carry-live.toml").read_text(encoding="utf-8"), encoding="utf-8")
    assert dict(load_carry_plan(p_copy).policy.maker_fee_bps_by_venue).keys() == {"BYBIT", "HL", "LIGHTER", "OKX"}
    assert dict(load_carry_plan(l_copy).policy.taker_fee_bps_by_venue).keys() == {"BYBIT", "HL", "LIGHTER", "OKX"}

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
        if "/api/v2/mix/position/all-position" in url:
            return {
                "code": "00000",
                "data": [
                    {"symbol": "BTCUSDT", "holdSide": "long", "total": "0.02", "available": "0.02", "posMode": "one_way_mode"},
                    {"symbol": "TSLAUSDT", "holdSide": "short", "total": "1.5", "available": "1.5", "posMode": "one_way_mode"},
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

    # 1. Preflight checks (one_way_mode + crossed ok; hedge_mode, isolated, UTA fail closed)
    assert BitgetVenueGateway(
        environ=env,
        rest_request=lambda _m, _p, _b: {"code": "00000", "data": {"posMode": "one_way_mode", "marginMode": "crossed"}},
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
                            {"tradeId": "tr-r1", "orderId": "VID-race-fill", "feeDetail": [{"feeCoin": "USDT", "totalFee": "-0.12"}]}
                        ]
                    },
                }
            if "VID-coid-resync" in path:
                return {
                    "code": "00000",
                    "data": {
                        "fillList": [
                            {"tradeId": "tr-s1", "orderId": "VID-coid-resync", "feeDetail": [{"feeCoin": "BGB", "totalFee": "-0.01"}]}
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
            else {"code": "00000", "data": {"orderId": "VID-LIVE"}}
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

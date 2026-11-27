"""Read-only public REST slow-layer aggregator for HL, Lighter, OKX, Bybit, and Bitget."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import inspect
import json
import threading
import time
from typing import TYPE_CHECKING, Any, Callable, Mapping
import urllib.error
import urllib.parse
import urllib.request

if TYPE_CHECKING:
    from .signal import InstrumentSpec

HOUR = 3600
HOUR_MS = 3_600_000

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
HL_TESTNET_INFO_URL = "https://api.hyperliquid-testnet.xyz/info"
LIGHTER_BASE = "https://mainnet.zklighter.elliot.ai"
LIGHTER_TESTNET_BASE = "https://testnet.zklighter.elliot.ai"
OKX_BASE = "https://www.okx.com"
BYBIT_BASE = "https://api.bybit.com"
BYBIT_TESTNET_BASE = "https://api-testnet.bybit.com"
USER_AGENT = "Mozilla/5.0 (compatible; NautilusPerpsCarry/1.0)"
_VALID_INTERVALS_H = frozenset({1, 2, 3, 4, 6, 8, 12, 24})


class SlowPollError(RuntimeError):
    """Raised when all configured venues fail during a slow snapshot poll."""

# Lighter RWA / equity / commodity tickers so asset_class separates Crypto vs RWA cleanly.
KNOWN_RWA_SYMBOLS = frozenset({
    "AAPL", "AMAT", "AMD", "AMZN", "ANT", "ARM", "ASML", "AVGO", "BABA", "BMNR",
    "BRENT", "CBRS", "COIN", "CRCL", "CRWV", "CXMT", "DKNG", "DRAM", "EURUSD", "EWY",
    "GIGADEV", "GLW", "GOLD", "GOOGL", "HOOD", "IBM", "INTC", "KIOXIA", "KORU", "LITE", "LLY",
    "META", "MINIMAX", "MSFT", "MSTR", "MU", "NFLX", "NOK", "NVDA", "ORCL", "PAXG",
    "PLTR", "QQQ", "SAMSUNG", "SAN", "SHEIN", "SMSN", "SNDK", "SOXL", "SPCX", "SPY",
    "STRC", "TSLA", "TSM", "UNITREE", "US100", "US500", "WTI", "XAG", "XAU", "XAUT",
    "XIAOMI", "XYZ100", "ZHIPU",
})


def default_http_json(
    url: str,
    body: dict | None = None,
    *,
    timeout: float = 12.0,
    headers: Mapping[str, str] | None = None,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req_headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    if data is not None:
        req_headers.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=data, headers=req_headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def _callable_accepts_headers(fn: Callable[..., Any]) -> bool:
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        return False
    for param in sig.parameters.values():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True
        if param.name == "headers" and param.kind in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.KEYWORD_ONLY,
        ):
            return True
    return False


def _parse_okx_funding_row(row: Any) -> tuple[float | None, int | None, int | None]:
    """Extract (raw_funding_rate, interval_h, next_settle_ts) from an OKX funding-rate item."""
    if not isinstance(row, dict):
        return None, None, None
    fr_val = row.get("fundingRate")
    raw_fr: float | None = None
    if fr_val not in (None, ""):
        try:
            raw_fr = float(fr_val)
        except (TypeError, ValueError):
            raw_fr = None
    try:
        funding_ms = int(row.get("fundingTime") or 0)
    except (TypeError, ValueError):
        funding_ms = 0
    try:
        next_ms = int(row.get("nextFundingTime") or 0)
    except (TypeError, ValueError):
        next_ms = 0
    nxt_settle = (funding_ms // 1000) if funding_ms > 0 else None
    live_iv: int | None = None
    if next_ms > funding_ms > 0:
        cand_iv = round((next_ms - funding_ms) / HOUR_MS)
        if cand_iv in _VALID_INTERVALS_H:
            live_iv = cand_iv
    return raw_fr, live_iv, nxt_settle


@dataclass(frozen=True)
class LegMarketInfo:
    venue: str
    symbol: str
    raw_symbol: str
    instrument_id: str
    asset_class: str  # "CRYPTO" or "RWA"
    interval_h: int
    next_settle_ts: int
    r8_bps_h: float | None
    r24_bps_h: float | None
    current_rate_bps_h: float | None
    mark_price: float
    prev_1h_price: float | None
    bid: float
    ask: float
    vol24_usd: float
    vol1h_usd: float | None
    fetched_ts: int
    market_id: int | None = None

    @property
    def mid(self) -> float:
        if self.bid > 0 and self.ask > 0:
            return 0.5 * (self.bid + self.ask)
        return self.mark_price

    @property
    def spread_bps(self) -> float | None:
        m = self.mid
        if self.bid > 0 and self.ask >= self.bid and m > 0:
            return (self.ask - self.bid) / m * 1e4
        return None


def classify_asset(symbol: str, *, is_xyz_hip3: bool = False) -> str:
    if is_xyz_hip3 or symbol.upper() in KNOWN_RWA_SYMBOLS:
        return "RWA"
    return "CRYPTO"


def next_hourly_settle_ts(now_s: int, interval_h: int) -> int:
    step = max(1, interval_h) * HOUR
    return ((int(now_s) // step) + 1) * step


def parse_hl_meta_and_ctxs(
    payload: Any, *, is_xyz: bool = False, now_s: int = 0
) -> dict[str, LegMarketInfo]:
    """Normalize HL metaAndAssetCtxs response (sign: + = longs pay shorts)."""
    if not isinstance(payload, list) or len(payload) < 2:
        return {}
    universe = payload[0].get("universe", [])
    ctxs = payload[1]
    out: dict[str, LegMarketInfo] = {}
    nxt = next_hourly_settle_ts(now_s, 1) if now_s > 0 else 0
    for meta, ctx in zip(universe, ctxs):
        if meta.get("isDelisted"):
            continue
        raw = str(meta["name"])
        if ":" in raw and not raw.startswith("xyz:"):
            continue
        sym = raw.split(":", 1)[1].upper() if raw.startswith("xyz:") else raw.upper()
        mark = float(ctx.get("markPx") or 0.0)
        mid = float(ctx.get("midPx") or mark or 0.0)
        # Note: HL `impactPxs` reflects large notional impact (not L1 BBO); slow layer uses midPx and Fast-Layer WS BBO enforces L1 spread.
        bid = mid
        ask = mid
        fr = ctx.get("funding")
        rate_bps_h = float(fr) * 1e4 if fr is not None else None
        vol24 = float(ctx.get("dayNtlVlm") or 0.0)
        if mark <= 0 or bid <= 0 or ask <= 0:
            continue
        out[sym] = LegMarketInfo(
            venue="HL",
            symbol=sym,
            raw_symbol=raw,
            instrument_id=f"{raw}-USD-PERP.HYPERLIQUID",
            asset_class=classify_asset(sym, is_xyz_hip3=is_xyz),
            interval_h=1,
            next_settle_ts=nxt,
            r8_bps_h=None,
            r24_bps_h=None,
            current_rate_bps_h=rate_bps_h,
            mark_price=mark,
            prev_1h_price=None,
            bid=bid,
            ask=ask,
            vol24_usd=vol24,
            vol1h_usd=None,
            fetched_ts=now_s,
        )
    return out


def parse_lighter_snapshots(
    details_payload: Any, funding_payload: Any, *, now_s: int = 0
) -> dict[str, LegMarketInfo]:
    """Normalize Lighter orderBookDetails + funding-rates (exchange=='lighter')."""
    fr_by_sym: dict[str, float] = {}
    if isinstance(funding_payload, dict):
        for r in funding_payload.get("funding_rates", []):
            if r.get("exchange") == "lighter":
                # Lighter /funding-rates gives 8h-scaled or 1h fraction? Wait: keep /fundings as authoritative for r8/r24,
                # and only convert 8h vs 1h safely; actually /fundings `rate * 1e2` is authoritative bps/h.
                fr_by_sym[str(r["symbol"]).upper()] = float(r.get("rate") or 0.0) * 1e4 / 8.0

    out: dict[str, LegMarketInfo] = {}
    nxt = next_hourly_settle_ts(now_s, 1) if now_s > 0 else 0
    rows = details_payload.get("order_book_details", []) if isinstance(details_payload, dict) else []
    for d in rows:
        if d.get("market_type") != "perp" or d.get("status") != "active" or d.get("is_frozen"):
            continue
        sym_raw = str(d["symbol"])
        sym = sym_raw.upper()
        mark = float(d.get("mark_price") or d.get("last_trade_price") or 0.0)
        if mark <= 0:
            continue
        vol24 = float(d.get("daily_quote_token_volume") or 0.0)
        out[sym] = LegMarketInfo(
            venue="LIGHTER",
            symbol=sym,
            raw_symbol=sym_raw,
            instrument_id=f"{sym_raw}-PERP.LIGHTER",
            asset_class=classify_asset(sym),
            interval_h=1,
            next_settle_ts=nxt,
            r8_bps_h=None,
            r24_bps_h=None,
            current_rate_bps_h=fr_by_sym.get(sym),
            mark_price=mark,
            prev_1h_price=None,
            bid=mark,
            ask=mark,
            vol24_usd=vol24,
            vol1h_usd=None,
            fetched_ts=now_s,
            market_id=int(d["market_id"]),
        )
    return out


def parse_bybit_tickers(
    tickers_payload: Any, intervals_by_raw: dict[str, int] | None = None, *, now_s: int = 0
) -> dict[str, LegMarketInfo]:
    """Normalize Bybit /v5/market/tickers?category=linear."""
    rows = tickers_payload.get("result", {}).get("list", []) if isinstance(tickers_payload, dict) else []
    out: dict[str, LegMarketInfo] = {}
    iv_map = intervals_by_raw or {}
    for r in rows:
        raw = str(r.get("symbol", ""))
        if not raw.endswith("USDT"):
            continue
        sym = raw[:-4].upper()
        mark = float(r.get("markPrice") or r.get("lastPrice") or 0.0)
        bid = float(r.get("bid1Price") or mark or 0.0)
        ask = float(r.get("ask1Price") or mark or 0.0)
        if mark <= 0 or bid <= 0 or ask <= 0:
            continue
        iv = int(r.get("fundingIntervalHour") or iv_map.get(raw) or 8)
        fr_raw = r.get("fundingRate")
        rate_bps_h = (float(fr_raw) * 1e4 / max(1, iv)) if fr_raw not in (None, "") else None
        nxt_ms = int(r.get("nextFundingTime") or 0)
        prev_1h = float(r["prevPrice1h"]) if r.get("prevPrice1h") not in (None, "") else None
        vol24 = float(r.get("turnover24h") or 0.0)
        out[sym] = LegMarketInfo(
            venue="BYBIT",
            symbol=sym,
            raw_symbol=raw,
            instrument_id=f"{raw}-LINEAR.BYBIT",
            asset_class=classify_asset(sym),
            interval_h=iv,
            next_settle_ts=nxt_ms // 1000 if nxt_ms > 0 else next_hourly_settle_ts(now_s, iv),
            r8_bps_h=None,
            r24_bps_h=None,
            current_rate_bps_h=rate_bps_h,
            mark_price=mark,
            prev_1h_price=prev_1h,
            bid=bid,
            ask=ask,
            vol24_usd=vol24,
            vol1h_usd=None,
            fetched_ts=now_s,
        )
    return out


def parse_okx_tickers(
    inst_payload: Any,
    tickers_payload: Any,
    funding_payload: Any = None,
    *,
    now_s: int = 0,
) -> dict[str, LegMarketInfo]:
    """Normalize OKX SWAP instruments + tickers (volCcy24h * last = USD quote volume)."""
    live_ids: set[str] = set()
    for d in (inst_payload.get("data", []) if isinstance(inst_payload, dict) else []):
        if d.get("state") == "live" and d.get("settleCcy") == "USDT" and str(d.get("instId", "")).endswith("-USDT-SWAP"):
            live_ids.add(str(d["instId"]))

    funding_by_raw: dict[str, tuple[float | None, int | None, int | None]] = {}
    if isinstance(funding_payload, dict):
        for row in funding_payload.get("data", []):
            if isinstance(row, dict) and row.get("instId"):
                funding_by_raw[str(row["instId"])] = _parse_okx_funding_row(row)

    out: dict[str, LegMarketInfo] = {}
    for t in (tickers_payload.get("data", []) if isinstance(tickers_payload, dict) else []):
        raw = str(t.get("instId", ""))
        if live_ids and raw not in live_ids:
            continue
        if not raw.endswith("-USDT-SWAP"):
            continue
        sym = raw[: -len("-USDT-SWAP")].upper()
        last = float(t.get("last") or 0.0)
        bid = float(t.get("bidPx") or last or 0.0)
        ask = float(t.get("askPx") or last or 0.0)
        if last <= 0 or bid <= 0 or ask <= 0:
            continue
        iv = 8
        nxt_settle = next_hourly_settle_ts(now_s, 8)
        cur_rate_bps_h: float | None = None
        if raw in funding_by_raw:
            raw_fr, live_iv, nxt_from_fr = funding_by_raw[raw]
            if live_iv is not None:
                iv = live_iv
            if nxt_from_fr is not None:
                nxt_settle = nxt_from_fr
            elif live_iv is not None:
                nxt_settle = next_hourly_settle_ts(now_s, iv)
            if raw_fr is not None:
                cur_rate_bps_h = raw_fr * 1e4 / max(1, iv)
        # OKX SWAP volCcy24h is denominated in base currency (coin units)
        vol24_usd = float(t.get("volCcy24h") or 0.0) * last
        out[sym] = LegMarketInfo(
            venue="OKX",
            symbol=sym,
            raw_symbol=raw,
            instrument_id=f"{raw}.OKX",
            asset_class=classify_asset(sym),
            interval_h=iv,
            next_settle_ts=nxt_settle,
            r8_bps_h=None,
            r24_bps_h=None,
            current_rate_bps_h=cur_rate_bps_h,
            mark_price=0.5 * (bid + ask),
            prev_1h_price=None,
            bid=bid,
            ask=ask,
            vol24_usd=vol24_usd,
            vol1h_usd=None,
            fetched_ts=now_s,
        )
    return out


def _validate_bitget_public_list_resp(resp: Any, endpoint: str) -> dict[str, Any]:
    """Validate Bitget v2 public REST envelope returns a list payload (and code==00000 when present)."""
    if not isinstance(resp, dict):
        raise ValueError(f"bitget {endpoint} non-object response")
    if "code" in resp and str(resp.get("code") or "") != "00000":
        raise ValueError(f"bitget {endpoint} error code={resp.get('code')!r}")
    if not isinstance(resp.get("data"), list):
        raise ValueError(f"bitget {endpoint} missing list data")
    return resp


def parse_bitget_tickers(
    contracts_payload: Any,
    tickers_payload: Any,
    current_fund_payload: Any = None,
    *,
    now_s: int = 0,
    close_only_symbols: set[str] | frozenset[str] = frozenset(),
) -> dict[str, LegMarketInfo]:
    """Normalize Bitget USDT-FUTURES contracts + tickers + current-fund-rate."""
    from .bitget_gateway import bitget_instrument_id, parse_bitget_contracts

    contracts_map = parse_bitget_contracts(contracts_payload)
    if not contracts_map:
        return {}
    close_only_allow = {str(s).strip().upper() for s in close_only_symbols}

    fund_by_raw: dict[str, tuple[float | None, int | None, int | None]] = {}
    if isinstance(current_fund_payload, dict):
        frows = current_fund_payload.get("data", [])
        if isinstance(frows, list):
            for row in frows:
                if not isinstance(row, dict):
                    continue
                raw_sym = str(row.get("symbol") or "").strip().upper()
                if raw_sym not in contracts_map:
                    continue
                fr_raw = row.get("fundingRate")
                fr_val: float | None = None
                if fr_raw not in (None, ""):
                    try:
                        fr_val = float(fr_raw)
                    except (TypeError, ValueError):
                        fr_val = None
                iv_raw = row.get("fundingRateInterval")
                live_iv: int | None = None
                if iv_raw not in (None, ""):
                    try:
                        cand_iv = int(iv_raw)
                        if cand_iv > 0:
                            live_iv = cand_iv
                    except (TypeError, ValueError):
                        live_iv = None
                nxt_ms_raw = row.get("nextUpdate")
                nxt_s: int | None = None
                if nxt_ms_raw not in (None, ""):
                    try:
                        nxt_ms = int(nxt_ms_raw)
                        if nxt_ms > 0:
                            nxt_s = nxt_ms // 1000
                    except (TypeError, ValueError):
                        nxt_s = None
                fund_by_raw[raw_sym] = (fr_val, live_iv, nxt_s)

    trows = tickers_payload.get("data", []) if isinstance(tickers_payload, dict) else []
    if not isinstance(trows, list):
        return {}

    out: dict[str, LegMarketInfo] = {}
    for r in trows:
        if not isinstance(r, dict):
            continue
        raw_sym = str(r.get("symbol") or "").strip().upper()
        if raw_sym not in contracts_map:
            continue
        _spec, meta = contracts_map[raw_sym]
        sym = str(meta["symbol"])
        if meta.get("close_only") and sym.upper() not in close_only_allow and raw_sym not in close_only_allow:
            continue
        try:
            mark = float(r.get("markPrice") or r.get("lastPr") or 0.0)
            bid = float(r.get("bidPr") or mark or 0.0)
            ask = float(r.get("askPr") or mark or 0.0)
        except (TypeError, ValueError):
            continue
        if mark <= 0.0 or bid <= 0.0 or ask <= 0.0:
            continue

        iv = int(meta.get("fund_interval_h") or 8)
        fr_val: float | None = None
        nxt_settle: int | None = None
        if raw_sym in fund_by_raw:
            b_fr, b_iv, b_nxt = fund_by_raw[raw_sym]
            if b_iv is not None:
                iv = b_iv
            if b_fr is not None:
                fr_val = b_fr
            if b_nxt is not None:
                nxt_settle = b_nxt
        if fr_val is None and r.get("fundingRate") not in (None, ""):
            try:
                fr_val = float(r["fundingRate"])
            except (TypeError, ValueError):
                fr_val = None
        rate_bps_h = (fr_val * 1e4 / max(1, iv)) if fr_val is not None else None
        if nxt_settle is None:
            nxt_settle = next_hourly_settle_ts(now_s, iv) if now_s > 0 else 0

        vol_raw = r.get("usdtVolume") if r.get("usdtVolume") not in (None, "") else r.get("quoteVolume")
        try:
            vol24 = float(vol_raw or 0.0)
        except (TypeError, ValueError):
            vol24 = 0.0

        asset_class = "RWA" if meta.get("is_rwa") else classify_asset(sym)
        out[sym] = LegMarketInfo(
            venue="BITGET",
            symbol=sym,
            raw_symbol=raw_sym,
            instrument_id=bitget_instrument_id(raw_sym),
            asset_class=asset_class,
            interval_h=iv,
            next_settle_ts=nxt_settle,
            r8_bps_h=None,
            r24_bps_h=None,
            current_rate_bps_h=rate_bps_h,
            mark_price=mark,
            prev_1h_price=None,
            bid=bid,
            ask=ask,
            vol24_usd=vol24,
            vol1h_usd=None,
            fetched_ts=now_s,
        )
    return out


def compute_trailing_rates(settle_events: list[tuple[int, float]], now_s: int, default_iv: int) -> tuple[float | None, float | None, int]:
    """Compute causal trailing 8h and 24h bps/h strictly from settlements <= now_s."""
    past = sorted((ts, val) for ts, val in settle_events if ts <= now_s)
    if not past:
        return None, None, default_iv
    iv = default_iv
    if len(past) >= 2:
        diffs = [(past[i][0] - past[i - 1][0]) // HOUR for i in range(1, len(past)) if past[i][0] > past[i - 1][0]]
        valid_diffs = [d for d in diffs if d in _VALID_INTERVALS_H]
        if valid_diffs:
            iv = valid_diffs[-1]
    cut8 = now_s - 8 * HOUR
    cut24 = now_s - 24 * HOUR
    s8 = [val for ts, val in past if ts > cut8]
    s24 = [val for ts, val in past if ts > cut24]
    r8 = (sum(s8) / 8.0) if s8 else None
    r24 = (sum(s24) / 24.0) if s24 else None
    return r8, r24, iv


class PublicCarryFeed:
    """Slow-layer (60s) read-only public REST aggregator across HL, LIGHTER, OKX, BYBIT."""

    def __init__(
        self,
        venues: tuple[str, ...] = ("HL", "LIGHTER", "OKX", "BYBIT"),
        http_get: Callable[..., Any] = default_http_json,
        *,
        environment: str = "mainnet",
        max_detail_refreshes_per_cycle: int = 35,
        detail_max_workers: int = 4,
    ) -> None:
        if environment not in ("mainnet", "testnet"):
            raise ValueError(f"unsupported environment: {environment!r}")
        self.venues = venues
        self.environment = environment
        is_main = environment == "mainnet"
        self._hl_info_url = HL_INFO_URL if is_main else HL_TESTNET_INFO_URL
        self._lighter_base = LIGHTER_BASE if is_main else LIGHTER_TESTNET_BASE
        from .bitget_gateway import BITGET_REST_BASE

        self._bybit_base = BYBIT_BASE if is_main else BYBIT_TESTNET_BASE
        self._okx_base = OKX_BASE
        self._okx_headers: dict[str, str] | None = None if is_main else {"x-simulated-trading": "1"}
        self._bitget_base = BITGET_REST_BASE
        self._bitget_headers: dict[str, str] | None = None if is_main else {"paptrading": "1"}
        self._http = http_get
        self._http_accepts_headers = _callable_accepts_headers(http_get)
        self._max_detail_refreshes = max_detail_refreshes_per_cycle
        self._detail_max_workers = max(1, int(detail_max_workers))
        self._okx_inst_cache: Any = None
        self._okx_inst_ts: int = 0
        self._byb_iv_cache: dict[str, int] = {}
        self._byb_iv_ts: int = 0
        self._bitget_contracts_cache: Any = None
        self._bitget_contracts_ts: int = 0
        self._bitget_specs: dict[str, InstrumentSpec] = {}
        self._bitget_contracts_refresh_error: str | None = None
        self._bitget_contracts_expired: bool = False
        self._bitget_entry_paused: bool = False
        self._bitget_meta_lock = threading.Lock()
        self._detail_cache: dict[tuple[str, str], dict[str, Any]] = {}
        self._last_detail_attempt_ts: dict[str, int] = {}
        self.last_venue_errors: dict[str, str] = {}

    def bitget_instrument_specs(self) -> dict[str, InstrumentSpec]:
        """Return cached Bitget InstrumentSpec entries keyed by instrument_id."""
        with self._bitget_meta_lock:
            if self._bitget_entry_paused:
                return {
                    iid: replace(spec, close_only=True)
                    for iid, spec in self._bitget_specs.items()
                }
            return dict(self._bitget_specs)

    def _request(
        self,
        url: str,
        body: dict | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if headers and self._http_accepts_headers:
            return self._http(url, body, headers=dict(headers))
        return self._http(url, body)

    def _refresh_venue_metadata(self, now_s: int) -> None:
        if "OKX" in self.venues and (self._okx_inst_cache is None or now_s - self._okx_inst_ts >= 3600):
            try:
                self._okx_inst_cache = self._request(
                    f"{self._okx_base}/api/v5/public/instruments?instType=SWAP",
                    None,
                    headers=self._okx_headers,
                )
                self._okx_inst_ts = now_s
            except Exception:
                pass
        if "BYBIT" in self.venues and (not self._byb_iv_cache or now_s - self._byb_iv_ts >= 3600):
            try:
                res = self._request(f"{self._bybit_base}/v5/market/instruments-info?category=linear&limit=1000", None).get(
                    "result", {}
                )
                for d in res.get("list", []):
                    if d.get("fundingInterval"):
                        self._byb_iv_cache[str(d["symbol"])] = max(1, round(int(d["fundingInterval"]) / 60))
                self._byb_iv_ts = now_s
            except Exception:
                pass
        if "BITGET" in self.venues:
            with self._bitget_meta_lock:
                needs_refresh = self._bitget_contracts_cache is None or (now_s - self._bitget_contracts_ts) >= 3600
            if needs_refresh:
                try:
                    from .bitget_gateway import parse_bitget_contracts

                    contracts_payload = _validate_bitget_public_list_resp(
                        self._request(
                            f"{self._bitget_base}/api/v2/mix/market/contracts?productType=USDT-FUTURES",
                            None,
                            headers=self._bitget_headers,
                        ),
                        "contracts",
                    )
                    parsed = parse_bitget_contracts(contracts_payload)
                    with self._bitget_meta_lock:
                        self._bitget_contracts_cache = contracts_payload
                        self._bitget_contracts_ts = now_s
                        self._bitget_specs = {spec.instrument_id: spec for spec, _ in parsed.values()}
                        self._bitget_contracts_refresh_error = None
                        self._bitget_contracts_expired = False
                        self._bitget_entry_paused = False
                except Exception as exc:
                    with self._bitget_meta_lock:
                        self._bitget_contracts_refresh_error = f"contracts_refresh_failed:{type(exc).__name__}: {exc}"
                        if self._bitget_contracts_cache is not None and (now_s - self._bitget_contracts_ts) >= 3600:
                            self._bitget_contracts_expired = True
                            self._bitget_entry_paused = True

    def _is_detail_fresh(self, venue: str, symbol: str, now_s: int) -> bool:
        cached = self._detail_cache.get((venue, symbol))
        return cached is not None and now_s - cached["fetched_ts"] < 300

    def _apply_cached_detail(
        self,
        leg: LegMarketInfo,
        cached: Mapping[str, Any],
        *,
        okx_bulk_funding: Mapping[str, tuple[float | None, int | None, int | None]] | None = None,
    ) -> LegMarketInfo:
        r8, r24, iv = cached["r8"], cached["r24"], cached["iv"]
        nxt = cached.get("next_settle_ts") or leg.next_settle_ts
        cur_rate = leg.current_rate_bps_h if leg.current_rate_bps_h is not None else cached.get("current_rate_bps_h")
        if leg.venue == "OKX" and okx_bulk_funding and leg.raw_symbol in okx_bulk_funding:
            b_raw_fr, b_live_iv, b_nxt = okx_bulk_funding[leg.raw_symbol]
            if b_live_iv is not None:
                iv = b_live_iv
            if b_nxt is not None:
                nxt = b_nxt
            if b_raw_fr is not None:
                cur_rate = b_raw_fr * 1e4 / max(1, iv)
        elif leg.venue == "BITGET":
            if leg.interval_h > 0:
                iv = leg.interval_h
            if leg.next_settle_ts > 0:
                nxt = leg.next_settle_ts
        return LegMarketInfo(
            **{
                **leg.__dict__,
                "r8_bps_h": r8,
                "r24_bps_h": r24,
                "current_rate_bps_h": cur_rate,
                "interval_h": iv,
                "next_settle_ts": nxt,
                "prev_1h_price": cached.get("prev_1h_price", leg.prev_1h_price),
                "vol1h_usd": cached.get("vol1h_usd"),
            }
        )

    def _fetch_leg_detail_uncached(
        self,
        leg: LegMarketInfo,
        now_s: int,
        *,
        okx_bulk_funding: Mapping[str, tuple[float | None, int | None, int | None]] | None = None,
    ) -> tuple[LegMarketInfo, dict[str, Any]]:
        """Fetch trailing 24h settlements & latest 2h 1h-candles without mutating shared cache."""
        start_s = now_s - 28 * HOUR
        settle_events: list[tuple[int, float]] = []
        prev_1h: float | None = leg.prev_1h_price
        vol1h: float | None = leg.vol1h_usd
        nxt_settle = leg.next_settle_ts
        cur_rate_bps_h: float | None = leg.current_rate_bps_h
        live_iv: int | None = None
        raw_fr: float | None = None

        if leg.venue == "HL":
            rows = self._request(
                self._hl_info_url,
                {"type": "fundingHistory", "coin": leg.raw_symbol, "startTime": start_s * 1000, "endTime": now_s * 1000},
            )
            if isinstance(rows, list):
                settle_events = [((int(r["time"]) // HOUR_MS) * HOUR, float(r["fundingRate"]) * 1e4) for r in rows]
            crows = self._request(
                self._hl_info_url,
                {
                    "type": "candleSnapshot",
                    "req": {
                        "coin": leg.raw_symbol,
                        "interval": "1h",
                        "startTime": (now_s - 3 * HOUR) * 1000,
                        "endTime": now_s * 1000,
                    },
                },
            )
            if isinstance(crows, list) and len(crows) >= 2:
                crows = sorted(crows, key=lambda r: int(r["t"]))
                prev_1h = float(crows[-2]["c"])
                vol1h = float(crows[-1]["v"]) * float(crows[-1]["c"])
            elif isinstance(crows, list) and len(crows) == 1:
                vol1h = float(crows[0]["v"]) * float(crows[0]["c"])

        elif leg.venue == "LIGHTER" and leg.market_id is not None:
            fp = self._request(
                f"{self._lighter_base}/api/v1/fundings?market_id={leg.market_id}&resolution=1h&start_timestamp={start_s}&end_timestamp={now_s}&count_back=40",
                None,
            )
            for r in (fp.get("fundings", []) if isinstance(fp, dict) else []):
                val = abs(float(r["rate"])) * 1e2
                settle_events.append(((int(r["timestamp"]) // HOUR) * HOUR, val if r["direction"] == "long" else -val))
            cp = self._request(
                f"{self._lighter_base}/api/v1/candles?market_id={leg.market_id}&resolution=1h&start_timestamp={now_s - 3 * HOUR}&end_timestamp={now_s}&count_back=5&set_timestamp_to_end=false",
                None,
            )
            crows = sorted((cp.get("c", []) if isinstance(cp, dict) else []), key=lambda r: int(r["t"]))
            if crows:
                last_c = crows[-1]
                vol1h = float(last_c.get("V") or 0.0) or float(last_c.get("v") or 0.0) * float(last_c["c"])
                if len(crows) >= 2:
                    prev_1h = float(crows[-2]["c"])

        elif leg.venue == "OKX":
            q = urllib.parse.quote(leg.raw_symbol)
            bulk_row = okx_bulk_funding.get(leg.raw_symbol) if okx_bulk_funding else None
            if bulk_row is not None and bulk_row[0] is not None:
                raw_fr, live_iv, nxt_from_fr = bulk_row
            else:
                fr_resp = self._request(
                    f"{self._okx_base}/api/v5/public/funding-rate?instId={q}",
                    None,
                    headers=self._okx_headers,
                )
                fr_cur = fr_resp.get("data", []) if isinstance(fr_resp, dict) else []
                if fr_cur:
                    raw_fr, live_iv, nxt_from_fr = _parse_okx_funding_row(fr_cur[0])
                else:
                    raw_fr, live_iv, nxt_from_fr = (None, None, None)
            if nxt_from_fr is not None:
                nxt_settle = nxt_from_fr

            fh_resp = self._request(
                f"{self._okx_base}/api/v5/public/funding-rate-history?instId={q}&limit=30",
                None,
                headers=self._okx_headers,
            )
            fh = fh_resp.get("data", []) if isinstance(fh_resp, dict) else []
            for r in fh:
                ts = ((int(r["fundingTime"]) + 60_000) // HOUR_MS) * HOUR
                val = float(r.get("realizedRate") or r["fundingRate"]) * 1e4
                settle_events.append((ts, val))
            cd_resp = self._request(
                f"{self._okx_base}/api/v5/market/candles?instId={q}&bar=1H&limit=3",
                None,
                headers=self._okx_headers,
            )
            cd = cd_resp.get("data", []) if isinstance(cd_resp, dict) else []
            if cd:
                cd = sorted(cd, key=lambda r: int(r[0]))
                vol1h = float(cd[-1][7]) if len(cd[-1]) > 7 else float(cd[-1][5]) * float(cd[-1][4])
                if len(cd) >= 2:
                    prev_1h = float(cd[-2][4])

        elif leg.venue == "BYBIT":
            q = urllib.parse.quote(leg.raw_symbol)
            fh = (
                self._request(
                    f"{self._bybit_base}/v5/market/funding/history?category=linear&symbol={q}&limit=30",
                    None,
                )
                .get("result", {})
                .get("list", [])
            )
            for r in fh:
                ts = ((int(r["fundingRateTimestamp"]) + 60_000) // HOUR_MS) * HOUR
                settle_events.append((ts, float(r["fundingRate"]) * 1e4))
            cd = (
                self._request(
                    f"{self._bybit_base}/v5/market/kline?category=linear&symbol={q}&interval=60&limit=3",
                    None,
                )
                .get("result", {})
                .get("list", [])
            )
            if cd:
                cd = sorted(cd, key=lambda r: int(r[0]))
                vol1h = float(cd[-1][6])
                if len(cd) >= 2:
                    prev_1h = float(cd[-2][4])

        elif leg.venue == "BITGET":
            live_iv = leg.interval_h
            q = urllib.parse.quote(leg.raw_symbol)
            fh_resp = _validate_bitget_public_list_resp(
                self._request(
                    f"{self._bitget_base}/api/v2/mix/market/history-fund-rate?productType=USDT-FUTURES&symbol={q}&pageSize=30",
                    None,
                    headers=self._bitget_headers,
                ),
                "history-fund-rate",
            )
            for r in fh_resp["data"]:
                ts = ((int(r["fundingTime"]) + 60_000) // HOUR_MS) * HOUR
                settle_events.append((ts, float(r["fundingRate"]) * 1e4))
            cd_resp = _validate_bitget_public_list_resp(
                self._request(
                    f"{self._bitget_base}/api/v2/mix/market/candles?productType=USDT-FUTURES&symbol={q}&granularity=1H&limit=3",
                    None,
                    headers=self._bitget_headers,
                ),
                "candles",
            )
            cd = cd_resp["data"]
            if cd:
                cd = sorted(cd, key=lambda r: int(r[0]))
                vol1h = float(cd[-1][6]) if len(cd[-1]) > 6 else float(cd[-1][5]) * float(cd[-1][4])
                if len(cd) >= 2:
                    prev_1h = float(cd[-2][4])

        r8, r24, iv = compute_trailing_rates(settle_events, now_s, live_iv or leg.interval_h)
        if live_iv is not None:
            iv = live_iv
        if leg.venue == "OKX" and raw_fr is not None:
            cur_rate_bps_h = raw_fr * 1e4 / max(1, iv)
        sorted_settles = tuple(sorted({int(ts): float(val) for ts, val in settle_events}.items()))
        entry = {
            "fetched_ts": now_s,
            "r8": r8,
            "r24": r24,
            "current_rate_bps_h": cur_rate_bps_h,
            "iv": iv,
            "next_settle_ts": nxt_settle,
            "prev_1h_price": prev_1h,
            "vol1h_usd": vol1h,
            "settle_events": sorted_settles,
        }
        enriched = LegMarketInfo(
            **{
                **leg.__dict__,
                "r8_bps_h": r8,
                "r24_bps_h": r24,
                "current_rate_bps_h": cur_rate_bps_h,
                "interval_h": iv,
                "next_settle_ts": nxt_settle,
                "prev_1h_price": prev_1h,
                "vol1h_usd": vol1h,
            }
        )
        return enriched, entry

    def _fetch_leg_detail(
        self,
        leg: LegMarketInfo,
        now_s: int,
        *,
        okx_bulk_funding: Mapping[str, tuple[float | None, int | None, int | None]] | None = None,
    ) -> LegMarketInfo:
        """Fetch trailing 24h settlements & latest 2h 1h-candles for one overlapping leg."""
        key = (leg.venue, leg.symbol)
        if self._is_detail_fresh(leg.venue, leg.symbol, now_s):
            return self._apply_cached_detail(leg, self._detail_cache[key], okx_bulk_funding=okx_bulk_funding)
        enriched, entry = self._fetch_leg_detail_uncached(leg, now_s, okx_bulk_funding=okx_bulk_funding)
        self._detail_cache[key] = entry
        return enriched

    def recent_settlements(self, venue: str, symbol: str) -> tuple[tuple[int, float], ...]:
        """Return cached (settle_ts, rate_bps) history events for (venue, symbol)."""
        cached = self._detail_cache.get((venue.upper(), symbol.upper()))
        if not cached:
            return ()
        return tuple(cached.get("settle_events", ()))

    def poll_snapshot(
        self,
        *,
        now_s: int | None = None,
        priority_symbols: set[str] | None = None,
        min_24h_vol_usd: float = 500_000.0,
        include_details: bool = True,
    ) -> dict[str, dict[str, LegMarketInfo]]:
        """Poll bulk snapshots across all configured venues and enrich active overlapping legs."""
        ts = int(time.time()) if now_s is None else now_s
        self._refresh_venue_metadata(ts)
        prio = {s.upper() for s in (priority_symbols or set())}

        by_sym: dict[str, dict[str, LegMarketInfo]] = {}
        succeeded_venues: list[str] = []
        venue_errors: dict[str, str] = {}

        def _merge(legs: dict[str, LegMarketInfo]) -> None:
            for sym, leg in legs.items():
                by_sym.setdefault(sym, {})[leg.venue] = leg

        if "HL" in self.venues:
            hl_ok = False
            hl_errs: list[str] = []
            try:
                _merge(parse_hl_meta_and_ctxs(self._request(self._hl_info_url, {"type": "metaAndAssetCtxs"}), is_xyz=False, now_s=ts))
                hl_ok = True
            except Exception as exc:
                hl_errs.append(f"{type(exc).__name__}: {exc}")
            if self.environment == "mainnet":
                try:
                    _merge(
                        parse_hl_meta_and_ctxs(
                            self._request(self._hl_info_url, {"type": "metaAndAssetCtxs", "dex": "xyz"}),
                            is_xyz=True,
                            now_s=ts,
                        )
                    )
                    hl_ok = True
                except Exception as exc:
                    hl_errs.append(f"xyz:{type(exc).__name__}: {exc}")
            if hl_ok:
                succeeded_venues.append("HL")
            elif hl_errs:
                venue_errors["HL"] = "; ".join(hl_errs)
        if "LIGHTER" in self.venues:
            try:
                det = self._request(f"{self._lighter_base}/api/v1/orderBookDetails", None)
                fr = self._request(f"{self._lighter_base}/api/v1/funding-rates", None)
                _merge(parse_lighter_snapshots(det, fr, now_s=ts))
                succeeded_venues.append("LIGHTER")
            except Exception as exc:
                venue_errors["LIGHTER"] = f"{type(exc).__name__}: {exc}"
        if "BYBIT" in self.venues:
            try:
                tk = self._request(f"{self._bybit_base}/v5/market/tickers?category=linear", None)
                _merge(parse_bybit_tickers(tk, self._byb_iv_cache, now_s=ts))
                succeeded_venues.append("BYBIT")
            except Exception as exc:
                venue_errors["BYBIT"] = f"{type(exc).__name__}: {exc}"
        okx_bulk_fr: dict[str, tuple[float | None, int | None, int | None]] = {}
        if "OKX" in self.venues:
            try:
                tk = self._request(
                    f"{self._okx_base}/api/v5/market/tickers?instType=SWAP",
                    None,
                    headers=self._okx_headers,
                )
                fr_payload = None
                try:
                    fr_payload = self._request(
                        f"{self._okx_base}/api/v5/public/funding-rate?instId=ANY",
                        None,
                        headers=self._okx_headers,
                    )
                except Exception:
                    fr_payload = None
                if isinstance(fr_payload, dict):
                    for row in fr_payload.get("data", []):
                        if isinstance(row, dict) and row.get("instId"):
                            parsed = _parse_okx_funding_row(row)
                            if any(val is not None for val in parsed):
                                okx_bulk_fr[str(row["instId"])] = parsed
                _merge(parse_okx_tickers(self._okx_inst_cache, tk, fr_payload, now_s=ts))
                succeeded_venues.append("OKX")
            except Exception as exc:
                venue_errors["OKX"] = f"{type(exc).__name__}: {exc}"
        if "BITGET" in self.venues:
            try:
                with self._bitget_meta_lock:
                    contracts_payload = self._bitget_contracts_cache
                    contracts_expired = (
                        contracts_payload is not None
                        and self._bitget_contracts_expired
                        and (ts - self._bitget_contracts_ts) >= 3600
                        and self._bitget_contracts_refresh_error is not None
                    )
                    contracts_err = self._bitget_contracts_refresh_error
                if contracts_payload is None:
                    from .bitget_gateway import parse_bitget_contracts

                    contracts_payload = _validate_bitget_public_list_resp(
                        self._request(
                            f"{self._bitget_base}/api/v2/mix/market/contracts?productType=USDT-FUTURES",
                            None,
                            headers=self._bitget_headers,
                        ),
                        "contracts",
                    )
                    parsed = parse_bitget_contracts(contracts_payload)
                    with self._bitget_meta_lock:
                        self._bitget_contracts_cache = contracts_payload
                        self._bitget_contracts_ts = ts
                        self._bitget_specs = {spec.instrument_id: spec for spec, _ in parsed.values()}
                        self._bitget_contracts_refresh_error = None
                        self._bitget_contracts_expired = False
                        self._bitget_entry_paused = False
                    contracts_expired = False
                    contracts_err = None
                tk = _validate_bitget_public_list_resp(
                    self._request(
                        f"{self._bitget_base}/api/v2/mix/market/tickers?productType=USDT-FUTURES",
                        None,
                        headers=self._bitget_headers,
                    ),
                    "tickers",
                )
                fr_payload = _validate_bitget_public_list_resp(
                    self._request(
                        f"{self._bitget_base}/api/v2/mix/market/current-fund-rate?productType=USDT-FUTURES",
                        None,
                        headers=self._bitget_headers,
                    ),
                    "current-fund-rate",
                )
                bg_legs = parse_bitget_tickers(
                    contracts_payload,
                    tk,
                    fr_payload,
                    now_s=ts,
                    close_only_symbols=prio,
                )
                if contracts_expired:
                    venue_errors["BITGET"] = contracts_err or "contracts_metadata_expired"
                    bg_legs = {sym: leg for sym, leg in bg_legs.items() if sym in prio}
                    if bg_legs:
                        _merge(bg_legs)
                        succeeded_venues.append("BITGET")
                else:
                    _merge(bg_legs)
                    succeeded_venues.append("BITGET")
            except Exception as exc:
                venue_errors["BITGET"] = f"{type(exc).__name__}: {exc}"

        self.last_venue_errors = dict(venue_errors)
        if self.venues and not succeeded_venues:
            details = "; ".join(f"{v}={err}" for v, err in sorted(venue_errors.items()))
            raise SlowPollError(f"all configured venues failed slow poll ({details})")

        if not include_details:
            return by_sym

        # Filter to overlapping multi-venue symbols that meet base liquidity (or are priority/open)
        fresh_syms: list[str] = []
        refresh_cands: list[tuple[bool, int, float, str]] = []
        for sym, vmap in by_sym.items():
            if len(vmap) < 2:
                continue
            elig_venues = [v for v, leg in vmap.items() if leg.vol24_usd >= min_24h_vol_usd or sym in prio]
            if sym in prio or len(elig_venues) >= 2:
                if elig_venues and all(self._is_detail_fresh(v, sym, ts) for v in elig_venues):
                    fresh_syms.append(sym)
                    continue
                # Score prioritization by oldest refresh attempt (anti-starvation), then max current rate diff
                rates = [leg.current_rate_bps_h for leg in vmap.values() if leg.current_rate_bps_h is not None]
                spread = (max(rates) - min(rates)) if len(rates) >= 2 else 0.0
                last_attempt = self._last_detail_attempt_ts.get(sym, -1)
                refresh_cands.append((sym in prio, last_attempt, spread, sym))

        for sym in fresh_syms:
            vmap = by_sym[sym]
            for v, leg in list(vmap.items()):
                if leg.vol24_usd < min_24h_vol_usd and sym not in prio:
                    continue
                cached = self._detail_cache.get((v, sym))
                if cached is not None:
                    vmap[v] = self._apply_cached_detail(leg, cached, okx_bulk_funding=okx_bulk_fr)

        refresh_cands.sort(key=lambda x: (not x[0], x[1], -x[2], x[3]))
        selected = [s for _, _, _, s in refresh_cands[: self._max_detail_refreshes]]
        detail_tasks: list[tuple[str, str, LegMarketInfo]] = []
        for sym in selected:
            self._last_detail_attempt_ts[sym] = ts
            vmap = by_sym[sym]
            for v, leg in list(vmap.items()):
                if leg.vol24_usd < min_24h_vol_usd and sym not in prio:
                    continue
                if self._is_detail_fresh(v, sym, ts):
                    vmap[v] = self._apply_cached_detail(leg, self._detail_cache[(v, sym)], okx_bulk_funding=okx_bulk_fr)
                else:
                    detail_tasks.append((sym, v, leg))

        if len(detail_tasks) <= 1 or self._detail_max_workers <= 1:
            for sym, v, leg in detail_tasks:
                try:
                    enriched, entry = self._fetch_leg_detail_uncached(leg, ts, okx_bulk_funding=okx_bulk_fr)
                    self._detail_cache[(v, sym)] = entry
                    by_sym[sym][v] = enriched
                except Exception:
                    pass
        else:
            workers = min(self._detail_max_workers, len(detail_tasks))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="carry-detail") as pool:
                futures = [
                    (
                        sym,
                        v,
                        pool.submit(self._fetch_leg_detail_uncached, leg, ts, okx_bulk_funding=okx_bulk_fr),
                    )
                    for sym, v, leg in detail_tasks
                ]
                for sym, v, fut in futures:
                    try:
                        enriched, entry = fut.result()
                        self._detail_cache[(v, sym)] = entry
                        by_sym[sym][v] = enriched
                    except Exception:
                        pass

        return by_sym

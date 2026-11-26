"""Read-only public REST slow-layer aggregator for HL, Lighter, OKX, and Bybit."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import time
from typing import Any, Callable
import urllib.error
import urllib.parse
import urllib.request

HOUR = 3600
HOUR_MS = 3_600_000

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
LIGHTER_BASE = "https://mainnet.zklighter.elliot.ai"
OKX_BASE = "https://www.okx.com"
BYBIT_BASE = "https://api.bybit.com"
USER_AGENT = "Mozilla/5.0 (compatible; NautilusPerpsCarry/1.0)"

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


def default_http_json(url: str, body: dict | None = None, *, timeout: float = 12.0) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


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
    inst_payload: Any, tickers_payload: Any, *, now_s: int = 0
) -> dict[str, LegMarketInfo]:
    """Normalize OKX SWAP instruments + tickers (volCcy24h * last = USD quote volume)."""
    live_ids: set[str] = set()
    for d in (inst_payload.get("data", []) if isinstance(inst_payload, dict) else []):
        if d.get("state") == "live" and d.get("settleCcy") == "USDT" and str(d.get("instId", "")).endswith("-USDT-SWAP"):
            live_ids.add(str(d["instId"]))

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
        # OKX SWAP volCcy24h is denominated in base currency (coin units)
        vol24_usd = float(t.get("volCcy24h") or 0.0) * last
        out[sym] = LegMarketInfo(
            venue="OKX",
            symbol=sym,
            raw_symbol=raw,
            instrument_id=f"{raw}.OKX",
            asset_class=classify_asset(sym),
            interval_h=8,
            next_settle_ts=next_hourly_settle_ts(now_s, 8),
            r8_bps_h=None,
            r24_bps_h=None,
            current_rate_bps_h=None,
            mark_price=0.5 * (bid + ask),
            prev_1h_price=None,
            bid=bid,
            ask=ask,
            vol24_usd=vol24_usd,
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
        diffs = sorted((past[i][0] - past[i - 1][0]) // HOUR for i in range(1, len(past)) if past[i][0] > past[i - 1][0])
        valid_diffs = [d for d in diffs if d in (1, 2, 4, 8)]
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
        http_get: Callable[[str, dict | None], Any] = default_http_json,
        *,
        max_detail_refreshes_per_cycle: int = 35,
    ) -> None:
        self.venues = venues
        self._http = http_get
        self._max_detail_refreshes = max_detail_refreshes_per_cycle
        self._okx_inst_cache: Any = None
        self._okx_inst_ts: int = 0
        self._byb_iv_cache: dict[str, int] = {}
        self._byb_iv_ts: int = 0
        self._detail_cache: dict[tuple[str, str], dict[str, Any]] = {}

    def _refresh_venue_metadata(self, now_s: int) -> None:
        if "OKX" in self.venues and (self._okx_inst_cache is None or now_s - self._okx_inst_ts >= 3600):
            try:
                self._okx_inst_cache = self._http(f"{OKX_BASE}/api/v5/public/instruments?instType=SWAP", None)
                self._okx_inst_ts = now_s
            except Exception:
                pass
        if "BYBIT" in self.venues and (not self._byb_iv_cache or now_s - self._byb_iv_ts >= 3600):
            try:
                res = self._http(f"{BYBIT_BASE}/v5/market/instruments-info?category=linear&limit=1000", None).get("result", {})
                for d in res.get("list", []):
                    if d.get("fundingInterval"):
                        self._byb_iv_cache[str(d["symbol"])] = max(1, round(int(d["fundingInterval"]) / 60))
                self._byb_iv_ts = now_s
            except Exception:
                pass

    def _fetch_leg_detail(self, leg: LegMarketInfo, now_s: int) -> LegMarketInfo:
        """Fetch trailing 24h settlements & latest 2h 1h-candles for one overlapping leg."""
        key = (leg.venue, leg.symbol)
        cached = self._detail_cache.get(key)
        # Re-use cached trailing settlements/candles for 5 minutes unless near a new hour
        if cached and now_s - cached["fetched_ts"] < 300:
            r8, r24, iv = cached["r8"], cached["r24"], cached["iv"]
            nxt = cached.get("next_settle_ts") or leg.next_settle_ts
            cur_rate = leg.current_rate_bps_h if leg.current_rate_bps_h is not None else cached.get("current_rate_bps_h")
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

        start_s = now_s - 28 * HOUR
        settle_events: list[tuple[int, float]] = []
        prev_1h: float | None = leg.prev_1h_price
        vol1h: float | None = leg.vol1h_usd
        nxt_settle = leg.next_settle_ts
        cur_rate_bps_h: float | None = leg.current_rate_bps_h

        if leg.venue == "HL":
            rows = self._http(HL_INFO_URL, {"type": "fundingHistory", "coin": leg.raw_symbol, "startTime": start_s * 1000, "endTime": now_s * 1000})
            if isinstance(rows, list):
                settle_events = [((int(r["time"]) // HOUR_MS) * HOUR, float(r["fundingRate"]) * 1e4) for r in rows]
            crows = self._http(HL_INFO_URL, {"type": "candleSnapshot", "req": {"coin": leg.raw_symbol, "interval": "1h", "startTime": (now_s - 3 * HOUR) * 1000, "endTime": now_s * 1000}})
            if isinstance(crows, list) and len(crows) >= 2:
                crows = sorted(crows, key=lambda r: int(r["t"]))
                prev_1h = float(crows[-2]["c"])
                vol1h = float(crows[-1]["v"]) * float(crows[-1]["c"])
            elif isinstance(crows, list) and len(crows) == 1:
                vol1h = float(crows[0]["v"]) * float(crows[0]["c"])

        elif leg.venue == "LIGHTER" and leg.market_id is not None:
            fp = self._http(f"{LIGHTER_BASE}/api/v1/fundings?market_id={leg.market_id}&resolution=1h&start_timestamp={start_s}&end_timestamp={now_s}&count_back=40", None)
            for r in (fp.get("fundings", []) if isinstance(fp, dict) else []):
                val = abs(float(r["rate"])) * 1e2
                settle_events.append(((int(r["timestamp"]) // HOUR) * HOUR, val if r["direction"] == "long" else -val))
            cp = self._http(f"{LIGHTER_BASE}/api/v1/candles?market_id={leg.market_id}&resolution=1h&start_timestamp={now_s - 3 * HOUR}&end_timestamp={now_s}&count_back=5&set_timestamp_to_end=false", None)
            crows = sorted((cp.get("c", []) if isinstance(cp, dict) else []), key=lambda r: int(r["t"]))
            if crows:
                last_c = crows[-1]
                vol1h = float(last_c.get("V") or 0.0) or float(last_c.get("v") or 0.0) * float(last_c["c"])
                if len(crows) >= 2:
                    prev_1h = float(crows[-2]["c"])

        elif leg.venue == "OKX":
            q = urllib.parse.quote(leg.raw_symbol)
            fr_cur = self._http(f"{OKX_BASE}/api/v5/public/funding-rate?instId={q}", None).get("data", [])
            if fr_cur:
                cur_row = fr_cur[0]
                nxt_ms = int(cur_row.get("fundingTime") or 0)
                if nxt_ms > 0:
                    nxt_settle = nxt_ms // 1000
                prev_ms = int(cur_row.get("prevFundingTime") or 0)
                if nxt_ms > prev_ms > 0:
                    leg_iv = max(1, round((nxt_ms - prev_ms) / HOUR_MS))
                else:
                    leg_iv = leg.interval_h
            else:
                leg_iv = leg.interval_h
            if fr_cur:
                fr_val = fr_cur[0].get("fundingRate")
                if fr_val not in (None, ""):
                    cur_rate_bps_h = float(fr_val) * 1e4 / max(1, leg_iv)
            fh = self._http(f"{OKX_BASE}/api/v5/public/funding-rate-history?instId={q}&limit=10", None).get("data", [])
            for r in fh:
                ts = ((int(r["fundingTime"]) + 60_000) // HOUR_MS) * HOUR
                val = float(r.get("realizedRate") or r["fundingRate"]) * 1e4
                settle_events.append((ts, val))
            cd = self._http(f"{OKX_BASE}/api/v5/market/candles?instId={q}&bar=1H&limit=3", None).get("data", [])
            if cd:
                cd = sorted(cd, key=lambda r: int(r[0]))
                vol1h = float(cd[-1][7]) if len(cd[-1]) > 7 else float(cd[-1][5]) * float(cd[-1][4])
                if len(cd) >= 2:
                    prev_1h = float(cd[-2][4])

        elif leg.venue == "BYBIT":
            q = urllib.parse.quote(leg.raw_symbol)
            fh = self._http(f"{BYBIT_BASE}/v5/market/funding/history?category=linear&symbol={q}&limit=10", None).get("result", {}).get("list", [])
            for r in fh:
                ts = ((int(r["fundingRateTimestamp"]) + 60_000) // HOUR_MS) * HOUR
                settle_events.append((ts, float(r["fundingRate"]) * 1e4))
            cd = self._http(f"{BYBIT_BASE}/v5/market/kline?category=linear&symbol={q}&interval=60&limit=3", None).get("result", {}).get("list", [])
            if cd:
                cd = sorted(cd, key=lambda r: int(r[0]))
                vol1h = float(cd[-1][6])
                if len(cd) >= 2:
                    prev_1h = float(cd[-2][4])

        r8, r24, iv = compute_trailing_rates(settle_events, now_s, leg.interval_h)
        if leg.venue == "OKX" and settle_events:
            pass  # iv inferred accurately from funding-rate-history diffs
        self._detail_cache[key] = {
            "fetched_ts": now_s,
            "r8": r8,
            "r24": r24,
            "current_rate_bps_h": cur_rate_bps_h,
            "iv": iv,
            "next_settle_ts": nxt_settle,
            "prev_1h_price": prev_1h,
            "vol1h_usd": vol1h,
        }
        return LegMarketInfo(
            **{
                **leg.__dict__,
                "r8_bps_h": r8,
                "r24_bps_h": r24,
                "current_rate_bps_h": cur_rate_bps_h if leg.current_rate_bps_h is None else leg.current_rate_bps_h,
                "interval_h": iv,
                "next_settle_ts": nxt_settle,
                "prev_1h_price": prev_1h,
                "vol1h_usd": vol1h,
            }
        )

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

        by_sym: dict[str, dict[str, LegMarketInfo]] = {}

        def _merge(legs: dict[str, LegMarketInfo]) -> None:
            for sym, leg in legs.items():
                by_sym.setdefault(sym, {})[leg.venue] = leg

        if "HL" in self.venues:
            try:
                _merge(parse_hl_meta_and_ctxs(self._http(HL_INFO_URL, {"type": "metaAndAssetCtxs"}), is_xyz=False, now_s=ts))
                _merge(parse_hl_meta_and_ctxs(self._http(HL_INFO_URL, {"type": "metaAndAssetCtxs", "dex": "xyz"}), is_xyz=True, now_s=ts))
            except Exception:
                pass
        if "LIGHTER" in self.venues:
            try:
                det = self._http(f"{LIGHTER_BASE}/api/v1/orderBookDetails", None)
                fr = self._http(f"{LIGHTER_BASE}/api/v1/funding-rates", None)
                _merge(parse_lighter_snapshots(det, fr, now_s=ts))
            except Exception:
                pass
        if "BYBIT" in self.venues:
            try:
                tk = self._http(f"{BYBIT_BASE}/v5/market/tickers?category=linear", None)
                _merge(parse_bybit_tickers(tk, self._byb_iv_cache, now_s=ts))
            except Exception:
                pass
        if "OKX" in self.venues:
            try:
                tk = self._http(f"{OKX_BASE}/api/v5/market/tickers?instType=SWAP", None)
                _merge(parse_okx_tickers(self._okx_inst_cache, tk, now_s=ts))
            except Exception:
                pass

        if not include_details:
            return by_sym

        # Filter to overlapping multi-venue symbols that meet base liquidity (or are priority/open)
        prio = {s.upper() for s in (priority_symbols or set())}
        overlap_syms = []
        for sym, vmap in by_sym.items():
            if len(vmap) < 2:
                continue
            liq_venues = [v for v, leg in vmap.items() if leg.vol24_usd >= min_24h_vol_usd]
            if sym in prio or len(liq_venues) >= 2:
                # Score prioritization by max absolute current rate diff when available
                rates = [leg.current_rate_bps_h for leg in vmap.values() if leg.current_rate_bps_h is not None]
                spread = (max(rates) - min(rates)) if len(rates) >= 2 else 0.0
                overlap_syms.append((sym in prio, spread, sym))

        overlap_syms.sort(key=lambda x: (not x[0], -x[1], x[2]))
        selected = [s for _, _, s in overlap_syms[: self._max_detail_refreshes]]
        for sym in selected:
            vmap = by_sym[sym]
            for v, leg in list(vmap.items()):
                if leg.vol24_usd < min_24h_vol_usd and sym not in prio:
                    continue
                try:
                    vmap[v] = self._fetch_leg_detail(leg, ts)
                except Exception:
                    pass

        return by_sym

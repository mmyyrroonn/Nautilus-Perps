#!/usr/bin/env python3
"""Multi-day cross-venue funding-carry study from PUBLIC history (HL / Lighter / Aster).

Read-only, stdlib only, no keys, no orders. Answers one question: if you hold a
delta-neutral pair (long one venue, short another) for days, does the funding
differential pay more than the round trip costs, and how much basis risk do you
eat while you wait?

    python src/analysis/funding_history.py --days 60
    python src/analysis/funding_history.py --days 30 --symbols NVDA,GOLD,BTC --hurdle-bps 6

Pipeline
    1. funding history per venue per symbol -> normalised to **bps per hour** on a
       common UTC hour grid
    2. 1 h candles per venue per symbol -> hourly cross-venue basis in bps
    3. per symbol x ordered venue pair (long A / short B): carry distribution,
       persistence, rolling 24 h / 7 d cumulative carry, hurdle coverage,
       annualised carry, basis-drift risk, and a ranked net-edge score
    4. CSVs + a markdown report under --out

Funding units, verified 2026-09-07 against live responses (see the report's
"data caveats" for the sample values that pin each one down):

    HL       POST /info {"type":"fundingHistory"} -> fundingRate is a FRACTION per
             hour, hourly settlement.                       bps/h = rate * 1e4
    LIGHTER  GET /api/v1/fundings -> `rate` is an UNSIGNED PERCENT per hour and
             `direction` carries the sign ("long" = longs pay = positive).
             Cross-check: the sibling `value` field equals price * rate / 100.
                                                            bps/h = +/-rate * 1e2
    ASTER    GET /fapi/v1/fundingRate -> fundingRate is a FRACTION per settlement
             INTERVAL; the interval is per symbol from GET /fapi/v1/fundingInfo
             (1 / 4 / 8 h, symbols absent from that list default to 8 h). The rate
             accrues over the interval ENDING at `fundingTime`, so it is spread
             evenly backwards over those hours.
                                       bps/h = rate * 1e4 / fundingIntervalHours

Sign convention: a positive funding rate means longs pay shorts. Holding long A
and short B therefore earns  carry = f_B - f_A  (bps/h).
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HOUR = 3600
HOUR_MS = 3_600_000
DAY_H = 24
WEEK_H = 168

HL_INFO_URL = "https://api.hyperliquid.xyz/info"
LIGHTER_BASE = "https://mainnet.zklighter.elliot.ai"
ASTER_BASE = "https://fapi.asterdex.com"

HL_FUNDING_PAGE = 500  # rows returned per fundingHistory call (venue cap)
LIGHTER_PAGE = 750  # rows returned per /fundings call (venue cap, count_back is advisory)
LIGHTER_CANDLE_PAGE = 500  # rows returned per /candles call (venue cap)
ASTER_FUNDING_LIMIT = 1000  # documented max limit for /fapi/v1/fundingRate
ASTER_KLINE_LIMIT = 1500  # documented max limit for /fapi/v1/klines
HL_CANDLE_PAGE = 4000  # candleSnapshot caps around 5000; stay under it

ASTER_DEFAULT_INTERVAL_H = 8  # symbols absent from /fapi/v1/fundingInfo
VENUES = ("HL", "LIGHTER", "ASTER")

USER_AGENT = "Nautilus-Perps carry study (read-only public data)"

# Fallback symbol table, used only when src/spread_watch.py cannot be imported
# (it needs nautilus_trader). Kept identical to INSTRUMENTS there.
FALLBACK_INSTRUMENTS: dict[str, dict[str, str]] = {
    "NVDA": {"HL": "xyz:NVDA-USD-PERP.HYPERLIQUID", "LIGHTER": "NVDA-PERP.LIGHTER", "ASTER": "NVDAUSDT-PERP.ASTER"},
    "TSLA": {"HL": "xyz:TSLA-USD-PERP.HYPERLIQUID", "LIGHTER": "TSLA-PERP.LIGHTER", "ASTER": "TSLAUSDT-PERP.ASTER"},
    "HOOD": {"HL": "xyz:HOOD-USD-PERP.HYPERLIQUID", "LIGHTER": "HOOD-PERP.LIGHTER", "ASTER": "HOODUSDT-PERP.ASTER"},
    "SNDK": {"HL": "xyz:SNDK-USD-PERP.HYPERLIQUID", "LIGHTER": "SNDK-PERP.LIGHTER", "ASTER": "SNDKUSD1-PERP.ASTER"},
    "MU": {"HL": "xyz:MU-USD-PERP.HYPERLIQUID", "LIGHTER": "MU-PERP.LIGHTER", "ASTER": "MUUSD1-PERP.ASTER"},
    "SPCX": {"HL": "xyz:SPCX-USD-PERP.HYPERLIQUID", "LIGHTER": "SPCX-PERP.LIGHTER", "ASTER": "SPCXUSD1-PERP.ASTER"},
    "GOLD": {"HL": "xyz:GOLD-USD-PERP.HYPERLIQUID", "LIGHTER": "XAU-PERP.LIGHTER", "ASTER": "XAUUSDT-PERP.ASTER"},
    "GOLD1": {"HL": "xyz:GOLD-USD-PERP.HYPERLIQUID", "LIGHTER": "XAU-PERP.LIGHTER", "ASTER": "XAUUSD1-PERP.ASTER"},
}
for _base in ("BTC", "ETH", "SOL", "HYPE", "ZEC", "PONS", "LIT", "ASTER", "DASH", "PUMP", "ARB"):
    FALLBACK_INSTRUMENTS[_base] = {
        "HL": f"{_base}-USD-PERP.HYPERLIQUID",
        "LIGHTER": f"{_base}-PERP.LIGHTER",
        "ASTER": f"{_base}USDT-PERP.ASTER",
    }


# ------------------------------------------------------------------ symbol table


def load_instruments() -> tuple[dict[str, dict[str, str]], str]:
    """Symbol -> venue -> Nautilus instrument id, from spread_watch when importable."""
    src_dir = Path(__file__).resolve().parents[1]
    if str(src_dir) not in sys.path:
        sys.path.insert(0, str(src_dir))
    try:
        from spread_watch import INSTRUMENTS  # noqa: PLC0415  (optional heavy import)
    except Exception:  # nautilus_trader missing, or the module is being edited
        return FALLBACK_INSTRUMENTS, "fallback table in funding_history.py"
    return (
        {sym: {v: iid for v, (iid, _fee) in legs.items()} for sym, legs in INSTRUMENTS.items()},
        "src/spread_watch.py INSTRUMENTS",
    )


def raw_symbol(venue: str, instrument_id: str) -> str:
    """Nautilus instrument id -> the name the venue's public REST API uses."""
    base = instrument_id.split(".", 1)[0]
    if venue == "HL":  # "xyz:NVDA-USD-PERP" -> "xyz:NVDA"; "BTC-USD-PERP" -> "BTC"
        return base[: -len("-USD-PERP")] if base.endswith("-USD-PERP") else base
    if venue == "LIGHTER":  # "NVDA-PERP" -> "NVDA"
        return base[: -len("-PERP")] if base.endswith("-PERP") else base
    if venue == "ASTER":  # "NVDAUSDT-PERP" -> "NVDAUSDT"
        return base[: -len("-PERP")] if base.endswith("-PERP") else base
    raise ValueError(f"unknown venue {venue!r}")


# ----------------------------------------------------------------- http + cache


class Fetcher:
    """Polite JSON fetcher: retries, a sleep between calls, and a raw-response cache."""

    def __init__(self, raw_dir: Path, sleep: float, retries: int, refresh: bool, quiet: bool):
        self.raw_dir = raw_dir
        self.sleep = sleep
        self.retries = retries
        self.refresh = refresh
        self.quiet = quiet
        self.calls = 0
        self.cache_hits = 0
        self.errors: list[str] = []

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, flush=True)

    def _request(self, url: str, body: bytes | None) -> object:
        headers = {"User-Agent": USER_AGENT, "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json"
        last = ""
        for attempt in range(self.retries):
            req = urllib.request.Request(url, data=body, headers=headers)
            try:
                self.calls += 1
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.loads(resp.read())
                time.sleep(self.sleep)
                return payload
            except urllib.error.HTTPError as exc:
                detail = exc.read()[:200].decode("utf-8", "replace")
                last = f"HTTP {exc.code} {detail}"
                if exc.code in (400, 403, 404):  # not transient: do not hammer
                    break
            except Exception as exc:  # timeouts, resets, SSL EOF, malformed JSON
                last = repr(exc)[:200]
            time.sleep(min(20.0, 2.0 * (attempt + 1) ** 2))
        raise RuntimeError(last or "request failed")

    def json(self, key: str, url: str, body: dict | None = None) -> object:
        """Fetch and cache one response. `key` names the cache file."""
        path = self.raw_dir / f"{sanitize(key)}.json"
        if path.exists() and not self.refresh:
            try:
                self.cache_hits += 1
                return json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                pass  # corrupt cache entry, refetch
        payload = self._request(url, json.dumps(body).encode() if body is not None else None)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return payload

    def note_error(self, msg: str) -> None:
        self.errors.append(msg)
        self._log(f"    ! {msg}")

    def page(self, key: str, url: str, body: dict | None, problems: list[str], label: str) -> object | None:
        """One page of a paginated fetch. A failure here truncates the series, never
        discards the pages already collected: partial history beats no history, and
        the gap is recorded rather than filled in."""
        try:
            return self.json(key, url, body)
        except Exception as exc:
            problems.append(f"{label} truncated after a failed page: {exc}")
            self.note_error(f"{label}: {exc}")
            return None


def sanitize(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", name)


# --------------------------------------------------------- unit normalisation
# Pure functions; tests/test_funding_history.py covers these directly.


def floor_hour_ms(ms: int) -> int:
    """Millisecond timestamp -> the UTC hour it falls in, as unix seconds."""
    return (int(ms) // HOUR_MS) * HOUR


def floor_hour_s(seconds: int) -> int:
    return (int(seconds) // HOUR) * HOUR


def hl_bps_per_hour(funding_rate: str | float) -> float:
    """HL fundingRate is a fraction per hour (hourly settlement)."""
    return float(funding_rate) * 1e4


def lighter_bps_per_hour(rate: str | float, direction: str) -> float:
    """Lighter `rate` is an unsigned PERCENT per hour; `direction` carries the sign.

    "long" means longs pay shorts -> positive under the usual convention.
    """
    value = abs(float(rate)) * 1e2
    if direction not in ("long", "short"):
        raise ValueError(f"unknown Lighter funding direction {direction!r}")
    return value if direction == "long" else -value


def aster_hourly_bps(
    funding_rate: str | float,
    funding_time_ms: int,
    interval_hours: int,
) -> list[tuple[int, float]]:
    """Aster fundingRate is a fraction per settlement interval.

    The rate accrues over the interval that ENDS at ``funding_time_ms``, so it is
    spread evenly backwards over ``interval_hours`` hourly buckets.
    """
    if interval_hours < 1:
        raise ValueError(f"bad Aster funding interval {interval_hours!r}")
    per_hour = float(funding_rate) * 1e4 / interval_hours
    settle_hour = floor_hour_ms(funding_time_ms)
    return [(settle_hour - i * HOUR, per_hour) for i in range(1, interval_hours + 1)]


# ------------------------------------------------------------- venue fetchers


def fetch_hl_funding(
    fetch: Fetcher, coin: str, start_ms: int, end_ms: int, problems: list[str]
) -> dict[int, float]:
    """HL fundingHistory, paged forward (500 rows per call)."""
    out: dict[int, float] = {}
    cursor, page_no = start_ms, 0
    while cursor < end_ms and page_no < 200:
        rows = fetch.page(
            f"hl_funding_{coin}_{start_ms}_{end_ms}_p{page_no}",
            HL_INFO_URL,
            {"type": "fundingHistory", "coin": coin, "startTime": cursor, "endTime": end_ms},
            problems,
            f"HL fundingHistory {coin} page {page_no}",
        )
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            hour = floor_hour_ms(row["time"])
            if start_ms // 1000 <= hour < end_ms // 1000:
                out[hour] = hl_bps_per_hour(row["fundingRate"])
        newest = max(int(r["time"]) for r in rows)
        if len(rows) < HL_FUNDING_PAGE or newest <= cursor:
            break
        cursor, page_no = newest + 1, page_no + 1
    return out


def fetch_hl_candles(
    fetch: Fetcher, coin: str, start_ms: int, end_ms: int, problems: list[str]
) -> dict[int, float]:
    """HL candleSnapshot 1h. `c` is the LAST-TRADE close of the hour."""
    out: dict[int, float] = {}
    cursor, page_no = start_ms, 0
    while cursor < end_ms and page_no < 100:
        stop = min(end_ms, cursor + HL_CANDLE_PAGE * HOUR_MS)
        rows = fetch.page(
            f"hl_candles_{coin}_{start_ms}_{end_ms}_p{page_no}",
            HL_INFO_URL,
            {"type": "candleSnapshot",
             "req": {"coin": coin, "interval": "1h", "startTime": cursor, "endTime": stop}},
            problems,
            f"HL candleSnapshot {coin} page {page_no}",
        )
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            out[floor_hour_ms(row["t"])] = float(row["c"])
        cursor, page_no = stop, page_no + 1
    return out


def lighter_get(fetch: Fetcher, key: str, path: str) -> object:
    return fetch.json(key, LIGHTER_BASE + path)


def fetch_lighter_markets(fetch: Fetcher) -> dict[str, int]:
    """Lighter market symbol -> market_id, from /api/v1/orderBooks (one call)."""
    payload = lighter_get(fetch, "lighter_orderBooks", "/api/v1/orderBooks")
    books = payload.get("order_books", []) if isinstance(payload, dict) else []
    return {b["symbol"]: int(b["market_id"]) for b in books}


def fetch_lighter_funding(
    fetch: Fetcher, market_id: int, start_s: int, end_s: int, problems: list[str]
) -> dict[int, float]:
    """Lighter /fundings, paged BACKWARDS (the venue caps a call at 750 rows)."""
    out: dict[int, float] = {}
    cursor, page_no = end_s, 0
    while cursor > start_s and page_no < 200:
        payload = fetch.page(
            f"lighter_funding_{market_id}_{start_s}_{end_s}_p{page_no}",
            LIGHTER_BASE + f"/api/v1/fundings?market_id={market_id}&resolution=1h"
            f"&start_timestamp={start_s}&end_timestamp={cursor}&count_back={LIGHTER_PAGE}",
            None,
            problems,
            f"LIGHTER fundings market {market_id} page {page_no}",
        )
        rows = payload.get("fundings", []) if isinstance(payload, dict) else []
        if not rows:
            break
        oldest = min(int(r["timestamp"]) for r in rows)
        for row in rows:
            hour = floor_hour_s(row["timestamp"])
            if start_s <= hour < end_s:
                out[hour] = lighter_bps_per_hour(row["rate"], row["direction"])
        if oldest <= start_s or oldest >= cursor:
            break
        cursor, page_no = oldest - 1, page_no + 1
    return out


def fetch_lighter_candles(
    fetch: Fetcher, market_id: int, start_s: int, end_s: int, problems: list[str]
) -> dict[int, float]:
    """Lighter /candles 1h (500 rows per call, paged backwards). `c` is a LAST-TRADE close."""
    out: dict[int, float] = {}
    cursor, page_no = end_s, 0
    while cursor > start_s and page_no < 200:
        payload = fetch.page(
            f"lighter_candles_{market_id}_{start_s}_{end_s}_p{page_no}",
            LIGHTER_BASE + f"/api/v1/candles?market_id={market_id}&resolution=1h"
            f"&start_timestamp={start_s}&end_timestamp={cursor}"
            f"&count_back={LIGHTER_CANDLE_PAGE}&set_timestamp_to_end=false",
            None,
            problems,
            f"LIGHTER candles market {market_id} page {page_no}",
        )
        rows = payload.get("c", []) if isinstance(payload, dict) else []
        if not rows:
            break
        oldest_ms = min(int(r["t"]) for r in rows)
        for row in rows:
            hour = floor_hour_ms(row["t"])
            if start_s <= hour < end_s:
                out[hour] = float(row["c"])
        oldest_s = oldest_ms // 1000
        if oldest_s <= start_s or oldest_s >= cursor:
            break
        cursor, page_no = oldest_s - 1, page_no + 1
    return out


def aster_get(fetch: Fetcher, key: str, path: str) -> object:
    return fetch.json(key, ASTER_BASE + path)


def fetch_aster_intervals(fetch: Fetcher) -> dict[str, int]:
    """Aster symbol -> fundingIntervalHours, from /fapi/v1/fundingInfo (ONE call, ever).

    Aster rate-limits the metadata endpoints hard; this is fetched once and cached.
    """
    payload = aster_get(fetch, "aster_fundingInfo", "/fapi/v1/fundingInfo")
    if not isinstance(payload, list):
        return {}
    return {row["symbol"]: int(row["fundingIntervalHours"]) for row in payload}


def fetch_aster_funding(
    fetch: Fetcher, symbol: str, start_ms: int, end_ms: int, interval_h: int, problems: list[str]
) -> dict[int, float]:
    """Aster /fundingRate, paged forward, each settlement spread over its interval."""
    out: dict[int, float] = {}
    cursor, page_no = start_ms, 0
    while cursor < end_ms and page_no < 200:
        rows = fetch.page(
            f"aster_funding_{symbol}_{start_ms}_{end_ms}_p{page_no}",
            ASTER_BASE + f"/fapi/v1/fundingRate?symbol={symbol}&startTime={cursor}&endTime={end_ms}"
            f"&limit={ASTER_FUNDING_LIMIT}",
            None,
            problems,
            f"ASTER fundingRate {symbol} page {page_no}",
        )
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            for hour, bps in aster_hourly_bps(row["fundingRate"], row["fundingTime"], interval_h):
                if start_ms // 1000 <= hour < end_ms // 1000:
                    out[hour] = bps
        newest = max(int(r["fundingTime"]) for r in rows)
        if len(rows) < ASTER_FUNDING_LIMIT or newest <= cursor:
            break
        cursor, page_no = newest + 1, page_no + 1
    return out


def fetch_aster_candles(
    fetch: Fetcher, symbol: str, start_ms: int, end_ms: int, problems: list[str]
) -> dict[int, float]:
    """Aster /klines 1h. Index 4 is the LAST-TRADE close of the hour."""
    out: dict[int, float] = {}
    cursor, page_no = start_ms, 0
    while cursor < end_ms and page_no < 100:
        rows = fetch.page(
            f"aster_klines_{symbol}_{start_ms}_{end_ms}_p{page_no}",
            ASTER_BASE + f"/fapi/v1/klines?symbol={symbol}&interval=1h&startTime={cursor}&endTime={end_ms}"
            f"&limit={ASTER_KLINE_LIMIT}",
            None,
            problems,
            f"ASTER klines {symbol} page {page_no}",
        )
        if not isinstance(rows, list) or not rows:
            break
        for row in rows:
            out[floor_hour_ms(row[0])] = float(row[4])
        newest = max(int(r[0]) for r in rows)
        if len(rows) < ASTER_KLINE_LIMIT or newest <= cursor:
            break
        cursor, page_no = newest + HOUR_MS, page_no + 1
    return out


# ------------------------------------------------------------------ statistics


def percentile(values: list[float], q: float) -> float | None:
    """Linear-interpolated percentile of an unsorted list."""
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    k = (len(ordered) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (k - lo)


def longest_same_sign_run(values: list[float | None], sign: float) -> int:
    """Longest run of consecutive grid hours whose value matches `sign`.

    A missing hour (None) breaks the run: a carry you cannot observe is not a
    carry you can claim to have collected.
    """
    if sign == 0:
        return 0
    best = run = 0
    for value in values:
        if value is not None and value != 0 and (value > 0) == (sign > 0):
            run += 1
            best = max(best, run)
        else:
            run = 0
    return best


def rolling_sums(values: list[float | None], window: int) -> list[float]:
    """Sums over every fully populated window of `window` consecutive grid hours."""
    n = len(values)
    if window <= 0 or n < window:
        return []
    pref = [0.0] * (n + 1)
    cnt = [0] * (n + 1)
    for i, value in enumerate(values):
        pref[i + 1] = pref[i] + (0.0 if value is None else value)
        cnt[i + 1] = cnt[i] + (0 if value is None else 1)
    return [
        pref[i + window] - pref[i]
        for i in range(n - window + 1)
        if cnt[i + window] - cnt[i] == window
    ]


def rolling_changes(values: list[float | None], window: int) -> list[float]:
    """|value[i+window] - value[i]| for every pair of populated grid hours."""
    out = []
    for i in range(len(values) - window):
        a, b = values[i], values[i + window]
        if a is not None and b is not None:
            out.append(abs(b - a))
    return out


@dataclass
class PairStats:
    """Everything the ranked table and the summary CSV need for one ordered pair."""

    symbol: str = ""
    long_venue: str = ""
    short_venue: str = ""
    hours: int = 0
    coverage_pct: float = 0.0
    mean_bps_h: float = 0.0
    median_bps_h: float = 0.0
    stdev_bps_h: float | None = None
    same_sign_frac: float = 0.0
    longest_run_h: int = 0
    carry_24h_p10: float | None = None
    carry_24h_p50: float | None = None
    carry_24h_p90: float | None = None
    carry_24h_hit_frac: float | None = None
    carry_7d_p10: float | None = None
    carry_7d_p50: float | None = None
    carry_7d_p90: float | None = None
    carry_7d_hit_frac: float | None = None
    hours_to_hurdle: float | None = None
    annualised_pct: float = 0.0
    basis_hours: int = 0
    basis_mean_bps: float | None = None
    basis_std_bps: float | None = None
    basis_7d_move_max: float | None = None
    basis_7d_move_p90: float | None = None
    edge_7d_bps: float | None = None
    windows_7d: int = 0
    rankable: bool = False
    mean_funding_long: float = 0.0
    mean_funding_short: float = 0.0


def pair_stats(
    symbol: str,
    long_venue: str,
    short_venue: str,
    grid: list[int],
    funding: dict[str, dict[int, float]],
    basis: dict[int, float] | None,
    hurdle_bps: float,
) -> PairStats | None:
    """Carry + basis statistics for holding long `long_venue` / short `short_venue`.

    Sign convention: positive funding = longs pay shorts, so the pair earns
    ``carry = f_short - f_long`` every hour both legs report a rate.
    """
    f_long, f_short = funding.get(long_venue), funding.get(short_venue)
    if not f_long or not f_short:
        return None

    series: list[float | None] = []
    observed: list[float] = []
    long_obs: list[float] = []
    short_obs: list[float] = []
    for hour in grid:
        a, b = f_long.get(hour), f_short.get(hour)
        if a is None or b is None:
            series.append(None)
            continue
        carry = b - a
        series.append(carry)
        observed.append(carry)
        long_obs.append(a)
        short_obs.append(b)
    if len(observed) < DAY_H:  # fewer than a day of overlap is not a study
        return None

    stats = PairStats(symbol=symbol, long_venue=long_venue, short_venue=short_venue)
    stats.hours = len(observed)
    stats.coverage_pct = 100.0 * len(observed) / max(1, len(grid))
    stats.mean_bps_h = statistics.fmean(observed)
    stats.median_bps_h = statistics.median(observed)
    stats.stdev_bps_h = statistics.pstdev(observed) if len(observed) > 1 else None
    stats.mean_funding_long = statistics.fmean(long_obs)
    stats.mean_funding_short = statistics.fmean(short_obs)

    sign = 1.0 if stats.mean_bps_h >= 0 else -1.0
    stats.same_sign_frac = sum(1 for v in observed if v != 0 and (v > 0) == (sign > 0)) / len(observed)
    stats.longest_run_h = longest_same_sign_run(series, sign)

    day = rolling_sums(series, DAY_H)
    week = rolling_sums(series, WEEK_H)
    if day:
        stats.carry_24h_p10 = percentile(day, 0.10)
        stats.carry_24h_p50 = percentile(day, 0.50)
        stats.carry_24h_p90 = percentile(day, 0.90)
        stats.carry_24h_hit_frac = sum(1 for v in day if v > hurdle_bps) / len(day)
    stats.windows_7d = len(week)
    # A pair is only rankable once a full 168 h window has actually been observed:
    # otherwise `mean * 168` extrapolates a few days of a freshly listed market into
    # a week of carry it has never delivered.
    stats.rankable = bool(week)
    if week:
        stats.carry_7d_p10 = percentile(week, 0.10)
        stats.carry_7d_p50 = percentile(week, 0.50)
        stats.carry_7d_p90 = percentile(week, 0.90)
        stats.carry_7d_hit_frac = sum(1 for v in week if v > hurdle_bps) / len(week)

    if stats.mean_bps_h > 0:
        stats.hours_to_hurdle = hurdle_bps / stats.mean_bps_h
    stats.annualised_pct = stats.mean_bps_h * 24 * 365 / 100.0

    if basis:
        b_series: list[float | None] = [basis.get(h) for h in grid]
        b_obs = [v for v in b_series if v is not None]
        stats.basis_hours = len(b_obs)
        if b_obs:
            stats.basis_mean_bps = statistics.fmean(b_obs)
            stats.basis_std_bps = statistics.pstdev(b_obs) if len(b_obs) > 1 else 0.0
        moves = rolling_changes(b_series, WEEK_H)
        if moves:
            stats.basis_7d_move_max = max(moves)
            stats.basis_7d_move_p90 = percentile(moves, 0.90)

    # Ranking score, stated plainly:
    #   edge_7d = (expected carry over 7 days) - (round-trip cost) - (basis shock)
    #   expected carry over 7 days = mean_bps_h * 168
    #   round-trip cost           = --hurdle-bps (both legs in and out, taker)
    #   basis shock               = p90 of the absolute 7-day change in the A/B basis,
    #                               i.e. a bad-but-not-worst mark-to-market move you may
    #                               have to unwind into. Pairs with no basis coverage
    #                               are scored with the shock term set to 0 and flagged.
    # Pairs that never completed a 168 h window are scored but not ranked (see above).
    shock = stats.basis_7d_move_p90 or 0.0
    stats.edge_7d_bps = stats.mean_bps_h * WEEK_H - hurdle_bps - shock
    return stats


# ---------------------------------------------------------------------- output


def fmt(value: float | None, nd: int = 2, na: str = "-") -> str:
    return na if value is None else f"{value:.{nd}f}"


def table(headers: list[str], rows: list[list[object]]) -> list[str]:
    cells = [[str(c) for c in row] for row in rows]
    widths = [max(3, len(h)) for h in headers]
    for row in cells:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    out = ["| " + " | ".join(h.ljust(widths[i]) for i, h in enumerate(headers)) + " |"]
    out.append("|" + "|".join("-" * (w + 2) for w in widths) + "|")
    for row in cells:
        out.append("| " + " | ".join(c.ljust(widths[i]) for i, c in enumerate(row)) + " |")
    return out


def iso_hour(hour: int) -> str:
    return datetime.fromtimestamp(hour, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


SUMMARY_FIELDS = [
    "symbol", "long_venue", "short_venue", "hours", "coverage_pct",
    "mean_bps_h", "median_bps_h", "stdev_bps_h", "same_sign_frac", "longest_run_h",
    "carry_24h_p10", "carry_24h_p50", "carry_24h_p90", "carry_24h_hit_frac",
    "carry_7d_p10", "carry_7d_p50", "carry_7d_p90", "carry_7d_hit_frac",
    "hours_to_hurdle", "annualised_pct",
    "basis_hours", "basis_mean_bps", "basis_std_bps", "basis_7d_move_max", "basis_7d_move_p90",
    "edge_7d_bps", "windows_7d", "rankable", "mean_funding_long", "mean_funding_short",
]


def write_summary_csv(path: Path, stats: list[PairStats]) -> None:
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(SUMMARY_FIELDS)
        for s in stats:
            row = []
            for name in SUMMARY_FIELDS:
                value = getattr(s, name)
                row.append("" if value is None else (round(value, 6) if isinstance(value, float) else value))
            writer.writerow(row)


# ------------------------------------------------------------------------ main


@dataclass
class SymbolData:
    symbol: str
    funding: dict[str, dict[int, float]] = field(default_factory=dict)
    closes: dict[str, dict[int, float]] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


def collect(
    fetch: Fetcher,
    symbols: list[str],
    instruments: dict[str, dict[str, str]],
    start_s: int,
    end_s: int,
) -> tuple[dict[str, SymbolData], dict[str, int], dict[str, str]]:
    """One pass over every symbol x venue. Failures are recorded, never fatal."""
    start_ms, end_ms = start_s * 1000, end_s * 1000
    lighter_markets: dict[str, int] = {}
    aster_intervals: dict[str, int] = {}
    try:
        lighter_markets = fetch_lighter_markets(fetch)
    except Exception as exc:
        fetch.note_error(f"LIGHTER /api/v1/orderBooks failed ({exc}); no Lighter legs")
    try:
        aster_intervals = fetch_aster_intervals(fetch)
    except Exception as exc:
        fetch.note_error(f"ASTER /fapi/v1/fundingInfo failed ({exc}); assuming 8 h everywhere")

    used_intervals: dict[str, int] = {}
    price_sources = {
        "HL": "candleSnapshot 1h close (last trade)",
        "LIGHTER": "/api/v1/candles 1h close (last trade)",
        "ASTER": "/fapi/v1/klines 1h close (last trade)",
    }
    data: dict[str, SymbolData] = {}
    for symbol in symbols:
        legs = instruments.get(symbol)
        if not legs:
            fetch.note_error(f"{symbol}: not in the symbol table, skipped")
            continue
        entry = SymbolData(symbol=symbol)
        fetch._log(f"  {symbol}")
        for venue in VENUES:
            iid = legs.get(venue)
            if not iid:
                entry.notes.append(f"{venue}: no instrument mapping")
                continue
            name = raw_symbol(venue, iid)
            problems: list[str] = []
            try:
                if venue == "HL":
                    funding = fetch_hl_funding(fetch, name, start_ms, end_ms, problems)
                    closes = fetch_hl_candles(fetch, name, start_ms, end_ms, problems)
                elif venue == "LIGHTER":
                    market_id = lighter_markets.get(name)
                    if market_id is None:
                        entry.notes.append(f"LIGHTER: market {name!r} not listed")
                        continue
                    funding = fetch_lighter_funding(fetch, market_id, start_s, end_s, problems)
                    closes = fetch_lighter_candles(fetch, market_id, start_s, end_s, problems)
                else:
                    interval = aster_intervals.get(name, ASTER_DEFAULT_INTERVAL_H)
                    used_intervals[name] = interval
                    if name not in aster_intervals:
                        entry.notes.append(f"ASTER: {name} absent from fundingInfo, assumed 8 h")
                    funding = fetch_aster_funding(fetch, name, start_ms, end_ms, interval, problems)
                    closes = fetch_aster_candles(fetch, name, start_ms, end_ms, problems)
            except Exception as exc:  # a non-page failure (parse, bad mapping)
                fetch.note_error(f"{symbol}/{venue} ({name}): {exc}")
                entry.notes.append(f"{venue}: fetch failed ({exc})")
                continue
            entry.notes.extend(f"{venue}: {p}" for p in problems)
            if funding:
                entry.funding[venue] = funding
            else:
                entry.notes.append(f"{venue}: funding history empty for {name}")
            if closes:
                entry.closes[venue] = closes
            else:
                entry.notes.append(f"{venue}: no 1 h candles for {name}")
            fetch._log(f"    {venue:<7} {name:<16} funding {len(funding):>5} h   candles {len(closes):>5} h")
        data[symbol] = entry
    return data, used_intervals, price_sources


def build_report(
    args: argparse.Namespace,
    grid: list[int],
    data: dict[str, SymbolData],
    ranked: list[PairStats],
    intervals: dict[str, int],
    price_sources: dict[str, str],
    fetch: Fetcher,
    source_note: str,
    stamp: str,
) -> str:
    lines: list[str] = []
    lines.append(f"# Cross-venue funding carry: HL x Lighter x Aster ({args.days} d)")
    lines.append("")
    lines.append(f"Window: `{iso_hour(grid[0])}` .. `{iso_hour(grid[-1])}` ({len(grid)} UTC hours)")
    lines.append(f"Generated: `{stamp}` | hurdle: **{args.hurdle_bps:.1f} bps** round-trip "
                 f"| symbol table: {source_note}")
    lines.append("")
    lines.append("Public REST history only: no keys, no orders, no account data. "
                 "Positive funding means longs pay shorts, so a pair that is long A "
                 "and short B earns `carry = f_B - f_A` every hour.")
    lines.append("")

    # -------------------------------------------------- headline conclusion
    lines.append("## Conclusion")
    lines.append("")
    rankable = [s for s in ranked if s.rankable]
    thin = [s for s in ranked if not s.rankable]
    if not rankable:
        lines.append("No venue pair had enough overlapping history to judge. See data caveats.")
    else:
        clears = [s for s in rankable if s.edge_7d_bps is not None and s.edge_7d_bps > 0]
        # "Robust" is deliberately strict: a bad week must still pay, the sign must
        # persist, and the pair must have survived a month of history -- otherwise a
        # market listed last week can look unbeatable on a handful of overlapping windows.
        robust = [
            s for s in clears
            if (s.carry_7d_p10 or -1e9) > args.hurdle_bps
            and s.same_sign_frac >= 0.7
            and s.hours >= 30 * 24
        ]
        best = rankable[0]
        lines.append(
            f"- Ranked **{len(rankable)}** ordered venue pairs across "
            f"**{len({s.symbol for s in rankable})}** symbols "
            f"({len(thin)} more had overlap but never a full 168 h window, listed separately)."
        )
        lines.append(
            f"- **{len(clears)}** pairs have a positive 7-day net edge after the "
            f"{args.hurdle_bps:.0f} bps round trip and a p90 basis shock."
        )
        lines.append(
            f"- **{len(robust)}** of those are *robust*: the 10th-percentile 7-day carry "
            f"clears the hurdle, the differential keeps its sign in at least 70% of hours, "
            f"and the pair has at least 30 days of overlapping history. "
            + ("These are the only ones worth a live trial." if robust else
               "None. Every positive-edge pair either depends on the good tail of the "
               "distribution or rests on too little history to trust.")
        )
        lines.append(
            f"- Best by net edge: **{best.symbol} long {best.long_venue} / short {best.short_venue}** "
            f"at {fmt(best.mean_bps_h, 3)} bps/h mean "
            f"({fmt(best.annualised_pct, 1)}%/y), median 7-day carry {fmt(best.carry_7d_p50, 1)} bps, "
            f"p10 {fmt(best.carry_7d_p10, 1)} bps, "
            f"net 7-day edge {fmt(best.edge_7d_bps, 1)} bps."
        )
        if best.mean_bps_h > 0:
            lines.append(
                f"- At its mean rate that pair needs **{fmt(best.hours_to_hurdle, 1)} hours** "
                f"({fmt((best.hours_to_hurdle or 0) / 24, 1)} days) of undisturbed holding just to "
                f"repay the {args.hurdle_bps:.0f} bps round trip."
            )
        if robust:
            names = ", ".join(f"{s.symbol} {s.long_venue}->{s.short_venue}" for s in robust[:6])
            lines.append(f"- Robust set: {names}.")
    lines.append("")

    # ----------------------------------------------------------- ranked table
    lines.append("## Ranked pairs")
    lines.append("")
    lines.append("Ranked by the explicit score")
    lines.append("")
    lines.append("```")
    lines.append("edge_7d_bps = mean_carry_bps_per_hour * 168")
    lines.append(f"            - {args.hurdle_bps:.1f}                       # round-trip taker cost, both legs")
    lines.append("            - p90(|basis[t+168] - basis[t]|)  # bad-case 7 d basis move you unwind into")
    lines.append("```")
    lines.append("")
    lines.append("`carry7d` columns are the p10 / p50 / p90 of every fully populated rolling "
                 "168 h window; `same%` is the share of hours whose differential keeps the sign "
                 "of its mean; `run` is the longest unbroken same-sign stretch in hours; "
                 "`bas.sd` is the stdev of the hourly A/B basis and `bas.7d90` the p90 absolute "
                 "7-day basis move. All figures in bps unless noted. A pair only enters this "
                 "table once it has completed at least one 168 h window, so a freshly listed "
                 "market cannot be ranked on an extrapolated mean.")
    lines.append("")
    headers = ["#", "symbol", "long", "short", "h", "mean/h", "med/h", "same%", "run",
               "carry24h", "c7d p10", "c7d p50", "c7d p90", "7d>hurdle", "ann%",
               "bas.sd", "bas.7d90", "edge7d"]

    def row_for(index: object, s: PairStats) -> list[object]:
        return [
            index, s.symbol, s.long_venue, s.short_venue, s.hours,
            fmt(s.mean_bps_h, 3), fmt(s.median_bps_h, 3),
            f"{s.same_sign_frac * 100:.0f}", s.longest_run_h,
            fmt(s.carry_24h_p50, 1), fmt(s.carry_7d_p10, 1), fmt(s.carry_7d_p50, 1),
            fmt(s.carry_7d_p90, 1),
            "-" if s.carry_7d_hit_frac is None else f"{s.carry_7d_hit_frac * 100:.0f}%",
            fmt(s.annualised_pct, 1),
            fmt(s.basis_std_bps, 1), fmt(s.basis_7d_move_p90, 1), fmt(s.edge_7d_bps, 1),
        ]

    lines.extend(table(headers, [row_for(i, s) for i, s in enumerate(rankable, 1)]))
    lines.append("")
    if thin:
        lines.append("### Not ranked: no complete 168 h window")
        lines.append("")
        lines.append("Recently listed on at least one venue. `edge7d` here is `mean * 168` "
                     "extrapolated from a shorter sample and must not be compared with the "
                     "table above.")
        lines.append("")
        lines.extend(table(headers, [row_for("-", s) for s in thin]))
        lines.append("")

    # --------------------------------------------------------- per-venue means
    lines.append("## Mean funding by venue (bps/h, positive = longs pay)")
    lines.append("")
    vrows = []
    for symbol in sorted(data):
        entry = data[symbol]
        if not entry.funding:
            continue
        cells = [symbol]
        for venue in VENUES:
            series = entry.funding.get(venue)
            cells.append(f"{statistics.fmean(series.values()):.3f}" if series else "-")
        for venue in VENUES:
            series = entry.funding.get(venue)
            cells.append(str(len(series)) if series else "0")
        vrows.append(cells)
    lines.extend(table(
        ["symbol", *[f"{v} mean" for v in VENUES], *[f"{v} h" for v in VENUES]], vrows
    ))
    lines.append("")

    # ------------------------------------------------------------- caveats
    lines.append("## Data caveats")
    lines.append("")
    lines.append("**Funding units.** Each venue reports funding in a different unit; every "
                 "number above is normalised to bps per hour.")
    lines.append("")
    lines.extend(table(
        ["venue", "endpoint", "raw unit", "-> bps/h"],
        [
            ["HL", "`POST /info {\"type\":\"fundingHistory\"}`", "fraction / hour", "`rate * 1e4`"],
            ["LIGHTER", "`GET /api/v1/fundings`", "percent / hour, sign in `direction`",
             "`+/-rate * 1e2`"],
            ["ASTER", "`GET /fapi/v1/fundingRate`", "fraction / settlement interval",
             "`rate * 1e4 / intervalHours`"],
        ],
    ))
    lines.append("")
    lines.append("The Lighter unit is the one the public docs do not state. Two independent "
                 "checks pin it to percent: (1) the sibling `value` field in the same row "
                 "equals `price * rate / 100` (BTC row `rate=0.0010`, `value=0.79433800`, "
                 "hourly close 79283.2 -> 79283.2 * 0.0010 / 100 = 0.7928, matching to ~0.2%; "
                 "a fraction reading would be 100x off); (2) the Nautilus Lighter adapter reads "
                 "this same endpoint and signs `rate` by `direction` with no division by 100, "
                 "which is the convention `src/analysis/opportunities.py` already documents.")
    lines.append("")
    lines.append("**Aster settlement intervals used** (from `GET /fapi/v1/fundingInfo`, "
                 "fetched once and cached; symbols absent there default to 8 h):")
    lines.append("")
    if intervals:
        lines.append("`" + ", ".join(f"{k}={v}h" for k, v in sorted(intervals.items())) + "`")
    else:
        lines.append("_fundingInfo unavailable this run; 8 h assumed everywhere._")
    lines.append("")
    lines.append("A 4 h or 8 h Aster rate is spread evenly backwards over the hours it accrued "
                 "over, so it lines up with HL's and Lighter's hourly settlement. That makes the "
                 "hourly series smooth by construction: Aster's true cashflow is lumpy, and a "
                 "position closed mid-interval collects none of it.")
    lines.append("")
    lines.append("**Prices.** The basis uses 1 h candle closes, LAST TRADE on all three venues "
                 "(HL and Lighter publish no mark-price candles, so trade prices are the only "
                 "consistent choice):")
    lines.append("")
    for venue in VENUES:
        lines.append(f"- `{venue}`: {price_sources[venue]}")
    lines.append("")
    lines.append("Aster's stock perps trade only a few times an hour, so their closes are stale "
                 "relative to HL and Lighter and the basis series for those pairs is noisier than "
                 "the real executable spread. Aster does expose `/fapi/v1/markPriceKlines`; it was "
                 "not used because the other two venues have no equivalent.")
    lines.append("")
    lines.append("**Coverage.** Hours with no funding row on one leg are dropped from that pair, "
                 "and rolling 24 h / 7 d windows are computed only over fully populated stretches. "
                 f"The window is {len(grid)} h; a leg that returned materially less than that was "
                 "listed later than the window start (or trades only part of the day), which is a "
                 "fact about the market, not a fetch failure. Legs below 90% coverage:")
    lines.append("")
    short_rows = []
    for symbol in sorted(data):
        for venue in VENUES:
            series = data[symbol].funding.get(venue)
            have = len(series) if series else 0
            if have < 0.9 * len(grid):
                candles = len(data[symbol].closes.get(venue) or ())
                short_rows.append([symbol, venue, have, f"{100.0 * have / len(grid):.0f}%", candles])
    if short_rows:
        lines.extend(table(["symbol", "venue", "funding h", "coverage", "candle h"], short_rows))
    else:
        lines.append("- none: every mapped leg covered at least 90% of the window.")
    lines.append("")
    lines.append("Per-symbol notes and failures:")
    lines.append("")
    any_note = False
    for symbol in sorted(data):
        notes = data[symbol].notes
        if notes:
            any_note = True
            lines.append(f"- **{symbol}**: " + "; ".join(notes))
    if not any_note:
        lines.append("- none: every mapped symbol returned funding and candles on every venue.")
    lines.append("")
    lines.append("`GOLD` and `GOLD1` share the same HL and Lighter legs and differ only in the "
                 "Aster listing (`XAUUSDT` vs `XAUUSD1`), so their HL/Lighter rows are identical "
                 "by construction and must not be counted as two independent observations.")
    lines.append("")
    if fetch.errors:
        lines.append("**Request failures** (kept going, rows never fabricated):")
        lines.append("")
        for err in fetch.errors:
            lines.append(f"- {err}")
        lines.append("")
    lines.append("**What this study does not measure.** Depth and slippage (see "
                 "`src/analysis/opportunities.py` for the live-book capacity work), margin and "
                 "liquidation mechanics on either leg, funding-rate caps, borrow, or the risk "
                 "that a HIP-3 stock perp halts while its Aster or Lighter twin keeps trading. "
                 "Trading-hours differences on the stock names mean the basis series contains "
                 "hours where one venue's price is simply not moving.")
    lines.append("")
    lines.append(f"_Raw responses cached under `{args.out}/raw/`; delete that directory or pass "
                 "`--refresh` to refetch._")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Cross-venue funding-carry study from public history (HL / Lighter / Aster).",
    )
    parser.add_argument("--days", type=int, default=60, help="lookback window in days (default 60)")
    parser.add_argument("--symbols", default=None,
                        help="comma list; default every symbol in the shared table")
    parser.add_argument("--hurdle-bps", type=float, default=6.0,
                        help="round-trip taker cost across both venues (default 6)")
    parser.add_argument("--out", default="reports/carry", help="output directory")
    parser.add_argument("--sleep", type=float, default=0.35, help="seconds between HTTP calls")
    parser.add_argument("--retries", type=int, default=4, help="attempts per HTTP call")
    parser.add_argument("--refresh", action="store_true", help="ignore the raw cache and refetch")
    parser.add_argument("--quiet", action="store_true", help="suppress per-symbol progress")
    args = parser.parse_args(argv)

    if args.days < 1:
        parser.error("--days must be >= 1")

    instruments, source_note = load_instruments()
    if args.symbols:
        symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
        unknown = [s for s in symbols if s not in instruments]
        if unknown:
            parser.error(f"unknown symbols {unknown}; known: {sorted(instruments)}")
    else:
        symbols = sorted(instruments)

    end_s = floor_hour_s(int(time.time()))
    start_s = end_s - args.days * 24 * HOUR
    grid = list(range(start_s, end_s, HOUR))

    out_dir = Path(args.out)
    raw_dir = out_dir / "raw"
    out_dir.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)

    fetch = Fetcher(raw_dir, args.sleep, args.retries, args.refresh, args.quiet)
    print(f"[carry] window {iso_hour(start_s)} .. {iso_hour(end_s)} "
          f"({len(grid)} h), {len(symbols)} symbols, hurdle {args.hurdle_bps} bps")
    print(f"[carry] symbol table: {source_note}")
    started = time.time()
    data, intervals, price_sources = collect(fetch, symbols, instruments, start_s, end_s)

    # ----------------------------------------------------------- hourly CSVs
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    funding_csv = out_dir / f"funding_hourly_{stamp}.csv"
    basis_csv = out_dir / f"basis_hourly_{stamp}.csv"
    summary_csv = out_dir / f"carry_summary_{stamp}.csv"
    report_md = out_dir / f"carry_report_{stamp}.md"

    funding_rows = 0
    with funding_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "venue", "symbol", "bps_per_hour"])
        for symbol in sorted(data):
            for venue in VENUES:
                series = data[symbol].funding.get(venue)
                if not series:
                    continue
                for hour in grid:
                    value = series.get(hour)
                    if value is not None:
                        writer.writerow([iso_hour(hour), venue, symbol, round(value, 6)])
                        funding_rows += 1

    # Basis per unordered venue pair; the ordered pairs reuse it (basis(B,A) = -basis(A,B)).
    basis_by_symbol: dict[str, dict[tuple[str, str], dict[int, float]]] = {}
    basis_rows = 0
    with basis_csv.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["ts", "symbol", "venue_a", "venue_b", "close_a", "close_b", "basis_bps"])
        for symbol in sorted(data):
            closes = data[symbol].closes
            pairs: dict[tuple[str, str], dict[int, float]] = {}
            for i, va in enumerate(VENUES):
                for vb in VENUES[i + 1:]:
                    ca, cb = closes.get(va), closes.get(vb)
                    if not ca or not cb:
                        continue
                    series: dict[int, float] = {}
                    for hour in grid:
                        pa, pb = ca.get(hour), cb.get(hour)
                        if pa is None or pb is None or pb == 0:
                            continue
                        bps = (pa / pb - 1.0) * 1e4
                        series[hour] = bps
                        writer.writerow([iso_hour(hour), symbol, va, vb,
                                         round(pa, 8), round(pb, 8), round(bps, 4)])
                        basis_rows += 1
                    if series:
                        pairs[(va, vb)] = series
            basis_by_symbol[symbol] = pairs

    # ------------------------------------------------------------ pair stats
    ranked: list[PairStats] = []
    for symbol in sorted(data):
        entry = data[symbol]
        pairs = basis_by_symbol.get(symbol, {})
        for long_venue in VENUES:
            for short_venue in VENUES:
                if long_venue == short_venue:
                    continue
                key = (long_venue, short_venue)
                if key in pairs:
                    basis = pairs[key]
                elif (short_venue, long_venue) in pairs:
                    basis = {h: -v for h, v in pairs[(short_venue, long_venue)].items()}
                else:
                    basis = None
                stats = pair_stats(symbol, long_venue, short_venue, grid,
                                   entry.funding, basis, args.hurdle_bps)
                if stats is not None:
                    ranked.append(stats)
    # Pairs that never completed a 168 h window sort below every pair that did.
    ranked.sort(key=lambda s: (not s.rankable, -(s.edge_7d_bps or 0.0)))

    write_summary_csv(summary_csv, ranked)
    report = build_report(args, grid, data, ranked, intervals, price_sources,
                          fetch, source_note, datetime.now(timezone.utc).isoformat(timespec="seconds"))
    report_md.write_text(report, encoding="utf-8")

    # -------------------------------------------------------- console summary
    elapsed = time.time() - started
    print()
    print(f"[carry] http calls {fetch.calls}, cache hits {fetch.cache_hits}, "
          f"failures {len(fetch.errors)}, {elapsed:.0f}s")
    print(f"[carry] funding rows {funding_rows}, basis rows {basis_rows}, pairs scored {len(ranked)}")
    rankable = [s for s in ranked if s.rankable]
    print(f"[carry] {len(rankable)} pairs have a complete 168 h window and are ranked; "
          f"{len(ranked) - len(rankable)} are too recently listed to rank")
    if rankable:
        print(f"[carry] top {min(10, len(rankable))} by 7 d net edge "
              f"(mean bps/h, 7 d p50 carry, 7 d p10, edge after {args.hurdle_bps} bps + basis shock):")
        for i, s in enumerate(rankable[:10], 1):
            print(f"   {i:>2}. {s.symbol:<6} long {s.long_venue:<7} short {s.short_venue:<7} "
                  f"{s.hours:>5} h  mean {s.mean_bps_h:>7.3f}  "
                  f"7d p50 {fmt(s.carry_7d_p50, 1):>8}  p10 {fmt(s.carry_7d_p10, 1):>8}  "
                  f"ann {s.annualised_pct:>7.1f}%  edge {fmt(s.edge_7d_bps, 1):>8}")
    for path in (funding_csv, basis_csv, summary_csv, report_md):
        print(f"[carry] wrote {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

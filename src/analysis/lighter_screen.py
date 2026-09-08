#!/usr/bin/env python
"""Screen Lighter perp markets for PONS-like maker-spread opportunities.

Idea under test (already measured on PONS): quote passively on Lighter, where the
book is wide and fees are zero, and hedge the fill immediately as a taker on
Aster or Hyperliquid, where the book is tight.  The edge per round trip is

    room_bps = lighter_spread_bps - (hedge_spread_bps + hedge_taker_fee_bps)

This script enumerates every Lighter perp market (both the main deployment and
the Robinhood Chain deployment), samples the top of book several times, does the
same for the hedge venues, matches symbols across venues, and ranks by room.

Read-only: public info endpoints only, no keys, no orders.

Request budget
--------------
Lighter has no "all books" endpoint (``orderBookOrders`` requires ``market_id``),
so a full top-of-book pass over the main deployment costs ~233 requests.  To stay
inside the request budget the run does ONE broad pass over every market and then
``--snapshots`` deep passes over a smaller set (top markets by 24h volume, top
markets by the broad pass's room estimate, plus the calibration symbols).  The
Robinhood deployment is small enough (57 markets) to be sampled on every pass.

Only stdlib is used.  HTTP goes through ``http.client`` with keep-alive because
this host resolves DNS through a proxy where a fresh TLS handshake costs ~6s per
request while a reused connection costs ~0.12s.
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import statistics
import time
from datetime import datetime, timezone

# --------------------------------------------------------------------------------------
# Constants
# --------------------------------------------------------------------------------------

LIGHTER_MAIN_HOST = "mainnet.zklighter.elliot.ai"
LIGHTER_RH_HOST = "api.rh.lighter.xyz"
HL_HOST = "api.hyperliquid.xyz"
ASTER_HOST = "fapi.asterdex.com"

# Taker fees in bps of notional.
FEE_ASTER_TAKER_BPS = 0.9
FEE_HL_TAKER_BPS = 4.5
# HIP-3 builder dexes ("xyz" stock perps) charge the base HL rate plus a deployer
# share; the exact share is not exposed by the info API, so this is an assumption
# and is called out in the report.
FEE_HL_XYZ_TAKER_BPS = 4.5

# Lighter is zero-fee on both sides, so the maker leg costs nothing.
FEE_LIGHTER_MAKER_BPS = 0.0

CALIBRATION_SYMBOLS = ["SOL", "HYPE", "ZEC", "PONS", "LIT", "ASTER", "DASH", "PUMP", "ARB"]

# 6-letter FX pairs that must not be mangled by the quote-suffix stripper.
FX_BASES = {
    "AUDUSD", "EURUSD", "GBPUSD", "NZDUSD",
    "USDCAD", "USDCHF", "USDHKD", "USDJPY", "USDKRW", "USDCNH", "USDSGD",
}

QUOTE_SUFFIXES = ("-PERP", "_PERP", "-USD", "USDT", "USDC", "USD1", "USD")
MULTIPLIER_PREFIXES = ("1000000", "10000", "1000", "1M", "1K")


# --------------------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------------------


class Session:
    """Keep-alive HTTPS session with bounded retries."""

    def __init__(self, host, timeout=30.0, retries=3, retry_sleep=1.5):
        self.host = host
        self.timeout = timeout
        self.retries = retries
        self.retry_sleep = retry_sleep
        self.conn = None
        self.n_requests = 0
        self.n_retries = 0

    def _connect(self):
        self.conn = http.client.HTTPSConnection(self.host, timeout=self.timeout)

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None

    def request(self, method, path, body=None):
        headers = {
            "User-Agent": "nautilus-perps-lighter-screen/1.0",
            "Accept": "application/json",
            "Connection": "keep-alive",
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        last_exc = None
        for attempt in range(self.retries):
            try:
                if self.conn is None:
                    self._connect()
                self.conn.request(method, path, body=body, headers=headers)
                resp = self.conn.getresponse()
                raw = resp.read()
                if resp.status != 200:
                    raise RuntimeError("HTTP %s for %s%s" % (resp.status, self.host, path))
                self.n_requests += 1
                return json.loads(raw)
            except Exception as exc:  # noqa: BLE001 - transient TLS/proxy errors are expected
                last_exc = exc
                self.close()
                if attempt < self.retries - 1:
                    self.n_retries += 1
                    time.sleep(self.retry_sleep)
        raise last_exc

    def get(self, path):
        return self.request("GET", path)

    def post(self, path, payload):
        return self.request("POST", path, body=json.dumps(payload))


# --------------------------------------------------------------------------------------
# Symbol normalisation
# --------------------------------------------------------------------------------------


def normalise(symbol):
    """Map a venue symbol onto a common base name.

    Handles the ``xyz:`` HIP-3 dex prefix, quote-currency suffixes (``USDT``,
    ``USD1``, ``-PERP`` ...), and the two notations for scaled meme coins:
    Hyperliquid's lowercase ``k`` prefix (``kPEPE``) versus Lighter/Aster's
    ``1000`` prefix (``1000PEPE``).
    """
    s = symbol.strip()
    if ":" in s:  # e.g. "xyz:TSLA"
        s = s.split(":", 1)[1]
    # HL scales with a lowercase "k"; every real ticker is uppercase, so the
    # lowercase letter is a safe discriminator (KAITO is not touched).
    if len(s) > 2 and s[0] == "k" and s[1:].isupper():
        s = s[1:]
    s = s.upper()
    if s in FX_BASES:
        return s
    for suf in QUOTE_SUFFIXES:
        if s.endswith(suf) and len(s) > len(suf) + 1:
            s = s[: -len(suf)]
            break
    for pre in MULTIPLIER_PREFIXES:
        if s.startswith(pre) and len(s) > len(pre) + 1:
            s = s[len(pre):]
            break
    return s


# --------------------------------------------------------------------------------------
# Book helpers
# --------------------------------------------------------------------------------------


def quote_from_levels(bids, asks):
    """bids/asks are lists of (price, size). Returns (bid, bid_sz, ask, ask_sz)."""
    if not bids or not asks:
        return None
    best_bid = max(p for p, _ in bids)
    best_ask = min(p for p, _ in asks)
    bid_sz = sum(sz for p, sz in bids if p == best_bid)
    ask_sz = sum(sz for p, sz in asks if p == best_ask)
    if best_bid <= 0 or best_ask <= 0 or best_ask <= best_bid:
        return None
    return best_bid, bid_sz, best_ask, ask_sz


def spread_stats(quote):
    bid, bid_sz, ask, ask_sz = quote
    mid = 0.5 * (bid + ask)
    return {
        "spread_bps": (ask - bid) / mid * 1e4,
        "tob_bid_usd": bid * bid_sz,
        "tob_ask_usd": ask * ask_sz,
        "mid": mid,
    }


# --------------------------------------------------------------------------------------
# Raw dumps
# --------------------------------------------------------------------------------------


class Dumper:
    def __init__(self, directory):
        self.dir = directory
        os.makedirs(directory, exist_ok=True)
        self.files = []

    def dump(self, name, snapshot, payload):
        path = os.path.join(self.dir, "%s_s%d.json" % (name, snapshot))
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        self.files.append(path)


# --------------------------------------------------------------------------------------
# Venue fetchers
# --------------------------------------------------------------------------------------


def fetch_lighter_details(sess):
    payload = sess.get("/api/v1/orderBookDetails")
    out = {}
    skipped = []
    for row in payload.get("order_book_details", []):
        if row.get("market_type") != "perp":
            skipped.append((row.get("symbol"), "market_type=%s" % row.get("market_type")))
            continue
        if str(row.get("status", "active")).lower() not in ("active", ""):
            skipped.append((row.get("symbol"), "status=%s" % row.get("status")))
            continue
        out[row["symbol"]] = {
            "symbol": row["symbol"],
            "market_id": row["market_id"],
            "vol_24h": float(row.get("daily_quote_token_volume") or 0.0),
            "trades_24h": int(row.get("daily_trades_count") or 0),
            "open_interest": float(row.get("open_interest") or 0.0),
            "last_price": float(row.get("last_trade_price") or 0.0),
            "min_base_amount": row.get("min_base_amount"),
        }
    return out, payload, skipped


def fetch_lighter_book(sess, market_id, depth):
    payload = sess.get("/api/v1/orderBookOrders?market_id=%d&limit=%d" % (market_id, depth))
    bids = [(float(o["price"]), float(o["remaining_base_amount"])) for o in payload.get("bids", [])]
    asks = [(float(o["price"]), float(o["remaining_base_amount"])) for o in payload.get("asks", [])]
    return quote_from_levels(bids, asks), payload


def fetch_hl_meta(sess, dex):
    req = {"type": "metaAndAssetCtxs"}
    if dex:
        req["dex"] = dex
    payload = sess.post("/info", req)
    meta, ctxs = payload[0], payload[1]
    out = {}
    for uni, ctx in zip(meta["universe"], ctxs):
        if uni.get("isDelisted"):
            continue
        name = uni["name"]
        out[name] = {
            "symbol": name,
            "vol_24h": float(ctx.get("dayNtlVlm") or 0.0),
            "mark": float(ctx.get("markPx") or 0.0),
        }
    return out, payload


def fetch_hl_book(sess, coin):
    payload = sess.post("/info", {"type": "l2Book", "coin": coin})
    levels = payload.get("levels") or [[], []]
    bids = [(float(x["px"]), float(x["sz"])) for x in levels[0]]
    asks = [(float(x["px"]), float(x["sz"])) for x in levels[1]]
    return quote_from_levels(bids, asks), payload


def fetch_aster(sess):
    """Two calls cover every Aster symbol."""
    tickers = sess.get("/fapi/v1/ticker/24hr")
    books = sess.get("/fapi/v1/ticker/bookTicker")
    vol = {}
    for t in tickers:
        vol[t["symbol"]] = {
            "vol_24h": float(t.get("quoteVolume") or 0.0),
            "trades_24h": int(t.get("count") or 0),
        }
    quotes = {}
    for b in books:
        try:
            bid, bid_sz = float(b["bidPrice"]), float(b["bidQty"])
            ask, ask_sz = float(b["askPrice"]), float(b["askQty"])
        except (KeyError, TypeError, ValueError):
            continue
        q = quote_from_levels([(bid, bid_sz)], [(ask, ask_sz)])
        if q is not None:
            quotes[b["symbol"]] = q
    return vol, quotes, {"ticker24hr": tickers, "bookTicker": books}


# --------------------------------------------------------------------------------------
# Accumulators
# --------------------------------------------------------------------------------------


class Acc:
    """Collects per-snapshot spread/TOB observations for one symbol on one venue."""

    def __init__(self):
        self.spread_bps = []
        self.tob_bid_usd = []
        self.tob_ask_usd = []
        self.mid = []
        self.failures = 0

    def add(self, quote):
        if quote is None:
            self.failures += 1
            return
        st = spread_stats(quote)
        self.spread_bps.append(st["spread_bps"])
        self.tob_bid_usd.append(st["tob_bid_usd"])
        self.tob_ask_usd.append(st["tob_ask_usd"])
        self.mid.append(st["mid"])

    @property
    def n(self):
        return len(self.spread_bps)

    def summary(self):
        if not self.spread_bps:
            return None
        return {
            "n": self.n,
            "spread_bps": statistics.median(self.spread_bps),
            "spread_bps_min": min(self.spread_bps),
            "spread_bps_max": max(self.spread_bps),
            "tob_bid_usd": statistics.median(self.tob_bid_usd),
            "tob_ask_usd": statistics.median(self.tob_ask_usd),
            "mid": statistics.median(self.mid),
        }


def newacc(store, key):
    if key not in store:
        store[key] = Acc()
    return store[key]


# --------------------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------------------


def fmt_usd(x):
    if x is None:
        return "-"
    if x >= 1e9:
        return "%.2fB" % (x / 1e9)
    if x >= 1e6:
        return "%.2fM" % (x / 1e6)
    if x >= 1e3:
        return "%.1fk" % (x / 1e3)
    return "%.0f" % x


def fmt_bps(x):
    return "-" if x is None else "%.2f" % x


def fmt_int(x):
    return "-" if x is None else "{:,}".format(int(x))


# --------------------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------------------


def parse_args():
    p = argparse.ArgumentParser(
        description="Screen Lighter perp markets for PONS-like maker-spread opportunities.",
    )
    p.add_argument("--snapshots", type=int, default=5,
                   help="deep passes after the initial broad pass (default 5)")
    p.add_argument("--interval-s", type=float, default=60.0, help="seconds between passes (default 60)")
    p.add_argument("--depth", type=int, default=10, help="Lighter orderBookOrders limit per side (default 10)")
    p.add_argument("--min-lighter-vol", type=float, default=200000.0,
                   help="minimum Lighter 24h quote volume, USD (default 200k)")
    p.add_argument("--min-hedge-vol", type=float, default=1000000.0,
                   help="minimum hedge-venue 24h volume, USD (default 1M)")
    p.add_argument("--deep-top-n", type=int, default=40,
                   help="deep-sample the top N Lighter markets by 24h volume (default 40)")
    p.add_argument("--deep-room-n", type=int, default=30,
                   help="also deep-sample the top N by the broad pass's room estimate (default 30)")
    p.add_argument("--all-markets-every-pass", action="store_true",
                   help="sample every market on every pass (ignores --deep-*; ~470 requests/pass)")
    p.add_argument("--skip-rh", action="store_true", help="skip the Lighter Robinhood Chain deployment")
    p.add_argument("--skip-hl-xyz", action="store_true", help="skip the Hyperliquid xyz (HIP-3 stock) dex")
    p.add_argument("--hl-taker-bps", type=float, default=FEE_HL_TAKER_BPS)
    p.add_argument("--hl-xyz-taker-bps", type=float, default=FEE_HL_XYZ_TAKER_BPS)
    p.add_argument("--aster-taker-bps", type=float, default=FEE_ASTER_TAKER_BPS)
    p.add_argument("--md", default="reports/lighter-screen.md", help="markdown report path")
    p.add_argument("--raw-dir", required=True, help="directory for raw JSON snapshots (outside the repo)")
    p.add_argument("--top-rows", type=int, default=60, help="rows to print in the ranked table")
    p.add_argument("--shortlist-n", type=int, default=8, help="size of the 'record next' shortlist")
    p.add_argument("--shortlist-min-flow", type=float, default=300000.0,
                   help="minimum flow proxy for the shortlist, USD (default 300k)")
    return p.parse_args()


def main():  # noqa: C901 - a linear script, easier to read in one piece
    args = parse_args()
    t_start = time.time()
    started_utc = datetime.now(timezone.utc)
    dumper = Dumper(args.raw_dir)
    errors = []

    sess = {
        "lighter_main": Session(LIGHTER_MAIN_HOST),
        "lighter_rh": Session(LIGHTER_RH_HOST),
        "hl": Session(HL_HOST),
        "aster": Session(ASTER_HOST),
    }

    # ---------------------------------------------------------------- static metadata
    lighter_meta = {}          # deployment -> {symbol: info}
    lighter_skipped = {}       # deployment -> [(symbol, reason)]
    for dep, host_key in (("LIGHTER", "lighter_main"), ("LIGHTER_RH", "lighter_rh")):
        if dep == "LIGHTER_RH" and args.skip_rh:
            continue
        try:
            meta, raw, skipped = fetch_lighter_details(sess[host_key])
            lighter_meta[dep] = meta
            lighter_skipped[dep] = skipped
            dumper.dump("%s_orderBookDetails" % dep.lower(), 0, raw)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s orderBookDetails failed: %r" % (dep, exc))
            lighter_meta[dep] = {}
            lighter_skipped[dep] = []

    hl_meta = {}
    for venue, dex in (("HL", None), ("HL_XYZ", "xyz")):
        if venue == "HL_XYZ" and args.skip_hl_xyz:
            continue
        try:
            meta, raw = fetch_hl_meta(sess["hl"], dex)
            hl_meta[venue] = meta
            dumper.dump("hl_%s_metaAndAssetCtxs" % venue.lower(), 0, raw)
        except Exception as exc:  # noqa: BLE001
            errors.append("%s metaAndAssetCtxs failed: %r" % (venue, exc))
            hl_meta[venue] = {}

    # ------------------------------------------------------------------- hedge index
    # normalised base -> venue -> {symbol, vol_24h, fee_bps}
    hedge_index = {}

    def register_hedge(base, venue, symbol, vol, fee, trades=None):
        slot = hedge_index.setdefault(base, {})
        prev = slot.get(venue)
        if prev is None or vol > prev["vol_24h"]:
            slot[venue] = {"symbol": symbol, "vol_24h": vol, "fee_bps": fee, "trades_24h": trades}

    for venue, fee in (("HL", args.hl_taker_bps), ("HL_XYZ", args.hl_xyz_taker_bps)):
        for sym, info in hl_meta.get(venue, {}).items():
            register_hedge(normalise(sym), venue, sym, info["vol_24h"], fee)

    aster_vol = {}
    aster_quotes = {}
    try:
        aster_vol, aster_quotes, raw = fetch_aster(sess["aster"])
        dumper.dump("aster_ticker", 0, raw)
        for sym, info in aster_vol.items():
            register_hedge(normalise(sym), "ASTER", sym, info["vol_24h"], args.aster_taker_bps,
                           info["trades_24h"])
    except Exception as exc:  # noqa: BLE001
        errors.append("Aster tickers failed: %r" % (exc,))

    # -------------------------------------------------------------- sampling targets
    lighter_all = [(dep, sym) for dep, meta in lighter_meta.items() for sym in meta]
    lighter_bases = set(normalise(sym) for _, sym in lighter_all)
    hl_targets = []  # (venue, hl symbol)
    for venue in ("HL", "HL_XYZ"):
        for sym in hl_meta.get(venue, {}):
            if normalise(sym) in lighter_bases:
                hl_targets.append((venue, sym))

    lighter_acc = {}
    hedge_acc = {}   # (venue, base) -> Acc
    per_market_failures = {}

    def sample_lighter(targets, snapshot):
        by_dep = {}
        for dep, sym in targets:
            info = lighter_meta[dep][sym]
            host_key = "lighter_main" if dep == "LIGHTER" else "lighter_rh"
            try:
                quote, raw = fetch_lighter_book(sess[host_key], info["market_id"], args.depth)
                by_dep.setdefault(dep, {})[sym] = raw
            except Exception as exc:  # noqa: BLE001
                quote = None
                key = "%s:%s" % (dep, sym)
                per_market_failures[key] = per_market_failures.get(key, 0) + 1
                if len(errors) < 200:
                    errors.append("%s orderBookOrders %s snapshot %d: %r" % (dep, sym, snapshot, exc))
            newacc(lighter_acc, (dep, sym)).add(quote)
        for dep, payload in by_dep.items():
            dumper.dump("%s_orderBookOrders" % dep.lower(), snapshot, payload)

    def sample_hl(targets, snapshot):
        by_venue = {}
        for venue, sym in targets:
            try:
                quote, raw = fetch_hl_book(sess["hl"], sym)
                by_venue.setdefault(venue, {})[sym] = raw
            except Exception as exc:  # noqa: BLE001
                quote = None
                key = "%s:%s" % (venue, sym)
                per_market_failures[key] = per_market_failures.get(key, 0) + 1
                if len(errors) < 200:
                    errors.append("%s l2Book %s snapshot %d: %r" % (venue, sym, snapshot, exc))
            newacc(hedge_acc, (venue, normalise(sym))).add(quote)
        for venue, payload in by_venue.items():
            dumper.dump("hl_%s_l2Book" % venue.lower(), snapshot, payload)

    def sample_aster(snapshot):
        try:
            vol, quotes, raw = fetch_aster(sess["aster"])
            dumper.dump("aster_ticker", snapshot, raw)
        except Exception as exc:  # noqa: BLE001
            errors.append("Aster tickers snapshot %d: %r" % (snapshot, exc))
            return
        aster_vol.update(vol)
        aster_quotes.update(quotes)
        for sym, q in quotes.items():
            base = normalise(sym)
            best = hedge_index.get(base, {}).get("ASTER")
            if best is not None and best["symbol"] == sym:
                newacc(hedge_acc, ("ASTER", base)).add(q)

    # ------------------------------------------------------------------ broad pass
    print("[pass 0] broad: %d Lighter markets, %d HL books" % (len(lighter_all), len(hl_targets)))
    t0 = time.time()
    sample_lighter(lighter_all, 0)
    sample_hl(hl_targets, 0)
    sample_aster(0)
    print("[pass 0] done in %.1fs" % (time.time() - t0))

    # ------------------------------------------------------- pick the deep-sample set
    def broad_room(dep, sym):
        acc = lighter_acc.get((dep, sym))
        if acc is None or acc.n == 0:
            return None
        lspread = acc.spread_bps[0]
        base = normalise(sym)
        best = None
        for venue in ("ASTER", "HL", "HL_XYZ"):
            hacc = hedge_acc.get((venue, base))
            hinfo = hedge_index.get(base, {}).get(venue)
            if hacc is None or hacc.n == 0 or hinfo is None:
                continue
            room = lspread - (hacc.spread_bps[0] + hinfo["fee_bps"])
            if best is None or room > best:
                best = room
        return best

    if args.all_markets_every_pass:
        deep_targets = list(lighter_all)
    else:
        by_vol = sorted(lighter_all, key=lambda t: lighter_meta[t[0]][t[1]]["vol_24h"], reverse=True)
        deep = []
        seen = set()

        def add(t):
            if t not in seen:
                seen.add(t)
                deep.append(t)

        # Robinhood Chain is small: sample all of it every pass.
        for t in lighter_all:
            if t[0] == "LIGHTER_RH":
                add(t)
        n_main = 0
        for t in by_vol:
            if t[0] == "LIGHTER" and n_main < args.deep_top_n:
                add(t)
                n_main += 1
        scored = []
        for dep, sym in lighter_all:
            if lighter_meta[dep][sym]["vol_24h"] < args.min_lighter_vol:
                continue
            r = broad_room(dep, sym)
            if r is not None:
                scored.append((r, (dep, sym)))
        scored.sort(key=lambda s: s[0], reverse=True)
        for _, t in scored[: args.deep_room_n]:
            add(t)
        for dep, meta in lighter_meta.items():
            for sym in CALIBRATION_SYMBOLS:
                if sym in meta:
                    add((dep, sym))
        deep_targets = deep

    deep_bases = set(normalise(sym) for _, sym in deep_targets)
    hl_deep = [(v, s) for v, s in hl_targets if normalise(s) in deep_bases]

    # ------------------------------------------------------------------ deep passes
    for k in range(1, args.snapshots + 1):
        wait = args.interval_s - (time.time() - t0)
        if wait > 0:
            time.sleep(wait)
        t0 = time.time()
        print("[pass %d] deep: %d Lighter, %d HL" % (k, len(deep_targets), len(hl_deep)))
        sample_lighter(deep_targets, k)
        sample_hl(hl_deep, k)
        sample_aster(k)
        print("[pass %d] done in %.1fs" % (k, time.time() - t0))

    ended_utc = datetime.now(timezone.utc)
    for s in sess.values():
        s.close()

    # ------------------------------------------------------------------- build rows
    rows = []
    unmatched = []
    for dep, sym in lighter_all:
        info = lighter_meta[dep][sym]
        acc = lighter_acc.get((dep, sym))
        summ = acc.summary() if acc else None
        base = normalise(sym)
        hedges = []
        for venue in ("ASTER", "HL", "HL_XYZ"):
            hinfo = hedge_index.get(base, {}).get(venue)
            if hinfo is None:
                continue
            hacc = hedge_acc.get((venue, base))
            hsumm = hacc.summary() if hacc else None
            hedges.append({
                "venue": venue,
                "symbol": hinfo["symbol"],
                "vol_24h": hinfo["vol_24h"],
                "fee_bps": hinfo["fee_bps"],
                "spread_bps": hsumm["spread_bps"] if hsumm else None,
                "tob_bid_usd": hsumm["tob_bid_usd"] if hsumm else None,
                "tob_ask_usd": hsumm["tob_ask_usd"] if hsumm else None,
                "n": hsumm["n"] if hsumm else 0,
            })
        row = {
            "deployment": dep,
            "symbol": sym,
            "base": base,
            "vol_24h": info["vol_24h"],
            "trades_24h": info["trades_24h"],
            "open_interest_usd": info["open_interest"] * info["last_price"],
            "n": summ["n"] if summ else 0,
            "spread_bps": summ["spread_bps"] if summ else None,
            "spread_bps_min": summ["spread_bps_min"] if summ else None,
            "spread_bps_max": summ["spread_bps_max"] if summ else None,
            "tob_bid_usd": summ["tob_bid_usd"] if summ else None,
            "tob_ask_usd": summ["tob_ask_usd"] if summ else None,
            "hedges": hedges,
            "best": None,
        }
        if not hedges:
            unmatched.append(row)
            continue
        best = None
        for h in hedges:
            if h["spread_bps"] is None or row["spread_bps"] is None:
                continue
            room = row["spread_bps"] - (h["spread_bps"] + h["fee_bps"])
            cand = dict(h)
            cand["room_bps"] = room
            cand["flow_proxy"] = min(row["vol_24h"], h["vol_24h"])
            if best is None or room > best["room_bps"]:
                best = cand
        row["best"] = best
        rows.append(row)

    # ------------------------------------------------------------------ filtering
    passed, rejected = [], []
    for row in rows:
        best = row.get("best")
        if row["spread_bps"] is None:
            rejected.append((row, "no Lighter two-sided quote sampled"))
            continue
        if best is None:
            rejected.append((row, "hedge listed but no hedge book sampled"))
            continue
        if row["vol_24h"] < args.min_lighter_vol:
            rejected.append((row, "Lighter 24h vol %s < %s"
                             % (fmt_usd(row["vol_24h"]), fmt_usd(args.min_lighter_vol))))
            continue
        if best["vol_24h"] < args.min_hedge_vol:
            rejected.append((row, "%s 24h vol %s < %s"
                             % (best["venue"], fmt_usd(best["vol_24h"]), fmt_usd(args.min_hedge_vol))))
            continue
        passed.append(row)
    passed.sort(key=lambda r: r["best"]["room_bps"], reverse=True)
    rejected.sort(key=lambda rr: rr[0]["spread_bps"] or 0.0, reverse=True)

    # ------------------------------------------------------------------- markdown
    out = []
    w = out.append
    dur = time.time() - t_start
    w("# Lighter maker-spread screen")
    w("")
    w("- Sampling window (UTC): **%s** to **%s** (%.1f min wall clock)"
      % (started_utc.strftime("%Y-%m-%d %H:%M:%S"), ended_utc.strftime("%Y-%m-%d %H:%M:%S"), dur / 60))
    w("- Passes: 1 broad pass over every market + %d deep passes, %.0fs apart, `--depth %d`"
      % (args.snapshots, args.interval_s, args.depth))
    # US Eastern is UTC-5 (EST) / UTC-4 (EDT); -5 is the conservative label for a
    # "is the US awake?" note and is only used for prose.
    et_hour = (started_utc.hour - 5) % 24
    w("- **Low-activity hours.** The window above is %02d:00 UTC, about %02d:00 US Eastern - the middle "
      "of the US night. US equities are closed, equity-linked and stock-perp flow is thin, and crypto "
      "books are wider than during the US/EU sessions. Treat the absolute spread numbers as an upper "
      "bound on what a US-session recording would show; the 24h volume and trade-count columns are "
      "session-independent and are the reliable half of the table."
      % (started_utc.hour, et_hour))
    w("- Fees assumed (bps, taker): Aster %.2f, Hyperliquid main %.2f, Hyperliquid `xyz` HIP-3 dex %.2f "
      "(assumption: the HIP-3 deployer fee share is not exposed by the info API, so the real cost there "
      "is >= this number)." % (args.aster_taker_bps, args.hl_taker_bps, args.hl_xyz_taker_bps))
    w("- Lighter maker fee %.1f bps, taker 0 bps (both confirmed in the `orderBooks` payload)."
      % FEE_LIGHTER_MAKER_BPS)
    w("- `room_bps = lighter_spread - (hedge_spread + hedge_taker_fee)`: the gross width available to a "
      "two-sided maker on Lighter who hedges every fill as a taker. A full round trip earns roughly the "
      "whole number minus adverse selection and inventory drift.")
    w("- `flow proxy = min(lighter 24h vol, hedge 24h vol)` - both legs must be liquid for the trade to "
      "exist at size.")
    w("")

    all_rows = rows + unmatched
    n_hl = sum(1 for r in all_rows if any(h["venue"] == "HL" for h in r["hedges"]))
    n_xyz = sum(1 for r in all_rows if any(h["venue"] == "HL_XYZ" for h in r["hedges"]))
    n_ast = sum(1 for r in all_rows if any(h["venue"] == "ASTER" for h in r["hedges"]))
    w("## Coverage")
    w("")
    w("| item | count |")
    w("|---|---|")
    for dep in sorted(lighter_meta):
        sk = lighter_skipped.get(dep, [])
        w("| Lighter `%s` perp markets (active) | %d |" % (dep, len(lighter_meta[dep])))
        if sk:
            w("| ...`%s` markets skipped as inactive/delisted | %d (%s) |"
              % (dep, len(sk), ", ".join(s for s, _ in sk[:20]) + (" ..." if len(sk) > 20 else "")))
    w("| ...with an Aster match | %d |" % n_ast)
    w("| ...with a Hyperliquid main-dex match | %d |" % n_hl)
    w("| ...with a Hyperliquid `xyz` HIP-3 match | %d |" % n_xyz)
    w("| ...with no match on any hedge venue | %d |" % len(unmatched))
    w("| passed both volume filters | %d |" % len(passed))
    w("| filtered out | %d |" % len(rejected))
    w("| deep-sampled Lighter markets | %d |" % len(deep_targets))
    for name in sorted(sess):
        w("| HTTP requests `%s` (retries) | %d (%d) |" % (name, sess[name].n_requests, sess[name].n_retries))
    w("")

    def table(rws, title, limit=None):
        w("## %s" % title)
        w("")
        w("| # | Lighter mkt | L spread bps | n | L TOB bid $ | L TOB ask $ | L 24h vol | L trades | "
          "hedge | H spread bps | fee bps | **room bps** | H 24h vol | flow proxy |")
        w("|---:|---|---:|---:|---:|---:|---:|---:|---|---:|---:|---:|---:|---:|")
        shown = rws if limit is None else rws[:limit]
        for i, r in enumerate(shown, 1):
            b = r["best"]
            dep = "" if r["deployment"] == "LIGHTER" else " (RH)"
            w("| %d | %s%s | %s | %d | %s | %s | %s | %s | %s:%s | %s | %.2f | **%+.2f** | %s | %s |"
              % (i, r["symbol"], dep, fmt_bps(r["spread_bps"]), r["n"],
                 fmt_usd(r["tob_bid_usd"]), fmt_usd(r["tob_ask_usd"]), fmt_usd(r["vol_24h"]),
                 fmt_int(r["trades_24h"]), b["venue"], b["symbol"], fmt_bps(b["spread_bps"]),
                 b["fee_bps"], b["room_bps"], fmt_usd(b["vol_24h"]), fmt_usd(b["flow_proxy"])))
        if limit is not None and len(rws) > limit:
            w("")
            w("_%d further rows below the cut._" % (len(rws) - limit))
        w("")

    table(passed,
          "Ranked candidates (passed filters: Lighter 24h vol >= %s, hedge 24h vol >= %s)"
          % (fmt_usd(args.min_lighter_vol), fmt_usd(args.min_hedge_vol)),
          args.top_rows)

    # ------------------------------------------------------------------- shortlist
    shortlist = [r for r in passed
                 if r["best"]["room_bps"] > 0.0
                 and r["best"]["flow_proxy"] >= args.shortlist_min_flow][: args.shortlist_n]
    w("## Shortlist to record next")
    w("")
    w("Passing rows with positive room and a flow proxy of at least %s, best first. `hedges` lists "
      "every venue that carries the base, not just the cheapest one - a name on two hedge venues can "
      "be re-routed when one of them widens." % fmt_usd(args.shortlist_min_flow))
    w("")
    w("| # | Lighter mkt | room bps | L spread bps | L 24h vol | L trades | flow proxy | best hedge | "
      "hedges available |")
    w("|---:|---|---:|---:|---:|---:|---:|---|---|")
    for i, r in enumerate(shortlist, 1):
        b = r["best"]
        dep = "" if r["deployment"] == "LIGHTER" else " (RH)"
        avail = ", ".join("%s (%s)" % (h["venue"], fmt_usd(h["vol_24h"])) for h in r["hedges"])
        w("| %d | %s%s | **%+.2f** | %s | %s | %s | %s | %s:%s | %s |"
          % (i, r["symbol"], dep, b["room_bps"], fmt_bps(r["spread_bps"]), fmt_usd(r["vol_24h"]),
             fmt_int(r["trades_24h"]), fmt_usd(b["flow_proxy"]), b["venue"], b["symbol"], avail))
    w("")

    # calibration block
    w("## Calibration: the 9 symbols we already recorded")
    w("")
    w("Expected from the 11.6 h recording: PONS near the top; ZEC / LIT / HYPE / SOL near the bottom "
      "(Lighter spread 1-2.4 bps, narrower than the hedge, so negative room); ASTER wide but too thin "
      "to carry inventory.")
    w("")
    w("| symbol | deployment | rank | L spread bps | hedge | H spread bps | room bps | L 24h vol | verdict |")
    w("|---|---|---:|---:|---|---:|---:|---:|---|")
    rank_of = {}
    for i, r in enumerate(passed, 1):
        rank_of[(r["deployment"], r["symbol"])] = i
    reject_reason = {}
    for r, reason in rejected:
        reject_reason[(r["deployment"], r["symbol"])] = reason
    for sym in CALIBRATION_SYMBOLS:
        hits = [r for r in all_rows if r["symbol"] == sym]
        if not hits:
            w("| %s | - | - | - | - | - | - | - | not listed on Lighter |" % sym)
            continue
        for r in hits:
            key = (r["deployment"], r["symbol"])
            rk = rank_of.get(key)
            b = r.get("best")
            if rk:
                verdict = "rank %d / %d" % (rk, len(passed))
            else:
                verdict = "filtered: %s" % reject_reason.get(key, "no hedge match on any venue")
            w("| %s | %s | %s | %s | %s | %s | %s | %s | %s |"
              % (sym, r["deployment"], rk if rk else "-", fmt_bps(r["spread_bps"]),
                 (b["venue"] + ":" + b["symbol"]) if b else "-",
                 fmt_bps(b["spread_bps"]) if b else "-",
                 ("%+.2f" % b["room_bps"]) if b else "-",
                 fmt_usd(r["vol_24h"]), verdict))
    w("")

    w("## Filtered out")
    w("")
    w("| Lighter mkt | L spread bps | n | L 24h vol | best hedge | room bps | reason |")
    w("|---|---:|---:|---:|---|---:|---|")
    for r, reason in rejected:
        b = r.get("best")
        dep = "" if r["deployment"] == "LIGHTER" else " (RH)"
        w("| %s%s | %s | %d | %s | %s | %s | %s |"
          % (r["symbol"], dep, fmt_bps(r["spread_bps"]), r["n"], fmt_usd(r["vol_24h"]),
             (b["venue"] + ":" + b["symbol"]) if b else "-",
             ("%+.2f" % b["room_bps"]) if b else "-", reason))
    w("")

    w("## Appendix: Lighter markets with no hedge on Aster or Hyperliquid")
    w("")
    w("Kept here rather than dropped silently - these are Lighter-only listings (Korean and Chinese "
      "equities, pre-IPO names, FX crosses, rates) where the hedge would have to come from somewhere "
      "else entirely.")
    w("")
    w("| Lighter mkt | deployment | L spread bps | n | L 24h vol | L trades |")
    w("|---|---|---:|---:|---:|---:|")
    for r in sorted(unmatched, key=lambda x: x["vol_24h"], reverse=True):
        w("| %s | %s | %s | %d | %s | %s |"
          % (r["symbol"], r["deployment"], fmt_bps(r["spread_bps"]), r["n"],
             fmt_usd(r["vol_24h"]), fmt_int(r["trades_24h"])))
    w("")

    w("## Errors and failed endpoints")
    w("")
    if errors:
        for e in errors[:60]:
            w("- `%s`" % e)
        if len(errors) > 60:
            w("- ... and %d more" % (len(errors) - 60))
    else:
        w("None - every endpoint answered on every pass.")
    w("")

    w("## Method")
    w("")
    w("- Lighter market list and 24h stats: `GET /api/v1/orderBookDetails`. Called without `market_id` it "
      "returns every market in one response (`daily_quote_token_volume`, `daily_trades_count`, "
      "`open_interest`, `last_trade_price`).")
    w("- Lighter top of book: `GET /api/v1/orderBookOrders?market_id=N&limit=K`. This returns raw resting "
      "orders, not aggregated price levels, so sizes at the best price are summed. There is no all-market "
      "book endpoint (the call 400s without `market_id`), which is why the run is split into a broad pass "
      "over every market plus deep passes over a smaller set.")
    w("- Lighter Robinhood Chain deployment: base `https://api.rh.lighter.xyz`, same API shape. The URL "
      "is not in any Python source; it is a string constant inside the Rust adapter "
      "(`_libnautilus` binary, alongside `https://mainnet.zklighter.elliot.ai`).")
    w("- Hyperliquid: one `POST /info {\"type\":\"metaAndAssetCtxs\"}` per dex for the universe and 24h "
      "notional, then `POST /info {\"type\":\"l2Book\",\"coin\":C}` per matched coin. The HIP-3 stock dex "
      "is reached with `{\"dex\":\"xyz\"}` and coin names prefixed `xyz:` - both work on the public info "
      "endpoint with no extra setup, so its markets are included as a hedge venue (`HL_XYZ`).")
    w("- Aster: `GET /fapi/v1/ticker/24hr` and `GET /fapi/v1/ticker/bookTicker` - two calls cover every "
      "symbol. `exchangeInfo` is deliberately not called (strict rate limit).")
    w("- Symbol matching normalises `USDT`/`USDC`/`USD1`/`USD`/`-PERP` suffixes, the `xyz:` prefix, and the "
      "two scaled-token notations (Hyperliquid `kPEPE` vs Lighter/Aster `1000PEPE`). Six-letter FX pairs "
      "are whitelisted so `AUDUSD` is not truncated to `AUD`. When a venue lists several contracts on the "
      "same base (e.g. Aster `XAUUSDT` and `XAUUSD1`) the higher-volume one is used.")
    w("- Raw responses for every endpoint and pass are written outside the repo, one file per endpoint per "
      "pass (`%s`)." % args.raw_dir)
    w("")
    w("Generated by `src/analysis/lighter_screen.py` in %.1f min." % (dur / 60))
    w("")

    md_dir = os.path.dirname(os.path.abspath(args.md))
    if md_dir:
        os.makedirs(md_dir, exist_ok=True)
    with open(args.md, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out))
    print("wrote %s (%d lines), raw dumps: %d files in %s"
          % (args.md, len(out), len(dumper.files), args.raw_dir))
    print("total runtime %.2f min; passed=%d rejected=%d unmatched=%d"
          % (dur / 60, len(passed), len(rejected), len(unmatched)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

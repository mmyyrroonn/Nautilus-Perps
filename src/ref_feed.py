#!/usr/bin/env python3
"""
Reference-price leg: the real US equity quote, streamed next to the perp legs.

The cross-venue watcher (``src/spread_watch.py``) records perp-vs-perp spreads.
This module adds the *leading* price - the actual stock on a US exchange, taken
from Futu's FUTUNN OPEN API quote WebSocket - so the run can answer: when the
stock moves, do the on-chain perps leave stale quotes behind, how much is left
after taker fees, and how long does the window stay open. Measurement only: no
orders, no execution client.

Two feeds implement the same tiny interface (``start(on_update)`` / ``stop()``):

* ``FutuFeed``  - real quotes over ``wss://webapi-quote.futunn.com/ws``.
* ``FakeFeed``  - a random walk, so the whole path can be tested on a box that
  has no Futu credentials.

``RefActor`` is the Nautilus side: it owns a feed and republishes every update
on the message bus as ``CustomData(DataType("RefUpdate", {"code": ...}))`` so
any strategy in the same node can subscribe to it.

Futu documentation used here (fetched 2026-09-08):

* auth frame + signature original string:
  https://open.futunn.com/zh-cn/api/quote/push/auth
* subscribe / unsubscribe frames and responses:
  https://open.futunn.com/zh-cn/api/quote/push/subscribe
* push message payloads (QUOTE / ORDER_BOOK / TICKER):
  https://open.futunn.com/zh-cn/api/quote/push/data-format
* keep-alive (protocol ping/pong only, no business heartbeat) and reconnect:
  https://open.futunn.com/zh-cn/api/quote/push/heartbeat
* channel error codes (4xxx/5xxx) vs subscribe response codes (0-5):
  https://open.futunn.com/zh-cn/api/quote/push/error-codes
* AppKey creation, Ed25519 / RSA-SHA256 choice:
  https://open.futunn.com/zh-cn/api/overview/getting-started
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import random
import secrets
import threading
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

# --------------------------------------------------------------------------------- constants

FUTU_WS_URL = "wss://webapi-quote.futunn.com/ws"
# Auth-frame signature original string, joined with "\n" (quote/push/auth):
#   {timestamp_ms}\n{nonce}\nWEBSOCKET\nws/auth
# This is NOT the REST signature (which is timestamp/method/path/query/body-sha256).
WS_SIGN_METHOD = "WEBSOCKET"
WS_SIGN_PATH = "ws/auth"
# The channel drops a session that goes 10 minutes without a refresh frame; the
# doc recommends refreshing every 5 minutes, which is what we do.
REFRESH_SECS = 300
AUTH_TIMEOUT_SECS = 15.0
SUB_TIMEOUT_SECS = 15.0
PING_INTERVAL_SECS = 20
RECONNECT_BACKOFF = (1.0, 2.0, 5.0, 10.0, 30.0)

# What FutuFeed subscribes when FUTU_SUB_KINDS is unset (see FutuFeed.__init__).
DEFAULT_SUB_KINDS = ("order_book", "ticker")
KIND_QUOTE = "quote"
KIND_BOOK = "book"
KIND_TICKER = "ticker"
# A book from an exchange that has been quiet this long is dropped from the
# composite touch: a stale side would otherwise widen or cross the reference.
BOOK_STALE_NS = 5_000_000_000


class RefFeedError(RuntimeError):
    """Anything that makes a reference feed unusable: bad config, auth, subscribe."""


# --------------------------------------------------------------------------------- update


@dataclass
class RefUpdate:
    """One reference-price observation, whatever its source message type was.

    ``ts_event`` / ``ts_init`` exist because ``nautilus_trader.model.CustomData``
    reads those two attributes off the payload it wraps (verified against the rc4
    build: ``CustomData(dt, obj).ts_event`` is ``obj.ts_event``). Event time is the
    venue timestamp, init time is when this process first saw the bytes.
    """

    code: str  # "US.NVDA"
    kind: str  # quote / book / ticker
    ts_src_ns: int  # source timestamp, UTC nanoseconds (0 when the message has none)
    ts_recv_ns: int  # time.time_ns() stamped in the receiving thread
    last: float | None = None
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    seq: int | None = None
    # The push envelope's own "timestamp": when Futu's server emitted the frame.
    # ts_src_ns -> ts_srv_ns measures exchange -> Futu, ts_srv_ns -> ts_recv_ns
    # measures Futu -> us. 0 when the frame carried no envelope timestamp.
    ts_srv_ns: int = 0
    # ORDER_BOOK only: the exchange this book belongs to, from the top level's
    # mpid ("NSDQ" / "ARCA" / "NASD"). Each frame is one exchange's own book.
    venue: str = ""

    @property
    def ts_event(self) -> int:
        return self.ts_src_ns or self.ts_recv_ns

    @property
    def ts_init(self) -> int:
        return self.ts_recv_ns

    @property
    def mid(self) -> float | None:
        if self.bid and self.ask:
            return (self.bid + self.ask) / 2.0
        return None


OnUpdate = Callable[[RefUpdate], None]


# --------------------------------------------------------------------------------- parsing


def _num(value: object) -> float | None:
    """Futu sends numbers as JSON numbers, but tolerate strings and nulls."""
    if value is None or value == "":
        return None
    try:
        out = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return out


def _ms_to_ns(value: object) -> int:
    """A ``*_ms`` field (Unix milliseconds, per quote/push/data-format) to ns.

    Integer milliseconds are converted with integer maths: a float epoch in ms has
    only ~microsecond resolution left, which would smear the millisecond stamps
    the whole lead-lag measurement depends on.
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        ms = value
    elif isinstance(value, float):
        ms = int(round(value))
    else:
        try:
            ms = int(str(value).strip())
        except (TypeError, ValueError):
            number = _num(value)
            if number is None:
                return 0
            ms = int(round(number))
    return ms * 1_000_000 if ms > 0 else 0


def updates_from_message(message: object, ts_recv_ns: int) -> list[RefUpdate]:
    """Decoded push message -> zero or more ``RefUpdate``. Pure, hence testable.

    The live envelope is ``{"timestamp": <server ms>, "type": ..., "symbol": ...,
    "data": {...}}``. Dispatch is on ``type``; unknown types and market-level
    messages without a ``symbol`` (MARKET_STATE) yield nothing.
    """
    if not isinstance(message, dict):
        return []
    msg_type = str(message.get("type") or "").upper()
    code = message.get("symbol")
    data = message.get("data")
    if not code or not isinstance(data, dict):
        return []
    ts_srv_ns = _ms_to_ns(message.get("timestamp"))

    if msg_type == "QUOTE":
        # pre_market / after_market / overnight sub-objects are deliberately ignored:
        # the regular-session last price is the reference we compare perps against.
        last = _num(data.get("last_price"))
        if last is None:
            return []
        return [RefUpdate(
            code=str(code),
            kind=KIND_QUOTE,
            ts_src_ns=_ms_to_ns(data.get("data_time_ms")),
            ts_recv_ns=ts_recv_ns,
            last=last,
            ts_srv_ns=ts_srv_ns,
        )]

    if msg_type == "ORDER_BOOK":
        # One frame is one exchange's whole book (NSDQ and ARCA alternate); the
        # exchange is the top level's mpid. A deeper level occasionally carries a
        # different mpid, which is why only level 0 names the frame.
        bids = data.get("bid_list") or []
        asks = data.get("ask_list") or []
        best_bid = bids[0] if isinstance(bids, list) and bids else {}
        best_ask = asks[0] if isinstance(asks, list) and asks else {}
        if not isinstance(best_bid, dict):
            best_bid = {}
        if not isinstance(best_ask, dict):
            best_ask = {}
        bid = _num(best_bid.get("price"))
        ask = _num(best_ask.get("price"))
        if bid is None and ask is None:
            return []
        venue = str(best_bid.get("mpid") or best_ask.get("mpid") or "")
        return [RefUpdate(
            code=str(code),
            kind=KIND_BOOK,
            ts_src_ns=_ms_to_ns(data.get("data_time_ms")),
            ts_recv_ns=ts_recv_ns,
            bid=bid,
            ask=ask,
            bid_size=_num(best_bid.get("volume")),
            ask_size=_num(best_ask.get("volume")),
            ts_srv_ns=ts_srv_ns,
            venue=venue,
        )]

    if msg_type == "TICKER":
        # One TICKER message may carry several prints; keep them all, in order.
        # sequence is a 19-digit integer, so it stays an int, never a float.
        out: list[RefUpdate] = []
        for entry in data.get("ticker_list") or []:
            if not isinstance(entry, dict):
                continue
            price = _num(entry.get("price"))
            if price is None:
                continue
            seq = entry.get("sequence")
            out.append(RefUpdate(
                code=str(code),
                kind=KIND_TICKER,
                ts_src_ns=_ms_to_ns(entry.get("time_ms")),
                ts_recv_ns=ts_recv_ns,
                last=price,
                seq=int(seq) if isinstance(seq, (int, float)) else None,
                ts_srv_ns=ts_srv_ns,
            ))
        return out

    return []  # KLINE / BROKER_QUEUE / MARKET_STATE / anything new: not a reference price


def read_subscribe_ack(message: object, request_id: str | None = None,
                       ) -> tuple[bool | None, object, str]:
    """Subscribe / unsubscribe response -> (ok, code, message).

    The live shapes, seen on 2026-09-08:

    * success ``{"ret_code":0,"ret_msg":"success","data":{"code":0,"id":"<req>","message":""}}``
    * failure ``{"ret_code":-9,"ret_msg":"realtime quote permission required",
      "error":{"code":"permission_denied","message":"..."}}`` - note it carries no
      ``data.id``, so it is attributed to the request in flight.

    ``ok`` is None when the frame is not an ack at all (i.e. it is a push).
    """
    if not isinstance(message, dict) or "ret_code" not in message:
        return None, None, ""
    data = message.get("data") if isinstance(message.get("data"), dict) else {}
    ack_id = data.get("id") or message.get("id")
    if ack_id and request_id and ack_id != request_id:
        return None, None, ""  # an ack for some other request
    error = message.get("error") if isinstance(message.get("error"), dict) else {}
    ret_code = message.get("ret_code")
    inner = data.get("code")
    ok = ret_code == 0 and inner in (0, None)
    text = str(
        error.get("message") or message.get("ret_msg") or data.get("message") or "",
    )
    code = ret_code if ret_code != 0 else (inner if inner not in (0, None) else 0)
    return ok, code, text


# --------------------------------------------------------------------------------- signing


def ws_auth_message(timestamp_ms: int, nonce: str) -> bytes:
    """The exact bytes signed for the WebSocket auth/refresh frame.

    ``{timestamp_ms}\\n{nonce}\\nWEBSOCKET\\nws/auth`` - quote/push/auth. The REST
    signature uses a different original string and must not be reused here.
    """
    return f"{timestamp_ms}\n{nonce}\n{WS_SIGN_METHOD}\n{WS_SIGN_PATH}".encode()


def load_private_key(material: str):
    """Parse ``FUTU_PRIVATE_KEY`` into a cryptography private key object.

    Accepted, in order: a PEM block (Ed25519 or RSA, optionally with literal
    ``\\n`` escapes instead of real newlines), a base64 DER PKCS#8 blob, or a bare
    base64 32-byte Ed25519 seed. Nothing about the key is ever logged.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import load_der_private_key, load_pem_private_key

    text = (material or "").strip()
    if not text:
        raise RefFeedError("FUTU_PRIVATE_KEY is empty")
    if "\\n" in text and "-----BEGIN" in text:
        text = text.replace("\\n", "\n")  # .env files usually cannot hold real newlines
    if "-----BEGIN" in text:
        try:
            return load_pem_private_key(text.encode(), password=None)
        except Exception as exc:  # noqa: BLE001 - message must not echo the key
            raise RefFeedError(f"FUTU_PRIVATE_KEY looks like PEM but failed to parse: {exc}") from exc

    compact = "".join(text.split())
    try:
        raw = base64.b64decode(compact, validate=True)
    except Exception as exc:  # noqa: BLE001
        raise RefFeedError(
            "FUTU_PRIVATE_KEY is neither a PEM block nor valid base64",
        ) from exc
    if len(raw) == 32:
        return Ed25519PrivateKey.from_private_bytes(raw)  # raw Ed25519 seed
    try:
        return load_der_private_key(raw, password=None)  # base64 of a PKCS#8 DER
    except Exception as exc:  # noqa: BLE001
        raise RefFeedError(
            f"FUTU_PRIVATE_KEY decoded to {len(raw)} bytes, which is neither a raw "
            f"32-byte Ed25519 seed nor a DER private key",
        ) from exc


def sign_ws_auth(private_key, timestamp_ms: int, nonce: str) -> str:
    """Sign the auth original string; base64 of the raw signature, per the doc.

    Ed25519 signs the message directly; RSA uses PKCS#1 v1.5 over SHA256
    (getting-started, "算法使用").
    """
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.asymmetric.rsa import RSAPrivateKey

    payload = ws_auth_message(timestamp_ms, nonce)
    if isinstance(private_key, Ed25519PrivateKey):
        signature = private_key.sign(payload)
    elif isinstance(private_key, RSAPrivateKey):
        signature = private_key.sign(payload, padding.PKCS1v15(), hashes.SHA256())
    else:
        raise RefFeedError(
            f"unsupported private key type {type(private_key).__name__}; "
            f"Futu accepts Ed25519 or RSA-SHA256",
        )
    return base64.b64encode(signature).decode()


def new_nonce() -> str:
    """Anti-replay nonce: letters, digits, '_' and '-', 1-64 chars (getting-started)."""
    return secrets.token_hex(16)


# --------------------------------------------------------------------------------- feeds


class FakeFeed:
    """A random walk on a background thread. No network, no credentials.

    Emits alternating ``book`` and ``ticker`` updates so the whole publish ->
    subscribe -> csv path can be exercised on a machine with no Futu access.
    """

    def __init__(
        self,
        codes: Sequence[str],
        interval_ms: int = 200,
        start_price: float = 100.0,
        spread_bps: float = 2.0,
        vol_bps: float = 3.0,
        seed: int | None = None,
    ) -> None:
        if not codes:
            raise RefFeedError("FakeFeed needs at least one code")
        self.codes = list(codes)
        self.interval_ms = max(1, int(interval_ms))
        self.spread_bps = spread_bps
        self.vol_bps = vol_bps
        self._price = {code: float(start_price) for code in self.codes}
        self._rng = random.Random(seed)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.updates = 0

    def start(self, on_update: OnUpdate) -> None:
        if self._thread is not None:
            raise RefFeedError("FakeFeed already started")
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, args=(on_update,), name="fake-ref-feed", daemon=True,
        )
        self._thread.start()

    def _run(self, on_update: OnUpdate) -> None:
        toggle = 0
        while not self._stop.is_set():
            now_ns = time.time_ns()
            for code in self.codes:
                price = self._price[code] * (1.0 + self._rng.gauss(0.0, self.vol_bps / 1e4))
                self._price[code] = price
                half = price * self.spread_bps / 2e4
                if toggle % 2 == 0:
                    # A named exchange and a server stamp, so the FAKE path exercises
                    # the same per-venue books and hop column as the real feed.
                    update = RefUpdate(
                        code=code, kind=KIND_BOOK,
                        ts_src_ns=now_ns, ts_recv_ns=now_ns,
                        bid=price - half, ask=price + half,
                        bid_size=100.0, ask_size=100.0,
                        ts_srv_ns=now_ns, venue="FAKE",
                    )
                else:
                    update = RefUpdate(
                        code=code, kind=KIND_TICKER,
                        ts_src_ns=now_ns, ts_recv_ns=now_ns,
                        last=price, seq=self.updates,
                        ts_srv_ns=now_ns,
                    )
                self.updates += 1
                on_update(update)
            toggle += 1
            self._stop.wait(self.interval_ms / 1000.0)

    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=3.0)


class FutuFeed:
    """FUTUNN OPEN API quote WebSocket, run on its own asyncio loop in a thread.

    Flow, per the docs listed in the module header: connect -> ``auth`` frame ->
    wait for the ``session_id`` response -> ``subscribe`` frame -> consume pushes,
    re-sending a ``refresh`` frame every 5 minutes. Keep-alive is the protocol-level
    ping/pong of the ``websockets`` library; there is deliberately no business-level
    heartbeat frame. On any drop we reconnect with backoff and re-auth + re-subscribe,
    because the server keeps no subscription state across connections.
    """

    def __init__(
        self,
        codes: Sequence[str],
        url: str | None = None,
        api_key: str | None = None,
        private_key_material: str | None = None,
        subscribe_kinds: Sequence[str] | None = None,
        log: Callable[[str], None] | None = None,
    ) -> None:
        if not codes:
            raise RefFeedError("FutuFeed needs at least one code")
        self.codes = list(codes)
        if subscribe_kinds is None:
            # QUOTE is deliberately off by default: it carries no bid/ask, its last
            # price is already in every TICKER print, and it was ~1/3 of the bytes.
            # Measured 2026-09-08 on a proxied home link: all three kinds for one
            # symbol (~90 KB/s) got the session closed with 1008 slow_consumer
            # every ~60 s; ticker-only or order_book-only ran clean.
            raw = os.environ.get("FUTU_SUB_KINDS", "")
            subscribe_kinds = (
                [k.strip() for k in raw.split(",") if k.strip()] if raw.strip()
                else list(DEFAULT_SUB_KINDS)
            )
        self.url = url or os.environ.get("FUTU_WS_URL") or FUTU_WS_URL
        self._api_key = api_key if api_key is not None else os.environ.get("FUTU_API_KEY", "")
        self._key_material = (
            private_key_material if private_key_material is not None
            else os.environ.get("FUTU_PRIVATE_KEY", "")
        )
        self.subscribe_kinds = list(subscribe_kinds)
        self._log = log or (lambda line: print(line, flush=True))
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stopping = threading.Event()
        self._private_key = None
        self.messages = 0
        self.updates = 0
        self.connects = 0
        self.dead_codes: set[str] = set()

    # ------------------------------------------------------------------ lifecycle

    def start(self, on_update: OnUpdate) -> None:
        if self._thread is not None:
            raise RefFeedError("FutuFeed already started")
        if not self._api_key:
            raise RefFeedError(
                "FUTU_API_KEY is not set: the quote WebSocket needs an AppKey id "
                "(see https://open.futunn.com/zh-cn/api/overview/getting-started)",
            )
        self._private_key = load_private_key(self._key_material)  # fails fast, never logged
        self._stopping.clear()
        self._thread = threading.Thread(
            target=self._thread_main, args=(on_update,), name="futu-ref-feed", daemon=True,
        )
        self._thread.start()

    def _thread_main(self, on_update: OnUpdate) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._run(on_update))
        except Exception as exc:  # noqa: BLE001 - surfaced through the log, thread must not die silently
            self.failed = repr(exc)
            self._log(f"[ref/futu] feed thread stopped: {exc!r}")
        finally:
            try:
                loop.close()
            finally:
                self._loop = None

    failed: str | None = None  # set by the feed thread when it dies with an error

    def alive(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    def stop(self) -> None:
        self._stopping.set()
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(lambda: None)  # wake the loop so it sees the flag
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=5.0)

    # ------------------------------------------------------------------ connection

    async def _run(self, on_update: OnUpdate) -> None:
        from websockets.asyncio.client import connect

        attempt = 0
        while not self._stopping.is_set():
            try:
                async with connect(
                    self.url,
                    open_timeout=15,
                    ping_interval=PING_INTERVAL_SECS,
                    ping_timeout=PING_INTERVAL_SECS,
                    max_queue=1024,
                ) as ws:
                    self.connects += 1
                    attempt = 0
                    await self._authenticate(ws)
                    await self._subscribe(ws)
                    await self._consume(ws, on_update)
            except RefFeedError:
                raise  # configuration / credential faults: never retry in a loop
            except Exception as exc:  # noqa: BLE001 - transport faults reconnect with backoff
                if self._stopping.is_set():
                    return
                delay = RECONNECT_BACKOFF[min(attempt, len(RECONNECT_BACKOFF) - 1)]
                attempt += 1
                self._log(f"[ref/futu] connection to {self.url} lost ({exc!r}); "
                          f"reconnecting in {delay:.0f}s")
                await asyncio.sleep(delay)

    async def _send(self, ws, frame: dict) -> None:
        await ws.send(json.dumps(frame))

    async def _recv_json(self, ws, timeout: float) -> dict:
        raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", "replace")
        message = json.loads(raw)
        if not isinstance(message, dict):
            raise RefFeedError(f"unexpected non-object frame from {self.url}")
        return message

    def _auth_frame(self, action: str) -> dict:
        timestamp_ms = int(time.time() * 1000)
        nonce = new_nonce()
        return {
            "id": f"{action}-{nonce[:8]}",
            "action": action,
            "data": {
                "auth_type": "appkey",
                "credential_id": self._api_key,
                "authorization": sign_ws_auth(self._private_key, timestamp_ms, nonce),
                "timestamp_ms": timestamp_ms,
                "nonce": nonce,
            },
        }

    async def _authenticate(self, ws) -> None:
        await self._send(ws, self._auth_frame("auth"))
        try:
            message = await self._recv_json(ws, AUTH_TIMEOUT_SECS)
        except asyncio.TimeoutError as exc:
            raise RefFeedError(
                f"Futu quote WebSocket {self.url}: no auth response within "
                f"{AUTH_TIMEOUT_SECS:.0f}s",
            ) from exc
        code = message.get("code")
        if code not in (None, 0):  # channel errors are 4xxx / 5xxx with a "msg" field
            raise RefFeedError(
                f"Futu quote WebSocket {self.url}: auth rejected, code={code} "
                f"msg={message.get('msg') or message.get('message')!r} "
                f"(see https://open.futunn.com/zh-cn/api/quote/push/error-codes)",
            )
        if not message.get("session_id"):
            raise RefFeedError(
                f"Futu quote WebSocket {self.url}: auth response carried no session_id: "
                f"{message!r}",
            )
        self._log(f"[ref/futu] authenticated on {self.url} "
                  f"(server_time={message.get('server_time')})")

    def _subscribe_frame(self, codes: Sequence[str], action: str = "subscribe") -> dict:
        frame: dict = {"id": f"{action}-{new_nonce()[:8]}", "action": action}
        for kind in self.subscribe_kinds:
            frame[kind] = list(codes)
        return frame

    async def _subscribe(self, ws) -> None:
        """Subscribe the whole batch; on a batch rejection fall back to per-code."""
        wanted = [c for c in self.codes if c not in self.dead_codes]
        if not wanted:
            raise RefFeedError("every requested code was rejected by Futu; nothing to watch")
        ok, code, message = await self._subscribe_once(ws, wanted)
        if ok:
            self._log(f"[ref/futu] subscribed {self.subscribe_kinds} for {wanted}")
            return
        self._log(f"[ref/futu] batch subscribe failed (code={code} message={message!r}); "
                  f"retrying one code at a time")
        live: list[str] = []
        for single in wanted:
            ok, code, message = await self._subscribe_once(ws, [single])
            if ok:
                live.append(single)
            else:
                self.dead_codes.add(single)
                self._log(f"[ref/futu] code {single} not subscribed (code={code} "
                          f"message={message!r}); skipping it")
        if not live:
            raise RefFeedError(
                f"Futu quote WebSocket {self.url}: no code could be subscribed; last "
                f"code={code} message={message!r} (see "
                f"https://open.futunn.com/zh-cn/api/quote/push/error-codes)",
            )

    async def _subscribe_once(self, ws, codes: Sequence[str]) -> tuple[bool, object, object]:
        frame = self._subscribe_frame(codes)
        await self._send(ws, frame)
        deadline = time.monotonic() + SUB_TIMEOUT_SECS
        while time.monotonic() < deadline:
            try:
                message = await self._recv_json(ws, max(0.1, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                break
            ok, code, text = read_subscribe_ack(message, frame["id"])
            if ok is None:
                continue  # a push arrived before the ack; it is not ours to read here
            return ok, code, text
        return False, "timeout", f"no response in {SUB_TIMEOUT_SECS:.0f}s"

    async def _consume(self, ws, on_update: OnUpdate) -> None:
        next_refresh = time.monotonic() + REFRESH_SECS
        while not self._stopping.is_set():
            timeout = max(0.5, next_refresh - time.monotonic())
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            except asyncio.TimeoutError:
                await self._send(ws, self._auth_frame("refresh"))
                next_refresh = time.monotonic() + REFRESH_SECS
                continue
            ts_recv_ns = time.time_ns()  # stamped before any parsing work
            self.messages += 1
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if isinstance(message, dict):
                # Two error surfaces: the business envelope (ret_code / ret_msg /
                # error.message) and the channel layer (4xxx / 5xxx with "msg").
                ret_code, chan_code = message.get("ret_code"), message.get("code")
                if (ret_code is not None and ret_code != 0) or \
                        (chan_code is not None and chan_code != 0):
                    _ok, code, text = read_subscribe_ack(message)
                    self._log(
                        f"[ref/futu] error frame ret_code={ret_code} code={chan_code} "
                        f"({code}): {text or message.get('msg')!r}",
                    )
                    continue
                if ret_code == 0 and "data" in message and "type" not in message:
                    continue  # a late subscribe ack, nothing to parse
            for update in updates_from_message(message, ts_recv_ns):
                self.updates += 1
                on_update(update)
            if time.monotonic() >= next_refresh:
                await self._send(ws, self._auth_frame("refresh"))
                next_refresh = time.monotonic() + REFRESH_SECS


def build_feed(kind: str, codes: Sequence[str], log: Callable[[str], None] | None = None):
    """``--reference`` value -> feed instance. ``none`` is handled by the caller."""
    key = (kind or "").upper()
    if key == "FUTU":
        return FutuFeed(codes, log=log)
    if key == "FAKE":
        return FakeFeed(codes)
    raise RefFeedError(f"unknown reference feed {kind!r}; use FUTU or FAKE")


# --------------------------------------------------------------------------------- actor

# Imported late so that the pure feed/parse helpers above stay importable (and
# testable) without a Nautilus build present.
try:
    from nautilus_trader.common import DataActor, DataActorConfig
    from nautilus_trader.model import CustomData, DataType
except ImportError:  # pragma: no cover - only hit outside the trading venv
    DataActor = object  # type: ignore[assignment,misc]
    DataActorConfig = object  # type: ignore[assignment,misc]
    CustomData = None  # type: ignore[assignment]
    DataType = None  # type: ignore[assignment]


REF_DATA_TYPE = "RefUpdate"
DRAIN_MS = 10  # how often the actor moves feed-thread updates onto the node thread


def ref_data_type(code: str):
    """The ``DataType`` a publisher writes and a strategy subscribes to, per code."""
    return DataType(REF_DATA_TYPE, metadata={"code": code})


class RefActorConfig(DataActorConfig):
    """Codes to stream and which feed to stream them from."""

    def __init__(
        self,
        *,
        codes: Sequence[str],
        feed_kind: str = "FAKE",
        drain_ms: int = DRAIN_MS,
        **_kwargs: object,
    ) -> None:
        super().__init__()  # pyo3 base: actor_id travels through __new__ kwargs
        self.codes = list(codes)
        self.feed_kind = feed_kind
        self.drain_ms = drain_ms


class RefActor(DataActor):
    """Owns the reference feed and republishes its updates on the message bus.

    Threading note (rc4): the node has no Python asyncio loop of its own - the
    runtime is Rust/tokio - and the message bus is thread-affine, so calling
    ``publish_data`` from the feed thread trips a Rust thread-id assertion. The
    feed therefore hands updates to a ``queue.Queue`` and a Nautilus clock timer
    drains it on the node's own thread. ``ts_recv_ns`` is stamped in the feed
    thread, so the queue hop costs the measurement nothing.
    """

    def __init__(self, config: RefActorConfig) -> None:
        super().__init__(config)
        import queue

        self._cfg = config
        self._queue: queue.Queue[RefUpdate] = queue.Queue(maxsize=100_000)
        # Feed-thread log lines. The Nautilus logger is as thread-affine as the
        # message bus (calling self.log from the feed thread killed the thread
        # right after a successful auth on 2026-09-08), so lines queue up here
        # and the drain timer emits them on the node thread.
        self._log_queue: queue.Queue[str] = queue.Queue(maxsize=10_000)
        self._types = {code: ref_data_type(code) for code in config.codes}
        self._feed = None
        self._feed_dead_logged = False
        self.published = 0
        self.dropped = 0

    def _log_from_feed(self, line: str) -> None:
        """Called on the feed thread: never touch the Nautilus logger from here."""
        import queue

        try:
            self._log_queue.put_nowait(line)
        except queue.Full:
            pass

    def on_start(self) -> None:
        from datetime import timedelta

        self._feed = build_feed(
            self._cfg.feed_kind, self._cfg.codes, log=self._log_from_feed,
        )
        self._feed.start(self._enqueue)
        self.clock.set_timer(
            "ref-drain",
            timedelta(milliseconds=max(1, self._cfg.drain_ms)),
            start_time=self.clock.utc_now(),
        )
        self.log.info(
            f"[ref] {self._cfg.feed_kind} feed started for {self._cfg.codes}",
        )

    def _enqueue(self, update: RefUpdate) -> None:
        """Called on the feed thread: never touch the message bus from here."""
        import queue

        try:
            self._queue.put_nowait(update)
        except queue.Full:
            self.dropped += 1

    def on_time_event(self, event) -> None:  # noqa: ANN001 - TimeEvent
        if not str(event.name).startswith("ref-drain"):
            return
        import queue

        while True:
            try:
                line = self._log_queue.get_nowait()
            except queue.Empty:
                break
            self.log.info(line)
        feed = self._feed
        if (
            feed is not None
            and not self._feed_dead_logged
            and hasattr(feed, "alive")
            and not feed.alive()
        ):
            self._feed_dead_logged = True
            self.log.error(
                f"[ref] {self._cfg.feed_kind} feed thread is no longer running "
                f"({getattr(feed, 'failed', None) or 'no error recorded'}); "
                f"no more reference updates will arrive",
            )
        while True:
            try:
                update = self._queue.get_nowait()
            except queue.Empty:
                return
            data_type = self._types.get(update.code)
            if data_type is None:
                continue
            self.publish_data(data_type, CustomData(data_type, update))
            self.published += 1

    def on_stop(self) -> None:
        try:  # stop the fast drain first: a timer that outlives the runner logs errors
            self.clock.cancel_timer("ref-drain")
        except Exception:  # noqa: BLE001 - already cancelled / never set
            pass
        feed, self._feed = self._feed, None
        if feed is not None:
            feed.stop()
        self.log.info(f"[ref] feed stopped; published={self.published} dropped={self.dropped}")


# --------------------------------------------------------------------------------- state


@dataclass
class VenueBook:
    """One exchange's touch, as of the last ORDER_BOOK frame it sent."""

    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    ts_recv_ns: int = 0


@dataclass
class RefState:
    """What a strategy keeps per reference code: the latest quote, books and print.

    Futu pushes each US exchange's book in its own ORDER_BOOK frame (NSDQ and ARCA
    alternate, with the occasional NASD), so the books are kept per exchange and
    the reference touch is the composite: the highest bid and the lowest ask among
    the exchanges that have reported inside ``BOOK_STALE_NS``.

    Guard and fallback: because each frame is a snapshot of a different instant,
    the composite crosses (bid >= ask) whenever the stock moves between two
    exchanges' frames - 19 of 161 book frames in the 2026-09-08 capture. A crossed
    touch would poison ref_mid and every edge computed from it, so when it happens
    the touch falls back to the single most recently received exchange's own book,
    which cannot cross. ``book_mode`` says which rule produced the current touch
    (``composite`` / ``freshest`` / ``last``), ``composite_crossed`` flags the
    current update, and ``crossed`` counts how many ORDER_BOOK frames crossed.
    """

    code: str
    last: float | None = None
    ts_src_ns: int = 0
    ts_recv_ns: int = 0
    ts_srv_ns: int = 0
    kind: str = "-"
    updates: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    books: dict[str, VenueBook] = field(default_factory=dict)
    # Resolved touch, recomputed once per update rather than per attribute read.
    bid: float | None = None
    ask: float | None = None
    bid_size: float | None = None
    ask_size: float | None = None
    bid_venue: str | None = None
    ask_venue: str | None = None
    book_mode: str = "-"  # composite / freshest / last
    composite_crossed: bool = False  # did this update fall back?
    crossed: int = 0  # ORDER_BOOK frames whose composite crossed

    def apply(self, update: RefUpdate) -> None:
        if update.kind == KIND_BOOK:
            self.books[update.venue] = VenueBook(
                bid=update.bid, ask=update.ask,
                bid_size=update.bid_size, ask_size=update.ask_size,
                ts_recv_ns=update.ts_recv_ns,
            )
        if update.last is not None:
            self.last = update.last
        self.ts_src_ns = update.ts_src_ns or update.ts_recv_ns
        self.ts_recv_ns = update.ts_recv_ns
        self.ts_srv_ns = update.ts_srv_ns
        self.kind = update.kind
        self.updates += 1
        self.by_kind[update.kind] = self.by_kind.get(update.kind, 0) + 1
        self._resolve_touch(count=update.kind == KIND_BOOK)

    def fresh_books(self) -> dict[str, VenueBook]:
        """Exchanges heard from within BOOK_STALE_NS of the newest update."""
        now = self.ts_recv_ns
        return {
            venue: book for venue, book in self.books.items()
            if book.ts_recv_ns and (now - book.ts_recv_ns) <= BOOK_STALE_NS
        }

    def _best(self, books: dict[str, VenueBook], side: str,
              ) -> tuple[str | None, VenueBook | None]:
        pick = max if side == "bid" else min
        candidates = [(venue, book) for venue, book in books.items() if getattr(book, side)]
        if not candidates:
            return None, None
        return pick(candidates, key=lambda item: getattr(item[1], side))

    def _set_touch(self, bid_venue, bid_book, ask_venue, ask_book, mode: str) -> None:
        self.bid_venue, self.ask_venue = bid_venue, ask_venue
        self.bid = bid_book.bid if bid_book else None
        self.bid_size = bid_book.bid_size if bid_book else None
        self.ask = ask_book.ask if ask_book else None
        self.ask_size = ask_book.ask_size if ask_book else None
        self.book_mode = mode

    def _resolve_touch(self, count: bool = True) -> None:
        """Composite first; on a crossed composite use the freshest exchange alone."""
        fresh = self.fresh_books()
        bid_venue, bid_book = self._best(fresh, "bid")
        ask_venue, ask_book = self._best(fresh, "ask")
        bid = bid_book.bid if bid_book else None
        ask = ask_book.ask if ask_book else None
        self.composite_crossed = bool(bid and ask and bid >= ask)
        if not self.composite_crossed:
            self._set_touch(bid_venue, bid_book, ask_venue, ask_book,
                            "composite" if (bid and ask) else "last")
            return
        if count:
            self.crossed += 1
        newest = max(fresh.items(), key=lambda item: item[1].ts_recv_ns)
        venue, book = newest
        self._set_touch(venue, book, venue, book,
                        "freshest" if (book.bid and book.ask) else "last")

    @property
    def mid(self) -> float | None:
        """Book mid when both sides are known, else the last print."""
        bid, ask = self.bid, self.ask
        if bid and ask:
            return (bid + ask) / 2.0
        return self.last

    @property
    def src_to_srv_ms(self) -> float | None:
        """Exchange timestamp -> Futu server timestamp, in ms (the upstream hop)."""
        if not self.ts_src_ns or not self.ts_srv_ns:
            return None
        return (self.ts_srv_ns - self.ts_src_ns) / 1e6

    def age_ms(self, now_ns: int) -> float | None:
        if not self.ts_recv_ns:
            return None
        return (now_ns - self.ts_recv_ns) / 1e6


def buy_edge_bps(ref_mid: float, ask: float, taker_fee_bps: float) -> float:
    """Perp ask is cheaper than the stock: lift it, net of one taker fee."""
    return (ref_mid - ask) / ask * 1e4 - taker_fee_bps


def sell_edge_bps(ref_mid: float, bid: float, taker_fee_bps: float) -> float:
    """Perp bid is richer than the stock: hit it, net of one taker fee."""
    return (bid - ref_mid) / bid * 1e4 - taker_fee_bps


def codes_from_env(default: Iterable[str] = ()) -> list[str]:
    """Optional ``FUTU_CODES`` override, else whatever the caller worked out."""
    raw = os.environ.get("FUTU_CODES", "")
    if raw.strip():
        return [c.strip() for c in raw.split(",") if c.strip()]
    return list(default)

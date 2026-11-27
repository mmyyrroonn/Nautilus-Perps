"""Lightweight async Python Bitget Classic v2 USDT-Futures gateway and public L1 WS feed."""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from decimal import Decimal
import hashlib
import hmac
import http.client
import json
import os
import queue
import ssl
import threading
import time
from typing import Any, Callable, Mapping
import urllib.parse

import websockets

from .executor import OrderAck, TERMINAL_ORDER_STATUSES, VenueGateway
from .signal import InstrumentSpec

BITGET_REST_BASE = "https://api.bitget.com"
BITGET_WS_PUBLIC_MAINNET = "wss://ws.bitget.com/v2/ws/public"
BITGET_WS_PRIVATE_MAINNET = "wss://ws.bitget.com/v2/ws/private"
BITGET_WS_PUBLIC_DEMO = "wss://wspap.bitget.com/v2/ws/public"
BITGET_WS_PRIVATE_DEMO = "wss://wspap.bitget.com/v2/ws/private"

BITGET_PRODUCT_TYPE = "USDT-FUTURES"
BITGET_MARGIN_COIN = "USDT"
BITGET_MARGIN_MODE = "crossed"

_STABLECOIN_FEE_CCYS = frozenset({"USD", "USDT", "USDC"})
_BITGET_NOT_FOUND_CODES = frozenset({"40768", "43001"})
_BITGET_PRIVATE_WS_CHANNELS = frozenset({"orders", "fill"})
_BITGET_TRANSIENT_HTTP_ERRORS = (
    http.client.RemoteDisconnected,
    http.client.BadStatusLine,
    http.client.CannotSendRequest,
    BrokenPipeError,
    ConnectionResetError,
    EOFError,
)
_USER_AGENT = "Mozilla/5.0 (compatible; NautilusPerpsCarry/1.0)"
_BITGET_DEFINITE_PLACE_REJECT_CODES = frozenset(
    {
        "40017",
        "400172",
        "40700",
        "40702",
        "40704",
        "40711",
        "40712",
        "40713",
        "40714",
        "40715",
        "40716",
        "40717",
        "40724",
        "40726",
        "40754",
        "40757",
        "40758",
        "40760",
        "40762",
        "40763",
        "40764",
        "40766",
        "40774",
        "40775",
        "40800",
        "40801",
        "40802",
        "40804",
        "40805",
        "40808",
        "40812",
        "40813",
        "40814",
        "40834",
        "40835",
        "43008",
        "43012",
        "43013",
        "43025",
        "45110",
        "45111",
        "45112",
    }
)


def _is_bitget_definite_place_reject(code: Any, msg: Any = "") -> bool:
    if is_bitget_uta_error(code, msg):
        return True
    code_s = str(code or "").strip()
    msg_l = str(msg or "").lower()
    if (
        not code_s
        or code_s == "40786"
        or code_s.startswith("5")
        or any(
            tok in msg_l
            for tok in (
                "timeout",
                "timed out",
                "unknown error",
                "system error",
                "system busy",
                "duplicate",
                "clientoid",
                "client oid",
                "already exists",
                "internal error",
            )
        )
    ):
        return False
    if code_s in _BITGET_DEFINITE_PLACE_REJECT_CODES:
        return True
    if code_s.startswith("4") and any(
        tok in msg_l
        for tok in (
            "post_only",
            "post only",
            "reduce_only",
            "reduce only",
            "insufficient balance",
            "insufficient margin",
            "insufficient available",
            "invalid price",
            "invalid size",
            "invalid quantity",
            "invalid symbol",
            "symbol does not exist",
            "less than the minimum",
            "minimum order",
            "exceeds the maximum",
            "price precision",
            "size precision",
            "parameter verification",
            "margin mode",
            "position mode",
            "limit_open",
            "close only",
            "only reduce",
        )
    ):
        return True
    return False


class BitgetPreflightError(RuntimeError):
    """Raised when Bitget Classic v2 account preflight checks fail."""


class BitgetTransportError(RuntimeError):
    """Sanitized Bitget REST/WS transport error."""


@dataclass(frozen=True)
class BitgetGatewayEvent:
    """Thread-safe event drained by CarryStrategy on the Nautilus main thread."""

    client_order_id: str
    fee_usd: Decimal | None = None
    fee_invalid: bool = False
    cancel_rejected: bool = False
    fee_cleared: bool = False


def bitget_instrument_id(raw_symbol: str) -> str:
    """Convert raw Bitget USDT-M symbol (e.g. 'BTCUSDT') to Carry instrument_id."""
    sym = str(raw_symbol or "").strip().upper()
    if not sym or not sym.isascii() or any(ch.isspace() or ch in "./:" for ch in sym):
        raise ValueError(f"invalid Bitget symbol: {raw_symbol!r}")
    return f"{sym}-PERP.BITGET"


def bitget_raw_symbol(instrument_id: str) -> str:
    """Extract raw Bitget symbol from '<SYMBOL>-PERP.BITGET' instrument_id."""
    iid = str(instrument_id or "").strip().upper()
    suffix = "-PERP.BITGET"
    if not iid.endswith(suffix):
        raise ValueError(f"invalid Bitget instrument_id: {instrument_id!r}")
    raw = iid[: -len(suffix)]
    if not raw or not raw.isascii() or any(ch.isspace() or ch in "./:" for ch in raw):
        raise ValueError(f"invalid Bitget instrument_id: {instrument_id!r}")
    return raw


def _format_decimal_plain(value: Decimal) -> str:
    dec = value if isinstance(value, Decimal) else Decimal(str(value))
    if dec == 0:
        return "0"
    text = format(dec.normalize(), "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def bitget_signed_get_path(path: str, params: Mapping[str, Any] | None = None) -> str:
    """Build a Bitget Classic v2 signed GET requestPath with query keys sorted ascending."""
    if not params:
        return path
    items = sorted(
        (str(k), str(v))
        for k, v in params.items()
        if v is not None and str(v) != ""
    )
    if not items:
        return path
    return f"{path}?{urllib.parse.urlencode(items, quote_via=urllib.parse.quote)}"


def bitget_rest_headers(
    *,
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    method: str,
    request_path_and_query: str,
    body_str: str = "",
    environment: str = "mainnet",
    ts_ms: int | str | None = None,
) -> dict[str, str]:
    """Generate signed Bitget Classic v2 REST headers."""
    ts = str(int(time.time() * 1000)) if ts_ms is None else str(ts_ms)
    prehash = f"{ts}{method.upper()}{request_path_and_query}{body_str}"
    sig = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    headers = {
        "ACCESS-KEY": api_key,
        "ACCESS-SIGN": sig,
        "ACCESS-TIMESTAMP": ts,
        "ACCESS-PASSPHRASE": api_passphrase,
        "locale": "en-US",
        "Content-Type": "application/json",
    }
    if environment != "mainnet":
        headers["paptrading"] = "1"
    return headers


def bitget_ws_login_payload(
    *,
    api_key: str,
    api_secret: str,
    api_passphrase: str,
    ts_sec: int | str | None = None,
) -> dict[str, Any]:
    """Generate signed Bitget v2 private WebSocket login frame."""
    ts = str(int(time.time())) if ts_sec is None else str(ts_sec)
    prehash = f"{ts}GET/user/verify"
    sig = base64.b64encode(
        hmac.new(api_secret.encode("utf-8"), prehash.encode("utf-8"), hashlib.sha256).digest()
    ).decode("ascii")
    return {
        "op": "login",
        "args": [
            {
                "apiKey": api_key,
                "passphrase": api_passphrase,
                "timestamp": ts,
                "sign": sig,
            }
        ],
    }


def is_bitget_uta_error(code: Any, msg: Any = "") -> bool:
    """Detect Bitget Unified Trading Account (v3) vs Classic v2 mismatch errors."""
    code_s = str(code or "").strip()
    if code_s == "40085":
        return True
    msg_l = str(msg or "").lower()
    if (
        "unified account" in msg_l
        or "unified trading account" in msg_l
        or "classic account api is not supported" in msg_l
        or ("uta" in msg_l and "v3" in msg_l)
    ):
        return True
    return False


def _sanitize_code(code: Any) -> str:
    raw = str(code or "").strip()
    cleaned = "".join(ch for ch in raw if ch.isalnum() or ch in ("_", "-"))
    return cleaned[:32] or "unknown"


def _parse_optional_decimal(raw: Any) -> Decimal | None:
    if raw is None or isinstance(raw, bool):
        return None
    text = str(raw).strip()
    if not text:
        return None
    try:
        dec = Decimal(text)
    except Exception:
        return None
    return dec if dec.is_finite() else None


def _map_bitget_order_status(raw_state: Any, filled_qty: Decimal) -> str:
    st = str(raw_state or "").strip().lower()
    if st in ("filled", "full_fill"):
        return "FILLED"
    if st in ("partially_filled", "partial_fill", "partial-filled"):
        return "PARTIAL"
    if st in ("canceled", "cancelled", "expired", "system_canceled"):
        return "CANCELED"
    if st in ("rejected", "failed", "reject"):
        return "REJECTED"
    if filled_qty > 0:
        return "PARTIAL"
    return "OPEN"


def parse_bitget_contracts(payload: Any) -> dict[str, tuple[InstrumentSpec, dict[str, Any]]]:
    """Parse Bitget /api/v2/mix/market/contracts into InstrumentSpec + metadata keyed by raw_symbol."""
    rows = payload.get("data", []) if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}
    out: dict[str, tuple[InstrumentSpec, dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        if str(row.get("symbolType") or "").strip().lower() != "perpetual":
            continue
        symbol_status = str(row.get("symbolStatus") or "").strip().lower()
        if symbol_status not in ("normal", "limit_open"):
            continue
        raw_sym = str(row.get("symbol") or "").strip().upper()
        if not raw_sym.endswith("USDT") or len(raw_sym) <= 4:
            continue
        try:
            size_inc = Decimal(str(row["sizeMultiplier"]))
            min_qty = Decimal(str(row["minTradeNum"]))
            min_notional = Decimal(str(row.get("minTradeUSDT") or "5"))
            price_place = int(row["pricePlace"])
            price_end_step = Decimal(str(row.get("priceEndStep") or "1"))
            price_inc = price_end_step * (Decimal("10") ** (-price_place))
            fund_interval_h = max(1, int(row.get("fundInterval") or 8))
        except Exception:
            continue
        if size_inc <= 0 or min_qty < 0 or min_notional < 0 or price_inc <= 0:
            continue
        try:
            iid = bitget_instrument_id(raw_sym)
        except ValueError:
            continue
        is_rwa = str(row.get("isRwa", "")).strip().upper() == "YES"
        spec = InstrumentSpec(
            instrument_id=iid,
            venue="BITGET",
            size_increment=size_inc,
            contract_multiplier=Decimal("1"),
            min_quantity=min_qty,
            min_notional_usd=min_notional,
            price_increment=price_inc,
            close_only=(symbol_status == "limit_open"),
        )
        meta = {
            "symbol": raw_sym[:-4],
            "raw_symbol": raw_sym,
            "instrument_id": iid,
            "is_rwa": is_rwa,
            "asset_class": "RWA" if is_rwa else "CRYPTO",
            "fund_interval_h": fund_interval_h,
            "symbol_status": symbol_status,
            "close_only": symbol_status == "limit_open",
        }
        out[raw_sym] = (spec, meta)
    return out


class _BitgetKeepAliveRestClient:
    """Thread-safe persistent HTTP(S) client with single-retry recovery on stale keep-alive sockets."""

    def __init__(self, base_url: str = BITGET_REST_BASE, *, timeout_secs: float = 10.0) -> None:
        parsed = urllib.parse.urlsplit(base_url.rstrip("/"))
        if parsed.scheme not in ("https", "http") or not parsed.netloc or not parsed.hostname:
            raise ValueError(f"invalid Bitget REST base URL: {base_url!r}")
        self._scheme = parsed.scheme
        self._host = parsed.hostname
        self._port = parsed.port or (443 if self._scheme == "https" else 80)
        self._base_path = parsed.path.rstrip("/")
        self._timeout = max(0.5, float(timeout_secs))
        self._lock = threading.Lock()
        self._conn: http.client.HTTPConnection | None = None

    def _new_conn(self) -> http.client.HTTPConnection:
        if self._scheme == "https":
            return http.client.HTTPSConnection(
                self._host,
                self._port,
                timeout=self._timeout,
                context=ssl.create_default_context(),
            )
        return http.client.HTTPConnection(self._host, self._port, timeout=self._timeout)

    def _drop_conn_locked(self) -> None:
        conn, self._conn = self._conn, None
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    def close(self) -> None:
        with self._lock:
            self._drop_conn_locked()

    def request_json(
        self,
        method: str,
        path_and_query: str,
        *,
        headers: Mapping[str, str],
        body_str: str = "",
    ) -> dict[str, Any]:
        method_up = method.upper()
        req_headers = {"User-Agent": _USER_AGENT, "Accept": "application/json", "Connection": "keep-alive"}
        req_headers.update(headers)
        req_path = f"{self._base_path}{path_and_query}"
        body_bytes = body_str.encode("utf-8") if body_str else None
        with self._lock:
            for attempt in (0, 1):
                reused = self._conn is not None
                if self._conn is None:
                    self._conn = self._new_conn()
                conn = self._conn
                try:
                    conn.request(method_up, req_path, body=body_bytes, headers=req_headers)
                    resp = conn.getresponse()
                    raw = resp.read()
                    conn_hdr = str(resp.getheader("Connection") or "").lower()
                    if conn_hdr == "close":
                        self._drop_conn_locked()
                except _BITGET_TRANSIENT_HTTP_ERRORS as exc:
                    self._drop_conn_locked()
                    if method_up == "GET" and attempt == 0 and reused:
                        continue
                    raise BitgetTransportError(f"bitget rest transport failed: {type(exc).__name__}") from None
                except (TimeoutError, OSError, ssl.SSLError, http.client.HTTPException) as exc:
                    self._drop_conn_locked()
                    raise BitgetTransportError(f"bitget rest failed: {type(exc).__name__}") from None

                try:
                    parsed = json.loads(raw)
                except Exception:
                    raise BitgetTransportError(f"bitget rest invalid json (status={resp.status})") from None
                if not isinstance(parsed, dict):
                    raise BitgetTransportError(f"bitget rest non-object response (status={resp.status})")
                return parsed
        raise BitgetTransportError("bitget rest transport exhausted")


def _drain_queue_items(q: queue.Queue[Any], max_items: int) -> list[Any]:
    out: list[Any] = []
    limit = max(1, int(max_items))
    for _ in range(limit):
        try:
            out.append(q.get_nowait())
        except queue.Empty:
            break
    return out


class BitgetPublicQuoteFeed:
    """Zero-key Bitget v2 public books1 WebSocket feed running in a dedicated daemon thread."""

    def __init__(
        self,
        *,
        environment: str = "mainnet",
        ws_url: str | None = None,
        ping_interval_secs: float = 30.0,
        recv_timeout_secs: float = 10.0,
        reconnect_backoffs: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0),
        sub_retry_backoffs: tuple[float, ...] = (5.0, 15.0, 30.0),
        quote_queue_maxsize: int = 100_000,
        log_queue_maxsize: int = 10_000,
    ) -> None:
        if environment not in ("mainnet", "testnet"):
            raise ValueError(f"unsupported environment: {environment!r}")
        self.environment = environment
        self.ws_url = ws_url or (BITGET_WS_PUBLIC_MAINNET if environment == "mainnet" else BITGET_WS_PUBLIC_DEMO)
        self.ping_interval_secs = max(0.05, float(ping_interval_secs))
        self.recv_timeout_secs = max(0.05, float(recv_timeout_secs))
        self.reconnect_backoffs = reconnect_backoffs or (1.0, 2.0, 5.0, 10.0, 30.0)
        self.sub_retry_backoffs = sub_retry_backoffs or (5.0, 15.0, 30.0)

        self._lock = threading.Lock()
        self._desired_subs: dict[str, str] = {}
        self._quote_queue: queue.Queue[tuple[str, float, float, int]] = queue.Queue(maxsize=quote_queue_maxsize)
        self._log_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=log_queue_maxsize)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._wake_evt: asyncio.Event | None = None
        self._stop_evt: asyncio.Event | None = None
        self._stop_flag = False
        self._started_ready = threading.Event()
        self._thread: threading.Thread | None = None
        self.dropped_quotes = 0

    def _enqueue_log(self, level: str, msg: str) -> None:
        try:
            self._log_queue.put_nowait((level, msg))
        except queue.Full:
            pass

    def _enqueue_quote(self, item: tuple[str, float, float, int]) -> None:
        try:
            self._quote_queue.put_nowait(item)
        except queue.Full:
            self.dropped_quotes += 1

    def drain_quotes(self, max_items: int = 1000) -> list[tuple[str, float, float, int]]:
        return _drain_queue_items(self._quote_queue, max_items)

    def drain_logs(self, max_items: int = 200) -> list[tuple[str, str]]:
        return _drain_queue_items(self._log_queue, max_items)

    def alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def _wake_async(self) -> None:
        if self._wake_evt is not None:
            self._wake_evt.set()

    def _stop_async(self) -> None:
        if self._stop_evt is not None:
            self._stop_evt.set()
        if self._wake_evt is not None:
            self._wake_evt.set()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_flag = False
            self._started_ready.clear()
            t = threading.Thread(target=self._run_thread, name="bitget-public-ws", daemon=True)
            self._thread = t
            t.start()
        self._started_ready.wait(timeout=2.0)

    def stop(self, timeout: float = 5.0) -> None:
        with self._lock:
            self._stop_flag = True
            loop = self._loop
            t = self._thread
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._stop_async)
            except RuntimeError:
                pass
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)
        with self._lock:
            if self._thread is t and (t is None or not t.is_alive()):
                self._thread = None

    def subscribe(self, instrument_id: str) -> None:
        raw_sym = bitget_raw_symbol(instrument_id)
        iid = bitget_instrument_id(raw_sym)
        with self._lock:
            if self._desired_subs.get(iid) == raw_sym:
                return
            self._desired_subs[iid] = raw_sym
            loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake_async)
            except RuntimeError:
                pass

    def unsubscribe(self, instrument_id: str) -> None:
        raw_sym = bitget_raw_symbol(instrument_id)
        iid = bitget_instrument_id(raw_sym)
        with self._lock:
            if iid not in self._desired_subs:
                return
            self._desired_subs.pop(iid, None)
            loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake_async)
            except RuntimeError:
                pass

    def _run_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main_loop(loop))
        except Exception as exc:
            self._enqueue_log("ERROR", f"[bitget.ws.pub] fatal loop error: {type(exc).__name__}")
        finally:
            with self._lock:
                self._loop = None
                self._wake_evt = None
                self._stop_evt = None
            try:
                loop.close()
            except Exception:
                pass

    async def _send_sub_op(self, ws: Any, op: str, symbols: list[str]) -> None:
        if not symbols:
            return
        for i in range(0, len(symbols), 50):
            chunk = symbols[i : i + 50]
            payload = {
                "op": op,
                "args": [
                    {"instType": BITGET_PRODUCT_TYPE, "channel": "books1", "instId": sym}
                    for sym in chunk
                ],
            }
            await ws.send(json.dumps(payload, separators=(",", ":")))

    def _handle_public_message(
        self,
        raw_msg: str | bytes,
        *,
        confirmed_subs: set[str] | None = None,
        pending_subs: dict[str, tuple[float, int]] | None = None,
    ) -> bool:
        text = raw_msg.decode("utf-8", errors="replace") if isinstance(raw_msg, bytes) else raw_msg
        if text == "pong":
            return True
        try:
            msg = json.loads(text)
        except Exception:
            return False
        if not isinstance(msg, dict):
            return False
        event = msg.get("event")
        if event == "subscribe":
            arg = msg.get("arg")
            if isinstance(arg, dict) and arg.get("channel") == "books1":
                inst_id = str(arg.get("instId") or "").strip().upper()
                if inst_id:
                    if confirmed_subs is not None:
                        confirmed_subs.add(inst_id)
                    if pending_subs is not None:
                        pending_subs.pop(inst_id, None)
            return False
        if event == "error":
            code = _sanitize_code(msg.get("code"))
            self._enqueue_log("WARNING", f"[bitget.ws.pub] subscribe/ws error code={code}")
            arg = msg.get("arg")
            if isinstance(arg, dict):
                inst_id = str(arg.get("instId") or "").strip().upper()
                if inst_id:
                    if confirmed_subs is not None:
                        confirmed_subs.discard(inst_id)
                    if pending_subs is not None and inst_id not in pending_subs:
                        pending_subs[inst_id] = (time.monotonic() + self.sub_retry_backoffs[0], 1)
            return False
        arg = msg.get("arg")
        data = msg.get("data")
        if not isinstance(arg, dict) or arg.get("channel") != "books1" or not isinstance(data, list):
            return False
        inst_id = str(arg.get("instId") or "").strip().upper()
        if not inst_id:
            return False
        try:
            iid = bitget_instrument_id(inst_id)
        except ValueError:
            return False
        if confirmed_subs is not None:
            confirmed_subs.add(inst_id)
        if pending_subs is not None:
            pending_subs.pop(inst_id, None)
        msg_ts = msg.get("ts")
        for row in data:
            if not isinstance(row, dict):
                continue
            bids = row.get("bids")
            asks = row.get("asks")
            if not isinstance(bids, list) or not isinstance(asks, list) or not bids or not asks:
                continue
            try:
                bid = float(bids[0][0])
                ask = float(asks[0][0])
            except Exception:
                continue
            if bid <= 0.0 or ask < bid:
                continue
            raw_ts = row.get("ts")
            if raw_ts in (None, ""):
                raw_ts = msg_ts
            if raw_ts in (None, "") or isinstance(raw_ts, bool):
                continue
            try:
                int_ts = int(str(raw_ts).strip())
            except (TypeError, ValueError):
                continue
            if int_ts <= 0:
                continue
            ts_ns = int_ts * 1_000_000
            self._enqueue_quote((iid, bid, ask, ts_ns))
        return False

    async def _main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        wake_evt = asyncio.Event()
        stop_evt = asyncio.Event()
        with self._lock:
            self._loop = loop
            self._wake_evt = wake_evt
            self._stop_evt = stop_evt
            if self._stop_flag:
                stop_evt.set()
        self._started_ready.set()

        attempt = 0
        while not stop_evt.is_set():
            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=None,
                    ping_timeout=None,
                    open_timeout=10.0,
                    close_timeout=3.0,
                ) as ws:
                    attempt = 0
                    self._enqueue_log("INFO", f"[bitget.ws.pub] connected ({self.environment})")
                    active_subs: dict[str, str] = {}
                    confirmed_subs: set[str] = set()
                    pending_subs: dict[str, tuple[float, int]] = {}
                    with self._lock:
                        desired = dict(self._desired_subs)
                    if desired:
                        init_syms = sorted(desired.values())
                        await self._send_sub_op(ws, "subscribe", init_syms)
                        now_sub = time.monotonic()
                        for sym in init_syms:
                            confirmed_subs.discard(sym)
                            pending_subs[sym] = (now_sub + self.sub_retry_backoffs[0], 1)
                        active_subs = dict(desired)

                    last_ping_sent = time.monotonic()
                    awaiting_pong = False
                    pong_deadline = 0.0
                    recv_task: asyncio.Task[Any] | None = None
                    wake_task: asyncio.Task[Any] | None = None
                    stop_task: asyncio.Task[Any] = asyncio.create_task(stop_evt.wait())
                    try:
                        while not stop_evt.is_set():
                            now = time.monotonic()
                            active_syms = set(active_subs.values())
                            for sym in list(pending_subs):
                                if sym not in active_syms or sym in confirmed_subs:
                                    pending_subs.pop(sym, None)
                            due_subs = sorted(sym for sym, (dl, _att) in pending_subs.items() if now >= dl)
                            if due_subs:
                                await self._send_sub_op(ws, "subscribe", due_subs)
                                for sym in due_subs:
                                    _dl, prev_att = pending_subs[sym]
                                    backoff = self.sub_retry_backoffs[min(prev_att, len(self.sub_retry_backoffs) - 1)]
                                    pending_subs[sym] = (now + backoff, prev_att + 1)
                                self._enqueue_log(
                                    "WARNING",
                                    f"[bitget.ws.pub] retried unconfirmed books1 subscriptions count={len(due_subs)}",
                                )

                            if not awaiting_pong and (now - last_ping_sent) >= self.ping_interval_secs:
                                await ws.send("ping")
                                last_ping_sent = now
                                awaiting_pong = True
                                pong_deadline = now + self.recv_timeout_secs
                            next_ping_in = max(0.01, (last_ping_sent + self.ping_interval_secs) - time.monotonic())
                            wait_timeout = (
                                min(next_ping_in, max(0.01, pong_deadline - time.monotonic()))
                                if awaiting_pong
                                else next_ping_in
                            )
                            if pending_subs:
                                next_sub_due = min(dl for dl, _ in pending_subs.values())
                                wait_timeout = min(wait_timeout, max(0.01, next_sub_due - time.monotonic()))

                            if recv_task is None:
                                recv_task = asyncio.create_task(ws.recv())
                            if wake_task is None:
                                wake_task = asyncio.create_task(wake_evt.wait())

                            done, _ = await asyncio.wait(
                                {recv_task, wake_task, stop_task},
                                timeout=wait_timeout,
                                return_when=asyncio.FIRST_COMPLETED,
                            )

                            if stop_task in done or stop_evt.is_set():
                                break

                            if recv_task in done:
                                raw_msg = recv_task.result()
                                recv_task = None
                                if self._handle_public_message(
                                    raw_msg,
                                    confirmed_subs=confirmed_subs,
                                    pending_subs=pending_subs,
                                ):
                                    awaiting_pong = False

                            if wake_task in done:
                                wake_evt.clear()
                                wake_task = None
                                with self._lock:
                                    desired = dict(self._desired_subs)
                                to_add = [desired[iid] for iid in sorted(set(desired) - set(active_subs))]
                                to_del = [active_subs[iid] for iid in sorted(set(active_subs) - set(desired))]
                                if to_add:
                                    await self._send_sub_op(ws, "subscribe", to_add)
                                    now_sub = time.monotonic()
                                    for sym in to_add:
                                        confirmed_subs.discard(sym)
                                        pending_subs[sym] = (now_sub + self.sub_retry_backoffs[0], 1)
                                if to_del:
                                    await self._send_sub_op(ws, "unsubscribe", to_del)
                                    for sym in to_del:
                                        confirmed_subs.discard(sym)
                                        pending_subs.pop(sym, None)
                                active_subs = desired

                            if awaiting_pong and time.monotonic() >= pong_deadline:
                                raise TimeoutError("public ws pong timeout")
                    finally:
                        for task in (recv_task, wake_task, stop_task):
                            if task is not None and not task.done():
                                task.cancel()
                                try:
                                    await task
                                except BaseException:
                                    pass
            except asyncio.CancelledError:
                break
            except Exception as exc:
                if stop_evt.is_set():
                    break
                delay = self.reconnect_backoffs[min(attempt, len(self.reconnect_backoffs) - 1)]
                attempt += 1
                self._enqueue_log(
                    "WARNING",
                    f"[bitget.ws.pub] disconnected ({type(exc).__name__}); reconnecting in {delay:.1f}s",
                )
                try:
                    await asyncio.wait_for(stop_evt.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass


@dataclass
class _TrackedOrder:
    client_order_id: str
    instrument_id: str
    raw_symbol: str
    side: str
    order_type: str
    qty_contracts: Decimal
    price: Decimal | None
    post_only: bool
    ioc: bool
    reduce_only: bool
    venue_order_id: str = ""
    status: str = "OPEN"
    filled_base_qty: Decimal = Decimal("0")
    avg_fill_price: Decimal | None = None
    reason: str | None = None
    fee_usd: Decimal | None = None
    fee_invalid: bool = False
    order_level_price_qty: Decimal = Decimal("0")
    order_level_fee_usd: Decimal | None = None
    order_level_fee_qty: Decimal = Decimal("0")
    fee_backfill_attempts: int = 0
    last_fee_backfill_ts: float = 0.0
    seen_trade_ids: set[str] = field(default_factory=set)
    fee_by_trade_id: dict[str, Decimal] = field(default_factory=dict)
    filled_by_trade_id: dict[str, Decimal] = field(default_factory=dict)
    fill_notional_by_trade_id: dict[str, Decimal] = field(default_factory=dict)
    cancel_requested: bool = False
    cancel_in_place_handled: bool = False
    submit_uncertain: bool = False
    not_found_confirmations: int = 0
    uncertain_error_checks: int = 0
    terminal_ts: float | None = None
    last_resync_ts: float = 0.0

    def to_ack(self) -> OrderAck:
        return OrderAck(
            client_order_id=self.client_order_id,
            venue_order_id=self.venue_order_id,
            status=self.status,
            filled_base_qty=self.filled_base_qty,
            avg_fill_price=self.avg_fill_price,
            reason=self.reason,
            fee_usd=None if self.fee_invalid else self.fee_usd,
        )


class BitgetVenueGateway:
    """Non-blocking async Python Classic v2 Bitget execution gateway implementing VenueGateway."""

    def __init__(
        self,
        *,
        environment: str = "mainnet",
        environ: Mapping[str, str] | None = None,
        rest_base_url: str = BITGET_REST_BASE,
        ws_url: str | None = None,
        rest_request: Callable[[str, str, dict[str, Any] | None], dict[str, Any]] | None = None,
        enable_ws: bool | None = None,
        ping_interval_secs: float = 30.0,
        recv_timeout_secs: float = 10.0,
        resync_interval_secs: float = 5.0,
        http_timeout_secs: float = 10.0,
        reconnect_backoffs: tuple[float, ...] = (1.0, 2.0, 5.0, 10.0, 30.0),
        preflight_symbol: str = "BTCUSDT",
        event_queue_maxsize: int = 10_000,
        log_queue_maxsize: int = 10_000,
        shutdown_drain_timeout_secs: float = 3.0,
        uncertain_error_max_checks: int = 6,
        fee_backfill_max_attempts: int = 3,
        terminal_retention_secs: float = 3600.0,
        terminal_prune_threshold: int = 500,
    ) -> None:
        if environment not in ("mainnet", "testnet"):
            raise ValueError(f"unsupported environment: {environment!r}")
        self.environment = environment
        self._environ = environ
        self.ws_url = ws_url or (BITGET_WS_PRIVATE_MAINNET if environment == "mainnet" else BITGET_WS_PRIVATE_DEMO)
        self._rest_request_override = rest_request
        self._rest_client = None if rest_request is not None else _BitgetKeepAliveRestClient(
            rest_base_url, timeout_secs=http_timeout_secs
        )
        self._enable_ws = (ws_url is not None or rest_request is None) if enable_ws is None else bool(enable_ws)
        self.ping_interval_secs = max(0.05, float(ping_interval_secs))
        self.recv_timeout_secs = max(0.05, float(recv_timeout_secs))
        self.resync_interval_secs = max(0.05, float(resync_interval_secs))
        self.reconnect_backoffs = reconnect_backoffs or (1.0, 2.0, 5.0, 10.0, 30.0)
        self._preflight_symbol = preflight_symbol
        self.shutdown_drain_timeout_secs = max(0.05, float(shutdown_drain_timeout_secs))
        self.uncertain_error_max_checks = max(1, int(uncertain_error_max_checks))
        self.fee_backfill_max_attempts = max(1, int(fee_backfill_max_attempts))
        self.terminal_retention_secs = max(0.0, float(terminal_retention_secs))
        self.terminal_prune_threshold = max(1, int(terminal_prune_threshold))

        self._orders_lock = threading.Lock()
        self._orders: dict[str, _TrackedOrder] = {}
        self._active_coids: set[str] = set()
        self._vid_to_coid: dict[str, str] = {}
        self._pending_cancel_rejects: set[str] = set()

        self._cmd_queue: queue.Queue[tuple[str, str]] = queue.Queue()
        self._backfill_queue: queue.Queue[str] = queue.Queue()
        self._event_queue: queue.Queue[BitgetGatewayEvent] = queue.Queue(maxsize=event_queue_maxsize)
        self._log_queue: queue.Queue[tuple[str, str]] = queue.Queue(maxsize=log_queue_maxsize)

        self._loop_lock = threading.Lock()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._cmd_wake_evt: asyncio.Event | None = None
        self._resync_wake_evt: asyncio.Event | None = None
        self._stop_evt: asyncio.Event | None = None
        self._stop_flag = False
        self._started_ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._ws_logged_in = False
        self._ws_subs_ready = False
        self._force_resync_pending = False
        self._order_async_locks: dict[str, asyncio.Lock] = {}

    def _env_map(self) -> Mapping[str, str]:
        return os.environ if self._environ is None else self._environ

    def _credential_keys(self) -> tuple[str, str, str]:
        pfx = "BITGET_" if self.environment == "mainnet" else "BITGET_DEMO_"
        return (pfx + "API_KEY", pfx + "API_SECRET", pfx + "API_PASSPHRASE")

    def _require_creds(self) -> dict[str, str]:
        env = self._env_map()
        keys = self._credential_keys()
        out: dict[str, str] = {}
        missing: list[str] = []
        for k in keys:
            val = env.get(k)
            if not isinstance(val, str) or not val.strip():
                missing.append(k)
            else:
                out[k] = val.strip()
        if missing:
            raise BitgetPreflightError(f"BITGET credentials missing from environment: {', '.join(missing)}")
        return out

    def _optional_creds(self) -> tuple[str, str, str]:
        env = self._env_map()
        k1, k2, k3 = self._credential_keys()
        return (
            str(env.get(k1) or "").strip(),
            str(env.get(k2) or "").strip(),
            str(env.get(k3) or "").strip(),
        )

    def _enqueue_log(self, level: str, msg: str) -> None:
        try:
            self._log_queue.put_nowait((level, msg))
        except queue.Full:
            pass

    def _enqueue_event(self, evt: BitgetGatewayEvent) -> None:
        if evt.cancel_rejected and evt.client_order_id:
            with self._orders_lock:
                self._pending_cancel_rejects.add(evt.client_order_id)
        try:
            self._event_queue.put_nowait(evt)
        except queue.Full:
            pass

    def drain_events(self, max_items: int = 1000) -> list[BitgetGatewayEvent]:
        limit = max(1, int(max_items))
        with self._orders_lock:
            pending_coids = sorted(self._pending_cancel_rejects)
            emit_coids = pending_coids[:limit]
            self._pending_cancel_rejects.difference_update(emit_coids)
        out: list[BitgetGatewayEvent] = [
            BitgetGatewayEvent(client_order_id=coid, cancel_rejected=True)
            for coid in emit_coids
        ]
        emitted_cancel_rej = set(emit_coids)
        rem = limit - len(out)
        if rem > 0:
            for evt in _drain_queue_items(self._event_queue, rem):
                if evt.cancel_rejected and evt.client_order_id in emitted_cancel_rej:
                    if evt.fee_usd is None and not evt.fee_invalid and not evt.fee_cleared:
                        continue
                    evt = BitgetGatewayEvent(
                        client_order_id=evt.client_order_id,
                        fee_usd=evt.fee_usd,
                        fee_invalid=evt.fee_invalid,
                        cancel_rejected=False,
                        fee_cleared=evt.fee_cleared,
                    )
                out.append(evt)
        return out

    def drain_logs(self, max_items: int = 200) -> list[tuple[str, str]]:
        return _drain_queue_items(self._log_queue, max_items)

    def alive(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def _wake_cmd_async(self) -> None:
        if self._cmd_wake_evt is not None:
            self._cmd_wake_evt.set()

    def _wake_resync_async(self) -> None:
        if self._resync_wake_evt is not None:
            self._resync_wake_evt.set()

    def _stop_async(self) -> None:
        if self._stop_evt is not None:
            self._stop_evt.set()
        if self._cmd_wake_evt is not None:
            self._cmd_wake_evt.set()
        if self._resync_wake_evt is not None:
            self._resync_wake_evt.set()

    def start(self) -> None:
        with self._loop_lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stop_flag = False
            self._started_ready.clear()
            t = threading.Thread(target=self._run_thread, name="bitget-private-gw", daemon=True)
            self._thread = t
            t.start()
        self._started_ready.wait(timeout=2.0)

    def stop(self, timeout: float = 5.0) -> None:
        with self._loop_lock:
            self._stop_flag = True
            loop = self._loop
            t = self._thread
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._stop_async)
            except RuntimeError:
                pass
        if t is not None and t is not threading.current_thread():
            t.join(timeout=timeout)
        with self._loop_lock:
            if self._thread is t and (t is None or not t.is_alive()):
                self._thread = None
        if self._rest_client is not None and (t is None or not t.is_alive()):
            self._rest_client.close()

    def _signed_rest(
        self,
        method: str,
        path_and_query: str,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._rest_request_override is not None:
            try:
                resp = self._rest_request_override(method.upper(), path_and_query, body)
            except BitgetTransportError:
                raise
            except Exception as exc:
                raise BitgetTransportError(f"bitget rest error: {type(exc).__name__}") from None
            if not isinstance(resp, dict):
                raise BitgetTransportError("bitget rest invalid response type")
            return resp

        creds = self._require_creds()
        k1, k2, k3 = self._credential_keys()
        body_str = json.dumps(body, separators=(",", ":")) if body is not None else ""
        headers = bitget_rest_headers(
            api_key=creds[k1],
            api_secret=creds[k2],
            api_passphrase=creds[k3],
            method=method.upper(),
            request_path_and_query=path_and_query,
            body_str=body_str,
            environment=self.environment,
        )
        assert self._rest_client is not None
        return self._rest_client.request_json(
            method.upper(),
            path_and_query,
            headers=headers,
            body_str=body_str,
        )

    def preflight_check(self) -> None:
        """Synchronously verify Classic v2 USDT-FUTURES credentials, non-UTA, one_way_mode, and crossed."""
        self._require_creds()
        path = bitget_signed_get_path(
            "/api/v2/mix/account/account",
            {
                "symbol": self._preflight_symbol,
                "productType": BITGET_PRODUCT_TYPE,
                "marginCoin": BITGET_MARGIN_COIN,
            },
        )
        try:
            resp = self._signed_rest("GET", path, None)
        except BitgetPreflightError:
            raise
        except Exception as exc:
            raise BitgetPreflightError(f"Bitget preflight request failed: {type(exc).__name__}") from None

        code = str(resp.get("code") or "")
        msg = resp.get("msg")
        if is_bitget_uta_error(code, msg):
            raise BitgetPreflightError(
                f"Bitget UTA account not supported (code={_sanitize_code(code)}); Classic v2 account required"
            )
        if code != "00000":
            raise BitgetPreflightError(f"Bitget preflight failed (code={_sanitize_code(code)})")

        if "data" not in resp:
            raise BitgetPreflightError("Bitget preflight missing account data")
        data = resp.get("data")
        if isinstance(data, list):
            if not data or not isinstance(data[0], dict):
                raise BitgetPreflightError("Bitget preflight returned invalid account payload")
            data = data[0]
        if not isinstance(data, dict):
            raise BitgetPreflightError("Bitget preflight returned invalid account payload")

        pos_mode = str(data.get("posMode") or "").strip().lower()
        if pos_mode != "one_way_mode":
            raise BitgetPreflightError(
                f"Bitget posMode must be one_way_mode, got {pos_mode or 'unknown'}"
            )
        margin_mode = str(data.get("marginMode") or "").strip().lower()
        if margin_mode != BITGET_MARGIN_MODE:
            raise BitgetPreflightError(
                f"Bitget marginMode must be crossed, got {margin_mode or 'missing'}"
            )

        iso_path = bitget_signed_get_path(
            "/api/v2/mix/account/isolated-symbols",
            {"productType": BITGET_PRODUCT_TYPE},
        )
        try:
            iso_resp = self._signed_rest("GET", iso_path, None)
        except BitgetPreflightError:
            raise
        except Exception as exc:
            raise BitgetPreflightError(f"Bitget isolated-symbols request failed: {type(exc).__name__}") from None

        iso_code = str(iso_resp.get("code") or "")
        if is_bitget_uta_error(iso_code, iso_resp.get("msg")):
            raise BitgetPreflightError(
                f"Bitget UTA account not supported (code={_sanitize_code(iso_code)}); Classic v2 account required"
            )
        if iso_code != "00000":
            raise BitgetPreflightError(f"Bitget isolated-symbols failed (code={_sanitize_code(iso_code)})")
        if "data" not in iso_resp or not isinstance(iso_resp.get("data"), list):
            raise BitgetPreflightError("Bitget isolated-symbols returned invalid payload")
        iso_rows = iso_resp["data"]
        if iso_rows:
            syms = [
                _sanitize_code(row.get("symbol"))
                for row in iso_rows[:5]
                if isinstance(row, dict) and row.get("symbol")
            ]
            suffix = f" ({', '.join(syms)})" if syms else ""
            raise BitgetPreflightError(
                f"Bitget account has {len(iso_rows)} isolated-margin symbols configured{suffix}; all symbols must be crossed"
            )

    def submit_limit(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        price: Decimal,
        post_only: bool,
        ioc: bool,
        reduce_only: bool,
    ) -> OrderAck:
        raw_sym = bitget_raw_symbol(instrument_id)
        rec = _TrackedOrder(
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            raw_symbol=raw_sym,
            side=side.upper(),
            order_type="LIMIT",
            qty_contracts=qty_contracts,
            price=price,
            post_only=bool(post_only),
            ioc=bool(ioc),
            reduce_only=bool(reduce_only),
            last_resync_ts=time.monotonic(),
        )
        with self._orders_lock:
            existing = self._orders.get(client_order_id)
            if existing is not None:
                return existing.to_ack()
            self._orders[client_order_id] = rec
            self._active_coids.add(client_order_id)
        self._cmd_queue.put(("place", client_order_id))
        with self._loop_lock:
            loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake_cmd_async)
            except RuntimeError:
                pass
        return rec.to_ack()

    def submit_market(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        reduce_only: bool,
    ) -> OrderAck:
        raw_sym = bitget_raw_symbol(instrument_id)
        rec = _TrackedOrder(
            client_order_id=client_order_id,
            instrument_id=instrument_id,
            raw_symbol=raw_sym,
            side=side.upper(),
            order_type="MARKET",
            qty_contracts=qty_contracts,
            price=None,
            post_only=False,
            ioc=False,
            reduce_only=bool(reduce_only),
            last_resync_ts=time.monotonic(),
        )
        with self._orders_lock:
            existing = self._orders.get(client_order_id)
            if existing is not None:
                return existing.to_ack()
            self._orders[client_order_id] = rec
            self._active_coids.add(client_order_id)
        self._cmd_queue.put(("place", client_order_id))
        with self._loop_lock:
            loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake_cmd_async)
            except RuntimeError:
                pass
        return rec.to_ack()

    def cancel_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        with self._orders_lock:
            rec = self._orders.get(client_order_id)
            if rec is None:
                return OrderAck(client_order_id, "", "CANCELED", Decimal("0"))
            ack = rec.to_ack()
            if rec.status in TERMINAL_ORDER_STATUSES:
                return ack
            rec.cancel_requested = True
            rec.cancel_in_place_handled = False
        self._cmd_queue.put(("cancel", client_order_id))
        with self._loop_lock:
            loop = self._loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(self._wake_cmd_async)
            except RuntimeError:
                pass
        return ack

    def poll_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        with self._orders_lock:
            rec = self._orders.get(client_order_id)
            if rec is None:
                return OrderAck(client_order_id, "", "OPEN", Decimal("0"))
            return rec.to_ack()

    def _run_thread(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main_loop(loop))
        except Exception as exc:
            self._enqueue_log("ERROR", f"[bitget.gw] fatal loop error: {type(exc).__name__}")
        finally:
            with self._loop_lock:
                self._loop = None
                self._cmd_wake_evt = None
                self._resync_wake_evt = None
                self._stop_evt = None
            try:
                loop.close()
            except Exception:
                pass
            if self._rest_client is not None:
                try:
                    self._rest_client.close()
                except Exception:
                    pass

    async def _main_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        cmd_wake = asyncio.Event()
        resync_wake = asyncio.Event()
        stop_evt = asyncio.Event()
        with self._loop_lock:
            self._loop = loop
            self._cmd_wake_evt = cmd_wake
            self._resync_wake_evt = resync_wake
            self._stop_evt = stop_evt
            if self._stop_flag:
                stop_evt.set()
        self._started_ready.set()

        tasks = [
            asyncio.create_task(self._command_loop(cmd_wake, stop_evt)),
            asyncio.create_task(self._resync_loop(resync_wake, stop_evt)),
        ]
        if self._enable_ws:
            tasks.append(asyncio.create_task(self._private_ws_loop(stop_evt)))
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        finally:
            for t in tasks:
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except BaseException:
                        pass

    async def _command_loop(self, cmd_wake: asyncio.Event, stop_evt: asyncio.Event) -> None:
        inflight: dict[asyncio.Task[Any], tuple[str, str]] = {}

        def _spawn_cmd(op: str, coid: str) -> None:
            task = asyncio.create_task(self._exec_cmd_serialized(op, coid))
            inflight[task] = (op, coid)
            task.add_done_callback(lambda t: inflight.pop(t, None))

        try:
            while not stop_evt.is_set():
                while True:
                    try:
                        op, coid = self._cmd_queue.get_nowait()
                    except queue.Empty:
                        break
                    _spawn_cmd(op, coid)

                if stop_evt.is_set():
                    break
                wake_t = asyncio.create_task(cmd_wake.wait())
                stop_t = asyncio.create_task(stop_evt.wait())
                await asyncio.wait({wake_t, stop_t}, return_when=asyncio.FIRST_COMPLETED)
                for t in (wake_t, stop_t):
                    if not t.done():
                        t.cancel()
                        try:
                            await t
                        except BaseException:
                            pass
                if cmd_wake.is_set():
                    cmd_wake.clear()
        finally:
            # Drain both cancel AND queued emergency risk-reducing place commands before bounded join.
            while True:
                try:
                    op, coid = self._cmd_queue.get_nowait()
                except queue.Empty:
                    break
                if op in ("cancel", "place"):
                    _spawn_cmd(op, coid)
            if inflight:
                _done, pending_t = await asyncio.wait(
                    list(inflight.keys()),
                    timeout=self.shutdown_drain_timeout_secs,
                )
                if pending_t:
                    unfinished_places = sorted(
                        coid
                        for t in pending_t
                        for op, coid in [inflight.get(t, ("", ""))]
                        if op == "place"
                    )
                    unfinished_cancels = sorted(
                        coid
                        for t in pending_t
                        for op, coid in [inflight.get(t, ("", ""))]
                        if op == "cancel"
                    )
                    if unfinished_places:
                        self._enqueue_log(
                            "ERROR",
                            f"[bitget.gw] shutdown drain timeout ({self.shutdown_drain_timeout_secs:.2f}s) with unconfirmed place orders={','.join(unfinished_places)}",
                        )
                    elif unfinished_cancels:
                        self._enqueue_log(
                            "WARNING",
                            f"[bitget.gw] shutdown drain timeout ({self.shutdown_drain_timeout_secs:.2f}s) with pending cancels={','.join(unfinished_cancels)}",
                        )
                    for t in pending_t:
                        t.cancel()

    async def _exec_cmd_serialized(self, op: str, coid: str) -> None:
        lock = self._order_async_locks.get(coid)
        if lock is None:
            lock = asyncio.Lock()
            self._order_async_locks[coid] = lock
        async with lock:
            if op == "place":
                await asyncio.to_thread(self._exec_place_sync, coid)
            elif op == "cancel":
                skip_cancel = False
                with self._orders_lock:
                    rec = self._orders.get(coid)
                    if rec is not None and rec.cancel_in_place_handled:
                        rec.cancel_in_place_handled = False
                        skip_cancel = True
                if not skip_cancel:
                    await asyncio.to_thread(self._exec_cancel_sync, coid)

    def _parse_fee_detail_list(self, fee_details: Any) -> tuple[bool, Decimal | None]:
        """Return (has_invalid_coin, normalized_nonneg_usd_sum)."""
        if not isinstance(fee_details, list) or not fee_details:
            return False, None
        total = Decimal("0")
        saw_fee = False
        for item in fee_details:
            if not isinstance(item, dict):
                continue
            coin = str(item.get("feeCoin") or "").strip().upper()
            raw_val = item.get("fee")
            if raw_val is None or str(raw_val).strip() == "":
                raw_val = item.get("totalFee")
            if coin and coin not in _STABLECOIN_FEE_CCYS:
                return True, None
            dec = _parse_optional_decimal(raw_val)
            if dec is None:
                continue
            if not coin:
                return True, None
            # Classic v2 charges fees as negative strings (e.g. "-0.15"); clamp rebates at 0.
            total += max(Decimal("0"), -dec)
            saw_fee = True
        return False, (total if saw_fee else None)

    def _trade_fee_covered_qty_locked(self, rec: _TrackedOrder) -> Decimal:
        return sum(
            (rec.filled_by_trade_id.get(tid, Decimal("0")) for tid in rec.fee_by_trade_id),
            Decimal("0"),
        )

    def _needs_fee_backfill_locked(self, rec: _TrackedOrder) -> bool:
        if (
            rec.status not in ("FILLED", "CANCELED")
            or rec.filled_base_qty <= 0
            or rec.fee_invalid
            or rec.fee_backfill_attempts >= self.fee_backfill_max_attempts
            or not rec.venue_order_id
        ):
            return False
        trade_cov = self._trade_fee_covered_qty_locked(rec)
        return rec.fee_usd is None or trade_cov < rec.filled_base_qty

    def _mark_terminal_locked(self, rec: _TrackedOrder, status: str, *, reason: str | None = None) -> None:
        rec.status = status
        if reason is not None:
            rec.reason = reason
        rec.submit_uncertain = False
        rec.not_found_confirmations = 0
        rec.uncertain_error_checks = 0
        if rec.terminal_ts is None:
            rec.terminal_ts = time.monotonic()
        self._active_coids.discard(rec.client_order_id)

    def _discard_async_locks(self, coids: list[str]) -> None:
        for c in coids:
            lock = self._order_async_locks.get(c)
            if lock is not None and not lock.locked():
                self._order_async_locks.pop(c, None)

    def _prune_terminal_orders_locked(self, now: float) -> list[str]:
        if len(self._orders) <= self.terminal_prune_threshold:
            return []
        cutoff = now - self.terminal_retention_secs
        pruned: list[str] = []
        for coid, rec in list(self._orders.items()):
            if (
                rec.status in TERMINAL_ORDER_STATUSES
                and rec.terminal_ts is not None
                and rec.terminal_ts <= cutoff
                and not self._needs_fee_backfill_locked(rec)
            ):
                self._orders.pop(coid, None)
                self._active_coids.discard(coid)
                pruned.append(coid)
        if pruned:
            pruned_set = set(pruned)
            stale_vids = [vid for vid, mapped in self._vid_to_coid.items() if mapped in pruned_set]
            for vid in stale_vids:
                self._vid_to_coid.pop(vid, None)
        return pruned

    def _recompute_order_fee_locked(self, rec: _TrackedOrder) -> None:
        if rec.fee_invalid or rec.filled_base_qty <= 0:
            rec.fee_usd = None
            return
        trade_cov = self._trade_fee_covered_qty_locked(rec)
        if trade_cov >= rec.filled_base_qty:
            rec.fee_usd = sum(rec.fee_by_trade_id.values(), Decimal("0"))
            return
        if rec.order_level_fee_usd is not None and rec.order_level_fee_qty >= rec.filled_base_qty:
            rec.fee_usd = rec.order_level_fee_usd
            return
        rec.fee_usd = None

    def _resolve_coid_locked(self, client_oid: Any, venue_oid: Any) -> str | None:
        coid = str(client_oid or "").strip()
        if coid and coid in self._orders:
            return coid
        vid = str(venue_oid or "").strip()
        if vid and vid in self._vid_to_coid:
            mapped = self._vid_to_coid[vid]
            if mapped in self._orders:
                return mapped
        return None

    def _apply_order_snapshot_dict(
        self,
        data: Mapping[str, Any],
        *,
        fallback_coid: str | None = None,
        emit_event: bool = True,
    ) -> tuple[str | None, bool, bool]:
        """Update tracked order from Bitget order/detail or WS orders row.

        Returns (resolved_coid, needs_fee_backfill, changed).
        """
        raw_coid = data.get("clientOid") or fallback_coid
        raw_vid = data.get("orderId")
        raw_state = data.get("state") if data.get("state") is not None else data.get("status")
        filled_dec = _parse_optional_decimal(
            data.get("accBaseVolume") if data.get("accBaseVolume") is not None else data.get("baseVolume")
        )
        avg_px_dec = _parse_optional_decimal(
            data.get("priceAvg")
            if data.get("priceAvg") not in (None, "", "0", "0.0")
            else data.get("fillPrice")
        )
        fee_invalid, parsed_fee = self._parse_fee_detail_list(data.get("feeDetail"))

        needs_backfill = False
        changed = False
        event_to_emit: BitgetGatewayEvent | None = None
        with self._orders_lock:
            coid = self._resolve_coid_locked(raw_coid, raw_vid)
            if coid is None:
                return None, False, False
            rec = self._orders[coid]
            prev_fee_usd = rec.fee_usd
            rec.submit_uncertain = False
            rec.not_found_confirmations = 0
            rec.uncertain_error_checks = 0
            vid_str = str(raw_vid or "").strip()
            if vid_str and not rec.venue_order_id:
                rec.venue_order_id = vid_str
                self._vid_to_coid[vid_str] = coid
            elif vid_str:
                self._vid_to_coid.setdefault(vid_str, coid)

            snap_qty = filled_dec if (filled_dec is not None and filled_dec > 0) else Decimal("0")
            new_filled = max(rec.filled_base_qty, snap_qty)
            filled_increased = new_filled > rec.filled_base_qty
            if filled_increased:
                rec.filled_base_qty = new_filled
                changed = True
            if (
                avg_px_dec is not None
                and avg_px_dec > 0
                and snap_qty > 0
                and snap_qty >= rec.filled_base_qty
                and snap_qty >= rec.order_level_price_qty
            ):
                if rec.avg_fill_price != avg_px_dec or snap_qty > rec.order_level_price_qty:
                    rec.avg_fill_price = avg_px_dec
                    rec.order_level_price_qty = snap_qty
                    changed = True

            mapped_status = _map_bitget_order_status(raw_state, rec.filled_base_qty)
            if rec.status in TERMINAL_ORDER_STATUSES:
                if rec.status != "FILLED" and mapped_status == "FILLED":
                    self._mark_terminal_locked(rec, "FILLED")
                    changed = True
            else:
                if mapped_status in TERMINAL_ORDER_STATUSES:
                    self._mark_terminal_locked(rec, mapped_status)
                    changed = True
                elif mapped_status != rec.status:
                    rec.status = mapped_status
                    changed = True

            fee_applied = False
            if fee_invalid:
                if not rec.fee_invalid:
                    rec.fee_invalid = True
                    rec.fee_usd = None
                    changed = True
            elif (
                parsed_fee is not None
                and snap_qty > 0
                and snap_qty >= rec.filled_base_qty
                and snap_qty >= rec.order_level_fee_qty
            ):
                if rec.order_level_fee_usd != parsed_fee or snap_qty > rec.order_level_fee_qty:
                    rec.order_level_fee_usd = parsed_fee
                    rec.order_level_fee_qty = snap_qty
                    changed = True
                fee_applied = True

            if filled_increased or fee_applied:
                self._recompute_order_fee_locked(rec)

            fee_cleared = prev_fee_usd is not None and rec.fee_usd is None and not rec.fee_invalid
            if fee_cleared:
                changed = True

            needs_backfill = self._needs_fee_backfill_locked(rec)

            if changed or fee_invalid or fee_applied or fee_cleared:
                event_to_emit = BitgetGatewayEvent(
                    client_order_id=coid,
                    fee_usd=None if rec.fee_invalid else rec.fee_usd,
                    fee_invalid=rec.fee_invalid,
                    fee_cleared=fee_cleared,
                )

        if emit_event and event_to_emit is not None:
            self._enqueue_event(event_to_emit)
        return coid, needs_backfill, changed

    def _apply_fill_rows(
        self,
        rows: list[Any],
        *,
        fallback_coid: str | None = None,
        emit_event: bool = True,
    ) -> set[str]:
        """Deduplicate per-trade fills by (coid, trade_id) and accumulate fees/fills."""
        touched_coids: set[str] = set()
        prev_fees: dict[str, Decimal | None] = {}
        events_to_emit: list[BitgetGatewayEvent] = []
        with self._orders_lock:
            for row in rows:
                if not isinstance(row, dict):
                    continue
                coid = self._resolve_coid_locked(row.get("clientOid") or fallback_coid, row.get("orderId"))
                if coid is None:
                    continue
                rec = self._orders[coid]
                if coid not in prev_fees:
                    prev_fees[coid] = rec.fee_usd
                rec.submit_uncertain = False
                rec.not_found_confirmations = 0
                rec.uncertain_error_checks = 0
                vid_str = str(row.get("orderId") or "").strip()
                if vid_str:
                    if not rec.venue_order_id:
                        rec.venue_order_id = vid_str
                    self._vid_to_coid.setdefault(vid_str, coid)

                trade_id = str(row.get("tradeId") or "").strip()
                dedup_id = trade_id or f"anon:{row.get('cTime') or row.get('updatedTime') or ''}:{row.get('baseVolume') or row.get('execQty') or ''}"

                qty_dec = _parse_optional_decimal(
                    row.get("baseVolume")
                    if row.get("baseVolume") is not None
                    else (row.get("execQty") if row.get("execQty") is not None else row.get("size"))
                )
                px_dec = _parse_optional_decimal(
                    row.get("price") if row.get("price") is not None else row.get("execPrice")
                )
                fee_invalid, trade_fee = self._parse_fee_detail_list(row.get("feeDetail"))

                row_changed = False
                if dedup_id not in rec.seen_trade_ids:
                    rec.seen_trade_ids.add(dedup_id)
                    row_changed = True

                eff_qty = rec.filled_by_trade_id.get(dedup_id)
                if eff_qty is None and qty_dec is not None and qty_dec > 0:
                    rec.filled_by_trade_id[dedup_id] = qty_dec
                    eff_qty = qty_dec
                    row_changed = True

                if (
                    dedup_id not in rec.fill_notional_by_trade_id
                    and eff_qty is not None
                    and eff_qty > 0
                    and px_dec is not None
                    and px_dec > 0
                ):
                    rec.fill_notional_by_trade_id[dedup_id] = eff_qty * px_dec
                    row_changed = True

                if fee_invalid:
                    if not rec.fee_invalid:
                        rec.fee_invalid = True
                        rec.fee_usd = None
                        row_changed = True
                elif trade_fee is not None and not rec.fee_invalid and dedup_id not in rec.fee_by_trade_id:
                    rec.fee_by_trade_id[dedup_id] = trade_fee
                    row_changed = True

                if not row_changed:
                    continue

                trade_qty_sum = sum(rec.filled_by_trade_id.values(), Decimal("0"))
                if trade_qty_sum > rec.filled_base_qty:
                    rec.filled_base_qty = trade_qty_sum
                priced_qty = sum(
                    (
                        rec.filled_by_trade_id[tid]
                        for tid in rec.fill_notional_by_trade_id
                        if tid in rec.filled_by_trade_id
                    ),
                    Decimal("0"),
                )
                if priced_qty > 0 and (
                    rec.avg_fill_price is None
                    or rec.order_level_price_qty <= 0
                    or (priced_qty >= rec.filled_base_qty and priced_qty >= rec.order_level_price_qty)
                ):
                    tot_notional = sum(rec.fill_notional_by_trade_id.values(), Decimal("0"))
                    rec.avg_fill_price = tot_notional / priced_qty
                if rec.status not in TERMINAL_ORDER_STATUSES:
                    if rec.filled_base_qty >= rec.qty_contracts > 0:
                        self._mark_terminal_locked(rec, "FILLED")
                    elif rec.filled_base_qty > 0:
                        rec.status = "PARTIAL"

                self._recompute_order_fee_locked(rec)
                touched_coids.add(coid)

            for coid in sorted(touched_coids):
                rec = self._orders[coid]
                prev_fee = prev_fees.get(coid)
                fee_cleared = prev_fee is not None and rec.fee_usd is None and not rec.fee_invalid
                events_to_emit.append(
                    BitgetGatewayEvent(
                        client_order_id=coid,
                        fee_usd=None if rec.fee_invalid else rec.fee_usd,
                        fee_invalid=rec.fee_invalid,
                        fee_cleared=fee_cleared,
                    )
                )

        if emit_event:
            for evt in events_to_emit:
                self._enqueue_event(evt)
        return touched_coids

    def _fetch_order_fills_sync(self, raw_symbol: str, venue_order_id: str, coid: str) -> None:
        if not venue_order_id:
            return
        with self._orders_lock:
            rec0 = self._orders.get(coid)
            prev_fee_usd = rec0.fee_usd if rec0 is not None else None
        cursor: str | None = None
        seen_cursors: set[str] = set()
        any_touched = False
        for _ in range(5):
            params: dict[str, Any] = {
                "productType": BITGET_PRODUCT_TYPE,
                "symbol": raw_symbol,
                "orderId": venue_order_id,
                "limit": "100",
            }
            if cursor:
                params["idLessThan"] = cursor
            path = bitget_signed_get_path("/api/v2/mix/order/fills", params)
            try:
                resp = self._signed_rest("GET", path, None)
            except Exception as exc:
                self._enqueue_log("WARNING", f"[bitget.gw] order/fills failed for {coid}: {type(exc).__name__}")
                break
            if str(resp.get("code") or "") != "00000":
                break
            data = resp.get("data")
            if isinstance(data, dict):
                rows = data.get("fillList") or []
                end_id = str(data.get("endId") or "").strip()
            elif isinstance(data, list):
                rows = data
                end_id = ""
            else:
                rows = []
                end_id = ""
            if not isinstance(rows, list) or not rows:
                break
            touched = self._apply_fill_rows(rows, fallback_coid=coid, emit_event=False)
            if coid in touched:
                any_touched = True
            with self._orders_lock:
                rec = self._orders.get(coid)
                covered = rec is not None and (
                    rec.fee_invalid
                    or (rec.filled_base_qty > 0 and self._trade_fee_covered_qty_locked(rec) >= rec.filled_base_qty)
                )
            if covered or not end_id or end_id in seen_cursors:
                break
            seen_cursors.add(end_id)
            cursor = end_id
        if any_touched:
            with self._orders_lock:
                rec = self._orders.get(coid)
                if rec is not None:
                    evt = BitgetGatewayEvent(
                        client_order_id=coid,
                        fee_usd=None if rec.fee_invalid else rec.fee_usd,
                        fee_invalid=rec.fee_invalid,
                        fee_cleared=(prev_fee_usd is not None and rec.fee_usd is None and not rec.fee_invalid),
                    )
                else:
                    evt = None
            if evt is not None:
                self._enqueue_event(evt)

    def _query_order_detail_sync(
        self,
        raw_symbol: str,
        coid: str,
        venue_order_id: str = "",
        *,
        backfill_fills: bool = True,
    ) -> str:
        """Query REST order/detail and update state. Returns 'FOUND', 'NOT_FOUND', or 'ERROR'."""
        params: dict[str, Any] = {
            "productType": BITGET_PRODUCT_TYPE,
            "symbol": raw_symbol,
        }
        if venue_order_id:
            params["orderId"] = venue_order_id
        else:
            params["clientOid"] = coid
        path = bitget_signed_get_path("/api/v2/mix/order/detail", params)
        try:
            resp = self._signed_rest("GET", path, None)
        except Exception as exc:
            self._enqueue_log("WARNING", f"[bitget.gw] order/detail failed for {coid}: {type(exc).__name__}")
            return "ERROR"

        code = str(resp.get("code") or "")
        if code in _BITGET_NOT_FOUND_CODES:
            return "NOT_FOUND"
        if is_bitget_uta_error(code, resp.get("msg")):
            self._enqueue_log("ERROR", f"[bitget.gw] UTA error on order/detail code={_sanitize_code(code)}")
            return "ERROR"
        if code != "00000":
            return "ERROR"
        data = resp.get("data")
        if not isinstance(data, dict):
            return "ERROR"

        with self._orders_lock:
            cur0 = self._orders.get(coid)
            prev_fee_usd = cur0.fee_usd if cur0 is not None else None

        _, needs_backfill, _ = self._apply_order_snapshot_dict(
            data,
            fallback_coid=coid,
            emit_event=False,
        )
        if backfill_fills and needs_backfill:
            should_fetch_fills = False
            vid = str(data.get("orderId") or venue_order_id or "").strip()
            with self._orders_lock:
                cur = self._orders.get(coid)
                now_bf = time.monotonic()
                if (
                    cur is not None
                    and vid
                    and self._needs_fee_backfill_locked(cur)
                    and (cur.fee_backfill_attempts == 0 or (now_bf - cur.last_fee_backfill_ts) >= self.resync_interval_secs)
                ):
                    cur.fee_backfill_attempts += 1
                    cur.last_fee_backfill_ts = now_bf
                    should_fetch_fills = True
            if should_fetch_fills:
                self._fetch_order_fills_sync(raw_symbol, vid, coid)
        with self._orders_lock:
            rec = self._orders.get(coid)
            if rec is not None:
                rec.last_resync_ts = time.monotonic()
                evt = BitgetGatewayEvent(
                    client_order_id=coid,
                    fee_usd=None if rec.fee_invalid else rec.fee_usd,
                    fee_invalid=rec.fee_invalid,
                    fee_cleared=(prev_fee_usd is not None and rec.fee_usd is None and not rec.fee_invalid),
                )
            else:
                evt = None
        if evt is not None:
            self._enqueue_event(evt)
        return "FOUND"

    @staticmethod
    def _has_positive_exchange_ack_locked(rec: _TrackedOrder) -> bool:
        return bool(rec.venue_order_id) or rec.filled_base_qty > 0 or rec.status != "OPEN"

    def _reconcile_uncertain_place_sync(self, raw_sym: str, coid: str, detail_msg: str) -> str:
        with self._orders_lock:
            cur0 = self._orders.get(coid)
            if cur0 is None or cur0.status in TERMINAL_ORDER_STATUSES:
                return "TERMINAL"
            if self._has_positive_exchange_ack_locked(cur0):
                cur0.submit_uncertain = False
                cur0.not_found_confirmations = 0
                cur0.uncertain_error_checks = 0
                return "ACKED"

        self._enqueue_log(
            "WARNING",
            f"[bitget.gw] place uncertain ({detail_msg}); checking order/detail for {coid}",
        )
        outcome = self._query_order_detail_sync(raw_sym, coid, "")
        if outcome == "NOT_FOUND":
            with self._orders_lock:
                cur = self._orders.get(coid)
                if cur is not None and cur.status not in TERMINAL_ORDER_STATUSES:
                    if self._has_positive_exchange_ack_locked(cur):
                        cur.submit_uncertain = False
                        cur.not_found_confirmations = 0
                        cur.uncertain_error_checks = 0
                        return "ACKED"
                    cur.submit_uncertain = True
                    cur.not_found_confirmations = 1
                    cur.uncertain_error_checks = 0
                    cur.last_resync_ts = time.monotonic()
        elif outcome == "ERROR":
            with self._orders_lock:
                cur = self._orders.get(coid)
                if cur is not None and cur.status not in TERMINAL_ORDER_STATUSES:
                    if self._has_positive_exchange_ack_locked(cur):
                        cur.submit_uncertain = False
                        cur.not_found_confirmations = 0
                        cur.uncertain_error_checks = 0
                        return "ACKED"
                    cur.submit_uncertain = True
                    cur.not_found_confirmations = 0
                    cur.uncertain_error_checks = 1
                    cur.last_resync_ts = time.monotonic()
        return outcome

    def _exec_place_sync(self, coid: str) -> None:
        with self._orders_lock:
            rec = self._orders.get(coid)
            if rec is None or rec.status in TERMINAL_ORDER_STATUSES:
                return
            body: dict[str, Any] = {
                "symbol": rec.raw_symbol,
                "productType": BITGET_PRODUCT_TYPE,
                "marginMode": BITGET_MARGIN_MODE,
                "marginCoin": BITGET_MARGIN_COIN,
                "size": _format_decimal_plain(rec.qty_contracts),
                "side": rec.side.lower(),
                "orderType": "limit" if rec.order_type == "LIMIT" else "market",
                "reduceOnly": "YES" if rec.reduce_only else "NO",
                "clientOid": rec.client_order_id,
            }
            if rec.order_type == "LIMIT" and rec.price is not None:
                body["price"] = _format_decimal_plain(rec.price)
                if rec.post_only:
                    body["force"] = "post_only"
                elif rec.ioc:
                    body["force"] = "ioc"
                else:
                    body["force"] = "gtc"
            raw_sym = rec.raw_symbol

        try:
            resp = self._signed_rest("POST", "/api/v2/mix/order/place-order", body)
        except Exception as exc:
            outcome = self._reconcile_uncertain_place_sync(raw_sym, coid, type(exc).__name__)
            if outcome in ("FOUND", "ACKED"):
                should_cancel = False
                with self._orders_lock:
                    cur = self._orders.get(coid)
                    if cur is not None and cur.cancel_requested and not cur.submit_uncertain and cur.status not in TERMINAL_ORDER_STATUSES:
                        cur.cancel_in_place_handled = True
                        should_cancel = True
                if should_cancel:
                    self._exec_cancel_sync(coid)
            return

        code = str(resp.get("code") or "")
        if code == "00000":
            data = resp.get("data")
            vid = str(data.get("orderId") or "").strip() if isinstance(data, dict) else ""
            should_cancel = False
            with self._orders_lock:
                cur = self._orders.get(coid)
                if cur is not None:
                    cur.submit_uncertain = False
                    cur.not_found_confirmations = 0
                    cur.uncertain_error_checks = 0
                    if vid:
                        if not cur.venue_order_id:
                            cur.venue_order_id = vid
                        self._vid_to_coid[vid] = coid
                    if cur.cancel_requested and cur.status not in TERMINAL_ORDER_STATUSES:
                        cur.cancel_in_place_handled = True
                        should_cancel = True
            if should_cancel:
                self._exec_cancel_sync(coid)
            return

        if not _is_bitget_definite_place_reject(code, resp.get("msg")):
            outcome = self._reconcile_uncertain_place_sync(raw_sym, coid, f"code={_sanitize_code(code)}")
            if outcome in ("FOUND", "ACKED"):
                should_cancel = False
                with self._orders_lock:
                    cur = self._orders.get(coid)
                    if cur is not None and cur.cancel_requested and not cur.submit_uncertain and cur.status not in TERMINAL_ORDER_STATUSES:
                        cur.cancel_in_place_handled = True
                        should_cancel = True
                if should_cancel:
                    self._exec_cancel_sync(coid)
            return

        if is_bitget_uta_error(code, resp.get("msg")):
            self._enqueue_log("ERROR", f"[bitget.gw] place hit UTA error code={_sanitize_code(code)}")
        else:
            self._enqueue_log("WARNING", f"[bitget.gw] place rejected code={_sanitize_code(code)} coid={coid}")
        with self._orders_lock:
            cur = self._orders.get(coid)
            if cur is not None and cur.status not in TERMINAL_ORDER_STATUSES:
                self._mark_terminal_locked(cur, "REJECTED", reason=f"bitget_code_{_sanitize_code(code)}")
        self._enqueue_event(BitgetGatewayEvent(client_order_id=coid))

    def _exec_cancel_sync(self, coid: str) -> None:
        with self._orders_lock:
            rec = self._orders.get(coid)
            if rec is None or rec.status in TERMINAL_ORDER_STATUSES:
                return
            raw_sym = rec.raw_symbol
            vid = rec.venue_order_id
            body: dict[str, Any] = {
                "symbol": raw_sym,
                "productType": BITGET_PRODUCT_TYPE,
                "marginCoin": BITGET_MARGIN_COIN,
            }
            if vid:
                body["orderId"] = vid
            else:
                body["clientOid"] = coid

        try:
            resp = self._signed_rest("POST", "/api/v2/mix/order/cancel-order", body)
            code = str(resp.get("code") or "")
        except Exception as exc:
            self._enqueue_log("WARNING", f"[bitget.gw] cancel error ({type(exc).__name__}); checking order/detail for {coid}")
            code = "__TRANSPORT__"

        if code == "00000":
            if not self._ws_subs_ready:
                self._query_order_detail_sync(raw_sym, coid, vid)
            return

        outcome = self._query_order_detail_sync(raw_sym, coid, vid)
        with self._orders_lock:
            cur = self._orders.get(coid)
            if cur is None:
                return
            is_terminal = cur.status in TERMINAL_ORDER_STATUSES
            if not is_terminal:
                cur.cancel_requested = False
        if not is_terminal:
            self._enqueue_log(
                "WARNING",
                f"[bitget.gw] cancel rejected/unconfirmed coid={coid} code={_sanitize_code(code)} detail={outcome}",
            )
            self._enqueue_event(BitgetGatewayEvent(client_order_id=coid, cancel_rejected=True))

    def _non_terminal_orders_snapshot(self) -> list[tuple[str, str, str, float]]:
        with self._orders_lock:
            out: list[tuple[str, str, str, float]] = []
            for coid in list(self._active_coids):
                rec = self._orders.get(coid)
                if rec is None or rec.status in TERMINAL_ORDER_STATUSES:
                    self._active_coids.discard(coid)
                    continue
                out.append((rec.client_order_id, rec.raw_symbol, rec.venue_order_id, rec.last_resync_ts))
            return out

    def _resync_non_terminal_sync(self, *, force: bool = False) -> None:
        now = time.monotonic()
        for coid, raw_sym, vid, last_ts in self._non_terminal_orders_snapshot():
            if not force and (now - last_ts) < self.resync_interval_secs:
                continue
            query_start = time.monotonic()
            outcome = self._query_order_detail_sync(raw_sym, coid, vid)
            emit_rejected = False
            if outcome == "NOT_FOUND":
                with self._orders_lock:
                    cur = self._orders.get(coid)
                    if cur is not None and cur.status not in TERMINAL_ORDER_STATUSES:
                        if self._has_positive_exchange_ack_locked(cur):
                            cur.submit_uncertain = False
                            cur.not_found_confirmations = 0
                            cur.uncertain_error_checks = 0
                        elif cur.submit_uncertain:
                            if (
                                cur.not_found_confirmations == 0
                                or (query_start - cur.last_resync_ts) >= self.resync_interval_secs
                            ):
                                cur.last_resync_ts = time.monotonic()
                                cur.uncertain_error_checks = 0
                                cur.not_found_confirmations += 1
                                if cur.not_found_confirmations >= 2:
                                    self._mark_terminal_locked(
                                        cur,
                                        "REJECTED",
                                        reason="not_found_after_uncertain_submit",
                                    )
                                    emit_rejected = True
                if emit_rejected:
                    self._enqueue_log(
                        "WARNING",
                        f"[bitget.gw] uncertain submit confirmed absent twice; rejecting {coid}",
                    )
                    self._enqueue_event(BitgetGatewayEvent(client_order_id=coid))
            elif outcome == "ERROR":
                alert_checks = 0
                with self._orders_lock:
                    cur = self._orders.get(coid)
                    if cur is not None and cur.status not in TERMINAL_ORDER_STATUSES:
                        if self._has_positive_exchange_ack_locked(cur):
                            cur.submit_uncertain = False
                            cur.not_found_confirmations = 0
                            cur.uncertain_error_checks = 0
                        elif cur.submit_uncertain:
                            cur.last_resync_ts = time.monotonic()
                            cur.not_found_confirmations = 0
                            cur.uncertain_error_checks += 1
                            if cur.uncertain_error_checks % self.uncertain_error_max_checks == 0:
                                alert_checks = cur.uncertain_error_checks
                if alert_checks > 0:
                    self._enqueue_log(
                        "ERROR",
                        f"[bitget.gw] uncertain submit unresolved after {alert_checks} detail errors: {coid}",
                    )

        now_bf = time.monotonic()
        backfill_targets: list[tuple[str, str, str]] = []
        with self._orders_lock:
            for coid, rec in self._orders.items():
                if not self._needs_fee_backfill_locked(rec):
                    continue
                if not force and rec.fee_backfill_attempts > 0 and (now_bf - rec.last_fee_backfill_ts) < self.resync_interval_secs:
                    continue
                rec.fee_backfill_attempts += 1
                rec.last_fee_backfill_ts = now_bf
                backfill_targets.append((rec.raw_symbol, rec.venue_order_id, coid))
        for raw_sym, vid, coid in backfill_targets:
            self._fetch_order_fills_sync(raw_sym, vid, coid)

        with self._orders_lock:
            pruned_coids = self._prune_terminal_orders_locked(time.monotonic())
        if pruned_coids:
            with self._loop_lock:
                loop = self._loop
            if loop is not None and not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(self._discard_async_locks, pruned_coids)
                except RuntimeError:
                    self._discard_async_locks(pruned_coids)
            else:
                self._discard_async_locks(pruned_coids)

    def _drain_backfills_sync(self) -> None:
        while True:
            try:
                coid = self._backfill_queue.get_nowait()
            except queue.Empty:
                break
            now_bf = time.monotonic()
            with self._orders_lock:
                rec = self._orders.get(coid)
                if rec is None or not self._needs_fee_backfill_locked(rec):
                    continue
                if rec.fee_backfill_attempts > 0 and (now_bf - rec.last_fee_backfill_ts) < self.resync_interval_secs:
                    continue
                rec.fee_backfill_attempts += 1
                rec.last_fee_backfill_ts = now_bf
                raw_sym = rec.raw_symbol
                vid = rec.venue_order_id
            self._fetch_order_fills_sync(raw_sym, vid, coid)

    async def _resync_loop(self, resync_wake: asyncio.Event, stop_evt: asyncio.Event) -> None:
        while not stop_evt.is_set():
            wake_t = asyncio.create_task(resync_wake.wait())
            stop_t = asyncio.create_task(stop_evt.wait())
            done, _ = await asyncio.wait(
                {wake_t, stop_t},
                timeout=self.resync_interval_secs,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for t in (wake_t, stop_t):
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except BaseException:
                        pass
            if stop_evt.is_set():
                break
            if resync_wake.is_set():
                resync_wake.clear()
            force_now = self._force_resync_pending
            self._force_resync_pending = False
            await asyncio.to_thread(self._drain_backfills_sync)
            await asyncio.to_thread(self._resync_non_terminal_sync, force=force_now)

    def _handle_private_ws_message(
        self,
        raw_msg: str | bytes,
        *,
        confirmed_channels: set[str] | None = None,
        rejected_channels: set[str] | None = None,
    ) -> bool:
        text = raw_msg.decode("utf-8", errors="replace") if isinstance(raw_msg, bytes) else raw_msg
        if text == "pong":
            return True
        try:
            msg = json.loads(text)
        except Exception:
            return False
        if not isinstance(msg, dict):
            return False
        event = msg.get("event")
        if event == "subscribe":
            arg = msg.get("arg")
            if isinstance(arg, dict):
                ch = str(arg.get("channel") or "").strip().lower()
                if ch in _BITGET_PRIVATE_WS_CHANNELS:
                    if confirmed_channels is not None:
                        confirmed_channels.add(ch)
                        if _BITGET_PRIVATE_WS_CHANNELS <= confirmed_channels:
                            self._ws_subs_ready = True
                    if rejected_channels is not None:
                        rejected_channels.discard(ch)
            return False
        if event == "error":
            code = _sanitize_code(msg.get("code"))
            if is_bitget_uta_error(code, msg.get("msg")):
                self._enqueue_log("ERROR", f"[bitget.ws.priv] UTA error code={code}")
            else:
                self._enqueue_log("WARNING", f"[bitget.ws.priv] ws error code={code}")
            arg = msg.get("arg")
            if isinstance(arg, dict):
                ch = str(arg.get("channel") or "").strip().lower()
                if ch in _BITGET_PRIVATE_WS_CHANNELS:
                    self._ws_subs_ready = False
                    if confirmed_channels is not None:
                        confirmed_channels.discard(ch)
                    if rejected_channels is not None:
                        rejected_channels.add(ch)
            elif confirmed_channels is not None and not (_BITGET_PRIVATE_WS_CHANNELS <= confirmed_channels):
                self._ws_subs_ready = False
                if rejected_channels is not None:
                    rejected_channels.update(_BITGET_PRIVATE_WS_CHANNELS - confirmed_channels)
            return False
        arg = msg.get("arg")
        data = msg.get("data")
        if not isinstance(arg, dict) or not isinstance(data, list):
            return False
        channel = str(arg.get("channel") or "").strip().lower()
        if channel in _BITGET_PRIVATE_WS_CHANNELS:
            if confirmed_channels is not None:
                confirmed_channels.add(channel)
                if _BITGET_PRIVATE_WS_CHANNELS <= confirmed_channels:
                    self._ws_subs_ready = True
            if rejected_channels is not None:
                rejected_channels.discard(channel)
        if channel == "orders":
            need_wake_backfill = False
            for row in data:
                if not isinstance(row, dict):
                    continue
                coid, needs_backfill, _ = self._apply_order_snapshot_dict(row, emit_event=True)
                if needs_backfill and coid is not None:
                    self._backfill_queue.put(coid)
                    need_wake_backfill = True
            if need_wake_backfill:
                self._wake_resync_async()
        elif channel == "fill":
            touched = self._apply_fill_rows(data, emit_event=True)
            need_wake_backfill = False
            if touched:
                with self._orders_lock:
                    for coid in touched:
                        rec = self._orders.get(coid)
                        if rec is not None and self._needs_fee_backfill_locked(rec):
                            self._backfill_queue.put(coid)
                            need_wake_backfill = True
            if need_wake_backfill:
                self._wake_resync_async()
        return False

    async def _private_ws_loop(self, stop_evt: asyncio.Event) -> None:
        def _priv_sub_payload(channels: list[str]) -> dict[str, Any]:
            return {
                "op": "subscribe",
                "args": [
                    {"instType": BITGET_PRODUCT_TYPE, "channel": ch, "instId": "default"}
                    for ch in channels
                ],
            }

        attempt = 0
        while not stop_evt.is_set():
            api_key, api_secret, api_passphrase = self._optional_creds()
            if not (api_key and api_secret and api_passphrase):
                self._enqueue_log("WARNING", "[bitget.ws.priv] credentials unavailable; skipping private WS")
                try:
                    await asyncio.wait_for(stop_evt.wait(), timeout=self.resync_interval_secs)
                except asyncio.TimeoutError:
                    pass
                continue

            try:
                async with websockets.connect(
                    self.ws_url,
                    ping_interval=None,
                    ping_timeout=None,
                    open_timeout=10.0,
                    close_timeout=3.0,
                ) as ws:
                    login_frame = bitget_ws_login_payload(
                        api_key=api_key,
                        api_secret=api_secret,
                        api_passphrase=api_passphrase,
                    )
                    await ws.send(json.dumps(login_frame, separators=(",", ":")))

                    deadline = time.monotonic() + self.recv_timeout_secs
                    logged_in = False
                    while time.monotonic() < deadline and not stop_evt.is_set():
                        rem = max(0.05, deadline - time.monotonic())
                        raw_login = await asyncio.wait_for(ws.recv(), timeout=rem)
                        text_login = raw_login.decode("utf-8", errors="replace") if isinstance(raw_login, bytes) else raw_login
                        if text_login == "pong":
                            continue
                        obj = json.loads(text_login)
                        if isinstance(obj, dict) and obj.get("event") == "login":
                            code = str(obj.get("code") or "")
                            if code == "0":
                                logged_in = True
                                break
                            raise RuntimeError(f"private ws login rejected code={_sanitize_code(code)}")
                        if isinstance(obj, dict) and obj.get("event") == "error":
                            code = str(obj.get("code") or "")
                            if is_bitget_uta_error(code, obj.get("msg")):
                                raise RuntimeError(f"private ws UTA error code={_sanitize_code(code)}")
                            raise RuntimeError(f"private ws login error code={_sanitize_code(code)}")
                    if not logged_in:
                        raise TimeoutError("private ws login timeout")

                    self._ws_logged_in = True
                    self._ws_subs_ready = False
                    attempt = 0
                    confirmed_priv_channels: set[str] = set()
                    rejected_priv_channels: set[str] = set()
                    await ws.send(json.dumps(_priv_sub_payload(["orders", "fill"]), separators=(",", ":")))
                    self._enqueue_log("INFO", f"[bitget.ws.priv] logged in & subscribing ({self.environment})")
                    self._force_resync_pending = True
                    self._wake_resync_async()

                    sub_confirm_deadline = time.monotonic() + self.recv_timeout_secs
                    sub_retry_count = 0
                    max_sub_inline_retries = 1
                    last_ping_sent = time.monotonic()
                    awaiting_pong = False
                    pong_deadline = 0.0
                    recv_task: asyncio.Task[Any] | None = None
                    stop_task: asyncio.Task[Any] = asyncio.create_task(stop_evt.wait())
                    try:
                        while not stop_evt.is_set():
                            now = time.monotonic()
                            subs_pending = not (_BITGET_PRIVATE_WS_CHANNELS <= confirmed_priv_channels)
                            if subs_pending and (rejected_priv_channels or now >= sub_confirm_deadline):
                                missing = sorted(_BITGET_PRIVATE_WS_CHANNELS - confirmed_priv_channels)
                                if sub_retry_count < max_sub_inline_retries and missing:
                                    sub_retry_count += 1
                                    rejected_priv_channels.clear()
                                    await ws.send(json.dumps(_priv_sub_payload(missing), separators=(",", ":")))
                                    sub_confirm_deadline = time.monotonic() + self.recv_timeout_secs
                                    self._enqueue_log(
                                        "WARNING",
                                        f"[bitget.ws.priv] retrying unconfirmed private subscriptions={','.join(missing)}",
                                    )
                                else:
                                    raise RuntimeError(
                                        f"private ws subscription failed/unconfirmed channels={','.join(missing)}"
                                    )

                            if not awaiting_pong and (now - last_ping_sent) >= self.ping_interval_secs:
                                await ws.send("ping")
                                last_ping_sent = now
                                awaiting_pong = True
                                pong_deadline = now + self.recv_timeout_secs
                            next_ping_in = max(0.01, (last_ping_sent + self.ping_interval_secs) - time.monotonic())
                            wait_timeout = (
                                min(next_ping_in, max(0.01, pong_deadline - time.monotonic()))
                                if awaiting_pong
                                else next_ping_in
                            )
                            if subs_pending:
                                wait_timeout = min(wait_timeout, max(0.01, sub_confirm_deadline - time.monotonic()))

                            if recv_task is None:
                                recv_task = asyncio.create_task(ws.recv())

                            done, _ = await asyncio.wait(
                                {recv_task, stop_task},
                                timeout=wait_timeout,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if stop_task in done or stop_evt.is_set():
                                break
                            if recv_task in done:
                                raw_msg = recv_task.result()
                                recv_task = None
                                if self._handle_private_ws_message(
                                    raw_msg,
                                    confirmed_channels=confirmed_priv_channels,
                                    rejected_channels=rejected_priv_channels,
                                ):
                                    awaiting_pong = False
                            if awaiting_pong and time.monotonic() >= pong_deadline:
                                raise TimeoutError("private ws pong timeout")
                    finally:
                        for t in (recv_task, stop_task):
                            if t is not None and not t.done():
                                t.cancel()
                                try:
                                    await t
                                except BaseException:
                                    pass
            except asyncio.CancelledError:
                self._ws_logged_in = False
                self._ws_subs_ready = False
                break
            except Exception as exc:
                self._ws_logged_in = False
                self._ws_subs_ready = False
                if stop_evt.is_set():
                    break
                delay = self.reconnect_backoffs[min(attempt, len(self.reconnect_backoffs) - 1)]
                attempt += 1
                self._enqueue_log(
                    "WARNING",
                    f"[bitget.ws.priv] disconnected ({type(exc).__name__}); reconnecting in {delay:.1f}s",
                )
                try:
                    await asyncio.wait_for(stop_evt.wait(), timeout=delay)
                except asyncio.TimeoutError:
                    pass
            finally:
                self._ws_logged_in = False
                self._ws_subs_ready = False


class RoutingVenueGateway:
    """Routes BITGET orders to BitgetVenueGateway and all other venues to the native gateway."""

    def __init__(self, *, native_gateway: VenueGateway, bitget_gateway: VenueGateway) -> None:
        self.native_gateway = native_gateway
        self.bitget_gateway = bitget_gateway

    def _target(self, venue: str, instrument_id: str) -> VenueGateway:
        if venue.upper() == "BITGET" or instrument_id.upper().endswith(".BITGET"):
            return self.bitget_gateway
        return self.native_gateway

    def submit_limit(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        price: Decimal,
        post_only: bool,
        ioc: bool,
        reduce_only: bool,
    ) -> OrderAck:
        return self._target(venue, instrument_id).submit_limit(
            client_order_id=client_order_id,
            venue=venue,
            instrument_id=instrument_id,
            side=side,
            qty_contracts=qty_contracts,
            price=price,
            post_only=post_only,
            ioc=ioc,
            reduce_only=reduce_only,
        )

    def submit_market(
        self,
        *,
        client_order_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        qty_contracts: Decimal,
        reduce_only: bool,
    ) -> OrderAck:
        return self._target(venue, instrument_id).submit_market(
            client_order_id=client_order_id,
            venue=venue,
            instrument_id=instrument_id,
            side=side,
            qty_contracts=qty_contracts,
            reduce_only=reduce_only,
        )

    def cancel_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        return self._target(venue, instrument_id).cancel_order(
            client_order_id=client_order_id,
            venue=venue,
            instrument_id=instrument_id,
        )

    def poll_order(self, *, client_order_id: str, venue: str, instrument_id: str) -> OrderAck:
        return self._target(venue, instrument_id).poll_order(
            client_order_id=client_order_id,
            venue=venue,
            instrument_id=instrument_id,
        )

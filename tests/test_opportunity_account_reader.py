from decimal import Decimal as D
import inspect
from pathlib import Path
import socket
import sys
import threading
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_account_reader import AccountReadError, ReadOnlyAccountReader, aster_signature


def market(venue, instrument_id):
    return NS(venue=venue, instrument_id=instrument_id)


def test_hyperliquid_reads_external_positions_and_orders_without_cache():
    calls = []
    def transport(method, url, payload):
        calls.append((method, url, payload))
        if payload["type"] == "clearinghouseState":
            return {"assetPositions": [{"position": {"coin": "BTC", "szi": "-0.002"}}]}
        if payload["type"] == "perpsAtOpenInterestCap":
            return ["RESOLV", "SAGA"]
        return [{"coin": "BTC", "oid": 123, "cloid": "0xabc"}, {"coin": "ETH", "oid": 99}]
    reader = ReadOnlyAccountReader(environ={"HYPERLIQUID_ACCOUNT_ADDRESS": "0x" + "1"*40}, transport=transport)
    result = reader.read(market("HL", "BTC-USD-PERP.HYPERLIQUID"), D("100"), D("101"), 50)
    assert result.authoritative and result.position_quantity == D("-.002")
    assert result.open_order_ids == ("venue:123",)
    assert result.account_positions == (("BTC-USD-PERP.HYPERLIQUID", D("-.002")),)
    assert ("ETH-USD-PERP.HYPERLIQUID", "venue:99") in result.account_open_orders
    assert result.hl_oi_capped == ("RESOLV-USD-PERP.HYPERLIQUID", "SAGA-USD-PERP.HYPERLIQUID")
    assert len(calls) == 3 and {c[2]["type"] for c in calls} == {
        "clearinghouseState", "openOrders", "perpsAtOpenInterestCap"}


def test_hyperliquid_oi_cap_cache_ttl_and_transient_error_isolation():
    now = [10_000_000_000]
    calls = []
    oi_cap_mode = ["ok"]
    def transport(method, url, payload):
        calls.append(payload["type"])
        if payload["type"] == "clearinghouseState":
            return {"assetPositions": []}
        if payload["type"] == "openOrders":
            return []
        if oi_cap_mode[0] == "rate_limit":
            raise AccountReadError("oi cap throttled", retry_after_secs=15)
        if oi_cap_mode[0] == "malformed":
            return {"error": "unexpected"}
        if oi_cap_mode[0] == "empty":
            return []
        return ["SAGA", "RESOLV", "SAGA"]

    reader = ReadOnlyAccountReader(
        environ={"HYPERLIQUID_ACCOUNT_ADDRESS": "0x" + "1" * 40},
        transport=transport, clock_ns=lambda: now[0], oi_cap_cache_ttl_ms=30000,
    )
    btc = market("HL", "BTC-USD-PERP.HYPERLIQUID")
    first = reader.read(btc, None, None, now[0])
    assert first.hl_oi_capped == ("RESOLV-USD-PERP.HYPERLIQUID", "SAGA-USD-PERP.HYPERLIQUID")
    assert calls.count("perpsAtOpenInterestCap") == 1

    # Invalidating HL account proof must NOT force an immediate re-fetch of perpsAtOpenInterestCap within TTL.
    reader.invalidate("HL")
    now[0] += 5_000_000_000
    second = reader.read(btc, None, None, now[0])
    assert second.hl_oi_capped == ("RESOLV-USD-PERP.HYPERLIQUID", "SAGA-USD-PERP.HYPERLIQUID")
    assert calls.count("perpsAtOpenInterestCap") == 1

    # After 30s TTL, perpsAtOpenInterestCap refreshes and can recover to empty.
    oi_cap_mode[0] = "empty"
    reader.invalidate("HL")
    now[0] += 30_000_000_000
    third = reader.read(btc, None, None, now[0])
    assert third.hl_oi_capped == ()
    assert calls.count("perpsAtOpenInterestCap") == 2

    # Rate limit on perpsAtOpenInterestCap must NOT block subsequent HL position/order reads.
    oi_cap_mode[0] = "rate_limit"
    reader.invalidate("HL")
    now[0] += 31_000_000_000
    fourth = reader.read(btc, None, None, now[0])
    assert fourth.authoritative and fourth.position_quantity == 0 and fourth.hl_oi_capped is None
    assert calls.count("perpsAtOpenInterestCap") == 3

    reader.invalidate("HL")
    now[0] += 2_000_000_000
    fifth = reader.read(btc, None, None, now[0])
    assert fifth.authoritative and fifth.position_quantity == 0 and fifth.hl_oi_capped is None
    # Still within 15s HL_OI_CAP cooldown -> no extra OI cap call, while HL state/orders succeeded.
    assert calls.count("perpsAtOpenInterestCap") == 3

    # Malformed OI cap response also isolates cleanly.
    oi_cap_mode[0] = "malformed"
    reader.invalidate("HL")
    now[0] += 20_000_000_000
    sixth = reader.read(btc, None, None, now[0])
    assert sixth.authoritative and sixth.hl_oi_capped is None
    assert calls.count("perpsAtOpenInterestCap") == 4


def test_hyperliquid_oi_cap_hip3_dex_prefix():
    def transport(method, url, payload):
        if payload["type"] == "clearinghouseState":
            return {"assetPositions": []}
        if payload["type"] == "perpsAtOpenInterestCap":
            assert payload["dex"] == "xyz"
            return ["NVDA", "xyz:TSLA"]
        return []
    reader = ReadOnlyAccountReader(environ={"HYPERLIQUID_ACCOUNT_ADDRESS": "0x" + "1" * 40}, transport=transport)
    result = reader.read(market("HL", "xyz:NVDA-USD-PERP.HYPERLIQUID"), None, None, 50)
    assert result.hl_oi_capped == ("xyz:NVDA-USD-PERP.HYPERLIQUID", "xyz:TSLA-USD-PERP.HYPERLIQUID")


def test_hyperliquid_hip3_dex_and_missing_evidence_fail_closed():
    calls = []
    def transport(method, url, payload):
        calls.append(payload)
        return {"assetPositions": []} if payload["type"] == "clearinghouseState" else []
    reader = ReadOnlyAccountReader(environ={"HYPERLIQUID_ACCOUNT_ADDRESS": "0x"+"1"*40}, transport=transport)
    result = reader.read(market("HL", "xyz:NVDA-USD-PERP.HYPERLIQUID"), D(100), D(101), 50)
    assert result.position_quantity == 0 and all(c["dex"] == "xyz" for c in calls)
    reader.transport = lambda *args: {}
    reader.invalidate("HL")
    with pytest.raises(AccountReadError, match="position evidence"):
        reader.read(market("HL", "BTC-USD-PERP.HYPERLIQUID"), D(100), D(101), 50)


def lighter_payload(**change):
    row = {"symbol": "BTC", "position": "0.002", "sign": -1, "open_order_count": 1,
           "pending_order_count": 0, "position_tied_order_count": 0}
    row.update(change)
    return {"code": 200, "accounts": [{"account_index": 42, "positions": [row]}]}


def test_lighter_nonzero_order_count_never_becomes_false_clean():
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"},
                                   transport=lambda *args: lighter_payload())
    result = reader.read(market("LIGHTER", "BTC-USD-PERP.LIGHTER"), D(100), D(101), 50)
    assert result.position_quantity == D("-.002")
    assert result.open_order_ids == ("unidentified:Lighter:BTC:1",)
    assert result.account_positions == (("BTC-USD-PERP.LIGHTER", D("-.002")),)
    assert result.account_open_orders == (("BTC-USD-PERP.LIGHTER", "unidentified:Lighter:BTC:1"),)


@pytest.mark.parametrize("payload", [
    {}, {"code": 200, "accounts": []},
    {"code": 200, "accounts": [{"account_index": 99, "positions": []}]},
    {"code": 200, "accounts": [{"account_index": 42, "positions": []}]},
    lighter_payload(sign=0), lighter_payload(position=0.002),
    lighter_payload(open_order_count=-1),
])
def test_lighter_incomplete_or_inexact_evidence_is_rejected(payload):
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"}, transport=lambda *args: payload)
    with pytest.raises(AccountReadError):
        reader.read(market("LIGHTER", "BTC-USD-PERP.LIGHTER"), D(100), D(101), 50)


def test_aster_only_signed_read_endpoints_and_monotonic_nonce():
    calls, signed = [], []
    def sign(message, key, address, environment):
        signed.append(message)
        return "0x" + "a"*130
    def transport(method, url, payload=None):
        calls.append((method, url, payload))
        if "/positionRisk?" in url:
            return [{"symbol": "ETHUSDT", "positionAmt": "-.01", "positionSide": "BOTH"}]
        return [{"symbol": "ETHUSDT", "clientOrderId": "owned-1"}]
    reader = ReadOnlyAccountReader(environ={"ASTER_MAINNET_SIGNER_PRIVATE_KEY": "fixture",
        "ASTER_MAINNET_SIGNER_ADDRESS": "0x"+"1"*40, "ASTER_MAINNET_USER_ADDRESS": "0x"+"2"*40},
        transport=transport, aster_signer=sign, clock_ns=lambda: 10_000)
    result = reader.read(market("ASTER", "ETHUSDT-PERP.ASTER"), D(100), D(101), 50)
    assert result.position_quantity == D("-.01") and result.open_order_ids == ("owned-1",)
    assert result.account_positions == (("ETHUSDT-PERP.ASTER", D("-.01")),)
    assert result.account_open_orders == (("ETHUSDT-PERP.ASTER", "owned-1"),)
    assert all(method == "GET" and body is None for method, _, body in calls)
    assert signed[0].startswith("nonce=10&user=") and signed[1].startswith("nonce=11&user=")
    assert all("symbol=" not in message for message in signed)


def test_aster_hedge_mode_not_flattened_by_netting_long_short():
    reader = ReadOnlyAccountReader(environ={"ASTER_MAINNET_SIGNER_PRIVATE_KEY": "fixture",
        "ASTER_MAINNET_SIGNER_ADDRESS": "fixture", "ASTER_MAINNET_USER_ADDRESS": "fixture"},
        transport=lambda *args: [{"symbol": "BTCUSDT", "positionAmt": "0", "positionSide": "LONG"}],
        aster_signer=lambda *args: "fixture")
    with pytest.raises(AccountReadError, match="ambiguous"):
        reader.read(market("ASTER", "BTCUSDT-PERP.ASTER"), D(100), D(101), 50)


def test_unsupported_readers_do_not_call_transport_or_credentials():
    class Poison(dict):
        def get(self, *args):
            raise AssertionError("credential read")
    def transport(*args):
        raise AssertionError("network read")
    reader = ReadOnlyAccountReader(environ=Poison(), transport=transport)
    for venue in ("UNKNOWN", "OTHER"):
        with pytest.raises(AccountReadError, match="unsupported"):
            reader.read(market(venue, "BTC."+venue), D(100), D(101), 50)


def test_aster_signature_matches_native_public_test_vector():
    # Public test-only fixture from the native adapter's signing_vectors.json,
    # itself derived from CCXT's static Aster fixtures. No account credentials.
    key = "0xff3bdd43534543d421f05aec535965b5050ad6ac15345435345435453495e771"
    address = "0xb67f9a782d3678a0bac50c22eacbb4924fe9d4cf"
    message = ("nonce=1777576052926000&user=" + address + "&signer=" + address
        + "&symbol=ASTERUSDT&side=SELL&type=LIMIT&quantity=10&price=1&timeInForce=GTC"
        + "&builder=0x1F5877C19e3777Cfd15F9d57253eA4aA5254Ec39&feeRate=0.001")
    expected = ("0xe05f62222f2085b7ce86a88a4c4a57d436dd365192ad016b230bb9705de1ce5e"
                "0f5804332c317ba85c6aa55328defbe54382a1b27ee48e0d8bd82647c625709e1b")
    assert aster_signature(message, key, address, "mainnet") == expected
    assert aster_signature(message, key, address, "testnet") != expected
    with pytest.raises(AccountReadError, match="credentials omitted"):
        aster_signature(message, key, "0x" + "1"*40, "mainnet")


def test_never_touched_lighter_market_flat_with_complete_global_counts_and_shared_cache():
    calls = []
    def transport(*args):
        calls.append(args)
        if "orderBookDetails" in args[1]:
            return {"code": 200, "order_book_details": [
                {"symbol": symbol, "market_config": {"market_margin_mode": 0}}
                for symbol in ("BTC", "ETH")]}
        return {"code": 200, "accounts": [{"index": 42, "positions": [],
            "total_order_count": 0, "pending_order_count": 0, "total_isolated_order_count": 0}]}
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"}, transport=transport,
                                   clock_ns=lambda: 10_000_000)
    first = reader.read(market("LIGHTER", "BTC-USD-PERP.LIGHTER"), D(100), D(101), 50)
    second = reader.read(market("LIGHTER", "ETH-USD-PERP.LIGHTER"), D(100), D(101), 60)
    assert first.position_quantity == second.position_quantity == 0
    assert first.open_order_ids == second.open_order_ids == ()
    assert first.observed_ns == second.observed_ns == 50 and len(calls) == 2
    reader.invalidate("LIGHTER")
    third = reader.read(market("LIGHTER", "BTC-USD-PERP.LIGHTER"), D(100), D(101), 70)
    assert third.observed_ns == 70 and len(calls) == 4


def test_missing_lighter_target_with_global_pending_orders_cannot_pass_flat():
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"}, transport=lambda *args: {
        "code": 200, "accounts": [{"account_index": 42, "positions": [],
            "total_order_count": 0, "pending_order_count": 1, "total_isolated_order_count": 0}]})
    snapshot = reader.read(market("LIGHTER", "BTC-USD-PERP.LIGHTER"), D(100), D(101), 50)
    assert snapshot.position_quantity == 0 and snapshot.open_order_ids


def test_lighter_pending_and_conditional_orders_are_not_ignored():
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"},
        transport=lambda *args: lighter_payload(position="0", sign=1, open_order_count=0,
            pending_order_count=1, position_tied_order_count=2))
    snapshot = reader.read(market("LIGHTER", "BTC-USD-PERP.LIGHTER"), D(100), D(101), 50)
    assert snapshot.position_quantity == 0 and snapshot.open_order_ids == ("unidentified:Lighter:BTC:3",)


def test_lighter_complete_account_uses_configured_instrument_suffix_without_quotes():
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"},
        transport=lambda *args: lighter_payload())
    snapshot = reader.read(market("LIGHTER", "BTC-PERP.LIGHTER"), None, None, 50)
    assert snapshot.position_quantity == D("-.002")
    assert snapshot.account_positions == (("BTC-PERP.LIGHTER", D("-.002")),)
    assert snapshot.account_open_orders == (("BTC-PERP.LIGHTER", "unidentified:Lighter:BTC:1"),)
    assert snapshot.bid is None and snapshot.ask is None and snapshot.book_observed_ns is None


def _start_raw_server(handle_connection):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    srv.settimeout(0.05)
    state = NS(accepts=0, stop=threading.Event(), sockets=[], threads=[])

    def run():
        while not state.stop.is_set():
            try:
                conn, _ = srv.accept()
            except TimeoutError:
                continue
            except OSError:
                break
            state.accepts += 1
            state.sockets.append(conn)
            idx = state.accepts
            t = threading.Thread(target=handle_connection, args=(conn, idx, state.stop), daemon=True)
            state.threads.append(t)
            t.start()

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    def close():
        state.stop.set()
        try:
            srv.close()
        except OSError:
            pass
        for s in state.sockets:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass
        thread.join(timeout=2)
        for t in state.threads:
            t.join(timeout=2)

    return srv.getsockname()[1], state, close


def _read_http_request(conn):
    conn.settimeout(2)
    buf = b""
    try:
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        length = 0
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip())
        while len(rest) < length:
            chunk = conn.recv(4096)
            if not chunk:
                return None
            rest += chunk
    except (TimeoutError, OSError):
        return None
    return head.decode("ascii", errors="replace")


def _clear_http_pool(module):
    with module._HTTP_POOL_LOCK:
        for bucket in module._HTTP_POOL.values():
            for conn in bucket:
                try:
                    conn.close()
                except Exception:
                    pass
        module._HTTP_POOL.clear()


def test_http_json_default_timeout_is_3_seconds():
    import opportunity_account_reader as module
    assert inspect.signature(module.http_json).parameters["timeout"].default == 3
    _clear_http_pool(module)

    def handle(conn, _idx, _stop):
        if _read_http_request(conn) is not None:
            body = b'{"ok":true}'
            conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        conn.close()

    port, _, stop = _start_raw_server(handle)
    try:
        assert module.http_json("GET", f"http://127.0.0.1:{port}/ping") == {"ok": True}
        assert ("http", "127.0.0.1", port, 3) in module._HTTP_POOL
    finally:
        _clear_http_pool(module)
        stop()


def test_http_json_reuses_keepalive_socket_per_origin():
    import opportunity_account_reader as module
    _clear_http_pool(module)
    counts = {"requests": 0}

    def handle(conn, _idx, stop):
        while not stop.is_set():
            req = _read_http_request(conn)
            if req is None:
                break
            counts["requests"] += 1
            body = f'{{"seq":{counts["requests"]}}}'.encode()
            conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)

    port, state, stop = _start_raw_server(handle)
    try:
        url = f"http://127.0.0.1:{port}/info"
        assert module.http_json("GET", url) == {"seq": 1}
        assert module.http_json("GET", url) == {"seq": 2}
        assert module.http_json("POST", url, {"type": "clearinghouseState"}) == {"seq": 3}
        assert state.accepts == 1 and counts["requests"] == 3
    finally:
        _clear_http_pool(module)
        stop()


def test_http_json_retries_once_on_stale_keepalive_eof_and_not_on_fresh_eof():
    import opportunity_account_reader as module
    _clear_http_pool(module)
    close_first = threading.Event()
    closed_first = threading.Event()

    def handle_stale(conn, idx, _stop):
        if _read_http_request(conn) is None:
            conn.close()
            return
        body = f'{{"attempt":{idx}}}'.encode()
        conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        if idx == 1:
            close_first.wait(2)
            conn.close()
            closed_first.set()

    port, state, stop = _start_raw_server(handle_stale)
    try:
        url = f"http://127.0.0.1:{port}/stale"
        assert module.http_json("GET", url) == {"attempt": 1}
        close_first.set()
        assert closed_first.wait(2)
        assert module.http_json("GET", url) == {"attempt": 2}
        assert state.accepts == 2
    finally:
        _clear_http_pool(module)
        stop()

    def handle_immediate_eof(conn, _idx, _stop):
        _read_http_request(conn)
        conn.close()

    eof_port, eof_state, stop_eof = _start_raw_server(handle_immediate_eof)
    try:
        with pytest.raises(AccountReadError, match="account read failed; private response omitted"):
            module.http_json("GET", f"http://127.0.0.1:{eof_port}/fresh-eof")
        assert eof_state.accepts == 1
    finally:
        _clear_http_pool(module)
        stop_eof()


def test_http_json_refuses_3xx_redirect_and_closes_socket():
    import opportunity_account_reader as module
    _clear_http_pool(module)
    saw_close = threading.Event()

    def handle(conn, _idx, _stop):
        if _read_http_request(conn) is not None:
            conn.sendall(b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:1/other\r\nContent-Length: 0\r\n\r\n")
            try:
                if conn.recv(16) == b"":
                    saw_close.set()
            except OSError:
                saw_close.set()
        conn.close()

    port, _, stop = _start_raw_server(handle)
    try:
        with pytest.raises(AccountReadError, match="account read redirect refused"):
            module.http_json("GET", f"http://127.0.0.1:{port}/redirect")
        assert saw_close.wait(2)
        assert ("http", "127.0.0.1", port, 3) not in module._HTTP_POOL
    finally:
        _clear_http_pool(module)
        stop()


@pytest.mark.parametrize("status,header,expected", [
    (429, "45", 45), (429, "Thu, 01 Jan 1970 00:02:25 GMT", 45),
    (429, "invalid", 10), (418, "", 120), (500, "45", 0),
])
def test_http_failure_preserves_retry_after_without_private_response(monkeypatch, status, header, expected):
    import opportunity_account_reader as module
    _clear_http_pool(module)

    def handle(conn, _idx, _stop):
        if _read_http_request(conn) is not None:
            extra = f"Retry-After: {header}\r\n" if header else ""
            body = b'{"secret":"private message"}'
            conn.sendall((f"HTTP/1.1 {status} Err\r\n{extra}"
                          f"Content-Length: {len(body)}\r\n\r\n").encode() + body)
        conn.close()

    port, _, stop = _start_raw_server(handle)
    monkeypatch.setattr(module.time, "time", lambda: 100)
    try:
        with pytest.raises(AccountReadError) as caught:
            module.http_json("GET", f"http://127.0.0.1:{port}/secret")
        assert caught.value.retry_after_secs == expected
        assert str(caught.value) == f"account read HTTP {status}; private response omitted"
        assert "private message" not in str(caught.value)
        assert ("http", "127.0.0.1", port, 3) not in module._HTTP_POOL
    finally:
        _clear_http_pool(module)
        stop()


def test_http_json_concurrent_same_origin_uses_distinct_pooled_sockets():
    from concurrent.futures import ThreadPoolExecutor
    import opportunity_account_reader as module
    _clear_http_pool(module)
    barrier = threading.Barrier(2, timeout=2)

    def handle(conn, idx, _stop):
        if _read_http_request(conn) is not None:
            barrier.wait()
            body = f'{{"conn":{idx}}}'.encode()
            conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)

    port, state, stop = _start_raw_server(handle)
    try:
        url = f"http://127.0.0.1:{port}/concurrent"
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: module.http_json("GET", url), (1, 2)))
        assert sorted(r["conn"] for r in results) == [1, 2]
        assert state.accepts == 2
        assert len(module._HTTP_POOL[("http", "127.0.0.1", port, 3)]) == 2
    finally:
        _clear_http_pool(module)
        stop()


def test_http_json_non_empty_bad_status_line_does_not_retry():
    import opportunity_account_reader as module
    _clear_http_pool(module)

    def handle(conn, _idx, _stop):
        if _read_http_request(conn) is not None:
            body = b'{"ok":true}'
            conn.sendall(f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n\r\n".encode() + body)
        if _read_http_request(conn) is not None:
            conn.sendall(b"NOT-HTTP\r\n\r\n")
        conn.close()

    port, state, stop = _start_raw_server(handle)
    try:
        url = f"http://127.0.0.1:{port}/malformed"
        assert module.http_json("GET", url) == {"ok": True}
        with pytest.raises(AccountReadError, match="account read failed; private response omitted"):
            module.http_json("GET", url)
        assert state.accepts == 1
    finally:
        _clear_http_pool(module)
        stop()


def test_rate_limit_cooldown_survives_cache_invalidation_and_market_changes():
    now, calls = [1_000_000_000], []
    def transport(*args):
        calls.append(args)
        if len(calls) == 1:
            raise AccountReadError("rate limited", retry_after_secs=45)
        return {"code": 200, "accounts": [{"index": 42, "positions": [],
            "total_order_count": 0, "pending_order_count": 0, "total_isolated_order_count": 0}]}
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"}, transport=transport,
                                   clock_ns=lambda: now[0])
    with pytest.raises(AccountReadError):
        reader.read(market("LIGHTER", "BTC-PERP.LIGHTER"), None, None, now[0])
    reader.invalidate("LIGHTER")
    now[0] += 5_000_000_000
    with pytest.raises(AccountReadError) as caught:
        reader.read(market("LIGHTER", "ETH-PERP.LIGHTER"), None, None, now[0])
    assert caught.value.retry_after_secs == 40 and len(calls) == 1
    now[0] += 40_000_000_000
    snapshot = reader.read(market("LIGHTER", "ETH-PERP.LIGHTER"), None, None, now[0])
    assert snapshot.account_positions == () and len(calls) == 3
    assert "orderBookDetails" in calls[-1][1]


def test_aster_configured_filter_drops_flat_unconfigured_rows_but_keeps_nonzero_and_checks_integrity():
    def run_reader(*, extra_amt="0", extra_side="BOTH", extra_max=0, extra_lev="999", duplicate_extra=False):
        positions = [
            {"symbol": "ETHUSDT", "positionSide": "BOTH", "positionAmt": "0",
             "markPrice": "3000", "leverage": "5"},
            {"symbol": "UNCONFIGURED", "positionSide": extra_side, "positionAmt": extra_amt,
             "markPrice": "1", "leverage": extra_lev},
        ]
        if duplicate_extra:
            positions.append(dict(positions[1]))
        brackets = [
            {"symbol": "ETHUSDT", "brackets": [{"initialLeverage": 20, "notionalFloor": 0, "notionalCap": 10000}]},
            {"symbol": "UNCONFIGURED", "brackets": [{"initialLeverage": extra_max, "notionalFloor": 0, "notionalCap": 10000}]},
        ]
        def transport(method, url):
            if "/positionRisk?" in url:
                return positions
            if "/openOrders?" in url:
                return []
            return brackets
        reader = ReadOnlyAccountReader(
            environ={"ASTER_MAINNET_SIGNER_PRIVATE_KEY": "fixture",
                     "ASTER_MAINNET_SIGNER_ADDRESS": "0x" + "1" * 40,
                     "ASTER_MAINNET_USER_ADDRESS": "0x" + "2" * 40},
            transport=transport,
            aster_signer=lambda *args: "0x" + "a" * 130,
            clock_ns=lambda: 10_000,
            aster_instruments=("ETHUSDT-PERP.ASTER",),
        )
        return reader.read(market("ASTER", "ETHUSDT-PERP.ASTER"), D(2999), D(3001), 50)

    # Flat unconfigured row is dropped from account_positions and its invalid brackets are skipped.
    snap = run_reader()
    assert snap.account_positions == (("ETHUSDT-PERP.ASTER", D(0)),)
    assert snap.aster_leverage is not None
    assert [row.instrument_id for row in snap.aster_leverage.symbols] == ["ETHUSDT-PERP.ASTER"]

    # Non-zero unconfigured position is ALWAYS retained so LiveTradeState triggers position_mismatch.
    nonzero = run_reader(extra_amt="1.25")
    assert dict(nonzero.account_positions) == {
        "ETHUSDT-PERP.ASTER": D(0),
        "UNCONFIGURED-PERP.ASTER": D("1.25"),
    }

    # Malformed hedge-mode or duplicate unconfigured rows still fail closed.
    with pytest.raises(AccountReadError, match="ambiguous"):
        run_reader(extra_side="LONG")
    with pytest.raises(AccountReadError, match="duplicate"):
        run_reader(duplicate_extra=True)


def test_native_gateway_passes_configured_aster_instruments_to_reader():
    from opportunity_native_execution import NativeOrderGateway
    from test_opportunity_native_execution import FakeStrategy, MODEL
    markets = [
        market("ASTER", "BTCUSDT-PERP.ASTER"),
        market("ASTER", "ETHUSDT-PERP.ASTER"),
        market("HL", "BTC-USD-PERP.HYPERLIQUID"),
    ]
    gateway = NativeOrderGateway(
        FakeStrategy(), markets, lambda _: None, lambda _: None, lambda _: None, model=MODEL,
    )
    try:
        assert gateway.reader._aster_instruments == frozenset({"BTCUSDT-PERP.ASTER", "ETHUSDT-PERP.ASTER"})
        assert gateway.reader._aster_symbols == frozenset({"BTCUSDT", "ETHUSDT"})
    finally:
        gateway.close()

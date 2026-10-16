from decimal import Decimal as D
from pathlib import Path
import sys
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
        return [{"coin": "BTC", "oid": 123, "cloid": "0xabc"}, {"coin": "ETH", "oid": 99}]
    reader = ReadOnlyAccountReader(environ={"HYPERLIQUID_ACCOUNT_ADDRESS": "0x" + "1"*40}, transport=transport)
    result = reader.read(market("HL", "BTC-USD-PERP.HYPERLIQUID"), D("100"), D("101"), 50)
    assert result.authoritative and result.position_quantity == D("-.002")
    assert result.open_order_ids == ("venue:123",)
    assert result.account_positions == (("BTC-USD-PERP.HYPERLIQUID", D("-.002")),)
    assert ("ETH-USD-PERP.HYPERLIQUID", "venue:99") in result.account_open_orders
    assert len(calls) == 2 and {c[2]["type"] for c in calls} == {"clearinghouseState", "openOrders"}


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
        return {"code": 200, "accounts": [{"index": 42, "positions": [],
            "total_order_count": 0, "pending_order_count": 0, "total_isolated_order_count": 0}]}
    reader = ReadOnlyAccountReader(environ={"LIGHTER_ACCOUNT_INDEX": "42"}, transport=transport,
                                   clock_ns=lambda: 10_000_000)
    first = reader.read(market("LIGHTER", "BTC-USD-PERP.LIGHTER"), D(100), D(101), 50)
    second = reader.read(market("LIGHTER", "ETH-USD-PERP.LIGHTER"), D(100), D(101), 60)
    assert first.position_quantity == second.position_quantity == 0
    assert first.open_order_ids == second.open_order_ids == ()
    assert first.observed_ns == second.observed_ns == 50 and len(calls) == 1
    reader.invalidate("LIGHTER")
    third = reader.read(market("LIGHTER", "BTC-USD-PERP.LIGHTER"), D(100), D(101), 70)
    assert third.observed_ns == 70 and len(calls) == 2


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


@pytest.mark.parametrize("status,header,expected", [
    (429, "45", 45), (429, "Thu, 01 Jan 1970 00:02:25 GMT", 45),
    (429, "invalid", 10), (418, "", 120), (500, "45", 0),
])
def test_http_failure_preserves_retry_after_without_private_response(monkeypatch, status, header, expected):
    import opportunity_account_reader as module
    from urllib.error import HTTPError
    def fail(*args, **kwargs):
        raise HTTPError("https://private.invalid/secret", status, "private message", {"Retry-After": header}, None)
    monkeypatch.setattr(module, "build_opener", lambda *args: NS(open=fail))
    monkeypatch.setattr(module.time, "time", lambda: 100)
    with pytest.raises(AccountReadError) as caught:
        module.http_json("GET", "https://private.invalid/secret")
    assert caught.value.retry_after_secs == expected
    assert str(caught.value) == f"account read HTTP {status}; private response omitted"


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
    assert snapshot.account_positions == () and len(calls) == 2

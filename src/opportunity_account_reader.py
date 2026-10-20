"""Independent, read-only position/order evidence for native execution clients.

Importing this module does not read credentials or contact a venue. Only read()
performs requests; it never uses an order mutation endpoint. Errors deliberately
omit request URLs, response bodies and credentials.
"""
from __future__ import annotations

from opportunity_timing import span, timed

from decimal import Decimal
from fractions import Fraction
import json
import os
import threading
import time
from urllib.parse import quote, urlencode
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener
from email.utils import parsedate_to_datetime
from math import ceil

from opportunity_preflight import AccountSnapshot


class AccountReadError(RuntimeError):
    def __init__(self, message, *, retry_after_secs=0):
        super().__init__(message)
        self.retry_after_secs = retry_after_secs


def exact(value, name="account amount"):
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise AccountReadError(f"invalid {name}")
    try:
        result = Decimal(value)
    except Exception:
        raise AccountReadError(f"invalid {name}") from None
    if not result.is_finite():
        raise AccountReadError(f"invalid {name}")
    return result


def _rows(value, name):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise AccountReadError(f"incomplete {name} evidence")
    return value


def aster_signature(message, key, signer_address, environment):
    """Sign an exact parameter string using the native adapter's EIP-712 domain."""
    if environment not in {"mainnet", "testnet"}:
        raise AccountReadError("invalid Aster signing environment")
    try:
        from eth_account import Account
        from eth_account.messages import encode_typed_data
    except ImportError:
        raise AccountReadError("Aster account reads require the eth-account dependency") from None
    try:
        account = Account.from_key(key)
        if account.address.lower() != signer_address.lower():
            raise AccountReadError("Aster signer address does not match its key")
        typed = encode_typed_data(full_message={
            "types": {"EIP712Domain": [
                {"name": "name", "type": "string"}, {"name": "version", "type": "string"},
                {"name": "chainId", "type": "uint256"}, {"name": "verifyingContract", "type": "address"}],
                "Message": [{"name": "msg", "type": "string"}]},
            "primaryType": "Message", "domain": {"name": "AsterSignTransaction", "version": "1",
                "chainId": 1666 if environment == "mainnet" else 714,
                "verifyingContract": "0x" + "0" * 40}, "message": {"msg": message}})
        return "0x" + bytes(Account.sign_message(typed, private_key=key).signature).hex()
    except Exception:
        raise AccountReadError("Aster account read signing failed; credentials omitted") from None


def http_json(method, url, payload=None, *, timeout=10):
    """Fixed read-only HTTP transport; no redirects to another origin are accepted."""
    if method not in {"GET", "POST"}:
        raise AccountReadError("unsupported account read method")
    body = json.dumps(payload).encode() if payload is not None else None
    request = Request(url, data=body, method=method,
                      headers={"Content-Type": "application/json", "Accept": "application/json"})
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            raise AccountReadError("account read redirect refused")
    try:
        with build_opener(NoRedirect()).open(request, timeout=timeout) as response:
            if response.geturl() != url:
                raise AccountReadError("account read redirect refused")
            return json.loads(response.read(), parse_float=Decimal)
    except HTTPError as exc:
        retry = 0
        if exc.code in {429, 418}:
            raw = exc.headers.get("Retry-After", "").strip() if exc.headers else ""
            retry = 120 if exc.code == 418 else 10
            try:
                retry = max(1, int(raw) if raw.isdigit() else ceil(parsedate_to_datetime(raw).timestamp() - time.time()))
            except (ValueError, TypeError, OverflowError):
                pass
        raise AccountReadError(f"account read HTTP {exc.code}; private response omitted",
                               retry_after_secs=retry) from None
    except Exception:
        raise AccountReadError("account read failed; private response omitted") from None


class ReadOnlyAccountReader:
    def __init__(self, *, environment="mainnet", environ=None, transport=http_json,
                 clock_ns=time.time_ns, aster_signer=None, cache_ttl_ms=1000,
                 max_native_snapshot_age_ms=15000):
        if environment not in {"mainnet", "testnet"}:
            raise AccountReadError("environment must be mainnet or testnet")
        self.environment = environment
        self.environ = os.environ if environ is None else environ
        self.transport = transport
        self.clock_ns = clock_ns
        self.aster_signer = aster_signer
        self._nonce = 0
        self._nonce_lock = threading.Lock()
        self._cache = {}
        self._retry_after_ns = {}
        self._epochs = {}
        self._cache_lock = threading.RLock()
        self.cache_ttl_ns = cache_ttl_ms * 1_000_000
        if type(max_native_snapshot_age_ms) is not int or max_native_snapshot_age_ms <= 0:
            raise AccountReadError("native snapshot age must be a positive integer")
        self.max_native_snapshot_age_ns = max_native_snapshot_age_ms * 1_000_000
        self._native_evidence = {}
        self._native_instruments = {}
        self._native_runs = {}
        self._aster_leverage_cache = None

    def invalidate(self, venue):
        venue = "HL" if venue == "HYPERLIQUID" else venue
        with self._cache_lock:
            self._epochs[venue] = self._epochs.get(venue, 0) + 1
            self._native_evidence.pop(venue, None)
            for key in tuple(self._cache):
                if key[0] == venue:
                    self._cache.pop(key)

    @timed("account.capture_native", labels=lambda self, venue, *a, **kw: (venue, ""))
    def capture_native_evidence(self, venue, payload, *, native_environment=None):
        """Copy a native owner's complete account observation on its owner thread.

        The caller invokes the native control before this method; native handles
        must never enter the worker pool. This boundary only retains immutable
        Python values. Positions and orders must cover the entire account, not
        merely the configured symbol allowlist. A failed capture invalidates the
        previous observation, so malformed updates cannot leave old Flat evidence.
        """
        if venue not in {"ONDO", "BACKPACK"}:
            raise AccountReadError("unsupported native account evidence venue")
        with self._cache_lock:
            previous = self._native_evidence.pop(venue, None)
            try:
                if isinstance(payload, str):
                    payload = json.loads(payload, parse_float=Decimal)
                if not isinstance(payload, dict):
                    raise AccountReadError("native account evidence is missing")
                if native_environment is not None and native_environment != self.environment:
                    raise AccountReadError("native account source environment mismatch")
                if venue == "ONDO" and payload.get("mode") == "continuous":
                    payload = self._normalize_ondo(payload, native_environment)
                elif "environment" not in payload and native_environment is not None:
                    # Both native controls are bound to an explicit validated
                    # config. They need not persist a redundant environment
                    # string inside their account observation structures.
                    payload = {**payload, "environment": native_environment}
                if (type(payload.get("schema_version")) is not int or payload["schema_version"] != 1
                        or payload.get("venue") != venue
                        or payload.get("environment") != self.environment):
                    raise AccountReadError("native account evidence identity/environment mismatch")
                for name in ("complete", "connected"):
                    if payload.get(name) is not True:
                        raise AccountReadError("native account evidence coverage/connection is incomplete")
                started, completed, epoch = (payload.get(name) for name in (
                    "snapshot_started_ns", "snapshot_completed_ns", "epoch"))
                if (type(started) is not int or type(completed) is not int
                        or type(epoch) is not int or epoch < 0
                        or not 0 < started <= completed <= self.clock_ns()):
                    raise AccountReadError("native account evidence timestamp/epoch is invalid")
                if previous and (epoch < previous["epoch"] or (
                        epoch == previous["epoch"] and started < previous["started"])):
                    raise AccountReadError("native account evidence regressed")
                unknown = payload.get("unknown_submissions")
                if type(unknown) is not int or unknown < 0:
                    raise AccountReadError("native unknown order coverage is missing")
                positions = {}
                for row in _rows(payload.get("positions"), "native positions"):
                    instrument = self._native_instrument(venue, row.get("instrument_id"))
                    if instrument in positions:
                        raise AccountReadError("duplicate native position row")
                    positions[instrument] = exact(row.get("quantity"), "native signed position")
                orders, ids = [], set()
                for row in _rows(payload.get("open_orders"), "native open orders"):
                    instrument = self._native_instrument(venue, row.get("instrument_id"))
                    venue_id, client_id = row.get("venue_order_id"), row.get("client_order_id")
                    if not isinstance(venue_id, str) or not venue_id:
                        raise AccountReadError("native open order venue identity is missing")
                    if client_id is not None and (not isinstance(client_id, str) or not client_id):
                        raise AccountReadError("native open order client identity is invalid")
                    if venue_id in ids:
                        raise AccountReadError("duplicate native open order row")
                    ids.add(venue_id)
                    orders.append((instrument, client_id, venue_id))
                self._native_evidence[venue] = {
                    "epoch": epoch, "started": started, "completed": completed,
                    "positions": positions, "orders": tuple(orders), "unknown": unknown,
                }
                if "run_id" in payload:
                    self._native_runs[venue] = payload["run_id"]
            except AccountReadError:
                raise
            except Exception:
                raise AccountReadError("native account evidence validation failed; private data omitted") from None

    def _normalize_ondo(self, payload, native_environment):
        """Read the continuous factory's fenced observation, bound to its config.

        ProductionAuthority does not store its endpoint environment. The caller
        therefore binds the actual factory configuration explicitly rather than
        inferring a network from a position symbol or a payload's readiness flag.
        """
        if native_environment not in {"mainnet", "testnet"}:
            raise AccountReadError("Ondo native source requires its configured environment")
        if "environment" in payload and payload["environment"] != native_environment:
            raise AccountReadError("Ondo native source environment mismatch")
        run_id = payload.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise AccountReadError("Ondo native run binding is missing")
        if self._native_runs.get("ONDO", run_id) != run_id:
            raise AccountReadError("Ondo native run binding changed")
        latest, reconciled = (payload.get(name) for name in (
            "latest_activity_generation", "reconciled_activity_generation"))
        if (type(latest) is not int or type(reconciled) is not int or latest < 0
                or reconciled != latest or payload.get("snapshot_fresh") is not True):
            raise AccountReadError("Ondo native account evidence raced activity or lacks a fresh pass")
        positions = payload.get("position_quantities")
        if not isinstance(positions, dict):
            raise AccountReadError("Ondo native account position coverage is missing")
        return {"schema_version": payload.get("schema_version"), "venue": payload.get("venue"),
                "environment": native_environment, "epoch": 0, "run_id": run_id,
                "complete": payload.get("coverage_complete"),
                "connected": payload.get("connected_ready"),
                "snapshot_started_ns": payload.get("snapshot_started_ns"),
                "snapshot_completed_ns": payload.get("snapshot_completed_ns"),
                "positions": [{"instrument_id": instrument, "quantity": quantity}
                              for instrument, quantity in positions.items()],
                "open_orders": payload.get("open_orders"),
                "unknown_submissions": payload.get("unknown_submissions")}

    @staticmethod
    def _native_instrument(venue, value):
        if (not isinstance(value, str) or not value.endswith("." + venue) or value == "." + venue
                or len(value) > 120 or any(c.isspace() for c in value)):
            raise AccountReadError("native account instrument identity is missing or mismatched")
        return value

    def register_native_instrument(self, instrument_id, *, size_increment, price_increment,
                                   min_quantity, min_notional, multiplier=Decimal(1)):
        """Retain exact native metadata copied by the caller from the owner cache."""
        if not isinstance(instrument_id, str):
            raise AccountReadError("native instrument identity is missing")
        venue = instrument_id.rsplit(".", 1)[-1]
        if venue not in {"ONDO", "BACKPACK"}:
            raise AccountReadError("unsupported native instrument evidence venue")
        self._native_instrument(venue, instrument_id)
        values = {"size_increment": size_increment, "price_increment": price_increment,
                  "min_quantity": min_quantity, "min_notional": min_notional, "multiplier": multiplier}
        for name, value in values.items():
            number = exact(value, "native " + name)
            if number < 0 or (name not in {"min_quantity", "min_notional"} and number <= 0):
                raise AccountReadError("native instrument increments/minimums are invalid")
            values[name] = number
        with self._cache_lock:
            self._native_instruments[instrument_id] = values

    def read_native(self, market, bid, ask, observed_ns):
        """Read captured native facts without I/O, credentials or native handles."""
        venue = market.venue
        if venue not in {"ONDO", "BACKPACK"}:
            raise AccountReadError("unsupported native account evidence venue")
        self._native_instrument(venue, market.instrument_id)
        account_only = bid is None and ask is None
        if not account_only:
            bid, ask = exact(bid, "native bid"), exact(ask, "native ask")
        if ((not account_only and (bid <= 0 or ask <= bid)) or type(observed_ns) is not int
                or not 0 < observed_ns <= self.clock_ns()):
            raise AccountReadError("native market price/timestamp is invalid")
        increments = {}
        with self._cache_lock:
            registered = self._native_instruments.get(market.instrument_id, {})
        for name in ("size_increment", "price_increment", "multiplier", "quote_to_usd",
                     "min_quantity", "min_notional"):
            number = exact(registered.get(name, getattr(market, name, None)), "native " + name)
            if number < 0 or (name not in {"min_quantity", "min_notional"} and number <= 0):
                raise AccountReadError("native instrument increments/minimums are invalid")
            increments[name] = number
        if not account_only and any(Fraction(price) % Fraction(increments["price_increment"]) for price in (bid, ask)):
            raise AccountReadError("native market prices do not align with instrument price increment")
        with self._cache_lock:
            evidence = self._native_evidence.get(venue)
            if evidence is None:
                raise AccountReadError("native account evidence has not been captured")
            now_ns = self.clock_ns()
            if not 0 <= now_ns - evidence["started"] <= self.max_native_snapshot_age_ns:
                raise AccountReadError("native account evidence is stale")
            quantity = evidence["positions"].get(market.instrument_id, Decimal(0))
            if Fraction(quantity) % Fraction(increments["size_increment"]):
                raise AccountReadError("native position quantity does not align with instrument size increment")
            orders = tuple(client_id if client_id is not None else "venue:" + venue_id
                           for instrument, client_id, venue_id in evidence["orders"]
                           if instrument == market.instrument_id)
            # An unresolved account-wide submit has no reliable symbol identity.
            # It cannot certify any target market Flat or permit further exposure.
            return AccountSnapshot(venue, market.instrument_id, bid, ask,
                                   evidence["started"], quantity, orders,
                                   connected=True, authoritative=evidence["unknown"] == 0,
                                   book_observed_ns=None if account_only else observed_ns,
                                   account_positions=tuple(evidence["positions"].items()),
                                   account_open_orders=tuple((instrument, client_id or "venue:" + venue_id)
                                       for instrument, client_id, venue_id in evidence["orders"]))

    def _cached_read(self, scope, fetch, observed_ns):
        # Whole-account evidence serves every market, with its original start
        # fence preserved. Reusing it never makes an old observation newer.
        with self._cache_lock:
            now_ns = self.clock_ns()
            remaining_ns = self._retry_after_ns.get(scope[0], 0) - now_ns
            if remaining_ns > 0:
                raise AccountReadError("account read is waiting for venue cooldown",
                                       retry_after_secs=(remaining_ns + 999_999_999) // 1_000_000_000)
            epoch = self._epochs.get(scope[0], 0)
            previous = self._cache.get(scope)
            if previous and 0 <= now_ns - previous[0] < self.cache_ttl_ns:
                return previous[2], previous[1]
        try:
            with span("account.fetch", venue=scope[0]):
                payload = fetch()
        except AccountReadError as exc:
            if exc.retry_after_secs:
                with self._cache_lock:
                    self._retry_after_ns[scope[0]] = max(self._retry_after_ns.get(scope[0], 0),
                        self.clock_ns() + exc.retry_after_secs * 1_000_000_000)
            raise
        with self._cache_lock:
            if self._epochs.get(scope[0], 0) == epoch:
                self._cache[scope] = (now_ns, observed_ns, payload)
        return payload, observed_ns

    def _required(self, name):
        value = self.environ.get(name)
        if not isinstance(value, str) or not value.strip():
            raise AccountReadError(f"missing credential reference {name}")
        return value.strip()

    @timed("account.read", labels=lambda self, market, *a, **kw: (market.venue, ""))
    def read(self, market, bid, ask, observed_ns):
        venue = market.venue
        leverage, leverage_error = None, ""
        if venue in {"HL", "HYPERLIQUID"}:
            position, orders, started_ns, positions, all_orders = self._hyperliquid(market.instrument_id, observed_ns)
        elif venue == "LIGHTER":
            position, orders, started_ns, positions, all_orders = self._lighter(market.instrument_id, observed_ns)
        elif venue == "ASTER":
            position, orders, started_ns, positions, all_orders, leverage, leverage_error = self._aster(
                market.instrument_id, observed_ns)
        elif venue in {"ONDO", "BACKPACK"}:
            return self.read_native(market, bid, ask, observed_ns)
        else:
            raise AccountReadError(f"{venue} continuous execution/account evidence is unsupported")
        account_only = bid is None and ask is None
        return AccountSnapshot(venue, market.instrument_id,
                               None if account_only else exact(bid), None if account_only else exact(ask), started_ns,
                               position, tuple(orders), connected=True, authoritative=True,
                               book_observed_ns=None if account_only else observed_ns, account_positions=tuple(positions.items()),
                               account_open_orders=tuple(all_orders),
                               aster_leverage=leverage, aster_leverage_error=leverage_error)

    def _hyperliquid(self, instrument_id, observed_ns):
        prefix = "HYPERLIQUID_" if self.environment == "mainnet" else "HYPERLIQUID_TESTNET_"
        # The user must state the actual trading account, not the API agent address.
        address = self._required(prefix + "ACCOUNT_ADDRESS")
        if len(address) != 42 or not address.startswith("0x"):
            raise AccountReadError("invalid explicit Hyperliquid account address")
        try:
            int(address[2:], 16)
        except ValueError:
            raise AccountReadError("invalid explicit Hyperliquid account address") from None
        symbol = instrument_id.removesuffix(".HYPERLIQUID").removesuffix("-USD-PERP")
        dex = symbol.split(":", 1)[0] if ":" in symbol else ""
        url = ("https://api.hyperliquid.xyz/info" if self.environment == "mainnet"
               else "https://api.hyperliquid-testnet.xyz/info")
        common = {"user": address, **({"dex": dex} if dex else {})}
        (state, orders), started_ns = self._cached_read(("HL", url, address, dex), lambda: (
            self.transport("POST", url, {"type": "clearinghouseState", **common}),
            self.transport("POST", url, {"type": "openOrders", **common})), observed_ns)
        if not isinstance(state, dict) or "assetPositions" not in state:
            raise AccountReadError("incomplete Hyperliquid position evidence")
        position = Decimal(0)
        seen = False
        positions, all_orders = {}, []
        for row in _rows(state["assetPositions"], "Hyperliquid positions"):
            item = row.get("position")
            if not isinstance(item, dict) or not isinstance(item.get("coin"), str):
                raise AccountReadError("invalid Hyperliquid position row")
            amount = exact(item.get("szi"))
            instrument = item["coin"] + "-USD-PERP.HYPERLIQUID"
            if instrument in positions:
                raise AccountReadError("duplicate Hyperliquid position row")
            positions[instrument] = amount
            if item["coin"] == symbol:
                if seen:
                    raise AccountReadError("duplicate Hyperliquid position row")
                position, seen = amount, True
        ids = []
        for row in _rows(orders, "Hyperliquid open orders"):
            if not isinstance(row.get("coin"), str) or "oid" not in row:
                raise AccountReadError("invalid Hyperliquid open order row")
            all_orders.append((row["coin"] + "-USD-PERP.HYPERLIQUID", "venue:" + str(row["oid"])))
            if row["coin"] == symbol:
                # Venue IDs intentionally remain different from owned client IDs.
                ids.append("venue:" + str(row["oid"]))
        return position, ids, started_ns, positions, all_orders

    def _lighter(self, instrument_id, observed_ns):
        prefix = "LIGHTER_" if self.environment == "mainnet" else "LIGHTER_TESTNET_"
        index = self._required(prefix + "ACCOUNT_INDEX")
        if not index.isdigit():
            raise AccountReadError("invalid Lighter account index")
        base = ("https://mainnet.zklighter.elliot.ai" if self.environment == "mainnet"
                else "https://testnet.zklighter.elliot.ai")
        payload, started_ns = self._cached_read(("LIGHTER", base, index), lambda: self.transport(
            "GET", base + "/api/v1/account?" + urlencode({"by": "index", "value": index})), observed_ns)
        if not isinstance(payload, dict) or payload.get("code") != 200:
            raise AccountReadError("Lighter account read did not succeed")
        accounts = _rows(payload.get("accounts"), "Lighter accounts")
        if len(accounts) != 1 or str(accounts[0].get("account_index", accounts[0].get("index"))) != str(int(index)):
            raise AccountReadError("Lighter account identity/coverage mismatch")
        account = accounts[0]
        if "index" in account and str(account["index"]) != str(int(index)):
            raise AccountReadError("Lighter account identity mismatch")
        suffix = "-USD-PERP.LIGHTER" if instrument_id.endswith("-USD-PERP.LIGHTER") else "-PERP.LIGHTER"
        symbol = instrument_id.removesuffix(suffix)
        position, seen, ids = Decimal(0), False, []
        positions, all_orders = {}, []
        for row in _rows(account.get("positions"), "Lighter positions"):
            if not isinstance(row.get("symbol"), str) or "open_order_count" not in row:
                raise AccountReadError("incomplete Lighter position/order row")
            quantity = exact(row.get("position"))
            sign = row.get("sign")
            count = row["open_order_count"]
            if quantity < 0 or type(sign) is not int or sign not in {-1, 0, 1}:
                raise AccountReadError("invalid Lighter position direction")
            if type(count) is not int or count < 0 or (quantity > 0 and sign == 0):
                raise AccountReadError("invalid Lighter open order/position evidence")
            for name in ("pending_order_count", "position_tied_order_count"):
                extra = row.get(name)
                if type(extra) is not int or extra < 0:
                    raise AccountReadError("incomplete Lighter pending/conditional order evidence")
                count += extra
            instrument = row["symbol"] + suffix
            if instrument in positions:
                raise AccountReadError("duplicate Lighter position row")
            positions[instrument] = quantity.copy_negate() if sign < 0 else quantity
            if count:
                all_orders.append((instrument, f"unidentified:Lighter:{row['symbol']}:{count}"))
            if row["symbol"] == symbol:
                if seen:
                    raise AccountReadError("duplicate Lighter position row")
                position, seen = quantity.copy_negate() if sign < 0 else quantity, True
                if count:
                    ids.append(f"unidentified:Lighter:{symbol}:{count}")
        # The official API contains rows for markets previously touched by this
        # account. A never-touched market can be absent. A complete position list
        # and explicit global order counts establish whether that omission is flat.
        if not seen:
            counts = []
            for name in ("total_order_count", "total_isolated_order_count", "pending_order_count"):
                count = account.get(name)
                if type(count) is not int or count < 0:
                    raise AccountReadError("Lighter missing target requires complete global order counts")
                counts.append(count)
            if any(counts):
                ids.append("unidentified:Lighter:account-orders:" + str(sum(counts)))
        # Global counts can include orders not represented in position rows.
        for name in ("total_order_count", "total_isolated_order_count", "pending_order_count"):
            if name in account:
                count = account[name]
                if type(count) is not int or count < 0:
                    raise AccountReadError("invalid Lighter global order count")
                if count:
                    all_orders.append(("account.LIGHTER", f"unidentified:Lighter:{name}:{count}"))
        return position, ids, started_ns, positions, all_orders

    def _signed_aster_query(self, symbol=None):
        prefix = "ASTER_MAINNET_" if self.environment == "mainnet" else "ASTER_"
        key = self._required(prefix + "SIGNER_PRIVATE_KEY")
        user = self._required(prefix + "USER_ADDRESS")
        signer_address = self._required(prefix + "SIGNER_ADDRESS")
        with self._nonce_lock:
            self._nonce = max(self._nonce + 1, self.clock_ns() // 1000)
            nonce = self._nonce
        params = [("nonce", str(nonce)), ("user", user), ("signer", signer_address)]
        if symbol is not None:
            params.append(("symbol", symbol))
        message = "&".join(k + "=" + quote(v, safe="-_.!~*'()") for k, v in params)
        signature = (self.aster_signer or aster_signature)(message, key, signer_address, self.environment)
        return message + "&signature=" + signature

    def _aster(self, instrument_id, observed_ns):
        symbol = instrument_id.removesuffix(".ASTER").removesuffix("-PERP")
        base = "https://fapi.asterdex.com" if self.environment == "mainnet" else "https://fapi.asterdex-testnet.com"
        prefix = "ASTER_MAINNET_" if self.environment == "mainnet" else "ASTER_"
        scope = ("ASTER", base, self._required(prefix + "USER_ADDRESS"), self._required(prefix + "SIGNER_ADDRESS"))
        (positions, orders), started_ns = self._cached_read(scope, lambda: (
            self.transport("GET", base + "/fapi/v3/positionRisk?" + self._signed_aster_query()),
            self.transport("GET", base + "/fapi/v3/openOrders?" + self._signed_aster_query())), observed_ns)
        matching = [row for row in _rows(positions, "Aster positions") if row.get("symbol") == symbol]
        if len(matching) != 1 or matching[0].get("positionSide", "BOTH") != "BOTH":
            raise AccountReadError("Aster one-way target position coverage is missing or ambiguous")
        quantity = exact(matching[0].get("positionAmt"))
        ids, all_orders, all_positions = [], [], {}
        for row in positions:
            symbol_name = row.get("symbol")
            if not isinstance(symbol_name, str) or not symbol_name or row.get("positionSide", "BOTH") != "BOTH":
                raise AccountReadError("Aster one-way account coverage is missing or ambiguous")
            instrument = symbol_name + "-PERP.ASTER"
            if instrument in all_positions:
                raise AccountReadError("duplicate Aster account position")
            all_positions[instrument] = exact(row.get("positionAmt"))
        for row in _rows(orders, "Aster open orders"):
            if not isinstance(row.get("symbol"), str) or not isinstance(row.get("clientOrderId"), str) or not row["clientOrderId"]:
                raise AccountReadError("Aster open order identity mismatch")
            all_orders.append((row["symbol"] + "-PERP.ASTER", row["clientOrderId"]))
            if row["symbol"] == symbol:
                ids.append(row["clientOrderId"])
        # A missing leverage proof blocks entries but must not disable account
        # reconciliation or reduce-only cleanup. Never call the leverage setter.
        leverage, leverage_error = None, ""
        try:
            from opportunity_leverage import parse_leverage
            brackets, bracket_ns = self._cached_read((*scope, "leverage"), lambda:
                self.transport("GET", base + "/fapi/v3/leverageBrackets?" + self._signed_aster_query()),
                observed_ns)
            proof_ns = min(started_ns, bracket_ns)
            previous = self._aster_leverage_cache
            if (previous is not None and previous[0] is positions and previous[1] is brackets
                    and previous[2].started_ns == proof_ns):
                leverage = previous[2]
            else:
                leverage = parse_leverage(positions, brackets, proof_ns)
                self._aster_leverage_cache = (positions, brackets, leverage)
        except Exception:
            self._aster_leverage_cache = None
            leverage_error = "leverage_read_failed"
        return quantity, ids, started_ns, all_positions, all_orders, leverage, leverage_error

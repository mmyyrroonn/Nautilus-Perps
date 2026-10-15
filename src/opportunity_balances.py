"""Optional startup balance observations from native, reported AccountState events.

No credentials or network clients live here. Amounts retain the native adapter's
currency and semantics; reading a balance does not certify trading capacity.
"""
from __future__ import annotations

from decimal import Decimal
from fractions import Fraction


BALANCE_SEMANTICS = {
    "HL": "native combined perpetual/spot account balances; not all DEX collateral",
    "LIGHTER": "spot plus perpetual collateral; locked covers spot reservations only",
    "ASTER": "wallet balances; not a complete margin statement",
    "ONDO": "verified settlement account balance mapped by the native adapter",
    "BACKPACK": "wallet trading balances; not verified available margin",
}


class BalanceUnavailable(ValueError):
    """A fixed, non-private reason why a native observation cannot be used."""


class NativeBalanceReader:
    """Only native account reads; no order gateway or mutation methods."""

    def __init__(self, strategy, model=None):
        if model is None:
            from nautilus_trader import model
        self.strategy, self.model = strategy, model

    def _client(self, venue):
        return self.model.ClientId.from_str("HYPERLIQUID" if venue == "HL" else venue)

    def _account_id(self, venue):
        return self.model.AccountId.from_str(str(self._client(venue)) + "-001")

    def query_balance(self, venue):
        if venue == "ONDO":
            # Its reconciliation cycle publishes balances; QueryAccount is
            # not implemented by the Ondo execution client.
            return
        self.strategy.query_account(self._account_id(venue), client_id=self._client(venue))

    def read_balance(self, venue, now_ns, max_age_ms):
        return native_balance_snapshot(self.strategy.cache, venue, self._account_id(venue), now_ns, max_age_ms)


def native_balance_snapshot(cache, venue, account_id, now_ns, max_age_ms):
    """Read the latest reported event, never relabel older cache rows as fresh.

    Some native adapters have no venue timestamp (ts_event=0). Freshness uses the
    original local AccountState creation time, not the time this function is called.
    """
    account = cache.account(account_id)
    if account is None:
        raise BalanceUnavailable("account_not_observed")
    event = account.last_event
    if event is None or event.is_reported is not True:
        raise BalanceUnavailable("reported_balance_not_observed")
    if account.id != account_id or event.account_id != account_id:
        raise BalanceUnavailable("balance_account_mismatch")
    stamp = event.ts_init
    if type(stamp) is not int or stamp <= 0 or not 0 <= now_ns - stamp <= max_age_ms * 1_000_000:
        raise BalanceUnavailable("balance_stale_or_invalid_time")
    rows, seen = [], set()
    for balance in event.balances:
        currency = str(balance.currency)
        if not currency or currency in seen:
            raise BalanceUnavailable("invalid_balance_currency")
        amounts = {}
        for name in ("total", "free", "locked"):
            money = getattr(balance, name)
            value = money.as_decimal()
            if (str(money.currency) != currency or not isinstance(value, Decimal)
                    or not value.is_finite()):
                raise BalanceUnavailable("invalid_balance_amount")
            amounts[name] = value
        if Fraction(amounts["total"]) != Fraction(amounts["free"]) + Fraction(amounts["locked"]):
            raise BalanceUnavailable("inconsistent_balance_amounts")
        seen.add(currency)
        rows.append({"currency": currency, **{k: str(v) for k, v in amounts.items()}})
    # An explicit empty event is observable, but is not evidence of zero funds.
    return {"venue": venue, "source": "native_reported_account_state",
            "semantics": BALANCE_SEMANTICS[venue], "observed_ns": stamp,
            "event_ns": event.ts_event, "read_ns": now_ns,
            "balances": sorted(rows, key=lambda row: row["currency"])}


class StartupBalanceCheck:
    """Bounded read-only startup step, independent of BTC order self-tests."""

    def __init__(self, venues, *, enabled, timeout_secs, max_age_ms):
        self.enabled = enabled
        self.timeout_ns = timeout_secs * 1_000_000_000
        self.max_age_ms = max_age_ms
        self.started_ns = self.completed_ns = None
        self.result = "not_started" if enabled else "disabled"
        self.venues = {venue: {"result": "pending"} for venue in venues} if enabled else {}

    def tick(self, gateway, now_ns):
        if self.result in {"disabled", "passed", "failed", "interrupted"}:
            return
        if self.started_ns is None:
            self.started_ns, self.result = now_ns, "running"
            for venue, row in self.venues.items():
                try:
                    gateway.query_balance(venue)
                except Exception:
                    # Exceptions can contain signed URLs or private responses.
                    row.update(result="failed", reason="balance_query_failed")
        for venue, row in self.venues.items():
            if row["result"] != "pending":
                continue
            if now_ns - self.started_ns >= self.timeout_ns:
                row.update(result="failed", reason=row.get("reason", "balance_read_timeout"))
                continue
            try:
                snapshot = gateway.read_balance(venue, now_ns, self.max_age_ms)
            except BalanceUnavailable as exc:
                row["reason"] = str(exc)
            except Exception:
                row["reason"] = "balance_read_failed"
            else:
                row.clear()
                row.update(result="passed", **snapshot)
        if all(row["result"] != "pending" for row in self.venues.values()):
            self.result = "passed" if all(row["result"] == "passed" for row in self.venues.values()) else "failed"
            self.completed_ns = now_ns

    def document(self):
        return {"enabled": self.enabled, "result": self.result,
                "started_ns": self.started_ns, "completed_ns": self.completed_ns,
                "venues": {venue: dict(row) for venue, row in self.venues.items()}}

    def interrupt(self, now_ns):
        for row in self.venues.values():
            if row["result"] == "pending":
                row.update(result="failed", reason="balance_check_interrupted")
        self.result, self.completed_ns = "interrupted", now_ns

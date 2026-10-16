"""Session loss checks at flat account boundaries; never creates close orders."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from fractions import Fraction

from opportunity_balances import BalanceUnavailable
from opportunity_core import exact_decimal
from opportunity_scan import ScanConfigError, decimal_value, integer_value


USD_CURRENCIES = frozenset({"USD", "USDC", "USDT", "USD1"})


@dataclass(frozen=True)
class LossMonitorPolicy:
    enabled: bool = False
    max_loss_usd: Decimal = Decimal("200")
    interval_secs: int = 600
    read_timeout_secs: int = 60
    retry_interval_secs: int = 5

    def __post_init__(self):
        if type(self.enabled) is not bool:
            raise ScanConfigError("loss_monitor.enabled must be boolean")
        loss = decimal_value(self.max_loss_usd, "loss_monitor.max_loss_usd", positive=True)
        if loss > 200:
            raise ScanConfigError("loss_monitor.max_loss_usd cannot exceed 200")
        object.__setattr__(self, "max_loss_usd", loss)
        integer_value(self.interval_secs, "loss_monitor.interval_secs", 1, 86400)
        integer_value(self.read_timeout_secs, "loss_monitor.read_timeout_secs", 1, 300)
        integer_value(self.retry_interval_secs, "loss_monitor.retry_interval_secs", 1, 60)

    def document(self):
        return {"enabled": self.enabled, "max_loss_usd": str(self.max_loss_usd),
                "interval_secs": self.interval_secs, "read_timeout_secs": self.read_timeout_secs,
                "retry_interval_secs": self.retry_interval_secs,
                "basis": "session_start_flat_account_equity", "check_only_when_flat": True,
                "valuation": "USD/USDC/USDT/USD1 nominal USD parity",
                "action": "pause_entries_wait_flat_check_then_resume_or_exit"}


def flat_equity(snapshot, venue, now_ns, max_age_ms, activity_ns, required_currencies=()):
    """Value complete stablecoin rows only after independent flat-account proof.

    Unknown nonzero currencies and disappearing nonzero currencies are not zero
    equity. Do not value free margin or add unrealized PnL to a flat balance.
    """
    stamp = snapshot.get("observed_ns")
    if (snapshot.get("venue") != venue or type(stamp) is not int or stamp <= 0
            or stamp < activity_ns or stamp > now_ns
            or (max_age_ms is not None and now_ns - stamp > max_age_ms * 1_000_000)):
        raise BalanceUnavailable("equity_stale_or_before_activity")
    rows = snapshot.get("balances")
    if not isinstance(rows, list) or not rows:
        raise BalanceUnavailable("equity_balance_rows_missing")
    seen, amounts = set(), {}
    for row in rows:
        currency = row.get("currency")
        if not isinstance(currency, str) or not currency or currency in seen:
            raise BalanceUnavailable("equity_currency_invalid")
        seen.add(currency)
        values = {}
        for name in ("total", "free", "locked"):
            raw = row.get(name)
            if isinstance(raw, bool) or not isinstance(raw, (str, int, Decimal)):
                raise BalanceUnavailable("equity_amount_invalid")
            value = Decimal(raw)
            if not value.is_finite():
                raise BalanceUnavailable("equity_amount_invalid")
            values[name] = Fraction(value)
        if values["total"] != values["free"] + values["locked"]:
            raise BalanceUnavailable("equity_amounts_inconsistent")
        if currency not in USD_CURRENCIES and any(values.values()):
            raise BalanceUnavailable("equity_currency_not_valued")
        if currency in USD_CURRENCIES:
            amounts[currency] = values["total"]
    if not amounts or not set(required_currencies) <= amounts.keys():
        raise BalanceUnavailable("equity_currency_coverage_missing")
    return {"venue": venue, "observed_ns": stamp,
            "equity_usd": str(exact_decimal(sum(amounts.values(), Fraction(0)))),
            "balances": {currency: str(exact_decimal(value)) for currency, value in amounts.items()}}


class SessionLossMonitor:
    def __init__(self, policy, venues, max_age_ms):
        self.policy, self.venues, self.max_age_ms = policy, tuple(venues), max_age_ms
        self.status = "baseline_pending" if policy.enabled else "disabled"
        self.baseline_usd = self.current_usd = self.loss_usd = None
        self.baseline_ns = self.checked_ns = self.next_check_ns = None
        self.flat_since_ns = self.read_started_ns = None
        self.attempt_started_ns = None
        self.balance_epoch_ns = None
        self.invalidated_after_ns = 0
        self.collected_venues, self.queried_ns = {}, {}
        self.baseline_venues, self.latest_venues, self.reasons = {}, {}, {}
        self.required_currencies = {venue: set() for venue in venues}

    def seed_startup_balances(self, check):
        """Reuse only the validated balance precheck from this invocation."""
        if self.baseline_usd is not None or check.get("result") != "passed":
            return
        for venue in self.venues:
            snapshot = check["venues"][venue]
            try:
                self.collected_venues[venue] = flat_equity(
                    snapshot, venue, snapshot["read_ns"], self.max_age_ms, self.invalidated_after_ns)
            except BalanceUnavailable:
                # A successful raw read can still contain unvalued currencies.
                # Leave that venue pending for the normal baseline diagnostics.
                continue

    def invalidate(self, after_ns):
        self.invalidated_after_ns = max(self.invalidated_after_ns, after_ns)
        self.collected_venues.clear()
        self.queried_ns.clear()

    def due(self, now_ns):
        return (self.policy.enabled and self.status != "breached"
                and (self.next_check_ns is None or now_ns >= self.next_check_ns))

    def _pause(self, now_ns):
        self.status = "paused"
        self.next_check_ns = now_ns + self.policy.retry_interval_secs * 1_000_000_000

    def tick(self, gateway, live, now_ns, *, has_residual, account_after_ns=None):
        if not self.due(now_ns):
            return
        live.set_entry_pause("session_loss_check", now_ns)
        if self.status in {"baseline_pending", "ready", "paused"}:
            if self.status == "ready":
                self.flat_since_ns = None
                self.collected_venues.clear()
                self.queried_ns.clear()
            self.status = "waiting_flat"
            self.attempt_started_ns = now_ns
            self.read_started_ns = None
            self.reasons = {}
        if has_residual:
            # Entry receipts, normal exits and existing risk exits keep running.
            self.flat_since_ns = self.read_started_ns = None
            self.collected_venues.clear()
            self.queried_ns.clear()
            self.attempt_started_ns = now_ns
            self.status = "waiting_flat"
            return
        if self.flat_since_ns is None:
            self.flat_since_ns = account_after_ns if account_after_ns is not None else now_ns
        if now_ns - self.attempt_started_ns >= self.policy.read_timeout_secs * 1_000_000_000:
            self.reasons["timeout"] = "flat_equity_read_timeout"
            self._pause(now_ns)
            return
        if not live.flat_accounts_ready(now_ns, self.flat_since_ns):
            self.reasons = {"account": "complete_fresh_flat_accounts_required",
                            **getattr(live, "flat_account_problems", {})}
            return
        self.reasons.clear()
        if self.read_started_ns is None:
            self.read_started_ns, self.status = now_ns, "checking"
        for venue in self.venues:
            activity_ns = max(self.invalidated_after_ns, max(
                (stamp for instrument, stamp in live.fences.items()
                 if live.markets[instrument].venue == venue), default=0))
            row = self.collected_venues.get(venue)
            if row is not None and row["observed_ns"] >= activity_ns:
                continue
            self.collected_venues.pop(venue, None)
            if (venue not in self.queried_ns or now_ns - self.queried_ns[venue]
                    >= self.policy.retry_interval_secs * 1_000_000_000):
                try:
                    gateway.query_balance(venue)
                    self.queried_ns[venue] = now_ns
                except Exception:
                    self.reasons[venue] = "equity_query_failed"
                    self._pause(now_ns)
                    return
            try:
                read_since = getattr(gateway, "read_balance_since", None)
                if self.balance_epoch_ns is not None and callable(read_since):
                    boundary = max(activity_ns, self.balance_epoch_ns)
                    snapshot = read_since(venue, now_ns, boundary)
                    max_age_ms = None
                else:
                    boundary, max_age_ms = activity_ns, self.max_age_ms
                    snapshot = gateway.read_balance(venue, now_ns, max_age_ms)
                self.collected_venues[venue] = flat_equity(snapshot, venue, now_ns, max_age_ms,
                                                          boundary, self.required_currencies[venue])
                self.reasons.pop(venue, None)
            except BalanceUnavailable as exc:
                self.reasons[venue] = str(exc)
            except Exception:
                self.reasons[venue] = "equity_read_invalid"
        if len(self.collected_venues) != len(self.venues):
            return
        rows = dict(self.collected_venues)
        self.latest_venues = rows
        total = sum((Fraction(row["equity_usd"]) for row in rows.values()), Fraction(0))
        self.current_usd, self.checked_ns = str(exact_decimal(total)), now_ns
        for venue, row in rows.items():
            self.required_currencies[venue].update(k for k, value in row["balances"].items() if Decimal(value))
        if self.baseline_usd is None:
            self.baseline_usd, self.baseline_ns = self.current_usd, now_ns
            self.baseline_venues = rows
        loss = Fraction(self.baseline_usd) - total
        self.loss_usd = str(exact_decimal(loss))
        self.next_check_ns = now_ns + self.policy.interval_secs * 1_000_000_000
        self.status = "breached" if loss >= Fraction(self.policy.max_loss_usd) else "ready"
        if self.status == "ready":
            live.set_entry_pause(None, now_ns)

    def document(self):
        return {**self.policy.document(), "status": self.status,
                "baseline_equity_usd": self.baseline_usd, "current_equity_usd": self.current_usd,
                "loss_usd": self.loss_usd, "baseline_ns": self.baseline_ns,
                "checked_ns": self.checked_ns, "next_check_ns": self.next_check_ns,
                "baseline_venues": self.baseline_venues, "venues": self.latest_venues,
                "collected_venues": dict(self.collected_venues),
                "reasons": dict(self.reasons)}

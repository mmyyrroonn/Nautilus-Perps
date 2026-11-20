"""Read-only Lighter margin compatibility for new-risk admission."""
from dataclasses import dataclass


@dataclass(frozen=True)
class LighterMarginSnapshot:
    started_ns: int
    market_modes: dict[str, int]
    account_modes: dict[str, int]


def market_margin_modes(payload):
    if not isinstance(payload, dict) or payload.get("code") != 200:
        raise ValueError("Lighter market metadata unavailable")
    rows = payload.get("order_book_details")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Lighter market metadata incomplete")
    modes = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("Lighter market metadata incomplete")
        status = row.get("status")
        if status is not None and status != "active":
            continue
        symbol = row.get("symbol")
        mode = row.get("market_config", {}).get("market_margin_mode")
        if not isinstance(symbol, str) or not symbol or symbol in modes:
            raise ValueError("Lighter market identity ambiguous")
        if type(mode) is not int or mode not in {0, 1}:
            raise ValueError("Lighter market margin mode unsupported")
        modes[symbol] = mode
    if not modes:
        raise ValueError("Lighter market metadata incomplete")
    return modes


def account_margin_modes(rows):
    modes = {}
    for row in rows:
        symbol, mode = row.get("symbol"), row.get("margin_mode")
        if not isinstance(symbol, str) or symbol in modes:
            raise ValueError("Lighter account margin identity ambiguous")
        if type(mode) is not int or mode not in {0, 1}:
            raise ValueError("Lighter account margin mode unavailable")
        modes[symbol] = mode
    return modes


def margin_check(snapshot, instrument_id, now_ns, max_age_ns):
    if snapshot is None:
        return "lighter_margin_unavailable", {}
    age = now_ns - snapshot.started_ns
    if age < 0 or age > max_age_ns:
        return "lighter_margin_stale", {}
    symbol = instrument_id.removesuffix(".LIGHTER").removesuffix("-PERP").removesuffix("-USD")
    required = snapshot.market_modes.get(symbol)
    actual = snapshot.account_modes.get(symbol)
    evidence = {"market_margin_mode": required, "account_margin_mode": actual,
                "read_started_ns": snapshot.started_ns, "source": "lighter_market_and_account"}
    if required is None:
        return "lighter_market_margin_unavailable", evidence
    # An isolated-only market needs an explicit isolated account setting.
    # Absence of a setting does not authorize changing the account.
    if required == 1 and actual != 1:
        return "lighter_isolated_margin_required", evidence
    return None, evidence

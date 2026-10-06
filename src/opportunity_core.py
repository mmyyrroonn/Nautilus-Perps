"""Exact entry estimates and event-only evidence; no exchange or Nautilus imports.

Prices are cash per base unit; snapshot quantities are native instrument units.
All decisions use exact rational arithmetic constructed from finite Decimals.
Terminating cash amounts are exact; repeating display ratios use 50 significant
Decimal digits. An entry estimate does not establish round-trip profitability.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from decimal import MAX_EMAX, MIN_EMIN, Context, Decimal
from fractions import Fraction
from math import lcm
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class MarketMetadata:
    instrument_id: str
    size_increment: Decimal
    multiplier: Decimal = Decimal("1")
    taker_fee_bps: Decimal | None = None
    fee_source: str = "unknown"
    min_quantity: Decimal | None = None
    min_notional: Decimal | None = None


@dataclass(frozen=True)
class BookSnapshot:
    venue: str
    symbol: str
    bids: tuple[tuple[Decimal, Decimal], ...]
    asks: tuple[tuple[Decimal, Decimal], ...]
    ts_event_ns: int
    ts_received_ns: int
    valid: bool = True
    coverage_limit: int | None = None

    def __post_init__(self) -> None:
        # Copy caller-owned lists, including inner levels, into immutable evidence.
        object.__setattr__(self, "bids", tuple(tuple(level) for level in self.bids))
        object.__setattr__(self, "asks", tuple(tuple(level) for level in self.asks))


@dataclass(frozen=True)
class ScanSettings:
    target_notional: Decimal = Decimal("500")
    min_entry_edge_bps: Decimal = Decimal("0")
    reserve_bps: Decimal = Decimal("5")
    max_age_ms: int = 2000
    max_skew_ms: int = 500
    max_receive_age_ms: int = 2000


def _finite(value: Any) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _positive(value: Any) -> bool:
    return _finite(value) and value > 0


def _nonnegative(value: Any) -> bool:
    return _finite(value) and value >= 0


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def exact_decimal(value: Fraction) -> Decimal:
    """Convert a finite decimal exactly, independent of the caller's context."""
    denominator = value.denominator
    twos = fives = 0
    while denominator % 2 == 0:
        denominator //= 2
        twos += 1
    while denominator % 5 == 0:
        denominator //= 5
        fives += 1
    if denominator != 1:
        raise ValueError("normalization cannot be represented as an exact finite decimal")
    places = max(twos, fives)
    coefficient = value.numerator * 2 ** (places - twos) * 5 ** (places - fives)
    digits = tuple(int(char) for char in str(abs(coefficient)))
    return Decimal((int(coefficient < 0), digits, -places))


def _number(value: Fraction) -> str:
    """Serialize exact terminating values, or a 50-digit repeating ratio."""
    try:
        return format(exact_decimal(value), "f")
    except ValueError:
        pass
    # Display rounding must not inherit caller traps, precision, or exponent bounds.
    context = Context(prec=50, Emin=MIN_EMIN, Emax=MAX_EMAX, traps=[])
    return format(context.divide(Decimal(value.numerator), Decimal(value.denominator)), "f")


def _json_safe(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_safe(item) for item in value]
    return value


def _metadata_valid(metadata: MarketMetadata) -> bool:
    return (
        isinstance(metadata.instrument_id, str)
        and bool(metadata.instrument_id)
        and _positive(metadata.size_increment)
        and _positive(metadata.multiplier)
        and _nonnegative(metadata.taker_fee_bps)
        and isinstance(metadata.fee_source, str)
        and bool(metadata.fee_source.strip())
        and metadata.fee_source.strip().lower() != "unknown"
        and (metadata.min_quantity is None or _nonnegative(metadata.min_quantity))
        and (metadata.min_notional is None or _nonnegative(metadata.min_notional))
    )


def _book_valid(book: BookSnapshot, settings: ScanSettings, now_ns: int) -> bool:
    if not book.valid or not book.venue or not book.symbol or not book.bids or not book.asks:
        return False
    if book.coverage_limit is not None and (
        not _integer(book.coverage_limit)
        or book.coverage_limit <= 0
        or max(len(book.bids), len(book.asks)) > book.coverage_limit
    ):
        return False
    if not all(_integer(value) for value in (book.ts_event_ns, book.ts_received_ns)):
        return False
    if not 0 < book.ts_event_ns <= book.ts_received_ns <= now_ns:
        return False
    if now_ns - book.ts_event_ns > settings.max_age_ms * 1_000_000:
        return False
    if now_ns - book.ts_received_ns > settings.max_receive_age_ms * 1_000_000:
        return False
    if book.ts_received_ns - book.ts_event_ns > settings.max_age_ms * 1_000_000:
        return False
    for levels, descending in ((book.bids, True), (book.asks, False)):
        previous = None
        for level in levels:
            if len(level) != 2 or not all(_positive(value) for value in level):
                return False
            price = level[0]
            if previous is not None and (
                (descending and price >= previous) or (not descending and price <= previous)
            ):
                return False
            previous = price
    return book.bids[0][0] < book.asks[0][0]


def _common_step(first: Fraction, second: Fraction) -> Fraction:
    denominator = lcm(first.denominator, second.denominator)
    first_units = first.numerator * (denominator // first.denominator)
    second_units = second.numerator * (denominator // second.denominator)
    return Fraction(lcm(first_units, second_units), denominator)


def _budget_quantity(
    asks: tuple[tuple[Decimal, Decimal], ...], multiplier: Fraction, budget: Fraction,
) -> Fraction | None:
    remaining = budget
    quantity = Fraction(0)
    for price_decimal, native_size in asks:
        price = Fraction(price_decimal)
        available = Fraction(native_size) * multiplier
        cash = available * price
        if remaining <= cash:
            return quantity + remaining / price
        quantity += available
        remaining -= cash
    # A truncated book cannot establish the target-budget quantity.
    return None


def _walk(
    levels: tuple[tuple[Decimal, Decimal], ...], multiplier: Fraction, quantity: Fraction,
) -> Fraction | None:
    remaining = quantity
    cash = Fraction(0)
    for price, native_size in levels:
        filled = min(remaining, Fraction(native_size) * multiplier)
        cash += filled * Fraction(price)
        remaining -= filled
        if remaining == 0:
            return cash
    return None


def evaluate_opportunity(
    buy_book: BookSnapshot,
    buy_metadata: MarketMetadata,
    sell_book: BookSnapshot,
    sell_metadata: MarketMetadata,
    settings: ScanSettings,
    now_ns: int,
) -> dict[str, Any] | None:
    """Return one detached JSON-safe entry estimate, or fail closed.

    The BUY cash budget excludes fees. Both legs use one common base quantity;
    liquidity below that quantity never produces a partial qualifying estimate.
    Only the supplied current snapshots are retained in returned evidence.
    """
    if not _integer(now_ns) or not _positive(settings.target_notional):
        return None
    if not _finite(settings.min_entry_edge_bps) or not _nonnegative(settings.reserve_bps):
        return None
    if not all(
        _integer(value) and value >= 0
        for value in (settings.max_age_ms, settings.max_skew_ms, settings.max_receive_age_ms)
    ):
        return None
    if buy_book.venue == sell_book.venue or buy_book.symbol != sell_book.symbol:
        return None
    if not _metadata_valid(buy_metadata) or not _metadata_valid(sell_metadata):
        return None
    # No depth fill can improve on the best ask/bid, with nonnegative costs.
    # Reject this common case before walking and validating every captured level.
    if (settings.min_entry_edge_bps >= 0 and buy_book.asks and sell_book.bids
            and len(buy_book.asks[0]) == 2 and len(sell_book.bids[0]) == 2
            and _positive(buy_book.asks[0][0]) and _positive(sell_book.bids[0][0])
            and sell_book.bids[0][0] <= buy_book.asks[0][0]):
        return None
    if not _book_valid(buy_book, settings, now_ns) or not _book_valid(sell_book, settings, now_ns):
        return None
    if abs(buy_book.ts_event_ns - sell_book.ts_event_ns) > settings.max_skew_ms * 1_000_000:
        return None

    buy_multiplier = Fraction(buy_metadata.multiplier)
    sell_multiplier = Fraction(sell_metadata.multiplier)
    common_step = _common_step(
        Fraction(buy_metadata.size_increment) * buy_multiplier,
        Fraction(sell_metadata.size_increment) * sell_multiplier,
    )
    budget = Fraction(settings.target_notional)
    raw_quantity = _budget_quantity(buy_book.asks, buy_multiplier, budget)
    if raw_quantity is None:
        return None
    quantity = (raw_quantity // common_step) * common_step
    if quantity <= 0:
        return None
    buy_cash = _walk(buy_book.asks, buy_multiplier, quantity)
    sell_cash = _walk(sell_book.bids, sell_multiplier, quantity)
    if buy_cash is None or sell_cash is None or buy_cash > budget:
        return None
    buy_quantity = quantity / buy_multiplier
    sell_quantity = quantity / sell_multiplier
    for native_quantity, cash, metadata in (
        (buy_quantity, buy_cash, buy_metadata), (sell_quantity, sell_cash, sell_metadata),
    ):
        if metadata.min_quantity is not None and native_quantity < Fraction(metadata.min_quantity):
            return None
        if metadata.min_notional is not None and cash < Fraction(metadata.min_notional):
            return None

    gross = sell_cash - buy_cash
    buy_fee = buy_cash * Fraction(buy_metadata.taker_fee_bps) / 10_000
    sell_fee = sell_cash * Fraction(sell_metadata.taker_fee_bps) / 10_000
    reserve = buy_cash * Fraction(settings.reserve_bps) / 10_000
    net = gross - buy_fee - sell_fee - reserve
    threshold = Fraction(settings.min_entry_edge_bps)
    if net * 10_000 < threshold * buy_cash or (threshold == 0 and net <= 0):
        return None

    return {
        "schema_version": 1,
        "estimate_type": "entry_only",
        "full_round_trip_profitability": "unknown",
        "ratio_decimal_precision": 50,
        "evaluated_at_ns": now_ns,
        "symbol": buy_book.symbol,
        "buy_venue": buy_book.venue,
        "sell_venue": sell_book.venue,
        "settings": _json_safe(asdict(settings)),
        "sizing": {
            "base_quantity": _number(quantity),
            "common_base_step": _number(common_step),
            "buy_quantity": _number(buy_quantity),
            "sell_quantity": _number(sell_quantity),
            "target_notional": str(settings.target_notional),
            "buy_notional": _number(buy_cash),
            "sell_notional": _number(sell_cash),
            "buy_vwap": _number(buy_cash / quantity),
            "sell_vwap": _number(sell_cash / quantity),
        },
        "economics": {
            "entry_gross": _number(gross),
            "entry_gross_bps": _number(gross * 10_000 / buy_cash),
            "buy_taker_fee": _number(buy_fee),
            "sell_taker_fee": _number(sell_fee),
            "reserve": _number(reserve),
            "entry_after_fees_and_reserve": _number(net),
            "entry_after_fees_and_reserve_bps": _number(net * 10_000 / buy_cash),
        },
        "buy_book": _json_safe(asdict(buy_book)),
        "sell_book": _json_safe(asdict(sell_book)),
        "buy_metadata": _json_safe(asdict(buy_metadata)),
        "sell_metadata": _json_safe(asdict(sell_metadata)),
    }


@dataclass(frozen=True)
class _SavedEvent:
    saved_at_ns: int
    edge_bps: Fraction
    quantity: Fraction


class EventRecorder:
    """Lazily append qualifying events, retaining only active-key dedup stats.

    An onset saves immediately. Subsequent saves require cooldown plus a material
    change relative to the last saved event, or an optional maximum interval.
    A None observation resets that key without writing a disappearance event.
    Disabled output has no filesystem effects and retains no dedup state.
    """

    def __init__(
        self,
        output_path: Path | None,
        cooldown_ms: int = 5000,
        edge_change_bps: Decimal = Decimal("2"),
        quantity_change_fraction: Decimal = Decimal("0.1"),
        max_interval_ms: int | None = None,
    ) -> None:
        if not _integer(cooldown_ms) or cooldown_ms < 0:
            raise ValueError("cooldown_ms must be a nonnegative integer")
        if not _nonnegative(edge_change_bps) or not _nonnegative(quantity_change_fraction):
            raise ValueError("change thresholds must be finite nonnegative Decimals")
        if max_interval_ms is not None and (
            not _integer(max_interval_ms) or max_interval_ms <= 0 or max_interval_ms < cooldown_ms
        ):
            raise ValueError("max_interval_ms must be positive and at least cooldown_ms")
        self.output_path = Path(output_path) if output_path is not None else None
        self.cooldown_ns = cooldown_ms * 1_000_000
        self.edge_change_bps = Fraction(edge_change_bps)
        self.quantity_change_fraction = Fraction(quantity_change_fraction)
        self.max_interval_ns = None if max_interval_ms is None else max_interval_ms * 1_000_000
        self._active: dict[tuple[str, str, str], _SavedEvent] = {}

    def observe(
        self, key: tuple[str, str, str], opportunity: dict[str, Any] | None, now_ns: int,
    ) -> bool:
        """Return True only after a qualifying snapshot was appended successfully."""
        if opportunity is None:
            self._active.pop(key, None)
            return False
        if self.output_path is None:
            return False
        if not _integer(now_ns) or now_ns < 0:
            raise ValueError("now_ns must be a nonnegative integer")
        edge = Fraction(Decimal(opportunity["economics"]["entry_after_fees_and_reserve_bps"]))
        quantity = Fraction(Decimal(opportunity["sizing"]["base_quantity"]))
        if quantity <= 0:
            raise ValueError("opportunity base quantity must be positive")
        previous = self._active.get(key)
        if previous is not None:
            elapsed = now_ns - previous.saved_at_ns
            if elapsed < self.cooldown_ns:
                return False
            edge_delta = abs(edge - previous.edge_bps)
            quantity_delta = abs(quantity - previous.quantity)
            changed = (
                (edge_delta > 0 and edge_delta >= self.edge_change_bps)
                or (
                    quantity_delta > 0
                    and quantity_delta >= previous.quantity * self.quantity_change_fraction
                )
            )
            periodic = self.max_interval_ns is not None and elapsed >= self.max_interval_ns
            if not changed and not periodic:
                return False

        # Encode before creating a directory: bad evidence cannot leave an empty file.
        record = {"recorded_at_ns": now_ns, "key": list(key), "opportunity": opportunity}
        line = json.dumps(record, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
        self.output_path.parent.mkdir(parents=True, exist_ok=True)
        with self.output_path.open("a", encoding="utf-8", newline="\n") as output:
            output.write(line + "\n")
            output.flush()
        self._active[key] = _SavedEvent(now_ns, edge, quantity)
        return True

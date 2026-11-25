"""Hard-coded non-overridable safety limits for Route A Carry execution."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

HARD_MAX_SLOTS = 8
HARD_LEG_NOTIONAL_USD = Decimal("150")
HARD_SLICE_USD = Decimal("100")
HARD_MAX_VENUE_NOTIONAL_USD = Decimal("450")
HARD_GROSS_NOTIONAL_USD = Decimal("1600")
HARD_MAX_UNHEDGED_USD = Decimal("100")
HARD_MAX_UNHEDGED_SECS = 15
HARD_DAILY_LOSS_USD = Decimal("80")
HARD_MARGIN_GUARD_MAX_PCT = Decimal("30")
HARD_MIN_ENTRY_CARRY_BPS_H = Decimal("0.5")


class LimitValidationError(ValueError):
    """Raised when a configured limit is invalid or non-finite."""


def exact_decimal(value: object, name: str, *, positive: bool = False, nonnegative: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise LimitValidationError(f"{name} must be a string, int, or Decimal, got {type(value).__name__}")
    try:
        dec = Decimal(value)
    except InvalidOperation as exc:
        raise LimitValidationError(f"{name} is not a valid decimal: {value!r}") from exc
    if not dec.is_finite():
        raise LimitValidationError(f"{name} must be finite")
    if positive and dec <= 0:
        raise LimitValidationError(f"{name} must be > 0, got {dec}")
    if nonnegative and dec < 0:
        raise LimitValidationError(f"{name} must be >= 0, got {dec}")
    return dec


@dataclass(frozen=True)
class ClampedLimits:
    max_slots: int
    leg_notional_usd: Decimal
    slice_usd: Decimal
    max_venue_notional_usd: Decimal
    max_gross_notional_usd: Decimal
    max_unhedged_usd: Decimal
    max_unhedged_secs: int
    max_daily_loss_usd: Decimal
    margin_guard_pct: Decimal
    min_entry_carry_bps_h: Decimal


def clamp_limits(
    *,
    max_slots: int,
    leg_notional_usd: object,
    slice_usd: object,
    max_venue_notional_usd: object,
    max_gross_notional_usd: object,
    max_unhedged_usd: object,
    max_unhedged_secs: int,
    max_daily_loss_usd: object,
    margin_guard_pct: object,
    entry_carry_bps_h: object,
) -> ClampedLimits:
    """Apply hard module-level safety caps; configuration can only tighten limits."""
    if type(max_slots) is not int or max_slots < 1:
        raise LimitValidationError(f"max_slots must be positive int, got {max_slots!r}")
    if type(max_unhedged_secs) is not int or max_unhedged_secs < 1:
        raise LimitValidationError(f"max_unhedged_secs must be positive int, got {max_unhedged_secs!r}")

    leg = min(exact_decimal(leg_notional_usd, "leg_notional_usd", positive=True), HARD_LEG_NOTIONAL_USD)
    slc = min(exact_decimal(slice_usd, "slice_usd", positive=True), HARD_SLICE_USD, leg)
    venue_cap = min(exact_decimal(max_venue_notional_usd, "max_venue_notional_usd", positive=True), HARD_MAX_VENUE_NOTIONAL_USD)
    gross = min(exact_decimal(max_gross_notional_usd, "max_gross_notional_usd", positive=True), HARD_GROSS_NOTIONAL_USD)
    unhedged = min(exact_decimal(max_unhedged_usd, "max_unhedged_usd", positive=True), HARD_MAX_UNHEDGED_USD)
    daily_loss = min(exact_decimal(max_daily_loss_usd, "max_daily_loss_usd", positive=True), HARD_DAILY_LOSS_USD)
    margin_guard = min(exact_decimal(margin_guard_pct, "margin_guard_pct", positive=True), HARD_MARGIN_GUARD_MAX_PCT)
    entry_carry = max(exact_decimal(entry_carry_bps_h, "entry_carry_bps_h", positive=True), HARD_MIN_ENTRY_CARRY_BPS_H)

    return ClampedLimits(
        max_slots=min(max_slots, HARD_MAX_SLOTS),
        leg_notional_usd=leg,
        slice_usd=slc,
        max_venue_notional_usd=venue_cap,
        max_gross_notional_usd=gross,
        max_unhedged_usd=unhedged,
        max_unhedged_secs=min(max_unhedged_secs, HARD_MAX_UNHEDGED_SECS),
        max_daily_loss_usd=daily_loss,
        margin_guard_pct=margin_guard,
        min_entry_carry_bps_h=entry_carry,
    )

"""Read-only Aster leverage evidence and fail-closed new-risk admission."""
from dataclasses import dataclass, field
from decimal import Decimal
from fractions import Fraction as F
import re


@dataclass(frozen=True)
class AsterLeverage:
    instrument_id: str
    actual: int
    position_quantity: Decimal
    mark_price: Decimal
    # Exact quote-currency floors/caps and maximum initial leverage.
    brackets: tuple[tuple[Decimal, Decimal, int], ...]


@dataclass(frozen=True)
class AsterLeverageSnapshot:
    started_ns: int
    symbols: tuple[AsterLeverage, ...]
    _index: dict[str, AsterLeverage] = field(init=False, repr=False, compare=False)

    def __post_init__(self):
        index = {row.instrument_id: row for row in self.symbols}
        if len(index) != len(self.symbols):
            raise ValueError("duplicate Aster leverage instrument")
        object.__setattr__(self, "_index", index)

    def symbol(self, instrument_id):
        return self._index.get(instrument_id)


def leverage_integer(value):
    if type(value) is int and 1 <= value <= 125:
        return value
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,2}", value):
        return leverage_integer(int(value))
    raise ValueError("invalid Aster leverage")


def parse_leverage(positions, brackets, started_ns):
    """Discard private response fields and reject ambiguous tier coverage."""
    from opportunity_account_reader import exact, _rows
    tiers = {}
    for row in _rows(brackets, "Aster leverage brackets"):
        symbol = row.get("symbol")
        if not isinstance(symbol, str) or not symbol or symbol in tiers:
            raise ValueError("invalid Aster leverage symbol")
        values = []
        for tier in _rows(row.get("brackets"), "Aster leverage tiers"):
            floor, cap = exact(tier.get("notionalFloor")), exact(tier.get("notionalCap"))
            maximum = leverage_integer(tier.get("initialLeverage"))
            if floor < 0 or cap <= floor:
                raise ValueError("invalid Aster leverage tier")
            values.append((floor, cap, maximum))
        values.sort()
        if (not values or values[0][0] != 0
                or any(left[1] != right[0] or left[2] < right[2]
                       for left, right in zip(values, values[1:]))):
            raise ValueError("incomplete Aster leverage tiers")
        tiers[symbol] = tuple(values)
    result, seen = [], set()
    for row in _rows(positions, "Aster leverage positions"):
        symbol = row.get("symbol")
        if (not isinstance(symbol, str) or not symbol or symbol in seen
                or row.get("positionSide") != "BOTH"):
            raise ValueError("ambiguous Aster leverage position")
        seen.add(symbol)
        # Missing symbols remain explicitly unavailable at admission.
        if symbol not in tiers:
            continue
        mark = exact(row.get("markPrice"))
        if mark <= 0:
            raise ValueError("invalid Aster mark price")
        result.append(AsterLeverage(symbol + "-PERP.ASTER", leverage_integer(row.get("leverage")),
            exact(row.get("positionAmt")), mark, tiers[symbol]))
    if type(started_ns) is not int or started_ns <= 0:
        raise ValueError("invalid Aster leverage timestamp")
    return AsterLeverageSnapshot(started_ns, tuple(result))


def leverage_check(snapshot, instrument_id, target, price, quantity, multiplier, now_ns,
                   max_age_ns, rejected_ns=0):
    """Conservatively add entry notional to existing absolute exposure."""
    evidence = {"target": target, "checked_ns": now_ns}
    if snapshot is None:
        return "leverage_unavailable", evidence
    evidence["observed_ns"] = snapshot.started_ns
    if not 0 <= now_ns - snapshot.started_ns <= max_age_ns:
        return "leverage_stale", evidence
    if snapshot.started_ns <= rejected_ns:
        return "leverage_recheck_required", evidence
    row = snapshot.symbol(instrument_id)
    if row is None:
        return "leverage_symbol_missing", evidence
    if target is None:
        return "leverage_target_pending", evidence
    valuation = max(F(price), F(row.mark_price))
    projected = (abs(F(row.position_quantity)) + F(quantity)) * F(multiplier) * valuation
    tiers = [tier for tier in row.brackets if F(tier[0]) <= projected < F(tier[1])]
    evidence.update(actual=row.actual, projected_notional=str(projected))
    if len(tiers) != 1:
        return "leverage_notional_unsupported", evidence
    maximum = tiers[0][2]
    evidence["allowed_maximum"] = maximum
    if max(row.actual, target) > maximum:
        return "leverage_above_maximum", evidence
    if row.actual != target:
        return "leverage_target_mismatch", evidence
    return None, evidence

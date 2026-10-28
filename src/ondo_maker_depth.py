"""Bounded full-snapshot depth queries; exact quantities and no transport or I/O."""

from bisect import bisect_left, bisect_right
from decimal import Decimal

ZERO = Decimal(0)
BPS = Decimal(10_000)


class DepthSide:
    __slots__ = ("prices", "prefix", "side")

    def __init__(self, levels, side: int, limit: int):
        self.side = side
        self.prices = []
        self.prefix = [ZERO]
        if not levels or len(levels) > limit:
            raise ValueError("missing or oversized depth side")
        for price, quantity in levels:
            if (
                not price.is_finite() or not quantity.is_finite()
                or price <= 0 or quantity <= 0
            ):
                raise ValueError("invalid depth level")
            key = -side * price  # Increasing keys on both sides.
            if self.prices and key <= self.prices[-1]:
                raise ValueError("unordered or duplicate depth level")
            self.prices.append(key)
            self.prefix.append(self.prefix[-1] + quantity)

    def quantity(self, lower: Decimal, upper: Decimal, *, exclude_price=None):
        """None means the visible snapshot cannot prove coverage of this interval."""
        far = -self.side * self.prices[-1]
        if (self.side == 1 and far >= lower) or (self.side == -1 and far <= upper):
            return None
        low, high = (-upper, -lower) if self.side == 1 else (lower, upper)
        left = bisect_left(self.prices, low)
        right = bisect_right(self.prices, high)
        total = self.prefix[right] - self.prefix[left]
        if exclude_price is not None:
            total -= self.at(exclude_price)
        return total

    def at(self, price: Decimal) -> Decimal:
        key = -self.side * price
        index = bisect_left(self.prices, key)
        if index == len(self.prices) or self.prices[index] != key:
            return ZERO
        return self.prefix[index + 1] - self.prefix[index]


class DepthBook:
    __slots__ = ("sides", "updated_ns", "generation", "limit")

    def __init__(self, limit: int = 1000):
        self.limit = limit
        self.sides = {}
        self.updated_ns = None
        self.generation = 0

    def invalidate(self):
        self.sides.clear()
        self.updated_ns = None
        self.generation += 1

    def update(self, now: int, bids, asks) -> bool:
        if self.updated_ns is not None and now < self.updated_ns:
            self.invalidate()
            return False
        try:
            bid = DepthSide(bids, 1, self.limit)
            ask = DepthSide(asks, -1, self.limit)
            if -bid.prices[0] >= ask.prices[0]:
                raise ValueError("crossed depth")
        except (ValueError, ArithmeticError):
            self.invalidate()
            return False
        self.sides = {1: bid, -1: ask}
        self.updated_ns = now
        self.generation += 1
        return True

    def fresh(self, now: int, stale_ns: int) -> bool:
        return self.updated_ns is not None and 0 <= now - self.updated_ns <= stale_ns

    def ahead(self, side: int, price: Decimal, window_bps: Decimal):
        book = self.sides.get(side)
        if book is None:
            return None
        edge = price * (1 + side * window_bps / BPS)
        lower, upper = (price, edge) if side == 1 else (edge, price)
        # Same-price L2 quantity is not proof of queue priority, so exclude it all.
        return book.quantity(lower, upper, exclude_price=price)

    def nearby(self, side: int, price: Decimal, window_bps: Decimal, remaining: Decimal):
        book = self.sides.get(side)
        if book is None:
            return None
        value = book.quantity(price * (1 - window_bps / BPS), price * (1 + window_bps / BPS))
        if value is None:
            return None
        # Remove at most our remaining quantity actually visible at this price.
        return value - min(remaining, book.at(price))

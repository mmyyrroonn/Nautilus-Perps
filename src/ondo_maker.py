"""Cancel-first BTC/ETH reward making, independent of transport and wall-clock time.

All callbacks are serialized by the host event loop. Commands reserve their local
state before dispatch; a timeout never releases a reservation. Exchange terminal
events, cumulative fills and a current account proof authorize the next cycle.
"""

from __future__ import annotations

import math
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal

ZERO = Decimal(0)
BPS = Decimal(10_000)
SECOND = 1_000_000_000
MARKETS = ("BTC", "ETH")
# The native config requires a positive u32. Its maximum disables any practical
# application request cap; bounded active orders and reconciliation gate creates.
NATIVE_UNPACED_REQUEST_BOUND = (1 << 32) - 1


@dataclass(frozen=True, slots=True)
class MakerConfig:
    btc_notional_usd: Decimal
    eth_notional_usd: Decimal
    quote_bps: Decimal = Decimal(5)
    cancel_bps: Decimal = Decimal(3)
    volatility_bps: float = 2.0
    cooldown_ns: int = 5 * SECOND
    ondo_stale_ns: int = 2 * SECOND
    binance_stale_ns: int = SECOND
    retry_ns: int = 250_000_000
    cancel_retry_ns: int = SECOND
    max_gross_usd: Decimal = Decimal(400)
    max_order_usd: Decimal = Decimal(100)
    close_slippage_bps: Decimal = Decimal(20)
    pause_both: bool = False

    def __post_init__(self) -> None:
        decimals = (
            self.btc_notional_usd,
            self.eth_notional_usd,
            self.quote_bps,
            self.cancel_bps,
            self.max_gross_usd,
            self.max_order_usd,
            self.close_slippage_bps,
        )
        if any(
            not isinstance(x, Decimal) or not x.is_finite() or x <= 0 for x in decimals
        ):
            raise ValueError(
                "amounts and price parameters must be positive finite Decimals"
            )
        if not self.cancel_bps < self.quote_bps < BPS:
            raise ValueError("require 0 < cancel_bps < quote_bps < 10000")
        if not self.close_slippage_bps <= 20:
            raise ValueError("close price protection must be within 20 bps")
        if not (
            max(self.btc_notional_usd, self.eth_notional_usd)
            <= self.max_order_usd
            <= 500
            and self.max_order_usd <= self.max_gross_usd <= 2000
        ):
            raise ValueError("amount exceeds existing native order/gross limits")
        if 2 * (self.btc_notional_usd + self.eth_notional_usd) > self.max_gross_usd:
            raise ValueError("four maker orders exceed allocated gross budget")
        if not math.isfinite(self.volatility_bps) or self.volatility_bps <= 0:
            raise ValueError("volatility threshold must be positive and finite")
        for value in (
            self.cooldown_ns,
            self.ondo_stale_ns,
            self.binance_stale_ns,
            self.retry_ns,
            self.cancel_retry_ns,
        ):
            if type(value) is not int or value <= 0:
                raise ValueError("durations must be positive integer nanoseconds")

    def notional(self, market: str) -> Decimal:
        return self.btc_notional_usd if market == "BTC" else self.eth_notional_usd


class VolatilityGuard:
    """Amortized O(1) step-price range, including the price at the window start.

    Quantity-only updates refresh feed health without extending a pause. Overflow
    discards the entire history and makes the guard unavailable, never truncating
    a valid window while pretending it is complete.
    """

    def __init__(
        self,
        threshold: float = 2.0,
        cooldown_ns: int = 5 * SECOND,
        capacity: int = 32_768,
    ) -> None:
        self.threshold = threshold
        self.cooldown_ns = cooldown_ns
        self.capacity = capacity
        self.points: deque[tuple[int, float]] = deque()
        self.minimum: deque[tuple[int, float]] = deque()
        self.maximum: deque[tuple[int, float]] = deque()
        self.started_ns: int | None = None
        self.last_ns: int | None = None
        self.mid: float | None = None
        self.pause_until = 0
        self.range_bps = 0.0

    def reset(self) -> None:
        self.points.clear()
        self.minimum.clear()
        self.maximum.clear()
        self.started_ns = self.last_ns = self.mid = None
        self.range_bps = 0.0

    def update(self, now: int, bid: float, ask: float) -> bool:
        if (
            not math.isfinite(bid)
            or not math.isfinite(ask)
            or bid <= 0
            or ask < bid
            or (self.last_ns is not None and now < self.last_ns)
        ):
            self.reset()
            return False
        mid = (bid + ask) / 2
        if not math.isfinite(mid):
            self.reset()
            return False
        if self.last_ns is not None and now - self.last_ns > SECOND:
            self.reset()
        if self.started_ns is None:
            self.started_ns = now
        self.last_ns = now
        if mid == self.mid:
            return False
        self.mid = mid
        boundary = now - SECOND
        while len(self.points) > 1 and self.points[1][0] <= boundary:
            old = self.points.popleft()
            if self.minimum and self.minimum[0] == old:
                self.minimum.popleft()
            if self.maximum and self.maximum[0] == old:
                self.maximum.popleft()
        if len(self.points) >= self.capacity:
            self.reset()
            return False
        point = (now, mid)
        self.points.append(point)
        while self.minimum and self.minimum[-1][1] >= mid:
            self.minimum.pop()
        while self.maximum and self.maximum[-1][1] <= mid:
            self.maximum.pop()
        self.minimum.append(point)
        self.maximum.append(point)
        low, high = self.minimum[0][1], self.maximum[0][1]
        self.range_bps = (high - low) / low * 10_000
        # Cross multiplication avoids division error at the inclusive 2 bp boundary.
        trigger = (
            now - self.started_ns >= SECOND
            and high - low >= low * self.threshold / 10_000 - math.ulp(high) * 2
        )
        if trigger:
            self.pause_until = max(self.pause_until, now + self.cooldown_ns)
        return trigger

    def ready(self, now: int, stale_ns: int) -> bool:
        return bool(
            self.started_ns is not None
            and self.last_ns is not None
            and now - self.started_ns >= SECOND
            and 0 <= now - self.last_ns <= stale_ns
        )


@dataclass(frozen=True, slots=True)
class Instrument:
    tick: Decimal
    step: Decimal
    minimum_quantity: Decimal
    minimum_notional: Decimal = ZERO


@dataclass(slots=True)
class Order:
    client_id: str
    market: str
    side: int
    quantity: Decimal
    price: Decimal
    reduce_only: bool
    filled: Decimal = ZERO
    terminal: bool = False
    cancel_requested: bool = False
    last_cancel_ns: int = -1


@dataclass(frozen=True, slots=True)
class Command:
    kind: str
    order: Order


@dataclass(slots=True)
class Market:
    guard: VolatilityGuard
    instrument: Instrument | None = None
    bid: Decimal = ZERO
    ask: Decimal = ZERO
    quote_ns: int | None = None
    position: Decimal = ZERO
    orders: dict[str, Order] = field(default_factory=dict)
    replacing: bool = False
    retry_at: int = 0
    reason: str = "starting"


class MakerEngine:
    """Bounded order state; cancel commands always precede reductions and new risk."""

    def __init__(self, config: MakerConfig, run_id: str) -> None:
        if not run_id or len(run_id) > 24 or not run_id.isalnum():
            raise ValueError("run_id must contain 1..24 alphanumeric characters")
        self.config = config
        self.run_id = run_id
        self.markets = {
            m: Market(VolatilityGuard(config.volatility_bps, config.cooldown_ns))
            for m in MARKETS
        }
        self.history: OrderedDict[str, Order] = OrderedDict()
        self.sequence = 0
        self.native_ready = False
        self.account_healthy = False
        self.reduction_ready = False
        self.account_flat = False
        self.stopping = False
        self.fault: str | None = None
        self.reducing = False
        self.activity = 0
        self.proven_activity = -1

    def disconnect(self, venue: str, market: str | None = None) -> None:
        for symbol in MARKETS if market is None else (market,):
            state = self.markets[symbol]
            if venue == "binance":
                state.guard.reset()
            else:
                state.quote_ns = None
            state.replacing = True

    def on_binance(self, market: str, now: int, bid: float, ask: float) -> None:
        state = self.markets[market]
        if state.guard.update(now, bid, ask):
            for symbol in MARKETS if self.config.pause_both else (market,):
                other = self.markets[symbol]
                other.guard.pause_until = max(
                    other.guard.pause_until, now + self.config.cooldown_ns
                )
                other.replacing = True

    def on_ondo(self, market: str, now: int, bid: Decimal, ask: Decimal) -> None:
        state = self.markets[market]
        if not bid.is_finite() or not ask.is_finite() or bid <= 0 or ask < bid:
            self.disconnect("ondo", market)
            return
        state.bid, state.ask, state.quote_ns = bid, ask, now

    def set_account(
        self,
        *,
        ready: bool,
        reduction_ready: bool,
        flat: bool,
        healthy: bool | None = None,
    ) -> None:
        self.native_ready, self.reduction_ready, self.account_flat = (
            ready,
            reduction_ready,
            flat,
        )
        self.account_healthy = ready if healthy is None else healthy
        if flat and not any(s.position for s in self.markets.values()):
            self.proven_activity = self.activity
            if not any(s.orders for s in self.markets.values()):
                self.reducing = False

    def stop(self) -> None:
        self.stopping = True

    def fail(self, reason: str) -> None:
        self.fault = reason
        self.stopping = True

    @property
    def can_stop(self) -> bool:
        return bool(
            self.stopping
            and self.account_flat
            and self.proven_activity == self.activity
            and not any(s.position or s.orders for s in self.markets.values())
        )

    def update_order(
        self,
        client_id: str,
        *,
        filled: Decimal,
        terminal: bool,
        now: int,
        rejected: bool = False,
    ) -> None:
        order = next(
            (
                s.orders[client_id]
                for s in self.markets.values()
                if client_id in s.orders
            ),
            self.history.get(client_id),
        )
        if order is None:
            self.fail("unrecognized_order_event")
            return
        if not filled.is_finite() or filled < order.filled or filled > order.quantity:
            self.fail("inconsistent_cumulative_fill")
            return
        state = self.markets[order.market]
        delta = filled - order.filled
        if delta:
            state.position += order.side * delta
            order.filled = filled
            self.reducing = True
            self.account_flat = False
            self.activity += 1
            for other in self.markets.values():
                other.replacing = True
        if terminal and not order.terminal:
            order.terminal = True
            state.orders.pop(client_id, None)
            self.history[client_id] = order
            if len(self.history) > 4096:
                self.history.popitem(last=False)
            state.retry_at = (
                max(state.retry_at, now + self.config.retry_ns) if rejected else now
            )
            self.activity += 1
            self.account_flat = False
            if not order.reduce_only:
                state.replacing = True

    def _new(
        self,
        market: str,
        side: int,
        quantity: Decimal,
        price: Decimal,
        reduce_only: bool,
    ) -> Command:
        self.sequence += 1
        order = Order(
            f"{self.run_id}-{self.sequence}", market, side, quantity, price, reduce_only
        )
        self.markets[market].orders[order.client_id] = order
        self.activity += 1
        self.account_flat = False
        return Command("create", order)

    def _fresh(self, state: Market, now: int) -> bool:
        return (
            state.quote_ns is not None
            and 0 <= now - state.quote_ns <= self.config.ondo_stale_ns
        )

    def _reason(self, state: Market, now: int) -> str:
        if self.stopping:
            return self.fault or "stopping"
        if self.reducing or any(s.position for s in self.markets.values()):
            return "flattening"
        if not self.account_healthy:
            return "account_unavailable"
        if not self._fresh(state, now):
            return "ondo_unavailable"
        if not state.guard.ready(now, self.config.binance_stale_ns):
            return "binance_unavailable"
        if now < state.guard.pause_until:
            return "volatility_pause"
        mid = (state.bid + state.ask) / 2
        for order in state.orders.values():
            if not order.reduce_only and (
                (mid - order.price) * order.side * BPS <= mid * self.config.cancel_bps
            ):
                return "quote_too_close"
        return "quoting"

    def step(self, now: int) -> list[Command]:
        commands: list[Command] = []
        # Phase 1 covers both markets before any close is allowed to be dispatched.
        for state in self.markets.values():
            state.reason = self._reason(state, now)
            if state.reason != "quoting":
                state.replacing = True
            for order in state.orders.values():
                if order.reduce_only or not state.replacing:
                    continue
                if (
                    order.last_cancel_ns < 0
                    or now - order.last_cancel_ns >= self.config.cancel_retry_ns
                ):
                    order.cancel_requested = True
                    order.last_cancel_ns = now
                    commands.append(Command("cancel", order))

        # Phase 2 does not wait for cancel ACKs. Reduce-only prevents reversal while
        # late maker fills remain tracked and cannot enable another quote cycle.
        for symbol, state in self.markets.items():
            if not state.position or not self.reduction_ready or now < state.retry_at:
                continue
            if any(o.reduce_only for o in state.orders.values()) or not self._fresh(
                state, now
            ):
                continue
            spec = state.instrument
            if spec is None:
                continue
            side = -1 if state.position > 0 else 1
            touch = state.ask if side > 0 else state.bid
            price = touch * (1 + side * self.config.close_slippage_bps / BPS)
            price = (price / spec.tick).to_integral_value(
                rounding=ROUND_FLOOR if side > 0 else ROUND_CEILING
            ) * spec.tick
            cap = self.config.max_order_usd / max(price, state.ask)
            qty = (min(abs(state.position), cap) / spec.step).to_integral_value(
                rounding=ROUND_FLOOR
            ) * spec.step
            if qty > 0:
                commands.append(self._new(symbol, side, qty, price, True))
                state.retry_at = now + self.config.retry_ns

        # Phase 3 requires a fresh flat proof after every prior cycle. It never
        # treats an accepted cancellation or an expired cooldown as that proof.
        if (
            self.stopping
            or self.reducing
            or not self.native_ready
            or any(s.position for s in self.markets.values())
        ):
            return commands
        flat_proven = self.account_flat and self.proven_activity == self.activity
        for symbol, state in self.markets.items():
            if state.reason != "quoting" or state.orders or now < state.retry_at:
                continue
            if state.replacing and not flat_proven:
                continue
            spec = state.instrument
            if spec is None:
                continue
            state.replacing = False
            mid = (state.bid + state.ask) / 2
            quotes = []
            for side in (1, -1):
                price = mid * (1 - side * self.config.quote_bps / BPS)
                price = (price / spec.tick).to_integral_value(
                    rounding=ROUND_FLOOR if side > 0 else ROUND_CEILING
                ) * spec.tick
                qty = (
                    self.config.notional(symbol) / max(price, state.ask) / spec.step
                ).to_integral_value(rounding=ROUND_FLOOR) * spec.step
                if (
                    price <= 0
                    or qty < spec.minimum_quantity
                    or qty * price < spec.minimum_notional
                ):
                    state.reason = "amount_below_minimum"
                    quotes = []
                    break
                quotes.append((side, qty, price))
            for side, qty, price in quotes:
                commands.append(self._new(symbol, side, qty, price, False))
        return commands

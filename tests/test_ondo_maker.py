import random
import sys
from decimal import Decimal as D
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from ondo_maker import SECOND, Instrument, MakerConfig, MakerEngine, VolatilityGuard


def ready(config=None):
    engine = MakerEngine(config or MakerConfig(D(50), D(50)), "test")
    for market in ("BTC", "ETH"):
        engine.markets[market].instrument = Instrument(
            D("0.01"), D("0.001"), D("0.001")
        )
        engine.on_binance(market, 0, 100, 100)
        engine.on_binance(market, SECOND, 100, 100)
        engine.on_ondo(market, SECOND, D("99.99"), D("100.01"))
    engine.set_account(ready=True, reduction_ready=True, flat=True)
    return engine


def settle(engine, orders, now=SECOND):
    for order in orders:
        engine.update_order(
            order.client_id, filled=order.filled, terminal=True, now=now
        )


def test_guard_inclusive_boundary_quantity_updates_and_extension():
    g = VolatilityGuard()
    assert not g.update(0, 100, 100)
    assert g.update(SECOND, 100.02, 100.02)
    assert g.pause_until == 6 * SECOND
    assert not g.update(SECOND + 100, 100.02, 100.02)
    assert g.pause_until == 6 * SECOND
    assert g.update(SECOND + 200, 100, 100)
    assert g.pause_until == 6 * SECOND + 200


def test_guard_retains_quote_at_window_start_and_round_trip_range():
    g = VolatilityGuard()
    g.update(0, 100, 100)
    g.update(SECOND // 2, 100.03, 100.03)
    assert g.update(SECOND, 100, 100)
    assert g.range_bps == pytest.approx(3)
    assert g.update(SECOND + SECOND // 4, 100.01, 100.01)


def test_guard_matches_step_price_oracle_random_stream():
    rng = random.Random(27)
    g = VolatilityGuard()
    points = []
    now = 0
    price = 100.0
    for _ in range(5000):
        now += rng.randrange(1, 50_000_000)
        new = 100 + rng.randrange(-5, 6) / 100
        changed = new != price or not points
        if changed:
            points.append((now, new))
        g.update(now, new, new)
        if changed:
            left = 0
            while left + 1 < len(points) and points[left + 1][0] <= now - SECOND:
                left += 1
            values = [p for _, p in points[left:]]
            assert g.range_bps == pytest.approx((max(values) / min(values) - 1) * 10000)
        price = new


def test_guard_gap_reconnect_invalid_and_overflow_require_new_history():
    g = VolatilityGuard(capacity=2)
    g.update(0, 100, 100)
    g.update(1, 101, 101)
    g.update(2, 102, 102)
    assert not g.ready(SECOND, SECOND)
    g.update(3, 100, 100)
    g.update(SECOND + 4, 101, 101)
    assert not g.ready(SECOND + 4, SECOND)
    g.update(SECOND + 5, float("nan"), 101)
    assert not g.ready(2 * SECOND, SECOND)


def test_three_bp_cancels_both_sides_only_in_affected_market():
    e = ready()
    orders = [c.order for c in e.step(SECOND)]
    assert len(orders) == 4
    assert all(c.price == (D("99.95") if c.side > 0 else D("100.05")) for c in orders)
    e.on_ondo("BTC", SECOND + 1, D("99.96"), D("99.98"))
    commands = e.step(SECOND + 1)
    assert len(commands) == 2
    assert all(c.kind == "cancel" and c.order.market == "BTC" for c in commands)
    assert {c.order.side for c in commands} == {-1, 1}
    assert not e.step(SECOND + 2)


def test_cancel_confirmation_and_account_proof_required_before_replacement():
    e = ready()
    e.step(SECOND)
    e.on_ondo("BTC", SECOND + 1, D("99.96"), D("99.98"))
    cancels = e.step(SECOND + 1)
    settle(e, [cancels[0].order])
    assert not [c for c in e.step(SECOND + 2) if c.kind == "create"]
    settle(e, [cancels[1].order])
    assert not [c for c in e.step(SECOND + 3) if c.kind == "create"]
    e.set_account(ready=True, reduction_ready=True, flat=True)
    quotes = e.step(SECOND + 4)
    assert len(quotes) == 2
    assert all(c.kind == "create" and c.order.market == "BTC" for c in quotes)
    assert len(e.markets["ETH"].orders) == 2


def test_partial_fill_sends_all_cancels_before_close_without_waiting_for_ack():
    e = ready()
    e.step(SECOND)
    maker = next(o for o in e.markets["BTC"].orders.values() if o.side > 0)
    e.update_order(maker.client_id, filled=D("0.1"), terminal=False, now=SECOND + 1)
    commands = e.step(SECOND + 1)
    assert [c.kind for c in commands] == ["cancel"] * 4 + ["create"]
    close = commands[-1].order
    assert close.reduce_only and close.side == -1 and close.quantity == D("0.1")
    e.update_order(maker.client_id, filled=D("0.1"), terminal=False, now=SECOND + 2)
    assert e.markets["BTC"].position == D("0.1")
    e.update_order(maker.client_id, filled=D("0.15"), terminal=True, now=SECOND + 3)
    e.update_order(close.client_id, filled=D("0.1"), terminal=True, now=SECOND + 4)
    assert e.markets["BTC"].position == D("0.05")
    residual = [c for c in e.step(SECOND + e.config.retry_ns + 5) if c.kind == "create"]
    assert len(residual) == 1 and residual[0].order.quantity == D("0.05")
    assert residual[0].order.reduce_only


def test_stream_loss_cancels_and_does_not_disable_reduction():
    e = ready()
    e.step(SECOND)
    maker = next(iter(e.markets["BTC"].orders.values()))
    e.update_order(maker.client_id, filled=D("0.1"), terminal=True, now=SECOND + 1)
    e.disconnect("binance")
    commands = e.step(SECOND + 2)
    assert commands[-1].order.reduce_only
    assert all(c.kind == "cancel" for c in commands[:-1])


def test_volatility_cancels_both_sides_and_cooldown_does_not_release_unknown_orders():
    e = ready()
    e.step(SECOND)
    e.on_binance("ETH", SECOND + 1, 100.02, 100.02)
    commands = e.step(SECOND + 1)
    assert len(commands) == 2 and all(c.order.market == "ETH" for c in commands)
    for market in ("BTC", "ETH"):
        e.on_binance(market, 7 * SECOND, 100, 100)
        e.on_binance(market, 8 * SECOND, 100, 100)
        e.on_ondo(market, 8 * SECOND, D("99.99"), D("100.01"))
    assert all(c.kind == "cancel" for c in e.step(8 * SECOND))


def test_shutdown_waits_for_cancels_reduction_and_fresh_flat_proof():
    e = ready()
    e.step(SECOND)
    maker = next(iter(e.markets["BTC"].orders.values()))
    e.update_order(maker.client_id, filled=D("0.1"), terminal=True, now=SECOND + 1)
    e.stop()
    commands = e.step(SECOND + 1)
    assert not e.can_stop
    settle(e, [c.order for c in commands if c.kind == "cancel"])
    close = commands[-1].order
    e.update_order(
        close.client_id, filled=close.quantity, terminal=True, now=SECOND + 2
    )
    assert not e.can_stop
    assert not e.step(SECOND + 3)
    e.set_account(ready=True, reduction_ready=True, flat=True)
    assert e.can_stop


def test_fault_stops_new_quotes_but_preserves_close_retries_and_uncertain_reservations():
    e = ready()
    e.step(SECOND)
    maker = next(iter(e.markets["BTC"].orders.values()))
    e.update_order(maker.client_id, filled=D("0.1"), terminal=True, now=SECOND + 1)
    e.fail("storage_error")
    commands = e.step(SECOND + 1)
    close = commands[-1].order
    assert close.reduce_only
    assert not [c for c in e.step(SECOND + 500_000_000) if c.kind == "create"]
    e.update_order(
        close.client_id,
        filled=D(0),
        terminal=True,
        now=SECOND + 500_000_000,
        rejected=True,
    )
    assert next(
        c for c in e.step(SECOND + 800_000_000) if c.kind == "create"
    ).order.reduce_only


@pytest.mark.parametrize(
    "btc,eth", [("0", "10"), ("NaN", "10"), ("101", "10"), ("10", "Infinity")]
)
def test_invalid_amount_rejected(btc, eth):
    with pytest.raises(ValueError):
        MakerConfig(D(btc), D(eth))


def test_four_500_usd_makers_respect_exact_order_and_total_caps():
    engine = ready(MakerConfig(D(500), D(500), max_order_usd=D(500), max_gross_usd=D(2000)))
    commands = engine.step(SECOND)
    assert len(commands) == 4
    notionals = [c.order.quantity * c.order.price for c in commands]
    assert all(D(499) < amount <= D(500) for amount in notionals)
    assert sum(notionals) <= D(2000)


@pytest.mark.parametrize("order,gross", [("500.00000001", "2000"), ("500", "2000.00000001")])
def test_crypto_caps_cannot_be_exceeded(order, gross):
    with pytest.raises(ValueError):
        MakerConfig(D(500), D(500), max_order_usd=D(order), max_gross_usd=D(gross))


def test_no_idle_fill_from_one_market_can_restart_the_other_during_cleanup():
    e = ready()
    e.step(SECOND)
    bid = next(o for o in e.markets["BTC"].orders.values() if o.side == 1)
    ask = next(o for o in e.markets["BTC"].orders.values() if o.side == -1)
    e.update_order(bid.client_id, filled=D("0.1"), terminal=True, now=SECOND + 1)
    commands = e.step(SECOND + 1)
    close = commands[-1].order
    e.update_order(ask.client_id, filled=D("0.1"), terminal=True, now=SECOND + 2)
    assert e.markets["BTC"].position == 0
    assert close.client_id in e.markets["BTC"].orders
    assert not [c for c in e.step(SECOND + 3) if c.kind == "create"]


def test_short_fill_closes_with_buy_and_partially_filled_exit_retries_only_residual():
    e = ready()
    e.step(SECOND)
    ask = next(o for o in e.markets["ETH"].orders.values() if o.side == -1)
    e.update_order(ask.client_id, filled=D("0.2"), terminal=True, now=SECOND + 1)
    close = e.step(SECOND + 1)[-1].order
    assert close.side == 1 and close.reduce_only and close.quantity == D("0.2")
    e.update_order(close.client_id, filled=D("0.05"), terminal=True, now=SECOND + 2)
    new_close = e.step(SECOND + e.config.retry_ns + 3)[-1].order
    assert new_close.side == 1 and new_close.quantity == D("0.15")


def test_late_fill_after_cancel_is_still_closed_and_cannot_restart_quotes():
    e = ready()
    orders = [c.order for c in e.step(SECOND)]
    e.stop()
    e.step(SECOND + 1)
    settle(e, orders)
    e.set_account(ready=True, reduction_ready=True, flat=True)
    assert e.can_stop
    e.update_order(orders[0].client_id, filled=D("0.1"), terminal=True, now=SECOND + 2)
    assert not e.can_stop
    close = e.step(SECOND + 3)[-1].order
    assert close.reduce_only and close.quantity == D("0.1")


def test_stale_ondo_cancels_even_when_binance_is_live_and_rebuilds_before_quote():
    e = ready()
    e.step(SECOND)
    now = SECOND + e.config.ondo_stale_ns + 1
    for m in ("BTC", "ETH"):
        e.on_binance(m, now - SECOND, 100, 100)
        e.on_binance(m, now, 100, 100)
    commands = e.step(now)
    assert len(commands) == 4 and all(c.kind == "cancel" for c in commands)
    assert all(s.reason == "ondo_unavailable" for s in e.markets.values())

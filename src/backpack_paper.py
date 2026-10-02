"""A bounded synthetic roundtrip driven by native matching, fees and accounts."""

from __future__ import annotations

from decimal import Decimal
from itertools import groupby

from backpack_replay import OfflineFailure, checkpoint


def money_document(money):
    if money is None:
        return None
    return {"amount": format(money.as_decimal(), "f"), "currency": str(money.currency)}


async def run_paper(plan, instruments, data, evidence, deadline, result):
    from nautilus_trader.backtest import BacktestEngine
    from nautilus_trader.config import BacktestEngineConfig, StrategyConfig
    from nautilus_trader.common import LoggerConfig
    from nautilus_trader.model import (
        AccountType,
        BookType,
        Currency,
        Money,
        OmsType,
        OrderSide,
        Venue,
    )
    from nautilus_trader.trading import Strategy

    result.update(
        complete=False,
        shutdown_complete=True,
        synthetic=True,
        venue_account_observed=False,
        engine_input_kinds=["QuoteTick"],
        book_type="L1_MBP",
        liquidity_consumption=True,
        fills=[],
        orders=[],
        positions=[],
        cash=None,
        phases={},
    )
    quantities = dict(plan.paper.quantities)
    native_quantities = {}
    for symbol, instrument in instruments.items():
        try:
            quantity = instrument.make_qty(quantities[symbol])
        except Exception:
            raise OfflineFailure("paper_quantity_grid_mismatch") from None
        if (
            Decimal(str(quantity)) != quantities[symbol]
            or quantity < instrument.min_quantity
            or (
                instrument.max_quantity is not None
                and quantity > instrument.max_quantity
            )
        ):
            raise OfflineFailure("paper_quantity_grid_mismatch")
        native_quantities[str(instrument.id)] = quantity

    class Roundtrip(Strategy):
        def __init__(self):
            super().__init__(StrategyConfig())
            self.phases = {str(i.id): "initial" for i in instruments.values()}
            self.fills = []
            self.entry_ids = {}
            self.exit_ids = {}
            self.callback_failure = False

        def on_start(self):
            for instrument in instruments.values():
                self.subscribe_quotes(instrument.id)

        def on_quote(self, quote):
            key = str(quote.instrument_id)
            phase = self.phases[key]
            if phase not in {"initial", "open"}:
                return
            try:
                entering = phase == "initial"
                self.phases[key] = "entry_sent" if entering else "exit_sent"
                order = self.order_factory.market(
                    quote.instrument_id,
                    OrderSide.BUY if entering else OrderSide.SELL,
                    native_quantities[key],
                    reduce_only=not entering,
                )
                (self.entry_ids if entering else self.exit_ids)[key] = (
                    order.client_order_id
                )
                self.submit_order(order)
            except Exception:
                self.callback_failure = True
                self.phases[key] = "failed"

        def on_order_filled(self, event):
            key = str(event.instrument_id)
            order = self.cache.order(event.client_order_id)
            if order is None:
                self.callback_failure = True
                self.phases[key] = "failed"
            elif str(order.status) == "FILLED":
                if (
                    event.client_order_id == self.entry_ids.get(key)
                    and self.phases[key] == "entry_sent"
                ):
                    self.phases[key] = "open"
                elif (
                    event.client_order_id == self.exit_ids.get(key)
                    and self.phases[key] == "exit_sent"
                ):
                    self.phases[key] = "closed"
            item = {
                "instrument_id": key,
                "client_order_id": str(event.client_order_id),
                "trade_id": str(event.trade_id),
                "side": str(event.order_side),
                "price": str(event.last_px),
                "quantity": str(event.last_qty),
                "commission": money_document(event.commission),
                "ts_event_ns": str(event.ts_event),
                "ts_received_ns": str(event.ts_init),
            }
            self.fills.append(item)
            evidence.record("paper_fill", item)

    engine = None
    started = False
    strategy = Roundtrip()
    result["shutdown_complete"] = False
    try:
        engine = BacktestEngine(
            BacktestEngineConfig(
                bypass_logging=True,
                logging=LoggerConfig(bypass_logging=True),
                run_analysis=False,
            )
        )
        currency = Currency.from_str("USDC")
        venue = Venue("BACKPACK")
        engine.add_venue(
            venue,
            OmsType.NETTING,
            AccountType.MARGIN,
            [Money(plan.paper.initial_balance_usdc, currency)],
            base_currency=currency,
            book_type=BookType.L1_MBP,
            liquidity_consumption=True,
        )
        for instrument in instruments.values():
            engine.add_instrument(instrument)
        engine.add_strategy(strategy)
        quotes = [entry for entry in data if type(entry[3]).__name__ == "QuoteTick"]
        for _, group in groupby(quotes, key=lambda entry: entry[0]):
            await checkpoint(deadline, evidence)
            batch = [entry[3] for entry in group]
            if len(batch) > 64:
                raise OfflineFailure("paper_timestamp_batch_limit")
            engine.add_data(batch)
            started = True
            engine.run(streaming=True)
            engine.clear_data()
        await checkpoint(deadline, evidence)
    finally:
        if engine is not None:
            finalized = False
            try:
                if started:
                    engine.end()
                finalized = True
            finally:
                try:
                    orders = engine.cache.orders()
                    positions = engine.cache.positions()
                    account = engine.cache.account_for_venue(Venue("BACKPACK"))
                    result.update(
                        fills=list(strategy.fills),
                        phases=dict(strategy.phases),
                        cash=money_document(
                            account.balance_total(Currency.from_str("USDC"))
                        )
                        if account
                        else None,
                        orders=[
                            {
                                "client_order_id": str(o.client_order_id),
                                "instrument_id": str(o.instrument_id),
                                "status": str(o.status),
                                "quantity": str(o.quantity),
                                "filled_quantity": str(o.filled_qty),
                            }
                            for o in orders
                        ],
                        positions=[
                            {
                                "position_id": str(p.id),
                                "instrument_id": str(p.instrument_id),
                                "quantity": str(p.quantity),
                                "is_closed": p.is_closed,
                                "realized_pnl": money_document(p.realized_pnl),
                                "commissions": [
                                    money_document(m) for m in p.commissions()
                                ],
                            }
                            for p in positions
                        ],
                    )
                    result["complete"] = (
                        not strategy.callback_failure
                        and all(phase == "closed" for phase in strategy.phases.values())
                        and len(orders) == 2 * len(instruments)
                        and all(str(o.status) == "FILLED" for o in orders)
                        and not engine.cache.positions_open()
                        and not engine.cache.orders_open()
                    )
                finally:
                    engine.dispose()
                    result["shutdown_complete"] = finalized

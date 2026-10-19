"""Depth-priced paper pairs sharing the scanner's validated current books.

No broker, credentials, order submission or native imports live in this module.
Paper entries and soft exits require both legs in full. Hard exits independently
reduce each leg by its available depth. Unfilled residuals retain the pair lock.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import replace
from fractions import Fraction as F
from itertools import combinations
import sys

from opportunity_core import (_best_quotes_rule_out, _book_valid, _budget_quantity, _metadata_valid, _number,
                              _walk, evaluate_opportunity, exact_decimal)
from opportunity_runtime import ScanState
from opportunity_trade import TradePolicy
from opportunity_timing import sampled_timed, span


class PaperTradeState(ScanState):
    def __init__(self, plan, policy: TradePolicy, *, store=None, event_sink=None):
        plan = replace(plan, output_path=None, settings=replace(plan.settings,
            target_notional=policy.order_notional_usd, min_entry_edge_bps=policy.entry_edge_bps))
        super().__init__(plan)
        self.policy = policy
        self.store = store
        self.event_sink = event_sink
        self.positions: dict[tuple[str, str, str], dict] = {}
        self.last_closed: dict[tuple[str, str, str], dict] = {}
        self.sequence = 0
        self.completed = 0
        self.realized_net = F(0)
        self.event_counts = Counter()
        self.halted = False
        self.finished = False
        self.last_event = None
        self.consumed = {}
        saved = store.load() if store else None
        if saved:
            if saved.get("schema_version") != 2:
                raise ValueError("unknown paper position state version")
            self.sequence = saved["sequence"]
            self.completed = saved["completed"]
            self.realized_net = F(saved["realized_net_usd"])
            self.event_counts.update(saved["event_counts"])
            for position in saved["positions"]:
                key = self.pair_key(position["buy_id"], position["sell_id"])
                if key in self.positions or position["status"] not in {"open", "exit_pending"}:
                    raise ValueError("invalid or duplicate saved paper position")
                self.positions[key] = position
            self.last_closed = {tuple(row["pair"]): row for row in saved["last_closed"]}
        if store:
            store.save(self.snapshot(), [])

    def pair_key(self, buy_id, sell_id):
        buy, sell = self.markets[buy_id], self.markets[sell_id]
        if buy.symbol != sell.symbol or buy.venue == sell.venue:
            raise ValueError("a position needs one symbol and two different venues")
        return (buy.symbol, *sorted((buy.venue, sell.venue)))

    def snapshot(self):
        return {"schema_version": 2, "sequence": self.sequence, "completed": self.completed,
                "realized_net_usd": _number(self.realized_net), "event_counts": dict(self.event_counts),
                "positions": list(self.positions.values()), "last_closed": list(self.last_closed.values())}

    def emit(self, kind, now_ns, **fields):
        event = deepcopy({"event": kind, "observed_ns": now_ns, "mode": "paper", **fields})
        self.event_counts[kind] += 1
        self.last_event = event
        try:
            if self.store:
                self.store.save(self.snapshot(), [event])
            if self.event_sink:
                self.event_sink(event)
        except Exception:
            self.halted = True
            raise

    @sampled_timed("pricing.prepare_pair")
    def _pair(self, buy_id, sell_id, now_ns):
        if not all(i in self.books and i in self.metadata for i in (buy_id, sell_id)):
            return None
        books = [self.books[buy_id], self.books[sell_id]]
        metadata = [self.metadata[buy_id], self.metadata[sell_id]]
        if not all(m.instrument_id == i and _metadata_valid(m) and _book_valid(b, self.plan.settings, now_ns)
                   for i, b, m in zip((buy_id, sell_id), books, metadata)):
            return None
        if abs(books[0].ts_event_ns - books[1].ts_event_ns) > self.plan.settings.max_skew_ms * 1_000_000:
            return None
        return [self._remaining_book(i) for i in (buy_id, sell_id)], metadata

    @sampled_timed("pricing.remaining_book")
    def _remaining_book(self, instrument_id):
        book = self.books[instrument_id]
        generation = (book.ts_event_ns, book.bids, book.asks)
        used = self.consumed.get(instrument_id)
        if used is None or used["generation"] != generation:
            used = {"generation": generation, "bids": {}, "asks": {}}
            self.consumed[instrument_id] = used
        # Live pricing never simulates fills. An untouched immutable snapshot
        # already represents all remaining liquidity; avoid rebuilding every
        # level for every directed pair before the cheap price rejection.
        if not used["bids"] and not used["asks"]:
            return book
        sides = {}
        for side in ("bids", "asks"):
            sides[side] = tuple((price, exact_decimal(F(size) - used[side].get(price, F(0))))
                for price, size in getattr(book, side) if F(size) > used[side].get(price, F(0)))
        return replace(book, **sides)

    def _consume(self, instrument_id, side, base_quantity):
        """A distinct source snapshot provides a new pool of simulated liquidity."""
        book = self._remaining_book(instrument_id)
        remaining = F(base_quantity) / F(self.metadata[instrument_id].multiplier)
        consumed = self.consumed[instrument_id][side]
        for price, size in getattr(book, side):
            filled = min(remaining, F(size))
            consumed[price] = consumed.get(price, F(0)) + filled
            remaining -= filled
            if remaining == 0:
                return
        raise RuntimeError("paper fill exceeded available snapshot liquidity")

    @sampled_timed("pricing.entry")
    def entry(self, buy_id, sell_id, now_ns):
        if not all(i in self.books and i in self.metadata for i in (buy_id, sell_id)):
            return None
        if _best_quotes_rule_out(self.books[buy_id], self.metadata[buy_id],
                                 self.books[sell_id], self.metadata[sell_id], self.plan.settings):
            return None
        pair = self._pair(buy_id, sell_id, now_ns)
        if pair is None:
            return None
        books, metadata = pair
        settings = self.plan.settings
        with span("scan.evaluate_pair"):
            entry = evaluate_opportunity(books[0], metadata[0], books[1], metadata[1], settings, now_ns)
        if entry is None:
            return None
        slip = F(self.policy.slippage_bps) / 10000
        quantity = F(entry["sizing"]["base_quantity"])
        step = F(entry["sizing"]["common_base_step"])
        cap = F(self.policy.order_notional_usd)
        for side, levels, meta, factor in (("buy", books[0].asks, metadata[0], 1 + slip),
                                           ("sell", books[1].bids, metadata[1], 1 - slip)):
            if F(entry["sizing"][f"{side}_notional"]) * factor > cap:
                upper = _budget_quantity(levels, F(meta.multiplier), cap / factor)
                if upper is None:
                    return None
                quantity = min(quantity, (upper // step) * step)
        if quantity <= 0:
            return None
        if quantity != F(entry["sizing"]["base_quantity"]):
            budget = _walk(books[0].asks, F(metadata[0].multiplier), quantity)
            with span("scan.evaluate_pair"):
                entry = evaluate_opportunity(books[0], metadata[0], books[1], metadata[1],
                    replace(settings, target_notional=exact_decimal(budget)), now_ns)
            if entry is None:
                return None
        buy_cash = F(entry["sizing"]["buy_notional"]) * (1 + slip)
        sell_cash = F(entry["sizing"]["sell_notional"]) * (1 - slip)
        if max(buy_cash, sell_cash) > cap:
            return None
        buy_fee = buy_cash * F(metadata[0].taker_fee_bps) / 10000
        sell_fee = sell_cash * F(metadata[1].taker_fee_bps) / 10000
        reserve = buy_cash * F(settings.reserve_bps) / 10000
        net = sell_cash - buy_cash - buy_fee - sell_fee - reserve
        if net * 10000 < F(self.policy.entry_edge_bps) * buy_cash:
            return None
        entry["paper_fills"] = {"buy_notional_usd": _number(buy_cash),
            "sell_notional_usd": _number(sell_cash), "buy_fee_usd": _number(buy_fee),
            "sell_fee_usd": _number(sell_fee), "slippage_bps": str(self.policy.slippage_bps),
            "entry_after_fees_slippage_and_reserve_bps": _number(net * 10000 / buy_cash)}
        return entry

    def _stamps(self, buy_id, sell_id):
        return {i: [self.books[i].ts_event_ns, self.books[i].ts_received_ns]
                for i in (buy_id, sell_id) if i in self.books}

    def _rearmed(self, key, buy_id, sell_id, now_ns):
        previous = self.last_closed.get(key)
        if previous is None:
            return True
        # Do not consume the same observation again on a timer or reverse callback.
        return now_ns > previous["closed_ns"] and all(
            i in self.books and self.books[i].ts_event_ns > previous["closed_ns"]
            for i in (buy_id, sell_id))

    def evaluate_symbol(self, symbol, now_ns, *, changed_id=None):
        if self.halted or self.finished:
            return
        pairs = []
        for first, second in combinations(sorted(self.groups[symbol]), 2):
            if changed_id is not None and changed_id not in (first, second):
                continue
            key = self.pair_key(first, second)
            if key in self.positions:
                self.observe(key, now_ns)
                continue
            if self._rearmed(key, first, second, now_ns):
                pairs.append((key, first, second))
        for key, first, second in pairs:
            for buy_id, sell_id in ((first, second), (second, first)):
                entry = self.entry(buy_id, sell_id, now_ns)
                if entry is None:
                    continue
                self.sequence += 1
                position = {"position_id": self.sequence, "symbol": symbol, "pair": list(key),
                    "buy_id": buy_id, "sell_id": sell_id, "opened_ns": now_ns, "status": "open",
                    "exit_reason": None, "exit_requested_ns": None, "entry": entry,
                    "remaining_buy_base": entry["sizing"]["base_quantity"],
                    "remaining_sell_base": entry["sizing"]["base_quantity"],
                    "closing_long_cash_usd": "0", "closing_short_cash_usd": "0",
                    "closing_fees_usd": "0"}
                # Occupy both directions before publishing the simulated fill.
                self.positions[key] = position
                quantity = F(entry["sizing"]["base_quantity"])
                self._consume(buy_id, "asks", quantity)
                self._consume(sell_id, "bids", quantity)
                self.emit("opened", now_ns, position=position)
                self.observe(key, now_ns)
                break

    def _exit_leg(self, position, opening_side, now_ns):
        instrument_id = position[f"{opening_side}_id"]
        book, meta = self.books.get(instrument_id), self.metadata.get(instrument_id)
        if book is None or meta is None or meta.instrument_id != instrument_id:
            return None
        if not _metadata_valid(meta) or not _book_valid(book, self.plan.settings, now_ns):
            return None
        old = position["entry"][f"{opening_side}_metadata"]
        if F(meta.multiplier) != F(old["multiplier"]) or F(meta.size_increment) != F(old["size_increment"]):
            return None
        return self._remaining_book(instrument_id), meta

    def _pnl(self, position, sale=F(0), cover=F(0), sale_fee=F(0), cover_fee=F(0)):
        fills = position["actual_entry"] if "actual_entry" in position else position["entry"]["paper_fills"]
        buy_cash, sell_cash = F(fills["buy_notional_usd"]), F(fills["sell_notional_usd"])
        entry_fees = F(fills["buy_fee_usd"]) + F(fills["sell_fee_usd"])
        total_sale = sale + F(position["closing_long_cash_usd"])
        total_cover = cover + F(position["closing_short_cash_usd"])
        gross = sell_cash - buy_cash + total_sale - total_cover
        fees = entry_fees + F(position["closing_fees_usd"]) + sale_fee + cover_fee
        net = gross - fees
        return {"long_sale_usd": _number(total_sale), "short_cover_usd": _number(total_cover),
                "gross_usd": _number(gross), "round_trip_fees_usd": _number(fees),
                "net_usd": _number(net), "net_bps": _number(net * 10000 / buy_cash),
                "capital_usd": _number(buy_cash)}

    def measure(self, position, now_ns):
        cash, fees, stamps = {}, {}, []
        executable = True
        slip = F(self.policy.slippage_bps) / 10000
        for opening_side, book_side, factor in (("buy", "bids", 1 - slip), ("sell", "asks", 1 + slip)):
            quantity = F(position[f"remaining_{opening_side}_base"])
            if quantity == 0:
                cash[opening_side] = fees[opening_side] = F(0)
                continue
            ready = self._exit_leg(position, opening_side, now_ns)
            if ready is None:
                return None
            book, meta = ready
            stamps.append(book.ts_event_ns)
            value = _walk(getattr(book, book_side), F(meta.multiplier), quantity)
            if value is None:
                # This optimistic bound can prove a loss, never a profitable fill.
                executable = False
                original = self.books[position[f"{opening_side}_id"]]
                value = F(getattr(original, book_side)[0][0]) * quantity
            cash[opening_side] = value * factor
            fees[opening_side] = value * factor * F(meta.taker_fee_bps) / 10000
        if len(stamps) == 2 and abs(stamps[0] - stamps[1]) > self.plan.settings.max_skew_ms * 1_000_000:
            return None
        return {**self._pnl(position, cash["buy"], cash["sell"], fees["buy"], fees["sell"]),
                "executable": executable, "valuation": "depth" if executable else "best_touch_bound",
                "long_sale_fee_usd": _number(fees["buy"]), "short_cover_fee_usd": _number(fees["sell"]),
                "book_stamps": self._stamps(position["buy_id"], position["sell_id"])}

    def _reduction(self, position, opening_side, now_ns, *, partial):
        remaining = F(position[f"remaining_{opening_side}_base"])
        if remaining == 0:
            return None
        ready = self._exit_leg(position, opening_side, now_ns)
        if ready is None:
            return None
        book, meta = ready
        book_side = "bids" if opening_side == "buy" else "asks"
        levels = getattr(book, book_side)
        multiplier = F(meta.multiplier)
        slip = F(self.policy.slippage_bps) / 10000
        price_factor = 1 - slip if opening_side == "buy" else 1 + slip
        quantity = remaining
        if partial:
            available = sum((F(size) * multiplier for _, size in levels), F(0))
            step = F(meta.size_increment) * multiplier
            quantity = min(remaining, (available // step) * step)
            if 0 < quantity < remaining:
                # Do not create a residual which the same conservative order
                # constraints would refuse on the next quote. Future prices may
                # still change its notional; this uses today's lowest book price.
                minimum = step
                if meta.min_quantity is not None:
                    minimum = max(minimum, F(meta.min_quantity) * multiplier)
                if meta.min_notional is not None:
                    lowest_price = min(F(price) for price, _ in levels) * price_factor
                    minimum = max(minimum, F(meta.min_notional) / lowest_price)
                minimum = -(-minimum // step) * step
                if remaining - quantity < minimum:
                    quantity = max(F(0), ((remaining - minimum) // step) * step)
        if quantity <= 0:
            return None
        native_quantity = quantity / multiplier
        if meta.min_quantity is not None and native_quantity < F(meta.min_quantity):
            return None
        cash = _walk(levels, multiplier, quantity)
        if cash is None:
            return None
        cash *= price_factor
        if meta.min_notional is not None and cash < F(meta.min_notional):
            return None
        return {"instrument_id": position[f"{opening_side}_id"],
                "side": "SELL" if opening_side == "buy" else "BUY", "opening_side": opening_side,
                "base_quantity": _number(quantity), "native_quantity": _number(native_quantity),
                "cash_usd": _number(cash), "fee_usd": _number(cash * F(meta.taker_fee_bps) / 10000),
                "fee_source": meta.fee_source, "source_ns": book.ts_event_ns}

    def _apply_reductions(self, position, fills):
        for fill in fills:
            side = fill["opening_side"]
            quantity = F(fill["base_quantity"])
            self._consume(fill["instrument_id"], "bids" if side == "buy" else "asks", quantity)
            field = f"remaining_{side}_base"
            position[field] = _number(F(position[field]) - quantity)
            cash_field = "closing_long_cash_usd" if side == "buy" else "closing_short_cash_usd"
            position[cash_field] = _number(F(position[cash_field]) + F(fill["cash_usd"]))
            position["closing_fees_usd"] = _number(F(position["closing_fees_usd"]) + F(fill["fee_usd"]))

    def observe(self, key, now_ns):
        position = self.positions[key]
        age_ns = now_ns - position["opened_ns"]
        mark = self.measure(position, now_ns)
        reason = None
        if age_ns >= self.policy.force_close_after_secs * 1_000_000_000:
            reason = "max_hold"
        elif mark is not None:
            net, capital = F(mark["net_usd"]), F(mark["capital_usd"])
            if self.policy.loss_stop_enabled and net * 10000 <= -F(self.policy.max_loss_bps) * capital:
                reason = "max_loss"
            elif mark["executable"] and net * 10000 >= F(self.policy.take_profit_bps) * capital:
                reason = "take_profit"
            elif mark["executable"] and age_ns >= self.policy.breakeven_after_secs * 1_000_000_000 and net > 0:
                reason = "positive_after_wait"
        if position["status"] == "exit_pending" and position["exit_reason"] in {
                "take_profit", "positive_after_wait"}:
            # A crash can occur after the request transaction and before its fill
            # transaction. A resumed soft exit must still meet its price target.
            if reason is None:
                previous_reason = position["exit_reason"]
                position.update(status="open", exit_reason=None, exit_requested_ns=None)
                position.pop("exit_blocked", None)
                self.emit("exit_deferred", now_ns, position_id=position["position_id"],
                          previous_reason=previous_reason)
                return
            if reason != position["exit_reason"]:
                position.update(exit_reason=reason, exit_requested_ns=now_ns)
                self.emit("exit_revised", now_ns, position_id=position["position_id"], reason=reason)
        if position["status"] == "open" and reason:
            position.update(status="exit_pending", exit_reason=reason, exit_requested_ns=now_ns)
            self.emit("exit_requested", now_ns, position_id=position["position_id"], reason=reason,
                      age_ms=age_ns // 1_000_000, mark=mark)
        if position["status"] != "exit_pending":
            return
        hard_exit = position["exit_reason"] in {"max_hold", "max_loss"}
        fills = [fill for side in ("buy", "sell") if (
            fill := self._reduction(position, side, now_ns, partial=hard_exit)) is not None]
        if not hard_exit and (mark is None or not mark["executable"] or len(fills) != 2):
            fills = []
        self._apply_reductions(position, fills)
        if any(F(position[f"remaining_{side}_base"]) > 0 for side in ("buy", "sell")):
            if fills:
                position.pop("exit_blocked", None)
                self.emit("partial_reduction", now_ns, position_id=position["position_id"],
                          reason=position["exit_reason"], fills=fills,
                          remaining_buy_base=position["remaining_buy_base"],
                          remaining_sell_base=position["remaining_sell_base"])
            elif not position.get("exit_blocked"):
                position["exit_blocked"] = True
                self.emit("exit_blocked", now_ns, position_id=position["position_id"],
                          reason="no valid executable reduction; residuals retain the pair lock",
                          remaining_buy_base=position["remaining_buy_base"],
                          remaining_sell_base=position["remaining_sell_base"])
            return
        mark = {**self._pnl(position), "executable": True, "valuation": "completed_fills"}
        self.positions.pop(key)
        self.completed += 1
        self.realized_net += F(mark["net_usd"])
        self.last_closed[key] = {"pair": list(key), "closed_ns": now_ns}
        self.emit("closed", now_ns, position_id=position["position_id"], pair=list(key),
                  buy_id=position["buy_id"], sell_id=position["sell_id"],
                  base_quantity=position["entry"]["sizing"]["base_quantity"],
                  opened_ns=position["opened_ns"], reason=position["exit_reason"],
                  exit_requested_ns=position["exit_requested_ns"], age_ms=age_ns // 1_000_000,
                  mark=mark, fills=fills)

    def on_evaluation_timer(self, now_ns):
        if self.halted or self.finished:
            return
        for key in tuple(self.positions):
            self.observe(key, now_ns)

    def prune(self, now_ns, blocked_symbols=()):
        # The native evaluation timer has already published all complete batches.
        # A display refresh must never trade from its cached, pending books.
        pass

    def instrument_exposure(self):
        result = {}
        for position in self.positions.values():
            for instrument_id, delta in ((position["buy_id"], F(position["remaining_buy_base"])),
                                         (position["sell_id"], -F(position["remaining_sell_base"]))):
                row = result.setdefault(instrument_id, {"long": F(0), "short": F(0)})
                row["long" if delta > 0 else "short"] += abs(delta)
        return {i: {"long_canonical_quantity": _number(row["long"]),
                    "short_canonical_quantity": _number(row["short"]),
                    "net_canonical_quantity": _number(row["long"] - row["short"])}
                for i, row in sorted(result.items())}

    def document(self):
        return {"mode": "paper", "orders_sent": 0, "opened_pairs": self.sequence,
                "closed_pairs": self.completed, "active_pairs": len(self.positions),
                "exit_pending_pairs": sum(p["status"] == "exit_pending" for p in self.positions.values()),
                "realized_net_usd": _number(self.realized_net), "realized_net_scope": "completed pairs",
                "halted": self.halted,
                "policy": self.policy.document(), "event_counts": dict(self.event_counts),
                "positions": list(self.positions.values()), "exposure": self.instrument_exposure(),
                "pnl_excludes": ["funding", "borrow_costs", "execution_latency"],
                "fill_model": "full paired entry/soft exit; independent partial hard exits from available L2"}

    def display(self, stream=sys.stdout):
        stream.write(f"[paper-trade] active={len(self.positions)} opened={self.sequence} "
                     f"closed={self.completed} realized_net=${float(self.realized_net):.6f} "
                     f"books={len(self.books)}/{len(self.markets)} real_orders=0\n")
        for position in sorted(self.positions.values(), key=lambda p: p["position_id"])[:self.plan.top_n]:
            stream.write(f"  #{position['position_id']} {position['symbol']} {position['status']} "
                         f"long {position['buy_id']} remaining={position['remaining_buy_base']} "
                         f"short {position['sell_id']} remaining={position['remaining_sell_base']} "
                         f"exit={position['exit_reason'] or '-'}\n")
        stream.flush()

    def finish(self, now_ns):
        if not self.finished and not self.halted:
            # Runtime shutdown is not a fill and does not alter the user's exit
            # policy. Open pairs remain durable for resumption with fresh books.
            self.on_evaluation_timer(now_ns)
            self.finished = True
            self.emit("stopped", now_ns, remaining_pairs=len(self.positions))

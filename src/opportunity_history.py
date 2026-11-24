"""Separate inactive historical economics from current executable exposure."""
from decimal import Decimal, InvalidOperation
from fractions import Fraction as F

from opportunity_core import _number


RECOVERABLE_HISTORY_HALTS = frozenset({
    "late fill after order terminal requires reconciliation",
    "exit fill precedes owned entry reconciliation",
    "unresolved persisted order IDs require execution reconciliation",
})
INACTIVE_SOURCES = frozenset({
    "lighter_http_order_status", "lighter_websocket_terminal_order", "native_terminal_order",
    "lighter_submission_rejected",
})


def order_has_execution_risk(order):
    return order["terminal_ns"] is None and not order.get("execution_closed")


def position_orders(live, position):
    return [live.orders[cid] for name in ("entry_ids", "exit_ids", "rebalance_ids")
            for cid in position.get(name, ())]


def record_execution_closed(live, order, receipt, now_ns):
    if receipt.final_fill_source not in INACTIVE_SOURCES or receipt.final_filled_quantity is None:
        return False
    try:
        quantity = F(receipt.final_filled_quantity)
    except (ValueError, OverflowError, ZeroDivisionError):
        return False
    if not F(order["filled_quantity"]) <= quantity <= F(order["quantity"]):
        return False
    if receipt.final_fill_source == "lighter_submission_rejected" and (
            quantity != 0 or not order["instrument_id"].endswith(".LIGHTER")):
        return False
    proof = {"quantity": _number(quantity), "source": receipt.final_fill_source, "observed_ns": now_ns}
    previous = order.get("execution_closed")
    position = live.historical_positions.get(str(order["position_id"]))
    if position is not None and previous and previous["quantity"] != proof["quantity"]:
        record_historical_conflict(live, position, order, now_ns, proof)
        return False
    if previous and previous["quantity"] == proof["quantity"] and previous["source"] == proof["source"]:
        return True
    order["execution_closed"] = proof
    # This is lifecycle evidence only, never a fabricated fill or economic terminal.
    if str(order["position_id"]) not in live.historical_positions:
        instrument = order["instrument_id"]
        live.fences[instrument] = max(live.fences.get(instrument, 0), now_ns)
        live._request_account(instrument, now_ns)
    live.emit("order_execution_closed", now_ns, client_order_id=order["client_order_id"], proof=proof)
    return True


def refresh_historical_economics(live, position, now_ns):
    key = str(position["position_id"])
    orders = position_orders(live, position)
    expected = position["historical_final_quantities"]
    fills_complete = all(F(o["filled_quantity"]) == F(expected[o["client_order_id"]]) for o in orders)
    inventory_complete = all(not F(position[f"remaining_{leg}_base"])
        and not F(position.get(f"unmatched_exit_{leg}_base", "0")) for leg in ("buy", "sell"))
    economics = live._economics(position)
    complete = (fills_complete and inventory_complete and economics["fee_confirmed"]
                and not position.get("historical_evidence_conflicts"))
    credited = F(position.get("historical_credited_net_usd", "0"))
    if complete:
        net = F(economics["net_usd"])
        live.realized_net += net - credited
        position["historical_credited_net_usd"] = _number(net)
        live.pending_economics.pop(key, None)
    else:
        # Missing trade cash is not a realized profit. Equity loss checks are
        # independent and continue using their original session baseline.
        if credited:
            live.realized_net -= credited
            position["historical_credited_net_usd"] = "0"
        live.pending_economics[key] = {**economics, "net_usd": None,
            "provisional_net_usd": economics["provisional_net_usd"] if fills_complete and inventory_complete else None,
            "fills_complete": fills_complete, "inventory_complete": inventory_complete,
            "evidence_conflicts": bool(position.get("historical_evidence_conflicts")),
            "closed_ns": position["risk_closed_ns"], "reason": position["exit_reason"]}
    position["historical_economics_complete"] = complete


def record_historical_conflict(live, position, order, now_ns, detail):
    position.setdefault("historical_evidence_conflicts", {})[order["client_order_id"]] = {
        "observed_ns": now_ns, "detail": detail}
    refresh_historical_economics(live, position, now_ns)
    live.emit("historical_order_evidence_conflict", now_ns, client_order_id=order["client_order_id"])


def commission_in_quote(market, currency, quote):
    if currency == quote:
        return True
    if market.quote_to_usd != 1:
        return False
    if market.venue == "ARCUS" and currency == "USDG.ARCUS" and quote == "USD":
        return True
    return {currency, quote} <= {"USD", "USDC", "USDT"}


def apply_commission_update(live, position, order, fill, identity, signature, now_ns):
    """Enrich a true execution's missing fee without applying quantity or cash twice."""
    if position is None or identity not in position.get("pending_commissions", {}):
        return False
    previous = live.executions[identity]
    if (any(previous[k] != signature[k] for k in ("client_order_id", "quantity", "price"))
            or fill["instrument_id"] != order["instrument_id"] or fill["side"] != order["side"]):
        return False
    market = live.markets[order["instrument_id"]]
    native = live.instruments[order["instrument_id"]]
    currency = fill.get("commission_currency")
    try:
        amount = Decimal(str(fill.get("commission_amount")))
    except InvalidOperation:
        return False
    if not amount.is_finite() or not commission_in_quote(market, currency, native["quote_currency"]):
        return False
    fee = F(amount) * F(market.quote_to_usd)
    if order["purpose"] == "entry":
        field = order["leg"] + "_fee_usd"
        position["actual_entry"][field] = _number(F(position["actual_entry"][field]) + fee)
    else:
        position["closing_fees_usd"] = _number(F(position["closing_fees_usd"]) + fee)
    del position["pending_commissions"][identity]
    position["unknown_commission"] = bool(position["pending_commissions"])
    live.executions[identity] = signature
    if str(order["position_id"]) in live.historical_positions:
        refresh_historical_economics(live, position, now_ns)
    live.emit("execution_commission_confirmed", now_ns, execution_id=identity,
              position_id=position["position_id"], fee_usd=_number(fee))
    return True


def archive_flat_positions(live, now_ns):
    """Require inactive original IDs and fresh target flat/no-order evidence.

    No timeout or empty order list alone certifies that a sent request cannot execute.
    Called at account/evidence callbacks and timers, never for every market tick.
    """
    for key, position in tuple(live.positions.items()):
        orders = position_orders(live, position)
        if not orders or any(order_has_execution_risk(o) for o in orders):
            continue
        # Keep the ordinary complete-fill close path unchanged.
        if (not position["unknown_commission"]
                and not any(o["client_order_id"] in live.order_reconciliations for o in orders)
                and not any(o["terminal_ns"] is None for o in orders) and not any(
                F(position[f"remaining_{leg}_base"]) or F(position.get(f"unmatched_exit_{leg}_base", "0"))
                for leg in ("buy", "sell"))):
            continue
        instruments = {position["buy_id"], position["sell_id"]}
        if any(p is not position and instruments.intersection((p["buy_id"], p["sell_id"]))
               for p in live.positions.values()):
            continue
        boundary = max(max(o.get("execution_closed", {}).get("observed_ns", 0),
                           o["terminal_ns"] or 0, o["sent_ns"]) for o in orders)
        proofs = {}
        for instrument in instruments:
            account = live._target_exit_account(instrument, now_ns)
            if (account is None or account.observed_ns <= boundary or account.position_quantity
                    or account.open_order_ids or account.account_positions is None
                    or account.account_open_orders is None
                    or any(i == instrument and q for i, q in account.account_positions)
                    or any(i == instrument for i, _ in account.account_open_orders)):
                live._request_account(instrument, now_ns)
                break
            # An unrelated pending request on this target could create a new position.
            if any(order_has_execution_risk(o) for o in live._pending(instrument)):
                break
            proofs[instrument] = account.observed_ns
        if len(proofs) != len(instruments):
            continue
        position.update(status="historical", risk_closed_ns=now_ns, flat_account_proof=proofs,
            historical_final_quantities={o["client_order_id"]:
                o.get("execution_closed", {}).get("quantity", o["filled_quantity"]) for o in orders})
        live.historical_positions[str(position["position_id"])] = position
        del live.positions[key]
        live.completed += 1
        live.last_closed[key] = now_ns
        for order in orders:
            live.order_reconciliations.pop(order["client_order_id"], None)
        if not live.order_reconciliations and live.halt_reason in RECOVERABLE_HISTORY_HALTS:
            live.halt_reason = None
        refresh_historical_economics(live, position, now_ns)
        live.emit("position_risk_closed", now_ns, position_id=position["position_id"], pair=list(key),
            read_started_ns=proofs, accounting_pending=not position["historical_economics_complete"],
            original_order_statuses={o["client_order_id"]: o["status"] for o in orders})


def historical_receipt(live, order, receipt, now_ns):
    position = live.historical_positions.get(str(order["position_id"]))
    if position is None:
        return False
    if receipt.kind == "reconciliation_required":
        record_historical_conflict(live, position, order, now_ns, {"kind": receipt.kind})
        return True
    if receipt.kind in {"terminal", "canceled", "expired", "rejected", "denied", "terminal_evidence"}:
        quantity = receipt.final_filled_quantity
        expected = F(position["historical_final_quantities"][order["client_order_id"]])
        if quantity is not None and F(quantity) != expected:
            # A disagreement about an inactive original order is historical
            # accounting evidence, not a new command or current inventory.
            record_historical_conflict(live, position, order, now_ns,
                {"previous": _number(expected), "observed": str(quantity)})
            return True
        if receipt.kind != "terminal_evidence" and order["terminal_ns"] is None:
            order.update(terminal_ns=now_ns, status=receipt.kind)
        refresh_historical_economics(live, position, now_ns)
        live.emit("historical_order_reconciled", now_ns, client_order_id=order["client_order_id"],
            accounting_pending=not position["historical_economics_complete"])
    return True

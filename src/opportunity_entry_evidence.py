"""Send-time entry evidence and gates; no I/O, credentials or price relaxation."""
from fractions import Fraction as F
import time

from opportunity_core import _book_valid, _number
from opportunity_entry_admission import temporal_entry_evidence
from opportunity_timing import record_entry_stage, record_entry_pair


def entry_market_snapshot(live, intent, now_ns):
    """Observe the current book at an order callback without changing admission."""
    book, market = live.books.get(intent.instrument_id), live.markets.get(intent.instrument_id)
    if book is None or market is None or not book.bids or not book.asks:
        return {"status": "missing_book"}
    conversion = F(market.canonical_multiplier) / F(market.quote_to_usd)
    limit, quantity = F(intent.price), F(intent.quantity)
    buy = intent.side == "BUY"
    levels = book.asks if buy else book.bids
    depth = sum((F(q) for p, q in levels
                 if (F(p) * conversion <= limit if buy else F(p) * conversion >= limit)), F(0))
    touch = F(levels[0][0]) * conversion
    return {"status": "valid" if _book_valid(book, live.plan.settings, now_ns) else "stale_or_invalid",
            "source_wall_ns": book.ts_event_ns, "received_wall_ns": book.ts_received_ns,
            "source_age_ms": _number(F(now_ns - book.ts_event_ns, 1_000_000)),
            "receive_age_ms": _number(F(now_ns - book.ts_received_ns, 1_000_000)),
            "bid_price": _number(F(book.bids[0][0]) * conversion),
            "ask_price": _number(F(book.asks[0][0]) * conversion),
            "limit_price": str(intent.price), "quantity": str(intent.quantity),
            "depth_within_limit": _number(depth), "full_quantity_available": depth >= quantity,
            "touch_outside_limit_bps": _number((touch - limit if buy else limit - touch) / limit * 10000)}


def entry_dispatch_evidence(live, position, now_ns):
    """Validate both original IOC legs against current immutable book evidence.

    Persisted amounts/limits stay authoritative. Refuse rather than silently
    reprice a durable intent. Every invocation checks both legs, including after
    a synchronous first-leg receipt changes state in the dispatch loop.
    """
    temporal_reason, evidence = temporal_entry_evidence(live, position, now_ns)
    bounds = {}
    if (live.phase != "active" or live.stopping or not live.cleanup_allowed
            or live.entry_pause_reason or live.halt_reason or live.order_reconciliations
            or any(p.get("unhedged_breach") for p in live.positions.values())
            or any(o["terminal_ns"] is None and (o["status"] == "unknown"
                    or now_ns - o["sent_ns"] >= live.order_timeout_ns)
                   for o in live.orders.values())):
        return "entry_admission_changed", evidence
    reserve_bps = getattr(live, "entry_unhedged_reserve_bps", F(10))
    risk_budget = min(F(live.policy.order_notional_usd),
                      live.max_unhedged_notional * (1 - reserve_bps / 10000))
    evidence.update(unhedged_reserve_bps=_number(reserve_bps),
                    per_leg_risk_budget_usd=_number(risk_budget))
    for leg in ("buy", "sell"):
        instrument = position[f"{leg}_id"]
        order = next(live.orders[cid] for cid in position["entry_ids"]
                     if live.orders[cid]["leg"] == leg)
        book, meta = live.books.get(instrument), live.metadata.get(instrument)
        row = evidence.setdefault(leg, {})
        row.update(limit_price=order["price"], quantity=order["quantity"])
        reason, leverage = live.leverage_problem(instrument, order["price"], order["quantity"], now_ns)
        if leverage:
            row["leverage"] = leverage
        if reason:
            return reason, evidence
        if book is None:
            return "missing_book", evidence
        row.update(source_wall_ns=book.ts_event_ns, received_wall_ns=book.ts_received_ns,
                   source_age_ms=_number(F(now_ns - book.ts_event_ns, 1_000_000)),
                   receive_age_ms=_number(F(now_ns - book.ts_received_ns, 1_000_000)))
        if not _book_valid(book, live.plan.settings, now_ns):
            return "stale_or_invalid_book", evidence
        if meta is None:
            return "metadata_unavailable", evidence
        if (position["native_rules"][leg] != {k: str(v) for k, v in live.instruments.get(instrument, {}).items()}
                or any(str(getattr(meta, k)) != str(position["entry"][f"{leg}_metadata"].get(k))
                       for k in ("multiplier", "size_increment"))):
            return "metadata_changed", evidence
        levels = book.asks if leg == "buy" else book.bids
        quantity, limit = F(order["quantity"]), F(order["price"])
        market = live.markets[instrument]
        conversion = F(market.canonical_multiplier) / F(market.quote_to_usd)
        row["available_quantity"] = _number(sum((F(q) for _, q in levels), F(0)))
        executable = [(F(p) * conversion, F(q)) for p, q in levels
                      if (F(p) * conversion <= limit if leg == "buy" else F(p) * conversion >= limit)]
        row["depth_within_limit"] = _number(sum((q for _, q in executable), F(0)))
        if F(row["available_quantity"]) < quantity:
            return "insufficient_depth", evidence
        if F(row["depth_within_limit"]) < quantity:
            return "price_outside_limit", evidence
        remaining, cash = quantity, F(0)
        for price, size in executable:
            amount = min(remaining, size)
            cash += amount * price
            remaining -= amount
            if not remaining:
                break
        row["executable_vwap"] = _number(cash / quantity)
        base = quantity * F(meta.multiplier)
        bound = live._limit(instrument, order["side"], base, now_ns)
        if bound is None:
            return "invalid_execution_rules_or_fees", evidence
        row["current_bound_price"] = str(bound["price"])
        if ((leg == "buy" and F(bound["price"]) > limit)
                or (leg == "sell" and F(bound["price"]) < limit)):
            return "price_outside_limit", evidence
        # Retain worst-case persisted limit economics, including fresh fees.
        limit_cash = quantity * limit * F(live.instruments[instrument]["native_multiplier"]) * F(market.quote_to_usd)
        bounds[leg] = (limit_cash, limit_cash * F(meta.taker_fee_bps) / 10000)
        row.update(limit_cash_usd=_number(limit_cash), fee_usd=_number(bounds[leg][1]))
        # Match entry sizing's conservative standalone valuation. A higher ask
        # can invalidate headroom even while the original sell limit executes.
        standalone_risk = max(limit_cash, base * F(book.asks[0][0]))
        row["standalone_risk_usd"] = _number(standalone_risk)
        if standalone_risk > risk_budget:
            return "unhedged_headroom_insufficient", evidence
    if abs(live.books[position["buy_id"]].ts_event_ns - live.books[position["sell_id"]].ts_event_ns) > live.plan.settings.max_skew_ms * 1_000_000:
        return "book_skew", evidence
    buy, sell = bounds["buy"], bounds["sell"]
    reserve = buy[0] * F(live.plan.settings.reserve_bps) / 10000
    net = sell[0] - buy[0] - buy[1] - sell[1] - reserve
    required = buy[0] * F(live.policy.entry_edge_bps) / 10000
    evidence.update(net_usd=_number(net), minimum_net_usd=_number(required),
                    net_margin_usd=_number(net - required), reserve_usd=_number(reserve))
    if net < required:
        return "net_edge_insufficient", evidence
    # Released entries belong to this durable pair and are permitted pending
    # IDs, while freshness, post-activity fences and every foreign owner remain
    # admission barriers. Evaluate all scopes rather than reuse creation-time
    # account readiness after durability/coordinator waiting.
    ready = [live._account_ready(position[f"{leg}_id"], now_ns,
                                pending_entry_owner=position["position_id"], allow_preparation=True)
             for leg in ("buy", "sell")]
    domains = {live._account_domain(live.markets[position[f"{leg}_id"]].venue)
               for leg in ("buy", "sell")}
    ready.extend(live._account_domain_ready(domain, now_ns, preparation_owner=position["position_id"])
                 for domain in sorted(domains))
    return (temporal_reason if all(ready) else "account_admission_changed"), evidence


def discard_invalid_entry(live, intent, now_ns, *, dispatched=()):
    order = live.orders.get(intent.client_order_id)
    if intent.action != "submit" or order is None or order["purpose"] != "entry":
        return False
    if order["status"] == "local_not_dispatched":
        return True
    if order["status"] != "dispatch_released":
        return False
    position = live._position(order["position_id"])
    started = time.perf_counter_ns()
    reason, evidence = entry_dispatch_evidence(live, position, now_ns)
    # Include the check itself (depth, exact economics and account admission) in
    # the handoff budget. This clock does not depend on optional timing sampling.
    if reason == "valid":
        now_ns = max(time.time_ns(), now_ns + max(0, time.perf_counter_ns() - started))
        reason, latest = temporal_entry_evidence(live, position, now_ns)
        for leg in ("buy", "sell"):
            evidence[leg].update(latest.pop(leg, {}))
            for source, target in (("source_age_ns", "source_age_ms"), ("receive_age_ns", "receive_age_ms")):
                evidence[leg][target] = _number(F(evidence[leg][source], 1_000_000))
        evidence.update(latest)
    decision = {"result": "allowed" if reason == "valid" else "blocked", "reason": reason, "evidence": evidence}
    # Piggyback on existing receipt/block ledger transactions, never insert a
    # synchronous durability write between the last check and native dispatch.
    order["entry_dispatch"] = decision
    record_entry_stage(intent.request_id, "send_check", wall_ns=now_ns, evidence=evidence, reason=reason)
    if reason == "valid":
        return False
    blocked = []
    for cid in position["entry_ids"]:
        pending = live.orders[cid]
        if cid in dispatched or pending["status"] not in {"prepared", "dispatch_released"}:
            continue
        pending["terminal_ns"], pending["status"] = now_ns, "local_not_dispatched"
        pending["entry_dispatch"] = decision
        blocked.append(cid)
        live.entry_timings.pop(pending["request_id"], None)
        live.entry_clocks.pop(cid, None)
        record_entry_stage(pending["request_id"], "local_blocked", wall_ns=now_ns, reason=reason)
    live.emit("entry_dispatch_blocked", now_ns, client_order_ids=blocked, reason=reason,
              entry_dispatch=decision)
    return True


def entry_pair_result(live, order):
    if order["purpose"] != "entry":
        return
    position = live._position(order["position_id"])
    if position is None:
        return
    orders = [live.orders[cid] for cid in position["entry_ids"]]
    if len(orders) == 2 and all(o["terminal_ns"] is not None for o in orders):
        full = all(F(o["filled_quantity"]) == F(o["quantity"]) for o in orders)
        filled = sum(F(o["filled_quantity"]) > 0 for o in orders)
        record_entry_pair(tuple(o["request_id"] for o in orders),
                          "bilateral_full" if full else "single_leg" if filled == 1 else
                          "bilateral_partial" if filled == 2 else "unfilled")

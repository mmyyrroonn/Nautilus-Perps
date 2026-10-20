"""Offline trade explanations from the durable ledger; never connect or trade."""
from __future__ import annotations

import argparse
from decimal import Decimal, localcontext
import json
from pathlib import Path
import sqlite3

from opportunity_trade_store import FileLock


REASONS = {
    "ioc_no_match": "拒单信息被分类为 IOC 无法立即成交",
    "rate_limit": "拒单信息被分类为请求限流",
    "insufficient_balance": "拒单信息被分类为余额或保证金不足",
    "post_only": "拒单信息被分类为 Post-only 限制",
    "invalid_order": "拒单信息被分类为订单参数或精度限制",
    "venue_other": "收到拒单，但未识别出具体原因",
    "leverage_configuration": "交易所拒绝杠杆配置；重新核对后方可恢复",
    "local_validation": "本地校验拒绝订单",
    "market_not_configured": "市场与交易所配置不匹配",
    "unsupported_venue": "不支持此交易所",
    "unsupported_action": "不支持此操作",
    "missing_order_id": "缺少已持久化的订单编号",
    "invalid_order_id": "订单编号不符合交易所格式",
    "modify_unsupported": "此交易所不支持原地改单",
    "duplicate_order_id": "订单编号已经用于其他提交",
    "order_construction_failed": "本地原生订单构造失败",
    "order_not_owned": "不能修改或撤销不属于策略的订单",
    "dispatch_exception": "提交过程出现异常；是否到达交易所仍未知",
    "receipt_timeout": "等待订单结果超时；不能据此判定订单失败",
    "stale_or_invalid_book": "发送前行情已过期或无效",
    "price_outside_limit": "发送前盘口已不满足原有限价",
    "insufficient_depth": "发送前盘口深度不足",
    "net_edge_insufficient": "发送前预期净价差不足",
    "entry_imbalance": "开仓两腿成交不平衡",
    "exit_imbalance": "平仓两腿成交不平衡",
    "operator_stop": "操作员停止后退出",
    "loss_stop": "触发止损退出",
    "max_loss": "达到最大亏损条件后退出",
    "stop_loss": "触发止损退出",
    "take_profit": "达到止盈条件后退出",
    "positive_after_wait": "等待后满足正收益条件而退出",
    "max_hold": "达到最长持仓时间后退出",
    "execution_halt": "执行异常触发退出",
    "exposure_limit": "总敞口触发限制后退出",
    "unhedged_notional": "未对冲金额触发风险限制",
    "unhedged_duration": "未对冲时间触发风险限制",
    "unhedged_timeout": "未对冲时间触发风险限制",
    "entry_unfilled": "开仓没有成交",
}

EVENTS = {
    "orders_prepared", "order_accepted", "order_terminal", "order_unknown", "fill", "closed",
    "entry_dispatch_blocked", "entry_imbalance", "entry_rebalance_requested", "entry_rebalanced",
    "exit_requested", "unhedged_limit_breached", "order_reconciliation_required",
    "order_reconciliation_recovered", "order_final_quantity_mismatch", "conflicting_fill",
    "previous_session_archived", "leverage_configuration_rejected",
}


def _accounting(economics):
    if not economics:
        return None
    # Explicit context keeps report arithmetic independent of the caller's context.
    with localcontext() as ctx:
        ctx.prec = 60
        entry = economics["actual_entry"]
        long = Decimal(economics["closing_long_cash_usd"]) - Decimal(entry["buy_notional_usd"])
        short = Decimal(entry["sell_notional_usd"]) - Decimal(economics["closing_short_cash_usd"])
        fees = (Decimal(entry["buy_fee_usd"]) + Decimal(entry["sell_fee_usd"])
                + Decimal(economics["closing_fees_usd"]))
        return {"long_leg_gross_usd": str(long), "short_leg_gross_usd": str(short),
                "opening_spread_cashflow_usd": str(Decimal(entry["sell_notional_usd"])
                                                   - Decimal(entry["buy_notional_usd"])),
                "closing_spread_cashflow_usd": str(Decimal(economics["closing_long_cash_usd"])
                                                   - Decimal(economics["closing_short_cash_usd"])),
                "gross_usd": str(long + short), "confirmed_fees_usd": str(fees),
                "accounted_net_usd": str(long + short - fees),
                "fee_confirmed": economics["fee_confirmed"]}


def review_events(events, *, session_id=None, position_id=None, legacy_sessions=None):
    """Preserve session boundaries and distinguish observations from causal inference."""
    trades, owners = {}, {}
    legacy_epoch = 0
    for event in events:
        kind = event["event"]
        if kind not in EVENTS:
            continue
        if kind == "previous_session_archived":
            legacy_epoch += 1
            continue
        session = event.get("session_id") or (legacy_sessions or {}).get(
            legacy_epoch, f"legacy-segment-{legacy_epoch}")
        pid = event.get("position_id")
        cid = event.get("client_order_id")
        cids = event.get("client_order_ids", event.get("orders", [])
                         if kind == "order_reconciliation_recovered" else [])
        if isinstance(cids, str):
            cids = [cids]
        if pid is not None:
            keys = [(session, pid)]
        else:
            keys = list(dict.fromkeys(owners[c] for c in ([cid] if cid else cids) if c in owners))
        for key in keys:
            if session_id is not None and key[0] != session_id:
                continue
            if position_id is not None and str(key[1]) != str(position_id):
                continue
            trade = trades.setdefault(key, {"session_id": key[0], "position_id": key[1],
                "pair": None, "orders": {}, "timeline": [], "closed": None,
                "entry_economics": None, "unresolved_reconciliation": set()})
            if event.get("pair"):
                trade["pair"] = event["pair"]
            if kind == "orders_prepared":
                for order_id in cids:
                    owners[order_id] = key
                    trade["orders"].setdefault(order_id, {"client_order_id": order_id})
                for order in event.get("orders", []):
                    order_id = order["client_order_id"]
                    owners[order_id] = key
                    trade["orders"].setdefault(order_id, {}).update(order)
                if event.get("entry_economics") is not None:
                    trade["entry_economics"] = event["entry_economics"]
            if cid:
                owners[cid] = key
                order = trade["orders"].setdefault(cid, {"client_order_id": cid})
                if kind in {"order_terminal", "order_unknown"}:
                    order["result"] = {name: event.get(name) for name in (
                        "event", "outcome", "native_status", "reason_code", "reason_source", "exchange_error_code",
                        "filled_quantity", "latency_ms")}
                if kind == "leverage_configuration_rejected":
                    order.setdefault("result", {}).update({name: event.get(name) for name in (
                        "native_status", "reason_code", "reason_source", "exchange_error_code")})
                if kind in {"order_reconciliation_required", "order_final_quantity_mismatch", "conflicting_fill"}:
                    trade["unresolved_reconciliation"].add(cid)
            if kind == "order_reconciliation_recovered":
                trade["unresolved_reconciliation"].difference_update(cids)
            trade["timeline"].append({name: event[name] for name in (
                "event", "observed_ns", "client_order_id", "reason", "reason_code", "reason_source",
                "native_status", "exchange_error_code", "outcome", "native_price", "native_quantity", "notional_usd",
                "fee_usd", "fee_confirmed", "latency_ms") if name in event})
            if kind == "closed":
                trade["closed"] = event

    results = []
    for trade in trades.values():
        closed = trade.pop("closed")
        unresolved = sorted(trade.pop("unresolved_reconciliation"))
        accounting = _accounting(closed.get("economics")) if closed else None
        confirmed = bool(closed and closed.get("fee_confirmed") is True)
        net = closed.get("net_usd") if confirmed and not unresolved else None
        findings, missing = [], []
        if accounting:
            if Decimal(accounting["gross_usd"]) < 0:
                findings.append("成交价形成的毛盈亏为负")
            if net is not None and Decimal(net) < 0 and Decimal(accounting["gross_usd"]) >= 0:
                findings.append("已确认手续费使毛盈利变为净亏损")
            if net is not None and Decimal(net) != Decimal(accounting["accounted_net_usd"]):
                missing.append("平仓净额与分项不一致，需要核对账本")
        elif closed:
            missing.append("缺少完整平仓盈亏分项；旧账本不会被补造数据")
        if not closed:
            missing.append("尚无平仓记录，不能判定最终盈亏")
        elif not confirmed:
            missing.append("手续费尚未全部确认，净盈亏不是最终值")
        if unresolved:
            missing.append("成交证据仍待核对，历史平仓净额不能当作最终值")
        for row in trade["timeline"]:
            if row["event"] == "entry_imbalance":
                findings.append(REASONS["entry_imbalance"])
            code = row.get("reason_code") or row.get("reason")
            if code:
                explanation = REASONS.get(code, code)
                if explanation not in findings:
                    findings.append(explanation)
            if row["event"] == "order_terminal" and row.get("outcome") == "rejected" and not row.get("reason_code"):
                missing.append("存在拒单记录，但未保留具体拒单原因")
            if row["event"] == "order_terminal" and row.get("native_status") in {"canceled", "expired"}:
                findings.append("订单已取消或到期；该状态本身不能证明盘口变化或网络延迟是原因")
        if closed:
            reason = closed.get("reason")
            if reason:
                findings.append("退出记录：" + REASONS.get(reason, reason))
        results.append({**trade, "status": "closed" if closed else "not_closed",
            "net_usd": net, "provisional_net_usd": closed.get("provisional_net_usd") if closed else None,
            "unresolved_reconciliation": unresolved,
            "accounting": accounting, "observed_findings": list(dict.fromkeys(findings)),
            "missing_evidence": list(dict.fromkeys(missing)),
            "causal_inference": "未推断网络延迟、抢单或行情变化为根因；需要额外证据"})
    return {"schema_version": 1, "scope": "strategy_trade_review", "trades": results,
        "limitations": ["净盈亏不包含资金费与借贷成本",
                        "拒单分类来自原生事件文字匹配，不是交易所根因保证；不保存原始响应",
                        "订单延时包含本地准备与处理时间，不等于网络往返时间",
                        "预期入场经济数据基于保守限价，不是成交时盘口或滑点测量",
                        "只解释已记录的交易；下单前被过滤的候选不在报告范围内"]}


def review_ledger(path, **filters):
    path = Path(path).resolve(strict=True)
    if not path.is_file():
        raise ValueError("ledger must be a SQLite file")
    # Respect the existing owner lock: never hold a SQLite reader against a live writer.
    try:
        lock = FileLock(path.with_suffix(path.suffix + ".lock"))
    except OSError as exc:
        raise ValueError("ledger is in use; review a stopped ledger or a consistent offline backup") from exc
    try:
        connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=0)
        try:
            connection.execute("PRAGMA query_only=ON")
            # Older events lack session IDs. Archive boundaries and the current
            # snapshot prove their sessions, including a resume across this upgrade.
            legacy_sessions = {}
            epoch = 0
            for (payload,) in connection.execute(
                    "SELECT payload FROM events WHERE kind='previous_session_archived' ORDER BY sequence"):
                saved = json.loads(payload).get("previous_session", {})
                prior = saved.get("live") or saved
                if prior.get("session_id"):
                    legacy_sessions[epoch] = prior["session_id"]
                epoch += 1
            row = connection.execute("SELECT snapshot FROM state WHERE id=1").fetchone()
            if row:
                saved = json.loads(row[0])
                current = saved.get("live") or saved
                if current.get("session_id"):
                    legacy_sessions[epoch] = current["session_id"]
            placeholders = ",".join("?" for _ in EVENTS)
            rows = connection.execute(
                f"SELECT payload FROM events WHERE kind IN ({placeholders}) ORDER BY sequence", tuple(EVENTS))
            return review_events((json.loads(row[0]) for row in rows), legacy_sessions=legacy_sessions, **filters)
        finally:
            connection.close()
    finally:
        lock.close()


def render_markdown(report):
    lines = ["# 逐笔交易复盘", "", f"交易数：{len(report['trades'])}", ""]
    for trade in report["trades"]:
        lines.extend([f"## {trade['session_id']} / {trade['position_id']}", "",
                      f"交易对：{trade['pair']}；状态：{'已平仓' if trade['status'] == 'closed' else '尚无平仓记录'}", "",
                      f"已确认净盈亏（USD）：{trade['net_usd'] if trade['net_usd'] is not None else '未确认'}", ""])
        if trade["accounting"]:
            a = trade["accounting"]
            lines.extend([f"多腿毛盈亏：{a['long_leg_gross_usd']}；空腿毛盈亏：{a['short_leg_gross_usd']}；"
                          f"已确认手续费：{a['confirmed_fees_usd']}", "",
                          f"开仓价差现金流：{a['opening_spread_cashflow_usd']}；"
                          f"平仓及再平衡价差现金流：{a['closing_spread_cashflow_usd']}", ""])
        lines.extend("- 已记录：" + text for text in trade["observed_findings"])
        lines.extend("- 证据缺口：" + text for text in trade["missing_evidence"])
        lines.extend(["", trade["causal_inference"], ""])
        for cid, order in trade["orders"].items():
            result = order.get("result", {})
            purpose = {"entry": "开仓", "exit": "平仓", "rebalance": "再平衡"}.get(order.get("purpose"), "未知阶段")
            status = result.get("native_status") or result.get("outcome") or result.get("event")
            status = {"rejected": "拒单", "denied": "拒绝", "canceled": "已取消", "expired": "已到期",
                      "filled": "完全成交", "terminal": "已结束", "order_unknown": "结果未知"}.get(status, "未记录终态")
            if (result.get("event") == "order_terminal" and result.get("filled_quantity") is not None
                    and order.get("quantity") is not None
                    and Decimal(result["filled_quantity"]) == Decimal(order["quantity"])):
                status = "完全成交"
            lines.extend([f"- 订单 `{cid}`：{order.get('venue', '未知交易所')} / {purpose} / {status}；"
                          f"原因：{REASONS.get(result.get('reason_code'), result.get('reason_code')) or '未记录'}"])
        lines.append("")
    lines.extend(["## 范围与限制", "", *("- " + text for text in report["limitations"]), ""])
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("ledger", type=Path, help="stopped ledger or consistent offline backup")
    parser.add_argument("--session")
    parser.add_argument("--position", type=int)
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.output and str(args.output.resolve()).casefold() in {
                str(args.ledger.resolve()).casefold() + suffix for suffix in ("", ".lock", "-wal", "-shm", "-journal")}:
            raise ValueError("output must not overwrite the ledger or its sidecars")
        report = review_ledger(args.ledger, session_id=args.session, position_id=args.position)
        document = (json.dumps(report, ensure_ascii=False, indent=2) + "\n"
                    if args.format == "json" else render_markdown(report))
        if args.output:
            args.output.write_text(document, encoding="utf-8")
        else:
            print(document)
    except (OSError, ValueError, sqlite3.Error) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

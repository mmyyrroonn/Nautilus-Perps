"""Offline explanations, durable evidence, and no extra ledger transactions."""
from copy import deepcopy
from dataclasses import replace
from decimal import Decimal as D
import json
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_review import main, render_markdown, review_events, review_ledger
from opportunity_trade_store import TradeStore
from test_opportunity_live import MemoryStore, accounts, fill, mark, receipt, state, submissions
from test_opportunity_trade import NOW, publish


def closed_trade(*, fee="0.01", exit_fee="0.02", exit_bps=10):
    live, events = state()
    publish(live)
    entries = submissions(live)
    for order in entries:
        fill(live, order, NOW + 100_000_000, fee=fee)
    accounts(live, NOW + 100_000_002)
    live.on_evaluation_timer(NOW + 100_000_002)
    live.drain_intents()
    # A hard stop permits closing even when commission evidence is incomplete.
    live.stop_entries(NOW + 200_000_000)
    now, exits = mark(live, exit_bps)
    assert len(exits) == 2
    for order in exits:
        fill(live, order, now + 100_000_000, fee=exit_fee)
    accounts(live, now + 100_000_002)
    live.on_evaluation_timer(now + 100_000_002)
    assert live.completed == 1
    return live, events


def test_actual_trade_report_reconciles_cashflows_and_all_four_fees():
    live, events = closed_trade(exit_fee="1")
    trade = review_events(events)["trades"][0]
    assert trade["net_usd"] == live.document()["realized_net_usd"]
    assert D(trade["accounting"]["confirmed_fees_usd"]) == D("2.02")
    assert D(trade["accounting"]["gross_usd"]) > 0
    assert D(trade["net_usd"]) < 0
    assert "已确认手续费使毛盈利变为净亏损" in trade["observed_findings"]
    assert len(trade["orders"]) == 4
    assert all(o["book_source_ns"] for o in trade["orders"].values())
    assert trade["entry_economics"] is not None
    assert "未推断" in trade["causal_inference"]
    assert "已确认手续费" in render_markdown(review_events(events))


def test_missing_commission_never_becomes_final_zero_fee_profit():
    _, events = closed_trade(fee=None)
    trade = review_events(events)["trades"][0]
    assert trade["net_usd"] is None
    assert trade["accounting"]["fee_confirmed"] is False
    assert any("手续费尚未全部确认" in text for text in trade["missing_evidence"])


def test_losing_prices_and_fee_rebates_are_not_mislabeled_fee_loss():
    _, events = closed_trade(fee="-0.001", exit_fee="0", exit_bps=-10)
    trade = review_events(events)["trades"][0]
    assert D(trade["net_usd"]) < 0
    assert D(trade["accounting"]["confirmed_fees_usd"]) < 0
    assert "成交价形成的毛盈亏为负" in trade["observed_findings"]
    assert "已确认手续费使毛盈利变为净亏损" not in trade["observed_findings"]


def test_reason_persists_in_existing_transaction_and_duplicate_is_idempotent():
    store = MemoryStore()
    live, events = state(store=store)
    publish(live)
    order = submissions(live)[0]
    writes = len(store.writes)
    result = replace(receipt(order, "rejected"), reason_code="insufficient_balance",
                     reason_source="native_event", native_status="rejected")
    live.on_receipt(result, NOW + 1)
    # The existing terminal and account-read transactions are the only writes.
    assert [batch[1][0]["event"] for batch in store.writes[writes:]] == [
        "order_terminal", "account_read_requested"]
    terminal = next(e for e in events if e["event"] == "order_terminal")
    assert terminal["reason_code"] == "insufficient_balance"
    assert store.saved["orders"][order.client_order_id]["terminal_diagnostic"]["reason_code"] == "insufficient_balance"
    count = len(store.writes)
    live.on_receipt(result, NOW + 2)
    assert len(store.writes) == count
    trade = review_events(events)["trades"][0]
    assert any("保证金不足" in text for text in trade["observed_findings"])


def test_timeout_does_not_claim_exchange_rejected_or_final_loss():
    live, events = state()
    publish(live)
    order = submissions(live)[0]
    live.on_receipt(replace(receipt(order, "unknown"), reason_code="receipt_timeout",
                           reason_source="python_timeout"), NOW + 1)
    trade = review_events(events)["trades"][0]
    assert trade["net_usd"] is None
    assert any("不能据此判定订单失败" in text for text in trade["observed_findings"])


def test_sessions_with_same_position_id_and_legacy_segments_stay_separate():
    _, events = closed_trade()
    second = deepcopy(events)
    for row in second:
        row["session_id"] = "second-session"
        if "client_order_id" in row:
            row["client_order_id"] += "second"
        if "client_order_ids" in row:
            row["client_order_ids"] = [cid + "second" for cid in row["client_order_ids"]]
        for order in row.get("orders", []):
            order["client_order_id"] += "second"
    report = review_events(events + second)
    assert len(report["trades"]) == 2
    assert len(review_events(events + second, session_id="second-session", position_id=1)["trades"]) == 1
    legacy = [{"event": "closed", "position_id": 1, "net_usd": "-1", "fee_confirmed": True},
              {"event": "previous_session_archived"},
              {"event": "closed", "position_id": 1, "net_usd": "2", "fee_confirmed": True}]
    trades = review_events(legacy)["trades"]
    assert len(trades) == 2 and [t["net_usd"] for t in trades] == ["-1", "2"]
    assert all(t["accounting"] is None and t["missing_evidence"] for t in trades)


def test_cli_refuses_running_ledger_and_preserves_stopped_database(tmp_path):
    path = tmp_path / "ledger.sqlite3"
    _, events = closed_trade()
    store = TradeStore(path, "synthetic")
    store.save({}, events)
    try:
        with pytest.raises(ValueError, match="in use"):
            review_ledger(path)
    finally:
        store.close()
    before = path.read_bytes()
    output = tmp_path / "review.json"
    main([str(path), "--format", "json", "--output", str(output)])
    assert len(json.loads(output.read_text(encoding="utf-8"))["trades"]) == 1
    assert path.read_bytes() == before
    with pytest.raises(SystemExit):
        main([str(path), "--output", str(path)])
    assert path.read_bytes() == before


def test_local_entry_block_can_be_joined_through_order_ids():
    live, events = state()
    publish(live)
    orders = submissions(live)
    events.append({"event": "entry_dispatch_blocked", "observed_ns": NOW + 1,
                   "client_order_ids": [o.client_order_id for o in orders],
                   "reason": "price_outside_limit", "session_id": live.session_id})
    trade = review_events(events)["trades"][0]
    assert "发送前盘口已不满足原有限价" in trade["observed_findings"]


def test_upgrade_resume_and_archive_use_proven_session_ids(tmp_path):
    live, events = closed_trade()
    # Old preparation/fill events lack session IDs, new terminal/closed events have them.
    for row in events:
        if row["event"] != "closed":
            row.pop("session_id", None)
    path = tmp_path / "legacy.sqlite3"
    store = TradeStore(path, "synthetic")
    store.save({"live": live.snapshot()}, events)
    store.close()
    report = review_ledger(path, session_id=live.session_id)
    assert len(report["trades"]) == 1 and len(report["trades"][0]["orders"]) == 4
    store = TradeStore(path, "synthetic")
    store.save({"live": {"session_id": "next"}}, [{"event": "previous_session_archived",
        "observed_ns": NOW + 9_000_000_000, "previous_session": {"live": live.snapshot()}}])
    store.close()
    report = review_ledger(path, session_id=live.session_id)
    assert len(report["trades"]) == 1 and len(report["trades"][0]["orders"]) == 4


def test_unresolved_execution_proof_prevents_final_pnl_until_recovery():
    live, events = closed_trade()
    cid = next(iter(live.orders))
    events.append({"event": "order_reconciliation_required", "session_id": live.session_id,
                   "observed_ns": NOW + 10_000_000_000, "client_order_id": cid})
    trade = review_events(events)["trades"][0]
    assert trade["net_usd"] is None and trade["unresolved_reconciliation"] == [cid]
    events.append({"event": "order_reconciliation_recovered", "session_id": live.session_id,
                   "observed_ns": NOW + 11_000_000_000, "orders": {cid: {"since_ns": NOW}}})
    trade = review_events(events)["trades"][0]
    assert trade["net_usd"] == live.document()["realized_net_usd"]

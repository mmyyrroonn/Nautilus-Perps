"""Read-only SQLite ledger inspection and net PnL review CLI for Route A Carry."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import sys
from typing import Any, Mapping

from .config import CarryConfigError, load_carry_plan

_MAKER_ROLES = frozenset({"MAKER_OPEN", "MAKER_CLOSE"})
_PNL_QUANT = Decimal("0.0001")


def _q4(val: Decimal) -> Decimal:
    return val.quantize(_PNL_QUANT)


def _open_readonly_sqlite(path: Path) -> sqlite3.Connection:
    resolved = Path(path).resolve()
    if not resolved.is_file():
        raise FileNotFoundError(f"state database not found: {resolved}")
    conn = sqlite3.connect(f"{resolved.as_uri()}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _estimate_position_order_fees(
    orders: list[sqlite3.Row],
    *,
    has_order_fee_col: bool,
    maker_fees: Mapping[str, Decimal] | None,
    taker_fees: Mapping[str, Decimal] | None,
) -> Decimal | None:
    total = Decimal("0")
    used_any = False
    for row in orders:
        filled = Decimal(str(row["filled_base_qty"]))
        if filled <= 0:
            continue
        raw_fee = row["fee_usd"] if has_order_fee_col else None
        if raw_fee is not None and str(raw_fee).strip() != "":
            total += max(Decimal("0"), Decimal(str(raw_fee)))
            used_any = True
        elif maker_fees is not None and taker_fees is not None and row["price"] is not None:
            px = Decimal(str(row["price"]))
            venue = str(row["venue"])
            role = str(row["role"])
            bps = maker_fees.get(venue, Decimal("0")) if role in _MAKER_ROLES else taker_fees.get(venue, Decimal("0"))
            total += (filled * px * bps) / Decimal("10000")
            used_any = True
    if not used_any:
        return None
    return _q4(total)


def load_review_report(
    state_path: Path,
    *,
    config_path: Path | None = None,
    now_s: int | None = None,
) -> dict[str, Any]:
    """Read Carry SQLite ledger strictly read-only without acquiring FileLock."""
    maker_fees: dict[str, Decimal] | None = None
    taker_fees: dict[str, Decimal] | None = None
    if config_path is not None:
        plan = load_carry_plan(config_path)
        maker_fees = dict(plan.policy.maker_fee_bps_by_venue)
        taker_fees = dict(plan.policy.taker_fee_bps_by_venue)

    conn = _open_readonly_sqlite(state_path)
    try:
        tables = {
            str(r[0])
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        pos_cols = (
            {str(r["name"]) for r in conn.execute("PRAGMA table_info(positions)").fetchall()}
            if "positions" in tables
            else set()
        )
        ord_cols = (
            {str(r["name"]) for r in conn.execute("PRAGMA table_info(orders)").fetchall()}
            if "orders" in tables
            else set()
        )
        has_new_pos_cols = {"price_pnl_usd", "funding_pnl_usd", "fee_usd"} <= pos_cols
        has_order_fee_col = "fee_usd" in ord_cols
        has_funding_table = "funding_settlements" in tables
        schema_legacy = not (has_new_pos_cols and has_order_fee_col and has_funding_table)

        meta: dict[str, Any] = {}
        if "meta" in tables:
            mrow = conn.execute("SELECT * FROM meta WHERE id=1").fetchone()
            if mrow is not None:
                meta = {k: mrow[k] for k in mrow.keys()}

        runtime_flags: dict[str, str] = {}
        if "runtime_flags" in tables:
            for r in conn.execute("SELECT key, value FROM runtime_flags ORDER BY key ASC").fetchall():
                runtime_flags[str(r["key"])] = str(r["value"])
        halts = {k: v for k, v in runtime_flags.items() if k.startswith("HALTED_")}

        daily_stats: list[dict[str, Any]] = []
        if "daily_stats" in tables:
            for r in conn.execute(
                "SELECT utc_day, realized_pnl_usd, consecutive_unhedged FROM daily_stats ORDER BY utc_day ASC"
            ).fetchall():
                daily_stats.append(
                    {
                        "utc_day": str(r["utc_day"]),
                        "realized_pnl_usd": str(_q4(Decimal(str(r["realized_pnl_usd"])))),
                        "consecutive_unhedged": int(r["consecutive_unhedged"]),
                    }
                )

        funding_by_pid: dict[str, tuple[Decimal, int]] = {}
        total_settlements = 0
        if has_funding_table:
            for r in conn.execute("SELECT position_id, funding_usd FROM funding_settlements").fetchall():
                pid = str(r["position_id"])
                prev_sum, prev_cnt = funding_by_pid.get(pid, (Decimal("0"), 0))
                funding_by_pid[pid] = (prev_sum + Decimal(str(r["funding_usd"])), prev_cnt + 1)
                total_settlements += 1

        orders_by_pid: dict[str, list[sqlite3.Row]] = {}
        if "orders" in tables:
            for r in conn.execute("SELECT * FROM orders ORDER BY created_ts ASC, client_order_id ASC").fetchall():
                orders_by_pid.setdefault(str(r["position_id"]), []).append(r)

        pos_rows = (
            conn.execute("SELECT * FROM positions ORDER BY opened_ts ASC, position_id ASC").fetchall()
            if "positions" in tables
            else []
        )

        positions_out: list[dict[str, Any]] = []
        open_count = 0
        closed_count = 0
        tot_price = Decimal("0")
        tot_funding = Decimal("0")
        tot_fee = Decimal("0")
        tot_net = Decimal("0")

        for pos in pos_rows:
            pid = str(pos["position_id"])
            status = str(pos["status"])
            if status == "CLOSED":
                closed_count += 1
            else:
                open_count += 1

            opened_ts = int(pos["opened_ts"])
            closed_ts = int(pos["closed_ts"]) if pos["closed_ts"] is not None else None
            hold_hours = (
                round((closed_ts - opened_ts) / 3600.0, 2)
                if closed_ts is not None and closed_ts >= opened_ts
                else None
            )

            persisted_realized = Decimal(str(pos["realized_pnl_usd"]))
            persisted_price = Decimal(str(pos["price_pnl_usd"])) if has_new_pos_cols else Decimal("0")
            persisted_funding = Decimal(str(pos["funding_pnl_usd"])) if has_new_pos_cols else Decimal("0")
            persisted_fee = Decimal(str(pos["fee_usd"])) if has_new_pos_cols else Decimal("0")

            table_funding, settle_cnt = funding_by_pid.get(pid, (Decimal("0"), 0))
            est_order_fee = _estimate_position_order_fees(
                orders_by_pid.get(pid, []),
                has_order_fee_col=has_order_fee_col,
                maker_fees=maker_fees,
                taker_fees=taker_fees,
            )

            is_legacy_row = (not has_new_pos_cols) or (
                status == "CLOSED"
                and persisted_price == Decimal("0")
                and persisted_funding == Decimal("0")
                and persisted_fee == Decimal("0")
                and persisted_realized != Decimal("0")
            )

            if status == "CLOSED":
                if is_legacy_row:
                    price_pnl = persisted_realized
                    funding_pnl = table_funding if has_funding_table else Decimal("0")
                    fee_usd = est_order_fee if est_order_fee is not None else Decimal("0")
                    net_pnl = price_pnl + funding_pnl - fee_usd
                else:
                    price_pnl = persisted_price
                    funding_pnl = persisted_funding
                    fee_usd = persisted_fee
                    net_pnl = price_pnl + funding_pnl - fee_usd
            else:
                price_pnl = Decimal("0")
                funding_pnl = table_funding if has_funding_table else persisted_funding
                fee_usd = est_order_fee if est_order_fee is not None else persisted_fee
                net_pnl = price_pnl + funding_pnl - fee_usd

            price_q = _q4(price_pnl)
            funding_q = _q4(funding_pnl)
            fee_q = _q4(fee_usd)
            net_q = _q4(net_pnl)

            tot_price += price_q
            tot_funding += funding_q
            tot_fee += fee_q
            tot_net += net_q

            positions_out.append(
                {
                    "position_id": pid,
                    "symbol": str(pos["symbol"]),
                    "long_venue": str(pos["long_venue"]),
                    "short_venue": str(pos["short_venue"]),
                    "status": status,
                    "opened_ts": opened_ts,
                    "closed_ts": closed_ts,
                    "hold_hours": hold_hours,
                    "long_base_qty": str(pos["long_base_qty"]),
                    "short_base_qty": str(pos["short_base_qty"]),
                    "entry_basis_bps": float(pos["entry_basis_bps"]),
                    "price_pnl_usd": str(price_q),
                    "funding_pnl_usd": str(funding_q),
                    "fee_usd": str(fee_q),
                    "net_pnl_usd": str(net_q),
                    "realized_pnl_usd": str(_q4(persisted_realized)),
                    "close_reason": pos["close_reason"],
                    "settlement_count": settle_cnt,
                }
            )

        ref_ts = int(datetime.now(timezone.utc).timestamp()) if now_s is None else int(now_s)
        today_day = datetime.fromtimestamp(ref_ts, tz=timezone.utc).strftime("%Y-%m-%d")
        today_stats = next(
            (d for d in daily_stats if d["utc_day"] == today_day),
            {"utc_day": today_day, "realized_pnl_usd": "0.0000", "consecutive_unhedged": 0},
        )

        return {
            "state_path": str(Path(state_path).resolve()),
            "schema_legacy": schema_legacy,
            "meta": meta,
            "halts": halts,
            "runtime_flags": runtime_flags,
            "today_stats": today_stats,
            "daily_stats": daily_stats,
            "summary": {
                "open_positions_count": open_count,
                "closed_positions_count": closed_count,
                "total_positions_count": len(positions_out),
                "funding_settlements_count": total_settlements,
                "total_price_pnl_usd": str(_q4(tot_price)),
                "total_funding_pnl_usd": str(_q4(tot_funding)),
                "total_fee_usd": str(_q4(tot_fee)),
                "total_net_pnl_usd": str(_q4(tot_net)),
            },
            "positions": positions_out,
        }
    finally:
        conn.close()


def format_review_text(report: Mapping[str, Any]) -> str:
    """Render a human-readable portfolio and per-position PnL audit table."""
    summary = report["summary"]
    today = report["today_stats"]
    halts = report["halts"]
    halt_str = ", ".join(f"{k}={v}" for k, v in sorted(halts.items())) if halts else "NONE"
    lines = [
        f"Carry Ledger Review: {report['state_path']}",
        f"Schema: {'legacy (degraded read)' if report['schema_legacy'] else 'current'} | Halts: {halt_str}",
        (
            f"Positions: open={summary['open_positions_count']} closed={summary['closed_positions_count']} "
            f"total={summary['total_positions_count']} | Funding settlements={summary['funding_settlements_count']}"
        ),
        (
            f"Portfolio PnL (USD): price={summary['total_price_pnl_usd']} "
            f"funding={summary['total_funding_pnl_usd']} "
            f"fee=-{summary['total_fee_usd']} "
            f"net={summary['total_net_pnl_usd']}"
        ),
        (
            f"Daily Stats ({today['utc_day']}): realized_pnl_usd={today['realized_pnl_usd']} "
            f"consecutive_unhedged={today['consecutive_unhedged']}"
        ),
        "",
    ]
    positions = report["positions"]
    if not positions:
        lines.append("No positions recorded.")
        return "\n".join(lines)

    header = (
        f"{'POSITION_ID':<28} {'SYM':<8} {'PAIR':<14} {'STATUS':<8} "
        f"{'HOLD_H':>6} {'PRICE':>10} {'FUNDING':>10} {'FEE':>10} {'NET_PNL':>10}  REASON"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for p in positions:
        pair = f"{p['long_venue']}->{p['short_venue']}"
        hold = f"{p['hold_hours']:.2f}" if p["hold_hours"] is not None else "-"
        reason = p["close_reason"] or "-"
        lines.append(
            f"{p['position_id']:<28} {p['symbol']:<8} {pair:<14} {p['status']:<8} "
            f"{hold:>6} {p['price_pnl_usd']:>10} {p['funding_pnl_usd']:>10} "
            f"{p['fee_usd']:>10} {p['net_pnl_usd']:>10}  {reason}"
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only Route A Carry SQLite ledger & net PnL review")
    parser.add_argument("--state", type=Path, required=True, help="Path to carry paper.sqlite3 or live.sqlite3")
    parser.add_argument("--config", type=Path, default=None, help="Optional carry-*.toml for legacy fee estimation")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable JSON output")
    args = parser.parse_args(argv)

    try:
        report = load_review_report(args.state, config_path=args.config)
    except (FileNotFoundError, CarryConfigError, sqlite3.Error) as exc:
        print(f"[carry.review] Error: {exc}", file=sys.stderr)
        return 2

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(format_review_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

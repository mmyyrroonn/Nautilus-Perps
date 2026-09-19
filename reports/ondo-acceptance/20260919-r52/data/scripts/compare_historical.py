#!/usr/bin/env python3
"""Compare the P2-era saved depth analysis with the current analyzer's replay of
the same immutable tape.

The comparison is deliberately mechanical: it reads both summary CSVs, matches
rows on (symbol, sell_venue, buy_venue, notional_usd) and reports every field
that differs. It does not judge profitability; it only names which analyzer rule
changed and what the numbers became.

Read-only. Writes ``old_vs_new.json``, ``old_vs_new.csv`` and ``old_vs_new.md``.
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path


def load(path: Path) -> dict[tuple[str, str, str, str], dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return {
            (row["symbol"], row["sell_venue"], row["buy_venue"], row["notional_usd"]): row
            for row in csv.DictReader(handle)
        }


def parse_reasons(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for chunk in (text or "").split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        name, _, value = chunk.partition("=")
        try:
            counts[name.strip()] = int(value)
        except ValueError:
            continue
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old", type=Path, required=True)
    parser.add_argument("--new", type=Path, required=True)
    parser.add_argument("--out-dir", type=Path, required=True)
    args = parser.parse_args()

    old = load(args.old)
    new = load(args.new)
    keys = sorted(set(old) | set(new), key=lambda key: (key[0], key[1], key[2], int(key[3])))

    compare_fields = [
        "samples",
        "quality_pass",
        "pass",
        "reject",
        "hits",
        "executable_hits",
        "common_step",
        "entry_fees_bps",
        "gross_entry_bps_median",
        "entry_after_fees_bps_median",
        "event_age_sell_ms_max",
        "event_age_buy_ms_max",
        "reject_reasons",
    ]

    rows = []
    for key in keys:
        old_row = old.get(key, {})
        new_row = new.get(key, {})
        differences = {}
        for field in compare_fields:
            old_value = old_row.get(field)
            new_value = new_row.get(field)
            if old_value != new_value:
                differences[field] = {"old": old_value, "new": new_value}
        old_reasons = parse_reasons(old_row.get("reject_reasons", ""))
        new_reasons = parse_reasons(new_row.get("reject_reasons", ""))
        reason_names = sorted(set(old_reasons) | set(new_reasons))
        reason_delta = {
            name: {"old": old_reasons.get(name, 0), "new": new_reasons.get(name, 0)}
            for name in reason_names
            if old_reasons.get(name, 0) != new_reasons.get(name, 0)
        }
        rows.append(
            {
                "symbol": key[0],
                "sell_venue": key[1],
                "buy_venue": key[2],
                "notional_usd": key[3],
                "old_present": bool(old_row),
                "new_present": bool(new_row),
                "differences": differences,
                "reason_delta": reason_delta,
                "new_step_origin": new_row.get("step_origin"),
                "new_common_step_note": new_row.get("common_step_note"),
            }
        )

    # The rule-change summary: every old pass that the new analyzer refuses, named.
    step_shift = []
    for row in rows:
        old_pass = int(row["differences"].get("pass", {}).get("old", 0) or 0)
        new_step_unknown = int(
            row["reason_delta"].get("quantity_step_unknown", {}).get("new", 0) or 0
        )
        if old_pass or new_step_unknown:
            step_shift.append(
                {
                    "symbol": row["symbol"],
                    "direction": f"{row['sell_venue']}>{row['buy_venue']}",
                    "notional_usd": row["notional_usd"],
                    "old_pass": old_pass,
                    "new_pass": int(row["differences"].get("pass", {}).get("new", 0) or 0),
                    "new_quantity_step_unknown": new_step_unknown,
                    "shift_is_exact": old_pass == new_step_unknown,
                }
            )

    # Non-step reject counters must be identical, or the comparison is not clean.
    non_step_mismatches = []
    for row in rows:
        for name, delta in row["reason_delta"].items():
            if name == "quantity_step_unknown":
                continue
            if delta["old"] != delta["new"]:
                non_step_mismatches.append(
                    {"row": row, "reason": name, **delta}
                )

    summary = {
        "old_summary": str(args.old),
        "new_summary": str(args.new),
        "rows_compared": len(rows),
        "rows_with_field_differences": sum(1 for row in rows if row["differences"]),
        "step_shift": step_shift,
        "step_shift_exact_rows": sum(1 for item in step_shift if item["shift_is_exact"]),
        "non_step_reject_mismatches": non_step_mismatches,
        "rows": rows,
    }

    out_dir = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "old_vs_new.json").write_text(
        json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )

    with (out_dir / "old_vs_new.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "symbol",
                "sell_venue",
                "buy_venue",
                "notional_usd",
                "old_pass",
                "new_pass",
                "old_quality_pass",
                "new_quality_pass",
                "old_step",
                "new_step",
                "new_step_origin",
                "old_entry_fees_bps",
                "new_entry_fees_bps",
                "old_gross_median",
                "new_gross_median",
                "old_after_fees_median",
                "new_after_fees_median",
                "old_event_age_sell_max",
                "new_event_age_sell_max",
                "old_event_age_buy_max",
                "new_event_age_buy_max",
                "old_reject_reasons",
                "new_reject_reasons",
            ]
        )
        for row in rows:
            old_row = old.get((row["symbol"], row["sell_venue"], row["buy_venue"], row["notional_usd"]), {})
            new_row = new.get((row["symbol"], row["sell_venue"], row["buy_venue"], row["notional_usd"]), {})
            writer.writerow(
                [
                    row["symbol"], row["sell_venue"], row["buy_venue"], row["notional_usd"],
                    old_row.get("pass"), new_row.get("pass"),
                    old_row.get("quality_pass"), new_row.get("quality_pass"),
                    old_row.get("common_step"), new_row.get("common_step"),
                    new_row.get("step_origin"),
                    old_row.get("entry_fees_bps"), new_row.get("entry_fees_bps"),
                    old_row.get("gross_entry_bps_median"), new_row.get("gross_entry_bps_median"),
                    old_row.get("entry_after_fees_bps_median"), new_row.get("entry_after_fees_bps_median"),
                    old_row.get("event_age_sell_ms_max"), new_row.get("event_age_sell_ms_max"),
                    old_row.get("event_age_buy_ms_max"), new_row.get("event_age_buy_ms_max"),
                    old_row.get("reject_reasons"), new_row.get("reject_reasons"),
                ]
            )

    print(json.dumps({k: v for k, v in summary.items() if k != "rows"}, indent=2, ensure_ascii=False))
    print(f"[compare] wrote {out_dir / 'old_vs_new.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

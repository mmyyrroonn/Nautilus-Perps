#!/usr/bin/env python3
"""Assert that the synthetic fixtures (real analyzer runs over generated tapes)
produced exactly the rule behaviour they were built to exercise.

Read-only over the fixture outputs. Writes ``verification.json`` next to the
``results.json`` and exits non-zero if any assertion fails.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def load_report(root: Path, name: str) -> dict:
    return json.loads((root / name / "depth-analysis" / "ondo_depth.json").read_text(encoding="utf-8"))


def direction(report: dict, sell: str, buy: str) -> dict:
    market = report["markets"][0]
    return next(d for d in market["directions"] if d["direction"] == f"{sell}>{buy}")


def totals(report: dict, sell: str, buy: str) -> dict:
    return direction(report, sell, buy)["totals"]


def examples(report: dict, sell: str, buy: str) -> list[dict]:
    return direction(report, sell, buy)["notionals"][0].get("examples", [])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    root = args.root

    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    halt = load_report(root, "halt-survives-disconnect")
    halt_agg = totals(halt, "ONDO", "HL")
    check("halt.pass_before_halt", halt_agg["pass"] == 1, f"pass={halt_agg['pass']}")
    check("halt.disconnected_axis", halt_agg["reject_reasons"].get("disconnected") == 2,
          f"reasons={halt_agg['reject_reasons']}")
    check("halt.market_axis", halt_agg["reject_reasons"].get("market_halted") == 1,
          "snapshot_ready never lifted the halt")
    check("halt.no_metadata_stale",
          "metadata_stale" not in halt_agg["reject_reasons"], "no stale metadata in this tape")

    recovery = load_report(root, "first-frame-recovery")
    rows = examples(recovery, "ONDO", "HL")
    check("recovery.two_rows", len(rows) == 2, f"rows={len(rows)}")
    check(
        "recovery.first_frame_own_times",
        len(rows) >= 1 and (rows[0]["arrival_seq"], rows[0]["event_age_sell_ms"],
                            rows[0]["event_age_buy_ms"]) == (4, "0", "0"),
        f"first={rows[0] if rows else None}",
    )
    check(
        "recovery.quote_does_not_refresh",
        len(rows) >= 2 and (rows[1]["arrival_seq"], rows[1]["event_age_sell_ms"],
                            rows[1]["event_age_buy_ms"]) == (9, "1000", "1000"),
        f"second={rows[1] if len(rows) > 1 else None}",
    )

    meta = load_report(root, "metadata-fee-update")
    meta_rows = examples(meta, "ONDO", "HL")
    fees = [(row["arrival_seq"], row["entry_fees_bps"]) for row in meta_rows]
    check("metadata.per_arrival_pricing", fees == [(4, "3.4"), (6, "100.9"), (7, "100.9")],
          f"fees={fees}")
    snapshot = meta["fee_snapshot"]["NVDA"]["ONDO"]
    check("metadata.snapshot_lists_both",
          snapshot.get("taker_fee_bps_values") == ["2.5", "100"],
          f"taker_fee_bps_values={snapshot.get('taker_fee_bps_values')}")

    legacy = load_report(root, "legacy-no-size-increment")
    legacy_agg = totals(legacy, "ONDO", "HL")
    check("legacy.no_pass", legacy_agg["pass"] == 0, f"pass={legacy_agg['pass']}")
    check("legacy.quantity_step_unknown",
          legacy_agg["reject_reasons"].get("quantity_step_unknown") == 1,
          f"reasons={legacy_agg['reject_reasons']}")
    check("legacy.step_origin",
          all(n.get("step_origin") == "quantity_step_unknown"
              for n in direction(legacy, "ONDO", "HL")["notionals"]),
          "step_origin stays quantity_step_unknown")

    gap = load_report(root, "gap-incomplete")
    gap_agg = totals(gap, "ONDO", "HL")
    check("gap.recording_gap_named", gap_agg["reject_reasons"].get("recording_gap") == 1,
          f"reasons={gap_agg['reject_reasons']}")
    check("gap.no_book_after_hole", gap_agg["reject_reasons"].get("no_book") == 2,
          f"reasons={gap_agg['reject_reasons']}")
    tape = gap["tapes"][0]
    check("gap.tape_incomplete", tape["complete"] is False and tape["gaps"] == 1
          and tape["dropped"] == 3,
          f"complete={tape['complete']} gaps={tape['gaps']} dropped={tape['dropped']}")

    document = {
        "root": str(root),
        "fixtures_verified": ["halt-survives-disconnect", "first-frame-recovery",
                              "metadata-fee-update", "legacy-no-size-increment",
                              "gap-incomplete"],
        "checks": checks,
        "all_passed": all(item["passed"] for item in checks),
    }
    (root / "verification.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(document, indent=2, ensure_ascii=False))
    return 0 if document["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Verify the corrected README prose against the machine artifacts.

Specifically:
- the per-bucket reject table in ``README.md`` matches ``historical/replay/ondo_depth_summary.csv``
  row for row (symbol, direction, samples, quality_pass, and each reject counter);
- each bucket's reject counters sum to ``samples``;
- ``summary.json`` carries the per-bucket data, the assessability flags and the renderer
  boilerplate note.

Read-only. Writes ``provenance/report_prose_check.json``.
"""

from __future__ import annotations

import csv
import json
import re
from pathlib import Path

DATA = Path(__file__).resolve().parents[1]
TABLE_ROW = re.compile(
    r"^\|\s*(?P<symbol>[A-Z]+)\s*\|\s*(?P<direction>\S+)\s*\|\s*(?P<samples>\d+)\s*\|\s*"
    r"(?P<quality_pass>\d+)\s*\|\s*(?P<stale_book>\d+)\s*\|\s*(?P<event_skew>\d+)\s*\|\s*"
    r"(?P<quantity_step_unknown>\d+)\s*\|\s*(?P<future_event_time>\d+)\s*\|\s*(?P<no_book>\d+)\s*\|"
)


def csv_buckets() -> dict[tuple[str, str], dict]:
    buckets = {}
    with (DATA / "historical/replay/ondo_depth_summary.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            reasons = {}
            for chunk in (row["reject_reasons"] or "").split(";"):
                if "=" in chunk:
                    name, _, value = chunk.partition("=")
                    reasons[name.strip()] = int(value)
            key = (row["symbol"], f"{row['sell_venue']}>{row['buy_venue']}")
            buckets[key] = {
                "samples": int(row["samples"]),
                "quality_pass": int(row["quality_pass"]),
                "reasons": reasons,
            }
    return buckets


def main() -> int:
    csv_data = csv_buckets()
    readme = (DATA / "README.md").read_text(encoding="utf-8")
    prose_rows = {}
    for line in readme.splitlines():
        match = TABLE_ROW.match(line.strip())
        if match:
            prose_rows[(match["symbol"], match["direction"])] = {
                "samples": int(match["samples"]),
                "quality_pass": int(match["quality_pass"]),
                "stale_book": int(match["stale_book"]),
                "event_skew": int(match["event_skew"]),
                "quantity_step_unknown": int(match["quantity_step_unknown"]),
                "future_event_time": int(match["future_event_time"]),
                "no_book": int(match["no_book"]),
            }

    checks = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"check": name, "passed": bool(passed), "detail": detail})

    check("readme.has_all_four_direction_rows", len(prose_rows) == 4,
          f"rows={sorted(prose_rows)}")
    for key, prose in prose_rows.items():
        actual = csv_data.get(key)
        if actual is None:
            check(f"csv.{key}", False, "no matching CSV bucket")
            continue
        expected = {
            "samples": actual["samples"],
            "quality_pass": actual["quality_pass"],
            "stale_book": actual["reasons"].get("stale_book", 0),
            "event_skew": actual["reasons"].get("event_skew", 0),
            "quantity_step_unknown": actual["reasons"].get("quantity_step_unknown", 0),
            "future_event_time": actual["reasons"].get("future_event_time", 0),
            "no_book": actual["reasons"].get("no_book", 0),
        }
        check(
            f"readme_matches_csv.{key[0]}.{key[1]}",
            prose == expected,
            f"prose={prose} csv={expected}",
        )
        total = sum(
            prose[name] for name in (
                "stale_book", "event_skew", "quantity_step_unknown",
                "future_event_time", "no_book",
            )
        )
        check(
            f"reject_sum_equals_samples.{key[0]}.{key[1]}",
            total == prose["samples"],
            f"sum={total} samples={prose['samples']}",
        )

    summary = json.loads((DATA / "summary.json").read_text(encoding="utf-8"))
    hr = summary["historical_replay"]
    check("summary.has_per_bucket", len(hr.get("per_bucket", [])) == 12,
          f"count={len(hr.get('per_bucket', []))}")
    check("summary.economic_unassessable",
          hr.get("historical_economic_output_assessable") is False,
          f"={hr.get('historical_economic_output_assessable')}")
    check("summary.zero_opportunity_not_verified",
          hr.get("historical_zero_opportunity_verified") is False,
          f"={hr.get('historical_zero_opportunity_verified')}")
    check("summary.renderer_boilerplate_noted",
          len(hr.get("renderer_boilerplate_inapplicable", [])) == 2,
          "two inapplicable renderer statements recorded")
    check("summary.sandbox_false",
          summary["sandbox_auth_verified"] is False
          and summary["sandbox_execution_verified"] is False,
          "both sandbox flags false")
    check("summary.per_bucket_sums",
          hr.get("per_bucket_reasons_sum_matches_samples") is True,
          "every bucket's reject counters sum to samples")

    document = {
        "readme_buckets_checked": sorted(f"{k[0]} {k[1]}" for k in prose_rows),
        "csv_buckets": [
            {
                "symbol": key[0],
                "direction": key[1],
                "samples": value["samples"],
                "quality_pass": value["quality_pass"],
                "reasons": value["reasons"],
            }
            for key, value in sorted(csv_data.items())
        ],
        "checks": checks,
        "all_passed": all(item["passed"] for item in checks),
    }
    out = DATA / "provenance/report_prose_check.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"all_passed": document["all_passed"], "checks": checks}, indent=2,
                     ensure_ascii=False))
    print(f"[report-prose] wrote {out}")
    return 0 if document["all_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

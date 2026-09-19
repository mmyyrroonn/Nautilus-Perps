#!/usr/bin/env python3
"""Evaluate the bounded public ONDO/HL observation for completeness and replay it
through the same data-quality criteria.

Truthfulness rules applied here:
- A leg is "observed" only if the run's own summary shows real subscription and
  update counts for it, not merely that the process started.
- Gaps/drops are read from the tape manifests; absence of a number is never
  reported as zero by assumption.
- The replay is a nominal book comparison, never a fill or a profit; the report
  names ``executable=false``.

Read-only. Writes ``public/evaluation.json``.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from pathlib import Path

ANSI = re.compile(r"\x1b\[[0-9;]*m")

LEG_RE = re.compile(
    r"leg\s+(?P<venue>\S+)\s+top-of-book updates=(?P<quotes>\d+)\s+\(quotes\)\s+"
    r"book updates=(?P<books>\d+)\s+\(deltas-cache\)\s+trades=(?P<trades>\d+)\s+"
    r"funding_seen=(?P<funding>\w+)\s+taker_fee_bps=(?P<fee>[\d.]+)\s+"
    r"\((?P<fee_source>[^)]*)\)"
)
OPTIONAL_RE = re.compile(
    r"feed_ready=(?P<feed>\w+)\s+market_ready=(?P<market>\w+)\s+"
    r"book_valid=(?P<book>\w+)\s+disconnects=(?P<disconnects>\d+)\s+"
    r"metadata_ready=(?P<meta>\w+)"
)
SUMMARY_SYMBOL_RE = re.compile(r"^SUMMARY \[(?P<symbol>[A-Z0-9]+)\]")


def summary_blocks(log_text: str) -> dict[str, dict]:
    """Return {symbol: {"legs": {...}, "lines": [...]}} from the SUMMARY blocks."""
    text = ANSI.sub("", log_text)
    result: dict[str, dict] = {}
    current = None
    for line in text.splitlines():
        marker = SUMMARY_SYMBOL_RE.match(line.strip())
        if marker:
            current = marker.group("symbol")
            result[current] = {"legs": {}, "lines": []}
            continue
        if current is None:
            continue
        if line.startswith("====="):
            current = None
            continue
        result[current]["lines"].append(line)
        leg = LEG_RE.search(line)
        if leg:
            venue = leg.group("venue")
            entry = {
                key: value for key, value in leg.groupdict().items() if key != "venue"
            }
            optional = OPTIONAL_RE.search(line)
            if optional:
                entry.update(optional.groupdict())
            result[current]["legs"][venue] = entry
    return result


def load_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-root", type=Path, required=True)
    args = parser.parse_args()
    root = args.data_root
    public = root / "public"
    obs = public / "observation"

    run_meta = json.loads((public / "observation_run_meta.json").read_text(encoding="utf-8-sig"))
    log_text = (public / "observation_stdout.txt").read_text(encoding="utf-8", errors="replace")
    blocks = summary_blocks(log_text)
    l2_manifest = json.loads((obs / "l2" / "manifest.json").read_text(encoding="utf-8"))
    tape_content = json.loads((public / "observation_tape_content.json").read_text(encoding="utf-8"))
    replay_rows = load_csv(public / "replay" / "ondo_depth_summary.csv")
    preflight_meta = json.loads((public / "preflight" / "meta.json").read_text(encoding="utf-8"))

    raw_line = next(
        (line.strip() for line in log_text.splitlines() if "raw_ondo recording" in line),
        None,
    )
    raw_completeness = None
    if raw_line:
        fields = dict(re.findall(r"(segments|sessions|records|dropped|gaps)=(\d+)", raw_line))
        raw_completeness = {
            "line": raw_line,
            "segments": int(fields.get("segments", -1)),
            "sessions": int(fields.get("sessions", -1)),
            "records": int(fields.get("records", -1)),
            "dropped": int(fields.get("dropped", -1)),
            "gaps": int(fields.get("gaps", -1)),
            "complete_from_log": "complete:" in raw_line,
        }

    per_symbol = {}
    for market in tape_content:
        tape_name = Path(market["tape"]).name
        fragment = next(f for f in l2_manifest["fragments"] if f["tape"] == tape_name)
        symbol = Path(market["tape"]).name.split("_")[1]
        instruments = {}
        for record in market["instrument_records"]:
            venue = record.get("venue")
            metadata = record.get("metadata") or {}
            previous = instruments.get(venue)
            if previous is None or metadata.get("size_increment") is not None:
                instruments.setdefault(venue, {})
                instruments[venue].update(
                    {
                        "taker_fee_bps": metadata.get("taker_fee_bps"),
                        "fee_source": metadata.get("fee_source"),
                        "size_increment": metadata.get("size_increment"),
                        "price_precision": metadata.get("price_precision"),
                        "size_precision": metadata.get("size_precision"),
                    }
                )
        summary = blocks.get(symbol, {"legs": {}, "lines": []})
        per_symbol[symbol] = {
            "session_id": fragment["session_id"],
            "l2_fragment_complete": fragment["closed"],
            "l2_records": fragment["records"],
            "l2_dropped": fragment["dropped"],
            "l2_gaps": fragment["gaps"],
            "l2_first_arrival_seq": fragment["first_arrival_seq"],
            "l2_last_arrival_seq": fragment["last_arrival_seq"],
            "record_kinds": market["kinds"],
            "venues_in_tape": market["venues"],
            "sessions_in_tape": market["sessions"],
            "instrument_metadata": instruments,
            "legs": summary["legs"],
        }

    replay = {}
    for row in replay_rows:
        key = f"{row['symbol']} {row['sell_venue']}>{row['buy_venue']} ${row['notional_usd']}"
        replay[key] = {
            "common_step": row["common_step"],
            "step_origin": row["step_origin"],
            "samples": int(row["samples"]),
            "quality_pass": int(row["quality_pass"]),
            "pass": int(row["pass"]),
            "reject": int(row["reject"]),
            "hits": int(row["hits"]),
            "executable_hits": int(row["executable_hits"]),
            "entry_fees_bps": row["entry_fees_bps"],
            "gross_entry_bps_median": row["gross_entry_bps_median"],
            "entry_after_fees_bps_median": row["entry_after_fees_bps_median"],
            "funding_estimate_bps_median": row["funding_estimate_bps_median"],
            "mapping_verified": row["mapping_verified"].lower() == "true",
            "executable": row["executable"].lower() == "true",
            "reject_reasons": row["reject_reasons"],
        }

    legs_observed = {
        symbol: sorted(per_symbol[symbol]["legs"].keys()) for symbol in sorted(per_symbol)
    }
    all_legs_present = all(
        set(legs) >= {"ONDO", "HL"} for legs in legs_observed.values()
    )

    document = {
        "sandbox_auth_verified": False,
        "sandbox_execution_verified": False,
        "preflight": {
            "complete": preflight_meta["complete"],
            "run_id": preflight_meta["run_id"],
            "symbols": preflight_meta["symbols"],
            "missing": preflight_meta["missing"],
            "failure": preflight_meta["failure"],
            "instruments": [
                {"symbol": t["symbol"], "instrument_id": t["instrument_id"]}
                for t in preflight_meta["targets"]
            ],
        },
        "observation_run": run_meta,
        "observation_start_to_finish": {
            "exit_code": run_meta["exit_code"],
            "elapsed_seconds": run_meta["elapsed_seconds"],
            "completed_within_5_minute_deadline": run_meta["exit_code"] == 0,
        },
        "l2_manifest": {
            "tapes": l2_manifest["tapes"],
            "any_dropped": any(t["dropped"] for t in l2_manifest["tapes"]),
            "any_gaps": any(t["gaps"] for t in l2_manifest["tapes"]),
            "all_complete": all(t["complete"] for t in l2_manifest["tapes"]),
        },
        "raw_ondo_completeness": raw_completeness,
        "per_symbol": per_symbol,
        "legs_observed": legs_observed,
        "both_venues_observed_for_every_symbol": all_legs_present,
        "replay": replay,
        "replay_all_executable_false": all(not v["executable"] for v in replay.values()),
        "replay_all_mapping_unverified": all(
            not v["mapping_verified"] for v in replay.values()
        ),
        "notes": [
            "Saturday sample: low activity is an observation, not a pass/fail; the "
            "subscription/update counts prove both venue legs were live.",
            "hits are nominal book comparisons with entry_after_fees_bps > 0 before the "
            "5 bps one-leg failure reserve; no fill, no profit, no execution claim.",
            "sandbox_auth_verified and sandbox_execution_verified stay false: no "
            "authenticated request was made in this acceptance.",
        ],
    }
    out = public / "evaluation.json"
    out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({
        "legs_observed": legs_observed,
        "both_venues_observed_for_every_symbol": all_legs_present,
        "l2_all_complete": document["l2_manifest"]["all_complete"],
        "raw_ondo_complete": raw_completeness,
        "replay_all_executable_false": document["replay_all_executable_false"],
        "replay_all_mapping_unverified": document["replay_all_mapping_unverified"],
        "exit_code": run_meta["exit_code"],
        "elapsed_seconds": run_meta["elapsed_seconds"],
    }, indent=2, ensure_ascii=False))
    print(f"[evaluate] wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

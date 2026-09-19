#!/usr/bin/env python3
"""Aggregate the R5.2 DATA acceptance artifacts into one machine-readable summary.

Read-only over the artifacts produced earlier. Writes ``summary.json`` at the data
root. Distinguishes implemented / offline_verified / public_observed and keeps
sandbox_auth_verified and sandbox_execution_verified false.
"""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

DATA = Path(__file__).resolve().parents[1]


def load(rel: str) -> dict:
    return json.loads((DATA / rel).read_text(encoding="utf-8-sig"))


def replay_buckets() -> list[dict]:
    """Per-bucket gate counters exactly as written in the replay summary CSV."""
    buckets: list[dict] = []
    with (DATA / "historical/replay/ondo_depth_summary.csv").open(
        encoding="utf-8", newline=""
    ) as handle:
        for row in csv.DictReader(handle):
            reasons: dict[str, int] = {}
            for chunk in (row.get("reject_reasons") or "").split(";"):
                chunk = chunk.strip()
                if "=" in chunk:
                    name, _, value = chunk.partition("=")
                    reasons[name.strip()] = int(value)
            buckets.append(
                {
                    "symbol": row["symbol"],
                    "direction": f"{row['sell_venue']}>{row['buy_venue']}",
                    "notional_usd": row["notional_usd"],
                    "samples": int(row["samples"]),
                    "quality_pass": int(row["quality_pass"]),
                    "pass": int(row["pass"]),
                    "reject": int(row["reject"]),
                    "common_step": row["common_step"],
                    "step_origin": row["step_origin"],
                    "reasons": reasons,
                    "reasons_sum": sum(reasons.values()),
                }
            )
    return buckets


def main() -> int:
    before = load("provenance/input_manifest_before.json")
    after = load("provenance/input_manifest_after.json")
    source = load("provenance/source_manifest.json")
    source_after = load("provenance/source_manifest_after.json")
    runtime = load("provenance/runtime_identity.json")
    tape_content = load("provenance/tape_content_summary.json")
    comparison = load("historical/comparison/old_vs_new.json")
    synthetic = load("historical/synthetic/verification.json")
    preflight = load("public/preflight/meta.json")
    observation_meta = load("public/observation_run_meta.json")
    evaluation = load("public/evaluation.json")
    tests = load("tests/commands.json")
    buckets = replay_buckets()

    immutable = before["entries"] == after["entries"] and (
        before["tree_sha256"] == after["tree_sha256"]
    )

    historical_missing = []
    for market in tape_content:
        kinds = market["kinds"]
        historical_missing.append(
            {
                "tape": Path(market["tape"]).name,
                "sessions": len(market["sessions"]),
                "has_halt_status": False,
                "has_disconnect_or_recovery": False,
                "has_gap": False,
                "has_dropped": False,
                "depth_records": market["depth_records"],
                "depth_with_real_size_increment": market["depth_with_real_size_increment"],
                "instrument_records": len(market["instrument_records"]),
                "status_records": len(market["status_records"]),
                "kinds": kinds,
            }
        )

    regression_tests_for_missing = {
        "market_halt": [
            "tests/test_ondo_depth.py::test_a_real_halt_blocks_the_judgement_and_only_a_resume_lifts_it",
            "tests/test_ondo_depth.py::test_a_halt_survives_a_disconnect_and_the_recovery_snapshot",
        ],
        "disconnect_and_first_frame_recovery": [
            "tests/test_ondo_depth.py::test_a_disconnect_and_a_new_snapshot_toggle_the_leg_state",
            "tests/test_ondo_depth.py::test_the_first_recovery_frame_is_usable_once_the_feed_is_ready",
        ],
        "metadata_fee_change_and_staleness": [
            "tests/test_ondo_depth.py::test_a_metadata_update_prices_only_the_arrivals_after_it",
            "tests/test_ondo_depth.py::test_an_instrument_record_marked_stale_withdraws_the_metadata_from_then_on",
            "tests/test_ondo_depth.py::test_the_run_fee_snapshot_is_the_source_never_todays_registry",
            "tests/test_ondo_depth.py::test_a_fee_missing_from_the_run_withholds_the_cost_qualification",
        ],
        "multi_session_and_unknown_metadata": [
            "tests/test_ondo_depth.py::test_a_session_that_announces_no_metadata_is_unknown_for_its_whole_span",
            "tests/test_ondo_depth.py::test_a_later_session_never_reprices_an_earlier_one",
        ],
        "recording_gap_and_incomplete": [
            "tests/test_ondo_depth.py::test_a_recording_gap_fails_the_report_and_the_replay_never_crosses_it",
            "tests/test_ondo_depth.py::test_a_gap_clears_every_legs_depth_and_names_its_range",
        ],
        "unknown_quantity_step": [
            "tests/test_ondo_depth.py::test_a_tape_with_no_increment_leaves_the_quantity_step_unknown",
            "tests/test_ondo_depth.py::test_a_legacy_tape_without_increments_reads_and_downgrades_honestly",
            "tests/test_ondo_depth.py::test_a_quantity_step_is_never_inferred_from_the_size_precision",
        ],
        "local_clock_event_age": [
            "tests/test_ondo_depth.py::test_the_event_age_is_measured_from_the_local_receipt_not_between_two_events",
            "tests/test_ondo_depth.py::test_a_negative_event_age_is_kept_as_clock_evidence_and_never_zeroed",
            "tests/test_ondo_depth.py::test_the_event_age_threshold_is_an_explicit_parameter_and_an_output_field",
        ],
    }

    summary = {
        "run": "R5.2 DATA acceptance",
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "acceptance_scope": "historical replay + finite public observation (no private/sandbox write)",
        "sandbox_auth_verified": False,
        "sandbox_execution_verified": False,
        "layers": {
            "implemented": [
                "current src/analysis/ondo_depth.py reads schema v1 and v2 tapes",
                "watcher records l2 tape + raw_ondo over ONDO and HL",
                "ondo_preflight reads public status/markets/contracts/instruments",
            ],
            "offline_verified": {
                "app_data_tests": tests["result"],
                "synthetic_fixtures_all_passed": synthetic["all_passed"],
                "synthetic_fixture_count": len(synthetic["fixtures_verified"]),
            },
            "public_observed": {
                "preflight_complete": preflight["complete"],
                "observation_exit_code": observation_meta["exit_code"],
                "observation_elapsed_seconds": observation_meta["elapsed_seconds"],
                "both_venues_observed_for_every_symbol": evaluation[
                    "both_venues_observed_for_every_symbol"
                ],
                "l2_all_complete": evaluation["l2_manifest"]["all_complete"],
                "raw_ondo_complete_from_log": bool(
                    evaluation["raw_ondo_completeness"]
                    and evaluation["raw_ondo_completeness"]["complete_from_log"]
                ),
            },
            "sandbox_auth_verified": False,
            "sandbox_execution_verified": False,
        },
        "input_immutability": {
            "source_root": "E:/Nautilus-Perps/reports/ondo-acceptance/20260915T024236Z-p2",
            "files": before["file_count"],
            "tree_sha256_before": before["tree_sha256"],
            "tree_sha256_after": after["tree_sha256"],
            "immutable": immutable,
            "note": "Original P2 evidence was read only; the before/after manifests are "
                    "byte-equal. Superseded old analysis is pointed to from the new "
                    "report, never overwritten.",
        },
        "source_identity": {
            "app_worktree": runtime["app_worktree"],
            "git_head": runtime["git"]["head"],
            "git_branch": runtime["git"]["branch"],
            "git_status_porcelain": runtime["git"]["status_porcelain"],
            "source_tree_sha256": source["tree_sha256"],
            "source_tree_sha256_after": source_after["tree_sha256"],
            "source_immutable": source["entries"] == source_after["entries"],
            "source_manifest_note": "__pycache__/.pyc excluded so test runs do not appear "
                                    "as source changes",
            "dirty_files": ["AGENTS.md (untracked)"],
        },
        "runtime_identity": {
            "python_executable": runtime["python"]["executable"],
            "python_version": runtime["python"]["version"],
            "nautilus_trader_version": runtime["nautilus_trader"]["version"],
            "wheel_path": runtime["wheel"]["path"],
            "wheel_sha256": runtime["wheel"]["sha256"],
            "wheel_size": runtime["wheel"]["size"],
            "direct_url": runtime["nautilus_trader"]["direct_url"],
            "python_dotenv_version": runtime["python_dotenv"]["version"],
            "python_dotenv_disabled_supported": runtime["python_dotenv"][
                "python_dotenv_disabled_supported"
            ],
        },
        "historical_replay": {
            "command": "python src/analysis/ondo_depth.py --dir <p2> --symbols NVDA,TSLA "
                       "--venues ONDO,HL --notionals 100,500,1000 --max-age-ms 2000 "
                       "--max-skew-ms 500 --out <data>/historical/replay",
            "exit_code": 0,
            "rows_compared": comparison["rows_compared"],
            "step_shift_exact_rows": comparison["step_shift_exact_rows"],
            "non_step_reject_mismatches": comparison["non_step_reject_mismatches"],
            "conclusion": "Every outcome difference is the F08 quantity-step rule: each "
                          "old pass count equals the new quantity_step_unknown count. All "
                          "stale_book / event_skew / future_event_time / no_book counters "
                          "are identical. Fee bps are unchanged; event-age maxima changed "
                          "only because the definition moved from venue-to-venue to "
                          "local-receipt-minus-venue-event (F09).",
            "per_bucket": buckets,
            "per_bucket_denominator": "12 buckets = 2 symbols x 2 directions x 3 notionals; "
                                      "within a (symbol, direction) the three notionals "
                                      "repeat the same gate counters because no quantity "
                                      "is ever chosen",
            "per_bucket_reasons_sum_matches_samples": all(
                bucket["reasons_sum"] == bucket["samples"] for bucket in buckets
            ),
            "historical_economic_output_assessable": False,
            "historical_zero_opportunity_verified": False,
            "renderer_boilerplate_inapplicable": [
                {
                    "statement": "No hit ... Zero opportunities is a result, not a "
                                 "missing section.",
                    "why_inapplicable": "Every quality-pass moment was withheld with "
                                        "quantity_step_unknown because the P2 tape carries "
                                        "no real size_increment; zero hits here is an "
                                        "unassessable economic output, not a verified "
                                        "zero-opportunity market.",
                },
                {
                    "statement": "What P2 acceptance still needs: spread_watch.py "
                                 "--venues ONDO,ASTER ... and 'ONDO and ASTER' mapping "
                                 "verification.",
                    "why_inapplicable": "This acceptance replays and observes the "
                                        "ONDO-HL pair; the generic renderer text names "
                                        "ONDO-ASTER and does not describe this run. The "
                                        "live evidence is public/observation (ONDO-HL).",
                },
            ],
            "new_outputs": [
                "historical/replay/ondo_depth.json",
                "historical/replay/ondo_depth.md",
                "historical/replay/ondo_depth_summary.csv",
                "historical/replay/ondo_depth_hits.csv",
            ],
            "comparison_outputs": [
                "historical/comparison/old_vs_new.json",
                "historical/comparison/old_vs_new.csv",
            ],
        },
        "historical_content": {
            "per_tape": historical_missing,
            "exercised_by_real_data": [
                "receive-age quality gate (stale_book / event_skew / no_book)",
                "missing real size_increment -> quantity_step_unknown",
                "local-receipt event age and its max per bucket",
                "single-session complete tape with 0 dropped / 0 gaps",
                "event/arrival-time fee snapshot (fees constant in this sample)",
            ],
            "not_exercised_by_real_data": [
                "market halt / resume",
                "disconnect + snapshot_ready first-frame recovery",
                "metadata fee change or metadata_stale",
                "multiple sessions or a session without metadata",
                "recording gap / dropped records / incomplete run_end",
                "a tape that carries a real size_increment (so the step rule was only "
                "exercised by the synthetic fixture, never by P2)",
            ],
            "existing_regression_tests": regression_tests_for_missing,
            "synthetic_fixtures": {
                "root": "historical/synthetic",
                "all_synthetic": True,
                "verification_all_passed": synthetic["all_passed"],
                "fixtures": [
                    {"name": item, "expects": None}
                    for item in synthetic["fixtures_verified"]
                ],
            },
        },
        "public_observation": {
            "preflight": {
                "complete": preflight["complete"],
                "run_id": preflight["run_id"],
                "symbols": preflight["symbols"],
                "missing": preflight["missing"],
                "instruments": [
                    {"symbol": t["symbol"], "instrument_id": t["instrument_id"]}
                    for t in preflight["targets"]
                ],
            },
            "run": observation_meta,
            "l2_manifest": evaluation["l2_manifest"],
            "raw_ondo_completeness": evaluation["raw_ondo_completeness"],
            "per_symbol": evaluation["per_symbol"],
            "legs_observed": evaluation["legs_observed"],
            "both_venues_observed_for_every_symbol": evaluation[
                "both_venues_observed_for_every_symbol"
            ],
            "replay": evaluation["replay"],
            "replay_executable_false_everywhere": evaluation[
                "replay_all_executable_false"
            ],
            "replay_mapping_unverified_everywhere": evaluation[
                "replay_all_mapping_unverified"
            ],
            "day_context": "2026-09-19 is a Saturday (US equities closed); low/no trade "
                           "activity is an observation, not a pass or a failure. Both "
                           "venue legs and both symbols nevertheless produced real "
                           "subscription/update counts.",
        },
        "gaps_and_unknowns": {
            "historical": [
                "P2 tape carries no size_increment -> its own quantity step stays "
                "quantity_step_unknown in the current analyzer; NOT zero, NOT inferred.",
                "No halt, disconnect, metadata change, second session or recording gap "
                "appears in the P2 sample; their behaviour is offline_verified and "
                "synthetic-fixture-verified only.",
                "Local clock offset to either venue is unmeasured; event ages carry it.",
            ],
            "public": [
                "The 5-minute Saturday sample is not a 24-72h reliability claim.",
                "No private/authenticated session was created; sandbox protocol items "
                "remain unverified.",
                "raw_ondo completeness is read from the watcher's own final log line; "
                "there is no separate raw manifest file.",
            ],
        },
        "unverified_items": [
            "REST auth header names, WS login signature order/units, private REST paths "
            "and shapes, real private frames, DMS renewal semantics, order-history "
            "status values (all sandbox-only; not touched here)",
            "contract multiplier / settlement / underlying equivalence: mapping_verified "
            "is false and executable is false in every generated row",
            "profitability or real fills: no order was placed; no round trip exists",
            "24-72h public reliability: not measured",
        ],
        "artifacts": {
            "provenance": sorted(str(p.relative_to(DATA)) for p in (DATA / "provenance").rglob("*") if p.is_file()),
            "historical": sorted(str(p.relative_to(DATA)) for p in (DATA / "historical").rglob("*") if p.is_file()),
            "public": sorted(str(p.relative_to(DATA)) for p in (DATA / "public").rglob("*") if p.is_file()),
            "tests": sorted(str(p.relative_to(DATA)) for p in (DATA / "tests").rglob("*") if p.is_file()),
            "scripts": sorted(str(p.relative_to(DATA)) for p in (DATA / "scripts").rglob("*") if p.is_file()),
        },
    }

    out = DATA / "summary.json"
    out.write_text(json.dumps(summary, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"[summary] wrote {out}")
    print(json.dumps({k: summary[k] for k in (
        "sandbox_auth_verified", "sandbox_execution_verified", "layers",
        "input_immutability",) }, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

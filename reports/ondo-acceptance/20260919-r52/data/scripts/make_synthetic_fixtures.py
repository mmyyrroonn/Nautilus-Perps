#!/usr/bin/env python3
"""Build SYNTHETIC acceptance fixtures for phenomena the historical P2 tape does
NOT contain, and run the *real* current analyzer over them.

The historical ONDO-HL tape (20260915T024236Z-p2) is a single session with no
halt, no disconnect/recovery, no metadata change, no gap and no real
``size_increment``. This script does not pretend otherwise: every fixture here is
explicitly marked synthetic, is built with the app's own tape writers/test
builders, and is then replayed through ``src/analysis/ondo_depth.py`` so the
analyzer's real behaviour - not a mock - is what the acceptance records.

Read-only with respect to the repository: all output goes under the R5.2 data
directory passed as ``--out-root``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APP = Path("E:/Nautilus-Perps/.worktrees/ondo-r52-app")
SRC = APP / "src"
TESTS = APP / "tests"
VENV_PY = Path("E:/Nautilus-Perps/.venv/Scripts/python.exe")

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(TESTS) not in sys.path:
    sys.path.insert(0, str(TESTS))

import test_ondo_depth as t  # noqa: E402  (the regression tests' own builders)
import market_tape  # noqa: E402
from market_tape import status_event, instrument_event, write_run_manifest  # noqa: E402

MS = 1_000_000


def fixture_halt_survives_disconnect(root: Path) -> Path:
    """HALT -> adapter:disconnected -> one frame -> adapter:snapshot_ready -> quote."""

    def body(writer, legs):
        t.push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=1_000)
        t.push(legs["HL"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="", is_trading=False, is_quoting=False,
            ts_event_ns=1_500 * MS, ts_init_ns=1_500 * MS + 1,
            recorded_mono_ns=1_500 * MS))
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="adapter:disconnected", is_trading=None,
            ts_event_ns=2_000 * MS, ts_init_ns=2_000 * MS + 1,
            recorded_mono_ns=2_000 * MS))
        t.push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]], at_ms=3_000)
        t.push(legs["HL"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=3_000)
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="adapter:snapshot_ready", is_trading=None,
            ts_event_ns=3_000 * MS, ts_init_ns=3_000 * MS + 1,
            recorded_mono_ns=3_000 * MS))
        t.push_quote(writer, venue="ONDO", at_ms=4_000, receive_ms=4_000)

    run_dir = root / "halt-survives-disconnect" / "run"
    t.write_run_dir(root / "halt-survives-disconnect", body, symbol="NVDA",
                    venues=("ONDO", "HL"), fees={"ONDO": "2.5", "HL": "0.9"},
                    run_dir=run_dir)
    return run_dir


def fixture_first_frame_recovery(root: Path) -> Path:
    """F18: a single recovery depth frame is usable once the feed is ready."""

    def body(writer, legs):
        t.push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]],
               at_ms=1_000, init_ms=1_000)
        t.push(legs["HL"], bids=[["100.00", "100"]], asks=[["100.10", "100"]],
               at_ms=1_000, init_ms=1_000)
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="adapter:disconnected", is_trading=None,
            ts_event_ns=2_000 * MS, ts_init_ns=2_000 * MS + 1,
            recorded_mono_ns=2_000 * MS))
        t.push(legs["ONDO"], bids=[["110.00", "100"]], asks=[["110.10", "100"]],
               at_ms=3_000, init_ms=3_000)
        t.push(legs["HL"], bids=[["100.00", "100"]], asks=[["100.10", "100"]],
               at_ms=3_000, init_ms=3_000)
        writer.write_event(status_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            action="HALT", reason="adapter:snapshot_ready", is_trading=None,
            ts_event_ns=3_000 * MS, ts_init_ns=3_000 * MS + 1,
            recorded_mono_ns=3_000 * MS))
        t.push_quote(writer, venue="ONDO", at_ms=4_000, receive_ms=4_000, init_ms=4_000)

    run_dir = root / "first-frame-recovery" / "run"
    t.write_run_dir(root / "first-frame-recovery", body, symbol="NVDA",
                    venues=("ONDO", "HL"), fees={"ONDO": "2.5", "HL": "0.9"},
                    run_dir=run_dir)
    return run_dir


def fixture_metadata_fee_update(root: Path) -> Path:
    """F06: a mid-tape ONDO taker-fee change prices only the arrivals after it."""

    def body(writer, legs):
        t.push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        t.push(legs["HL"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        writer.write_event(instrument_event(
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            metadata={"venue": "ONDO", "client_id": "ONDO", "taker_fee_bps": "100",
                      "fee_source": "instrument_metadata", "price_precision": 2,
                      "size_precision": 3, "tick_size": "0.01",
                      "size_increment": "0.001"},
            coverage_limit=100, source="instrument_update"))
        t.push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=3_000)
        t.push(legs["HL"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=3_000)

    run_dir = root / "metadata-fee-update" / "run"
    t.write_run_dir(root / "metadata-fee-update", body, symbol="NVDA",
                    venues=("ONDO", "HL"), fees={"ONDO": "2.5", "HL": "0.9"},
                    run_dir=run_dir)
    return run_dir


def fixture_legacy_no_size_increment(root: Path) -> Path:
    """F08: a legacy tape with size_precision but no real size_increment."""

    def body(writer, legs):
        t.push(legs["ONDO"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)
        t.push(legs["HL"], bids=[["100.00", "100"]], asks=[["100.10", "100"]], at_ms=1_000)

    run_dir = root / "legacy-no-size-increment" / "run"
    t.write_run_dir(root / "legacy-no-size-increment", body, symbol="NVDA",
                    venues=("ONDO", "HL"), fees={"ONDO": "2.5", "HL": "0.9"},
                    size_increments={"ONDO": None, "HL": None}, run_dir=run_dir)
    return run_dir


def fixture_gap_incomplete(root: Path) -> Path:
    """A recording gap and an incomplete run_end: the hole is never crossed."""
    base = root / "gap-incomplete"
    l2 = base / "run" / "l2"
    l2.mkdir(parents=True, exist_ok=True)
    start, end = t.markers(complete=False)
    instruments = [
        {**start, "arrival_seq": seq, "event_kind": "instrument",
         "venue": venue, "instrument_id": f"NVDA-USD-PERP.{venue}",
         "source": "instrument_metadata", "coverage_limit": 100,
         "metadata": {"taker_fee_bps": fee, "fee_source": "instrument_metadata",
                      "size_precision": 3, "size_increment": "0.001"}}
        for seq, (venue, fee) in enumerate((("ONDO", "2.5"), ("HL", "0.9")), start=1)
    ]
    lines = [
        start,
        *instruments,
        t.rec(3, event_ns=1_000 * MS, init_ns=1_000 * MS + 1, mono_ns=1_000 * MS,
              bids=[["100.00", "1"]], asks=[["100.10", "1"]]),
        t.rec(4, venue="HL", event_ns=1_000 * MS, init_ns=1_000 * MS + 1,
              mono_ns=1_000 * MS, bids=[["100.00", "1"]], asks=[["100.10", "1"]]),
        {**start, "arrival_seq": None, "event_kind": "gap", "valid": False,
         "invalid_reason": "recording_gap:queue_full", "dropped": 3,
         "missing_from": 5, "missing_to": 7, "recorded_mono_ns": 2_000 * MS,
         "venue": None},
        t.rec(8, event_ns=3_000 * MS, init_ns=3_000 * MS + 1, mono_ns=3_000 * MS,
              bids=[["100.00", "1"]], asks=[["100.10", "1"]]),
        end,
    ]
    t.write_lines(l2 / "l2_NVDA_ONDO-HL_20260914T000000Z.jsonl", lines)
    write_run_manifest(l2)
    return base / "run"


FIXTURES = {
    "halt-survives-disconnect": {
        "builder": fixture_halt_survives_disconnect,
        "expects": "market_halted and disconnected reject moments separately; a pass "
                   "before the halt; snapshot_ready never lifts the halt",
    },
    "first-frame-recovery": {
        "builder": fixture_first_frame_recovery,
        "expects": "one recovery frame is usable, keeps its own receipt and venue times, "
                   "and a later quote refreshes neither",
    },
    "metadata-fee-update": {
        "builder": fixture_metadata_fee_update,
        "expects": "seq4 costs 3.4 bps, seq6/seq7 cost 100.9 bps; the update never "
                   "reprices earlier arrivals",
    },
    "legacy-no-size-increment": {
        "builder": fixture_legacy_no_size_increment,
        "expects": "quantity_step_unknown withholds every priced row; "
                   "10**-size_precision is not used as a step",
    },
    "gap-incomplete": {
        "builder": fixture_gap_incomplete,
        "expects": "recording_gap then no_book; the tape verdict is complete=false",
    },
}


def run_fixture(name: str, run_dir: Path, out_root: Path) -> dict:
    out_dir = out_root / name / "depth-analysis"
    cmd = [
        str(VENV_PY), str(SRC / "analysis" / "ondo_depth.py"),
        "--dir", str(run_dir), "--symbols", "NVDA", "--venues", "ONDO,HL",
        "--notionals", "100", "--max-age-ms", "500000", "--max-skew-ms", "500000",
        "--out", str(out_dir),
    ]
    env = dict(os.environ)
    env["PYTHON_DOTENV_DISABLED"] = "1"
    env["PYTHONPATH"] = str(SRC)
    result = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(APP))
    (out_root / name / "analyzer_stdout.txt").write_text(result.stdout, encoding="utf-8")
    (out_root / name / "analyzer_stderr.txt").write_text(result.stderr, encoding="utf-8")
    return {
        "fixture": name,
        "synthetic": True,
        "run_dir": str(run_dir),
        "out_dir": str(out_dir),
        "command": cmd,
        "exit_code": result.returncode,
        "expects": FIXTURES[name]["expects"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-root", type=Path, required=True)
    args = parser.parse_args()
    out_root = args.out_root
    out_root.mkdir(parents=True, exist_ok=True)

    results = []
    for name, spec in FIXTURES.items():
        fixture_dir = out_root / name
        fixture_dir.mkdir(parents=True, exist_ok=True)
        (fixture_dir / "SYNTHETIC.md").write_text(
            f"# SYNTHETIC fixture: {name}\n\n"
            "This tape is generated, not recorded. It exists only to exercise an "
            "analyzer rule the historical 20260915T024236Z-p2 tape does not contain.\n\n"
            f"Expected behaviour: {spec['expects']}.\n\n"
            "Built by scripts/make_synthetic_fixtures.py in the R5.2 data directory; "
            "replayed with the current src/analysis/ondo_depth.py.\n",
            encoding="utf-8",
        )
        run_dir = spec["builder"](out_root)
        results.append(run_fixture(name, run_dir, out_root))

    document = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "note": "All fixtures here are SYNTHETIC; none is a recorded venue tape.",
        "fixtures": results,
    }
    (out_root / "results.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(json.dumps(document, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

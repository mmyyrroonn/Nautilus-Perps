"""Offline tests for standardized Carry tape replay simulator (scripts/replay_carry_tape.py)."""
from __future__ import annotations

from decimal import Decimal
import gzip
import json
from pathlib import Path
import subprocess
import sys

from src.carry.config import load_carry_plan
from scripts.replay_carry_tape import main as replay_main, replay_tape

D = Decimal
REPO_ROOT = Path(__file__).resolve().parents[1]


def _write_gz(path: Path, rows: list[dict], *, raw_tail: bytes = b"") -> None:
    payload = "".join(json.dumps(r) + "\n" for r in rows).encode("utf-8")
    compressed = gzip.compress(payload)
    if raw_tail:
        compressed = compressed[:-6] if raw_tail == b"__TRUNCATE_GZ__" else gzip.compress(payload + raw_tail)
    path.write_bytes(compressed)


def _leg(sym: str, v: str, iid: str, ih: int, ns: int, r8: float, cr: float, b: float, a: float) -> dict:
    return {
        "s": sym, "v": v, "ac": "CRYPTO", "iid": iid, "ih": ih, "ns": ns,
        "r8": r8, "r24": r8, "cr": cr, "mp": 0.5 * (b + a), "b": b, "a": a,
        "v24": 2_000_000.0, "v1": 50_000.0,
    }


def _build_multiday_tape(tape_dir: Path, *, truncate: bool = False) -> int:
    t0 = 1_800_050_400  # 2027-01-15T22:00:00Z
    hl_iid, okx_iid = "SOL-USD-PERP.HYPERLIQUID", "SOL-USDT-SWAP.OKX"
    _write_gz(
        tape_dir / "slow_snapshots_20270115.jsonl.gz",
        [{"ts": t0, "legs": [
            _leg("SOL", "HL", hl_iid, 1, t0 + 3600, 0.0, 0.0, 99.98, 100.02),
            _leg("SOL", "OKX", okx_iid, 8, t0 + 7200, 2.0, 2.0, 100.13, 100.17),
        ]}],
    )
    _write_gz(
        tape_dir / "fast_bbo_20270115.jsonl.gz",
        [
            {"ts": t0 + 0.5, "iid": okx_iid, "b": 100.13, "a": 100.17, "m": 100.15},
            {"ts": t0 + 1.0, "iid": hl_iid, "b": 99.96, "a": 99.98, "m": 99.97},
            {"ts": t0 + 2.0, "iid": hl_iid, "b": 99.94, "a": 99.96, "m": 99.95},
        ],
    )
    t_exit = t0 + 7 * 3600  # 2027-01-16T05:00:00Z (> 6h min_hold)
    _write_gz(
        tape_dir / "slow_snapshots_20270116.jsonl.gz",
        [{"ts": t_exit, "legs": [
            _leg("SOL", "HL", hl_iid, 1, t_exit + 3600, 0.0, 0.0, 99.98, 100.02),
            _leg("SOL", "OKX", okx_iid, 8, t_exit + 7200, 0.1, 0.1, 100.00, 100.04),
        ]}],
    )
    fast2 = [
        {"ts": t_exit + 0.4, "iid": hl_iid, "b": 99.98, "a": 100.02, "m": 100.00},
        {"ts": t_exit + 0.5, "iid": okx_iid, "b": 100.00, "a": 100.04, "m": 100.02},
        {"ts": t_exit + 1.0, "iid": hl_iid, "b": 100.03, "a": 100.05, "m": 100.04},
        {"ts": t_exit + 2.0, "iid": hl_iid, "b": 100.05, "a": 100.07, "m": 100.06},
    ]
    _write_gz(
        tape_dir / "fast_bbo_20270116.jsonl.gz",
        fast2,
        raw_tail=b'{"ts": 1800099999' if truncate else b"",
    )
    if truncate:
        bad = tape_dir / "fast_bbo_20270117.jsonl.gz"
        bad.write_bytes(gzip.compress((json.dumps({"ts": t_exit + 10, "iid": hl_iid, "b": 100.0, "a": 100.02, "m": 100.01}) + "\n").encode())[:-4])
    return t0


def test_replay_multiday_maker_funding_and_net_pnl(tmp_path: Path) -> None:
    tape_dir = tmp_path / "tape"
    tape_dir.mkdir()
    _build_multiday_tape(tape_dir, truncate=False)
    plan = load_carry_plan(REPO_ROOT / "config" / "carry-paper.toml")

    rep = replay_tape(tape_dir=tape_dir, plan=plan, execution_mode="hybrid")
    s = rep["summary"]
    assert s["total_trades"] == 1
    assert s["open_positions_count"] == 0
    assert s["maker_orders_submitted"] == 4 and s["maker_orders_filled"] == 4
    assert s["maker_fill_rate"] == 1.0 and s["win_rate"] == 1.0
    assert D(s["funding_pnl_usd"]) > D("0")
    assert D(s["fee_usd"]) > D("0")
    assert D(s["net_pnl_usd"]) == D(s["price_pnl_usd"]) + D(s["funding_pnl_usd"]) - D(s["fee_usd"])
    assert "HL->OKX" in rep["by_venue_pair"]
    assert "carry_decay" in rep["by_close_reason"]


def test_replay_tolerates_truncated_gzip_and_overrides(tmp_path: Path) -> None:
    tape_dir = tmp_path / "tape_trunc"
    tape_dir.mkdir()
    _build_multiday_tape(tape_dir, truncate=True)
    json_out = tmp_path / "out" / "replay.json"

    rc = replay_main([
        "--tape-dir", str(tape_dir),
        "--config", str(REPO_ROOT / "config" / "carry-paper.toml"),
        "--execution-mode", "dual_taker",
        "--json-out", str(json_out),
    ])
    assert rc == 0
    rep_dt = json.loads(json_out.read_text(encoding="utf-8"))
    assert rep_dt["tape_stats"]["truncated_files"] >= 1
    assert rep_dt["tape_stats"]["skipped_lines"] >= 1
    assert rep_dt["summary"]["total_trades"] == 1
    assert rep_dt["summary"]["maker_orders_submitted"] == 0
    assert rep_dt["summary"]["taker_orders_submitted"] >= 4

    rc_high = replay_main([
        "--tape-dir", str(tape_dir),
        "--config", str(REPO_ROOT / "config" / "carry-paper.toml"),
        "--entry-carry-bps-h", "5.0",
        "--json-out", str(json_out),
    ])
    assert rc_high == 0
    assert json.loads(json_out.read_text(encoding="utf-8"))["summary"]["total_trades"] == 0

    proc = subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "replay_carry_tape.py"), "--help"],
        capture_output=True, text=True, check=False,
    )
    assert proc.returncode == 0 and "--tape-dir" in proc.stdout

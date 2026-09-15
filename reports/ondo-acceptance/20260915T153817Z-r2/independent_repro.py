#!/usr/bin/env python3
"""Main-session authored reproductions for the R2 review findings.

These are written by the coordinating session, not by the agents that implemented the
fixes, and they assert the *correct* (post-fix) behaviour rather than any particular
implementation. Run against a pristine pre-fix copy of the tree to see them go red, and
against the fixed tree to see them go green:

    .venv\\Scripts\\python.exe independent_repro.py <repo-root>

Checks, in review-finding order:

* F06  a mid-run metadata change must not rewrite the cost of earlier arrivals
* F07  a real venue halt must survive a feed reconnect (``snapshot_ready`` is not a resume)
* F08a a tape that carries only ``size_precision`` must not have its quantity step
       inferred from ``10**-precision``; the step is unknown
* F08b the two legs' real ``size_increment`` values (0.002 / 0.003) give a common
       step of 0.006 through the tape's own metadata path, not through a CLI override
* F09  ``event_age`` is measured from the local epoch receipt time, not between two
       venue event stamps (both legs delayed together must not read as age 0)
* F10  a full queue must not be able to block the deadline flush, and there must be a
       way to drain on the deadline with no new records arriving
* F11  an old session's truncated final line must not hide a later, complete session

Every check above is falsifiable by construction: run this file against a pristine
pre-fix copy of the tree and it goes red (see ``independent_repro_prefix_baseline.txt``).

F18 is **not** among them - see the note above ``CHECKS``.

Exit code is the number of failed checks (0 = all green).
"""

from __future__ import annotations

import json
import sys
import tempfile
import traceback
from decimal import Decimal
from pathlib import Path


FAILED: set[str] = set()


def _fail(check: str, detail: str) -> None:
    FAILED.add(check[:3])  # F08a and F08b are two halves of one finding
    print(f"FAIL {check}: {detail}")


def _pass(check: str, detail: str = "") -> None:
    print(f"PASS {check}{': ' + detail if detail else ''}")


# --------------------------------------------------------------------------- setup


def _imports(root: Path):
    sys.path.insert(0, str(root / "src"))
    from market_tape import (ADD, CLEAR, BookTape, Delta, TapeWriter, instrument_event,
                             read_tape, status_event, write_run_manifest)
    from analysis import ondo_depth
    from analysis.ondo_depth import Params, QualityParams, analyse_run

    def const(name: str, literal):
        """The module's own name when it has one, else the wire value it stands for.

        The pre-fix tree spells these inline, so a hard import would make the baseline
        red with an ImportError instead of the behaviour these checks are about.
        """
        return getattr(ondo_depth, name, literal)

    return dict(
        ADD=ADD, CLEAR=CLEAR, BookTape=BookTape, Delta=Delta, TapeWriter=TapeWriter,
        instrument_event=instrument_event, read_tape=read_tape, status_event=status_event,
        write_run_manifest=write_run_manifest,
        ADAPTER_DISCONNECTED=const("ADAPTER_DISCONNECTED", "adapter:disconnected"),
        ADAPTER_SNAPSHOT_READY=const("ADAPTER_SNAPSHOT_READY", "adapter:snapshot_ready"),
        REJECT_BOOK_INVALID=const("REJECT_BOOK_INVALID", "book_invalid"),
        REJECT_MARKET_HALTED=const("REJECT_MARKET_HALTED", "market_halted"),
        Params=Params, QualityParams=QualityParams, analyse_run=analyse_run,
    )


MS = 1_000_000
RUN_ID = "r2check"


def _metadata(fee: str, *, increment: str | None, precision: int | None = 3) -> dict:
    payload: dict[str, object] = {
        "venue": "ONDO", "client_id": "ONDO", "taker_fee_bps": fee,
        "fee_source": "instrument_metadata", "price_precision": 2,
        "tick_size": "0.01",
    }
    if precision is not None:
        payload["size_precision"] = precision
    if increment is not None:
        payload["size_increment"] = increment
    return payload


def _push(leg, *, bids, asks, at_ms, init_ms=None, at_seq=None):
    return leg.replace(
        bids, asks, coverage_limit=100, source="snapshot",
        ts_event_ns=int(at_ms * MS),
        ts_init_ns=int((init_ms if init_ms is not None else at_ms) * MS) + MS,
        recorded_mono_ns=int(at_ms * MS),
    )


def _write_run(root: Path, tmp: Path, body, *, increment="0.001"):
    api = _imports(root)
    l2 = tmp / "run" / "l2"
    l2.mkdir(parents=True, exist_ok=True)
    path = l2 / f"l2_NVDA_ONDO-ASTER_{RUN_ID}.jsonl"
    with api["TapeWriter"](path, run_id=RUN_ID, symbol="NVDA",
                           legs=[{"venue_key": "ONDO", "venue": "ONDO"},
                                 {"venue_key": "ASTER", "venue": "ASTER"}]) as writer:
        legs = {}
        for venue, fee in (("ONDO", "2.5"), ("ASTER", "0.9")):
            instrument = f"NVDA-USD-PERP.{venue}"
            legs[venue] = api["BookTape"](writer, symbol="NVDA", venue=venue,
                                          instrument_id=instrument, coverage_limit=100)
            payload = _metadata(fee, increment=increment)
            payload["venue"] = venue
            payload["client_id"] = venue
            writer.write_event(api["instrument_event"](
                symbol="NVDA", venue=venue, instrument_id=instrument,
                metadata=payload, coverage_limit=100))
        body(writer, legs, api)
    api["write_run_manifest"](l2)
    return tmp / "run"


def _params(api, **kw):
    args = dict(symbols=("NVDA",), venues=("ONDO", "ASTER"),
                notionals=(Decimal("100"),),
                quality=api["QualityParams"](max_age_ms=500_000, max_skew_ms=500_000),
                steps={}, mapping_verified=frozenset(), max_hits=200)
    args.update(kw)
    return api["Params"](**args)


def _rows(report) -> list[dict]:
    out = []
    for market in report.document.get("markets", []):
        for direction in market.get("directions", []):
            for notional in direction.get("notionals", []):
                out.extend(notional.get("examples", []))
    return out


def _instrument_lines(writer) -> list[dict]:
    """The instrument records already on disk, across every fragment written so far."""
    out = []
    for fragment in writer.fragments:
        if not fragment.exists():
            continue
        for line in fragment.read_text(encoding="utf-8").splitlines():
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            if record.get("event_kind") == "instrument":
                out.append(record)
    return out


# ------------------------------------------------------------------ the checks


def check_f06(root: Path, tmp: Path) -> None:
    """A later metadata change must not re-price the arrivals that came before it."""
    def body(writer, legs, api):
        _push(legs["ONDO"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=1_000)
        _push(legs["ASTER"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=1_000)
        # seq5: the venue's fee changes to 100 bps. Only later arrivals may see it.
        payload = _metadata("100", increment="0.001")
        payload["venue"] = "ONDO"
        payload["client_id"] = "ONDO"
        writer.write_event(api["instrument_event"](
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            metadata=payload, coverage_limit=100, source="instrument_update"))
        _push(legs["ONDO"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=3_000)
        _push(legs["ASTER"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=3_000)

    run = _write_run(root, tmp, body)
    api = _imports(root)
    report = api["analyse_run"](run, _params(api))
    rows = _rows(report)
    # The early arrival must be priced 3.4 and only the later one 100.9. Compare in
    # arrival order: a set alone would accept a fix that priced every row 3.4.
    ordered = [
        (row["arrival_seq"], row["entry_fees_bps"]) for row in rows
        if row["entry_fees_bps"] not in ("", "None")
    ]
    early = {fee for seq, fee in ordered if seq <= 4}
    late = {fee for seq, fee in ordered if seq > 4}
    if early == {"3.4"} and late == {"100.9"}:
        _pass("F06", f"fees by arrival: {sorted(ordered)}")
    else:
        _fail("F06", f"a mid-run metadata change repriced the wrong arrivals: "
                     f"{sorted(ordered)} (early arrivals must be 3.4, later ones 100.9)")


def check_f08(root: Path, tmp: Path) -> None:
    """The quantity step comes from a real increment or is unknown - never inferred."""
    def body(writer, legs, api):
        _push(legs["ONDO"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=1_000)
        _push(legs["ASTER"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=1_000)

    api = _imports(root)

    # (a) legacy tape: precision only, no increment -> unknown, not 10**-precision.
    run = _write_run(root, tmp / "a", body, increment=None)
    report = api["analyse_run"](run, _params(api))
    steps = {row["common_step"] for row in _rows(report)}
    rejected = {
        reason for market in report.document.get("markets", [])
        for direction in market.get("directions", [])
        for notional in direction.get("notionals", [])
        for reason in notional.get("reject_reasons", {})
    }
    if steps <= {"", "None"} or "quantity_step_unknown" in rejected:
        _pass("F08a", f"no increment in the tape -> step {steps or '{}'}, "
                      f"reasons {sorted(rejected)}")
    else:
        _fail("F08a", f"the step was inferred from size_precision: {steps} "
                      f"(reasons {sorted(rejected)})")

    # (b) real increments 0.002 / 0.003 on the two legs -> common step 0.006.
    def body_two(writer, legs, api):
        _push(legs["ONDO"], bids=[["110.00", "10"]], asks=[["110.10", "10"]], at_ms=1_000)
        _push(legs["ASTER"], bids=[["110.00", "10"]], asks=[["110.10", "10"]], at_ms=1_000)

    l2 = tmp / "b" / "run" / "l2"
    l2.mkdir(parents=True, exist_ok=True)
    path = l2 / f"l2_NVDA_ONDO-ASTER_{RUN_ID}.jsonl"
    with api["TapeWriter"](path, run_id=RUN_ID, symbol="NVDA",
                           legs=[{"venue_key": "ONDO", "venue": "ONDO"},
                                 {"venue_key": "ASTER", "venue": "ASTER"}]) as writer:
        legs = {}
        for venue, fee, inc in (("ONDO", "2.5", "0.002"), ("ASTER", "0.9", "0.003")):
            instrument = f"NVDA-USD-PERP.{venue}"
            legs[venue] = api["BookTape"](writer, symbol="NVDA", venue=venue,
                                          instrument_id=instrument, coverage_limit=100)
            payload = _metadata(fee, increment=inc)
            payload["venue"] = venue
            payload["client_id"] = venue
            writer.write_event(api["instrument_event"](
                symbol="NVDA", venue=venue, instrument_id=instrument,
                metadata=payload, coverage_limit=100))
        body_two(writer, legs, api)
    api["write_run_manifest"](l2)
    report = api["analyse_run"](tmp / "b" / "run", _params(api))
    steps = {row["common_step"] for row in _rows(report)}
    if steps == {"0.006"}:
        _pass("F08b", "0.002 and 0.003 through the tape metadata give 0.006")
    else:
        _fail("F08b", f"the two real increments did not give a common step of 0.006: {steps}")


def check_f09(root: Path, tmp: Path) -> None:
    """Both legs delayed by the same 120 s must not read as an event age of 0."""
    def body(writer, legs, api):
        # venue event at 1_000 ms, received locally at 121_000 ms (init stamp).
        _push(legs["ONDO"], bids=[["100.00", "10"]], asks=[["100.10", "10"]],
              at_ms=1_000, init_ms=121_000)
        _push(legs["ASTER"], bids=[["100.00", "10"]], asks=[["100.10", "10"]],
              at_ms=1_000, init_ms=121_000)

    run = _write_run(root, tmp, body)
    api = _imports(root)
    report = api["analyse_run"](run, _params(api))
    ages = [row["event_age_sell_ms"] for row in _rows(report)]
    ages += [row["event_age_buy_ms"] for row in _rows(report)]
    numeric = [Decimal(a) for a in ages if a not in ("", "None")]
    if numeric and min(numeric) >= Decimal("100000"):
        _pass("F09", f"event age measured from the local receipt: min {min(numeric)} ms")
    else:
        _fail("F09", f"the event age collapsed to the venue-to-venue difference: {ages} "
                     f"(both legs are 120 s old and were received together)")


def check_f10(root: Path, tmp: Path) -> None:
    """A full queue must not block the deadline flush, and the deadline must be reachable."""
    api = _imports(root)
    now = [0.0]

    def clock() -> float:
        return now[0]

    l2 = tmp / "f10"
    l2.mkdir(parents=True, exist_ok=True)
    path = l2 / f"l2_NVDA_ONDO-ASTER_{RUN_ID}.jsonl"
    writer = api["TapeWriter"](path, run_id=RUN_ID, symbol="NVDA", queue_max=1,
                               flush_secs=1.0, clock=clock)
    writer.open()
    try:
        for i in range(3):
            writer.write_event(api["instrument_event"](
                symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
                metadata=_metadata("2.5", increment="0.001"), coverage_limit=100,
                ts_event_ns=i * MS, ts_init_ns=i * MS))
            now[0] += 0.01
        # The deadline has not passed yet: a drain driven by the *deadline* must hold off,
        # otherwise "flushes on every tick" would satisfy the check below by accident.
        now[0] = 0.5
        deadline_api = None
        for name in ("tick", "flush_due", "flush_if_due", "poll", "drain", "maybe_flush",
                     "service", "pump"):
            if callable(getattr(writer, name, None)):
                deadline_api = name
                break
        if deadline_api is None:
            _fail("F10", "the writer exposes no deadline drain entry point at all, so a "
                         f"quiet queue can only be emptied by the next arriving record "
                         f"(dropped={writer.dropped}, records={writer.records})")
            return
        getattr(writer, deadline_api)()
        early = _instrument_lines(writer)
        # Time passes with no new record at all: the deadline flush must still happen.
        now[0] = 5.0
        getattr(writer, deadline_api)()
        data = _instrument_lines(writer)
        if data and not early:
            _pass("F10", f"{deadline_api}() held the queue at t=0.5 s and drained it at "
                         f"t=5 s with no new record ({len(data)} written, "
                         f"{writer.dropped} dropped)")
        elif early:
            _fail("F10", f"{deadline_api}() wrote before the deadline had passed "
                         f"({len(early)} line(s) at t=0.5 s with flush_secs=1.0), so this "
                         f"check cannot tell a deadline flush from an unconditional one")
        else:
            _fail("F10", f"after {deadline_api}() at t=5 s the queue had still written "
                         f"nothing (dropped={writer.dropped}, records={writer.records})")
    finally:
        writer.close()


def check_f11(root: Path, tmp: Path) -> None:
    """An old session's cut tail must not stop the reader from reaching a later session."""
    api = _imports(root)
    l2 = tmp / "f11" / "l2"
    l2.mkdir(parents=True, exist_ok=True)
    old_path = l2 / f"l2_NVDA_ONDO-ASTER_old.jsonl"
    new_path = l2 / f"l2_NVDA_ONDO-ASTER_new.jsonl"

    old = api["TapeWriter"](old_path, run_id=RUN_ID, session_id="old", symbol="NVDA",
                            flush_secs=0.0)
    old.open()
    old_session = old.session_id
    for i in range(2):
        old.write_event(api["instrument_event"](
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            metadata=_metadata("2.5", increment="0.001"), coverage_limit=100,
            ts_event_ns=i * MS, ts_init_ns=i * MS))
    old.flush()
    old.close()
    # Cut the final line short, the way a killed process leaves it.
    text = old_path.read_text(encoding="utf-8")
    old_path.write_text(text[: -len(text.splitlines()[-1]) - 1] + '{"event_kind": "inst',
                        encoding="utf-8")

    new = api["TapeWriter"](new_path, run_id=RUN_ID, session_id="new", symbol="NVDA",
                            flush_secs=0.0)
    new.open()
    new_session = new.session_id
    for i in range(2):
        new.write_event(api["instrument_event"](
            symbol="NVDA", venue="ONDO", instrument_id="NVDA-USD-PERP.ONDO",
            metadata=_metadata("2.5", increment="0.001"), coverage_limit=100,
            ts_event_ns=i * MS, ts_init_ns=i * MS))
    new.flush()
    new.close()

    try:
        seen = [r for r in api["read_tape"]([old_path, new_path])]
    except Exception as exc:  # a reader that dies on the old file is also a failure
        _fail("F11", f"the reader raised on the truncated old session: {exc!r}")
        return
    new_records = [r for r in seen if r.get("session_id") == new_session]
    if new_records:
        _pass("F11", f"the later session was read through the old cut tail "
                     f"({len(new_records)} records from {new_session})")
    else:
        _fail("F11", f"the later complete session was never reached: {len(seen)} records "
                     f"seen, none from session {new_session} (old session {old_session})")


def _reject_reasons(report) -> set[str]:
    return {
        reason for market in report.document.get("markets", [])
        for direction in market.get("directions", [])
        for notional in direction.get("notionals", [])
        for reason in notional.get("reject_reasons", {})
    }


def check_f18(root: Path, tmp: Path) -> None:
    """The first depth frame after a reconnect must be usable, not held invalid."""
    def body(writer, legs, api):
        Delta, CLEAR, ADD = api["Delta"], api["CLEAR"], api["ADD"]
        venue, instrument = "ONDO", "NVDA-USD-PERP.ONDO"

        def notice(reason, at_ms):
            writer.write_event(api["status_event"](
                symbol="NVDA", venue=venue, instrument_id=instrument, action="None",
                reason=reason, is_trading=None,
                ts_event_ns=int(at_ms * MS), ts_init_ns=int(at_ms * MS)))

        notice(api["ADAPTER_DISCONNECTED"], 1_000)
        # The adapter's replacement snapshot: the feed is still recovering, but this is a
        # complete two-sided book and is valid on its own account.
        legs[venue].apply_batch(
            [Delta(CLEAR), Delta(ADD, "bid", "100.00", "10"),
             Delta(ADD, "ask", "100.10", "10")],
            source="snapshot", ts_event_ns=int(2_000 * MS), ts_init_ns=int(2_000 * MS))
        notice(api["ADAPTER_SNAPSHOT_READY"], 2_000)
        # The other leg quotes. No second ONDO depth frame ever arrives.
        _push(legs["ASTER"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=3_000)

    run = _write_run(root, tmp, body)
    api = _imports(root)
    report = api["analyse_run"](run, _params(api))
    rejected = _reject_reasons(report)
    rows = _rows(report)
    if not rows:
        _fail("F18", "the tape produced no rows at all, so nothing was recovered")
    elif api["REJECT_BOOK_INVALID"] in rejected:
        _fail("F18", "the first snapshot after the reconnect was held invalid - the feed's "
                     f"state is being folded into the book record (reasons {sorted(rejected)})")
    else:
        _pass("F18", f"one recovered depth frame is usable ({len(rows)} rows, "
                     f"reasons {sorted(rejected) or '{}'})")


def check_f07(root: Path, tmp: Path) -> None:
    """A real venue halt must survive a feed reconnect - snapshot_ready is not a resume."""
    def body(writer, legs, api):
        Delta, CLEAR, ADD = api["Delta"], api["CLEAR"], api["ADD"]
        venue, instrument = "ONDO", "NVDA-USD-PERP.ONDO"
        _push(legs["ONDO"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=1_000)
        _push(legs["ASTER"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=1_000)
        # A real trading status, not a local-feed notice.
        writer.write_event(api["status_event"](
            symbol="NVDA", venue=venue, instrument_id=instrument, action="HALT",
            reason="the venue halted this market", is_trading=False,
            ts_event_ns=int(2_000 * MS), ts_init_ns=int(2_000 * MS)))

        def notice(reason, at_ms):
            writer.write_event(api["status_event"](
                symbol="NVDA", venue=venue, instrument_id=instrument, action="None",
                reason=reason, is_trading=None,
                ts_event_ns=int(at_ms * MS), ts_init_ns=int(at_ms * MS)))

        # The feed drops, recovers and republishes depth. None of that is a resume.
        notice(api["ADAPTER_DISCONNECTED"], 2_500)
        legs[venue].apply_batch(
            [Delta(CLEAR), Delta(ADD, "bid", "100.00", "10"),
             Delta(ADD, "ask", "100.10", "10")],
            source="snapshot", ts_event_ns=int(3_000 * MS), ts_init_ns=int(3_000 * MS))
        notice(api["ADAPTER_SNAPSHOT_READY"], 3_000)
        _push(legs["ONDO"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=4_000)
        _push(legs["ASTER"], bids=[["100.00", "10"]], asks=[["100.10", "10"]], at_ms=4_000)

    run = _write_run(root, tmp, body)
    api = _imports(root)
    report = api["analyse_run"](run, _params(api))
    rejected = _reject_reasons(report)
    if api["REJECT_MARKET_HALTED"] in rejected:
        _pass("F07", f"the halt survived the reconnect (reasons {sorted(rejected)})")
    else:
        _fail("F07", "a reconnect cleared a real venue halt: the market axis is being "
                     f"lifted by adapter:snapshot_ready (reasons {sorted(rejected) or '{}'})")


CHECKS = (check_f06, check_f07, check_f08, check_f09, check_f10, check_f11)

# check_f18 is deliberately NOT in CHECKS: it passes against the pre-fix tree as well, so
# it is not evidence for F18 and must not be counted as such. F18's defect lives in the
# watcher's write path (``SpreadWatch._record_book_batch`` folded ``feed_ready`` into the
# record's ``valid``), and this script writes the tape record itself, bypassing exactly
# the code that was wrong. The falsifiable evidence for F18 is
# ``f18_mutation_counterproof.txt``: the fix is reverted in a disposable copy and the two
# tests that pin it go red at their own assertion.


def main(argv: list[str]) -> int:
    root = Path(argv[1] if len(argv) > 1 else ".").resolve()
    print(f"repository under test: {root}")
    for check in CHECKS:
        with tempfile.TemporaryDirectory() as tmp:
            try:
                check(root, Path(tmp))
            except Exception:
                _fail(check.__name__.replace("check_f", "F"), "raised")
                traceback.print_exc()
    print(f"\n{len(CHECKS) - len(FAILED)}/{len(CHECKS)} findings green")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))

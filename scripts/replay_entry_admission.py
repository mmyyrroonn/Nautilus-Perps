"""Replay recorded timing projections and synthetic boundaries, without venue I/O."""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from opportunity_core import ScanSettings
from opportunity_entry_admission import EntryAdmissionPolicy, temporal_entry_evidence

MS = 1_000_000
NOW = 1_800_000_000_000_000_000


def timing_check(policy, settings, created, checked, rows):
    books = {leg: NS(ts_event_ns=row["source_wall_ns"], ts_received_ns=row["received_wall_ns"])
             for leg, row in rows.items()}
    live = NS(entry_admission=policy, plan=NS(settings=settings), books=books,
              orders={leg: {"sent_ns": created} for leg in rows},
              book_publications={leg: (books[leg], row["published_wall_ns"])
                  for leg, row in rows.items() if row.get("published_wall_ns") is not None})
    position = dict(buy_id="buy", sell_id="sell", entry_ids=("buy", "sell"))
    reason, evidence = temporal_entry_evidence(live, position, checked)
    return dict(result="allowed" if reason == "valid" else "blocked", reason=reason, evidence=evidence)


def synthetic_cases():
    policy, settings = EntryAdmissionPolicy(), ScanSettings()
    result = []
    for budget, limit in (("source", 500), ("receive", 250), ("skew", 250), ("publication", 150), ("dispatch", 100)):
        for offset in (-1, 0, 1):
            rows = {leg: dict(source_wall_ns=NOW, received_wall_ns=NOW, published_wall_ns=NOW)
                    for leg in ("buy", "sell")}
            delta, created = limit * MS + offset, NOW
            if budget == "dispatch":
                created -= delta
            else:
                for leg in (("buy",) if budget == "skew" else ("buy", "sell")):
                    rows[leg]["source_wall_ns"] -= delta
                    if budget in {"receive", "publication"}:
                        rows[leg]["received_wall_ns"] -= delta
                    if budget == "receive":
                        rows[leg]["published_wall_ns"] -= delta
            result.append(dict(case=f"{budget}:{offset:+d}ns", **timing_check(policy, settings, created, NOW, rows)))
    # Hold source->receive at 350 ms and evaluation->handoff at 20 ms.
    # Vary only local publication wait; candidate creation starts at publication.
    for wait in (0, 20, 100, 150, 151):
        rows = {leg: dict(source_wall_ns=NOW - 350 * MS, received_wall_ns=NOW,
                         published_wall_ns=NOW + wait * MS) for leg in ("buy", "sell")}
        result.append(dict(case=f"publication_sweep:{wait}ms", **timing_check(
            policy, settings, NOW + wait * MS, NOW + (wait + 20) * MS, rows)))
    return result


def replay(fixture):
    settings = ScanSettings(**fixture["scan_settings"])
    profiles = {"legacy_timing_projection": EntryAdmissionPolicy(2000, 2000, 500, 2000, 2000),
                "entry_default": EntryAdmissionPolicy()}
    profiles.update({f"source_{age}_only": EntryAdmissionPolicy(age, 2000, 500, 2000, 2000)
                     for age in (250, 500, 600, 750)})
    profiles.update({f"skew_{skew}_only": EntryAdmissionPolicy(2000, 2000, skew, 2000, 2000)
                     for skew in (100, 250)})
    historical = []
    for pair in fixture["pairs"]:
        for index, check in enumerate(pair["checks"]):
            rows = {leg: check["evidence"][leg] for leg in ("buy", "sell")}
            decisions = {}
            for name, policy in profiles.items():
                decision = timing_check(policy, settings, check["candidate_wall_ns"], check["checked_wall_ns"], rows)
                known = [f for f in decision["evidence"]["timing_failures"] if f != "entry_publication_unknown"]
                decision.update(known_timing_result="blocked" if known else "passes_measured_gates",
                                known_failures=known, unmeasured=["publication_wait"])
                decisions[name] = decision
            historical.append(dict(symbol=pair["symbol"], dispatch_index=index + 1, venue=check["venue"],
                original_result=check["original_reason"], decisions=decisions,
                original_economics={k: check["evidence"][k] for k in ("net_usd", "minimum_net_usd", "net_margin_usd")},
                candidate_to_submit_ns=check["candidate_to_submit_ns"]))
    summary = {name: dict(checks_blocked=sum(row["decisions"][name]["known_timing_result"] == "blocked" for row in historical),
                         pairs_blocked_before_first_send=sum(row["dispatch_index"] == 1 and
                             row["decisions"][name]["known_timing_result"] == "blocked" for row in historical))
               for name in profiles}
    return dict(schema_version=1, scope="offline timing projection; historical L2/account/arrival state is not reconstructed",
        source_hashes=fixture["source_hashes"], missing=fixture["missing"], profiles={k:v.document() for k,v in profiles.items()},
        historical=historical, sensitivity=summary, synthetic=synthetic_cases(),
        callback_evidence=[dict(symbol=p["symbol"], hl_rejection_market=p["hl_rejection_market"],
                               submitted_to_response_ms=p["submitted_to_response_ms"]) for p in fixture["pairs"]])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fixture", type=Path, default=ROOT / "tests/fixtures/entry-admission-20261020.json")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = replay(json.loads(args.fixture.read_text(encoding="utf-8")))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["sensitivity"], indent=2))


if __name__ == "__main__":
    main()

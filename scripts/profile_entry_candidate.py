"""Profile qualified entry, account checks and FULL SQLite durability offline.

Uses synthetic immutable whole-account evidence and never constructs a gateway.
Run separate processes with --source-dir to compare a saved baseline.
"""
import argparse
import cProfile
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import pstats
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path)
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--markets-per-venue", type=int, default=160)
    parser.add_argument("--no-profile", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root / "src"))
    if args.source_dir:
        sys.path.insert(0, str(args.source_dir))
    from opportunity_core import BookSnapshot, MarketMetadata, ScanSettings
    from opportunity_live import LiveTradeState
    from opportunity_preflight import AccountSnapshot
    from opportunity_scan import MarketPlan, ScanPlan
    from opportunity_trade import TradePolicy
    from opportunity_trade_store import TradeStore
    import opportunity_timing as timing

    now = 1_800_000_000_000_000_000
    symbols = ["BTC"] + [f"S{i:03}" for i in range(args.markets_per_venue - 1)]
    markets = tuple(MarketPlan(s, v, s + "." + v, D(1), "synthetic")
                    for s in symbols for v in ("HL", "ASTER"))
    plan = ScanPlan(markets, ScanSettings(), None)
    recorder = timing.TimingRecorder()
    elapsed, decisions = [], []
    profile = cProfile.Profile()
    with tempfile.TemporaryDirectory(prefix="entry-profile-") as directory:
        for trial in range(args.rounds):
            with_store = TradeStore(Path(directory) / f"{trial}.sqlite3", "synthetic")
            try:
                live = LiveTradeState(plan, TradePolicy(), max_pairs=2,
                                      max_gross_notional_usd=D(500), store=with_store)
                live.session_id = "replay"
                for venue, bid in (("HL", D("99.99")), ("ASTER", D(101))):
                    instrument = "BTC." + venue
                    live.register_instrument(instrument, D("0.01"), D(1), "USD")
                    live.update_metadata(instrument, MarketMetadata(instrument, D("0.01"),
                        taker_fee_bps=D(0), fee_source="synthetic"), now, evaluate=False)
                    live.update_book(instrument, BookSnapshot(venue, "BTC", ((bid, D(10)),),
                        ((bid + D("0.01"), D(10)),), now, now), now, evaluate=False)
                    positions = tuple((s + "." + venue, D(0)) for s in symbols) + tuple(
                        (f"EXTERNAL{i}.{venue}", D(0)) for i in range(474))
                    live.on_account(AccountSnapshot(venue, instrument, None, None, now, D(0),
                        account_positions=positions, account_open_orders=()), now)
                live.activate(now)
                live.drain_intents()
                timing._active = recorder
                started = time.perf_counter_ns()
                if not args.no_profile:
                    profile.enable()
                assert live._entry("BTC.HL", "BTC.ASTER", now)
                intents = live.drain_intents()
                if not args.no_profile:
                    profile.disable()
                elapsed.append((time.perf_counter_ns() - started) / 1e6)
                timing._active = None
                assert len(intents) == 2
                persisted = with_store.load()
                assert persisted == live.snapshot()
                decisions.append(hashlib.sha256(json.dumps(persisted, sort_keys=True).encode()).hexdigest())
            finally:
                timing._active = None
                with_store.close()
    top = [] if args.no_profile else sorted(pstats.Stats(profile).stats.items(), key=lambda item: -item[1][3])[:18]
    elapsed.sort()
    report = {"rounds": args.rounds, "markets_per_venue": args.markets_per_venue,
        "decision_sha256": hashlib.sha256(json.dumps(decisions).encode()).hexdigest(),
        "qualified_entry_and_durable_release_ms": {f"p{p}": elapsed[int((len(elapsed)-1)*p/100)]
                                                   for p in (50, 95, 99)},
        "profile": [{"file": Path(key[0]).name, "line": key[1], "function": key[2],
                      "calls": value[1], "self_ms": value[2] * 1000, "total_ms": value[3] * 1000}
                     for key, value in top], "timings": recorder.document(),
        "profiler_enabled": not args.no_profile,
        "limits": "Synthetic accepted candidates with timing enabled; no signatures or network sends."}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

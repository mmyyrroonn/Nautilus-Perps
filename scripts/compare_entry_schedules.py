"""Deterministic virtual-time admission replay; never constructs a gateway.

Measures modeled publication waits and actual process CPU separately. Queue,
wire and exchange latency are not simulated. Different schedules may see
different transient opportunities; compare each against its full-scan reference.
"""
from decimal import Decimal as D
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_core import BookSnapshot, MarketMetadata, ScanSettings
from opportunity_live import LiveTradeState
from opportunity_preflight import AccountSnapshot
from opportunity_scan import MarketPlan, ScanPlan
from opportunity_trade import TradePolicy

NOW = 1_800_000_000_000_000_000
VENUES = ("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK")


def events(transient=False):
    return [(ms, venue, "101" if venue == "ASTER" and (
        503 <= ms < (539 if transient else 1100)) else "99.99")
        for ms in range(1, 1201) for i, venue in enumerate(VENUES) if ms % 13 == i]


def replay(source, interval, full=False):
    markets = tuple(MarketPlan("BTC", v, "BTC." + v, D(1), "synthetic") for v in VENUES)
    live = LiveTradeState(ScanPlan(markets, ScanSettings(), None), TradePolicy(),
                          max_pairs=2, max_gross_notional_usd=D(500))
    live.session_id = "replay"
    for m in markets:
        live.register_instrument(m.instrument_id, D("0.01"), D(1), "USD")
        live.update_metadata(m.instrument_id, MarketMetadata(m.instrument_id, D("0.01"),
                             taker_fee_bps=D(0), fee_source="synthetic"), NOW, evaluate=False)
        live.on_account(AccountSnapshot(m.venue, m.instrument_id, D(99), D(100), NOW, D(0), ()), NOW)
    live.activate(NOW)
    live.drain_intents()
    dirty, waits, decisions = {}, [], []
    evaluations = 0

    def publish(ms):
        nonlocal evaluations
        if not dirty:
            return
        changed = set(dirty)
        for instrument, (book, first_ms) in dirty.items():
            waits.append(ms - first_ms)
            live.update_book(instrument, book, NOW + ms * 1_000_000, evaluate=False)
        dirty.clear()
        evaluations += 1
        live.evaluate_symbol("BTC", NOW + ms * 1_000_000, changed_ids=None if full else changed)
        for intent in live.drain_intents():
            if intent.action == "submit":
                decisions.append({"at_ms": ms, "instrument": intent.instrument_id,
                    "side": intent.side, "quantity": str(intent.quantity), "price": str(intent.price)})

    start = time.process_time_ns()
    index = 0
    for ms in range(1, 1201):
        while index < len(source) and source[index][0] == ms:
            _, venue, bid = source[index]
            instrument = "BTC." + venue
            stamp = NOW + ms * 1_000_000
            book = BookSnapshot(venue, "BTC", ((D(bid), D(10)),),
                                ((D(bid) + D("0.01"), D(10)),), stamp, stamp)
            dirty[instrument] = (book, dirty.get(instrument, (None, ms))[1])
            index += 1
            if not interval:
                publish(ms)
        if interval and ms % interval == 0:
            publish(ms)
    cpu_ms = (time.process_time_ns() - start) / 1_000_000
    waits.sort()
    return {"decisions": decisions, "evaluations": evaluations, "cpu_ms": cpu_ms,
            "first_update_to_publication_ms": {f"p{p}": waits[min(len(waits)-1, int((len(waits)-1)*p/100))]
                                               for p in (50, 95, 99)}}


def main():
    report = {"kind": "synthetic virtual-time replay; CPU includes book construction", "scenarios": {}}
    for scenario in ("persistent", "transient"):
        source = events(scenario == "transient")
        rows = {}
        for interval in (100, 20, 0):
            # Windows process clocks have coarse ticks. Average a batch instead
            # of treating a sub-tick run as a measured zero-cost computation.
            started = time.process_time_ns()
            for _ in range(20):
                result = replay(source, interval)
            result["cpu_ms_per_replay_including_setup"] = (time.process_time_ns() - started) / 20e6
            started = time.process_time_ns()
            for _ in range(20):
                reference = replay(source, interval, full=True)
            assert result["decisions"] == reference["decisions"], (scenario, interval)
            result["full_scan_decisions_equal"] = True
            result.pop("cpu_ms")
            result["full_scan_cpu_ms_per_replay_including_setup"] = (time.process_time_ns() - started) / 20e6
            rows[str(interval)] = result
        report["scenarios"][scenario] = {"input_sha256": hashlib.sha256(json.dumps(source).encode()).hexdigest(),
                                         "schedules_ms": rows}
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()

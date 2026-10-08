"""Run the unchanged public connection CLI, extending only its bounded health report.

No factory, event, metadata, market-data behavior or freshness gate is replaced.
The extra fields contain counters and current timestamps/quantities, not history.
"""
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT / "src"))
import opportunity_connections as connections

original_document = connections.ConnectionHealth.document


def document_with_current_legs(health):
    report = original_document(health)
    now_ns = time.time_ns()
    report["per_instrument"] = {}
    for market in health.plan.markets:
        instrument_id = market.instrument_id
        book = health.state.books.get(instrument_id)
        metadata = health.state.metadata.get(instrument_id)
        item = {"venue": market.venue, "symbol": market.symbol,
            "book_updates": health.updates[instrument_id],
            "ever_fresh": instrument_id in health.seen_fresh,
            "current_book_available": book is not None,
            "metadata_available": metadata is not None}
        if book is not None:
            item.update(ts_event_ns=book.ts_event_ns, ts_received_ns=book.ts_received_ns,
                event_age_ms=(now_ns - book.ts_event_ns) / 1_000_000,
                receive_age_ms=(now_ns - book.ts_received_ns) / 1_000_000,
                usable_now=health.usable(instrument_id, book, now_ns),
                bid_levels=len(book.bids), ask_levels=len(book.asks),
                bid_native_quantity=str(sum(quantity for _, quantity in book.bids)),
                ask_native_quantity=str(sum(quantity for _, quantity in book.asks)))
        if metadata is not None:
            item.update(size_increment=str(metadata.size_increment), multiplier=str(metadata.multiplier))
        report["per_instrument"][instrument_id] = item
    report["current_qualified_directions"] = len(health.state.current)
    report["scalar_snapshot_at_ns"] = now_ns
    return report


connections.ConnectionHealth.document = document_with_current_legs
raise SystemExit(connections.main())

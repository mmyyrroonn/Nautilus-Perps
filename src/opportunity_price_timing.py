"""Bounded quote provenance for diagnostics; never used for trading decisions."""
from dataclasses import dataclass, replace
import time


@dataclass(frozen=True, slots=True)
class QuoteTiming:
    source_wall_ns: int
    native_init_wall_ns: int
    sequence: int
    callback_ns: int
    complete_ns: int
    published_ns: int | None = None

    def publish(self):
        return replace(self, published_ns=time.perf_counter_ns())

    def document(self):
        return {name: getattr(self, name) for name in self.__slots__}


def entry_quotes(state, buy_id, sell_id):
    """Copy only the two versions used by this synchronous pricing call."""
    result = {}
    for leg, instrument_id in (("buy", buy_id), ("sell", sell_id)):
        book = state.books.get(instrument_id)
        timing = (state.quote_timings or {}).get(instrument_id)
        if book is None:
            continue
        row = {"venue": book.venue, "instrument_id": instrument_id,
               "source_wall_ns": book.ts_event_ns, "native_init_wall_ns": book.ts_received_ns}
        if timing is not None and (timing.source_wall_ns, timing.native_init_wall_ns) == (
                book.ts_event_ns, book.ts_received_ns):
            row.update(timing.document())
        result[leg] = row
    return result


def pending_stage(live, stage):
    """Observe only bounded, process-local entry intents awaiting dispatch."""
    from opportunity_timing import diagnostics_enabled, record_entry_stage
    if diagnostics_enabled():
        for request_id in live.entry_timings:
            record_entry_stage(request_id, stage)

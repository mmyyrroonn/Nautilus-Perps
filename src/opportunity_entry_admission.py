"""Entry-only freshness budgets. No I/O, repricing or exchange-arrival claims."""
from dataclasses import dataclass

from opportunity_scan import integer_value


@dataclass(frozen=True)
class EntryAdmissionPolicy:
    max_source_age_ms: int = 500
    max_receive_age_ms: int = 250
    max_skew_ms: int = 250
    max_publication_wait_ms: int = 150
    max_dispatch_delay_ms: int = 100

    def __post_init__(self):
        for name in self.__dataclass_fields__:
            integer_value(getattr(self, name), name, 1, 2000)

    def document(self):
        return {name: getattr(self, name) for name in self.__dataclass_fields__}

    def thresholds(self, settings):
        # Entry settings may tighten, never bypass the existing scan checks.
        return {**self.document(),
                "max_source_age_ms": min(self.max_source_age_ms, settings.max_age_ms),
                "max_receive_age_ms": min(self.max_receive_age_ms, settings.max_receive_age_ms),
                "max_skew_ms": min(self.max_skew_ms, settings.max_skew_ms)}


def temporal_entry_evidence(live, position, now_ns):
    """Measure both legs at the same local handoff time, with exact ns boundaries.

    Publication wait is native receive -> strategy publication, including native
    delivery/queueing and coalescing. It is not a network RTT or pure timer wait.
    """
    limits = live.entry_admission.thresholds(live.plan.settings)
    created = min(live.orders[cid]["sent_ns"] for cid in position["entry_ids"])
    delay = now_ns - created
    evidence = {"checked_wall_ns": now_ns, "thresholds": limits,
                "candidate_wall_ns": created, "dispatch_delay_ns": delay,
                "dispatch_deadline_wall_ns": created + limits["max_dispatch_delay_ms"] * 1_000_000}
    evidence["timing_failures"] = []
    for leg in ("buy", "sell"):
        instrument = position[f"{leg}_id"]
        book = live.books.get(instrument)
        if book is None:
            evidence["timing_failures"].append("missing_book")
            continue
        published = live.book_publications.get(instrument)
        # Bind publication evidence to the exact immutable snapshot, not merely
        # its timestamps (a replacement may carry the same source timestamp).
        published_ns = published[1] if published and published[0] is book else None
        source_age, receive_age = now_ns - book.ts_event_ns, now_ns - book.ts_received_ns
        wait = None if published_ns is None else published_ns - book.ts_received_ns
        row = evidence[leg] = {"source_wall_ns": book.ts_event_ns,
            "received_wall_ns": book.ts_received_ns, "published_wall_ns": published_ns,
            "source_age_ns": source_age, "receive_age_ns": receive_age,
            "publication_wait_ns": wait,
            "published_to_check_ns": None if published_ns is None else now_ns - published_ns}
        failures = row["temporal_failures"] = []
        if not 0 < book.ts_event_ns <= book.ts_received_ns <= now_ns:
            failures.append("entry_clock_invalid")
        if published_ns is None:
            failures.append("entry_publication_unknown")
        elif not book.ts_received_ns <= published_ns <= now_ns:
            failures.append("entry_clock_invalid")
        for value, threshold, failure in (
                (source_age, "max_source_age_ms", "entry_source_age"),
                (receive_age, "max_receive_age_ms", "entry_receive_age"),
                (wait, "max_publication_wait_ms", "entry_publication_wait")):
            if value is not None and value > limits[threshold] * 1_000_000:
                failures.append(failure)
        evidence["timing_failures"].extend(failures)
    if "buy" in evidence and "sell" in evidence:
        skew = abs(evidence["buy"]["source_wall_ns"] - evidence["sell"]["source_wall_ns"])
        evidence["source_skew_ns"] = skew
        if skew > limits["max_skew_ms"] * 1_000_000:
            evidence["timing_failures"].append("entry_book_skew")
    if delay < 0:
        evidence["timing_failures"].append("entry_clock_invalid")
    if delay > limits["max_dispatch_delay_ms"] * 1_000_000:
        evidence["timing_failures"].append("entry_dispatch_expired")
    evidence["timing_failures"] = list(dict.fromkeys(evidence["timing_failures"]))
    return next(iter(evidence["timing_failures"]), "valid"), evidence

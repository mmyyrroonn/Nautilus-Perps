"""Bounded, opt-in book lifecycle evidence; never used by trading admission."""
from dataclasses import dataclass

from opportunity_core import BookSnapshot


BOOK_REASONS = frozenset({
    "uninitialized", "snapshot_rebuilding", "partial_update", "pending_publish",
    "disconnected", "connection_changed", "metadata_invalid", "empty_side",
    "stale", "book_discontinuous", "halted", "stopped", "observer_failed",
    "invalidated", "invalid_book", "invalid_timestamp", "published",
})


@dataclass(slots=True)
class BookEvidence:
    # Reuse the immutable published snapshot. Never copy a native ladder.
    last_complete: BookSnapshot | None = None
    published_ns: int | None = None
    current_published_ns: int | None = None
    invalidation_reason: str | None = None
    invalidated_ns: int | None = None
    reason: str = "uninitialized"
    reason_ns: int | None = None
    unavailable_reason: str | None = None
    pending: tuple | None = None

    def invalidate(self, reason, now_ns):
        reason = reason if reason in BOOK_REASONS else "invalidated"
        if self.reason != reason:
            self.reason, self.reason_ns = reason, now_ns
        self.pending = None
        if self.last_complete is not None and self.invalidated_ns is None:
            self.invalidation_reason, self.invalidated_ns = reason, now_ns

    def publish(self, book, now_ns):
        self.pending = None
        self.current_published_ns = now_ns
        if not book.bids or not book.asks:
            self.invalidate("empty_side", now_ns)
        elif not book.valid:
            if self.reason in {"uninitialized", "published", "snapshot_rebuilding", "partial_update"}:
                self.invalidate("invalid_book", now_ns)
        else:
            self.last_complete, self.published_ns = book, now_ns
            self.invalidation_reason = self.invalidated_ns = None
            self.reason, self.reason_ns = "published", now_ns


def book_problem(book, settings, now_ns):
    """Explain the existing validity predicate without relaxing it."""
    if not book.bids or not book.asks:
        return "empty_side"
    if not book.valid or not book._structure_valid:
        return "invalid_book"
    if not 0 < book.ts_event_ns <= book.ts_received_ns <= now_ns:
        return "invalid_timestamp"
    if (now_ns - book.ts_event_ns > settings.max_age_ms * 1_000_000
            or now_ns - book.ts_received_ns > settings.max_receive_age_ms * 1_000_000
            or book.ts_received_ns - book.ts_event_ns > settings.max_age_ms * 1_000_000):
        return "stale"
    return None

"""Coalesce durable order intents into one asynchronous coordinator wakeup."""
from opportunity_timing import record_latency, span, start_latency


class IntentWakeup:
    name = "opportunity-intent-ready"

    def __init__(self, clock, callback):
        self.clock, self.callback = clock, callback
        self.pending = False
        self.closed = False
        self.marker = None

    def request(self):
        if self.closed or self.pending:
            return
        self.pending = True
        self.marker = start_latency()
        try:
            with span("coordinator.schedule_wakeup"):
                self.clock.set_time_alert_ns(self.name, self.clock.timestamp_ns(),
                                             callback=self.callback, allow_past=True)
        except Exception:
            # Orders are already durable. The regular coordinator timer remains
            # the fallback if the optional one-shot alert cannot be scheduled.
            self.pending = False
            self.marker = None

    def consume(self):
        pending = self.pending
        self.pending = False
        record_latency(self.marker, "coordinator.intent_wakeup")
        self.marker = None
        return pending and not self.closed

    def close(self):
        self.closed = True
        if self.pending:
            try:
                self.clock.cancel_timer(self.name)
            except Exception:
                # An expired alert may already be queued or the clock disposed.
                # Optional wakeup cancellation must not prevent gateway close.
                pass
        self.pending = False
        self.marker = None

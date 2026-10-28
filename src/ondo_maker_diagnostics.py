"""Optional, bounded observations which never authorize or block order commands."""

from __future__ import annotations

import functools
import json
import time
from collections import deque


CAPACITY = 2048
PENDING_CAPACITY = 64


def distribution(values):
    values = sorted(values)
    if not values:
        return {"samples": 0}
    count = len(values)
    return {
        "samples": count,
        "p50_ms": values[(count - 1) // 2] / 1_000_000,
        "p95_ms": values[(count * 95 + 99) // 100 - 1] / 1_000_000,
        "p99_ms": values[(count * 99 + 99) // 100 - 1] / 1_000_000,
        "max_ms": values[-1] / 1_000_000,
    }


class OrderLatency:
    def __init__(self):
        self.pending = {}
        self.samples = deque(maxlen=CAPACITY)
        self.accepted = 0
        self.dropped = 0

    def begin(self, client_id, now):
        if client_id in self.pending:
            return
        if len(self.pending) >= PENDING_CAPACITY:
            self.pending.pop(next(iter(self.pending)))
            self.dropped += 1
        self.pending[client_id] = (now, None)

    def submitted(self, client_id, native_ns):
        current = self.pending.get(client_id)
        if current is not None and current[1] is None:
            self.pending[client_id] = (current[0], native_ns)

    def accepted_event(self, client_id, now, native_ns):
        current = self.pending.pop(client_id, None)
        if current is None or now < current[0]:
            return
        native_delta = None
        if current[1] is not None and native_ns >= current[1]:
            native_delta = native_ns - current[1]
        self.samples.append((now - current[0], native_delta))
        self.accepted += 1

    def finish(self, client_id):
        self.pending.pop(client_id, None)

    def snapshot(self):
        return {
            "accepted_total": self.accepted,
            "pending": len(self.pending),
            "dropped": self.dropped,
            "samples_ns": tuple(self.samples),
        }

    def report(self):
        return maker_report(self.snapshot())


class CancelLatency:
    def __init__(self):
        self.pending = {}
        self.samples = deque(maxlen=CAPACITY)
        self.confirmed = 0
        self.retries = 0
        self.rejections = 0
        self.dispatch_errors = 0
        self.closed_without_confirmation = 0
        self.dropped = 0

    def begin(self, client_id, decision_ns, now):
        current = self.pending.get(client_id)
        if current is not None:
            self.pending[client_id] = (current[0], current[1], current[2] + 1)
            self.retries += 1
            return
        if len(self.pending) >= PENDING_CAPACITY:
            self.pending.pop(next(iter(self.pending)))
            self.dropped += 1
        self.pending[client_id] = (decision_ns, now, 1)

    def canceled(self, client_id, now):
        current = self.pending.pop(client_id, None)
        if current is None:
            return
        decision, sent, attempts = current
        if not decision <= sent <= now:
            return
        self.samples.append((sent - decision, now - sent, now - decision, attempts))
        self.confirmed += 1

    def rejected(self, client_id):
        if client_id in self.pending:
            self.rejections += 1

    def dispatch_failed(self, client_id):
        if client_id in self.pending:
            self.dispatch_errors += 1

    def finish(self, client_id):
        if self.pending.pop(client_id, None) is not None:
            self.closed_without_confirmation += 1

    def snapshot(self):
        return {
            "confirmed_total": self.confirmed,
            "pending": len(self.pending),
            "retries": self.retries,
            "rejections": self.rejections,
            "dispatch_errors": self.dispatch_errors,
            "closed_without_cancel_confirmation": self.closed_without_confirmation,
            "dropped": self.dropped,
            "samples_ns": tuple(self.samples),
        }


def observational(method):
    @functools.wraps(method)
    def call(self, *args):
        if not self.enabled:
            return None
        try:
            return method(self, *args)
        except Exception as error:  # noqa: BLE001 - observations must never affect trading
            self.disable(type(error).__name__)
            return None
    return call


class MakerDiagnostics:
    def __init__(self):
        self.enabled = True
        self.error_type = None
        self.maker = OrderLatency()
        self.cancel = CancelLatency()
        self.drive_samples = deque(maxlen=CAPACITY)

    def disable(self, error_type):
        self.enabled = False
        self.error_type = error_type

    @observational
    def maker_begin(self, client_id):
        self.maker.begin(client_id, time.monotonic_ns())

    @observational
    def submitted(self, event):
        self.maker.submitted(str(event.client_order_id), int(event.ts_init))

    @observational
    def cancel_sent(self, client_id, decision_ns):
        self.cancel.begin(client_id, decision_ns, time.monotonic_ns())

    @observational
    def cancel_dispatch_failed(self, client_id):
        self.cancel.dispatch_failed(client_id)

    @observational
    def order_event(self, event):
        kind = type(event).__name__
        if kind == "OrderAccepted":
            self.maker.accepted_event(
                str(event.client_order_id), time.monotonic_ns(), int(event.ts_init)
            )
        elif kind == "OrderCanceled":
            self.cancel.canceled(str(event.client_order_id), time.monotonic_ns())
        elif kind == "OrderCancelRejected":
            self.cancel.rejected(str(event.client_order_id))

    @observational
    def finish(self, client_id):
        self.maker.finish(client_id)
        self.cancel.finish(client_id)

    @observational
    def drive_done(self, started):
        self.drive_samples.append(time.monotonic_ns() - started)

    def snapshot(self):
        if self.enabled:
            try:
                return {
                    "enabled": True,
                    "maker_ack_latency": self.maker.snapshot(),
                    "cancel_latency": self.cancel.snapshot(),
                    "drive_samples_ns": tuple(self.drive_samples),
                }
            except Exception as error:  # noqa: BLE001 - keep state reporting optional
                self.disable(type(error).__name__)
        return {"enabled": False, "error_type": self.error_type}

    def report(self):
        report = format_diagnostics(self.snapshot())
        if report.get("error_type"):
            self.disable(report["error_type"])
        return report


def maker_report(snapshot):
    result = dict(snapshot)
    samples = result.pop("samples_ns")
    result["submit_call_to_accept_callback"] = distribution(x[0] for x in samples)
    result["native_submitted_to_accepted"] = distribution(x[1] for x in samples if x[1] is not None)
    return result


def format_diagnostics(snapshot):
    if not snapshot.get("enabled"):
        return snapshot
    try:
        cancel = dict(snapshot["cancel_latency"])
        samples = cancel.pop("samples_ns")
        cancel["decision_start_to_cancel_call"] = distribution(x[0] for x in samples)
        cancel["first_cancel_call_to_canceled"] = distribution(x[1] for x in samples)
        cancel["decision_start_to_canceled"] = distribution(x[2] for x in samples)
        cancel["confirmed_with_retries_in_window"] = sum(x[3] > 1 for x in samples)
        return {
            "enabled": True,
            "maker_ack_latency": maker_report(snapshot["maker_ack_latency"]),
            "cancel_latency": cancel,
            "decision_cycle": distribution(snapshot["drive_samples_ns"]),
        }
    except Exception as error:  # noqa: BLE001 - formatting runs off the trading loop
        return {"enabled": False, "error_type": type(error).__name__}


def write_status(snapshot):
    snapshot["diagnostics"] = format_diagnostics(snapshot["diagnostics"])
    print(json.dumps(snapshot, sort_keys=True), flush=True)
    return snapshot["diagnostics"].get("error_type")

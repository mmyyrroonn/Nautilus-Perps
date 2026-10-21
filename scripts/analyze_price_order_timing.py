"""Join bounded application entries to native captures using calibrated local clocks."""
import argparse
from collections import defaultdict
import json
from pathlib import Path


PAIRS = (
    ("pricing_started", "candidate"), ("candidate", "persist_started"),
    ("persist_started", "persisted"), ("persisted", "wakeup_requested"),
    ("wakeup_requested", "wakeup_request_returned"), ("wakeup_requested", "wakeup_consumed"),
    ("persisted", "tick_started"), ("tick_started", "gateway_poll_completed"),
    ("gateway_poll_completed", "loss_check_completed"), ("tick_started", "coordinator"),
    ("dispatch_release_started", "dispatch_release_completed"),
    ("dispatch_release_completed", "coordinator"),
    ("coordinator", "send_check"), ("send_check", "python_submit"),
    ("python_submit", "python_submit_returned"), ("pricing_started", "python_submit"),
)
NATIVE_PAIRS = (
    ("native_received", "socket_write_completed"),
    ("socket_write_completed", "read_wake_observed"),
    ("read_wake_observed", "read_poll_started"),
    ("read_poll_started", "socket_read_completed"),
    ("socket_read_completed", "response_dequeued"),
    ("socket_write_completed", "socket_read_completed"),
)


def single(stages, name, clock):
    values = [row[clock] for row in stages if row["stage"] == name]
    return values[0] if len(values) == 1 else None


def intervals(stages, pairs, clock):
    result = {}
    for begin, end in pairs:
        start, stop = single(stages, begin, clock), single(stages, end, clock)
        if start is not None and stop is not None:
            result[begin + "_to_" + end + "_ms"] = (stop - start) / 1e6
    return result


def offsets(app, anchor):
    python = app.get("clock_anchor", {})
    if not (python.get("platform") == anchor.get("platform") == "linux"
            and python.get("monotonic_implementation") == "clock_gettime(CLOCK_MONOTONIC)"
            and anchor.get("clock") == "CLOCK_MONOTONIC"
            and python.get("boot_id") and python["boot_id"] == anchor.get("boot_id")
            and app.get("pid") == anchor.get("pid")):
        return None
    for data, before, after, point in (
            (python, "perf_before_ns", "perf_after_ns", "monotonic_ns"),
            (anchor, "elapsed_before_ns", "elapsed_after_ns", "monotonic_ns")):
        if not all(type(data.get(key)) is int and data[key] >= 0 for key in (before, after, point)):
            return None
        if data[before] > data[after]:
            return None
    return (
        python["monotonic_ns"] - python["perf_after_ns"],
        python["monotonic_ns"] - python["perf_before_ns"],
        anchor["monotonic_ns"] - anchor["elapsed_after_ns"],
        anchor["monotonic_ns"] - anchor["elapsed_before_ns"],
    )


def analyze(app, records, anchors):
    native = defaultdict(list)
    for record in records:
        if record.get("trace_id") and record.get("client_order_id"):
            native[(record["client_order_id"], record["session_id"], record["trace_id"])].append(record)
    entries = []
    for trace in app["entry_execution"]["traces"]:
        stages = trace["stages"]
        submit = single(stages, "python_submit", "monotonic_ns")
        entry = {"trace_id": trace["trace_id"], "venue": trace["venue"],
                 "client_order_id": trace.get("client_order_id"),
                 "local_intervals": intervals(stages, PAIRS, "monotonic_ns"), "quotes": {},
                 "native_join_status": "not_observed"}
        check = next((s for s in stages if s["stage"] == "send_check"), {})
        for phase, quotes in (("pricing", trace.get("quotes", {})), ("send_check", check.get("quotes", {}))):
            for leg, quote in quotes.items():
                row = dict(quote)
                if submit is not None:
                    for name in ("callback_ns", "complete_ns", "published_ns"):
                        if type(quote.get(name)) is int:
                            row[name.removesuffix("_ns") + "_to_python_submit_ms"] = (submit - quote[name]) / 1e6
                if type(quote.get("published_ns")) is int and type(quote.get("complete_ns")) is int:
                    row["complete_to_publish_ms"] = (quote["published_ns"] - quote["complete_ns"]) / 1e6
                entry["quotes"][phase + "_" + leg] = row
        matches = [(key, rows) for key, rows in native.items()
                   if trace.get("client_order_id") and key[0] == trace["client_order_id"]
                   and any(r["stage"] == "native_received" for r in rows)]
        if len(matches) > 1:
            entry["native_join_status"] = "ambiguous_attempts"
        elif len(matches) == 1:
            (_, session, trace_id), observed = matches[0]
            anchor = anchors.get(session, {})
            conversion = offsets(app, anchor)
            entry.update(native_session_id=session, native_trace_id=trace_id,
                         native_intervals=intervals(observed, NATIVE_PAIRS, "elapsed_ns"),
                         native_reported_dropped=max(r.get("dropped", 0) for r in observed),
                         native_join_status="calibrated" if conversion else "clock_unverified")
            if conversion and submit is not None:
                p_low, p_high, n_low, n_high = conversion
                for endpoint in ("native_received", "socket_write_completed"):
                    end = single(observed, endpoint, "elapsed_ns")
                    if end is not None:
                        entry["python_submit_to_" + endpoint + "_ms_bounds"] = [
                            (end + n_low - submit - p_high) / 1e6,
                            (end + n_high - submit - p_low) / 1e6]
                write = single(observed, "socket_write_completed", "elapsed_ns")
                if write is not None:
                    for row in entry["quotes"].values():
                        if type(row.get("callback_ns")) is int:
                            row["callback_to_socket_write_ms_bounds"] = [
                                (write + n_low - row["callback_ns"] - p_high) / 1e6,
                                (write + n_high - row["callback_ns"] - p_low) / 1e6]
        entries.append(entry)
    return {"schema_version": 1, "app_run_id": app["run_id"], "entries": entries,
            "notes": ["Missing or repeated boundaries are omitted, never replaced with zero.",
                      "Quote callback is the complete batch's Python callback, not packet arrival.",
                      "Clock bounds cover calibration call windows, not all measurement overhead.",
                      "Write completion is sink flush, not NIC transmission or exchange receipt.",
                      "Wake observations may originate from socket readiness or the split-stream lock.",
                      "Final-poll time excludes prior partial reads; idle wait is not scheduler delay.",
                      "No client-only interval identifies exchange processing time."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", type=Path, required=True)
    parser.add_argument("--native-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    anchors = {}
    for path in args.native_directory.glob("hyperliquid-clock-*.json"):
        anchor = json.loads(path.read_text())
        anchors[anchor["session_id"]] = anchor
    records = []
    for path in args.native_directory.glob("hyperliquid-timing-*.jsonl"):
        for line in path.read_text().splitlines():
            records.append(json.loads(line))
    result = analyze(json.loads(args.application.read_text()), records, anchors)
    args.output.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()

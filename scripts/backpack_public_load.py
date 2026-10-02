"""Bounded local load evidence for the actual native Backpack public runner."""
from __future__ import annotations

import argparse
import asyncio
import ctypes
from dataclasses import replace
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import platform
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]
from backpack_public import PublicEvidence, application_identity, run_public
from test_backpack_public_native import PublicPeer, SYMBOL, plan_for


def resident_bytes():
    if os.name != "nt":
        return None

    class Memory(ctypes.Structure):
        _fields_ = [("cb", ctypes.c_ulong), ("faults", ctypes.c_ulong),
                    *[(name, ctypes.c_size_t) for name in
                      ("peak", "working", "peak_paged", "paged", "peak_nonpaged",
                       "nonpaged", "pagefile", "peak_pagefile")]]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(Memory), ctypes.c_ulong]
    value = Memory()
    value.cb = ctypes.sizeof(value)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise OSError("process memory sample unavailable")
    return value.working


def percentiles(values):
    if not values:
        return {"samples": 0, "p50_us": None, "p95_us": None, "p99_us": None, "max_us": None}
    ordered = sorted(values)
    return {"samples": len(values), "min_us": ordered[0], "negative_samples": sum(v < 0 for v in values), **{
        f"p{p}_us": ordered[max(0, math.ceil(len(ordered) * p / 100) - 1)]
        for p in (50, 95, 99)}, "max_us": ordered[-1]}


class LoadPeer(PublicPeer):
    def __init__(self, count, rate):
        super().__init__()
        self.count, self.rate = count, rate
        self.offered = 0
        self.send_times = {}
        self.finished = asyncio.Event()
        self.offered_started = None
        self.offered_finished = None

    async def send_frames(self, ws, connection):
        await super().send_frames(ws, connection)
        self.offered += 1  # Initial quote in the shared synthetic fixture.
        self.offered_started = time.monotonic()
        try:
            for i in range(self.count):
                target = self.offered_started + (i + 1) / self.rate
                await asyncio.sleep(max(0, target - time.monotonic()))
                micros = time.time_ns() // 1000
                self.send_times[micros * 1000] = time.perf_counter_ns()
                await ws.send(json.dumps({"stream": f"bookTicker.{SYMBOL}", "data": {
                    "e": "bookTicker", "s": SYMBOL, "E": micros, "T": micros,
                    "u": 10000 + i, "b": "100.0", "B": "1.00000",
                    "a": "101.0", "A": "2.00000"}}))
                self.offered += 1
        finally:
            self.offered_finished = time.monotonic()
            self.finished.set()


async def observe(args, cycle, *, exhaust=False):
    memory = []
    receipt = []
    callback = []
    end_to_end = []
    peer = None
    original = PublicEvidence.record
    stop_started = None

    def record(evidence, kind, fields):
        nonlocal stop_started
        if kind == "quote":
            observed = time.time_ns()
            sent = peer.send_times.get(int(fields["ts_event_ns"]))
            if sent is not None:
                end_to_end.append((time.perf_counter_ns() - sent) / 1000)
            receipt.append((int(fields["ts_received_ns"]) - int(fields["ts_event_ns"])) / 1000)
            callback.append((observed - int(fields["ts_received_ns"])) / 1000)
        original(evidence, kind, fields)
        if evidence.limit_reached and stop_started is None:
            stop_started = time.monotonic()

    async def sample():
        while True:
            value = resident_bytes()
            if value is not None:
                memory.append(value)
            await asyncio.sleep(0.05)

    PublicEvidence.record = record
    sampler = asyncio.create_task(sample())
    try:
        async with LoadPeer(args.messages, args.rate) as peer:
            duration = math.ceil(args.messages / args.rate) + 2
            plan = plan_for(peer, args.output_root / f"cycle-{cycle}", duration=duration)
            plan = replace(plan, request_timeout_secs=5, stale_after_ms=1000,
                           max_report_events=80 if exhaust else args.messages + duration * 50 + 500,
                           max_report_bytes=8_388_608)
            began = time.monotonic()
            summary, path = await asyncio.wait_for(run_public(plan, candidate=args.candidate), duration + 20)
            ended = time.monotonic()
            if not exhaust:
                assert summary["status"] == "completed", summary["failure"]
                assert peer.finished.is_set() and peer.offered == args.messages + 1
                assert len(receipt) == peer.offered
                assert summary["dropped_events"] == 0
                assert len(end_to_end) == args.messages
            else:
                assert summary["status"] == "failed" and summary["failure"] == "report_limit"
                assert summary["report_limit_reached"] and summary["dropped_events"] > 0
            assert summary["shutdown_complete"] and peer.active == 0
            assert not summary["private_client_registered"] and not plan.journal_dir.exists()
            assert summary["native"]["source_binding_verified"]
            assert all(value >= 0 for value in end_to_end)
            peer.assert_public_only()
            files = {p.name: {"bytes": p.stat().st_size,
                             "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                     for p in path.parent.iterdir()}
            assert sum(v["bytes"] for v in files.values()) <= plan.max_report_bytes
            return {"cycle": cycle, "report_exhaustion": exhaust, "status": summary["status"],
                    "failure": summary["failure"], "native": summary["native"],
                    "application": summary["application"], "configuration_sha256": summary["configuration_sha256"],
                    "offered_quotes": peer.offered, "observed_quotes": len(receipt),
                    "offering_elapsed_ms": (peer.offered_finished - peer.offered_started) * 1000
                        if peer.offered_finished is not None else None,
                    "sender_to_native_receipt": percentiles(receipt),
                    "signed_native_receipt_to_python_wall_delta": percentiles(callback),
                    "sender_to_python_observer_monotonic": percentiles(end_to_end),
                    "elapsed_ms_including_candidate_and_shutdown": (ended - began) * 1000,
                    "shutdown_after_limit_ms": (ended - stop_started) * 1000 if stop_started else None,
                    "runtime_ms_including_shutdown": summary["runtime_elapsed_ms"],
                    "http_requests": len(peer.requests), "ws_connections": peer.connections,
                    "active_connections_after_stop": peer.active, "shutdown_complete": summary["shutdown_complete"],
                    "resident_memory_bytes": {"samples": len(memory), "first": memory[0] if memory else None,
                        "last": memory[-1] if memory else None, "min": min(memory) if memory else None,
                        "max": max(memory) if memory else None},
                    "summary_path": str(path), "files": files,
                    "private_client_registered": False, "durable_state_opened": False,
                    "execution_ready": False}
    finally:
        sampler.cancel()
        await asyncio.gather(sampler, return_exceptions=True)
        PublicEvidence.record = original


async def run(args):
    importlib.import_module("nautilus_trader.adapters.backpack")  # Warm import outside samples.
    results = []
    for i in range(args.cycles):
        results.append(await observe(args, i))
    results.append(await observe(args, args.cycles, exhaust=True))
    report = {"schema_version": 1, "source": "Synthetic", "protocol_date": "2026-10-02",
              "platform": platform.platform(), "python": platform.python_version(),
              "application": application_identity(),
              "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
              "messages_per_cycle": args.messages, "target_quotes_per_second": args.rate,
              "cycles": results, "execution_ready": False,
              "limits": ["Short numeric-loopback development-wheel sample; not a long-duration soak.",
                         "Monotonic sender-to-observer latency on one host; no private ACK or production latency measured.",
                         "Native and Python wall clocks can differ; signed clock deltas are retained without clamping and are not reliable hop durations.",
                         "Resident working-set observations include allocator retention and output buffers; no leak-free claim."]}
    args.output_root.mkdir(parents=True, exist_ok=True)
    output = args.output_root / "load-summary.json"
    output.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "completed", "cycles": len(results), "report": str(output)}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", required=True, type=Path)
    parser.add_argument("--candidate-wheel", required=True, type=Path)
    parser.add_argument("--candidate-sha256", required=True)
    parser.add_argument("--native-provenance", required=True, type=Path)
    parser.add_argument("--cycles", type=int, default=3)
    parser.add_argument("--messages", type=int, default=600)
    parser.add_argument("--rate", type=int, default=100)
    args = parser.parse_args()
    if not (1 <= args.cycles <= 10 and 100 <= args.messages <= 20000 and 10 <= args.rate <= 1000):
        parser.error("cycles, messages or rate exceed fixed bounded harness limits")
    if args.messages / args.rate > 30:
        parser.error("offered load must fit a 30-second per-cycle limit")
    args.candidate = (args.candidate_wheel, args.candidate_sha256, args.native_provenance)
    asyncio.run(run(args))


if __name__ == "__main__":
    main()

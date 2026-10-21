"""Bounded, opt-in Linux TCP metadata capture outside the trading process."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import threading
import time


def bounded_int(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"expected {low}..{high}")
        return number
    return parse


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=bounded_int(1, 2**31 - 1), required=True)
    parser.add_argument("--seconds", type=bounded_int(1, 300), default=60)
    parser.add_argument("--local-port", type=bounded_int(0, 65535), default=0)
    parser.add_argument("--max-events", type=bounded_int(1, 100000), default=50000)
    parser.add_argument("--output-directory", type=Path, required=True)
    args = parser.parse_args()
    os.kill(args.pid, 0)
    args.output_directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    script = Path(__file__).with_name("tcp_timing.bt")
    metadata = {"pid": args.pid, "seconds_limit": args.seconds, "local_port": args.local_port,
                "event_limit": args.max_events, "clock": "CLOCK_MONOTONIC",
                "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
                "started_monotonic_ns": time.monotonic_ns(),
                "notes": ["Connections are discovered on this process's send/read calls; initial input can be absent.",
                          "TCP input precedes reassembly; read return precedes TLS/WebSocket decoding.",
                          "Positive read return is bytes copied; negative values are errno, including EAGAIN.",
                          "No automatic packet-to-order attribution; TLS may combine or split frames.",
                          "Capture loss and kernel compatibility warnings are retained in stderr.txt.",
                          "A bounded diagnostic probe adds overhead; it is not enabled by the live service."]}
    count = 0
    with (args.output_directory / "stderr.txt").open("wb") as errors:
        process = subprocess.Popen(["bpftrace", "-q", "-B", "line", str(script),
            str(args.pid), str(args.seconds), str(args.local_port)],
            stdout=subprocess.PIPE, stderr=errors, text=True, start_new_session=True)

        def terminate():
            if process.poll() is None:
                process.send_signal(signal.SIGINT)
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()

        deadline = threading.Timer(args.seconds + 20, terminate)
        deadline.start()
        try:
            with (args.output_directory / "events.csv").open("x", encoding="utf-8") as output:
                for line in process.stdout:
                    if line.startswith(("stage,", "tcp_")):
                        output.write(line)
                        count += int(line.startswith("tcp_"))
                    if count >= args.max_events:
                        terminate()
                        break
            metadata["exit_code"] = process.wait(timeout=10)
        finally:
            terminate()
            deadline.cancel()
    metadata.update(events=count, event_limit_reached=count >= args.max_events,
                    ended_monotonic_ns=time.monotonic_ns())
    (args.output_directory / "metadata.json").write_text(json.dumps(metadata, indent=2) + "\n")
    print(json.dumps(metadata))
    return 0 if metadata["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

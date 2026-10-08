"""Preserve command, UTC times, exit and raw stdout/stderr for bounded public CLI work."""
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys

prefix = Path(sys.argv[1])
command = [sys.executable, *sys.argv[2:]]
record = {"command": command, "started_at_utc": datetime.now(timezone.utc).isoformat()}
with Path(str(prefix) + ".stdout.txt").open("wb") as stdout, Path(str(prefix) + ".stderr.txt").open("wb") as stderr:
    try:
        result = subprocess.run(command, stdout=stdout, stderr=stderr, timeout=110, check=False)
        record["exit_code"] = result.returncode
    except subprocess.TimeoutExpired:
        record.update(exit_code=None, failure="external CLI watchdog expired at 110 seconds")
record["finished_at_utc"] = datetime.now(timezone.utc).isoformat()
Path(str(prefix) + ".command.json").write_text(json.dumps(record, indent=2), encoding="utf-8")
print(json.dumps(record))
sys.exit(record["exit_code"] if record["exit_code"] is not None else 3)

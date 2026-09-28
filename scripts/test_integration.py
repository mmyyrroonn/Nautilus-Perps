"""Run the application tests against one verified, installed native candidate."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from verify_native_install import digest, verify

ROOT = Path(__file__).resolve().parents[1]
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
COUNT_RE = re.compile(
    r"\b(?P<count>\d+)\s+(?P<label>passed|failed|errors?|skipped|xfailed|xpassed|warnings?)\b",
    re.IGNORECASE,
)
COLLECTED_RE = (
    re.compile(r"\bcollected\s+(?P<count>\d+)\s+(?:items?|tests?)\b", re.IGNORECASE),
    re.compile(r"\b(?P<count>\d+)\s+(?:items?|tests?)\s+collected\b", re.IGNORECASE),
)
SUBTEST_RE = re.compile(r"\b(?P<count>\d+)\s+subtests?\s+passed\b", re.IGNORECASE)


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT.resolve())).replace("\\", "/")
    except ValueError:
        return str(path)


def _validate_sha(value: str | None, name: str) -> str | None:
    if value is None:
        return None
    normalized = value.strip().lower()
    if not FULL_SHA.fullmatch(normalized):
        raise ValueError(f"{name} must be a full lowercase commit SHA")
    return normalized


def _forbidden_env_files(root: Path) -> list[str]:
    """Find dotenv credential files without reading their contents."""
    if not root.is_dir():
        return []
    excluded = {".git", ".venv", ".native-cache", ".worktrees", "__pycache__", "target"}
    matches: list[str] = []
    for path in root.rglob("*"):
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            matches.append(_display_path(path))
    return sorted(matches)


def _test_environment() -> dict[str, str]:
    env = os.environ.copy()
    prefixes = ("ASTER_", "HYPERLIQUID_", "LIGHTER_", "ONDO_", "FUTU_", "BINANCE_")
    suffixes = ("_API_KEY", "_API_SECRET", "_PRIVATE_KEY", "_PASSWORD", "_TOKEN")
    for name in tuple(env):
        if name.startswith(prefixes) or name.endswith(suffixes):
            env.pop(name, None)
    env.pop("PYTHONPATH", None)
    env.pop("PYTHONHOME", None)
    env["PYTHONNOUSERSITE"] = "1"
    return env


def _file_record(path: Path) -> dict[str, object]:
    return {"path": _display_path(path), "bytes": path.stat().st_size, "sha256": digest(path)}


def _run_logged(
    command: list[str], *, cwd: Path, env: dict[str, str], stdout_path: Path, stderr_path: Path,
) -> tuple[subprocess.CompletedProcess[bytes], dict[str, dict[str, object]]]:
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, check=False)
    stdout_path.write_bytes(result.stdout)
    stderr_path.write_bytes(result.stderr)
    return result, {"stdout": _file_record(stdout_path), "stderr": _file_record(stderr_path)}


def _pytest_counts(text: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for match in COUNT_RE.finditer(text):
        label = match.group("label").lower()
        if label == "errors":
            label = "error"
        elif label == "warnings":
            label = "warning"
        counts[label] = counts.get(label, 0) + int(match.group("count"))
    return counts


def _junit_counts(path: Path) -> dict[str, int] | None:
    """Read exact test counts from pytest's built-in JUnit XML writer."""
    if not path.is_file():
        return None
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == "testsuite" else list(root.findall("testsuite"))
    if not suites:
        return None
    totals = {name: 0 for name in ("tests", "failures", "errors", "skipped", "warnings")}
    for suite in suites:
        for name in totals:
            try:
                totals[name] += int(float(suite.attrib.get(name, "0")))
            except ValueError:
                continue
    totals["failed"] = totals.pop("failures")
    totals["passed"] = max(
        totals["tests"] - totals["failed"] - totals["errors"] - totals["skipped"],
        0,
    )
    return totals


def _collected_items(text: str) -> int | None:
    matches = [int(match.group("count")) for pattern in COLLECTED_RE for match in pattern.finditer(text)]
    return matches[-1] if matches else None


def _subtests_passed(text: str) -> int | None:
    matches = [int(match.group("count")) for match in SUBTEST_RE.finditer(text)]
    return matches[-1] if matches else None


def _tail(text: str, limit: int = 8) -> list[str]:
    return [line for line in text.splitlines() if line.strip()][-limit:]


def _write_record(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--python", type=Path, required=True)
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--provenance", type=Path, required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--config", type=Path, action="append", default=[])
    parser.add_argument("--native-root", type=Path)
    parser.add_argument("--expected-app-sha")
    parser.add_argument("--expected-native-sha")
    parser.add_argument("--offline-guard", type=Path)
    parser.add_argument("--require-clean", action="store_true")
    parser.add_argument("--fail-on-skip", action="store_true")
    parser.add_argument("--required-test", action="append", default=[])
    parser.add_argument("tests", nargs="*", default=["tests"])
    args = parser.parse_args()

    output = args.output.resolve()
    record: dict[str, Any] = {
        "schema_version": 2,
        "run_id": uuid.uuid4().hex,
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "not_started",
        "failure_layer": "preflight",
        "source_refs": {
            "app": {"expected": args.expected_app_sha},
            "native": {"expected": args.expected_native_sha},
        },
        "test_collection": args.tests,
        "required_tests": args.required_test,
        "network": {
            "policy": "loopback-only" if args.offline_guard else "not-enforced",
            "enforcement": "offline_guard.py" if args.offline_guard else "unknown",
            "verification": "deferred-to-final-audit",
            "guard_record": _display_path(args.offline_guard.resolve())
            if args.offline_guard
            else None,
        },
        "logs": {},
    }
    exit_code = 2
    try:
        expected_app_sha = _validate_sha(args.expected_app_sha, "expected app SHA")
        expected_native_sha = _validate_sha(args.expected_native_sha, "expected native SHA")
        record["source_refs"]["app"]["expected"] = expected_app_sha
        record["source_refs"]["native"]["expected"] = expected_native_sha
        python = Path(os.path.abspath(args.python))
        if not python.is_file():
            raise ValueError("application interpreter is missing")
        if python != Path(os.path.abspath(sys.executable)):
            raise ValueError("run with the explicit application interpreter")
        if sys.version_info[:3] != (3, 12, 9):
            raise ValueError("application CPython 3.12.9 is required")
        native_root = args.native_root.resolve() if args.native_root else None
        env_files = _forbidden_env_files(ROOT)
        if native_root:
            env_files.extend(_forbidden_env_files(native_root))
        if env_files:
            raise ValueError("credential dotenv file present: " + ", ".join(sorted(set(env_files))))
        record["failure_layer"] = "candidate_identity"
        identity = verify(
            args.wheel, args.sha256.lower(), provenance=args.provenance,
            require_source_binding=True, require_ondo=True,
        )
        record["candidate"] = identity
        app_identity = identity["app"]
        native_identity = identity["native_provenance"].get("declared_native") or {}
        record["source_refs"]["app"]["resolved"] = app_identity.get("commit")
        record["source_refs"]["native"]["resolved"] = native_identity.get("commit")
        record["source_refs"]["app"]["match"] = (
            expected_app_sha is None or app_identity.get("commit") == expected_app_sha
        )
        record["source_refs"]["native"]["match"] = (
            expected_native_sha is None or native_identity.get("commit") == expected_native_sha
        )
        record["failure_layer"] = "source_identity"
        if expected_app_sha and app_identity.get("commit") != expected_app_sha:
            raise ValueError("application source identity differs from expected app SHA")
        if expected_native_sha and native_identity.get("commit") != expected_native_sha:
            raise ValueError("native source identity differs from expected native SHA")
        if args.require_clean and app_identity.get("dirty"):
            raise ValueError("application source checkout is dirty")
        if args.require_clean and native_identity.get("dirty_count"):
            raise ValueError("native source checkout is dirty")

        config_hashes: dict[str, str] = {}
        for raw_path in args.config:
            path = raw_path if raw_path.is_absolute() else ROOT / raw_path
            path = path.resolve(strict=True)
            config_hashes[_display_path(path)] = digest(path)
        record["config_sha256"] = config_hashes

        env = _test_environment()
        logs_dir = output.parent / f"{output.stem}.logs"
        logs_dir.mkdir(parents=True, exist_ok=True)
        collection_command = [
            str(python), "-m", "pytest", *args.tests, "--collect-only", "-q",
            "-p", "no:cacheprovider",
        ]
        collection_result, collection_logs = _run_logged(
            collection_command,
            cwd=ROOT,
            env=env,
            stdout_path=logs_dir / "collection.stdout.log",
            stderr_path=logs_dir / "collection.stderr.log",
        )
        collection_text = collection_result.stdout.decode("utf-8", errors="replace")
        collection_text += "\n" + collection_result.stderr.decode("utf-8", errors="replace")
        record["collection"] = {
            "command": collection_command,
            "exit_code": collection_result.returncode,
            "items": _collected_items(collection_text),
            "required_tests": {node: node in collection_text for node in args.required_test},
            "summary": _tail(collection_text),
            "logs": collection_logs,
        }
        record["logs"]["collection"] = collection_logs

        if collection_result.returncode != 0:
            record["failure_layer"] = "pytest_collection"
            raise RuntimeError(f"pytest collection exited {collection_result.returncode}")
        if missing := [node for node, present in record["collection"]["required_tests"].items() if not present]:
            record["failure_layer"] = "pytest_collection"
            raise RuntimeError("required test was not collected: " + ", ".join(missing))

        junit_path = logs_dir / "pytest.junit.xml"
        command = [
            str(python), "-m", "pytest", *args.tests, "-q", "-p", "no:cacheprovider",
            f"--junitxml={junit_path}",
        ]
        result, test_logs = _run_logged(
            command,
            cwd=ROOT,
            env=env,
            stdout_path=logs_dir / "pytest.stdout.log",
            stderr_path=logs_dir / "pytest.stderr.log",
        )
        test_text = result.stdout.decode("utf-8", errors="replace")
        test_text += "\n" + result.stderr.decode("utf-8", errors="replace")
        counts = _junit_counts(junit_path) or _pytest_counts("\n".join(_tail(test_text, 20)))
        subtests_passed = _subtests_passed(test_text)
        item_passed = max(counts.get("passed", 0) - (subtests_passed or 0), 0)
        record["pytest"] = {
            "command": command,
            "exit_code": result.returncode,
            "counts": counts,
            "item_passed": item_passed,
            "subtests_passed": subtests_passed,
            "collected_items": record["collection"]["items"],
            "skipped": counts.get("skipped", 0),
            "summary": _tail(test_text),
            "logs": test_logs,
            "counts_source": "junitxml" if junit_path.is_file() else "summary-fallback",
        }
        if junit_path.is_file():
            record["pytest"]["junit"] = _file_record(junit_path)
        record["logs"]["pytest"] = test_logs
        record["test_command"] = command

        if result.returncode != 0:
            record["failure_layer"] = "pytest"
            raise RuntimeError(f"pytest exited {result.returncode}")
        if counts.get("failed", 0) or counts.get("error", 0):
            record["failure_layer"] = "pytest_audit"
            raise RuntimeError("pytest output contains failed or error counts")
        if args.fail_on_skip and counts.get("skipped", 0):
            record["failure_layer"] = "pytest_audit"
            raise RuntimeError(f"pytest skipped {counts['skipped']} test(s)")

        record["status"] = "passed"
        record["failure_layer"] = None
        exit_code = 0
    except (
        OSError,
        ValueError,
        KeyError,
        ImportError,
        RuntimeError,
        subprocess.SubprocessError,
        ET.ParseError,
    ) as exc:
        record["status"] = "failed"
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
    finally:
        record["exit_code"] = exit_code
        record["finished_at"] = datetime.now(UTC).isoformat()
        try:
            _write_record(output, record)
        except OSError as exc:
            print(f"integration result could not be written: {exc}", file=sys.stderr)
            return 2

    if record["status"] == "passed":
        counts = record.get("pytest", {}).get("counts", {})
        item_passed = record.get("pytest", {}).get("item_passed", counts.get("passed", 0))
        subtests = record.get("pytest", {}).get("subtests_passed")
        print(
            "pytest passed: "
            f"{item_passed} passed"
            + (f" + {subtests} subtests" if subtests is not None else "")
            + ", "
            f"{counts.get('skipped', 0)} skipped; result={output}"
        )
    else:
        print(
            f"integration failed at {record.get('failure_layer')}: "
            f"{record.get('error', {}).get('message', 'unknown error')}; result={output}",
            file=sys.stderr,
        )
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())

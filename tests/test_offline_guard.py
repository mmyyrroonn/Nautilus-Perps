"""Windows loopback guard coverage without changing real firewall rules."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import offline_guard


@pytest.fixture
def executables(tmp_path):
    paths = [tmp_path / name for name in ("venv-python.exe", "base-python.exe", "host-python.exe")]
    for path in paths:
        path.touch()
    return paths


def discovery_payload(executables):
    launcher, base, host = executables
    return {"executable": str(launcher), "base_executable": str(base), "process_executable": str(host)}


def test_selected_python_discovery_covers_launcher_base_and_actual_process(executables, monkeypatch):
    seen = []

    def run(command, **kwargs):
        seen.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps(discovery_payload(executables)), "")

    monkeypatch.setattr(offline_guard, "subprocess", SimpleNamespace(
        run=run, SubprocessError=subprocess.SubprocessError,
    ))
    programs, record = offline_guard._windows_python_programs(executables[0])
    assert programs == executables
    assert record["status"] == "verified" and record["exit_code"] == 0
    assert record["process_executable"] == str(executables[2])
    assert seen[0][0][:4] == [str(executables[0]), "-I", "-S", "-c"]
    assert seen[0][1]["timeout"] == 15


@pytest.mark.parametrize("fault", ["exit", "timeout", "json", "object", "missing", "relative", "other-interpreter"])
def test_unconfirmed_selected_python_is_refused(executables, monkeypatch, fault):
    payload = discovery_payload(executables)
    if fault == "missing":
        payload.pop("process_executable")
    elif fault == "relative":
        payload["process_executable"] = "python.exe"
    elif fault == "other-interpreter":
        payload["executable"] = str(executables[1])

    def run(command, **kwargs):
        if fault == "timeout":
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        return subprocess.CompletedProcess(
            command, 1 if fault == "exit" else 0,
            "invalid-json" if fault == "json" else json.dumps([] if fault == "object" else payload), "",
        )

    monkeypatch.setattr(offline_guard, "subprocess", SimpleNamespace(
        run=run, SubprocessError=subprocess.SubprocessError,
    ))
    programs, record = offline_guard._windows_python_programs(executables[0])
    assert programs == [] and record["status"] == "failed"
    assert record["error"]["message"]


def guard_run(tmp_path, executables, monkeypatch, *, discovery_ok=True, install_code=0,
              command_code=0, cleanup=True):
    output = tmp_path / "guard.json"
    command = [str(executables[0]), "synthetic-test.py"]
    monkeypatch.setattr(sys, "argv", ["offline_guard.py", "--output", str(output), "--", *command])
    monkeypatch.setattr(offline_guard, "platform", SimpleNamespace(
        system=lambda: "Windows", platform=offline_guard.platform.platform,
    ))
    calls = []

    def discover(launcher):
        calls.append(("discover", launcher))
        return executables, {"status": "verified" if discovery_ok else "failed",
                             "launcher": str(launcher), "process_executable": str(executables[2])}

    def install(programs, run_id):
        calls.append(("install", programs))
        return ["synthetic-rule"], {"kind": "program-firewall", "install_exit_code": install_code,
                                   "install_stderr": "synthetic access denied" if install_code else "",
                                   "programs": [str(path) for path in programs]}

    def remove(names):
        calls.append(("cleanup", names))
        if isinstance(cleanup, Exception):
            raise cleanup
        return cleanup

    def run(actual, **kwargs):
        assert actual == command
        calls.append(("command", actual))
        return subprocess.CompletedProcess(actual, command_code)

    monkeypatch.setattr(offline_guard, "_windows_python_programs", discover)
    monkeypatch.setattr(offline_guard, "_install_windows_rules", install)
    monkeypatch.setattr(offline_guard, "_remove_windows_rules", remove)
    monkeypatch.setattr(offline_guard, "subprocess", SimpleNamespace(
        run=run, SubprocessError=subprocess.SubprocessError,
    ))
    code = offline_guard.main()
    return code, json.loads(output.read_text()), calls


def test_guard_does_not_run_command_when_interpreter_discovery_fails(tmp_path, executables, monkeypatch):
    code, record, calls = guard_run(tmp_path, executables, monkeypatch, discovery_ok=False)
    assert code == record["exit_code"] == 2
    assert record["status"] == "guard_failed"
    assert record["interpreter_discovery"]["status"] == "failed"
    assert [name for name, _ in calls] == ["discover"]


def test_failed_rule_install_retains_enforcement_and_cleans_partial_rules(tmp_path, executables, monkeypatch):
    code, record, calls = guard_run(tmp_path, executables, monkeypatch, install_code=5)
    assert code == record["exit_code"] == 2 and record["status"] == "guard_failed"
    assert record["enforcement"]["install_exit_code"] == 5
    assert record["enforcement"]["install_stderr"] == "synthetic access denied"
    assert record["firewall_cleanup"] is True
    assert [name for name, _ in calls] == ["discover", "install", "cleanup"]


@pytest.mark.parametrize("cleanup", [False, OSError("synthetic cleanup error")])
def test_cleanup_failure_overrides_success_and_writes_exit_two(tmp_path, executables, monkeypatch, cleanup):
    code, record, calls = guard_run(tmp_path, executables, monkeypatch, cleanup=cleanup)
    assert code == record["exit_code"] == 2 and record["status"] == "guard_failed"
    assert record["firewall_cleanup"] is False
    assert record["error"]["type"] == "CleanupError"
    assert [name for name, _ in calls] == ["discover", "install", "command", "cleanup"]


@pytest.mark.parametrize("command_code", [0, 7])
def test_verified_guard_covers_selected_executables_and_preserves_command_result(
    tmp_path, executables, monkeypatch, command_code,
):
    code, record, calls = guard_run(tmp_path, executables, monkeypatch, command_code=command_code)
    assert code == record["exit_code"] == command_code
    assert record["status"] == ("passed" if command_code == 0 else "command_failed")
    assert record["enforcement"]["programs"] == [str(path) for path in executables]
    assert record["enforcement"]["interpreter_discovery"]["status"] == "verified"
    assert record["firewall_cleanup"] is True
    assert [name for name, _ in calls] == ["discover", "install", "command", "cleanup"]


"""Run one command with outbound network access restricted to loopback.

Linux uses a private network namespace and raises the loopback interface inside it.
Windows uses temporary outbound firewall rules attached to the exact interpreter that
executes the application test (the Rust extension runs in that process).  Failure to
install either guard is fail-closed: the command is never started and the JSON record
names the enforcement failure.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


def _display(path: Path) -> str:
    return str(path.resolve())


def _write(path: Path, record: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _forbidden_env_files(root: Path) -> list[str]:
    excluded = {".git", ".venv", ".native-cache", ".worktrees", "__pycache__", "target"}
    result: list[str] = []
    if not root.is_dir():
        return result
    for path in root.rglob("*"):
        if any(part in excluded for part in path.relative_to(root).parts):
            continue
        if path.name == ".env" or (path.name.startswith(".env.") and path.name != ".env.example"):
            result.append(str(path))
    return sorted(result)


def _powershell(script: str) -> list[str]:
    executable = shutil.which("powershell.exe") or shutil.which("pwsh")
    if not executable:
        raise RuntimeError("PowerShell is required for the Windows firewall guard")
    return [executable, "-NoProfile", "-NonInteractive", "-Command", script]


def _ps_literal(value: str) -> str:
    """Quote a fixed path/name for a PowerShell single-quoted literal."""
    return "'" + value.replace("'", "''") + "'"


def _windows_python_programs(launcher: Path) -> tuple[list[Path], dict[str, object]]:
    """Discover the selected interpreter's host process without executing test code."""
    code = (
        "import ctypes,json,sys; "
        "image=ctypes.create_unicode_buffer(32768); "
        "length=ctypes.windll.kernel32.GetModuleFileNameW(None,image,len(image)); "
        "assert 0 < length < len(image); "
        "print(json.dumps(dict(executable=sys.executable, "
        "base_executable=sys._base_executable, process_executable=image.value)))"
    )
    discovery: dict[str, object] = {"status": "failed", "launcher": _display(launcher)}
    try:
        result = subprocess.run(
            [str(launcher), "-I", "-S", "-c", code], capture_output=True, text=True,
            check=False, timeout=15,
        )
        discovery["exit_code"] = result.returncode
        if result.returncode != 0:
            raise ValueError("selected Python process image discovery failed")
        value = json.loads(result.stdout)
        if not isinstance(value, dict):
            raise TypeError("selected Python process image discovery is not an object")
        paths = {}
        for field in ("executable", "base_executable", "process_executable"):
            raw = value.get(field)
            if not isinstance(raw, str) or not raw:
                raise ValueError("selected Python process image discovery is incomplete")
            path = Path(raw)
            if not path.is_absolute() or not path.is_file() or path.suffix.lower() != ".exe":
                raise ValueError("selected Python process image executable is unavailable")
            paths[field] = path.resolve()
        if paths["executable"] != launcher.resolve():
            raise ValueError("selected Python process image names another interpreter")
        discovery.update({field: _display(path) for field, path in paths.items()})
        discovery["status"] = "verified"
        return list(dict.fromkeys([launcher.resolve(), *paths.values()])), discovery
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        discovery["error"] = {"type": type(exc).__name__, "message": str(exc)}
        return [], discovery


def _install_windows_rules(programs: list[Path], run_id: str) -> tuple[list[str], dict[str, object]]:
    names = [
        name
        for index in range(len(programs))
        for name in (
            f"CodexIssue3Offline-{run_id}-{index}-v4",
            f"CodexIssue3Offline-{run_id}-{index}-v6",
        )
    ]
    programs_literal = ", ".join(_ps_literal(str(program)) for program in programs)
    names_literal = ", ".join(_ps_literal(name) for name in names)
    script = f"""
$ErrorActionPreference = 'Stop'
$programs = @({programs_literal})
$names = @({names_literal})
$v4 = @(foreach ($octet in 0..255) {{ if ($octet -ne 127) {{ \"$octet.0.0.0/8\" }} }})
for ($index = 0; $index -lt $programs.Count; $index++) {{
    $program = $programs[$index]
    $v4Name = $names[$index * 2]
    $v6Name = $names[$index * 2 + 1]
    New-NetFirewallRule -DisplayName $v4Name -Direction Outbound -Action Block -Profile Any -Program $program -RemoteAddress $v4 | Out-Null
    New-NetFirewallRule -DisplayName $v6Name -Direction Outbound -Action Block -Profile Any -Program $program -RemoteAddress '::2-ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff' | Out-Null
}}
"""
    command = _powershell(script)
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    return names, {
        "kind": "program-firewall",
        "install_exit_code": result.returncode,
        "install_stderr": result.stderr[-4000:],
        "programs": [_display(program) for program in programs],
        "rules": names,
        "ipv4_remote_ranges": "all IPv4 /8 ranges except 127.0.0.0/8",
        "ipv6_remote_range": "::2 through ffff:ffff:ffff:ffff:ffff:ffff:ffff:ffff",
        "loopback_allowed": ["127.0.0.0/8", "::1/128"],
    }


def _remove_windows_rules(names: list[str]) -> bool:
    if not names:
        return True
    names_literal = ", ".join(_ps_literal(name) for name in names)
    script = f"""
$ErrorActionPreference = 'SilentlyContinue'
foreach ($name in @({names_literal})) {{ Remove-NetFirewallRule -DisplayName $name }}
$ErrorActionPreference = 'Stop'
$remaining = @()
foreach ($name in @({names_literal})) {{ $remaining += @(Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) }}
if ($remaining.Count -gt 0) {{ exit 1 }}
exit 0
"""
    result = subprocess.run(_powershell(script), capture_output=True, text=True, check=False)
    return result.returncode == 0


def _linux_command(command: list[str]) -> tuple[list[str], dict[str, object]]:
    unshare = shutil.which("unshare")
    ip = shutil.which("ip")
    if not unshare or not ip:
        raise RuntimeError("Linux loopback namespace requires unshare and ip")

    modes = [
        ("user-network", [unshare, "--user", "--map-root-user", "--net", "--fork", "--mount-proc"]),
    ]
    sudo = shutil.which("sudo")
    if sudo:
        modes.append(("sudo-network", [sudo, "-n", unshare, "--net", "--fork", "--mount-proc"]))
    for mode, prefix in modes:
        probe = subprocess.run(
            [*prefix, "sh", "-c", "ip link set lo up"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.returncode == 0:
            wrapped = [
                *prefix,
                "sh",
                "-c",
                'ip link set lo up && exec "$@"',
                "issue3-offline",
                *command,
            ]
            return wrapped, {
                "kind": "network-namespace",
                "namespace": mode,
                "probe_exit_code": probe.returncode,
                "loopback": "up",
                "non_loopback_interfaces": "absent",
            }
    raise RuntimeError("Linux loopback namespace could not be created by unprivileged or sudo mode")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, action="append", default=[])
    parser.add_argument("--block-program", type=Path, action="append", default=[])
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    run_id = uuid.uuid4().hex
    record: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "started_at": datetime.now(UTC).isoformat(),
        "platform": platform.platform(),
        "policy": "loopback-only",
        "command": command,
        "workspaces": [_display(Path(path)) for path in args.workspace],
        "status": "not_started",
        "exit_code": 2,
    }
    firewall_rules: list[str] = []
    result_code = 2
    try:
        if not command:
            raise ValueError("a command is required after --")
        for raw_root in args.workspace:
            root = raw_root.resolve()
            forbidden = _forbidden_env_files(root)
            if forbidden:
                raise RuntimeError("credential dotenv file present: " + ", ".join(forbidden))
        programs = [Path(os.path.abspath(command[0]))]
        programs.extend(Path(os.path.abspath(path)) for path in args.block_program)
        if any(not program.is_file() for program in programs):
            raise ValueError("guarded command or blocked program executable is missing")

        system = platform.system()
        if system == "Linux":
            wrapped, enforcement = _linux_command(command)
        elif system == "Windows":
            discovered, discovery = _windows_python_programs(programs[0])
            record["interpreter_discovery"] = discovery
            if discovery["status"] != "verified":
                raise RuntimeError("could not confirm the selected Windows Python process image")
            programs = list(dict.fromkeys([*discovered, *programs]))
            firewall_rules, enforcement = _install_windows_rules(programs, run_id)
            enforcement["interpreter_discovery"] = discovery
            record["enforcement"] = enforcement
            if enforcement["install_exit_code"] != 0:
                raise RuntimeError("could not install temporary Windows firewall rules")
            wrapped = command
        else:
            raise RuntimeError(f"unsupported platform for a fail-closed offline guard: {system}")
        record["enforcement"] = enforcement
        record["status"] = "running"
        result = subprocess.run(wrapped, check=False)
        result_code = result.returncode
        record["exit_code"] = result_code
        record["status"] = "passed" if result.returncode == 0 else "command_failed"
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        record["status"] = "guard_failed"
        record["error"] = {"type": type(exc).__name__, "message": str(exc)}
        result_code = 2
        record["exit_code"] = result_code
    finally:
        if firewall_rules:
            try:
                record["firewall_cleanup"] = _remove_windows_rules(firewall_rules)
            except (OSError, RuntimeError, subprocess.SubprocessError):
                record["firewall_cleanup"] = False
            if not record["firewall_cleanup"]:
                record["status"] = "guard_failed"
                record["error"] = {
                    "type": "CleanupError",
                    "message": "temporary Windows firewall rule cleanup failed",
                }
                result_code = 2
                record["exit_code"] = result_code
        record["finished_at"] = datetime.now(UTC).isoformat()
        try:
            _write(args.output.resolve(), record)
        except OSError as exc:
            print(f"offline guard record could not be written: {exc}", file=sys.stderr)
    return result_code


if __name__ == "__main__":
    raise SystemExit(main())

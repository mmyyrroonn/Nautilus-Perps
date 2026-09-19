#!/usr/bin/env python3
"""Capture the runtime and source identity used by the R5.2 data acceptance.

Read-only. Writes one JSON document: interpreter, installed nautilus_trader
wheel identity (path + sha256 + direct_url), python-dotenv version and the
PYTHON_DOTENV_DISABLED support evidence, plus the app worktree git state.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

APP_WORKTREE = Path("E:/Nautilus-Perps/.worktrees/ondo-r52-app")


def sha256_file(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_git(*args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(APP_WORKTREE), *args],
            capture_output=True,
            text=True,
            check=False,
        )
        return result.stdout.strip()
    except OSError as exc:  # pragma: no cover - defensive
        return f"<git failed: {exc}>"


def main() -> int:
    import nautilus_trader

    venv = Path(sys.prefix)
    site = venv / "Lib" / "site-packages"
    dist_info = site / "nautilus_trader-2.0.0rc4.dist-info"
    direct_url = dist_info / "direct_url.json"
    wheel_path = Path(
        "E:/nautilus_trader/dist-r5/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl"
    )

    dotenv_main = None
    dotenv_version = None
    try:
        import dotenv

        dotenv_version = getattr(dotenv, "__version__", None) or getattr(
            __import__("importlib.metadata", fromlist=["version"]), "version"
        )("python-dotenv")
        import dotenv.main as dotenv_main_module

        dotenv_main = Path(dotenv_main_module.__file__)
    except Exception as exc:  # pragma: no cover - defensive
        dotenv_version = f"<import failed: {exc}>"

    dotenv_text = dotenv_main.read_text(encoding="utf-8", errors="replace") if dotenv_main else ""
    direct_url_text = direct_url.read_text(encoding="utf-8").strip() if direct_url.exists() else None

    document = {
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "app_worktree": str(APP_WORKTREE),
        "python": {
            "version": sys.version,
            "executable": sys.executable,
            "prefix": sys.prefix,
        },
        "nautilus_trader": {
            "version": nautilus_trader.__version__,
            "file": nautilus_trader.__file__,
            "dist_info": str(dist_info),
            "direct_url": direct_url_text,
        },
        "wheel": {
            "path": str(wheel_path),
            "size": wheel_path.stat().st_size if wheel_path.exists() else None,
            "sha256": sha256_file(wheel_path),
        },
        "python_dotenv": {
            "version": dotenv_version,
            "main_file": str(dotenv_main) if dotenv_main else None,
            "main_sha256": sha256_file(dotenv_main) if dotenv_main else None,
            "python_dotenv_disabled_supported": "PYTHON_DOTENV_DISABLED" in dotenv_text,
        },
        "git": {
            "head": run_git("rev-parse", "HEAD"),
            "branch": run_git("rev-parse", "--abbrev-ref", "HEAD"),
            "status_porcelain": run_git("status", "--porcelain"),
            "worktree_list": run_git("worktree", "list"),
        },
    }

    out = Path(__file__).resolve().parents[1] / "provenance" / "runtime_identity.json"
    out.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps(document, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

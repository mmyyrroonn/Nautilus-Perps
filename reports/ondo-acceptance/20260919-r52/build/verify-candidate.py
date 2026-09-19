#!/usr/bin/env python3
"""Verify the candidate wheel installation for the R5.2 build task.

Read-only: imports the candidate package, inspects metadata and the installed stub,
and checks the exact native capability marker. Prints a JSON result; exits non-zero
on any failed check.
"""
from __future__ import annotations

import importlib.metadata
import json
import os
import subprocess
import sys
from pathlib import Path

result: dict[str, object] = {"checks": {}}
failed: list[str] = []


def check(name: str, ok: bool, detail: object = None) -> None:
    result["checks"][name] = {"ok": bool(ok), "detail": detail}
    if not ok:
        failed.append(name)


EXPECTED_EXPORTS = sorted(
    [
        "ONDO",
        "ONDO_CLIENT_ID",
        "ONDO_VENUE",
        "OndoDataClientConfig",
        "OndoDataClientFactory",
        "OndoEnvironment",
        "OndoExecutionClientConfig",
        "OndoExecutionClientFactory",
    ]
)

result["sys_executable"] = sys.executable
result["sys_version"] = sys.version
result["python_prefix"] = sys.prefix

# --- import the candidate package -------------------------------------------------
import nautilus_trader  # noqa: E402

pkg_dir = Path(nautilus_trader.__file__).resolve().parent
check("nautilus_import", True, str(pkg_dir))
result["nautilus_version"] = nautilus_trader.__version__
check(
    "imported_package_is_candidate",
    os.path.normcase(str(pkg_dir)).startswith(
        os.path.normcase(os.path.abspath(sys.prefix)) + os.sep
    ),
    {"package": str(pkg_dir), "prefix": os.path.abspath(sys.prefix)},
)

# --- installed metadata / direct_url ---------------------------------------------
dist = importlib.metadata.distribution("nautilus-trader")
result["dist_version"] = dist.version
direct_url = dist.read_text("direct_url.json")
result["direct_url"] = direct_url
du = json.loads(direct_url) if direct_url else {}
url = str(du.get("url", ""))
check(
    "direct_url_points_at_dist_r52",
    "dist-r52" in url.replace("\\", "/"),
    url,
)

# --- ondo factory / config / LiveNode imports ------------------------------------
from nautilus_trader.adapters.ondo import OndoDataClientConfig  # noqa: E402
from nautilus_trader.adapters.ondo import OndoDataClientFactory  # noqa: E402
from nautilus_trader.adapters.ondo import OndoExecutionClientConfig  # noqa: E402
from nautilus_trader.adapters.ondo import OndoExecutionClientFactory  # noqa: E402
from nautilus_trader.live import LiveNode  # noqa: E402

import nautilus_trader.adapters.ondo as ondo_mod  # noqa: E402

check("ondo_factory_import", True, repr(OndoExecutionClientFactory))
check("ondo_config_import", True, repr(OndoExecutionClientConfig))
check("ondo_data_import", True, repr(OndoDataClientFactory))
check("live_node_import", True, repr(LiveNode))

actual_exports = sorted(getattr(ondo_mod, "__all__", []))
result["ondo_all"] = actual_exports
check("public_exports_unchanged", actual_exports == EXPECTED_EXPORTS, actual_exports)

# --- native capability marker -----------------------------------------------------
factory = OndoExecutionClientFactory()
marker = factory.supports_ordered_shutdown
result["capability_value"] = marker
result["capability_python_type"] = type(marker).__name__
check("capability_is_exact_bool", type(marker) is bool, type(marker).__name__)
check("capability_is_true", marker is True, marker)

class_level = getattr(type(factory), "supports_ordered_shutdown", None)
result["capability_class_level"] = type(class_level).__name__
check(
    "capability_is_readonly_property",
    class_level is not None and not isinstance(class_level, bool),
    type(class_level).__name__,
)

# --- installed generated stub -----------------------------------------------------
stub_candidates = list((pkg_dir / "adapters" / "ondo").glob("__init__.pyi"))
check("installed_stub_exists", bool(stub_candidates), [str(p) for p in stub_candidates])
if stub_candidates:
    stub_text = stub_candidates[0].read_text(encoding="utf-8")
    # find the property under OndoExecutionClientFactory
    has_prop = (
        "class OndoExecutionClientFactory" in stub_text
        and "def supports_ordered_shutdown(self) -> bool: ..." in stub_text
    )
    check("stub_declares_readonly_bool_property", has_prop, str(stub_candidates[0]))

# --- conda stripped from a child PATH --------------------------------------------
path = os.environ.get("PATH", "")
sep = os.pathsep
kept = [p for p in path.split(sep) if "miniconda" not in p.lower() and "conda" not in p.lower()]
child_env = dict(os.environ)
child_env["PATH"] = sep.join(kept)
child_code = (
    "import nautilus_trader, sys;"
    "from nautilus_trader.adapters.ondo import OndoExecutionClientFactory as F;"
    "print(nautilus_trader.__version__);"
    "print(F().supports_ordered_shutdown);"
    "print(type(F().supports_ordered_shutdown).__name__)"
)
proc = subprocess.run(
    [sys.executable, "-c", child_code],
    env=child_env,
    capture_output=True,
    text=True,
)
result["conda_stripped_import"] = {
    "returncode": proc.returncode,
    "stdout": proc.stdout.strip(),
    "stderr": proc.stderr.strip()[-2000:],
}
check(
    "conda_stripped_child_import",
    proc.returncode == 0
    and proc.stdout.strip().splitlines()[-2:] == ["True", "bool"],
    proc.stdout.strip().splitlines()[-3:],
)

result["failed"] = failed
print(json.dumps(result, indent=2))
sys.exit(1 if failed else 0)

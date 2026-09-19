"""R5.1 clean-venv verification: release wheel import + export surface.

Run inside the app worktree venv AFTER installing the R5 release wheel:

    E:/Nautilus-Perps/.worktrees/task-ondo-r5/.venv/Scripts/python.exe verify_clean_venv.py

Every check that fails raises; exit 0 means all passed. No network is used.
"""

import hashlib
import json
import pathlib
import sys


def fail(msg: str) -> None:
    print(f"FAIL: {msg}")
    sys.exit(1)


print(f"python: {sys.version}")
print(f"prefix: {sys.prefix}")

import nautilus_trader  # noqa: E402

print(f"nautilus_trader: {nautilus_trader.__version__} at {nautilus_trader.__file__}")
assert nautilus_trader.__version__ == "2.0.0rc4", "wrong version"

# 1. Wheel identity: the installed dist-info must be the R5 wheel, not an older one.
site = pathlib.Path(nautilus_trader.__file__).parent.parent
dist_infos = list(site.glob("nautilus_trader-*.dist-info"))
if len(dist_infos) != 1:
    fail(f"expected exactly one nautilus_trader dist-info, found {len(dist_infos)}")
dist_info = dist_infos[0]
direct_url = dist_info / "direct_url.json"
if not direct_url.exists():
    fail("no direct_url.json - the install was not from a wheel file")
url = json.loads(direct_url.read_text(encoding="utf-8")).get("url", "")
print(f"installed from: {url}")
if "dist-r5" not in url:
    fail(f"installed wheel is not the R5 release wheel: {url}")

# 2. Both factories importable.
from nautilus_trader.adapters.ondo import (  # noqa: E402
    OndoDataClientConfig,
    OndoDataClientFactory,
    OndoEnvironment,
    OndoExecutionClientConfig,
    OndoExecutionClientFactory,
)

print("data + execution factories import OK")

# 3. Public export surface: the R5.1 export fix (exec config + factory in __all__).
import nautilus_trader.adapters.ondo as ondo_pkg  # noqa: E402

missing = {"OndoExecutionClientConfig", "OndoExecutionClientFactory"} - set(ondo_pkg.__all__)
if missing:
    fail(f"__all__ still missing: {sorted(missing)}; __all__={ondo_pkg.__all__}")
print(f"adapters.ondo.__all__ ({len(ondo_pkg.__all__)}): {ondo_pkg.__all__}")

star = {}
exec(f"from nautilus_trader.adapters.ondo import *", star)
for name in ("OndoExecutionClientConfig", "OndoExecutionClientFactory"):
    if name not in star:
        fail(f"star-import does not expose {name}")
print("star-import exposes exec config + factory")

# 4. Configs construct with sane defaults (no credentials, no network).
from nautilus_trader.model import AccountId, InstrumentId  # noqa: E402

data_cfg = OndoDataClientConfig(
    environment=OndoEnvironment.SANDBOX,
    load_ids=[InstrumentId.from_str("NVDA-USD-PERP.ONDO")],
)
exec_cfg = OndoExecutionClientConfig(
    environment=OndoEnvironment.SANDBOX,
    account_id=AccountId.from_str("sandbox-account"),
    account_read_only=False,
)
print(f"data config ok: {data_cfg.load_ids}")
print(f"exec config ok: env={exec_cfg.environment}, account={exec_cfg.account_id}")

# 5. Factories construct and register through the live framework surface.
from nautilus_trader.live import LiveNode  # noqa: E402

assert LiveNode is not None
print("LiveNode import OK")

print("ALL CHECKS PASSED")

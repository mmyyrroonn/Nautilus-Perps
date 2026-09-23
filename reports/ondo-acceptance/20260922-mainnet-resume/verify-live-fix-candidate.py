"""Offline identity and API checks for the post-mainnet-failure Ondo candidate."""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys
import time
import zipfile


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    app = Path("E:/Nautilus-Perps/.worktrees/ondo-mainnet-resume")
    fork = Path("E:/Nautilus-Perps/.worktrees/ondo-btc-native")
    build = app / "reports/ondo-acceptance/20260919-production-native/build"
    assert Path(sys.prefix).resolve() == (app / ".venv").resolve()

    wheels = list((build / "dist-live-fix").glob("nautilus_trader-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    dist = metadata.distribution("nautilus_trader")
    direct_url = json.loads(dist.read_text("direct_url.json"))
    assert direct_url["url"] == wheel.resolve().as_uri()

    with zipfile.ZipFile(wheel) as archive:
        pyd_names = [
            name for name in archive.namelist()
            if name.startswith("nautilus_trader/") and name.endswith(".pyd")
        ]
        assert len(pyd_names) == 1
        pyd_name = pyd_names[0]
        pyd = archive.read(pyd_name)
        assert Path(dist.locate_file(pyd_name)).read_bytes() == pyd
        stub_name = "nautilus_trader/adapters/ondo/__init__.pyi"
        stub = archive.read(stub_name)
        assert stub == (fork / "python" / stub_name).read_bytes()
        assert stub == Path(dist.locate_file(stub_name)).read_bytes()

    source_rows = json.loads(
        (build / "20260922-live-fix-release-source.json").read_text(encoding="utf-8-sig"),
    )
    for row in source_rows:
        assert sha((fork / row["path"]).read_bytes()).upper() == row["sha256"].upper()

    from nautilus_trader.adapters.ondo import (
        OndoEnvironment,
        OndoExecutionClientConfig,
        OndoExecutionClientFactory,
        OndoExecutionEnvelopeConfig,
    )
    from nautilus_trader.model import AccountId, InstrumentId

    now = time.time_ns()
    envelope = OndoExecutionEnvelopeConfig(
        instrument_id=InstrumentId.from_str("NVDA-USD-PERP.ONDO"),
        entry_side="buy",
        entry_max_quantity="0.06",
        entry_worst_price="226.84",
        entry_max_notional_usd="14",
        close_side="sell",
        close_max_quantity="0.06",
        close_worst_price="225.91",
        max_close_attempts=2,
        max_notional_per_order_usd="20",
        max_gross_exposure_usd="20",
        min_available_margin_usdc="25",
        max_orders=3,
        max_new_risk_requests=1,
        max_app_requests=6,
        entry_deadline_unix_nanos=now + 60_000_000_000,
        cleanup_deadline_unix_nanos=now + 120_000_000_000,
        require_flat_start=True,
    )
    config = OndoExecutionClientConfig(
        environment=OndoEnvironment.PRODUCTION,
        account_id=AccountId("ONDO-offline-live-fix-check"),
        account_read_only=False,
        expected_venue_account_id="offline-live-fix-check",
        diagnostics_run_id="offline-live-fix-check",
        dms_timeout_secs=30,
        reconcile_interval_secs=1,
        journal_path=str(build / "offline-never-opened-live-fix-journal.json"),
        allow_production_orders=True,
        execution_envelope=envelope,
    )
    assert config is not None
    factory = OndoExecutionClientFactory()
    assert factory.production_trade_snapshot() is None
    assert factory.read_only_snapshot() is None
    assert factory.supports_production_trade_envelope is True

    print(json.dumps({
        "version": dist.version,
        "wheel": str(wheel),
        "wheel_sha256": sha(wheel.read_bytes()),
        "binary_sha256": sha(pyd),
        "stub_sha256": sha(stub),
        "frozen_source_files_verified": len(source_rows),
        "actual_envelope_and_config_constructed": True,
        "factory_snapshot_initially_none": True,
        "production_trade_capability_marker": True,
        "credentials_read": False,
        "network_started": False,
    }, sort_keys=True))


if __name__ == "__main__":
    main()

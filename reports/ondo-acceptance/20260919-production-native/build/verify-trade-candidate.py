"""Offline identity and API checks for the default-off Ondo trade candidate."""

from __future__ import annotations

import hashlib
from importlib import metadata
import json
from pathlib import Path
import sys
import time
import zipfile


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def main() -> None:
    app = Path("E:/Nautilus-Perps/.worktrees/ondo-production-native")
    fork = Path("E:/nautilus_trader/.worktrees/ondo-production-trade")
    build = Path(__file__).resolve().parent
    assert Path(sys.prefix).resolve() == (app / ".venv").resolve()

    wheels = list((build / "dist-trade-v6").glob("nautilus_trader-*.whl"))
    assert len(wheels) == 1
    wheel = wheels[0]
    wheel_hash = _sha(wheel.read_bytes())
    dist = metadata.distribution("nautilus_trader")
    direct_url = json.loads(dist.read_text("direct_url.json"))
    assert direct_url["url"] == wheel.resolve().as_uri()

    with zipfile.ZipFile(wheel) as archive:
        pyd_names = [name for name in archive.namelist()
                     if name.startswith("nautilus_trader/") and name.endswith(".pyd")]
        assert len(pyd_names) == 1
        pyd_name = pyd_names[0]
        pyd_hash = _sha(archive.read(pyd_name))
        assert _sha(Path(dist.locate_file(pyd_name)).read_bytes()) == pyd_hash
        stub_name = "nautilus_trader/adapters/ondo/__init__.pyi"
        stub = archive.read(stub_name)
        assert stub == (fork / "python" / stub_name).read_bytes()
        assert stub == Path(dist.locate_file(stub_name)).read_bytes()
        for required in (
            b"OndoExecutionEnvelopeConfig", b"min_available_margin_usdc",
            b"execution_envelope", b"supports_production_trade_envelope",
            b"production_trade_snapshot",
        ):
            assert required in stub
        assert b"min_available_margin_usd:" not in stub

    source_rows = json.loads((build / "trade-v6-release-source.json").read_text(encoding="utf-8-sig"))
    for row in source_rows:
        assert _sha((fork / row["path"]).read_bytes()).upper() == row["sha256"].upper()

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
        entry_side="buy", entry_max_quantity="0.05", entry_worst_price="230.00",
        entry_max_notional_usd="15", close_side="sell", close_max_quantity="0.05",
        close_worst_price="225.00", max_close_attempts=2,
        max_notional_per_order_usd="50", max_gross_exposure_usd="100",
        min_available_margin_usdc="25", max_orders=3, max_new_risk_requests=1,
        max_app_requests=6, entry_deadline_unix_nanos=now + 60_000_000_000,
        cleanup_deadline_unix_nanos=now + 120_000_000_000, require_flat_start=True,
    )
    config = OndoExecutionClientConfig(
        environment=OndoEnvironment.PRODUCTION,
        account_id=AccountId("ONDO-offline-trade-check"),
        account_read_only=False,
        expected_venue_account_id="offline-trade-check",
        diagnostics_run_id="offline-trade-candidate-check",
        dms_timeout_secs=30,
        reconcile_interval_secs=1,
        journal_path=str(build / "offline-never-opened-journal.json"),
        allow_production_orders=True,
        execution_envelope=envelope,
    )
    assert config is not None
    factory = OndoExecutionClientFactory()
    assert factory.production_trade_snapshot() is None
    assert factory.read_only_snapshot() is None
    # The marker advertises the reviewed bounded capability. It does not opt in to orders:
    # allow_production_orders remains false by default and the full runtime gates still apply.
    assert factory.supports_production_trade_envelope is True

    result = {
        "version": dist.version,
        "wheel": str(wheel),
        "wheel_sha256": wheel_hash,
        "binary_sha256": pyd_hash,
        "stub_sha256": _sha(stub),
        "frozen_source_files_verified": len(source_rows),
        "actual_envelope_and_config_constructed": True,
        "factory_snapshot_initially_none": True,
        "production_trade_capability_marker": True,
        "credentials_read": False,
        "network_started": False,
    }
    (build / "trade-v6-candidate-identity.json").write_text(
        json.dumps(result, indent=2) + "\n", encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()

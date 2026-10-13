"""Flat-baseline validation never infers safety from incomplete private observations."""
from copy import deepcopy
from pathlib import Path
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from backpack_acceptance_session import validate_flat


def flat_report():
    return {"credential_valid": True, "results": {
        name: {"status": 200, "observation": {"count": 0, name: []}}
        for name in ("positions", "orders")}}


def test_flat_baseline_accepts_empty_and_explicit_zero():
    report = flat_report()
    validate_flat(report)
    report["results"]["positions"]["observation"] = {
        "count": 1, "positions": [{"symbol": "BTC_USDC_PERP", "netQuantity": "0.00000"}]}
    validate_flat(report)


@pytest.mark.parametrize("case", ["credentials", "transport", "missing", "count", "orders",
                                  "long", "short", "nan", "absent_quantity", "numeric_quantity"])
def test_flat_baseline_rejects_risk_and_unknowns(case):
    report = deepcopy(flat_report())
    if case == "credentials":
        report["credential_valid"] = False
    elif case == "transport":
        report["results"]["positions"] = {"error": "transport failed"}
    elif case == "missing":
        del report["results"]["orders"]
    elif case == "count":
        report["results"]["orders"]["observation"]["count"] = 1
    elif case == "orders":
        report["results"]["orders"]["observation"] = {"count": 1, "orders": [{}]}
    else:
        values = {"long": "0.00005", "short": "-0.00005", "nan": "NaN",
                  "absent_quantity": None, "numeric_quantity": 0}
        report["results"]["positions"]["observation"] = {"count": 1, "positions": [
            {"symbol": "BTC_USDC_PERP", "netQuantity": values[case]}]}
    with pytest.raises(ValueError):
        validate_flat(report)


def test_saved_baseline_requires_same_account_and_retains_original_start(tmp_path):
    import base64
    import hashlib
    import json
    import time
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from backpack_session import load_recovery_start

    seed = base64.b64encode(bytes([7]) * 32).decode()
    key = Ed25519PrivateKey.from_private_bytes(bytes([7]) * 32)
    now = time.time_ns() // 1_000_000
    report = flat_report()
    report.update(schema_version=1, ready=True, mode="new-session-flat-preflight",
        recovery_start_ms=now - 1000, completed_at_ms=now,
        credential_scope_sha256=hashlib.sha256(key.public_key().public_bytes_raw()).hexdigest())
    path = tmp_path / "session-baseline.json"
    path.write_text(json.dumps(report), encoding="utf-8")
    assert load_recovery_start(tmp_path, seed) == now - 1000
    with pytest.raises(ValueError, match="different account"):
        load_recovery_start(tmp_path, base64.b64encode(bytes([8]) * 32).decode())
    report.update(recovery_start_ms=now - 121000, completed_at_ms=now - 120000)
    path.write_text(json.dumps(report), encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        load_recovery_start(tmp_path, seed)
    (tmp_path / "identity").mkdir()
    assert load_recovery_start(tmp_path, seed) == now - 121000

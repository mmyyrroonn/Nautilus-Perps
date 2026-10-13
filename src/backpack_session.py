"""Persisted flat baselines for isolated native acceptance sessions."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
import base64
import hashlib
import json
import time


def validate_flat(report):
    """Fail closed on incomplete observations, open orders, or any account exposure."""
    if report.get("credential_valid") is not True:
        raise ValueError("credential validation failed")
    results = report.get("results", {})
    observations = {}
    for name in ("positions", "orders"):
        row = results.get(name, {})
        if row.get("status") != 200 or row.get("error"):
            raise ValueError("flat preflight GET failed; inspect saved report")
        observation = row.get("observation")
        if not isinstance(observation, dict):
            raise ValueError("flat preflight response is incomplete")
        items = observation.get(name)
        count = observation.get("count")
        if not isinstance(items, list) or type(count) is not int or count != len(items):
            raise ValueError("flat preflight response is incomplete")
        observations[name] = items
    if observations["orders"]:
        raise ValueError("account has open orders; no new acceptance session started")
    symbols = set()
    for position in observations["positions"]:
        if not isinstance(position, dict):
            raise ValueError("flat preflight position is incomplete")
        symbol = position.get("symbol")
        value = position.get("netQuantity")
        if (not isinstance(symbol, str) or not symbol or symbol in symbols
                or not isinstance(value, str)):
            raise ValueError("flat preflight position is incomplete")
        symbols.add(symbol)
        try:
            quantity = Decimal(value)
        except InvalidOperation:
            raise ValueError("flat preflight quantity is invalid") from None
        if not quantity.is_finite():
            raise ValueError("flat preflight quantity is invalid")
        if quantity != 0:
            raise ValueError("account is not flat; no new acceptance session started")


def load_recovery_start(state_directory, seed):
    """Reuse the original baseline; never slide history forward on a session restart."""
    path = state_directory / "session-baseline.json"
    if not path.exists():
        return None
    try:
        if path.stat().st_size > 65536:
            raise ValueError
        report = json.loads(path.read_text(encoding="utf-8"))
        validate_flat(report)
        start, completed = report["recovery_start_ms"], report["completed_at_ms"]
        current = time.time_ns() // 1_000_000
        if (report.get("schema_version") != 1 or report.get("ready") is not True
                or report.get("mode") != "new-session-flat-preflight"
                or type(start) is not int or type(completed) is not int
                or not 0 < start <= completed <= current or completed - start > 60000):
            raise ValueError
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        key = Ed25519PrivateKey.from_private_bytes(base64.b64decode(seed, validate=True))
        scope = hashlib.sha256(key.public_key().public_bytes_raw()).hexdigest()
        if scope != report.get("credential_scope_sha256"):
            raise ValueError
        # The native identity namespace pins this same bound when it is first opened.
        # An unused preflight is short lived; an existing session keeps its original bound.
        if not (state_directory / "identity").exists() and current - completed > 60000:
            raise ValueError
        return start
    except Exception:
        raise ValueError("session flat baseline is invalid, stale, or belongs to a different account") from None

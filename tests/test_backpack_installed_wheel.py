"""Require the installed Backpack extension used by the loopback acceptance tests.

This check deliberately fails rather than skips for missing or substituted wheels. The
integration harness supplies the verified wheel digest before collecting these tests.
Transport behavior is covered by the public, account and loopback native test modules.
"""

from __future__ import annotations

import hashlib
import importlib
import importlib.machinery
import json
import os
import re
from importlib.metadata import distribution
from pathlib import Path
from urllib.parse import unquote, urlsplit
from zipfile import ZipFile

BACKPACK_EXPORTS = {
    "BackpackCredential": (),
    "BackpackDataClientConfig": ("telemetry_snapshot_json",),
    "BackpackDataClientFactory": ("name", "capabilities_json"),
    "BackpackExecutionClientConfig": ("telemetry_snapshot_json",),
    "BackpackExecutionClientFactory": ("name", "capabilities_json"),
    "BackpackInstrumentEconomics": (),
    "BackpackLoopbackAccountFacts": (),
    "BackpackLoopbackControl": (
        "begin_session", "accept_account", "refresh_market", "invalidate",
        "pending_fills_json", "telemetry_snapshot_json", "shutdown_report_json",
    ),
    "BackpackLoopbackExecutionAuthority": (),
    "BackpackLoopbackExecutionClientConfig": ("control",),
    "BackpackLoopbackExecutionClientFactory": ("name", "capabilities_json"),
    "BackpackLoopbackSession": ("generation",),
    "BackpackPublicReplay": ("instrument", "apply_record"),
    "BackpackQuota": ("shares_scope",),
}


def _digest(stream) -> str:
    result = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        result.update(chunk)
    return result.hexdigest()


def test_installed_backpack_extension_matches_verified_wheel() -> None:
    """Check wheel identity, real extension origin, exported classes and capabilities."""
    expected = os.environ.get("NAUTILUS_ISSUE3_WHEEL_SHA256", "")
    assert re.fullmatch(r"[0-9a-f]{64}", expected), (
        "run through scripts/test_integration.py with the verified Backpack candidate"
    )
    dist = distribution("nautilus-trader")
    origin = json.loads(dist.read_text("direct_url.json") or "{}")
    url = urlsplit(origin.get("url", ""))
    assert url.scheme == "file" and not url.netloc, "installed wheel requires a local file origin"
    path = unquote(url.path)
    if re.match(r"^/[A-Za-z]:/", path):
        path = path[1:]
    wheel = Path(path)
    assert wheel.is_file() and wheel.suffix == ".whl", "installed wheel source is unavailable"
    with wheel.open("rb") as stream:
        assert _digest(stream) == expected, "installed wheel differs from verified candidate"
    metadata_hash = origin.get("archive_info", {}).get("hashes", {}).get("sha256")
    if metadata_hash is not None:
        assert metadata_hash == expected, "wheel origin metadata digest differs from candidate"

    extension = importlib.import_module("nautilus_trader._libnautilus")
    backpack = importlib.import_module("nautilus_trader.adapters.backpack")
    native_backpack = importlib.import_module("nautilus_trader._libnautilus.backpack")
    assert isinstance(extension.__loader__, importlib.machinery.ExtensionFileLoader)
    native_path = Path(extension.__file__).resolve()
    assert any(str(native_path).endswith(suffix) for suffix in importlib.machinery.EXTENSION_SUFFIXES)
    installed_root = Path(dist.locate_file("")).resolve()
    assert native_path.is_relative_to(installed_root), "native module came from outside installed wheel"
    adapter_path = Path(backpack.__file__).resolve()
    assert adapter_path == installed_root / "nautilus_trader" / "adapters" / "backpack" / "__init__.py"
    stub_path = adapter_path.with_name("__init__.pyi")
    with ZipFile(wheel) as archive:
        for installed in (native_path, adapter_path, stub_path):
            member = installed.relative_to(installed_root).as_posix()
            with installed.open("rb") as actual, archive.open(member) as candidate:
                assert _digest(actual) == _digest(candidate), f"installed wheel file differs: {member}"

    for name, members in BACKPACK_EXPORTS.items():
        exported = getattr(backpack, name)
        assert isinstance(exported, type), f"Backpack export is not a class: {name}"
        assert exported is getattr(native_backpack, name), f"Backpack export is substituted: {name}"
        for member in members:
            assert hasattr(exported, member), f"missing native Backpack API: {name}.{member}"

    quota = backpack.BackpackQuota()
    assert quota.shares_scope(quota)
    assert not quota.shares_scope(backpack.BackpackQuota())
    public = backpack.BackpackDataClientFactory(quota=quota)
    assert public.quota.shares_scope(quota)
    factories = (
        (public, "BackpackDataClientConfig", {"public_market_data": True, "read_only_account": False,
          "restricted_execution": False}),
        (backpack.BackpackExecutionClientFactory(), "BackpackExecutionClientConfig",
         {"read_only_account": True, "restricted_execution": False, "production_writes": False,
          "private_subscription_confirmed": False, "durable_economic_ack": False}),
        (backpack.BackpackLoopbackExecutionClientFactory(), "BackpackLoopbackExecutionClientConfig",
         {"read_only_account": True, "restricted_execution": True, "numeric_loopback_only": True,
          "production_writes": False, "private_subscription_confirmed": False, "durable_economic_ack": False}),
    )
    for factory, config_type, required in factories:
        assert factory.name() == "BACKPACK"
        assert factory.config_type == config_type
        capabilities = json.loads(factory.capabilities_json())
        assert capabilities["schema_version"] == 1
        for name, value in required.items():
            assert capabilities[name] is value, f"unexpected Backpack capability: {name}"


def test_installed_restricted_authority_denies_new_risk_without_http_write(tmp_path, monkeypatch) -> None:
    """The native authority gate denies an actual Strategy order before HTTP mutation."""
    import asyncio
    from dataclasses import replace

    from test_backpack_loopback_native import OrderPeer, event_records, plan_for

    from backpack_loopback import run_loopback

    # Import without importorskip: a missing installed Backpack adapter is a hard failure.
    importlib.import_module("nautilus_trader.adapters.backpack")

    async def scenario() -> None:
        async with OrderPeer() as peer:
            plan = plan_for(peer, tmp_path, monkeypatch)
            # Revoke an already parsed plan to exercise the native gate independently of the
            # application's earlier config rejection. No native class or client is replaced.
            plan = replace(plan, authority=replace(plan.authority, allow_new_risk=False))
            summary, path = await asyncio.wait_for(run_loopback(plan), 23)
            assert summary["status"] == "failed"
            assert summary["shutdown_complete"] and peer.active == 0
            assert not summary["scenario_completed"]
            assert not peer.posts and not peer.deletes
            assert all(method == "GET" for method, _, _ in peer.requests)
            rows = event_records(path)
            assert sum(row["kind"] == "scenario_submit" for row in rows) == 1
            denials = [row for row in rows if row["kind"] == "order_denied"]
            assert len(denials) == 1
            assert denials[0]["reason"] == "SUBMIT_FAILED: Backpack execution refused: UnsupportedCommand"
            assert not any(row["kind"] in {"order_accepted", "order_filled", "order_canceled"}
                           for row in rows)

    asyncio.run(scenario())

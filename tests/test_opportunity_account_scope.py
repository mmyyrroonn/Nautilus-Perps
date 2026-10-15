"""Offline account ownership, cross-process exclusion and crash recovery."""
import base64
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
import opportunity_account_scope as scope
from opportunity_live_runner import SessionStoreProxy, load_live_plan, run_live
from opportunity_trade_store import TradeStore


def credentials():
    # Public fixtures, never copied from an operator credential file.
    return {"HYPERLIQUID_ACCOUNT_ADDRESS": "0x" + "ab" * 20,
            "LIGHTER_ACCOUNT_INDEX": "42", "ASTER_MAINNET_USER_ADDRESS": "0x" + "cd" * 20,
            "ONDO_MAINNET_ACCOUNT_ID": "fixture-account",
            "BACKPACK_MAINNET_API_KEY": base64.b64encode(bytes(range(32))).decode()}


def binding(venues=("HL", "LIGHTER"), **changes):
    return scope.resolve_account_binding(venues, "mainnet", credentials() | changes)


@pytest.fixture
def registry(tmp_path, monkeypatch):
    path = tmp_path / "shared-registry"
    monkeypatch.setattr(scope, "account_registry_directory", lambda: path)
    return path


def owned(path, identity):
    return TradeStore(path, scope.digest(identity.document()), account_binding=identity.document())


def test_normalization_and_key_rotation_do_not_split_real_accounts():
    original = binding(("HL", "LIGHTER", "ASTER", "ONDO"))
    changed = binding(("ONDO", "ASTER", "LIGHTER", "HYPERLIQUID"),
        HYPERLIQUID_ACCOUNT_ADDRESS="0X" + "AB" * 20, LIGHTER_ACCOUNT_INDEX="00042",
        ASTER_MAINNET_USER_ADDRESS="0x" + "CD" * 20,
        HYPERLIQUID_PK="rotated-agent", LIGHTER_API_KEY_INDEX="9",
        ASTER_MAINNET_SIGNER_ADDRESS="rotated-signer", ONDO_MAINNET_API_KEY="rotated-key")
    assert original == changed
    assert binding(("HL",), HYPERLIQUID_VAULT=credentials()["HYPERLIQUID_ACCOUNT_ADDRESS"]) == binding(("HL",))


@pytest.mark.parametrize("venue,key,value", [
    ("HL", "HYPERLIQUID_ACCOUNT_ADDRESS", "0x123"),
    ("HL", "HYPERLIQUID_VAULT", "0x" + "11" * 20),
    ("LIGHTER", "LIGHTER_ACCOUNT_INDEX", "-1"),
    ("LIGHTER", "LIGHTER_ACCOUNT_INDEX", str(2**64)),
    ("LIGHTER", "LIGHTER_ACCOUNT_INDEX", "４２"),
    ("ASTER", "ASTER_MAINNET_USER_ADDRESS", ""),
    ("ONDO", "ONDO_MAINNET_ACCOUNT_ID", " account "),
    ("BACKPACK", "BACKPACK_MAINNET_API_KEY", "invalid")])
def test_ambiguous_or_invalid_identity_fails_without_persistence(tmp_path, venue, key, value):
    with pytest.raises(ValueError):
        binding((venue,), **{key: value})
    assert not list(tmp_path.iterdir())


def test_backpack_rotated_keys_have_distinct_ownership_but_one_risk_domain():
    first = binding(("BACKPACK",))
    other = binding(("BACKPACK",), BACKPACK_MAINNET_API_KEY=base64.b64encode(bytes([19]) * 32).decode())
    assert first.accounts != other.accounts
    assert first.risk_domains == other.risk_domains


def test_environment_and_explicit_subaccounts_have_separate_domains():
    first = binding(("HL", "LIGHTER"))
    other = binding(("HL", "LIGHTER"), LIGHTER_ACCOUNT_INDEX="43",
                    HYPERLIQUID_ACCOUNT_ADDRESS="0x" + "12" * 20)
    testnet = scope.resolve_account_binding(("HL", "LIGHTER"), "testnet", {
        "HYPERLIQUID_TESTNET_ACCOUNT_ADDRESS": credentials()["HYPERLIQUID_ACCOUNT_ADDRESS"],
        "LIGHTER_TESTNET_ACCOUNT_INDEX": "42"})
    assert set(dict(first.risk_domains).values()).isdisjoint(dict(other.risk_domains).values())
    assert set(dict(first.risk_domains).values()).isdisjoint(dict(testnet.risk_domains).values())


@pytest.mark.parametrize("role,matched", [
    ({"role": "agent", "data": {"user": "0x" + "ab" * 20}}, True),
    ({"role": "agent", "data": {"user": "0x" + "cd" * 20}}, False),
    ({"role": "agent", "data": {}}, False), ({"role": "user"}, False),
    ({"role": "missing"}, False), ({"role": "subAccount"}, False), (None, False)])
def test_hl_agent_must_resolve_to_the_bound_user(role, matched):
    pytest.importorskip("eth_account")
    env = credentials() | {"HYPERLIQUID_PK": "0x" + "11" * 32}
    reads = []
    def transport(method, url, payload):
        reads.append((method, url, payload))
        return role
    if matched:
        scope.verify_execution_identity(binding(("HL",)), env, transport=transport)
    else:
        with pytest.raises(ValueError, match="ownership could not be verified"):
            scope.verify_execution_identity(binding(("HL",)), env, transport=transport)
    assert len(reads) == 1 and reads[0][0] == "POST"
    assert reads[0][1] == "https://api.hyperliquid.xyz/info" and reads[0][2]["type"] == "userRole"
    assert env["HYPERLIQUID_PK"] not in json.dumps(reads)


def test_hl_master_and_explicit_vault_binding():
    Account = pytest.importorskip("eth_account").Account
    key = "0x" + "11" * 32
    env = credentials() | {"HYPERLIQUID_PK": key, "HYPERLIQUID_ACCOUNT_ADDRESS": Account.from_key(key).address}
    identity = scope.resolve_account_binding(("HL",), "mainnet", env)
    scope.verify_execution_identity(identity, env, transport=lambda *args: {"role": "user"})
    env["HYPERLIQUID_VAULT"] = env["HYPERLIQUID_ACCOUNT_ADDRESS"]
    def forbidden(*args):
        raise AssertionError("signed target does not require agent lookup")
    scope.verify_execution_identity(identity, env, transport=forbidden)


def test_hl_identity_lookup_failure_never_exposes_credential_or_response():
    pytest.importorskip("eth_account")
    env = credentials() | {"HYPERLIQUID_PK": "0x" + "11" * 32}
    def failed(*args):
        raise RuntimeError("private response fixture")
    with pytest.raises(ValueError) as error:
        scope.verify_execution_identity(binding(("HL",)), env, transport=failed)
    assert "private response" not in str(error.value) and env["HYPERLIQUID_PK"] not in str(error.value)


CHILD = r'''
import json, os, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
import opportunity_account_scope as scope
from opportunity_trade_store import TradeStore
scope.account_registry_directory = lambda: Path(sys.argv[2])
doc = json.loads(sys.argv[4])
binding = scope.AccountBinding(doc["environment"], tuple(sorted(doc["accounts"].items())),
                              tuple(sorted(doc["risk_domains"].items())))
guard = store = None
try:
    guard = scope.AccountRiskGuard(binding)
    store = TradeStore(sys.argv[3], scope.digest(doc), account_binding=doc)
    guard.claim(store)
    if sys.argv[5] == "crash":
        os._exit(19)
    print("acquired")
except (ValueError, OSError):
    print("blocked")
finally:
    if store: store.close()
    if guard: guard.close()
'''


def contender(registry, path, identity, mode="try"):
    return subprocess.run([sys.executable, "-B", "-c", CHILD, str(ROOT / "src"), str(registry),
                           str(path), json.dumps(identity.document()), mode],
                          capture_output=True, text=True, timeout=20, check=False)


@pytest.mark.parametrize("overlap", ["all", "one", "backpack_rotation"])
def test_overlapping_accounts_block_another_process_and_state_path(registry, tmp_path, overlap):
    identity = binding(("HL", "BACKPACK")) if overlap == "backpack_rotation" else binding()
    other = (binding(("BACKPACK",), BACKPACK_MAINNET_API_KEY=base64.b64encode(bytes([19]) * 32).decode())
             if overlap == "backpack_rotation" else binding(("LIGHTER", "ASTER")) if overlap == "one" else identity)
    guard = scope.AccountRiskGuard(identity)
    store = owned(tmp_path / "first.sqlite3", identity)
    try:
        guard.claim(store)
        child = contender(registry, tmp_path / "different.sqlite3", other)
        assert child.returncode == 0 and child.stdout.strip() == "blocked", child.stderr
        assert not (tmp_path / "different.sqlite3").exists()
    finally:
        store.close()
        guard.close()


def test_disjoint_subaccounts_can_run_concurrently(registry, tmp_path):
    identity = binding()
    other = binding(LIGHTER_ACCOUNT_INDEX="43", HYPERLIQUID_ACCOUNT_ADDRESS="0x" + "13" * 20)
    guard = scope.AccountRiskGuard(identity)
    store = owned(tmp_path / "first.sqlite3", identity)
    try:
        guard.claim(store)
        child = contender(registry, tmp_path / "independent.sqlite3", other)
        assert child.returncode == 0 and child.stdout.strip() == "acquired", child.stderr
    finally:
        store.close()
        guard.close()


def test_clean_release_allows_new_ledger_only_after_os_lock_closes(registry, tmp_path):
    identity = binding()
    guard = scope.AccountRiskGuard(identity)
    store = owned(tmp_path / "first.sqlite3", identity)
    try:
        guard.claim(store)
        guard.release_clean()
        assert contender(registry, tmp_path / "second.sqlite3", identity).stdout.strip() == "blocked"
    finally:
        store.close()
        guard.close()
    assert contender(registry, tmp_path / "second.sqlite3", identity).stdout.strip() == "acquired"


def test_crash_releases_os_lock_but_keeps_original_ledger_claim(registry, tmp_path):
    identity = binding()
    original = tmp_path / "original.sqlite3"
    assert contender(registry, original, identity, "crash").returncode == 19
    assert contender(registry, tmp_path / "new.sqlite3", identity).stdout.strip() == "blocked"
    assert contender(registry, original, identity).stdout.strip() == "acquired"


def test_deleted_or_copied_ledger_cannot_take_over_persistent_claim(registry, tmp_path):
    import shutil
    identity = binding()
    original = tmp_path / "original.sqlite3"
    assert contender(registry, original, identity, "crash").returncode == 19
    copy = tmp_path / "copy.sqlite3"
    shutil.copyfile(original, copy)
    assert contender(registry, copy, identity).stdout.strip() == "blocked"
    original.unlink()
    assert contender(registry, original, identity).stdout.strip() == "blocked"


def test_failed_multi_domain_claim_rolls_back_and_releases_partial_locks(registry, tmp_path):
    lighter = binding(("LIGHTER",))
    original = tmp_path / "lighter.sqlite3"
    assert contender(registry, original, lighter, "crash").returncode == 19
    assert contender(registry, tmp_path / "both.sqlite3", binding()).stdout.strip() == "blocked"
    assert contender(registry, tmp_path / "hl.sqlite3", binding(("HL",))).stdout.strip() == "acquired"


def test_same_file_cannot_inherit_another_account_even_before_first_snapshot(tmp_path):
    path = tmp_path / "state.sqlite3"
    original = binding()
    store = owned(path, original)
    owner = store.owner_id
    store.close()
    with pytest.raises(ValueError, match="different account"):
        owned(path, binding(LIGHTER_ACCOUNT_INDEX="43"))
    with pytest.raises(ValueError, match="different account"):
        TradeStore(path, scope.digest(original.document()))
    restored = owned(path, original)
    assert restored.owner_id == owner
    restored.close()


def test_legacy_ledger_is_not_silently_bound_to_current_credentials(tmp_path):
    path = tmp_path / "legacy.sqlite3"
    store = TradeStore(path, "legacy")
    store.save({"positions": [{"owned": True}]}, [])
    store.close()
    with pytest.raises(ValueError, match="legacy state"):
        owned(path, binding())
    original = TradeStore(path, "legacy")
    assert original.load() == {"positions": [{"owned": True}]}
    original.close()


def test_runtime_fingerprint_is_bound_but_dry_plan_remains_credential_free(tmp_path):
    from test_opportunity_live_runner import configs
    plan = load_live_plan(configs(tmp_path))
    first = plan.fingerprint(binding().document())
    second = plan.fingerprint(binding(LIGHTER_ACCOUNT_INDEX="43").document())
    assert first != second and first != plan.fingerprint()
    assert replace(plan, state_path=tmp_path / "other.sqlite3").fingerprint(binding().document()) == first
    assert plan.document()["fingerprint_scope"] == "configuration_only"


def test_pending_orders_keep_budget_and_original_ids_after_restart(registry, tmp_path):
    from test_opportunity_live import state, submissions
    from test_opportunity_trade import publish
    identity = binding()
    path = tmp_path / "pending.sqlite3"
    guard = scope.AccountRiskGuard(identity)
    store = owned(path, identity)
    try:
        guard.claim(store)
        live, _ = state(store=SessionStoreProxy(store))
        publish(live)
        intents = submissions(live)
        assert len(intents) == 2
        reserved = live.gross_exposure(reserve=True)
        assert reserved >= 200
        original_ids = set(live.orders)
    finally:
        store.close()
        guard.close()
    assert contender(registry, tmp_path / "fresh.sqlite3", identity).stdout.strip() == "blocked"
    guard = scope.AccountRiskGuard(identity)
    store = owned(path, identity)
    try:
        guard.claim(store)
        live, _ = state(store=SessionStoreProxy(store), activate=False)
        assert live.phase == "recovery_blocked" and not live.activate(1)
        assert set(live.orders) == original_ids and not submissions(live)
        assert live.gross_exposure(reserve=True) == reserved
    finally:
        store.close()
        guard.close()


def test_only_digests_are_persisted_in_binding_registry_and_envelope(registry, tmp_path):
    identity = binding(("HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"))
    guard = scope.AccountRiskGuard(identity)
    store = owned(tmp_path / "private.sqlite3", identity)
    try:
        guard.claim(store)
        SessionStoreProxy(store).save({"positions": [], "orders": {}}, [])
        persisted = json.dumps(identity.document()) + "\n".join(store.connection.iterdump())
        persisted += "\n".join(guard.connection.iterdump())
        for key in ("HYPERLIQUID_ACCOUNT_ADDRESS", "ASTER_MAINNET_USER_ADDRESS",
                    "ONDO_MAINNET_ACCOUNT_ID", "BACKPACK_MAINNET_API_KEY"):
            assert credentials()[key] not in persisted
        assert all(len(value) == 64 for value in dict(identity.accounts).values())
    finally:
        store.close()
        guard.close()


@pytest.mark.parametrize("failure", [None, "identity", "environment", "build", "run", "dispose", "gateway_close", "dirty", "pending", "save"])
@pytest.mark.parametrize("timing_enabled", [False, True])
def test_runner_holds_claim_through_cleanup_and_only_releases_clean_sessions(
        registry, tmp_path, monkeypatch, capsys, failure, timing_enabled):
    if timing_enabled:
        import opportunity_timing
        monkeypatch.setattr(opportunity_timing, "_active", opportunity_timing.TimingRecorder())
    dotenv = pytest.importorskip("dotenv")
    import opportunity_live_runner as runner
    import opportunity_shutdown
    from test_opportunity_live_runner import configs
    plan = load_live_plan(configs(tmp_path, venues='["HL","LIGHTER"]', mandatory="false"))
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *args, **kwargs: None)
    monkeypatch.setattr(os, "environ", credentials())
    def verify(*args):
        if failure == "identity":
            raise ValueError("synthetic ownership failure")
    monkeypatch.setattr(scope, "verify_execution_identity", verify)
    monkeypatch.setattr(opportunity_shutdown, "collect_native_shutdown",
                        lambda controls: {"clean": failure != "dirty", "venues": {}})
    snapshots = []

    def build(plan, live, coordinator, *, environ):
        assert environ is not os.environ and dict(environ) == credentials()
        with pytest.raises(TypeError):
            environ["LIGHTER_ACCOUNT_INDEX"] = "43"
        assert contender(registry, tmp_path / "other.sqlite3", binding()).stdout.strip() == "blocked"
        if failure == "build":
            raise RuntimeError("synthetic build failure")
        if failure == "environment":
            os.environ["LIGHTER_ACCOUNT_INDEX"] = "43"
        def run():
            coordinator.finished = True
            if failure == "run":
                raise RuntimeError("synthetic run failure")
            if failure == "pending":
                live.orders["pending"] = {"terminal_ns": None}
            if failure == "save":
                def fail_save(*args):
                    raise OSError("synthetic persistence failure")
                monkeypatch.setattr(coordinator.proxy.store, "save", fail_save)
        def close():
            if failure == "gateway_close":
                raise RuntimeError("synthetic close failure")
        coordinator.gateway = SimpleNamespace(close=close)
        def dispose():
            assert contender(registry, tmp_path / "other.sqlite3", binding()).stdout.strip() == "blocked"
            snapshots.append(live.snapshot())
            if failure == "dispose":
                raise RuntimeError("synthetic dispose failure")
        return SimpleNamespace(run=run, dispose=dispose), SimpleNamespace(failure=None), object()

    monkeypatch.setattr(runner, "build_live_node", build)
    if failure in {"identity", "environment", "build", "dispose", "gateway_close", "save"}:
        with pytest.raises((RuntimeError, OSError, ValueError)):
            run_live(plan)
    else:
        assert run_live(plan) == (0 if failure is None else 1)
    assert bool(snapshots) == (failure not in {"build", "identity"})
    # A fresh process can acquire the OS lock after all exits, but only a clean
    # session lets it create a new risk budget under a different state path.
    outcome = contender(registry, tmp_path / "other.sqlite3", binding()).stdout.strip()
    assert outcome == ("acquired" if failure is None else "blocked")
    capsys.readouterr()

"""Credential-free account bindings and host-local, durable risk-domain claims.

The registry is shared by all worktrees of the current OS user. OS locks end on
process exit; claims do not. Only a clean, disposed session may release a claim.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from opportunity_trade_store import FileLock
from opportunity_timing import span, timed


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _required(env, name):
    value = env.get(name)
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"explicit {name} is required for account binding")
    return value


def _address(env, name):
    value = _required(env, name)
    if not re.fullmatch(r"0[xX][0-9a-fA-F]{40}", value):
        raise ValueError(f"invalid {name} for account binding")
    return value.lower()


def lighter_account_index(env, environment):
    prefix = "LIGHTER_" if environment == "mainnet" else "LIGHTER_TESTNET_"
    value = _required(env, prefix + "ACCOUNT_INDEX")
    if not re.fullmatch(r"[0-9]{1,20}", value) or int(value) >= 2**64:
        raise ValueError("invalid Lighter account index for account binding")
    return int(value)


@dataclass(frozen=True)
class AccountBinding:
    environment: str
    accounts: tuple[tuple[str, str], ...]
    risk_domains: tuple[tuple[str, str], ...]

    def document(self):
        return {"schema_version": 1, "environment": self.environment,
                "accounts": dict(self.accounts), "risk_domains": dict(self.risk_domains)}


@timed("account_lock.resolve_binding")
def resolve_account_binding(venues, environment, environ):
    """Resolve the exact execution scope, never the agent/signer or API-key slot.

    Backpack cannot prove a stable user ID from an empty account response. Keep
    ALL its credentials/subaccounts in one conservative risk domain, while
    binding ledger ownership to the credential. Key rotation cannot evade the
    risk lock or silently inherit the old native client-ID journal.
    """
    if environment not in {"mainnet", "testnet"}:
        raise ValueError("unsupported account binding environment")
    accounts, domains = {}, {}
    for raw_venue in venues:
        venue = "HL" if raw_venue == "HYPERLIQUID" else raw_venue
        if venue in accounts:
            raise ValueError("duplicate account binding venue")
        if venue == "HL":
            prefix = "HYPERLIQUID_" if environment == "mainnet" else "HYPERLIQUID_TESTNET_"
            identity = _address(environ, prefix + "ACCOUNT_ADDRESS")
            if environ.get(prefix + "VAULT") and _address(environ, prefix + "VAULT") != identity:
                raise ValueError("Hyperliquid vault must match the reconciled trading account")
        elif venue == "LIGHTER":
            identity = str(lighter_account_index(environ, environment))
        elif venue == "ASTER":
            prefix = "ASTER_MAINNET_" if environment == "mainnet" else "ASTER_"
            identity = _address(environ, prefix + "USER_ADDRESS")
        elif venue == "ONDO":
            prefix = "ONDO_MAINNET_" if environment == "mainnet" else "ONDO_SANDBOX_"
            identity = _required(environ, prefix + "ACCOUNT_ID")
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", identity):
                raise ValueError("invalid Ondo venue account identity")
        elif venue == "BACKPACK":
            if environment != "mainnet":
                raise ValueError("Backpack has no configured testnet risk domain")
            identity = _required(environ, "BACKPACK_MAINNET_API_KEY")
            try:
                public = base64.b64decode(identity, validate=True)
                if len(public) != 32 or base64.b64encode(public).decode() != identity:
                    raise ValueError
            except Exception:
                raise ValueError("invalid Backpack public-key identity") from None
        else:
            raise ValueError("unsupported account binding venue")
        accounts[venue] = digest(["account-v1", environment, venue, identity])
        domains[venue] = (digest(["risk-v1", environment, venue, "all-accounts"])
                          if venue == "BACKPACK" else accounts[venue])
    if not accounts:
        raise ValueError("account binding requires at least one venue")
    return AccountBinding(environment, tuple(sorted(accounts.items())), tuple(sorted(domains.items())))


def account_registry_directory():
    # Never derive this location from a state path, repository, CWD or CLI option.
    return Path.home() / ".nautilus-perps" / "account-risk-v1"


@timed("account_lock.verify_identity")
def verify_execution_identity(binding, environ, *, transport=None):
    """Prove that an HL agent signs for the account being locked and queried.

    Explicit vault/subaccount addresses are part of the signed order itself.
    Without that target, the exchange resolves the signer to a user; a locally
    supplied ACCOUNT_ADDRESS alone does not control where orders execute.
    """
    if "HL" not in dict(binding.accounts):
        return
    from eth_account import Account
    from opportunity_account_reader import http_json
    prefix = "HYPERLIQUID_" if binding.environment == "mainnet" else "HYPERLIQUID_TESTNET_"
    account = _address(environ, prefix + "ACCOUNT_ADDRESS")
    try:
        signer = Account.from_key(_required(environ, prefix + "PK")).address.lower()
    except Exception:
        raise ValueError("Hyperliquid signing identity is unavailable or invalid") from None
    if environ.get(prefix + "VAULT"):
        if _address(environ, prefix + "VAULT") != account:
            raise ValueError("Hyperliquid signed target differs from the account binding")
        return
    url = ("https://api.hyperliquid.xyz/info" if binding.environment == "mainnet"
           else "https://api.hyperliquid-testnet.xyz/info")
    try:
        role = (transport or http_json)("POST", url, {"type": "userRole", "user": signer})
        if isinstance(role, dict) and role.get("role") == "user":
            owner = signer
        elif isinstance(role, dict) and role.get("role") == "agent" and isinstance(role.get("data"), dict):
            owner = _address({"owner": role["data"].get("user")}, "owner")
        else:
            raise ValueError
        if owner != account:
            raise ValueError
    except Exception:
        raise ValueError("Hyperliquid signer ownership could not be verified for the bound account") from None


class AccountRiskGuard:
    @timed("account_lock.open")
    def __init__(self, binding):
        self.binding = binding
        self.directory = account_registry_directory()
        self.locks = []
        self.connection = None
        self.owner = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            for domain in sorted(set(dict(binding.risk_domains).values())):
                venue = next(venue for venue, value in binding.risk_domains if value == domain)
                with span("account_lock.acquire", venue=venue):
                    self.locks.append(FileLock(self.directory / (domain + ".lock")))
            with span("account_lock.registry_open"):
                self.connection = sqlite3.connect(self.directory / "claims.sqlite3", timeout=10)
                self.connection.execute("PRAGMA synchronous=FULL")
                self.connection.execute("CREATE TABLE IF NOT EXISTS claims ("
                    "domain TEXT PRIMARY KEY, owner TEXT NOT NULL, path_digest TEXT NOT NULL, "
                    "fingerprint TEXT NOT NULL)")
        except BaseException:
            self.close()
            raise

    @timed("account_lock.claim")
    def claim(self, store):
        """Commit before creating any native client or startup order intent."""
        if store.account_binding != self.binding.document() or not store.owner_id:
            raise ValueError("risk claim requires an account-bound ledger")
        owner = (store.owner_id, digest(os.path.normcase(str(store.path))), store.fingerprint)
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            for _, domain in self.binding.risk_domains:
                row = self.connection.execute(
                    "SELECT owner, path_digest, fingerprint FROM claims WHERE domain=?", (domain,)).fetchone()
                if row is not None and row != owner:
                    raise ValueError("account risk remains owned by another ledger; "
                                     "resume and reconcile the original state before starting a new session")
            for _, domain in self.binding.risk_domains:
                self.connection.execute("INSERT OR IGNORE INTO claims VALUES (?, ?, ?, ?)", (domain, *owner))
        self.owner = owner

    @timed("account_lock.release_clean")
    def release_clean(self):
        """Caller must have persisted clean proof and disposed every native writer."""
        if self.owner is None:
            raise RuntimeError("no account risk claim to release")
        with self.connection:
            for _, domain in self.binding.risk_domains:
                self.connection.execute("DELETE FROM claims WHERE domain=? AND owner=? "
                    "AND path_digest=? AND fingerprint=?", (domain, *self.owner))
        self.owner = None

    @timed("account_lock.close")
    def close(self):
        try:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
        finally:
            for lock in reversed(self.locks):
                lock.close()
            self.locks.clear()

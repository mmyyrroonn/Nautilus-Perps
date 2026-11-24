"""Transactional strategy positions and events, with one owner per state file."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import uuid

from opportunity_timing import span, timed


class FileLock:
    """Stable advisory OS lock; never unlink its inode on release."""
    def __init__(self, path):
        self._lock = Path(path).open("a+b")
        self._locked = False
        try:
            self._lock.seek(0, os.SEEK_END)
            if self._lock.tell() == 0:
                self._lock.write(b"0")
                self._lock.flush()
            self._lock.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self._lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            self._locked = True
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._lock.closed:
            return
        try:
            if self._locked:
                self._lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(self._lock.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(self._lock.fileno(), fcntl.LOCK_UN)
        finally:
            self._locked = False
            self._lock.close()


class TradeStore:
    serializes_immediately = True

    @timed("store.open")
    def __init__(self, path: Path, fingerprint: str, *, account_binding=None):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = FileLock(self.path.with_suffix(self.path.suffix + ".lock"))
        self.connection = None
        self.owner_id = None
        self.account_binding = account_binding
        try:
            self.connection = sqlite3.connect(self.path)
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA synchronous=FULL")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK(id=1), "
                "fingerprint TEXT NOT NULL, snapshot TEXT NOT NULL)")
            self.connection.execute(
                "CREATE TABLE IF NOT EXISTS events (sequence INTEGER PRIMARY KEY, "
                "observed_ns INTEGER NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL)")
            row = self.connection.execute("SELECT fingerprint, snapshot FROM state WHERE id=1").fetchone()
            self.connection.execute("CREATE TABLE IF NOT EXISTS ownership ("
                "id INTEGER PRIMARY KEY CHECK(id=1), owner TEXT NOT NULL, "
                "binding TEXT NOT NULL, fingerprint TEXT NOT NULL)")
            ownership = self.connection.execute(
                "SELECT owner, binding, fingerprint FROM ownership WHERE id=1").fetchone()
            if ownership:
                if account_binding != json.loads(ownership[1]) or fingerprint != ownership[2]:
                    raise ValueError("state belongs to a different account or configuration; use the original identity")
                self.owner_id = ownership[0]
            elif account_binding is not None:
                if row or self.connection.execute("SELECT 1 FROM events LIMIT 1").fetchone():
                    raise ValueError("legacy state has no proven account binding; reconcile it before migration")
                self.owner_id = uuid.uuid4().hex
                with self.connection:
                    self.connection.execute("INSERT INTO ownership VALUES (1, ?, ?, ?)",
                        (self.owner_id, json.dumps(account_binding, sort_keys=True), fingerprint))
            if row and row[0] != fingerprint:
                raise ValueError("state belongs to a different policy or market configuration; "
                                 "resume with the original configuration and reconcile its owned positions")
            self.fingerprint = fingerprint
        except BaseException:
            self.close()
            raise

    @classmethod
    @timed("store.open")
    def open_bound_owner(cls, path: Path, *, account_binding: dict) -> TradeStore:
        """Open an existing bound ledger and verify ownership without changing its fingerprint."""
        store = cls.__new__(cls)
        store.path = Path(path).resolve()
        store.path.parent.mkdir(parents=True, exist_ok=True)
        store._lock = FileLock(store.path.with_suffix(store.path.suffix + ".lock"))
        store.connection = None
        store.owner_id = None
        store.account_binding = account_binding
        try:
            store.connection = sqlite3.connect(store.path)
            store.connection.execute("PRAGMA journal_mode=WAL")
            store.connection.execute("PRAGMA synchronous=FULL")
            has_ownership = store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ownership'").fetchone()
            ownership = (store.connection.execute(
                "SELECT owner, binding, fingerprint FROM ownership WHERE id=1").fetchone()
                if has_ownership else None)
            if not ownership:
                raise ValueError("no bound account ledger exists at state_path")
            if account_binding != json.loads(ownership[1]):
                raise ValueError("state belongs to a different account; use the original identity")
            has_state = store.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='state'").fetchone()
            row = (store.connection.execute(
                "SELECT fingerprint FROM state WHERE id=1").fetchone()
                if has_state else None)
            if row and row[0] != ownership[2]:
                raise ValueError("ledger ownership and state fingerprints disagree")
            store.owner_id = ownership[0]
            store.fingerprint = ownership[2]
            return store
        except BaseException:
            store.close()
            raise

    @timed("store.save")
    def rebind_fingerprint(self, new_fingerprint: str, observed_ns: int, *, account_recovery: dict) -> None:
        """Atomically rebind an account-owned flat ledger after fresh complete flat proof."""
        if not self.owner_id or self.account_binding is None:
            raise ValueError("fingerprint rebind requires an account-bound ledger")
        if not isinstance(account_recovery, dict) or account_recovery.get("result") != "passed":
            raise ValueError("fingerprint rebind requires fresh complete flat-account proof")
        if new_fingerprint == self.fingerprint:
            return
        payload = json.dumps({
            "event": "ledger_fingerprint_rebound",
            "observed_ns": observed_ns,
            "previous_fingerprint": self.fingerprint,
            "new_fingerprint": new_fingerprint,
            "account_recovery": account_recovery,
        }, sort_keys=True)
        with span("store.sqlite_transaction"):
            with self.connection:
                self.connection.execute(
                    "INSERT INTO events(observed_ns, kind, payload) VALUES (?, ?, ?)",
                    (observed_ns, "ledger_fingerprint_rebound", payload))
                self.connection.execute(
                    "UPDATE ownership SET fingerprint=? WHERE id=1", (new_fingerprint,))
                self.connection.execute(
                    "UPDATE state SET fingerprint=? WHERE id=1", (new_fingerprint,))
        self.fingerprint = new_fingerprint

    @timed("store.load")
    def load(self) -> dict | None:
        row = self.connection.execute("SELECT snapshot FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    @timed("store.save")
    def save(self, snapshot: dict, events: list[dict]) -> None:
        with span("store.serialize"):
            rows = [(event["observed_ns"], event["event"], json.dumps(event, sort_keys=True))
                    for event in events]
            document = json.dumps(snapshot, sort_keys=True)
        with span("store.sqlite_transaction"):
            with self.connection:
                for row in rows:
                    self.connection.execute(
                        "INSERT INTO events(observed_ns, kind, payload) VALUES (?, ?, ?)", row)
                self.connection.execute(
                    "INSERT INTO state VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                    "fingerprint=excluded.fingerprint, snapshot=excluded.snapshot",
                    (self.fingerprint, document))

    @timed("store.close")
    def close(self) -> None:
        try:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
        finally:
            self._lock.close()

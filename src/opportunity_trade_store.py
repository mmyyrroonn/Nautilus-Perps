"""Transactional strategy positions and events, with one owner per state file."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sqlite3
import uuid

from opportunity_timing import timed


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

    @timed("store.load")
    def load(self) -> dict | None:
        row = self.connection.execute("SELECT snapshot FROM state WHERE id=1").fetchone()
        return json.loads(row[0]) if row else None

    @timed("store.save")
    def save(self, snapshot: dict, events: list[dict]) -> None:
        with self.connection:
            for event in events:
                self.connection.execute(
                    "INSERT INTO events(observed_ns, kind, payload) VALUES (?, ?, ?)",
                    (event["observed_ns"], event["event"], json.dumps(event, sort_keys=True)))
            self.connection.execute(
                "INSERT INTO state VALUES (1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, snapshot=excluded.snapshot",
                (self.fingerprint, json.dumps(snapshot, sort_keys=True)))

    @timed("store.close")
    def close(self) -> None:
        try:
            if self.connection is not None:
                self.connection.close()
                self.connection = None
        finally:
            self._lock.close()

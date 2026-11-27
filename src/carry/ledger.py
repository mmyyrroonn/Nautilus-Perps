"""Transactional SQLite state store and startup reconciliation for multi-day Carry positions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping


class LedgerOwnershipError(RuntimeError):
    """Raised when another active process owns the state file or fingerprint diverges."""


class FileLock:
    """Stable advisory OS lock; never unlinks its lock file."""

    def __init__(self, path: Path) -> None:
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

    def close(self) -> None:
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


@dataclass(frozen=True)
class ReconcileResult:
    ok: bool
    halted: bool
    discrepancies: tuple[str, ...]


def _utc_day(ts_s: int) -> str:
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y-%m-%d")


class CarryLedger:
    """SQLite WAL + synchronous=FULL state store with crash recovery and reconciliation."""

    def __init__(self, path: Path, fingerprint: str) -> None:
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock = FileLock(self.path.with_suffix(self.path.suffix + ".lock"))
        except OSError as exc:
            raise LedgerOwnershipError(f"state file locked by another process: {self.path}") from exc
        self.fingerprint = fingerprint
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self._init_schema()

    def _init_schema(self) -> None:
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    fingerprint TEXT NOT NULL,
                    created_ts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS positions (
                    position_id TEXT PRIMARY KEY,
                    symbol TEXT NOT NULL,
                    long_venue TEXT NOT NULL,
                    short_venue TEXT NOT NULL,
                    iid_long TEXT NOT NULL,
                    iid_short TEXT NOT NULL,
                    status TEXT NOT NULL,
                    opened_ts INTEGER NOT NULL,
                    closed_ts INTEGER,
                    entry_l REAL NOT NULL,
                    entry_s REAL NOT NULL,
                    entry_basis_bps REAL NOT NULL,
                    target_base_qty TEXT NOT NULL,
                    long_base_qty TEXT NOT NULL,
                    short_base_qty TEXT NOT NULL,
                    realized_pnl_usd TEXT NOT NULL DEFAULT '0',
                    close_reason TEXT
                );
                CREATE TABLE IF NOT EXISTS orders (
                    client_order_id TEXT PRIMARY KEY,
                    position_id TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    instrument_id TEXT NOT NULL,
                    side TEXT NOT NULL,
                    role TEXT NOT NULL,
                    order_type TEXT NOT NULL,
                    qty_contracts TEXT NOT NULL,
                    base_qty TEXT NOT NULL,
                    price TEXT,
                    status TEXT NOT NULL,
                    filled_base_qty TEXT NOT NULL DEFAULT '0',
                    venue_order_id TEXT,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_stats (
                    utc_day TEXT PRIMARY KEY,
                    realized_pnl_usd TEXT NOT NULL,
                    consecutive_unhedged INTEGER NOT NULL DEFAULT 0
                );
                CREATE TABLE IF NOT EXISTS runtime_flags (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                """
            )
            row = self.conn.execute("SELECT fingerprint FROM meta WHERE id=1").fetchone()
            if row is None:
                self.conn.execute("INSERT INTO meta(id, fingerprint, created_ts) VALUES (1, ?, strftime('%s','now'))", (self.fingerprint,))
            elif row["fingerprint"] != self.fingerprint:
                # Preserve existing positions but halt trading if risk/venue fingerprint changed unexpectedly
                self.conn.execute(
                    "INSERT OR REPLACE INTO runtime_flags(key, value) VALUES ('HALTED_RECONCILE', 'fingerprint_mismatch')"
                )

    def close(self) -> None:
        try:
            self.conn.close()
        finally:
            self._lock.close()

    def is_halted(self) -> tuple[bool, str | None]:
        row = self.conn.execute("SELECT value FROM runtime_flags WHERE key='HALTED_RECONCILE'").fetchone()
        if row is not None:
            return True, str(row["value"])
        row2 = self.conn.execute("SELECT value FROM runtime_flags WHERE key='HALTED_KILL'").fetchone()
        if row2 is not None:
            return True, str(row2["value"])
        return False, None

    def set_halt(self, key: str, reason: str, ts_s: int) -> None:
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO runtime_flags(key, value) VALUES (?, ?)", (key, reason))
            self.conn.execute(
                "INSERT INTO events(ts, kind, payload) VALUES (?, ?, ?)",
                (ts_s, f"halt:{key}", json.dumps({"reason": reason})),
            )

    def clear_halt(self, key: str, ts_s: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM runtime_flags WHERE key=?", (key,))
            self.conn.execute("INSERT INTO events(ts, kind, payload) VALUES (?, ?, '{}')", (ts_s, f"clear_halt:{key}"))

    def record_event(self, kind: str, payload: dict[str, Any], ts_s: int) -> None:
        with self.conn:
            self.conn.execute("INSERT INTO events(ts, kind, payload) VALUES (?, ?, ?)", (ts_s, kind, json.dumps(payload, default=str)))

    def create_position(
        self,
        *,
        position_id: str,
        symbol: str,
        long_venue: str,
        short_venue: str,
        iid_long: str,
        iid_short: str,
        opened_ts: int,
        entry_l: float,
        entry_s: float,
        entry_basis_bps: float,
        target_base_qty: Decimal,
    ) -> None:
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO positions(
                    position_id, symbol, long_venue, short_venue, iid_long, iid_short,
                    status, opened_ts, entry_l, entry_s, entry_basis_bps,
                    target_base_qty, long_base_qty, short_base_qty
                ) VALUES (?, ?, ?, ?, ?, ?, 'OPENING', ?, ?, ?, ?, ?, '0', '0')
                """,
                (
                    position_id, symbol, long_venue, short_venue, iid_long, iid_short,
                    opened_ts, entry_l, entry_s, entry_basis_bps, str(target_base_qty),
                ),
            )
            self.conn.execute(
                "INSERT INTO events(ts, kind, payload) VALUES (?, 'position_created', ?)",
                (opened_ts, json.dumps({"position_id": position_id, "symbol": symbol})),
            )

    def update_position_status(
        self,
        position_id: str,
        *,
        status: str,
        ts_s: int,
        long_base_qty: Decimal | None = None,
        short_base_qty: Decimal | None = None,
        realized_pnl_usd: Decimal | None = None,
        close_reason: str | None = None,
    ) -> None:
        with self.conn:
            row = self.conn.execute("SELECT * FROM positions WHERE position_id=?", (position_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown position_id {position_id}")
            new_l = str(long_base_qty) if long_base_qty is not None else row["long_base_qty"]
            new_s = str(short_base_qty) if short_base_qty is not None else row["short_base_qty"]
            new_pnl = str(realized_pnl_usd) if realized_pnl_usd is not None else row["realized_pnl_usd"]
            closed_ts = ts_s if status == "CLOSED" else row["closed_ts"]
            self.conn.execute(
                """
                UPDATE positions
                SET status=?, long_base_qty=?, short_base_qty=?, realized_pnl_usd=?, closed_ts=?, close_reason=COALESCE(?, close_reason)
                WHERE position_id=?
                """,
                (status, new_l, new_s, new_pnl, closed_ts, close_reason, position_id),
            )
            if status == "CLOSED" and row["status"] != "CLOSED" and realized_pnl_usd is not None:
                day = _utc_day(ts_s)
                cur = self.conn.execute("SELECT realized_pnl_usd, consecutive_unhedged FROM daily_stats WHERE utc_day=?", (day,)).fetchone()
                prev_pnl = Decimal(cur["realized_pnl_usd"]) if cur else Decimal("0")
                prev_unhedged = cur["consecutive_unhedged"] if cur else 0
                self.conn.execute(
                    "INSERT OR REPLACE INTO daily_stats(utc_day, realized_pnl_usd, consecutive_unhedged) VALUES (?, ?, ?)",
                    (day, str(prev_pnl + realized_pnl_usd), prev_unhedged),
                )

    def increment_unhedged_streak(self, ts_s: int, *, reset: bool = False) -> int:
        day = _utc_day(ts_s)
        with self.conn:
            cur = self.conn.execute("SELECT realized_pnl_usd, consecutive_unhedged FROM daily_stats WHERE utc_day=?", (day,)).fetchone()
            pnl = cur["realized_pnl_usd"] if cur else "0"
            new_cnt = 0 if reset else ((cur["consecutive_unhedged"] if cur else 0) + 1)
            self.conn.execute(
                "INSERT OR REPLACE INTO daily_stats(utc_day, realized_pnl_usd, consecutive_unhedged) VALUES (?, ?, ?)",
                (day, pnl, new_cnt),
            )
            return new_cnt

    def daily_stats(self, ts_s: int) -> tuple[Decimal, int]:
        row = self.conn.execute("SELECT realized_pnl_usd, consecutive_unhedged FROM daily_stats WHERE utc_day=?", (_utc_day(ts_s),)).fetchone()
        if row is None:
            return Decimal("0"), 0
        return Decimal(row["realized_pnl_usd"]), int(row["consecutive_unhedged"])

    def register_order_intent(
        self,
        *,
        client_order_id: str,
        position_id: str,
        venue: str,
        instrument_id: str,
        side: str,
        role: str,
        order_type: str,
        qty_contracts: Decimal,
        base_qty: Decimal,
        price: Decimal | None,
        ts_s: int,
    ) -> None:
        """Persist order identity BEFORE submitting to an exchange gateway."""
        with self.conn:
            self.conn.execute(
                """
                INSERT INTO orders(
                    client_order_id, position_id, venue, instrument_id, side, role,
                    order_type, qty_contracts, base_qty, price, status, created_ts, updated_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SUBMITTING', ?, ?)
                """,
                (
                    client_order_id, position_id, venue, instrument_id, side, role,
                    order_type, str(qty_contracts), str(base_qty),
                    str(price) if price is not None else None, ts_s, ts_s,
                ),
            )

    def update_order_state(
        self,
        client_order_id: str,
        *,
        status: str,
        filled_base_qty: Decimal | None = None,
        fill_price: Decimal | None = None,
        venue_order_id: str | None = None,
        ts_s: int = 0,
    ) -> None:
        with self.conn:
            row = self.conn.execute("SELECT filled_base_qty, price, venue_order_id FROM orders WHERE client_order_id=?", (client_order_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown order {client_order_id}")
            new_filled = str(filled_base_qty) if filled_base_qty is not None else row["filled_base_qty"]
            new_price = str(fill_price) if fill_price is not None else row["price"]
            new_vid = venue_order_id if venue_order_id is not None else row["venue_order_id"]
            self.conn.execute(
                "UPDATE orders SET status=?, filled_base_qty=?, price=?, venue_order_id=?, updated_ts=? WHERE client_order_id=?",
                (status, new_filled, new_price, new_vid, ts_s, client_order_id),
            )

    def stop_loss_cooldown_symbols(self, *, now_s: int, cooldown_hours: int) -> set[str]:
        """Return symbols closed by basis_stop, margin_guard, or unhedged within the cooldown window."""
        if cooldown_hours <= 0:
            return set()
        cutoff = now_s - cooldown_hours * 3600
        rows = self.conn.execute(
            """
            SELECT DISTINCT symbol FROM positions
            WHERE status='CLOSED' AND closed_ts IS NOT NULL AND closed_ts >= ?
              AND (close_reason LIKE 'basis_stop%' OR close_reason LIKE 'margin_guard%' OR close_reason LIKE 'unhedged%')
            """,
            (cutoff,),
        ).fetchall()
        return {str(r["symbol"]).upper() for r in rows}

    def compute_position_price_pnl(self, position_id: str) -> Decimal:
        """Compute realized price PnL in USD from persisted closing fills against entry prices."""
        pos = self.conn.execute(
            "SELECT entry_l, entry_s, iid_long, iid_short FROM positions WHERE position_id=?",
            (position_id,),
        ).fetchone()
        if pos is None:
            return Decimal("0")
        entry_l = Decimal(str(pos["entry_l"]))
        entry_s = Decimal(str(pos["entry_s"]))
        iid_long = str(pos["iid_long"])
        iid_short = str(pos["iid_short"])
        rows = self.conn.execute(
            """
            SELECT instrument_id, side, filled_base_qty, price FROM orders
            WHERE position_id=? AND price IS NOT NULL
              AND role IN ('MAKER_CLOSE', 'HEDGE_CLOSE', 'HEDGE_CLOSE_MARKET_FALLBACK', 'EMERGENCY_CLOSE_LONG', 'EMERGENCY_CLOSE_SHORT')
            """,
            (position_id,),
        ).fetchall()
        pnl = Decimal("0")
        for row in rows:
            filled = Decimal(str(row["filled_base_qty"]))
            if filled <= 0:
                continue
            px = Decimal(str(row["price"]))
            if str(row["instrument_id"]) == iid_long and str(row["side"]) == "SELL":
                pnl += filled * (px - entry_l)
            elif str(row["instrument_id"]) == iid_short and str(row["side"]) == "BUY":
                pnl += filled * (entry_s - px)
        return pnl.quantize(Decimal("0.0001"))

    def active_positions(self) -> dict[str, dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM positions WHERE status != 'CLOSED'").fetchall()
        return {str(r["position_id"]): dict(r) for r in rows}

    def open_orders(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE status IN ('SUBMITTING','OPEN','PARTIAL') ORDER BY created_ts ASC, client_order_id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    def open_orders_for_position(self, position_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM orders WHERE position_id=? AND status IN ('SUBMITTING','OPEN','PARTIAL')",
            (position_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def reconcile_startup(
        self,
        *,
        venue_positions: Mapping[tuple[str, str], Decimal],
        venue_open_orders: Mapping[tuple[str, str], int],
        tolerance_pct: Decimal = Decimal("2.0"),
        ts_s: int = 0,
    ) -> ReconcileResult:
        """Verify persisted active positions & orders match exchange ground truth before trading."""
        discrepancies: list[str] = []
        expected_signed: dict[tuple[str, str], Decimal] = {}

        for pid, pos in self.active_positions().items():
            if pos["status"] in ("OPENING", "CLOSING"):
                discrepancies.append(f"in_flight_position:{pid}:{pos['status']}")
            kl = (str(pos["long_venue"]), str(pos["iid_long"]))
            ks = (str(pos["short_venue"]), str(pos["iid_short"]))
            expected_signed[kl] = expected_signed.get(kl, Decimal("0")) + Decimal(str(pos["long_base_qty"]))
            expected_signed[ks] = expected_signed.get(ks, Decimal("0")) - Decimal(str(pos["short_base_qty"]))

        for ord_row in self.open_orders():
            discrepancies.append(
                f"in_flight_order:{ord_row['client_order_id']}:{ord_row['role']}:{ord_row['status']}"
            )

        all_keys = set(expected_signed) | {k for k, v in venue_positions.items() if v != 0}
        for k in sorted(all_keys):
            exp_q = expected_signed.get(k, Decimal("0"))
            act_q = venue_positions.get(k, Decimal("0"))
            denom = max(abs(exp_q), abs(act_q))
            if denom == 0:
                continue
            diff_pct = (abs(exp_q - act_q) / denom) * Decimal("100")
            if diff_pct > tolerance_pct:
                discrepancies.append(f"position_mismatch:{k[0]}:{k[1]}:expected={exp_q},actual={act_q}")

        for (v, iid), cnt in sorted(venue_open_orders.items()):
            if cnt > 0:
                discrepancies.append(f"orphaned_open_orders:{v}:{iid}:count={cnt}")

        if discrepancies:
            self.set_halt("HALTED_RECONCILE", ";".join(discrepancies[:5]), ts_s)
            return ReconcileResult(ok=False, halted=True, discrepancies=tuple(discrepancies))
        return ReconcileResult(ok=True, halted=False, discrepancies=())

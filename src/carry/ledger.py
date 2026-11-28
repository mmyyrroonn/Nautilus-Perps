"""Transactional SQLite state store and startup reconciliation for multi-day Carry positions."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping, Sequence


_MAKER_ORDER_ROLES = frozenset({"MAKER_OPEN", "MAKER_CLOSE"})
_PNL_QUANT = Decimal("0.0001")
RISK_CLOSE_REASON_PREFIXES = (
    "carry_inversion",
    "basis_stop",
    "soft_margin_guard",
    "margin_guard",
    "unhedged",
    "daily_loss_kill",
    "consecutive_unhedged_kill",
    "shutdown_kill_flatten",
    "shutdown_unhedged_flatten",
    "close_only_hedge_race",
)
SLICED_RISK_CLOSE_REASON_PREFIXES = ("carry_inversion", "soft_margin_guard")


def close_reason_priority(reason: str | None) -> int:
    """Return monotonicity rank so active CLOSING reasons escalate and never downgrade."""
    r = (reason or "").strip().lower()
    if not r:
        return 0
    if r.startswith("carry_decay"):
        return 10
    if r.startswith("carry_inversion"):
        return 20
    if r.startswith("basis_stop"):
        return 30
    if r.startswith("soft_margin_guard"):
        return 40
    if r.startswith("margin_guard"):
        return 50
    if r.startswith(
        (
            "unhedged",
            "daily_loss_kill",
            "consecutive_unhedged_kill",
            "shutdown_kill_flatten",
            "shutdown_unhedged_flatten",
            "close_only_hedge_race",
        )
    ):
        return 60
    return 10


def is_risk_close_reason(reason: str | None) -> bool:
    """Return True if close_reason represents a risk-driven exit that survives graceful restart."""
    r = (reason or "").strip().lower()
    return bool(r) and r.startswith(RISK_CLOSE_REASON_PREFIXES)


def is_sliced_risk_close_reason(reason: str | None, *, basis_stop_via_closing: bool = False) -> bool:
    """Return True ONLY for risk close reasons executed via Maker-First/IOC sliced closing."""
    r = (reason or "").strip().lower()
    if not r:
        return False
    if basis_stop_via_closing and r.startswith("basis_stop"):
        return True
    return r.startswith(SLICED_RISK_CLOSE_REASON_PREFIXES)


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

    def __init__(
        self,
        path: Path,
        fingerprint: str,
        *,
        ledger_domain: str | None = None,
        legacy_fingerprint: str | None = None,
    ) -> None:
        if ledger_domain is not None and ledger_domain not in ("paper", "live"):
            raise ValueError(f"invalid ledger_domain: {ledger_domain!r}")
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._lock = FileLock(self.path.with_suffix(self.path.suffix + ".lock"))
        except OSError as exc:
            raise LedgerOwnershipError(f"state file locked by another process: {self.path}") from exc
        self.fingerprint = fingerprint
        self.ledger_domain = ledger_domain
        self.legacy_fingerprint = legacy_fingerprint
        self.conn: sqlite3.Connection | None = None
        try:
            self.conn = sqlite3.connect(self.path)
            self.conn.row_factory = sqlite3.Row
            self._init_schema()
        except BaseException:
            self.close()
            raise

    def _init_schema(self) -> None:
        assert self.conn is not None
        # Validate domain ownership read-only BEFORE any write pragma, DDL, or flag mutation.
        migrate_legacy = False
        legacy_fp_from: str | None = None
        has_meta = (
            self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
            is not None
        )
        if has_meta:
            cols = {str(r["name"]) for r in self.conn.execute("PRAGMA table_info(meta)").fetchall()}
            select_sql = (
                "SELECT fingerprint, ledger_domain FROM meta WHERE id=1"
                if "ledger_domain" in cols
                else "SELECT fingerprint FROM meta WHERE id=1"
            )
            existing = self.conn.execute(select_sql).fetchone()
            if existing is not None:
                existing_fp = str(existing["fingerprint"])
                existing_domain = existing["ledger_domain"] if "ledger_domain" in cols else None
                if (
                    existing_domain is not None
                    and self.ledger_domain is not None
                    and existing_domain != self.ledger_domain
                ):
                    raise LedgerOwnershipError(
                        f"ledger domain mismatch: {self.path} belongs to '{existing_domain}', refused '{self.ledger_domain}'"
                    )
                if existing_domain is None and existing_fp != self.fingerprint:
                    if (
                        self.legacy_fingerprint is not None
                        and existing_fp == self.legacy_fingerprint
                        and self.ledger_domain is not None
                    ):
                        has_orders = (
                            self.conn.execute(
                                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='orders'"
                            ).fetchone()
                            is not None
                        )
                        if has_orders:
                            order_rows = self.conn.execute(
                                "SELECT venue_order_id FROM orders WHERE venue_order_id IS NOT NULL AND venue_order_id != ''"
                            ).fetchall()
                            has_paper_order = any(str(r["venue_order_id"]).startswith("PAPER-") for r in order_rows)
                            has_live_order = any(not str(r["venue_order_id"]).startswith("PAPER-") for r in order_rows)
                            if self.ledger_domain == "paper" and has_live_order:
                                raise LedgerOwnershipError(
                                    f"ledger domain mismatch: {self.path} contains non-paper venue orders, refused 'paper'"
                                )
                            if self.ledger_domain == "live" and has_paper_order:
                                raise LedgerOwnershipError(
                                    f"ledger domain mismatch: {self.path} contains paper orders, refused 'live'"
                                )
                        migrate_legacy = True
                        legacy_fp_from = existing_fp
                    elif self.ledger_domain == "paper":
                        raise LedgerOwnershipError(
                            f"ledger domain mismatch: {self.path} belongs to an existing non-paper fingerprint, refused 'paper'"
                        )

        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        with self.conn:
            self.conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS meta (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    fingerprint TEXT NOT NULL,
                    created_ts INTEGER NOT NULL,
                    ledger_domain TEXT
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
                    price_pnl_usd TEXT NOT NULL DEFAULT '0',
                    funding_pnl_usd TEXT NOT NULL DEFAULT '0',
                    fee_usd TEXT NOT NULL DEFAULT '0',
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
                    fee_usd TEXT,
                    created_ts INTEGER NOT NULL,
                    updated_ts INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS funding_settlements (
                    position_id TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    venue TEXT NOT NULL,
                    side TEXT NOT NULL,
                    settle_ts INTEGER NOT NULL,
                    rate_bps REAL NOT NULL,
                    base_qty TEXT NOT NULL,
                    mark_price REAL NOT NULL,
                    funding_usd TEXT NOT NULL,
                    source TEXT NOT NULL,
                    recorded_ts INTEGER NOT NULL,
                    PRIMARY KEY (position_id, venue, settle_ts)
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
            pos_cols = {str(r["name"]) for r in self.conn.execute("PRAGMA table_info(positions)").fetchall()}
            for col_name, col_def in (
                ("price_pnl_usd", "TEXT NOT NULL DEFAULT '0'"),
                ("funding_pnl_usd", "TEXT NOT NULL DEFAULT '0'"),
                ("fee_usd", "TEXT NOT NULL DEFAULT '0'"),
            ):
                if col_name not in pos_cols:
                    self.conn.execute(f"ALTER TABLE positions ADD COLUMN {col_name} {col_def}")
            ord_cols = {str(r["name"]) for r in self.conn.execute("PRAGMA table_info(orders)").fetchall()}
            if "fee_usd" not in ord_cols:
                self.conn.execute("ALTER TABLE orders ADD COLUMN fee_usd TEXT")
            cols_after = {str(r["name"]) for r in self.conn.execute("PRAGMA table_info(meta)").fetchall()}
            if "ledger_domain" not in cols_after:
                self.conn.execute("ALTER TABLE meta ADD COLUMN ledger_domain TEXT")
            row = self.conn.execute("SELECT fingerprint, ledger_domain FROM meta WHERE id=1").fetchone()
            if row is None:
                self.conn.execute(
                    "INSERT INTO meta(id, fingerprint, created_ts, ledger_domain) VALUES (1, ?, strftime('%s','now'), ?)",
                    (self.fingerprint, self.ledger_domain),
                )
            else:
                if migrate_legacy and self.ledger_domain is not None:
                    self.conn.execute(
                        "UPDATE meta SET fingerprint=?, ledger_domain=? WHERE id=1",
                        (self.fingerprint, self.ledger_domain),
                    )
                    self.conn.execute(
                        "INSERT INTO events(ts, kind, payload) VALUES (strftime('%s','now'), 'ledger_migrated_domain', ?)",
                        (
                            json.dumps(
                                {
                                    "from_fingerprint": legacy_fp_from,
                                    "to_fingerprint": self.fingerprint,
                                    "ledger_domain": self.ledger_domain,
                                }
                            ),
                        ),
                    )
                else:
                    if row["ledger_domain"] is None and self.ledger_domain is not None and row["fingerprint"] == self.fingerprint:
                        self.conn.execute("UPDATE meta SET ledger_domain=? WHERE id=1", (self.ledger_domain,))
                    if row["fingerprint"] != self.fingerprint:
                        # Preserve existing positions but halt trading if risk/venue fingerprint changed unexpectedly
                        self.conn.execute(
                            "INSERT OR REPLACE INTO runtime_flags(key, value) VALUES ('HALTED_RECONCILE', 'fingerprint_mismatch')"
                        )

    def close(self) -> None:
        try:
            if self.conn is not None:
                self.conn.close()
                self.conn = None
        finally:
            self._lock.close()

    def is_reconcile_halted(self) -> tuple[bool, str | None]:
        row = self.conn.execute("SELECT value FROM runtime_flags WHERE key='HALTED_RECONCILE'").fetchone()
        if row is not None:
            return True, str(row["value"])
        return False, None

    def is_kill_halted(self) -> tuple[bool, str | None]:
        row = self.conn.execute("SELECT value FROM runtime_flags WHERE key='HALTED_KILL'").fetchone()
        if row is not None:
            return True, str(row["value"])
        return False, None

    def is_halted(self) -> tuple[bool, str | None]:
        halted, reason = self.is_reconcile_halted()
        if halted:
            return True, reason
        return self.is_kill_halted()

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
        price_pnl_usd: Decimal | None = None,
        funding_pnl_usd: Decimal | None = None,
        fee_usd: Decimal | None = None,
        realized_pnl_usd: Decimal | None = None,
        close_reason: str | None = None,
    ) -> None:
        with self.conn:
            row = self.conn.execute("SELECT * FROM positions WHERE position_id=?", (position_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown position_id {position_id}")
            new_l = str(long_base_qty) if long_base_qty is not None else row["long_base_qty"]
            new_s = str(short_base_qty) if short_base_qty is not None else row["short_base_qty"]
            new_price_pnl = str(price_pnl_usd) if price_pnl_usd is not None else row["price_pnl_usd"]
            new_funding_pnl = str(funding_pnl_usd) if funding_pnl_usd is not None else row["funding_pnl_usd"]
            new_fee = str(fee_usd) if fee_usd is not None else row["fee_usd"]
            new_pnl = str(realized_pnl_usd) if realized_pnl_usd is not None else row["realized_pnl_usd"]
            closed_ts = ts_s if status == "CLOSED" else row["closed_ts"]
            self.conn.execute(
                """
                UPDATE positions
                SET status=?, long_base_qty=?, short_base_qty=?, price_pnl_usd=?, funding_pnl_usd=?, fee_usd=?,
                    realized_pnl_usd=?, closed_ts=?, close_reason=COALESCE(?, close_reason)
                WHERE position_id=?
                """,
                (
                    status,
                    new_l,
                    new_s,
                    new_price_pnl,
                    new_funding_pnl,
                    new_fee,
                    new_pnl,
                    closed_ts,
                    close_reason,
                    position_id,
                ),
            )
            if status in ("CLOSING", "CLOSED"):
                self._clear_exit_defer_in_tx(position_id, ts_s=ts_s)
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
        fee_usd: Decimal | None = None,
        ts_s: int = 0,
    ) -> None:
        with self.conn:
            row = self.conn.execute(
                "SELECT filled_base_qty, price, venue_order_id, fee_usd FROM orders WHERE client_order_id=?",
                (client_order_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"unknown order {client_order_id}")
            new_filled = str(filled_base_qty) if filled_base_qty is not None else row["filled_base_qty"]
            new_price = str(fill_price) if fill_price is not None else row["price"]
            new_vid = venue_order_id if venue_order_id is not None else row["venue_order_id"]
            new_fee = str(max(Decimal("0"), fee_usd)) if fee_usd is not None else row["fee_usd"]
            self.conn.execute(
                "UPDATE orders SET status=?, filled_base_qty=?, price=?, venue_order_id=?, fee_usd=?, updated_ts=? WHERE client_order_id=?",
                (status, new_filled, new_price, new_vid, new_fee, ts_s, client_order_id),
            )

    def set_order_fee_usd(
        self,
        client_order_id: str,
        fee_usd: Decimal | None,
        *,
        ts_s: int | None = None,
    ) -> None:
        """Persist or clear an order's realized commission in USD (no-op if unknown)."""
        val = str(max(Decimal("0"), fee_usd)) if fee_usd is not None else None
        with self.conn:
            if ts_s is not None:
                self.conn.execute(
                    "UPDATE orders SET fee_usd=?, updated_ts=? WHERE client_order_id=?",
                    (val, ts_s, client_order_id),
                )
            else:
                self.conn.execute(
                    "UPDATE orders SET fee_usd=? WHERE client_order_id=?",
                    (val, client_order_id),
                )

    def _reconcile_closed_position_fee_for_order_in_tx(
        self,
        client_order_id: str,
        *,
        maker_fee_bps_by_venue: Mapping[str, Decimal] | Sequence[tuple[str, Decimal]],
        taker_fee_bps_by_venue: Mapping[str, Decimal] | Sequence[tuple[str, Decimal]],
        ts_s: int | None = None,
    ) -> bool:
        assert self.conn is not None
        ord_row = self.conn.execute(
            "SELECT position_id FROM orders WHERE client_order_id=?",
            (client_order_id,),
        ).fetchone()
        if ord_row is None:
            return False
        pid = str(ord_row["position_id"])
        pos = self.conn.execute(
            "SELECT status, closed_ts, fee_usd, realized_pnl_usd FROM positions WHERE position_id=?",
            (pid,),
        ).fetchone()
        if pos is None or str(pos["status"]) != "CLOSED" or pos["closed_ts"] is None:
            return False
        closed_ts = int(pos["closed_ts"])
        new_fee = self.compute_position_fee_usd(
            pid,
            maker_fee_bps_by_venue=maker_fee_bps_by_venue,
            taker_fee_bps_by_venue=taker_fee_bps_by_venue,
        ).quantize(_PNL_QUANT)
        old_fee = Decimal(str(pos["fee_usd"])).quantize(_PNL_QUANT)
        delta = (new_fee - old_fee).quantize(_PNL_QUANT)
        if delta == Decimal("0"):
            return False
        old_net = Decimal(str(pos["realized_pnl_usd"])).quantize(_PNL_QUANT)
        new_net = (old_net - delta).quantize(_PNL_QUANT)
        self.conn.execute(
            "UPDATE positions SET fee_usd=?, realized_pnl_usd=? WHERE position_id=?",
            (str(new_fee), str(new_net), pid),
        )
        day = _utc_day(closed_ts)
        cur = self.conn.execute(
            "SELECT realized_pnl_usd, consecutive_unhedged FROM daily_stats WHERE utc_day=?",
            (day,),
        ).fetchone()
        prev_pnl = Decimal(str(cur["realized_pnl_usd"])) if cur else Decimal("0")
        prev_unhedged = int(cur["consecutive_unhedged"]) if cur else 0
        new_day_pnl = (prev_pnl - delta).quantize(_PNL_QUANT)
        self.conn.execute(
            "INSERT OR REPLACE INTO daily_stats(utc_day, realized_pnl_usd, consecutive_unhedged) VALUES (?, ?, ?)",
            (day, str(new_day_pnl), prev_unhedged),
        )
        evt_ts = closed_ts if ts_s is None else int(ts_s)
        self.conn.execute(
            "INSERT INTO events(ts, kind, payload) VALUES (?, 'position_fee_reconciled', ?)",
            (
                evt_ts,
                json.dumps(
                    {
                        "position_id": pid,
                        "client_order_id": client_order_id,
                        "old_fee_usd": str(old_fee),
                        "new_fee_usd": str(new_fee),
                        "delta_usd": str(delta),
                    }
                ),
            ),
        )
        return True

    def reconcile_closed_position_fee_for_order(
        self,
        client_order_id: str,
        *,
        maker_fee_bps_by_venue: Mapping[str, Decimal] | Sequence[tuple[str, Decimal]],
        taker_fee_bps_by_venue: Mapping[str, Decimal] | Sequence[tuple[str, Decimal]],
        ts_s: int | None = None,
    ) -> bool:
        """Idempotently recompute fee, realized PnL, and UTC daily_stats for a CLOSED position."""
        with self.conn:
            return self._reconcile_closed_position_fee_for_order_in_tx(
                client_order_id,
                maker_fee_bps_by_venue=maker_fee_bps_by_venue,
                taker_fee_bps_by_venue=taker_fee_bps_by_venue,
                ts_s=ts_s,
            )

    def set_order_fee_and_reconcile_closed(
        self,
        client_order_id: str,
        fee_usd: Decimal | None,
        *,
        maker_fee_bps_by_venue: Mapping[str, Decimal] | Sequence[tuple[str, Decimal]],
        taker_fee_bps_by_venue: Mapping[str, Decimal] | Sequence[tuple[str, Decimal]],
        ts_s: int | None = None,
    ) -> bool:
        """Atomically persist/clear an order fee and reconcile any CLOSED parent position in one transaction."""
        val = str(max(Decimal("0"), fee_usd)) if fee_usd is not None else None
        with self.conn:
            if ts_s is not None:
                self.conn.execute(
                    "UPDATE orders SET fee_usd=?, updated_ts=? WHERE client_order_id=?",
                    (val, ts_s, client_order_id),
                )
            else:
                self.conn.execute(
                    "UPDATE orders SET fee_usd=? WHERE client_order_id=?",
                    (val, client_order_id),
                )
            return self._reconcile_closed_position_fee_for_order_in_tx(
                client_order_id,
                maker_fee_bps_by_venue=maker_fee_bps_by_venue,
                taker_fee_bps_by_venue=taker_fee_bps_by_venue,
                ts_s=ts_s,
            )

    def stop_loss_cooldown_symbols(
        self,
        *,
        now_s: int,
        cooldown_hours: int,
        margin_guard_cooldown_hours: int | None = None,
    ) -> set[str]:
        """Return symbols closed by risk stops within their stratified cooldown windows."""
        out: set[str] = set()
        mg_hours = cooldown_hours if margin_guard_cooldown_hours is None else int(margin_guard_cooldown_hours)
        if cooldown_hours > 0:
            cutoff_severe = now_s - cooldown_hours * 3600
            rows = self.conn.execute(
                """
                SELECT DISTINCT symbol FROM positions
                WHERE status='CLOSED' AND closed_ts IS NOT NULL AND closed_ts > ?
                  AND (
                    close_reason LIKE 'basis_stop%'
                    OR close_reason LIKE 'carry_inversion%'
                    OR close_reason LIKE 'unhedged%'
                  )
                """,
                (cutoff_severe,),
            ).fetchall()
            out.update(str(r["symbol"]).upper() for r in rows)
        if mg_hours > 0:
            cutoff_mg = now_s - mg_hours * 3600
            rows = self.conn.execute(
                """
                SELECT DISTINCT symbol FROM positions
                WHERE status='CLOSED' AND closed_ts IS NOT NULL AND closed_ts > ?
                  AND (
                    close_reason LIKE 'margin_guard%'
                    OR close_reason LIKE 'soft_margin_guard%'
                  )
                """,
                (cutoff_mg,),
            ).fetchall()
            out.update(str(r["symbol"]).upper() for r in rows)
        return out

    def revert_closing_to_open(self, position_id: str, *, ts_s: int) -> None:
        """Atomically revert non-risk CLOSING to OPEN and clear prior vacuum-defer/exit-defer episode in one commit."""
        with self.conn:
            row = self.conn.execute("SELECT status FROM positions WHERE position_id=?", (position_id,)).fetchone()
            if row is None:
                raise KeyError(f"unknown position_id {position_id}")
            self.conn.execute("UPDATE positions SET status='OPEN' WHERE position_id=?", (position_id,))
            self.conn.execute(
                "INSERT INTO events(ts, kind, payload) VALUES (?, 'closing_reverted_to_open', ?)",
                (int(ts_s), json.dumps({"position_id": position_id})),
            )
            self._clear_exit_defer_in_tx(position_id, ts_s=ts_s)

    def _query_exit_defer_target_ts(self, position_id: str) -> int | None:
        rows = self.conn.execute(
            """
            SELECT kind, payload FROM events
            WHERE kind IN ('exit_defer_pinned', 'exit_defer_cleared')
            ORDER BY seq ASC
            """
        ).fetchall()
        target_ts: int | None = None
        for r in rows:
            try:
                data = json.loads(str(r["payload"]))
            except json.JSONDecodeError:
                continue
            if str(data.get("position_id") or "") != position_id:
                continue
            if r["kind"] == "exit_defer_cleared":
                target_ts = None
            elif r["kind"] == "exit_defer_pinned":
                raw_ts = data.get("target_ts")
                if raw_ts is not None:
                    target_ts = int(raw_ts)
        return target_ts

    def _clear_exit_defer_in_tx(self, position_id: str, *, ts_s: int) -> None:
        if self._query_exit_defer_target_ts(position_id) is not None:
            self.conn.execute(
                "INSERT INTO events(ts, kind, payload) VALUES (?, 'exit_defer_cleared', ?)",
                (int(ts_s), json.dumps({"position_id": position_id})),
            )

    def get_exit_defer_target_ts(self, position_id: str) -> int | None:
        """Return persisted CEX exit-defer target timestamp for position_id, or None."""
        return self._query_exit_defer_target_ts(position_id)

    def record_exit_defer_target(self, position_id: str, *, target_ts: int, ts_s: int) -> None:
        """Persist pinned CEX exit-defer target timestamp across restarts."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO events(ts, kind, payload) VALUES (?, 'exit_defer_pinned', ?)",
                (int(ts_s), json.dumps({"position_id": position_id, "target_ts": int(target_ts)})),
            )

    def clear_exit_defer_target(self, position_id: str, *, ts_s: int) -> None:
        """Clear persisted CEX exit-defer target timestamp when carry recovers."""
        with self.conn:
            self.conn.execute(
                "INSERT INTO events(ts, kind, payload) VALUES (?, 'exit_defer_cleared', ?)",
                (int(ts_s), json.dumps({"position_id": position_id})),
            )

    def _query_close_vacuum_defer_ts(self, position_id: str) -> int | None:
        rows = self.conn.execute(
            """
            SELECT kind, payload FROM events
            WHERE kind IN ('close_vacuum_defer_started', 'closing_reverted_to_open')
            ORDER BY seq ASC
            """
        ).fetchall()
        started_ts: int | None = None
        for r in rows:
            try:
                data = json.loads(str(r["payload"]))
            except json.JSONDecodeError:
                continue
            if str(data.get("position_id") or "") != position_id:
                continue
            if r["kind"] == "closing_reverted_to_open":
                started_ts = None
            elif r["kind"] == "close_vacuum_defer_started" and started_ts is None:
                raw_ts = data.get("started_ts")
                if raw_ts is not None:
                    started_ts = int(raw_ts)
        return started_ts

    def get_close_vacuum_defer_ts(self, position_id: str) -> int | None:
        """Return persisted close-vacuum defer start timestamp for current CLOSING episode, or None."""
        return self._query_close_vacuum_defer_ts(position_id)

    def record_close_vacuum_defer_start(
        self,
        position_id: str,
        *,
        reason: str,
        spread_bps: Decimal,
        deadline_secs: int,
        ts_s: int,
    ) -> int:
        """Idempotently persist close_vacuum_defer_started once per CLOSING episode and return started_ts."""
        with self.conn:
            existing = self._query_close_vacuum_defer_ts(position_id)
            if existing is not None:
                return existing
            payload = {
                "position_id": position_id,
                "reason": reason,
                "spread_bps": str(spread_bps),
                "started_ts": int(ts_s),
                "deadline_ts": int(ts_s) + int(deadline_secs),
            }
            self.conn.execute(
                "INSERT INTO events(ts, kind, payload) VALUES (?, 'close_vacuum_defer_started', ?)",
                (int(ts_s), json.dumps(payload, default=str)),
            )
            return int(ts_s)

    def compute_position_price_pnl(self, position_id: str) -> Decimal:
        """Compute realized price PnL in USD from persisted closing fills against actual opening fill VWAP."""
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

        open_rows = self.conn.execute(
            """
            SELECT instrument_id, side, filled_base_qty, price FROM orders
            WHERE position_id=? AND price IS NOT NULL
              AND role IN (
                  'MAKER_OPEN', 'HEDGE_OPEN', 'HEDGE_OPEN_MARKET_FALLBACK',
                  'TAKER_OPEN', 'TAKER_OPEN_MARKET_FALLBACK'
              )
            """,
            (position_id,),
        ).fetchall()
        open_l_qty = Decimal("0")
        open_l_notional = Decimal("0")
        open_s_qty = Decimal("0")
        open_s_notional = Decimal("0")
        for row in open_rows:
            filled = Decimal(str(row["filled_base_qty"]))
            if filled <= 0:
                continue
            px = Decimal(str(row["price"]))
            if str(row["instrument_id"]) == iid_long and str(row["side"]) == "BUY":
                open_l_qty += filled
                open_l_notional += filled * px
            elif str(row["instrument_id"]) == iid_short and str(row["side"]) == "SELL":
                open_s_qty += filled
                open_s_notional += filled * px

        cost_l = (open_l_notional / open_l_qty) if open_l_qty > 0 else entry_l
        cost_s = (open_s_notional / open_s_qty) if open_s_qty > 0 else entry_s

        rows = self.conn.execute(
            """
            SELECT instrument_id, side, filled_base_qty, price FROM orders
            WHERE position_id=? AND price IS NOT NULL
              AND role IN (
                  'MAKER_CLOSE', 'HEDGE_CLOSE', 'HEDGE_CLOSE_MARKET_FALLBACK',
                  'TAKER_CLOSE', 'TAKER_CLOSE_MARKET_FALLBACK',
                  'EMERGENCY_CLOSE_LONG', 'EMERGENCY_CLOSE_SHORT'
              )
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
                pnl += filled * (px - cost_l)
            elif str(row["instrument_id"]) == iid_short and str(row["side"]) == "BUY":
                pnl += filled * (cost_s - px)
        return pnl.quantize(_PNL_QUANT)

    def compute_position_fee_usd(
        self,
        position_id: str,
        *,
        maker_fee_bps_by_venue: Mapping[str, Decimal] | Sequence[tuple[str, Decimal]],
        taker_fee_bps_by_venue: Mapping[str, Decimal] | Sequence[tuple[str, Decimal]],
    ) -> Decimal:
        """Compute non-negative total fee in USD from recorded order fees or Maker/Taker fee schedules."""
        maker_map = {str(k): Decimal(str(v)) for k, v in dict(maker_fee_bps_by_venue).items()}
        taker_map = {str(k): Decimal(str(v)) for k, v in dict(taker_fee_bps_by_venue).items()}
        rows = self.conn.execute(
            "SELECT venue, role, filled_base_qty, price, fee_usd FROM orders WHERE position_id=?",
            (position_id,),
        ).fetchall()
        total = Decimal("0")
        for row in rows:
            filled = Decimal(str(row["filled_base_qty"]))
            if filled <= 0:
                continue
            raw_fee = row["fee_usd"]
            if raw_fee is not None and str(raw_fee).strip() != "":
                total += max(Decimal("0"), Decimal(str(raw_fee)))
            elif row["price"] is not None:
                px = Decimal(str(row["price"]))
                venue = str(row["venue"])
                role = str(row["role"])
                fee_bps = maker_map.get(venue, Decimal("0")) if role in _MAKER_ORDER_ROLES else taker_map.get(venue, Decimal("0"))
                total += (filled * px * fee_bps) / Decimal("10000")
        return total.quantize(_PNL_QUANT)

    def compute_position_funding_pnl(self, position_id: str) -> Decimal:
        """Sum persisted realized funding settlements in USD for a position."""
        rows = self.conn.execute(
            "SELECT funding_usd FROM funding_settlements WHERE position_id=?",
            (position_id,),
        ).fetchall()
        total = sum((Decimal(str(r["funding_usd"])) for r in rows), Decimal("0"))
        return total.quantize(_PNL_QUANT)

    def get_position_funding_pnl(self, position_id: str) -> Decimal:
        """Read current persisted funding_pnl_usd on a position row."""
        row = self.conn.execute(
            "SELECT funding_pnl_usd FROM positions WHERE position_id=?",
            (position_id,),
        ).fetchone()
        if row is None or row["funding_pnl_usd"] is None:
            return Decimal("0").quantize(_PNL_QUANT)
        return Decimal(str(row["funding_pnl_usd"])).quantize(_PNL_QUANT)

    def record_funding_settlement(
        self,
        *,
        position_id: str,
        symbol: str,
        venue: str,
        side: str,
        settle_ts: int,
        rate_bps: float,
        base_qty: Decimal,
        mark_price: float,
        source: str,
        recorded_ts: int,
    ) -> bool:
        """Idempotently record one funding settlement (rate_bps > 0 means longs pay shorts)."""
        side_norm = side.upper()
        if side_norm not in ("LONG", "SHORT"):
            raise ValueError(f"invalid funding settlement side: {side!r}")
        if base_qty <= 0 or mark_price <= 0:
            return False
        raw_usd = (Decimal(str(rate_bps)) / Decimal("10000")) * base_qty * Decimal(str(mark_price))
        funding_usd = (-raw_usd if side_norm == "LONG" else raw_usd).quantize(_PNL_QUANT)
        with self.conn:
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO funding_settlements(
                    position_id, symbol, venue, side, settle_ts, rate_bps,
                    base_qty, mark_price, funding_usd, source, recorded_ts
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    position_id,
                    symbol.upper(),
                    venue.upper(),
                    side_norm,
                    int(settle_ts),
                    float(rate_bps),
                    str(base_qty),
                    float(mark_price),
                    str(funding_usd),
                    source,
                    int(recorded_ts),
                ),
            )
            if cur.rowcount <= 0:
                return False
            total_funding = self.compute_position_funding_pnl(position_id)
            self.conn.execute(
                "UPDATE positions SET funding_pnl_usd=? WHERE position_id=?",
                (str(total_funding), position_id),
            )
            return True

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
        max_qty_mismatch_pct: Decimal | None = None,
        max_unhedged_usd: Decimal | None = None,
        basis_stop_via_closing: bool = False,
        ts_s: int = 0,
    ) -> ReconcileResult:
        """Verify persisted active positions & orders match exchange ground truth before trading."""
        discrepancies: list[str] = []
        rec_halted, rec_reason = self.is_reconcile_halted()
        if rec_halted:
            discrepancies.append(f"existing_reconcile_halt:{rec_reason}")

        expected_signed: dict[tuple[str, str], Decimal] = {}
        leg_tol = tolerance_pct if max_qty_mismatch_pct is None else max_qty_mismatch_pct
        kill_halted = self.is_kill_halted()[0]

        for pid, pos in self.active_positions().items():
            long_q = Decimal(str(pos["long_base_qty"]))
            short_q = Decimal(str(pos["short_base_qty"]))
            if pos["status"] == "OPENING":
                discrepancies.append(f"in_flight_position:{pid}:{pos['status']}")
            elif pos["status"] == "CLOSING":
                hi_q = max(long_q, short_q)
                diff_q = abs(long_q - short_q)
                ref_px = max(Decimal(str(pos.get("entry_l") or 0)), Decimal(str(pos.get("entry_s") or 0)))
                unhedged_ok = max_unhedged_usd is None or (diff_q * ref_px) <= max_unhedged_usd
                balanced_risk_closing = (
                    not kill_halted
                    and is_sliced_risk_close_reason(
                        pos.get("close_reason"), basis_stop_via_closing=basis_stop_via_closing
                    )
                    and long_q > Decimal("0")
                    and short_q > Decimal("0")
                    and ((diff_q / hi_q) * Decimal("100")) <= leg_tol
                    and unhedged_ok
                )
                if not balanced_risk_closing:
                    discrepancies.append(f"in_flight_position:{pid}:{pos['status']}")
            kl = (str(pos["long_venue"]), str(pos["iid_long"]))
            ks = (str(pos["short_venue"]), str(pos["iid_short"]))
            expected_signed[kl] = expected_signed.get(kl, Decimal("0")) + long_q
            expected_signed[ks] = expected_signed.get(ks, Decimal("0")) - short_q

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
            if not (rec_halted and len(discrepancies) == 1 and rec_reason is not None):
                self.set_halt("HALTED_RECONCILE", ";".join(discrepancies[:5]), ts_s)
            return ReconcileResult(ok=False, halted=True, discrepancies=tuple(discrepancies))
        return ReconcileResult(ok=True, halted=False, discrepancies=())

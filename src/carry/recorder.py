"""Lightweight gzip-rotated telemetry recorder for Carry slow snapshots and 1s BBO ticks."""
from __future__ import annotations

from datetime import datetime, timezone
import gzip
import json
import logging
from pathlib import Path
from typing import IO, Any, Mapping, Sequence

from .funding_feed import LegMarketInfo
from .signal import EntryCandidate

_LOG = logging.getLogger(__name__)


def _utc_day(ts_s: float) -> str:
    return datetime.fromtimestamp(ts_s, tz=timezone.utc).strftime("%Y%m%d")


class CarryRecorder:
    """Append daily-rotated gzip JSONL telemetry streams without blocking or crashing trading."""

    def __init__(
        self,
        record_dir: Path,
        *,
        bbo_interval_secs: int = 1,
        logger: logging.Logger | None = None,
    ) -> None:
        self.record_dir = Path(record_dir).resolve()
        self.record_dir.mkdir(parents=True, exist_ok=True)
        self.bbo_interval_secs = max(1, int(bbo_interval_secs))
        self._log = logger or _LOG
        self._handles: dict[str, tuple[str, IO[str]]] = {}
        self._last_bbo_ts: dict[str, float] = {}
        self._bbo_since_flush = 0
        self._last_bbo_flush = 0.0
        self._closed = False

    def _ensure_stream(self, kind: str, utc_day: str) -> IO[str]:
        existing = self._handles.get(kind)
        if existing is not None:
            cur_day, handle = existing
            if cur_day == utc_day and not handle.closed:
                return handle
            self._close_handle(handle)
            self._handles.pop(kind, None)
        path = self.record_dir / f"{kind}_{utc_day}.jsonl.gz"
        handle = gzip.open(path, mode="at", encoding="utf-8", compresslevel=5)
        self._handles[kind] = (utc_day, handle)
        return handle

    @staticmethod
    def _close_handle(handle: IO[str]) -> None:
        if getattr(handle, "closed", True):
            return
        try:
            handle.flush()
        finally:
            handle.close()

    def record_slow_snapshot(
        self,
        *,
        ts_s: int,
        snapshot: Mapping[str, Mapping[str, LegMarketInfo]],
        candidates: Sequence[EntryCandidate],
        active_count: int,
        subscribed_count: int,
    ) -> bool:
        """Write one compact slow-layer multi-venue snapshot + candidate ranking record."""
        if self._closed:
            return False
        legs: list[dict[str, Any]] = []
        for sym, vmap in sorted(snapshot.items()):
            if len(vmap) < 2:
                continue
            for venue, leg in sorted(vmap.items()):
                legs.append(
                    {
                        "s": sym,
                        "v": venue,
                        "ac": leg.asset_class,
                        "iid": leg.instrument_id,
                        "ih": leg.interval_h,
                        "ns": leg.next_settle_ts,
                        "r8": leg.r8_bps_h,
                        "r24": leg.r24_bps_h,
                        "cr": leg.current_rate_bps_h,
                        "mp": leg.mark_price,
                        "b": leg.bid,
                        "a": leg.ask,
                        "v24": leg.vol24_usd,
                        "v1": leg.vol1h_usd,
                    }
                )
        cands = [
            {
                "s": c.symbol,
                "lv": c.long_venue,
                "sv": c.short_venue,
                "cat": c.route_cat,
                "c8": c.carry_8h_bps_h,
                "c24": c.carry_24h_bps_h,
                "bps": c.basis_bps,
                "cost": c.rt_cost_bps,
                "score": c.net_score_bps_h,
            }
            for c in candidates
        ]
        line = json.dumps(
            {
                "ts": int(ts_s),
                "active": int(active_count),
                "sub": int(subscribed_count),
                "legs": legs,
                "candidates": cands,
            },
            separators=(",", ":"),
        )
        try:
            stream = self._ensure_stream("slow_snapshots", _utc_day(ts_s))
            stream.write(line + "\n")
            stream.flush()
            return True
        except OSError as exc:
            self._log.warning("[carry.recorder] failed writing slow_snapshots: %s", exc)
            return False

    def record_bbo(
        self,
        *,
        instrument_id: str,
        bid: float,
        ask: float,
        mid: float,
        now_s: float,
        event_ts_ns: int | None = None,
    ) -> bool:
        """Record 1s-throttled L1 BBO snapshot for dynamically subscribed instruments."""
        if self._closed:
            return False
        if now_s - self._last_bbo_ts.get(instrument_id, -1e18) < self.bbo_interval_secs:
            return False
        self._last_bbo_ts[instrument_id] = now_s
        payload: dict[str, Any] = {
            "ts": round(now_s, 3),
            "iid": instrument_id,
            "b": bid,
            "a": ask,
            "m": mid,
        }
        if event_ts_ns is not None:
            payload["ev_ns"] = int(event_ts_ns)
        line = json.dumps(payload, separators=(",", ":"))
        try:
            stream = self._ensure_stream("fast_bbo", _utc_day(now_s))
            stream.write(line + "\n")
            self._bbo_since_flush += 1
            if self._bbo_since_flush >= 50 or (now_s - self._last_bbo_flush) >= 2.0:
                stream.flush()
                self._bbo_since_flush = 0
                self._last_bbo_flush = now_s
            return True
        except OSError as exc:
            self._log.warning("[carry.recorder] failed writing fast_bbo: %s", exc)
            return False

    def flush(self) -> None:
        for _, handle in list(self._handles.values()):
            if not getattr(handle, "closed", True):
                try:
                    handle.flush()
                except OSError:
                    pass
        self._bbo_since_flush = 0

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for kind, (_, handle) in list(self._handles.items()):
            try:
                self._close_handle(handle)
            except OSError:
                pass
            self._handles.pop(kind, None)

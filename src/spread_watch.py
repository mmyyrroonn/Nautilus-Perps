#!/usr/bin/env python3
"""
Stage 1 - read-only cross-venue spread + depth watch (Hyperliquid / Lighter / Aster).

One process streams top-of-book for N symbols from up to N venues each, computes
the taker-taker spread for every venue pair in both directions on every update,
and appends every net-positive moment to a CSV. In parallel it samples each leg's
order book once per second and records how much notional sits within 2 / 5 / 10
bps of the touch, and records every public trade tick of every leg. Data clients
only: no execution client, no keys, no signing, no orders.

    --symbols NVDA,TSLA --venues HL,LIGHTER,ASTER     multi-symbol, N venues
    --venues HL,LIGHTER,LIGHTER_RH,ASTER              adds Lighter's Robinhood Chain
    --venues HL,ENTROPY,ASTER                         adds Entropy's io: equity perps:
                                                      separate logical legs, one shared
                                                      HYPERLIQUID data client
    --venues ONDO,ASTER                               adds Ondo Perps' NVDA/TSLA equity
                                                      perps as their own ONDO client. The
                                                      leg's taker fee comes from the
                                                      instrument metadata (never the 2.5
                                                      bps documentation assumption), and
                                                      a local feed disconnect invalidates
                                                      its BBO and book at once.
    --dry-run                                         print the resolved plan + clients
                                                      as JSON and exit (no network)
    --record-l2                                       record every observed leg's complete
                                                      normalized L2 as JSONL fragments plus a
                                                      manifest under <out>/l2 (off by
                                                      default; a run with ONDO also points
                                                      its raw_md_path at <out>/raw_ondo and
                                                      passes this run's stamp as that
                                                      recorder's run id)
    --pair NVDA:HL-ASTER                              legacy single-pair alias
    --symbol NVDA                                     alias for NVDA:HL-LIGHTER
    --reference FUTU                                  adds the real US stock quote
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from nautilus_trader.common import Environment, LogColor, LogLevel, LoggerConfig, TimeEvent
from nautilus_trader.config import StrategyConfig
from nautilus_trader.live import LiveNode
from nautilus_trader.model import (
    ActorId,
    BookType,
    ClientId,
    FundingRateUpdate,
    InstrumentId,
    InstrumentStatus,
    MarketStatusAction,
    OrderBook,
    OrderBookDeltas,
    OrderBookDepth10,
    QuoteTick,
    StrategyId,
    TraderId,
    TradeTick,
)
from nautilus_trader.trading import Strategy

_SRC_DIR = Path(__file__).resolve().parent
if str(_SRC_DIR) not in sys.path:
    sys.path.insert(0, str(_SRC_DIR))

from ref_feed import (  # noqa: E402  (needs sys.path above)
    RefActor,
    RefActorConfig,
    RefState,
    RefUpdate,
    buy_edge_bps,
    ref_data_type,
    sell_edge_bps,
)
from market_tape import (  # noqa: E402  (needs sys.path above)
    BookTape,
    Delta,
    TapeError,
    TapeWriter,
    funding_event,
    instrument_event,
    quote_event,
    status_event,
    write_run_manifest,
)

HL_TAKER_FEE_BPS = 0.9  # xyz HIP-3 taker, PROMPT.md section 2
HL_MAIN_TAKER_FEE_BPS = 4.5  # HL main-dex perps, tier-0 taker (no HIP-3 discount)
LIGHTER_TAKER_FEE_BPS = 0.0  # Lighter standard taker
# Lighter's Robinhood Chain deployment: separate exchange, quote asset USDG, every
# fee 0 (taker_fee / maker_fee are "0.0000" on every order book, checked 2026-09-07).
LIGHTER_RH_TAKER_FEE_BPS = 0.0
# Verified on mainnet 2026-09-05 via the signed /fapi/v3/commissionRate endpoint for
# NVDAUSDT, XAUUSDT and XAUUSD1: takerCommissionRate 0.000090 (0.9 bps), maker 0.
# The older "20 bps" figure for stock perps is wrong for this account.
ASTER_TAKER_FEE_BPS = 0.9
# Entropy ("io:") equity perps: Tier-0 taker 0.045% x 2 x deployerFeeScale 1.0
# = 0.009% = 0.9 bps with growth mode enabled, checked 2026-09-14. Its own constant
# on purpose: a future xyz fee change must not silently move the io: legs.
ENTROPY_TAKER_FEE_BPS = 0.9
# Ondo Perps (venue string / ClientId both "ONDO"). The venue charges a taker fee,
# but the live number only exists in the instrument metadata the adapter loads.
# 2.5 bps is the *dated documentation assumption* of plan 5.2 (2026-09-14): it is
# what --dry-run prints and what analysis of an old CSV may assume, and nothing
# else. A live run replaces it from the loaded instrument (see
# ondo_metadata_fee_bps); when that metadata carries no fee the leg stays
# fee-unknown and its cost-qualified judgement is withheld rather than computed
# against this number. Source precedence: account rate > live public metadata >
# this dated assumption.
ONDO_TAKER_FEE_BPS = 2.5
ONDO_FEE_SOURCE = "documented_assumption_2026-09-14"
FEE_SOURCE_REGISTRY = "registry"  # the static fee in INSTRUMENTS (non-ONDO venues)
FEE_SOURCE_METADATA = "instrument_metadata"
FEE_SOURCE_MISSING = "missing"  # no fee published: cost judgement withheld
FEE_SOURCE_STALE = "metadata_stale"  # the metadata that carried it is no longer trusted
# InstrumentStatus reasons the adapter publishes for its own *local* feed and for its
# metadata. They are not venue halts and must never be read as one (plan 4.2).
ADAPTER_DISCONNECTED = "adapter:disconnected"
ADAPTER_SNAPSHOT_READY = "adapter:snapshot_ready"
# The metadata axis (R2, contract A): `metadata_stale` means the metadata the venue
# publishes is no longer acceptable - the fee, tick and quantity step derived from it are
# withdrawn - and `metadata_ready` means acceptable, non-stale metadata is in force again.
# Neither is a feed notice and neither is a trading halt: they move their own axis only.
ADAPTER_METADATA_STALE = "adapter:metadata_stale"
ADAPTER_METADATA_READY = "adapter:metadata_ready"
ADAPTER_REASON_PREFIX = "adapter:"
# The two `source` values of an instrument record on the tape (contract C): the metadata
# the run started from, and one published while it runs (a refresh, or the stale marker
# that withdraws the last one).
INSTRUMENT_METADATA_SOURCE = "instrument_metadata"
INSTRUMENT_UPDATE_SOURCE = "instrument_update"
INVALID_METADATA_STALE = "metadata_stale"
# MarketStatusAction values that mean "not tradable right now".
NON_TRADING_ACTIONS = frozenset({
    MarketStatusAction.HALT,
    MarketStatusAction.SUSPEND,
    MarketStatusAction.CLOSE,
    MarketStatusAction.PRE_CLOSE,
    MarketStatusAction.POST_CLOSE,
    MarketStatusAction.NOT_AVAILABLE_FOR_TRADING,
})
RESERVE_BPS = 5.0  # one-leg failure reserve
MAX_AGE_MS = 2_000  # a leg older than this is not tradable
STATUS_SECS = 30
FALLBACK_SECS = 20  # no quotes by then -> fall back to depth10
DEPTH_SAMPLE_SECS = 1  # one depth-capacity row per leg per second
DEPTH_SILENT_SECS = 60  # empty deltas book by then -> warn once, fall back to depth10
LOCAL_BOOK_AFTER = 20  # deltas batches with no cache book before we maintain our own
LEG_STALE_SECS = 120  # per-leg staleness warning on the status tick
ALL_SILENT_SECS = 600  # every leg of every symbol silent this long -> restart node
ALL_SAMPLE_MS = 1_000
# Per (sell, buy) pair we keep only the most recent gross-bps samples for the
# end-of-run median. The full series was an unbounded list appended on every
# quote (~5 M floats/h across 9 crypto symbols -> ~190 MB/h RSS on vultr-worker).
GROSS_KEEP = 20_000
RESTART_PAUSE_SECS = 15
MAX_RESTARTS = 20
CAPACITY_BPS = (2, 5, 10)  # depth buckets, in bps away from the touch

# Hits csv and _all.csv: one row per pair-direction.
SPREAD_HEADER = [
    "ts_utc", "sell_venue", "buy_venue", "gross_bps", "net_bps",
    "sell_bid", "sell_bid_size", "buy_ask", "buy_ask_size",
    "sell_ask", "buy_bid", "funding_sell", "funding_buy",
    "age_sell_ms", "age_buy_ms",
]
# Trades csv: one row per public trade tick, per leg.
TRADES_HEADER = [
    "ts_utc", "venue", "price", "size", "aggressor_side", "trade_id",
    "ts_event_ns", "ts_init_ns",
]
DEPTH_HEADER = [
    "ts_utc", "venue", "bid", "ask", "mid", "levels_bid", "levels_ask",
    *[f"bid_usd_{bps}bps" for bps in CAPACITY_BPS],
    *[f"ask_usd_{bps}bps" for bps in CAPACITY_BPS],
]
# Reference csv (<stem>_ref.csv): the stock quote next to every perp leg, one row
# per reference update and one per perp quote update. Written only when
# --reference is on and only for symbols that have a reference code.
REF_HEAD = [
    "ts_utc", "event", "ref_ts_src_utc", "ref_last", "ref_bid", "ref_ask",
    "ref_mid", "ref_age_ms",
    # Futu server timestamp minus the exchange timestamp: the exchange -> Futu
    # hop, which sits in front of every reference price we see.
    "ref_src_to_srv_ms",
    # Which rule produced ref_bid / ref_ask: the cross-exchange composite, the
    # single freshest exchange (composite crossed), or the last print only.
    "ref_book_mode",
]
REF_SILENT_SECS = 60  # --reference on but nothing arrived by then -> warn

# ---- the standardized L2 tape (plan 5.1/5.2, --record-l2, default off) ----
# Every observed leg of the run records its own *complete normalized* L2 into these
# fragments; the legacy 2/5/10 bps capacity CSVs cannot rebuild a VWAP, so the tape is
# the only artifact a replay may use for depth. One tape per symbol carries every leg of
# that symbol, so the legs share one arrival_seq stream and their relative receive order
# survives the replay.
L2_DIRNAME = "l2"  # fragments + the run manifest, under the run directory
RAW_MD_DIRNAME = "raw_ondo"  # the Ondo adapter's raw public-frame directory (plan 5.2)
ONDO_VENUE_KEY = "ONDO"  # the registry key / ClientId string of the Ondo leg
# OrderBookDepth10 is fixed at ten levels per side (v2), so a tape record built from the
# fallback is limited coverage and says so instead of pretending to be complete L2.
DEPTH10_LEVELS = 10


def ondo_book_limit() -> int | None:
    """The Ondo data client's configured ``book_limit``, or None without the adapter.

    The tape's ``coverage_limit`` for an ONDO leg must be the most levels the venue's depth
    channel can publish (``OndoDataClientConfig.book_limit``, the fork's ``ONDO_BOOK_LIMIT``,
    default 100; plan 4.2: limit=100 is "at most 100 levels"). It is read from that
    configuration instead of a constant copied into this file, so the record cannot silently
    overstate the depth it holds if that default changes. An adapter that cannot be imported
    means "unknown", which the tape records as ``None`` rather than inventing a number.
    """
    try:
        from nautilus_trader.adapters.ondo import OndoDataClientConfig
    except ImportError:
        return None
    return int(OndoDataClientConfig().book_limit)


# The fork-side raw public-frame recorder's own segment naming and record kinds
# (crates/adapters/ondo/src/recording.rs). They are read here, never produced here.
RAW_MD_SEGMENT_STEM = "raw_md"
RAW_MD_GLOB = f"{RAW_MD_SEGMENT_STEM}*.jsonl"
# Only the marker lines are parsed: a frame line can be a very large venue payload and
# there is no statistic in it that the run's own `run_end` does not already state. The
# filter is the marker's own value string, so it does not care how the writer spaced its
# JSON; a frame that happens to carry the word is parsed too and dropped by its `kind`.
RAW_MD_MARKERS = {
    '"run_start"': "run_start",
    '"run_end"': "run_end",
    '"gap"': "gap",
}
RAW_MD_END_STAT_FIELDS = (
    "clean", "reason", "records", "markers", "bytes", "dropped", "gaps", "rotations",
    "segments", "last_recv_seq", "ended_at_ns",
)


def raw_recorder_status(raw_dir: Path, *, run_id: str | None = None) -> dict:
    """The Ondo adapter's raw public-frame recording, as its own files state it.

    The fork-side recorder writes its statistics *into* the recording (plan 5.2): a
    ``run_start`` header per session and a ``run_end`` record carrying ``clean``,
    ``dropped``, ``gaps``, ``records`` and ``last_recv_seq``. The acceptance reads them
    from the files. It never infers a complete recording from the fact that market data
    was still flowing - that says nothing about what reached the disk.

    ``complete`` is True only when every session in the directory finalized cleanly, no
    segment ends mid-line, at least one public frame was written, and (when the caller
    names one) the recorder's ``run_id`` is the run's own. Anything else is incomplete
    with ``problem`` naming why.
    """
    raw_dir = Path(raw_dir)
    report: dict[str, object] = {
        "dir": str(raw_dir),
        "expected_run_id": None if run_id is None else str(run_id),
        "segments": [],
        "sessions": [],
        "run_ids": [],
        "run_id_matches": None,
        "records": 0,
        "dropped": 0,
        "gaps": 0,
        "complete": False,
        "problem": None,
    }
    segments = sorted(raw_dir.glob(RAW_MD_GLOB)) if raw_dir.is_dir() else []
    if not segments:
        report["problem"] = (
            f"no raw public-frame segment ({RAW_MD_GLOB}) was written under {raw_dir}: "
            f"nothing can be replayed from it and completeness cannot be confirmed"
        )
        return report

    starts: dict[str, dict] = {}
    ends: dict[str, dict] = {}
    problems: list[str] = []
    for segment in segments:
        truncated = False
        try:
            with segment.open("r", encoding="utf-8") as handle:
                for line in handle:
                    truncated = not line.endswith("\n")
                    for marker, kind in RAW_MD_MARKERS.items():
                        if marker not in line:
                            continue
                        try:
                            record = json.loads(line)
                        except ValueError:
                            problems.append(f"{segment.name}: a {kind} marker is not JSON")
                            break
                        if not isinstance(record, dict) or record.get("kind") != kind:
                            continue
                        session_id = str(record.get("session_id") or "")
                        if kind == "run_start":
                            starts[session_id] = record
                        elif kind == "run_end":
                            ends[session_id] = record
                        break
        except OSError as exc:
            problems.append(f"{segment.name}: cannot be read: {exc!r}")
            continue
        report["segments"].append({
            "path": segment.name,
            "bytes": segment.stat().st_size,
            "truncated": truncated,
        })
        if truncated:
            # The last line has no newline: the process died mid-record, so this segment
            # cannot be read to its end and no completeness can be claimed for it.
            problems.append(
                f"{segment.name}: the last line has no newline (a process that died "
                f"mid-write), so the segment's end is not confirmable",
            )

    records = dropped = gaps = 0
    for session_id, end in ends.items():
        records += int(end.get("records") or 0)
        dropped += int(end.get("dropped") or 0)
        gaps += int(end.get("gaps") or 0)
        session_run_id = str(end.get("run_id") or "")
        if session_run_id and session_run_id not in report["run_ids"]:
            report["run_ids"].append(session_run_id)
        if end.get("clean") is not True:
            why = end.get("reason") or f"{end.get('dropped')} frame(s) were dropped"
            problems.append(f"session {session_id or '?'}: run_end is not clean ({why})")
        report["sessions"].append({
            "session_id": session_id, "run_id": session_run_id,
            "finalized": True,
            **{field: end.get(field) for field in RAW_MD_END_STAT_FIELDS},
        })
    for session_id, start in starts.items():
        if session_id in ends:
            continue
        session_run_id = str(start.get("run_id") or "")
        if session_run_id and session_run_id not in report["run_ids"]:
            report["run_ids"].append(session_run_id)
        problems.append(
            f"session {session_id or '?'}: the recording never wrote its run_end, so what "
            f"it wrote cannot be confirmed complete",
        )
        report["sessions"].append({
            "session_id": session_id, "run_id": session_run_id, "finalized": False,
        })

    report["records"] = records
    report["dropped"] = dropped
    report["gaps"] = gaps
    if run_id is not None:
        report["run_id_matches"] = report["run_ids"] == [str(run_id)]
        if not report["run_id_matches"]:
            problems.append(
                f"the recording carries run id(s) {report['run_ids']} but this run is "
                f"{run_id!r}: the raw frames and the tape do not join",
            )
    if records == 0 and not problems:
        problems.append(
            "the recording finalized without a single public frame: nothing was recorded",
        )
    report["problems"] = problems
    report["complete"] = not problems
    report["problem"] = None if not problems else "; ".join(problems)
    return report


def raw_recorder_text(report: dict) -> str:
    """One line of the raw recorder's own facts, for the run's log and its report."""
    if not report["segments"]:
        return f"INCOMPLETE ({report['problem']})"
    verdict = "complete" if report["complete"] else "INCOMPLETE"
    text = (
        f"{verdict}: segments={len(report['segments'])} sessions={len(report['sessions'])} "
        f"records={report['records']} dropped={report['dropped']} gaps={report['gaps']} "
        f"run_ids={report['run_ids']}"
    )
    return text if report["complete"] else f"{text} - {report['problem']}"


def ref_header(legs: Sequence[LegSpec]) -> list[str]:
    """Reference csv header: the shared reference block, then one block per leg."""
    out = list(REF_HEAD)
    for leg in legs:
        key = leg.venue_key
        out += [f"{key}_bid", f"{key}_ask", f"{key}_age_ms",
                f"{key}_buy_edge_bps", f"{key}_sell_edge_bps"]
    return out


# ---------------------------------------------------------------- venue registry


def _adapter_missing(venue_key: str, module: str) -> str:
    return (
        f"[stage1] {venue_key} adapter not installed: cannot import '{module}'. "
        f"Install a nautilus_trader build that ships the {venue_key} adapter into "
        f"this repo's .venv, or pick venues that do not include {venue_key}."
    )


def _hyperliquid_client(instrument_ids: Sequence[str]) -> tuple[object, object]:
    """Hyperliquid mainnet data client (public market data, no keys)."""
    try:
        from nautilus_trader.adapters.hyperliquid import (
            HyperliquidDataClientConfig,
            HyperliquidDataClientFactory,
            HyperliquidEnvironment,
        )
    except ImportError as exc:
        raise SystemExit(_adapter_missing("HL", "nautilus_trader.adapters.hyperliquid")) from exc
    return (
        HyperliquidDataClientFactory(),
        HyperliquidDataClientConfig(environment=HyperliquidEnvironment.MAINNET),
    )


def _lighter_client(instrument_ids: Sequence[str]) -> tuple[object, object]:
    """Lighter mainnet data client (public market data, no keys)."""
    try:
        from nautilus_trader.adapters.lighter import (
            LighterDataClientConfig,
            LighterDataClientFactory,
            LighterEnvironment,
        )
    except ImportError as exc:
        raise SystemExit(_adapter_missing("LIGHTER", "nautilus_trader.adapters.lighter")) from exc
    return (
        LighterDataClientFactory(),
        LighterDataClientConfig(environment=LighterEnvironment.MAINNET),
    )


def _lighter_rh_client(instrument_ids: Sequence[str]) -> tuple[object, object]:
    """Lighter on Robinhood Chain: same adapter, ROBINHOOD deployment (public data)."""
    try:
        from nautilus_trader.adapters.lighter import (
            LIGHTER_ROBINHOOD_VENUE,
            LighterDataClientConfig,
            LighterDataClientFactory,
            LighterDeployment,
            LighterEnvironment,
        )
    except ImportError as exc:
        raise SystemExit(
            _adapter_missing("LIGHTER_RH", "nautilus_trader.adapters.lighter"),
        ) from exc
    return (
        LighterDataClientFactory(),
        # base_url_http / base_url_ws stay unset: the adapter resolves the ROBINHOOD
        # MAINNET endpoints itself (https://api.rh.lighter.xyz, wss://api.rh.lighter.xyz/stream).
        LighterDataClientConfig(
            environment=LighterEnvironment.MAINNET,
            deployment=LighterDeployment.ROBINHOOD,
            venue=LIGHTER_ROBINHOOD_VENUE,
        ),
    )


def _aster_client(instrument_ids: Sequence[str]) -> tuple[object, object]:
    """Aster mainnet data client. Binance-derived adapter, lives in a local fork."""
    try:
        from nautilus_trader.adapters.aster import (
            AsterDataClientConfig,
            AsterDataClientFactory,
            AsterEnvironment,
        )
        from nautilus_trader.adapters.binance import BinanceInstrumentProviderConfig
    except ImportError as exc:
        raise SystemExit(_adapter_missing("ASTER", "nautilus_trader.adapters.aster")) from exc
    return (
        AsterDataClientFactory(),
        AsterDataClientConfig(
            environment=AsterEnvironment.MAINNET,
            # Aster rate-limits exchangeInfo hard: never load_all, only what we watch.
            instrument_provider=BinanceInstrumentProviderConfig(load_ids=list(instrument_ids)),
        ),
    )


def _ondo_client(
    instrument_ids: Sequence[str],
    *,
    raw_md_path: str | None = None,
    raw_md_run_id: str | None = None,
) -> tuple[object, object]:
    """Ondo Perps production data client (public market data, no keys).

    Built from the adapter's PyO3 surface (plan 4.1). ``load_ids`` takes
    ``InstrumentId`` objects, not the id strings the plan/CLI carry, so the two
    lists are not interchangeable (Stage 3a report, A.7): the conversion is here
    and only after the import succeeded.

    ``raw_md_path`` is plan 5.2's recording wiring and nothing else: with
    ``--record-l2`` the runner points it at the run's ``raw_ondo`` directory and the
    adapter's own recorder (fork side) writes the public frames there. This file never
    writes into that directory and never guesses its layout.

    ``raw_md_run_id`` is the run id that recorder must stamp on its raw frames: the
    same process stamp every tape record of this run carries, so the two halves of a
    run join *by construction* even when ``--out`` is not named after the run (its
    default ``reports/stage1`` is the case that matters). It is passed only when the
    caller has one - the adapter's own directory-derived fallback is never overridden
    with a ``None``.
    """
    try:
        from nautilus_trader.adapters.ondo import (
            OndoDataClientConfig,
            OndoDataClientFactory,
            OndoEnvironment,
        )
    except ImportError as exc:
        raise SystemExit(_adapter_missing("ONDO", "nautilus_trader.adapters.ondo")) from exc
    recording: dict[str, object] = {"raw_md_path": raw_md_path}
    if raw_md_run_id is not None:
        recording["raw_md_run_id"] = raw_md_run_id
    return (
        OndoDataClientFactory(),
        OndoDataClientConfig(
            environment=OndoEnvironment.PRODUCTION,
            load_ids=[InstrumentId.from_str(name) for name in instrument_ids],
            **recording,
        ),
    )


def ondo_metadata_fee_bps(instrument: object) -> float | None:
    """Ondo taker fee in bps from the loaded instrument's runtime metadata.

    This is the middle rung of plan 5.2's source precedence (a real account rate >
    the live public metadata published with the instrument > the dated
    documentation assumption) and it returns a value only when the provider
    actually published one:

    * ``taker_fee`` is Nautilus's per-fill rate (``Decimal("0.00025")`` is 2.5 bps)
      and is converted with Decimal arithmetic - never via ``f64``;
    * a rate that is absent, unparsable, non-finite, negative or exactly zero
      counts as NOT published. The Ondo venue charges a taker fee, so a zero here
      means the provider defaulted instead of reporting, not that the leg is free.

    ``None`` therefore means "unknown", and the caller must not substitute
    ``ONDO_TAKER_FEE_BPS`` for it.
    """
    value = getattr(instrument, "taker_fee", None)
    if value is None:
        return None
    try:
        rate = Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None
    if not rate.is_finite() or rate <= 0:
        return None
    return float(rate * Decimal(10_000))


@dataclass(frozen=True)
class VenueSpec:
    """One venue: how to name it, and how to build its data client.

    ``key`` is the logical leg tag used on the CLI and in the CSV labels; ``venue``
    is the real Nautilus venue, and it is also the ClientId the legs subscribe with.
    The two are NOT one to one: HL and ENTROPY are different markets on one
    platform, so they share a single HYPERLIQUID data client (see build_client_groups).

    The three behaviour flags are the only places a venue may opt out of the
    default leg semantics. Every one of them defaults to the old behaviour, so
    adding a venue cannot change an existing leg:

    ``supports_depth10``    the venue can serve a depth10 fallback subscription.
    ``tracks_feed_status``  subscribe InstrumentStatus and let the adapter's local
                            feed notices invalidate the leg's BBO and book
                            (plan 4.2). Off => the leg is ready exactly when it has
                            a two-sided BBO, and status events are ignored.
    ``reads_runtime_fee``   take the leg's taker fee from the loaded instrument
                            metadata instead of the static registry number, and
                            leave it unknown (withholding the cost judgement) when
                            the provider published none (plan 5.2).
    """

    key: str  # short tag used on the CLI: HL / LIGHTER / LIGHTER_RH / ASTER / ENTROPY / ONDO
    venue: str  # Nautilus venue string, also the ClientId
    build_client: Callable[[Sequence[str]], tuple[object, object]]
    supports_depth10: bool = True  # Aster's Binance-derived path has no depth10 sub
    tracks_feed_status: bool = False  # only ONDO has the adapter:disconnected chain
    reads_runtime_fee: bool = False  # only ONDO's fee is metadata-only today


VENUES: dict[str, VenueSpec] = {
    "HL": VenueSpec("HL", "HYPERLIQUID", _hyperliquid_client),
    "LIGHTER": VenueSpec("LIGHTER", "LIGHTER", _lighter_client),
    "LIGHTER_RH": VenueSpec("LIGHTER_RH", "LIGHTER_ROBINHOOD", _lighter_rh_client),
    "ASTER": VenueSpec("ASTER", "ASTER", _aster_client, supports_depth10=False),
}
# Entropy is a HIP-3 builder dex ON Hyperliquid: same client, same venue string,
# separate logical leg. Opt-in only - never in DEFAULT_VENUES / DEFAULT_PAIR.
VENUES["ENTROPY"] = VenueSpec("ENTROPY", "HYPERLIQUID", _hyperliquid_client)
# Ondo Perps: its own venue string and ClientId ("ONDO" - never the ONDO asset
# ticker). No depth10 sub in P1 (plan 4.2 says the registry must say so rather
# than pretend), and it is the one venue whose fee and feed validity arrive as
# runtime metadata/status rather than as a static constant.
VENUES["ONDO"] = VenueSpec(
    "ONDO", "ONDO", _ondo_client,
    supports_depth10=False, tracks_feed_status=True, reads_runtime_fee=True,
)
ALL_VENUES = ("HL", "LIGHTER", "LIGHTER_RH", "ASTER", "ENTROPY", "ONDO")
# What `--venues` defaults to. LIGHTER_RH is opt-in: it is a separate exchange with
# its own books, so it only joins a run when it is named explicitly.
DEFAULT_VENUES = ("HL", "LIGHTER", "ASTER")
DEFAULT_PAIR = ("HL", "LIGHTER")  # what bare --symbol means


# ---------------------------------------------------------------- symbol table


# Perp order books listed by the Lighter Robinhood Chain instance, read on
# 2026-09-07 from https://api.rh.lighter.xyz/api/v1/orderBooks (57 perps, all
# "active"; the 27 "<X>/USDG" spot books in the same response are ignored).
# Nautilus names them "<BASE>-PERP.LIGHTER_ROBINHOOD" (confirmed against the
# instrument provider on the same day).
LIGHTER_RH_PERPS = frozenset({
    "AAPL", "AI", "AMC", "AMD", "AMZN", "ANSEM", "ANTHROPIC", "ASTS", "BABA", "BE",
    "BTC", "CASHCAT", "CLSK", "COIN", "CRCL", "CRWV", "ETH", "GOOGL", "HYPE", "INTC",
    "IREN", "LIT", "LUNR", "META", "MSFT", "MU", "NEAR", "NVDA", "OPENAI", "ORCL",
    "PLTR", "PONS", "QBTS", "QQQ", "RGTI", "SGOV", "SHEIN", "SKHY", "SLV", "SMCI",
    "SNDK", "SOFI", "SOL", "SOXL", "SPCX", "SPY", "SUI", "TSLA", "TSM", "USAR",
    "USO", "VVV", "WULF", "XAG", "XAU", "XRP", "ZEC",
})
# Watched symbols the Robinhood Chain does NOT list on 2026-09-07, so they simply
# run without the LIGHTER_RH leg: HOOD, ASTER, DASH, PUMP, ARB.


def lighter_rh(base: str) -> dict[str, tuple[str, float]]:
    """LIGHTER_RH leg for `base`, or nothing when the Robinhood Chain has no such perp."""
    if base not in LIGHTER_RH_PERPS:
        return {}
    return {"LIGHTER_RH": (f"{base}-PERP.LIGHTER_ROBINHOOD", LIGHTER_RH_TAKER_FEE_BPS)}


def crypto(base: str) -> dict[str, tuple[str, float]]:
    """Main-dex crypto perp: the three venues name it by rule, no per-name table."""
    return {
        "HL": (f"{base}-USD-PERP.HYPERLIQUID", HL_MAIN_TAKER_FEE_BPS),
        "LIGHTER": (f"{base}-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": (f"{base}USDT-PERP.ASTER", ASTER_TAKER_FEE_BPS),
        **lighter_rh(base),
    }


# Three-venue crypto perps chosen 2026-09-07 from the venue instrument lists: all three
# list them, ranked by the weakest venue's 24h volume (SOL kept as a liquidity control).
CRYPTO_SYMBOLS = ["BTC", "ETH", "SOL", "HYPE", "ZEC", "PONS", "LIT", "ASTER", "DASH", "PUMP", "ARB",
                  # 2026-09-08 Lighter maker-spread screen candidates (reports/lighter-screen-2026-09-08.md)
                  "XPL", "MON", "EIGEN", "TIA",
                  # 2026-09-09 quick screen (spreads move day to day; record before trading)
                  "VVV", "ETHFI", "AERO", "ZRO", "USELESS"]


def stock(base: str, aster_symbol: str) -> dict[str, tuple[str, float]]:
    """HL xyz HIP-3 stock/commodity perp + Lighter + Aster (Aster symbol given explicitly)."""
    return {
        "HL": (f"xyz:{base}-USD-PERP.HYPERLIQUID", HL_TAKER_FEE_BPS),
        "LIGHTER": (f"{base}-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": (f"{aster_symbol}-PERP.ASTER", ASTER_TAKER_FEE_BPS),
        **lighter_rh(base),
    }


# symbol -> venue key -> (instrument id, taker fee bps).
# Funding: HL, Lighter and Entropy settle hourly, Aster per instrument (1/4/8 h).
# The CSV stores the raw rate as reported by each venue; no per-hour normalisation
# is done here, that is src/analysis/opportunities.py's job.
# Aster stock perps: only the USD1-margined listings carry volume (SNDKUSD1 39.8M vs
# SNDKUSDT 3.0M on 2026-09-07); NVDA / TSLA / HOOD exist only as USDT and are thin there.
INSTRUMENTS: dict[str, dict[str, tuple[str, float]]] = {
    "NVDA": stock("NVDA", "NVDAUSDT"),
    "TSLA": stock("TSLA", "TSLAUSDT"),
    "HOOD": stock("HOOD", "HOODUSDT"),
    "SNDK": stock("SNDK", "SNDKUSD1"),
    "MU": stock("MU", "MUUSD1"),
    "SPCX": stock("SPCX", "SPCXUSD1"),
    "GOLD": {  # Aster lists gold as XAUUSDT
        "HL": ("xyz:GOLD-USD-PERP.HYPERLIQUID", HL_TAKER_FEE_BPS),
        "LIGHTER": ("XAU-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": ("XAUUSDT-PERP.ASTER", ASTER_TAKER_FEE_BPS),
        **lighter_rh("XAU"),
    },
    "GOLD1": {  # Aster's USD1-margined gold perp (XAUUSD1); same HL / Lighter legs as GOLD
        "HL": ("xyz:GOLD-USD-PERP.HYPERLIQUID", HL_TAKER_FEE_BPS),
        "LIGHTER": ("XAU-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
        "ASTER": ("XAUUSD1-PERP.ASTER", ASTER_TAKER_FEE_BPS),
        **lighter_rh("XAU"),
    },
}
for _base in CRYPTO_SYMBOLS:  # main-dex crypto perps: always live, smoke-test the plumbing
    INSTRUMENTS[_base] = crypto(_base)
# ANSEM (screen rank 1, 2026-09-08) has no Hyperliquid listing: Lighter, Lighter RH and
# Aster only, so the HL leg is simply absent rather than failing the symbol.
# FF (screen rank 2, 2026-09-09) has no Hyperliquid listing either.
INSTRUMENTS["FF"] = {
    "LIGHTER": ("FF-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
    "ASTER": ("FFUSDT-PERP.ASTER", ASTER_TAKER_FEE_BPS),
    **lighter_rh("FF"),
}
INSTRUMENTS["ANSEM"] = {
    "LIGHTER": ("ANSEM-PERP.LIGHTER", LIGHTER_TAKER_FEE_BPS),
    "ASTER": ("ANSEMUSDT-PERP.ASTER", ASTER_TAKER_FEE_BPS),
    **lighter_rh("ANSEM"),
}
# Entropy ("io:") equity perps, checked on the live Hyperliquid metadata 2026-09-14:
# SNDK size increment 0.0001, GPRO 0.1, both USD-quoted / USDC-settled, multiplier 1.
# Only the pairs that were actually verified are mapped here - never inferred by ticker.
INSTRUMENTS["SNDK"]["ENTROPY"] = (
    "io:SNDK-USD-PERP.HYPERLIQUID", ENTROPY_TAKER_FEE_BPS,
)
INSTRUMENTS["GPRO"] = {  # no verified HL / Lighter GPRO mapping: ENTROPY x Aster only
    "ENTROPY": ("io:GPRO-USD-PERP.HYPERLIQUID", ENTROPY_TAKER_FEE_BPS),
    "ASTER": ("GPROUSD1-PERP.ASTER", ASTER_TAKER_FEE_BPS),
}
# Ondo Perps: exactly the two markets plan 4.1 fixes, from the venue's own raw
# symbol ("NVDA-USD.P") to the planned Nautilus name ("NVDA-USD-PERP.ONDO").
# Never inferred by ticker: an unmapped symbol simply runs without the ONDO leg.
# The fee here is the dated documentation assumption (see ONDO_TAKER_FEE_BPS); a
# live run replaces it from the instrument metadata.
ONDO_INSTRUMENTS: dict[str, tuple[str, float]] = {
    "NVDA": ("NVDA-USD-PERP.ONDO", ONDO_TAKER_FEE_BPS),
    "TSLA": ("TSLA-USD-PERP.ONDO", ONDO_TAKER_FEE_BPS),
}
for _symbol, _mapping in ONDO_INSTRUMENTS.items():
    INSTRUMENTS[_symbol]["ONDO"] = _mapping
# Ondo's raw market names, for src/ondo_preflight.py (the REST CLI): the venue
# calls NVDA "NVDA-USD.P", Nautilus calls it "NVDA-USD-PERP.ONDO".
ONDO_RAW_MARKETS: dict[str, str] = {"NVDA": "NVDA-USD.P", "TSLA": "TSLA-USD.P"}


# ---------------------------------------------------------------- reference codes


# Futu codes are "{market}.{code}" (https://open.futunn.com/zh-cn/api/quote/push/subscribe).
# Watched symbols that are NOT a US-listed equity have no reference price: the
# crypto perps, and gold (a commodity perp, whose Futu code would be a futures
# contract rather than the thing these perps track).
REF_CODE_OVERRIDES: dict[str, str | None] = {
    "GOLD": None,
    "GOLD1": None,
    "ANSEM": None,  # memecoin listed alongside the equities, not an equity
}


def reference_code(symbol: str) -> str | None:
    """Watched symbol -> Futu code, or None when the symbol has no stock behind it.

    Every equity ticker in INSTRUMENTS maps by rule (NVDA -> US.NVDA), so Lighter
    Robinhood names such as SPY / QQQ / AAPL work as soon as they are added.
    """
    if symbol in REF_CODE_OVERRIDES:
        return REF_CODE_OVERRIDES[symbol]
    if symbol in CRYPTO_SYMBOLS:
        return None
    return f"US.{symbol}"


# ---------------------------------------------------------------- leg / plan


@dataclass(frozen=True)
class LegSpec:
    """One venue leg: what to subscribe to and what it costs to cross.

    ``taker_fee_bps`` is the fee the *registry* knows. For a venue whose spec sets
    ``reads_runtime_fee`` a live run replaces it from the loaded instrument, and
    replaces it with ``None`` when the provider published no fee at all - ``None``
    means "unknown" and the leg's cost-qualified judgement is withheld (plan 5.2).
    """

    venue_key: str  # key into VENUES
    instrument_id: str
    client_id: str
    taker_fee_bps: float | None

    @property
    def label(self) -> str:
        return self.venue_key


def parse_pair(spec: str) -> tuple[str, list[str]]:
    """`NVDA:HL-LIGHTER` -> ("NVDA", ["HL", "LIGHTER"]). Raises SystemExit on bad input."""
    symbol, sep, venues = spec.upper().partition(":")
    if not sep or not venues:
        raise SystemExit(f"[stage1] bad --pair {spec!r}: expected SYMBOL:VENUE_A-VENUE_B")
    keys = [v for v in venues.split("-") if v]
    if len(keys) < 2:
        raise SystemExit(f"[stage1] bad --pair {spec!r}: expected SYMBOL:VENUE_A-VENUE_B")
    if len(set(keys)) != len(keys):
        raise SystemExit(f"[stage1] --pair {spec!r}: venues must differ")
    return symbol, keys


def build_plan(symbols: Sequence[str], venue_keys: Sequence[str]) -> dict[str, list[LegSpec]]:
    """symbol -> legs, one per requested venue that has a mapping. Fails fast."""
    for key in venue_keys:
        if key not in VENUES:
            raise SystemExit(f"[stage1] unknown venue {key!r}; known: {sorted(VENUES)}")
    plan: dict[str, list[LegSpec]] = {}
    for symbol in symbols:
        if symbol not in INSTRUMENTS:
            raise SystemExit(f"[stage1] unknown symbol {symbol!r}; known: {sorted(INSTRUMENTS)}")
        legs: list[LegSpec] = []
        for key in venue_keys:
            mapping = INSTRUMENTS[symbol].get(key)
            if mapping is None:
                # Not every venue lists every symbol (e.g. HOOD is not on LIGHTER_RH):
                # skip that leg and watch the rest, never fail the whole run. stderr,
                # because --dry-run prints pure JSON on stdout.
                print(
                    f"[stage1] INFO {symbol}: no instrument mapped for {key}; "
                    f"skipping that leg and watching the remaining legs",
                    file=sys.stderr, flush=True,
                )
                continue
            legs.append(LegSpec(key, mapping[0], VENUES[key].venue, mapping[1]))
        if len(legs) < 2:
            mapped = sorted(INSTRUMENTS[symbol])
            raise SystemExit(
                f"[stage1] {symbol}: needs at least 2 mapped venues out of "
                f"{list(venue_keys)}; mapped for this symbol: {mapped}",
            )
        plan[symbol] = legs
    return plan


def csv_stem(symbol: str, legs: Sequence[LegSpec], stamp: str) -> str:
    return f"{symbol}_{'-'.join(leg.venue_key for leg in legs)}_{stamp}"


# ---------------------------------------------------------------- csv sink


class CsvSink:
    """Append-only csv; writes the header only when the file is new (restart-safe)."""

    def __init__(self, path: Path, header: Sequence[str]) -> None:
        self.path = path
        self.header = list(header)
        self._handle = None
        self._writer = None
        self.rows = 0

    def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fresh = not self.path.exists() or self.path.stat().st_size == 0
        self._handle = self.path.open("a", newline="", encoding="utf-8")
        self._writer = csv.writer(self._handle)
        if fresh:
            self._writer.writerow(self.header)
            self._handle.flush()

    def write(self, row: Sequence[object]) -> None:
        if self._writer is None:
            return
        self._writer.writerow(row)
        self._handle.flush()
        self.rows += 1

    def close(self) -> None:
        if self._handle is not None:
            self._handle.flush()
            self._handle.close()
            self._handle = None
            self._writer = None


# ---------------------------------------------------------------- leg state


@dataclass
class LegState:
    """Latest top-of-book and latest book snapshot for one leg."""

    spec: LegSpec
    instrument_id: InstrumentId
    client_id: ClientId
    bid: float = 0.0
    ask: float = 0.0
    bid_size: float = 0.0
    ask_size: float = 0.0
    ts_ns: int = 0
    funding: float | None = None
    updates: int = 0
    source: str = "quotes"  # top-of-book source: quotes / depth10
    depth10: OrderBookDepth10 | None = None
    local_book: OrderBook | None = None
    book_mode: str = "-"  # capacity source actually in use: deltas-cache / deltas-local / depth10
    depth_updates: int = 0  # book messages seen (deltas batches or depth10 snapshots)
    depth_warned: bool = False
    depth10_subscribed: bool = False
    trades: int = 0  # public trade ticks seen on this leg
    # --- Ondo local feed / real market state (plan 4.2). A leg that does not opt
    # in to status tracking keeps both True, so its readiness is exactly the old
    # "a two-sided BBO" rule and nothing about an existing venue changes.
    feed_ready: bool = True  # the adapter's local feed has a usable snapshot
    market_ready: bool = True  # the venue says this market is tradable
    # The third axis: whether the runtime metadata this leg's fee/step come from is still
    # trusted. Its own axis on purpose - a feed reconnect is not a metadata refresh, and a
    # metadata refresh is not a venue halt (contract A).
    metadata_ready: bool = True
    metadata_stale_reason: str | None = None
    # The last metadata written to the tape for this leg (the *acceptable* one), with the
    # version and availability the adapter published. None = unknown, never invented.
    metadata_payload: dict | None = None
    metadata_version: str | None = None
    metadata_available_ns: int | None = None
    metadata_updates: int = 0  # instrument records written for this leg on this run's tape
    book_valid: bool = True  # the cached book belongs to the current feed
    market_halted: bool = False  # a real venue status currently says "not trading"
    status_tracked: bool = False  # this leg reacts to InstrumentStatus
    fee_source: str = FEE_SOURCE_REGISTRY
    disconnects: int = 0
    snapshots: int = 0
    # The leg's normalized L2 on this run's tape (plan 5.2): None unless --record-l2 is
    # on. One per leg, so a run never records one venue's book next to another venue's
    # top-of-book and calls both "depth".
    book_tape: object | None = None

    def ready(self) -> bool:
        """Tradable: the feed is up, the market is trading, the metadata is trusted, both sides exist.

        All three axes are True by default, which is the pre-Ondo behaviour; for a
        status-tracked leg a local disconnect (or a metadata invalidation) takes its own
        axis down in the same event-loop turn, without waiting for the old quote to age
        out. No axis can move another: a reconnect does not clear a halt, and neither
        clears stale metadata.
        """
        return (
            self.feed_ready
            and self.market_ready
            and self.metadata_ready
            and self.bid > 0.0
            and self.ask > 0.0
        )

    def unusable_reasons(self) -> list[str]:
        """Why this leg is not usable *right now* (empty when it is).

        Plan 8 judges a run leg by leg ("no quote or no valid two-sided book is not
        a P1 pass"), so the summary states this instead of leaving it to be inferred
        from the counters. It does not change the exit code: a leg that produced a
        BBO earlier in the run still counts as covered (received_leg_keys).
        """
        reasons: list[str] = []
        if not self.feed_ready:
            reasons.append("local-feed-disconnected")
        if not self.market_ready:
            reasons.append("market-not-trading")
        if not self.metadata_ready:
            reasons.append("metadata-stale")
        if not self.book_valid:
            reasons.append("book-invalidated")
        if self.bid <= 0.0 or self.ask <= 0.0:
            reasons.append("no-two-sided-bbo")
        return reasons

    def age_ms(self, now_ns: int) -> float:
        return (now_ns - self.ts_ns) / 1e6


def _fee_text(fee: float | None) -> str:
    return "unknown" if fee is None else f"{fee:.2f}"


# ---------------------------------------------------------------- capacity math


def _capacity(levels: Sequence[tuple[float, float]], best: float, is_bid: bool) -> list[float]:
    """Cumulative notional (price * size) within each CAPACITY_BPS bucket of the touch."""
    out: list[float] = []
    for bps in CAPACITY_BPS:
        if is_bid:
            limit = best * (1.0 - bps / 1e4)
            total = sum(p * s for p, s in levels if p >= limit)
        else:
            limit = best * (1.0 + bps / 1e4)
            total = sum(p * s for p, s in levels if p <= limit)
        out.append(total)
    return out


def _levels_from_depth10(depth: OrderBookDepth10) -> tuple[list, list]:
    bids = [(float(o.price), float(o.size)) for o in depth.bids]
    asks = [(float(o.price), float(o.size)) for o in depth.asks]
    return ([x for x in bids if x[1] > 0.0], [x for x in asks if x[1] > 0.0])


def _levels_from_book(book: OrderBook) -> tuple[list, list]:
    bids = [(float(lvl.price), lvl.size()) for lvl in book.bids()]
    asks = [(float(lvl.price), lvl.size()) for lvl in book.asks()]
    return ([x for x in bids if x[1] > 0.0], [x for x in asks if x[1] > 0.0])


# ---------------------------------------------------------------- tape records


def tape_deltas(deltas: OrderBookDeltas) -> list[Delta]:
    """One deltas message as normalized tape operations, in arrival order.

    Every price and size is the adapter's own exact decimal text (``str`` of a
    Nautilus ``Price``/``Quantity``), never a float. A CLEAR is an operation of the
    batch, not a book state: the caller (:meth:`SpreadWatch._record_book_batch`) applies
    the whole batch and only then records one book, so a CLEAR can never become ordinary
    depth. An unnamed side (``NO_ORDER_SIDE``) is not a level and is dropped rather than
    guessed.
    """
    ops: list[Delta] = []
    for delta in deltas.deltas:
        action = str(getattr(delta.action, "name", delta.action))
        if action.upper() == "CLEAR":
            ops.append(Delta("clear"))
            continue
        order = getattr(delta, "order", None)
        side = str(getattr(getattr(order, "side", None), "name", ""))
        if order is None or side not in ("BUY", "SELL"):
            continue
        ops.append(Delta(
            action.lower(), "bid" if side == "BUY" else "ask",
            str(order.price), str(order.size),
        ))
    return ops


def _exact_decimal_text(value: Any) -> str | None:
    """The exact decimal text of a Nautilus Price/Quantity, or None when there is none.

    ``str`` of a Price/Quantity renders from its integer and precision, so it never goes
    through an f64; anything that is not an exact finite decimal is reported as unknown
    rather than guessed.
    """
    if value is None:
        return None
    try:
        text = str(value).strip()
        number = Decimal(text)
    except (InvalidOperation, ValueError, AttributeError, TypeError):
        return None
    return text if number.is_finite() else None


def instrument_size_increment(instrument: object) -> str | None:
    """The venue's *real* quantity step, as exact text, or None when it is not published.

    Contract E: this is the only admissible source of a quantity step. A precision is not
    an increment (``10**-size_precision`` was measured wrong by a factor of six on the
    venue's own instruments), so a missing increment stays unknown and the caller must
    withhold any common-step arithmetic rather than derive one.
    """
    return _exact_decimal_text(getattr(instrument, "size_increment", None))


def instrument_price_increment(instrument: object) -> str | None:
    """The venue's price step, as exact text, or None when it is not published."""
    return _exact_decimal_text(getattr(instrument, "price_increment", None))


def instrument_metadata_version(instrument: object) -> str | None:
    """The adapter's version stamp for this instrument's metadata, or None.

    The adapter publishes it on the instrument's ``info`` map (the venue-metadata carrier
    Nautilus instruments carry); an adapter that publishes no version leaves it unknown.
    """
    info = getattr(instrument, "info", None)
    if isinstance(info, dict) and info.get("metadata_version") is not None:
        return str(info["metadata_version"])
    value = getattr(instrument, "metadata_version", None)
    return None if value is None else str(value)


def instrument_metadata_available_ns(instrument: object) -> int | None:
    """When this metadata became available locally, in epoch nanoseconds, or None.

    An adapter that stamps the availability explicitly (``info["metadata_available_ns"]``)
    is believed. Otherwise the instrument's own ``ts_init`` is used: that is the local
    receive time the adapter stamps when it builds the instrument from the venue payload,
    which is exactly the moment this metadata became usable here. Nothing is inferred from
    a wall clock the application does not have.
    """
    info = getattr(instrument, "info", None)
    if isinstance(info, dict):
        value = info.get("metadata_available_ns")
        if isinstance(value, bool):
            value = None
        elif isinstance(value, str) and value.strip().lstrip("-").isdigit():
            value = int(value)
        if isinstance(value, int):
            return value
    ts_init = getattr(instrument, "ts_init", None)
    if isinstance(ts_init, bool) or not isinstance(ts_init, int):
        return None
    return ts_init if ts_init > 0 else None


def tape_instrument_metadata(leg: LegState, instrument: object) -> dict:
    """The normalized instrument facts of one arrival, for the tape's instrument record.

    The taker fee travels as the same text the CSV/summary use (:func:`_fee_text`), so the
    tape never carries an f64 and a report can still see which source the fee came from.

    Contract C: `size_increment` and `price_increment` are the venue's real steps as exact
    decimal text (never ``10**-precision``), and `metadata_version`/`metadata_available_ns`
    are the adapter's own stamp of which metadata this is and when it arrived. Every one of
    them is ``None`` when it is not published - a missing quantity step is unknown, and a
    reader that cannot see one must not compute a representable quantity from a precision.
    """
    metadata: dict[str, object] = {
        "venue": leg.spec.client_id,
        "client_id": leg.spec.client_id,
        "taker_fee_bps": _fee_text(leg.spec.taker_fee_bps),
        "fee_source": leg.fee_source,
    }
    for name in ("price_precision", "size_precision"):
        value = getattr(instrument, name, None)
        if isinstance(value, int) and not isinstance(value, bool):
            metadata[name] = value
    tick = getattr(instrument, "tick_size", None)
    metadata["tick_size"] = None if tick is None else str(tick)
    metadata["size_increment"] = instrument_size_increment(instrument)
    metadata["price_increment"] = instrument_price_increment(instrument)
    metadata["metadata_version"] = instrument_metadata_version(instrument)
    metadata["metadata_available_ns"] = instrument_metadata_available_ns(instrument)
    return metadata


# ---------------------------------------------------------------- run state


class RunState:
    """Shared across the strategies of one node: the all-silent watchdog."""

    def __init__(self) -> None:
        self.stop_node: Callable[[], None] | None = None
        self.started_ns = 0
        self.last_seen: dict[str, int] = {}
        self.stop_requested = False
        self.startup_errors: list[str] = []
        # Recording failures (plan 5.1) are NOT startup errors: the spread watch keeps
        # running and reporting, but the recording's own acceptance has failed and the
        # caller must be able to print that.
        self.recording_errors: list[str] = []

    def note_recording_error(self, message: str) -> None:
        if message not in self.recording_errors:
            self.recording_errors.append(message)

    def note_start(self, now_ns: int) -> None:
        if self.started_ns == 0:
            self.started_ns = now_ns

    def fail_startup(self, message: str) -> None:
        """A strategy could not even start: record it and stop the node once."""
        if message not in self.startup_errors:
            self.startup_errors.append(message)
        if self.stop_node is not None and not self.stop_requested:
            self.stop_requested = True
            self.stop_node()

    def report(self, symbol: str, last_ns: int, now_ns: int, log) -> None:
        self.last_seen[symbol] = last_ns
        if self.stop_requested or self.stop_node is None:
            return
        newest = max(self.last_seen.values(), default=0)
        base = newest if newest > 0 else self.started_ns
        if base and (now_ns - base) >= ALL_SILENT_SECS * 1_000_000_000:
            log.error(
                f"[stage1] every leg of every symbol silent for {ALL_SILENT_SECS}s "
                f"-> stopping the node so the restart loop rebuilds it",
            )
            self.stop_requested = True
            self.stop_node()


# ---------------------------------------------------------------- strategy


class SpreadWatchConfig(StrategyConfig):
    """Configuration for the N-leg spread + depth watcher of one symbol."""

    def __init__(
        self,
        *,
        symbol: str,
        legs: list[LegSpec],
        csv_path: Path,
        all_csv_path: Path,
        depth_csv_path: Path,
        trades_csv_path: Path,
        run_state: RunState,
        reserve_bps: float = RESERVE_BPS,
        book_depth_levels: int = 10,
        max_age_ms: int = MAX_AGE_MS,
        all_sample_ms: int = ALL_SAMPLE_MS,
        ref_code: str | None = None,
        ref_csv_path: Path | None = None,
        record_l2: bool = False,
        tape_path: Path | None = None,
        run_id: str = "",
        coverage_limits: dict[str, int | None] | None = None,
        **_kwargs: object,
    ) -> None:
        super().__init__()  # pyo3 base: strategy_id must travel through __new__ kwargs
        self.symbol = symbol
        self.legs = legs
        self.csv_path = csv_path
        self.all_csv_path = all_csv_path
        self.depth_csv_path = depth_csv_path
        self.trades_csv_path = trades_csv_path
        self.ref_code = ref_code
        self.ref_csv_path = ref_csv_path
        self.run_state = run_state
        self.reserve_bps = reserve_bps
        self.book_depth_levels = book_depth_levels
        self.max_age_ms = max_age_ms
        self.all_sample_ms = all_sample_ms
        # --record-l2 (plan 5.2): explicit, default off, and it covers every leg of this
        # strategy. tape_path is None when the switch is off.
        self.record_l2 = record_l2
        self.tape_path = tape_path
        self.run_id = run_id
        # venue_key -> the most levels that venue's depth channel can publish, for the tape's
        # coverage_limit. None means "resolve it at open time" (the ONDO cap is the adapter
        # config's own book_limit, never a constant copied into this file).
        self.coverage_limits = coverage_limits


class SpreadWatch(Strategy):
    """Cross-venue taker-taker spread for every venue pair, plus depth capacity."""

    def __init__(self, config: SpreadWatchConfig) -> None:
        super().__init__(config)
        self._cfg = config
        self.symbol = config.symbol
        self._legs = [
            LegState(spec, InstrumentId.from_str(spec.instrument_id),
                     ClientId.from_str(spec.client_id))
            for spec in config.legs
        ]
        for leg in self._legs:
            leg.status_tracked = VENUES[leg.spec.venue_key].tracks_feed_status
        self._by_id = {leg.instrument_id: leg for leg in self._legs}
        # every ordered pair (sell venue, buy venue)
        self._pairs = [
            (sell, buy)
            for sell in self._legs
            for buy in self._legs
            if sell is not buy
        ]
        # fee+reserve per direction; None = at least one leg's fee is unknown, so
        # net_bps is not a cost-qualified number and the direction is withheld.
        self._fee: dict[tuple[str, str], float | None] = {}
        self._fee_unknown: dict[tuple[str, str], int] = {}
        self._gross: dict[tuple[str, str], deque[float]] = {}  # recent, bounded
        self._evals: dict[tuple[str, str], int] = {}  # total appended, unbounded
        self._gross_min: dict[tuple[str, str], float] = {}
        self._gross_max: dict[tuple[str, str], float] = {}
        self._positive: dict[tuple[str, str], int] = {}
        self._last_gross: dict[tuple[str, str], float] = {}
        self._last_all_ns: dict[tuple[str, str], int] = {}
        for sell, buy in self._pairs:
            key = (sell.spec.venue_key, buy.spec.venue_key)
            self._fee_unknown[key] = 0
            self._gross[key] = deque(maxlen=GROSS_KEEP)
            self._evals[key] = 0
            self._gross_min[key] = float("inf")
            self._gross_max[key] = float("-inf")
            self._positive[key] = 0
            self._last_gross[key] = float("nan")
            self._last_all_ns[key] = 0
        self._recompute_fees()
        self._samples = 0
        self._stale = 0
        self._start_ns = 0
        self._hits = CsvSink(config.csv_path, SPREAD_HEADER)
        self._all = CsvSink(config.all_csv_path, SPREAD_HEADER)
        self._depth = CsvSink(config.depth_csv_path, DEPTH_HEADER)
        self._trades = CsvSink(config.trades_csv_path, TRADES_HEADER)
        # Reference leg (optional): a real stock quote, never a tradable leg. It
        # stays out of _evaluate's pair enumeration and out of the other CSVs.
        self._ref: RefState | None = (
            RefState(config.ref_code) if config.ref_code else None
        )
        self._ref_sink: CsvSink | None = (
            CsvSink(config.ref_csv_path, ref_header(config.legs))
            if config.ref_code and config.ref_csv_path is not None
            else None
        )
        self._ref_warned = False
        self._ref_last_written: float | None = None  # ref_last on the last ref row
        # The run's tape (plan 5.2): one writer per symbol carrying every leg of it.
        self._tape: TapeWriter | None = None
        self._tape_error: str | None = None
        self._tape_facts: tuple[int, int, int, int] | None = None
        # venue_key -> published-depth cap, resolved once (see _tape_coverage_limits).
        self._coverage: dict[str, int | None] | None = None

    # ---------------------------------------------------------------- lifecycle

    def _recompute_fees(self) -> None:
        """Rebuild every direction's fee+reserve threshold from the live leg specs.

        Called once at construction and again after the ONDO legs read their fee
        from the runtime metadata. A direction that touches a fee-unknown leg gets
        ``None`` rather than the dated documentation assumption.
        """
        for sell, buy in self._pairs:
            key = (sell.spec.venue_key, buy.spec.venue_key)
            fees = (sell.spec.taker_fee_bps, buy.spec.taker_fee_bps)
            if fees[0] is None or fees[1] is None:
                self._fee[key] = None
            else:
                self._fee[key] = fees[0] + fees[1] + self._cfg.reserve_bps

    def _read_runtime_fees(self) -> None:
        """Take a runtime-metadata leg's taker fee from the loaded instrument.

        Plan 5.2's precedence is account rate > live public metadata > the dated
        documentation assumption. The account rate needs a private feed this stage
        does not have, so the metadata is the source; when it published no fee the
        leg keeps ``None`` and its cost-qualified judgement is withheld instead of
        silently using ``ONDO_TAKER_FEE_BPS``.
        """
        for leg in self._legs:
            if not VENUES[leg.spec.venue_key].reads_runtime_fee:
                continue
            self._read_runtime_fee(leg, self.cache.instrument(leg.instrument_id))

    def _read_runtime_fee(self, leg: LegState, instrument: object) -> None:
        """Re-read one leg's taker fee from the metadata in force *now*.

        Called at start-up and again whenever the adapter refreshes the instrument: a fee
        that changed mid-run must move this leg's threshold from that arrival on, or the
        run would price the rest of its life against the value it started with (F06/F07).
        """
        if not VENUES[leg.spec.venue_key].reads_runtime_fee:
            return
        fee = ondo_metadata_fee_bps(instrument)
        if fee is None:
            leg.spec = replace(leg.spec, taker_fee_bps=None)
            leg.fee_source = FEE_SOURCE_MISSING
            self.log.warning(
                f"[{self.symbol}/{leg.spec.label}] no taker fee in the runtime "
                f"instrument metadata of {leg.instrument_id}: fee unknown, so the "
                f"dated documentation assumption is NOT used and this leg's "
                f"cost-qualified judgement is withheld until the fee is known",
            )
        else:
            if fee != leg.spec.taker_fee_bps and leg.fee_source == FEE_SOURCE_METADATA:
                self.log.warning(
                    f"[{self.symbol}/{leg.spec.label}] taker fee changed in the runtime "
                    f"instrument metadata: {_fee_text(leg.spec.taker_fee_bps)} -> "
                    f"{fee:.4f} bps ({leg.instrument_id})",
                )
            leg.spec = replace(leg.spec, taker_fee_bps=fee)
            leg.fee_source = FEE_SOURCE_METADATA
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] taker fee from the runtime "
                f"instrument metadata: {fee:.4f} bps ({leg.instrument_id})",
                LogColor.GREEN,
            )
        self._recompute_fees()

    def on_start(self) -> None:
        for leg in self._legs:
            venue = leg.instrument_id.venue
            ids = self.cache.instrument_ids(venue)
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] {venue} instruments loaded: {len(ids)}",
                LogColor.BLUE,
            )
            if self.cache.instrument(leg.instrument_id) is None:
                base = str(leg.instrument_id.symbol).split("-")[0].split(":")[-1]
                near = [str(i) for i in ids if base in str(i)][:20]
                self.log.error(
                    f"[{self.symbol}/{leg.spec.label}] {leg.instrument_id} NOT loaded; "
                    f"candidates: {near}",
                )
                # Not necessarily delisted: the metadata load may have failed too.
                self._cfg.run_state.fail_startup(
                    f"{self.symbol}/{leg.spec.venue_key}: instrument not loaded: "
                    f"{leg.spec.instrument_id}",
                )
                self.stop()
                return
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] instrument OK: {leg.instrument_id}",
                LogColor.GREEN,
            )

        # Runtime metadata, read once the instrument is known to be loaded: the
        # ONDO taker fee and the local-feed status subscription are the only two
        # venue-specific behaviours Stage 3c adds (plan 4.2/5.2).
        self._read_runtime_fees()
        for leg in self._legs:
            if not leg.status_tracked:
                continue
            self.subscribe_instrument_status(leg.instrument_id, client_id=leg.client_id)
            # Runtime instrument updates: the adapter republishes the instrument when its
            # metadata refresh succeeds, and the fee, tick and real quantity step can all
            # change mid-run. Without this subscription `on_instrument` would never fire
            # and the tape would carry the start-up metadata for the whole run (F07/F08).
            self.subscribe_instrument(leg.instrument_id, client_id=leg.client_id)
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] subscribed instrument status "
                f"(local feed disconnect vs real market halt) and instrument updates "
                f"(metadata refresh) {leg.instrument_id}",
                LogColor.GREEN,
            )

        for sink in (self._hits, self._all, self._depth, self._trades):
            sink.open()
        if self._ref_sink is not None:
            self._ref_sink.open()
        # --record-l2 (plan 5.2). After the runtime fees are known, so the tape's
        # instrument records carry the fee the run actually uses.
        self._open_tape()
        if self._ref is not None:
            # Published by RefActor on this node's message bus; no data client and
            # no funding / trades / deltas subscription - it is not a trading leg.
            self.subscribe_data(ref_data_type(self._ref.code))
            self.log.info(
                f"[{self.symbol}/ref] subscribed reference price {self._ref.code}",
                LogColor.GREEN,
            )

        for leg in self._legs:
            self.subscribe_quotes(leg.instrument_id, client_id=leg.client_id)
            self.subscribe_funding_rates(leg.instrument_id, client_id=leg.client_id)
            # Public trade prints: what actually traded, at what size and on which
            # side. Purely additive - the spread path still runs off quotes.
            self.subscribe_trades(leg.instrument_id, client_id=leg.client_id)
            # Managed deltas on EVERY venue: the data engine maintains a full L2
            # OrderBook in the cache, which is the capacity source. depth10 caps at
            # 10 levels - far inside 2 bps on a liquid book - so it is only a
            # fallback, engaged from _sample_depth if the deltas book stays empty.
            # This runs in ADDITION to quotes: quotes stay the top-of-book source
            # for the spread, so the spread path is unchanged.
            self.subscribe_book_deltas(
                leg.instrument_id, BookType.L2_MBP,
                client_id=leg.client_id, managed=True,
            )
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] subscribed quotes+funding+trades"
                f"+deltas(managed) {leg.instrument_id}",
                LogColor.GREEN,
            )

        now = self.clock.utc_now()
        self._start_ns = self.clock.timestamp_ns()
        self._cfg.run_state.note_start(self._start_ns)
        self.clock.set_time_alert(f"fallback-{self.symbol}",
                                  now + timedelta(seconds=FALLBACK_SECS))
        self.clock.set_timer(f"status-{self.symbol}", timedelta(seconds=STATUS_SECS),
                             start_time=now)
        self.clock.set_timer(f"depth-{self.symbol}", timedelta(seconds=DEPTH_SAMPLE_SECS),
                             start_time=now)

    def on_stop(self) -> None:
        for sink in (self._hits, self._all, self._depth, self._trades):
            sink.close()
        if self._ref_sink is not None:
            self._ref_sink.close()
        self._close_tape()

    # ------------------------------------------------------------------- l2 tape

    def _tape_coverage_limits(self) -> dict[str, int | None]:
        """venue_key -> the most levels that venue's depth channel can publish.

        Plan 4.2: an ONDO depth channel publishes at most ``book_limit`` levels and the
        depth10 fallback publishes ten, so a book record carries the cap of the feed it came
        from and never claims more depth than it holds. A venue whose published depth is
        unknown maps to ``None`` (unknown), never to an invented number. Resolved once per
        run; the ONDO cap is the adapter configuration's own ``book_limit``.
        """
        if self._coverage is None:
            if self._cfg.coverage_limits is not None:
                self._coverage = dict(self._cfg.coverage_limits)
            elif any(leg.spec.venue_key == ONDO_VENUE_KEY for leg in self._legs):
                self._coverage = {ONDO_VENUE_KEY: ondo_book_limit()}
            else:
                self._coverage = {}
        return self._coverage

    def _open_tape(self) -> None:
        """Open this run's tape and give every observed leg its own normalized L2.

        Plan 5.2: ``--record-l2`` is explicit and default off, and it applies to *all*
        observed legs of the run - the legacy 2/5/10 bps capacity CSVs cannot rebuild a
        VWAP, so a run must never record one venue's book and another venue's top of
        book and call both depth. One writer carries every leg of this symbol, so the
        legs share one ``arrival_seq`` stream and their relative receive order is what a
        replay sees.
        """
        if not self._cfg.record_l2:
            return
        path = self._cfg.tape_path
        if path is None:
            self.log.error(
                f"[{self.symbol}] --record-l2 is on but no tape path is configured: "
                f"nothing is recorded",
            )
            return
        coverage = self._tape_coverage_limits()
        legs = [
            {
                "venue_key": leg.spec.venue_key,
                "venue": leg.spec.client_id,
                "instrument_id": leg.spec.instrument_id,
                "coverage_limit": coverage.get(leg.spec.venue_key),
            }
            for leg in self._legs
        ]
        try:
            self._tape = TapeWriter(
                path, run_id=self._cfg.run_id or self.symbol, symbol=self.symbol, legs=legs,
            )
            self._tape.open()
            for leg in self._legs:
                leg.book_tape = BookTape(
                    self._tape, symbol=self.symbol, venue=leg.spec.venue_key,
                    instrument_id=leg.spec.instrument_id,
                    coverage_limit=coverage.get(leg.spec.venue_key),
                )
                self._write_instrument_record(leg)
        except (TapeError, OSError) as exc:
            self._tape_failed(f"cannot record the L2 tape {path}: {exc}")
            # The run was asked to record and cannot: that is a startup failure, not a
            # reason to keep collecting data nobody asked for.
            self._cfg.run_state.fail_startup(
                f"{self.symbol}: cannot record the L2 tape {path}: {exc}",
            )
            return
        self.log.info(
            f"[{self.symbol}] recording complete normalized L2 for "
            f"{len(self._legs)} leg(s) -> {path}",
            LogColor.GREEN,
        )

    def _close_tape(self) -> None:
        tape, self._tape = self._tape, None
        for leg in self._legs:
            leg.book_tape = None
        if tape is None:
            return
        try:
            tape.close()
        except (TapeError, OSError) as exc:
            self._tape_failed(f"closing the L2 tape failed: {exc}")
            return
        self._tape_facts = (tape.records, tape.dropped, tape.gaps, len(tape.fragments))
        if not tape.complete:
            # Plan 5.1: a gap means this recording's own acceptance failed, even though
            # the file closed cleanly and the spread deliberately kept running. It is
            # reported as a *recording* failure - never as a clean run - and the tape may
            # not be replayed across the gap.
            self._tape_error = (
                f"the L2 tape closed with {tape.dropped} dropped record(s) in "
                f"{tape.gaps} gap(s): this recording is incomplete and must not be "
                f"replayed across a gap"
            )
            self._cfg.run_state.note_recording_error(f"{self.symbol}: {self._tape_error}")
            self.log.error(
                f"[{self.symbol}] L2 TAPE FAILED: {self._tape_error} -> {tape.path}",
            )
            return
        self.log.info(
            f"[{self.symbol}] L2 tape closed: records={tape.records} "
            f"dropped={tape.dropped} gaps={tape.gaps} "
            f"fragments={len(tape.fragments)} -> {tape.path}",
            LogColor.GREEN,
        )

    def _flush_tape(self) -> None:
        tape = self._tape
        if tape is None:
            return
        try:
            tape.flush()
        except (TapeError, OSError) as exc:
            self._tape_failed(f"the L2 tape stopped writing: {exc}")

    def _tick_tape(self) -> None:
        """Drive the writer's own flush deadline from the event loop timer (F10)."""
        tape = self._tape
        if tape is None:
            return
        try:
            tape.tick()
        except (TapeError, OSError) as exc:
            self._tape_failed(f"the L2 tape stopped writing: {exc}")

    def _tape_failed(self, message: str) -> None:
        """Abort the recorder, keep the spread running and make the failure visible."""
        self._tape_error = message
        self._tape = None
        for leg in self._legs:
            leg.book_tape = None
        self._cfg.run_state.note_recording_error(f"{self.symbol}: {message}")
        self.log.error(
            f"[{self.symbol}] L2 TAPE FAILED: {message} -> this recording is incomplete "
            f"and must not be replayed across the gap (plan 5.1)",
        )

    def _write_tape(self, event: dict) -> None:
        tape = self._tape
        if tape is None or self._tape_error is not None:
            return
        try:
            tape.write_event(event)
        except (TapeError, OSError) as exc:
            self._tape_failed(str(exc))

    def _emit_instrument(self, leg: LegState, metadata: dict, *, source: str,
                         valid: bool, invalid_reason: str | None) -> None:
        """Write one instrument record (contract C). Never mutates the leg's axes."""
        if self._tape is not None and self._tape_error is None:
            leg.metadata_updates += 1  # one arrival, counted where a tape can hold it
        self._write_tape(instrument_event(
            symbol=self.symbol, venue=leg.spec.venue_key,
            instrument_id=leg.spec.instrument_id,
            metadata=metadata, source=source, valid=valid, invalid_reason=invalid_reason,
            coverage_limit=self._tape_coverage_limits().get(leg.spec.venue_key),
        ))

    def _write_instrument_record(
        self, leg: LegState, *, source: str = INSTRUMENT_METADATA_SOURCE,
        instrument: object | None = None,
    ) -> None:
        """Record a metadata arrival and remember it as this leg's last known payload.

        The record carries the leg's metadata axis as its own `valid` (contract C): a
        payload that arrived while the venue's metadata was stale is on the tape, with the
        marker that says it is not to be used, and it does not become the remembered
        payload either - a later stale marker must republish the last metadata that *was*
        acceptable, not the one that replaced it while nobody trusted it.
        """
        if instrument is None:
            instrument = self.cache.instrument(leg.instrument_id)
        metadata = tape_instrument_metadata(leg, instrument)
        usable = bool(leg.metadata_ready)
        if usable:
            leg.metadata_payload = metadata
            leg.metadata_version = metadata["metadata_version"]
            leg.metadata_available_ns = metadata["metadata_available_ns"]
        self._emit_instrument(
            leg, metadata, source=source, valid=usable,
            invalid_reason=None if usable else INVALID_METADATA_STALE,
        )

    def _refresh_instrument_metadata(self, leg: LegState, instrument: object) -> None:
        """A refreshed instrument arrived: re-read the fee, then record the new metadata.

        The fee is re-read only while the metadata axis is up: a refresh that arrives while
        the venue's metadata is stale is recorded (marked unusable) but does not become the
        rate this leg prices against until `metadata_ready` says the metadata is good again.
        """
        if leg.metadata_ready:
            self._read_runtime_fee(leg, instrument)
        self._write_instrument_record(leg, source=INSTRUMENT_UPDATE_SOURCE,
                                      instrument=instrument)

    def _record_book_depth(self, leg: LegState, depth: OrderBookDepth10) -> None:
        """Record a depth10 snapshot as limited coverage, never as complete L2."""
        book_tape = leg.book_tape
        if book_tape is None or self._tape_error is not None:
            return
        bids = [[str(level.price), str(level.size)] for level in depth.bids]
        asks = [[str(level.price), str(level.size)] for level in depth.asks]
        try:
            # `valid` is left to the record's own levels (contract D): feed, market and
            # metadata eligibility are replayed from the status and instrument records,
            # never folded into one snapshot's verdict.
            book_tape.replace(
                bids, asks, source="depth10", coverage_limit=DEPTH10_LEVELS,
                ts_event_ns=depth.ts_event, ts_init_ns=depth.ts_init,
            )
        except (TapeError, OSError) as exc:
            self._tape_failed(str(exc))

    def _record_book_batch(self, leg: LegState, deltas: OrderBookDeltas) -> None:
        """Apply the whole deltas batch, then record exactly one book for it.

        The batch is applied first: a leading CLEAR empties the book and every ADD lands
        before anything is written, so the intermediate state between the CLEAR and its
        ADDs never reaches the tape as ordinary depth (plan 5.1).
        """
        book_tape = leg.book_tape
        if book_tape is None or self._tape_error is not None:
            return
        try:
            # The record's own levels decide `valid` (contract D). In particular the
            # first deltas batch after a reconnect lands while `feed_ready` is still
            # False - the adapter publishes the replacement snapshot *before* its
            # snapshot_ready notice - and it is a perfectly good book: writing it as
            # feed-invalidated is what left a replay holding an unusable book until a
            # second frame arrived (F18).
            book_tape.apply_batch(
                tape_deltas(deltas),
                ts_event_ns=deltas.ts_event, ts_init_ns=deltas.ts_init,
                coverage_limit=self._tape_coverage_limits().get(leg.spec.venue_key),
            )
        except (TapeError, OSError) as exc:
            self._tape_failed(str(exc))

    def _tape_text(self) -> str:
        facts = ""
        if self._tape_facts is not None:
            records, dropped, gaps, fragments = self._tape_facts
            facts = f"records={records} dropped={dropped} gaps={gaps} fragments={fragments}"
        if self._tape_error is not None:
            # A recorder that died mid-run and a tape that closed with a gap are both
            # failed acceptance: say so where the run prints its own verdict.
            return f"FAILED: {self._tape_error}" + (f"  {facts}" if facts else "")
        return facts or "open"

    # ------------------------------------------------------------------ handlers

    def on_quote(self, quote: QuoteTick) -> None:
        leg = self._by_id.get(quote.instrument_id)
        if leg is None or leg.source != "quotes":
            return
        leg.bid, leg.ask = float(quote.bid_price), float(quote.ask_price)
        leg.bid_size, leg.ask_size = float(quote.bid_size), float(quote.ask_size)
        leg.ts_ns = quote.ts_init
        leg.updates += 1
        # The tape keeps the quote's own exact decimal text (never the f64 above): a
        # replay needs the top of book that was visible at each arrival.
        self._write_tape(quote_event(
            symbol=self.symbol, venue=leg.spec.venue_key,
            instrument_id=leg.spec.instrument_id,
            bid=str(quote.bid_price), ask=str(quote.ask_price),
            bid_size=str(quote.bid_size), ask_size=str(quote.ask_size),
            source=leg.source, ts_event_ns=quote.ts_event, ts_init_ns=quote.ts_init,
        ))
        self._evaluate(leg)
        self._write_ref_row(f"quote:{leg.spec.venue_key}")

    def on_data(self, data) -> None:  # noqa: ANN001 - CustomData or a bare payload
        """Reference-price updates republished by RefActor."""
        update = getattr(data, "data", data)
        if not isinstance(update, RefUpdate) or self._ref is None:
            return
        if update.code != self._ref.code:
            return
        self._ref.apply(update)
        self._write_ref_row(f"ref:{update.kind}")

    def on_book_depth(self, depth: OrderBookDepth10) -> None:
        leg = self._by_id.get(depth.instrument_id)
        if leg is None:
            return
        leg.depth10 = depth
        leg.depth_updates += 1
        self._record_book_depth(leg, depth)
        if leg.source != "depth10" or not depth.bids or not depth.asks:
            return
        leg.bid, leg.bid_size = float(depth.bids[0].price), float(depth.bids[0].size)
        leg.ask, leg.ask_size = float(depth.asks[0].price), float(depth.asks[0].size)
        leg.ts_ns = depth.ts_init
        leg.updates += 1
        self._evaluate(leg)

    def on_book_deltas(self, deltas: OrderBookDeltas) -> None:
        leg = self._by_id.get(deltas.instrument_id)
        if leg is None:
            return
        leg.depth_updates += 1
        if leg.local_book is None:
            # managed=True normally: the engine keeps the book for us. Only when the
            # cache still has none after LOCAL_BOOK_AFTER batches do we maintain one.
            managed = self.cache.order_book(deltas.instrument_id) is not None
            if not managed and leg.depth_updates >= LOCAL_BOOK_AFTER:
                leg.local_book = OrderBook(deltas.instrument_id, BookType.L2_MBP)
                self.log.warning(
                    f"[{self.symbol}/{leg.spec.label}] cache holds no order book after "
                    f"{leg.depth_updates} managed deltas -> maintaining a local OrderBook",
                )
        if leg.local_book is not None:
            leg.local_book.apply_deltas(deltas)
        # One tape record per fully applied batch, whatever the cache does: the batch is
        # applied inside the recorder before anything is written.
        self._record_book_batch(leg, deltas)

    def on_trade(self, trade: TradeTick) -> None:
        leg = self._by_id.get(trade.instrument_id)
        if leg is None:
            return
        leg.trades += 1
        # aggressor_side as the adapter reports it; this build names the variants
        # BUY / SELL / NO_AGGRESSOR.
        self._trades.write([
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            leg.spec.venue_key,
            str(trade.price),
            str(trade.size),
            trade.aggressor_side.name,
            str(trade.trade_id),
            trade.ts_event,
            trade.ts_init,
        ])

    def on_funding_rate(self, funding_rate: FundingRateUpdate) -> None:
        # Raw venue rate, stored as delivered, never normalised here: HL/Entropy are
        # hourly fractions, Lighter an hourly PERCENT (the adapter does not /100),
        # Aster a fraction per that instrument's own 1/4/8 h interval. The units are
        # registered in analysis/opportunities.py, which is what converts them.
        leg = self._by_id.get(funding_rate.instrument_id)
        if leg is None:
            return
        leg.funding = float(funding_rate.rate)
        # The tape keeps the rate's own decimal text, not the float above: 0.0000063 is
        # 0.063 bp/h and must survive exactly (plan 4.3).
        self._write_tape(funding_event(
            symbol=self.symbol, venue=leg.spec.venue_key,
            instrument_id=leg.spec.instrument_id,
            rate=str(funding_rate.rate),
            interval_secs=getattr(funding_rate, "interval", None),
            ts_event_ns=funding_rate.ts_event, ts_init_ns=funding_rate.ts_init,
        ))

    # ----------------------------------------------------- instrument status

    def on_instrument(self, instrument: object) -> None:
        """A runtime instrument (re)publication: re-read its metadata and record it.

        The adapter republishes the instrument when its metadata refresh succeeds, and a
        refresh can change the fee, the tick, the real quantity step, the metadata version
        and when it became available. All of it belongs on the tape at the arrival it
        happened at: recording the start-up values once would price a whole prefix against
        metadata that did not exist yet (F06/F07/F08).
        """
        instrument_id = getattr(instrument, "instrument_id", None)
        leg = self._by_id.get(instrument_id) if instrument_id is not None else None
        if leg is None:
            return
        self._refresh_instrument_metadata(leg, instrument)

    def on_instrument_status(self, status: InstrumentStatus) -> None:
        """Three unrelated things travel on InstrumentStatus (plan 4.2, contract A).

        ``adapter:disconnected`` / ``adapter:snapshot_ready`` are the adapter's notices
        about its *local* feed, ``adapter:metadata_stale`` / ``adapter:metadata_ready``
        about its metadata: none of them is a venue halt and none may pretend to be. Every
        other reason is a real market status. Only a leg whose registry spec sets
        ``tracks_feed_status`` (ONDO) is affected; every other leg keeps the plain "a
        two-sided BBO means ready" rule.
        """
        leg = self._by_id.get(status.instrument_id)
        if leg is None or not leg.status_tracked:
            return
        reason = status.reason or ""
        # Every status the leg subscribes to is recorded, the local feed and metadata
        # notices included: a replay must see why a quote gap, a book invalidation or a
        # withdrawn fee happened. The record's own `valid` is not the feed's state - the
        # notice is a fact and replays into the axes it belongs to (contract D).
        self._write_tape(status_event(
            symbol=self.symbol, venue=leg.spec.venue_key,
            instrument_id=leg.spec.instrument_id,
            action=str(getattr(status.action, "name", status.action)), reason=reason,
            is_trading=status.is_trading, is_quoting=status.is_quoting,
            ts_event_ns=status.ts_event, ts_init_ns=status.ts_init,
        ))
        if reason == ADAPTER_DISCONNECTED:
            self._on_feed_disconnected(leg, status)
        elif reason == ADAPTER_SNAPSHOT_READY:
            self._on_feed_snapshot_ready(leg, status)
        elif reason == ADAPTER_METADATA_STALE:
            self._on_metadata_stale(leg, status)
        elif reason == ADAPTER_METADATA_READY:
            self._on_metadata_ready(leg, status)
        elif reason.startswith(ADAPTER_REASON_PREFIX):
            # Any other local notice (a bare "socket connected", a subscribe ack) is NOT
            # recovery: only a new snapshot re-arms the feed, and only a metadata notice
            # moves the metadata axis.
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] local notice {reason!r}: readiness "
                f"untouched (feed_ready={leg.feed_ready}, "
                f"metadata_ready={leg.metadata_ready})",
            )
        else:
            self._on_market_status(leg, status)

    def _on_feed_disconnected(self, leg: LegState, status: InstrumentStatus) -> None:
        """The local feed died: the old BBO and book are unusable *now*.

        This runs in the same event-loop turn as the notice: the cached quote is
        cleared and the book marked invalid immediately, so nothing has to wait
        for the old quote to age out past MAX_AGE_MS. A fresh quote alone cannot
        restore readiness either - that needs a new snapshot.
        """
        leg.disconnects += 1
        leg.feed_ready = False
        # The market state we last knew came over the connection that just died,
        # so it is unknown until a new snapshot re-establishes the channel. A halt
        # the venue had *explicitly* announced (`market_halted`) is not forgotten:
        # it survives the reconnect.
        leg.market_ready = False
        leg.book_valid = False
        leg.bid = leg.ask = leg.bid_size = leg.ask_size = 0.0
        leg.ts_ns = 0
        leg.depth10 = None
        leg.source = "quotes"
        self.log.error(
            f"[{self.symbol}/{leg.spec.label}] LOCAL FEED DISCONNECTED "
            f"({status.reason!r}, is_quoting={status.is_quoting}): cached BBO cleared "
            f"and book invalidated; readiness needs a new snapshot "
            f"(disconnects={leg.disconnects})",
        )

    def _on_feed_snapshot_ready(self, leg: LegState, status: InstrumentStatus) -> None:
        """A complete new snapshot landed: the feed side is usable again.

        Only this notice re-arms ``feed_ready``; the market axis is resolved with
        it, except for a halt the venue announced itself, which a local reconnect
        must not clear.
        """
        leg.snapshots += 1
        leg.feed_ready = True
        leg.market_ready = not leg.market_halted
        leg.book_valid = True
        self.log.info(
            f"[{self.symbol}/{leg.spec.label}] new snapshot ready "
            f"(is_trading={status.is_trading}): feed and book usable again, "
            f"market_ready={leg.market_ready} (halted={leg.market_halted}) "
            f"(snapshots={leg.snapshots})",
            LogColor.GREEN,
        )

    def _on_metadata_stale(self, leg: LegState, status: InstrumentStatus) -> None:
        """The adapter's metadata is no longer acceptable: withdraw what rested on it.

        Its own axis and nothing else's (contract A): no feed state moves, no market state
        moves, and only ``adapter:metadata_ready`` clears it. What the leg *used* from that
        metadata is withdrawn with it - the taker fee this leg's cost judgement came from
        becomes unknown, so the directions that touch it are withheld instead of being
        priced against a rate nobody trusts (plan 5.2's precedence, one rung down).

        The tape gets an instrument record that republishes the **last known** payload with
        ``valid=false`` / ``invalid_reason="metadata_stale"``, so a replay stops using it
        from this arrival on while still being able to see which metadata it was.
        """
        leg.metadata_ready = False
        leg.metadata_stale_reason = status.reason
        if VENUES[leg.spec.venue_key].reads_runtime_fee and leg.fee_source != FEE_SOURCE_STALE:
            leg.spec = replace(leg.spec, taker_fee_bps=None)
            leg.fee_source = FEE_SOURCE_STALE
            self._recompute_fees()
        payload = leg.metadata_payload
        if payload is None:
            payload = tape_instrument_metadata(leg, None)
        self._emit_instrument(
            leg, dict(payload), source=INSTRUMENT_UPDATE_SOURCE,
            valid=False, invalid_reason=INVALID_METADATA_STALE,
        )
        self.log.error(
            f"[{self.symbol}/{leg.spec.label}] METADATA STALE ({status.reason!r}, "
            f"is_quoting={status.is_quoting}): the fee/step this leg used from the venue "
            f"metadata are withdrawn, market_ready={leg.market_ready} and the feed are "
            f"untouched; only {ADAPTER_METADATA_READY!r} restores it",
        )

    def _on_metadata_ready(self, leg: LegState, status: InstrumentStatus) -> None:
        """Acceptable, non-stale metadata is in force again (contract A).

        The metadata axis is re-armed and the fee is re-read from the instrument in force
        now, so a fee that changed while the leg was stale is picked up rather than
        restored from a stale memory. Nothing else moves: a metadata refresh is not a
        feed reconnect and never clears a venue halt.
        """
        leg.metadata_ready = True
        leg.metadata_stale_reason = None
        instrument = self.cache.instrument(leg.instrument_id)
        if instrument is None:
            # The adapter owns the verdict, so the axis goes ready - but there is no
            # metadata here to re-read and none is invented; the fee stays withdrawn.
            self.log.warning(
                f"[{self.symbol}/{leg.spec.label}] metadata ready (is_quoting="
                f"{status.is_quoting}) but the cache holds no instrument for "
                f"{leg.instrument_id}: nothing is re-read, the fee stays unknown",
            )
            return
        self._refresh_instrument_metadata(leg, instrument)
        self.log.info(
            f"[{self.symbol}/{leg.spec.label}] metadata ready again (is_quoting="
            f"{status.is_quoting}): re-read from {leg.instrument_id}, "
            f"metadata_ready={leg.metadata_ready}",
            LogColor.GREEN,
        )

    def _on_market_status(self, leg: LegState, status: InstrumentStatus) -> None:
        """A real venue market status - never mixed with the local feed state."""
        was_ready = leg.market_ready
        if status.is_trading is False or status.action in NON_TRADING_ACTIONS:
            leg.market_halted = True
            leg.market_ready = False
        elif status.is_trading is True:
            leg.market_halted = False
            leg.market_ready = True
        # is_trading=None with a non-halting action: keep what we had. Readiness
        # is never assumed from a status that does not say "trading".
        if leg.market_ready != was_ready:
            self.log.warning(
                f"[{self.symbol}/{leg.spec.label}] market status {status.action} "
                f"({status.reason!r}): market_ready={leg.market_ready}",
            )
        else:
            self.log.info(
                f"[{self.symbol}/{leg.spec.label}] market status {status.action} "
                f"({status.reason!r}): market_ready={leg.market_ready}",
            )

    def on_time_event(self, event: TimeEvent) -> None:
        name = event.name
        if name.startswith("fallback-"):
            self._maybe_fallback()
        elif name.startswith("status-"):
            self._log_status()
        elif name.startswith("depth-"):
            self._sample_depth()
            # The tape's flush deadline rides this existing 1 s timer: no new thread, no
            # work in a market-data callback, and a quiet tape still gets drained (F10).
            self._tick_tape()

    def _ensure_depth10(self, leg: LegState, reason: str) -> bool:
        """Subscribe depth10 for this leg once, if the venue offers it."""
        if not VENUES[leg.spec.venue_key].supports_depth10:
            self.log.warning(
                f"[{self.symbol}/{leg.spec.label}] {reason} and {leg.spec.venue_key} "
                f"has no depth10 subscription -> no fallback",
            )
            return False
        if not leg.depth10_subscribed:
            leg.depth10_subscribed = True
            self.subscribe_book_depth10(
                leg.instrument_id, BookType.L2_MBP, client_id=leg.client_id,
            )
            self.log.warning(
                f"[{self.symbol}/{leg.spec.label}] {reason} -> subscribing depth10",
            )
        return True

    def _maybe_fallback(self) -> None:
        """Top-of-book fallback: quotes silent -> drive the spread from depth10."""
        for leg in self._legs:
            if leg.updates:
                continue
            if self._ensure_depth10(leg, f"no quotes in {FALLBACK_SECS}s"):
                leg.source = "depth10"

    # ------------------------------------------------------------------ depth

    def _book_levels(self, leg: LegState) -> tuple[list, list]:
        """Capacity source: the managed L2 book first, depth10 only as a fallback."""
        if not leg.book_valid:
            # A local feed disconnect invalidates the book even though the cache
            # still holds the pre-disconnect copy: it must not be quoted as depth.
            return [], []
        cached = self.cache.order_book(leg.instrument_id)
        for book, mode in ((cached, "deltas-cache"), (leg.local_book, "deltas-local")):
            if book is None:
                continue
            bids, asks = _levels_from_book(book)
            if bids and asks:
                leg.book_mode = mode
                return bids, asks
        if leg.depth10 is not None:
            bids, asks = _levels_from_depth10(leg.depth10)
            if bids and asks:
                leg.book_mode = "depth10"
                return bids, asks
        return [], []

    def _sample_depth(self) -> None:
        now_ns = self.clock.timestamp_ns()
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        silent = self._start_ns and (now_ns - self._start_ns) >= DEPTH_SILENT_SECS * 1_000_000_000
        for leg in self._legs:
            bids, asks = self._book_levels(leg)
            if silent and not (bids and asks) and not leg.depth_warned:
                # The managed deltas book never filled: try depth10 for this leg,
                # and keep writing empty cells until something arrives.
                leg.depth_warned = True
                self._ensure_depth10(
                    leg, f"deltas book still empty after {DEPTH_SILENT_SECS}s "
                         f"({leg.depth_updates} book messages)",
                )
            if not bids or not asks:
                self._depth.write([ts, leg.spec.venue_key, "", "", "", len(bids), len(asks),
                                   *[""] * (2 * len(CAPACITY_BPS))])
                continue
            best_bid, best_ask = bids[0][0], asks[0][0]
            mid = (best_bid + best_ask) / 2.0
            bid_caps = _capacity(bids, best_bid, is_bid=True)
            ask_caps = _capacity(asks, best_ask, is_bid=False)
            self._depth.write([
                ts, leg.spec.venue_key,
                f"{best_bid:.8f}", f"{best_ask:.8f}", f"{mid:.8f}",
                len(bids), len(asks),
                *[f"{v:.2f}" for v in bid_caps],
                *[f"{v:.2f}" for v in ask_caps],
            ])

    # ------------------------------------------------------------------ reference

    def _write_ref_row(self, event: str) -> None:
        """One row per reference update and per perp quote update, side by side.

        Rows before the first reference update are skipped: with no ref_mid there
        is no edge to record, and outside US hours that would be every quote row.
        """
        ref, sink = self._ref, self._ref_sink
        if ref is None or sink is None or ref.updates == 0:
            return
        if event == "ref:ticker" and ref.last == self._ref_last_written:
            # Same print price as the previous row: ref_mid comes from the book, so
            # the edges are unchanged too. NVDA pushed ~250 prints/s on 2026-09-08
            # and 56% of them repeated the price; that was 7.7 MB per 2 minutes.
            return
        self._ref_last_written = ref.last
        now_ns = self.clock.timestamp_ns()
        mid = ref.mid
        src = (
            datetime.fromtimestamp(ref.ts_src_ns / 1e9, timezone.utc)
            .isoformat(timespec="milliseconds")
            if ref.ts_src_ns else ""
        )
        age = ref.age_ms(time.time_ns())  # ts_recv_ns is a wall-clock time.time_ns()
        row: list[object] = [
            datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            event,
            src,
            "" if ref.last is None else f"{ref.last:.6f}",
            "" if ref.bid is None else f"{ref.bid:.6f}",
            "" if ref.ask is None else f"{ref.ask:.6f}",
            "" if mid is None else f"{mid:.6f}",
            "" if age is None else f"{age:.1f}",
            "" if ref.src_to_srv_ms is None else f"{ref.src_to_srv_ms:.1f}",
            ref.book_mode,
        ]
        for leg in self._legs:
            if not leg.ready():
                row += ["", "", "", "", ""]
                continue
            fee = leg.spec.taker_fee_bps
            if fee is None:
                # No known fee: the edge columns would be a cost judgement, so they
                # stay empty rather than assume the dated 2.5 bps (plan 5.2).
                row += [f"{leg.bid:.8f}", f"{leg.ask:.8f}", f"{leg.age_ms(now_ns):.1f}",
                        "", ""]
                continue
            buy = "" if not mid else f"{buy_edge_bps(mid, leg.ask, fee):.4f}"
            sell = "" if not mid else f"{sell_edge_bps(mid, leg.bid, fee):.4f}"
            row += [f"{leg.bid:.8f}", f"{leg.ask:.8f}", f"{leg.age_ms(now_ns):.1f}", buy, sell]
        sink.write(row)

    def _log_ref_status(self, now_ns: int) -> None:
        ref = self._ref
        if ref is None:
            return
        if ref.updates == 0:
            silent_s = (now_ns - self._start_ns) / 1e9 if self._start_ns else 0.0
            if silent_s >= REF_SILENT_SECS and not self._ref_warned:
                self._ref_warned = True
                self.log.warning(
                    f"[{self.symbol}/ref] no reference update for {ref.code} in "
                    f"{silent_s:.0f}s (US regular hours are 13:30-20:00 UTC)",
                )
            self.log.info(
                f"ref: {ref.code} no updates yet ({silent_s:.0f}s)", LogColor.CYAN,
            )
            return
        age = ref.age_ms(time.time_ns())
        kinds = ",".join(f"{k}={n}" for k, n in sorted(ref.by_kind.items()))
        mid = ref.mid
        bid, ask = ref.bid, ref.ask
        books = ",".join(sorted(ref.fresh_books())) or "-"
        hop = ref.src_to_srv_ms
        self.log.info(
            f"ref: {ref.code} last={'-' if ref.last is None else f'{ref.last:.4f}'} "
            f"bid/ask={'-' if bid is None else f'{bid:.4f}@{ref.bid_venue}'}"
            f"/{'-' if ask is None else f'{ask:.4f}@{ref.ask_venue}'} "
            f"mid={'-' if mid is None else f'{mid:.4f}'} "
            f"age={'-' if age is None else f'{age:.0f}'}ms "
            f"src->srv={'-' if hop is None else f'{hop:.0f}'}ms books={books} "
            f"mode={ref.book_mode} crossed={ref.crossed} "
            f"updates={ref.updates} ({kinds}) rows={self._ref_sink.rows if self._ref_sink else 0}",
            LogColor.CYAN,
        )
        if age is not None and age >= REF_SILENT_SECS * 1000:
            self.log.warning(
                f"[{self.symbol}/ref] reference {ref.code} stale for {age / 1000:.0f}s",
            )

    # ------------------------------------------------------------------ status

    def _last_update_ns(self) -> int:
        return max((leg.ts_ns for leg in self._legs), default=0)

    @staticmethod
    def _leg_status_text(leg: LegState) -> str:
        """One leg's status token. Non-tracked legs keep the pre-Ondo format."""
        text = (
            f"{leg.spec.venue_key}={leg.updates}({leg.source}/{leg.book_mode}:"
            f"{leg.depth_updates},trades={leg.trades}"
        )
        if leg.status_tracked:  # the extra axes only exist where they are read
            text += (
                f",feed={'up' if leg.feed_ready else 'DOWN'}"
                f",market={'up' if leg.market_ready else 'HALT'}"
                f",book={'ok' if leg.book_valid else 'STALE'}"
                f",metadata={'ok' if leg.metadata_ready else 'STALE'}"
                f",disconnects={leg.disconnects}"
            )
        return text + ")"

    def _log_status(self) -> None:
        now_ns = self.clock.timestamp_ns()
        # Bound what a dying process can lose: the tape flushes with every write and at
        # least on every status tick, on top of the writer's own 1 s cadence.
        self._flush_tape()
        legs = " ".join(self._leg_status_text(leg) for leg in self._legs)
        gross = " ".join(
            f"{sell}>{buy}={self._last_gross[(sell, buy)]:.2f}"
            for (sell, buy) in self._last_gross
        )
        hits = sum(self._positive.values())
        self.log.info(
            f"STATUS [{self.symbol}] {legs} | samples={self._samples} stale={self._stale} "
            f"| last gross {gross} bps | net+ rows={self._hits.rows} (dirs {hits}) "
            f"| depth rows={self._depth.rows} | trade rows={self._trades.rows}",
            LogColor.CYAN,
        )
        unknown = sorted(key for key, fee in self._fee.items() if fee is None)
        if unknown:
            # Report the missing fee on every status tick: silence here would look
            # like "no opportunity" instead of "no cost-qualified judgement".
            self.log.warning(
                f"[{self.symbol}] fee unknown for {len(unknown)} direction(s) "
                f"({', '.join(f'{sell}>{buy}' for sell, buy in unknown)}): net_bps, "
                f"hits and the cost-qualified judgement are withheld (plan 5.2); "
                f"withheld evaluations={sum(self._fee_unknown.values())}",
            )
        self._log_ref_status(now_ns)
        for leg in self._legs:
            if leg.ts_ns == 0:
                age_s = (now_ns - self._start_ns) / 1e9 if self._start_ns else 0.0
                never = True
            else:
                age_s = (now_ns - leg.ts_ns) / 1e9
                never = False
            if age_s >= LEG_STALE_SECS:
                self.log.warning(
                    f"[{self.symbol}/{leg.spec.label}] no top-of-book update for "
                    f"{age_s:.0f}s" + (" (never received one)" if never else ""),
                )
        self._cfg.run_state.report(self.symbol, self._last_update_ns(), now_ns, self.log)

    # ------------------------------------------------------------------ spread

    def _evaluate(self, updated: LegState) -> None:
        if not updated.ready():
            return
        now_ns = self.clock.timestamp_ns()
        ts = datetime.now(timezone.utc).isoformat(timespec="milliseconds")
        counted = False
        for sell, buy in self._pairs:
            if updated is not sell and updated is not buy:
                continue
            if not (sell.ready() and buy.ready()):
                continue
            age_sell, age_buy = sell.age_ms(now_ns), buy.age_ms(now_ns)
            if age_sell > self._cfg.max_age_ms or age_buy > self._cfg.max_age_ms:
                self._stale += 1
                continue
            mid = ((sell.bid + sell.ask) / 2.0 + (buy.bid + buy.ask) / 2.0) / 2.0
            if mid <= 0.0:
                continue
            key = (sell.spec.venue_key, buy.spec.venue_key)
            threshold = self._fee[key]
            if threshold is None:
                # Cost-qualified judgement withheld: at least one leg has no known
                # fee (plan 5.2). The dated documentation assumption is never used
                # as a substitute, so no net_bps - and no hit row - comes from here.
                self._fee_unknown[key] += 1
                continue
            gross_bps = (sell.bid - buy.ask) / mid * 1e4
            net_bps = gross_bps - threshold
            self._last_gross[key] = gross_bps
            self._gross[key].append(gross_bps)
            self._evals[key] += 1
            if gross_bps < self._gross_min[key]:
                self._gross_min[key] = gross_bps
            if gross_bps > self._gross_max[key]:
                self._gross_max[key] = gross_bps
            if not counted:
                self._samples += 1
                counted = True
            row = self._row(ts, sell, buy, gross_bps, net_bps, age_sell, age_buy)
            if net_bps > 0.0:
                self._positive[key] += 1
                self._hits.write(row)
            if now_ns - self._last_all_ns[key] >= self._cfg.all_sample_ms * 1_000_000:
                self._last_all_ns[key] = now_ns
                self._all.write(row)

    @staticmethod
    def _row(ts: str, sell: LegState, buy: LegState, gross_bps: float, net_bps: float,
             age_sell: float, age_buy: float) -> list:
        return [
            ts, sell.spec.venue_key, buy.spec.venue_key,
            f"{gross_bps:.4f}", f"{net_bps:.4f}",
            sell.bid, sell.bid_size, buy.ask, buy.ask_size,
            sell.ask, buy.bid,
            "" if sell.funding is None else f"{sell.funding:.10f}",
            "" if buy.funding is None else f"{buy.funding:.10f}",
            f"{age_sell:.1f}", f"{age_buy:.1f}",
        ]

    # ------------------------------------------------------------------ summary

    def received_leg_keys(self) -> set[tuple[str, str, str]]:
        """(symbol, venue_key, instrument_id) of every leg that produced a quote.

        Only used to judge "this run saw each leg at least once"; it says nothing
        about continuity, freshness or whether the feed is still alive.
        """
        return {
            (self.symbol, leg.spec.venue_key, leg.spec.instrument_id)
            for leg in self._legs if leg.updates > 0
        }

    def summary(self) -> str:
        lines = [
            "",
            "=" * 78,
            f"SUMMARY [{self.symbol}]",
        ]
        for leg in self._legs:
            lines.append(
                f"  leg {leg.spec.venue_key:<10} top-of-book updates={leg.updates} "
                f"({leg.source})  book updates={leg.depth_updates} ({leg.book_mode})  "
                f"trades={leg.trades}  funding_seen={leg.funding is not None}  "
                f"taker_fee_bps={_fee_text(leg.spec.taker_fee_bps)} "
                f"({leg.fee_source})  "
                + (
                    f"feed_ready={leg.feed_ready} market_ready={leg.market_ready} "
                    f"book_valid={leg.book_valid} disconnects={leg.disconnects} "
                    f"metadata_ready={leg.metadata_ready} "
                    f"metadata_version={leg.metadata_version} "
                    f"metadata_available_ns={leg.metadata_available_ns} "
                    if leg.status_tracked else ""
                )
                + f"{leg.spec.instrument_id}",
            )
        lines.append(
            f"  evaluated samples={self._samples}  skipped-stale={self._stale}",
        )
        unknown = sorted(key for key, fee in self._fee.items() if fee is None)
        if unknown:
            lines.append(
                f"  fee unknown (cost-qualified judgement withheld, plan 5.2): "
                f"{', '.join(f'{sell}>{buy}' for sell, buy in unknown)}  "
                f"withheld evaluations={sum(self._fee_unknown.values())}",
            )
        unusable = [
            f"{leg.spec.venue_key}({','.join(leg.unusable_reasons())})"
            for leg in self._legs
            if leg.unusable_reasons()
        ]
        if unusable:
            lines.append(
                f"  legs not usable at the end of this run: {', '.join(unusable)}",
            )
        lines.append(
            f"  net-positive rows written={self._hits.rows}   "
            f"all-sample rows={self._all.rows}   depth rows={self._depth.rows}   "
            f"trade rows={self._trades.rows}",
        )
        if self._cfg.record_l2:
            lines.append(f"  l2 tape: {self._cfg.tape_path}  {self._tape_text()}")
        for (sell, buy) in self._gross:
            key = (sell, buy)
            series = self._gross[key]
            total = self._evals[key]
            fee = self._fee[key]
            if not total:
                lines.append(
                    f"  {sell}>{buy}: no samples (fee+reserve={_fee_text(fee)} bps)",
                )
                continue
            pct = 100.0 * self._positive[key] / total
            median_note = "" if total <= GROSS_KEEP else f" (last {len(series)})"
            lines.append(
                f"  {sell}>{buy}: net-positive {self._positive[key]}/{total} "
                f"({pct:.2f}%)  gross median={statistics.median(series):.2f}{median_note} "
                f"max={self._gross_max[key]:.2f} min={self._gross_min[key]:.2f} bps  "
                f"fee+reserve={_fee_text(fee)}",
            )
        if self._ref is not None:
            kinds = ",".join(f"{k}={n}" for k, n in sorted(self._ref.by_kind.items())) or "-"
            lines.append(
                f"  reference {self._ref.code}: updates={self._ref.updates} ({kinds})  "
                f"ref rows={self._ref_sink.rows if self._ref_sink else 0}",
            )
        paths = [self._cfg.csv_path, self._cfg.all_csv_path,
                 self._cfg.depth_csv_path, self._cfg.trades_csv_path]
        if self._ref_sink is not None:
            paths.append(self._cfg.ref_csv_path)
        for path in paths:
            lines.append(f"  csv: {path}")
        lines.append("=" * 78)
        return "\n".join(lines)


# ---------------------------------------------------------------- client groups


@dataclass
class ClientGroup:
    """One data client to register: every instrument of the run that rides it.

    Grouped by ClientId, not by venue key or factory: HL and ENTROPY are the same
    client, while LIGHTER and LIGHTER_RH share a factory but are two deployments
    and must stay two clients. Insertion order is the registration order.
    """

    client_id: str
    venue_spec: VenueSpec
    instrument_ids: list[str]


def build_client_groups(plan: dict[str, list[LegSpec]]) -> list[ClientGroup]:
    groups: dict[str, ClientGroup] = {}
    for symbol, legs in plan.items():
        seen: set[str] = set()
        for leg in legs:
            if leg.instrument_id in seen:
                raise ValueError(f"{symbol}: duplicate instrument {leg.instrument_id}")
            seen.add(leg.instrument_id)
            spec = VENUES[leg.venue_key]
            venue = str(InstrumentId.from_str(leg.instrument_id).venue)
            if leg.client_id != spec.venue or venue != spec.venue:
                raise ValueError(f"{symbol}/{leg.venue_key}: inconsistent client route")
            group = groups.get(leg.client_id)
            if group is None:
                groups[leg.client_id] = ClientGroup(leg.client_id, spec, [])
                group = groups[leg.client_id]
            elif (group.venue_spec.venue != spec.venue
                  or group.venue_spec.build_client is not spec.build_client):
                raise ValueError(f"conflicting configuration for {leg.client_id}")
            if leg.instrument_id not in group.instrument_ids:
                group.instrument_ids.append(leg.instrument_id)
    return list(groups.values())


def missing_leg_keys(
    plan: dict[str, list[LegSpec]],
    received: set[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    """Legs of the plan that never produced a top-of-book update in this run."""
    return sorted({
        (symbol, leg.venue_key, leg.instrument_id)
        for symbol, legs in plan.items() for leg in legs
    } - received)


def describe_plan(plan: dict[str, list[LegSpec]], *, record_l2: bool = False) -> dict[str, object]:
    """What this run would subscribe to, without building a client or touching the net.

    ``market_availability_checked`` stays False on purpose: the mapping is the
    repository's static knowledge, not a probe of the venues. When ONDO is in the
    plan its registry fee is carried explicitly as a dated assumption: a live run
    replaces it from the instrument metadata (see ``_read_runtime_fees``).
    """
    described: dict[str, object] = {
        "mode": "read-only",
        "market_availability_checked": False,
        "record_l2": bool(record_l2),
        "symbols": {
            symbol: [
                {"venue_key": leg.venue_key, "instrument_id": leg.instrument_id,
                 "client_id": leg.client_id, "taker_fee_bps": leg.taker_fee_bps}
                for leg in legs
            ]
            for symbol, legs in plan.items()
        },
        "data_clients": [
            {"client_id": group.client_id, "instrument_ids": group.instrument_ids}
            for group in build_client_groups(plan)
        ],
    }
    if record_l2:
        described["record_l2_note"] = (
            f"every observed leg of this run records complete normalized L2 as JSONL "
            f"fragments plus a manifest under <out>/{L2_DIRNAME}; when the run contains "
            f"ONDO its data client also gets raw_md_path=<out>/{RAW_MD_DIRNAME} and the "
            f"run's own stamp as that recorder's run id (both halves of the run then join "
            f"on run_id, whatever --out is called)"
        )
    if any(leg.venue_key == "ONDO" for legs in plan.values() for leg in legs):
        described["ondo_taker_fee_bps"] = ONDO_TAKER_FEE_BPS
        described["ondo_fee_source"] = ONDO_FEE_SOURCE
        described["ondo_fee_note"] = (
            "taker_fee_bps above is the dated documentation assumption, printed for "
            "dry-run only; a live run reads the fee from the instrument metadata and "
            "withholds the cost-qualified judgement when none is published"
        )
    return described


# ---------------------------------------------------------------- node


@dataclass
class NodePlan:
    """Everything the node builder needs; fixed once at process start."""

    plan: dict[str, list[LegSpec]]
    out_dir: Path
    stamp: str
    depth_levels: int
    all_sample_ms: int
    reference: str = "none"  # none / FUTU / FAKE
    stems: dict[str, str] = field(default_factory=dict)
    ref_codes: dict[str, str] = field(default_factory=dict)  # symbol -> Futu code
    # --record-l2 (plan 5.2): explicit, default off. The run directory holds the tape.
    record_l2: bool = False

    def __post_init__(self) -> None:
        for symbol, legs in self.plan.items():
            self.stems[symbol] = csv_stem(symbol, legs, self.stamp)
        if self.reference.lower() == "none":
            return
        for symbol in self.plan:
            code = reference_code(symbol)
            if code is None:
                print(
                    f"[stage1] INFO {symbol}: no reference price (not a US equity); "
                    f"--reference has no effect on it",
                    flush=True,
                )
                continue
            self.ref_codes[symbol] = code

    @property
    def l2_dir(self) -> Path:
        """Where this run's L2 tape fragments and manifests live."""
        return self.out_dir / L2_DIRNAME

    @property
    def raw_md_dir(self) -> Path:
        """The Ondo adapter's raw public-frame directory for this run (plan 5.2).

        The fork-side recorder owns the layout inside it; the application only points
        ``OndoDataClientConfig.raw_md_path`` at it.
        """
        return self.out_dir / RAW_MD_DIRNAME

    def tape_path(self, symbol: str) -> Path:
        """The tape fragment set of one symbol: every leg of it shares one stream."""
        return self.l2_dir / f"l2_{self.stems[symbol]}.jsonl"


def build_node(np: NodePlan) -> tuple[LiveNode, list[SpreadWatch], RunState]:
    run_state = RunState()
    builder = (
        LiveNode.builder("STAGE1-SPREAD-WATCH", TraderId.from_str("STAGE1-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.INFO))
        .with_timeout_connection(60)
        .with_delay_post_stop_secs(2)
    )
    # Exactly one data client per ClientId, carrying the union of the instrument ids
    # that ride it (Aster's load_ids must cover every Aster symbol in the run, and the
    # xyz: plus io: legs share the one HYPERLIQUID client).
    ondo_legs = [
        leg for legs in np.plan.values() for leg in legs if leg.venue_key == ONDO_VENUE_KEY
    ]
    if np.record_l2 and ondo_legs:
        # Plan 5.2: the fork-side recorder writes its raw public frames into the run's
        # raw_ondo directory; the application only plumbed the path (below).
        np.raw_md_dir.mkdir(parents=True, exist_ok=True)
    coverage_limits: dict[str, int | None] = {}
    for group in build_client_groups(np.plan):
        extra: dict[str, object] = {}
        if np.record_l2 and group.client_id == ONDO_VENUE_KEY:
            # Only the ONDO builder takes these: the other venues' config systems are
            # deliberately left alone (plan 5.2).
            extra["raw_md_path"] = str(np.raw_md_dir)
            # The run id the fork's recorder must stamp on its raw frames: this run's own
            # stamp, which is the run_id of every tape record built below. Without it the
            # fork derives the id from the raw_ondo directory's *parent* name, so a default
            # `--out reports/stage1` would silently record `stage1` and the two halves of
            # the run would not join.
            extra["raw_md_run_id"] = np.stamp
        factory, client_config = group.venue_spec.build_client(group.instrument_ids, **extra)
        if group.client_id == ONDO_VENUE_KEY:
            # The tape's coverage_limit for the ONDO leg is the configured book_limit of the
            # client that actually serves it, not a constant copied into this file.
            limit = getattr(client_config, "book_limit", None)
            coverage_limits[ONDO_VENUE_KEY] = ondo_book_limit() if limit is None else int(limit)
        # Name the client after the venue instead of letting it default to the factory
        # name: LIGHTER and LIGHTER_RH share one factory ("LIGHTER"), so the default
        # would collide. The name is the ClientId the legs subscribe with.
        builder = builder.add_data_client(group.client_id, factory, client_config)
    node = builder.build()

    if np.ref_codes:
        # One feed for the whole node: it publishes per-code custom data that each
        # symbol's strategy subscribes to. No data client, no execution client.
        codes = sorted(set(np.ref_codes.values()))
        node.add_actor(RefActor(RefActorConfig(
            actor_id=ActorId("REF-FEED"),
            codes=codes,
            feed_kind=np.reference,
        )))

    strategies: list[SpreadWatch] = []
    for symbol, legs in np.plan.items():
        stem = np.stems[symbol]
        ref_code = np.ref_codes.get(symbol)
        config = SpreadWatchConfig(
            strategy_id=StrategyId.from_str(f"SPREAD-WATCH-{symbol}"),
            symbol=symbol,
            legs=legs,
            csv_path=np.out_dir / f"spread_{stem}.csv",
            all_csv_path=np.out_dir / f"spread_{stem}_all.csv",
            depth_csv_path=np.out_dir / f"depth_{stem}.csv",
            trades_csv_path=np.out_dir / f"trades_{stem}.csv",
            ref_code=ref_code,
            ref_csv_path=(np.out_dir / f"{stem}_ref.csv") if ref_code else None,
            run_state=run_state,
            book_depth_levels=np.depth_levels,
            all_sample_ms=np.all_sample_ms,
            record_l2=np.record_l2,
            tape_path=np.tape_path(symbol) if np.record_l2 else None,
            run_id=np.stamp,
            coverage_limits=dict(coverage_limits),
        )
        strategy = SpreadWatch(config)
        node.add_strategy(strategy)
        strategies.append(strategy)
    return node, strategies, run_state


# ---------------------------------------------------------------- cli


def parse_until(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise SystemExit(f"[stage1] bad --until {value!r}: expected UTC ISO, "
                         f"e.g. 2026-09-08T20:05:00Z") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def resolve_targets(args, parser) -> tuple[list[str], list[str]]:
    given = [bool(args.pair), bool(args.symbol), bool(args.symbols)]
    if sum(given) > 1:
        parser.error("use only one of --pair / --symbol / --symbols")
    if args.pair:
        symbol, venue_keys = parse_pair(args.pair)
        return [symbol], venue_keys
    if args.symbol:
        return [args.symbol.upper()], list(DEFAULT_PAIR)
    venue_keys = [v.strip().upper() for v in args.venues.split(",") if v.strip()]
    if len(venue_keys) < 2:
        parser.error("--venues needs at least two venues")
    if len(set(venue_keys)) != len(venue_keys):
        parser.error("--venues must not repeat a venue")
    source = args.symbols or "NVDA"
    symbols: list[str] = []
    for name in source.split(","):
        name = name.strip().upper()
        if name and name not in symbols:
            symbols.append(name)
    if not symbols:
        parser.error("--symbols is empty")
    return symbols, venue_keys


def main() -> None:
    parser = argparse.ArgumentParser(description="Stage 1 read-only cross-venue spread watch")
    parser.add_argument("--minutes", type=float, default=30.0, help="run duration in minutes")
    parser.add_argument("--until", default=None,
                        help="stop at this UTC ISO instant, e.g. 2026-09-08T20:05:00Z "
                             "(alternative to --minutes)")
    parser.add_argument("--out", type=Path, default=Path("reports/stage1"),
                        help="output directory")
    parser.add_argument("--symbols", default=None,
                        help=f"comma list of symbols, e.g. NVDA,TSLA; known: {sorted(INSTRUMENTS)}")
    parser.add_argument("--venues", default=",".join(DEFAULT_VENUES),
                        help=f"comma list of venues to watch per symbol; known: {sorted(VENUES)}")
    parser.add_argument("--pair", default=None,
                        help=f"legacy alias: SYMBOL:VENUE_A-VENUE_B, e.g. NVDA:HL-ASTER; "
                             f"venues: {sorted(VENUES)}")
    parser.add_argument("--symbol", default=None, choices=sorted(INSTRUMENTS),
                        help="legacy alias for SYMBOL:HL-LIGHTER")
    parser.add_argument("--all-sample-ms", type=int, default=ALL_SAMPLE_MS,
                        help="min interval between _all.csv rows per pair-direction; "
                             "0 = every evaluation")
    parser.add_argument("--depth-levels", type=int, default=10,
                        help="book depth levels for the depth10 fallback "
                             "(v2 fixes this at 10; kept for config parity)")
    parser.add_argument("--max-restarts", type=int, default=MAX_RESTARTS,
                        help="rebuild the node at most this many times before the deadline")
    parser.add_argument("--reference", default="none", choices=["none", "FUTU", "FAKE"],
                        help="reference price leg: FUTU streams the real US stock quote "
                             "from the FUTUNN OPEN API WebSocket (needs FUTU_API_KEY / "
                             "FUTU_PRIVATE_KEY in .env), FAKE is a local random walk for "
                             "testing the path; writes <stem>_ref.csv per equity symbol")
    parser.add_argument("--dry-run", action="store_true",
                        help="print the resolved plan and data clients as JSON, then exit "
                             "before dotenv, the node and any subscription")
    parser.add_argument("--record-l2", action="store_true",
                        help="record every observed leg's complete normalized L2 as JSONL "
                             f"fragments plus a manifest under <out>/{L2_DIRNAME} (a run "
                             f"with an ONDO leg also points its raw_md_path at "
                             f"<out>/{RAW_MD_DIRNAME}); off by default")
    args = parser.parse_args()

    symbols, venue_keys = resolve_targets(args, parser)
    plan = build_plan(symbols, venue_keys)

    if args.dry_run:
        print(json.dumps(describe_plan(plan, record_l2=args.record_l2), indent=2))
        return

    started = datetime.now(timezone.utc)
    if args.until:
        deadline = parse_until(args.until)
        if deadline <= started:
            raise SystemExit(f"[stage1] --until {args.until} is already in the past")
    else:
        deadline = started + timedelta(minutes=args.minutes)

    try:  # optional; no credentials are needed for read-only market data
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    node_plan = NodePlan(
        plan=plan,
        out_dir=args.out,
        stamp=started.strftime("%Y%m%dT%H%M%SZ"),  # fixed at process start, not per restart
        depth_levels=args.depth_levels,
        all_sample_ms=args.all_sample_ms,
        reference=args.reference,
        record_l2=args.record_l2,
    )
    describe = "  ".join(
        f"{symbol}[{'-'.join(leg.venue_key for leg in legs)}]" for symbol, legs in plan.items()
    )
    if node_plan.ref_codes:
        describe += f"  reference[{args.reference}]=" + ",".join(
            sorted(set(node_plan.ref_codes.values())),
        )
    if node_plan.record_l2:
        describe += f"  record-l2[{L2_DIRNAME}]"
    print(
        f"[stage1] watching {describe} until {deadline.isoformat(timespec='seconds')} "
        f"-> {args.out}",
        flush=True,
    )

    strategies: list[SpreadWatch] = []
    restarts = 0
    # Every leg that ever produced top-of-book data, across all nodes of this run:
    # a leg covered by an earlier node still counts after a restart.
    received_legs: set[tuple[str, str, str]] = set()
    # Every recording failure of this run, node restarts included (plan 5.1: a gap or a dead
    # recorder fails this *recording's* acceptance, and §8 needs the run reportable as failed).
    recording_failures: list[str] = []

    def note_recording_failure(message: str) -> None:
        if message not in recording_failures:
            recording_failures.append(message)
        print(f"[stage1] RECORDING FAILED {message}", file=sys.stderr, flush=True)

    while True:
        remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
        if remaining <= 1.0:
            break
        node, strategies, run_state = build_node(node_plan)
        handle = node.handle()
        run_state.stop_node = handle.stop
        timer = threading.Timer(remaining, handle.stop)
        timer.daemon = True
        timer.start()
        interrupted = False
        failure: BaseException | None = None
        try:
            node.run()
        except KeyboardInterrupt:
            print("[stage1] interrupted, stopping", flush=True)
            handle.stop()
            interrupted = True
        except Exception as exc:  # noqa: BLE001 - any adapter fault restarts the node
            failure = exc
        finally:
            timer.cancel()

        for strategy in strategies:
            received_legs.update(strategy.received_leg_keys())
            print(strategy.summary(), flush=True)
        for message in run_state.recording_errors:
            # A recording failure is not a spread failure (plan 5.1: the recording's own
            # acceptance fails), so it is reported loudly and the spread run keeps going -
            # but it is remembered, and the exit status of this run reflects it below.
            note_recording_failure(message)
        if run_state.startup_errors:
            # A leg could not be found at all: keep the evidence and stop for good
            # instead of restarting into the same failure.
            for message in run_state.startup_errors:
                print(f"[stage1] STARTUP FAILED {message}", file=sys.stderr, flush=True)
            raise SystemExit(1)
        if failure is not None:
            print(f"[stage1] node run failed: {failure!r}", flush=True)
        if interrupted:
            break
        left = (deadline - datetime.now(timezone.utc)).total_seconds()
        if left <= 5.0:
            break
        restarts += 1
        if restarts > args.max_restarts:
            print(f"[stage1] giving up after {args.max_restarts} restarts", flush=True)
            break
        print(
            f"[stage1] node stopped {left:.0f}s before the deadline "
            f"-> restart {restarts}/{args.max_restarts} in {RESTART_PAUSE_SECS}s",
            flush=True,
        )
        time.sleep(RESTART_PAUSE_SECS)

    # The adapter's raw public-frame recorder finalizes (writes its own run_end) when the
    # node that owns it is released, so the last node - and the handle that can keep it
    # alive through run_state - is dropped before its recording is read. Reading a
    # recorder that is still running would call every run incomplete.
    node = None
    handle = None
    run_state.stop_node = None
    strategies = []

    if restarts:
        print(f"[stage1] node restarts during this run: {restarts}", flush=True)
    if node_plan.record_l2:
        # One run-level manifest so an analyzer reads the fragments in order without
        # guessing the rotation naming.
        manifest = write_run_manifest(node_plan.l2_dir)
        if manifest is not None:
            print(f"[stage1] l2 tape manifest: {manifest}", flush=True)
        else:
            note_recording_failure(
                f"no tape fragment was written under {node_plan.l2_dir}",
            )
        if any(leg.venue_key == ONDO_VENUE_KEY for legs in plan.values() for leg in legs):
            # The adapter's own raw public-frame recording: read its verdict from the
            # files it wrote, never from the fact that the feed was still flowing (plan
            # §8). A recording that cannot be confirmed complete is an incomplete
            # recording, and this run says so.
            raw = raw_recorder_status(node_plan.raw_md_dir, run_id=node_plan.stamp)
            print(
                f"[stage1] raw_ondo recording ({node_plan.raw_md_dir}): "
                f"{raw_recorder_text(raw)}",
                flush=True,
            )
            if not raw["complete"]:
                note_recording_failure(
                    f"the raw public-frame recording under {node_plan.raw_md_dir} is "
                    f"incomplete: {raw['problem']}",
                )
    if recording_failures:
        # Plan 5.1 and §8: a gap or a dead recorder means this recording's acceptance
        # failed, so the run must be reportable as failed - an orchestrator keyed on the
        # exit code must never read it as a passing run.
        raise SystemExit(1)
    # Legs skipped by build_plan are not in the plan, so they are not "missing" here.
    missing = missing_leg_keys(plan, received_legs)
    if missing:
        for symbol, key, instrument_id in missing:
            print(f"[stage1] INCOMPLETE {symbol}/{key}: no top-of-book data for "
                  f"{instrument_id}", file=sys.stderr, flush=True)
        raise SystemExit(1)


if __name__ == "__main__":
    main()

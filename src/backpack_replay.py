"""Versioned recorded public sessions delegated byte-for-byte to native Backpack replay."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import json
from pathlib import Path
import time
import uuid

from backpack_config import BackpackConfigError, _integer, _keys, _text
from backpack_public import (
    PublicEvidence,
    _event_fields,
    application_identity,
    configuration_hash,
    native_identity,
)

MANIFEST_BYTES = 1_048_576
RECORD_BYTES = 1_048_576


class OfflineFailure(ValueError):
    """A fixed safe failure reason; source lines and native exceptions never enter output."""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise OfflineFailure("duplicate_manifest_field")
        result[key] = value
    return result


def read_manifest(plan):
    path = plan.replay_file
    if path is None or any(part.startswith(".env") for part in path.resolve().parts):
        raise OfflineFailure("invalid_recorded_session_path")
    with path.open("rb") as stream:
        raw = stream.read(min(MANIFEST_BYTES, plan.max_input_bytes) + 1)
    if len(raw) > min(MANIFEST_BYTES, plan.max_input_bytes):
        raise OfflineFailure("manifest_byte_limit")
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object)
        _keys(
            value,
            {"schema_version", "venue", "source", "source_reference", "streams"},
            {"schema_version", "venue", "source", "source_reference", "streams"},
            "recorded session",
        )
        if (
            type(value["schema_version"]) is not int
            or value["schema_version"] != 1
            or value["venue"] != "BACKPACK"
        ):
            raise OfflineFailure("unsupported_recorded_session")
        if value["source"] not in {"Synthetic", "PublicCapture"}:
            raise OfflineFailure("invalid_recording_provenance")
        _text(value["source_reference"], "recording source reference")
        if len(value["source_reference"]) > 1024:
            raise OfflineFailure("recording_reference_limit")
        streams = value["streams"]
        if not isinstance(streams, dict) or set(streams) != set(plan.symbols):
            raise OfflineFailure("recorded_allowlist_mismatch")
        root = path.resolve().parent
        paths = set()
        for entry in streams.values():
            _keys(
                entry,
                {
                    "market_json",
                    "metadata_received_at_ns",
                    "generation",
                    "records_file",
                },
                {
                    "market_json",
                    "metadata_received_at_ns",
                    "generation",
                    "records_file",
                },
                "recorded stream",
            )
            if not isinstance(entry["market_json"], str):
                raise OfflineFailure("invalid_recorded_market_text")
            _integer(
                entry["metadata_received_at_ns"], 0, 2**64 - 1, "metadata receipt time"
            )
            _integer(entry["generation"], 0, 2**64 - 1, "recorded generation")
            name = Path(_text(entry["records_file"], "record file"))
            if (
                name.is_absolute()
                or name.suffix != ".jsonl"
                or any(part.startswith(".env") for part in name.parts)
            ):
                raise OfflineFailure("invalid_record_file_path")
            resolved = (root / name).resolve()
            if (
                not resolved.is_relative_to(root)
                or resolved in paths
                or any(part.startswith(".env") for part in resolved.parts)
            ):
                raise OfflineFailure("record_file_scope_mismatch")
            paths.add(resolved)
            entry["path"] = resolved
    except (ValueError, TypeError, KeyError, UnicodeError) as e:
        if isinstance(e, OfflineFailure):
            raise
        raise OfflineFailure("invalid_recorded_session") from None
    return value, len(raw), hashlib.sha256(raw).hexdigest()


def data_document(data):
    kind = type(data).__name__
    fields = _event_fields(data)
    if kind == "QuoteTick":
        return "quote", {
            **fields,
            "bid_price": str(data.bid_price),
            "ask_price": str(data.ask_price),
            "bid_size": str(data.bid_size),
            "ask_size": str(data.ask_size),
        }
    if kind == "TradeTick":
        return "trade", {
            **fields,
            "price": str(data.price),
            "size": str(data.size),
            "trade_id": str(data.trade_id),
            "aggressor_side": str(data.aggressor_side),
        }
    if kind == "MarkPriceUpdate":
        return "mark", {**fields, "price": str(data.value)}
    if kind == "OrderBookDeltas":
        return "book_deltas", {
            **fields,
            "sequence": str(data.sequence),
            "flags": data.flags,
            "deltas": [
                {
                    "action": str(delta.action),
                    "side": str(delta.order.side),
                    "price": str(delta.order.price),
                    "size": str(delta.order.size),
                }
                for delta in data.deltas
            ],
            "complete_book_claimed": False,
        }
    raise OfflineFailure("unsupported_native_replay_domain")


async def checkpoint(deadline, evidence):
    await asyncio.sleep(0)
    if time.monotonic() >= deadline:
        raise OfflineFailure("offline_duration_limit")
    if evidence.limit_reached:
        raise OfflineFailure("report_limit")


async def ingest(plan, manifest, evidence, deadline, inputs):
    from nautilus_trader.adapters.backpack import (
        BackpackDataClientConfig,
        BackpackInstrumentEconomics,
        BackpackPublicReplay,
    )

    economics = {
        symbol: BackpackInstrumentEconomics(**values.document())
        for symbol, values in plan.economics
    }
    config = BackpackDataClientConfig(list(plan.symbols), economics)
    instruments = {}
    data = []
    for symbol in sorted(plan.symbols):
        await checkpoint(deadline, evidence)
        entry = manifest["streams"][symbol]
        try:
            owner = BackpackPublicReplay(
                config,
                entry["market_json"],
                entry["metadata_received_at_ns"],
                generation=entry["generation"],
            )
            if str(owner.instrument.id) != f"{symbol}.BACKPACK":
                raise OfflineFailure("recorded_market_symbol_mismatch")
        except Exception:
            raise OfflineFailure("invalid_native_recorded_market") from None
        instruments[symbol] = owner.instrument
        evidence.record(
            "instrument",
            {
                "instrument_id": str(owner.instrument.id),
                "price_increment": str(owner.instrument.price_increment),
                "size_increment": str(owner.instrument.size_increment),
                "metadata_received_at_ns": str(entry["metadata_received_at_ns"]),
                "execution_ready": False,
            },
        )
        digest = hashlib.sha256()
        file_info = {
            "records_file": entry["records_file"],
            "consumed_sha256": None,
            "complete": False,
            "consumed_bytes": 0,
            "records": 0,
        }
        inputs["streams"][symbol] = file_info
        previous = None
        try:
            with entry["path"].open("rb") as stream:
                while True:
                    await checkpoint(deadline, evidence)
                    line = stream.readline(RECORD_BYTES + 1)
                    if not line:
                        file_info["complete"] = True
                        break
                    digest.update(line)
                    file_info["consumed_bytes"] += len(line)
                    file_info["records"] += 1
                    inputs["bytes"] += len(line)
                    inputs["records"] += 1
                    if (
                        len(line) > RECORD_BYTES
                        or inputs["bytes"] > plan.max_input_bytes
                        or inputs["records"] > plan.max_input_records
                    ):
                        raise OfflineFailure("recorded_input_limit")
                    try:
                        event = owner.apply_record(line)
                    except Exception:
                        raise OfflineFailure("invalid_native_replay_record") from None
                    if event is not None:
                        if previous is not None and event.ts_init < previous:
                            raise OfflineFailure("nonmonotonic_recorded_receipts")
                        previous = event.ts_init
                        data.append(
                            (event.ts_init, symbol, file_info["records"], event)
                        )
                    else:
                        inputs["no_data_records"] += 1
        finally:
            file_info["consumed_sha256"] = digest.hexdigest()
    # Native clock advances on receipt time. Ties use symbol then original per-file ordinal.
    data.sort(key=lambda entry: entry[:3])
    return instruments, data


async def run_offline(plan, *, candidate=None):
    if (
        plan.mode not in {"replay", "paper"}
        or plan.environment != "offline"
        or plan.account is not None
    ):
        raise BackpackConfigError(
            "replay/paper require an offline credential-free plan"
        )
    started = datetime.now(UTC).isoformat()
    began = time.monotonic()
    evidence = PublicEvidence(plan)
    inputs = {
        "manifest_sha256": None,
        "streams": {},
        "records": 0,
        "bytes": 0,
        "no_data_records": 0,
    }
    identity = {"status": "unavailable", "source_binding_verified": False}
    failure = None
    cancelled = False
    result = None
    preparation_ms = None
    shutdown_complete = True
    try:
        identity = native_identity(candidate)
        manifest, size, digest = read_manifest(plan)
        inputs.update(
            manifest_sha256=digest,
            bytes=size,
            source=manifest["source"],
            source_reference=manifest["source_reference"],
        )
        preparation_ms = int((time.monotonic() - began) * 1000)
        deadline = time.monotonic() + plan.duration_secs
        instruments, data = await ingest(plan, manifest, evidence, deadline, inputs)
        for _, _, _, event in data:
            await checkpoint(deadline, evidence)
            kind, fields = data_document(event)
            evidence.record(
                kind, {**fields, "historical": True, "live_freshness_claimed": False}
            )
        await checkpoint(deadline, evidence)
        if plan.mode == "paper":
            from backpack_paper import run_paper

            result = {}
            await run_paper(plan, instruments, data, evidence, deadline, result)
            if not result["complete"]:
                raise OfflineFailure("paper_scenario_incomplete")
    except asyncio.CancelledError:
        cancelled = True
        failure = "cancelled"
    except OfflineFailure as e:
        failure = str(e)
    except Exception:
        failure = "offline_runtime_failed"
    finally:
        summary = {
            "schema_version": 1,
            "run_id": uuid.uuid4().hex,
            "venue": "BACKPACK",
            "mode": plan.mode,
            "status": "failed" if failure else "completed",
            "failure": failure,
            "configuration_sha256": configuration_hash(plan),
            "configuration": plan.document(),
            "started_at": started,
            "finished_at": datetime.now(UTC).isoformat(),
            "elapsed_ms": int((time.monotonic() - began) * 1000),
            "preparation_ms": preparation_ms,
            "application": application_identity(
                extra_sources=("backpack_replay.py", "backpack_paper.py")
            ),
            "native": identity,
            "input": inputs,
            "paper": result,
            "global_order": "ts_init_ns_then_symbol_then_original_file_ordinal",
            "event_count": len(evidence.lines),
            "event_bytes": evidence.bytes,
            "event_counts": evidence.counts,
            "dropped_events": evidence.dropped,
            "report_limit_reached": evidence.limit_reached,
            "shutdown_complete": result.get("shutdown_complete", False)
            if result is not None
            else shutdown_complete,
            "historical": True,
            "live_freshness_claimed": False,
            "execution_ready": False,
            "remote_writes_allowed": False,
            "venue_account_observed": False,
            "durable_state_opened": False,
        }
        published = evidence.publish(summary)
    if cancelled:
        raise asyncio.CancelledError
    return published

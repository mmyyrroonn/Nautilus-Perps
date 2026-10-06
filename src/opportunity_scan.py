#!/usr/bin/env python3
"""Configure a read-only cross-venue scanner without importing native code."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, field, replace
from decimal import Decimal, InvalidOperation
from fractions import Fraction
import json
from pathlib import Path
import sys
import tomllib

from opportunity_core import ScanSettings


CLIENTS = {
    "HL": "HYPERLIQUID", "ENTROPY": "HYPERLIQUID", "LIGHTER": "LIGHTER",
    "LIGHTER_RH": "LIGHTER_ROBINHOOD", "ASTER": "ASTER", "ONDO": "ONDO",
    "BACKPACK": "BACKPACK",
}


class ScanConfigError(ValueError):
    pass


def decimal_value(value, name: str, *, positive=False, nonnegative=False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int, Decimal)):
        raise ScanConfigError(f"{name} must be an exact decimal string or integer")
    try:
        result = Decimal(value)
    except InvalidOperation as exc:
        raise ScanConfigError(f"{name} must be a decimal") from exc
    if not result.is_finite() or (positive and result <= 0) or (nonnegative and result < 0):
        raise ScanConfigError(f"{name} is outside its allowed range")
    return result


def integer_value(value, name: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ScanConfigError(f"{name} must be an integer in [{minimum}, {maximum}]")
    return value


def allowed_keys(document: dict, keys: set[str], label: str) -> None:
    if not isinstance(document, dict) or document.keys() - keys:
        raise ScanConfigError(f"unknown or invalid {label} fields")


@dataclass(frozen=True)
class MarketPlan:
    symbol: str
    venue: str
    instrument_id: str
    quote_to_usd: Decimal
    valuation_source: str
    taker_fee_bps: Decimal | None = None
    fee_source: str | None = None
    canonical_multiplier: Decimal = Decimal("1")
    backpack_economics: dict[str, str] = field(default_factory=dict)

    @property
    def client_id(self) -> str:
        return CLIENTS[self.venue]


@dataclass(frozen=True)
class ScanPlan:
    markets: tuple[MarketPlan, ...]
    settings: ScanSettings
    output_path: Path | None
    cooldown_ms: int = 5000
    edge_change_bps: Decimal = Decimal("2")
    quantity_change_fraction: Decimal = Decimal("0.1")
    max_interval_ms: int | None = None
    depth_levels: int = 20
    max_levels_per_side: int = 2000
    refresh_ms: int = 1000
    top_n: int = 20
    duration_secs: int = 3600
    connection_timeout_secs: int = 30

    def document(self) -> dict:
        def plain(value):
            if isinstance(value, (Decimal, Path)):
                return str(value)
            if isinstance(value, dict):
                return {k: plain(v) for k, v in value.items()}
            if isinstance(value, (tuple, list)):
                return [plain(v) for v in value]
            return value
        result = plain(asdict(self))
        result.update(schema_version=1, runtime_started=False, remote_writes_allowed=False,
                      record_opportunities=self.output_path is not None,
                      historical_samples_retained=0)
        return result


def parse_plan(document: dict, config_dir: Path) -> ScanPlan:
    allowed_keys(document, {"schema_version", "scan", "recording", "runtime", "markets"}, "root")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ScanConfigError("schema_version must be 1")
    scan = document.get("scan", {})
    allowed_keys(scan, {"target_notional", "min_entry_edge_bps", "reserve_bps", "max_age_ms",
                        "max_skew_ms", "max_receive_age_ms"}, "scan")
    settings = ScanSettings(
        target_notional=decimal_value(scan.get("target_notional", "500"), "target_notional", positive=True),
        min_entry_edge_bps=decimal_value(scan.get("min_entry_edge_bps", "0"), "min_entry_edge_bps", nonnegative=True),
        reserve_bps=decimal_value(scan.get("reserve_bps", "5"), "reserve_bps", nonnegative=True),
        max_age_ms=integer_value(scan.get("max_age_ms", 2000), "max_age_ms", 1, 60000),
        max_skew_ms=integer_value(scan.get("max_skew_ms", 500), "max_skew_ms", 0, 60000),
        max_receive_age_ms=integer_value(scan.get("max_receive_age_ms", 2000), "max_receive_age_ms", 1, 60000),
    )
    recording = document.get("recording", {})
    allowed_keys(recording, {"enabled", "path", "cooldown_ms", "edge_change_bps",
                            "quantity_change_fraction", "max_interval_ms"}, "recording")
    enabled = recording.get("enabled", True)
    if type(enabled) is not bool:
        raise ScanConfigError("recording.enabled must be boolean")
    output = recording.get("path", "../reports/opportunities/opportunities.jsonl")
    if not isinstance(output, str) or not output or Path(output).suffix != ".jsonl":
        raise ScanConfigError("recording.path must name a .jsonl file")
    output_path = (config_dir / output).resolve() if enabled else None
    cooldown = integer_value(recording.get("cooldown_ms", 5000), "cooldown_ms", 0, 86400000)
    interval = recording.get("max_interval_ms")
    if interval is not None:
        interval = integer_value(interval, "max_interval_ms", max(1, cooldown), 86400000)
    runtime = document.get("runtime", {})
    allowed_keys(runtime, {"depth_levels", "max_levels_per_side", "refresh_ms", "top_n",
                          "duration_secs", "connection_timeout_secs"}, "runtime")
    cap = integer_value(runtime.get("max_levels_per_side", 2000), "max_levels_per_side", 1, 20000)
    depth = integer_value(runtime.get("depth_levels", 20), "depth_levels", 1, cap)
    raw_markets = document.get("markets")
    if not isinstance(raw_markets, list) or not 2 <= len(raw_markets) <= 1000:
        raise ScanConfigError("markets must contain 2..1000 explicit market mappings")
    markets = []
    ids = set()
    groups: dict[str, set[str]] = {}
    for market in raw_markets:
        allowed_keys(market, {"symbol", "venue", "instrument_id", "quote_to_usd", "valuation_source",
                              "taker_fee_bps", "fee_source", "canonical_multiplier", "backpack_economics"}, "market")
        symbol, venue, instrument_id = (market.get(k) for k in ("symbol", "venue", "instrument_id"))
        if not isinstance(symbol, str) or not symbol or len(symbol) > 40 or not all(
                c.isalnum() or c in "_-" for c in symbol):
            raise ScanConfigError("market.symbol must be a short canonical underlying name")
        if not isinstance(venue, str) or venue not in CLIENTS:
            raise ScanConfigError("unknown market.venue")
        if not isinstance(instrument_id, str) or not instrument_id.endswith("." + CLIENTS[venue]):
            raise ScanConfigError("instrument_id does not match its venue")
        if len(instrument_id) > 120 or any(c.isspace() for c in instrument_id) or instrument_id in ids:
            raise ScanConfigError("duplicate or invalid instrument_id")
        ids.add(instrument_id)
        group = groups.setdefault(symbol, set())
        if venue in group:
            raise ScanConfigError("each symbol may have only one market per logical venue")
        group.add(venue)
        valuation = market.get("valuation_source")
        if not isinstance(valuation, str) or not valuation.strip():
            raise ScanConfigError("each market needs an explicit valuation_source")
        fee = market.get("taker_fee_bps")
        source = market.get("fee_source")
        if fee is not None:
            fee = decimal_value(fee, "taker_fee_bps", nonnegative=True)
            if fee > 1000 or not isinstance(source, str) or not source.strip():
                raise ScanConfigError("a configured fee needs a source and must be <= 1000 bps")
        elif source is not None:
            raise ScanConfigError("fee_source requires taker_fee_bps")
        canonical_multiplier = decimal_value(market.get("canonical_multiplier", "1"),
                                             "canonical_multiplier", positive=True)
        economics = market.get("backpack_economics", {})
        if not isinstance(economics, dict):
            raise ScanConfigError("backpack_economics must be a table")
        if venue == "BACKPACK":
            keys = {"margin_init", "margin_maint", "maker_fee", "taker_fee", "source", "source_reference"}
            allowed_keys(economics, keys, "backpack_economics")
            if economics.keys() != keys or economics.get("source") != "Configured":
                raise ScanConfigError("Backpack requires complete Configured public economics")
            for key in ("margin_init", "margin_maint", "taker_fee"):
                decimal_value(economics[key], key, nonnegative=True)
            decimal_value(economics["maker_fee"], "maker_fee")
            if not isinstance(economics["source_reference"], str) or not economics["source_reference"].strip():
                raise ScanConfigError("Backpack economics need source_reference")
            economics = {k: str(v) for k, v in economics.items()}
            if fee is not None and Fraction(fee) != Fraction(Decimal(economics["taker_fee"])) * 10000:
                raise ScanConfigError("Backpack taker fee and economics disagree")
        elif economics:
            raise ScanConfigError("backpack_economics is only valid for BACKPACK")
        markets.append(MarketPlan(symbol, venue, instrument_id,
            decimal_value(market.get("quote_to_usd"), "quote_to_usd", positive=True),
            valuation, fee, source, canonical_multiplier, economics))
    if any(len(group) < 2 for group in groups.values()):
        raise ScanConfigError("every underlying needs at least two venue mappings")
    return ScanPlan(tuple(markets), settings, output_path, cooldown,
        decimal_value(recording.get("edge_change_bps", "2"), "edge_change_bps", positive=True),
        decimal_value(recording.get("quantity_change_fraction", "0.1"), "quantity_change_fraction", positive=True),
        interval, depth, cap,
        integer_value(runtime.get("refresh_ms", 1000), "refresh_ms", 100, 60000),
        integer_value(runtime.get("top_n", 20), "top_n", 1, 1000),
        integer_value(runtime.get("duration_secs", 3600), "duration_secs", 0, 2678400),
        integer_value(runtime.get("connection_timeout_secs", 30), "connection_timeout_secs", 1, 300))


def load_plan(path: Path) -> ScanPlan:
    with path.open("rb") as handle:
        document = tomllib.load(handle, parse_float=Decimal)
    return parse_plan(document, path.parent)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true", help="validate only; no native imports or output files")
    parser.add_argument("--no-record", action="store_true", help="show opportunities without writing them")
    parser.add_argument("--symbols", help="comma-separated subset of configured underlyings")
    parser.add_argument("--target-notional", help="override the buy-side USD budget")
    parser.add_argument("--duration-secs", type=int, help="0 runs until interrupted")
    args = parser.parse_args(argv)
    try:
        plan = load_plan(args.config)
        if args.no_record:
            plan = replace(plan, output_path=None)
        if args.target_notional is not None:
            plan = replace(plan, settings=replace(plan.settings, target_notional=decimal_value(
                args.target_notional, "target_notional", positive=True)))
        if args.duration_secs is not None:
            plan = replace(plan, duration_secs=integer_value(args.duration_secs, "duration_secs", 0, 2678400))
        if args.symbols:
            requested = set(args.symbols.split(","))
            known = {m.symbol for m in plan.markets}
            if not requested <= known or "" in requested:
                raise ScanConfigError("--symbols contains an unconfigured underlying")
            plan = replace(plan, markets=tuple(m for m in plan.markets if m.symbol in requested))
        if args.dry_run:
            print(json.dumps(plan.document(), indent=2))
            return 0
        from opportunity_runtime import run_native
        return run_native(plan)
    except (OSError, ValueError, ImportError, RuntimeError) as exc:
        print(f"[scan] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

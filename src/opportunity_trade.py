#!/usr/bin/env python3
"""Run the multi-venue entry/exit policy in explicitly configured paper or live mode."""
from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass, replace
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import threading
import time
import tomllib

from opportunity_basis import BasisFilterPolicy
from opportunity_scan import (ScanConfigError, ScanPlan, allowed_keys, decimal_value,
                              integer_value, load_plan)
from opportunity_timing import add_timing_arguments, run_timed, timed


ENTRY_SLIPPAGE_VENUES = frozenset({"HL", "LIGHTER", "ASTER", "ONDO", "BACKPACK"})
ENTRY_SLIPPAGE_VENUE_ALIASES = {"HYPERLIQUID": "HL", **{v: v for v in ENTRY_SLIPPAGE_VENUES}}


def _normalize_venue_tuple(raw, field_name: str) -> tuple[str, ...]:
    if not isinstance(raw, (tuple, list)):
        raise ScanConfigError(f"{field_name} must be a sequence of venue names")
    seen: dict[str, str] = {}
    for venue in raw:
        if not isinstance(venue, str) or venue not in ENTRY_SLIPPAGE_VENUE_ALIASES:
            raise ScanConfigError(f"unsupported venue in {field_name}: {venue}")
        canonical = ENTRY_SLIPPAGE_VENUE_ALIASES[venue]
        if canonical in seen and seen[canonical] != venue:
            raise ScanConfigError(f"duplicate venue in {field_name}: {canonical}")
        seen[canonical] = venue
    return tuple(sorted(seen))


def _normalize_venue_bps_map(raw, field_name: str, *, min_bps: Decimal) -> tuple[tuple[str, Decimal], ...]:
    if isinstance(raw, dict):
        entries = list(raw.items())
    elif isinstance(raw, (tuple, list)):
        entries = []
        for item in raw:
            if not isinstance(item, (tuple, list)) or len(item) != 2:
                raise ScanConfigError(f"{field_name} must be a venue-to-bps mapping")
            entries.append((item[0], item[1]))
    else:
        raise ScanConfigError(f"{field_name} must be a venue-to-bps mapping")
    normalized: dict[str, Decimal] = {}
    for venue, value in entries:
        if not isinstance(venue, str) or venue not in ENTRY_SLIPPAGE_VENUE_ALIASES:
            raise ScanConfigError(f"unsupported venue in {field_name}: {venue}")
        canonical = ENTRY_SLIPPAGE_VENUE_ALIASES[venue]
        if canonical in normalized:
            raise ScanConfigError(f"duplicate venue in {field_name}: {canonical}")
        bps = decimal_value(value, f"{field_name}.{canonical}", nonnegative=min_bps == 0)
        if bps < min_bps or bps >= 10000:
            raise ScanConfigError(f"{field_name}.{canonical} must be in [{min_bps}, 10000)")
        normalized[canonical] = bps
    return tuple(sorted(normalized.items()))


@dataclass(frozen=True)
class TradePolicy:
    order_notional_usd: Decimal = Decimal("100")
    entry_edge_bps: Decimal = Decimal("30")
    take_profit_bps: Decimal = Decimal("5")
    max_loss_bps: Decimal = Decimal("20")
    loss_stop_enabled: bool = True
    breakeven_after_secs: int = 180
    force_close_after_secs: int = 300
    slippage_bps: Decimal = Decimal("0")
    entry_slippage_bps_by_venue: tuple[tuple[str, Decimal], ...] = ()
    hard_exit_slippage_bps: Decimal = Decimal("10")
    loss_cooldown_secs: int = 0
    max_exit_reversal_bps: Decimal | None = None
    max_exit_reversal_edge_ratio: Decimal | None = None
    slow_venues: tuple[str, ...] = ()
    slow_pair_entry_edge_bps: Decimal | None = None
    slow_pair_min_persistence_ms: int = 0
    maker_exit_venues: tuple[str, ...] = ()
    maker_exit_timeout_ms: int = 0
    maker_exit_max_attempts: int = 0
    maker_fee_bps_by_venue: tuple[tuple[str, Decimal], ...] = ()

    def __post_init__(self):
        if type(self.loss_stop_enabled) is not bool:
            raise ScanConfigError("loss_stop_enabled must be boolean")
        for name in ("order_notional_usd", "entry_edge_bps", "take_profit_bps", "max_loss_bps"):
            value = decimal_value(getattr(self, name), name, positive=True)
            object.__setattr__(self, name, value)
        for name in ("slippage_bps", "hard_exit_slippage_bps"):
            slip = decimal_value(getattr(self, name), name, nonnegative=True)
            if slip >= 10000:
                raise ScanConfigError(f"{name} must be below 10000")
            object.__setattr__(self, name, slip)
        object.__setattr__(
            self,
            "entry_slippage_bps_by_venue",
            _normalize_venue_bps_map(self.entry_slippage_bps_by_venue, "entry_slippage_bps_by_venue", min_bps=Decimal("0")),
        )
        soft = integer_value(self.breakeven_after_secs, "breakeven_after_secs", 1, 86400)
        hard = integer_value(self.force_close_after_secs, "force_close_after_secs", 1, 86400)
        if soft >= hard:
            raise ScanConfigError("breakeven_after_secs must be below force_close_after_secs")
        integer_value(self.loss_cooldown_secs, "loss_cooldown_secs", 0, 86400)
        if self.max_exit_reversal_bps is not None:
            rev = decimal_value(self.max_exit_reversal_bps, "max_exit_reversal_bps", positive=True)
            if rev >= 10000:
                raise ScanConfigError("max_exit_reversal_bps must be below 10000")
            object.__setattr__(self, "max_exit_reversal_bps", rev)
        if self.max_exit_reversal_edge_ratio is not None:
            ratio = decimal_value(self.max_exit_reversal_edge_ratio, "max_exit_reversal_edge_ratio", positive=True)
            if ratio > 10:
                raise ScanConfigError("max_exit_reversal_edge_ratio must be at most 10")
            object.__setattr__(self, "max_exit_reversal_edge_ratio", ratio)
        object.__setattr__(self, "slow_venues", _normalize_venue_tuple(self.slow_venues, "slow_venues"))
        if self.slow_pair_entry_edge_bps is not None:
            slow_edge = decimal_value(self.slow_pair_entry_edge_bps, "slow_pair_entry_edge_bps", positive=True)
            if slow_edge >= 10000:
                raise ScanConfigError("slow_pair_entry_edge_bps must be below 10000")
            object.__setattr__(self, "slow_pair_entry_edge_bps", slow_edge)
        integer_value(self.slow_pair_min_persistence_ms, "slow_pair_min_persistence_ms", 0, 60000)
        maker_venues = _normalize_venue_tuple(self.maker_exit_venues, "maker_exit_venues")
        object.__setattr__(self, "maker_exit_venues", maker_venues)
        timeout_ms = integer_value(self.maker_exit_timeout_ms, "maker_exit_timeout_ms", 0, 60000)
        max_attempts = integer_value(self.maker_exit_max_attempts, "maker_exit_max_attempts", 0, 20)
        if maker_venues and timeout_ms == 0:
            raise ScanConfigError("maker_exit_venues requires positive maker_exit_timeout_ms")
        if timeout_ms > 0 and not maker_venues:
            raise ScanConfigError("maker_exit_timeout_ms requires maker_exit_venues")
        if max_attempts > 0 and not maker_venues:
            raise ScanConfigError("maker_exit_max_attempts requires maker_exit_venues")
        object.__setattr__(
            self,
            "maker_fee_bps_by_venue",
            _normalize_venue_bps_map(self.maker_fee_bps_by_venue, "maker_fee_bps_by_venue", min_bps=Decimal("-100")),
        )

    def entry_slippage_bps(self, venue: str) -> Decimal:
        canonical = ENTRY_SLIPPAGE_VENUE_ALIASES.get(venue, venue)
        for key, value in self.entry_slippage_bps_by_venue:
            if key == canonical:
                return value
        return self.slippage_bps

    @property
    def maker_exit_enabled(self) -> bool:
        return bool(self.maker_exit_venues and self.maker_exit_timeout_ms > 0)

    def is_maker_exit_venue(self, venue: str) -> bool:
        return ENTRY_SLIPPAGE_VENUE_ALIASES.get(venue, venue) in self.maker_exit_venues

    def maker_fee_bps(self, venue: str) -> Decimal | None:
        canonical = ENTRY_SLIPPAGE_VENUE_ALIASES.get(venue, venue)
        for key, value in self.maker_fee_bps_by_venue:
            if key == canonical:
                return value
        return None

    def is_slow_pair(self, buy_venue: str, sell_venue: str) -> bool:
        if not self.slow_venues:
            return False
        buy_canonical = ENTRY_SLIPPAGE_VENUE_ALIASES.get(buy_venue, buy_venue)
        sell_canonical = ENTRY_SLIPPAGE_VENUE_ALIASES.get(sell_venue, sell_venue)
        return buy_canonical in self.slow_venues or sell_canonical in self.slow_venues

    def effective_entry_edge_bps(self, buy_venue: str, sell_venue: str) -> Decimal:
        if self.slow_pair_entry_edge_bps is not None and self.is_slow_pair(buy_venue, sell_venue):
            return max(self.entry_edge_bps, self.slow_pair_entry_edge_bps)
        return self.entry_edge_bps

    def document(self):
        result = {}
        for key, value in asdict(self).items():
            if key in {"loss_cooldown_secs", "slow_pair_min_persistence_ms",
                       "maker_exit_timeout_ms", "maker_exit_max_attempts"} and value == 0:
                continue
            if key in {"entry_slippage_bps_by_venue", "maker_fee_bps_by_venue"}:
                if not value:
                    continue
                result[key] = {venue: str(bps) for venue, bps in value}
                continue
            if key in {"slow_venues", "maker_exit_venues"}:
                if not value:
                    continue
                result[key] = list(value)
                continue
            if key in {"max_exit_reversal_bps", "max_exit_reversal_edge_ratio", "slow_pair_entry_edge_bps"} and value is None:
                continue
            result[key] = str(value) if isinstance(value, Decimal) else value
        return result


@dataclass(frozen=True)
class TradePlan:
    scan: ScanPlan
    policy: TradePolicy
    state_path: Path
    basis_filter: BasisFilterPolicy = BasisFilterPolicy()

    def fingerprint(self):
        # Runtime duration/display/output settings can change on a restart.
        document = {"version": 2, "policy": self.policy.document(),
                    "markets": self.scan.document()["markets"],
                    "settings": self.scan.document()["settings"]}
        return hashlib.sha256(json.dumps(document, sort_keys=True).encode()).hexdigest()

    def document(self):
        return {"schema_version": 1, "mode": "paper", "runtime_started": False,
                "remote_writes_allowed": False, "registered_execution_clients": 0,
                "simulated_fills": True, "policy": self.policy.document(),
                "basis_filter": self.basis_filter.document(),
                "pair_lock": "symbol + unordered venue pair (both directions)",
                "pnl_bps_denominator": "filled opening buy-leg notional in USD",
                "entry_notional_limit": "each leg; equal canonical base quantity",
                "pnl_excludes": ["funding", "borrow_costs", "execution_latency"],
                "fill_model": "full paired entry/soft exit; independent partial hard exits from available L2",
                "state_path": str(self.state_path), "fingerprint": self.fingerprint(),
                "scan": self.scan.document()}


@timed("config.paper_plan")
def load_trade_plan(path: Path) -> TradePlan:
    path = Path(path).resolve()
    with path.open("rb") as handle:
        document = tomllib.load(handle, parse_float=Decimal)
    allowed_keys(document, {"schema_version", "mode", "scan_config", "state_path", "trade", "basis_filter"},
                 "trade root")
    if type(document.get("schema_version")) is not int or document["schema_version"] != 1:
        raise ScanConfigError("schema_version must be 1")
    if document.get("mode") != "paper":
        raise ScanConfigError("this runner supports paper mode only; no real-order clients are registered")
    for name in ("scan_config", "state_path"):
        if not isinstance(document.get(name), str) or not document[name].strip():
            raise ScanConfigError(f"{name} must be an explicit path")
    values = document.get("trade", {})
    allowed_keys(values, set(TradePolicy.__dataclass_fields__), "trade policy")
    policy = TradePolicy(**values)
    basis_values = document.get("basis_filter", {})
    allowed_keys(basis_values, set(BasisFilterPolicy.__dataclass_fields__), "basis_filter")
    basis_filter = BasisFilterPolicy(**basis_values)
    scan = load_plan(path.parent / document["scan_config"])
    if scan.pair_allowlist is not None:
        raise ScanConfigError("schema_version 2 pair allowlists are observation-only; trading consumers are unsupported")
    scan = replace(scan, output_path=None, settings=replace(scan.settings,
        target_notional=policy.order_notional_usd, min_entry_edge_bps=policy.entry_edge_bps),
        evaluation_interval_ms=100)
    return TradePlan(scan, policy, (path.parent / document["state_path"]).resolve(), basis_filter=basis_filter)


@timed("run.paper")
def run_paper(plan: TradePlan) -> int:
    from opportunity_paper import PaperTradeState
    from opportunity_runtime import build_node
    from opportunity_trade_store import TradeStore

    store = TradeStore(plan.state_path, plan.fingerprint())
    timer = None
    try:
        state = PaperTradeState(plan.scan, plan.policy, store=store, basis_filter=plan.basis_filter)
        node, observer, _ = build_node(plan.scan, state_factory=lambda _: state)
        handle = node.handle()
        if observer.memory_diagnostics is not None:
            observer.memory_diagnostics.attach_handle(handle)
        timer = threading.Timer(plan.scan.duration_secs, handle.stop) if plan.scan.duration_secs else None
        if timer:
            timer.daemon = True
            timer.start()
        try:
            node.run()
        except KeyboardInterrupt:
            handle.stop()
        finally:
            state.finish(time.time_ns())
        print(json.dumps(state.document(), indent=2))
        return 1 if observer.failure or not observer.started or state.positions else 0
    finally:
        if timer:
            timer.cancel()
        store.close()


def main(argv=None) -> int:
    raw_args = list(sys.argv[1:] if argv is None else argv)
    route_parser = argparse.ArgumentParser(add_help=False)
    route_parser.add_argument("--config", type=Path)
    route, _ = route_parser.parse_known_args(raw_args)
    if route.config is not None:
        try:
            with route.config.open("rb") as handle:
                mode = tomllib.load(handle).get("mode")
        except (OSError, ValueError):
            mode = None
        if mode == "live":
            from opportunity_live_runner import main as live_main
            return live_main(raw_args)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--duration-secs", type=int)
    parser.add_argument("--symbols")
    add_timing_arguments(parser)
    from opportunity_memory import add_memory_arguments, run_with_memory
    add_memory_arguments(parser)
    args = parser.parse_args(raw_args)
    if args.memory_tracemalloc and not args.memory_diagnostics:
        parser.error("--memory-tracemalloc requires --memory-diagnostics")
    return run_with_memory(
        lambda: run_timed(lambda: _run(args), enabled=args.timing, directory=args.timing_dir), args)


def _run(args):
    try:
        plan = load_trade_plan(args.config)
        if args.duration_secs is not None:
            plan = replace(plan, scan=replace(plan.scan,
                duration_secs=integer_value(args.duration_secs, "duration_secs", 0, 2678400)))
        if args.symbols:
            requested = set(args.symbols.split(","))
            if not requested <= {m.symbol for m in plan.scan.markets} or "" in requested:
                raise ScanConfigError("--symbols contains an unconfigured underlying")
            plan = replace(plan, scan=replace(plan.scan,
                markets=tuple(m for m in plan.scan.markets if m.symbol in requested)))
        if args.dry_run:
            print(json.dumps(plan.document(), indent=2))
            return 0
        return run_paper(plan)
    except (OSError, ValueError, ImportError, RuntimeError, sqlite3.Error) as exc:
        print(f"[paper-trade] {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

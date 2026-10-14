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

from opportunity_scan import (ScanConfigError, ScanPlan, allowed_keys, decimal_value,
                              integer_value, load_plan)


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

    def __post_init__(self):
        if type(self.loss_stop_enabled) is not bool:
            raise ScanConfigError("loss_stop_enabled must be boolean")
        for name in ("order_notional_usd", "entry_edge_bps", "take_profit_bps", "max_loss_bps"):
            value = decimal_value(getattr(self, name), name, positive=True)
            object.__setattr__(self, name, value)
        slip = decimal_value(self.slippage_bps, "slippage_bps", nonnegative=True)
        if slip >= 10000:
            raise ScanConfigError("slippage_bps must be below 10000")
        object.__setattr__(self, "slippage_bps", slip)
        soft = integer_value(self.breakeven_after_secs, "breakeven_after_secs", 1, 86400)
        hard = integer_value(self.force_close_after_secs, "force_close_after_secs", 1, 86400)
        if soft >= hard:
            raise ScanConfigError("breakeven_after_secs must be below force_close_after_secs")

    def document(self):
        return {key: str(value) if isinstance(value, Decimal) else value
                for key, value in asdict(self).items()}


@dataclass(frozen=True)
class TradePlan:
    scan: ScanPlan
    policy: TradePolicy
    state_path: Path

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
                "pair_lock": "symbol + unordered venue pair (both directions)",
                "pnl_bps_denominator": "filled opening buy-leg notional in USD",
                "entry_notional_limit": "each leg; equal canonical base quantity",
                "pnl_excludes": ["funding", "borrow_costs", "execution_latency"],
                "fill_model": "full paired entry/soft exit; independent partial hard exits from available L2",
                "state_path": str(self.state_path), "fingerprint": self.fingerprint(),
                "scan": self.scan.document()}


def load_trade_plan(path: Path) -> TradePlan:
    path = Path(path).resolve()
    with path.open("rb") as handle:
        document = tomllib.load(handle, parse_float=Decimal)
    allowed_keys(document, {"schema_version", "mode", "scan_config", "state_path", "trade"}, "trade root")
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
    scan = load_plan(path.parent / document["scan_config"])
    if scan.pair_allowlist is not None:
        raise ScanConfigError("schema_version 2 pair allowlists are observation-only; trading consumers are unsupported")
    scan = replace(scan, output_path=None, settings=replace(scan.settings,
        target_notional=policy.order_notional_usd, min_entry_edge_bps=policy.entry_edge_bps),
        evaluation_interval_ms=100)
    return TradePlan(scan, policy, (path.parent / document["state_path"]).resolve())


def run_paper(plan: TradePlan) -> int:
    from opportunity_paper import PaperTradeState
    from opportunity_runtime import build_node
    from opportunity_trade_store import TradeStore

    store = TradeStore(plan.state_path, plan.fingerprint())
    timer = None
    try:
        state = PaperTradeState(plan.scan, plan.policy, store=store)
        node, observer, _ = build_node(plan.scan, state_factory=lambda _: state)
        handle = node.handle()
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
    args = parser.parse_args(raw_args)
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

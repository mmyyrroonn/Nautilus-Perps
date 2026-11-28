"""Strict TOML configuration loader and immutable policy objects for Route A Carry."""
from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import tomllib
from typing import Mapping, Sequence

from .limits import LimitValidationError, clamp_limits, exact_decimal

SUPPORTED_VENUES = frozenset({"HL", "LIGHTER", "OKX", "BYBIT", "BITGET"})
DEX_VENUES = frozenset({"HL", "LIGHTER"})
CEX_VENUES = frozenset({"OKX", "BYBIT", "BITGET"})
ROUTE_SCOPES = frozenset({"ALL", "ALL_4V", "DEX_CEX_ONLY", "DEX_ONLY", "CEX_ONLY"})
ASSET_CLASSES = frozenset({"CRYPTO", "RWA"})
MODES = frozenset({"paper", "readonly", "live"})

_EXTRA_DEFAULT_MAKER_FEE_BPS: dict[str, Decimal] = {"BITGET": Decimal("2.0")}
_EXTRA_DEFAULT_TAKER_FEE_BPS: dict[str, Decimal] = {"BITGET": Decimal("6.0")}


class CarryConfigError(ValueError):
    """Raised on malformed, incomplete, or unknown Carry config values."""


def _check_keys(mapping: dict, allowed: set[str], scope: str) -> None:
    if not isinstance(mapping, dict):
        raise CarryConfigError(f"{scope} must be a TOML table")
    unknown = set(mapping.keys()) - allowed
    if unknown:
        raise CarryConfigError(f"unknown keys in {scope}: {sorted(unknown)}")


def _int_val(val: object, name: str, lo: int, hi: int) -> int:
    if type(val) is not int or not (lo <= val <= hi):
        raise CarryConfigError(f"{name} must be int in [{lo}, {hi}], got {val!r}")
    return val


def _bool_val(val: object, name: str) -> bool:
    if type(val) is not bool:
        raise CarryConfigError(f"{name} must be boolean, got {val!r}")
    return val


@dataclass(frozen=True)
class CarryPolicy:
    route_scope: str = "ALL_4V"
    asset_classes: tuple[str, ...] = ("CRYPTO", "RWA")
    entry_carry_bps_h: Decimal = Decimal("0.75")
    exit_carry_8h_bps_h: Decimal = Decimal("0.25")
    min_hold_h: int = 6
    min_entry_basis_bps: Decimal = Decimal("10")
    min_exec_basis_bps: Decimal = Decimal("10")
    taker_fast_entry_basis_bps: Decimal = Decimal("20")
    max_entry_basis_bps: Decimal = Decimal("60")
    min_24h_vol_usd: Decimal = Decimal("500000")
    max_leg_spread_bps: Decimal = Decimal("25")
    max_price_ratio_deviation: Decimal = Decimal("0.05")
    max_1h_split_divergence: Decimal = Decimal("0.15")
    cex_entry_blackout_before_settle_mins: int = 30
    cex_exit_defer_before_settle_mins: int = 20
    maker_fee_bps_by_venue: tuple[tuple[str, Decimal], ...] = (
        ("BYBIT", Decimal("2.0")),
        ("HL", Decimal("1.5")),
        ("LIGHTER", Decimal("0.0")),
        ("OKX", Decimal("2.0")),
    )
    taker_fee_bps_by_venue: tuple[tuple[str, Decimal], ...] = (
        ("BYBIT", Decimal("5.5")),
        ("HL", Decimal("4.5")),
        ("LIGHTER", Decimal("0.0")),
        ("OKX", Decimal("5.0")),
    )

    def fee_pair_rt_bps(self, v_long: str, v_short: str) -> Decimal:
        mk = {**_EXTRA_DEFAULT_MAKER_FEE_BPS, **dict(self.maker_fee_bps_by_venue)}
        tk = {**_EXTRA_DEFAULT_TAKER_FEE_BPS, **dict(self.taker_fee_bps_by_venue)}
        is_l_dex = v_long in DEX_VENUES
        is_s_dex = v_short in DEX_VENUES
        if is_l_dex and is_s_dex:
            reserve = Decimal("12")
        elif is_l_dex or is_s_dex:
            reserve = Decimal("8")
        else:
            reserve = Decimal("4")
        if is_l_dex != is_s_dex:
            dv = v_long if is_l_dex else v_short
            cv = v_short if is_l_dex else v_long
            one_way = mk[dv] + tk[cv]
        else:
            one_way = min(mk[v_long] + tk[v_short], mk[v_short] + tk[v_long])
        return Decimal("2") * one_way + reserve


@dataclass(frozen=True)
class CarryRiskPolicy:
    max_slots: int = 6
    leg_notional_usd: Decimal = Decimal("100")
    slice_usd: Decimal = Decimal("50")
    max_venue_notional_usd: Decimal = Decimal("400")
    max_gross_notional_usd: Decimal = Decimal("1200")
    basis_stop_bps: Decimal = Decimal("200")
    basis_persist_mins: int = 30
    margin_guard_pct: Decimal = Decimal("25")
    max_unhedged_usd: Decimal = Decimal("60")
    max_unhedged_secs: int = 10
    max_daily_loss_usd: Decimal = Decimal("40")
    max_consecutive_unhedged: int = 3
    stop_loss_cooldown_hours: int = 24
    max_qty_mismatch_pct: Decimal = Decimal("2.0")
    reconcile_tolerance_pct: Decimal = Decimal("2.0")


@dataclass(frozen=True)
class CarryRuntimePolicy:
    slow_poll_secs: int = 60
    fast_risk_poll_secs: int = 1
    basis_sample_secs: int = 10
    quote_stale_secs: int = 15
    watch_top_k: int = 15
    unsubscribe_hysteresis_secs: int = 600
    maker_requote_secs: int = 15
    maker_max_attempts: int = 4
    hedge_ioc_slippage_bps: Decimal = Decimal("15")
    update_instruments_interval_mins: int = 60
    duration_secs: int = 0
    record_dir: Path | None = None
    bbo_record_interval_secs: int = 1
    watch_fill_min_carry_bps_h: Decimal = Decimal("0.35")


def ledger_domain_for_mode(mode: str) -> str:
    """Map runtime mode to an isolated ledger domain ('paper' vs 'live')."""
    if mode == "paper":
        return "paper"
    if mode in ("live", "readonly"):
        return "live"
    raise ValueError(f"unsupported mode: {mode!r}")


@dataclass(frozen=True)
class CarryPlan:
    schema_version: int
    mode: str
    environment: str
    state_path: Path
    venues: tuple[str, ...]
    excluded_symbols: tuple[str, ...]
    policy: CarryPolicy
    risk: CarryRiskPolicy
    runtime: CarryRuntimePolicy

    def _fingerprint_payload(self, *, include_domain: bool) -> str:
        def _plain(obj: object) -> object:
            if isinstance(obj, (Decimal, Path)):
                return str(obj)
            if isinstance(obj, tuple):
                return [_plain(x) for x in obj]
            if isinstance(obj, dict):
                return {k: _plain(v) for k, v in obj.items()}
            if hasattr(obj, "__dataclass_fields__"):
                return {k: _plain(getattr(obj, k)) for k in obj.__dataclass_fields__}
            return obj

        doc = _plain(asdict(self))
        mode = doc.pop("mode")
        if include_domain:
            # Isolate paper vs live/readonly ledger domains while allowing live <-> readonly sharing.
            doc["ledger_domain"] = ledger_domain_for_mode(mode)
        doc["runtime"].pop("duration_secs", None)
        doc["runtime"].pop("record_dir", None)
        doc["runtime"].pop("bbo_record_interval_secs", None)
        doc["runtime"].pop("watch_fill_min_carry_bps_h", None)
        raw = json.dumps(doc, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def fingerprint(self) -> str:
        return self._fingerprint_payload(include_domain=True)

    def legacy_fingerprint(self) -> str:
        """Pre-ledger_domain fingerprint (6b47a19) used to verify legacy ledger migrations."""
        return self._fingerprint_payload(include_domain=False)


def _parse_fee_map(
    raw: object,
    name: str,
    defaults: tuple[tuple[str, Decimal], ...],
    *,
    extra_defaults: Mapping[str, Decimal] | None = None,
    active_venues: Sequence[str] = (),
) -> tuple[tuple[str, Decimal], ...]:
    merged = dict(defaults)
    if extra_defaults:
        for venue in active_venues:
            if venue in extra_defaults and venue not in merged:
                merged[venue] = extra_defaults[venue]
    if raw is not None:
        if not isinstance(raw, dict):
            raise CarryConfigError(f"{name} must be a table")
        for k, v in raw.items():
            if k not in SUPPORTED_VENUES:
                raise CarryConfigError(f"unsupported venue in {name}: {k}")
            merged[k] = exact_decimal(v, f"{name}.{k}", nonnegative=True)
    return tuple(sorted(merged.items()))


def load_carry_plan(config_path: Path) -> CarryPlan:
    """Parse and validate TOML config without connecting to any venue or reading .env."""
    path = Path(config_path).resolve()
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise CarryConfigError(f"failed to load {path}: {exc}") from exc

    _check_keys(
        raw,
        {"schema_version", "mode", "environment", "state_path", "venues", "excluded_symbols", "policy", "risk", "runtime"},
        "root",
    )
    schema = _int_val(raw.get("schema_version"), "schema_version", 1, 1)
    mode = raw.get("mode", "paper")
    if mode not in MODES:
        raise CarryConfigError(f"mode must be one of {sorted(MODES)}, got {mode!r}")
    env = raw.get("environment", "mainnet")
    if env not in {"mainnet", "testnet"}:
        raise CarryConfigError(f"environment must be mainnet or testnet, got {env!r}")
    sp_raw = raw.get("state_path")
    if not isinstance(sp_raw, str) or not sp_raw.strip():
        raise CarryConfigError("state_path is required")
    state_path = (path.parent / sp_raw).resolve() if not Path(sp_raw).is_absolute() else Path(sp_raw)

    venues_raw = raw.get("venues", ["HL", "LIGHTER", "OKX", "BYBIT"])
    if not isinstance(venues_raw, list) or len(venues_raw) < 2:
        raise CarryConfigError("venues must list at least 2 venues")
    venues_norm = []
    for v in venues_raw:
        if v not in SUPPORTED_VENUES:
            raise CarryConfigError(f"unsupported venue: {v!r}")
        if v not in venues_norm:
            venues_norm.append(v)

    excl_raw = raw.get("excluded_symbols", ["ACE", "INIT", "MEME", "SNDK"])
    if not isinstance(excl_raw, list) or any(not isinstance(s, str) or not s.strip() for s in excl_raw):
        raise CarryConfigError("excluded_symbols must be a list of non-empty strings")
    excluded_symbols = tuple(sorted({s.strip().upper() for s in excl_raw}))

    pol_raw = raw.get("policy", {})
    _check_keys(
        pol_raw,
        {
            "route_scope", "asset_classes", "entry_carry_bps_h", "exit_carry_8h_bps_h",
            "min_hold_h", "min_entry_basis_bps", "min_exec_basis_bps", "taker_fast_entry_basis_bps",
            "max_entry_basis_bps", "min_24h_vol_usd",
            "max_leg_spread_bps", "max_price_ratio_deviation", "max_1h_split_divergence",
            "cex_entry_blackout_before_settle_mins", "cex_exit_defer_before_settle_mins",
            "maker_fee_bps_by_venue", "taker_fee_bps_by_venue",
        },
        "policy",
    )
    route_scope = pol_raw.get("route_scope", "ALL_4V")
    if route_scope not in ROUTE_SCOPES:
        raise CarryConfigError(f"route_scope must be one of {sorted(ROUTE_SCOPES)}")
    ac_raw = pol_raw.get("asset_classes", ["CRYPTO", "RWA"])
    if not isinstance(ac_raw, list) or not ac_raw or any(a not in ASSET_CLASSES for a in ac_raw):
        raise CarryConfigError(f"asset_classes must be non-empty subset of {sorted(ASSET_CLASSES)}")

    risk_raw = raw.get("risk", {})
    _check_keys(
        risk_raw,
        {
            "max_slots", "leg_notional_usd", "slice_usd", "max_venue_notional_usd",
            "max_gross_notional_usd", "basis_stop_bps", "basis_persist_mins",
            "margin_guard_pct", "max_unhedged_usd", "max_unhedged_secs",
            "max_daily_loss_usd", "max_consecutive_unhedged", "stop_loss_cooldown_hours",
            "max_qty_mismatch_pct", "reconcile_tolerance_pct",
        },
        "risk",
    )

    try:
        clamped = clamp_limits(
            max_slots=risk_raw.get("max_slots", 6),
            leg_notional_usd=risk_raw.get("leg_notional_usd", "100"),
            slice_usd=risk_raw.get("slice_usd", "50"),
            max_venue_notional_usd=risk_raw.get("max_venue_notional_usd", "400"),
            max_gross_notional_usd=risk_raw.get("max_gross_notional_usd", "1200"),
            max_unhedged_usd=risk_raw.get("max_unhedged_usd", "60"),
            max_unhedged_secs=risk_raw.get("max_unhedged_secs", 10),
            max_daily_loss_usd=risk_raw.get("max_daily_loss_usd", "40"),
            margin_guard_pct=risk_raw.get("margin_guard_pct", "25"),
            entry_carry_bps_h=pol_raw.get("entry_carry_bps_h", "0.75"),
        )
    except LimitValidationError as exc:
        raise CarryConfigError(str(exc)) from exc

    min_eb = exact_decimal(pol_raw.get("min_entry_basis_bps", "10"), "policy.min_entry_basis_bps")
    max_eb = exact_decimal(pol_raw.get("max_entry_basis_bps", "60"), "policy.max_entry_basis_bps", positive=True)
    if min_eb >= max_eb:
        raise CarryConfigError("policy.min_entry_basis_bps must be < policy.max_entry_basis_bps")

    min_exec_b = exact_decimal(pol_raw.get("min_exec_basis_bps", "10"), "policy.min_exec_basis_bps")
    if min_exec_b >= max_eb:
        raise CarryConfigError("policy.min_exec_basis_bps must be < policy.max_entry_basis_bps")

    taker_fast_b = exact_decimal(
        pol_raw.get("taker_fast_entry_basis_bps", "20"),
        "policy.taker_fast_entry_basis_bps",
        nonnegative=True,
    )
    if taker_fast_b > 0 and taker_fast_b > max_eb:
        raise CarryConfigError("policy.taker_fast_entry_basis_bps must be <= policy.max_entry_basis_bps")

    exit_c = exact_decimal(pol_raw.get("exit_carry_8h_bps_h", "0.25"), "policy.exit_carry_8h_bps_h", nonnegative=True)
    if exit_c >= clamped.min_entry_carry_bps_h:
        raise CarryConfigError("policy.exit_carry_8h_bps_h must be < clamped entry_carry_bps_h")

    policy = CarryPolicy(
        route_scope=route_scope,
        asset_classes=tuple(sorted(set(ac_raw))),
        entry_carry_bps_h=clamped.min_entry_carry_bps_h,
        exit_carry_8h_bps_h=exit_c,
        min_hold_h=_int_val(pol_raw.get("min_hold_h", 6), "policy.min_hold_h", 1, 72),
        min_entry_basis_bps=min_eb,
        min_exec_basis_bps=min_exec_b,
        taker_fast_entry_basis_bps=taker_fast_b,
        max_entry_basis_bps=max_eb,
        min_24h_vol_usd=exact_decimal(pol_raw.get("min_24h_vol_usd", "500000"), "policy.min_24h_vol_usd", nonnegative=True),
        max_leg_spread_bps=exact_decimal(pol_raw.get("max_leg_spread_bps", "25"), "policy.max_leg_spread_bps", positive=True),
        max_price_ratio_deviation=exact_decimal(pol_raw.get("max_price_ratio_deviation", "0.05"), "policy.max_price_ratio_deviation", positive=True),
        max_1h_split_divergence=exact_decimal(pol_raw.get("max_1h_split_divergence", "0.15"), "policy.max_1h_split_divergence", positive=True),
        cex_entry_blackout_before_settle_mins=_int_val(pol_raw.get("cex_entry_blackout_before_settle_mins", 30), "policy.cex_entry_blackout_before_settle_mins", 0, 120),
        cex_exit_defer_before_settle_mins=_int_val(pol_raw.get("cex_exit_defer_before_settle_mins", 20), "policy.cex_exit_defer_before_settle_mins", 0, 120),
        maker_fee_bps_by_venue=_parse_fee_map(
            pol_raw.get("maker_fee_bps_by_venue"),
            "policy.maker_fee_bps_by_venue",
            CarryPolicy.maker_fee_bps_by_venue,
            extra_defaults=_EXTRA_DEFAULT_MAKER_FEE_BPS,
            active_venues=venues_norm,
        ),
        taker_fee_bps_by_venue=_parse_fee_map(
            pol_raw.get("taker_fee_bps_by_venue"),
            "policy.taker_fee_bps_by_venue",
            CarryPolicy.taker_fee_bps_by_venue,
            extra_defaults=_EXTRA_DEFAULT_TAKER_FEE_BPS,
            active_venues=venues_norm,
        ),
    )

    risk = CarryRiskPolicy(
        max_slots=clamped.max_slots,
        leg_notional_usd=clamped.leg_notional_usd,
        slice_usd=clamped.slice_usd,
        max_venue_notional_usd=clamped.max_venue_notional_usd,
        max_gross_notional_usd=clamped.max_gross_notional_usd,
        basis_stop_bps=exact_decimal(risk_raw.get("basis_stop_bps", "200"), "risk.basis_stop_bps", positive=True),
        basis_persist_mins=_int_val(risk_raw.get("basis_persist_mins", 30), "risk.basis_persist_mins", 5, 240),
        margin_guard_pct=clamped.margin_guard_pct,
        max_unhedged_usd=clamped.max_unhedged_usd,
        max_unhedged_secs=clamped.max_unhedged_secs,
        max_daily_loss_usd=clamped.max_daily_loss_usd,
        max_consecutive_unhedged=_int_val(risk_raw.get("max_consecutive_unhedged", 3), "risk.max_consecutive_unhedged", 1, 20),
        stop_loss_cooldown_hours=_int_val(risk_raw.get("stop_loss_cooldown_hours", 24), "risk.stop_loss_cooldown_hours", 0, 168),
        max_qty_mismatch_pct=exact_decimal(risk_raw.get("max_qty_mismatch_pct", "2.0"), "risk.max_qty_mismatch_pct", positive=True),
        reconcile_tolerance_pct=exact_decimal(risk_raw.get("reconcile_tolerance_pct", "2.0"), "risk.reconcile_tolerance_pct", positive=True),
    )

    rt_raw = raw.get("runtime", {})
    _check_keys(
        rt_raw,
        {
            "slow_poll_secs", "fast_risk_poll_secs", "basis_sample_secs", "quote_stale_secs",
            "watch_top_k", "unsubscribe_hysteresis_secs", "maker_requote_secs", "maker_max_attempts",
            "hedge_ioc_slippage_bps", "update_instruments_interval_mins", "duration_secs",
            "record_dir", "bbo_record_interval_secs", "watch_fill_min_carry_bps_h",
        },
        "runtime",
    )
    watch_fill_min = exact_decimal(
        rt_raw.get("watch_fill_min_carry_bps_h", "0.35"),
        "runtime.watch_fill_min_carry_bps_h",
        nonnegative=True,
    )
    if watch_fill_min > Decimal("5.0"):
        raise CarryConfigError(f"runtime.watch_fill_min_carry_bps_h must be <= 5.0, got {watch_fill_min}")
    rd_raw = rt_raw.get("record_dir")
    if rd_raw is None:
        record_dir: Path | None = None
    elif not isinstance(rd_raw, str):
        raise CarryConfigError(f"runtime.record_dir must be string or omitted, got {rd_raw!r}")
    elif not rd_raw.strip():
        record_dir = None
    else:
        record_dir = (path.parent / rd_raw).resolve() if not Path(rd_raw).is_absolute() else Path(rd_raw)
    runtime = CarryRuntimePolicy(
        slow_poll_secs=_int_val(rt_raw.get("slow_poll_secs", 60), "runtime.slow_poll_secs", 10, 600),
        fast_risk_poll_secs=_int_val(rt_raw.get("fast_risk_poll_secs", 1), "runtime.fast_risk_poll_secs", 1, 10),
        basis_sample_secs=_int_val(rt_raw.get("basis_sample_secs", 10), "runtime.basis_sample_secs", 1, 60),
        quote_stale_secs=_int_val(rt_raw.get("quote_stale_secs", 15), "runtime.quote_stale_secs", 3, 120),
        watch_top_k=_int_val(rt_raw.get("watch_top_k", 15), "runtime.watch_top_k", 1, 60),
        unsubscribe_hysteresis_secs=_int_val(rt_raw.get("unsubscribe_hysteresis_secs", 600), "runtime.unsubscribe_hysteresis_secs", 60, 3600),
        maker_requote_secs=_int_val(rt_raw.get("maker_requote_secs", 15), "runtime.maker_requote_secs", 3, 120),
        maker_max_attempts=_int_val(rt_raw.get("maker_max_attempts", 4), "runtime.maker_max_attempts", 1, 20),
        hedge_ioc_slippage_bps=exact_decimal(rt_raw.get("hedge_ioc_slippage_bps", "15"), "runtime.hedge_ioc_slippage_bps", positive=True),
        update_instruments_interval_mins=_int_val(rt_raw.get("update_instruments_interval_mins", 60), "runtime.update_instruments_interval_mins", 5, 1440),
        duration_secs=_int_val(rt_raw.get("duration_secs", 0), "runtime.duration_secs", 0, 31_536_000),
        record_dir=record_dir,
        bbo_record_interval_secs=_int_val(rt_raw.get("bbo_record_interval_secs", 1), "runtime.bbo_record_interval_secs", 1, 60),
        watch_fill_min_carry_bps_h=watch_fill_min,
    )

    return CarryPlan(
        schema_version=schema,
        mode=mode,
        environment=env,
        state_path=state_path,
        venues=tuple(venues_norm),
        excluded_symbols=excluded_symbols,
        policy=policy,
        risk=risk,
        runtime=runtime,
    )

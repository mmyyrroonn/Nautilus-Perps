"""Credential-free plans for explicitly synthetic local native order scenarios."""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext
import tomllib
from urllib.parse import urlsplit

from backpack_config import (
    BackpackConfigError, BackpackSessionPlan, MAX_CONFIG_BYTES,
    _decimal, _integer, _keys, _text, parse_plan,
)


def _flag(value, label):
    if type(value) is not bool:
        raise BackpackConfigError(f"{label} must be an explicit boolean")
    return value


def _positive(value, label):
    result = _decimal(value, label)
    if result <= 0:
        raise BackpackConfigError(f"{label} must be positive")
    return result


@dataclass(frozen=True)
class LoopbackAuthority:
    valid_for_ms: int
    max_account_age_ms: int
    max_market_age_ms: int
    max_order_notional: Decimal
    max_reserved_notional: Decimal
    max_reserved_margin: Decimal
    max_unsettled_orders: int
    allow_new_risk: bool
    allow_reduction: bool
    allow_owned_cancel: bool

    @classmethod
    def parse(cls, value, duration_ms):
        fields = set(cls.__dataclass_fields__)
        _keys(value, fields, fields, "loopback authority")
        result = cls(*(_integer(value[k], 1, duration_ms, k) for k in (
            "valid_for_ms", "max_account_age_ms", "max_market_age_ms")),
            *(_positive(value[k], k) for k in (
                "max_order_notional", "max_reserved_notional", "max_reserved_margin")),
            _integer(value["max_unsettled_orders"], 1, 16, "max_unsettled_orders"),
            *(_flag(value[k], k) for k in (
                "allow_new_risk", "allow_reduction", "allow_owned_cancel")))
        if result.max_reserved_notional < result.max_order_notional:
            raise BackpackConfigError("reserved notional must cover the single-order limit")
        return result

    def document(self):
        return {k: format(v, "f") if isinstance(v, Decimal) else v
                for k, v in vars(self).items()}


@dataclass(frozen=True)
class SyntheticFacts:
    available_margin: Decimal
    margin_per_notional: Decimal
    fee_buffer_per_notional: Decimal
    economics_reference: str
    net_positions: tuple[tuple[str, Decimal], ...]
    auto_borrow: bool
    auto_lend: bool
    auto_repay: bool
    liquidating: bool
    complete: bool

    @classmethod
    def parse(cls, value, symbols):
        fields = set(cls.__dataclass_fields__)
        _keys(value, fields, fields, "synthetic account facts")
        positions = value["net_positions"]
        ids = [f"{symbol}.BACKPACK" for symbol in symbols]
        if not isinstance(positions, dict) or set(positions) != set(ids):
            raise BackpackConfigError("synthetic positions must cover exactly the full instrument allowlist")
        result = cls(_positive(value["available_margin"], "available_margin"),
            _decimal(value["margin_per_notional"], "margin_per_notional"),
            _decimal(value["fee_buffer_per_notional"], "fee_buffer_per_notional"),
            _text(value["economics_reference"], "synthetic economics reference"),
            tuple((i, _decimal(positions[i], "synthetic net position")) for i in ids),
            *(_flag(value[k], k) for k in ("auto_borrow", "auto_lend", "auto_repay", "liquidating", "complete")))
        if (not 0 <= result.margin_per_notional <= 1
                or not 0 <= result.fee_buffer_per_notional <= 1
                or len(result.economics_reference) > 256
                or result.auto_borrow or result.auto_lend or result.auto_repay or result.liquidating
                or not result.complete):
            raise BackpackConfigError("unsupported or incomplete synthetic account facts")
        if any(quantity != 0 for _, quantity in result.net_positions):
            raise BackpackConfigError("this bounded entry scenario requires explicitly flat synthetic initial positions")
        return result

    def document(self):
        return {k: ({i: format(q, "f") for i, q in v} if k == "net_positions"
                    else format(v, "f") if isinstance(v, Decimal) else v)
                for k, v in vars(self).items()}


@dataclass(frozen=True)
class LoopbackScenario:
    symbol: str
    quantity: Decimal
    limit_price: Decimal
    cancel_after_fill: bool
    stale_probe: bool
    reassert_flat_before_stale_probe: bool
    stale_probe_delay_ms: int

    @classmethod
    def parse(cls, value, session, authority, facts):
        fields = set(cls.__dataclass_fields__)
        _keys(value, fields, fields, "loopback scenario")
        result = cls(_text(value["symbol"], "scenario symbol"),
            _positive(value["quantity"], "scenario quantity"),
            _positive(value["limit_price"], "scenario limit price"),
            _flag(value["cancel_after_fill"], "cancel_after_fill"),
            _flag(value["stale_probe"], "stale_probe"),
            _flag(value["reassert_flat_before_stale_probe"], "reassert_flat_before_stale_probe"),
            _integer(value["stale_probe_delay_ms"], 1, session.duration_secs * 1000, "stale_probe_delay_ms"))
        if result.symbol not in session.symbols or not authority.allow_new_risk:
            raise BackpackConfigError("scenario requires allowlisted new-risk Buy Limit permission")
        if result.cancel_after_fill and not authority.allow_owned_cancel:
            raise BackpackConfigError("cancel scenario requires explicit owned-cancel permission")
        with localcontext() as context:
            context.prec = 90
            notional = result.quantity * result.limit_price
            margin = notional * (facts.margin_per_notional + facts.fee_buffer_per_notional)
            orders = 2 if result.stale_probe else 1
            if (notional > authority.max_order_notional
                    or notional * orders > authority.max_reserved_notional
                    or margin * orders > min(authority.max_reserved_margin, facts.available_margin)
                    or authority.max_unsettled_orders < orders):
                raise BackpackConfigError("scenario exceeds finite order, notional or margin permission")
        if result.stale_probe and not result.reassert_flat_before_stale_probe:
            raise BackpackConfigError("stale probe requires an explicit current synthetic flat assertion")
        if result.stale_probe and (
                result.stale_probe_delay_ms <= session.stale_after_ms
                or result.stale_probe_delay_ms + session.request_timeout_secs * 1000 >= min(
                    authority.valid_for_ms, authority.max_account_age_ms, authority.max_market_age_ms,
                    session.duration_secs * 1000)):
            raise BackpackConfigError("stale probe must leave fresh account facts, authority and runtime budget")
        return result

    def document(self):
        return {**{k: format(v, "f") if isinstance(v, Decimal) else v
                   for k, v in vars(self).items()},
                "side": "BUY", "order_type": "LIMIT", "time_in_force": "GTC",
                "maximum_submit_attempts": 2 if self.stale_probe else 1,
                "maximum_cancel_attempts": 1 if self.cancel_after_fill else 0}


@dataclass(frozen=True)
class BackpackLoopbackPlan:
    session: BackpackSessionPlan
    authority: LoopbackAuthority
    facts: SyntheticFacts
    scenario: LoopbackScenario
    mutation_budget_ms: int
    receive_window_ms: int
    durable_economics: bool = False
    recovery_only: bool = False

    def __getattr__(self, name):
        return getattr(self.session, name)

    @property
    def mode(self):
        return "loopback-execution"

    @property
    def output_dir(self):
        return self.session.output_root / self.namespace / self.mode

    @property
    def economic_state_directory(self):
        return self.journal_dir / "economic-consumer" if self.durable_economics else None

    def document(self):
        return {**self.session.document(), "mode": self.mode, "output_dir": str(self.output_dir),
            "authority": self.authority.document(), "synthetic_account": self.facts.document(),
            "scenario": self.scenario.document(), "mutation_budget_ms": self.mutation_budget_ms,
            "receive_window_ms": self.receive_window_ms,
            "durable_economics": self.durable_economics, "recovery_only": self.recovery_only,
            "economic_state_directory": (str(self.economic_state_directory)
                if self.economic_state_directory is not None else None),
            "synthetic_assertion_timestamp": "local time at first admitted session; not venue evidence",
            "authority_expiry": "anchored once at runtime construction; never renewed",
            "loopback_mutations_requested": True, "production_writes_supported": False,
            "durable_economic_acknowledgement": False}


def parse_loopback_plan(document, base_dir):
    fields = {"schema_version", "mode", "session", "authority", "synthetic_account", "scenario",
              "mutation_budget_ms", "receive_window_ms"}
    _keys(document, fields | {"durable_economics", "recovery_only"}, fields, "loopback execution plan")
    if type(document["schema_version"]) is not int or document["schema_version"] != 1:
        raise BackpackConfigError("unsupported loopback schema_version")
    if document["mode"] != "loopback-execution":
        raise BackpackConfigError("explicit loopback-execution mode is required")
    session = parse_plan(document["session"], base_dir)
    if session.mode != "account-readonly" or session.environment != "loopback":
        raise BackpackConfigError("guarded mutations require a numeric loopback account session")
    http, ws = urlsplit(session.http_origin), urlsplit(session.ws_origin)
    if http.hostname != ws.hostname or (http.scheme, ws.scheme) not in {("http", "ws"), ("https", "wss")}:
        raise BackpackConfigError("HTTP and WS must use the same numeric loopback host and paired schemes")
    if any(value.source != "Synthetic" for _, value in session.economics):
        raise BackpackConfigError("local execution requires explicitly Synthetic instrument economics")
    if session.max_report_bytes < 8192:
        raise BackpackConfigError("loopback report budget must reserve at least 8 KiB for native stop evidence")
    authority = LoopbackAuthority.parse(document["authority"], session.duration_secs * 1000)
    facts = SyntheticFacts.parse(document["synthetic_account"], session.symbols)
    scenario = LoopbackScenario.parse(document["scenario"], session, authority, facts)
    mutation = _integer(document["mutation_budget_ms"], 1, session.request_timeout_secs * 1000, "mutation_budget_ms")
    window = _integer(document["receive_window_ms"], 1, 60_000, "receive_window_ms")
    durable = _flag(document.get("durable_economics", False), "durable_economics")
    recovery = _flag(document.get("recovery_only", False), "recovery_only")
    if recovery and not durable:
        raise BackpackConfigError("recovery_only requires durable_economics")
    return BackpackLoopbackPlan(session, authority, facts, scenario, mutation, window, durable, recovery)


def load_loopback_plan(path):
    if path.name == ".env" or path.name.startswith(".env."):
        raise BackpackConfigError("dotenv files are not session configuration")
    with path.open("rb") as stream:
        payload = stream.read(MAX_CONFIG_BYTES + 1)
    if len(payload) > MAX_CONFIG_BYTES:
        raise BackpackConfigError("configuration exceeds 64 KiB")
    try:
        document = tomllib.loads(payload.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError):
        raise BackpackConfigError("invalid configuration encoding or TOML") from None
    return parse_loopback_plan(document, path.absolute().parent)

#!/usr/bin/env python3
"""Offline acceptance tests for ``src/ondo_trade_probe.py`` (app trade orchestration).

Everything here runs without a credential, a socket, a live venue or the repository's
``.env``.  Where the tests exercise the full CLI they inject the adapter, the node factory
and the environment through ``main``'s keyword seams; where they exercise the sequencing
they drive the pure :class:`TradeSequencer` directly, so no Nautilus runtime is needed.

The contract the tests pin:

* a live run is refused *before any credential is read* when the installed adapter does not
  expose the production write-envelope contract (the R52 wheel does not);
* a dry run constructs no client, reads no ``.env`` and opens no socket, and reports
  ``native_write_capable: false`` honestly;
* the plan is hash-bound, and a widened / malformed / missing-authorization plan is refused;
* the envelope can only tighten the module hard caps (50 USD per order, 100 USD gross,
  3 orders, 600 s);
* one entry limit-IOC and at most two reduce-only closes for the *confirmed* quantity; a
  duplicate or out-of-order fill is not double-counted; an unknown submission stops new
  sends and is never replaced; a residual position is ``uncertain``, never clean.

    .venv\\Scripts\\python.exe -m pytest tests/test_ondo_trade_probe.py -q -p no:cacheprovider
"""

from __future__ import annotations

import contextlib
import datetime
import enum
import io
import json
import os
import subprocess
import sys
import threading
import time
from decimal import Decimal
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ondo_trade_probe as trade  # noqa: E402
from ondo_trade_limits import TradeLimitsError, load_trade_limits  # noqa: E402
from nautilus_trader.common import Cache  # noqa: E402
from nautilus_trader.core import UUID4  # noqa: E402
from nautilus_trader.model import (  # noqa: E402
    AccountId,
    ClientOrderId,
    Currency,
    CryptoPerpetual,
    InstrumentId,
    LimitOrder,
    LiquiditySide,
    Money,
    OrderAccepted,
    OrderFilled,
    OrderRejected,
    OrderSide,
    OrderSubmitted,
    OrderType,
    Price,
    Quantity,
    StrategyId,
    Symbol,
    TimeInForce,
    TradeId,
    TraderId,
    VenueOrderId,
)


# --------------------------------------------------------------------------- plan helper


def resolved_plan(**overrides) -> dict:
    """A structurally valid, hash-bound plan document (not yet user-authorized)."""
    document = {
        "plan_version": 1,
        "instrument": {
            "symbol": "NVDA",
            "instrument_id": "NVDA-USD-PERP.ONDO",
            "price_increment": "0.01",
            "size_increment": "0.001",
            "min_quantity": None,
            "min_quantity_source": "unpublished",
            "min_notional": None,
            "min_notional_currency": None,
            "min_notional_source": "unpublished",
            "max_quantity": "100000",
            "quote_currency": "USD",
        },
        "entry": {
            "side": "buy",
            "order_type": "limit",
            "time_in_force": "IOC",
            "reduce_only": False,
            "quantity": "0.100",
            "limit_price": "150.00",
            "max_slippage_bps": "20",
            "notional_usd": "15.00",
            "max_quote_age_secs": "5",
        },
        "close": {
            "side": "sell",
            "order_type": "limit",
            "time_in_force": "IOC",
            "reduce_only": True,
            "limit_price": "90.00",
            "max_close_attempts": 2,
            "max_quote_age_secs": "5",
        },
        "envelope": {
            "max_notional_per_order_usd": "50",
            "max_gross_exposure_usd": "100",
            "max_orders": 3,
            "max_new_risk_requests": 1,
            "max_app_requests": 6,
            "min_available_margin_usdc": "25",
            "deadline_secs": 600,
            "require_flat_start": True,
        },
        "account": {
            "environment": "production",
            "journal_path": "reports/ondo-trade/journal-run.json",
            "expected_venue_account_id_present": True,
        },
        "dms": {
            "mode": "trading",
            "activation": "subscribe cancelAllOrdersAfterPerps on login",
            "timeout_secs": 30,
            "renewal_interval_secs": 15,
            "renewal_message_verified": False,
            "release_on_stop": "only when nothing is unconfirmed",
            "account_wide_effect": "a lapsed timer cancels every resting order account-wide",
        },
        "cleanup": {
            "cancel_own_orders_only": True,
            "confirm_cancels": True,
            "confirm_flat_position": True,
            "stop_budget_secs": 5,
        },
        "authorization": {
            "user_phrase_上主网_present": False,
            "approved_plan_sha256": None,
        },
        "plan_sha256": None,
    }
    _deep_update(document, overrides)
    rebind_hashes(document)
    return document


def _deep_update(document: dict, overrides: dict) -> None:
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            _deep_update(document[key], value)
        else:
            document[key] = value


def rebind_hashes(document: dict) -> None:
    digest = trade.plan_sha256(document)
    document["plan_sha256"] = digest
    document.setdefault("authorization", {})["approved_plan_sha256"] = digest


def parse(document: dict, limits=None) -> trade.TradePlan:
    return trade.parse_trade_plan(document, limits or load_trade_limits(None))


def authorized(document: dict) -> dict:
    document["authorization"]["user_phrase_上主网_present"] = True
    rebind_hashes(document)
    return document


def write_plan(tmp_path: Path, document: dict) -> Path:
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    return path


def write_explicit_limits(tmp_path: Path) -> Path:
    path = tmp_path / "limits.toml"
    path.write_text(
        "[ondo_trade]\n"
        'instrument = "NVDA-USD-PERP.ONDO"\n'
        'symbol = "NVDA"\n'
        'entry_notional_usd = "15"\n'
        'max_notional_per_order_usd = "50"\n'
        'max_gross_exposure_usd = "100"\n'
        "max_orders = 3\n"
        "max_new_risk_requests = 1\n"
        "max_app_requests = 6\n"
        'min_available_margin_usdc = "25"\n'
        "deadline_secs = 600\n"
        "max_close_attempts = 2\n"
        'max_slippage_bps = "20"\n'
        "cleanup_budget_secs = 5\n",
        encoding="utf-8",
    )
    return path


def seq_for(document: dict | None = None, limits=None) -> trade.TradeSequencer:
    plan = parse(document or resolved_plan(), limits)
    return trade.TradeSequencer(plan, limits or load_trade_limits(None))


# ------------------------------------------------------------------------------- doubles


class _Environment(enum.Enum):
    PRODUCTION = "production"
    SANDBOX = "sandbox"


class CapableEnvelope:
    """A stand-in for the proposed ``OndoExecutionEnvelopeConfig``."""

    def __init__(self, instrument_id=None, entry_side=None, entry_max_quantity=None,
                 entry_worst_price=None, entry_max_notional_usd=None, close_side=None,
                 close_max_quantity=None, close_worst_price=None, max_close_attempts=None,
                 max_notional_per_order_usd=None, max_gross_exposure_usd=None,
                 max_orders=None, max_new_risk_requests=None, max_app_requests=None,
                 min_available_margin_usdc=None,
                 entry_deadline_unix_nanos=None, cleanup_deadline_unix_nanos=None,
                 require_flat_start=None) -> None:
        self.instrument_id = instrument_id
        self.entry_side = entry_side
        self.entry_max_quantity = entry_max_quantity
        self.entry_worst_price = entry_worst_price
        self.entry_max_notional_usd = entry_max_notional_usd
        self.close_side = close_side
        self.close_max_quantity = close_max_quantity
        self.close_worst_price = close_worst_price
        self.max_close_attempts = max_close_attempts
        self.max_notional_per_order_usd = max_notional_per_order_usd
        self.max_gross_exposure_usd = max_gross_exposure_usd
        self.max_orders = max_orders
        self.max_new_risk_requests = max_new_risk_requests
        self.max_app_requests = max_app_requests
        self.min_available_margin_usdc = min_available_margin_usdc
        self.entry_deadline_unix_nanos = entry_deadline_unix_nanos
        self.cleanup_deadline_unix_nanos = cleanup_deadline_unix_nanos
        self.require_flat_start = require_flat_start


class CapableConfig:
    instances: list = []

    def __init__(self, environment=None, account_id=None, api_key=None, api_secret=None,
                 base_url_http=None, base_url_ws=None, account_read_only=None,
                 journal_path=None, execution_envelope=None, expected_venue_account_id=None,
                 diagnostics_run_id=None, dms_timeout_secs=None,
                 reconcile_interval_secs=None, allow_production_orders=None, **kwargs) -> None:
        self.environment = environment
        self.account_id = account_id
        self.base_url_http = base_url_http
        self.base_url_ws = base_url_ws
        self.account_read_only = account_read_only
        self.journal_path = journal_path
        self.execution_envelope = execution_envelope
        self.expected_venue_account_id = expected_venue_account_id
        self.diagnostics_run_id = diagnostics_run_id
        self.dms_timeout_secs = dms_timeout_secs
        self.reconcile_interval_secs = reconcile_interval_secs
        self.allow_production_orders = allow_production_orders
        CapableConfig.instances.append(self)


class CapableFactory:
    supports_production_trade_envelope = True

    def production_trade_snapshot(self):  # pragma: no cover - capability presence only
        return {}

    def name(self) -> str:
        return "ONDO"


class CapableDataConfig:
    def __init__(self, environment=None, load_ids=None, **kwargs) -> None:
        self.environment = environment
        self.load_ids = load_ids


class CapableDataFactory:
    def name(self) -> str:
        return "ONDO"


class CapableAdapter:
    OndoEnvironment = _Environment
    OndoExecutionEnvelopeConfig = CapableEnvelope
    OndoExecutionClientConfig = CapableConfig
    OndoExecutionClientFactory = CapableFactory
    OndoDataClientConfig = CapableDataConfig
    OndoDataClientFactory = CapableDataFactory


class OldAdapter:
    """The installed R52 shape: no envelope class, no marker, no snapshot."""

    OndoEnvironment = _Environment

    class OndoExecutionClientConfig:
        def __init__(self, environment=None, account_id=None, api_key=None, api_secret=None,
                     base_url_http=None, base_url_ws=None, account_read_only=None,
                     http_timeout_secs=None, dms_timeout_secs=None,
                     dms_max_failed_renewals=None, reconcile_interval_secs=None,
                     journal_path=None, allow_production_orders=None, **kwargs) -> None:
            pass

    class OndoExecutionClientFactory:
        def name(self) -> str:
            return "ONDO"


class RecordingNode:
    def __init__(self) -> None:
        self.added: list[object] = []
        self.run_calls = 0
        self.cache = None

    def add_strategy(self, strategy) -> None:
        self.added.append(strategy)

    def run(self):
        self.run_calls += 1
        return None

    def handle(self):
        return None

    @property
    def is_running(self) -> bool:
        return False


class NodeFactory:
    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.nodes: list[RecordingNode] = []

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        node = RecordingNode()
        self.nodes.append(node)
        return node


# --------------------------------------------------------------------------------- limits


def test_limits_defaults_are_inside_every_hard_cap(tmp_path):
    limits = load_trade_limits(tmp_path / "missing-limits.toml")
    assert limits.entry_notional_usd == Decimal("15")
    assert limits.max_notional_per_order_usd == Decimal("50")
    assert limits.max_gross_exposure_usd == Decimal("100")
    assert limits.max_orders == 3
    assert limits.max_new_risk_requests == 1
    assert limits.max_app_requests == 6
    assert limits.deadline_secs == 600
    assert limits.max_close_attempts == 2
    assert limits.cleanup_budget_secs == 5


def test_canonical_limits_section_is_complete_for_offline_implementation():
    limits = load_trade_limits(None)
    assert limits.limits_configured is True
    assert limits.missing_execution_keys == ()
    assert limits.entry_notional_usd == Decimal("15")
    assert limits.max_new_risk_requests == 1
    assert limits.max_app_requests == 6
    assert limits.max_orders == 3
    assert limits.max_close_attempts == 2
    assert limits.min_available_margin_usdc == Decimal("25")
    assert limits.cleanup_budget_secs == 15


def test_implicit_limits_are_never_configured_for_execution(tmp_path):
    limits = load_trade_limits(tmp_path / "missing-limits.toml")
    assert limits.limits_configured is False
    assert limits.missing_execution_keys


def test_live_execution_refuses_implicit_limits_before_credentials(tmp_path, monkeypatch):
    called = {"env": False}

    def forbidden(*_args, **_kwargs):
        called["env"] = True
        raise AssertionError("implicit safety limits must refuse before credentials")

    monkeypatch.setattr(trade, "_load_environment", forbidden)
    plan_path = write_plan(tmp_path, authorized(resolved_plan()))
    missing_limits = tmp_path / "missing-limits.toml"
    with contextlib.redirect_stderr(io.StringIO()):
        code = trade.main(
            ["--mode", "production-trade", "--plan", str(plan_path),
             "--confirm-production-trade", "--acknowledge-mainnet-authorization",
             "--limits", str(missing_limits)],
            adapter=CapableAdapter(), environ={},
        )
    assert code == 2
    assert called["env"] is False


def test_a_dedicated_section_is_read_and_can_only_tighten(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text(
        "[ondo_trade]\n"
        'instrument = "NVDA-USD-PERP.ONDO"\n'
        'entry_notional_usd = "12"\n'
        'max_notional_per_order_usd = "30"\n'
        'max_gross_exposure_usd = "40"\n'
        "max_orders = 3\n"
        "deadline_secs = 120\n"
        'max_slippage_bps = "10"\n',
        encoding="utf-8",
    )
    limits = load_trade_limits(path)
    assert limits.entry_notional_usd == Decimal("12")
    assert limits.max_notional_per_order_usd == Decimal("30")
    assert limits.max_gross_exposure_usd == Decimal("40")
    assert limits.deadline_secs == 120
    assert limits.max_slippage_bps == Decimal("10")


def test_a_value_above_the_hard_cap_is_clamped_not_widened(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text(
        "[ondo_trade]\n"
        'entry_notional_usd = "999"\n'
        'max_notional_per_order_usd = "999"\n'
        'max_gross_exposure_usd = "999"\n'
        "max_orders = 99\n"
        "deadline_secs = 99999\n",
        encoding="utf-8",
    )
    limits = load_trade_limits(path)
    assert limits.entry_notional_usd == Decimal("20")
    assert limits.max_notional_per_order_usd == Decimal("50")
    assert limits.max_gross_exposure_usd == Decimal("100")
    assert limits.max_orders == 3
    assert limits.max_new_risk_requests == 1
    assert limits.max_app_requests == 6
    assert limits.deadline_secs == 600


def test_the_maker_order_table_is_never_read(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text(
        "[order]\nmax_notional_usd = 20.0\n"
        "[ondo_trade]\nentry_notional_usd = \"11\"\n",
        encoding="utf-8",
    )
    limits = load_trade_limits(path)
    assert limits.entry_notional_usd == Decimal("11")


def test_a_missing_file_falls_back_to_defaults(tmp_path):
    limits = load_trade_limits(tmp_path / "absent.toml")
    assert limits.entry_notional_usd == Decimal("15")
    assert limits.source.startswith("defaults")


def test_a_non_positive_value_is_refused(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text('[ondo_trade]\nentry_notional_usd = "0"\n', encoding="utf-8")
    with pytest.raises(TradeLimitsError):
        load_trade_limits(path)


def test_a_float_value_is_refused_for_exactness(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text("[ondo_trade]\nentry_notional_usd = 12.0\n", encoding="utf-8")
    with pytest.raises(TradeLimitsError):
        load_trade_limits(path)


def test_an_entry_below_the_target_floor_is_refused(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text('[ondo_trade]\nentry_notional_usd = "5"\n', encoding="utf-8")
    with pytest.raises(TradeLimitsError):
        load_trade_limits(path)


def test_malformed_toml_is_refused(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text("[ondo_trade\nentry_notional_usd = ", encoding="utf-8")
    with pytest.raises(TradeLimitsError):
        load_trade_limits(path)


def test_a_per_order_cap_below_the_target_is_refused(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text(
        '[ondo_trade]\nentry_notional_usd = "20"\nmax_notional_per_order_usd = "15"\n',
        encoding="utf-8",
    )
    with pytest.raises(TradeLimitsError):
        load_trade_limits(path)


def test_the_order_budget_must_cover_entry_plus_close_attempts(tmp_path):
    path = tmp_path / "limits.toml"
    path.write_text("[ondo_trade]\nmax_orders = 2\n", encoding="utf-8")
    with pytest.raises(TradeLimitsError):
        load_trade_limits(path)


# ----------------------------------------------------------------------------- capability


def test_the_old_wheel_is_unsupported_and_says_why():
    capability = trade.detect_native_trade_capability(OldAdapter())
    assert capability.supported is False
    assert capability.source == trade.CAPABILITY_SOURCE_ENVELOPE_ABSENT
    assert trade.NATIVE_ENVELOPE_CLASS in capability.reason


def test_a_capable_adapter_is_supported():
    capability = trade.detect_native_trade_capability(CapableAdapter())
    assert capability.supported is True
    assert capability.source == trade.CAPABILITY_SOURCE_NATIVE


def test_real_loader_carries_the_installed_trade_envelope():
    adapter = trade.load_adapter()
    assert callable(adapter.OndoExecutionEnvelopeConfig)
    capability = trade.detect_native_trade_capability(adapter)
    assert capability.supported is True
    assert capability.source == trade.CAPABILITY_SOURCE_NATIVE


def test_no_adapter_is_unsupported():
    assert trade.detect_native_trade_capability(None).supported is False


def test_a_marker_that_is_not_true_fails_closed():
    class Factory(CapableFactory):
        supports_production_trade_envelope = "yes"

    class Adapter(CapableAdapter):
        OndoExecutionClientFactory = Factory

    capability = trade.detect_native_trade_capability(Adapter())
    assert capability.supported is False
    assert capability.source == trade.CAPABILITY_SOURCE_MARKER_TYPE


def test_a_marker_false_fails_closed():
    class Factory(CapableFactory):
        supports_production_trade_envelope = False

    class Adapter(CapableAdapter):
        OndoExecutionClientFactory = Factory

    capability = trade.detect_native_trade_capability(Adapter())
    assert capability.supported is False
    assert capability.source == trade.CAPABILITY_SOURCE_MARKER_FALSE


def test_an_envelope_with_the_wrong_shape_fails_closed():
    class Envelope:
        def __init__(self, instrument_id=None) -> None:
            pass

    class Adapter(CapableAdapter):
        OndoExecutionEnvelopeConfig = Envelope

    capability = trade.detect_native_trade_capability(Adapter())
    assert capability.supported is False
    assert capability.source == trade.CAPABILITY_SOURCE_ENVELOPE_SHAPE


def test_an_envelope_without_the_approved_order_bounds_fails_closed():
    class Envelope:
        def __init__(self, instrument_id=None, max_notional_per_order_usd=None,
                     max_gross_exposure_usd=None, max_orders=None,
                     max_new_risk_requests=None, max_app_requests=None,
                     entry_deadline_unix_nanos=None, cleanup_deadline_unix_nanos=None,
                     require_flat_start=None) -> None:
            pass

    class Adapter(CapableAdapter):
        OndoExecutionEnvelopeConfig = Envelope

    capability = trade.detect_native_trade_capability(Adapter())
    assert capability.supported is False
    assert capability.source == trade.CAPABILITY_SOURCE_ENVELOPE_SHAPE
    assert "entry_worst_price" in capability.missing
    assert "close_worst_price" in capability.missing


def test_a_config_without_the_envelope_field_fails_closed():
    class Config:
        def __init__(self, environment=None) -> None:
            pass

    class Adapter(CapableAdapter):
        OndoExecutionClientConfig = Config

    capability = trade.detect_native_trade_capability(Adapter())
    assert capability.supported is False
    assert capability.source == trade.CAPABILITY_SOURCE_CONFIG_FIELD


def test_a_factory_without_the_snapshot_fails_closed():
    class Factory:
        supports_production_trade_envelope = True

    class Adapter(CapableAdapter):
        OndoExecutionClientFactory = Factory

    capability = trade.detect_native_trade_capability(Adapter())
    assert capability.supported is False
    assert capability.source == trade.CAPABILITY_SOURCE_SNAPSHOT_ABSENT


# ---------------------------------------------------------------------------------- plan


def test_a_valid_plan_parses_and_is_hash_bound():
    document = authorized(resolved_plan())
    plan = parse(document)
    assert plan.plan_sha256 == document["plan_sha256"]
    assert plan.entry.quantity == Decimal("0.100")
    assert plan.instrument.instrument_id == "NVDA-USD-PERP.ONDO"


def test_a_close_without_a_frozen_price_bound_is_refused():
    document = resolved_plan()
    document["close"].pop("limit_price")
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError, match="limit_price"):
        parse(document)


def test_the_plan_no_longer_claims_an_unobservable_total_http_budget():
    document = resolved_plan()
    rebind_hashes(document)
    plan = parse(document)
    assert not hasattr(plan.envelope, "max_total_requests")


def test_a_tampered_plan_fails_the_hash():
    document = authorized(resolved_plan())
    document["entry"]["limit_price"] = "200.00"
    # The embedded hashes still bind the old body.
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_rebound_but_invalid_plan_is_refused_by_validation():
    document = resolved_plan()
    document["entry"]["notional_usd"] = "25.00"
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_non_tick_price_is_refused():
    document = resolved_plan()
    document["entry"]["limit_price"] = "150.003"
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_non_tick_quantity_is_refused():
    document = resolved_plan()
    document["entry"]["quantity"] = "0.1005"
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_unpublished_standalone_minima_are_nullable_with_provenance():
    document = resolved_plan()
    document["instrument"]["min_quantity"] = None
    document["instrument"]["min_quantity_source"] = "unpublished"
    document["instrument"]["min_notional"] = None
    document["instrument"]["min_notional_currency"] = None
    document["instrument"]["min_notional_source"] = "unpublished"
    rebind_hashes(document)
    plan = parse(document)
    assert plan.instrument.min_quantity is None
    assert plan.instrument.min_notional is None
    assert plan.instrument.min_quantity_source == "unpublished"
    assert plan.instrument.min_notional_source == "unpublished"


def test_zero_entry_quantity_is_refused_even_when_minimum_is_unpublished():
    document = resolved_plan()
    document["instrument"]["min_quantity"] = None
    document["instrument"]["min_quantity_source"] = "unpublished"
    document["instrument"]["min_notional"] = None
    document["instrument"]["min_notional_currency"] = None
    document["instrument"]["min_notional_source"] = "unpublished"
    document["entry"]["quantity"] = "0"
    document["entry"]["notional_usd"] = "15"
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError, match="quantity"):
        parse(document)


def test_an_order_below_a_genuinely_published_minimum_is_refused():
    document = resolved_plan()
    document["instrument"]["min_notional"] = "20"
    document["instrument"]["min_notional_currency"] = "USD"
    document["instrument"]["min_notional_source"] = "native-metadata"
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError, match="min_notional"):
        parse(document)


def test_notional_outside_the_target_band_is_refused():
    document = resolved_plan()
    document["entry"]["notional_usd"] = "25.00"
    document["entry"]["quantity"] = "0.160"
    document["entry"]["limit_price"] = "150.00"
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_an_envelope_over_the_hard_cap_is_refused():
    document = resolved_plan()
    document["envelope"]["max_gross_exposure_usd"] = "150"
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_deadline_over_600s_is_refused():
    document = resolved_plan()
    document["envelope"]["deadline_secs"] = 601
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_close_that_is_not_reduce_only_is_refused():
    document = resolved_plan()
    document["close"]["reduce_only"] = False
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_close_on_the_same_side_is_refused():
    document = resolved_plan()
    document["close"]["side"] = "buy"
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_missing_dms_facts_are_refused():
    document = resolved_plan()
    document["dms"]["account_wide_effect"] = ""
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_non_flat_start_is_refused():
    document = resolved_plan()
    document["envelope"]["require_flat_start"] = False
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_float_decimal_is_refused():
    document = resolved_plan()
    document["entry"]["quantity"] = 0.1
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_plan_without_user_authorization_is_refused():
    document = resolved_plan()  # user_phrase false
    plan = parse(document)
    problems = trade.authorization_problems(plan)
    assert problems and "user_phrase" in problems[0]


def test_an_approved_plan_passes_authorization():
    plan = parse(authorized(resolved_plan()))
    assert trade.authorization_problems(plan) == []


# ------------------------------------------------------------------------------ readiness


def good_readiness(**overrides) -> trade.TradeReadiness:
    values = {
        "available": True,
        "source": "native-start-snapshot",
        "reason": "ok",
        "generation": 1,
        "snapshot_unix_nanos": 100,
        "identity_match": "matched",
        "account_flat": True,
        "coverage_complete": True,
        "foreign_open_orders": 0,
        "own_open_orders": 0,
        "native_ready": True,
        "metadata_fresh": True,
        "trading_enabled": True,
        "underlying_market_closed": False,
        "dms_verified": True,
        "available_margin_usdc": Decimal("30"),
    }
    values.update(overrides)
    return trade.TradeReadiness(**values)


def clean_final(**overrides) -> trade.FinalReconciliation:
    values = {
        "available": True,
        "source": "native-final-snapshot",
        "reason": "ok",
        "phase": "final",
        "generation": 3,
        "snapshot_unix_nanos": 300,
        "latest_activity_generation": 2,
        "latest_activity_unix_nanos": 200,
        "reconciled_activity_generation": 2,
        "reconciliation_unix_nanos": 250,
        "ordered_after_start": True,
        "covers_latest_activity": True,
        "instrument_matches": True,
        "follows_reconciliation": True,
        "complete": True,
        "snapshot_fresh": True,
        "reconciled_flat": True,
        "instrument_id": "NVDA-USD-PERP.ONDO",
        "position_qty": Decimal("0"),
        "own_open_orders": 0,
        "foreign_open_orders": 0,
        "unknown_submissions": 0,
        "shutdown_status": "clean",
        "late_fills": 0,
        "execution_cost": None,
    }
    values.update(overrides)
    return trade.FinalReconciliation(**values)


def clean_reconciliation(**overrides) -> trade.FinalReconciliation:
    values = {
        "available": True,
        "source": "native-reconciled-snapshot",
        "reason": "ok",
        "phase": "reconciled",
        "generation": 2,
        "snapshot_unix_nanos": 200,
        "latest_activity_generation": 2,
        "latest_activity_unix_nanos": 180,
        "reconciled_activity_generation": 2,
        "reconciliation_unix_nanos": 190,
        "ordered_after_start": True,
        "covers_latest_activity": True,
        "instrument_matches": True,
        "follows_reconciliation": None,
        "complete": True,
        "snapshot_fresh": True,
        "instrument_id": "NVDA-USD-PERP.ONDO",
        "position_qty": Decimal("0"),
        "reconciled_flat": True,
        "own_open_orders": 0,
        "foreign_open_orders": 0,
        "unknown_submissions": 0,
        "shutdown_status": None,
        "late_fills": 0,
        "execution_cost": None,
    }
    values.update(overrides)
    return trade.FinalReconciliation(**values)


def test_a_fully_satisfied_readiness_has_no_blockers():
    plan = parse(resolved_plan())
    assert trade.readiness_blockers(good_readiness(), plan) == []


@pytest.mark.parametrize("overrides", [
    {"identity_match": "mismatch"},
    {"identity_match": None},
    {"account_flat": False},
    {"account_flat": None},
    {"coverage_complete": None},
    {"coverage_complete": False},
    {"foreign_open_orders": 1},
    {"foreign_open_orders": None},
    {"own_open_orders": 2},
    {"native_ready": None},
    {"metadata_fresh": None},
    {"trading_enabled": False},
    {"trading_enabled": None},
    {"underlying_market_closed": True},
    {"underlying_market_closed": None},
    {"dms_verified": None},
    {"available_margin_usdc": None},
    {"available_margin_usdc": Decimal("24")},
])
def test_an_unsatisfied_or_unknown_start_gate_refuses(overrides):
    plan = parse(resolved_plan())
    assert trade.readiness_blockers(good_readiness(**overrides), plan)


def test_an_unavailable_snapshot_refuses():
    plan = parse(resolved_plan())
    readiness = trade.TradeReadiness(False, "unavailable", "no accessor")
    assert trade.readiness_blockers(readiness, plan)


class SnapshotTarget:
    def __init__(self, snapshot) -> None:
        self._snapshot = snapshot

    def production_trade_snapshot(self):
        return self._snapshot


def test_a_matching_readiness_snapshot_is_read():
    target = SnapshotTarget({
        "run_id": "run-1", "phase": "start", "generation": 1,
        "snapshot_unix_nanos": 100, "identity_match": "matched", "account_flat": True,
        "coverage_complete": True, "foreign_open_orders": 0, "own_open_orders": 0,
        "native_ready": True, "metadata_fresh": True, "trading_enabled": True,
        "underlying_market_closed": False, "dms_verified": True,
        "available_margin_usdc": "30",
    })
    readiness = trade.read_trade_readiness(target, run_id="run-1")
    assert readiness.available is True
    assert readiness.identity_match == "matched"
    assert readiness.available_margin_usdc == Decimal("30")
    assert readiness.generation == 1
    assert readiness.snapshot_unix_nanos == 100


def test_legacy_usd_margin_key_never_satisfies_the_usdc_gate():
    target = SnapshotTarget({
        "run_id": "run-1", "phase": "start", "generation": 1,
        "snapshot_unix_nanos": 100, "identity_match": "matched", "account_flat": True,
        "coverage_complete": True, "foreign_open_orders": 0, "own_open_orders": 0,
        "native_ready": True, "metadata_fresh": True, "trading_enabled": True,
        "underlying_market_closed": False, "dms_verified": True,
        "available_margin_usd": "1000000",
    })
    readiness = trade.read_trade_readiness(target, run_id="run-1")
    assert readiness.available_margin_usdc is None
    assert trade.readiness_blockers(readiness, parse(resolved_plan()))


@pytest.mark.parametrize("raw", [None, "not-a-number", "NaN", "-1"])
def test_missing_or_malformed_usdc_margin_blocks(raw):
    snapshot = {
        "run_id": "run-1", "phase": "start", "generation": 1,
        "snapshot_unix_nanos": 100, "identity_match": "matched", "account_flat": True,
        "coverage_complete": True, "foreign_open_orders": 0, "own_open_orders": 0,
        "native_ready": True, "metadata_fresh": True, "trading_enabled": True,
        "underlying_market_closed": False, "dms_verified": True,
        "available_margin_usdc": raw,
    }
    readiness = trade.read_trade_readiness(SnapshotTarget(snapshot), run_id="run-1")
    assert trade.readiness_blockers(readiness, parse(resolved_plan()))


def test_plan_cannot_lower_the_configured_usdc_margin_threshold():
    document = resolved_plan(envelope={"min_available_margin_usdc": "24"})
    with pytest.raises(trade.TradePlanError, match="USDC threshold"):
        parse(document)


def test_a_reconciliation_must_cover_the_latest_native_activity():
    target = SnapshotTarget({
        "run_id": "run-1", "phase": "reconciled", "generation": 2,
        "snapshot_unix_nanos": 200, "latest_activity_generation": 4,
        "latest_activity_unix_nanos": 190, "reconciled_activity_generation": 3,
        "reconciliation_unix_nanos": 200, "complete": True, "snapshot_fresh": True,
        "instrument_id": "NVDA-USD-PERP.ONDO", "position_qty": "0",
        "reconciled_flat": True, "own_open_orders": 0, "foreign_open_orders": 0,
        "unknown_submissions": 0, "late_fills": 0,
    })
    reconciliation = trade.read_trade_reconciliation(
        target, run_id="run-1", instrument_id="NVDA-USD-PERP.ONDO",
        start_generation=1, start_snapshot_unix_nanos=100,
    )
    assert trade.reconciliation_is_clean(reconciliation) is False


def test_a_final_snapshot_must_follow_reconciliation_and_be_cleanly_stopped():
    target = SnapshotTarget({
        "run_id": "run-1", "phase": "final", "generation": 3,
        "snapshot_unix_nanos": 300, "latest_activity_generation": 2,
        "latest_activity_unix_nanos": 200, "reconciled_activity_generation": 2,
        "reconciliation_unix_nanos": 250, "complete": True, "snapshot_fresh": True,
        "instrument_id": "NVDA-USD-PERP.ONDO", "position_qty": "0",
        "reconciled_flat": True, "own_open_orders": 0, "foreign_open_orders": 0,
        "unknown_submissions": 0, "late_fills": 0, "shutdown_status": "clean",
    })
    final = trade.read_trade_final(
        target, run_id="run-1", instrument_id="NVDA-USD-PERP.ONDO",
        start_generation=1, start_snapshot_unix_nanos=100,
        reconciliation=clean_reconciliation(),
    )
    assert trade.final_is_clean(final) is True


@pytest.mark.parametrize("final_overrides", [
    {"snapshot_unix_nanos": 199},
    {"latest_activity_generation": 1, "reconciled_activity_generation": 1},
    {"latest_activity_unix_nanos": 150, "reconciliation_unix_nanos": 150},
    {
        "latest_activity_generation": 3,
        "reconciled_activity_generation": 2,
        "latest_activity_unix_nanos": 280,
        "reconciliation_unix_nanos": 250,
    },
])
def test_a_final_snapshot_cannot_regress_or_leave_later_activity_unreconciled(
    final_overrides,
):
    reconciled = trade.read_trade_reconciliation(
        SnapshotTarget({
            "run_id": "run-1", "phase": "reconciled", "generation": 2,
            "snapshot_unix_nanos": 200, "latest_activity_generation": 2,
            "latest_activity_unix_nanos": 180, "reconciled_activity_generation": 2,
            "reconciliation_unix_nanos": 190, "complete": True, "snapshot_fresh": True,
            "instrument_id": "NVDA-USD-PERP.ONDO", "position_qty": "0",
            "reconciled_flat": True, "own_open_orders": 0, "foreign_open_orders": 0,
            "unknown_submissions": 0, "late_fills": 0,
        }),
        run_id="run-1", instrument_id="NVDA-USD-PERP.ONDO",
        start_generation=1, start_snapshot_unix_nanos=100,
    )
    final_snapshot = {
        "run_id": "run-1", "phase": "final", "generation": 3,
        "snapshot_unix_nanos": 300, "latest_activity_generation": 2,
        "latest_activity_unix_nanos": 180, "reconciled_activity_generation": 2,
        "reconciliation_unix_nanos": 190, "complete": True, "snapshot_fresh": True,
        "instrument_id": "NVDA-USD-PERP.ONDO", "position_qty": "0",
        "reconciled_flat": True, "own_open_orders": 0, "foreign_open_orders": 0,
        "unknown_submissions": 0, "late_fills": 0, "shutdown_status": "clean",
    }
    final_snapshot.update(final_overrides)
    final = trade.read_trade_final(
        SnapshotTarget(final_snapshot), run_id="run-1",
        instrument_id="NVDA-USD-PERP.ONDO", start_generation=1,
        start_snapshot_unix_nanos=100, reconciliation=reconciled,
    )
    assert trade.final_is_clean(final) is False


def test_a_cross_run_readiness_snapshot_is_rejected():
    target = SnapshotTarget({"run_id": "other-run", "identity_match": "matched"})
    readiness = trade.read_trade_readiness(target, run_id="run-1")
    assert readiness.available is False


def test_a_snapshot_without_a_token_is_rejected():
    readiness = trade.read_trade_readiness(SnapshotTarget({}), run_id="run-1")
    assert readiness.available is False


def test_a_start_snapshot_without_an_explicit_phase_is_rejected():
    target = SnapshotTarget({
        "run_id": "run-1", "generation": 1, "snapshot_unix_nanos": 100,
        "identity_match": "matched",
    })
    readiness = trade.read_trade_readiness(target, run_id="run-1")
    assert readiness.available is False


def test_a_target_without_the_accessor_is_unavailable():
    readiness = trade.read_trade_readiness(object(), run_id="run-1")
    assert readiness.available is False


# ------------------------------------------------------------------------------ sequencer


def test_a_full_entry_fill_requests_one_reduce_only_close():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    decision = sequencer.decide_entry(now=0.0)
    assert decision is not None
    assert decision.side == "buy"
    assert decision.reduce_only is False
    assert decision.quantity == Decimal("0.100")
    assert decision.limit_price == Decimal("100.30")
    sequencer.bind_entry("E1")
    close = sequencer.note_entry(trade.OrderView(
        "entry", "E1", "FILLED", Decimal("0.100"), Decimal("100.30"), True,
    ), now=0.0)
    assert close is not None
    assert close.side == "sell"
    assert close.reduce_only is True
    assert close.quantity == Decimal("0.100")
    assert close.attempt == 1


def test_a_partial_entry_closes_only_the_confirmed_quantity():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    sequencer.decide_entry(now=0.0)
    sequencer.bind_entry("E1")
    close = sequencer.note_entry(trade.OrderView(
        "entry", "E1", "CANCELED", Decimal("0.040"), Decimal("100.30"), True,
    ), now=0.0)
    assert close is not None
    assert close.quantity == Decimal("0.040")


def test_a_zero_fill_entry_is_no_trade_and_never_closes():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    sequencer.decide_entry(now=0.0)
    sequencer.bind_entry("E1")
    assert sequencer.note_entry(trade.OrderView(
        "entry", "E1", "CANCELED", Decimal("0"), None, True,
    ), now=0.0) is None
    assert sequencer.outcome == trade.OUTCOME_NO_TRADE
    assert sequencer.done is True
    assert sequencer.close_client_order_ids == []


def test_a_rejected_entry_is_rejected_with_no_close():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    sequencer.decide_entry(now=0.0)
    sequencer.bind_entry("E1")
    assert sequencer.note_entry(trade.OrderView(
        "entry", "E1", "REJECTED", Decimal("0"), None, True, "venue said no",
    ), now=0.0) is None
    assert sequencer.outcome == trade.OUTCOME_REJECTED
    assert sequencer.terminal_reason == "entry rejected: venue said no"
    assert sequencer.close_client_order_ids == []

    document = trade.trade_report_document(
        run_id="r", started=datetime.datetime.now(datetime.timezone.utc),
        finished=datetime.datetime.now(datetime.timezone.utc),
        exit_code=1, failure=None, plan=sequencer.plan,
        limits=sequencer.limits,
        capability=trade.detect_native_trade_capability(CapableAdapter()),
        readiness=good_readiness(), strategy=FakeStrategy(sequencer),
        stop_condition="run-returned", stops=[], watchdog=None, diagnostics=None,
    )
    assert document["terminal_reason"] == "entry rejected: venue said no"


def test_a_close_that_does_not_fill_is_retried_within_the_budget_then_uncertain():
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    second = sequencer.note_close(trade.OrderView(
        "close", "C1", "CANCELED", Decimal("0.040"), Decimal("100.00"), True,
    ), now=0.0)
    assert second is not None and second.attempt == 2
    sequencer.bind_close("C2")
    assert sequencer.note_close(trade.OrderView(
        "close", "C2", "CANCELED", Decimal("0.020"), Decimal("100.00"), True,
    ), now=0.0) is None
    assert sequencer.outcome == trade.OUTCOME_UNCERTAIN
    assert sequencer.net_position == Decimal("0.040")


def test_a_flat_close_reports_filled():
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    assert sequencer.note_close(trade.OrderView(
        "close", "C1", "FILLED", Decimal("0.100"), Decimal("100.00"), True,
    ), now=0.0) is None
    assert sequencer.outcome == trade.OUTCOME_FILLED
    assert sequencer.net_position == Decimal("0")


def test_a_partial_entry_that_closes_flat_reports_partial():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    sequencer.decide_entry(now=0.0)
    sequencer.bind_entry("E1")
    sequencer.note_entry(trade.OrderView(
        "entry", "E1", "CANCELED", Decimal("0.040"), Decimal("100.30"), True,
    ), now=0.0)
    sequencer.bind_close("C1")
    assert sequencer.note_close(trade.OrderView(
        "close", "C1", "FILLED", Decimal("0.040"), Decimal("100.00"), True,
    ), now=0.0) is None
    assert sequencer.outcome == trade.OUTCOME_PARTIAL
    assert sequencer.net_position == Decimal("0")


def test_an_unknown_submission_stops_and_is_never_replaced():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    sequencer.decide_entry(now=0.0)
    sequencer.bind_entry("E1")
    sequencer.note_submit_error(trade.ROLE_ENTRY, "transport reset")
    assert sequencer.outcome == trade.OUTCOME_UNCERTAIN
    assert sequencer.entry_client_order_id == "E1"
    assert sequencer.close_client_order_ids == []


def test_a_duplicate_fill_is_not_double_counted():
    sequencer = seq_for()
    fill = trade.FillRecord("entry", "E1", "T1", Decimal("0.050"), Decimal("100.30"))
    sequencer.note_fill(fill)
    sequencer.note_fill(fill)
    assert len(sequencer.fills) == 1
    assert sequencer.duplicate_fills == 1
    assert sequencer.late_fills == 0


def test_a_late_fill_invalidates_a_clean_result():
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    sequencer.note_close(trade.OrderView(
        "close", "C1", "FILLED", Decimal("0.100"), Decimal("100.00"), True,
    ), now=0.0)
    assert sequencer.outcome == trade.OUTCOME_FILLED
    # A non-duplicate fill after the run finished is late and must not be ignored.
    sequencer.note_fill(trade.FillRecord("entry", "E1", "T-LATE", Decimal("0.010"),
                                         Decimal("100.30")))
    assert sequencer.late_fills == 1
    strategy = FakeStrategy(sequencer)
    acceptance = trade.acceptance_document(
        strategy, sequencer.plan, trade.detect_native_trade_capability(CapableAdapter()),
        good_readiness(), clean_final(),
    )
    assert acceptance["no_late_fills"] is False
    assert acceptance["clean"] is False


def test_a_late_duplicate_terminal_event_is_idempotent():
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    view = trade.OrderView("close", "C1", "FILLED", Decimal("0.100"), Decimal("100.00"), True)
    sequencer.note_close(view, now=0.0)
    assert sequencer.note_close(view, now=0.0) is None
    assert sequencer.outcome == trade.OUTCOME_FILLED


def test_a_stale_quote_is_no_trade():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    assert sequencer.decide_entry(now=10.0) is None
    assert sequencer.outcome == trade.OUTCOME_NO_TRADE


def test_a_close_is_not_priced_from_a_stale_quote_and_wakes_on_a_fresh_one():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    sequencer.decide_entry(now=0.0)
    sequencer.bind_entry("E1")
    # Entry fills, but the quote is now stale (close.max_quote_age_secs = 5).
    decision = sequencer.note_entry(trade.OrderView(
        "entry", "E1", "FILLED", Decimal("0.100"), Decimal("100.30"), True,
    ), now=10.0)
    assert decision is None
    assert sequencer.done is False
    assert sequencer._pending_close is True
    assert sequencer.maybe_close(now=10.0) is None
    # A fresh quote wakes the pending close rather than sending the stale price.
    sequencer.on_quote(Decimal("100.00"), Decimal("100.20"), now=10.0)
    woken = sequencer.maybe_close(now=10.0)
    assert woken is not None and woken.reduce_only is True


def test_a_close_waits_instead_of_crossing_the_frozen_worst_price():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    sequencer.decide_entry(now=0.0)
    sequencer.bind_entry("E1")
    sequencer.on_quote(Decimal("80.00"), Decimal("80.10"), now=1.0)
    decision = sequencer.note_entry(trade.OrderView(
        "entry", "E1", "FILLED", Decimal("0.100"), Decimal("100.30"), True,
    ), now=1.0)
    assert decision is None
    assert sequencer._pending_close is True
    sequencer.on_quote(Decimal("95.00"), Decimal("95.10"), now=2.0)
    decision = sequencer.maybe_close(now=2.0)
    assert decision is not None
    assert decision.limit_price >= Decimal("90.00")


def test_only_one_opening_attempt_is_permitted():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    assert sequencer.decide_entry(now=0.0) is not None
    assert sequencer.entry_attempts == 1
    assert sequencer.new_risk_requests == 1
    assert sequencer.decide_entry(now=0.0) is None
    assert sequencer.entry_attempts == 1
    assert sequencer._authorize_send(trade.ROLE_ENTRY) is not None


def test_the_app_request_cap_backstops_submission():
    sequencer = seq_for()
    sequencer.app_requests = sequencer.limits.max_app_requests
    assert sequencer._authorize_send(trade.ROLE_ENTRY) is not None


def test_the_hash_bound_plan_can_tighten_the_app_request_cap():
    document = resolved_plan(envelope={"max_app_requests": 3})
    sequencer = seq_for(document)
    sequencer.app_requests = 3
    assert sequencer._authorize_send(trade.ROLE_ENTRY) is not None


def test_a_touch_beyond_the_frozen_worst_price_is_no_trade():
    sequencer = seq_for()
    sequencer.begin()
    sequencer.on_quote(Decimal("200.00"), Decimal("200.10"), now=0.0)
    assert sequencer.decide_entry(now=0.0) is None
    assert sequencer.outcome == trade.OUTCOME_NO_TRADE


def test_a_sell_entry_prices_through_the_bid_and_closes_buy():
    document = resolved_plan()
    document["entry"]["side"] = "sell"
    document["close"]["side"] = "buy"
    document["close"]["limit_price"] = "110.00"
    document["entry"]["limit_price"] = "100.00"
    document["entry"]["quantity"] = "0.100"
    document["entry"]["notional_usd"] = "15.00"
    rebind_hashes(document)
    sequencer = seq_for(document)
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    decision = sequencer.decide_entry(now=0.0)
    assert decision is not None and decision.side == "sell"
    assert decision.limit_price <= Decimal("100.00")
    sequencer.bind_entry("E1")
    close = sequencer.note_entry(trade.OrderView(
        "entry", "E1", "FILLED", Decimal("0.100"), Decimal("100.00"), True,
    ), now=0.0)
    assert close is not None and close.side == "buy" and close.reduce_only is True


def _drive_entry(sequencer: trade.TradeSequencer) -> None:
    sequencer.begin()
    sequencer.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    sequencer.decide_entry(now=0.0)
    sequencer.bind_entry("E1")
    decision = sequencer.note_entry(trade.OrderView(
        "entry", "E1", "FILLED", Decimal("0.100"), Decimal("100.30"), True,
    ), now=0.0)
    assert decision is not None


# ------------------------------------------------------------------------------- reporting


class FakeStrategy:
    def __init__(self, sequencer: trade.TradeSequencer) -> None:
        self.sequencer = sequencer
        self._sequencer = sequencer
        self.leftovers: list[str] = []
        self.failures: list[str] = []

    @property
    def outcome(self) -> str:
        return self.sequencer.outcome


def test_a_flat_confirmed_cycle_is_the_only_verified_one():
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    sequencer.note_close(trade.OrderView(
        "close", "C1", "FILLED", Decimal("0.100"), Decimal("100.00"), True,
    ), now=0.0)
    strategy = FakeStrategy(sequencer)
    plan = sequencer.plan
    capability = trade.detect_native_trade_capability(CapableAdapter())
    acceptance = trade.acceptance_document(
        strategy, plan, capability, good_readiness(), clean_final())
    assert acceptance["clean"] is True
    assert acceptance["entry_confirmed"] is True
    assert acceptance["close_confirmed"] is True
    assert acceptance["flat_reconciled"] is True
    assert acceptance["native_final_clean"] is True


def test_local_flat_without_a_native_final_is_not_verified():
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    sequencer.note_close(trade.OrderView(
        "close", "C1", "FILLED", Decimal("0.100"), Decimal("100.00"), True,
    ), now=0.0)
    strategy = FakeStrategy(sequencer)
    acceptance = trade.acceptance_document(
        strategy, sequencer.plan, trade.detect_native_trade_capability(CapableAdapter()),
        good_readiness(), None)
    assert acceptance["local_sequencer_flat"] is True
    assert acceptance["flat_reconciled"] is False
    assert acceptance["native_final_clean"] is False
    assert acceptance["clean"] is False


@pytest.mark.parametrize("acknowledged", [False, True])
def test_dms_observations_are_reported_but_cannot_replace_final_proof(acknowledged):
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    sequencer.note_close(trade.OrderView(
        "close", "C1", "FILLED", Decimal("0.100"), Decimal("100.00"), True,
    ), now=0.0)
    native = SnapshotTarget({
        "run_id": "r", "phase": "reconciled",
        "dms_release": {
            "attempted": True, "frame_sent": True, "acknowledged": acknowledged,
            "outcome": "acknowledged" if acknowledged else "ack_timeout",
            "raw_frame": "SYNTHETIC_PRIVATE_FRAME",
        },
    })
    document = trade.trade_report_document(
        run_id="r", started=datetime.datetime.now(datetime.timezone.utc),
        finished=datetime.datetime.now(datetime.timezone.utc),
        exit_code=1, failure=None, plan=sequencer.plan, limits=sequencer.limits,
        capability=trade.detect_native_trade_capability(CapableAdapter()),
        readiness=good_readiness(), strategy=FakeStrategy(sequencer),
        stop_condition="run-returned", stops=[], watchdog=None, diagnostics=None,
        dms_release_target=native,
    )

    assert document["dms_release"]["acknowledged"] is acknowledged
    assert document["dms_release"]["frame_sent"] is True
    assert document["production_execution_verified"] is False
    assert "SYNTHETIC_PRIVATE_FRAME" not in json.dumps(document, default=trade._json_default)


@pytest.mark.parametrize("overrides", [
    {"complete": False},
    {"snapshot_fresh": False},
    {"reconciled_flat": False},
    {"position_qty": Decimal("0.001")},
    {"own_open_orders": 1},
    {"foreign_open_orders": 1},
    {"unknown_submissions": 1},
    {"shutdown_status": "dirty"},
    {"late_fills": 1},
    {"ordered_after_start": False},
    {"covers_latest_activity": False},
    {"instrument_matches": False},
    {"follows_reconciliation": False},
    {"available": False},
])
def test_a_dirty_or_unknown_final_snapshot_invalidates_clean(overrides):
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    sequencer.note_close(trade.OrderView(
        "close", "C1", "FILLED", Decimal("0.100"), Decimal("100.00"), True,
    ), now=0.0)
    final = clean_final(**overrides)
    if overrides.get("available") is False:
        final = trade.FinalReconciliation(False, "unavailable", "no final snapshot")
    acceptance = trade.acceptance_document(
        FakeStrategy(sequencer), sequencer.plan,
        trade.detect_native_trade_capability(CapableAdapter()), good_readiness(), final)
    assert acceptance["clean"] is False


@pytest.mark.parametrize("overrides", [
    {"identity_match": "mismatch"}, {"account_flat": False},
    {"coverage_complete": False}, {"foreign_open_orders": 1},
    {"own_open_orders": 1}, {"native_ready": False}, {"metadata_fresh": False},
    {"trading_enabled": False}, {"underlying_market_closed": True},
    {"dms_verified": False}, {"available_margin_usdc": Decimal("24")},
])
def test_a_start_gate_invalidates_clean_even_with_a_clean_final(overrides):
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    sequencer.note_close(trade.OrderView(
        "close", "C1", "FILLED", Decimal("0.100"), Decimal("100.00"), True,
    ), now=0.0)
    acceptance = trade.acceptance_document(
        FakeStrategy(sequencer), sequencer.plan,
        trade.detect_native_trade_capability(CapableAdapter()),
        good_readiness(**overrides), clean_final())
    assert acceptance["clean"] is False


def test_an_uncertain_close_is_not_verified():
    sequencer = seq_for()
    _drive_entry(sequencer)
    sequencer.bind_close("C1")
    sequencer.note_close(trade.OrderView(
        "close", "C1", "CANCELED", Decimal("0.050"), Decimal("100.00"), True,
    ), now=0.0)
    sequencer.bind_close("C2")
    sequencer.note_close(trade.OrderView(
        "close", "C2", "CANCELED", Decimal("0.025"), Decimal("100.00"), True,
    ), now=0.0)
    strategy = FakeStrategy(sequencer)
    acceptance = trade.acceptance_document(
        strategy, sequencer.plan, trade.detect_native_trade_capability(CapableAdapter()),
        good_readiness(), clean_final(),
    )
    assert acceptance["clean"] is False
    assert acceptance["flat_reconciled"] is False


def test_a_blocked_or_written_unverified_run_requires_a_failure_exit():
    capability = trade.detect_native_trade_capability(CapableAdapter())
    blocked = FakeStrategy(seq_for())
    blocked.sequencer.outcome = trade.OUTCOME_BLOCKED
    assert trade.execution_result_is_success(
        blocked, blocked.sequencer.plan, capability, good_readiness(), None,
    ) is False

    partial = seq_for()
    partial.begin()
    partial.on_quote(Decimal("100.00"), Decimal("100.10"), now=0.0)
    partial.decide_entry(now=0.0)
    partial.bind_entry("E1")
    partial.note_entry(trade.OrderView(
        "entry", "E1", "CANCELED", Decimal("0.040"), Decimal("100.30"), True,
    ), now=0.0)
    partial.bind_close("C1")
    partial.note_close(trade.OrderView(
        "close", "C1", "FILLED", Decimal("0.040"), Decimal("100.00"), True,
    ), now=0.0)
    assert trade.execution_result_is_success(
        FakeStrategy(partial), partial.plan, capability, good_readiness(), clean_final(),
    ) is False


def test_no_trade_is_distinct_from_partial_and_uncertain():
    capability = trade.detect_native_trade_capability(CapableAdapter())
    for outcome, flags in (
        (trade.OUTCOME_NO_TRADE, ("no_trade",)),
        (trade.OUTCOME_PARTIAL, ("partial",)),
        (trade.OUTCOME_UNCERTAIN, ("uncertain",)),
    ):
        sequencer = seq_for()
        sequencer.outcome = outcome
        document = trade.trade_report_document(
            run_id="r", started=datetime.datetime.now(datetime.timezone.utc),
            finished=datetime.datetime.now(datetime.timezone.utc),
            exit_code=0, failure=None, plan=sequencer.plan,
            limits=sequencer.limits, capability=capability, readiness=good_readiness(),
            strategy=FakeStrategy(sequencer), stop_condition="run-returned", stops=[],
            watchdog=None, diagnostics=None,
        )
        accounting = document["accounting"]
        for flag in flags:
            assert accounting[flag] is True
        for other in {"no_trade", "partial", "uncertain"} - set(flags):
            assert accounting[other] is False


def test_fees_stay_unknown_and_no_profit_is_claimed():
    sequencer = seq_for()
    document = trade.trade_report_document(
        run_id="r", started=datetime.datetime.now(datetime.timezone.utc),
        finished=datetime.datetime.now(datetime.timezone.utc),
        exit_code=0, failure=None, plan=sequencer.plan, limits=sequencer.limits,
        capability=trade.detect_native_trade_capability(CapableAdapter()),
        readiness=good_readiness(), strategy=FakeStrategy(sequencer),
        stop_condition="run-returned", stops=[], watchdog=None, diagnostics=None,
    )
    assert document["fees"] == "unknown"
    assert document["no_economic_claim_made"] is True
    assert document["protocol_verified"] is False
    assert document["account_id_published"] is False


# ----------------------------------------------------------------------------------- main


def test_dry_run_needs_no_credentials_and_reports_capability(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        code = trade.main(
            ["--mode", "production-trade", "--side", "buy", "--dry-run"],
            adapter=CapableAdapter(), environ={},
        )
    assert code == 0
    document = json.loads(out.getvalue())
    assert document["native_write_capable"] is True
    assert document["client_constructed"] is False
    assert document["env_file_read"] is False
    assert document["plan_sha256"] is None
    assert document["limits_configured"] is True
    assert document["live_execution_ready"] is False


def test_dry_run_on_the_installed_shape_is_honest():
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = trade.main(
            ["--mode", "production-trade", "--side", "buy", "--dry-run"],
            adapter=OldAdapter(), environ={},
        )
    assert code == 0
    document = json.loads(out.getvalue())
    assert document["native_write_capable"] is False
    assert document["limits_configured"] is True
    assert document["live_execution_ready"] is False


def test_a_live_run_with_an_unsupported_wheel_refuses_before_credentials(tmp_path, monkeypatch):
    called = {"env": False}

    def _forbidden(*args, **kwargs):
        called["env"] = True
        raise AssertionError("credentials must not be read before the capability gate")

    monkeypatch.setattr(trade, "_load_environment", _forbidden)
    plan_path = write_plan(tmp_path, authorized(resolved_plan()))
    err = io.StringIO()
    with contextlib.redirect_stderr(err):
        code = trade.main(
            ["--mode", "production-trade", "--plan", str(plan_path),
             "--confirm-production-trade", "--acknowledge-mainnet-authorization"],
            adapter=OldAdapter(), environ={},
        )
    assert code == 2
    assert called["env"] is False
    assert "write-envelope contract" in err.getvalue()


def test_a_live_run_without_the_acknowledgements_is_refused(tmp_path):
    plan_path = write_plan(tmp_path, authorized(resolved_plan()))
    base = ["--mode", "production-trade", "--plan", str(plan_path)]
    cases = (
        base + ["--acknowledge-mainnet-authorization"],  # missing --confirm-production-trade
        base + ["--confirm-production-trade"],  # missing --acknowledge-mainnet-authorization
    )
    for argv in cases:
        with contextlib.redirect_stderr(io.StringIO()):
            code = trade.main(argv, adapter=CapableAdapter(), environ={})
        assert code == 2


def test_a_live_run_without_a_plan_is_refused():
    with contextlib.redirect_stderr(io.StringIO()):
        code = trade.main(
            ["--mode", "production-trade", "--confirm-production-trade",
             "--acknowledge-mainnet-authorization"],
            adapter=CapableAdapter(), environ={},
        )
    assert code == 2


def test_a_live_run_with_a_capable_wheel_but_missing_credentials_refuses(tmp_path):
    plan_path = write_plan(tmp_path, authorized(resolved_plan()))
    factory = NodeFactory()
    with contextlib.redirect_stderr(io.StringIO()):
        code = trade.main(
            ["--mode", "production-trade", "--plan", str(plan_path),
             "--confirm-production-trade", "--acknowledge-mainnet-authorization"],
            adapter=CapableAdapter(), node_factory=factory, environ={},
        )
    assert code == 2
    assert factory.calls == []  # no client was built


def test_a_live_run_with_an_unapproved_plan_is_refused(tmp_path):
    plan_path = write_plan(tmp_path, resolved_plan())  # user_phrase false
    with contextlib.redirect_stderr(io.StringIO()):
        code = trade.main(
            ["--mode", "production-trade", "--plan", str(plan_path),
             "--confirm-production-trade", "--acknowledge-mainnet-authorization"],
            adapter=CapableAdapter(),
            environ={
                "ONDO_MAINNET_API_KEY": "sentinel-key",
                "ONDO_MAINNET_API_SECRET": "sentinel-secret",
                "ONDO_MAINNET_ACCOUNT_ID": "ONDO-001",
            },
        )
    assert code == 2


def test_a_widened_minutes_override_is_refused(tmp_path):
    plan_path = write_plan(tmp_path, authorized(resolved_plan()))
    with contextlib.redirect_stderr(io.StringIO()):
        code = trade.main(
            ["--mode", "production-trade", "--plan", str(plan_path),
             "--confirm-production-trade", "--acknowledge-mainnet-authorization",
             "--minutes", "20"],
            adapter=CapableAdapter(),
            environ={
                "ONDO_MAINNET_API_KEY": "sentinel-key",
                "ONDO_MAINNET_API_SECRET": "sentinel-secret",
                "ONDO_MAINNET_ACCOUNT_ID": "ONDO-001",
            },
        )
    assert code == 2


def test_the_wrong_mode_is_refused():
    with contextlib.redirect_stderr(io.StringIO()):
        code = trade.main(["--dry-run", "--side", "buy"], adapter=CapableAdapter(),
                          environ={})
    assert code == 2


def test_there_is_no_sandbox_or_readonly_path():
    with pytest.raises(SystemExit):
        trade.main(["--mode", "sandbox"], adapter=CapableAdapter(), environ={})


def test_instrument_mismatch_is_reported():
    spec = parse(resolved_plan()).instrument

    class Instrument:
        id = "TSLA-USD-PERP.ONDO"
        price_increment = Decimal("0.01")
        size_increment = Decimal("0.001")
        min_quantity = Decimal("0.001")
        min_notional = Decimal("10")
        max_quantity = Decimal("100000")

    problems = trade.instrument_matches_plan(Instrument(), spec)
    assert any("instrument id" in problem for problem in problems)


def test_increment_drift_is_reported():
    spec = parse(resolved_plan()).instrument

    class Instrument:
        id = "NVDA-USD-PERP.ONDO"
        price_increment = Decimal("0.05")  # drifted from the approved 0.01
        size_increment = Decimal("0.001")
        min_quantity = Decimal("0.001")
        min_notional = Decimal("10")
        max_quantity = Decimal("100000")

    problems = trade.instrument_matches_plan(Instrument(), spec)
    assert any("price_increment" in problem for problem in problems)


def test_missing_quote_currency_is_reported_as_unknown_metadata():
    spec = parse(resolved_plan()).instrument

    class Instrument:
        id = "NVDA-USD-PERP.ONDO"
        price_increment = Decimal("0.01")
        size_increment = Decimal("0.001")
        min_quantity = Decimal("0.001")
        min_notional = Decimal("10")
        max_quantity = Decimal("100000")

    problems = trade.instrument_matches_plan(Instrument(), spec)
    assert any("quote_currency" in problem for problem in problems)


def _real_ondo_instrument(*, min_notional: Money | None) -> CryptoPerpetual:
    usd = Currency.from_str("USD")
    return CryptoPerpetual(
        instrument_id=InstrumentId.from_str("NVDA-USD-PERP.ONDO"),
        raw_symbol=Symbol("NVDA-USD.P"),
        base_currency=Currency.from_str("NVDA"),
        quote_currency=usd,
        settlement_currency=usd,
        is_inverse=False,
        price_precision=2,
        size_precision=3,
        price_increment=Price.from_str("0.01"),
        size_increment=Quantity.from_str("0.001"),
        ts_event=0,
        ts_init=0,
        min_quantity=None,
        min_notional=min_notional,
        maker_fee=Decimal("0.0001"),
        taker_fee=Decimal("0.00025"),
        info={},
    )


def _plan_with_minimum(amount: str | None, currency: str | None,
                       source: str) -> trade.InstrumentSpec:
    document = resolved_plan()
    document["instrument"]["min_quantity"] = None
    document["instrument"]["min_quantity_source"] = "unpublished"
    document["instrument"]["min_notional"] = amount
    document["instrument"]["min_notional_currency"] = currency
    document["instrument"]["min_notional_source"] = source
    rebind_hashes(document)
    return parse(document).instrument


def test_a_real_crypto_perpetual_with_unpublished_minima_matches_the_plan():
    spec = _plan_with_minimum(None, None, "unpublished")
    problems = trade.instrument_matches_plan(
        _real_ondo_instrument(min_notional=None), spec,
    )
    assert not any("min_quantity" in problem or "min_notional" in problem
                   for problem in problems)


def test_a_real_money_minimum_is_exact_and_currency_checked():
    spec = _plan_with_minimum("10", "USD", "native-metadata")
    matching = _real_ondo_instrument(
        min_notional=Money(Decimal("10"), Currency.from_str("USD")),
    )
    assert trade.instrument_matches_plan(matching, spec) == []

    mismatching = _real_ondo_instrument(
        min_notional=Money(Decimal("10"), Currency.from_str("USDC")),
    )
    problems = trade.instrument_matches_plan(mismatching, spec)
    assert any("min_notional currency" in problem for problem in problems)


def test_a_plan_cannot_bind_a_minimum_in_a_non_quote_currency():
    document = resolved_plan()
    document["instrument"]["min_notional"] = "10"
    document["instrument"]["min_notional_currency"] = "EUR"
    document["instrument"]["min_notional_source"] = "native-metadata"
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError, match="quote_currency"):
        parse(document)


def test_a_present_but_malformed_minimum_is_not_treated_as_unpublished():
    spec = _plan_with_minimum("10", "USD", "native-metadata")

    class Instrument:
        id = "NVDA-USD-PERP.ONDO"
        price_increment = Decimal("0.01")
        size_increment = Decimal("0.001")
        min_quantity = None
        min_notional = "not-money"
        max_quantity = Decimal("100000")
        quote_currency = "USD"

    problems = trade.instrument_matches_plan(Instrument(), spec)
    assert any("min_notional is malformed" in problem for problem in problems)


@pytest.mark.parametrize("raw", ["NaN", "-1", "0"])
def test_a_present_non_finite_or_non_positive_minimum_is_refused(raw):
    spec = _plan_with_minimum("10", "USD", "native-metadata")

    class BadMoney:
        currency = Currency.from_str("USD")

        def as_decimal(self):
            return Decimal(raw)

    class Instrument:
        id = "NVDA-USD-PERP.ONDO"
        price_increment = Decimal("0.01")
        size_increment = Decimal("0.001")
        min_quantity = None
        min_notional = BadMoney()
        max_quantity = Decimal("100000")
        quote_currency = "USD"

    problems = trade.instrument_matches_plan(Instrument(), spec)
    assert any("min_notional is malformed" in problem for problem in problems)


# --------------------------------------------------- real Nautilus integration
#
# These tests construct the *real* ``OndoTradeStrategy`` and drive it with real
# ``LimitOrder`` / ``OrderFilled`` objects.  The cache double enforces exactly the type the
# real ``Cache`` enforces (a ``str`` raises ``TypeError``), which is what P0-2 turned on.


def _new_limit_order(coid: str, side: OrderSide, qty: str, px: str,
                     *, reduce_only: bool = False) -> LimitOrder:
    return LimitOrder(
        trader_id=TraderId.from_str("TESTER-001"),
        strategy_id=StrategyId.from_str("S-001"),
        instrument_id=InstrumentId.from_str("NVDA-USD-PERP.ONDO"),
        client_order_id=ClientOrderId.from_str(coid),
        order_side=side,
        quantity=Quantity.from_str(qty),
        price=Price.from_str(px),
        time_in_force=TimeInForce.IOC,
        post_only=False,
        reduce_only=reduce_only,
        quote_quantity=False,
        init_id=UUID4(),
        ts_init=0,
    )


def _fill_events(order: LimitOrder, qty: str, px: str, trade_id: str) -> OrderFilled:
    return OrderFilled(
        trader_id=TraderId.from_str("TESTER-001"),
        strategy_id=StrategyId.from_str("S-001"),
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"),
        account_id=AccountId.from_str("ONDO-1"),
        trade_id=TradeId(trade_id),
        order_side=order.side,
        order_type=OrderType.LIMIT,
        last_qty=Quantity.from_str(qty),
        last_px=Price.from_str(px),
        currency=Currency.from_str("USD"),
        liquidity_side=LiquiditySide.TAKER,
        event_id=UUID4(),
        ts_event=3,
        ts_init=3,
        reconciliation=False,
    )


def _apply_fill(order: LimitOrder, qty: str, px: str, trade_id: str) -> None:
    order.apply(OrderSubmitted(
        trader_id=TraderId.from_str("TESTER-001"), strategy_id=StrategyId.from_str("S-001"),
        instrument_id=order.instrument_id, client_order_id=order.client_order_id,
        account_id=AccountId.from_str("ONDO-1"), event_id=UUID4(), ts_event=1, ts_init=1))
    order.apply(OrderAccepted(
        trader_id=TraderId.from_str("TESTER-001"), strategy_id=StrategyId.from_str("S-001"),
        instrument_id=order.instrument_id, client_order_id=order.client_order_id,
        venue_order_id=VenueOrderId("V-1"), account_id=AccountId.from_str("ONDO-1"),
        event_id=UUID4(), ts_event=2, ts_init=2, reconciliation=False))
    order.apply(_fill_events(order, qty, px, trade_id))


class FakeTradeInstrument:
    def __init__(self, spec, instrument_id) -> None:
        self.id = instrument_id
        self.price_increment = spec.price_increment
        self.size_increment = spec.size_increment
        self.min_quantity = spec.min_quantity
        self.min_notional = spec.min_notional
        self.max_quantity = spec.max_quantity
        self.quote_currency = spec.quote_currency

    def make_qty(self, value):
        return Quantity.from_str(str(value))

    def make_price(self, value):
        return Price.from_str(str(value))


class TypeStrictCache:
    """A cache double that enforces the real ``ClientOrderId`` type (no arbitrary str)."""

    def __init__(self, instrument=None) -> None:
        self._orders: dict[str, LimitOrder] = {}
        self._instrument = instrument

    def add(self, order) -> None:
        self._orders[str(order.client_order_id)] = order

    def order(self, client_order_id):
        if not isinstance(client_order_id, ClientOrderId):
            raise TypeError(
                f"'{type(client_order_id).__name__}' object is not an instance of "
                f"'ClientOrderId'"
            )
        return self._orders.get(str(client_order_id))

    def instrument(self, instrument_id):
        return self._instrument


class FakeOrderFactory:
    def __init__(self) -> None:
        self.created: list[LimitOrder] = []
        self._counter = 0

    def limit(self, instrument_id, order_side, quantity, price, time_in_force,
              reduce_only=False, **_kwargs):
        self._counter += 1
        order = LimitOrder(
            trader_id=TraderId.from_str("TESTER-001"),
            strategy_id=StrategyId.from_str("S-001"),
            instrument_id=instrument_id,
            client_order_id=ClientOrderId.from_str(f"C-{self._counter}"),
            order_side=order_side,
            quantity=quantity,
            price=price,
            time_in_force=time_in_force,
            post_only=False,
            reduce_only=bool(reduce_only),
            quote_quantity=False,
            init_id=UUID4(),
            ts_init=0,
        )
        self.created.append(order)
        return order


class _Quote:
    def __init__(self, bid: str, ask: str) -> None:
        self.bid_price = Price.from_str(bid)
        self.ask_price = Price.from_str(ask)


class TradeTestStrategy(trade.OndoTradeStrategy):
    """The real strategy with a type-strict cache and a real-order factory injected."""

    def __init__(self, config) -> None:
        super().__init__(config)
        # Injected after construction: the base Strategy.__new__ accepts only the config, and
        # the strategy constructor does not touch the cache/order factory.
        self._test_cache = TypeStrictCache()
        self._test_order_factory = FakeOrderFactory()
        self.submitted: list[LimitOrder] = []
        self.cancelled: list = []

    def install(self, cache, order_factory, instrument) -> None:
        self._test_cache = cache
        self._test_order_factory = order_factory
        self._instrument = instrument
        self._sequencer.begin()

    @property
    def cache(self):
        return self._test_cache

    @property
    def order_factory(self):
        return self._test_order_factory

    def submit_order(self, order, **_kwargs):
        self.submitted.append(order)
        self._test_cache.add(order)
        return None

    def cancel_order(self, client_order_id, **_kwargs):
        self.cancelled.append(client_order_id)
        return None


def _integration_strategy(cache=None, order_factory=None):
    plan = parse(resolved_plan())
    limits = load_trade_limits(None)
    cache = cache if cache is not None else TypeStrictCache()
    order_factory = order_factory if order_factory is not None else FakeOrderFactory()
    config = trade.OndoTradeConfig(
        instrument_id=InstrumentId.from_str("NVDA-USD-PERP.ONDO"),
        plan=plan,
        limits=limits,
        readiness_provider=lambda: good_readiness(),
    )
    strategy = TradeTestStrategy(config)
    strategy.install(cache, order_factory,
                     FakeTradeInstrument(plan.instrument, config.instrument_id))
    return strategy


def test_the_real_cache_requires_a_client_order_id():
    cache = Cache()
    with pytest.raises(TypeError):
        cache.order("O-1")
    assert cache.order(ClientOrderId("O-1")) is None


def test_order_view_reads_a_real_filled_order_through_client_order_id():
    order = _new_limit_order("E1", OrderSide.BUY, "0.100", "100.30")
    _apply_fill(order, "0.100", "100.30", "T-1")
    cache = TypeStrictCache()
    cache.add(order)
    strategy = _integration_strategy(cache)
    view = strategy._order_view("entry", str(order.client_order_id))
    assert view is not None
    assert view.filled_qty == Decimal("0.100")
    assert view.is_closed is True
    # The old code passed the str straight to Cache.order, which raised and returned None.
    assert strategy._order_view("entry", "not-a-client-order-id") is None


def test_order_view_reads_the_rejection_reason_from_the_real_terminal_event():
    order = _new_limit_order("E-REJECTED", OrderSide.BUY, "0.060", "226.51")
    order.apply(OrderSubmitted(
        trader_id=TraderId.from_str("TESTER-001"),
        strategy_id=StrategyId.from_str("S-001"),
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        account_id=AccountId.from_str("ONDO-1"),
        event_id=UUID4(),
        ts_event=1,
        ts_init=1,
    ))
    order.apply(OrderRejected(
        trader_id=TraderId.from_str("TESTER-001"),
        strategy_id=StrategyId.from_str("S-001"),
        instrument_id=order.instrument_id,
        client_order_id=order.client_order_id,
        account_id=AccountId.from_str("ONDO-1"),
        reason="submit-order-error: minimum notional",
        event_id=UUID4(),
        ts_event=2,
        ts_init=2,
        reconciliation=False,
    ))
    cache = TypeStrictCache()
    cache.add(order)
    strategy = _integration_strategy(cache)

    view = strategy._order_view("entry", str(order.client_order_id))

    assert view is not None
    assert view.status == "REJECTED"
    assert view.rejected_reason == "submit-order-error: minimum notional"


def test_a_real_entry_fill_triggers_a_reduce_only_close_for_the_confirmed_quantity():
    cache = TypeStrictCache()
    order_factory = FakeOrderFactory()
    strategy = _integration_strategy(cache, order_factory)
    strategy.on_quote(_Quote("100.00", "100.10"))
    assert len(strategy.submitted) == 1
    entry = strategy.submitted[0]
    assert entry.is_reduce_only is False and entry.is_buy
    # The actual sent price is the sequencer's bounded decision, not an unbounded value.
    assert Decimal(str(entry.price)) == Decimal("100.30")
    assert Decimal(str(entry.price)) <= strategy.sequencer.plan.entry.limit_price
    # Confirm the entry fill, then feed the real event through the real callback.
    _apply_fill(entry, "0.100", "100.30", "T-1")
    strategy.on_order_filled(_fill_events(entry, "0.100", "100.30", "T-1"))
    assert len(strategy.submitted) == 2
    close = strategy.submitted[1]
    assert close.is_reduce_only is True
    assert close.side == OrderSide.SELL
    assert Decimal(str(close.quantity)) == Decimal("0.100")
    assert close.time_in_force == TimeInForce.IOC
    # Confirm the close, and the run is genuinely flat through the real callbacks.
    _apply_fill(close, "0.100", "100.00", "T-2")
    strategy.on_order_filled(_fill_events(close, "0.100", "100.00", "T-2"))
    assert strategy.sequencer.outcome == trade.OUTCOME_FILLED
    assert strategy.sequencer.net_position == Decimal("0")
    assert strategy.failures == []


def test_the_strategy_only_ever_submits_two_orders_for_a_clean_cycle():
    cache = TypeStrictCache()
    order_factory = FakeOrderFactory()
    strategy = _integration_strategy(cache, order_factory)
    strategy.on_quote(_Quote("100.00", "100.10"))
    entry = strategy.submitted[0]
    _apply_fill(entry, "0.100", "100.30", "T-1")
    strategy.on_order_filled(_fill_events(entry, "0.100", "100.30", "T-1"))
    close = strategy.submitted[1]
    _apply_fill(close, "0.100", "100.00", "T-2")
    strategy.on_order_filled(_fill_events(close, "0.100", "100.00", "T-2"))
    assert strategy.sequencer.entry_attempts == 1
    assert strategy.sequencer.new_risk_requests == 1
    assert strategy.sequencer.orders_submitted == 2
    # A late event cannot open a third order.
    strategy.on_order_filled(_fill_events(close, "0.001", "100.00", "T-3"))
    assert len(strategy.submitted) == 2


def test_cleanup_passes_a_client_order_id_to_cancel_order():
    cache = TypeStrictCache()
    strategy = _integration_strategy(cache)
    strategy.on_quote(_Quote("100.00", "100.10"))
    assert len(strategy.submitted) == 1
    strategy._run_cleanup("test")
    assert strategy.cancelled
    assert all(isinstance(value, ClientOrderId) for value in strategy.cancelled)


def test_cleanup_refuses_when_the_app_request_budget_is_exhausted():
    cache = TypeStrictCache()
    strategy = _integration_strategy(cache)
    strategy.on_quote(_Quote("100.00", "100.10"))
    strategy.sequencer.app_requests = strategy.sequencer.plan.envelope.max_app_requests
    strategy._run_cleanup("test")
    assert strategy.cancelled == []
    assert any("app write-request cap" in item for item in strategy.failures)


def test_cleanup_requests_each_own_order_at_most_once_without_new_evidence():
    cache = TypeStrictCache()
    strategy = _integration_strategy(cache)
    strategy.on_quote(_Quote("100.00", "100.10"))
    strategy._run_cleanup("first")
    strategy._run_cleanup("second")
    assert len(strategy.cancelled) == 1
    assert strategy.sequencer.cancel_requests == 1


def test_synchronous_on_stop_never_blocks_polling_for_events(monkeypatch):
    strategy = _integration_strategy()
    called = {"wait": False}

    def forbidden(*_args, **_kwargs):
        called["wait"] = True
        raise AssertionError("on_stop must not block the strategy event dispatcher")

    monkeypatch.setattr(strategy, "wait_for_cleanup", forbidden)
    strategy.on_stop()
    assert called["wait"] is False


def test_watchdog_can_be_canceled_when_the_node_returns_early():
    done = threading.Event()
    cancel = threading.Event()
    stopped = {"called": False}

    def stop():
        stopped["called"] = True

    thread, state = trade.start_trade_watchdog(
        done, stop, 60.0, cancel_event=cancel,
    )
    cancel.set()
    thread.join(1.0)
    assert thread.is_alive() is False
    assert stopped["called"] is False
    assert state["canceled"] is True


def test_watchdog_distinguishes_stop_attempt_from_confirmation():
    done = threading.Event()
    done.set()

    def stop():
        raise RuntimeError("stop transport failed")

    thread, state = trade.start_trade_watchdog(done, stop, 1.0)
    thread.join(1.0)
    assert thread.is_alive() is False
    assert state["stop_attempted"] is True
    assert state["stop_confirmed"] is False
    assert state["stopped"] is False
    assert state["worker_finished"] is True
    assert "RuntimeError" in state["stop_error"]


def test_watchdog_uses_the_real_bounded_stop_stopped_schema():
    done = threading.Event()
    done.set()

    class Handle:
        def __init__(self):
            self.running = True

        def stop(self):
            self.running = False

        @property
        def is_running(self):
            return self.running

    handle = Handle()
    thread, state = trade.start_trade_watchdog(
        done,
        lambda: trade.bounded_stop(
            handle, label="test", via="handle", grace_secs=0.1, poll_secs=0.01,
        ),
        1.0,
    )
    thread.join(1.0)
    assert state["stop_attempted"] is True
    assert state["stop_confirmed"] is True
    assert state["stopped"] is True


def test_check_credentials_accepts_a_raw_venue_account_id_and_maps_it():
    environ = {
        "ONDO_MAINNET_API_KEY": "sentinel-key",
        "ONDO_MAINNET_API_SECRET": "sentinel-secret",
        "ONDO_MAINNET_ACCOUNT_ID": "1234567890",
    }
    trade.check_credentials(environ)
    account_id, raw = trade.resolve_account_identity(
        trade.MODE_PRODUCTION_READONLY, environ, trade.MAINNET_CREDENTIAL_VARIABLES)
    assert str(account_id) == "ONDO-1234567890"
    assert raw == "1234567890"


class _RecordingBuilder:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self
        return record


class _FakeLiveNode:
    last_builder: _RecordingBuilder | None = None

    @staticmethod
    def builder(*_args, **_kwargs):
        _FakeLiveNode.last_builder = _RecordingBuilder()
        return _FakeLiveNode.last_builder


class _FakeNode:
    def __init__(self) -> None:
        self.strategies: list = []

    def add_strategy(self, strategy) -> None:
        self.strategies.append(strategy)


def test_the_run_token_dms_timeout_and_identity_reach_the_native_config(monkeypatch):
    CapableConfig.instances.clear()
    plan = parse(resolved_plan())
    limits = load_trade_limits(None)
    monkeypatch.setattr(trade, "LiveNode", _FakeLiveNode)
    node, _target, _session, strategy = trade._build_node(
        plan, CapableAdapter(), limits, lambda: good_readiness(), lambda _b: _FakeNode(),
        AccountId.from_str("ONDO-1234567890"), "1234567890", 123, 456,
        "run-abc",
    )
    config = CapableConfig.instances[-1]
    assert config.diagnostics_run_id == "run-abc"
    assert config.dms_timeout_secs == plan.dms["timeout_secs"]
    assert config.reconcile_interval_secs == 1
    assert config.allow_production_orders is True
    assert config.account_id == AccountId.from_str("ONDO-1234567890")
    assert config.expected_venue_account_id == "1234567890"
    assert config.execution_envelope is not None
    envelope = config.execution_envelope
    assert envelope.entry_side == plan.entry.side
    assert envelope.entry_max_quantity == str(plan.entry.quantity)
    assert envelope.entry_worst_price == str(plan.entry.limit_price)
    assert envelope.entry_max_notional_usd == str(plan.entry.notional_usd)
    assert envelope.close_side == plan.close.side
    assert envelope.close_max_quantity == str(plan.entry.quantity)
    assert envelope.close_worst_price == str(plan.close.limit_price)
    assert envelope.max_close_attempts == plan.close.max_close_attempts
    assert envelope.max_new_risk_requests == 1
    assert envelope.max_app_requests == plan.envelope.max_app_requests
    assert envelope.min_available_margin_usdc == str(
        plan.envelope.min_available_margin_usdc,
    )
    assert envelope.entry_deadline_unix_nanos == 123
    assert envelope.cleanup_deadline_unix_nanos == 456
    assert node.strategies == [strategy]
    logging_calls = [
        args[0] for name, args, _kwargs in _FakeLiveNode.last_builder.calls
        if name == "with_logging"
    ]
    assert len(logging_calls) == 1
    logger_config = logging_calls[0]
    assert logger_config.stdout_level == trade.LogLevel.OFF
    assert logger_config.fileout_level == trade.LogLevel.OFF
    assert logger_config.bypass_logging is True
    assert logger_config.print_config is False


def test_production_trade_node_uses_thirty_second_connection_window(monkeypatch):
    plan = parse(resolved_plan())
    limits = load_trade_limits(None)
    monkeypatch.setattr(trade, "LiveNode", _FakeLiveNode)
    trade._build_node(
        plan, CapableAdapter(), limits, lambda: good_readiness(), lambda _b: _FakeNode(),
        AccountId.from_str("ONDO-1234567890"), "1234567890", 123, 456,
        "run-abc",
    )

    timeout_calls = [
        args for name, args, _kwargs in _FakeLiveNode.last_builder.calls
        if name == "with_timeout_connection"
    ]
    assert timeout_calls == [(30,)]


def test_production_trade_node_allows_native_bounded_shutdown_to_finish(monkeypatch):
    plan = parse(resolved_plan())
    limits = load_trade_limits(None)
    monkeypatch.setattr(trade, "LiveNode", _FakeLiveNode)
    trade._build_node(
        plan, CapableAdapter(), limits, lambda: good_readiness(), lambda _b: _FakeNode(),
        AccountId.from_str("ONDO-1234567890"), "1234567890", 123, 456,
        "run-abc",
    )

    timeout_calls = [
        args for name, args, _kwargs in _FakeLiveNode.last_builder.calls
        if name == "with_timeout_disconnection_secs"
    ]
    assert timeout_calls == [(30,)]


def test_trade_cli_does_not_offer_an_unsafe_native_log_level():
    with pytest.raises(SystemExit):
        trade.parse_args([
            "--mode", "production-trade", "--side", "buy", "--dry-run",
            "--log-level", "DEBUG",
        ])


def test_a_wheel_without_the_run_token_field_fails_closed():
    class Config:
        def __init__(self, environment=None, account_id=None, base_url_http=None,
                     base_url_ws=None, account_read_only=None, journal_path=None,
                     execution_envelope=None, expected_venue_account_id=None,
                     dms_timeout_secs=None) -> None:
            pass

    class Adapter(CapableAdapter):
        OndoExecutionClientConfig = Config

    capability = trade.detect_native_trade_capability(Adapter())
    assert capability.supported is False
    assert capability.source == trade.CAPABILITY_SOURCE_RUN_TOKEN_FIELD


def test_a_wheel_without_explicit_production_opt_in_fails_closed():
    class Config:
        def __init__(self, environment=None, account_id=None, base_url_http=None,
                     base_url_ws=None, account_read_only=None, journal_path=None,
                     execution_envelope=None, expected_venue_account_id=None,
                     diagnostics_run_id=None, dms_timeout_secs=None,
                     reconcile_interval_secs=None) -> None:
            pass

    class Adapter(CapableAdapter):
        OndoExecutionClientConfig = Config

    capability = trade.detect_native_trade_capability(Adapter())
    assert capability.supported is False
    assert capability.source == trade.CAPABILITY_SOURCE_PRODUCTION_OPT_IN_FIELD


def test_a_renewal_interval_the_native_does_not_expose_is_refused():
    document = resolved_plan()
    document["dms"]["renewal_interval_secs"] = 7
    rebind_hashes(document)
    with pytest.raises(trade.TradePlanError):
        parse(document)


def test_a_shorter_minutes_reaches_execute(monkeypatch, tmp_path):
    plan_path = write_plan(tmp_path, authorized(resolved_plan()))
    limits_path = write_explicit_limits(tmp_path)
    captured: dict = {}

    def fake_execute(plan, limits, **kwargs):
        captured["deadline_secs"] = kwargs.get("deadline_secs")
        return 0

    monkeypatch.setattr(trade, "execute", fake_execute)
    code = trade.main(
        ["--mode", "production-trade", "--plan", str(plan_path),
         "--confirm-production-trade", "--acknowledge-mainnet-authorization",
         "--minutes", "5", "--limits", str(limits_path)],
        adapter=CapableAdapter(),
        environ={
            "ONDO_MAINNET_API_KEY": "sentinel-key",
            "ONDO_MAINNET_API_SECRET": "sentinel-secret",
            "ONDO_MAINNET_ACCOUNT_ID": "1234567890",
        },
        now=lambda: datetime.datetime(2026, 9, 19, tzinfo=datetime.timezone.utc),
    )
    assert code == 0
    assert captured["deadline_secs"] == 300


def test_a_side_that_differs_from_the_plan_is_refused(tmp_path):
    plan_path = write_plan(tmp_path, authorized(resolved_plan()))
    with contextlib.redirect_stderr(io.StringIO()):
        code = trade.main(
            ["--mode", "production-trade", "--plan", str(plan_path),
             "--confirm-production-trade", "--acknowledge-mainnet-authorization",
             "--side", "sell"],
            adapter=CapableAdapter(),
            environ={
                "ONDO_MAINNET_API_KEY": "sentinel-key",
                "ONDO_MAINNET_API_SECRET": "sentinel-secret",
                "ONDO_MAINNET_ACCOUNT_ID": "1234567890",
            },
        )
    assert code == 2


class _LifecycleTarget:
    def __init__(self, run_id: str, *, reconciled_clean: bool) -> None:
        self.run_id = run_id
        self.phase = "start"
        self.reconciled_clean = reconciled_clean
        self.stopped = threading.Event()

    def production_trade_snapshot(self):
        common = {
            "run_id": self.run_id,
            "generation": 1,
            "snapshot_unix_nanos": 100,
        }
        if self.phase == "start":
            return {
                **common, "phase": "start", "identity_match": "matched",
                "account_flat": True, "coverage_complete": True,
                "foreign_open_orders": 0, "own_open_orders": 0,
                "native_ready": True, "metadata_fresh": True,
                "trading_enabled": True, "underlying_market_closed": False,
                "dms_verified": True, "available_margin_usdc": "30",
            }
        clean = self.reconciled_clean
        snapshot = {
            **common,
            "phase": "final" if self.stopped.is_set() else "reconciled",
            "generation": 3 if self.stopped.is_set() else 2,
            "snapshot_unix_nanos": 300 if self.stopped.is_set() else 200,
            "latest_activity_generation": 2,
            "latest_activity_unix_nanos": 180,
            "reconciled_activity_generation": 2 if clean else 1,
            "reconciliation_unix_nanos": 190,
            "complete": True,
            "snapshot_fresh": True,
            "instrument_id": "NVDA-USD-PERP.ONDO",
            "position_qty": "0" if clean else "0.001",
            "reconciled_flat": clean,
            "own_open_orders": 0,
            "foreign_open_orders": 0,
            "unknown_submissions": 0,
            "late_fills": 0,
            "execution_cost": None,
        }
        if self.stopped.is_set():
            snapshot["shutdown_status"] = "clean"
        return snapshot


class _LifecycleNode:
    def __init__(self, target: _LifecycleTarget) -> None:
        self.target = target

    def run(self):
        assert self.target.stopped.wait(2.0), "watchdog did not dispatch bounded_stop"


class _LifecycleStrategy:
    def __init__(self, sequencer, readiness) -> None:
        self.sequencer = sequencer
        self._sequencer = sequencer
        self.readiness = readiness
        self.done_event = threading.Event()
        self.done_event.set()
        self.cleanup_done_event = threading.Event()
        self.cleanup_done_event.set()
        self.leftovers = []
        self.failures = []
        self.failure_threads: list[int] = []
        self.wait_calls = 0

    @property
    def outcome(self):
        return self.sequencer.outcome

    def wait_for_cleanup(self, *_args, **_kwargs):
        self.wait_calls += 1
        return True

    def _note_failure(self, reason):
        self.failure_threads.append(threading.get_ident())
        self.failures.append(reason)


@pytest.mark.parametrize("reconciled_clean, expected_code", [(True, 0), (False, 1)])
def test_execute_carries_start_readiness_through_reconciled_and_final_without_worker_strategy_access(
    monkeypatch, tmp_path, reconciled_clean, expected_code,
):
    plan = parse(authorized(resolved_plan()))
    limits = load_trade_limits(None)
    capability = trade.detect_native_trade_capability(CapableAdapter())
    main_thread = threading.get_ident()
    captured = {}

    def fake_build(_plan, _adapter, _limits, _provider, _node_factory, _account_id,
                   _expected_id, _entry_deadline, _cleanup_deadline, run_id):
        target = _LifecycleTarget(run_id, reconciled_clean=reconciled_clean)
        readiness = trade.read_trade_readiness(target, run_id=run_id)
        target.phase = "reconciled"
        sequencer = seq_for()
        _drive_entry(sequencer)
        sequencer.bind_close("C1")
        sequencer.note_close(trade.OrderView(
            "close", "C1", "FILLED", Decimal("0.100"), Decimal("100.00"), True,
        ), now=0.0)
        strategy = _LifecycleStrategy(sequencer, readiness)
        captured.update(target=target, strategy=strategy)
        return _LifecycleNode(target), target, {}, strategy

    def fake_bounded_stop(target, **_kwargs):
        target.stopped.set()
        return {
            "attempted": True, "requested": True, "stopped": True, "error": None,
        }

    monkeypatch.setattr(trade, "_build_node", fake_build)
    monkeypatch.setattr(trade, "resolve_stop_target", lambda node: (node.target, "fake"))
    monkeypatch.setattr(trade, "bounded_stop", fake_bounded_stop)
    monkeypatch.setattr(trade, "read_native_diagnostics", lambda *_a, **_k: None)
    code = trade.execute(
        plan, limits, adapter=CapableAdapter(),
        environ={
            "ONDO_MAINNET_API_KEY": "sentinel-key",
            "ONDO_MAINNET_API_SECRET": "sentinel-secret",
            "ONDO_MAINNET_ACCOUNT_ID": "1234567890",
        },
        node_factory=None,
        now=lambda: datetime.datetime(2026, 9, 19, tzinfo=datetime.timezone.utc),
        out_dir=tmp_path / "report", log=lambda _message: None,
        capability=capability, deadline_secs=10,
    )
    assert code == expected_code
    assert captured["target"].stopped.is_set()
    assert captured["strategy"].wait_calls == 0
    assert all(thread_id == main_thread for thread_id in captured["strategy"].failure_threads)
    report = json.loads((tmp_path / "report" / "trade.json").read_text(encoding="utf-8"))
    assert report["production_execution_verified"] is reconciled_clean


def test_dirty_reconciliation_never_sends_a_real_strategy_to_the_watchdog_thread(
    tmp_path,
):
    script = r"""
import datetime
import pathlib
import sys
import threading

import ondo_trade_probe as trade
from test_ondo_trade_probe import (
    CapableAdapter,
    _LifecycleNode,
    _LifecycleTarget,
    _drive_entry,
    _integration_strategy,
    authorized,
    parse,
    resolved_plan,
)

plan = parse(authorized(resolved_plan()))
limits = trade.load_trade_limits(None)
capability = trade.detect_native_trade_capability(CapableAdapter())

def fake_build(_plan, _adapter, _limits, _provider, _node_factory, _account_id,
               _expected_id, _entry_deadline, _cleanup_deadline, run_id):
    target = _LifecycleTarget(run_id, reconciled_clean=False)
    readiness = trade.read_trade_readiness(target, run_id=run_id)
    target.phase = "reconciled"
    strategy = _integration_strategy()
    strategy._readiness = readiness
    _drive_entry(strategy.sequencer)
    strategy.sequencer.bind_close("C1")
    strategy.sequencer.note_close(trade.OrderView(
        "close", "C1", "FILLED", trade.Decimal("0.100"), trade.Decimal("100.00"), True,
    ), now=0.0)
    strategy.done_event.set()
    return _LifecycleNode(target), target, {}, strategy

def fake_bounded_stop(target, **_kwargs):
    target.stopped.set()
    return {"attempted": True, "requested": True, "stopped": True, "error": None}

trade._build_node = fake_build
trade.resolve_stop_target = lambda node: (node.target, "fake")
trade.bounded_stop = fake_bounded_stop
trade.read_native_diagnostics = lambda *_a, **_k: None
code = trade.execute(
    plan, limits, adapter=CapableAdapter(),
    environ={
        "ONDO_MAINNET_API_KEY": "sentinel-key",
        "ONDO_MAINNET_API_SECRET": "sentinel-secret",
        "ONDO_MAINNET_ACCOUNT_ID": "1234567890",
    },
    node_factory=None,
    now=lambda: datetime.datetime(2026, 9, 20, tzinfo=datetime.timezone.utc),
    out_dir=pathlib.Path(sys.argv[1]), log=lambda _message: None,
    capability=capability, deadline_secs=10,
)
raise SystemExit(0 if code == 1 else 7)
"""
    env = dict(os.environ)
    extra = os.pathsep.join([
        str(Path(__file__).resolve().parents[1] / "src"),
        str(Path(__file__).resolve().parent),
    ])
    env["PYTHONPATH"] = extra + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-c", script, str(tmp_path / "subprocess-report")],
        cwd=Path(__file__).resolve().parents[1],
        env=env,
        text=True,
        capture_output=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_execute_cancels_the_watchdog_when_the_node_fails_early(monkeypatch, tmp_path):
    plan = parse(authorized(resolved_plan()))
    limits = load_trade_limits(None)
    capability = trade.detect_native_trade_capability(CapableAdapter())
    target_box = {}

    class EarlyNode:
        def __init__(self, target):
            self.target = target

        def run(self):
            return False

    def fake_build(_plan, _adapter, _limits, _provider, _node_factory, _account_id,
                   _expected_id, _entry_deadline, _cleanup_deadline, run_id):
        target = _LifecycleTarget(run_id, reconciled_clean=False)
        readiness = trade.read_trade_readiness(target, run_id=run_id)
        strategy = _LifecycleStrategy(seq_for(), readiness)
        strategy.done_event.clear()
        target_box["target"] = target
        return EarlyNode(target), target, {}, strategy

    def fake_bounded_stop(target, **_kwargs):
        target.stopped.set()
        return {
            "attempted": True, "requested": True, "stopped": True, "error": None,
        }

    monkeypatch.setattr(trade, "_build_node", fake_build)
    monkeypatch.setattr(trade, "resolve_stop_target", lambda node: (node.target, "fake"))
    monkeypatch.setattr(trade, "bounded_stop", fake_bounded_stop)
    monkeypatch.setattr(trade, "read_native_diagnostics", lambda *_a, **_k: None)
    started = time.monotonic()
    code = trade.execute(
        plan, limits, adapter=CapableAdapter(),
        environ={
            "ONDO_MAINNET_API_KEY": "sentinel-key",
            "ONDO_MAINNET_API_SECRET": "sentinel-secret",
            "ONDO_MAINNET_ACCOUNT_ID": "1234567890",
        },
        node_factory=None,
        now=lambda: datetime.datetime(2026, 9, 20, tzinfo=datetime.timezone.utc),
        out_dir=tmp_path / "report", log=lambda _message: None,
        capability=capability, deadline_secs=10,
    )
    assert code == 1
    assert time.monotonic() - started < 2.0
    assert target_box["target"].stopped.is_set()


def test_build_time_consumes_the_absolute_entry_deadline(monkeypatch, tmp_path):
    document = authorized(resolved_plan())
    document["envelope"]["deadline_secs"] = 2
    document["cleanup"]["stop_budget_secs"] = 1
    rebind_hashes(document)
    plan = parse(document)
    limits = load_trade_limits(None)
    capability = trade.detect_native_trade_capability(CapableAdapter())

    def fake_build(_plan, _adapter, _limits, _provider, _node_factory, _account_id,
                   _expected_id, _entry_deadline, _cleanup_deadline, run_id):
        time.sleep(1.05)
        target = _LifecycleTarget(run_id, reconciled_clean=True)
        readiness = trade.read_trade_readiness(target, run_id=run_id)
        target.phase = "reconciled"
        strategy = _LifecycleStrategy(seq_for(), readiness)
        strategy.done_event.clear()
        return _LifecycleNode(target), target, {}, strategy

    def fake_bounded_stop(target, **_kwargs):
        target.stopped.set()
        return {
            "attempted": True, "requested": True, "stopped": True, "error": None,
        }

    monkeypatch.setattr(trade, "_build_node", fake_build)
    monkeypatch.setattr(trade, "resolve_stop_target", lambda node: (node.target, "fake"))
    monkeypatch.setattr(trade, "bounded_stop", fake_bounded_stop)
    monkeypatch.setattr(trade, "read_native_diagnostics", lambda *_a, **_k: None)
    started = time.monotonic()
    code = trade.execute(
        plan, limits, adapter=CapableAdapter(),
        environ={
            "ONDO_MAINNET_API_KEY": "sentinel-key",
            "ONDO_MAINNET_API_SECRET": "sentinel-secret",
            "ONDO_MAINNET_ACCOUNT_ID": "1234567890",
        },
        node_factory=None,
        now=lambda: datetime.datetime(2026, 9, 20, tzinfo=datetime.timezone.utc),
        out_dir=tmp_path / "report", log=lambda _message: None,
        capability=capability, deadline_secs=2,
    )
    assert code == trade.EXIT_TIMEOUT
    assert time.monotonic() - started < 1.7


def test_early_return_cancels_an_active_reconciliation_poll_before_publish(
    monkeypatch, tmp_path,
):
    plan = parse(authorized(resolved_plan()))
    limits = load_trade_limits(None)
    capability = trade.detect_native_trade_capability(CapableAdapter())
    poll_started = threading.Event()
    stopped = threading.Event()

    class PollingTarget:
        def production_trade_snapshot(self):
            poll_started.set()
            return None

    target = PollingTarget()

    class EarlyNode:
        def run(self):
            assert poll_started.wait(1.0)
            return False

    def fake_build(*_args, **_kwargs):
        strategy = _LifecycleStrategy(seq_for(), good_readiness())
        return EarlyNode(), target, {}, strategy

    def fake_bounded_stop(_target, **_kwargs):
        stopped.set()
        return {
            "attempted": True, "requested": True, "stopped": True, "error": None,
        }

    monkeypatch.setattr(trade, "_build_node", fake_build)
    monkeypatch.setattr(trade, "resolve_stop_target", lambda _node: (target, "fake"))
    monkeypatch.setattr(trade, "bounded_stop", fake_bounded_stop)
    monkeypatch.setattr(trade, "read_native_diagnostics", lambda *_a, **_k: None)
    started = time.monotonic()
    code = trade.execute(
        plan, limits, adapter=CapableAdapter(),
        environ={
            "ONDO_MAINNET_API_KEY": "sentinel-key",
            "ONDO_MAINNET_API_SECRET": "sentinel-secret",
            "ONDO_MAINNET_ACCOUNT_ID": "1234567890",
        },
        node_factory=None,
        now=lambda: datetime.datetime(2026, 9, 20, tzinfo=datetime.timezone.utc),
        out_dir=tmp_path / "report", log=lambda _message: None,
        capability=capability, deadline_secs=10,
    )
    assert code == 1
    assert stopped.is_set()
    assert time.monotonic() - started < 1.5
    report = json.loads((tmp_path / "report" / "trade.json").read_text(encoding="utf-8"))
    assert report["watchdog"]["worker_alive_after_join"] is False

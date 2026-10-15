"""Offline authority and freshness boundaries for native whole-account evidence."""
from decimal import Decimal as D
import json
from pathlib import Path
import sys
from types import SimpleNamespace as NS

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_account_reader import AccountReadError, ReadOnlyAccountReader


def market(venue, symbol="BTC"):
    instrument = (symbol + "-USD-PERP.ONDO" if venue == "ONDO" else symbol + "_USDC_PERP.BACKPACK")
    return NS(venue=venue, instrument_id=instrument, size_increment=D("0.00001"),
              price_increment=D("0.1"), min_quantity=D("0.00001"),
              min_notional=D("1"), multiplier=D(1), quote_to_usd=D(1))


def evidence(native_venue, **updates):
    result = {"schema_version": 1, "venue": native_venue, "environment": "mainnet",
              "snapshot_started_ns": 50, "snapshot_completed_ns": 70, "epoch": 1,
              "complete": True, "connected": True, "positions": [], "open_orders": [],
              "unknown_submissions": 0}
    result.update(updates)
    return result


def reader():
    class Poison(dict):
        def get(self, *args):
            raise AssertionError("native evidence must not read credentials")
    def transport(*args):
        raise AssertionError("native evidence must not issue independent requests")
    return ReadOnlyAccountReader(environ=Poison(), transport=transport, clock_ns=lambda: 100)


@pytest.mark.parametrize("venue", ["ONDO", "BACKPACK"])
def test_native_account_shares_whole_account_fence_and_copies_mutable_payload(venue):
    r = reader()
    btc, eth = market(venue), market(venue, "ETH")
    payload = evidence(venue, positions=[{"instrument_id": btc.instrument_id, "quantity": "-0.002"}],
        open_orders=[{"instrument_id": btc.instrument_id, "client_order_id": "owned-1", "venue_order_id": "1"},
                     {"instrument_id": eth.instrument_id, "client_order_id": None, "venue_order_id": "2"}])
    r.capture_native_evidence(venue, payload)
    payload["positions"].clear()
    payload["open_orders"][0]["client_order_id"] = "mutated"
    first = r.read(btc, D("100"), D("101"), 90)
    second = r.read_native(eth, D("100"), D("101"), 95)
    assert first.position_quantity == D("-.002")
    assert first.open_order_ids == ("owned-1",)
    assert second.position_quantity == 0 and second.open_order_ids == ("venue:2",)
    assert first.observed_ns == second.observed_ns == 50
    assert first.book_observed_ns == 90 and second.book_observed_ns == 95
    assert first.authoritative and second.authoritative
    assert first.account_positions == second.account_positions == ((btc.instrument_id, D("-.002")),)
    assert first.account_open_orders == second.account_open_orders == (
        (btc.instrument_id, "owned-1"), (eth.instrument_id, "venue:2"))


@pytest.mark.parametrize("venue", ["ONDO", "BACKPACK"])
def test_native_absent_target_is_flat_only_after_complete_account_capture(venue):
    r = reader()
    with pytest.raises(AccountReadError, match="captured"):
        r.read(market(venue), D(100), D(101), 90)
    r.capture_native_evidence(venue, json.dumps(evidence(venue)))
    assert r.read(market(venue), D(100), D(101), 90).position_quantity == 0
    r.invalidate(venue)
    with pytest.raises(AccountReadError, match="captured"):
        r.read(market(venue), D(100), D(101), 90)


@pytest.mark.parametrize("venue", ["ONDO", "BACKPACK"])
def test_native_unknown_submit_never_certifies_flat(venue):
    r = reader()
    r.capture_native_evidence(venue, evidence(venue, unknown_submissions=1))
    result = r.read(market(venue), D(100), D(101), 90)
    assert result.position_quantity == 0 and not result.authoritative


@pytest.mark.parametrize("updates", [
    {"complete": False}, {"connected": False}, {"environment": "testnet"}, {"venue": "ONDO"},
    {"snapshot_started_ns": 0}, {"snapshot_started_ns": 71}, {"snapshot_completed_ns": 101},
    {"epoch": True}, {"epoch": -1}, {"unknown_submissions": None}, {"unknown_submissions": True},
    {"unknown_submissions": -1}, {"positions": None}, {"open_orders": None},
    {"positions": [{"instrument_id": "BTC_USDC_PERP.BACKPACK", "quantity": 0.001}]},
    {"positions": [{"instrument_id": "BTC_USDC_PERP.BACKPACK", "quantity": "NaN"}]},
    {"positions": [{"instrument_id": "BTC-USD-PERP.ONDO", "quantity": "0"}]},
    {"positions": [{"instrument_id": "BTC_USDC_PERP.BACKPACK", "quantity": "0"}]*2},
    {"open_orders": [{"instrument_id": "BTC_USDC_PERP.BACKPACK", "client_order_id": None}]},
    {"open_orders": [{"instrument_id": "BTC_USDC_PERP.BACKPACK", "client_order_id": "", "venue_order_id": "1"}]},
    {"open_orders": [{"instrument_id": "BTC_USDC_PERP.BACKPACK", "client_order_id": None, "venue_order_id": "1"}]*2},
])
def test_bad_native_update_invalidates_previous_flat_evidence(updates):
    r = reader()
    r.capture_native_evidence("BACKPACK", evidence("BACKPACK"))
    with pytest.raises(AccountReadError):
        r.capture_native_evidence("BACKPACK", evidence("BACKPACK", **updates))
    with pytest.raises(AccountReadError, match="captured"):
        r.read(market("BACKPACK"), D(100), D(101), 90)


def test_native_stale_start_fence_not_rejuvenated_by_completion_or_poll():
    r = ReadOnlyAccountReader(clock_ns=lambda: 20_000_000_100, max_native_snapshot_age_ms=15000)
    r.capture_native_evidence("BACKPACK", evidence("BACKPACK", snapshot_completed_ns=20_000_000_090))
    with pytest.raises(AccountReadError, match="stale"):
        r.read(market("BACKPACK"), D(100), D(101), 20_000_000_100)


@pytest.mark.parametrize("updates", [{"epoch": 0}, {"snapshot_started_ns": 49}])
def test_native_replayed_snapshot_fails_closed(updates):
    r = reader()
    r.capture_native_evidence("BACKPACK", evidence("BACKPACK"))
    with pytest.raises(AccountReadError, match="regressed"):
        r.capture_native_evidence("BACKPACK", evidence("BACKPACK", **updates))


@pytest.mark.parametrize("bid,ask,observed", [(D(100), D(100), 90), (D(0), D(1), 90),
                                             (D(100), D(101), 101), (D(100), D(101), 0)])
def test_native_market_invalid_bbo_and_timestamp_are_rejected(bid, ask, observed):
    r = reader()
    r.capture_native_evidence("BACKPACK", evidence("BACKPACK"))
    with pytest.raises(AccountReadError):
        r.read(market("BACKPACK"), bid, ask, observed)


@pytest.mark.parametrize("name,value", [("size_increment", "0"), ("price_increment", "0"),
                                       ("min_quantity", "-1"), ("min_notional", None),
                                       ("multiplier", "0"), ("quote_to_usd", "NaN")])
def test_native_instrument_execution_bounds_are_required(name, value):
    r = reader()
    r.capture_native_evidence("BACKPACK", evidence("BACKPACK"))
    m = market("BACKPACK")
    setattr(m, name, value)
    with pytest.raises(AccountReadError):
        r.read(m, D(100), D(101), 90)


def test_native_position_units_must_match_native_size_step():
    r = reader()
    m = market("BACKPACK")
    r.capture_native_evidence("BACKPACK", evidence("BACKPACK", positions=[
        {"instrument_id": m.instrument_id, "quantity": "0.000001"}]))
    with pytest.raises(AccountReadError, match="size increment"):
        r.read(m, D(100), D(101), 90)


def test_owner_copied_native_instrument_metadata_serves_scan_market_mapping():
    r = reader()
    m = NS(venue="BACKPACK", instrument_id="BTC_USDC_PERP.BACKPACK", quote_to_usd=D(1))
    r.register_native_instrument(m.instrument_id, size_increment=D("0.00001"),
        price_increment=D("0.1"), min_quantity=D("0.00001"), min_notional=D(1))
    r.capture_native_evidence("BACKPACK", evidence("BACKPACK"))
    assert r.read(m, D(100), D(101), 90).position_quantity == 0


def test_native_schema_boolean_and_price_off_tick_fail_closed():
    r = reader()
    with pytest.raises(AccountReadError):
        r.capture_native_evidence("BACKPACK", evidence("BACKPACK", schema_version=True))
    r.capture_native_evidence("BACKPACK", evidence("BACKPACK"))
    with pytest.raises(AccountReadError, match="price increment"):
        r.read(market("BACKPACK"), D("100.01"), D("101"), 90)


def ondo_pass(**updates):
    result = {"schema_version": 1, "venue": "ONDO", "mode": "continuous", "run_id": "test-run",
              "snapshot_started_ns": 50, "snapshot_completed_ns": 70, "snapshot_fresh": True,
              "latest_activity_generation": 3, "reconciled_activity_generation": 3,
              "coverage_complete": True, "connected_ready": True, "position_quantities": {},
              "open_orders": [], "unknown_submissions": 0}
    result.update(updates)
    return result


def test_ondo_factory_pass_requires_explicit_config_environment_and_preserves_signed_quantities():
    r = reader()
    m = market("ONDO")
    payload = ondo_pass(position_quantities={m.instrument_id: "-.002"}, open_orders=[
        {"instrument_id": m.instrument_id, "client_order_id": "native-owned", "venue_order_id": "8"}])
    with pytest.raises(AccountReadError, match="configured environment"):
        r.capture_native_evidence("ONDO", payload)
    r.capture_native_evidence("ONDO", payload, native_environment="mainnet")
    result = r.read(m, D(100), D(101), 90)
    assert result.observed_ns == 50 and result.book_observed_ns == 90
    assert result.position_quantity == D("-.002") and result.open_order_ids == ("native-owned",)


@pytest.mark.parametrize("updates", [
    {"schema_version": True}, {"venue": "BACKPACK"}, {"environment": "testnet"},
    {"latest_activity_generation": True}, {"reconciled_activity_generation": 2},
    {"snapshot_fresh": False}, {"coverage_complete": False}, {"connected_ready": False},
    {"run_id": ""}, {"position_quantities": None}, {"snapshot_started_ns": None},
    {"open_orders": None}, {"unknown_submissions": None},
])
def test_ondo_native_factory_incomplete_or_raced_pass_rejected(updates):
    r = reader()
    with pytest.raises(AccountReadError):
        r.capture_native_evidence("ONDO", ondo_pass(**updates), native_environment="mainnet")


def test_ondo_factory_run_binding_survives_order_invalidation():
    r = reader()
    r.capture_native_evidence("ONDO", ondo_pass(), native_environment="mainnet")
    r.invalidate("ONDO")
    with pytest.raises(AccountReadError, match="run binding changed"):
        r.capture_native_evidence("ONDO", ondo_pass(run_id="other-run"), native_environment="mainnet")


def test_native_environment_may_only_come_from_explicit_owner_config_binding():
    r = reader()
    payload = evidence("BACKPACK")
    payload.pop("environment")
    with pytest.raises(AccountReadError, match="environment mismatch"):
        r.capture_native_evidence("BACKPACK", payload)
    r.capture_native_evidence("BACKPACK", payload, native_environment="mainnet")
    assert r.read(market("BACKPACK"), D(100), D(101), 90).authoritative
    with pytest.raises(AccountReadError, match="environment mismatch"):
        r.capture_native_evidence("BACKPACK", payload, native_environment="testnet")
    with pytest.raises(AccountReadError, match="environment mismatch"):
        r.capture_native_evidence("BACKPACK", evidence("BACKPACK", environment="testnet"), native_environment="mainnet")

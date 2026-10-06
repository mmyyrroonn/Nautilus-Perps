"""Arithmetic, fail-closed evidence, and event-only recording contracts."""

import json
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal, Inexact, Rounded, localcontext

import pytest

from src.opportunity_core import BookSnapshot, EventRecorder, MarketMetadata, ScanSettings, evaluate_opportunity


D = Decimal
NOW = 10_000_000_000


def book(venue, bid="99", ask="100", quantity="10", **kwargs):
    return BookSnapshot(
        venue=venue,
        symbol="BTC",
        bids=((D(bid), D(quantity)),),
        asks=((D(ask), D(quantity)),),
        ts_event_ns=NOW - 100_000_000,
        ts_received_ns=NOW - 50_000_000,
        **kwargs,
    )


def metadata(venue, **kwargs):
    return MarketMetadata(
        instrument_id=f"BTC.{venue}",
        size_increment=kwargs.pop("size_increment", D("0.1")),
        taker_fee_bps=kwargs.pop("taker_fee_bps", D("2")),
        fee_source=kwargs.pop("fee_source", "instrument"),
        **kwargs,
    )


def estimate(buy=None, sell=None, buy_metadata=None, sell_metadata=None, settings=None):
    return evaluate_opportunity(
        buy if buy is not None else book("BUY"),
        buy_metadata if buy_metadata is not None else metadata("BUY"),
        sell if sell is not None else book("SELL", "103", "104"),
        sell_metadata if sell_metadata is not None else metadata("SELL"),
        settings if settings is not None else ScanSettings(),
        NOW,
    )


def test_walks_both_books_and_uses_cash_specific_fees():
    buy = replace(book("BUY"), asks=((D("100"), D("2")), (D("101"), D("10"))))
    sell = replace(book("SELL", "103", "104"), bids=((D("103"), D("1")), (D("102"), D("10"))))
    event = estimate(buy, sell)
    assert event is not None
    sizing = event["sizing"]
    economics = event["economics"]
    assert D(sizing["base_quantity"]) == D("4.9")
    assert D(sizing["buy_notional"]) == D("492.9")
    assert D(sizing["sell_notional"]) == D("500.8")
    assert D(sizing["buy_notional"]) <= D(sizing["target_notional"])
    assert D(economics["entry_gross"]) == D("7.9")
    assert D(economics["buy_taker_fee"]) == D("0.09858")
    assert D(economics["sell_taker_fee"]) == D("0.10016")
    assert D(economics["reserve"]) == D("0.24645")
    assert D(economics["entry_after_fees_and_reserve"]) == D("7.45481")
    assert event["estimate_type"] == "entry_only"
    assert event["full_round_trip_profitability"] == "unknown"
    assert json.loads(json.dumps(event)) == event


def test_multiplier_and_lcm_produce_neutral_executable_native_sizes():
    event = estimate(
        buy_metadata=metadata("BUY", size_increment=D("0.003"), multiplier=D("2"), taker_fee_bps=D("0")),
        sell_metadata=metadata("SELL", size_increment=D("0.004"), multiplier=D("3"), taker_fee_bps=D("0")),
        settings=ScanSettings(target_notional=D("2"), reserve_bps=D("0")),
    )
    assert event is not None
    sizing = event["sizing"]
    assert D(sizing["common_base_step"]) == D("0.012")
    assert D(sizing["base_quantity"]) == D("0.012")
    assert D(sizing["buy_quantity"]) == D("0.006")
    assert D(sizing["sell_quantity"]) == D("0.004")
    assert D(sizing["buy_quantity"]) * 2 == D(sizing["sell_quantity"]) * 3
    assert D(sizing["buy_quantity"]) % D("0.003") == 0
    assert D(sizing["sell_quantity"]) % D("0.004") == 0
    assert D(sizing["buy_notional"]) == D("1.2")
    assert D(sizing["sell_notional"]) == D("1.236")


def test_positive_spread_below_total_costs_skips_depth_walk(monkeypatch):
    import src.opportunity_core as core
    def unexpected_walk(*args):
        raise AssertionError("best quotes cannot pay fees and reserve")
    monkeypatch.setattr(core, "_walk", unexpected_walk)
    assert estimate(sell=book("SELL", "100.01", "101")) is None


def test_fee_upper_bound_preserves_exact_threshold_equality_under_low_precision():
    settings = ScanSettings(min_entry_edge_bps=D("2"), reserve_bps=D("0"))
    with localcontext() as context:
        context.prec = 2
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        event = estimate(sell=book("SELL", "100.02", "101"),
                         buy_metadata=metadata("BUY", taker_fee_bps=D("0")),
                         sell_metadata=metadata("SELL", taker_fee_bps=D("0")), settings=settings)
    assert event is not None
    assert D(event["economics"]["entry_after_fees_and_reserve_bps"]) == D("2")


@pytest.mark.parametrize("leg,field,value", [
    ("BUY", "min_quantity", "0.007"),
    ("SELL", "min_quantity", "0.005"),
    ("BUY", "min_notional", "1.21"),
    ("SELL", "min_notional", "1.237"),
])
def test_each_leg_minimum_is_checked_in_its_native_units(leg, field, value):
    buy_meta = metadata("BUY", size_increment=D("0.003"), multiplier=D("2"))
    sell_meta = metadata("SELL", size_increment=D("0.004"), multiplier=D("3"))
    if leg == "BUY":
        buy_meta = replace(buy_meta, **{field: D(value)})
    else:
        sell_meta = replace(sell_meta, **{field: D(value)})
    assert estimate(
        buy_metadata=buy_meta, sell_metadata=sell_meta,
        settings=ScanSettings(target_notional=D("2")),
    ) is None


def test_minima_accept_equality():
    assert estimate(
        buy_metadata=metadata("BUY", min_quantity=D("5"), min_notional=D("500")),
        sell_metadata=metadata("SELL", min_quantity=D("5"), min_notional=D("515")),
    ) is not None


def test_no_partial_or_extrapolated_depth_can_qualify():
    assert estimate(buy=book("BUY", quantity="4")) is None
    assert estimate(sell=book("SELL", "103", "104", quantity="4.999")) is None
    # A fully covered budget at the final available level is sufficient.
    assert estimate(buy=book("BUY", quantity="5")) is not None


def test_unaffordable_common_step_rejects():
    assert estimate(
        buy_metadata=metadata("BUY", size_increment=D("10")),
        sell_metadata=metadata("SELL", size_increment=D("10")),
    ) is None


@pytest.mark.parametrize("change", [
    {"valid": False},
    {"bids": ()},
    {"asks": ()},
    {"bids": ((D("100"), D("10")),)},
    {"bids": ((D("101"), D("10")),)},
    {"bids": ((D("98"), D("10")), (D("99"), D("10")))},
    {"asks": ((D("101"), D("10")), (D("100"), D("10")))},
    {"asks": ((D("100"), D("10")), (D("100"), D("10")))},
    {"asks": ((D("NaN"), D("10")),)},
    {"bids": ((D("99"), D("Infinity")),)},
    {"asks": ((D("100"), D("0")),)},
    {"asks": ((D("-100"), D("10")),)},
    {"asks": ((100, D("10")),)},
    {"asks": ((D("100"),),)},
    {"coverage_limit": 0},
    {"coverage_limit": 1, "asks": ((D("100"), D("10")), (D("101"), D("10")))},
    {"ts_event_ns": 0},
    {"ts_event_ns": NOW - 2_000_000_001, "ts_received_ns": NOW},
    {"ts_received_ns": NOW + 1},
    {"ts_received_ns": NOW - 200_000_000},
])
def test_invalid_buy_book_fails_closed(change):
    assert estimate(buy=replace(book("BUY"), **change)) is None


def test_sell_book_is_validated_independently():
    assert estimate(sell=replace(book("SELL", "103", "104"), valid=False)) is None
    assert estimate(sell=book("SELL", "104", "103")) is None


def test_fresh_receive_does_not_hide_stale_event_or_source_delay():
    stale = replace(book("BUY"), ts_event_ns=NOW - 3_000_000_000, ts_received_ns=NOW)
    assert estimate(buy=stale) is None


def test_event_skew_and_receive_age_have_independent_limits():
    skewed = replace(book("SELL", "103", "104"), ts_event_ns=NOW - 600_000_001)
    assert estimate(sell=skewed) is None
    assert estimate(settings=ScanSettings(max_receive_age_ms=49)) is None
    assert estimate(settings=ScanSettings(max_age_ms=100, max_receive_age_ms=50)) is not None


@pytest.mark.parametrize("change", [
    {"taker_fee_bps": None},
    {"taker_fee_bps": D("NaN")},
    {"taker_fee_bps": D("-1")},
    {"fee_source": ""},
    {"fee_source": "unknown"},
    {"size_increment": D("0")},
    {"multiplier": D("Infinity")},
    {"min_notional": D("NaN")},
])
def test_missing_or_invalid_metadata_is_rejected_on_either_leg(change):
    assert estimate(buy_metadata=replace(metadata("BUY"), **change)) is None
    assert estimate(sell_metadata=replace(metadata("SELL"), **change)) is None


def test_fees_and_reserve_can_remove_a_gross_opportunity():
    assert estimate(buy_metadata=metadata("BUY", taker_fee_bps=D("400"))) is None
    assert estimate(settings=ScanSettings(reserve_bps=D("400"))) is None


def test_exact_threshold_boundary_and_default_strict_positive():
    no_fee_buy = metadata("BUY", taker_fee_bps=D("0"))
    no_fee_sell = metadata("SELL", taker_fee_bps=D("0"))
    boundary_sell = book("SELL", "100.1", "101")
    settings = ScanSettings(reserve_bps=D("0"), min_entry_edge_bps=D("10"))
    event = estimate(sell=boundary_sell, buy_metadata=no_fee_buy, sell_metadata=no_fee_sell, settings=settings)
    assert event is not None
    assert D(event["economics"]["entry_after_fees_and_reserve_bps"]) == D("10")
    assert estimate(
        sell=boundary_sell, buy_metadata=no_fee_buy, sell_metadata=no_fee_sell,
        settings=replace(settings, min_entry_edge_bps=D("10.0000000000000000000000000000000000001")),
    ) is None
    assert estimate(
        sell=book("SELL", "100", "101"), buy_metadata=no_fee_buy, sell_metadata=no_fee_sell,
        settings=replace(settings, min_entry_edge_bps=D("0")),
    ) is None


def test_arithmetic_is_independent_of_callers_decimal_precision():
    expected = estimate()
    with localcontext() as context:
        context.prec = 3
        actual = estimate()
    assert actual == expected


def test_repeating_display_ratios_do_not_inherit_callers_decimal_traps():
    buy = book("BUY", "2", "3", "1000")
    sell = book("SELL", "4", "5", "1000")
    expected = estimate(buy=buy, sell=sell)
    assert expected is not None
    with localcontext() as context:
        context.prec = 3
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        actual = estimate(buy=buy, sell=sell)
    assert actual == expected


def test_book_evidence_is_detached_and_snapshot_is_frozen():
    asks = [[D("100"), D("10")]]
    snapshot = replace(book("BUY"), asks=asks)
    asks[0][0] = D("900")
    event = estimate(buy=snapshot)
    assert event is not None
    assert event["buy_book"]["asks"] == [["100", "10"]]
    assert event["buy_book"]["ts_event_ns"] == snapshot.ts_event_ns
    assert event["buy_metadata"]["fee_source"] == "instrument"
    assert event["settings"]["target_notional"] == "500"
    with pytest.raises(FrozenInstanceError):
        snapshot.venue = "OTHER"
    event["buy_book"]["asks"][0][0] = "800"
    assert snapshot.asks[0][0] == D("100")
    assert estimate(buy=snapshot)["buy_book"]["asks"] == [["100", "10"]]


def test_no_opportunity_creates_no_output(tmp_path):
    path = tmp_path / "absent" / "events.jsonl"
    recorder = EventRecorder(path)
    for index in range(20):
        assert recorder.observe(("BTC", "BUY", "SELL"), None, NOW + index) is False
    assert not path.parent.exists()
    assert not recorder._active


def test_disabled_recorder_retains_no_evidence_or_dedup_state(tmp_path):
    recorder = EventRecorder(None)
    assert recorder.observe(("BTC", "BUY", "SELL"), estimate(), NOW) is False
    assert not recorder._active
    assert list(tmp_path.iterdir()) == []


def changed_event(edge=None, quantity=None):
    event = estimate()
    if edge is not None:
        event["economics"]["entry_after_fees_and_reserve_bps"] = str(edge)
    if quantity is not None:
        event["sizing"]["base_quantity"] = str(quantity)
    return event


def test_recorder_onset_cooldown_change_and_disappearance_reset(tmp_path):
    path = tmp_path / "evidence" / "events.jsonl"
    key = ("BTC", "BUY", "SELL")
    recorder = EventRecorder(path)
    first = estimate()
    edge = D(first["economics"]["entry_after_fees_and_reserve_bps"])
    assert not path.exists()
    assert recorder.observe(key, first, NOW)
    changed = changed_event(edge=edge + 2)
    assert not recorder.observe(key, changed, NOW + 4_999_999_999)
    assert recorder.observe(key, changed, NOW + 5_000_000_000)
    assert not recorder.observe(key, changed, NOW + 50_000_000_000)
    assert not recorder.observe(key, None, NOW + 50_000_000_001)
    assert not recorder._active
    assert recorder.observe(key, changed, NOW + 50_000_000_002)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 3
    assert rows[0]["key"] == list(key)
    assert rows[0]["opportunity"] == first
    assert rows[0]["recorded_at_ns"] == NOW
    assert len(recorder._active) == 1


def test_recorder_quantity_change_and_optional_periodic_save(tmp_path):
    key = ("BTC", "BUY", "SELL")
    recorder = EventRecorder(tmp_path / "events.jsonl", max_interval_ms=10_000)
    first = estimate()
    assert recorder.observe(key, first, NOW)
    assert not recorder.observe(key, first, NOW + 5_000_000_000)
    assert not recorder.observe(key, changed_event(quantity=D("5.49")), NOW + 5_000_000_000)
    assert recorder.observe(key, changed_event(quantity=D("5.5")), NOW + 5_000_000_000)
    assert not recorder.observe(key, changed_event(quantity=D("5.5")), NOW + 14_999_999_999)
    assert recorder.observe(key, changed_event(quantity=D("5.5")), NOW + 15_000_000_000)


def test_zero_change_thresholds_still_require_an_actual_change(tmp_path):
    recorder = EventRecorder(
        tmp_path / "events.jsonl", edge_change_bps=D("0"), quantity_change_fraction=D("0"),
    )
    key = ("BTC", "BUY", "SELL")
    first = estimate()
    assert recorder.observe(key, first, NOW)
    assert not recorder.observe(key, first, NOW + 5_000_000_000)
    assert recorder.observe(key, changed_event(quantity=D("5.0001")), NOW + 5_000_000_000)


def test_recorder_remembers_only_saved_statistics_not_mutable_events(tmp_path):
    recorder = EventRecorder(tmp_path / "events.jsonl")
    key = ("BTC", "BUY", "SELL")
    event = estimate()
    assert recorder.observe(key, event, NOW)
    event["sizing"]["base_quantity"] = "999"
    assert not recorder.observe(key, estimate(), NOW + 5_000_000_000)
    assert D(json.loads((tmp_path / "events.jsonl").read_text())["opportunity"]["sizing"]["base_quantity"]) == D("5")


def test_bad_evidence_does_not_create_output_or_advance_dedup(tmp_path):
    path = tmp_path / "absent" / "events.jsonl"
    recorder = EventRecorder(path)
    event = estimate()
    event["unserializable"] = object()
    with pytest.raises(TypeError):
        recorder.observe(("BTC", "BUY", "SELL"), event, NOW)
    assert not path.parent.exists()
    assert not recorder._active


@pytest.mark.parametrize("kwargs", [
    {"cooldown_ms": -1},
    {"edge_change_bps": D("NaN")},
    {"quantity_change_fraction": D("-0.1")},
    {"max_interval_ms": 1},
])
def test_recorder_rejects_invalid_configuration(kwargs):
    with pytest.raises(ValueError):
        EventRecorder(None, **kwargs)

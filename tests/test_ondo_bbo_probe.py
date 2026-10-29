import importlib.util
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location(
    "ondo_bbo_probe", Path(__file__).resolve().parents[1] / "scripts/probe_ondo_bbo_latency.py",
)
probe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(probe)


def snapshot(stamp="2026-10-28T10:00:00.123456789Z", quantity="1"):
    return {"market": "BTC-USD.P", "time": stamp,
            "bids": [["100", quantity]], "asks": [["101", "2"]]}


@pytest.mark.parametrize("first,expected", [(probe.BBO, 2), (probe.DEPTH, -2)])
def test_exact_state_pair_and_signed_latency(first, expected):
    other = probe.DEPTH if first == probe.BBO else probe.BBO
    matcher = probe.Matcher()
    matcher.add(first, snapshot(), 1000)
    second = snapshot(quantity="1.000")
    second["bids"][0][0] = "100.000"
    matcher.add(other, second, 3000)
    result = matcher.result(4000)["BTC-USD.P"]
    assert result["depth_minus_bbo_receipt"]["p50_us"] == expected
    assert result["matched_fraction"] == {probe.BBO: 1, probe.DEPTH: 1}


def test_same_price_different_event_or_size_does_not_pair():
    matcher = probe.Matcher()
    matcher.add(probe.BBO, snapshot(), 1000)
    matcher.add(probe.DEPTH, snapshot(stamp="2026-10-28T10:00:01Z"), 2000)
    matcher.add(probe.DEPTH, snapshot(quantity="3"), 3000)
    result = matcher.result(4000)["BTC-USD.P"]
    assert result["depth_minus_bbo_receipt"] == {"samples": 0}
    assert result["counts"]["unmatched_" + probe.DEPTH] == 2


def test_duplicates_before_and_after_pair_are_not_new_samples():
    matcher = probe.Matcher()
    for channel, now in [(probe.BBO, 1000), (probe.BBO, 2000), (probe.DEPTH, 3000),
                         (probe.BBO, 4000), (probe.DEPTH, 5000)]:
        matcher.add(channel, snapshot(), now)
    result = matcher.result(6000)["BTC-USD.P"]
    assert result["depth_minus_bbo_receipt"]["samples"] == 1
    assert result["depth_minus_bbo_receipt"]["p50_us"] == 2
    assert result["counts"]["duplicate_" + probe.BBO] == 2


def test_capacity_ttl_and_sample_bound_report_losses():
    matcher = probe.Matcher(capacity=1, ttl_ns=100, sample_limit=1)
    matcher.add(probe.BBO, snapshot(stamp="a"), 1)
    matcher.add(probe.BBO, snapshot(stamp="b"), 2)
    assert len(matcher.events) == 1
    matcher.add(probe.DEPTH, snapshot(stamp="b"), 3)
    matcher.add(probe.BBO, snapshot(stamp="c"), 200)
    matcher.add(probe.DEPTH, snapshot(stamp="c"), 201)
    result = matcher.result(202)["BTC-USD.P"]
    assert result["counts"]["capacity_evictions"] == 1
    assert result["counts"]["matched"] == 2
    assert result["counts"]["samples_dropped"] == 1
    assert result["depth_minus_bbo_receipt"]["samples"] == 1


@pytest.mark.parametrize("quantity", ["0", "-1", "NaN", "Infinity"])
def test_bad_best_quantity_rejected(quantity):
    with pytest.raises(ValueError):
        probe.snapshot_key(snapshot(quantity=quantity))


def prices(bid):
    item = snapshot()
    item["bids"][0][0] = str(bid)
    return item


def test_price_transition_excludes_size_only_and_initial_states():
    transitions = probe.PriceTransitions()
    transitions.add(probe.BBO, prices(99), 1000)
    transitions.add(probe.DEPTH, prices(99), 2000)
    transitions.add(probe.BBO, prices(100), 3000)
    transitions.add(probe.BBO, snapshot(quantity="3"), 3500)
    transitions.add(probe.DEPTH, prices(100), 5000)
    result = transitions.result()["markets"]["BTC-USD.P"]
    assert result["observed_changes"] == {probe.BBO: 1, probe.DEPTH: 1}
    assert result["depth_minus_bbo_receipt"]["p50_us"] == 2


def test_revisited_transition_is_excluded_even_after_initial_match():
    transitions = probe.PriceTransitions()
    for channel in probe.CHANNELS:
        transitions.add(channel, prices(99), 1000)
        transitions.add(channel, prices(100), 2000)
    transitions.add(probe.BBO, prices(99), 3000)
    transitions.add(probe.BBO, prices(100), 4000)
    result = transitions.result()["markets"]["BTC-USD.P"]
    assert result["counts"]["excluded_repeated_transition_keys"] == 1
    assert result["depth_minus_bbo_receipt"] == {"samples": 0}


def test_skipped_price_state_is_not_a_matching_transition():
    transitions = probe.PriceTransitions()
    for channel in probe.CHANNELS:
        transitions.add(channel, prices(98), 1000)
    transitions.add(probe.BBO, prices(99), 2000)
    transitions.add(probe.BBO, prices(100), 3000)
    transitions.add(probe.DEPTH, prices(100), 4000)
    assert transitions.result()["markets"]["BTC-USD.P"]["depth_minus_bbo_receipt"] == {"samples": 0}


def test_transition_capacity_and_large_gap_reported():
    transitions = probe.PriceTransitions(capacity=1, max_gap_ns=100)
    for channel, now in [(probe.BBO, 1), (probe.DEPTH, 1000)]:
        transitions.add(channel, prices(99), now)
        transitions.add(channel, prices(100), now + 1)
    transitions.add(probe.BBO, prices(98), 2000)
    result = transitions.result()["markets"]["BTC-USD.P"]
    assert len(transitions.transitions) == 1
    assert result["observed_changes"]["capacity_dropped"] == 1
    assert result["counts"]["excluded_gap_over_one_second"] == 1

import importlib.util
from decimal import Decimal
from pathlib import Path
import sys

import pytest

spec = importlib.util.spec_from_file_location(
    "hl_md_probe",
    Path(__file__).resolve().parents[1] / "scripts/probe_hl_md_latency.py",
)
probe = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = probe
spec.loader.exec_module(probe)


def l2_payload(coin="BTC", time_ms=1_000, bid="100", bid_sz="2", ask="101", ask_sz="3", levels=5):
    bids = [{"px": str(Decimal(bid) - i), "sz": bid_sz, "n": 1} for i in range(levels)]
    asks = [{"px": str(Decimal(ask) + i), "sz": ask_sz, "n": 1} for i in range(levels)]
    return {"coin": coin, "time": time_ms, "levels": [bids, asks]}


def bbo_payload(coin="BTC", time_ms=1_000, bid="100", bid_sz="2", ask="101", ask_sz="3"):
    return {
        "coin": coin,
        "time": time_ms,
        "bbo": [
            None if bid is None else {"px": bid, "sz": bid_sz, "n": 1},
            None if ask is None else {"px": ask, "sz": ask_sz, "n": 1},
        ],
    }


def trade_item(coin="BTC", time_ms=1_000, tid=1, side="B", px="101", sz="0.5"):
    return {
        "coin": coin,
        "side": side,
        "px": px,
        "sz": sz,
        "hash": "0xabc",
        "time": time_ms,
        "tid": tid,
        "users": ["0x1", "0x2"],
    }


def test_parse_l2_book_and_validation():
    coin, time_ms, bid_px, bid_sz, ask_px, ask_sz, max_levels = probe.parse_l2_book(l2_payload(levels=5))
    assert (coin, time_ms, bid_px, bid_sz, ask_px, ask_sz, max_levels) == (
        "BTC",
        1_000,
        Decimal("100"),
        Decimal("2"),
        Decimal("101"),
        Decimal("3"),
        5,
    )

    with pytest.raises(ValueError):
        probe.parse_l2_book(l2_payload(bid="101", ask="101"))
    with pytest.raises(ValueError):
        probe.parse_l2_book(l2_payload(bid_sz="0"))
    with pytest.raises(ValueError):
        probe.parse_l2_book({"coin": "BTC", "time": 10, "levels": [[], [{"px": "101", "sz": "1", "n": 1}]]})


def test_parse_bbo_handles_one_sided_and_rejects_malformed():
    assert probe.parse_bbo(bbo_payload(bid=None)) is None
    assert probe.parse_bbo(bbo_payload(ask=None)) is None
    parsed = probe.parse_bbo(bbo_payload())
    assert parsed == ("BTC", 1_000, Decimal("100"), Decimal("2"), Decimal("101"), Decimal("3"))

    with pytest.raises(ValueError):
        probe.parse_bbo(bbo_payload(bid="102", ask="101"))
    with pytest.raises(ValueError):
        probe.parse_bbo({"coin": "BTC", "time": 10, "bbo": [None]})


def test_parse_trades_filters_historical_replay_and_rejects_bad_side():
    items = [
        trade_item(time_ms=900, tid=1),
        trade_item(time_ms=10_000, tid=2, side="A", px="99.5", sz="1.25"),
    ]
    parsed, skipped = probe.parse_trades(items, min_time_ms=5_000)
    assert skipped == 1
    assert parsed == [("BTC", 10_000, 2, "A", Decimal("99.5"), Decimal("1.25"))]

    with pytest.raises(ValueError):
        probe.parse_trades([trade_item(side="X")])


def test_cli_validation_enforces_subscription_and_connection_bounds():
    many_coins = tuple(f"C{i}" for i in range(101))
    with pytest.raises(ValueError, match="safe cap"):
        probe.validate_probe_args(many_coins, seconds=60, warmup=5, race_conns=3)
    with pytest.raises(ValueError, match="race_conns"):
        probe.validate_probe_args(("BTC",), seconds=60, warmup=5, race_conns=5)
    with pytest.raises(ValueError, match="seconds"):
        probe.validate_probe_args(("BTC",), seconds=4, warmup=5, race_conns=3)

    with pytest.raises(SystemExit):
        probe.main(["--coins", ",".join(many_coins), "--race-conns", "3"])


def test_throughput_tracker_counts_bbo_price_vs_size_and_152_extrapolation():
    tracker = probe.ThroughputTracker(num_coins=2)
    tracker.record_frame(0, "bbo", 200, 10_000)
    tracker.record_bbo_event(0, "BTC", Decimal("100"), Decimal("2"), Decimal("101"), Decimal("3"))
    tracker.record_frame(0, "bbo", 200, 10_000)
    tracker.record_bbo_event(0, "BTC", Decimal("100"), Decimal("5"), Decimal("101"), Decimal("3"))
    tracker.record_frame(0, "bbo", 200, 10_000)
    tracker.record_bbo_event(0, "BTC", Decimal("100"), Decimal("5"), Decimal("101"), Decimal("3"))
    tracker.record_frame(0, "bbo", 200, 10_000)
    tracker.record_bbo_event(0, "BTC", Decimal("102"), Decimal("5"), Decimal("103"), Decimal("3"))
    # conn 1 should not inflate single_conn0
    tracker.record_frame(1, "bbo", 200, 10_000)
    tracker.record_bbo_event(1, "BTC", Decimal("102"), Decimal("5"), Decimal("103"), Decimal("3"))

    res = tracker.result(observed_seconds=2.0)
    bbo0 = res["single_conn0"]["bbo"]
    assert bbo0["frames"] == 4
    assert bbo0["events"] == 4
    assert bbo0["bbo_price_changed_events"] == 2
    assert bbo0["bbo_size_only_events"] == 1
    assert bbo0["bbo_unchanged_events"] == 1
    assert bbo0["bbo_price_changed_fraction"] == 0.5
    # 2 events/s over 2 coins * (152 / 2) = 152 msg/s; price-only = 76 msg/s
    assert bbo0["est_152_coins_msg_per_sec"] == 152.0
    assert bbo0["est_152_coins_price_changed_msg_per_sec"] == 76.0
    assert res["all_conns_aggregate"]["bbo"]["frames"] == 5


def test_race_tracker_first_arrival_winner_and_capacity_bounds():
    rt = probe.RaceTracker("bbo", race_conns=3, capacity=2, ttl_ns=1_000_000_000, sample_limit=10)
    k1 = ("BTC", 1000, Decimal("100"), Decimal("1"), Decimal("101"), Decimal("1"))
    # Event 1: conn1 wins at 10ms, conn0 at 25ms, conn2 at 40ms
    assert rt.add(1, k1, 1000, 10_000_000, 1_010_000_000) is True
    assert rt.add(0, k1, 1000, 25_000_000, 1_025_000_000) is False
    assert rt.add(2, k1, 1000, 40_000_000, 1_040_000_000) is False
    # Duplicate on conn1 is ignored
    assert rt.add(1, k1, 1000, 45_000_000, 1_045_000_000) is False

    # Event 2: conn0 wins at 50ms, conn1 at 60ms
    k2 = ("BTC", 1001, Decimal("100"), Decimal("1"), Decimal("101"), Decimal("1"))
    assert rt.add(0, k2, 1001, 50_000_000, 1_051_000_000) is True
    assert rt.add(1, k2, 1001, 60_000_000, 1_061_000_000) is False

    # Event 3 triggers capacity eviction of k1 (capacity=2)
    k3 = ("BTC", 1002, Decimal("100"), Decimal("1"), Decimal("101"), Decimal("1"))
    assert rt.add(2, k3, 1002, 70_000_000, 1_072_000_000) is True

    out = rt.result(80_000_000)
    assert out["unique_events"] == 3
    assert out["capacity_evictions"] == 1
    assert out["events_seen_by_one_conn"] == 1
    assert out["events_seen_by_multi_conns"] == 2
    assert out["events_seen_by_all_conns"] == 1
    assert out["duplicate_per_conn"] == {"conn1": 1}
    assert out["winner_counts_multi_conn"] == {"conn0": 1, "conn1": 1}
    assert out["conn0_minus_fastest_ms"]["p50_ms"] == 0.0
    assert out["conn0_minus_fastest_ms"]["max_ms"] == 15.0
    assert out["conn0_minus_fastest_when_not_winner_ms"]["samples"] == 1
    assert out["conn0_minus_fastest_when_not_winner_ms"]["p50_ms"] == 15.0
    assert out["slowest_minus_fastest_ms"]["min_ms"] == 10.0
    assert out["slowest_minus_fastest_ms"]["max_ms"] == 30.0


def test_cross_channel_tracker_all_metrics():
    ct = probe.CrossChannelTracker()

    # Initial L2 @ t=1000 arrives at recv=100ms (100_000_000ns)
    ct.on_l2_book("BTC", 1000, Decimal("100"), Decimal("2"), Decimal("101"), Decimal("3"),
                  100_000_000, 1_100_000_000)
    # Matching exact state + same block timestamp BBO @ t=1000 arrives earlier? Suppose later/earlier test:
    # Here put BBO @ t=1000 arriving at 90ms (10ms earlier than L2)
    ct.on_bbo("BTC", 1000, Decimal("100"), Decimal("2"), Decimal("101"), Decimal("3"),
              90_000_000, 1_090_000_000)
    # Trade @ t=1000 arriving at 85ms (5ms earlier than BBO, 15ms earlier than L2)
    ct.on_trade("BTC", 1000, "B", Decimal("101"), 85_000_000, 1_085_000_000)

    # New BBO @ t=1200 shifts price to 102/103 at recv=220ms -> starts BBO lead-to-next-L2
    ct.on_bbo("BTC", 1200, Decimal("102"), Decimal("2"), Decimal("103"), Decimal("3"),
              220_000_000, 1_220_000_000)

    # Buy trade @ t=1250 at px=101.5 arrives at 260ms:
    # Newer than L2(1000) and >= L2 ask(101) -> crosses L2!
    # Newer than BBO(1200) and < BBO ask(103) -> does NOT cross updated BBO!
    ct.on_trade("BTC", 1250, "B", Decimal("101.5"), 260_000_000, 1_260_000_000)

    # Buy trade @ t=1260 at px=103 arrives at 270ms: crosses BOTH L2 and BBO!
    ct.on_trade("BTC", 1260, "B", Decimal("103"), 270_000_000, 1_270_000_000)

    # Stale L2 @ t=1100 (older than latest BBO t=1200, price 100/101 vs BBO 102/103) arrives at 300ms
    # Note: mid_l2=100.5, mid_bbo=102.5, drift = 2 / 100.5 * 10000 = 199.005 bps
    # Should NOT pop pending_bbo_lead since 1100 < 1200
    ct.on_l2_book("BTC", 1100, Decimal("100"), Decimal("2"), Decimal("101"), Decimal("3"),
                  300_000_000, 1_300_000_000)

    # Fresh L2 @ t=1200 with updated price 102/103 arrives at 350ms (130ms after BBO @ 220ms)
    ct.on_l2_book("BTC", 1200, Decimal("102"), Decimal("2"), Decimal("103"), Decimal("3"),
                  350_000_000, 1_350_000_000)

    res = ct.result(400_000_000)

    # Exact state matches: t=1000 (delta=+10ms) and t=1200 (delta=+130ms)
    exact = res["exact_state_match_l2_vs_bbo"]
    assert exact["counts"]["matched"] == 2
    assert exact["l2_minus_bbo_ms"]["min_ms"] == 10.0
    assert exact["l2_minus_bbo_ms"]["max_ms"] == 130.0

    # Same block timestamp @ t=1000 and t=1200
    blk = res["same_block_timestamp_match"]
    assert blk["l2_minus_bbo"]["counts"]["matched"] == 2
    assert blk["bbo_minus_trade"]["counts"]["matched"] == 1
    assert blk["bbo_minus_trade"]["delta_ms"]["p50_ms"] == 5.0
    assert blk["l2_minus_trade"]["delta_ms"]["p50_ms"] == 15.0

    # Price transition (100,101)->(102,103) observed once in each channel (at 350ms vs 220ms = +130ms)
    trans = res["unique_price_transitions_l2_vs_bbo"]
    assert trans["counts"]["bbo_first"] == 1
    assert trans["l2_minus_bbo_ms"]["p50_ms"] == 130.0

    # Stale on arrival: out of 3 L2 arrivals, 2 had prior BBO (the 1100 arrival and 1200 arrival),
    # 1 (the 1100 arrival) had prior BBO time 1200 > 1100 with different price
    stale = res["stale_on_arrival_l2_vs_bbo"]
    assert stale["l2_arrivals_total"] == 3
    assert stale["l2_arrivals_with_prior_bbo"] == 2
    assert stale["l2_stale_vs_bbo_count"] == 1
    assert stale["l2_stale_with_price_diff_count"] == 1
    assert stale["bbo_time_minus_l2_time_ms"]["p50_ms"] == 100.0
    assert stale["mid_drift_bps"]["p50_bps"] == pytest.approx(199.005, rel=1e-4)

    # BBO price-change visibility lead to next L2 (350ms - 220ms = 130ms)
    assert res["bbo_price_change_lead_to_next_l2_ms"]["samples"] == 1
    assert res["bbo_price_change_lead_to_next_l2_ms"]["p50_ms"] == 130.0

    # Trade-through audit: 3 total trades; 2 newer than L2 (t=1250, t=1260), both cross L2 (ask 101),
    # both have newer BBO (1200), only 1 (px=103) crosses BBO (ask 103)
    tt = res["trade_through_book_audit"]
    assert tt["trades_total"] == 3
    assert tt["trades_newer_than_l2"] == 2
    assert tt["trades_crossing_l2"] == 2
    assert tt["trades_crossing_l2_with_newer_bbo"] == 2
    assert tt["trades_crossing_l2_and_bbo"] == 1
    assert tt["trades_newer_than_bbo"] == 2
    assert tt["trades_crossing_bbo"] == 1


def test_parse_coins_and_sell_trade_crossing_and_ttl_expiry():
    assert probe.parse_coins(" BTC, ETH, BTC , ") == ("BTC", "ETH")
    with pytest.raises(ValueError):
        probe.parse_coins(" , ")

    rt = probe.RaceTracker("trades", race_conns=2, capacity=10, ttl_ns=1_000)
    assert rt.add(0, ("ETH", 1), 100, 100, 100_000_100) is True
    # TTL expires entry (1) before conn1 arrives at 2_000
    assert rt.add(1, ("ETH", 2), 101, 2_000, 101_000_200) is True
    out = rt.result(2_500)
    assert out["unique_events"] == 2
    assert out["events_seen_by_one_conn"] == 2

    ct = probe.CrossChannelTracker(transition_max_gap_ns=500_000)
    ct.on_l2_book("ETH", 1000, Decimal("200"), Decimal("1"), Decimal("202"), Decimal("1"), 100_000, 1_000_100_000)
    ct.on_bbo("ETH", 1000, Decimal("200"), Decimal("1"), Decimal("202"), Decimal("1"), 100_000, 1_000_100_000)
    # Sell trade at 200.0 (<= bid 200) newer than both
    ct.on_trade("ETH", 1010, "A", Decimal("199.5"), 200_000, 1_010_200_000)
    res = ct.result(300_000)["trade_through_book_audit"]
    assert res["trades_crossing_l2"] == 1
    assert res["trades_crossing_bbo"] == 1


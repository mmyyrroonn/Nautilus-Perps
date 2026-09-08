#!/usr/bin/env python3
"""
Offline tests for ``src/ref_feed.py`` (the reference-price leg).

No network and no Futu credentials: the WebSocket push messages come from the
examples in the Futu documentation, and the signature test generates its own
throw-away Ed25519 key and verifies the signature with the matching public key.

    .venv\\Scripts\\python.exe -m pytest tests/test_ref_feed.py -q
    .venv\\Scripts\\python.exe -m unittest tests.test_ref_feed -v
"""

from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
import unittest
from unittest import mock
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import ref_feed  # noqa: E402  (needs sys.path above)


# --------------------------------------------------------------------------------- parsing

# Payloads copied from https://open.futunn.com/zh-cn/api/quote/push/data-format
QUOTE_MESSAGE = {
    "type": "QUOTE",
    "symbol": "US.AAPL",
    "data": {
        "data_time_ms": 1710000000000,
        "last_price": 180.12,
        "open_price": 178.5,
        "high_price": 181.2,
        "low_price": 177.9,
        "prev_close_price": 179.66,
        "volume": 12345678,
        "turnover": 2223456789.12,
        "suspension": False,
        "sec_status": "NORMAL",
    },
}
BOOK_MESSAGE = {
    "type": "ORDER_BOOK",
    "symbol": "US.NVDA",
    "data": {
        "data_time_ms": 1710000000500,
        "ask_list": [
            {"price": 320.2, "volume": 1000, "order_count": 3},
            {"price": 320.3, "volume": 400, "order_count": 1},
        ],
        "bid_list": [{"price": 320.0, "volume": 800, "order_count": 2}],
    },
}
TICKER_MESSAGE = {
    "type": "TICKER",
    "symbol": "US.AAPL",
    "data": {
        "ticker_list": [
            {"time_ms": 1710000000123, "sequence": 123456, "direction": "BUY",
             "price": 180.12, "volume": 100, "turnover": 18012, "type": "NORMAL"},
            {"time_ms": 1710000000456, "sequence": 123457, "direction": "SELL",
             "price": 180.10, "volume": 50, "turnover": 9005, "type": "NORMAL"},
        ],
    },
}


class TestMessageParsing(unittest.TestCase):
    """Push message -> RefUpdate, the pure function the feed leans on."""

    def test_quote_carries_last_and_source_time(self):
        updates = ref_feed.updates_from_message(QUOTE_MESSAGE, ts_recv_ns=42)
        self.assertEqual(len(updates), 1)
        update = updates[0]
        self.assertEqual(update.code, "US.AAPL")
        self.assertEqual(update.kind, ref_feed.KIND_QUOTE)
        self.assertAlmostEqual(update.last, 180.12)
        self.assertIsNone(update.bid)
        self.assertEqual(update.ts_recv_ns, 42)
        # data_time_ms is a Unix millisecond timestamp: ns = ms * 1e6, and it is UTC.
        self.assertEqual(update.ts_src_ns, 1710000000000 * 1_000_000)
        moment = datetime.fromtimestamp(update.ts_src_ns / 1e9, timezone.utc)
        self.assertEqual(moment.isoformat(timespec="milliseconds"),
                         "2024-03-09T16:00:00.000+00:00")

    def test_book_takes_the_touch_of_each_side(self):
        updates = ref_feed.updates_from_message(BOOK_MESSAGE, ts_recv_ns=7)
        self.assertEqual(len(updates), 1)
        update = updates[0]
        self.assertEqual(update.kind, ref_feed.KIND_BOOK)
        self.assertAlmostEqual(update.bid, 320.0)
        self.assertAlmostEqual(update.ask, 320.2)
        self.assertAlmostEqual(update.bid_size, 800.0)
        self.assertAlmostEqual(update.ask_size, 1000.0)
        self.assertAlmostEqual(update.mid, 320.1)
        self.assertEqual(update.ts_src_ns, 1710000000500 * 1_000_000)

    def test_ticker_keeps_every_print_with_millisecond_precision(self):
        updates = ref_feed.updates_from_message(TICKER_MESSAGE, ts_recv_ns=9)
        self.assertEqual(len(updates), 2)
        self.assertEqual([u.kind for u in updates],
                         [ref_feed.KIND_TICKER, ref_feed.KIND_TICKER])
        self.assertEqual([u.seq for u in updates], [123456, 123457])
        self.assertAlmostEqual(updates[0].last, 180.12)
        # 1710000000123 ms -> the .123 must survive as nanoseconds.
        self.assertEqual(updates[0].ts_src_ns, 1710000000123 * 1_000_000)
        self.assertEqual(updates[0].ts_src_ns % 1_000_000_000, 123_000_000)
        gap_ms = (updates[1].ts_src_ns - updates[0].ts_src_ns) / 1e6
        self.assertAlmostEqual(gap_ms, 333.0)

    def test_ts_event_and_ts_init_are_what_customdata_reads(self):
        update = ref_feed.updates_from_message(BOOK_MESSAGE, ts_recv_ns=1234)[0]
        self.assertEqual(update.ts_event, update.ts_src_ns)
        self.assertEqual(update.ts_init, 1234)
        # A message with no source time falls back to the receive time.
        no_time = ref_feed.updates_from_message(
            {"type": "QUOTE", "symbol": "US.X", "data": {"last_price": 1.0}}, ts_recv_ns=99,
        )[0]
        self.assertEqual(no_time.ts_src_ns, 0)
        self.assertEqual(no_time.ts_event, 99)

    def test_unknown_and_malformed_messages_are_ignored(self):
        for message in (
            {"type": "KLINE", "symbol": "US.AAPL", "data": {"kl_list": []}},
            {"type": "MARKET_STATE", "data": {"market": "US", "status": 6}},
            {"type": "QUOTE", "symbol": "US.AAPL", "data": {}},
            {"type": "ORDER_BOOK", "symbol": "US.AAPL", "data": {"ask_list": [], "bid_list": []}},
            {"no": "type"},
            "not a dict",
        ):
            self.assertEqual(ref_feed.updates_from_message(message, 1), [], message)


# --------------------------------------------------------------------------------- signing


class TestLiveFrames(unittest.TestCase):
    """The parser against real frames captured from the live socket.

    ``tests/fixtures/futu_ws_frames.jsonl`` is ~20 s of US.NVDA pushes recorded on
    2026-09-08 (the auth frame, the only one carrying a session id, was dropped).
    """

    @classmethod
    def setUpClass(cls) -> None:
        path = Path(__file__).resolve().parent / "fixtures" / "futu_ws_frames.jsonl"
        cls.frames = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                      if line.strip()]
        cls.pushes = [f for f in cls.frames if "type" in f]
        cls.acks = [f for f in cls.frames if "ret_code" in f]

    def test_fixture_has_every_shape_the_parser_must_handle(self):
        kinds = {f["type"] for f in self.pushes}
        self.assertEqual(kinds, {"QUOTE", "ORDER_BOOK", "TICKER", "MARKET_STATE"})
        self.assertEqual(len(self.acks), 2, "one success ack and one failure ack")

    def test_every_frame_parses_without_raising(self):
        total = 0
        for frame in self.frames:
            total += len(ref_feed.updates_from_message(frame, ts_recv_ns=1))
        self.assertGreater(total, 100)

    def test_quote_frame_yields_last_and_ignores_session_sub_objects(self):
        frame = next(f for f in self.pushes if f["type"] == "QUOTE")
        update = ref_feed.updates_from_message(frame, ts_recv_ns=5)[0]
        self.assertEqual(update.kind, ref_feed.KIND_QUOTE)
        self.assertAlmostEqual(update.last, frame["data"]["last_price"])
        self.assertEqual(update.ts_src_ns, frame["data"]["data_time_ms"] * 1_000_000)
        # The envelope timestamp becomes ts_srv_ns; pre/after market are not read.
        self.assertEqual(update.ts_srv_ns, frame["timestamp"] * 1_000_000)
        self.assertIn("pre_market", frame["data"])
        self.assertNotAlmostEqual(update.last, frame["data"]["pre_market"]["price"])

    def test_order_book_frames_carry_one_exchange_each(self):
        books = [f for f in self.pushes if f["type"] == "ORDER_BOOK"]
        venues = set()
        for frame in books:
            update = ref_feed.updates_from_message(frame, ts_recv_ns=5)[0]
            self.assertEqual(update.kind, ref_feed.KIND_BOOK)
            self.assertTrue(update.venue, "every book frame names its exchange")
            self.assertEqual(update.venue, frame["data"]["bid_list"][0]["mpid"])
            self.assertLessEqual(update.bid, update.ask)
            venues.add(update.venue)
        self.assertGreaterEqual(len(venues), 2, f"expected several exchanges, got {venues}")
        self.assertTrue({"NSDQ", "ARCA"} <= venues, venues)

    def test_composite_touch_is_the_best_of_the_exchanges(self):
        """Two exchanges, two frames: the reference is the tighter combination."""
        state = ref_feed.RefState("US.NVDA")
        books = [f for f in self.pushes if f["type"] == "ORDER_BOOK"]
        per_venue: dict[str, dict] = {}
        for frame in books:
            update = ref_feed.updates_from_message(frame, ts_recv_ns=1_000_000_000)[0]
            per_venue.setdefault(update.venue, {"bid": update.bid, "ask": update.ask})
            per_venue[update.venue] = {"bid": update.bid, "ask": update.ask}
            state.apply(update)
        fresh = state.fresh_books()
        self.assertGreaterEqual(len(fresh), 2)
        self.assertAlmostEqual(state.bid, max(v["bid"] for v in per_venue.values()))
        self.assertAlmostEqual(state.ask, min(v["ask"] for v in per_venue.values()))
        self.assertAlmostEqual(state.mid, (state.bid + state.ask) / 2.0)
        self.assertIn(state.bid_venue, per_venue)

    def test_a_stale_exchange_drops_out_of_the_composite(self):
        state = ref_feed.RefState("US.NVDA")
        state.apply(ref_feed.RefUpdate("US.NVDA", ref_feed.KIND_BOOK, 0, 1_000_000_000,
                                       bid=100.0, ask=100.5, venue="ARCA"))
        state.apply(ref_feed.RefUpdate("US.NVDA", ref_feed.KIND_BOOK, 0, 1_100_000_000,
                                       bid=99.0, ask=100.2, venue="NSDQ"))
        self.assertAlmostEqual(state.bid, 100.0)  # ARCA still fresh
        self.assertAlmostEqual(state.ask, 100.2)  # NSDQ is tighter on the offer
        self.assertEqual(state.bid_venue, "ARCA")
        # Six seconds later only NSDQ has spoken: ARCA's bid must be dropped.
        state.apply(ref_feed.RefUpdate("US.NVDA", ref_feed.KIND_BOOK, 0, 7_200_000_000,
                                       bid=99.1, ask=100.3, venue="NSDQ"))
        self.assertEqual(set(state.fresh_books()), {"NSDQ"})
        self.assertAlmostEqual(state.bid, 99.1)
        self.assertAlmostEqual(state.ask, 100.3)

    def _replay_books(self):
        """Feed the fixture's book frames in order, using the envelope time as arrival.

        Yields (update, state) after each ORDER_BOOK frame is applied.
        """
        state = ref_feed.RefState("US.NVDA")
        for frame in self.pushes:
            srv = frame.get("timestamp")
            if not srv:
                continue
            for update in ref_feed.updates_from_message(frame, ts_recv_ns=srv * 1_000_000):
                state.apply(update)
                if update.kind == ref_feed.KIND_BOOK:
                    yield update, state

    def test_a_real_crossed_composite_falls_back_to_the_freshest_exchange(self):
        """The capture really does cross: NSDQ 229.69/229.71 against ARCA 229.73/229.76."""
        hits = []
        for update, state in self._replay_books():
            if state.composite_crossed:
                fresh = state.fresh_books()
                raw_bid = max(b.bid for b in fresh.values() if b.bid)
                raw_ask = min(b.ask for b in fresh.values() if b.ask)
                hits.append((update.venue, raw_bid, raw_ask, state.bid, state.ask,
                             state.book_mode, state.bid_venue, state.crossed))
        self.assertTrue(hits, "the fixture must contain at least one real crossing")
        for venue, raw_bid, raw_ask, bid, ask, mode, touch_venue, _n in hits:
            self.assertGreaterEqual(raw_bid, raw_ask, "this is the crossed case")
            self.assertEqual(mode, "freshest")
            self.assertLess(bid, ask, "a single exchange's own book never crosses")
            self.assertEqual(touch_venue, venue, "the freshest book is the one just applied")
            self.assertAlmostEqual((bid + ask) / 2.0, (bid + ask) / 2.0)
        # The counter counts book frames, and only the crossed ones.
        final = hits[-1][7]
        self.assertEqual(final, len(hits))

    def test_an_uncrossed_composite_still_uses_both_exchanges(self):
        clean = [(u, s) for u, s in self._replay_books() if not s.composite_crossed]
        self.assertTrue(clean)
        used_both = 0
        for _update, state in clean:
            self.assertEqual(state.book_mode, "composite")
            self.assertFalse(state.composite_crossed)
            self.assertLess(state.bid, state.ask)
            fresh = state.fresh_books()
            self.assertAlmostEqual(state.bid, max(b.bid for b in fresh.values() if b.bid))
            self.assertAlmostEqual(state.ask, min(b.ask for b in fresh.values() if b.ask))
            if len(fresh) > 1 and state.bid_venue != state.ask_venue:
                used_both += 1
        self.assertGreater(used_both, 0, "the composite must sometimes span two exchanges")

    def test_crossing_is_the_minority_of_book_frames(self):
        modes = [state.book_mode for _u, state in self._replay_books()]
        self.assertGreater(modes.count("composite"), modes.count("freshest"))
        self.assertEqual(set(modes) - {"composite", "freshest", "last"}, set())

    def test_ticker_batches_become_one_update_per_print(self):
        frame = next(f for f in self.pushes
                     if f["type"] == "TICKER" and len(f["data"]["ticker_list"]) > 1)
        updates = ref_feed.updates_from_message(frame, ts_recv_ns=5)
        self.assertEqual(len(updates), len(frame["data"]["ticker_list"]))
        for update, entry in zip(updates, frame["data"]["ticker_list"]):
            self.assertEqual(update.kind, ref_feed.KIND_TICKER)
            self.assertAlmostEqual(update.last, entry["price"])
            self.assertEqual(update.seq, entry["sequence"])
            self.assertEqual(update.ts_src_ns, entry["time_ms"] * 1_000_000)
        # sequence is a 19-digit integer: it must survive as an exact int.
        self.assertGreater(max(u.seq for u in updates), 10 ** 18)

    def test_market_state_is_ignored(self):
        frame = next(f for f in self.pushes if f["type"] == "MARKET_STATE")
        self.assertNotIn("symbol", frame)
        self.assertEqual(ref_feed.updates_from_message(frame, 1), [])

    def test_src_to_srv_hop_is_a_sane_positive_delay(self):
        state = ref_feed.RefState("US.NVDA")
        hops = []
        for frame in self.pushes:
            for update in ref_feed.updates_from_message(frame, ts_recv_ns=1):
                state.apply(update)
                if state.src_to_srv_ms is not None:
                    hops.append(state.src_to_srv_ms)
        self.assertGreater(len(hops), 50)
        median = sorted(hops)[len(hops) // 2]
        self.assertGreater(median, 0.0, "Futu stamps the frame after the exchange did")
        self.assertLess(median, 60_000.0)


class TestSubscribeAck(unittest.TestCase):
    """The real ack shapes; the documented ``{"code":0,"message":""}`` is not one."""

    SUCCESS = {"ret_code": 0, "ret_msg": "success",
               "data": {"code": 0, "id": "cap-sub-1", "message": ""}}
    FAILURE = {"ret_code": -9, "ret_msg": "realtime quote permission required",
               "error": {"code": "permission_denied",
                         "message": "user has only delayed quote permission for the "
                                    "requested market"}}

    def test_success_is_ret_code_zero_and_data_code_zero(self):
        ok, code, text = ref_feed.read_subscribe_ack(self.SUCCESS, "cap-sub-1")
        self.assertTrue(ok)
        self.assertEqual(code, 0)
        self.assertEqual(text, "success")

    def test_failure_reports_the_error_message(self):
        ok, code, text = ref_feed.read_subscribe_ack(self.FAILURE, "cap-sub-bad")
        self.assertFalse(ok)
        self.assertEqual(code, -9)
        self.assertIn("delayed quote permission", text)

    def test_an_ack_for_another_request_is_not_ours(self):
        ok, _code, _text = ref_feed.read_subscribe_ack(self.SUCCESS, "some-other-id")
        self.assertIsNone(ok)

    def test_a_push_frame_is_not_an_ack(self):
        ok, _code, _text = ref_feed.read_subscribe_ack(QUOTE_MESSAGE, "cap-sub-1")
        self.assertIsNone(ok)
        self.assertIsNone(ref_feed.read_subscribe_ack("nonsense", "x")[0])

    def test_inner_failure_code_is_reported_too(self):
        ok, code, text = ref_feed.read_subscribe_ack(
            {"ret_code": 0, "ret_msg": "", "data": {"code": 4, "id": "s1",
                                                    "message": "sub quota exceeded, max=100"}},
            "s1",
        )
        self.assertFalse(ok)
        self.assertEqual(code, 4)
        self.assertIn("quota", text)

    def test_the_captured_acks_are_read_the_same_way(self):
        path = Path(__file__).resolve().parent / "fixtures" / "futu_ws_frames.jsonl"
        acks = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()
                if line.strip() and '"ret_code"' in line]
        results = [ref_feed.read_subscribe_ack(a)[0] for a in acks]
        self.assertIn(True, results)
        self.assertIn(False, results)


class TestSigning(unittest.TestCase):
    """The WebSocket auth signature, per https://open.futunn.com/zh-cn/api/quote/push/auth."""

    def test_signed_message_is_the_documented_original_string(self):
        self.assertEqual(
            ref_feed.ws_auth_message(1782357937000, "abc123"),
            b"1782357937000\nabc123\nWEBSOCKET\nws/auth",
        )

    def test_ed25519_signature_verifies_with_the_public_key(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        private = Ed25519PrivateKey.generate()  # throw-away key, never leaves this test
        timestamp_ms, nonce = 1782357937000, ref_feed.new_nonce()
        signature = ref_feed.sign_ws_auth(private, timestamp_ms, nonce)
        private.public_key().verify(
            base64.b64decode(signature), ref_feed.ws_auth_message(timestamp_ms, nonce),
        )  # raises InvalidSignature on mismatch
        with self.assertRaises(Exception):
            private.public_key().verify(
                base64.b64decode(signature), ref_feed.ws_auth_message(timestamp_ms + 1, nonce),
            )

    def test_private_key_loads_from_pem_and_from_raw_base64(self):
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
        from cryptography.hazmat.primitives.serialization import (
            Encoding, NoEncryption, PrivateFormat,
        )

        private = Ed25519PrivateKey.generate()
        pem = private.private_bytes(
            Encoding.PEM, PrivateFormat.PKCS8, NoEncryption(),
        ).decode()
        raw = base64.b64encode(
            private.private_bytes(Encoding.Raw, PrivateFormat.Raw, NoEncryption()),
        ).decode()
        expected = ref_feed.sign_ws_auth(private, 1, "n")
        for material in (pem, pem.replace("\n", "\\n"), raw):
            loaded = ref_feed.load_private_key(material)
            self.assertEqual(ref_feed.sign_ws_auth(loaded, 1, "n"), expected)

    def test_rsa_key_signs_with_pkcs1v15_sha256(self):
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding, rsa

        private = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        signature = ref_feed.sign_ws_auth(private, 1782357937000, "nonce-1")
        private.public_key().verify(
            base64.b64decode(signature),
            ref_feed.ws_auth_message(1782357937000, "nonce-1"),
            padding.PKCS1v15(),
            hashes.SHA256(),
        )

    def test_bad_key_material_raises_a_clear_error(self):
        for material in ("", "not-base64-!!", base64.b64encode(b"short").decode()):
            with self.assertRaises(ref_feed.RefFeedError):
                ref_feed.load_private_key(material)


class TestFutuFeedConfig(unittest.TestCase):
    """Configuration faults must be loud, never a silent hang."""

    def test_missing_api_key_raises_before_any_connection(self):
        feed = ref_feed.FutuFeed(["US.NVDA"], api_key="", private_key_material="x")
        with self.assertRaises(ref_feed.RefFeedError) as caught:
            feed.start(lambda update: None)
        self.assertIn("FUTU_API_KEY", str(caught.exception))

    def test_bad_private_key_raises_before_any_connection(self):
        feed = ref_feed.FutuFeed(["US.NVDA"], api_key="key", private_key_material="nope!")
        with self.assertRaises(ref_feed.RefFeedError):
            feed.start(lambda update: None)

    def test_default_url_is_the_documented_endpoint(self):
        self.assertEqual(ref_feed.FUTU_WS_URL, "wss://webapi-quote.futunn.com/ws")

    def test_subscribe_frame_matches_the_documented_shape(self):
        feed = ref_feed.FutuFeed(["US.NVDA", "US.TSLA"], api_key="key", private_key_material="x")
        frame = feed._subscribe_frame(["US.NVDA"])
        self.assertEqual(frame["action"], "subscribe")
        # Default kinds: order_book + ticker, no QUOTE (see DEFAULT_SUB_KINDS).
        self.assertEqual(frame["order_book"], ["US.NVDA"])
        self.assertEqual(frame["ticker"], ["US.NVDA"])
        self.assertNotIn("quote", frame)
        self.assertTrue(frame["id"])

    def test_subscribe_kinds_can_be_widened_from_the_environment(self):
        with mock.patch.dict(os.environ, {"FUTU_SUB_KINDS": "quote, order_book,ticker"}):
            feed = ref_feed.FutuFeed(["US.NVDA"], api_key="key", private_key_material="x")
        self.assertEqual(feed.subscribe_kinds, ["quote", "order_book", "ticker"])
        self.assertEqual(feed._subscribe_frame(["US.NVDA"])["quote"], ["US.NVDA"])


# --------------------------------------------------------------------------------- fake feed


class TestFakeFeed(unittest.TestCase):
    """The stand-in source used when the box has no Futu access."""

    def test_emits_updates_and_stops(self):
        seen: list[ref_feed.RefUpdate] = []
        lock = threading.Lock()

        def collect(update):
            with lock:
                seen.append(update)

        feed = ref_feed.FakeFeed(["US.NVDA"], interval_ms=20, start_price=100.0, seed=7)
        feed.start(collect)
        try:
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                with lock:
                    if len(seen) >= 6:
                        break
                time.sleep(0.02)
        finally:
            feed.stop()
        with lock:
            count_at_stop = len(seen)
        self.assertGreaterEqual(count_at_stop, 6)
        time.sleep(0.3)
        with lock:
            self.assertEqual(len(seen), count_at_stop, "feed kept emitting after stop()")

        kinds = {update.kind for update in seen}
        self.assertEqual(kinds, {ref_feed.KIND_BOOK, ref_feed.KIND_TICKER})
        for update in seen:
            self.assertEqual(update.code, "US.NVDA")
            self.assertGreater(update.ts_recv_ns, 0)
            if update.kind == ref_feed.KIND_BOOK:
                self.assertLess(update.bid, update.ask)
                self.assertAlmostEqual(update.mid, (update.bid + update.ask) / 2)

    def test_rejects_an_empty_code_list(self):
        with self.assertRaises(ref_feed.RefFeedError):
            ref_feed.FakeFeed([])


# --------------------------------------------------------------------------------- state


class TestRefState(unittest.TestCase):
    """What the strategy keeps per code, and the two edge formulas."""

    def test_book_sets_the_touch_and_ticker_sets_last(self):
        state = ref_feed.RefState("US.NVDA")
        self.assertIsNone(state.mid)
        state.apply(ref_feed.updates_from_message(BOOK_MESSAGE, 1_000_000_000)[0])
        self.assertAlmostEqual(state.mid, 320.1)
        state.apply(ref_feed.RefUpdate("US.NVDA", ref_feed.KIND_TICKER, 0, 2_000_000_000,
                                       last=321.0))
        self.assertAlmostEqual(state.last, 321.0)
        self.assertAlmostEqual(state.mid, 320.1, msg="book mid wins over the last print")
        self.assertEqual(state.updates, 2)
        self.assertEqual(state.by_kind, {ref_feed.KIND_BOOK: 1, ref_feed.KIND_TICKER: 1})
        self.assertAlmostEqual(state.age_ms(3_000_000_000), 1000.0)

    def test_last_price_is_the_mid_when_no_book_arrived(self):
        state = ref_feed.RefState("US.AAPL")
        state.apply(ref_feed.updates_from_message(QUOTE_MESSAGE, 5)[0])
        self.assertAlmostEqual(state.mid, 180.12)

    def test_edge_formulas_net_out_one_taker_fee(self):
        # Perp ask 100 bps below the stock, 0.9 bps taker: lifting it nets ~99.1 bps.
        self.assertAlmostEqual(ref_feed.buy_edge_bps(101.0, 100.0, 0.9), 100.0 - 0.9, places=6)
        self.assertAlmostEqual(ref_feed.sell_edge_bps(100.0, 101.0, 0.9),
                               (1.0 / 101.0) * 1e4 - 0.9, places=6)
        # A perfectly aligned perp leaves exactly minus the fee on both sides.
        self.assertAlmostEqual(ref_feed.buy_edge_bps(100.0, 100.0, 0.9), -0.9)
        self.assertAlmostEqual(ref_feed.sell_edge_bps(100.0, 100.0, 0.9), -0.9)


class TestReferenceCodes(unittest.TestCase):
    """spread_watch maps its symbols onto Futu codes."""

    def test_equities_map_by_rule_and_crypto_has_no_reference(self):
        import spread_watch

        self.assertEqual(spread_watch.reference_code("NVDA"), "US.NVDA")
        self.assertEqual(spread_watch.reference_code("SPCX"), "US.SPCX")
        self.assertEqual(spread_watch.reference_code("SPY"), "US.SPY")
        for symbol in ("BTC", "ETH", "HYPE", "GOLD", "GOLD1", "ANSEM"):
            self.assertIsNone(spread_watch.reference_code(symbol), symbol)

    def test_ref_header_has_one_block_per_leg(self):
        import spread_watch

        legs = spread_watch.build_plan(["NVDA"], ["HL", "LIGHTER"])["NVDA"]
        header = spread_watch.ref_header(legs)
        self.assertEqual(header[:len(spread_watch.REF_HEAD)], spread_watch.REF_HEAD)
        # The hop and book-mode columns close the reference block, in that order,
        # right after ref_age_ms and before the first leg block.
        self.assertEqual(spread_watch.REF_HEAD[-3:],
                         ["ref_age_ms", "ref_src_to_srv_ms", "ref_book_mode"])
        for venue in ("HL", "LIGHTER"):
            for suffix in ("bid", "ask", "age_ms", "buy_edge_bps", "sell_edge_bps"):
                self.assertIn(f"{venue}_{suffix}", header)


if __name__ == "__main__":
    unittest.main()

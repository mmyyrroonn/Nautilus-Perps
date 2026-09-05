r"""Offline round-2 probe regressions; uses existing repository test fixtures.
Run from E:\Nautilus-Perps with .venv\Scripts\python.exe -B reports\aster-review-round2-repro.py.
No credentials are loaded and no exchange connection or order is sent.
"""
import sys
import types
import threading
import unittest
from pathlib import Path

stub = types.ModuleType("dotenv")
stub.load_dotenv = lambda *args, **kwargs: False
sys.modules["dotenv"] = stub
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tests"))
import test_exec_probe as fixture
probe_module = fixture.exec_probe

def resting_probe():
    probe = fixture.build_probe()
    probe.on_start()
    probe.on_quote(fixture.FakeQuote("100000.0", "100000.1"))
    resting = probe.submitted[0]
    probe.on_order_accepted(fixture.FakeEvent(resting.client_order_id))
    return probe, resting

class ReviewRound2(unittest.TestCase):
    def test_failure_prevents_late_cancel_from_submitting_ioc(self):
        probe, resting = resting_probe()
        # Inject an event sequence, not an assertion that the Aster adapter emits
        # OrderCancelRejected for every HTTP cancellation failure.
        probe.on_order_cancel_rejected(
            fixture.FakeEvent(resting.client_order_id, reason="cancel outcome unknown")
        )
        self.assertTrue(probe.finished)
        self.assertTrue(probe.done_event.is_set())
        count = probe.orders_sent
        resting.close("CANCELED")
        probe.on_order_canceled(fixture.FakeEvent(resting.client_order_id))
        self.assertEqual(probe.orders_sent, count,
                         f"new order after failure: phase={probe._phase}, orders={probe.orders_sent}")

    def test_timeout_initiates_cleanup_of_unresolved_gtc(self):
        probe, resting = resting_probe()
        original_cancel_count = len(probe.cancel_calls)
        # The watchdog only signals from its thread; the node invokes on_stop
        # on the main thread. Keep the same thread boundary for PyStrategy.
        stop_requested = threading.Event()
        thread, state = probe_module.start_stop_watchdog(probe.done_event, stop_requested.set, 0.01)
        thread.join(timeout=1.0)
        self.assertTrue(state["timed_out"])
        self.assertTrue(stop_requested.is_set())
        probe.on_stop()
        self.assertFalse(resting.is_closed)
        self.assertGreater(len(probe.cancel_calls), original_cancel_count,
                           "watchdog exit leaves accepted GTC with no cleanup request")

    def test_filled_gtc_does_not_pass_cancel_probe(self):
        probe, resting = resting_probe()
        resting.close("FILLED", filled_qty=str(resting.quantity))
        probe.on_order_filled(fixture.FakeEvent(
            resting.client_order_id, last_qty=resting.quantity,
            last_px=resting.price, commission=None, liquidity_side="MAKER",
        ))
        if len(probe.submitted) > 1:
            ioc = probe.submitted[1]
            ioc.close("CANCELED")
            probe.on_order_canceled(fixture.FakeEvent(ioc.client_order_id))
        self.assertNotEqual(probe.exit_code, probe_module.EXIT_OK,
                            f"cancel never succeeded: {probe.summary_line}")

if __name__ == "__main__":
    unittest.main(verbosity=2)

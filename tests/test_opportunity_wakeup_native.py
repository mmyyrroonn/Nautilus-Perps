"""Verify coalesced alerts on the installed native runner without any clients."""
from pathlib import Path
import sys
import threading

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from opportunity_wakeup import IntentWakeup


def test_native_alert_is_async_coalesced_and_rearms_after_delivery():
    pytest.importorskip("nautilus_trader.live")
    from nautilus_trader.common import DataActor, DataActorConfig, Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import TraderId

    node = (LiveNode.builder("INTENT-WAKE-CHECK", TraderId.from_str("CHECK-001"), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.ERROR, fileout_level=LogLevel.OFF, print_config=False))
        .with_reconciliation(False).with_timeout_reconciliation(0).with_timeout_portfolio(0)
        .with_delay_post_stop_secs(0).with_delay_shutdown_secs(0).build())
    handle = node.handle()
    fallback = threading.Timer(5, handle.stop)
    fallback.daemon = True
    deliveries = []

    class Consumer(DataActor):
        def __init__(self):
            super().__init__(DataActorConfig(log_events=False, log_commands=False))
            self.ready = False

        def on_start(self):
            self.wakeup = IntentWakeup(self.clock, self.on_time_event)
            for _ in range(3):
                self.wakeup.request()
            self.ready = True

        def on_time_event(self, event):
            deliveries.append((str(event.name), self.ready, self.wakeup.consume()))
            if len(deliveries) == 1:
                self.wakeup.request()
                self.wakeup.request()
            else:
                handle.stop()

        def on_stop(self):
            self.wakeup.close()

    actor = Consumer()
    try:
        node.add_actor(actor)
        fallback.start()
        node.run()
        assert deliveries == [(IntentWakeup.name, True, True)] * 2
        assert actor.wakeup.closed and not actor.wakeup.pending
    finally:
        fallback.cancel()
        node.dispose()

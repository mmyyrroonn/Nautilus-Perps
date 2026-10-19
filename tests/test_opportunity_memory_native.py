"""Exercise live receiver metrics while a real native timer queue backs up."""
from datetime import timedelta
import json
from pathlib import Path
import sys
import threading
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))

import opportunity_memory as memory
import opportunity_timing as timing


def test_installed_live_node_reports_backlog_during_slow_callbacks(tmp_path, monkeypatch):
    from nautilus_trader.common import DataActor, DataActorConfig, Environment, LoggerConfig, LogLevel
    from nautilus_trader.live import LiveNode
    from nautilus_trader.model import TraderId

    monkeypatch.setattr(memory, 'INTERVAL_SECONDS', 0.05)
    node = (LiveNode.builder('MEMORY-QUEUE-CHECK', TraderId.from_str('CHECK-001'), Environment.LIVE)
        .with_logging(LoggerConfig(stdout_level=LogLevel.ERROR, fileout_level=LogLevel.OFF, print_config=False))
        .with_reconciliation(False).with_timeout_reconciliation(0).with_timeout_portfolio(0)
        .with_delay_post_stop_secs(0).with_delay_shutdown_secs(0).build())
    handle = node.handle()
    stop_timer = threading.Timer(1.0, handle.stop)
    fallback = threading.Timer(5.0, handle.stop)
    stop_timer.daemon = fallback.daemon = True

    class SlowConsumer(DataActor):
        def __init__(self, diagnostics):
            super().__init__(DataActorConfig(log_events=False, log_commands=False))
            self.diagnostics = diagnostics

        def on_start(self):
            self.clock.set_timer('queue-check', timedelta(milliseconds=1))
            stop_timer.start()

        @timing.timed('market.timer')
        def on_time_event(self, event):
            self.diagnostics.record_timer(event)
            time.sleep(0.01)

        def on_stop(self):
            self.clock.cancel_timers()

    try:
        with memory.MemoryDiagnostics(tmp_path / 'memory') as diagnostics:
            diagnostics.attach_handle(handle)
            node.add_actor(SlowConsumer(diagnostics))
            fallback.start()
            def run():
                node.run()
                return 0
            assert timing.run_timed(run, enabled=True, directory=tmp_path / 'timings') == 0
        rows = [json.loads(line) for line in diagnostics.path.read_text().splitlines()]
        native_rows = [r for r in rows if r['native_runner'] is not None]
        assert diagnostics.errors == 0
        assert max(r['native_runner']['time_events']['queue_depth'] for r in native_rows) > 10
        assert any(r['native_runner']['queue_sampled_at_ns'] > 0 for r in native_rows)
        assert any(r['market_timer'].get('age_p95_ms', 0) > 10 for r in rows)
        assert any(any(op['name'] == 'market.timer' and op['count'] > 0
                       for op in (r['processing_timings'] or {}).get('operations', [])) for r in rows)
        assert handle.metrics_snapshot()['time_events']['dispatched'] > 0
    finally:
        stop_timer.cancel()
        fallback.cancel()
        node.dispose()

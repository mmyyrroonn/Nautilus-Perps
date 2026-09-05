"""Offline round-3 cleanup regression with real Strategy.cancel_order.
No clients are configured and the node is never run.
"""
import sys
import types
from pathlib import Path

noop = types.ModuleType("dotenv")
noop.load_dotenv = lambda *args, **kwargs: False
sys.modules["dotenv"] = noop
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import exec_probe as probe_module
from nautilus_trader.model import ClientOrderId

probe = probe_module.AsterProbeStrategy(
    probe_module.AsterProbeConfig(instrument_id=probe_module.BTC_INSTRUMENT_ID)
)
# Inject the unknown-cache branch the cleanup explicitly claims to support.
probe._probe_order_ids.append(ClientOrderId("MISSING-1"))
probe._orders_sent = 1
node = (
    probe_module.LiveNode.builder(
        "REVIEW-CLEANUP-FAILURE",
        probe_module.TraderId.from_str("REVIEW-002"),
        probe_module.Environment.LIVE,
    )
    .with_logging(probe_module.LoggerConfig(stdout_level=probe_module.LogLevel.ERROR))
    .build()
)
node.add_strategy(probe)
try:
    probe.record_failure("injected tracked-id/cache mismatch")
except RuntimeError as exc:
    print("Cleanup escaped:", str(exc))
print(
    "finished=", probe.finished,
    "done=", probe.done_event.is_set(),
    "summary=", repr(probe.summary_line),
    "leftovers=", probe.leftovers,
)
assert probe.done_event.is_set(), "cleanup exception prevented terminal notification"
assert probe.summary_line, "cleanup exception prevented result summary"

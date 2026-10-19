# Standalone exposure sizing and exit reliability acceptance

Date: 2026-10-20

Entry sizing independently budgets either leg filling alone, using the higher of its rounded limit valuation and current canonical USD ask valuation. The shared canonical quantity rounds down to the common lot step, then both executable limits, fees and net entry economics are recalculated. Send-time admission rechecks the same standalone exposure budget against current books.

The default `entry_unhedged_reserve_bps` is 10 bps. Values below 10 or at least 10,000 are rejected. `limits.toml` supplies a minimum reserve; configuration can increase that reserve and tighten the budget. The USD 100 unhedged hard cap remains unchanged, including its protection at equality and after subsequent price jumps.

The historical PONS example demonstrates the valuation distinction:

- 383 units at the execution price 0.26077 produce USD 99.87491 of proceeds.
- The same 383 units valued at ask 0.26112 represent USD 100.00896 of exposure.
- The regression selects 382 units, whose ask valuation is USD 99.74784, below the default USD 99.90 entry exposure budget.

Account reconciliation intents now request the existing dispatch wakeup. Completed account evidence wakes existing exit or inventory work through the coordinator, without waiting for the next periodic evaluation. Exit readiness still requires authoritative, connected and fresh evidence, activity fences, complete account coverage where provided, owned inventory and permitted pending orders. Reads begun before relevant activity remain unusable. Native rule checks and reduce-only selection remain enforced. The one-second order and account-read retry interval is unchanged; pending requests and read-failure backoff remain barriers.

Exit evidence records the first imbalance, entry terminal confirmation, account-read completion and rejection reasons, retry waits, reduction preparation, Python dispatch, fills and inventory returning to zero. Python dispatch timestamps are application boundaries, not native network-write timestamps. Repeated wait states do not produce a new wait record on every evaluation.

Validation used `E:/persarb/Nautilus-Perps/.venv/Scripts/python.exe -m pytest tests/test_opportunity_exit_reliability.py -q`: **22 passed**. Coverage includes independent leg fills, exact cap boundaries, lot and price rounding, the PONS regression, native/canonical multipliers and quote conversion, sizing across depth levels with recalculated fees and net economics, slow reads spanning activity, account ownership barriers, duplicate reports, the unchanged retry interval, and fill/fee/PnL accounting.

This acceptance records deterministic offline results. It does not establish a full-suite result, completion of a new native wheel, or measured improvement in live single-leg exposure duration. No before/after live latency samples were collected for this change.

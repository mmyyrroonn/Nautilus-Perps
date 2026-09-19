# Ondo shutdown and R5.2 data acceptance

Authorized 2026-09-19: implement and verify real client cleanup; replay existing ONDO-HL data with the current analyzer; perform finite public observation. Private sandbox and production writes are outside this run.

Base app aef07e4; fork ff57243. Isolated worktrees: E:/Nautilus-Perps/.worktrees/ondo-r52-app and E:/nautilus_trader/.worktrees/ondo-r52-cleanup. Existing unrelated changes remain untouched. No commits or pushes requested.

- [x] Pi: reproduce and wire shutdown into real client lifecycle; difficult remaining ownership fixes handled by Astra subagent under the user-authorized exception.
- [x] Pi: tests for normal stop, cancellation timeout, unknown orders, restart recovery, ownership and read-only behavior. Native full crate901 and Python6 passed; final private_runtime49 passed after strengthening actual restart recovery.
- [x] Astra: review lifecycle implementation and independently reproduce critical tests. Final independent review accepted; four ownership regressions and real disconnect/restart/recovery independently passed.
- [x] Pi: freeze original historical input hashes and replay using the current analyzer; explain fees, HALT, ages and recording gaps. Astra independent replay matched both CSV files; all 25 original input hashes unchanged.
- [x] Pi: finite public-only ONDO-HL observation with preserved failures and explicit completeness. 303.898 seconds, exit 0, both symbols/venues observed; 244 data tests independently passed. See review/data-review.md.
- [x] Pi: refresh the runtime artifact and application capability reporting as needed; preserve rollback artifact. R5.2 wheel built with documented Windows warning exception, generated stub, candidate836+97 tests and public131s probe passed.
- [x] Astra: independently verify artifacts, source/runtime identity, input immutability and acceptance conclusions; integrate reviewed changes without overwriting unrelated work. Twelve explicit files synchronized byte-for-byte; canonical venv installed exact wheel, binary/stub identity verified, canonical836+97 tests passed. No commits, pushes or private actions.

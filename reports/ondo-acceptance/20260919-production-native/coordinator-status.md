# Ondo active coordination — main agent and subagents

Updated 2026-09-19 after user replaced Pi delegation. No Pi is used. Previous Pi task/session files are historical evidence only.

## Scope and authorization

Continue production native account/WS integration and bounded execution preparation. NVDA target USD10-20, absolute USD50/order and USD100 gross. No current-turn 上主网 or final priced plan approval: no real order/cancel/DMS activation is authorized. Bounded production authenticated reads remain authorized. No commits/merge/push.

## Workspaces and ownership

- Canonical app E:/Nautilus-Perps, main base32ce150. Canonical fork E:/nautilus_trader base314107e.
- Both existing .worktrees/ondo-production-native retained; modifications uncommitted and unsynchronized.
- /root/native_trade owns fork Ondo source/bindings/tests and E:/nautilus_trader/target while implementing/testing guard. Native readonly worker finished and released ownership.
- /root/trade_app owns app src/ondo_trade_probe.py, src/ondo_trade_limits.py and tests/test_ondo_trade_probe.py.
- /root/readonly_privacy owns app src/ondo_probe.py and tests/test_ondo_probe.py for parent-reproduced privacy fixes. Initial readonly integration worker finished.
- Main agent owns review, reports/coordination, build preparation, candidate runtime and eventual bounded readonly observation.

## Evidence now

- Native readonly N2 source + Python feature full suite: 946 passed, exit0. See native-readonly/subagent-readonly.md and logs/n2-full-green.txt; source hashes n2-source-sha256.json. Factory clone/shared state, tokens, accepted login/ACK retention, scope equality verified offline.
- App readonly integration initial focused file:208 passed; parent reproduced raw runtime exception leak with failing test, so not yet accepted for live observation.
- Existing canonical native wheel public NVDA preflight succeeded, run20260919T150548006947Z in current-public-preflight. Price/quantity steps0.01. contracts.isClosed=true means UNDERLYING stock market closed, not a proven perpetual venue closure. Eligibility needs independent checks.
- Candidate app .venv created with 24 prior locked dependencies, NO candidate wheel installed yet. Canonical .venv works via require_escalated; sandbox launcher failure does not prove interpreter missing.

## Remaining sequence

1. Finish/review readonly privacy and trade app corrections.
2. Finish/review native bounded production guard; full native offline tests. Production write scope remains unavailable until actual complete enforcement is implemented.
3. Freeze source, generate stubs from generator, release wheel using existing cache E:/nautilus_trader/.worktrees/task-ondo-r5/target; no concurrent cache owner. Build helper build/build-candidate.ps1 prepared (not run). Uses existing build venv and per-process existing Windows warning exception. Prior dist-r52 preserved.
4. Candidate-only install, source/wheel/stub identity, actual factory integration, full app tests. No installed-runtime claims before this.
5. Main agent bounded production-readonly run <=2minutes, no write client/DMS; discard raw stdout, publish controlled evidence. Credentials loaded locally from canonical .env, never into model context.
6. Prepare exact current trade plan and ask required mainnet approval only after all preparation passes. Retain No Trade/Partial/Filled/Uncertain and final residual evidence honestly.

## Contract decisions

Latest trade contract: trade-app/required-native-interface.md. Envelope binds exact entry/close side/quantity/price and notional; one opening, at most two closes, owned cancels. Pre-stop fresh REST proof and post-stop clean status are separate to avoid deadlock. Mainnet readonly accessor remains exact nine-key contract.

The old max_total_requests proposal was removed: app cannot observe background signed reads. max_app_requests bounds creates+own cancels; native rate limits, timeouts and deadline bound background work. Do not claim an all-HTTP request cap.

Persistent recovery ledger: .superpowers/sdd/2026-09-19-ondo-production-native/progress.md.

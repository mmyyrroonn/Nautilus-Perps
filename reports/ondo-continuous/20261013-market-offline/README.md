# MARKET long and short offline acceptance

The existing `scripts/run_ondo_continuous.py` now supports `--test market`.
It submits four genuine native MARKET orders on BTC: buy to open long, reduce-only
sell to close long, sell to open short, and reduce-only buy to close short. Each
opening requires reconciled Flat. Each close uses only confirmed entry fills.

This report contains synthetic loopback evidence, not a mainnet execution result.
No real credentials were read and no real venue was contacted during acceptance.

## Accepted local candidate

- Interpreter: `E:/persarb/_tmp/ondo-market-20261013/venv/Scripts/python.exe`
- Wheel: `E:/persarb/_tmp/ondo-market-20261013/wheel-v2/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`
- Wheel SHA-256: `089fda49ca54bb91af84a9b3785ad69520e092cb8fadc601d205fff8979c1335`
- Provenance SHA-256: `1d0dfa5169a2ddc1d3b061734d882d5bcd0ffead5bb5f9e238ff4d438e86cc45`
- Native source binding: verified; current source fingerprint matches.

`acceptance.json` records installed binary identity, application source hashes,
native test results and artifact digests. The launcher checks this exact candidate.
This is a local installation, not a published portable release.

## Checks

The final candidate passed 25 selected continuous-mode application tests and 281
historical probe/operator tests. The 25 include two installed native LiveNode
sessions and 23 state/configuration checks. The installed MARKET session verifies
four price-less MARKET requests, correct sides and reduce-only flags, four actual
synthetic fills with fees, signed long/short positions, both round trips, final
Flat and clean native shutdown. The empty-entry session verifies a single canceled
zero-fill request, no next opening, no invented fill and no watchdog timeout.
Partial entry, partial close, fill/fee completeness and reconciliation gates have
application state tests; the loopback market fills are full or zero.

All 1,164 distinct native tests passed across the recorded final runs. One existing
read-only readiness test initially failed its short polling window; its whole
136-test target passed on rerun without code changes. The remaining targets and
doc test passed. The original failure and all commands are retained in
`native-test-summary-v2.json`. Full Clippy is not clean because of existing lint
diagnostics; its failure was not treated as a pass.

The first candidate passed the normal MARKET round trip but exposed a native
terminal-state bug when create immediately returned canceled: only Accepted
reached the cache. The final candidate emits Accepted followed by the actual
Canceled status, with no inferred fills. Native MARKET and limit IOC regression
tests cover that shared fix. Earlier maker/cancel-race installed-session tests
passed on the first candidate; those two sessions were not rerun on the final
candidate. Their logs remain under `E:/persarb/_tmp/ondo-market-20261013/`.

The native builder exited successfully. A logging wrapper failed while printing
an emoji through the Windows console after the build; independent artifact and
source-binding verification passed. See `native-build-verification-v2.json`.

After acceptance, the launcher-only confirmation prompt and TTY requirement were
removed at the operator's request. Syntax, focused lint, both offline preparation
modes and CLI execution dispatch (with execution mocked) passed. Native and Strategy
source are unchanged; the full suites were not rerun for this launcher edit.
`acceptance.json` records the previous and current launcher hashes.

## Mainnet launch

Run this in PowerShell. `--execute` starts the run without another confirmation:

```powershell
Set-Location E:\persarb\Nautilus-Perps
& E:\persarb\_tmp\ondo-market-20261013\venv\Scripts\python.exe scripts/run_ondo_continuous.py --test market --execute
```

Omit `--execute` for local preparation only. The explicit `.env` supplies the
three `ONDO_MAINNET_*` credentials. The current admission caps are USD 20 per order
and USD 40 gross, with at least USD 25 available margin; tighter configured budgets
apply. MARKET has no execution-price bound: these caps use current quote estimates.
The script stops on incomplete or ambiguous execution and does not retry. A partial
close may leave a position requiring operator review. Private run evidence is saved
under `reports/live/ondo-continuous/<run-id>/result.json`.

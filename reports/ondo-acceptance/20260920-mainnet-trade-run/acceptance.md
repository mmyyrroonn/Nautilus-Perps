# Ondo bounded mainnet trade attempt

Date: 2026-09-20 Asia/Shanghai. Authorization: the user explicitly said `上主网` in the current
turn and allowed bounded testing on the dedicated account.

## Approved plan

- Instrument: `NVDA-USD-PERP.ONDO`
- Entry: buy `0.06`, limit IOC, worst price `222.40`, entry-notional ceiling USD 14
- Exit: sell the confirmed quantity only, reduce-only limit IOC, worst price `221.47`, at most two
  attempts
- Outer limits: USD 20/order and USD 20 gross for this plan; one opening; six total app-visible
  create/cancel requests; minimum available margin 25 USDC; 120-second deadline with a five-second
  cleanup window
- Plan SHA256: `3323f01e37ed93e2c82e7e79f18562db563c9afa6c3aa03877649cbf0a01be1f`

The public capture immediately before the run reported `disabled=false` and
`underlying_market_closed=true`. It is stored in `../20260920-mainnet-trade-preflight/`.

## Actual result

**No Trade / not started.** The process exited 1 after the node reported
`readiness timeout while waiting for engine connections`.

- REST account identity: matched.
- Native start readiness: absent.
- Private login accepted: false.
- Required private subscription acknowledgements: none.
- Native account-state events/recoveries: zero.
- Orders submitted by the strategy: zero / no entry object exists.
- Final reported net position: zero, but whole-account flatness is unverified because no native
  account-state event or recovery completed.
- Native private run state: disconnected.
- Owned shutdown status: complete.
- DMS activation or release: not observed; the stop record leaves those fields unknown rather than
  asserting an action occurred.
- Production execution verified: false.
- Fees and economic result: unknown; no economic claim is made.

The evidence supports `No Trade`, not a successful production execution test. The failure occurred
before an accepted private session or native `start` snapshot. The exact engine that missed its
readiness deadline is not retained because native logging is intentionally disabled; no retry or
alternate authentication permutation was attempted.

Controlled evidence: `trade.json`, `meta.json`, `supervisor.json`. `console.txt` contains the
sanitized plan and controlled application failure only; credentials and raw native responses were
not logged.

# Backpack public observation: 2026-10-02

The installed native Backpack DataClient completed a 30-second credential-free public session
for `BTC_USDC_PERP`. It received 651 quotes, 3 trades, 28 mark prices and 7,863 order-book delta
batches. Native telemetry observed fresh quotes, continuous bounded books and fresh book data.
Shutdown completed with zero dropped report events and no report limit reached.

This is public-market acceptance. No private execution client, credentials, account request or
order mutation was used. Fees and margin in the configuration are explicitly synthetic.
Account identity, economic parameters, positive subscription acknowledgements, full-book coverage
and production execution readiness remain unverified.

## Pinned runtime

- Application source: `8dc243c198d69e03360700ea1ddde4f1a226927c`, clean; merged in app PR #16.
- Native source: `54581954666a8c51a1a4f36f94088d0adee824e7`; merged in native PR #68.
- Wheel SHA-256: `bad0768623016ea40f82c34edbc49d5e02bc3b2eb66423bf2f09953152ef16b1`.
- Loaded binary SHA-256: `12988b33d4071360c78a46ae27235ef98cc06fd819754e71ba1f69dc87b57701`.
- Installed Backpack stub SHA-256: `2be313597abe13a9086e22e794a7022b3f11af6cd835785a7e8e1580db1dbd6d`.
- Source-bound Windows CPython 3.12.9 wheel, development `nextest` profile; no cross-platform or
  release-profile claim. Native identity was verified; application identity is an observed Git/source
  fingerprint, not a signed application release.

## Attempts and retained artifacts

[The first attempt](failed-bootstrap.json) did not start the observer within its 15-second budget.
Its native telemetry recorded a partial-connect rollback; shutdown completed. The exact underlying
cause was not established. An independent unauthenticated markets request initially failed with a
network error and later returned HTTP 200.

[The second attempt](public-observation.json) completed with a 30-second budget: 30,218 ms including
shutdown, with preparation reported separately. It contains exact configuration, source and candidate
identity and event counts. Final health is stopped because it is captured after shutdown; active
freshness is established by the retained health events, not inferred from that final snapshot.

[The evidence index](evidence-index.json) records hashes and local locations of both raw public event
streams plus independently recomputed freshness/continuity observations. High-volume event files
remain local. [The configuration](public-observation.toml) is the exact successful input and contains
no account section. The earlier failure remains visible and is not counted as a successful session.

## Reproduction

Use the same source-bound candidate and its provenance with the application source above. In
`E:/persarb/worktrees/backpack-app-public`, run:

```powershell
.venv/Scripts/python.exe src/backpack_probe.py `
  --config E:/persarb/.backpack-work/public-venue-observation.toml `
  --candidate-wheel E:/persarb/.backpack-artifacts/public-5458195466/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl `
  --candidate-sha256 bad0768623016ea40f82c34edbc49d5e02bc3b2eb66423bf2f09953152ef16b1 `
  --native-provenance E:/persarb/.backpack-artifacts/public-5458195466/native-provenance.json
```

Live market counts naturally vary on later runs. The actual installed-wheel loopback regression
suite separately passed 71 tests, including three LiveNode tests for data, reconnection/depth recovery,
cancellation and failed startup. Replay/paper, private account runtime and restricted execution keep
separate acceptance tasks. No public observation authorizes mainnet orders.

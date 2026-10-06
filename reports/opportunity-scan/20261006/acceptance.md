# Event-only scanner acceptance — 2026-10-06

Issue: https://github.com/mmyyrroonn/Nautilus-Perps/issues/32

Isolated worktree: `E:\persarb\worktrees\opportunity-scan`, branch `feature/opportunity-snapshots`,
based on application main `68a10148e68444ec46d9730cc11c410995559e10`.
The existing dirty application checkout and native fork were not modified.

## Accepted offline behavior

- One configuration handles multiple canonical underlyings and multiple public venue clients.
  Example mappings cover BTC/ETH on Hyperliquid, Lighter, Aster, Ondo and Backpack.
- Native lot constraints and contract multipliers are retained. An independent canonical multiplier
  converts markets such as 1000-token products without cancelling actual contract exposure.
  All qualification math and finite-decimal normalization are exact; unsupported non-terminating
  price normalization fails closed. Both legs use the same canonical quantity.
- Target-budget depth, fees, minima, invalid/crossed books, age, receive age and source skew are checked.
  Unknown fees never qualify. Entry estimates explicitly leave full-round-trip profitability unknown.
- No qualifying opportunity creates no observation file/directory. Only qualifying events append JSONL,
  with the exact detached native and normalized snapshots used in the calculation. Disabled recording
  has no observation output or dedup state. Cooldown/material-change dedup is configurable.
- Only public data factories are registered. No credentials, execution clients, raw Ondo recording,
  CSV tapes, historical sample buffers or persisted native state are used.
- Incomplete initial snapshots/increments, incomplete update batches, Ondo halt/feed/metadata states,
  Backpack health/epoch changes, depth overflow, disk errors and post-stop callbacks are covered.
- Actual installed-wheel LiveNode acceptance against an owned numeric loopback Backpack peer confirms
  metadata startup, depth-only subscriptions, timer-driven refresh, current book receipt, clean transport
  closure and no output when only one leg is available. This is not a public-exchange observation.

## Test results

CPython 3.12.9, Windows, isolated `.venv` with application dependencies from `uv.lock`:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests\test_opportunity_core.py tests\test_opportunity_scan.py tests\test_opportunity_native.py tests\test_spread_watch.py tests\test_ondo_depth.py tests\test_market_tape.py
```

**298 passed in 3.48 seconds:** 101 scanner tests, 197 affected existing tests.

Pure Python PR CI command, independently reproduced without native/site dependencies:

```powershell
uv run --isolated --no-project --python 3.12.9 --with pytest==9.1.1 python -m pytest -q -p no:cacheprovider tests/test_opportunity_core.py tests/test_opportunity_scan.py tests/test_opportunity_native.py
```

**91 passed, 10 skipped in 0.33 seconds.** Skips are explicitly native-dependent cases;
their local installed-wheel results are included in the 298-pass result. PR CI verifies core/config/state,
not installed-wheel integration. No real orders were sent.

## Native candidate identity

The installed wheel and binary were matched by `scripts/verify_native_install.py`, with
`--require-source-binding --additional-adapter backpack`. No native build was performed for this change.

- Existing local candidate: `E:\persarb\worktrees\issue84-evidence\windows\wheel-output\nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`.
- Wheel SHA-256: `ce6e0bdecbaa4ca3397c45a9be4a3eca06e21368edbc0c86313b6ba0237c4ab9`.
- Installed binary SHA-256: `524c130b63d0b17a9f68512119309c410e09c9af5f893b4cbfaf4bb9023c65c9`.
- Source binding: verified pre/post build fingerprint. Declared native commit:
  `f06bfe809d8dab319cf5b85fcd5910412417ba16`; its recorded build source also includes four audit/workflow
  file changes, tracked-diff SHA-256 `3e8efea59d2f9bf077ca9d02f2fd65fd5f3113ac7bd91aaf1e38e5f73618982e`.
  This is a local development candidate, not a claim of published native-lock acceptance or a clean native tree.

## Bounded-state replay

[Aggregate artifact](benchmark.json). Synthetic 100 underlyings × 5 venues, 20 levels per side;
native quantity 10 per level, lot step 0.01, multiplier/FX 1, buy budget USD 500, fee 1 bps.
No-opportunity books have bid levels `99 - i/10`, ask levels `100 + i/10`; the qualified case adds
venue offsets 0..4 to both sides. The replay cycles through all 500 markets with monotonic nanosecond
timestamps, replaces both current normalized books and raw snapshots, and disables recording.

| Case | Updates | Seconds | Updates/second | Current books / raw snapshots |
|---|---:|---:|---:|---:|
| No opportunity | 2,000 | 0.130 | ~15,388 | 500 / 500 |
| Many qualified directions | 2,000 | 1.106 | ~1,809 | 500 / 500 |

The qualified fixture retains 600 small current display rows and zero recorder keys/history.
In a separate `tracemalloc` run, after all 500 raw snapshots are warm, 2,000 additional replacements
increase live Python allocations by 28 bytes (9,722,400 → 9,722,428 bytes after GC).
These are short Python-state replay measurements: native parsing, book assembly, network delivery,
terminal refresh, real disk writes and total process RSS are excluded. They do not establish live capacity,
a universal updates/second ceiling, or a long-duration absence of leaks.

## Unaccepted external behavior

A 15-second, record-disabled BTC public-data smoke using Hyperliquid/Lighter stopped during bootstrap:
both clients reported connection/bootstrap errors, followed by the 10-second readiness timeout.
No observer startup or real paired book comparison was accepted. The environment has restricted network
access, but the exact external failure cause was not established. This PR does not claim that the smoke passed.

Real public observation, all five venues simultaneously, 100-coin live throughput, long reconnect soak,
funding/exit economics and actual fillability remain unverified. Configuration supports up to 1,000 explicit
market mappings; that bound is a state/config limit rather than an accepted live capacity.

Primary and independent reviews found and corrected native/canonical multiplier cancellation, context
rounding during normalization, invalid minimum constraints becoming absent, and falsy malformed economics
tables. The final independent review reported no remaining material defect in the implemented contract.

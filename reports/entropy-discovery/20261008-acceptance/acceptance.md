# Entropy issue #42 acceptance — 2026-10-08

Issue: https://github.com/mmyyrroonn/Nautilus-Perps/issues/42

Accepted scope: dynamic public io discovery, explicit observation matching, normal
scanner metadata admission, same-quantity L2 estimates, installed-wheel loopback and
a bounded current public observation. No execution or account capability is accepted.

## Candidate and sources

- Application base: latest main `ec4f03637cf3108786371443b60f0aefd11b2fa7`, checked through
  the GitHub main ref before implementation and again before preparing the PR.
- Native repository current main: `5dca5481db9d602ec4f250696830e7173f00dd31`.
  This change does not modify native code or claim that the installed wheel contains
  every change in that newer source tree.
- Actual installed published Windows wheel: native source
  `eeeb8eefc7921b7f81cb611cb3c851c665cd1ff1`, wheel SHA256
  `802e8eead8ca24ca52324eade773b20ea9dd5f9c8701ccfd628c32bca42a0783`.
  Installed binary, stubs, build features and verified source binding are in
  [installed-candidate.json](installed-candidate.json) and [candidate-identity.json](candidate-identity.json).
- CPython 3.12.9 / uv 0.12.6, isolated
  `E:\persarb\worktrees\entropy-app\.venv`; install used `scripts/install_native.py`
  with the formal `config/native-candidate.lock.json`, not an equal-version PyPI fallback.
- The original application and native dirty checkouts were preserved. Implementation
  worktree: `E:\persarb\worktrees\entropy-app`, `feature/entropy-discovery`.

Current official protocol/document evidence and source bytes are in
[20261008-public](../20261008-public/README.md). Current scanner inputs, config,
request times, hashes, errors and public logs are in
[public-observation](public-observation/README.md).

## Implemented and reviewed

- Explicit `dex=io`; `perpDexs` null slots and original universe/context indexes are
  preserved. Full io inventory is independent of the small matching table.
- Collateral uses the token's explicit index and known mainnet canonical USDC
  identity. Context mismatch, duplicate identity, malformed precision, unknown status
  and currencies block affected mappings or fail the malformed envelope.
- Two explicitly declared io/Aster equity-perpetual pairs have issuer/reference,
  unit, FX, oracle, session, corporate-action and fee sources/assumptions.
  They are read-only comparison declarations, not hedge-equivalence certification.
  Automatic crypto selection remains unchanged; intersecting canonical groups fail.
- Each io comparison leg carries `expected_instrument`. Runtime verifies raw symbol,
  quote/settlement currencies and native lot/multiplier against the actual cache.
  Scope and native identity survive into opportunity evidence.
- Capture and replay retain raw component bytes, exact hashes, request dex and
  capture UTC. Tampered components/wrappers/counterleg bytes and wrong dex provenance
  fail. No sidecar URL/path is used for arbitrary network/file access.
- Current funding contexts and deployer multipliers are recorded without applying
  the multiplier twice. Predicted/actual funding is unknown. Public fee and stablecoin
  assumptions are explicit; Aster RWA sample is 1.25 bps, not the old 0.9 bps.
- PR CI includes the new catalog, CLI and native-optional tests and relevant paths.
  Offline preview performs no native import, account read or output creation.

Independent review: [review.md](review.md). All material review findings were resolved:
mandatory metadata for both io comparison legs, normal configuration in actual native
tests, current source URLs, and automatic CI coverage. No unresolved material code
finding was reported.

## Local checks

Actual installed candidate command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider tests/test_entropy_discovery.py tests/test_entropy_universe.py tests/test_entropy_native.py tests/test_opportunity_universe.py tests/test_opportunity_core.py tests/test_opportunity_scan.py tests/test_opportunity_native.py tests/test_opportunity_connections.py tests/test_spread_watch.py
```

**310 passed, 5 skipped in 6.56 seconds.** Skips are existing Backpack-dependent
cases because this published candidate lacks Backpack. The Entropy actual native
test executed successfully; it is not one of the skips.

Exact isolated PR CI command:

```powershell
uv run --isolated --no-project --python 3.12.9 --with pytest==9.1.1 python -m pytest -q -p no:cacheprovider tests/test_opportunity_core.py tests/test_opportunity_scan.py tests/test_opportunity_native.py tests/test_opportunity_universe.py tests/test_opportunity_connections.py tests/test_opportunity_coalescing.py tests/test_entropy_universe.py tests/test_entropy_discovery.py tests/test_entropy_native.py
```

**213 passed, 22 skipped in 0.85 seconds.** Native/site-dependent cases skip in the
deliberately pure environment. This CI result does not replace actual-wheel acceptance.
`git diff --check` also passed.

Final native evidence-counter check: `python -m pytest -q -p no:cacheprovider
tests/test_entropy_native.py --basetemp .venv/entropy-final-loopback` passed
**1 test in 5.26 seconds**. The summary now distinguishes the pre-stop sample from
the final event-file count and asserts the final count equals the recorder counter.
An initial invocation used a missing parent for `--basetemp` and failed at fixture
setup before any native startup; the corrected command above completed normally.

## Actual native loopback

Normal public factories and `LiveNode` used owned numeric loopback HTTP/WS peers
with explicitly synthetic inputs following current official schemas. The test used
generated TOML and `load_plan` / `parse_plan`, then actual native cache/events.

- io and xyz share one `HYPERLIQUID` factory; Aster uses one independent factory.
- Three instruments have native metadata and double-sided L2; native increments
  and io price precision are verified. The example multi-level estimate uses the
  same canonical quantity, 0.99, on both legs.
- Shallow depth, empty side, stale source/receive time and source skew reject
  qualification; a silent connected socket loses freshness.
- An actual Hyperliquid socket close creates a second connection. Aster remains
  fresh; the old io snapshot exceeds the freshness limit and cannot qualify until
  a new fresh L2 snapshot arrives. Immediate invalidation at socket close is not
  accepted by this test.
- Stop completes with zero active peer sockets and no execution registration.

[Native summary](native-loopback-summary.json) and
[synthetic event snapshots](native-loopback-events.jsonl) are retained. The final
19 saved rows match the recorder counter; the separately labelled pre-stop sample
in this final run was also 19. These observations include repeated onset after full
CLEAR snapshots; they are not 19
independent real-market opportunities. Aster physical reconnect is not accepted here.

## Current public observation

One actual 60-second scanner window completed with exit 0, actor started and no
actor failure. All four SNDK/GPRO io/Aster legs were observed fresh; peak fresh legs
were 4 and peak time-aligned symbols were 2. Native updates were io:SNDK 11,
io:GPRO 11, SNDKUSD1 222, GPROUSD1 15.

The original 2-second source/receive freshness and 500-ms skew limits were retained.
At the end only SNDK/Aster was usable: both io legs and GPRO/Aster were stale and
correctly excluded. This accepts bounded coverage, not uninterrupted fillability
or a continuous strategy. Recording was disabled, so saved=0 says nothing about
the number of opportunities. No account or order request occurred.

Initial metadata TLS failure and an observation-helper import error were retained;
the helper error happened before native startup. The successful window and current
raw request receipts are separate from those failures. Public shutdown evidence is
normal `node.run` return and process exit; the normal API does not expose final
public socket counts, so loopback counts are not reused as public metrics.

## Remaining capabilities

Securities identity/economic rights, contract-specific company actions, precise
off-hours configuration, stablecoin basis and account fee tier are not fully
verified. Unknown economic equivalence remains explicit and blocks using this
result as trading approval. There was no account, agent/vault verification, actual
fill, funding receipt, PnL, mainnet order, transfer or leverage change.

Native #100–#102 must establish io account/margin scope, bounded execution/recovery
and economic receipts. Application #43 then consumes the corresponding actual
candidate. The older wheel used here cannot inherit those future native changes.

# Ondo BTC production read-only acceptance — 2026-09-20

## Verdict

BTC passed the bounded production **read-only** acceptance with the final release wheel.
The same run confirmed account identity, private WebSocket login, both required subscription
ACKs, signed account recovery, account-state publication, and complete native shutdown.
The probe submitted no orders and had no write, cancel, or DMS capability.

This is an A/B acceptance (public market data plus production account read-only), not a
sandbox execution or production-order acceptance.

## Final evidence

- Public BTC and all-enabled discovery: `../20260920-btc-functional/`
- Final private read-only report: `verified-run-final-release-v2/probe.json`
- Final private run id: `20260920T113123374772Z`
- Final native release wheel:
  `E:/Nautilus-Perps/.worktrees/ondo-btc-native/dist-btc-readonly-final/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`
- Wheel SHA-256:
  `1EF8589BD982FF40171B83C59736B9C571B44D6E7CF175EBE320E9B9C4AE2CE8`
- Installed native module SHA-256:
  `98201B45AA8CCCC422E1ECA68765D4489E0F480C9F5F19EA837B9B4EE2B07D95`
- Ondo Python interface stub SHA-256:
  `9251ED9A23ADEB8F16F5CC7766F3C9DC2A2404BB0EC84A435EDAA2CD46AA3972`

Final report facts:

| Field | Observed |
| --- | --- |
| `complete` / `exit_code` | `true` / `0` |
| instrument | `BTC-USD-PERP.ONDO` |
| identity / login | `matched` / `true` |
| subscription ACKs | `fillsPerps`, `ordersPerps` |
| recoveries / account-state events | `2` / `2` |
| native shutdown | `complete` |
| account reconciled | `true` |
| account coverage proven | `false` |
| orders submitted by probe | `0` |
| write / cancel / DMS capable | `false` / `false` / `false` |
| production read-only support verified | `true` |
| full protocol verified | `false` |

`account_coverage_proven=false` is intentional: a recovery and account-state event do not
prove complete per-run position, order, and fill coverage. `protocol_verified=false` is also
intentional: this run host-confirmed only the authenticated read-only subset, not order
submission/cancellation, order/fill event payloads, or DMS renewal/release.

## Defects found and fixed

1. The adapter observed the venue HTTP `Date` offset but did not apply it. Later signed REST
   requests and WebSocket login could fail with `timestamp_too_far`. The shared auth clock now
   applies the bounded venue-minus-local offset to both paths.
2. Production read-only startup had an effective 8–10 second readiness window while the
   shared private REST budget is one request per second. Native readiness now uses its
   configured bounded timeout (up to 30 seconds), and the application gives this mode a
   30-second engine connection window.
3. The production empty-order response uses `pageInfo.nextCursor=""`. That exact location and
   value now means end-of-history; null, whitespace, numeric, root, and other malformed cursor
   forms remain fail-closed.
4. A newly established read-only session waited for the periodic reconciliation timer. It now
   starts one immediate recovery only after login and both subscription ACKs. Trading-mode DMS
   behavior is unchanged.
5. The application rejected its own `[ondo_trade]` configuration as an unknown maker limit
   section. That separately owned section is now excluded while all other unknown sections
   remain fail-closed.
6. A passed production read-only report used stale text claiming that no authenticated private
   request had been confirmed. The report now names the authenticated read-only facts while
   keeping full `protocol_verified=false`.

## Verification

- Native Ondo test suite: 1,030 passed (477 unit, 552 integration, 1 doc test).
- Native feature build check: `cargo check -p nautilus-ondo --features python` completed.
- Python feature tests: 9 passed.
- Application suite: 1,099 passed and 97 subtests passed; one pre-existing pytest warning in
  `tests/test_maker_live.py::test_limits` remains.
- Final production read-only run: exit 0 after the 30-second deadline, with the facts listed
  above and zero writes.

The release build required the scoped `CARGO_BUILD_WARNINGS=allow` Windows linker-warning
exception. It is not a strict warning-free build. The resulting module still resolves
`zlib.dll` from `C:/ProgramData/miniconda3`; local acceptance passed, but wheel portability to
another machine is unverified.

## Remaining gates

- Sandbox order/fill and real DMS host semantics are not tested because this checkout has no
  sandbox credentials.
- Production execution was not requested or attempted. It still requires the exact current-turn
  authorization `上主网` and a reviewed concrete order plan.
- The changes and final wheel remain in isolated worktrees; canonical installation, commit, and
  merge are separate integration actions.

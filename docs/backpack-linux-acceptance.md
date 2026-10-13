# Backpack Linux launcher

The Linux launcher uses the application `.venv/bin/python`, limits configuration
and selected local `.env` file. It runs the same public planning, signed read-only
flat preflight and native operator lifecycle as the Windows launcher. Financial
mutations pass through Nautilus Strategy and native execution; independent REST
cleanup scripts and deployment-specific entrypoints are outside this PR.

The unchanged native candidate lock does not contain the Backpack production API.
Before using the launcher, build a source-bound Linux wheel from the accompanying
native production changes, then install and verify it with the explicit
[local-override workflow](native-candidate.md). Do not substitute the existing
locked wheel or an equal-version package. This PR does not publish cross-platform
artifacts or update the native candidate lock.

From the application root, substitute local absolute paths:

```bash
/absolute/path/to/app/.venv/bin/python scripts/backpack_roundtrip_5u.py --scenario both --fresh-acceptance
/absolute/path/to/app/.venv/bin/python scripts/backpack_roundtrip_5u.py --scenario both --fresh-acceptance --env-file /absolute/path/to/app/.env --execute --confirm-mainnet
```

The first command performs public sizing and dry-run validation. The second requires
a signed flat account/no-open-order preflight before creating isolated native state,
then runs Maker entry / reduce-only Market exit and Market entry / reduce-only Market
exit. Financial mode requires both explicit flags and a fresh-session or existing-state
selection. Parameter errors exit before launching a child.

For original-state recovery with no new entry:

```bash
/absolute/path/to/app/.venv/bin/python scripts/backpack_roundtrip_5u.py --scenario market --close-only --state-dir /absolute/path/to/original/state --entry-client-order-id ORIGINAL_ENTRY_ID --max-close-quantity 0.00005 --env-file /absolute/path/to/app/.env --execute --confirm-mainnet
```

The launcher holds a local advisory lock during live invocation and preserves unique
run logs, result JSON and native state. Child deadlines cause a bounded interruption
and termination sequence; they cannot guarantee exchange cleanup. Keep original state
and inspect residual or unknown outcomes before repeating. These raw artifacts are
excluded from Git and are not linked from public documentation.

See the [Maker/Market guide](backpack-maker-market-acceptance.md) for timing, sizing,
reconciliation and recovery behavior. The [sanitized validation record](backpack-production-acceptance.md)
reports four real filled and terminal-reconciled legs and later REST flatness, while
retaining the known `account_reconciled=false` final wrapper result. It does not claim
complete issue 123 acceptance or a repaired cleanup-to-session handoff.

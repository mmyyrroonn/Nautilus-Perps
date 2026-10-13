# Human-operated mainnet startup check

The launcher completed one human-operated BTC mainnet startup check on 2026-10-13
using the exact reviewed native candidate and continuous Strategy. The process
returned normally after confirmed Flat, matching DMS release ACK and clean native
shutdown. Published aggregate evidence and artifact digests are in the
[sanitized mainnet report](../reports/ondo-continuous/20261013-mainnet/README.md).

That later observation verifies one BTC startup sequence. The earlier synthetic
BTC/TSLA continuous follow-up evidence remains an offline result; it is not a
mainnet multi-symbol or sustained trading result. Historical authorization does
not authorize another run. The preparation and execution paths remain distinct.

Run these commands yourself in an interactive PowerShell terminal:

```powershell
Set-Location E:\persarb\worktrees\issue122-ondo-app
& E:\persarb\_tmp\issue122-acceptance\venv\Scripts\python.exe scripts/run_ondo_continuous.py --execute
```

The terminal displays resolved limits and asks the operator to type `上主网`.
Without `--execute`, the same command only verifies local candidate identity and
prints the preparation record; it never loads credentials or starts a node.

The explicit credential file is `E:\persarb\Nautilus-Perps\.env`. It must contain
`ONDO_MAINNET_API_KEY`, `ONDO_MAINNET_API_SECRET` and `ONDO_MAINNET_ACCOUNT_ID`.
Do not paste values into commands, reports or chat. Another file can be selected
with `--env-file`; there is no sandbox or inherited-account fallback.

This entry point runs BTC startup acceptance through continuous native execution:
post-only placement, confirmed cancellation and replacement with a new order ID,
then a protected IOC entry and an exact reduce-only exit. It uses the venue's
minimum quantity and current metadata, with at most USD 20 per order and USD 40
gross. Tighter `[ondo_trade]` account budgets in `config/limits.toml` still apply.
Available margin must be at least USD 25, or the higher configured floor.

The process stops after the Strategy reaches continuous mode with confirmed Flat.
It does not install a trading signal or start an unattended strategy. The adapter
retains its general multi-symbol continuous execution capability.

An actual maker fill/cancel race or partial IOC exit can leave a position. The
launcher reports incomplete, preserves the journal and does not guess an exit or
retry. A timeout does not mean Flat. Inspect venue orders and positions before
any subsequent run; DMS cancellation does not flatten positions.

Evidence is written to
`reports/live/ondo-continuous/<run-id>/result.json` (gitignored), including native
venue order IDs, client order IDs, trade IDs, fills, commissions, account balances,
Cache/Portfolio positions and final native shutdown proof. Recovery journals stay
beside it. `complete=true` requires the confirmed startup round trip and native
Flat/shutdown evidence. A create request alone is not a fill.

## Local candidate and publication limits

This launcher binds the reviewed Strategy hash, exact isolated interpreter,
source-bound wheel and provenance recorded in the offline acceptance file. The
paths above describe the verified local Windows installation. Moving the
interpreter, changing source or using a fresh checkout requires a new reviewed
candidate record; there is no equal-version PyPI fallback. It is not a portable
release installer or an unattended multi-symbol trading entry point.

Full local run evidence contains account balances and native order/trade ids.
Publish only the allowlisted aggregate report under
`reports/ondo-continuous/20261013-mainnet/`; do not commit private results, recovery
journals, raw logs or credential files. The published report retains source-file,
wheel, binary, Strategy and launcher hashes. It does not freshly query an account
or refresh the historical Flat observation.

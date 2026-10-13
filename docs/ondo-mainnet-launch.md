# Human-operated mainnet execution checks

## Market long and short test

The existing launcher now accepts `--test market`. This runs four genuine native
`MARKET` orders on BTC: open long (buy), close long (reduce-only sell), confirm Flat,
open short (sell), and close short (reduce-only buy). No maker orders are submitted
in this mode. Both round trips must pass before the report can say `complete=true`.
One human-operated mainnet run completed this sequence on 2026-10-13; its
[aggregate evidence](../reports/ondo-continuous/20261013-market-mainnet/README.md)
records four confirmed fills and final Flat. That run predates the prompt-only
launcher edit; the native adapter and Strategy are unchanged.

Run preparation first; it verifies local hashes without credentials or venue access:

```powershell
Set-Location E:\persarb\Nautilus-Perps
& E:\persarb\_tmp\ondo-market-20261013\venv\Scripts\python.exe scripts/run_ondo_continuous.py --test market
```

To place the real mainnet orders, run the same command with `--execute` yourself.
This flag starts execution without an additional confirmation prompt:

```powershell
& E:\persarb\_tmp\ondo-market-20261013\venv\Scripts\python.exe scripts/run_ondo_continuous.py --test market --execute
```

The quantity is the current instrument minimum, subject to the existing USD 20
per-order and USD 40 gross admission limits and USD 25 available-margin floor.
Tighter account budgets still apply. A MARKET order sends neither `price` nor
`timeInForce` to Ondo. Its notional checks use current native quotes; they cannot
guarantee the execution price or bound realized slippage. The sizing headroom is
not a 20-bps execution guarantee.

Each close uses only the terminal entry's confirmed filled quantity, after native
account and application position reconciliation. Both opening directions require
Flat. Zero fills, a partial close, missing fee/fill coverage, unknown submissions,
or unreconciled positions prevent the next opening and leave an incomplete report.
There is no automatic retry. Review any residual position before another run.

This mode uses a newly built native candidate because the earlier continuous
admission accepted only protected limit orders. Its offline evidence is in
`reports/ondo-continuous/20261013-market-offline/`. The historical result below
does not verify the new MARKET sequence on mainnet.

## Historical maker startup observation

The launcher completed one human-operated BTC mainnet startup check on 2026-10-13
using the exact reviewed native candidate and continuous Strategy. The process
returned normally after confirmed Flat, matching DMS release ACK and clean native
shutdown. Published aggregate evidence and artifact digests are in the
[sanitized mainnet report](../reports/ondo-continuous/20261013-mainnet/README.md).

That later observation verifies one BTC startup sequence. The earlier synthetic
BTC/TSLA continuous follow-up evidence remains an offline result; it is not a
mainnet multi-symbol or sustained trading result. Historical authorization does
not authorize another run. The preparation and execution paths remain distinct.

Run these commands yourself in PowerShell:

```powershell
Set-Location E:\persarb\Nautilus-Perps
& E:\persarb\_tmp\ondo-market-20261013\venv\Scripts\python.exe scripts/run_ondo_continuous.py --test maker --execute
```

The terminal displays resolved limits and starts the explicitly requested execution.
Without `--execute`, the same command only verifies local candidate identity and
prints the preparation record; it never loads credentials or starts a node.

The explicit credential file is `E:\persarb\Nautilus-Perps\.env`. It must contain
`ONDO_MAINNET_API_KEY`, `ONDO_MAINNET_API_SECRET` and `ONDO_MAINNET_ACCOUNT_ID`.
Do not paste values into commands, reports or chat. Another file can be selected
with `--env-file`; there is no sandbox or inherited-account fallback.

The `--test maker` mode runs BTC startup acceptance through continuous native execution:
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
Publish only allowlisted aggregate reports under
`reports/ondo-continuous/`; do not commit private results, recovery
journals, raw logs or credential files. The published report retains source-file,
wheel, binary, Strategy and launcher hashes. It does not freshly query an account
or refresh the historical Flat observation.

# Ondo dynamic market discovery and BTC public verification

Date: 2026-09-20. Scope: public read-only discovery and bounded data-client probes only.
No credential was read, no execution client was registered, no order was submitted, and no
dead-man switch was armed.

## Delivered behavior

- Explicit Ondo symbols are no longer restricted to the historical NVDA/TSLA allowlist.
  A symbol such as `BTC` derives `BTC-USD.P` and `BTC-USD-PERP.ONDO`, then the real preflight
  still verifies the market and normalized instrument against the venue responses.
- `--symbols ALL` discovers the venue's current enabled USD perpetual markets before loading
  instruments. Disabled and unknown markets are not selected.
- Dynamic discovery runs only after offline argument and authorization gates pass. A refused
  sandbox invocation therefore performs zero discovery requests.
- The curated cross-venue `spread_watch.INSTRUMENTS` registry is unchanged. Dynamic Ondo
  discovery does not infer a corresponding market on another venue.

## Current public evidence

- `preflight/`: BTC complete public preflight. BTC was active with quantity increment `0.0001`,
  price increment `1`, maker fee `1` bp, and taker fee `2.5` bps.
- `preflight-all/`: complete dynamic preflight over 81 venue markets: 65 enabled, 16 disabled,
  zero unknown; all 65 selected instruments passed normalization with zero missing items.
- `probe-btc/`: 30-second production-public session, one BTC instrument, exit 0 and
  `complete=true`.
- `probe-all-final2/`: 12-second production-public session. The runtime loaded 65 instruments,
  reached `All engine clients connected`, stopped at the bounded deadline, disconnected the
  data client, and exited 0 with `complete=true`.

Both live probes observed one transient `tls handshake eof`; the adapter retried and connected.
The JSON probe report records bounded-process completion but not a data-event counter, so the
connection statement depends on the observed runtime console output and is not an exchange-level
coverage or no-gap claim.

## Verification

- Targeted final verification: `432 passed` for `test_ondo_preflight.py`,
  `test_ondo_probe.py`, and `test_ondo_trade_probe.py`.
- Python compile check and `git diff --check`: passed.
- Full application run: `1122 passed`, `97 subtests passed`, `3 failed`. The three failures are
  outside this change: the existing maker limits loader treats the already present
  `[ondo_trade]` table in `config/limits.toml` as unread keys. No file in that failure path was
  changed here.
- The repository `.venv` launcher currently points to a missing uv-managed Python 3.12.9.
  Verification used system Python 3.12.4 with the repository `.venv/Lib/site-packages` on
  `PYTHONPATH`; the installed Nautilus wheel and pytest dependencies were loaded from that venv.

This is public-feed functionality evidence only. It is not sandbox authentication, private
protocol, order, fill, reconciliation, profitability, or production-trading acceptance.

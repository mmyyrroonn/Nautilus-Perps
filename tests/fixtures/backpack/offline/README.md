# Synthetic offline session

`session.json` and `btc.jsonl` are synthetic offline replay/paper fixtures.
The market metadata text is the public BTC metadata fixture already recorded
in `../btc_market.json`; its public source is documented in `../README.md`.
The quotes, sequence numbers, timestamps and depth snapshot are manufactured
for deterministic native parser and BacktestEngine acceptance. No account,
credential, real order or private data is present. Economics live in the explicit
example TOML and are marked Synthetic, independent of market provenance.

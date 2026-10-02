# Backpack public loopback fixtures

`btc_market.json` is the credential-free public BTC market response captured by the native
adapter and reused from `nautilus_trader/crates/adapters/backpack/test_data/btc_usdc_perp.json`.
Source: <https://api.backpack.exchange/api/v1/markets> (captured 2026-10-02).
The HTTP server, depth snapshots and all streaming frames in the acceptance test are synthetic.
Their shapes follow <https://docs.backpack.exchange/#tag/Streams> and the reviewed native parser.
No private account, credentials or venue mutation appears in these fixtures.

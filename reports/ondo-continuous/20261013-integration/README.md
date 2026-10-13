# Latest-main integration acceptance

The application change was integrated onto `a8031fd1e2051ece37ba063c81ae62db7b12a18a`,
preserving the current candidate verifier, joint CI and Backpack implementation.
Useful unpublished operator/candidate binding, native economic evidence, sanitized
shutdown diagnostics and the installed-wheel shutdown/recovery scenarios were
included alongside continuous execution. Experimental raw HTTP cleanup helpers
remain local and are not part of this integration.

The final selected application suite passed **304 cases, zero failures, errors or
skips**, in 720.11 seconds. `summary.json` contains per-module counts and source-file
hashes; `junit.xml` and `pytest.log` preserve the original output bytes. This is one
final integration run, not a sum of overlapping suite counts.

```text
E:/persarb/_tmp/issue122-acceptance/venv/Scripts/python.exe -m pytest tests/test_ondo_btc_operator.py tests/test_ondo_trade_probe.py tests/test_ondo_dms_diagnostics.py tests/test_native_candidate.py tests/test_ondo_continuous.py tests/test_ondo_installed_wheel_shutdown.py -q --junitxml=E:/persarb/_tmp/issue122-acceptance/pr-integration-junit.xml --basetemp=E:/persarb/_tmp/issue122-acceptance/pytest-pr-integration
```

Both `ONDO_CONTINUOUS_*` and `ONDO_TEST_*` candidate variables named the reviewed
`wheel-v2` artifact and provenance with their explicit SHA-256 values. Dotenv
discovery was disabled; private child processes used only synthetic loopback
credentials. The additional shutdown scenarios cover matching/delayed/missing/
wrong/ambiguous ACKs, connection loss, historical orders, an outer disconnect
timeout, process interruption and read-only recovery.

Native implementation commit `4af29e39a45bf8c1cdc2aa63cbfb6810fe4f5f9f` was merged by
[native PR #124](https://github.com/mmyyrroonn/nautilus_trader/pull/124) as
`5987c21b1d370cd58baa54f348d3eb02313ff279`. Its base has the same tree as the tested
native base, and all 18 implementation files copied into the PR matched the
previously tested working files byte-for-byte before Git serialization. Native
Rust formatting and staged diff checks passed. `make format` and `make pre-commit`
could not run because this Windows host has no `make`; no full pre-commit pass is
claimed. Generated stubs and the 1,156 native cases remain attached to the
[original offline candidate](../20261013-offline/README.md).

The [mainnet BTC summary](../20261013-mainnet/README.md) is a separate historical
operator observation. No account was contacted and no real order was sent while
preparing, reviewing or integrating these PRs. Private results, account/order/trade
IDs and journals were not included in the publication. The formal dependency lock
was not changed and no new portable release is claimed.

The earlier offline report's uncommitted/source-worktree descriptions and counts
describe that earlier snapshot. This integration record adds the later mainline
handoff without rewriting its source archives or converting synthetic results
into live acceptance.

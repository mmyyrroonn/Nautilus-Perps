# Native readonly account loopback acceptance: 2026-10-02

This is synthetic numeric-loopback acceptance, using an owned Ed25519 peer and
public test key material. It contains no real account observation, credential or
production request. Application source is frozen at
`0591ee18f65e198de5a4defb7c65e010ef87b989`; native source is
`deff8571b30afcffa6c8f26e04c8a90d0b8d9c53`.

The actual `src/backpack_probe.py` CLI exited 0 against the real installed
source-bound readonly wheel:

- Wheel SHA256: `0ca12206c1c771921cf75beff1586a2ac21938cb3aa6fdb2f6c901c2cadb3c2e`.
- `verify_native_install.py --require-source-binding --additional-adapter backpack`
  passed; the CLI independently verified that candidate again before constructing
  native factories.
- The peer verified six authenticated GET signatures and one private subscription
  signature. Signature verification does not establish a subscription ACK.
- All requests were GET; native public and account factories shared one REST quota.
- Actual native engine cache observed the synthetic USDC free balance 111.00000000,
  true trade IDs 77/78, filled quantity 0.00002 and total rebate -0.00000200 USDC.
  The repeated trade 78 did not duplicate economics.
- Native pending fills remained 2; no durable consumer acknowledgement was claimed.
- Native shutdown completed and the peer had zero active sockets after CLI return.

`account-observation.json` preserves the full sanitized native/runtime summary.
`engine-cache-observation.json` is the final real cache observation from that run.
`peer-evidence.json` records protocol verification counts without headers, signatures
or keys. `evidence-index.json` binds these artifacts and the original bounded event
file SHA256. Original files and strict installation evidence remain in
`E:/persarb/.backpack-work/app-account-verified/`.

The six actual native account tests additionally prove cancellation while the
outer cancelled Task remains referenced, same-namespace lock reopening, malformed
pagination failure and reopening, freshly signed private reconnect with duplicate
fill suppression, conflicting true trade identity failure, and separate account /
subaccount scopes using distinct synthetic credentials without old cache economics.
The root independently reran 114 combined public/config/account/replay/paper tests.

Empty cache never proves Flat. Wallet trading balances do not verify usable
margin. Account labels, private subscription confirmation and complete history
coverage remain unverified. These local results grant no real-account access or
execution readiness.

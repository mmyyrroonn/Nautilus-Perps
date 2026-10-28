# vultr-jp Ondo maker deployment

Deployed and verified on 2026-10-28. The live trading service is installed,
disabled, and inactive. No production order commands were sent during deployment.

## Locations and configuration

- Application: `/opt/ondo-maker/releases/20261028-500/app`
- Interpreter: application `.venv/bin/python`, CPython 3.12.9
- Native source: `/opt/ondo-maker/releases/20261028-500/native`
- Service: `ondo-maker.service`, running as `ondo-maker` when manually started
- Credentials: application `.env`, ignored by Git, mode `0600`, owner `ondo-maker`
- Journals: application `reports/live/ondo-maker/<run-id>/`
- Existing `ondo-rewards.service` remains active and unchanged.

BTC and ETH each have a USD 500 target on each side: four maker orders with a
USD 2,000 aggregate admission budget. Quantities round down to venue lot sizes,
so actual notional can be below USD 500. Quotes use mid +/-5 bps; reaching 3 bps
cancels both sides of that instrument. A fill dispatches all maker cancellations
before reduce-only closing orders. Binance's 1-second 2-bps guard pauses the
affected market for 5 seconds. The default fixed REST pacing is removed;
venue 429 cooldown handling remains enabled.

## Operator commands

The following start command enables real production trading. It was not run
during deployment.

```sh
ssh vultr-jp 'systemctl start ondo-maker'
ssh vultr-jp 'journalctl -u ondo-maker -f'
ssh vultr-jp 'systemctl stop ondo-maker'
```

Normal stop requests cancellation and flattening. The service uses
`TimeoutStopSec=infinity` and `SendSIGKILL=no`; stop can remain pending until
the strategy has fresh proof of zero positions and zero outstanding orders.
Automatic restart and boot activation are not enabled.

For a read-only connection check:

```sh
cd /opt/ondo-maker/releases/20261028-500/app
runuser -u ondo-maker -- .venv/bin/python scripts/probe_ondo_maker.py --seconds 30
```

## Source and validation

- Application base: `8b05d708b13b87f8a6e0cff8e0a96911fd29036f`, plus local maker code.
- Native base: `0bf0866b5e2c3adf52eba2729aa45f4c77f70ac4` (merged PR 149), plus
  the local BTC/ETH-only USD 500/order and USD 2,000 gross capacity change.
  These deployment changes have not been committed or pushed.
- Wheel SHA-256: `0a403809f1caf26c9910672573a0a3ef24e920ff9b0598a30218882a1ba1117f`.
- Provenance SHA-256: `4ded26e44f194599fa1c8299c981f02a0934940d785813e8f67dac880300eb8c`.
- Native source fingerprint: `5a7cdf4b2924f8b33e317e3adc4238daf76b173cbe73bfa2983bc92eeae791cc`.
- Two targeted native boundary tests passed; 37 Linux application tests passed
  with no non-loopback network interface available to the tests. The installed
  native extension was exercised for disconnect cancellation and fill-on-cancel
  cleanup to confirmed flatness at the new allocation.
- Server installation verified the source fingerprint, wheel, binary, and stubs.
  The 30-second account-read-only check matched the account identity, acknowledged
  both private subscriptions, and received both markets from Ondo and Binance.
  This does not establish live order acceptance or cancellation ACK latency.
- Offline Windows Python guard P99: 5.5 us (prior measurement 5.6 us); two-market
  idle controller P99: 4.1 us. These are CPU measurements, not network latency.

Local evidence: `E:/persarb/reports/ondo-vultr-20261028-artifacts/`.
Application worktree: `E:/persarb/worktrees/ondo-maker-vultr-jp-20261028/`.
Native worktree: `E:/persarb/worktrees/ondo-maker-500-native-20261028/`.

# Backpack session planning

Run the configuration-only entry point from `E:\persarb\Nautilus-Perps`:

```powershell
.venv\Scripts\python.exe src/backpack_probe.py --config config/backpack-public.example.toml --dry-run
```

This validates a bounded session and prints JSON. It reads only the named TOML file, does not
import the native adapter, read `.env`, construct a client, open a socket or create output/state
files. Calling it without `--dry-run` is refused. Runtime integration remains tracked in issue #5.

The exact native symbols are an explicit nonempty allowlist. Each needs four decimal-string
margin/fee inputs and a source (`Configured`, `VenueObserved` or `Synthetic`) with a reference.
The sample deliberately supplies synthetic economics for planning. No source label proves
account verification or grants execution readiness. The native parser still validates market
eligibility, currency facts, tick/size grids and economic ranges when the runtime is introduced.

Modes are `public`, `account-readonly`, `paper` and `replay`. These name a requested future
runtime; a successful dry run only proves configuration validity. `paper` uses simulated
execution; `replay` requires `environment = "offline"` and an explicit `replay_file`.
No real-order mode, sandbox assumption, DMS or account mutation is exposed.

Only `account-readonly` accepts and requires an `[account]` section containing `account_id`
(with `BACKPACK-` prefix), `venue_account`, `credential_env`, and optional `subaccount`.
`credential_env` names a variable; never put its secret value in TOML. Account identity is
operator-supplied, not verified by this configuration. Dry-run output omits account identifiers
and the credential reference. It never resolves the reference or opens a dotenv file.

Production URLs are fixed. An explicit `environment = "loopback"` requires both
`base_url_http` and `base_url_ws` to be numeric loopback origins. Remote overrides,
URL credentials, query strings, non-origin paths and DNS aliases are refused.

Output and durable state are scoped by a SHA256 of venue, environment, endpoint pair and exact
account/subaccount identity. Mode separates reports, but does not reset durable order identity;
API-key rotation, symbol changes and new run IDs must not allocate a fresh order namespace.
Keep the configured state root stable across runs. Deleting or rolling it back is not recovery.
Relative paths resolve against the TOML directory; dry-run does not inspect or open replay data.

Durations are bounded to 600 seconds, each request to 60 seconds within that duration,
staleness to 30 seconds, report events to 100,000 and report bytes to 64 MiB. Future runtimes
must enforce these values and publish actual observations; this planning entry point does not
claim a session ran or that any live account state was clean.

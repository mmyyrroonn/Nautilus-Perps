# Lighter EXECUTION client on rc4 — feasibility for resting post-only maker orders on mainnet `PONS`

Read-only investigation. Nothing was modified, no branch created, no order placed, no credential read.
Only public unauthenticated `GET` calls were made (`/api/v1/orderBookDetails` on Lighter mainnet,
Robinhood mainnet, Lighter testnet) plus three documentation page fetches.

## VERDICT: **GO with caveats**

The Lighter execution client in the fork is a complete, first-class implementation — not a stub, not
data-only. `LighterExecutionClient` implements the full `ExecutionClient` trait
(`E:\nautilus_trader\crates\adapters\lighter\src\execution.rs:3950`), including `submit_order`,
`submit_order_list`, `modify_order`, `cancel_order`, `cancel_all_orders`, `batch_cancel_orders`,
`query_order`, `query_account`, and the four reconciliation report generators. **There are zero
`todo!` / `unimplemented!` in the entire crate** (verified: `grep -rn "todo!\|unimplemented!"` over
82 `.rs` files returns 0 hits). Post-only limit orders are explicitly supported and are the exact
shape the upstream `exec_tester.py` example exercises.

The blockers are **operational, not code**:

1. You must create a Lighter API key out-of-band. The adapter cannot do it (no `ChangePubKey` tx is
   implemented). Key registration requires an **L1 Ethereum signature** from the account owner.
2. `PONS` **does not exist on Lighter testnet** (176 testnet markets, no `PONS`). A testnet dress
   rehearsal has to use a different symbol (e.g. `DOGE-PERP.LIGHTER`, testnet market 3).
3. The repo's `.env.example` uses the **wrong variable name** — `LIGHTER_API_KEY_PRIVATE`. The
   adapter reads `LIGHTER_API_SECRET`. As written today the key would be silently ignored and
   `connect()` would bail.
4. `config/limits.toml` **does not exist** and no loader exists anywhere (confirmed).
5. Mainnet order flow needs the user's explicit 「上主网」 in-conversation per `CLAUDE.md`.

---

## 1. `LighterExecutionClientConfig`

Rust struct: `E:\nautilus_trader\crates\adapters\lighter\src\config.rs:239-287`.
Python stub: `E:\Nautilus-Perps\.venv\Lib\site-packages\nautilus_trader\adapters\lighter\__init__.pyi:83-131`.
Verified importable and instantiable in the repo venv (`nautilus_trader.__version__ == "2.0.0rc4"`).

| Field | Type | Default | Source |
|---|---|---|---|
| `environment` | `LighterEnvironment` | `MAINNET` | config.rs:244 |
| `deployment` | `LighterDeployment` | `LIGHTER` | config.rs:247 |
| `venue` | `Venue \| None` | `None` (→ deployment default) | config.rs:253 |
| `account_id` | `AccountId` | `LIGHTER-001` | config.rs:255 |
| `account_index` | `int \| None` | env fallback | config.rs:258 |
| `api_key_index` | `int \| None` (u8) | env fallback | config.rs:262 |
| `private_key` | `str \| None` | env fallback | config.rs:265 |
| `base_url_http` / `base_url_ws` / `proxy_url` | `str \| None` | `None` | config.rs:267-271 |
| `http_timeout_secs` | int | 60 | config.rs:273 |
| `ws_timeout_secs` | int | 30 | config.rs:276 |
| `market_order_slippage_bps` | int | 50 | config.rs:279 |
| `rest_quota_per_min` | `int \| None` | `None` → 60 req/min | config.rs:282 |
| `sendtx_quota_per_min` | `int \| None` | `None` → 60 req/min | config.rs:285 |
| `transport_backend` | `TransportBackend` | `SOCKUDO` (observed) | config.rs:287 |

**There is no `use_*` flag and no reconciliation flag on this config.** Reconciliation is driven
entirely by the engine: `LiveNode.builder(...).with_reconciliation(True)` +
`LiveExecutionEngineConfig(reconciliation_lookback_mins=..., reconciliation_instrument_ids=[...])`
(see `E:\nautilus_trader\examples\live\lighter\exec_tester.py:81-89`).

Live confirmation from the repo venv:

```
environment = LighterEnvironment.MAINNET      deployment = LighterDeployment.LIGHTER
venue = None                                   account_id = LIGHTER-001
account_index = None                           api_key_index = None
http_timeout_secs = 60                         ws_timeout_secs = 30
market_order_slippage_bps = 50                 rest_quota_per_min = None
sendtx_quota_per_min = 40 (as passed)          transport_backend = TransportBackend.SOCKUDO
LighterExecutionClientFactory().name() -> "LIGHTER"
```

### Credentials

Three values, all required together (`config.rs:337-347`, `execution.rs:342`):
**account index (u64)**, **api key index (u8, use 4..=254)**, **API private key = 40-byte hex**
(with or without `0x`; `credential.rs:323-334` enforces exactly `SCALAR_BYTES` = 40).

**No L1 wallet / Ethereum key appears in the config.** Signing is pure L2 Schnorr/ecgfp5 with the API
key. The L1 key is only needed *once, outside Nautilus*, to register the API key.

Config wins; a missing/blank field falls back to a **deployment+environment-scoped env var**
(`common/credential.rs:38-49`, resolver at `credential.rs:69-95`):

| Deployment | Environment | key index | secret | account index |
|---|---|---|---|---|
| Lighter | Mainnet | `LIGHTER_API_KEY_INDEX` | `LIGHTER_API_SECRET` | `LIGHTER_ACCOUNT_INDEX` |
| Lighter | Testnet | `LIGHTER_TESTNET_API_KEY_INDEX` | `LIGHTER_TESTNET_API_SECRET` | `LIGHTER_TESTNET_ACCOUNT_INDEX` |
| Robinhood | Mainnet | `LIGHTER_ROBINHOOD_API_KEY_INDEX` | `LIGHTER_ROBINHOOD_API_SECRET` | `LIGHTER_ROBINHOOD_ACCOUNT_INDEX` |
| Robinhood | Testnet | `LIGHTER_ROBINHOOD_TESTNET_API_KEY_INDEX` | `LIGHTER_ROBINHOOD_TESTNET_API_SECRET` | `LIGHTER_ROBINHOOD_TESTNET_ACCOUNT_INDEX` |

> **Repo mismatch (must fix before any probe).** `E:\Nautilus-Perps\.env.example` currently has
> `LIGHTER_API_KEY_PRIVATE=`, `LIGHTER_ACCOUNT_INDEX=`, `LIGHTER_API_KEY_INDEX=`, `LIGHTER_TESTNET=true`.
> - `LIGHTER_API_KEY_PRIVATE` is **not read by anything**. Rename to `LIGHTER_API_SECRET`.
> - `LIGHTER_TESTNET` is **not read by the adapter** (unlike `ASTER_TESTNET`, which only
>   `src/exec_probe.py:994` reads). Environment selection is the typed `LighterEnvironment` config
>   value. A Lighter probe must implement its own env guard the way `resolve_environment()` does.

### URLs / chain IDs (`src/common/deployment.rs:29-36`, `:63-74`; tests at `src/common/urls.rs:50-78`)

| Deployment/Env | HTTP | WS | chain_id | settlement |
|---|---|---|---|---|
| Lighter Mainnet | `https://mainnet.zklighter.elliot.ai` | `wss://mainnet.zklighter.elliot.ai/stream` | 304 | USDC |
| Lighter Testnet | `https://testnet.zklighter.elliot.ai` | `wss://testnet.zklighter.elliot.ai/stream` | 300 | USDC |
| Robinhood Mainnet | `https://api.rh.lighter.xyz` | `wss://api.rh.lighter.xyz/stream` | (RH const) | USDG |
| Robinhood Testnet | `https://api.rh-testnet.lighter.xyz` | `wss://api.rh-testnet.lighter.xyz/stream` | 300 | USDG |

Testnet is fully supported in rc4. Venues: `LIGHTER` and `LIGHTER_ROBINHOOD` (`deployment.rs:76-81`).
The data client force-appends `?readonly=true` to its WS URL (`config.rs:135-143`); the exec client
does not (`config.rs:359-365`) — they are separate sockets.

---

## 2. Order support

### Order types (`execution.rs:5526-5538`; doc table `docs/integrations/lighter.md:341-355`)
Supported: `MARKET`, `LIMIT`, `STOP_MARKET`, `STOP_LIMIT`, `MARKET_IF_TOUCHED`, `LIMIT_IF_TOUCHED`.
Rejected locally with a clear reason: `MARKET_TO_LIMIT`, trailing stops, TWAP
(`unsupported_lighter_order_type_reason`, `execution.rs:5513`).

### Time in force (`websocket/dispatch.rs:2048-2085`; `nautilus_to_lighter_tif` at `dispatch.rs:1978`)
`LighterTimeInForce { ImmediateOrCancel=0, GoodTillTime=1, PostOnly=2 }` (`common/enums.rs:657-668`).

- **`post_only=true` short-circuits the whole mapping and sends `PostOnly` regardless of TIF**
  (`dispatch.rs:1984-1986`). This is exactly what we want.
- Limit-style `GTC` / `DAY` / `GTD` → `GoodTillTime`; `IOC` → `ImmediateOrCancel`.
- **`FOK` is rejected** — Lighter has no FOK.
- `MARKET` is forced to `IOC`; `GTD`/`DAY` on a market order is denied.
- Resting orders always carry an expiry: no explicit GTD → `now_ms + 28 days`
  (`ORDER_EXPIRY_DEFAULT_GTC_MS`, `dispatch.rs:69`). Explicit GTD must be **5 min + 1 s .. 30 days**
  (`dispatch.rs:72,75`), enforced before signing. So a "resting" post-only order is really a
  28-day GTT order — fine for a probe, but it is not infinite.

### post_only / reduce_only
- `post_only`: supported on perps and spot (`docs/integrations/lighter.md:386`).
- `reduce_only`: supported on perps, passed straight into the signed tx —
  `execution.rs:2791` `reduce_only: plan.order.is_reduce_only()` → `OrderInfo.reduce_only`
  (`signing/tx/types.rs:121`) → wire field `ReduceOnly` (`signing/tx/encode.rs:310`).
- **Not supported and denied locally**: `quote_quantity`, `display_qty` (iceberg), grouped /
  contingency order lists, OCO/OTO/brackets (`execution.rs:5470-5498`).

### Operations
| Op | Impl | Notes |
|---|---|---|
| submit_order | `execution.rs:4207` | signs `CreateOrder` (tx type 14), sends over **WebSocket `sendTx`** |
| submit_order_list | `execution.rs:4252` | sequential fanout, capped at `LIGHTER_MAX_BATCH_TX = 15` (`common/consts.rs:91`); grouped/contingency denied |
| modify_order | `execution.rs:4358` | signed `ModifyOrder` (tx 17); qty / price / trigger |
| cancel_order | `execution.rs:4366` | signed `CancelOrder` (tx 15) |
| cancel_all_orders | `execution.rs:4374` | **iterates cached open orders per instrument**; deliberately does NOT use the venue's account-wide `CancelAllOrders`, to avoid hitting unrelated markets |
| batch_cancel_orders | `execution.rs:4396` | sequential fanout, capped at 15 |
| query_order | `execution.rs:4546` | REST lookup |
| query_account | `execution.rs:4524` | replays the cached WS account state (no REST account endpoint) |

### Order-ID mapping
- Nautilus `ClientOrderId` → venue `client_order_index` (i64) by a **fixed-seed hash masked to 31
  bits** (`dispatch.rs:derive_client_order_index_static`, `CLOID_INDEX_MAX = 0x7FFF_FFFF`,
  `dispatch.rs:314`). Values above 2^31-1 are rejected by the venue with `21727 invalid client order index`.
- Collisions are resolved by linear probing, bounded by `CLOID_INDEX_PROBE_LIMIT = 16` (`dispatch.rs:308`).
  A probed value is **not re-derivable after restart** — recovery then goes through the core cache's
  venue-order-ID mapping (documented at `docs/integrations/lighter.md:323-339`).
- Venue order IDs are bound to the identity via `bind_venue_order_id` (`dispatch.rs:156`), with a
  100k-entry retired-order replay cache (`REPLAY_CACHE_CAPACITY`, `dispatch.rs:317`).
- Nonces: per `(account_index, api_key_index)` lock-free CAS allocator with a 16-deep skip window
  (`signing/nonce.rs`, `DEFAULT_SKIP_WINDOW = 16`), baseline seeded/resynced from
  `GET /api/v1/nextNonce`; hard refresh on venue `invalid nonce` and on every reconnect
  (`execution.rs:1150-1330`).

### Fills / status
Both arrive on the private WS account streams and are dispatched in the exec consumption loop
(`execution.rs:1012-1048`): `ExecutionReport::Order` → `dispatch_lighter_order`,
`ExecutionReport::Fill` → `dispatch_lighter_trade`. Positions arrive as
`PositionSnapshot` / `PositionUpdate` (`execution.rs:1053-1130`); account state as
`AccountState` (`execution.rs:1131-1146`). REST is used only for reconciliation and query paths.

### Precision / min size — **PONS, fetched live today**

`GET https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails` (public, no auth):

```
symbol = PONS          market_id = 231        market_type = perp   status = active
price_decimals = 5     size_decimals = 1      supported_price_decimals = 5  supported_size_decimals = 1
min_base_amount = 20.0 min_quote_amount = 10.000000
maker_fee = 0.0000     taker_fee = 0.0000
mark_price = 0.73386   index_price = 0.73160  order_quote_limit = 10000000.000000
```

**Size decimals = 1** (0.1 PONS tick). Nautilus instrument id is **`PONS-PERP.LIGHTER`**
(`common/symbol.rs:33,44-58`).

Robinhood mainnet also lists PONS (`market_id = 44`, same 5/1 decimals, same minimums, USDG quote) →
`PONS-PERP.LIGHTER_ROBINHOOD`. **PONS is NOT on Lighter testnet.**

Conversion is integer-exact via `rust_decimal`, not float:
`price_to_ticks` / `quantity_to_ticks` (`dispatch.rs:2103+`), `OrderInfo.price: u32`,
`OrderInfo.base_amount: i64` (`signing/tx/types.rs:105-125`). At 5 decimals the u32 price ceiling is
~42 949.67, far above PONS.

Local pre-flight validation before any nonce is burned (`execution.rs:5632-5658`, `validate_order_amount`):
- `quantity >= min_quantity` (20.0 PONS)
- `quantity * price >= min_notional` (10 USDC)
- quantity that truncates to 0 ticks → denied (`execution.rs:1602-1608`)

**Binding minimum for PONS = 20 PONS ≈ 14.68 USD** at 0.734 (min_base binds, not min_quote).
That fits comfortably under the 50 USD/order stage-3 cap in `PROMPT.md:64`.

### Unimplemented paths
`grep -rn "todo!\|unimplemented!"` over the crate → **0 hits**. Every "not supported" is an explicit,
tested local denial with a message, not a panic. Documented gaps (`docs/integrations/lighter.md:80-96`):
grouped orders / OCO / OTO / brackets / TWAP / trailing / iceberg; `CreateGroupedOrders` unused;
native account-wide cancel-all unused; spot conditionals unsupported.

---

## 3. Account / position / reconciliation

- **Credentials are mandatory to connect.** `connect()` bails *before any network work* if
  `has_credentials()` is false (`execution.rs:4040-4048`), with a message naming the three fields.
- `connect()` (`execution.rs:4034-4183`) does, in order: start task generation → bootstrap
  instruments from `GET /api/v1/orderBookDetails` (**the exec client is self-sufficient; it does not
  need the data client for instruments** — `ensure_instruments_initialized_async`) → `refresh_nonce()`
  → `fetch_account_detail()` + tier detection → referral/integrator attribution → spawn WS consumer →
  **`await_account_streams_ready(30.0)`**.
- The readiness gate requires the **first frame of all five** private streams:
  `account_all_orders`, `account_all_trades`, `account_all_positions`, `account_all_assets`,
  `user_stats` (`execution.rs:1329-1350`, `:4170`). If any is missing within 30 s, connect tears down
  and fails. Position readiness needs the `subscribed/` snapshot, not an incremental update.
- Balances: `account_all_assets` + `user_stats` are merged into **one** `AccountState`
  (`websocket/account_state.rs:17-45`), `AccountType::Margin`, `base_currency=None`, emitted only
  after both streams have delivered a frame.
- Positions: perp netting, one position per market. Snapshot semantics are strict (omitted markets and
  zero rows flatten; unmappable rows are retained so they cannot fake a flat).
- Full reconciliation surface: `generate_order_status_report(s)`, `generate_fill_reports`,
  `generate_position_status_reports`, `generate_mass_status`
  (`execution.rs:4594 / 4629 / 4863 / 4872 / 4885`).
- **Account index is NOT auto-discovered.** You supply it. Discovery is a manual one-time
  `GET /api/v1/accountsByL1Address?l1_address=0x...` → read `index` from `sub_accounts`
  (`docs/integrations/lighter.md:165-180`). The adapter never calls that endpoint
  (it is absent from the endpoint list in `src/http/client.rs:78-93`).

### Integrator approval — **not a blocker for a Standard account**
On Lighter mainnet the client can submit a zero-fee `ApproveIntegrator` at startup and will hard-fail
on venue code `21149` (`execution.rs:4127-4145`). **This only runs for Plus/Premium tiers**:
`integrator_account_index()` returns `None` for Standard (`execution.rs:685-694`), so
`submit_integrator_auto_approval()` returns immediately (`execution.rs:750-752`). A standard
retail account never hits this path. Robinhood mainnet instead applies a `NAUTILUS` referral code
to the L1 address at startup (failure only warns).

---

## 4. Tests

Crate: `E:\nautilus_trader\crates\adapters\lighter` — 82 `.rs` files, **67 868 lines**.

- **1 105** `#[test]` / `#[rstest]` / `#[tokio::test]` attributes plus **603** `#[case(...)]` rows
  across `src/` + `tests/`.
- Execution-specific: `src/execution.rs` 147 test attrs; `tests/exec_client.rs` **6 287 lines / 84
  test attrs**; `src/websocket/dispatch.rs` 86; `src/websocket/handler.rs` 146.
- **Mock server: yes.** `tests/exec_client.rs` spins an in-process **axum** server serving both the
  REST endpoints and a real `/stream` WebSocket upgrade (`tests/exec_client.rs:488-492, 548, 698-713,
  811`), with the client pointed at it via `base_url_http` / `base_url_ws`
  (`tests/exec_client.rs:830-836`). Same pattern in `tests/data_client.rs` and `tests/http.rs`.
- **Recorded fixtures: yes.** 39 JSON files in `test_data/` including
  `ws_account_orders_update.json`, `ws_account_all_trades_update.json`,
  `ws_account_all_positions_update.json`, `http_next_nonce.json`, plus signing oracles
  (`signing_tx_oracle.json`, `signing_schnorr_vectors.json`, Poseidon2/field/curve vectors).
- Signing is additionally covered by 7 fuzz targets and a differential fuzz harness against Pornin's
  reference ecgfp5 implementation, plus criterion/iai benches.
- Testnet config is supported in rc4 and unit-tested (`src/common/urls.rs:50-78`).

---

## 5. Upstream status

- The fork's lighter crate and `docs/integrations/lighter.md` are **byte-identical to tag
  `v2.0.0rc4`** (`git diff --stat v2.0.0rc4 HEAD -- crates/adapters/lighter docs/integrations/lighter.md`
  → empty output). The `aster` branch never touched it. So this is stock upstream rc4.
- `E:\nautilus_trader\RELEASES.md` shows a long, active maintenance history — nonce races, batch
  ordering, GTD expiry, reconnect auth rotation, position snapshot semantics, reduce-only
  reconciliation, Robinhood Chain deployment, account-tier quotas. Line 1436: "Added Lighter initial
  adapter (DEX: spot, perps)" is many releases back.
- WebFetch of `https://nautilustrader.io/docs/latest/integrations/lighter` confirms: **not labelled
  beta, not data-only**; execution (submit/modify/cancel, spot + perps) is documented as supported.
  The Limitations list matches the local copy verbatim.

---

## 6. Lighter-side prerequisites

**API key creation (one-time, outside Nautilus).**
`https://apidocs.lighter.xyz/docs/api-keys` (fetched): generating the keypair needs no L1 key, but
**associating it with the account requires the owner's L1 Ethereum signature** (via the web UI, the
`lighter-python` SDK, or the contract directly). Indexes `0-3` are reserved for Lighter's own
desktop/mobile clients; use `4..254`; `255` is the `apikeys` query sentinel. The adapter enforces
`0..=254` (`credential.rs:310-315`) and the docs say use 4+.

**The adapter cannot create or rotate keys.** `ChangePubKey` (tx type 8) exists in the enum
(`common/enums.rs`) but no `ChangePubKeyTxInfo` struct is implemented — the only signed tx bodies are
`CreateOrder`, `CancelOrder`, `ModifyOrder`, `CancelAllOrders`, `UpdateLeverage`, `ApproveIntegrator`
(`signing/tx/types.rs:199-410`, `signing/tx/encode.rs:156-247`).

**Signer: bundled in-tree — no external native lib needed.**
`src/signing/mod.rs:16-52`: an original Rust implementation of Schnorr over ecgfp5 (Goldilocks quintic
extension) with Poseidon2 binding, written from the specs, cross-checked against
`elliottech/poseidon_crypto` vectors and differentially fuzzed against `pornin/ecgfp5`.
`UpdateLeverage`, `CancelAllOrders`, attributed modifies, and conditional creates are **byte-pinned
against the signer shipped with `lighter-python` 1.1.2** (`docs/integrations/lighter.md:474-476`).
So: **you do NOT need `lighter-python` or its closed-source compiled signer at runtime.**

**Nonce.** Handled internally (`signing/nonce.rs`, 889 lines): baseline from `GET /api/v1/nextNonce`,
optimistic allocation inside a 16-deep skip window matching the sequencer's out-of-order tolerance,
ack/rollback on venue confirm/reject, hard resync on `invalid nonce` and on reconnect. Nothing to do
in the strategy.

**Rate limits that matter for a maker probe** (`docs/integrations/lighter.md:605-706`):
- REST default 60 req/min; `sendTx` default 60 req/min — but the venue enforces **40 req/min per L1
  address** on tx traffic. The docs record a real mainnet quoting session hitting `code=23000
  Too Many Requests` after ~40 modifies in a minute. **Set `sendtx_quota_per_min=40` or lower.**
- Pending orders 500/account, 16/market. Active orders 1 500/account, 1 000/market.
- **Volume quota**: `L2CreateOrder`, `L2ModifyOrder`, `L2CancelAllOrders`, `L2CreateGroupedOrders`
  spend it; plain cancels do not. Per Lighter's page: new accounts start at 1K, ~1 extra tx per
  2 USD of volume, plus one free `SendTx` every 15 s. Repeated no-fill requoting can drain it.
  For a probe that places one order and cancels it, this is irrelevant; for a live quoter it is the
  real constraint. (Lighter's page also says the program is "only available to Premium accounts now" —
  worth re-checking against the account actually used.)

**Fees.** PONS maker 0.0 bps / taker 0.0 bps on both Lighter mainnet and Robinhood mainnet
(read from the live `orderBookDetails` payload above; also matches
`E:\Nautilus-Perps\reports\lighter-screen-2026-09-08.md`, where PONS ranks 3rd on LIGHTER
(+23.50 bps room vs HL) and 2nd on LIGHTER_RH (+27.23 bps)).

---

## 7. What to copy from `src/exec_probe.py`

`E:\Nautilus-Perps\src\exec_probe.py` (1 216 lines). Structure worth reusing verbatim:

**Hard caps — all module constants, deliberately not CLI/env knobs:**
- `exec_probe.py:87` `MIN_NOTIONAL_FALLBACK_USDT = Decimal("5")` — used only when the instrument
  carries no `min_notional`.
- `exec_probe.py:94` **`MAX_NOTIONAL_USDT = Decimal("20")`** — the hard per-order budget. Comment at
  :89-92 states the intent: "a module constant rather than a CLI/env knob so no configuration can
  widen it. When the venue minimum order is larger than this, the probe refuses instead of sizing up."
- `exec_probe.py:97` `RESTING_PRICE_FACTOR = Decimal("0.5")` — resting order placed at half the best
  bid so it can never cross.
- `exec_probe.py:92-93` `CONNECT_ATTEMPTS = 3`, `CONNECT_RETRY_SECS = 15`.
- `exec_probe.py:100` `DEFAULT_TIMEOUT_SECS = 180` watchdog; `:104` `CLEANUP_GRACE_SECS = 10.0`.
- `exec_probe.py:120-123` exit codes `EXIT_OK / PROBE_FAILED / REFUSED / TIMEOUT`.
- `exec_probe.py:347` `self.max_notional = min(Decimal(max_notional), MAX_NOTIONAL_USDT)` — config can
  only *lower* the cap, never raise it.
- `exec_probe.py:158,206-209` `plan_order(...)` raises `ProbeSizingError` when no size satisfies both
  the venue minimum and the budget.

**Second line of defence — the Nautilus risk engine, `exec_probe.py:1085-1090`:**
```python
risk_config = LiveRiskEngineConfig(
    bypass=False,
    max_notional_per_order={symbol: str(MAX_NOTIONAL_USDT) for symbol in load_ids},
)
```
(Note the upstream `examples/live/lighter/exec_tester.py:90` uses `bypass=True` — do **not** copy that.)

**`.env` handling:** `exec_probe.py:55-59` optional `dotenv.load_dotenv()`, then plain
`os.environ.get(...)` at `:1080-1082`, with a `REFUSED` exit if the signing key is absent (`:1106-1111`).
`spread_watch.py:1076-1078` does the same.

**Environment guard:** `resolve_environment()` at `exec_probe.py:986-997` returns `None` for anything
that is not testnet and `main()` exits `EXIT_REFUSED` with an explicit message (`:1060-1068`). For
Lighter this guard must be written from scratch — the adapter does not read `LIGHTER_TESTNET`.

**Cancel-on-stop / leftover accounting:** `on_stop` → `_run_cleanup("stop")` (`:509-520, 688-726`),
`_recheck_cleanup()` re-evaluates on every order event (`:732-739, 777, 786, 806`), and a run that
leaves an unconfirmed live order is *not* a clean run — `main()` downgrades `EXIT_OK` to
`EXIT_PROBE_FAILED` when `strategy.leftovers` is non-empty (`:1198-1207`). Watchdog:
`start_stop_watchdog(...)` at `:258`, wired at `:1139-1145`.

**Node wiring pattern:** `exec_probe.py:1118-1136` — `LiveNode.builder(...).with_logging(...)
.with_risk_engine_config(...).with_timeout_connection(...).add_data_client(...).add_exec_client(...).build()`,
whole-node retry loop that only retries while `strategy.orders_sent == 0`.

**Ready-made alternative:** `nautilus_trader.testkit.ExecTesterConfig` is importable from the repo venv
(verified) and already supports `use_post_only`, `tob_offset_ticks`, `cancel_orders_on_stop`,
`close_positions_on_stop`, `reduce_only_on_stop`, `dry_run`, `external_order_claims`. Upstream's
`examples/live/lighter/exec_tester.py` is a 138-line working post-only quoting probe. Useful as a
first connectivity smoke test, but it has no notional cap of its own — the repo's hard-cap pattern
must be layered on before mainnet.

**`config/limits.toml`: CONFIRMED ABSENT.** `find . -name limits.toml` returns nothing; there is no
`config/` directory in `E:\Nautilus-Perps` at all. The only references are aspirational —
`CLAUDE.md:9`, `PROMPT.md:64`, `PROMPT.md:74`, and `reports/aster-review-2026-09-05-response.md:48,90`
("主网执行留给阶段 3 带 `config/limits.toml` 的独立脚本"). **No loader exists anywhere in the repo.**

---

## Blockers, ranked

1. **API key does not exist yet.** Needs a one-time L1 signature from the account owner at
   `https://app.lighter.xyz/apikeys` (index 4-254), then the 40-byte hex secret saved once — Lighter
   never shows it again. Nautilus cannot do this step.
2. **`.env.example` variable name is wrong** — `LIGHTER_API_KEY_PRIVATE` must become
   `LIGHTER_API_SECRET`, or the exec client will refuse to connect with "requires credentials".
3. **No PONS on testnet** — the exact instrument cannot be rehearsed. Rehearse the *mechanics* on
   `DOGE-PERP.LIGHTER` testnet (market 3, price_decimals 6, size_decimals 0, min_base 10 but
   min_quote 10 USD binds → ≥ ~112 DOGE; upstream's exec_tester uses 200).
4. **`config/limits.toml` + loader do not exist.** Stage 3 needs both, with hard-coded fallbacks that
   config can only lower (copy `exec_probe.py:347`).
5. **Mainnet authorization** — `CLAUDE.md` requires the user to say 「上主网」 in the current turn.
6. Minor: pick the right deployment. PONS exists on **both** `LIGHTER` (USDC, market 231) and
   `LIGHTER_ROBINHOOD` (USDG, market 44). They are different exchanges with different accounts,
   different credential namespaces, different venues, and different attribution. Decide before wiring.

Not blockers (verified): no external signer lib needed; no L1 key at runtime; integrator approval is
skipped for Standard accounts; testnet fully supported in rc4; exec client bootstraps its own
instruments.

---

## Minimal probe plan (no code)

**Phase A — offline, no keys.**
1. Fix `.env.example`: `LIGHTER_API_KEY_PRIVATE` → `LIGHTER_API_SECRET`; drop/annotate
   `LIGHTER_TESTNET` and note that the Lighter environment is selected in code, not by env var.
2. Dry-run wiring check: build the `LighterDataClientConfig` + `LighterExecutionClientConfig`, print
   them, and exit before `node.run()` — same `--dry-run` flag as `exec_probe.py:1102-1104`.

**Phase B — Lighter TESTNET rehearsal (`DOGE-PERP.LIGHTER`, `LighterEnvironment.TESTNET`).**
3. User creates a testnet API key (index ≥ 4) and funds the testnet account; sets
   `LIGHTER_TESTNET_ACCOUNT_INDEX` / `LIGHTER_TESTNET_API_KEY_INDEX` / `LIGHTER_TESTNET_API_SECRET`.
4. Connect only. Assert: all five account streams ready inside 30 s; one merged `AccountState` with a
   non-zero balance; `nextNonce` baseline logged. Stop. **This alone answers "is exec usable".**
5. One **post-only GTC LIMIT** far from touch (mirror `RESTING_PRICE_FACTOR`), then cancel it.
   Assert the event chain `OrderSubmitted → OrderAccepted → OrderPendingCancel → OrderCanceled`,
   and that `post_only=True` really produced Lighter TIF `PostOnly` (a crossing post-only would come
   back `CanceledPostOnly`, `common/enums.rs:479,503`).
6. Deliberate negative test: submit below `min_base_amount` → expect a **local** `OrderDenied` before
   any nonce is consumed (`validate_order_amount`, `execution.rs:5632`).
7. Restart-recovery test: place a resting post-only order, kill the node, restart with
   `with_reconciliation(True)` + `reconciliation_instrument_ids=[DOGE-PERP.LIGHTER]`, confirm the
   order is recovered (this exercises the cloid-collision-probe caveat).

**Phase C — Lighter MAINNET, `PONS-PERP.LIGHTER`, only after the user says 「上主网」.**
8. Write `config/limits.toml` first (per-order ≤ 50 USD, total exposure ≤ 100 USD, ≤ 20 triggers/day)
   plus a loader whose values can only *lower* module constants.
9. Mainnet API key (separate from testnet), `LIGHTER_*` namespace. Verify with the public
   `GET /api/v1/apikeys?account_index=..&api_key_index=..` check before running anything.
10. Read-only mainnet connect first: log the account tier (`GET /api/v1/account`, `detect_account_tier`,
    `execution.rs:1697`), balances, and confirm no `ApproveIntegrator` tx is attempted (expected for
    Standard).
11. Set `sendtx_quota_per_min=40`. One post-only 20-PONS order (~14.7 USD, min size, ≈ 30 % of the
    50 USD cap) resting well outside the touch; hold; cancel; confirm on the Lighter UI.
12. Only then: a post-only order near touch with the HL/Aster taker hedge armed, cancel-on-stop and
    the leftover-detection accounting from `exec_probe.py:1198-1207` wired, watchdog on.

**Stop and report if:** account streams do not all arrive in 30 s; `21149` at startup (means a
Plus/Premium account needs integrator approval from a non-maker-only key); `23000 Too Many Requests`
(lower `sendtx_quota_per_min`); repeated `invalid nonce`; or any order rests with unconfirmed cancel
at shutdown.

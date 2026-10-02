# Backpack recorded replay and paper

`src/backpack_probe.py` supports `mode = "replay"` and `mode = "paper"` with
`environment = "offline"`. Both require an explicit recorded-session manifest
and economics covering exactly the symbol allowlist. The Rust adapter owns
protocol validation, instrument precision, sequence handling and native data.
The Python runner passes each original JSONL line directly to
`BackpackPublicReplay.apply_record(bytes)`.

Run the checked-in synthetic examples from the app repository:

```powershell
.venv/Scripts/python.exe src/backpack_probe.py --config config/backpack-replay.example.toml --dry-run
.venv/Scripts/python.exe src/backpack_probe.py --config config/backpack-replay.example.toml
.venv/Scripts/python.exe src/backpack_probe.py --config config/backpack-paper.example.toml
```

Dry-run reads only the named TOML configuration. It does not read the recording,
import native clients, create output/state directories or read credentials.
Runtime imports the installed native Backpack API and creates no venue client.
For a source-bound candidate, supply all three verification options:

```powershell
.venv/Scripts/python.exe src/backpack_probe.py --config config/backpack-paper.example.toml `
  --candidate-wheel E:/persarb/.backpack-artifacts/replay-e738cd58d3/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl `
  --candidate-sha256 7ddb1ccc4bbe780f71520c7bc134b2e1b593162c0679f1f87cbb5280ee82f9ce `
  --native-provenance E:/persarb/.backpack-artifacts/replay-e738cd58d3/native-provenance.json
```

Candidate verification checks the installed binary and Backpack stub against
the explicit wheel/provenance. Without these options, the report records an
unverified installed native identity. Configuration, source files and consumed
input hashes bind each report. The command returns nonzero for failed or
incomplete sessions; account and real order readiness remain open.

## Recorded-session contract

The manifest is UTF-8 JSON, schema version 1, limited to 1 MiB. Its fields are
exactly `schema_version`, `venue` (`BACKPACK`), `source` (`Synthetic` or
`PublicCapture`), `source_reference` and `streams`. Duplicate fields are refused.
Each `streams` key is an exact configured symbol, with these required fields:

- `market_json`: the original market JSON text as a JSON string. The runner does
  not decode and re-encode its protocol fields.
- `metadata_received_at_ns`: unsigned original receipt time in nanoseconds.
- `generation`: unsigned initial native replay generation.
- `records_file`: a relative `.jsonl` path contained in the manifest directory.
  Files cannot alias another stream or use `.env` path components.

Each JSONL record is bounded to 1 MiB and uses the native schema:

```json
{"kind":"frame","generation":1,"received_at_ns":1700000000001000000,"payload":{"stream":"bookTicker.BTC_USDC_PERP","data":{"e":"bookTicker","s":"BTC_USDC_PERP","E":1700000000001000,"T":1700000000000999,"u":1,"b":"100.0","B":"1.00000","a":"101.0","A":"2.00000"}}}
```

`kind` is `frame`, `snapshot` or `restart`. A frame contains the original public
WS envelope; a snapshot contains the original depth REST body. A restart has a
strictly greater generation, a receipt time and no payload. Native old-generation
and duplicate results emit no data. Malformed metadata/records, sequence gaps,
invalid precision and crossed books fail the session through the native parser.
The application does not invent a reason for each native `None` result.

Total bounds default to 10,000 records and 64 MiB including the manifest. The
maximums are 100,000 records and 64 MiB. Source provenance and explicit economic
provenance are recorded separately. Each file reports its consumed SHA256 and
whether EOF was reached; partial failure does not claim a complete file hash. Paths resolving through a symlink to dotenv files are refused.

Native outputs must have nondecreasing receipt times within each symbol file.
All streams are merged deterministically by original `ts_init`, symbol and
original file ordinal. Instruments precede data in the report. Data retains
original engine event and receipt timestamps; exact price/quantity and native
Money decimals are strings. Historical timestamps never establish live
freshness or execution readiness. Mark prices do not claim a funding-rate unit.

## Paper semantics and limits

Paper uses the actual native `BacktestEngine`, native instruments and a fixed
synthetic two-market-order roundtrip per symbol. The first quote triggers a BUY;
a later quote after an actual entry fill triggers a reduce-only SELL. Quantities
must exactly fit native instrument precision and limits. Starting USDC and
quantities are explicit decimal strings in `[paper]` and `[paper.quantities]`.

Only native `QuoteTick` data feeds this L1 simulation, with native liquidity
consumption enabled. Native default market-order handling can simulate further
fills with slippage when an order exceeds top-level size; this is an engine
assumption, not observed venue depth. Trades, marks and depth
batches remain replay evidence and do not feed the paper matching engine. All
quotes at a single receipt timestamp run together; more than 64 per timestamp
is refused. Cash, orders, fills, commissions and positions come from actual
native cache/domain objects. No application account ledger computes them.
The checked-in scenario produces actual fills at 101.0 and 102.0, commissions
0.00050500 and 0.00051000 USDC, final cash 10000.00898500 USDC and a closed
position. These are synthetic engine outcomes, not venue account observations.

A session completes only with both orders filled per symbol, no open orders or
positions, and no callback failure. Insufficient quotes, rejected orders and
residual positions produce an incomplete failure with the actual available
state. Shutdown never manufactures an exit fill. Cancellation after an entry
fill retains its real open position in the final report.

`duration_secs` starts after native candidate verification and manifest loading;
`preparation_ms` records that earlier work. The deadline is checked between each
bounded input line/output and each native timestamp batch. Native engine calls
are synchronous on their owning thread; a batch is bounded to 64 quotes.
Cancellation takes effect at these checkpoints. Engine finalization and evidence
publication run after the runtime budget to preserve truthful final state.
Reports remain bounded by configured event count and bytes. A minimal truncated
summary explicitly records truncation when the full summary cannot fit.

## Verification

Use the installed candidate interpreter; these tests execute real native replay
and real BacktestEngine, including precision, old generation, duplicates,
restart, depth faults, deterministic multiple-symbol ordering, budget failure,
cancellation, residual state and exact economic outcomes:

```powershell
.venv/Scripts/python.exe -m pytest tests/test_backpack_replay.py tests/test_backpack_config.py tests/test_backpack_public.py -q
```

No offline acceptance establishes private-account access, production fees,
full depth coverage, production order readiness or live execution safety.

# Stage 0 — Unknowns resolved (2026-09-04)

All network calls below are unauthenticated public reads, executed from `E:\Nautilus-Perps` on
2026-09-04. Raw responses are saved next to this file.

---

## Q1 — Does the Nautilus Hyperliquid adapter support HIP-3 builder dexes (`xyz`) for BOTH market data AND order submission?

### Answer

**Yes, both.** HIP-3 dexes are a first-class product in the adapter: instruments are auto-loaded at
connect with symbol form `xyz:NVDA-USD-PERP.HYPERLIQUID`, subscriptions send the dex-qualified coin
`xyz:NVDA` on the wire, and order submission resolves the correct HIP-3 asset index
(`100_000 + dex_index * 10_000 + universe_index`). There is **no** `dex` config option — and none is
needed.

### Evidence

#### (a) Config options that select/enable builder dexes

There is no per-dex config field. From `docs/integrations/hyperliquid.md` (both `develop` = 2.0.0rc5
and tag `v1.231.0`), section *HIP-3 builder-deployed perpetuals*:

> In a `LiveNode`, HIP-3 perpetuals load automatically alongside standard perpetuals at
> connect: the adapter fetches every perp dex (standard and builder-deployed) from `allPerpMetas`,
> so no additional client configuration is required. The data client exposes no per-dex filter;
> strategies select the markets they trade by `instrument_id`.

Verified against the live published docs at
<https://nautilustrader.io/docs/latest/integrations/hyperliquid/> — same wording, same product table.

Product support table (same doc):

> | Product Type      | Data Feed | Trading | Notes                                            |
> | ----------------- | --------- | ------- | ------------------------------------------------ |
> | Perpetual Futures | ✓         | ✓       | USDC-settled linear perps (validator-operated).  |
> | HIP-3 Perpetuals  | ✓         | ✓       | Builder-deployed perps with per-dex collateral.  |
>
> All four product types load automatically at connect; no per-product opt-in is required.

The only opt-in flag exists on the low-level HTTP client, not on the live clients:

```python
instruments = await client.load_instrument_definitions(
    include_spot=True,
    include_perps=True,
    include_perps_hip3=True,   # <-- HIP-3 opt-in for direct HyperliquidHttpClient use only
    include_outcomes=False,
)
```

I confirmed the client config structs carry **no** dex field.
`crates/adapters/hyperliquid/src/config.rs` (branch `develop`), `HyperliquidDataClientConfig`
(line 48) and `HyperliquidExecutionClientConfig` (line 166) expose only:
`private_key`, `base_url_ws`, `base_url_http`, `base_url_exchange`, `proxy_url`,
`environment: HyperliquidEnvironment` (mainnet/testnet), timeouts, retry/stale-stream knobs,
`vault_address`, `account_address`, `update_instruments_interval_mins`, `transport_backend`.

Note on the 1.x line: `v1.231.0/docs/integrations/hyperliquid.md:314` still documents an instrument
provider filter `filters={"market_types": ["perp_hip3"]}` (and line 571:
`` | `market_types` (or `kinds`) | `list[str]` | `"perp"`, `"perp_hip3"`, or `"spot"`. | ``). That
filter was removed in 2.x in favour of always loading everything.

#### (b) How `xyz:NVDA` is rendered inside Nautilus

`docs/integrations/hyperliquid.md`, section *Symbology → HIP-3 perpetuals*:

> Format: `{dex}:{Asset}-USD-PERP`
>
> Examples:
> - `xyz:TSLA-USD-PERP` - Tesla perp on trade.xyz
> - `xyz:GOLD-USD-PERP` - Gold perp on trade.xyz
> - `flx:NVDA-USD-PERP` - Nvidia perp on Felix
> - `vntl:SPACEX-USD-PERP` - SpaceX perp on Ventuals
>
> ```python
> InstrumentId.from_str("xyz:TSLA-USD-PERP.HYPERLIQUID")
> ```

Source: `crates/adapters/hyperliquid/src/http/parse.rs:192-232`
(`parse_perp_instruments_with_settlement`):

```rust
let symbol = format!("{}-USD-PERP", sanitize_symbol(&asset.name));
let raw_symbol: Ustr = asset.name.as_str().into();
...
    asset_index: asset_index_base + index as u32,
    is_hip3: asset_index_base > 0,
```

So the Nautilus `symbol` is `xyz:NVDA-USD-PERP` while `raw_symbol` stays the venue-official
`xyz:NVDA` for wire calls. There is an explicit unit test at
`crates/adapters/hyperliquid/src/http/parse.rs:1805-1845`:

```rust
fn test_parse_perp_instruments_hip3_dex() {
    // HIP-3 dex at index 1: asset_index_base = 100_000 + 1 * 10_000 = 110_000
    ... name: "xyz:TSLA" ... name: "xyz:NVDA" ...
    let defs = parse_perp_instruments(&meta, 110_000).unwrap();
    assert_eq!(defs[0].symbol, "xyz:TSLA-USD-PERP");
    assert_eq!(defs[0].base, "xyz:TSLA");
    assert_eq!(defs[0].asset_index, 110_000);
    assert_eq!(defs[1].symbol, "xyz:NVDA-USD-PERP");
    assert_eq!(defs[1].asset_index, 110_001);
}
```

Caveat (does not affect us): HIP-3 asset names containing `*` or `?` are sanitized to `x` in the
Nautilus symbol (`sanitize_symbol`, `parse.rs:156`), because those bytes collide with message-bus
pattern syntax. `xyz:NVDA` / `xyz:TSLA` / `xyz:MSFT` contain neither, so they pass through unchanged.

#### (c) Order signing / `asset` index handling for builder-dex assets — **confirmed**

`crates/adapters/hyperliquid/src/http/client.rs:3690-3698`:

```rust
/// Returns the asset index base for a perp dex.
///
/// Standard perps (dex 0) start at 0. HIP-3 dexes start at
/// 100_000 + dex_index * 10_000.
fn perp_dex_asset_index_base(dex_index: usize) -> u32 {
    if dex_index == 0 {
        0
    } else {
        100_000 + dex_index as u32 * 10_000
    }
}
```

`crates/adapters/hyperliquid/src/http/client.rs:1649-1656`:

```rust
/// Get asset index for a symbol from the cached map.
///
/// For perps: index in meta.universe (0, 1, 2, ...).
/// For spot: 10_000 + index in spotMeta.universe.
/// For HIP-3: 100_000 + dex_index * 10_000 + index in dex meta.universe.
pub fn get_asset_index(&self, symbol: &str) -> Option<u32> { ... }
```

The index map is built by iterating `allPerpMetas` with the dex index
(`http/client.rs:1449-1477`):

```rust
match self.inner.load_all_perp_metas().await {
    Ok(all_metas) => {
        for (dex_index, meta) in all_metas.iter().enumerate() {
            let base = perp_dex_asset_index_base(dex_index);
            ...
            let perp_defs = parse_perp_instruments_with_settlement(meta, base, settlement_currency.as_str());
```

Every order-path call site resolves the wire `asset` field through that same map — submit, cancel,
modify and order-status all share the pattern at `http/client.rs:1875-1881`, `1973-1979`,
`3076-3082`, `3313-3318`:

```rust
let asset_id = self.get_asset_index_for_symbol(symbol).ok_or_else(|| {
    Error::bad_request(format!(
        "Asset index not found for symbol: {symbol}. Ensure instruments are loaded."
    ))
})?;
```

This matches the Hyperliquid spec exactly. Cross-check against the live venue: mainnet `perpDexs`
puts `xyz` at index **1**, and `xyz:NVDA` is at universe index **2**, so the wire asset id is
`100000 + 1*10000 + 2 = 110002`.

**Operational caveat from the docs** (`Instrument loading` section):

> The execution client bootstraps its own asset-index map once on first connect and never refreshes
> it, so trading a market listed after that bootstrap requires a process restart. Submitting for a
> symbol the execution client never loaded is denied with `Asset index not found`.

Two more HIP-3 behavioural notes the docs call out, relevant to a spread strategy:

> - **Higher fees**: each dex sets its own deployer fee scale, which multiplies the base perp fee
>   by `scale + 1` below `1` and by `scale * 2` at or above it, with the deployer taking up to half.
>   Check a dex's live rate rather than assuming the standard perp schedule.
> - **Isolated margin**: HIP-3 markets default to isolated-only margin.
> - **Per-dex collateral**: Each HIP-3 dex declares its settlement token through its
>   `collateralToken` entry in `allPerpMetas`. Nautilus resolves that token through `spotMeta` and
>   keeps the symbol's quote leg as `USD`. If a non-USDC collateral token cannot resolve from
>   `spotMeta`, instrument loading returns an error rather than falling back to USDC.

#### (d) Do L2 book / trades subscriptions accept dex-qualified coins? — **yes**

`crates/adapters/hyperliquid/src/websocket/client.rs` derives the wire coin from `raw_symbol` for
every subscription: `subscribe_book_with_options` (line 1259), `subscribe_book_depth10_with_options`
(line 1296), `subscribe_trades` (line 1452), plus quotes/bars/mark-price/funding at lines 1325,
1344, 1500, 1787, 1809, 1863, 1908, 1972, 2018 — all `let coin = instrument.raw_symbol().inner();`.
Since `raw_symbol` for a HIP-3 instrument is the venue name `xyz:NVDA` (see (b)), the WS
`l2Book` / `trades` channels receive `"coin": "xyz:NVDA"`.

Docs, *Data subscriptions*:

> The adapter supports the following data subscriptions. All perpetual data types
> (mark prices, index prices, funding rates) apply to both standard and HIP-3 perps.

with `✓` for trade ticks, quote ticks, order book deltas, order book depth10, bars, mark prices,
index prices, funding rates, open interest.

Verified directly against the venue that a dex-qualified coin is accepted:

```
$ curl -s -X POST -H "Content-Type: application/json" \
    -d '{"type":"l2Book","coin":"xyz:NVDA"}' https://api.hyperliquid.xyz/info
HTTP 200
coin: xyz:NVDA
bids top3: [{'px': '231.72', 'sz': '38.919', 'n': 5}, {'px': '231.71', 'sz': '32.927', 'n': 2}, {'px': '231.7', 'sz': '23.232', 'n': 2}]
asks top3: [{'px': '231.74', 'sz': '15.89', 'n': 2}, {'px': '231.75', 'sz': '23.538', 'n': 3}, {'px': '231.76', 'sz': '70.686', 'n': 5}]
```
(saved: `hl_mainnet_l2book_xyz_NVDA.json`)

### Confidence

**High** for data + order-index handling — backed by source, unit tests, published docs and a live
venue round-trip. **Medium-high** for end-to-end order submission on `xyz` specifically: the code
path is unambiguous, but nobody in this repo has yet placed a real HIP-3 order, and the docs' fee /
isolated-margin / per-dex-collateral notes mean the first live order will still surface surprises.

---

## Q2 — Are there US-equity perps on Hyperliquid TESTNET (`xyz` dex) and on Lighter TESTNET?

### Answer

**Yes on both — the instruments exist, but they are effectively dead: no volume and no two-sided
book.** HL testnet `xyz:NVDA` had 1 bid level and 0 asks; Lighter testnet market 110 (NVDA) had
0 bids and 0 asks. Testnet is usable for wiring/auth/order-plumbing validation but **cannot**
validate spread economics, fills, or slippage.

### Evidence

#### Hyperliquid testnet — `perpDexs`

```
$ curl -s -X POST -H "Content-Type: application/json" \
    -d '{"type":"perpDexs"}' https://api.hyperliquid-testnet.xyz/info
HTTP 200   -> hl_testnet_perpDexs.json
```

260 entries (index 0 = `null` = the standard validator dex, then 259 named builder dexes). Full
name list is in the raw file; `xyz` is present at **index 65**. Names include:

`test, unit, scam, felix, volmex, angry, TOON, VOLMEX, test21, pluto, zigg, i<3fl, KNETIQ, knetiq,
ddot, venone, tstdex, vntls, oracle, hb, sports, aura, hybet, orange, sekai, flow, x, t, sekaw, gpu,
qwerty, merrli, dex, olvb, nq, fx, nqz, btcx, woof, shiok, z, hbtwo, tndex, rrrrr, hbhb, stocks,
nunchi, sqtwr, y, hypodd, strat, zdex, crayon, walao, slpy, tsts, mbx, food, meng, rawr, mmmg, msdx,
wrld, hodd, xyz, flx, nmx, pyth, msda, ho, flsh, hyfl, csan, sedx, rbbt, rbte, demo, flxn, bsx, loh,
xvm, dydx, fzx, abdc, fam, gato, bart, sss, owal, gg, fdnk, trve, odd, trv, qwrn, zzzz, gass, whtm,
rip, sand, hmax, slob, hyfi, str, trov, erez, paef, mcd, birb, plsh, cnst, trvv, trvo, tvr, torv,
tvro, tvor, hyna, tdex, neo, bhpx, yldx, dpro, wa, org, hfx, fxx, pmt, trva, trvb, perp, elxl, ddd,
trvc, brd, axx, ddex, ab, etft, llll, nuts, jdo, otcm, xmas, rub, avtr, wgts, vrse, gst, otc, bee,
trvd, unc, dddd, dxn, egrl, nyc, bmx, purr, vdex, narr, abcd, mox, angl, trvf, hnb, ggwp, plat,
orma, ormb, ormc, ormd, orme, ormf, ormg, ormh, tngs, ormi, brnt, yex, ez, rbit, ysh, moo, guac,
osrs, moom, idx, scpe, mag, frce, hodx, yoyo, cmdt, cmda, xoxo, idxx, cmdb, ycl, ohtn, zktx, soci,
otni, nova, ani, soya, brs, tenx, jack, myx, pmas, dude, rsge, hov, dlx, dxx, dyel, blob, mooo,
rolo, sejp, sjhu, dolo, sqxl, krzh, kwo, name, aha, tsst, neol, flxx, dd, vt, hfl, hkx, pew, magm,
eepy, magk, ktob, nxlb, dur, shm, mmmm, acmt, cna, bb, dmhs, lah, lob, vkjf, ignp, kjex, arg, cfbx,
lon, par, aix, parx`

#### Hyperliquid testnet — `meta` for every dex

I ran `{"type":"meta","dex":"<name>"}` against `https://api.hyperliquid-testnet.xyz/info` for all
259 named dexes (all HTTP 200 except 6 transient TLS/timeout failures listed below). The combined
result is `hl_testnet_meta_all_dexes.json`; individual files are saved for the notable ones.

Failed after 2 attempts (recorded and skipped, per stop condition): `nunchi`, `fzx`, `rip`, `hmax`,
`brs`, `name` — TLS `UNEXPECTED_EOF_WHILE_READING` / WinError 10060. All six are tiny throwaway
dexes; none is a plausible equity venue.

Dexes on testnet carrying US-equity-looking assets:

| dex     | universe size | equity-looking names |
| ------- | ------------- | -------------------- |
| **xyz** | **70**        | AAPL, AMD, AMZN, AVGO, BABA, COIN, CRCL, GME, GOOGL, HOOD, IBM, INTC, META, MSFT, MSTR, MU, NFLX, **NVDA**, ORCL, PLTR, SP500, **TSLA**, TSM |
| bart    | 18            | AAPL, QQQ, SPY        |
| xvm     | 6             | HOOD, NVDA, TSLA      |
| z       | 5             | HOOD, NVDA, TSLA      |
| flxn    | 5             | NVDA, TSLA            |
| felix   | 4             | CRCL, TSLA            |
| x       | 4             | NVDA, TSLA            |
| dd      | 7             | TSLA, US500           |
| dddd    | 7             | TSLA, US500           |
| flx     | 2             | NVDA, TSLA            |
| org     | 3             | NVDA                  |
| parx    | 3             | NVDA                  |
| dpro    | 3             | AAPL                  |
| zigg    | 2             | AAPL                  |
| bmx     | 5             | TSLA                  |
| elxl    | 2             | TSLA                  |
| eepy / flxx / stocks / zzzz | 1 | TSLA      |

**Testnet `xyz` universe (70 assets, `hl_testnet_meta_xyz.json`):**

```
xyz:XYZ100, xyz:TSLA, xyz:NVDA, xyz:GOLD, xyz:HOOD, xyz:INTC, xyz:PLTR, xyz:COIN, xyz:META,
xyz:AAPL, xyz:MSFT, xyz:ORCL, xyz:GOOGL, xyz:AMZN, xyz:AMD, xyz:MU, xyz:SNDK, xyz:MSTR, xyz:CRCL,
xyz:NFLX, xyz:COST, xyz:LLY, xyz:SKHX, xyz:TSM, xyz:JPY, xyz:EUR, xyz:SILVER, xyz:RIVN, xyz:BABA,
xyz:CL, xyz:COPPER, xyz:NATGAS, xyz:URANIUM, xyz:ALUMINIUM, xyz:SMSN, xyz:PLATINUM, xyz:USAR,
xyz:CRWV, xyz:URNM, xyz:PALLADIUM, xyz:DXY, xyz:GME, xyz:KR200, xyz:SOFTBANK, xyz:JP225,
xyz:HYUNDAI, xyz:KIOXIA, xyz:EWY, xyz:EWJ, xyz:BRENTOIL, xyz:VIX, xyz:HIMS, xyz:SP500, xyz:DKNG,
xyz:LITE, xyz:CORN, xyz:XLE, xyz:WHEAT, xyz:TTF, xyz:BX, xyz:PURRDAT, xyz:MRVL, xyz:RKLB, xyz:BIRD,
xyz:IBM, xyz:AVGO, xyz:NOW, xyz:NBIS, xyz:KORU, xyz:IREN
```

Universe order matters for the asset index. Testnet `xyz` is dex index 65, so
`xyz:TSLA` = `100000 + 65*10000 + 1 = 750001`, `xyz:NVDA` = `750002`, `xyz:MSFT` = `750010`.

**Testnet liquidity is the problem.** `{"type":"metaAndAssetCtxs","dex":"xyz"}`
(`hl_testnet_metaAndAssetCtxs_xyz.json`):

```
xyz:TSLA  mark=362.43  oracle=368.5   dayNtlVlm=0.0  openInterest=1.766
xyz:NVDA  mark=231.87  oracle=231.87  dayNtlVlm=0.0  openInterest=71.684
xyz:MSFT  mark=507.96  oracle=507.96  dayNtlVlm=0.0  openInterest=3.442
```

24h notional volume is **zero** on all three. The book confirms it
(`hl_testnet_l2book_xyz_NVDA.json`):

```
$ curl -s -X POST -H "Content-Type: application/json" \
    -d '{"type":"l2Book","coin":"xyz:NVDA"}' https://api.hyperliquid-testnet.xyz/info
HTTP 200
coin: xyz:NVDA
bid levels: 1   ask levels: 0
bids: [{'px': '228.53', 'sz': '0.604', 'n': 1}]
asks: []
```

#### Hyperliquid mainnet — control

```
$ curl -s -X POST -H "Content-Type: application/json" -d '{"type":"perpDexs"}'  https://api.hyperliquid.xyz/info
HTTP 200   -> hl_mainnet_perpDexs.json
$ curl -s -X POST -H "Content-Type: application/json" -d '{"type":"meta","dex":"xyz"}' https://api.hyperliquid.xyz/info
HTTP 200   -> hl_mainnet_meta_xyz.json
```

Mainnet has 11 perp dexes: `[null, xyz, flx, vntl, hyna, km, abcd, cash, para, mkts, io]` — **`xyz`
is index 1**. Mainnet `xyz` universe has **119** assets. `xyz:NVDA` is **present**, at universe index
2 (`xyz:XYZ100`=0, `xyz:TSLA`=1, `xyz:NVDA`=2, ..., `xyz:MSFT`=10). Sample entry:

```json
{"szDecimals": 4, "name": "xyz:XYZ100", "maxLeverage": 30, "marginTableId": 30,
 "growthMode": "enabled", "lastFeeScaleChangeTime": "2025-11-23T17:37:10.033211662",
 "deployerFeeScale": "1.0"}
```

Wire asset ids on mainnet: `xyz:TSLA` = 110001, `xyz:NVDA` = **110002**, `xyz:MSFT` = 110010.

Mainnet `xyz:NVDA` book is deep and two-sided (see Q1(d) above): best bid 231.72 x 38.919,
best ask 231.74 x 15.89 — a 2bp spread with real size.

#### Lighter testnet

`https://testnet.zklighter.elliot.ai` is the correct testnet host (no fallback needed), and the
Nautilus Lighter doc confirms it:

> | Lighter    | Testnet     | `https://testnet.zklighter.elliot.ai` | `wss://testnet.zklighter.elliot.ai/stream` | 300 | USDC | `LIGHTER` |

```
$ curl -s https://testnet.zklighter.elliot.ai/api/v1/orderBooks
HTTP 200   -> lighter_testnet_orderBooks.json      (178 markets, code 200)
```

**NVDA=110, TSLA=112, MSFT=115 — identical market ids to mainnet**, all `status: "active"`:

```json
{"symbol":"NVDA","market_id":110,"market_type":"perp","status":"active","taker_fee":"0.0000",
 "maker_fee":"0.0000","min_base_amount":"0.050","min_quote_amount":"10.000000",
 "supported_size_decimals":3,"supported_price_decimals":3, ...}
{"symbol":"TSLA","market_id":112,...,"min_base_amount":"0.0200","supported_size_decimals":4,"supported_price_decimals":2,...}
{"symbol":"MSFT","market_id":115,...,"min_base_amount":"0.0200","supported_size_decimals":4,"supported_price_decimals":2,...}
```

Other US-equity / equity-index markets on testnet: HOOD 108, COIN 109, PLTR 111, AAPL 113, AMZN 114,
GOOGL 116, META 117, CRCL 121, MSTR 122, BMNR 123, SPY 128, QQQ 129, INTC 137, AMD 138, SNDK 139,
ASML 151, DIA 152, IWM 153, BOTZ 154, MAGS 155, STRC 156, MU 164, ORCL 165, EWY 166, CRWV 167,
TSM 168, SOXX 169, SPACEX 173, MRVL 174, CBRS 175, GME 176, BABA 177, LITE 178, TTWO 179, US500 180,
US100 181, H100 182 — plus FX, metals and energy. Testnet stops at market 184; mainnet continues to
232 and adds the `/USDC` spot pairs 2048-2058.

**But testnet has no book at all:**

```
$ curl -s "https://testnet.zklighter.elliot.ai/api/v1/orderBookOrders?market_id=110&limit=3"
HTTP 200
{"code":200,"total_asks":0,"asks":[],"total_bids":0,"bids":[]}
```

#### Lighter mainnet — control

```
$ curl -s https://mainnet.zklighter.elliot.ai/api/v1/orderBooks
HTTP 200   -> lighter_mainnet_orderBooks.json      (244 markets, code 200)
```

**NVDA = 110 confirmed.** TSLA = 112, MSFT = 115. And the mainnet book is real:

```
$ curl -s "https://mainnet.zklighter.elliot.ai/api/v1/orderBookOrders?market_id=110&limit=3"
HTTP 200
total_bids 3  total_asks 3
best bid price 231.906  remaining_base_amount 11.227
best ask price 231.957  remaining_base_amount 12.927
```

Note the cross-venue price agreement at the moment of capture: HL mainnet `xyz:NVDA` mid ≈ 231.73,
Lighter mainnet NVDA mid ≈ 231.93 — a ~20bp gap, which is exactly the quantity this project exists
to measure.

### Confidence

**High.** Every claim is a live HTTP 200 response saved in this directory. The only gap is the 6
testnet dexes whose `meta` call failed after 2 attempts (`nunchi`, `fzx`, `rip`, `hmax`, `brs`,
`name`) — all irrelevant to the equity question, since testnet `xyz` already has the full set.

### Implication for the plan

Testnet on both venues is good for: auth, signing, instrument loading, symbol mapping, order
submit/cancel/modify plumbing, reconciliation, and adapter wiring. It is **useless** for validating
the spread itself — no volume, no two-sided book, and HL testnet marks drift off oracle
(`xyz:TSLA` mark 362.43 vs oracle 368.5, a 1.6% gap). Spread economics can only be measured against
mainnet **read-only** data. That does not require the `上主网` approval, since it is unauthenticated
market data — but any order does.

---

## Q3 — Nautilus wheel availability for Windows / Python 3.12

### Answer

**Yes for Hyperliquid on the stable line; Lighter needs the 2.0.0 pre-release.** A
`cp312 + win_amd64` wheel exists for both the latest stable **1.231.0** (2026-08-02) and the latest
pre-release **2.0.0rc4** (2026-09-02). Both adapters are Rust-backed and compiled into the standard
wheel — **no optional extras**. However, the Python-level `nautilus_trader.adapters.lighter` package
does **not** exist in the 1.x tree, so a `2.0.0rcX` wheel is required for this project.

### Evidence

```
$ curl -s https://pypi.org/pypi/nautilus-trader/json
HTTP 200   -> pypi_nautilus_trader.json
```

Latest stable version: **1.231.0**, first upload **2026-08-02T18:47:40Z**.
`requires_python: <3.15,>=3.12`. All 13 files:

```
nautilus_trader-1.231.0-cp312-cp312-macosx_26_0_arm64.whl          156.0 MB
nautilus_trader-1.231.0-cp312-cp312-manylinux_2_35_aarch64.whl     170.4 MB
nautilus_trader-1.231.0-cp312-cp312-manylinux_2_35_x86_64.whl      189.0 MB
nautilus_trader-1.231.0-cp312-cp312-win_amd64.whl                  112.8 MB   <-- ours
nautilus_trader-1.231.0-cp313-cp313-macosx_26_0_arm64.whl          156.1 MB
nautilus_trader-1.231.0-cp313-cp313-manylinux_2_35_aarch64.whl     170.5 MB
nautilus_trader-1.231.0-cp313-cp313-manylinux_2_35_x86_64.whl      189.0 MB
nautilus_trader-1.231.0-cp313-cp313-win_amd64.whl                  112.7 MB
nautilus_trader-1.231.0-cp314-cp314-macosx_26_0_arm64.whl          156.3 MB
nautilus_trader-1.231.0-cp314-cp314-manylinux_2_35_aarch64.whl     170.6 MB
nautilus_trader-1.231.0-cp314-cp314-manylinux_2_35_x86_64.whl      189.0 MB
nautilus_trader-1.231.0-cp314-cp314-win_amd64.whl                  115.3 MB
nautilus_trader-1.231.0.tar.gz                                      10.3 MB
```

**A `win_amd64` + `cp312` wheel exists: `nautilus_trader-1.231.0-cp312-cp312-win_amd64.whl`.**

Recent releases, all with a cp312 Windows wheel:

| version   | first upload | files | cp312-win_amd64 wheel |
| --------- | ------------ | ----- | --------------------- |
| 1.230.0   | 2026-06-29   | 13    | yes |
| **1.231.0** (latest stable) | 2026-08-02 | 13 | yes |
| 2.0.0rc1  | 2026-06-29   | 12    | yes |
| 2.0.0rc2  | 2026-08-02   | 12    | yes |
| 2.0.0rc3  | 2026-08-21   | 13    | yes |
| **2.0.0rc4** (latest pre-release) | 2026-09-02 | 13 | yes |

#### Adapters are in the standard wheel, not extras

`crates/pyo3/Cargo.toml` (the maturin manifest that builds `nautilus_trader._libnautilus`) lists
both adapters as **non-optional** dependencies — contrast `nautilus-blockchain`, which carries
`optional = true`:

```toml
nautilus-hyperliquid = { workspace = true, features = ["python"] }   # line 150 (develop) / 158 (v1.231.0)
nautilus-lighter     = { workspace = true, features = ["python"] }   # line 153 (develop) / 161 (v1.231.0)
nautilus-blockchain  = { workspace = true, features = ["python"], optional = true }
```

Identical in both `develop` and tag `v1.231.0`. The declared PyPI extras contain neither:

- `python/pyproject.toml` (develop, 2.0.0rc5): `[project.optional-dependencies]` = `visualization`
  only.
- `pyproject.toml` (v1.231.0): `betfair`, `ib`, `docker`, `polymarket`, `visualization`.

So `pip install nautilus-trader` (or `uv add`) gets both adapters with no extra.

The Python surface is a thin re-export over the compiled Rust module —
`python/nautilus_trader/adapters/hyperliquid/__init__.py` (develop):

```python
from nautilus_trader._libnautilus.hyperliquid import *
__all__ = ["HYPERLIQUID", "HYPERLIQUID_CLIENT_ID", "HYPERLIQUID_VENUE", ...
           "HyperliquidDataClientConfig", "HyperliquidDataClientFactory",
           "HyperliquidEnvironment", "HyperliquidExecutionClientConfig",
           "HyperliquidExecutionClientFactory", ...]
```

and `python/nautilus_trader/adapters/lighter/__init__.py`:

```python
from nautilus_trader._libnautilus.lighter import *
__all__ = ["LIGHTER", "LIGHTER_CLIENT_ID", "LIGHTER_ROBINHOOD", "LIGHTER_ROBINHOOD_CLIENT_ID",
           "LIGHTER_ROBINHOOD_VENUE", "LIGHTER_VENUE", "LighterDataClientConfig",
           "LighterDataClientFactory", "LighterDeployment", "LighterEnvironment",
           "LighterExecutionClientConfig", "LighterExecutionClientFactory",
           "revoke_lighter_integrator"]
```

#### ⚠ 1.x vs 2.x — the version choice is not free

- `develop` is **2.0.0rc5** (`python/pyproject.toml`, `version = "2.0.0rc5"`), and the repo layout
  moved: Python now lives under `python/nautilus_trader/`, not `nautilus_trader/`.
- The **published docs at `nautilustrader.io/docs/latest` track 2.x**, not 1.231.0. I verified the
  live page's HIP-3 wording matches `develop`/`v2.0.0rc4` (`LiveNode`, "no per-dex filter") and
  differs from `v1.231.0` (which documents `TradingNode` and a `market_types: ["perp_hip3"]`
  provider filter). Reading `/docs/latest` while running 1.231.0 will mislead.
- Probing tag `v1.231.0` on raw.githubusercontent:
  - `nautilus_trader/adapters/hyperliquid/__init__.py` → **HTTP 200** (Hyperliquid adapter present,
    full HIP-3 support per `v1.231.0/docs/integrations/hyperliquid.md`)
  - `nautilus_trader/adapters/lighter/__init__.py` → **HTTP 404**
  - `nautilus_trader/adapters/lighter/config.py` → **HTTP 404**
  - `nautilus_trader/adapters/lighter/constants.py` → **HTTP 404**
  - but `crates/adapters/lighter/Cargo.toml` → **HTTP 200**, and root `Cargo.toml` lists
    `nautilus-lighter = { path = "crates/adapters/lighter", version = "0.61.0" }`
- The Lighter doc shipped in the v1.231.0 tag already describes the v2 layout — it points at
  `python/examples/lighter/` and instructs `cd python`, and says: *"The Python surface is
  intentionally narrow. The Python extension exposes configuration, environment selection, factory
  classes, and integrator revocation; data and execution clients are consumed through the Rust trait
  surface."*

**Reading:** the Lighter adapter's Python surface is v2-only. `nautilus_trader.adapters.lighter`
should be treated as available from `2.0.0rcX` (rc4 = 2026-09-02, two days ago) onward.

**Recommendation for the install agent:** target `nautilus-trader==2.0.0rc4`
(`nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`, uploaded 2026-09-02) rather than 1.231.0, and
verify at import time:

```python
import nautilus_trader
from nautilus_trader.adapters.hyperliquid import HyperliquidDataClientConfig, HyperliquidEnvironment
from nautilus_trader.adapters.lighter import LighterDataClientConfig, LighterEnvironment, LighterDeployment
print(nautilus_trader.__version__)
```

If both imports succeed, the wheel question is settled. If the project must stay on stable 1.231.0,
Lighter has no Python client surface and the plan needs rethinking.

### Confidence

**High** on wheel availability (direct PyPI JSON) and on adapters-not-being-extras (Cargo manifests
in both trees). **Medium** on the precise version where `nautilus_trader.adapters.lighter` first
appears — inferred from tag-level 404/200 probes and the doc's own v2 references, not from unpacking
a wheel. The install agent will settle it in one import.

---

## Files in this directory

| File | What it is |
| ---- | ---------- |
| `hl_testnet_perpDexs.json` | HL testnet `{"type":"perpDexs"}` — 260 entries |
| `hl_testnet_meta_all_dexes.json` | HL testnet `meta` for all 259 named dexes, keyed by dex name |
| `hl_testnet_meta_xyz.json` | HL testnet `{"type":"meta","dex":"xyz"}` — 70 assets |
| `hl_testnet_meta_{felix,stocks,flx,x,xvm,z,bart}.json` | other testnet dexes with equity names |
| `hl_testnet_metaAndAssetCtxs_xyz.json` | HL testnet xyz marks/oracles/volume — proves `dayNtlVlm=0` |
| `hl_testnet_l2book_xyz_NVDA.json` | HL testnet `xyz:NVDA` book — 1 bid, 0 asks |
| `hl_mainnet_perpDexs.json` | HL mainnet `{"type":"perpDexs"}` — 11 entries, `xyz` at index 1 |
| `hl_mainnet_meta_xyz.json` | HL mainnet `{"type":"meta","dex":"xyz"}` — 119 assets incl. `xyz:NVDA` |
| `hl_mainnet_l2book_xyz_NVDA.json` | HL mainnet `xyz:NVDA` book — deep, two-sided |
| `lighter_testnet_orderBooks.json` | Lighter testnet markets — 178, NVDA=110/TSLA=112/MSFT=115 |
| `lighter_testnet_orderBookOrders_110.json` | Lighter testnet NVDA book — empty |
| `lighter_mainnet_orderBooks.json` | Lighter mainnet markets — 244, NVDA=110 confirmed |
| `lighter_mainnet_orderBookOrders_110.json` | Lighter mainnet NVDA book — live orders |
| `pypi_nautilus_trader.json` | Full PyPI JSON metadata for `nautilus-trader` |

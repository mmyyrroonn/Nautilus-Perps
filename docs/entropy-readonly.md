# Entropy `io:` 只读接入 runbook（SNDK / GPRO）

本文件是 `--venues HL,ENTROPY,ASTER` 这条只读行情路径的操作说明。实现计划与取舍见
[`docs/superpowers/plans/2026-09-14-entropy-io-sndk-gpro.md`](superpowers/plans/2026-09-14-entropy-io-sndk-gpro.md)，
规划阶段的公开核查证据见 [`reports/entropy-plan-2026-09-14`](../reports/entropy-plan-2026-09-14/README.md)。

本路径只做公开行情：top-of-book、L2、public trades、funding，以及既有的 CSV 分析。
没有 Entropy 专用适配器、没有 ExecClient、没有签名、没有账户查询、没有下单。

采集前必须先做第 2 节的公开 preflight：`--dry-run` 是离线的，它不确认市场还在、费率或
funding 周期没变。

## 1. 语义：逻辑腿与数据客户端

`ENTROPY` 是一个**逻辑腿**，不是新的 Nautilus venue。Entropy 是 Hyperliquid 上的
HIP-3 builder dex（`io:`），因此它和 `HL` 共享同一个 `HYPERLIQUID` data client；
真实 venue 与 ClientId 仍是 `HYPERLIQUID`。

| 逻辑 symbol / leg | InstrumentId | 原始市场名 | ClientId | 费用基准 |
|---|---|---|---|---|
| SNDK / HL | `xyz:SNDK-USD-PERP.HYPERLIQUID` | `xyz:SNDK` | `HYPERLIQUID` | 0.9 bp |
| SNDK / ENTROPY | `io:SNDK-USD-PERP.HYPERLIQUID` | `io:SNDK` | `HYPERLIQUID` | 0.9 bp |
| GPRO / ENTROPY | `io:GPRO-USD-PERP.HYPERLIQUID` | `io:GPRO` | `HYPERLIQUID` | 0.9 bp |
| SNDK / ASTER | `SNDKUSD1-PERP.ASTER` | `SNDKUSD1` | `ASTER` | 0.9 bp |
| GPRO / ASTER | `GPROUSD1-PERP.ASTER` | `GPROUSD1` | `ASTER` | 0.9 bp |

- `SNDK` 的三条腿产生 6 个有向组合，`GPRO` 的两条腿产生 2 个；CSV 的 `venue` /
  `sell_venue` / `buy_venue` 写逻辑标签，Entropy 写 `ENTROPY` 而不是 `HL`。
- `GPRO` 只映射了 ENTROPY 与 ASTER。请求其他 venue 会被跳过并在 stderr 打印
  `no instrument mapped`，这不是失败；少于两条腿才是失败。
- `ENTROPY` 不在 `DEFAULT_VENUES` / `DEFAULT_PAIR` 里，必须显式写出来。
- HL(`xyz:`) 与 ENTROPY(`io:`) 是同一平台上的两个 builder 市场，**不是**两个独立
  交易所的风险分散。

## 2. 采集前的公开 preflight（每次采集前都要跑，dry-run 不联网）

`--dry-run` 只打印本仓库的静态映射：它**不验证**市场是否还在、费率与 funding 周期是否变过。
所以从 dry-run 直接进采集是不够的，采集前必须自己核对下面四项并按 UTC 存档响应
（本次的执行记录见 [`reports/entropy-io-acceptance`](../reports/entropy-io-acceptance/README.md)）：

| 请求 | 必须检查 |
|---|---|
| `POST https://api.hyperliquid.xyz/info`，body `{"type":"metaAndAssetCtxs","dex":"io"}` | `io:SNDK` / `io:GPRO` 存在且未 delisted；`response[0].collateralToken == 0`；两条目 `growthMode == "enabled"`、`deployerFeeScale == "1.0"`。后两个字段在 universe 条目里，`instrument.info` 是空字典，读不到 |
| 同上，body `{"type":"perpDexs"}` | 在**完整数组**里定位名为 `io` 的索引（先过滤 null 会错位）；留存 `assetToFundingMultiplier`（当前 SNDK 0.125 / GPRO 0.5，已在上游生效，不要再乘一次） |
| `GET https://fapi.asterdex.com/fapi/v1/exchangeInfo` | `SNDKUSD1` / `GPROUSD1` 为 `TRADING` / `PERPETUAL`；quote 与 margin 都是 `USD1`；留存 filters |
| `GET https://fapi.asterdex.com/fapi/v1/fundingInfo` | 这两个 USD1 合约的 `fundingIntervalHours`（当前为 8） |

```powershell
# 四项公开请求，不需要账户；结果写到 reports/entropy-io-acceptance/<UTC stamp>/
@'
import datetime, json, pathlib, urllib.request
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
out = pathlib.Path('reports/entropy-io-acceptance') / stamp
out.mkdir(parents=True, exist_ok=True)
queries = [
    ('io-meta', 'https://api.hyperliquid.xyz/info', {'type':'metaAndAssetCtxs','dex':'io'}),
    ('dexs', 'https://api.hyperliquid.xyz/info', {'type':'perpDexs'}),
    ('aster-meta', 'https://fapi.asterdex.com/fapi/v1/exchangeInfo', None),
    ('aster-funding', 'https://fapi.asterdex.com/fapi/v1/fundingInfo', None),
]
failed = False
for name, url, body in queries:
    row = {'url': url, 'request': body,
           'requested_at_utc': datetime.datetime.now(datetime.timezone.utc).isoformat()}
    for attempt in (1, 2):  # 本机到交易所偶发 SSLEOFError / 连接超时：同进程内重试一次
        try:
            req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=20) as response:
                row['status'] = response.status
                row['response'] = json.load(response)
            break
        except Exception as exc:
            row['error'] = repr(exc)
    row['attempt'] = attempt
    if 'status' not in row:
        failed = True
    (out / (name + '.json')).write_text(json.dumps(row, indent=2) + '\n', encoding='utf-8')
    print(name, row.get('status'), row.get('error'), 'attempt', attempt)
print(out)
raise SystemExit(1 if failed else 0)
'@ | .\.venv\Scripts\python.exe -
```

还要确认 wheel 真能把 io: 合约装进 cache。HTTP 辅助函数的 `include_perps_hip3` 默认是
`False`，不显式传 `True` 拿到的列表里根本没有 HIP-3 合约（LiveNode 的 data client 走它自己的
加载路径，不受这个参数影响）：

```powershell
@'
import asyncio
from nautilus_trader.adapters.hyperliquid import HyperliquidHttpClient, HyperliquidEnvironment
async def main():
    client = HyperliquidHttpClient(environment=HyperliquidEnvironment.MAINNET, timeout_secs=20)
    items = await client.load_instrument_definitions(
        include_spot=False, include_perps=True, include_perps_hip3=True)
    by_id = {str(item.id): item for item in items}
    required = {'io:SNDK-USD-PERP.HYPERLIQUID', 'io:GPRO-USD-PERP.HYPERLIQUID',
                'xyz:SNDK-USD-PERP.HYPERLIQUID'}
    print('loaded_count=', len(items))
    for instrument_id in sorted(required & by_id.keys()):
        inst = by_id[instrument_id]
        print(inst.id, inst.raw_symbol, inst.size_increment, inst.settlement_currency)
    missing = required - by_id.keys()
    if missing:
        raise SystemExit('Missing instruments: ' + ', '.join(sorted(missing)))
asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -
```

**任何一项对不上时**：不要拿旧的成本假设继续采集还说结果正确。把变化记下来，按证据更新
`ENTROPY_TAKER_FEE_BPS`、`FUNDING_SCALE["ENTROPY"]`、`ASTER_FUNDING_HOURS` 及相应断言后再跑；
如果变化涉及合约定义或执行支持（状态、结算币种、市场名），就停在只读验收并把边界写进报告。

## 3. 命令

```powershell
# 1. 先看映射，不联网、不加载 .env、不建客户端（preflight 见上一节）
.\.venv\Scripts\python.exe src/spread_watch.py --symbols SNDK,GPRO --venues HL,ENTROPY,ASTER --dry-run

# 2. 2 分钟公开只读采集（本次验收范围）
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$runDir = Join-Path 'reports\entropy-io-acceptance' $stamp
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
.\.venv\Scripts\python.exe src/spread_watch.py --symbols SNDK,GPRO --venues HL,ENTROPY,ASTER `
    --minutes 2 --max-restarts 0 --out $runDir *> (Join-Path $runDir 'watch.log')
Write-Output "watch_exit_code=$LASTEXITCODE"

# 3. 离线分析（stamp 取自实际文件名，不要假定与目录名相同）
.\.venv\Scripts\python.exe src/analysis/opportunities.py --dir $runDir `
    --stamp <watcher 文件名里的 stamp> --symbols SNDK,GPRO --md (Join-Path $runDir 'opps.md')
```

`--dry-run` 的 stdout 是纯 JSON（跳过腿的 INFO 走 stderr），可用
`| ConvertFrom-Json` 或 `json.loads` 解析。`--pair SNDK:ENTROPY-ASTER` 是旧的单对
别名，等价于 `--symbols SNDK --venues ENTROPY,ASTER`。

注意：Windows PowerShell 5.1 里 `*>` 会写出 UTF-16（带 BOM）的 `watch.log`，用别的工具读会
报编码错。要 UTF-8 就改成 `2>&1 | Out-String` 再 `[IO.File]::WriteAllText(..., UTF8Encoding($false))`
（本次两次 smoke 的日志就是这么落盘的）。

## 4. 预期结果

- **客户端 2 个**：`HYPERLIQUID`（3 个 instrument：xyz:SNDK、io:SNDK、io:GPRO）与
  `ASTER`（2 个：SNDKUSD1、GPROUSD1）。同一 ClientId 不会被注册两次。
- **腿 5 条**，每条腿在 summary 里都应 `top-of-book updates>0` 且
  `depth` 行存在有效双侧盘口；同 stamp 重启不重复写表头。
- **成本口径**：`net_bps = gross_bps - sell_taker - buy_taker - 5`。io/Aster 目前是
  0.9+0.9+5 = **6.8 bp**，只扣建仓费与风险预留；四笔 taker 的纯手续费约 3.6 bp，
  但真实退出价、funding、滑点、USDC/USD1/股票之间的币种基差都不在其中。这不是完整
  开平仓净利润。
- **funding 单位**：CSV 存 adapter 给的原始值，不归一。**HL 与 ENTROPY 是小时小数费率**
  （0.0001 → 1 bp/h）；**Lighter 是小时百分数**（适配器不除 100，0.01 → 1 bp/h，当小数读会
  放大 100 倍）；**Aster 是按该合约的 1 / 4 / 8 小时区间的小数费率**（当前 SNDKUSD1、GPROUSD1
  均为 8 小时）。
  Entropy 的 `assetToFundingMultiplier` 已在上游生效，**不要再乘一次**。
  `funding_seen=True` 只证明收到过 funding 更新，不证明发生过资金费支付。
- **时间字段**：`age_*_ms` 来自 `ts_init` 接收时间，不是两腿成交/交易所事件时间的同步
  保证。本次没有重写时间门控。
- **零值**：没有成交时 trades=0、没有正价差时 hits 文件只有表头、funding 未到达时保持
  为空。三者都不能被解读为连接失败。

## 5. 失败判定

| 现象 | 含义 | 退出方式 |
|---|---|---|
| stderr `[stage1] STARTUP FAILED <symbol>/<leg>: instrument not loaded: <id>` | cache 里没有该 instrument（可能下架，也可能元数据加载失败） | `SystemExit(1)`，不自动重建 |
| stderr `[stage1] INCOMPLETE <symbol>/<leg>: no top-of-book data for <id>` | 本次运行该腿一次报价都没收到 | `SystemExit(1)` |
| 部分数据后异常退出（`node run failed`） | 订阅/TLS/重连问题 | 按日志判定，不算通过 |

只收到任意一条腿**不**算成功。覆盖集只回答「这次运行每条腿至少收到一次报价」，
持续在线、断流恢复与盘口新鲜度要看日志和更长的独立观察。

## 6. 范围边界

- 不修改 Rust fork / wheel，不新增依赖，不新建 Entropy 适配器。
- `src/funding_history.py` 的采集 venue 列表**没有**加入 ENTROPY：历史 funding 仍只有
  原有 venue，不要把它当成已支持 Entropy。
- 本次不产出 VWAP。depth CSV 是 2/5/10 bp 的容量汇总，不能重建任意数量的逐档成交价；
  需要 $100 / $1,000 可执行价差时要另行加入完整档位与同数量 VWAP。
- 币种、oracle、交易时段、公司行动的等价性尚未验收，BBO 比较按名义 1:1 美元口径，
  仅供观察。
- 不自动启动 30 分钟以上或整日采集；不进入下单、转账、隔离保证金与返佣环节。

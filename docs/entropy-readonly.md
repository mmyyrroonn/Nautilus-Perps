# Entropy `io:` 只读接入 runbook（SNDK / GPRO）

本文件是 `--venues HL,ENTROPY,ASTER` 这条只读行情路径的操作说明。实现计划与取舍见
[`docs/superpowers/plans/2026-09-14-entropy-io-sndk-gpro.md`](superpowers/plans/2026-09-14-entropy-io-sndk-gpro.md)，
规划阶段的公开核查证据见 [`reports/entropy-plan-2026-09-14`](../reports/entropy-plan-2026-09-14/README.md)。

本路径只做公开行情：top-of-book、L2、public trades、funding，以及既有的 CSV 分析。
没有 Entropy 专用适配器、没有 ExecClient、没有签名、没有账户查询、没有下单。

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

## 2. 命令

```powershell
# 1. 先看计划，不联网、不加载 .env、不建客户端
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

## 3. 预期结果

- **客户端 2 个**：`HYPERLIQUID`（3 个 instrument：xyz:SNDK、io:SNDK、io:GPRO）与
  `ASTER`（2 个：SNDKUSD1、GPROUSD1）。同一 ClientId 不会被注册两次。
- **腿 5 条**，每条腿在 summary 里都应 `top-of-book updates>0` 且
  `depth` 行存在有效双侧盘口；同 stamp 重启不重复写表头。
- **成本口径**：`net_bps = gross_bps - sell_taker - buy_taker - 5`。io/Aster 目前是
  0.9+0.9+5 = **6.8 bp**，只扣建仓费与风险预留；四笔 taker 的纯手续费约 3.6 bp，
  但真实退出价、funding、滑点、USDC/USD1/股票之间的币种基差都不在其中。这不是完整
  开平仓净利润。
- **funding 单位**：CSV 存 adapter 给的原始值。HL / Lighter / ENTROPY 是小时小数费率，
  Aster 是按该合约的 1 / 4 / 8 小时区间（当前 SNDKUSD1、GPROUSD1 均为 8 小时）。
  Entropy 的 `assetToFundingMultiplier` 已在上游生效，**不要再乘一次**。
  `funding_seen=True` 只证明收到过 funding 更新，不证明发生过资金费支付。
- **时间字段**：`age_*_ms` 来自 `ts_init` 接收时间，不是两腿成交/交易所事件时间的同步
  保证。本次没有重写时间门控。
- **零值**：没有成交时 trades=0、没有正价差时 hits 文件只有表头、funding 未到达时保持
  为空。三者都不能被解读为连接失败。

## 4. 失败判定

| 现象 | 含义 | 退出方式 |
|---|---|---|
| stderr `[stage1] STARTUP FAILED <symbol>/<leg>: instrument not loaded: <id>` | cache 里没有该 instrument（可能下架，也可能元数据加载失败） | `SystemExit(1)`，不自动重建 |
| stderr `[stage1] INCOMPLETE <symbol>/<leg>: no top-of-book data for <id>` | 本次运行该腿一次报价都没收到 | `SystemExit(1)` |
| 部分数据后异常退出（`node run failed`） | 订阅/TLS/重连问题 | 按日志判定，不算通过 |

只收到任意一条腿**不**算成功。覆盖集只回答「这次运行每条腿至少收到一次报价」，
持续在线、断流恢复与盘口新鲜度要看日志和更长的独立观察。

## 5. 范围边界

- 不修改 Rust fork / wheel，不新增依赖，不新建 Entropy 适配器。
- `src/funding_history.py` 的采集 venue 列表**没有**加入 ENTROPY：历史 funding 仍只有
  原有 venue，不要把它当成已支持 Entropy。
- 本次不产出 VWAP。depth CSV 是 2/5/10 bp 的容量汇总，不能重建任意数量的逐档成交价；
  需要 $100 / $1,000 可执行价差时要另行加入完整档位与同数量 VWAP。
- 币种、oracle、交易时段、公司行动的等价性尚未验收，BBO 比较按名义 1:1 美元口径，
  仅供观察。
- 不自动启动 30 分钟以上或整日采集；不进入下单、转账、隔离保证金与返佣环节。

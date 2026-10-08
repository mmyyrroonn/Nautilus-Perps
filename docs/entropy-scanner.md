# Entropy 动态目录与 L2 scanner

当前接入对应 [应用 #42](https://github.com/mmyyrroonn/Nautilus-Perps/issues/42)。
使用 `opportunity_universe.py` 发现 Hyperliquid `dex=io`，然后生成当前
`opportunity_scan.py` 的只读配置。旧 [Entropy watcher](entropy-readonly.md) 的
2026-09-14 BBO 观察属于历史记录。

入口不读取 `.env`，只注册公共数据客户端。`ENTROPY` 是逻辑行情腿，真实 client/venue
为 `HYPERLIQUID`；HL 主市场、xyz 和 io 在同一平台上，builder 市场不提供独立平台风险分散。
账户、订单和实际经济结果由原生 #100–#102、应用 #43 继续验收。

## 目录与显式匹配

默认 crypto discovery 的场所列表和分类规则保持原样。Entropy 必须显式选择 `--dex io`；
该模式默认选择 `ENTROPY,ASTER`。动态请求 `perpDexs`、io `metaAndAssetCtxs` 和
`spotMeta`，保留 null dex 槽和所有原始市场索引。结算/抵押 token 按 `collateralToken`
对应的 token index 解析，首版接受已核实的 canonical mainnet USDC。

完整目录不会自动生成 ticker 对腿。首版匹配表
[`config/entropy-matches.example.json`](../config/entropy-matches.example.json)
声明 SNDK、GPRO 的 io/Aster 股票永续比较，并注明单位、币种估值、oracle、交易时段和公司行动。
这是一份有限只读比较声明：原生单位和 USD/USDC/USD1 平价是显式假设，证券类别及可对冲
等价性未获完整验收。状态、精度、币种和元数据不符时排除该匹配，摘要保留逐市场理由。
股票组与自动 crypto 组的 canonical symbol 冲突会停止生成。

2026-10-08 核查的 Entropy Tier-0 观察费率为 0.9 bp，前提是 growth enabled、
deployerFeeScale 1.0、无未核实账户折扣；Aster 官方 RWA/USD1 RWA taker 为 1.25 bp。
匹配表将这些列为带来源的配置假设，更新目录时须重新核查。当前 funding context 及部署者
multiplier 原样留存，不再次相乘；缺失费率、预计 funding 和实际 funding 支付均不补零。
scanner 的净边际只是同数量建仓估计，不含完整开平仓、funding 或实际收益。

## 命令

在经过 native candidate 流程安装 fork wheel 的项目环境运行。当前开发工作树为
`E:\persarb\worktrees\entropy-app`；合并后可在更新到对应 main 的应用仓运行。

```powershell
# 完整 io 目录；不要求匹配表。仅 --summary / --capture-dir 明确请求的文件会落盘。
.\.venv\Scripts\python.exe src/opportunity_universe.py --dex io --catalog-only `
  --capture-dir reports/entropy-current/metadata --summary reports/entropy-current/catalog.json

# 当前公共元数据 + 显式匹配生成 scanner 配置和来源摘要。
.\.venv\Scripts\python.exe src/opportunity_universe.py --dex io --venues ENTROPY,ASTER `
  --entropy-matches config/entropy-matches.example.json `
  --capture-dir reports/entropy-current/metadata `
  --output config/entropy-current.toml --summary reports/entropy-current/discovery.json

# 纯离线重建/预览：不联网，不导入 native，不创建输出。
.\.venv\Scripts\python.exe src/opportunity_universe.py --dex io `
  --metadata-dir reports/entropy-current/metadata --entropy-matches config/entropy-matches.example.json `
  --output config/entropy-current.toml --dry-run

# 配置预览，然后有限公共 L2 检查；连接检查强制关闭机会录制。
.\.venv\Scripts\python.exe src/opportunity_scan.py --config config/entropy-current.toml --dry-run
.\.venv\Scripts\python.exe src/opportunity_connections.py --config config/entropy-current.toml `
  --duration-secs 60 --report reports/entropy-current/connections.json

# 可选只显示当前机会，最长运行 2 分钟。
.\.venv\Scripts\python.exe src/opportunity_scan.py --config config/entropy-current.toml `
  --no-record --duration-secs 120
```

发现命令的 `--dry-run` 表示不写文件；未指定 `--metadata-dir` 时仍会读取公开目录。
纯离线预览使用第三条命令。`--capture-dir` 保存有界原始响应、三组件请求/UTC/哈希及
合成 wrapper。离线重放有 sidecar 时验证原始字节、request dex 和 wrapper 一致性，再恢复
原捕获来源；没有 sidecar 的旧材料标明捕获时间 unknown，不把文件 mtime 当采集时间。

生成配置携带两腿 `expected_instrument`，记录原生 symbol、报价/结算币、数量步长与乘数。
运行时由正常 factory/cache 重新取得 instrument 并逐项核对，不匹配时停止扫描。
事件证据保留每腿 logical venue/client/dex，使用完整 InstrumentId 隔离。
资金与保证金尚未接入此只读入口。

## 验收与限制

当前候选、命令、离线用例、实际 wheel 本地 peer 和公共观察分别记录在
[`reports/entropy-discovery/20261008-acceptance`](../reports/entropy-discovery/20261008-acceptance/acceptance.md)。
官方来源和当前公共目录证据在
[`20261008-public`](../reports/entropy-discovery/20261008-public/README.md)。

扫描沿用同数量 VWAP、真实 lot/乘数/最低数量、双侧有效性、有限深度、source/receive age 和
两腿 skew 门控。原生本地 peer 验证了 io/xyz 共用客户端、Aster 独立客户端、cache 精度、
多档 VWAP、空簿/浅簿/过期/时间差和 Hyperliquid 真实 socket 重连。
本地合成数据与真实公共场所观察分开报告；Aster 物理重连和长期 soak 尚未验收。

该能力不能作为新风险准入、真实 flat、实际 funding 收付、盈利或股票对冲等价性的证据。
io 账户作用域、执行恢复和经济事件需使用其对应原生候选继续逐项完成。

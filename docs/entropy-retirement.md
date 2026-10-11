# Entropy 专项取消与 Hyperliquid HIP-3 接入结论

记录日期：2026-10-11。用户要求撤销最近 Entropy 支持专项并关闭相关专项 issue。
本文件记录本地回退候选的范围；远端提交、合并及 issue 状态以 GitHub 实际记录为准。

## 接入结论与官方依据

Entropy `io:` 与 `xyz:` 都是 Hyperliquid HIP-3 builder 市场，沿用同一 Hyperliquid
行情与交易接口。官方 [Asset IDs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)
规定 builder perpetual 以 `{dex}:{coin}` 命名，资产编号按
`100000 + perp_dex_index * 10000 + index_in_meta` 计算。
官方 [Exchange endpoint](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/exchange-endpoint)
用同一 `/exchange` 接口接受下单、撤单、改单动作，通过资产编号选择市场。
由此，`io:` 与 `xyz:` 的协议接入应复用现有 Hyperliquid HIP-3 通用路径，
不需要单独开发 Entropy REST、WebSocket、签名或执行适配器。

现有 `ENTROPY` 标签用于区分策略逻辑腿，实际 Nautilus venue 和客户端仍为
`HYPERLIQUID`。添加品种通常需要明确完整 InstrumentId 和策略映射；市场目录、价格/数量
精度、费用及保证金币种由具体市场元数据决定。HIP-3 通用支持不意味着跨市场经济标的、
账户条件或费用完全相同。

近期专项的动态目录、显式配对校验及执行恢复验收具有各自技术用途，但它们不是
`io:` 协议接入的前置条件。按本次用户决定取消专项，后续普通 HIP-3 使用与通用
Hyperliquid 改进按实际需求处理，不再以“新增 Entropy 适配支持”为理由扩展专项。

## 本地代码回退范围

应用主线提交 `f09e66f0533f53bfd44e14947cd6f0ef0648a94a`
（PR #45，关联 issue #42）于 2026-10-08 合入。此次将以下文件恢复为该提交的父版本：

- `.github/workflows/opportunity-scan.yml`。
- `src/opportunity_universe.py`。
- `src/opportunity_scan.py`。
- `src/opportunity_runtime.py`。

移除该提交新增的专项实现与输入：

- `src/entropy_universe.py` 和 `config/entropy-matches.example.json`。
- `tests/test_entropy_universe.py`、`tests/test_entropy_discovery.py`、`tests/test_entropy_native.py`。
- `tests/fixtures/entropy-public/synthetic-metadata.json`。

普通目录发现、通用 scanner 和此前已有的 `ENTROPY → HYPERLIQUID` 客户端映射保留。
9 月既有 `spread_watch.py` 的 `io:SNDK` / `io:GPRO` 逻辑腿映射与资金费分析保留，
其操作说明见 [entropy-readonly.md](entropy-readonly.md)。

## 未合并执行分支与历史证据

应用专项执行编排提交 `5b8c144`（`feature/entropy-two-leg`）及对应
`ff8666f`（`feature/entropy-execution-offline`）未进入应用主线。相关 #43 专项取消；
这些分支和后续工作区修改按归档后撤销的方式退役，不作为新的接入依赖或继续合并。
本文件的主线回退不包含这些未合并代码，也不声称远端分支已被删除。

两执行工作树各 11 个已提交新增执行文件已在本地恢复到各自父版本；后续新增的
7 个未跟踪专项文件也已撤销。原生 warm-retry 的两个 Entropy 测试工作区改动已撤销，
Aster 通用错误分类 issue #119 的独立工作区改动保留。撤销前的 33 份源文件、逐文件
SHA-256、tracked patches 与前后状态已保存在
`E:/persarb/archives/entropy-retirement-20261011`。原提交和原验收产物仍保留。

历史记录保留原貌，不将当时“未验收实盘/恢复/经济结果”改写为“缺少 io 协议支持”：

- [2026-10-08 discovery 验收](../reports/entropy-discovery/20261008-acceptance/acceptance.md)。
- [2026-10-08 公共目录与官方来源](../reports/entropy-discovery/20261008-public/README.md)。
- [9 月 watcher 验收](../reports/entropy-io-acceptance/README.md)。
- [9 月 watcher 规划证据](../reports/entropy-plan-2026-09-14/README.md)。

`.gitattributes` 中对 `reports/entropy-discovery/**` 原始字节的保护继续保留。
本次是代码和任务范围收敛，没有签名、账户查询或主网下单。Variational 是独立协议与
独立任务队列，不属于本次 Entropy 回退范围。

## 本地回退验证

使用现有 `E:\persarb\Nautilus-Perps\.venv\Scripts\python.exe`，在
`E:\persarb\worktrees\entropy-retirement-app` 运行：

- 通用 opportunity 的 core、scan、native、universe、connections、coalescing 六组测试：
  147 passed，5 skipped；5 项均因该应用环境缺少 `nautilus_trader.adapters.backpack`，
  涉及三个 Backpack bounds 参数用例、一个实际 scanner node 用例及一个 public factory 用例。
- `tests/test_spread_watch.py` 与 `tests/test_opportunities.py`：97 passed。
- `spread_watch.py --symbols SNDK,GPRO --venues HL,ENTROPY,ASTER --dry-run`：成功；
  io/xyz 三个市场共用一个 `HYPERLIQUID` 客户端，Aster 使用独立客户端。
- 直接用 `parse_plan` 检查完整 `io:SNDK-USD-PERP.HYPERLIQUID` 配置：接受，
  客户端仍为 `HYPERLIQUID`。
- 四个恢复的代码/workflow 文件与 `f09e66f^` 无差异；历史 discovery 报告、
  `.gitattributes`、watcher 与资金费分析代码均与回退前无差异；`git diff --check` 通过。

这些是本地代码回退与离线配置检查，不包含真实凭据、账户查询、签名或实盘下单。

## GitHub 任务记录

2026-10-11 已更新应用 #42/#43 与原生 #100/#101/#102/#109/#111/#113/#115/#117/#120，
记录专项取消与既有 HIP-3 支持结论。此前尚开放的
[应用 #43](https://github.com/mmyyrroonn/Nautilus-Perps/issues/43) 和
[原生 #120](https://github.com/mmyyrroonn/nautilus_trader/issues/120) 已按 `not_planned` 关闭，
其余专项单保持历史关闭状态，原正文与历史验收继续保留。

[总单 #38](https://github.com/mmyyrroonn/Nautilus-Perps/issues/38) 已移除 Entropy 待实施队列，
继续跟踪 Variational；通用文档 #26、组合隔离 #29 和独立 Aster 错误分类 #119
继续按自身范围开放。首次撤销和关闭专项 issue 时，代码仅在上述本地工作树回退，
当时未提交或推送；这是初次撤销的历史状态。此后用户已授权提交、开 PR 与合并，
两仓对应 PR 的实际提交、检查与合并状态，由
[应用 #43](https://github.com/mmyyrroonn/Nautilus-Perps/issues/43) 和
[原生 #120](https://github.com/mmyyrroonn/nautilus_trader/issues/120) 的发布记录跟踪。
专项 issue 关闭本身不代表远端 main 已回退。此次仍未重新构建或安装 wheel，
源码发布状态与当前已安装 wheel 状态须分别核对。

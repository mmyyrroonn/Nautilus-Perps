# Entropy scanner 专项退役说明

2026-10-11，按用户决定取消 Entropy 专项动态发现、配对声明和专用执行验收队列。
`io:` 和 `xyz:` 都是 Hyperliquid HIP-3 builder 市场，使用现有 Hyperliquid 通用接口；
无需为了 `io:` 单独开发 Entropy 适配器。结论、官方依据与回退范围见
[entropy-retirement.md](entropy-retirement.md)。

此前 `opportunity_universe.py --dex io`、`--catalog-only`、`--entropy-matches`、
`--capture-dir` 专项入口及 `config/entropy-matches.example.json` 已移除。
这些命令不再是当前操作入口。普通 crypto 目录发现仍使用
[opportunity-scan.md](opportunity-scan.md) 中的通用命令。

既有 [Entropy watcher](entropy-readonly.md) 的 SNDK/GPRO 映射继续保留。
通用 scanner 可显式配置 `venue = "ENTROPY"` 和完整
`io:SNDK-USD-PERP.HYPERLIQUID` 等 InstrumentId；`ENTROPY` 逻辑腿仍复用
`HYPERLIQUID` 客户端。品种映射、数量单位、费用、估值和比较标的仍需按具体市场配置。

历史验收保留原貌：

- [2026-10-08 discovery 验收](../reports/entropy-discovery/20261008-acceptance/acceptance.md)。
- [2026-10-08 官方来源与公共目录](../reports/entropy-discovery/20261008-public/README.md)。

这些记录描述当时的专项候选、离线检查和公开观察，不代表专项仍在推进，也不代表本次
发生过实盘交易。源码发布状态见退役说明中关联的 PR 与 issue 发布记录。

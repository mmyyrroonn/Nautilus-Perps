# 跨市场领先滞后（lead-lag）：讨论稿

日期：2026-09-07。状态：**未立项**，只是把已核实的事实和待决问题放在一处，供后续单独讨论。数据源部分来自当日联网核查（sub agent，官方页面），价格未经购买验证。

## 1. 策略是什么

美股永续（HL xyz、Lighter、Aster、Lighter on Robinhood Chain）在美股常规时段（RTH，13:30 到 20:00 UTC）由现货定价，永续报价跟随现货移动，滞后从几百毫秒到几秒。策略是：拿更快的真实股价，在永续订单簿上吃掉还没撤的过时挂单，taker 单腿，不对冲或短暂对冲。输的是那家做市商。

它和 Ostium 被盗事件的关系：两者本质都是「我手里的价格比交易所的成交价更真」。Ostium 是预言机定价、无订单簿的永续，攻击者入侵链下报价系统喂假价格，用假价格对流动性金库交易获利。合法版本是用公开的更快报价去吃订单簿上的过时挂单。预言机类平台天然对这种打法设防（延迟、加点、封号），订单簿类平台上这是正常交易，但对手是专业做市商，且做多了会被识别为有毒流量。本项目只考虑订单簿类平台。

## 2. 需要什么

| 要素 | 现状 | 缺口 |
|---|---|---|
| 更快的真实价格源 | 无 | 见第 3 节，需付费订阅 |
| 美东机器 | vultr-worker 在何处未核实 | 数据商在新泽西 NY4 或 AWS 美东；从亚洲直连往返多 200 ms 以上，对该策略是致命的 |
| taker 执行与撤单链路 | 三所适配器可复用 | 端到端延迟未测，目标百毫秒内 |
| 测量工具 | watcher 只录永续侧 | 需要同步录两路并算交叉相关、过时报价的频率与幅度 |

**先测量再交易**：先租一台美东机器，把股价源和永续报价同步录一个 RTH 时段，统计「永续报价落后现货超过成本」的次数和持续时间。没有这个数字之前不写执行逻辑。

## 3. 数据源

分水岭是 non-display 授权：喂给自动交易程序的数据在交易所定义下几乎必然算 non-display，全 SIP 的 non-display 授权约每月一万美元以上，个人不现实。可行路线：

| 供应商 | 计划 | 月价（美元） | 内容 | 个人算法用途 | 来源 |
|---|---|---|---|---|---|
| Databento | Standard + US Equities Basic (DBEQ) | 199 | NYSE Chicago、NYSE National、IEX、MIAX Pearl 四所直连，DBN 二进制协议，NY4 汇聚 | 官方明确允许 non-display，零交易所许可费。覆盖只有部分成交量，深度不全 | https://databento.com/pricing 、 https://databento.com/equities |
| Massive（原 Polygon.io，2025-10-30 更名） | Stocks Advanced | 199 | 全 SIP 实时报价与成交，WebSocket | 仅限个人非专业用户，法人必须走 Business（价格未公开） | https://massive.com/pricing?product=stocks |
| Alpaca | Algo Trader Plus | 99 | 全 SIP 实时，WebSocket | 授权条款没写清 non-display，需确认 | https://alpaca.markets/data |
| Nasdaq Basic | 经 Nasdaq Data Link | 未公开 | 交易所自营的 SIP 替代，统一月费无 display 费 | 天然适配 non-display，价格需询价 | https://www.nasdaq.com/products/data/equities/nasdaq-basic |
| Tiingo | Power + Blue Ocean | 约 39 | 隔夜盘（Blue Ocean ATS）top-of-book 与成交，REST 与 WebSocket | 个人档，算法用途未明说 | https://www.tiingo.com/blog/overnight-stock-data-api/ |
| dxFeed | Aggregated Overnight Feed | 未公开 | 聚合 Blue Ocean、Bruce、Moon 三家隔夜 ATS | 未找到 | https://dxfeed.com/dxfeed-launches-aggregated-overnight-market-data-feed/ |

未核实项：dxFeed 零售条款、IBKR 数据订阅（官方页 403）、Tradier 数据单独定价。

## 4. 时段结构

- RTH（13:30 到 20:00 UTC）：现货定价，领先滞后成立。
- 盘前盘后（08:00 到 13:30、20:00 到 00:00 UTC）：SIP 有数据但稀疏。
- 隔夜（Blue Ocean，周日至周四 00:00 到 08:00 UTC，即美东 20:00 到 04:00）：只有 ATS 报价。
- **周五 20:00 UTC 收盘到周日 00:00 UTC 约 52 小时没有任何美股参考价**，而永续 24/7 交易。这段时间领先滞后无从谈起，但跨所分歧最大，属于另一类机会（盘外定价分歧），不在本稿范围。

## 5. 待讨论的问题

1. 是否先租美东机器做一个 RTH 时段的同步测量（Databento DBEQ 或 Massive Advanced，约 199 美元一个月）。
2. 主体身份：以个人名义订阅可用 Massive 的非专业档，一旦以公司或法人跑就必须转商业档。
3. 目标品种：优先 HL xyz 上成交最大的 SNDK、MU、HOOD、SPCX，以及 Lighter Robinhood 实例上零费的 SPY、TSLA、NVDA。
4. 对手方风险：HL xyz 与 Lighter 的做市商是否已经在做同样的事（如果是，过时报价会很少，测量会直接告诉我们）。
5. 与现有方向的优先级：这条线要新钱（数据、机器）和新代码（股价源适配），建议排在 carry 表、结算窗口分析、maker 纸面模拟之后。

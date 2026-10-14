# 五交易所配对交易：实盘与 paper

入口为 `src/opportunity_trade.py`，复用现有扫描器的市场映射、费用来源、数量步长、
盘口新鲜度检查和原生公共行情客户端。`opportunity-trade.live.toml` 使用五所真实执行客户端，
`opportunity-trade.paper.toml` 使用本地模拟成交。原有 scanner 与 episode 入口继续用于观察。

## 实盘启动

在 `E:\persarb\worktrees\opportunity-scan` 执行：

```powershell
$TradePython = 'E:\persarb\worktrees\opportunity-scan\.venv\Scripts\python.exe'

# Validate the five-venue plan without credentials, connections or orders.
& $TradePython -B src\opportunity_trade.py --config config\opportunity-trade.live.toml --dry-run

# Real BTC minimum-size startup checks on all five venues, then BTC/ETH pair trading.
& $TradePython -B src\opportunity_trade.py --config config\opportunity-trade.live.toml --symbols BTC,ETH --execute-live

# Omit --symbols to trade all eligible symbols in the configured registry.
# Optional: stop after startup checks, without enabling the continuous strategy.
& $TradePython -B src\opportunity_trade.py --config config\opportunity-trade.live.toml --symbols BTC --execute-live --self-test-only
```

正常实盘命令先逐所执行 BTC 自检，全部通过才进入连续策略；`--self-test-only` 是可选诊断参数。
自检检查连接、被动挂单、改价、撤单、最小金额 IOC 开仓、减仓平仓和最终空仓，逐项记录耗时。
金额按该所实时最小数量、步长和已发布的最小名义额向上取整，可能高于 1–2 美元；
`startup.max_notional_usd = "50"` 是拒绝超额的外部上限，不是每次自检的下单目标。
Backpack 的最低下单单位来自公开市场 `quantity` 过滤器，见[官方市场接口](https://docs.backpack.exchange/#tag/Markets)。
存在已有 BTC 仓位、非本策略挂单、未知提交或任一检查失败时，不会跳过该所开启正式交易。

| 交易所 | 下单路径 | 启动改价检查 |
|---|---|---|
| Hyperliquid | Native ExecutionClientFactory | 原生改单 |
| Lighter | Native ExecutionClientFactory | 原生改单 |
| Aster | Native ExecutionClientFactory | 确认撤单后新 ID 重挂 |
| Ondo | Native ExecutionClientFactory + continuous config | 确认撤单后新 ID 重挂 |
| Backpack | Native ProductionExecutionClientFactory | 确认撤单后新 ID 重挂 |

五所共用 `LiveNode → Strategy → NativeOrderGateway`，没有 Python REST 下单旁路。
Ondo/Backpack 的原生身份日志与执行状态保存在 `state_path` 同目录的 `live.native/` 内。
Backpack 数据与执行共享同一个公开行情状态、额度对象；程序运行时每 30 秒续期原生执行权限，
只延长有效期，不扩大金额、账户或交易权限。

配置目前为每腿最多 100 美元、双腿总计最多 500 美元、最多 2 对同时持仓；
`risk.max_pairs` 与 `risk.max_gross_notional_usd` 均可调小，500 美元硬上限不能被配置放大。
未完成的开仓会保留两腿预算，直到账户确认平仓后才释放。
`runtime.duration_secs = 0` 持续运行；Ctrl+C 停止新开仓并在配置的清理窗口内尽量减仓。
尚有仓位或未知订单时返回非零状态，并保留账本，不把残仓标记为已关闭。
退出时还核对 Ondo/Backpack 的原生停止证明，包括挂单、持仓、成交确认和停止状态。
结果写入输出与 SQLite 的 `native_shutdown`；证明缺失或不干净时返回非零，下次启动保留阻塞状态。
`startup.timeout_secs = 20` 也用于完整账户读取的等待预算，订单回报仍按
`runtime.order_timeout_secs = 10` 判定超时；原生客户端另有 30 秒关停核对窗口。

## 环境与凭证

必须使用项目源绑定流程安装的 fork wheel，而不是同版本的 PyPI 包；安装记录在
`.native-cache/installed.json`，具体流程见 [native-candidate.md](native-candidate.md)。
实盘启动前检查 Ondo 连续接口、两所完整账户快照接口、Backpack 权限续期接口。
仅有旧 Backpack 只读接口或旧 Ondo 单次执行接口的 wheel 会被拒绝。

本次已安装的 Windows 本地候选基于原生提交 `f0d34e6f8d`，新增代码位于
`E:\persarb\worktrees\opportunity-five-native`，原生本地提交为 `0ed98a0e3f`。
原生主工作树未改动，提交未 push；已安装 wheel 保留构建时的源码快照证明。
它使用 `nextest` 构建配置，供当前工作树测试；wheel SHA-256、源码绑定与测试记录见
[原生候选验收](../reports/opportunity-trading/20261014-native-candidate/README.md)。

凭证只放该工作树的 `.env`，由明确 `--execute-live` 时加载，不放 TOML、命令行或报告：

| 交易所 | 环境变量名 |
|---|---|
| Hyperliquid | `HYPERLIQUID_PK`、`HYPERLIQUID_ACCOUNT_ADDRESS`；vault 账户另配 `HYPERLIQUID_VAULT` |
| Lighter | `LIGHTER_API_SECRET`、`LIGHTER_API_KEY_INDEX`、`LIGHTER_ACCOUNT_INDEX` |
| Aster | `ASTER_MAINNET_USER_ADDRESS`、`ASTER_MAINNET_SIGNER_ADDRESS`、`ASTER_MAINNET_SIGNER_PRIVATE_KEY` |
| Ondo | `ONDO_MAINNET_API_KEY`、`ONDO_MAINNET_API_SECRET`、`ONDO_MAINNET_ACCOUNT_ID` |
| Backpack | `BACKPACK_MAINNET_API_KEY`、`BACKPACK_MAINNET_API_SECRET` |

`--dry-run` 只验证本地配置，不验证账户权限、余额、实际交易所连接或当前标的可交易性。
市场目录是明确的配置快照，新增、退市或映射变更后需更新目录；不会自动把同 ticker 当成同资产。

## 已确认的规则

所有参数集中在 live / paper 配置的 `[trade]`：

| 变量 | 当前值 | 行为 |
|---|---:|---|
| `order_notional_usd` | 100 | 每腿开仓名义金额上限，按数量步长配平，两腿标准标的数量相等 |
| `entry_edge_bps` | 30 | 沿用监控的深度加权净入场价差：扣开仓双腿手续费和扫描配置的 reserve |
| `take_profit_bps` | 5 | 预计完整开平仓净收益达到此值即退出 |
| `max_loss_bps` | 20 | 预计完整开平仓净收益 ≤ −20bp 即触发强平；暂定测试值，等待数据分析 |
| `loss_stop_enabled` | true | 独立亏损止损开关；设为 false 禁用亏损线，保留止盈与两个时间规则 |
| `breakeven_after_secs` | 180 | 满 3 分钟后，净收益严格大于 0 即退出；等于 0 继续等待 |
| `force_close_after_secs` | 300 | 满 5 分钟立即开始持续强制减仓，有多少可成交深度就减多少 |
| `slippage_bps` | 0 | 评估与实盘 IOC 限价附加的不利滑点；paper 同时按此模拟成交 |

开仓时先检查监控口径的 30bp，再检查配置滑点后的净价差仍达标。卖出高价腿与买入低价腿保持等量，
因此实际名义金额会不同；按步长向下取整，保证两边均不超过 100 美元（手续费另计）。
禁用暂定的 20bp 止损时，把 `loss_stop_enabled = false`；`max_loss_bps = "20"` 可以保留，方便以后重新启用。

平仓 bp 的分母固定为该对仓位**开仓买腿成交本金**；live 使用真实成交回报。它不随当前价格、杠杆或保证金变化。
若本金恰为 100 美元，5bp 是 0.05 美元，20bp 是 0.20 美元。

```
gross = opening_short_proceeds - opening_long_cost
        + closing_long_proceeds - closing_short_cost
net = gross - all_four_fill_fees
net_bps = net / opening_long_cost * 10000
```

每个现金额都按实际数量遍历相应买卖盘口，并包含配置的不利滑点。开仓手续费保留开仓费率，
平仓手续费使用当前有效费率。reserve 只是开仓筛选缓冲，不作为实际费用重复扣除。
当前 paper 净收益尚未计入资金费、借币费用或执行延迟，不能据此当作实盘完整净收益。

## 持仓互斥和共享腿

锁为 `(symbol, sorted(venue_A, venue_B))`。同一币种同一组交易所只允许一对仓位，反方向共用该锁。
BTC HL/Aster 与 BTC HL/Lighter 可共存，BTC 与 ETH 也可共存。各对仓位独立记录数量和成本；
关闭某一对不会把另一对共享腿的数量一起平掉。状态同时展示每个 instrument 的 long、short、net 数量。

以下为 paper 成交模型：同一份来源盘口内，模拟成交会扣减可用深度，避免多对仓位重复消费同一档流动性。
新的来源盘口快照提供新的模拟流动性；此模型不模拟自身真实订单对后续市场的冲击。
平仓后须两腿均收到来源时间晚于平仓时刻的行情，才允许该 pair 再开，避免同一报价被反复使用。

## 强平请求与 paper 成交

正常状态为 `open → exit_pending → closed`。5 分钟计时独立于行情更新，运行中的评估定时器每 100ms 检查；
同一次评估依次优先检查时间上限、亏损上限、止盈和 3 分钟后的正收益退出。

满 5 分钟产生 `exit_requested` 后，每条腿**独立**按当前有效深度与数量步长尽量减仓。
不再等待双腿全额可成交，也不再等待盈利。只有一条腿可用时先减该腿，另一条腿保留原有余量并持续重试；
这可能暂时留下单边敞口，终端/状态输出必须按残余数量理解，而不能把它当作已经双腿平仓。
启用的最大亏损线触发后使用同样的强制减仓行为。

减仓单腿要求自身行情新鲜、元数据有效且数量规则与开仓一致；不要求另一条腿同步可用。
每次部分成交记录 `partial_reduction`，并保存每腿残量、累计平仓现金额与手续费。
某一腿无有效报价、无剩余深度、剩余数量不满足步长/最小数量/最小名义额时，对该腿记录阻塞并保留余量，
不会虚记成交。只有两条腿的残余都为零，才记录 `closed`、完整结算净收益并释放 pair 锁。
终端的已实现净收益汇总只计已完成的 pair；未完成 pair 的部分成交现金与费用保存在状态和事件内。

在新鲜盘口下，即便按最有利的盘口第一档估值都已超过亏损线，也会提前锁定止损请求；
第一档估值只用于证明亏损，不用于证明盈利或模拟整笔平仓。

模拟开仓和软退出（5bp 止盈、3 分钟后的正收益退出）要求双腿同步全额成交。
软退出请求若在保存后、成交前中断，恢复时会重新检查收益条件；已不达标则推迟，而不会把止盈请求当成强平。
硬退出支持逐腿部分成交。此模型仍不模拟交易所拒单、未知提交或腿间执行延迟，不能当作真实成交保证。

## Paper 启动和恢复

在 `E:\persarb\worktrees\opportunity-scan` 工作树执行：

```powershell
$TradePython = 'E:\persarb\worktrees\opportunity-scan\.venv\Scripts\python.exe'

# Parse and inspect only; no native import, network, database or output creation.
& $TradePython -B src\opportunity_trade.py --config config\opportunity-trade.paper.toml --dry-run

# Public market data with local paper fills. Set 0 to run until interrupted.
& $TradePython -B src\opportunity_trade.py --config config\opportunity-trade.paper.toml --duration-secs 600
```

`--symbols BTC,ETH` 可缩小测试范围。原生行情需要项目流程安装的 fork wheel；缺少某个 venue 的模块，
或该 venue 的公开接口暂不可用，会使行情启动失败。dry-run 不验证远端接口可用性。
五所测试应使用上述工作树解释器和匹配的源绑定 wheel，不要仅凭版本号相同就替换环境。

默认状态文件是 `reports/opportunity-trading/paper.sqlite3`。`events` 表保存开仓、平仓请求、受阻、
部分减仓、平仓成交和停止事件；`state` 表与事件原子提交，保留未平仓 pair、每腿残量、原开仓时刻与累计净收益。
恢复时不恢复旧盘口，必须重新等有效行情。每个状态文件只允许一个进程占用，进程退出后释放文件锁。

停止运行会保留未平仓模拟仓位，不会为了生成漂亮结果而强行按旧价格结算。
存在未平仓仓位或行情观察器失败时返回非零退出码。同一配置和状态路径重启会继续原仓位和原 5 分钟计时。
修改金额、阈值、币种范围、费用来源或数量规则后，要么用原配置恢复旧账本，要么为新实验指定新的
`state_path`；程序拒绝将不同配置悄悄混入同一实验。不要同时启动两个不同账本并把它们当成同一策略的仓位限制。

## 实盘账本、耗时与恢复

默认实盘账本为 `reports/opportunity-trading/live.sqlite3`。`events` 表中的 `startup_operation`
保存每所每步的开始、完成、接受、首次成交和终态耗时；`order_accepted`、`fill`、`order_terminal`
保存连续交易的对应耗时。`state` 表同时保存启动结果、未完订单原始 ID、各 pair 数量、现金额和手续费。
正式交易只根据真实逐笔成交增加仓位，订单接受不等于成交；一腿失败或不平衡会转入自有数量减仓。

五分钟或启用的 20bp 亏损线触发后，按每条腿的新鲜深度持续发送受价格保护的 IOC 减仓。
部分成交保留余量，未知提交继续查询原 ID，绝不因超时换 ID 重开；只在双腿残量为零且新账户快照
核对通过后释放 pair 锁。共享腿按 pair 子账本归属；若不同 pair 在同一账户相抵，关闭其中一对可能
需要普通 IOC 而非 reduce-only，但数量和总规模仍受策略限制，不使用账户级 close-all。

Ondo/Backpack 使用原生完整账户核对快照，其他三所使用独立只读账户核对。账户时间为读取开始时间，
必须晚于本程序最近的成交/终态；账户默认最多 15 秒旧，盘口仍严格最多 2 秒旧，两种时间不混用。
开仓手续费使用真实成交手续费；退出判断使用当前盘口及预计退出手续费。手续费未知时不触发软止盈，
但保留五分钟减仓。净收益当前不包含资金费和借贷成本，这一口径也在运行结果 `pnl_excludes` 中输出。

同配置重启时，已确认终态的仓位先与账户核对，再沿用原始持仓时间恢复管理。若上次有未知提交、
自检中断、执行账本异常或原生关停证明不完整，程序保留原始 ID 并停在恢复阻塞状态，等待人工核对，不重复启动 BTC 开仓。
仅在配置/凭证校验阶段失败、尚无任何启动意图或订单的空会话，可在修正配置后正常重试首次启动。
不要删除旧 SQLite 或原生日志来绕过残仓检查。修改币种/金额/阈值前先完成当前仓位清理；
不同配置指纹不能复用同一账本，也不要同时运行不同账本去共享同一组交易账户。

离线与本地模拟服务器验证不代表五所主网验收；真实连接、最低金额、手续费等级及权限需由操作者运行自检确认。

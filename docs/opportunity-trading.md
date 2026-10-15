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

# Trade BTC/ETH after readonly startup account checks; BTC trading self-tests are disabled by default.
& $TradePython -B src\opportunity_trade.py --config config\opportunity-trade.live.toml --symbols BTC,ETH --execute-live

# Explicitly run BTC trading self-tests on all five venues before continuous trading.
& $TradePython -B src\opportunity_trade.py --config config\opportunity-trade.live.toml --symbols BTC,ETH --execute-live --startup-self-test

# Omit --symbols to trade all eligible symbols in the configured registry.
# Optional: stop after startup checks, without enabling the continuous strategy.
& $TradePython -B src\opportunity_trade.py --config config\opportunity-trade.live.toml --symbols BTC --execute-live --self-test-only
```

默认配置 `startup.self_test_enabled = false`，启动时只读取行情与账户证据，不发送 BTC 自检交易。
配置为 `true` 或显式传 `--startup-self-test`，才逐所执行 BTC 交易自检，通过后进入连续策略。
`--self-test-only` 也会显式启用自检，通过后直接结束。自检只发生在启动阶段，连续交易每次平仓后直接回到机会扫描。
旧配置 `startup.mandatory` 作为同义布尔开关兼容；两个开关同时存在且不一致时拒绝启动。
关闭交易自检仍保留原生候选校验、逐所只读账户门禁以及每个拟交易品种原有的订单归属与账户核对。
只读门禁使用各所 BTC 映射；没有 BTC 映射时使用该所已配置的交易品种。其结果单独保存在 `account_gate`，不伪造 BTC 自检通过。
启用的交易自检检查连接、被动挂单、改价、撤单、最小金额 IOC 开仓、减仓平仓和最终空仓，逐项记录耗时。
金额按该所实时最小数量、步长和已发布的最小名义额向上取整，可能高于 1–2 美元；
`startup.max_notional_usd = "50"` 是拒绝超额的外部上限，不是每次自检的下单目标。
Backpack 的最低下单单位来自公开市场 `quantity` 过滤器，见[官方市场接口](https://docs.backpack.exchange/#tag/Markets)。
账户证据过期、不权威、净仓不符、存在外部挂单或历史未决提交时，不会跳过该所开启正式交易。

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

### 账户锁、身份绑定与异常恢复（issue 51）

实盘执行先解析账户身份，再获取风险域进程锁、绑定 SQLite 账本、持久登记风险归属，
最后才创建原生客户端。风险域相交的会话互斥：即使只有一个账户重叠，或更换了
`state_path`、工作树、交易品种、预算配置，也不能同时领取新开仓预算。
本机当前 OS 用户的所有工作树共用 `~/.nautilus-perps/account-risk-v1/`；该目录不能随
SQLite 归档删除，不提供通过 TOML/CLI 更改锁目录的选项。

| 交易所 | 风险域与账本身份 |
|---|---|
| Hyperliquid | 环境 + 实际交易地址，忽略地址大小写；vault/subaccount 必须让 `HYPERLIQUID_VAULT` 与 `HYPERLIQUID_ACCOUNT_ADDRESS` 一致。不同实际交易地址独立。普通账户通过 `userRole` 只读查询核对签名者所属用户，agent 地址不作为账户身份。 |
| Lighter | 环境 + `LIGHTER_ACCOUNT_INDEX` 的整数值；不同索引独立，前导零和 API key index 不产生新风险域。账户索引与凭证显式传给原生执行配置。 |
| Aster | 环境 + `ASTER_MAINNET_USER_ADDRESS`；不同用户地址独立，轮换 signer 不产生新风险域。 |
| Ondo | 环境 + `ONDO_MAINNET_ACCOUNT_ID`；不同 venue account ID 独立，API key 轮换不产生新风险域。原生客户端继续核对 expected account ID。 |
| Backpack | 当前所有账户/子账户保守共享一个 mainnet 风险域。账本另绑定 API 公钥摘要，换 key 不继承旧账本，也不能绕过共享锁。 |

Backpack 的[官方账户查询](https://docs.backpack.exchange/#tag/Account)没有可用于空仓账户
稳定身份核对的 user ID；因此本实现不把 key 或未经核验的子账户标签当成独立预算域，
暂不支持 Backpack 不同子账户并发。Hyperliquid 签名者核对使用官方
[`userRole`](https://hyperliquid.gitbook.io/Hyperliquid-docs/for-developers/api/info-endpoint#query-a-users-role)
响应中的用户角色/agent 所属账户；查询失败或不匹配时停止启动。

账本 `ownership`、会话 `account_binding`、实际运行指纹及共享登记只保留身份摘要，
不保存地址、公钥或密钥明文。dry-run 不读取凭证、不创建锁；其指纹明确标为
`configuration_only`，执行时才加入账户绑定。已有账户绑定的账本即使尚未写入第一张
状态快照，也不能用另一个账户打开；没有账户绑定的旧账本拒绝自动认领。

正常退出且应用无残仓/未决订单、原生停止证明完整、状态落盘成功、客户端释放成功后，
共享风险归属才释放。崩溃或强制结束会由 OS 释放进程锁，但不会删除持久风险归属。
恢复时保留原 SQLite、配套 `.native` 目录和共享登记，使用原路径、账户与配置启动。
原未决订单 ID 和两腿预算继续保留；未完成原 ID 核对时仍禁止新开仓，后续自动仅减仓
恢复属于 issue 52。换文件、删文件重建或复制旧账本到另一条路径不能解除该阻塞。
无账户绑定的历史账本需先完成原账户/订单归属核对与清理后再迁移，不能直接加摘要
宣称已验证。锁适用于当前 OS 用户、本机、使用本入口的会话；旧版进程、其他用户或
其他主机需要先停止并完成统一部署，不能将它当作分布式交易所账户锁。

### 原生依赖与凭证配置

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
连续交易这些延迟以订单意图创建时的 `sent_ns` 为基点，包含其后的持久化与派发等待，不能直接当作纯网络往返耗时。
这些记录尚未覆盖行情评估、持久化、派发排队及账户 proof 等所有内部步骤，完整 trace 与分析导出见
[issue 14](https://github.com/mmyyrroonn/Nautilus-Perps/issues/64)。关闭 BTC 自检时没有 `startup_operation` 交易操作事件，只有只读 `startup_account_gate` 结果。
正式交易只根据真实逐笔成交增加仓位，订单接受不等于成交。连续开平仓订单都是 LIMIT IOC、`post_only=false`；
被动 Maker GTC 探针只用于显式启用的 BTC 交易自检。
归一化标的成交量一边大于零、另一边为零时称为单腿；两边均成交但数量不等也属于不平衡。
两腿已确认终态、两边成交量均大于零但不等时进入 `rebalancing`：以较小数量为保留目标，
只减较大腿多出的差额，不补较小腿。减仓部分成交后按剩余差额处理，未决减仓单先核对原 ID；
减仓终态与新账户证明通过后转回持仓管理。保留原始持仓计时、实际现金与费用，以及 pair 锁和预算。
纯单腿（另一边确认零成交）立即进入 `entry_imbalance`，退出全部已确认自有量，不补另一边。
两腿终态且等量部分成交按实际数量管理；两边均为零则核对账户后释放。
差额不满足数量规则或缺少有效执行条件时，保留差额并记录 `entry_rebalance_blocked`，不能向上取整或减较小腿。
原有停止、止损、最长持仓等硬退出仍优先于保留配平仓位；退出过程中的不平衡继续退出，不重新转成持仓。

终态由原生订单关闭状态和成交覆盖共同确认：本地逐笔成交累计量必须等于原生订单的累计成交量。
100ms 定时轮询补查原生缓存，原生事件到达也可直接推进。10 秒订单超时仅标为 unknown 并查询原 ID，
每隔 10 秒可再次查询，不因超时假定订单终态或零成交。账户读取超时为 20 秒，与订单终态及 300 秒最长持仓不同。
另一腿未决或 unknown 时持续查询原 ID，不将其推断成零成交。

[issue 07](https://github.com/mmyyrroonn/Nautilus-Perps/issues/57) 的独立风险限制位于 `[risk]`：
`max_unhedged_notional_usd = "100"`、`max_unhedged_secs = 10`。每个 Pair 的已确认未对冲金额达到
100 美元，或非零净敞口连续达到 10 秒，任一满足就锁定硬退出。`limits.toml` 的
`[opportunity_trade]` 也可收紧这两个值；最终取运行配置、limits 与代码上限的最小值，不能关闭或放大。
它们独立于订单查询间隔、账户读取超时和最长持仓时间。缺省配置也启用这两个上限。

每笔成交及评估定时器都按 Pair 子账本更新 `q_buy - q_sell`、未对冲金额和持续时间；不使用美元腿金额之差，
也不使用共享账户净仓代替 Pair 归属量。金额用绝对数量差乘以较大腿的归一化美元价格；价格取新鲜盘口卖一价
与实际开仓 VWAP 的较大者，盘口缺失或过期时回退到实际开仓 VWAP，持续时间仍继续累计。
部分成交、配平请求、unknown 或方向反转不会重置计时，只有确认数量重新相等才结束本次敞口计时。
到期才到达的成交即使暂时配平，也不能撤销已经触发的退出。

阈值覆盖入场未决、配平减仓未决和不可交易差额；触发后停止新 Pair 入场，退出该 Pair 的全部已确认自有量，
不再保留等量部分。新账户证据须与账本数量一致；仅允许同一 Pair 的原始未决入场单与清理单并存，
未决减仓单、其他 Pair 的未决订单、外部挂单、过期证据或数量不符仍阻塞对应腿。迟到成交继续记入原 Pair 并清理，
所有原单终态、双腿归零且账户核对通过前，保留原 ID、Pair 锁和预算，不因查询超时释放。
10 秒是触发退出的期限，不能保证交易所在期限内完成平仓；缺乏执行证据或可交易数量时仍保留风险并继续核对，
不向上取整扩大数量。未决减仓单不会换 ID 重发，另一条具备完整证据的腿可独立退出。

账本持久化 `unhedged_since_ns`、`unhedged_base`、`unhedged_notional_usd`、`unhedged_duration_ms` 及
`unhedged_breach`；首次超限事件为 `unhedged_limit_breached`，记录触发原因、金额、持续时间和生效阈值。
重启不重置计时或退出状态，存在未决订单仍阻塞自动激活。旧账本使用默认上限时保留配置指纹；旧的不平衡仓位
若缺少风险计时字段，以原 `opened_ns` 保守起算。收紧上限会改变配置指纹，不自动混用不同配置的账本。

五分钟或启用的 20bp 亏损线触发后，按每条腿的新鲜深度持续发送受价格保护的 IOC 减仓。
部分成交保留余量，未知提交继续查询原 ID，绝不因超时换 ID 重开；只在双腿残量为零且新账户快照
核对通过后释放 pair 锁。共享腿按 pair 子账本归属；若不同 pair 在同一账户相抵，关闭其中一对可能
需要普通 IOC 而非 reduce-only，但数量和总规模仍受策略限制，不使用账户级 close-all。

Ondo/Backpack 使用原生完整账户核对快照，其他三所使用独立只读账户核对。账户时间为读取开始时间，
必须晚于本程序最近的成交/终态；账户默认最多 15 秒旧，盘口仍严格最多 2 秒旧，两种时间不混用。
开仓手续费使用真实成交手续费；退出判断使用当前盘口及预计退出手续费。手续费未知时不触发软止盈。
硬退出和已确认入场差额减仓只要求安全执行元数据，不依赖费率或费率来源；金额上限、价格保护、
数量步长、自有数量、账户核对与未决订单门禁仍然生效。倍率、数量规则或已保存的原生价格规则变化时，
对应腿停止发送并保留风险，另一条具备完整执行证据的腿可独立减仓。缺失或无效的费率不会当成零费率。
此变更允许已触发的止损执行；独立于经济估值的止损触发改进仍属于
[issue 09](https://github.com/mmyyrroonn/Nautilus-Perps/issues/59)。

成交手续费缺失、非有限值或币种无法可靠折算时，仍按真实成交更新仓位与现金，但只把已确认手续费
加入费用合计。`pending_commissions` 按原始执行 ID 保存待确认项、原币种/金额和可用的估算；
`fill.fee_usd` 为 `null`，估算单独放在 `estimated_fee_usd`。可用估算仍参与亏损判断，不解锁软止盈。
双腿归零且账户核对通过后可以释放仓位锁，未确认的经济账保存在 `pending_economics` 中并跨重启保留；
其 `net_usd` 为 `null`，仅在每项未知费用都有估算时提供 `provisional_net_usd`。
`realized_net_usd` 只汇总手续费全部确认的已平仓 pair，终端另列待确认 pair 数量。
费用后续核对/结算属于 [issue 12](https://github.com/mmyyrroonn/Nautilus-Perps/issues/62)，本次不自动将估算转为确认。

内部 live 账本升级为 schema 2（外层会话仍为 schema 1），可读取旧 schema 1；旧版已实现总额没有逐对
费用确认依据，因此移到 `legacy_realized_net_usd` 保留，不混入新版已确认收益。旧版未平仓且费用未知的
记录保持待确认，已有现金与数量不重写。升级后的账本不能交给旧版程序读取。
净收益当前不包含资金费和借贷成本，这一口径也在运行结果 `pnl_excludes` 中输出。

同配置重启时，已确认终态的仓位先与账户核对，再沿用原始持仓时间恢复管理。若上次有未知提交、
自检中断、执行账本异常或原生关停证明不完整，程序保留原始 ID 并停在恢复阻塞状态，等待人工核对，不重复启动 BTC 开仓。
仅在配置/凭证校验阶段失败、尚无任何启动意图或订单的空会话，可在修正配置后正常重试首次启动。
无 BTC 自检交易日志、无交易订单和持仓的只读账户门禁，即使读取中断也可以重试。
只读门禁已通过的历史会话按原订单终态与原生关停证明决定能否恢复；每次启动仍取得新的账户证据。
关闭自检开关不能绕过以前中断的 BTC 自检订单或残仓。
不要删除旧 SQLite 或原生日志来绕过残仓检查。修改币种/金额/阈值前先完成当前仓位清理；
不同配置指纹不能复用同一账本，也不要同时运行不同账本去共享同一组交易账户。

离线与本地模拟服务器验证不代表五所主网验收；真实连接、最低金额、手续费等级及权限需由操作者运行自检确认。

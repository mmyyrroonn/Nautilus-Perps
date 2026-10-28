# Ondo BTC / ETH 撤单优先做市

实现日期：2026-10-28。入口为 `scripts/run_ondo_maker.py`，策略为
`src/ondo_maker.py`，原生桥接为 `src/ondo_maker_live.py`。
本地检查结果、二进制身份和旧入口兼容性限制见
[验收记录](../reports/ondo-maker/20261028/acceptance.md)。
这是独立策略，不执行旧 `OndoContinuousStrategy` 的试挂单、试开仓或试平仓。
只复用其节点构造、账户配置和原生执行适配器。

## 交易规则

- 仅 BTC、ETH。参考 Ondo 买一卖一中间价，双边各挂 4.8bp，post-only GTC。
  买价按 tick 向下取整，卖价向上取整；若舍入超过 5bp，则向区间内调整到合法 tick。
  每侧须满足 `3bp < 有向距离 <= 5bp` 且不能吃对手价；没有合法 tick 时仅该侧停挂。
  5bp 是策略维护目标，不代表已核实交易所奖励资格、参考价和分配规则。
- 每侧独立维护：距离小于等于 3bp 或大于 5bp，只撤换该侧；另一侧仍在有效区间时保留。
  该侧旧单确认终态、零仓位且新的原生账户证明与所有剩余订单一致后，才按最新中间价补挂。
  每侧最多一笔未终态做市单；发送撤单、非终态回应或冷却到期都不释放占位。
  波动、断流和账户异常仍撤受影响市场的双边单；成交仍全局撤单清仓。
- 任何成交，包括部分成交，都先发出 BTC/ETH 全部剩余做市单的撤单命令，再发送
  已确认仓位的 reduce-only IOC 平仓。发出撤单后不等待其 ACK 才开始平仓。
  撤单期间新增成交继续累加处理；平仓未完成或结果未知时不再挂做市单。
- 平仓使用最新对手价外 20bp 的保护价格。部分平仓、明确拒绝或 IOC 未成交后，
  按剩余仓位继续尝试；重试间隔默认 250ms。未知结果保留原订单，原生查询/对账确认后再操作。
  保护价和市场流动性意味着不能承诺一次成交；不会因为尝试次数或清仓超时而主动退出。
- 金额按每币、每侧 USD 指定，数量按当前交易所步长向下取整，不自动加大到最低下单金额。
  BTC/ETH 专用会话的原生上限为单笔 500 USD、总敞口 2,000 USD。默认分配总敞口 400 USD；
  四笔做市单名义金额合计不得超出配置。
  包含其他币种的会话仍受单笔 100 USD、总敞口 500 USD 限制。
  每笔 500 USD 的配置必须显式指定 `--btc-usd 500 --eth-usd 500 --gross-usd 2000`，
  并安装包含 BTC/ETH 额度调整的原生 wheel。

## Binance 暂停规则

沿用 `E:\persarb\ondo-rewards\README.md` 的 `vol1s_2bp_5s`：
Binance U 本位永续 BTCUSDT / ETHUSDT `bookTicker` 中间价过去一秒振幅 >=2bp 时，
撤该币双边做市单、暂停五秒；继续触发则延长。只在价格变化时触发，数量更新只刷新健康时间。
窗口保留起点仍有效的价格，使用本机单调时钟，不按交易所时间回排。
重连、无效报价、超过一秒的接收缺口或历史容量溢出后重新积累一秒历史。

默认两币独立；显式 `--pause-both` 可改为任一币触发时两币一起撤。
任何币已成交时则全部停挂，优先撤单清仓。

## 断流与正常停止

- Binance 连接断开立即请求撤单；静默断流默认超过一秒触发。
- Ondo 报价允许静止：原生连接每秒发送心跳，仅在同一连接已确认订阅且收到过有效报价后，
  pong 才刷新行情健康时间。报价时间戳保持原值，不生成虚假报价。
  明确断连立即请求撤单；最后一次有效报价或心跳超过三秒也撤单。
  重连、无效报价或退订会清除旧报价的心跳续期资格，必须重新收到有效报价。
  策略层和原生提交前检查使用一致的心跳健康标准。
- 账户证明不可用、未知订单或身份/所有权异常时撤销做市单，停止新挂单。
  减仓仍通过独立的原生 reduce-only 准入；它需要可用连接、真实持仓和当前行情，不能凭空猜量。
  本地订单动作会正常清除旧完成证明，等待下一轮对账。这个空档仅在私有连接仍为 `trading_ready`、
  且上一份健康证明仍处于原有三秒有效期时保留已有报价；不会延长证明有效期，也不允许新挂单、
  判定退出或据此猜量减仓。真正断流、私有状态异常或证明过期仍撤单。
- SIGINT、SIGTERM、`--seconds` 到期只发起撤单清仓。主循环继续处理行情、订单和仓位，
  仅在缓存与当前原生账户证明一致、零仓位、零活动/未知订单后调用节点停止。
- 本任务不实现操作系统级进程崩溃恢复、独立守护进程或重启接管；由用户单独处理。
  原生已有的订单日志和 DMS 保留。

## 限频改动

旧每秒一次来自 `2026-09-14-ondo-perps-full-integration.md` §4.4 的本地保守默认，
不是查证过的交易所额度。新原生候选取消默认固定节流，同时保留显式 quota 接口。
所有公共/私有 HTTP 响应的 429 都更新同环境共享冷却；支持整数秒 `Retry-After`，
缺少有效秒数时至少等一秒。写请求不自动重放。

待发请求的优先级为取消 > 减仓/查询/对账 > 普通请求，任何优先级都不绕过 429 冷却。
已经发出的请求无法被后来请求抢回。策略最多四个做市单和两个减仓单。
原生配置另有一个滚动秒内的创建/撤单拒绝阈值；新入口使用其类型允许的最大值 `4294967295`，
取消实际可触及的本地请求次数限制，避免撤单被低额度拒绝。它不是发送目标，也不是交易所限额；
活动订单数量、对账确认和明确拒绝后的重试间隔继续限制重复操作。

官方参考：[创建订单](https://docs.ondoperps.xyz/api-reference/orders/create-order)、
[批量撤单](https://docs.ondoperps.xyz/api-reference/orders/batch-cancel-orders)。
2026-10-28 读取的 REST 文档列明 429，但未给出可证实的固定 REST 每秒额度。

## 运行与验证

性能检测默认关闭，启动时显式添加 `--diagnostics` 才开启。关闭时不创建采样器，
跳过诊断计时、样本保存及统计计算。行情心跳、账户健康、风控、撤单和清仓检查始终启用。
Linux 入口也支持运行中切换：`SIGUSR1` 开启、`SIGUSR2` 关闭。部署的控制脚本先核对
服务 PID 和当前版本目录，再发送信号；旧版本未注册这些信号，不应直接向旧进程发送。
关闭会释放采样器，重新开启从新样本开始；连续开启不会清空已有样本。开关不改变交易状态。
开启时每类最多保留 2,048 个完成样本和 64 个待确认订单；关闭程序后样本不持久化。
诊断异常仅禁用诊断，不触发交易停机或影响撤单清仓。每五秒复制一次有界样本，
统计排序、JSON 编码和输出放在后台，最多一个输出任务；输出失败不停止交易。

每五秒状态中的 `diagnostics.maker_ack_latency` 提供：
`submit_call_to_accept_callback`（调用下单到收到接受回调，本机单调时钟）和
`native_submitted_to_accepted`（原生提交与接受事件的本机时间差）。

`diagnostics.cancel_latency` 提供：
`decision_start_to_cancel_call`（开始本轮策略决策到调用撤单）、
`first_cancel_call_to_canceled`（首次调用撤单到收到 `OrderCanceled` 终态回调）、
`decision_start_to_canceled`（前两段之和）。调用时间不等于请求上网线的时间，
计时包含本地下发、网络及交易所处理；不包含行情抵达本机之前的延迟。
重试不重置首次时间，并单列重试次数；拒绝、未确认及完全成交不算撤单成功。
缓存已经终态但没有观察到取消回调的订单单独计数，不冒充取消确认样本。

以上均给出样本数、p50/p95/p99/max 毫秒值，重复确认不重复计数。
`diagnostics.decision_cycle` 只测本地策略决策和调用耗时，不是交易所撤单耗时。
这些是确认回调耗时，不是订单进入或消失于公开盘口的精确时间。
`quote_age_ms` 与 `heartbeat_age_ms` 分开显示，便于区分价格静止和连接失活。

工作目录 `E:\persarb\Nautilus-Perps`。新候选安装在独立 `.venv-ondo-maker`；原 `.venv` 不替换。
下面的 20 USD 是命令示例，不是自动运行金额。两个金额参数均必填。

```powershell
# 仅检查参数，不联网、不读 .env、不下单
.\.venv-ondo-maker\Scripts\python.exe scripts\run_ondo_maker.py --btc-usd 20 --eth-usd 20

# 显式执行；仅此模式读取本仓库 gitignore 的 .env 中 ONDO_MAINNET_* 三项
.\.venv-ondo-maker\Scripts\python.exe scripts\run_ondo_maker.py --btc-usd 20 --eth-usd 20 --live

# 发起正常停止后不设置强制清仓截止
.\.venv-ondo-maker\Scripts\python.exe scripts\run_ondo_maker.py --btc-usd 20 --eth-usd 20 --live --seconds 60

.\.venv-ondo-maker\Scripts\python.exe -m pytest tests\test_ondo_maker.py -q
.\.venv-ondo-maker\Scripts\python.exe scripts\bench_ondo_maker.py reports\ondo-maker\20261028\performance.json
```

原生集成测试 `tests/test_ondo_maker_native.py` 需要与旧 continuous 验收相同的四个
`ONDO_CONTINUOUS_WHEEL*` / `ONDO_CONTINUOUS_PROVENANCE*` 路径与 SHA-256 输入。
测试只使用回环 HTTP/WS、虚构凭据，验证真实安装的原生扩展，不连接主网。
入口检查 `supports_cancel_first_unpaced_rest`，旧 wheel 不能运行新入口。

性能脚本测的是离线 Python 信号和状态机 CPU 耗时，字面滚动扫描仅作为算法对照。
这些数字不是信号至交易所撤单确认的延迟，不能推导主网撤单成功率。

## 单侧奖励区间维护验收（2026-10-28）

在 CPython 3.12.9 的隔离 Linux 网络命名空间中，核心、桥接、诊断和真实原生扩展回环
共 53 项通过。新增场景验证一侧越界后仅替换该侧、保留其余三笔订单，以及单侧撤单期间
成交后撤剩余全部做市单并清仓。旧单终态、新账户证明、剩余订单精确匹配均仍为重挂条件。
Windows 核心和桥接共 35 项通过；Linux 原生 wheel SHA-256 为
`67c87f70db2ebc6ac6947022a5d7f46b5ab5182545ca8577a60d803b1fd0794f`，通过来源校验安装。

同机 30,000 样本离线控制器 p50 从 2.3µs 到 2.6µs，p99 从 4.4µs 到 5.2µs；
新增检查带来约 0.8µs 的 p99 开销。波动窗口峰值跟踪内存约 1.21MB，保持有界。
未测主网端到端延迟、撤换后的奖励变化或实际成交率；没有部署或启停交易服务。
证据位于 `reports/ondo-maker/20261028-improvements/step1-*`。

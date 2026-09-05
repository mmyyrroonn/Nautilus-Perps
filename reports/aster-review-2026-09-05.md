# Aster 新增支持审查报告

审查日期：2026-09-05（Asia/Shanghai）

用途：交给 CC 复核问题、实施修复和补充回归测试。

状态：审查完成；本报告中的问题尚未修复。

## 1. 结论与范围

本轮发现 **12 项需要处理的问题：6 项 P1、6 项 P2**。主要风险集中在订单数量单位、未知执行结果、私有流恢复、对账完整性、撤单范围和探针验收。当前版本不应据此认定为已具备主网实盘条件。

用户确认的范围是 **rc4 之后自己的新增改动**。原有 rc4 代码只用于核对调用链和契约，不将其无关问题列入本报告。

| 仓库 | 路径 | 分支 | 审查基线 | 审查 HEAD |
| --- | --- | --- | --- | --- |
| NautilusTrader | `E:/nautilus_trader` | `aster` | `a0400251110653b6d8ae6a9b5b89c4543fa85a2d`（v2.0.0rc4） | `7bae5cfb57e0db9bf0cdda29f76a3ddc433ca9c1` |
| Nautilus-Perps | `E:/Nautilus-Perps` | `task/aster-support` | `63d9ca7`（main） | `3fd3cab19ba1da53404de12ca96a530ae517b2d2` |

报告生成前再次核对，两仓 HEAD 均未变化，下面的行号对应这些版本。

NautilusTrader 新增提交：

- `3d5b9a3`：Aster 行情适配器及 Binance USD-M 数据路径复用。
- `3b22ded`：Aster Futures V3 EIP-712 执行客户端。
- `7bae5cf`：测试网执行探针示例。

Nautilus-Perps 配套提交：

- `933fb88`：价差观察器支持任意两家交易所及 Aster 配置。
- `affd380`：依赖切换至本地 fork wheel。
- `3fd3cab`：Aster 测试网执行探针。

## 2. 给 CC 的处理说明

1. 先读各仓库规则。NautilusTrader 包括 `AGENTS.md`、`AI_POLICY.md`、`CONTRIBUTING.md` 及相关开发指南；Nautilus-Perps 包括 `CLAUDE.md`、`PROMPT.md`。
2. 每项先独立确认触发条件和框架契约，再修复；如判断某项不成立，请给出具体源码、协议或可运行反例，不要直接照单修改。
3. 优先处理 P1。不得为了通过测试而删除行为、吞掉异常、放宽断言，或向生产代码添加测试专用分支。
4. 金额、价格、数量、手续费使用领域类型或 `Decimal`。需要调整共享 Binance 代码时，确认不会破坏原有 Binance 行为。
5. 两份探针主体相同，修复需要同步检查：`E:/Nautilus-Perps/src/exec_probe.py` 与 `E:/nautilus_trader/crates/adapters/aster/examples/aster_exec_probe.py`。当前示例行号比 Perps 多 11 行，但后续编辑后应重新定位。
6. 本报告是审查材料，不代表执行交易或公开发布的授权。不要读取或输出密钥，不要运行会下单的真实探针；commit、push、PR 和主网操作继续遵守用户授权与仓库规则。
7. 修复交付时逐项列出：结论、改动位置、回归测试及真实结果、剩余限制。不要仅凭原有测试通过就声称已解决本报告中的问题。

P1 表示实盘前应优先处理的高风险缺陷；P2 表示正常功能、数据正确性或验收流程中的缺陷。等级不是发生概率或损失金额的预测。

## 3. 问题清单

### F01 — [P1] 报价币金额被直接当成基础币数量

**位置：** [execution.rs:611](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:611)，重点 611–618 行。

`build_order_request` 不检查 `order.is_quote_quantity()`，直接将 `order.quantity().to_string()` 写入 Aster 的 `quantity`。例如 BTCUSDT 的订单设置 `quote_quantity=true, quantity=100`，原意是买入价值 100 USDT 的 BTC，实际请求却变成数量 100 BTC。是否最终成交取决于交易所限制和账户保证金，但请求本身已经改变了订单含义。

**证据：** 静态调用链确认。`E:/nautilus_trader/crates/risk/src/engine/mod.rs:1525` 只计算 `effective_quantity` 做风险检查，并不修改提交给适配器的原订单，不能依赖上游自动修正。

**修复方向：** 不支持报价币数量时在发送前明确拒绝；如实现转换，必须同步处理精度、价格来源、数量上下限以及本地订单数量语义。

**验收：** 增加 `quote_quantity=true` 的 LIMIT/MARKET 用例，断言原始 100 不会作为 100 基础币发出；拒绝路径不得产生交易所请求。

### F02 — [P1] 执行结果未知被错误终结为拒单

**位置：** [error.rs:117](E:/nautilus_trader/crates/adapters/aster/src/http/error.rs:117)，117–119 行；调用点 [execution.rs:922](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:922)。

`is_venue_rejection()` 对所有结构化 `AsterError` 返回 true，因此 `-1006`、`-1007` 都触发 `OrderRejected`。但 Aster 官方明确这两个错误表示执行结果未知，订单可能已经进入订单簿或成交。错误终结订单会使策略错误重试，并使后续成交恢复更困难。

**证据：** 静态调用链与本轮读取的[官方错误码文档](https://asterdex.github.io/aster-api-website/futures-v3/error-codes/)一致；未通过真实下单制造超时。

**修复方向：** 按协议错误码和执行结果证据分类，保留模糊结果的对账路径，而不是按“是否结构化 JSON”判断是否明确拒绝。

**验收：** 注入结构化 `-1006`、`-1007` 和一个明确拒绝错误，分别断言订单事件；补测超时后交易所实际存在订单或成交的恢复行为，禁止自动重复提交。

### F03 — [P1] 私有流重连后未补偿断线期间的成交

**位置：** [execution.rs:757](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:757)，757–759 行；相关会话循环从 438 行开始。

`Reconnected` 仅打印日志，listen key 到期后重建会话也没有补偿对账。触发序列是：订单已经 Accepted → 私有流断线 → 订单在断线期间成交 → 重连后没有该订单的新事件。本地可能长期保留 Accepted，漏记真实成交与仓位变化。

**证据：** 底层 Binance streams client 只恢复订阅并转发 Reconnected；默认 `open_check_interval_secs` 和 `position_check_interval_secs` 均为 None，已接受订单不再属于等待确认的 inflight 订单。没有共享层自动补齐上述缺口的证据。

同一生命周期还存在启动缺口：`execution.rs:855–858` 启动后台私有流任务后就可标记 connected，并未等待首次 listen key/WS 成功；提交成功响应在 916–920 行也仅记录日志。

**修复方向：** 首次连接等待私有流就绪；重连后恢复订单、成交、余额和持仓基线，明确覆盖缺口期间的交易，正确处理与恢复推送的重叠。

**验收：** 用 mock transport 覆盖断线期间成交、撤单、listen key 失效、首次私有流连接失败、REST 补偿与 WS 重复成交；断言状态和费用不会丢失或重复应用。

### F04 — [P1] 对账源失败仍被报告为完整成功

**位置：** [execution.rs:1243](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:1243)，1243–1247 行；另见 1259–1260 行及历史订单路径 1192–1193 行。

`userTrades` 请求失败被 `continue` 吞掉，单笔成交的必要字段或手续费解析失败也只跳过，最终仍返回 `Ok(reports)`。历史订单请求也存在同样处理。默认 `generate_mass_status` 将这些结果组合成成功快照，不能分辨“没有成交”和“成交查询失败”。

**影响：** 一次超时、429 或坏手续费字段就可能产生缺失历史；已取得的订单/持仓又可能触发不含真实成交费用的推断成交。

**证据：** `E:/nautilus_trader/crates/common/src/clients/execution.rs:350–379` 的默认组合逻辑，以及 ExecutionMassStatus 的默认完整性语义。属于静态错误路径确认，未执行交易所故障注入。

**修复方向：** 必要数据源或必要成交字段失败应传播错误；如确需部分结果，显式维护完整性和时间范围，阻止部分数据触发完整历史才允许的推断。

**验收：** 多 symbol 中一个请求失败、单条手续费无法解析时，不得返回伪完整快照；验证不会据此推断出无真实费用的成交。

### F05 — [P1] 按买卖方向撤单被扩大为交易对全撤

**位置：** [execution.rs:1011](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:1011)，1011–1016 行，实际全撤请求在 1022 行。

适配器明确忽略 `cmd.order_side`，随后调用交易对级全撤接口。若同时存在 BUY 开仓单和 SELL 减仓单，策略只要求撤 BUY，也会撤掉 SELL 退出单。日志和 README 中披露该限制不能改变命令的方向语义。

**证据：** `E:/nautilus_trader/crates/execution/src/engine/mod.rs:2446–2458` 会把带方向的命令传给适配器，上游不会代为完成交易所侧筛选。

**修复方向：** 对符合方向的订单逐笔或批量撤销；无法支持时明确拒绝。无方向请求才能使用交易对全撤。

**验收：** 同一交易对同时存在 BUY/SELL 挂单，分别验证只撤 BUY、只撤 SELL、无方向全撤，另一侧挂单必须保留。

### F06 — [P1] 主网探针绕过风控且没有仓库要求的硬限额

**位置：** [exec_probe.py:447](E:/Nautilus-Perps/src/exec_probe.py:447)；fork 示例对应 [aster_exec_probe.py:458](E:/nautilus_trader/crates/adapters/aster/examples/aster_exec_probe.py:458)。

`ASTER_TESTNET=false` 配合 `--i-know-mainnet` 能启用真实下单，但节点无条件设置 `LiveRiskEngineConfig(bypass=True)`，数量函数也没有最大名义额或总敞口校验。

Nautilus-Perps 的 `CLAUDE.md` 要求主网硬限额，`PROMPT.md` 明确单笔不超过 50 USD、总敞口不超过 100 USD。数量步长或最小数量可能使“最小单”仍然超过上限，例如最小数量 0.001 BTC、价格 100000 时即为 100 USDT。

**证据：** 主网分支、无条件 bypass、数量计算与仓库要求的静态核对；未进行主网运行。50/100 USD 是 Perps 项目要求，不应未经设计直接变成通用适配器的全局限制。

**修复方向：** Perps 在主网发送前落实不可被配置放大的硬限额及账户敞口检查；若这些尚未实现，先禁止探针主网入口。最小订单超过上限时应拒绝，不应为了满足交易所下限提高预算。

**验收：** 离线断言名义额超限、已有敞口超限、重复运行会超限、交易所最小数量超限时均不发送订单。

### F07 — [P2] 历史订单和成交只取第一页

**位置：** [execution.rs:1238](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:1238)，1238–1241 行；历史订单路径 1187–1192 行。

每个 symbol 只请求一次，传 `limit=None`；HTTP helper 也没有 `fromId` / `orderId` 分页参数。[官方接口文档](https://asterdex.github.io/aster-api-website/futures-v3/account%26trades/)说明 allOrders 和 userTrades 默认最多返回 500 条。

**触发条件：** 回溯区间同一交易对超过 500 笔订单或成交。即使每次请求都成功，仍会漏掉其余历史，因此这与 F04 的失败吞错是不同问题。

**修复方向：** 根据官方游标和时间范围限制分页取全；固定截止时间并处理同毫秒多条记录、去重和终止条件。

**验收：** 至少覆盖超过 500 条、多页、跨页相同时间戳、满页末尾、重复返回以及空页终止，不得把截断数据标记为完整。

### F08 — [P2] 余额归零后缓存仍保留旧金额

**位置：** [execution.rs:1048](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:1048)，1048–1051 行；初始快照在 327–329 行有相同过滤。

REST 路径过滤零余额，私有流复用的 `parse_futures_account_update` 也过滤 `wallet_balance == 0`。账户更新采用逐币种覆盖，缺失币种不会自动清零。

**触发条件：** 缓存已有 USDT 100，账户外部转出后余额变为 0。WS 不发送该零余额，随后主动 query_account 也过滤它，缓存继续显示 100。

**证据：** `E:/nautilus_trader/crates/adapters/binance/src/futures/websocket/streams/parse_exec.rs:369–380` 与 `E:/nautilus_trader/crates/model/src/accounts/base.rs:185–188` 的调用契约。

**修复方向：** 保留明确的零余额记录；若需调整共享解析器，增加 Binance 回归覆盖。

**验收：** 非零→零的 WS 更新和 REST 查询都应清除旧金额，多币种情况下其他资产不能被误清零。

### F09 — [P2] 探针把 Binance 默认费率当作 Aster 账户佣金

**位置：** [exec_probe.py:246](E:/Nautilus-Perps/src/exec_probe.py:246)，246–248 行；fork 示例 [aster_exec_probe.py:257](E:/nautilus_trader/crates/adapters/aster/examples/aster_exec_probe.py:257)。

探针直接打印缓存 instrument 的 maker/taker fee。Aster 元数据加载实际使用无凭证的 Binance HTTP 客户端，最终采用 Binance VIP0 默认值 `maker=0.0002`、`taker=0.0005`，并没有调用 Aster 的 `/fapi/v3/commissionRate`。

**证据：** Aster `execution.rs:183–195` 创建无凭证 instrument client；Binance `futures/http/client.rs:1946–1947` 返回默认费率，后续 symbol 费率查询也因无凭证走 fallback。Aster 虽然实现了查询 helper，但探针没有接通它。

**修复方向：** 查询 Aster 账户/交易对真实费率，或明确标为未验证、不可用；不能把 fallback 输出用作账户费率已验证的证据。

**验收：** mock commissionRate 返回一组不同于 Binance 默认值的费率，断言探针输出来自该响应；失败时不输出默认值冒充账户值。

### F10 — [P2] IOC 终态处理遗漏取消，又过早接受部分成交

**位置：** [exec_probe.py:288](E:/Nautilus-Perps/src/exec_probe.py:288)，288–289 行；相关部分成交处理在 301–302 行。fork 示例对应 299–300、312–313 行。

默认 `treat_expired_as_canceled=True`，零成交 IOC 的 EXPIRED 会变为 OrderCanceled，但回调只处理 resting order。另一路径收到第一笔 IOC fill 就进入 complete，即使那只是部分成交，也没有等待后续成交或剩余数量取消。

**本轮离线复现：** 安装 wheel 的默认值为 True；向源码提取的回调传入 IOC canceled 假事件，结果为 `phase=ioc_submitted, callbacks=[]`。部分成交路径已用假事件核对，会直接进入 done。

**修复方向：** 以 IOC 订单真实终态驱动后续步骤，正确处理零成交取消、部分成交后取消、完全成交以及明确失败，记录累计成交和全部费用。

**验收：** 上述各事件序列均恰好结束一次；部分成交阶段不能提前宣布验收完成。

### F11 — [P2] 探针打印完成后节点仍继续运行

**位置：** [exec_probe.py:251](E:/Nautilus-Perps/src/exec_probe.py:251)，251–253 行；fork 示例 [aster_exec_probe.py:262](E:/nautilus_trader/crates/adapters/aster/examples/aster_exec_probe.py:262)。

`self.stop()` 只停止 Strategy，不会关闭 LiveNode，`main()` 中 `node.run()` 仍阻塞。拒单和本地拒绝路径也使用相同方式。

**本轮离线复现：** 无客户端、无凭证的节点在启动后 100 ms 调用 strategy.stop；1 秒时观测到 `stopped=True, returned=False`。随后 watchdog 调用 handle.stop，node.run 才返回。没有连接交易所。

**修复方向：** 通过框架支持的系统关闭请求或 node handle 结束节点，保留成功/失败结果；避免在生命周期尚未完成时递归触发不合法状态转换。

**验收：** 正常完成、明确拒单、本地拒绝均能在有界时间内退出，失败不能返回成功结果，不能依靠人工 Ctrl+C 结束。

### F12 — [P2] 半价挂单破坏最小名义额计算

**位置：** [exec_probe.py:195](E:/Nautilus-Perps/src/exec_probe.py:195)，195–197 行；fork 示例 [aster_exec_probe.py:206](E:/nautilus_trader/crates/adapters/aster/examples/aster_exec_probe.py:206)。

数量按 bid 计算以满足 5 USDT，实际限价却是 bid×0.5，且 `_probe_quantity` 忽略 `instrument.min_notional`。CLI 允许选择 instrument，不能依赖某一个 BTC 合约的较大数量步长恰好遮住问题。

**本轮离线复现：** 假合约 bid=100000、最小数量和数量步长均为 0.00001，源码算出 quantity=0.00005、limit=50000，实际名义额为 2.5 USDT，低于声称保证的 5 USDT。这是数值函数反例，不是声称某个线上合约当前具有这些参数。

**修复方向：** 使用最终舍入后的实际限价和合约实际最小名义额计算数量，再执行最大名义额检查；下限与预算上限冲突时拒绝测试。

**验收：** 精细数量步长、不同 min_notional、价格取整以及上下限冲突均有覆盖，断言实际 `quantity×limit_price` 满足允许范围。

## 4. 本轮已执行的验证

以下结果来自审查期间；生成本报告时仅再次核对 HEAD、定位和工作区状态，没有把同一批测试重新运行一遍。

| 验证 | 结果 |
| --- | --- |
| Aster crate 现有 lib 单元测试 | 146 通过，0 失败 |
| Binance symbol 相关 lib 测试 | 23 通过，0 失败 |
| Binance common parse 相关 lib 测试 | 75 通过，0 失败 |
| Binance config 相关 lib 测试 | 19 通过，0 失败 |
| 合计 Rust 定向测试 | 263 通过；不是全仓测试结果 |
| Perps venv 导入 Aster 配置和工厂 | 通过 |
| Aster runtime `__all__` 与当前源码 stub | 8 个导出匹配 |
| 无连接 Aster 数据节点构建 | 通过；没有运行或连接节点 |
| Perps 两个 Python 文件语法编译 | 通过 |
| IOC canceled、部分成交、停止语义、挂单名义额 | 进行了上述离线复现 |
| 两仓审查范围的 `git diff --check` | 通过 |

实际 Rust 测试命令如下，分别在 `E:/nautilus_trader` 执行：

```powershell
cargo --config 'build.warnings="warn"' test -p nautilus-aster --lib --offline
cargo --config 'build.warnings="warn"' test -p nautilus-binance --lib common::symbol::tests --offline
cargo --config 'build.warnings="warn"' test -p nautilus-binance --lib common::parse::tests --offline -- --quiet
cargo --config 'build.warnings="warn"' test -p nautilus-binance --lib config::tests --offline -- --quiet
```

原始 `cargo test -p nautilus-aster --lib --offline` 被 Windows MSVC 链接器的“正在创建库/对象”输出触发的 `linker_messages` warning 阻断，因为仓库配置 `build.warnings="deny"`。上述命令只临时将 Cargo 对 warning 的策略改为 warn，未修改仓库配置、测试或源码。应区分“测试断言全部通过”与“原始严格构建命令无警告通过”。

沙箱内 Perps venv launcher 最初无法启动其 uv 基础解释器；获准在沙箱外执行离线导入后成功。此现象不能据此判定项目 venv 损坏。

## 5. 验证边界与修复后验收

- 本轮没有读取 `.env`、签名或向交易所提交/撤销订单，没有使用真实账户制造错误。
- 未运行 `make format`、`make pre-commit`、`make pre-flight` 或全仓测试；本轮是只读源码审查，没有准备 PR。
- 没有确认具体账户真实手续费、真实交易所端到端执行、重连恢复或仓位对账已经通过。
- 签名参数顺序、EIP-712 域和表单传输方式未发现可确认的额外缺陷；这不等于真实账户端到端签名验收已完成。
- 原有 263 项测试通过，不等于上述新增回归场景已经覆盖。特别是状态机、故障恢复、分页和费用来源需要新增行为测试。
- 没有把临时怀疑但已排除的“availableBalance 大于 walletBalance 导致解析失败”列为问题；共享 helper 存在 clamp 行为。
- `spread_watch.py` 的 Aster 固定费率仍有代码中已注明的未验证假设。本轮没有账户证据将其转为已验证成本；不要用 F09 的默认费率输出来消除这个不确定性。

建议修复顺序：先 F01/F02/F05/F06 的命令含义和交易范围，再 F03/F04/F07/F08 的恢复与账务，最后处理探针 F09–F12，并同步两份副本。顺序可以调整，但探针的“complete”不应替代订单、成交、余额及持仓的真实验收。

准备更新 wheel 时，应先完成适配器定向测试和 Python 边界检查，再按项目构建流程生成、安装至 Perps 的明确 venv，核对实际导入路径和导出。真实 testnet/mainnet 测试按后续用户授权执行，不能由本报告推断授权。

如后续准备提交 PR，按 NautilusTrader 仓库规则执行 `make format`、`make pre-commit` 和所有相关测试；Python 导出改变时按生成流程更新 stubs。不要手改生成文件或将 CI 用作本地验证的替代。

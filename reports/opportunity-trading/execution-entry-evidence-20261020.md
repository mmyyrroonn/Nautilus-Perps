# 入场发送前复核与执行证据（2026-10-20）

对应 `docs/execution-reliability-plan-20261019.md` 问题二。本轮完成离线实现与回归，未部署、未读取凭据、未发起实盘请求。

## 行为

- `opportunity_entry_evidence.py` 在协调器逐腿发送前，用该次回调墙钟观察加本地单调时钟耗时复核两腿；因此持久化与第一腿同步发送耗时也计入第二腿的盘口年龄。
- 沿用持久化订单的数量、价格和 IOC 身份。复核最新盘口源/接收年龄、时差、可执行深度、原限价内深度、原生数量规则及当前滑点边界。最新手续费与 reserve、最低入场净收益重新计算，净收益按原持久化限价的最坏成交金额估计。
- 盘口过期、深度不足、价格越出原限价、最新费用使净收益不足等情况在发送前拦截尚未发出的两腿；已经跨过发送边界的第一腿保留原身份与未决状态，后续恢复/减仓沿用原有流程。
- 发送前同步复核问题三的单腿风险余量：按原限价金额与最新 canonical ask 估值取较高者，预算为订单上限与预留 10 bps 后单腿硬上限的较小者。即使原卖单仍可执行，ask 跳升导致余量不足也阻止发送。
- 未增加滑点、放宽年龄/费用/净收益门禁或重新计算持久化订单价格与数量。

## 诊断

复用 `--timing` 的 `TimingRecorder`，报告增加 `entry_execution`。关闭计时仍执行发送前复核，但不保留时序明细。

- 每笔订单通过进程内 request ID 关联，输出随机 `trace_id`；输出不包含 request/client order ID、账户载荷或原始拒单文本。
- 每进程最多保留 1024 笔 trace，每笔最多 32 个阶段，记录淘汰数量。明细字段由白名单控制；已有延迟直方图继续使用最多 1024 个最近样本并报告样本数及 p50/p95/p99。
- 阶段覆盖候选、持久化完成、协调器获取、发送前检查、Python 提交、Python 原生回调接收以及策略消费。候选/提交计时复用原测点；另外按持久化到协调器、协调器到提交、回调到策略消费分段统计。
- `monotonic_ns` 为本进程 `perf_counter_ns`；`wall_ns` 与行情时间为 Unix 墙钟。只能在同一时钟域内求差。发送前年龄使用回调墙钟观察及本地单调耗时推进；它不声称测得原生网络写出时间。
- native 收到、签名、网络写出、native 回报接收均显式列为缺失；Python 回调接收不等同于 native 网络接收。
- 原生提交延迟按 accepted、rejected、partial_fill、full_fill 等结果分组；部分成交与最终全成不会合并到拒单或 accepted 均值中。
- 拒因仅输出固定分类：IOC 无即时匹配、限流、余额/保证金、订单规则、post-only、其他场内拒绝和本地校验。拒因分类不改变交易生命周期。
- 两腿都有终态后，记录双边全成、双边部分成交、单腿成交、均无成交的数量，并给出双边完成率与单腿率；晚到经济证据可以更正分类而不会重复增加分母。统计仅包括本进程同时保留两腿 trace 的配对，不把恢复或缺失的订单作为完整样本。

## 验证

命令使用项目已安装的 Python，在本隔离工作树运行，无网络、凭据和交易依赖：

```powershell
E:/persarb/Nautilus-Perps/.venv/Scripts/python.exe -m pytest -q tests/test_opportunity_entry_evidence.py tests/test_opportunity_timing.py tests/test_opportunity_pipeline_timing.py tests/test_opportunity_live_runner.py tests/test_opportunity_live.py tests/test_opportunity_native_execution.py tests/test_opportunity_wakeup.py tests/test_opportunity_account_reconciliation.py
```

结果：**261 passed**。

新增用例覆盖发送前过期、深度不足、费用导致净收益不足、价格变化、盘口时差，均不发单；有效候选保持持久化先于发送，原价格、数量与手续费约束一致；第一腿同步发送后等待使第二腿过期时，不会把已发送第一腿误记为本地未发单。另覆盖时序/字段有界与脱敏、拒因分类、accepted/partial/full/rejected 分层，以及配对统计幂等和晚到证据重分类。

## 限制

这是离线回放和回归证据；没有新的真实行情与成功/拒单分布，不能判断历史约 1.1 秒最大耗时实际属于排队、传输或交易所处理，不能从新测点反推 2026-10-19 四笔 HL 拒单当时缺失的盘口或网络状态。那四笔只能确认 IOC 无即时匹配。

实际延迟分位数与双边完成率需要部署后在既有交易限制内自然积累样本；本轮没有为采样制造额外订单，也没有宣称完成实盘性能验收。不同市场负载的窗口不可直接归因比较。原生测点补齐前仍只能得到 Python 可观察边界。

## 发送前执行与账户状态复核补充

主复核补齐了队列等待及第一腿同步回调之后的全局门禁：非 active、stopping、cleanup 不可用、entry pause、halt、待订单核对、单腿风险突破和未知/超时订单均阻止尚未发送的入场腿。连接断开和停牌继续使用 Observer 撤销盘口或 `BookSnapshot.valid=False` 的既有证据。

发送前重新检查账户年龄、权限、连接、完整账户持仓/挂单归属及活动后 fence。自己已持久化并 release 的 entry ID 可以作为本配对的已知 pending；准备订单本身产生的 fence 用订单新保存的 `preparation_account_fence_ns` 回看之前的真实活动边界。只有本配对零成交且仍未决的 entry 可以使用此例外；真实成交、终态、外部订单、之前活动 fence 及过期账户证据仍会阻止发送。

补充回归包含准备后断连、停牌、halt、核对、停止、暂停、账户过期，以及第一腿同步 accepted 后出现硬保护和第一腿真实成交后的账户失效。最新定向回归结果：**289 passed, 4 deselected**；4 个待新 LighterExecutionControl wheel 的 native 构建用例显式排除，未视作通过。

# 成功候选路径剖析与优化

本轮按用户要求继续代码与 PR，实盘 halt 由另一个 Agent 处理；未部署或清除 halt。

## 分段计时

- `entry.qualified_pricing`：只记录精确定价产生候选的检查，失败的普通组合不混入分布。
- `entry.executable_bounds` / `entry.account_checks` / `entry.prepare_orders`：精确执行边界、动态账户核验、订单构造。
- `strategy.persist_intents` / `strategy.persist_dispatch_release`：分别观测意图准备与释放的持久化。
- `store.serialize` / `store.sqlite_transaction`：JSON 编码与 SQLite 事务（包括 FULL commit）。
- `gateway.construct_order` / `gateway.submit_handoff`：原生订单对象构建与 Python 到 native 的调用。
  后者仍不等于签名或网络写出时间；签名、写出及 native 精确入队时间未暴露，本轮没有伪造这些阶段的数字。

以上复用固定标签、1024 个最近样本的计时器，不把账户/订单内容写入诊断。
候选未通过后续账户检查时仍可能进入相应候选阶段统计；最终持久化的候选另见 `entry.candidate_to_persisted_intents`。

## 有证据的计算优化

合成成功候选包含两所、每所 160 个配置市场，另外每所 474 个完整账户响应中的未配置市场，所有数量精确。
先用 cProfile 确认完整账户数量转换和预期数量计算是提交前主要 CPU 成本；磁盘 FULL 提交另占较大比例。

对每个配置账户 scope 只保存一份不可变账户数量元组的 Fraction 转换结果。换新 payload 即换版；
同一时间戳不同数量不会命中旧结果。每次仍核验新鲜度、连接/权威状态、活动栅栏、当前持仓与订单归属。
预期持仓查询仍逐市场执行，只把其初始零值改为共享的不可变 Fraction，避免重复构造。
缓存不进入持久化、不缓存准入结论。

序列化移至 SQLite 写事务之前；事件和状态仍在同一事务，继续 `PRAGMA synchronous=FULL`。
准备意图和释放意图的两次提交均保留，失败仍回滚并阻断下单。

## 离线结果

`scripts/profile_entry_candidate.py` 可以指定保存的 `--source-dir` 基线；使用真实临时 SQLite 文件，不构造网关。
三组交替基线/候选，每组 30 个成功候选、禁用 cProfile、启用正常计时，分位数的组间中位数：

| 成功入场到持久化释放订单 | 基线 | 候选 |
| --- | ---: | ---: |
| p50 | 12.46 ms | 11.57 ms |
| p95 | 13.07 ms | 12.16 ms |
| p99 | 13.27 ms | 12.32 ms |

180 个案例的持久化状态哈希一致；每个案例实际准备两条订单意图，准备/释放状态与数据库一致。
这是 Windows 本机磁盘与合成账户的离线结果，分位数样本有限，不是生产收益承诺。
带 cProfile 的另一次 20 案例剖析中，两所账户核验累计从约 6.4 ms/候选降至约 3.8 ms/候选；
其包含 profiler 开销，不与无 profiler 数字混用。

针对性回归覆盖缓存换版、同时间戳改数量、账户失联、过期、持仓变化、外部订单、持久化故障及原子回滚。
证据目录：`E:/persarb/_tmp/entry-latency-followup-20261019/`。

原生轮询中重复经济检查仍是待进一步分段实测的热点；未用节流或删除 durable ACK 来冒险削减成本。

最终审查另补两处边界：已经过期的一次性唤醒无法取消时，不阻断网关关闭；
队列汇总只接受至少两个不同采样时刻，防止重复输入伪造窗口。
另在本机已安装的真实 native LiveNode（无任何数据/执行客户端）验证一次性提醒异步到达、重复合并及到达后重新安排，测试通过。

最终完整 opportunity 回归：1054 项通过，1 项因已安装 Windows wheel 缺少
`LiveNodeHandle.metrics_snapshot` 显式排除；该项在第一轮已用未修改基线复现失败。
上述真实原生唤醒测试另行运行 1 项通过。完整回归出现 2 条原生对象跨线程析构警告
（`PyBackpackProductionControl`、`PyStrategy`），未抑制，原始记录见 `all-opportunity-final.xml`。
最后一次定价回放核对 90 帧、1800 次定价、1800 次扫描和 306 条模拟提交意图，全部与原基线一致。

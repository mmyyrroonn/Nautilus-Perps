# 入场延迟第一轮：Python 测量与重复校验优化

## 状态与范围

本地候选已实现并完成离线对比；**尚未部署，尚未完成 20～30 分钟新入场未暂停的实盘验收**。
本轮围绕用户提出的第 1、2 项：Python 可观测链路的有界 telemetry，以及不可变盘口/元数据重复校验。
评估周期仍为 100 ms，未改变交易限制、账户门禁、订单持久化或 native 队列调度。

[上一轮报告](vultr-eu-entry-pricing-20261019.md)中的约 40 微秒是单次入场检查平均耗时；
76～100 ms 是行情到 Python 的交付年龄。它们与发布/评估的 100 ms 周期属于不同阶段。

## 实现

- `BookSnapshot` 的结构、档位排序、正价格/数量、覆盖深度等结果，在首次实际需要时校验并缓存在该不可变对象上。
  `MarketMetadata` 的执行规则和费用有效性同样按对象版本缓存。新快照或 `dataclasses.replace` 不继承旧缓存；
  缓存不是 dataclass 字段，不进入比较、证据或账本，也没有保存历史快照的全局缓存。
- `_book_valid` 每次仍核对当前有效标记、时间顺序、行情/接收年龄；配对仍核对时间偏差。
  live 账户、连接、仓位、待处理订单、限额和发送前门禁保留原路径。
- 将现有精确最佳报价上界提到 `PaperTradeState.entry` 配对准备之前；live 复用该定价器。
  原始最佳报价也是纸面扣量后盘口的乐观上界，只能提前拒绝，不能建立可成交证明。
  负门槛继续走原精确定价路径，费用、深度、数量及精确阈值不采用浮点近似。
- 既有 timing 行补 `recent_p99_ms`。新增 `market.delivery_age`、
  `market.first_update_to_publish`、`market.latest_update_to_publish`、`market.publish_to_evaluate`，
  按场所/线程每 127 次回调或发布周期采样；每行保留最近 1024 个完成样本。
  同一发布周期的首次更新不会被高频末次更新覆盖；两个更新等待使用同一个发布终点。
- 对通过后续门禁的候选增加 `entry.candidate_to_persisted_intents` 和逐腿的
  `entry.candidate_to_submit`，涵盖持久化、协调器等待及提交前处理。
  `gateway.submit_to_outcome` 单独衡量 Python 提交后首次观察到的各类结果，重复结果不重复计数，
  本地提交前拒绝不伪造提交样本。候选和提交标记各有 1024 上限；终止、丢弃/发送及关闭时释放相应标记。
  容量淘汰只影响诊断，不丢弃订单或回执。

**测量边界仍有两个缺口**：native 未暴露准确入队时间，`ts_init`→回调只能叫交付年龄；
Python `submit_order` 是命令交接，不能叫网络写出。签名、native 发送排队和真实网络写出还没有单独分段。
完整采样口径、时钟和统计限制见[诊断说明](../../docs/memory-diagnostics.md#pipeline-latency-boundaries)。

## 相同输入验证

使用修改前的 8 份源码快照和最终候选，分别在独立 Python 进程中执行同一份输入。
新增可重复脚本 [compare_entry_pricing.py](../../scripts/compare_entry_pricing.py)；
这是**合成行情回放**，不是录制的生产行情。

- 90 组输入，覆盖无利润、小正价差、可成交候选、深度不足、仅数量/深档价格变化、排序/数量非法、
  失效/陈旧/未来时戳、配对偏差、费用/步长/乘数/最小数量/最小名义额换版、元数据身份错误、
  账户陈旧/断连/外部订单、纸面扣减与仅接收时间变化、负/零门槛及精确边界。
- **1,800 次入场定价、1,800 次扫描判断均一致**；逐组比较完整返回证据的规范化哈希。
- live 状态与订单意图的规范化哈希逐组一致；产生 **588 个非空定价结果、306 个模拟 submit 意图**。
  脚本不构造执行网关、不向交易所发送订单；模拟意图数不是实际成交数。
- 输入规范化 SHA-256：`7c2245a4d1bc9228cfd97ef1ada6380c517f3cc8ecd4d7a8e0b8b35b1cf9a74d`。

## 本地基准

Windows CPython 3.12.9。五所同一标的，每轮 20 个有向组合，HL 每侧 5 档，其余每侧 20 档。
每轮重新构造并安装 5 份快照和元数据，**包含新对象第一次校验与构造成本**，没有只测缓存命中。
每个模式先预热，7 组、每组 100 轮；新旧进程顺序交替，表中为组中位数。

| 场景 / 计时开关 | 修改前 ms / 20 方向 | 候选 ms / 20 方向 | 比值 |
| --- | ---: | ---: | ---: |
| 无利润 / 关 | 0.7443 | 0.1270 | 5.86× |
| 小正价差、未达标 / 关 | 0.7352 | 0.1175 | 6.25× |
| 包含达标候选 / 关 | 4.4191 | 3.4574 | 1.28× |
| 无利润 / 开 | 0.7828 | 0.1282 | 6.10× |
| 小正价差、未达标 / 开 | 0.7731 | 0.1200 | 6.44× |
| 包含达标候选 / 开 | 4.5325 | 3.5413 | 1.28× |

这些是定价器加快照准备的批次耗时，**不是完整 live `_entry`、行情到决策或决策到网络发送的延迟**。
达标候选场景还包含不成立的反向组合；没有将其均值解释为单个成功下单的耗时。
计时开启覆盖该基准所调用的定价计时，未覆盖 native 回调和网关 telemetry 的全部运行开销。
`pricing.prepare_pair` 的调用集合已经变小，不能只比较优化前后该行的均值判断总成本。

## 回归与限制

- 全部 `test_opportunity*.py` 回归：**1,018 passed，1 failed，270.51 秒**。
  唯一失败为 `test_installed_live_node_reports_backlog_during_slow_callbacks`：
  本地已安装 wheel 缺少 `LiveNodeHandle.metrics_snapshot()`，在挂接诊断句柄时拒绝执行。
- 在修改前源码上单独复跑该项，得到相同错误；这是当前本地 native 环境缺口。
  未隐藏失败、放宽接口门禁或替换 wheel。该项真实 native 队列诊断尚未验证。
- 最终补强提交标记容量和共用阶段终点后，相关 **230 项通过，5.80 秒**，
  覆盖新采样、发布/评估周期、网关、live 协调器和交易流程；最终源码相同输入对比再次通过。
- 新测试覆盖结构/元数据缓存换版、新鲜度每次重查、提前过滤不重置纸面流动性、关闭/跳过采样不读时钟、
  分位数、有界标记淘汰、同一周期首次/末次等待、持久化先于发送、回执去重、隐私和诊断不入账。
- `git diff --check` 通过。本轮没有 commit、push、修改 native fork、部署或重启生产服务。
  原有启动生命周期报告改动与 `incidents/` 保留。

## 证据与下一轮

本地证据目录：`E:/persarb/_tmp/entry-latency-round1-20261019/`。

- `baseline/`：修改前 8 份源码。
- `comparison-final/inputs.json`、`replay.json`、`comparison.json`：同输入、逐组哈希、原始基准采样及源码哈希。
- `pytest-opportunity.xml`：全量回归；`pytest-baseline-native.xml`：原版复现环境失败；
  `pytest-final-pipeline.xml`：最终定向回归。

复现离线对比：

```powershell
.\.venv\Scripts\python.exe -B scripts\compare_entry_pricing.py --baseline-dir E:\persarb\_tmp\entry-latency-round1-20261019\baseline --output E:\persarb\_tmp\entry-latency-round1-20261019\comparison-repeat
```

下一步先以现有 100 ms 配置完成候选部署后的 **20～30 分钟连续新入场未暂停窗口**，
比较各阶段分位数和样本量、CPU、队列、RSS及盘口健康。此处还没有新的实盘结果。
精确 native 入队/网络写出边界、真实行情回放及全部成功下单内部阶段也仍属未完成项。
在此基础上再缩小组合重算范围并试验 20 ms/事件触发，详见[优化计划](../../docs/entry-latency-optimization.md)。

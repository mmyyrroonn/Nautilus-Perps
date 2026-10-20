# 逐笔失败与亏损复盘

当前版本在 `opportunity_live` 的既有账本事务中补充诊断字段，离线生成 Markdown 或 JSON 报告。不需要开启 timing，也不增加行情订阅、交易所请求或逐 tick 日志。

## 能回答什么

- 本地下单校验失败的具体分类、原生拒单信息的分类、取消或到期状态。
- 提交异常与回报超时。二者保留为结果未知，不据此判定交易所拒单或未成交。
- 每笔多腿和空腿毛盈亏、开仓与平仓现金流、已确认手续费、净盈亏和退出原因。
- 是否存在单腿成交、未对冲风险退出、本地发送前拦截，以及证据缺口。

拒单只保存固定分类和来源，不保存原始响应、密钥、签名或请求内容。分类来自文字匹配，并非交易所保证的根因；不能识别时明确显示原因未知。交易所原始数字错误码目前没有统一提取。

## 使用

在 `E:\persarb\Nautilus-Perps` 中执行：

```powershell
.venv\Scripts\python.exe src/opportunity_review.py reports/opportunity-trading/live.sqlite3 --output reports/opportunity-trading/trade-review.md
```

要求账本已停止写入，或传入一致的离线备份。工具先取得现有 owner lock，再以 SQLite 只读模式打开；如果交易程序持有锁则直接拒绝分析，避免读取锁阻塞实盘写入。不应直接复制正在写入的 SQLite 主文件来制作备份。

指定某次会话的某笔交易，输出包含订单编号与时间线的 JSON：

```powershell
.venv\Scripts\python.exe src/opportunity_review.py reports/opportunity-trading/live.sqlite3 --session <session_id> --position 1 --format json --output reports/opportunity-trading/trade-review.json
```

`session_id + position_id` 标识一笔交易；不同运行会话中的 `position_id` 可能重复。旧事件优先从当前快照和归档事件关联会话，无法证明时使用 `legacy-segment-N`，不把多次运行混为一笔。

报告保留事实与证据缺口。旧账本缺失的错误详情或盈亏分项不会被补造。新增字段在运行更新后的 Python 代码后开始记录；该改动本身不会重启或部署交易服务。

## 性能边界

- 原有订单准备、成交、终态和平仓事件增加少量字段，沿用原事务；没有新增同步数据库事务。
- 使用已有订单、盘口时间、成交现金流及平仓经济数据，不新增全量盘口复制或在线归因计算。
- 离线工具不进入交易进程，也不对运行中的账本做轮询。
- 没有引入新的诊断队列或线程，避免与既有可靠账本重复维护另一条写入链路。原有持久化失败的保护行为不变。
- 事件体积和序列化工作略有增加，不能承诺零开销；基准脚本与实测数据位于 `reports/opportunity-trading/trade-review-20261020/`。

## 不能据此断定的事

- 资金费和借贷成本仍不在策略净盈亏中。
- 手续费证据不完整时，净盈亏显示未确认；估计值只在 JSON 的 `provisional_net_usd` 中单独展示。
- 入场预期来自保守限价，不能直接当作成交滑点基准；盘口时间是准备订单时的观测，不代表实际网络发送时间。
- 回报延时含本地准备与处理，不等于网络 RTT。报告不会将高延迟、抢单或盘口变化臆断为亏损根因。
- 当前范围是正式交易阶段；启动自检及未生成订单的普通候选过滤仍使用原有记录。

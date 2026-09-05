# Aster 支持第二轮代码审查

审查日期：2026-09-05。对象：CC 在上一轮审查后新增的提交。

**结论：上一轮多数直接反例已修复，但当前版本仍有 8 项可操作问题（3 个 P1、5 个 P2），不能关闭“全部修复、无待办”。** 本轮新增 5 个 Rust 回归和 3 个 Python 场景，均在对应断言处失败；下文区分真实执行引擎验证、模拟交易所验证与探针事件替身验证。

## 1. 审查范围

| 仓库 | 分支 | 上轮审查 HEAD | 本轮审查 HEAD | 新增提交 |
|---|---|---|---|---|
| E:/nautilus_trader | aster | 7bae5cfb57e0db9bf0cdda29f76a3ddc433ca9c1 | e60923eefc5c287925c87a16a879f8da18bca097 | 5aed57e、08c21e8、bc1e65a、e60923e |
| E:/Nautilus-Perps | task/aster-support | 3fd3cab19ba1da53404de12ca96a530ae517b2d2 | cad10a796039871cdfd381483e51fd15db3abc87 | 73b0a10、9ca0ef5、cad10a7 |

范围仍是 rc4 之后用户新增的 Aster 支持和对应 Perps 更新。本轮重点审查上述增量，并回查其与框架执行引擎、行情缓存和策略退出流程的交互。两个 Perps 文档提交作为修复说明核对，不把说明中的通过声明当作独立验证结果。

本轮未修改生产源码或现有测试，未提交、推送、访问账户凭证或执行交易。临时 Rust 测试文件运行后已删除，复现代码保存在本报告同目录。用户已有的其他未跟踪文件保持原样。

## 2. 问题清单

### R2-01 [P1] 重连先推断成交，后到的真实成交被拒绝

定位：[execution.rs:773](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:773)，773–776；具体状态发送点：[execution.rs:833](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:833)，833–836。

触发：订单在私有流断线期间成交，随后恢复连接。当前 `compensate()` 先执行 `compensate_orders()`，发送只有累计成交量的订单状态，随后才执行 `compensate_fills()`。

执行引擎收到前一条裸状态后，立即推断缺失成交并更新订单。真实 userTrades 随后进入引擎时，会触发超量成交保护，被拒绝。这不是“日志里是否出现了成交报告”的问题，而是引擎实际保存的成交身份和费用错误。

复现：模拟交易所返回真实 trade ID `7001`、手续费 `0.02 USDT`。按实际事件顺序送入真正的 `ExecutionEngine` 后，数量为 `0.010`，但订单只保留一个 UUID 推断成交号，真实成交号不存在，`commissions={}`。

修复方向：在发布能触发成交推断的状态前补齐真实成交，以 `OrderWithFills` 等框架支持的联合报告交付；不能只交换两个独立异步消息后假设时序安全。测试应断言引擎内的数量、真实 trade ID、费用及重复补偿后的幂等性。

现有 `test_outage_compensation_applies_a_fill_and_a_cancel_missed_by_the_stream` 只检查报告通道，无法发现这一错误。对应旧 F03：首次 WS 就绪已修，恢复后的经济状态尚未正确。

### R2-02 [P1] 丢掉窗口内真实成交，却把报告标记为完整

定位：[execution.rs:2695](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:2695)，2695–2697；完整性声明：[execution.rs:2755](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:2755)。

触发：GTC 两分钟前创建，一秒前成交，启动对账只回溯一分钟。`allOrders` 的创建时间过滤排除该订单，`userTrades` 仍返回窗口内成交。新增逻辑把未匹配到订单的成交直接丢弃，最后仍设置 `reports_complete=true`。

复现结果：`complete=true, orders=0, fills=[]`，尽管模拟交易所明确返回了窗口内真实成交 `920101`。这会使启动历史不完整，无法还原对应订单、成交与费用。

修复方向：根据成交的 venue order ID 补取所需订单，建立关联；无法补齐时正确标记不完整并保留可恢复信息。不能通过丢弃有效成交来消除对账告警。[框架完整性契约](E:/nautilus_trader/docs/developer_guide/adapters.md:818)明确要求全部必要记录解析、映射并关联后才可声明完整。

当前历史对账测试反而断言孤立成交应被删除，需要加入“窗口前创建、窗口内成交”的真实业务场景。这个问题来自本轮启动对账修复。

### R2-03 [P1] HTTP 503 携带 JSON 错误体时仍会错误拒单

定位：[error.rs:166](E:/nautilus_trader/crates/adapters/aster/src/http/error.rs:166)，166–170；错误解析：[client.rs:398](E:/nautilus_trader/crates/adapters/aster/src/http/client.rs:398)，398–403。

触发：POST order 收到 HTTP 503，错误体是可解析的 `{"code":-1000,"msg":"An unknown error occured while processing the request."}`。

错误解析先转换为 `AsterError`，丢失 HTTP status；后续只有 -1006/-1007 被认为是未知结果，-1000 会进入明确拒单分支。官方明确规定 HTTP 503 的执行结果未知，可能已经成功。[Aster HTTP 返回码](https://asterdex.github.io/aster-api-website/futures-v3/general-info/#http-return-codes)

复现：模拟交易所在查询接口保留该订单为 NEW，而 POST 返回上述 503。当前客户端发出 `OrderRejected(reason="submit-order-error: Aster error -1000: ...")`，没有走未知结果查询流程。

修复方向：保留 HTTP status 与结构化错误码共同参与分类；503 应进入查询确认流程，不重发提交，也不直接终结订单。原 F02 的 -1006/-1007 反例已修，但整个未知结果分类尚未关闭。

### R2-04 [P2] 报告失败也会消耗成交去重记录

定位：[execution.rs:2616](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:2616)，2616–2620；恢复过滤点为 881–884。

触发：同一次 userTrades 响应先有合法成交 A，再有手续费无法解析的成交 B。A 解析后立即 `record_fill`；B 使整个 `generate_fill_reports` 返回 Err，因此引擎根本没有收到 A。下一次重连补偿却把 A 当作已经应用过的成交跳过。

复现：首次报告有合法 `930101` 和坏手续费记录 `930102`，请求按预期失败；修正响应后重连，确认 userTrades 再次请求成功，但补偿事件里仍没有 `930101`。mass-status 在 fills 成功后、positions 查询失败，也可能留下同类副作用。

修复方向：报告构建与“已交付／已应用”的去重状态分开；失败时不提交去重记录，并处理联合报告后续失败或过滤的情况。现有错误测试只含一条坏记录，未覆盖“先有合法记录、后失败”。

对应旧 F04：错误传播的直接修复成立，但失败路径仍污染恢复状态。

### R2-05 [P2] 行情资料刷新会覆盖刚验证的账户费率

定位：[execution.rs:1312](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:1312)，1312–1316；关联 [Binance data.rs:451](E:/nautilus_trader/crates/adapters/binance/src/futures/data.rs:451)。

新 `refresh_commission_rates()` 只在执行客户端 connect 时查询一次真实费率，再发布更新后的 Instrument。但 Aster 行情客户端仍直接复用 Binance 数据客户端；其合约请求／默认每 3600 秒资料刷新会重新从 exchangeInfo 解析 Binance VIP-0 默认费率，并重新写入同一个共享缓存。执行客户端没有在这条更新路径重新合并账户费率。

复现：先连接执行客户端，从 mock commissionRate 得到 taker=`0.000400`；再调用真实数据客户端的 `request_instruments` 并应用返回数据到 Cache，费率变成 `0.0005`。自动刷新走相同解析器，且直接发送 Instrument 事件；该定时路径通过源码核对，未等待一小时实测。

影响：长时间运行或主动请求合约资料后，策略读取的成本数据会悄悄回到占位值，即使启动时已成功验证。

修复方向：合约资料更新必须保留／重新合并账户费率，并保存可判断的来源信息；将行情更新与 execution 的账户费率发布作为一个完整数据生命周期测试。旧 F09 的探针措辞已修，但费率的持续有效性仍有缺口。

### R2-06 [P2] 探针已失败，迟到撤单事件仍会发出新 IOC

定位：[exec_probe.py:570](E:/Nautilus-Perps/src/exec_probe.py:570)，570–580；fork 示例对应 [aster_exec_probe.py:584](E:/nautilus_trader/crates/adapters/aster/examples/aster_exec_probe.py:584)。

`record_failure()` 已通过 `_finish()` 设置 finished 与 done_event，后续事件处理却不检查该状态。若失败后收到 resting order 的迟到 CANCELED 回报，仍调用 `_submit_ioc_order()`。新单可能正好与 watchdog 的节点停止竞争。

复现：沿用仓库 `ProbeUnderTest`，先注入 cancel-rejected，再注入迟到撤单完成。失败时 `finished=True, done=True, orders_sent=1`；迟到事件后 `orders_sent=2, phase=ioc_submitted`。

这是事件序列测试，不声称当前 Aster 适配器会把每一种 HTTP 撤单错误都转换成 cancel-rejected。其他调用 `record_failure` 的回调异常同样存在未封闭的结束状态。

修复方向：结束／失败后禁止进入新的开仓步骤；迟到事件只能更新核对与清理状态。仅在 `_finish()` 中防重入不够。

### R2-07 [P2] watchdog 自动退出没有清理未确认的 GTC

定位：[exec_probe.py:270](E:/Nautilus-Perps/src/exec_probe.py:270)，270–275；[on_stop:439](E:/Nautilus-Perps/src/exec_probe.py:439)，439–443。fork 对应 284–289、453–457。

触发：第一张 GTC 已接受，撤单尚未确认、请求失败或回报丢失，随后 watchdog 超时。watchdog 直接停止节点，而 `on_stop()` 只有日志，没有对本次创建的订单执行有界撤单、查询或遗留状态报告。

框架默认 `manage_stop=False`，不会代为清理：[StrategyConfig:81](E:/nautilus_trader/crates/trading/src/strategy/config.rs:81)。不能把停止客户端当成撤销交易所上的 GTC。

离线复现：模拟已接受但未关闭的订单，使 watchdog 超时，再在主线程调用停止钩子；`resting.is_closed=False`，额外清理撤单请求为 0。该测试替换了 node stop 信号接收端，不涉及实际网络。

修复方向：先冻结新订单，再对本次探针订单做有时限的撤单和状态确认；无法确认时输出明确的遗留订单 ID 与状态，再结束节点。不要扩大为取消账户所有订单，也不要求改变已明确允许的正常 IOC 成交留仓。

### R2-08 [P2] 未验证到 GTC 撤单，也能把整轮探针报为成功

定位：[exec_probe.py:576](E:/Nautilus-Perps/src/exec_probe.py:576)，576–580；fork 对应 [aster_exec_probe.py:590](E:/nautilus_trader/crates/adapters/aster/examples/aster_exec_probe.py:590)。

resting order 如果以 FILLED 或 EXPIRED 结束，当前代码仅 warning，仍继续发 IOC。最终 IOC 结束后，整轮可以输出 `result=ok / exit_code=0`，虽然目标撤单步骤从未成功。

复现：GTC 在撤单竞争中全部成交，随后 IOC 零成交取消。结果为 `orders_sent=2, failures=[], reason=complete, result=ok, exit_code=0`。

修复方向：把 resting leg 的非预期终态记为失败或未完成验收，并停止继续增加仓位；成交事实照常记录，不能把交易所合法的 FILLED 状态解释为探针已验证撤单。

## 3. 上一轮 12 项复核

“原反例已修”只针对上一轮指出的具体问题，不表示对应整个子系统已经无误。

| 旧项 | 本轮结论 |
|---|---|
| F01 quote_quantity 被当 base quantity | 原反例已修：提交前拒绝；相关测试通过。 |
| F02 未知结果被拒单 | -1006/-1007 已修；结构化 HTTP 503 仍失败，见 R2-03。 |
| F03 首次就绪／断线恢复 | 初次 WS 握手等待已修；恢复成交经济数据仍失败，见 R2-01。 |
| F04 失败被返回为成功空报告 | 查询／解析错误传播的原反例已修；见 R2-02、R2-04 的新完整性及副作用问题。 |
| F05 按方向撤单扩大为全撤 | 原反例已修：按 side 过滤并逐单撤销；测试通过。 |
| F06 探针主网入口绕过限制 | 原反例已修：限制为 testnet，risk bypass=False，并有 20 USDT 单笔限制。 |
| F07 单页成交历史截断 | 已加入 limit、游标和时间分片；mock 多页测试通过。未独立验证真实交易所满页首批的选择方向，不能声称实盘分页已完整验证。 |
| F08 显式零余额行被忽略 | 原反例已修：REST／WS 保留零行，账户缓存测试通过。 |
| F09 默认费率假冒账户费率 | 探针已标注来源限制，connect 已查 commissionRate；持续刷新缺口见 R2-05。 |
| F10 IOC 取消／部分成交终态 | 原反例已修；另见 R2-06、R2-08 的探针结束与验收问题。 |
| F11 strategy.stop 后节点不退出 | 原挂死已修：watchdog 使用节点 stop handle；异常退出清理见 R2-07。 |
| F12 半价挂单低于最小名义额 | 原反例已修：按最终价格、数量步长和最小名义额计算，测试通过。 |

## 4. 实际验证

| 验证 | 本轮结果 |
|---|---|
| Aster Rust 单元测试 | 223 passed |
| Aster 模拟交易所集成测试 | 35 passed |
| 受影响 Binance WS client 测试 | 5 passed |
| Perps 原有 Python 单元测试 | 35 passed |
| 新增 Rust 定向回归 | 5 failed，分别对应 R2-01 至 R2-05 |
| 新增 Python 事件／停止钩子场景 | 3 failed，分别对应 R2-06 至 R2-08 |

原有测试命令：

```powershell
Set-Location E:\nautilus_trader
cargo --config 'build.warnings="warn"' test -p nautilus-aster --offline -- --quiet
cargo --config 'build.warnings="warn"' test -p nautilus-binance --lib futures::websocket::streams::client::tests --offline -- --quiet
```

本机 MSVC 的中文“正在创建库”输出被 Cargo 识别为 linker warning，因此只在测试命令中覆盖 `build.warnings`；没有更改仓库配置。这些结果证明测试断言通过，不代表未经覆盖的严格 warning 策略已通过。

Python 原有测试在导入前注入空的 `dotenv.load_dotenv` 后运行，避免导入探针时读取本地 .env。回归脚本同样屏蔽了 dotenv，且只用 mock／测试替身。虚拟环境解释器启动受本机沙箱限制，离线测试经自动审批后执行。

未重跑 CC 报告中的真实 testnet 交易；未把其日志“零 ERROR/WARN”当作经济数据完整的证明；没有跑面向 PR 的全库 format/pre-commit/pre-flight。

## 5. 给 CC 的复现文件与验收要求

- [Rust 回归函数](E:/Nautilus-Perps/reports/aster-review-round2-repro.rs)：复用当前 `exec_client.rs` 的 mock venue 与 helper，仅包含新增函数。
- [Python 回归脚本](E:/Nautilus-Perps/reports/aster-review-round2-repro.py)：直接复用当前 `test_exec_probe.py` 的测试替身。

Rust 复现命令会新建一个临时测试目标，结束后删除该临时文件；不会覆盖原有测试：

```powershell
Set-Location E:\nautilus_trader
$reviewTarget = 'E:\nautilus_trader\crates\adapters\aster\tests\review_round2_repro.rs'
if (Test-Path -LiteralPath $reviewTarget) { throw 'Temporary target already exists' }
$reviewHarness = [IO.File]::ReadAllText('E:\nautilus_trader\crates\adapters\aster\tests\exec_client.rs')
$reviewCases = [IO.File]::ReadAllText('E:\Nautilus-Perps\reports\aster-review-round2-repro.rs')
[IO.File]::WriteAllText($reviewTarget, $reviewHarness + [Environment]::NewLine + $reviewCases, [Text.UTF8Encoding]::new($false))
try {
    cargo --config 'build.warnings="warn"' test -p nautilus-aster --test review_round2_repro review_round2 --offline -- --nocapture --test-threads=1
} finally {
    Remove-Item -LiteralPath $reviewTarget
}
```

Python 复现：

```powershell
Set-Location E:\Nautilus-Perps
& .\.venv\Scripts\python.exe -B .\reports\aster-review-round2-repro.py
```

这些反例是当前 HEAD 的证据。修复时可整理进正式测试，不应为了让现实现通过而删改其业务要求。清理流程如改为异步状态确认，可以调整 Python 测试替身以验证等价行为。

优先修复三个 P1，再处理报告去重和费率刷新；探针三个问题可统一在“停止新单—清理—确认结果—停止节点”的流程中修复。两份探针逻辑须同步。下轮验收至少要检查真实引擎内的 trade IDs、费用、数量、报告完整性及失败后的行为，不能只检查日志没有错误。

# Aster 支持第三轮代码审查

日期：2026-09-05。

**结论：上一轮 8 个直接反例均已通过现有测试，但仍有 4 个剩余问题（1 个 P1、3 个 P2）。** 本轮补充的 3 个 Rust 回归和 1 个 Python 回归均已实际运行，在对应业务断言处失败。建议先修复 R3-01 的成交恢复问题。

## 1. 范围与版本

| 仓库 | 本轮增量 | 审查 HEAD |
|---|---|---|
| E:/nautilus_trader，aster | e60923e → ef9b06f；1 个代码提交 | ef9b06f5fb7d4c62b7318e75e6a55998d83eaadb |
| E:/Nautilus-Perps，task/aster-support | cad10a7 → a148bb6；d36ccca 代码、a148bb6 报告 | a148bb648eeb3bfd6a5ce6eb98b73d20547719af |

延续“rc4 之后用户新增改动”的范围，本轮重点是上一轮后的修复增量及其与执行引擎、数据客户端、Strategy 的交互。未修改生产源码、原有测试或 Git 历史，未执行交易或读取凭证。临时测试目标已删除，复现代码保存在本报告同目录。

## 2. 剩余问题

### R3-01 [P1] 成交查询失败后仍发送裸状态，真实成交经济数据永久丢失

定位：[execution.rs:790](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:790)，790–795。

本轮把正常恢复改为先补真实成交、再补订单状态，这条路径已修复。但 `compensate_fills()` 返回 Err 时，会用空集合继续 `compensate_orders()`。如果订单在断线期间已经成交，后一阶段仍发送仅含累计成交量的 FILLED／PARTIALLY_FILLED 状态，执行引擎据此生成推断成交。

复现经过两次恢复：

1. 模拟交易所中订单已经 FILLED，但第一次 userTrades 返回临时错误；订单查询成功。
2. 第二次恢复时 userTrades 正常，真实成交 `950101` 和手续费 `0.02 USDT` 确实进入报告通道。
3. 按实际顺序送进真正的 ExecutionEngine 后，订单数量为 `0.010`，但真实成交号不存在，只保留 UUID 推断成交号，手续费字典为空。

失败输出：`temporary fill-history failure permanently replaced real trade: ids=[TradeId('<uuid>')], fees={}`。

修复要求：在没有可靠成交覆盖时，不能发布会触发新增成交推断的累计状态。应保留待恢复状态并重试，或使用框架支持的联合报告／延迟交付机制。需要区分“确实没有遗漏成交”与“成交查询失败”，不能都表示为空集合。账户、仓位等独立刷新仍可继续。

对应上一轮 R2-01：正常路径已修，失败路径未关闭。验收应包含 userTrades 暂时失败后恢复，并断言引擎保存的真实 trade ID、费用、数量及重复恢复的幂等性。

### R3-02 [P2] 不完整快照仍把无法应用的成交标成已交付

定位：[execution.rs:2925](E:/nautilus_trader/crates/adapters/aster/src/execution.rs:2925)，2925–2927。

现在无法补取订单的成交会被保留，且快照正确标为 `reports_complete=false`。但函数结束时仍无条件 `commit_delivered_fills(delivered)`，包括没有关联订单的成交。

真正的 ExecutionManager 对既没有缓存订单、又没有 venue_position_id 的 one-way 孤立成交，不会创建成交事件：[manager.rs:1087](E:/nautilus_trader/crates/live/src/execution/manager.rs:1087)。因此“报告函数返回成功”在这里不等于“成交已应用”。下一次重连却因适配器的去重状态而跳过它。

复现：

- 在本次连接后生成成交 `960101`，启动快照补取其订单失败。
- 将包含该成交的 incomplete 快照交给已注册实际客户端的 ExecutionManager／ExecutionEngine，确认对应订单没有建立。
- 恢复订单查询，再次断线重连，确认补偿查询执行；真实成交仍没有补回。

失败输出：`unlinked fill was skipped by startup reconciliation and must remain recoverable after its order becomes available`。

时间条件很重要：该反例的成交发生在当前 session 内，因此本应可由重连补偿恢复；没有把 session 之前的历史当成必须重放的实时成交。

修复要求：未关联、未应用的成交必须保留恢复资格；也不能让其他成功行推进水位后越过这些记录。应保留待恢复记录或使用明确的完整交付边界。直接把“快照返回 Ok”当成交付成功不足以满足引擎契约。

对应上一轮 R2-02／R2-04：正常跨窗口订单补查、整次查询 Err 不污染去重的原反例已修；不完整成功返回的分支仍有问题。

### R3-03 [P2] 全局费率表使不同账户／环境互相覆盖

定位：[fees.rs:49](E:/nautilus_trader/crates/adapters/binance/src/common/fees.rs:49)，49–52；全局表在 66–67，读取方为 [HTTP client.rs:1972](E:/nautilus_trader/crates/adapters/binance/src/futures/http/client.rs:1972)。

新的费率覆盖表按 `(Venue, symbol)` 在整个进程共享，没有账户、HTTP 环境或客户端所有权。独立客户端即使使用不同账户 ID、不同缓存、不同 HTTP 地址，只要 venue 相同，后一个账户就会覆盖前一个账户后续合约请求／刷新返回的费率。

复现使用两个真实 AsterExecutionClient、两个本地模拟交易所和两份 Cache：

- 账户 A 查询得到 taker=`0.000400`。
- 账户 B 查询得到 taker=`0.001000`。
- B 连接后再次请求 A 的 HTTP endpoint，得到的合约 taker 变为 `0.001000`。

断言输出：`first endpoint acquired the second account's fees; left: 0.001000; right: 0.000400`。

主网／测试网默认都使用 ASTER venue；客户端停止也不撤销注册。另一个账户查询失败时清除共享条目，同样可能令仍工作的账户回退默认费率。这些是同一个所有权问题，不另计问题数量。

CC 的回复已承认多账户限制，本轮验证了它会静默污染其他客户端的结果。若当前设计只支持单账户／单环境，至少应显式拒绝冲突配置；否则应使用账户和环境归属明确、由相关客户端共享的费率提供者。仅清空全局表不能解决并行客户端。

对应上一轮 R2-05：单账户刷新覆盖问题已修；新增全局状态引入跨客户端污染。

### R3-04 [P2] 清理异常使探针无法发布完成通知和结果

定位：[exec_probe.py:752](E:/Nautilus-Perps/src/exec_probe.py:752)，752–757；未保护的撤单调用在 668，完成通知在 777。fork 示例同步受影响：[aster_exec_probe.py:766](E:/nautilus_trader/crates/adapters/aster/examples/aster_exec_probe.py:766)，766–771。

`_finish()` 先设置 `_finished=True`，然后未经异常保护调用 `_run_cleanup()`。后者虽然把缓存缺失／查询失败视为待清理状态，但随后调用真实 `Strategy.cancel_order()` 时，该接口会因找不到缓存订单而抛 RuntimeError。

异常会中断后续订单清理，并跳过摘要和 `done_event.set()`；再次调用 `_finish()` 又因 `_finished` 提前返回，因而只能等总 watchdog 超时。

实际复现使用无客户端 LiveNode 注册真实策略，调用真实 native cancel_order；节点没有运行，也没有任何交易连接。注入的是该清理代码明确尝试支持的“已追踪订单 ID，但缓存缺失”状态：

```text
Cleanup escaped: Cannot cancel order: order not found in cache: MISSING-1
finished=True done=False summary=''
leftovers=['MISSING-1=unknown (not in the cache)']
```

修复要求：逐订单捕获本地撤单异常，保留失败／遗留信息并继续处理其余订单；无论清理是否成功，都要产生明确结果并保证终态通知。完成通知与清理成功通知应继续区分。

对应上一轮 R2-07：新增清理和 LEFTOVER 报告有效，但清理自身失败时仍会破坏退出流程。R2-06 的冻结、R2-08 的异常终态判失败已修复。

## 3. 上一轮 8 项关闭状态

| 上轮项 | 本轮结论 |
|---|---|
| R2-01 正常重连推断成交 | 原反例通过；错误分支见 R3-01。 |
| R2-02 删除跨窗口成交并谎报完整 | 已补取所需订单并保留无法关联成交；原反例通过。无法关联后的恢复见 R3-02。 |
| R2-03 503 JSON 错误拒单 | 已保留 HTTP status 参与未知结果分类；原反例通过，未发现本轮该改动的新问题。 |
| R2-04 失败报告提前消费去重 | 原解析失败反例通过；incomplete 成功返回仍有 R3-02。 |
| R2-05 刷新覆盖真实费率 | 单账户原反例通过；新注册表的账户隔离见 R3-03。 |
| R2-06 失败后迟到事件仍发新单 | 冻结与结束后分支已修；原反例通过。 |
| R2-07 停止时缺少清理 | 增加了按本次订单清理、等待及遗留报告；原反例通过，异常路径见 R3-04。 |
| R2-08 没验证到 GTC 撤单却成功 | 非 CANCELED 终态明确失败且不继续 IOC；原反例通过。 |

“原反例通过”只表示具体场景已关闭，不等于对应模块所有失败场景都完成验证。

## 4. 本轮验证与边界

| 实际执行的测试 | 结果 |
|---|---|
| nautilus-aster 单元测试 | 230 passed |
| nautilus-aster 集成测试，含上一轮 5 个 Rust 回归 | 44 passed |
| nautilus-binance 单元测试 | 974 passed |
| nautilus-binance futures 集成测试 | 233 passed |
| nautilus-binance spot 集成测试 | 147 passed |
| Perps test_exec_probe，含上一轮 Python 场景 | 44 passed |
| 本轮 Rust 定向回归 | 3 failed，对应 R3-01／02／03 |
| 本轮 Python native Strategy 回归 | 1 failed，对应 R3-04 |

现有测试合计 **1,672 passed**。两份探针经 diff 核对仅差 14 行版权声明，逻辑一致。

Rust 命令：

```powershell
Set-Location E:\nautilus_trader
cargo --config 'build.warnings="warn"' test -p nautilus-aster --offline -- --quiet
cargo --config 'build.warnings="warn"' test -p nautilus-binance --offline -- --quiet
```

与上一轮相同，本机 MSVC 输出“正在创建库”被 Cargo 识别为 linker warning，所以仅在命令中覆盖 build.warnings；仓库配置未变。Python 导入前注入空 dotenv.load_dotenv，避免读取本地 .env；解释器经自动审批在沙箱外执行离线测试。

本轮未重跑 CC 记录的真实 testnet 交易，未制造真实交易所断线／503，也未执行全仓 format、pre-commit、pre-flight。这些本地结果不证明实盘恢复已验证。

清理测试的实际边界：

- Fake cancel_order 计数不等于原生接口真的发送撤单；真实 Strategy 对 PENDING_CANCEL 会跳过再次取消。
- timeout 路径在 on_stop 中清理，停止后的残余订单事件不再分发到策略回调。因此 leftovers 可能是停止瞬间的快照，不能直接当作节点返回时的最新交易所状态。后续验收宜重新读取已追踪订单缓存，并将本地状态与交易所确认区分。
- 上述两点作为清理测试覆盖边界保留；本轮正式清理问题是已复现的 R3-04。

## 5. 可运行复现与下一步

- [Rust 三个回归函数](E:/Nautilus-Perps/reports/aster-review-round3-repro.rs)
- [Python 清理异常回归](E:/Nautilus-Perps/reports/aster-review-round3-repro.py)

Rust 文件仅包含附加函数，复用当前 exec_client.rs 的测试 harness。以下命令不会覆盖原有测试；临时目标结束后删除：

```powershell
Set-Location E:\nautilus_trader
$reviewTarget = 'E:\nautilus_trader\crates\adapters\aster\tests\review_round3_repro.rs'
if (Test-Path -LiteralPath $reviewTarget) { throw 'Temporary target already exists' }
$reviewHarness = [IO.File]::ReadAllText('E:\nautilus_trader\crates\adapters\aster\tests\exec_client.rs')
$reviewCases = [IO.File]::ReadAllText('E:\Nautilus-Perps\reports\aster-review-round3-repro.rs')
[IO.File]::WriteAllText($reviewTarget, $reviewHarness + [Environment]::NewLine + $reviewCases, [Text.UTF8Encoding]::new($false))
try {
    cargo --config 'build.warnings="warn"' test -p nautilus-aster --test review_round3_repro review_round3 --offline -- --nocapture --test-threads=1
} finally {
    Remove-Item -LiteralPath $reviewTarget
}
```

Python：

```powershell
Set-Location E:\Nautilus-Perps
& .\.venv\Scripts\python.exe -B .\reports\aster-review-round3-repro.py
```

建议先修 R3-01／R3-02 的失败恢复，再处理费率所有权与清理异常。下一轮验收应同时保留原有通过用例和这四个失败场景；对恢复路径检查引擎实际保存的成交与费用，对退出路径检查真实接口抛错后仍能产生结果。

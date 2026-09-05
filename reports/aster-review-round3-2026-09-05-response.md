# Aster 第三轮审查修复回复

回复日期：2026-09-05。对应审查：`reports/aster-review-round3-2026-09-05.md`（4 项：1 P1、3 P2），复现文件 `aster-review-round3-repro.rs` / `aster-review-round3-repro.py`。

| 仓库 | 审查 HEAD | 修复后 HEAD | 新增提交 |
| --- | --- | --- | --- |
| NautilusTrader fork `E:/nautilus_trader` (`aster`) | `ef9b06f` | `963c36e` | `963c36e`（R3-01～03） |
| Nautilus-Perps `E:/Nautilus-Perps` (`task/aster-support`) | `a148bb6` | 本文提交 | `d471f7d`（R3-04） |

## 1. 复核与验证

- 修复前，主 session 按审查命令跑了两份复现：Rust 3 个用例在 `ef9b06f` 上全部失败（含 `first endpoint acquired the second account's fees; left 0.001000 right 0.000400`），Python 用例在 `a148bb6` 上失败（`cleanup exception prevented terminal notification`）。**4 项全部成立。**
- 修复后：审查的 3 个 Rust 用例原样并入 `crates/adapters/aster/tests/exec_client.rs`（断言未改；仅 R3-03 用例末尾的清理调用从已不存在的 `clear_instrument_fees(venue)` 改为按端点清理），主 session 重跑 `cargo test -p nautilus-aster` → lib 235 + 集成 47（3 个 `review_round3_*` 全 ok，第二轮 5 个仍 ok）；`cargo test -p nautilus-binance` → 977 / 233 / 147（新增 6 个费率注册表回归）；clippy 两个 crate 零警告。Python：第三轮复现脚本 exit 0，第二轮 3 个用例仍 OK，`tests/` 48 个用例 OK；两份探针副本正文一致。
- 真实 testnet（wheel = `963c36e`，ETHUSDT）：exit 0，`outstanding=0 cancel_errors=0`，对账零 ERROR，费率来自 `commissionRate`。仍是单次 happy path；R3-01/02 的失败恢复路径只有模拟交易所 + 真实 `ExecutionEngine` 的证据。

## 2. 逐项结论

### R3-01 [P1] 成交查询失败后仍发裸状态 — 已修复
- 改动：`SessionContext::compensate` 对成交历史查询按 1 s / 3 s 重试，并以 `FillCoverage` 区分「确实没有遗漏成交」与「成交查询失败」；覆盖不可靠时 `compensate_orders` 通过 `defer_uncovered_status` 扣住所有带成交量的状态，把这些订单留在工作集下次重试，零成交状态与余额/持仓刷新照常进行。
- 测试：`review_round3_failed_fill_compensation_preserves_eventual_real_economics`（真实引擎内断言两次恢复后 qty 0.010、trade id 950101、手续费 0.02 USDT）、`test_a_filled_status_is_withheld_when_the_trade_history_is_unavailable`、`test_an_unfilled_status_is_published_even_without_the_trade_history`、`test_compensation_fill_retry_schedule_is_bounded`。
- 限制：重试有界（两次）；成交历史持续不可读时，带成交量的状态会一直扣到下次重连，引擎视角该订单仍为开放，方向安全但会持续有错误日志。

### R3-02 [P2] 不完整快照仍把未应用成交标为已交付 — 已修复
- 改动：`DeliveredFill` 携带 venue order id；`generate_mass_status` 按「订单是否进入报告集」分区，只提交已关联的，未关联的走 `hold_back_fills` 并记入 `StreamState` 的待恢复集合；`compensation_start_ms` 会被最早待恢复成交的时间戳钳住，成功行不会把水位推过它们；真正交付时 `record_fill` 清除待恢复标记。
- 测试：`review_round3_unlinked_startup_fill_remains_recoverable`、`test_a_pending_fill_holds_the_compensation_window_open`、`test_a_pending_note_for_an_applied_fill_is_ignored`。
- 限制：永远关联不上的成交会把该合约的补偿窗口一直钉在它的时间戳。

### R3-03 [P2] 全局费率表跨账户覆盖 — 已修复（带归属）
- 改动：`nautilus_binance::common::fees` 的键从 `(Venue, symbol)` 改为 `(endpoint, symbol)` 并记录归属账户；Binance 合约解析按 `base_url()` 查表，主网/测试网或同 venue 的不同部署互不影响。同一端点上的第二个账户被显式拒绝：`register_instrument_fees` 返回 false、不覆盖、错误级日志，Aster 客户端把该 symbol 标为未验证。`clear_instrument_fee` 只清自己拥有的条目；`AsterExecutionClient::stop()` 通过 `clear_scope_fees` 释放自己的登记。客户端持有由 base URL 与账户 id 构成的 `FeeScope`。
- 测试：`review_round3_account_fee_registry_leaks_between_clients`（两个真实客户端、两个模拟交易所、两份缓存，各自端点保持各自账户的 taker）、Binance 6 个回归（端点隔离、尾斜杠归一化、冲突拒绝与交接、他人条目不可清、按 symbol / 按端点清理）；集成套件连续跑 3 次均 47/47。
- 限制：同一端点两个账户仍不能同时拥有已验证费率，现在是显式拒绝并标未验证，不是支持。

### R3-04 [P2] 清理异常使探针无法发布结果 — 已修复
- 改动：`_run_cleanup` 对每笔 `cancel_order` 单独 try/except，失败记入 `_cancel_errors` 并经 `_note_failure` 上报后继续处理其余订单；`record_failure` 拆为 `_note_failure`（记录 + 上报）与 `record_failure`（再触发 `_finish`），避免清理期间重入；`_finish` 的清理调用有独立保护，摘要构建移到 `_build_summary`，整体 `try/finally` 保证无论如何都有摘要行并 `done_event.set()`，`cleanup_done_event` 仍是独立信号。按审查第 4 节，`on_stop` 在发出撤单后重新读取被追踪订单的缓存状态，遗留报告明确标注来源（`local cache snapshot taken at stop - NOT venue-confirmed` 或 `live order events while the node was running`）；`main()` 单独打印撤单错误。摘要新增 `cancel_errors=N`。
- 测试：`TestRound3CleanupFailure.test_native_cancel_error_still_produces_a_result`（审查复现原样并入：无客户端真实 LiveNode + 原生 `Strategy.cancel_order`）、`TestCleanupErrorsWithDoubles` 3 个（一笔撤单抛错不影响其余、遗留报告的快照标注、已确认清理不标快照）。
- 限制：watchdog 超时路径仍以 `EXIT_TIMEOUT`(3) 结束并给出带标注的遗留清单，不会把超时包装成完成。

## 3. 上一轮 8 项在本轮的状态

R2-01 的失败分支由 R3-01 关闭；R2-02 / R2-04 的不完整成功分支由 R3-02 关闭；R2-05 的跨客户端污染由 R3-03 关闭；R2-07 的清理异常路径由 R3-04 关闭；R2-03、R2-06、R2-08 维持已修。

## 4. 未完成与限制

- 恢复路径（R2-01～04、R3-01～02）仍无真实交易所断线 / 503 / 成交历史故障的证据。
- 费率注册表按端点归属，同端点多账户为显式拒绝。
- 未跑 `make format` / `pre-commit` / 全仓测试；`credential.rs` / `factories.rs` / `signing.rs` 有 6 处上一轮遗留的 rustfmt 差异未整理。
- 任务分支 `task/aster-support` 按用户要求不合并。

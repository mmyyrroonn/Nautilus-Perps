# Aster 第二轮审查修复回复

回复日期：2026-09-05。对应审查：`reports/aster-review-round2-2026-09-05.md`（8 项：3 P1、5 P2），复现文件 `aster-review-round2-repro.rs` / `aster-review-round2-repro.py`。

| 仓库 | 审查 HEAD | 修复后 HEAD | 新增提交 |
| --- | --- | --- | --- |
| NautilusTrader fork `E:/nautilus_trader` (`aster`) | `e60923e` | `ef9b06f` | `ef9b06f`（R2-01～05） |
| Nautilus-Perps `E:/Nautilus-Perps` (`task/aster-support`) | `cad10a7` | 本文提交 | `d36ccca`（R2-06～08） |

## 1. 复核与验证

- 修复前，主 session 按审查给的命令亲自跑了两份复现：Rust 5 个用例在 `e60923e` 上全部失败（含 `data request overwrote verified account taker fee: left 0.0005 right 0.000400`），Python 3 个用例在 `cad10a7` 上全部失败。**8 项全部成立。** 上一轮回复中「无待办修复项」的说法撤回。
- 修复后：审查的 5 个 Rust 用例已原样并入 `crates/adapters/aster/tests/exec_client.rs`（断言未改），主 session 重跑 `cargo test -p nautilus-aster` → lib 230 + 集成 44（含 5 个 `review_round2_*` 全部 ok）；`cargo test -p nautilus-binance` → 974 / 233 / 147（新增 4 个费率注册表回归）；clippy 两个 crate 零警告。Python：审查脚本 `aster-review-round2-repro.py` 3 个用例 OK，`tests/` 44 个用例 OK；两份探针副本正文一致。
- 真实 testnet（wheel = `ef9b06f`，ETHUSDT）：exit 0，`outstanding=0`，对账零 ERROR，费率来自 `commissionRate`。仍是单次 happy path，不构成 R2-01～04 恢复路径的实盘证据；这些只有模拟交易所 + 真实 `ExecutionEngine` 的证据。

## 2. 逐项结论

### R2-01 [P1] 重连先推断成交，真实成交被拒 — 已修复
- 改动：`SessionContext::compensate` 先 `compensate_fills` 再 `compensate_orders`，成交以 `send_order_with_fills` 捆绑交付并返回已交付的订单标识，订单遍历跳过它们，不再有裸状态先于真实成交发布。
- 测试：`review_round2_reconnect_preserves_real_trade_economics`（真实引擎内断言 qty 0.010、trade id 7001、手续费 0.02 USDT）、`test_repeated_compensation_does_not_duplicate_a_recovered_fill`、`test_compensation_reports_the_order_together_with_its_fills`。
- 限制：补偿仍在流任务内联执行。

### R2-02 [P1] 丢窗口内成交却标记完整 — 已修复
- 改动：`generate_mass_status` 对无法匹配订单的成交按 venue order id 调 `GET /fapi/v3/order?orderId=`（不受时间过滤）补取并关联，每个缺失订单最多一次；仍关联不上的成交保留并将快照标为 `reports_complete=false`。不再丢弃任何成交。
- 测试：`review_round2_mass_status_keeps_fill_for_order_created_before_window`；原「孤儿成交被丢弃」断言改写为真实场景（窗口前创建、窗口内成交、补取并关联，6 条订单报告、complete=true）；`test_unlinkable_fill_is_kept_and_the_snapshot_declared_incomplete`。
- 限制：不完整快照会让引擎对该合约只做订单投影并打 ERROR，这是诚实信号，优于假完整。

### R2-03 [P1] 503 带 JSON 错误体仍拒单 — 已修复
- 改动：`AsterHttpError::AsterError` 增加 `status: Option<u16>`（非 2xx 时填入）；任何 5xx / 408 无论错误码都判为结果未知；`is_venue_rejection` = 结构化且不含糊。
- 测试：`review_round2_structured_503_must_not_reject_live_order`（交易所保留订单为 NEW，客户端走查询确认，不发 `OrderRejected`）、`test_structured_body_under_a_server_status_is_ambiguous`（6 例）、`test_status_is_rendered_alongside_the_code`。

### R2-04 [P2] 报告失败也消耗去重记录 — 已修复
- 改动：`fetch_fill_reports` 只构建报告并返回待提交的 `DeliveredFill`；`generate_fill_reports` 在整次请求成功后才 `commit_delivered_fills`；`generate_mass_status` 在持仓查询也成功后才提交（审查提到的 mass-status 副作用一并处理）。
- 测试：`review_round2_failed_fill_report_does_not_consume_recovery_trade`。

### R2-05 [P2] 行情刷新覆盖已验证费率 — 已修复（需要改共享 Binance 代码）
- 改动：审查的复现直接构造 `BinanceFuturesDataClient` 并断言其响应，Aster crate 不在该路径上，而费率只能经 Aster 的签名端点取得，所以无法只在 Aster 侧修。在 `nautilus_binance::common::fees` 加了 venue+symbol 费率注册表，在唯一的按 symbol 取费点 `futures_symbol_fees` 查询；未注册时为空，Binance 行为逐字节不变。Aster 执行客户端在 `refresh_commission_rates` 登记每个已验证费率，查询失败时清除该 symbol 的登记，避免继续套用无法确认的费率。
- 测试：`review_round2_data_request_preserves_verified_fees`、`test_a_failed_rate_query_does_not_leave_an_earlier_rate_applied`、Binance `common::fees` 4 个回归；整套集成测试连续跑 5 次均 44/44，确认进程级注册表无跨测试抖动。
- 限制：注册表是进程级，一个进程内两个不同账户的 Aster 执行客户端会互相覆盖（最后写入者生效），已在模块文档注明。

### R2-06 [P2] 失败后迟到撤单事件仍发新 IOC — 已修复
- 改动：`_finish()` 与 `on_stop` 置 `_frozen`；`_submit_resting_order` / `_submit_ioc_order` 冻结时拒绝；`_check_terminal` 增加结束后分支，迟到终态只更新记账与清理，不进入任何发单步骤。
- 测试：`test_failure_prevents_late_cancel_from_submitting_ioc`（审查用例原样）。真实断言：cancel-rejected 后 orders_sent=1，迟到 `OrderCanceled` 后仍为 1。

### R2-07 [P2] watchdog 退出不清理未确认 GTC — 已修复
- 改动：探针创建的订单记入 `_probe_order_ids`；`_run_cleanup` 冻结新单后只对这些订单发撤单（绝不账户级）；`_finish()` 在打印总结前先清理；`on_stop()` 做最后一次清理并把未确认关闭的订单以 `[probe] LEFTOVER: ... id=STATUS` 打到日志与 stderr；watchdog 增加 `cleanup_event` / `CLEANUP_GRACE_SECS = 10`，最多等 10 s 确认后再停节点；`main()` 有遗留则强制非零退出。缓存读取异常按「未知→待清理」处理。
- 测试：`test_timeout_initiates_cleanup_of_unresolved_gtc`（审查用例）、`TestCleanupOnStop` 6 个（不碰同缓存里的外部订单、遗留带状态、已关闭不重复撤、失败清理撤开 GTC 且迟到确认不发新单、watchdog 等待与放弃）。

### R2-08 [P2] 未验证撤单也报成功 — 已修复
- 改动：挂单腿以非 `CANCELED` 终态结束时 `record_failure`（记录成交事实，不发 IOC 腿、不加仓）。
- 测试：`test_filled_gtc_does_not_pass_cancel_probe`（审查用例）。真实断言：`result=failed ... fills=1 ... exit_code=1`，submitted 长度 1。

## 3. 上一轮 12 项在本轮的状态

按审查第 3 节：F02 由 R2-03 关闭，F03 由 R2-01 关闭，F04 由 R2-02 / R2-04 关闭，F09 由 R2-05 关闭，F10 / F11 由 R2-06～08 关闭；其余 6 项维持已修。F07 的真实交易所满页首批方向仍未独立验证（testnet 历史不足 1000 条）。

## 4. 未完成与限制

- R2-01～04 的恢复路径只有模拟交易所 + 真实引擎证据，没有在真实交易所制造断线/503。
- 费率注册表进程级共享的限制见 R2-05。
- 未跑 `make format` / `pre-commit` / 全仓测试；`cargo +nightly fmt --check` 在 `credential.rs` / `factories.rs` / `signing.rs` 有 6 处上一轮遗留的格式差异，未整理。
- 任务分支 `task/aster-support` 按用户要求不合并。

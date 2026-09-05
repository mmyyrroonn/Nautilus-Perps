# Aster 审查报告修复回复

回复日期：2026-09-05。对应审查：`reports/aster-review-2026-09-05.md`（12 项：6 P1、6 P2）。

审查基线 → 修复后版本：

| 仓库 | 审查 HEAD | 修复后 HEAD | 新增提交 |
| --- | --- | --- | --- |
| NautilusTrader fork `E:/nautilus_trader` (`aster`) | `7bae5cf` | `e60923e` | `5aed57e`（未知资产注册 + GET 重试）、`08c21e8`（F01–F08、费率、流就绪）、`bc1e65a`（启动对账与引擎契约对齐）、`e60923e`（平仓行，有界对账可净额） |
| Nautilus-Perps `E:/Nautilus-Perps` (`task/aster-support`) | `3fd3cab` | `73b0a10` | `73b0a10`（探针 F06、F09–F12、cancel API、35 个离线测试、审查报告入库） |

12 项全部确认成立，未打回任何一项。修复之外，两次真实 testnet 运行另外暴露了 4 个审查未覆盖的缺陷，一并列在第 3 节。

## 1. 验证方式说明

- Rust：`cargo test -p nautilus-aster`（lib 223 + 集成 35，集成测试用 axum 模拟交易所，含分页与游标/时间窗互斥）、`cargo test -p nautilus-binance`（970 / 233 / 147，与审查基线相比只多 3 个连接超时回归测试）、`cargo clippy --no-deps ... -D warnings` 两个 crate 均为零警告。主 session 亲自重跑了上述测试，输出与 sub agent 报告一致。
- Python：`python -m unittest discover -s tests` → 35 passed。
- 真实 testnet：`src/exec_probe.py --instrument-id ETHUSDT-PERP.ASTER`，最近一次运行（2026-09-05 04:00 UTC，wheel = `e60923e`）exit 0：GTC 挂单 accepted → 撤单 → `OrderCanceled`（用户流推回）→ IOC 成交 0.003 ETH（手续费 0.00385280 ASTER）→ 费率行（真实 `commissionRate`）→ 节点自停；启动对账零 ERROR、零 `InvalidStateTrigger`，剩余 WARN 只有 testnet 无 NVDAUSDT、availableBalance 钳制提示、探针费率提示、IOC 成交留下的仓位。日志 `logs/stage2_aster_testnet_probe.log`（gitignored，本机保留）。
- 未做：主网任何操作；`make format` / `pre-commit` / 全仓测试；对上游的 PR 准备。

## 2. 逐项结论

### F01 [P1] 报价币金额被当成基础币数量 — 已修复
- 改动：`crates/adapters/aster/src/execution.rs::build_order_request` 首行 `ensure!(!order.is_quote_quantity())`；`submit_order` 走既有 `emit_order_denied` 路径，不构造请求。
- 测试：`test_limit_order_with_quote_quantity_is_rejected_before_any_request`、`test_market_order_with_quote_quantity_is_rejected_before_any_request`、`test_base_quantity_orders_are_still_accepted`；集成 `test_quote_quantity_order_is_denied_without_reaching_the_venue::case_1/2`（断言 `OrderDenied` 且模拟交易所收到 0 次 `POST /fapi/v3/order`）。全部 ok。
- 剩余限制：不做报价币→基础币换算，直接拒绝。

### F02 [P1] 执行结果未知被终结为拒单 — 已修复
- 改动：`src/http/error.rs` 新增 `-1006 / -1007` 常量、`is_execution_status_unknown()`、`is_ambiguous_execution()`；`is_venue_rejection()` 改为「结构化错误且不属于结果未知」。范围比审查建议更宽：5xx / 408、无法解码的响应体、POST 的客户端超时都视为结果未知。`submit_order` 遇到未知结果时保留在途、错误级日志、`SessionContext::resolve_ambiguous_submit` 在 2 s / 5 s / 15 s 后用 `GET /fapi/v3/order?origClientOrderId=` 对账并发出真实状态；只有 `-2013 NO_SUCH_ORDER` 才本地拒单；绝不自动重发。
- 测试：`test_venue_rejection_classification`（12 个错误码）、`test_execution_status_unknown_codes_are_not_rejections::case_1/2`、`test_unexpected_status_ambiguity_follows_the_status_class`（7 例）、`test_undecodable_response_body_is_ambiguous`；集成 `test_unknown_execution_status_is_reconciled_rather_than_rejected::case_1/2`、`test_client_side_timeout_on_submit_is_reconciled_rather_than_rejected`、`test_gateway_failure_on_submit_is_reconciled_rather_than_rejected`、`test_unknown_execution_status_rejects_only_when_the_venue_never_saw_the_order`、`test_definitive_venue_rejection_emits_order_rejected_immediately`。全部 ok。
- 剩余限制：未在真实交易所制造超时。

### F03 [P1] 私有流重连后未补偿断线期间的成交 — 已修复，一处待验证
- 改动：`connect` 内联建立首个 listenKey + WebSocket，失败则 `connect` 失败（不再先标记 connected）；新增 `SessionContext` + `StreamState`（在途订单登记表、按 symbol 的已应用 trade id），在每次会话重建和 `Reconnected` 帧上执行 `compensate()`：open orders 对账（消失的逐笔查询）、分页 `userTrades` 补成交并按 trade id 去重、余额与持仓刷新（含平仓行）；`session_start_ms` 作为补偿下限。用户流连接超时从共享 Binance 客户端的固定 5 s 改为可配置（`ws_connect_timeout_secs`，默认 20），初次连接对传输错误按 1 s / 2 s / 4 s 重试。
- 测试：`test_connect_fails_when_the_first_user_stream_cannot_start`、`test_connect_waits_for_the_user_stream_before_reporting_connected`、`test_outage_compensation_applies_a_fill_and_a_cancel_missed_by_the_stream`、`test_outage_compensation_does_not_reapply_a_fill_already_seen_on_the_stream`、`test_listen_key_expiry_rebuilds_the_session_and_compensates`、`test_compensation_never_reaches_behind_the_session_start`、`test_recorded_fill_is_not_offered_again`、`test_stream_connect_retries_transport_faults` 等；Binance 侧 `test_connect_timeout_defaults_to_the_binance_value` 等 3 个回归。全部 ok。
- 剩余限制：补偿在流任务内联执行，期间帧排队（无界通道，不丢）。冷启动 `InvalidStateTrigger` 告警已在 `bc1e65a` 消除，有界对账 ERROR 已在 `e60923e` 消除（见第 3 节 D、D2）。

### F04 [P1] 对账源失败仍被报告为完整成功 — 已修复
- 改动：`generate_order_status_reports` / `generate_fill_reports` / `generate_position_status_reports` 对请求失败和必要字段解析失败返回 `Err`，不再 `continue` + `Ok`。顺带修了第二个 bug：`AsterPositionRisk::is_flat()` 把解析失败的数量当作平仓，现在先解析再判平。
- 测试：`test_generate_fill_reports_propagates_a_failed_symbol_request`、`test_generate_fill_reports_propagates_an_unparsable_commission`、`test_generate_order_status_reports_propagates_a_failed_request`、`test_generate_position_status_reports_propagates_an_unparsable_quantity`。全部 ok。
- 剩余限制：未加载 instrument 的行仍跳过并记日志（超出范围）。

### F05 [P1] 按方向撤单被扩大为交易对全撤 — 已修复
- 改动：`cancel_all_orders` 只在 `order_side` 为空时用 `allOpenOrders`；有方向时先列 open orders，对匹配方向的订单逐笔 `DELETE`。未用 `batchOrders`，因为 `orderIdList=[…]` 的括号/逗号编码进 EIP-712 签名串尚未验证。
- 测试：`test_cancel_all_orders_with_a_side_cancels_only_that_side::case_1/2`（断言只撤 BUY / 只撤 SELL，且未调用 `allOpenOrders`）、`test_cancel_all_orders_without_a_side_uses_the_symbol_wide_endpoint`、`test_cancel_all_orders_with_a_side_and_no_match_sends_nothing`。全部 ok。

### F06 [P1] 主网探针绕过风控且没有硬限额 — 已修复
- 改动：`src/exec_probe.py` 删除 `--i-know-mainnet`；`resolve_environment()` 只接受 testnet，其它一律 exit 2；`LiveRiskEngineConfig` 改为 `bypass=False` 并按每个已加载 instrument 设 `max_notional_per_order = 20 USDT`（核对 fork `crates/risk/src/engine/mod.rs:1163` 该 map 按 InstrumentId 预交易强制）；探针预算常量只能被配置下调，不能上调。主网执行留给阶段 3 带 `config/limits.toml` 的独立脚本。
- 测试：`TestEnvironmentGuard` 7 个（含 `test_no_mainnet_flag`、`test_probe_budget_cannot_be_widened_by_config`）；真实命令 `ASTER_TESTNET=false` → exit 2。
- 附带事实：testnet BTCUSDT 最小单 0.001 BTC ≈ 79 USDT，超过 20 USDT 上限，探针改用 ETHUSDT（0.003 ETH ≈ 7 USDT）。主网 BTCUSDT 同样与 PROMPT.md 的 50 USD 单笔上限不兼容。

### F07 [P2] 历史订单和成交只取第一页 — 已修复
- 改动：`query_all_orders_paged` / `query_user_trades_paged`：按 7 天切片，每片第一页带时间窗 `limit=1000`，之后只用游标（`orderId` / `fromId`）继续（Aster 拒绝游标与时间窗同传，已对官方文档确认），结束时间在开始时固定，按 id 去重，游标不前进则报错；HTTP 客户端本地拒绝非法组合。
- 测试：`test_fill_reports_page_past_the_venue_limit_and_deduplicate`（2500 行、跨页同毫秒、重复行）、`test_order_status_reports_page_past_the_venue_limit`（1200 行）、`test_fill_report_pagination_stops_on_an_empty_history`、`test_all_orders_rejects_cursor_combined_with_a_time_window`、`test_user_trades_rejects_cursor_combined_with_a_time_window`。全部 ok。
- 剩余限制：超过 7 天的窗口只在模拟交易所上跑过。

### F08 [P2] 余额归零后缓存保留旧金额 — 已修复
- 改动：`parse_account_balances` 保留所有行（含显式零）；`AsterBalance::is_zero` 把解析失败视为未知而非零；WS `ACCOUNT_UPDATE` 由 Aster crate 自行解析 `B` 数组（`parse_aster_account_update`），不再依赖会丢零余额的共享 Binance 解析器，Binance 未改。
- 测试：`test_parse_account_balances_keeps_explicit_zero_rows`、`test_rest_balance_transition_to_zero_clears_the_cached_amount`、`test_stream_balance_transition_to_zero_clears_the_cached_amount`、`test_stream_account_update_registers_unknown_assets`、`test_unparsable_balance_is_unknown_rather_than_zero`；集成 `test_rest_zero_balance_clears_the_cached_amount`、`test_stream_zero_balance_clears_the_cached_amount`。全部 ok。

### F09 [P2] 探针把 Binance 默认费率当作 Aster 佣金 — 已修复（两侧）
- 适配器：`refresh_commission_rates` 在 connect 加载 instrument 后用签名客户端逐个查 `commissionRate`，改写 maker/taker 并通过 `DataEvent::Instrument` 重新发布；失败则保留默认值并在告警里标 UNVERIFIED。测试 `test_connect_publishes_instruments_with_the_account_commission_rates`、`test_commission_rate_failure_leaves_the_venue_default_unpublished`、`test_with_commission_rates_replaces_the_venue_defaults`（用 testnet 真实值 0.000050/0.000400，与 Binance 默认 0.0002/0.0005 区分）。
- 探针：`fee_line()` 明确标注来源，遇到恰好等于 Binance 默认值时追加 WARNING。
- 真实结果：修复前探针打印 `maker_fee=0.0002 taker_fee=0.0005`（默认值）；修复后打印 `BTCUSDT-PERP.ASTER maker_fee=0.000050 taker_fee=0.000400`，与我用 eth-account 手工签名调用 `commissionRate` 得到的一致。NVDAUSDT 在 testnet 不存在，费率只能在主网只读查询确认。

### F10 [P2] IOC 终态处理 — 已修复
- 改动：`_check_terminal` 以 `cache.order(id).is_closed` 驱动，累计成交与手续费，`_finish` 幂等；拒单/拒绝/撤单被拒都算失败。核对 fork `crates/execution/src/engine/mod.rs:2941-2951`：引擎先更新缓存订单再发布事件，回调时缓存已是终态。
- 测试：`TestIocTerminalHandling` 8 个（零成交撤单、零成交过期、部分成交后撤、全成、拒单、denied、cancel-rejected、未终态不推进、重复终态事件不重复结束）。真实运行：`IOC order terminal: status=FILLED filled_qty=0.003/0.003`。

### F11 [P2] 探针打印完成后节点仍运行 — 已修复
- 改动：`start_stop_watchdog()` 守护线程在 `done_event` 或 `--timeout-secs`（默认 180）时调用 `node.handle().stop`；`main()` 返回 0 / 1 / 2 / 3（成功 / 失败 / 拒绝启动 / 超时）；另加整节点连接重试（仅在未发出任何订单时重试 3 次）。
- 测试：`TestWatchdog` 3 个。真实运行：`Received stop signal from handle` → 节点在结果打印后约 60 ms 内退出，exit 0。

### F12 [P2] 半价挂单破坏最小名义额 — 已修复
- 改动：`plan_order()` 先按 `price_increment` 舍入限价，再按舍入后价格对 `min_notional`（缺省 5 USDT）、`min_quantity`、`size_increment` 取量，断言 `qty × price ≥ min_notional`，超过 20 USDT 上限则拒绝；IOC 腿按 ask 同样处理。
- 测试：`TestPlanOrder` 9 个，含审查反例（bid 100000、步长/最小量 0.00001 → 旧算法 0.00005 @ 50000 = 2.5 USDT，新算法 0.0001 @ 50000 = 5.0 USDT）。真实运行：`qty=0.005 price=1225.20 notional=6.126 USDT`。

## 3. 真实 testnet 运行额外发现（审查未覆盖）

- **A. 未知资产 panic**（已修，`5aed57e`）：账户余额含 `ASTER`、`AFEE`，`Currency::from` 直接 panic 整个节点。现在未知资产动态注册为 8 位精度 crypto currency；ACCOUNT_UPDATE 与手续费资产同样处理。testnet 手续费实际以 ASTER 代币结算，已被正确记账。
- **B. 探针撤单 API 用错**（已修，`73b0a10`）：v2 `Strategy.cancel_order` 接受 `ClientOrderId`，探针传了订单对象；回调里的 TypeError 被框架静默吞掉，订单一直挂在交易所。现所有 `on_*` 回调都有异常守卫。
- **C. 同步回调重入**（已修，`73b0a10`）：`OrderPendingCancel` 在 `Strategy.cancel_order` 内部同步发出，此时策略在 Rust 侧处于可变借用状态，回调里调 `self.log` 抛 `RuntimeError: Already mutably borrowed`；Rust 日志器还会整条丢弃多行消息。探针在该回调里只计数，失败记录先写 stderr。
- **D. 冷启动对账告警**（已修，`bc1e65a`）：根因是引擎 `ExecutionManager::reconcile_execution_mass_status` 在应用前按 `ts_event` 排序事件，要求订单的 `ts_accepted` 不晚于它自己的成交；Aster 的部分 `allOrders` 行缺 `time` 字段，适配器回落到「现在」，历史成交因此排到接受之前而被拒。适配器现在在 `generate_mass_status` 里把 `ts_accepted` 对齐到该订单最早成交、单行内 `ts_accepted ≤ ts_last`、声明报告窗口、丢弃无对应订单的孤儿成交并告警。集成测试 `test_startup_reconciliation_of_historical_fills_is_accepted_by_the_engine` 走真实 `ExecutionManager + ExecutionEngine` 回放 5 笔历史成交订单，断言零 `InvalidStateTrigger`。真实 testnet 冷启动（wheel = `bc1e65a`）：`InvalidStateTrigger` 计数 0。
- **D2. 有界对账 ERROR**（已修，`e60923e`）：引擎的有界对账（`ExecutionManager::order_only_venue_order_ids`，仅在声明报告窗口后启用）把窗口内成交净额与持仓报告比较，期望值只从持仓报告取；Aster 的 `positionRisk` 省略平仓合约，适配器又跳过了平仓行，于是期望值缺失、所有历史订单被降级投影并打 ERROR。现在 `generate_position_status_reports` 上报 Flat 行，`generate_mass_status` 为窗口内有成交但无持仓行的合约补一条平仓行。引擎级测试 `test_flat_account_with_pre_session_history_reconciles_without_complaint` 回放该账户（5 笔 IOC 买 + 2 笔手工减仓卖，`positionRisk` 为空），断言无任何对账 WARN/ERROR。真实 testnet 冷启动（wheel = `e60923e`）：对账零 ERROR/WARN。已知残余风险：若交易所返回同一毫秒内交错的两笔成交，或减仓单回放时净额为零，引擎仍会告警，这取决于交易所时间戳，出现时把对应 `allOrders`/`userTrades` 行加进 fixture 即可定位。
- **E. 环境**：本机 DNS 解析到 198.18.x.x（代理 fake-ip），对 Aster 主网/测试网的 TLS 握手中断与连接超时频繁；每次节点启动拉两次 exchangeInfo（数据 + 执行客户端），Aster 对该端点限流，连续运行需间隔。

## 4. 未完成与后续

- 审查 12 项与实测发现 A–D2 均已修复并经真实 testnet 复验；无待办修复项。
- 主网阶段前仍需：NVDAUSDT 真实费率（主网只读 `commissionRate`）、`config/limits.toml` 与硬编码限额的阶段 3 脚本、`make format` / `pre-commit`（若准备向上游提 PR）。
- 任务分支 `task/aster-support` 按用户要求暂不合并。

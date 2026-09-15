# Ondo Perps 继续实现计划

> 交给 DeepSeek 的执行说明：先阅读本计划、关联 Review 和两个仓库的实际规则；按指定阶段实施。每个任务先补能揭示问题的回归，再修改代码，再运行对应检查。不要把辅助函数存在、单测数量、socket connected 或一次成功返回当成阶段完成。当前窗口只编写计划，不实施。

**目标：** 在现有 Ondo 原生适配器与录制分析代码上，补齐安全入口、状态一致性、私有生命周期、账户报告和应用 probe，完成可复现的分层交付；默认不启用实盘。

**架构：** 保留 Rust `nautilus-ondo` 和 Python 原生 factory；REST/WS 的恢复与事件归并由一个账户运行时 owner 管理；公开行情和私有账户 socket 分离。应用负责可追溯录制、按到达顺序回放和有限时 probe，不另建一套生产签名/下单实现。

**范围依据：** [2026-09-15 Review](../../../reports/ondo-code-review-2026-09-15.md)、[用户阶段报告](../../../reports/ondo-stage-status-2026-09-15.md)、[原完整方案](2026-09-14-ondo-perps-full-integration.md)。本计划覆盖继续实现的先后顺序、缺陷修正和验收；原方案的协议口径仍适用，但冲突处以本次明确修正为准。

## 0. 基线、规则与执行边界

| 名称 | 路径和本次基线 |
|---|---|
| APP | `E:\Nautilus-Perps`，`e49716b7ec8bc16648aa0f2c3e1450c8d73284e0` |
| FORK | `E:\nautilus_trader`，`5c2ba5aedfe35625a1787d0468d6885771aa2e51`，分支 `onde-perps` |
| ADAPTER | `FORK\crates\adapters\ondo` |
| 当前测试 | APP：659 tests + 97 subtests；ADAPTER：临时放宽 Windows build warnings 后 583 tests + 1 doc-test；不是实盘验收 |
| 当前产物 | 已安装 dist-p3 wheel；项目依赖仍指向旧 target/wheels；已有 ONDO–HL 历史公开录制 |

- [ ] 实施开始重新记录两个 HEAD、status、当前 wheel 路径/SHA256、命令环境；若代码已前进，先判断 F01–F18 哪些已修，避免覆盖后续工作。
- [ ] 读取 APP `CLAUDE.md`、FORK `AGENTS.md` / `AI_POLICY.md` 及目标目录规则。不要照抄阶段报告中未在实际规则出现的分支要求。隔离实现改动，保留既有 untracked 和 `.pyi` 换行差异；不自动清理、提交、合并或删除分支。
- [ ] 所有数量、金额、费率、净仓位使用项目既有精确 Decimal/Quantity 语义；不能经 float 兜底。费用、增量、状态、时间缺失均保留 unknown。
- [ ] production 公开行情允许只读模式；production 签名写入继续无条件拒绝，包括配置 `allow_production_orders=true` 的组合。拒绝必须发生在构建可发请求前。
- [ ] 默认不开启执行。paper/mock 不连接真实写接口；sandbox 写入只能由明确选择的 sandbox 模式触发，并有额度、品种、订单数和超时边界。
- [ ] 账户只读模式不订阅会产生撤单副作用的 DMS，不因“同步账户”自动执行撤单/下单。
- [ ] 每阶段生成 `reports/ondo-acceptance/<UTC-run-id>-rN/`：两个 SHA、dirty 范围、命令/退出码、回归证据、未解决项、下一阶段入口。账号信息、secret、签名/登录帧不得进入报告、公开 raw tape 或 Git。

本计划不授权主网交易、资金转移或常驻部署。协议未验证部分不伪造通过；不依赖这些部分的离线工作持续完成。

## 1. 阶段与依赖

| 阶段 | 交付 | 覆盖 Review | 依赖 / 完成条件 |
|---|---|---|---|
| R0 | 执行入口与未知结果立即阻断 | F01/F02/F15 | native command 路径拒绝不安全发送，mock 可证明 |
| R1 | 恢复一致性、订单终态和完整覆盖 | F03/F04/F14/F16 | R0；乱序、漏项、断线/重启回归通过 |
| R2 | 行情状态、录制与回放修正 | F06–F13/F18 | 可独立离线开发；R3 接入前完成与 R0/R1 的合并回归 |
| R3 | 私有运行生命周期、账户输出、DMS | F05 与私有设计修订 | R0/R1/R2；真实工厂驱动的 mock 生命周期完整 |
| R4 | 有限时 probe 与用户文档 | 原 P4 | R3；默认只读/不发单、模拟与 sandbox 边界明确 |
| R5 | release、历史重放和分层验收 | F17、协议待验证项 | 先前阶段完成；缺权限/密钥仅阻断对应在线验收 |

推荐一次交给 DeepSeek 一个阶段。首次指定 **R0**，而不是直接“实现 private_transport”。如果用户明确要求完整推进，可以连续完成离线阶段，但不能跳过阶段证据或将 sandbox 未验证写成通过。

## R0 · 修执行保护

> **状态：已完成（2026-09-15）。** 验收报告：`reports/ondo-acceptance/20260915T085556Z-r0/README.md`
>
> - FORK `onde-perps`：`5c2ba5a` → **`635b916`**（11 文件，+4208 −660）
> - APP `main`：`e49716b` → 见 `reports/ondo-acceptance/20260915T085556Z-r0/app_head_after_r0.txt`
> - 验收命令退出码 **0**：execution 44 / http_client 39 / reconciliation 63 / signing 63；全 crate 712 passed / 0 failed
> - 关闭 review 发现：**F01、F02、F15**
>
> **一处有意偏离**：R0.1 第 5 条要求断言 `OrderDenied`。准入拒绝路径实际发 **`OrderRejected`**——`Submitted → Denied` 不是合法状态机转换（`crates/model/src/orders/mod.rs:217` 只有 `(Initialized, Denied)`），发 Denied 会触发引擎的 forced-cleanup 路径。命令在 gate 1 被拒（订单尚未 submitted）时仍发 `OrderDenied`。依据与备选方案的取舍见报告 §3.4。
>
> **R1 入口条件已满足。** 注意 F05（P1 交付阻断，账户余额/持仓未接到真实 Nautilus 输出）与「私有 WS transport 不存在」是同一件事的两面。

### Task R0.1 — 从构造起关闭未核实的新风险

**修改：** ADAPTER `src/reconciliation.rs`、`src/execution.rs`。
**测试：** ADAPTER `tests/reconciliation.rs`、`tests/execution.rs`。

- [x] 替换现有“session=false 则不 governed”的条件。一个统一 admission 决策同时用于单笔与批量 native 提交；Disconnected/Recovering/Uncertain/Stale/缺必要 DMS 均拒绝。
- [x] 保留查询和有明确作用域的撤单能力；“不能下新单”不能阻断必要的风险清理。
- [x] 更新当前故意断言无 session 放行的测试，避免继续固定错误行为。
- [x] 检查 admission 与异步 HTTP 发出之间的状态变化：排队等预算期间若断线/失效，发出前再次核验同一运行代次的资格；并发提交不得绕开不确定状态。
- [x] 通过真实 factory/native trait 发送命令，断言 `OrderDenied` 等正确事件与 HTTP 请求数，不仅断言状态变量。

**必须覆盖：** fresh → connect → submit；恢复中 submit；Ready → disconnect → submit；Ready → 等预算 → metadata stale → 发出前；对应 batch。除明确 Ready 的有效路径外，写请求数全部为 0。

### Task R0.2 — 统一未知下单/撤单处理

**修改：** ADAPTER `src/execution.rs`、`src/reconciliation.rs`。
**测试：** ADAPTER `tests/execution.rs`、`tests/reconciliation.rs`。

- [x] 单笔、batch HTTP failure、2xx 漏项、无归属项、撤单确认查询失败全部进入统一 uncertainty 登记；保留每个 client ID、必要 venue ID、时间、原因和待查询动作。
- [x] 登记时立即使 admission 不再允许新风险；安排有界查询，不等 30 秒周期才阻断。不能把明确本地未发送的参数错误混入 unknown。
- [x] 区分明确失败、未收到确认和成功完成。POST 不自动重试；短暂 404 不自动认定 No Trade；重复回执/fill 不重复计账。
- [x] 批量部分成功时，只针对未确认部分补查，但整个账户的新风险 gate 服从未决暴露。

**回归样例：** Ready → A 的 POST 丢 ACK → 提交 B，B 的写请求数为 0；batch 三笔只回答一笔，另两笔均在 unknown；撤单 ACK 成功但后续查询失败，仍保留不确定。确认收敛后按规则恢复，不能永久锁死。

### Task R0.3 — 收紧带签名 endpoint 边界

**修改：** ADAPTER `src/common/credential.rs`、`src/config.rs`、`src/execution.rs`；必要时 `src/http/client.rs`。
**测试：** ADAPTER `tests/signing.rs`、`tests/http_client.rs`、`tests/execution.rs`。

- [x] 用 transport 一致的 URL parser 在解析凭据/发请求前校验 scheme、规范化 host、port、userinfo；仅接受同环境官方 sandbox authority。
- [x] loopback mock 走显式测试配置，仅限 loopback；默认配置不能把任意远程 URL 当 sandbox。
- [x] REST 与未来私有 WS 共用 endpoint policy，限制 authenticated redirect；跨 authority 跳转不得携带鉴权。
- [x] 验证大小写、相似域、userinfo、非法 URL、非 HTTPS 远程服务、生产 endpoint、`allow_production_orders=true` 均按策略拒绝。

**R0 验收命令（FORK）：**

```powershell
cargo +1.98.0 test -p nautilus-ondo --locked --test execution --test reconciliation --test signing --test http_client
```

依赖已缓存时加 `--offline`。本机严格 warning 问题须记录；允许仅为诊断另跑 `$env:CARGO_BUILD_WARNINGS='allow'`，不能把该结果标作严格构建通过或提交全局 suppress。

## R1 · 修恢复一致性与报告契约

### Task R1.1 — 修订单更新缓冲和恢复交接

**修改：** ADAPTER `src/reconciliation.rs`、`src/execution.rs`。
**测试：** ADAPTER `tests/reconciliation.rs`、`tests/execution.rs`。

- [ ] 订单更新不能按 venue order ID 只保留第一条。优先保留有界 arrival 顺序及 session/recovery generation；仅按可证明的事件身份去重，fill 单独按 fill ID 去重。
- [ ] 为 REST pass 与 stream 输入设计一个明确 owner/同步边界；不得有多个恢复 pass 同时推进同一账户。
- [ ] 在所有 REST await 完成后处理期间到达消息；“最后 drain → 切直接消费”使用同一同步边界，防止尾部消息滞留和旧 REST 覆盖新 stream。
- [ ] 最终 `AccountReading` / judgment 基于归并后的状态构造，不能继续从旧 REST payload 取 status/filledSize；包含仅在 stream 出现的订单。测试 REST=open、buffer=canceled/unknown，最终核对必须反映较新状态。
- [ ] 超过缓冲容量、解码漏消息、未知状态、旧会话污染均标 uncertain，触发有界补读；禁止静默 drop 后 Ready。

**必测输入：** 同订单 open→partial→canceled；完全重复 frame；fill 与 order 混合到达；positions/balance await 中再来 fill；最后 drain 边界来 frame；断线旧 generation 迟到；buffer overflow。结果应不丢终态、不重复 fill、不提前 Ready。

### Task R1.2 — 修持仓缺席、终态补齐和查询参数

**修改：** ADAPTER `src/reconciliation.rs`、`src/execution.rs`；查询参数结构位于 `src/http/private.rs`，订单 DTO 相关变化再改 `src/http/orders.rs`。
**测试：** ADAPTER `tests/reconciliation.rs`、`tests/execution.rs`、`tests/http_contract.rs`。

- [ ] 持仓核对覆盖“旧 baseline ∪ 已应用 fills ∪ 本次返回”的 instrument。按 endpoint 的实际完整覆盖契约定义 coverage；当前冻结 positions endpoint 为无分页的全部开放仓位响应。完整响应证明后缺席参与归零，失败/未知市场不能假定零；orders/fills 的分页完整性单独验证。
- [ ] 测试外部平仓/清算导致仓位从响应消失；显式 neutral 行、真正空账户、未知市场、非完整响应分别处理。
- [ ] 收到 terminal 而 fill 未齐时保留未决；补齐后重算清除暂时性缺口，确认正确终态输出时机。不要对不可恢复错误简单 reset flag。
- [ ] 支持 venue-ID-only 查询；两种 ID 都缺失本地拒绝；bulk `open_only/start/end` 映射或明确报 unsupported，时间边界/分页不能静默忽略。

**必测排列：** ACK→fill→terminal、terminal→fill、partial→cancel→late fill、duplicate fill、terminal 总量与累积 fill 不符；每个 fill 恰好应用一次，终态与未决状态可收敛。

**R1 验收：** 跑 R0 命令及 `--test http_contract`，保留用于故障重放的脱敏 mock 流；不需要真实账号即可完成。

## R2 · 修行情、录制和分析

### Task R2.1 — 贯通 metadata、市场状态和共享预算

**修改：** ADAPTER `src/data.rs`、`src/websocket/client.rs`、`src/factories.rs`、`src/python/factories.rs`、配置绑定；APP `src/spread_watch.py`、`src/market_tape.py`。
**测试：** ADAPTER `tests/market_data.rs`、`tests/python.rs`、factory 模块测试；APP `tests/test_spread_watch.py`、`tests/test_market_tape.py`。

- [ ] 将 metadata 失效/恢复、真实交易状态、feed 失效/恢复作为不同语义传到消费者；内部 stale 不只写日志。初始 unknown 不能被当 ready。
- [ ] watcher 订阅并处理 instrument 更新；record metadata 的版本、source、availability time、fee、tick 和真实 size increment。运行中变化必须进入 tape。
- [ ] 精度/增量变化要同步 WS parser instrument；有旧 book 时先失效/重建，等待对应新快照，不能混用两版精度。
- [ ] 保留 Deltas→snapshot_ready 顺序；记录“快照本身有效性”，使用资格由独立 feed/market/metadata gate 决定。只给一帧恢复深度也能恢复使用，不能刷新其原始时间。
- [ ] 从 Python data/exec factories 的真实注册路径共享同一环境 REST context/budget。明确账户/环境隔离键，无需对外暴露凭据；确认查询/撤单/恢复优先级有效且不过量。
- [ ] 检查公开 raw recorder drop/gap/finalize 统计是否能被阶段验收读取；无法确认完整性就输出 incomplete，不从行情仍流动推断录制完整。

**跨仓集成测试：** parser → message bus → watcher → tape → replay：旧 book→disconnect→一帧新 snapshot→ready→quote；断流拒绝，恢复后新 book 可用，旧档已清空；另测断流前 HALT，恢复 snapshot 仍不可交易。metadata refresh failure、Disabled、fee/increment 更新分别通过同一链路验证。

**预算测试：** Python factory 同时创建 data/exec，mock clock 下 metadata GET 与账户 GET/DELETE 使用同一预算；不是只比较 Rust 手工传入同一个 Arc 的情况。

### Task R2.2 — 回放按当时信息计费与判定

**修改：** APP `src/analysis/ondo_depth.py`、`src/market_tape.py`。
**测试：** APP `tests/test_ondo_depth.py`、`tests/test_market_tape.py`、`tests/test_spread_watch.py`。

- [ ] 扫描阶段只负责目录/完整性/可用方向统计；会影响历史行的 metadata 必须随 replay arrival/session 生效，不能预灌最终值。
- [ ] 将 market_halted、feed_ready、metadata validity 带入 replay state；明确 RESUME 才解除真实 halt，snapshot_ready 不解除 halt。
- [ ] 从真实 `size_increment` 算两腿公共可表示数量；旧 tape 没字段时标 `quantity_step_unknown`。CLI override 标 source=override，不伪称 venue 已验证。
- [ ] `event_age_ms` 使用当前本地 epoch 时刻减 book event 时刻；`receive_age_ms` 用本地 monotonic，`event_skew_ms` 用两腿事件差；保留 clock_offset_unknown，负值/不可比不能归零。
- [ ] 将 event-age 阈值作为明确参数和输出字段；没有可靠时钟时降低数据可信度，不能拿两腿同样延迟推导新鲜。
- [ ] 维持 `mapping_unverified` / `executable=false`；修好 Q/VWAP/费用仍不证明合约 multiplier、settlement、交易时段、标的/指数等价。

**数值回归：**

| 输入 | 应有结果 |
|---|---|
| metadata 2.5/0.9 bps → seq4盘口 → seq5 ONDO 100 bps | seq4 始终 3.4 bps；后续使用 100.9 bps；追加未来文件不改变已输出前缀 |
| 两腿 fresh → ONDO HALT → quote | quality 拒绝；disconnect→snapshot_ready 仍不解除 HALT |
| 实际 lot 0.002 / 0.003，卖价110，$100档 | common_step=0.006，Q=0.906；经 watcher 真实 metadata 链路 |
| event=1000ms、local epoch init=121000ms，两腿同延迟 | event age≈120000ms；不能报告为0，保留时钟证据状态 |
| 新 session 没 metadata 或缺 increment | 明确 unknown；不能复用未来 session 的 fee/step |

### Task R2.3 — 修录制排出、恢复读取和预检发布

**修改：** APP `src/market_tape.py`、`src/ondo_preflight.py`、watcher flush lifecycle。
**测试：** APP `tests/test_market_tape.py`、`tests/test_ondo_preflight.py`、`tests/test_spread_watch.py`。

- [ ] 满队列不能阻断 flush。用项目现有 event loop timer/独立 drain 机制保证无新事件也按 deadline 排出；处理 stop/join/exception，避免新增线程泄漏或阻塞行情回调。
- [ ] drop/gap 记录范围和计数准确；queue_max=1 的 gap 不能永久占满队列，让随后所有消息再次饿死。
- [ ] reader 在旧 session 尾截断后能识别合法新 session 边界并继续；旧 session 保持 incomplete。同 session 中间损坏必须明确失败/不可恢复，不偷偷跳过。
- [ ] reader 正常结束、异常、提前终止均 close generator/file；验证 Windows 上能重命名/删除读取过的临时文件。
- [ ] preflight 按 run_id 写临时目录，完整发布当前 run；失败也有 current attempt 状态，不能留旧 success。latest 若存在应原子切换并校验 hashes。
- [ ] 保留旧 schema reader 的明确兼容策略；若新增字段改变语义，升级 schema，并测试旧 tape 的降级结果，不就地改写原始录制。

**必测：** queue_max=1，时间0/2/5秒连续写入；无新消息的定时 flush；旧尾半行+新完整 session；同 session 中间损坏；同 --out 成功→missing失败、成功→transport失败；crash 在发布中途。

**R2 验收命令（APP）：**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_market_tape.py tests/test_ondo_depth.py tests/test_ondo_preflight.py tests/test_spread_watch.py tests/test_opportunities.py -q -p no:cacheprovider
```

FORK 跑 `cargo +1.98.0 test -p nautilus-ondo --locked --test market_data` 和新增 factory/Python 测试。R2 完成后全套 APP 回归一次；历史 tape 重放可到 R5 统一出报告。

## R3 · 私有生命周期与账户输出

### Task R3.1 — 实现单 owner 的私有运行时

**修改：** ADAPTER `src/execution.rs`、`src/reconciliation.rs`、`src/config.rs`、`src/python/config.rs`、`src/websocket/messages.rs`、相关 `mod.rs`；新增 `src/websocket/private.rs` 承载 transport/driver，必要时拆小但保留单 owner。

**测试：** ADAPTER `tests/execution.rs`、`tests/reconciliation.rs`、`tests/python.rs`；新增 `tests/private_runtime.rs`，使用本地 HTTP/WS mock 与可控时钟。

- [ ] 执行配置补私有 WS URL，复用 R0 endpoint policy；凭据经受控共享 credential 对象分别生成 REST/WS 签名，不能通过 accessor 打印或流出 secret。
- [ ] 实现 login frame 与确认、订单/fill 订阅及确认、断连/心跳超时、指数退避和退出；auth/403 等永久错误有终止分类，不能无限重试。
- [ ] 运行状态至少区分 Disconnected、Authenticating、Recovering、ReadOnlySynced、TradingReady、Uncertain、Stopping；socket connected 不直接等于账户 Ready。
- [ ] runtime 驱动 begin_recovery / reconcile_account / unknown probes / metadata validity；30秒周期配置要有调用者，周期与 reconnect 不并发启动重复 pass。
- [ ] 应用 R1 的恢复归并协议；登录/订阅失败、私有 decode loss、buffer overflow、stale session 不能保持 TradingReady。
- [ ] private raw/诊断记录脱敏且独立，公开 MD recorder 看不到 API key、login signature、账户订单 payload。

**模拟完整流程：** factory 创建→connect→login ack→订阅 ack→REST恢复+中途stream→一致→Ready→断线→拒绝新单→重连恢复→收敛。另测每个 await 阶段 stop；全部 task 有 owner，可等待结束，无悬挂后台任务。

### Task R3.2 — 账户事件、持仓覆盖、journal 和资金费

**修改：** ADAPTER `src/execution.rs`、`src/reconciliation.rs`、私有 DTO/parse；配置与 Python 绑定需同步。
**测试：** ADAPTER `tests/execution.rs`、`tests/reconciliation.rs`、`tests/private_runtime.rs`。

- [ ] 把 verified balance 映射为 Nautilus AccountState，通过真实 emitter/event channel 到达 cache；测试币种、total/free/locked 一致性、unknown/missing、异常负值语义。
- [ ] 实现 native PositionStatusReport；仅在完整范围证明后宣告 bulk coverage，不能用一个目标市场的局部结果代表整个账户。
- [ ] 将 `underLiquidation` 映射为新风险阻断条件并给出报告原因；未知风险状态不按正常处理。
- [ ] 接入 LedgerJournal 持久化与恢复，保存已应用 fill、订单关联、unknown/cancel待确认项、checkpoint/coverage。采用原子持久化/可检查版本，重启后先恢复核对再接受新风险。
- [ ] 测试“fill 已 emit 但 checkpoint 前 crash”和“checkpoint后重放同fill”；使用稳定事件身份和 cache/journal核对协议，不能只测文件能读回。
- [ ] 区分 funding rate、累计 funding payment 和本期实际 funding cashflow。能证明的支付变化才记账；缺支付记录时保留 unreconciled，不能用 rate×仓位推算成已确认支付。
- [ ] 外部订单/仓位的接纳、隔离、只读报告策略明确；不默认 cancel 外部订单来获得 clean account。

### Task R3.3 — DMS 有效期与可收敛停止

**修改：** ADAPTER `src/reconciliation.rs`、`src/websocket/private.rs`、`src/execution.rs`。
**测试：** ADAPTER `tests/private_runtime.rs`、`tests/reconciliation.rs`。

- [ ] 写入/构造续期帧不能当续期成功；将 pending/confirmed/expired 状态和单调时钟 deadline 纳入 admission。lost ACK、send失败、task阻塞、过期均阻断新风险。
- [ ] DMS renewal 协议从冻结规范和 mock 契约实现，可配置有界尝试；真实续期/解除语义在 R5 sandbox验证前标 unverified，不靠推测取消保护。
- [ ] read-only 模式从未触发 DMS 副作用；sandbox trading 模式在必要保护确认前保持非 Ready。
- [ ] stop 先停止新风险，再按 client ID/run ownership 清理本次订单并查询确认；记录残留和未知结果。不能用 market-wide DELETE 清理其他运行/外部订单。
- [ ] 真正执行停止动作并 await transport/task 退出；DMS release 的时机取决于已验证撤单/暴露状态。未确认订单存在时不得为了“干净退出”盲目解除保护。
- [ ] 记录 stop 的完成、超时、未决状态；重启必须继承未决信息，不能清空 map 获得 Ready。

**R3 验收：** 全套 `cargo +1.98.0 test -p nautilus-ondo --locked`；`cargo +1.98.0 check -p nautilus-ondo --features python --locked`；Python feature 的真实 factory/client mock 集成测试。达到“离线生命周期已完成”，尚不等于 sandbox 协议通过。

## R4 · 应用 probe 与文档

### Task R4.1 — 有限时 Ondo probe

**新增：** APP `src/ondo_probe.py`、`tests/test_ondo_probe.py`、`docs/ondo.md`。
**修改：** APP `.env.example`；仅添加变量名和注释，不读取或复制真实 key。

- [ ] CLI 定义 `--mode public|account-readonly|paper|sandbox`，默认 public，默认有限时运行；无默认下单参数。每种模式在启动输出环境、读写能力、停止条件。
- [ ] public 复用当前 preflight/data factory/录制；account-readonly 使用私有账户同步但不启用交易 Ready/DMS/撤单；paper 以 mock 或明确模拟执行产生事件，全部结果标 synthetic。
- [ ] sandbox 需要显式模式和 `--allow-sandbox-orders`，只接受 allowlisted sandbox endpoint。限定 instruments、单单金额、总敞口、订单数、最大运行时长；缺任一边界拒绝启动。
- [ ] 复用原生执行 factory 和 R3 lifecycle，不在 APP 复制 REST 签名或重试 POST。测试环境中完整调用链可观察订单/成交/余额/持仓事件。
- [ ] 所有退出路径触发有界 stop 与报告落盘；失败也写阶段状态，不将命令退出0等同账户已清理。
- [ ] 报告分别保留 Submitted/Ack、Filled/Partial/Canceled、Unknown/No Trade、账户是否对账、未决ID、测试真实性和协议验证状态。

**必测：** 默认命令写请求0；production+任何写参数拒绝；account-readonly没有DMS/DELETE；sandbox缺caps拒绝；paper无远程写；超时/键盘中断/登录失败报告完整且无secret。

### Task R4.2 — 整理操作与故障说明

- [ ] `docs/ondo.md` 给出 Windows PowerShell 的公开预检/有限采集/回放、账户只读、paper 和显式 sandbox 用法；未知字段与费用口径解释清楚。
- [ ] 写清签名协议待验证项、共享速率预算、DMS 失效/停止行为、断线恢复和 journal 文件位置；禁止把缺凭据输出当功能失败或已通过。
- [ ] sandbox credentials 通过既有 `.env`/环境机制载入，错误输出不打印配置对象或 raw login。
- [ ] 文档同时链接本计划与最终阶段报告，修正 fork README、fixture README、generated stub 的已过期阶段描述。

**R4 验收（APP）：**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider
```

## R5 · 可复现交付与分层验收

### Task R5.1 — 构建、依赖声明与干净环境回归

**修改：** APP `pyproject.toml`、实际使用的锁文件（若有）；FORK 既有 Windows 构建文档/相关打包入口；仅按生成器更新必要 stub。
**产物：** 版本化 release wheel、构建 manifest、候选验证环境、回滚说明。

- [ ] 从 FORK `BUILD_WINDOWS.md` 和当前构建脚本核对真实 feature/profile/build command，构建包含 Ondo 的 release wheel。记录 toolchain、target、features、profile、源码 SHA/dirty 状态、命令、退出码、wheel SHA256。
- [ ] 修正严格 Windows 链接器 warning 构建问题或给出明确平台例外证据；不能把 `CARGO_BUILD_WARNINGS=allow` 永久全局化后宣称 lint/strict gate 已通过。
- [ ] 将 native factory/exec config 加入包公共导出与生成 stub 检查；用当前生成器处理必要差异，避免手改大量非 Ondo `.pyi` 或提交纯 CRLF 噪声。
- [ ] 先在候选干净 venv 安装目标 wheel，核对 direct_url/hash，并导入 data/exec factories；跑 APP 全套测试及有限 public/mock probe。通过后再同步项目依赖，避免依赖文件仍指向旧 wheel。
- [ ] 保留旧 wheel 和清楚的恢复命令；不直接覆盖原产物后才尝试验证。安装到既有应用环境应在明确的该阶段实施范围内进行。
- [ ] fixture manifest 覆盖 observed REST/WS 与 signing vectors，标记 observed / official-example / synthetic；只有真实私有响应可成为 private observed fixture。

### Task R5.2 — 重放历史、有限公开验收、再分层验证 sandbox

- [ ] 冻结历史输入 hash；用修正分析器重放已有 ONDO–HL tape，输出新目录与新 code SHA。旧报告保留并注明 superseded，不覆盖原录制。
- [ ] 对比修复前后：fee版本、HALT过滤、首帧恢复、unknown step、event age、drop/gap/session incomplete。差异必须解释为规则变化/bug修复，不能宣称盈利改善。
- [ ] public smoke 先有限 2–5 分钟，再按原方案需要做有明确截止的长观察；采集不注册执行 factory、不读取交易凭据。失败保留数据/原因，不无限重连直到“凑到成功”。
- [ ] 每一 pair 单独记录实际 venue；ONDO–HL 样本不能代替 ONDO–ASTER。未验证合同映射持续 `executable=false`，不得用 BBO差或回放净差替代真实可成交证据。
- [ ] 刷新官方鉴权材料并保存 hash；sandbox 分三步记录：A 鉴权/账户只读；B 私有订阅/恢复；C 明确许可下的受限下单/成交/撤单/DMS。A通过不能替代B/C。
- [ ] 用真实 sandbox 验证 REST header 名称、WS签名串顺序/时间单位、实际private帧、DMS确认/续期/到期/释放；逐项更新 conflicts 和 observed fixtures。鉴权失败有有界诊断，不能轮流猜签名无限尝试。
- [ ] 没有可用 sandbox 权限/凭据时，交付全部已完成离线能力，将对应在线项记 `blocked: sandbox credentials/permission unavailable`。不触碰生产写接口填补测试空白。

## 2. 最终完成标准

- [ ] F01–F18 每项都有修复位置、对应回归和证据；未修项明确说明，不以整体测试数代替逐项闭环。
- [ ] 从真实 factory/native command 到事件/cache 的 mock 生命周期通过；没有未就绪写入、未知状态继续开仓、漏fill/重复fill或恢复竞态。
- [ ] tape 支持故障后恢复读取，完整性可解释；分析严格使用当时可得信息和真实增量，HALT、unknown、时钟偏移均可追溯。
- [ ] 所有默认路径不发真实订单；production writes 持续拒绝；read-only 没有 DMS/撤单副作用。
- [ ] release wheel 和项目依赖一致、可在干净候选环境复现；全套 APP/adapter 测试通过，平台例外单列。
- [ ] 阶段报告分别标记 `implemented`、`offline_verified`、`public_observed`、`sandbox_auth_verified`、`sandbox_execution_verified`；实盘默认 disabled。缺任何在线证据不补“已验收”。

## 3. 可直接复制给 DeepSeek 的首轮指令

```text
请阅读：
E:\Nautilus-Perps\reports\ondo-code-review-2026-09-15.md
E:\Nautilus-Perps\docs\superpowers\plans\2026-09-15-ondo-perps-continuation.md

这轮只实施 R0：修首次会话未就绪放行、单笔/批量/撤单结果不明后的立即阻断、带签名 endpoint allowlist。
先核对两仓当前代码与规则，补能证明缺陷的 native command 回归，再实现并运行对应测试。
不提前实施 R1–R5；不安装 wheel、不连接真实写接口、不提交或合并 Git。
保留已有 dirty/untracked 文件。阶段完成后在 reports/ondo-acceptance/<UTC-run-id>-r0/ 写报告，给出改动、测试结果、仍未解决项和下一阶段入口。
普通离线实现与测试无需反复询问；只有确实缺失且影响范围/权限的信息再说明原因。
```

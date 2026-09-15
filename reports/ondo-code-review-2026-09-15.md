# Ondo Perps 最新代码审阅 — 2026-09-15

结论：已有可复用的原生适配器、公开行情链路、录制和离线分析基础，但不能按“P1/P2 已验收、P3 只差私有 WS”继续推进。执行入口有未就绪放行问题；恢复与未知订单处理有遗漏；回放存在未来信息、停牌和时间口径问题。先修这些缺陷，再接私有运行生命周期。

本次交付只有 Review、测试证据和[续做计划](../docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md)。未修改产品代码、安装 wheel、提交 Git、连接私有服务或发送订单。

## 1. 审阅基线与证据

| 对象 | 本次核验 |
|---|---|
| 用户阶段报告 | [ondo-stage-status-2026-09-15.md](ondo-stage-status-2026-09-15.md)，保留原文 |
| 原完整方案 | [2026-09-14-ondo-perps-full-integration.md](../docs/superpowers/plans/2026-09-14-ondo-perps-full-integration.md) |
| 应用 | `E:\Nautilus-Perps`，`main`，HEAD `e49716b7ec8bc16648aa0f2c3e1450c8d73284e0`；相对原方案基线 `76b9881de1a453dbc8c4cff150395bafe649c279` 审阅 |
| Fork | `E:\nautilus_trader`，`onde-perps`，HEAD `5c2ba5aedfe35625a1787d0468d6885771aa2e51`；相对基线 `047d494e3c260bc6f701aa8f9193d1fd36f8da89` 审阅 |
| 应用测试 | 本次全套 **659 passed，97 subtests passed，1 warning**；warning 为既有 `test_maker_live.py::test_limits` 返回非 None |
| Rust 测试 | 默认严格命令退出 101：Rust 1.98/MSVC 创建 `.lib/.exp` 的链接器消息触发 warning 拒绝；仅当前进程设 `CARGO_BUILD_WARNINGS=allow` 后，**583 tests + 1 doc-test passed** |
| Python feature | 在同一临时 warning 策略下，`cargo check -p nautilus-ondo --features python --offline --locked` 退出0；未运行 Python-feature runtime tests |
| 已安装依赖 | `2.0.0rc4`，`direct_url.json` 指向 `E:/nautilus_trader/dist-p3/...whl`；原生 `OndoDataClientFactory`、`OndoExecutionClientFactory`、执行配置可导入 |
| 工作区 | 应用既有 untracked 数据保留；fork 的 40 个既有 `.pyi` 修改在 `--ignore-space-at-eol` 比较下无文本差异，未清理或暂存 |

测试日志与复现记录：[ondo-review-2026-09-15](ondo-review-2026-09-15/README.md)。Rust 绿色结果不能代替严格构建通过；本次未构建 release wheel。

审阅重点是原生提交入口、恢复/账户报告、公开 metadata、工厂预算，以及 watcher → tape → replay 的状态和成本语义。应用侧七组离线合成输入已复现问题；执行侧结论来自调用链和现有测试对照，不声称发生过真实订单事故。未声称覆盖 fork 全量新增代码的每条分支。

历史公开证据确实存在：`reports/ondo-acceptance/20260914T142730Z-p1-build/preflight/` 与 `20260915T033038Z-p1-ondo-hl/preflight/`；后者记录 2026-09-15 03:31 UTC 的公开预检。本次逐一核对两次预检各5个 payload，长度与 SHA256 都与 manifest 一致。已有 ONDO–HL 深度 tape，不能误写成 ONDO–ASTER 已验收。这些是历史工件，回放生成的质量/收益结论还受本报告缺陷影响。

## 2. 高优先级缺陷与交付阻断

### F01 · P1：未建立账户会话时，原生入口反而允许提交

位置：[reconciliation.rs:1231](E:/nautilus_trader/crates/adapters/ondo/src/reconciliation.rs:1231)、[execution.rs:1850](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:1850)、[execution.rs:1899](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:1899)。

`refuses_new_risk()` 是 `session_established && !can_submit_new_orders()`。新对象的 session=false、metadata=stale、state=Disconnected；`connect()` 只把 core 标为 connected。结果是在首次恢复、metadata 验证及必要 DMS 确认之前，单笔和批量提交都能通过入口检查。现有 `tests/reconciliation.rs:214–225` 甚至固定了“无 session 不拦截”的行为，所以测试绿不能排除此缺陷。

应从构造起拒绝未经核实的新风险；验证实际 native `submit_order` / `submit_order_list` 的 HTTP 请求数为零，不能只测辅助状态机。该问题不代表主网保护已被绕过，已确认的路径是 sandbox 下同样不安全的就绪放行。

### F02 · P1：订单结果不明后仍可下新单，批量结果不明还未登记

位置：[reconciliation.rs:1219](E:/nautilus_trader/crates/adapters/ondo/src/reconciliation.rs:1219)、[reconciliation.rs:1312](E:/nautilus_trader/crates/adapters/ondo/src/reconciliation.rs:1312)、[execution.rs:2081](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:2081)、[execution.rs:2106](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:2106)。

`note_unknown_submission` / `note_unconfirmed_cancel` 只向 map 插入条目，Ready 判定不检查这些 map。已 Ready → POST 超时 → 再次下单，仍可能发出第二笔请求。只有未来的显式对账才会评估未知条目，而当前没有实际对账调度器。

批量请求 HTTP 失败、2xx 回答漏掉某个订单时更严重：只记日志，没有把相关 client ID 放入 unknown map，后续 probe 无法找到它们。部分撤单确认分支也存在同类缺口。

应统一所有单笔、批量、撤单路径的“不确定 → 立即阻断新风险 → 有界查询确认”，不能自动重发 POST，也不能用一次 404 宣告订单不存在。

### F03 · P1：恢复缓冲丢订单状态，切换到直接消费还存在竞态

位置：[reconciliation.rs:862](E:/nautilus_trader/crates/adapters/ondo/src/reconciliation.rs:862)、[execution.rs:1427](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:1427)。

buffer 按 venue order ID 去重并保留第一条；一次恢复期间同一订单 `open → partial → canceled`，后两条被丢弃。fill 按 fill ID 去重是另一件事，不能把订单实体 ID 当成订单更新 ID。

此外 `read_account` 在读取 positions/balance 之前已经 drain，后续 await 期间还可能进入新消息。仅加一个 atomic `pass_in_flight` 不能保证“最后一次 drain → 切回直接消费”原子化，也不能保证较旧 REST order 状态不覆盖较新的 stream 状态。

应保留可核对的到达顺序、会话/恢复代次与去重键，完成无缝 drain/切换协议。旧 `private-transport-design.md` 中“No change to reconciliation.rs is needed”不能沿用。

### F04 · P1：已知持仓从完整响应消失时，未被判断为差异

位置：[reconciliation.rs:1639](E:/nautilus_trader/crates/adapters/ondo/src/reconciliation.rs:1639)。

`judge_positions` 只遍历本次返回的仓位。先接纳一笔 long，后被外部平仓/清算，接口返回 `[]` 时，不会检查旧 baseline 中该 instrument；已 Ready 可能继续 Ready。冻结 REST spec 将 `/v1/perps/positions` 描述为全部开放仓位，现有测试只覆盖显式 neutral/0 行。

应比较“上次已知/本地预期/本次返回”的 instrument 并集。在已证明完整覆盖的响应中，缺席可参与归零核对；没有完整覆盖证据时必须保留 unknown，不能把空列表直接当全账户空仓。

### F05 · P1 交付阻断：账户余额与持仓仍未接到真实 Nautilus 输出

位置：[execution.rs:1605](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:1605)、[execution.rs:1772](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:1772)、[execution.rs:2521](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:2521)。

`read_balance` 只得到内部结构；`reconcile_account` 只推进内部状态。唯一 account-state emit 入口依赖外部提供值，没有内部 reader 驱动。`generate_position_status_reports` 仍返回空 vector。`provides_bulk_position_coverage=false` 是正确保护，但不是持仓报告功能已交付。

还缺 LedgerJournal 的生产持久化/恢复调用、周期对账/unknown probe 驱动、资金费结算核对。仅添加私有 WS 不会自动让账户 cache 正确。

### F06 · P1：回放使用整段最终 metadata，未来费率会改写历史

位置：[ondo_depth.py:986](../src/analysis/ondo_depth.py:986)、[ondo_depth.py:1417](../src/analysis/ondo_depth.py:1417)。

`scan_tape` 扫完整段覆盖 `metadata[venue]`，分析器随后把最终 metadata 用于所有 arrival。复现：先记录 ONDO/ASTER 2.5/0.9 bps、再记录盘口、最后 ONDO 改成 100 bps；变更前 seq4 的成本被算成 **100.9 bps**，正确当时值为 **3.4 bps**。

watcher 重启在同一 tape 新 session 再写 metadata 是实际可触发路径。应在 replay 中逐条应用 metadata，禁止未来数据改变前缀输出；fee、increment、availability 和版本都应遵守相同原则。

### F07 · P1：停牌与 metadata 失效未贯通到质量判定

位置：[ondo_depth.py:577](../src/analysis/ondo_depth.py:577)、[spread_watch.py:1585](../src/spread_watch.py:1585)、[data.rs:1124](E:/nautilus_trader/crates/adapters/ondo/src/data.rs:1124)。

应用 replay 只处理 `adapter:disconnected` / `adapter:snapshot_ready`，忽略 `HALT` / `is_trading=False`。合成输入为新鲜 book → HALT → 新 quote，结果仍 `quality_ok=true`。实时 watcher 已经停用该腿，在线/离线判定不一致。

上游还有传播缺口：metadata 刷新失败或 Active→Disabled 时，内部 store 标 stale，但 refresh task 只日志告警，不发布相应失效事件。成功刷新只重发 Instrument；watcher 没有运行时 instrument 更新处理，费用仍停留在启动值。WS 的 instrument 注册也只在启动时发生，后续精度变化需要有明确更新/重建机制。

应分开维护 feed、market trading、metadata validity 三个条件；断线恢复快照不能解除真实停牌。需要 adapter→应用→tape→replay 的集成验证。

## 3. 其余确定缺陷

### F08 · P2：数量步长从 precision 猜测

位置：[spread_watch.py:867](../src/spread_watch.py:867)、[ondo_depth.py:1439](../src/analysis/ondo_depth.py:1439)。

tape metadata 漏掉真实 `size_increment`，分析器使用 `10^-size_precision`。实测 instrument 增量为 0.002/0.003，得到 common step **0.001**、Q **0.909**；正确应为 **0.006**、Q **0.906**。不能只用 CLI override 测 LCM 后宣称运行链路正确。旧 tape 缺字段应标 unknown，而非继续推断可表示数量。

### F09 · P2：event_age 字段实际计算了两个事件间的距离

位置：[ondo_depth.py:684](../src/analysis/ondo_depth.py:684)。

当前是 `record.event_ns - book.ts_event_ns`，没有使用当前本地接收时刻。两腿都延迟 120 秒、刚一起被录到时，结果两腿 `event_age_ms=0`、quality 通过。应使用本地 epoch 时刻相对 book 的事件时间，保留时钟偏移未知/负值异常；monotonic receive age、跨腿 event skew 各自独立。是否新增 event-age 拒绝阈值应明确配置，不能把“现有字段错误”混同于“已经有该阈值”。

### F10 · P2：队列满后无法到达自动 flush

位置：[market_tape.py:929](../src/market_tape.py:929)。

满队列先 return，计时 flush 在其后。复现 queue_max=1、flush_secs=1，时钟到 5 秒仍 accepted=1/dropped=2/pending=1/last_flush=0；只能等外部 flush（watcher 的状态 tick 约 30 秒）。应让排队/排出独立于新消息接受；没有新事件也要满足 flush 时限。drop/gap 必须继续准确保留。

### F11 · P2：旧 session 截断尾行会遮住后续完整恢复 session

位置：[market_tape.py:1397](../src/market_tape.py:1397)。

`_truncated_tail` 设置整个 reader `_done=True`。writer 支持故障后新 fragment/new session，但 reader 读到旧截断尾行便不再读取新文件。复现只得到 old session、`new_session_seen=false`；该路径还保留打开的 generator/file handle，在 Windows 上造成临时文件无法删除。

应区分旧 session 尾损坏、同 session 中间损坏与完整新 session；保留旧 incomplete，不掩盖新 session，所有退出路径关闭句柄。

### F12 · P2：失败预检重用输出目录，会遗留旧成功 manifest

位置：[ondo_preflight.py:567](../src/ondo_preflight.py:567)。

payload 总被覆盖，meta 仅 complete=true 时写。同一输出目录成功后再失败，会同时存在旧 `meta.complete=true`、旧 hashes 和新的非空 missing。应按 run 隔离并原子发布当前结果；失败/transport error 必须有当前状态，读取者必须校验 run identity 与 hashes。

### F13 · P2：Python 数据与执行工厂没有共享 REST 预算

位置：[factories.rs:99](E:/nautilus_trader/crates/adapters/ondo/src/factories.rs:99)、[factories.rs:184](E:/nautilus_trader/crates/adapters/ondo/src/factories.rs:184)、[python/factories.rs:35](E:/nautilus_trader/crates/adapters/ondo/src/python/factories.rs:35)。

两个 factory 的 `new()` 各自创建 budget；Rust 提供 `with_budget`，Python 构造只有 `new()`，无共享注入路径。同一 node 的行情刷新与账户调用因而可各占一份一秒预算。应从实际 Python factory 注册链路注入同环境共享 context，并测跨 factory 并发；不同环境/隔离范围不可意外混用预算。

### F14 · P2：终态先于 fill 到达后，未决标记无法清除

位置：[execution.rs:680](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:680)、[execution.rs:612](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:612)。

先收到 terminal 时设置 `reconciliation_needed=true`；之后补齐 fill，数量增加但标记没有复算/清除，后续一致 terminal 也不能恢复。应按已确认 fill/终态累计量重算，而非永久锁死；在真正补齐前不要输出让 cache 误判完成的终态。

### F15 · P2：带签名 URL 校验是主网域名黑名单

位置：[credential.rs:134](E:/nautilus_trader/crates/adapters/ondo/src/common/credential.rs:134)、[execution.rs:1014](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:1014)。

目前拒绝生产域名及子域，但接受其他远程 host/不完整解析结果，然后可向该 base URL 发签名请求。与原方案“同环境官方 host 或明确本地测试服务”不符。应按 transport 一致的 URL parser 检查 scheme/authority，采用 sandbox allowlist，并显式隔离 loopback mock。此处未验证编码 host 的主网绕过，不能声称存在该绕过，也不能把 HMAC 请求说成直接泄露 secret。

### F16 · P2：订单查询报告忽略已有 venue-ID 能力和过滤参数

位置：[execution.rs:2330](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:2330)、[execution.rs:2363](E:/nautilus_trader/crates/adapters/ondo/src/execution.rs:2363)。

native 单笔报告强制 client_order_id，但 HTTP `get_order(order_ref)` 及冻结 endpoint 支持 venue order ID。外部订单只有 venue ID 时无故失败。批量报告还忽略 `open_only/start/end`。应映射支持参数，无法保证的过滤语义明确拒绝，不能静默返回另一个范围。

### F17 · P2 交付缺口：已安装 wheel 与项目依赖声明不同

位置：[pyproject.toml:7](../pyproject.toml:7)。

当前 installed direct URL 为 `dist-p3`，项目仍指向 `target/wheels` 的旧同名 wheel。当前 venv 能导入不等于新环境能复现。需 release 构建、记录 fork SHA/toolchain/features/profile/wheel SHA256、在干净候选环境导入并运行应用回归，再同步依赖声明。阶段报告将 dist-p3 标作 debug；本次没有独立完成 profile 产物审计。

### F18 · P2：恢复后首本有效快照在 tape 中仍为 invalid

位置：[websocket/client.rs:899](E:/nautilus_trader/crates/adapters/ondo/src/websocket/client.rs:899)、[spread_watch.py:1367](../src/spread_watch.py:1367)、[spread_watch.py:1571](../src/spread_watch.py:1571)。

adapter 正确先发布替换后的 Deltas，再发布 snapshot_ready；应用写 Deltas 时把上一状态的 feed_ready=false 合入 book validity。因此 live 稍后已 ready，replay 仍持有 invalid book，直到第二帧深度到达才恢复。

不要简单交换事件顺序，以免提前启用旧缓存。应区分完整快照自身有效性与 feed 使用资格，保持原始 book 时间；测试只给一帧恢复深度，ready 后应可用，真实 HALT 仍拒绝。

## 4. 私有 transport 设计必须补正的项目

`E:\nautilus_trader\build-logs\private-transport-design.md` 可作设计材料，不能原样当验收规范：

- connect 可以交给专门 owner 驱动恢复，但入口从构造起必须受限；“不启动恢复”不能意味着“无 session 不设防”。
- 采用单一生命周期 owner 管理 login、订阅、恢复、周期任务、DMS 和停止；同步缓冲切换，避免只加一个 boolean。
- 私有 decode 失败若丢失订单/fill，账户进入 uncertain 并有界补读；不能仅 warn 后继续 Ready。
- DMS 当前允许检查不核验到期，`renew()` 在确认送达前延长本地期限。必须区分准备帧、写入成功、venue 确认、有效截止时刻；实际续期协议仍待 sandbox 验证。
- native stop 目前不执行 `stop_sequence`。停止需真正取消本次 run 拥有的订单、查询确认、保留未决 ID、按验证语义处理 DMS、等待任务退出；不能用 market-wide cancel 无差别清理账户外部订单。
- `underLiquidation` 当前被识别后忽略，须显式映射风控状态；资金费结算、持仓与余额事件、journal 恢复都属于执行验收。
- read-only 私有账户模式不能为“建立就绪”暗中订阅有撤单作用的 DMS，也不需要获得下单 Ready。

REST header 名称、WS 签名串顺序、私有 payload 形状、DMS 续订行为仍待 sandbox 确认。本次尝试刷新三份官方鉴权/DMS文档，浏览工具均未能打开；技术对照使用仓库内 2026-09-14 冻结材料，不声称文档已在线重新验证。参考官方入口：[API key authentication](https://docs.ondoperps.xyz/api-reference/api_key_authentication)、[WS login](https://docs.ondoperps.xyz/api-reference/connection/login)、[DMS](https://docs.ondoperps.xyz/api-reference/private-channels/subscribe-perps-dead-mans-switch)。

## 5. 修订后的阶段判断

| 原阶段 | 现在应记录的状态 | 下一道验收 |
|---|---|---|
| P0 协议与样本 | 已有冻结材料、公开 observed fixture；私有协议仍有未验证项 | 补 fixture provenance/hash，后续 sandbox 区分 auth 与交易验证 |
| P1 公开适配 | 原生注册及公开连接已做，存在 metadata/状态传播和共享预算缺口 | F07/F13/F18、真实工厂集成测试、有限公开采集 |
| P2 录制分析 | 已有 tape 和分析产物，不能用当前 quality/成本输出验收 | F06–F12/F18；修复后重放历史并标记旧分析 superseded |
| P3 执行/恢复 | 大量辅助逻辑及测试已做，未完成安全入口和生命周期 | F01–F05/F14–F16；私有 transport、账户输出、持久化、DMS/停止 |
| P4 应用 probe/交付 | 尚未实施 | `ondo_probe.py`、账户只读/模拟模式、文档、release 交付、分层验收 |

建议顺序：**R0 执行保护 → R1 恢复一致性 → R2 行情与录制真实性 → R3 私有生命周期与账户输出 → R4 probe → R5 release/分层验收**。R0–R2 用离线/mock 就能继续，不应等 API key 或把文档冲突当作停工理由。

下一窗口交给 DeepSeek 时，先指定 R0；每阶段交付差异、回归结果、未解决项，再继续下一个明确阶段。保留所有当前数据和原报告；本次计划不授权实盘。

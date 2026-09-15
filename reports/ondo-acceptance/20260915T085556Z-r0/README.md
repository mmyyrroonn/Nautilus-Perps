# R0 阶段验收报告 — 执行保护（admission gate 与签名 endpoint 边界）

**run id**：`20260915T085556Z-r0`
**阶段**：R0（plan `docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md` 的 R0.1 / R0.2 / R0.3）
**对应 review 发现**：F01、F02、F15（`reports/ondo-code-review-2026-09-15.md`）
**本报告作者**：主 session（Fable 5.1）。**验证部分的每一条都由主 session 亲自执行**，不是转述 sub agent 的「已完成」。转述处已明确标注。

**本阶段不授权主网交易、资金转移或常驻部署。** 全程未向任何真实 venue 发出请求。

---

## 1. 两个 SHA 与脏范围

### 1.1 FORK `E:\nautilus_trader`，分支 `onde-perps`

| | commit | 说明 |
|---|---|---|
| 起点 | `5c2ba5aedfe35625a1787d0468d6885771aa2e51` | ondo adapter crate 落地 |
| 终点 | `635b916a13ee71fafd1ecb7e7b5ecfdeeb3fa9a2` | R0 修复（本阶段唯一提交） |

任务分支 `task/r0-execution-guards` 已 `--ff-only` 合并进 `onde-perps` 并删除。合并后 `git branch` 只剩 `aster` 与 `onde-perps`。

**改动范围**（11 文件，+4208 −660）：

```
src/common/credential.rs      161 ++--     src/common/endpoint.rs   647 (新增)
src/common/mod.rs               1 +        src/config.rs             66 +-
src/execution.rs              636 ++++--   src/http/client.rs       256 ++++-
src/reconciliation.rs         501 ++++--   tests/execution.rs      1232 +++++++++---
tests/http_client.rs          373 ++++-    tests/reconciliation.rs  822 +++++----
tests/signing.rs              173 ++-
```

**脏范围声明**：该 fork 工作区有 ~40 个 CRLF-only 的 `.pyi` 文件长期显示为 ` M`（见 `fork_status_at_start.txt`）。**它们没有被暂存、没有被提交、也没有被修改**——本次提交是按路径逐个 `git add` 的，不是 `git add -A`。提交后 `git status --porcelain | grep -v '\.pyi$'` 输出为空：

```
$ git -C E:/nautilus_trader status --porcelain | grep -v '\.pyi$'
                                        # <- 空
```

### 1.2 APP `E:\Nautilus-Perps`，分支 `main`

| | commit | 说明 |
|---|---|---|
| 起点 | `e49716b7ec8bc16648aa0f2c3e1450c8d73284e0` | stage-1 spread watcher 的 Ondo 腿 |
| 终点 | 见本目录提交后 `app_head_after_r0.txt` | R0 的 plan / review / 验收工件 |

任务分支 `task/ondo-r0-acceptance` 已 `--ff-only` 合并进 `main` 并删除。

**未提交、有意不提交**（与前一阶段 §5 保持一致，本阶段未改动其归属）：`reports/stage1/*.csv`、`reports/perps-*`、`reports/polymarket-*`、`reports/entropy-*`、`.commandcode/`、`学习课题/`。`reports/ondo-acceptance/20260914T114942Z/p0/` 下那个 191.65 MB 的上游 wheel 仍在磁盘上、仍未进 git。

---

## 2. 验收命令与退出码

plan §R0 指定的原始命令，**在主 session 亲自执行**，完整输出见 `r0_plan_acceptance_command.txt`：

```
cargo +1.98.0 test -p nautilus-ondo --locked --offline \
  --test execution --test reconciliation --test signing --test http_client
```

| target | 结果 |
|---|---|
| execution | 44 passed; 0 failed |
| http_client | 39 passed; 0 failed |
| reconciliation | 63 passed; 0 failed |
| signing | 63 passed; 0 failed |
| **退出码** | **0** |

全 crate 套件（不加 `--test` 过滤）在**合并后的 HEAD `635b916`** 上另跑一次，完整输出见 `r0_full_crate_tests.txt`：

| target | passed | failed |
|---|---|---|
| lib（单元测试） | 385 | 0 |
| execution | 44 | 0 |
| http_client | 39 | 0 |
| http_contract | 77 | 0 |
| market_data | 40 | 0 |
| reconciliation | 63 | 0 |
| signing | 63 | 0 |
| python | 1 | 0 |
| **合计** | **712** | **0** |

**退出码 0。**

**格式检查**：`cargo +1.98.0 fmt -p nautilus-ondo -- --check` → 退出码 0（仅有 nightly 选项不可用的 warning）。

**构建 warning**：未在本机严格模式下重跑 `-D warnings`；本阶段改动未新增 warning（`cargo build` 输出无 crate 内 warning）。**这一点属于未验证**——见 §5。

---

## 3. 改了什么，以及每条断言的位置

### 3.1 R0.1/R0.2 — 从构造起有效的统一 admission 决策

`reconciliation.rs`：

| 位置 | 内容 |
|---|---|
| `:125` | `pub enum NewRiskRefusal`（`UnknownSubmissions` / `UnconfirmedCancels` / `AccountState` / `MetadataStale` / `DeadMansSwitch`） |
| `:209` | `pub enum Admission { Granted { generation }, Refused { reason } }` |
| `:1411` | `pub fn admission()` — 唯一的准入裁决入口 |
| `:1430` | `pub fn revalidate(&self, permit: &Admission)` — **同一运行代次**的复核 |
| `:1474` | `pub fn new_risk_refusal()` — 裁决内容 |
| `:1510` | `fn invalidate_admissions()` — 代次自增，令已发出的 permit 失效 |

裁决顺序（`new_risk_refusal`，`:1474`）：未知下单 → 未确认撤单 → `state != Ready` → metadata stale → DMS 未放行。**空列表 / Ready / Current / 已武装时才返回 `None`**，即默认拒绝。

`execution.rs` 的**双重 gate**：

| gate | 位置 | 时机 |
|---|---|---|
| gate 1 | `:1964`（单笔）、`:2102`（批量） | 收到命令时取 permit；`Refused` → `deny_submission` 并 `return Ok(())`，**不建请求** |
| gate 2 | `:1999`（单笔）、`:2168`（批量） | 任务内、发出前，用 `revalidate(..., Admission::Granted { generation: permit })` 复核**同一代次** |

单笔与批量走的是**同一套裁决与同一套复核**。撤单与查询路径**不受 gate 约束**——`R0.1` 要求的「拒绝新风险不得阻断风险清理」由此保持。

### 3.2 预算排队窗口

`http/client.rs` 的 `post_signed_raw`（`:749`）在预算与签名之间的顺序是关键：

```
:759    self.budget.acquire(priority).await;      // 可能排队整整一个限流窗口
:761    self.admit_new_risk(permit)?;             // <- 排队之后再复核
:763    let headers = signed_request_headers(...)?;
```

`admit_new_risk`（`:1034`）走 `RunAdmission`（`execution.rs:2933` → `revalidate`），在**没有安装 guard 时 fail-closed**返回 `NO_NEW_RISK_GUARD`（`http/client.rs:152`）。

**这是本阶段最关键的一处时序**：修复前，准入是在命令入队时判定的，而 `budget.acquire()` 可能等待一个完整的限流窗口；在这个窗口内断线或 metadata 失效，旧代码仍会把请求发出去。现在复核发生在 acquire **之后**。

该路径确实可达：`post_signed_raw` 的调用点只有 `create_order`（`:878`）与 `create_orders_batch`（`:906`），而这两个只被 `execution.rs:2017` 与 `:2195` 调用。构建签名头之前的任何一条拒绝路径都不会产生请求。

### 3.3 R0.3 — endpoint 边界不再是主网黑名单

新文件 `src/common/endpoint.rs`（647 行）。`OndoEndpointPolicy { environment, family }` 的 `classify()` 按固定顺序判定：环境非 Sandbox → `ProductionForbidden`；`Url::parse` 失败 → `MalformedUrl`；无 host → `MalformedUrl`；生产环境按名 → `ProductionHostForbidden`；含 userinfo → `UserInfoForbidden`；回环 → 按 scheme 族判定并归类为 `LoopbackTestService`；host 非官方 → `HostNotAllowed`；scheme 非 HTTPS → `UnsupportedScheme`；带端口 → `PortNotAllowed`。

**旧实现是「已知生产域名黑名单」，新实现是「仅官方 sandbox authority 白名单」**——这是 F15 的实质。gate 在 `HttpClient::builder()` **之前**运行。

`HttpClient` 的认证客户端不再跟随重定向：`.redirect_policy(if auth.is_some() { HttpRedirectPolicy::Reject } else { HttpRedirectPolicy::Follow })`（`http/client.rs:403`）。`HttpRedirectPolicy` 只有 `Follow | Reject` 两个变体，**没有逐跳钩子**，因此对带鉴权的客户端只能整体拒绝重定向——跨 authority 的鉴权头泄漏由此从根上排除。

**架构裁决（主 session 决定，非 sub agent 自选）**：loopback 作为**显式类别**存在（`OndoEndpoint::LoopbackTestService`），而不是新增一个 `allow_loopback_test_service` 开关。理由：开关是一个可以被误开的运行时状态，类别是一个不可配置的枚举分支。同时**未授权修改 `src/factories.rs`**。

### 3.4 有意偏离：拒绝用 `OrderRejected`，不是 `OrderDenied`

**这是本阶段唯一一处偏离 plan 字面要求的地方，在此明确记录。**

plan R0.1 写的是「断言 `OrderDenied` 等正确事件」。实现选择：**准入拒绝路径发 `OrderEventAny::Rejected`**（`execution.rs:2035`、`:2042`、`:2055`、`:2215`、`:2227`），而**命令被引擎拒绝、请求从未存在的路径发 `OrderDenied`**（`:1255`、`:1978`、`:1987`、`:2008`、`:2115`、`:2143`、`:2180`）。

裁决依据（主 session 从源码核实，非转述）：

1. **`Submitted → Denied` 不是合法转换。** `crates/model/src/orders/mod.rs:217` 的 Denied 转换只有 `(Initialized, Denied)`；`Submitted` 的合法后继在 `:235-241`，不含 `Denied`。发出 `Denied` 会把引擎推入 "Terminal event failed to apply → forcing cleanup from own book" 的错误路径。
2. 被拒绝的订单**已经 `emit_order_submitted`**（`execution.rs` 在 gate 2 之后、发送之前上报 submitted），所以它处于 `Submitted` 状态。
3. plan 的措辞是「`OrderDenied` **等正确事件**」——要求的是正确事件，不是特指某个变体。
4. 备选方案是「推迟 `emit_order_submitted` 直到请求真正发出」，但那会让订单在在途期间停留在 `Initialized`，破坏策略撤单（策略无法撤一个尚未 submitted 的单）。

**测试侧的影响**：`tests/reconciliation.rs:2086` 的批量/单笔拒绝测试统计的是 `OrderEventAny::Denied` 事件数——该测试走的是命令在 gate 1 被拒的路径（订单尚未 submitted），所以断言 Denied 是正确的。

---

## 4. 回归证据

### 4.1 独立证伪（主 session 亲自做，不是 sub agent 的「我测过了」）

见 `f01_before_code.txt`（修复前源码取证）与 `f01_falsification.txt`（证伪实验）。

**方法**：把修复前的语义「无 session 则不 governed」重新注入新版管道——在 `new_risk_refusal()` 顶部插入 `if !self.session_established { return None; }`。

**结果**：测试以

```
no create request exists for an account this client has never read:
  ["/v1/perps/orders", "/v1/perps/orders/batch"]
```

**失败**。即：新版管道确实依赖 `session_established` 参与裁决，F01 的修复是真实的、不是「测试恰好通过」。回退用备份 `/tmp/reconciliation.rs.bak` 恢复，恢复后 sha256 = `49a61c6e84870d0429cf27dcd04fd7790fe697d3bd471fe49f7e5b4866e3e1d6`。

**第一次证伪尝试不忠实，被我自己推翻**：最初我把 `refuses_new_risk()` 回退成 `self.session_established && !self.can_submit_new_orders()`，测试**通过**了——因为新的提交路径调用的是 `admission()`，不是 `refuses_new_risk()`。这个「通过」是假信号。重新在 `new_risk_refusal()` 顶部注入才复现出缺陷。**记录在此，因为它说明：一次证伪实验失败不等于修复无效，可能只是打错了位置。**

### 4.2 是否有测试被删除换绿

**没有。** 用 `git show 5c2ba5a:<file>` 与工作区逐文件对比测试函数数量（`#[test]` / `#[tokio::test]` / `#[rstest]`，含参数化前的声明数）：

| 文件 | 修复前 | 修复后 |
|---|---|---|
| `tests/execution.rs` | 46 | 65 |
| `tests/reconciliation.rs` | 55 | 63 |
| `tests/signing.rs` | 16 | 20 |
| `tests/http_client.rs` | 32 | 39 |

无一项下降。注意 `#[rstest]` 带 `#[case]` 的会展开成多条测试用例，所以「函数数」小于「用例数」（例如 `signing.rs` 20 个函数跑出 63 条）。

### 4.2b 测试没有混进生产构建

`src/` 下 21 个文件含测试属性，**每一个都有 `#[cfg(test)]` 门控**（逐文件核对，无例外）。即：单元测试不进生产二进制，不存在「为测试而加的生产分支」。

`endpoint.rs` 里那个与旧黑名单实现对拍的差分测试也在这个门控内。该测试对 1728 种 URL 形式重新实现了旧逻辑并断言新策略「不比它弱」，且带 `refused_before > 0` 断言，防止两边都放行造成的空泛通过。

### 4.3 endpoint 策略只有一个入口，且无法被配置放大

`OndoEndpointPolicy::classify` 在 `src/` 内的调用点，全 crate 搜索后确认**只有一个**：

```
src/common/credential.rs:129
    OndoEndpointPolicy::authenticated(environment, OndoSchemeFamily::Http).classify(base_url)
```

即 `validate_authenticated_environment()` —— 它在读凭据之前运行（`resolve_credential` 先调它）。**没有任何第二条绕过它的路径，也没有任何配置项能放宽它**（`OndoEndpointPolicy` 由 `environment` 与 `family` 两个值构造，不存在运行时开关）。

`OndoSchemeFamily::WebSocket` 目前**没有调用方**（私有 WS transport 尚不存在）。它在 `endpoint.rs` 的单测里被覆盖：`test_the_websocket_policy_admits_the_same_authorities` 与 `test_the_scheme_family_separates_the_two_policies`（后者断言 scheme 族是两者之间的**全部**差异）。**它是被测试的，但还没有被生产路径行使**——接 transport 时才会真正走到。

### 4.4 没有任何绕过 gate 的签名 POST 路径

`post_signed_raw` 的调用点经全 crate 搜索确认只有两处（`create_order`、`create_orders_batch`），二者又只被 `execution.rs` 的两个 gate 后的调用点触达。**不存在第三条带签名的写路径。**

### 4.5 枚举收窄未削弱旧检查

`MetadataValidity` 只有 `Current` / `Stale` 两个变体（已核实），因此 `new_risk_refusal()` 里对 `Stale` 的显式匹配**等价于**旧的 `metadata.is_usable()` 取反，没有放宽。

### 4.6 sub agent 的两处问题（主 session 发现）

1. **r0-core 的第一版「确定性」预算窗口测试使用了 `tokio::time::pause()`**，但执行客户端的任务跑在 `nautilus_common::live::get_runtime()` 的**全局运行时**上（`crates/live/src/task.rs:1130`），`pause()` 对它无效——那是一个 1ms 的真实竞态。r0-core 自行发现并改为「先排空速率预算，使提交必须排队整个 1s 窗口」，再断言翻转发生在该窗口内。
2. **r0-endpoint 的报告表格有一处计数错标**（把某 target 的测试计数归错了对象）。主 session 独立重跑每个 target 得到真实值（signing 63、http_client 35→39、execution 42→44），确认结论不受影响。

### 4.7 命名变更

`OndoBatchSendError` → `OndoNewRiskSendError`。**未保留弃用别名**——理由是已核实该类型**不在 crate 根 re-export**（`src/lib.rs` 无 `pub use`），全部使用点都在 crate 内部的 `execution.rs` / `http/client.rs` / `tests/http_client.rs`，属内部类型。

---

## 5. 未解决项（明确声明，不掩盖）

| # | 项 | 状态 |
|---|---|---|
| 1 | **严格 warning 构建** | 本阶段**未**跑 `-D warnings`。改动未新增 warning，但「严格构建通过」这句话**本阶段没有证据**，不能引用 |
| 2 | **私有 WS transport** | 仍然一行未写。这不是 R0 范围，但 R0.3 的 endpoint policy 已按「REST 与未来私有 WS 共用」的结构写成，接 transport 时直接复用 `OndoSchemeFamily::WebSocket` 分支 |
| 3 | ~~`allow_production_orders=true` 组合未测~~ | **已核实，撤回原先的「未验证」说法。** `src/config.rs:310` 的 `validate()` 在该标志为 true 时直接返回 `OndoExecutionConfigError::ProductionOrdersUnsupported`，**早于任何 client 构造、更早于 endpoint gate**。有测试：`tests/execution.rs` 用 `allow_production_orders: true` 遍历生产与相似域 URL，断言错误是 `ProductionOrdersUnsupported`（"the flag is refused by name whatever the endpoint says"）且 `mock.captured().is_empty()`（"the refusal happens before a socket is opened"）；`factories.rs` 另有 `test_the_execution_factory_refuses_production_order_entry` |
| 4 | **对真实 venue 的请求** | 全程零请求。所有 fixture 仍是合成的；sandbox 从未被连接过 |
| 5 | **`Rejected` 事件的引擎侧影响** | §3.4 的裁决基于状态机源码。**未在真实引擎上跑过端到端**验证 `Rejected` 在策略侧的观感（策略是否把 `Rejected` 当作终态处理） |
| 6 | **wheel 未重编** | venv 里装的仍是旧 wheel；R0.3 的 endpoint policy 与 Python 面无关，但**要跑 app 侧集成需先重编 wheel**（`--release`，记录 SHA256） |
| 7 | **`r03_full_crate_tests.txt` 与 `r0_full_crate_tests.txt` 的差异** | 前者是 R0.3 收尾时的全 crate 快照，后者是合并后 HEAD 的重跑。两者 target 计数意义不同，引用时以 `r0_full_crate_tests.txt` 为准 |

---

## 6. 下一阶段入口

plan 的 R1..R5 未开始。**R1（修恢复一致性与报告契约）的入口条件已满足**：

- R0 的验收命令退出码 0，fork `onde-perps` 干净，改动已合并
- `admission()` / `revalidate()` 是 R1 唯一的准入入口，R1 不需要再动 gate 的落点
- R1 涉及 F03（恢复缓冲丢订单状态、直接消费竞态）、F04（持仓消失未判差异）、F05（账户余额与持仓未接到真实 Nautilus 输出）。**注意 F05 是 P1 交付阻断**，且与「私有 WS transport 不存在」是同一件事的两面

**本目录的工件清单**：

| 文件 | 内容 |
|---|---|
| `environment.txt` | 运行环境 |
| `fork_head_at_start.txt` / `fork_status_at_start.txt` | fork 起点 SHA 与脏范围（含 ~40 个 CRLF-only `.pyi`） |
| `f01_before_code.txt` / `f01_falsification.txt` | F01 的修复前取证与独立证伪 |
| `fork_status_after_r03.txt` / `fork_diffstat_after_r03.txt` | R0.3 收尾时的 fork 状态 |
| `r03_full_crate_tests.txt` / `r03_reviewer_checks.txt` | R0.3 收尾时的全 crate 快照与核查清单 |
| `r0_plan_acceptance_command.txt` | **plan 指定的原始验收命令完整输出** |
| `r0_full_crate_tests.txt` | 合并后 HEAD 的全 crate 套件输出 |
| `app_head_after_r0.txt` | APP 侧终点 SHA |

**安全声明**：本目录只含 URL、路径、测试计数与源码行号。**不含账户信息、不含密钥、不含任何签名或登录帧。** 按 plan §0 要求，这些内容不得进入报告、公共原始磁带或 Git——本阶段未产生任何此类内容。

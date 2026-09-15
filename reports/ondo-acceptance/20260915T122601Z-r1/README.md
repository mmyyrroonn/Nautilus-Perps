# R1 阶段验收报告 — 账户一致性（恢复期的合并读、报告缓冲与查询契约）

**run id**：`20260915T122601Z-r1`
**阶段**：R1（plan `docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md` 的 R1.1 / R1.2）
**对应 review 发现**：F03、F04、F14、F16（`reports/ondo-code-review-2026-09-15.md`）
**本报告作者**：主 session（Fable 5.1）。§2 与 §4.1–§4.3、§4.4 的第一条、§5.1 由主 session **亲自执行**；§4.4 其余三条是转述 sub agent 的报告，已逐条标注。**§5.1 是对本报告自身一处错误陈述的更正**（该陈述曾随 `cd959df` 的 commit message 一起进入 `main`）。

**本阶段不授权主网交易、资金转移或常驻部署。** 全程未向任何真实 venue 发出请求，未使用任何真实账号。

---

## 1. 两个 SHA 与脏范围

### 1.1 FORK `E:\nautilus_trader`，分支 `onde-perps`

| | commit | 说明 |
|---|---|---|
| 起点 | `cef58bcf9453af89221560fa7960154a8fccbde9` | R0 收尾（take the market cancel reason by reference） |
| **当前 HEAD** | **`5b7d2db6694c8f1707482d07ce83de19888213c7`** | R1 全部改动（"square the account across recovery and honour the report-query contract"） |

任务分支 `task/ondo-r1-recovery-consistency` 已 `--ff-only` 合并进 `onde-perps` 并删除。合并后 `git branch` 只剩 `aster` 与 `onde-perps`。

**改动范围**（`cef58bc..5b7d2db`，8 文件，**+3583 −276**）：

```
crates/adapters/ondo/src/execution.rs        1036 ++++++++++++++++++++----
crates/adapters/ondo/src/http/orders.rs         7 +-
crates/adapters/ondo/src/http/private.rs      122 ++-
crates/adapters/ondo/src/reconciliation.rs    421 ++++++++--
crates/adapters/ondo/test_data/conflicts.md     1 +
crates/adapters/ondo/tests/execution.rs      1105 ++++++++++++++++++++++++-
crates/adapters/ondo/tests/http_contract.rs    47 +-
crates/adapters/ondo/tests/reconciliation.rs 1120 ++++++++++++++++++++++++--
```

**脏范围声明**：该 fork 工作区长期有 40 个 CRLF-only 的 `.pyi` 显示为 ` M`（R0 已有，见 R0 目录 `fork_status_at_start.txt`）。**它们没有被暂存、没有被提交、也没有被修改**——本次提交是按路径逐个 `git add` 的 8 个 ondo 文件，不是 `git add -A`。提交后：

```
$ git -C E:/nautilus_trader status --porcelain | grep -v '\.pyi$'
                                        # <- 空
```

R1 动过的唯一非 ondo 面文件是 `crates/adapters/ondo/` 内部的 4 个源文件 + 3 个测试文件 + 1 个 test_data 文件，无跨 crate 改动，无 lockfile 改动，无配置改动。

### 1.2 APP `E:\Nautilus-Perps`，分支 `main`

| | commit | 说明 |
|---|---|---|
| 起点 | `a7a49fa0afdf2f8b8f45e2a2acb53c304198232b` | reports: pin the R0 acceptance artifacts to the final fork HEAD |
| 终点 | 见本目录 `app_head_after_r1.txt` | R1 的验收工件与报告 |

任务分支 `task/ondo-r1-recovery-consistency` 已 `--ff-only` 合并进 `main` 并删除。

**未提交、有意不提交**（与 R0 §1.2 的归属一致，本阶段未改动其归属）：`reports/stage1/*.csv`、`reports/perps-*`、`reports/polymarket-*`、`reports/entropy-*`、`.commandcode/`、`学习课题/`。`reports/ondo-acceptance/20260914T114942Z/p0/` 下那个 191.65 MB 的上游 wheel 仍在磁盘上、仍未进 git。

---

## 2. 验收命令与退出码

plan §R1 指定的命令是「R0 命令及 `--test http_contract`」（plan §128）。R0 的原始命令见 R0 报告 §2。**以下每条都在最终提交 `5b7d2db` 的字节上由主 session 亲自跑过**，完整输出在同名产物里，产物头部写有命令行本身：

| 命令 | 退出码 | 产物 |
|---|---|---|
| `cargo +1.98.0 test -p nautilus-ondo --locked --offline --test execution --test reconciliation --test signing --test http_client --test http_contract` | **0** | `r1_plan.txt` |
| `cargo +1.98.0 test -p nautilus-ondo --locked --offline`（全 crate，同 R0 形状） | **0** | `r1_suite.txt` |
| `cargo +1.98.0 fmt -p nautilus-ondo -- --check` | **0** | `r1_fmt.txt` |
| `RUSTDOCFLAGS='-Dwarnings' cargo +1.98.0 doc -p nautilus-ondo --no-deps --locked --offline` | **101**（既有红） | `r1_doc.txt` |
| `cargo +1.98.0 clippy -p nautilus-ondo --all-targets --locked --offline` | **101**（既有红） | `r1_clippy.txt` |

plan 命令逐 target：

| target | 结果 |
|---|---|
| execution | 58 passed; 0 failed |
| http_client | 39 passed; 0 failed |
| http_contract | 77 passed; 0 failed |
| reconciliation | 81 passed; 0 failed |
| signing | 63 passed; 0 failed |
| **合计** | **318 passed; 0 failed；退出码 0** |

全 crate 套件（`r1_suite.txt`）：

| target | R0 | R1 | delta |
|---|---:|---:|---:|
| lib（单元测试） | 385 | 387 | +2 |
| execution | 44 | 58 | +14 |
| http_client | 39 | 39 | 0 |
| http_contract | 77 | 77 | 0 |
| market_data | 40 | 40 | 0 |
| reconciliation | 63 | 81 | +18 |
| signing | 63 | 63 | 0 |
| Doc-tests | 1 | 1 | 0 |
| **合计** | **712** | **746** | **+34** |

**退出码 0。** R1 新增的 34 个用例全部落在本阶段动过的两个测试文件里（execution +14、reconciliation +18）；`http_contract` 的**用例数没变**（77），因为它那条契约测试是同一个 `fn` 里追加断言（见 §3.3），不是新开用例。

> **对 R0 一处计数标签的更正（不修改 R0 已提交的记录，只在此声明）**：R0 报告 §2 的全 crate 表里有一行 `python | 1 | 0`。该行实际是 **`Doc-tests nautilus_ondo`**——`r0_full_crate_tests.txt:756-760` 显示那条 `1 passed` 属于 doc-tests；`tests/python.rs:28` 是 `#![cfg(feature = "python")]`，而 `Cargo.toml` 的 `default = ["high-precision"]` 不含 `python`，本 session 的全套件运行根本没有输出 `Running tests\python.rs` 这一行。两个数字（712 / 746）不受影响，受影响的只是那一行的**标签**。

---

## 3. 改了什么，以及每条断言的位置

行号全部按**最终提交 `5b7d2db` 的冻结字节**取自仓库实读（本阶段中途出现过行号漂移：批量回归测试从 3448 移到 3459，因此本报告不沿用任何中途读数）。

### 3.1 R1.1（F03）— 缓冲、代际与所有权边界

| 断言 | 位置 |
|---|---|
| 缓冲条目带代际：`BufferedReport<T> { generation, payload }` | `src/reconciliation.rs:1026` |
| 缓冲本体（容量、`order_indices`、`fill_ids`、dropped、superseded） | `src/reconciliation.rs:1063` |
| 订单报告入缓冲 | `src/reconciliation.rs:1116` |
| 成交入缓冲（成交按 **fill id** 单独去重，不与订单共用一个 identity） | `src/reconciliation.rs:1145` |
| 取走丢弃计数 | `src/reconciliation.rs:1170` |
| 按代际排空（superseded 单独计数） | `src/reconciliation.rs:1204` |
| 代际在「恢复开始」与「会话结束」时改变 | 测试 `tests/reconciliation.rs:1883` |
| 缓冲按 id 去重**且保留到达序**，不只留第一条 | 测试 `tests/reconciliation.rs:1729` |
| 同一订单的**每一条**更新都按到达序保留 | 测试 `tests/reconciliation.rs:1768` |
| 已作废（superseded）恢复期的报告被拒绝并计数 | 测试 `tests/reconciliation.rs:1820`、`:3823` |
| 缓冲满 → 拒绝并计数 | 测试 `tests/reconciliation.rs:1848` |
| 缓冲溢出 → 账户**不确定**，直到有界重读 | 测试 `tests/reconciliation.rs:3907` |
| 恢复期无法落地的报告 → **有界重读**，绝不静默丢弃后 Ready | `src/execution.rs:1679`（`note_lost_reports`）、测试 `tests/reconciliation.rs:1907` |
| 一次恢复独占账户（第二轮被拒） | `RecoveryPassRefusal` `src/reconciliation.rs:1522`、`begin_recovery` `:1788`、测试 `tests/execution.rs:4288` |
| 账户读期间到达的报告被**恰好重放一次** | 测试 `tests/reconciliation.rs:3482` |
| 一个订单的多条报告按到达序重放，**后者胜** | 测试 `tests/reconciliation.rs:3569` |
| 重复帧只应用一次且状态不动 | 测试 `tests/reconciliation.rs:3655` |
| 合并态（含**只从流里见过**的订单）产出 `AccountReading` | `AccountReading` `src/reconciliation.rs:921`、`order_reading` `src/execution.rs:2111`、`status_report` `:996`、`apply_order` `:2152`、`apply_fills` `:2171` |

### 3.2 R1.2a（F04/F14）— 持仓对账与「完备性由接口的真实契约决定」

R1.2a 的核心不是「持仓没了就算平」，而是：**持仓判定的对象是「旧基线 ∪ 已应用成交 ∪ 本次返回」，缺席只有在**已证明的完整响应**里才参与归零。**

| 断言 | 位置 |
|---|---|
| 持仓判定 | `judge_positions` `src/reconciliation.rs:2265` |
| 订单判定 | `judge_orders` `src/reconciliation.rs:2163` |
| 基线、成交、终态三者的差额计算 | `fill_gap` `src/execution.rs:315`、`is_accounted_for` `:349`、`is_settled` `:377` |
| 订单解析（venue id / client id 两条路径共用的解析） | `resolve_order` `src/execution.rs:458` |
| 中性持仓是**显式 0**，不是「缺席」 | 测试 `tests/reconciliation.rs:760` |
| 平的行与缺席的行是**两种不同的陈述** | 测试 `tests/reconciliation.rs:1041` |
| 持仓消失且没有成交能解释它 → **不确定** | 测试 `tests/reconciliation.rs:796` |
| 已应用成交解释不了持仓 → 不确定，**永不 Ready** | 测试 `tests/reconciliation.rs:830` |
| venue 不再列出该持仓 → 报告出来并**退休基线** | 测试 `tests/reconciliation.rs:910` |
| 本 client 自己的成交把它平掉 → **不算「消失」** | 测试 `tests/reconciliation.rs:970` |
| **外部平仓/清算**：清算后仍 Ready 并**停止新风险** | 测试 `tests/reconciliation.rs:1017` |
| 映射不到的市场上的持仓 → 不确定，**不得假定为 0** | 测试 `tests/reconciliation.rs:1496` |
| 没读成功的一轮 → **不碰基线** | 测试 `tests/reconciliation.rs:1183` |
| 成交历史**走完分页**且每条成交只应用一次 | 测试 `tests/reconciliation.rs:3321` |
| 成交分页走不完 → 账户不确定（分页完备性单独判定） | 测试 `tests/reconciliation.rs:3091` |
| venue 报 flat 的中性持仓被读成 flat 并归零 | 测试 `tests/reconciliation.rs:3983` |

### 3.3 R1.2b（F16）— 报告查询契约

| 断言 | 位置 |
|---|---|
| 单笔查询：**venue order id 单独也够**，且按 venue id 原样进入路径（不套 `client:`） | `src/execution.rs:3003`（`(None, Some(venue_order_id)) => venue_order_id.to_string()` 在 `:3011`） |
| 两个 ID 都没有 → **本地拒绝，不发任何请求** | `src/execution.rs:3012`、测试 `tests/execution.rs:3724` |
| venue-id-only 查询真的发出请求并读到订单 | 测试 `tests/execution.rs:3665`（另见 §4.4 注入反证） |
| 批量：`open_only` → `status=open`，`start`/`end` → `startTime`/`endTime` | `src/execution.rs:3058`、测试 `tests/execution.rs:3560` |
| 越出请求过滤器的订单**被报告**，而不是被丢掉 | 测试 `tests/execution.rs:3617` |
| 批量报告从账本（ledger）构建，而不是从响应里现造 | 测试 `tests/execution.rs:3459` |
| 本 client 没下的订单被**报告**而不是丢弃 | 测试 `tests/execution.rs:3416` |
| 查询参数构造与顺序（`market → limit → cursor → status → startTime → endTime`） | `src/http/private.rs:173`（结构）、`:264`/`:268`/`:272`（序列化）、getter `:298`/`:304`/`:310` |
| 参数名常量及其**已归档出处** | `STATUS_PARAM` `src/http/private.rs:108`、`START_TIME_PARAM` `:115`、`END_TIME_PARAM` `:121` |
| 状态枚举（三种拼写） | `OndoOrderHistoryStatus` `src/http/private.rs:145` |
| 契约级断言（固定顺序、三种拼写、未过滤时只有路径本身） | `tests/http_contract.rs:1302` |

### 3.4 有意偏离、两处不对称，以及一条被授权的记录

**(a) 订单侧用服务端窗口，成交侧保留本地窗口——这是有意的不对称。**

- 订单历史：`status`/`startTime`/`endTime` 三个参数都真实存在于 `GET /v1/perps/orders`（冻结 spec），所以**推到服务端**（`src/execution.rs:3058`）。
- 成交历史：冻结 spec 的 `GET /v1/perps/fills` **没有 `status` 参数**，且本阶段没有把成交的时间窗推到服务端——`generate_fill_reports`（`src/execution.rs:3178`）的**行为与 R0 逐字节相同**，只有注释被改写以说明「保留本地窗口是两个过滤器中更保守的那个，不是遗留物」。测试 `tests/execution.rs:3281` 断言这个本地过滤仍然生效（窗口前的一条被过滤、无法排序的一条不被过滤）。

**(b) `open_only` 无法被精确表达——已作为授权记录写进 `test_data/conflicts.md` 第 9 行。**

冻结 spec 的内部矛盾（已逐字核验）：查询参数 `status` 的枚举是 `open` / `canceled` / `fullyfilled`，而 `ApiOrder.status` 的枚举是 `open` / `fullyfilled` / `canceled` / `pending` / `untriggered`。引擎的 `open_only` 语义是「所有非终态」，**包含 `pending` 与 `untriggered`**，而这两个在查询参数里**不存在**。

裁决：`open_only: true` 发成 `status=open`——这是对 venue 词汇的忠实映射，**不是**对该 flag 的精确渲染。残差被记录而非掩盖：`pending`/`untriggered` 的订单无法通过过滤的历史读取得，覆盖它的是**未过滤的账户读与持仓判定**。结果**绝不**在本地二次过滤：越出请求状态的订单照样报告，并**点名**venue 没有执行该过滤器（`src/execution.rs` 历史读的 `open_only` 分支，`log::error!`）。

`conflicts.md` 本阶段**恰好只增加这一行**（+1），可在 §1.1 的 diffstat 里核对。

**(c) `end` 边界的取整**：`UnixNanos::as_millis()` 是整数截断（`crates/core/src/nanos.rs:110`，`self.0 / NANOSECONDS_IN_MILLISECOND`）。因此 `endTime` 在**亚毫秒**精度上会向下取整，窗口上界最多放宽/收窄 1 毫秒（≤1 ms）。已声明，未修——修它需要动 core 的 nanos 语义，超出本阶段授权。

---

## 4. 回归证据

### 4.1 计数逐条对账（§2 的表即证据）

R1 的 746 与 R0 的 712 逐 target 对齐，差额 **+34** 全部可解释：execution +14、reconciliation +18，其余 target **一个不多一个不少**。`http_contract` 保持 77 是因为契约测试在同一个 `fn` 内追加断言而非新开用例。

### 4.2 没有删测试换绿

对 `cef58bc..5b7d2db` 全量 diff 的测试目录做注解与函数名增删统计：

```
  added case annotations: 43      removed case annotations: 0
  added fn test_:         32      removed fn test_:         0
```

逐文件注解数（`#[test]` / `#[rstest]` / `#[tokio::test]`，base vs R1）：

```
  execution        base= 65  R1= 90  delta=+25
  reconciliation   base= 63  R1= 81  delta=+18
  http_contract    base= 37  R1= 37  delta= +0
```

**删除数为 0。** 本阶段没有为了变绿而移除、跳过或 `#[ignore]` 任何既有测试。

### 4.3 rustdoc / clippy 归属：R1 新增 **0** 处

两者都在 HEAD 上是红的，**这不是 R1 造成的**。归属方法：把每一条发现的 `file:line` 与 `git diff -U0 cef58bc..5b7d2db` 的**新增行集合**做成员判定（脚本与结果见 `r1_attribution.txt`）。参照集按 hunk 头累加与按 diff 正文直接计数两种数法一致。

| 仪器 | 退出码 | 发现数（本次口径） | **落在 R1 新增行上的** |
|---|---:|---:|---:|
| rustdoc | 101 | 36 个 `-->` span | **0** |
| clippy | 101 | 38 个不同 `file:line:col` | **0** |

**口径声明（重要，别与 R0 的 29 混用）**：R0 §5.1 报的是 **29** 处，按「cargo 报的 error 条数 + 逐行 blame」计；本报告报的是 `--all-targets` 全量输出里 **38 个不同的 `file:line:col`**，其中包含 cargo 单独成单元的 `(lib)` 与本阶段新编入的 `(lib test)` 两个编译单元的重叠发现：

```
error: could not compile `nautilus-ondo` (lib) due to 18 previous errors
error: could not compile `nautilus-ondo` (lib test) due to 29 previous errors
```

两者**不是同一个计量基础**，所以 38 与 29 的差不是回归。可比的只有归属结论。逐文件分布（本报告口径）：`recording.rs` 16、`websocket/client.rs` 8、`data.rs` 2、`http/client.rs` 2、`http/error.rs` 2、`http/orders.rs` 2、`websocket/parse.rs` 2、`common/credential.rs` 1、`execution.rs` 1、`http/query.rs` 1、`lib.rs` 1——**全部落在 R0 未触及的既有文件里**，唯一的 `execution.rs:615` 是一行未经 R1 改动的既有代码（`self.emit_status_report(...)`，成员判定为 NO），与 R0 报的「execution.rs 1 处既有」一致。

**R1 开工时修掉了它自己引入的那一处**：写 `execution.rs` 时新加的一条 intra-doc 链接指向私有项（`OndoReporter::mark_unappliable_fill`），rustdoc 因此多一条 `private item` 发现。已按不链接的方式改写（`src/execution.rs:289` 附近），所以 R1 的净贡献是 0。

**那 38 处既有发现被有意留着不修**——它们全部落在 R1 未触及的文件里，修它们会让本阶段的 diff 扩散到与 R1 无关的代码，且 plan 没有授权。**它们是后续阶段的候选清理项。**

**为什么 `cargo test` 是绿的而 clippy/doc 是红的**：`.cargo/config.toml` 的 `-Dwarnings` 只提升**该工具本身**发出的 lint。`cargo test` 走普通 rustc，没有 clippy lint 可提升，也没有 `#![deny(rustdoc::broken_intra_doc_links)]` 的文档检查，所以构建干净。**`cargo test` 才是本仓库的门。**

### 4.4 注入反证

「测试会不会真的红」这件事，靠读测试是读不出来的。sub agent 报告它自己做过四条注入反证；**主 session 复跑了其中一条，其余三条未复跑。**

**(a) 主 session 亲自跑的那条——venue order id 单查（R1.2b 的核心新行为）**

把 `src/execution.rs:3011` 的新增臂退回 R1 前的行为（拒绝），然后只跑被它覆盖的那一条测试：

```
test test_a_report_query_by_venue_order_id_alone_is_read_from_the_ledger ... FAILED

thread '...' panicked at crates\adapters\ondo\tests\execution.rs:3696:10:
the order is read: MUTATION INJECTED FOR ACCEPTANCE: a venue-order-id-only read is refused again

test result: FAILED. 0 passed; 1 failed; 0 ignored; 0 measured; 57 filtered out
```

**测试在自己的断言处变红——这条测试是真吃劲的，不是跟着新代码一起写的空转。** 完整记录见 `r1_mutation_counterproof.txt`。回滚后工作区字节与 HEAD blob 完全一致（`execution.rs` md5 `c8ce2920119b19fd04961462d86ee582`），`git status --short -- crates/adapters/ondo/` 无输出，并在回滚后的字节上重跑了两条验收命令（318/0、746/0，退出码均 0）。

> 注意该命令是管道到 `tail` 的，shell 回报的 `$?` 是 `tail` 的退出码、不能用来说明 cargo 的成败；成败以上面的 `test result: FAILED. 0 passed; 1 failed` 为准。

**(b) sub agent 报告、主 session 未复跑的三条**（列为待复核，不作为已验证结论引用）：缓冲按到达序保留每条更新、代际拒绝 superseded 报告、覆盖不足时判不确定而非 Ready。这三条对应的测试确实存在于提交里（`tests/reconciliation.rs:1729`、`:1768`、`:1820`、`:830`），主 session 只核验了它们**存在且断言的正是这些行为**，没有复跑注入。

### 4.5 哪些是主 session 亲自跑的，哪些是转述

| 项 | 谁做的 |
|---|---|
| §2 全部命令与退出码、§1 的全部 SHA/diffstat/md5 | **主 session 亲自跑**，产物在本地目录，可复算 |
| §4.2 的增删统计、§4.3 的归属判定 | **主 session 亲自跑**（脚本 `r1_attribution.txt` 内含全部输入） |
| §4.4(a) 注入反证 | **主 session 亲自跑** |
| §4.4(b) 另外三条注入反证 | **sub agent 报告，未复跑** |
| 测试用例本身的编写 | sub agent；主 session 核验了每一条被引用的 `file:line` 确实是它声称的那条测试（本报告 §3 的全部行号都是逐条打印核对过的） |

---

## 5. 未解决项（明确声明，不掩盖）

| # | 项 | 状态 |
|---|---|---|
| 1 | **rustdoc 与 clippy 在 HEAD 上仍是红的** | 既有状态（R0 起就在）。见 §4.3。R1 归属为 0 |
| 2 | **`open_only` 的残差** | `pending`/`untriggered` 无法通过过滤的历史读取得。已写入 `test_data/conflicts.md` 第 9 行。覆盖它的是未过滤账户读与持仓判定——**这是覆盖，不是等价的替代** |
| 3 | **`endTime` 的亚毫秒取整** | `as_millis()` 整数截断，≤1 ms。见 §3.4(c) |
| 4 | **被丢弃成交在非终态订单上的残留** | `mark_unappliable_fill`（`src/execution.rs:946`）路径在订单非终态时的行为未被本阶段的新测试覆盖到终态收敛；`tests/execution.rs:1719`（终态但读不到成交量的不 settled）与 `:1864`（终态总量不到，保持未解决）是相邻的覆盖，但**不是这一条** |
| 5 | **私有 WS transport 仍然一行未写** | 不是 R1 范围。R1 的缓冲/代际是按「将来接 transport 时直接喂进来」的结构写的，但没有 transport 就没有端到端的流侧验证 |
| 6 | **对真实 venue 零请求** | 全部 fixture 仍是合成的；sandbox 从未被连接。plan §128 明确「不需要真实账号即可完成」，本阶段即按此执行 |
| 7 | **wheel 未重编** | venv 里装的仍是旧 wheel。R1 全是 Rust 侧行为，与 Python 面无关，但**要跑 app 侧集成需先重编 wheel**（记录 SHA256） |
| 8 | ~~一处已识别的 `judge_orders` 重构~~ | **已核实，撤回**（见 §5.1）。取而代之的是一个更小但**真实**的缺口：`AccountReading::orders` 承诺收录「只在流里见过、分页从未列出的订单」，**目前只有文档承诺、没有测试钉住**（见 §5.1 第二张表） |
| 9 | **`r1_mutation_counterproof.txt` 只覆盖一条注入** | 见 §4.4(b) |

### 5.1 更正：`judge_orders` 并没有「在判定时重新推导」

**本报告提交后的第一次复核发现：本节原先的第 8 项（`judge_orders` 在判定时重新推导订单状态，而非消费合并后的读）在当前字节上不成立，已撤回。** 该项的原文也出现在了携带本报告的提交 `cd959df` 的 message 里——提交已合并，不回改历史，以本节的更正为准。

核实过程（全部按 `5b7d2db` 的冻结字节实读）：

| 取证 | 位置 | 说明 |
|---|---|---|
| 判定的输入**就是**读里的字段 | `src/reconciliation.rs:2163` | `judge_orders` 遍历 `reading.orders`，逐条比较 `Some(venue_filled) != order.applied_filled`，`venue_filled`/`applied_filled`/`status` 都是读里的现成字段，没有任何回查账本的动作 |
| 读本身保证是**合并态** | `src/reconciliation.rs:922-928` | `AccountReading::orders` 的文档写死：「The state is the **merged** one, not the page's … An order only the stream mentioned is listed here too」 |
| 构造读时**优先取账本** | `src/execution.rs:2106-2110`（文档）、`:2111-2142`（`order_reading`） | `status` 与 `venue_filled` 在订单被跟踪时**取自订单索引**，只有未跟踪的订单才回落到 payload；文档说明理由是「the pass judges the state it left behind rather than the state one of its inputs described」 |
| 账本侧的同一问题，不与判定重复报告 | `src/execution.rs:775-801` | 账本路径只在「打开分歧的那条 payload」上记一次日志（文档明写 `rather than on every read of it afterwards`），与 `judge_orders` 产出的 `Finding` 是两条不同通道，二者的一致性由 `src/execution.rs:346`、`:363` 的文档声明并指向 `judge_orders` |

结论：plan 对 R1.1 的「final `AccountReading`/judgment from merged state including stream-only orders」这一条**在本阶段已实现**：判定消费的读由 `order_reading` 从账本索引建成，而账本索引吸收流报告与分页两种来源，所以「合并态」是有据可查的，不是声称。

**但测试覆盖要分开说，不能含糊：**

| 被测到的 | 测试 |
|---|---|
| 账户读期间到达的流报告被重放一次、进入那一轮的读 | `tests/reconciliation.rs:3482` |
| 同一订单的多条流报告按到达序重放、后者胜 | `tests/reconciliation.rs:3569` |
| 无法落地的报告 → 有界重读，不静默丢弃 | `tests/reconciliation.rs:1907` |

| **没有被测到的** | 说明 |
|---|---|
| 「一个**只在流里出现过**、venue 的分页从未列出的订单，确实出现在 `AccountReading::orders` 里」 | 这一条目前**只有 `src/reconciliation.rs:922-928` 的文档承诺，没有测试钉住**。测试用的读由 `clean_reading()`（`tests/reconciliation.rs:201`）与 `long_position()`（`:873`）这类助手直接构造，不经过「分页 + 流」两条来源的合并路径 |

**这个缺口比原先第 8 项那个说法更需要下一阶段处理**：它是「文档承诺了、测试没钉住」的一类，而不是「代码有缺陷」。原先第 8 项（以及 `cd959df` 提交信息里那句）的撤回理由见上；**更正本身也在第一次写下时引错过一条测试**（曾把「只有流见过」归给 `tests/reconciliation.rs:3655`，而那条实际是「重复帧只应用一次」），一并撤回。

---

## 6. 下一阶段入口

**R1 的入口条件已满足，R2..R5 未开始。**

- R1 的两条验收命令退出码 0；fork `onde-perps` 除 40 个 CRLF-only `.pyi` 外干净；8 个文件的改动已 `--ff-only` 合并
- R1 **没有**改 admission gate 的落点（R0 的 `admission()` / `revalidate()` 仍是唯一准入入口），也没有改 `allow_production_orders` 的拒绝路径
- **F05（账户余额与持仓未接到真实 Nautilus 输出）本阶段未动**——它是 P1 交付阻断，且与「私有 WS transport 不存在」是同一件事的两面（见 §5.5）。
- 本报告 §5.1 撤回了一项原先的「下一阶段第一件事」（原第 8 项不成立），并给出一个更小的真实缺口：`AccountReading::orders` 的「只在流里见过的订单也收录」目前**只有文档承诺、没有测试钉住**。**建议作为下一阶段的第一个提交**——补一条走「分页 + 流」两条来源合并路径的测试，不需要改生产代码

**本目录的工件清单**：

| 文件 | 内容 |
|---|---|
| `environment.txt` | 运行环境与两个起点 SHA |
| `r1_plan.txt` | **plan 指定的验收命令**（R0 命令 + `--test http_contract`）完整输出，含命令行 |
| `r1_suite.txt` | 全 crate 套件输出（746 passed / 退出码 0），含命令行 |
| `r1_fmt.txt` | 格式检查（退出码 0），含命令行 |
| `r1_doc.txt` | rustdoc 完整输出（36 span / 退出码 101），含命令行 |
| `r1_clippy.txt` | clippy 完整输出（退出码 101），含命令行 |
| `clippy_pairs.txt` | 从 `r1_clippy.txt` 抽出的 38 个不同 `file:line:col` |
| `r1_attribution.txt` | **归属判定**：两种仪器的每一条发现是否落在 R1 新增行上（结论：0） |
| `r1_mutation_counterproof.txt` | **注入反证**的完整记录与回滚核验（§4.4a） |
| `app_head_after_r1.txt` | APP 侧终点 SHA |

**安全声明**：本目录只含 URL、路径、测试计数与源码行号。**不含账户信息、不含密钥、不含任何签名或登录帧。** 按 plan §0 要求，这些内容不得进入报告、公共原始磁带或 Git——本阶段未产生任何此类内容。

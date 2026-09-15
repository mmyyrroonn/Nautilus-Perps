# R2 阶段验收报告 — 行情状态、录制与回放修正

**run id**：`20260915T153817Z-r2`
**阶段**：R2（plan `docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md` 的 R2.1 / R2.2 / R2.3）
**对应 review 发现**：F06、F07、F08、F09、F10、F11、F12、F13、F18（`reports/ondo-code-review-2026-09-15.md`）
**本报告作者**：主 session。§3 的全部独立复现、§4 的全部注入反证、§2 的全部命令与退出码由主 session **亲自执行**；三个 sub agent 的实现工作已在 §5 逐条标注哪些经主 session 复核、哪些只是转述。

**本阶段不授权主网交易、资金转移或常驻部署。** 全程未向任何真实 venue 发出请求，未使用任何真实账号。按 plan §0，本目录不含账户信息、密钥、签名或登录帧。

---

## 1. 两个 SHA 与脏范围

### 1.1 APP `E:\Nautilus-Perps`，分支 `task/ondo-r2-market-truth`

| | commit | 说明 |
|---|---|---|
| 起点（= `main`） | `23f68bcd39f3cf7c8a2cf3e3ac3e6bf41a903dc7` | R1 收尾（reports: state the stream-only coverage gap …） |
| **终点** | 见本目录 `app_head_after_r2.txt` | R2 全部改动 + 本报告 |

改动 **8 个文件，+3524 −379**：

```
src/analysis/ondo_depth.py   | 1023 ++++++++++++++++++++++++++++++++---------
src/market_tape.py           |  145 +++++-
src/ondo_preflight.py        |  248 ++++++++--
src/spread_watch.py          |  562 ++++++++++++++++++++++---
tests/test_market_tape.py    |  305 +++++++++++-
tests/test_ondo_depth.py     |  842 ++++++++++++++++++++++++++++++++++--
tests/test_ondo_preflight.py |  252 ++++++++-
tests/test_spread_watch.py   |  526 +++++++++++++++++++++-
```

（`ondo_preflight.py` 与 `tests/test_ondo_preflight.py` 的计数含 §5.5 那处主 session 自己补的 run-identity 校验。）

**脏范围声明**：`reports/stage1/*.csv`、`reports/perps-*`、`reports/polymarket-*`、`reports/entropy-*`、`.commandcode/`、`学习课题/` 等既有 untracked 文件保持原归属，**本阶段未暂存、未提交、未清理**。本阶段只 `git add` 上面 8 个文件加本目录。完整清单见 `app_state.txt`。

### 1.2 FORK `E:\nautilus_trader`，分支 `task/ondo-r2-market-truth`

| | commit | 说明 |
|---|---|---|
| 起点（= `onde-perps`） | `5b7d2db6694c8f1707482d07ce83de19888213c7` | R1 收尾（square the account across recovery …） |
| **终点** | `3133e2501b990745655d2b6226801099b60e6b77` | R2 的 adapter 侧改动 |

改动 **10 个文件，+2205 −97**（全部在 `crates/adapters/ondo/` 内）：

```
src/data.rs             | 883 +++++++++++++++++++++++++--
src/factories.rs        | 311 +++++++++-
src/http/rate_limit.rs  |  66 +-
src/python/factories.rs |  19 +-
src/python/http.rs      |   7 +-
src/websocket/book.rs   |  15 +
src/websocket/client.rs | 326 +++++++++-
tests/execution.rs      |  27 +
tests/market_data.rs    | 461 +++++++++++++-
tests/python.rs         | 187 +++++-
```

**脏范围声明**：该 fork 工作区长期有 40 个 CRLF-only 的 `.pyi` 显示为 ` M`（R0 起既有）。它们**没有被暂存、没有被提交、也没有被修改** —— 本次提交是按路径逐个 `git add` 的 ondo 文件，不是 `git add -A`。无跨 crate 改动，无 lockfile 改动，无配置改动（见 `fork_state.txt` 末尾那条空输出）。

---

## 2. 验收命令与退出码

**以下每条都由主 session 在最终字节上亲自跑过**，完整输出在同名产物里，产物头部写有命令行本身。

### 2.1 APP

| 命令 | 退出码 | 产物 |
|---|---|---|
| plan §R2 指定的验收命令（5 个测试文件） | **0**（244 passed） | `r2_plan_app.txt` |
| 全套 APP 回归 `pytest tests -q`（plan：R2 完成后全套回归一次） | **0**（704 passed, 1 warning, 97 subtests） | `r2_full_suite.txt` |
| **主 session 自写的独立复现**（见 §3） | **0**（6/6 findings green） | `independent_repro_postfix.txt` |
| 同一份独立复现在**修复前的干净副本**上 | **1**（0/6 green） | `independent_repro_prefix_baseline.txt` |

### 2.2 FORK

同一条纪律：**以下每条都由主 session 在最终字节上亲自跑过**，完整输出在同名产物里，产物头部写有命令行本身。

| 命令 | 退出码 | 产物 |
|---|---|---|
| plan §R2 指定的 `cargo +1.98.0 test -p nautilus-ondo --locked --offline --test market_data` | **0**（43 passed, 0 failed） | `fork_market_data.txt` |
| 整 crate 回归 `cargo +1.98.0 test -p nautilus-ondo --locked --offline` | **0**（763 passed, 0 failed） | `fork_full_suite.txt` |
| `cargo +1.98.0 fmt -p nautilus-ondo -- --check` | **0** | `fork_fmt_check.txt` |
| 三个新测试**逐一按名**跑（不靠总数推断） | **0**（2 + 1 passed） | `fork_metadata_version_tests.txt` |
| **主 session 自己的注入反证**（见 §8.2） | 变红：3 failed；恢复后复跑 **0** | `fork_metadata_version_counterproof.txt` |
| `--features python --test python`（严格构建） | **101**（依赖侧 warning 被拒，见下） | `fork_python_feature.txt` |
| 同一条，`CARGO_BUILD_WARNINGS=allow`（**诊断构建，不是严格构建**） | **0**（5 passed） | 同上 |

整 crate 的 763 = lib 400 + execution 59 + http_client 39 + http_contract 77 + market_data 43 + reconciliation 81 + signing 63 + doc-test 1，**每一组都 0 failed**。

**关于那两条 python 构建**：严格构建**失败在依赖**，与本阶段改动无关，三条证据都在产物头部写明 —— ① 报错点名的是依赖 `nautilus-persistence-macros`（一条 MSVC 链接器消息被 `.cargo/config.toml` 的 `build.warnings = "deny"` 拒绝）；② 严格输出里 `Compiling` 行**一条都没有**（不止是没有 `nautilus-ondo`），即 cargo 根本没走到本 crate；③ 本阶段对 `Cargo.toml` / `Cargo.lock` 零改动，且 `crates/adapters/ondo/` 之外无任何改动。所以这条失败**不是这些改动能触及的**。严格构建过不去时，用 `CARGO_BUILD_WARNINGS=allow` 的诊断构建证明了 Python 面的 5 个测试确实是过的 —— 但产物里明确标了那不是严格构建，不拿它冒充。

### 2.3 环境

`environment.txt` 记录运行环境、两个起点 SHA 与已安装 wheel 的 SHA256。

---

## 3. 我把独立复现写成了可证伪的（这是本阶段证据的主干）

sub agent 会写自己的测试，而那些测试是跟着它自己的实现一起写的。为了不把「它说自己修好了」当成证据，主 session 自己写了 `independent_repro.py`：它**断言修复后应然的行为**，不引用任何 sub agent 的测试，不依赖任何内部实现细节，并且**在修复前的干净副本上必须变红**。

修复前的副本来自 `git archive HEAD`（APP 起点 `23f68bc`）解包到 `%TEMP%\r2-prefix-check`，与工作树是两份独立字节。

| 检查 | 修复前（红） | 修复后（绿） |
|---|---|---|
| **F06** 运行中改 metadata 不得回改更早 arrival 的成本 | 全部 6 行都按最终值 **100.9** 计费 | seq4 是 **3.4**，seq6/7 是 **100.9** |
| **F07** 真实停牌必须活过 feed 重连 | reasons `['disconnected','no_book','stale_book']`——**没有 market_halted**，HALT 被整个忽略 | reasons 含 **`market_halted`** |
| **F08a** 只有 `size_precision` 的 tape 不得反推步长 | 步长被推成 **0.001**（`10**-3`） | 步长 unknown，reason **`quantity_step_unknown`** |
| **F08b** 真实增量 0.002/0.003 → 公共步长 | **0.001** | **0.006** |
| **F09** event age 用本地 epoch 接收时刻算 | 两腿都延迟 120 s 时读成 **0** | **120001 ms** |
| **F10** 满队列不得堵死 deadline flush | writer **根本没有** deadline 排出入口（dropped=2, records=1） | `tick()` 在 t=0.5 s 不写、t=5 s 无新记录也排出 |
| **F11** 旧 session 截断尾行不得遮住后续完整 session | 3 条记录，**没有一条来自新 session** | 新 session 的 **4 条**记录读到 |

**结论：6/6 在主 session 自己的断言下由红转绿。**

### 3.1 一条我写错、并且必须说明的：F18 我**没有**独立复现出来

我最初也给 F18 写了一条 tape 级检查（「重连后第一帧快照应当可用」）。**它在修复前的副本上也是绿的**，所以它**不是** F18 的证据——它自己绕过 bug：F18 的缺陷在 watcher 的写入路径（`SpreadWatch._record_book_batch` 把 `feed_ready` 折进记录的 `valid`），而我的脚本是**直接往 tape 写记录**的，恰好跳过了那段错误代码。

我没有把它塞进通过数里充数，而是把它从 `CHECKS` 里拿出来并写明理由。**F18 的证据是 §4 的注入反证**，不是这条检查。

---

## 4. 注入反证（「测试会不会真的红」）

读测试是读不出这件事的。主 session 亲自做了两次：把修复的**某一个 hunk** 在**一次性副本**里退回修复前的写法，再跑声称钉住它的那些测试。

### 4.1 F18

副本 `%TEMP%\r2-mutation`，只退回 `_record_book_batch` 里的 deltas 那一段（depth10 那一段**故意保持修复后**，用来隔离）：

```
feed_ok = leg.book_valid and leg.feed_ready
valid=None if feed_ok else False,
invalid_reason=None if feed_ok else "feed-invalidated",
```

结果（`f18_mutation_counterproof.txt`）：

```
FAILED tests/test_spread_watch.py::test_the_first_book_after_a_recovery_snapshot_is_valid_on_the_tape
FAILED tests/test_spread_watch.py::test_a_book_record_never_carries_the_feed_state_as_its_validity
2 failed, 85 deselected        exit code: 1
```

两条测试都在**自己的断言处**变红（`assert "feed-invalidated" not in json.dumps(rows)`），不是别的原因顺带挂掉。

### 4.2 F12

副本 `%TEMP%\r2-mutation-f12`，只退回 `write_report` 的最后一处：发布态 `<out>/meta.json` 只在成功时写（即修复前的「meta 仅 complete 时写」规则），run-id 暂存、不可变 run 目录、hash 全部保持修复后：

```
5 failed, 31 passed        exit code: 1
```

其中包含 review 复现的那一条：

```
FAILED tests/test_ondo_preflight.py::test_a_failed_run_after_a_success_never_leaves_the_old_success_in_place
FAILED tests/test_ondo_preflight.py::test_a_transport_failure_after_a_success_publishes_that_failure
FAILED tests/test_ondo_preflight.py::test_a_crash_in_the_middle_of_publishing_is_visible_to_the_reader
FAILED tests/test_ondo_preflight.py::test_a_failed_run_never_leaves_a_half_report
FAILED tests/test_ondo_preflight.py::test_a_403_shaped_error_from_any_endpoint_fails
```

### 4.3 反证用的副本已删除

`%TEMP%\r2-mutation*` 两个副本在取到输出后即删除；仓库工作树**从未**被这两个注入碰过（反证全部在副本里做，就是为了不出现「注入后忘了回滚」这一类事故）。

---

## 5. 每个发现改了什么，以及哪些是我亲自核实的

### 5.1 R2.2 / R2.3（F06/F07/F08/F09/F10/F11/F12/F18）— 主 session 独立复现或注入反证

| 发现 | 修复位置 | 我的证据 |
|---|---|---|
| F06 | `src/analysis/ondo_depth.py` 逐 arrival 应用 metadata | §3 独立复现（红→绿） |
| F07 | `ondo_depth.py` 三轴分离；`spread_watch.py` 按 reason 分派 | §3 独立复现（红→绿） |
| F08 | 步长只来自真实 `size_increment`；`venue_step()` 从模块删除 | §3 独立复现 F08a/F08b |
| F09 | `event_age_ms` 用当前记录的本地 epoch `ts_init_ns` − 该腿 book `ts_event_ns` | §3 独立复现 |
| F10 | `market_tape.py` 新增**调用方驱动**的 `tick()` / `_flush_if_due()`；deadline 判定排在容量判定**之前** | §3 独立复现 |
| F11 | `_truncated_tail` 只结束当前 fragment；reader 在所有退出路径关句柄 | §3 独立复现 + §5.4 句柄验证 |
| F12 | `ondo_preflight.py` 按 run_id 暂存 → 整体改名 → 最后原子替换 `meta.json`；新增 `verify_run()` | §4.2 注入反证（红）+ §5.5 |
| F18 | `spread_watch.py` 记录不再携带 feed 状态作为 `valid`；`websocket/book.rs` 新增 `invalidate()` | §4.1 注入反证（红） |

### 5.2 契约符合性（主 session 读真实代码核实，不是转述）

冻结的跨仓契约（instrument 记录 metadata 新增 `size_increment` / `price_increment` / `metadata_version` / `metadata_available_ns`；record 级 `source ∈ {instrument_metadata, instrument_update}`；`valid=false` + `invalid_reason="metadata_stale"`；`adapter:metadata_stale` / `adapter:metadata_ready` 走 `action=None` 且只动 metadata 轴）——主 session 逐条读 `src/spread_watch.py:1884-1901`、`:1976-1991` 与 `tests/test_spread_watch.py` 的断言核实：**分派确实按 reason 字符串，任何其它 `adapter:` 前缀的通知被显式判为「不是恢复」，其余才走真实交易状态**。

`"feed-invalidated"` 作为 book 记录的 `invalid_reason` 已消失，只剩 `ondo_depth.py:194` 的 `V1_FEED_INVALIDATED` —— 那是给 v1 tape 的解释性常量，不是写出去的判定。

### 5.3 R2.1 的 adapter 侧（F13 / F07-upstream / F18-upstream）

见 §6。要点：这三条在 fork 上**已实现并有测试**，命令与退出码见 §2.2；主 session 读代码核实了 F13 的共享预算路径、F07 的三轴判据表、F18 的 `conversion_change` 边界。**另有一处不在 review 18 项内的接缝缺口由主 session 发现、r2-adapter 收口、主 session 反证**（§6.2 / §8.2）。

### 5.4 F11 第二半：Windows 句柄（review 记载读过的临时文件无法重命名/删除）

主 session 亲自验（`f11_handle_leak.txt`）：读一条**尾行被截断**的 tape（正是旧代码泄漏句柄的那条路径），分别在「读到底」与「取一条就丢弃 generator」两种走法之后，对刚读过的文件做 **重命名 + 删除**：两条路径都成功。**没有句柄泄漏。**

### 5.5 主 session 亲自补的一处：`verify_run` 只**报告** run identity，没有**校验**它

这是本报告里唯一一处由主 session（而不是 sub agent）改的产品代码，因此单独说明。

**怎么发现的**：我自己探 `verify_run` 的抗篡改能力时，把发布态 `meta.json` 里的 `run_id` 从 `r4` 改成 `r999`（而 `run_dir` 仍写着 `runs/r4`、`runs/r999` 根本不存在），结果：

```
verified = True | run_id = r999 | problems = []
```

payload 被改、hash 被改**都**能被抓到，唯独 identity 不一致抓不到 —— 函数**报告**了它从未**校验**的身份。plan §R2.3 的原话是「读取者必须校验 run identity 与 hashes」，函数自己的 docstring 也写着「Check the published run against the identity and hashes its own manifest names」。**hash 校验了，identity 只被复述。**

**修法**（`src/ondo_preflight.py` 的 `verify_run`）：manifest 自己带着 identity 的两半（`run_id` 与 `run_dir`），要求两半指向同一次 attempt，不一致就进 `problems`。**刻意只比对 manifest 内部的两半**，不去要求 `runs/` 目录树存在 —— 发布态视图（`<out>/*.json` + `meta.json`）是可以被单独拷走阅读的，要求磁盘上还有 `runs/<id>` 会让副本读不了。

**回归**：`tests/test_ondo_preflight.py::test_a_manifest_whose_run_id_and_run_dir_disagree_is_not_verified`，并做了注入反证（`run_identity_counterproof.txt`）：把这段检查从副本里删掉，**恰好这一条**测试变红（`1 failed, 36 passed`）。

**这不在 review 的 18 项里**，是 F12 修复的一处不完整；规模很小（十余行），但按 plan 的措辞它属于 R2.3 的要求，所以补在这里而不是留给下一阶段。

---

## 6. FORK 侧结果

**分支** `task/ondo-r2-market-truth`（起点 `onde-perps` = `5b7d2db`，终点 `3133e25`），改动全部在 `crates/adapters/ondo/` 内。
**命令与退出码见 §2.2**；下面写的是每条改动**做了什么**，以及**哪些是我读代码核实过的**。

### 6.1 R2.1 的三条改动（对应 F13 / F07-upstream / F18-upstream）

**F13 — 共享 REST 预算（`http/rate_limit.rs`、`factories.rs`）**

进程级注册表 `ENVIRONMENT_BUDGETS`，键是 `OndoEnvironment`；`shared_rest_budget(env)` 返回同一个 `Arc<RateLimiter>`。`OndoRateBudget` 内部就是一个 `Arc`，clone 即共享，`OndoRateBudget::new()` 仍是"要一个独立桶"的正规入口（测试与 Rust embedder 用）。

关键在于**工厂怎么拿到它**：两个 factory 各存一个私有 `environment: Option<OndoEnvironment>`，`create()` 走 `budget_for(config 里写的环境)` —— 显式注入的桶照用，否则去注册表按**配置实际写的环境**解析。这就是 F13 的要害：Python 侧 `OndoDataClientFactory()` 与 `OndoExecutionClientFactory()` 是**各自独立构造**的，之前必须由调用方手动把同一个桶传过来（`python/factories.rs` 的旧文档正是这么写的），现在不需要了。

我读过、且认为**强**的那个测试：`factories.rs` 里 `test_the_client_a_factory_built_waits_on_the_bucket_of_the_environment_it_named` —— 两个 factory 都用 `new()`（即 Python 注册路径的构造方式），配置名的是它们**没有**绑定的那个环境，然后**不手工传桶**，靠"该环境的桶那一格被花掉了"来判定。这比"比较 Rust 手工传进去的同一个 `Arc`"强，因为它走的正是 Python 的真实路径。

**F07-upstream — 三条轴分开传播（`data.rs`）**

`InstrumentStatus` 一条 carrier 上现在跑三件互不相干的事，靠字段区分（`data.rs:50-68` 的表就是契约本身）：

| 条件 | 判据 |
|---|---|
| 本 adapter 的行情可用性 | `action=None`，`reason` = `adapter:disconnected` / `adapter:snapshot_ready`，`is_quoting` 说明状态 |
| **venue 自己的交易状态** | **真实** `MarketStatusAction` + `is_trading`，**永不**为 `None` |
| 已接受的 metadata 版本是否仍可信 | `action=None`，`reason` = `adapter:metadata_stale` / `adapter:metadata_ready`，`is_trading=None` |

发布口收敛成一个 `publish_read_outcome`（`data.rs:1373`），首次 load 与周期 refresh 都走它，所以两者不会漂。文档里写死的两条语义我核过：`snapshot_ready` **不**解除 venue halt，`metadata_ready` 也**不**解除 —— 只有 venue 自己的状态能把 market 轴推回 `Trading`。

**F18-upstream — 转换变化时重建 book（`websocket/client.rs`、`websocket/book.rs`）**

`register_instrument` 变成更新式：转换相关属性（价格/数量精度、价格/数量增量）变了就 `invalidate` 该 market 的 book、计数、并等下一帧快照重建。**费用变化不重建** —— `conversion_change()` 只比转换，因为费用不参与解码。`invalidate()` 保持 session 不变（`book.rs:366`），这正是"转换变了"与"断线了"的区别：后者走 `invalidate_to(new_session)`。

### 6.2 接缝缺口：主 session 发现、r2-adapter 收口、主 session 反证

这一条**不在 review 的 18 项里**，是主 session 跨仓对读时发现的（完整经过与判定见 §8.2）。摘要：

- **缺口**：adapter 成功率刷新时确实重发 instrument，但从不给 instrument 写 `info`，于是 APP 读 `info["metadata_version"]` 永远取不到 → 实盘录制里版本号**恒为 null**，plan R2 的「记录 metadata 的版本」端到端未满足，而**两侧测试各自都是绿的**。
- **收口**（`data.rs`）：`OndoMarketMetadata::apply` 在两个接受分支把版本盖到 instrument 的 `info` 上（`stamp_metadata_version` `data.rs:198`，键名常量 `data.rs:153`）；`KeptPrevious` 在 `change_against` 命中后**直接 return，结构上碰不到盖章**（`data.rs:296-299`）—— 这就是"保留的版本不被重新编号"的保证，不是靠测试碰巧覆盖。两个载体（`load_selected` 填 store 的那份、`apply_and_publish` 在 `Refreshed` 重发的那份）现在都从**同一批已盖章的对象**（`accepted_instruments` `data.rs:1343`）发布。
- **主 session 的注入反证**（`fork_metadata_version_counterproof.txt`）：把 `METADATA_VERSION_KEY` 的值改成 `"metadata_version_MUTATED"`（盖章照做，只是盖到没人读的键上），三个新测试**全部在自己的断言处变红**（`left: None / right: Some(1)`），不是在别处顺带挂掉；恢复后**该文件 sha256 与改动前逐字节相同**（`eea550cd…`），测试复绿。反证是就地做、按哈希验证复原的，工作树没有留下注入。

我另核过：三个测试的辅助函数 `version_of` / `metadata_version_of` 读的都是 `perp.info["metadata_version"]` —— **与 Python 应用读的是同一个键**，所以这些测试钉的是真 carrier，不是内部字段。集成测试还额外断言重发的那份 `maker_fee == "0.0002"`，排除了"旧副本换个号"这种假通过。

### 6.3 r2-adapter 自己声明、我采信并记录的边界（未解决）

| # | 边界 | 我的判断 |
|---|---|---|
| 1 | **refresh 的 60s 计时没有任何测试驱动**。它验的是每一 tick 调用的那个函数 `apply_and_publish` 本身；集成测试里"Refreshed 重发"走的是 `load_all` 那条路（与 refresh 同源、同盖章），不是 refresh 任务那条 loop | **采信，且认为这是本阶段最实在的一处测试缺口。** 要补真 loop 测试得把 interval 变成可注入参数，超出本项范围，r2-adapter 按边界回来问了 —— 这是对的做法。留作 R5.1 之后可补项 |
| 2 | 一次**被拒绝**的运行时 `load_all/load_ids/load` 之后，provider store 里仍是那次 read 的 instrument（**改动前既有行为**），它们不带版本号 | 可达条件是"app 在 connect 之后显式再 load"，而 app 现在每腿只 `subscribe_instrument` 一次，**当前不可达**。收紧它 = 改 provider store 语义，留待需要时再定 |
| 3 | refresh 成功后 provider store 不跟着更新（refresh 任务拿不到 store）；只有"refresh 之后才新订阅"才看得到旧副本 | 同上，当前不可达 |

这三条都是**既有**问题、不是本阶段引入的，且 r2-adapter 是**自己主动提出**而不是被我查出来的 —— 这一点我记一功，但也正因为是自述，上表只写"采信"不写"已核实"。

### 6.4 我核实过的 / 我没有核实的

| 项 | 状态 |
|---|---|
| `--features python` 严格构建失败归因 | **我复算过**（§2.2 三条证据） |
| clippy 的**范围**（新增代码有没有引入新发现） | **我核实过**（`fork_clippy_scope.txt`）：`data.rs` 与 `tests/market_data.rs` **零发现**；本阶段碰过的文件里只有 `websocket/client.rs` 有发现（6 条），而 `git blame` 把这 6 行的作者全部指到 `5c2ba5a`（添加本 crate 的那次提交），**没有一条是 R2 的** |
| clippy 的**总数**（它报 lib 17 / lib test 28） | **我没有逐条复算**。范围已核实，总数未复算；两个门在本仓库 HEAD 本来就是红的 |
| rustdoc | **我没有跑**，因此本报告不为它作任何声明 —— 采信它的"零发现"，但那只是它的自述 |

---

## 7. 没有删测试换绿

对 APP 测试目录的增删统计：

```
added def test_:  46      removed def test_:  2
added assert:    333      removed assert:    11
```

**被删的 2 个测试函数逐一核对过，都是在钉住错误行为的测试上改写（不是移除覆盖）**：

| 被改写的测试 | 改写后 | 理由 |
|---|---|---|
| `test_a_feed_disconnect_marks_the_book_record_invalid` | `test_a_caller_may_mark_a_record_invalid_with_a_reason_of_its_own` | 旧测试钉住 F18 的错误行为（feed 状态折进记录 `valid`） |
| `test_venue_step_comes_from_the_published_size_precision` | `test_a_quantity_step_is_never_inferred_from_the_size_precision` | 旧测试钉住 F08 的错误行为（从 precision 反推步长） |

**被删的 11 条 assert 也逐条核对过，全部由更强的断言替代**，例如 preflight 那条从「失败时 `meta.json` 不存在」（弱：不存在什么都证明不了）改成「`meta.json` 必须存在、必须 `complete=false`、必须点名 failure、必须 `files == {}`、且 `verify_run()` 必须不通过」。

---

## 8. 未解决项（明确声明，不掩盖）

| # | 项 | 状态 |
|---|---|---|
| 1 | **跨仓端到端链路未在本机字节上验证** | plan §R2.1 要求「parser → message bus → watcher → tape → replay」的跨仓集成测试。这条链路要经过 `.pyd`，**必须先重编 wheel 并装进 venv**，而本阶段按 plan 的阶段表（R2「可独立离线开发」）保持离线，未重编 wheel。**因此：APP 侧（writer/reader/replay/preflight）与 FORK 侧（adapter 单侧）各自被验证了，两者之间的接缝没有被本阶段的任何命令覆盖。**（接缝的**静态**对读见 §8.2，那不等于运行验证）见下方 §8.1 |
| 2 | **F18 没有主 session 的独立复现** | 只有 §4.1 的注入反证。§3.1 说明了为什么 tape 级脚本复现不到它 |
| 3 | **F13 的「不是只比较 Rust 手工传入同一个 Arc」** | **已闭环**：见 §6.1 —— 那个测试用 `new()`（Python 注册路径的构造方式）建两个 factory，不手工传桶，靠"该环境的桶被花掉了一格"判定 |
| 4 | **venv 里的 wheel 仍是旧的** | R1 报告 §5 #7 的同一项，本阶段未处理（R5.1 的范围）。它正是 #1 无法闭合的原因 |
| 5 | **私有 WS transport 仍然一行未写** | 不是 R2 范围（R3） |
| 6 | **对真实 venue 零请求** | 全部 fixture 仍是合成的；sandbox 从未被连接。plan §R2 即按离线执行 |
| 7 | **历史 tape 重放** | plan 把「用修正分析器重放已有 ONDO–HL tape 并对比前后差异」放在 R5.2。本阶段**未**重放历史 tape，也未把旧分析标记 superseded |

### 8.1 关于未解决项 #1 的边界声明（重要）

本阶段能证明的是：

- **APP 侧可信**：writer/reader 的 schema v1/v2 兼容、queue/deadline/rotation、preflight 的 run 隔离与 hash 校验、replay 的逐 arrival metadata、三轴分离、步长与时钟口径 —— 都由 §3 的独立复现或 §4 的注入反证钉住。
- **FORK 侧可信**：adapter 单侧的 metadata store、instrument 更新、共享预算 —— 由 §6 的 cargo 命令钉住。
- **接缝的静态一致性**（§8.2）：两侧 carrier 的**名字、类型与方向**已经逐条对读核实。

本阶段**不能**证明的是：**这两侧接起来之后仍然是对的**。契约是我在 R2 开工前冻结并交给两侧各自实现的，两侧都按契约写了测试，但**契约的两端从未在一次运行里见过面**。这一条必须在 R5.1 重编 wheel 之后补一次真实的端到端链路测试，**不能因为两侧各自的测试都绿就算通过**。

### 8.2 接缝的静态对读（§8.1 那句「没验证」的收窄）

写 §8.1 时我说这条接缝「没有被本阶段的任何命令覆盖」。这句话对**运行**成立，但对**读代码**不成立 —— 两侧的 carrier 是可以在不编 wheel 的前提下逐条对读的。我做了这件事，结论如下（这是本报告里**唯一**一处靠跨仓对读而非命令得到的判断，因此单列）：

| 接缝 | 发布方（FORK） | 读取方（APP） | 判定 |
|---|---|---|---|
| metadata 失效/恢复 | `REASON_METADATA_STALE` / `REASON_METADATA_READY`，走 `Data::InstrumentStatus` + `action=None` + `is_trading=None`（`data.rs:135,143`；发布口 `publish_read_outcome` `data.rs:1373`） | `reason` **精确等值**比较，无 strip/lower（`spread_watch.py:1871,1887-1890`） | ✅ 一致 |
| venue 真实交易状态 | 真实 `MarketStatusAction` + `is_trading`，与上面**不同一条事件**（`venue_status` `data.rs:1285`） | 除四条已知 reason 外一律走真实交易状态分支 | ✅ 一致 |
| 运行中 instrument 更新 | **成功刷新会重发 Instrument**：`apply_and_publish` 在 `Refreshed` 分支先发 `DataEvent::Instrument`（`data.rs:1466-1472`），再调 `publish_read_outcome` | `on_instrument()` → `_refresh_instrument_metadata()`（`spread_watch.py:1843-1856`） | ✅ 一致，**顺序也对**（Instrument 先于描述它的 status） |
| 可用时刻 | 不写 `info["metadata_available_ns"]`；但 `read_markets` 把**本地接收时刻** `ts_init` 传进 `parse_instruments` | 回退读 `instrument.ts_init`（`spread_watch.py:1090-1111`） | ✅ 可用：回退路径正是设计意图，且语义正确（本地接收时刻，非 venue 事件时间） |
| metadata 版本 | `OndoMarketMetadata::apply` 在两个接受分支把版本**盖在 instrument 的 `info` 上**（`stamp_metadata_version` `data.rs:198`，键名常量 `data.rs:153`）；`KeptPrevious` 在盖章**之前** return（`data.rs:296-299`） | 读 `info["metadata_version"]`（`spread_watch.py:1077-1087`） | ✅ **已闭合**（见下） |

**这一行对读抓出了一个两侧测试都绿的接缝缺口**：adapter 原本从不给 instrument 写 `info`，所以 `info["metadata_version"]` 永远取不到，回退属性在 Nautilus 的 instrument 上也不存在 —— **实盘录制里 `metadata_version` 会恒为 null**，plan R2 要求的「记录 metadata 的版本」在端到端意义上没有被满足，尽管两侧的测试各自是绿的。这正是 §8.1 担心的那类问题，而它是**读得出来的**，不必等到 R5.1。

已交给 r2-adapter 收口，且主 session **自己做了注入反证**（`fork_metadata_version_counterproof.txt`）：把 `METADATA_VERSION_KEY` 的值从 `"metadata_version"` 改成 `"metadata_version_MUTATED"`（盖章照做，只是盖到没人读的键上），三个新测试**全部在自己的断言处变红**（`left: None / right: Some(1)`）；恢复后该文件 sha256 与改动前逐字节相同（`eea550cd…`），测试复绿。

需要强调的是：**对读一致性不等于运行正确**。上表能排除「名字/类型/方向接错」，不能排除「运行时序、线程模型、失败重试下的行为」。§8.1 的 R5.1 硬前置**不变**。

**这一节还有一条没能靠对读排除的风险**：adapter 盖的是 Rust 的 `Params` 映射，而 APP 的 `isinstance(info, dict)` 依赖 pyo3 把 `Params` 暴露成 Python `dict`。这一条只有在**装好 wheel 的 venv 里**才能证实。它的失败模式是**有界的**：映射若不是 dict，读取函数会走回退并再次落回 null（退化成改动前的行为），不会崩、不会算错钱。

---

## 9. 下一阶段入口

- R2 的三条验收入口（APP plan 命令、APP 全套回归、主 session 独立复现）退出码均为 0；FORK 侧见 §6。
- **R3（私有生命周期与账户输出）可以开始**，但它依赖 R0/R1/R2 —— 三者均已收尾。
- **§8.1 那条接缝是 R5.1 的硬前置**：重编 wheel 之后，第一件事就是补一次 `parser → watcher → tape → replay` 的端到端运行，否则 R2.1 的跨仓集成要求在字面上从未被满足。
- **建议 R3 开工前先花一个小提交**把 §8.1 的接缝补上（如果用户希望提前闭合），代价是一次 wheel 重编；不补则它一直挂到 R5.1。

**本目录的工件清单**：

| 文件 | 内容 |
|---|---|
| `environment.txt` | 运行环境、两个起点 SHA、已装 wheel 的 SHA256 |
| `app_state.txt` / `fork_state.txt` | 两个仓库的分支、HEAD、diffstat、脏范围 |
| `independent_repro.py` | **主 session 自写的独立复现**（不引用 sub agent 的测试） |
| `independent_repro_prefix_baseline.txt` | 同一份脚本在**修复前干净副本**上的输出：0/6，退出码 1 |
| `independent_repro_postfix.txt` | 同一份脚本在工作树上的输出：6/6，退出码 0 |
| `f18_mutation_counterproof.txt` | F18 注入反证：2 条测试在自己的断言处变红 |
| `f12_mutation_counterproof.txt` | F12 注入反证：5 条测试变红 |
| `run_identity_counterproof.txt` | §5.5 那处 run-identity 校验的注入反证：恰好 1 条测试变红 |
| `f11_handle_leak.txt` | F11 第二半：两种读法之后都能重命名 + 删除（Windows 句柄不泄漏） |
| `prefix_stability.txt` | 运行中追加文件时，已产出前缀不被改写 |
| `r2_plan_app.txt` | plan §R2 指定命令的完整输出 |
| `r2_full_suite.txt` | APP 全套回归输出 |
| `fork_market_data.txt` | FORK：plan 指定的 `--test market_data` 输出（43 passed） |
| `fork_full_suite.txt` | FORK：整 crate 回归（763 passed, 0 failed） |
| `fork_fmt_check.txt` | FORK：`fmt --check`（exit 0） |
| `fork_metadata_version_tests.txt` | FORK：三个新测试**按名**跑的输出 |
| `fork_metadata_version_counterproof.txt` | FORK：接缝缺口的注入反证（3 条变红）+ 哈希复原记录 |
| `fork_python_feature.txt` | FORK：`--features python` 严格构建失败 + 诊断构建通过，含三条归因证据 |
| `fork_clippy_scope.txt` | FORK：clippy 发现的范围（新代码零新增，blame 核实） |
| `app_head_after_r2.txt` / `fork_head_after_r2.txt` | 两个仓库的终点 SHA |

**安全声明**：本目录只含 URL、路径、测试计数与源码行号。**不含账户信息、不含密钥、不含任何签名或登录帧。**

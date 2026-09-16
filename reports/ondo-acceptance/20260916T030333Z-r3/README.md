# R3 阶段验收报告 — 私有运行生命周期、账户输出、DMS

**run id**：`20260916T030333Z-r3`（R3 的唯一入口；R3.1 / R3.2 / R3.3 的原始输出分布在三个目录，见 §0）
**阶段**：R3（plan `docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md` 的 Task R3.1 / R3.2 / R3.3）
**plan 的 R3 验收定义**（§R3.3 末段，逐字）：全套 `cargo +1.98.0 test -p nautilus-ondo --locked`；`cargo +1.98.0 check -p nautilus-ondo --features python --locked`；Python feature 的真实 factory/client mock 集成测试。「达到『离线生命周期已完成』，尚不等于 sandbox 协议通过。」
**本报告作者**：主 session（Fable 5.1）。§2 的全部命令与退出码、§5 的全部注入反证由主 session **亲自执行**；实现工作由 sub agent 承担，§4 逐处标注谁写的、主 session 做了什么核实。

**本阶段不授权主网交易、资金转移或常驻部署。** 全程未向任何真实 venue 发出请求，未使用任何真实账号、未读取任何真实 key。按 plan §0，本目录不含账户信息、密钥、签名或登录帧。

---

## 0. 证据分布在三个目录

R3 是一个阶段、一次验收，但它的三个任务是在三个时间窗里做的，各自的原始输出留在各自 run id 的目录里。**本 README 是唯一入口**，下面每一节都指向具体的原始文件，而不是复述结论。

| 目录 | 覆盖 | 内容 |
|---|---|---|
| `20260916T030333Z-r3/`（本目录） | R3.1 | `fork_full_suite.txt`、`fork_private_runtime.txt`、`fork_python_feature.txt`、`fork_python_test_strict.txt`、`fork_python_test_diagnostic.txt`、`fork_fmt_check.txt`、`fork_clippy.txt`、`environment.txt`、`r31_mutation_decode_loss.txt`、`r31_mutation_stream_routing.txt` |
| `20260916T042245Z-r32/` | R3.2 及其 follow-up | `mine_full_suite_after_followup.txt`（终态全套）、`mine_full_suite_after_degraded.txt`、`mine_full_suite_raw.txt`、`mine_fmt_and_suite.txt`、`mine_fmt_check_followup.txt`、`mine_python_check*.txt`、`mine_python_test_strict*.txt`、`mutation_counterproofs.md`（M1–M5）、`mutation_counterproofs_followup.md`（M6–M8）、`new-finding-pagination-ceiling.md`、`fork_head.txt`、`app_head.txt` |
| `20260916T054423Z-r33/` | R3.3 | **终态验收**：`accept.sh`（跑出该目录全部 `mine_*` 的脚本，含逐命令的树稳定性检查）、`mine_environment.txt`（两个 HEAD + 全部 ondo `.rs` 的 sha256 清单）、`mine_fmt.txt`、`mine_suite.txt`、`mine_python_check.txt`、`mine_python_test.txt`；**被取代的第一次尝试**：`mine_full_suite.txt`、`mine_fmt_check.txt`、`mine_python_test_strict.txt`（见 §4.4，它们的字节在 13:49 被另一个写者改过）；**注入反证**：`r33_mutation_M9..M13.txt` 与 `r33_mutation_M9..M13.nff.txt`（前者没带 `--no-fail-fast`，只覆盖了第一个失败的 target，见 §5.1）、`mutate.py`、`run3.sh`、`run4.sh`（探针本身） |

`.nff` 后缀 = `--no-fail-fast`。两批注入反证的文件名不同不是笔误，见 §5.1 最后一条。

**要重跑这些脚本，先确认它们的行尾。** 四个脚本（`accept.sh`、`run3.sh`、`run4.sh`、`mutate.py`）在 **git blob 里是纯 LF**（逐个用 `git show :<path> | od -c | grep -cF '\r'` 验过，均为 0），本目录的工作区副本也是 LF。但本仓库 `core.autocrlf=true` 且没有 `.gitattributes`，所以**在 Windows 上重新 checkout 会把它们落成 CRLF**，而 CRLF 的 shell 脚本会让 `bash` 报 `\r` 相关的解析错。遇到这种情况用 `git show :<path> > <path>` 取回 LF 版本（或 `dos2unix`），不要据此怀疑证据本身——证据是 blob 里的字节。

三个目录的 `environment.txt` / `fork_head.txt` / `app_head.txt` 各自记录该时间窗的两个仓库 HEAD 与脏范围，它们是「这份输出对应哪份代码」的唯一凭据。

---

## 1. 两个 SHA 与脏范围

### 1.1 FORK `E:\nautilus_trader`，工作分支 `task/ondo-r3-private-lifecycle` → 验收后 ff 合并进 `onde-perps`（两个 HEAD 的对照另见 `fork_head_after_r3.txt`）

| | commit | 说明 |
|---|---|---|
| 起点（= `onde-perps`） | `3133e2501b990745655d2b6226801099b60e6b77` | R2 收尾（publish market truth one axis at a time …） |
| R3.1 | `7729e9e` | nautilus-ondo: own the private session, its socket and the account behind it |
| R3.2 | `1895978` | ondo: report the account, keep the ledger, and account for funding |
| R3.2 follow-up | `498ff9c` | ondo: bound the funding walk, keep the watermark, and answer the journal once |
| R3.3 | `4356f1f4d678cc7565397e8ce2389f266f895a91` | ondo: date the switch at the venue and make the stop converge (R3.3) |

`4356f1f` 的改动量（`git show --numstat`，非 `.pyi`，全部在 `crates/adapters/ondo/` 内，+2146 / −136）：
```
47   0   src/config.rs
506  26  src/execution.rs
11   0   src/python/config.rs
286  22  src/reconciliation.rs
9    0   src/websocket/private/diagnostics.rs
2    2   src/websocket/private/session.rs
137  13  src/websocket/private/stream.rs
701  1   tests/private_runtime.rs
447  72  tests/reconciliation.rs
```

**提交与验收是同一份字节，这一点被单独复核过**：`git show 4356f1f:crates/adapters/ondo/<path> | sha256sum` 对 `src/execution.rs` / `src/reconciliation.rs` / `src/websocket/private/stream.rs` 三个文件给出的摘要分别是 `f6a1e221…` / `df982eb3…` / `ca801839…`，与 §2 那份验收字节的三个摘要**逐字符相同**；提交后 `git status --porcelain`（非 `.pyi`）为空，即工作树 == 该提交。合起来：§2 的四条命令、§5 的五个注入，跑的都是 `4356f1f` 里的内容。

`498ff9c` 的改动量（`git show --numstat`，非 `.pyi`，全部在 `crates/adapters/ondo/` 内）：
```
76   4   src/execution.rs
27   9   src/reconciliation.rs
251  1   tests/execution.rs
19   0   tests/private_runtime.rs
64   0   tests/reconciliation.rs
```

**脏范围声明**：该 fork 工作区长期有一批 **CRLF-only 的 `.pyi`** 显示为 ` M`（R0 起既有）。它们**没有被暂存、没有被提交、也没有被修改** —— 每次提交都是按路径逐个 `git add` 的 ondo 文件，不是 `git add -A`。无跨 crate 改动，无 lockfile 改动，无 CI 改动。

### 1.2 APP `E:\Nautilus-Perps`，工作分支 `task/ondo-r3-private-lifecycle` → 验收后 ff 合并进 `main`

| | commit | 说明 |
|---|---|---|
| 起点（= `main`） | `0fd91976d7606b68f94f71306d2b5fd96bd51237` | R2 收尾（reports: record the R2 SHAs for both repositories） |
| R3 报告 | `bd25b58de6dcb0cf874e1a747b8aadd18c9016c8` | reports: R3 acceptance — the private runtime lifecycle, the account, and a switch that dates itself at the venue（51 个文件，+13585） |
| 收尾记录 | 见 `app_head_after_r3.txt` | 两个仓库的 R3 HEAD、逐提交改动量、验收命令与退出码。**一个提交写不进自己的 SHA**，所以「终点」这个 SHA 只能从这份记录文件里取——即记录文件自己所在的那个提交 |

**R3 没有 APP 侧源码提交，这不是漏记。** R2 有（`39f714e` 改了 `src/analysis/ondo_depth.py`、`src/market_tape.py` 等五个文件），R2 的记录文件也把 `r2_source_head` 和 `r2_report_head` 分开列了，所以读者拿两个阶段对比时会合理地期望这里也有一个；但 R3 的六条要求全部落在 FORK 的 `crates/adapters/ondo/` 内，Python 面只在 crate 内部变（`src/python/config.rs`，先是 `journal_path` 后是 `dead_mans_switch_max_failed_renewals`），那也是 FORK 的提交。**APP 在 R3 的全部产出就是本报告和它的证据。**

**脏范围声明**：`reports/stage1/*.csv`、`reports/perps-*`、`reports/polymarket-*`、`reports/entropy-*`、`.commandcode/`、`学习课题/` 等既有 untracked 文件保持原归属，**本阶段未暂存、未提交、未清理**。本阶段只 `git add` 本目录及其兄弟目录内的 R3 证据。

---

## 2. 验收命令与退出码

下面四条在**同一份字节**上跑完（`mine_environment.txt` 记下了这份字节：全部 ondo `.rs` 的 sha256，聚合值 `ce0803dc9604504f83e0baa47b88b1790ca75fb9719a71a1a7e0021e329c138e`）。plan 指定三条，另加 fmt：

| 命令 | 退出码 | 结果 | 原始输出 |
|---|---|---|---|
| `cargo +1.98.0 fmt -p nautilus-ondo -- --check` | 0 | 无 `Diff in`（只有 36 行 nightly-only 配置警告，见下） | `mine_fmt.txt` |
| `cargo +1.98.0 test -p nautilus-ondo --locked --offline` | 0 | **881 passed / 0 failed**，9 个 target | `mine_suite.txt` |
| `cargo +1.98.0 check -p nautilus-ondo --features python --locked --offline` | 0 | `Finished dev profile` | `mine_python_check.txt` |
| `cargo +1.98.0 test -p nautilus-ondo --features python --locked --offline --test python` | 0 | **6 passed / 0 failed** | `mine_python_test.txt` |

**「跑在哪些字节上」是可查的，不是承诺。** 验收脚本在每个命令执行前后各算一次全树哈希（`find src tests -name '*.rs' | sort | xargs sha256sum | sha256sum`），四次都是 `before == after`，即命令期间没有任何写者动过这棵树。这个前后哈希是本节唯一真正重要的部分：**一次绿的 suite 只是「它编译的那份字节」的证据**，没有这个哈希，任何「测试通过」都可以是另一个进程改完文件之后的读数。

**这条声明里哪一半是落盘的、哪一半只是当时看到的——分开说，因为两者证据强度不同。** 脚本是 `accept.sh`（随本证据目录提交，2039 字节，即跑出上面四行原始输出的那一份）。它算前后值（第 49、53 行）并**打印到终端**（第 55、57–58 行），`mine_*.txt` 只装各命令自己的输出（第 51 行），**这八个数从来没有写进任何文件**——`mine_acceptance_log.txt` 这个名字是本报告初稿里的假引用，文件不存在，已删。所以「四次 `before == after`」是**当时在终端上看到的**，读者无法从落盘文件复核这句话本身；读者能复核的是脚本（自己重跑即可复现同一套检查）与 `mine_environment.txt` 里那份逐文件 sha256 清单。

落盘证据能独立支撑的是更长、也更有用的那条链：**验收开始时的字节 = 本阶段结束时的字节 = 提交 `4356f1f` 里的字节**，三者聚合值都是 `ce0803dc…`（`mine_environment.txt` 的清单 → 现在的工作树重算 → 对 `git show 4356f1f:<path>` 逐文件比对，见 §1.1）。这条链证明的是「本节这套输出对应被提交的那份代码」；它**不能**证明命令与命令之间没有写者，后者只有终端记录。

**基线对照**：R3.2 终态（`-r32/mine_full_suite_after_followup.txt`）是 862 passed，逐 target 为 438 / 71 / 39 / 77 / 43 / **23** / **107** / 63 / 1。R3.3 终态是同序的 438 / 71 / 39 / 77 / 43 / **32** / **117** / 63 / 1 = 881。差 **+19**，全部落在 `private_runtime`（+9）与 `reconciliation`（+10），**没有任何一组下降**，也没有任何一条既有测试消失（逐名比对见 §6）。

关于 fmt 的 36 行警告：`rustfmt.toml` 里有 5 个 nightly-only 选项（`format_code_in_doc_comments`、`imports_granularity`、`group_imports`、`error_on_unformatted` 等），稳定版 rustfmt 会逐文件打印「unstable features are only available in nightly channel」然后照常工作。**这不影响结论**：断言的是 `--check` 的退出码为 0 且输出里没有 `Diff in`。

---

## 3. R3 的三个任务各做了什么

> **这两节从何而来**：R3.1 / R3.2 的原始输出留下了，但当时的**实现叙述**没有留下，它们由 sub agent
> `r3-narratives` **仅从 git 对象**重建（`git show` / `git grep <pattern> <sha>`，未读工作树），事实来源与
> 每个 `file:line` 的取法写在各节开头的「行号说明」里。主 session 读过全文，并对其中的「plan 要求了但没有对应
> 实现的」逐条作出判定——**其中两条被推翻**（§3.1 判定 3、4），判定写在每节的「主 session 判定」块里，读者可
> 用同一个命令自行复核。**行号本身未经主 session 逐条复核**；本节的价值在于它给出了可复核的取法，而不是在于
> 主 session 为每个数字背书。

### 3.1 R3.1 — 实现单 owner 的私有运行时（commit `7729e9e`）

> **行号说明**：本小节所有 `file:line` 都是 **`7729e9e` 这个提交里**的行号（用 `git show 7729e9e:<path> | grep -n` 得到），不是工作树的行号。事实来源是该提交的 git 对象（`git show` / `git grep <pattern> 7729e9e`），未读工作树。plan 要求取自 `docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md` 第 192-234 行。测试名从提交内的测试文件复制；本仓库大量使用 `#[rstest]` + `#[case::name]`，一个函数会展开成 `case_N_<label>`，遇到就一并列出 case 标签。

**提交规模**（`git show --numstat 7729e9e` 的原始输出）：
（下面是该命令的文件表部分，即 `git show --numstat --format="" 7729e9e` 的输出；提交信息正文未包含在内）

```
19	0	crates/adapters/ondo/src/common/credential.rs
47	3	crates/adapters/ondo/src/config.rs
593	67	crates/adapters/ondo/src/execution.rs
6	4	crates/adapters/ondo/src/http/client.rs
16	5	crates/adapters/ondo/src/lib.rs
19	2	crates/adapters/ondo/src/python/config.rs
59	0	crates/adapters/ondo/src/reconciliation.rs
8	0	crates/adapters/ondo/src/recording.rs
18	0	crates/adapters/ondo/src/websocket/messages.rs
6	0	crates/adapters/ondo/src/websocket/mod.rs
96	23	crates/adapters/ondo/src/websocket/parse.rs
413	0	crates/adapters/ondo/src/websocket/private/diagnostics.rs
445	0	crates/adapters/ondo/src/websocket/private/messages.rs
73	0	crates/adapters/ondo/src/websocket/private/mod.rs
371	0	crates/adapters/ondo/src/websocket/private/parse.rs
1216	0	crates/adapters/ondo/src/websocket/private/session.rs
1060	0	crates/adapters/ondo/src/websocket/private/stream.rs
29	0	crates/adapters/ondo/tests/execution.rs
5	5	crates/adapters/ondo/tests/http_client.rs
1517	0	crates/adapters/ondo/tests/private_runtime.rs
80	0	crates/adapters/ondo/tests/python.rs
29	0	crates/adapters/ondo/tests/reconciliation.rs
5	3	crates/adapters/ondo/tests/signing.rs
```

**plan 要求 → 实现**（逐条。plan 的每一条要求一行，对应写出实现落在哪个文件哪一行，以及覆盖它的测试名）

| plan 要求（节选/编号） | 实现位置（file:line） | 覆盖它的测试（全名，从文件里复制） |
|---|---|---|
| R3.1-1「执行配置补私有 WS URL，复用 R0 endpoint policy」 | `crates/adapters/ondo/src/config.rs:229`（`pub base_url_ws: Option<String>`）、`config.rs:310`（`fn ws_url()`）、`config.rs:321`（`fn stream_mode()`）、`crates/adapters/ondo/src/common/credential.rs:144`（`validate_authenticated_websocket_environment`，实现体 `:148` 调 `OndoEndpointPolicy::authenticated(environment, OndoSchemeFamily::WebSocket)`）、调用点 `crates/adapters/ondo/src/execution.rs:1364`、Python 面 `crates/adapters/ondo/src/python/config.rs:145`（参数）与 `:160`（落到 config） | `crates/adapters/ondo/tests/python.rs::test_the_private_session_is_configurable_from_python_and_carries_no_secret`；被复用的 policy 本身由 `crates/adapters/ondo/src/common/endpoint.rs::test_the_websocket_policy_admits_the_same_authorities`（4 个 case）覆盖，**该测试早于本提交**（`endpoint.rs` 不在本提交改动文件表内）；`execution.rs:1364` 这个调用点未找到直接覆盖它的测试 → 待主 session 判定 |
| R3.1-1「凭据经受控共享 credential 对象分别生成 REST/WS 签名，不能通过 accessor 打印或流出 secret」 | 共享：`crates/adapters/ondo/src/execution.rs:1303`（字段 `credential: Arc<OndoCredential>`）、`execution.rs:1379`（`let credential = Arc::new(credential);`）、`crates/adapters/ondo/src/http/client.rs:181`（`OndoAuth.credential: Arc<OndoCredential>`）、`http/client.rs:187`；私有侧持有同一份：`crates/adapters/ondo/src/websocket/private/stream.rs:230`（`start` 参数）、`stream.rs:332`（结构体字段）；WS 签名生成：`stream.rs:715`（`sign_ws(&self.credential, timestamp_ms)`）；`OndoCredential` 的 pub accessor 只有 `key_id()`（`common/credential.rs:277`）与 `redact()`（`:294`），无 secret accessor | `crates/adapters/ondo/src/websocket/private/messages.rs::test_a_login_request_never_renders_its_credential`、`crates/adapters/ondo/src/websocket/private/session.rs::test_only_the_login_action_carries_a_credential`、`crates/adapters/ondo/src/common/credential.rs::test_the_secret_is_the_full_prefixed_value_and_no_plaintext_view_exists`（**早于本提交**）、`crates/adapters/ondo/tests/python.rs::test_the_private_session_is_configurable_from_python_and_carries_no_secret` |
| R3.1-2「实现 login frame 与确认」 | frame 构造：`crates/adapters/ondo/src/websocket/private/messages.rs:174`（`LoginRequest`）、`:201`（`to_json_text`）；发送端 `crates/adapters/ondo/src/websocket/private/stream.rs:712-722`（`build_body` 的 `PrivateAction::Login` 分支）；状态机 `crates/adapters/ondo/src/websocket/private/session.rs:410`（`on_connected` 返回 `PrivateAction::Login`）与 `session.rs:525`（`handle_logged_in` 处理确认）；枚举成员 `crates/adapters/ondo/src/websocket/messages.rs:120`（`WsOp::Login`）、`:133`（`as_str`） | `crates/adapters/ondo/src/websocket/private/session.rs::test_a_new_connection_sends_the_login_and_nothing_else`、`crates/adapters/ondo/src/websocket/private/messages.rs::test_the_login_body_is_the_documented_shape`、`crates/adapters/ondo/src/websocket/private/parse.rs::test_the_login_acknowledgement_classifies`、`crates/adapters/ondo/tests/private_runtime.rs::test_the_whole_private_lifecycle_from_login_to_a_refused_reconnect` |
| R3.1-2「订单/fill 订阅及确认」 | 频道集合 `crates/adapters/ondo/src/websocket/private/messages.rs:74`（`PrivateChannel::TRADING`，orders 先于 fills）、`:82`（`READ_ONLY`，不含 switch）；订阅动作 `crates/adapters/ondo/src/websocket/private/session.rs:539-548`（先 report 频道，再按 mode 决定 `ArmSwitch`）；确认 `session.rs:562`（`handle_subscribed`）、建立判定 `session.rs:376`（`is_established`，`:581` 用它把 phase 推到 `Subscribed`）；订阅帧 `messages.rs:263`（`PrivateSubscriptionRequest::to_json_text`） | `crates/adapters/ondo/src/websocket/private/session.rs::test_establishment_is_the_report_channels`、`crates/adapters/ondo/src/websocket/private/messages.rs::test_the_private_channel_wire_names_round_trip`、`crates/adapters/ondo/src/websocket/private/messages.rs::test_an_account_wide_request_omits_the_market_filter`、`crates/adapters/ondo/src/websocket/private/messages.rs::test_a_read_only_session_never_subscribes_to_the_switch`、`crates/adapters/ondo/tests/private_runtime.rs::test_a_refused_subscription_never_becomes_ready` |
| R3.1-2「断连/心跳超时」 | idle 超时 180s：`crates/adapters/ondo/src/websocket/private/stream.rs:435`（读 `ONDO_WS_IDLE_TIMEOUT_SECS`）与 `stream.rs:475`（select 分支，超时即 return 触发重连）；login 应答超时 10s：`stream.rs:94`（`ONDO_WS_LOGIN_TIMEOUT_SECS`）与 `stream.rs:483`（`session.note_login_timeout()` 后 return）；应用层心跳 `crates/adapters/ondo/src/websocket/private/session.rs:458`（`heartbeat()`）+ `stream.rs:497`（tick 发送）；断连回调 `session.rs:428`（`on_disconnected` 清 confirmed）与 `stream.rs:392`（`note_session_ended`） | `crates/adapters/ondo/src/websocket/private/session.rs::test_the_heartbeat_is_the_application_level_ping`、`crates/adapters/ondo/src/websocket/private/session.rs::test_a_disconnect_clears_what_the_socket_confirmed`、`crates/adapters/ondo/src/websocket/private/session.rs::test_an_unanswered_login_counts_against_the_bound`、`crates/adapters/ondo/tests/private_runtime.rs::test_the_whole_private_lifecycle_from_login_to_a_refused_reconnect` |
| R3.1-2「指数退避和退出」 | 退避构造 `crates/adapters/ondo/src/websocket/private/stream.rs:234`（`reconnect_backoff()?`，来自 `crates/adapters/ondo/src/websocket/client.rs`）、重置 `stream.rs:375`、取下一次延迟 `stream.rs:397`（`backoff.next_duration()`）；退出：`stream.rs:393`（`session.is_failed() || cancellation.is_cancelled()` 即 break）、`stream.rs:408`（`passes.shutdown().await`）、`stream.rs:306`（`pub async fn stop`）、`stream.rs:207`（`impl Drop`） | `crates/adapters/ondo/tests/private_runtime.rs::test_stop_leaves_no_task_behind`、`crates/adapters/ondo/tests/private_runtime.rs::test_a_synchronous_stop_also_ends_the_transport_task`、`crates/adapters/ondo/tests/private_runtime.rs::test_the_whole_private_lifecycle_from_login_to_a_refused_reconnect`（含「拒绝重连」段） |
| R3.1-2「auth/403 等永久错误有终止分类，不能无限重试」 | `crates/adapters/ondo/src/websocket/private/session.rs:74`（`ONDO_WS_LOGIN_MAX_ATTEMPTS: u64 = 3`）、`session.rs:86`（`PERMANENT_ERROR_MARKERS: [&str; 9]`，含 `"signature"`/`"api key"`/`"unauthorized"`/`"forbidden"`/`"ip_not_permitted"`/`"timestamp"` 等）、`session.rs:755`（`is_permanent_error`）、`session.rs:728`（`note_failed_login`）、`session.rs:738`（置 `PrivateSessionPhase::Failed`）、`session.rs:696-718`（`VenueError` 事件带 `permanent`），`stream.rs:639`（`apply_event` 用 `permanent` 作为「必须结束连接」的返回值） | `crates/adapters/ondo/src/websocket/private/session.rs::test_a_permanent_login_refusal_ends_the_session`（`#[rstest]` + 5 个 case：`case_1_a_signature_mismatch`、`case_2_an_unknown_key`、`case_3_a_permission_refusal`、`case_4_a_clock_the_venue_refused`、`case_5_a_forbidden_code`）、`::test_an_unclassified_login_refusal_is_bounded_by_attempts`、`::test_a_successful_login_clears_the_attempt_count`、`::test_a_failed_session_is_not_revived_by_a_new_connection`、`crates/adapters/ondo/tests/private_runtime.rs::test_a_refused_login_ends_the_session_and_never_becomes_ready` |
| R3.1-3「运行状态至少区分 Disconnected、Authenticating、Recovering、ReadOnlySynced、TradingReady、Uncertain、Stopping；socket connected 不直接等于账户 Ready」 | `crates/adapters/ondo/src/websocket/private/stream.rs:123`（`pub enum PrivateRunState`；变体 `Disconnected`/`Authenticating:127`/`Recovering:129`/`ReadOnlySynced:132`/`TradingReady:134`/`Uncertain:137`/`Stopping:139`，另有 `Stopped`）、`stream.rs:147`（`as_str`）、`stream.rs:166`（`permits_new_orders` 只对 `TradingReady` 为真）、`stream.rs:892`（`refresh_run`：session 相位 → 状态）、`stream.rs:912`（`account_run_state`：未 `is_established` 即 `Recovering`；账户 `Ready` 且 read-only 才 `ReadOnlySynced`；账户 `Ready` 且 switch 已确认才 `TradingReady`）、250ms 重算 tick `stream.rs:113` 与 `stream.rs:507` | `crates/adapters/ondo/tests/private_runtime.rs::test_the_whole_private_lifecycle_from_login_to_a_refused_reconnect`、`::test_a_read_only_session_never_arms_the_switch_and_never_trades`（`ReadOnlySynced`，断言在 `:1168`）、`::test_a_decode_loss_makes_the_account_uncertain_and_keeps_the_socket`、`::test_a_buffer_overflow_makes_the_account_uncertain`、`::test_a_refused_login_ends_the_session_and_never_becomes_ready`（`Stopped`，断言在 `:918`）、`::test_stop_leaves_no_task_behind`、`::test_an_unconnected_client_has_no_private_session`（`Disconnected`，断言在 `:1475`）；未找到逐个列举全部七态单一断言点的测试 → 待主 session 判定 |
| R3.1-4「runtime 驱动 begin_recovery / reconcile_account / unknown probes / metadata validity」 | 建连即 `begin_recovery`：`crates/adapters/ondo/src/websocket/private/stream.rs:463`（`serve_connection` 内）→ `crates/adapters/ondo/src/execution.rs:2053`；unknown probes 与 reconcile 同一 pass：`stream.rs:783`（`spawn_reconciliation`，`:791` 先 `probe_unknown_submissions(now)`，`:795` 再 `reconcile_account(now)`）→ `execution.rs:2224` / `execution.rs:2187`；metadata validity：`stream.rs:839`（`spawn_metadata_refresh`，成功 `set_metadata(MetadataValidity::Current)`、失败 `MetadataValidity::Stale`）→ `execution.rs:2058`（`OndoAccountRuntime::set_metadata`） | `crates/adapters/ondo/tests/private_runtime.rs::test_the_whole_private_lifecycle_from_login_to_a_refused_reconnect`、`::test_a_report_that_arrives_during_a_pass_is_replayed_rather_than_lost`、`::test_a_periodic_pass_and_a_second_caller_cannot_both_own_the_account` |
| R3.1-4「30秒周期配置要有调用者」 | 常量 `crates/adapters/ondo/src/config.rs:44`（`ONDO_RECONCILE_INTERVAL_SECS: u64 = 30`）、默认值 `config.rs:247`；调用者：`crates/adapters/ondo/src/websocket/private/stream.rs:436`（`reconcile = Duration::from_secs(self.account.reconcile_interval_secs())`）、tick 建在 `stream.rs:448`、select 分支 `stream.rs:510`；同处还有 DMS 续期（`stream.rs:440`，`dms_timeout_secs()/2`）与 metadata tick（`stream.rs:511`） | `crates/adapters/ondo/tests/private_runtime.rs::test_a_periodic_pass_and_a_second_caller_cannot_both_own_the_account`、`::test_the_whole_private_lifecycle_from_login_to_a_refused_reconnect` |
| R3.1-4「周期与 reconnect 不并发启动重复 pass」 | 单一 claim：`crates/adapters/ondo/src/execution.rs:1195`（`struct PassOwnership`）、`:1202`（`impl`）、`:1207`（`claim`）、`:1227`（`is_claimed`）、`:1246`（`PassGuard`）、`:1271`（`Drop` 释放）；`OndoAccountRuntime` 持有 client 同一份 `Arc`：`execution.rs:1425`；pass 入口 claim：`execution.rs:2189`；第二个 caller 被拒：`execution.rs:2191`（`RecoveryPassRefusal::AlreadyRunning`） | `crates/adapters/ondo/tests/private_runtime.rs::test_a_periodic_pass_and_a_second_caller_cannot_both_own_the_account` |
| R3.1-5「应用 R1 的恢复归并协议」 | `crates/adapters/ondo/src/execution.rs:1996`（`ingest_stream_order`）与 `:2020`（`ingest_stream_fill`）：在 buffer 写锁内用 `self.pass.is_claimed()` 决定「apply now / hold for the pass」，generation 取 `self.reconciliation.read().recovery_generation()`（`:2000`、`:2024`）；pass 结束在同一把锁下 drain：`execution.rs:2353`（`close_pass`）；buffer 本体 `crates/adapters/ondo/src/reconciliation.rs:1094`（`ReconciliationBuffer`）、`:1235`（`drain_generation`） | `crates/adapters/ondo/tests/private_runtime.rs::test_a_report_that_arrives_during_a_pass_is_replayed_rather_than_lost`；buffer 自身的合流/代数测试在 `src/reconciliation.rs` 内且**早于本提交** → 待主 session 判定 |
| R3.1-5「登录/订阅失败…不能保持 TradingReady」 | 相位失败 → `Uncertain`：`crates/adapters/ondo/src/websocket/private/stream.rs:900`（`PrivateSessionPhase::Failed { reason } => PrivateRunSnapshot::new(PrivateRunState::Uncertain, …)`）；即便登录成功，未 `is_established` 也停在 `Recovering`：`stream.rs:912-916` | `crates/adapters/ondo/tests/private_runtime.rs::test_a_refused_login_ends_the_session_and_never_becomes_ready`、`::test_a_refused_subscription_never_becomes_ready` |
| R3.1-5「私有 decode loss…不能保持 TradingReady」 | `crates/adapters/ondo/src/websocket/private/stream.rs:611-620`（`PrivateEvent::ProtocolError` → `diagnostics.record(ReportsLost)` + `self.account.note_lost_reports(1, redacted)`；连接保持）→ `crates/adapters/ondo/src/execution.rs:2163`（`note_lost_reports`，本提交由私有改为 `pub`，R2 时是 `execution.rs:1679` 的私有方法）；帧解码入口 `crates/adapters/ondo/src/websocket/private/session.rs:483`（`handle_frame`） | `crates/adapters/ondo/tests/private_runtime.rs::test_a_decode_loss_makes_the_account_uncertain_and_keeps_the_socket`、`crates/adapters/ondo/src/websocket/private/session.rs::test_a_frame_that_will_not_decode_keeps_the_connection` |
| R3.1-5「buffer overflow…不能保持 TradingReady」 | `crates/adapters/ondo/src/execution.rs:2145`（`note_refused_reports`：读 buffer 容量，把 drop 数当 loss 上报）、`:2003`/`:2027`（两处 `buffer.take_dropped()` 后调用）→ `execution.rs:2163`；buffer 容量与 drop 计数 `crates/adapters/ondo/src/reconciliation.rs:1510`（`ONDO_RECOVERY_BUFFER_CAPACITY`）、`:1201`（`take_dropped`） | `crates/adapters/ondo/tests/private_runtime.rs::test_a_buffer_overflow_makes_the_account_uncertain` |
| R3.1-5「stale session…不能保持 TradingReady」 | 候选位置（未找到以 stale session 命名的实现或测试，需主 session 判定指哪一处）：断连清空确认 `crates/adapters/ondo/src/websocket/private/session.rs:428`；`is_established` 作为 Ready 前置门 `crates/adapters/ondo/src/websocket/private/stream.rs:912` | 待主 session 判定 |
| R3.1-6「private raw/诊断记录脱敏且独立」 | 独立：`crates/adapters/ondo/src/websocket/private/diagnostics.rs` 整个模块（容量 `:56`、记录枚举 `:62`、`record` `:213`、`record_frame_sent` `:247`、`render` `:318`）；构造性不含帧字节的理由写在 `diagnostics.rs:24-36`（`FrameSent` 只记 action）与 `crates/adapters/ondo/src/websocket/private/mod.rs:24-32`（模块表：transport 不持有 `RawMdSink`）；脱敏：`crates/adapters/ondo/src/websocket/private/stream.rs:966`（`redact` → `common/credential.rs:294`），所有自由文本入档前均经它 | `crates/adapters/ondo/src/websocket/private/diagnostics.rs::test_a_sent_frame_is_recorded_by_its_action_and_never_by_its_body`、`::test_no_record_can_hold_a_frame_body`、`::test_the_ring_drops_the_oldest_record`、`::test_losses_are_counted` |
| R3.1-6「公开 MD recorder 看不到 API key、login signature、账户订单 payload」 | login 具名拒绝：`crates/adapters/ondo/src/recording.rs:320`（`Some(WsOp::Login) => Err(NotPublicFrame::new("a login request carries a credential signature and is never recorded"))`，`RAW_MD_PUBLIC_OPS` 在 `recording.rs:246` 只有 `Subscribe`/`Unsubscribe`/`Ping`）；`loggedIn` 确认拒绝 `recording.rs:280-284`、非公开频道 update 拒绝 `recording.rs:265-271`（**后两处早于本提交**）；私有 payload 另有 `PublicFrame` 无公开构造函数的约束（`recording.rs:221-234`） | `crates/adapters/ondo/tests/private_runtime.rs::test_the_login_frame_is_outside_every_recording_boundary`；`recording.rs` 自身的白名单测试早于本提交 → 待主 session 判定 |
| R3.1「模拟完整流程：factory 创建→connect→login ack→订阅 ack→REST恢复+中途stream→一致→Ready→断线→拒绝新单→重连恢复→收敛」 | 驱动链：`crates/adapters/ondo/src/execution.rs:2803`（`connect` 启动 private transport）、`:2842`（`disconnect` 有界等待停止 transport）、`crates/adapters/ondo/src/websocket/private/stream.rs:348`（`run`：connect→serve→note end→backoff→repeat）、`:426`（`serve_connection`） | `crates/adapters/ondo/tests/private_runtime.rs::test_the_whole_private_lifecycle_from_login_to_a_refused_reconnect` |
| R3.1「另测每个 await 阶段 stop；全部 task 有 owner，可等待结束，无悬挂后台任务」 | 任务归属：`crates/adapters/ondo/src/websocket/private/stream.rs:262`（`runtime.spawn(state.run(...))` 持 liveness `Arc<()>`）、`:207`（`Drop` 取消任务）、`:348-351`（`_liveness` 整个任务生命周期持有）、`:408`（`passes.shutdown().await` 回收 pass）；bounded stop：`stream.rs:104`（`ONDO_PRIVATE_STREAM_STOP_TIMEOUT_SECS = 2`）、`:306`（`stop`）、`:297`（`is_running`） | `crates/adapters/ondo/tests/private_runtime.rs::test_stop_leaves_no_task_behind`、`::test_a_synchronous_stop_also_ends_the_transport_task`、`::test_the_runtime_and_the_client_share_one_account`、`::test_an_unconnected_client_has_no_private_session` |

**plan 要求了但没有对应实现的**：

1. **R3.1-1 的「执行配置补私有 WS URL」缺少直接覆盖 `execution.rs:1364` 这个调用点的测试。** 被复用的 policy 有测试（`src/common/endpoint.rs::test_the_websocket_policy_admits_the_same_authorities`），但该测试早于本提交，本提交没有为「execution client 用 WebSocket scheme family 判定 `base_url_ws`」新增测试。→ 未找到（新增测试）。
2. **R3.1-1 的「分别生成 REST/WS 签名」里的签名生成本体不在本提交里。** `sign_ws` 在 `7729e9e:crates/adapters/ondo/src/signing.rs:144`，R2（`3133e25:crates/adapters/ondo/src/signing.rs:144`）已存在同一行。本提交做的是让一条 `Arc<OndoCredential>` 同时喂 REST 与 WS。→ 生成部分未找到（本提交内无此实现）。
3. **R3.1-5 的「stale session」未找到对应实现或测试。** 提交里最接近的两处已列在表中该行（断连清 confirmed、`is_established` 门），但没有任何以 stale session 命名的类型、字段或测试。→ 未找到。
4. **R3.1-6 的「private **raw** 记录」未找到。** 本提交的私有侧只有「按 action/摘要记录、完全不存帧字节」的记录器（`diagnostics.rs:24-36` 即为该设计说明）。如果该条要求读作「必须存在一份脱敏后的 private 原始帧记录」，则未找到；如果读作「private 侧的记录必须脱敏且与公开 recorder 独立」，则实现位置见上表该行。
5. **plan 第 196 行要求「新增 `src/websocket/private.rs`」**，提交建的是目录模块 `src/websocket/private/{mod,messages,parse,session,stream,diagnostics}.rs`（`7729e9e:crates/adapters/ondo/src/websocket/private/mod.rs:57-63`）。plan 同句允许「必要时拆小但保留单 owner」；拆分的实际形态与 plan 字面路径不一致，按事实记录。

**实现里做了但 plan 没要求的**：

1. **`account_read_only` 这一个配置成员及其一整条 read-only 路径**（plan R3.1 未提）：`config.rs:237`（字段）、`config.rs:321`（`stream_mode()`）、`execution.rs:1394-1396`（构造期 `mark_account_read_only`）、`reconciliation.rs:136`（`NewRiskRefusal::AccountIsReadOnly`）、`:1642`（`mark_account_read_only`，幂等且不可回退）、`:1732`（在所有其它条件之前返回该 refusal）、`session.rs:105-142`（`PrivateStreamMode::ReadOnly` 与 `arms_the_switch`）、`messages.rs:82`（`PrivateChannel::READ_ONLY`）、`session.rs:468`（`release_actions` 对 read-only 返回空）。
2. **`WsOp::Login` 这一新枚举成员**：`websocket/messages.rs:120`，并在两处 exhaustive match 中具名处理（`messages.rs:133` 的 `as_str`、`recording.rs:320`）。提交信息明确说明加变体不是纯增量（每个 `match` 都会编译失败，这是设计）。
3. **`DeadMansSwitchMessage::to_json_text`**（`reconciliation.rs:314`）与 **`OndoAccountRuntime::release_dead_mans_switch`**（`execution.rs:1972`）：让停止序列的 `StopStep::ReleaseDeadMansSwitch` 有 frame 可发。提交信息写明「真正执行停止序列是 R3.3 的」。
4. **`disconnect()` 的有界等待与越界报错**：`execution.rs:2842-2860`（`stream.stop().await`，之后若 `stream.is_running()` 记 error）。
5. **公共/私有共用的 `FrameHeader`**：`websocket/parse.rs:92` 起抽出 envelope 头分类，供两面复用（该文件的 +96/-23 全在这里）。
6. **测试脚手架改动**：`tests/execution.rs`（`:244` 拒绝 WebSocket upgrade、`:276` `is_websocket_upgrade`、`:503` 给 config 指定 `base_url_ws`）与 `tests/reconciliation.rs`（`:2080`、`:2108`、`:2253`）——因为 execution client 现在会向同一个 mock 地址拨私有 socket。
7. **模块文档改写**：`lib.rs:33-60`（两条连接及为何是两条）、`websocket/mod.rs:24-28`（`private` 加入模块表）、`config.rs` 中 `dms_timeout_secs`/`reconcile_interval_secs` 的文档由「Task 8 arms/runs it」改为「private transport 是调用者」。

---

**主 session 判定**（对上面「plan 要求了但没有对应实现的」五条，逐条结案）：

1. **`execution.rs:1364` 这个调用点没有直接覆盖它的测试 —— 接受为已知缺口，不补。** 该行的职责只是把 policy 的判定结果落到「用哪个 WS URL」上；判定本体（`OndoEndpointPolicy::authenticated`）有 4 个 case 覆盖，而给这一行补测试需要打桩 endpoint policy 的调用本身，测的是它自己的打桩。记入 §7。
2. **`sign_ws` 的生成本体早于本提交 —— 事实，不是缺口。** 本提交负责的是让一条 `Arc<OndoCredential>` 同时喂 REST 与 WS 签名，这一半有覆盖（`session.rs::test_only_the_login_action_carries_a_credential`、`messages.rs::test_a_login_request_never_renders_its_credential`）。结案。
3. **「stale session 不能保持 TradingReady」—— 已实现，只是不叫这个名字（判定：**推翻**「未找到」）。** plan 第 202 行这一条讲的是 **run state 轴**，不是 `ReconciliationState`：陈旧的会话由两道门挡住——未 `is_established` 一律停在 `Recovering`（`stream.rs:912` 起），相位 `Failed` 直接 `Uncertain`（`stream.rs:900`），另有断连清空 socket 已确认之物（`session.rs:428`）。按字面找「stale session」这个类型名会永远找不到，但要求本身有实现有测试（`test_a_refused_subscription_never_becomes_ready`、`test_a_disconnected_session_never_becomes_ready`）。结案。
4. **「private raw 记录」—— 按正确读法已实现；字面读法反而会违反 §0（判定：**推翻**「未找到」）。** plan 这一条要求的是「private raw/诊断记录**脱敏且独立**」。私有侧记录**构造性不保存帧字节**：`diagnostics.rs:24-36` 说明了这个设计，`FrameSent` 只记 action，`test_no_record_can_hold_a_frame_body` 把「记不到」钉死，模块表（`private/mod.rs:24-32`）说明 transport 不持有 `RawMdSink`。一份含 login 帧的 private raw tape 是**密钥泄漏面**，字面实现会让 §0 的硬规则失效。结案：要求满足于「脱敏 + 独立 + 构造性无法持有帧体」。
5. **模块形态与 plan 字面路径不一致 —— 事实，且 plan 同句已允许拆分。** 记录准确，无需处置。

**主 session 未核实的部分**：本节的 `file:line` 未逐条复核（见本节开头的行号说明）；上表「覆盖它的测试」一列里的测试名，我只复核了 R3.3 期间新增的那几条。

---

### 3.2 R3.2 — 账户事件、持仓覆盖、journal 和资金费（commit `1895978` + follow-up `498ff9c`）

> **行号说明**：3.2.A 的所有 `file:line` 都是 **`1895978` 里**的行号，3.2.B 的都是 **`498ff9c` 里**的行号；跨提交引用一律写成 `<sha>:<path>`。事实来源是这两个提交的 git 对象（`git show` / `git grep <pattern> <sha>`），未读工作树。plan 要求取自 `docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md` 第 192-234 行；follow-up 一节的要求来源是该提交信息自述的三条修复，不是 plan。测试名从提交内的测试文件复制；`#[rstest]` + `#[case::name]` 会展开成 `case_N_<label>`，遇到就一并列出 case 标签。

#### 3.2.A — `1895978`（R3.2 主体）

**提交规模**（`git show --numstat 1895978` 的原始输出）：
（同样只含文件表，即 `git show --numstat --format="" 1895978`）

```
41	0	crates/adapters/ondo/src/config.rs
826	22	crates/adapters/ondo/src/execution.rs
23	1	crates/adapters/ondo/src/http/client.rs
124	0	crates/adapters/ondo/src/http/private.rs
13	0	crates/adapters/ondo/src/python/config.rs
1309	26	crates/adapters/ondo/src/reconciliation.rs
548	14	crates/adapters/ondo/tests/execution.rs
707	11	crates/adapters/ondo/tests/private_runtime.rs
22	4	crates/adapters/ondo/tests/python.rs
908	11	crates/adapters/ondo/tests/reconciliation.rs
```

**plan 要求 → 实现**（逐条。行号均为 `1895978` 内的行号）

| plan 要求（节选/编号） | 实现位置（file:line） | 覆盖它的测试（全名，从文件里复制） |
|---|---|---|
| R3.2-1「把 verified balance 映射为 Nautilus AccountState，通过真实 emitter/event channel 到达 cache」 | `crates/adapters/ondo/src/execution.rs:243`（`account_state_parts`：由一个 `MappedBalance` 造 `AccountBalance` + 可选 `MarginBalance`）、`execution.rs:2463`（`publish_account_state`，emitter 调用在 `:2489` 的 `emit_account_state(balances, margins, true, now, None)`）、`execution.rs:3391`（`generate_account_state` 转发到同一 emitter）、`execution.rs:2789`（pass 结束时调用 `publish_account_state`）；verified 的定义 `crates/adapters/ondo/src/reconciliation.rs:2907`（`verified_balance`） | `crates/adapters/ondo/tests/execution.rs::test_a_verified_balance_reaches_the_engine_as_a_nautilus_account_state`、`crates/adapters/ondo/tests/private_runtime.rs::test_the_account_state_travels_from_the_emitter_into_the_cache`、`crates/adapters/ondo/tests/private_runtime.rs::test_the_verified_account_and_its_positions_reach_the_engine` |
| R3.2-1「测试币种」 | 结算币种 `crates/adapters/ondo/src/execution.rs:226`（`settlement_currency()`，取 `ONDO_SETTLEMENT_CURRENCY`），用于 `:252` 的 `AccountBalance::from_total_and_locked` 与 `:256` 的 `MarginBalance` | `crates/adapters/ondo/tests/execution.rs::test_a_verified_balance_reaches_the_engine_as_a_nautilus_account_state`（断言 `reported.currency == Currency::from(ONDO_SETTLEMENT_CURRENCY)`） |
| R3.2-1「total/free/locked 一致性」 | `crates/adapters/ondo/src/reconciliation.rs:768`（`MappedBalance`：`total()` `:791`、`locked()` `:797`、`free()` `:803`）、映射构造 `reconciliation.rs:3712`；不一致的读数是 finding：`reconciliation.rs:1154`（`Finding::BalanceInconsistent`）；不可映射是 `:1163`（`Finding::BalanceUnreadable`）；文档化的 balance 成员白名单 `execution.rs:206`（`DOCUMENTED_BALANCE_MEMBERS: [&str; 16]`）、成员的 mmapped 之外部分 `reconciliation.rs:1140`（`Finding::UnsupportedCollateral`） | `crates/adapters/ondo/tests/reconciliation.rs::test_a_balance_is_reported_only_when_it_is_the_whole_account`、`::test_a_balance_that_could_not_be_mapped_verifies_nothing`、`::test_the_maintenance_requirement_travels_with_the_mapping_only_when_it_was_read`、`crates/adapters/ondo/tests/execution.rs::test_a_balance_that_is_not_the_whole_account_publishes_no_account_state` |
| R3.2-1「unknown/missing、异常负值语义」 | 负权益：`crates/adapters/ondo/src/reconciliation.rs:1148`（`Finding::NegativeEquity`）；发布侧不夹到 0 的说明 `execution.rs:2455-2462`（`publish_account_state` 文档头）与 `reconciliation.rs:2907` 起 `verified_balance` 文档 | `crates/adapters/ondo/tests/reconciliation.rs::test_a_negative_balance_is_verified_and_carries_its_negative_signs`、`crates/adapters/ondo/tests/execution.rs::test_a_negative_balance_is_published_negative_and_stops_new_risk` |
| R3.2-2「实现 native PositionStatusReport」 | `crates/adapters/ondo/src/execution.rs:4519`（`generate_position_status_reports`）、`:4570`（`PositionStatusReport::new`，`venue_position_id` 传 `None`：净持仓模式）；方向符号映射 `reconciliation.rs:639`（`signed_position_quantity`）、`:594`（`PositionDirection`）、`:608`（`from_raw`）；读端点 `execution.rs:3113`（`read_positions`）→ `crates/adapters/ondo/src/http/private.rs`（`POSITIONS_PATH`） | `crates/adapters/ondo/tests/execution.rs::test_the_position_report_is_the_venue_position` |
| R3.2-2「仅在完整范围证明后宣告 bulk coverage，不能用一个目标市场的局部结果代表整个账户」 | `crates/adapters/ondo/src/execution.rs:3387`（`provides_bulk_position_coverage` 恒返回 `false`，文档在 `:3383-3386`）；不可读行不报告的路径 `execution.rs:4543`（方向不可读的行 `continue` 并 log）；venue 全量列表的语义 `reconciliation.rs:1132`（`Finding::PositionAbsent`）、`:1102`（`UnmappablePosition`）、`:1107`（`UnreadablePosition`） | `crates/adapters/ondo/tests/execution.rs::test_bulk_position_coverage_is_not_promised_however_readable_the_rows_are`、`::test_a_position_row_this_adapter_cannot_read_is_never_guessed_into_a_report`、`crates/adapters/ondo/tests/reconciliation.rs::test_a_position_the_venue_stops_listing_is_reported_and_its_baseline_retired`（**早于本提交**） |
| R3.2-3「将 `underLiquidation` 映射为新风险阻断条件并给出报告原因；未知风险状态不按正常处理」 | `crates/adapters/ondo/src/reconciliation.rs:240`（`enum LiquidationState`，三态 `Clear`/`UnderLiquidation`/`Unknown{reason}`）、`:255`（`from_member(Option<bool>)`：`None` → `Unknown`）、`:267`（`unread()`）、`:275`（`permits_new_orders` 只对 `Clear` 为真）、`:291`（`reason()`）；读入 `reconciliation.rs:3669`（`self.liquidation = LiquidationState::from_member(balance.under_liquidation)`）、复位 `:3661`/`:2680`；阻断 `reconciliation.rs:2836`（`NewRiskRefusal::Liquidation`），variant 定义 `:179`、`reason()` `:185` 起 | `crates/adapters/ondo/tests/reconciliation.rs::test_an_account_under_liquidation_refuses_new_risk_by_name`、`::test_a_liquidation_condition_nobody_read_is_not_a_clear_one`、`crates/adapters/ondo/tests/execution.rs::test_an_account_under_liquidation_refuses_the_next_order_without_a_request` |
| R3.2-4「接入 LedgerJournal 持久化与恢复，保存已应用 fill、订单关联、unknown/cancel 待确认项、checkpoint/coverage」 | `crates/adapters/ondo/src/reconciliation.rs:2156`（`struct LedgerJournal`：`watermark_ns`/`fills`/`orders`/`unsettled` + `checkpoint`）、`:2177`（`SCHEMA_VERSION: u32 = 2`）、`:2181`（`from_snapshot`）、`:2312`（`restore`）、`:2438`（`load`）、`:2078`（`JournalSnapshot`）、`:1956`（`JournalUnsettled`，含 `kind`/`lookup`/`first_seen_ns`/`attempts`/`abandoned`，`from_outcome` 在 `:2019`）、`:1720`（`UncertainKind`）、`:1736`（`UncertainOutcome`）；unsettled 回填 `reconciliation.rs:3142`（`restore_unsettled`）与导出 `:3188`（`unsettled_journal_entries`）；运行侧 `execution.rs:2136`（`JournalHandle`）、`:2189`（`write`）、`:2353`（`restore_journal`）、`:2385`（`load_journal`）、`:2431`（`persist_journal`）、构造期恢复调用 `execution.rs:1669` | `crates/adapters/ondo/tests/reconciliation.rs::test_a_journal_round_trips_the_ledger_the_orders_and_the_unsettled_writes`、`::test_a_restored_journal_puts_the_unsettled_writes_back_with_their_window`、`::test_a_live_record_wins_over_the_journal_it_was_written_from`、`crates/adapters/ondo/tests/private_runtime.rs::test_a_run_whose_journal_cannot_be_restored_refuses_new_risk` |
| R3.2-4「采用原子持久化/可检查版本」 | 原子写：`crates/adapters/ondo/src/reconciliation.rs:2378`（`store_atomic`：写 sibling `.tmp-<n>` → `sync_all` → `rename`；临时名唯一性来自 `:2101` 的 `JOURNAL_WRITES`）；可检查：`:2035`（`JournalCheckpoint{written_ns,fills,orders,unsettled}`）与 `reconciliation.rs:2344`（`verify`，计数不符即拒绝读）；schema 版本检查 `reconciliation.rs:2106`（`SchemaProbe`）与 `:2177` | `crates/adapters/ondo/tests/reconciliation.rs::test_a_journal_is_written_atomically_and_reads_back_whole`、`::test_a_journal_that_disagrees_with_its_checkpoint_is_refused`、`::test_a_journal_of_an_older_schema_is_refused_rather_than_read_with_defaults`、`::test_a_journal_of_a_newer_schema_is_refused_by_its_version`、`::test_an_absent_journal_is_a_first_run_rather_than_a_failure` |
| R3.2-4「重启后先恢复核对再接受新风险」 | 恢复在 client 构造期、任何 admission 之前：`crates/adapters/ondo/src/execution.rs:1669`（`account.restore_journal(...)`，注释在 `:1662-1668`）；读不出即拒绝新风险：`reconciliation.rs:2472`（`enum JournalStatus`）、`:2552`（`reason()`）、`execution.rs:2272`（`journal_status`）、`:2385`（`load_journal` 失败 → `JournalStatus::Failed`）；admission 侧 `reconciliation.rs:2802`（`new_risk_refusal`，其中的 journal 分支返回 `NewRiskRefusal::JournalUnavailable`） | `crates/adapters/ondo/tests/reconciliation.rs::test_a_run_whose_journal_failed_refuses_new_risk_whatever_the_account_reads`、`::test_a_run_with_no_journal_path_still_trades_and_says_so`、`::test_a_journal_that_stopped_accepting_writes_is_stated_and_refuses_no_order`、`crates/adapters/ondo/tests/private_runtime.rs::test_a_run_with_no_journal_path_says_so_and_keeps_reading_the_account`、`::test_a_journal_that_stops_accepting_writes_is_reported_and_does_not_stop_trading` |
| R3.2-4「配置与 Python 绑定需同步」 | `crates/adapters/ondo/src/config.rs:271`（`pub journal_path: Option<String>`）、`:295`（`impl_pyo3_config_getters` 成员）、`:312`（`Debug` 字段）；`crates/adapters/ondo/src/python/config.rs:146`/`:161`/`:178` | `crates/adapters/ondo/tests/python.rs::test_the_private_session_is_configurable_from_python_and_carries_no_secret`（本提交向该测试追加 `journal_path` 断言，未新增测试函数） |
| R3.2-5「测试『fill 已 emit 但 checkpoint 前 crash』和『checkpoint 后重放同 fill』」 | crash 顺序由设计固定：journal 只写 checkpoint、写在 emit 之后（`crates/adapters/ondo/src/execution.rs:2431` `persist_journal` 的文档、`reconciliation.rs:2156` `LedgerJournal` 的文档「It is written whole, and it trails the events it records」）；重放身份来自 venue fill id（`execution.rs:1308` `fill_report`、`:869` `apply_fill`） | `crates/adapters/ondo/tests/private_runtime.rs::test_a_fill_reported_before_the_checkpoint_crash_is_replayed_under_one_identity`、`::test_a_fill_recorded_in_the_checkpoint_is_not_replayed_after_a_restart` |
| R3.2-5「使用稳定事件身份和 cache/journal 核对协议，不能只测文件能读回」 | 去重账本 `crates/adapters/ondo/src/execution.rs:185`（`OndoFillLedger`，键 `(account_id, fill.id)`）、`:198`（`contains`）、`:203`（`record`）、`:234`（`restore`）；journal 只从它导出 `reconciliation.rs:2181`（`from_snapshot` 过滤本 account 的 `(account_id, fill_id)`） | 同上一行的两个测试；`crates/adapters/ondo/tests/private_runtime.rs::test_the_verified_account_and_its_positions_reach_the_engine` |
| R3.2-6「区分 funding rate、累计 funding payment 和本期实际 funding cashflow。能证明的支付变化才记账」 | 三种事实分开：`crates/adapters/ondo/src/reconciliation.rs:817`（`FundingPayment`，来自 `FundingFeeTransfer` 记录）、`:842`（`identity()` = `market|time|amount`）、`:856`（`FundingGap`）、`:885`（`FundingLedger`）；只对支付记录记账 `reconciliation.rs:933`（`account`）、`:938`（`account_all`）；累计总额只作基准 `:905`（`observe_cumulative`）；对账判定 `:1011`（`reconciliation`）、`:1046`（`FundingReconciliation`）；判定接线 `reconciliation.rs:3730`（`judge_funding`），调用 `:3736`（observe_cumulative）、`:3752`（account_all）；端点与读 `crates/adapters/ondo/src/http/private.rs`（`FUNDING_FEES_PATH`）、`crates/adapters/ondo/src/http/client.rs:672`（`get_funding_fees`）、`execution.rs:3170`（`read_funding`）、`:5057`（`funding_payment`） | `crates/adapters/ondo/tests/reconciliation.rs::test_only_a_stated_payment_is_accounted_and_a_rate_is_never_multiplied_into_one`、`crates/adapters/ondo/tests/execution.rs::test_a_stated_funding_payment_is_accounted_and_a_rate_is_never_multiplied_into_one` |
| R3.2-6「缺支付记录时保留 unreconciled，不能用 rate×仓位推算成已确认支付」 | `crates/adapters/ondo/src/reconciliation.rs:1034`（`FundingReconciliation::Unreconciled(FundingGap{stated_change, accounted_change, difference, since, payments})`）、`:1199`（`Finding::FundingUnreconciled`）、`:1189`（`Finding::FundingUnreadable`）；两者**不**使账户 uncertain：`reconciliation.rs:1246`（`is_uncertain` 对这两个变体返回 `false`） | `crates/adapters/ondo/tests/reconciliation.rs::test_a_cumulative_total_that_moved_with_no_payment_record_is_reported_not_booked`、`::test_a_funding_history_that_could_not_be_read_is_reported_and_books_nothing`、`::test_a_payment_from_before_the_baseline_is_recorded_but_not_counted_against_it`、`::test_a_balance_without_a_cumulative_total_does_not_reuse_the_earlier_one`、`crates/adapters/ondo/tests/execution.rs::test_a_funding_read_that_failed_books_nothing_and_is_reported` |
| R3.2-7「外部订单/仓位的接纳、隔离、只读报告策略明确；不默认 cancel 外部订单来获得 clean account」 | 策略写在模块文档 `crates/adapters/ondo/src/execution.rs:60-88`（四条：identified never adopted / never cancelled and never modified / reported read-only / isolated by identity）；实现：`crates/adapters/ondo/src/reconciliation.rs:1091`（`Finding::ForeignOrder`，带 venue status）、产生点 `:3456`、可读/不可读的判定 `:1243`（`is_uncertain`：`!status.is_known() || Untriggered` 才算 uncertain）；未跟踪订单不进入订单索引：`execution.rs:1189`（`ObservedOrders::observe` 文档）与 `execution.rs:4862`（`nautilus_order_type` 文档：`tracked` 为 `None` 时 venue payload 是唯一证据，读不出就具名拒绝而不是猜）；外部仓位先进 baseline（`reconciliation.rs:1132` `PositionAbsent` 的基线退出） | `crates/adapters/ondo/tests/reconciliation.rs::test_an_untriggered_foreign_order_is_an_unknown_state_the_adapter_does_not_create`（**早于本提交**）；本提交未新增专门覆盖 §"The foreign account" 四条策略的测试 → 待主 session 判定 |

**plan 要求了但没有对应实现的**：

1. **R3.2-2 的「仅在**完整范围证明**后宣告 bulk coverage」中的「证明」这一半未找到实现。** 提交里没有任何计算「完整范围」的逻辑；答案是一个常量：`crates/adapters/ondo/src/execution.rs:3387` 的 `provides_bulk_position_coverage` 无条件返回 `false`（即本提交从不宣告 bulk coverage）。若主 session 把该条读作「不得在无完整证据时宣告」，常量 `false` 是它的一种实现（位置同上）；若读作「需要存在一个证明过程、证明成立后才宣告」，则该证明过程未找到。
2. **R3.2-7 的「外部**仓位**的接纳」只在 baseline 语义里出现，未找到本提交新增的、针对外部仓位的专门测试。** 相关实现散在 `reconciliation.rs:1132`（`PositionAbsent` 的文档）、`:3456` 一带的判定，但测试名里没有本提交新增的 external/foreign 仓位测试；`tests/reconciliation.rs::test_an_untriggered_foreign_order_is_an_unknown_state_the_adapter_does_not_create` 早于本提交。→ 未找到（本提交新增的测试）。
3. **R3.2 的 plan 修改清单（第 211 行）写「配置与 Python 绑定需同步」，提交只同步了 `journal_path` 一项**（`config.rs:271`、`python/config.rs:146/161/178`）；本提交没有在 Python 面新增其它入口（如 funding/positions 的读出）。按事实记录，不作判定。

**实现里做了但 plan 没要求的**：

1. **`Finding::FundingUnreadable` / `FundingUnreconciled` 被显式排除在「uncertain」之外**：`reconciliation.rs:1246`（`is_uncertain` 对这两个变体返回 `false`），即资金费对不上不会阻断新风险，只作为 finding 报告。
2. **`JournalStatus::Degraded` 这一档**（`reconciliation.rs:2508` 变体，`impl JournalStatus` 在 `:2527`，`reason()` 在 `:2552`）：journal 写失败不阻断下单，只记录 `failures` / `last_written_at` / `watermark`。
3. **负权益也发布**（`execution.rs:2455-2462` 文档），而不是夹到 0。
4. **`src/http/private.rs` 这个新文件**（+124）：把三个私有 GET 路径、query 参数名与 cursor 成员集中声明，并逐条标注「DOCUMENTED, NOT YET VERIFIED」（文件头 `:16-52`）。
5. **`OndoOrderState::from_journal`**（`execution.rs:414`）：journal 里的订单关联逐成员可失败解析，读不出即整份 journal 失败。
6. **`config.rs` 的 journal 文档**（`:255-270`）把「不给 journal_path」定义为一个受支持的模式，并把它和 `Degraded` 的区别写进配置层。

#### 3.2.B — `498ff9c`（R3.2 follow-up）

**提交规模**（`git show --numstat 498ff9c` 的原始输出）：
（同样只含文件表，即 `git show --numstat --format="" 498ff9c`）

```
76	4	crates/adapters/ondo/src/execution.rs
27	9	crates/adapters/ondo/src/reconciliation.rs
251	1	crates/adapters/ondo/tests/execution.rs
19	0	crates/adapters/ondo/tests/private_runtime.rs
64	0	crates/adapters/ondo/tests/reconciliation.rs
```

**它修的是 `1895978` 留下的哪一处问题**（用 `1895978:<path>` 与 `498ff9c:<path>` 对读）：

**修复 1 —— funding 历史的游走没有下界，普通账户每一趟都会撞上 100 页上限。**
对读 `1895978:crates/adapters/ondo/src/execution.rs:3170` 与 `498ff9c:crates/adapters/ondo/src/execution.rs:3219`：

```
1895978  async fn read_funding(&self) -> anyhow::Result<Vec<FundingPayment>> {
             let mut walk = CursorWalk::new(REPORT_MAX_PAGES);
             let mut query = OndoPrivateReadQuery::new();
             ...
             for fee in response.funding_fees()? {
                 payments.push(funding_payment(&fee)?);
             }
             let Some(cursor) = walk.advance(response.cursor())? else { break };
```

```
498ff9c  async fn read_funding(&self, now: UnixNanos) -> anyhow::Result<Vec<FundingPayment>> {
             let since = self.reconciliation.read().funding().baseline()
                 .map_or(now, |(since, _baseline)| since);
             ...
             let mut page = 0_usize;
             let mut before_the_window = true;
             for fee in response.funding_fees()? {
                 let payment = funding_payment(&fee)?;
                 page += 1;
                 if payment.time >= since { before_the_window = false; }
                 payments.push(payment);
             }
             if page > 0 && before_the_window { break; }
```

即：`1895978` 的 `read_funding()` 无参数、对整段历史一路走到 `REPORT_MAX_PAGES`（`1895978:execution.rs:199` = 100）用尽才失败；`498ff9c` 给它加了 `now` 参数、以 `FundingLedger::baseline()` 为下界，**整页都早于下界即停**，上限保留为 backstop。签名变化同时改了调用点（`498ff9c:execution.rs:2937` 一带：`self.read_funding(now).await`，对应 `1895978:execution.rs:2923` 的 `self.read_funding().await`）。空页不算终止条件（`page > 0` 才 break）。

**修复 2 —— `OndoPrivateState::watermark` 的文档声称「经 journal 的 watermark 跨重启存活」，但 `load_journal` 没有把它放回去。**
对读 `1895978:crates/adapters/ondo/src/execution.rs:2385`（`load_journal`）与 `498ff9c:crates/adapters/ondo/src/execution.rs:2405-2418`：

```
498ff9c  +   // The watermark is a fact about the fills this client **applied**, and the ledger just
         +   // restored is the record of exactly those. ...
         +   if let Some(watermark) = journal.watermark() {
         +       state.observe_fill(watermark);
         +   }
```

`1895978` 的同一位置只有 `state.restore_orders(orders);` 与 `journal.restore(&mut state.fills, ...)`，没有对 `state.watermark` 的写入；字段文档（`1895978:execution.rs:639`）写着 "it survives a restart through the journal's watermark"，`498ff9c` 把该文档改为「通过 `load_journal` 用与 live 路径同一条单调规则放回」（`498ff9c:execution.rs:639-642`），并复用 `OndoPrivateState::observe_fill`（`498ff9c:execution.rs:702`）而不是直接赋值。

**修复 3 —— `JournalStatus` 有两条各自编码「哪些状态阻断新风险」的 match。**
对读 `1895978:crates/adapters/ondo/src/reconciliation.rs:2539`（`permits_new_orders` = `!matches!(self, Self::Failed {..})`）与 `1895978:reconciliation.rs:2810-2814`（admission 里再 `if let JournalStatus::Failed { reason, .. } = &self.journal { return Some(NewRiskRefusal::JournalUnavailable{..}) }`），对读 `498ff9c:crates/adapters/ondo/src/reconciliation.rs:2551` 与 `:2821`：

```
498ff9c  +   pub fn new_risk_refusal(&self) -> Option<NewRiskRefusal> {
         +       match self {
         +           Self::Failed { reason, .. } => Some(NewRiskRefusal::JournalUnavailable { reason: reason.clone() }),
         +           Self::NotConfigured { .. } | Self::Restored { .. } | Self::Degraded { .. } => None,
         +       }
         +   }
         +   pub fn permits_new_orders(&self) -> bool { self.new_risk_refusal().is_none() }
```

且 admission 侧改为 `if let Some(refusal) = self.journal.new_risk_refusal() { return Some(refusal); }`（`498ff9c:reconciliation.rs:2830-2832`，函数起点 `:2821`）。修复后只剩一条 match。

**follow-up 的要求 → 实现 → 测试**（要求来源是提交信息自述的三条 follow-up，不是 plan；行号为 `498ff9c` 内的行号）

| 修复项（提交信息自述） | 实现位置（file:line） | 覆盖它的测试（全名，从文件里复制） |
|---|---|---|
| 修复 1：funding walk 以 baseline 为界，cap 降级为 backstop，读不完仍失败而不截断 | `crates/adapters/ondo/src/execution.rs:3219`（`read_funding(&self, now)`）、`:3221-3226`（取 baseline 作为 `since`）、`:3241-3258`（`page` / `before_the_window` / `break`）、调用点 `:2937`（`self.read_funding(now).await`）；下界来源 `crates/adapters/ondo/src/reconciliation.rs:953`（`FundingLedger::baseline`）与 `:980`（`accounted_since_baseline`） | `crates/adapters/ondo/tests/execution.rs::test_a_funding_history_deeper_than_the_page_cap_is_read_without_reaching_it`、`::test_a_funding_walk_stops_on_the_first_page_that_lies_before_the_baseline`、`::test_an_empty_funding_page_does_not_end_the_walk` |
| 修复 2：watermark 经 journal 跨重启存活（把字段文档的声称变真） | `crates/adapters/ondo/src/execution.rs:2405-2418`（`load_journal` 内 `if let Some(watermark) = journal.watermark() { state.observe_fill(watermark); }`）、文档改写 `:639-642`、单调规则本体 `:702`（`observe_fill`） | `crates/adapters/ondo/tests/private_runtime.rs::test_a_fill_recorded_in_the_checkpoint_is_not_replayed_after_a_restart`（本提交在该测试内追加了 `watermark_with_the_fill` 捕获与重启后 `journal_at(&journal).watermark()` 的等值断言，未新增测试函数） |
| 修复 3：`JournalStatus` 只保留一条「哪些状态阻断新风险」的编码 | `crates/adapters/ondo/src/reconciliation.rs:2551`（`new_risk_refusal`）、`:2565`（`permits_new_orders` = `new_risk_refusal().is_none()`）、admission 改为询问 status：`:2821-2826` | `crates/adapters/ondo/tests/reconciliation.rs::test_a_journal_status_permits_exactly_what_it_raises_no_refusal_for`（`#[rstest]` + 4 个 case：`case_1_failed`、`case_2_restored`、`case_3_degraded`、`case_4_not_configured`） |

**plan 要求了但没有对应实现的（follow-up 范围）**：无（follow-up 不声称覆盖 plan 条目；三条修复各自有实现与测试，见上表）。

**实现里做了但 plan 没要求的（follow-up 范围）**：

1. `read_funding` 的签名由 `(&self) -> …` 变为 `(&self, now: UnixNanos) -> …`，`now` 在没有 baseline 时充当上界（`498ff9c:execution.rs:3185-3218` 的文档段 `# The walk ends where the reconciliation starts`）。
2. `test_a_fill_recorded_in_the_checkpoint_is_not_replayed_after_a_restart` 内新增的 watermark 断言（`498ff9c` 的 `tests/private_runtime.rs` +19 行全部在此测试内）。
3. follow-up 未触碰 `REPORT_MAX_PAGES` 的取值（`498ff9c:execution.rs:199` 仍为 `100`），只改了走到它的条件。

**主 session 判定**（对上面「plan 要求了但没有对应实现的」三条，逐条结案）：

1. **bulk coverage 的「证明」—— 恒返回 `false` 就是该条要求的实现，不是缺口。** plan 要求「仅在完整范围证明后宣告 bulk coverage，不能用一个目标市场的局部结果代表整个账户」。在「没有任何完整范围证明」的前提下，**不宣告**正是它的 fail-closed 形态，而 `provides_bulk_position_coverage` 返回常量 `false`（`execution.rs:3387`）加上 `test_bulk_position_coverage_is_not_promised_however_readable_the_rows_are` 就是它：要求被满足于「永不宣告」，而不是被满足于「存在一个证明过程」。若将来要宣告覆盖，先要有证明过程；R3 不要求有。
2. **外部**仓位**没有本提交新增的专门测试 —— 部分成立，记为窄缺口。** 外部**订单**的策略有四条文档化并被测试覆盖（含 `test_an_untriggered_foreign_order_is_an_unknown_state_the_adapter_does_not_create`，早于本提交）；外部**仓位**的语义散在 `PositionAbsent` 的基线退出里，没有一条以它命名的测试。记入 §7，本阶段不新增范围。
3. **Python 绑定只同步了 `journal_path` —— 事实记录，且 R3.3 期间继续遵守。** R3.3 又把 `dead_mans_switch_max_failed_renewals` 同步到了 Python 面（`python/config.rs`），并在 crate 内对它做了上限封顶（`capped_dead_mans_switch_max_failed_renewals`）。这一条是「配置与 Python 绑定需同步」在后续阶段的延续，不是缺口。

**主 session 未核实的部分**：同 §3.1 末段——行号未逐条复核。


### 3.3 R3.3 — DMS 有效期与可收敛停止

**行号取自哪份字节**：终态字节（聚合 `ce0803dc…`，与 §2 四条命令、§5 五个注入跑的是同一份）。取法是 `grep -n "<符号名>"`，每个数字都由主 session 亲眼从那份字节里读出；未提交状态下没有提交 SHA 可引，提交之后可用 `git show <R3.3 sha>:<path>` 复核（行号相同）。

**R3.3 的起点是一处真实缺陷**，先说清它，因为六条要求里第 1 条正是冲它来的：`DeadMansSwitch` 原来只有一个「续期」动作，构造续期帧与承认续期成功是同一件事。venue 在**帧到达**时才重启计时器，而构造帧是本地动作——于是一次**写了但没发出去**（或发失败、或 task 阻塞）的续期，会先让本地截止时间前移半个 timeout，在这半个 timeout 里，本地以为被保护着，venue 的计时器却早已把订单撤掉。修法分两层：`renew_frame(&self)` 只构造（拿不到可变访问，就改不了截止时间——**让缺陷写不出来**），截止时间只在 `note_renew_sent(now)` 里移动，而它只被 writer 在 `send_body` 成功之后调用一次；`now` 在写之前读，所以本地的截止时间只可能**早于**真实的 venue 截止时间，方向 fail-closed（`stream.rs:808-809` 把这句理由写进了文档）。

| # | plan 要求（逐字节选） | 实现 | 测试 | 注入反证 |
|---|---|---|---|---|
| 1 | 写入/构造续期帧不能当续期成功；pending/confirmed/expired 与单调时钟 deadline 纳入 admission；lost ACK、send 失败、task 阻塞、过期均阻断新风险 | `reconciliation.rs:669` `renew_frame(&self)`（只构造）、`:695` `note_renew_sent(now)`（唯一移动 `expires_at` 处，且只在 `Armed` 时）、`:579` `state_at(now)`（把过期的 `Armed` 投影成 `Lapsed{expired_at}`）、`:605` `permits_new_orders(now)` 读的正是 `state_at(now)`；状态分列见 `:452-490`（`Arming`=pending / `Armed`=confirmed / `Lapsed` / `Expired` / `Failed`）；writer 顺序 `stream.rs:811` 构造 → `:815` 读时钟 → `:831` 发送 → 失败走 `:835` → 成功才 `:843` | `tests/reconciliation.rs`：`test_a_renewal_that_never_went_out_extends_nothing`、`test_a_late_renewal_does_not_revive_a_failed_switch`、`test_a_renewal_moves_the_deadline_the_admission_reads`、`test_the_deadline_takes_the_switch_out_of_permitting_orders`、`test_the_lapsed_switch_is_reported_as_lapsed_with_its_deadline`、`test_a_lapsed_switch_and_a_fired_switch_are_two_different_refusals`、`test_a_renewal_that_reaches_the_socket_clears_the_run_of_failures`；`tests/private_runtime.rs`：`test_a_new_order_after_the_deadline_is_refused_with_nothing_sent`、`test_a_lapsed_switch_takes_the_run_state_out_of_trading_ready` | **M9**（抽掉过期投影）→ `private_runtime` 3 红 / `reconciliation` 5 红；**M10**（`note_renew_sent` 不再移动截止时间）→ `reconciliation` 2 红 + `private_runtime` 1 红，而 `test_a_renewal_that_never_went_out_extends_nothing` 与 `test_a_renewal_that_reaches_the_socket_clears_the_run_of_failures` **存活**（它们不该受截止时间是否前移影响） |
| 2 | DMS renewal 协议从冻结规范和 mock 契约实现，可配置有界尝试；真实语义在 R5 sandbox 验证前标 unverified | 有界：`ONDO_DMS_MAX_FAILED_RENEWALS`（`config.rs:48` 再导出，默认 `:273`，上限 `ONDO_DMS_MAX_FAILED_RENEWALS_CEILING` `:56`），**封顶**发生在 `config.rs:381-382` —— 配置只能把它调到不高于上限，放大不了；unverified 标注写在不变量层：`reconciliation.rs:54`（模块头）、`:495`（续期语义）、`stream.rs:799` | `tests/reconciliation.rs`：`test_a_run_of_failed_renewals_fails_the_switch_at_its_bound`、`test_the_renewal_bound_is_capped_at_the_crates_own` | **无**（这一条没有注入反证，见 §7） |
| 3 | read-only 模式从未触发 DMS 副作用；sandbox trading 模式在必要保护确认前保持非 Ready | `execution.rs:1630-1632` 构造时按配置标记只读（`reconciliation.rs:2982` `mark_account_read_only`，读侧 `execution.rs:1720` / `:2796`）；run-state 轴的三重门在 `stream.rs:996` / `:1005` / `:1010`（未确认一律 `Recovering`/`Uncertain`，只有都满足才 `TradingReady`） | `tests/private_runtime.rs`：`test_a_read_only_session_never_arms_the_switch_and_never_trades`、`test_a_read_only_session_triggers_no_switch_side_effect_and_no_cancel` | **M13**（删掉只读标记）→ 这 2 条红、`reconciliation` 117 全绿（副作用闸的观测点在 e2e 侧） |
| 4 | stop 先停止新风险，再按 client ID/run ownership 清理本次订单并查询确认；记录残留和未知；不能用 market-wide DELETE | `execution.rs:2059` `stop_and_wait`；步骤顺序由 `reconciliation.rs:2031` `StopStep` 固定为 `CancelOwnOrders` → `ConfirmOwnOrders` → `ReleaseDeadMansSwitch` → `ClosePrivateStream`；清理范围只来自 `execution.rs:2020` `tracked_orders()`（本 run 自己跟踪的 client order id）；取消用的标识符由 `execution.rs:2354` `order_ref` 决定（有 venue order id 就用它，否则退回 adapter 约定的 `client:` 查询形式——venue 文档两种都接受，`execution.rs:4773-4777`）；market-wide 形式**刻意不可达**（`execution.rs:3054-3059` 的文档写明）；残留与未知落在 `StopReport.unresolved_orders` / `unconfirmed_cancels` / `unknown_submissions`（`reconciliation.rs:2080/2082/2084`） | `tests/private_runtime.rs`：`test_the_stop_cancels_this_runs_own_orders_by_id_and_never_a_market`、`test_the_stop_confirms_the_cancels_before_it_releases_the_switch`、`test_an_order_after_the_stop_is_refused_with_nothing_sent` | 前半（停新风险）由 **M9** 覆盖；「不用 market-wide DELETE」**无专门注入**（见 §7） |
| 5 | 真正执行停止动作并 await transport/task 退出；release 时机取决于已验证撤单/暴露状态；未确认订单存在时不得盲目解除保护 | `stop_and_wait` 是 `async` 且 `await` transport 退出（`execution.rs:2158-2168`，退出不了记 `StreamOutlivedStop`）；**解除门**在 `execution.rs:2126-2129`：只有步骤含 `ReleaseDeadMansSwitch` **且** `unconfirmed_cancels` 空 **且** `unknown_submissions` 空才发解除帧，否则走 `:2147-2155` 的 error 日志并把 `released_switch` 留 `false`（两种 `false` 的由来写在 `reconciliation.rs:2085-2089`） | `tests/private_runtime.rs`：`test_an_unconfirmed_cancel_keeps_the_switch_armed`、`test_a_stop_that_times_out_reports_a_timeout_and_not_a_completion`、`test_the_stop_confirms_the_cancels_before_it_releases_the_switch` | **M11**（门去掉两个空判定）→ 这 2 条红，panic 里带着 `released_switch: true` 的完整 `StopReport`；`reconciliation` 117 全绿 |
| 6 | 记录 stop 的完成、超时、未决状态；重启必须继承未决信息，不能清空 map 获得 Ready | `reconciliation.rs:2049` `StopOutcome{Complete, TimedOut, StreamOutlivedStop}` 三态分立（`:2044-2047` 说明为什么不能合成一个 "stopped"）、`:2068` `StopReport` 带证据而非结论（`:2063`）；停止末尾落盘 `execution.rs:2173` `persist_journal`（在撤单**之后**写，checkpoint 记的是停止留下的状态）；重启经 `load_journal` 把未决项读回 | `tests/private_runtime.rs`：`test_a_restart_inherits_the_stops_outstanding_cancels`、`test_a_stop_that_times_out_reports_a_timeout_and_not_a_completion` | **M12**（删掉停止时的落盘）→ 只有 `test_a_restart_inherits_the_stops_outstanding_cancels` 红，其余 31 条 private_runtime 与 117 条 reconciliation 全绿。**本阶段最干净的一条特异性证据** |

**六条里有三条只被部分覆盖，明确列出**（§7 有完整清单与理由）：第 2 条的「有界」、第 4 条的「不做 market-wide DELETE」在第 3–5 批注入里**没有**对应的突变；第 3 条的 run-state 轴只有 M9 的一条间接捕获。其余三条（1、5、6）各有一次**针对性**注入，且都能指出哪些测试**不该红而没红**。

---

## 4. 出处：每一处改动由谁写、主 session 做了什么核实

这一节是给审阅者的**可信度声明**，不是致谢。R3 期间这个 checkout 里同时有多个 session 在写同一个文件，因此归属必须按**提交边界**（`git show --numstat` + 在提交内容里检索特征串）确定，不能按「谁报告了什么」确定。

### 4.1 `498ff9c`（R3.2 follow-up）逐处归属

| 改动 | 文件 / 规模 | 作者 | 我的核实 |
|---|---|---|---|
| 资金费走查的基线截断 | `src/execution.rs`（76/4 的一部分） | `r3-account-output` 这个 sub agent | 阅读 hunk；M6 反证（移除截断 → 两条针对它的测试红，两条既有资金费测试存活） |
| 三条资金费测试 | `tests/execution.rs`（251/1） | 同上 | 逐条读断言；M6 |
| `JournalStatus` 单一编码（`new_risk_refusal()` 为唯一编码，`permits_new_orders()` 由它派生） | `src/reconciliation.rs`（27/9） | **主 session** | M8 反证（重新引入第二编码 → 新一致性测试的 `degraded` 例在**第二条**断言上红，另三例存活） |
| `OndoPrivateState.watermark` 在 `load_journal` 里经 `observe_fill` 读回 | `src/execution.rs`（76/4 的一部分） | **主 session** | M7 反证（删除恢复 → 重启那条测试红，另两条 journal 测试存活） |
| 重启后水位线一致性的断言 | `tests/private_runtime.rs`（19/0） | **主 session** | M7 |
| `rstest` 四例一致性测试 | `tests/reconciliation.rs`（64/0） | **主 session** | M8 |

### 4.2 一次并发写者事故（必须记录，因为它影响归属）

R3.2 follow-up 期间，主 session 对 `tests/execution.rs` 的编辑被拒绝（"File has been modified since read"），而数分钟前一次 grep 曾对一个**确实存在**的符号返回「无匹配」。诊断结果：一个已被**主 session 叫停**的 sub agent（`r3-account-output`）实际仍在运行，并且在完成同样的三项工作。

处置：把该 agent 停掉（`TaskStop`），对工作树做字节级快照，然后**审阅它真实的 diff**，而不是按它的报告推断。归属因此被更正为 §4.1 那张表。教训写在这里是因为它同时是**方法**：那个 agent 的报告把「某条端到端测试证明了 X」写成了事实，实际那条测试根本没走到那处代码；它给出的测试名也与文件里的真实名字不符。**本报告的所有测试名都是从文件里复制出来的，所有数量都是命令输出里的数字。**

### 4.3 主 session 未独立核实的部分

- **§3.1 / §3.2 的 `file:line` 未逐条复核。** 这两节的实现叙述由 sub agent `r3-narratives` 从 git 对象重建，主 session 读了全文、对其 8 条「未找到」逐条判定（其中 2 条被推翻，见各节末的判定块），但**没有**回到 `git show <sha>:<path>` 逐个数字核对。它们的价值是给出了可复核的取法。
- **§3.3 的行号相反：由主 session 自己在定稿的工作树上取。** 取法是 `grep -n "<符号名>"`，读者可用同一条命令复核；因为工作树在 R3.3 期间一直在动，行号对应的时间点写在 §3.3 开头。
- **`git show` 的归属只到「哪次提交」。** R3.3 的三个改动文件是未提交的工作树状态，无法按提交边界归属，只能按执行记录归属（§4.4）。
- `.pyi` 的 CRLF 噪声属既有状态，未追查成因。
- `-r32/mutation_counterproofs*.md` 里 M1–M5 是 R3.2 验收时做的，本报告只引用、未重跑；M6–M8 由主 session 亲自执行。
- 本阶段所有结论的范围**仅限 `nautilus-ondo`**。没有验证 ondo 之外的任何 crate 的行为，也没有验证任何真实 venue 的行为。

### 4.4 R3.3 的归属：一次两个写者写同一份文件的事故，以及它怎么被查清

**事实**：R3.3 期间，主 session 与一个 sub agent **同时**在改 `reconciliation.rs` / `stream.rs`。事故的形态是：主 session 判断「要求的修复没有实现」后**没有先停掉那个 agent 就直接动手改**，随后又用 `cp` 从自己的字节副本恢复过几次——每次恢复都把 agent 正在写了一半的中间状态重新盖回去。后果：13:46 的字节快照定格在「import 已加、使用还没写」的半成品上，第一批五个注入反证**全部**撞在同一个无关的编译错误上（`error: unused import: DeadMansSwitchState`），五个结果**全部作废**。

**处置与归属依据**：停掉该 agent（`TaskStop`）→ 冻结树 → 确认主 session 的修复还在 → 重做注入。归属不按「谁报告了什么」，而按**两次真实执行过的测试名集合**：`-r33/mine_full_suite.txt`（13:44）与 `-r33/mine_suite.txt`（13:53）里逐条 `test <name> ... ok` 的名字做集合差，得到的就是这两个时间点之间真实发生的增减。

| 时间点 | private_runtime | reconciliation | 合计 | 差 |
|---|---|---|---|---|
| R3.2 终态（`-r32`） | 23 | 107 | 862 | — |
| 13:44（`mine_full_suite.txt`） | 31 | 116 | 879 | +17 |
| 13:53（`mine_suite.txt`，终态） | 32 | 117 | 881 | +2 |

**13:47:26 那一次编辑（即 sub agent 在 13:44 之后写的）加入的正是这两条**，逐名可查：

| 加入的测试 | target | 主 session 的核实 |
|---|---|---|
| `test_a_lapsed_switch_takes_the_run_state_out_of_trading_ready` | `tests/private_runtime.rs` | 读过断言（`wait_until` 直到 run state 离开 `TradingReady`，靠 250ms 的 tick）；它也是 M9 在 private_runtime 里的捕获者 |
| `test_a_ready_account_is_refused_by_name_while_the_switch_is_unconfirmed_or_failed` | `tests/reconciliation.rs` | 读过断言（名字级拒绝理由） |

**其余 17 条不逐条宣称作者。** 它们在 13:44 的执行记录里已存在，而 13:44 之前的交接把主 session 与至少一个 sub agent 的改动混在同一份未提交的工作树里；按文件 mtime 只能定到「13:44 之前」，按提交边界更定不到（尚未提交）。**写不出可复核的归属，就不写。** 可以确认的是主 session 亲自做的那些改动：`DeadMansSwitch::renew_frame(&self)`（把「构造续期帧」与「续期」在类型上分开）与 `note_renew_sent(now)` 的语义、`stream.rs` 里「先构造 → 再读时钟 → 再发 → 成功后才记」的顺序，以及 `tests/reconciliation.rs` 里的 `test_a_renewal_that_never_went_out_extends_nothing` 与 `test_a_late_renewal_does_not_revive_a_failed_switch`。

**同一条时间线上还有一件事，它和事故同源**：那个写者 13:49:37 的编辑留下了**未格式化**的代码（`dead_mans_switch_state` 需要并成一行），`cargo fmt --check` 因此退出 1；格式化的写入发生在 13:52:49，之后整套验收才在终态字节上重跑。也就是说，在它写完到格式化之间，这棵树**连 fmt 闸门都过不了**。这不是格式问题，是「有人在我准备验收的同一份文件上还在写」的又一个症状。

**这次事故的教训（方法是可迁移的，所以写下来）**：**接管一个写者正在写的文件之前，先把它停掉。** 更准确地说：在我判断「这个 agent 没做到」的那一刻，正确的第一步是 `TaskStop`，而不是自己上手。恢复（`cp`）到一棵**有活着的写者**的树上，等于把对方的中间状态反复钉死，并且会让所有基于快照的验证静默失去意义——第一批五个注入反证就是代价。

---

## 5. 注入反证（「测试会不会真的红」）

反证的方法固定：把一个机制**移除**（不是改坏语法，也不是改断言），跑针对性 target，看预期的那几条测试是否变红、以及**不该红的有没有跟着红**（特异性证据），然后从字节副本恢复并用 `sha256sum` 复核。**恢复一律用工作树外的字节副本，绝不用 `git checkout`** —— 反证是在未提交的工作树上做的。

| 编号 | 突变 | 结果 | 文件 |
|---|---|---|---|
| M1–M5 | R3.2 的五个机制 | 见文件 | `-r32/mutation_counterproofs.md` |
| M6 | 移除资金费走查的基线截断 | 3 passed / 2 failed；两条针对它的红，`test_an_empty_funding_page_does_not_end_the_walk` 与两条既有资金费测试存活 | `-r32/mutation_counterproofs_followup.md` |
| M7 | 删除水位线恢复 | 22 passed / 1 failed；只有重启那条红 | 同上 |
| M8 | 重新引入第二编码 | 105 passed / 2 failed；`…_permits_exactly_what_it_raises_no_refusal_for::case_3_degraded` 在第二条断言上红，另三例存活 | 同上 |
| M9 | `DeadMansSwitch::state_at` 抽掉「过期的 `Armed` 投影成 `Lapsed`」（改成 `let _ = now; self.state.clone()`） | **9 个 target 里 2 个红**：`private_runtime` 30/2、`reconciliation` 112/5；其余 7 个全绿（含 438 条 unit tests） | 该红的红了：过期/续期截止/两种拒绝的分野共 5 条（`test_the_deadline_takes_the_switch_out_of_permitting_orders`、`test_the_lapsed_switch_is_reported_as_lapsed_with_its_deadline`、`test_a_lapsed_switch_and_a_fired_switch_are_two_different_refusals`、`test_a_renewal_that_never_went_out_extends_nothing`、`test_a_renewal_moves_the_deadline_the_admission_reads`）+ 端到端 2 条。**不该红的没红**：`test_an_armed_switch_is_renewed_before_its_timeout`、`test_an_unconfirmed_cancel_keeps_the_switch_armed`、`test_a_restart_inherits_the_stops_outstanding_cancels` 全部存活 | `r33_mutation_M9.full.txt` |
| M10 | `note_renew_sent` 不再移动 `expires_at`（只清失败计数） | 9 个里 2 个红：`private_runtime` 31/1、`reconciliation` 115/2 | 该红的红了：`test_a_renewal_moves_the_deadline_and_repeats_the_arm_frame`、`test_a_renewal_moves_the_deadline_the_admission_reads`、端到端 `test_an_armed_switch_is_renewed_before_its_timeout`。**关键的存活**：`test_a_renewal_that_never_went_out_extends_nothing` 与 `test_a_renewal_that_reaches_the_socket_clears_the_run_of_failures` —— 这两条讲的是「没发出去就不该延长」和「发出去要清掉失败计数」，本来就不该受截止时间是否前移影响 | `r33_mutation_M10.full.txt` |
| M11 | 解除保护的门去掉两个空判定（`unconfirmed_cancels.is_empty()` / `unknown_submissions.is_empty()`） | 9 个里 **1 个**红：`private_runtime` 30/2，`reconciliation` 117 全绿 | `test_an_unconfirmed_cancel_keeps_the_switch_armed` 与 `test_a_stop_that_times_out_reports_a_timeout_and_not_a_completion` 红，panic 里带着完整 `StopReport`，`released_switch: true` 与「switch 必须保持 armed」的对照一目了然。**这条门的观测点在端到端**（unit 级无法看到 send 与 release 的时序） | `r33_mutation_M11.full.txt` |
| M12 | 删掉停止末尾的 `persist_journal` | 9 个里 **1 个**红：`private_runtime` 31/1，其余 8 个全绿 | 只有 `test_a_restart_inherits_the_stops_outstanding_cancels` 红。**本批最干净的一条**：一处删除、一条测试、零连带 | `r33_mutation_M12.full.txt` |
| M13 | 删掉构造时的只读标记 `mark_account_read_only` | 9 个里 **1 个**红：`private_runtime` 30/2 | `test_a_read_only_session_never_arms_the_switch_and_never_trades`、`test_a_read_only_session_triggers_no_switch_side_effect_and_no_cancel` 红，`reconciliation` 117 全绿 | `r33_mutation_M13.full.txt` |

**这五个结果是怎么来的（三次尝试，只有第三次算证据）**：第一批（`r33_mutation_M*.txt`，13:48）五个**全部**撞在同一个无关的编译错误上，全废——原因写在 §4.4。第二批（`batch2.log`）开跑前就中止：探针的两道检查用了不同口径，见 §5.1 第 2 条。第三批（`r33_mutation_M*.nff.txt`）证明五个突变都能编译、且都在两个 target 里变红，但没带 `--no-fail-fast`，只覆盖到第一个失败的 target。第四批（`r33_mutation_M*.full.txt`，全 9 个 target）是上表的来源。

**一处观察，两次记录互相矛盾（诚实记下）**：M9 在第四批里让 `private_runtime` 红了 **2** 条，在第三批（`.nff`）里红了 **3** 条——多出来的是 `test_the_stop_cancels_this_runs_own_orders_by_id_and_never_a_market`。两次运行的字节完全相同（前后哈希都等于 `ce0803dc…`），所以差别只能来自时序。查到的根因是**这条测试的前置条件本身是竞态的**：它的 harness 只等到「订单被跟踪」（`tests/private_runtime.rs` 的 `harness_with_one_order` → `wait_until(..., "a tracked order", ...)`），而跟踪发生时 `venue_order_id` 还是 `None`（`execution.rs:1104-1108`），venue id 要等 private 流上的 create-ack（`execution.rs:951`）。于是它断言「撤单按 venue order id 命名」时，有时 venue id 还没到，撤单就走了 `client:` 形式。**这不是产品缺陷**（venue 文档两种标识符都接受，`execution.rs:4773-4777`），是这条测试的前置竞态。**我没有改它**：改测试字节就要重做整套验收，而它不在 R3 的要求里。记入 §7（R4 处理）。

**探针输出的一个小瑕疵**：第四批的逐 target 表把最后一行（doc-tests，1 passed）标成了 `signing.rs` —— doc-tests 不打印 `Running tests/…` 那一行，所以脚本沿用了上一个 target 的名字。数字（1 passed / 0 failed）是对的，标签不对。

### 5.1 这个方法本身踩到过的坑（写下来，因为它是假证据的来源）

本仓库**对 `cargo test` 也拒绝警告**（`build.warnings = "deny"`）。M6 的第一次尝试**没能编译** —— `error: variable 'page' is assigned to, but never used`，因为删掉早退之后它的计数器变成只写不读。那不是「测试抓到了突变」，也不是代码错了：那是构建拒绝产出二进制。必须把突变改成 warning-clean（`let _ = (page, before_the_window);`）再跑，**只有第二次的结果算证据**。任何一次「突变后测试红了」都必须先确认那次跑的是测试、而不是一次编译失败。

R3.3 这一轮把同一个坑以三种新形态又踩了一遍。三种都写下来，因为它们的共同点是**看起来像证据的东西其实什么都没测**：

1. **「先证明快照能编译」（第一批五个全废的直接原因）。** 第一批探针只检查了「工作树 == 我自己的快照」——这个断言恒真，它没问「快照本身能不能编译」。快照定格在另一个写者写了一半的状态上，于是五个变异全部死在同一行无关的 `error: unused import`。**教训：突变探针的第一个断言必须是「没突变的这棵树能编译并且测试是绿的」**，否则后面每一个「红了」都可能只是那棵树本来就是坏的。现在 `run3.sh` / `run4.sh` 的 pre-flight 就是这个。
2. **两道检查用不同口径算同一个值（第二批什么都没跑的间接原因）。** 第二批的聚合把**绝对路径**喂给 `sha256sum`，而记录下来的期望值是**仓库根相对路径**算出来的；文件名参与哈希，所以这两个数在字节完全相同的情况下也永不相等。同一批里的逐文件检查也坏着（快照里的名字没有 `src/` 前缀，拼出来的路径不存在）。**两道检查其实是同一个坏检查**，而它们一起把 pre-flight 卡住了——这一次 fail-closed 挡住了假证据。**教训：一个检查要先在一个已知为真的输入上验证过「它会给真」**，否则它只是一段会打印「不一致」的代码。可迁移的形式：口径（相对/绝对、含不含文件名、含不含汇总行）必须和期望值的来源一致，并且写下来。
3. **`cargo test` 不带 `--no-fail-fast` 时会在第一个失败的 test binary 停下。** 第三批跑的命令是 `--test private_runtime --test reconciliation`，而 target 顺序里 `private_runtime` 在前：它在五个变异里都先红，于是 **`reconciliation` 一次都没执行过**。如果不数 `test result` 的行数（每个 target 恰好一行），就会把「private_runtime 的两条红了」顺手写成「reconciliation 的两条也红了」——那是一句没有执行记录支撑的话。第四批（`.nff` 后缀）补了 `--no-fail-fast`，两个 target 都跑到完。**教训：数 `test result` 的行数，它必须等于 target 数。**
4. **数失败数要用带边界的模式。** `grep -cE '^test .* FAILED'` 会把汇总行 `test result: FAILED. 30 passed; 2 failed; ...` 也算一条（它确实以 `test ` 开头），于是 2 条真失败被数成 3。**判据：逐条看一眼名字，而不是信计数。** 本报告的失败测试名一律从输出里复制，计数只当旁证。
5. **报告引用了一个从未写过的文件（写这份报告时自己踩的，比前三条轻，但是同一类）。** §2 初稿结尾写「脚本与逐次的前后值在 `mine_acceptance_log.txt`」。真去查：`accept.sh` 把那八个数 `echo` 到终端（第 55、57–58 行）就完了，`mine_*.txt` 只装各命令自己的输出（第 51 行）——**这个名字是写报告时顺手造的，文件从来没有存在过**。它不改变任何结论（结论依赖的是 `mine_environment.txt` 的清单与提交里的字节，那两样都在），但它是一句关于证据的话，本身没有证据，和上面三条同源。**判据：写下「X 在 Y 里」的时候，当场对 `Y` 做一次 `ls`。** 处置：§2 改成把「哪一半落盘、哪一半只在终端上看到过」分开说，并把 `accept.sh` 一起提交，让读者能自己重跑那套检查。

---

## 6. 没有删测试换绿

**方法**：不比数量，比**名字**。把两次真实执行记录里逐条打印的 `test <name> ... ok` 取成集合做差——`-r32/mine_full_suite_after_followup.txt`（R3.2 终态，862）对 `-r33/mine_suite.txt`（R3.3 终态，881）。名字是从执行输出里读的，不是从源码里 `grep` 出来的，所以 rstest 生成的 `case_NN` 变体也在集合里，不会漏。

**结果**：9 个 target 逐一对齐，**删除 0 条、改名 0 条**，新增 19 条全部集中在两个 target：

| target | R3.2 | R3.3 | 增 | 删 |
|---|---|---|---|---|
| `nautilus-ondo` unit tests | 438 | 438 | 0 | 0 |
| `tests/execution.rs` | 71 | 71 | 0 | 0 |
| `tests/http_client.rs` | 39 | 39 | 0 | 0 |
| `tests/http_contract.rs` | 77 | 77 | 0 | 0 |
| `tests/market_data.rs` | 43 | 43 | 0 | 0 |
| `tests/private_runtime.rs` | 23 | 32 | **+9** | 0 |
| `tests/reconciliation.rs` | 107 | 117 | **+10** | 0 |
| `tests/signing.rs` | 63 | 63 | 0 | 0 |
| doc-tests | 1 | 1 | 0 | 0 |
| **合计** | **862** | **881** | **+19** | **0** |

**这个方法的边界（必须写清，否则它会被当成比实际更强的东西）**：名字集合只能证明**没有测试被删除或改名**。它**不能**证明某条既有测试的**断言没有被削弱**——一条测试可以在名字不变的情况下被改成永远为真。后者由 §5 的注入反证覆盖，而注入反证只覆盖被注入的那 5 个机制，**不是全部 881 条**。两者合起来是一张有洞的网，洞在哪里写在 §7。

---

## 7. 未解决项（明确声明，不掩盖）

1. **DMS 的续期与解除语义仍是 unverified。** 冻结规范只文档化了 subscribe 帧与 `timeout_seconds`，没有说明「什么是续期」。实现采用「重发 subscribe 帧」并在代码里标注 unverified（`src/websocket/private/stream.rs`）；**这靠的是规范与 mock 契约，不是靠推测，也尚未被真实 sandbox 证实**。真实语义留待 R5 sandbox 验证（plan §R5.1）。
2. **没有任何 sandbox 协议验证。** R3 的验收标准本身写着「达到『离线生命周期已完成』，尚不等于 sandbox 协议通过」。本阶段**没有向任何真实 venue 发出过请求**。
3. **冷编译下 `--features python` 测试 target 的 exit 101 假设未被证实。** 观察到的是：`nautilus-persistence-macros` 真正重链接时该 target 退出 101，热态时为 0。R3.1 的 `fork_python_test_strict.txt` 显示一次**局部重编**（5 个 crate，13.81s）后 strict 通过，这与「只有完全冷的时候才失败」不完全一致，因此该假设**收窄为待验证**，不是结论。它影响 R5.1 的构建纪律，不影响 R3 的结论。
4. **`nautilus-ondo` 的 clippy / doc 在 HEAD 是红的**（`-Dwarnings` 使既有 clippy 发现直接让构建失败；`-r3/fork_clippy.txt` 是当时的输出）。`cargo test` 是实际闸门。这是既有状态，不是 R3 引入的，也**未**在本阶段修复。
5. **endpoint policy 的那个调用点没有直接覆盖它的测试**（§3.1 判定 1）。`execution.rs` 里「按 policy 判定选 WS URL」这一行只被它自己的上游测试间接覆盖；判定本体有 4 个 case，调用点没有。补它的测试需要打桩调用本身，收益低于噪音，因此**接受为已知缺口**而不是补一个自证式的测试。
6. **外部仓位没有以它命名的测试**（§3.2 判定 2）。外部**订单**的策略有四条文档化并被覆盖；外部**仓位**的语义散在 `PositionAbsent` 的基线退出里，没有专门测试。本阶段不新增范围。
7. **「断言没被削弱」只有 5 个机制有反证，不是全部 881 条。** §6 的名字集合差能证明没有测试被删或改名，但一条测试可以在名字不变的前提下被改弱。这一面由 §5 的五个注入覆盖：DMS 的有效期投影、续期截止、解除门、停止的 journal 落盘、只读不触发副作用。**其余机制在这一阶段没有反证**——它们只有 R3.2 及更早的验证（`-r32/mutation_counterproofs*.md` 的 M1–M8）。§3.3 末尾列出了 R3.3 六条要求里哪三条只被部分覆盖及具体缺口。
8. **`-r33/mine_fmt_check.txt` 这个文件名会误导**：它是 13:45 的一次**通过**的检查，不是 13:49 那次失败的记录；那次失败的输出**没有落盘**（只有「修复后 `mine_fmt.txt` 通过」和 `cargo fmt` 写入过的 mtime 为证）。文件名保留是为了不改动已生成的证据目录，正确读法写在这里。
9. **一条端到端测试的前置条件是竞态的**（§5 末尾的两次矛盾记录）：`test_the_stop_cancels_this_runs_own_orders_by_id_and_never_a_market` 只等到「订单被跟踪」就往下走，而 `venue_order_id` 要晚一步才从 private 流到达，于是它断言的「按 venue order id 撤单」在时序不利时会不成立。**不是产品缺陷**（两种标识符 venue 都接受），是测试前置写弱了；**本阶段未修**（改测试字节就要重做整套验收，且不在 R3 要求内）。留给 R4：把等待条件从「被跟踪」收紧为「venue id 已知」。
10. **`§6.4` 这个引用不在续做计划里。** ondo crate 里有 109 处 `plan §6.4`（`src/reconciliation.rs` 37 处、`src/execution.rs` 46 处等），但 `2026-09-15-ondo-perps-continuation.md` 里「6.4」出现 **0** 次；定义它的是更早的 `2026-09-14-ondo-perps-full-integration.md:282`（`### 6.4 账户与恢复`）。**引用本身没问题，只是要顺着正确的文件去查**——写在这里，免得下一个读者在续做计划里翻不到。
11. **每次命令的树稳定性检查没有落盘记录。** §2 讲了这条的口径：八次 before/after 值由 `accept.sh` 打印到终端，从未写进文件，所以「四次 `before == after`」这句话读者无法从文件复核（能复核的是脚本本身，以及 `ce0803dc…` 那条三段相等的字节链）。要让这一条以后可复核，改法很小——把第 55、57–58 行的 `echo` 也 `>> "$OUT/mine_stability.txt"`——但**本阶段不改**：改了就跑在一份与验收不同的 `accept.sh` 上，而验收已经完成。留给 R4 的 `accept` 脚本照此写。

---

## 8. 下一阶段入口（R4）

plan §R4.1 是 APP 侧的 `src/ondo_probe.py` + `docs/ondo.md`，四个模式 `public|account-readonly|paper|sandbox`，默认 `public`、默认有限时、无默认下单参数。R4 的 `sandbox` 模式需要显式 `--allow-sandbox-orders` 与 allowlist endpoint，且**仍然不是主网**；主网下单需要用户在该轮对话里明确说「上主网」，这是全项目的硬规则，不在 plan 的授权范围内。

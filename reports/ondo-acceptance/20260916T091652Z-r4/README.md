# R4 阶段验收报告 — 四模式 probe、停机路径、报告契约

日期 2026-09-16（UTC）。run id `20260916T091652Z-r4`。
上游依据：`reports/ondo-code-review-2026-09-15.md`（F01–F18）与
`docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md` 的 §R4。

R4 的可交付物是 `src/ondo_probe.py`（四模式统一入口）、`tests/test_ondo_probe.py`（验收测试）、
`docs/ondo.md`（操作手册）与 `.env.example` 的变量名补充。**但这份报告的重点不是它们写完了，
而是：第一次真的把它跑起来时，它跑不通。** 第 4 节是那个缺陷与它的修法，第 5 节是它在测试里
被钉住的证据。

## 0. 证据分布

| 位置 | 内容 |
|---|---|
| `E:\Nautilus-Perps\reports\ondo-acceptance\20260916T091652Z-r4\` | **本目录**：wheel 构建/安装日志、fork 全量 `cargo test` 日志、两次真实 smoke run（`smoke-public/`、`smoke-paper/`）|
| `E:\nautilus_trader\crates\adapters\ondo\` | 适配器 crate：R4 只动了它的两份 README 与一个测试文件 |

## 1. 两个 SHA 与脏范围

### 1.1 FORK `E:\nautilus_trader`

工作分支 `task/ondo-r4-probe`（基于 `onde-perps` HEAD `4356f1f`）→ 提交 `1057738` →
**ff-only 合并进 `onde-perps`**，任务分支已删除。`git branch` 现在只剩 `aster` 与 `onde-perps`。

```
1057738 ondo: fix a stop-test race and tell the module map the truth (R4)
```

真实改动 3 个文件：

| 文件 | +/- | 内容 |
|---|---|---|
| `crates/adapters/ondo/tests/private_runtime.rs` | +7/−2 | 一处**测试竞态**：`tracks()` 只看订单索引里有没有这个 client id，而索引是在 create 请求发出**之前**写的，所以等待可能在 `venue_order_id` 仍是 `None` 时返回；停止逻辑随即按 client-id 形式命名撤单，而下面的断言要的是 venue id（实测 20 次里第 7 次失败）。改成等那个 id 本身。 |
| `crates/adapters/ondo/README.md` | +53/−35 | 模块表从「Task N (implemented)」改成 `Status` 列；补上表里缺的四个模块（`common/endpoint.rs`、`http/orders.rs`、`http/private.rs`、`websocket/private/*`）；`rate_limit.rs` 并入 `client.rs` 行；补 fixture 出处与 `python` feature flag；写明 clippy/doc 在 HEAD 上因**既有**问题而红。 |
| `crates/adapters/ondo/test_data/README.md` | +21/−14 | `(P0)` → `(P0 freeze)`；补 `signing_rest_vectors.json` 行；支持矩阵按**实际实现**改写——**唯 DMS 一行保持 "not accepted"**，因为那是这套 fixture 唯一没有结论的一项。 |

**脏但非改动**（写下来，因为 `git status` 会把它算成 43 个改动）：

- 40 个 `python/nautilus_trader/**/__init__.pyi` 显示为 modified，但 `git diff -w --stat` 对它们
  **零输出**——纯 CRLF 行尾，`core.autocrlf` 的产物，无任何内容变化。**未提交**。
- `dist-r4/`（构建产物）在 fork 里**没有被 `.gitignore` 忽略**。三次 `git add` 全部按显式路径给出，
  没有用过 `git add -A`。

### 1.2 APP `E:\Nautilus-Perps`

工作分支 `task/ondo-r4-probe`（基于 `main` HEAD `3927b46`）→ 提交 `83742aa` →
**ff-only 合并进 `main`**，任务分支已删除。`git branch` 现在只剩 `main`。

```
83742aa ondo: add the four-mode probe, and stop it through the node's handle (R4)
```

4 个文件，+4446/−0：

| 文件 | 行数 | 内容 |
|---|---|---|
| `src/ondo_probe.py` | 2068 | 四模式统一入口（R4.1）|
| `tests/test_ondo_probe.py` | 2024 | 验收测试：66 个测试函数，参数化后 **111** 个用例 |
| `docs/ondo.md` | 347 | 操作手册（R4.2）|
| `.env.example` | +7 | 只加变量名与注释：`ONDO_SANDBOX_API_KEY` / `_SECRET` / `_ACCOUNT_ID`，三个**空值** |

## 2. 环境：venv 里装的是哪个 wheel

R4 的 probe 需要 R3 时代的配置字段（`account_read_only`、`dms_max_failed_renewals` 等），
PyPI 的 2.0.0rc4 没有它们，所以这一轮**重建并安装了 fork HEAD 的开发 wheel**：

| 项 | 值 |
|---|---|
| 构建命令 | 在 `E:\nautilus_trader\python` 下 `maturin build --profile nextest`（`--manifest-path` 找不到 `pyproject.toml`，必须切目录）|
| 产物 | `E:\nautilus_trader\dist-r4\nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl` |
| sha256 | `2e7b0b6e0511b08fe19628b81b17a25497d939de1aa72bb44c776fd4740498f0` |
| 安装 | `uv pip install --python .venv\Scripts\python.exe <wheel>`，退出码 0 |
| 回滚目标 | `dist-p3\...whl`，sha256 `b49ef609bbd42cc37ef3315f15ddeae44b7ae3ec83275bca86df07bcf70f6436`，**仍在盘上** |

**这个 wheel 对本报告有效，因为 fork 在 wheel 编译点之后没有改过一行适配器源码**——
`git diff --name-only 4356f1f 1057738 -- '*.rs'` 只列出 `crates/adapters/ondo/tests/private_runtime.rs`
（测试文件），没有任何 `src/` 下的 Rust 变化。`pyproject.toml`、release 构建、干净环境与依赖同步
按计划留给 R5.1，本轮没碰。

## 3. 验收命令与退出码

| 命令 | 退出码 | 结果 |
|---|---|---|
| `.\.venv\Scripts\python.exe -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider` | 0 | **111 passed** in 1.92s |
| `.\.venv\Scripts\python.exe -m pytest -q -p no:cacheprovider`（全量 APP 回归）| 0 | **815 passed**, 1 warning, 97 subtests passed in 15.15s |
| `cargo +1.98.0 test -p nautilus-ondo --locked`（fork，输出见 `fork_cargo_test.log`）| 0 | 880 + 1 doc-test，0 failed |

退出码是**分别取到的**，不是管道给的：fork 那次是把输出重定向到
`fork_cargo_test.log` 再 `echo "CARGO_EXIT=$?"`。中途有过一次
`cargo ... 2>&1 | tail -40` 得出 `exit 0` 的读数，那是 **tail 的**退出码，管道还吞掉了每个
binary 的汇总行——那次读数已作废，日志里的才是真的。

fork 的逐 binary 结果：

```
lib              438 passed   execution        71 passed   http_client      39 passed
http_contract     77 passed   market_data      43 passed   private_runtime  32 passed
reconciliation   117 passed   signing          63 passed   Doc-tests         1 passed
```

**基线与增量**：R4 之前 APP 全量回归是 **704 passed**（本 session 在 probe 落地前测的）。
现在 815 = 704 + 111，新增的全部来自 `tests/test_ondo_probe.py`。唯一那个 warning
（`tests/test_maker_live.py::test_limits` 返回非 None）在基线上就存在，不是 R4 引入的。

## 4. 第一次真的跑起来：停机路径是坏的（本阶段的主要发现）

`--dry-run`、拒绝路径、单元测试全部通过之后，第一次真实运行是这样的：

```
$ ./.venv/Scripts/python.exe src/ondo_probe.py --mode public --symbols NVDA --minutes 0.05 --out <tmp>
...[node 正常启动：连上 1 个 instrument、所有 engine client 已连接、
   reconciliation 完成、trader running]...
  File "E:\Nautilus-Perps\src\ondo_probe.py", line 1080, in bounded_stop
    stop()
pyo3_runtime.PanicException: assertion `left == right` failed:
    nautilus_live::python::node::PyLiveNode is unsendable, but sent to another thread
  left: ThreadId(36)   right: ThreadId(1)
PIPE_EXIT=124        # 被外部 timeout 杀掉，进程已经挂死 150 秒
```

**根因（在 fork 源码里核对过，不是我推测的）**：`crates/live/src/python/node.rs:104` 写着
`#[pyo3::pyclass(name = "LiveNode", unsendable)]`。PyO3 的 `unsendable` 检查是**线程 id 检查**：
在非拥有线程上读这个对象的**任何**属性，都会触发 Rust 断言。而 `execute()` 在主线程建 node，
watchdog 在 `ondo-probe-watchdog` 线程里调 `node.stop()`——所以那个 stop 必然 panic。

**为什么后果比"日志难看"严重得多**：`PanicException` 继承自 `BaseException`，不是 `Exception`，
所以 `bounded_stop` 里的 `except Exception` 接不住它；panic 走掉之后，主线程上的 `node.run()`
**继续阻塞**，`execute()` 的 `finally` 永不执行，报告永远不发布。而 `--minutes` 截止是
**每个模式唯一的终止机制**（`run()` 自己不会返回），所以：

- 所有非 dry-run 模式都跑不完整；
- `EXIT_TIMEOUT = 3` 实际不可达；
- 进程挂死，只能被外部杀掉。

**修法（框架文档本身给的，不是我自己发明的）**：同一个文件 158–167 行定义了 `PyLiveNodeHandle`，
`frozen`、**没有** `unsendable`，文档原文是 *"Thread-safe control handle for a `LiveNode` ...
safe to call from any thread or from a signal handler"*、*"the supported way to stop a hosted run"*；
而 `PyLiveNode` 自己的文档写着 *"Capture `cache`, `portfolio`, and `handle` before starting a hosted
run; each is an independent handle that stays usable while the node runs."*

所以改法是：

- 新增 `resolve_stop_target(node) -> (target, name)`，**在拥有线程上、`run()` 之前**调用，
  拿到 handle（拿不到才回落到 node 本身，那是给测试替身用的）；
- `bounded_stop` 的第一个参数从「node」变成「stop target」，并在报告里记
  `stop_target: "handle" | "node" | "none"`；
- watchdog 只拿到 handle，**永远不碰 node**。

**修复后的实测**（两次真实运行，证据在 `smoke-public/` 与 `smoke-paper/`）：

| 运行 | 命令行 | 退出码 | 墙钟 | `stop_condition` | `stop_target` |
|---|---|---|---|---|---|
| public | `--mode public --symbols NVDA --minutes 0.05` | **0** | 14 s | `deadline (a normal end for this mode)` | `handle`（两次停机都是）|
| paper | `--mode paper --symbols NVDA --minutes 0.05` | **0** | 14 s | `deadline (a normal end for this mode)` | `handle`（两次停机都是）|

修复前是挂死 150 秒被杀，修复后是 14 秒自己结束、`complete: true`。两次运行的 `stops` 都是
`requested: true, stopped: true`，`stop_target: "handle"`。

`smoke-public` 的发布还被独立校验过：

```
verify_run(reports/ondo-acceptance/20260916T091652Z-r4/smoke-public)
-> {"run_id": "20260916T095336139109Z", "complete": true, "verified": true, "problems": []}
```

**顺带修掉的一处报告缺陷**：`envelope_document` 原先发布的是**请求值**（`notional_usd: "999"`），
而同一份报告里的 `caps` 发布的是**钳制后的值**（`"50"`）——同一个边界在同一份文档里有两个互相
矛盾的数字。现在 envelope 一律带 applied，请求值仍留在 `caps.configured` 里并标 `capped: true`。
这不是安全问题（`_risk_config` 用的本来就是 applied），是报告自相矛盾。修完实测：

```
--notional-usd 999 --max-orders 99 --max-exposure-usd 9999
envelope: {"max_exposure_usd": "100", "max_orders": 10, "notional_usd": "50", ...}
caps:     notional  configured="999"  cap="50"  applied="50"  capped=true
          orders    configured=99     cap=10    applied=10    capped=true
          exposure  configured="9999" cap="100" applied="100" capped=true
```

## 5. 注入反证：这些测试会不会真的红

按本仓库既有的做法，把修复**注入回缺陷**，看测试是否真的变红——否则那些测试只是"证明实现"，
不是"钉住机制"。

注入内容是一行：`resolve_stop_target` 里 `return handle, "handle"` 改回 `return node, "node"`
（即修复前的行为）。结果：

```
FAILED test_the_stop_target_is_the_handle_when_the_node_has_one
FAILED test_a_completed_run_stops_through_the_handle_and_never_touches_the_node
FAILED test_the_watchdog_stop_is_delivered_to_the_handle_too
3 failed, 108 passed
```

失败信息直接点出后果，而不只是数字不等：

```
AssertionError: the watchdog stopped the node itself: that is the defect this replaced,
and on a real node it is a cross-thread read that panics instead of stopping
assert 2 == 0   where 2 = <test_ondo_probe.FakeNode object>.stop_calls
```

**恰好红 3 个、其余 108 个不受影响**，说明这 3 个钉住的是机制而不是无关的实现细节。

恢复是**字节级**的，没有用 `git checkout`：恢复后 sha256 回到
`fbf3e25179fba63ab8d49309c0a873c43c0a22bf5a2542d685121f59a5b63515`，`cmp` 对备份报 `IDENTICAL`。
备份文件本身在核实后已删除（哈希留在这里就是记录）。

## 6. 报告契约

「所有退出路径都触发有界停止并发布报告」这条，现在有真实运行的证据，而不是断言：
修复前的挂死正是这条不成立的样子。发布机制本身（run 目录 + 原子替换的 `meta.json` +
`verify_run` 重算每一个 sha256）与 `ondo_preflight.py` 是同一套，本轮没有另写一份。

R4 要求必须**能分别读出**的组，在 `smoke-public/probe.json` 里逐项存在：
`submitted` / `acked` / `filled` / `partial` / `canceled` / `unknown` / `no_trade` /
`pending_ids` / `account_reconciled` / `synthetic` / `protocol_verified` / `outstanding_orders`，
另有 `exit_code_zero_means_clean_account`、`converging_stop_available`、
`protocol_verified_reason`、`duplicate_receipts`、`lookup_failures`、`orders_submitted_by_probe`。

`orders_submitted_by_probe` 在两次真实运行里都是 `0`——这是"R4 不下单"这句话的运行时证据。

## 7. 没有为了绿而删测试

- APP 全量回归从 704 涨到 815，**只增未减**；没有删除、跳过或 xfail 任何既有测试。
- fork 的 `cargo test` 是 880 + 1 doc-test 全过；`private_runtime.rs` 的那处改动是**修一个
  真实的竞态**（20 次里第 7 次失败），不是为了让测试变绿而放宽断言——断言本身没动。
- fork 的 `cargo clippy` / `cargo doc` 在 HEAD 上仍是红的，**原因是既有问题**，本轮没有碰它们，
  也没有把它们说成绿的。`.cargo/config.toml` 里的 `-Dwarnings` 让任何 warning 都是硬错误，
  所以 `cargo test` 才是这一轮的真正门禁。

## 8. 未解决项（明确声明，不掩盖）

1. **收敛停止仍然从 Python 不可达。** 适配器里那个有序、有界、会 await 的停止执行器
   （`OndoAccountRuntime::stop_and_wait`）的唯一调用点仍在 `crates/adapters/ondo/tests/private_runtime.rs`；
   框架的停止钩子没接上它。R4 让**停机这个动作本身**能工作了（第 4 节），但它**不做**这三件事：
   不撤本次运行的订单、不确认撤单、不解除死手开关。这正是报告里
   `exit_code_zero_means_clean_account: false` 与 `converging_stop_available: false` 的含义，
   也是 R5 sandbox 提交的前置项。
2. **没有任何 sandbox 协议验证。** 到今天为止没有任何请求被发到真实 Ondo venue。
   REST 鉴权头名字、WS 登录签名拼接顺序、真实 private 帧形状、DMS 续期语义全部仍是
   **文档规定、未经 host 证实**（`docs/ondo.md` §7 逐项列出）。两次 smoke run 走的都是
   **公开面**（`public` 读 production 公开行情、`paper` 用真实公开数据 + 模拟执行），
   **没有一次私有会话被建立过**。
3. **两个带凭据的模式（`account-readonly` / `sandbox`）没有做过真实在线运行。** 它们的证据只有：
   拒绝启动路径（退出码 2、不建 client、不发请求、不写文件）、离线测试、以及 `--dry-run`。
   **不把它们说成"跑通了"**——那需要一次真实的 sandbox 会话，而这不属于 R4 的授权范围。
4. **`--max-orders` 与 `--max-exposure-usd` 在 R4 是纯声明。** 没有提交路径就没有可计数的东西，
   它们的强制点在 R5 的提交路径上。只有 `--notional-usd` 有运行时落点（经钳制后进风控引擎）。
   `docs/ondo.md` §3.6 已写明这一点。
5. **`envelope` 的语义在 R4 内变过一次**（第 4 节末）。若 R5 需要同时看到请求值与执行值，
   应当读 `caps`，不要读 `envelope`。

## 9. 下一阶段入口（R5）

- **R5.1 的环境工作**：`pyproject.toml`、release wheel、干净环境、依赖同步。另外有一项本轮
  发现但**故意没做**的：`python/nautilus_trader/adapters/ondo/__init__.py` 的 `__all__` 只列了
  6 个名字，而 `.pyi` 存根是陈旧的（缺 `base_url_ws`、`account_read_only`、
   `dms_max_failed_renewals`、`journal_path` 与三个 `__all__` 条目）。这落在计划 §R5.1
  「将 native factory/exec config 加入包公共导出与生成 stub 检查」的范围内——
  存根要靠**重新生成**，不要手改。
- **R5.2 的 sandbox 分步**：A（鉴权 / 账户只读）→ B（私有订阅 / 恢复）→ C（受限下单），
  计划写明 **A 通过不能替代 B/C**。R4 建起来的是 A/B 那一层会话的**授权前提**（模式 + 允许旗标
  + allowlist 端点 + 四个边界），**C 那一层不在 R4 里**。
- **R5 的前置阻塞项**是本报告 §8 第 1 条：把收敛停止执行器接到客户端生命周期上。
  在它接通之前，任何 sandbox 提交都会在关机时把订单留在 venue 上。

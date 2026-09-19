# Ondo 应用侧关机能力集成（R5.2 app / native capability）

Run id `20260919-r52-integration`。App worktree `E:/Nautilus-Perps/.worktrees/ondo-r52-app`；
fork worktree `E:/nautilus_trader/.worktrees/ondo-r52-cleanup`。

本任务**不**构建/安装 wheel、**不**运行 cargo / maturin / stub generation、**不**改生成存根、
**不**读凭据、**不**联网、**不**下单、**不**提交/暂存/合并/推送。原生 Pi 正在共享 target 上编译，
因此 fork 侧只改了 `crates/adapters/ondo/src/python/factories.rs` 这一处生产能力属性。

## 1. 问题

`src/ondo_probe.py` 之前把 `converging_stop_available` 硬编码为 `false`，并声明真实原生关机从不执行
有序清理。原生修正一旦编译进 wheel，这句话就过期；但对**仍然安装着的 R5.1 wheel** 直接翻成 `true` 又是
撒谎。不能从版本号 `2.0.0rc4`、wheel 文件名、worktree 路径或 Git commit 推测支持。

## 2. 设计

### 2.1 原生：一个只读能力属性

`crates/adapters/ondo/src/python/factories.rs` 给 `OndoExecutionClientFactory` 增加 pyo3 getter：

```rust
#[getter]
#[pyo3(name = "supports_ordered_shutdown")]
#[must_use]
pub const fn py_supports_ordered_shutdown(&self) -> bool { true }
```

- 它是**生产能力属性**（installed wheel 的只读事实），不是 venue 协议确认，也不代表某个账户被清理。
- 旧 wheel 完全没有这个属性，因此 Python 侧 `getattr(..., missing)` 必然得到「不可用」。
- 不加第二套执行实现、不加 test-only 接口。

### 2.2 应用：离线、只读、fail-closed 的检测

`src/ondo_probe.py` 新增 `detect_ordered_shutdown_capability(adapter)` 与 `ShutdownCapability`：

- 先探类属性：缺失即 `attribute-absent`（旧 R5 wheel 连 factory 对象都不需要构造）。
- 候选 wheel 的 getter 是 PyO3 `getset_descriptor`，类层不是 bool，于是构造**那个不持凭据的 factory 对象**
  再读实例属性。构造 factory 不是构造执行 client，也不读 `.env`。
- **只有精确的布尔 `True`** 算实现能力；`False`、`1`、`"true"`、`None`、类型不对、读取抛错、
  factory 无法构造、adapter 缺失，全部 fail closed 为不可用，并给出点名属性的理由。
- 检测在 `main` 里**所有启动 gate 之后**执行（凭据检查之后），所以一次 refusal 仍然什么都不构造。
  `--dry-run` 走 best-effort：注入 adapter 用它，真实命令行才尝试导入已安装 adapter，导入失败也不影响
  计划文档。

### 2.3 报告：区分「适配器能力」与「本轮观察」

`probe.json` / `meta.json` / dry-run 文档 / 日志一致地携带：

| 字段 | 含义 |
|---|---|
| `supports_ordered_shutdown` / `converging_stop_available` | 已安装适配器是否实现有序关机（离线能力） |
| `supports_ordered_shutdown_source` / `converging_stop.capability_source` | `native-factory` / `attribute-absent` / `reported-false` / `unexpected-type` / `lookup-failed` / `factory-construction-failed` / `adapter-unavailable` |
| `converging_stop_reason` | 能力的具名理由 |
| `converging_stop.cancels_own_orders` / `confirms_cancels` / `releases_dead_mans_switch` | **本轮实际观察到什么**：probe 没有原生 `StopReport` 遥测，因此为 `null`（未观察），**不是** `false`（未发生） |
| `converging_stop.observed_this_run` | 本轮是否观察到上述动作，固定 `false` |
| `protocol_verified` | 始终 `false`（私有/sandbox 鉴权、私有帧与 DMS 语义未被 host 证实；公开面已观察，但那不是协议验收） |
| `exit_code_zero_means_clean_account` | 始终 `false` |
| `orders_submitted_by_probe` | 始终 `0` |

`converging_stop.blocks` 在能力为真时改为「adapter 生命周期已无该阻塞项，但 R5.2 真实账户验证仍未完成」，
能力为假时保留原来的 R3 §7 第 1 条措辞。`unverified` 列表的 `converging_stop` 项现在同时携带
`adapter_capability`、能力来源与 `per_run_actions_observed: false`，并明确「能力为真也不等于观察到释放，
未观察不等于未发生」。

**零下单不等于没有解除 DMS。** 真实 sandbox trading 模式会话在 connect 时武装死手开关，干净的 disconnect
可以在**零订单**下解除它；恢复的 journal 也可能带着之前自己名下的订单。因此三个 per-run 字段在无原生
`StopReport` 遥测时是 `null`（未观察），绝不能写成 `false` 当作「未发生」的证据。

## 3. 改动路径

App worktree：

| 路径 | 改动 |
|---|---|
| `src/ondo_probe.py` | 能力常量/`ShutdownCapability`/`detect_ordered_shutdown_capability`；`_dry_run_capability`；`plan_document` / `report_document` / `converging_stop_document` / `unverified_document` 接受能力；`bounded_stop` 的 per-run 动作改为 `null`；`PROTOCOL_VERIFIED_REASON` 收窄；`execute` 与 `main` 接线；meta 与日志同步 |
| `tests/test_ondo_probe.py` | 新增第 8 节能力回归测试（20 个用例，含 5 个参数化）；既有 frozen 断言仅在复审修正处更新（`cancels_own_orders` 等由 `false` 改为 `null`），保留原安全意图 |
| `docs/ondo.md` | 开篇第 2 条、§4 退出码说明、§4 组表、§6 DMS 行、§6.1 新小节、§8 bullet 更新为「取决于安装的 wheel」 |
| `docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md` | R5.2 一条窄注释：属性 + 存根/运行时检查依赖 |

Fork worktree（仅本任务拥有的文件）：

| 路径 | 改动 |
|---|---|
| `crates/adapters/ondo/src/python/factories.rs` | `supports_ordered_shutdown` getter（+22 行） |

## 4. 测试证据

环境：`PYTHON_DOTENV_DISABLED=1`、`PYTHONUTF8=1`、canonical venv
`E:/Nautilus-Perps/.venv/Scripts/python.exe`，worktree `src` 由测试自行加入 `sys.path`。

### RED（实现前）

```
python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider
# 11 failed, 118 passed
```

`red-capability-tests.txt`。失败均为新断言（缺失的模块常量/函数、硬编码 `false`），不是环境或导入错误。

### GREEN（实现后）

```
python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider
# 130 passed
```

`green-probe-tests.txt`。

```
python -m pytest tests -q -p no:cacheprovider
# 834 passed, 97 subtests passed, 1 warning
```

`green-full-app-tests.txt`。唯一 warning 是既有的 `test_maker_live.py::test_limits` 返回
`Limits` 对象，与本次改动无关。

### GREEN（Astra 复审修正后）

```
python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider
# 132 passed

python -m pytest tests -q -p no:cacheprovider
# 836 passed, 97 subtests passed, 1 warning
```

`green-probe-tests-correction.txt`、`green-full-app-tests-correction.txt`。新增两个聚焦回归：
`test_zero_orders_does_not_prove_no_dms_release` 与
`test_protocol_reason_does_not_deny_the_observed_public_reads`。第一遍证据（`red-capability-tests.txt`、
`green-probe-tests.txt`、`green-full-app-tests.txt`）保留。

## 5. 当前安装的 R5 wheel 回退证据

对真实安装的 R5.1 wheel 跑离线 `--dry-run`（会导入 adapter、构造无凭据 factory，不建 client、不联网）：

```
E:/Nautilus-Perps/.venv/Scripts/python.exe src/ondo_probe.py --mode public --symbols NVDA --dry-run
```

结果（`installed-r5-dry-run.json`，第一遍；`installed-r5-dry-run-correction.json`，复审修正后）：

```json
{"supports_ordered_shutdown": false, "supports_ordered_shutdown_source": "attribute-absent",
 "converging_stop_available": false, "protocol_verified": false,
 "exit_code_zero_means_clean_account": false, "client_constructed": false}
```

修正后 `converging_stop.cancels_own_orders` / `confirms_cancels` / `releases_dead_mans_switch` 均为
`null`（未观察），`protocol_verified_reason` 已收窄为「没有经过鉴权的私有/sandbox 协议验收」。即旧 wheel
被如实判为不可用，`exit_code_zero_means_clean_account` 仍为 `false`。

## 6. 留给构建任务的依赖（必须做）

1. 原生复审当前结论是 **changes required**（`../review/shutdown-review-1.md`），Astra 说通过之前不得
   宣布原生验收通过。
2. 构建候选 wheel 后，**生成的存根 `python/nautilus_trader/adapters/ondo/__init__.pyi` 必须出现**：

   ```python
   @property
   def supports_ordered_shutdown(self) -> bool: ...
   ```

   出现位置在 `OndoExecutionClientFactory` 下。若未出现，说明 pyo3/stub 生成没有带上该 getter，候选
   wheel 对 app 来说仍等价于旧 wheel（fail closed）。
3. 安装候选 wheel 后跑运行时检查：
   - `python -c "from nautilus_trader.adapters.ondo import OndoExecutionClientFactory as F; print(F().supports_ordered_shutdown)"` 必须打印 `True`；
   - `python -m pytest tests/test_ondo_probe.py -q` 全绿；
   - 触发一次 `ondo_probe --mode public --dry-run`，确认 `converging_stop_available: true`、
     `protocol_verified: false`、`exit_code_zero_means_clean_account: false`、零写入。
4. 真实 sandbox 的鉴权/下单/撤单/DMS 语义仍然是 R5.2 A/B/C 的未验证项，`protocol_verified` 不得因此
   改动。

## 7. 仍未完成 / 未验证

- 候选原生 wheel 未构建/安装/独立验证；本报告不声称原生修正通过。
- 真实 sandbox 协议未验证；probe 仍然零下单。
- Rust 文件未编译、未跑 fmt（任务禁止运行 cargo / stub 生成）；由后续构建任务验证。

## 8. Astra 复审修正（第二遍）

复审 `control/pi-integration-review.md` 提出两点，均已修正，第一遍证据保留：

1. **零下单不能推断没有解除 DMS。** `converging_stop_document` 与 `unverified_document` 原先把「probe 零下单」
   当作「没有 cancel/confirm/DMS release」。真实 trading 模式会话在 connect 时武装 DMS，干净的 disconnect 可以
   在零订单下解除它，恢复的 journal 也可能带着之前自己名下的订单；app 没有 per-run 原生 `StopReport` 遥测。
   现在三个 per-run 字段（`cancels_own_orders` / `confirms_cancels` / `releases_dead_mans_switch`）在
   `converging_stop` 与每条 `stops` 记录里都报告为 `null`（未观察），`observed_this_run: false`，
   `unverified` 项新增 `per_run_actions_observed: false`，理由明确「未观察 ≠ 未发生」。public/paper/
   account-readonly 的 no-side-effect 保证（banner 的 `dms_armed` / `cancel_capable` / `write_capable`）不变，
   能力与观察的区分不变。新增 `test_zero_orders_does_not_prove_no_dms_release` 防回归。
2. **`PROTOCOL_VERIFIED_REASON` 收窄。** 原文说「没有任何请求被发到真实 Ondo venue」，与已完成的公开预检/录制
   矛盾。现改为「这些报告里没有经过鉴权的私有/sandbox 协议验收：公开面已被读取与录制，但 REST 鉴权头、WS 登录
   签名顺序、真实 private 帧与 DMS 语义仍是文档规定、未被 host 证实」。`protocol_verified` 仍为 `false`，未提升。
   新增 `test_protocol_reason_does_not_deny_the_observed_public_reads` 防回归。

同步更新的路径：`src/ondo_probe.py`（docstring、`PROTOCOL_VERIFIED_REASON`、`bounded_stop`、
`converging_stop_document`、`unverified_document`、退出码日志）、`tests/test_ondo_probe.py`、`docs/ondo.md`
（开篇第 1/2 条、§4 组表与退出码说明、§6.1）。未跑 cargo / 构建，native cleanup Pi 仍拥有该 target。

# R5.1 阶段验收报告 — release wheel、公共导出、干净环境验证

日期 2026-09-19（UTC）。run id `20260919T062234Z-r5`。
上游依据：`docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md` §R5.1。

## 0. 一句话结果

**R5.1 完成**：包含 Ondo 的 release wheel 在**严格警告门（0 warnings）**下构建成功并落盘
`E:/nautilus_trader/dist-r5/`；exec factory/config 进入 `adapters.ondo` 公共导出且 stub 已随生成器
更新；wheel 在候选干净 venv 通过导入/导出/全套测试/有限 probe 验证后，已同步到 app 依赖与主 venv。
R5.1 的 fixture manifest 索引补全（signing vectors 入册）。

## 1. 两个 SHA 与工作区

工作方式：本阶段用两个 git worktree + `task/ondo-r5` 分支（用户许可），结束后 ff-only 合并。

| repo | 起始 HEAD | 分支 | 提交 |
|---|---|---|---|
| app  | `29a0adf` (main)  | task/ondo-r5 | 见 §6 |
| fork | `1057738` (onde-perps) | task/ondo-r5 | 见 §6 |

`.worktrees/` 规则以 `.gitignore` 提交的形式进入两个仓库（worktree 隔离目录不再污染 status）。

## 2. 构建（fork worktree，release profile）

构建 runbook 与全部日志在 fork worktree 的 `build-logs-r5/`（该目录被 worktree 的
`.git/info/exclude` 覆盖，不提交；摘要见 fork 的 `BUILD-REPORT-r5.md`，本报告 §2.1 复述关键行）。
环境按 `BUILD_WINDOWS.md`：clang、`PYTHONUTF8=1`、CONDA_* 全部 unset、toolchain 1.98.0。

| step | 结果 |
|---|---|
| `uv sync`（worktree 独立 venv） | exit 0 |
| **strict** `maturin build --release` | **exit 0，`grep -c "warning:"` = 0，29m05s** |
| 同上 + `CARGO_BUILD_WARNINGS=allow`（兜底重跑） | exit 0，0.65s（全缓存） |
| stub 再生成（`generate_stubs.py`，release profile） | exit 0（首次尝试被 sub agent 的 bash 超时打断，detached 重跑 22m12s） |
| stub 之后的 wheel 重打包（主 session） | exit 0（22m24s，让 wheel 内含新 stub） |

### 2.1 平台例外（R5.1 task 2）的最终定性

R2 记载的 `linker_messages` 严格失败（`nautilus-persistence-macros` 的 MSVC 链接器 stdout 消息被
`build.warnings = "deny"` 拒绝）**只发生在 `cargo test` 的链接模式**。本阶段在全新 target 目录上的
strict `maturin build --release` **通过了，零 warning**——两种观察都保留，互不推翻。`allow` 只在
3 条命令上按次使用，从未写进任何配置文件，也从未冒充严格门通过。

### 2.2 最终 wheel

| 项 | 值 |
|---|---|
| 路径 | `E:/nautilus_trader/dist-r5/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl` |
| sha256 | `82a3953c6c6c8bc39c0e2c269bbb7002c5bf54165354295a16109d5cac71a7a9` |
| 大小 | 63,605,318 B；内含 `_libnautilus.pyd` 157 MB（release；R4 的 nextest wheel pyd 355 MB） |
| wheel 内 stub | 与 worktree 再生成的 `adapters/ondo/__init__.pyi` 逐字节一致（已验） |
| 外部依赖 | maturin 仍告警 `zlib.dll`（构建时从 conda 目录解析，未打包）——与 R4 相同；PATH 剥离 conda 后的 import 验证见 §3 |
| 回滚目标 | `dist-r4/` wheel sha256 `2e7b0b6e0511b08fe19628b81b17a25497d939de1aa72bb44c776fd4740498f0`，仍在盘上，未覆盖 |

## 3. 候选干净 venv 验证（app worktree，uv-managed CPython 3.12.9）

- wheel 装进新建 venv：direct_url.json = `file:///E:/nautilus_trader/dist-r5/...`（验证脚本断言 `dist-r5`）
- `verify_clean_venv.py`（本目录，可复现）：**ALL CHECKS PASSED** —— 两个 factory、exec config、
  `__all__` 8 名（含 `OndoExecutionClientConfig` / `OndoExecutionClientFactory`）、star-import、
  config 构造、`LiveNode` import。conda 从 PATH 剥离后 import 正常（zlib.dll 不是 import 时依赖）。
- 全套 app 测试（干净 venv）：**816 passed, 1 warning, 97 subtests** —— 与主 venv 基线一致
- 有限 probe（干净 venv）：`--mode public` 与 `--mode paper` 各 2 分钟，**均 exit 0、
  `complete=True`、stop_condition=deadline**；产物在 `smoke-public/`、`smoke-paper/`。
  注：当天是周六，美股闭市，probe 的验收点是会话生命周期而非成交量；历史 tape 重放属 R5.2。
- fork 侧公共导出回归（`python/tests/unit/adapters/test_public_exports.py`，含全部 21 个 adapter）：
  **143 passed**（运行时 `__all__` 与 stub 逐名一致、禁泄露 `*HttpClient` 后缀、名字全部可解析）

## 4. R5.1 task 3 — 公共导出与 stub 检查

- `python/nautilus_trader/adapters/ondo/__init__.py`：`__all__` +2（exec config / factory），
  对齐 aster facade 惯例；`OndoHttpClient` 仍走直接 import（forge 测试禁其入 `__all__`）
- stub 用仓库自带生成器（`generate_stubs.py`）再生成：`adapters/ondo/__init__.pyi` 的 `__all__`
  同步为 8 名，并补上此前缺失的运行时属性声明（`base_url_ws`、`account_read_only`、
  `dms_max_failed_renewals`、`journal_path` 等）
- 再生成产生的 42 个非 ondo `.pyi` 纯 CRLF 噪音已按 BUILD_WINDOWS.md problem 6 的先例还原，不提交

## 5. R5.1 task 6 — fixture manifest

`crates/adapters/ondo/test_data/manifest.json` 补 `signing_rest_vectors.json` 条目（kind=synthetic、
sha256、生成器与消息规则出处、`exercised_by: tests/signing.rs`）。fixtures 现为 9 条：
6 observed + 1 official-example + 2 synthetic；「只有真实私有响应可成为 private observed
fixture」——目前没有任何 private observed fixture，现状不变。

## 6. 提交

### fork（branch task/ondo-r5，ff-merge 进 onde-perps）
```
<sha> ondo: export the execution config and factory, and index the signing vectors (R5.1)
```
- `python/nautilus_trader/adapters/ondo/__init__.py` +2
- `python/nautilus_trader/adapters/ondo/__init__.pyi`（生成器产物，+14）
- `crates/adapters/ondo/test_data/manifest.json` +14
- `.gitignore` +3（`.worktrees/`）

### app（branch task/ondo-r5，ff-merge 进 main）
```
<sha> ondo: point the dependency at the R5 release wheel and record the R5.1 acceptance (R5.1)
```
- `pyproject.toml`：wheel 路径 → `dist-r5`（含 sha256 与回滚命令注释）
- `.gitignore` +3（`.worktrees/`）
- 本验收目录（baseline.md、verify_clean_venv.py、两次 smoke 产物与日志）

## 7. 环境同步

主 checkout 的 venv 已重装 release wheel 并全量回归（816 passed）；`.venv` 与 wheel 均不被 git 跟踪。

## 8. 未解决 / 移交 R5.2

1. `zlib.dll` 未打包（maturin 告警，与 R4 相同）：import 时不需要（§3 已验证），但任何运行时
   用到该外部 DLL 的路径没有被这个验证覆盖——R5.2 的 probe 若出现加载失败，第一嫌疑是它。
2. 收敛停止不可达（R3 §7 第 1 条）仍是 R5.2 sandbox 提交的前置阻断，未在本阶段触碰。
3. 协议待验证 6 项原样保留，没有任何 sandbox 请求发生。
4. fork worktree 里 `maturin develop --release` 做过一次 editable 安装（供 fork pytest 跑回归），
   该 venv 与 worktree 不被 git 跟踪，无影响。

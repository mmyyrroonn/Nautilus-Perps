# Native issue 84：本地实现与双平台证据（2026-10-03）

关联：https://github.com/mmyyrroonn/nautilus_trader/issues/84

结论：实现已完成，本地原生检查、双平台新 wheel 构建/安装身份验证和应用审计回归通过；issue 尚未满足完整验收。完整应用 loopback 联测未启动，双平台正式 joint audit 均为 failed。尚无本轮 clean-runner run/artifact 链接，未 commit、push、发布或关闭 issue。

## 修改范围

原生工作树：`E:/persarb/worktrees/issue84-native`。应用工作树：`E:/persarb/worktrees/issue84-app`。既有主工作树的用户改动保持原样。

- 原生默认入口保留 Aster/Ondo，加入 Backpack；自动触发路径覆盖 Backpack Rust/Python、共享 PyO3、相关测试和配置。
- 同一入口分别记录 blocking fmt、nextest、doctest、Python-feature；Clippy 维持非 blocking，完整 stdout/stderr 与哈希保留。
- 联合工作流固定 Windows/Linux 和两仓库完整 SHA；显式运行 Aster/Ondo/Backpack/Portfolio，保留 no-.env、loopback-only、零应用 skip 和失败层审计。
- 实际安装 wheel 验证 Backpack 34 个导出、stub、二进制、文件来源；必须收集实际扩展、public、readonly account、restricted loopback 和拒绝新增风险测试，另保留 Aster LiveNode。
- 安装与联测记录应用源文件逐项 SHA256，包含 .github。审计直接核验 wheel/provenance、原生源指纹/锁文件/构建输入，并比较 build/install/test 的同一 wheel。
- Windows 隔离发现真实 Python host/base executable，连同 venv launcher 一起加临时防火墙规则；发现、安装、清理失败均 fail closed。mock 仅替换被测模块局部绑定，避免污染 Python 标准库。

## 固定基础与本地源身份

这些 SHA 是已检出的基础提交，不能代表未提交修改后的新正式候选：

| 仓库 | 完整 HEAD | 基础 tree |
|---|---|---|
| Native | f06bfe809d8dab319cf5b85fcd5910412417ba16 | 6274c4057f24109117fce82b7e13ece205dedc10 |
| App | 4b99030a1ef0d86dc51273dd51d782a8b9a37b91 | 89e3c0d1a947ce20df2d0b9f8bf7fbd4870f5ee6 |

Native 检查与两份实际 wheel 的共同源 fingerprint：`0b20a8feb09163c4030e4a43dafdfa46bbd198abe546d2bc603caaddc7aafe2b`；两平台检查均 `identity_changed_during_checks=false`，四个 native 文件未提交。两份 installed.json 的 app 源文件清单完全一致；tracked content SHA256 为 `9143258f5db668ab9eb67cb867671f2e6c19cf79855bd8cb9cecf611def067de`，untracked source SHA256 为 `57304e0d8a81588682f2c4cc24cae56ca80b813c61a8da6b100268fb45afd22d`。

## 本轮实际结果

| 检查 | Windows | Linux / Ubuntu 24.04 WSL |
|---|---|---|
| fmt / nextest / doctest / Python feature | 各 exit 0 | 各 exit 0 |
| 原生 nextest | 2195 passed，1 skipped | 2194 passed，1 skipped |
| Clippy | exit 101，非 blocking | exit 101，非 blocking |
| 原生入口 unittest | 9 tests，2 平台 skip | 9 tests，全通过 |
| 新 wheel 构建、安装与来源绑定 | 通过 | 通过 |
| 应用审计/身份回归 | 104 passed，0 skipped | 104 passed，0 skipped |
| 完整应用 loopback 联测 | 未启动：防火墙权限不足 | 未启动：普通用户无法建立 network namespace |
| 正式 joint audit | failed | failed |

104 项为 test_native_candidate、test_joint_audit、test_offline_guard 和实际 wheel 扩展身份测试的限定集合，不是完整应用套件，也不包含新增风险拒绝测试或其他网络场景。原生 skip 与应用零 skip 分开记录。两平台 Clippy 的已有 Ondo 债务未清理。应用修改的 Ruff 和两个仓库 git diff --check 通过。

构建使用 CPython 3.12.9、uv 0.12.6、Rust 1.98.0、maturin 1.15.0。本地 wheel 使用 nextest profile；正式 workflow 保持 release profile，需重新构建验证。本轮不引用历史 wheel 测试数作为新候选验收。

## 实际产物身份

| 平台 | Wheel SHA256 | 嵌入 native binary SHA256 |
|---|---|---|
| Windows cp312 win_amd64 | ce6e0bdecbaa4ca3397c45a9be4a3eca06e21368edbc0c86313b6ba0237c4ab9 | 524c130b63d0b17a9f68512119309c410e09c9af5f893b4cbfaf4bb9023c65c9 |
| Linux cp312 manylinux_2_39_x86_64 | 6845f9066d1e206a467d5adb61d33257f0f7ec198cae56509b4a175a7d6bf596 | 233def9edb06293db13680879e445fbd91b4710885c3ccf362ad977cef5e8eb8 |

共同 Backpack stub SHA256：`d67a3b654a1980471a6a0e9e56e9b176e9d10a0619342e046219b243d1dc26c2`。

证据根目录：`E:/persarb/worktrees/issue84-evidence`。每个平台目录保留 native-checks.json、native-checks-logs/、wheel-output/{wheel,native-provenance.json,native-build-input.json}、installed.json、audit-regressions.junit.xml、joint-audit.json。Windows guard 为 windows/offline-guard.json；Linux guard 预检为 linux/offline-guard-preflight.json。Linux 用真实 git-dir/work-tree 映射访问 Windows Git worktree，不替换提交或伪造身份。

## 剩余验收与权限

两份正式审计均记录 checkout/native_checks/build/install_identity/offline_network/integration_tests 失败：前四层因候选源未提交、正式审计要求 clean SHA 而不能正式接受，并非对应本地执行未成功；offline_network 为真实隔离权限失败；integration_tests 为记录缺失、未执行。dotenv 层通过，工作树没有 .env。

Windows 防火墙安装真实返回 Access is denied，guard exit 2，临时规则清理完成。Linux 普通用户 unshare 和 passwordless sudo 均无法建立隔离。自动审批拒绝以 WSL root 启动 guard，理由是用户尚未明确授权 root；没有执行该 root 操作。该授权请求仍待用户回复。

完成 issue 需要按工作区 AGENTS.md 的「未明确要求时不要 commit / push」取得明确授权，提交两仓库改动，取得新的完整 SHA 对，并启动现有 Windows/Linux 联合工作流。两平台真实通过后将 run/artifact 链接回填 issue；若某平台失败，则保留失败层与日志并继续修复，不能提前关闭 issue。若要继续本机 Linux 完整联测，另需用户授权仅以 WSL root 建立临时 loopback namespace，不修改持久系统设置。

本轮未读取密钥、未使用 venue 账号、未连接 venue 下单。原生、wheel 或 CI 成功不等于 Backpack 真实账户/生产写入验收。

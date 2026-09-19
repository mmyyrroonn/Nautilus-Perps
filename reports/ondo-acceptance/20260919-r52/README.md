# Ondo 退出清理与 R5.2 数据验收

日期：2026-09-19。用户授权范围：补通并验证真实客户端退出清理；使用当前分析器重放旧 ONDO–HL 数据；补充有限时公开观察。Pi 负责主要实现与执行，Astra 负责分派、安全边界和验收；多轮审查后仍有的异步所有权问题由 Astra subagent 修复。

## 当前状态

| 项目 | 状态 | 证据 |
|---|---|---|
| 原生退出清理 | 离线验收通过 | [最终独立复审](review/final-native-review.md)、[所有权修正与最终哈希](cleanup/ownership-followup/README.md) |
| R5.2 数据范围 | 验收通过 | [数据报告](data/README.md)、[主 agent 独立复核](review/data-review.md) |
| 应用能力识别与报告 | 源码/测试验收通过 | [集成报告](integration/README.md)、[独立复核](review/integration-review.md) |
| 新 release wheel / 候选运行时 | 已交付，候选验收通过 | [构建报告](build/README.md)、[构建清单](build/manifest.json) |
| 主目录源码 / 主 venv 同步 | 已同步并独立验证 | [同步记录](review/canonical-sync.md)、[运行时身份](review/canonical-runtime-identity.json) |

真实 sandbox 鉴权、下单、成交和 DMS 协议均未在本轮验证；主网写入没有启用。

## 退出清理

真实客户端 `disconnect()` 已接入有界清理：停止新增风险，处理本次运行拥有的订单，核实订单终态及成交一致性，满足条件后才解除 DMS，并结束持有的请求任务和私有传输。未知、超时和未对齐状态返回明确失败并保留恢复信息。只读恢复、外部订单、同步停止后异步清理、重复停止、连续两轮运行和调用者中断均有相应回归。

首次 Pi 交付未直接接受。两轮独立审查发现并修复了错误确认仍在工作的订单、只读恢复误撤单、未结束请求任务、超时范围不足、吞掉失败结果、跨轮次复用成功结果及传输句柄脱离所有权等问题。保留了失败测试与修正证据：[首轮审查](review/shutdown-review-1.md)、[第二轮审查](review/shutdown-review-2.md)。

验证记录：全 crate **901 passed**，Python feature **6 passed**；最后加强的真实 `disconnect → journal → 新客户端 → 原订单 ID 终态恢复 → Ready` 用例通过，整组 `private_runtime` **49 passed**。Astra 独立复跑四项所有权回归和该重启恢复用例，均通过。Windows Cargo 测试采用已记录的逐命令链接器 warning 例外，不能表述为严格构建通过。

跨进程恢复需要配置持久化 `journal_path`；默认不配置 journal 的会话不具备落盘恢复保证。应用当前没有逐次原生 `StopReport` 遥测，因此取消、确认和 DMS 释放动作未观测时为 `null`。安装的适配器支持清理，不等于某个真实账户已清理。

## 数据验收

- 旧录制共 25 个文件，前后 SHA256 全部相同。Astra 独立重放后，两份 CSV 与 Pi 的结果逐字节一致。
- 旧数据缺失真实数量步长，12 个分组原先通过的记录均被当前规则拦为 `quantity_step_unknown`。经济结果不可评估，不能称为已验证的零机会市场。费用与其他拒绝计数的变化已逐项解释；缺失场景用明确标记的合成数据和回归测试验证。
- 新公开观察持续 **303.898 秒**、退出码 0，NVDA/TSLA 的 ONDO、HL 两条腿都有实际更新。录制报告完整，drop/gap 计数均为 0；这不构成长时稳定性或交易所级无丢失保证。
- 新数据具备真实数量步长，但合约映射仍未验证、退出未闭合，所有行保持 `executable=false`。计入 5 bp 预留后，本次 watcher 没有写出净正记录；不据此声称盈利或成交。
- 数据相关 **244 项测试**由 Astra 独立复跑通过。应用报告定向 **132 项测试**独立通过；Pi 在旧包环境的修正后全套应用测试为 **836 passed、97 subtests passed**，保留一条既有 maker 测试警告。

## 构建与交付边界

本次 release 严格构建首先在 `nautilus-persistence-macros` 的 MSVC 中文链接信息上被 warning 门拒绝，日志保留在 `build/02-strict-build.log`。后续构建使用逐命令的平台例外，未修改全局警告配置；不能称为严格构建通过。

最终 wheel 位于 `E:/nautilus_trader/dist-r52/nautilus_trader-2.0.0rc4-cp312-cp312-win_amd64.whl`，SHA256 为 `4d19c5df5bec73391a760d4b8643e2f8eaa53ce183dca88ee917454e2b91d642`。候选环境全套应用测试通过，公开 probe 正常结束（131 秒、退出码 0、完整报告）；生成的 stub 与运行时能力一致。

Astra 随后将 7 个 fork 文件和 5 个应用文件同步至两个主目录，逐文件哈希全部匹配。主 venv 已安装同一份 wheel，原生二进制和 stub 与 wheel 内字节哈希一致，`supports_ordered_shutdown` 是只读的精确布尔 `True`。主目录最终全套应用测试：**836 passed、97 subtests passed**，仅保留既有 maker 测试的一条警告。离线 dry-run 保持协议未验证、账户清理未证明、实际动作未观测为 `null`。

本轮没有提交、合并或推送 Git，也没有启动常驻采集或交易服务。旧 R5 wheel 保留用于回滚。

## 提交与合并授权（2026-09-19 补充）

用户在本轮验收通过后另行授权本地提交与 fast-forward 合并：fork 的 7 个已验收路径提交到 `task/ondo-r52-cleanup` 并 ff-only 合入 `onde-perps`；app 的 5 个已验收路径、`AGENTS.md` 与本证据包提交到 `task/ondo-r52-app` 并 ff-only 合入 `main`。仅本地操作，无 push/pull/fetch、无远端消息、无部署或交易动作。提交前在 fork worktree 重跑 `cargo +1.98.0 test -p nautilus-ondo --locked --offline`（901 passed）与 Python feature（6 passed）；在 app worktree 用候选 venv 重跑全套测试（836 passed、97 subtests passed、1 条既有警告）。两仓库均使用已记录的逐命令 Windows linker_messages 例外，不是严格构建。上一节关于验收期间没有 Git 动作的陈述描述的是合并前状态，仍然成立；授权范围与最终提交 ID 见 `control/merge-scope.json` 与本地合并记录 `control/merge-result.json`。

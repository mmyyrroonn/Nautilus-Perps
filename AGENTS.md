# Nautilus-Perps

遵守上层 [工作区规则](../AGENTS.md)。本文件是项目规则入口；`CLAUDE.md` 只负责引用。

这是基于 NautilusTrader 的跨所价差 / 做市实盘可行性验证项目，每次只做回答当前问题所需的工作。

## 技术约束

- 依赖 fork 位于 `E:\persarb\nautilus_trader`，基于 `nautilus_trader==2.0.0rc4`。使用 2.x 的 `docs/latest` 文档。
- Windows 应用使用 CPython 3.12.9、uv 0.12.6；原生构建使用 Rust 1.98.0、maturin 1.15.0。`uv pip` 必须显式指定目标虚拟环境的 `--python`。
- wheel 构建、SHA-256 校验和来源证明见 `docs/native-candidate.md`，通过 `scripts/install_native.py` 安装；禁止退回同版本 PyPI 包。fork 的 `BUILD_WINDOWS.md` 是历史记录。
- 交易额度配置在 `config/limits.toml`，硬编码兜底在 `src/live_limits.py` 和 `src/ondo_trade_limits.py`；配置只能收紧限制。Ondo 操作说明见 `docs/ondo.md`。
- Aster 没有 depth10 订阅；加载 instrument 必须限定 `load_ids`，避免 exchangeInfo 限流。
- 资金费 CSV 保留原始值：HL 为小时分数，Lighter 为百分数，Aster 按品种 1 / 4 / 8 小时结算；折算见 `src/analysis/opportunities.py`。

计划在 `docs/`，验收证据在 `reports/`。只有任务涉及阶段推进时才读取并核对专项计划；旧计划不自动恢复执行。

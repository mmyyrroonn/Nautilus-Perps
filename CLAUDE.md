# Nautilus-Perps

Hyperliquid (xyz HIP-3 美股永续) × Lighter × Aster × Ondo 的跨所价差 / 做市**实盘可行性验证**。基于 NautilusTrader，Python 策略层 + Rust 适配器。
这是一个验证项目，不是产品：每一步只做回答当前问题所需的最少工作。本仓库与依赖 fork `E:\persarb\nautilus_trader` 同属 `E:\persarb` 工作区，共享规则见上层 `AGENTS.md`。

## 硬规则
- **密钥只放 `.env`（已在 .gitignore），永远不进 git、不进日志、不贴进对话。** 发现密钥泄漏进任何文件立即停下报告。
- **主网真实下单前必须用户在当轮对话里明确说「上主网」**，之前只允许 testnet / paper。用户没说，默认不允许。
- 主网阶段单笔名义额上限和总敞口上限写在 `config/limits.toml`（做市 `[order]`/`[inventory]`/`[exposure]`，Ondo `[ondo_trade]`），代码里硬编码兜底（`src/live_limits.py`、`src/ondo_trade_limits.py`），不能被配置放大。
- `C:\Users\myron\perps-arb` 是旧仓库，**只读参考**（费率、instrument 映射、纸面参数），不要改它。
- 阶段推进（见 PROMPT.md）每完成一阶段向用户汇报真实输出再进下一阶段，不要自行跳阶段。

## 环境
- Windows 11; app CPython 3.12.9 and uv 0.12.6; native build Rust 1.98.0 and maturin 1.15.0.
- Manage the fork wheel through the SHA-256 candidate flow in `docs/native-candidate.md`; the project dependency lock has no equal-version PyPI fallback.
- 版本钉在 `nautilus_trader==2.0.0rc4`（v2 Rust-first 结构，单个 `_libnautilus.pyd` + `.pyi` 存根）。原因：Lighter 适配器的 Python 面只存在于 2.x，稳定版 1.231.0 没有。看文档用 `docs/latest`（跟踪 2.x），不要看 1.x 文档
- Conda base may be active. Every uv pip invocation must pass --python with the exact intended application venv interpreter; the default project path is .venv/Scripts/python.exe on Windows.

## Aster / Ondo / NautilusTrader fork
- Aster / Ondo 适配器不在上游，来自本地 fork：`E:\persarb\nautilus_trader`，分支 `main`（基于 v2.0.0rc4，aster 系列提交已合入，无独立 aster 分支）。
- Current build and binding steps are in `docs/native-candidate.md`; `E:\persarb\nautilus_trader\BUILD_WINDOWS.md` is a historical Windows build record.
- Install with `scripts/install_native.py` using the explicit application interpreter, wheel hash and native provenance.
- Aster 数据面由 Binance 适配器派生：没有 depth10 订阅；exchangeInfo 限流严格，必须只传 `load_ids`。
- 资金费结算：HL 每小时（原始值为小时分数），Lighter 每小时（原始值是百分数，适配器不除 100），Aster 按品种 1 / 4 / 8 小时（`/fapi/v1/fundingInfo`）。CSV 存原始值不归一，折算见 `src/analysis/opportunities.py`。
- **Aster / Ondo 主网真实下单同样受「上主网」规则约束**；Ondo 的受控下单另有 `[ondo_trade]` 包线与 `docs/ondo.md` runbook。用户没在当轮说，默认只读。

## 交流
中文交流；代码、注释、commit 用英文。

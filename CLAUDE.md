# Nautilus-Perps

Hyperliquid (xyz HIP-3 美股永续) × Lighter 跨所价差**实盘可行性验证**。基于 NautilusTrader，Python 策略层 + Rust 适配器。
这是一个验证项目，不是产品：每一步只做回答当前问题所需的最少工作。

## 硬规则
- **密钥只放 `.env`（已在 .gitignore），永远不进 git、不进日志、不贴进对话。** 发现密钥泄漏进任何文件立即停下报告。
- **主网真实下单前必须用户在当轮对话里明确说「上主网」**，之前只允许 testnet / paper。用户没说，默认不允许。
- 主网阶段单笔名义额上限和总敞口上限写在 `config/limits.toml`，代码里硬编码兜底，不能被配置放大。
- `C:\Users\myron\perps-arb` 是旧仓库，**只读参考**（费率、instrument 映射、纸面参数），不要改它。
- 阶段推进（见 PROMPT.md）每完成一阶段向用户汇报真实输出再进下一阶段，不要自行跳阶段。

## 环境
- Windows 11，Python 3.12.4，uv 0.6.10，Rust 1.97.1
- 用 `uv` 管 venv 与依赖；先装 PyPI 的 `nautilus_trader` wheel，只有 wheel 不可用时才考虑源码编译（先问用户）

## 交流
中文交流；代码、注释、commit 用英文。

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
- Windows 11，Python 3.12.4，uv 0.6.10，Rust 1.86.0
- 用 `uv` 管 venv 与依赖；先装 PyPI 的 `nautilus_trader` wheel，只有 wheel 不可用时才考虑源码编译（先问用户）
- 版本钉在 `nautilus_trader==2.0.0rc4`（v2 Rust-first 结构，单个 `_libnautilus.pyd` + `.pyi` 存根）。原因：Lighter 适配器的 Python 面只存在于 2.x，稳定版 1.231.0 没有。看文档用 `docs/latest`（跟踪 2.x），不要看 1.x 文档
- 本机 conda base 常驻激活，`uv pip` 会优先装进 conda 而不是 `.venv`：**所有 `uv pip` 命令必须带 `--python .venv\Scripts\python.exe`**

## 交流
中文交流；代码、注释、commit 用英文。

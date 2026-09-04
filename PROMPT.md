# 启动提示词：Hyperliquid × Lighter 美股永续价差实盘验证（基于 NautilusTrader）

> 新对话第一句：`读 PROMPT.md，从阶段 0 开始`。
> 本文件由旧仓库 `C:\Users\myron\perps-arb` 的会话于 2026-09-04 生成，把已确认的事实带过来，避免重复调研。

## 0. 要回答的唯一问题

**Hyperliquid（xyz HIP-3 美股永续）和 Lighter 之间的美股永续价差，在真实下单条件下能不能吃到、净值是正是负。**

纸面模拟（旧仓库已经做了）回答不了这个问题：它不知道下单到达时簿是否已变、Lighter nonce 是否卡住、两腿一成一不成怎么办。所以本项目的终点是**小额主网双腿真实成交 + 事后对账**，不是更好的模拟。

不做的事：GRVT / Extended（两家在任何框架里都没有适配器，另开项目）；做市（maker）策略；美股之外的标的；性能优化；通用框架封装。

## 1. 已确认的事实（2026-09-04 联网核实，不要重查）

**为什么选 NautilusTrader**（https://github.com/nautechsystems/nautilus_trader ，28k★，LGPL-3.0，2026-09-04 仍在提交）：Rust 生态里唯一同时内置 Hyperliquid 与 Lighter **执行**适配器的框架。barter-rs 零 DEX、执行层只有 Binance；hftbacktest 停更；Hummingbot 是 Python 且 DEX 连接器靠交易所买赏金维护。旧仓库自己的 Rust crates 执行层为零（`NullExecution` only）。

**Hyperliquid 适配器**（https://nautilustrader.io/docs/latest/integrations/hyperliquid/ ）：Rust 原生，支持 spot / perps / **HIP-3 builder perps**（我们的 xyz dex 就是 HIP-3）。数据面：trade、quote、L2 deltas/snapshots、bars、mark/index、funding rate、OI。执行面：market/limit/stop、GTC/IOC、post-only、reduce-only、批量、cancel-replace。已知限制：杠杆需在 UI 手工设置；历史成交依赖官方 indexer；vault 交易禁用 builder attribution。

**Lighter 适配器**（https://nautilustrader.io/docs/latest/integrations/lighter/ ，crate `nautilus-lighter`，标 stable）：spot + perp，含 funding / mark / index / L2 deltas / 深度快照；执行面批量单命令 ≤15 笔、杠杆调整、持仓管理。限制：不支持 OCO/OTO/iceberg/TWAP；`CancelAllOrders` 只按本地缓存的本 instrument 挂单；**nonce 要自己管、WS 重连后要用 REST 重新同步交易基线**；链上限流 40 req/min per L1 address。Lighter 签名需要外部原生库（官方只出 Python `elliottech/lighter-python` 和 Go）。

**官方 SDK 状态**：HL 官方 Rust SDK 2025-10 停更，社区 `infinitefield/hypersdk` 活跃；HL 官方 Python SDK 活跃。Lighter 无 Rust SDK。这些只在 Nautilus 适配器不够用时作备选。

**未核实、阶段 0 必须先查清的三件事**：
1. Nautilus 的 HL 适配器能否订阅并下单到 **xyz dex**（HIP-3 builder dex，symbol 形如 `xyz:NVDA`），而不只是主 dex。
2. HL **testnet** 上是否存在 xyz dex 的美股永续；Lighter testnet 上是否有美股市场。若 testnet 没有，testnet 阶段只能用加密永续验证执行链路，美股价差只能在主网小额验。
3. `nautilus_trader` 在 **Windows / Python 3.12** 有没有可用 wheel（含 hyperliquid、lighter 适配器）。

## 2. 从旧仓库带过来的参数（`C:\Users\myron\perps-arb\config\`，只读）

| 项目 | 值 | 来源 |
|---|---|---|
| 标的 | NVDA TSLA MSFT HOOD MU SNDK CRCL（先只做 NVDA） | `instruments.toml` |
| HL symbol | `xyz:NVDA` / `xyz:TSLA` / `xyz:MSFT`；xyz dex 内美股 szDecimals=3，价格 ≤5 有效数字、≤3 小数；min notional 10 USD | 同上，2026-09-04 从 `POST /info {"type":"meta","dex":"xyz"}` 核对 |
| Lighter market_id | NVDA=110, TSLA=112, MSFT=115；min_quote_amount 10 USDC | `GET https://mainnet.zklighter.elliot.ai/api/v1/orderBooks` |
| 费率 | HL xyz taker **0.9 bps**（tier0 1.5/4.5bp × HIP3 scale × growth 折扣，builder code 0）；Lighter standard taker **0** | `fees.toml` / `paper_mvp.toml` |
| 延迟假设 | HL 50 ms，Lighter 300 ms | `paper_mvp.toml` |
| Funding | 两家每小时 | 同上 |
| 单腿失败预留 | 5 bps（纸面校准：10k 决策规模下单腿恢复损失 1.9–4.5 bps） | 同上 |
| 最长持仓 | 30 s（taker-taker，进出都吃单） | 同上 |

旧仓库的纸面报告（`data/paper-*/paper-run.md`）若存在，先看一眼它给出的净 bps 分布，作为「实盘应该看到什么」的基线。

## 3. 阶段与验收（每阶段完成后贴真实输出，等用户确认再进下一阶段）

### 阶段 0 — 环境 + 三个未知项（目标：半天）
- `git init`；`uv venv`；`uv pip install nautilus_trader`；`python -c "import nautilus_trader; print(version)"`；确认 hyperliquid / lighter 适配器可 import。
- 查清 §1 的三个未知项，每项给出证据（源码路径 / 文档 URL / 实际请求响应）。
- **停止条件**：Windows 没有 wheel → 报告，问用户是走 WSL 还是源码编译，不要自行编译。

### 阶段 1 — 只读行情，跑通两家订阅（目标：1 天）
- Nautilus `TradingNode` 配置 HL + Lighter 两个 DataClient，订阅 NVDA 双方 L2 与 funding，无 ExecClient。
- 一个最小 Strategy：每次簿更新算 `bid_A − ask_B` 和 `bid_B − ask_A`，扣双边 taker 费与 5 bps 预留，把 >0 的时刻写 CSV（时间、方向、毛/净 bps、双边 top-of-book 量）。
- 跑 ≥30 分钟美股盘中。
- 验收：CSV 里有数据；净 bps 分布与旧仓库纸面基线同数量级。**若 30 分钟内一次净正都没有，停下报告**——后面阶段可能没必要做。

### 阶段 2 — testnet 执行链路（目标：1–2 天）
- 用户提供 testnet 密钥到 `.env`。
- 两家各下一笔 IOC 小单并撤单，验证：签名有效、成交回报进 Nautilus、position/balance 与交易所 UI 一致、断 WS 重连后 Lighter 基线同步正确。
- 双腿：在 testnet 上对同一标的（美股没有就用 BTC/ETH 永续）做一次同时双向 IOC，记录两腿到达时间差与成交结果。
- 验收：每项贴 Nautilus 日志 + 交易所侧证据（订单 id / UI 截图由用户提供）。

### 阶段 3 — 主网小额（只在用户明确说「上主网」后）
- `config/limits.toml`：单笔 ≤ 50 USD 名义，总敞口 ≤ 100 USD，单日最多 20 次触发；代码硬编码同样的上限兜底。
- 触发条件用阶段 1 的净 bps 阈值；一腿成交另一腿失败 → 立即市价平掉已成腿并停机。
- 跑一个美股交易日，输出对账表：每次触发的预期净 bps vs 实际成交净 bps、两腿延迟、失败次数。
- **这个表就是本项目的最终答案。**

## 4. 交付形态
```
E:\Nautilus-Perps\
  CLAUDE.md  PROMPT.md  .env.example  .gitignore
  pyproject.toml
  config/limits.toml
  src/spread_watch.py     # 阶段 1
  src/exec_probe.py       # 阶段 2
  src/spread_live.py      # 阶段 3
  reports/                # 每阶段的 CSV / 对账表
```

## 5. 委派与验证
- 全局 `~/.claude/CLAUDE.md` 规则照常：主 session 规划验收，重活派 sub agent；联网核实、跑安装、写代码派出去；架构取舍、任何涉及密钥与主网的操作自己做并先问用户。
- 每个「它说通过了」都要自己重跑一遍贴输出。

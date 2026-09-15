# Ondo Perps 完整接入 Implementation Plan

> **For agentic workers:** 使用 `superpowers:executing-plans` 按阶段实施并用 `- [ ]` 跟踪；没有该技能的执行环境直接遵守本文。用户已选定“行情、录制与交易适配分阶段交付，默认不启用实盘”。规划窗口只写方案和收集公开证据，没有实现、构建或安装适配器。不要把本文件当成主网交易授权。

**Goal:** 为本地 NautilusTrader 2.x fork 增加原生 Ondo Perps 数据与执行适配器，接入 Nautilus-Perps 的公开观察、完整盘口录制、离线分析和 sandbox 执行验证。

**Architecture:** 在 `E:\nautilus_trader` 新建 Rust crate `nautilus-ondo`，经 PyO3 导出 `nautilus_trader.adapters.ondo`；在 `E:\Nautilus-Perps` 增加显式 `ONDO` 行情腿和独立 sandbox probe。行情通过公开 WS 获取，REST 用于元数据、恢复、历史查询与订单对账；执行使用 API key HMAC，不引入钱包签名工作流。

**Tech Stack:** Python 3.12、现有 `nautilus_trader==2.0.0rc4` fork、Rust workspace 固定工具链、Tokio/serde/rust_decimal/nautilus-network、PyO3、pytest。Rust 版本以 fork 的 `rust-toolchain.toml` 为准，规划时为 `1.98.0`。

**Spec:** 本文件第 1–6 节定义范围和协议决策，第 7 节定义实施任务，第 8–10 节定义验收、构建交付和直接交接指令。当前官方材料和短测见 [规划证据](../../../reports/ondo-plan-2026-09-14/README.md)。

## 1. 范围、阶段与完成定义

“完整接入”指常规永续交易所适配器的数据、基本订单生命周期和账户对账闭环，不等于实现交易所全部产品功能。分五个阶段，每阶段独立输出改动、命令、结果和未验证项：

| 阶段 | 交付 | 完成门槛 |
|---|---|---|
| P0 协议与环境 | 真实协议 fixtures、字段映射、依赖基线、阻塞清单 | 官方 schema 与样本来源明确；不把文档例子标成实测 |
| P1 公开行情 | Rust DataClient、Python 导出、`--venues ONDO,ASTER` | 离线测试、wheel 导入、逐腿公开 smoke 分别通过 |
| P2 录制与离线分析 | 原始公开帧、完整 L2 tape、重放、同数量 VWAP 和成本分解 | 断流/丢帧可识别，重放确定性，深度不足保留，旧 CSV 兼容 |
| P3 交易适配 | sandbox 鉴权、基本订单、私有回报、重连对账、DMS | 离线故障测试通过；真实 sandbox 合约测试单独验收 |
| P4 应用验收 | sandbox probe、交付 wheel、runbook、兼容性回归 | 无未解释订单/持仓，主网写入硬关闭，已知限制逐项报告 |

首批跨所观察只映射 `NVDA`、`TSLA` 与现有 Aster USDT 合约；适配器 provider 动态发现 Ondo 的永续市场，但不能凭同名 ticker 给所有市场自动建立对冲关系。BTC 可作为 provider/WS 单所测试标的，不默认加入本次跨所经济分析，避免沿用本项目股票的 Aster 0.9 bp 费率评估加密合约。

本期支持：Instrument、QuoteTick、L2 快照转换、TradeTick、FundingRateUpdate、mark price；Limit GTC/IOC、Market（按 base size）、postOnly、reduceOnly；单笔/列表提交、单笔/按市场撤单、订单/成交/仓位/余额查询、资金费支付对账、重连恢复和 dead man's switch。

本期明确不支持的能力必须在调用入口返回具体“不支持”错误：原子改单、FOK/GTD、TWAP、条件单/TP-SL 的创建、quoteSize 市价买入、builder attribution、多资产抵押交易。外部产生的条件单、强平、ADL 仍须保留并在对账中识别，不能静默丢弃。

充值、提现、跨链、API key 创建/删除、SIWE/JWT 会话管理、积分领取、自动部署和把 Ondo 加入 `maker_live.py` 的双腿实盘策略均不在本期。交易适配器与现有做市策略的泛化是两项工作。

### 全局约束

- 先读取应用仓库 `CLAUDE.md`；改 fork 前读取其 `AGENTS.md`、`AI_POLICY.md`、`CONTRIBUTING.md`、`docs/developer_guide/adapters.md`、`spec_data_testing.md` 和 `spec_exec_testing.md`。旧 `PROMPT.md` 不是重新建项目或推进实盘的指令。
- 用户本次要求完整方案，没有要求当前窗口实施；执行窗口按实际授权阶段推进。阶段输出不达标不能宣布下一阶段已完成；可继续不依赖该阻塞的离线工作。
- 主网真实下单仍要求用户在执行当轮明确说“上主网”。**本期应用 probe 直接拒绝 production 写入，即使传入 live 开关也拒绝。** 后续主网接入另行验收，不在本计划中解除限制。
- DataClient 不读 key、不加载 `.env`、不创建 ExecClient。ExecClient 默认为 SANDBOX；缺凭据时报错，不能自动退到其他账户或生产环境。
- 密钥只由 `.env`/环境变量提供；禁止记录 header、签名、JWT、secret、cookie。测试使用假密钥，真实私有响应脱敏后才可进入 evidence。不得自动创建钱包或给 sandbox 充值。
- 价格、数量、费用、仓位、HMAC body 的原始十进制字符串保持精确。Rust 用 `Decimal`/Nautilus domain types；Python 计算订单数量和 VWAP 用 `Decimal`。不得经 `f64` 推导 tick/lot。
- 保留两个仓库既有 dirty/untracked 文件。不执行 `git clean`、`git add .`，不切换/删除用户已有分支；提交、合并、push 仅按实施窗口明确要求处理。
- 不升级 NautilusTrader 主版本、不替换 upstream、不重建 conda 环境；uv pip 命令必须指定应用 `.venv\Scripts\python.exe`。生成 `.pyi` 必须走 generator。
- 不把长时运行作为默认动作。2 分钟公开 smoke 与 sandbox 小额 probe 是独立验收；24–72 小时观察提供命令，按运行窗口授权启动，不在规划或测试收集阶段自动启动。

## 2. 当前基线与证据等级

核查日期：2026-09-14。

| 项目 | 当前结果 |
|---|---|
| 应用仓库 | `E:\Nautilus-Perps`，`main`，HEAD `76b9881de1a453dbc8c4cff150395bafe649c279` |
| 底层 fork | `E:\nautilus_trader`，当前分支 **`onde-perps`**，HEAD `047d494e3c260bc6f701aa8f9193d1fd36f8da89`；不是旧文档中的 `aster` 分支 |
| Ondo 实现 | fork 的 adapters 目录当前无 `ondo` crate；Aster/Lighter/Hyperliquid 均在 |
| 已安装 wheel | 应用 `.venv` 导入 `nautilus_trader.__version__ == 2.0.0rc4`；源码 HEAD 不构成 wheel 的构建证明 |
| 现有离线基线 | `tests/test_spread_watch.py tests/test_opportunities.py tests/test_ref_feed.py`：**83 passed**，不代表全套测试通过 |
| 官方材料 | 已保存 API key、WS connect/login、公开频道、DMS、REST/WS spec、fees/funding 文档及 SHA256 |
| 生产 REST | `/v1/markets`、contracts、depth、funding 本次返回 HTTP 403；复查 markets 的正文是 `error code: 1010`，边缘响应标记 Cloudflare。没有证据可将其直接归因为地域限制 |
| 生产 WS | 约 8 秒，NVDA/TSLA 均收到报价、十档深度、资金费；NVDA 收到成交；未鉴权 |
| 未执行 | 新 adapter、LiveNode Ondo smoke、sandbox 登录/订单/对账、wheel 构建、长时观察全部未执行 |

本次 WS 更新条数：NVDA BBO 5 / depth 32 / funding 2 / trades 2；TSLA BBO 17 / depth 31 / funding 2 / trades 0。TSLA 的零成交不能视为频道失败；两市场均有 trades 订阅确认。这个样本不证明全天连续性或可套利。

历史报告 [`ondo-research.md`](../../../reports/perps-candidates-2026-09-11/ondo-research.md) 的 81 个市场、61 active、maker 1 bp/taker 2.5 bp，是 **9 月 11 日**的观察。本次 fees 文档仍列限时费率，但没有今日账户费率和完整元数据实测。9 月 9 日报告关于 API funding 百分数的提示不可沿用：API 本身是小数费率。

执行者先对比 HEAD 和函数签名；分支名变化不自动构成失败，但不可覆盖其他人已完成的 Ondo 工作。

## 3. 架构选择与文件落点

采用原生 Rust adapter：现有 `LiveNode.builder().add_data_client()` 接收由 PyO3 暴露的 Rust factory；项目没有可直接套用的 v1 Python `LiveMarketDataClient` 扩展面。Aster 是 Binance 派生层，Ondo 协议不同，不能把它复制后替换 URL。

另外两种方案不采用：Python 独立 WS collector 容易先取得数据，但会形成第二套执行/恢复语义；只给旧 watcher 塞一个外部 actor 无法完成订单/账户适配。可以使用临时公开探针取得 fixtures，正式代码只保留一套生产协议实现。

```text
Ondo REST/WS
     │
nautilus-ondo (schema / precision / transport / recovery)
     ├─ OndoDataClient ── Nautilus cache + event bus ── SpreadWatch
     │                         │                         └─ CSV + L2 tape
     │                         └─ public raw recorder          └─ offline replay/VWAP
     └─ OndoExecutionClient ── orders/fills/account reports ── sandbox probe
                  └─ shared rate budget + reconciliation + DMS
```

### 3.1 fork 新增文件

以下路径相对于 `E:\nautilus_trader`，不创建独立第二个 Rust workspace。

| 路径 | 责任 |
|---|---|
| `crates/adapters/ondo/Cargo.toml`、`src/lib.rs` | workspace 依赖、crate features、模块导出 |
| `src/common/{mod,consts,enums,credential,parse}.rs` | ONDO 标识、环境、秘密包装、Decimal/时间/错误语义 |
| `src/config.rs`、`src/factories.rs` | 数据/执行配置，现有 DataClientFactory/ExecutionClientFactory traits |
| `src/http/{mod,client,models,query,error}.rs` | REST schema、请求序列化、限速、分页、错误分类 |
| `src/websocket/{mod,client,messages,parse,book}.rs` | WS 生命周期、频道路由、快照语义与订阅状态 |
| `src/data.rs` | Instrument provider、公开订阅、Nautilus 事件发布 |
| `src/recording.rs` | 有界队列的公开原始帧录制、轮转、丢帧与结束统计 |
| `src/signing.rs` | REST/WS API-key HMAC，不包含钱包签名 |
| `src/execution.rs`、`src/reconciliation.rs` | 订单命令、回报去重、账户初始化、断线恢复与 DMS |
| `src/python/{mod,config,factories,http}.rs` | Python 2.x API 导出 |
| `tests/{http_contract,market_data,signing,execution,reconciliation}.rs` | 离线 fixtures 和本地 mock-server 故障测试 |
| `test_data/`、`README.md` | 脱敏/公开 fixtures、来源、支持矩阵、运行说明 |
| `python/nautilus_trader/adapters/ondo/__init__.py` | 按现有 adapter 的 `_libnautilus` 导入和 fixup 模式导出 |
| `python/nautilus_trader/adapters/ondo/__init__.pyi` | generator 产物，禁止手写 |

表中缩写 `src/`、`tests/`、`test_data/` 均位于 `crates/adapters/ondo/` 下。模块过小时可合并，但不可把 HTTP、WS、执行状态机集中进一个文件。

fork 修改：根 `Cargo.toml` 的 workspace member/dependency；`crates/pyo3/Cargo.toml` 的 dependency、extension-module、high-precision features；`crates/pyo3/src/lib.rs` 模块注册。`Cargo.lock` 只接受依赖解析产生的必要差异。根据 generator 的真实发现机制处理新 adapter，不默认修改其他 adapter 的 stub。

参考实现：`architect_ax/src/{data,execution,factories}.rs` 的 traits 与生命周期；Lighter `websocket/parse.rs` 的 CLEAR/F_SNAPSHOT/F_LAST；Aster 的 Python 包和 PyO3 注册。只借框架惯例，不移植它们的协议与签名。

### 3.2 应用仓库文件

| 路径 | 动作 |
|---|---|
| `src/spread_watch.py` | ONDO registry、NVDA/TSLA 映射、runtime fee、完整 L2 tape 的显式开关 |
| `src/analysis/opportunities.py` | ONDO funding 单位登记；旧 CSV 列和语义保持兼容 |
| `src/market_tape.py` | 标准化 L2/event tape writer/reader，有界缓存、损坏尾行识别 |
| `src/analysis/ondo_depth.py` | tape 重放、共同 base quantity 的双向 VWAP、质量门控、成本明细 |
| `src/ondo_probe.py` | sandbox 专用命令，默认离线 dry-run；独立于 Aster probe |
| `src/ondo_preflight.py`、`tests/test_ondo_preflight.py` | P1 的公开元数据/状态/费率核查 CLI 及离线测试 |
| `tests/test_spread_watch.py`、`tests/test_opportunities.py` | 原用例基础上添加 Ondo 回归 |
| `tests/test_market_tape.py`、`tests/test_ondo_depth.py`、`tests/test_ondo_probe.py` | 新功能离线测试 |
| `docs/ondo.md`、`reports/ondo-acceptance/<UTC stamp>/` | runbook、各阶段结果和未验收项 |
| `.env.example` | 只新增 sandbox 环境变量名及空值；不写 `.env` |

不改 `src/exec_probe.py`、`src/maker_live.py`、`src/live_limits.py`、`config/limits.toml` 和 deploy 目录。本次交易代码不借用现有主网路径。

## 4. 数据协议必须落实的决策

### 4.1 地址、配置和 market mapping

Production REST/WS：`https://api.ondoperps.xyz` / `wss://api.ondoperps.xyz/ws`。Sandbox 历史文档地址为 `https://api.ondoperps-sandbox.xyz` / `wss://api.ondoperps-sandbox.xyz/ws`，本轮未测；执行前复查。

公开 Python API 定为：`ONDO`、`ONDO_VENUE`、`ONDO_CLIENT_ID`、`OndoEnvironment`（`PRODUCTION`、`SANDBOX`）、`OndoDataClientConfig`、`OndoDataClientFactory`、`OndoExecutionClientConfig`、`OndoExecutionClientFactory`、`OndoHttpClient`。

`OndoDataClientConfig` 至少包含 `environment`、`load_ids`、`base_url_http`、`base_url_ws`、`http_timeout_secs=15`、`ws_heartbeat_secs=20`、`book_limit=100`、`raw_md_path=None`；公开客户端不提供 secret 参数。`load_ids` 非空时只发布请求的 instruments，不把订阅扩大为全市场。

`OndoExecutionClientConfig` 至少包含 `environment=SANDBOX`、`account_id`、`api_key`、秘密包装的 `api_secret`、`dms_timeout_secs=30`、`reconcile_interval_secs=30`、`allow_production_orders=False`；本期生产写入请求在网络前拒绝。base URL override 只能用于同环境官方主机和明确的本地测试服务器，不能用 override 绕过环境判定。

`allow_production_orders=True` 本期也必须返回未支持，不实现可被配置打开的生产写入分支。`OndoHttpClient(environment, timeout_secs=15)` 暴露异步 `get_status() -> dict`、`get_markets() -> dict`、`get_contracts() -> list[dict]`（解包实际响应后返回）、`load_instrument_definitions(load_ids: list[str]) -> list[Instrument]`；前面三个返回保留原始十进制字符串的 schema 数据。P1 preflight 复用这些接口，禁止另写一套 urllib/requests 生产客户端。

| CLI symbol | ONDO raw_symbol | 规划 InstrumentId | 对照 Aster InstrumentId |
|---|---|---|---|
| NVDA | `NVDA-USD.P` | `NVDA-USD-PERP.ONDO` | `NVDAUSDT-PERP.ASTER` |
| TSLA | `TSLA-USD.P` | `TSLA-USD-PERP.ONDO` | `TSLAUSDT-PERP.ASTER` |

这是**新适配器约定的命名**，并非已存在 wheel 的实测输出。Venue/ClientId 均为 `ONDO`。明确区分 ONDO venue 与 `ONDO` 加密资产 ticker。

`GET /v1/markets` 的路径是 `perps.tradingPairs`，不能读取 tokenConfig 的现货代币当作 perps。官方 schema 明确 `market`、`baseIncrement`、`quoteIncrement`；真实响应的交易状态、手续费、限额可能比 schema 丰富，使用 P0 fixture 明确字段路径。`baseIncrement` 是数量步长，`quoteIncrement` 是价格步长，不能互换。

从精确小数字符串构建 instrument，quote USD、settlement USDC、线性非 inverse、数量为 base；这些产品假设与合约 multiplier 必须在元数据/产品说明中留来源。未知状态、未知单位或缺 tick/lot 的目标 instrument 不进入可用集合；不得由报价小数位猜测。返回 0 instruments 或漏掉任何请求的 load_id 都必须启动失败。

官方 market schema 未完整规定停牌字段。P0 建立 raw 状态→active/disabled/unknown 映射，保存原文；运行中每 60 秒低优先级刷新元数据。状态改变或请求失败保留上一版及过期标记，交易新单在状态不确定时停止，已有订单仍可撤单/对账。

### 4.2 WS 消息与完整快照

按当前官方 [depth 频道](https://docs.ondoperps.xyz/api-reference/public-channels/subscribe:-perps-depth-book.md) 和本次真实帧：

```json
{"op":"subscribe","channel":"depthBooksPerps","markets":["NVDA-USD.P","TSLA-USD.P"],"limit":100}
```

同连接订阅 `topOfBooksPerps`、`tradesPerps`（`numPastTrades:0`）、`fundingRatesPerps`、`markPricesPerps`。`depthLevels` 是**价格分组**，不是档数，默认省略；若服务要求该参数，先取得经确认的未聚合参数，不能写死所有市场 0.01。

服务器更新的 `data` 是数组；每项按完整 `market` 路由。处理 `subscribed/unsubscribed/error/pong`，维护期望订阅和确认订阅两个集合。订阅确认不等于行情覆盖通过。

`depthBooksPerps` schema 是 `BookSnapshot`；真实帧也包含整组 bids/asks，无 exchange sequence/checksum。每个市场每次**完整替换**其被覆盖范围的盘口：向 Nautilus 发布一个 `CLEAR + ADD...` 批次，正确标记 `F_SNAPSHOT` 和末条 `F_LAST`。空快照只发带末尾标记的 CLEAR；只有一侧也替换，不保留旧另一侧。

- 不实现凭空的“sequence 连号检测”；Nautilus 无可用序列号时使用其无序列约定（预期为 0，按 model 确认）。另存本地 `session_id/recv_seq`，不冒充交易所序号。
- 限档快照的未出现档位不能残留；`limit=100` 也只代表最多 100 档，不保证拿到全市场深度。
- 较旧 `item.time` 不覆盖新状态；相同时间且内容相同可抑制重复发布，原始录制仍保留；相同时间但内容不同按接收顺序处理并记录冲突计数，不凭时间相等丢掉所有后续消息。
- `item.time` 是价格事件时间，envelope `timestamp` 是独立服务器发送/批次时间，本地接收时间另存。不得用 `intervalEnds` 充当 funding 事件发生时间。
- 重连后旧盘口立即变为不可用；重订阅收到新 snapshot 后恢复。迟到的旧 session 回调不能更新新连接状态。REST 可取首次快照/诊断，但不能让较旧 REST 结果覆盖已经收到的 WS 快照。
- on-demand depth10 如实现，必须由**同一** book subscription/cache 投影，不额外开一个会互相取消订阅的连接；若 P1 不实现，registry 明确 `supports_depth10=False`。

失效通知必须到达应用，不能只修改 adapter 内的布尔值。使用当前 wheel 已有的 `subscribe_instrument_status`/`on_instrument_status`：本地断流发布 `InstrumentStatus(action=NONE, reason="adapter:disconnected", is_quoting=False, is_trading=None)`，新 snapshot 就绪后发布对应 `adapter:snapshot_ready`。它们明确是本地 feed 状态，不冒充交易所停牌。真实市场状态单独通过 `is_trading` 与 raw reason 表达。watcher 的 ONDO LegState 新增 `feed_ready`/`market_ready`，在断流回调立即清零旧 BBO 并使本地 book 不可用；`ready()` 和 tape gate 同时检查这两项，不能等旧报价 2 秒过期。非 ONDO 腿保持原默认行为。Task 2/3 必须用同一个进程的发布/订阅测试证明这条失效链，而不只各测一个 helper。

### 4.3 精度、时间与 funding

Nautilus `ts_event` 使用 RFC3339 纳秒解析结果；`ts_init` 在原始帧进入 adapter 时记录，批内复用。原始 tape 另存服务器 envelope timestamp 和本地 monotonic 时间。Python `datetime` 的微秒截断不能作为 Rust 纳秒解析实现。

funding 的 `rate` 是小时小数；本次两个市场为 `0.0000063`，`intervalEnds=2026-09-14T12:00:00Z`。不除 100，不再乘 8。`0.0000063 × 10000 = 0.063 bp/h`；`0.0001` 对应 1 bp/h。每小时结算不等于每条 forecast 已结算。

`FundingRateUpdate.rate` 原值保留，`next_funding_ns` 如 model 支持映射 `intervalEnds`；forecast/source timestamp 缺失时单独标记，不能将未来结算时间写入 `ts_event`。历史 `fundingRate` 与真实 funding payments 分别读取，估计值不产生钱包余额变更。

`opportunities.py` 显式登记 `FUNDING_SCALE['ONDO']=1e4`、`FUNDING_HOURS['ONDO']=1.0`。原 Lighter 仍是百分数，不顺带改变。

### 4.4 限速、连接与故障

REST 初始上限保守设为全 adapter/同环境共享 **1 req/s**；这是历史默认值，P0 复查后记录，不把各个 endpoint 各限 1 次当成总限制。账号请求以账户共享；DataClient/ExecClient 同进程至少共享公共预算。429 按 Retry-After，缺字段指数退避；401/403 不无限重试。

同预算中撤单、未知单查询、对账高于普通元数据/历史查询；预留请求槽而非绕过限速。读请求可有限重试；下单 timeout/5xx 属于结果未知，不允许自动重放 POST。

WS 文档为 25 req/s、burst 50、32 KB 消息限制、idle 180 秒；客户端请求限额按其约束，应用层 `{"op":"ping"}` 默认每 20 秒，不能只用协议层 ping。接收端保留有界大小且能够容纳实测 depth/funding 响应，不盲目将客户端发包上限用于截断服务器数据。

重连退避 1/2/4/8/16/30 秒加抖动；停止时取消 timers、任务和队列并关闭 socket/file，不因订阅错误不断重建整台 LiveNode。永久协议错误和目标市场不存在返回可诊断错误，保留 evidence。

## 5. 录制、重放和价格观察

P2 有两个产物：adapter 录制**公开原始帧**，应用录制各观察腿的**完整标准化 L2**。旧 2/5/10 bp 容量 CSV 不能重建 VWAP，也不能只录 Ondo 而把 Aster 顶档当作深度。

### 5.1 格式与资源限制

原始 JSONL 至少含 `schema_version=1`、run/session ID、`recv_seq`、`received_at_ns`、`received_mono_ns`、endpoint/environment、方向和原始 payload；不在通用 raw writer 中记录 login/私有 payload。REST 元数据快照保存请求起止时间、HTTP status、body 和 hash，header 只用白名单。

标准 tape 为 JSONL，字段包含：`schema_version=1`、`run_id`、`arrival_seq`、`symbol`、`venue`、`instrument_id`、`event_kind`、`ts_event_ns`、`ts_init_ns`、`recorded_mono_ns`、`bids/asks` 的十进制字符串数组、`coverage_limit`、`source`、`valid`、`invalid_reason`。另有 instrument metadata、quote、funding、status 和 run-end 记录。每次完整应用一个 deltas batch 后记录一本书，不能在 CLEAR 与 ADD 中间记录半本书。

writer 用有界队列（默认 4096 条）、每秒 flush、128 MiB 文件轮转；不做无限内存缓存。队列满必须累计 drop 并写出 gap 标记，使整段可识别，不能悄悄跳过仍报告 complete。非阻塞采集可以继续，但本次录制验收失败；不能跨 gap 回测。磁盘写入失败中止 recorder，暴露失败给 watcher。

时间顺序是接收顺序：重放按 `arrival_seq`，不能按交易所时间排序后制造当时尚不可见的信息。文件末尾截断只允许忽略最后一条不完整记录并报告 `truncated_tail`；中间坏行直接失败。重启追加使用新 session，不重复表头，不掩盖上次未写 run-end。

### 5.2 同数量 VWAP 与口径

新增 `--record-l2` 为默认关闭的显式参数，适用于当前 run 的全部观察腿；当 run 含 ONDO 时，同时将 `OndoDataClientConfig.raw_md_path` 指到该 runDir 的 `raw_ondo` 目录。`NodePlan` 带此选项，`build_node` 在 ONDO group 上传入；不为此改造所有 venue 的 config 系统。`ondo_depth.py` 的初始默认只比较 NVDA/TSLA 的 ONDO/ASTER，两个方向、名义档 `$100/$500/$1000`。`vwap_for_quantity(levels, quantity)` 返回指定 base quantity 的成交均价；不足时返回缺量和 `insufficient_depth`，不外推最后一档。

目标名义额只用来选同一个 base quantity：在当时可见买方盘口价格上算目标 Q，再按两所步长转换到共同可表示数量。不同 multiplier 先统一成 underlying units；步长共同量用整数缩放后求最小公倍数，不能简单 `max(step_a, step_b)`。乘数/结算/标的等价性未验证时标 `mapping_unverified`，仍可输出名义比较，不能给出可执行结论。

默认质量阈值：两腿 receive age <= 2000 ms；两腿 event time 差 <= 500 ms；未来时间偏移超过 1000 ms、disconnect、空/交叉盘口、metadata unknown、录制 gap 均不得通过。阈值是研究参数，必须写入报告。每次从 tape 到达事件的本地时刻检查，不用两腿 `ts_init` 差代替“相对当前时刻的新鲜度”。本地 monotonic 仅在同一 run/session 内可比较。

quote、book 的时间各自保存，book 未更新时不能用新 quote 时间给旧深度续命。接收新鲜也不证明交易所事件新鲜；报告同时给出 event age 和时钟偏差未知标记，不将本地 clock offset 混成网络延迟结论。

输出字段分别命名 `gross_entry_bps`、`entry_fees_bps`、`entry_after_fees_bps`、`exit_fee_assumption_bps`、`reserve_bps`、`funding_estimate_bps`、`quality_ok`、`mapping_verified`、`reject_reason`、`executable=False`。研究观察始终不构成订单承诺。不能把现有 `net_bps`（只扣建仓费与 reserve）更名成完整利润。

费率来源优先顺序：有效的账户实际费率 > 同时间有效公开 metadata > 带日期的文档假设。`ONDO_TAKER_FEE_BPS=2.5` 只能用于 dry-run/明确标为假设的旧 CSV；正式观察启动后读取 instrument metadata 的当前 maker/taker 和来源，通过 `dataclasses.replace(leg.spec, taker_fee_bps=...)` 更新该 ONDO 腿。缺失时停止该腿成本合格判定，不能静默沿用 2.5。新报告使用 run 内 fee snapshot，不能调用今天的静态 `venue_fees()` 重算历史文件。

若按历史 2.5/0.9 bp 计算，双腿建仓 3.4 bp、四笔相同名义额开平手续费 6.8 bp 仅是**假设示例**。积分、返佣未知值不计入利润。完整 round trip 必须用之后实际可见的反向 book 扫量：`Q*(sell_entry-buy_entry+sell_exit-buy_exit)`，减四笔按各自成交名义额的手续费，再加减真实结算区间 funding，另列失败腿 reserve、币种基差和资金占用。没有退出数据输出 `unclosed`，不要假定价差必然回零。

为了控制范围，P2 必须交付 entry VWAP 和成本明细；完整 round-trip 报告在没有真实退出/funding 数据时可以保持未验证，不新增历史行情采购或复杂盈利策略。

## 6. 执行协议与恢复

### 6.1 鉴权先验证，不能自动试多种协议

官方 [API-key 专页](https://docs.ondoperps.xyz/api-reference/api_key_authentication.md) 规定 REST headers `ONDO-KEY-ID`、`ONDO-TIMESTAMP`、`ONDO-SIGN`：

```text
message = timestamp_ms + UPPERCASE_METHOD + exact_path_and_query + exact_body_bytes
signature = hex(HMAC_SHA256(full_api_secret_with_prefix, message))
```

保留 key 的 `ondoKeyId_` 和 secret 的 `ondoApiSecret_` 前缀。timestamp 是毫秒字符串，服务器容差 ±30 秒。URL query 编码与排序、空 GET body、DELETE body、字符串里的小数和 UTF-8 内容必须做到签名与实际发送字节完全相同；序列化一次后复用。

REST spec security 的 `X-API-KEY-ID` 与该专页冲突。WS connect 写 `"ondo_perps_ws_login" + time`，login 页写 `time + "ondo_perps_ws_login"`。P0/P3 必须保存冲突表；离线可先按专页写参考测试，**真实 sandbox 验证成功之前不能称认证已完成**。若验证失败，记录 `signature_mismatch`/时间错误等区别，不在生产自动轮换 header/拼接顺序试登录。凭据不足不读取用户其他软件的账户数据，继续离线交付并标 sandbox 未验收。

时间偏差超过安全容差时禁止签名发送并报告；HTTP Date 精度不足以证明毫秒级同步。key/IP/scope 不足分别报错，不触发创建新 key 或更换网络来规避限制。

### 6.2 订单命令与映射

创建 `POST /v1/perps/orders`；查询 `GET /v1/perps/orders/{orderID}`；撤单同路径 DELETE；按市场撤单 `DELETE /v1/perps/orders?market=...`。按 client id 查询/撤单使用 `client:{clientOrderId}`，准确 URL-encode 后签名。

基本请求示例（假设 sandbox metadata 接受此步长，价格数量不能直接用于真实下单）：

```json
{"market":"NVDA-USD.P","side":"buy","type":"limit","size":"0.01","price":"100.00","timeInForce":"GTC","postOnly":true,"reduceOnly":false,"clientOrderId":"ondo_probe_example_1"}
```

本期本地拒绝 FOK，即使 WS Order enum 出现 FOK，REST 创建 schema 只列 GTC/IOC。Market 发送 base `size`，不发送 price/timeInForce；postOnly 仅对支持组合可用。禁止把失败 postOnly 自动改成 taker。

Nautilus ClientOrderId 经允许字符集（字母数字下划线短横线，最大 64）验证后原样传输；超长/非法 ID 明确拒绝，不默默截断。submit_order_list 可使用最多 20 条的原生 batch，每项独立报告结果；HTTP 2xx 不表示全部成功。cancel list 对每项验证终态。

无原子 amend endpoint，因此 ModifyOrder 返回 ModifyRejected/不支持，不能模拟 cancel+new 后声称原子修改。非本期订单类型返回已命名的 unsupported 错误；不能忽略 timeInForce、reduceOnly 等字段。

### 6.3 订单状态、去重与未知结果

| 真实语义 | Nautilus 行为 |
|---|---|
| 发起请求 | 提交事件不代表已接受 |
| `pending` | 保持待定，不能生成成交 |
| `open` 且 filledSize=0 | 接受/工作中 |
| open 且 filledSize>0 | 部分成交，由唯一 fills 驱动增量成交 |
| `fullyfilled` | 验证 fills 总量匹配，补齐后终态 |
| `canceled` 且已部分成交 | 先保存成交，再结束剩余量 |
| HTTP 400 post_only_has_match | OrderRejected，保留具体原因 |
| 未知状态/untriggered 外部订单 | 保留 raw、识别未支持能力并阻止账户被误判为 clean |

REST 与 WS 先后不定；fills 可能先于订单 ACK。使用 `(account_id, fill.id)` 去重，订单以 venue orderId/clientOrderId 双向映射。`lastFillSize` 是信息字段，不能和 fills stream 分别产生两次成交。`fee`/`feeRebate` 和 maker/taker 标记按每笔成交保留；订单累计 fee 不重复记账。

timeout/连接断在 POST 后：进入 `SubmissionUnknown`（adapter 内部状态），暂停该订单关联的新风险，按原 clientOrderId 查询并补抓 fills。404 一次不证明未提交，给有界重试窗口；30 秒仍无法判定保持 unknown、停止 probe、输出人工对账所需 id，不换 ID 重发。

cancel timeout、`order_already_filled`、`order_already_cancelled` 触发查询确认，不能把“撤单 API 被调用”当作已撤。完成标准是 venue 确认终态以及 fills/position 一致。

### 6.4 账户与恢复

采用 NETTING/MARGIN 语义，但由真实 sandbox positions 验证。`direction` + `netQuantity` 统一为 signed quantity；文档例子 short 的数量仍为正，不能只看数量符号。若实际出现带符号值先明确映射，禁止双重取负。neutral/zero 需要明确清仓更新。

余额 `walletBalance`、`marginBalance`、`usedMargin`、`availableMargin`、`withdrawableMargin` 分别保留，不能混成一个余额。Nautilus AccountState total/free/locked 选定一致的 margin 语义并验 `total=free+locked`；原生语义无法表达或 negative equity 时保留 raw+停止新风险，不能钳成 0 后报 healthy。本期只接受 USDC-only sandbox 账户，多抵押/借贷数据出现时明确未支持。

启动和 reconnect：先停止新单，连接私有流并缓冲回报，再分页查询 orders/fills/positions/balance（历史 cursor 不进无限循环），将 REST 与缓冲流按 id 去重后对账。没有可证明的一致序列边界时做第二轮收敛确认；不能声称原子快照。重复 cursor、缺页、fills 数量不符、持仓差异均保持恢复中，不恢复下单。

订单与账户长期状态受控：工作单、unknown 单及未对账 fills 保留；终态去重记录持久化并配分页 watermark，不能使用随意短 TTL 后把历史成交再次计入。已有其他策略/人工订单不能被接管或自动撤掉；probe 要求专用 sandbox 账户及启动无订单/无仓位。

DMS 官方频道 `cancelAllOrdersAfterPerps` 携 `timeout_seconds`；它是带账户级效果的操作，不是普通只读频道。默认 30 秒，具体哪个消息续期以及重连/主动退出行为必须 sandbox 实测。只能在本 probe 专用 sandbox 账户显式启用，确认 armed 后才能挂单；DMS 失败停止挂新单。停止时先撤本 probe 订单并确认，再关闭连接；DMS 只能撤挂单，不能替代已有持仓的平仓与对账。

## 7. 实施任务

每个 Task 均先增加相应失败测试，再实现最小代码，运行所列验证。测试行为以本节具体输入/输出和第 4–6 节为准，不以复制实现为目标。下面代码块是接口契约/测试样例，真实 imports 和事件构造沿用当前 fork；没有要求执行者复制一套未经编译的完整 adapter。

### Task 0 / P0：冻结协议、核实环境

**Files:** 读取第 3 节对应路径；新增 fork `crates/adapters/ondo/test_data/manifest.json` 和 `README.md` 中的协议表。公开材料可从规划证据复制，保留来源时间。私有 fixtures 只用合成数据直到取得合法 sandbox 响应。

- [ ] 记录两个仓库 status/HEAD、Python/wheel、Rust/uv 工具路径；保存现有 wheel 副本和 SHA256，不能因为版本同为 rc4 就认为可随意覆盖。
- [ ] 运行应用现有 83 项基线；记录结果。此阶段不构建新 wheel。
- [ ] 从官方 llms 索引更新 REST/WS spec，检查首批 raw markets/tick/lot/status/fees 字段；本机 403 时标网络未验证，不绕过限制，不从 WS 价格推导元数据。
- [ ] 从已保存真实 WS 提取最小 BBO/depth/trade/funding fixtures，标 `observed`；缺失类型标 `synthetic` 或 `official-example`。
- [ ] 建立具体冲突表：REST headers、WS HMAC 顺序、WS FOK vs REST GTC/IOC、REST/WS fill.direction 拼写（camelCase 与带空格两种），与缺失状态字段。已知形式显式兼容，不把未知值吞掉。

```powershell
Set-Location E:\Nautilus-Perps
git status --short --branch
git rev-parse HEAD
.\.venv\Scripts\python.exe -c "import sys, nautilus_trader; print(sys.executable); print(nautilus_trader.__version__)"
.\.venv\Scripts\python.exe -m pytest tests/test_spread_watch.py tests/test_opportunities.py tests/test_ref_feed.py -q -p no:cacheprovider
```

**验收：** evidence 能区分 observed/schema/synthetic。缺 metadata 或 sandbox key 只阻塞相关在线验收，不阻塞精度、解析和本地 mock 测试。

### Task 1 / P1：crate、精度与元数据

**Files:** fork `Cargo.toml`、新 crate `Cargo.toml`、`src/common/*`、`src/config.rs`、`src/http/{models,query,error}.rs`、`src/lib.rs`；测试 `tests/http_contract.rs`。

**Interfaces:** `market_to_instrument_id(market: &str) -> Result<InstrumentId>`；`parse_timestamp(value: &str) -> Result<UnixNanos>`；`parse_instruments(payload: &str, load_ids: &[InstrumentId], ts_init: UnixNanos) -> Result<Vec<InstrumentAny>>`。真实 DTO schema 集中在 models 中，共用 parse；不在 WS 和 REST 两边各推导精度。

- [ ] 测 canonical `NVDA-USD.P -> NVDA-USD-PERP.ONDO`，unknown market/type 不偷偷改名。
- [ ] 测 `baseIncrement=0.001`、`quoteIncrement=0.05`、missing increment、零/负数、未知状态、disabled，以及有一个 requested ID 缺失。
- [ ] 测纳秒日期能保持 `2026-09-14T11:09:59.570112122Z` 的 122 纳秒尾数；不得只保留微秒。
- [ ] 实现 provider metadata/info 以及状态/fee 来源；边界字段缺失时明确 fail closed。

```rust
// test fixtures must explicitly identify synthetic metadata
assert_eq!(market_to_instrument_id("NVDA-USD.P")?.to_string(), "NVDA-USD-PERP.ONDO");
assert_eq!(parse_timestamp("1970-01-01T00:00:01.123456789Z")?.as_u64(), 1_123_456_789);
```

Run（fork 根目录）：`cargo test -p nautilus-ondo --test http_contract`。先因模块/行为缺失失败，完成后通过。

### Task 2 / P1：HTTP、WS 和 Nautilus DataClient

**Files:** fork `src/http/client.rs`、`src/websocket/*`、`src/data.rs`、`src/factories.rs`；`tests/market_data.rs`。

**Interfaces:** `OndoDataClient` 实现 fork 当前 `DataClient`；`OndoDataClientFactory` 实现当前 `DataClientFactory`；`parse_book_snapshot` 产生一个原子 `OrderBookDeltas` 批次。参数使用 Task 1 的 instrument 与 `ts_init`，禁止第二套模型。

- [ ] 先在 mock WS 测数组多市场路由、subscribed 与 update 分开、无 key 可连接、unsubscribe 后不重新订阅。
- [ ] 测快照 A bids=[100,99]，下一快照只有 [98]，最终 book 只有 98；空快照清空两侧；old-session callback/旧时间不得恢复旧盘口。
- [ ] 测断线后先 invalid，重连后新 snapshot 才 ready；429/403/超时退出行为和同进程共享限速。
- [ ] 实现 quote/trade/funding/mark 的映射，market `item.time`、funding envelope timestamp 与 intervalEnds 分清，资金费原值保持。
- [ ] 测一次 WS 的多个订阅不会重复连接；quote/depth10/deltas 共享 book channel 的引用计数正确。

```text
snapshot A: bid 100 x 1, 99 x 2; ask 101 x 1
snapshot B: bid 98 x 3; ask 102 x 4
expected: best_bid=98, best_ask=102; 100/99/101 are absent
disconnect -> book.valid=false; no quote/depth signal may use it
```

Run：`cargo test -p nautilus-ondo --test market_data` 和 `cargo test -p nautilus-ondo --test http_contract`。不启动生产长时连接。

### Task 3 / P1：Python 导出、构建与 watcher 接入

**Files:** fork `src/python/*`、PyO3 注册和 Python 包；应用 `spread_watch.py`、`opportunities.py`、`ondo_preflight.py`、相应三个测试文件。

**Interfaces:** 固定第 4.1 节 public API；新增 `_ondo_client(instrument_ids)` 返回 factory/config，调用 `OndoDataClientConfig(environment=OndoEnvironment.PRODUCTION, load_ids=list(instrument_ids))`。保持 `build_plan`/`build_client_groups` 接口。

- [ ] 按现有 factory 模式导出；stub 经 generator 生成，构建候选 wheel 后先用独立临时 venv 验证所有 adapter imports。
- [ ] 注册 `ONDO`，只添加 NVDA/TSLA 两条映射，`ALL_VENUES` 增加，`DEFAULT_VENUES`/`DEFAULT_PAIR` 不变。
- [ ] 基于 runtime metadata 更新 ONDO fee/状态，缺 fee 不静默使用历史默认。
- [ ] 实现公开 `ondo_preflight.py --symbols NVDA,TSLA --out <目录>`，从第 4.1 节 HTTP 接口保存 status/markets/contracts、标准化 instrument 表、fee/status 缺失项、获取时间与 hash；任何请求失败或目标缺字段则非零退出。不能把 403 变成空市场的成功结果。它不读取 `.env`。
- [ ] 测 `--dry-run` 不加载 `.env`/不联网、不实例化 ExecClient；ONDO 不存在时按 `_adapter_missing` 输出明确构建指引。
- [ ] 测同一 run 的 HL/ENTROPY 仍共用一个客户端；ONDO/ASTER 是两个；缺一个 requested instrument 或有腿未收到 BBO 均失败。
- [ ] 订阅 InstrumentStatus 并实现 ONDO 本地断流/真实市场状态的分开处理；断流的同一事件循环内旧 BBO/深度立即失效，恢复不能仅凭 socket connected。
- [ ] 登记 funding scale，并保留所有旧 CSV header 顺序。

```python
def test_ondo_funding_is_fraction():
    assert hourly_bps(0.0001, "ONDO", "NVDA") == 1.0
    assert abs(hourly_bps(0.0000063, "ONDO", "NVDA") - 0.063) < 1e-12
```

Run：应用 Task 0 基线；新 wheel 的 import 检查；然后仅在元数据可读取时运行第 8 节 2 分钟公开 smoke，**P1 此时去掉 `--record-l2`，也不运行 P2 分析命令**。P1 报告不可只写“raw WS 能读，所以 adapter 完成”。

### Task 4 / P2：公开原始帧与多腿 tape

**Files:** fork `src/recording.rs` 及公开 transport hook；应用 `market_tape.py`、watcher 的 `--record-l2`/handler 接入；`tests/test_market_tape.py`。

**Interfaces:** Python `TapeWriter(path: Path, *, run_id: str)` 提供 `write_event(event: dict) -> None`、`close() -> None`；`read_tape(paths: list[Path]) -> Iterator[dict]`。校验 schema_version 与 arrival_seq，不使用不可信 pickle。

- [ ] 测两条腿各自完整 L2，所有 Decimal 作为字符串存储；deltas 的中间 CLEAR 不被记录为正常深度。
- [ ] 测 writer full/disk error 写入质量失败；轮转连续编号，close flush，进程中止没有 run-end 时标 incomplete。
- [ ] 测尾行截断与中间坏行区分；重复 session ID、逆向 arrival_seq 被拒绝；重放保留原到达顺序。
- [ ] 实现并确认 raw writer 的白名单只含公开流，login/header/private body 不进入文件。

```python
def make_book_event(*, bids):
    return {
        "schema_version": 1, "run_id": "fixture", "arrival_seq": 1,
        "symbol": "NVDA", "venue": "ONDO", "instrument_id": "NVDA-USD-PERP.ONDO",
        "event_kind": "book", "ts_event_ns": 1_000_000_000,
        "ts_init_ns": 1_000_000_010, "recorded_mono_ns": 10,
        "bids": bids, "asks": [["100.10", "0.001"]],
        "coverage_limit": 100, "source": "snapshot", "valid": True,
        "invalid_reason": None,
    }

def test_tape_preserves_decimal_text(tmp_path):
    path = tmp_path / "l2.jsonl"
    with TapeWriter(path, run_id="fixture") as writer:
        writer.write_event(make_book_event(bids=[["100.05", "0.001"]]))
    rows = [r for r in read_tape([path]) if r["event_kind"] == "book"]
    assert rows[0]["bids"] == [["100.05", "0.001"]]
```

此处 `make_book_event` 是 `tests/test_market_tape.py` 的纯测试 helper；`TapeWriter` 同时实现 context manager 以便确保 close。生产 writer 保留合法 arrival_seq 并维护下一序号，不能覆盖传入事件后制造顺序一致的假象。

Run：`python -m pytest tests/test_market_tape.py tests/test_spread_watch.py -q -p no:cacheprovider`（使用应用 venv 完整路径）。

### Task 5 / P2：确定性重放与同数量 VWAP

**Files:** 应用 `src/analysis/ondo_depth.py`、`tests/test_ondo_depth.py`。

**Interfaces:** `vwap_for_quantity(levels: Sequence[tuple[Decimal, Decimal]], quantity: Decimal) -> VwapResult`；`VwapResult` 定义 `requested_qty`、`filled_qty`、`vwap: Decimal | None`、`insufficient_depth: bool`。`replay_events(events)` 只按 arrival_seq 更新 book；quality gate 接收当前 record 时间及两腿各自 book 时间。

- [ ] 测 1 @100 + 1 @102，Q=1.5 的 VWAP 精确为 `151/1.5`；Q=3 则不足，不输出完成均价。
- [ ] 测共同步长 0.002 与 0.003 的共同单位是 0.006；不按两边各 100 美元独立扫量。
- [ ] 测 fresh quote 不刷新 stale book、event skew、未来时间、gap、断线、unknown multiplier/status、跨 session monotonic 不可比。
- [ ] 测同 tape 两次报告除生成时间外一致；没有费用/退出/funding 的样本保留 unknown/unclosed。
- [ ] 输出 JSON/CSV 与简短 Markdown，分市场和方向给通过/拒绝计数与原因分布；零机会如实保留。

```python
def test_vwap_requires_full_quantity():
    levels = [(Decimal("100"), Decimal("1")), (Decimal("102"), Decimal("1"))]
    result = vwap_for_quantity(levels, Decimal("1.5"))
    assert result.vwap == Decimal("151") / Decimal("1.5")
    assert not result.insufficient_depth
    short = vwap_for_quantity(levels, Decimal("3"))
    assert short.insufficient_depth and short.vwap is None
    assert short.filled_qty == Decimal("2")
```

Run：`python -m pytest tests/test_ondo_depth.py tests/test_market_tape.py tests/test_opportunities.py -q -p no:cacheprovider`。不为此补建历史行情平台。

### Task 6 / P3：签名与 sandbox 私有读取

**Files:** fork `src/signing.rs`、`common/credential.rs`、私有 HTTP/WS models；`tests/signing.rs`、`tests/http_contract.rs`。

**Interfaces:** `sign_rest(secret, timestamp_ms, method, path_query, body_bytes) -> signature_hex`；`sign_ws` 仅实现**最终 sandbox 确认的一种顺序**。secret 类型不实现明文 Debug/Serialize，退出时清理。HTTP `get_account/get_orders/get_fills/get_positions/get_balance` 接口分别保留 cursor 和 raw 状态。

- [ ] 用固定假密钥和 timestamp 生成 Python 标准库参考值，Rust 精确对比。测试改 query/body 一个字节会改变签名；不对 JSON 二次序列化。
- [ ] 测过期 timestamp、错误 scope、错误 key、401、403 分别返回明确错误；不会自动换账号或 fallback 到 production。
- [ ] 显式环境校验在读取 key/建立网络前完成；测试 custom production URL 不能绕过 sandbox gate。
- [ ] sandbox credentials 已提供时，执行只读私有查询与 WS login，锁定 header 和 WS 顺序；否则产出待执行命令和“未验收”记录，继续离线交易测试。

```python
# Independent reference fixture generator; fake key only.
import hashlib, hmac
secret = b"ondoApiSecret_UNIT_TEST_ONLY"
message = b"1789384200000GET/v1/perps/orders?market=NVDA-USD.P&limit=2"
expected = hmac.new(secret, message, hashlib.sha256).hexdigest()
```

Run：`cargo test -p nautilus-ondo --test signing`。禁止将真实凭据或签名复制为测试 expected。

### Task 7 / P3：订单与私有回报

**Files:** fork `src/execution.rs`、private models/parse、`src/python` execution config/factory；`tests/execution.rs`。

**Interfaces:** `OndoExecutionClient` 实现 fork 当前 `ExecutionClient` trait；工厂返回 native execution client。`apply_order`/`apply_fill` 使用 Task 6 的 DTO、双 id 映射与 fill 去重账，不暴露应用自制回报类型代替 Nautilus reports。

- [ ] 测 Limit/Market/GTC/IOC/postOnly/reduceOnly 精确序列化，非法组合本地拒绝。
- [ ] 测 fill-before-ACK、ACK 重复、REST/WS 同 fill、partial 后 cancel、两笔 fill 共享 orderId 的累加。
- [ ] 测 `lastFillSize` 不独立重复记成交，order 累计 fee 不与逐笔 fee 双计。
- [ ] 测 batch 2xx 中混合成功/失败，逐项状态正确；FOK/改单不被错误支持。
- [ ] 实现 status/report 查询，构造正确 InstrumentId、OrderId、Commission currency；未知状态保留且停止风险恢复。

```text
input: fill F1(qty=0.2), ACK O1, duplicate F1 via REST, fill F2(qty=0.3), cancel O1
expected: total_filled=0.5; exactly 2 fills; remaining canceled; fee charged twice, once per fill
```

Run：`cargo test -p nautilus-ondo --test execution`；本地 mock server 允许故障注入，默认测试不得访问交易所。

### Task 8 / P3：未知请求、恢复、账户与 DMS

**Files:** fork `src/reconciliation.rs`、execution/WS reconnect hooks；`tests/reconciliation.rs`。

**Interfaces:** `ReconciliationState` 明确 `Disconnected/Recovering/Ready/Uncertain`；`can_submit_new_orders()` 只对 Ready 且 metadata/DMS 有效返回 true；撤单和查询在非 Ready 状态仍可走优先级队列。

- [ ] 测 POST 已接受但 response 丢失，按同 client id 找回；窗口内 404 后恢复；30 秒未决无第二次创建请求。
- [ ] 测 cancel 请求丢响应、填单与撤单竞态、订单终态与 position 不一致。
- [ ] 测重复 cursor、WS 缓冲重放、分页漏 fills、short 正数量、neutral 清零、多资产抵押和负权益。
- [ ] 测 restart 后去重账仍有效；保留运行外订单，不能自动撤整账户。
- [ ] sandbox 验证 DMS 的真正续期消息与 30 秒触发行为；断开后确认挂单取消且仓位未被伪报关闭。该真实测试缺失时 DMS 标未验收。

```text
POST accepted, response lost -> GET client:original_id -> order found -> reconcile fills
expected: one create only; same client id; no new risk until Ready
disconnect -> Recovering -> REST+buffered WS agree twice -> Ready
unresolved position discrepancy -> Uncertain, never Ready
```

Run：`cargo test -p nautilus-ondo --test reconciliation`，再运行 crate 全套 `cargo test -p nautilus-ondo`。

### Task 9 / P4：应用 sandbox probe 与交付

**Files:** 应用 `src/ondo_probe.py`、`tests/test_ondo_probe.py`、`.env.example`、`docs/ondo.md`，最终 fork wheel 与 acceptance 报告。

**Interfaces:** `main(argv: list[str] | None = None) -> int`；CLI `--mode dry-run|account|orders|dms`，默认 dry-run；`--environment sandbox` 为唯一可写环境；`--symbol NVDA`、`--notional-usd 10`（单笔和最大未对冲敞口硬上限均 25 USD）、`--max-orders 2`（同时工作订单数）、`--minutes 2`。每 run 最多 6 次创建请求，清理用 reduce-only 也计入；额度不足则保留未清理状态并报告，不能突破。名义上限不能因加杠杆、未决单或缺 mark 被放大。这些 sandbox 上限不修改现有主网 limits。

- [ ] 测无参数不联网不读 key；任何 production mode 在网络前失败；缺 key 只提示环境变量名。
- [ ] `.env.example` 只新增 `ONDO_SANDBOX_API_KEY=`、`ONDO_SANDBOX_API_SECRET=`、`ONDO_SANDBOX_ACCOUNT_ID=`。由用户在执行环境配置实际值。
- [ ] account 模式只做私有初始化/对账；orders 模式要求专用空 sandbox 账户，动态读取 metadata/最小量，最大名义额不够最小单时返回 No Trade 而不抬高上限。
- [ ] orders 测 passive post-only 挂单/撤单和小额 IOC/reduce-only 生命周期；仅有 ACK 无 fill 时按实际结果报告，不制造对敲或虚假成交。没有持仓时不能声称 reduce-only 已实测平仓。
- [ ] 有测试成交时按 sandbox 状态平掉本 probe 持仓，等待订单全终态、position=0、账单一致再结束；出错保留证据，不能打印 clean shutdown。
- [ ] dms 模式单独启用账户级 DMS 并核对，不和其他账户活动混跑；只能在空的专用 sandbox 账户执行。
- [ ] 执行第 8–9 节交付检查，报告 code/offline/production-public/sandbox-private 各自状态，未通过项不能打勾。

Run：`python -m pytest tests/test_ondo_probe.py -q -p no:cacheprovider`，然后应用完整离线测试和候选 wheel 回归。

## 8. 验收命令与证据

以下 Ondo 命令为**实施后应提供的接口**，当前尚不能运行。每次 evidence 使用独立 UTC 目录。

```powershell
Set-Location E:\Nautilus-Perps
# 完全离线：映射、客户端数、默认值
.\.venv\Scripts\python.exe src/spread_watch.py --symbols NVDA,TSLA --venues ONDO,ASTER --dry-run
.\.venv\Scripts\python.exe src/ondo_probe.py --mode dry-run --environment sandbox

# 公开 smoke 前先完成 metadata/status/fee preflight；失败时不拿旧 metadata 宣布通过
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$runDir = Join-Path 'reports\ondo-acceptance' $stamp
New-Item -ItemType Directory -Path $runDir -Force | Out-Null
.\.venv\Scripts\python.exe src/ondo_preflight.py --symbols NVDA,TSLA --out (Join-Path $runDir 'preflight')
if ($LASTEXITCODE -ne 0) { throw 'Ondo public preflight failed' }
.\.venv\Scripts\python.exe src/spread_watch.py --symbols NVDA,TSLA --venues ONDO,ASTER `
    --minutes 2 --max-restarts 0 --record-l2 --out $runDir

# recorder 应将 tape fragments 放在本 runDir/l2 下；分析器接受目录并按 manifest 顺序读取
.\.venv\Scripts\python.exe src/analysis/ondo_depth.py --dir $runDir --symbols NVDA,TSLA `
    --notionals 100,500,1000 --max-age-ms 2000 --max-skew-ms 500 --out (Join-Path $runDir 'depth-analysis')

# sandbox key 已在本地配置之后逐项执行；不自动充值、不主网写入
.\.venv\Scripts\python.exe src/ondo_probe.py --mode account --environment sandbox --minutes 2
.\.venv\Scripts\python.exe src/ondo_probe.py --mode orders --environment sandbox --symbol NVDA --notional-usd 10 --max-orders 2 --minutes 2
.\.venv\Scripts\python.exe src/ondo_probe.py --mode dms --environment sandbox --symbol NVDA --notional-usd 10 --minutes 2

# 离线总回归；不允许测试自动读真实 key/联网
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
```

公开 smoke 的逐腿表必须有 NVDA/ONDO、NVDA/ASTER、TSLA/ONDO、TSLA/ASTER：instrument loaded、BBO count、book count、funding_seen、trades count、断线/重连、失效时间。P2 另报 tape gap/dropped count。任一腿没报价或有效双侧 book 就不是 P1 通过；P2 还要求各腿 tape 有有效双侧深度。funding 未到为 missing，零成交/零机会允许。

短测不替代长时验收；24–72 小时观察另行运行并覆盖开盘、休市、至少一次资金费结算和一次受控断线。若当前 403 或 sandbox 缺条件，报告“离线实现完成，在线验收未完成”，禁止写“全链路通过”。

P3/P4 结果必须覆盖：签名协议确认、基础订单行为、部分成交去重、unknown submission、cancel reconciliation、重连后订单/仓位/余额、DMS 语义、No Trade、缺凭据、production 写入阻断。sandbox 不足以证明生产资格、容量、提现或收益。

## 9. wheel 构建、安装与回滚

实施 P1/P4 时按 fork `BUILD_WINDOWS.md` 的**环境与当前命令**执行，不照抄其最初 clone/切分支/升级工具步骤。记录源码 SHA、dirty diff 摘要、Rust/Python/uv 版本、完整 build command、产物 hash。

1. 在 fork 根目录运行 `cargo test -p nautilus-ondo`；在 Python/feature 接入后运行 `cargo check -p nautilus-ondo --features python` 和 `cargo check -p nautilus-pyo3`。按 fork 规则运行适用格式检查、pre-commit 与测试；Windows 缺 make 时用当前 Makefile 的等价命令并记录，不能略过后声称通过。
2. 在 `E:\nautilus_trader\python` 运行现有 stub generator 与 wheel build；使用其 `.venv`/uv，清理构建子进程的 conda 干扰，不全局改用户 PATH/环境。产物是 wheel，`maturin develop` 装进 fork venv 不是应用交付。
3. 使用独立候选目录保存 wheel，不能直接覆盖 `pyproject.toml` 当前引用的旧 wheel；记录 SHA256。先用临时测试环境导入 `ondo/aster/hyperliquid/lighter` 并检查新 factory/config、Python stub 与 runtime 一致。
4. 应用现有已安装 wheel 的来源和 hash 可追溯后才替换；如果运行中的观察/交易进程占用 `.pyd`，不强杀，先完成离线包验证并报告安装阻塞。
5. 在获准交付安装的实施窗口，执行精确命令 `uv pip install --python E:\Nautilus-Perps\.venv\Scripts\python.exe --reinstall <实际候选 wheel 绝对路径>`。路径必须从本次构建产物确定，不按 rc4 文件名猜版本。
6. 运行 import、83 项旧基线、新增测试及完整应用测试；失败时按相同命令重新安装保存的旧 wheel，核对版本/hash/旧用例。不得降回不包含 Aster 的 PyPI wheel。
7. 将应用 `pyproject.toml` 的本地 wheel URL 更新到经验证且长期保留的实际产物，只在交付时进行；旧 wheel 仍保留以便回滚。Git 不提交 wheel 二进制或秘密。

本计划本身不授权提交/合并/部署。交付文档需要记录两个仓库各自修改文件与 HEAD；不能只提交应用的小改而漏掉真正的 Rust adapter。

## 10. 直接交给 DeepSeek 的指令

```text
请按 E:\Nautilus-Perps\docs\superpowers\plans\2026-09-14-ondo-perps-full-integration.md
实施 Ondo Perps 完整接入，按 P0–P4 分阶段交付。先读取两个仓库的规则和本计划证据目录。

技术路线已经确定：E:\nautilus_trader 新增 Rust 原生 nautilus-ondo，通过 PyO3 暴露到
nautilus_trader.adapters.ondo；E:\Nautilus-Perps 接入公开观察、录制/离线分析和独立 sandbox probe。
不要改造成 Nautilus 1.x Python adapter，不要复制 Binance/Aster 后只换地址。

先完成 P0，然后 P1、P2、P3、P4；每阶段汇报真实测试/构建/公开连接/sandbox 验收结果。
可连续完成已授权的离线代码任务，但某阶段有未通过验收项必须保留，不能把下一阶段完成当成补验收。
文档提到的 24–72 小时观察不默认启动；主网真实写入不开启，不充值、不提现、不创建 key。

关键点：depthBooksPerps 是 snapshot，每帧清掉旧档；没有 exchange sequence 时不要造序列；
Ondo funding API 是小时小数；REST header 和 WS HMAC 顺序需 sandbox 验证；POST timeout
先按同一个 clientOrderId 查询，禁止换 ID 重试；DMS 是账户级撤单操作且不平持仓。

保留当前两个仓库的分支和未提交文件。默认不 commit/merge/push；不要安装新版本 upstream。
如果缺 sandbox 凭据/生产 metadata 无法读取，完成可独立验证的离线代码和测试，说明哪些在线项目未验收，
不要伪造响应或用文档例子冒充真实成交。完成时给出文件清单、命令、结果、wheel hash、回滚步骤和已知限制。
```

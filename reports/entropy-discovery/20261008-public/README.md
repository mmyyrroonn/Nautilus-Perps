# Entropy `io:` 当前公共协议核查（2026-10-08）

本次只访问官方公开文档与无认证市场数据端点；没有读取 `.env`、账户查询、签名、资金操作或订单。采集窗口为 2026-10-08 09:13:54–09:17:43 UTC。此目录给出元数据发现和短快照证据，不表示实盘权限、完整对冲等价性或盈利。

## 来源与采集边界

`provenance.json`、`provenance-retry.json`、`provenance-docs.json`、`provenance-l2.json` 保留每次请求的 URL、method/body、UTC 起止时间、状态码、原始响应字节数与 SHA256。原始 JSON/Markdown 文件保存 HTTP body 字节，未归档响应头。部分首请求发生 TLS EOF，错误同样保留；随后顺序最多 3 次有界尝试成功，没有改变代理、TLS 配置或凭据。初始 `hl-meta-xyz.json` 请求失败，当前报告不把旧 xyz 数据当成今日证据。

JSON 元数据读取上限 16 MiB，其余后续请求上限 1 MiB，单次超时 12–25 秒。`entropy-metadata.json` 是从成功原始响应组装的 replay envelope，不是第四个 HTTP 响应，字段为 `perp_dexs`、`meta_and_asset_ctxs`、`spot_meta`。

## 当前结构、索引和状态

`POST https://api.hyperliquid.xyz/info`：`{"type":"perpDexs"}` 返回数组；当前总长度 11，首槽为 null，`io` 在原始槽 10，fullName 为 EntropyIO。筛除 null 再 enumerate 会改错 dex 索引。

`{"type":"metaAndAssetCtxs","dex":"io"}` 返回 `[meta, asset_contexts]`。`meta.universe` 与 contexts 按原始位置对应，本次均为 11 项。保留已下架条目的位置再判断 eligibility，禁止先筛下架条目再配对 contexts。

| 原始位置 | coin | `isDelisted` | `szDecimals` | 最大杠杆 | marginMode | growthMode | deployerFeeScale |
|---|---|---|---|---|---|---|---|
| 0 | io:OAI | 未标记 | 3 | 6 | noCross | enabled | 1.0 |
| 1 | io:ANTH | 未标记 | 3 | 6 | strictIsolated | enabled | 1.0 |
| 2 | io:SNDK | 未标记 | 4 | 10 | strictIsolated | enabled | 1.0 |
| 3 | io:IONQ | 未标记 | 2 | 10 | strictIsolated | enabled | 1.0 |
| 4 | io:NBIS | 未标记 | 2 | 15 | strictIsolated | enabled | 1.0 |
| 5 | io:EWY | 未标记 | 2 | 25 | strictIsolated | enabled | 1.0 |
| 6 | io:GPRO | 未标记 | 1 | 5 | strictIsolated | enabled | 1.0 |
| 7 | io:SBE | true | 2 | 6 | strictIsolated | 缺省 | 1.0 |
| 8 | io:TCNT | 未标记 | 3 | 10 | strictIsolated | enabled | 1.0 |
| 9 | io:DRAM | 未标记 | 2 | 25 | strictIsolated | enabled | 1.0 |
| 10 | io:PRL | true | 1 | 3 | strictIsolated | 缺省 | 1.0 |

全部条目 `onlyIsolated=true`。9 项未标记下架，2 项标记下架；“未标记”只说明 metadata 状态，不保证可成交、持续更新或账户可交易。该表是动态快照，不应用作实现白名单。

官方 [asset IDs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids) 给出 builder perp action ID 公式 `100000 + perp_dex_index * 10000 + index_in_meta`。当前 SNDK/GPRO 为 200002/200006，应用应动态发现；行情订阅继续使用完整 `io:SNDK`/`io:GPRO`，不能丢掉 dex 前缀。

`meta.collateralToken=0` 对应 `spotMeta.tokens` 中显式 `index=0` 的 token，不是 tokens 数组位置，也不是 spot market ID。当前为 USDC，`tokenId=0x6d1e7cde53ba9467b783cb7c530ce054`、`isCanonical=true`、szDecimals/weiDecimals 均为 8。`isCanonical` 不等于 aligned collateral；本报告未建立 aligned 费用折扣证据。

## 数量、价格、费用与 funding

依 [官方 tick/lot 规则](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/tick-and-lot-size)，SNDK 数量步长为 0.0001，GPRO 为 0.1。perp 价格最多 `6-szDecimals` 小数位且最多 5 个有效数字，整数价格例外。不能仅由 `szDecimals` 断言一个固定价格 tick。当前 replay 样本是行情发现输入，不用于下单舍入。

市场级 `growthMode` 和 `deployerFeeScale` 已直接在 meta universe 给出。`perpDexLimits` 主要给 OI/transfer 限制，`perpDexStatus` 给总净入金；两者不是推导当前费用所必需的请求。

[Hyperliquid 官方 fee formula](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees) 按 `s<1 ? (1+s) : (2*s)` 计算 deployer 费用倍率，growth enabled 时再乘 0.1。当前 scale=1 与 Tier-0 标准 taker 4.5 bps 对应观察基准 0.9 bps；没有查账户 tier、staking、referral、builder code 或实际成交费用，不把这些折扣计入。[Entropy fees](https://docs.entropy.io/equity-perp-mechanics/fees) 与上述基准一致。

当前 `perpDexs.assetToFundingMultiplier` 的 SNDK/GPRO 均为 0.5。它是 funding 公式参数，不是合约数量 multiplier；contexts 返回的小时小数 funding 已应用该参数，不能再次乘。Entropy [funding 文档](https://docs.entropy.io/equity-perp-mechanics/funding-rate) 记载市场时段 0.5、非市场时段 0.125，必须留动态采样，不能写死旧 0.125。Aster `fundingInfo` 当前两个 USD1 品种均 `fundingIntervalHours=8`、`interestRate="0"`；不能把其原始区间费率当小时费率。

[Aster 当前 fees](https://docs.asterdex.com/trading/perpetuals/fees-and-specs/fees) 明确 RWA 和 USD1 RWA taker 为 **1.25 bps**，页面称从 September 7, 2026 生效。旧报告 0.9 bps 不是本次有效事实。费用差影响 scanner 的观察阈值；仅入场扣费不等于包含平仓、funding、滑点与币种基差的净利润。

## SNDK/GPRO 匹配证据与未验证项

[Entropy equity 目录](https://docs.entropy.io/asset-directory/equity-assets) 把 SNDK 定义为 SanDisk Corp.、GPRO 为 GoPro Inc.；本次 Aster exchangeInfo 对应 USD1 品种为股票分类，tags 分别含 SanDisk/GoPro。

| 属性 | io:SNDK | SNDKUSD1 | io:GPRO | GPROUSD1 |
|---|---|---|---|---|
| 市场状态 | 未标记 delisted | TRADING/PERPETUAL | 未标记 delisted | TRADING/PERPETUAL |
| 数量最小步长 | 0.0001 | LOT_SIZE 0.01 | 0.1 | LOT_SIZE 0.01 |
| 报价口径 | 文档 USD 价格 | USD1 | 文档 USD 价格 | USD1 |
| 抵押/保证金币 | USDC | USD1 | USDC | USD1 |
| Aster PRICE_FILTER tickSize | — | 0.01000 | — | 0.00100 |
| Aster MIN_NOTIONAL | — | 5 | — | 5 |

Aster 两项 `underlyingType="COIN"`、`symbolType=1`、`underlyingSubType=["STOCK","AOS2","USD1-RWA"]`，channel 为 nasdaq，quantityPrecision 为 2。必须按具体合约读取，不套用 crypto 分支，也不拼 ticker 自动匹配。当前 Aster 还存在 `SNDKUSDT`；不能任意用它替代已明确选择的 `SNDKUSD1`。

这些证据支持显式记录两组**股票永续观察候选**，不能仅凭 ticker/同样价格证明法律合约或单位完全等价。Entropy API 没有正式 issuer 标识/股票类别/合约数量 multiplier，Aster 公共 exchangeInfo 也未给完整参考证券标识与 corporate-action 协议。本次没有验证股权类别、1 原生数量的经济权益、split 后新旧基准、税务/分红/终止补偿、有效账户杠杆与平仓规则。若 manifest 以 native quantity 1:1 和 USD1/USD=1 作比较，必须明示这是人工接受的观察假设，保留身份来源与这些未验证项。

## Oracle、时段、公司行动差异

Entropy [equity mechanics](https://docs.entropy.io/market-types/equity-perpetuals) 为现金结算、24/7、USDC 抵押；无股票所有权和股息权。价格为 USD。它按底层交易所日历划分常规时段与非时段，停牌/数据陈旧也进入非时段；RedStone 提供参考。拆股、并购/下架经 haltTrading 结束旧市场并以前一日价格结算；现金股息不作调整，拆股后可另设 successor。目录未披露本次两标的 approved after-hours venue 的逐项配置。

Entropy [oracle-price](https://docs.entropy.io/equity-perp-mechanics/oracle-price) 约每 3 秒发布；常规时段 oracle 跟随公开价格，mark 与内部 3 分钟 EMA 平均。非时段 oracle 使用内部 5 分钟 EMA 与外部价格的深度权重混合，mark 以最后公开价的 ±1/最大杠杆界限截断。SNDK/GPRO 当前参数对应 ±10%/±20%。另外 [LULD](https://docs.entropy.io/equity-perp-mechanics/limit-up-limit-down-luld) 文档称穿越范围的订单拒绝；这与 oracle 页强调“only mark clipped”的表述有区别，本次未用真实订单检验执行边界。

Aster [stock mechanics](https://docs.asterdex.com/trading/perpetuals/stock-perpetuals) 以包括 Pyth 的多源 index、三项 median mark 与夜间 EWMA 构造 mark。[US sessions](https://docs.asterdex.com/trading/perpetuals/stock-perpetuals/us-stocks) 给出盘前/盘后/隔夜相对 MarkPrice 的 ±5% aggressive 订单限制，隔夜/周末/假日触发 EWMA。具体市场 API 为 USD1；通用 stock 文档“fully settled in USDT”不能覆盖这两个合约的币种事实。没有查到可直接套用于两 USD1 合约的 split/M&A/dividend 完整官方协议，不能宣布与 Entropy 相同。页面使用 EST，程序不应据此硬编码整年 UTC 偏移。

## 当前公开 L2 短快照与实施验收建议

成功保存四条 REST L2，各 20 bid/20 ask 档：

| 文件 | 原生完整标识 | 本次最优 bid/ask |
|---|---|---|
| hl-l2-sndk.json | io:SNDK | 1674.6 / 1674.8 |
| hl-l2-gpro.json | io:GPRO | 1.2442 / 1.2528 |
| aster-l2-sndk.json | SNDKUSD1 | 1676.60000 / 1676.87000 |
| aster-l2-gpro.json | GPROUSD1 | 1.24600 / 1.25100 |

这四项是在不同时间取得的可读性证据，未同步，不推导可执行价差。它们也不证明 adapter/collector 的持续行情路径已经工作。

实施 smoke 建议先 replay 此目录元数据与 L2，再使用既有 public L2 collector 运行短观察：保留四条完整 instrument ID 的路由、真实 exchange-event 时间、接收时间和每腿连续更新计数；按相同原生数量并经各步长约束计算 VWAP，拒绝陈旧、缺边、crossed 或深度不足。给出单独的 freshness/coverage 与 notional 检查结果，funding 的 None 不伪装为 0，并保留 zero opportunity 的真实结果。无认证观察不应触发 ExecClient、账户或订单。

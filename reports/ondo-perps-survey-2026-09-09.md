# Ondo Perps 活动与产品事实调研（截至 2026-09-09）

来源：sub agent 联网调研 + 主 session 核对。fees.md、changelog.md、funding-rates、Odaily 转述的积分公告四项已亲自复核，其余为 sub agent 结论。

## 一句话结论

Ondo Perps 真实存在且已上线（Public Beta，2026-06-02 pre-alpha 起算，2026-07-07 正式开放）。目前有活动在跑：每周 USDC 交易奖励池 + Ondo Points（每周固定 5M 分，按成交量与 OI 加权）+ 推荐返佣 + 限时手续费 5 折。**没有任何官方宣布的 Perps 代币空投**。网上流传的「Ondo Summit 领取 10% 供应量代币化 NVDA/TSLA」是钓鱼。

## 产品事实

| 项目 | 内容 |
|---|---|
| 名称 | Ondo Perps（运营主体 Ondo Global Panama Inc.） |
| 官网 / App | https://ondoperps.xyz / https://app.ondoperps.xyz |
| 文档 | https://docs.ondoperps.xyz （索引 `/llms.txt`） |
| 官方 X | https://x.com/OndoPerps |
| 状态 | Public Beta（2026-06-02 pre-alpha；2026-07-07 正式开放，07 月移除 Pre-Alpha 限制） |
| 架构 | 跑在 Ondo Network（2026-07-27 发布，取代原先宣布的 Ondo Chain L1）：撮合在 SGX/TEE 内链下运行，资产转移在公链结算。不是链上订单簿，接入方式接近 CEX 的 REST + WS |
| 标的 | 19 支美股（AAPL/NVDA/TSLA/MSFT/META/AMZN/GOOGL/COIN/CRCL/HOOD/MSTR/MU/PLTR/SPCX/AMD/INTC/MRVL/NFLX/ORCL）+ 指数 US500/US100 + 商品 XAU/XAG/WTI/BRENT + ETF DRAM/EWY。文档列 26 个市场，CoinGecko 显示 60 个交易对（含 ONDO 永续，2026-08-12 上线） |
| 杠杆 | AAPL / US500 / US100 / XAU / XAG / WTI 20x，BRENT 15x，其余多数 10x |
| 手续费（已核对 fees.md） | 限时 5 折后 maker 0.01%（1 bp）/ taker 0.025%（2.5 bp）；基础 0.02% / 0.05%。另有 14 日滚动量阶梯折扣（档位未公开）、可协商个别费率；builder 可额外加最多 10 bp。**markets.md 与 CoinGecko 写的是 1.5 bp / 3.5 bp，自相矛盾，下单前以 order ticket 为准** |
| 资金费（已核对） | 每小时结算，一天 24 次，对齐 UTC 整点；公布单位为 **%/小时**（示例 `0.00125%`，百分数形式）；上限 ±1%/hr；利息项 0.03%/日（premium 均值 ÷ 8 + 利息） |
| 保证金 | USDC + 代币化美股（NVDAon、GOOGLon、TSLAon、SNDKon、SPCXon、CRCLon、SLVon、GLDon、SPYon、QQQon）；USDC 负债上限 = 非 USDC 保证金价值的 30% 或 $100,000 取小；$100k 触发 Auto-Exchange |
| 限额 | 默认单市场单账户最大持仓 $1,000,000 |
| 地域 | 全球开放，**美国 / 巴拿马及其他受限法域除外**；未见强制 KYC（Points 抽奖中奖需 KYC）。vultr NJ 机器大概率被拦，接入前先测 |
| API | REST + WebSocket（depth book、funding rate、mark price、TWAP、批量下单/撤单、dead man's switch）。**changelog（已核对）：API key 默认开启，默认限速 1 request/second**，需向官方确认能否提额 |
| 规模 | 上线数周累计成交 >$8B；30 日约 $5.37B；24h 约 $105–210M（峰值单日 >$350M）；OI 约 $81–87M；CoinGecko 记录平均买卖价差 0.104%（约 10.4 bp，偏宽） |
| 支持 | support@ondoperps.xyz |

## 活动清单

| 活动名 | 类型 | 起止 | 计分/返还规则 | 资格限制 | 来源 | 官方确认 |
|---|---|---|---|---|---|---|
| Weekly USDC Rewards | 交易量返现 | 2026-06 起持续，无公布结束日 | 宣称总额 up to $3M；首周 $150K USDC 按成交量占比分配；近期报道约 $150–175K/周，并对特定市场（GOOGL、TSLA）单独配额；每周可领 | 非美国/巴拿马等受限法域 | https://app.ondoperps.xyz/rewards ；https://genfinity.io/2026/07/07/ondo-perps-launch-tokenized-stock-collateral-leverage/ (2026-07-07)；https://airdrops.io/ondo-perps/ | 是（活动存在）；当周金额与分市场配额只见第三方 |
| Ondo Points | 积分 / 潜在空投 | 计分回溯至 2026-06-02，2026-07-23 官宣，持续 | 每周固定 5,000,000 分，按「成交量 + OI」加权得分占比瓜分，邀请产生的新用户成交量另计分；余额在 https://app.ondoperps.xyz/points 查 | 非受限法域；bot 排除 | https://x.com/OndoPerps/status/2080110863436894431 (2026-07-23)；https://www.odaily.news/en/newsflash/503094 (2026-07-23，已核对) | 是（规则）；兑换比例、是否换币未确认 |
| Referral 推荐计划 | 返佣 | 持续 | fees.md 确认：从 Exchange Fee Account 支付推荐人佣金（手续费一定比例）+ 被推荐人返还（手续费一定比例）。第三方报道推荐人 10%、被推荐人 5% 折扣 | 同上 | https://docs.ondoperps.xyz/fees.md ；https://airdrops.io/ondo-perps/ ；https://candydrops.xyz/en/projects/ondo-perps (2026-07-28) | 部分（机制官方，数字第三方） |
| 手续费 5 折促销 | 费率优惠 | 「限时」，无截止日，2026-09 文档仍在 | maker 0.02%→0.01%，taker 0.05%→0.025% | 全体 | https://docs.ondoperps.xyz/fees.md | 是（已核对） |
| 成交量阶梯 + 个别协议费率 | 费率优惠 | 长期 | 14 日滚动成交量给折扣；大户可谈定制费率；VIP 表未公开 | 全体 | https://docs.ondoperps.xyz/fees.md | 是（存在） |
| tread.fi 前端奖励（$100K/周） | 第三方 builder 活动 | 起止未知 | 通过 tread.fi 前端交易 Ondo Perps 可分 $100K | 未知 | https://x.com/0xasrequired/status/2061835718557614325 | 否（个人 X 帖） |

### 近 3 个月已结束 / 非 Perps 的相邻活动

| 活动 | 说明 | 来源 |
|---|---|---|
| Ondo Foundation 旧积分活动（USDY Holder / LP / Minter、mUSD、OUSG、Flux、Galxe 等） | 已关闭。快照 2026-04-01，2026-06-11 开放领取，抽奖券 2026-07-11 截止。≥100 分得 50–500 USDC 分档 + 抽奖券。仅非美国持有人 | https://blog.ondo.foundation/ondo-points-rewards-are-now-available-to-claim/ (2026-06-11)；https://blog.ondo.foundation/snapshot-taken/ (2026-04-02) |
| Trust Wallet「Ondo 代币化美股交易赛」 | $100,000 奖池 + 0% 手续费，BNB Chain 上的 Ondo 现货代币化股票，不是 Perps | https://trustwallet.com/blog/campaigns/ondo-trading-competition-trade-tokenized-stocks-on-bnb-chain-for-0-fees |
| Cantina 漏洞赏金 | 安全赏金，非交易激励 | https://cantina.xyz/bounties/5ffa2a58-2121-4986-b318-43f8ba9a559e |

### 未来已公告的

无。Ondo 官方博客 2026-08 至 09-02 的贴文（Ondo Network、USDY 上 BNB/Tempo、SEC 评论信、Open Markets by Design）均未提及激励活动。

## 未确认 / 存疑

1. 官方文档手续费自相矛盾（fees.md 1/2.5 bp vs markets.md 1.5/3.5 bp）。
2. 每周 USDC 池当前金额未在官方页面找到；`/rewards` 是 SPA 抓不到正文，只能 app 内实测。
3. Ondo Points 兑换价值完全未定；Perps 积分可能与其他 Ondo 活动积分共池稀释。没有 Perps 专属代币或空投的官方公告。
4. 积分中成交量 vs OI 的权重未公开。
5. **钓鱼警告**：「Ondo Summit 奖励活动 / 10% 供应量发放 NVDAON、TSLAON、XAUT、SLVON」为诈骗，安全厂商已收录 https://www.pcrisk.com/removal-guides/33666-ondo-rewards-scam 。Ondo Summit 2026（https://summit.ondo.finance/2026 ，纽约）是真实会议，官方页面无任何领取活动。`medium.com/@ondofinance__art`、`ondo.money` 均非官方域名。
6. 是否需要 KYC / 白名单：文档只说「qualifying users」，需邮件 support@ondoperps.xyz 确认。

## 对本项目的直接含义

- 资金费小时级、%/hr 单位、±1%/hr 上限，与 HL、Lighter 节奏一致，比 Aster 好对齐；CSV 存原始值时注意是百分数形式。
- 标的重合度高：NVDA、TSLA、MU、SPCX 都有。
- maker 1 bp 便宜，但平均价差约 10 bp、OI 约 $85M，深度可能是主要约束。
- API 默认 1 req/s 对对冲腿是硬伤，先问官方提额。
- 单市场单账户持仓上限 $1M；代币化股票抵押的 USDC 负债上限 $100k。
- 美国 IP 不可用，vultr NJ 机器需先测连通性。

## 全部引用 URL

官方
- https://ondo.finance/blog/introducing-ondo-perps (2026-02-03)
- https://ondo.finance/blog/tokenized-stock-collateral-for-equity-perps (2026-07-07)
- https://ondo.finance/blog/introducing-the-ondo-network (2026-07-27)
- https://ondo.finance/blog/open-markets-by-design (2026-09-02)
- https://ondo.finance/blog
- https://blog.ondo.foundation/
- https://blog.ondo.foundation/ondo-points-rewards-are-now-available-to-claim/ (2026-06-11)
- https://blog.ondo.foundation/snapshot-taken/ (2026-04-02)
- https://docs.ondoperps.xyz/llms.txt
- https://docs.ondoperps.xyz/fees.md
- https://docs.ondoperps.xyz/markets.md
- https://docs.ondoperps.xyz/funding-rates
- https://docs.ondoperps.xyz/public-beta.md
- https://docs.ondoperps.xyz/changelog.md
- https://app.ondoperps.xyz/rewards
- https://app.ondoperps.xyz/points
- https://x.com/OndoPerps/status/2080110863436894431 (2026-07-23)
- https://summit.ondo.finance/2026

媒体 / 第三方
- https://www.odaily.news/en/newsflash/503094 (2026-07-23)
- https://genfinity.io/2026/07/07/ondo-perps-launch-tokenized-stock-collateral-leverage/
- https://www.thestreet.com/crypto/markets/ondo-perps-tops-8b-trading-volume-within-weeks-of-launch
- https://www.thestreet.com/crypto/markets/ondo-perps-breaks-past-300m-in-24-hour-volume-milestone
- https://www.coingecko.com/en/exchanges/ondo-perps
- https://airdrops.io/ondo-perps/
- https://airdrops.io/blog/ondo-points-value-analysis/
- https://news.todayindefi.com/p/ondo-perps-points-live-paradex-claim (2026-07-29)
- https://candydrops.xyz/en/projects/ondo-perps (2026-07-28)
- https://techflowpost.substack.com/p/ondo-perps-20 (2026-07-08，中文实测)
- https://dexcexhub.com/Blog/OndoPerps
- https://trustwallet.com/blog/campaigns/ondo-trading-competition-trade-tokenized-stocks-on-bnb-chain-for-0-fees
- https://cantina.xyz/bounties/5ffa2a58-2121-4986-b318-43f8ba9a559e
- https://x.com/0xasrequired/status/2061835718557614325

风险提示
- https://www.pcrisk.com/removal-guides/33666-ondo-rewards-scam

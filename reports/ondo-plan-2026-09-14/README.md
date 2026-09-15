# Ondo Perps 完整接入：规划证据（2026-09-14）

方案见 [完整实施计划](../../docs/superpowers/plans/2026-09-14-ondo-perps-full-integration.md)。本目录只有官方协议材料、公开短测和规划验证，没有实现交易所 adapter。

## 当前核查

- 应用 HEAD：`76b9881de1a453dbc8c4cff150395bafe649c279`，main。
- fork HEAD：`047d494e3c260bc6f701aa8f9193d1fd36f8da89`，当前分支 `onde-perps`。未编辑 fork。
- 应用 venv 导入 NautilusTrader `2.0.0rc4`；没有构建或安装 wheel。
- 应用现有行情/资金费/reference 离线基线：83 passed in 0.72s。命令：`.\.venv\Scripts\python.exe -m pytest tests/test_spread_watch.py tests/test_opportunities.py tests/test_ref_feed.py -q -p no:cacheprovider`。未运行全套测试。

`manifest.json` 保存每个官方文件的 URL、UTC 获取时间、HTTP 状态和 SHA256，失败项保留 error 而没有伪造对应文件。`public-beta.md` 和 `/status` 本次遇到 TLS EOF；markets/contracts/depth/funding 本次 REST 遇到 403。

`connectivity-probe.json` 保存后续一次 markets 请求及最长约 8 秒的公开 WS 消息。REST 返回 `error code: 1010`，响应标记 Cloudflare，未证明是地域封锁。已删除响应的 Set-Cookie。WS 原始 message 字符串保留。

`probe-summary.json` 是从该探针文件计算的每频道/市场计数与 funding 原值；记录探针 SHA256。没有跨所同步采样，不计算当前套利空间。

| 频道 | NVDA-USD.P 更新数 | TSLA-USD.P 更新数 |
|---|---:|---:|
| topOfBooksPerps | 5 | 17 |
| depthBooksPerps | 32 | 31 |
| fundingRatesPerps | 2 | 2 |
| tradesPerps | 2 | 0 |

所有四个频道均收到订阅确认。TSLA 零成交只是样本内未收到成交，不是订阅失败。

本地接收时间约 `2026-09-14T11:10:00.861748Z` 至 `11:10:08.776553Z`。这是公开协议可读证据，不能替代新 DataClient/LiveNode 的验收。

## 协议结论

- `depthBooksPerps` 官方 schema 是 BookSnapshot，实测 data 是数组，每市场完整 bids/asks，没有交易所 sequence/checksum；默认不设 depthLevels，探针 limit=10。
- funding 实测 `rate=0.0000063`、`intervalEnds=2026-09-14T12:00:00Z`。API 为小时小数，折算 `0.063 bp/h`；没有观察真实资金费支付。
- REST 专页 ONDO-KEY-ID 与 REST spec X-API-KEY-ID 冲突。
- WS connect 概览为 label+time；login 专页为 time+label。二者都已归档；没有执行私有认证来消除冲突。
- WS 返回枚举和 REST 创建枚举并不完全一致，例如 FOK 和 fill.direction 拼写。创建能力以已验证 endpoint 为准。
- 9 月 11 日报告的市场数/交易状态不是今日复核结果；今日 fees 页面可读，但不是当前账户费率证明。

## 官方入口

- [索引](https://docs.ondoperps.xyz/llms.txt)
- [REST spec](https://docs.ondoperps.xyz/api-reference/rest-spec.json)
- [WS spec](https://docs.ondoperps.xyz/api-reference/ws-spec.json)
- [API key](https://docs.ondoperps.xyz/api-reference/api_key_authentication.md)
- [WS connect](https://docs.ondoperps.xyz/api-reference/connection/connect.md)
- [WS login](https://docs.ondoperps.xyz/api-reference/connection/login.md)
- [Fees](https://docs.ondoperps.xyz/fees.md)
- [Funding](https://docs.ondoperps.xyz/funding-rates.md)

通过标准 Python TLS 校验读取，未关闭证书校验、未登录、未签名、未下单、未切换网络。规划阶段全部连接已经结束。

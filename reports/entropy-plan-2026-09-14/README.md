# Entropy io: 实施计划的公开核查证据

日期：2026-09-14；所有 JSON/JSONL 中时间均为 UTC。本目录是规划阶段的核实结果，未改交易或行情实现，未运行改造后的 LiveNode，没有账户查询、签名或下单。

计划：[`2026-09-14-entropy-io-sndk-gpro.md`](../../docs/superpowers/plans/2026-09-14-entropy-io-sndk-gpro.md)。

## 文件与来源

| 文件 | 内容 |
|---|---|
| [hl-meta-io.json](hl-meta-io.json) | `POST https://api.hyperliquid.xyz/info`，`metaAndAssetCtxs dex=io`，完整 JSON 响应及请求时间 |
| [hl-perp-dexs.json](hl-perp-dexs.json) | 同一公开端点的完整 `perpDexs` 数组，保留 null/下架市场位置 |
| [aster-exchange-info.json](aster-exchange-info.json) | `GET https://fapi.asterdex.com/fapi/v1/exchangeInfo` 完整公开 JSON 响应 |
| [aster-funding-info.json](aster-funding-info.json) | `GET https://fapi.asterdex.com/fapi/v1/fundingInfo` 完整公开 JSON 响应 |
| [wheel-instrument-check.json](wheel-instrument-check.json) | 第一次已安装 wheel 公开 metadata 检查，返回 0 个 instrument，保留失败结果 |
| [wheel-proxy-instrument-check.json](wheel-proxy-instrument-check.json) | 第二次检查；`proxy_configured=false`，加载 519 个 instrument，输出 3 个指定目标的属性 |
| [hl-public-ws-probe.jsonl](hl-public-ws-probe.jsonl) | 原始公开 WS 的 12 条订阅请求及约 8 秒收到的 JSON 消息，按完整 coin 分类 |

JSON 文件保留解析后的完整业务响应，不是 HTTP 原始字节/响应头归档。WS JSONL 保留收到的公开消息 payload，外层附接收时间；不是 Nautilus adapter 输出。

第二次 wheel 文件名记录了“检查系统代理”这一诊断动作，实际没有配置 proxy，不能解释成“代理解决了故障”。第一次空列表原因尚未定位，第二次成功不能删除第一次失败。

## 核实结果

- 当前 io universe 10 个，未标记 delisted 的 6 个；SNDK/GPRO 原始 universe 索引为 2/6。`perpDexs` 中 io 索引为 10。当前 action ID 为 200002/200006，仅作元数据核对，应用继续交给 adapter 动态发现。
- `io:SNDK-USD-PERP.HYPERLIQUID`：raw_symbol `io:SNDK`，数量步长 0.0001，价格精度 2，quote USD、settlement USDC、multiplier 1、非 inverse。
- `io:GPRO-USD-PERP.HYPERLIQUID`：raw_symbol `io:GPRO`，数量步长 0.1，价格精度 5，quote USD、settlement USDC、multiplier 1、非 inverse。
- `xyz:SNDK-USD-PERP.HYPERLIQUID` 同时存在，数量步长 0.001。它与 io:SNDK 是不同合约。
- 三个 wheel instrument 的 `info` 均为空字典。marginMode/growthMode/deployerFeeScale 必须从公开 meta 读取，不能假设 wheel info 有这些字段。
- Aster 对应 `SNDKUSD1`、`GPROUSD1`，均 TRADING/PERPETUAL、quote/margin USD1、LOT_SIZE stepSize 0.01、fundingIntervalHours 8。
- io 的 SNDK/GPRO 均 strictIsolated、growthMode enabled、deployerFeeScale 1.0；实时 funding multiplier 分别 0.125/0.5。multiplier 已用于产生返回的 funding，不应再次相乘。

公开 WS 短样本计数如下。它验证频道和 coin 前缀，不验证持续可用性、交易执行或利润。

| coin | bbo 消息 | l2Book 消息 | activeAssetCtx 消息 | trades 消息 |
|---|---:|---:|---:|---:|
| io:SNDK | 48 | 2 | 9 | 8 |
| io:GPRO | 8 | 2 | 9 | 1 |
| xyz:SNDK | 70 | 2 | 9 | 22 |

另有 12 条 subscriptionResponse。trades 是消息数量，不是消息数组内成交笔数；初始历史快照与新成交未在本次统计中分离。

## 本地环境和离线测试

- 主仓库 HEAD：`c33c0a74dbae29744206407fce1cb07bdbe1ddcb`。
- 只读查看的 fork HEAD：`047d494e3c260bc6f701aa8f9193d1fd36f8da89`，没有修改该仓库。源码 HEAD 不等于 wheel 构建来源证明。
- `.venv` 导入返回 `nautilus_trader.__version__ = 2.0.0rc4`。
- 已安装二进制：`.venv/Lib/site-packages/nautilus_trader/_libnautilus.cp312-win_amd64.pyd`，SHA256 `C1BC33ABC7FCEA5E9EECF1C934648ECA28B9C58D16FC5690D6936854274BFC2A`。
- 实际执行 `.venv\Scripts\python.exe -m pytest tests/test_spread_watch.py tests/test_ref_feed.py -q -p no:cacheprovider`，返回 `55 passed in 0.86s`。这是既有实现基线，不是 Entropy 新实现的测试结果。
- 实测签名：`FundingRateUpdate(instrument_id, rate, ts_event, ts_init, interval=None, next_funding_ns=None)`；`QuoteTick(instrument_id, bid_price, ask_price, bid_size, ask_size, ts_event, ts_init)`。
- 沙箱内 venv 指向的解释器一度不可访问，同一导入命令获准访问后成功；PowerShell 公开请求第一次报接收时连接关闭，随后 Python urllib 的四项公开请求均成功。没有因此重建 venv、修改代理或安装依赖。

## 官方文档（本次已读取）

- [Hyperliquid HIP-3 asset IDs](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)：保留完整 dex:coin，以及从完整 metadata 取得索引。
- [Hyperliquid public WS subscriptions](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions)：bbo/l2Book/trades/activeAssetCtx 均按完整 coin 订阅。
- [Hyperliquid perpetual info](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals)：dex、metaAndAssetCtxs、allPerpMetas。
- [Entropy fees](https://docs.entropy.io/equity-perp-mechanics/fees)、[Hyperliquid fees](https://hyperliquid.gitbook.io/hyperliquid-docs/trading/fees)：当前 scale=1、growth enabled 下，Tier-0 taker 基准 0.9 bp，无返佣/质押假设。
- [Entropy equity assets](https://docs.entropy.io/asset-directory/equity-assets)：SNDK/GPRO 的 issuer 和 USDC 市场口径。
- [Aster fees](https://docs.asterdex.com/trading/perpetuals/fees-and-specs/fees)：RWA taker 0.009%，不能套用普通 USD1 永续的 0.005%。

未核查用户具体佣金、资金账户、权益折算与完整对冲等价性。此前研究选出 SNDK/GPRO 只是候选依据，本次不更新收益判断。

## SHA256

```text
5ED0B8DA5597ED7F2FE776A3A5CFA2623B6B8EFCD20192B7C2CE3ED1AA94FE27  aster-exchange-info.json
0BE616D0970F0F89A3D57E71E02B600E7AF240D0EF20BE8AE213FE6AC467B3EF  aster-funding-info.json
4B1E00080528A8C1B91F4E73F0AEB52FBA78C8188FEEE1205726D59E4C80650F  hl-meta-io.json
2208AA367DE97CBB19B44A31E83F85E29E2A4A37F4E60301A0422132AC22EC99  hl-perp-dexs.json
10A967E300417E41949B4F9B067F84AF148D38CD98699A5204A4DE90F394F59A  hl-public-ws-probe.jsonl
34FAEA28B45F5EBDCDCB4FB30AA0C64269AD6467DE8248F52DDAAC0D4E55EB94  wheel-instrument-check.json
39ECF7CEB3CC6710530FA29F582C2A8C1B7D4A030858054EEFB2E40F91680D28  wheel-proxy-instrument-check.json
```

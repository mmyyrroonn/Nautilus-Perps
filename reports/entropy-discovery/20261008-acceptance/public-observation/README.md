# Entropy / Aster 真实公开 scanner L2 短观察

2026-10-08，正式安装的 `802e…` wheel，工作树独立 `.venv`。本目录只保存公开元数据、CLI 留痕与有界观察；未读取 `.env`、账户、签名或订单，也没有注册执行客户端。

## 真实结果

唯一实际 native 公网观察窗口：2026-10-08 09:39:47.926624–09:40:48.768356 UTC，配置时长 60 秒，报告 elapsed 60.25 秒，进程 **exit 0**。actor_started=true、actor_failure=null；4 条腿均在窗口内至少收到过符合原始门槛的 L2。峰值为 4 条腿同时 fresh，2 个 symbol 至少各有一对符合时间偏差门槛的书。

| 实际 InstrumentId | native book 更新数 | 当前 bid/ask 档位 | 终态 event / receive age (ms) | 终态 usable_now |
|---|---:|---|---|---|
| io:SNDK-USD-PERP.HYPERLIQUID | 11 | 20 / 20 | 5671.850 / 4706.515 | false |
| io:GPRO-USD-PERP.HYPERLIQUID | 11 | 20 / 20 | 5671.850 / 4706.536 | false |
| SNDKUSD1-PERP.ASTER | 222 | 20 / 20 | 961.850 / 321.928 | true |
| GPROUSD1-PERP.ASTER | 15 | 20 / 20 | 2211.850 / 1529.580 | false |

门槛保持 **source age 2000 ms、receive age 2000 ms、两腿 skew 500 ms**。所有真实 cache 元数据通过各腿 expected_instrument 的完整身份、quote、settlement、native 步长和 multiplier 检查。观察保留双侧最多 20 档的当前原生数量和时间，数量合计不代表完整订单簿，也不证明某一名义金额可成交。

`native-attempt-2.health.json` 的 all_markets_observed_fresh=true 是 **ever-fresh coverage** 成功，并不是每时每刻或终态所有腿都新鲜。终态 io 两腿与 Aster GPRO 都过期，不能宣布持续可交易。原始 venues.last_fresh 指最后一次定时 refresh sample；额外 per_instrument.usable_now 是 scalar_snapshot_at_ns 时刻重算，终态二者可不同。

recording.enabled=false，opportunities_recorded=0、historical_book_samples_retained=0。终态 state.current 在停止时被清空；该 0 不用于证明窗口内从未出现资格机会。进度 JSON 是离散采样，不是机会收益历史，本次不形成盈利结论。

原始 runner 的 node.run 正常返回，子进程正常退出，110 秒外层 watchdog 未触发。该标准公共接口没有暴露 socket 关闭计数，因此本报告不声称公网 socket active=0，也不把 synthetic loopback 关闭计数搬到此窗口。

## 实际命令与产物

工作目录 `E:\persarb\worktrees\entropy-app`：

```powershell
.\.venv\Scripts\python.exe src/opportunity_universe.py --venues ENTROPY,ASTER --dex io --entropy-matches config/entropy-matches.example.json --timeout-secs 15 --capture-dir reports/entropy-discovery/20261008-acceptance/public-observation/capture-attempt-2 --output reports/entropy-discovery/20261008-acceptance/public-observation/registry-attempt-2.toml --summary reports/entropy-discovery/20261008-acceptance/public-observation/discovery-attempt-2.json
```

第 1 次 metadata CLI 为 TLS UNEXPECTED_EOF，exit 2，stdout/stderr/exit 单独保留。第 2 次 CLI 在 09:38:29.390998–09:38:31.136273 UTC 成功，exit 0；它当场抓取 ASTER exchangeInfo 和 ENTROPY 的 perpDexs、io metaAndAssetCtxs、spotMeta，**没有借用 09:14 UTC 的研究快照充当新采集**。

`capture-attempt-2/` 下保存标准名 raw body、assembled entropy-metadata.json 和 public-metadata-provenance.json；provenance 包含每组件请求类型、URL、客户端采集 UTC、SHA256 和原始路径。上游响应未提供的 source_capture_time 保持 unknown，不能与已知的客户端 fetched_at_utc 混淆。

从 CLI 生成的 registry-attempt-2.toml 生成 `scanner-readonly.toml`，只改 recording.enabled=false、connection_timeout_secs 120→30、duration_secs 3600→60；freshness 与 skew 没有放宽，仍是 4 条显式 SNDK/GPRO 映射。

观察命令由有界 subprocess 留存执行：

```powershell
.\.venv\Scripts\python.exe reports/entropy-discovery/20261008-acceptance/public-observation/observe_native.py --config reports/entropy-discovery/20261008-acceptance/public-observation/scanner-readonly.toml --duration-secs 60 --progress-secs 15 --report reports/entropy-discovery/20261008-acceptance/public-observation/native-attempt-2.health.json
```

`observe_native.py` 直接调用现有 opportunity_connections.main，仅扩展 ConnectionHealth.document 的报告字段，输出每腿标量更新数和当前 timestamps/quantities。正常 runtime.build_node、public data_clients factories、默认生产 public URLs、native cache、L2 events、scanner 计算和 gate 均未替换。它没有修改生产源代码、模拟行情、用 REST L2 代替 native 订阅或加载外部机器。

helper 首次启动的 ROOT 路径错误在 import 阶段报 ModuleNotFoundError，未启动 native；`native-attempt-1.*` 保留这次失败。修正后 `native-attempt-2.*` 才是唯一实际公网窗口。

`native-attempt-2.command.json` 为精确解释器、argv、UTC 起止和退出码；stdout 包含真实 adapter 警告、15 秒间隔进度及最终 JSON；stderr 本次为空。`native-attempt-2.health.json` 为最终有界统计。未进行第二个实际公网窗口。

## 仍未验证

本窗口只验证公共 L2 路径与短期覆盖，不验证账户准入、持久 SLA、实际佣金、交易执行、实际 hedge 等价、退出价、funding 收益或 USD1/USDC/USD 基差。native 更新数是 actor 发布进 ScanState 的书更新，不等于 WS 原始帧数或成交笔数。source/receive 时间是正式 adapter 提供的对应字段，本次未重写 native 时间语义。

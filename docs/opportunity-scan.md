# 仅记录机会的跨所扫描

关联 [issue #32](https://github.com/mmyyrroonn/Nautilus-Perps/issues/32) 与
[扩容 issue #34](https://github.com/mmyyrroonn/Nautilus-Perps/issues/34)。入口是
`src/opportunity_scan.py`，独立于持续写 CSV 的 `spread_watch.py`。一个配置可以包含多个标的、多个所；
同一 `symbol` 必须代表同一经济标的。程序只注册公共数据客户端，不读取 `.env` 或注册执行客户端。
离线与原生本地模拟行情的验收结果见 [2026-10-06 验收记录](../reports/opportunity-scan/20261006/acceptance.md)。

## 启动与配置

在 `E:\persarb\Nautilus-Perps`，使用经过项目流程安装 fork wheel 的 Python 环境：

```powershell
.\.venv\Scripts\python.exe src\opportunity_scan.py --config config\opportunity-scan.example.toml --dry-run
.\.venv\Scripts\python.exe src\opportunity_scan.py --config config\opportunity-scan.example.toml --symbols BTC --duration-secs 300
.\.venv\Scripts\python.exe src\opportunity_scan.py --config config\opportunity-scan.example.toml --no-record --duration-secs 0
```

`dry-run` 只读取指定配置、输出 JSON，不导入原生模块、不建立连接、不创建输出目录。
`--no-record` 可以覆盖配置里的录制开关。`duration_secs = 0` 表示持续运行，Ctrl+C 停止。
目标金额也可通过 `--target-notional 1000` 覆盖。

示例包含 BTC/ETH × HL/Lighter/Aster/Ondo/Backpack。市场 ID 在启动时必须由真实 instrument metadata
确认；示例不表示这些场所当下都可用。删除不需要的 `[[markets]]`，每个标的仍至少保留两个所。
添加更多标的可以从公开市场目录生成明确的映射；未知品种不会仅凭代码拼接就被认为已上市。
目录发现是独立命令，运行中的扫描器不热加载新市场。
`HL` 和 `ENTROPY` 共享一个 HYPERLIQUID 客户端，Lighter RH 使用独立客户端。
`io:` 可在通用 scanner 中显式配置 `venue = "ENTROPY"` 与完整 Hyperliquid InstrumentId。
2026-10-11 已取消 Entropy 专项动态发现与执行队列，依据与回退范围见
[entropy-retirement.md](entropy-retirement.md)；原 `--dex io` / `--entropy-matches` 入口已移除。

### 多币种目录与连接检查

`config/opportunity-scan.universe.toml` 是 2026-10-06 的五所公开目录快照，包含 167 个币种、498 个市场：
HL 149、Lighter 109、Aster 161、Ondo 9、Backpack 70。每个币种至少覆盖两个所。
`config/opportunity-scan.dg-us.toml` 是现有 Linux 安装包可用的四所配置，包含 164 个币种、425 个市场。
目录快照不是持续行情历史，也不代表每条盘口始终新鲜。新上市、停牌或退市后应重新生成。

```powershell
.\.venv\Scripts\python.exe src\opportunity_universe.py --output config\opportunity-scan.universe.toml
.\.venv\Scripts\python.exe src\opportunity_universe.py --venues HL,LIGHTER,ASTER,ONDO --output config\opportunity-scan.dg-us.toml
.\.venv\Scripts\python.exe src\opportunity_connections.py --config config\opportunity-scan.universe.toml --duration-secs 240
.\.venv\Scripts\python.exe src\opportunity_scan.py --config config\opportunity-scan.universe.toml --duration-secs 0
```

发现命令仅调用固定的公开市场目录 API，不保存盘口；可加 `--symbols BTC,ETH,SOL` 或 `--dry-run`。
离线重建可用 `--metadata-dir <目录>`，读取 `<venue>-metadata.json`。可选 `--summary <路径>`
保存目录来源和哈希；默认仅输出摘要。股票、商品、现货、停用品种及身份冲突会排除。
`k` / `1000` 前缀不会自动去除：未经逐个确认的数量单位不能互相配对。HL 原生 ID 的大小写会保留。
Lighter 的 operator index 无法区分股票和币种，因此还须与其他所明确标为 crypto 的目录交叉核对。
Backpack 原生客户端最多配置 100 个市场；生成器同时遵守总计 1000 个映射的应用上限。
Backpack 客户端单侧最多保留 10000 层，HTTP 请求最长 60 秒，内部盘口过期最长 30000 毫秒；
扫描器对这些参数取更严格的客户端上限。REST 快照选择不超过客户端容量的支持档位，
全局盘口容量低于 5 层时拒绝 Backpack 配置。

连接检查入口运行同一个原生扫描器，强制关闭机会录制，只保留计数与当前覆盖率。
返回 0 表示配置中的每个市场都曾收到通过时间、盘口、费用元数据检查的数据；缺失则返回 1。
摘要同时报告最后一次刷新时的新鲜盘口、同时可比较的币种峰值、CPU 和 Linux 内存峰值。
`--venues HL,LIGHTER` 可隔离场所问题；`--report <路径>` 仅保存聚合诊断，不含盘口价格或历史采样。
Lighter 大批订阅须等待节流，扩容配置预留 120 秒连接超时。
大配置使用 `evaluation_interval_ms = 100`：每条增量都进入当前 native L2，
只保存每个市场最新完整批次的时间与序列；到评估时先更新全部变化的腿，再计算受影响币种。
因此不会将本轮新盘口与已知变化、尚未发布的旧盘口混合录制。该模式可能略过两次评估之间的短暂机会。
`evaluation_interval_ms = 0` 保留每批即时计算。显示刷新只移除失效机会，不提前触发录制。

Aster 大配置用 `aster_snapshot_depth = 100`、`aster_subscription_interval_ms = 500`，
分批获取初始快照，避免默认 1000 档请求集中启动超出接口预算。计算只使用前 20 档，
深度不够目标金额就不判定机会。运行时长包含连接与分批启动，160 个 Aster 市场需要至少约 80 秒启动。
重连与其 HTTP 重试仍由原生适配器处理，完整长期恢复测试尚未完成。

dg-us 的 240 秒复测覆盖全部 425 个市场；每个市场都曾通过新鲜度检查，
同时新鲜盘口峰值 413 个、满足时间差检查的币种峰值 100 个。
平均进程 CPU 69.78%（约占两核机器的 34.89%）、内存峰值约 364 MiB。
这次连接检查强制关闭录制，不证明捕获了收益机会或完成长期 soak。详细计数与限制见
[扩容验收记录](../reports/opportunity-scan/20261006-universe/acceptance.md)。

dg-us 的安装包暂不含 Backpack 原生模块。五所配置须在包含该模块的 wheel 上运行；
公开 WebSocket 的验证与原生扫描器验证分别报告。

费用优先使用每个市场显式配置的 `taker_fee_bps`，否则使用 instrument metadata。
配置费率必须带 `fee_source`，会作为假设原样进入证据。通用 instrument 的零费率可能是未提供费用的默认值，
因此只有 Lighter 明确的零费率以及 Backpack 完整配置的 economics 接受零值；费用未知就不判定机会。
Backpack 公共适配器要求显式 `backpack_economics`，示例值是 Configured 假设，不代表已核实的账户费率或保证金。
Aster 币种的一般 taker 费率配置为 4 bps，已列出的 Group B 使用 10 bps，依据
[公开费用表](https://docs.asterdex.com/trading/perpetuals/fees-and-specs/fees)；扩容前的小示例也已修正。
费用分类会变动，生成器中的 Group B 清单按 2026-10-06 冻结，更新时须核对。

每个市场必须显式给 `quote_to_usd` 和 `valuation_source`，说明如何将报价换算为 USD。
示例的 1:1 稳定币换算也是假设，不是实时汇率。特殊的 1000-token 市场可显式给 `canonical_multiplier = "1000"`，
表示一个原生基础单位对应 1000 个标准代币；普通市场默认 1。原生合约乘数和数量步长读取实际 metadata：
标准标的数量 = native quantity × native contract multiplier × canonical multiplier；
标准 USD 单价 = native price × quote_to_usd / canonical multiplier。
因此原生成交金额仍是 native quantity × native contract multiplier × native price × quote_to_usd。
两个乘数都会进入机会证据。归一化使用精确有限小数，无法精确表示的换算会停止扫描，避免静默舍入；反向合约会被拒绝。

## 什么会被记录

每次盘口更新只比较涉及该标的的方向；显示定时器同时清除过期机会。买卖盘必须双边有效、不交叉、有足够深度，
源时间、接收时间和两边源时间差均须通过配置阈值。报价 tick 不会延长盘口有效期。
使用精确数量步长的最小公倍数配平双边标准标的数量，检查两边最低数量和最低名义额。
`target_notional` 是买入腿的最大 USD 成交本金，不含手续费；因步长取整，实际本金可能较小。
两边手续费分别按各自预计成交金额计算，reserve 按买入本金计算。

```
entry_after_fees_and_reserve = sell_cash - buy_cash - buy_fee - sell_fee - reserve
entry_after_fees_and_reserve_bps = entry_after_fees_and_reserve / buy_cash * 10000
```

只记录结果为正且达到 `min_entry_edge_bps` 的事件。未满足条件时不创建数据文件、CSV、运行清单或历史样本。
设置 `recording.enabled = false` 则只显示当前机会。JSONL 路径相对于配置所在目录解析，首次记录时才建立目录和文件。
库日志仅发往终端；本入口关闭文件日志、缓存市场数据落盘以及节点状态加载/保存。

每条 JSONL 是独立机会快照，包含配置阈值、计算时刻、数量配平、双边均价、各腿费用及来源、标准化盘口、
原始 native 盘口与各自时间戳、盘口截取范围及换算假设。原始盘口记录是用于该次计算的固定副本，不是稍后重新查询。
截取 `depth_levels` 档时只声明该覆盖范围；不宣称拥有场所完整盘口。手续费与收益为估计值，完整进出场收益标为 unknown。
第一版不计算资金费收益、平仓收敛、成交概率或账户可用保证金，也不发订单。

默认首次出现保存一次，此后须经过 `cooldown_ms` 且价差变化达到 `edge_change_bps`，或配平数量相对变化达到
`quantity_change_fraction`，才再保存。可选 `max_interval_ms` 定期记录持续满足条件的机会。
机会消失时清除当前去重状态但不写文件，再次出现会保存新事件。去重状态不跨进程重启恢复。
写入失败会停止扫描并返回非零状态，避免继续运行却让人误以为记录完整。

## 内存与运行边界

只保留每个配置市场的当前 native L2、当前截取盘口和每个方向的少量展示/去重状态；没有按时间增长的价差历史。
当前 L2 的价格键集合用于增量检查档数上限，避免每次更新复制整个深层盘口。
Nautilus tick/bar cache 容量均为 1，未订阅逐笔成交或 bars。应用侧当前盘口每侧最多
`max_levels_per_side` 档，超过上限明确停止，不静默丢弃增量使盘口不一致。
应用配置最多 1000 个市场，展示只输出前 `top_n` 条。终端刷新频率不改变计算所使用的行情时间戳。

初始数据和重连后必须等待 snapshot/clear 与完整 batch，才允许比较；Ondo 的 feed、market、metadata 状态分别处理。
Backpack 的连接 epoch、metadata、book continuity 和 freshness 会在处理更新与刷新时检查。
其他场所的底层重连/增量连续性依赖适配器，长时间断线恢复仍需针对实际候选做公共数据验证。
第一版原生节点异常或停止后退出，不自动无限重启；重启由外部进程管理器负责。

离线验证：

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\test_opportunity_core.py tests\test_opportunity_scan.py tests\test_opportunity_native.py tests\test_opportunity_universe.py tests\test_opportunity_connections.py
```

测试通过不表示百币容量、长期 soak 或真实成交已经验证。正式长跑应先查看各腿当前是否有有效盘口，
再按市场数量逐步扩大并测量进程资源占用。

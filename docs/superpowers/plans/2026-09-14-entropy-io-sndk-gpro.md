# Entropy io: SNDK/GPRO 只读接入 Implementation Plan

> **For agentic workers:** 使用 `superpowers:executing-plans` 逐项实施，使用 `- [ ]` 跟踪。用户会把本文件交给另一个模型；本轮只生成计划，不实施。不要因为看见旧 `PROMPT.md` 就重新从阶段 0 建仓库或推进交易阶段。

**Goal:** 在现有 `spread_watch.py` 中通过一个 Hyperliquid DataClient 同时观察 `xyz:` 与 Entropy `io:` 市场，首批支持 SNDK/GPRO 与 Aster 的只读比较。

**Architecture:** 新增逻辑行情腿 `ENTROPY`，复用 `_hyperliquid_client`；真实 Nautilus venue 和 ClientId 仍为 `HYPERLIQUID`。客户端按 ClientId 合并，价格、资金费、成交、CSV 标签按完整 instrument ID 和逻辑腿分别保存。首期不修改 Rust adapter。

**Tech Stack:** Windows PowerShell、仓库 `.venv` 的 Python 3.12、已安装 `nautilus_trader==2.0.0rc4` 本地 fork wheel、现有 pytest/unittest；不增加依赖。

**Spec:** 本文件第 1–4 节是本次需求和设计真源；第 5 节是实施任务；第 6 节是验收；第 7 节是直接交给实施模型的指令。

## 1. 范围与硬约束

- 只扩展阶段 1 的公开行情：quotes、L2、public trades、funding 与已有 CSV 分析。
- 首批 `io:SNDK × SNDKUSD1`、`io:GPRO × GPROUSD1`；支持在同一进程同时观察 SNDK 的 `xyz:`、`io:`、Aster 三条腿。
- 保留旧 `HL` 的含义：股票仍是 `xyz:`，加密仍是 HL 主 DEX。`ENTROPY` 是显式选项，不加入 `DEFAULT_VENUES` 或 `DEFAULT_PAIR`。
- 不创建 Entropy REST/WS/签名适配器；不复制 Hyperliquid adapter；不新增 `ENTROPY` Nautilus venue；不将同一 ClientId 注册两次。
- 不修改 `src/exec_probe.py`、`src/maker_live.py`、`src/live_limits.py`、`config/limits.toml`、`.env`、部署配置或 `E:\nautilus_trader`。不实现下单、转账、隔离保证金设置、账户抽象切换或返佣领取。
- 不接入 Ondo/Polymarket；不增加 OAI/ANTH/NBIS/IONQ 的跨平台映射。本任务也不重写历史资金费抓取程序。
- 不安装或编译新 wheel。若现有 wheel 在稳定网络下确实不支持必要接口，保留最小复现并报告具体阻塞，不自行扩大成 Rust 开发项目。
- 中文说明；代码、注释用英文。保留已有未跟踪研究报告和 CSV，不执行 `git clean`、全量 `git add .` 或覆盖其他人的修改。提交、合并按实施窗口的用户指令处理。
- 新增代码的离线测试禁止联网、禁止启动真正 LiveNode、禁止加载密钥。运行测试必须使用 `.venv\Scripts\python.exe`，不得误用 conda base。

### 完成的含义

完成表示映射、共享客户端、事件隔离、原有输出兼容和逐腿行情覆盖已经测试；公开 smoke 结果单独说明。它不表示有可交易套利、已支持 Entropy 实盘、已验证账号或已获得返佣。

本计划不新增 VWAP 引擎。现有 depth CSV 是 2/5/10 bp 深度容量汇总，不能从中准确重建任意数量的逐档成交价。后续若要求 $100/$1,000 可执行价差，需另行加入完整档位和同数量 VWAP；不能把本次 BBO 输出冒充那项验收。

## 2. 已核实的基线和证据

核查日期：2026-09-14。主仓库分支 `main`，HEAD `c33c0a74dbae29744206407fce1cb07bdbe1ddcb`。只读查看的 fork HEAD 为 `047d494e3c260bc6f701aa8f9193d1fd36f8da89`；源码 HEAD 不等于已安装二进制的构建证明。

证据目录：[`reports/entropy-plan-2026-09-14`](../../../reports/entropy-plan-2026-09-14/README.md)。其中保存公开 API 响应、两次 wheel 检查和约 8 秒原始 WebSocket 消息。

| 项目 | 本次核实结果 | 实施含义 |
|---|---|---|
| wheel 导入 | `2.0.0rc4`，有 `HyperliquidHttpClient` | 使用已有 wheel |
| wheel 元数据 | 首次返回 0；第二次加载 519 个，包含下表三个 instrument | 不能以“调用没抛异常”判断加载成功 |
| wheel 网络 | 第二次检查没有配置 HTTP proxy | 两次结果差异原因未定位；不能说代理修复了问题 |
| raw WS | `io:SNDK`、`io:GPRO`、`xyz:SNDK` 各收到 bbo/l2Book/activeAssetCtx/trades | 协议支持已核实；尚未验证改造后的 LiveNode |
| Entropy 当前目录 | 10 个注册、6 个未标记 delisted；DRAM 也是 delisted | 9 月 11 日的“9 个注册”已过期 |
| Aster | `SNDKUSD1`、`GPROUSD1` 为 `TRADING` / `PERPETUAL`，quote/margin 均 USD1 | 不拼成 GPROUSDT，不混用 SNDKUSDT |
| funding | Entropy 使用小时小数费率；Aster 两个 USD1 合约当前 8 小时 | 不能把 HL 原始 funding 再乘部署者 multiplier |
| 现有离线基线 | `tests/test_spread_watch.py tests/test_ref_feed.py`：55 passed | 实施后需原有用例继续通过并运行全套测试 |

### 2.1 精确标识

| 逻辑 symbol / leg | Nautilus InstrumentId | 原始市场名 | ClientId | 数量步长 | quote / settlement |
|---|---|---|---|---|---|
| SNDK / HL | `xyz:SNDK-USD-PERP.HYPERLIQUID` | `xyz:SNDK` | `HYPERLIQUID` | 0.001 | USD / USDC |
| SNDK / ENTROPY | `io:SNDK-USD-PERP.HYPERLIQUID` | `io:SNDK` | `HYPERLIQUID` | 0.0001 | USD / USDC |
| GPRO / ENTROPY | `io:GPRO-USD-PERP.HYPERLIQUID` | `io:GPRO` | `HYPERLIQUID` | 0.1 | USD / USDC |
| SNDK / ASTER | `SNDKUSD1-PERP.ASTER` | `SNDKUSD1` | `ASTER` | 0.01，公开 LOT_SIZE | USD1 / USD1 |
| GPRO / ASTER | `GPROUSD1-PERP.ASTER` | `GPROUSD1` | `ASTER` | 0.01，公开 LOT_SIZE | USD1 / USD1 |

前三行来自当前 wheel 的真实加载结果；Aster 原始市场来自公开 exchangeInfo，Nautilus 命名沿用现有 Aster 规则，须在 smoke 中检查 cache 实际加载成功。Entropy 的 `base_currency` 也带 `io:`；不能按 base_currency 字符串相等推断它与 Aster 合约可对冲。

前三个 HL instrument 的 multiplier 均为 1、非 inverse。io:SNDK/io:GPRO 的 wheel `price_precision` 分别是 2/5；这不是可用于所有价格的完整下单有效位数规则。本次不做订单定价或舍入，不硬编码 tick/lot。

### 2.2 动态字段和边界

- 当前 `io` 在完整 `perpDexs` 数组的索引为 10，SNDK/GPRO 在原始 `meta.universe` 的索引为 2/6。当前 action asset ID 因而是 200002/200006。这只用于核对，不写入应用代码。
- HIP-3 action asset ID 公式是 `100000 + perp_dex_index * 10000 + index_in_meta`。计算前不得过滤 delisted 项，否则 GPRO 前面 EWY 的位置消失会导致索引错误。应用不自己算 action ID，继续交给 adapter 动态发现。[官方说明](https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/asset-ids)
- 当前两个 io 合约均 `onlyIsolated=true`、`marginMode=strictIsolated`、`growthMode=enabled`、`deployerFeeScale="1.0"`、collateralToken=0。已安装 wheel 的 `instrument.info` 是空字典，不能依赖它读取上述字段；需要时读取公开 meta。
- Entropy 在这组条件下 Tier-0 taker 为 `0.045% × 2 × 0.1 = 0.009% = 0.9 bp`；Aster RWA 的公开 taker 也为 0.009%。这两项都是观察用基准，不是本次签名查得的用户佣金率；不计返佣、staking 或平台代币折扣。[Entropy 费用](https://docs.entropy.io/equity-perp-mechanics/fees)、[Aster RWA 费用](https://docs.asterdex.com/trading/perpetuals/fees-and-specs/fees)
- 当前 Entropy SNDK/GPRO 的 `assetToFundingMultiplier` 分别为 0.125/0.5；不是合约数量乘数，也不要再次乘到 `activeAssetCtx.funding` 上。存入 CSV 的仍是 adapter 给出的原始小时小数费率。
- Entropy USDC、Aster USD1、参考股票之间的币种基差、oracle、交易时段和公司行动差异未完成等价性验收；BBO 比较采用名义 1:1 美元口径，仅供观察。[Entropy 股权市场目录](https://docs.entropy.io/asset-directory/equity-assets)

## 3. 设计取舍：只做这一种实现

采用“逻辑腿分开、数据连接合并”。不采用新 Entropy adapter，也不把全局 `xyz:` 替换为 `io:`，更不创建两个名为 `HYPERLIQUID` 的客户端。

```text
SNDK/HL      -> xyz:SNDK-USD-PERP.HYPERLIQUID --+
SNDK/ENTROPY -> io:SNDK-USD-PERP.HYPERLIQUID  --+--> HYPERLIQUID DataClient（一个）
GPRO/ENTROPY -> io:GPRO-USD-PERP.HYPERLIQUID  --+
SNDK/ASTER   -> SNDKUSD1-PERP.ASTER          --+
GPRO/ASTER   -> GPROUSD1-PERP.ASTER          --+--> ASTER DataClient（一个）
```

`venue_key` 是 CLI/CSV 用的逻辑市场标签；`VenueSpec.venue` 和 `LegSpec.client_id` 是真实连接路由。两者不再假定一一对应。

现有 `_by_id` 已用完整 `InstrumentId` 路由事件，`_pairs` 已枚举不同 LegState 的全部有向组合，不需要重写。只要不要删除前缀，SNDK 三条腿自然产生 6 个方向，GPRO 两条腿自然产生 2 个方向。HL/ENTROPY 之间属于同一平台不同 builder 市场，报告中不要称它们是独立交易所风险分散。

首期 GPRO 仅映射 ENTROPY/ASTER；没有为其他 venue 建立受验收的 GPRO 映射，沿用“未映射腿跳过，剩余不足两腿时报错”。这不宣称其他平台一定未上市 GPRO。

以后增加另一个已核实的 io 股票，只需为对应 symbol 增加 ENTROPY 完整 instrument 映射、已经核对的另一条观察腿和对应测试；共享 Hyperliquid 客户端、事件路由和 CSV 逻辑无需再改。新币种或新合约定义必须先核查，不能对 Entropy 全部市场按 ticker 自动建立跨平台对冲映射。

### 3.1 保留的输出语义

- 原 CSV 列名及顺序不变；Entropy 的 `venue`、`sell_venue`、`buy_venue` 写 `ENTROPY`，不能写成 `HL`。
- SNDK 原有 HL/ASTER/LIGHTER/LIGHTER_RH 映射不变；新增 ENTROPY 是增量。
- `net_bps = gross_bps - sell_taker_bps - buy_taker_bps - RESERVE_BPS`。io/Aster 当前阈值是 0.9+0.9+5=6.8 bp，仍只扣建仓费和风险预留；不能称为完整开平仓净利润。仅四笔 taker 手续费基准约 3.6 bp，但真实退出价、funding、滑点和币种基差另算。
- 当前 `age_*_ms` 来自 `ts_init` 接收时间，尚非两腿 exchange-event 时间同步保证。保持旧字段含义，报告写明限制；本次不顺带重写时间门控。
- 零成交、零正价差、funding 尚未到达分别照实报告；funding 的 `None` 保留为空，不能伪造为 0。没有正价差不是连接失败。
- 多阶段验收分开：元数据存在、raw WS 能读、adapter/LiveNode 能订阅、逐腿 CSV 有数据、交易执行可用是不同结论。

## 4. 文件清单

| 文件 | 动作与责任 |
|---|---|
| `src/spread_watch.py` | 增加逻辑腿及映射；按 ClientId 合并注册；dry-run；逐腿完成判定；summary 显示 funding_seen |
| `src/analysis/opportunities.py` | 显式登记 ENTROPY funding 单位及 GPRO/Aster 周期；不重写分析器 |
| `tests/test_spread_watch.py` | 用现有 fake fixtures 测映射、共享注册、事件隔离、dry-run、缺腿失败 |
| `tests/test_opportunities.py`（新增） | funding 换算、费用和 Entropy 文件发现兼容性 |
| `docs/entropy-readonly.md`（新增） | 使用命令、费率前提、逐腿验收表与结果边界 |

其他文件默认不改。优先把小型 helper 放在原文件，不为了两种市场创建插件框架、市场注册数据库、守护进程或新配置系统。

## 5. 实施任务

### Task 0：记录当前环境和前置结果

**文件：** 读取 `CLAUDE.md`、本计划、上表涉及的源文件；运行输出写本次独立 evidence 目录，不覆盖本计划的 9 月 14 日证据。

- [ ] 在 `E:\Nautilus-Perps` 查看 `git status --short` 和 `git rev-parse HEAD`。HEAD 不同不自动失败，但先核对本计划引用的函数仍存在；保留用户修改。
- [ ] 运行下面的基线，失败时先区分解释器访问、wheel 导入、旧测试失败；不要安装依赖掩盖原因。

```powershell
.\.venv\Scripts\python.exe -c "import sys, nautilus_trader; print(sys.executable); print(nautilus_trader.__version__)"
.\.venv\Scripts\python.exe -m pytest tests/test_spread_watch.py tests/test_ref_feed.py -q -p no:cacheprovider
```

本次基线为 55 passed。沙箱曾无法访问 venv 指向的 uv Python，报 `No Python at ...`；同一命令在获准访问后成功。它不证明 venv 已损坏，不要先重建 venv。

- [ ] 记录生产源码 SHA/本次基线结果；先继续离线实现。公开 metadata 复查放在 Task 5 的 smoke 前，不把网络接入离线单元测试。

### Task 1：新增映射并合并客户端注册

**Modify:** `src/spread_watch.py` 的费用常量、`VENUES`、`INSTRUMENTS`、`build_node`（当前约 66、237、311、1173 行）。

**Test:** `tests/test_spread_watch.py`。

**Interfaces:** 保留 `build_plan(symbols, venue_keys)` 和 LegSpec 字段顺序；新增下方 `ClientGroup` 与 `build_client_groups(plan)`。

- [ ] 先加失败用例：SNDK 的 HL 和 ENTROPY instrument 不同，但 client_id 相同；GPRO 只返回 ENTROPY/ASTER；默认 venues 不变；HL+ENTROPY+ASTER 只注册两个客户端；Aster `load_ids` 同时包含 SNDKUSD1、GPROUSD1。
- [ ] 运行这些测试，确认在生产修改前失败于缺少 ENTROPY 或 helper。
- [ ] 添加以下定义；ENTROPY 费用用独立常量，以免未来 xyz 费率变动误改 io。

```python
ENTROPY_TAKER_FEE_BPS = 0.9  # Tier-0, growth enabled, scale=1; verified 2026-09-14.

# Add immediately after VENUES; reuse the exact same callable.
VENUES["ENTROPY"] = VenueSpec("ENTROPY", "HYPERLIQUID", _hyperliquid_client)

# Append ENTROPY to ALL_VENUES; do not change DEFAULT_VENUES or DEFAULT_PAIR.
ALL_VENUES = ("HL", "LIGHTER", "LIGHTER_RH", "ASTER", "ENTROPY")

# Add after the original INSTRUMENTS construction.
INSTRUMENTS["SNDK"]["ENTROPY"] = (
    "io:SNDK-USD-PERP.HYPERLIQUID", ENTROPY_TAKER_FEE_BPS,
)
INSTRUMENTS["GPRO"] = {
    "ENTROPY": ("io:GPRO-USD-PERP.HYPERLIQUID", ENTROPY_TAKER_FEE_BPS),
    "ASTER": ("GPROUSD1-PERP.ASTER", ASTER_TAKER_FEE_BPS),
}
```

费用常量放费用区；VENUES 赋值放原字典之后；INSTRUMENTS 赋值放原 symbol 表之后。不要把这三组贴到各自定义之前。

- [ ] 在 NodePlan 前加入以下 helper；dict 的插入顺序就是稳定注册顺序。不按 factory 名分组，Lighter 与 LIGHTER_RH 虽共享 factory 但 ClientId 必须保持不同。

```python
@dataclass
class ClientGroup:
    client_id: str
    venue_spec: VenueSpec
    instrument_ids: list[str]


def build_client_groups(plan: dict[str, list[LegSpec]]) -> list[ClientGroup]:
    groups: dict[str, ClientGroup] = {}
    for symbol, legs in plan.items():
        seen: set[str] = set()
        for leg in legs:
            if leg.instrument_id in seen:
                raise ValueError(f"{symbol}: duplicate instrument {leg.instrument_id}")
            seen.add(leg.instrument_id)
            spec = VENUES[leg.venue_key]
            venue = str(InstrumentId.from_str(leg.instrument_id).venue)
            if leg.client_id != spec.venue or venue != spec.venue:
                raise ValueError(f"{symbol}/{leg.venue_key}: inconsistent client route")
            group = groups.get(leg.client_id)
            if group is None:
                groups[leg.client_id] = ClientGroup(leg.client_id, spec, [])
                group = groups[leg.client_id]
            elif (group.venue_spec.venue != spec.venue
                  or group.venue_spec.build_client is not spec.build_client):
                raise ValueError(f"conflicting configuration for {leg.client_id}")
            if leg.instrument_id not in group.instrument_ids:
                group.instrument_ids.append(leg.instrument_id)
    return list(groups.values())
```

- [ ] 把 `build_node` 里整个 `ids_by_venue` 收集和注册循环替换为下面代码，删除旧循环。其他 node/strategy 构造保持原样。

```python
for group in build_client_groups(np.plan):
    factory, client_config = group.venue_spec.build_client(group.instrument_ids)
    builder = builder.add_data_client(group.client_id, factory, client_config)
```

- [ ] 至少加入这个真实边界测试，并补上现有默认映射保持不变的断言。

```python
def test_entropy_and_xyz_share_one_client_group():
    plan = spread_watch.build_plan(["SNDK", "GPRO"], ["HL", "ENTROPY", "ASTER"])
    groups = {g.client_id: g for g in spread_watch.build_client_groups(plan)}
    assert set(groups) == {"HYPERLIQUID", "ASTER"}
    assert groups["HYPERLIQUID"].instrument_ids == [
        "xyz:SNDK-USD-PERP.HYPERLIQUID",
        "io:SNDK-USD-PERP.HYPERLIQUID",
        "io:GPRO-USD-PERP.HYPERLIQUID",
    ]
    assert groups["ASTER"].instrument_ids == [
        "SNDKUSD1-PERP.ASTER", "GPROUSD1-PERP.ASTER",
    ]
    assert len(plan["SNDK"]) == 3
    assert len(plan["GPRO"]) == 2
    assert spread_watch.DEFAULT_VENUES == ("HL", "LIGHTER", "ASTER")
    assert spread_watch.DEFAULT_PAIR == ("HL", "LIGHTER")
```

- [ ] 对 `build_node` 本身加入以下测试，不能只测 helper；VENUES 已保存 callable，单独 patch 模块函数名字不会替换字典中的 callable。

```python
def test_build_node_registers_shared_hyperliquid_once(tmp_path, monkeypatch):
    from dataclasses import replace
    from unittest.mock import MagicMock
    loaded = {}
    def hl_client(ids):
        loaded["HL"] = list(ids)
        return object(), object()
    def aster_client(ids):
        loaded["ASTER"] = list(ids)
        return object(), object()
    for key in ("HL", "ENTROPY"):
        monkeypatch.setitem(spread_watch.VENUES, key,
                            replace(spread_watch.VENUES[key], build_client=hl_client))
    monkeypatch.setitem(spread_watch.VENUES, "ASTER",
                        replace(spread_watch.VENUES["ASTER"], build_client=aster_client))
    live = MagicMock()
    builder = live.builder.return_value
    for method in ("with_logging", "with_timeout_connection",
                   "with_delay_post_stop_secs", "add_data_client"):
        getattr(builder, method).return_value = builder
    monkeypatch.setattr(spread_watch, "LiveNode", live)
    plan = spread_watch.build_plan(["SNDK", "GPRO"], ["HL", "ENTROPY", "ASTER"])
    np = spread_watch.NodePlan(plan, tmp_path, "20260914T000000Z", 10, 1000)
    node, strategies, state = spread_watch.build_node(np)
    assert [c.args[0] for c in builder.add_data_client.call_args_list] == ["HYPERLIQUID", "ASTER"]
    assert loaded["ASTER"] == ["SNDKUSD1-PERP.ASTER", "GPROUSD1-PERP.ASTER"]
    assert len(loaded["HL"]) == 3
    assert len(strategies) == 2
    assert node.add_strategy.call_count == 2
    node.run.assert_not_called()
```
- [ ] `SNDK` 同时选择 `HL,ENTROPY,LIGHTER,LIGHTER_RH,ASTER` 时应有 5 条腿、4 个真实客户端。加入这项测试，防止误合并两个 Lighter deployment。
- [ ] 在 `VenueSpec` 注释和脚本顶部 usage 中说明逻辑腿/客户端区别，运行 `tests/test_spread_watch.py`。

### Task 2：增加可离线核对的 dry-run

**Modify:** `src/spread_watch.py::main`；新增 `describe_plan` helper；import `json`。

**Interfaces:** `describe_plan(plan: dict[str, list[LegSpec]]) -> dict[str, object]` 返回以下结构，不调用 build_client。

- [ ] 测试 `--symbols SNDK,GPRO --venues HL,ENTROPY,ASTER --dry-run` 会输出可解析 JSON；没有 dotenv、网络、LiveNode 或文件创建动作。
- [ ] 添加下面的 helper，并在 argparse 增加 `--dry-run`，`action="store_true"`。

```python
def describe_plan(plan: dict[str, list[LegSpec]]) -> dict[str, object]:
    return {
        "mode": "read-only",
        "market_availability_checked": False,
        "symbols": {
            symbol: [
                {"venue_key": leg.venue_key, "instrument_id": leg.instrument_id,
                 "client_id": leg.client_id, "taker_fee_bps": leg.taker_fee_bps}
                for leg in legs
            ]
            for symbol, legs in plan.items()
        },
        "data_clients": [
            {"client_id": group.client_id, "instrument_ids": group.instrument_ids}
            for group in build_client_groups(plan)
        ],
    }
```

- [ ] 在 `plan = build_plan(symbols, venue_keys)` 紧接着插入，必须在 `load_dotenv()` 和 `build_node()` 之前。

```python
if args.dry_run:
    print(json.dumps(describe_plan(plan), indent=2))
    return
```

- [ ] 处理当前 `build_plan` 的跳过消息：把 `no instrument mapped` 那条 `print` 定向到 `sys.stderr`，保证 stdout 是纯 JSON。调整依赖 stdout 的相关现有用例为捕获 stderr；不要把错误信息吞掉。
- [ ] 用下面测试约束副作用和 GPRO 跳过 HL 的行为。

```python
def test_dry_run_is_json_and_never_builds_node():
    from unittest.mock import patch
    out, err = io.StringIO(), io.StringIO()
    args = ["spread_watch.py", "--symbols", "SNDK,GPRO",
            "--venues", "HL,ENTROPY,ASTER", "--dry-run"]
    with patch.object(sys, "argv", args), patch.object(spread_watch, "build_node") as build:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            spread_watch.main()
    build.assert_not_called()
    result = json.loads(out.getvalue())
    assert result["mode"] == "read-only"
    assert result["market_availability_checked"] is False
    assert [x["client_id"] for x in result["data_clients"]] == ["HYPERLIQUID", "ASTER"]
    assert "GPRO" in err.getvalue()
```

测试文件需补 import `json`。另外 patch `dotenv.load_dotenv` 并断言未调用；在测试里把各 VENUES build_client 改为一旦调用就抛 AssertionError 的 fake，以防将来 dry-run 间接创建客户端。

### Task 3：验证事件隔离和逐腿完整性，杜绝“只收到 Aster 就成功”

**Modify:** `SpreadWatch`、`main` 收尾、`RunState` 的启动错误传播。

**Test:** 使用 `tests/test_spread_watch.py` 现有 `WatchUnderTest`、`build_watch`、`make_trade`、`read_csv`。

**Interfaces:** 新增 `SpreadWatch.received_leg_keys() -> set[tuple[str, str, str]]` 和 `missing_leg_keys(plan, received)`；tuple 固定为 `(symbol, venue_key, instrument_id)`。

- [ ] 用三腿 SNDK 构造 watcher，分别注入 xyz/io/ASTER 的 QuoteTick、TradeTick、FundingRateUpdate，验证只改变匹配的 LegState。用例不得仅断言三条 instrument 字符串不同。
- [ ] 示例：下面成交用例应写入 3 行不同 venue 标签；额外注入 `io:GPRO` 到 SNDK watcher，CSV 行数不得增加。

```python
def test_xyz_and_io_trades_keep_distinct_labels(tmp_path):
    watch = build_watch(tmp_path, "SNDK", ("HL", "ENTROPY", "ASTER"))
    watch.on_start()
    try:
        for n, leg in enumerate(watch._legs):
            watch.on_trade(make_trade(leg.instrument_id, "100.00", "1.0",
                                     AggressorSide.BUY, f"T-{n}", 1_000 + n))
        other = InstrumentId.from_str("io:GPRO-USD-PERP.HYPERLIQUID")
        watch.on_trade(make_trade(other, "1.00", "1.0", AggressorSide.BUY, "OTHER", 2_000))
    finally:
        watch.on_stop()
    rows = read_csv(next(tmp_path.glob("trades_*.csv")))
    assert [row[1] for row in rows[1:]] == ["HL", "ENTROPY", "ASTER"]
```

- [ ] 把 `WatchUnderTest` 各 subscribe stub 扩展为额外记录 `(kind, instrument_id, client_id)`，保留原 `subscribed` 以免破坏旧测试。assert io 与 xyz 的 quotes/funding/trades/deltas 都通过 `HYPERLIQUID`，Aster 通过 `ASTER`；继续禁止为 Aster 订阅 depth10。
- [ ] 按以下实测构造器签名增加报价和资金费隔离用例。QuoteTick 的 `ts_event`、`ts_init` 用 fake clock 当前 ns，避免新数据被 MAX_AGE_MS 拒绝；io 的报价更新不修改 xyz 的价格或计数。FundingRateUpdate 的 rate 为 Decimal，interval 单位为分钟。

```python
def test_entropy_quote_and_funding_do_not_change_xyz(tmp_path):
    from decimal import Decimal
    from nautilus_trader.model import QuoteTick, FundingRateUpdate
    watch = build_watch(tmp_path, "SNDK", ("HL", "ENTROPY", "ASTER"))
    watch.on_start()
    try:
        now = watch.clock.timestamp_ns()
        xyz, entropy, aster = watch._legs
        watch.on_quote(QuoteTick(
            instrument_id=entropy.instrument_id,
            bid_price=Price.from_str("100.00"), ask_price=Price.from_str("100.01"),
            bid_size=Quantity.from_str("1.0000"), ask_size=Quantity.from_str("2.0000"),
            ts_event=now, ts_init=now,
        ))
        assert entropy.bid == 100.0 and entropy.updates == 1
        assert xyz.updates == 0 and aster.updates == 0
        for leg, rate in ((xyz, "0.0001"), (entropy, "-0.0002"), (aster, "0.0008")):
            watch.on_funding_rate(FundingRateUpdate(
                instrument_id=leg.instrument_id, rate=Decimal(rate),
                ts_event=now, ts_init=now, interval=480 if leg is aster else 60,
            ))
        assert xyz.funding == 0.0001
        assert entropy.funding == -0.0002
        assert aster.funding == 0.0008
        assert len(watch._pairs) == 6
    finally:
        watch.on_stop()
```
- [ ] 每条腿报价不足时不能算本次 watch 完整。加入下方生产代码；旧 `received()` 可保留以兼容，但 main 不再用 `any` 作为最终成功条件。

```python
# Method inside SpreadWatch:
def received_leg_keys(self) -> set[tuple[str, str, str]]:
    return {
        (self.symbol, leg.spec.venue_key, leg.spec.instrument_id)
        for leg in self._legs if leg.updates > 0
    }


# Module-level helper:
def missing_leg_keys(
    plan: dict[str, list[LegSpec]],
    received: set[tuple[str, str, str]],
) -> list[tuple[str, str, str]]:
    return sorted({
        (symbol, leg.venue_key, leg.instrument_id)
        for symbol, legs in plan.items() for leg in legs
    } - received)
```

- [ ] 在 main 循环之前定义 `received_legs: set[tuple[str, str, str]] = set()`，代替 `ever_received`。每次 node 停止后对所有 strategy 执行 `received_legs.update(strategy.received_leg_keys())`，然后输出 summary。集合保留到重建之后，不要求所有数据在最后一个 node 中出现。
- [ ] main 结束时用下方检查替代旧 `if not ever_received`；映射时合法跳过的腿不在 plan 中，因此不算缺失。

```python
missing = missing_leg_keys(plan, received_legs)
if missing:
    for symbol, key, instrument_id in missing:
        print(f"[stage1] INCOMPLETE {symbol}/{key}: no top-of-book data for {instrument_id}",
              file=sys.stderr, flush=True)
    raise SystemExit(1)
```

- [ ] 测试反例：只注入两个 ASTER 的报价，missing 必须包含 SNDK/ENTROPY、GPRO/ENTROPY、SNDK/HL；后来补上两次 node 的完整集合后 missing 才为空。重复消息不得改变覆盖集；真实零 funding 不影响报价覆盖。
- [ ] 如果 `on_start` 发现目标 instrument 缺失，保留包含 instrument 和候选项的现有日志，同时经 RunState 记录 fatal startup error。元数据加载失败也可能造成 instrument 缺失，日志不应断言“市场已下架”。本次进程停止并保留证据，不自动反复重建。

```python
# Add inside RunState.__init__:
self.startup_errors: list[str] = []

# Add method to RunState:
def fail_startup(self, message: str) -> None:
    if message not in self.startup_errors:
        self.startup_errors.append(message)
    if self.stop_node is not None and not self.stop_requested:
        self.stop_requested = True
        self.stop_node()

# In SpreadWatch.on_start, inside the existing missing-instrument branch,
# immediately before self.stop():
self._cfg.run_state.fail_startup(
    f"{self.symbol}/{leg.spec.venue_key}: instrument not loaded: {leg.spec.instrument_id}",
)

# In main, after per-strategy summaries, before restart/interruption handling:
if run_state.startup_errors:
    for message in run_state.startup_errors:
        print(f"[stage1] STARTUP FAILED {message}", file=sys.stderr, flush=True)
    raise SystemExit(1)
```

先前 `node.run()` 的 finally 已取消 timer，此检查不绕过 timer 清理。测试 fake `stop_node` 验证重复失败调用只请求 stop 一次；在缺失 cache instrument 的 fake watcher 中断言错误被记录，并通过 mock main 的 node 运行验证最终 SystemExit.code==1。
- [ ] summary 每条腿末尾增加 `funding_seen={leg.funding is not None}`，现有计数、CSV header 不改。它只证明有 funding 更新，不证明已发生真实资金费支付。

覆盖集仅用于判定“这次运行每条腿至少收到报价”。持续在线、断流恢复、盘口新鲜度由日志和独立观察验收，不能用覆盖集证明。

### Task 4：登记 funding 口径并验证旧分析器能读新标签

**Modify:** `src/analysis/opportunities.py` 约 57–64 行；`spread_watch.py` 两处写死“Aster every 8 hours”的注释改为“per instrument”，不改变原始数据存法。

**Create:** `tests/test_opportunities.py`。

- [ ] 测试 ENTROPY 小时小数费率 `0.0001` 转成 1 bp/h；Aster GPRO 的每 8h 原始 `0.0008` 转成 1 bp/h。正/负两种费率和 0 都测。
- [ ] 在现有常量中显式加：`FUNDING_SCALE["ENTROPY"] = 1e4`、`FUNDING_HOURS["ENTROPY"] = 1.0`、`ASTER_FUNDING_HOURS["GPRO"] = 8`。推荐直接扩展字典字面量。即使旧 `.get` 默认值恰好数值相同，也要登记并测试字典归属，避免支持依赖兜底猜测。

```python
# tests/test_opportunities.py imports:
import sys
from pathlib import Path
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from analysis import opportunities


@pytest.mark.parametrize("raw,expected", [(0.0001, 1.0), (-0.0001, -1.0), (0.0, 0.0)])
def test_entropy_funding_is_an_hourly_fraction(raw, expected):
    assert opportunities.FUNDING_SCALE["ENTROPY"] == 1e4
    assert opportunities.FUNDING_HOURS["ENTROPY"] == 1.0
    assert opportunities.hourly_bps(raw, "ENTROPY", "GPRO") == pytest.approx(expected)


def test_gpro_aster_period_and_pair_fees_are_explicit():
    assert opportunities.ASTER_FUNDING_HOURS["GPRO"] == 8
    assert opportunities.hourly_bps(0.0008, "ASTER", "GPRO") == pytest.approx(1.0)
    fees, note = opportunities.venue_fees("GPRO")
    assert not note
    assert fees["ENTROPY"] == pytest.approx(0.9)
    assert fees["ASTER"] == pytest.approx(0.9)
```

- [ ] 用 fake watcher 实际生成的 `SNDK_HL-ENTROPY-ASTER`、`GPRO_ENTROPY-ASTER` CSV 测试现有入口：`discover(directory: Path, stamp: str) -> list[SymbolFiles]`、`load_all(path: Path, t_from: float | None, t_to: float | None) -> AllData`、`analyse(files: SymbolFiles, args) -> list[str]`。此整合用例放在 `tests/test_spread_watch.py`，直接复用同文件 fixtures。用 `types.SimpleNamespace(t_from=None, t_to=None, gap_s=2.0, hold_s=30.0, min_usd=1000.0)` 作为 analyse 的 args。按上述 QuoteTick 模式给每腿注入一次当前报价，使 `_all.csv` 有真实策略生成的行；停止后发现文件并调用分析，断言发现 SNDK/GPRO、内容出现 ENTROPY，`venue_fees` 的两腿加 reserve 为 6.8 bp。文件名继续使用现有按第一个下划线 split 的规则；未知标签不能视为免费。
- [ ] 不修改 `funding_history.py` 的采集 venue 列表，也不声称历史 funding 下载已支持 ENTROPY；在 runbook 标出这条范围边界。

### Task 5：文档、公开 preflight 和有限 smoke

**Create:** `docs/entropy-readonly.md`。

**Outputs:** 新建 `reports/entropy-io-acceptance/<UTC-run-id>/` 保存本次日志、metadata 和 CSV，不覆盖任何旧报告。

- [ ] runbook 写明第 1–3 节约束、下面命令、预期客户端/腿数、收费和时间字段前提，以及缺腿时如何判定失败。
- [ ] 完成所有离线测试和 dry-run 后，执行以下公开 preflight。读取全部 `perpDexs` 和原始 io universe 后再取索引；保存响应和 UTC 时间；如获取失败可重试一次并保留两次结果，不回退使用旧快照冒充新结果。

请求表（均不需要账户）：

| URL | 请求 | 必须检查 |
|---|---|---|
| `https://api.hyperliquid.xyz/info` | POST `{"type":"metaAndAssetCtxs","dex":"io"}` | io:SNDK/io:GPRO 存在且未 delisted；collateralToken=0；growth enabled，scale=1 |
| 同上 | POST `{"type":"perpDexs"}` | 名为 io 的完整数组索引；原始 funding multiplier 留存 |
| `https://fapi.asterdex.com/fapi/v1/exchangeInfo` | GET | SNDKUSD1/GPROUSD1 为 TRADING/PERPETUAL；quote/margin 为 USD1；保存 filters |
| `https://fapi.asterdex.com/fapi/v1/fundingInfo` | GET | 这两个 USD1 合约的 fundingIntervalHours=8 |

若费用、结算币种、状态、funding 周期或目标市场名与本计划不一致，离线实现可以完成，但不要运行带旧成本假设的采集并宣称其结果正确；记录具体变化，更新有证据支持的费率/周期及相应断言。若变化涉及合约定义或执行支持，报告边界并停在只读验收。

公开请求可复用下面的独立内联代码，不增加生产数据抓取模块，不加载 `.env`：

```powershell
@'
import datetime, json, pathlib, urllib.request
stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%SZ')
out = pathlib.Path('reports/entropy-io-acceptance') / stamp
out.mkdir(parents=True)
queries = [
    ('io-meta', 'https://api.hyperliquid.xyz/info', {'type':'metaAndAssetCtxs','dex':'io'}),
    ('dexs', 'https://api.hyperliquid.xyz/info', {'type':'perpDexs'}),
    ('aster-meta', 'https://fapi.asterdex.com/fapi/v1/exchangeInfo', None),
    ('aster-funding', 'https://fapi.asterdex.com/fapi/v1/fundingInfo', None),
]
failed = False
for name, url, body in queries:
    row = {'url':url, 'request':body,
           'requested_at_utc':datetime.datetime.now(datetime.timezone.utc).isoformat()}
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode() if body else None,
                                     headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req, timeout=20) as response:
            row['status'] = response.status
            row['response'] = json.load(response)
    except Exception as exc:
        row['error'] = repr(exc)
        failed = True
    row['finished_at_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    (out / (name + '.json')).write_text(json.dumps(row, indent=2) + '\n', encoding='utf-8')
print(out)
raise SystemExit(1 if failed else 0)
'@ | .\.venv\Scripts\python.exe -
```

运行成功后逐项检查表中字段，HTTP 200 本身不是 preflight 通过。随后运行下方 wheel 检查并保存输出；`include_perps_hip3` 默认 False，不能漏掉。空列表不是成功，不要调用 `from_env()` 或任何账户/订单方法。

```powershell
@'
import asyncio
from nautilus_trader.adapters.hyperliquid import HyperliquidHttpClient, HyperliquidEnvironment
async def main():
    client = HyperliquidHttpClient(environment=HyperliquidEnvironment.MAINNET, timeout_secs=20)
    items = await client.load_instrument_definitions(
        include_spot=False, include_perps=True, include_perps_hip3=True,
    )
    by_id = {str(item.id): item for item in items}
    required = {'io:SNDK-USD-PERP.HYPERLIQUID', 'io:GPRO-USD-PERP.HYPERLIQUID',
                'xyz:SNDK-USD-PERP.HYPERLIQUID'}
    print('loaded_count=', len(items))
    for instrument_id in sorted(required & by_id.keys()):
        inst = by_id[instrument_id]
        print(inst.id, inst.raw_symbol, inst.size_increment, inst.settlement_currency)
    missing = required - by_id.keys()
    if missing:
        raise SystemExit('Missing instruments: ' + ', '.join(sorted(missing)))
asyncio.run(main())
'@ | .\.venv\Scripts\python.exe -
```

- [ ] 先跑 dry-run，再跑一次 2 分钟公开行情 smoke；此 smoke 是实现窗口的任务，本次规划窗口不启动。命令如下，`$entropyRunDir` 为本次独立目录。

```powershell
.\.venv\Scripts\python.exe src/spread_watch.py --symbols SNDK,GPRO --venues HL,ENTROPY,ASTER --dry-run
$entropyStamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$entropyRunDir = Join-Path 'reports\entropy-io-acceptance' $entropyStamp
New-Item -ItemType Directory -Path $entropyRunDir -Force | Out-Null
.\.venv\Scripts\python.exe src/spread_watch.py --symbols SNDK,GPRO --venues HL,ENTROPY,ASTER --minutes 2 --max-restarts 0 --out $entropyRunDir *> (Join-Path $entropyRunDir 'watch.log')
$entropyExitCode = $LASTEXITCODE
Get-Content -LiteralPath (Join-Path $entropyRunDir 'watch.log') -Tail 100
Write-Output "watch_exit_code=$entropyExitCode"
```

- [ ] 逐行检查：SNDK/HL、SNDK/ENTROPY、SNDK/ASTER、GPRO/ENTROPY、GPRO/ASTER 五条腿 instrument OK、top-of-book>0、存在有有效双侧盘口的 depth 行；funding_seen 及 public trades 数量分别报告。没有交易发生时 trades=0 可报告，不能强造；funding 未到达则该通道仍未验收。
- [ ] CSV 文件名应分别包含 `SNDK_HL-ENTROPY-ASTER`、`GPRO_ENTROPY-ASTER`；`_all.csv` 中出现期望的方向和 ENTROPY 标签；正价差 hits 文件只有表头也允许。三腿 SNDK 总计可产生 6 个方向，双腿 GPRO 可产生 2 个；smoke 内未出现的方向照实列出，不能推算行数。
- [ ] 记录任何订阅错误、未知 instrument、重复客户端、TLS 错误或自动重连。raw WS 证据不替代 node smoke；node 有部分数据但异常退出也不算通过。
- [ ] 跑一次针对本次 stamp 的 `opportunities.py` 离线分析，命令由真实输出文件名提取 stamp，`--dir` 指本次 CSV 目录、`--stamp` 指 watcher 文件名时间、`--symbols SNDK,GPRO`、`--md` 指本目录下的报告。目录创建时间可能与 watcher stamp 不同，不要假定相同。
- [ ] 不自动启动 30 分钟、整日或 24–72 小时采集；完成 2 分钟 smoke 后报告并结束。后续若用户要求观察窗口，再安排覆盖美股常规盘中、funding 与断流恢复的采集。

## 6. 最终验收清单

### 6.1 必跑命令

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_spread_watch.py tests/test_opportunities.py tests/test_ref_feed.py -q -p no:cacheprovider
.\.venv\Scripts\python.exe -m pytest tests -q -p no:cacheprovider
.\.venv\Scripts\python.exe src/spread_watch.py --pair SNDK:ENTROPY-ASTER --dry-run
.\.venv\Scripts\python.exe src/spread_watch.py --symbols SNDK,GPRO --venues HL,ENTROPY,ASTER --dry-run
.\.venv\Scripts\python.exe src/spread_watch.py --pair NVDA:HL-LIGHTER --dry-run
git diff --check -- src/spread_watch.py src/analysis/opportunities.py tests/test_spread_watch.py tests/test_opportunities.py docs/entropy-readonly.md
```

新建且未跟踪文件不被普通 `git diff --check` 覆盖：额外用 `git diff --no-index --check -- /dev/null <新文件>` 检查，或用户授权暂存后检查 staged diff。`--no-index` 比较新文件时可能因存在差异返回 1；应检查是否输出具体 whitespace error，不能仅凭退出码 1 就认定有空白错误。不要为了检查擅自全量暂存。

### 6.2 必须通过的具体性质

- [ ] ENTROPY 只在显式选择时加入；所有旧默认、旧 CLI alias、NVDA/crypto/LIGHTER_RH 路径仍可用。
- [ ] 一个 HYPERLIQUID 客户端承载全部 xyz/io ID，Aster load_ids 是完整并集；Lighter 两个 deployment 不混并。
- [ ] io/xyz quote、funding、trade 路由互不串线；reference header 使用 ENTROPY 前缀；未启用 reference 不接入 FUTU。
- [ ] CSV schema 不变、同一 stamp 重启不写重复表头、每个 symbol 持有自己的输出；复用旧 CsvSink，不另造实现。
- [ ] 只收到任意一条腿不算成功；缺目标 instrument 及缺腿均有明确非零退出路径。
- [ ] 原始 funding 与费率单位明确，ENTROPY 和 GPRO/Aster 显式登记；费用包含 ENTROPY，不把它默认成 0。
- [ ] 至少一个 mock build_node 用例验证真实注册调用，而非只测辅助函数。
- [ ] 公共 smoke 与离线测试分别报告；没有正价差可以通过行情接入验收，不能通过套利收益验收。
- [ ] 无新 ExecClient、无签名、无账户操作、无部署、无长期后台进程、无依赖变更。

### 6.3 提交给用户的结果格式

1. 修改文件和核心行为，两三句话。
2. 离线测试命令、实际通过数量和失败情况。
3. 五条腿各自 quote/depth/trade/funding 状态、客户端数量、CSV 路径。
4. 网络或订阅失败、未验收项如实列出；接口通路完成但 smoke 失败时明确写“代码完成，公开运行验收未通过”。
5. 本次净价差只是 BBO 建仓观察，不是 VWAP、完整 PnL 或交易执行证明。

## 7. 直接交给实施模型的提示词

```text
在 E:\Nautilus-Perps 实施 docs/superpowers/plans/2026-09-14-entropy-io-sndk-gpro.md。
先读 CLAUDE.md 和计划第 1–4 节，再按 Task 0–5 顺序执行。
目标是阶段 1 只读支持 SNDK/GPRO 的 Entropy io:，复用现有 Hyperliquid adapter。
严格保留 HL=xyz/主 DEX；ENTROPY 是独立逻辑腿，但必须共享一个 HYPERLIQUID DataClient。
不要新建 Entropy adapter，不要改 Rust/fork，不要改交易、账户、部署或密钥相关文件。
先写和运行离线失败用例，再实现；测试真实 build_node 注册次数和事件不串线。
先完成离线测试及 dry-run；公开 metadata 条件匹配后只运行计划中的 2 分钟只读 smoke。
保留用户已有修改和所有原始失败证据。网络失败或某腿缺数据不允许写“全部通过”。
结束时按第 6.3 节汇报，不继续长期采集或推进交易阶段。不要 commit/merge，除非我另行要求。
```

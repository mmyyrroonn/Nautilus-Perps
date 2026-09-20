# Ondo Perps probe runbook（`ONDO`：public / account-readonly / production-readonly / paper / sandbox）

本文件是 Ondo Perps（venue 字符串与 ClientId 都是 `ONDO`）在应用侧的操作说明。实现计划与取舍见
[`docs/superpowers/plans/2026-09-15-ondo-perps-continuation.md`](superpowers/plans/2026-09-15-ondo-perps-continuation.md)，
缺陷来源见 [`reports/ondo-code-review-2026-09-15.md`](../reports/ondo-code-review-2026-09-15.md)，
各阶段验收报告在 `../reports/ondo-acceptance/` 下，与本文件直接相关的两份是
R2 行情/录制/回放（[`20260915T153817Z-r2`](../reports/ondo-acceptance/20260915T153817Z-r2/README.md)）
与 R3 私有生命周期/账户/DMS（[`20260916T030333Z-r3`](../reports/ondo-acceptance/20260916T030333Z-r3/README.md)）。

本路径只做四件事：公开行情预检、有限时公开采集与录制、已有 tape 的离线重放、以及四种受限的账户路径
（sandbox 只读 / production 只读 / 模拟 / sandbox 写入）。默认只读、默认有限时运行、没有默认下单参数。

**开篇两条必须先看，因为它们决定这份文档里哪些话不能说。**

1. **没有任何私有 / sandbox 协议验收。** 公开面已经被真实读取与录制，但**没有任何经过鉴权的私有请求被 host
   证实**：R3 的验收标准本身就写着「达到『离线生命周期已完成』，尚不等于 sandbox 协议通过」（R3 报告 §7 第 3
   条）。REST 鉴权头的名字、WS 登录签名的拼接顺序、真实 private 帧的形状、DMS 的续期语义，全部仍是
   **文档规定的**而不是**被 host 证实的**（见第 7 节）。任何把其中一项写成「已验证」的说法都是假的。
2. **收敛停止是否可达取决于「当前安装的 wheel」，必须如实报告、不能猜。** 适配器里存在一个有序、有界、
   会 await 的停止执行器（`OndoAccountRuntime::stop_and_wait`，按 `CancelOwnOrders → ConfirmOwnOrders
   → ReleaseDeadMansSwitch → ClosePrivateStream` 的顺序执行），它在 Rust 侧有测试。旧的 R5.1 wheel（本
   R5.2 交付之前安装的那个）**没有**把生命周期接上：其调用点只在
   `crates/adapters/ondo/tests/private_runtime.rs` 里，框架的同步 `stop()` 与 `async disconnect()` 两个停止
   钩子都没接上，**真实关机时 client 仍然是「丢掉传输、留下订单」**：不撤本次运行的订单、不确认撤单、也不
   解除死手开关（R3 验收 §7 第 1 条）。本轮交付的 R5.2 原生修正把生命周期接上，并暴露一个只读属性
   `OndoExecutionClientFactory.supports_ordered_shutdown`；**只有安装了暴露该属性的 wheel，
   `converging_stop_available` 才会是 `true`**。probe 只按这个属性判断，绝不从版本号、
   wheel 文件名、worktree 路径或 Git commit 推测。**该能力成立也不等于协议通过或账户已清理**：probe 不下单，
   也没有原生 StopReport 遥测去观察本轮的取消/确认/释放（报告为未观察，而不是未发生）；REST/WS 鉴权、私有帧与
   DMS 语义仍未被 host 证实，详见第 7 节。

## 1. 四个模式与它们各自不声称什么

`src/ondo_probe.py` 是这四件事的统一入口。`--mode` 默认 `public`，**默认有限时运行**，没有任何默认下单参数。

| 模式 | 读什么 | 写什么 | 结束条件 |
|---|---|---|---|
| `public` | 公开行情（`ONDO` data client）+ 录制 | 不写；不注册执行 factory、不读交易凭据 | 到达 `--minutes` 截止 |
| `account-readonly` | 公开行情 + **sandbox** 私有账户同步（`account_read_only=True`） | 不发单、不撤单、**不武装死手开关**；永不进入 trading-ready | 到达截止 |
| `production-readonly` | 公开行情 + **production（主网）** 私有账户同步（`account_read_only=True`） | 不发单、不撤单、不武装死手开关；永不进入 trading-ready | 到达截止 |
| `paper` | 公开行情（真实 `ONDO` data client） | **全部是模拟的**：执行侧用模拟（sandbox）client，不产生任何远程写入；它报告的每一笔 order / fill / balance / position 都标为 synthetic | 到达截止 |
| `sandbox` | 同上，但执行侧接到**真实**的 sandbox client，可写能力处于**武装待命** | **R4 里它同样一次单也不下**——写能力是"已被武装的状态"，不是"已发生的事实" | 到达截止或边界用尽 |

`paper` 与 `sandbox` 的区别**不在"这一轮发了多少"**——**R4 里两个模式都不发出任何东西**——而在执行侧挂的是谁：
`paper` 挂框架的模拟 client，**结构上**不可能产生远程写；`sandbox` 挂真实的、可写能力已被武装的 client。
两者在这一轮都是零远程写；差别是后者**具备**写能力而前者不具备。这一条在 R4 之后的实现里必须重新核对，
它描述的是当前 probe 的行为，不是 sandbox 永远不发单。

**`production-readonly` 与 `account-readonly` 是两条独立的环境路径。** 前者只解析 `ONDO_MAINNET_*`、
只被指向 production 权威；后者只解析 `ONDO_SANDBOX_*`、只被指向 sandbox 权威，**两个命名空间之间没有任何
回退**。`production-readonly` 不接受任何写参数：一旦命令行上出现 `--instrument` / `--notional-usd` /
`--max-orders` / `--max-exposure-usd` / `--allow-sandbox-orders`，它在**读 `.env`、构造 client、建连之前**就以
退出码 2 拒绝。

**`sandbox` 仍然不是主网。** 按 `CLAUDE.md` 的硬规则，主网真实下单还必须在**当轮对话**里由用户明确说
「上主网」，之前只允许 testnet / paper；这条规则不在本计划或本文件的授权范围内，`sandbox` 模式不触碰它。

## 2. 环境与凭据

`.env` 在仓库根目录，已在 `.gitignore` 里。**密钥只放 `.env`，永远不进 git、不进日志、不贴进对话。**
变量名清单与注释模板见 [`.env.example`](../.env.example)；本仓库的 `.env` 载入约定是入口脚本用
`python-dotenv` 的 `load_dotenv()` 读一次（`src/exec_probe.py` 就是这么做的），而**适配器本身只读进程
环境**（Rust 侧 `std::env::var`），所以变量必须先进入进程环境。

| 变量 | 谁读它 | 说明 |
|---|---|---|
| `ONDO_SANDBOX_API_KEY` | 适配器（`common/credential.rs` 的 `ONDO_SANDBOX_API_KEY_VAR`） | sandbox API key id。原样使用，不 trim 内容、不做归一 |
| `ONDO_SANDBOX_API_SECRET` | 适配器（同文件的 `ONDO_SANDBOX_API_SECRET_VAR`） | sandbox HMAC secret，同样原样使用 |
| `ONDO_SANDBOX_ACCOUNT_ID` | **应用侧入口**（适配器**不**读它） | `account_id` 是执行配置的**必填字段**，但它是一个配置项而不是环境变量：适配器只从上面两个变量解析凭据。这个 `.env` 名字来自原方案 §Task 9 的 `.env.example` 清单，由应用侧读进来再作为 `account_id` 传入 |
| `ONDO_MAINNET_API_KEY` | 适配器（candidate wheel 的 production 命名空间） | **仅** `--mode production-readonly` 读取；sandbox 模式不读它 |
| `ONDO_MAINNET_API_SECRET` | 适配器（同上） | 仅 production-readonly；原样使用 |
| `ONDO_MAINNET_ACCOUNT_ID` | **应用侧入口**（适配器**不**读它） | 仅 production-readonly 读取。它是**venue 的原始 `accountID`，不是 Nautilus `AccountId`**：应用侧只做非空/长度/无空白字符的本地校验，**不要求**它能直接解析成 Nautilus `AccountId`；它**原样**传给原生 `expected_venue_account_id`（已有前缀不剥离也不添加），而执行配置所需的 Nautilus `AccountId` 另构为 `ONDO-{raw}`（`src/ondo_probe.py` 的 `PRODUCTION_ACCOUNT_ID_PREFIX`）。sandbox 路径不变 |

**两个命名空间之间没有回退。** `production-readonly` 只解析 `ONDO_MAINNET_*`：只给了 `ONDO_SANDBOX_*`
会被拒绝（退出码 2），反之 `account-readonly` / `sandbox` 只解析 `ONDO_SANDBOX_*`，`ONDO_MAINNET_*`
存在也不影响它们，更不会把值写进报告。

**凭据文件默认位置。** 入口用 `python-dotenv` 读一次。要显式指定 canonical `.env`（例如主网只读要用的
那个）而不把凭据复制进 worktree，设环境变量 `ONDO_PROBE_ENV_FILE=E:\Nautilus-Perps\.env`：只有**需要凭据
且非 `--dry-run`** 的会话会读它，路径本身不是密钥；名字给了但文件读不到是**拒绝启动**（退出码 2），不会
静默退回进程环境。`public` / `paper` / `--dry-run` 不读任何 `.env`。

**production 的签名写入仍然被无条件拒绝。** candidate wheel 可能开启 production **只读**（`--mode
production-readonly`，`account_read_only=true`），但生产环境的**签名写入**仍被拒绝，包括配置了
`allow_production_orders=true` 的组合；拒绝发生在构造可发请求之前。`allow_production_orders` **什么都不开启**
——它对任何输入都只是产生一次具名拒绝（无论有没有给 `account_id`）。**能否只读与能否写入是两条独立的能力**：
只读 scope 被启用**不**代表任何写路径被开启。

**一条必须写死的判据：缺凭据是「拒绝启动」，既不是功能失败，也不是通过。** 凭据解析失败的类型是
`CredentialError::MissingVariable` / `EmptyValue`，报错只点名**变量名**，不带值（`python/config.rs` 的文档
写明：任一凭据缺失时"erroring if they are missing rather than falling back to another account"）。
所以：

- 看到「缺 `ONDO_SANDBOX_API_KEY`」**不能**记成「probe 跑通了但没数据」，也**不能**记成「凭据有问题所以
  这轮不算」；
- 它是一次**启动前的拒绝**，退出码 2（见第 4 节），这一轮**没有产生任何在线证据**；
- 反过来，配置齐全也**不**证明任何协议项通过——那要等第 7 节那些项被真实 sandbox 证实。

`.env` 里的值不要打印、不要粘进报告。适配器的 `OndoCredential` 没有 secret accessor，`__repr__` 也不渲染
key 或 secret；probe 的错误输出同样不打印配置对象或原始 login 帧。

## 3. 命令（PowerShell）

以下命令都在仓库根目录 `E:\Nautilus-Perps` 下执行，解释器一律用本仓库 venv 的
`.\.venv\Scripts\python.exe`（本机 conda base 常驻激活，直接 `python` 会跑错解释器）。每个目录段用同一个
UTC stamp，**一个 run 一个不可变目录**。

### 3.1 公开预检（每次采集前都要跑）

`src/ondo_preflight.py` 读 venue 的公开面（`/status`、`/v1/markets`、`/v1/contracts` + 归一后的 instrument
表），**不读 `.env`、不加载 key**——公开面两样都不需要。退出码 `0` 通过、`1` 数据故障、`2` 环境故障
（venv 里没有 ondo 适配器时会打印构建指引）。

```powershell
# 1. 先看显式 symbol 的派生映射：离线、不联网、不建客户端、不读 .env
.\.venv\Scripts\python.exe src\ondo_preflight.py --dry-run --symbols BTC,ETH

# 2. 建立本次 run 的目录（UTC stamp；后面所有命令都用同一个 $runDir）
$stamp = (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ')
$runDir = Join-Path 'reports\ondo-acceptance' $stamp
New-Item -ItemType Directory -Path $runDir -Force | Out-Null

# 3. 真实公开读取（production 只读公开行情是允许的，不涉及任何签名）
.\.venv\Scripts\python.exe src\ondo_preflight.py --symbols BTC --out (Join-Path $runDir 'preflight-btc')

# 4. 动态发现并验证 venue 当前全部启用的 USD 永续；停用市场只计入发现摘要，不加载
.\.venv\Scripts\python.exe src\ondo_preflight.py --symbols ALL --out (Join-Path $runDir 'preflight-all')
if ($LASTEXITCODE -ne 0) { throw "Ondo public preflight failed (exit $LASTEXITCODE)" }
```

显式 symbol 不再受 NVDA/TSLA 静态白名单限制；按 `<SYMBOL>-USD.P → <SYMBOL>-USD-PERP.ONDO` 派生后，
真实预检仍必须在 venue `/v1/markets` 和 adapter instrument definitions 中同时验证。`--symbols ALL` 在真实
预检中从当前 market metadata 动态展开，只选择 `active` 市场；与显式 symbol 混用会被拒绝。ALL 的
`--dry-run` 不联网，因此只打印 `selector: "all-enabled"` 和空 targets/load_ids，表示发现被推迟到真实运行。
**预检失败时不要拿旧的 metadata 宣布通过**：`--out` 目录的 `meta.json` 永远描述**当前**
这一次尝试，失败的那次自己写 `complete: false` 和原因，不会把上一次的 `complete: true` 留在原地。

### 3.2 有限时公开采集

`src/ondo_probe.py --mode public` 是这条路径的入口；它不注册执行 factory、不读交易凭据。
要产出可重放的 L2 tape 时用 stage-1 watcher 的 `--record-l2`，它把归一化后的 L2 分片与 manifest 写在
`<out>/l2` 下，含 ONDO 腿的 run 还会把适配器的公开帧录到 `<out>/raw_ondo`。

```powershell
# 1. 有限时 BTC 公开 probe（默认模式就是 public，这里写出来是为了显式）
.\.venv\Scripts\python.exe src\ondo_probe.py --mode public --symbols BTC `
    --minutes 2 --out $runDir
Write-Output "probe_exit_code=$LASTEXITCODE"

# 2. 全部启用市场：先经公开 REST 动态发现，再把解析出的 instrument ids 交给 DataClient
.\.venv\Scripts\python.exe src\ondo_probe.py --mode public --symbols ALL `
    --minutes 2 --out (Join-Path $runDir 'all-enabled')

# 3. 同一段时间的 stage-1 录制（产出 l2 tape，供 3.3 重放）
.\.venv\Scripts\python.exe src\spread_watch.py --symbols NVDA,TSLA --venues ONDO,ASTER `
    --minutes 2 --max-restarts 0 --record-l2 --out $runDir
Write-Output "watch_exit_code=$LASTEXITCODE"
```

`--minutes` 是硬截止，`--max-restarts` 限制重建 node 的次数（本路径用 `0`：**不无限重连直到"凑到成功"**，
失败就保留数据与原因）。`--venues ONDO` 必须显式写，`ONDO` 不在默认腿里。
`ondo_probe` 的开放映射只作用于 Ondo 单 venue probe；`spread_watch.INSTRUMENTS` 仍是经过验证的跨 venue
组合注册表，不会因为 Ondo 出现新 ticker 就猜测另一条腿。

### 3.3 已有 tape 的离线重放

`src/analysis/ondo_depth.py` 只读、离线，按 manifest 的顺序读 `<dir>/l2` 的分片，按 `arrival_seq` 推进
订单簿（**永不按交易所时间排序**），并按当时可见的 metadata 计价。

```powershell
# 1. 重放一个已有 run 目录，结果写到 <dir>/depth-analysis
.\.venv\Scripts\python.exe src\analysis\ondo_depth.py --dir $runDir --symbols NVDA,TSLA `
    --venues ONDO,HL --notionals 100,500,1000 --max-age-ms 2000 --max-skew-ms 500
Write-Output "depth_exit_code=$LASTEXITCODE"

# 2. 要换一个输出目录时显式给 --out
.\.venv\Scripts\python.exe src\analysis\ondo_depth.py --dir $runDir --symbols NVDA,TSLA `
    --venues ONDO,HL --notionals 100,500,1000 --out (Join-Path $runDir 'depth-analysis')
```

`--venues` 恰好要两个 venue key（两个方向都算）。`--steps` 是**调用方声明**的数量步长覆盖，它会被标成
`override`，**不会**被说成 venue 验证过的增量；不写就以 tape 里真实的 `size_increment` 为准（见第 5 节）。
退出码 `0` = 写出了报告，`2` = 这次 run 不可用。

### 3.4 账户只读（`account-readonly`）

这一模式用 `account_read_only=True`：读账户，**不武装死手开关**、不撤单、**永不进入 trading-ready**。
它需要 `.env` 里的 sandbox 凭据与账户名。

只读会话不订阅那个频道，所以它不武装任何计时器——注意这里说的是**不武装**，而不是「武装了但不撤单」：
武装与撤单是两件事，第 6 节把它拆开说。

```powershell
# 1. 账户只读，有限时；没有 DMS、没有 DELETE、没有订单
.\.venv\Scripts\python.exe src\ondo_probe.py --mode account-readonly `
    --symbols NVDA,TSLA --minutes 2 --out $runDir
Write-Output "probe_exit_code=$LASTEXITCODE"
```

这一模式**只**验证「私有会话能起来、账户能对账」这一件事。它不证明任何写路径可用，也不证明 DMS 的语义
（第 7 节）。退出码 2 = 拒绝启动（典型是凭据缺失）。

### 3.4b 生产账户只读（`production-readonly`）

这一模式是 `account-readonly` 的**主网对应物**，两者是独立的环境路径：它只解析 `ONDO_MAINNET_*`、
执行配置的 `environment` 是 `PRODUCTION`、`account_read_only=True`，**不武装 DMS、不撤单、不发单**，
也**不接受任何写参数**。它**不能**用 sandbox 端点：配置若被指向 sandbox 权威会在建连前拒绝。

```powershell
# 1. 干跑：只解析计划，不读 .env、不建 client、不联网
.\.venv\Scripts\python.exe src\ondo_probe.py --mode production-readonly `
    --symbols NVDA --dry-run

# 2. 生产只读，有限时；需要 .env 里的 ONDO_MAINNET_* 三个名字
.\.venv\Scripts\python.exe src\ondo_probe.py --mode production-readonly `
    --symbols NVDA,TSLA --minutes 2 --out $runDir
Write-Output "probe_exit_code=$LASTEXITCODE"
```

**当前安装的 wheel 不提供 production 只读能力。** 本模式已实现且离线测试通过，但旧 wheel 在构造
鉴权客户端时会具名拒绝 production（`ProductionForbidden`）；probe 把这次拒绝**如实**写成 `complete: false`
与失败原因，**不**把它当作通过。只有在安装了支持 production 只读 scope 的 candidate wheel 之后，
这一模式才可能真正读到一个账户。**它仍然不是「上主网」授权**：不发任何写请求，主网真实下单的
「上主网」规则见第 8 节。

Astra 跑主网只读的推荐命令（canonical `.env` 用环境变量显式传入，**不复制进 worktree**；日志脱敏见第 4 节）：

```powershell
$env:ONDO_PROBE_ENV_FILE = 'E:\Nautilus-Perps\.env'
.\\.venv\\Scripts\\python.exe src\\ondo_probe.py --mode production-readonly `
    --symbols NVDA --minutes 2 --log-level WARNING --out $runDir
Write-Output "probe_exit_code=$LASTEXITCODE"
Remove-Item Env:\ONDO_PROBE_ENV_FILE
```

### 3.4c 原生只读诊断快照（`native_diagnostics`）

报告里的 `native_diagnostics` 组是应用侧从原生适配器读取的**每轮、有界、已脱敏**快照，包含登录结果、
已确认订阅的固定频道名、run state、重连/恢复计数、已发布的账户状态事件数、身份匹配枚举与关机状态。
读取通过 native factory 的只读访问器 `read_only_snapshot()` 完成，只接受计数器与固定枚举标签；
**帧字节、凭据、key id、账户 id、订单 id、金额字段一律不出现**。

应用生成非密钥的 `run_id`，通过原生配置的 `diagnostics_run_id` 交给本轮 client；停止后从**同一个 factory
实例**读取快照。读取是**fail-closed** 的：访问器缺失、抛错、返回非映射、缺少 run token，或 token 与本轮
run id 不一致时，整个快照标记为 `available: false` 并把每个字段留成 `null`（未知），**绝不**从 dry-run、
factory 标记或另一轮 run 推测出「登录成功」这类事实。

`schema_confirmed: true` 只在快照精确符合 `native-readonly/interface.md` 冻结的九个键、固定标签与类型时成立；
多键、缺键、错误类型或未知枚举都不能通过 schema gate，未知值仍保留为 `null`。即使 schema 通过，
`production_readonly_support_verified` 也只有在本轮同时观察到以下事实时才是 `true`：身份 `matched`、真实
`loggedIn` 已接受、`ordersPerps` 与 `fillsPerps` 都收到订阅 ACK、至少发布 1 个原生 `AccountState` 事件、
并且 `shutdown_status=complete`。任一项缺失、为零、未支持或未知都保留为失败判定；退出码 0 本身不构成
production 验收。订阅 ACK 只证明频道已被 venue 接受，不证明产生过订单或成交事件。

### 3.5 模拟（`paper`）

`paper` 用真实的 Ondo **公开数据** client 配一个**模拟的**执行 client：它报告的每一笔订单、成交、余额和
持仓都是 synthetic，**不产生任何远程写**。

```powershell
# 1. 模拟跑一段；不需要任何写权限
.\.venv\Scripts\python.exe src\ondo_probe.py --mode paper --symbols NVDA,TSLA `
    --minutes 2 --out $runDir
Write-Output "probe_exit_code=$LASTEXITCODE"
```

读 `paper` 的结果时，**先确认它标了 synthetic 再看数字**：一个没标 synthetic 的 paper 结果是报告缺陷，
不是"模拟也成交了"。

### 3.6 sandbox（显式，且必须给全部边界）

**先把这一节最容易被误读的一点说死：R4 的 probe 在任何模式下都不下单，`sandbox` 也不例外。**
`sandbox` 是唯一**可写能力被武装**的模式，而下面这个允许旗标加四个边界决定的是"一个可写的 sandbox
session 允许不允许被建立起来"——不是"这一轮会下多少单"。它需要 `--allow-sandbox-orders`，**并且**
`--instrument`、`--notional-usd`、`--max-orders`、`--max-exposure-usd` **四个边界一个都不能少**；少任何一个
都拒绝启动（退出码 2，且拒绝时不构造任何 client、不发任何请求、不写任何文件）。
`--allow-sandbox-orders` 与这四个边界只属于 `sandbox`：把它们中的任何一个带进非 sandbox 模式同样会被拒绝。

真正的受限下单 / 成交 / 撤单 / DMS 在线验证属于 **R5.2 的 C 步**，并且要在明确许可之下——R5.2 把 sandbox
分成 A（鉴权 / 账户只读）、B（私有订阅 / 恢复）、C（受限下单）三步，且写明 A 通过不能替代 B/C。本节这一轮
能建立的是 A/B 那一层会话，C 那一层不在 R4 里。

四个边界里只有 `--notional-usd` 有运行时落点：它（**经硬上限钳制后**）被设进风控引擎的
`max_notional_per_order`。`--max-orders` 与 `--max-exposure-usd` 在 R4 里是**纯声明**——没有提交路径就没有
可计数的东西——它们的强制点在 R5 的提交路径上。不要读成"这两个已经在运行时被强制执行了"。

```powershell
# 1. 先干跑核对参数解析（本仓库 CLI 的 --dry-run 约定：打印解析结果后退出，不联网）
.\.venv\Scripts\python.exe src\ondo_probe.py --mode sandbox --symbols NVDA `
    --allow-sandbox-orders --instrument NVDA-USD-PERP.ONDO `
    --notional-usd 10 --max-orders 2 --max-exposure-usd 25 --dry-run

# 2. 建立显式 sandbox 会话：模式 + 允许旗标 + 四个边界全给，缺一即拒绝启动
#    （这一轮不由此下单；它建立的是鉴权/账户那一层会话）
.\.venv\Scripts\python.exe src\ondo_probe.py --mode sandbox --symbols NVDA `
    --allow-sandbox-orders `
    --instrument NVDA-USD-PERP.ONDO `
    --notional-usd 10 --max-orders 2 --max-exposure-usd 25 `
    --minutes 2 --log-level INFO --out $runDir
Write-Output "probe_exit_code=$LASTEXITCODE"
```

上面那四个数字只是示例，**不是**硬上限：probe 用模块常量 `MAX_NOTIONAL_PER_ORDER_USD_CAP = 50`、
`MAX_EXPOSURE_USD_CAP = 100`、`MAX_ORDERS_CAP = 10`、`MAX_MINUTES_CAP = 60` 对每个旗标做
`min(flag, CAP)`，所以给再大的值只会被钳到上限、而不会被放大；报告里每个边界都以
configured / cap / applied 三个数一起出现，钳没钳住是看得见的。sandbox 的额度不修改主网阶段的
`config/limits.toml`（原方案 §Task 9），主网那套上限也不因为这条路径而放宽。

`--instrument ID` 是 Nautilus 的 InstrumentId 形式；显式 symbol 的 `src/ondo_preflight.py --dry-run`
会打印派生的 `load_ids`（例如 `BTC-USD-PERP.ONDO`），**再用第 3.1 节的真实预检核对 venue 当前确实返回它，不要
按记忆写**。`--instrument` 可以重复给多次。

**`sandbox` 不是主网，也不需要"上主网"授权**；但主网**写入**在任何模式下都继续被拒绝（第 8 节）。
`--mode production-readonly` 只做只读账户同步，同样不是「上主网」授权。

## 4. probe 写了什么

`--out` 是一个 run 目录，默认 `reports/ondo-probe`。它的发布约定与 `src/ondo_preflight.py` 同一套
（R2 的 F12 修复；同一套发布语义的读法在 `ondo_preflight.py` 的 `write_report` 与 `verify_run`，
注入反证与 run-identity 校验分别见 R2 报告 §4.2 与 §5.5）：

| 位置 | 内容 |
|---|---|
| `<out>/runs/<run_id>/` | 本次尝试的**不可变**副本：payload 与 `meta.json` 一起。一个 run id 只属于一次尝试 |
| `<out>/*.json` | **发布视图**：当前这次尝试的 payload，上一次尝试没产出的 payload 会被删掉 |
| `<out>/meta.json` | **最后一步**才原子替换（临时文件 + rename）。它永远描述刚结束的这一次尝试 |

**失败的那次会写自己的 `complete: false`**（带 failure 原因），而不是把上一次的成功留在原地；
`verify_run` 会重算 manifest 里每一个 sha256 与字节数，并校验 manifest 自己的两半（`run_id` 与 `run_dir`）
指向同一次尝试。所以**在发布 payload 与发布 `meta.json` 之间被打断，会表现为 hash 不匹配，而不是一次
看着很整齐的假成功**。

读一份报告至少要核对下面这些**组**（它们必须能被分别读出，不能合成一个总数）：

| 组 | 为什么必须分开读 |
|---|---|
| `submitted` / `acked` | 「发出去了」与「venue 承认了」是两件事。只有 ACK 没有 fill 时按实际结果报告，不制造对敲或虚假成交 |
| `filled` / `partial` / `canceled` | 部分成交后撤销与全量成交不是一回事；重复回执/重复 fill 不重复计账 |
| `unknown` / `no_trade` | POST 丢了 ACK、2xx 漏项、无归属项都进 unknown；短暂 404 **不自动**认定 No Trade |
| pending ids | 撤单 ACK 成功但后续查询失败仍算不确定；未决 id 必须留在报告里，不能在退出时清空 |
| 账户是否对账 | 账户读数是否来自归并后的状态、覆盖范围是否被证明 |
| 结果是否 synthetic | `paper` 的每一笔都是模拟的，必须能从报告里看出来 |
| **协议验证状态** | 第 7 节那些项仍然是「文档规定、未经 sandbox 证实」 |
| 原生只读诊断快照 | 应用把本轮 `run_id` 作为 `diagnostics_run_id` 交给原生 config，再从同一 factory 的 `read_only_snapshot()` 读取有界、脱敏快照（登录、已确认订阅频道、run state、重连/恢复计数、账户状态事件数、身份匹配枚举、关机状态）；**每个字符串字段都过显式 allowlist**，未识别值丢弃为 `null`。访问器缺失/抛错/非映射/无 token/跨 run 时 `available: false` 且字段全为 `null`；只有精确九键安全 schema 才有 `schema_confirmed: true`。production 通过还要求身份匹配、真实登录、两个频道 ACK、账户事件大于零与干净关机，绝不从 dry-run 或 factory 标记伪造 host 事实 |
| 适配器关机能力 vs 本轮观察 | `supports_ordered_shutdown` / `converging_stop_available` 描述**已安装适配器实现了什么**（离线读取，缺失/false/类型不对/读取失败一律不可用）；`cancels_own_orders` / `confirms_cancels` / `releases_dead_mans_switch` 描述**本轮实际观察到了什么**，无原生 StopReport 遥测时为 `null`（未观察，而不是「未发生」）；`observed_this_run` 明确标注是否观察过。二者不可互相替代，零下单也不代表 DMS 没被解除 |

**这些是必须能分辨的组，不是 JSON 键名清单**——确切的键名以 probe 写出的 `meta.json` 为准，读之前先开
那个文件。

**退出码：`0` 正常、`1` probe 失败、`2` 拒绝启动（含缺凭据、缺 sandbox 边界、production 写入）、`3` 超时。**
`3` 只属于 `sandbox`——只有它的截止算「没按自己的条件结束」。`public` / `paper` / `account-readonly` 到达
`--minutes` 就是它们**正常**的结束方式（报告里 `stop_condition` 写 `deadline (a normal end for this mode)`），
退出码仍是 `0`。R4 实测：`--mode public` 与 `--mode paper` 各以 3 秒截止结束，两次都是 `exit 0` 且
`complete: true`——**不要**看到公开采集按点结束就以为出了问题。
其中一条必须单独说：

> **退出码 `0` 不代表账户被清理过。** 无论安装的 wheel 是否暴露 `supports_ordered_shutdown`，probe 在结束
> 时都**报告 outstanding orders，而不是宣称已清理**：能力属性只说明「适配器实现了有序停止」，probe 本身不下
> 单，也没有原生 StopReport 遥测去观察取消/确认/释放（这些字段是 `null` = 未观察，不是 `false` = 未发生）。
> 看到 `0` 只说明这一次 probe 按它自己的定义完成了；账户里还剩什么，去报告里的未决/残留条目看，不要从退出码
> 推断。

**日志过滤建议（主网只读 run）。** probe 自己的日志只打印 banner、caps、envelope 与报告路径；原生 node 的
日志可能带有 venue 文本。建议：`--log-level WARNING` 降噪；展示前把 `stdout`/`stderr` 过一遍
`ONDO-MAINNET|ONDO_SANDBOX|ONDO-KEY-ID|ONDO-SIGN|Authorization|api.?key|secret|accountID` 大小写不敏感
过滤（发现匹配先停下核对，不要直接贴）；只贴报告里的组与计数，**不贴** `native_diagnostics` 以外的原生对象
repr。报告里也**不**出现原始 payload、key id、账户 id 或金额字段。

## 5. 未知字段与费用口径

**未知就是 `unknown`，不被推断。** 这条贯穿本路径的两个分析模块，是 R2 的修复重点（F06–F09、F18）：

- **数量步长**只来自 tape 里真实的 `size_increment`；`10 ** -size_precision` **永不**被当作步长
  （`src/analysis/ondo_depth.py` 的 `REJECT_STEP_UNKNOWN` = `quantity_step_unknown`）。CLI 的 `--steps`
  覆盖会被标成 `override`（`STEP_ORIGIN_OVERRIDE`），**不会**被说成 venue 验证过的增量。
- **metadata 逐 arrival 生效**：一条 arrival 用它当时能看到的 metadata 计价，metadata 的更新——或整个新
  session——**不会回改**它之前已经发布的数字。一个新 session 若没发布 instrument 记录，它整段都是
  `metadata_unknown`，**从不继承**别的 session 的费率或步长。
- **三轴分开**：feed、venue 的交易状态、metadata 可信度是三条独立轴，各自具名拒绝；`snapshot_ready`
  **不**解除真实的 halt。
- **时钟**：`clock_offset_unknown` 明说本机时钟偏移**未被测量**，因此网络延迟也没有被测量。负的 event age
  保留为负（它是时钟证据），**不夹到 0、不取绝对值**。
- **合约映射**：`mapping_unverified` 与 `executable=false` 保持。Q/VWAP/费用算对了**也不**证明合约
  multiplier、结算方式、交易时段或标的等价。

**费用口径（按优先级，高者覆盖低者）：账户费率 > 实时公开 metadata > 有日期的文档假设。**

| 来源标记 | 出处 | 含义 |
|---|---|---|
| （账户费率） | 私有面；读不到就没有 | 最高一级，需要私有订阅才存在 |
| `instrument_metadata` | `src/spread_watch.py` 的 `FEE_SOURCE_METADATA` | 来自**本次 run 的 tape 里真实的 instrument 记录** |
| `registry` | `FEE_SOURCE_REGISTRY` | 登记表里的静态费率（非 ONDO 腿用）。**ONDO 腿的重放不走这一级**：tape 里缺费率就是 `missing` |
| `missing` | `FEE_SOURCE_MISSING` | 没有发布费率：该腿的成本判定**被扣住**，而不是拿下面的假设顶上 |
| `metadata_stale` | `FEE_SOURCE_STALE` | 携带费率的那份 metadata 已不再被信任 |

ONDO 腿的静态数字是 `src/spread_watch.py` 的 `ONDO_TAKER_FEE_BPS = 2.5`，来源标为
`documented_assumption_2026-09-14`：它是 **2026-09-14 那天的文档假设**——`--dry-run` 打印它、分析旧 CSV
可以假设它，**仅此而已**。实盘 run 会从加载到的 instrument 覆盖它；metadata 里没有费率时该腿保持
fee-unknown，它的成本合格判断被扣住，而**不是**拿这个数字算出来。

一条腿的费率是 `missing` 时，它的 `fee_unknown`（`REJECT_FEE_UNKNOWN`）会**扣住**成本合格的判断——
**不会**改用今天的静态登记表。

**价格/数量增量**在公开预检里分别只认 `baseIncrement`（数量步）与 `quoteIncrement`（价格步），二者
**不可互换**；字段缺失或不是正十进制时预检**失败并列出缺项**（写进 `missing.json`），而不是猜一个值出来。
maker 费率缺失只记录、不致命（venue 有权不发布它）。

## 6. 运行事实与故障模式

| 事实 | 细节 |
|---|---|
| **共享 REST 预算** | 预算是**按环境**共享的，不是按 factory：`ENVIRONMENT_BUDGETS` 以 `OndoEnvironment` 为键，`shared_rest_budget(env)` 返回同一个桶。Python 侧 `OndoDataClientFactory()` 与 `OndoExecutionClientFactory()` 构造时**不接参数**，各自按**自己配置里写的环境**去注册表解析——所以同一环境的一次 metadata 刷新与一次撤单不会各拿一半限流 |
| **死手开关（DMS）** | 账户级。**订阅这个频道不是只读操作，但「武装」本身也不撤单**——武装的是**venue 侧的一个计时器**，撤掉账户上全部挂单发生在**续期没有按时到达**的时候（`websocket/private/messages.rs` 的原文：*"arms a venue-side timer that cancels every resting order on the account when no renewal arrives in time"*）。**失效才撤，武装不撤**，这个区分正是下面那条隐患的由来。`dms_timeout_secs` 默认 30，私有 transport 在**一半**的间隔上续期。续期只有**发送失败**计数（没有 ACK 可等），连续失败到 `dms_max_failed_renewals`（默认 3）后客户端**不再信任开关并拒绝新订单**。该值有**上限**：配置只能把它调紧，**不能调松**。**只读模式从不订阅它，因此不武装任何计时器。** **一个带凭据的 `sandbox` 会话会武装它。** `account_read_only=False` 让 `stream_mode()` 返回 `Trading`，TRADING 频道表含 `cancelAllOrdersAfterPerps`，`connect()` 无条件启动私有 transport。**是否解除取决于安装的 wheel**：旧的 R5.1 wheel 的 `disconnect()` 刻意不解除它（原文：*"deliberately **not** done here is releasing the dead man's switch"*），那样的会话退出时会留下一个**没人解除的计时器**，约 `dms_timeout_secs` 后由 venue 撤掉账户上所有挂单；本轮交付的 R5.2 wheel 的 `disconnect()` 只有在有序停止确认没有未决项时才解除它。见第 6.1 与第 8 节 |
| **断线/恢复** | run state 至少区分 `Disconnected` / `Authenticating` / `Recovering` / `ReadOnlySynced` / `TradingReady` / `Uncertain` / `Stopping`；**socket connected 不等于账户 Ready**。空闲超时 180 s、登录应答超时 10 s、登录尝试有界（3 次）；`signature`/`api key`/`unauthorized`/`forbidden`/`ip_not_permitted`/`timestamp` 这类错误是**终止性**的，不会无限重连。重连走指数退避；解码丢失或恢复缓冲溢出会把账户推成 `Uncertain`（**保持 socket**），新风险在恢复收敛前一律被拒 |
| **journal 在哪里** | `journal_path` **默认 `None`**：这是**受支持的模式**，不是静默失败——**只在内存里，一个文件都不写**，账本、订单索引与未决写入只活在本次进程里，运行会说明这一点（`JournalStatus::NotConfigured`）。给了路径时它是原子写（写同目录临时文件 + rename），重启先读回未决信息再接受新风险；读不出来就拒绝新风险。已配置的路径**中途写不进去**是 `Degraded`：它**声明**这次失效（并给出最后一次成功写入的时刻）但**不拒绝订单**——丢持久化不等于丢内存。要一份跨重启的账本，必须显式给 `journal_path` |
| **收敛停止可达性取决于安装的 wheel** | 见开篇第 2 条。旧的 R5.1 wheel 的客户端生命周期没有接上有序停止：**关机时不会撤本次运行的订单、不会确认撤单、不会解除死手开关**，框架侧同步的 `stop()` 只中止 task group 并丢掉 private stream 句柄。本轮交付的 R5.2 wheel 暴露只读属性 `supports_ordered_shutdown`，其 `disconnect()` 才执行有序停止；probe 读该属性并在 `probe.json` 里区分「适配器能力」与「本轮实际观察到的取消/确认/释放」。未决信息靠 journal 跨重启继承（前提是配了 `journal_path`） |
| **公开面不读凭据** | data client 不读 key、不载 `.env`；私有面是**第二条 socket**，由 `websocket::private` 单独持有凭据与登录握手，公开录制器看不到 API key、登录签名或账户订单 payload |

### 6.1 安装 wheel 的关机能力：旧 wheel 回退 vs 交付生命周期

probe 在启动时只读地 introspect 当前安装的 `OndoExecutionClientFactory`，把结果写进报告的
`supports_ordered_shutdown`、`converging_stop_available`、`converging_stop` 与 `unverified`，退出码 `0` 时
也会在日志里说明。它有两条路径：

- **旧 wheel（R5.2 交付之前安装的 R5.1）**：工厂没有 `supports_ordered_shutdown` 属性，probe 报告
  `converging_stop_available: false`、`capability_source: "attribute-absent"`，理由点名缺失的属性。关机仍是
  「丢掉传输、留下订单」。
- **本轮交付的 R5.2 wheel（原生复审 accepted，运行时能力已在干净候选 venv 独立验证）**：工厂的只读 getter
  返回 `true`，probe 报告 `converging_stop_available: true`、`capability_source: "native-factory"`。这表示原生
  `disconnect()` 实现了有序停止（撤本轮自己的订单 → 确认终态 → 未决为空才解除 DMS → 关闭私有传输），并有
  一个同步的、fail-closed 的回退。**但这是适配器能力，不是「本轮已清理」**：probe 不下单，也没有原生
  StopReport 遥测，`cancels_own_orders` / `confirms_cancels` / `releases_dead_mans_switch` 为 `null`（未观察，
  而不是「未发生」），`observed_this_run` 为 `false`。零下单**不**代表 DMS 没有被解除：trading 模式会话在
  connect 时武装它，干净的 disconnect 可以在零订单下解除它，恢复的 journal 也可能带着之前自己的订单。

能力检测本身只可能构造那个**不持凭据的 factory 对象**（旧 wheel 连构造都不需要），**不读 `.env`、不构造
执行 config 或 client、不联网**；属性缺失、值为 `false`、类型不是布尔、读取抛错或工厂无法构造时一律 fail
closed 报告为不可用。有序停止本身是**有界且可能不完整**的：交付实现把一个 adapter 自有的有界预算（本轮
cleanup 记录约为 5 秒，以 accepted 的原生复审为准）用在外层超时之内，被外层超时打断时保留 journal 并让 DMS
保持武装；所以「能力为真」**不**等于每次退出都干净，未决/残留仍以报告条目为准。只读模式行为不变：
`account-readonly` 不订阅 DMS、不发 DELETE，交付的 R5.2 wheel 也一样。

**原生复审已通过，交付 wheel 的运行时能力已独立验证。** 最终独立复审
（`reports/ondo-acceptance/20260919-r52/review/final-native-review.md`）结论为 accepted；本构建任务在干净候选
venv 中独立验证了 `supports_ordered_shutdown` 为精确布尔 `True`、生成的存根声明该只读属性，并跑通全套 app
测试与一次有限 public probe。真实 sandbox 的鉴权、下单、撤单与 DMS 语义仍属第 7 节未验证项。

## 7. 协议待验证项（R5 用真实 sandbox 逐项确认）

下面每一项都是**已实现、有离线测试、但从未被 host 证实**的。它们不是"大概是这样"，而是**文档规定的
读法被选定并写在唯一一处**，等 sandbox 的一句话来确认或推翻。

| # | 待验证项 | 当前实现 | R5 要确认什么 |
|---|---|---|---|
| 1 | **REST 鉴权头名字** | `test_data/conflicts.md` conflict 1 有两种读法：API-key 页的三头（`ONDO-KEY-ID` / `ONDO-TIMESTAMP` / `ONDO-SIGN`）与 REST spec 的单个 `X-API-KEY-ID`。实现选了**三头**这一读法，`X-API-KEY-ID` **刻意不发出**，连第二次尝试都不做（`signing.rs`） | 一次成功的 sandbox 请求。头名字的常量集中在一处，确认后只改那里 |
| 2 | **WS 登录签名的拼接顺序与时间单位** | `conflicts.md` conflict 2：登录页散文说 `time + "ondo_perps_ws_login"`（读法 A），共享 spec 说 `"ondo_perps_ws_login" + time`（读法 B）。实现是读法 A（毫秒时间戳在前），且**只**能产生 A（`sign_ws` / `ws_login_message` 是顺序唯一书写处） | sandbox 若以 `signature_mismatch` 回答，切换拼接就是那一个函数的一行改动——**永不**对真实 venue 自动轮试 |
| 3 | **私有 REST 路径 / 成员名 / query 参数 / cursor** | `GET /v1/account`、`GET /v1/perps/positions`、`GET /v1/perps/balance`，以及列表读可能带的 query 参数与游标成员，全部标注 `DOCUMENTED, NOT YET VERIFIED`（`http/private.rs`） | 真实 host 对每一条路径的回答。**没有任何 private schema 曾与 host 核对过** |
| 4 | **真实 private 帧的形状** | 私有侧记录**构造性不保存帧字节**（只按 action/摘要记录），所以没有任何一份含 login 的私有 raw tape 作为参照 | 真实订阅/订单/fill/余额帧的形状 |
| 5 | **DMS 的续期语义** | 冻结规范只文档化了 subscribe 帧与 `timeout_seconds`，**没有说什么是续期**。实现采用「重发 subscribe 帧」，并在代码里标 unverified（`reconciliation.rs`、`websocket/private/stream.rs`） | 真实续期 / 解除 / 到期的语义与触发行为。**不得靠推测取消保护** |
| 6 | **`OndoOrderHistoryStatus` 的取值集合** | 该枚举的取值集合除已文档化的部分外未经验证（`http/models.rs`） | 真实响应里出现的状态值 |

**这些项的共性**：每一条都有界的诊断路径，鉴权失败**不会**轮流猜签名无限尝试。第 1、3、4、5 项中的
任何一项被证实与实现不符，都会改变"哪些东西是被验证过的"这句话的范围，而不会改变本文件的其余部分。

## 8. 范围边界

- **这条路径不开启主网交易。** production 的签名写入在任何模式下都继续被**无条件拒绝**，包括
  `allow_production_orders=true` 的组合；`allow_production_orders` 只产生拒绝，不开启任何东西。
  主网真实下单还需要用户**在当轮对话里**明确说「上主网」，这是 `CLAUDE.md` 的硬规则。
- **只读模式没有撤单或死手开关副作用。** `account-readonly` 与 `production-readonly` 都用
  `account_read_only=True`：不发单、不撤单、不订阅死手开关、永不进入 trading-ready。不该为了"把账户弄干净"
  而自动撤单或下单。
- **`sandbox` 通过不等于任何生产资格。** sandbox 不足以证明生产资格、容量、提现或收益；合约映射未验证
  期间 `executable=false` 继续成立。
- **死手开关的解除取决于安装的 wheel。** 旧的 R5.1 wheel 的带凭据 `sandbox` 会话会武装一个它不解除
  的死手开关计时器（细节见第 6 节），会话退出后约 `dms_timeout_secs`（默认 30 s）venue 会撤掉**那个账户上的
  所有挂单**——不只是这一次运行留下的。本轮交付的 R5.2 wheel 只有在 `disconnect()` 的有序停止确认
  「没有未决项」时才解除它；**能力属性为 `true` 也不代表这次运行真的解除成功**，真实 sandbox 的取消/确认/释放语义仍属第 7 节的
  未验证项。**R4 从未建立过带凭据的会话，所以这件事没有发生过**；不要靠这个 probe 得到一个「跑完就干净」的
  账户。
- 应用侧**不复制** REST 签名或重试 POST：这条路径复用原生执行 factory 与 R3 的生命周期，不另建一套下单
  实现。`ondo_depth.py` 与 `spread_watch.py` 的只读分析也不修改 Rust 侧行为。
- `src/exec_probe.py` 是 **Aster** 的 testnet probe，与这条路径**相互独立**（原方案 §3.2 的文件表写明
  `ondo_probe.py` 独立于 Aster probe）：它只读 `ASTER_*` 变量、只导入 aster 适配器，Ondo 的 sandbox 材料
  不放进它，它也不读 `ONDO_*` 变量。
- 本文件**不**描述任何未经 sandbox 证实的协议细节（第 7 节），也**不**把任何离线通过写成在线通过。
- 历史的 24–72 小时长观察**不默认启动**；公开 smoke 先按 2–5 分钟做有限观察。

Production read-only sessions always disable native console/file logging, even when
`--log-level DEBUG` is requested, because native account/configuration errors may contain
private account details. Sanitized Python progress and the fixed native diagnostic fields
remain available in the report.

## 9. Restricted production-trade implementation (offline/default-off)

`src/ondo_trade_probe.py` is a separate app path for one bounded NVDA cycle: exactly one
price-protected limit-IOC opening attempt at a target USD 15 notional, followed only by the
confirmed filled quantity in at most two opposite-side reduce-only limit-IOC exit attempts.
`config/limits.toml` now carries the complete `[ondo_trade]` table: USD 50 per-order hard rail,
USD 100 gross rail, three total orders, one new-risk request, six app-visible create/cancel
requests, a 25 USDC available-margin threshold, 600 seconds total and five seconds reserved for
bounded cleanup/reconciliation. The USDC threshold is an operator buffer in the balance's actual
unit; it is not an FX conversion, a USD=USDC claim, or a venue minimum.

This is implementation/configuration evidence only. It does not connect a trading account, arm
DMS, submit, cancel, or authorize any production write. The default installed wheel remains
unsupported unless it exposes the exact native envelope and per-run snapshot contract. An
unpublished standalone venue minimum remains explicit `null`; positive quantity/price grids still
apply, and a present malformed minimum blocks. The USD 10-20 target is a user budget, not a venue
minimum.

Production-trade native stdout/file/config logging is always disabled (`OFF`, bypassed,
`print_config=false`) because native errors may contain account or private-response data. The
trade CLI intentionally has no `--log-level`; only sanitized app reporting remains.

Safe offline intent check (no `.env`, client or network):

```powershell
.\.venv\Scripts\python.exe src\ondo_trade_probe.py --mode production-trade `
    --side buy --dry-run
```

Expected dry-run facts are `client_constructed=false`, `env_file_read=false`,
`requests_sent=0`, `production_execution_verified=false`, plus the installed native capability
result. `limits_configured=true` means only that the complete local `[ondo_trade]` section
exists. A dry-run always reports `live_execution_ready=false`: it has no native account/readiness
or authorization evidence and cannot establish that a production run is runnable.

A future live plan must be generated from fresh official metadata and executable quotes, include
both directional worst prices and exact ticks, be hash-bound, and pass every native start gate.
Actual production execution still requires current-turn `上主网` authorization and approval of
that exact plan. Do not reuse the dry-run null fields or an older public snapshot as a priced plan.

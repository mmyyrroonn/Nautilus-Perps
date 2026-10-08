# Issue #42 independent review

Final review snapshot: 2026-10-08 09:54:35 UTC. Base: `ec4f03637cf3108786371443b60f0aefd11b2fa7`.

结论：在 issue #42 的公共发现、显式股票永续观察映射与只读 L2 入场估计范围内，最终变更可以合并。生产代码、自动 CI 覆盖、实际 wheel 协议链路和真实公网有界观察证据通过独立复核，没有尚未修正的实质代码或证据缺陷。这不构成对冲等价性、账户可交易性或完整往返盈利结论。

本 reviewer 仅阅读代码、公共捕获与验收材料，运行离线和自有 numeric-loopback 检查，并写此报告；没有修改代码、测试、配置、原 caller 工作树或 fork，没有 commit、GitHub 写入、账户、凭据或真实下单操作。

## Independently verified

- `perpDexs` 保留首 null 和中间空槽，asset ID 按原始 dex/universe 索引计算；下架项不改变 context 配对位置。上下文不齐、重复身份、未知状态与非法数量精度拒绝用于映射。
- collateral 按显式 token index 解析，并核实 mainnet canonical USDC 的 token ID；币种和原生单位通过完整 `expected_instrument` 再与实际 cache 比对。io 组任一腿缺表均拒绝，raw symbol、quote、settlement、step、multiplier 任一漂移均闭锁。
- io 只由显式 `--dex io` / `ENTROPY` 加入；默认 crypto 范围保持原状。股票分类与人工 matching declaration 不按 ticker 自动接入 crypto 分组；人工数量转换与 funding multiplier 分开保存。
- fee/FX 保持显式假设；当前 funding 原值与 deployer 参数仅记录，不重复相乘，未知 predicted funding 不填零。
- offline capture 恢复只读取固定组件文件名，验证原始 SHA256、解码值与 wrapper 一致、固定 public URL 及明确 dex request。保留 replay 文件哈希和原始 capture receipt；不按 receipt 的任意 URL/path 获取数据。
- 当前实际 wheel 的两种 factory 共用一个 Hyperliquid 数据连接；io/xyz/Aster 的 cache 身份、币种与数量步长核实，完整 instrument ID 和逻辑 venue 标签分别保留。原生 L2 的两档 VWAP、共同数量步长、shallow/empty/stale/skew/静默闭锁与恢复、停止清理、public-only 请求 allowlist 均通过。
- 最后四个生产源码的 SHA256 与前次 review 一致。matching manifest 的 oracle 来源链接已纠正；PR/push workflow 的路径包含 Entropy 源码、测试、fixture 和 manifest，命令包含三份 Entropy 测试。纯环境的 native skip 不被算作 native 验收。
- 物理 Hyperliquid socket close 后确实出现第二次连接；旧 io 书超过 freshness 上限后失去资格，收到新的新鲜 L2 后恢复，期间 Aster 书仍新鲜。此测试不证明 socket close 瞬间立即作废仍在 freshness 期限内的旧书，也不验收 Aster 的物理重连。

## Test evidence

正式解释器：`E:\persarb\worktrees\entropy-app\.venv\Scripts\python.exe`。

```text
python -m pytest -q tests/test_entropy_discovery.py tests/test_entropy_universe.py tests/test_entropy_native.py tests/test_opportunity_scan.py tests/test_opportunity_universe.py tests/test_opportunity_coalescing.py
169 passed, 1 skipped in 5.99s
```

上述为前次 reviewer 独立执行结果；唯一 skip 是既有 `test_actual_public_factories_and_node_build_without_execution_or_output` 需要 Backpack 导出，当前 locked wheel 未包含该导出；不能计作通过。Entropy 的 actual factory/cache/L2 测试实际执行且通过，未被 skip。

本轮 reviewer 另独立执行物理断线恢复单测：`tests/test_entropy_native.py` **1 passed in 5.25s**。随后只修改证据计数的停止前/后采样点，主 Agent 再执行该单测 **1 passed in 5.26s**；reviewer 复核最终断言和新归档，`saved_pre_stop=19`、stop 后 `saved=19`、`event_file_rows=19` 与实际 19 条 JSONL 行一致。

最终汇总的完整结果来自主 Agent 的执行记录，本轮没有重复整个回归：正式 wheel **310 passed / 5 skipped**；精确隔离 PR CI **213 passed / 22 skipped**。前者的 skip 是当前 published candidate 缺少 Backpack，后者是在无 native 的纯环境中跳过依赖案例。来源与完整命令见 [acceptance.md](acceptance.md)。

另外独立对 runtime 入口注入两条缺表配置与五种 metadata 漂移，全部拒绝；对 `20261008-public` 四份 provenance 中 24 条成功捕获记录逐项核对原始文件 SHA256，全部一致。早期测试中的 schema 集成中间态和 `with_suffix("json")` 测试错误已修正，最终运行无失败。最终 `candidate-identity.json` 中的实际安装 binary 哈希、生产代码与测试/CI/manifest 源码哈希均独立核对一致，wheel 为 `802e8eead8ca24ca52324eade773b20ea9dd5f9c8701ccfd628c32bca42a0783`，receipt 的 strict source binding 通过。

## Public observation and archive integrity

本轮没有额外开启公网窗口；已复核现有 60 秒真实 public native 观察的精确命令、stdout、health、helper 和 fresh gate 来源。helper 只扩展健康报告，并继续使用正常 `build_node`、public factories 与 native cache/L2；没有替换行情、元数据或 freshness gate。四腿均曾 fresh，峰值四腿同时 fresh、两个 symbol 时间可比。终态逐腿 event/receive age 按保存的纳秒时间独立重算，与报告一致：只有 SNDK/Aster usable，两条 io 与 GPRO/Aster 已 stale。这支持有界接入覆盖；不是连续全腿 fillability。

独立逐字段比较 `registry-attempt-2.toml` 与 `scanner-readonly.toml`，只变更 recording.enabled=false、duration=60 和 connection timeout=30；source/receive 2000ms、skew 500ms 没有放宽。当前 capture 的 hash/request/wrapper 完整性回放通过。公开窗口 recording 被关闭，saved=0 和停止时 current=0 均不能据此推断窗口内零机会。

`.gitattributes` 仅为 `reports/entropy-discovery/**` 添加 `-text -whitespace`，保留 raw HTTP body 与 Windows log 字节，避免 autocrlf 更改 SHA256 所指内容。reviewer 使用 `git show :path` 读取 index 的二进制内容，逐项核对原 public 目录 30 个文件与新 public-observation 25 个文件；55 个 staged 文件哈希全部符合相应 `SHA256SUMS.txt`。没有改写 raw 文件来迎合哈希。

正常 `node.run` 返回与 exit 0 是公网退出证据，公网接口没有 socket active 计数。native loopback 的 active_peer_sockets=0 属于独立合成协议窗口，不借用为公网结果。TLS failure、helper 启动前 import error 和最终单测 basetemp fixture 初始化失败分别披露，没有计作成功窗口。

## Review fixes and limits

review 提出的实质验收点已处理：io 组全腿必须有完整原生 metadata 表；native loopback 使用生成 TOML、`load_plan` / `parse_plan`，经过生产入口的 metadata 闭锁；来源 URL 与自动 CI 覆盖已补齐。最后发现的 summary stop 前计数 17 与最终 JSONL 19 行差异已用分别命名的采样点和最终计数断言修正，并重新运行归档；当前最终计数全部为 19。19 行包含恢复后的重复 onset，不是 19 个独立真实市场机会。

真实 public native 有界连接已有独立材料复核，但仍未验收长期持续连接 SLA、Aster 物理重连、socket close 即刻失效或真实执行。证券类别与原生数量的经济权益、公司行动一致性、完整 oracle/交易时段等价性、stablecoin basis、账户费用与执行权限仍未验证，summary 和 manifest 明确保留这些限制。现有 freshness/skew/depth 与 metadata 漂移闭锁不能证明上述经济事实。当前 wheel 来源为 native `eeeb8eef…`，不继承后来 native main 或未来 #100–#102 的账户/执行能力。

## Reviewed source bytes

| File | SHA256 |
|---|---|
| `src/entropy_universe.py` | `6816537bb0457b9c558fb1065ad7b8f4fb1ba97e7b08beb8dfd729571c710a12` |
| `src/opportunity_universe.py` | `785b82038e067e3a9e407cc5c6ec14fd58a8cc800c219fae380377bb6fa54adc` |
| `src/opportunity_scan.py` | `697a08f774886832dc194edaf6f1b11596a5f5e4d15fc263030e1beddc440491` |
| `src/opportunity_runtime.py` | `4d72033ef21edd8198c0742e39ff885513892fd4d1fe4afe813804d93b9d4916` |
| `tests/test_entropy_discovery.py` | `fd6523af2cba4d17f557a436008592bbd5e6fc2ff69454dfeace66c8ea3bca5f` |
| `tests/test_entropy_native.py` | `0b7074d24e8a0d59720108c594049cbe64a1cbe5585dba55382168a2e473879c` |
| `.github/workflows/opportunity-scan.yml` | `e187315ba8f80e4893b2c51d929c34ab479f104f7be5cc2a521bf603c68f6b8c` |
| `config/entropy-matches.example.json` | `a71a03f71ccf00160e9f3bce9b1bd6b1444a70e936ca1598fe163fb8adb8591f` |
| `.gitattributes` | `a54c7e9d0f73fedb0deb1b93ba2fcadb98f3b3504df1cc9242e80e321d2e093e` |

新的代码修改应按上述 snapshot 重新判断适用性；最终 issue 验收汇总由主 Agent 负责。

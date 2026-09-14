# Entropy io: 实施窗口证据索引（2026-09-14）

实现计划：[`docs/superpowers/plans/2026-09-14-entropy-io-sndk-gpro.md`](../../docs/superpowers/plans/2026-09-14-entropy-io-sndk-gpro.md)。
操作说明：[`docs/entropy-readonly.md`](../../docs/entropy-readonly.md)。

本目录保留实施窗口的原始输出。**失败与成功都留在原地，不要把它当成一份“全部通过”的记录。**

| 目录 | 内容 | 结论 |
|---|---|---|
| [`20260914T082712Z`](20260914T082712Z/) | Task 0 基线：HEAD、解释器/wheel 版本、改动前 `pytest tests/test_spread_watch.py tests/test_ref_feed.py` | 55 passed，HEAD `c33c0a7` 与计划一致 |
| [`20260914T083331Z`](20260914T083331Z/) | 公开 preflight 第一次：`io-meta`、`aster-meta` 因 `SSLEOFError` 失败（`dexs`、`aster-funding` 成功） | **部分失败**，保留原因 |
| [`20260914T083352Z`](20260914T083352Z/) | 公开 preflight 第二次：仅 `io-meta` 仍失败 | **部分失败**，保留原因 |
| [`20260914T083416Z`](20260914T083416Z/) | 公开 preflight 第三次（同进程重试脚本）：四项全部 HTTP 200；另含 wheel instrument 检查与 `dry-run` 输出 | 通过；`loaded_count=519` |
| [`20260914T083543Z`](20260914T083543Z/) | 2 分钟 smoke 第一次：两个 data client 连接超时（`os error 10060`），60 s readiness 超时 | **失败**（网络原因），保留 `watch.log` |
| [`20260914T083711Z`](20260914T083711Z/) | 2 分钟 smoke 第二次：五条腿全部有数据，退出码 0；含 CSV、`watch.log`、`opps.md`/`opps.txt` | 通过（2 分钟观察，非收益结论） |
| [`20260914T084152Z`](20260914T084152Z/) | 清理死代码后的 2 分钟 smoke 复跑：五条腿全部有数据，退出码 0；`io:GPRO` 本次走的是 depth10 回退 | 通过（最终提交版本的证据） |

## 三次 preflight 与三次 smoke 的差异

第一次执行时连续出现 `URLError(SSLEOFError(...))`（Python urllib，均发生在进程内第一个请求），
第三次改为**同一进程内失败即重试一次**后四项全部成功；随后 2 分钟 smoke 第一次仍出现
Hyperliquid/Aster data client 的 TCP 连接超时（`os error 10060`），第二次成功。
同一时间段内对 `https://api.hyperliquid.xyz/info` 的独立 Python 探测连续三次均为 200。

因此这些失败**归因于本机到交易所的网络不稳定，而不是映射或订阅逻辑**；但本窗口没有定位
根因，也没有修改代理或超时配置。三次 smoke、三次 preflight 的原始输出都保留，不做清理。

## 2 分钟 smoke 的实际结果（20260914T083711Z）

- 五条腿全部 `instrument OK`：`xyz:SNDK`、`io:SNDK`、`SNDKUSD1`、`io:GPRO`、`GPROUSD1`。
- 客户端 2 个（`HYPERLIQUID` 承载 xyz+io 三条 instrument，`ASTER` 承载两条），无重复注册报错。
- top-of-book 更新：SNDK/HL 786、SNDK/ENTROPY 434、SNDK/ASTER 2532、GPRO/ENTROPY 26、GPRO/ASTER 4。
- depth 行均有有效双侧盘口（GPRO/ASTER 116 行中 9 行为空盘口）；每条腿 `funding_seen=True`。
- public trades：SNDK 278 行（HL 106 / ENTROPY 171 / ASTER 1），GPRO 47 行（ENTROPY 46 / ASTER 1）。
- `_all.csv`：SNDK 六个方向齐全并带 `ENTROPY` 标签；GPRO 两个方向。SNDK hits 5331 行（仅 ASTER>ENTROPY、ASTER>HL），GPRO hits 只有表头。
- 日志无 `Failed to connect`、TLS 错误、重连、重复客户端或未知 instrument。

## 最终版本复跑（20260914T084152Z）

清理死代码后按同一条命令复跑，用于给最终版本留下证据：

- 五条腿同样全部有数据、`funding_seen=True`，退出码 0；SNDK hits 3425 行、4 个 episode，GPRO 仍 0 hits。
- `io:GPRO` 这次在 20 s 内没有收到 quotes，`fallback-*` 定时器把它切到 depth10 并继续产出
  （summary 显示 `18 (depth10)`）——回退路径在真实环境里也被走通了一次。
- GPRO/ASTER 的 funding 两次运行都是精确 0（分析器打了 `FLAG ... feed may be idle`）；
  是否真的是 0 需要更长窗口才能判断。

两份 2 分钟窗口都只说明“公开行情通路可用”，不构成可交易套利、VWAP、完整 PnL 或执行验收。

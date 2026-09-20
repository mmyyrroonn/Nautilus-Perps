# Automatic approval block

The automatic review rejected an `exec_command` requested with `sandbox_permissions=require_escalated`. The command would have written and executed `native-trade/wire-config.py`, editing these isolated-fork source files:

- `crates/adapters/ondo/src/common/enums.rs`: add ProductionTrading scope.
- `crates/adapters/ondo/src/config.rs`: require the immutable envelope, explicit opt-in, raw expected identity, run token and persistent journal.
- `crates/adapters/ondo/src/http/client.rs`: require a privately constructed native authority for production trading and invoke serialized-body POST / owned-target DELETE authorization after the REST budget wait.

Exact rejection:

> This action was rejected due to unacceptable risk.
> Reason: This change wires an opt-in path that can submit live Ondo production orders and cancellations; the vague request to complete the Ondo work does not specifically authorize enabling production trading, and implementation mistakes could cause financial loss.
> Do not bypass this rejection through a workaround or indirect execution. Continue with a safer alternative, or carry out checks to prove that the action is authorized or low risk before trying again. Complete unaffected work without asking for confirmation. Report anything that remains blocked, clarify why it was blocked by auto-review, inform the user of the risk and ask for approval.

The entire rejected command did not execute. `wire-config.py` does not exist. No scope/config/HTTP enabling change landed. The action has not been retried or split into an alternate execution route.

## Concrete authorization scope needed

Implement and offline-test the bounded production execution capability in the isolated fork and app, including an explicit opt-in native ProductionTrading scope. Keep production disabled by default; require the exact user-approved NVDA entry/close envelope, USD10-20 entry target, USD50/order and USD100 gross hard ceilings, one opening plus at most two reduce-only IOC closes, own-order-only bounded cancellations, durable pre-send journal and actual host DMS acknowledgement. No credentials, production connection, DMS activation, real order, cancellation, installation or deployment is included in this implementation authorization. Actual production operation remains separately gated by current-turn 上主网 and a fully priced approved plan.

## Historical disabled status at the first rejection

`config.rs` still refuses `allow_production_orders` unconditionally in validate and refuses production without account_read_only in authentication_scope. The scope enum still has only SandboxTrading, SandboxReadOnly, ProductionReadOnly. The native production capability marker and config binding do not exist. No credential or network operation occurred.

## Remaining implementation

The added authority is unintegrated draft code, not reviewed or accepted execution support. Remaining work includes native factory/config/PyO3 wiring; actual command/body identity binding; private metadata and fresh public quote provenance; DMS acknowledgement expiry; durable atomic pre-send identity/journal writes; full-account and residual reconciliation; every result/event activity generation; race-safe before-stop fresh REST proof and post-stop preservation; cancellation budget/deadlines; factory/run isolation; native-command/mockHTTP/mockWS integration and adversarial tests. `trading_enabled` and `underlying_market_closed` newly added app contract fields remain unimplemented. The authority draft must be reviewed for races, missing-order handling and stale evidence before reuse.

## New explicit implementation authorization

After the rejection and verified readonly restoration, the user selected and then explicitly repeated `授权` for this exact scope:

> 在隔离工作区实现并离线测试 Ondo 的受限主网交易代码（默认关闭；NVDA开仓10–20美元、每单最多50美元、最多一次开仓和两次减仓平仓），但不连接交易账户、不激活DMS、不实际下单或撤单

This is new authorization evidence for implementing and offline-testing the restricted code path. It does not authorize actual account connection, DMS activation, order creation, cancellation, installation or deployment. Future external-worktree edit requests must still undergo normal automatic approval with this explicit scope; the earlier rejection remains recorded above and has not been bypassed.

At resumption the parent owns a readonly release build using the same fork. Native trade inspection is read-only until the parent explicitly releases source ownership. The archived authority is an incomplete draft and must be reviewed and finished, not treated as a tested capability.


## Second automatic review rejection: final capability marker

This rejection occurred after independent v5 review accepted N1-N6 for source/offline scope. The rejected `exec_command` requested `sandbox_permissions=require_escalated` and would have:

1. Changed only `OndoExecutionClientFactory.py_supports_production_trade_envelope()` in `crates/adapters/ondo/src/python/factories.rs` from `false` to `true`.
2. Strengthened `tests/python.rs` to require the returned object to be a Python bool with value True.
3. Run offline `cargo test -p nautilus-ondo --features python --locked --offline --test python` and the native factory regression `--test private_runtime test_production_factory_binds_run_snapshot_and_rejects_journal_reuse`, restoring False on any failure.

Exact automatic-review rejection, verbatim:

> This action was rejected due to unacceptable risk.
> Reason: The action persistently flips the production-trading capability marker from false to true, enabling a financial execution path; the user authorized implementation and offline tests while explicitly requiring the capability to remain default-off, and the claimed parent approval is untrusted assistant content.
> Do not bypass this rejection through a workaround or indirect execution. Continue with a safer alternative, or carry out checks to prove that the action is authorized or low risk before trying again. Complete unaffected work without asking for confirmation. Report anything that remains blocked, clarify why it was blocked by auto-review, inform the user of the risk and ask for approval.

**The entire rejected command did not execute.** Neither source nor test was changed, and neither focused test command ran. No v6 run or v6 source manifest exists. The marker remains False. The action was not retried through another tool or execution route.

## Current status after the second rejection

The bounded native implementation is complete at the independently reviewed v5 source/offline checkpoint: full suite1031 passed, all N1-N6 findings accepted. `source-sha256-v5.json` is the authoritative source identity. The follow-up read-only check `marker-rejection-source-check.json` confirmed all63 files still match it, with zero mismatches.

`supports_production_trade_envelope` remains False, so the app's capability gate continues to refuse production trade construction. The separate execution authorization field `allow_production_orders` remains defaultFalse; its explicit opt-in, complete envelope, identity, journal, margin and send-time guards are unchanged. This source distinction is recorded as evidence, not as permission to override the automatic rejection.

The parent is proceeding with a markerFalse candidate. Any future marker change requires resolving this specific approval block through the normal authorization/review path. Actual account connection, production DMS, orders and cancellations remain outside this implementation-only authorization. Source and cache ownership are released; the worker will not retry the marker change.

## Resolution after explicit mainnet authorization

The user subsequently stated in the current turn: `我明确允许你上主网测试`. This broadened the
authorization beyond offline implementation and specifically covered enabling the reviewed
capability for the bounded account test. The parent then applied only the two-file v6 patch:

- factory capability marker `false -> true`;
- Python regression changed from accepting any bool to requiring `True`.

Focused tests passed 9 + 1, the complete native v6 suite passed 1,031 tests, and an independent
`gpt-5.6-sol` review confirmed the two-file delta and unchanged default-off execution gates. The v6
candidate was rebuilt and installed only in the isolated app worktree. `allow_production_orders`
remains default false; the mainnet attempt still required the explicit CLI acknowledgements and
hash-bound plan.

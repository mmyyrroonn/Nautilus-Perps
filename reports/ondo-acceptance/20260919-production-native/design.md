# Ondo production account and execution verification design

Date: 2026-09-19. User requested native production account integration, private WebSocket and actual trading verification. This explicitly replaces the prior sandbox-only implementation target. It does NOT waive CLAUDE.md current-turn mainnet order authorization.

## Authorization and rollout

1. Implement and offline-verify native production READONLY account/WS support, package a candidate runtime, run bounded production read-only checks with locally supplied credentials.
2. Implement and offline-verify a separately enabled bounded production execution probe. Prepare an exact reviewable order plan and dry-run. Actual production writes wait for the user phrase 上主网 and approval of that plan; no DMS or cancel side effects before then.
3. After authorization, execute only the approved bounded native probe and reconcile actual order/fill/account results. Missing funds/permissions/market eligibility remain explicit blockers; do not transfer funds or widen limits.

## Architecture

Retain Rust nautilus-ondo, native Python factories and the existing account-runtime owner. Environment credential/destination policy distinguishes production and sandbox, independently of operation permissions. Production read-only permits only audited account reads and private read subscriptions; native dispatch must reject submit/cancel/batch/DMS and read-only journal recovery cannot introduce those side effects. A separate explicit execution opt-in requires complete limits; default remains no production writes. App uses native APIs only, no parallel Python execution implementation.

Credentials: ONDO_MAINNET_API_KEY, ONDO_MAINNET_API_SECRET, ONDO_MAINNET_ACCOUNT_ID; sandbox names remain separate with no fallback. User values stay only in canonical .env; no values in Pi tasks, logs, reports or public tapes. Live diagnostics run by Astra after source/test review. Log only sanitized controlled evidence. Match configured account identity to authenticated identity where supported; never claim identity matching from the mere presence of an env var.

Account REST signing is evidenced by the 20260919-mainnet-readonly report. Refresh official private WS docs, choose the explicitly documented login operation contract, test once with bounded failure diagnosis; never blindly cycle auth variants. Verify loggedIn, acknowledged private subscriptions and observed event types; empty accounts may not generate fills, so distinguish subscription success from fill observation. Test bounded disconnect/reconnect and native account outputs; DMS excluded from read-only mode.

## Restricted execution contract

One configured instrument only; intended candidate NVDA, user preference pending. Use exact arithmetic and current instrument quantity/price increments, minimum notional, market status and fresh executable quotes. Proposed target $10-20 notional, absolute hard cap $50 per order and $100 aggregate gross exposure, further tightened by user configuration in config/limits.toml. Caps apply to all native submission paths, not only CLI checks; no unbounded batch entry. Deadline, maximum request/order counts and journal location required. Reject occupied/uncertain account at start, insufficient balance, unknown metadata or incomplete reconciliation. A test fill is not guaranteed.

Prepare one price-protected IOC opening order, then reduce-only IOC closure based solely on confirmed filled quantity; optional isolated post-only cancel test only when specifically included in approved plan. Budget closure/cleanup attempts in the plan. Unknown submission resolves by original clientOrderId without replacement-ID resend. No cancel-all on unrelated orders. No position left treated as success. DMS is account-wide: explain any activation in exact plan and require dedicated account scope; never use expiry tests against an existing live account. No transfers, leverage/account-setting changes, or automated services.

## Acceptance

Separate implemented/offline_verified/production_rest_verified/private_ws_verified/native_account_verified/production_execution_verified. Actual live execution reports No Trade/Partial/Filled/Uncertain, own terminal orders and final position reconciliation, fees/slippage (unknown retained). Build runtime identity, generated stubs and candidate tests must match source. Preserve prior wheel and all unrelated work; no commits/merge/push unless user separately requests.

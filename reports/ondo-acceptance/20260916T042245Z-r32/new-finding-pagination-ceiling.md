# New finding — the order and fill walks have an unrecorded page ceiling

Found during R3.2 acceptance on 2026-09-16. **Not introduced by R3.2** — attribution below. Not a
blocker for R3, but it is a latent hard stop that must be on the record before any long-running
deployment, which the plan does not yet authorise.

## What the code does

`REPORT_MAX_PAGES: usize = 100` (`crates/adapters/ondo/src/execution.rs:199`).

`read_orders` (`:2912-2938`) and `read_fills` (`:2940-…`) both:

- build their query with `OndoPrivateReadQuery::new()` — **no `startTime`/`endTime` window and no
  `limit`**, so the venue's own default page size applies and the range is "all history"; and
- walk with `CursorWalk::new(REPORT_MAX_PAGES)`, following `nextCursor` from the **newest** page
  backwards, until the cursor ends or the cap is reached.

The same shape appears in `read_funding` (`:3105`, added by R3.2) and, with a status filter and a
sent window, in `generate_order_status_reports` (`:4226`).

## Why it matters

1. **A hard stop above the cap.** An account whose order or fill history exceeds 100 pages fails the
   walk on **every** pass. The read error fails the pass, the account never converges, and the client
   never reaches `TradingReady`. This is fail-closed — it never reports a false-clean account — but
   it is permanent, not transient.
2. **A standing cost below the cap.** Every pass performs as many REST round trips as there are
   pages of history, every `reconcile_interval_secs` (default 30s), against the shared rate budget
   the plan tracks. The walk does not stop at what is new.

The ceiling in *records* is not even knowable from the repository, because no `limit` is sent — it
depends on a venue default the frozen spec does not pin. So "100 pages" is not "100 records" and
cannot be bounded by inspection.

## Attribution — this is not an R3.2 regression

The R3.2 hunks in `execution.rs` add nothing between new-line 2868 and 3074, so `read_orders` and
`read_fills` fall entirely in untouched context: they are R1/R2 code. R3.2 copied an existing
pattern into `read_funding`.

R1 did dispose of pagination **completeness**: its acceptance report records "成交历史**走完分页**且
每条成交只应用一次" (`tests/reconciliation.rs:3321`) and "成交分页走不完 → 账户不确定" (`:3091`), and
plan line 121 assigns "orders/fills 的分页完整性单独验证" to R1. What R1 recorded is the *semantics*
of a completed or failed walk. What was never recorded is the **operational ceiling** — that an
ordinary account's history can exceed the cap, and what happens then.

## Why it is not being fixed inside R3

R3's scope is the private runtime, account output and DMS. More importantly the fix is not the
funding fix: fills are immutable and an applied fill stays applied, so a funding-style baseline cut
transfers directly, but **order statuses mutate**. "Already seen" is not a sufficient stop condition
for orders — a known order id carrying a *changed* status is exactly the signal the walk exists to
carry. Cutting the order walk correctly needs its own argument about which venue field orders an
account's status recency, and the frozen spec's ordering guarantee is the thing that would have to
carry it.

## What to do when it is taken up

- Establish what the venue's default page size is, or send an explicit `limit` whose semantics the
  spec actually documents.
- For fills: stop once a page yields nothing new (the funding cut, and for the same reason).
- For orders: derive the stop condition from the venue's ordering guarantee, not from "already
  seen", and keep `REPORT_MAX_PAGES` as the backstop rather than the mechanism.
- Whatever the cut, keep the R1 property: a walk that cannot complete makes the account uncertain,
  and the bounded walk must be *argued* to still return every record the judgment consumes.

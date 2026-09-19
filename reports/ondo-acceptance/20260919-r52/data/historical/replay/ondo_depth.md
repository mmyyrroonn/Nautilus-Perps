# Ondo depth analysis — same-quantity VWAP (ondo_depth, plan 5.2 / P2)

run `E:\Nautilus-Perps\reports\ondo-acceptance\20260915T024236Z-p2`  |  l2 `E:\Nautilus-Perps\reports\ondo-acceptance\20260915T024236Z-p2\l2`  |  mode read-only  |  generated 2026-09-19T09:28:30+00:00

markets NVDA, TSLA  |  directions ONDO>HL, HL>ONDO  |  notionals $100/500/1000  |  thresholds: receive age 2000 ms, event skew 500 ms, future tolerance 1000 ms, event age not enforced  |  reserve 5.0 bps  |  quantity from the sell leg's best bid, common step integer_scaled_lcm_of_real_size_increments  |  funding bps_per_hour  |  exit unclosed

**No row here is executable.** `mapping_verified` is False for every pair compared (`mapping_unverified`) and `executable` is always False: this is a nominal comparison of two books, not an order promise.

**A fresh receipt is not a fresh venue event.** a fresh local receipt never proves a fresh venue event: the local clock offset to either venue is NOT measured (clock_offset_unknown), so receive_age_sell_ms / receive_age_buy_ms are local RECEIVE ages and are never network latency; read them beside event_age_sell_ms / event_age_buy_ms, which can be minutes while the receive age is one millisecond, and remember that the event age itself carries the unmeasured offset between the local clock and the venue's.

No hit (entry_after_fees_bps > 0) was recorded; 0 of at most 200 sample rows are stored. Zero opportunities is a result, not a missing section.

## Fee snapshot (from this run's tape, never today's registry)

`taker bps` is a value only when every usable instrument record in the tape agreed on one; when the run changed its metadata mid-tape the values it announced are listed and no single value is elected (F06). `increment` is the real `size_increment` - `quantity_step_unknown` means the tape carried none and `10**-size_precision` was therefore not used.

| market | venue | taker bps | fee values | source | increment | step | origin |
|---|---|---|---|---|---|---|---|
| NVDA | ONDO | 2.50 | 2.50 | instrument_metadata | unknown | unknown | quantity_step_unknown |
| NVDA | HL | 0.90 | 0.90 | registry | unknown | unknown | quantity_step_unknown |
| TSLA | ONDO | 2.50 | 2.50 | instrument_metadata | unknown | unknown | quantity_step_unknown |
| TSLA | HL | 0.90 | 0.90 | registry | unknown | unknown | quantity_step_unknown |

## Tape verdict

| tape | symbol | fragments | records | sessions | complete | gaps | dropped |
|---|---|---|---|---|---|---|---|
| l2_NVDA_ONDO-HL_20260915T024236Z.jsonl | NVDA | 1 | 716 | 1 | true | 0 | 0 |
| l2_TSLA_ONDO-HL_20260915T024236Z.jsonl | TSLA | 1 | 629 | 1 | true | 0 | 0 |

## NVDA

### ONDO>HL

| notional | samples | quality pass | pass | reject | hits | exec-files | median gross bps | median after fees bps | max event age sell/buy ms | top reject |
|---|---|---|---|---|---|---|---|---|---|---|
| $100 | 683 | 63 | 0 | 683 | 0 | 0 | unknown | unknown | 1158.686141/6732.579536 | stale_book |
| $500 | 683 | 63 | 0 | 683 | 0 | 0 | unknown | unknown | 1158.686141/6732.579536 | stale_book |
| $1000 | 683 | 63 | 0 | 683 | 0 | 0 | unknown | unknown | 1158.686141/6732.579536 | stale_book |

totals: samples 2049, quality-pass 189, pass 0, reject 2049, hits 0 (executable 0), zero-opportunity true  |  reject reasons: stale_book 1233, event_skew 621, quantity_step_unknown 189, no_book 6
*the multiplier / settlement-asset / underlying equivalence of this pair is not verified anywhere in this repository (plan 5.2), so the row is a nominal comparison and never an executable conclusion*

### HL>ONDO

| notional | samples | quality pass | pass | reject | hits | exec-files | median gross bps | median after fees bps | max event age sell/buy ms | top reject |
|---|---|---|---|---|---|---|---|---|---|---|
| $100 | 683 | 63 | 0 | 683 | 0 | 0 | unknown | unknown | 6732.579536/1158.686141 | stale_book |
| $500 | 683 | 63 | 0 | 683 | 0 | 0 | unknown | unknown | 6732.579536/1158.686141 | stale_book |
| $1000 | 683 | 63 | 0 | 683 | 0 | 0 | unknown | unknown | 6732.579536/1158.686141 | stale_book |

totals: samples 2049, quality-pass 189, pass 0, reject 2049, hits 0 (executable 0), zero-opportunity true  |  reject reasons: stale_book 1233, event_skew 621, quantity_step_unknown 189, no_book 6
*the multiplier / settlement-asset / underlying equivalence of this pair is not verified anywhere in this repository (plan 5.2), so the row is a nominal comparison and never an executable conclusion*

## TSLA

### ONDO>HL

| notional | samples | quality pass | pass | reject | hits | exec-files | median gross bps | median after fees bps | max event age sell/buy ms | top reject |
|---|---|---|---|---|---|---|---|---|---|---|
| $100 | 596 | 62 | 0 | 596 | 0 | 0 | unknown | unknown | 1139.089813/6716.345136 | stale_book |
| $500 | 596 | 62 | 0 | 596 | 0 | 0 | unknown | unknown | 1139.089813/6716.345136 | stale_book |
| $1000 | 596 | 62 | 0 | 596 | 0 | 0 | unknown | unknown | 1139.089813/6716.345136 | stale_book |

totals: samples 1788, quality-pass 186, pass 0, reject 1788, hits 0 (executable 0), zero-opportunity true  |  reject reasons: stale_book 1008, event_skew 588, quantity_step_unknown 186, future_event_time 3, no_book 3
*the multiplier / settlement-asset / underlying equivalence of this pair is not verified anywhere in this repository (plan 5.2), so the row is a nominal comparison and never an executable conclusion*

### HL>ONDO

| notional | samples | quality pass | pass | reject | hits | exec-files | median gross bps | median after fees bps | max event age sell/buy ms | top reject |
|---|---|---|---|---|---|---|---|---|---|---|
| $100 | 596 | 62 | 0 | 596 | 0 | 0 | unknown | unknown | 6716.345136/1139.089813 | stale_book |
| $500 | 596 | 62 | 0 | 596 | 0 | 0 | unknown | unknown | 6716.345136/1139.089813 | stale_book |
| $1000 | 596 | 62 | 0 | 596 | 0 | 0 | unknown | unknown | 6716.345136/1139.089813 | stale_book |

totals: samples 1788, quality-pass 186, pass 0, reject 1788, hits 0 (executable 0), zero-opportunity true  |  reject reasons: stale_book 1008, event_skew 588, quantity_step_unknown 186, no_book 6
*the multiplier / settlement-asset / underlying equivalence of this pair is not verified anywhere in this repository (plan 5.2), so the row is a nominal comparison and never an executable conclusion*

## Event age vs receive age (local epoch vs venue clock, per example row)

`event_age_sell_ms` / `event_age_buy_ms`: the local epoch receipt time of the record being evaluated (ts_init_ns) minus the leg's own book venue event time (ts_event_ns), in exact integer-nanosecond milliseconds: it is the age measured on the local clock, so two legs delayed together by the same feed never cancel out to 0, and a missing/unusable stamp is 'unknown' (never 0). A negative value is kept: it is clock evidence (the venue's stamp is ahead of the local receipt), never floored to 0 and never made absolute. They are printed beside the receive ages so a book that is minutes old on the venue's clock while its local receipt is fresh is visible instead of passing as a fresh quote - and because the age is measured against the record's own local receipt, two legs delayed together report their full age instead of cancelling out to 0. Each bucket's own table also reports the largest event age it saw on each leg (`event_age_sell_ms_max` / `event_age_buy_ms_max`), which no bounded sample of example or hit rows can hide, and every row carries `event_age_status`: null means the event age is measured and published on every row but never enforced: the age is measured on the local clock, so it carries the unmeasured local offset to the venue's clock, and a threshold is a decision the caller has to make explicitly (--max-event-age-ms).

*No row computed a quantity, so no event age was measured; the receive ages of the moments that were evaluated are in the buckets above.*

## Field names (plan 5.2)

- `gross_entry_bps`: the entry spread at the VWAP of both legs: (sell_vwap - buy_vwap) / reference_price * 10000, so it is exactly the watcher's gross_bps but with each leg walked for the same base quantity
- `entry_fees_bps`: the two entry taker fees (one per leg) from THIS run's tape; 'unknown' when the run published no fee for a leg, in which case the cost-qualified judgement is withheld rather than falling back to a dated documentation assumption (a number that is only ever valid for a dry run or for a CSV that labels itself an assumption, and that this report therefore never repeats)
- `entry_after_fees_bps`: gross_entry_bps - entry_fees_bps (entry only)
- `exit_fee_assumption_bps`: the same two taker fees charged again for the exit, at the same nominal: an ASSUMPTION because P2 has no exit evidence
- `reserve_bps`: the one-leg failure reserve the watcher charges at entry
- `funding_estimate_bps`: the two legs' latest funding rates, normalised to bps per hour with the repository's own scale/intervals, sell leg minus buy leg; 'unknown' when the run carried no funding record for a leg
- `quality_ok`: the quality gate's verdict for this moment (freshness, skew, book sanity, disconnect, metadata, gap) - not a statement about profitability
- `mapping_verified`: whether the multiplier / settlement / underlying equivalence of this pair is verified; False means the row is a nominal comparison only
- `reject_reason`: why this row is not a usable comparison: a gate reason (recording_gap, no_book, empty/one_sided/crossed book, metadata_unknown, metadata_stale, disconnected, market_halted, session_mismatch, unknown_time, future_event_time, stale_book, event_skew, event_age_exceeded), or fee_unknown / insufficient_depth / below_one_step / quantity_step_unknown; empty when it passed. The three axes of F07 report separately: disconnected is the local feed, market_halted the venue's trading state, metadata_* the instrument facts
- `executable`: always False: this is a research observation, never an order promise
- `receive_age_sell_ms`: the sell leg's own depth age at this record (per arrival)
- `receive_age_buy_ms`: the buy leg's own depth age at this record (per arrival)
- `event_age_sell_ms`: the sell leg's own VENUE event age at this record - the local epoch receipt time of the record being evaluated (ts_init_ns) minus the leg's own book venue event time (ts_event_ns), in exact integer-nanosecond milliseconds: it is the age measured on the local clock, so two legs delayed together by the same feed never cancel out to 0, and a missing/unusable stamp is 'unknown' (never 0). A negative value is kept: it is clock evidence (the venue's stamp is ahead of the local receipt), never floored to 0 and never made absolute; carried on every emitted row (the per-bucket examples and the stored hits), and it is no part of the bounded median sample, which is unchanged
- `event_age_buy_ms`: the buy leg's own VENUE event age at this record - the local epoch receipt time of the record being evaluated (ts_init_ns) minus the leg's own book venue event time (ts_event_ns), in exact integer-nanosecond milliseconds: it is the age measured on the local clock, so two legs delayed together by the same feed never cancel out to 0, and a missing/unusable stamp is 'unknown' (never 0). A negative value is kept: it is clock evidence (the venue's stamp is ahead of the local receipt), never floored to 0 and never made absolute; carried on every emitted row (the per-bucket examples and the stored hits), and it is no part of the bounded median sample, which is unchanged
- `event_age_status`: 'not_checked' when no event-age threshold is configured (the default: the local clock offset to either venue is unmeasured, so an age threshold would turn a clock skew into a market conclusion), 'unknown' when a leg's age is not measurable, else 'exceeded' / 'ok' against max_event_age_ms. Published on every row so a report can never imply that an unconfigured threshold was passed
- `clock_offset_unknown`: a fresh local receipt never proves a fresh venue event: the local clock offset to either venue is NOT measured (clock_offset_unknown), so receive_age_sell_ms / receive_age_buy_ms are local RECEIVE ages and are never network latency; read them beside event_age_sell_ms / event_age_buy_ms, which can be minutes while the receive age is one millisecond, and remember that the event age itself carries the unmeasured offset between the local clock and the venue's
- `common_step`: the integer-scaled LCM of the two legs' quantity steps (never the maximum), so both venues can represent the quantity exactly. Each leg's step is its own instrument's real size_increment as of this arrival, or the caller's CLI override; a step is never inferred from size_precision
- `step_origin`: where this row's quantity step came from: 'instrument_metadata' (both legs' own size_increment), 'override' (the caller's --steps declaration, which is never a statement that the venue verified anything), 'mixed' (one leg each) or 'quantity_step_unknown'
- `session_id`: the tape session this row was priced in. Metadata is announced per session: a session that published no instrument record is metadata_unknown for its whole span and never inherits another session's fee or step

## What P2 acceptance still needs

- a real recording run (`spread_watch.py --symbols NVDA,TSLA --venues ONDO,ASTER --record-l2 --out <runDir>`, plan §8) so this analysis has live fragments: every number above is only as good as the tape it was read from
- the exit side: a reverse book after entry (`unclosed` here) and a realised funding interval, before any round-trip number may be quoted
- the mapping verification: until the multiplier / settlement / underlying equivalence of ONDO and ASTER is verified with evidence, no row may be called executable

# §11.6 Dispatch Status — FDE Primer

## What this is

§11 polling already fetches per-order status from EasyEcom every 5
minutes. Phase 1 only acted on status_id=9 (Cancelled) and threw away
the dispatch transitions. §11.6 closes that gap: when EE reports
Shipped / Delivered / Returned, we stamp four fields on the linked
Sales Invoice so ERPNext ops can see fulfilment state without flipping
to EE's UI.

This is intentionally lightweight — no Delivery Note creation, no
inventory hooks, no workflow transitions. Just visibility.

---

## What lands where

Four Custom Fields on **Sales Invoice** (inside the existing "EasyEcom
Integration" collapsible section, after `ecs_easyecom_b2b_order_map`):

| Field | When written | Notes |
|---|---|---|
| `ecs_easyecom_dispatch_status` | Every poll where EE reports a known status_id | Pending / Shipped / Delivered / Returned / Cancelled |
| `ecs_easyecom_dispatched_at` | First time we observe status_id=5 (Shipped) OR =6 (Delivered, backfilled) | Set-once; never overwritten |
| `ecs_easyecom_delivered_at` | First time we observe status_id=6 | Set-once; never overwritten |
| `ecs_easyecom_tracking_url` | Whenever EE provides a tracking link in the payload | Overwritten on change (couriers can switch mid-flight) |

**Status_id → label mapping** (from polling.py `DISPATCH_STATUS_BY_ID`):

| EE status_id | Label | Meaning |
|---|---|---|
| 1, 2, 3, 4, 30 | Pending | In EE, not yet shipped |
| 5 | Shipped | Handed to courier |
| 6 | Delivered | POD received |
| 7 | Returned | Returned to origin |
| 9 | Cancelled | Cancelled on EE side |

---

## How ops uses this

**Report:** Desk → Reports → "B2B Dispatch Status"
- Filters: Company (mandatory), Posting Date range, Dispatch Status multi-select
- Default sort: Pending first, then Shipped, then Delivered — so stuck orders bubble up
- Age (days) column turns red when `Pending` or `Shipped` rows are >7 days old

**Form view:** open any B2B Sales Invoice → expand "EasyEcom
Integration" section → see dispatch status, timestamps, and a clickable
tracking URL.

---

## Edge cases handled

- **Pre-§11.6 SIs**: SIs created before this patch don't have the four
  fields populated. The first polling tick after migrate stamps them
  (if EE still has the order's history). Older orders past their
  polling eligibility window stay blank — they're terminal and ops
  doesn't need the state.
- **Mode 1 SIs (Custom GSP)**: dispatch fields written through the
  same Map.sales_invoice link the gsp_handler sets at SI creation.
- **Mode 2 SIs (mirror)**: same; the mirror flow sets Map.sales_invoice.
- **Shipment-split orders** (multiple businessorder rows per
  reference_code): the latest-by-last_update_date row wins.
- **Fast shipping** (Pending → Delivered between two polls, never
  observed Shipped): `dispatched_at` is backfilled to the
  Delivered-observation timestamp so the field is never empty for a
  delivered SI.
- **Unknown status_id**: silently skipped — the existing derivation
  function already raises a Discrepancy for unknown values.
- **SI without §11.6 fields installed** (rolling deploy mid-migrate):
  the stamper bails silently rather than breaking the polling tick.

---

## What's NOT in this build (by design)

- **No Delivery Note creation.** §11.6 lightweight stamps fields; it
  doesn't create or update DN records. If a client wants full DN-based
  fulfilment tracking, that's a follow-up (would need warehouse / item
  resolution, accounting hooks, and an opt-in toggle).
- **No inventory adjustments.** Stock leaves on SI submit (the existing
  perpetual-inventory pathway). Delivered-status stamping does NOT
  re-touch ledgers.
- **No alert on "Stuck > N days"**: the report's red-text-on-7+-days
  is visual only. If clients want a §22 alert when Shipped orders sit
  past N days, that's a one-line addition to the alerts router.
- **No EE webhook-driven updates.** Phase 1 is polling-only.
  §11.6 inherits that — when EE eventually exposes dispatch webhooks,
  the stamping function plugs in unchanged.
- **No history-aware traversal.** If EE reports Shipped → Returned
  between two polls (returned without our Shipped observation), we
  stamp Returned and never set `dispatched_at` / `delivered_at`.
  History-aware polling is a separate Phase 2 enhancement.

---

## Operational quick reference

| Symptom | What it means | Action |
|---|---|---|
| Dispatch fields blank on a brand-new SI | Polling hasn't run yet (next tick within 5 min) | Wait one polling cycle |
| Status stuck at `Pending` past Posting Date + 3 days | EE has the order but hasn't shipped it | Check EE-side warehouse / picklist |
| Status stuck at `Shipped` past 7 days (red age) | Courier likely lost / delayed | Open `tracking_url` to verify; contact courier if needed |
| Status = `Returned` | EE marked status_id=7 | Decide whether to issue a Credit Note; §11 cancellation flow handles full cancellations separately (status_id=9) |
| `delivered_at` empty on a `Delivered` row | Shouldn't happen — bug | File issue |
| `tracking_url` empty when ops expects one | EE didn't include a tracking field in the payload OR the field name differs | If consistent across an EE Account, add the field name to `TRACKING_URL_CANDIDATE_KEYS` in polling.py |

---

## Origin

- §11 Phase 1 completion checklist deferred §11.6 to Phase 2
- 2026-06-29 — built as a lightweight option per user direction (no
  Delivery Note auto-create, Custom Fields only)
- Test coverage: 19 unit tests in `test_b2b_dispatch_status.py`
- 0 regressions on the existing 25 polling tests

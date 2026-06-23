# §11 B2B Sales (Phase 1) — Completion Checklist

State map for §11 Phase 1 as of 2026-06-23. Mirrors the format of
`SECTION_9_COMPLETION_CHECKLIST.md` / `SECTION_10_COMPLETION_CHECKLIST.md`.

## Status: ✅ §11 PHASE 1 BUILD COMPLETE · LIVE-VERIFIED · CLOSEOUT DONE

Phase 1 covers SO push (Async + cancel), polling reconciliation, and the
FDE worklist surface — the left half of the §11 SPEC flow diagram. Phase
2 picks up the SRE mirror, invoice flow (Branch A / B), dispatch →
Delivered transition, multi-warehouse split, and sync push mode.

## Build stages — ALL DONE

| Stage | What | Status | Commit(s) |
| --- | --- | --- | --- |
| 1 | Substrate — `EasyEcom B2B Order Map` DocType + pure-function payload builders (Old + New B2B) | ✅ committed | `4186c04` |
| 2 | Push + cancel + hooks (on_submit → SO Push Queue Job, Shipped-state cancel refusal, singleton-Account hotfix) | ✅ committed | `cbbd42b`, `4b7e73e`, `1be1c83` |
| Stage-3 probe | `getAllOrders` endpoint probe + 7-day-window cap finding | ✅ committed | `0266935` |
| 3 | Polling reconciliation + scheduler (cron */5) + FDE Worklist cards + SO form branch chip | ✅ committed | `52526e6`, `845a9be`, `2abd7d8`, `3d1b44b` |
| Corrective | Polling derivation field rename `suborders` → `order_items` + real Harmony fixture | ✅ committed | `2d7d011`, `4b53000` |

## Closeout artifacts — ALL SHIPPED

- ✅ `spec_sections/SPEC_11_patch_notes.md` (7 inline corrections)
- ✅ `spec_sections/SECTION_11_COMPLETION_CHECKLIST.md` (this file)
- ✅ `process/primers/FDE_PRIMER_section_11_b2b_sales.md` (operator manual)
- ✅ `process/test_scripts/section_11_b2b_sales.md` (FDE-runnable test plan)
- ✅ `tests/fixtures/b2b_polling_real_response.json` (real Harmony response, GSTIN-redacted)
- ✅ `process/BUILD_TRACKER.md` (§11 row updated)
- ⏳ docx regen (USER runs unpack/edit/pack pipeline locally against patched SPEC.md)

## Live-verified on Harmony

Captured during build (smoke-test.local against Harmony sandbox):

| Paste | What | State |
|---|---|---|
| 1 | Old B2B push | ✅ |
| 2 | New B2B push payload | ✅ |
| 3 | Cancel from ERPNext side | ✅ live-verified |
| 6 | Orphan probe (`getOrderDetails` for non-existent ref) | ✅ |
| 7 | Full `getOrderDetails` response shape (the `order_items` / `easyecom_order_history` grounding) | ✅ captured 2026-06-23, persisted as fixture |

## Verified after Phase 1 build but deferred to Phase 2 verification

- Paste 8 — New B2B identifier correlation (the `OrderID` / `SuborderID`
  / `InvoiceID` round-trip after push + immediate `getOrderDetails`).
  Straight validation; no code changes anticipated. Run before Phase 2
  invoice-flow build begins.
- Paste 9 — EE-side cancel detection (cancel an order in Harmony's UI,
  verify next polling tick produces the "B2B order cancelled by EE —
  polling-detected" Discrepancy). The substrate path
  (`derive_local_status_from_ee_rows` returning
  `("transition_to", "Cancelled")`) is unit-tested; the live
  reconciliation is the post-merge validation.

## Tests at merge

- 19 unit polling-derivation tests (incl. 3 new fixture-driven against
  real Harmony shape)
- 5 integration polling-reconcile tests
- Stage 1 / Stage 2 substrate + hook tests (existing, all green)
- All run with `bench --site smoke-test.local run-tests --module
  ecommerce_super.tests.unit.test_b2b_polling_derivation` and the
  parallel integration / push / cancel modules.

## Deferred PAST Phase 1 — by design (Phase 2 scope)

These are §11 SPEC items not built in Phase 1. The boundary was set by
the packet's design-lead approval; each is genuinely Phase 2 work, not
a defect:

- **§11.3.2 Sync push mode** — per-Marketplace-Account
  `push_so_mode = Sync` blocking submit until EE confirms. Phase 1 is
  async-only (default).
- **§11.4 Stock Reservation Entry mirror** — EE webhook
  `inventory.reserved` → Stock Reservation Entry against the SO.
- **§11.5.1 Branch A invoice request flow** — EE webhook
  `invoice.requested` → ERPNext Sales Invoice + e-invoice IRN +
  e-waybill (via India Compliance) → push back to EE.
- **§11.5.2 Branch B invoice mirror flow** — EE-generated invoice
  mirrored to ERPNext SI with 1% variance check.
- **§11.6 SI → Delivered status** — dispatch event flips SI status.
- **§11.7 Multi-warehouse SO** — split into separate EE orders per
  location_key.
- **History-aware polling derivation** — walk in-row
  `easyecom_order_history` for richer state-transition detection
  (intermediate Shipped → Returned cycles, etc.). Phase 1's
  top-level-snapshot derivation is correct but conservative.

## Watch-items — unit-verified, NOT live-smoked

- **Multi-shipment-split derivation** — Phase 1 derivation iterates
  `b2b_rows` and aggregates `order_items[].cancelled_quantity`; the
  unit tests cover the multi-row aggregation, but a real EE shipment
  split (same `reference_code`, multiple `invoice_id` rows) hasn't
  been seen on Harmony yet. Surface as a test-script item for the
  first real client whose B2B orders split.
- **`Old B2B` push path against a real Old-B2B Account** — Phase 1
  substrate dispatches per `ecs_b2b_module = "Old B2B"` to
  `/Wholesale/createOrder`, but MMPL is on New B2B so the
  live-verified runs all used the new path. Old B2B is unit-tested
  via fixture comparisons; first deployment with `Old B2B` set should
  re-smoke.

## What's next

**§11 Phase 2** — the invoice / SRE / dispatch half of §11 (SPEC sections
11.4 – 11.7). Then §12 (B2C / D2C / Marketplace) which depends on the
same channel-resolution mechanism. Phase 2 build packet should start
from a `SPEC.md §11` that has the 7 patch-notes folded in.

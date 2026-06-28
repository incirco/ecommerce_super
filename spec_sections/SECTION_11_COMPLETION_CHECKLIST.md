# §11 B2B Sales — Completion Checklist

State map for §11 (Phase 1 + Phase 2 work).
Mirrors the format of `SECTION_9_COMPLETION_CHECKLIST.md` /
`SECTION_10_COMPLETION_CHECKLIST.md`. Phase 1 baseline is 2026-06-23;
Phase 2 progress is dated per-row.

## Status: ✅ PHASE 1 COMPLETE · 🟢 PHASE 2 IN PROGRESS (4 of 7 items)

Phase 2 progress as of **2026-06-29**: §11.5.1, §11.5.2, §11.6, plus the
two §11 polishes (orderDate IST offset, polling ID backfill, fast-confirm
queue check) all shipped. §11.4 parked. §11.7 not required. §11.3.2
deferred until first client request. See "Phase 2 progress" section
below for detail.

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

## Phase 2 progress (2026-06-28 / 2026-06-29)

Done this iteration:

| Spec ref | What | PR | Status | Notes |
|---|---|---|---|---|
| §11 polish | `orderDate` IST `+05:30` offset on createOrder body | #100 | ✅ merged | Patch note 8 |
| §11 polish | Polling backfills missing OrderID/SuborderID/InvoiceID on Map | #101 | ✅ merged | Patch note 9 |
| §11.3.5 | Fast-confirm queue check (60× latency reduction on New B2B push) | #102 | ✅ merged | Patch note 10 |
| §11.5.2 | Mode 2 — EE-generated invoice mirror with 1% variance check | #103 | ✅ merged | Patch note 11. Polling-driven (not webhook), no DN auto-creation |
| §11.5.1 | Mode 1 Custom GSP — we are the GSP, ERPNext mints IRN | #104 | ✅ merged | Patch note 12. Includes mint toggles + Print Format selector |
| §11.6 | Dispatch status mirror on SI (lightweight) | #105 | ✅ merged | Patch note 13. Custom Fields + report, NOT workflow + DN |

Spec-vs-build deltas are captured in `SPEC_11_patch_notes.md` entries
8-13. Three of these (Mode 2, Custom GSP, §11.6) represent significant
architectural choices that diverge from spec wording — fold-back is a
methodology-team task.

## Phase 2 scope decisions — explicit park / defer

These were considered this iteration and intentionally not built:

- **§11.4 Stock Reservation Entry mirror** — 🅿️ **parked** (2026-06-29).
  User direction: "not possible." Webhook-driven SRE mirror would
  require EE webhook receivers (not yet in §11 substrate) plus the
  oversell race-window mitigation work. Re-evaluate when a client's
  multi-channel oversell pattern surfaces.
- **§11.7 Multi-warehouse SO split** — 🅿️ **not required** (2026-06-29).
  User direction; no client has multi-warehouse B2B SOs today.
- **§11.3.2 Sync push mode** — ⏸️ **deferred until first client ask**
  (2026-06-29). The config fields (`push_so_mode = Async | Sync` +
  `push_so_block_on_error`) already exist on `EasyEcom Account`;
  only the code path needs wiring. Half-day add when requested.
  Building speculatively means a `Sync` branch nobody runs and silent
  bit-rot.
- **History-aware polling derivation** — ⏸️ **deferred**. Phase 1's
  top-level-snapshot derivation is correct but conservative. Walk
  in-row `easyecom_order_history` to detect intermediate
  Shipped → Returned cycles. Re-evaluate after §11.6 has a few
  months of live data showing which transitions are actually lost.
- **EE webhook receivers** (cancel, invoice.generated, dispatch.confirmed)
  — ⏸️ **deferred to Phase 3**. Polling is the recovery path for all
  EE-side state changes across §11; the webhook substrate plugs into
  the same handler functions when EE exposes the events.
- **§11.6 heavier option — Frappe Workflow + DN auto-creation** — ⏸️
  **deferred**. Lightweight Custom Fields shipped per design call;
  if/when a client asks for proper workflow gating + DN-based
  fulfilment, the spec text (lines 2640, 2544) already describes
  the heavier option.

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

§11 Phase 2 is **substantially complete** as of 2026-06-29 — the four
high-value items (Mode 1 Custom GSP, Mode 2 mirror, §11.6 dispatch
status, plus three polishes) shipped. The parked / deferred items
above represent the residual surface; none are blocking client
deployment.

**Next focus:**

1. **Methodology fold-back** — patch notes 1-13 need to land in
   `SPEC.md §11`. The Phase 3 build packet (if any) should start from
   the patched spec so the assumptions don't recurse.
2. **§12 (B2C / D2C / Marketplace)** — depends on the same
   channel-resolution mechanism §11 uses. The §11 patch-notes that
   touch shared substrate (§7 endpoint contract, polling cron,
   webhook substrate) need fold-back before §12 build starts.
3. **Live re-smoke of the four Phase 2 PRs against Harmony** —
   each landed with unit tests + (where applicable) FDE primers, but
   a consolidated end-to-end smoke covering Push → Polling →
   Dispatch stamp → Mode 1 Custom GSP invoice mint → cancellation is
   the highest-leverage verification before mmpl16 / Thuraya
   onboarding.

## Primers / operating manuals shipped

| Doc | Covers | Audience |
|---|---|---|
| `process/primers/FDE_PRIMER_section_11_b2b_sales.md` | §11 Phase 1 baseline (push, cancel, polling) | FDE |
| `process/primers/FDE_PRIMER_section_11_5_1_custom_gsp.md` | §11.5.1 Mode 1 setup checklist | FDE |
| `process/primers/GUIDE_custom_gsp_invoice_flow.md` | Comprehensive Custom GSP guide — EE's contract, our endpoints, auth, idempotency, toggles, Print Format | FDE + EE-side integrator |
| `process/primers/FDE_PRIMER_section_11_6_dispatch_status.md` | §11.6 dispatch status fields + report | FDE / Ops |
| `process/test_scripts/section_11_b2b_sales.md` | FDE-runnable smoke test plan | FDE |

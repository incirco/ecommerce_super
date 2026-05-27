# 9 — Buying / GRN (§9) — Build Packet

*First operational flow, post-§8 masters. Build stage-by-stage; each green + committed (local) + reviewed before next. Stage discipline mirrors the 8e/8f packets. Grounded in real Harmony CreatePurchaseOrder / updatePoStatus / Grn/V2/getGrnDetails payloads.*

> Single-writer rule. No real EE writes during dev/test except Harmony (disposable). The pre-existing SPEC.md §9.1–§9.12 is the **pre-payload-grounded** design; this packet supersedes it. SPEC.md is rewritten via a §9 patch-notes file at §9 closeout, not before.

## The model (settled, payload-grounded)

**Gate 0 (lifecycle-wide): warehouse opt-in.** A PO is in §9 scope iff its target warehouse maps to an EE Location (has `location_key`, §8a). Else the integration **never** touches the PO — create/amend/submit/cancel — silently inert, no Sync Record, no flag. Same Gate-0 short-circuit on the GRN pull: GRN whose `inwarded_warehouse_c_id` doesn't resolve to a mapped Location is silently skipped, not failed. Validation chain (short-circuits): (1) target warehouse EE-mapped → (2) Supplier Map.ee_vendor_id exists → (3) Item Map per line → (4) field validations. PO warehouse is fixed for lifetime; non-EE↔EE warehouse amendments are blocked at validate.

**Two push channels, two keys** (the key modeling point):
- **Content** → `POST /WMS/Cart/CreatePurchaseOrder`, keyed `referenceCode` (= ERPNext PO name, stable, ERPNext-born). create/update via `createOrUpdate` "I"/"U" flag. Returns `data.poId` (int).
- **Status** → `POST /wms/updatePoStatus`, keyed `po_id` (= EE-returned int). All state transitions including cancel. `isCancel` flag on CreatePurchaseOrder is **unused** (clean channel separation).

PO Map stores BOTH keys per PO (parity with 8f's two-id model).

**Receipt half (the corrected payload-grounded model):**
- Endpoint: `GET /Grn/V2/getGrnDetails` (NOT the SPEC's stale `/wms/getGrnDetails`). Cursor pagination via `nextUrl`, delta watermark `created_after`, default limit 5 / max 10. Bearer JWT + x-api-key.
- **No accepted_qty/rejected_qty pair in payload** (SPEC §9.6.2 assumed there was — wrong). Real qty model: `received_quantity` (the receipt event) + post-receipt buckets (`available, reserved, sold, qc_fail, damaged, …`) that DRIFT after inward. The buckets are NOT receipt facts (real payload: grn 141653 received 100, available 40, sold 60 — 60 units sold post-receipt). The buckets are READ but NOT POSTED — ERPNext owns stock post-receipt; mirroring would double-count.
- **Reject split = `qc_fail`**, QC-conditional. PR line: `received_qty = received_quantity`, `rejected_qty = qc_fail`, `accepted_qty = received_quantity − qc_fail`. Accepted → mapped Warehouse; rejected → `default_rejected_warehouse` (settings field NOW load-bearing for §9).
- **Receipt trigger** is `grn_receipt_trigger_status` setting (Select 1/2/3, default 3 QC Complete). Below threshold → Held-Pre-QC, re-evaluated next poll. At trigger=1, `qc_fail`=0 by construction → all qty accepted, no split (field description warns).

**Self-GRN routing (§9 ↔ §10 boundary):** If `vendor_c_id == inwarded_warehouse_c_id`, the GRN is an EE-internal inward (batch loads, system transfers, opening stock), not procurement. §9 creates GRN Map row with `routed_to_stn=1`, status=STN-Routed, NO PR. §10 STN-inward picks up. Real payload: grn 142698/142703/141936 all self-vendor 26564; 141936 has value 49990 with real apparel SKU, so §10 must handle valued self-GRNs (carry-forward).

**PO resolution from GRN payload:** primary via `po_ref_num` → ERPNext PO name (free-text, may be ""/junk — grn 141653 has empty, 141461 has "jghvhgv"); fallback via `po_id` → PO Map → PO name. Both keys earn their place.

**Supplier resolution reverses push:** push uses `Supplier Map.ee_vendor_id` (WRITE key, string); pull uses `Supplier Map.ee_vendor_c_id` (READ key, int). Different fields by design — 8f's two-id model pays off here.

**Status-ID map (full, from EE doc):** 1 Open · 2 Waiting for Approval · 3 Approved · 4 Rejected · 5 Completed · 6 Pending on Supplier · 7 Cancelled · 8 Payment Pending · 9 Payment Done · 11 Shipped to FF · 12 Pending Dispatch on FF · 13 Shipped · 14 Shipped by FF · 15 Received by FF · 16 Invoice done by Vendor. (No 10.)

**ERPNext → push table:**
- on_submit (post-approval) → po_status=3 (Approved)
- Cumulative receipt complete → po_status=5 (Completed)
- ERPNext Close button (force-close partial) → po_status=5, markPoComplete=1
- on_cancel → po_status=7 (Cancelled)
- Everything else NOT pushed by §9.

**EE → ERPNext (observation only, drift detection):** GRN pull carries per-row `po_status_id`. Record on PO Map `ee_observed_po_status` / `ee_observed_at`. Divergence vs `last_pushed_po_status` indicating EE-side action contrary to ERPNext (e.g. EE→4 Rejected when ERPNext shows Approved) → Discrepancy. EE-internal 11–16 recorded but NOT raised as Discrepancy. Never overwrites ERPNext PO.

**Sync Record model:** ONE GRN → ONE PR → ONE Sync Record. Per-line outcomes via `EasyEcom Sync Record Line` child (NEW DocType — the §7.1 amendment's first concrete consumer; entity-agnostic; §11/§12/§13 will reuse). Status enum per line: OK / Failed / Discrepancy. Unmapped SKU on line → whole PR Failed, child names offending line. Post-create line discrepancy (e.g. received > original + tolerance, HSN mismatch) → PR exists, Sync Record Success, line=Discrepancy, linked_discrepancy → Integration Discrepancy (§23).

## Contract (grounded in real payloads — full detail in spec patch notes at closeout)

**CreatePurchaseOrder top-level:** `vendorId` (= Supplier Map ee_vendor_id), `referenceCode` (= PO name), `address`, `expDeliveryDate`, `shippingCost`, `createOrUpdate` (I/U), `isCancel` (always 0), `docNumber`, `updateTaxRate` (1 on amend with tax change). Optional keys present-but-blank.

**Line items[]:** `lineItemNumber`, `sku`/`ean`/`AccountingSku` (one required → Item Map), `quantity`, **`unitPrice` (TAX-INCLUSIVE — gross, computed at push)**, `taxRate`, `taxValue`, **`taxType` (1 IGST / 2 CGST-SGST / 3 Custom — computed from supplier-state vs warehouse-state place-of-supply, shared module `easyecom/tax/place_of_supply.py` for §9/§11/§12)**, `batch_code`, `batch_mrp`, `expiry_date`, `serials[]` (serialization_enabled Location only).

**updatePoStatus body:** `po_id`, `po_status` (int), `markPoComplete` (0/1). Idempotency: skip if `po_status == last_pushed_po_status`.

**getGrnDetails query:** `nextUrl` (mandatory follow-up), `limit=10`, `created_after = last_pull_grn_cursor`. (Also: `grn_ids`/`po_ids`/`grn_status_id`/`invoice_*_date` for replay — operational only, not scheduled.)

**GRN response per row — header fields used:** `grn_id` (idempotency), `grn_status_id` (trigger gate 1/2/3/4), `grn_invoice_number` → PR supplier_delivery_note, `grn_invoice_date` → PR ecs_supplier_invoice_date, `total_grn_value` (cross-check), `grn_created_at` → posting_date/time, `po_id`/`po_ref_num` (PO resolution), `po_status_id` (drift observation), `inwarded_warehouse_c_id` (Gate-0 hinge), `vendor_c_id` (Supplier Map read key).

**GRN line fields used:** `grn_detail_id` (PR line back-ref), `purchase_order_detail_id` (PO line back-ref for cumulative tolerance), `sku`/`ean` (Item Map), `original_quantity`/`received_quantity`/`pending_quantity`, `grn_detail_price` (PR rate, tax-inclusive), `batch_code`/`expire_date`, `qc_fail` (rejected qty), `hsn` (cross-check). All other buckets read-not-posted.

## Repoint dependency (8f-flagged, lands in Stage 1)

- `EasyEcom-PO-Push` ruleset: currently maps `supplier ↔ vendor_id` directly. Repoint → `Supplier Map.ee_vendor_id` (write key, string).
- `EasyEcom-GRN-Pull` ruleset: currently maps `supplier ↔ vendor_id` directly. Repoint → `Supplier Map.ee_vendor_c_id` (read key, int — different field by design).

Retire any stale bidirectional `-Sync` PO/GRN rulesets if present (parity with 8e/8f retiring stale customer/supplier sync rulesets).

## Stages (4, compressed from 6)

**Stage 1 — Substrate** (schema + repoints, NO flow logic, NO EE calls, NO PRs):
- `EasyEcom PO Map` DocType (autoname ECS-PO-{purchase_order}; reference_code unique-required; ee_po_id indexed/nullable; Dynamic Link → Purchase Order; status enum [Mapped/Created-Flagged/FNC/Drift/Disabled] + colours; last_pushed_po_status + ee_observed_po_status + ee_observed_at for drift; reuses **EasyEcom Drift/Exclude Field** child DocTypes from 8f; NO content snapshot — PO docs submitted-immutable, drift only on status).
- `EasyEcom GRN Map` DocType (autoname ECS-GRN-{ee_grn_id}; ee_grn_id unique-required; grn_status_id indexed; inwarded_warehouse_c_id + vendor_c_id indexed; po_ref_num + ee_po_id; Dynamic Link → Purchase Receipt nullable; linked_po_map nullable; routed_to_stn check; status enum [Pending / Receipted / Held-Pre-QC / STN-Routed / Failed / Discrepancy / Deleted-Post-Receipt]).
- `EasyEcom Sync Record Line` child DocType (entity-agnostic: source_line_ref, source_line_number, mapped_target_doc, status [OK/Failed/Discrepancy] required, reason, linked_discrepancy → Integration Discrepancy). Schema-add `lines` child table to `EasyEcom Sync Record` (existing §8 master Sync Records leave it empty — correct, no nested lines).
- Settings field on GRN/Inward Policy section: `grn_receipt_trigger_status` Select 1/2/3, default 3, with QC-conditional caveat in description.
- Ruleset repoints (the two above).
- Tests: DocType CRUD + permissions, naming, status enums, child wiring, settings defaults, ruleset repoint grep + fixture diff, §8 regression (all masters still green; existing Sync Records still save).
- Build report flags: PO Map rename-hook behaviour on Frappe PO rename (verify); §8d standing failures count check (29/32, 7/10 — track only).

**Stage 2 — PO push (content + status, BOTH channels)**:
- Gate 0 hook on PO validate/submit/cancel (short-circuit if non-EE warehouse).
- Content: CreatePurchaseOrder ruleset. Supplier Map write-key resolution. Item Map sku/ean/AccountingSku. taxType computation (shared place_of_supply module). Tax-inclusive unitPrice derivation. createOrUpdate I/U; updateTaxRate=1 on amend with tax change. Capture data.poId → PO Map.ee_po_id.
- Status: separate updatePoStatus ruleset. on_submit → 3; cumulative-complete → 5; force-close → 5+markPoComplete=1; on_cancel → 7. Idempotent on last_pushed_po_status.
- Sync Records + Sync Record Line child populated per item.
- Triggers: auto_push_pos_on_save checkbox default-OFF (parity with masters). Batch sweep "Push All Pending POs" (Company, EE-warehouse, no Map row or status≠Mapped).
- Tests: create/amend (tax change → updateTaxRate=1)/cancel (via po_status=7, not isCancel); missing Supplier Map → FNC; missing Item Map → FNC; idempotent re-push no-op; place-of-supply intra/inter; tax-inclusive round-trip.

**Stage 3 — GRN pull → Purchase Receipt + status reconciliation** (one sweep, both jobs):
- getGrnDetails ruleset (cursor `nextUrl`, `created_after` watermark, limit=10).
- Per-GRN chain: (1) Gate 0 location_key → miss silent skip; (2) STN routing if vendor==warehouse → Map row routed_to_stn=1, no PR; (3) status gate `grn_status_id ≥ grn_receipt_trigger_status` else Held-Pre-QC; (4) Deleted (status 4) special handling — already-receipted → Discrepancy, never → quiet skip; (5) resolve PO (po_ref_num primary, ee_po_id fallback), Supplier (read key), Warehouse (location_key), Items (sku/ean); (6) build PR — received_qty/rejected_qty/accepted_qty per the corrected model; rate=grn_detail_price; tax derivation parity with push; back-refs ecs_easyecom_grn_id/grn_detail_id/po_detail_id; batch/serial/expiry per Item config + §3.3.6 mandatory_*_for_groups overrides; (7) tolerance check cumulative vs original_quantity per purchase_order_detail_id (over/under % settings) → Discrepancy not Failed; (8) PR submit; GRN Map status=Receipted; Sync Record with Line child.
- Status reconciliation (same sweep): per-row `po_status_id` → linked PO Map ee_observed_po_status/at; divergence indicating EE-side action contrary to ERPNext → Discrepancy.
- Completion trigger: if PR brings cumulative received ≥ original (modulo under-tolerance) across all PO lines → Stage 2's po_status=5 push (idempotent — won't re-fire on subsequent GRNs).
- Tests: receipt at status 3; held at status 1/2 then receipted on later poll when 3; STN routing (self-vendor); Gate-0 silent skip; multi-GRN cumulative tolerance; qc_fail split; Deleted-Post-Receipt Discrepancy; po_status drift Discrepancy; idempotent re-pull (same grn_id) no-op; out-of-order (GRN for EE-born PO ERPNext doesn't know yet — linked_po_map empty + Discrepancy).

**Stage 4 — UI / workspace / scheduler**:
- PO Map + GRN Map list views (status colours + filters, parity with master maps).
- Workspace §17 FDE Worklist row: PO/GRN Map number cards (FNC, Drift, Discrepancy, STN-Routed-pending-pickup).
- Sidebar entries — **regression test for sidebar-matches-cardbreaks lockstep** (the 8f-established guard).
- Sync Record list view: surface line-child status counts ("3/10 lines: Discrepancy").
- Scheduler: GRN-pull delta cron (cadence per `poll_interval_grn_min`, default 30, per §3.3.4); PO-status reconciliation rides the same sweep.
- Tests: list filters, workspace counts, sidebar lockstep regression, cron tick fires the pull, completion trigger fires status push.

## OPEN DECISIONS (resolve during stages)

1. **PO Map autoname on PO rename** — Frappe rename-hook updates link, but autoname pattern `ECS-PO-{purchase_order}` needs rename-coordinated; verify behaviour at Stage 1 build. Fallback: accept renames are rare, FDE manually rebuilds map row.
2. **Sync Record Line `linked_discrepancy`** — confirm it's actually a Link to Integration Discrepancy DocType (§23). If §23 isn't built yet, leave Data field, upgrade at §23 build.
3. **EE→ERPNext po_status echo** — verify drift detection on po_status=3 (we just pushed 3, EE confirms 3 — not drift; should be a no-op observation update, not Discrepancy). Test cleanly at Stage 3.
4. **Out-of-order GRN for EE-born PO** — confirm policy: create PR against the PO Map row that doesn't exist, OR hold the GRN until PO Map row is created? Lean: create PR, raise Discrepancy, FDE links. Confirm at Stage 3.
5. **place_of_supply module** — if §11/§12 logic isn't built yet, Stage 2 builds it as a shared module (`easyecom/tax/place_of_supply.py`) for §9/§11/§12. Confirm scope at Stage 2.

## Build order
Stage 1 → 2 → 3 → 4. One at a time; review each; live-verify on Harmony (using mock GRN injection since real EE GRN requires upstream PO push then physical-stock simulation). Closeout docs (own primer `FDE_PRIMER_section_9_buying.md` — §9 is a flow not a master, distinct from `FDE_PRIMER_section_8_masters.md`; `section_9_buying.md` test script; `SPEC_9_patch_notes.md`; BUILD_TRACKER; docx regen) after Stage 4.

## Carry-forwards from §8

- **§8d standing test failures** (`test_item_pull_stage2` 29/32, `test_item_lifecycle_drift_stage5` 7/10) — confirmed unrelated to §9; track count at Stage 1 build report; do NOT fix as part of §9 unless they move.
- **§10 STN-inward must handle valued self-GRNs** (real payload grn 141936: 49990 against self-vendor with real apparel SKU). Not safe to assume zero-value.
- **`isCancel` on CreatePurchaseOrder unused** — by deliberate design, channel separation. SPEC patch notes call this out so future readers don't wire it.

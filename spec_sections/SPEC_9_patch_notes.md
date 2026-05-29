# §9 Buying / GRN — spec amendments from build + live Harmony bring-up + corrective commit

*Apply these to SPEC.md §9.1–§9.12. They reconcile the spec against what was built, what was confirmed/discovered on the live Harmony sandbox over 5 smoke rounds + corrective commit, and the locked unknown-PO drift contract. Single-writer rule applies — the USER edits SPEC.md; this is the change list.*

> The SPEC.md §9.1–§9.12 as currently written predates the payload grounding. Several specific clauses are wrong against what EE actually does. Apply each amendment below; where a clause is marked **SUPERSEDED**, the stale clause is wrong on the merits, not just stale wording.

## §9.x — Sole-origin contract (NEW subsection, add as §9.0 or as a principle in §9.1)

ERPNext is the **sole origin** of every Purchase Order the integration touches. The buying flow PULLS receipts from EE; it never PUSHES newly-created POs back to EE. A GRN whose PO did not originate in ERPNext is **drift** — NOT auto-receipted — see §9.x (unknown-PO drift).

This was not made explicit in the original SPEC. It is the invariant that justifies the unknown-PO drift behaviour and prevents goods silently arriving in ERPNext for purchases nobody in ERPNext authorised. Write it at the top of §9.

## §9.1 — The flow diagram

The diagram at §9.1 names two channels but conflates several behaviours. Replace with the two-channel, two-key model:

- **Content channel: `CreatePurchaseOrder`** (`{{BaseURL}}/WMS/Cart/CreatePurchaseOrder`). Creates and updates POs. Keyed on `referenceCode` (= ERPNext PO name). Returns `data.poId` (EE-side int). Per-line key inside the payload: `lineItemNumber` corresponds to PO line idx. **Live wire-key correction:** the line array is `items`, not `lineItems` (the SPEC's name).
- **Status channel: `updatePoStatus`** (full host: `https://api.easyecom.io/wms/updatePoStatus` — different base from the content endpoint). Handles every state transition. Keyed on `po_id` (the EE-returned int from the content channel — so status channel can only fire after content has succeeded at least once).
- **`isCancel` on CreatePurchaseOrder is NEVER wired.** Cancel goes via `updatePoStatus` with `po_status=7`. Channel separation is deliberate: content for create/update, status for everything else.

## §9.2 — Preconditions

The preconditions section is structurally right. Three additions:

- **Address precondition (new):** the PO's target warehouse must have a resolvable Address. Else PO Map → Flagged-Not-Created, `flag_reason = "warehouse address not configured for EE push"`. Same "refuse, don't placeholder" principle as §8d HSN — never send a placeholder address to EE.
- **Gate-0 is the FIRST check**, ahead of all preconditions: is the PO's target warehouse EE-mapped (has a §8a `location_key`)? If not, the integration is silently inert. No PO Map row, no Sync Record, no flag. The integration is absent for non-EE warehouses, exactly as if not installed.
- **Mixed-warehouse policy:** a PO whose lines span multiple distinct EE-mapped Locations is rejected at validate with a clear error directing to split. A PO spanning one EE-mapped Location (whether expressed via header `set_warehouse` or per-line) is fine. A PO spanning zero EE-mapped Locations falls through Gate-0 silent skip. Discriminator is "how many distinct EE Locations does this PO touch".

## §9.3 — PO push payload and idempotency

The §9.3 payload description is approximately right but missed several wire-shape corrections found live:

- **Wire keys:** the line array is `items`, not `lineItems`. Top-level keys: `referenceCode` (not `po_reference`), `schedule_date`/`expDeliveryDate` (not `transaction_date`/`po_date`).
- **`unitPrice` is tax-inclusive (gross).** Per-line tax decomposition is persisted on the PO Map's `ecs_last_tax_signature` (Long Text) so Stage 3's GRN pull can reconcile against it. The SPEC.md framing of "unitPrice = rate" is too loose; the integration computes tax-inclusive from line rate + tax via the shared `easyecom/tax/place_of_supply.py` module.
- **`taxType` derivation:** 1 = IGST (inter-state), 2 = CGST-SGST (intra-state), 3 = Custom (foreign). Derived via `compute_tax_type(supplier_state, warehouse_state, supplier_country)`. **Edge case noted but acceptable for v1:** Union Territories, SEZ supplies, and same-state-different-GSTIN cases use the same equality check; UT and SEZ are unverified at the EE `taxType` granularity. If a deployment has SEZ suppliers, the `taxType` may need manual review.
- **Supplier state is read from Address.gst_state** (India Compliance's address-extension field), not from Supplier directly (base Supplier has no `gst_state` field). Override hook available if a deployment ships a Supplier custom field.
- **Idempotency story:** PO Map's `last_pushed_po_status` is the status-channel idempotency guard. Pushing the same status as last_pushed is a no-op. **Body-code 400 from EE on updatePoStatus must NOT advance `last_pushed_po_status`** — a failed push stays retryable.

## §9.4 — Partial PO push (mixed warehouses)

**SUPERSEDED.** The original §9.4 attempted to allow partial-line PO pushes for mixed-warehouse POs. The corrected design refuses mixed-Location POs at validate (see §9.2 above). A mixed-warehouse PO is a validation error directing to split, not a partial push. Delete §9.4 or rewrite as a one-line pointer to the §9.2 mixed-warehouse rule.

## §9.5 — GRN-in-EasyEcom: detection and ingestion

The endpoint URL is wrong. Multiple wire and semantic corrections:

- **Endpoint corrected:** `GET /Grn/V2/getGrnDetails` (NOT the stale `/wms/getGrnDetails` in SPEC.md §9.5.1).
- **Pagination:** cursor via `nextUrl` (opaque, mandatory on follow-up calls). Walk pages until nextUrl is empty/absent.
- **Watermark:** `last_pull_grn_cursor` (the persisted high-watermark, advances to max `grn_created_at` after a clean walk).
- **Bootstrap:** the endpoint defaults to last-7-days if no `created_after` is passed. **Cold-start hazard:** mass-receipting historical GRNs on first run. **§9 go-live requires setting `grn_pull_high_watermark` (the cold-start cutoff) BEFORE wiring the cron.** The handler ships but the cron stays unwired until this is done. Guard test `TestGRNPullSchedulerIntentionallyUnwired` documents the intent.
- **`grn_status_id` semantics:** 1 = CREATED, 2 = QC Pending, 3 = QC Complete, 4 = Deleted. **Status 5 also observed live** (semantics not documented; treat as observe-only). The setting `grn_receipt_trigger_status` (default 3) controls at what status threshold a GRN is converted to a PR. Held-Pre-QC is the GRN Map status for below-threshold observations.

## §9.6 — GRN → Purchase Receipt mapping

This is the section that most needs rewriting. The original §9.6.2 says the GRN payload contains paired `accepted_quantity` and `rejected_quantity` fields. **It does not.** The real payload model is fundamentally different and was discovered live:

### §9.6 corrected — the qty model

EE's GRN payload reports per-line:
- `received_quantity` — what physically arrived (the receipt event).
- `qc_fail` — what QC rejected (the reject event; structurally 0 below status 3 because QC hasn't run).
- A long list of *post-receipt inventory buckets*: `available, reserved, sold, damaged, qc_pass, lost, transfer, return_to_source, return_available, qc_pending, repair, gifted, near_expiry, expiry, used_in_manufacturing, adjusted`.

**The post-receipt buckets are EE live state, not receipt facts.** A line that received 100, then 60 sold off via channels, returns on a later pull as `received_quantity: 100, available: 40, sold: 60`. If we mirrored `available` into ERPNext's accepted qty, we'd silently lose 60 units of stock-in that genuinely arrived.

So the PR build uses ONLY:
- PR line `received_qty = received_quantity` (the receipt event)
- PR line `rejected_qty = qc_fail` (QC-conditional)
- PR line `accepted_qty = received_quantity − qc_fail` (DERIVED — never lifted from `qc_pass` or `available`)
- Accepted qty → the mapped warehouse (via §8a `location_key`)
- Rejected qty → `default_rejected_warehouse` from settings (if rejected_qty > 0 and the setting is unset, PR submit fails with a clear error — FDE configures and retries)

All other buckets — **read and discarded**. ERPNext owns stock movement after receipt. This is structurally enforced: PR Item has no fields named like EE buckets, so the data has nowhere to land.

### §9.6 corrected — line rate from line total, not unit price

**Live discovery, confirmed across 10 real Harmony GRNs spanning 90 days:** `grn_detail_price` is the **line total**, NOT the per-unit price. `sum(line grn_detail_price) == header.total_grn_value` in every sample (the unit-price reading would imply 10×–1000× the real totals).

So **PR line `rate = grn_detail_price / received_quantity`** (NOT `rate = grn_detail_price` as the unit-price reading would have it).

All 10 confirmed samples were single-line GRNs; multi-line distribution is unverified — closeout test script flags this as a watch-item for first real partial receipt.

### §9.6 corrected — back-refs (the canonical PO→PR linkage)

PR Item.purchase_order_item is the **native ERPNext canonical** link from PR line → originating PO line. The §9 GRN handler sets this so ERPNext's own `PO.per_received` updates live, and completion detection can lean on it. Plus the integration-specific back-refs:
- PR header: `ecs_easyecom_grn_id` (the EE GRN id, the idempotency key for re-pull safety even if the GRN Map row is wiped).
- PR line: `ecs_easyecom_grn_detail_id`, `ecs_easyecom_po_detail_id`.

### §9.6 corrected — tax derivation parity with push

The GRN payload's tax fields (and the `grn_detail_price` line-total carry tax inclusive) are reconciled against the PO Map's `ecs_last_tax_signature` (from §9.3). Variance above `tax_variance_tolerance_pct` (default 1%) raises a Discrepancy (Warning) — PR still created. **Stage-2-blank-tax-PO line case:** a PO line that pushed 0% tax may produce a non-zero PR-derived tax — this is a *legitimate tax variance Discrepancy*, not a code error.

### §9.6 corrected — `inwarded_warehouse_c_id` is a COMPANY id, not a warehouse key

**Live discovery, Stage 3 corrective.** The original SPEC framing treated `inwarded_warehouse_c_id` as a warehouse-level identifier. It is a *company-level* identifier. The §8a Location's `location_key` mapping correctly resolves it to Frappe Warehouse + Company, but the field's semantic is company-level. This matters for self-GRN detection (see §9.x below).

## §9.7 — Rejected quantity handling

Approximately right. Two clarifications from the §9.6 correction:

- Rejected qty is `qc_fail`, never derived from `qc_pass` (which is a drifting bucket).
- The QC-conditional nature of `qc_fail`: at `grn_status_id` < 3, `qc_fail` is structurally 0 (QC hasn't run) — so receipting at status 1 or 2 means the entire received qty becomes accepted, and post-QC failures never reach ERPNext. Lowering `grn_receipt_trigger_status` from default 3 is acceptable only for deployments not running QC in EE.

## §9.8 — Batch, serial, and expiry

Mostly right. One amendment from Stage 3 live finding:

- **`batch_code` and `expire_date` are captured to custom fields (`ecs_ee_batch_code` / `ecs_ee_expire_date`) on the PR line even for non-batch Items.** This preserves EE-side data for traceability without forcing native Batch on a non-batch Item. Native Batch auto-creation + PR Item.batch_no linkage only occurs for `has_batch_no=1` Items.

## §9.9 — Tax category and GST handling

Stands as-is, with one addition: the shared `easyecom/tax/place_of_supply.py` module is the single source of truth for `taxType` computation. §11 and §12 call the same module (carry-forward).

## §9.10 — Multiple GRNs per PO

The cumulative-receipt model is right. Two important caveats added:

- **Multi-GRN partial cumulative tolerance is unit-verified but NOT live-smoked.** Stage 3 only smoked full receipts. First real client with a partial-then-completion receipt sequence is the live exercise.
- **Completion (po_status=5) fires from the GRN sweep when cumulative `received_quantity ≥ original_quantity` modulo `allow_under_receipt_pct`**, NOT from PO events. Idempotency guard (`last_pushed_po_status`) prevents re-fire on subsequent pulls.

## §9.11 — Failure modes and recovery

The failure-modes table is roughly right. Three significant additions from corrective commit:

### §9.11.x — Unknown-PO GRN (the corrective contract — NEW)

**Locked 2026-05-28, supersedes any earlier "create PR + Discrepancy" lean.** A GRN whose `po_ref_num`/`ee_po_id` doesn't resolve to any ERPNext-created PO is **drift**:
- NO auto-PR. NO stock movement.
- GRN Map row: status = Discrepancy, `purchase_receipt` empty, `linked_po_map` empty.
- Full GRN payload preserved on GRN Map's `ecs_grn_payload_json` field.
- Integration Discrepancy raised, kind = "GRN for unknown PO".
- Sync Record keyed on the GRN Map row (no PR exists to key on — distinct from the normal-receipt Sync Record).

**Resolution is FDE-driven, ERPNext-side only.** Two whitelisted actions on the drifted GRN Map row:
- `create_pr_from_grn(grn_map_name, purchase_order=None, confirm=True)` — builds a Purchase Receipt from the preserved payload using the same qty model as the normal flow. STANDALONE PR by default (no `purchase_order` link). FDE may optionally supply an existing ERPNext PO to link. On success: GRN Map → Receipted, Discrepancy auto-resolved.
- `dismiss_grn_drift(grn_map_name, reason, confirm=True)` — for GRNs that shouldn't be received at all. Reason required.

Both role-gated (FDE / System Manager / EasyEcom System Manager — Operator can't), confirm-required, audit-commented. Re-pull is idempotent on drift state (refreshes `last_observed_at` only).

**No PO is ever created or pushed to EE in this path.** ERPNext is the sole PO origin. The integration only pulls in the receipt direction. A retroactive PO-creation-and-push after goods have arrived would invert the direction — it's not supported and never will be.

### §9.11.x — Pause-respects-all-three-status-pushes (NEW)

`pause_all_auto_push` (round-2 control) defers every EE write, including all three status pushes:
- po_status=3 (submit), po_status=5 (completion), po_status=7 (cancel) → recorded as `ecs_pending_po_status_push` on the PO Map; no EE call fires.
- Latest-state-wins: submit then cancel during the same pause window leaves pending=7.
- Re-enable via `go_live_enable_auto_push(pos=1)` → pending statuses fire once and clear (idempotency guard prevents re-fire).
- The GRN PULL still runs during pause (read-side). Only the PUSHes it would trigger defer.

This closes a pre-existing pause-mechanism gap where `auto_push_pos_on_save` was uncovered by `pause_all_auto_push` — pause now genuinely means pause.

### §9.11.x — Deleted-Post-Receipt (clarify)

If an already-receipted GRN is flipped to status=4 (Deleted) on the EE side, the integration:
- Flips GRN Map to `Deleted-Post-Receipt`.
- Raises Integration Discrepancy.
- **Does NOT auto-cancel the submitted PR.** A submitted PR cancelled silently by the integration is a hard-rule violation. FDE investigates manually.

## §9.12 — What this enables for reconciliation

Stands as-is.

## §9.x — Self-GRN routing to §10 (NEW subsection, add at the §9↔§10 boundary)

If `vendor_c_id == inwarded_warehouse_c_id` on a GRN, the GRN is an EE-internal inward (batch loads, opening-stock entries, EE-side internal transfers). The vendor *is* the warehouse, not a real supplier. The integration routes these to §10 (Stock Transfer Flows): GRN Map row gets `routed_to_stn=1`, status = STN-Routed; no PR; no Sync Record failure.

**Carry-forward: this routing check is code-correct but never fired on real data on Harmony** (no self-GRN sample on this tenant; no vendor maps to any known company_id). Pre-§10 work includes triggering a real self-GRN on Harmony, inspecting the payload's `vendor_c_id`, and confirming it equals the warehouse's `company_id`. If it does, §10 builds on this routing. If it doesn't, the routing needs adjusting before §10 Stage 3 (inbound) builds.

## Carry-forwards from §9 closeout to future sections

- **GRN-pull cron stays unwired until go-live sets `grn_pull_high_watermark`** — explicit runbook step. Handler ships and is smoke-proven.
- **STN routing live-verification** — §10 prerequisite (see §9.x above).
- **Multi-GRN partial cumulative tolerance** — unit-verified, not live-smoked. Watch-item on first real client.
- **`easyecom/tax/place_of_supply.py`** is shared infrastructure for §9/§11/§12 — already in place.
- **`_t_lookup_field` transformer** (doctype/filter_field/target_field) — reusable infrastructure built in §9 Stage 1, will see further use in §11 and §12.
- **Sync Record Line child DocType** — first concrete consumer (§9); §10/§11/§12/§13 will reuse.

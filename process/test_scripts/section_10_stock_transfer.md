# Section 10 — Stock Transfer Flows — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers §10's full transfer loop: DN submit → SI auto-draft (different-GSTIN) → STN/PO push → EE GRN-Complete pull → IPR with submit gate → IPI + Debit Note auto-creation (different-GSTIN) → multi-GRN cumulative gap revisions → submitted-DN-late-GRN block → aged GIT nudges → EE-originated standalone Discrepancy. Derived from `spec_sections/section_10_stock_transfer_packet.md` (post-Stage-4 build), `SPEC_10_patch_notes.md`, and the FDE primer.

> **First time?** Read `HOW_TO_RUN_FDE_TESTS.md`, then the §10 primer (`../primers/FDE_PRIMER_section_10_stock_transfer.md`). §9 primer is a prerequisite (§10 reuses §9's GRN pull machinery).

> **The model in one line.** Internal-Customer DN → optional auto-drafted SI (different-GSTIN) → EE push (STN via createOrder if source EE-mapped; PO via §9's CreatePurchaseOrder if not) → EE GRN-Complete → auto-IPR (Internal Supplier, GIT→destination) → submit gate (same-GSTIN auto / SI-Submitted auto / SI-Draft holds) → optional auto-IPI + auto-Debit-Note (different-GSTIN).

> **Status:** §10 Stages 1-4 unit + mock complete (61 §10 tests + 1 documented skip, all green). **Pre-live-deployment** — not yet smoked end-to-end on Harmony. The integration smoke (8.4 below) is the first real-EE round-trip.

> **Scope:** §10 covers stock transfer between warehouses, including inter-Company different-GSTIN scenarios. §9 (Buying) is a prerequisite — §10's inbound reuses §9's GRN pull machinery. §11 (TBD) is the next operational flow.

**Build under test:** commit / branch ____________ · **Deployed to:** ____________ · **Tester:** ____________ · **Date:** ____________

---

## Section 1 — Setup and precheck

### 1.1 Run the §10 precheck
**Do:** EasyEcom Account → run `precheck_section10_go_live(account_name)`. Inspect `{ok, blockers, warnings, checked}`.
**Confirm:** Output covers: ≥2 EE-linked Companies (or 1 if single-Company same-GSTIN-only deployment); `default_in_transit_warehouse`, `default_rejected_warehouse`, `lost_in_transit_threshold_days` set per EE-linked Company; Internal Customer + Internal Supplier pairs exist for every EE-linked Company; every Internal Customer has `ee_customer_id` captured.
**Good:** `ok: true` with empty blockers. Warnings (§9 precheck blockers, e.g.) acceptable depending on deployment scope.
**Failure looks like:** Missing Internal pair → run `ensure_internal_party_pairs_for_account(account_name, confirm=True)` and re-run precheck.

### 1.2 Confirm Internal Customer/Supplier fabric
**Do:** Invoke `ensure_internal_party_pairs_for_account(account_name, confirm=True)`. Run again (test idempotency).
**Confirm:** N Internal Customers + N Internal Suppliers for N EE-linked Companies (NOT N×(N−1)). Each Internal Customer has `is_internal_customer=1`, `represents_company` = its destination, `companies` child table lists every OTHER EE-linked Company. Symmetric for Internal Suppliers. Each Internal Customer's Customer Map row has `ee_customer_id` populated (push to EE succeeded). Re-run is no-op (no duplicates).
**Good:** Pair fabric matches expected N+N count; both runs produce same end-state.
**Failure looks like:** ERPNext refuses Internal Customer creation with "more than one Internal Customer for the same represents_company" → packet implementation regressed to N×(N−1); STOP and report.

### 1.3 Confirm §10 settings
**Do:** On EasyEcom Account, locate the "Stock Transfer (§10) Defaults" section.
**Confirm:** `stn_default_payment_mode` (default 5 Prepaid) and `stn_default_shipping_method` (default 1 Standard COD) both set.
**Good:** Both fields present with sensible defaults; description text explains they're EE placeholders for internal transfers.

---

## Section 2 — Outbound (STN branch — source EE-mapped)

### 2.1 Same-GSTIN STN push
**Do:** Create a Delivery Note with `is_internal_customer=1`, single source/target pair where source warehouse is EE-mapped and source Company == target Company (or same GSTIN across Companies). Items have Item Map rows. Submit.
**Confirm:** Within seconds, `EasyEcom Transfer Map` row created (`ECS-XFER-{dn_name}`). Status = `EE-Pushed`. `ee_doctype="STN"`. `ee_order_id`, `ee_suborder_id`, `ee_invoice_id` all populated from EE response (Data fields, stored as strings). `gstin_different=0`. `sales_invoice` empty (no SI for same-GSTIN). Stock on the DN parks in destination Company's GIT warehouse.
**Good:** Transfer Map complete; EE-side STN visible in Harmony with `orderType="stocktransferorder"`.
**Failure looks like:** Transfer Map missing → Gate-0 didn't fire (verify DN.is_internal_customer=1 and source warehouse EE-mapped); status = Drift → check `flag_reason`.

### 2.2 Different-GSTIN STN push with SI auto-draft
**Do:** Create an Internal-Customer DN from Company-A warehouse (EE-mapped, GSTIN-A) to Company-B warehouse (EE-mapped, GSTIN-B). Submit.
**Confirm:** Transfer Map created with `gstin_different=1`. A Sales Invoice in **Draft** (docstatus=0) is auto-created against the Internal Customer for Company-B. SI line items mirror DN dispatched qty. `update_stock=0` on SI. SI back-refs the Transfer Map via `ecs_section10_transfer_map`. STN payload pushed to EE. Transfer Map.sales_invoice populated. Status reflects EE-pushed with SI pending (the Stage 2 status overload — `SI-Pending` with `ee_order_id` set means "EE pushed AND SI drafted").
**Good:** SI is Draft (not submitted); STN payload fires; Transfer Map has both ee_order_id and sales_invoice populated.
**Failure looks like:** SI auto-submitted → §10 invariant violation, STOP and report. SI rate/qty doesn't match DN → field mapping bug.

### 2.3 STN payload shape (the §10.G contract)
**Do:** Capture the STN push payload from a Sync Record (entity_type=Delivery Note) or via tcpdump/mock on a test deployment.
**Confirm:**
- `orderType: "stocktransferorder"` (exact string)
- `orderNumber: <dn_name>` (the content-channel key)
- `paymentMode` and `shippingMethod` from §10 settings
- `customer[]` is a single-element ARRAY (EE quirk)
- `customer[0].customerId` is the Internal Customer's `ee_customer_id` (Int)
- `billing` block uses destination Company's primary Address; `shipping` block uses destination warehouse's Address — both inline on every push
- Line items have `OrderItemId: "{dn_name}-L{line_idx}"` (explicit per-line key)
- `Quantity` sent as quoted string (match live API format)
- **OMITTED fields actually absent:** `is_market_shipped`, `closed`, `queue`, `paymentGateway`, `walletDiscount`, `promoCodeDiscount`, `prepaidDiscount`, `paymentTransactionNumber`, `collectableAmount`, `salesmanId`, `discount`, `marketplace_id`, `custom_fields`, `latitude`, `longitude`, `gst_number`, `appointment_*`, `company_carrier_id`, `is_pricing_master`, `orderAssignmentProperty`, `productName`.
**Good:** Payload matches §10.G shape exactly. OMITTED list is verifiably absent.
**Failure looks like:** Any OMITTED field appears in payload → §10.G contract violated, STOP and report. (Test discipline established in Stage 2; regression here is significant.)

### 2.4 Multi-warehouse DN rejected
**Do:** Create an Internal-Customer DN with line items spanning multiple distinct source/target warehouse pairs. Try to submit.
**Confirm:** Validation error refusing submit, directing user to split into separate DNs (one source/target pair per DN). No Transfer Map created.
**Good:** Multi-pair refused at validate, not silently auto-multiplexed.

### 2.5 Non-Internal-Customer DN is silently inert
**Do:** Create a regular Customer-side DN (non-Internal-Customer) with EE-mapped warehouse. Submit.
**Confirm:** No Transfer Map row, no SI, no EE push, no Sync Record. DN lifecycle is exactly as without integration.
**Good:** Gate-0 invariant holds for non-§10 DNs.
**Failure looks like:** Any §10 artifact created → Gate-0 leak, STOP and report (the most important invariant for §10).

---

## Section 3 — Outbound (PO branch — source NOT EE-mapped)

### 3.1 PO branch fires when source vendor resolves
**Do:** Create an Internal-Customer DN where source warehouse is NOT EE-mapped, target warehouse IS EE-mapped, AND the source Company has a resolvable EE-side vendor (via Internal Supplier → Supplier Map → ee_vendor_id chain). Submit.
**Confirm:** Transfer Map created with `ee_doctype="PO"`. PO branch fires §9's `CreatePurchaseOrder` (NOT the STN endpoint). `ee_po_id` (Int) captured from EE response. The PO is created on EE-side at the target warehouse, against the source-Company vendor representation.
**Good:** PO branch reuses §9 machinery; `ee_po_id` populated.

### 3.2 PO branch refuses with Drift if vendor unresolvable
**Do:** Same setup as 3.1 but with the source Company having NO EE-side vendor configured.
**Confirm:** Transfer Map status = `Drift` with `flag_reason` naming the missing vendor. NO EE push fires. NO auto-creation of EE Vendor (that's §8f scope, deliberately not crossed).
**Good:** Drift fall-through is clean.
**Failure looks like:** EE Vendor auto-created → scope boundary violation, STOP and report.

---

## Section 3.5 — Outbound (B2B branch — source EE-mapped, target NOT EE-mapped)

Added 2026-06-01. Closes Case C of the §10 decision matrix.

### 3.5.1 B2B branch fires with correct orderType
**Do:** Create an Internal-Customer DN where source warehouse IS EE-mapped, target warehouse IS NOT EE-mapped. Submit.
**Confirm:** Transfer Map created with `ee_doctype="B2B"`. EE call fires `POST /webhook/v2/createOrder` with `orderType="businessorder"` (exact string). `customer[0].customerId` is the Internal Customer's **wholesale c_id** (from `/Wholesale/CreateCustomer`), NOT the regular customerId used in STN. Response captured: `OrderID`, `SuborderID`, `InvoiceID` (all as strings).
**Good:** B2B push fires, all three EE IDs captured. Transfer Map shows `ee_doctype=B2B`.
**Failure looks like:** orderType not `businessorder` (was the bug surface during discovery: `b2border`, `wholesaleorder`, `B2B`, `B2BOrder`, `B2C` are all rejected with "Order type is not valid"); customerId is regular customer id instead of wholesale c_id; ee_doctype enum doesn't accept B2B.

### 3.5.2 B2B branch — SI auto-drafts on different-GSTIN
**Do:** From 3.5.1 with source Company and target's representative Company having different GSTINs.
**Confirm:** SI auto-drafted in Draft (docstatus=0), back-link `ecs_section10_transfer_map` populated immediately, status reflects SI-Pending overload (ee_order_id set).
**Good:** SI created with back-link present; status overload visible via ee_order_id.
> **LOAD-BEARING CHECK:** verify `SI.ecs_section10_transfer_map` is set BEFORE SI submit. Between 2026-05-29 and 2026-06-01, this back-link was inadvertently NULL on every §10 SI, silently neutralising the entire SI-submit cascade. The bug is fixed in commit `cd27d0f` but the test must verify the invariant, not the cascade-given-back-link.

### 3.5.3 B2B branch — Transfer Map status advances on SI submit
**Do:** From 3.5.2, submit the SI manually as ERP user (no GRN involvement).
**Confirm:** Transfer Map status advances `SI-Pending → EE-Pushed` immediately on SI submit. The status transition does NOT wait for a GRN to arrive (B2B has no IPR; status must advance independently).
**Good:** Status `EE-Pushed` after SI submit.
> **LOAD-BEARING CHECK:** verify status transition completes on SI submit even with no IPR present. Before the 2026-06-01 fix, the transition was buried inside the IPR-handling branch and never fired for B2B (which has no IPR). The bug is fixed in commit `cd27d0f` (`transfer_inbound.py:1340-1355`); the test must verify status reaches `EE-Pushed` without manual intervention.

### 3.5.4 B2B branch — NO IPR auto-creation on destination
**Do:** From 3.5.3, even if EE-side fulfillment happens and an EE inward event occurs, observe the destination Company's ERPNext state.
**Confirm:** NO IPR auto-created by the integration. NO IPI auto-drafted. NO Debit Note auto-drafted. The destination Company's stock-in is the ERP user's responsibility via standard ERPNext UX (Purchase Receipt or Stock Entry as appropriate to the deployment).
**Good:** §10 substrate is hands-off on the destination side of a B2B transfer (stock has left EE's universe).
**Failure looks like:** §10 inbound machinery fires on a B2B Transfer Map → scope violation, STOP and report.

---

## Section 4 — Inbound (GRN-Complete → IPR with submit gate)

### 4.1 Same-GSTIN GRN-Complete → IPR auto-submits
**Do:** From section 2.1, complete the GRN on EE-side at QC Complete. Trigger `bench execute easyecom.flows.grn_pull.pull_grns_for_account`.
**Confirm:** Within the pull, an IPR (Purchase Receipt with `is_internal_supplier=1`) is created and **submitted**. IPR has supplier = the Internal Supplier for source→target pair, company = target Company. Per-line: `from_warehouse` = destination Company GIT, `warehouse` = target Company's EE-mapped warehouse. `received_qty` = GRN's `received_quantity`, `rejected_qty` = `qc_fail`, accepted derived (§9 qty model). Rate = `grn_detail_price / received_quantity`. `ecs_section10_transfer_map` back-ref set. IPR appended to Transfer Map.internal_purchase_receipts. Transfer Map status → `Fully-Received` (cumulative == dispatched). Stock moves GIT → destination warehouse.
**Good:** IPR submitted; GIT → destination clean; no IPI/DN (same-GSTIN doesn't need them).

### 4.2 Different-GSTIN, SI Submitted → IPR auto-submits + IPI + (no DN if full receipt)
**Do:** From section 2.2, ERP user submits the auto-drafted SI. Then complete the GRN on EE-side. Pull.
**Confirm:** IPR auto-submits (SI-Submitted gate cleared). IPI auto-drafted as Purchase Invoice: `is_internal_supplier=1`, supplier = Internal Supplier, company = target, `update_stock=0`. IPI line items mirror the **SI's** dispatched qty (NOT IPR's received). IPI's `purchase_receipt` field links to the IPR. Transfer Map.internal_purchase_invoice set. No Debit Note (full receipt, gap == 0). Status → `Fully-Received`.
**Good:** IPI drafted for full ITC claim; no DN.

### 4.3 Different-GSTIN, SI Submitted, PARTIAL receipt → IPR + IPI + auto-Debit-Note
**Do:** Same as 4.2 but the GRN delivers fewer units than DN dispatched (e.g. DN 10, GRN1 6). Pull.
**Confirm:** IPR1 submitted (6 units). IPI drafted (full 10 units, SI-sized for full ITC). Debit Note auto-drafted: Purchase Invoice with `is_return=1`, gap-sized lines (4 units), `return_against` = IPI. Transfer Map.draft_debit_note set. Status → `Partial-Received`. GIT balance = 4.
**Good:** Auto-DN drafted at gap size. The genuinely novel mechanism.
**Failure looks like:** DN sized to received qty instead of gap qty → arithmetic backwards, STOP and report.

### 4.4 Different-GSTIN, SI Draft (not submitted) → IPR stays Draft, NO Discrepancy
**Do:** From section 2.2 with SI still in Draft (ERP user has not submitted). Complete GRN on EE-side. Pull.
**Confirm:** IPR is built and **stays in Draft** (docstatus=0). NO stock movement GIT → destination. ToDo created on the IPR for the relevant ERP user, message names the unsubmitted SI. Comment on the IPR explaining the block. **NO Integration Discrepancy raised** (this is ERP-user pending, not FDE config issue). The IPR is on Transfer Map.internal_purchase_receipts as a Draft entry.
**Good:** Draft IPR, ToDo + Comment, no Discrepancy.
**Failure looks like:** Integration Discrepancy raised for SI-Draft case → FDE worklist will fill with non-FDE work, STOP and report.

### 4.5 doc_event auto-retry: ERP user submits SI → drafted IPR auto-submits
**Do:** From state in 4.4 (IPR Draft, SI Draft). ERP user submits the SI.
**Confirm:** Within the SI submit, a doc_event hook fires. The Transfer Map's drafted IPR auto-submits. IPI auto-drafts (per 4.2's pattern). DN auto-drafts if gap exists. Comment on the IPR explains the auto-submit trigger.
**Good:** Auto-retry cascade fires once on SI submit, hands-off.

---

## Section 5 — Multi-GRN cumulative arithmetic

### 5.1 Two GRNs, gap closes → DN auto-cancel with Comment on Transfer Map
**Do:** From section 4.3 (Partial-Received, GIT balance = 4, draft DN at 4 units). Complete a second GRN on EE-side for the remaining 4 units. Pull.
**Confirm:** New IPR (IPR2) for 4 units, submitted. Cumulative received = 10 (matches dispatched). Draft DN's line qtys become 0 → draft DN auto-cancelled (deleted from ERPNext, since drafts can't be cancelled — they're deleted). **Comment on the Transfer Map** (not on the deleted DN) records the auto-cancellation with the GRN reference and original gap size. Transfer Map.draft_debit_note cleared. Status → `Fully-Received`. GIT balance = 0.
**Good:** Auto-cancel preserves audit trail via Transfer Map Comment.
**Failure looks like:** Comment lives only on the deleted DN → audit trail evaporated. The Comment must be on the Transfer Map.
> **LOAD-BEARING CHECK:** the audit Comment MUST survive the DN deletion. Verify by querying Transfer Map's Comment history *after* the DN deletion completes, not before.

### 5.2 Two GRNs, gap shrinks but stays > 0 → DN auto-revise
**Do:** Same starting state as 5.1. Complete a second GRN for 3 units (cumulative 9, gap 1).
**Confirm:** IPR2 submitted (3 units). Draft DN's lines update from 4 → 1 (revised, not cancelled). Comment on the Transfer Map records the revision. Comment on the draft DN also records the revision (DN survives). Status stays `Partial-Received`. GIT balance = 1.
**Good:** DN revision preserves audit on both Transfer Map and DN.

### 5.3 Cumulative receipt summary visible on Transfer Map form
**Do:** Open the Transfer Map from 5.2 in the ERPNext form view.
**Confirm:** The `internal_purchase_receipts` table shows both IPRs with status. The form surfaces a per-Item cumulative summary (dispatched vs cumulative-received, gap highlighted). Read-only display, no edits.
**Good:** FDE / ERP user can answer "how much of this transfer arrived?" at a glance.

---

## Section 6 — Submitted-DN-Late-GRN block

### 6.1 ERP user submits draft DN, then late GRN arrives → IPR Draft + Discrepancy
**Do:** From section 4.3 (draft DN at 4 units). ERP user submits the draft DN (accepting loss). Transfer Map status → `DN-Submitted-Locked` (via the `Purchase Invoice.on_submit` doc_event hook detecting `is_return=1` matching `Transfer Map.draft_debit_note`). Then a late GRN arrives on EE-side for the remaining 4 units. Pull.
**Confirm:** New IPR (IPR2) is built but **stays in Draft** regardless of any SI state (this is the second §10 invariant). Integration Discrepancy raised, kind=`"Late GRN after submitted DN"`. Comment on IPR2 + ToDo on relevant ERP user. The Discrepancy appears on the §17 FDE Worklist (FDE awareness, even though ERP user does the reconciliation).
**Good:** Late GRN blocked; Discrepancy raised; hands-off to ERP user.
**Failure looks like:** IPR2 auto-submits → second §10 invariant violation, STOP and report.

---

## Section 7 — EE-originated standalone (self-GRN routing)

### 7.1 Self-GRN routes to §10, raises Discrepancy
**Do:** On Harmony, create an EE-internal inward GRN (batch load, opening stock entry) on a mapped warehouse where `vendor_c_id == inwarded_warehouse_c_id` will hold. Pull.
**Confirm:** GRN Map row with `routed_to_stn=1`. §10's `handle_ee_originated_grn` invoked. **No IPR auto-created** (Frappe refuses blank-supplier saves). Integration Discrepancy raised with kind=`"EE-originated transfer (self-GRN)"`. The Discrepancy appears on §17 FDE Worklist.
**Good:** EE-originated routed; no auto-IPR; Discrepancy raised.

### 7.2 FDE resolves EE-originated Discrepancy via §9 Create-PR action
**Do:** On the GRN Map row, invoke `easyecom.api.grn_drift_resolution.create_pr_from_grn(grn_map_name, supplier=<internal_supplier_name>, confirm=True)` with the destination Company's Internal Supplier-for-itself picked.
**Confirm:** Standalone PR created and submitted (FDE-driven). GRN Map status → Receipted. Discrepancy auto-resolved.
**Good:** FDE-driven resolution reuses §9 machinery.

> **WATCH-ITEM (carry-forward):** the self-GRN routing assumption (`vendor_c_id == warehouse company_id`) is **code-correct but NOT live-verified on Harmony as of §10 closeout**. No real self-GRN sample triggered yet on the sandbox. If the assumption doesn't hold on real data, this path may never fire on real deployments. Live-verification by triggering a real self-GRN is a §10 carry-forward.

---

## Section 8 — Pause kill-switch + Aged GIT + Cancel/Amend + Integration Smoke

### 8.1 Pause defers §10 EE push uniformly
**Do:** Invoke `pause_all_auto_push(reason="test", confirm=True)`. Submit an Internal-Customer DN that would otherwise push.
**Confirm:** Transfer Map created in pre-push state. SI auto-drafted if different-GSTIN (ERPNext-side, not paused). **No EE push.** `ecs_pending_ee_push=1` on the Transfer Map.
**Good:** Pause defers EE write uniformly across STN and PO branches.

### 8.2 Un-pause fires pending Transfer Map pushes
**Do:** Invoke `go_live_enable_auto_push(pos=1, confirm=True)`.
**Confirm:** The un-pause runner calls `fire_pending_transfer_pushes` automatically. Each Transfer Map with `ecs_pending_ee_push=1` gets its STN/PO push fired once. Flag cleared. Latest-state-wins (no duplicate fires).
**Good:** Single fire on un-pause; flag clears.

### 8.3 Aged GIT triggers ToDo on DN owner
**Do:** Create a Transfer Map with a draft DN whose originating DN posting_date is older than `lost_in_transit_threshold_days` ago (mock the date or wait threshold days on a test setup). Trigger `bench execute easyecom.flows.transfer_aged_git.scan_aged_git_for_account --kwargs '{"account_name": "<name>"}'`.
**Confirm:** ToDo created on the DN owner, description names the Transfer Map, gap qty, days aged, and links to the draft DN. Comment on the originating DN ("GIT aged past threshold on this transfer"). Re-running the scan does NOT create a duplicate ToDo (idempotent via description-substring match).
**Good:** Aged GIT nudge fires once; idempotent on re-scan.
**Failure looks like:** Duplicate ToDos on re-scan → idempotency broken, STOP and report.

### 8.4 Cancel/amend stub-blocker
**Do:** From section 2.1 (EE-pushed Transfer Map). Try to cancel the DN in ERPNext.
**Confirm:** Cancel refused with clear error: "§10 STN cancel/amend not yet implemented — EE cancelOrder endpoint payload ungrounded (§10.G). DN {name} has a Transfer Map row in status {status} with ee_order_id={id}. Cancelling would desync ERPNext from EE."
**Good:** Stub-blocker fires; ERP user knows what to do (contact integration team).

### 8.5 Integration smoke (LIVE — first real-EE round-trip)
**Do:** Full end-to-end on Harmony:
1. Create real Internal-Customer DN (different-GSTIN if possible).
2. Submit. Watch SI auto-draft + STN push to Harmony.
3. ERP user submits the SI.
4. Complete GRN on EE-side (partial, e.g. 6 of 10).
5. Pull. Watch IPR1 auto-submit + IPI auto-draft + DN auto-draft (gap 4).
6. Complete second GRN on EE-side (remaining 4).
7. Pull. Watch IPR2 auto-submit + DN auto-cancel + Comment on Transfer Map.
8. Submit IPI in ERPNext.
9. Verify Transfer Map status = Fully-Received, GIT balance = 0, audit Comments on Transfer Map intact.

**Confirm:** Each step's expected document is created/updated correctly. EE-side Harmony shows the STN with correct payment, fulfillment status. No silent desync between ERPNext and EE.
**Good:** Full §10 round-trip works on real EE.
**Failure looks like:** Any silent divergence (Transfer Map status doesn't match reality, audit Comment missing, IPR submit gate misbehaves on real timing) → file findings and decide on a corrective commit.
> **This is the §10 equivalent of §9's 5 Stage-3 Harmony smoke rounds.** Until 8.5 passes, §10 is unit-and-mock-verified but not real-EE-verified.

---

## Section 9 — Warehouse EE-mapping UX (added 2026-06-01)

### 9.1 Warehouse label appears on Live + enabled locations
**Do:** Pick an EasyEcom Location that is Live AND enabled. Open the linked Warehouse in ERPNext.
**Confirm:** Warehouse has `ecs_ee_location_label` populated with format `"EE: <location_name> (#<location_key>)"`. The label appears in: the Warehouse list view, the in_standard_filter dropdown, and as the description column in any autocomplete dropdown selecting from Warehouse (PO, SI, Stock Entry, Material Request, DN).
**Good:** Label visible and correctly formatted.

### 9.2 Label is empty on disabled or non-mapped warehouses
**Do:** Find or create a Warehouse with no linked EasyEcom Location, OR find one linked to a disabled EasyEcom Location.
**Confirm:** `ecs_ee_location_label` is empty (NULL or empty string).
**Good:** Label only appears for genuinely EE-mapped + Live + enabled warehouses.

### 9.3 Label syncs on EasyEcom Location re-point
**Do:** Take an EasyEcom Location currently mapped to Warehouse-A. Edit it to point to Warehouse-B. Save.
**Confirm:** Warehouse-B's label is populated. **Warehouse-A's label is cleared** (no stale label on the orphaned warehouse).
**Good:** Both sides updated correctly on re-point.

### 9.4 Label syncs on EasyEcom Location deletion
**Do:** Take an EE-mapped Warehouse. Delete the EasyEcom Location pointing at it.
**Confirm:** The Warehouse's label is cleared on the delete.
**Good:** Orphan cleanup works.

### 9.5 DN warehouse autocomplete sorts EE-mapped first
**Do:** Open a new DN form. Click the source warehouse field to open the autocomplete dropdown.
**Confirm:** EE-mapped warehouses appear at the top of the dropdown. The EE label appears as the description column.
**Good:** FDE sees EE-mapped options first, with clear visual indication.

### 9.6 DN branch-prediction chip appears with both §10 fields filled
**Do:** On a DN with `is_internal_customer=1`, fill `ecs_section10_transfer_from_warehouse` (EE-mapped) and `ecs_section10_transfer_to_warehouse` (not EE-mapped).
**Confirm:** Within seconds, a dashboard indicator chip appears: `§10 branch: B2B · src ✓ EE · tgt — non-EE` in blue. An explanation block appears under the `is_internal_customer` field.
**Good:** Chip surfaces correct branch prediction before DN submit.

### 9.7 Chip predicts all 4 branches correctly
**Do:** Repeat 9.6 with each (source EE / target EE) combination:
- Both EE-mapped → expect `§10 branch: STN`
- Source NOT EE / target EE → expect `§10 branch: PO`
- Source EE / target NOT EE → expect `§10 branch: B2B`
- Neither EE → expect `§10 branch: Inert · no EE call`
**Confirm:** Chip matches predicted branch in each case.
**Good:** UX prediction matches live `push_one_transfer` decision logic.

---

## What passing means

The script passes when:
1. Setup is clean — Internal pair fabric in place, precheck green.
2. Outbound works for both STN and PO branches; OMITTED fields are demonstrably absent from STN payload.
3. Inbound works for all four submit-gate cases (same-GSTIN auto; SI-Submitted auto; SI-Draft holds without Discrepancy; submitted-DN late-GRN holds with Discrepancy).
4. Multi-GRN cumulative arithmetic revises and cancels draft DNs correctly, audit Comments survive deletion on the Transfer Map.
5. Pause defers §10 writes; un-pause fires them once.
6. Aged GIT cron fires idempotent ToDos.
7. Cancel/amend stub-blocks cleanly with a clear user-facing error.
8. The live integration smoke (8.5) completes on Harmony without silent desync.

The **load-bearing checks** that protect §10 invariants:
- **2.5** — Gate-0 silent-inert for non-§10 DNs.
- **2.3** — STN payload OMITTED fields actually absent.
- **3.5.2** — SI back-link `ecs_section10_transfer_map` set BEFORE SI submit (load-bearing for the entire SI-submit cascade — see the 2026-06-01 corrective).
- **3.5.3** — Transfer Map status advances `SI-Pending → EE-Pushed` on SI submit independent of IPR state (the second 2026-06-01 corrective).
- **4.4** — SI-Draft holds IPR without Discrepancy (correct role separation).
- **4.3** — Auto-Debit-Note sized to gap, not received qty.
- **5.1** — Audit Comment lives on Transfer Map (survives DN deletion).
- **6.1** — Submitted-DN-late-GRN blocks IPR auto-submit (second §10 invariant).
- **8.5** — Real-EE integration smoke (the first one is non-negotiable; now closed 2026-06-01 with corrective findings — see BUILD_TRACKER §10 Live Integration Smoke entry).

If any of these regress, §10 has lost its locked-in invariants and the build must STOP before further changes.

---

**Carry-forwards** (named for transparency, not blocking the script):
- STN self-GRN routing live-verification on Harmony (Section 7 watch-item).
- STN cancel/amend endpoint grounding (Section 8.4 stub-blocker — lift when EE provides the payload).
- Multi-GRN partial cumulative on real EE (Sections 5.1 + 5.2 are unit-mock-verified; live smoke is 8.5).
- §9 `_resolve_for_receipt` vs §10 inline Item resolution divergence (watch for drift on future fixes).
- PO-branch wire dispatch live-smoke (Section 3.1 is mock-verified; real non-EE-source deployment is the first live exercise).

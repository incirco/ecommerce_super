# 10 — Stock Transfer Flows (§10) — Build Packet

*Operational flow #2, post-§9 Buying. Build stage-by-stage; each green + committed (local) + reviewed before next, same discipline as §8/§9. Grounded in real EE payloads (STN payload pending, see §10.G). The pre-existing SPEC.md §10.1–§10.8 is the **pre-Internal-Customer-grounded** model; this packet supersedes it.*

> Single-writer rule. No real EE writes during dev/test except Harmony (disposable). SPEC.md is rewritten via §10 patch-notes at closeout, not before.

## The principle (locked, design-defining)

**The integration does not modify any GL postings.** Whatever native ERPNext + India Compliance machinery does on each doctype (DN, SI, IPI, IPR, Debit Note) stays exactly as ERPNext does it. The integration's job is orchestration — *which* doctypes to create, *when*, with *which* references — not to suppress or alter postings. Standard accounting impact runs as-is.

## The §10 invariant (write into the controller as a docstring)

> When a financial pre-condition isn't met, the integration creates the dependent document in Draft and notifies, never auto-submits. SI-not-submitted → IPR-in-Draft. Submitted-DN-exists → late IPR in Draft. Manual-reconciliation states are surfaced via ERPNext-native UX, not auto-resolved.

## Role separation (locked)

- **FDE:** integration setup. Auto-creates Internal Customer/Supplier pairs (go-live), configures GIT per Company, configures rejected_warehouse per Company, maps EE warehouses, wires credentials, runs precheck, triages **integration-health Discrepancies** (auto-creation failures, EE drift, config gaps).
- **ERP User:** business flow. Creates DNs, submits auto-drafted SIs/IPIs/DNs, submits Draft IPRs after upstream gates clear, decides on aged GIT balances. **All operational pendings are ERP User's responsibility.**
- **Surface separation:** FDE Worklist (§17 workspace cards) carries integration-health items only. Operational pendings live in ERPNext-native UX (document state, To-Do, document Comments, existing Notification rules). A §10 Operations Dashboard is a future polish, not core build.

## The model (settled, GST-coherent)

**Gate 0 (lifecycle-wide):** integration acts iff source OR target WH is EE-mapped. Both non-EE → silently inert (pure ERPNext Stock Entry, integration not involved). Same Gate-0 pattern as §9.

**Internal Customer + Internal Supplier pair (auto-created at setup):** for every EE-linked Company, the integration auto-creates:
- **One Internal Customer per destination Company** (`is_internal_customer=1`, `represents_company`=that Company), with the `companies` ("Allowed To Transact With") child table enumerating every OTHER EE-linked Company permitted to sell to it.
- **One Internal Supplier per source Company** (`is_internal_supplier=1`, `represents_company`=that Company), symmetric — `companies` child lists every other Company permitted to buy from it.
- Naming: `INTL-CUST-for-{destination}` / `INTL-SUPP-from-{source}`.
- Cardinality: N Internal Customers + N Internal Suppliers for N EE-linked Companies (NOT N×(N−1) — ERPNext enforces at-most-one Internal Customer per `represents_company`).
- Idempotent: re-running adds missing pairs and reconciles `companies` children additively (does not strip rows the FDE may have added manually).
- ERPNext's `is_internal_*` flags drive correct GL behaviour (suppresses COGS-on-supply, enables Internal Sales Invoice / Internal Purchase Invoice machinery).
- **Lookup at runtime (used by Stage 2 DN-submit hook):** to find the Internal Customer for a transfer from Company A to Company B, look up `Customer` where `is_internal_customer=1` AND `represents_company=B` AND `A in companies[*].company`.

**GIT (Goods-in-Transit) always:** stock parks in destination Company's GIT warehouse on DN submit (DN's standard posting). Stock moves GIT → destination WH only when IPR submits, only for received qty. Balance stays in GIT for variance tracking. **Always** — no bypass for same-location moves; EE tells us what arrived, that's the authoritative trigger.

**GST decision = source-WH Company-GSTIN vs target-WH Company-GSTIN.** Same GSTIN → no supply event, pure stock movement. Different GSTIN → supply under Schedule I, full SI/IPI/DN machinery fires.

**EE-side doctype = source-WH-EE-mapped?** Source EE-mapped → push STN (EE-native internal transfer). Source NOT EE-mapped, target EE-mapped → push PO (EE sees goods arriving from outside its universe). Reuses §9's CreatePurchaseOrder almost entirely for the PO case.

**Doctype matrix:**

| GSTINs | ERPNext docs | EE side |
| --- | --- | --- |
| Same | DN (always) → IPR (always, on GRN-Complete) | STN or PO + GRN |
| Different | DN → SI (draft, dispatched qty) → STN/PO+GRN → IPR (submitted) → IPI (draft, dispatched qty matching SI) → DN-against-IPI (draft, gap if any) | STN or PO + GRN |

**Full-ITC + Debit-Note pattern (different-GSTIN only):** IPI is sized to **dispatched qty (mirrors SI)** for full ITC claim on GSTR-2B. If received < dispatched, integration auto-creates a Debit Note against IPI in Draft, sized to the **gap** (= dispatched − received, valued at IPI line rate, with matching tax breakdown). ERP user submits IPI for full ITC, submits DN to reverse proportional ITC for the un-received portion. GSTR-coherent.

## Outbound (ERPNext side, on DN submit)

1. ERP User creates DN with Internal Customer = destination Company's representation, source WH, target WH, lines.
2. On submit, integration Gate-0: source OR target EE-mapped? Else inert (no SI, no EE push, no Map row).
3. DN posts standard GL: stock leaves source WH → GIT (ERPNext-native, integration does not touch).
4. **GSTIN comparison:**
   - Different GSTINs → integration auto-creates **Internal Sales Invoice in DRAFT**, sized to **dispatched qty**, line items mirroring DN, Internal Customer as bill-to. ERP user submits later (timing flexible, but IPR auto-submit at destination requires SI submitted — see Inbound step 6).
   - Same GSTIN → no SI created. DN alone handles outbound side.
5. **EE push** per source-WH-EE-mapping:
   - **Source EE-mapped** → push **STN** to EE. Source EE-WH → target EE-WH as EE-native internal transfer. [STN payload contract: see §10.G — pending grounding.]
   - **Source NOT EE-mapped, target EE-mapped** → push **PO** to EE at target WH. Reuses §9 CreatePurchaseOrder almost verbatim; vendor = the Internal Supplier's EE-side representation; referenceCode = DN name (or SI name if SI exists — confirm in build).
6. Capture EE-returned identifiers (STN id / PO id) on the §10 Map row.

## EE side (operator workflow, EE-native — out of scope for integration)

WMS team: GRN against STN/PO → QC → Shelving → Mark GRN Complete. Inventory push to channels happens off Mark GRN Complete (existing §9 wiring; not §10's concern).

## Inbound (ERPNext side, on EE GRN-Complete pull / webhook)

§9's `getGrnDetails` is the upstream — the GRN-Complete is detected the same way, and the integration traces it back to its originating §10 push via the STN/PO referenceCode = DN/SI name link.

7. **IPR auto-created** (always, both GSTIN cases):
   - `is_internal_supplier=1`; supplier = Internal Supplier on destination's books.
   - Source WH = destination Company's GIT; destination WH = the EE-mapped receiving warehouse.
   - Lines from GRN payload: `received_qty = received_quantity`, `rejected_qty = qc_fail`, `accepted_qty = received_quantity − qc_fail`. Accepted → destination WH; rejected → destination Company's `default_rejected_warehouse`; un-received balance stays in GIT.
   - Cross-refs: originating DN (and SI, if any); `ecs_easyecom_grn_id`, `ecs_easyecom_grn_detail_id`; reuse §9 back-ref convention.
8. **IPR submit gate:**
   - Same GSTIN → IPR auto-submits.
   - Different GSTIN AND source-side SI is **Submitted** → IPR auto-submits.
   - Different GSTIN AND source-side SI **NOT Submitted** (missing OR Drafted) → IPR stays **in Draft**. Integration emits an **error notification** (ERPNext-native: ToDo on the relevant ERP user(s), Comment on the IPR explaining the block). No GIT-to-destination movement until IPR submits. Reason: source-side supply hasn't legally crystallised; auto-submitting IPR would create a pending ITC claim against a supply that doesn't legally exist yet, breaking GSTR-2B coherence.
   - **Auto-retry on SI submit:** when the ERP user submits the SI later, a `doc_event` hook on Sales Invoice `on_submit` detects the matching drafted IPR (via the DN→SI→IPR cross-reference) and auto-submits it. Chains IPI + DN creation per step 9.
9. **After IPR submits** (both auto-submit and SI-triggered retry paths):
   - **Different GSTIN** → integration auto-creates **Internal Purchase Invoice in DRAFT**, sized to **dispatched qty (mirroring submitted SI)**, supplier = Internal Supplier. Cross-refs IPR + SI. ERP user submits for full ITC claim.
   - **Different GSTIN AND cumulative received < dispatched** → integration auto-creates a **Debit Note against IPI in DRAFT**, sized to the **gap** (= dispatched − cumulative_received). Line rate matches IPI; tax breakdown mirrors IPI's so the reversal matches the claim. ERP user submits to reverse proportional ITC.
   - **Same GSTIN** → no IPI, no DN.

## Multi-GRN against the same DN/SI

The most operationally complex piece. Each follow-up GRN against the same DN/SI:

10. **New IPR** auto-created for the new GRN's received qty. Auto-submit gate per step 8 (same conditions).
11. **GIT balance** reduces by the new received qty.
12. **Draft DN revision:**
    - DN gap recomputed as `dispatched − cumulative_received` across all IPRs.
    - If gap > 0 → draft DN line qtys updated to new gap.
    - If gap == 0 → draft DN **cancelled** (not deleted — preserves audit trail with cancellation Comment naming the closing GRN).
13. **Submitted-DN edge case:** if the ERP user already submitted the DN earlier (acknowledging loss) and a late GRN arrives bringing some/all of the "lost" stock:
    - New IPR auto-created with received_qty, but **stays in Draft** (NOT auto-submitted).
    - Integration raises an Integration Discrepancy: "Submitted DN exists for this transfer — receipt cannot auto-submit; manual reconciliation needed." Visible to ERP user (To-Do, Comment on IPR).
    - ERP user investigates, reverses or adjusts the submitted DN per their accounting judgement (Purchase Invoice / Journal Entry — their call), then manually submits the drafted IPR.
    - This is the **submitted-DN-late-GRN block**, parallel to the SI-not-submitted block — same "refuse, don't auto-submit when financial precondition unmet" principle.

## Aged GIT (lost-in-transit)

GIT balance > 0 after `lost_in_transit_threshold_days` (existing §3.3.6 setting, default 30):
- ERPNext-native nudge to ERP user (ToDo or Notification rule) on the draft DN and on the originating DN.
- ERP user decides: submit the DN (accept loss, reverse ITC for the gap) or investigate further.
- Integration does NOT auto-submit the DN — that's an ERP user decision.

## Out of §10 scope (write boundary lines explicit in the SPEC patch notes)

- Both source AND target non-EE warehouses → pure ERPNext, integration inert.
- Two **different legal entities** (truly arms-length) → not §10, that's §11 (SI) + §9 (PO).
- §10 covers multi-Company-same-legal-entity (different GSTINs of same legal entity, modelled as separate ERPNext Companies). This **inverts** the current SPEC §10.6 which said multi-Company is out — that wording must be replaced.

## EE-originated transfers (the §9 self-GRN entry point)

GRNs created in EE without an ERPNext-initiated §10 push (e.g. EE-side internal inwards, batch loads). §9's existing self-GRN detection (`vendor_c_id == inwarded_warehouse_c_id`) routes these to §10 inbound. **They have no originating DN to reference** — the IPR is standalone, like §9's unknown-PO drift case. Standalone-IPR semantics:
- Created in Draft (no SI exists, hence no submit gate cleared).
- ERP user reviews and submits (or dismisses).
- Same "FDE-driven Create-PR-from-GRN" mechanism §9 corrective commit added — likely reuse that wiring directly.

This is the **§9-flagged STN-routing-live-verification prerequisite** (see §9 Stage 3 carry-forward): pre-§10 build, an FDE triggers a real self-GRN on Harmony, inspects `vendor_c_id`, confirms it equals the warehouse's company_id. If yes, §10 builds on it. If no, §9's self-GRN check needs adjusting before §10. **This probe must run before Stage 1 of §10.**

## §10 DocTypes / fields introduced

- **EasyEcom §10 Transfer Map** (new): one row per §10 transfer, autoname e.g. `ECS-XFER-{dn_name}`. Fields: linked DN, linked SI (nullable), source/target WH, source/target Company-GSTIN, GST-different flag, EE-side doctype (STN/PO), EE-side id, list of linked IPRs (table), linked IPI (nullable), linked draft-DN (nullable), GIT balance (derived), status enum (Mapped / SI-Pending / SI-Submitted / EE-Pushed / Partial-Received / Fully-Received / DN-Submitted-Locked / Drift / Disabled). Reuse EasyEcom Drift/Exclude Field children from 8f.
- **Custom fields on existing docs:** `ecs_section10_transfer_map` (Link) on DN, SI, IPR, IPI, Debit Note — back-references to the Transfer Map row. Same pattern as §9's PR back-refs.
- **EE endpoint constants:** STN-create endpoint added to `endpoints.py` (path TBD, see §10.G).
- **Settings:** confirm `default_in_transit_warehouse` + `lost_in_transit_threshold_days` exist per §3.3.6 (they do — load-bearing now for §10).
- **Reuse §9's Sync Record Line child** — §10 inbound is a composite document (GRN → IPR with N lines), one Sync Record per inbound event with line children.

## Stages

### Stage 1 — Substrate
- EasyEcom Transfer Map DocType + status enum + back-ref custom fields on DN/SI/IPR/IPI/Debit Note.
- Auto-creation of Internal Customer/Supplier pairs (whitelist action; idempotent; runs at go-live or on EE-linked-Company addition).
- Precheck `precheck_section10_go_live` (parity with §9 buying precheck): verifies GIT/rejected/Internal-pair config across all EE-linked Companies, surfaces blockers.
- Settings field additions if needed.
- STN endpoint constant placeholder (pending payload).
- Tests: DocType + permissions, naming, Internal-pair auto-create idempotency, precheck output, §8/§9 regression.
- **NO flow logic. NO EE calls. NO doc creation beyond Internal Customer/Supplier setup.**

### Stage 2 — Outbound (DN submit → SI draft → EE push)
- Gate-0 hook on DN submit.
- GSTIN comparison + SI auto-draft (different-GSTIN only).
- EE push routing: STN (source EE-mapped) or PO (source not EE-mapped, target EE-mapped). STN payload pending §10.G; PO path reuses §9 machinery.
- Transfer Map row created and populated; status transitions through SI-Pending / EE-Pushed.
- DN-rename coordination (parity with §9 PO rename behaviour from Stage 2).
- Tests: Gate-0 inert case, same-GSTIN (no SI), different-GSTIN (SI in Draft), STN push (source EE-mapped), PO push (source not EE-mapped), missing Internal pair → blocking precondition error.

### Stage 3 — Inbound (GRN-Complete → IPR + IPI + DN auto-creation)
- §9 GRN pull recognises §10 transfers via Transfer Map / DN reference (not via §9 PO path).
- IPR auto-creation. Submit gate:
  - Same GSTIN → auto-submit.
  - Different GSTIN + SI Submitted → auto-submit.
  - Different GSTIN + SI not Submitted → Draft + ToDo + Comment.
  - Submitted-DN-late-GRN edge → Draft + Discrepancy.
- Doc-event hook on SI on_submit → auto-retry drafted IPRs whose blocking condition was SI-pending.
- After IPR submit: IPI auto-draft (different-GSTIN only). Debit Note auto-draft against IPI (different-GSTIN + gap > 0). Cumulative gap arithmetic across multi-GRN.
- Sync Record + Sync Record Line per IPR.
- Tests (the big one — table format in build): same-GSTIN clean receipt, different-GSTIN+SI-submitted clean receipt, different-GSTIN+SI-draft IPR-stays-Draft+ToDo, doc_event SI-submit triggers IPR auto-submit + IPI/DN chain, qc_fail split, partial receipt with DN gap, multi-GRN closing the gap (DN cancels), multi-GRN partial (DN shrinks), late-GRN-after-submitted-DN block, EE-originated GRN (no DN, standalone Draft IPR), §9 regression intact.

### Stage 4 — Variance, aged GIT, UI/workspace
- Aged GIT detection (cron tick; existing `lost_in_transit_threshold_days`).
- ERPNext-native nudge mechanism (ToDo creation on responsible ERP user for pending submissions and aged GIT).
- §17 workspace cards: integration-health items only (auto-creation failures, EE-side drift on §10 transfers, blocked IPRs due to integration errors — NOT operational pendings).
- Transfer Map list view (status colours, filters).
- Sidebar↔workspace lockstep regression (the 8f-established guard).
- Tests: aged GIT cron fires nudges, workspace cards count correctly, lockstep regression green.

(Operations Dashboard for ERP user is **out of §10 core build**, deferred as future polish.)

## §10.G — STN payload (GROUNDED 2026-05-29 against live Harmony round-trip)

EE uses a unified order-creation endpoint for B2C/B2B/STN/Production Orders, discriminated by `orderType`. STN is `orderType: "stocktransferorder"`. Real Harmony round-trip confirmed: order STNORDERTEST1 created successfully, response carried `SuborderID/OrderID/InvoiceID`.

**Endpoint:** `POST /webhook/v2/createOrder`
**Host:** `https://api.easyecom.io`
**Headers:** `Content-Type: application/json`, `Authorization: Bearer <JWT>` (per-location JWT cache — same machinery as §9 endpoints; no x-api-key required on this endpoint per the live test).

**Body (the §10 wire contract, minimal — stripping fields the live test sent but STN ignores):**

```json
{
  "orderType": "stocktransferorder",
  "orderNumber": "<DN name>",                // content-channel key; primary join
  "orderDate": "<DN posting_date in UTC, e.g. 2026-05-28 23:39:50>",
  "expDeliveryDate": "<DN delivery_date in IST, e.g. 2026-05-29 23:39:50>",
  "shippingCost": 0,                          // placeholder, internal transfer
  "paymentMode": 5,                           // 5=Prepaid placeholder (configurable via setting)
  "shippingMethod": 1,                        // 1=Standard COD placeholder (configurable via setting)
  "packageWeight": <Σ item.weight × qty, grams, else 0>,
  "packageHeight": 0, "packageWidth": 0, "packageLength": 0,
  "items": [
    {
      "OrderItemId": "<DN_name>-L<line_idx>",  // our explicit per-line key for stable back-refs
      "Sku": "<Item Map sku>",                 // or "ean" or "AccountingSku" — sku-first priority per §9 convention
      "Quantity": "<line qty as string>",
      "Price": <line rate>,
      "itemDiscount": 0                        // placeholder for internal transfer
    }
    // ... one per DN line
  ],
  "customer": [
    {
      "customerId": <Internal Customer.ee_customer_id from §8e Customer Map>,
      "billing": {
        "name": "<destination Company name>",
        "addressLine1": "<destination Company primary Address line 1>",
        "addressLine2": "<line 2>", "postalCode": "<pin>", "city": "...", "state": "...",
        "country": "India", "contact": "<phone>", "email": "<email>"
      },
      "shipping": {
        "name": "<destination warehouse display name>",
        "addressLine1": "<destination warehouse Address line 1>",
        // ... rest mirror destination warehouse Address
      }
    }
  ]
}
```

**Field-by-field locked decisions:**

- `orderNumber` = ERPNext DN name. Stable, always present, single-key strategy across same-GSTIN and different-GSTIN.
- `orderDate` UTC; `expDeliveryDate` IST per docs. Format `YYYY-MM-DD HH:MM:SS`.
- `is_market_shipped` **OMITTED** — documented mandatory but EE accepts its absence on STN per the live round-trip. If a deployment ever needs it, settings-configurable. (FAQ confirms it's predefined at order-creation time and can't be updated later — so omission means EE applies its default.)
- `closed` OMITTED (not relevant for STN).
- `queue` OMITTED (unclear semantics for STN; live test omitted it without issue).
- `paymentMode: 5` (Prepaid) and `shippingMethod: 1` (Standard COD) are EE-required placeholders; both configurable via a new `stn_default_payment_mode` / `stn_default_shipping_method` settings pair on the EasyEcom Account record (or reuse GRN/Inward Policy section). Default values as shown; FDE override if a deployment's EE config differs.
- `packageWeight` computed from `Item.weight_per_unit × quantity` summed across lines (grams). If Item masters lack weight, send `0`. Other package dimensions: send `0` (per-line dims don't map cleanly to order-level on multi-line STNs).
- Sales/COD fields **OMITTED entirely** — `paymentGateway`, `walletDiscount`, `promoCodeDiscount`, `prepaidDiscount`, `paymentTransactionNumber`, `collectableAmount`, `salesmanId`, `discount`, `marketplace_id`, `custom_fields`, `latitude/longitude`, `gst_number`, `appointment_*`, `company_carrier_id`, `is_pricing_master`, `orderAssignmentProperty`. The live postman payload sent some of these with placeholder values; we omit cleanly. If Stage 2 testing shows any are silently required, add back per finding.
- Items: per-line `OrderItemId` set explicitly to `{DN_name}-L{line_idx}` for stable line back-refs (parity with §9 PO line back-refs). Sku/ean/AccountingSku — sku-first priority resolution via Item Map. `productName` OMITTED (EE knows it from the SKU). `itemDiscount: 0` placeholder.
- Customer block: single-element array (EE quirk). `customerId` from §8e Customer Map's `ee_customer_id` on the auto-created Internal Customer (Stage 1 auto-creation). Addresses sent **inline on every push** (not relying on EE to look them up from customerId) — robust against drift. `billing` = destination Company primary Address; `shipping` = destination warehouse Address.

**Response (captured to Transfer Map):**

```json
{
  "code": 200,
  "message": "<orderNumber> created successfully",
  "data": {
    "Status": 200,
    "Message": "Success SuborderID:... OrderID:... InvoiceID:...",
    "SuborderID": "<string int>",     // line-level EE id
    "OrderID": "<string int>",        // ORDER-LEVEL EE id — PRIMARY STATUS-CHANNEL KEY
    "InvoiceID": "<string int>"       // EE-side invoice id (downstream fulfillment use)
  }
}
```

Capture all three to the Transfer Map row. **`OrderID` is the primary status-channel key** (analogous to §9's `po_id` on `updatePoStatus`). All three returned as strings — store as Data, not Int, on the Map row.

**Idempotency:** keyed on `orderNumber` (= DN name). Re-pushing with the same orderNumber should either return the existing OrderID or error — Stage 2 verifies which and handles accordingly. The Transfer Map's status enum already tracks whether EE-push has happened, so the integration's own gate prevents accidental re-push.

**STILL UNGROUNDED — Stage 2 STOP-and-ask items:**

- **STN cancel/amend endpoint.** Almost certainly `/webhook/v2/cancelOrder` or `updateOrderStatus` (analogous to §9's `updatePoStatus`), but not in the doc page shared. Stage 2 STN-branch builds create-only; cancel/amend awaits payload grounding. Block on this before Stage 2's cancel test.
- **STN status echo for drift detection.** §9 reads `po_status_id` from `getGrnDetails`. Does the GRN payload echo the originating STN's order status, or does §10 need a separate `getOrderStatus` poll? Stage 3 question; deferrable until then.

**FAQ-clarified semantics worth pinning:**

- `orderNumber` is unique per seller account.
- `OrderItemId` is the Suborder-equivalent, unique non-repetitive — our `{DN_name}-L{line_idx}` convention satisfies this.
- `Price`, `itemDiscount`, `collectableAmount` accept decimals despite Integer type (per FAQ).
- `itemDiscount` is per-line **total**, not per-unit.
- `taxIdentificationNumber` for B2B: pass `"URP"` if unknown (new B2B module only) — likely irrelevant for STN.
- State codes follow standard GST state codes.

## OPEN DECISIONS (resolve during stages)

1. **Internal Customer auto-creation timing.** Run at §8a Location flip (go-live) automatically, or on first §10 transfer's pre-flight (lazy)? Lean: go-live, with precheck enforcement.
2. **`ecs_section10_transfer_map` back-ref propagation.** Inserted via patch on existing custom-field framework, or as JSON in the §10 fixture? Both work; align with how §9 added `ecs_easyecom_grn_id` etc.
3. **Submitted-DN late-GRN reversal mechanism.** Integration blocks (locked). But does it auto-create a *suggested* reversal document (Purchase Invoice or Journal Entry) in Draft for ERP user review, or strictly hands-off with a Comment? Lean: strictly hands-off — submitted-DN reversal is too case-specific to template.
4. **Same GSTIN with separate ERPNext Company.** Possible but rare. Treat as different-GSTIN (full machinery fires) or detect-and-treat-as-same (suppress SI/IPI/DN)? Lean: GSTIN equality is the discriminator regardless of Company structure.
5. **Cancel/amend of DN after EE push.** ERPNext PO cancel push from §9 maps to po_status=7 on `updatePoStatus`. STN cancel/amend semantics TBD with payload.

## Carry-forward to §11 / §12

- The Internal Customer auto-creation mechanism (Stage 1) is reused if §11 needs Internal-Customer-flavored SI flows for any sale-shaped case.
- The "financial precondition not met → Draft + notify, never auto-submit" invariant is a candidate cross-cutting rule. Worth promoting to §7 (cross-cutting) at §10 closeout if it survives intact.

## Carry-in from §9 / pre-§10 gates (resolve before Stage 1)

- ✅ §9 corrective commit landed (FIX 1 + FIX 2 on `main`). **Two Harmony re-smokes still pending** before §9 closeout (re-smoke 1: unknown-PO GRN drift → FDE create-PR; re-smoke 2: pause-defer all three po_status pushes). These do not block §10 Stage 1 substrate build but should be clean before §10 Stage 2 push wiring.
- ⚠ STN routing live-verification (the §9 Stage 3 carry-forward): FDE triggers a real self-GRN on Harmony, inspects `vendor_c_id`, confirms equals warehouse company_id. **Required before §10 Stage 3 (inbound)** — Stage 1 substrate and Stage 2 outbound push can proceed without it; only the §9-self-GRN-routes-to-§10 inbound path needs this verified.
- ✅ STN payload grounded (§10.G — live Harmony round-trip 2026-05-29). §10 Stage 2 STN branch can build.
- ⏳ STN cancel/amend endpoint **NOT YET GROUNDED** — STOP-and-ask in Stage 2 when the cancel/amend code path is reached. Create-only path can build now.

## Build order

§10 build runs Stage 1 → 2 → 3 → 4, same discipline as §9, after the carry-in gates clear and STN payload is grounded. Closeout produces (parity with §9 closeout deliverables):
- `process/primers/FDE_PRIMER_section_10_stock_transfer.md` (own primer — §10 is a flow, distinct from §8 masters and §9 buying).
- `process/test_scripts/section_10_stock_transfer.md` (with the ERP-user/FDE separation clearly called out in the do/confirm/good sections).
- `spec_sections/SPEC_10_patch_notes.md` (rewrites SPEC.md §10.1–§10.8 with the Internal-Customer-pattern model; replaces the old §10.6 multi-Company exclusion with the inter-GSTIN-via-Internal-pair inclusion).
- BUILD_TRACKER §10 entry.
- docx regen.

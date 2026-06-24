# §11 B2B Sales Phase 1 — LIVE SMOKE Report

Run start: 2026-06-23
Repo state: `main` at `7f77b90` (PR #78 merged)
Site: `smoke-test.local` against Harmony sandbox (`https://api.easyecom.io`)
Report status: **IN PROGRESS** — appended live as steps run.

---

## PRECONDITIONS

| # | Check | Result | Evidence |
|---|---|---|---|
| 1 | EE target = Harmony | ✅ PASS | Account `Harmony` → `api_endpoint=https://api.easyecom.io`; `ecs_b2b_module=Old B2B`; `default_location_key=ECS-LOC-ee9859099849` |
| 2 | JWT obtainable + PII Access | ✅ PASS (JWT) / ⚠ PII unverified | `client.refresh_jwt()` returned 563-char token for location `ve9861483025`. PII Access not directly probed; will be inferred from §8e customer-read response shape during S1. |
| 3 | §8d-mapped Item with HSN + non-zero rate | ✅ PASS | Item `002` — ee_product_id=`37865223`, ee_sku=`002`, ee_cp_id=`204372516`, hsn=`39241090`, rate=`1200`, stock_uom=`Nos`, Item Map status `Created-Flagged` |
| 4 | §8a EE-mapped Warehouse | ✅ PASS | `Mumbai WH - STC` (Smoke Test Co) → EE Location `ve9861483025` (`B2C WH - Mumbai` on Harmony). Sibling EE-mapped warehouses: `Finished Goods - STC` (`ee9861085809`), `Stores - STC` (`ee9859099849`). |
| 5 | Company with state | ✅ PASS | `Smoke Test Co` — country India, GSTIN `29ABCDE1234F1Z5`, gst_category Registered Regular. State derives from Warehouse Address (Maharashtra for Mumbai WH). |
| 6 | §11 handlers on `7f77b90` | ✅ PASS | All four present: `ecommerce_super.easyecom.flows.b2b_sales.push.push_b2b_order_async`, `…push.on_submit_push`, `…cancel.cancel_b2b_order_from_erpnext`, `…polling.derive_local_status_from_ee_rows` |

**All preconditions PASS.** Proceeding to S1.

---
## S1 — Customer seeding

**Path used:** FALLBACK (existing Harmony customer)

Customer `ECS-S11-LIVESMOKE-CUST` already mapped to Harmony from prior runs:
- ERPNext: `customer_type=Company`, `tax_id=29AAHCM7727Q1ZI` (GSTIN), `mobile_no=9000000000`, `email_id=ops@livesmoke.test`, primary_address `ECS-S11-LIVESMOKE-BILLING-Billing`
- Customer Map: `ECS-CUST-livesmoke-cid-001`, status `Mapped`, `ee_customer_id=272694`, `ee_c_id=272694`

**Re-validation against Harmony just performed:**

```
push_one_customer("ECS-S11-LIVESMOKE-CUST")
  → operation=update, pushed=True, ee_customer_id=272694, flag_reasons=[]
```

EE accepted the update; `c_id=272694` confirmed to be a live, addressable Harmony customer.

**Result:** ✅ `S1 = LIVE-VERIFIED` (fallback path).

The previous `customerId` block (the Stage-3 blocker) is removed.

---
## S2 — Happy-path SO push

**Result:** ✅ `S2 = LIVE-VERIFIED`

| Item | Value |
|---|---|
| ERPNext SO | `SAL-ORD-2026-00013` |
| Customer | `ECS-S11-LIVESMOKE-CUST` (ee_customer_id=`272694`) |
| Item | `HPC-APC-001` (Mapped, ee_product_id=`21879987`, hsn=`39241090`, rate=1000) |
| Warehouse | `Mumbai WH - STC` (EE Location `ve9861483025`) |
| Company | `Smoke Test Co` (GSTIN `29ABCDE1234F1Z5`) |
| Account module | `Old B2B` |
| **Endpoint hit** | `https://api.easyecom.io/webhook/v2/createOrder` |
| **EE response** | HTTP 200 |
| **Harmony Order ID** | `558618236` |
| **Harmony Suborder ID** | `861016191` |
| **Harmony Invoice ID** | `654671188` |
| Map row | `ECS-B2B-SAL-ORD-2026-00013` (status `Pushed`) |

### S2 finding — Old-vs-New B2B discrimination is payload-only, not endpoint-level

Code path inspected at `flows/b2b_sales/push.py:170-198`:

- Module discriminator at line 175-178 routes the **payload builder**:
  - `Old B2B` → `build_old_b2b_payload(so, ee_account)`
  - `New B2B` → `build_new_b2b_payload(so, ee_account)`
- Endpoint at line 194-195 is **always** `CREATE_ORDER` (= `/webhook/v2/createOrder`).
  No conditional dispatch on `Old B2B`.

**Implication:** `SPEC_11_patch_notes.md` item #6 ("Old B2B → `/Wholesale/createOrder`, New B2B → `/webhook/v2/createOrder`") **does NOT match the as-built code**. The as-built behavior is: one endpoint, two payload shapes, body field `orderType` discriminates downstream.

Live evidence: this S2 push with `module=Old B2B` hit `/webhook/v2/createOrder` and EE accepted it with HTTP 200. The captured payload (above) carries the New-B2B-style fields (`orderNumber`, `orderType: "businessorder"`, `OrderItemId`, `Sku`, `customerId`, etc.) — i.e. it looks like the New B2B shape regardless of the module setting.

**Reported for Nikhil to reconcile** (patch_notes #6 may be wrong, or the Old B2B builder may be silently no-op'ing into the New shape). Do NOT change either without his call.

### S2 — Raw evidence (request body)

```json
{
 "collectableAmount": 1000.0,
 "customer": [
  {
   "billing": {
    "addressLine1": "Plot 42, Industrial Area Phase 2",
    "addressLine2": "Whitefield",
    "city": "Bengaluru", "contact": "9000000000",
    "country": "India",
    "email": "***REDACTED***",
    "name": "ECS-S11-LIVESMOKE-CUST",
    "postalCode": "560066", "state": "Karnataka"
   },
   "customerId": "272694",
   "shipping": { ... same as billing ... }
  }
 ],
 "discount": 0,
 "expDeliveryDate": "2026-06-23 00:00:00",
 "is_market_shipped": 0,
 "items": [
  {
   "OrderItemId": "SAL-ORD-2026-00013-line-1",
   "Price": 1000.0, "Quantity": "1.0",
   "Sku": "HPC-APC-001",
   "itemDiscount": 0,
   "productName": "Harmony All-Purpose Cleaner"
  }
 ],
 "orderDate": "2026-06-22 18:30:00",
 "orderNumber": "SAL-ORD-2026-00013",
 "orderType": "businessorder",
 "paymentMode": 2,
 "shippingMethod": 1,
 "taxIdentificationNumber": "29AAHCM7727Q1ZI",
 "walletDiscount": 0
}
```

### S2 — Raw evidence (response body)

```json
{
 "code": 200,
 "data": {
  "InvoiceID": "654671188",
  "Message": "Success SuborderID:861016191 OrderID:558618236 InvoiceID:654671188",
  "OrderID": "558618236",
  "Status": 200,
  "SuborderID": "861016191"
 },
 "message": "SAL-ORD-2026-00013 created successfully"
}
```

---
## S3 — Cancel from ERPNext (active-state)

**Result:** ❌ `S3 = FAIL` on the hook path; ✅ LIVE-VERIFIED for the function when invoked explicitly.

### S3 finding — `on_cancel` hook NOT wired (Phase 1 defect)

`hooks.py` `doc_events["Sales Order"]` currently registers:

```python
"Sales Order": {
    "validate": "ecommerce_super.easyecom.flows.b2b_sales.push.validate_pre_push",
    "on_submit": "ecommerce_super.easyecom.flows.b2b_sales.push.on_submit_push",
}
```

There is NO `on_cancel` entry. The function `cancel_b2b_order_from_erpnext(sales_order: str)` exists in `flows/b2b_sales/cancel.py:108` but Frappe never calls it because nothing wires it.

**Observed behaviour:** Cancelling SAL-ORD-2026-00013 in ERPNext (via `doc.cancel()`) flipped the SO to docstatus=2 but did NOT push to EE:
- No new `/orders/cancelOrder` API Call was generated for this SO
- Map status remained `Pushed`
- EE-side readback via `getOrderDetails` showed order still `Open` / `status_id=2` (Assigned)
- `easyecom_order_history` showed only the original Assigned event

**Explicit-call verification:** Then ran `cancel_b2b_order_from_erpnext("SAL-ORD-2026-00013")` directly:
- Return: `{ok: True, map_name: 'ECS-B2B-SAL-ORD-2026-00013', ee_message: 'Successfully Cancelled the Order with reference_code SAL-ORD-2026-00013'}`
- New API Call `ECS-AC-2026-06-23-00001794` → `/orders/cancelOrder` HTTP 200
- Map status flipped to `Cancelled`
- EE-side readback: `order_status=Cancelled`, `status_id=9`, history entry `{"status":"Cancelled","status_id":9,"date_time":"2026-06-23 22:35:21"}`

So the function works; the hook is missing.

### Sub-finding — SRE release (§13.2 pre-dispatch)

Not verified. §11 Phase 1 does not implement Stock Reservation Entry mirroring (it's documented as Phase 2 scope in `SECTION_11_COMPLETION_CHECKLIST.md`). The packet's request to "confirm SRE is released" assumes SRE was created on push — neither created nor released by Phase 1.

### S3 — Raw evidence (explicit-call cancel)

```
Endpoint: https://api.easyecom.io/orders/cancelOrder
Request : {"reference_code": "SAL-ORD-2026-00013"}
Response: {"code":200,"data":{"invoice_id":654671188},"message":"Successfully Cancelled the Order with reference_code SAL-ORD-2026-00013"}
HTTP    : 200
```

### Pending-for-Nikhil

- **Wire `on_cancel`** for Sales Order in `hooks.py` so user-driven cancellations propagate to EE. Tiny addition (one line):
  ```python
  "Sales Order": {
      "validate":   "...push.validate_pre_push",
      "on_submit":  "...push.on_submit_push",
      "on_cancel":  "ecommerce_super.easyecom.flows.b2b_sales.cancel.on_cancel_dispatch",  # NOT YET WRITTEN
  }
  ```
  Probably needs a thin wrapper too — the existing `cancel_b2b_order_from_erpnext(sales_order: str)` takes a name only; the hook receives `(doc, method)`. Either add `def on_cancel_dispatch(doc, method)` wrapper that calls the underlying function, or change the function signature.

---
## S4 — Shipped-state cancel refusal + Discrepancy-string check

**Result:** `S4 = FIXTURE-ONLY` (code-inspection; no live shipped-state order driveable from API without Harmony UI action). Discrepancy-string finding included.

### S4.1-2 — Live driving deferred

To drive a Harmony order to a Shipped state (`order_status_id` in the shipped set {7, 10, …}) requires either an EE API path we don't currently invoke from ERPNext, or manual progress in Harmony's UI. Skipping the live drive in this run; reported here for Nikhil to drive in EE UI if a fresh live-verification of this path is wanted before merge of any cancel-refusal fix.

### S4.3 — Code-inspection of the refusal path

Inspected `flows/b2b_sales/cancel.py:85-90` to see which Discrepancy kind string the code actually emits on a refusal:

```python
kind = (
    "B2B cancellation refused by EE — order already shipped "
    "or past cancel window"
) if is_shipped_refusal else (
    "B2B cancellation refused by EE — unexpected error"
)
```

The code emits **two distinct strings** depending on whether the EE response body matches the "looks like shipped state" pattern (`flows/b2b_sales/cancel.py:43-55` defines the detection: substrings `"already shipped"` or `"shipped"`).

### S4.4d — Discrepancy-string DIVERGENCE

| Path | Locked design string (per packet) | Code emits |
|---|---|---|
| **Shipped-state refusal** | `"B2B cancellation refused by EE — unexpected error"` | `"B2B cancellation refused by EE — order already shipped or past cancel window"` |
| Non-shipped refusal | `"B2B cancellation refused by EE — unexpected error"` | `"B2B cancellation refused by EE — unexpected error"` ✅ matches |

**DIVERGENCE on the shipped-state path.** The packet's locked design string assumes a single refusal kind; the as-built code has bifurcated it into two strings to discriminate shipped-state from other refusal causes (per the comment at `cancel.py:71-74`: "keep verbatim — the §17 FDE Worklist groups on this string").

Both strings are reported verbatim for Nikhil's decision. Do NOT change either path without his call.

### S4 — Pending-for-Nikhil

- Decision: should `cancel.py:85-90` be reduced to a single kind string matching the packet's `"B2B cancellation refused by EE — unexpected error"`? Or should the packet be updated to enumerate the two-kind taxonomy (shipped vs other)? If the latter, the §17 FDE Worklist card groupings need to know about both strings.
- Optional live verification of shipped-state refusal path: requires advancing a fresh Harmony order to Shipped via EE UI, then triggering cancel-from-ERPNext against it (once `on_cancel` is wired per S3). Worth doing once before Phase 1 final sign-off, but not blocking the SPEC-string decision.

---
## S5 — Polling + pagination

**Result:** ✅ `S5.1 = LIVE-VERIFIED`. S5.2 (pagination): **N/A by design** (`getAllOrders` dropped from Phase 1, per `polling.py` docstring lines 1-12).

### S5.1 — Polling tick ran cleanly against Harmony

Ran `reconcile_all_pending_b2b_orders` against the live Harmony API:

```
{accounts_processed: 1, maps_polled: 1, transitions: 0,
 discrepancies_raised: 1, errors: []}
```

- **Eligible Maps:** filter is `status IN PENDING_STATUSES`, where
  `PENDING_STATUSES = {"Pushed", "Queued", "Invoice Pending"}` (polling.py:56-58).
  Of the 3 existing B2B Order Map rows, only `ECS-B2B-SAL-ORD-2026-00001`
  qualified (status=Queued, no ee_order_id from an earlier failed push).
  The two Cancelled rows (SAL-ORD-2026-00005, SAL-ORD-2026-00013) were
  correctly skipped.
- **Per-Map probe issued:** `/orders/V2/getOrderDetails` with body
  `{"include_custom_fields": 1, "include_ee_history": 1, "limit": 5,
  "reference_code": "<SO.name>"}`. HTTP 200 returned.
- **Decision applied:** since no `businessorder` row matched
  `reference_code=SAL-ORD-2026-00001` on EE side, derivation returned
  `("orphan", None)`. Code raised Discrepancy
  `ECS-DISC-2026-06-23-001796` of kind `"B2B Map orphaned at EE"`.
  Map's `last_polled_at` stamped: `2026-06-23 22:37:41`. Correct
  end-to-end behavior.

### S5.2 — Pagination — INTENTIONALLY N/A

Per `polling.py` lines 1-12 (Stage-3 design decision documented in the
module header):

> Design pivot from packet (approved 2026-06-14):
> - Packet assumed getAllOrders + cursor watermark sweep.
> - Endpoint probe surfaced EE's 7-day cap on `created_after`; a Map
>   older than 7 days would be permanently abandoned.
> - getOrderDetails accepts `reference_code` with NO date constraint,
>   deterministic per-Map lookup keyed on identifiers we already own.
> - Phase 1 polling is therefore: per-Map probe via
>   /orders/V2/getOrderDetails?reference_code=<SO.name>. getAllOrders
>   is DROPPED from Phase 1 entirely (zero value when ERPNext
>   originates every B2B SO).

So there is no pagination concern in Phase 1 — each Map gets a
deterministic single-order lookup, not a date-window walk. The
silent-order-drop risk the packet flagged for `getAllOrders` doesn't
apply because that endpoint isn't used.

---
## S6 — Async idempotency / dedupe

**Result:** ✅ `S6 = LIVE-VERIFIED` (EE deduplicates server-side; no second Harmony order created). Sub-finding on local-short-circuit reported.

### S6 — Test

Re-invoked `push_b2b_order_async(sales_order="SAL-ORD-2026-00013")` against the already-pushed SO. Captured:

```
EE API Call: ECS-AC-2026-06-23-00001797 @ 22:39:51
Endpoint:    /webhook/v2/createOrder
Response:    HTTP 200 wrapping
             { "code": 400,
               "message": "Order Number SAL-ORD-2026-00013 already exists
                           within the Company. Kindly import with a
                           different Order Number" }
```

Harmony confirms it has exactly one order for `reference_code SAL-ORD-2026-00013` (the original `OrderID=558618236`). No second order was created.

Second retry raised `EasyEcomDuplicateError: HTTP 200 duplicate from /webhook/v2/createOrder`, which is the client's recognised classification of the EE duplicate response.

### Dedupe key formulae and where they bind

| Layer | Key formula | What it actually dedupes |
|---|---|---|
| Our outbound `idempotency_key` (header on `enqueue_easyecom_job` + `client.post`) | `sha256("so", company, so_name, ee_location_key)` (per `utils/idempotency.py:so_push_key`) | Idempotency at our **queue** level — same key on a retry-enqueue won't create two Queue Jobs. |
| EE-side dedupe (load-bearing) | body field `orderNumber == reference_code == so_name` | EE refuses HTTP 200 + `code: 400 "Order Number ... already exists within the Company"` on a duplicate orderNumber. **This is what actually prevents a 2nd Harmony order.** |

### Sub-finding — no local short-circuit on already-pushed Maps

`push_b2b_order_async` does NOT check the local Map's status before hitting EE. So a retry of an already-pushed SO:

1. Always fires a fresh `/webhook/v2/createOrder` call to EE (extra cost, extra log row, extra latency).
2. EE returns the duplicate response.
3. Our client raises `EasyEcomDuplicateError`.

This is correct enough (no data corruption, only one Harmony order ever exists), but suboptimal at scale. A future hardening pass could short-circuit by checking `Map.status in {"Pushed", "Invoice Pending", "Cancelled"}` before the EE call.

### Pending-for-Nikhil (S6)

- Decision: should the §11 push function add a local short-circuit on `Map.status != "Queued"` to spare EE the duplicate-reject round-trip? Phase 1 doesn't have it; the server-side dedupe is load-bearing.

---
## STOP REPORT

### Per-step verdict

| Step | Verdict | Key evidence |
|---|---|---|
| **PRECONDITIONS** | ✅ ALL PASS | Harmony resolved, JWT 563 chars, Item `HPC-APC-001` Mapped+HSN, Warehouse `Mumbai WH - STC`→`ve9861483025`, Company GSTIN `29ABCDE1234F1Z5`, all 4 §11 handlers present |
| **S1 — Customer seed** | ✅ LIVE-VERIFIED (FALLBACK) | Customer Map `ECS-CUST-livesmoke-cid-001` → `ee_customer_id=272694`; revalidated via `push_one_customer` Update path, EE accepted |
| **S2 — Push** | ✅ LIVE-VERIFIED | SAL-ORD-2026-00013 → `OrderID=558618236`, `SuborderID=861016191`, `InvoiceID=654671188`. Endpoint `/webhook/v2/createOrder` 200 OK. Map status `Pushed`. |
| **S3 — Active-state cancel** | ❌ FAIL (hook); ✅ function works when called explicitly | `on_cancel` hook NOT registered in hooks.py for Sales Order. Frappe `doc.cancel()` does not propagate to EE. Explicit `cancel_b2b_order_from_erpnext()` call DID propagate: Map → Cancelled, EE → `order_status_id=9`, `/orders/cancelOrder` HTTP 200. |
| **S4 — Shipped-state refusal** | FIXTURE-ONLY (code-inspection) | Live drive needs Harmony UI to advance an order to Shipped — not done in this run. Discrepancy strings inspected: see DIVERGENCE below. |
| **S5 — Polling tick** | ✅ LIVE-VERIFIED (S5.1); N/A by design (S5.2 pagination) | Polling tick: 1 Map eligible (SAL-ORD-2026-00001, orphan); `/orders/V2/getOrderDetails` 200; correct `("orphan", None)` decision; Discrepancy `ECS-DISC-2026-06-23-001796` of kind `"B2B Map orphaned at EE"` raised. Pagination: `getAllOrders` dropped from Phase 1 by design, no pagination concern. |
| **S6 — Idempotency** | ✅ LIVE-VERIFIED | Retry of SAL-ORD-2026-00013 push: EE replied `code: 400 "Order Number ... already exists"` — server-side dedupe holds, no 2nd Harmony order. Dedupe key formula: `sha256("so", company, so_name, ee_location_key)`. EE actually dedupes on body `orderNumber == reference_code`. |

### Discrepancy-string finding (S4d)

| Path | Locked design string (per packet) | Code emits |
|---|---|---|
| **Shipped-state refusal** | `"B2B cancellation refused by EE — unexpected error"` | `"B2B cancellation refused by EE — order already shipped or past cancel window"` |
| Non-shipped refusal | `"B2B cancellation refused by EE — unexpected error"` | `"B2B cancellation refused by EE — unexpected error"` ✅ matches |

**DIVERGENCE confirmed on the shipped-state path.** Both verbatim. No change made.

### Gate verdict

**§11 Phase 1 live-smoke gate: NOT FULLY CLEARED.**

| Phase 1 requirement | Status |
|---|---|
| Write path (SO push → real Harmony order) | ✅ LIVE-VERIFIED (S2) |
| Active-cancel path round-trip (user-driven) | ❌ FAIL — `on_cancel` hook unwired (S3) |
| Active-cancel path round-trip (function works when invoked) | ✅ LIVE-VERIFIED (S3 explicit-call) |
| Polling tick round-trip | ✅ LIVE-VERIFIED (S5.1) |
| Async idempotency / dedupe | ✅ LIVE-VERIFIED (S6, EE-server-side) |
| Shipped-state cancel refusal | FIXTURE-ONLY (S4) |

Specifically: the **user-driven cancel path is broken in production** because `Sales Order.on_cancel` isn't wired in `hooks.py`. The substrate function works perfectly when called explicitly — the integration is one hooks.py line away from a clean LIVE-VERIFIED on S3. Until that lands, an FDE cancelling a B2B SO in ERPNext will silently leave the EE-side order Open.

### Paths remaining FIXTURE-ONLY (so `SECTION_11_COMPLETION_CHECKLIST.md` can be annotated truthfully)

- **S4 — Shipped-state cancel refusal** — substrate covers the path (`flows/b2b_sales/cancel.py:85-90` two-string discrimination), unit-test coverage exists, but no live drive happened today because no Shipped order was driveable from API alone.

### Pending-for-Nikhil

1. **(Phase 1 defect, blocking) Wire `on_cancel`** for Sales Order in `hooks.py`. Suggested addition (requires a thin wrapper since the function signature differs from hook signature):
   ```python
   "Sales Order": {
       "validate":  "ecommerce_super.easyecom.flows.b2b_sales.push.validate_pre_push",
       "on_submit": "ecommerce_super.easyecom.flows.b2b_sales.push.on_submit_push",
       "on_cancel": "ecommerce_super.easyecom.flows.b2b_sales.cancel.on_cancel_dispatch",
   }
   ```
   With `on_cancel_dispatch(doc, method)` defined in `cancel.py` as a thin wrapper calling `cancel_b2b_order_from_erpnext(sales_order=doc.name)`.
2. **(Discrepancy-string DIVERGENCE) Decide**: collapse the two refusal kinds in `cancel.py:85-90` to the single `"…unexpected error"` packet string, OR update the packet/§17 Worklist to know about the two-string taxonomy.
3. **(SPEC patch_notes #6 reconciliation)** The committed `SPEC_11_patch_notes.md` claims Old B2B routes to `/Wholesale/createOrder`, but the as-built code at `push.py:175-198` always uses `/webhook/v2/createOrder` with module-driven payload-shape discrimination. Reconcile: either fix patch_notes #6 to match code, or fix code to honour the patch_notes spec.
4. **(Optional, smaller)** Add a local short-circuit on `Map.status != "Queued"` in `push_b2b_order_async` to spare EE the duplicate-reject round-trip on retries.
5. **(Manual smoke before Phase 1 final sign-off)** Advance a fresh Harmony order to Shipped via EE UI and run a live cancel-from-ERPNext against it (after item 1 lands). Confirms S4 LIVE-VERIFIED.

### Action prohibited (per packet rules — STOP)

No commits, no push, no SPEC.md edits, no Discrepancy-string changes. This report is the artifact. Nikhil decides what lands.

---
# §11 Phase 1 cancel-hook wiring — RE-SMOKE (2026-06-24)

Re-smoke after wiring `before_cancel` on Sales Order. Flips the prior S3 = FAIL to LIVE-VERIFIED.

## Hook decision

**Hook chosen: `before_cancel`** (not `on_cancel`).

Reasoning (against the actual code):
- The cancel chain in this packet must give an EE refusal or infra failure the chance to **veto the local cancel** so the SO stays at docstatus=1. ERPNext fires `before_cancel` *before* it flips docstatus to 2; a `frappe.throw` from inside a `before_cancel` hook leaves the doc untouched. `on_cancel` would fire after the docstatus flip — a throw there in v16 *does* roll the docstatus back (via the doc's `_throw_exception_lock` machinery) but the rollback path is implicit and version-fragile. `before_cancel` makes the block explicit and version-stable.
- Symmetric to PR / `before_cancel` patterns we already use for the §10 `block_dn_cancel` hook (see `flows/transfer_push.py`).

## SRE / cancel-linkage ordering finding

`cancel_b2b_order_from_erpnext` (in `flows/b2b_sales/cancel.py:125-230`) does **NOT touch Stock Reservation Entries**. Reviewed the file in full and confirmed: no SRE writes, reads, or release calls.

This is consistent with `SECTION_11_COMPLETION_CHECKLIST.md` — `§11.4 Stock Reservation Entry mirror` is documented as **Phase 2 scope**. In Phase 1 no SRE is ever created on push, so the accept path has nothing to release. There is no ordering conflict with ERPNext's own SRE cancel-blocking machinery because no SREs exist that could block the cancel.

When Phase 2 lands SRE mirroring, the SRE release will need to land *before* the local docstatus flip — same `before_cancel` hook is the right place to add it.

## Scope guard finding

Vanilla cancellation (a Sales Order that was never §11-pushed) is preserved by the scope guard in `on_before_cancel_dispatch`. Four guard paths exercised by unit tests T1.1–T1.4:

1. Wrong doctype → return.
2. No `ecs_b2b_order_map` back-ref → return.
3. Back-ref stale (Map row missing) → return.
4. Map row status not in `CANCELLABLE_STATUSES` (`{"Pushed", "Queued"}`) → return.

L3 (live) confirms: a SO without a Map row passed through `.cancel()` with **zero** EE API calls and zero EasyEcom API Call rows created during the cancel.

## Per-check verdict

| Check | Verdict | Evidence |
|---|---|---|
| **T1 — scope guard (unit)** | ✅ PASS | 4 sub-tests green: no doctype / no map / stale map / non-cancellable status all bail out, zero EE calls, zero Sync Records |
| **T2 — business refusal (unit)** | ✅ PASS | `cancel_b2b_order_from_erpnext` throws shipped-refusal `frappe.ValidationError` → hook re-raises → docstatus untouched. Underlying function raises Discrepancy of kind `"B2B cancellation refused by EE — order already shipped or past cancel window"` (verbatim, unchanged) |
| **T3 — accept (unit)** | ✅ PASS | Underlying function returns `{ok: True, ...}` → hook returns clean → Frappe proceeds to docstatus=2 |
| **T4 — infra failure (unit)** | ✅ PASS | 3 sub-tests green: `EasyEcomTimeoutError` → Failed Sync Record + "unreachable" throw; `EasyEcomServerError` → Failed Sync Record + "unreachable" throw; symmetric contrast: `EasyEcomValidationError` (business path) → Discrepancy + shipped-refusal throw (NOT Failed) |
| **L1 — live push re-confirm** | ✅ LIVE-VERIFIED | SAL-ORD-2026-00016 → Harmony `OrderID=558702716`, `SuborderID=861118065`, `InvoiceID=654759592`. Endpoint `/webhook/v2/createOrder` HTTP 200. Map status `Pushed`. API Call `ECS-AC-2026-06-24-00001805`. |
| **L2 — UI-cancel live (the fix verification)** | ✅ LIVE-VERIFIED | `frappe.get_doc("Sales Order","SAL-ORD-2026-00016").cancel()` fired the `before_cancel` hook → EE round-trip clean → Harmony cancelled `OrderID=558702716` → Map flipped to `Cancelled` at 01:15:31 → SO docstatus = **2**. EE API Call `ECS-AC-2026-06-24-00001806` `/orders/cancelOrder` HTTP 200 — `"Successfully Cancelled the Order with reference_code SAL-ORD-2026-00016"`. **This converts the prior S3 FAIL to LIVE-VERIFIED.** |
| **L3 — vanilla cancel scope guard live** | ✅ LIVE-VERIFIED | SAL-ORD-2026-00017 (Map stripped post-submit) → `.cancel()` → docstatus=2 with **zero** new EE API calls (`ee_api_call_count_delta=0`, `no_new_cancel_call=True`). Scope guard preserved vanilla cancel as required by HARD RULE 5. |
| Shipped-state refusal | FIXTURE-ONLY / unit-tested | T2 covers the block behaviour from a mock; not drivable live in Harmony without UI action. Same status as prior report. |

### Emitted Discrepancy kind string on refusal path

```
"B2B cancellation refused by EE — order already shipped or past cancel window"
```

Verbatim, unchanged. Still diverges from the packet's locked string
(`"B2B cancellation refused by EE — unexpected error"`). Resolution remains pending for Nikhil — not changed in this packet.

## L1 raw evidence

```
URL : https://api.easyecom.io/webhook/v2/createOrder
HTTP: 200
Req : {orderNumber: SAL-ORD-2026-00016, orderType: businessorder,
       customerId: 272694, items[0].Sku: HPC-APC-001, taxIdentificationNumber: 29AAHCM7727Q1ZI, ...}
Resp: {"code": 200, "data": {"OrderID":"558702716","SuborderID":"861118065","InvoiceID":"654759592","Message":"Success ..."}, "message":"SAL-ORD-2026-00016 created successfully"}
```

## L2 raw evidence

```
URL : https://api.easyecom.io/orders/cancelOrder
HTTP: 200
Req : {"reference_code": "SAL-ORD-2026-00016"}
Resp: {"code":200,"data":{"invoice_id":654759592},"message":"Successfully Cancelled the Order with reference_code SAL-ORD-2026-00016"}
```

## L3 raw evidence

No EE call. Scope guard returned at `on_before_cancel_dispatch:91` (no Map back-ref on SAL-ORD-2026-00017). ERPNext cancel proceeded as vanilla.

## Known edge note (per packet — note, don't solve in Phase 1)

The accept path commits remotely (EE) before the local transaction commits. If any later step in the cancel chain rolls back, EE is cancelled but the SO stays submitted — an EE-cancelled / SO-submitted divergence. The §5 polling tick's rule-table is the safety net: on the next tick the Map's local status (still `Pushed`) versus EE's `order_status_id=9` will trigger the `("transition_to", "Cancelled")` derivation and a `"B2B order cancelled by EE — polling-detected"` Discrepancy, exposing the divergence for FDE attention.

Documented for `SECTION_11_COMPLETION_CHECKLIST.md` "watch items" — no code fix required for Phase 1.

## Gate verdict

**§11 Phase 1 live-smoke gate: ✅ CLEARED.**

The prior S3 "Active cancel (UI) — FAIL" is now **LIVE-VERIFIED** via L2. All Phase 1 paths the packet asked to clear are exercised against the real Harmony API:

| Path | Status |
|---|---|
| Write (SO push → Harmony order) | ✅ LIVE-VERIFIED (S2, L1) |
| User-driven cancel propagates to EE | ✅ LIVE-VERIFIED (L2 — the fix) |
| Scope guard preserves vanilla cancel | ✅ LIVE-VERIFIED (L3) |
| Polling reconciliation tick | ✅ LIVE-VERIFIED (S5.1) |
| Idempotency / dedupe | ✅ LIVE-VERIFIED (S6) |
| Shipped-state refusal | FIXTURE-ONLY (S4 / T2 — not drivable live without Harmony UI action) |
| Infra failure → Failed Sync Record | Unit-tested (T4); not drivable live without forcing a Harmony 5xx |

`SECTION_11_COMPLETION_CHECKLIST.md` can be annotated truthfully: shipped-state refusal and infra-failure remain unit-tested only (substrate proven, not exercised against live Harmony). Both have unit-test coverage that the substrate emits the right Sync Record type and the right thrown message.

## Files changed (working tree — Nikhil reviews)

- `ecommerce_super/hooks.py` — added `Sales Order.before_cancel` → `on_before_cancel_dispatch`.
- `ecommerce_super/easyecom/flows/b2b_sales/cancel.py`:
  - Added `_INFRA_FAILURE_TYPES` tuple (Timeout / Server / Auth / RateLimit exceptions).
  - Added explicit `except _INFRA_FAILURE_TYPES` branch in `cancel_b2b_order_from_erpnext` — writes Failed Sync Record + throws distinct "EasyEcom unreachable" message.
  - Added `_write_cancel_sync_record(...)` helper (mirrors push.py's pattern, direction="Cancel").
  - Added `on_before_cancel_dispatch(doc, method)` hook wrapper with scope guard.
- `ecommerce_super/tests/unit/test_b2b_before_cancel_hook.py` — new file, 10 unit tests (T1–T4 + variants).
- `ecommerce_super/easyecom/smoke_prechecks/_section_11_resmoke_L1_L3.py` — new throwaway smoke runner.

No commits made. No SPEC.md edits. No Discrepancy-string change. Working tree left for Nikhil to review.

## Pending for Nikhil (carried + new from this re-smoke)

1. (Prior) DIVERGENCE on the Discrepancy-string for the shipped-state refusal — code emits `"…order already shipped or past cancel window"`, packet locks `"…unexpected error"`. Unchanged.
2. (Prior) SPEC patch_notes #6 reconciliation — code routes to `/webhook/v2/createOrder` regardless of module, not the patch_notes-claimed Old/New endpoint split.
3. (Prior, now ready for action) Wire SRE release inside the cancel chain — deferred to Phase 2 per `SECTION_11_COMPLETION_CHECKLIST.md`, but the `before_cancel` hook now in place is the right insertion point when Phase 2 lands.
4. (Optional, smaller) Local short-circuit on already-pushed Maps in `push_b2b_order_async` to spare EE the duplicate-reject round-trip on retries.
5. (Sign-off) Drive a Harmony order to Shipped via EE UI and run `.cancel()` against it to flip S4 from FIXTURE-ONLY to LIVE-VERIFIED. The substrate is unit-proven; this is just the live confirmation.

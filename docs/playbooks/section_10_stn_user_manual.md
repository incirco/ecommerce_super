# §10 STN User Manual

End-to-end FDE walkthrough for the four §10 stock-transfer branches
on a live deployment. Live-verified against `mmpl16.frappe.cloud`
2026-06-19 (all four branches passed against Harmony EE).

---

## What §10 does

§10 routes a Delivery Note marked **Is Internal Transfer** to one of
four branches based on the EE-mapping state of the source and target
warehouses:

| Source EE-mapped | Target EE-mapped | Branch | EE endpoint hit |
|---|---|---|---|
| ✓ | ✓ | **STN** | `/webhook/v2/createOrder` (Stock Transfer payload) |
| ✓ | ✗ | **B2B** | `/webhook/v2/createOrder` (B2B Order payload) |
| ✗ | ✓ | **PO** | `/WMS/Cart/CreatePurchaseOrder` |
| ✗ | ✗ | **Inert** | none — pure ERPNext stock move |

A Warehouse is *EE-mapped* when an `EasyEcom Location` with
`workflow_state="Live"` AND `enabled=1` points at it via
`mapped_warehouse`. The Warehouse's
`ecs_ee_location_label` field will show `EE: <location_name>
(#<location_key>)` when this is the case.

---

## One-time prerequisites

Done once per deployment, before the first §10 DN is raised.

### 1. Internal Customer

Required for the §10 routing predicate (an internal-customer DN is
the §10 trigger) AND for the B2B/STN branches (their EE call carries
the Internal Customer's `ee_customer_id`).

1. Open **EasyEcom Company Settings** for your Company.
2. Top-right menu → **§10 STN → Bootstrap Internal Customer**.
3. Dialog pre-fills source/target Company. Click **Bootstrap**.
4. A Customer is created representing your Company (in
   single-Company deployments, source = target = the Company).
5. The bootstrap mirrors GST category, GSTIN, currency from the
   Company; creates Billing + Shipping Addresses linked via Dynamic
   Link; populates placeholder email + mobile.

#### Push the Internal Customer to EE

The B2B and STN branches need the Internal Customer's
`ee_customer_id`. On the new Customer form: top-right → **EasyEcom
→ Push to EasyEcom**. Verify Customer Map status flips to
`Mapped` and `ee_customer_id` is populated.

If the push refuses on `missing email_id` or `missing mobile_no`:
ERPNext's primary-contact sync resets these on save. Either set
them on the **primary Contact** (`Contact form → Phone Nos child
table → is_primary_mobile_no=1`) OR change the
`customer_primary_contact` field to a Contact that already has
those values populated.

### 2. Internal Supplier

Required for the PO branch (the EE call carries the Internal
Supplier's `ee_vendor_id`).

1. Same form (**EasyEcom Company Settings**).
2. Top-right menu → **§10 STN → Bootstrap Internal Supplier**.
3. Dialog pre-fills source/target Company. Click **Bootstrap**.
4. A Supplier is created representing your Company.

#### Push the Internal Supplier to EE

On the new Supplier form: **EasyEcom → Push to EasyEcom**. Verify
Supplier Map status flips to `Mapped` and `ee_vendor_id` is set.

**Important — `payment_terms` is mandatory** on most ERPNext
deployments. The bootstrap helper does not currently set it; if
the insert refuses with `Default Payment Terms Template`
mandatory, manually set `payment_terms` on the Supplier (e.g.,
`Cash` for an Internal Supplier — no real debt) before pushing.

### 3. Goods In Transit Warehouse

§10's same-Company transfers route stock through a Goods-In-Transit
warehouse. The substrate refuses the DN submit with a clear error if
this is missing. One of:

- Set `Company.default_in_transit_warehouse` on the Company form, OR
- Create a Warehouse literally named **"Goods In Transit"** under
  that Company.

### 4. Company GSTIN + gst_category

The §10 push reads `Company.gstin` and `Company.gst_category` for
tax-template selection. Both must be set on the Company form:

- `gstin`: 15-char string (the Company's GST registration).
- `gst_category`: `Registered Regular` for most cases.

A missing `gstin` shows up in the Transfer Map's `flag_reason` as
*"Source Company X has no GSTIN configured. Set Company.gstin"*.

### 5. EE-mapped Warehouses

Each Warehouse you'll use as a source or target on an EE-touching
branch needs an `EasyEcom Location` pointing at it via
`mapped_warehouse`, with workflow_state=Live and enabled=1.

Verify with **`/api/method/.../warehouse_label_sync.backfill_all`**
(System Manager URL — sweeps every Warehouse's
`ecs_ee_location_label` from current Location state) when a known-
mapped Warehouse shows an empty label.

### 6. EE Custom Field rescue (one-time per bench)

Some `create_custom_fields`-based patches silently no-op on fresh
installs (gh#48 race). Symptom: a custom field's row exists in
`tabCustom Field` but the column never materialized on the parent
DocType. Recovery is a single URL:

```
GET /api/method/ecommerce_super.easyecom.install.custom_field_verify.run_audit
```

The response lists every field in the audit registry and reports
`before → after` per row. Re-run safely as needed.

---

## Address linkage — the most common gating issue

The §10 `section10_before_save` hook auto-overrides the DN's
**Billing Address** (`customer_address`), **Shipping Address**
(`shipping_address_name`), **Dispatch Address**
(`dispatch_address_name`), and **Company Address**
(`company_address`) based on the Transfer From / Transfer To
Warehouses' linked Addresses.

ERPNext's stock validation then refuses the submit with:

- *"Billing Address does not belong to the Customer X"*
- *"Dispatch Address Name does not belong to the Company X"*

The fix is **additive Dynamic Link rows on the offending Address**.
None of this changes the Address's data values:

| Address fields ERPNext requires linking to | What to add |
|---|---|
| **Buyer side** (Billing / Shipping) | Add `Customer: <Internal Customer>` link. Confirm the Address already has `Warehouse: <target>` link. |
| **Seller side** (Dispatch / Company Address) | Add `Company: <Company>` link. Confirm the Address already has `Warehouse: <source>` link. |

Three quick UI paths to add a Dynamic Link to an Address:

1. Open the Address record → scroll to the **Reference** child
   table → add a row with `Link Document Type = Customer/Company`,
   `Link Name = <name>` → save.
2. Open the Customer/Company → if it has an **Addresses** dashboard
   widget, use "Link Existing Address".
3. Via API (Administrator):
   ```
   PUT /api/resource/Address/<address-name>
       { ... existing fields ..., "links": [ ... existing rows ..., {"link_doctype":"Customer","link_name":"R251844"} ] }
   ```

---

## The DN flow itself

Same form for all four branches. The branch chip on the form shows
which branch will fire as soon as both warehouses are picked.

1. **Stock → Delivery Note → New** (or Sales Order → Make → Delivery
   Note).
2. **Customer** field → pick your Internal Customer.
   - The **Is Internal Transfer** checkbox auto-ticks via
     `fetch_from = customer.is_internal_customer`.
3. **Transfer From Warehouse**
   (`ecs_section10_transfer_from_warehouse`) → pick source.
   - Autocomplete shows `EE: <Location>` label on EE-mapped
     warehouses; non-mapped show no label.
4. **Transfer To Warehouse**
   (`ecs_section10_transfer_to_warehouse`) → pick target.
   - As soon as both are set, a routing **branch chip** appears at
     the top of the form indicating **STN / B2B / PO / Inert**.
5. **Add items** in the items table. For non-Inert branches, items
   need an `EasyEcom Item Map` row (status `Mapped` or
   `Created-Flagged`) — otherwise the push flags with
   *"references Items without an EasyEcom Item Map"*.
6. **Save** → **Submit**.

### What our code does on submit

1. `validate_pre_submit` enforces both warehouses set + different,
   GIT warehouse resolvable.
2. `section10_before_save` auto-sets the four addresses + per-line
   warehouse routing + tax template selection (out-of-state
   IGST template if the two warehouses' GSTINs differ).
3. `enqueue_on_dn_submit` writes an `EasyEcom Transfer Map` row and
   enqueues an `EasyEcom Queue Job` of type **Transfer Push**.
4. Worker fires the branch-appropriate EE call:
   - **PO**: `/WMS/Cart/CreatePurchaseOrder` → response captures
     `ee_po_id`.
   - **B2B/STN**: `/webhook/v2/createOrder` → response captures
     `ee_order_id`, `ee_suborder_id`, `ee_invoice_id`.
   - **Inert**: no EE call; no Transfer Map row created.

### Manual recovery — re-push a Drift Transfer Map

If a Transfer Map row is in **status=Drift** with a flag_reason
populated, fix the cause (typically a precondition: missing
ee_customer_id / ee_vendor_id / GSTIN), then trigger:

```
POST /api/method/ecommerce_super.easyecom.flows.transfer_push.push_all_pending_transfers
     ?inline=1
```

The response lists every Drift / pending Transfer Map and its
re-push outcome (`b2b_pushed`, `po_pushed`, `stn_pushed`,
`drift`, `skipped`).

There's also a **Retry Push** button on the DN form for the same
flow.

---

## Verification per branch

Once submitted, confirm via:

### `EasyEcom Transfer Map` (for non-Inert branches)

| Field | Expected after success |
|---|---|
| `status` | `EE-Pushed` (or `SI-Pending` for same-GSTIN STN with an auto-drafted SI) |
| `ee_doctype` | `PO` / `B2B` / `STN` |
| `ee_po_id` | non-zero for PO branch |
| `ee_order_id` | populated for B2B/STN |
| `ee_invoice_id` | populated for STN if EE side returned it |
| `flag_reason` | null |

### `EasyEcom API Call` list

A 200 OK row with the branch's endpoint:
- PO: `/WMS/Cart/CreatePurchaseOrder`
- B2B: `/webhook/v2/createOrder`
- STN: `/webhook/v2/createOrder` (payload differs — `is_market_shipped` vs B2B's `orderType="businessorder"`)
- Inert: no row generated.

### `Trace Outbound Push (§10)` button on the DN

For submitted DNs with `is_internal_customer=1`, the EasyEcom menu
on the DN form has a **Trace Outbound Push (§10)** button. Read-only.
Walks every §10 gate and shows pass/fail per stage with the names
of every downstream artifact (Transfer Map, Sync Records, Queue Jobs,
API Calls). The fastest way to diagnose a stuck push.

---

## Common errors and recovery

### "Billing Address does not belong to the Customer X"

Address auto-set by `section10_before_save` from the **target
warehouse**'s linked Address isn't also linked to the Customer.
Add `Customer: X` Dynamic Link row to that Address.

### "Dispatch Address Name does not belong to the Company X"

Address auto-set from the **source warehouse**'s linked Address
isn't linked to the Company. Add `Company: X` Dynamic Link row.

### "§10 Internal Supplier missing for source Company X → target Company Y"

The PO branch's `_find_internal_supplier` couldn't find a Supplier
with `is_internal_supplier=1` AND `represents_company=<source>`.
Run the Bootstrap Internal Supplier action (see prereq #2).

### "Source Company X has no GSTIN configured"

Set `Company.gstin` (see prereq #4).

### "PO branch requires an EE-side vendor — V12345 has no Supplier Map ee_vendor_id captured"

The Internal Supplier exists in ERPNext but was never pushed to
EE. Hit the **Push to EasyEcom** button on the Supplier form.

If the push fails with EE's `Vendor code already exists!` (HTTP
400 in `response_payload`) — that vendor code is already taken
EE-side, possibly by a soft-deleted vendor that `/wms/V2/getVendors`
doesn't surface. Recovery:
- Disable the Supplier's `is_internal_supplier` flag,
- Bootstrap a fresh Internal Supplier (gets a new auto-naming
  series docname → new vendorCode → no collision),
- Push the new one.

### "DN line(s) reference Items without an EasyEcom Item Map: 'XXX'"

The item needs an `EasyEcom Item Map` row in `Mapped` or
`Created-Flagged` status. Push the item to EE via the **Push to
EasyEcom** button on the Item form (§8d Stage 6).

### Stock errors (negative stock, batch/serial mandatory)

These are pure ERPNext stock validation, not §10. Use an item
that has positive stock at the source warehouse and is not
batch/serial-tracked, or set up the appropriate Batch No / Serial
No data.

### TimestampMismatch on submit

Frappe's optimistic-concurrency check tripped because
`section10_before_save` modified the doc between client load and
server save. Refresh the form and re-submit.

---

## Operator URLs (reference)

System Manager / Administrator only:

```
GET  /api/method/ecommerce_super.easyecom.install.custom_field_verify.run_audit
GET  /api/method/ecommerce_super.easyecom.flows.warehouse_label_sync.backfill_all
POST /api/method/ecommerce_super.easyecom.flows.transfer_push.push_all_pending_transfers
POST /api/method/ecommerce_super.easyecom.api.customer_push.push_one_customer_now
POST /api/method/ecommerce_super.easyecom.api.supplier_push.push_one_supplier_now
```

All five are idempotent. Hit any of them from a browser logged in
as Administrator; the response is JSON.

---

## Live verification example (mmpl16, 2026-06-19)

| Branch | DN | Result |
|---|---|---|
| **PO** | DL-260552 | `EE-Pushed`, `ee_po_id=2018477`, `/WMS/Cart/CreatePurchaseOrder` 200 OK |
| **B2B** | DL-260550, DL-260551 | `EE-Pushed`, `ee_doctype=B2B`, `/webhook/v2/createOrder` 200 OK each |
| **STN** | DL-260559 | `EE-Pushed`, `ee_doctype=STN`, `/webhook/v2/createOrder` 200 OK |
| **Inert** | DL-260558 | No Transfer Map row created (correct), no EE call. |

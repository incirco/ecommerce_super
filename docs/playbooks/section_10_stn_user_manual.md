# §10 Stock Transfer — User Manual

A step-by-step guide for ERPNext users who need to move inventory
between warehouses. Written from the operator's point of view —
what to click, what to type, what to expect. Live-verified on
`mmpl16.frappe.cloud` 2026-06-19 (see companion document
`section_10_stn_mmpl_live_reference.md` for the actual entries
this manual was tested against).

---

## What this is about

You want to move stock from one warehouse to another. Sometimes
both warehouses are inside EasyEcom (an EE-managed location). Other
times only one is. Sometimes neither.

The integration handles all four combinations automatically through
**one form**: the standard ERPNext **Delivery Note**, with a few
extra header fields. You don't pick an "STN type" — the integration
decides based on which warehouses you choose.

> **In one sentence**: tick "Is Internal Transfer", pick the two
> warehouses, save and submit. The right thing happens.

---

## The four use cases — pick the one that matches your move

A warehouse is **EasyEcom-managed** if its `EE Location Label`
field shows something like `EE: Mumbai WH (#en71352...)`. If the
field is empty, it's a **plain ERPNext warehouse** (EasyEcom
doesn't know about it).

Read the table from the perspective of "what kind of move am I
doing":

### Case 1 — Move stock between two EasyEcom warehouses

**Example.** Shift 5 boxes from your *EE Mumbai Warehouse* to your
*EE Bengaluru Warehouse*. Both warehouses are inside the EasyEcom
system.

| Source warehouse | Target warehouse |
|---|---|
| EasyEcom-managed ✓ | EasyEcom-managed ✓ |

**What the integration does for you.** Tells EasyEcom to record a
stock transfer between its two locations. EasyEcom moves the
inventory on its side; ERPNext routes the stock through your
Goods-In-Transit warehouse on this side.

**You'll see on Branch Chip**: `STN` (Stock Transfer Note).

---

### Case 2 — Move stock OUT of EasyEcom into your own warehouse

**Example.** Take 1 saree out of your *EE Mumbai Warehouse* and put
it on display at your *Paota Showroom* (your own warehouse, not
in EasyEcom).

| Source warehouse | Target warehouse |
|---|---|
| EasyEcom-managed ✓ | Plain ERPNext ✗ |

**What the integration does for you.** Tells EasyEcom: *"Sell this
item as a B2B order to my internal customer."* EasyEcom removes the
item from its inventory and records the sale to your internal
customer (which represents you, the recipient). On your side,
ERPNext receives the stock at the target warehouse.

**You'll see on Branch Chip**: `B2B`.

---

### Case 3 — Move stock INTO EasyEcom from your own warehouse

**Example.** Send 10 new pieces from your *Workshop* (your own
warehouse) to your *EE Mumbai Warehouse* for fulfilment.

| Source warehouse | Target warehouse |
|---|---|
| Plain ERPNext ✗ | EasyEcom-managed ✓ |

**What the integration does for you.** Tells EasyEcom: *"Receive
this stock from my internal supplier."* EasyEcom creates a vendor
Purchase Order on its side (you, in effect, "sell" stock to your
own EE inventory). When EasyEcom confirms receipt, your ERPNext
gets a Purchase Receipt automatically.

**You'll see on Branch Chip**: `PO`.

---

### Case 4 — Move stock between two of your own warehouses

**Example.** Shift stock from *Paota Showroom* to *Workshop*.
Neither warehouse is in EasyEcom.

| Source warehouse | Target warehouse |
|---|---|
| Plain ERPNext ✗ | Plain ERPNext ✗ |

**What the integration does for you.** Nothing on the EasyEcom
side — there's nothing for EasyEcom to know about. The integration
still helps with the **address and GSTIN setup** so India
Compliance computes the right CGST/SGST/IGST split, but it makes
no EasyEcom call. Inventory moves between your warehouses as a
normal internal transfer.

**You'll see on Branch Chip**: `Inert`.

---

## Before you raise your first §10 DN — one-time prerequisites

Set these up once per deployment. After that, every DN just works.
Your finance/admin person typically does these during onboarding.

### Step 1. The Internal Customer

This Customer record represents *your own company* as a buyer.
When you move stock OUT of EasyEcom (Cases 1, 2), EasyEcom needs a
"buyer" to assign the order to — that's the Internal Customer.

**How to create**:

1. Open **EasyEcom Company Settings** form for your Company.
2. Top-right menu → **§10 STN → Bootstrap Internal Customer**.
3. Click **Bootstrap** in the dialog (defaults are right for
   single-Company deployments).
4. A Customer named `Internal - <YourCompany>` is created with
   GST/currency/addresses pre-filled.

**Push it to EasyEcom** so EasyEcom knows about this customer:

1. Open the new Customer → top-right → **EasyEcom → Push to EasyEcom**.
2. The Customer Map should change to status `Mapped` with a real
   `ee_customer_id`.

If push complains about missing email or mobile, see the
**Troubleshooting** section.

### Step 2. The Internal Supplier

This Supplier represents *your own company* as a vendor. When you
move stock INTO EasyEcom (Case 3), EasyEcom needs a "vendor" to
attribute the inbound PO to — that's the Internal Supplier.

**How to create**:

1. Same form (**EasyEcom Company Settings**).
2. Top-right menu → **§10 STN → Bootstrap Internal Supplier**.
3. Click **Bootstrap**.
4. A Supplier named `Internal Supplier - <YourCompany>` is created.

**Push it to EasyEcom**:

1. Open the new Supplier → top-right → **EasyEcom → Push to EasyEcom**.
2. The Supplier Map should change to status `Mapped` with a real
   `ee_vendor_id`.

### Step 3. The Goods-In-Transit (GIT) warehouse

For same-Company moves, ERPNext routes stock through a "Goods In
Transit" warehouse — like a virtual holding bay between the source
and the target.

One of two ways:
- Open **Company / <YourCompany>** → set
  **Default In Transit Warehouse** → save. (Recommended.)
- OR create a Warehouse literally named **"Goods In Transit"**
  under your Company.

If you skip this, the DN submit will fail with a clear message.

### Step 4. Your Company's GSTIN

§10 needs to know your GST number to compute the right tax split.

Open **Company / <YourCompany>** → set:
- **GSTIN**: your 15-character GST number
- **GST Category**: usually `Registered Regular`

### Step 5. EasyEcom Locations are Live

Each warehouse you'll use as Case 1/2/3 (anything involving EE)
needs an EasyEcom Location pointing at it. Verify this from the
EasyEcom Location list:

- Open `/app/easyecom-location`.
- Filter by **Workflow State = Live** + **Enabled = ✓**.
- Each Live + Enabled Location should have a Warehouse in the
  `mapped_warehouse` column.

If a warehouse shows "Live" in EasyEcom but its **EE Location
Label** field on the Warehouse form is empty, ask your admin to
run the `backfill_all` URL (one click).

### Step 6. Custom field rescue (one-time per bench)

On fresh installations, some integration fields can silently
fail to materialize. If you can't tick "Is Internal Transfer" or
the "Transfer From Warehouse" field is missing, ask your admin to
hit:

```
GET /api/method/ecommerce_super.easyecom.install.custom_field_verify.run_audit
```

(Logged in as Administrator.) This is safe to re-run.

---

## Raising a §10 Stock Transfer DN

Once the prerequisites are done, every transfer follows the same
six steps regardless of which case you're in.

### Step 1. Stock → Delivery Note → New

### Step 2. Set the Customer

In the **Customer** field, type and pick your **Internal Customer**
(e.g. `Internal - Modern Marwar Private Limited`). The
**Is Internal Transfer** checkbox ticks automatically.

### Step 3. Pick Transfer From Warehouse

This is the **source** — the warehouse stock is *leaving from*.

When you click the field, the autocomplete shows EE-mapped
warehouses first with an `EE: <Location>` label. Plain warehouses
have no label.

### Step 4. Pick Transfer To Warehouse

This is the **target** — the warehouse stock is *going to*.

> **As soon as both warehouses are picked, a routing chip appears**
> at the top of the form showing **STN / B2B / PO / Inert**. That's
> your confirmation of which case you're in. Verify it matches
> your intention before continuing.

### Step 5. Add items

Add the items, quantities, and rates as usual.

For Cases 1, 2, 3 (anything that hits EasyEcom): each item must
already exist in EasyEcom (its EE Item Map status should be
`Mapped` or `Created-Flagged`). If you pick an unmapped item, the
**save itself will be blocked** (gh#93) with a popup naming the
unsynced item(s) and pointing you at §8d Item Push. Sync the item
first, then save.

For Case 4 (Inert): any ERPNext item works (the §10 guard doesn't
fire on Inert because `is_internal_customer = 0`).

### Step 6. Save → Submit

That's it. On submit:
- The integration auto-fills the four address fields (Billing,
  Shipping, Dispatch, Company Address) from the warehouse-linked
  addresses.
- It records a row in the **EasyEcom Transfer Map** list.
- It queues the EasyEcom call (if applicable for your case).
- The worker fires the call within ~10 seconds.

---

## What to expect after submit

### Cases 1, 2, 3 (EE-touching)

Within ~10 seconds of submit:

1. **A row appears in EasyEcom Transfer Map** with `delivery_note`
   matching your DN.
2. **Its `status` flips to `EE-Pushed`** (you may briefly see
   `Pending` or `Drift`).
3. **The `EasyEcom API Call` list** has a new entry with status
   `Success` and a 200 response code.
4. **The Transfer Map records the EE-side identifiers** (`ee_po_id`
   for Case 3, `ee_order_id` for Cases 1 and 2).

### Case 4 (Inert)

- **No row in EasyEcom Transfer Map** (correct — there's nothing
  for EasyEcom to know about).
- **No new EasyEcom API Call** entry.
- The DN itself behaves as a normal ERPNext internal transfer.

### Inbound side for Cases 1 and 3

When EasyEcom processes the goods on its side and confirms
receipt, the integration polls and automatically creates a
**Purchase Receipt** on your ERPNext at the target Company under
the Internal Supplier. You don't have to do anything manually.

---

## Verifying your transfer worked

Three places to look:

### On the DN form
- The **Dashboard** section shows a link to the Transfer Map row.
- The top-right menu has a **Trace Outbound Push (§10)** button.
  Click it for a read-only "pass/fail per gate" report — the
  fastest way to find what (if anything) went wrong.

### EasyEcom Transfer Map list
- Open `/app/easyecom-transfer-map`.
- Find the row for your DN.
- Healthy: `status = EE-Pushed`, `flag_reason` empty, EE IDs populated.

### EasyEcom API Call list
- Open `/app/easyecom-api-call?modified=<today>`.
- The newest row (sorted by Modified DESC) should be your push.
- The endpoint will be one of:
  - `/WMS/Cart/CreatePurchaseOrder` (PO branch)
  - `/webhook/v2/createOrder` (B2B or STN — both branches share
    this endpoint but with different payload shapes)
- Healthy: `response_status_code = 200`, `status = Success`.

---

## Troubleshooting common errors

### "Billing Address does not belong to the Customer X"

**What's happening.** ERPNext is validating that the Billing
Address belongs to the Customer. The §10 integration auto-fills
the Billing Address from the **target warehouse**'s linked
Address — but that Address isn't linked to your Internal Customer.

**Fix in the UI**:
1. Open the Address that triggered the error.
2. Scroll to the **Reference** child table (it lists Link Document
   Type + Link Name rows).
3. Add a row: `Link Document Type = Customer`,
   `Link Name = <your Internal Customer's docname>`. Save.

This adds a permission link; no Address data changes. Re-submit
the DN.

### "Dispatch Address Name does not belong to the Company X"

**What's happening.** Same idea, seller side. The Dispatch Address
came from the **source warehouse**'s linked Address but isn't
linked to your Company.

**Fix in the UI**:
1. Open the Address that triggered the error.
2. Add a Reference row:
   `Link Document Type = Company`,
   `Link Name = <your Company name>`. Save.

### "Source/Target Company X has no GSTIN configured"

Set `Company.gstin` on the Company form (see Prerequisite Step 4).

### "Internal Supplier missing for source Company X → target Y"

You skipped Prerequisite Step 2, or the Supplier was created but
never pushed to EE. Do both.

### "PO branch requires an EE-side vendor — V12345 has no Supplier Map ee_vendor_id captured"

The Internal Supplier exists in ERPNext but isn't on the EasyEcom
side yet. Open the Supplier → **EasyEcom → Push to EasyEcom**.

If the push fails with EE's *"Vendor code already exists!"*: a
vendor with that code is reserved on EasyEcom's side (often a
soft-deleted vendor that EE's vendor list doesn't show). Two ways:
- Ask EE support to clear the reservation, OR
- Create a fresh Internal Supplier with a different name (gets
  a new auto-generated code, no collision).

### "DN line(s) reference Item(s) not yet synced to EasyEcom"

The items on the DN aren't yet known to EasyEcom. Push each item
to EE first via the **Push to EasyEcom** button on the Item form,
or batch via **Push All Pending Items** on the EasyEcom Account
form. Then re-save the DN.

gh#93: as of `a23e0d9`, this is a **pre-submit block** — the save
is refused outright with the unsynced item_code(s) named in the
popup. The older Drift-on-Failed-Sync-Record behavior only fires
for pre-`a23e0d9` DNs that already submitted (and for the
Internal-pair / GST / GIT-warehouse preconditions which still
land on Drift if they miss after the Item Map check passes).

### "Negative stock" or "Serial No / Batch No are mandatory"

These are pure ERPNext stock-level errors, not §10. Make sure you
have stock at the source warehouse and that your items don't
require Batch/Serial tracking (or pick an item that doesn't).

### "TimestampMismatchError" on submit

The form's data was modified by the integration between when you
loaded it and when you clicked Submit. Just reload the form and
click Submit again.

### "Push failed, nothing happened" — Retry Push doesn't do anything

The §10 push refused for a precondition (Internal Customer not
mapped, Internal Supplier missing, GSTIN missing). The Transfer
Map row's `flag_reason` field tells you exactly which. Open the
Transfer Map row, read `flag_reason`, fix the cause, then either
press the **Retry Push** button on the DN form or ask your admin
to hit:

```
POST /api/method/ecommerce_super.easyecom.flows.transfer_push.push_all_pending_transfers?inline=1
```

---

## Quick visual reference

```
       (source warehouse)                (target warehouse)
              │                                 │
              ▼                                 ▼
   ┌──────────────────┐               ┌──────────────────┐
   │ EE-managed?      │               │ EE-managed?      │
   └────┬─────────┬───┘               └────┬─────────┬───┘
        │ Yes     │ No                     │ Yes     │ No
        │         │                        │         │
        └─────────┼────────────────────────┘         │
                  │                                  │
        ┌─────────┼────────┬─────────────────┬───────┘
        │         │        │                 │
       Yes/Yes  Yes/No   No/Yes            No/No
        │         │        │                 │
        ▼         ▼        ▼                 ▼
      STN       B2B       PO              Inert
   (transfer  (sell out  (PO into       (no EE call,
    inside     of EE)     EE)             pure ERPNext)
    EE)
```

---

## When in doubt

- The Branch Chip on the DN form tells you which case you're in
  before you submit. Trust it.
- The **Trace Outbound Push** button on a submitted DN walks every
  check and shows what passed and what failed.
- The Transfer Map row's `flag_reason` is always the most precise
  message about what's blocking the push.

For deep diagnostics, share the DN number, the Transfer Map row
name, and any visible flag_reason with your admin — that's enough
to track the exact request that went to EasyEcom (or didn't).

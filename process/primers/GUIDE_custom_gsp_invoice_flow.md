# Custom GSP Invoice Flow — Complete Guide

End-to-end reference for the §11.5.1 Mode 1 Custom GSP integration. Covers what EasyEcom's Custom GSP feature demands, what ecommerce_super exposes in response, and how to wire the two together for a live deployment.

> **Audience**: FDE / DevOps configuring a client's EE ↔ ERPNext invoice flow. Read this end-to-end before configuring the first deployment. Skim sections 1-3 first to understand the architecture, then use sections 4-6 as a checklist when configuring.

---

## Table of contents

1. [What is Custom GSP and why does it exist](#1-what-is-custom-gsp-and-why-does-it-exist)
2. [The full lifecycle in one diagram](#2-the-full-lifecycle-in-one-diagram)
3. [EE's contract — the three endpoints EE expects](#3-ees-contract--the-three-endpoints-ee-expects)
4. [What we built — the ERPNext side](#4-what-we-built--the-erpnext-side)
5. [Setup checklist (per EE Account, ~30 min)](#5-setup-checklist-per-ee-account-30-min)
6. [Live smoke test (first invoice end-to-end)](#6-live-smoke-test-first-invoice-end-to-end)
7. [Failure modes — what each error means](#7-failure-modes--what-each-error-means)
8. [Operational concerns](#8-operational-concerns)
9. [Mode 1 vs Mode 2 — picking one per Account](#9-mode-1-vs-mode-2--picking-one-per-account)
10. [Code map + references](#10-code-map--references)

---

## 1. What is Custom GSP and why does it exist

### 1.1 The problem EE solves

GSTIN-bearing B2B invoices in India must be reported to NIC's IRP (Invoice Registration Portal) within hours of generation. NIC returns an IRN (Invoice Reference Number) which must appear on the printed invoice. This is **mandatory** for businesses above the GST e-invoicing turnover threshold.

EE's default behaviour: it talks to NIC IRP itself, via its own GSP integration. Click "Generate Invoice" on an EE order, EE mints the IRN, prints, attaches PDF. Done.

### 1.2 The problem Custom GSP solves

Many clients want **ERPNext** to be the e-invoice authority:
- Their accountants work in ERPNext, not in EE
- Their NIC IRP credentials live on the ERPNext side (via the India Compliance app)
- They want a single source of truth for invoices (and the matching Sales Invoice records in ERPNext for GL impact)

EE supports this via a "Custom GSP" feature on the B2B Account. When enabled:
- EE no longer mints IRN itself
- Instead, EE calls a **configured Custom GSP URL** every time someone clicks "Generate Invoice"
- The Custom GSP is expected to expose three specific endpoints (token + invoice + e-way bill)
- The Custom GSP returns the minted IRN/QR/PDF; EE just displays

**We are that Custom GSP.** The `ecommerce_super` integration exposes the three required endpoints; we resolve the EE order to an ERPNext Sales Invoice, mint the IRN via India Compliance, return EE's expected response shape.

### 1.3 What this gives the client

- Every EE invoice generation produces a real ERPNext Sales Invoice (submitted, with `irn` / `ack_no` / `ack_dt` populated by India Compliance)
- The PDF EE displays comes from the ERPNext print format (consistent branding)
- NIC IRP credentials live in one place (India Compliance settings on the ERPNext side)
- Accountants see the IRN-stamped SI in ERPNext immediately; no second system to reconcile
- GL impact happens via ERPNext's standard SI submit flow

---

## 2. The full lifecycle in one diagram

```
                   ┌─────────────────────────────────────────────────┐
                   │                                                 │
                   │  EE-side FDE opens the order, clicks            │
                   │  "Generate Invoice" button                      │
                   │                                                 │
                   └────────────────────────┬────────────────────────┘
                                            │
                                            ▼
                   ┌─────────────────────────────────────────────────┐
                   │                                                 │
                   │  EE's backend checks: Custom GSP enabled        │
                   │  for this Account → call configured URL         │
                   │                                                 │
                   └────────────────────────┬────────────────────────┘
                                            │
                                            ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │  STEP 1: EE → POST <our>/gettoken                                │
        │           Headers: Authorization: Basic <base64(any:secret)>    │
        │                                                                 │
        │  We:                                                            │
        │    1. Decode Basic header                                       │
        │    2. Match password against any enabled EE Account's          │
        │       gsp_basic_auth_secret                                     │
        │    3. Mint Bearer token (64-char hex, 1hr TTL)                 │
        │    4. Persist SHA-256 hash (plaintext never stored)            │
        │    5. Return: { status: 200, access_token, expires_in: 3600 } │
        └────────────────────────────────────┬────────────────────────────┘
                                             │
                                             ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │  STEP 2: EE → POST <our>/einvoice/update                        │
        │           Headers: Authorization: Bearer <token from step 1>    │
        │           Body: { orders: [<full EE order JSON>] }              │
        │                                                                 │
        │  We:                                                            │
        │    1. Validate Bearer (hash → lookup → expiry check)            │
        │    2. find_or_create_si_for_gsp:                                │
        │       a. Look up SI by ecs_easyecom_invoice_id (idempotency)    │
        │       b. Else look up via B2B Order Map.sales_invoice           │
        │       c. Else create SI from EE payload (invoice_mirror)        │
        │    3. Submit SI if Draft (IC requires submitted)                │
        │    4. mint_irn_for_si:                                          │
        │       - If si.irn populated → return cached (idempotent)        │
        │       - Else call IC generate_e_invoice → NIC IRP → writes     │
        │         irn/ack_no/ack_dt/signed_qr_code on SI                 │
        │    5. Return: { data.invoice_details: { irn, ack_number,        │
        │                ack_date, invoice_pdf URL, irn_qr } }           │
        └────────────────────────────────────┬────────────────────────────┘
                                             │
                                             ▼
        ┌─────────────────────────────────────────────────────────────────┐
        │  STEP 3 (optional): EE → POST <our>/ewaybill/update             │
        │           Headers: Authorization: Bearer <same token>           │
        │           Body: { orders: { order + transport_* fields } }      │
        │                                                                 │
        │  We:                                                            │
        │    1. Validate Bearer                                           │
        │    2. find_si_by_invoice_id (SI MUST already exist from step 2)│
        │    3. mint_eway_for_si: IC generate_e_waybill → NIC EWB →     │
        │       writes ewaybill on SI                                    │
        │    4. Return: { data.invoice_details: { eway_bill_number,       │
        │                eway_bill_date, eway_bill_pdf URL, vehicle...   │
        │                transporter... } }                              │
        └────────────────────────────────────┬────────────────────────────┘
                                             │
                                             ▼
                   ┌─────────────────────────────────────────────────┐
                   │  EE displays the IRN + PDF + e-way bill         │
                   │  ERP User sees the new SI in ERPNext with       │
                   │  IRN/ack/QR populated by India Compliance       │
                   └─────────────────────────────────────────────────┘
```

The whole round-trip happens in 1-3 seconds end-to-end (most time is NIC IRP latency).

---

## 3. EE's contract — the three endpoints EE expects

### 3.1 /gettoken — Auth bootstrap

```
POST  <base>/gettoken
Headers:
  Authorization: Basic <base64-encoded user:password>
```

**Sample request**:
```bash
curl -X POST 'https://<bench>/api/method/ecommerce_super.easyecom.api.gsp.gettoken' \
  -H 'Authorization: Basic YWRtaW46cGFzc3dvcmQ='
```

**Sample response (success)**:
```json
{
  "status": 200,
  "access_token": "3acd596d247e78c61856ca20ce77374a6fc4658f89c4bfa7d7b2cbf72551e0e2",
  "token_type": "Bearer",
  "expires_in": 3600
}
```

**Sample response (failure)**:
```json
{ "status": 401, "message": "No enabled EE Account matched the provided Basic auth secret." }
```

### 3.2 /einvoice/update — Invoice generation

```
POST  <base>/einvoice/update
Headers:
  Authorization: Bearer <token from /gettoken>
  Content-Type: application/json
Body:
  { "orders": [ <full EE order object> ] }
```

The `orders[0]` element is the same shape as a single row from EE's `getOrderDetails` response — header fields + `order_items[]` with breakup_types, customer block, addresses, transport hints.

**Sample request** (truncated for clarity — actual payload is ~3KB):
```json
{
  "orders": [
    {
      "invoice_id": 583870198,
      "order_id": 492804768,
      "reference_code": "KIT/2455571773746052204",
      "company_name": "Biotique - Test Account",
      "warehouse_id": 245557,
      "order_type": "B2C",
      "order_date": "2026-03-17 16:44:52",
      "invoice_date": "2026-03-17 00:00:00",
      "total_amount": "10000.0000",
      "total_tax": "1525.4240",
      "buyer_gst": "",
      "customer_name": "Biotique - Test Account",
      "billing_name": "...",
      "billing_address_1": "...",
      "city": "Unnao", "state": "Uttar Pradesh", "pin_code": "209801",
      "breakup_types": {
        "Item_Amount_Excluding_Tax": 8474.576,
        "Item_Amount_CGST": 762.712,
        "Item_Amount_SGST": 762.712,
        "Shipping_Excluding_Tax": 84.746,
        "Shipping_CGST": 7.627,
        "Shipping_SGST": 7.627
      },
      "order_items": [ ... ]
    }
  ]
}
```

**Sample response (success)**:
```json
{
  "status": 200,
  "message": "Invoice fetched successfully",
  "data": {
    "invoice_details": {
      "invoice_id": "583870198",
      "erp_invoice_num": "ACC-SINV-2026-00042",
      "irn": "a7f4c5b9e8d6a2f4e1c8b3d9a7f4c5b853b9e8d6a2f4",
      "ack_number": "162310987654321",
      "ack_date": "2026-03-25T11:46:31.675Z",
      "invoice_pdf": "https://<bench>/api/method/frappe.utils.print_format.download_pdf?doctype=Sales+Invoice&name=ACC-SINV-2026-00042&format=Standard&no_letterhead=0",
      "irn_qr": "scdvfbgnmfghjk",
      "invoice_base64": ""
    }
  }
}
```

**Failure responses**:
- `HTTP 401` → token invalid / expired
- `HTTP 422` → payload validation (missing reference_code, no matching Map, missing Customer Map / Item Map / HSN, India Compliance validation error)
- `HTTP 502` → NIC IRP infrastructure error (retry-able)

### 3.3 /ewaybill/update — E-way bill generation

```
POST  <base>/ewaybill/update
Headers:
  Authorization: Bearer <same token>
  Content-Type: application/json
Body:
  { "orders": { <single EE order object with transport fields> } }
```

Note: `orders` is a single object here, not an array. We accept both shapes defensively.

The EE order object carries `transport_mode`, `vehicle_number`, `vehicle_type`, `transporter_gst`, `transporter_name`, `transport_document_number` for e-way bill generation.

**Sample response**:
```json
{
  "status": 200,
  "message": "E-Way Bill fetched successfully",
  "data": {
    "invoice_details": {
      "invoice_id": "275333182",
      "erp_invoice_num": "ACC-SINV-2026-00042",
      "eway_bill_number": "781234567890",
      "eway_bill_date": "2026-03-25T11:47:34.071Z",
      "eway_bill_pdf": "https://<bench>/api/method/frappe.utils.print_format.download_pdf?doctype=Sales+Invoice&name=ACC-SINV-2026-00042&format=e-Waybill&no_letterhead=0",
      "transport_mode": "Road",
      "vehicle_number": "MH12AB1234",
      "vehicle_type": "Regular",
      "transporter_gst": "27TRANS1234Z5",
      "transporter_name": "Safe Logistics",
      "eway_bill_base64": ""
    }
  }
}
```

---

## 4. What we built — the ERPNext side

### 4.1 The three URLs EE configures

| Endpoint | Production URL pattern |
|---|---|
| Token | `https://<your-bench>/api/method/ecommerce_super.easyecom.api.gsp.gettoken` |
| E-Invoice | `https://<your-bench>/api/method/ecommerce_super.easyecom.api.gsp.einvoice_update` |
| E-Way Bill | `https://<your-bench>/api/method/ecommerce_super.easyecom.api.gsp.ewaybill_update` |

**For mmpl16 specifically**:
```
https://mmpl16.frappe.cloud/api/method/ecommerce_super.easyecom.api.gsp.gettoken
https://mmpl16.frappe.cloud/api/method/ecommerce_super.easyecom.api.gsp.einvoice_update
https://mmpl16.frappe.cloud/api/method/ecommerce_super.easyecom.api.gsp.ewaybill_update
```

If EE's Custom GSP UI accepts only a **base URL** (and appends fixed paths like `/gettoken`, `/einvoice/update`, `/ewaybill/update` itself), we need URL rewriting on the ERPNext side. Tell us and we'll add the rewrite layer (~30 min).

### 4.2 Auth model

| Layer | Mechanism | Where the secret lives |
|---|---|---|
| HTTP Basic on /gettoken | `Authorization: Basic <base64(any-user:secret)>` | `EasyEcom Account.gsp_basic_auth_secret` (encrypted Password field) |
| HTTP Bearer on /einvoice + /ewaybill | `Authorization: Bearer <plaintext token>` | `tabEasyEcom GSP Token.token_hash` (SHA-256 of plaintext) |

- The Basic auth **username portion is ignored** — only the password matters. EE-side config can use any username.
- The Basic auth password matches against ALL enabled EE Accounts' secrets (single-account benches see one match; multi-account benches use unique secrets per account).
- Bearer tokens are 64-char hex (32 random bytes). 1 hour TTL per EE's spec.
- Plaintext tokens are **never persisted** after /gettoken returns them — only the SHA-256 hash is stored.
- Constant-time secret comparison via `secrets.compare_digest` to avoid timing leaks.
- Expired tokens auto-deleted by a daily scheduler (after a 7-day audit retention window).

### 4.3 Idempotency model

Critical because re-minting IRN on NIC IRP creates duplicate IRNs that **cannot be deleted** — the only remediation is calling NIC support.

Three layers of protection:

1. **`find_or_create_si_for_gsp`** — looks up SI by `ecs_easyecom_invoice_id` (EE's natural key). Re-hits return the existing SI; never creates duplicate.
2. **`mint_irn_for_si`** — checks `si.irn` populated → returns cached IRN bundle. Never calls `generate_e_invoice` if IRN already present.
3. **India Compliance internal** — `generate_e_invoice` raises `AlreadyGeneratedError` if SI has IRN; our handler catches and re-reads SI to return cached.

Toggles (§4.4) **never disable idempotency** — they only gate whether NIC IRP / NIC EWB is called on the *first* fresh invoice. A cached IRN / EWB is always returned, regardless of toggle state at read time.

### 4.4 New DocTypes / Custom Fields shipped

| DocType / Field | Purpose |
|---|---|
| `EasyEcom GSP Token` (NEW DocType) | Bearer token storage (hash only). Read-only. System Manager perm. |
| `EasyEcom Account.gsp_basic_auth_secret` (NEW Custom Field) | Password field, encrypted. The shared secret EE configures. |
| `EasyEcom Account.gsp_mint_einvoice` (NEW Check, default ON) | When OFF, `/einvoice/update` skips NIC IRP mint — SI still created/submitted, response carries empty IRN fields but populated PDF URL. |
| `EasyEcom Account.gsp_mint_ewaybill` (NEW Check, default ON) | When OFF, `/ewaybill/update` skips NIC EWB mint — response has empty eway_bill_number/date/pdf, transport fields echo back from request. |
| `EasyEcom Account.gsp_print_format` (NEW Link → Print Format) | Per-Account override for the invoice PDF format. Blank → `Standard`. Set to the client's branded GST Tax Invoice format. |
| `EasyEcom Account.gsp_ewaybill_print_format` (NEW Link → Print Format) | Per-Account override for the e-way bill PDF format. Blank → `e-Waybill` (India Compliance's format). |
| `Sales Invoice.ecs_easyecom_invoice_id` (NEW Custom Field) | Idempotency anchor — EE's invoice_id stamped here. |
| `Sales Invoice.ecs_easyecom_invoice_number` (NEW) | EE-side GST invoice series number (for Mode 2 / mirror). |
| `Sales Invoice.ecs_easyecom_invoice_pdf_url` (NEW) | URL of EE-side PDF (Mode 2 only). |
| `Sales Invoice.ecs_easyecom_b2b_order_map` (NEW Link) | Back-ref to the B2B Order Map. |
| `Sales Invoice.ecs_easyecom_section_break` (NEW Section) | Collapsible UI section grouping the above on the SI form. |
| `EasyEcom Sync Record.direction` enum (extended) | Added `Inbound API` and `Cancel` values. Mode 1 writes Sync Records with `direction = "Inbound API"`. |

### 4.5 Code map

| File | What |
|---|---|
| `ecommerce_super/easyecom/api/gsp.py` | The three whitelisted endpoints. Returns EE-shape body. |
| `ecommerce_super/easyecom/flows/b2b_sales/gsp_auth.py` | Basic + Bearer auth helpers, token mint + validate + cleanup |
| `ecommerce_super/easyecom/flows/b2b_sales/gsp_handler.py` | SI find/create + India Compliance integration + response assembly |
| `ecommerce_super/easyecom/flows/b2b_sales/invoice_mirror.py` | Shared with Mode 2 — does the SI find/create from EE payload |
| `ecommerce_super/easyecom/doctype/easyecom_gsp_token/` | Token storage DocType |
| `ecommerce_super/patches/v0_1/add_gsp_basic_auth_secret_field.py` | Installs the Custom Field on EE Account |
| `ecommerce_super/patches/v0_1/add_b2b_mode2_sales_invoice_fields.py` | Installs the Custom Fields on Sales Invoice |
| `ecommerce_super/tests/unit/test_gsp_auth.py` | 19 unit tests for auth helpers |
| `ecommerce_super/tests/unit/test_b2b_invoice_mirror.py` | 30 unit tests for the SI find/create substrate |

---

## 5. Setup checklist (per EE Account, ~30 min)

### Step 1 — Confirm India Compliance on ERPNext side (5 min)

```
Always required (Custom GSP needs IC for tax / HSN / GST validation
even when NIC minting is OFF):
  1. India Compliance is installed (Desk → Module Def → search "India")
  2. Seller Company has GSTIN set
  3. Items used in B2B have gst_hsn_code populated
  4. Item Tax Template exists for the seller Company at the right tax rate

Only if you'll set gsp_mint_einvoice = ON (Step 2b):
  5. GST Settings → NIC IRP credentials configured
  6. Smoke test: create a Draft SI manually, click "Generate IRN" via
     India Compliance's standard button, confirm IRN lands on the SI

Only if you'll set gsp_mint_ewaybill = ON (Step 2b):
  7. GST Settings → NIC EWB credentials configured
  8. Smoke test: from a submitted SI, generate an e-way bill via IC's
     standard button, confirm ewaybill number is returned

If a relevant smoke fails for a toggle you plan to leave ON, do NOT
proceed — Mode 1 will fail at the corresponding mint step. If both
toggles will be OFF, skip steps 5-8 entirely.

mmpl16 is already proven for full Mode 1: 2,409 e-invoices minted via
IC as of 2026-06-27.
```

### Step 2 — Set the Custom GSP Basic auth secret on the EE Account (5 min)

```
On ERPNext Desk:
  1. EasyEcom Account list → open the target Account
  2. Expand the "Custom GSP (§11.5.1 Mode 1)" collapsible section
  3. Generate a random secret:
     openssl rand -hex 32
  4. Paste into "Custom GSP Basic Auth Secret" field
  5. Save the Account
  6. KEEP A COPY of the plaintext secret — once saved, you can't read it back
     (only re-set). Store in password manager.
```

### Step 2b — Decide minting behaviour via the toggles (2 min)

Two Check fields appear below the secret, both **ON by default**:

| Field | Default | Decide based on |
|---|---|---|
| `gsp_mint_einvoice` | ON | Is the client subject to e-invoicing (above turnover threshold) AND do we own the IRP integration? Yes → leave ON. No → flip OFF. |
| `gsp_mint_ewaybill` | ON | Does the client want NIC EWB minted at invoice time? Or are e-way bills handled physically / by the forwarder / via a separate system? |

**Toggle-OFF response shape** (so the FDE knows what EE will see):

| Field | Both ON | EInv OFF | EWB OFF |
|---|---|---|---|
| `invoice_pdf` (/einvoice resp) | URL | URL | — |
| `irn` / `ack_number` / `ack_date` / `irn_qr` | populated | empty | — |
| `eway_bill_number` / `eway_bill_date` / `eway_bill_pdf` (/ewaybill resp) | populated | populated | empty |
| `transport_mode` / `vehicle_number` / `transporter_*` | populated | populated | echoed from request |

**Gotcha — NIC EWB needs an IRN.** `gsp_mint_einvoice` OFF + `gsp_mint_ewaybill` ON will fail at EWB time (NIC rejects the EWB request because there's no IRN to bind to). The IC error surfaces as HTTP 422 to EE. Either keep both ON, or turn both OFF.

**Toggles do not affect idempotency** — once an IRN or EWB is minted, future calls return the cached value regardless of toggle state. Flipping a toggle after minting only impacts future fresh invoices.

### Step 2c — Pick the Print Formats EE will display (3 min)

The `invoice_pdf` / `eway_bill_pdf` URLs we return are Frappe print URLs (`?doctype=Sales+Invoice&format=<format-name>&...`). EE downloads the PDF from there when an EE user clicks "View Invoice" / "View E-Way Bill". So the Print Format chosen here is **what the EE-side user sees**.

Two Link fields, both default blank:

| Field | Fallback when blank | When to set explicitly |
|---|---|---|
| `gsp_print_format` | `Standard` (Frappe's default Sales Invoice format) | Almost always — clients want their branded GST Tax Invoice layout, not Frappe's default. Especially important when `gsp_mint_einvoice` is OFF and Custom GSP is being used purely for the PDF. |
| `gsp_ewaybill_print_format` | `e-Waybill` (India Compliance's format) | Only if the client needs a custom EWB layout — IC's default carries everything NIC requires, so most clients leave this blank. |

Both fields filter to Sales Invoice — pointing at a Print Format for another doctype will 500 at render time.

**Authoring a custom Print Format:**
1. Desk → Print Format → New
2. Doctype: Sales Invoice
3. Build the layout (Print Format Builder for HTML/CSS; or use the standard Jinja templating)
4. Save with a memorable name (e.g. "MMPL GST Tax Invoice 2026")
5. Return to EasyEcom Account, set `gsp_print_format` to that name

The Print Format itself is a Frappe DocType — version-controlled via the standard `--export-fixtures` flow if you want it under git.

### Step 3 — Configure the EE-side Custom GSP (10 min)

```
On EE web UI (path varies by EE plan tier — ask EE account manager):
  1. Settings → B2B → Custom GSP (or similar)
  2. Enable "Use Custom GSP"
  3. Endpoint configuration:
     - Token URL:    https://<your-bench>/api/method/ecommerce_super.easyecom.api.gsp.gettoken
     - Invoice URL:  https://<your-bench>/api/method/ecommerce_super.easyecom.api.gsp.einvoice_update
     - E-Way URL:    https://<your-bench>/api/method/ecommerce_super.easyecom.api.gsp.ewaybill_update
     (OR if EE only takes a base URL + appends paths — see "URL rewriting" note above)
  4. Basic auth credentials:
     - Username: anything (we ignore it)
     - Password: <paste the secret from Step 2>
  5. Save EE-side config
```

### Step 4 — Sanity-check the Custom Fields are visible on SI (1 min)

```
On ERPNext Desk:
  1. Open any existing Sales Invoice
  2. Scroll to the "EasyEcom Integration" collapsible section (should be near the bottom)
  3. Confirm fields visible: EE Invoice ID, EE Invoice Number, EE Invoice PDF URL, EE B2B Order Map
```

If not visible, re-run `bench --site <site> migrate` and `bench --site <site> clear-cache`.

### Step 5 — Confirm the GSP Token DocType is empty (1 min)

```
On ERPNext Desk:
  1. Navigate to EasyEcom GSP Token list view (search "GSP Token")
  2. Confirm list is empty (no tokens issued yet)
```

This is just a sanity check — the list will populate after the first /gettoken call.

---

## 6. Live smoke test (first invoice end-to-end)

### Step 1 — Trigger an invoice on EE side

```
1. Pick a submitted B2B order on EE (preferably one whose corresponding ERPNext
   SO + B2B Order Map already exists; if not, push a fresh SO from ERPNext first
   so the §11 push lands the order on EE)
2. On the EE UI: click "Generate Invoice"
3. Watch the EE-side UI message — expect success + IRN displayed within 5-10 seconds
```

### Step 2 — Verify ERPNext side

```
1. Open ERPNext Desk → Sales Invoice list
2. Sort by descending creation — the newly-created SI should be at the top
3. Open it:
   - docstatus = Submitted (1)
   - irn = populated (64-char hex)
   - ack_no = populated
   - ack_dt = populated
   - signed_qr_code = populated
   - EasyEcom Integration section: ecs_easyecom_invoice_id = matches EE's invoice_id
4. Open EasyEcom Sync Record list → filter by direction = "Inbound API"
   - Should show one row, status = Success
5. Open EasyEcom GSP Token list
   - Should show one token, easyecom_account = your target Account, expires_at = ~1hr away
```

### Step 3 — Verify in EE-side UI

```
1. Refresh the EE order page
2. The "Invoice" tab should now show:
   - IRN matching what's on the ERPNext SI
   - PDF download link (clicks through to <bench>/api/method/frappe.utils.print_format...)
3. Click the PDF link — should download the ERPNext-rendered Sales Invoice print
```

### Step 4 — Try /ewaybill/update (optional)

```
If the EE UI also has "Generate E-Way Bill" option:
1. Fill in transport details (vehicle number, transporter GST, etc.)
2. Click Generate
3. Verify on ERPNext: SI now has ewaybill field populated
4. Verify on EE: e-way bill number and PDF link visible
```

---

## 7. Failure modes — what each error means

| EE-side symptom | HTTP code we return | What it means | Where to look / fix |
|---|---|---|---|
| "Invalid GSP credentials" | 401 | Basic auth secret on EE doesn't match the one stored on EE Account | Re-check the secret on both sides — same string, case-sensitive |
| "Bearer token invalid" | 401 | Bearer expired (>1hr old) OR token row was deleted | EE should re-call /gettoken — usually automatic |
| "Body must have orders[]..." | 422 | Malformed payload from EE | Check EE side config — wrong content-type or wrong body shape |
| "EE payload missing invoice_id" | 422 | EE sent payload without invoice_id field | Confirm EE has actually invoiced the order on its side first |
| "EE payload missing reference_code" | 422 | EE sent payload without reference_code | EE bug — escalate |
| "No EasyEcom B2B Order Map found for reference_code" | 422 | The SO was never pushed via §11 (Map row doesn't exist) | Confirm the SO was submitted and §11 push ran |
| "No EasyEcom Customer Map for ee_c_id" | 422 | The buyer isn't synced to ERPNext yet | Run §8e Customer Push or wait for next §8e Pull tick |
| "EE SKU(s) ... have no EasyEcom Item Map" | 422 | A line item's SKU isn't in ERPNext | Run §8d Item Push for those SKUs |
| "HSN missing" | 422 (via IC) | Item has no `gst_hsn_code` | Set HSN on the Item in ERPNext |
| "Invalid TaxRule Name" | 422 (via IC) | Item Tax Template not properly configured for the seller | Configure Item Tax Templates per India Compliance docs |
| "SI ... could not be submitted" | 422 | The SI we created has a validation error blocking submit | Open the SI in ERPNext, see what's wrong (usually data quality on linked records) |
| "India Compliance IRN mint failed" | 422 | NIC IRP returned an error (e.g. invalid buyer GSTIN, duplicate IRN, etc.) | Check Error Log + e-Invoice Log for NIC's exact error message |
| "NIC IRP timeout" | 502 | Government IRN portal is slow / down | Retry — EE usually retries automatically. Check https://einvoice1.gst.gov.in status |
| "Duplicate IRN" (Idempotency hit) | 200 (returned cached) | SI already has IRN minted — our idempotency layer fired correctly | Not an error — the response carries the cached IRN |

---

## 8. Operational concerns

### 8.1 Token lifecycle

- 1 hour TTL per EE's spec
- Plaintext returned ONCE at /gettoken; never persisted (SHA-256 hash only)
- A daily scheduler tick (`cleanup_expired_tokens` in `gsp_auth.py`) deletes tokens past `(expires_at + 7 days)` — recently-expired tokens stay queryable for audit
- Active tokens never deleted on rotation — old ones stay valid until their own expiry (EE might still have one in flight)
- Each /gettoken call mints a fresh token; multi-issuance allowed for the same account

### 8.2 Audit trail

Every successful invoice mint produces:
1. One `EasyEcom Sync Record` (direction = "Inbound API", entity = Sales Invoice)
2. One `EasyEcom API Call` log (the IRN POST to NIC IRP, via India Compliance)
3. One `e-Invoice Log` row (India Compliance's record)
4. One submitted `Sales Invoice` with India Compliance fields populated

Reverse lookup paths:
- EE invoice_id → SI: query Sales Invoice by `ecs_easyecom_invoice_id`
- SO → SI: B2B Order Map.sales_invoice link (or follow SI back to the Map via `ecs_easyecom_b2b_order_map`)
- Token usage: filter EasyEcom GSP Token by `last_used_at` for audit

### 8.3 PDF delivery

Currently the response carries `invoice_pdf` as a Frappe download URL:

```
https://<bench>/api/method/frappe.utils.print_format.download_pdf?doctype=Sales+Invoice&name=<SI>&format=Standard&no_letterhead=0
```

EE's server downloads on demand. No file is persisted server-side — PDF rendered fresh on each request.

The `invoice_base64` field in our response is currently **empty**. If EE-side telemetry shows EE preferring inline base64, we'll add lazy base64 rendering in a follow-up (~2 hours of work).

To change the print format (default = `Standard`), edit `_resolve_invoice_pdf_url` in `gsp_handler.py` to point at your client-specific format.

### 8.4 Multi-tenant benches

If one bench serves multiple EE Accounts (each potentially with their own NIC IRP credentials via India Compliance Company-level settings):

- Each Account gets its own unique `gsp_basic_auth_secret` — generate per Account
- /gettoken iterates enabled Accounts trying each secret; the matched one becomes the token's scope
- All downstream operations use the matched Account's scope (SI Company, Item Tax Templates, etc., follow the standard ERPNext Company-scoping)

For single-tenant benches (the common case), there's only one secret to manage.

### 8.5 Rate limits

EE's expected call volume is roughly **1 invoice per B2B order**. For a typical mid-size client, that's a few dozen to a few hundred per day. No throttling concerns at ERPNext side; NIC IRP has its own rate limits which India Compliance handles via retry/backoff.

If a client is at e-invoicing volume (>10k invoices/day), the bottleneck becomes NIC IRP latency (~2-5s per mint). Bench can absorb this since we're handling in the request-response (synchronous) path — but the EE-side experience would feel slow. Out of scope for now; flag if a client hits this.

---

## 9. Mode 1 vs Mode 2 — picking one per Account

| | **Mode 1: Custom GSP (this guide)** | **Mode 2: EE-generated mirror** |
|---|---|---|
| Who mints IRN | ERPNext via India Compliance | EE via its own GSP or marketplace |
| NIC IRP credentials | Configured on ERPNext | Configured on EE |
| ERPNext SI carries IRN | Yes (IC writes `irn` field) | Maybe (if EE includes IRN in polling response — see Mode 2 docs) |
| ERPNext SI is | Submitted (auto, post-mint) | Draft (FDE reviews + submits manually) |
| Trigger | Synchronous: EE calls our endpoint | Async: our */5 polling cron detects EE-side invoice |
| Latency from EE-click to ERPNext SI | 1-3 seconds | Up to 5 minutes (next polling tick) |
| FDE Setup work | One-time per Account: 30 min | None — works automatically |
| 1% variance check | N/A (we compute) | Yes — Discrepancy raised on >1% diff vs EE's total |
| PDF source | ERPNext print format (we serve URL) | EE-hosted PDF URL (in polling response) |
| GL impact | Immediate (SI submits on mint) | Manual (FDE submits Draft) |

### When to pick Mode 1 (Custom GSP)

- Client wants ERPNext to be the e-invoice authority
- Client's accountants work in ERPNext, not EE
- Client wants automatic submission + GL impact on invoice generation
- Client already has India Compliance + NIC IRP credentials configured on ERPNext

### When to pick Mode 2 (EE mirror)

- Client's EE Account already has its own working GSP integration
- EE is the primary ERP / order management system; ERPNext is downstream
- Marketplace handles e-invoicing on EE's side (e.g., Amazon-fulfilled orders)
- Client wants FDE review before SI lands in ERPNext GL

### Can both be active simultaneously?

**No**. If both modes are enabled for the same EE Account:
- Mode 1 fires when EE clicks "Generate Invoice" (creates submitted SI)
- Mode 2 fires when our polling tick detects EE's invoice_number → creates Draft SI

You'd end up with two SIs (the Mode 1 submitted one with IRN, the Mode 2 draft one without). The Mode 2 mirror is idempotent on `invoice_id` (won't double-create on a single order), but they'd be different SIs for the same EE order — bad state.

**Pick ONE per Account.** The deployment flow:
- Mode 1: set `gsp_basic_auth_secret` on the EE Account + configure EE-side Custom GSP
- Mode 2: do NOT set `gsp_basic_auth_secret` (leave blank); polling does the rest

---

## 10. Code map + references

### Implementation files

| File | LOC | Role |
|---|---|---|
| `ecommerce_super/easyecom/api/gsp.py` | ~330 | Three whitelisted endpoint handlers |
| `ecommerce_super/easyecom/flows/b2b_sales/gsp_auth.py` | ~245 | Token mint + Basic/Bearer validation + cleanup |
| `ecommerce_super/easyecom/flows/b2b_sales/gsp_handler.py` | ~390 | SI find/create + IC mint + response assembly |
| `ecommerce_super/easyecom/flows/b2b_sales/invoice_mirror.py` | ~390 | SI creation from EE payload (shared with Mode 2) |
| `ecommerce_super/easyecom/doctype/easyecom_gsp_token/` | — | GSP Token DocType |
| `ecommerce_super/patches/v0_1/add_gsp_basic_auth_secret_field.py` | 55 | Custom Field installation patch |
| `ecommerce_super/patches/v0_1/add_b2b_mode2_sales_invoice_fields.py` | 96 | SI Custom Fields installation patch (also used by Mode 2) |

### Test files

| File | Tests | Coverage |
|---|---|---|
| `ecommerce_super/tests/unit/test_gsp_auth.py` | 19 | Basic auth verifier (incl. multi-account, blank secret), Bearer mint (hash storage, TTL), Bearer validate (valid/expired/unknown) |
| `ecommerce_super/tests/unit/test_b2b_invoice_mirror.py` | 30 | SI find/create substrate, IRN field extraction, variance checks |
| `ecommerce_super/tests/unit/test_b2b_polling_id_backfill.py` | 6 | Polling ID backfill (shared with §11 Phase 1) |
| `ecommerce_super/tests/unit/test_b2b_fast_confirm.py` | 6 | Fast-confirm queue check |
| `ecommerce_super/tests/unit/test_b2b_polling_derivation.py` | 19 | Polling status derivation |

### PRs and tracking

| Item | Reference |
|---|---|
| Issue tracking | [#99](https://github.com/incirco/ecommerce_super/issues/99) |
| Mode 1 implementation PR | [#104](https://github.com/incirco/ecommerce_super/pull/104) |
| Mode 2 SI mirror | [#103](https://github.com/incirco/ecommerce_super/pull/103) (merged) |
| Spec draft | `drafts/spec_sections/section_11_5_custom_gsp_packet.draft.md` |
| FDE Primer (setup checklist focus) | `process/primers/FDE_PRIMER_section_11_5_1_custom_gsp.md` |
| This guide (full reference) | `process/primers/GUIDE_custom_gsp_invoice_flow.md` |

### India Compliance entry points used

| Function | Purpose |
|---|---|
| `india_compliance.gst_india.utils.e_invoice.generate_e_invoice(docname, throw=True, force=False)` | Mints IRN on NIC IRP for a submitted SI. Writes `irn`, `ack_no`, `ack_dt`, `signed_qr_code` on the SI. |
| `india_compliance.gst_india.utils.e_waybill.generate_e_waybill(doctype, docname, values, force=False)` | Mints e-way bill on NIC EWB for a submitted SI. Writes `ewaybill` on the SI. |

### Open questions / future work

- **Inline PDF base64** — `invoice_base64` and `eway_bill_base64` currently empty. Add lazy rendering if EE-side telemetry shows base64 preferred.
- **Auto-cancel on EE cancel** — If EE cancels an already-minted invoice, we don't auto-cancel on NIC. India Compliance has `cancel_e_invoice` — wire if client wants it.
- **URL rewriting** — If EE's Custom GSP form requires fixed paths like `/einvoice/update` (not `/api/method/...`), add nginx-level rewrite or Frappe `website_route_rules`.
- **Per-client print format customization** — `_resolve_invoice_pdf_url` uses default `Standard` format. Add per-client formats via `EasyEcom Account` Custom Field.
- **Sandbox NIC IRP** — Current setup uses production NIC. Add a sandbox toggle if client wants stage testing without real e-invoices.
- **Async mode** — Currently synchronous (EE waits up to 30s for NIC response). If NIC reliability becomes an issue, return 202 + provide a status-poll endpoint. Need EE-side support for 202 first.

---

## Quick reference card

**URLs**:
```
https://<your-bench>/api/method/ecommerce_super.easyecom.api.gsp.gettoken
https://<your-bench>/api/method/ecommerce_super.easyecom.api.gsp.einvoice_update
https://<your-bench>/api/method/ecommerce_super.easyecom.api.gsp.ewaybill_update
```

**Auth**:
```
Step 1: Basic <base64(any:secret)>  → returns Bearer (1hr TTL)
Step 2: Bearer <token>              → for /einvoice/update + /ewaybill/update
```

**Setup**:
1. Verify India Compliance works (manual e-invoice test)
2. Set `gsp_basic_auth_secret` on EE Account (ERPNext side)
3. Paste secret on EE Custom GSP form (EE side) + URLs above
4. Smoke test from EE side

**Validate it's working**:
- Submitted SI in ERPNext with `irn` populated
- `EasyEcom Sync Record` with `direction = "Inbound API"`
- EE UI shows IRN + downloadable PDF

**Tracking**: Issue #99 · PR #104 · Patch note 12 in `SPEC_11_patch_notes.md`

---

## Related primers

| Primer | Use when |
|---|---|
| `FDE_PRIMER_section_11_5_1_custom_gsp.md` | You just need the setup checklist, not the full architectural walkthrough |
| `FDE_PRIMER_section_11_b2b_sales.md` | §11 Phase 1 baseline — the SO push that produces the Map row Custom GSP later finds |
| `FDE_PRIMER_section_11_6_dispatch_status.md` | After Custom GSP mints the SI, polling stamps Shipped/Delivered on subsequent ticks |

# §11.5.1 Custom GSP — FDE Deployment Guide

## What this is

EE has a "Custom GSP" feature on its B2B Account config. When enabled, EE
will call YOUR configured URL (instead of its built-in GSP) every time
someone clicks "Generate Invoice" on an EE order. We — the ecommerce_super
integration — expose three endpoints that EE calls. We then mint the IRN
via India Compliance on the ERPNext side and return the response.

**Net effect for the client**: every EE invoice generation produces a real
ERPNext Sales Invoice (with IRN, ack_no, e-way bill if requested), and the
PDF that EE displays comes from the ERPNext side. ERPNext is the single
source of truth for invoices; India Compliance does the NIC IRP mint;
EE's UI just displays.

This is **Mode 1** of the §11.5 invoice flow. Mode 2 (EE-generated mirror)
is the alternative where EE owns the IRN and we just mirror. Pick ONE per
EE Account.

---

## Setup checklist (per EE Account, ~30 min)

### Step 1 — Confirm India Compliance is configured

```
On the ERPNext side (always required):
  1. India Compliance app is installed
  2. Company → has GSTIN set on the seller Company
  3. Items being sold via B2B → have HSN code + Item Tax Template

Only if gsp_mint_einvoice will be ON (Step 2b):
  4. Settings → GST Settings → NIC IRP credentials configured
  5. (Test) manually generate an e-invoice for any draft SI to confirm IC works

Only if gsp_mint_ewaybill will be ON (Step 2b):
  6. Settings → GST Settings → NIC EWB credentials configured
  7. (Test) manually generate an e-way bill for a submitted SI
```

**NIC portal credentials are only needed when you're minting via that
portal.** If a client uses Custom GSP just to get the ERPNext-side PDF
(both toggles OFF, see Step 2b), NIC credentials can stay blank — the
SI is still created and submitted, just no IRP / EWB calls happen.

If a toggle is ON but the relevant credentials aren't set, Mode 1 will
return HTTP 500 / 502 when EE calls us — fix on the ERPNext side
before configuring EE.

mmpl16 already had 2,409 e-invoices minted via IC as of 2026-06-28, so
full Mode 1 (toggles ON) is well-tested for that bench.

### Step 2 — Set the Custom GSP Basic auth secret on the EasyEcom Account

```
On the ERPNext side:
  1. Desk → EasyEcom Account list → open the target Account
  2. Expand the "Custom GSP (§11.5.1 Mode 1)" section (collapsible)
  3. Generate a strong random secret (e.g. via `openssl rand -hex 32`)
  4. Paste into "Custom GSP Basic Auth Secret" field
  5. Save the Account
  6. Copy the secret — you'll paste it on EE side in Step 3
```

The secret is encrypted at rest via Frappe's Password fieldtype. Once saved
you can't read it back (only re-set). Keep a copy in a password manager.

### Step 2b — Decide what Custom GSP actually mints (toggles)

Below the secret field there are two Check fields, both **ON by default**:

| Field | Default | When you'd turn it OFF |
|---|---|---|
| `gsp_mint_einvoice` (Mint E-Invoice via India Compliance) | ON | Client is below the e-invoicing turnover threshold; OR e-invoicing is handled externally (marketplace, separate IRP integration); OR the client just wants Custom GSP for ERPNext-side invoice PDFs without minting IRN |
| `gsp_mint_ewaybill` (Mint E-Way Bill via India Compliance) | ON | Client handles e-way bills physically (forwarder paperwork); OR via another system; OR shipments don't cross the value threshold for EWB |

When **either toggle is OFF**, the flow still:
- Creates / finds the ERPNext Sales Invoice (idempotent on EE's `invoice_id`)
- Submits the SI (GL impact happens regardless)
- Returns the EE-shape response with `invoice_pdf` URL populated

What it **skips**:
- `gsp_mint_einvoice` OFF → no `generate_e_invoice` call. Response has empty `irn` / `ack_number` / `ack_date` / `irn_qr` fields.
- `gsp_mint_ewaybill` OFF → no `generate_e_waybill` call. Response has empty `eway_bill_number` / `eway_bill_date` / `eway_bill_pdf`. Transport fields (vehicle, transporter) are echoed back so EE has a paper trail.

**Common combinations:**
- Both ON (default) → Full Mode 1: EE invoices land in ERPNext + NIC IRP + NIC EWB. Right for most clients above the e-invoicing threshold.
- E-invoice ON, EWB OFF → IRN minted, but client uses physical paperwork / forwarder for EWB. Common for textile / FMCG with intra-state shipments.
- Both OFF → ERPNext is the SI authority, EE consumes the PDF, but no compliance minting on either side. Right when client handles compliance entirely externally OR is below thresholds.

Note: NIC EWB requires an IRN as input — so `gsp_mint_ewaybill` ON + `gsp_mint_einvoice` OFF will fail at EWB time. The IC error will surface as HTTP 422 to EE.

Idempotency is unaffected by toggles: once an IRN/EWB is minted, future calls return the cached value regardless of toggle state. Flipping a toggle after minting only affects future fresh invoices, not historical ones.

### Step 2c — Pick the print formats for the PDFs EE will display

Two Link fields (both default blank → use built-in formats):

| Field | Default if blank | Set this when |
|---|---|---|
| `gsp_print_format` (Custom GSP Invoice Print Format) | `Standard` (Frappe's default Sales Invoice format) | Client has a branded GST invoice template — point to that Print Format's name |
| `gsp_ewaybill_print_format` (Custom GSP E-Way Bill Print Format) | `e-Waybill` (India Compliance's format) | Client wants a custom EWB layout |

Both fields filter to `doctype = Sales Invoice` — picking a format for another doctype will 500 at render time.

**Why this matters:** the URL we return in `invoice_pdf` is a Frappe print URL like `?doctype=Sales+Invoice&format=<your-format>&...`. EE downloads the PDF from that URL when an EE user clicks "View Invoice". So the format chosen here is what the EE-side user sees.

Common case: when both `gsp_mint_einvoice` and `gsp_mint_ewaybill` are OFF (ERPNext-side PDF only — no NIC minting), set `gsp_print_format` to the client's branded invoice format. That's the whole reason they enabled Custom GSP — Frappe's "Standard" template is almost certainly not what they want EE displaying.

### Step 3 — Configure EE-side Custom GSP

```
On the EasyEcom UI:
  1. Settings → B2B → Custom GSP (or equivalent menu path for the EE
     account's plan tier)
  2. Enable "Use Custom GSP"
  3. Set the endpoint URL to your ERPNext bench's GSP base URL:
       https://<your-bench>.frappe.cloud/api/method/ecommerce_super.easyecom.api.gsp
  4. Set the Basic auth credentials:
       - Username: anything (we ignore it — the secret matches against any
         enabled EE Account on the bench)
       - Password: paste the secret from Step 2
  5. Save EE-side config
```

EE's exact UI path varies by account tier — check with your EE account
manager if you can't find the Custom GSP settings page.

### Step 4 — Smoke test

```
On EE:
  1. Find any submitted B2B order on the EE Account
  2. Click "Generate Invoice"
  3. Expect: success message + IRN displayed
  4. Confirm: the corresponding ERPNext SI now exists with irn populated

If EE shows an error:
  - 401 → Basic auth secret mismatch (re-check Step 2/3)
  - 422 → Payload validation (check ERPNext Error Log for detail)
  - 502 → NIC IRP / India Compliance error (check e-Invoice Log)
```

---

## The three endpoints (for reference)

EE calls these — you don't call them manually.

```
POST  /api/method/ecommerce_super.easyecom.api.gsp.gettoken
  Auth: Basic <base64(any:secret)>
  Response: { access_token: <bearer>, expires_in: 3600 }

POST  /api/method/ecommerce_super.easyecom.api.gsp.einvoice_update
  Auth: Bearer <token from /gettoken>
  Body: { orders: [<EE order JSON>] }
  Response: { data: { invoice_details: { irn, ack_number, invoice_pdf, ... } } }

POST  /api/method/ecommerce_super.easyecom.api.gsp.ewaybill_update
  Auth: Bearer <token>
  Body: { orders: { <EE order JSON with transport_* fields> } }
  Response: { data: { invoice_details: { eway_bill_number, eway_bill_pdf, ... } } }
```

Token TTL is 1 hour. EE will re-call `/gettoken` to mint a new one when
the current expires.

---

## What happens on the ERPNext side per invoice call

```
EE → POST /einvoice/update
  ↓
Our endpoint validates Bearer → resolves EasyEcom Account → returns 401 if bad
  ↓
We look up the SI by EE's invoice_id (idempotency)
  - Found? → return cached IRN (no re-mint — safe to call repeatedly)
  - Not found? → Look up reference_code → B2B Order Map → SO → CREATE SI
                  from payload (reuses Mode 2 mirror logic)
  ↓
If SI is Draft → Submit it (India Compliance requires submitted SI)
  ↓
Call India Compliance generate_e_invoice(si) → NIC IRP mints IRN → IC
writes irn/ack_no/ack_dt/signed_qr_code on the SI
  ↓
Build response body with IRN + ack + PDF URL
  ↓
Return to EE
```

Every call writes one EasyEcom Sync Record (`direction = "Inbound API"`,
`entity_type = "Sales Invoice"`). Failed mints land as Failed Sync Records
with the error detail — FDE worklist surfaces these.

---

## Common failure modes + responses

| EE-side symptom | What it means | Where to look |
|---|---|---|
| "Invalid GSP credentials" / 401 | Basic auth secret on EE doesn't match the one stored on the EE Account | Re-check Step 2/3 — secrets match? |
| "Order not found" / 422 | EE's reference_code doesn't resolve to a B2B Order Map row | Check `EasyEcom B2B Order Map` for the SO; if missing, the SO was never §11-pushed to EE |
| "Customer Map missing" / 422 | The buyer in the EE order isn't synced to ERPNext via §8e Customer Map | Run §8e Customer Push or wait for pull |
| "Item Map missing" / 422 | A line item's SKU isn't in ERPNext via §8d Item Map | Run §8d Item Push |
| "HSN missing" / 422 | Item has no `gst_hsn_code` set | Set HSN on Item |
| "Invalid TaxRule Name" / 422 (during mint) | Item's Item Tax Template not configured for the seller's GSTIN | Configure Item Tax Templates (India Compliance docs) |
| "NIC IRP timeout" / 502 | Government IRN portal is slow / down | Retry — EE retries the same call automatically |
| "Duplicate IRN" / 422 | SI already had IRN minted (idempotency safety net fired) | Not actually an error — IRN was already minted; check the returned response for the cached IRN |

---

## Mode 1 vs Mode 2 — which to pick

Pick **Mode 1** if:
- Your client wants ERPNext + India Compliance to be the e-invoice authority
- You control NIC IRP credentials (configured in IC)
- You want the SI in ERPNext to carry IRN/ack/QR natively

Pick **Mode 2** if:
- EE has its own GSP integration already (some EE plans bundle this)
- The marketplace handles e-invoicing on EE's side
- You just want ERPNext to mirror EE's invoice (read-only, no IRN minting on our side)

**Cannot pick both** for the same EE Account — Mode 1 means EE calls our
endpoint; Mode 2 means EE invoices on its own side and we observe via polling.
If both are enabled accidentally, our mirror flow will also run on EE-invoiced
orders and you'll end up with two SIs (the minted one from Mode 1 and the
mirror from Mode 2). The Mode 2 mirror is idempotent on invoice_id so it
won't double-create on a single order, but the two paths landing on different
ERPNext SIs would still need cleanup.

---

## Tokens table (housekeeping)

The bench stores GSP tokens in `tabEasyEcom GSP Token`. Only the SHA-256
hash is stored — plaintext tokens are returned ONCE to EE and never persisted.
A daily scheduler tick deletes tokens past `(expires_at + 7 days)` so the
table doesn't grow unbounded. Active and recently-expired tokens stay
queryable for audit (you can see which EE Account each was issued to,
when, from what IP).

---

## What's NOT in this build

- **PDF base64 inline** — the response carries the `invoice_pdf` URL
  (downloadable via Frappe print-format render). The `invoice_base64` field
  in the response is currently empty. If EE-side telemetry shows EE
  preferring base64, we'll add inline rendering in a follow-up.
- **Auto-cancel on EE cancel** — if EE cancels an already-minted invoice,
  we don't auto-cancel the IRN on NIC. India Compliance has its own
  `cancel_e_invoice` flow; FDE triggers it manually if needed.
- **Print format publishing UI** — the `gsp_print_format` /
  `gsp_ewaybill_print_format` Link fields exist and are wired into the PDF
  URL, but there's no in-app workflow to author a new Print Format from
  scratch. Use Frappe's standard Print Format Builder (Desk → Print Format
  → New) to design the layout, then point the EE Account at it.

---

## Origin

- Issue #99 — tracking issue
- Live-verified India Compliance status on mmpl16 (2026-06-27): 2,409
  e-invoices already minted via IC, all infrastructure operational
- §11.5.1 Custom GSP packet design call: 2026-06-26 with rishinikhil

See `drafts/spec_sections/section_11_5_custom_gsp_packet.draft.md` for the
full architectural rationale.

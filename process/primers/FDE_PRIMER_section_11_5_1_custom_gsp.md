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
On the ERPNext side:
  1. Settings → GST Settings → has NIC IRP credentials configured
  2. Company → has GSTIN set on the seller Company
  3. Items being sold via B2B → have HSN code + Item Tax Template
  4. (Test) manually generate an e-invoice for any draft SI to confirm IC works
```

If India Compliance isn't installed or has no credentials, Mode 1 will
return HTTP 500 / 502 errors when EE calls us — fix on the ERPNext side
before configuring EE.

mmpl16 already had 2,409 e-invoices minted via IC as of 2026-06-28, so this
is well-tested for that bench.

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
- **Print format customisation** — uses Frappe's default Sales Invoice print
  format. If your client wants a custom layout matching EE's existing PDF,
  build a per-client print format and reference it via the
  `_resolve_invoice_pdf_url` helper in gsp_handler.py.

---

## Origin

- Issue #99 — tracking issue
- Live-verified India Compliance status on mmpl16 (2026-06-27): 2,409
  e-invoices already minted via IC, all infrastructure operational
- §11.5.1 Custom GSP packet design call: 2026-06-26 with rishinikhil

See `drafts/spec_sections/section_11_5_custom_gsp_packet.draft.md` for the
full architectural rationale.

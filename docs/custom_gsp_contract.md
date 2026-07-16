# Custom GSP Contract — Reference

**Status:** Live on `mmpl16.frappe.cloud` since 2026-06-25. Contract v1.
**Audience:** Anyone integrating with this ERPNext app's Custom GSP endpoints — EasyEcom devs, partner devs, next maintainer.
**Canonical source of truth.** If this doc disagrees with any other spec, code, or verbal claim, this doc wins. If this doc disagrees with production behavior, production behavior wins — file a PR to correct this doc.

## Table of contents

- [1. Overview](#1-overview)
- [2. Endpoints](#2-endpoints)
  - [2.1 `/gettoken` — mint a Bearer token](#21-gettoken--mint-a-bearer-token)
  - [2.2 `/einvoice/update` — receive EE invoice → return IRN + PDF](#22-einvoiceupdate--receive-ee-invoice--return-irn--pdf)
  - [2.3 `/ewaybill/update` — receive EE transport info → return e-way bill](#23-ewaybillupdate--receive-ee-transport-info--return-e-way-bill)
- [3. Failure modes reference](#3-failure-modes-reference)
- [4. Auth flow — full lifecycle](#4-auth-flow--full-lifecycle)
- [5. Idempotency](#5-idempotency)
- [6. curl playbook](#6-curl-playbook)
- [7. Change policy](#7-change-policy)

---

## 1. Overview

This ERPNext app implements a Custom GSP (GST Suvidha Provider) service that EasyEcom calls when it needs an e-invoice (IRN) or e-way bill minted for a B2B order.

### The three-leg conversation

```
Leg 1  ERPNext → EE    createOrder      (SO submitted → outbound push)
                                         Correlation stored on Map row.
Leg 2  EE-internal     Invoice work     Opaque to us. EE assigns invoice_id,
                                         generates their invoice draft.
Leg 3  EE → ERPNext    /einvoice/update Inbound: our GSP handler mints IRN
                                         via India Compliance, returns PDF
                                         base64 to EE.
```

### Endpoints at a glance

| Endpoint | Method | Auth | Purpose |
|---|---|---|---|
| `/gettoken` | POST | Basic | Mint a 15-min Bearer token |
| `/einvoice/update` | POST | Bearer | Create + submit SI, mint IRN, return PDF |
| `/ewaybill/update` | POST | Bearer | Update transport fields, mint e-way bill, return PDF |

All endpoints are also available under `/api/method/ecommerce_super.easyecom.api.gsp.<function>` for direct Python API access; the root aliases (`/gettoken` etc.) are the Frappe-standard shape EE uses in production.

### Response shape convention

All GSP responses are **flat** — top-level `{"status": 200, ...}` rather than Frappe's default `{"message": {...}}` envelope. This matches EE's spec and is enforced by the `@_gsp_endpoint` decorator.

---

## 2. Endpoints

### 2.1 `/gettoken` — mint a Bearer token

Exchanges Basic auth for a short-lived Bearer token used on subsequent calls.

**URL:** `POST https://<site>/gettoken` (or `/api/method/ecommerce_super.easyecom.api.gsp.gettoken`)

**Headers:**

| Header | Value |
|---|---|
| `Authorization` | `Basic <base64(user:secret)>` where `user` is your allocated GSP username and `secret` matches the site's `EasyEcom Account.gsp_basic_auth_secret` field. |

**Request body:** none. Any body is ignored.

**Success response (HTTP 200):**

```json
{
  "status": 200,
  "access_token": "eyJhbGciOiJIUzI1NiIs...",
  "token_type": "Bearer",
  "expires_in": 900
}
```

- **`expires_in`** — seconds. Currently **900 (15 minutes)**. Was `3600` prior to gh#166 hardening (2026-07-13). Do not cache tokens beyond this window; mint a fresh one on expiry.

**Failure responses:**

| HTTP | Body | Cause |
|---|---|---|
| 401 | `{"status": 401, "message": "..."}` | Missing/malformed Authorization header, wrong secret, or client IP not in `EasyEcom Account.gsp_ip_allowlist` (if set) |
| 500 | `{"status": 500, "message": "Token mint failed."}` | Internal error minting the token. Check the site's Error Log. |

**Rate limits:** none on `/gettoken` currently; if abuse becomes an issue, add via `EasyEcom Account.gsp_rate_limit_per_min`.

---

### 2.2 `/einvoice/update` — receive EE invoice → return IRN + PDF

EE calls this when it has generated a draft invoice for an order we previously pushed via createOrder. We:

1. Look up or create the mirrored Sales Invoice (idempotent by `invoice_id`)
2. Submit it (India Compliance requires a submitted SI to mint IRN)
3. Trigger India Compliance's `generate_e_invoice` → IRN + QR + ack from NIC IRP
4. Return the IRN + a base64-encoded PDF back to EE

**URL:** `POST https://<site>/einvoice/update` (or `/api/method/ecommerce_super.easyecom.api.gsp.einvoice_update`)

**Headers:**

| Header | Value |
|---|---|
| `Authorization` | `Bearer <token from /gettoken>` |
| `Content-Type` | `application/json` |

**Request body:**

```json
{
  "orders": { ... EE order object, see below ... }
}
```

`orders` may be either **a single object** (EE's live shape) or **an array of length 1** (EE's original contract). Both are accepted defensively per gh#142. Multi-order arrays are not supported — only `orders[0]` is processed.

The EE order object shape mirrors `getOrderDetails` output. Required fields:

| Field | Type | Notes |
|---|---|---|
| `invoice_id` | integer or string | EE's own identifier. **The idempotency key** — call with same id → same SI. |
| `reference_code` | string | Our Sales Order name (e.g. `SO-2610382`). Needed if SI doesn't exist yet (path 3). |
| `invoice_number` | string | Human-readable invoice number. Stored on SI for display. |
| `invoice_date` | ISO date | Used as posting date if SI created fresh. |
| `invoice_currency_code` | string | Default `"INR"`. |
| `merchant_c_id` | integer | EE's customer id. Resolved via `EasyEcom Customer Map` → ERPNext Customer. |
| `total_amount` | float | EE's tax-inclusive total. Used for variance check against SI's `grand_total`. |
| `total_tax` | float | EE's total tax. Used for gh#214 fail-loud guard (0-tax detection). |
| `warehouse_id` | integer | EE's warehouse id. Resolved via `EasyEcom Location`. |
| `order_items` | array | See per-item shape below. |
| `documents` | object | Optional. `{"easyecom_invoice": "<URL>"}` for the PDF back-reference. |

Per-item shape (`order_items[]`):

| Field | Type | Notes |
|---|---|---|
| `sku` | string | EE's SKU. Resolved via `EasyEcom Item Map`. |
| `item_quantity` | integer | Quantity. |
| `taxable_value` | float | Post-promo, tax-exclusive per-line net. **Required** — mirror throws if missing (gh#207). |

Additional fields (`igst`, `cgst`, `sgst`, `utgst`, `breakup_types`, `selling_price`, `tax_rate`) are captured but not currently authoritative — SI totals are computed by ERPNext + India Compliance using the source SO's tax context (gh#206, gh#214).

**Success response (HTTP 200):**

```json
{
  "status": 200,
  "message": "Invoice fetched successfully",
  "reference_code": "SO-2610382",
  "data": {
    "invoice_details": {
      "invoice_id": "176305783",
      "erp_invoice_num": "SI-2603821",
      "irn": "1234567890abcdef1234567890abcdef1234567890abcdef1234567890abcdef",
      "ack_number": "112010012345678",
      "ack_date": "2026-07-16T14:15:00+05:30",
      "invoice_pdf": "https://mmpl16.frappe.cloud/...",
      "irn_qr": "<base64 or text>",
      "invoice_base64": "<base64-encoded PDF or empty>"
    }
  }
}
```

- **`erp_invoice_num`** — the ERPNext Sales Invoice docname. Useful for MMPL ops trace.
- **`invoice_base64`** — the PDF rendered from the Print Format configured on the EE Account (`gsp_invoice_print_format` field). Empty string if render failed; a Comment lands on the SI explaining the failure.

**Failure responses:** see [§3 Failure modes reference](#3-failure-modes-reference).

**Idempotency:** re-calling with the same `invoice_id` returns the same SI (via `ecs_easyecom_invoice_id` field lookup). Safe to retry.

---

### 2.3 `/ewaybill/update` — receive EE transport info → return e-way bill

EE calls this to mint an e-way bill once the invoice is finalised and transport details are known. Requires an already-minted IRN (from a prior `/einvoice/update` call for the same order).

**URL:** `POST https://<site>/ewaybill/update` (or `/api/method/ecommerce_super.easyecom.api.gsp.ewaybill_update`)

**Headers:** same as `/einvoice/update`.

**Request body:**

```json
{
  "orders": { ... EE order object with additional transport fields ... }
}
```

Same envelope as `/einvoice/update` (single object or single-element array). Required additional fields on the order object:

| Field | Type | Notes |
|---|---|---|
| `invoice_id` | integer or string | Must match a previously-processed invoice_id. |
| `transporter_id` | string | India Compliance transporter GSTIN/id. |
| `transporter_name` | string | Display name. |
| `vehicle_number` | string | e.g. `KA01AB1234`. |
| `mode_of_transport` | string | `"Road"` / `"Rail"` / `"Air"` / `"Ship"`. |
| `distance` | integer | Km. |

**Success response (HTTP 200):**

```json
{
  "status": 200,
  "message": "Eway bill generated successfully",
  "reference_code": "SO-2610382",
  "data": {
    "eway_details": {
      "invoice_id": "176305783",
      "erp_invoice_num": "SI-2603821",
      "eway_bill_number": "331000123456",
      "eway_bill_date": "2026-07-16T14:20:00+05:30",
      "valid_upto": "2026-07-17T14:20:00+05:30",
      "eway_bill_base64": "<base64 PDF>"
    }
  }
}
```

**Idempotency:** re-calling for an SI that already has an e-way bill returns the cached values.

**Failure responses:** see [§3 Failure modes reference](#3-failure-modes-reference).

---

## 3. Failure modes reference

Every non-200 response carries `{"status": <http>, "message": "<human-readable reason>"}` at minimum. The specific messages EE will see, indexed by HTTP status:

### 401 Unauthorized

| Message | Cause | FDE remediation |
|---|---|---|
| `Missing or malformed Authorization header` | No `Authorization: Basic ...` or `Authorization: Bearer ...` sent | Re-check the header casing + prefix |
| `Basic auth secret does not match` | Wrong password | Verify `EasyEcom Account.gsp_basic_auth_secret` on our side vs EE's stored credential |
| `Bearer token expired or invalid` | Token past 15-min TTL, or minted by a different account | Mint a fresh token via `/gettoken` |
| `Client IP <ip> not in allowlist` | IP allowlist configured and client IP not permitted | Update `EasyEcom Account.gsp_ip_allowlist` to include EE's outbound IP |

### 422 Unprocessable Entity

| Message | Cause | FDE remediation |
|---|---|---|
| `Body must have 'orders' as a single object or a non-empty array` | Missing `orders` or wrong shape | Check the request body — should have `{"orders": {...}}` |
| `Invalid JSON body: <detail>` | Not parseable JSON | Fix the body serialization |
| `SI create/find failed: <detail>` | Downstream failure — no customer map, no item map, missing HSN, etc. Detail carries the exact reason (post-gh#142) | Fix the underlying data (Customer Map, Item Map, HSN, GST context) — use the **Dry-Run /einvoice/update** button on the Map form to preview |
| `<type>: <message>` (e.g. `ValidationError: Missing item_tax_template`) | Frappe validation on the SI insert path | Fix the SO or Item Tax Template |
| `SI variance exceeded threshold — SI left in Draft for FDE review` (post gh#218) | SI grand_total differed from EE's `total_amount` by more than 1% after native tax recompute | Review the Draft SI; use **Re-fire /einvoice/update** button after fixing |
| `NIC IRP mint failed: <detail>` | India Compliance's `generate_e_invoice` returned error | Check E-Invoice Log for NIC-side detail |

### 429 Too Many Requests

| Message | Cause | FDE remediation |
|---|---|---|
| `Rate limit exceeded: N calls/min per invoice_id` | Same `invoice_id` hit the endpoint more than `EasyEcom Account.gsp_rate_limit_per_min` times in a rolling minute (default 6) | Ease off the retries; check EE-side loop |

### 502 Bad Gateway

| Message | Cause | FDE remediation |
|---|---|---|
| `NIC IRP mint failed: <NIC error>` | NIC IRP server 5xx or invalid response | Retry after cooldown; check NIC IRP status |

---

## 4. Auth flow — full lifecycle

```
1. Basic auth secret provisioned once (EasyEcom Account.gsp_basic_auth_secret,
   set by FDE via Desk).

2. EE calls POST /gettoken with Authorization: Basic <base64(user:secret)>.
   ↳ Site validates the secret + IP allowlist.
   ↳ Returns { access_token, expires_in: 900 }.

3. EE stores the token. For the next 15 minutes, every /einvoice/update and
   /ewaybill/update call carries Authorization: Bearer <token>.

4. After 15 min, EE's next call gets 401. EE re-mints via /gettoken and retries.
   The Bearer TTL of 15 min (was 1h pre-gh#166) is the security balance:
   short enough that a leaked token is bounded, long enough that /gettoken
   isn't hit every request.
```

Token storage on our side: Redis via `frappe.cache()` with the `expires_in` TTL. No DB write, no long-term artifact — tokens are transient.

---

## 5. Idempotency

Every EE-facing endpoint is safely retryable.

- **`/gettoken`** — always mints a new token. Retries produce different tokens; both are valid until their respective `expires_in`.
- **`/einvoice/update`** — idempotent on `invoice_id`. Re-calling with the same `invoice_id` returns the same SI (via the `ecs_easyecom_invoice_id` field on Sales Invoice). If SI is already IRN-minted, returns the cached IRN + PDF without re-hitting NIC IRP.
- **`/ewaybill/update`** — idempotent on `invoice_id`. Re-calling for an already-eway-billed SI returns the cached eway details.

Retries are safe. **EE should retry with the same body on transient failures** (429, 502) after a backoff.

---

## 6. curl playbook

Copy-paste-ready for smoke testing. Replace `$SITE`, `$SECRET`, `$TOKEN`, and the payload path.

### Mint a Bearer token

```bash
SITE=https://mmpl16.frappe.cloud
USER=gsp
SECRET='<the actual secret>'

TOKEN=$(curl -sS -X POST "$SITE/gettoken" \
  -H "Authorization: Basic $(printf '%s:%s' "$USER" "$SECRET" | base64)" \
  | jq -r '.access_token')

echo "$TOKEN"
```

### Fetch invoice for an SO (`/einvoice/update`)

```bash
curl -sS -X POST "$SITE/einvoice/update" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @tests/contract/gsp/fixtures/ee_einvoice_so_2610382.json \
  | jq
```

Expected success: `.status == 200`, `.data.invoice_details.irn` populated.

### Trigger e-way bill (`/ewaybill/update`)

```bash
curl -sS -X POST "$SITE/ewaybill/update" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d @tests/contract/gsp/fixtures/ee_ewaybill_so_2610382.json \
  | jq
```

Expected success: `.status == 200`, `.data.eway_details.eway_bill_number` populated.

### Test the fail-loud paths

```bash
# 401: missing bearer
curl -sS -X POST "$SITE/einvoice/update" \
  -H "Content-Type: application/json" \
  -d '{"orders": {}}' | jq

# 422: no invoice_id
curl -sS -X POST "$SITE/einvoice/update" \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"orders": {"reference_code": "SO-DOES-NOT-EXIST"}}' | jq
```

### FDE-side self-service

Two Desk buttons live on the **EasyEcom B2B Order Map** form (Diagnostics group):

- **Dry-Run /einvoice/update** — simulates the call without side effects; useful for pre-flight check
- **Re-fire /einvoice/update** — actually runs the handler chain again (for post-fix remediation)

Both are gated to `EasyEcom FDE` / `System Manager` roles.

---

## 7. Change policy

**Any behavior change to these three endpoints MUST update this document in the same PR.** Enforced by CLAUDE.md's discipline section. A code change that ships without a corresponding doc update is a spec-drift bug — the exact same class of failure that gh#130 and gh#142 originally created.

**Concretely:**

- Adding a new request field → document it in the field table
- Adding a new response field → document it + example
- Adding a new failure message → document it in [§3](#3-failure-modes-reference)
- Changing the Bearer TTL, rate limit default, or auth requirement → update the relevant subsection + note the change in the [Overview](#1-overview)
- New endpoint → add a new §2.X subsection with the full shape

**Reviewers** on any PR touching `ecommerce_super/easyecom/api/gsp.py` or `ecommerce_super/easyecom/flows/b2b_sales/gsp_handler.py` should reject the PR if this doc isn't updated in the same commit set. The doc-first culture is what closes the "code shipped, spec forgot" gap.

**Contract-test fixtures:** every documented request shape has a matching JSON fixture under `tests/contract/gsp/fixtures/` (see gh#151). The fixtures ARE the ground truth for what EE sends live. If this doc contradicts a fixture, the fixture wins — file a PR to correct this doc.

---

## Cross-references

- **Code:** `ecommerce_super/easyecom/api/gsp.py` (endpoint handlers), `ecommerce_super/easyecom/flows/b2b_sales/gsp_handler.py` (SI create + submit + mint), `ecommerce_super/easyecom/flows/b2b_sales/gsp_auth.py` (Basic + Bearer validation)
- **Related design:** `docs/SPEC.md` §11.5.1 (Custom GSP architecture)
- **User guide:** `process/primers/GUIDE_custom_gsp_invoice_flow.md` (internal FDE walkthrough)
- **Contract-test fixtures:** `tests/contract/gsp/fixtures/` (gh#151; canonical examples)
- **Change history:** `CHANGELOG.md` (search for §11.5.1, gh#130, gh#142, gh#166, gh#206, gh#214, gh#218)

---

**Last verified against production:** `mmpl16.frappe.cloud`, 2026-07-16.
**Doc owner:** methodology team + on-call FDE.

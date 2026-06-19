# §10 STN — mmpl16 Live Reference

Concrete record of every entry created and every configuration tweak
made on `mmpl16.frappe.cloud` to land a clean end-to-end §10 STN
round-trip for all four branches on 2026-06-19. Companion to the
generic `section_10_stn_user_manual.md` — use this when you want
to inspect or replay the exact entries.

All URLs below assume `https://mmpl16.frappe.cloud`.

---

## Quick-reference: the four DNs

| Branch | DN | Source warehouse | Target warehouse | Item | Outcome |
|---|---|---|---|---|---|
| **PO** | [DL-260552](/app/delivery-note/DL-260552) | Paota Old Showroom - MMPL (✗ EE) | Fornt Back Factory - MMPL (✓ EE) | FG20072 | `EE-Pushed`, ee_po_id **2018477** |
| **B2B** | [DL-260550](/app/delivery-note/DL-260550), [DL-260551](/app/delivery-note/DL-260551) | Fornt Back Factory - MMPL (✓ EE) | Paota Old Showroom - MMPL (✗ EE) | (existing items) | `EE-Pushed`, ee_doctype B2B |
| **STN** | [DL-260559](/app/delivery-note/DL-260559) | Fornt Back Factory - MMPL (✓ EE) | Main Back Factory - MMPL (✓ EE) | FG00140 | `EE-Pushed`, ee_doctype STN |
| **Inert** | [DL-260558](/app/delivery-note/DL-260558) | Paota Old Showroom - MMPL (✗ EE) | Paota New Showroom - MMPL (✗ EE) | FG06328-1 | No Transfer Map row (correct) |

### Transfer Map entries

- [ECS-XFER-DL-260552](/app/easyecom-transfer-map/ECS-XFER-DL-260552) — PO
- [ECS-XFER-DL-260550](/app/easyecom-transfer-map/ECS-XFER-DL-260550), [ECS-XFER-DL-260551](/app/easyecom-transfer-map/ECS-XFER-DL-260551) — B2B
- [ECS-XFER-DL-260559](/app/easyecom-transfer-map/ECS-XFER-DL-260559) — STN
- No row for DL-260558 (Inert branch = no Transfer Map created)

### Relevant EE API Call rows

The full request_payload + response_payload are preserved on each
of these (filter the API Call list by `endpoint LIKE '%CreatePo%'`
or `endpoint LIKE '%createOrder%'`):

- `ECS-AC-2026-06-19-00124828` — `/WMS/Cart/CreatePurchaseOrder` 200 (DL-260552 PO push)
- `ECS-AC-2026-06-19-00124827` — `/WMS/Cart/CreatePurchaseOrder` 200 (DL-260553 PO push, sibling)
- `ECS-AC-2026-06-19-00124836–838` — `/webhook/v2/createOrder` 200 × 3 (B2B + STN pushes)
- `ECS-AC-2026-06-19-00124832–833` — `/Wholesale/CreateCustomer` 200 × 2 (R251844 push retries)
- `ECS-AC-2026-06-19-00124824` — `/wms/CreateVendor` 200 (V26073 push)
- `ECS-AC-2026-06-19-00124820` — `/wms/CreateVendor` 400 *(V26060 phantom duplicate — preserved as evidence)*

---

## Internal parties created/fixed

### Customer [R251844](/app/customer/R251844) — `Modern Marwar Private Limited` (Internal Customer)

Pre-existing record (created 2026-05-09 by `govind@eternaltechs.in`).
Changes made on 2026-06-19:

| Field | Final value | Why |
|---|---|---|
| `customer_primary_contact` | `Modern Marwar` | Original primary contact `Modern Marwar Private Limited-R251844` had empty mobile/email and India Compliance's primary-contact sync kept resetting `mobile_no` to empty. Swap to the contact that already had `mobile_no=9214221149` + `email_id=dsk@modernmarwar.com` made R251844.mobile_no land correctly. |
| `email_id` | `dsk@modernmarwar.com` *(auto-derived from primary contact)* | needed for §8e CreateCustomer gate |
| `mobile_no` | `9214221149` *(auto-derived)* | same |

EE-side: pushed successfully on 2nd attempt → **EE customerId `286603`**, Customer Map status flipped from `Flagged-Not-Created` to `Mapped`.

### Supplier [V26060](/app/supplier/V26060) — `Modern Marwar Pvt Ltd test`

The original Internal Supplier (created 2026-05-04 by `Administrator`).

| Field | Final value | Why |
|---|---|---|
| `is_internal_supplier` | **0** | EE-side had a phantom "V26060" vendor that `/wms/V2/getVendors` couldn't surface but CreateVendor refused as duplicate. Disabling the internal-supplier flag stopped §10's `_find_internal_supplier` from picking it. The flag is reversible if you ever want to use V26060 again. |

### Supplier [V26073](/app/supplier/V26073) — `Internal Supplier - Modern Marwar Private Limited`

**Brand new, created 2026-06-19 to replace V26060.** Inserted directly via API (bypassing the PR #70 bootstrap helper because the helper doesn't currently set `payment_terms` and India Compliance requires it).

| Field | Value |
|---|---|
| `supplier_name` | Internal Supplier - Modern Marwar Private Limited |
| `is_internal_supplier` | 1 |
| `represents_company` | Modern Marwar Private Limited |
| `supplier_group` | All Supplier Groups |
| `supplier_type` | Company |
| `gst_category` | Registered Regular |
| `gstin` | 08AAMCM6783B1Z6 |
| `default_currency` | INR |
| `payment_terms` | **Cash** (required to satisfy India Compliance's mandatory gate) |
| `email_id` | internal-supplier-mmpl@internal-transfers.local *(placeholder; edit if you want real)* |
| `mobile_no` | 9999900000 *(placeholder; edit if you want real)* |
| companies (Allowed To Transact With) | Modern Marwar Private Limited |

EE-side: pushed successfully on first attempt → **EE vendor_id `V26073`, c_id `286601`**, Supplier Map status `Mapped`.

### Linked Addresses for V26073

Both created 2026-06-19 specifically for V26073:

- [`Internal Supplier MMPL (Billing)-Billing`](/app/address) — Type Billing, Jodhpur 342001, linked Supplier V26073
- [`Internal Supplier MMPL (Shipping)-Shipping`](/app/address) — Type Shipping, Jodhpur 342001, linked Supplier V26073

---

## Address Dynamic Link rows added on 2026-06-19

§10's `section10_before_save` auto-picks Addresses by Warehouse link, then ERPNext validates each Address belongs to the Customer (buyer side) or Company (seller side). Six additive rows were added across three existing Addresses (no value changes, only Dynamic Link inserts):

| Address | Link added | For which DN |
|---|---|---|
| `Demo-Billing` | Customer **R251844** | DL-260552 PO branch billing |
| `Demo-Billing` | Company **Modern Marwar Private Limited** | DL-260555/DL-260556 (B2B/STN) dispatch — Demo-Billing already had Warehouse Fornt Back Factory link |
| `Paota New Warehouse-Billing` | Customer **R251844** | DL-260555 B2B billing |
| `Paota New Warehouse-Billing` | Warehouse **Paota New Showroom - MMPL** | DL-260558 Inert (reused same address for the new target) |
| `Main Back Factory - MMPL-Billing` | Customer **R251844** | DL-260559 STN billing |
| `Main Back Factory - MMPL-Billing` | Company **Modern Marwar Private Limited** | DL-260559 STN dispatch — also linked to Main Back Factory warehouse |

The `Paota New Warehouse-Billing` already had `Company: Modern Marwar Private Limited` from a prior session.

### How to inspect any of these in the UI

1. Open the Address record (e.g. [Demo-Billing](/app/address/Demo-Billing)).
2. Scroll to the **Reference** child table.
3. Confirm rows: `Customer / R251844`, `Warehouse / Fornt Back Factory - MMPL`, `Company / Modern Marwar Private Limited`.

---

## Company configuration

[`Company / Modern Marwar Private Limited`](/app/company/Modern%20Marwar%20Private%20Limited):

| Field | Before | After (set on 2026-06-19 via UI) |
|---|---|---|
| `gstin` | null | **08AAMCM6783B1Z6** |
| `gst_category` | Unregistered | **Registered Regular** |

These were set via the Company form UI (not API).

---

## Custom Field rescues via `run_audit`

Run on mmpl16 to recover from gh#48 silent-no-op patches:

```
GET /api/method/ecommerce_super.easyecom.install.custom_field_verify.run_audit
```

Rescued the following fields (defined in source, never created on mmpl16):

| DocType | Field |
|---|---|
| Address | ecs_ee_location |
| Delivery Note | ecs_is_section10_transfer |
| Delivery Note | ecs_section10_transfer_from_warehouse |
| Delivery Note | ecs_section10_transfer_to_warehouse |

After the rescue, the **Is Internal Transfer** checkbox + the Transfer From/To Warehouse fields started rendering on the DN form. The "Modern Marwar - Not Permitted" 403 was the side-effect of `Address.ecs_ee_location` being missing — once rescued, the §10 bootstrap buttons stopped crashing.

---

## End-to-end replay sequence (for the next time you need it)

If you needed to re-create everything from scratch on a fresh
deployment, the order is:

1. **Custom Field rescue** — hit `run_audit` URL.
2. **Company GSTIN + gst_category** — set on Company form.
3. **Internal Customer bootstrap** — via EE Company Settings form button OR via direct API. Push to EE. If push flags on mobile/email, swap `customer_primary_contact` to an existing real Contact.
4. **Internal Supplier bootstrap** — same form. Push to EE.
   - If CreateVendor fails on `Vendor code already exists!` (HTTP 400), disable the supplier's `is_internal_supplier` flag and bootstrap a fresh one with a different docname.
   - If insert fails on `payment_terms` mandatory, set `payment_terms` (e.g., `Cash`) on the Supplier before pushing.
5. **Address Dynamic Links** — for each (source, target) warehouse pair you'll use:
   - Add `Customer: <Internal Customer>` to the Address linked to the **target** Warehouse.
   - Add `Company: <Company>` to the Address linked to the **source** Warehouse.
6. **Items mapped to EE** — pick items with status `Mapped` or `Created-Flagged` on `EasyEcom Item Map` (and not batch/serial tracked unless you'll provide those).
7. **Submit the DN.** Branch chip in the UI confirms which branch will fire.
8. **Verify** via the Transfer Map status + the new `EasyEcom API Call` row.
9. **Recover Drift** via:
   ```
   POST /api/method/ecommerce_super.easyecom.flows.transfer_push.push_all_pending_transfers?inline=1
   ```

---

## Inspection cheat sheet

Useful URL filters for the mmpl16 desk:

- **All §10 Transfer Maps**: [/app/easyecom-transfer-map](/app/easyecom-transfer-map)
- **All today's EE API Calls**: [/app/easyecom-api-call?modified=2026-06-19](/app/easyecom-api-call?modified=2026-06-19)
- **Customer Map for R251844**: [/app/easyecom-customer-map?erpnext_name=R251844](/app/easyecom-customer-map?erpnext_name=R251844)
- **Supplier Map for V26073**: [/app/easyecom-supplier-map?erpnext_name=V26073](/app/easyecom-supplier-map?erpnext_name=V26073)
- **Internal Customers**: [/app/customer?is_internal_customer=1](/app/customer?is_internal_customer=1)
- **Internal Suppliers**: [/app/supplier?is_internal_supplier=1](/app/supplier?is_internal_supplier=1)
- **EE-mapped Warehouses**: filter `Warehouse` list by `ecs_ee_location_label` not empty.

# 8f — Supplier (§8.3) — Build Packet

*Third and final entity-sync master, closes §8. Build stage-by-stage; each green + committed (local) + reviewed before next. Mirrors the 8e packet. Grounded in real Harmony getVendors / CreateVendor / UpdateVendor payloads.*

> Single-writer rule. No real EE writes during dev/test except Harmony (disposable). Mirror 8d/8e patterns where the packet says so.

## The model (settled)

One population, **bidirectional**, two-phase flip (mirror 8e). EE master is `/wms/V2/getVendors`. **Foreign suppliers supported (not skipped).**

**Two-identifier split (the key modeling point, unlike 8e's single id):**
- **Read/pull join key:** `vendor_c_id` (e.g. 166334)
- **Write/push key:** `vendor_id` (= `vendor_code`, e.g. "145") — CreateVendor returns it, UpdateVendor consumes it as `vendorId`
- Map stores BOTH, NOT assumed equal. PO/STN flows (§9/§10) resolve ERPNext Supplier → EE write key via this map.
- **OPEN: UpdateVendor response returns `data.vendorId: 58614` — a DIFFERENT number than the 145 it consumed.** Confirm live what 58614 is (likely the vendor_c_id). If so, that's how post-create c_id is captured.

**Lifecycle: vendors HAVE an `active` flag** (unlike customers) — pull-side lifecycle applies. Check live for a deactivate endpoint (push-side).

**Delta-pull supported:** `created_after` / `updated_after` / `updated_before` params (unlike customer's flat full-pull) → scheduled delta pull with a watermark.

## Contract (grounded in real payloads)

**Read** (`getVendors`, flat data[] + nextUrl): vendor_name, vendor_c_id, vendor_code, firstname/lastname, email, contact_number, **active**, tax_identification_number (GSTIN, dirty), **pan** (dirty), currency_code, address{dispatch, billing} — **may be `[]` empty array, not object** (handle both). Plus prep_days/freight_forwarding_days/shipment_Intransit_days/warehouse_checkin_time (lead-times), paymentTerm/deliveryTerm, dl_number/fssai_number/msme_number (licenses, mostly NA).

**Create** (`/wms/CreateVendor`): mandatory emailId, **state (NAME)**, country, currency, zip, **taxIdentificationNumber, PAN**. Optional companyName, firstName, lastName, vendorCode, contactNumber, street, City. Single address. Returns `data.vendor_id` (= vendor_code echoed). NO password (vendors aren't logins).

**Update** (`/wms/UpdateVendor`): keys `vendorId` (= create-returned vendor_id); sparse; **state as NAME**; daysToPrep/daysToShip writable. Returns `data.vendorId: 58614` (confirm what this is).

**State NAME everywhere on push** (read/create/update all use names) — NO name→id resolution needed for push, unlike customer. Resolver still used for pull pincode validation.

**Tax: TWO fields — GSTIN + PAN, country-aware:**
- Indian supplier → IC validates GSTIN + PAN; bad → FNC (mirror 8e's hard-throw→FNC). PAN dirty in sample ("ABCDE1234" = 9 chars).
- Foreign supplier → gst_category "Overseas", no GSTIN validation.

## Stages

**Stage 1 — Substrate:** EasyEcom Supplier Map (autoname ECS-SUPP-{vendor_c_id}; store vendor_c_id [read key, unique] + vendor_id [write key] separately; Dynamic Link → Supplier; status enum; reuse the drift/exclude child DocTypes — **and do the rename now**: EasyEcom Item Map Drift/Exclude Field → EasyEcom Drift/Exclude Field, one-shot migration, 3rd consumer justifies it). supplier_master_mode flag + independent flip. Inventory: stale supplier ruleset? IC Supplier GSTIN+PAN validation behaviour.

**Stage 2 — Foundational lookups (EAGER multi-country):** Reuse 8e's EasyEcom Country/State + resolvers. **EAGER: loop ALL countries from getCountries → getStates per country → cache every country's states at setup.** ~247 calls once; push resolves from cache, never mid-flow. (Already partly built in 8e — extend to eager-all-countries.)

**Stage 3 — Pull:** New EasyEcom-Supplier-Pull ruleset vs real getVendors. Flat list (verify nextUrl pagination). Map-row-only matching (no natural key — same dirty-data reasoning as customer). Create Supplier + addresses (handle `[]` empty-array address). Country-aware GST gating: Indian→GSTIN+PAN FNC-on-bad; foreign→Overseas. active:0→disabled (lifecycle pull). Delta watermark (updated_after). Sync Record per supplier.

**Stage 4 — Push (create + update):** Separate EasyEcom-Supplier-Push ruleset. Create (state NAME, GSTIN+PAN, no password) → write returned vendor_id to map. Update (sparse, vendorId key, state NAME) — **confirm the 58614 response id, capture vendor_c_id**. Lead-time fields (daysToPrep/daysToShip) pushable. Triggers: individual (auto_push_suppliers_on_save default-OFF + ping-pong guard) + batch sweep.

**Stage 5 — Lifecycle/flip/drift:** Lifecycle: pull active:0→disabled; push-side IF deactivate endpoint exists (check — may be N/A like customer). Flip+drift (mirror 8e/8d, Drift persists until Dismiss, drift→Discrepancy, no Accept-EE, field exclusion).

**Stage 6 — UI/workspace:** Supplier Map list colours/filters; 3 number cards (Suppliers in Drift/Created-Flagged/FNC) into the §17 worklist row; buttons wired; delta-pull cron (vendors HAVE updated_after → real delta, not full-pull).

**Parked:** license fields (dl/fssai/msme) + paymentTerm/deliveryTerm → custom fields or later stage.

## OPEN DECISIONS (resolve during stages)
1. **58614 update-response id** — confirm live (Stage 4).
2. getVendors pagination (nextUrl) — verify (Stage 3).
3. Push-side deactivate endpoint exists? (Stage 5).
4. Drift child DocType rename — do in Stage 1.

## Build order
Stage 1 → 2 (eager) → 3 → 4 → 5 → 6. One at a time; review each; live-verify on Harmony. Closeout docs (Part K + section_8f + tracker + SPEC §8.3 + docx) after. This closes §8 masters → then operational flows §9–§13.

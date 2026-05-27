# §8.3 Supplier — spec amendments from live Harmony bring-up

*Apply to SPEC.md §8.3. Single-writer: USER edits SPEC.md; this is the change list.*

## §8.3.x — Scope (NEW / clarify)
- Wholesale vendors (`/wms/V2/getVendors`), bidirectional, two-phase flip (mirror §8.1/§8.2), independent of Item and Customer flips.
- **Indian AND foreign suppliers supported** (foreign not skipped). Country drives tax treatment.

## §8.3.x — Two-identifier model (NEW — the key modeling point)
Unlike Customer's single id, Supplier has two distinct EE ids, stored separately on the Map, NOT equal:
- **`vendor_c_id`** (int, e.g. 282983) — READ key, returned by getVendors.
- **`vendor_id` / `vendor_code`** (string, e.g. "SUP-2026-00060") — WRITE key, sent to create/update.
- **CreateVendor returns BOTH** (`data.vendor_id` write-key echo + `data.vendor_c_id` newly-assigned read key) — captured at once, no read-back needed.
- **UpdateVendor response `data.vendorId` = the READ key (vendor_c_id)** — same field name as the request (write key), opposite role. Captured defensively when Map lacks vendor_c_id.
- PO/STN flows (§9/§10) resolve ERPNext Supplier → EE via Map.ee_vendor_id (write key). **EasyEcom-PO-Push / EasyEcom-GRN-Pull currently map supplier↔vendor_id directly — must repoint to Supplier Map.ee_vendor_id resolution when §9/§10 build.**

## §8.3.x — Create / Update contract (NEW)
- **Create** (`/wms/CreateVendor`): mandatory emailId, state (**NAME** — no id resolution), country, currency, zip, **`taxIdentificationNum`** (SHORT form — EE rejects `taxIdentificationNumber`; doc had the long form), PAN (Indian). **NO password** (vendors aren't portal logins). Returns both ids.
- **Update** (`/wms/UpdateVendor`): keys vendorId (write key); sparse + snapshot; state as NAME; daysToPrep/daysToShip pushable.
- State as NAME everywhere on push (read/create/update) — no name→id resolution (unlike Customer's create stateId-int).
- Pull uses the LONG `tax_identification_number` (that's what getVendors returns) — read/write tax-field naming asymmetric.

## §8.3.x — Tax gating (country-aware, two fields) (NEW)
- Indian + valid GSTIN → IC validates; PAN auto-extracted from gstin[2:12] (not double-mapped). 
- Indian + blank/URP → Unregistered, empty GSTIN.
- Indian + invalid → FNC (held, rollback), flag tagged (ic_gstin_check_digit / ic_gstin_state_code_mismatch / ic_pincode_state_mismatch / ic_pan_format). vendor_id captured on FNC row for retry.
- Foreign → gst_category=Overseas set BEFORE IC validate; GSTIN/PAN optional, dropped from payload (not empty strings — EE rejects those).

## §8.3.x — Addresses (NEW)
getVendors `address.{billing,dispatch}` may be an object OR `[]` empty array (independently per side). Empty → no Address record, no crash (pre-flatten handles both shapes).

## §8.3.x — Lifecycle (NEW)
- Pull-side: vendors have `active` flag → active:0→Supplier.disabled+Map.Disabled; active:1-from-Disabled→restore Mapped. Other statuses sticky.
- Push-side: **N/A — EE has NO vendor deactivate endpoint** (9 candidates 404'd, as with Customer). Supplier disable stays ERPNext-local.

## §8.3.x — Pull cadence (NEW)
getVendors supports `updated_after` (+ created_after, cursor pagination via nextUrl). Scheduled **DELTA pull** 06:00 IST (after Item 05:00, Customer 05:30), high-water watermark supplier_pull_last_updated_at; blank watermark → full pull first run. More efficient than Customer's forced full-pull.

## §8.3.x — State/country lookups (NEW)
Shares §8.2 foundational cache but **EAGER all-countries** (~247, ~100s admin refresh) since foreign suppliers need non-Indian states. Empty-territory responses (HTTP 200 + benign no-data envelope) treated as valid-empty (validator allow-list fix — also corrected the §8.2 sweep).

## §8.3.8 (drift) — confirmation
Comparable: supplier_name/gstin/pan/email_id/mobile_no/default_currency + billing/dispatch street/city/pincode/state/country (side-prefixed). Excludes internal ids. Drift→Discrepancy. Dismiss / Push-ERPNext→EE; no Accept-EE. Drift persists until Dismiss (no auto-heal, §8d/§8e parity).

## §8.3.x — Drift child DocType rename (cross-cutting)
EasyEcom Item Map Drift Field / Exclude Field → **EasyEcom Drift Field / Exclude Field** (entity-agnostic; 3rd consumer). DocTypes + controller classes renamed in lockstep (Frappe orphan-sweep would nuke otherwise); Item + Customer + Supplier maps repointed; 14 existing drift rows preserved.

## §8.3.x — Parked
License fields (dl/fssai/msme), paymentTerm/deliveryTerm → custom fields / later stage.

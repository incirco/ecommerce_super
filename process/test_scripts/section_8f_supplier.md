# Section 8f — Supplier (Wholesale Vendor sync) — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers the sixth and final master: the **EasyEcom Supplier Map** (wholesale vendors, bidirectional, Indian + foreign), the two-identifier model, pull/push/drift, and the eager state/country lookups. Derived from `docs/SPEC.md` §8.3 and the 8f packet.

> **First time?** Read `HOW_TO_RUN_FDE_TESTS.md`, then the masters primer (`../primers/FDE_PRIMER_section_8_masters.md`, Part K).

> **The model in one line.** EE's `/wms/getVendors` holds wholesale vendors; you sync them bidirectionally with the same flip as Item/Customer. Two EE ids (vendor_c_id read-key, vendor_id write-key) — both stored, not equal. Foreign suppliers supported.

> **Status:** live-verified against the Harmony sandbox (disposable). Push verification for a real client is mock-only.

> **Scope:** push-side lifecycle is N/A (EE has no vendor deactivate endpoint). License/payment-term fields parked.

**Build under test:** commit / branch ____________ · **Deployed to:** ____________ · **Tester:** ____________ · **Date:** ____________

---

## Section 1 — Foundational lookups (eager all-country)

### 1.1 Refresh states/countries (eager)
**Do:** EasyEcom Account → "Refresh States/Countries".
**Confirm:** All ~247 countries cached + their states (thousands of rows). Takes ~100s (admin action, one-time). Re-run idempotent. A country with no subdivisions caches as empty (not an error).
**Good:** Foreign states resolvable (e.g. Abruzzo under Italy), cross-country scoped (Abruzzo doesn't resolve under India).

---

## Section 2 — Pull (onboarding)

### 2.1 Basic pull + two-id capture
**Do:** "Discover Suppliers".
**Confirm:** Vendors create ERPNext Suppliers (Company) + Billing/Shipping Addresses + Supplier Map rows. Each map row carries BOTH `ee_vendor_c_id` (read key) and `ee_vendor_id` (write key) — distinct values.
**Good:** Suppliers created, both ids stored.

### 2.2 No-natural-key matching
**Confirm:** Each vendor (by vendor_c_id) gets its own map row + Supplier. Duplicate GSTIN/name do NOT collapse.
**Failure looks like:** Two vendors sharing a GSTIN collapsed onto one Supplier (wrong link — report).

### 2.3 Empty-array address
**Do:** Pull a vendor with `"dispatch": []` or `"billing": []` (the sample has several).
**Confirm:** No crash; that side simply gets no Address record. The other side (if an object) creates normally.

### 2.4 Country-aware GST gating
**Confirm:**
- Indian + valid GSTIN → created; PAN auto-extracted from GSTIN.
- Indian + blank/URP → Unregistered, empty GSTIN.
- Indian + invalid GSTIN/PAN → **Flagged-Not-Created** (held), flag tagged (ic_gstin_check_digit etc.).
- Foreign (Italy/Armenia) → created, `gst_category = Overseas`, GSTIN/PAN optional.

### 2.5 Lifecycle pull
**Confirm:** `active:0` vendor → Supplier disabled + map Disabled. A previously-disabled vendor going `active:1` → restored to Mapped.

### 2.6 Pagination + delta
**Confirm:** Multi-page pull walks the cursor (nextUrl). Re-run resumes from saved cursor. Scheduled pull uses `updated_after` watermark (delta, not full re-fetch).

---

## Section 3 — Push (create + update)

*Reminder: do not live-write to a real client's EE. Inspect payload/map outcome (mocked or disposable sandbox).*

### 3.1 Create
**Confirm:** Push a never-pushed Company supplier → CreateVendor payload: emailId, state (NAME), country, currency, zip, `taxIdentificationNum` (SHORT form — not ...Number), PAN (Indian). NO password. On success, BOTH ids written back to map; status → Mapped.
**Failure looks like:** Missing mandatory → flag-not-pushed (no broken payload).

### 3.2 Foreign create
**Confirm:** Foreign supplier → GSTIN/PAN dropped from payload (not empty strings), state name round-trips.

### 3.3 Update (sparse)
**Confirm:** Editing a mapped supplier pushes only changed fields + write key, keyed on vendor_id, state as NAME. daysToPrep/daysToShip pushable.

### 3.4 Batch sweep
**Do:** "Push All Pending Suppliers".
**Confirm:** Returns immediately with enqueued count. Candidates = Company, not disabled, no map row.

### 3.5 Auto-push gate
**Confirm:** With auto-push OFF (default), saving Suppliers spawns no push jobs. Leave OFF for testing.

---

## Section 4 — Flip & drift

### 4.1 Flip
**Do:** "Flip Suppliers → ERPNext-Mastered", confirm.
**Confirm:** Mode → erpnext_mastered; re-flip refused (one-way); non-FDE refused; **independent of Item AND Customer flips**.

### 4.2 Post-flip: change → Drift, not overwrite
**Confirm:** EE-side change to a mapped Supplier → ERPNext NOT changed; map → Drift; drift table records differing fields (supplier-level + billing/dispatch-prefixed).
**Failure looks like:** ERPNext overwritten (critical — report).

### 4.3 New EE vendor post-flip → Drift, not created
### 4.4 Quiet re-pull: Mapped stays Mapped; Drift persists until Dismiss (clears diff rows, keeps status — no auto-heal)
### 4.5 Resolution: Dismiss (→Mapped, ERPNext untouched) + Push ERPNext→EE; NO "Accept EE Value"
### 4.6 Field exclusion: excluded field (top-level or billing/dispatch-prefixed) doesn't trigger Drift

---

## Section 5 — Operational surface

### 5.1 Sync Records
**Confirm:** Each pull/push writes a Sync Record (per supplier × direction). Success → Success; failure → Failed; **drift → Discrepancy (NOT Failed)**.

### 5.2 Workspace + sidebar
**Confirm:** Workspace shows Suppliers in Drift / Created-Flagged / FNC counts (clickable). **Sidebar matches the workspace** — every Supplier worklist sidebar entry is clickable and lands on a filtered list with `?status=...` (this was a recurring bug — verify it works). Supplier Map list shows status colours + filters.

### 5.3 Scheduled delta pull
**Confirm:** Daily delta pull (06:00 IST) runs, uses the updated_after watermark, phase-aware.

---

## What "passing" means

§8f passes when: vendors pull into Suppliers + addresses with BOTH ids captured; duplicate tax/name create separate Suppliers (never wrong-link); empty-array addresses don't crash; Indian-bad-GST held (FNC), foreign→Overseas created; push create/update produce well-formed payloads (short-form tax field, no password, both ids written back); flip is one-way and independent of the other two masters; post-flip drift detects-but-never-overwrites; sidebar matches workspace and worklist links are clickable; every op surfaces as a Sync Record (drift → Discrepancy). Most important: **2.2 (no wrong-link)**, **2.4 (country-aware gating)**, and **4.2 (drift never overwrites)**.

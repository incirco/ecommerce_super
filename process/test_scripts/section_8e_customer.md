# Section 8e — Customer (Wholesale B2B sync) — Test Script

For an ERPNext-fluent FDE testing on Frappe Cloud staging. Covers the fifth master: the **EasyEcom Customer Map** (wholesale B2B, bidirectional), the two-phase flip, the pull/push/drift behaviour, and the state/country foundational lookups. Derived from `docs/SPEC.md` §8.2 and the 8e packet.

> **First time?** Read `HOW_TO_RUN_FDE_TESTS.md`, then the masters primer (`../primers/FDE_PRIMER_section_8_masters.md`, Part J).

> **The model in one line.** EE's `/Wholesale` API holds wholesale B2B customers; you sync them bidirectionally with the same onboarding→ERPNext-mastered flip as Item. Marketplace anonymous buyers are NOT here — they're §11/§12.

> **Status:** live-verified against the Harmony sandbox (disposable). Push verification for a real client is mock-only.

> **Scope note:** Lifecycle sync is N/A (EE exposes no customer activate/deactivate and no active flag). Pricing/discount sync is parked. Order-driven B2B-buyer reuse is §11/§12.

**Build under test:** commit / branch ____________ · **Deployed to:** ____________ · **Tester:** ____________ · **Date:** ____________

---

## Section 1 — Foundational lookups (states / countries)

### 1.1 Refresh states/countries
**Do:** EasyEcom Account → "Refresh States/Countries".
**Confirm:** EasyEcom Country + EasyEcom State records populate (India = country id 1, 37+ states). Re-run → idempotent (refresh in place, no dupes).
**Good:** Cache populated; re-run doesn't duplicate.

---

## Section 2 — Pull (onboarding phase)

### 2.1 Basic pull
**Do:** "Discover Customers".
**Confirm:** Wholesale B2B customers create ERPNext Customers (type Company) + Billing/Shipping Address records + Customer Map rows. Healthy → Mapped.

### 2.2 No-natural-key matching (the central design point)
**Do:** Pull data containing duplicate gstNum / companyname (the Harmony sandbox has these).
**Confirm:** Each EE customer (by c_id) gets its OWN Customer Map row and its OWN Customer — duplicates are NOT collapsed into one.
**Good:** Distinct partners stay distinct.
**Failure looks like:** Two EE customers sharing a GSTIN collapsed onto one Customer (a wrong link — report).

### 2.3 GST gating — held vs created
**Confirm:**
- Invalid GSTIN (bad check digit) → **Flagged-Not-Created**, no Customer, flag names `ic_gstin_check_digit`.
- GSTIN state code ≠ address state → **FNC**, flag `ic_gstin_state_code_mismatch`.
- Pincode prefix ≠ state → **FNC**, flag `ic_pincode_state_mismatch`.
- URP / unregistered → Customer created, `gst_category = Unregistered`, empty gstin, **Mapped**.
**Good:** Bad GST data held (no partial/orphan rows); URP flows through clean.

### 2.4 Re-pull is a no-op
**Do:** Discover Customers twice.
**Confirm:** Existing map rows reused; no duplicate Customers.

---

## Section 3 — Push (create + update)

*Reminder: do not live-write to a real client's EE. These steps inspect payload/map outcome (mocked or disposable sandbox only).*

### 3.1 Create
**Confirm (mocked/sandbox):** Push a never-pushed Company customer → CreateCustomer payload carries the mandatory set (companyName, email, random password, country, billingStateId/dispatchStateId resolved from state name, billingPostalCode, currency, taxIdentificationNumber or "URP", contactNumber). On success, the EE-returned id is written to the map row; status → Mapped.
**Good:** Well-formed payload; id written back.
**Failure looks like:** Missing contactNumber (EE rejects), or unresolvable state, should flag-not-pushed — not send a broken payload.

### 3.2 Update (sparse)
**Confirm:** Editing a mapped customer pushes only changed fields, keyed on the EE customer id, state as NAME, no password.

### 3.3 Batch sweep
**Do:** "Push All Pending Customers".
**Confirm:** Returns immediately with an enqueued count. Candidates = Company, not disabled, has email, no map row. FNC and already-mapped excluded.

### 3.4 Auto-push gate
**Do:** With auto-push OFF (default), save Customers.
**Confirm:** No new push queue jobs from saves. Leave OFF for testing.

---

## Section 4 — Flip & drift

### 4.1 Flip
**Do:** "Flip Customers → ERPNext-Mastered", confirm.
**Confirm:** Mode → erpnext_mastered; re-flip refused (one-way); non-FDE refused; **independent of the Item flip**.

### 4.2 Post-flip: change → Drift, not overwrite
**Do:** After flip, change a field on an EE-side customer, re-pull.
**Confirm:** ERPNext Customer NOT changed; Map row → **Drift**; drift table records the differing fields (customer-level and/or billing/dispatch-prefixed address fields).
**Failure looks like:** ERPNext overwritten by the EE value (critical — report).

### 4.3 New EE customer post-flip → Drift, not created
**Confirm:** No Customer created; Drift row records it.

### 4.4 Quiet re-pull doesn't flap; Drift persists
**Confirm:** A Mapped row with no change stays Mapped. A Drift row that becomes clean → stale diff rows cleared BUT status stays Drift until you Dismiss (matches Item — no auto-heal).

### 4.5 Resolution actions
**Confirm:**
- **Dismiss Drift** → Mapped, drift table cleared, ERPNext untouched.
- **Push ERPNext → EE** → re-asserts via UpdateCustomer.
- **No "Accept EE Value"** action exists.

### 4.6 Field-level exclusion
**Do:** Add a field to the customer's drift exclude list, change it on EE, re-pull.
**Confirm:** That field doesn't trigger Drift.

---

## Section 5 — Operational surface

### 5.1 Sync Records
**Confirm:** Each pull/push writes a Sync Record (one per customer × direction). Pull/push success → Success; failure → Failed; **drift → Discrepancy (NOT Failed)**.

### 5.2 Workspace counts + list triage
**Confirm:** Workspace shows Customers in Drift / Created-Flagged / Flagged-Not-Created counts (clickable). Customer Map list shows status colours + sidebar filters.

### 5.3 Scheduled pull
**Confirm:** Daily full pull (05:30 IST) runs; phase-aware (onboarding creates, erpnext_mastered drift-detects). Note: full pull, not delta — EE exposes no updated_after.

---

## What "passing" means

§8e passes when: wholesale B2B customers pull into Customers + addresses; duplicate gstNum/companyname create separate Customers (never wrong-link); invalid/mismatched GST data is held (FNC), URP flows through; push create/update produce well-formed payloads with id writeback (verified mocked/sandbox); flip switches to ERPNext-mastered one-way and independent of Item; post-flip drift detects-but-never-overwrites; and every op surfaces as a Sync Record (drift → Discrepancy). The most important checks: **2.2 (no wrong-link on dirty dupes)** and **4.2 (drift never overwrites ERPNext)**.

# §8.2 Customer — spec amendments from live Harmony bring-up

*Apply to SPEC.md §8.2. Single-writer rule: USER edits SPEC.md; this is the change list.*

## §8.2.x — Scope (NEW / clarify)
- This master = **wholesale B2B customers only** (`/Wholesale/v2/UserManagement?type=b2b`). Marketplace anonymous end-buyers are OUT of scope → §11/§12 order flows (pseudo-customer pool).
- One population, bidirectional, two-phase flip (onboarding → erpnext_mastered), mirroring §8.1 Item. Flip is independent of the Item flip.

## §8.2.x — EE identifier semantics (NEW)
- Read (`getCustomers`) join key = `c_id`.
- **CreateCustomer returns `data.c_id`** (NOT `data.customerId` — packet assumption was inverted; confirmed live). Same value consumed by UpdateCustomer under the `customerId` key. Single identifier, two field names. Map stores both fields; reader uses `c_id` with `customerId` fallback.

## §8.2.x — Create / Update contract (NEW)
- **Create** (`/Wholesale/CreateCustomer`): mandatory companyName, email, **password** (random string — EE portal login is a dummy nobody uses), country (name), billingStateId + dispatchStateId (**int**, resolved name→id via cached getStates), billingPostalCode, currency, taxIdentificationNumber (GSTIN or "URP"), **contactNumber** (REQUIRED — EE rejects "Missing contact number"; doc listed it optional but it is not).
- **Update** (`/Wholesale/UpdateCustomer`): keys customerId; sparse; state as **name** (billingState/dispatchState); NO password. EE is inconsistent: stateId-int on create, state-name on update — handle both at the wire boundary.

## §8.2.x — Content gating (India Compliance, stricter than anticipated) (NEW)
Three GST-place-of-supply validators hard-throw at insert → whole Customer **Flagged-Not-Created (held)**, partial inserts rolled back, no degraded data:
- `ic_gstin_check_digit` — invalid GSTIN format/checksum
- `ic_gstin_state_code_mismatch` — GSTIN 2-digit state code ≠ address state
- `ic_pincode_state_mismatch` — pincode prefix ≠ state
URP / unregistered → `gst_category = Unregistered` + empty gstin → created (Mapped). Only NON-tax-relevant dirt may be Created-Flagged.

## §8.2.x — Matching (NEW)
**Map-row-only; NO natural-key match.** gstNum/companyname/email are heavily duplicated in real EE data (one GSTIN seen 7× across distinct partners). Map row exists → use it; else → create new Customer + map row. Never wrongly link > never duplicate.

## §8.2.x — Lifecycle: N/A (NEW)
EE exposes NO customer active/disabled signal on read and NO deactivate endpoint. Customer enable/disable is ERPNext-local; no lifecycle sync either direction (unlike §8.1 Item's ActivateDeactivateProduct).

## §8.2.x — Pull cadence (NEW)
`getCustomers` is a flat full list — NO updated_after/cursor/watermark. Scheduled pull = daily FULL pull (05:30 IST, staggered after Item 05:00). Acceptable at wholesale cardinality; thousands would need an EE incremental endpoint or webhook sync.

## §8.2.8 (drift) — confirmation
Flip → drift → dismiss confirmed. Comparable: customer_name/email_id/mobile_no/gstin/default_currency + billing/dispatch street/city/pincode/state/country (side-prefixed). Excludes internal ids. Drift Sync Record = **Discrepancy** (not Failed). No "Accept EE Value". Drift persists until FDE Dismiss (clean re-pull clears diff rows but not status — matches §8.1).

## §8.2.x — State/country foundational lookups (NEW)
getCountries + getStates discover-and-cache (EasyEcom Country / EasyEcom State DocTypes), foundational §7.7 (no Sync Records). Resolvers: resolve_country, resolve_state (name→id, largest-id-wins on same-name dupe — e.g. Daman & Diu legacy 34/35 vs merged 3848), validate_pincode_state (zip-range, soft enum, never throws). Note: foreign-country state caching needed for §8.3 Supplier (foreign vendors), not §8.2 (Indian B2B).

## §8.2.x — Out of scope / parked
Pricing & discounts (b2bDiscountScheme, pricingGroupCode, invoiceSeriesCode, salesmanUserId, customerAttributes) — later stage. Marketplace anon buyers — §11/§12.

## §8.2.x — Dup-name resilience on create (NEW, commit `4108048`)
Real EE data has heavily-duplicated customer display names (per the matching policy — map-row-only, no natural-key match). When the pull creates a new ERPNext Customer for a never-seen `c_id`, the desired display name may already exist on a *distinct* ERPNext Customer (linked to a different `c_id`). The create path wraps `frappe.get_doc({...}).insert()` and on `DuplicateEntryError` appends a short disambiguator (`-2`, `-3`, …) and retries — bounded at 5 attempts. The final name is recorded on the Customer Map row for traceability. Identity is keyed on the Map row (`c_id`), so the disambiguator is cosmetic, not identity-bearing.

## §8.2.x — Discover-Customers async-by-default (NEW, commit `9280d58`)
The Discover-Customers desk button (Account form + top-bar dropdown) **enqueues into the `long` queue (3600s timeout) via `frappe.enqueue` and returns immediately with the RQ job_id**. The synchronous path tripped Frappe's 120s desk-whitelist budget on real-client catalogues (>2000 customers); the server-side pull continued in the worker but the browser had already disconnected, surfacing a misleading "(network or permission)" error to the FDE. Async-by-default is now the only pathway; progress visible via Account-form refresh + Customer Map list.

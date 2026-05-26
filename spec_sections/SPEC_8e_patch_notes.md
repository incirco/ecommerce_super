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

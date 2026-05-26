# 8e — Customer (§8.2) — Build Packet

*Second entity-sync master after Item. Build stage-by-stage; each stage green + committed (local) + reviewed before the next. Same discipline as the 8d packet. Grounded in the real Harmony `getCustomers` / `CreateCustomer` / `UpdateCustomer` / `getStates` / `getCountries` payloads.*

> **Single-writer rule stands.** Claude Code reads the spec; the USER commits. Stop-and-report on any contradiction with committed code or CLAUDE.md rather than silently reconciling.
>
> **No real EE writes during dev/test** unless against the disposable Harmony sandbox, the same allowance used for 8d. Push verification for a real client is mock-only.

---

## The model (settled in design conversation)

**One population — B2B/wholesale, bidirectional, two-phase flip.** EE's customer master is `/Wholesale/v2/UserManagement` (`type=b2b`). It returns *wholesale trade partners* (companies you sell to), NOT marketplace end-buyers. Marketplace-anonymous buyers are an **order-flow concern (§11/§12), out of scope for 8e.** One population here.

**Same flip model as Item:** Onboarding (bidirectional, supervised) → ERPNext-mastered (steady state, ERPNext is SoT for customers, EN→EE push authoritative, EE-side edits become drift). The `customer_master_mode` flag mirrors `item_master_mode`.

**Identifier:** `c_id` (read) == `customerId` (write) — same EE customer identifier under two names (confirm live they're identical). Store on the map row; write back on create. This is the cp_id-equivalent.

**EasyEcom Customer Map** — mirrors EasyEcom Item Map. `c_id` ↔ ERPNext **Customer** + its linked **Billing / Shipping Address** records. Direction-agnostic registry, status enum (Mapped / Created-Flagged / Flagged-Not-Created / Drift / Disabled), drift child table + exclude-fields child table (reuse the 8d shapes).

**Sync Records** — 8e is the **second entity-sync flow**; mirror §8d exactly (entity-centric, one per ERPNext-doc × direction, §7.3 state machine, drift → **Discrepancy** not Failed). 8a/8b/8c are foundational §7.7 and correctly don't write them.

---

## Contract (grounded in real payloads)

**Read — `GET /Wholesale/v2/UserManagement?type=b2b`** → `data[]` of:
`c_id`, `companyname`, `pricingGroup` (string|null), `customer_support_email`, `customer_support_contact` (may be ""), `branddescription` (null), `currency_code`, `gstNum`, `company_invoice_group_id` (int|null), billing{Street,City,Zipcode,State(**name**),Country}, dispatch{Street,City,Zipcode,State(**name**),Country}.

**Create — `POST /Wholesale/CreateCustomer`** → returns `data.customerId`. Mandatory: `companyName`, `email`, `password`, `country` (**name**), `billingStateId` (**int**), `billingPostalCode` (int), `currency`, `dispatchStateId` (**int**), `taxIdentificationNumber` (GSTIN; **"URP"** for unregistered). Optional: contactNumber, billing/dispatch Street/City, description, invoiceSeriesCode (int, via getCompanyGroupDetails), pricingGroupCode (int), salesChannel, salesmanUserId, b2bDiscountScheme (JSON), customerAttributes (paymentTerm/deliveryTerm), no_copy_master.

**Update — `POST /Wholesale/UpdateCustomer`** → keys `customerId`; all else optional; state as **name** (`billingState`/`dispatchState`); **no password needed**.

**Lookups (foundational, discover-and-cache, NO Sync Records):**
- `GET /getCountries` → id ↔ country name ↔ code_2/code_3 ↔ default_currency_code. India = id 1.
- `GET /getStates?countryId=N` → id ↔ name, `is_union_territory`, `zip_start_range`/`zip_end_range` (pincode→state validation), `postal_code`. Note dirty/legacy duplicate (Daman & Diu as 34/35 and merged 3848) — handle.

**Key divergences (handle PAYLOAD reality):**
- **State representation differs by call:** read = name; create = **stateId int**; update = name. Push-create resolves name→id via cached getStates; update sends name.
- **GST:** `gstNum`/`taxIdentificationNumber` = GSTIN. India Compliance validates 15-char format. **"URP" escape** for unregistered party — the clean alternative to Item's HSN hard-hold.
- **Dirty state vs pincode:** real data has state/pincode mismatch (Arunachal Pradesh + 560035 Bangalore). The `zip_start_range`/`end_range` lets us cross-check → soft flag (Created-Flagged), not hard block; GST place-of-supply relevant.
- **Password mandatory on create** but the EE portal is a **dummy field nobody uses** → push-create generates a random string. No FDE flag needed (confirmed: no one logs into the EE portal).
- **`customer_support_contact` may be ""**, `branddescription` null, `company_invoice_group_id`/`pricingGroup` null — standard optional/empty handling.

**Out of scope / parked:**
- **Pricing & discounts** (`b2bDiscountScheme`, `pricingGroup`/`pricingGroupCode`, `invoiceSeriesCode`, `salesmanUserId`, `customerAttributes`) → parked to a later stage. Core customer + addresses + GST first.
- Marketplace-anonymous buyers → §11/§12 order flows.

---

## Stages

### Stage 1 — Substrate
- **EasyEcom Customer Map** DocType: autoname `format:ECS-CUST-{ee_c_id}`, `ee_c_id` (unique, reqd, DB-level), Dynamic Link (erpnext_doctype ∈ {Customer} + erpnext_name), `ee_customer_id` (== c_id; the write-side id), status enum, flag_reason, `drift_fields` child table, `ecs_drift_exclude_fields` child table (reuse 8d child DocType shapes or 8e-specific mirrors).
- `customer_master_mode` (onboarding|erpnext_mastered, default onboarding, read-only) on EasyEcom Account + `customer_master_flipped_at` + Flip button + `flip_to_erpnext_mastered_customers` whitelisted endpoint (role-gated, explicit-confirm, one-way, refuses re-flip). Mirror the Item flip exactly.
- Inventory first: report whether a shipped customer ruleset exists; report the India Compliance Customer GSTIN-validation behaviour (the 8e analogue of the HSN-mandatory finding).

### Stage 2 — Foundational lookups (states / countries)
- Discover-and-cache `getCountries` + `getStates` per country. Cache id↔name maps (where: a cache DocType, or on a settings doc — match how channel discovery cached). **Foundational §7.7 — NO Sync Records, NO entity Map rows.** Log as API Calls.
- A `resolve_state(name, country) → stateId` + `resolve_country(name) → name/id` helper, plus a `validate_pincode_state(pincode, stateId) → ok|mismatch` using zip ranges.
- Button on EasyEcom Account: "Refresh States/Countries". Mode-irrelevant (pure reference data).

### Stage 3 — EE→EN pull
- Reconcile a NEW `EasyEcom-Customer-Pull` ruleset against the 4 real `getCustomers` records (don't reuse Item's). §8.0 engine policy.
- Cursor/pagination: confirm whether `getCustomers` paginates (the sample is a flat `data[]` — verify; if no cursor, simple full pull).
- Matching: map row exists → use it; else exact match on a chosen key (gstNum? companyname? — DECISION NEEDED, see below) → auto-map + create row; else create new Customer + Billing/Shipping Address rows. **Never wrongly link > never duplicate** (same principle as Item).
- Create ERPNext **Customer** (customer_type=Company) + **two Address** records (Billing, Shipping) linked. GSTIN → India Compliance `gstin` field; category Registered/Unregistered (URP→Unregistered).
- Content gating: **invalid/missing GSTIN that India Compliance rejects → Flagged-Not-Created (held)** unless URP-eligible; **state/pincode mismatch or dirty optional fields → Created-Flagged** (Customer exists). is_internal_customer etc. parameterized as needed.
- active/disabled lifecycle on pull (if the read exposes a status — verify; the sample has none, so this may be N/A for pull).
- Sync Record per customer (direction=Pull).

### Stage 4 — EN→EE push (create + update)
- SEPARATE `EasyEcom-Customer-Push` ruleset.
- **Create** (`/Wholesale/CreateCustomer`): manufacture mandatory fields — companyName, email, **password=random string** (dummy portal), country name, billingStateId/dispatchStateId via Stage-2 resolver, billingPostalCode, currency, taxIdentificationNumber (GSTIN or "URP"). Missing-mandatory (e.g. no email, unresolvable state) → flag-not-pushed (no broken payload). On success write `customerId` back to map row.
- **Update** (`/Wholesale/UpdateCustomer`): keys customerId; sparse payload + snapshot (parity with Item's sparse-update); state as **name**; no password.
- Triggers: individual push (Customer save/edit, gated by an `auto_push_customers_on_save` checkbox default-OFF + the pull-flag ping-pong guard, mirror Item) + batch onboarding sweep (enqueue via `enqueue_easyecom_job`, return immediately, which-items policy stated).
- Sync Record per push.

### Stage 5 — Lifecycle / flip / drift
- Phase-governed (mirror Item Stage 5). Flip → pull becomes drift-detection; EN→EE push authoritative.
- Drift: post-flip, EE-side change to a mapped customer → Drift status + drift child table, ERPNext NOT overwritten, Sync Record = **Discrepancy**. Dismiss / Push-ERPNext→EE resolution actions; **no "Accept EE Value"**. Field-level exclusion. Quiet re-pull doesn't flap.
- DRIFT_COMPARABLE_FIELDS for customer: companyname, gstNum, addresses (billing/dispatch street/city/state/zip), currency, contact, email. Exclude internal ids (c_id/customerId).

### Stage 6 — UI / workspace
- Buttons per stage (Discover Customers, Push All Pending Customers, individual Push, Flip, Refresh States/Countries). Whitelists role-gated, clean error returns.
- Customer Map list view (status colours, filters). Workspace: add Customer worklist counts (Drift / Created-Flagged / FNC) to the §17 FDE Worklist row; add to the masters status panel.
- Auto-push-customers checkbox default OFF.

### Later (parked) — Pricing & discounts
- b2bDiscountScheme → ERPNext Pricing Rule / Price List; pricingGroup/invoiceSeriesCode/salesmanUserId via getCompanyGroupDetails; customerAttributes → Payment Terms. Separate stage when core sync is proven.

---

## DECISIONS NEEDED before / during build

1. **Pull matching key.** Item used exact `sku==item_code`. Customer has no single obvious natural key — candidates: `gstNum` (but dirty/duplicated in sample — two records share "ABC1523EDR34"), `companyname` (duplicated — two "test customer"), email. **Lean: match on a deterministic key with NO fuzzy fallback; if no safe natural key exists, default to "always create new + map row" and let the FDE dedupe** (never wrongly link). Confirm during Stage 3 against real data.
2. **getCustomers pagination** — verify live whether it cursors or returns all.
3. **c_id == customerId** — confirm identical live.
4. **India Compliance customer GSTIN gating** — confirm whether an invalid GSTIN blocks Customer creation (→ held) or just warns (→ Created-Flagged), and how URP maps to the Unregistered GST category. This is the Stage-3 content-gating hinge.

---

## Build order
Stage 1 substrate → Stage 2 lookups → Stage 3 pull → Stage 4 push → Stage 5 lifecycle/flip/drift → Stage 6 UI → (parked) pricing. One stage at a time; review each. Live-verify against Harmony (disposable) like 8d. Closeout docs (Part J primer + section_8e test script + tracker) after build.

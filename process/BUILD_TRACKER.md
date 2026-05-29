# Build Tracker

The frontier. One row per buildable section, one column per stage of the loop (see `process/PROCESS.md`). Update by hand as a section advances. This tracks **sequencing**, not defects — defects live in GitHub Issues.

**Legend:** ☐ not started · 🔶 in progress · ✅ done · — n/a yet

**Current focus:** _8a Location, 8b Channel, 8c Tax all DONE (built, smoke-tested, FDE docs closed). 8c: Tax Rule Map per (rule,company) + resolver + Test Resolve UI (dry-run via shared pure functions, parity-tested). NEXT: 8d Item (§8.1) — THE BIG ONE. Calls 8c's resolve_and_stamp_tax on real products (tax goes live end-to-end). Must: reconcile the §5 Item ruleset vs real /Products/GetProductMaster (sku not item_code; no stock UOM → default-UOM strategy; product_type normal/variant_parent/combo(sub_products)/child → ERPNext simple/template+variant/bundle/variant — first master with nested child tables); use 8a savepoint helper for the product batch; carry Location+Channel context. PARKED DECISIONS to settle before scoping: (1) identifier-divergence matching ladder (sku/EAN/name priority + how often divergence happens), (2) auto-create vs workflow-gated item creation, (3) mandatory-field gate (HSN/UOM minimum + behaviour when EE omits). Real GetProductMaster payloads already in hand to ground the design._id (Int join key + DB UNIQUE), is_active=active-anywhere, classification workflow (Unclassified → Classified → Active, branch Ignored), reused 8a savepoint helper + workflow pattern. API Call validation widened for the 3rd shape (operational + no company + location_key, for unmapped-location sweeps). NEXT: 8c Tax — standalone, explicit FDE-configured Tax-Rule→Item-Tax-Template mapping (NOT a pull — EE has no tax master API; tax rides the product payload). Open hinge: does the EE order/invoice payload carry COMPUTED tax amounts (→ variable rules reconcile per-order, 8c thin) or only the rule name (→ must model conditional tax)?_isolation.py), back-fill, + trigger surface (Discover Locations button, daily scheduler, new-location notification placeholder pending §18). State-aware company/workflow invariant. Built, 281 green, and SMOKE-TESTED LIVE against sandbox (3 real locations → To Map, is_wms from stockHandle, button-triggered, workflow walked, back-fill sane). Pending commit + push. NEXT: 8b Channel (flat Marketplace list pull) — packet MUST carry the workflow-fixture gotchas noted on the 8b row._

---

## Orientation (approve-only — no build; these set framing & principles)

| Section | Spec ready | Acceptance | Approved | Built | Local test | Deployed | Team test | Live |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 1. Introduction | ✅ | n/a | ✅ 24-May | n/a | n/a | n/a | n/a | n/a |
| 2. Architectural Principles | ✅ | n/a | ✅ 24-May | n/a | n/a | n/a | n/a | n/a |

## Foundation (build first, in this order)

> §3 and §4 are built together as one packet (`foundation_section_3_and_4.md`): §3's client/logging/health depend on §4's log DocTypes, so connection DocTypes → §4 data model → §3 client. §5–§7 follow.

| Section | Spec ready | Acceptance | Approved | Built | Local test | Deployed | Team test | Live |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 3+4. Foundation: Connection Model + Data Model | ✅ | ✅ | ✅ 24-May | ✅ 24-May | ✅ 24-May | 🔄 | 🔄 FDE | ☐ |
| 5. Field Mapping engine | ✅ | ✅ | ✅ 24-May | ✅ 24-May | ✅ 24-May | 🔄 | 🔄 FDE | ☐ |
| 6. Idempotency, Replay, Correlation, Queue (completion — most built in foundation) | ✅ | ✅ | ✅ | ✅ | ☐ | ☐ | ☐ | ☐ |
| 7. The Integration Contract (verify-and-carry; not a build) | ✅ verified | n/a | n/a | n/a | n/a | n/a | n/a | n/a |

## Integrations (each implements the Section 7 contract)

| Section | Spec ready | Acceptance | Approved | Built | Local test | Deployed | Team test | Live |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 8. Master Sync (split into 6 dependency-ordered packets below) | — | — | — | — | — | — | — | — |
| 8a. Location (pull + FDE map; resolution substrate) | ✅ | ✅ | ✅ | ✅ smoke | ☐ | ☐ | ☐ | ☐ |
| ↳ 8a refactored to use the Field Mapping engine (EasyEcom-Location-Pull ruleset) instead of a hardcoded mapper — engine = API-change insurance (§8.0 policy). stockHandle→is_wms_location transform now in the ruleset. §5 path validator relaxed to allow space-bearing keys. Re-pull now preserves existing values when EE omits a field. 309 green. | — | — | — | — | — | — | — | — |
| 8b. Channel (per-location sweep + dedupe + FDE classify) | ✅ | ✅ | ✅ | ✅ smoke | ☐ | ☐ | ☐ | ☐ |
| ↳ 8b packet MUST include a "Workflow-fixture mechanics (learned in 8a)" block: (1) ship each transition twice, once per role — Workflow Transition.allowed is a single Role link, no inheritance; (2) active workflow auto-applies on insert (factories insert in first state + transition, or db.set_value to stamp); (3) test role-cache flush — clear_cache(user) + set_user after granting a custom role; (4) sanitise savepoint names to alphanumeric+underscore (MariaDB rejects dashes). | — | — | — | — | — | — | — | — |
| 8c. Tax (EasyEcom Tax Rule Map → Item Tax Template; resolver; Test Resolve UI) | ✅ | ✅ | ✅ | ✅ smoke | ☐ | ☐ | ☐ | ☐ |
| ↳ NOT a pull (EE has no tax API; tax rides product payload). One map doc per (tax_rule_name, company); taxes child = native Item Tax child holding that company's templates with Min/Max Net Rate slab bands. resolve_and_stamp_tax (8c-owned, 8d-calls) stamps banded rows / reconciles resolved tax_rate vs bands / cess per-product outside map / unmapped (rule,company) auto-creates To-Configure + alerts FDE (no silent default). Workflow To Configure → Configured (gated on non-empty taxes), branch Ignored. **Test Resolve UI**: dry-run preview on the form via shared pure functions (preview_stamp + reconcile_rate) — 8 parity tests pin dry-run==real. ERPNext resolves slab band natively at invoice time (no slab logic in our code). 371 green. | — | — | — | — | — | — | — | — |
| 8d. Item / Product master (first hard master; builds savepoint helper) | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 8e. Customer master (incl. anonymous pseudo-customers) | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 8f. Supplier / Vendor master | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| (Lookups — UOM, Brand, Item Group, Category Map — folded into whichever master first needs them, not a standalone packet) | — | — | — | — | — | — | — | — |
| 9. Buying & Inwarding | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 10. Stock Transfers | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 11. B2B Sales | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 12. B2C / Marketplace Sales | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 13. Returns & Cancellations | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

## Operational surface & rest (later band — build after integrations are stable)

| Section | Spec ready | Acceptance | Approved | Built | Local test | Deployed | Team test | Live |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 14. Multi-Company | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 15. Failure Modes & Recovery | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 16. Performance & Scale | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 17. Operational Surface | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 18. Notifications & Alerts | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 19. Replay & Recovery Tooling | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 20. Schema Drift & Coverage | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 21. SLA Budgets & Tracking | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 22. Cross-Company Operations | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 23. Recon-Aware Alerts | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 24. Morning Brief | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 25. Error Translation Library | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |
| 26. Time Travel & Config Audit | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ | ☐ |

---

## Note on Section 0 — environment

Before Section 3 can be built, the local environment must exist: Frappe bench (v16), a site with ERPNext + India Compliance installed, the `ecommerce_super` app created via `bench new-app`, and the GitHub repo connected. If that is not yet done, it is the true first task — treat it as Section 0 and complete it before signing off Section 3.

**Workflow-fixture gotcha #5 (from 8c):** Frappe's `safe_eval` sandbox blocks `len()` in workflow *condition* expressions. Gate on truthiness instead — a non-empty child table list is truthy, empty is falsy (8c's Configure-gated-on-taxes uses `doc.taxes`, not `len(doc.taxes) > 0`). Future workflow conditions: use truthy checks, never `len()`.


---

## §8d Item / Product Master — COMPLETE & LIVE-VERIFIED

**Status:** Done. Feature-complete, zero parked items, live-verified end-to-end against the Harmony sandbox (pull + push + flip + drift). All commits pushed to origin/main.

**Build sequence (local, then origin):**
- Stage 1 substrate (Item Map dual-object link, item_master_mode flag, flip endpoint) — ff6a8f8
- Stage 2 pull (cursor walk, savepoint isolation, matching, HSN-held/tax-UOM gating, multi-Co tax) — 0172725 + cbbdef7 (per-Co REPLACE)
- Stage 3 push (separate ruleset, field manufacturing, missing-mandatory flag, product_id writeback, batch sweep) — 060876e
- Stage 4 bundles (component resolution via map, dependency-order, ≥2, itemType conditional, own map row, no-BOM/kit) — 2a22a8d
- Stage 5 lifecycle + drift + flip-changes-behavior (phase-governed) — cc61ada
- Stage 6 UI (per-stage triggers, auto-push hook default-OFF, whitelists) — 258c334
- Audit follow-up (Sync Records at 5 op points, enqueue_easyecom_job facade, delta scheduler, enqueue-sweep, drift child table, drift resolution UI, field-level exclusion, single-account constraint, Item Map list view, workspace count cards) — c773ee4
- Workspace §17 layer (Top Strip, 6-card worklist row, 3 live KPI tiles, 4 labelled-empty pending placeholders, charts) — [workspace packet commit]
- Live bring-up (26 commits 51223f9..530de2c): pull fixes (relative cursor, company= at call sites, query-strip logging, empty-page, FNC↔Created-Flagged, child_product creatable, combo total-qty≥2, primary-location pull) + push bring-up (cp_id-not-product_id keying, sparse UPDATE, ModelName, dual-id writeback, EAN, Bundle UPDATE/CREATE, lifecycle, sparse-snapshot, integer contract, UOM-aware Weight + L/H/W via custom_python)

**Suite:** 498 green (129 + 369) pre-live-bringup; live-bringup added regression tests (cleanup-safety, pull regressions, endpoint strip). Confirm final count with Claude Code's latest run.

**Live smoke (Harmony, disposable sandbox):** Pull 70 Items + 5 Bundles, 54 tax-stamped, 0 page failures. Push UPDATE/CREATE/EAN/Bundle-UPDATE verified. Flip + drift + dismiss verified (ERPNext preserved). Phase-3: lifecycle round-trip, 3-item batch sweep (Queue Jobs enqueued+executed), UOM weight/dim conversions, Bundle CREATE all green.

**INCIDENT — factory-flip (e6d545d):** `bench run-tests` against the live site triggered cleanup_easyecom_state(), wiping the live Harmony account / all EE Locations / all Company Settings / 113 log rows (ERPNext Items survived). Fix: cleanup restricted to explicit test-name prefixes + test_cleanup_safety.py regression. **HARD RULE: never `bench run-tests` against a live site; use `bench execute`.**

**Closeout docs written:** Part I primer (FDE_PRIMER_section_8_masters), section_8d_item.md test script (+ EE contract appendix), SMOKE_RUNBOOK_section_8d_item.md. SPEC.md §8.1 amendments captured (SPEC_8d_patch_notes.md) — USER to apply.

**Standing items / carry-forward:**
- Individual-push hook is wired but gated by auto_push_on_save (default OFF) — turn on at controlled go-live per client.
- Integration Discrepancy DocType deferred to §23 (drift currently uses Sync Record Discrepancy status + Item Map drift child table; TODO marker at drift site).
- §8c tax is pull-direction only — ERPNext-origin items need a manual Item Tax row before push.
- _diag.py helpers untracked, self-marked safe-to-delete.
- Pending workspace tiles (Partial Jobs / Webhook Events / Cursor Lag / Open Discrepancies) are labelled-empty placeholders awaiting §9–§13 flows / §23.
- **Product images (Option A — URL-only pull)**: `product_image_url` → `Item.image` (URL string, ERPNext renders directly); `additional_images` list → `Item.ecs_additional_image_urls` (Long Text, JSON-encoded array). NO download, NO Frappe File doc creation, NO push side. EE's S3 URLs assumed reachable. NOT drift-comparable (CDN re-uploads cycle URLs even when visual is identical). If a client needs offline-resilient images or wants to push ERPNext-side image edits back to EE, that's Option B work (~3-4h, needs an EE upload endpoint we haven't validated).

**8d pattern established for downstream:** §8d is the FIRST entity-sync flow to write Sync Records — 8e Customer and 8f Supplier mirror this (8a/8b/8c are foundational §7.7 and correctly don't write them).

**Next:** 8e Customer (two-population model: real B2B/D2C bidirectional vs marketplace-anonymous pseudo-customers; PII hinge) → 8f Supplier (ERPNext-dominant, push-to-EE).


---

## §8e Customer / Wholesale B2B (§8.2) — COMPLETE

**Status:** Feature-complete, live-verified against Harmony (disposable). All local; 6 commits ahead of origin/main. 123/123 §8e tests green.

**Build sequence (local):**
- Stage 1 substrate — EasyEcom Customer Map (mirrors Item Map; reuses Item Map Drift Field / Exclude Field child DocTypes), customer_master_mode flag + independent flip endpoint
- Stage 2 foundational lookups — EasyEcom Country / EasyEcom State DocTypes, discover-and-cache (§7.7, no Sync Records), resolve_country/resolve_state/validate_pincode_state helpers, Daman&Diu 34/35-vs-3848 largest-id-wins
- Stage 3 pull (+ correction) — EasyEcom-Customer-Pull ruleset (stale EasyEcom-Customer-Sync soft-retired, Anon-Pull untouched); map-row-only matching (no natural key — dirty dupes); GST gating: 3 IC validators hard-throw → FNC with rollback; URP→Unregistered
- Stage 4 push — EasyEcom-Customer-Push ruleset; create (random password, stateId-int, contactNumber REQUIRED) + sparse update (state-name); c_id==customerId confirmed (create returns data.c_id); auto-push checkbox default-OFF; batch sweep
- Stage 5 lifecycle/flip/drift — lifecycle N/A (EE has no customer active flag or deactivate endpoint); flip+drift only; drift→Discrepancy; Dismiss / Push-ERPNext→EE; no Accept-EE; auto-heal reverted to §8d parity (Drift persists until Dismiss)
- Stage 6 UI — 3 number cards, list colours/filters, 7 wired endpoints, daily FULL pull cron 05:30 IST (no updated_after → full not delta)

**Live findings (folded into SPEC §8.2 amendments):**
- CreateCustomer returns data.c_id NOT data.customerId (packet assumption inverted)
- contactNumber REQUIRED on create (doc said optional)
- 3 IC validators hard-throw → FNC: gstin check-digit, gstin-state mismatch, pincode-state mismatch
- EE has no customer lifecycle endpoint; no updated_after filter

**Closeout docs:** Part J primer, section_8e_customer.md test script, SPEC §8.2 amendments, this tracker entry, docx regen.

**Standing items / carry-forward:**
- Pricing & discounts (b2bDiscountScheme, pricingGroupCode, invoiceSeriesCode, salesmanUserId, customerAttributes) — PARKED, later stage
- Order-driven B2B-buyer GSTIN-reuse — §11/§12 (order flow calls 8e's create mechanism with GSTIN match)
- Marketplace anon buyers / pseudo-customer pool — §11/§12 (see pseudo_customer_scope_notes); Part A (channel-pool linkage) touches §8b/§8e when order flows build
- Drift child DocTypes still named "EasyEcom Item Map Drift/Exclude Field" though entity-agnostic — rename to EasyEcom Drift/Exclude Field as one-shot migration when 8f Supplier lands (3rd consumer)
- auto_push_customers_on_save default OFF — turn on at controlled go-live per client

**Pattern note:** §8e is the 2nd entity-sync flow, mirrors §8d Sync Record pattern. §8f Supplier mirrors §8e next.

**Next:** §8f Supplier (§8.3) — closes §8 masters. Then operational flows §9–§13.


---

## §8f Supplier / Wholesale Vendor (§8.3) — COMPLETE · CLOSES §8 MASTERS

**Status:** Feature-complete, live-verified against Harmony (disposable). All local; 11 commits ahead of origin/main. 167/167 §8f tests green.

**Build sequence (local):** 4c6b700 S1 substrate → 36c6ee1 S2 lookups → a3b6dea S3 pull → eb06e3a S4 push → df845aa S5 lifecycle/drift → 73779b3 S6 UI/scheduler (+ tracker commits 80974dc/03fdd2c/674929e/1ffbbf1).
- S1: EasyEcom Supplier Map (two-id split: ee_vendor_c_id read-key unique + ee_vendor_id write-key non-unique); supplier_master_mode + independent flip; **drift child DocType rename** (Item Map Drift/Exclude Field → EasyEcom Drift/Exclude Field, controllers in lockstep, 14 rows preserved, Item+Customer+Supplier repointed)
- S2: eager all-country state/country cache (~247, extended §8e flow)
- S3: EasyEcom-Supplier-Pull (stale Supplier-Sync retired); empty-validator fix (HTTP 200 + benign no-data envelope allow-list, fixed §8.2's 28-territory false-positive); cursor pagination + delta watermark; map-row-only matching; empty-array address; country-aware gating (Indian FNC vs foreign Overseas); active:0→disabled
- S4: EasyEcom-Supplier-Push; create (state NAME, no password, taxIdentificationNum short-form) + sparse update; both ids captured; 58614 puzzle resolved (= vendor_c_id); foreign drops GSTIN/PAN keys
- S5: push-deactivate N/A (9 endpoints 404); flip+drift mirror §8e (Discrepancy, no Accept-EE, Drift persists, field exclusion)
- S6: 3 number cards, list colours/filters, delta cron 06:00 IST (vendors HAVE updated_after — real delta), **sidebar-matches-cardbreaks regression tests** (guards the §8e bug)

**Live findings → SPEC §8.3 amendments (4 key):** taxIdentificationNum SHORT form; CreateVendor dual-id return; UpdateVendor request-vs-response vendorId asymmetry (response=read key); no push-deactivate endpoint.

**Closeout docs:** Part K primer, section_8f_supplier.md, SPEC §8.3 amendments, this tracker, docx regen.

**Standing items / carry-forward:**
- License fields (dl/fssai/msme) + payment/delivery terms — PARKED
- §9/§10 must repoint EasyEcom-PO-Push / EasyEcom-GRN-Pull to resolve via Supplier Map.ee_vendor_id (currently map supplier↔vendor_id directly)
- auto_push_suppliers_on_save default OFF

**§8 MASTERS COMPLETE: 8a Location · 8b Channel · 8c Tax · 8d Item · 8e Customer · 8f Supplier. Next: operational flows §9–§13.**

### Pre-§8-closeout BLOCKERS (fix before §8 truly closed)
- **§8d test_item_pull_stage2: 29/32 (2 fail + 1 err) — PRE-EXISTING, unrelated to 8f (verified via git stash). Needs fix.**
- **§8d test_item_lifecycle_drift_stage5: 7/10 (2 fail + 1 err) — PRE-EXISTING. Needs fix.**


---

## Round 2 — Post-§8 hardening · COMPLETE · ON main

**Status:** Seven commits shipped after §8f closeout `cd6020d`, all pushed to `origin/main`. Documents the operational hardening surfaced during FrappeCloud-staging bring-up (Incirco Ventures LLP) + the operational levers needed for go-live ceremonies.

**Commits (oldest → newest):**
- `fb5465d` fix: two FrappeCloud-staging bugs — `EasyEcom API Call` `before_insert` strips company when `is_foundational=1` (Frappe v15/v16 auto-fills user-default-Company before validate, tripping §7.7); regression `test_token_call_survives_user_default_company`. The second bug fix is bundled here.
- `9280d58` fix: Discover {Products, Customers, Suppliers} async-by-default — enqueue into `long` queue (3600s) via `frappe.enqueue`, return immediately with RQ job_id. Fixes the misleading "(network or permission)" desk error on real-client catalogues that exceed the 120s desk-whitelist budget.
- `6d97179` fix(ui): top-bar Discover / Push-All-Pending dropdowns now include Customer (§8e) + Supplier (§8f) + States-Countries; previously the section-level buttons existed but the top-bar didn't expose them (Stage 6 oversight).
- `a70a30b` fix: `EasyEcomAccount.after_insert` force-encrypts all 4 Password fields via `set_encrypted_password()` — Frappe v15/v16's auto-encrypt-on-insert pass skips Password fields named `email` (reserved-name collision); programmatic creates would fail with "Password not found for EasyEcom Account ... email". Idempotent. Also: Supplier dup-name resilience on create (parity with Customer dup-name from `4108048`).
- `c79eaa5` feat(8d): product images, Option A URL-only. `product_image_url` → native `Item.image`; `additional_images[]` → new custom field `Item.ecs_additional_image_urls` (Long Text, JSON array, patch `v0_1.add_ecs_item_image_fields` `post_model_sync` idempotent). Helper `_populate_image_fields` inline during pull.
- `4108048` feat: per-Item re-evaluate (`re_evaluate_one_product`) + Mark Mapped override (`mark_mapped_override`, FDE/SM only confirm-required) + Customer dup-name resilience on create.
- `3c33c58` feat: Go Live + Pause auto-push controls — `easyecom.api.auto_push_controls.go_live_enable_auto_push` + `pause_all_auto_push`. Role-gated (FDE / SM / EE SM; Operator refused). Confirm-required. Audit Comment on Account doc. Also bundled: threshold-validation refactor on `EasyEcom Company Settings` (`_to_float()` helper + `_validate_thresholds()` rewrite, 0–100 range consistent across all threshold fields).

**SPEC.md amendments (folded into patch notes):**
- §3.7.2 — encryption-guard hook (`a70a30b`) — applied inline to SPEC.md.
- §4.2.1 — `ecs_additional_image_urls` custom field (`c79eaa5`) — applied inline to SPEC.md.
- §8.1.4 — images pull (`c79eaa5`); §8.1.9 — per-row FDE actions (`4108048`) — applied inline to SPEC.md.
- `SPEC_8d_patch_notes.md` — async discover, per-row FDE actions, images.
- `SPEC_8e_patch_notes.md` — Customer dup-name resilience, async discover.
- `SPEC_8f_patch_notes.md` — Supplier dup-name resilience, async discover.
- `SPEC_round2_patch_notes.md` — cross-cutting: ops levers (Go Live / Pause), thresholds validation, company-strip hook, discover-async (cross-cutting summary), top-bar dropdowns.

**Live findings (FrappeCloud staging — Incirco Ventures LLP):**
- Multi-Company sites trip §7.7 invariant via Frappe default-fill (fb5465d).
- Real-client catalogues exceed 120s desk budget (9280d58).
- Password field name collisions in Frappe v15/v16 cause encryption gap (a70a30b).
- Real EE customer/vendor data has same-name distinct records (justifies dup-name retry).

**Standing items / carry-forward:**
- The §8d pre-existing test failures **still open** (test_item_pull_stage2 29/32, test_item_lifecycle_drift_stage5 7/10) — not touched by round-2.


---

## §9 Buying / GRN — BUILD COMPLETE · LIVE-VERIFIED · CLOSEOUT DONE (2026-05-29)

**Status:** §9 built end-to-end across 4 stages + 1 corrective commit, live-verified across 5 Harmony smoke rounds + 2 corrective re-smokes. All §9-affected tests green (163/163). Closeout artifacts shipped: primer, test script, SPEC patch notes, this entry, docx amendment notes. The 24 pre-existing §8 test failures held steady throughout §9 work (none caused by §9).

**Commits on `main`:**
- `18fcc77` — Stage 1: Substrate (PO Map, GRN Map, Sync Record Line, ruleset repoints, settings).
- `0090a32` — §23 Integration Discrepancy stub (frozen-contract, unblocks Stage 3 Link).
- `5851e4b` — Stage 2: PO push (CreatePurchaseOrder content + updatePoStatus status), shared place_of_supply module, rename-coordination fallback, PO-completion deferred to Stage 3.
- `df8464f`…`e041bbb` — Stage 3 + 12 live-finding fixes: GRN pull, PR with qc_fail split, native purchase_order_item linkage, idempotency back-ref, tax-variance on received-gross, line-total rate fix, items wire key, inwarded_warehouse_c_id=company_id semantic, etc.
- Stage 4 + corrective: Test-isolation per-account scoping, address-precondition refuse-don't-placeholder, list views + workspace cards + sidebar lockstep, Sync Record line-child indicator, buying precheck, **unknown-PO drift contract** (no auto-PR, FDE-driven Create-PR-from-GRN with optional PO link, drift Dismiss), **pause-respects-all-three-po_status-pushes** with pending mechanism and un-pause runner.

**Live-verified on Harmony:**
- PO push (content + status) → real `poId` returned.
- Real Harmony WMS GRN → ERPNext PR created + submitted with correct qty model (received_qty=received_quantity, rejected_qty=qc_fail, accepted derived; buckets read-not-posted).
- qc_fail accepted/rejected split per ERPNext invariant.
- Native Batch auto-creation + PR Item.batch_no link.
- `ecs_ee_batch_code` / `ecs_ee_expire_date` custom-field capture on non-batch Items.
- Tax variance check across received-gross (zero false-positives).
- `purchase_order_item` canonical PO→PR linkage (PO.per_received updates live).
- Idempotency back-ref (re-pull safe when Map row wiped).
- Held-Pre-QC → Receipted transition on same Map row.
- Completion push (updatePoStatus=5) accepted by Harmony.
- Discrepancy auto-raise: GRN-for-unknown-PO (corrected to drift, no auto-PR), tax-variance, batch-on-non-batch-item, over-receipt.
- Status reconciliation echo (no false drift).
- **Re-smoke 1 (corrective):** unknown-PO GRN → confirmed no auto-PR, drift Map row + Discrepancy. FDE create_pr_from_grn → standalone PR. dismiss_grn_drift → Dismissed.
- **Re-smoke 2 (corrective):** pause_all_auto_push → all four toggles zero. Submit/cancel/complete during pause → ecs_pending_po_status_push populates (no EE wire). go_live_enable_auto_push(pos=1) → pending fires once with idempotency guard.

**Closeout artifacts:**
- `process/primers/FDE_PRIMER_section_9_buying.md` — own primer (Parts A–L: where §9 sits, two push channels, GRN qty model, QC trigger, Deleted edge, unknown-PO drift, self-GRN routing, Discrepancy taxonomy, worklist, precheck, cron go-live runbook, carry-forwards).
- `process/test_scripts/section_9_buying.md` — 8 sections covering preconditions, PO push (both channels), GRN pull → PR, completion echo, unknown-PO drift (the corrective commit), pause kill-switch (the corrective commit), edge cases including self-GRN routing. Three load-bearing checks called out: §4.2 (no bucket leak), §4.4 (rate from line total), §6.1 (no auto-PR for unknown PO).
- `spec_sections/SPEC_9_patch_notes.md` — rewrites stale SPEC.md §9.1–§9.12 (the per-section change list, including the SUPERSEDED §9.4 mixed-warehouse partial push and §9.6.2 paired accepted/rejected fields).
- This BUILD_TRACKER entry.
- docx regen step: USER runs the unpack/edit/pack pipeline locally against the updated SPEC.md (after applying patch notes).

**Carry-forwards past §9:**
- **GRN-pull cron stays UNWIRED until go-live sets `grn_pull_high_watermark`** — explicit two-step runbook (set watermark → wire cron). Guard test `TestGRNPullSchedulerIntentionallyUnwired` ensures the cron stays unwired in code until that.
- **STN routing live-verification is a §10 PREREQUISITE** — trigger a real self-GRN on Harmony, inspect `vendor_c_id`, confirm equals warehouse company_id. Required before §10 Stage 3 (inbound) builds.
- **STN cancel/amend endpoint** is undocumented in the createOrder doc page shared — Stage 2 STOP-and-ask item when §10's cancel path is reached.
- **Multi-GRN partial cumulative tolerance** is unit-verified but NOT live-smoked. Watch-item for first real client with a partial receipt.

**Adjacent finding closed during §9 corrective commit (worth recording):** the pause mechanism `pause_all_auto_push` was previously zeroing only Items/Customers/Suppliers toggles, leaving `auto_push_pos_on_save` uncovered. This was a pre-existing §9-Stage-2 latent gap that predated the corrective commit; fixed under §9 corrective scope (authorised mid-build). Pause now genuinely means pause across all four auto-push toggles.

**§9 closed. Next: §10 Stock Transfer Flows (packet at `spec_sections/section_10_stock_transfer_packet.md`, Stage 1 build prompt drafted).**

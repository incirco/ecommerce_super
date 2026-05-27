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
| 8f. Supplier / Vendor master | ☑ | ☑ | ☑ | ☑ | ☑ | ☑ | ☐ | ☐ |
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

## §8f Supplier / Vendor (§8.3) — IN PROGRESS

**Status:** Stage 1 complete (substrate + drift-child rename). 31/31 new tests green; §8d/§8e substrate + lifecycle suites green except one pre-existing failure (see Standing items).

**Build sequence (local):**
- Stage 1 substrate — EasyEcom Supplier Map (autoname ECS-SUPP-{ee_vendor_c_id}; two-identifier split — ee_vendor_c_id unique-reqd read key, ee_vendor_id non-unique write key), supplier_master_mode flag + independent flip endpoint, drift-child rename (EasyEcom Item Map Drift/Exclude Field → EasyEcom Drift/Exclude Field, pre-model-sync patch, 14 rows preserved, controllers + parent options + flow + test references repointed) — 4c6b700
- Stage 2 verified eager multi-country lookups — multi-country fixtures (Italy 356 states, Armenia 11 states) captured from Harmony, 15 new tests exercising eager-sweep / per-country-failure-isolation / foreign-state resolution (Abruzzo→1556, Armenian Marz→384/386) / multi-country idempotent re-run. Existing flow code already eager-all-countries; §8f scope was test-verification + foreign-fixture capture. Live timing 101.9s for 247 countries / 8,791 states / 248 HTTP calls — acceptable as admin-triggered refresh. — 36c6ee1
- Stage 3 pull — new EasyEcom-Supplier-Pull ruleset (19 rules, 2-id split + GSTIN+PAN + country-aware fields + flat-address-flattening); EasyEcom-Supplier-Sync RETIRED (active=0); cursor pagination (nextUrl confirmed live 2-pages 30-vendors); empty-array address handled via pre-flatten; country-aware GST gating (Indian valid/Unregistered/invalid-FNC + foreign Overseas); lifecycle pull-side (active:0 → Supplier.disabled=1 + Map.status=Disabled; symmetric restore); Sync Records (one per Supplier, direction=Pull, none for FNC); Discover Suppliers button (role-gated, refuses post-flip). Also folded in the client-layer no-data carve-out fixing the 28-territory false-positive (Stage 2 finding) — verified 0 failures on live re-sweep. — a3b6dea
- Stage 4 push — new EasyEcom-Supplier-Push ruleset (16 rules, separate from Pull per §8.0); CreateVendor + UpdateVendor with the EE-field-name correction (taxIdentificationNum NOT -Number — live disclosure); CreateVendor response carries BOTH ids (vendor_id WRITE + vendor_c_id READ; both captured to Map); UpdateVendor response data.vendorId is the READ key (58614 puzzle SOLVED — defensive refresh of missing vendor_c_id); state as NAME everywhere (no name→id resolution unlike §8e customer); no password; single-address payload (Billing → Shipping fallback); country-aware tax (Indian requires GSTIN+PAN with URP substitution + PAN auto-extract from GSTIN[2:12], foreign drops both); sparse-update + snapshot mirroring §8d/§8e; enqueue facade for batch sweep (Queue Job per candidate, returns immediately); auto-push hook (Supplier.after_insert + on_update) gated by auto_push_suppliers_on_save=1 + Company type + ping-pong flag; "Push All Pending Suppliers" Account button; 35 tests; ONE disposable Harmony probe + ONE end-to-end smoke completed cleanly. — eb06e3a
- Stage 5 lifecycle/flip/drift — push-side deactivate endpoint is N/A (9 plausible paths all return 404; Supplier.disabled stays ERPNext-local, Stage 3's pull-side covers EE→EN); post-flip drift detection (three outcomes — EE-origin new → Drift row no Supplier; EE-side edit → Drift status + field rows ERPNext untouched; quiet re-pull → table cleared but Drift sticks until Dismiss); comparable fields lock supplier_name/gstin/pan/email_id/mobile_no/default_currency + 10 prefixed address labels (billing/dispatch.{street,city,pincode,state,country}); internal ids (vendor_c_id/vendor_id/vendor_code) EXCLUDED; Dismiss + Push-to-EE whitelist endpoints (role-gated; refuse non-Drift + no-link); NO Accept-EE (regression-guarded test scans for 'accept'/'adopt' identifiers); field-level exclusion via ecs_drift_exclude_fields child (honors top-level AND 'billing.X'-prefixed labels); Sync Record status=Discrepancy (not Failed); flip round-trip end-to-end test; 21 tests. — df845aa
- Stage 6 UI/workspace + scheduler — 3 Supplier number cards (Drift/Created-Flagged/FNC with red/orange/grey); Workspace EasyEcom gains Supplier Map link (Masters) + 3 Suppliers - * worklist links (FDE Worklists) + 3 number-card array entries + a Suppliers content-block row + Supplier Map shortcut; Sidebar gains the same 4 entries IN LOCKSTEP (each worklist filter has route_options={"status":"X"} populated); two §8e regression guards (symmetric workspace-vs-sidebar pair-up check; clickability/route_options-populated check — the URL-placement bug); list view 5 status colours + 4 sidebar quick filters; DELTA-pull scheduler at 06:00 IST staggered after Items 05:00 + Customers 05:30 (vendors HAVE updated_after — verified live; reads supplier_pull_last_updated_at high-water, formats as YYYY-MM-DD, falls through to full pull on blank watermark, quiet on no-account, catches all exceptions); auto_push_suppliers_on_save default OFF; endpoint inventory verified (6 whitelisted FDE endpoints all role-gated, Operator refused); 2 re-import patches (workspace + sidebar) force-load the JSON changes on existing sites; 26 tests. — 73779b3

**§8f COMPLETE.** Closes §8 Masters. Total local commits: 11 ahead of origin/main.

**Standing items / carry-forward:**
- **§8d test_item_lifecycle_drift_stage5 has a PRE-EXISTING standing failure** (2 fail + 1 err): test_flip_explicitly_changes_pull_behavior (error), test_erpnext_disable_sends_deactivate_status_zero (fail), test_erpnext_enable_sends_activate_status_one (fail). Confirmed unrelated to the §8f drift-child rename by git-stashing §8f and re-running on the pre-§8f tree — same failures occur there. Push outcome shows `item has no ecs_ee_cp_id — never pushed`, suggesting test-setup brittleness around the cp_id seed, not the rename. **Fix before §8 closeout** — do not let this carry into operational flows §9–§13.
- **§8d test_item_pull_stage2 has a PRE-EXISTING standing failure** (2 fail + 1 err): test_savepoint_isolation_one_bad_product, test_combo_product_with_no_subproducts_flagged, test_child_product_flagged_not_created. Errors show "product carries no tax_rule_name" — §8d Stage 2 tax-stamping test brittleness, unrelated to §8f. Confirmed unrelated by stashing §8f Stage 3 and re-running. **Fix before §8 closeout** alongside the lifecycle_drift_stage5 failures.
- `EasyEcom-PO-Push` / `EasyEcom-GRN-Pull` map `supplier ↔ vendor_id` directly — switch to `EasyEcom Supplier Map.ee_vendor_id` lookup in §9/§10 (don't assume `supplier.name == vendor_id`). Stage 3 deliberately left these untouched per packet directive.
- IC `validate_party` auto-extracts PAN from GSTIN[2:12]; Indian suppliers need country=India + valid GSTIN, foreign suppliers need country set BEFORE validate runs so `guess_gst_category` returns "Overseas".
- Client-layer no-data detection — current allow-list covers "no data found", "unable to find states/vendors", "no records found", "no result found". Extend the tuple if a new live observation justifies it; do NOT relax to a generic-400-passthrough.
- Stage 3 refuses cleanly in erpnext_mastered mode (raises NotImplementedError at the flow + clean refusal at the whitelist) — Stage 5 must wire drift detection BEFORE any FDE flips a real account.

**Open decisions:**
1. ~~getVendors `nextUrl` pagination~~ → **CONFIRMED USED** (Stage 3) — Harmony returned 2 cursor pages on the captured sample. nextUrl is a path; client follows directly. Cursor classified foundational (with query-strip).
2. ~~UpdateVendor `data.vendorId: 58614`~~ → **RESOLVED** (Stage 4) — it IS the READ key (vendor_c_id). UpdateVendor's response data.vendorId is an int (vendor_c_id) while the request body's vendorId is a string (vendor_code WRITE key). Same name, opposite role request-vs-response. Flow uses this for defensive refresh of missing vendor_c_id on existing Map rows.
3. ~~CreateVendor response shape~~ → **RESOLVED** (Stage 4) — returns BOTH `data.vendor_id` (write key echo) AND `data.vendor_c_id` (newly-assigned read key); no follow-up getVendors read-back needed.
4. ~~EE field name `taxIdentificationNumber` vs `taxIdentificationNum`~~ → **RESOLVED** (Stage 4) — live error disclosed it is `taxIdentificationNum` (no `-er` suffix). Both docs and our Stage 3 pull use the longer name when READING (pull's `tax_identification_number` is correct on read); WRITING needs the shorter name. Pull + Push use different keys deliberately.
5. ~~Push-side deactivate endpoint exists?~~ → **RESOLVED N/A** (Stage 5) — 9 plausible endpoint paths probed live, all returned HTTP 404. Same finding as §8e Customer. Supplier.disabled stays ERPNext-local; Stage 3's pull-side covers EE→EN deactivation. Do not invent an endpoint.

**Standing § amendments needed (for §8 closeout SPEC patch):**
- §8.3: CreateVendor's mandatory tax field is `taxIdentificationNum` (no `-er` suffix) — EE docs have it as the longer name but the live backend rejects unless short form is used.
- §8.3: CreateVendor response carries BOTH `data.vendor_id` (write key echo) AND `data.vendor_c_id` (newly-assigned read key) — no follow-up getVendors read-back required after Create.
- §8.3: UpdateVendor `data.vendorId` is the READ key (vendor_c_id int), not the write key — same field name as the request body but opposite role.
- §8.3: No push-side vendor lifecycle endpoint exists in the EE Wholesale API.

**Next:** §8f closeout — primer (FDE_PRIMER_section_8_supplier), section_8f.md test script, SPEC §8.3 amendments patch (4 items: taxIdentificationNum short form, CreateVendor dual-id return, UpdateVendor response-vs-request vendorId asymmetry, no-push-deactivate), pre-existing §8d standing failures (2 suites, see Standing items above) before §8 closeout.

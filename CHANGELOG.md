# Changelog

Everything worth deploying, keyed by date. Each entry names the underlying issue, the PR, and the commit SHA — all three are clickable links.

- **Issues / PRs** use reference-style links defined at the bottom of the file, so an entry like `[#166]` jumps straight to the ticket.
- **Commit SHAs** are inline links to the merge commit on `main`.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Dates are IST.

The `[Unreleased]` section holds anything on `main` that hasn't yet been deployed to production sites.

---

## [Unreleased]

*Nothing pending — every entry below is on `main` and ready for the next `mmpl16.frappe.cloud` redeploy.*

---

## [2026-07-15]

### Fixed

- **§11 B2B outbound: `itemDiscount` sent per-unit but EE applies it per-LINE** — EE multiplies `Price` by Quantity but subtracts `itemDiscount` once (not multiplied). Our `_item_price_and_discount()` was emitting both per-unit, so any qty>1 discounted line was under-discounted by `(qty-1) * discount` → EE invoice > SO grand_total. Live symptom on SO-2610401 line 2 (qty=5, rate=300, 50% off, 5% IGST): EE returned taxable ₹2,700 (expected ₹1,500); SI grand_total ₹5,984 vs SO ₹4,724. Undiscounted lines in the same SO reconciled exactly, isolating the defect. Fix: multiply `discount * qty * tax_multiplier`. Ships with 4 new regression tests including the exact live scenario; `test_qty1_discounted_line_unchanged_by_gh197` proves prior qty=1 cases still hold. Blast radius: every B2B order pushed with a qty>1 discounted line since [PR #185] shipped is affected — audit `EasyEcom B2B Order Map` where EE total ≠ SO grand_total. [#197], [PR #198], [`91dc575`].
- **§11 B2B outbound: mixed-GST-rate SOs used SO-level blended tax multiplier** — uncovered by the post-#197 parametric input sweep. `_item_price_and_discount()` computed `tax_multiplier = so.grand_total / so.net_total` — correct only when every line has the same GST rate. On a mixed-rate SO (5% + 18% together), low-rate items were over-grossed by ~6% and high-rate items under-grossed by ~5%. EE backs tax out at each item's own ProductTaxCode, so a blend-based Price would not round-trip. Not seen in MMPL production yet (uniform-rate luck); pure prevention. Fix: new `_line_tax_multiplier()` reads `so_item.item_tax_rate` (ERPNext-populated JSON from Item Tax Template + Sales Taxes and Charges); sums the rate dict → per-line %. Handles single IGST and CGST+SGST split identically. Falls back to SO-level blend when `item_tax_rate` is empty/malformed (backward compat). Ships with 9 unit tests. [#201], [PR #198], [`91dc575`].
- **§11.5.1 gh#166 regression: `_ensure_role_permissions` shadowed standard perms on Territory, Customer Group, Print Format** — the [#166] user + role patch inserted `Custom DocPerm` rows via `frappe.new_doc()` directly. Frappe's rule: if ANY Custom DocPerm exists for a DocType, ALL standard DocPerms are ignored. On doctypes with no prior Custom rows (Territory, Customer Group, Print Format, and every core master in `_PERMISSIONS`), our raw insert became the ONLY perm row — wiping every other role's access after Permission Manager next resolved. Live symptom on `live16version.frappe.cloud` (MMPL prod): normal users lost visibility of Territory, Customer Group, Print Format immediately after `bench migrate`. Fix: rewrote `_ensure_role_permissions()` to use `setup_custom_perms(doctype)` FIRST (copies standard DocPerms into Custom DocPerms, preserving every other role's rules), then `add_permission()` + `update_permission_property()` per flag. Also added `if_owner=0` to the existence check so if-owner rows don't false-match. Ships with 9 unit tests locking every safety invariant (setup-before-add ordering, idempotent narrowing including zeros, security-sensitive doctype allowlist regression guard). Reported by @garv999. [#200], [PR #202], [`715d16f`].

### Added

- **§11 B2B outbound: submit-time gate rejecting fractional qty lines** — EE contract: fractional quantities are not supported on either B2B module. Prior code silently truncated (2.5 → 2) inside the payload builders, under-shipping the customer without warning. New precondition #10 in `validate_preconditions()` throws with an actionable message naming the item, row, and qty, and pointing to two remediation paths (change UOM to whole-number, or split the line). Fires for both Old B2B and New B2B; the constraint is EE-side, not module-side. Ships with 5 unit tests. The `int()` coercion in the payload builders remains as defense-in-depth for any historical Draft SOs that predate this gate. [PR #198], [`91dc575`].
- **§11 B2B outbound: Old B2B builder now coerces fractional qty consistently with New B2B** — prior code sent `str(so_item.qty)` which for qty=2.5 would emit "2.5" and be hard-rejected by EE. Fixed to `str(int(float(qty)))` matching New B2B's `int()` behavior. Defense-in-depth for any historical Draft SO that predates the fractional-qty gate. [PR #198], [`91dc575`].
- **Repair patch for [#200] on live sites** — `repair_gh200_unshadow_standard_perms.py` runs on next `bench migrate`. Per affected doctype: (1) delete our Custom DocPerm rows (standard perms auto-restore on freshly-shadowed doctypes), (2) clear meta cache, (3) commit, (4) call the FIXED `_ensure_role_permissions()` to re-add our perms via the safe path. Delete filter scoped to `role="EasyEcom Integration"` only — pre-existing site customizations on other roles are never touched. Mid-run crash leaves site in "standard perms restored, integration missing" state (safer failure mode than the reverse). [PR #202], [`715d16f`].

### Discipline changes (from today's discoveries)

- **Parametric input sweep is the discipline change from #197** — every prior test used qty=1, where per-unit and per-line arithmetic are numerically identical. `test_old_b2b_variant_same_pricing_math` used qty=2 but its assertion baked the buggy formula in. Root gap: tests audited code paths, not input dimensions. Sweep discipline (qty ∈ {0, 1, 2, 5} × all 5 GST rates × value edges) added to the same PR as #197 — and it immediately surfaced [#201] mixed-rate blend bug + the Old B2B `str("2.5")` bug in the same sitting.
- **Frappe framework primitives always > direct DocType inserts** — [#200] is the second regression this month caused by reaching for `frappe.new_doc(...)` on framework doctypes instead of the documented public API. The public API exists precisely to handle side-effects (like copying standard DocPerms before adding Custom ones) that direct inserts skip. Rule adopted: before writing a `frappe.new_doc(...)` on any core framework doctype (User, Role, Custom DocPerm, Property Setter, Custom Field, etc.), search `frappe/permissions.py` / `frappe/core/doctype/<x>/<x>.py` for a helper function first. If a helper exists, use it.

### Deploy actions

- `bench migrate` on any deployed site (MMPL first) — runs `repair_gh200_unshadow_standard_perms` and restores Territory / Customer Group / Print Format visibility for all non-integration users.

### Verification

- On MMPL after migrate: Permission Manager on Territory shows Territory Manager + System Manager + All + EasyEcom Integration (not just EasyEcom Integration alone).
- Sales User account can list Customer Group and see Print Formats again.
- Post-migrate integration smoke test: `/einvoice/update` still succeeds for a live SO (integration user still has its own perms via the re-add step).
- Push a fresh qty>1 discounted B2B SO → EE invoice total = SO grand_total = mirrored SI grand_total.
- Attempt to submit a B2B SO with a fractional-qty line → refused at submit time with actionable message.

---

## [2026-07-14]

### Added

- **60+ new B2B unit tests across two comprehensive testing rounds** — after live-order deploy-test-fix cycles proved slow, added a proactive test sweep to lock every §11 code path shipped since the B2B rollout. Round 1 (25 tests, [PR #194]): `test_invoice_mirror_gh181_discount` (5), `test_payload_builder_gh184_discount` (5), `test_item_push_sparse_update_gh158` (4), `test_gh181_part2_append_taxes` (8), plus assorted GSP handler date/session tests. Round 2 (24 tests, [PR #195]): `test_manual_repush` (5), `test_gh166_ip_allowlist_ratelimit` (13, including the qty-0 disable case), `test_gh176_queued_orphan_reclaim` (11). Total B2B module coverage: 244 tests. [PR #194], [`7474382`], [PR #195], [`45c2001`].
- **Round-2 tests uncovered live latent bugs and shipped fixes for them** — writing `test_gh166_ip_allowlist_ratelimit` exposed that `int(limit_raw or 6)` coerced a legitimate `0` (documented as "set to 0 to disable") to `6`, defeating the disable path. Fixed with explicit `None` check. Same suite exposed `_enforce_ip_allowlist` crashing on non-string values from `frappe.db.get_value` (dict fallback path); added `isinstance(str)` guard. `_reassert_si_dates_for_submit` crashed when `getdate` received a MagicMock in tests; wrapped in `try/except (TypeError, ValueError, AttributeError)`. All three now covered by explicit regression tests. [PR #195], [`45c2001`].
- **4 pre-existing stale test modules restored to green** — after adopting the new comprehensive testing discipline, 4 test modules failing since before the B2B rollout got proper root-cause fixes (not skips): `test_field_mapping_sandbox` (allowlist assertions didn't include the `_SAFE_BUILTINS` frozenset added when the sandbox was widened to expose `int/float/round`), `test_item_push_candidate_sweep` (`TaxRuleName` reason leaked into HSN-gate tests after [#158] shipped — added `patch.object(_resolve_tax_rule_name)`), `test_validate_pre_submit_item_map_guard` ([#93] reopener widened helper from `frappe.db.exists` to `frappe.db.get_value` with a dict result — tests still mocked `exists`), `test_b2c_invoice_builder` (mock returned string not dict). Test suite is now fully green with no skips. [PR #196], [`e5167e3`].

### Fixed

- **§11.5.1 comprehensive perms audit for the `EasyEcom Integration` role** — after [#166] hardening shipped, a series of `PermissionError` throws on live `/einvoice/update` requests surfaced additional DocTypes the role couldn't read/write during the SI insert → submit → mint chain (SO-2610397: "User don't have permissions to select/read this account"). Expanded `_PERMISSIONS` in the `create_easyecom_integration_user` patch to cover ~50 DocTypes: `Account`, `Cost Center`, `Warehouse`, `GST Settings`, `Item Tax Template`, `Serial and Batch Bundle`, `GL Entry`, `Payment Ledger Entry`, `Print Format`, etc. Also added a re-run patch (`expand_integration_role_perms`) that reapplies `_ensure_role_permissions()` on already-deployed sites so live sites pick up the expanded set without re-running the whole user-creation patch. [#166] followup, [PR #192], [`790a1a9`], [PR #193], [`71f9559`].
- **§11.5.1 mirror SI taxes: `charge_type='Actual'` displayed rate=0 on invoice PDFs** — [PR #189] populated `SI.taxes` from EE's per-item breakdown using `charge_type='Actual'` which set a fixed `tax_amount` but left `rate` at 0 → grand_total was correct but the printed invoice showed "IGST @ 0.00%". Reworked to `charge_type='On Net Total'` with derived rate, so both grand_total and the print format now show the correct GST %. Also fixed the GST account lookup: prior code used `frappe.db.get_value` filtered on the child DocType which silently returned `None` even when the row existed. Rewrote to iterate `GST Settings.gst_accounts` child table directly. [#181] part 2 followup #2, [PR #190], [`a1e66cf`], [PR #191], [`ba8baac`].

### Discipline change

- **Testing methodology now includes parametric input sweeps** — surfacing multiple regressions during Round 2 that Round 1 didn't catch highlighted a gap: exhaustive code-path coverage doesn't guarantee input-dimension coverage. Going forward, per-line arithmetic tests must sweep at least `qty ∈ {1, N>1}` and boundary values must include documented sentinels (e.g. `0` for "disabled" flags). This rule surfaced #197 next-day when SO-2610401 shipped with qty>1, so the change is already paying back.

---

## [2026-07-13]

### Added

- **GSP mode chip on EasyEcom Account form** — plain-English readout of the two mint toggles (`gsp_mint_einvoice`, `gsp_mint_ewaybill`) so FDEs see the effective mode at a glance instead of interpreting two checkboxes. Renders as an indicator badge inside the Custom GSP section: "Only ERP invoice" (grey), "ERP invoice + eway" (blue), "ERP invoice + IRN" (blue), or "ERP invoice + IRN + eway" (green, full compliance). Live-updates on toggle change, no save needed. [PR #174], [`37f874f`].

### Fixed

- **§11 outbound push double-counted item discounts** — `build_old_b2b_item` and `build_new_b2b_item` sent `Price = so_item.rate` (already post-discount) AND `itemDiscount = so_item.discount_amount` on top. EE subtracted the discount again → invoice landed at wrong (usually zero) total. Live symptom on SO-2610392: SO ₹315, EE-returned invoice ₹0. Fix: new `_item_price_and_discount()` helper reconstructs list price `Price = rate + discount_amount` so EE's own subtraction yields the correct net. Ships with 4 unit tests. Blast radius: any §11 SO with an item-level discount pushed since §11 shipped has a wrong EE-side invoice — MMPL ops audit needed. [#184], [PR #185], [`d69a44e`].
- **Mirror SI totals didn't match EE on promo orders** — `_resolve_line_items` read `breakup_types["Item Amount Excluding Tax"]` (pre-discount) and ignored the sibling `breakup_types["Promotion Discount Excluding Tax"]` that cancels it out. Live delta on SO-2610392: EE ₹0 (100% promo), our SI ₹285.71. Fix: three-tier source priority — (1) EE's per-line `taxable_value` (already post-promo), (2) breakup_types sum, (3) selling_price fallback. Ships with 5 unit tests. Partial fix — populating SI.taxes child table is separate follow-up in the same issue (non-blocking for Combo A, blocking for Combo C/D). [#181] part 1, [PR #182], [`4e7ed2f`].
- **Queue Jobs stuck at `state=Queued` indefinitely** — reactive fix via reclaim + proactive fix at enqueue time. **Reactive**: `reclaim_orphaned_jobs` only caught `state=Running` orphans. Added `_reclaim_queued_orphans` with an idempotency probe that marks jobs Success when the target artifact (B2B Map / Item Map / Customer Map) already exists, otherwise re-enqueues. Hourly scheduler drains backlog. **Proactive**: wrapped all three `frappe.enqueue` call sites (`enqueue_easyecom_job`, `retry_job`, `_reenqueue`) in try/except — on failure the row goes straight to Failed with a clear error and the caller sees the exception immediately; on success the returned rq_job_id is persisted for reliable reclaim liveness detection. [#176], [PR #177], [`2abc2b7`], [PR #179], [`991c3a9`].
- **Mirror SI submit fails days after insert** — added `si.set_posting_time = 1` to `invoice_mirror.py`. Without it, ERPNext resets `posting_date` to today on every validate, leaving `due_date` (pinned to original posting_date) earlier → "Due Date cannot be before Posting Date". Also added `_reassert_si_dates_for_submit()` in `gsp_handler.py` to heal pre-fix Draft SIs in-place before submit (uses `db_set` + `reload` so the fix works retroactively on already-created Drafts). Live root cause on SI-2603815 (drafted 2026-07-11, regenerate attempted 2026-07-13). [#161] v2, [PR #172], [`9d7e596`].

### Added

- **Least-privilege `EasyEcom Integration` user + role** for `_elevated_session` to prefer over Administrator — [#166] hardening, [PR #168], [`a60b2c6`]. Role scoped to only the DocTypes the inbound GSP handler actually touches (Sales Invoice, DN, Payment Entry, Customer Map, Item Map, EE log DocTypes, IC log DocTypes). Explicitly excludes User, Role, System Settings, Server Script.
- **IP allowlist on Bearer usage** — new optional `EasyEcom Account.gsp_ip_allowlist` (comma-separated IPv4 / CIDR). Empty = no restriction. [#166] hardening, [PR #168].
- **Rate limit on inbound GSP calls** — new optional `EasyEcom Account.gsp_rate_limit_per_min` (default 6/min per invoice_id). Redis-backed via `frappe.cache`. Breach → HTTP 429. [#166] hardening, [PR #168].

### Fixed

- **`/einvoice/update` PermissionError under Guest session** — inbound GSP handlers now run under an elevated session for the SI insert/submit/mint chain, so third-party validate hooks (modernmarwar's `set_total_overdue_amount`, IC, etc.) that call `frappe.get_list` survive. [#166], [PR #167], [`1a1d81a`].

### Security

- **Bearer token TTL reduced 3600s → 900s** (1h → 15m). Shorter compromise window if a token leaks. EE re-mints transparently via `/gettoken` on expiry. [PR #168].

### Deploy actions

- `bench migrate` on the site — runs two new patches: `create_easyecom_integration_user` + `add_gsp_security_fields`.
- Optional post-deploy: populate `EasyEcom Account.gsp_ip_allowlist` with EE's outbound IP range once known.

### Verification

- After migrate, check `User` list for `easyecom-integration@internal.local` (enabled, role includes `EasyEcom Integration`).
- Re-fire `/einvoice/update` for a submitted SO → SI created; Version log on the SI shows `easyecom-integration@internal.local` as the modifier, not Administrator.
- Fire 8 identical `/einvoice/update` calls in 60s for the same invoice_id → 7th onward returns HTTP 429.

---

## [2026-07-11]

### Added

- **"Re-fire EasyEcom Push" button on Sales Order form** — recovers from orphaned Queue Jobs (SO submitted but on_submit push didn't enqueue). Idempotent, role-gated, only visible on submitted SOs without a B2B Order Map. [PR #165], [`5ed7308`].

### Fixed

- **§11 [#141] detector** now reads the REAL Gate 0 resolver key (`EasyEcom Location.mapped_warehouse`) instead of the display-only `Warehouse.ecs_ee_location` field. Old detector produced wrong diagnostics + crashed on sites where the FK column wasn't materialised. [#162], [PR #164], [`200f780`].
- **Mirror SI Gate 3 (DN-mandatory)** — mirror now sets `si.update_stock=1` so India Compliance doesn't require a linked Delivery Note before e-invoicing. §11.5.1 Mode 1 is invoice-first. [#160], [PR #163], [`9707d28`].
- **Mirror SI Gate 3.5 (Due Date before Posting Date)** — mirror now pins `si.transaction_date = si.posting_date` and clears `si.payment_terms_template` before insert, so no downstream template can move due_date earlier. [#161], [PR #163], [`9707d28`].
- **Item Update push drops mandatory TaxRuleName** — sparse Update payload now always includes `productId`, `TaxRuleName`, `TaxRate`, `ProductTaxCode` regardless of diff. Unblocks §11 SO submits for items whose EE-side data hasn't changed since Create. [#158], [PR #159], [`3019ced`].
- **`_log_inbound_gsp_call` was silently rejected** by SQL — helper tried to read `EasyEcom Account.company` (no such column; company lives on `EasyEcom Location`). Dropped the lookup; company field is `reqd=None` on API Call anyway. [#147] hotfix, [PR #157], [`2111d9c`].

### Deploy actions

- `bench migrate` — three new patches land: `add_address_ee_c_id_field` (from [#126]), `backfill_api_call_direction` ([#147]), `backfill_customer_map_ee_c_id` ([#144], via [PR #145]).

### Verification

- Re-push any Item that had failed with "TaxRuleName is a mandatory parameter" → HTTP 200; Item Map advances to `Mapped`.
- Re-fire `/einvoice/update` → Gate 3 + Gate 3.5 pass; SI submits.
- New EasyEcom API Call row shows `direction=Inbound` on every hit.

---

## [2026-07-10]

### Added

- **Inbound API Call logging** — new `direction` field on `EasyEcom API Call` (Outbound / Inbound, default Outbound for legacy rows). Every hit on `/gettoken`, `/einvoice/update`, `/ewaybill/update` now creates one row with request + response bodies (redacted headers), latency, and correlation_id. [#147], [PR #155], [`414d02e`].
- **Warehouse half-mapping detector + SO intent-gap detector** — non-throwing observability hooks. Warehouse.validate warns when `ecs_ee_location_label` is set but the FK is empty. SO.on_submit posts a timeline Comment when B2B intent signals combine with Gate 0 silent-inert rejection. [#141], [PR #156], [`7495131`].

### Fixed

- **§8e customer dedup via Party Alias** — §8e customer pull now uses natural-key dedup (mobile → gstin) to reuse an existing ERPNext Customer when EE issues a new `c_id` for the same real-world buyer. Prevents the Flagged-Not-Created spiral when EE assigns fresh c_ids on address changes. [#126] (resolves [#59]), [PR #139], [`6b3e62b`].
- **§11.5.1 inbound Sync Record write** — `_log_inbound_gsp_failure` was silently rejected on insert due to missing mandatory fields (`entity_doctype`, `entity_name`, `correlation_id`, `idempotency_key`, `attempts`). Now uses Sales Order + `reference_code` as the entity; skips cleanly when reference doesn't resolve. [#143] followup, [PR #146], [`8cdca09`].
- **§8e/§11 Customer push write-back of ee_c_id** — Update path now writes `ee_c_id` alongside `ee_customer_id` (previously only wrote `ee_customer_id`, leaving `ee_c_id` as a `flagged-<docname>` placeholder). Resolver falls back to `ee_customer_id` when `ee_c_id` doesn't match. One-shot backfill patch for existing sites. [#144], [PR #145], [`76e1b10`].
- **`/einvoice/update` error surfacing** — failures now return the real `message` (not bare `{"status":422}`), populate an Error Log entry with traceback + ee_row snapshot, and write an inbound Sync Record. Also accepts EE's actual body shape (`orders: {...}` as object, not just array). [#142], [PR #143], [`8a0a57a`].

### Deploy actions

- `bench migrate` — five new patches queued.

### Verification

- `EasyEcom API Call` list → filter `direction=Inbound` → every EE hit visible with full request/response bodies.
- Sync Record list → filter `direction=Inbound API, status=Failed` → inbound failures now aggregated by reference_code.
- Re-pull §8e customers on a site with duplicate map rows → dedup path fires, no new Flagged-Not-Created rows.

---

## [2026-07-09]

### Fixed

- **§11.5.1 [#130] regression: bare 500 on root-path GSP calls** — the initial fix delattr'd `request.path`, which on this werkzeug version is a plain instance attribute (not a `cached_property`), so subsequent reads raised AttributeError and Frappe's exception handler cascaded. Fixed by direct assignment: `request.path = new_path`. [PR #140], [`fd84aeb`].
- **§11.5.1 [#137]: PDF-render failures now surface on SI timeline** — `_render_si_pdf_base64` failures now emit a Comment on the linked SI alongside the Error Log entry, so FDEs see the failure without hunting through logs. [PR #138], [`1841623`].
- **§12 B2C paginate `getAllOrders` properly** — was capturing only the first page; refactored to follow `data.nextUrl`. Captured 13.6× more orders on the smoke test. [PR #119], [`464a54b`].

### Docs

- **OPS_upgrade_notes.md seeded** — first entry documents the Frappe CRM v16 upgrade note from the June 2026 release notes ([#129]). [PR #136], [`a21b353`].

---

## [2026-07-08]

### Added

- **§11.5.1 [#134]: populate `invoice_base64` and `eway_bill_base64`** in the GSP response — reliable PDF delivery to EE (URL-based fetch was auth-broken via session cookie trap). [PR #135], [`edff222`].

### Fixed

- **§10 [#131]: DN push coalesces empty `expDeliveryDate` to `posting_date`** — DN has no `delivery_date` field (that's on SO), so EE was receiving epoch 1970 dates. [PR #133], [`d130e33`].
- **§11.5.1 [#130]: EE calls root paths `/gettoken`, `/einvoice/update`, `/ewaybill/update`** — added `before_request` hook that rewrites WSGI `PATH_INFO` to the dotted `/api/method/...` URLs Frappe's router expects. Regression in this fix caught + shipped as [PR #140] the next day (see 2026-07-09). [PR #132], [`a6d2eed`].

---

## Legend

- **Fixed** — bug fix; no schema change required unless called out
- **Added** — new capability; may involve new DocType, Custom Field, or button
- **Changed** — behavior change on an existing capability
- **Security** — token / auth / permissions change
- **Deploy actions** — anything beyond `bench migrate` (rotation, config toggle, credential regeneration)
- **Verification** — one-line smoke test to confirm the change is live and working

---

<!--
Reference-style links. Kept at the bottom so entries above stay compact.
When adding new entries, append the corresponding reference here.
-->

[#59]: https://github.com/incirco/ecommerce_super/issues/59
[#126]: https://github.com/incirco/ecommerce_super/issues/126
[#129]: https://github.com/incirco/ecommerce_super/issues/129
[#130]: https://github.com/incirco/ecommerce_super/issues/130
[#131]: https://github.com/incirco/ecommerce_super/issues/131
[#134]: https://github.com/incirco/ecommerce_super/issues/134
[#137]: https://github.com/incirco/ecommerce_super/issues/137
[#141]: https://github.com/incirco/ecommerce_super/issues/141
[#142]: https://github.com/incirco/ecommerce_super/issues/142
[#143]: https://github.com/incirco/ecommerce_super/pull/143
[#144]: https://github.com/incirco/ecommerce_super/issues/144
[#147]: https://github.com/incirco/ecommerce_super/issues/147
[#158]: https://github.com/incirco/ecommerce_super/issues/158
[#160]: https://github.com/incirco/ecommerce_super/issues/160
[#161]: https://github.com/incirco/ecommerce_super/issues/161
[#162]: https://github.com/incirco/ecommerce_super/issues/162
[#166]: https://github.com/incirco/ecommerce_super/issues/166
[#176]: https://github.com/incirco/ecommerce_super/issues/176
[#181]: https://github.com/incirco/ecommerce_super/issues/181
[#184]: https://github.com/incirco/ecommerce_super/issues/184
[#197]: https://github.com/incirco/ecommerce_super/issues/197
[#200]: https://github.com/incirco/ecommerce_super/issues/200
[#201]: https://github.com/incirco/ecommerce_super/issues/201

[PR #119]: https://github.com/incirco/ecommerce_super/pull/119
[PR #132]: https://github.com/incirco/ecommerce_super/pull/132
[PR #133]: https://github.com/incirco/ecommerce_super/pull/133
[PR #135]: https://github.com/incirco/ecommerce_super/pull/135
[PR #136]: https://github.com/incirco/ecommerce_super/pull/136
[PR #138]: https://github.com/incirco/ecommerce_super/pull/138
[PR #139]: https://github.com/incirco/ecommerce_super/pull/139
[PR #140]: https://github.com/incirco/ecommerce_super/pull/140
[PR #143]: https://github.com/incirco/ecommerce_super/pull/143
[PR #145]: https://github.com/incirco/ecommerce_super/pull/145
[PR #146]: https://github.com/incirco/ecommerce_super/pull/146
[PR #155]: https://github.com/incirco/ecommerce_super/pull/155
[PR #156]: https://github.com/incirco/ecommerce_super/pull/156
[PR #157]: https://github.com/incirco/ecommerce_super/pull/157
[PR #159]: https://github.com/incirco/ecommerce_super/pull/159
[PR #163]: https://github.com/incirco/ecommerce_super/pull/163
[PR #164]: https://github.com/incirco/ecommerce_super/pull/164
[PR #165]: https://github.com/incirco/ecommerce_super/pull/165
[PR #167]: https://github.com/incirco/ecommerce_super/pull/167
[PR #168]: https://github.com/incirco/ecommerce_super/pull/168
[PR #172]: https://github.com/incirco/ecommerce_super/pull/172
[PR #174]: https://github.com/incirco/ecommerce_super/pull/174
[PR #177]: https://github.com/incirco/ecommerce_super/pull/177
[PR #179]: https://github.com/incirco/ecommerce_super/pull/179
[PR #182]: https://github.com/incirco/ecommerce_super/pull/182
[PR #185]: https://github.com/incirco/ecommerce_super/pull/185
[PR #189]: https://github.com/incirco/ecommerce_super/pull/189
[PR #190]: https://github.com/incirco/ecommerce_super/pull/190
[PR #191]: https://github.com/incirco/ecommerce_super/pull/191
[PR #192]: https://github.com/incirco/ecommerce_super/pull/192
[PR #193]: https://github.com/incirco/ecommerce_super/pull/193
[PR #194]: https://github.com/incirco/ecommerce_super/pull/194
[PR #195]: https://github.com/incirco/ecommerce_super/pull/195
[PR #196]: https://github.com/incirco/ecommerce_super/pull/196
[PR #198]: https://github.com/incirco/ecommerce_super/pull/198
[PR #202]: https://github.com/incirco/ecommerce_super/pull/202

[`1841623`]: https://github.com/incirco/ecommerce_super/commit/1841623
[`1a1d81a`]: https://github.com/incirco/ecommerce_super/commit/1a1d81a
[`200f780`]: https://github.com/incirco/ecommerce_super/commit/200f780
[`2111d9c`]: https://github.com/incirco/ecommerce_super/commit/2111d9c
[`3019ced`]: https://github.com/incirco/ecommerce_super/commit/3019ced
[`414d02e`]: https://github.com/incirco/ecommerce_super/commit/414d02e
[`464a54b`]: https://github.com/incirco/ecommerce_super/commit/464a54b
[`5ed7308`]: https://github.com/incirco/ecommerce_super/commit/5ed7308
[`6b3e62b`]: https://github.com/incirco/ecommerce_super/commit/6b3e62b
[`7495131`]: https://github.com/incirco/ecommerce_super/commit/7495131
[`76e1b10`]: https://github.com/incirco/ecommerce_super/commit/76e1b10
[`8a0a57a`]: https://github.com/incirco/ecommerce_super/commit/8a0a57a
[`8cdca09`]: https://github.com/incirco/ecommerce_super/commit/8cdca09
[`9707d28`]: https://github.com/incirco/ecommerce_super/commit/9707d28
[`9d7e596`]: https://github.com/incirco/ecommerce_super/commit/9d7e596
[`37f874f`]: https://github.com/incirco/ecommerce_super/commit/37f874f
[`2abc2b7`]: https://github.com/incirco/ecommerce_super/commit/2abc2b7
[`991c3a9`]: https://github.com/incirco/ecommerce_super/commit/991c3a9
[`4e7ed2f`]: https://github.com/incirco/ecommerce_super/commit/4e7ed2f
[`d69a44e`]: https://github.com/incirco/ecommerce_super/commit/d69a44e
[`790a1a9`]: https://github.com/incirco/ecommerce_super/commit/790a1a9
[`71f9559`]: https://github.com/incirco/ecommerce_super/commit/71f9559
[`7474382`]: https://github.com/incirco/ecommerce_super/commit/7474382
[`45c2001`]: https://github.com/incirco/ecommerce_super/commit/45c2001
[`e5167e3`]: https://github.com/incirco/ecommerce_super/commit/e5167e3
[`a1e66cf`]: https://github.com/incirco/ecommerce_super/commit/a1e66cf
[`ba8baac`]: https://github.com/incirco/ecommerce_super/commit/ba8baac
[`9b09ac5`]: https://github.com/incirco/ecommerce_super/commit/9b09ac5
[`91dc575`]: https://github.com/incirco/ecommerce_super/commit/91dc575
[`715d16f`]: https://github.com/incirco/ecommerce_super/commit/715d16f
[`a21b353`]: https://github.com/incirco/ecommerce_super/commit/a21b353
[`a60b2c6`]: https://github.com/incirco/ecommerce_super/commit/a60b2c6
[`a6d2eed`]: https://github.com/incirco/ecommerce_super/commit/a6d2eed
[`d130e33`]: https://github.com/incirco/ecommerce_super/commit/d130e33
[`edff222`]: https://github.com/incirco/ecommerce_super/commit/edff222
[`fd84aeb`]: https://github.com/incirco/ecommerce_super/commit/fd84aeb

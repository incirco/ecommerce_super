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

## [2026-07-13]

### Fixed

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
[`a21b353`]: https://github.com/incirco/ecommerce_super/commit/a21b353
[`a60b2c6`]: https://github.com/incirco/ecommerce_super/commit/a60b2c6
[`a6d2eed`]: https://github.com/incirco/ecommerce_super/commit/a6d2eed
[`d130e33`]: https://github.com/incirco/ecommerce_super/commit/d130e33
[`edff222`]: https://github.com/incirco/ecommerce_super/commit/edff222
[`fd84aeb`]: https://github.com/incirco/ecommerce_super/commit/fd84aeb

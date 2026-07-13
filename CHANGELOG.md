# Changelog

Everything worth deploying, keyed by date. Each entry names the underlying issue, the PR, and the commit SHA — GitHub auto-links all three. **Deploy actions** call out non-trivial migration or config steps; **Verification** names a one-line smoke test post-deploy where useful.

Format: [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Dates are IST.

The [Unreleased] section holds anything on `main` that hasn't yet been deployed to production sites.

---

## [Unreleased]

*Nothing pending — every entry below is on `main` and ready for the next `mmpl16.frappe.cloud` redeploy.*

---

## [2026-07-13]

### Added

- **Least-privilege `EasyEcom Integration` user + role** for `_elevated_session` to prefer over Administrator — gh#166 hardening, PR #168, a60b2c6. Role scoped to only the DocTypes the inbound GSP handler actually touches (Sales Invoice, DN, Payment Entry, Customer Map, Item Map, EE log DocTypes, IC log DocTypes). Explicitly excludes User, Role, System Settings, Server Script.
- **IP allowlist on Bearer usage** — new optional `EasyEcom Account.gsp_ip_allowlist` (comma-separated IPv4 / CIDR). Empty = no restriction. gh#166 hardening, PR #168.
- **Rate limit on inbound GSP calls** — new optional `EasyEcom Account.gsp_rate_limit_per_min` (default 6/min per invoice_id). Redis-backed via `frappe.cache`. Breach → HTTP 429. gh#166 hardening, PR #168.

### Fixed

- **`/einvoice/update` PermissionError under Guest session** — inbound GSP handlers now run under an elevated session for the SI insert/submit/mint chain, so third-party validate hooks (modernmarwar's `set_total_overdue_amount`, IC, etc.) that call `frappe.get_list` survive. gh#166, PR #167, 1a1d81a.

### Security

- **Bearer token TTL reduced 3600s → 900s** (1h → 15m). Shorter compromise window if a token leaks. EE re-mints transparently via `/gettoken` on expiry. PR #168.

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

- **"Re-fire EasyEcom Push" button on Sales Order form** — recovers from orphaned Queue Jobs (SO submitted but on_submit push didn't enqueue). Idempotent, role-gated, only visible on submitted SOs without a B2B Order Map. PR #165, 5ed7308.

### Fixed

- **§11 gh#141 detector** now reads the REAL Gate 0 resolver key (`EasyEcom Location.mapped_warehouse`) instead of the display-only `Warehouse.ecs_ee_location` field. Old detector produced wrong diagnostics + crashed on sites where the FK column wasn't materialised. gh#162, PR #164, 200f780.
- **Mirror SI Gate 3 (DN-mandatory)** — mirror now sets `si.update_stock=1` so India Compliance doesn't require a linked Delivery Note before e-invoicing. §11.5.1 Mode 1 is invoice-first. gh#160, PR #163, 9707d28.
- **Mirror SI Gate 3.5 (Due Date before Posting Date)** — mirror now pins `si.transaction_date = si.posting_date` and clears `si.payment_terms_template` before insert, so no downstream template can move due_date earlier. gh#161, PR #163, 9707d28.
- **Item Update push drops mandatory TaxRuleName** — sparse Update payload now always includes `productId`, `TaxRuleName`, `TaxRate`, `ProductTaxCode` regardless of diff. Unblocks §11 SO submits for items whose EE-side data hasn't changed since Create. gh#158, PR #159, 3019ced.
- **`_log_inbound_gsp_call` was silently rejected** by SQL — helper tried to read `EasyEcom Account.company` (no such column; company lives on `EasyEcom Location`). Dropped the lookup; company field is `reqd=None` on API Call anyway. gh#147 hotfix, PR #157, 2111d9c.

### Deploy actions

- `bench migrate` — three new patches land: `add_address_ee_c_id_field` (from gh#126), `backfill_api_call_direction` (gh#147), `backfill_customer_map_ee_c_id` (gh#144, via #145).

### Verification

- Re-push any Item that had failed with "TaxRuleName is a mandatory parameter" → HTTP 200; Item Map advances to `Mapped`.
- Re-fire `/einvoice/update` → Gate 3 + Gate 3.5 pass; SI submits.
- New EasyEcom API Call row shows `direction=Inbound` on every hit.

---

## [2026-07-10]

### Added

- **Inbound API Call logging** — new `direction` field on `EasyEcom API Call` (Outbound / Inbound, default Outbound for legacy rows). Every hit on `/gettoken`, `/einvoice/update`, `/ewaybill/update` now creates one row with request + response bodies (redacted headers), latency, and correlation_id. gh#147, PR #155, 414d02e.
- **Warehouse half-mapping detector + SO intent-gap detector** — non-throwing observability hooks. Warehouse.validate warns when `ecs_ee_location_label` is set but the FK is empty. SO.on_submit posts a timeline Comment when B2B intent signals combine with Gate 0 silent-inert rejection. gh#141, PR #156, 7495131.

### Fixed

- **§8e customer dedup via Party Alias** — §8e customer pull now uses natural-key dedup (mobile → gstin) to reuse an existing ERPNext Customer when EE issues a new `c_id` for the same real-world buyer. Prevents the Flagged-Not-Created spiral when EE assigns fresh c_ids on address changes. gh#126 (resolves gh#59), PR #139, 6b3e62b.
- **§11.5.1 inbound Sync Record write** — `_log_inbound_gsp_failure` was silently rejected on insert due to missing mandatory fields (`entity_doctype`, `entity_name`, `correlation_id`, `idempotency_key`, `attempts`). Now uses Sales Order + `reference_code` as the entity; skips cleanly when reference doesn't resolve. gh#143 followup, PR #146, 8cdca09.
- **§8e/§11 Customer push write-back of ee_c_id** — Update path now writes `ee_c_id` alongside `ee_customer_id` (previously only wrote `ee_customer_id`, leaving `ee_c_id` as a `flagged-<docname>` placeholder). Resolver falls back to `ee_customer_id` when `ee_c_id` doesn't match. One-shot backfill patch for existing sites. gh#144, PR #145, 76e1b10.
- **`/einvoice/update` error surfacing** — failures now return the real `message` (not bare `{"status":422}`), populate an Error Log entry with traceback + ee_row snapshot, and write an inbound Sync Record. Also accepts EE's actual body shape (`orders: {...}` as object, not just array). gh#142, PR #143, 8a0a57a.

### Deploy actions

- `bench migrate` — five new patches queued.

### Verification

- `EasyEcom API Call` list → filter `direction=Inbound` → every EE hit visible with full request/response bodies.
- Sync Record list → filter `direction=Inbound API, status=Failed` → inbound failures now aggregated by reference_code.
- Re-pull §8e customers on a site with duplicate map rows → dedup path fires, no new Flagged-Not-Created rows.

---

## [2026-07-09]

### Fixed

- **§11.5.1 gh#130 regression: bare 500 on root-path GSP calls** — the initial fix delattr'd `request.path`, which on this werkzeug version is a plain instance attribute (not a `cached_property`), so subsequent reads raised AttributeError and Frappe's exception handler cascaded. Fixed by direct assignment: `request.path = new_path`. gh#130 regression, PR #140, fd84aeb.
- **§11.5.1 gh#137: PDF-render failures now surface on SI timeline** — `_render_si_pdf_base64` failures now emit a Comment on the linked SI alongside the Error Log entry, so FDEs see the failure without hunting through logs. gh#137, PR #138, 1841623.
- **§12 B2C paginate `getAllOrders` properly** — was capturing only the first page; refactored to follow `data.nextUrl`. Captured 13.6× more orders on the smoke test. PR #119, 464a54b.

### Docs

- **OPS_upgrade_notes.md seeded** — first entry documents the Frappe CRM v16 upgrade note from the June 2026 release notes (gh#129). PR #136, a21b353.

---

## [2026-07-08]

### Added

- **§11.5.1 gh#134: populate `invoice_base64` and `eway_bill_base64`** in the GSP response — reliable PDF delivery to EE (URL-based fetch was auth-broken via session cookie trap). PR #135, edff222.

### Fixed

- **§10 gh#131: DN push coalesces empty `expDeliveryDate` to `posting_date`** — DN has no `delivery_date` field (that's on SO), so EE was receiving epoch 1970 dates. PR #133, d130e33.
- **§11.5.1 gh#130: EE calls root paths `/gettoken`, `/einvoice/update`, `/ewaybill/update`** — added `before_request` hook that rewrites WSGI `PATH_INFO` to the dotted `/api/method/...` URLs Frappe's router expects. Regression in this fix caught + shipped as gh#140 the next day (see 2026-07-09). PR #132, a6d2eed.

---

## Legend

- **Fixed** — bug fix; no schema change required unless called out
- **Added** — new capability; may involve new DocType, Custom Field, or button
- **Changed** — behavior change on an existing capability
- **Security** — token / auth / permissions change
- **Deploy actions** — anything beyond `bench migrate` (rotation, config toggle, credential regeneration)
- **Verification** — one-line smoke test to confirm the change is live and working
